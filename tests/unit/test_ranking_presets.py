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
    PRESETS,
    RankingPreset,
    RankRecord,
    min_dd_first_key,
    resolve_preset,
    sort_by_preset,
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
        # Consciously extended (`DEC-OPTIONS-EXEC-FEEDBACK-001`): the
        # net_after_cost preset joins the vocabulary tail — the options-exec
        # headline net after the full cost stack, unpriced entries last.
        assert PRESET_NAMES == (
            "min_dd_first",
            "composite",
            "profit_factor",
            "win_rate",
            "calmar",
            "net_after_cost",
        )
        assert DEFAULT_PRESET == PRESET_MIN_DD_FIRST == "min_dd_first"

    def test_every_preset_names_itself(self) -> None:
        for token, preset in PRESETS.items():
            assert preset.name == token
            assert preset.describe.strip()

    def test_direction_flags(self) -> None:
        # min_dd_first is the ONE ascending preset; the alternates all rank
        # descending on the raw value (the ResultsStore.top_k law).
        assert PRESETS["min_dd_first"].reverse is False
        for token in ("composite", "profit_factor", "win_rate", "calmar"):
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
        ],
    )
    def test_higher_raw_value_first(self, token: str, field: str) -> None:
        low = _rec("a" * 64, **{field: 0.4})
        high = _rec("b" * 64, **{field: 3.1})
        assert PRESETS[token].sort_records([low, high]) == (high, low)

    @pytest.mark.parametrize("token", ["composite", "profit_factor", "win_rate", "calmar"])
    def test_value_tie_falls_to_run_id_asc(self, token: str) -> None:
        first = _rec("1" * 64)
        second = _rec("2" * 64)
        assert PRESETS[token].sort_records([second, first]) == (first, second)

    def test_composite_none_ranks_last(self) -> None:
        unscored = _rec("a" * 64, composite_score=None)
        low = _rec("c" * 64, composite_score=-9.0)
        high = _rec("b" * 64, composite_score=2.0)
        assert PRESETS["composite"].sort_records([unscored, low, high]) == (high, low, unscored)


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


class TestNetAfterCost:
    """`DEC-OPTIONS-EXEC-FEEDBACK-001` — the INT-EXACT ``net_after_cost`` preset.

    The key is ``(value_present, paisa)`` on raw Python ints: paisa sums can
    exceed the float64 53-bit mantissa, so the value must NEVER round-trip
    through float (the ``_composite_key``-style ``(1, float(value))`` shape
    would collapse a one-paisa distinction at 2**53); unpriced (``None``)
    entries rank AFTER every priced one under ``reverse=True``; ties fall to
    the ``run_id``-ASC stable pre-sort.
    """

    def test_reverse_flag_is_true(self) -> None:
        assert PRESETS["net_after_cost"].reverse is True

    def test_key_shape(self) -> None:
        from brutex.ranking.presets import _net_after_cost_key

        assert _net_after_cost_key(_rec("a" * 64, net_after_cost_paisa=123)) == (1, 123)
        # The defaulted trailing field: no override == honestly unpriced.
        assert _net_after_cost_key(_rec("a" * 64)) == (0, 0)

    def test_key_is_int_exact_beyond_the_float_mantissa(self) -> None:
        """``2**53`` vs ``2**53 + 1`` produce DISTINCT, correctly-ORDERED keys.

        WHY float would collapse them: float64 carries a 53-bit mantissa, so
        ``float(2**53) == float(2**53 + 1)`` — any float round-trip of the
        paisa would read the two nets as a TIE and hand the order to the
        run_id tie law instead of the money. Python int tuple comparison is
        exact at any magnitude (the /results exact-int-not-float precedent).
        """
        from brutex.ranking.presets import _net_after_cost_key

        assert float(2**53) == float(2**53 + 1)  # the trap being avoided
        lo = _net_after_cost_key(_rec("a" * 64, net_after_cost_paisa=2**53))
        hi = _net_after_cost_key(_rec("b" * 64, net_after_cost_paisa=2**53 + 1))
        assert lo != hi
        assert hi > lo
        assert lo == (1, 2**53)
        assert hi == (1, 2**53 + 1)

    def test_one_paisa_distinction_survives_the_sort_at_2_53(self) -> None:
        # ADVERSARIAL run_ids: the RICHER record carries the LATER run_id, so
        # a float-collapsed tie would flip the order to run_id ASC (poorer
        # first) — the int-exact key must keep the richer one first.
        poorer = _rec("a" * 64, net_after_cost_paisa=2**53)
        richer = _rec("b" * 64, net_after_cost_paisa=2**53 + 1)
        assert PRESETS["net_after_cost"].sort_records([poorer, richer]) == (richer, poorer)

    def test_negative_net_beats_unpriced_none(self) -> None:
        # None == honestly unpriced (no options-exec verdict) — it ranks
        # AFTER every priced entry, even a deeply NEGATIVE net; never coerced
        # to a fake 0 that would outrank a real loss.
        unpriced = _rec("a" * 64)  # defaulted None
        negative = _rec("b" * 64, net_after_cost_paisa=-1_000_000)
        assert PRESETS["net_after_cost"].sort_records([unpriced, negative]) == (
            negative,
            unpriced,
        )

    def test_net_tie_falls_to_run_id_asc(self) -> None:
        first = _rec("1" * 64, net_after_cost_paisa=500)
        second = _rec("2" * 64, net_after_cost_paisa=500)
        assert PRESETS["net_after_cost"].sort_records([second, first]) == (first, second)

    def test_locked_four_record_order(self) -> None:
        # Hand-derived (descending on the int-exact net, unpriced last):
        #   01: 2**53 + 1  → rank 1 (one paisa above 00 — must NOT tie)
        #   00: 2**53      → rank 2
        #   02: -100       → rank 3 (priced loss beats unpriced)
        #   03: None       → rank 4 (unpriced last)
        records = [
            _rec("00" + "0" * 62, net_after_cost_paisa=2**53),
            _rec("01" + "0" * 62, net_after_cost_paisa=2**53 + 1),
            _rec("02" + "0" * 62, net_after_cost_paisa=-100),
            _rec("03" + "0" * 62),  # defaulted None — unpriced
        ]
        ordered = PRESETS["net_after_cost"].sort_records(list(reversed(records)))
        assert [record.run_id[:2] for record in ordered] == ["01", "00", "02", "03"]
