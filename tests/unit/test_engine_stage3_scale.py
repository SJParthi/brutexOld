"""Unit battery for the instrument-scaled Stage-3 SL/TP axes (100% line+branch).

DEC-STAGE3-SLTP-INSTRUMENT-SCALE-001 (LANE W3). Covers: the exact
median-daily-range law (IST session grouping, lower median, exact 2-dp
paisa ints, degenerate refusals), the scaled-axes derivation (exact ceil
law, the STRUCTURAL 40×50 shape invariant, the Mode-A identity anchor at
r=400 paisa), the default-OMIT params fragment (the price_source
precedent — Mode-A ⇒ {}), the coarse log-spaced ladder cells (+ CSV
conduit), both fail-loud env knobs' truth tables, and the existing
run-identity law pin (scaled SL/TP values already re-key
``canonical_trade_params`` through ``sl_paisa``/``tp_paisa``).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from brutex.data.models import CandleBar
from brutex.engine.stage3_grid import MODE_A_SL_AXIS, MODE_A_TP_AXIS
from brutex.engine.stage3_scale import (
    COARSE_SCALE_LADDER,
    INSTRUMENT_SCALE_ENV,
    NIGHTLY_SCALE_ENV,
    coarse_sltp_cells,
    derive_scaled_axes,
    instrument_axis_params_fragment,
    is_mode_a_axes,
    median_daily_range_paisa,
    resolve_instrument_scale_env,
    resolve_nightly_scale_env,
    sltp_cell_csv,
)

_IST = timezone(timedelta(hours=5, minutes=30))


def _bar(day: int, minute: int, high: str, low: str) -> CandleBar:
    ts = (
        datetime(2026, 1, 5, 9, 16, tzinfo=_IST) + timedelta(days=day, minutes=minute)
    ).astimezone(UTC)
    return CandleBar(
        timestamp=ts,
        open=Decimal(low),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(high),
        volume=1,
        open_interest=None,
    )


# ── median_daily_range_paisa ─────────────────────────────────────────────────


class TestMedianDailyRange:
    def test_single_session_exact_paisa(self) -> None:
        bars = [_bar(0, 0, "101.50", "100.00"), _bar(0, 1, "102.25", "100.75")]
        # Session range = max(high) - min(low) = 102.25 - 100.00 = 2.25 → 225.
        assert median_daily_range_paisa(bars) == 225

    def test_lower_median_over_even_session_count(self) -> None:
        bars = [
            _bar(0, 0, "101.00", "100.00"),  # 100 paisa
            _bar(1, 0, "103.00", "100.00"),  # 300 paisa
        ]
        assert median_daily_range_paisa(bars) == 100  # LOWER median — never a float

    def test_odd_session_count_is_the_middle(self) -> None:
        bars = [
            _bar(0, 0, "101.00", "100.00"),  # 100
            _bar(1, 0, "105.00", "100.00"),  # 500
            _bar(2, 0, "103.00", "100.00"),  # 300
        ]
        assert median_daily_range_paisa(bars) == 300

    def test_sessions_grouped_by_ist_date(self) -> None:
        # Two bars 24h apart are DIFFERENT sessions even at the same wall time.
        bars = [_bar(0, 0, "101.00", "100.00"), _bar(1, 0, "101.00", "100.00")]
        assert median_daily_range_paisa(bars) == 100

    def test_intra_session_extremes_fold(self) -> None:
        bars = [
            _bar(0, 0, "101.00", "100.50"),
            _bar(0, 1, "104.00", "100.75"),  # session high
            _bar(0, 2, "103.00", "99.25"),  # session low
        ]
        assert median_daily_range_paisa(bars) == int((Decimal("104.00") - Decimal("99.25")) * 100)

    def test_empty_refused(self) -> None:
        with pytest.raises(ValueError, match="bars must be non-empty"):
            median_daily_range_paisa([])

    def test_flat_series_refused(self) -> None:
        bars = [_bar(0, 0, "100.00", "100.00"), _bar(1, 0, "100.00", "100.00")]
        with pytest.raises(ValueError, match="flat/degenerate"):
            median_daily_range_paisa(bars)


# ── derive_scaled_axes ───────────────────────────────────────────────────────


class TestDeriveScaledAxes:
    def test_mode_a_identity_anchor_r400(self) -> None:
        """r = 400 paisa reproduces the LITERAL Mode-A grid — the identity
        anchor (and the motivation in one number: Mode-A's ceiling models a
        4-rupee median daily range; SENSEX's is ~3 orders larger)."""
        sl_axis, tp_axis = derive_scaled_axes(400)
        assert sl_axis == MODE_A_SL_AXIS
        assert tp_axis == MODE_A_TP_AXIS
        assert is_mode_a_axes(sl_axis, tp_axis) is True

    def test_nifty_representative(self) -> None:
        # r = 15,000 paisa (150 index points): sl_tick = ceil(15000/80) = 188.
        sl_axis, tp_axis = derive_scaled_axes(15_000)
        assert (sl_axis.lo_paisa, sl_axis.hi_paisa, sl_axis.tick_paisa) == (188, 7_520, 188)
        assert (tp_axis.lo_paisa, tp_axis.hi_paisa, tp_axis.tick_paisa) == (375, 18_750, 375)

    def test_sensex_representative(self) -> None:
        # r = 90,000 paisa: the 200-paisa Mode-A ceiling (₹2, ~0.002%) becomes
        # a 45,000-paisa (₹450) ceiling — the motivating fix.
        sl_axis, tp_axis = derive_scaled_axes(90_000)
        assert (sl_axis.lo_paisa, sl_axis.hi_paisa, sl_axis.tick_paisa) == (1_125, 45_000, 1_125)
        assert (tp_axis.lo_paisa, tp_axis.hi_paisa, tp_axis.tick_paisa) == (2_250, 112_500, 2_250)

    def test_tiny_range_floors_at_one_paisa_tick(self) -> None:
        sl_axis, tp_axis = derive_scaled_axes(1)
        assert (sl_axis.lo_paisa, sl_axis.hi_paisa, sl_axis.tick_paisa) == (1, 40, 1)
        assert (tp_axis.lo_paisa, tp_axis.hi_paisa, tp_axis.tick_paisa) == (1, 50, 1)

    def test_shape_is_always_forty_by_fifty(self) -> None:
        for r in (1, 2, 399, 400, 401, 15_000, 90_000, 10**9):
            sl_axis, tp_axis = derive_scaled_axes(r)
            assert sl_axis.steps == 40
            assert tp_axis.steps == 50

    def test_custom_spans(self) -> None:
        sl_axis, tp_axis = derive_scaled_axes(8_000, sl_span=(1, 1), tp_span=(2, 1))
        assert sl_axis.tick_paisa == 200  # ceil(8000/40)
        assert tp_axis.tick_paisa == 320  # ceil(16000/50)

    def test_invalid_inputs_refused(self) -> None:
        with pytest.raises(ValueError, match="median_range_paisa"):
            derive_scaled_axes(0)
        with pytest.raises(ValueError, match="sl_span"):
            derive_scaled_axes(100, sl_span=(0, 1))
        with pytest.raises(ValueError, match="tp_span"):
            derive_scaled_axes(100, tp_span=(1, 0))


# ── The default-OMIT params fragment (identity law) ──────────────────────────


class TestInstrumentAxisParamsFragment:
    def test_mode_a_serialises_absent(self) -> None:
        """The load-bearing default-omit: a baseline run-identity is
        BYTE-IDENTICAL because the fragment contributes NOTHING."""
        assert instrument_axis_params_fragment(MODE_A_SL_AXIS, MODE_A_TP_AXIS) == {}
        sl_axis, tp_axis = derive_scaled_axes(400)  # the identity anchor
        assert instrument_axis_params_fragment(sl_axis, tp_axis) == {}

    def test_scaled_axes_emit_int_triples(self) -> None:
        sl_axis, tp_axis = derive_scaled_axes(15_000)
        fragment = instrument_axis_params_fragment(sl_axis, tp_axis)
        assert fragment == {
            "stage3_sl_axis": [188, 7_520, 188],
            "stage3_tp_axis": [375, 18_750, 375],
        }
        assert all(
            isinstance(part, int) for triple in fragment.values() for part in triple
        )  # JSON-canonical ints only — never a float near an identity hash

    def test_half_scaled_pair_still_emits(self) -> None:
        sl_axis, _ = derive_scaled_axes(15_000)
        fragment = instrument_axis_params_fragment(sl_axis, MODE_A_TP_AXIS)
        assert "stage3_sl_axis" in fragment and "stage3_tp_axis" in fragment

    def test_scaled_sl_tp_values_rekey_canonical_trade_params(self) -> None:
        """Item 7's run-identity law ALREADY holds through the existing
        ``sl_paisa``/``tp_paisa`` fields: distinct scaled values ⇒ distinct
        canonical params (⇒ distinct run_ids) with ZERO new keys — pinned."""
        from brutex.engine.sweep_run import canonical_trade_params

        baseline = canonical_trade_params(
            sl_paisa=MODE_A_SL_AXIS.lo_paisa,
            tp_paisa=MODE_A_TP_AXIS.lo_paisa,
            prior_n=3,
            proximity=None,
            start_equity_paisa=10_000_000,
        )
        sl_axis, tp_axis = derive_scaled_axes(90_000)
        scaled = canonical_trade_params(
            sl_paisa=sl_axis.lo_paisa,
            tp_paisa=tp_axis.lo_paisa,
            prior_n=3,
            proximity=None,
            start_equity_paisa=10_000_000,
        )
        assert scaled != baseline
        assert set(scaled.keys()) == set(baseline.keys())  # no new keys at default


# ── Coarse ladder cells + CSV conduit ────────────────────────────────────────


class TestCoarseCells:
    def test_ladder_is_five_log_spaced_fractions(self) -> None:
        assert COARSE_SCALE_LADDER == ((1, 4), (1, 2), (1, 1), (2, 1), (4, 1))

    def test_nifty_cells_exact(self) -> None:
        # base SL = ceil(r/4) at ×1; ladder ×¼..×4; TP = 2·SL.
        assert coarse_sltp_cells(15_000) == (
            (938, 1_876),
            (1_875, 3_750),
            (3_750, 7_500),
            (7_500, 15_000),
            (15_000, 30_000),
        )

    def test_tiny_range_dedupes_order_preserving(self) -> None:
        cells = coarse_sltp_cells(1)
        assert cells == ((1, 2),)  # all five rungs collapse — honestly "~5"

    def test_invalid_range_refused(self) -> None:
        with pytest.raises(ValueError, match="median_range_paisa"):
            coarse_sltp_cells(0)

    def test_csv_conduit_is_single_cell_single_value(self) -> None:
        # Refuter round 2: the renderer is deliberately PER-CELL — a
        # single-value CSV pair per invocation, never a flattened multi-value
        # pair (which the sweep grid would cross-multiply, 5 cells → 25).
        assert sltp_cell_csv((938, 1_876)) == ("938", "1876")
        assert sltp_cell_csv((15_000, 30_000)) == ("15000", "30000")

    def test_csv_refuses_junk(self) -> None:
        with pytest.raises(ValueError, match="positive paisa"):
            sltp_cell_csv((0, 10))
        with pytest.raises(ValueError, match="positive paisa"):
            sltp_cell_csv((10, 0))


# ── Env knobs (fail-loud truth tables) ───────────────────────────────────────


class TestEnvKnobs:
    @pytest.mark.parametrize("resolver", [resolve_instrument_scale_env, resolve_nightly_scale_env])
    def test_unset_blank_off(self, resolver: object, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(INSTRUMENT_SCALE_ENV, raising=False)
        monkeypatch.delenv(NIGHTLY_SCALE_ENV, raising=False)
        assert resolver() is False  # type: ignore[operator]
        assert resolver("") is False  # type: ignore[operator]

    @pytest.mark.parametrize("resolver", [resolve_instrument_scale_env, resolve_nightly_scale_env])
    @pytest.mark.parametrize(
        ("token", "expected"),
        [("1", True), ("true", True), ("TRUE", True), ("0", False), ("false", False)],
    )
    def test_token_vocabulary(self, resolver: object, token: str, expected: bool) -> None:
        assert resolver(token) is expected  # type: ignore[operator]

    def test_env_read_at_call_time(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(INSTRUMENT_SCALE_ENV, "1")
        assert resolve_instrument_scale_env() is True
        monkeypatch.setenv(NIGHTLY_SCALE_ENV, "true")
        assert resolve_nightly_scale_env() is True

    def test_junk_fails_loud_naming_the_knob(self) -> None:
        with pytest.raises(ValueError, match=INSTRUMENT_SCALE_ENV):
            resolve_instrument_scale_env("yes")
        with pytest.raises(ValueError, match=NIGHTLY_SCALE_ENV):
            resolve_nightly_scale_env("on")
