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
    DEFAULT_ONECLICK_SYMBOLS,
    DEFAULT_SEGMENT,
    DEFAULT_SL,
    DEFAULT_START,
    DEFAULT_SYMBOLS,
    DEFAULT_TOP,
    DEFAULT_TP,
    OneClickSweepPlan,
    SweepRunRequest,
    build_sweep_kwargs,
    default_results_root,
    group_symbols_by_exchange,
    oneclick_preflight_for_plan,
    resolve_oneclick_sweep_plan,
    resolve_sweep_request,
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
    assert req.sl == DEFAULT_SL
    assert req.tp == DEFAULT_TP
    assert req.exchange == DEFAULT_EXCHANGE
    assert req.segment == DEFAULT_SEGMENT
    assert req.mode == DEFAULT_MODE
    assert req.top == DEFAULT_TOP
    assert req.start == "2020-01-01"
    assert req.end == "2026-06-13"  # injected today
    assert req.acknowledge_sla_overrun is False


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
    assert req.sl == DEFAULT_SL
    assert req.tp == DEFAULT_TP
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
        "sl": DEFAULT_SL,
        "tp": DEFAULT_TP,
        "exchange": DEFAULT_EXCHANGE,
        "segment": DEFAULT_SEGMENT,
        "start": "2020-01-01",
        "end": "2026-06-13",
        "mode": DEFAULT_MODE,
        "top": DEFAULT_TOP,
        "acknowledge_sla_overrun": False,
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


def test_build_sweep_kwargs_maps_request_and_pins_institutional_defaults() -> None:
    """R5 institutional flip (gap-hunt BLOCKER #1) — regression pin.

    The one-click path must launch the INSTITUTIONAL sweep (gates ON,
    top-1000, refuse-whole), never the retired light research defaults.
    """
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
    # Pinned INSTITUTIONAL defaults (R5, gap-hunt BLOCKER #1) — never the
    # retired light research pins (overfit_gate=False/stage3_enable=False/
    # top=20/allow_missing_slices=True).
    assert kwargs["conditions"] is None
    assert kwargs["options_sim"] is False
    assert kwargs["overfit_gate"] is True
    assert kwargs["stage3_enable"] is True
    assert kwargs["allow_missing_slices"] is False
    assert kwargs["acknowledge_sla_overrun"] is False
    assert kwargs["max_tuples"] is None
    assert kwargs["max_combos"] is None
    assert kwargs["data_snapshot_hash"] is None


def test_build_sweep_kwargs_threads_acknowledge_sla_overrun() -> None:
    req = resolve_sweep_request(acknowledge_sla_overrun=True, today=_TODAY)
    kwargs = build_sweep_kwargs(req, lake_root=Path("/lake"))
    assert kwargs["acknowledge_sla_overrun"] is True


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


# ── multi-symbol / SENSEX fan-out (gap-hunt MAJOR #9, ruling 9) ──────────────


def test_group_symbols_by_exchange_splits_sensex_from_nse() -> None:
    groups = group_symbols_by_exchange(["NIFTY", "BANKNIFTY", "SENSEX"])
    assert groups == {"NSE": ("NIFTY", "BANKNIFTY"), "BSE": ("SENSEX",)}


def test_group_symbols_by_exchange_single_group() -> None:
    assert group_symbols_by_exchange(["SENSEX"]) == {"BSE": ("SENSEX",)}


def test_group_symbols_by_exchange_unknown_symbol_defaults_to_default_exchange() -> None:
    groups = group_symbols_by_exchange(["RELIANCE"])
    assert groups == {DEFAULT_EXCHANGE: ("RELIANCE",)}


def test_group_symbols_by_exchange_preserves_order_within_group() -> None:
    groups = group_symbols_by_exchange(["BANKNIFTY", "NIFTY"])
    assert groups == {"NSE": ("BANKNIFTY", "NIFTY")}


def test_resolve_oneclick_sweep_plan_default_fans_out_all_three() -> None:
    plan = resolve_oneclick_sweep_plan(today=_TODAY, run_id_factory=lambda: "fixed-id")
    assert isinstance(plan, OneClickSweepPlan)
    assert plan.sweep_run_id == "fixed-id"
    by_exchange = {req.exchange: req.symbols for req in plan.requests}
    assert by_exchange == {"NSE": "NIFTY,BANKNIFTY", "BSE": "SENSEX"}
    for req in plan.requests:
        assert req.segment == DEFAULT_SEGMENT
        assert req.top == DEFAULT_TOP
        assert req.mode == DEFAULT_MODE


def test_resolve_oneclick_sweep_plan_default_symbols_constant() -> None:
    assert DEFAULT_ONECLICK_SYMBOLS == "NIFTY,BANKNIFTY,SENSEX"


def test_resolve_oneclick_sweep_plan_single_exchange_selection() -> None:
    plan = resolve_oneclick_sweep_plan(symbols="NIFTY", today=_TODAY, run_id_factory=lambda: "x")
    assert len(plan.requests) == 1
    assert plan.requests[0].exchange == "NSE"
    assert plan.requests[0].symbols == "NIFTY"


def test_resolve_oneclick_sweep_plan_deterministic_group_order() -> None:
    plan = resolve_oneclick_sweep_plan(
        symbols="SENSEX,NIFTY", today=_TODAY, run_id_factory=lambda: "x"
    )
    # Exchange-sorted (BSE < NSE), regardless of input order.
    assert [req.exchange for req in plan.requests] == ["BSE", "NSE"]


def test_resolve_oneclick_sweep_plan_propagates_scope_refusal() -> None:
    with pytest.raises(ValueError, match="sweep REFUSED"):
        resolve_oneclick_sweep_plan(symbols="RELIANCE", today=_TODAY)


def test_resolve_oneclick_sweep_plan_default_run_id_factory_produces_a_string() -> None:
    plan = resolve_oneclick_sweep_plan(symbols="NIFTY", today=_TODAY)
    assert isinstance(plan.sweep_run_id, str)
    assert len(plan.sweep_run_id) > 0


def test_resolve_oneclick_sweep_plan_threads_acknowledge_sla_overrun() -> None:
    plan = resolve_oneclick_sweep_plan(
        symbols="NIFTY,SENSEX",
        acknowledge_sla_overrun=True,
        today=_TODAY,
        run_id_factory=lambda: "x",
    )
    assert all(req.acknowledge_sla_overrun is True for req in plan.requests)


def test_oneclick_preflight_for_plan_aggregates_across_groups(tmp_path: Path) -> None:
    plan = resolve_oneclick_sweep_plan(
        symbols="NIFTY,SENSEX", today=_TODAY, run_id_factory=lambda: "agg-id"
    )
    result = oneclick_preflight_for_plan(plan, lake_root=tmp_path, today=_TODAY, free_bytes=10**12)
    assert result["sweep_run_id"] == "agg-id"
    assert result["ok"] is False  # empty fake lake ⇒ every group has coverage gaps
    assert len(result["refusals"]) > 0
    assert isinstance(result["estimator"]["trials"], int)
    assert result["estimator"]["trials"] > 0
    assert result["estimator"]["estimate_basis"] == "provisional"


def test_oneclick_preflight_for_plan_calibrated_basis_when_calibration_file_present(
    tmp_path: Path,
) -> None:
    import json

    results_root = default_results_root(tmp_path)
    results_root.mkdir(parents=True, exist_ok=True)
    (results_root / "estimator_calibration.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "seconds_per_trial": 1e-6,
                "bytes_per_row": 100,
                "sample_trials": 1000,
                "measured_at_utc": "2026-07-08T00:00:00Z",
                "source": "smoke_run",
            }
        ),
        encoding="utf-8",
    )
    plan = resolve_oneclick_sweep_plan(
        symbols="NIFTY,SENSEX", today=_TODAY, run_id_factory=lambda: "cal-id"
    )
    result = oneclick_preflight_for_plan(plan, lake_root=tmp_path, today=_TODAY, free_bytes=10**12)
    assert result["estimator"]["estimate_basis"] == "calibrated"


def test_oneclick_preflight_for_plan_single_group() -> None:
    from tempfile import TemporaryDirectory

    plan = resolve_oneclick_sweep_plan(
        symbols="NIFTY", today=_TODAY, run_id_factory=lambda: "one-id"
    )
    with TemporaryDirectory() as tmp:
        result = oneclick_preflight_for_plan(
            plan, lake_root=Path(tmp), today=_TODAY, free_bytes=10**12
        )
    assert result["sweep_run_id"] == "one-id"
    assert set(result) == {"sweep_run_id", "ok", "refusals", "clamps", "estimator"}
