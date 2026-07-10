"""``brutex lake audit-unique`` — the READ-ONLY lake uniqueness census CLI.

DEC-LAKE-HEAL-001 (§5.1): parser + ``main`` dispatch threading, the exit-code
contract (0 clean / 1 poisoned partitions found / 2 scan errored — the
``cmd_audit_verify`` semantics), the ``--json`` shape (the SAME dict the
``/api/v1/universe/lake-uniqueness`` route serves), the human table + the
engine-index (NIFTY/BANKNIFTY/SENSEX) verdict line, the coordinate-filter /
``--no-options`` scope bounds, and the READ-ONLY pin — the command never
mutates a byte of the lake, even a poisoned one.

Lakes are REAL Parquet trees under ``tmp_path`` (the repo convention): clean
partitions via the production :class:`~brutex.data.parquet_writer.ParquetWriter`,
poison planted with raw pyarrow (the fixed writer refuses duplicates).
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

pytest.importorskip("polars")
pytest.importorskip("pyarrow")

from brutex import cli
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter


def _bar(minute: int) -> CandleBar:
    """One valid September-2025 1-minute bar (04:0x UTC = inside the IST session)."""
    return CandleBar(
        timestamp=datetime(2025, 9, 1, 4, minute, tzinfo=UTC),
        open=Decimal("100.00"),
        high=Decimal("101.00"),
        low=Decimal("99.00"),
        close=Decimal("100.50"),
        volume=10,
        open_interest=0,
    )


def _seed_clean(
    lake_root: Path,
    *,
    exchange: str = "NSE",
    segment: str = "INDEX",
    symbol: str = "NIFTY",
) -> None:
    """Write one clean month partition through the production writer."""
    ParquetWriter(lake_root=lake_root).write(
        bars=[_bar(0), _bar(1)],
        exchange=exchange,
        segment=segment,
        symbol=symbol,
        interval="1minute",
    )


def _poison_partition(dest: Path) -> None:
    """Plant duplicate timestamps VERBATIM with raw pyarrow.

    The fixed writer refuses to store duplicates (DEC-LAKE-WRITE-UNIQUE-001),
    so poison is planted exactly like the operator's pre-fix lake.
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    dest.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime(2025, 9, 1, 4, 0, tzinfo=UTC)
    prices = [100.0, 100.0]
    table = pa.table(
        {
            "timestamp": pa.array([ts, ts], type=pa.timestamp("us", tz="UTC")),
            "open": pa.array(prices, type=pa.float64()),
            "high": pa.array(prices, type=pa.float64()),
            "low": pa.array(prices, type=pa.float64()),
            "close": pa.array(prices, type=pa.float64()),
            "volume": pa.array([10, 10], type=pa.int64()),
            "open_interest": pa.array([None, None], type=pa.int64()),
        }
    )
    pq.write_table(table, dest)


def _poison_bars(
    lake_root: Path,
    *,
    exchange: str = "NSE",
    segment: str = "INDEX",
    symbol: str = "NIFTY",
) -> Path:
    """Poison one ``bars/`` month partition; returns the poisoned file path."""
    dest = lake_root / "bars" / exchange / segment / symbol / "1minute" / "2025" / "09.parquet"
    _poison_partition(dest)
    return dest


def _poison_options(lake_root: Path) -> Path:
    """Plant one duplicate ``(side, timestamp)`` pair in the expired-options tree."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    dest = lake_root / "options" / "NSE-NIFTY-2025-09" / "2025-09.parquet"
    dest.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime(2025, 9, 1, 4, 0, tzinfo=UTC)
    table = pa.table(
        {
            "side": pa.array(["CE", "CE"]),
            "timestamp": pa.array([ts, ts], type=pa.timestamp("us", tz="UTC")),
        }
    )
    pq.write_table(table, dest)
    return dest


def _poison_options_foreign(lake_root: Path) -> Path:
    """Plant an OVERLAPPING foreign ``(side, timestamp)`` key in the options tree.

    The canonical September file holds the key; the October file carries a
    misplaced copy of it (foreign + overlap) alongside its own home row
    (DEC-LAKE-HEAL-OPTIONS-FOREIGN-001).
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    series = lake_root / "options" / "NSE-NIFTY-2025-09"
    series.mkdir(parents=True, exist_ok=True)
    sep_ts = datetime(2025, 9, 1, 4, 0, tzinfo=UTC)
    oct_ts = datetime(2025, 10, 1, 4, 0, tzinfo=UTC)
    pq.write_table(
        pa.table(
            {
                "side": pa.array(["CE"]),
                "timestamp": pa.array([sep_ts], type=pa.timestamp("us", tz="UTC")),
            }
        ),
        series / "2025-09.parquet",
    )
    dest = series / "2025-10.parquet"
    pq.write_table(
        pa.table(
            {
                "side": pa.array(["CE", "CE"]),
                "timestamp": pa.array([oct_ts, sep_ts], type=pa.timestamp("us", tz="UTC")),
            }
        ),
        dest,
    )
    return dest


def _tree_digest(root: Path) -> dict[str, str]:
    """sha256 of every file under *root*, keyed by relative path (the READ-ONLY oracle)."""
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


# ── parser + dispatch ────────────────────────────────────────────────────────


class TestParserAndDispatch:
    """The ``lake`` argparse group + ``main`` dispatch threading."""

    def test_parser_defaults(self) -> None:
        args = cli.build_parser().parse_args(["lake", "audit-unique"])
        assert args.command == "lake"
        assert args.lake_command == "audit-unique"
        assert args.lake_root is None
        assert args.as_json is False
        assert args.exchange is None
        assert args.segment is None
        assert args.symbol is None
        assert args.interval is None
        assert args.no_options is False

    def test_parser_accepts_all_flags(self) -> None:
        args = cli.build_parser().parse_args(
            [
                "lake",
                "audit-unique",
                "--lake-root",
                "/tmp/lake",
                "--json",
                "--exchange",
                "NSE",
                "--segment",
                "INDEX",
                "--symbol",
                "NIFTY",
                "--interval",
                "1minute",
                "--no-options",
            ]
        )
        assert args.lake_root == Path("/tmp/lake")
        assert args.as_json is True
        assert args.exchange == "NSE"
        assert args.segment == "INDEX"
        assert args.symbol == "NIFTY"
        assert args.interval == "1minute"
        assert args.no_options is True

    def test_main_dispatch_threads_all_kwargs(self) -> None:
        with mock.patch.object(cli, "cmd_lake_audit_unique", return_value=0) as cmd:
            rc = cli.main(
                [
                    "lake",
                    "audit-unique",
                    "--lake-root",
                    "/tmp/lake",
                    "--json",
                    "--exchange",
                    "NSE",
                    "--no-options",
                ]
            )
        assert rc == 0
        cmd.assert_called_once_with(
            lake_root=Path("/tmp/lake"),
            as_json=True,
            exchange="NSE",
            segment=None,
            symbol=None,
            interval=None,
            no_options=True,
        )

    def test_main_unknown_lake_subcommand_exits_via_parser_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A lake subcommand other than ``audit-unique`` routes to ``parser.error``.

        Real argv can never produce it (argparse rejects at parse time), so the
        defensive arm is reachable only by scripting ``parse_args`` — the
        ``test_cli_backfill._ScriptedParser`` convention.
        """
        errors: list[str] = []

        class _ScriptedParser:
            def parse_args(self, argv: list[str] | None = None) -> argparse.Namespace:
                del argv
                return argparse.Namespace(command="lake", lake_command="nope")

            def error(self, message: str) -> None:
                errors.append(message)
                raise SystemExit(2)

        scripted = _ScriptedParser()
        monkeypatch.setattr(cli, "build_parser", lambda: scripted)
        with pytest.raises(SystemExit) as excinfo:
            cli.main([])
        assert excinfo.value.code == 2
        assert errors == ["unknown lake subcommand: nope"]


# ── the command body ─────────────────────────────────────────────────────────


class TestCmdLakeAuditUnique:
    """Exit codes + output shapes over REAL tmp lakes."""

    def test_clean_lake_exits_ok_with_table_and_engine_verdict(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed_clean(tmp_path)
        rc = cli.main(["lake", "audit-unique", "--lake-root", str(tmp_path)])
        assert rc == cli.EXIT_OK
        out = capsys.readouterr().out
        assert f"lake audit-unique — {tmp_path}" in out
        assert "segment" in out and "drift-dirs" in out
        assert "INDEX" in out and "OPTIONS" in out
        assert "engine indexes (NIFTY / BANKNIFTY / SENSEX): CLEAN — " in out
        assert "0 findings over 1 files" in out
        assert "mode=" in out and "last_heal=never" in out

    def test_poisoned_lake_exits_user_error_with_poisoned_verdict(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _poison_bars(tmp_path)
        rc = cli.main(["lake", "audit-unique", "--lake-root", str(tmp_path)])
        assert rc == cli.EXIT_USER_ERROR
        out = capsys.readouterr().out
        assert "engine indexes (NIFTY / BANKNIFTY / SENSEX): POISONED — " in out
        assert "1 findings" in out

    def test_json_shape_matches_route_dict(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("BRUTEX_LAKE_HEAL", raising=False)
        _poison_bars(tmp_path)
        rc = cli.main(["lake", "audit-unique", "--lake-root", str(tmp_path), "--json"])
        assert rc == cli.EXIT_USER_ERROR
        out = capsys.readouterr().out
        # The scanner's structlog console lines precede the JSON on stdout;
        # the payload is the trailing pretty-printed object.
        payload = json.loads(out[out.index("{") :])
        assert payload["present"] is True
        assert payload["mode"] == "auto"
        assert payload["engine_clean"] is False
        assert payload["findings_total"] == 1
        segments = {row["segment"]: row for row in payload["segments"]}
        assert set(segments) == {"INDEX", "CASH", "FNO", "OPTIONS"}
        assert segments["INDEX"]["files"] == 1
        assert segments["INDEX"]["dup_files"] == 1
        assert segments["INDEX"]["clean"] == 0
        assert payload["healed_total"] == 0
        assert payload["quarantined_files_total"] == 0
        assert payload["last_heal_utc"] is None
        assert payload["last_sweep_utc"] is None

    def test_read_only_pin_tree_bytes_unchanged(self, tmp_path: Path) -> None:
        """The census NEVER mutates the lake — byte-pinned even over a poisoned tree."""
        _seed_clean(tmp_path)
        _poison_bars(tmp_path, segment="CASH", symbol="RELIANCE")
        _poison_options(tmp_path)
        before = _tree_digest(tmp_path)
        rc = cli.main(["lake", "audit-unique", "--lake-root", str(tmp_path)])
        assert rc == cli.EXIT_USER_ERROR
        assert _tree_digest(tmp_path) == before

    def test_scan_error_exits_internal_error(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from brutex.data import lake_invariants

        def _boom(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("boom")

        monkeypatch.setattr(lake_invariants, "scan_lake_invariants", _boom)
        rc = cli.main(["lake", "audit-unique", "--lake-root", str(tmp_path)])
        assert rc == cli.EXIT_INTERNAL_ERROR
        assert "lake audit-unique scan errored: boom" in capsys.readouterr().err

    def test_options_foreign_overlap_render_real_numbers(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The OPTIONS row shows REAL foreign/overlap counts (drift stays "-")
        and an options-foreign-ONLY lake exits 1
        (DEC-LAKE-HEAL-OPTIONS-FOREIGN-001 closed the "-" placeholders)."""
        _seed_clean(tmp_path)
        _poison_options_foreign(tmp_path)
        rc = cli.main(["lake", "audit-unique", "--lake-root", str(tmp_path)])
        assert rc == cli.EXIT_USER_ERROR  # options-foreign is the ONLY finding
        out = capsys.readouterr().out
        (options_line,) = [line for line in out.splitlines() if line.startswith("OPTIONS")]
        cells = options_line.split()
        # segment / files / clean / dup / foreign / overlap / drift-dirs
        assert cells == ["OPTIONS", "2", "1", "0", "1", "1", "-"]
        # The JSON twin carries the same real numbers.
        capsys.readouterr()
        rc = cli.main(["lake", "audit-unique", "--lake-root", str(tmp_path), "--json"])
        assert rc == cli.EXIT_USER_ERROR
        out = capsys.readouterr().out
        payload = json.loads(out[out.index("{") :])
        options_row = next(row for row in payload["segments"] if row["segment"] == "OPTIONS")
        assert options_row["foreign_files"] == 1
        assert options_row["overlap_files"] == 1
        assert options_row["dup_files"] == 0
        assert options_row["clean"] == 1

    def test_no_options_flag_skips_the_options_tree(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A poisoned options file flips the verdict — unless ``--no-options`` bounds it out."""
        _seed_clean(tmp_path)
        _poison_options(tmp_path)
        assert cli.main(["lake", "audit-unique", "--lake-root", str(tmp_path)]) == (
            cli.EXIT_USER_ERROR
        )
        capsys.readouterr()
        rc = cli.main(["lake", "audit-unique", "--lake-root", str(tmp_path), "--no-options"])
        assert rc == cli.EXIT_OK
        out = capsys.readouterr().out
        assert "engine indexes (NIFTY / BANKNIFTY / SENSEX): CLEAN — " in out

    def test_coordinate_filters_bound_the_scan(self, tmp_path: Path) -> None:
        """``--exchange`` scopes the verdict: BSE clean while NSE carries the poison."""
        _seed_clean(tmp_path, exchange="BSE", segment="INDEX", symbol="SENSEX")
        _poison_bars(tmp_path, segment="CASH", symbol="RELIANCE")
        assert cli.main(["lake", "audit-unique", "--lake-root", str(tmp_path)]) == (
            cli.EXIT_USER_ERROR
        )
        rc = cli.main(["lake", "audit-unique", "--lake-root", str(tmp_path), "--exchange", "BSE"])
        assert rc == cli.EXIT_OK

    def test_heal_stats_and_sentinel_render_in_both_outputs(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """`.lake_heal/` stats + sentinel reads land on the table footer AND the JSON."""
        from brutex.data.lake_heal import SENTINEL_RELPATH, STATS_RELPATH

        _seed_clean(tmp_path)
        stats_path = tmp_path / STATS_RELPATH
        stats_path.parent.mkdir(parents=True, exist_ok=True)
        stats_path.write_text(
            json.dumps(
                {
                    "healed_partitions_total": 7,
                    "quarantined_files_total": 9,
                    "last_heal_utc": "2026-07-03T04:11:02Z",
                }
            ),
            encoding="utf-8",
        )
        (tmp_path / SENTINEL_RELPATH).write_text(
            json.dumps({"version": 1, "completed_utc": "2026-07-03T05:00:00Z"}),
            encoding="utf-8",
        )
        rc = cli.main(["lake", "audit-unique", "--lake-root", str(tmp_path)])
        assert rc == cli.EXIT_OK
        out = capsys.readouterr().out
        assert "healed_total=7" in out
        assert "quarantined_files_total=9" in out
        assert "last_heal=2026-07-03T04:11:02Z" in out
        rc = cli.main(["lake", "audit-unique", "--lake-root", str(tmp_path), "--json"])
        assert rc == cli.EXIT_OK
        out = capsys.readouterr().out
        payload = json.loads(out[out.index("{") :])
        assert payload["healed_total"] == 7
        assert payload["last_heal_utc"] == "2026-07-03T04:11:02Z"
        assert payload["last_sweep_utc"] == "2026-07-03T05:00:00Z"
