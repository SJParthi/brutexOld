"""Unit suite for :mod:`brutex.engine.resilience_streaks` — the fold + record.

Hand-built ``_TradeLike`` lists exercise every metric + every honesty edge
case (breakeven resets, 0-loss → None percentiles, still-underwater, parity-
fail → full NULL, ≤0 start-equity → None %, single-year → None spread,
worst-by-Σ ≠ longest-by-count, the IST-date TZ tooth). No parquet here (that
is ``test_resilience_sidecar``); no mocks of the module under test.
"""

# pyright: reportPrivateUsage=none
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta, timezone

import pytest
from brutex.engine.combo_aggregates import _consecutive_runs
from brutex.engine.resilience_streaks import (
    RESILIENCE_COLUMNS,
    ResilienceError,
    ResilienceStreaks,
    _nearest_rank_from_hist,
    _worst_bucket,
    fold_resilience,
    losing_run_length_hist,
    project_resilience_annotation,
    render_resilience_line,
    resilience_null,
    resilience_payload,
)

_IST = timezone(timedelta(hours=5, minutes=30))
_START = 1_000_000  # ₹10,000 per-unit account base


@dataclass(frozen=True, slots=True)
class _FakeTrade:
    pnl_paisa: int
    entry_ts: datetime
    exit_ts: datetime


def _t(pnl: int, year: int, month: int, day: int, hour: int = 10, minute: int = 0) -> _FakeTrade:
    """A trade whose IST session (year, month, day) is set explicitly."""
    entry = datetime(year, month, day, hour, minute, tzinfo=_IST).astimezone(UTC)
    exit_ = (
        datetime(year, month, day, hour, minute, tzinfo=_IST) + timedelta(minutes=1)
    ).astimezone(UTC)
    return _FakeTrade(pnl_paisa=pnl, entry_ts=entry, exit_ts=exit_)


def _fold(
    trades: list[_FakeTrade], *, start: int = _START, parity_ok: bool = True
) -> ResilienceStreaks:
    return fold_resilience(
        "run", trades, parity_ok=parity_ok, start_equity_paisa=start, fingerprint="fp"
    )


# ── streak distribution + count ───────────────────────────────────────────────


class TestStreakDistribution:
    def test_two_runs_hist_p50_p95(self) -> None:
        trades = [
            _t(-100, 2024, 1, 1),
            _t(-100, 2024, 1, 2),
            _t(200, 2024, 1, 3),
            _t(-50, 2024, 1, 4),
        ]
        r = _fold(trades)
        assert losing_run_length_hist([t.pnl_paisa for t in trades]) == {2: 1, 1: 1}
        assert r.losing_streak_count == 2
        assert r.losing_streak_p50 == 1
        assert r.losing_streak_p95 == 2

    def test_scratch_breaks_the_run(self) -> None:
        # [-1, 0, -1] → the scratch ends the first run → two runs of length 1.
        r = _fold([_t(-100, 2024, 1, 1), _t(0, 2024, 1, 2), _t(-100, 2024, 1, 3)])
        assert r.losing_streak_count == 2
        assert losing_run_length_hist([-1, 0, -1]) == {1: 2}

    def test_trailing_open_run_is_flushed(self) -> None:
        # The final trade is a loss with no following non-loss to trigger flush.
        r = _fold([_t(200, 2024, 1, 1), _t(-100, 2024, 1, 2), _t(-100, 2024, 1, 3)])
        assert r.losing_streak_count == 1
        assert r.worst_streak_len == 2

    def test_zero_loss_live_reports_none_percentiles(self) -> None:
        r = _fold([_t(200, 2024, 1, 1), _t(300, 2024, 1, 2)])
        assert r.stats_live is True
        assert r.losing_streak_count == 0
        assert r.losing_streak_p50 is None
        assert r.losing_streak_p95 is None
        assert r.worst_streak_pnl_paisa is None
        assert r.losses_by_month_json == "{}"
        assert r.longest_underwater_trades == 0
        assert r.longest_underwater_days == 0
        assert r.still_underwater_at_end is False


# ── worst consecutive-loss run ────────────────────────────────────────────────


class TestWorstStreak:
    def test_worst_by_sum_is_distinct_from_longest_by_count(self) -> None:
        # A short DEEP run (-1000 over 2) vs a long SHALLOW run (-40 over 4).
        trades = [
            _t(-500, 2024, 1, 1),
            _t(-500, 2024, 1, 2),
            _t(300, 2024, 1, 3),
            _t(-10, 2024, 1, 4),
            _t(-10, 2024, 1, 5),
            _t(-10, 2024, 1, 6),
            _t(-10, 2024, 1, 7),
        ]
        r = _fold(trades)
        assert r.worst_streak_pnl_paisa == -1000
        assert r.worst_streak_len == 2  # the deep run, NOT the len-4 run
        assert _consecutive_runs([t.pnl_paisa for t in trades])[0] == 4

    def test_tie_keeps_the_earlier_run(self) -> None:
        # Two runs each summing -200; strict '<' keeps the FIRST.
        trades = [
            _t(-100, 2024, 1, 1),
            _t(-100, 2024, 1, 2),
            _t(300, 2024, 1, 3),
            _t(-100, 2024, 1, 5),
            _t(-100, 2024, 1, 6),
        ]
        r = _fold(trades)
        assert r.worst_streak_pnl_paisa == -200
        # start ts is the first run's first trade entry.
        first_entry_us = (
            datetime(2024, 1, 1, 10, 0, tzinfo=_IST).astimezone(UTC)
            - datetime(1970, 1, 1, tzinfo=UTC)
        ) // timedelta(microseconds=1)
        assert r.worst_streak_start_ts_us == first_entry_us

    def test_worst_streak_start_end_timestamps(self) -> None:
        r = _fold([_t(-100, 2024, 3, 4), _t(-50, 2024, 3, 5), _t(200, 2024, 3, 6)])
        assert r.worst_streak_len == 2
        assert r.worst_streak_start_ts_us is not None
        assert r.worst_streak_end_ts_us is not None
        assert r.worst_streak_end_ts_us > r.worst_streak_start_ts_us

    def test_non_positive_start_equity_yields_none_pct_but_keeps_paisa(self) -> None:
        r = _fold([_t(-100, 2024, 1, 1), _t(-100, 2024, 1, 2)], start=0)
        assert r.worst_streak_pnl_paisa == -200
        assert r.worst_streak_pnl_pct is None

    def test_worst_pct_is_full_precision(self) -> None:
        r = _fold([_t(-100, 2024, 1, 1)], start=1_000_000)
        assert r.worst_streak_pnl_pct == pytest.approx(-0.01)


# ── underwater (trades + calendar days) ───────────────────────────────────────


class TestUnderwater:
    def test_recovers_mid_sequence(self) -> None:
        # down, down, up past peak → recovered.
        r = _fold([_t(-100, 2024, 1, 1), _t(-100, 2024, 1, 2), _t(300, 2024, 1, 3)])
        assert r.longest_underwater_trades == 2
        assert r.still_underwater_at_end is False

    def test_still_underwater_at_end_is_a_lower_bound(self) -> None:
        r = _fold([_t(-100, 2024, 1, 1), _t(-100, 2024, 1, 2)])
        assert r.still_underwater_at_end is True
        assert r.longest_underwater_trades == 2

    def test_never_underwater(self) -> None:
        r = _fold([_t(100, 2024, 1, 1), _t(100, 2024, 1, 2)])
        assert r.longest_underwater_trades == 0
        assert r.longest_underwater_days == 0
        assert r.still_underwater_at_end is False

    def test_independent_maxima_many_trades_one_day_vs_few_trades_many_days(self) -> None:
        # Episode A: 3 losing trades in ONE session (3 trades, 0 days) then recover.
        # Episode B: 2 losses spanning a 60-day calendar gap, never recovering.
        trades = [
            _t(-100, 2024, 1, 1),
            _t(-100, 2024, 1, 1),
            _t(-100, 2024, 1, 1),
            _t(400, 2024, 1, 1),  # recovers PAST the old peak → resets both episodes
            _t(-1, 2024, 1, 2),
            _t(-1, 2024, 3, 1),  # 60 IST days after the 2024-01-01 peak (leap year)
        ]
        r = _fold(trades)
        assert r.longest_underwater_trades == 3  # from episode A
        assert r.longest_underwater_days == 60  # from episode B — a DIFFERENT episode
        assert r.still_underwater_at_end is True

    def test_days_span_across_a_calendar_gap(self) -> None:
        r = _fold([_t(-100, 2024, 1, 1), _t(-100, 2024, 1, 31)])
        assert r.longest_underwater_days == 30
        assert r.still_underwater_at_end is True

    def test_scratch_neither_sets_peak_nor_deepens(self) -> None:
        # After a loss, a scratch keeps equity flat: still underwater, no deepen.
        r = _fold([_t(-100, 2024, 1, 1), _t(0, 2024, 1, 2)])
        assert r.still_underwater_at_end is True
        assert r.longest_underwater_trades == 2

    def test_first_trade_loss_does_not_crash(self) -> None:
        # peak_exit_date is anchored at trades[0].exit_ts even when trade 0 loses.
        r = _fold([_t(-100, 2024, 1, 1)])
        assert r.longest_underwater_trades == 1
        assert r.longest_underwater_days == 0


# ── loss clustering ───────────────────────────────────────────────────────────


class TestClustering:
    def test_worst_month_by_count_tie_earliest(self) -> None:
        trades = [
            _t(-1, 2024, 1, 1),
            _t(-1, 2024, 1, 2),
            _t(-1, 2024, 2, 1),
            _t(-1, 2024, 2, 2),
        ]
        r = _fold(trades)
        # 2 losers each month → tie → the EARLIEST key wins.
        assert r.worst_loss_month == "2024-01"
        assert r.worst_loss_month_count == 2

    def test_iso_week_twin(self) -> None:
        r = _fold([_t(-1, 2024, 1, 1), _t(-1, 2024, 1, 2), _t(-1, 2024, 1, 3)])
        # 2024-01-01 is ISO week 2024-W01.
        assert r.worst_loss_week == "2024-W01"
        assert r.worst_loss_week_count == 3

    def test_cross_midnight_ist_buckets_by_ist_exit_date(self) -> None:
        # A UTC instant late on 2024-01-31 is IST 2024-02-01 → February bucket.
        entry = datetime(2024, 1, 31, 19, 0, tzinfo=UTC)  # IST 2024-02-01 00:30
        exit_ = datetime(2024, 1, 31, 19, 1, tzinfo=UTC)
        r = _fold([_FakeTrade(-100, entry, exit_)])
        assert r.worst_loss_month == "2024-02"

    def test_month_losers_sum_equals_loss_count(self) -> None:
        import json

        trades = [_t(-1, 2024, 1, d) for d in (1, 2, 3)] + [_t(5, 2024, 1, 4)]
        r = _fold(trades)
        by_month = json.loads(r.losses_by_month_json)
        assert sum(by_month.values()) == sum(1 for t in trades if t.pnl_paisa < 0) == 3


# ── win-rate stability across years ───────────────────────────────────────────


class TestWinRateStability:
    def test_by_year_wins_total(self) -> None:
        import json

        trades = [_t(10, 2023, 1, 1), _t(-5, 2023, 1, 2), _t(10, 2024, 1, 1)]
        r = _fold(trades)
        assert json.loads(r.win_rate_by_year_json) == {"2023": [1, 2], "2024": [1, 1]}

    def test_spread_none_with_fewer_than_two_qualifying_years(self) -> None:
        # Year 2024 has 20 trades (qualifies); 2023 has 5 (below YEAR_MIN_TRADES).
        trades = [_t(10, 2023, 1, 1) for _ in range(5)]
        trades += [_t(10 if i % 2 else -10, 2024, 1, 1) for i in range(20)]
        r = _fold(trades)
        assert r.win_rate_year_spread is None

    def test_spread_is_max_minus_min_over_qualifying_years(self) -> None:
        # 2022: 20 trades, 10 wins → 0.5; 2023: 20 trades, 5 wins → 0.25;
        # 2024: 20 trades, 20 wins → 1.0. spread = 1.0 - 0.25 = 0.75.
        trades: list[_FakeTrade] = []
        trades += [_t(10 if i < 10 else -10, 2022, 1, 1) for i in range(20)]
        trades += [_t(10 if i < 5 else -10, 2023, 1, 1) for i in range(20)]
        trades += [_t(10, 2024, 1, 1) for _ in range(20)]
        r = _fold(trades)
        assert r.win_rate_year_spread == pytest.approx(0.75)

    def test_scratches_count_in_the_denominator(self) -> None:
        import json

        trades = [_t(10, 2024, 1, 1), _t(0, 2024, 1, 2), _t(-5, 2024, 1, 3)]
        r = _fold(trades)
        assert json.loads(r.win_rate_by_year_json) == {"2024": [1, 3]}


# ── NULL record ───────────────────────────────────────────────────────────────


class TestNullRecord:
    def test_parity_failed_is_full_null(self) -> None:
        r = _fold([_t(-1, 2024, 1, 1), _t(5, 2024, 1, 2)], parity_ok=False)
        assert r.stats_live is False
        assert r.n_trades == 2
        assert r.losing_streak_p50 is None
        assert r.losing_streak_count is None
        assert r.still_underwater_at_end is None
        assert r.losses_by_month_json == "{}"
        assert r.win_rate_by_year_json == "{}"

    def test_zero_trades_is_full_null(self) -> None:
        r = _fold([])
        assert r.stats_live is False
        assert r.n_trades == 0

    def test_null_helper_shape(self) -> None:
        n = resilience_null("rid", 7, "fp2")
        assert n.stats_live is False
        assert n.n_trades == 7
        assert n.fingerprint == "fp2"
        assert n.still_underwater_at_end is None

    def test_to_record_keys_match_columns(self) -> None:
        r = _fold([_t(-1, 2024, 1, 1), _t(5, 2024, 1, 2)])
        assert set(r.to_record()) == frozenset(name for name, _ in RESILIENCE_COLUMNS)
        assert set(resilience_null("r", 0, "f").to_record()) == frozenset(
            name for name, _ in RESILIENCE_COLUMNS
        )


# ── __post_init__ invariants ──────────────────────────────────────────────────


class TestInvariants:
    def test_empty_run_id_rejected(self) -> None:
        with pytest.raises(ResilienceError, match="run_id must be non-empty"):
            replace(_fold([_t(-1, 2024, 1, 1)]), run_id="")

    def test_negative_n_trades_rejected(self) -> None:
        with pytest.raises(ResilienceError, match="n_trades"):
            replace(_fold([_t(-1, 2024, 1, 1)]), n_trades=-1)

    def test_positive_worst_pnl_rejected(self) -> None:
        with pytest.raises(ResilienceError, match="worst_streak_pnl_paisa"):
            replace(_fold([_t(-1, 2024, 1, 1)]), worst_streak_pnl_paisa=1)

    def test_zero_worst_len_rejected(self) -> None:
        with pytest.raises(ResilienceError, match="worst_streak_len"):
            replace(_fold([_t(-1, 2024, 1, 1)]), worst_streak_len=0)

    def test_negative_uw_trades_rejected(self) -> None:
        with pytest.raises(ResilienceError, match="longest_underwater_trades"):
            replace(_fold([_t(-1, 2024, 1, 1)]), longest_underwater_trades=-1)

    def test_negative_uw_days_rejected(self) -> None:
        with pytest.raises(ResilienceError, match="longest_underwater_days"):
            replace(_fold([_t(-1, 2024, 1, 1)]), longest_underwater_days=-1)

    def test_zero_worst_month_count_rejected(self) -> None:
        with pytest.raises(ResilienceError, match="worst_loss_month_count"):
            replace(_fold([_t(-1, 2024, 1, 1)]), worst_loss_month_count=0)

    def test_zero_worst_week_count_rejected(self) -> None:
        with pytest.raises(ResilienceError, match="worst_loss_week_count"):
            replace(_fold([_t(-1, 2024, 1, 1)]), worst_loss_week_count=0)

    def test_negative_losing_streak_count_rejected(self) -> None:
        with pytest.raises(ResilienceError, match="losing_streak_count"):
            replace(_fold([_t(-1, 2024, 1, 1)]), losing_streak_count=-1)

    def test_null_consistency_scalar_rejected(self) -> None:
        null = resilience_null("r", 3, "f")
        with pytest.raises(ResilienceError, match="every nullable field None"):
            replace(null, losing_streak_p50=1)

    def test_null_consistency_json_rejected(self) -> None:
        null = resilience_null("r", 3, "f")
        with pytest.raises(ResilienceError, match='both jsons == "{}"'):
            replace(null, losses_by_month_json='{"2024-01":1}')


# ── nearest-rank helper guards ────────────────────────────────────────────────


class TestNearestRankHelper:
    def test_empty_histogram_raises(self) -> None:
        with pytest.raises(ResilienceError, match="non-empty"):
            _nearest_rank_from_hist({}, 50)

    def test_out_of_range_pct_raises(self) -> None:
        with pytest.raises(ResilienceError, match=r"pct must be in \[1, 100\]"):
            _nearest_rank_from_hist({1: 1}, 0)

    def test_worst_bucket_tie_earliest(self) -> None:
        assert _worst_bucket({"b": 2, "a": 2, "c": 1}) == ("a", 2)

    def test_hist_all_wins_is_empty(self) -> None:
        assert losing_run_length_hist([5, 3]) == {}

    def test_hist_trailing_and_interior_runs(self) -> None:
        assert losing_run_length_hist([5, -1, -1, 3, -1]) == {2: 1, 1: 1}


# ── payload + CLI line ────────────────────────────────────────────────────────


class TestPayloadAndLine:
    def test_payload_live_shape(self) -> None:
        r = _fold([_t(-100, 2024, 1, 1), _t(200, 2024, 1, 2)])
        pay = resilience_payload(r)
        assert pay["stats_live"] is True
        assert pay["worst_streak_pnl_paisa"] == r.worst_streak_pnl_paisa
        assert isinstance(pay["worst_streak_start"], str)
        assert pay["win_rate_by_year"] == {"2024": [1, 2]}
        assert pay["losses_by_month"] == {"2024-01": 1}

    def test_payload_none_record_is_all_none(self) -> None:
        pay = resilience_payload(None)
        assert pay["stats_live"] is False
        assert pay["n_trades"] is None
        assert pay["worst_streak_pnl_pct"] is None
        assert pay["win_rate_by_year"] == {}
        assert pay["fingerprint"] is None

    def test_payload_null_record_keeps_n_trades(self) -> None:
        pay = resilience_payload(resilience_null("r", 9, "fp"))
        assert pay["stats_live"] is False
        assert pay["n_trades"] == 9
        assert pay["worst_streak_start"] is None

    def test_absent_payload_every_display_field_is_none_never_zero(self) -> None:
        """FIX B (mutation-killer): the absent payload's EVERY nullable display
        field is ``None`` (never a fabricated 0) — a None→0 drift on any of
        these renders a lie on the ungated /screener + /operator surfaces.
        """
        pay = resilience_payload(None)  # the absent (record is None) payload
        for key in (
            "n_trades",
            "losing_streak_p50",
            "losing_streak_p95",
            "losing_streak_count",
            "worst_streak_pnl_paisa",
            "worst_streak_pnl_pct",
            "worst_streak_len",
            "worst_streak_start",
            "worst_streak_end",
            "longest_underwater_trades",
            "longest_underwater_days",
            "still_underwater_at_end",
            "worst_loss_month",
            "worst_loss_month_count",
            "worst_loss_week",
            "worst_loss_week_count",
            "win_rate_year_spread",
            "fingerprint",
        ):
            assert pay[key] is None, key
        assert pay["stats_live"] is False
        assert pay["win_rate_by_year"] == {}
        assert pay["losses_by_month"] == {}

    def test_absent_payload_is_single_sourced_from_null_record(self) -> None:
        """FIX B: the absent payload is DERIVED from ``resilience_null`` — every
        key matches the NULL-record payload except ``n_trades``/``fingerprint``
        (which an absent record cannot know → ``None``). Pins the derivation so
        no hand-literal can drift a field back in.
        """
        absent = resilience_payload(None)
        null_pay = resilience_payload(resilience_null("x", 0, "fp"))
        # n_trades / fingerprint are the only two deliberate divergences.
        assert absent["n_trades"] is None
        assert null_pay["n_trades"] == 0
        assert absent["fingerprint"] is None
        assert null_pay["fingerprint"] == "fp"
        for key in absent:
            if key in ("n_trades", "fingerprint"):
                continue
            assert absent[key] == null_pay[key], key

    def test_line_live_has_badge_and_money(self) -> None:
        r = _fold([_t(-100, 2024, 1, 1), _t(-100, 2024, 1, 2)])
        entry = {
            "run_id": "a" * 20,
            "metrics": {"max_consecutive_losses": 2},
            "resilience": resilience_payload(r),
        }
        line = render_resilience_line(entry)
        assert "STILL UNDERWATER" in line
        assert "₹" in line
        assert "longest streak 2 by count" in line

    def test_line_pairs_worst_by_rupees_length_with_its_own_cost(self) -> None:
        """FIX A (fails-before): when the LONGEST run (by count) is NOT the
        WORST run (by ₹), the line must show the worst-by-₹ run's OWN length
        beside its cost, and the longest-by-count separately — never conflate.
        """
        # A short DEEP run (2 × -1000, Σ-2000) then a long SHALLOW run
        # (4 × -10, Σ-40): longest-by-count = 4, worst-by-₹ = the len-2 run.
        trades = [
            _t(-1000, 2024, 1, 1),
            _t(-1000, 2024, 1, 2),
            _t(5000, 2024, 1, 3),
            _t(-10, 2024, 1, 4),
            _t(-10, 2024, 1, 5),
            _t(-10, 2024, 1, 6),
            _t(-10, 2024, 1, 7),
        ]
        r = _fold(trades)
        assert r.worst_streak_pnl_paisa == -2000  # the DEEP run
        assert r.worst_streak_len == 2  # ...whose length is 2
        entry = {
            "run_id": "e" * 20,
            "metrics": {"max_consecutive_losses": 4},  # the LONG shallow run
            "resilience": resilience_payload(r),
        }
        line = render_resilience_line(entry)
        # The longest-by-count (4) is labelled distinctly...
        assert "longest streak 4 by count" in line
        # ...and the worst-by-₹ phrase pairs the DEEP run's OWN length (2) with
        # its cost — the old conflated "worst-streak 4 ... cost ₹-20.00" wording
        # (4 juxtaposed with the len-2 run's ₹) must be gone.
        assert "worst streak (by ₹) 2 losses cost ₹-20.00" in line
        assert "worst-streak 4" not in line

    def test_line_recovered_badge(self) -> None:
        r = _fold([_t(-100, 2024, 1, 1), _t(300, 2024, 1, 2)])
        entry = {
            "run_id": "b" * 20,
            "metrics": {"max_consecutive_losses": 1},
            "resilience": resilience_payload(r),
        }
        assert "recovered" in render_resilience_line(entry)

    def test_line_null_record(self) -> None:
        entry = {
            "run_id": "c" * 20,
            "metrics": {"max_consecutive_losses": 0},
            "resilience": resilience_payload(None),
        }
        assert (
            render_resilience_line(entry)
            == f"  resilience {'c' * 12}: — (parity-failed or no trades)"
        )

    def test_line_zero_loss_dashes(self) -> None:
        r = _fold([_t(200, 2024, 1, 1), _t(300, 2024, 1, 2)])
        entry = {
            "run_id": "d" * 20,
            "metrics": {"max_consecutive_losses": 0},
            "resilience": resilience_payload(r),
        }
        line = render_resilience_line(entry)
        assert "typical — · P95 —" in line
        assert "cost — (—)" in line

    def test_line_fail_loud_on_non_mapping(self) -> None:
        with pytest.raises(ResilienceError, match="must be objects"):
            render_resilience_line({"run_id": "x", "metrics": "bad", "resilience": {}})

    def test_line_fail_loud_on_missing_key(self) -> None:
        with pytest.raises(KeyError):
            render_resilience_line({"run_id": "x", "resilience": {"stats_live": False}})


# ── annotation ────────────────────────────────────────────────────────────────


class TestAnnotation:
    def test_additive_only(self) -> None:
        r = _fold([_t(-1, 2024, 1, 1)])
        entry = {"rank": 1, "run_id": "r"}
        out = project_resilience_annotation(entry, r)
        assert {k: out[k] for k in entry} == entry
        assert entry == {"rank": 1, "run_id": "r"}  # unmutated
        assert "resilience" in out

    def test_double_annotation_refused(self) -> None:
        with pytest.raises(ValueError, match="refusing to overwrite"):
            project_resilience_annotation({"resilience": {}}, None)
