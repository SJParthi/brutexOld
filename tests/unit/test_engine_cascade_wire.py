"""Unit tests for the Stage-4 cascade wiring (DEC-CASCADE-WIRE-001).

Strict 100% line + branch coverage of :mod:`brutex.engine.cascade_wire` — the
adapter that re-builds the cascade's ``CandidateInput``\\s from a finished sweep
and runs the nine-filter cascade per ``(instrument, tf)`` pool. Covers:

* the manifest data model + every validation branch (``CascadeTupleSpec``,
  ``BarsContext``);
* deterministic config / manifest / report (de)serialisation + every error path;
* ``CandidateInput`` reconstruction (bull + bear) over hand-built bars, the
  per-bar return series, the drawdown helper, and the Monte-Carlo exit closure;
* the shared core ``run_cascade_over_specs`` (group runs / no-bars skip / empty
  candidates skip) + the end-to-end auto/manual write paths;
* the **byte-identity** guarantee — the AUTOMATIC path and the MANUAL replay
  produce the same report bytes.
"""

from __future__ import annotations

from collections.abc import Callable, MutableMapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from brutex.data.models import CandleBar
from brutex.engine import cascade_wire as cw
from brutex.engine.combo_mask import mask_to_entry_signals
from brutex.engine.results_store import ResultRow
from brutex.engine.stage1a import build_bar_bits
from brutex.engine.sweep_driver import build_equity_curve
from brutex.engine.sweep_orchestrator import SweepTuple
from brutex.engine.trade_sim import simulate_trades
from brutex.metrics.cascade import (
    CandidateVerdict,
    CascadeReport,
    FilterStage,
)
from brutex.metrics.compute import compute_metrics
from brutex.metrics.deflated_sharpe import (
    GAUSSIAN_KURTOSIS,
    deflated_sharpe_ratio,
    expected_max_sharpe_ratio,
)
from brutex.strategies.types import FibAnchorKind, Side
from structlog.testing import capture_logs

_IST = timezone(timedelta(hours=5, minutes=30))


def _rid(seed: int) -> str:
    """Return a valid 64-char lowercase-hex run-identity from *seed*."""
    return f"{seed:064x}"


def _bar(minute: int, o: str, h: str, lo: str, c: str) -> CandleBar:
    """Build one IST-session CandleBar (stored UTC) with Decimal prices."""
    ts = datetime(2026, 5, 13, 9, minute, tzinfo=_IST).astimezone(UTC)
    return CandleBar(
        timestamp=ts,
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(lo),
        close=Decimal(c),
        volume=100,
        open_interest=None,
    )


def _session_bars() -> list[CandleBar]:
    """A small rising-then-falling intraday session (6 one-minute bars)."""
    return [
        _bar(16, "100.00", "101.00", "99.50", "100.50"),
        _bar(17, "100.50", "102.00", "100.00", "101.50"),
        _bar(18, "101.50", "101.80", "100.20", "100.40"),
        _bar(19, "100.40", "100.90", "99.80", "100.10"),
        _bar(20, "100.10", "100.30", "99.00", "99.20"),
        _bar(21, "99.20", "99.80", "98.50", "98.70"),
    ]


def _spec(**overrides: object) -> cw.CascadeTupleSpec:
    """Build a valid one-winner :class:`CascadeTupleSpec`, overridable by keyword."""
    base: dict[str, object] = {
        "instrument": "NIFTY",
        "tf_label": "1minute",
        "tf_minutes": 1,
        "direction_label": "bull",
        "sl_paisa": None,
        "tp_paisa": None,
        "prior_n": 3,
        "proximity_bps": None,
        "start_equity_paisa": 10_000_000,
        "winners": ((_rid(1), 0),),
    }
    base.update(overrides)
    return cw.CascadeTupleSpec(**base)  # type: ignore[arg-type]


class TestSpecFillModeManifestLaw:
    """DEC-FILL-WORST-CASE-ONLY-001: the unconditional manifest token + the
    precise-era refusal law (worst-case is the ONLY fill model)."""

    def test_spec_dict_carries_the_permanent_worst_case_token(self) -> None:
        payload = cw._spec_to_dict(_spec())
        assert payload["fill_mode"] == "worst_case"
        assert cw._spec_from_dict(payload) == _spec()

    def test_absent_key_refuses_as_precise_era(self) -> None:
        # Fails-before pin: a pre-DEC manifest (no fill_mode key — the old
        # PRECISE elide) must refuse LOUDLY naming the DEC, never replay.
        payload = cw._spec_to_dict(_spec())
        del payload["fill_mode"]
        with pytest.raises(
            cw.CascadeWireError,
            match=r"precise-era cascade manifest refused \(DEC-FILL-WORST-CASE-ONLY-001\)",
        ):
            cw._spec_from_dict(payload)

    def test_literal_precise_token_refuses(self) -> None:
        payload = cw._spec_to_dict(_spec())
        payload["fill_mode"] = "precise"
        with pytest.raises(
            cw.CascadeWireError,
            match=r"re-run the sweep to produce a worst-case manifest",
        ):
            cw._spec_from_dict(payload)

    def test_junk_fill_mode_refuses(self) -> None:
        payload = cw._spec_to_dict(_spec())
        payload["fill_mode"] = "worst"
        with pytest.raises(
            cw.CascadeWireError,
            match=r"precise-era cascade manifest refused \(DEC-FILL-WORST-CASE-ONLY-001\)",
        ):
            cw._spec_from_dict(payload)

    def test_spec_carries_no_fill_mode_field(self) -> None:
        # Killed-token pin: the config object carries NO mode — the law is
        # global, so nothing can thread a per-spec fill choice back in.
        from dataclasses import fields

        assert "fill_mode" not in {f.name for f in fields(cw.CascadeTupleSpec)}

    def test_candidates_for_spec_runs_the_one_law_deterministically(self) -> None:
        bars = _session_bars()
        a = cw.candidates_for_spec(_spec(winners=((_rid(1), 0),)), bars)
        b = cw.candidates_for_spec(_spec(winners=((_rid(1), 0),)), bars)
        assert len(a) == 1
        assert a[0].returns == b[0].returns  # no hidden mode input survives


def _bars_for(bars: list[CandleBar]) -> Callable[[str, str], list[CandleBar]]:
    """Return a ``bars_for`` that serves *bars* for NIFTY/1minute, else empty."""

    def loader(instrument: str, tf_label: str) -> list[CandleBar]:
        if (instrument, tf_label) == ("NIFTY", "1minute"):
            return bars
        return []

    return loader


# ── Fib-anchor multi-day history (DEC-NIFTY-FIB-AXIS-001) ─────────────────────
#
# A 12-trading-day 1-minute stream ending on 2021-01-19, whose per-day OHLC is
# hand-picked (mirroring ``test_engine_fib_axis._DAY_OHLC``) so the prev-day and
# rolling-weekly Fib windows for the test day are DISTINCT: on 2021-01-19 the
# rolling-weekly ``retrace_50`` sits at ₹10650 but the prev-day's does not, so a
# test-day flat at ₹10650 fires Group-4 Fib bit 22 under ROLLING_WEEKLY and NO
# test-day Fib bit under PREV_DAY. This is the fixture that makes the Stage-4
# cascade-anchor bug observable end-to-end through :func:`candidates_for_spec`.

_FIB_TRADING_DAYS: list[date] = [
    date(2021, 1, 4),
    date(2021, 1, 5),
    date(2021, 1, 6),
    date(2021, 1, 7),
    date(2021, 1, 8),
    date(2021, 1, 11),
    date(2021, 1, 12),
    date(2021, 1, 13),
    date(2021, 1, 14),
    date(2021, 1, 15),
    date(2021, 1, 18),
    date(2021, 1, 19),
]

_FIB_DAY_OHLC: dict[date, tuple[str, str, str, str]] = {
    date(2021, 1, 4): ("10000", "10100", "9950", "10050"),
    date(2021, 1, 5): ("10050", "10200", "10000", "10150"),
    date(2021, 1, 6): ("10150", "10300", "10100", "10250"),
    date(2021, 1, 7): ("10250", "10400", "10200", "10350"),
    date(2021, 1, 8): ("10350", "10500", "10300", "10450"),
    date(2021, 1, 11): ("10450", "10600", "10400", "10550"),
    date(2021, 1, 12): ("10550", "10700", "10500", "10650"),
    date(2021, 1, 13): ("10650", "11200", "10600", "10800"),  # week-2 HIGH
    date(2021, 1, 14): ("10800", "10900", "9800", "10100"),  # week-2 LOW
    date(2021, 1, 15): ("10100", "10500", "10050", "10300"),
    date(2021, 1, 18): ("10500", "11500", "10400", "10600"),  # widens rolling high
    date(2021, 1, 19): ("10600", "10700", "10500", "10650"),  # TEST day
}

#: The Group-4 Fib bit fired on 2021-01-19 by the rolling-weekly retrace_50
#: (₹10650) but NOT by the prev-day anchor — verified empirically against
#: :func:`build_bar_bits` on this fixture.
_ROLLING_ONLY_FIB_BIT: int = 22


def _fib_anchor_history(
    *, test_day_bars: int = 30, prices_around: str = "10650"
) -> list[CandleBar]:
    """Build the 12-day stream ending 2021-01-19; 01-19 trades flat at *prices_around*."""
    bars: list[CandleBar] = []
    for day in _FIB_TRADING_DAYS:
        o, h, low, c = _FIB_DAY_OHLC[day]
        base = datetime(day.year, day.month, day.day, 9, 15, tzinfo=_IST)
        if day == _FIB_TRADING_DAYS[-1]:
            for i in range(test_day_bars):
                px = prices_around
                bars.append(_bar_at(base + timedelta(minutes=i), px, px, px, px))
            continue
        # First bar establishes the day's open + extremes + close; interior flat.
        bars.append(_bar_at(base, o, h, low, c))
        for i in range(1, 5):
            bars.append(_bar_at(base + timedelta(minutes=i), c, c, c, c))
    return bars


def _lockbox_intraday_history() -> list[CandleBar]:
    """Twelve real trading days whose ±500-paisa legs close IN-day.

    Fixture repair for DEC-TRADESIM-OPEN-SESSION-GUARD-001: the original
    ``_fib_anchor_history()`` shape (flat interiors, each 5-bar day a
    session-tail hole) closed its SL/TP trades ONLY via cross-session fills
    against each NEXT day's first bar — the exact overnight-gap fills the
    open-position session guard now closes at the prior day's last stored
    bar (every trade 0-pnl → flat complement equity → an undefined train
    Sharpe). Alternating up/down days with in-day ±6-point ranges give the
    lockbox split REAL in-day trade variance on BOTH sides of the window.
    """
    bars: list[CandleBar] = []
    for n, day in enumerate(_FIB_TRADING_DAYS):
        base_px = 10_000 + 25 * n
        base_ts = datetime(day.year, day.month, day.day, 9, 15, tzinfo=_IST)
        up_day = n % 2 == 0
        for i in range(6):
            o = base_px + i
            high, low = (o + 6, o - 1) if up_day else (o + 1, o - 6)
            bars.append(
                _bar_at(base_ts + timedelta(minutes=i), str(o), str(high), str(low), str(o))
            )
    return bars


def _bar_at(ts_ist: datetime, o: str, h: str, low: str, c: str) -> CandleBar:
    return CandleBar(
        timestamp=ts_ist.astimezone(UTC),
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(low),
        close=Decimal(c),
        volume=1000,
        open_interest=None,
    )


def _fat_tailed_bars() -> list[CandleBar]:
    """A 40-bar gap-up session with two down-spikes → fat-tailed WORST-CASE PnL.

    RE-CRAFTED for DEC-FILL-WORST-CASE-ONLY-001 (the one hardcoded adverse-max
    law): a long ENTERS at the bar HIGH and a triggered TP fills at
    ``min(level, low)``, so a positive-Sharpe fixture needs the TRIGGER bar's
    LOW to clear the target — every regular bar GAPS UP (open = prior close
    + 3.00, low = open − 0.20, close = open + 1.00, high = close + 0.30), so
    the next bar's low (prior close + 2.80) sits above the TP level (entry
    high + 2.00 = prior close + 2.30 relative to its own bar) and the win
    banks exactly +200 paisa. The two down-spike bars (16: −4.00, 30: −6.00)
    inject the heavy tail: bar 30's dip breaches the entry-relative floor and
    the trade closes at the adverse ``min(level, low)`` for −330 paisa.
    Measured on this tree: 13 trades = twelve +200 and one −330 → Sharpe
    ≈ 1.127 (> the test's SR_0 ≈ 0.207) with a DEFINED, non-zero excess
    kurtosis ≈ 8.083 — the regime where the excess-vs-non-excess distinction
    is material to the DSR. Deterministic (no RNG).
    """
    seq: list[tuple[str, str, str, str]] = []
    price = 100.0
    for i in range(40):
        o = price + 3.0
        if i in (16, 30):  # fat DOWNSIDE spikes (different depths → tail variety)
            depth = 4.0 if i == 16 else 6.0
            high, low, c = o + 0.1, o - depth, o - depth + 1.0
        else:
            c = o + 1.0
            high, low = c + 0.3, o - 0.2
        seq.append((f"{o:.2f}", f"{high:.2f}", f"{low:.2f}", f"{c:.2f}"))
        price = c

    def _fbar(i: int, row: tuple[str, str, str, str]) -> CandleBar:
        ts = datetime(2026, 5, 13, 9, 15, tzinfo=_IST) + timedelta(minutes=i)
        o, h, lo, c = row
        return CandleBar(
            timestamp=ts.astimezone(UTC),
            open=Decimal(o),
            high=Decimal(h),
            low=Decimal(lo),
            close=Decimal(c),
            volume=100,
            open_interest=None,
        )

    return [_fbar(i, row) for i, row in enumerate(seq)]


def _fat_tailed_candidate() -> Any:
    """Reconstruct one fat-tailed :class:`CandidateInput` over :func:`_fat_tailed_bars`.

    ``sl=500 / tp=200`` over the gap-up session produces 13 worst-case trades
    (twelve +200, one −330) with a defined, non-zero excess kurtosis (≈8.08)
    — the fixture the DSR-kurtosis-convention regression tests share.
    """
    bars = _fat_tailed_bars()
    bar_bits = build_bar_bits(bars, proximity=None, prior_n=3)
    return cw.build_candidate_input(
        run_id=_rid(1),
        mask=0,
        bars=bars,
        bar_bits=bar_bits,
        direction_label="bull",
        tf_minutes=1,
        sl_paisa=500,
        tp_paisa=200,
        start_equity_paisa=10_000_000,
    )


# ── CascadeTupleSpec validation ──────────────────────────────────────────────


class TestCascadeTupleSpec:
    def test_valid_spec_round_trips(self) -> None:
        spec = _spec()
        assert spec.instrument == "NIFTY"
        assert spec.winners == ((_rid(1), 0),)

    @pytest.mark.parametrize("field", ["instrument", "tf_label", "direction_label"])
    def test_empty_label_raises(self, field: str) -> None:
        with pytest.raises(cw.CascadeWireError, match="must be non-empty"):
            _spec(**{field: ""})

    def test_bad_direction_raises(self) -> None:
        with pytest.raises(cw.CascadeWireError, match="bull.*bear"):
            _spec(direction_label="HOLD")

    def test_tf_minutes_below_one_raises(self) -> None:
        with pytest.raises(cw.CascadeWireError, match="tf_minutes must be >= 1"):
            _spec(tf_minutes=0)

    @pytest.mark.parametrize("leg", ["sl_paisa", "tp_paisa"])
    def test_non_positive_leg_raises(self, leg: str) -> None:
        with pytest.raises(cw.CascadeWireError, match="must be > 0 or None"):
            _spec(**{leg: 0})

    def test_legs_accept_none_and_positive(self) -> None:
        spec = _spec(sl_paisa=50, tp_paisa=100)
        assert spec.sl_paisa == 50
        assert spec.tp_paisa == 100

    def test_prior_n_below_one_raises(self) -> None:
        with pytest.raises(cw.CascadeWireError, match="prior_n must be >= 1"):
            _spec(prior_n=0)

    def test_non_positive_equity_raises(self) -> None:
        with pytest.raises(cw.CascadeWireError, match="start_equity_paisa must be > 0"):
            _spec(start_equity_paisa=0)

    def test_bad_proximity_length_raises(self) -> None:
        with pytest.raises(cw.CascadeWireError, match="proximity_bps must be a 3-tuple"):
            _spec(proximity_bps=(10, 20))

    def test_valid_proximity_round_trips(self) -> None:
        spec = _spec(proximity_bps=(10, 20, 30))
        assert spec.proximity_bps == (10, 20, 30)

    @pytest.mark.parametrize("bad", ["abc", "A" * 64, "f" * 63])
    def test_malformed_run_id_raises(self, bad: str) -> None:
        with pytest.raises(cw.CascadeWireError, match="lowercase hex"):
            _spec(winners=((bad, 0),))

    @pytest.mark.parametrize("mask", [-1, cw._MASK_BITS_MAX + 1])
    def test_out_of_range_mask_raises(self, mask: int) -> None:
        with pytest.raises(cw.CascadeWireError, match="out of range"):
            _spec(winners=((_rid(1), mask),))

    def test_empty_winners_is_valid(self) -> None:
        assert _spec(winners=()).winners == ()


class TestBarsContext:
    def test_valid_context(self) -> None:
        ctx = cw.BarsContext(exchange="NSE", segment="INDEX", start="2021-01-01", end="2021-02-01")
        assert ctx.exchange == "NSE"

    @pytest.mark.parametrize("field", ["exchange", "segment", "start", "end"])
    def test_empty_field_raises(self, field: str) -> None:
        kwargs = {"exchange": "NSE", "segment": "INDEX", "start": "a", "end": "b"}
        kwargs[field] = ""
        with pytest.raises(cw.CascadeWireError, match="must be non-empty"):
            cw.BarsContext(**kwargs)  # type: ignore[arg-type]


# ── Cascade config helpers ────────────────────────────────────────────────────


class TestCascadeConfig:
    def test_seed_is_deterministic_from_sweep_id(self) -> None:
        cfg_a = cw.cascade_config_for_sweep("deadbeef" * 8)
        cfg_b = cw.cascade_config_for_sweep("deadbeef" * 8)
        assert cfg_a.seed == cfg_b.seed
        assert 0 <= cfg_a.seed < 2**31

    def test_overrides_applied(self) -> None:
        cfg = cw.cascade_config_for_sweep("a" * 64, overrides={"dsr_min": 0.5})
        assert cfg.dsr_min == 0.5

    def test_empty_sweep_id_raises(self) -> None:
        with pytest.raises(cw.CascadeWireError, match="must be non-empty"):
            cw.cascade_config_for_sweep("")

    def test_non_hex_sweep_id_raises(self) -> None:
        with pytest.raises(cw.CascadeWireError, match="not hex"):
            cw.cascade_config_for_sweep("zzzzzzzzzzzzzzzz")

    def test_config_dict_round_trips(self) -> None:
        cfg = cw.cascade_config_for_sweep("a" * 64)
        restored = cw.cascade_config_from_dict(cw.cascade_config_to_dict(cfg))
        assert restored == cfg

    def test_config_from_bad_dict_raises(self) -> None:
        # An unknown field is a structural error → TypeError → CascadeWireError.
        with pytest.raises(cw.CascadeWireError, match="invalid cascade_config"):
            cw.cascade_config_from_dict({"seed": 1, "not_a_real_field": 7})


# ── Pure numeric helpers ──────────────────────────────────────────────────────


class TestNumericHelpers:
    def test_finite_passes_real_value(self) -> None:
        assert cw._finite(1.5) == 1.5

    def test_finite_maps_none_to_zero(self) -> None:
        assert cw._finite(None) == 0.0

    @pytest.mark.parametrize("bad", [float("inf"), float("-inf"), float("nan")])
    def test_finite_maps_non_finite_to_zero(self, bad: float) -> None:
        assert cw._finite(bad) == 0.0

    def test_per_bar_returns(self) -> None:
        out = cw._per_bar_returns((1_100, 1_100, 900), start_equity_paisa=1_000)
        assert out == (0.1, 0.0, -0.2)

    def test_max_drawdown_empty_is_zero(self) -> None:
        assert cw._max_drawdown_return_units(()) == 0.0

    def test_max_drawdown_monotonic_is_zero(self) -> None:
        assert cw._max_drawdown_return_units((0.1, 0.2, 0.1)) == 0.0

    def test_max_drawdown_picks_largest_decline(self) -> None:
        # cumulative: 0.5, 0.3, 0.6, 0.1 → peak 0.6, trough 0.1 → dd 0.5.
        assert cw._max_drawdown_return_units((0.5, -0.2, 0.3, -0.5)) == pytest.approx(0.5)


# ── CandidateInput reconstruction ─────────────────────────────────────────────


class TestBuildCandidateInput:
    def _bar_bits(self, bars: list[CandleBar], spec: cw.CascadeTupleSpec) -> list[int]:
        from brutex.engine.stage1a import build_bar_bits

        return build_bar_bits(bars, proximity=None, prior_n=spec.prior_n)

    def test_reconstructs_bull_candidate(self) -> None:
        bars = _session_bars()
        spec = _spec()
        cand = cw.candidates_for_spec(spec, bars)[0]
        assert cand.candidate_id == _rid(1)
        # Per-bar return series is the equal-length pool axis (one per bar).
        assert len(cand.returns) == len(bars)
        assert len(cand.lockbox_days) == len(bars)
        assert cand.mc_n_bars == len(bars)
        # Every scalar metric is finite (the CandidateInput constructor enforces it).
        assert np.isfinite(cand.sharpe_ratio)
        assert np.isfinite(cand.training_sharpe)
        assert cand.training_max_drawdown >= 0.0

    def test_reconstructs_bear_candidate(self) -> None:
        bars = _session_bars()
        cand = cw.candidates_for_spec(_spec(direction_label="bear"), bars)[0]
        assert cand.candidate_id == _rid(1)
        assert cand.mc_n_bars == len(bars)

    def test_candidates_for_spec_with_proximity(self) -> None:
        bars = _session_bars()
        cands = cw.candidates_for_spec(_spec(proximity_bps=(10, 10, 10)), bars)
        assert len(cands) == 1

    def test_candidates_for_spec_empty_winners(self) -> None:
        assert cw.candidates_for_spec(_spec(winners=()), _session_bars()) == []

    def test_candidates_for_spec_uses_the_swept_fib_anchor(self) -> None:
        """A ROLLING_WEEKLY spec re-scores against rolling-weekly Fib levels (MAJOR fix).

        The Stage-4 cascade re-simulation MUST build its ``bar_bits`` at the
        tuple's OWN swept Fib anchor (DEC-NIFTY-FIB-AXIS-001) — before the fix
        :func:`candidates_for_spec` called ``build_bar_bits`` at the DEFAULT
        prev-day anchor, silently re-scoring a non-prev-day survivor against the
        WRONG Fib levels.

        The winner mask requires the Group-4 Fib bit that fires on 2021-01-19
        ONLY under ROLLING_WEEKLY (₹10650 is rolling's ``retrace_50`` but not
        prev-day's). Every test-day bar trades flat AT that level:
          * PREV_DAY   → the bit never fires → 0 entry signals;
          * ROLLING_WEEKLY → the bit fires on every test-day bar → 30 entries.
        The reconstructed candidate's entry-signal count therefore differs by
        anchor. FAILS BEFORE the fix (both anchors would score prev-day → 0
        entries either way), PASSES AFTER.
        """
        bars = _fib_anchor_history()
        mask = 1 << _ROLLING_ONLY_FIB_BIT

        def entry_count(anchor: FibAnchorKind) -> int:
            spec = _spec(winners=((_rid(1), mask),), fib_anchor=anchor)
            # The wire reconstructs a real CandidateInput from the spec (smoke: a
            # non-empty candidate is produced for the swept anchor).
            assert cw.candidates_for_spec(spec, bars)
            # The winner's entry signals are the SUPERSET-match of the mask over
            # the reconstructed bar_bits at the spec's anchor — count them via the
            # same builder + predicate the wire uses internally.
            bar_bits = build_bar_bits(bars, prior_n=spec.prior_n, fib_anchor=anchor)
            return sum(mask_to_entry_signals(bar_bits, mask))

        prev_entries = entry_count(FibAnchorKind.PREV_DAY)
        roll_entries = entry_count(FibAnchorKind.ROLLING_WEEKLY)
        # Anchor-sensitivity of the fixture (guards the fixture, not the fix).
        assert prev_entries == 0
        assert roll_entries == 30

        # The FIX proof: the anchor the wire threads into ``build_bar_bits`` is the
        # spec's swept anchor, so the reconstructed bar_bits (and hence entry
        # signals) DIFFER between the prev-day and rolling-weekly specs.
        captured: list[FibAnchorKind] = []
        real_build = build_bar_bits

        def spy(bars_arg: object, **kwargs: object) -> object:
            captured.append(kwargs["fib_anchor"])  # type: ignore[arg-type]
            return real_build(bars_arg, **kwargs)  # type: ignore[arg-type]

        from brutex.engine import stage1a

        original = stage1a.build_bar_bits
        stage1a.build_bar_bits = spy  # type: ignore[assignment]
        try:
            cw.candidates_for_spec(
                _spec(winners=((_rid(1), mask),), fib_anchor=FibAnchorKind.ROLLING_WEEKLY),
                bars,
            )
        finally:
            stage1a.build_bar_bits = original  # type: ignore[assignment]
        assert captured == [FibAnchorKind.ROLLING_WEEKLY]

    def test_candidates_for_spec_defaults_to_prev_day_anchor(self) -> None:
        """An unset ``fib_anchor`` threads PREV_DAY — the default path is byte-identical."""
        bars = _fib_anchor_history()
        mask = 1 << _ROLLING_ONLY_FIB_BIT
        captured: list[FibAnchorKind] = []
        real_build = build_bar_bits

        def spy(bars_arg: object, **kwargs: object) -> object:
            captured.append(kwargs["fib_anchor"])  # type: ignore[arg-type]
            return real_build(bars_arg, **kwargs)  # type: ignore[arg-type]

        from brutex.engine import stage1a

        original = stage1a.build_bar_bits
        stage1a.build_bar_bits = spy  # type: ignore[assignment]
        try:
            cw.candidates_for_spec(_spec(winners=((_rid(1), mask),)), bars)
        finally:
            stage1a.build_bar_bits = original  # type: ignore[assignment]
        assert captured == [FibAnchorKind.PREV_DAY]

    def test_mc_evaluate_returns_finite_or_nan(self) -> None:
        bars = _session_bars()
        cand = cw.candidates_for_spec(_spec(sl_paisa=50, tp_paisa=50), bars)[0]
        # Many entries → at least one trade closes → a (possibly defined) Sharpe.
        many = cand.mc_evaluate(np.array([0, 1, 2, 3], dtype=np.intp))
        assert isinstance(many, float)
        # A single entry → at most one trade → undefined per-trade Sharpe → nan.
        one = cand.mc_evaluate(np.array([0], dtype=np.intp))
        assert np.isnan(one)

    def test_kurtosis_is_nonexcess_matching_compute_sibling(self) -> None:
        """The wire fills ``CandidateInput.kurtosis`` as NON-excess (DEC-DSR-KURTOSIS-FIX-001).

        Regression for the DSR-kurtosis convention bug: the cascade
        :class:`~brutex.metrics.cascade.CandidateInput` (and the DSR PSR
        radicand it feeds) wants the NON-excess (Pearson) kurtosis — Gaussian
        ``3`` — but :func:`compute_metrics` stores EXCESS kurtosis (Gaussian
        ``0``). The wire must therefore add :data:`GAUSSIAN_KURTOSIS` back,
        exactly as the sibling :func:`with_stage4_statistics` does. The OLD
        code passed the raw excess value (no ``+3``), so this asserts the
        candidate's kurtosis equals the independently-recomputed
        ``compute_metrics(...).kurtosis + GAUSSIAN_KURTOSIS`` over the SAME
        reconstructed trades.
        """
        bars = _fat_tailed_bars()
        bar_bits = build_bar_bits(bars, proximity=None, prior_n=3)
        cand = _fat_tailed_candidate()

        # Independently re-run the exact reconstruction pipeline the wire uses,
        # then take compute_metrics' EXCESS kurtosis as the sibling's input.
        signals = mask_to_entry_signals(bar_bits, 0)
        trades = simulate_trades(
            bars,
            signals,
            direction=Side.BUY,
            tf_minutes=1,
            stop_loss_paisa=500,
            take_profit_paisa=200,
        )
        equity = build_equity_curve(bars, trades, start_equity_paisa=10_000_000, tf_minutes=1)
        metrics = compute_metrics(trades, equity, total_bars=len(bars))

        assert metrics.kurtosis is not None  # fixture sanity: a DEFINED excess kurtosis
        assert metrics.kurtosis != 0.0  # … that is genuinely fat-tailed (not a Gaussian no-op)
        # The smoking gun: wire kurtosis is the NON-excess form, +3 above the
        # excess value compute_metrics stores. The old buggy wire returned
        # `metrics.kurtosis` directly (== excess), which this would catch.
        assert cand.kurtosis == metrics.kurtosis + GAUSSIAN_KURTOSIS
        assert cand.kurtosis != metrics.kurtosis  # the +3 actually moved the value

    def test_kurtosis_none_defaults_to_gaussian_nonexcess(self) -> None:
        """An undefined-shape (constant-PnL) reconstruction yields the Gaussian baseline.

        When the trades are all identical (zero variance), ``compute_metrics``
        returns ``kurtosis=None``; the wire maps ``None`` → ``0.0`` via
        :func:`cw._finite` and THEN adds :data:`GAUSSIAN_KURTOSIS`, so the
        candidate carries the honest non-excess Gaussian value ``3.0`` (NOT
        the old ``0.0``, which the DSR would have mis-read as a non-excess
        kurtosis of zero).
        """
        # A FLAT session (o == h == l == c on every bar): under the one
        # worst-case law every fill lands at the same price, so the
        # reconstructed PnL is constant (zero) → kurtosis undefined.
        flat = [_bar(16 + i, "100.00", "100.00", "100.00", "100.00") for i in range(6)]
        bars = flat
        bar_bits = build_bar_bits(bars, proximity=None, prior_n=3)
        cand = cw.build_candidate_input(
            run_id=_rid(1),
            mask=0,
            bars=bars,
            bar_bits=bar_bits,
            direction_label="bull",
            tf_minutes=1,
            sl_paisa=20,
            tp_paisa=20,
            start_equity_paisa=10_000_000,
        )
        signals = mask_to_entry_signals(bar_bits, 0)
        trades = simulate_trades(
            bars,
            signals,
            direction=Side.BUY,
            tf_minutes=1,
            stop_loss_paisa=20,
            take_profit_paisa=20,
        )
        equity = build_equity_curve(bars, trades, start_equity_paisa=10_000_000, tf_minutes=1)
        metrics = compute_metrics(trades, equity, total_bars=len(bars))
        assert metrics.kurtosis is None  # fixture sanity: shape moment undefined
        assert cand.kurtosis == GAUSSIAN_KURTOSIS  # 0.0 (from _finite(None)) + 3.0

    def test_nonexcess_kurtosis_lowers_overstated_dsr_for_passing_combo(self) -> None:
        """The corrected kurtosis de-biases the DSR kill-gate toward REJECT (the fix's point).

        Feeding the candidate's now-non-excess kurtosis through
        :func:`deflated_sharpe_ratio` exactly as the cascade kill-gate does
        (``brutex/metrics/cascade.py`` filter #2) yields a LOWER DSR than the
        OLD excess value — for a combo whose observed Sharpe BEATS the
        deflation benchmark ``SR_0`` (the gate-relevant PASS regime). That is
        precisely the over-statement DEC-DSR-KURTOSIS-FIX-001 removes: under
        the old bug a luckier-than-real combo's DSR was inflated, biasing the
        gate toward PASS.

        (Honest envelope: the PSR's monotonicity in kurtosis flips sign with
        ``SR_hat - SR_0``; for a would-FAIL combo whose Sharpe is already
        below ``SR_0`` the corrected DSR can be marginally higher, but it
        stays below the gate. The bias that matters — surviving combos —
        is the PASS regime tested here.)
        """
        cand = _fat_tailed_candidate()
        excess_kurtosis = cand.kurtosis - GAUSSIAN_KURTOSIS

        # PASS regime: a small leaderboard so SR_0 < the observed Sharpe.
        n_trials, trial_var = 5, 0.03
        sr_0 = expected_max_sharpe_ratio(n_trials, trial_var)
        assert cand.sharpe_ratio > sr_0  # gate-relevant: this combo would PASS

        def _dsr(kurtosis: float) -> float:
            return deflated_sharpe_ratio(
                cand.sharpe_ratio,
                n_trials=n_trials,
                trial_sharpe_variance=trial_var,
                sample_length=max(cand.total_trades, 2),
                skew=cand.skew,
                kurtosis=kurtosis,
            ).deflated_sharpe_ratio

        old_overstated = _dsr(excess_kurtosis)  # the buggy excess-kurtosis DSR
        new_corrected = _dsr(cand.kurtosis)  # the fixed non-excess-kurtosis DSR
        assert new_corrected < old_overstated


# ── Lockbox train/holdout split + trade evidence (DEC-STATS-GATE-INTEGRITY-001) ──


class TestLockboxTrainingSplit:
    """R2-SR-F1 (train-complement training refs) + R2-SR-F2 (trade evidence)."""

    _WINDOW = 3
    _MIN_TRADES = 1

    def _build(self, bars: list[CandleBar], **overrides: object) -> Any:
        bar_bits = build_bar_bits(bars, proximity=None, prior_n=3)
        kwargs: dict[str, object] = {
            "run_id": _rid(1),
            "mask": 0,
            "bars": bars,
            "bar_bits": bar_bits,
            "direction_label": "bull",
            "tf_minutes": 1,
            "sl_paisa": None,
            "tp_paisa": None,
            "start_equity_paisa": 10_000_000,
            "lockbox_default_window_days": self._WINDOW,
            "lockbox_min_trades": self._MIN_TRADES,
        }
        kwargs.update(overrides)
        return cw.build_candidate_input(**kwargs)  # type: ignore[arg-type]

    def test_training_refs_are_the_train_complement_not_the_full_series(self) -> None:
        """THE R2-SR-F1 regression (fails before the fix).

        Over a 12-day history with a 3-day lockbox window, the training
        Sharpe/MDD must equal the values independently recomputed over ONLY
        the per-bar returns strictly BEFORE the resolved window start — and
        must DIFFER from the full-series values the old code fed (the
        contaminated reference that contained the holdout tail).
        """
        from brutex.calendars.nse import IST
        from brutex.metrics.cpcv import sharpe_of_returns
        from brutex.metrics.lockbox import resolve_lockbox_window

        bars = _lockbox_intraday_history()
        # SL/TP legs so trades CLOSE IN-day throughout the 12-day history
        # (with no legs the single mask-0 position holds to end_of_data and
        # the complement equity is flat — the fallback case tested
        # separately; the in-day ranges are the DEC-TRADESIM-OPEN-SESSION-
        # GUARD-001 fixture repair — see _lockbox_intraday_history).
        cand = self._build(bars, sl_paisa=500, tp_paisa=500)

        # Independent recompute of the exact reconstruction chain.
        bar_bits = build_bar_bits(bars, proximity=None, prior_n=3)
        trades = simulate_trades(
            bars,
            mask_to_entry_signals(bar_bits, 0),
            direction=Side.BUY,
            tf_minutes=1,
            stop_loss_paisa=500,
            take_profit_paisa=500,
        )
        equity = build_equity_curve(bars, trades, start_equity_paisa=10_000_000, tf_minutes=1)
        per_bar = cw._per_bar_returns(equity.equity_paisa, 10_000_000)
        day_ordinals = [b.timestamp.astimezone(IST).date().toordinal() for b in bars]
        trade_days = tuple(sorted(t.exit_ts.astimezone(IST).date().toordinal() for t in trades))
        window = resolve_lockbox_window(
            day_ordinals,
            evidence_days=trade_days,
            default_window_days=self._WINDOW,
            min_trades=self._MIN_TRADES,
        )
        train = [r for r, d in zip(per_bar, day_ordinals, strict=True) if d < window.window_start]
        expected_sharpe = sharpe_of_returns(train)
        assert expected_sharpe is not None  # fixture sanity: a REAL complement
        full_sharpe = sharpe_of_returns(per_bar)
        assert full_sharpe is not None
        assert expected_sharpe != full_sharpe  # the fixture separates the two

        assert cand.training_sharpe == expected_sharpe
        assert cand.training_sharpe != full_sharpe  # the OLD (contaminated) value
        # The MDD reference is the complement's too (fed as ONE pair with the
        # Sharpe). In THIS fixture the deepest drawdown happens to lie inside
        # the complement, so the full-series MDD coincides numerically — the
        # Sharpe inequality above is what pins the old-code regression.
        assert cand.training_max_drawdown == cw._max_drawdown_return_units(train)
        assert cand.training_max_drawdown <= cw._max_drawdown_return_units(per_bar)

    def test_lockbox_trade_days_are_sorted_ist_exit_ordinals(self) -> None:
        # R2-SR-F2: the evidence is the sorted per-trade EXIT-day IST ordinals,
        # one per closed trade, every value inside the bar-day span.
        from brutex.calendars.nse import IST

        bars = _session_bars()
        cand = cw.candidates_for_spec(_spec(sl_paisa=50, tp_paisa=50), bars)[0]
        bar_bits = build_bar_bits(bars, proximity=None, prior_n=3)
        trades = simulate_trades(
            bars,
            mask_to_entry_signals(bar_bits, 0),
            direction=Side.BUY,
            tf_minutes=1,
            stop_loss_paisa=50,
            take_profit_paisa=50,
        )
        expected = tuple(sorted(t.exit_ts.astimezone(IST).date().toordinal() for t in trades))
        trade_days = cand.lockbox_trade_days
        assert trade_days is not None  # narrows tuple[int, ...] | None for pyright
        assert trade_days == expected
        assert len(trade_days) == cand.total_trades > 0
        day_ordinals = [b.timestamp.astimezone(IST).date().toordinal() for b in bars]
        assert all(day_ordinals[0] <= d <= day_ordinals[-1] for d in trade_days)

    def test_degenerate_complement_falls_back_to_full_series_loudly(self) -> None:
        # Review A3: a single-session slice puts EVERY trade inside the lockbox
        # window → the train complement is empty → the training refs fall back
        # to the legacy FULL-series pair AS ONE UNIT, with ONE loud warning.
        bars = _session_bars()
        with capture_logs() as logs:
            cand = self._build(bars)
        events = [e for e in logs if e["event"] == "cascade_wire.training_ref_fallback"]
        assert len(events) == 1
        # Legacy-equal refs: the full per-bar series pair.
        bar_bits = build_bar_bits(bars, proximity=None, prior_n=3)
        trades = simulate_trades(
            bars,
            mask_to_entry_signals(bar_bits, 0),
            direction=Side.BUY,
            tf_minutes=1,
            stop_loss_paisa=None,
            take_profit_paisa=None,
        )
        equity = build_equity_curve(bars, trades, start_equity_paisa=10_000_000, tf_minutes=1)
        per_bar = cw._per_bar_returns(equity.equity_paisa, 10_000_000)
        from brutex.metrics.cpcv import sharpe_of_returns

        assert cand.training_sharpe == cw._finite(sharpe_of_returns(per_bar))
        assert cand.training_max_drawdown == cw._max_drawdown_return_units(per_bar)

    def test_zero_trade_candidate_has_empty_evidence_and_legacy_refs(self) -> None:
        # A mask whose bit never fires under PREV_DAY → zero entries → zero
        # trades → empty evidence tuple + the legacy (flat-series) refs.
        bars = _fib_anchor_history()
        with capture_logs() as logs:
            cand = self._build(bars, mask=1 << _ROLLING_ONLY_FIB_BIT)
        assert cand.lockbox_trade_days == ()
        assert cand.total_trades == 0
        # Flat equity → full-series Sharpe undefined → _finite(None) == 0.0.
        assert cand.training_sharpe == 0.0
        assert cand.training_max_drawdown == 0.0
        assert [e for e in logs if e["event"] == "cascade_wire.training_ref_fallback"]

    def test_config_lockbox_params_thread_into_the_resolver(self) -> None:
        # Review A1 (the config-threading regression): a NON-default lockbox
        # window/floor on the CascadeConfig must reach the wire's resolver call
        # — a hard-coded module default here would re-introduce the
        # train/holdout mismatch for any non-default config.
        captured: list[tuple[int, int]] = []
        real = cw.resolve_lockbox_window

        def spy(days: object, **kwargs: object) -> object:
            captured.append(
                (int(kwargs["default_window_days"]), int(kwargs["min_trades"]))  # type: ignore[arg-type, call-overload]
            )
            return real(days, **kwargs)  # type: ignore[arg-type]

        config = cw.cascade_config_for_sweep(
            "a" * 64, overrides={"lockbox_default_window_days": 11, "lockbox_min_trades": 7}
        )
        original = cw.resolve_lockbox_window
        cw.resolve_lockbox_window = spy  # type: ignore[assignment]
        try:
            cw.run_cascade_over_specs(
                [_spec()],
                bars_for=_bars_for(_session_bars()),
                config=config,
                sweep_id="a" * 64,
            )
        finally:
            cw.resolve_lockbox_window = original  # type: ignore[assignment]
        assert captured == [(11, 7)]

    def test_complete_log_carries_n_provisional(self) -> None:
        # R2-SR-F4: the summary structlog event surfaces the provisional count
        # (0 here — the lone reconstructed candidate dies at the hard gates).
        with capture_logs() as logs:
            report = cw.run_cascade_over_specs(
                [_spec()],
                bars_for=_bars_for(_session_bars()),
                config=cw.cascade_config_for_sweep("a" * 64),
                sweep_id="a" * 64,
            )
        (event,) = [e for e in logs if e["event"] == "cascade_wire.complete"]
        assert event["n_provisional"] == 0
        assert report.n_provisional == 0


# ── The shared pure core ──────────────────────────────────────────────────────


class TestRunCascadeOverSpecs:
    def test_runs_per_pool(self) -> None:
        bars = _session_bars()
        report = cw.run_cascade_over_specs(
            [_spec()],
            bars_for=_bars_for(bars),
            config=cw.cascade_config_for_sweep("a" * 64),
            sweep_id="a" * 64,
        )
        assert report.sweep_id == "a" * 64
        assert len(report.groups) == 1
        instrument, tf_label, _ = report.groups[0]
        assert (instrument, tf_label) == ("NIFTY", "1minute")
        assert report.n_input == 1
        # One trade << the 30-trade hard gate ⇒ rejected, never crashes.
        assert report.n_survived == 0

    def test_skips_pool_with_no_bars(self) -> None:
        report = cw.run_cascade_over_specs(
            [_spec()],
            bars_for=lambda _i, _t: [],
            config=cw.cascade_config_for_sweep("a" * 64),
            sweep_id="a" * 64,
        )
        assert report.groups == ()
        assert report.n_input == 0
        assert report.survivor_ids == ()

    def test_skips_pool_with_no_candidates(self) -> None:
        report = cw.run_cascade_over_specs(
            [_spec(winners=())],
            bars_for=_bars_for(_session_bars()),
            config=cw.cascade_config_for_sweep("a" * 64),
            sweep_id="a" * 64,
        )
        assert report.groups == ()


# ── ADR-0012 futures roll-schedule carriage (review round-1) ─────────────────

#: The pinned NIFTY-futures series alias (import-identity asserted in
#: test_futures_series / test_universe; spelled literally to self-describe).
_FUT_ALIAS = "NSE-NIFTY-FUT"
#: An arbitrary two-roll schedule (expiry day-ordinals).
_FUT_SCHEDULE = (date(2026, 5, 28).toordinal(), date(2026, 6, 25).toordinal())


class TestFuturesRollScheduleCarriage:
    """Stage-4 re-scores a futures pool under the SWEEP's roll schedule — never
    schedule-blind (review round-1 findings 3/6/11/13/17/21: the automatic
    cascade rebuilt futures winners' bar_bits with ``roll_schedule=None`` over
    roll-stitched bars, silently violating the D11 hard-reset law)."""

    def _spy(self, calls: list[tuple[str, tuple[int, ...] | None]]) -> Callable[..., list[Any]]:
        def spy(
            spec: cw.CascadeTupleSpec,
            bars: Sequence[CandleBar],
            *,
            lockbox_default_window_days: int,
            lockbox_min_trades: int,
            roll_schedule: tuple[int, ...] | None = None,
            compute_regime_evidence: bool = False,
        ) -> list[Any]:
            calls.append((spec.instrument, roll_schedule))
            return []

        return spy

    def test_pool_schedule_threads_into_candidates_for_spec(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Fails-before: run_cascade_over_specs never passed roll_schedule, so
        # the spy recorded None for the futures pool.
        calls: list[tuple[str, tuple[int, ...] | None]] = []
        monkeypatch.setattr(cw, "candidates_for_spec", self._spy(calls))
        cw.run_cascade_over_specs(
            [_spec(), _spec(instrument=_FUT_ALIAS)],
            bars_for=lambda _i, _t: _session_bars(),
            config=cw.cascade_config_for_sweep("a" * 64),
            sweep_id="a" * 64,
            roll_schedules={_FUT_ALIAS: _FUT_SCHEDULE},
        )
        assert sorted(calls) == [
            ("NIFTY", None),  # spot pools stay on the byte-identical None path
            (_FUT_ALIAS, _FUT_SCHEDULE),
        ]

    @pytest.mark.parametrize("schedules", [None, {}], ids=["no-map", "alias-missing"])
    def test_futures_pool_without_schedule_skips_loudly(
        self,
        monkeypatch: pytest.MonkeyPatch,
        schedules: dict[str, tuple[int, ...]] | None,
    ) -> None:
        # A pre-carriage manifest replay must never re-score a futures pool
        # schedule-blind: the pool SKIPS with one WARN naming the remedy.
        calls: list[tuple[str, tuple[int, ...] | None]] = []
        monkeypatch.setattr(cw, "candidates_for_spec", self._spy(calls))
        with capture_logs() as logs:
            report = cw.run_cascade_over_specs(
                [_spec(instrument=_FUT_ALIAS)],
                bars_for=lambda _i, _t: _session_bars(),
                config=cw.cascade_config_for_sweep("a" * 64),
                sweep_id="a" * 64,
                roll_schedules=schedules,
            )
        assert report.groups == ()
        assert calls == []  # candidates never rebuilt schedule-blind
        warned = [
            rec
            for rec in logs
            if rec["event"] == "cascade_wire.futures_pool_skipped_no_roll_schedule"
        ]
        assert len(warned) == 1
        assert warned[0]["instrument"] == _FUT_ALIAS
        assert "sweep-all" in str(warned[0]["remedy"])

    def test_spot_pool_with_a_schedule_entry_refuses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Round-3 finding 2, the POSITIVE invariant direction at the shared
        # core: a schedule entry for a NON-alias pool must never be applied
        # silently (pre-fix `roll_schedules.get(instrument)` threaded it into
        # candidates_for_spec → build_bar_bits(roll_schedule=...), breaking
        # the replay's byte-identity contract, then dying on Stage-1A's raw
        # beyond-schedule ValueError for a short schedule).
        calls: list[tuple[str, tuple[int, ...] | None]] = []
        monkeypatch.setattr(cw, "candidates_for_spec", self._spy(calls))
        with pytest.raises(cw.CascadeWireError, match="non-futures instrument 'NIFTY'"):
            cw.run_cascade_over_specs(
                [_spec()],
                bars_for=lambda _i, _t: _session_bars(),
                config=cw.cascade_config_for_sweep("a" * 64),
                sweep_id="a" * 64,
                roll_schedules={"NIFTY": _FUT_SCHEDULE},
            )
        assert calls == []  # the schedule never reached a spot bar_bits build

    def test_auto_path_records_schedules_and_replay_threads_them(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # AUTOMATIC: the launch's resolved map lands in the manifest…
        cw.run_and_write_from_specs(
            tmp_path,
            sweep_id="a" * 64,
            specs=[_spec()],
            bars_for=_bars_for(_session_bars()),
            bars_context=cw.BarsContext(
                exchange="NSE", segment="FNO", start="2026-05-01", end="2026-06-30"
            ),
            cascade_config=cw.cascade_config_for_sweep("a" * 64),
            roll_schedule_by_instrument={_FUT_ALIAS: _FUT_SCHEDULE},
        )
        manifest = cw.load_manifest(tmp_path)
        assert manifest.roll_schedules == ((_FUT_ALIAS, _FUT_SCHEDULE),)
        # …and the MANUAL replay hands the SAME frozen schedules to the core.
        seen: dict[str, object] = {}

        def spy_core(*args: Any, **kwargs: Any) -> cw.CascadeWireReport:
            seen.update(kwargs)
            return cw.CascadeWireReport(sweep_id="a" * 64, groups=())

        monkeypatch.setattr(cw, "run_cascade_over_specs", spy_core)
        cw.run_and_write_from_manifest(tmp_path, bars_for=_bars_for(_session_bars()))
        assert seen["roll_schedules"] == {_FUT_ALIAS: _FUT_SCHEDULE}

    def test_spot_only_auto_path_records_none(self, tmp_path: Path) -> None:
        cw.run_and_write_from_specs(
            tmp_path,
            sweep_id="a" * 64,
            specs=[_spec()],
            bars_for=_bars_for(_session_bars()),
            bars_context=cw.BarsContext(
                exchange="NSE", segment="INDEX", start="2021-01-01", end="2021-02-01"
            ),
            cascade_config=cw.cascade_config_for_sweep("a" * 64),
        )
        assert cw.load_manifest(tmp_path).roll_schedules is None


class TestCheckReplayRollSchedule:
    """The replay's frozen-vs-re-derived schedule cross-check (review round-2
    findings 1/4/9): `load_bars_for_manifest` stitches bars from the CURRENT
    lake while `run_and_write_from_manifest` re-scores under the manifest's
    FROZEN schedule — a drifted lake must REFUSE loudly naming both schedules,
    never silently rewrite the report under mismatched Stage-1A resets."""

    def test_matching_schedules_pass(self) -> None:
        cw.check_replay_roll_schedule(
            instrument=_FUT_ALIAS,
            frozen_schedules={_FUT_ALIAS: _FUT_SCHEDULE},
            fresh_schedule=_FUT_SCHEDULE,
        )

    def test_drifted_lake_refuses_naming_both_schedules(self) -> None:
        fresh = (*_FUT_SCHEDULE, date(2026, 7, 30).toordinal())  # a pulled successor
        with pytest.raises(cw.CascadeWireError) as excinfo:
            cw.check_replay_roll_schedule(
                instrument=_FUT_ALIAS,
                frozen_schedules={_FUT_ALIAS: _FUT_SCHEDULE},
                fresh_schedule=fresh,
            )
        message = str(excinfo.value)
        assert str(list(_FUT_SCHEDULE)) in message  # the FROZEN schedule named
        assert str(list(fresh)) in message  # the re-derived schedule named
        assert "contract set changed" in message
        assert "brutex sweep-all" in message  # the remediation

    def test_backfilled_head_contract_refuses_too(self) -> None:
        # The silent-D11 direction: a head contract backfilled AFTER the
        # launch (the head_unowned WARN's own remediation) PREPENDS an expiry.
        fresh = (date(2026, 4, 30).toordinal(), *_FUT_SCHEDULE)
        with pytest.raises(cw.CascadeWireError, match="contract set changed"):
            cw.check_replay_roll_schedule(
                instrument=_FUT_ALIAS,
                frozen_schedules={_FUT_ALIAS: _FUT_SCHEDULE},
                fresh_schedule=fresh,
            )

    @pytest.mark.parametrize(
        "frozen", [None, {}, {"OTHER": _FUT_SCHEDULE}], ids=["no-map", "empty", "other-instrument"]
    )
    def test_pre_carriage_manifest_abstains(
        self, frozen: dict[str, tuple[int, ...]] | None
    ) -> None:
        # No frozen schedule for this instrument ⇒ nothing to enforce — the
        # pool is loudly SKIPPED downstream by run_cascade_over_specs instead.
        cw.check_replay_roll_schedule(
            instrument=_FUT_ALIAS,
            frozen_schedules=frozen,
            fresh_schedule=_FUT_SCHEDULE,
        )


# ── Report serialisation ──────────────────────────────────────────────────────


def _manual_report() -> cw.CascadeWireReport:
    """A hand-built report carrying BOTH a survivor and a rejected verdict."""
    inner = CascadeReport(
        verdicts=(
            CandidateVerdict(
                candidate_id=_rid(1),
                passed=True,
                rejected_at=None,
                detail="survived all Stage-4 filters",
                composite_score=0.75,
                # R2-SR-F4: a provisional=True pass here vs the default-False
                # rejected verdict below, so the serializer is value-pinned for
                # BOTH flag states (hostile C1 — never only the default). A
                # hand-built serializer fixture: the leaderboard dsr stamp
                # below is not cross-validated against the flag.
                provisional=True,
                # DEC-RESULTS-CASCADE-VERDICT-JOIN-001: a MEASURED stage-3
                # rate here vs the default-None rejected verdict below, so
                # the serializer is value-pinned for BOTH states.
                cpcv_pass_rate=0.875,
            ),
            CandidateVerdict(
                candidate_id=_rid(2),
                passed=False,
                rejected_at=FilterStage.HARD_GATES,
                detail="total_trades 1 < 30",
                composite_score=None,
            ),
        ),
        survivor_ids=(_rid(1),),
        n_input=2,
        n_survived=1,
        stage_survivors={FilterStage.HARD_GATES.value: 1},
        # DEC-PBO-SIDECAR-001: a MEASURED pool PBO on one group...
        pool_pbo=0.25,
        # ...and DEC-TRIAL-STATS-CONSUME-001: a LEADERBOARD-sourced DSR stamp
        # here vs the "pool"/None defaults on the skipped group below, so the
        # serializer is value-pinned for BOTH states (the same hostile-C1 rule).
        dsr_n_source="leaderboard",
        dsr_n_trials=841,
        dsr_trial_variance=0.0625,
    )
    skipped = CascadeReport(
        verdicts=(),
        survivor_ids=(),
        n_input=0,
        n_survived=0,
        stage_survivors={},
        # ...and a SKIPPED (None → null) one on the other, so the serializer is
        # value-pinned for BOTH states (hostile C1 — never only the default).
        pool_pbo=None,
    )
    return cw.CascadeWireReport(
        sweep_id="a" * 64,
        groups=(("NIFTY", "1minute", inner), ("SENSEX", "5minute", skipped)),
    )


class TestReportSerialisation:
    def test_wire_report_to_dict_both_verdict_kinds(self) -> None:
        payload = cw.wire_report_to_dict(_manual_report())
        assert payload["version"] == cw.REPORT_VERSION
        assert payload["n_survived"] == 1
        verdicts = payload["groups"][0]["report"]["verdicts"]  # type: ignore[index]
        assert verdicts[0]["rejected_at"] is None
        assert verdicts[1]["rejected_at"] == FilterStage.HARD_GATES.value

    def test_pool_pbo_serialises_measured_and_skipped(self) -> None:
        # DEC-PBO-SIDECAR-001: the raw float round-trips (never rounded) and a
        # skipped filter serialises as JSON null — both states pinned.
        payload = cw.wire_report_to_dict(_manual_report())
        assert payload["groups"][0]["report"]["pool_pbo"] == 0.25  # type: ignore[index]
        assert payload["groups"][1]["report"]["pool_pbo"] is None  # type: ignore[index]
        raw = cw.serialize_wire_report(_manual_report())
        assert b'"pool_pbo":0.25' in raw
        assert b'"pool_pbo":null' in raw

    def test_dsr_stamp_serialises_both_states(self) -> None:
        # DEC-TRIAL-STATS-CONSUME-001: the deflation-source stamp round-trips
        # for the leaderboard state AND the legacy pool/None defaults.
        payload = cw.wire_report_to_dict(_manual_report())
        first = payload["groups"][0]["report"]  # type: ignore[index]
        assert first["dsr_n_source"] == "leaderboard"  # type: ignore[index]
        assert first["dsr_n_trials"] == 841  # type: ignore[index]
        assert first["dsr_trial_variance"] == 0.0625  # type: ignore[index]
        second = payload["groups"][1]["report"]  # type: ignore[index]
        assert second["dsr_n_source"] == "pool"  # type: ignore[index]
        assert second["dsr_n_trials"] is None  # type: ignore[index]
        raw = cw.serialize_wire_report(_manual_report())
        assert b'"dsr_n_source":"leaderboard"' in raw
        assert b'"dsr_n_source":"pool"' in raw
        assert b'"dsr_n_trials":null' in raw

    def test_serialize_is_deterministic(self) -> None:
        report = _manual_report()
        assert cw.serialize_wire_report(report) == cw.serialize_wire_report(report)

    def test_provisional_serialises_both_states(self) -> None:
        # R2-SR-F4: the ADDITIVE per-verdict provisional flag round-trips for
        # BOTH states (True on the pass, default False on the rejection).
        payload = cw.wire_report_to_dict(_manual_report())
        verdicts = payload["groups"][0]["report"]["verdicts"]  # type: ignore[index]
        assert verdicts[0]["provisional"] is True
        assert verdicts[1]["provisional"] is False
        raw = cw.serialize_wire_report(_manual_report())
        assert b'"provisional":true' in raw
        assert b'"provisional":false' in raw

    def test_report_properties(self) -> None:
        report = _manual_report()
        assert report.n_input == 2
        assert report.n_survived == 1
        assert report.survivor_ids == (_rid(1),)
        # R2-SR-F4: the aggregate counts the flagged pass in the first group
        # (the skipped second group contributes zero).
        assert report.n_provisional == 1


# ── Manifest serialisation ────────────────────────────────────────────────────


def _manifest(specs: tuple[cw.CascadeTupleSpec, ...] | None = None) -> cw.CascadeManifest:
    return cw.CascadeManifest(
        version=cw.MANIFEST_VERSION,
        sweep_id="a" * 64,
        cascade_config=cw.cascade_config_for_sweep("a" * 64),
        bars_context=cw.BarsContext(
            exchange="NSE", segment="INDEX", start="2021-01-01", end="2021-02-01"
        ),
        specs=(_spec(),) if specs is None else specs,
    )


class TestManifestSerialisation:
    def test_round_trip_preserves_everything(self) -> None:
        # Two specs exercise BOTH legs of every nullable field: the first has
        # sl/tp/proximity all None, the second has them all set.
        manifest = _manifest(
            specs=(_spec(), _spec(proximity_bps=(10, 20, 30), sl_paisa=50, tp_paisa=100))
        )
        restored = cw.manifest_from_json(cw.manifest_to_json(manifest))
        assert restored == manifest

    def test_round_trip_is_byte_stable(self) -> None:
        manifest = _manifest()
        once = cw.manifest_to_json(manifest)
        twice = cw.manifest_to_json(cw.manifest_from_json(once))
        assert once == twice

    def test_round_trip_preserves_spec_fib_anchor(self) -> None:
        # DEC-NIFTY-FIB-AXIS-001 (Stage-4 cascade anchor threading): a non-prev-day
        # spec's swept anchor must round-trip through the manifest so the anchor
        # survives serialize→deserialize (else the cascade would re-score prev-day).
        manifest = _manifest(specs=(_spec(fib_anchor=FibAnchorKind.ROLLING_WEEKLY),))
        restored = cw.manifest_from_json(cw.manifest_to_json(manifest))
        assert restored == manifest
        assert restored.specs[0].fib_anchor is FibAnchorKind.ROLLING_WEEKLY

    def test_pre_axis_spec_without_fib_anchor_key_parses(self) -> None:
        # Additive-parse precedent: a manifest written before the Fib axis (no
        # ``fib_anchor`` key on a spec) parses as PREV_DAY (byte-compatible).
        import json

        payload = json.loads(cw.manifest_to_json(_manifest(specs=(_spec(),))))
        del payload["specs"][0]["fib_anchor"]
        restored = cw.manifest_from_json(json.dumps(payload).encode("utf-8"))
        assert restored.specs[0].fib_anchor is FibAnchorKind.PREV_DAY

    def test_roll_schedules_round_trip(self) -> None:
        # ADR-0012 carriage (review round-1): the futures schedules survive
        # serialize → deserialize so the MANUAL replay uses the sweep's own
        # per-contract hard resets.
        base = _manifest()
        manifest = cw.CascadeManifest(
            version=base.version,
            sweep_id=base.sweep_id,
            cascade_config=base.cascade_config,
            bars_context=base.bars_context,
            specs=base.specs,
            roll_schedules=(("NSE-NIFTY-FUT", (739034, 739062)),),
        )
        restored = cw.manifest_from_json(cw.manifest_to_json(manifest))
        assert restored == manifest
        assert restored.roll_schedules == (("NSE-NIFTY-FUT", (739034, 739062)),)

    def test_pre_carriage_manifest_without_roll_schedules_key_parses(self) -> None:
        # A manifest written before the schedule carriage has no key at all —
        # it parses with roll_schedules=None (the additive-parse precedent).
        import json

        payload = json.loads(cw.manifest_to_json(_manifest()))
        del payload["roll_schedules"]
        restored = cw.manifest_from_json(json.dumps(payload).encode("utf-8"))
        assert restored.roll_schedules is None

    def test_malformed_roll_schedules_raises(self) -> None:
        import json

        payload = json.loads(cw.manifest_to_json(_manifest()))
        payload["roll_schedules"] = [["NSE-NIFTY-FUT", ["not-an-ordinal"]]]
        with pytest.raises(cw.CascadeWireError, match="invalid roll_schedules"):
            cw.manifest_from_json(json.dumps(payload).encode("utf-8"))

    @pytest.mark.parametrize(
        "schedule",
        [[True, 739030], [739000.9, 739030], ["739000", 739030]],
        ids=["bool", "float", "int-string"],
    )
    def test_non_int_ordinal_types_refused_not_coerced(self, schedule: list[object]) -> None:
        # Round-4 finding 2: the parse boundary documented itself as THE
        # validator for a hand-edited manifest, yet bare `int()` silently
        # coerced `true` → 1, truncated 739000.9 → 739000, and parsed an
        # int-string — now strictly int-not-bool, mirroring the audit reader
        # `_schedules_from_entries`.
        import json

        payload = json.loads(cw.manifest_to_json(_manifest()))
        payload["roll_schedules"] = [["NSE-NIFTY-FUT", schedule]]
        with pytest.raises(cw.CascadeWireError, match="non-int roll_schedule ordinal"):
            cw.manifest_from_json(json.dumps(payload).encode("utf-8"))

    def test_duplicate_instrument_roll_schedules_entry_refused_at_parse(self) -> None:
        # Round-7 finding 8: the parse boundary validated each entry's alias
        # + shape but never UNIQUENESS — both `dict(manifest.roll_schedules)`
        # consumers silently adopted whichever contradictory entry sorted
        # LAST, while the audit twin `_schedules_from_entries` refuses the
        # same per-instrument disagreement. The parse boundary now refuses
        # identically.
        import json

        payload = json.loads(cw.manifest_to_json(_manifest()))
        payload["roll_schedules"] = [
            ["NSE-NIFTY-FUT", [738000, 738030]],
            ["NSE-NIFTY-FUT", [738000, 738031]],
        ]
        with pytest.raises(cw.CascadeWireError, match="duplicate entry for instrument"):
            cw.manifest_from_json(json.dumps(payload).encode("utf-8"))

    def test_spot_instrument_roll_schedules_entry_refused_at_parse(self) -> None:
        # Round-3 finding 2: the manifest has no integrity digest, so a
        # hand-edited/corrupt `roll_schedules` entry for a SPOT instrument
        # was trusted verbatim and silently rebuilt that pool's bar_bits
        # WITH per-contract hard resets. The presence ⟺ alias invariant is
        # now enforced at the parse boundary.
        import json

        payload = json.loads(cw.manifest_to_json(_manifest()))
        payload["roll_schedules"] = [["NIFTY", [738000, 738100]]]
        with pytest.raises(cw.CascadeWireError, match="not.*the NIFTY-futures series alias"):
            cw.manifest_from_json(json.dumps(payload).encode("utf-8"))

    @pytest.mark.parametrize(
        "schedule",
        [[], [738100, 738000], [738000, 738000], [-5]],
        ids=["empty", "decreasing", "duplicate", "negative"],
    )
    def test_shape_violating_roll_schedule_refused_at_parse(self, schedule: list[int]) -> None:
        # The SweepRunConfig shape law (non-empty, strictly increasing,
        # non-negative ordinals), mirrored at the manifest parse boundary.
        import json

        payload = json.loads(cw.manifest_to_json(_manifest()))
        payload["roll_schedules"] = [["NSE-NIFTY-FUT", schedule]]
        with pytest.raises(cw.CascadeWireError, match="strictly increasing"):
            cw.manifest_from_json(json.dumps(payload).encode("utf-8"))

    def test_bad_json_raises(self) -> None:
        with pytest.raises(cw.CascadeWireError, match="not valid JSON"):
            cw.manifest_from_json(b"{not json")

    def test_non_object_json_raises(self) -> None:
        with pytest.raises(cw.CascadeWireError, match="must be a JSON object"):
            cw.manifest_from_json(b"[1, 2, 3]")

    def test_version_mismatch_raises(self) -> None:
        data = cw.manifest_to_json(_manifest())
        bumped = data.replace(b'"version":1', b'"version":99')
        with pytest.raises(cw.CascadeWireError, match="unsupported cascade manifest version"):
            cw.manifest_from_json(bumped)

    def test_missing_field_raises(self) -> None:
        import json

        payload = json.loads(cw.manifest_to_json(_manifest()))
        del payload["bars_context"]
        with pytest.raises(cw.CascadeWireError, match="missing/invalid field"):
            cw.manifest_from_json(json.dumps(payload).encode("utf-8"))

    def test_bad_spec_in_manifest_raises(self) -> None:
        import json

        payload = json.loads(cw.manifest_to_json(_manifest()))
        del payload["specs"][0]["tf_minutes"]
        with pytest.raises(cw.CascadeWireError, match="invalid cascade tuple spec"):
            cw.manifest_from_json(json.dumps(payload).encode("utf-8"))


# ── Stage-3 manifest record (DEC-STAGE3-WIRE-001, review F3) ─────────────────


def _stage3_cell_with(*, rel_path: str | None) -> cw.Stage3CellRecord:
    """A valid cell record with an explicit rel_path (DEC-ROW-PROVENANCE-001)."""
    return cw.Stage3CellRecord(
        instrument="NIFTY",
        tf_label="1minute",
        direction_label="bull",
        k=1,
        sl_paisa=85,
        tp_paisa=60,
        proximity_bps=None,
        rel_path=rel_path,
    )


def _stage3_cell(**overrides: object) -> cw.Stage3CellRecord:
    base: dict[str, object] = {
        "instrument": "NIFTY",
        "tf_label": "1minute",
        "direction_label": "bull",
        "k": 1,
        "sl_paisa": 80,
        "tp_paisa": 50,
        "proximity_bps": None,
    }
    base.update(overrides)
    return cw.Stage3CellRecord(**base)  # type: ignore[arg-type]


class TestStage3CellRecord:
    @pytest.mark.parametrize("field", ["instrument", "tf_label"])
    def test_empty_label_raises(self, field: str) -> None:
        with pytest.raises(cw.CascadeWireError, match=f"{field} must be non-empty"):
            _stage3_cell(**{field: ""})

    def test_bad_direction_raises(self) -> None:
        with pytest.raises(cw.CascadeWireError, match="'bull' or 'bear'"):
            _stage3_cell(direction_label="long")

    def test_negative_k_raises(self) -> None:
        with pytest.raises(cw.CascadeWireError, match="k must be >= 0"):
            _stage3_cell(k=-1)

    @pytest.mark.parametrize("leg", ["sl_paisa", "tp_paisa"])
    def test_non_positive_leg_raises(self, leg: str) -> None:
        # Refined cells are always quantized on-grid ints — a disabled leg
        # cannot reach the manifest.
        with pytest.raises(cw.CascadeWireError, match=f"{leg} must be > 0"):
            _stage3_cell(**{leg: 0})

    def test_bad_proximity_length_raises(self) -> None:
        with pytest.raises(cw.CascadeWireError, match="3-tuple or None"):
            _stage3_cell(proximity_bps=(5, 10))

    def test_valid_cell_with_proximity_constructs(self) -> None:
        cell = _stage3_cell(proximity_bps=(5, 10, 25), k=0)
        assert cell.proximity_bps == (5, 10, 25)
        assert cell.k == 0


class TestStage3ManifestRecord:
    def test_enabled_requires_train_hash(self) -> None:
        with pytest.raises(cw.CascadeWireError, match="requires a train_snapshot_hash"):
            cw.Stage3ManifestRecord(enabled=True, train_snapshot_hash=None, refined_cells=())

    def test_disabled_forbids_hash_and_cells(self) -> None:
        with pytest.raises(cw.CascadeWireError, match="forbids"):
            cw.Stage3ManifestRecord(enabled=False, train_snapshot_hash="ab" * 32, refined_cells=())
        with pytest.raises(cw.CascadeWireError, match="forbids"):
            cw.Stage3ManifestRecord(
                enabled=False, train_snapshot_hash=None, refined_cells=(_stage3_cell(),)
            )

    def test_valid_records_construct(self) -> None:
        disabled = cw.Stage3ManifestRecord(
            enabled=False, train_snapshot_hash=None, refined_cells=()
        )
        enabled = cw.Stage3ManifestRecord(
            enabled=True, train_snapshot_hash="ab" * 32, refined_cells=(_stage3_cell(),)
        )
        assert disabled.enabled is False
        assert enabled.refined_cells[0].instrument == "NIFTY"


class TestStage3ManifestSerialisation:
    def test_round_trip_preserves_stage3_record(self) -> None:
        # Cells exercise BOTH proximity legs (None + set).
        record = cw.Stage3ManifestRecord(
            enabled=True,
            train_snapshot_hash="ab" * 32,
            refined_cells=(_stage3_cell(), _stage3_cell(proximity_bps=(5, 10, 25))),
        )
        manifest = cw.CascadeManifest(
            version=cw.MANIFEST_VERSION,
            sweep_id="a" * 64,
            cascade_config=cw.cascade_config_for_sweep("a" * 64),
            bars_context=cw.BarsContext(
                exchange="NSE", segment="INDEX", start="2021-01-01", end="2021-02-01"
            ),
            specs=(_spec(),),
            stage3=record,
        )
        restored = cw.manifest_from_json(cw.manifest_to_json(manifest))
        assert restored == manifest
        assert restored.stage3 == record

    def test_stage3_none_round_trips(self) -> None:
        restored = cw.manifest_from_json(cw.manifest_to_json(_manifest()))
        assert restored.stage3 is None

    def test_pre_stage3_manifest_without_key_parses(self) -> None:
        # Backward compatibility: manifests written before DEC-STAGE3-WIRE-001
        # carry no "stage3" key at all — they parse with stage3=None.
        import json

        payload = json.loads(cw.manifest_to_json(_manifest()))
        del payload["stage3"]
        restored = cw.manifest_from_json(json.dumps(payload).encode("utf-8"))
        assert restored.stage3 is None

    def test_bad_stage3_record_raises(self) -> None:
        import json

        record = cw.Stage3ManifestRecord(
            enabled=True, train_snapshot_hash="ab" * 32, refined_cells=(_stage3_cell(),)
        )
        manifest = _manifest()
        payload = json.loads(
            cw.manifest_to_json(
                cw.CascadeManifest(
                    version=manifest.version,
                    sweep_id=manifest.sweep_id,
                    cascade_config=manifest.cascade_config,
                    bars_context=manifest.bars_context,
                    specs=manifest.specs,
                    stage3=record,
                )
            )
        )
        del payload["stage3"]["refined_cells"][0]["sl_paisa"]
        with pytest.raises(cw.CascadeWireError, match="invalid stage3 record"):
            cw.manifest_from_json(json.dumps(payload).encode("utf-8"))

    def test_auto_path_records_stage3_in_the_manifest(self, tmp_path: Path) -> None:
        record = cw.Stage3ManifestRecord(
            enabled=True, train_snapshot_hash="ab" * 32, refined_cells=(_stage3_cell(),)
        )
        cw.run_and_write_from_specs(
            tmp_path,
            sweep_id="a" * 64,
            specs=[_spec()],
            bars_for=_bars_for(_session_bars()),
            bars_context=cw.BarsContext(
                exchange="NSE", segment="INDEX", start="2021-01-01", end="2021-02-01"
            ),
            cascade_config=cw.cascade_config_for_sweep("a" * 64),
            stage3=record,
        )
        assert cw.load_manifest(tmp_path).stage3 == record


# ── Per-leaderboard trial-stats freeze (DEC-TRIAL-STATS-FREEZE-001) ──────────


def _lbt(**overrides: object) -> cw.LeaderboardTrialsRecord:
    base: dict[str, object] = {
        "instrument": "NIFTY",
        "tf_label": "1minute",
        "n_trials": 10,
        "n_finite": 8,
        "sum_sharpe": 1.5,
        "sum_sharpe_sq": 2.25,
        "complete": True,
    }
    base.update(overrides)
    return cw.LeaderboardTrialsRecord(**base)  # type: ignore[arg-type]


class TestLeaderboardTrialsRecord:
    @pytest.mark.parametrize("field", ["instrument", "tf_label"])
    def test_empty_label_raises(self, field: str) -> None:
        with pytest.raises(cw.CascadeWireError, match=f"{field} must be non-empty"):
            _lbt(**{field: ""})

    def test_invalid_moments_delegate_to_the_core_validation(self) -> None:
        # §11 COMMON: the slice-1 core owns the invariants; the record re-types.
        with pytest.raises(cw.CascadeWireError, match="invalid leaderboard trial stats"):
            _lbt(n_trials=2, n_finite=5)

    def test_to_stats_round_trips_variance(self) -> None:
        from brutex.engine.trial_stats import LeaderboardTrialStats

        stats = _lbt().to_stats()
        assert isinstance(stats, LeaderboardTrialStats)
        assert stats.n_trials == 10
        assert stats.variance >= 0.0


def _swept_tree(root: Path) -> cw.SweepGridConfig:  # type: ignore[name-defined]
    """Run a tiny REAL grid sweep so the tree carries stores + sidecars."""
    from brutex.data.option_dedup import OptionDedupIndex
    from brutex.engine.sweep_orchestrator import (
        SweepGridConfig,
        Timeframe,
        make_store_for,
        run_sweep_grid,
    )
    from brutex.engine.sweep_run import ComboDedup
    from brutex.orchestrator.audit import AuditWriter
    from brutex.strategies.types import Side

    grid = SweepGridConfig(
        instruments=["NIFTY"],
        timeframes=[Timeframe("1minute", 1)],
        directions=[Side.BUY, Side.SELL],
        k_values=[1],
        condition_bits=[0, 1],
        stop_losses=[80],
        take_profits=[50],
        proximities=[None],
        mode="A",
        data_snapshot_hash="snap0001",
        indicator_version="ind-v1",
        code_commit_sha="deadbeef",
        min_hits=0,  # tiny bars: keep every backtested combo in the population
    )
    with AuditWriter.open(root / "audit.jsonl") as audit:
        run_sweep_grid(
            grid,
            bars_for=lambda _i, _t: _session_bars(),
            store_for=make_store_for(root, grid),
            dedup=ComboDedup(OptionDedupIndex(capacity=1_000, fpr_target=1e-6)),
            audit=audit,
        )
    return grid


def _two_anchor_grid() -> cw.SweepGridConfig:  # type: ignore[name-defined]
    """A minimal grid sweeping BOTH prev-day AND rolling-weekly anchors.

    One instrument/tf/direction/K/sl/tp/proximity + two ``fib_anchors`` → exactly
    two grid tuples, one per anchor, differing ONLY in their swept anchor.
    """
    from brutex.engine.sweep_orchestrator import SweepGridConfig, Timeframe
    from brutex.strategies.types import Side

    return SweepGridConfig(
        instruments=["NIFTY"],
        timeframes=[Timeframe("1minute", 1)],
        directions=[Side.BUY],
        k_values=[1],
        condition_bits=[0, 1],
        stop_losses=[80],
        take_profits=[50],
        proximities=[None],
        fib_anchors=[FibAnchorKind.PREV_DAY, FibAnchorKind.ROLLING_WEEKLY],
        mode="A",
        data_snapshot_hash="snap0001",
        indicator_version="ind-v1",
        code_commit_sha="deadbeef",
    )


def _seed_tuple_store(
    root: Path,
    tuple_: SweepTuple,
    grid: cw.SweepGridConfig,  # type: ignore[name-defined]
    *,
    rows: Sequence[ResultRow],
) -> None:
    """Persist *rows* at *tuple_*'s deterministic store path under *root*."""
    from brutex.engine.results_store import ResultsStore
    from brutex.engine.sweep_orchestrator import tuple_store_path

    store = ResultsStore(path=tuple_store_path(root, tuple_, grid))
    for row in rows:
        store.append(row)
    store.flush()


class TestBuildStage2CascadeSpecs:
    """DEC-NIFTY-FIB-AXIS-001 review MAJOR 2 — the pragma-free Stage-2 helper.

    The Stage-2 grid-winner cascade assembly used to live inline in the pragma'd
    ``cmd_sweep_all`` shim, so the missing ``fib_anchor`` thread was invisible.
    These tests exercise the factored helper directly.
    """

    def test_each_grid_winner_spec_carries_its_swept_fib_anchor(self, tmp_path: Path) -> None:
        # Two grid tuples (prev-day + rolling-weekly), each with a persisted
        # winner. Each Stage-2 spec must carry its OWN anchor (fails-before:
        # without the ``fib_anchor=sweep_tuple.fib_anchor`` thread the
        # rolling-weekly tuple's spec would default PREV_DAY, mis-scoring it in
        # Stage-4). Mirrors the anchor-sensitivity of
        # ``test_candidates_for_spec_uses_the_swept_fib_anchor``.
        from brutex.engine.sweep_orchestrator import iter_sweep_tuples

        grid = _two_anchor_grid()
        for tuple_ in iter_sweep_tuples(grid):
            _seed_tuple_store(tmp_path, tuple_, grid, rows=[_result_row(1, mask=1, sharpe=0.5)])

        specs = cw.build_stage2_cascade_specs(tmp_path, grid, top_k=None)
        assert {spec.fib_anchor for spec in specs} == {
            FibAnchorKind.PREV_DAY,
            FibAnchorKind.ROLLING_WEEKLY,
        }
        assert all(spec.instrument == "NIFTY" and spec.tf_label == "1minute" for spec in specs)

    def test_skips_tuples_whose_store_is_absent(self, tmp_path: Path) -> None:
        # Only the prev-day tuple has a store; the rolling-weekly tuple never ran
        # (no bars) → the helper's ``not store_path.exists()`` branch skips it.
        from brutex.engine.sweep_orchestrator import iter_sweep_tuples

        grid = _two_anchor_grid()
        prev_day = next(
            t for t in iter_sweep_tuples(grid) if t.fib_anchor is FibAnchorKind.PREV_DAY
        )
        _seed_tuple_store(tmp_path, prev_day, grid, rows=[_result_row(1, mask=1, sharpe=0.5)])

        specs = cw.build_stage2_cascade_specs(tmp_path, grid, top_k=None)
        assert {spec.fib_anchor for spec in specs} == {FibAnchorKind.PREV_DAY}

    def test_empty_store_yields_no_spec(self, tmp_path: Path) -> None:
        # A store that exists but holds ZERO rows → ``spec_from_store_rows``
        # returns ``None`` (the ``if spec is not None`` branch drops it).
        from brutex.engine.sweep_orchestrator import iter_sweep_tuples

        grid = _two_anchor_grid()
        for tuple_ in iter_sweep_tuples(grid):
            _seed_tuple_store(tmp_path, tuple_, grid, rows=[])

        assert cw.build_stage2_cascade_specs(tmp_path, grid, top_k=None) == []

    def test_top_k_is_threaded_into_the_spec_winners(self, tmp_path: Path) -> None:
        from brutex.engine.sweep_orchestrator import iter_sweep_tuples

        grid = _two_anchor_grid()
        rows = [
            _result_row(1, mask=1, sharpe=0.5),
            _result_row(2, mask=2, sharpe=1.5),
        ]
        for tuple_ in iter_sweep_tuples(grid):
            _seed_tuple_store(tmp_path, tuple_, grid, rows=rows)

        specs = cw.build_stage2_cascade_specs(tmp_path, grid, top_k=1)
        assert specs
        assert all(len(spec.winners) == 1 for spec in specs)

    def test_corrupt_store_raises_typed_cascade_error(self, tmp_path: Path) -> None:
        """DB-02 (DEC-SWEEP-STORE-CORRUPT-CONTAIN-001, MIGRATION.md #1921).

        A corrupt / old-schema Stage-2 finalist store surfaces as a typed
        ``CascadeWireError`` naming the UNREADABLE file — never a raw error and
        never a silent skip (dropping a tuple's finalists would change Stage-2
        selection invisibly, a false green).
        """
        pytest.importorskip("polars")
        from brutex.engine.sweep_orchestrator import (
            TUPLE_STORE_CORRUPT_REMEDIATION,
            iter_sweep_tuples,
            tuple_store_path,
        )

        grid = _two_anchor_grid()
        first = next(iter_sweep_tuples(grid))
        _seed_tuple_store(tmp_path, first, grid, rows=[_result_row(1, mask=1, sharpe=0.5)])
        # Corrupt the persisted store parquet (VECTOR: overwrite *.parquet).
        store_path = tuple_store_path(tmp_path, first, grid)
        store_path.write_bytes(b"CORRUPT\x00\x01" * 40)
        with pytest.raises(cw.CascadeWireError, match="UNREADABLE") as excinfo:
            cw.build_stage2_cascade_specs(tmp_path, grid, top_k=None)
        message = str(excinfo.value)
        assert str(store_path) in message
        # Review round 1 MINOR-1: the message carries the ONE shared RUNNABLE
        # remediation chain (real flags — TestRemediationFlagsRunnable pins them
        # against the live argparse surface), never the fictional --out-root.
        assert TUPLE_STORE_CORRUPT_REMEDIATION in message
        assert "--results-root" in message
        assert "--out-root" not in message


@dataclass(frozen=True)
class _FakeRefinedCell:
    """A minimal structural stand-in for ``stage3_wire.Stage3RefinedCell``."""

    tuple_: SweepTuple
    path: Path


class TestBuildLeaderboardTrials:
    def test_aggregates_every_tuple_sidecar_per_leaderboard(self, tmp_path: Path) -> None:
        from brutex.engine.sweep_orchestrator import iter_sweep_tuples, tuple_store_path
        from brutex.engine.trial_stats import read_tuple_trial_stats, trial_stats_sidecar_path

        grid = _swept_tree(tmp_path)
        records = cw.build_leaderboard_trials(tmp_path, grid)
        assert len(records) == 1  # one (instrument, tf) leaderboard, both directions
        record = records[0]
        assert (record.instrument, record.tf_label) == ("NIFTY", "1minute")
        assert record.complete is True
        expected_n = sum(
            read_tuple_trial_stats(
                trial_stats_sidecar_path(tuple_store_path(tmp_path, t, grid))
            ).n_backtested
            for t in iter_sweep_tuples(grid)
        )
        assert record.n_trials == expected_n
        assert expected_n > 0
        assert record.to_stats().variance >= 0.0

    def test_missing_sidecar_marks_the_leaderboard_incomplete(self, tmp_path: Path) -> None:
        from brutex.engine.sweep_orchestrator import iter_sweep_tuples, tuple_store_path
        from brutex.engine.trial_stats import trial_stats_sidecar_path

        grid = _swept_tree(tmp_path)
        first = next(iter_sweep_tuples(grid))
        trial_stats_sidecar_path(tuple_store_path(tmp_path, first, grid)).unlink()
        (record,) = cw.build_leaderboard_trials(tmp_path, grid)
        assert record.complete is False

    def test_unreadable_sidecar_fails_loudly(self, tmp_path: Path) -> None:
        from brutex.engine.sweep_orchestrator import iter_sweep_tuples, tuple_store_path
        from brutex.engine.trial_stats import trial_stats_sidecar_path

        grid = _swept_tree(tmp_path)
        first = next(iter_sweep_tuples(grid))
        sidecar = trial_stats_sidecar_path(tuple_store_path(tmp_path, first, grid))
        sidecar.write_text("{corrupt")
        with pytest.raises(cw.CascadeWireError, match="unreadable trial-stats sidecar"):
            cw.build_leaderboard_trials(tmp_path, grid)

    def test_digest_mismatch_fails_loudly(self, tmp_path: Path) -> None:
        from brutex.engine.sweep_orchestrator import iter_sweep_tuples, tuple_store_path
        from brutex.engine.trial_stats import (
            read_tuple_trial_stats,
            trial_stats_sidecar_path,
            write_tuple_trial_stats,
        )

        grid = _swept_tree(tmp_path)
        first = next(iter_sweep_tuples(grid))
        store_path = tuple_store_path(tmp_path, first, grid)
        sidecar = trial_stats_sidecar_path(store_path)
        stats = read_tuple_trial_stats(sidecar)
        from dataclasses import replace

        write_tuple_trial_stats(sidecar, replace(stats, tuple_digest="0" * 16))
        with pytest.raises(cw.CascadeWireError, match="pairing mismatch.*digest"):
            cw.build_leaderboard_trials(tmp_path, grid)

    def test_store_content_drift_fails_loudly(self, tmp_path: Path) -> None:
        from brutex.engine.sweep_orchestrator import iter_sweep_tuples, tuple_store_path

        grid = _swept_tree(tmp_path)
        first = next(iter_sweep_tuples(grid))
        store_path = tuple_store_path(tmp_path, first, grid)
        store_path.write_bytes(store_path.read_bytes() + b"tampered")
        with pytest.raises(cw.CascadeWireError, match="pairing mismatch.*store"):
            cw.build_leaderboard_trials(tmp_path, grid)

    def test_absent_store_contributes_nothing(self, tmp_path: Path) -> None:
        from brutex.engine.sweep_orchestrator import SweepGridConfig, Timeframe
        from brutex.strategies.types import Side

        grid = SweepGridConfig(
            instruments=["NIFTY"],
            timeframes=[Timeframe("1minute", 1)],
            directions=[Side.BUY],
            k_values=[1],
            condition_bits=[0],
            stop_losses=[80],
            take_profits=[50],
            proximities=[None],
            mode="A",
            data_snapshot_hash="snap0001",
            indicator_version="ind-v1",
            code_commit_sha="deadbeef",
        )
        assert cw.build_leaderboard_trials(tmp_path, grid) == ()

    def test_cells_without_grid_context_rejected(self, tmp_path: Path) -> None:
        from brutex.engine.sweep_orchestrator import Timeframe
        from brutex.strategies.types import Side

        grid = _swept_tree(tmp_path)
        cell = _FakeRefinedCell(
            tuple_=SweepTuple(
                instrument="NIFTY",
                tf=Timeframe("1minute", 1),
                direction=Side.BUY,
                k=1,
                sl_paisa=85,
                tp_paisa=60,
                proximity=None,
            ),
            path=tmp_path / "x.parquet",
        )
        with pytest.raises(cw.CascadeWireError, match="without stage3_grid"):
            cw.build_leaderboard_trials(
                tmp_path,
                grid,
                stage3_cells=[cell],
            )

    def test_stage3_cells_join_the_leaderboard(self, tmp_path: Path) -> None:
        import dataclasses

        from brutex.engine.results_store import ResultsStore
        from brutex.engine.sweep_orchestrator import (
            SweepTuple,
            Timeframe,
            _tuple_digest,
            tuple_store_path,
        )
        from brutex.engine.trial_stats import (
            trial_stats_sidecar_path,
            tuple_trial_stats_from_sharpes,
            write_tuple_trial_stats,
        )
        from brutex.strategies.types import Side

        grid = _swept_tree(tmp_path)
        baseline = cw.build_leaderboard_trials(tmp_path, grid)[0]

        train_grid = dataclasses.replace(grid, data_snapshot_hash="ff" * 32)
        cell_tuple = SweepTuple(
            instrument="NIFTY",
            tf=Timeframe("1minute", 1),
            direction=Side.BUY,
            k=1,
            sl_paisa=85,
            tp_paisa=60,
            proximity=None,
        )
        cell_path = tuple_store_path(tmp_path, cell_tuple, train_grid)
        store = ResultsStore(path=cell_path)
        flush = store.flush()  # an empty refined-cell store is a valid pairing target
        write_tuple_trial_stats(
            trial_stats_sidecar_path(cell_path),
            tuple_trial_stats_from_sharpes(
                [0.5, None, 1.25],
                n_enumerated=3,
                n_conflict_pruned=0,
                n_minhits_dropped=0,
                tuple_digest=_tuple_digest(cell_tuple, train_grid),
                store_content_hash=flush.content_hash,
            ),
        )
        cell = _FakeRefinedCell(tuple_=cell_tuple, path=cell_path)
        (record,) = cw.build_leaderboard_trials(
            tmp_path,
            grid,
            stage3_cells=[cell],
            stage3_grid=train_grid,
        )
        assert record.n_trials == baseline.n_trials + 3
        assert record.n_finite == baseline.n_finite + 2
        assert record.complete is True

        # And an absent CELL sidecar flips the leaderboard incomplete.
        trial_stats_sidecar_path(cell_path).unlink()
        (record2,) = cw.build_leaderboard_trials(
            tmp_path,
            grid,
            stage3_cells=[cell],
            stage3_grid=train_grid,
        )
        assert record2.complete is False
        assert record2.n_trials == baseline.n_trials


class TestLeaderboardTrialsManifest:
    def test_round_trip_preserves_records(self) -> None:
        manifest = _manifest()
        with_lbt = cw.CascadeManifest(
            version=manifest.version,
            sweep_id=manifest.sweep_id,
            cascade_config=manifest.cascade_config,
            bars_context=manifest.bars_context,
            specs=manifest.specs,
            leaderboard_trials=(_lbt(), _lbt(instrument="BANKNIFTY", complete=False)),
        )
        restored = cw.manifest_from_json(cw.manifest_to_json(with_lbt))
        assert restored == with_lbt

    def test_absent_key_parses_to_none(self) -> None:
        import json

        payload = json.loads(cw.manifest_to_json(_manifest()))
        del payload["leaderboard_trials"]
        restored = cw.manifest_from_json(json.dumps(payload).encode("utf-8"))
        assert restored.leaderboard_trials is None

    def test_bad_record_raises(self) -> None:
        import json

        manifest = _manifest()
        payload = json.loads(
            cw.manifest_to_json(
                cw.CascadeManifest(
                    version=manifest.version,
                    sweep_id=manifest.sweep_id,
                    cascade_config=manifest.cascade_config,
                    bars_context=manifest.bars_context,
                    specs=manifest.specs,
                    leaderboard_trials=(_lbt(),),
                )
            )
        )
        del payload["leaderboard_trials"][0]["n_trials"]
        with pytest.raises(cw.CascadeWireError, match="invalid leaderboard_trials record"):
            cw.manifest_from_json(json.dumps(payload).encode("utf-8"))

    def test_auto_path_records_the_freeze(self, tmp_path: Path) -> None:
        records = (_lbt(),)
        cw.run_and_write_from_specs(
            tmp_path,
            sweep_id="a" * 64,
            specs=[_spec()],
            bars_for=_bars_for(_session_bars()),
            bars_context=cw.BarsContext(
                exchange="NSE", segment="INDEX", start="2021-01-01", end="2021-02-01"
            ),
            cascade_config=cw.cascade_config_for_sweep("a" * 64),
            leaderboard_trials=records,
        )
        assert cw.load_manifest(tmp_path).leaderboard_trials == records


# ── Frozen-N consumption by filter #2 (DEC-TRIAL-STATS-CONSUME-001) ──────────


class TestDsrTrialStatsConsumption:
    """The slice-4 wiring: the cascade deflates against the frozen leaderboard N.

    ADR-0008 condition 6 (all-or-fallback): a group's record must be PRESENT,
    COMPLETE and DEFLATABLE to be consumed; anything less falls back to pool-N
    with a LOUD ``cascade_wire.dsr_pool_fallback`` warning and a ``"pool"``
    stamp — a knowingly partial N is never deflated against.
    """

    def _run(self, records: tuple[cw.LeaderboardTrialsRecord, ...] | None) -> cw.CascadeWireReport:
        return cw.run_cascade_over_specs(
            [_spec()],
            bars_for=_bars_for(_session_bars()),
            config=cw.cascade_config_for_sweep("a" * 64),
            sweep_id="a" * 64,
            leaderboard_trials=records,
        )

    @staticmethod
    def _fallbacks(
        logs: list[MutableMapping[str, Any]],
    ) -> list[MutableMapping[str, Any]]:
        return [e for e in logs if e["event"] == "cascade_wire.dsr_pool_fallback"]

    def test_no_records_stamps_pool_silently(self) -> None:
        # Legacy launchers pass nothing: the pool fallback is the EXPECTED
        # state, so no warning fires (there is nothing to fall back FROM).
        with capture_logs() as logs:
            report = self._run(None)
        (_, _, group) = report.groups[0]
        assert group.dsr_n_source == "pool"
        assert group.dsr_n_trials == 1
        assert self._fallbacks(logs) == []

    def test_complete_record_stamps_leaderboard(self) -> None:
        record = _lbt()
        with capture_logs() as logs:
            report = self._run((record,))
        (_, _, group) = report.groups[0]
        assert group.dsr_n_source == "leaderboard"
        assert group.dsr_n_trials == record.n_trials
        assert group.dsr_trial_variance == record.to_stats().variance
        assert self._fallbacks(logs) == []

    def test_missing_slice_record_falls_back_loudly(self) -> None:
        # Records were supplied but none covers THIS slice → loud pool fallback.
        with capture_logs() as logs:
            report = self._run((_lbt(instrument="BANKNIFTY"),))
        (_, _, group) = report.groups[0]
        assert group.dsr_n_source == "pool"
        (event,) = self._fallbacks(logs)
        assert event["reason"] == "no frozen leaderboard record for this slice"

    def test_incomplete_record_falls_back_loudly(self) -> None:
        with capture_logs() as logs:
            report = self._run((_lbt(complete=False),))
        (_, _, group) = report.groups[0]
        assert group.dsr_n_source == "pool"
        (event,) = self._fallbacks(logs)
        assert event["reason"] == "incomplete"

    def test_undeflatable_record_falls_back_loudly(self) -> None:
        # Zero finite Sharpes → no dispersion → the Bailey-LdP expected-max
        # benchmark is undefined; deflating would fabricate significance.
        record = _lbt(n_finite=0, sum_sharpe=0.0, sum_sharpe_sq=0.0)
        with capture_logs() as logs:
            report = self._run((record,))
        (_, _, group) = report.groups[0]
        assert group.dsr_n_source == "pool"
        (event,) = self._fallbacks(logs)
        assert event["reason"] == "undeflatable"

    def test_auto_equals_manual_with_frozen_records(self, tmp_path: Path) -> None:
        # Condition 1: the manifest freeze IS the replay input — the MANUAL
        # path re-reads the frozen records (never the sidecars) and reproduces
        # the AUTOMATIC report byte-for-byte, leaderboard stamp included.
        bars_for = _bars_for(_session_bars())
        auto = cw.run_and_write_from_specs(
            tmp_path,
            sweep_id="a" * 64,
            specs=[_spec()],
            bars_for=bars_for,
            bars_context=cw.BarsContext(
                exchange="NSE", segment="INDEX", start="2021-01-01", end="2021-02-01"
            ),
            cascade_config=cw.cascade_config_for_sweep("a" * 64),
            leaderboard_trials=(_lbt(),),
        )
        auto_bytes = cw.report_path(tmp_path).read_bytes()
        assert b'"dsr_n_source":"leaderboard"' in auto_bytes
        manual = cw.run_and_write_from_manifest(tmp_path, bars_for=bars_for)
        assert cw.report_path(tmp_path).read_bytes() == auto_bytes
        assert cw.serialize_wire_report(manual) == cw.serialize_wire_report(auto)


# ── Spec building from persisted rows ─────────────────────────────────────────


def _result_row(seed: int, *, mask: int, sharpe: float) -> ResultRow:
    return ResultRow(
        run_id=_rid(seed),
        instrument="NIFTY",
        tf="1minute",
        direction="bull",
        mode="A",
        mask_bits=mask,
        net_pnl_paisa=1_000,
        gross_profit_paisa=1_500,
        gross_loss_paisa=500,
        max_drawdown_paisa=250,
        expectancy_paisa=25,
        total_trades=40,
        winning_trades=25,
        losing_trades=15,
        sharpe_ratio=sharpe,
        sortino_ratio=2.0,
        calmar_ratio=1.25,
        profit_factor=1.8,
        win_rate=0.625,
        max_drawdown_pct=12.5,
        avg_entry_price=Decimal("14020.50"),
        avg_exit_price=Decimal("14100.25"),
    )


class TestSpecFromStoreRows:
    def test_empty_rows_returns_none(self) -> None:
        assert (
            cw.spec_from_store_rows(
                instrument="NIFTY",
                tf_label="1minute",
                tf_minutes=1,
                direction_label="bull",
                sl_paisa=None,
                tp_paisa=None,
                prior_n=3,
                proximity_bps=None,
                start_equity_paisa=10_000_000,
                rows=[],
            )
            is None
        )

    def test_selects_all_when_top_k_none(self) -> None:
        rows = [_result_row(1, mask=1, sharpe=0.5), _result_row(2, mask=2, sharpe=1.5)]
        spec = cw.spec_from_store_rows(
            instrument="NIFTY",
            tf_label="1minute",
            tf_minutes=1,
            direction_label="bull",
            sl_paisa=None,
            tp_paisa=None,
            prior_n=3,
            proximity_bps=None,
            start_equity_paisa=10_000_000,
            rows=rows,
        )
        assert spec is not None
        # Sorted by Sharpe descending — the 1.5 winner ranks first.
        assert spec.winners == ((_rid(2), 2), (_rid(1), 1))

    def test_top_k_truncates_by_sharpe(self) -> None:
        rows = [
            _result_row(1, mask=1, sharpe=0.5),
            _result_row(2, mask=2, sharpe=1.5),
            _result_row(3, mask=3, sharpe=1.0),
        ]
        spec = cw.spec_from_store_rows(
            instrument="NIFTY",
            tf_label="1minute",
            tf_minutes=1,
            direction_label="bull",
            sl_paisa=None,
            tp_paisa=None,
            prior_n=3,
            proximity_bps=None,
            start_equity_paisa=10_000_000,
            rows=rows,
            top_k=2,
        )
        assert spec is not None
        assert spec.winners == ((_rid(2), 2), (_rid(3), 3))

    def test_defaults_to_prev_day_fib_anchor(self) -> None:
        spec = cw.spec_from_store_rows(
            instrument="NIFTY",
            tf_label="1minute",
            tf_minutes=1,
            direction_label="bull",
            sl_paisa=None,
            tp_paisa=None,
            prior_n=3,
            proximity_bps=None,
            start_equity_paisa=10_000_000,
            rows=[_result_row(1, mask=1, sharpe=0.5)],
        )
        assert spec is not None
        assert spec.fib_anchor is FibAnchorKind.PREV_DAY

    def test_threads_the_swept_fib_anchor(self) -> None:
        # DEC-NIFTY-FIB-AXIS-001: the swept anchor is carried onto the spec so the
        # Stage-4 cascade re-scores against the tuple's OWN Fib levels.
        spec = cw.spec_from_store_rows(
            instrument="NIFTY",
            tf_label="1minute",
            tf_minutes=1,
            direction_label="bull",
            sl_paisa=None,
            tp_paisa=None,
            prior_n=3,
            proximity_bps=None,
            start_equity_paisa=10_000_000,
            rows=[_result_row(1, mask=1, sharpe=0.5)],
            fib_anchor=FibAnchorKind.ROLLING_WEEKLY,
        )
        assert spec is not None
        assert spec.fib_anchor is FibAnchorKind.ROLLING_WEEKLY


# ── Output paths + IO + the end-to-end paths ──────────────────────────────────


class TestEndToEnd:
    def test_paths(self, tmp_path: Path) -> None:
        assert cw.manifest_path(tmp_path) == tmp_path / "cascade" / "manifest.json"
        assert cw.report_path(tmp_path) == tmp_path / "cascade" / "cascade-report.json"

    def test_load_manifest_missing_raises(self, tmp_path: Path) -> None:
        with pytest.raises(cw.CascadeWireError, match="no cascade manifest"):
            cw.load_manifest(tmp_path)

    def test_write_then_load_manifest_round_trips(self, tmp_path: Path) -> None:
        manifest = _manifest()
        cw.write_manifest(tmp_path, manifest)
        assert cw.load_manifest(tmp_path) == manifest

    def test_write_report(self, tmp_path: Path) -> None:
        path = cw.write_report(tmp_path, _manual_report())
        assert path.exists()
        assert path.read_bytes() == cw.serialize_wire_report(_manual_report())


class TestCascadeSidecarAtomicWrite:
    """R2N-08 (DEC-CASCADE-SIDECAR-ATOMIC-001, MIGRATION.md #1923).

    The manifest / report writes are atomic (tmp→fsync→os.replace); a crash /
    ENOSPC mid-write leaves the prior complete copy INTACT (never torn), maps to a
    typed ``CascadeWireError`` (NOT the lake-wide ``LakeWriteHalt`` escalation), and
    leaves no orphan ``.brutex-write-*`` temp.
    """

    def test_manifest_write_atomic_enospc_preserves_prior(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import errno

        from brutex.data.errors import LakeWriteHalt

        # Pre-seed a good manifest.
        good = _manifest()
        cw.write_manifest(tmp_path, good)
        prior_bytes = cw.manifest_path(tmp_path).read_bytes()

        def _enospc(_src: str, _dst: str) -> None:
            raise OSError(errno.ENOSPC, "No space left on device")

        monkeypatch.setattr("brutex.data.atomic_io.os.replace", _enospc)
        # A DIFFERENT manifest write now fails mid-replace.
        with pytest.raises(cw.CascadeWireError) as excinfo:
            cw.write_manifest(tmp_path, _manifest(specs=(_spec(),)))
        # Typed cascade abort — NOT a lake-wide halt escalation.
        assert not isinstance(excinfo.value, LakeWriteHalt)
        assert str(cw.manifest_path(tmp_path)) in str(excinfo.value)
        # The prior manifest bytes are UNCHANGED (the atomic replace never landed).
        monkeypatch.undo()
        assert cw.manifest_path(tmp_path).read_bytes() == prior_bytes
        # No orphan .brutex-write-* temp left behind.
        assert list(cw.manifest_path(tmp_path).parent.glob(".brutex-write-*")) == []

    def test_report_write_atomic_enospc_typed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import errno

        from brutex.data.errors import LakeWriteHalt

        def _enospc(_src: str, _dst: str) -> None:
            raise OSError(errno.ENOSPC, "No space left on device")

        monkeypatch.setattr("brutex.data.atomic_io.os.replace", _enospc)
        with pytest.raises(cw.CascadeWireError, match="cascade report") as excinfo:
            cw.write_report(tmp_path, _manual_report())
        assert not isinstance(excinfo.value, LakeWriteHalt)
        monkeypatch.undo()
        assert list((tmp_path / cw.CASCADE_DIRNAME).glob(".brutex-write-*")) == []

    def test_kill_mid_write_load_manifest_returns_old(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import errno

        good = _manifest()
        cw.write_manifest(tmp_path, good)

        def _enospc(_src: str, _dst: str) -> None:
            raise OSError(errno.ENOSPC, "No space left on device")

        monkeypatch.setattr("brutex.data.atomic_io.os.replace", _enospc)
        with pytest.raises(cw.CascadeWireError):
            cw.write_manifest(tmp_path, _manifest(specs=(_spec(),)))
        monkeypatch.undo()
        # load_manifest still serves the OLD complete cascade.
        assert cw.load_manifest(tmp_path) == good

    def test_auto_path_writes_manifest_and_report(self, tmp_path: Path) -> None:
        bars = _session_bars()
        report = cw.run_and_write_from_specs(
            tmp_path,
            sweep_id="a" * 64,
            specs=[_spec()],
            bars_for=_bars_for(bars),
            bars_context=cw.BarsContext(
                exchange="NSE", segment="INDEX", start="2021-01-01", end="2021-02-01"
            ),
            cascade_config=cw.cascade_config_for_sweep("a" * 64),
        )
        assert cw.manifest_path(tmp_path).exists()
        assert cw.report_path(tmp_path).exists()
        assert report.sweep_id == "a" * 64

    def test_auto_equals_manual_byte_for_byte(self, tmp_path: Path) -> None:
        """The golden contract: AUTOMATIC report bytes == MANUAL replay bytes."""
        bars = _session_bars()
        bars_for = _bars_for(bars)
        specs = [_spec(), _spec(winners=((_rid(2), 0),), direction_label="bear")]
        auto = cw.run_and_write_from_specs(
            tmp_path,
            sweep_id="a" * 64,
            specs=specs,
            bars_for=bars_for,
            bars_context=cw.BarsContext(
                exchange="NSE", segment="INDEX", start="2021-01-01", end="2021-02-01"
            ),
            cascade_config=cw.cascade_config_for_sweep("a" * 64),
        )
        auto_bytes = cw.report_path(tmp_path).read_bytes()

        manual = cw.run_and_write_from_manifest(tmp_path, bars_for=bars_for)
        manual_bytes = cw.report_path(tmp_path).read_bytes()

        assert manual_bytes == auto_bytes
        assert cw.serialize_wire_report(manual) == cw.serialize_wire_report(auto)


class TestStage3CellRelPath:
    """DEC-ROW-PROVENANCE-001: the additive cell-path attribution field."""

    def test_round_trip_preserves_rel_path(self) -> None:
        record = cw.Stage3ManifestRecord(
            enabled=True,
            train_snapshot_hash="ab" * 32,
            refined_cells=(
                _stage3_cell_with(rel_path="NIFTY/1minute/bull_k1_" + "ab" * 8 + ".parquet"),
            ),
        )
        manifest = _manifest()
        with_cells = cw.CascadeManifest(
            version=manifest.version,
            sweep_id=manifest.sweep_id,
            cascade_config=manifest.cascade_config,
            bars_context=manifest.bars_context,
            specs=manifest.specs,
            stage3=record,
        )
        assert cw.manifest_from_json(cw.manifest_to_json(with_cells)) == with_cells

    def test_absent_key_parses_to_none(self) -> None:
        # Pre-DEC-ROW-PROVENANCE-001 manifests carry no rel_path key at all.
        import json

        manifest = cw.CascadeManifest(
            version=_manifest().version,
            sweep_id=_manifest().sweep_id,
            cascade_config=_manifest().cascade_config,
            bars_context=_manifest().bars_context,
            specs=_manifest().specs,
            stage3=cw.Stage3ManifestRecord(
                enabled=True,
                train_snapshot_hash="ab" * 32,
                refined_cells=(_stage3_cell_with(rel_path=None),),
            ),
        )
        payload = json.loads(cw.manifest_to_json(manifest))
        del payload["stage3"]["refined_cells"][0]["rel_path"]
        restored = cw.manifest_from_json(json.dumps(payload).encode("utf-8"))
        assert restored.stage3 is not None
        assert restored.stage3.refined_cells[0].rel_path is None

    @pytest.mark.parametrize("bad", ["", "/abs/cell.parquet"])
    def test_bad_rel_path_raises(self, bad: str) -> None:
        with pytest.raises(cw.CascadeWireError, match="rel_path"):
            _stage3_cell_with(rel_path=bad)


# ── _manifest_window_dt (per-module 100% gate; DEC-VIX-STAMP-WIRE-001) ────────


class TestManifestWindowDt:
    """The ONE manifest window-endpoint parse for the manual replay's slice reads.

    The seam's behavioural suite also pins this in
    ``tests/unit/test_engine_vix_stamper_wiring.py``; these mirrors keep THIS
    module's per-file 100% gate honest on its OWN discovered covering suite
    (``scripts/ci/coverage_gate_discovery.py`` maps ``cascade_wire.py`` to the
    ``test_*cascade_wire*`` files only).
    """

    def test_naive_endpoint_is_utc(self) -> None:
        parsed = cw._manifest_window_dt("2026-01-05")
        assert parsed == datetime(2026, 1, 5, tzinfo=UTC)

    def test_aware_endpoint_offset_preserved(self) -> None:
        parsed = cw._manifest_window_dt("2026-01-05T09:15:00+05:30")
        assert parsed.utcoffset() == timedelta(hours=5, minutes=30)


class TestCpcvRateSerialisation:
    """DEC-RESULTS-CASCADE-VERDICT-JOIN-001: the additive per-verdict rate key."""

    def test_cpcv_pass_rate_serialises_both_states(self) -> None:
        # Additive at REPORT_VERSION 1 (the pool_pbo/provisional precedent):
        # the RAW full-precision fraction on the measured verdict, JSON null
        # on the never-measured one — and the version does NOT bump.
        payload = cw.wire_report_to_dict(_manual_report())
        assert payload["version"] == cw.REPORT_VERSION
        verdicts = payload["groups"][0]["report"]["verdicts"]  # type: ignore[index]
        assert verdicts[0]["cpcv_pass_rate"] == 0.875
        assert verdicts[1]["cpcv_pass_rate"] is None
        raw = cw.serialize_wire_report(_manual_report())
        assert b'"cpcv_pass_rate":0.875' in raw
        assert b'"cpcv_pass_rate":null' in raw


class TestWriteReportAtomic:
    """DEC-RESULTS-CASCADE-VERDICT-JOIN-001: write_report is tmp→fsync→replace."""

    def test_bytes_identical_and_no_temp_residue(self, tmp_path: Path) -> None:
        path = cw.write_report(tmp_path, _manual_report())
        assert path.read_bytes() == cw.serialize_wire_report(_manual_report())
        # The atomic_write_simple temp file never survives a clean write.
        residue = [p.name for p in path.parent.iterdir() if p.name != path.name]
        assert residue == []

    def test_lands_via_os_replace(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from brutex.data import atomic_io

        calls: list[tuple[str, str]] = []
        real_replace = atomic_io.os.replace

        def _spy(src: str | Path, dst: str | Path) -> None:
            calls.append((str(src), str(dst)))
            real_replace(src, dst)

        monkeypatch.setattr(atomic_io.os, "replace", _spy)
        path = cw.write_report(tmp_path, _manual_report())
        # Exactly ONE atomic rename, landing the final report path — a
        # concurrent /results read can never see a torn half-write.
        assert [dst for _, dst in calls] == [str(path)]

    def test_overwrite_replaces_previous_report(self, tmp_path: Path) -> None:
        path = cw.write_report(tmp_path, _manual_report())
        first = path.read_bytes()
        cw.write_report(tmp_path, _manual_report())
        assert path.read_bytes() == first  # deterministic bytes, atomically re-landed


class TestLbtToDictDelegate:
    """Direct pin on the ``_lbt_to_dict`` crate delegate.

    ``manifest_to_json`` serialises lbt records crate-side, so the retained
    standalone delegate is otherwise reached only by the golden/property
    suites — the whole-engine ``--cov=brutex.engine`` unit gate needs this
    direct exercise (the ``_float_stats`` retained-delegate precedent).
    """

    def test_round_trips_through_the_parse_delegate(self) -> None:
        record = _lbt()
        payload = cw._lbt_to_dict(record)
        assert payload["instrument"] == "NIFTY"
        assert payload["n_trials"] == 10
        assert cw._lbt_from_dict(dict(payload)) == record
