"""Pure-core tests for the dashboard sweep launcher (DEC-SWEEP-RUN-BUTTON-001).

100% line + branch coverage of :mod:`brutex.dashboard.sweep_runner` — every
validation arm of :func:`resolve_sweep_request` (defaults, blank-token fallbacks,
and the ValueError raises), the trivial path helpers, the 1:1
``cmd_sweep_all`` kwargs mapping, and — since
DEC-SWEEP-PREFLIGHT-ESTIMATOR-001 — the D8 launch fan-out
(:func:`resolve_sweep_launches`), the new refuse-whole defaults (all-21 TFs /
``k≤3`` / the 16-cell O-g SL/TP grid, each DERIVATION-pinned), the
``allow_missing_slices`` pin flip, the ``acknowledge_sla`` consent-axis
mapping, and the route-side merged preflight
(:func:`preflight_sweep_request` — real tmp lakes, real engine gate). The
``# pragma: no cover`` ``run_sweep_from_request`` shim is exercised by the CLI
integration suite.
"""

from __future__ import annotations

import fcntl
import os
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import polars as pl
import pytest
from brutex.dashboard.pull_controller import GROWW_HISTORY_FLOOR
from brutex.dashboard.sweep_runner import (
    DEFAULT_DIRECTIONS,
    DEFAULT_EXCHANGE,
    DEFAULT_FIB_ANCHORS,
    DEFAULT_INTERVALS,
    DEFAULT_K,
    DEFAULT_MODE,
    DEFAULT_SEGMENT,
    DEFAULT_SL,
    DEFAULT_START,
    DEFAULT_SYMBOLS,
    DEFAULT_TOP,
    DEFAULT_TP,
    SweepRunRequest,
    build_sweep_kwargs,
    default_results_root,
    preflight_sweep_request,
    resolve_sweep_launches,
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
    assert req.acknowledge_sla is False  # its own consent axis, default off


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
        "acknowledge_sla": False,
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
    # Pinned safe dashboard defaults (signal-only, refuse-whole, no overfit/stage3).
    assert kwargs["conditions"] is None
    assert kwargs["options_sim"] is False
    assert kwargs["overfit_gate"] is False
    assert kwargs["stage3_enable"] is False
    # O-a refuse-whole (DEC-SWEEP-PREFLIGHT-ESTIMATOR-001): the forgiving True
    # pin is SUPERSEDED — the one-click sweep hard-refuses on missing slices.
    assert kwargs["allow_missing_slices"] is False
    assert kwargs["acknowledge_sla_overrun"] is False
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


# ══════════════════════════════════════════════════════════════════════════════
# DEC-SWEEP-PREFLIGHT-ESTIMATOR-001 — defaults (backend of R1/R2, O-f/O-g/O-i)
# ══════════════════════════════════════════════════════════════════════════════


def test_default_intervals_are_all_21_from_lake_interval_labels() -> None:
    """Blank TFs ⇒ all 21 — rebuilt from ``LAKE_INTERVAL_LABELS``, never hand-typed."""
    from brutex.data.timeframes import LAKE_INTERVAL_LABELS

    tokens = DEFAULT_INTERVALS.split(",")
    assert len(tokens) == 21
    assert tokens == [LAKE_INTERVAL_LABELS[m] for m in sorted(LAKE_INTERVAL_LABELS)]
    assert tokens[0] == "1minute"
    assert tokens[-1] == "1day"


def test_default_k_is_1_2_3() -> None:
    """O-f: the dashboard k default grows to ``1,2,3`` (deeper k = warn, never block)."""
    assert DEFAULT_K == "1,2,3"


def test_default_sl_tp_derive_from_the_locked_mode_a_axes() -> None:
    """O-g DERIVATION pin (sanctioned-pending-operator-confirmation, R27).

    The two grid strings are axis-min / on-grid midpoint / axis-max of the
    LOCKED Mode-A stage-3 axes plus the ``none`` arm — traceable-derived, not
    invented. 4×4 = 16 cells = all 4 arms × the 3×3 value grid. A Mode-A axis
    change breaks this pin loudly, forcing a conscious re-derivation.
    """
    from brutex.engine.stage3_grid import MODE_A_SL_AXIS, MODE_A_TP_AXIS

    def _mid(lo: int, hi: int, tick: int) -> int:
        return ((lo + hi) // 2) // tick * tick

    sl_axis, tp_axis = MODE_A_SL_AXIS, MODE_A_TP_AXIS
    expected_sl = [
        "none",
        str(sl_axis.lo_paisa),
        str(_mid(sl_axis.lo_paisa, sl_axis.hi_paisa, sl_axis.tick_paisa)),
        str(sl_axis.hi_paisa),
    ]
    expected_tp = [
        "none",
        str(tp_axis.lo_paisa),
        str(_mid(tp_axis.lo_paisa, tp_axis.hi_paisa, tp_axis.tick_paisa)),
        str(tp_axis.hi_paisa),
    ]
    assert DEFAULT_SL == ",".join(expected_sl) == "none,5,100,200"
    assert DEFAULT_TP == ",".join(expected_tp) == "none,10,250,500"
    assert len(DEFAULT_SL.split(",")) * len(DEFAULT_TP.split(",")) == 16


def test_default_fib_anchors_match_cmd_sweep_all_default() -> None:
    """O-i drift pin: the preflight's anchor set == ``cmd_sweep_all``'s keyword default.

    The dashboard never passes ``fib_anchors`` to ``cmd_sweep_all`` (the
    function default applies), so the route-side estimate must use EXACTLY
    that default or the confirm gate would estimate a different launch.
    """
    import inspect

    from brutex.cli import cmd_sweep_all

    assert inspect.signature(cmd_sweep_all).parameters["fib_anchors"].default == (
        DEFAULT_FIB_ANCHORS
    )
    assert DEFAULT_FIB_ANCHORS == "prev_day"


def test_mixed_none_and_paisa_leg_tokens_round_trip() -> None:
    """The O-g grid grammar: ``none`` is a first-class TOKEN, mixable with paisa."""
    req = resolve_sweep_request(sl="none,5,100,200", tp="NONE,50", today=_TODAY)
    assert req.sl == "none,5,100,200"
    assert req.tp == "none,50"


def test_mixed_leg_bad_token_raises() -> None:
    with pytest.raises(ValueError, match="sl must be 'none' or positive paisa"):
        resolve_sweep_request(sl="none,5,oops", today=_TODAY)


def test_mixed_leg_non_positive_token_raises() -> None:
    with pytest.raises(ValueError, match="tp must be 'none' or positive paisa"):
        resolve_sweep_request(tp="none,-10", today=_TODAY)


def test_acknowledge_sla_maps_to_its_own_cmd_sweep_all_axis() -> None:
    """Consent axes stay separate (excision #4): SLA consent ⇒ acknowledge_sla_overrun.

    The estimate-proceed consent (``confirm_token``) is a ROUTE-level gate and
    never reaches the kwargs — no ``confirm``-shaped key exists in the mapping.
    """
    req = resolve_sweep_request(acknowledge_sla=True, today=_TODAY)
    assert req.acknowledge_sla is True
    kwargs = build_sweep_kwargs(req, lake_root=Path("/lake"))
    assert kwargs["acknowledge_sla_overrun"] is True
    assert "confirm_token" not in kwargs
    assert "confirm" not in kwargs


# ══════════════════════════════════════════════════════════════════════════════
# DEC-SWEEP-PREFLIGHT-ESTIMATOR-001 — D8 launch fan-out resolver
# ══════════════════════════════════════════════════════════════════════════════


def test_blank_symbols_fan_out_to_all_3_indices_nifty_first() -> None:
    """Blank symbols + blank coordinates ⇒ NSE (NIFTY,BANKNIFTY) then BSE (SENSEX,)."""
    launches = resolve_sweep_launches(today=_TODAY)
    assert [(lch.exchange, lch.segment, lch.symbols) for lch in launches] == [
        ("NSE", "INDEX", "NIFTY,BANKNIFTY"),
        ("BSE", "INDEX", "SENSEX"),
    ]


def test_fan_out_launch_order_derives_from_the_universe_declaration() -> None:
    """NIFTY-first is DERIVED from ``BRUTE_FORCE_INDICES`` order, never hand-typed."""
    from brutex.universe import BRUTE_FORCE_INDICES, ENGINE_SWEEP_SPOT_TRIPLES

    launches = resolve_sweep_launches(today=_TODAY)
    flattened = [
        (launch.exchange, symbol) for launch in launches for symbol in launch.symbols.split(",")
    ]
    assert flattened == [
        (spec.exchange.value, spec.underlying_symbol) for spec in BRUTE_FORCE_INDICES
    ]
    # Every fan-out coordinate is an engine triple (scope law, never expanded).
    for launch in launches:
        for symbol in launch.symbols.split(","):
            assert (launch.exchange, launch.segment, symbol) in ENGINE_SWEEP_SPOT_TRIPLES


def test_explicit_sensex_with_blank_coordinates_groups_to_bse() -> None:
    """``SENSEX`` no longer needs a manual ``exchange=BSE`` — the triple groups it."""
    launches = resolve_sweep_launches(symbols="SENSEX", today=_TODAY)
    assert [(lch.exchange, lch.segment, lch.symbols) for lch in launches] == [
        ("BSE", "INDEX", "SENSEX")
    ]


def test_explicit_symbols_group_by_triple_coordinates_first_seen_order() -> None:
    launches = resolve_sweep_launches(symbols="SENSEX,NIFTY,BANKNIFTY", today=_TODAY)
    assert [(lch.exchange, lch.symbols) for lch in launches] == [
        ("BSE", "SENSEX"),
        ("NSE", "NIFTY,BANKNIFTY"),
    ]


def test_duplicate_symbol_tokens_are_deduped_first_seen() -> None:
    launches = resolve_sweep_launches(symbols="NIFTY, NIFTY ,BANKNIFTY", today=_TODAY)
    assert [lch.symbols for lch in launches] == ["NIFTY,BANKNIFTY"]


def test_explicit_coordinates_keep_the_legacy_single_launch() -> None:
    """A non-blank exchange/segment is the operator's choice — one launch, byte-equal."""
    launches = resolve_sweep_launches(symbols="SENSEX", exchange="BSE", today=_TODAY)
    assert launches == (resolve_sweep_request(symbols="SENSEX", exchange="BSE", today=_TODAY),)


def test_explicit_segment_alone_also_keeps_the_single_launch() -> None:
    launches = resolve_sweep_launches(segment="INDEX", today=_TODAY)
    assert launches == (resolve_sweep_request(segment="INDEX", today=_TODAY),)


def test_explicit_mismatched_coordinates_still_refused_by_the_scope_gate() -> None:
    """``SENSEX`` pinned to NSE is a coordinate error — refused, never regrouped."""
    with pytest.raises(ValueError, match="sweep REFUSED"):
        resolve_sweep_launches(symbols="SENSEX", exchange="NSE", today=_TODAY)


def test_unknown_symbol_with_blank_coordinates_is_refused_by_the_scope_gate() -> None:
    """A non-engine symbol has no triple — it rides the default group and refuses."""
    with pytest.raises(ValueError, match="sweep REFUSED") as excinfo:
        resolve_sweep_launches(symbols="NIFTY,RELIANCE", today=_TODAY)
    assert "RELIANCE" in str(excinfo.value)


def test_fan_out_threads_the_shared_axes_to_every_launch() -> None:
    launches = resolve_sweep_launches(
        intervals="1minute",
        k="2",
        sl="none,5",
        tp="none",
        top=7,
        mode="B",
        start="2024-01-01",
        end="2024-02-01",
        acknowledge_sla=True,
        today=_TODAY,
    )
    assert len(launches) == 2
    for launch in launches:
        assert launch.intervals == "1minute"
        assert launch.k == "2"
        assert launch.sl == "none,5"
        assert launch.tp == "none"
        assert launch.top == 7
        assert launch.mode == "B"
        assert launch.start == "2024-01-01"
        assert launch.end == "2024-02-01"
        assert launch.acknowledge_sla is True


# ══════════════════════════════════════════════════════════════════════════════
# DEC-SWEEP-PREFLIGHT-ESTIMATOR-001 — route-side merged preflight (real gate)
# ══════════════════════════════════════════════════════════════════════════════

#: Injected clock + a short in-coverage window (mirrors the sweep_gate suite:
#: the expected sessions come from the REAL clamp, never a hand-guessed list).
_PF_TODAY = date(2025, 4, 1)
_PF_START = "2025-03-03"
_PF_END = "2025-03-07"


def _pf_expected_sessions() -> tuple[date, ...]:
    from brutex.engine.sweep_data_preflight import clamp_dates

    return clamp_dates(
        date.fromisoformat(_PF_START), date.fromisoformat(_PF_END), today=_PF_TODAY
    ).expected_sessions


def _write_days(lake: Path, exchange: str, symbol: str, days: tuple[date, ...]) -> None:
    """Seed real 1-minute month parquets holding one 09:30-IST bar per session."""
    by_month: dict[tuple[int, int], list[datetime]] = {}
    for day in days:
        by_month.setdefault((day.year, day.month), []).append(
            datetime(day.year, day.month, day.day, 4, 0, tzinfo=UTC)
        )
    for (year, month), timestamps in by_month.items():
        directory = lake / "bars" / exchange / "INDEX" / symbol / "1minute" / f"{year:04d}"
        directory.mkdir(parents=True, exist_ok=True)
        frame = pl.DataFrame(
            {"timestamp": timestamps}, schema={"timestamp": pl.Datetime("us", "UTC")}
        )
        frame.write_parquet(directory / f"{month:02d}.parquet")


def _pf_launches(**overrides: Any) -> tuple[SweepRunRequest, ...]:
    """The suite's small deterministic 2-launch fan-out (NSE NIFTY + BSE SENSEX)."""
    params: dict[str, Any] = {
        "symbols": "NIFTY,SENSEX",
        "intervals": "1minute",
        "k": "1",
        "sl": "none",
        "tp": "none",
        "start": _PF_START,
        "end": _PF_END,
        "today": _PF_TODAY,
    }
    params.update(overrides)
    return resolve_sweep_launches(**params)


def _run_preflight(lake: Path, launches: tuple[SweepRunRequest, ...]) -> dict[str, Any]:
    payload = preflight_sweep_request(
        launches,
        lake_root=lake,
        results_root=lake / "sweeps",
        today=_PF_TODAY,
        env={},
    )
    assert isinstance(payload, dict)
    return payload


def test_preflight_requires_at_least_one_launch(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="at least one resolved launch"):
        preflight_sweep_request((), lake_root=tmp_path)


def test_preflight_empty_lake_refuses_whole_across_the_fan_out(tmp_path: Path) -> None:
    """R4+O-a: BOTH launches refuse on an empty lake; the merge names both."""
    lake = tmp_path / "lake"
    lake.mkdir()
    payload = _run_preflight(lake, _pf_launches())
    assert payload["ok"] is False
    assert payload["confirm_token"] is None
    text = payload["refusal_text"]
    assert isinstance(text, str)
    assert "launch NSE/INDEX · NIFTY:" in text
    assert "launch BSE/INDEX · SENSEX:" in text
    assert "pull on /universe first" in text
    refusals = payload["refusals"]
    assert isinstance(refusals, list)
    assert any(entry["code"] == "coverage_missing" for entry in refusals)
    launches_out = payload["launches"]
    assert isinstance(launches_out, list)
    assert len(launches_out) == 2
    assert all(entry["payload"]["ok"] is False for entry in launches_out)
    # The estimate is SUMMED across launches even on a refusal (the operator
    # sees the cost of what was refused).
    estimate = payload["estimate"]
    assert isinstance(estimate, dict)
    assert estimate["trials"] == sum(
        entry["payload"]["estimate"]["trials"] for entry in launches_out
    )
    assert estimate["tuples"] == sum(
        entry["payload"]["estimate"]["tuples"] for entry in launches_out
    )


def test_preflight_seeded_lake_passes_and_mints_one_fanout_token(tmp_path: Path) -> None:
    """The ok merge: summed estimate + ONE token over the whole fan-out."""
    from brutex.dashboard.lake_revision import lake_revision
    from brutex.engine.sweep_estimate import selection_confirm_token, selection_from_raw

    lake = tmp_path / "lake"
    expected = _pf_expected_sessions()
    _write_days(lake, "NSE", "NIFTY", expected)
    _write_days(lake, "BSE", "SENSEX", expected)
    launches = _pf_launches()
    payload = _run_preflight(lake, launches)
    assert payload["ok"] is True
    assert payload["refusal_text"] == ""
    assert payload["refusals"] == []
    headline = payload["headline"]
    assert isinstance(headline, str)
    assert "proceed?" in headline
    assert "PROVISIONAL" in headline  # no calibration store on a fresh out_root
    # Trials/tuples are symmetric across the two 1-symbol launches ⇒ sum = 2×.
    launches_out = payload["launches"]
    assert isinstance(launches_out, list)
    per_launch = [entry["payload"]["estimate"]["trials"] for entry in launches_out]
    estimate = payload["estimate"]
    assert isinstance(estimate, dict)
    assert estimate["trials"] == sum(per_launch) == 2 * per_launch[0]
    assert estimate["est_hours"] == pytest.approx(estimate["est_seconds"] / 3600.0)
    # THE token: minted over the UNION selection + all launch coordinates +
    # the real lake_revision — recomputed independently here.
    first = launches[0]
    token_selection = selection_from_raw(
        symbols=["NIFTY", "SENSEX"],
        intervals=["1minute"],
        directions=first.directions.split(","),
        k_values=[1],
        stop_losses=(None,),
        take_profits=(None,),
        fib_anchors=(DEFAULT_FIB_ANCHORS,),
        condition_bits=None,
        top_k=first.top,
        start=date.fromisoformat(_PF_START),
        end=date.fromisoformat(_PF_END),
    )
    assert payload["confirm_token"] == selection_confirm_token(
        token_selection,
        launches=(("NSE", "INDEX"), ("BSE", "INDEX")),
        lake_token=lake_revision(lake),
    )
    # Identical calibration-source notes across launches appear ONCE (union).
    notes = payload["notes"]
    assert isinstance(notes, list)
    assert len(notes) == len(set(notes))
    assert any("calibration store" in note for note in notes)


def test_preflight_partial_fan_out_refuses_whole_naming_the_broken_launch(
    tmp_path: Path,
) -> None:
    """One ready launch + one not-ready launch ⇒ the WHOLE fan-out refuses (O-a)."""
    lake = tmp_path / "lake"
    _write_days(lake, "NSE", "NIFTY", _pf_expected_sessions())  # SENSEX not seeded
    payload = _run_preflight(lake, _pf_launches())
    assert payload["ok"] is False
    assert payload["confirm_token"] is None
    text = payload["refusal_text"]
    assert isinstance(text, str)
    assert "launch BSE/INDEX · SENSEX:" in text
    assert "launch NSE/INDEX · NIFTY:" not in text  # the ready launch is not blamed
    launches_out = payload["launches"]
    assert isinstance(launches_out, list)
    assert launches_out[0]["payload"]["ok"] is True  # named ready, but NOT run
    assert launches_out[1]["payload"]["ok"] is False


def test_preflight_identical_cross_launch_refusals_union_once(tmp_path: Path) -> None:
    """A held pull lock refuses BOTH launches with ONE unioned refusal entry."""
    lake = tmp_path / "lake"
    expected = _pf_expected_sessions()
    _write_days(lake, "NSE", "NIFTY", expected)
    _write_days(lake, "BSE", "SENSEX", expected)
    from brutex.data.sweep_exclusion import pull_lock_path

    lock_path = pull_lock_path(lake)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        payload = _run_preflight(lake, _pf_launches())
    finally:
        os.close(fd)
    assert payload["ok"] is False
    refusals = payload["refusals"]
    assert isinstance(refusals, list)
    pull_refusals = [entry for entry in refusals if entry["code"] == "pull_in_flight"]
    assert len(pull_refusals) == 1  # identical (code, message) unioned once


def test_preflight_absurd_k_warning_unions_once_and_never_blocks(tmp_path: Path) -> None:
    """R20: k>3 WARNS on both launches — one unioned warning, still ok."""
    lake = tmp_path / "lake"
    expected = _pf_expected_sessions()
    _write_days(lake, "NSE", "NIFTY", expected)
    _write_days(lake, "BSE", "SENSEX", expected)
    payload = _run_preflight(lake, _pf_launches(k="4"))
    assert payload["ok"] is True  # a warning, never a block (the SLA is the wall)
    estimate = payload["estimate"]
    assert isinstance(estimate, dict)
    assert estimate["absurd_k"] is True
    warnings = payload["warnings"]
    assert isinstance(warnings, list)
    assert len([w for w in warnings if w["code"] == "absurd_depth"]) == 1


def test_preflight_env_threads_to_the_estimator(tmp_path: Path) -> None:
    """The injected env drives the per-tuple fallback figure (fail-loud parse path)."""
    lake = tmp_path / "lake"
    expected = _pf_expected_sessions()
    _write_days(lake, "NSE", "NIFTY", expected)
    _write_days(lake, "BSE", "SENSEX", expected)
    payload = preflight_sweep_request(
        _pf_launches(),
        lake_root=lake,
        results_root=lake / "sweeps",
        today=_PF_TODAY,
        env={"BRUTEX_SWEEP_PER_TUPLE_SECONDS": "1.0"},
    )
    estimate = payload["estimate"]
    assert isinstance(estimate, dict)
    assert estimate["est_seconds_fallback"] == pytest.approx(float(estimate["tuples"]))


def test_preflight_defaults_results_root_to_lake_sweeps(tmp_path: Path) -> None:
    """``results_root=None`` ⇒ ``<lake>/sweeps`` — the cmd_sweep_all out_root law."""
    lake = tmp_path / "lake"
    lake.mkdir()
    payload = preflight_sweep_request(
        _pf_launches(symbols="NIFTY"), lake_root=lake, today=_PF_TODAY, env={}
    )
    notes = payload["notes"]
    assert isinstance(notes, list)
    assert any(str(lake / "sweeps") in note for note in notes)


def test_preflight_is_probe_only_nothing_written(tmp_path: Path) -> None:
    """R42: a refused (and an ok) preflight leaves the lake tree byte-identical."""
    lake = tmp_path / "lake"
    _write_days(lake, "NSE", "NIFTY", _pf_expected_sessions())

    def _snapshot() -> list[tuple[str, int, int]]:
        return [
            (str(p.relative_to(tmp_path)), p.stat().st_size, p.stat().st_mtime_ns)
            for p in sorted(tmp_path.rglob("*"))
        ]

    before = _snapshot()
    _run_preflight(lake, _pf_launches())  # refused (SENSEX missing)
    _run_preflight(lake, _pf_launches(symbols="NIFTY"))  # ok
    assert _snapshot() == before
    assert not (lake / "sweeps").exists()
    assert not (lake / ".locks").exists()


def test_merged_headline_label_arms() -> None:
    """The fan-out headline mirrors ``SweepEstimate.headline`` for every label arm."""
    from brutex.dashboard.sweep_runner import (
        _merged_headline,  # pyright: ignore[reportPrivateUsage]
    )

    provisional = _merged_headline(
        trials=72_676_800,
        est_seconds=181_440.0,
        display_bytes=140_000_000,
        dedup_map_bytes=0,
        provisional=True,
        calibration_samples=0,
        calibration_last_ts=None,
    )
    assert provisional == (
        "this selection = 72,676,800 trials, est. 50.4 h (PROVISIONAL — uncalibrated), "
        "~0.14 GB results — proceed?"
    )
    calibrated = _merged_headline(
        trials=1_000,
        est_seconds=3_600.0,
        display_bytes=1_000_000_000,
        dedup_map_bytes=2 * 2**30,
        provisional=False,
        calibration_samples=3,
        calibration_last_ts="2026-07-07T10:00:00Z",
    )
    assert calibrated == (
        "this selection = 1,000 trials, est. 1.0 h (calibrated from 3 runs, "
        "last 2026-07-07), ~1.00 GB results (+2.0 GiB dedup map reserve) — proceed?"
    )
    last_unknown = _merged_headline(
        trials=1,
        est_seconds=0.0,
        display_bytes=0,
        dedup_map_bytes=0,
        provisional=False,
        calibration_samples=1,
        calibration_last_ts=None,
    )
    assert "last unknown" in last_unknown
