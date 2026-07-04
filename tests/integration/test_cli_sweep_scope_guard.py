"""CLI integration for the engine scope gate (DEC-CORP-ACTION-GATE-001 / ADR-0016).

Proves the guard at both production sweep entries:
- ``brutex sweep RELIANCE …`` exits ``EXIT_USER_ERROR`` with the refusal
  message (never a raw traceback) BEFORE any lake I/O;
- ``brutex sweep-all --symbols NIFTY,RELIANCE …`` is refused naming ONLY the
  offender, and an in-scope ``--symbols NIFTY,BANKNIFTY`` proceeds PAST the
  guard into the existing no-bars/preflight handling;
- ``sweep-daemon`` and the dashboard 🚀 Run Sweep button reuse the SAME
  ``cmd_sweep_all`` chokepoint verbatim, so these two entries cover all four
  operator surfaces.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from brutex.cli import EXIT_USER_ERROR, main

pytest.importorskip("polars")
pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")

_PHRASE = "corporate-action adjustment layer missing"


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


class TestSweepSingleTupleGate:
    def test_out_of_scope_stock_is_clean_user_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = main(_sweep_argv("RELIANCE", tmp_path))
        assert rc == EXIT_USER_ERROR
        err = capsys.readouterr().err
        assert _PHRASE in err
        assert "DEC-CORP-ACTION-GATE-001" in err
        assert "'RELIANCE'" in err
        assert "Traceback" not in err

    def test_out_of_scope_refused_before_lake_io(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The lake root does not exist — a guard placed AFTER lake I/O would
        # fail differently; the refusal proves the gate ran first.
        rc = main(_sweep_argv("INDIAVIX", tmp_path))
        assert rc == EXIT_USER_ERROR
        assert _PHRASE in capsys.readouterr().err
        assert not (tmp_path / "lake").exists()

    def test_in_scope_symbol_proceeds_to_no_bars_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # NIFTY passes the gate; the empty lake then yields the EXISTING
        # honest no-bars user error (the guard changed nothing in scope).
        rc = main(_sweep_argv("NIFTY", tmp_path))
        assert rc == EXIT_USER_ERROR
        err = capsys.readouterr().err
        assert "no bars for NIFTY" in err
        assert _PHRASE not in err

    def test_lowercase_in_scope_symbol_is_normalized(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = main(_sweep_argv("nifty", tmp_path))
        assert rc == EXIT_USER_ERROR
        err = capsys.readouterr().err
        # Normalized upstream of the feed: the no-bars message names NIFTY.
        assert "no bars for NIFTY" in err
        assert _PHRASE not in err


class TestSweepAllGridGate:
    def test_mixed_symbols_refused_naming_only_offender(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = main(_sweep_all_argv("NIFTY,RELIANCE", tmp_path))
        assert rc == EXIT_USER_ERROR
        err = capsys.readouterr().err
        assert _PHRASE in err
        assert "'RELIANCE'" in err
        assert "'NIFTY'" not in err
        assert "Traceback" not in err

    def test_in_scope_symbols_proceed_past_guard(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # NIFTY,BANKNIFTY pass the gate; the empty lake then hits the EXISTING
        # "no bars for any" user error downstream of the guard.
        rc = main(_sweep_all_argv("NIFTY,BANKNIFTY", tmp_path))
        assert rc == EXIT_USER_ERROR
        err = capsys.readouterr().err
        assert "no bars for any" in err
        assert _PHRASE not in err


class TestGateSweepSymbolsHelper:
    """The coverage-measured parse+gate helper the pragma'd shims delegate to."""

    def test_admits_and_normalizes_csv(self) -> None:
        from brutex.cli import _gate_sweep_symbols

        assert _gate_sweep_symbols(" nifty , BANKNIFTY ") == ["NIFTY", "BANKNIFTY"]

    def test_refuses_naming_only_the_offender(self) -> None:
        from brutex.cli import _gate_sweep_symbols
        from brutex.engine.scope_guard import EngineScopeError

        with pytest.raises(EngineScopeError, match=_PHRASE) as excinfo:
            _gate_sweep_symbols("NIFTY,RELIANCE")
        assert "'RELIANCE'" in str(excinfo.value)
        assert "'NIFTY'" not in str(excinfo.value)

    def test_refuses_an_empty_csv(self) -> None:
        from brutex.cli import _gate_sweep_symbols
        from brutex.engine.scope_guard import EngineScopeError

        with pytest.raises(EngineScopeError, match=_PHRASE):
            _gate_sweep_symbols(" , ")


def _sweep_verify_argv(symbols: str, tmp_path: Path, *, recount: bool) -> list[str]:
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
    return argv


class TestSweepVerifyRecountGate:
    """Tier-3 --recount EXECUTES the real engine, so it is scope-gated.

    The hostile-review MAJOR this pins: before the gate,
    ``sweep-verify --symbols RELIANCE --recount --allow-context-mismatch``
    ran a REAL single-stock ``run_sweep_grid`` and persisted its stores into
    an operator-chosen ``--scratch-root`` — the exact silently-corrupt
    computation DEC-CORP-ACTION-GATE-001 exists to block.
    """

    def test_recount_out_of_scope_refused_before_any_engine_work(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = main(_sweep_verify_argv("RELIANCE", tmp_path, recount=True))
        assert rc == EXIT_USER_ERROR
        err = capsys.readouterr().err
        assert _PHRASE in err
        assert "'RELIANCE'" in err
        assert "Traceback" not in err
        # No engine work happened: no scratch stores, no lake/catalog conjured.
        assert not (tmp_path / "scratch").exists()
        assert not (tmp_path / "lake").exists()

    def test_allow_context_mismatch_is_not_a_scope_bypass(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The refusal above already ran WITH --allow-context-mismatch; this
        # pins the flag's honest contract explicitly: context override only.
        rc = main(_sweep_verify_argv("INDIAVIX", tmp_path, recount=True))
        assert rc == EXIT_USER_ERROR
        assert _PHRASE in capsys.readouterr().err

    def test_report_tiers_stay_unguarded_for_a_legacy_out_of_scope_tree(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Tier 1/2 run no engine — a legacy out-of-scope tree stays
        # report-verifiable (the documented trade-off in ADR-0016 §3).
        rc = main(_sweep_verify_argv("RELIANCE", tmp_path, recount=False))
        captured = capsys.readouterr()
        assert rc == 0
        assert _PHRASE not in captured.err
        assert _PHRASE not in captured.out

    def test_recount_in_scope_proceeds_past_the_gate(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # NIFTY passes the gate; the empty lake then hits the EXISTING honest
        # "no bars for any" user error INSIDE the recount tier.
        rc = main(_sweep_verify_argv("NIFTY", tmp_path, recount=True))
        assert rc == EXIT_USER_ERROR
        err = capsys.readouterr().err
        assert "no bars for any" in err
        assert _PHRASE not in err
