"""Unit tests for :mod:`brutex.engine.period_stats` (DEC-SWEEP-TEMPORAL-ROBUSTNESS-001).

The canonical-JSON grammar + cross-sum validators behind the four optional
``ResultRow`` temporal columns: fabricated / torn / non-canonical stored cells
must be structurally unloadable, and the re-encode must be byte-deterministic.
(The finalist FOLD + segmented-dd walk tests live beside them — added with the
engine-wiring commit that ships the fold.)
"""

from __future__ import annotations

import pytest
from brutex.engine.period_stats import (
    PERIOD_CELL_LEN,
    decode_judged_years,
    decode_period_stats,
    encode_judged_years,
    encode_period_stats,
    month_key,
    quarter_key,
    quarter_of_month,
    validate_period_stats,
    year_key,
)

#: A hand-built CONSISTENT bucket set (sums mirror a 40-trade, −123450-paisa,
#: 18-win row): the shared fixture for the cross-sum validator tests.
_MONTHS = {
    "2021-01": (10, 50_000, 6, 20_000),
    "2021-02": (5, -30_000, 2, 35_000),
    "2021-04": (12, -160_000, 5, 180_000),
    "2022-11": (13, 16_550, 5, 40_000),
}
_QUARTERS = {
    "2021Q1": (15, 20_000, 8, 45_000),
    "2021Q2": (12, -160_000, 5, 180_000),
    "2022Q4": (13, 16_550, 5, 40_000),
}
_YEARS = {
    "2021": (27, -140_000, 13, 200_000),
    "2022": (13, 16_550, 5, 40_000),
}
_JUDGED = '["2020","2021","2022"]'
_TOTALS = {"total_trades": 40, "net_pnl_paisa": -123_450, "winning_trades": 18}


def _validate(**overrides: object) -> tuple[str, str, str, str]:
    kwargs: dict[str, object] = {
        "stats_by_year_json": encode_period_stats(_YEARS),
        "stats_by_quarter_json": encode_period_stats(_QUARTERS),
        "stats_by_month_json": encode_period_stats(_MONTHS),
        "temporal_judged_years_json": _JUDGED,
        **_TOTALS,
    }
    kwargs.update(overrides)
    return validate_period_stats(**kwargs)  # type: ignore[arg-type]


class TestKeyHelpers:
    def test_year_key(self) -> None:
        assert year_key(2021) == "2021"
        assert year_key(987) == "0987"  # zero-padded canonical form

    def test_quarter_key(self) -> None:
        assert quarter_key(2021, 3) == "2021Q3"

    def test_month_key(self) -> None:
        assert month_key(2021, 7) == "2021-07"

    def test_quarter_of_month(self) -> None:
        assert [quarter_of_month(m) for m in range(1, 13)] == [
            1,
            1,
            1,
            2,
            2,
            2,
            3,
            3,
            3,
            4,
            4,
            4,
        ]

    def test_cell_len_pinned(self) -> None:
        assert PERIOD_CELL_LEN == 4


class TestEncodeDecodeRoundTrip:
    def test_round_trip_is_exact(self) -> None:
        payload = encode_period_stats(_MONTHS)
        assert decode_period_stats(payload, kind="month") == _MONTHS

    def test_encode_is_canonical_and_byte_deterministic(self) -> None:
        """sort_keys + tight separators: insertion order never changes bytes."""
        a = encode_period_stats({"2021": (1, 5, 1, 0), "2020": (2, -3, 0, 9)})
        b = encode_period_stats({"2020": (2, -3, 0, 9), "2021": (1, 5, 1, 0)})
        assert a == b == '{"2020":[2,-3,0,9],"2021":[1,5,1,0]}'

    def test_empty_maps_encode_and_decode(self) -> None:
        assert encode_period_stats({}) == "{}"
        assert decode_period_stats("{}", kind="year") == {}


class TestDecodeStrictness:
    def test_non_json_rejected(self) -> None:
        with pytest.raises(ValueError):
            decode_period_stats("not json", kind="year")

    def test_non_object_rejected(self) -> None:
        with pytest.raises(ValueError, match="JSON object"):
            decode_period_stats("[1,2]", kind="year")

    def test_nesting_bomb_rejected_as_value_error(self) -> None:
        """Review round 1: a pathologically nested valid-JSON payload raises
        `RecursionError` inside `json.loads`, which is NOT a `ValueError` —
        unmapped it escaped `ResultsStore.load`'s typed-damage tuple raw
        (the G14 breach). The `_loads` shim folds it into `ValueError` (the
        sweep_checkpoint house precedent)."""
        with pytest.raises(ValueError, match="nesting bomb"):
            decode_period_stats("[" * 60_000, kind="year")

    @pytest.mark.parametrize(
        ("kind", "bad_key"),
        [
            ("year", "21"),
            ("year", "2021Q1"),
            ("quarter", "2021Q5"),
            ("quarter", "2021"),
            ("month", "2021-13"),
            ("month", "2021-00"),
            ("month", "2021-1"),
        ],
    )
    def test_non_canonical_keys_rejected(self, kind: str, bad_key: str) -> None:
        with pytest.raises(ValueError, match="not canonical"):
            decode_period_stats('{"' + bad_key + '":[1,1,1,0]}', kind=kind)

    def test_non_list_cell_rejected(self) -> None:
        with pytest.raises(ValueError, match="4-int array"):
            decode_period_stats('{"2021":{"n":1}}', kind="year")

    def test_wrong_arity_cell_rejected(self) -> None:
        with pytest.raises(ValueError, match="4-int array"):
            decode_period_stats('{"2021":[1,1,1]}', kind="year")

    def test_bool_cell_value_rejected(self) -> None:
        """JSON true parses to Python True — an int subclass, still refused."""
        with pytest.raises(ValueError, match="must be an int"):
            decode_period_stats('{"2021":[true,1,1,0]}', kind="year")

    def test_float_cell_value_rejected(self) -> None:
        with pytest.raises(ValueError, match="must be an int"):
            decode_period_stats('{"2021":[1,1.5,1,0]}', kind="year")

    def test_out_of_int64_rejected(self) -> None:
        with pytest.raises(ValueError, match="int64 range"):
            decode_period_stats(f'{{"2021":[1,{2**63},1,0]}}', kind="year")

    def test_negative_n_rejected(self) -> None:
        with pytest.raises(ValueError, match=">= 0"):
            decode_period_stats('{"2021":[-1,0,0,0]}', kind="year")

    def test_negative_wins_rejected(self) -> None:
        with pytest.raises(ValueError, match=">= 0"):
            decode_period_stats('{"2021":[1,0,-1,0]}', kind="year")

    def test_negative_dd_rejected(self) -> None:
        with pytest.raises(ValueError, match=">= 0"):
            decode_period_stats('{"2021":[1,0,0,-1]}', kind="year")

    def test_wins_beyond_n_rejected(self) -> None:
        with pytest.raises(ValueError, match="exceeds n_trades"):
            decode_period_stats('{"2021":[1,0,2,0]}', kind="year")

    def test_negative_pnl_is_legal(self) -> None:
        assert decode_period_stats('{"2021":[1,-500,0,0]}', kind="year") == {
            "2021": (1, -500, 0, 0)
        }


class TestJudgedYearsColumn:
    def test_encode_sorts_and_dedups(self) -> None:
        assert encode_judged_years([2022, 2020, 2020]) == '["2020","2022"]'

    def test_round_trip(self) -> None:
        assert decode_judged_years(encode_judged_years(range(2020, 2023))) == (
            2020,
            2021,
            2022,
        )

    def test_empty_list_is_legal(self) -> None:
        """An all-sliver window judges nothing — the honest empty denominator."""
        assert decode_judged_years("[]") == ()

    def test_non_list_rejected(self) -> None:
        with pytest.raises(ValueError, match="JSON array"):
            decode_judged_years('{"2020":1}')

    def test_non_string_entry_rejected(self) -> None:
        with pytest.raises(ValueError, match="not canonical YYYY"):
            decode_judged_years("[2020]")

    def test_bad_grammar_rejected(self) -> None:
        with pytest.raises(ValueError, match="not canonical YYYY"):
            decode_judged_years('["20"]')

    def test_descending_rejected(self) -> None:
        with pytest.raises(ValueError, match="strictly ascending"):
            decode_judged_years('["2021","2020"]')

    def test_duplicate_rejected(self) -> None:
        with pytest.raises(ValueError, match="strictly ascending"):
            decode_judged_years('["2020","2020"]')

    def test_nesting_bomb_rejected_as_value_error(self) -> None:
        """The judged-years twin of the stats-column nesting-bomb mapping."""
        with pytest.raises(ValueError, match="nesting bomb"):
            decode_judged_years("[" * 60_000)


class TestCrossSumValidation:
    def test_consistent_set_passes_and_reencodes_canonically(self) -> None:
        year, quarter, month, judged = _validate()
        assert decode_period_stats(year, kind="year") == _YEARS
        assert decode_period_stats(quarter, kind="quarter") == _QUARTERS
        assert decode_period_stats(month, kind="month") == _MONTHS
        assert judged == _JUDGED

    def test_non_canonical_spelling_is_canonicalised(self) -> None:
        """Whitespace / key-order variants re-encode to ONE canonical string."""
        loose = '{ "2022": [13, 16550, 5, 40000] , "2021": [27, -140000, 13, 200000] }'
        year, _, _, _ = _validate(stats_by_year_json=loose)
        assert year == encode_period_stats(_YEARS)

    def test_month_totals_must_match_the_row(self) -> None:
        with pytest.raises(ValueError, match="month cells sum to"):
            _validate(total_trades=41)
        with pytest.raises(ValueError, match="month cells sum to"):
            _validate(net_pnl_paisa=0)
        with pytest.raises(ValueError, match="month cells sum to"):
            _validate(winning_trades=17)

    def test_tampered_year_cell_rejected(self) -> None:
        tampered = dict(_YEARS)
        tampered["2021"] = (27, -140_000 + 1, 13, 200_000)
        with pytest.raises(ValueError, match="does not equal the sum of its months"):
            _validate(stats_by_year_json=encode_period_stats(tampered))

    def test_tampered_quarter_cell_rejected(self) -> None:
        tampered = dict(_QUARTERS)
        tampered["2021Q1"] = (15, 20_000, 7, 45_000)
        with pytest.raises(ValueError, match="does not equal the sum of its months"):
            _validate(stats_by_quarter_json=encode_period_stats(tampered))

    def test_fabricated_extra_year_rejected(self) -> None:
        fabricated = dict(_YEARS)
        fabricated["2023"] = (0, 0, 0, 0)
        with pytest.raises(ValueError, match="do not match the month-derived"):
            _validate(stats_by_year_json=encode_period_stats(fabricated))

    def test_missing_quarter_bucket_rejected(self) -> None:
        partial = dict(_QUARTERS)
        del partial["2022Q4"]
        with pytest.raises(ValueError, match="do not match the month-derived"):
            _validate(stats_by_quarter_json=encode_period_stats(partial))

    def test_dd_cells_are_exempt_from_rollup(self) -> None:
        """max_dd does NOT roll up (module law): a year dd unlike any month
        combination is legal — only n/pnl/wins are cross-summed."""
        loose_dd = dict(_YEARS)
        loose_dd["2021"] = (27, -140_000, 13, 999_999)
        year, _, _, _ = _validate(stats_by_year_json=encode_period_stats(loose_dd))
        assert decode_period_stats(year, kind="year")["2021"][3] == 999_999

    def test_all_empty_zero_row_passes(self) -> None:
        year, quarter, month, judged = validate_period_stats(
            stats_by_year_json="{}",
            stats_by_quarter_json="{}",
            stats_by_month_json="{}",
            temporal_judged_years_json="[]",
            total_trades=0,
            net_pnl_paisa=0,
            winning_trades=0,
        )
        assert (year, quarter, month, judged) == ("{}", "{}", "{}", "[]")

    def test_unjudged_year_may_carry_buckets(self) -> None:
        """The judged list is a DENOMINATOR, not a bucket constraint: an
        unjudged sliver year's trades still store honestly."""
        _validate(temporal_judged_years_json='["2021"]')


# ── The finalist fold + segmented-dd walk (the producer side) ────────────────

from datetime import UTC, datetime, timedelta, timezone  # noqa: E402

from brutex.engine.period_stats import fold_period_stats  # noqa: E402
from brutex.metrics.types import ClosedTrade  # noqa: E402
from brutex.strategies.types import Side  # noqa: E402

_IST = timezone(timedelta(hours=5, minutes=30))


def _trade(pnl: int, exit_ist: datetime) -> ClosedTrade:
    """One closed trade exiting at the given IST instant (entry 1 bar before)."""
    exit_utc = exit_ist.replace(tzinfo=_IST).astimezone(UTC)
    return ClosedTrade(
        pnl_paisa=pnl,
        mfe_paisa=max(pnl, 0),
        mae_paisa=max(-pnl, 0),
        holding_bars=1,
        entry_ts=exit_utc - timedelta(minutes=5),
        exit_ts=exit_utc,
        direction=Side.BUY,
    )


class TestFoldPeriodStats:
    def test_empty_trades_yield_empty_maps(self) -> None:
        stats = fold_period_stats([], start_equity_paisa=10_000_000)
        assert stats.by_year == {}
        assert stats.by_quarter == {}
        assert stats.by_month == {}

    def test_fold_matches_hand_built_buckets(self) -> None:
        trades = [
            _trade(50_000, datetime(2021, 1, 5, 10, 0)),
            _trade(-30_000, datetime(2021, 2, 10, 11, 0)),
            _trade(-160_000, datetime(2021, 4, 20, 12, 0)),
            _trade(16_550, datetime(2022, 11, 3, 13, 0)),
        ]
        stats = fold_period_stats(trades, start_equity_paisa=10_000_000)
        assert stats.by_month == {
            "2021-01": (1, 50_000, 1, 0),
            "2021-02": (1, -30_000, 0, 30_000),
            "2021-04": (1, -160_000, 0, 160_000),
            "2022-11": (1, 16_550, 1, 0),
        }
        assert stats.by_quarter == {
            "2021Q1": (2, 20_000, 1, 30_000),
            "2021Q2": (1, -160_000, 0, 160_000),
            "2022Q4": (1, 16_550, 1, 0),
        }
        # Year dd: 2021 peak 10_050_000 → trough 9_860_000 = 190_000 — MORE
        # than any month's or quarter's dd (the walk carries the peak WITHIN
        # the year across its months).
        assert stats.by_year == {
            "2021": (3, -140_000, 1, 190_000),
            "2022": (1, 16_550, 1, 0),
        }

    def test_year_dd_exceeds_every_month_dd(self) -> None:
        """The non-rollup counter-example (design Q3): two consecutive losing
        months each dd 100, the YEAR dd is 200 — neither max nor sum of month
        dds reproduces it in general."""
        trades = [
            _trade(-100, datetime(2021, 11, 3, 10, 0)),
            _trade(-100, datetime(2021, 12, 3, 10, 0)),
        ]
        stats = fold_period_stats(trades, start_equity_paisa=1_000)
        assert stats.by_month["2021-11"][3] == 100
        assert stats.by_month["2021-12"][3] == 100
        assert stats.by_year["2021"][3] == 200

    def test_boundary_spanning_drawdown_is_split(self) -> None:
        """The peak RESETS at each period start: a Dec→Jan drawdown of 200
        reads 100 in each year — systematically UNDERSTATED per-period (the
        row's global max_drawdown_paisa stays the risk authority)."""
        trades = [
            _trade(-100, datetime(2021, 12, 20, 10, 0)),
            _trade(-100, datetime(2022, 1, 10, 10, 0)),
        ]
        stats = fold_period_stats(trades, start_equity_paisa=1_000)
        assert stats.by_year == {
            "2021": (1, -100, 0, 100),
            "2022": (1, -100, 0, 100),
        }

    def test_recovery_inside_a_period_moves_the_peak(self) -> None:
        """Standard dd walk within one month: peak follows new highs."""
        trades = [
            _trade(-50, datetime(2021, 6, 1, 10, 0)),
            _trade(200, datetime(2021, 6, 2, 10, 0)),  # new peak 1_150
            _trade(-80, datetime(2021, 6, 3, 10, 0)),  # dd 80 from the new peak
        ]
        stats = fold_period_stats(trades, start_equity_paisa=1_000)
        assert stats.by_month["2021-06"] == (3, 70, 1, 80)

    def test_input_order_is_irrelevant(self) -> None:
        """The fold re-sorts by exit_ts (stable) — deterministic regardless."""
        trades = [
            _trade(-100, datetime(2021, 12, 20, 10, 0)),
            _trade(300, datetime(2021, 3, 1, 10, 0)),
            _trade(-100, datetime(2022, 1, 10, 10, 0)),
        ]
        forward = fold_period_stats(trades, start_equity_paisa=1_000)
        backward = fold_period_stats(list(reversed(trades)), start_equity_paisa=1_000)
        assert forward == backward

    def test_dd_is_invariant_to_the_equity_base(self) -> None:
        """peak − equity subtracts the base away — threaded for basis-law
        fidelity, never a tunable (docstring law)."""
        trades = [
            _trade(-100, datetime(2021, 12, 20, 10, 0)),
            _trade(40, datetime(2022, 1, 10, 10, 0)),
        ]
        a = fold_period_stats(trades, start_equity_paisa=1_000)
        b = fold_period_stats(trades, start_equity_paisa=10_000_000)
        assert a == b

    def test_exit_attribution_books_to_the_exit_period(self) -> None:
        """A trade entered in June exiting in July belongs WHOLLY to July."""
        exit_utc = datetime(2021, 7, 1, 10, 0, tzinfo=_IST).astimezone(UTC)
        trade = ClosedTrade(
            pnl_paisa=500,
            mfe_paisa=500,
            mae_paisa=0,
            holding_bars=100,
            entry_ts=datetime(2021, 6, 28, 10, 0, tzinfo=_IST).astimezone(UTC),
            exit_ts=exit_utc,
            direction=Side.BUY,
        )
        stats = fold_period_stats([trade], start_equity_paisa=1_000)
        assert set(stats.by_month) == {"2021-07"}

    def test_ist_boundary_attribution(self) -> None:
        """A 2021-12-31 23:00 IST exit is 17:30 UTC the same IST day — the
        IST date (not the UTC date) picks the bucket."""
        stats = fold_period_stats(
            [_trade(10, datetime(2022, 1, 1, 1, 0))],  # 01:00 IST Jan 1 = Dec 31 UTC
            start_equity_paisa=1_000,
        )
        assert set(stats.by_year) == {"2022"}

    def test_fold_output_always_validates(self) -> None:
        """Cross-sum consistency BY CONSTRUCTION: encoding the fold's maps
        passes the store validator against the same trades' totals."""
        trades = [
            _trade(50_000, datetime(2021, 1, 5, 10, 0)),
            _trade(-30_000, datetime(2021, 2, 10, 11, 0)),
            _trade(0, datetime(2021, 3, 1, 11, 0)),  # scratch: neither win nor loss
            _trade(16_550, datetime(2022, 11, 3, 13, 0)),
        ]
        stats = fold_period_stats(trades, start_equity_paisa=10_000_000)
        validate_period_stats(
            stats_by_year_json=encode_period_stats(stats.by_year),
            stats_by_quarter_json=encode_period_stats(stats.by_quarter),
            stats_by_month_json=encode_period_stats(stats.by_month),
            temporal_judged_years_json=encode_judged_years([2021, 2022]),
            total_trades=len(trades),
            net_pnl_paisa=sum(t.pnl_paisa for t in trades),
            winning_trades=sum(1 for t in trades if t.pnl_paisa > 0),
        )
