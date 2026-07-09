"""Pure-core tests for the dashboard sweep launcher (DEC-SWEEP-RUN-BUTTON-001).

100% line + branch coverage of :mod:`brutex.dashboard.sweep_runner` — every
validation arm of :func:`resolve_sweep_request` (defaults, blank-token fallbacks,
and the ValueError raises), the trivial path helpers, and the 1:1
``cmd_sweep_all`` kwargs mapping. ZERO I/O, no FastAPI, no lake — these are the
hermetic unit tests the route layer leans on (the ``# pragma: no cover``
``run_sweep_from_request`` shim is exercised by the CLI integration suite).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from brutex.dashboard.pull_controller import GROWW_HISTORY_FLOOR
from brutex.dashboard.sweep_runner import (
    DEFAULT_DIRECTIONS,
    DEFAULT_EXCHANGE,
    DEFAULT_INTERVALS,
    DEFAULT_K,
    DEFAULT_MODE,
    DEFAULT_SEGMENT,
    DEFAULT_START,
    DEFAULT_SYMBOLS,
    DEFAULT_TOP,
    SweepRunRequest,
    build_sweep_kwargs,
    default_results_root,
    resolve_sweep_request,
    sweep_finish_note,
    sweep_progress_file,
)

_TODAY = date(2026, 6, 13)


# ── happy paths: defaults + custom ────────────────────────────────────────────


def test_all_defaults_mirror_the_cli_contract() -> None:
    req = resolve_sweep_request(today=_TODAY)
    assert req.symbols == DEFAULT_SYMBOLS
    assert req.intervals == DEFAULT_INTERVALS
    assert req.directions == DEFAULT_DIRECTIONS
    assert req.k == DEFAULT_K
    assert req.sl == "none"
    assert req.tp == "none"
    assert req.exchange == DEFAULT_EXCHANGE
    assert req.segment == DEFAULT_SEGMENT
    assert req.mode == DEFAULT_MODE
    assert req.top == DEFAULT_TOP
    assert req.start == "2020-01-01"
    assert req.end == "2026-06-13"  # injected today


def test_default_start_matches_pull_floor() -> None:
    # The local literal must track GROWW_HISTORY_FLOOR (a drift-guard, §11 COMMON;
    # DEC-PULL-FLOOR-2020-001 — mirrors test_spot_filter_floor_matches_pull_floor).
    assert DEFAULT_START == GROWW_HISTORY_FLOOR.isoformat() == "2020-01-01"


def test_blank_and_whitespace_inputs_fall_back_to_defaults() -> None:
    req = resolve_sweep_request(
        symbols="  ,  ",
        intervals="",
        directions="   ",
        k=" , ",
        sl="   ",
        tp="",
        mode="   ",
        exchange="  ",
        segment="",
        start="",
        end="   ",
        today=_TODAY,
    )
    assert req.symbols == DEFAULT_SYMBOLS
    assert req.intervals == DEFAULT_INTERVALS
    assert req.directions == DEFAULT_DIRECTIONS
    assert req.k == DEFAULT_K
    assert req.sl == "none"
    assert req.tp == "none"
    assert req.mode == DEFAULT_MODE
    assert req.exchange == DEFAULT_EXCHANGE
    assert req.segment == DEFAULT_SEGMENT
    assert req.start == "2020-01-01"
    assert req.end == "2026-06-13"


def test_custom_values_are_normalised() -> None:
    req = resolve_sweep_request(
        symbols=" SENSEX , ",
        intervals="1minute, 5minute",
        directions="buy , sell",
        k="1, 3 ,5",
        sl="1500,3000",
        tp="2500",
        top=50,
        mode="B",
        exchange=" BSE ",
        segment=" INDEX ",
        start="2022-03-01",
        end="2023-09-30",
        today=_TODAY,
    )
    assert req.symbols == "SENSEX"
    assert req.intervals == "1minute,5minute"
    assert req.directions == "BUY,SELL"  # upper-cased
    assert req.k == "1,3,5"
    assert req.sl == "1500,3000"
    assert req.tp == "2500"
    assert req.top == 50
    assert req.mode == "B"
    # Non-blank passthrough is trimmed; case must be lake-exact (a lowercase
    # coordinate is now REFUSED by the DEC-ENGINE-SCOPE-GUARD-001 scope law).
    assert req.exchange == "BSE"
    assert req.segment == "INDEX"
    assert req.start == "2022-03-01"
    assert req.end == "2023-09-30"


def test_to_dict_round_trips_every_field() -> None:
    req = resolve_sweep_request(today=_TODAY)
    assert req.to_dict() == {
        "symbols": DEFAULT_SYMBOLS,
        "intervals": DEFAULT_INTERVALS,
        "directions": DEFAULT_DIRECTIONS,
        "k": DEFAULT_K,
        "sl": "none",
        "tp": "none",
        "exchange": DEFAULT_EXCHANGE,
        "segment": DEFAULT_SEGMENT,
        "start": "2020-01-01",
        "end": "2026-06-13",
        "mode": DEFAULT_MODE,
        "top": DEFAULT_TOP,
    }


# ── validation: 400-able ValueError arms ──────────────────────────────────────


def test_unknown_interval_raises() -> None:
    with pytest.raises(ValueError, match="interval must be one of"):
        resolve_sweep_request(intervals="1minute,7tick", today=_TODAY)


def test_unknown_direction_raises() -> None:
    with pytest.raises(ValueError, match="direction must be one of"):
        resolve_sweep_request(directions="BUY,HOLD", today=_TODAY)


def test_non_integer_k_raises() -> None:
    with pytest.raises(ValueError, match="k must be a whole number"):
        resolve_sweep_request(k="1,abc", today=_TODAY)


@pytest.mark.parametrize("bad", ["0", "61", "-1"])
def test_out_of_range_k_raises(bad: str) -> None:
    with pytest.raises(ValueError, match="k must be a whole number"):
        resolve_sweep_request(k=bad, today=_TODAY)


def test_k_bound_is_the_full_60_vocabulary() -> None:
    """``_MAX_K`` tracks ``TOTAL_CONDITIONS`` (60) — the pre-fix 52 rejected 53..60.

    Pinned against the engine's own default condition set so a future vocab
    growth moves the dashboard bound automatically (DEC-VOCAB-U128-CODEGEN-001;
    the drift class the 60-vocab doc sweep exists to kill).
    """
    from brutex.cli import default_condition_bits
    from brutex.dashboard.sweep_runner import _MAX_K
    from brutex.vocabulary.bits import TOTAL_CONDITIONS

    assert _MAX_K == TOTAL_CONDITIONS == len(default_condition_bits()) == 60
    # The pre-fix boundary: 53..60 must be ACCEPTED now.
    assert resolve_sweep_request(k="53,60", today=_TODAY).k == "53,60"


def test_non_integer_sl_leg_raises() -> None:
    with pytest.raises(ValueError, match="sl must be 'none' or positive paisa"):
        resolve_sweep_request(sl="1500,oops", today=_TODAY)


def test_non_positive_tp_leg_raises() -> None:
    with pytest.raises(ValueError, match="tp must be 'none' or positive paisa"):
        resolve_sweep_request(tp="0", today=_TODAY)


def test_top_below_one_raises() -> None:
    with pytest.raises(ValueError, match="top must be a whole number >= 1"):
        resolve_sweep_request(top=0, today=_TODAY)


def test_unparseable_start_date_raises() -> None:
    with pytest.raises(ValueError, match="start must be a YYYY-MM-DD date"):
        resolve_sweep_request(start="01/01/2021", today=_TODAY)


def test_unparseable_end_date_raises() -> None:
    with pytest.raises(ValueError, match="end must be a YYYY-MM-DD date"):
        resolve_sweep_request(end="not-a-date", today=_TODAY)


def test_end_before_start_raises() -> None:
    with pytest.raises(ValueError, match="must not precede start"):
        resolve_sweep_request(start="2024-01-01", end="2023-12-31", today=_TODAY)


def test_out_of_engine_scope_symbol_raises_the_scope_refusal() -> None:
    """DEC-ENGINE-SCOPE-GUARD-001 Layer 2: an out-of-scope symbol is a ValueError.

    The dashboard resolver refuses an out-of-engine-scope launch with the
    full ``ScopeReport.refusal_message()`` (symbol + law + remedy) — the
    route layer maps this ValueError to a 400 (proven separately in
    ``test_dashboard_sweep_run_route.py``).
    """
    with pytest.raises(ValueError, match="sweep REFUSED") as excinfo:
        resolve_sweep_request(symbols="RELIANCE", today=_TODAY)
    message = str(excinfo.value)
    assert "RELIANCE" in message
    assert f"{DEFAULT_EXCHANGE}/{DEFAULT_SEGMENT}" in message
    assert "NIFTY/BANKNIFTY/SENSEX" in message  # the law + remedy tail


def test_explicit_none_leg_passes_through() -> None:
    req = resolve_sweep_request(sl="NONE", tp="none", today=_TODAY)
    assert req.sl == "none"
    assert req.tp == "none"


# ── path helpers ──────────────────────────────────────────────────────────────


def test_default_results_root() -> None:
    assert default_results_root(Path("/lake")) == Path("/lake/sweeps")


def test_sweep_progress_file() -> None:
    root = Path("/lake/sweeps")
    assert sweep_progress_file(root) == Path("/lake/sweeps/progress/sweep-progress.json")


# ── kwargs mapping ────────────────────────────────────────────────────────────


def test_build_sweep_kwargs_maps_request_and_pins_safe_defaults() -> None:
    req = resolve_sweep_request(
        symbols="NIFTY", intervals="5minute", directions="BUY", k="2", today=_TODAY
    )
    kwargs = build_sweep_kwargs(req, lake_root=Path("/lake"), results_root=Path("/lake/sweeps"))
    assert kwargs["lake_root"] == Path("/lake")
    assert kwargs["results_root"] == Path("/lake/sweeps")
    assert kwargs["catalog_path"] is None
    assert kwargs["symbols"] == "NIFTY"
    assert kwargs["intervals"] == "5minute"
    assert kwargs["directions"] == "BUY"
    assert kwargs["k"] == "2"
    assert kwargs["exchange"] == DEFAULT_EXCHANGE
    assert kwargs["segment"] == DEFAULT_SEGMENT
    assert kwargs["start"] == "2020-01-01"
    assert kwargs["end"] == "2026-06-13"
    # Pinned safe dashboard defaults (signal-only, forgiving, no overfit/stage3).
    assert kwargs["conditions"] is None
    assert kwargs["options_sim"] is False
    assert kwargs["overfit_gate"] is False
    assert kwargs["stage3_enable"] is False
    assert kwargs["allow_missing_slices"] is True
    assert kwargs["max_tuples"] is None
    assert kwargs["max_combos"] is None
    assert kwargs["data_snapshot_hash"] is None


def test_build_sweep_kwargs_defaults_results_and_catalog_to_none() -> None:
    req = resolve_sweep_request(today=_TODAY)
    kwargs = build_sweep_kwargs(req, lake_root=Path("/lake"))
    assert kwargs["results_root"] is None
    assert kwargs["catalog_path"] is None


def test_build_sweep_kwargs_threads_explicit_catalog() -> None:
    req = resolve_sweep_request(today=_TODAY)
    kwargs = build_sweep_kwargs(req, lake_root=Path("/lake"), catalog_path=Path("/cat.duckdb"))
    assert kwargs["catalog_path"] == Path("/cat.duckdb")


def test_request_is_frozen() -> None:
    req = SweepRunRequest(
        symbols="NIFTY",
        intervals="5minute",
        directions="BUY",
        k="2",
        sl="none",
        tp="none",
        exchange="NSE",
        segment="INDEX",
        start="2021-01-01",
        end="2026-06-13",
        mode="A",
        top=20,
    )
    with pytest.raises((AttributeError, TypeError)):
        req.symbols = "BANKNIFTY"  # type: ignore[misc]


# ── corporate-action extension of the scope gate (DEC-CORP-ACTION-GATE-001) ──


def test_out_of_scope_refusal_carries_the_corp_action_rationale() -> None:
    """The eager dashboard refusal (DEC-ENGINE-SCOPE-GUARD-001, proven above)
    now carries the corporate-action rationale + both DEC ids + ADR-0016
    (DEC-CORP-ACTION-GATE-001 — the extension), naming only the offender."""
    with pytest.raises(ValueError, match="corporate-action adjustment layer missing") as excinfo:
        resolve_sweep_request(symbols="NIFTY,RELIANCE", today=_TODAY)
    message = str(excinfo.value)
    assert "DEC-CORP-ACTION-GATE-001" in message
    assert "DEC-ENGINE-SCOPE-GUARD-001" in message
    assert "ADR-0016" in message
    assert "- RELIANCE:" in message
    assert "- NIFTY:" not in message


def test_whitespace_padded_in_scope_symbols_are_trimmed_and_admitted() -> None:
    # Trim-only normalisation (case must be lake-exact — a lowercase symbol
    # is refused with a did-you-mean hint, pinned below).
    req = resolve_sweep_request(symbols=" NIFTY , BANKNIFTY ", today=_TODAY)
    assert req.symbols == "NIFTY,BANKNIFTY"


def test_lowercase_in_scope_symbol_is_refused_with_a_did_you_mean_hint() -> None:
    # Lake partition keys are case-exact (DEC-ENGINE-SCOPE-GUARD-001): the
    # dashboard never silently upper-cases — the refusal names the fix.
    with pytest.raises(ValueError, match="did you mean") as excinfo:
        resolve_sweep_request(symbols="nifty", today=_TODAY)
    assert "'NIFTY'" in str(excinfo.value)


# ── sweep_finish_note (G5b, DEC-SWEEP-ACCEPTANCE-LIFECYCLE-001) ────────────────


class TestSweepFinishNote:
    """The dashboard job's honest completion headline — every branch pinned."""

    def test_summary_unavailable_exit_zero_is_done(self) -> None:
        # A clean sweep with no captured summary keeps the back-compat "done".
        assert (
            sweep_finish_note(exit_code=0, tuples_failed=None, tuples_quarantined_prior=None)
            == "done"
        )

    def test_summary_unavailable_nonzero_exit_names_it(self) -> None:
        assert (
            sweep_finish_note(exit_code=2, tuples_failed=None, tuples_quarantined_prior=None)
            == "sweep did not run (exit 2)"
        )

    def test_clean_summary_zero_zero_is_sweep_complete(self) -> None:
        assert (
            sweep_finish_note(exit_code=0, tuples_failed=0, tuples_quarantined_prior=0)
            == "sweep complete"
        )

    def test_clean_summary_zero_failed_none_quarantined(self) -> None:
        # The quarantined-prior None branch (a summary predating the field).
        assert (
            sweep_finish_note(exit_code=0, tuples_failed=0, tuples_quarantined_prior=None)
            == "sweep complete"
        )

    def test_failed_tuples_named(self) -> None:
        note = sweep_finish_note(exit_code=1, tuples_failed=3, tuples_quarantined_prior=0)
        assert note == (
            "sweep complete — 3 tuple(s) FAILED and were skipped "
            "(will redo next run; see sweep audit)"
        )

    def test_quarantined_prior_named(self) -> None:
        note = sweep_finish_note(exit_code=0, tuples_failed=0, tuples_quarantined_prior=2)
        assert note == "sweep complete — 2 tuple(s) quarantined from a prior run"

    def test_failed_and_quarantined_both_named(self) -> None:
        note = sweep_finish_note(exit_code=1, tuples_failed=3, tuples_quarantined_prior=2)
        assert note == (
            "sweep complete — 3 tuple(s) FAILED and were skipped "
            "(will redo next run; see sweep audit); "
            "2 tuple(s) quarantined from a prior run"
        )
