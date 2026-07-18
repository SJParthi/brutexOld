"""Integration — the ``brutex meta-annotate`` CLI end-to-end
(DEC-META-LABEL-ANNOTATE-001 + DEC-ABSTAIN-GATE-STREAM-001).

Two chains, both over REAL writers:

* the CLI (``cli.main`` argv → ``cmd_meta_annotate`` → ``annotate_run``)
  over a real-``write_tuple_sidecars`` run — the fourth sidecar + run
  summary land, a re-run is BYTE-IDENTICAL, ``--dry-run`` writes NOTHING
  (whole-tree digest pinned), pre-existing artifacts stay byte-untouched,
  and an unknown run id is a clean user error;
* the REAL sweep chain — ``cmd_sweep_all`` over a tiny real lake, then
  ``meta-annotate`` over the run it produced (the production sidecar
  shapes, lens arm included) — plus the DORMANT in-sim gate refusal: a
  set ``BRUTEX_ABSTAIN_GATE_INSIM`` refuses the sweep loudly BEFORE any
  artifact lands (the ``BRUTEX_FILL_MODE`` set-knob precedent), and the
  unset default launches normally.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("polars")
pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")
pytest.importorskip("lmdb")

from brutex import cli
from brutex.cli import EXIT_INTERNAL_ERROR, EXIT_OK, EXIT_USER_ERROR, cmd_sweep_all
from brutex.data.catalog import Catalog
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.sink import ParquetBarSink
from brutex.engine.minute_path import PathStatus
from brutex.engine.trade_capture import CapturedTrade, ComboCapture
from brutex.engine.trade_store import sidecar_paths_for, write_tuple_sidecars
from brutex.meta.abstain import ABSTAIN_GATE_INSIM_ENV
from brutex.meta.annotate import META_SIDECAR_SUFFIX, META_SUMMARY_FILENAME

_IST = timezone(timedelta(hours=5, minutes=30))
RUN_ID = "cli-meta-run"
COMBO = "combo-" + "c" * 10
BASE_ENTRY = datetime(2024, 1, 2, 4, 0, tzinfo=UTC)


def _capture(n: int = 40) -> ComboCapture:
    trades = []
    for i in range(n):
        entry = BASE_ENTRY + timedelta(days=i % 200, minutes=7 * (i % 13))
        pnl = 500 if (i * 7) % 3 == 0 else -400
        trades.append(
            CapturedTrade(
                entry_ts=entry,
                entry_px=Decimal("100.00"),
                exit_ts=entry + timedelta(minutes=30 + (i % 5)),
                exit_px=Decimal("101.00"),
                direction=cli_side(),
                lots=None,
                exit_reason="take_profit" if pnl > 0 else "stop_loss",
                pnl_paisa=pnl,
                costs_paisa=None,
                mae_paisa=10,
                mfe_paisa=20,
                mae_1m_paisa=None,
                mfe_1m_paisa=None,
                path_1m_status=PathStatus.NO_1M_DATA,
                vix_entry=Decimal("14.5") if i % 4 else None,
                vix_exit=None,
            )
        )
    return ComboCapture(
        run_id=COMBO,
        trades=tuple(trades),
        equity=(),
        equity_status=PathStatus.NO_1M_DATA,
        vix_missing_trades=sum(1 for t in trades if t.vix_entry is None),
        parity_ok=True,
        parity_detail="",
    )


def cli_side() -> Any:
    from brutex.strategies.types import Side

    return Side.BUY


def _seed_run(lake: Path) -> Path:
    """A real-writer sidecar trio under ``<lake>/sweeps``; returns the lake."""
    lake.mkdir(parents=True, exist_ok=True)
    sweeps = lake / "sweeps"
    store = sweeps / "NSE-NIFTY" / "5minute" / "tuple-cli.parquet"
    store.parent.mkdir(parents=True, exist_ok=True)
    store.touch()
    paths = sidecar_paths_for(store, sweeps, run_id=RUN_ID)
    write_tuple_sidecars(paths, [_capture()], [], store_content_hash="c" * 64)
    return lake


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        digest.update(str(path.relative_to(root)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _artifacts(lake: Path) -> tuple[Path, Path]:
    run_dir = lake / "sweep_runs" / RUN_ID
    meta = run_dir / "results-depth" / "NSE-NIFTY" / "5minute" / f"tuple-cli{META_SIDECAR_SUFFIX}"
    return meta, run_dir / META_SUMMARY_FILENAME


class TestCliOverSeededRun:
    def test_end_to_end_writes_sidecar_and_summary(self, tmp_path: Path) -> None:
        lake = _seed_run(tmp_path / "lake")
        rc = cli.main(["meta-annotate", "--lake-root", str(lake), "--run-id", RUN_ID, "--no-lens"])
        assert rc == EXIT_OK
        meta, summary = _artifacts(lake)
        assert meta.is_file() and summary.is_file()
        payload = json.loads(meta.read_text(encoding="utf-8"))
        combo = payload["combos"][COMBO]
        assert combo["n_trades"] == 40
        # 40 non-overlapping-ish trades → the REAL OOF fit engages.
        assert combo["oof"]["degenerate_reason"] is None
        assert any(p is not None for p in combo["p_win"].values())
        assert json.loads(summary.read_text(encoding="utf-8"))["stems_annotated"] == 1

    def test_rerun_is_byte_identical(self, tmp_path: Path) -> None:
        lake = _seed_run(tmp_path / "lake")
        argv = ["meta-annotate", "--lake-root", str(lake), "--run-id", RUN_ID, "--no-lens"]
        assert cli.main(argv) == EXIT_OK
        meta, summary = _artifacts(lake)
        first = (meta.read_bytes(), summary.read_bytes())
        assert cli.main(argv) == EXIT_OK
        assert (meta.read_bytes(), summary.read_bytes()) == first

    def test_dry_run_writes_nothing(self, tmp_path: Path) -> None:
        lake = _seed_run(tmp_path / "lake")
        before = _tree_digest(lake)
        rc = cli.main(["meta-annotate", "--lake-root", str(lake), "--run-id", RUN_ID, "--dry-run"])
        assert rc == EXIT_OK
        assert _tree_digest(lake) == before

    def test_pre_existing_artifacts_stay_byte_untouched(self, tmp_path: Path) -> None:
        lake = _seed_run(tmp_path / "lake")
        pre_existing = {p: p.read_bytes() for p in lake.rglob("*") if p.is_file()}
        assert (
            cli.main(["meta-annotate", "--lake-root", str(lake), "--run-id", RUN_ID, "--no-lens"])
            == EXIT_OK
        )
        for path, raw in pre_existing.items():
            assert path.read_bytes() == raw, f"pre-existing artifact rewritten: {path}"

    def test_unknown_run_id_is_a_clean_user_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        lake = _seed_run(tmp_path / "lake")
        rc = cli.main(["meta-annotate", "--lake-root", str(lake), "--run-id", "no-such-run"])
        assert rc == EXIT_USER_ERROR
        assert "no run directory" in capsys.readouterr().err

    def test_missing_lake_root_is_a_clean_user_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = cli.main(
            ["meta-annotate", "--lake-root", str(tmp_path / "absent"), "--run-id", RUN_ID]
        )
        assert rc == EXIT_USER_ERROR

    def test_malformed_lens_date_is_a_clean_user_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        lake = _seed_run(tmp_path / "lake")
        rc = cli.main(
            [
                "meta-annotate",
                "--lake-root",
                str(lake),
                "--run-id",
                RUN_ID,
                "--lens-start",
                "not-a-date",
            ]
        )
        assert rc == EXIT_USER_ERROR
        err = capsys.readouterr().err
        assert "meta-annotate:" in err
        # Round-3 I3-2: the error names the flag the operator actually
        # typed, never the shared helper's --start/--end wording.
        assert "--lens-start" in err
        assert "--start/--end" not in err

    def test_malformed_lens_end_names_the_lens_end_flag(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        lake = _seed_run(tmp_path / "lake")
        rc = cli.main(
            [
                "meta-annotate",
                "--lake-root",
                str(lake),
                "--run-id",
                RUN_ID,
                "--lens-end",
                "31-12-2024",
            ]
        )
        assert rc == EXIT_USER_ERROR
        err = capsys.readouterr().err
        assert "--lens-end" in err
        assert "--start/--end" not in err

    def test_explicit_lens_end_is_honored(self, tmp_path: Path) -> None:
        lake = _seed_run(tmp_path / "lake")
        rc = cli.main(
            [
                "meta-annotate",
                "--lake-root",
                str(lake),
                "--run-id",
                RUN_ID,
                "--no-lens",
                "--lens-end",
                "2024-12-31",
            ]
        )
        assert rc == EXIT_OK
        meta, _summary = _artifacts(lake)
        assert meta.is_file()

    def test_skipped_stem_reads_internal_error_and_names_the_reason(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # A corrupt stamp is a COUNTED skip (correct-or-absent): the CLI exits
        # EXIT_INTERNAL_ERROR and names the stem + reason on stderr.
        lake = _seed_run(tmp_path / "lake")
        stamp = lake / "sweep_runs" / RUN_ID / "results-depth" / "NSE-NIFTY" / "5minute"
        (next(stamp.glob("*.stamp.json"))).write_text("{not json", encoding="utf-8")
        rc = cli.main(["meta-annotate", "--lake-root", str(lake), "--run-id", RUN_ID, "--no-lens"])
        assert rc == EXIT_INTERNAL_ERROR
        err = capsys.readouterr().err
        assert "SKIPPED" in err
        assert "stamp_invalid" in err
        meta, _summary = _artifacts(lake)
        assert not meta.exists()


class TestRunIdContainment:
    """Round-3 I3-1: a path-escaping ``--run-id`` is refused BEFORE any join.

    ``sweep_run_dir`` joins the id verbatim, so pre-fix an absolute or
    ``..``-carrying ``--run-id`` whose target dir pre-existed WROTE
    ``meta_annotation.json`` OUTSIDE ``sweep_runs/`` (the executed refuter
    repro shapes) — both shapes are pinned refused + NOTHING-written, and
    a plain id still annotates (the happy-path guard).
    """

    def test_absolute_run_id_refused_and_nothing_written(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        lake = _seed_run(tmp_path / "lake")
        escape = tmp_path / "escape_abs"
        escape.mkdir()  # the pre-existing target of the executed repro
        rc = cli.main(
            ["meta-annotate", "--lake-root", str(lake), "--run-id", str(escape), "--no-lens"]
        )
        assert rc == EXIT_USER_ERROR
        err = capsys.readouterr().err
        assert "--run-id" in err
        assert list(escape.iterdir()) == []

    def test_dotdot_run_id_refused_and_nothing_written(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        lake = _seed_run(tmp_path / "lake")
        # sweep_run_dir joins <lake>/sweep_runs/<run_id>, so "../escape_rel"
        # would land at <lake>/escape_rel — pre-create it (the repro shape).
        escape = lake / "escape_rel"
        escape.mkdir()
        rc = cli.main(
            ["meta-annotate", "--lake-root", str(lake), "--run-id", "../escape_rel", "--no-lens"]
        )
        assert rc == EXIT_USER_ERROR
        assert "--run-id" in capsys.readouterr().err
        assert list(escape.iterdir()) == []

    def test_windows_style_and_bare_dotdot_run_ids_refused(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The bare ".." is the one component shape with no separator — it
        # exercises the final `..`-component arm of _run_id_escapes.
        lake = _seed_run(tmp_path / "lake")
        for bad in (r"..\escape", r"C:\escape", "a\\b", ".."):
            rc = cli.main(["meta-annotate", "--lake-root", str(lake), "--run-id", bad, "--no-lens"])
            assert rc == EXIT_USER_ERROR, bad
            assert "--run-id" in capsys.readouterr().err

    def test_plain_run_id_still_annotates(self, tmp_path: Path) -> None:
        # The happy-path guard: containment never blocks a legitimate id.
        lake = _seed_run(tmp_path / "lake")
        rc = cli.main(["meta-annotate", "--lake-root", str(lake), "--run-id", RUN_ID, "--no-lens"])
        assert rc == EXIT_OK
        meta, _summary = _artifacts(lake)
        assert meta.is_file()


# ── the REAL sweep chain + the dormant in-sim gate refusal ──────────────────

_EXCHANGE = "NSE"
_SEGMENT = "INDEX"


@pytest.fixture(autouse=True)
def _sequential_sweep_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRUTEX_SWEEP_WORKERS", "1")


def _bars(n: int = 30) -> list[CandleBar]:
    out: list[CandleBar] = []
    for i in range(n):
        ts = datetime(2024, 1, 2, 9, 16 + i, tzinfo=_IST).astimezone(UTC)
        close = Decimal("100.00") + Decimal(i % 7) / 10
        out.append(
            CandleBar(
                timestamp=ts,
                open=close - Decimal("0.05"),
                high=close + Decimal("0.25"),
                low=close - Decimal("0.30"),
                close=close,
                volume=1000,
                open_interest=None,
            )
        )
    return out


def _seed_lake(tmp_path: Path) -> tuple[Path, Path]:
    lake_root = tmp_path / "lake"
    catalog_path = tmp_path / "catalog.duckdb"
    writer = ParquetWriter(lake_root=lake_root)
    with Catalog.open(catalog_path) as cat:
        sink = ParquetBarSink(writer=writer, catalog=cat)
        sink.append(
            exchange=_EXCHANGE,
            segment=_SEGMENT,
            symbol="NIFTY",
            interval="1minute",
            bars=_bars(),
        )
    return lake_root, catalog_path


def _sweep_kwargs(lake_root: Path, catalog_path: Path, tmp_path: Path) -> dict[str, Any]:
    return {
        "lake_root": lake_root,
        "catalog_path": catalog_path,
        "results_root": tmp_path / "sweeps",
        "exchange": _EXCHANGE,
        "segment": _SEGMENT,
        "intervals": "1minute",
        "start": "2024-01-02",
        "end": "2024-01-03",
        "directions": "BUY",
        "k": "1",
        "conditions": "0,1",
        "sl": "none",
        "tp": "none",
        "top": 5,
        "mode": "A",
        "max_tuples": None,
        "max_combos": None,
        "data_snapshot_hash": None,
        "options_sim": False,
    }


class TestInSimGateRefusal:
    def test_set_env_refuses_the_sweep_before_any_artifact(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        lake_root, catalog_path = _seed_lake(tmp_path)
        monkeypatch.setenv(ABSTAIN_GATE_INSIM_ENV, "1")
        rc = cmd_sweep_all(symbols="NIFTY", **_sweep_kwargs(lake_root, catalog_path, tmp_path))
        assert rc == EXIT_USER_ERROR
        err = capsys.readouterr().err
        assert ABSTAIN_GATE_INSIM_ENV in err
        assert "DEC-ABSTAIN-GATE-STREAM-001" in err
        assert not (tmp_path / "sweep_runs").exists()
        assert not list((tmp_path / "sweeps").rglob("*.parquet"))

    def test_unset_env_sweeps_then_meta_annotate_chains(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lake_root, catalog_path = _seed_lake(tmp_path)
        monkeypatch.delenv(ABSTAIN_GATE_INSIM_ENV, raising=False)
        rc = cmd_sweep_all(symbols="NIFTY", **_sweep_kwargs(lake_root, catalog_path, tmp_path))
        assert rc == EXIT_OK
        run_dirs = sorted((tmp_path / "sweep_runs").iterdir())
        assert len(run_dirs) == 1
        run_id = run_dirs[0].name
        # N-4 / the R13 zero-risk law: the sweep's OWN top100.json bytes
        # must survive meta-annotate untouched (snapshot BEFORE, compare
        # AFTER — never inferred from the generic whole-tree pin alone).
        top100_path = run_dirs[0] / "top100.json"
        assert top100_path.is_file()
        top100_before = top100_path.read_bytes()
        rc = cli.main(
            [
                "meta-annotate",
                "--lake-root",
                str(lake_root),
                "--results-root",
                str(tmp_path / "sweeps"),
                "--run-id",
                run_id,
            ]
        )
        assert rc == EXIT_OK
        assert top100_path.read_bytes() == top100_before
        summary = json.loads(
            (tmp_path / "sweep_runs" / run_id / META_SUMMARY_FILENAME).read_text(encoding="utf-8")
        )
        assert summary["sweep_run_id"] == run_id
        assert summary["stems_skipped"] == 0
        # Anti-vacuous: the tiny real sweep genuinely produced a results-depth
        # trades sidecar (0-row is legal — the pass annotates the stem).
        assert summary["stems_total"] >= 1
        assert summary["stems_total"] == summary["stems_annotated"]
