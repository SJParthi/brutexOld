"""CLI integration — the corporate-action EXTENSION of the sweep scope gate.

DEC-CORP-ACTION-GATE-001 / ADR-0016, extending DEC-ENGINE-SCOPE-GUARD-001
(the base gate's own end-to-end suite is ``test_cli_sweep_scope.py`` — this
file deliberately does NOT re-prove refusal-before-lake-I/O / audit stamping /
daemon-flag refusal; it pins only what the extension ADDS):

* the hard refusal at every launch mouth now carries the corporate-action
  rationale — the literal phrase "corporate-action adjustment layer missing"
  + BOTH decision ids + ADR-0016;
* the ``--allow-out-of-scope`` research escape emits ONE loud
  ``sweep.scope_override_hazard`` structlog WARNING through the real CLI
  path — no override can silently produce split-corrupted results;
* ``sweep-verify --recount`` (which EXECUTES the real ``run_sweep_grid`` into
  ``--scratch-root`` — the extension's hostile-review MAJOR) runs the SAME
  ``check_sweep_scope`` gate with the SAME escape semantics; Tier 1/2 stay
  unguarded for a legacy out-of-scope tree.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.cli import EXIT_OK, EXIT_USER_ERROR, main
from brutex.data.catalog import Catalog
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.sink import ParquetBarSink
from structlog.testing import capture_logs

pytest.importorskip("polars")
pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")
pytest.importorskip("lmdb")

_PHRASE = "corporate-action adjustment layer missing"
_IST = timezone(timedelta(hours=5, minutes=30))


def _persist_vix(tmp_path: Path) -> tuple[Path, Path]:
    """Seed a tiny INDIAVIX 1-minute lake; return (lake_root, catalog_path)."""
    lake_root = tmp_path / "lake"
    catalog_path = tmp_path / "catalog.duckdb"
    bars = [
        CandleBar(
            timestamp=datetime(2024, 1, 2, 9, 16 + i, tzinfo=_IST).astimezone(UTC),
            open=Decimal("100.00"),
            high=Decimal("102.00"),
            low=Decimal("99.00"),
            close=Decimal("100.00") + Decimal(i) / 10,
            volume=1000,
            open_interest=None,
        )
        for i in range(8)
    ]
    writer = ParquetWriter(lake_root=lake_root)
    with Catalog.open(catalog_path) as cat:
        sink = ParquetBarSink(writer=writer, catalog=cat)
        sink.append(
            exchange="NSE", segment="INDEX", symbol="INDIAVIX", interval="1minute", bars=bars
        )
    return lake_root, catalog_path


def _sweep_argv(symbol: str, tmp_path: Path) -> list[str]:
    return [
        "sweep",
        symbol,
        "--start",
        "2024-01-02",
        "--end",
        "2024-01-03",
        "--interval",
        "1minute",
        "--lake-root",
        str(tmp_path / "lake"),
    ]


def _sweep_all_argv(symbols: str, tmp_path: Path) -> list[str]:
    return [
        "sweep-all",
        "--symbols",
        symbols,
        "--intervals",
        "1minute",
        "--start",
        "2024-01-02",
        "--end",
        "2024-01-03",
        "--lake-root",
        str(tmp_path / "lake"),
    ]


class TestRefusalCarriesTheCorpActionRationale:
    """The DEFAULT (no flag) hard refusal names the hazard + both DEC ids."""

    def test_sweep_refusal_names_the_hazard_and_both_dec_ids(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = main(_sweep_argv("RELIANCE", tmp_path))
        assert rc == EXIT_USER_ERROR
        err = capsys.readouterr().err
        assert _PHRASE in err
        assert "DEC-CORP-ACTION-GATE-001" in err
        assert "DEC-ENGINE-SCOPE-GUARD-001" in err
        assert "ADR-0016" in err
        assert "RELIANCE" in err
        assert "Traceback" not in err

    def test_sweep_all_refusal_names_the_hazard(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = main(_sweep_all_argv("NIFTY,RELIANCE", tmp_path))
        assert rc == EXIT_USER_ERROR
        err = capsys.readouterr().err
        assert _PHRASE in err
        assert "ADR-0016" in err
        # Only the offender is listed as refused (NIFTY passes the law).
        assert "- RELIANCE:" in err
        assert "- NIFTY:" not in err

    def test_in_scope_symbol_proceeds_to_no_bars_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # NIFTY passes the gate; the empty lake then yields the EXISTING
        # honest no-bars user error (the extension changed nothing in scope).
        rc = main(_sweep_argv("NIFTY", tmp_path))
        assert rc == EXIT_USER_ERROR
        err = capsys.readouterr().err
        assert "no bars for NIFTY" in err
        assert _PHRASE not in err


class TestOverrideEmitsTheHazardWarning:
    """The research escape is LOUD end-to-end: one hazard event per launch."""

    def test_sweep_all_override_emits_one_hazard_event(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        lake_root, catalog_path = _persist_vix(tmp_path)
        with capture_logs() as logs:
            rc = main(
                [
                    "sweep-all",
                    "--symbols",
                    "INDIAVIX",
                    "--intervals",
                    "1minute",
                    "--start",
                    "2024-01-02",
                    "--end",
                    "2024-01-03",
                    "--directions",
                    "BUY",
                    "--k",
                    "1",
                    "--conditions",
                    "0,1",
                    "--lake-root",
                    str(lake_root),
                    "--catalog",
                    str(catalog_path),
                    "--results-root",
                    str(tmp_path / "sweeps"),
                    "--allow-out-of-scope",
                ]
            )
        assert rc == EXIT_OK
        assert "WARNING" in capsys.readouterr().out
        hazards = [e for e in logs if e.get("event") == "sweep.scope_override_hazard"]
        assert len(hazards) == 1
        assert hazards[0]["symbols"] == ["INDIAVIX"]
        assert _PHRASE in hazards[0]["hazard"]
        assert "DEC-CORP-ACTION-GATE-001" in hazards[0]["hazard"]
        assert "ADR-0016" in hazards[0]["hazard"]


def _sweep_verify_argv(symbols: str, tmp_path: Path, *, recount: bool, override: bool) -> list[str]:
    """A sweep-verify invocation with a recorded context supplied via overrides.

    The results tree exists but is EMPTY (Tier 1 CLEAN, zero stores) so the
    run reaches the recount tier without any real sweep artifacts; the three
    context overrides stand in for the audit log's recorded context.
    """
    tree = tmp_path / "tree"
    tree.mkdir(exist_ok=True)
    argv = [
        "sweep-verify",
        "--results-root",
        str(tree),
        "--symbols",
        symbols,
        "--intervals",
        "1minute",
        "--start",
        "2024-01-02",
        "--end",
        "2024-01-03",
        "--data-snapshot-hash",
        "recorded-snapshot",
        "--indicator-version",
        "recorded-indicator",
        "--code-commit-sha",
        "recorded-sha",
        "--lake-root",
        str(tmp_path / "lake"),
        "--scratch-root",
        str(tmp_path / "scratch"),
    ]
    if recount:
        argv += ["--recount", "--allow-context-mismatch"]
    if override:
        argv += ["--allow-out-of-scope"]
    return argv


class TestSweepVerifyRecountGate:
    """Tier-3 --recount EXECUTES the real engine, so it is scope-gated.

    The extension's hostile-review MAJOR: before the gate, ``sweep-verify
    --symbols RELIANCE --recount --allow-context-mismatch`` ran a REAL
    single-stock ``run_sweep_grid`` and persisted its stores into an
    operator-chosen ``--scratch-root`` — the exact silently-corrupt
    computation DEC-CORP-ACTION-GATE-001 exists to block. The gate is the
    SAME ``check_sweep_scope`` with the SAME ``--allow-out-of-scope``
    semantics as sweep/sweep-all (DEC-ENGINE-SCOPE-GUARD-001), so a legacy
    out-of-scope tree stays recount-verifiable LOUDLY, never silently.
    """

    def test_recount_out_of_scope_refused_before_any_engine_work(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = main(_sweep_verify_argv("RELIANCE", tmp_path, recount=True, override=False))
        assert rc == EXIT_USER_ERROR
        err = capsys.readouterr().err
        assert "sweep-verify --recount:" in err
        assert _PHRASE in err
        assert "RELIANCE" in err
        assert "Traceback" not in err
        # No engine work happened: no scratch stores, no lake/catalog conjured.
        assert not (tmp_path / "scratch").exists()
        assert not (tmp_path / "lake").exists()

    def test_allow_context_mismatch_is_not_a_scope_bypass(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The refusal above already ran WITH --allow-context-mismatch; this
        # pins the flag's honest contract explicitly: context override only.
        rc = main(_sweep_verify_argv("INDIAVIX", tmp_path, recount=True, override=False))
        assert rc == EXIT_USER_ERROR
        assert _PHRASE in capsys.readouterr().err

    def test_recount_override_proceeds_loudly_to_the_no_bars_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # --allow-out-of-scope lets the legacy out-of-scope recount through
        # the scope gate LOUDLY (override WARNING + the hazard structlog
        # event); the empty lake then hits the existing no-bars error INSIDE
        # the recount tier — proving the launch got past the gate.
        with capture_logs() as logs:
            rc = main(_sweep_verify_argv("INDIAVIX", tmp_path, recount=True, override=True))
        captured = capsys.readouterr()
        assert rc == EXIT_USER_ERROR
        assert "no bars for any" in captured.err
        assert "--allow-out-of-scope OVERRIDE" in captured.out
        hazards = [e for e in logs if e.get("event") == "sweep.scope_override_hazard"]
        assert len(hazards) == 1
        assert hazards[0]["symbols"] == ["INDIAVIX"]
        assert _PHRASE in hazards[0]["hazard"]
        overrides = [e for e in logs if e.get("event") == "sweep.scope_override"]
        assert len(overrides) == 1

    def test_report_tiers_stay_unguarded_for_a_legacy_out_of_scope_tree(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Tier 1/2 run no engine — a legacy out-of-scope tree stays
        # report-verifiable WITHOUT any flag (the documented trade-off in
        # ADR-0016 §3 / the sweep_scope module docstring).
        rc = main(_sweep_verify_argv("RELIANCE", tmp_path, recount=False, override=False))
        captured = capsys.readouterr()
        assert rc == 0
        assert _PHRASE not in captured.err
        assert _PHRASE not in captured.out

    def test_recount_in_scope_proceeds_past_the_gate(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # NIFTY passes the gate; the empty lake then hits the EXISTING honest
        # "no bars for any" user error INSIDE the recount tier.
        rc = main(_sweep_verify_argv("NIFTY", tmp_path, recount=True, override=False))
        assert rc == EXIT_USER_ERROR
        err = capsys.readouterr().err
        assert "no bars for any" in err
        assert _PHRASE not in err
