"""Unit tests for brutex.metrics.regime_taxonomy (DEC-REGIME-TAXONOMY-001).

Covers: boundary-exact VIX bands + the junk-input refusal arms, the full
session-lens trend-token mapping (junk degrades to UNKNOWN), the era
bisect at every encoded edge (incl. the pre-2020 total-function arm), the
crisis-overlay window edges + the None-abstain law, the composite
classify_session (vol-None honest abstain), the validate_era_boundaries
laws (the synthetic tables below are MECHANISM tests only, never a claim
about any real regulatory date — Golden Rule #1), the TAXONOMY_VERSION
pin, and the verification-census pin on the shipped table.
"""

from __future__ import annotations

from datetime import date

import pytest
from brutex.metrics.regime_taxonomy import (
    COVID_2020_END,
    COVID_2020_START,
    ERA_BOUNDARIES,
    EXTREME_VOL_OVERLAY_START,
    TAXONOMY_VERSION,
    EraBoundary,
    StratumKey,
    TrendStratum,
    VolBand,
    classify_session,
    classify_vix_band,
    era_for_date,
    in_extreme_vol_overlay,
    is_covid_2020_overlay,
    trend_stratum_from_lens_token,
    validate_era_boundaries,
)

# ── VIX bands ────────────────────────────────────────────────────────────────


class TestClassifyVixBand:
    @pytest.mark.parametrize(
        ("vix", "band"),
        [
            (0.01, VolBand.LOW),
            (12.99, VolBand.LOW),
            (13.0, VolBand.MID),
            (16.99, VolBand.MID),
            (17.0, VolBand.HIGH),
            (24.99, VolBand.HIGH),
            (25.0, VolBand.EXTREME),
            (80.0, VolBand.EXTREME),
        ],
    )
    def test_boundary_exact_bands(self, vix: float, band: VolBand) -> None:
        assert classify_vix_band(vix) is band

    @pytest.mark.parametrize("junk", [float("nan"), float("inf"), float("-inf"), 0.0, -1.0])
    def test_junk_input_refused(self, junk: float) -> None:
        with pytest.raises(ValueError, match="never fabricated"):
            classify_vix_band(junk)


# ── Trend tokens ─────────────────────────────────────────────────────────────


class TestTrendStratumFromLensToken:
    @pytest.mark.parametrize(
        ("token", "stratum"),
        [
            ("up", TrendStratum.UP),
            ("down", TrendStratum.DOWN),
            ("sideways", TrendStratum.CHOP),
            ("unknown", TrendStratum.UNKNOWN),
            (None, TrendStratum.UNKNOWN),
            ("", TrendStratum.UNKNOWN),
            ("UP", TrendStratum.UNKNOWN),  # case-sensitive: lens emits lowercase
            ("junk-token", TrendStratum.UNKNOWN),
        ],
    )
    def test_full_mapping(self, token: str | None, stratum: TrendStratum) -> None:
        assert trend_stratum_from_lens_token(token) is stratum


# ── Era bisect ───────────────────────────────────────────────────────────────


class TestEraForDate:
    @pytest.mark.parametrize(
        ("d", "label"),
        [
            (date(2020, 1, 1), "pre_peak_margin"),
            (date(2021, 8, 31), "pre_peak_margin"),
            (date(2021, 9, 1), "post_peak_margin_full"),
            (date(2024, 11, 13), "post_peak_margin_full"),
            (date(2024, 11, 14), "post_weekly_rationalization"),
            (date(2026, 7, 19), "post_weekly_rationalization"),
        ],
    )
    def test_bisect_exact_edges(self, d: date, label: str) -> None:
        assert era_for_date(d).label == label

    def test_pre_2020_total_function_arm(self) -> None:
        # A date BEFORE the first boundary returns the FIRST era (total
        # function — documented labeling choice, never a raise).
        assert era_for_date(date(2019, 12, 31)).label == "pre_peak_margin"
        assert era_for_date(date.min).label == "pre_peak_margin"


# ── Crisis overlays ──────────────────────────────────────────────────────────


class TestCovidOverlay:
    def test_window_edges_inclusive(self) -> None:
        # Both ends INCLUSIVE per the module's documented convention.
        assert is_covid_2020_overlay(COVID_2020_START) is True
        assert is_covid_2020_overlay(COVID_2020_END) is True
        assert is_covid_2020_overlay(date(2020, 2, 14)) is False
        assert is_covid_2020_overlay(date(2020, 7, 1)) is False

    def test_interior_true(self) -> None:
        assert is_covid_2020_overlay(date(2020, 3, 23)) is True


class TestExtremeVolOverlay:
    def test_none_vix_abstains(self) -> None:
        assert in_extreme_vol_overlay(date(2025, 1, 1), None) is None

    def test_pre_2024_false_even_extreme(self) -> None:
        assert in_extreme_vol_overlay(date(2023, 12, 31), 40.0) is False

    def test_extreme_true_at_start_edge(self) -> None:
        assert in_extreme_vol_overlay(EXTREME_VOL_OVERLAY_START, 25.0) is True

    def test_non_extreme_vix_false(self) -> None:
        assert in_extreme_vol_overlay(date(2025, 1, 1), 24.99) is False

    def test_junk_vix_propagates_refusal(self) -> None:
        with pytest.raises(ValueError, match="never fabricated"):
            in_extreme_vol_overlay(date(2025, 1, 1), float("nan"))


# ── Composite ────────────────────────────────────────────────────────────────


class TestClassifySession:
    def test_full_composite(self) -> None:
        key = classify_session(date(2024, 12, 1), vix_close=18.5, trend_token="up")
        assert key == StratumKey(
            vol=VolBand.HIGH,
            trend=TrendStratum.UP,
            era_label="post_weekly_rationalization",
        )

    def test_vol_none_honest_abstain(self) -> None:
        key = classify_session(date(2020, 6, 1), vix_close=None, trend_token="sideways")
        assert key.vol is None
        assert key.trend is TrendStratum.CHOP
        assert key.era_label == "pre_peak_margin"

    def test_junk_vix_refused_never_binned(self) -> None:
        with pytest.raises(ValueError, match="never fabricated"):
            classify_session(date(2024, 1, 1), vix_close=0.0, trend_token="up")

    def test_unknown_trend_token_degrades(self) -> None:
        key = classify_session(date(2022, 1, 3), vix_close=12.0, trend_token=None)
        assert key.trend is TrendStratum.UNKNOWN
        assert key.vol is VolBand.LOW
        assert key.era_label == "post_peak_margin_full"


# ── Table validation laws (synthetic tables — mechanism only) ────────────────


def _row(start: date, label: str, *, verified: bool = True, source: str = "test src") -> EraBoundary:
    return EraBoundary(start, label, source, verified)


class TestValidateEraBoundaries:
    def test_empty_raises(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            validate_era_boundaries(())

    def test_wrong_floor_raises(self) -> None:
        with pytest.raises(ValueError, match="window floor"):
            validate_era_boundaries((_row(date(2021, 1, 1), "a"),))

    def test_unordered_raises(self) -> None:
        rows = (
            _row(date(2020, 1, 1), "a"),
            _row(date(2023, 1, 1), "b"),
            _row(date(2022, 1, 1), "c"),
        )
        with pytest.raises(ValueError, match="strictly ascending"):
            validate_era_boundaries(rows)

    def test_duplicate_start_raises(self) -> None:
        rows = (
            _row(date(2020, 1, 1), "a"),
            _row(date(2022, 1, 1), "b"),
            _row(date(2022, 1, 1), "c"),
        )
        with pytest.raises(ValueError, match="strictly ascending"):
            validate_era_boundaries(rows)

    def test_duplicate_label_raises(self) -> None:
        rows = (
            _row(date(2020, 1, 1), "a"),
            _row(date(2022, 1, 1), "a"),
        )
        with pytest.raises(ValueError, match="unique"):
            validate_era_boundaries(rows)

    def test_unverified_without_stamp_raises(self) -> None:
        rows = (
            _row(date(2020, 1, 1), "a"),
            _row(date(2022, 1, 1), "b", verified=False, source="best guess"),
        )
        with pytest.raises(ValueError, match="UNVERIFIED"):
            validate_era_boundaries(rows)

    def test_unverified_with_stamp_passes(self) -> None:
        rows = (
            _row(date(2020, 1, 1), "a"),
            _row(date(2022, 1, 1), "b", verified=False, source="UNVERIFIED — no citation"),
        )
        validate_era_boundaries(rows)  # must not raise

    def test_shipped_table_passes(self) -> None:
        validate_era_boundaries(ERA_BOUNDARIES)  # the import-time call, re-run


# ── Version + verification-census pins ───────────────────────────────────────


class TestPins:
    def test_taxonomy_version_pin(self) -> None:
        assert TAXONOMY_VERSION == 1

    def test_shipped_verified_census_pin(self) -> None:
        # Honesty census (DEC-ETC-REGIME-HISTORY-001: reconstruction is
        # NOT verification): EXACTLY ONE shipped row is verified=False —
        # the 2021-09-01 peak-margin boundary, whose circular id + phase
        # dates are SECONDARY-corroborated only (primary SEBI circular
        # PDF not retrieved). Flipping it back to verified=True requires
        # the primary circular in hand — a CONSCIOUS change.
        assert [row.label for row in ERA_BOUNDARIES if not row.verified] == [
            "post_peak_margin_full"
        ]
        unverified = next(
            row for row in ERA_BOUNDARIES if row.label == "post_peak_margin_full"
        )
        assert unverified.verified is False
        assert unverified.source.startswith("UNVERIFIED — ")
        # The honest source retains the circular id + phase dates.
        assert "SEBI/HO/MRD2/DCAP/CIR/P/2020/127" in unverified.source
        assert "primary circular not retrieved" in unverified.source
        # The UNVERIFIED-stamp law still passes over the shipped table.
        validate_era_boundaries(ERA_BOUNDARIES)

    def test_shipped_labels_pin(self) -> None:
        assert [row.label for row in ERA_BOUNDARIES] == [
            "pre_peak_margin",
            "post_peak_margin_full",
            "post_weekly_rationalization",
        ]
