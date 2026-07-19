"""Seconds-keyed timeframe registry tests (DEC-TF-REGISTRY-DYNAMIC-001).

The frozen-vocab class pins the DERIVED views byte-equal to the literal
pre-registry vocabulary; the config classes prove a NEW timeframe is one
registry entry away (20m) and that sub-minute specs are framework-accepted
but honestly excluded from the minute views (no sub-minute production feed
exists — Groww is 1-minute-only).
"""

from __future__ import annotations

from typing import cast

import pytest
from brutex.data.timeframes import (
    ALL_LAKE_INTERVAL_LABELS,
    ALL_TF_MINUTES,
    ALL_TFS,
    EXPECTED_TF_COUNT,
    LAKE_INTERVAL_LABELS,
    TF_LABELS,
    TfAnchor,
    TfSpec,
    all_lake_labels_view,
    lake_interval_labels_view,
    spec_for_lake_label,
    spec_for_minutes,
    tf_labels_view,
    tf_minutes_view,
    validate_registry,
)

_EXPECTED_MINUTES = (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 30, 60, 120, 180, 240, 375)

_EXPECTED_TF_LABELS = {
    1: "1m",
    2: "2m",
    3: "3m",
    4: "4m",
    5: "5m",
    6: "6m",
    7: "7m",
    8: "8m",
    9: "9m",
    10: "10m",
    11: "11m",
    12: "12m",
    13: "13m",
    14: "14m",
    15: "15m",
    30: "30m",
    60: "60m",
    120: "120m",
    180: "180m",
    240: "240m",
    375: "1d",
}

_EXPECTED_LAKE_LABELS = {
    1: "1minute",
    2: "2minute",
    3: "3minute",
    4: "4minute",
    5: "5minute",
    6: "6minute",
    7: "7minute",
    8: "8minute",
    9: "9minute",
    10: "10minute",
    11: "11minute",
    12: "12minute",
    13: "13minute",
    14: "14minute",
    15: "15minute",
    30: "30minute",
    60: "1hour",
    120: "2hour",
    180: "3hour",
    240: "4hour",
    375: "1day",
}


class TestFrozenVocabByteParity:
    """The derived views serve the EXACT pre-registry literal vocabulary."""

    def test_all_tf_minutes_literal(self) -> None:
        assert ALL_TF_MINUTES == _EXPECTED_MINUTES

    def test_tf_labels_literal(self) -> None:
        assert TF_LABELS == _EXPECTED_TF_LABELS

    def test_lake_interval_labels_literal(self) -> None:
        assert LAKE_INTERVAL_LABELS == _EXPECTED_LAKE_LABELS

    def test_all_lake_labels_registry_order(self) -> None:
        assert (
            tuple(_EXPECTED_LAKE_LABELS[m] for m in _EXPECTED_MINUTES) == ALL_LAKE_INTERVAL_LABELS
        )

    def test_registry_count(self) -> None:
        assert len(ALL_TFS) == EXPECTED_TF_COUNT == 21

    def test_daily_spec_shape(self) -> None:
        daily = ALL_TFS[-1]
        assert daily.label == "1d"
        assert daily.duration_s == 22_500
        assert daily.anchor is TfAnchor.IST_DAY
        assert daily.lake_label == "1day"

    def test_default_registry_is_all_minute(self) -> None:
        assert all(spec.is_minute for spec in ALL_TFS)

    def test_intraday_specs_are_session_anchored(self) -> None:
        assert all(spec.anchor is TfAnchor.SESSION_OPEN for spec in ALL_TFS[:-1])


class TestTfSpecValidation:
    def test_valid_minute_spec(self) -> None:
        spec = TfSpec(
            label="5m",
            duration_s=300,
            base_granularity_s=60,
            anchor=TfAnchor.SESSION_OPEN,
            lake_label="5minute",
        )
        assert spec.is_minute
        assert spec.minutes == 5

    def test_empty_label_raises(self) -> None:
        with pytest.raises(ValueError, match="label must be non-empty"):
            TfSpec(
                label="",
                duration_s=60,
                base_granularity_s=60,
                anchor=TfAnchor.SESSION_OPEN,
                lake_label="1minute",
            )

    def test_empty_lake_label_raises(self) -> None:
        with pytest.raises(ValueError, match="lake_label must be non-empty"):
            TfSpec(
                label="1m",
                duration_s=60,
                base_granularity_s=60,
                anchor=TfAnchor.SESSION_OPEN,
                lake_label="",
            )

    def test_non_anchor_raises(self) -> None:
        with pytest.raises(ValueError, match="must be a TfAnchor member"):
            TfSpec(
                label="1m",
                duration_s=60,
                base_granularity_s=60,
                anchor=cast(TfAnchor, "session_open"),
                lake_label="1minute",
            )

    def test_zero_duration_raises(self) -> None:
        with pytest.raises(ValueError, match="duration_s must be >= 1"):
            TfSpec(
                label="0s",
                duration_s=0,
                base_granularity_s=1,
                anchor=TfAnchor.SESSION_OPEN,
                lake_label="0second",
            )

    def test_zero_base_granularity_raises(self) -> None:
        with pytest.raises(ValueError, match="base_granularity_s must be >= 1"):
            TfSpec(
                label="1m",
                duration_s=60,
                base_granularity_s=0,
                anchor=TfAnchor.SESSION_OPEN,
                lake_label="1minute",
            )

    def test_duration_not_multiple_of_base_raises(self) -> None:
        with pytest.raises(ValueError, match="integer multiple"):
            TfSpec(
                label="90s",
                duration_s=90,
                base_granularity_s=60,
                anchor=TfAnchor.SESSION_OPEN,
                lake_label="90second",
            )

    def test_sub_minute_spec_is_not_minute(self) -> None:
        spec = TfSpec(
            label="1s",
            duration_s=1,
            base_granularity_s=1,
            anchor=TfAnchor.SESSION_OPEN,
            lake_label="1second",
        )
        assert not spec.is_minute

    def test_sub_minute_minutes_raises(self) -> None:
        spec = TfSpec(
            label="30s",
            duration_s=30,
            base_granularity_s=1,
            anchor=TfAnchor.SESSION_OPEN,
            lake_label="30second",
        )
        with pytest.raises(ValueError, match="not a whole-minute timeframe"):
            _ = spec.minutes


def _spec(label: str, duration_s: int, lake_label: str, *, base: int = 60) -> TfSpec:
    return TfSpec(
        label=label,
        duration_s=duration_s,
        base_granularity_s=base,
        anchor=TfAnchor.SESSION_OPEN,
        lake_label=lake_label,
    )


class TestValidateRegistry:
    def test_empty_registry_raises(self) -> None:
        with pytest.raises(ValueError, match="must not be empty"):
            validate_registry(())

    def test_duplicate_labels_raise(self) -> None:
        with pytest.raises(ValueError, match="duplicate TfSpec labels"):
            validate_registry((_spec("1m", 60, "1minute"), _spec("1m", 120, "2minute")))

    def test_duplicate_lake_labels_raise(self) -> None:
        with pytest.raises(ValueError, match="duplicate TfSpec lake_labels"):
            validate_registry((_spec("1m", 60, "1minute"), _spec("2m", 120, "1minute")))

    def test_duplicate_minutes_raise(self) -> None:
        with pytest.raises(ValueError, match="duplicate whole-minute durations"):
            validate_registry((_spec("1m", 60, "1minute"), _spec("one", 60, "oneminute")))

    def test_default_registry_validates(self) -> None:
        validate_registry(ALL_TFS)


class TestSpecLookups:
    def test_spec_for_lake_label_daily(self) -> None:
        assert spec_for_lake_label("1day").anchor is TfAnchor.IST_DAY

    def test_spec_for_lake_label_unknown_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown lake interval label"):
            spec_for_lake_label("9second")

    def test_spec_for_minutes_hit(self) -> None:
        assert spec_for_minutes(5).lake_label == "5minute"

    def test_spec_for_minutes_unknown_raises(self) -> None:
        with pytest.raises(ValueError, match="Unsupported timeframe: 20 minutes"):
            spec_for_minutes(20)

    def test_spec_for_minutes_skips_sub_minute(self) -> None:
        one_second = TfSpec(
            label="1s",
            duration_s=1,
            base_granularity_s=1,
            anchor=TfAnchor.SESSION_OPEN,
            lake_label="1second",
        )
        specs = (one_second, _spec("1m", 60, "1minute"))
        assert spec_for_minutes(1, specs).lake_label == "1minute"


class TestNewTfByConfig:
    """A NEW minute-multiple timeframe is ONE registry entry away."""

    _TWENTY = TfSpec(
        label="20m",
        duration_s=1_200,
        base_granularity_s=60,
        anchor=TfAnchor.SESSION_OPEN,
        lake_label="20minute",
    )

    def test_registry_with_20m_validates(self) -> None:
        validate_registry((*ALL_TFS, self._TWENTY))

    def test_20m_in_minutes_view(self) -> None:
        assert 20 in tf_minutes_view((*ALL_TFS, self._TWENTY))

    def test_20m_in_label_views(self) -> None:
        registry = (*ALL_TFS, self._TWENTY)
        assert tf_labels_view(registry)[20] == "20m"
        assert lake_interval_labels_view(registry)[20] == "20minute"
        assert "20minute" in all_lake_labels_view(registry)

    def test_20m_resolvable(self) -> None:
        registry = (*ALL_TFS, self._TWENTY)
        assert spec_for_minutes(20, registry) is self._TWENTY
        assert spec_for_lake_label("20minute", registry) is self._TWENTY


class TestSubMinuteFramework:
    """Sub-minute specs: framework-accepted, honestly excluded from minute views.

    No sub-minute production feed exists (Groww is 1-minute-only) — these
    prove the TYPE admits a future 1s/30s registration, nothing more.
    """

    _ONE_S = TfSpec(
        label="1s",
        duration_s=1,
        base_granularity_s=1,
        anchor=TfAnchor.SESSION_OPEN,
        lake_label="1second",
    )

    def test_registry_with_1s_validates(self) -> None:
        validate_registry((*ALL_TFS, self._ONE_S))

    def test_1s_excluded_from_minute_views(self) -> None:
        registry = (*ALL_TFS, self._ONE_S)
        assert tf_minutes_view(registry) == ALL_TF_MINUTES
        assert tf_labels_view(registry) == TF_LABELS
        assert lake_interval_labels_view(registry) == LAKE_INTERVAL_LABELS

    def test_1s_in_all_lake_labels_view(self) -> None:
        assert "1second" in all_lake_labels_view((*ALL_TFS, self._ONE_S))
