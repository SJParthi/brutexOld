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
        k="1, 3 ,2",
        sl="150,100",
        tp="200",
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
    assert req.k == "1,3,2"
    assert req.sl == "150,100"
    assert req.tp == "200"
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
        # R54 disclosure (operator ruling 2026-07-08): the derived effective
        # Stage-3 posture rides the request echo → the /sweep/run payload.
        "stage3_enable": True,
    }


def test_to_dict_discloses_the_stage3_downgrade_for_none_legs_r54() -> None:
    """R54 review round 2, finding 5: the Stage-3 downgrade must be visible on
    the operator-facing ``POST /api/v1/sweep/run`` response payload (which is
    ``to_dict() + {started}``) — never a server-log-only fact."""
    req = resolve_sweep_request(sl="none", today=_TODAY)
    assert req.to_dict()["stage3_enable"] is False
    mixed = resolve_sweep_request(sl="none,5,100,200", tp="100,200,300", today=_TODAY)
    assert mixed.to_dict()["stage3_enable"] is False


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

    Round-12 update: ``k=53..60`` are now legitimately refused by the
    SEPARATE ``max_combos`` guard (a round-12 adversarial-refuter fix) —
    ``C(60,53)`` alone enumerates ~386M masks, far past the engine's
    100,000-mask ceiling. The original assertion here proved the RANGE
    check no longer artificially caps at the stale 52 — that is now proven
    by asserting the refusal comes from the max_combos guard, never the
    OLD "k must be a whole number 1..52" range message.
    """
    from brutex.cli import default_condition_bits
    from brutex.dashboard.sweep_runner import _MAX_K
    from brutex.vocabulary.bits import TOTAL_CONDITIONS

    assert _MAX_K == TOTAL_CONDITIONS == len(default_condition_bits()) == 60
    with pytest.raises(ValueError, match="max_combos"):
        resolve_sweep_request(k="53,60", today=_TODAY)


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
    # The launch pair is operator-typed → repr-quoted (round-8 finding 2).
    assert f"{DEFAULT_EXCHANGE!r}/{DEFAULT_SEGMENT!r}" in message
    assert "NIFTY/BANKNIFTY/SENSEX" in message  # the law + remedy tail


def test_explicit_none_sl_leg_is_runnable_r54() -> None:
    """OPERATOR RULING 2026-07-08 (R54): both SL/TP arms AND no-SL/TP arms
    must be coverable — an explicit ``sl="none"`` leg is first-class
    RUNNABLE (warn-not-refuse), never the round-11 hard refusal.
    ``build_sweep_kwargs`` downgrades ``stage3_enable`` to ``False`` for the
    launch (CMA-ES cannot seed its x0 from a ``none`` leg, ADR-0007
    condition 3; with Stage-3 off the engine accepts ``None`` legs, so the
    launch stays provably runnable — R16 preserved)."""
    req = resolve_sweep_request(sl="none", today=_TODAY)
    assert req.sl == "none"
    assert req.tp == DEFAULT_TP
    kwargs = build_sweep_kwargs(req, lake_root=Path("/lake"))
    assert kwargs["sl"] == "none"
    assert kwargs["stage3_enable"] is False


def test_explicit_none_tp_leg_is_runnable_r54() -> None:
    """R54 (operator ruling 2026-07-08), the TP twin — case-insensitive
    ``tp="NONE"`` normalises + runs; Stage-3 downgraded OFF."""
    req = resolve_sweep_request(tp="NONE", today=_TODAY)
    assert req.tp == "none"
    kwargs = build_sweep_kwargs(req, lake_root=Path("/lake"))
    assert kwargs["tp"] == "none"
    assert kwargs["stage3_enable"] is False


def test_both_none_legs_runnable_with_one_warn_at_build_r54() -> None:
    """R54: the double-``none`` launch (the CLI's own ``sweep-all`` default
    shape, and the launch_button LOCKED_SL/LOCKED_TP shape) resolves
    runnable with ZERO warnings at request resolution and EXACTLY ONE
    ``sweep_runner.none_leg_stage3_downgrade`` WARN — naming both legs —
    when the launch kwargs are BUILT (R54 review round 2, finding 4: the
    WARN must fire at launch time, so a request that resolves but is never
    launched — e.g. the read-only preflight — logs no false "downgraded
    OFF for this launch" claim)."""
    from structlog.testing import capture_logs

    with capture_logs() as resolve_logs:
        req = resolve_sweep_request(sl="NONE", tp="none", today=_TODAY)
    assert req.sl == "none"
    assert req.tp == "none"
    assert [e for e in resolve_logs if e["event"] == "sweep_runner.none_leg_stage3_downgrade"] == []
    with capture_logs() as build_logs:
        kwargs = build_sweep_kwargs(req, lake_root=Path("/lake"))
    warns = [e for e in build_logs if e["event"] == "sweep_runner.none_leg_stage3_downgrade"]
    assert len(warns) == 1
    assert warns[0]["log_level"] == "warning"
    assert warns[0]["legs"] == ["sl", "tp"]
    assert kwargs["stage3_enable"] is False


def test_refused_request_emits_no_downgrade_warn_r54() -> None:
    """R54 review round 2, finding 4: a ``none``-leg request that is
    subsequently REFUSED (here: a bad interval) must log NO "downgraded OFF
    for this launch" WARN — nothing launches, nothing was downgraded."""
    from structlog.testing import capture_logs

    with capture_logs() as logs, pytest.raises(ValueError, match="interval must be one of"):
        resolve_sweep_request(sl="none", intervals="7tick", today=_TODAY)
    assert [e for e in logs if e["event"] == "sweep_runner.none_leg_stage3_downgrade"] == []


def test_gridded_legs_emit_no_downgrade_warn_and_keep_stage3_r54() -> None:
    """R54's complement: fully-gridded legs stay on the R5 institutional
    ``stage3_enable=True`` posture with ZERO downgrade warnings anywhere
    (resolution AND kwargs build)."""
    from structlog.testing import capture_logs

    with capture_logs() as logs:
        req = resolve_sweep_request(sl="50", tp="100", today=_TODAY)
        kwargs = build_sweep_kwargs(req, lake_root=Path("/lake"))
    assert [e for e in logs if e["event"] == "sweep_runner.none_leg_stage3_downgrade"] == []
    assert kwargs["stage3_enable"] is True
    assert req.stage3_enable is True


def test_mixed_csv_none_leg_is_runnable_end_to_end_r54() -> None:
    """R54 review round 2, finding 1 (BLOCKER): the operator's REAL mixed
    grid shape — a ``none`` VALUE among gridded values (``sl="none,5,100,200"``,
    PR #1297's default grid string, and the only single-sweep shape covering
    BOTH SL/TP arms AND no-SL/TP arms in one run) — must resolve runnable
    end-to-end: through ``build_sweep_kwargs`` into a CONSTRUCTIBLE
    ``SweepGridConfig`` whose none cell is enumerated, never dropped."""
    from brutex.cli import _parse_paisa_list  # pyright: ignore[reportPrivateUsage]
    from brutex.engine.sweep_orchestrator import SweepGridConfig, Timeframe, count_sweep_tuples
    from brutex.strategies.types import Side

    req = resolve_sweep_request(sl="none,5,100,200", tp="100,200,300", today=_TODAY)
    assert req.sl == "none,5,100,200"
    assert req.tp == "100,200,300"
    kwargs = build_sweep_kwargs(req, lake_root=Path("/lake"))
    assert kwargs["sl"] == "none,5,100,200"
    assert kwargs["stage3_enable"] is False
    # The engine grammar (`cmd_sweep_all` → `_parse_paisa_list`) enumerates
    # the none cell as a REAL grid cell.
    assert _parse_paisa_list(kwargs["sl"]) == [None, 5, 100, 200]
    grid = SweepGridConfig(
        instruments=["NIFTY"],
        timeframes=[Timeframe("5minute", 5)],
        directions=[Side.BUY],
        k_values=[1],
        condition_bits=[0, 1, 2],
        stop_losses=_parse_paisa_list(kwargs["sl"]),
        take_profits=_parse_paisa_list(kwargs["tp"]),
        proximities=[None],
        mode=kwargs["mode"],
        data_snapshot_hash="snap",
        indicator_version="test-iv",
        code_commit_sha="test-sha",
        stage3_enable=kwargs["stage3_enable"],
    )
    # 1 instrument × 1 tf × 1 direction × 1 k × 4 sl (incl. None) × 3 tp.
    assert count_sweep_tuples(grid) == 12
    assert None in grid.stop_losses


def test_mixed_csv_none_tokens_normalise_case_and_whitespace_r54() -> None:
    """The mixed grammar mirrors the CLI ``_parse_paisa_list`` token rules:
    ``none`` is case-insensitive and whitespace-tolerant inside the CSV."""
    req = resolve_sweep_request(sl=" NONE , 50 ", tp="100, none", today=_TODAY)
    assert req.sl == "none,50"
    assert req.tp == "100,none"
    assert req.stage3_enable is False


def test_none_leg_with_offgrid_other_leg_is_runnable_r54() -> None:
    """R54 review round 2, finding 2 (MAJOR): ``sl="none"`` downgrades the
    launch's effective Stage-3 posture OFF — and with Stage-3 off the engine
    accepts ANY positive paisa value, so an off-Mode-A-grid ``tp=1500`` on
    the same launch is provably engine-runnable and must NOT be refused on
    a Stage-3 justification the launch itself has disabled (the pre-fix
    behavior WARNed "downgraded OFF for this launch" then raised an
    off-grid refusal citing the disabled Stage-3 default — self-
    contradictory)."""
    from brutex.cli import _parse_paisa_list  # pyright: ignore[reportPrivateUsage]
    from brutex.engine.sweep_orchestrator import SweepGridConfig, Timeframe, count_sweep_tuples
    from brutex.strategies.types import Side

    req = resolve_sweep_request(sl="none", tp="1500", today=_TODAY)
    kwargs = build_sweep_kwargs(req, lake_root=Path("/lake"))
    assert kwargs["stage3_enable"] is False
    grid = SweepGridConfig(
        instruments=["NIFTY"],
        timeframes=[Timeframe("5minute", 5)],
        directions=[Side.BUY],
        k_values=[1],
        condition_bits=[0, 1, 2],
        stop_losses=_parse_paisa_list(kwargs["sl"]),
        take_profits=_parse_paisa_list(kwargs["tp"]),
        proximities=[None],
        mode=kwargs["mode"],
        data_snapshot_hash="snap",
        indicator_version="test-iv",
        code_commit_sha="test-sha",
        stage3_enable=kwargs["stage3_enable"],
    )
    assert count_sweep_tuples(grid) == 1


def test_leg_values_mirrors_the_cli_parse_paisa_list_grammar() -> None:
    """R54 review round 2, finding 3: ``leg_values`` is the ONE parsed-leg
    source of truth — pinned token-for-token against the CLI's
    ``_parse_paisa_list`` (the grammar ``cmd_sweep_all`` hands the engine)
    so the two can never drift."""
    from brutex.cli import _parse_paisa_list  # pyright: ignore[reportPrivateUsage]
    from brutex.dashboard.sweep_runner import leg_has_none, leg_values

    for leg in ("none", "none,5,100,200", "50,100,150", "NONE, 50", "", "none,none", " 5 "):
        assert list(leg_values(leg)) == _parse_paisa_list(leg)
        assert leg_has_none(leg) == (None in _parse_paisa_list(leg))


@pytest.mark.parametrize(
    ("sl", "tp", "expected_stage3"),
    [
        (None, None, True),  # institutional defaults, fully gridded
        ("50,100,150", "100,200,300", True),
        ("none", "100,200,300", False),
        ("50,100,150", "none", False),
        ("none", "none", False),
        ("none,5,100,200", "100,200,300", False),  # PR #1297's default shape
        ("50", "none,100", False),
        ("none,50", "none,100", False),
    ],
)
def test_stage3_enable_agrees_with_the_parsed_legs_for_every_accepted_shape(
    sl: str | None, tp: str | None, expected_stage3: bool
) -> None:
    """R54 review round 2, finding 3: ``build_sweep_kwargs``' ``stage3_enable``
    must agree with the PARSED legs (oracle: the CLI ``_parse_paisa_list``)
    for every accepted leg string — a whole-string comparison reads a mixed
    ``"none,50"`` leg as gridded, re-instating the round-11 deep engine
    crash the moment the grammar widened."""
    from brutex.cli import _parse_paisa_list  # pyright: ignore[reportPrivateUsage]

    req = resolve_sweep_request(sl=sl, tp=tp, today=_TODAY)
    kwargs = build_sweep_kwargs(req, lake_root=Path("/lake"))
    parsed_has_none = (None in _parse_paisa_list(req.sl)) or (None in _parse_paisa_list(req.tp))
    assert kwargs["stage3_enable"] == (not parsed_has_none) == expected_stage3
    assert req.stage3_enable is kwargs["stage3_enable"]


def test_oneclick_none_leg_plan_resolves_quietly_and_warns_once_per_group_kwargs_r54() -> None:
    """R54 review round 2, findings 4+7: resolving a one-click plan (which the
    read-only preflight GET does too) emits ZERO downgrade WARNs even when
    every group carries a ``none`` leg; the WARNs fire only when each
    group's engine-run kwargs are BUILT at launch — exactly one per
    launched exchange-group engine run (2 here: NSE + BSE)."""
    from structlog.testing import capture_logs

    with capture_logs() as resolve_logs:
        plan = resolve_oneclick_sweep_plan(sl="none", today=_TODAY, run_id_factory=lambda: "x")
    assert [e for e in resolve_logs if e["event"] == "sweep_runner.none_leg_stage3_downgrade"] == []
    assert len(plan.requests) == 2  # NSE (NIFTY,BANKNIFTY) + BSE (SENSEX)
    with capture_logs() as build_logs:
        for request in plan.requests:
            kwargs = build_sweep_kwargs(request, lake_root=Path("/lake"))
            assert kwargs["stage3_enable"] is False
    warns = [e for e in build_logs if e["event"] == "sweep_runner.none_leg_stage3_downgrade"]
    assert len(warns) == len(plan.requests) == 2
    assert {w["exchange"] for w in warns} == {"NSE", "BSE"}


def test_off_grid_sl_now_refused() -> None:
    """Same round-11 finding, the other failure shape: a syntactically-valid
    but off-Mode-A-grid paisa value (not a multiple of the 5-paisa SL tick)
    used to pass preflight and only fail deep inside engine construction."""
    with pytest.raises(ValueError, match="off the locked Mode-A grid"):
        resolve_sweep_request(sl="51", today=_TODAY)


def test_off_grid_tp_now_refused() -> None:
    with pytest.raises(ValueError, match="off the locked Mode-A grid"):
        resolve_sweep_request(tp="1500", today=_TODAY)


def test_k_over_max_combos_now_refused() -> None:
    """Round-12 adversarial-refuter finding: the SAME "preflight says ok,
    engine construction says no" failure mode round 11 fixed for SL/TP also
    applies to ``k`` — ``build_sweep_kwargs`` always uses the full 60-bit
    vocabulary (``conditions=None``) and the engine's default
    ``max_combos=100_000``, so ``k=4`` alone (``C(60,4)=487,635`` masks)
    used to preflight `ok: true` on a single-tuple launch (too small for
    the SLA guard to ever catch) and then crash deterministically inside
    ``SweepGridConfig`` construction. Must now refuse loudly at
    request-resolution time."""
    with pytest.raises(ValueError, match="max_combos"):
        resolve_sweep_request(k="4", sl="50", tp="100", today=_TODAY)


def test_k_within_max_combos_still_accepted() -> None:
    """The max_combos guard must not reject legitimate small-k requests."""
    req = resolve_sweep_request(k="1,2,3", sl="50", tp="100", today=_TODAY)
    assert req.k == "1,2,3"


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
    # repr-quoted (round-4 finding 3)
    assert "- 'RELIANCE':" in message
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


# ────── DEC-SWEEP-APRIORI-DEFAULT-001 (MIGRATION.md #1752) ──────
# The dashboard's extinction default: DEFAULT_K flips to the ONE sanctioned
# second copy of the CLI token (drift-pinned below), _normalise_k accepts the
# token / refuses a token+number mix, and run_sweep_from_request — the ONE
# in-process embedder of cmd_sweep_all — snapshot-restores BRUTEX_SWEEP_APRIORI
# (the dashboard process OUTLIVES the job, unlike a CLI run).


def test_default_k_is_the_extinction_token() -> None:
    assert DEFAULT_K == "extinction"


def test_default_k_drift_pin_against_the_cli_token_and_parser_default() -> None:
    """The CLI↔dashboard token DRIFT PIN: the dashboard's sanctioned second
    copy must equal the CLI constant AND the shared parser's ``--k`` default
    (introspected — the value that actually reaches every grid surface)."""
    from brutex.cli import EXTINCTION_K_TOKEN, build_parser

    assert DEFAULT_K == EXTINCTION_K_TOKEN
    args = build_parser().parse_args(["sweep-all", "--start", "2024-01-01", "--end", "2024-01-02"])
    assert args.k == DEFAULT_K


def test_extinction_token_passes_through_normalisation() -> None:
    req = resolve_sweep_request(k="extinction", today=_TODAY)
    assert req.k == "extinction"


def test_extinction_token_is_case_insensitive() -> None:
    req = resolve_sweep_request(k="  EXTINCTION  ", today=_TODAY)
    assert req.k == "extinction"


def test_blank_k_falls_to_the_extinction_default() -> None:
    req = resolve_sweep_request(k="   ", today=_TODAY)
    assert req.k == "extinction"


def test_numeric_csv_k_still_validates_as_before() -> None:
    # The legacy byte-identical arm — the classic fixed-K sweep.
    req = resolve_sweep_request(k="2, 3", today=_TODAY)
    assert req.k == "2,3"


def test_token_mixed_with_numbers_is_a_400() -> None:
    with pytest.raises(ValueError, match="either 'extinction'"):
        resolve_sweep_request(k="extinction,2", today=_TODAY)


def test_number_mixed_with_token_is_a_400_regardless_of_order() -> None:
    with pytest.raises(ValueError, match="either 'extinction'"):
        resolve_sweep_request(k="2,extinction", today=_TODAY)


class TestRunSweepFromRequestEnvHygiene:
    """The snapshot-restore law (ruling 5): the flag never leaks past the job."""

    @staticmethod
    def _request() -> SweepRunRequest:
        return resolve_sweep_request(today=_TODAY)

    def test_env_set_by_the_sweep_is_restored_to_unset(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import os

        from brutex import cli
        from brutex.dashboard.sweep_runner import run_sweep_from_request
        from brutex.engine.sweep_apriori import SWEEP_APRIORI_ENV

        monkeypatch.delenv(SWEEP_APRIORI_ENV, raising=False)

        def _fake_sweep(**_kwargs: object) -> int:
            os.environ[SWEEP_APRIORI_ENV] = "1"  # cmd_sweep_all's setdefault
            return 0

        monkeypatch.setattr(cli, "cmd_sweep_all", _fake_sweep)
        rc = run_sweep_from_request(
            self._request(), lake_root=tmp_path, results_root=tmp_path / "results"
        )
        assert rc == 0
        assert SWEEP_APRIORI_ENV not in os.environ  # restored — no cross-job leak

    def test_env_restored_even_when_the_sweep_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import os

        from brutex import cli
        from brutex.dashboard.sweep_runner import run_sweep_from_request
        from brutex.engine.sweep_apriori import SWEEP_APRIORI_ENV

        monkeypatch.delenv(SWEEP_APRIORI_ENV, raising=False)

        def _boom(**_kwargs: object) -> int:
            os.environ[SWEEP_APRIORI_ENV] = "1"
            raise RuntimeError("sweep blew up")

        monkeypatch.setattr(cli, "cmd_sweep_all", _boom)
        with pytest.raises(RuntimeError, match="sweep blew up"):
            run_sweep_from_request(
                self._request(), lake_root=tmp_path, results_root=tmp_path / "results"
            )
        assert SWEEP_APRIORI_ENV not in os.environ

    def test_operator_preset_value_is_restored_verbatim(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import os

        from brutex import cli
        from brutex.dashboard.sweep_runner import run_sweep_from_request
        from brutex.engine.sweep_apriori import SWEEP_APRIORI_ENV

        monkeypatch.setenv(SWEEP_APRIORI_ENV, "0")  # the operator's kill switch

        def _fake_sweep(**_kwargs: object) -> int:
            os.environ[SWEEP_APRIORI_ENV] = "1"  # a hypothetical overwrite
            return 0

        monkeypatch.setattr(cli, "cmd_sweep_all", _fake_sweep)
        run_sweep_from_request(
            self._request(), lake_root=tmp_path, results_root=tmp_path / "results"
        )
        assert os.environ[SWEEP_APRIORI_ENV] == "0"  # pop-or-restore, verbatim


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


def test_axes_counts_dedupes_repeated_csv_tokens() -> None:
    """Hostile-review fix: a repeated token must not inflate the estimate.

    The real engine dedupes every :class:`~brutex.engine.sweep_orchestrator.SweepGridConfig`
    axis at construction — the estimator must count the SAME de-duplicated
    size, or the R20 "true" trial/tuple count silently diverges from what
    actually runs.
    """
    from brutex.dashboard.sweep_runner import _axes_counts

    req = resolve_sweep_request(
        symbols="NIFTY,NIFTY", k="2,2,3", sl="50,50", tp="100", today=_TODAY
    )
    k_values, other_axes_product, tuple_count = _axes_counts(req)
    assert k_values == [2, 3]  # deduped, order-preserving
    # symbols(1 unique) * intervals(3) * directions(2) * sl(1 unique) * tp(1)
    assert other_axes_product == 1 * 3 * 2 * 1 * 1
    assert tuple_count == other_axes_product * len(k_values)


def test_oneclick_preflight_for_plan_aggregates_across_groups(tmp_path: Path) -> None:
    plan = resolve_oneclick_sweep_plan(
        symbols="NIFTY,SENSEX", today=_TODAY, run_id_factory=lambda: "agg-id"
    )
    result = oneclick_preflight_for_plan(plan, lake_root=tmp_path, today=_TODAY, free_bytes=10**12)
    assert result["sweep_run_id"] == "agg-id"
    assert result["ok"] is False  # empty fake lake ⇒ every group has coverage gaps
    assert len(result["refusals"]) > 0
    # Exact per-group trial counts, summed — not just "some positive number" —
    # so a regression that doubles/halves the aggregate cannot slip through
    # (round-3 adversarial-refuter finding: the prior assertion only checked
    # `> 0`).
    from brutex.dashboard.sweep_runner import _axes_counts
    from brutex.engine.sweep_estimator import count_true_trials

    expected_trials = 0
    for request in plan.requests:
        k_values, other_axes_product, _ = _axes_counts(request)
        expected_trials += count_true_trials(k_values, other_axes_product=other_axes_product)
    assert result["estimator"]["trials"] == expected_trials
    assert result["estimator"]["estimate_basis"] == "provisional"


def test_oneclick_preflight_for_plan_effective_condition_count_is_min_across_groups(
    tmp_path: Path,
) -> None:
    """Round-3 adversarial finding: every real fan-out shares one ``segment``
    across groups, so the ``min()`` aggregation in ``oneclick_preflight_for_plan``
    (vs. e.g. ``max()``) was correct but unreached/unproven by any test. Build
    a plan with two groups on DIFFERING segments directly (bypassing
    ``resolve_oneclick_sweep_plan``, which only accepts one ``segment`` for
    the whole plan) to pin that the aggregate is the worst-case (smallest)
    effective condition count, not the largest."""
    index_request = SweepRunRequest(
        symbols="NIFTY",
        intervals=DEFAULT_INTERVALS,
        directions=DEFAULT_DIRECTIONS,
        k="1",
        sl="50",
        tp="100",
        exchange="NSE",
        segment="INDEX",
        start=_TODAY.isoformat(),
        end=_TODAY.isoformat(),
        mode=DEFAULT_MODE,
        top=10,
    )
    fno_request = SweepRunRequest(
        symbols="SENSEX",
        intervals=DEFAULT_INTERVALS,
        directions=DEFAULT_DIRECTIONS,
        k="1",
        sl="50",
        tp="100",
        exchange="BSE",
        segment="FNO",
        start=_TODAY.isoformat(),
        end=_TODAY.isoformat(),
        mode=DEFAULT_MODE,
        top=10,
    )
    plan = OneClickSweepPlan(sweep_run_id="mixed-segment-id", requests=(index_request, fno_request))
    result = oneclick_preflight_for_plan(plan, lake_root=tmp_path, today=_TODAY, free_bytes=10**12)
    nominal = result["estimator"]["nominal_condition_count"]
    # INDEX's VWAP-dead-bits annotation (2 fewer) is strictly smaller than
    # FNO's full nominal count — the aggregate MUST report the smaller value.
    assert result["estimator"]["effective_condition_count"] == nominal - 2
    assert result["estimator"]["effective_condition_count"] < nominal


def _sla_boundary_group(*, exchange: str, symbol: str) -> SweepRunRequest:
    """A single-group request whose OWN tuple_count (6000) is comfortably
    under the SLA envelope's 11520-tuple ceiling (345600s / 30s-per-tuple) —
    but where TWO such groups combined (12000) blow well past it."""
    sl = ",".join(str(50 + i) for i in range(60))
    tp = ",".join(str(100 + i) for i in range(100))
    return SweepRunRequest(
        symbols=symbol,
        intervals="1minute",
        directions="BUY",
        k="1",
        sl=sl,
        tp=tp,
        exchange=exchange,
        segment="FNO",
        start=_TODAY.isoformat(),
        end=_TODAY.isoformat(),
        mode=DEFAULT_MODE,
        top=10,
    )


def test_oneclick_preflight_for_plan_refuses_on_combined_sla_overrun(tmp_path: Path) -> None:
    """Adversarial-refuter finding: each exchange group's OWN per-group SLA
    check can independently land right at ALLOWED while the true combined
    wall-clock (every group runs SEQUENTIALLY in one background job) blows
    past the CLAUDE.md §8 envelope — a silent overrun with no refusal
    anywhere. A multi-group plan must re-check the SUMMED tuple count."""
    plan = OneClickSweepPlan(
        sweep_run_id="sla-combined-id",
        requests=(
            _sla_boundary_group(exchange="NSE", symbol="NIFTY"),
            _sla_boundary_group(exchange="BSE", symbol="SENSEX"),
        ),
    )
    result = oneclick_preflight_for_plan(plan, lake_root=tmp_path, today=_TODAY, free_bytes=10**12)
    assert result["ok"] is False
    assert any("projected wall-clock" in r["reason"] for r in result["refusals"])


def test_oneclick_preflight_for_plan_single_group_sla_overrun_is_not_double_refused(
    tmp_path: Path,
) -> None:
    """Round-3 mutation-testing finding: the combined-SLA re-check guards on
    ``len(plan.requests) > 1`` specifically so a SINGLE group that already
    breaches the envelope on its own (already refused once by the per-group
    check inside the loop) does not ALSO get the summed re-check's refusal
    appended — a mutation to ``>= 1`` slipped through every prior test
    because they only asserted membership (``any(...)``), never an exact
    count. This single-group plan's own tuple_count (12000) already exceeds
    the 11520-tuple envelope alone."""
    sl = ",".join(str(50 + i) for i in range(120))
    tp = ",".join(str(100 + i) for i in range(100))
    single_group = SweepRunRequest(
        symbols="NIFTY",
        intervals="1minute",
        directions="BUY",
        k="1",
        sl=sl,
        tp=tp,
        exchange="NSE",
        segment="FNO",
        start=_TODAY.isoformat(),
        end=_TODAY.isoformat(),
        mode=DEFAULT_MODE,
        top=10,
    )
    plan = OneClickSweepPlan(sweep_run_id="single-overrun-id", requests=(single_group,))
    result = oneclick_preflight_for_plan(plan, lake_root=tmp_path, today=_TODAY, free_bytes=10**12)
    sla_refusals = [r for r in result["refusals"] if "projected wall-clock" in r["reason"]]
    assert len(sla_refusals) == 1


def test_oneclick_preflight_for_plan_acknowledged_combined_sla_overrun_proceeds(
    tmp_path: Path,
) -> None:
    """The same combined-overrun scenario, but with the operator's
    acknowledge flag set — a Clamp note, never a refusal (ruling #2)."""
    from dataclasses import replace

    groups = (
        _sla_boundary_group(exchange="NSE", symbol="NIFTY"),
        _sla_boundary_group(exchange="BSE", symbol="SENSEX"),
    )
    acknowledged_groups = tuple(replace(g, acknowledge_sla_overrun=True) for g in groups)
    plan = OneClickSweepPlan(sweep_run_id="sla-ack-id", requests=acknowledged_groups)
    result = oneclick_preflight_for_plan(plan, lake_root=tmp_path, today=_TODAY, free_bytes=10**12)
    assert not any("projected wall-clock" in r["reason"] for r in result["refusals"])
    assert any(c["field"] == "sla_overrun" for c in result["clamps"])


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


def test_oneclick_preflight_reports_a_stage3_downgrade_clamp_per_none_group_r54(
    tmp_path: Path,
) -> None:
    """R54 review round 2, finding 5: the one-click preflight verdict is THE
    operator proceed-dialog's data source — a ``none``-leg group's Stage-3
    downgrade must ride its purpose-built requested→applied ``clamps``
    channel (one exchange-named row per downgraded group), never a
    server-log-only WARN. The preflight resolution itself stays silent
    (finding 7's read-only "NEVER launches" contract)."""
    from structlog.testing import capture_logs

    with capture_logs() as logs:
        plan = resolve_oneclick_sweep_plan(
            symbols="NIFTY,SENSEX", tp="none", today=_TODAY, run_id_factory=lambda: "clamp-id"
        )
        result = oneclick_preflight_for_plan(
            plan, lake_root=tmp_path, today=_TODAY, free_bytes=10**12
        )
    assert [e for e in logs if e["event"] == "sweep_runner.none_leg_stage3_downgrade"] == []
    stage3_clamps = [c for c in result["clamps"] if c["field"] == "stage3_enable"]
    assert len(stage3_clamps) == 2  # one per exchange group (BSE + NSE)
    for clamp in stage3_clamps:
        assert clamp["requested"] == "true"
        assert clamp["applied"] == "false"
        assert "R54" in clamp["reason"]
        assert "tp" in clamp["reason"]
    assert {c["reason"].split(":", 1)[0] for c in stage3_clamps} == {"BSE", "NSE"}


def test_oneclick_preflight_mixed_leg_group_carries_the_stage3_clamp_r54(tmp_path: Path) -> None:
    """The mixed-CSV shape (finding 1) rides the same clamp disclosure: a
    ``sl="none,50"`` plan's groups each carry the ``stage3_enable`` row —
    the gridded cells' lost CMA-ES refinement is stated in the reason."""
    plan = resolve_oneclick_sweep_plan(
        symbols="NIFTY", sl="none,50", today=_TODAY, run_id_factory=lambda: "mixed-clamp-id"
    )
    result = oneclick_preflight_for_plan(plan, lake_root=tmp_path, today=_TODAY, free_bytes=10**12)
    stage3_clamps = [c for c in result["clamps"] if c["field"] == "stage3_enable"]
    assert len(stage3_clamps) == 1
    assert stage3_clamps[0]["reason"].startswith("NSE: ")
    assert "sl" in stage3_clamps[0]["reason"]
    assert "gridded SL/TP cells" in stage3_clamps[0]["reason"]


def test_oneclick_preflight_gridded_plan_carries_no_stage3_clamp_r54(tmp_path: Path) -> None:
    """Complement pin: a fully-gridded plan (the institutional default) emits
    NO ``stage3_enable`` clamp — the disclosure fires only on a real
    downgrade."""
    plan = resolve_oneclick_sweep_plan(
        symbols="NIFTY,SENSEX", today=_TODAY, run_id_factory=lambda: "grid-id"
    )
    result = oneclick_preflight_for_plan(plan, lake_root=tmp_path, today=_TODAY, free_bytes=10**12)
    assert [c for c in result["clamps"] if c["field"] == "stage3_enable"] == []


# ---------------------------------------------------------------------------
# The extinction-k estimator refusal arm (sweep_runner.py `_axes_counts`) —
# the d85c266f merge-forward composition (DEC-SWEEP-APRIORI-DEFAULT-001 ×
# DEC-SWEEP-ONECLICK-PREFLIGHT-ESTIMATOR-001) shipped this arm untested
# (coverage restore, 2026-07-19: line 977 + its branch were the ONE
# uncovered spot in this module — no pragma, genuinely exercised here).


def _extinction_k_request() -> SweepRunRequest:
    """A one-click-form-shaped request whose k is the EXPLICIT extinction
    token — the only shape that reaches `_axes_counts`' refusal arm (the
    one-click flow's own default k is the estimable institutional CSV)."""
    return SweepRunRequest(
        symbols="NIFTY",
        intervals=DEFAULT_INTERVALS,
        directions=DEFAULT_DIRECTIONS,
        k=DEFAULT_K,
        sl="50",
        tp="100",
        exchange="NSE",
        segment="INDEX",
        start=_TODAY.isoformat(),
        end=_TODAY.isoformat(),
        mode=DEFAULT_MODE,
        top=10,
    )


def test_axes_counts_refuses_extinction_k_loudly() -> None:
    """The adaptive extinction ladder cannot be enumerated ahead of time —
    `_axes_counts` must refuse LOUDLY (never a raw int() crash on the
    'extinction' token)."""
    from brutex.dashboard.sweep_runner import _axes_counts

    with pytest.raises(ValueError, match="not estimable ahead of time"):
        _axes_counts(_extinction_k_request())


def test_oneclick_preflight_for_plan_extinction_k_refuses_via_value_error(
    tmp_path: Path,
) -> None:
    """End-to-end through the public plan preflight: an explicit
    'extinction' k on the one-click form surfaces as the ValueError the
    route's ValueError→400 lane converts — a loud refusal, never a crash
    deeper in the estimator arithmetic."""
    plan = OneClickSweepPlan(sweep_run_id="ext-id", requests=(_extinction_k_request(),))
    with pytest.raises(ValueError, match="use the classic sweep form"):
        oneclick_preflight_for_plan(plan, lake_root=tmp_path, today=_TODAY, free_bytes=10**12)
