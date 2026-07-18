"""Unit suite for :mod:`brutex.ranking.presets` (DEC-SWEEP-RESULTS-DEPTH-001, WP3).

Pins the R30 total order per preset, the unknown-token degradation, the
run_id tie law, the None-composite honesty, the generic two-pass law and a
LOCKED 10-record ``min_dd_first`` order fixture (the design's presets golden,
inlined — the expected order is data, no digest needed).
"""

from __future__ import annotations

from typing import Any

import pytest
from brutex.ranking.presets import (
    DEFAULT_PRESET,
    PRESET_MIN_DD_FIRST,
    PRESET_NAMES,
    PRESET_WIN_RATE_SAFE,
    PRESETS,
    RankingPreset,
    RankRecord,
    min_dd_first_key,
    resolve_preset,
    sort_by_preset,
    win_rate_safe_key,
)
from structlog.testing import capture_logs


def _rec(run_id: str, **overrides: Any) -> RankRecord:
    base: dict[str, Any] = {
        "run_id": run_id,
        "max_drawdown_pct": 10.0,
        "composite_score": 1.0,
        "max_consecutive_losses": 3,
        "profit_factor": 1.5,
        "win_rate": 0.6,
        "calmar_ratio": 0.8,
    }
    base.update(overrides)
    return RankRecord(**base)


class TestPresetVocabulary:
    def test_display_order_and_default(self) -> None:
        # "growth" appended after the R30 five (DEC-METRICS-KELLY-GROWTH-001)
        # and "win_rate_safe" appended LAST
        # (DEC-METRICS-LOSSRATE-FIRSTCLASS-001 Stage B) — each a sanctioned
        # additive vocabulary extension.
        assert PRESET_NAMES == (
            "min_dd_first",
            "composite",
            "profit_factor",
            "win_rate",
            "calmar",
            "growth",
            "win_rate_safe",
            "recent",
            "recent_decay",
        )
        assert DEFAULT_PRESET == PRESET_MIN_DD_FIRST == "min_dd_first"
        assert PRESET_WIN_RATE_SAFE == "win_rate_safe"

    def test_every_preset_names_itself(self) -> None:
        for token, preset in PRESETS.items():
            assert preset.name == token
            assert preset.describe.strip()

    def test_direction_flags(self) -> None:
        # min_dd_first + win_rate_safe are the multi-part ASCENDING presets
        # (in-tuple sign flips, ONE reverse per preset); the single-metric
        # alternates all rank descending on the raw value (the
        # ResultsStore.top_k law).
        assert PRESETS["min_dd_first"].reverse is False
        assert PRESETS["win_rate_safe"].reverse is False
        for token in ("composite", "profit_factor", "win_rate", "calmar", "growth"):
            assert PRESETS[token].reverse is True

    @pytest.mark.parametrize("token", PRESET_NAMES)
    def test_resolve_known_token(self, token: str) -> None:
        assert resolve_preset(token) is PRESETS[token]

    @pytest.mark.parametrize("token", ["", "bogus", "MIN_DD_FIRST", "sharpe"])
    def test_resolve_unknown_degrades_to_default_logged(self, token: str) -> None:
        with capture_logs() as logs:
            resolved = resolve_preset(token)
        assert resolved is PRESETS[DEFAULT_PRESET]
        events = [log for log in logs if log["event"] == "ranking.presets.unknown_preset"]
        assert len(events) == 1
        assert events[0]["requested"] == token
        assert events[0]["resolved"] == DEFAULT_PRESET


class TestMinDdFirstLaw:
    def test_smaller_drawdown_wins(self) -> None:
        low = _rec("b" * 64, max_drawdown_pct=5.0)
        high = _rec("a" * 64, max_drawdown_pct=25.0)
        assert PRESETS["min_dd_first"].sort_records([high, low]) == (low, high)

    def test_composite_breaks_drawdown_tie_descending(self) -> None:
        weak = _rec("a" * 64, composite_score=0.5)
        strong = _rec("b" * 64, composite_score=2.5)
        assert PRESETS["min_dd_first"].sort_records([weak, strong]) == (strong, weak)

    def test_none_composite_ranks_after_every_scored_one(self) -> None:
        # Even a NEGATIVE composite outranks an unscored combo — None is
        # "honestly unscored", never coerced to a fake 0.0 that would beat it.
        unscored = _rec("a" * 64, composite_score=None)
        negative = _rec("b" * 64, composite_score=-4.0)
        assert PRESETS["min_dd_first"].sort_records([unscored, negative]) == (negative, unscored)

    def test_fewest_consecutive_losses_breaks_composite_tie(self) -> None:
        streaky = _rec("a" * 64, max_consecutive_losses=9)
        calm = _rec("b" * 64, max_consecutive_losses=1)
        assert PRESETS["min_dd_first"].sort_records([streaky, calm]) == (calm, streaky)

    def test_full_tie_falls_to_run_id_asc(self) -> None:
        first = _rec("1" * 64)
        second = _rec("2" * 64)
        assert PRESETS["min_dd_first"].sort_records([second, first]) == (first, second)

    def test_key_shape(self) -> None:
        scored = min_dd_first_key(_rec("a" * 64, composite_score=2.0))
        assert scored == (10.0, 0, -2.0, 3)
        unscored = min_dd_first_key(_rec("a" * 64, composite_score=None))
        assert unscored == (10.0, 1, -0.0, 3)


class TestDescendingPresets:
    @pytest.mark.parametrize(
        ("token", "field"),
        [
            ("composite", "composite_score"),
            ("profit_factor", "profit_factor"),
            ("win_rate", "win_rate"),
            ("calmar", "calmar_ratio"),
            ("growth", "log_growth_at_half_kelly"),
            ("recent", "trailing_sharpe_ratio"),
            ("recent_decay", "decay_weighted_net_paisa"),
        ],
    )
    def test_higher_raw_value_first(self, token: str, field: str) -> None:
        low = _rec("a" * 64, **{field: 0.4})
        high = _rec("b" * 64, **{field: 3.1})
        assert PRESETS[token].sort_records([low, high]) == (high, low)

    @pytest.mark.parametrize(
        "token",
        ["composite", "profit_factor", "win_rate", "calmar", "growth", "recent", "recent_decay"],
    )
    def test_value_tie_falls_to_run_id_asc(self, token: str) -> None:
        first = _rec("1" * 64)
        second = _rec("2" * 64)
        assert PRESETS[token].sort_records([second, first]) == (first, second)

    def test_composite_none_ranks_last(self) -> None:
        unscored = _rec("a" * 64, composite_score=None)
        low = _rec("c" * 64, composite_score=-9.0)
        high = _rec("b" * 64, composite_score=2.0)
        assert PRESETS["composite"].sort_records([unscored, low, high]) == (high, low, unscored)

    def test_growth_none_ranks_last(self) -> None:
        # DEC-METRICS-KELLY-GROWTH-001: an unscored / pre-extension-store
        # record (None log growth) ranks AFTER every scored one — the
        # composite None-after-scored idiom, never a fabricated 0.
        unscored = _rec("a" * 64, log_growth_at_half_kelly=None)
        low = _rec("c" * 64, log_growth_at_half_kelly=-0.4)
        high = _rec("b" * 64, log_growth_at_half_kelly=0.02)
        assert PRESETS["growth"].sort_records([unscored, low, high]) == (high, low, unscored)

    def test_rank_record_growth_field_defaults_none(self) -> None:
        # Defaulted-last so pre-extension record builders stay valid.
        assert _rec("a" * 64).log_growth_at_half_kelly is None

    @pytest.mark.parametrize(
        ("token", "field"),
        [
            ("recent", "trailing_sharpe_ratio"),
            ("recent_decay", "decay_weighted_net_paisa"),
        ],
    )
    def test_recency_none_ranks_last(self, token: str, field: str) -> None:
        # DEC-METRICS-TRAILING-WINDOW-001: an unscored record (a
        # pre-extension store row / anchor-less run / empty trailing window)
        # ranks AFTER every scored one — the None-after-scored idiom.
        unscored = _rec("a" * 64, **{field: None})
        low = _rec("c" * 64, **{field: -5.0})
        high = _rec("b" * 64, **{field: 2.0})
        assert PRESETS[token].sort_records([unscored, low, high]) == (high, low, unscored)

    def test_rank_record_recency_fields_default_none(self) -> None:
        # Defaulted-LAST so pre-extension record builders stay valid.
        record = _rec("a" * 64)
        assert record.trailing_sharpe_ratio is None
        assert record.decay_weighted_net_paisa is None


class TestWinRateSafeLaw:
    """DEC-METRICS-LOSSRATE-FIRSTCLASS-001 Stage B: the win_rate_safe order."""

    def test_higher_win_rate_wins(self) -> None:
        low = _rec("a" * 64, win_rate=0.4, sl_hit_rate=0.0)
        high = _rec("b" * 64, win_rate=0.9, sl_hit_rate=1.0)
        # Win rate dominates — even a worse SL-hit rate cannot outrank it.
        assert PRESETS["win_rate_safe"].sort_records([low, high]) == (high, low)

    def test_lower_sl_hit_rate_breaks_win_rate_tie(self) -> None:
        stopped = _rec("a" * 64, sl_hit_rate=0.8)
        safe = _rec("b" * 64, sl_hit_rate=0.1)
        assert PRESETS["win_rate_safe"].sort_records([stopped, safe]) == (safe, stopped)

    def test_none_sl_hit_rate_ranks_after_every_concrete_one(self) -> None:
        # A pre-extension row (None rate) ranks AFTER even an all-SL row
        # within an equal win rate — None is "honestly unknown", never
        # coerced to a fake 0.0 that would read "never hits SL".
        unknown = _rec("a" * 64, sl_hit_rate=None)
        all_sl = _rec("b" * 64, sl_hit_rate=1.0)
        assert PRESETS["win_rate_safe"].sort_records([unknown, all_sl]) == (all_sl, unknown)

    def test_higher_win_rate_outranks_unknown_penalty(self) -> None:
        # The None penalty applies WITHIN an equal win rate only — a higher
        # win rate with an unknown SL-hit rate still ranks first.
        unknown_high = _rec("a" * 64, win_rate=0.9, sl_hit_rate=None)
        concrete_low = _rec("b" * 64, win_rate=0.4, sl_hit_rate=0.0)
        assert PRESETS["win_rate_safe"].sort_records([unknown_high, concrete_low]) == (
            unknown_high,
            concrete_low,
        )

    def test_composite_breaks_sl_hit_rate_tie_descending(self) -> None:
        weak = _rec("a" * 64, sl_hit_rate=0.2, composite_score=0.5)
        strong = _rec("b" * 64, sl_hit_rate=0.2, composite_score=2.5)
        assert PRESETS["win_rate_safe"].sort_records([weak, strong]) == (strong, weak)

    def test_none_composite_ranks_after_every_scored_one(self) -> None:
        unscored = _rec("a" * 64, sl_hit_rate=0.2, composite_score=None)
        negative = _rec("b" * 64, sl_hit_rate=0.2, composite_score=-4.0)
        assert PRESETS["win_rate_safe"].sort_records([unscored, negative]) == (
            negative,
            unscored,
        )

    def test_full_tie_falls_to_run_id_asc(self) -> None:
        first = _rec("1" * 64, sl_hit_rate=0.3)
        second = _rec("2" * 64, sl_hit_rate=0.3)
        assert PRESETS["win_rate_safe"].sort_records([second, first]) == (first, second)

    def test_key_shape(self) -> None:
        concrete = win_rate_safe_key(
            _rec("a" * 64, win_rate=0.6, sl_hit_rate=0.25, composite_score=2.0)
        )
        assert concrete == (-0.6, 0, 0.25, 0, -2.0)
        unknown = win_rate_safe_key(
            _rec("a" * 64, win_rate=0.6, sl_hit_rate=None, composite_score=None)
        )
        assert unknown == (-0.6, 1, 0.0, 1, -0.0)

    def test_rank_record_sl_hit_rate_field_defaults_none(self) -> None:
        # Defaulted-LAST so pre-extension record builders stay valid.
        assert _rec("a" * 64).sl_hit_rate is None

    def test_registered_appended_last(self) -> None:
        # Stage C appended the two recency presets AFTER win_rate_safe (each
        # generation strictly after the previous — the appended-LAST law).
        assert PRESET_NAMES[-3:] == (PRESET_WIN_RATE_SAFE, "recent", "recent_decay")
        assert PRESETS[PRESET_WIN_RATE_SAFE].key is win_rate_safe_key


class TestSortByPresetGeneric:
    def test_projects_arbitrary_item_shapes(self) -> None:
        # The top100 builder's usage shape: items are (record, payload) pairs
        # ranked through a projection — the ONE law home serves both callers.
        items = [
            {"record": _rec("b" * 64, max_drawdown_pct=20.0), "tag": "worse"},
            {"record": _rec("a" * 64, max_drawdown_pct=2.0), "tag": "better"},
        ]
        ordered = sort_by_preset(
            items,
            preset=PRESETS["min_dd_first"],
            record_of=lambda item: item["record"],  # type: ignore[arg-type,return-value]
        )
        assert [item["tag"] for item in ordered] == ["better", "worse"]

    def test_returns_tuple_and_leaves_input_unmodified(self) -> None:
        records = [_rec("b" * 64), _rec("a" * 64)]
        snapshot = list(records)
        ordered = PRESETS["win_rate"].sort_records(records)
        assert isinstance(ordered, tuple)
        assert records == snapshot

    def test_empty_input(self) -> None:
        assert PRESETS["calmar"].sort_records([]) == ()

    def test_custom_preset_rides_the_same_law(self) -> None:
        # RankingPreset is an open dataclass: a caller-defined preset sorts
        # through the identical two-pass law (no hidden name switch).
        preset = RankingPreset(
            name="losses_asc",
            describe="fewest consecutive losses first",
            reverse=False,
            key=lambda record: (1, float(record.max_consecutive_losses)),
        )
        calm = _rec("b" * 64, max_consecutive_losses=1)
        streaky = _rec("a" * 64, max_consecutive_losses=8)
        assert preset.sort_records([streaky, calm]) == (calm, streaky)


class TestMinDdFirstLockedOrder:
    """The design's presets golden: a fixed 10-record fixture, order LOCKED."""

    def test_locked_ten_record_order(self) -> None:
        records = [
            _rec("00" + "0" * 62, max_drawdown_pct=12.0, composite_score=1.0),
            _rec("01" + "0" * 62, max_drawdown_pct=3.0, composite_score=0.2),
            _rec("02" + "0" * 62, max_drawdown_pct=3.0, composite_score=2.4),
            _rec("03" + "0" * 62, max_drawdown_pct=3.0, composite_score=None),
            _rec("04" + "0" * 62, max_drawdown_pct=7.5, composite_score=1.1),
            _rec(
                "05" + "0" * 62,
                max_drawdown_pct=7.5,
                composite_score=1.1,
                max_consecutive_losses=1,
            ),
            _rec(
                "06" + "0" * 62,
                max_drawdown_pct=7.5,
                composite_score=1.1,
                max_consecutive_losses=1,
            ),
            _rec("07" + "0" * 62, max_drawdown_pct=29.9, composite_score=9.9),
            _rec("08" + "0" * 62, max_drawdown_pct=0.5, composite_score=-1.0),
            _rec("09" + "0" * 62, max_drawdown_pct=0.5, composite_score=None),
        ]
        ordered = PRESETS["min_dd_first"].sort_records(list(reversed(records)))
        assert [record.run_id[:2] for record in ordered] == [
            "08",  # dd 0.5, scored
            "09",  # dd 0.5, unscored — after the scored one
            "02",  # dd 3.0, composite 2.4
            "01",  # dd 3.0, composite 0.2
            "03",  # dd 3.0, unscored
            "05",  # dd 7.5, composite 1.1, losses 1, run_id 05 < 06
            "06",
            "04",  # dd 7.5, composite 1.1, losses 3
            "00",  # dd 12.0
            "07",  # dd 29.9
        ]
