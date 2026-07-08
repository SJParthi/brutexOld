"""Unit tests for :mod:`brutex.data.window_ledger` (DEC-WINDOW-LEDGER-001).

100% line+branch on the pure model: `plan` insert + idempotent no-op; the single
`is_skippable` rule for every status; `record` classification incl. the
EMPTY→SKIPPED promotion boundary; `settle_known_gap`; attempt accumulation;
`first_seen_at` set-once; the `to_rows`/`from_rows` round-trip fixed point; and
thread-safety under a concurrent fan-out.
"""

from __future__ import annotations

import threading
from datetime import UTC, date, datetime

from brutex.data.fetch_coverage import WindowKey
from brutex.data.window_ledger import (
    DEMOTED_SETTLE_REASONS,
    EMPTY,
    EMPTY_TERMINAL_THRESHOLD,
    FAILED,
    PLANNED,
    REOPENED_MARKER,
    SETTLED_STATUSES,
    SKIPPED,
    SUCCESS,
    LedgerProgress,
    WindowLedger,
    expired_window_settle_reason,
    is_expired_window_settle_reason,
)

_NOW = datetime(2026, 6, 8, 12, 0, tzinfo=UTC)
_LATER = datetime(2026, 6, 9, 12, 0, tzinfo=UTC)


def _key(security_id: str = "NIFTY", interval: str = "1minute") -> WindowKey:
    return WindowKey(
        market="dhan",
        security_id=security_id,
        interval=interval,
        start=date(2021, 1, 1),
        end=date(2021, 4, 1),
    )


class TestPlan:
    """`plan` upserts PLANNED once and never resets a settled row."""

    def test_plan_inserts_planned(self) -> None:
        ledger = WindowLedger()
        ledger.plan(_key(), now=_NOW)
        record = ledger.records()[0]
        assert record.status == PLANNED
        assert record.attempts == 0
        assert record.bars == 0
        assert record.first_seen_at == _NOW.isoformat()
        assert record.last_attempt_at is None
        assert ledger.count == 1

    def test_plan_is_idempotent_noop_on_existing(self) -> None:
        ledger = WindowLedger()
        ledger.plan(_key(), now=_NOW)
        # A second plan (e.g. next pull) must NOT reset the row or its timestamp.
        ledger.plan(_key(), now=_LATER)
        record = ledger.records()[0]
        assert record.first_seen_at == _NOW.isoformat()
        assert ledger.count == 1

    def test_plan_does_not_reset_a_success_row(self) -> None:
        ledger = WindowLedger()
        ledger.record(_key(), raw_bars=10, error_type=None, bars_appended=10, now=_NOW)
        ledger.plan(_key(), now=_LATER)  # re-pull plan
        record = ledger.records()[0]
        assert record.status == "SUCCESS"  # untouched
        assert record.bars == 10


class TestIsSkippable:
    """The single skip rule — SUCCESS / SKIPPED skippable; everything else fetched."""

    def test_missing_window_is_not_skippable(self) -> None:
        assert WindowLedger().is_skippable(_key()) is False

    def test_success_is_skippable(self) -> None:
        ledger = WindowLedger()
        ledger.record(_key(), raw_bars=5, error_type=None, bars_appended=5, now=_NOW)
        assert ledger.is_skippable(_key()) is True

    def test_skipped_is_skippable(self) -> None:
        ledger = WindowLedger()
        ledger.settle_known_gap(_key(), now=_NOW)
        assert ledger.is_skippable(_key()) is True

    def test_planned_is_not_skippable(self) -> None:
        ledger = WindowLedger()
        ledger.plan(_key(), now=_NOW)
        assert ledger.is_skippable(_key()) is False

    def test_failed_is_not_skippable(self) -> None:
        ledger = WindowLedger()
        ledger.record(
            _key(), raw_bars=0, error_type="DhanRateLimitError", bars_appended=0, now=_NOW
        )
        assert ledger.is_skippable(_key()) is False

    def test_empty_below_threshold_is_not_skippable(self) -> None:
        ledger = WindowLedger()
        ledger.record(_key(), raw_bars=0, error_type=None, bars_appended=0, now=_NOW)
        assert ledger.is_skippable(_key()) is False


class TestRecord:
    """`record` classification + the EMPTY→SKIPPED promotion + attempt history."""

    def test_success_records_bars(self) -> None:
        ledger = WindowLedger()
        status = ledger.record(_key(), raw_bars=7, error_type=None, bars_appended=7, now=_NOW)
        assert status == "SUCCESS"
        record = ledger.records()[0]
        assert record.bars == 7
        assert record.attempts == 1
        assert record.last_error is None
        assert record.last_attempt_at == _NOW.isoformat()

    def test_failed_carries_error(self) -> None:
        ledger = WindowLedger()
        status = ledger.record(
            _key(), raw_bars=0, error_type="DhanTimeoutError", bars_appended=0, now=_NOW
        )
        assert status == "FAILED"
        assert ledger.records()[0].last_error == "DhanTimeoutError"

    def test_empty_below_threshold_stays_empty(self) -> None:
        ledger = WindowLedger()
        status = ledger.record(_key(), raw_bars=0, error_type=None, bars_appended=0, now=_NOW)
        assert status == "EMPTY"

    def test_empty_promotes_to_skipped_at_threshold(self) -> None:
        ledger = WindowLedger()
        status = ""
        for _ in range(EMPTY_TERMINAL_THRESHOLD):
            status = ledger.record(_key(), raw_bars=0, error_type=None, bars_appended=0, now=_NOW)
        assert status == SKIPPED  # genuinely empty → settled
        assert ledger.records()[0].attempts == EMPTY_TERMINAL_THRESHOLD

    def test_attempts_accumulate_and_first_seen_preserved(self) -> None:
        ledger = WindowLedger()
        ledger.plan(_key(), now=_NOW)  # first_seen = _NOW
        ledger.record(
            _key(), raw_bars=0, error_type="DhanRateLimitError", bars_appended=0, now=_LATER
        )
        ledger.record(_key(), raw_bars=9, error_type=None, bars_appended=9, now=_LATER)
        record = ledger.records()[0]
        assert record.status == "SUCCESS"  # FAILED → re-pulled → SUCCESS
        assert record.attempts == 2
        assert record.first_seen_at == _NOW.isoformat()  # preserved from plan

    def test_record_on_never_planned_window_stamps_first_seen(self) -> None:
        ledger = WindowLedger()
        ledger.record(_key(), raw_bars=3, error_type=None, bars_appended=3, now=_NOW)
        assert ledger.records()[0].first_seen_at == _NOW.isoformat()

    def test_custom_threshold(self) -> None:
        ledger = WindowLedger(_threshold=2)
        ledger.record(_key(), raw_bars=0, error_type=None, bars_appended=0, now=_NOW)
        status = ledger.record(_key(), raw_bars=0, error_type=None, bars_appended=0, now=_NOW)
        assert status == SKIPPED


class TestDegradedDemotion:
    """DEC-INTERIOR-GAP-DETECT-001 / DEC-PARTIAL-WINDOW-REPULL-001: a bars-returning fetch
    flagged INCOMPLETE (interior gap / truncation) is demoted to a re-pullable FAILED, BOUNDED
    by the empty-streak terminal threshold so the universe pull still converges."""

    def test_degraded_success_records_failed_with_reason_and_is_not_skippable(self) -> None:
        ledger = WindowLedger()
        status = ledger.record(
            _key(),
            raw_bars=5,
            error_type=None,
            bars_appended=5,
            now=_NOW,
            degraded=True,
            degraded_reason="INTERIOR_GAP",
        )
        assert status == FAILED
        rec = ledger.records()[0]
        assert rec.status == FAILED
        assert rec.last_error == "INTERIOR_GAP"  # the completeness reason, not a vendor error
        assert rec.bars == 5  # the stored bars are preserved
        assert not ledger.is_skippable(_key())  # re-pulled next pull

    def test_degraded_without_reason_falls_back_to_a_generic_tag(self) -> None:
        ledger = WindowLedger()
        ledger.record(_key(), raw_bars=5, error_type=None, bars_appended=5, now=_NOW, degraded=True)
        assert ledger.records()[0].last_error == "DEGRADED"

    def test_degraded_repull_is_bounded_settling_skipped_at_threshold(self) -> None:
        ledger = WindowLedger(_threshold=3)
        for _ in range(2):
            status = ledger.record(
                _key(),
                raw_bars=5,
                error_type=None,
                bars_appended=5,
                now=_NOW,
                degraded=True,
                degraded_reason="TRUNCATED",
            )
            assert status == FAILED  # still re-pullable before the threshold
        # The third consecutive degraded pull reaches the threshold → SKIPPED (gap accepted).
        final = ledger.record(
            _key(),
            raw_bars=5,
            error_type=None,
            bars_appended=5,
            now=_NOW,
            degraded=True,
            degraded_reason="TRUNCATED",
        )
        assert final == SKIPPED
        assert ledger.is_skippable(_key())  # the pull converges, never loops forever

    def test_a_later_complete_repull_resets_to_success(self) -> None:
        ledger = WindowLedger(_threshold=3)
        ledger.record(
            _key(),
            raw_bars=5,
            error_type=None,
            bars_appended=5,
            now=_NOW,
            degraded=True,
            degraded_reason="INTERIOR_GAP",
        )
        # The next pull returns a COMPLETE window (degraded=False) → clean SUCCESS, streak reset.
        status = ledger.record(_key(), raw_bars=9, error_type=None, bars_appended=9, now=_NOW)
        assert status == SUCCESS
        rec = ledger.records()[0]
        assert rec.empty_streak == 0
        assert rec.last_error is None

    def test_degraded_ignored_for_empty_response(self) -> None:
        # No bars → still EMPTY even with degraded set (nothing to be incomplete).
        ledger = WindowLedger()
        status = ledger.record(
            _key(), raw_bars=0, error_type=None, bars_appended=0, now=_NOW, degraded=True
        )
        assert status == EMPTY


class TestDegradedTombstone:
    """DEC-WINDOW-LEDGER-DEGRADED-TOMBSTONE-001 — `degraded_keys` feeds the merge-on-save
    so a concurrent pull's disk SUCCESS can never silently override a degraded demotion
    (the third instance of the `_pruned`/`_reopened` tombstone shape)."""

    def test_degraded_demotion_tombstones_key(self) -> None:
        # BUG-REGRESSION (fix b): the SUCCESS→FAILED interior-gap demotion must be
        # tombstoned exactly as reopen_skipped's demotions are — before the fix
        # `degraded_keys()` did not exist and the merge-on-save let a concurrent
        # disk SUCCESS resurrect over the demotion.
        ledger = WindowLedger()
        status = ledger.record(
            _key(),
            raw_bars=10,
            error_type=None,
            bars_appended=10,
            now=_NOW,
            degraded=True,
            degraded_reason="INTERIOR_GAP",
        )
        assert status == FAILED
        assert ledger.records()[0].last_error == "INTERIOR_GAP"
        assert ledger.degraded_keys() == frozenset(
            {("dhan", "NIFTY", "1minute", "2021-01-01", "2021-04-01")}
        )

    def test_degraded_accept_gap_not_tombstoned(self) -> None:
        # The accept-gap SKIPPED settle at the threshold is a legitimately terminal
        # rank-3 row — it must NOT be pinned against a concurrent SUCCESS. Threshold
        # 1 makes the FIRST degraded record settle SKIPPED directly, so the
        # `status == FAILED` guard is False and no tombstone is added.
        ledger = WindowLedger(_threshold=1)
        status = ledger.record(
            _key(),
            raw_bars=10,
            error_type=None,
            bars_appended=10,
            now=_NOW,
            degraded=True,
            degraded_reason="INTERIOR_GAP",
        )
        assert status == SKIPPED
        assert ledger.degraded_keys() == frozenset()

    def test_plain_records_not_tombstoned(self) -> None:
        # `degraded_success` False across every plain outcome: a clean SUCCESS, a
        # genuine EMPTY, and a transient FAILED (error_type set) never tombstone.
        ledger = WindowLedger()
        ledger.record(_key("NIFTY"), raw_bars=5, error_type=None, bars_appended=5, now=_NOW)
        ledger.record(_key("BANKNIFTY"), raw_bars=0, error_type=None, bars_appended=0, now=_NOW)
        ledger.record(
            _key("SENSEX"),
            raw_bars=0,
            error_type="GrowwRateLimitError",
            bars_appended=0,
            now=_NOW,
        )
        assert ledger.degraded_keys() == frozenset()

    def test_degraded_keys_empty_by_default(self) -> None:
        assert WindowLedger().degraded_keys() == frozenset()

    def test_success_repull_discards_stale_tombstone(self) -> None:
        # Hostile-review fold: a genuine COMPLETE re-record of the SAME key in the
        # SAME process supersedes the demotion — the stale tombstone is discarded
        # so the merge's more-settled rule applies to the new SUCCESS
        # unconditionally (pre-fold the set was add-only, so the stale tombstone
        # would have forced the memory row to win the merge outright forever).
        ledger = WindowLedger(_threshold=3)
        ledger.record(
            _key(),
            raw_bars=5,
            error_type=None,
            bars_appended=5,
            now=_NOW,
            degraded=True,
            degraded_reason="INTERIOR_GAP",
        )
        assert len(ledger.degraded_keys()) == 1
        status = ledger.record(_key(), raw_bars=9, error_type=None, bars_appended=9, now=_NOW)
        assert status == SUCCESS
        assert ledger.degraded_keys() == frozenset()

    def test_accept_gap_settle_discards_prior_demotion_tombstone(self) -> None:
        # Hostile-review fold: with threshold 2 the FIRST degraded record demotes
        # (FAILED, tombstoned) and the SECOND reaches the threshold → SKIPPED
        # (accept-gap). The "a SKIPPED settle is never tombstoned" contract now
        # holds UNCONDITIONALLY — the earlier demotion's tombstone is discarded at
        # the settle, so a concurrent disk SUCCESS may honestly displace the
        # terminal row under the more-settled rule.
        ledger = WindowLedger(_threshold=2)
        ledger.record(
            _key(),
            raw_bars=5,
            error_type=None,
            bars_appended=5,
            now=_NOW,
            degraded=True,
            degraded_reason="INTERIOR_GAP",
        )
        assert len(ledger.degraded_keys()) == 1
        final = ledger.record(
            _key(),
            raw_bars=5,
            error_type=None,
            bars_appended=5,
            now=_NOW,
            degraded=True,
            degraded_reason="INTERIOR_GAP",
        )
        assert final == SKIPPED
        assert ledger.degraded_keys() == frozenset()

    def test_repullable_rerecord_keeps_tombstone(self) -> None:
        # A transient FAILED (429) or a clean EMPTY re-record of the demoted key
        # gives NO completeness evidence — the demotion stands and the tombstone
        # is deliberately KEPT (discarding here would let a concurrent pull's disk
        # SUCCESS re-mask the confirmed gap on our own save).
        ledger = WindowLedger(_threshold=5)
        ledger.record(
            _key(),
            raw_bars=5,
            error_type=None,
            bars_appended=5,
            now=_NOW,
            degraded=True,
            degraded_reason="INTERIOR_GAP",
        )
        tombstoned = ledger.degraded_keys()
        assert len(tombstoned) == 1
        ledger.record(
            _key(),
            raw_bars=0,
            error_type="GrowwRateLimitError",
            bars_appended=0,
            now=_NOW,
        )
        assert ledger.degraded_keys() == tombstoned  # FAILED re-record keeps it
        ledger.record(_key(), raw_bars=0, error_type=None, bars_appended=0, now=_NOW)
        assert ledger.degraded_keys() == tombstoned  # EMPTY re-record keeps it too

    def test_from_rows_has_no_degraded_tombstone(self) -> None:
        # The tombstone is per-instance, never serialised: a demoted row round-trips
        # its FAILED status but the restored ledger carries NO tombstone (idempotence
        # pin — load→save→load stays a row fixed point, Golden Rule #7).
        ledger = WindowLedger()
        ledger.record(
            _key(),
            raw_bars=10,
            error_type=None,
            bars_appended=10,
            now=_NOW,
            degraded=True,
            degraded_reason="INTERIOR_GAP",
        )
        restored = WindowLedger.from_rows(ledger.to_rows())
        assert restored.to_rows() == ledger.to_rows()
        assert restored.degraded_keys() == frozenset()


class TestEmptyStreakR1:
    """R1 (DEC-WINDOW-LEDGER-001): promote on the CONSECUTIVE EMPTY streak, not total attempts."""

    def test_failed_attempts_do_not_inflate_the_empty_promotion(self) -> None:
        # Two 429 FAILs then ONE EMPTY: attempts == 3 but the streak is 1 — the
        # window must NOT be masked (it was empty once, rate-limited twice).
        ledger = WindowLedger()
        ledger.record(
            _key(), raw_bars=0, error_type="DhanRateLimitError", bars_appended=0, now=_NOW
        )
        ledger.record(
            _key(), raw_bars=0, error_type="DhanRateLimitError", bars_appended=0, now=_NOW
        )
        status = ledger.record(_key(), raw_bars=0, error_type=None, bars_appended=0, now=_NOW)
        assert status == "EMPTY"  # NOT SKIPPED — the R1 fix
        record = ledger.records()[0]
        assert record.attempts == 3
        assert record.empty_streak == 1
        assert ledger.is_skippable(_key()) is False

    def test_a_failed_outcome_preserves_the_empty_streak(self) -> None:
        # DEC-EMPTY-STREAK-RATELIMIT-001 (R2): a 429 / timeout between empties is NOT
        # evidence the window has data, so it must PRESERVE the genuine-empty streak
        # (the old behaviour RESET it, which is why a pre-listing window never strung
        # 3 clean empties together under a 429 storm → re-fetched forever → STUCK).
        ledger = WindowLedger()
        ledger.record(_key(), raw_bars=0, error_type=None, bars_appended=0, now=_NOW)  # EMPTY 1
        ledger.record(_key(), raw_bars=0, error_type=None, bars_appended=0, now=_NOW)  # EMPTY 2
        # A transient FAIL between empties now PRESERVES the streak (stays 2).
        ledger.record(
            _key(), raw_bars=0, error_type="GrowwRateLimitError", bars_appended=0, now=_NOW
        )
        assert ledger.records()[0].empty_streak == 2
        # So further clean empties keep accumulating (the FAIL did not reset) until the
        # streak finally reaches the threshold and the window settles SKIPPED.
        status = ""
        for _ in range(EMPTY_TERMINAL_THRESHOLD - 2):
            status = ledger.record(_key(), raw_bars=0, error_type=None, bars_appended=0, now=_NOW)
        assert status == SKIPPED
        assert ledger.records()[0].empty_streak == EMPTY_TERMINAL_THRESHOLD
        assert ledger.is_skippable(_key()) is True

    def test_pull_converges_under_a_429_storm(self) -> None:
        # The operator's exact scenario: a genuinely-empty (pre-listing) window that
        # is 429-throttled between every clean empty STILL converges to SKIPPED — the
        # full universe pull can finish instead of re-fetching it forever.
        ledger = WindowLedger()
        # A 429 BEFORE each clean empty — the storm interleaves the whole way, yet the
        # threshold-th clean empty still settles the window (FAILED preserves the streak).
        outcomes: list[str | None] = []
        for _ in range(EMPTY_TERMINAL_THRESHOLD):
            outcomes.append("GrowwRateLimitError")
            outcomes.append(None)
        status = ""
        for err in outcomes:
            status = ledger.record(_key(), raw_bars=0, error_type=err, bars_appended=0, now=_NOW)
        assert status == SKIPPED  # threshold CLEAN empties reached despite interleaved 429s
        assert ledger.is_skippable(_key()) is True

    def test_real_data_after_empties_is_never_masked(self) -> None:
        # The pull-until-success contract: a window that returns EMPTY twice then
        # finally returns BARS must record SUCCESS (data wins; streak resets), never
        # a premature SKIPPED.
        ledger = WindowLedger()
        ledger.record(_key(), raw_bars=0, error_type=None, bars_appended=0, now=_NOW)  # EMPTY 1
        ledger.record(_key(), raw_bars=0, error_type=None, bars_appended=0, now=_NOW)  # EMPTY 2
        status = ledger.record(_key(), raw_bars=9, error_type=None, bars_appended=9, now=_NOW)
        assert status == SUCCESS
        assert ledger.records()[0].empty_streak == 0

    def test_three_consecutive_empties_still_promote(self) -> None:
        ledger = WindowLedger()
        status = ""
        for _ in range(EMPTY_TERMINAL_THRESHOLD):
            status = ledger.record(_key(), raw_bars=0, error_type=None, bars_appended=0, now=_NOW)
        assert status == SKIPPED
        assert ledger.records()[0].empty_streak == EMPTY_TERMINAL_THRESHOLD

    def test_success_resets_streak_to_zero(self) -> None:
        ledger = WindowLedger()
        ledger.record(_key(), raw_bars=0, error_type=None, bars_appended=0, now=_NOW)  # EMPTY 1
        ledger.record(_key(), raw_bars=7, error_type=None, bars_appended=7, now=_NOW)  # SUCCESS
        assert ledger.records()[0].empty_streak == 0


class TestSettleKnownGap:
    """A DH-905 known-gap is settled SKIPPED (never re-pulled forever)."""

    def test_settle_without_prior(self) -> None:
        ledger = WindowLedger()
        status = ledger.settle_known_gap(_key(), now=_NOW, reason="DH-905")
        assert status == SKIPPED
        record = ledger.records()[0]
        assert record.attempts == 1
        assert record.last_error == "DH-905"
        assert record.first_seen_at == _NOW.isoformat()
        assert record.bars == 0

    def test_settle_preserves_prior_first_seen_and_bars(self) -> None:
        ledger = WindowLedger()
        ledger.record(_key(), raw_bars=4, error_type=None, bars_appended=4, now=_NOW)
        ledger.settle_known_gap(_key(), now=_LATER)
        record = ledger.records()[0]
        assert record.status == SKIPPED
        assert record.attempts == 2
        assert record.bars == 4  # prior bars preserved
        assert record.first_seen_at == _NOW.isoformat()
        assert record.last_error is None  # no reason given


class TestSeedLakeSuccess:
    """The lake cold-start: seed SUCCESS only for an ABSENT window, never overwrite."""

    def test_seeds_an_absent_window(self) -> None:
        ledger = WindowLedger()
        assert ledger.seed_lake_success(_key(), bars=42, now=_NOW) is True
        record = ledger.records()[0]
        assert record.status == "SUCCESS"
        assert record.bars == 42
        assert ledger.is_skippable(_key()) is True

    def test_never_overwrites_an_existing_row(self) -> None:
        ledger = WindowLedger()
        # A FAILED window must NEVER be seeded to SUCCESS (it needs a re-pull).
        ledger.record(
            _key(), raw_bars=0, error_type="DhanRateLimitError", bars_appended=0, now=_NOW
        )
        assert ledger.seed_lake_success(_key(), bars=99, now=_LATER) is False
        record = ledger.records()[0]
        assert record.status == "FAILED"  # untouched
        assert ledger.is_skippable(_key()) is False

    def test_does_not_overwrite_a_planned_row(self) -> None:
        ledger = WindowLedger()
        ledger.plan(_key(), now=_NOW)
        assert ledger.seed_lake_success(_key(), bars=5, now=_LATER) is False
        assert ledger.records()[0].status == PLANNED


def _tkey(end: date, start: date = date(2026, 6, 1)) -> WindowKey:
    """A trailing-tile key: fixed start, the *end* a given pull's clip produced."""
    return WindowKey(market="dhan", security_id="NIFTY", interval="1minute", start=start, end=end)


def _plan_index(*keys: WindowKey) -> dict[tuple[str, str, str], dict[str, str]]:
    """The series→tiles GC index a planner would build for *keys*."""
    index: dict[tuple[str, str, str], dict[str, str]] = {}
    for key in keys:
        tiles = index.setdefault((key.market, key.security_id, key.interval), {})
        tiles[key.start.isoformat()] = key.end.isoformat()
    return index


class TestPruneSuperseded:
    """DEC-WINDOW-TRAILING-GC-001 — superseded trailing rows pruned, nothing else.

    The no-mask theorem: pruning deletes rows, an absent row is never skippable,
    so GC can only turn skips into re-fetches — never a fetch into a skip.
    """

    def test_shorter_end_orphan_is_pruned(self) -> None:
        # Yesterday's settled trailing partial — superseded by today's longer tile.
        ledger = WindowLedger()
        orphan = _tkey(end=date(2026, 6, 9))
        ledger.record(orphan, raw_bars=5, error_type=None, bars_appended=5, now=_NOW)
        planned = _tkey(end=date(2026, 6, 10))
        ledger.plan(planned, now=_LATER)
        assert ledger.prune_superseded(_plan_index(planned)) == 1
        assert ledger.count == 1
        assert ledger.is_skippable(orphan) is False  # gone, not masked
        assert ledger.records()[0].window_end == "2026-06-10"

    def test_every_status_class_is_superseded_alike(self) -> None:
        # SUCCESS / FAILED / EMPTY / PLANNED / SKIPPED orphans: all unreachable
        # by the tiler once end advanced, all pruned (retries re-derive from the
        # tiling, never from ledger rows — hostile A2).
        ledger = WindowLedger()
        ledger.record(
            _tkey(date(2026, 6, 4)), raw_bars=5, error_type=None, bars_appended=5, now=_NOW
        )
        ledger.record(
            _tkey(date(2026, 6, 5)),
            raw_bars=0,
            error_type="DhanServerError",
            bars_appended=0,
            now=_NOW,
        )
        ledger.record(
            _tkey(date(2026, 6, 6)), raw_bars=0, error_type=None, bars_appended=0, now=_NOW
        )
        ledger.plan(_tkey(date(2026, 6, 7)), now=_NOW)
        ledger.settle_known_gap(_tkey(date(2026, 6, 8)), now=_NOW)
        planned = _tkey(end=date(2026, 6, 10))
        ledger.plan(planned, now=_LATER)
        assert ledger.prune_superseded(_plan_index(planned)) == 5
        assert [r.window_end for r in ledger.records()] == ["2026-06-10"]

    def test_equal_end_row_is_kept(self) -> None:
        # A same-day re-pull re-emits the IDENTICAL trailing key — its settled
        # row must survive so the second pull still skips it.
        ledger = WindowLedger()
        planned = _tkey(end=date(2026, 6, 10))
        ledger.record(planned, raw_bars=5, error_type=None, bars_appended=5, now=_NOW)
        assert ledger.prune_superseded(_plan_index(planned)) == 0
        assert ledger.is_skippable(planned) is True

    def test_longer_end_row_is_kept(self) -> None:
        # A shrunk-range re-run: the longer historical row carries real settled
        # state and is NOT superseded by the shorter plan.
        ledger = WindowLedger()
        longer = _tkey(end=date(2026, 6, 12))
        ledger.record(longer, raw_bars=9, error_type=None, bars_appended=9, now=_NOW)
        planned = _tkey(end=date(2026, 6, 10))
        ledger.plan(planned, now=_LATER)
        assert ledger.prune_superseded(_plan_index(planned)) == 0
        assert ledger.count == 2

    def test_longer_end_planned_ghost_is_pruned(self) -> None:
        # DEC-PLAN-CLAMP-PARITY-001 (operator live, 2026-06-10): the pre-parity
        # planner tiled the RAW end while the fetch clamped — one PLANNED row
        # per (instrument, interval) ending PAST every later clamped plan, so
        # the shorter-end rule could never reach it. A longer-end row that is
        # pure intent (PLANNED, zero attempts) is pruned by the current plan.
        ledger = WindowLedger()
        ghost = _tkey(end=date(2026, 6, 11))  # raw end the fetch never records
        ledger.plan(ghost, now=_NOW)
        planned = _tkey(end=date(2026, 6, 10))  # the clamped tiling
        ledger.plan(planned, now=_LATER)
        assert ledger.prune_superseded(_plan_index(planned)) == 1
        assert [r.window_end for r in ledger.records()] == ["2026-06-10"]
        assert ledger.is_skippable(ghost) is False  # never skippable, now gone

    def test_longer_end_planned_with_attempts_is_kept(self) -> None:
        # Defensive guard: a loaded row that claims PLANNED yet carries attempt
        # history (impossible via the live API — record() always re-classifies)
        # is NOT pure intent, so the ghost rule leaves it alone.
        ledger = WindowLedger.from_rows(
            [
                (
                    "dhan",
                    "NIFTY",
                    "1minute",
                    "2026-06-01",
                    "2026-06-12",
                    "PLANNED",
                    0,
                    1,
                    None,
                    _NOW.isoformat(),
                    _NOW.isoformat(),
                    0,
                    0,
                    None,
                )
            ]
        )
        planned = _tkey(end=date(2026, 6, 10))
        ledger.plan(planned, now=_LATER)
        assert ledger.prune_superseded(_plan_index(planned)) == 0
        assert ledger.count == 2

    def test_unplanned_series_is_kept(self) -> None:
        # An options-only / single-symbol pull must not touch other series' rows.
        ledger = WindowLedger()
        other = WindowKey(
            market="dhan",
            security_id="SENSEX",
            interval="1minute",
            start=date(2026, 6, 1),
            end=date(2026, 6, 9),
        )
        ledger.record(other, raw_bars=5, error_type=None, bars_appended=5, now=_NOW)
        planned = _tkey(end=date(2026, 6, 10))  # NIFTY only
        ledger.plan(planned, now=_LATER)
        assert ledger.prune_superseded(_plan_index(planned)) == 0
        assert ledger.is_skippable(other) is True

    def test_off_tiling_start_is_kept(self) -> None:
        # A grid-shift orphan (changed pull start): its window_start is not on
        # the planned tiling — out of this decision's scope, left untouched.
        ledger = WindowLedger()
        shifted = _tkey(end=date(2026, 6, 9), start=date(2026, 5, 15))
        ledger.record(shifted, raw_bars=5, error_type=None, bars_appended=5, now=_NOW)
        planned = _tkey(end=date(2026, 6, 10))  # tiling anchored 2026-06-01
        ledger.plan(planned, now=_LATER)
        assert ledger.prune_superseded(_plan_index(planned)) == 0
        assert ledger.count == 2

    def test_legacy_far_expiry_full_range_row_inert_under_stable_start_plan(self) -> None:
        # DEC-FNO-FAR-EXPIRY-WINDOW-001 legacy proof: a PRE-fix far-expiry pull left a
        # settled full-range row (start = 2020-01-01, the old full-history fallback).
        # The post-fix default plan uses the STABLE expiry-anchored start, so the
        # legacy row is off-tiling → KEPT — a BOUNDED one-time orphan (no new orphan
        # is ever minted: the strict-window law has no moving start) and a settled
        # SUCCESS row is never wrongly pruned.
        ledger = WindowLedger()
        legacy = _tkey(end=date(2026, 6, 30), start=date(2020, 1, 1))
        ledger.record(legacy, raw_bars=1000, error_type=None, bars_appended=1000, now=_NOW)
        planned = _tkey(end=date(2026, 7, 2), start=date(2026, 6, 1))  # stable expiry−lookback
        ledger.plan(planned, now=_LATER)
        assert ledger.prune_superseded(_plan_index(planned)) == 0
        assert ledger.is_skippable(legacy) is True  # intact settled history
        assert ledger.count == 2

    def test_legacy_far_expiry_full_range_row_prunable_by_full_range_replan(self) -> None:
        # ...and the legacy row REMAINS PRUNABLE exactly as before: its start is the
        # STABLE 2020-01-01 anchor, so an explicit full-range re-pull re-plans the
        # SAME start with a longer end and the EXISTING shorter-end rule collects it
        # (no new GC mechanism needed — DEC-FNO-FAR-EXPIRY-WINDOW-001).
        ledger = WindowLedger()
        legacy = _tkey(end=date(2026, 6, 30), start=date(2020, 1, 1))
        ledger.record(legacy, raw_bars=1000, error_type=None, bars_appended=1000, now=_NOW)
        planned = _tkey(end=date(2026, 7, 2), start=date(2020, 1, 1))
        ledger.plan(planned, now=_LATER)
        assert ledger.prune_superseded(_plan_index(planned)) == 1
        assert [r.window_start for r in ledger.records()] == ["2020-01-01"]
        assert ledger.records()[0].window_end == "2026-07-02"

    def test_prune_is_idempotent(self) -> None:
        ledger = WindowLedger()
        ledger.record(
            _tkey(date(2026, 6, 9)), raw_bars=5, error_type=None, bars_appended=5, now=_NOW
        )
        planned = _tkey(end=date(2026, 6, 10))
        ledger.plan(planned, now=_LATER)
        index = _plan_index(planned)
        assert ledger.prune_superseded(index) == 1
        assert ledger.prune_superseded(index) == 0  # fixed point

    def test_empty_index_is_a_noop(self) -> None:
        ledger = WindowLedger()
        ledger.record(
            _tkey(date(2026, 6, 9)), raw_bars=5, error_type=None, bars_appended=5, now=_NOW
        )
        assert ledger.prune_superseded({}) == 0
        assert ledger.count == 1

    def test_empty_ledger_prunes_nothing(self) -> None:
        assert WindowLedger().prune_superseded(_plan_index(_tkey(date(2026, 6, 10)))) == 0


def test_settled_statuses_is_the_skip_authority() -> None:
    # DEC-SPOT-COVERAGE-COMPLETENESS-001: the public settled set is EXACTLY the
    # statuses is_skippable() honours — read-only consumers classify with the
    # ledger's own authority, never a hand-typed mirror list.
    assert frozenset({SUCCESS, SKIPPED}) == SETTLED_STATUSES
    ledger = WindowLedger()
    ledger.record(_key(), raw_bars=5, error_type=None, bars_appended=5, now=_NOW)
    record = ledger.records()[0]
    assert (record.status in SETTLED_STATUSES) == ledger.is_skippable(_key())


def test_records_are_sorted() -> None:
    ledger = WindowLedger()
    ledger.plan(_key("SENSEX"), now=_NOW)
    ledger.plan(_key("BANKNIFTY"), now=_NOW)
    ledger.plan(_key("NIFTY"), now=_NOW)
    symbols = [r.security_id for r in ledger.records()]
    assert symbols == ["BANKNIFTY", "NIFTY", "SENSEX"]


def test_to_rows_from_rows_round_trip_is_a_fixed_point() -> None:
    ledger = WindowLedger()
    ledger.record(_key("NIFTY"), raw_bars=5, error_type=None, bars_appended=5, now=_NOW)
    ledger.plan(_key("SENSEX"), now=_NOW)  # last_attempt_at None branch
    rows = ledger.to_rows()
    restored = WindowLedger.from_rows(rows)
    assert restored.to_rows() == rows
    assert restored.count == 2
    assert restored.is_skippable(_key("NIFTY")) is True
    assert restored.is_skippable(_key("SENSEX")) is False


def test_from_rows_honours_threshold() -> None:
    ledger = WindowLedger.from_rows([], threshold=2)
    ledger.record(_key(), raw_bars=0, error_type=None, bars_appended=0, now=_NOW)
    status = ledger.record(_key(), raw_bars=0, error_type=None, bars_appended=0, now=_NOW)
    assert status == SKIPPED  # the injected threshold of 2 took effect


class TestInPullFetches:
    """DEC-WINDOW-INPULL-FETCHES-001: the per-window in-pull vendor-fetch count.

    ``attempts`` counts PULLS; ``in_pull_fetches`` counts the network fetches WITHIN
    one pull (the in-pull empty/transient retry, previously DEBUG-only), so the
    operator finally SEES that "attempts 1" already meant up to 3 fetches.
    """

    def test_record_stores_the_given_fetch_count(self) -> None:
        # A 3× in-pull empty retry: ONE pull, THREE fetches.
        ledger = WindowLedger()
        ledger.record(
            _key(), raw_bars=0, error_type=None, bars_appended=0, now=_NOW, in_pull_fetches=3
        )
        record = ledger.records()[0]
        assert record.attempts == 1  # ONE pull
        assert record.in_pull_fetches == 3  # THREE network fetches inside it

    def test_record_defaults_to_one_fetch(self) -> None:
        # The default keeps every legacy single-fetch caller honest.
        ledger = WindowLedger()
        ledger.record(_key(), raw_bars=5, error_type=None, bars_appended=5, now=_NOW)
        assert ledger.records()[0].in_pull_fetches == 1

    def test_record_overwrites_not_accumulates(self) -> None:
        # OVERWRITE (not sum): re-running the SAME pull is bit-identical (Golden Rule #7).
        ledger = WindowLedger()
        ledger.record(
            _key(), raw_bars=0, error_type=None, bars_appended=0, now=_NOW, in_pull_fetches=3
        )
        ledger.record(
            _key(), raw_bars=7, error_type=None, bars_appended=7, now=_NOW, in_pull_fetches=1
        )
        record = ledger.records()[0]
        assert record.attempts == 2  # two pulls
        assert record.in_pull_fetches == 1  # the LAST pull's count, not 3+1

    def test_plan_records_zero_fetches(self) -> None:
        ledger = WindowLedger()
        ledger.plan(_key(), now=_NOW)
        assert ledger.records()[0].in_pull_fetches == 0  # never fetched

    def test_seed_lake_success_records_one_fetch(self) -> None:
        ledger = WindowLedger()
        ledger.seed_lake_success(_key(), bars=42, now=_NOW)
        assert ledger.records()[0].in_pull_fetches == 1  # one fetch's worth of data

    def test_settle_known_gap_records_one_fetch(self) -> None:
        ledger = WindowLedger()
        ledger.settle_known_gap(_key(), now=_NOW, reason="DH-905")
        assert ledger.records()[0].in_pull_fetches == 1  # the fetch that returned the gap

    def test_round_trip_preserves_a_multi_fetch_count(self) -> None:
        ledger = WindowLedger()
        ledger.record(
            _key("NIFTY"), raw_bars=0, error_type=None, bars_appended=0, now=_NOW, in_pull_fetches=3
        )
        rows = ledger.to_rows()
        restored = WindowLedger.from_rows(rows)
        assert restored.to_rows() == rows  # fixed point
        assert restored.records()[0].in_pull_fetches == 3


class TestSettleEmpty:
    """DEC-OLD-GAP-FAST-SETTLE-001: ``record(settle_empty=True)`` settles an OLD empty in ONE pull.

    The fast-settle hint is honoured ONLY for an EMPTY classification: an empty window
    becomes terminal ``SKIPPED`` (streak forced to the threshold) in a single record;
    a SUCCESS / FAILED ignores it (a success has data; an error is not evidence of
    empty). The default ``False`` is byte-identical to the prior behaviour (covered by
    the existing :class:`TestRecord` / :class:`TestEmptyStreakR1` suites).
    """

    def test_settle_empty_settles_an_empty_in_one_record(self) -> None:
        ledger = WindowLedger()
        status = ledger.record(
            _key(), raw_bars=0, error_type=None, bars_appended=0, now=_NOW, settle_empty=True
        )
        assert status == SKIPPED  # terminal in ONE pull, no 3-pull streak needed
        record = ledger.records()[0]
        assert record.status == SKIPPED
        assert record.empty_streak == EMPTY_TERMINAL_THRESHOLD  # forced to the threshold
        assert record.attempts == 1  # ONE pull
        assert ledger.is_skippable(_key()) is True

    def test_settle_empty_on_success_is_a_noop(self) -> None:
        # A window WITH data is never settled-as-empty — settle_empty is ignored.
        ledger = WindowLedger()
        status = ledger.record(
            _key(), raw_bars=9, error_type=None, bars_appended=9, now=_NOW, settle_empty=True
        )
        assert status == SUCCESS  # untouched — settle_empty never masks real data
        record = ledger.records()[0]
        assert record.status == SUCCESS
        assert record.empty_streak == 0

    def test_settle_empty_on_failed_is_a_noop(self) -> None:
        # A 429/timeout is NOT evidence the window is empty — settle_empty is ignored.
        ledger = WindowLedger()
        status = ledger.record(
            _key(),
            raw_bars=0,
            error_type="GrowwRateLimitError",
            bars_appended=0,
            now=_NOW,
            settle_empty=True,
        )
        assert status == FAILED  # never settled on an error
        assert ledger.is_skippable(_key()) is False

    def test_settle_empty_default_false_keeps_conservative_streak(self) -> None:
        # The default (no settle_empty) keeps the across-pull EMPTY streak: a single
        # empty stays EMPTY 1/3, NOT skippable — byte-identical to the prior behaviour.
        ledger = WindowLedger()
        status = ledger.record(_key(), raw_bars=0, error_type=None, bars_appended=0, now=_NOW)
        assert status == EMPTY
        assert ledger.records()[0].empty_streak == 1
        assert ledger.is_skippable(_key()) is False


class TestOldEmptySingleConfirm:
    """DEC-OLD-EMPTY-SINGLE-CONFIRM-001: an OLD window settles off ONE clean empty.

    Operator-decreed 2026-07-04 — CONSCIOUSLY SUPERSEDES the budget-aware
    cumulative arm of DEC-DEAD-STRIKE-LEDGER-SETTLE-001 (#1172, the prior
    ``TestBudgetAwareOldGapSettle`` suite): the 2026-07-03 production run spent
    hours in a round-2 until-clean sweep re-confirming every deep-wing empty
    because an OLD window needed ``prior_streak + in_pull_fetches >= 2``. An
    expired window (``old_window=True``) now settles ``SKIPPED`` on a SINGLE
    genuine, unsuppressed clean-EMPTY fetch, with the honest
    "1 clean attempt, expired window" reason stamped. The data-safety law is
    pinned unchanged: RECENT windows keep the 3-streak law byte-identical,
    SUCCESS wins, a FAILED transient never settles, a breaker-suppressed empty
    neither bumps nor settles (the #1129 breaker + #919 detector stay the
    systemic-glitch guards; ``brutex reopen-skipped`` is the manual recovery).
    """

    def test_old_window_single_clean_empty_settles_in_pass_one(self) -> None:
        # THE new law: one clean empty on an expired window settles SKIPPED NOW —
        # no round-2 re-fetch (was EMPTY + re-fetchable under #1172).
        ledger = WindowLedger()
        status = ledger.record(
            _key(),
            raw_bars=0,
            error_type=None,
            bars_appended=0,
            now=_NOW,
            in_pull_fetches=1,
            old_window=True,
        )
        assert status == SKIPPED
        record = ledger.records()[0]
        assert record.empty_streak == EMPTY_TERMINAL_THRESHOLD  # forced — settled shape
        assert record.last_error == "vendor confirmed no trades (1 clean attempt, expired window)"
        assert ledger.is_skippable(_key()) is True

    def test_settle_reason_counts_genuine_prior_clean_pulls(self) -> None:
        # A pre-change ledger row carrying a genuine streak keeps the honest count:
        # one prior clean-empty pull + this one reads "2 clean attempts".
        ledger = WindowLedger()
        ledger.record(
            _key(),
            raw_bars=0,
            error_type=None,
            bars_appended=0,
            now=_NOW,
            in_pull_fetches=1,
            old_window=False,  # recorded as a RECENT empty on pull 1 (streak 1)
        )
        status = ledger.record(
            _key(),
            raw_bars=0,
            error_type=None,
            bars_appended=0,
            now=_LATER,
            in_pull_fetches=1,
            old_window=True,  # the window has since aged past the horizon
        )
        assert status == SKIPPED
        assert ledger.records()[0].last_error == (
            "vendor confirmed no trades (2 clean attempts, expired window)"
        )

    def test_recent_window_keeps_conservative_streak(self) -> None:
        # UNCHANGED (#1172 pin kept verbatim): old_window=False → the settle arm
        # never fires; two empties stay EMPTY 2/3 — the 3-streak law byte-identical.
        ledger = WindowLedger()
        for now in (_NOW, _LATER):
            status = ledger.record(
                _key(),
                raw_bars=0,
                error_type=None,
                bars_appended=0,
                now=now,
                in_pull_fetches=1,
                old_window=False,
            )
        assert status == EMPTY
        record = ledger.records()[0]
        assert record.empty_streak == 2
        assert record.last_error is None  # no expired-window stamp on a recent empty
        assert ledger.is_skippable(_key()) is False

    def test_failed_transient_never_settles_even_when_old(self) -> None:
        # A 429/timeout is NOT evidence the window is empty — an OLD window's
        # transient stays FAILED + re-pullable; the NEXT clean empty settles.
        ledger = WindowLedger()
        status = ledger.record(
            _key(),
            raw_bars=0,
            error_type="GrowwRateLimitError",
            bars_appended=0,
            now=_NOW,
            in_pull_fetches=3,  # transient fetches are NOT clean-empty evidence
            old_window=True,
        )
        assert status == FAILED
        assert ledger.records()[0].empty_streak == 0
        assert ledger.is_skippable(_key()) is False
        status = ledger.record(
            _key(),
            raw_bars=0,
            error_type=None,
            bars_appended=0,
            now=_LATER,
            in_pull_fetches=1,
            old_window=True,
        )
        assert status == SKIPPED  # the single-confirm law — settled on the clean empty

    def test_suppressed_empty_neither_bumps_nor_settles_even_when_old(self) -> None:
        # A breaker-suppressed empty is UNTRUSTED (DEC-EMPTY-WINDOW-CIRCUIT-BREAKER-001):
        # even under the single-confirm law it can NEVER cement an old window —
        # a throttle-shaped empty stays retryable.
        ledger = WindowLedger()
        status = ledger.record(
            _key(),
            raw_bars=0,
            error_type=None,
            bars_appended=0,
            now=_NOW,
            in_pull_fetches=1,
            old_window=True,
            suppress_settle=True,
        )
        assert status == EMPTY
        assert ledger.records()[0].empty_streak == 0  # preserved — no poisoned progress
        assert ledger.is_skippable(_key()) is False

    def test_old_window_ignored_for_success(self) -> None:
        # UNCHANGED (#1172 pin): old_window on a bars-returning fetch is a no-op —
        # data always wins.
        ledger = WindowLedger()
        status = ledger.record(
            _key(),
            raw_bars=5,
            error_type=None,
            bars_appended=5,
            now=_NOW,
            in_pull_fetches=2,
            old_window=True,
        )
        assert status == SUCCESS
        assert ledger.records()[0].empty_streak == 0

    def test_settle_empty_hint_still_settles_with_the_expired_reason(self) -> None:
        # The in-pull fast-settle hint (DEC-OLD-GAP-FAST-SETTLE-001 settle_empty)
        # keeps working and now ALSO stamps the honest expired-window reason.
        ledger = WindowLedger()
        status = ledger.record(
            _key(),
            raw_bars=0,
            error_type=None,
            bars_appended=0,
            now=_NOW,
            in_pull_fetches=2,
            settle_empty=True,
        )
        assert status == SKIPPED
        assert is_expired_window_settle_reason(ledger.records()[0].last_error)

    def test_reopen_skipped_reopens_a_single_confirm_settle(self) -> None:
        # The manual recovery (brutex reopen-skipped) un-cements a single-confirm
        # settle: streak reset, REOPENED marker, re-fetchable.
        ledger = WindowLedger()
        ledger.record(
            _key(),
            raw_bars=0,
            error_type=None,
            bars_appended=0,
            now=_NOW,
            in_pull_fetches=1,
            old_window=True,
        )
        assert ledger.is_skippable(_key()) is True
        reopened = ledger.reopen_skipped()
        assert reopened == 1
        assert ledger.is_skippable(_key()) is False
        assert ledger.records()[0].empty_streak == 0


class TestExpiredWindowSettleReason:
    """The pure reason helpers (DEC-OLD-EMPTY-SINGLE-CONFIRM-001)."""

    def test_singular_reads_the_decreed_string(self) -> None:
        assert expired_window_settle_reason(1) == (
            "vendor confirmed no trades (1 clean attempt, expired window)"
        )

    def test_plural_counts_read_attempts(self) -> None:
        assert expired_window_settle_reason(3) == (
            "vendor confirmed no trades (3 clean attempts, expired window)"
        )

    def test_predicate_matches_only_the_stamp(self) -> None:
        assert is_expired_window_settle_reason(expired_window_settle_reason(1))
        assert is_expired_window_settle_reason(expired_window_settle_reason(2))
        assert not is_expired_window_settle_reason(None)
        assert not is_expired_window_settle_reason("GrowwRateLimitError")
        assert not is_expired_window_settle_reason("vendor confirmed no trades (1 clean attempt)")
        assert not is_expired_window_settle_reason("something, expired window)")


def test_confirmed_empty_min_fetches_single_source_of_truth() -> None:
    # DEC-DEAD-STRIKE-LEDGER-SETTLE-001 relocation: the ledger is the canonical home and
    # ``brutex.adapters.ingest_common`` re-exports the SAME value (CLAUDE.md §11 COMMON).
    from brutex.adapters.ingest_common import CONFIRMED_EMPTY_MIN_FETCHES as REEXPORTED
    from brutex.data.window_ledger import CONFIRMED_EMPTY_MIN_FETCHES as CANONICAL

    assert REEXPORTED == CANONICAL == 2


def test_concurrent_plan_and_record_never_tears_the_map() -> None:
    # DEC-DHAN-PARALLEL-INGEST-001: one ledger, many workers stamping distinct
    # windows concurrently — the lock keeps the map consistent.
    ledger = WindowLedger()
    keys = [_key(f"SYM{i}") for i in range(200)]

    def worker(k: WindowKey) -> None:
        ledger.plan(k, now=_NOW)
        ledger.record(k, raw_bars=1, error_type=None, bars_appended=1, now=_NOW)

    threads = [threading.Thread(target=worker, args=(k,)) for k in keys]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert ledger.count == 200
    assert all(ledger.is_skippable(k) for k in keys)


def _k(symbol: str, interval: str, start: date, end: date) -> WindowKey:
    return WindowKey(market="groww", security_id=symbol, interval=interval, start=start, end=end)


class TestProgressCounts:
    """`progress_counts` — the live, idempotent O(N) window-progress fold
    (DEC-SPOT-WINDOW-PROGRESS-001) that drives the dashboard bar per WINDOW."""

    def test_empty_ledger_is_all_zero(self) -> None:
        p = WindowLedger().progress_counts()
        assert p == LedgerProgress(
            total=0,
            settled=0,
            resolved=0,
            no_data=0,
            planned=0,
            failed=0,
            bars=0,
            current="",
        )

    def test_counts_settled_failed_bars_and_total(self) -> None:
        ledger = WindowLedger()
        a = _k("NIFTY", "1day", date(2020, 1, 1), date(2020, 6, 29))
        b = _k("BANKNIFTY", "1day", date(2020, 1, 1), date(2020, 6, 29))
        c = _k("FINNIFTY", "1day", date(2020, 1, 1), date(2020, 6, 29))
        d = _k("MIDCAP50", "1day", date(2020, 1, 1), date(2020, 6, 29))
        e = _k("SENSEX", "1day", date(2020, 1, 1), date(2020, 6, 29))
        ledger.plan(d, now=_NOW)  # PLANNED → planned (re-pull WILL try it)
        ledger.record(a, raw_bars=120, error_type=None, bars_appended=120, now=_NOW)  # SUCCESS
        ledger.settle_known_gap(b, now=_NOW)  # SKIPPED → settled
        ledger.record(c, raw_bars=0, error_type="GrowwTimeoutError", bars_appended=0, now=_NOW)
        ledger.record(e, raw_bars=0, error_type=None, bars_appended=0, now=_NOW)  # EMPTY (no data)
        p = ledger.progress_counts()
        assert p.total == 5
        assert p.settled == 2  # SUCCESS + SKIPPED (the SKIP authority — SETTLED_STATUSES)
        # RESOLVED is WIDER than settled — it includes the EMPTY (no-data, not "pending"):
        assert p.resolved == 3  # SUCCESS + SKIPPED + EMPTY (DEC-EMPTY-RESOLVED-REPORTING-001)
        assert p.no_data == 1  # the genuine EMPTY window — its own honest bucket
        assert p.planned == 1  # the PLANNED window a re-pull WILL attempt
        assert p.failed == 1  # the FAILED (transient) window
        assert p.bars == 120

    def test_current_is_the_most_recently_attempted_window(self) -> None:
        ledger = WindowLedger()
        first = _k("AAA", "1day", date(2020, 1, 1), date(2020, 6, 29))
        last = _k("ZZZ", "1day", date(2020, 1, 1), date(2020, 6, 29))
        ledger.record(first, raw_bars=10, error_type=None, bars_appended=10, now=_NOW)
        ledger.record(last, raw_bars=10, error_type=None, bars_appended=10, now=_LATER)
        # The label is the LAST-attempted window's "<symbol> <interval>", regardless of the
        # sort order of the keys — so the bar names the symbol fetched most recently.
        assert ledger.progress_counts().current == "ZZZ 1day"

    def test_planned_only_window_has_no_current(self) -> None:
        ledger = WindowLedger()
        ledger.plan(_k("NIFTY", "1day", date(2020, 1, 1), date(2020, 6, 29)), now=_NOW)
        # A never-attempted (PLANNED) window has last_attempt_at None → no current label.
        assert ledger.progress_counts().current == ""

    def test_idempotent_repeated_poll_does_not_double_count(self) -> None:
        ledger = WindowLedger()
        key = _k("NIFTY", "1day", date(2020, 1, 1), date(2020, 6, 29))
        ledger.record(key, raw_bars=120, error_type=None, bars_appended=120, now=_NOW)
        first = ledger.progress_counts()
        second = ledger.progress_counts()
        assert first == second  # absolute fold — re-polling never inflates the counts

    def test_market_and_interval_filters_scope_the_fold(self) -> None:
        # DEC-PULL-PROGRESS-HONESTY-001: the ledger is CROSS-RUN and CROSS-MARKET,
        # so the F&O phase's live fold must scope to its own markets × interval —
        # otherwise the spot windows pollute its denominator (fails-before: the
        # unfiltered fold below reads total=3). None defaults stay byte-identical.
        ledger = WindowLedger()
        spot = WindowKey(
            market="groww",
            security_id="NIFTY",
            interval="1minute",
            start=date(2020, 1, 1),
            end=date(2020, 6, 29),
        )
        option = WindowKey(
            market="groww-option",
            security_id="NSE-NIFTY-25Jan24-21000-CE",
            interval="1minute",
            start=date(2020, 1, 1),
            end=date(2020, 6, 29),
        )
        option_daily = WindowKey(
            market="groww-option",
            security_id="NSE-NIFTY-25Jan24-21000-CE",
            interval="1day",
            start=date(2020, 1, 1),
            end=date(2020, 6, 29),
        )
        ledger.record(spot, raw_bars=999, error_type=None, bars_appended=999, now=_NOW)
        ledger.record(option, raw_bars=42, error_type=None, bars_appended=42, now=_LATER)
        ledger.record(option_daily, raw_bars=7, error_type=None, bars_appended=7, now=_NOW)
        assert ledger.progress_counts().total == 3  # unfiltered — everything
        assert ledger.progress_counts(markets=None, intervals=None).total == 3
        scoped = ledger.progress_counts(
            markets=frozenset({"groww-option", "groww-future"}), intervals=("1minute",)
        )
        assert scoped.total == 1
        assert scoped.bars == 42  # never the spot 999 nor the 1day 7
        assert scoped.current == f"{option.security_id} 1minute"
        # A filter matching nothing folds honestly to all-zero.
        nothing = ledger.progress_counts(markets=frozenset({"no-such-market"}))
        assert (nothing.total, nothing.bars, nothing.current) == (0, 0, "")


class TestProgressCountsSymbols:
    """`progress_counts(symbols=…)` — the per-group scope filter
    (DEC-PULL-SIX-STEP-ORDER-001), congruent with `markets`/`intervals`."""

    def test_symbols_filter_counts_only_matching_rows(self) -> None:
        ledger = WindowLedger()
        nifty = _k("NIFTY", "1minute", date(2020, 1, 1), date(2020, 6, 29))
        sensex = _k("SENSEX", "1minute", date(2020, 1, 1), date(2020, 6, 29))
        adani = _k("ADANIENT", "1minute", date(2020, 1, 1), date(2020, 6, 29))
        ledger.record(nifty, raw_bars=100, error_type=None, bars_appended=100, now=_NOW)
        ledger.record(sensex, raw_bars=50, error_type=None, bars_appended=50, now=_LATER)
        ledger.record(adani, raw_bars=7, error_type=None, bars_appended=7, now=_NOW)
        scoped = ledger.progress_counts(symbols=frozenset({"NIFTY", "SENSEX"}))
        assert scoped.total == 2
        assert scoped.settled == 2
        assert scoped.bars == 150  # never ADANIENT's 7
        assert scoped.current == "SENSEX 1minute"

    def test_symbols_none_is_byte_identical_to_unfiltered(self) -> None:
        ledger = WindowLedger()
        a = _k("NIFTY", "1minute", date(2020, 1, 1), date(2020, 6, 29))
        b = _k("ADANIENT", "1minute", date(2020, 1, 1), date(2020, 6, 29))
        ledger.record(a, raw_bars=10, error_type=None, bars_appended=10, now=_NOW)
        ledger.plan(b, now=_NOW)
        assert ledger.progress_counts(symbols=None) == ledger.progress_counts()

    def test_symbols_filter_matching_nothing_is_all_zero(self) -> None:
        ledger = WindowLedger()
        key = _k("NIFTY", "1minute", date(2020, 1, 1), date(2020, 6, 29))
        ledger.record(key, raw_bars=10, error_type=None, bars_appended=10, now=_NOW)
        nothing = ledger.progress_counts(symbols=frozenset({"NO-SUCH-SYMBOL"}))
        assert (nothing.total, nothing.bars, nothing.current) == (0, 0, "")

    def test_indices_group_reads_clean_despite_planned_stock_rows(self) -> None:
        """The ledger-wide-pollution regression (DEC-PULL-SIX-STEP-ORDER-001).

        The six-step spot pull settles the INDICES group first while the STOCKS
        group's windows are still PLANNED in the SAME shared ledger. Unfiltered,
        the indices group's until-clean outstanding read
        (``total - settled > 0``) would see the stock PLANNED rows and burn one
        vendor-call-free ``stop_no_progress`` round; scoped to its own symbols
        it reads outstanding == 0 → ``stop_clean`` honestly.
        """
        ledger = WindowLedger()
        nifty = _k("NIFTY", "1minute", date(2020, 1, 1), date(2020, 6, 29))
        adani = _k("ADANIENT", "1minute", date(2020, 1, 1), date(2020, 6, 29))
        tcs = _k("TCS", "1minute", date(2020, 1, 1), date(2020, 6, 29))
        ledger.record(nifty, raw_bars=100, error_type=None, bars_appended=100, now=_NOW)
        ledger.plan(adani, now=_NOW)
        ledger.plan(tcs, now=_NOW)
        # Unfiltered: the stock PLANNED rows pollute the indices denominator.
        unfiltered = ledger.progress_counts()
        assert unfiltered.total - unfiltered.settled == 2
        # Group-scoped: the indices group is honestly CLEAN.
        indices = ledger.progress_counts(symbols=frozenset({"NIFTY"}))
        assert indices.total - indices.settled == 0
        assert indices.planned == 0
        # And the stocks group sees exactly its own outstanding rows.
        stocks = ledger.progress_counts(symbols=frozenset({"ADANIENT", "TCS"}))
        assert stocks.total == stocks.planned == 2

    def test_symbols_composes_with_markets_and_intervals(self) -> None:
        ledger = WindowLedger()
        spot = WindowKey(
            market="groww",
            security_id="NIFTY",
            interval="1minute",
            start=date(2020, 1, 1),
            end=date(2020, 6, 29),
        )
        option = WindowKey(
            market="groww-option",
            security_id="NSE-NIFTY-25Jan24-21000-CE",
            interval="1minute",
            start=date(2020, 1, 1),
            end=date(2020, 6, 29),
        )
        ledger.record(spot, raw_bars=9, error_type=None, bars_appended=9, now=_NOW)
        ledger.record(option, raw_bars=4, error_type=None, bars_appended=4, now=_NOW)
        scoped = ledger.progress_counts(
            markets=frozenset({"groww"}),
            intervals=("1minute",),
            symbols=frozenset({"NIFTY"}),
        )
        assert (scoped.total, scoped.bars) == (1, 9)
        # The same symbol under a non-matching market folds to nothing.
        cross = ledger.progress_counts(
            markets=frozenset({"groww-option"}), symbols=frozenset({"NIFTY"})
        )
        assert cross.total == 0


class TestStreakTotal:
    """`streak_total` — the O(N) summed empty_streak fold (DEC-UNTIL-CLEAN-ALL-OUTSTANDING-001)."""

    def test_empty_ledger_is_zero(self) -> None:
        assert WindowLedger().streak_total() == 0

    def test_sums_empty_streaks_across_windows(self) -> None:
        ledger = WindowLedger()
        # One window seen EMPTY twice → empty_streak 2; another EMPTY once → 1.
        a = _key("NIFTY", "1day")
        b = _key("BANKNIFTY", "1day")
        ledger.record(a, raw_bars=0, error_type=None, bars_appended=0, now=_NOW)
        ledger.record(a, raw_bars=0, error_type=None, bars_appended=0, now=_NOW)
        ledger.record(b, raw_bars=0, error_type=None, bars_appended=0, now=_NOW)
        assert ledger.streak_total() == 3

    def test_success_resets_a_windows_contribution(self) -> None:
        ledger = WindowLedger()
        k = _key("NIFTY", "1day")
        ledger.record(k, raw_bars=0, error_type=None, bars_appended=0, now=_NOW)
        assert ledger.streak_total() == 1
        ledger.record(k, raw_bars=5, error_type=None, bars_appended=5, now=_NOW)  # SUCCESS
        assert ledger.streak_total() == 0

    def test_symbols_filter_scopes_the_fold(self) -> None:
        """`symbols=` folds ONLY the member security_ids (DEC-PULL-SIX-STEP-ORDER-001)."""
        ledger = WindowLedger()
        a = _key("NIFTY", "1minute")
        b = _key("ADANIENT", "1minute")
        # NIFTY EMPTY twice → streak 2; ADANIENT EMPTY once → streak 1.
        ledger.record(a, raw_bars=0, error_type=None, bars_appended=0, now=_NOW)
        ledger.record(a, raw_bars=0, error_type=None, bars_appended=0, now=_NOW)
        ledger.record(b, raw_bars=0, error_type=None, bars_appended=0, now=_NOW)
        assert ledger.streak_total(symbols=frozenset({"NIFTY"})) == 2
        assert ledger.streak_total(symbols=frozenset({"ADANIENT"})) == 1
        assert ledger.streak_total(symbols=frozenset({"NIFTY", "ADANIENT"})) == 3

    def test_none_is_byte_identical_to_unfiltered(self) -> None:
        """``symbols=None`` (the default) folds everything — byte-identical to today."""
        ledger = WindowLedger()
        ledger.record(_key("NIFTY"), raw_bars=0, error_type=None, bars_appended=0, now=_NOW)
        ledger.record(_key("TCS"), raw_bars=0, error_type=None, bars_appended=0, now=_NOW)
        assert ledger.streak_total(symbols=None) == ledger.streak_total() == 2

    def test_empty_collection_folds_nothing(self) -> None:
        ledger = WindowLedger()
        ledger.record(_key("NIFTY"), raw_bars=0, error_type=None, bars_appended=0, now=_NOW)
        assert ledger.streak_total(symbols=frozenset()) == 0

    def test_foreign_group_late_tail_never_perturbs_the_scoped_total(self) -> None:
        """The group-scoped until-clean regression (DEC-PULL-SIX-STEP-ORDER-001).

        In the default async-pipeline mode a PRIOR group's consumer tails can
        still drain while the current group's until-clean loop runs: a foreign
        late EMPTY tail bumps that group's streak (+1) and a foreign late
        SUCCESS tail RESETS one to 0 — so the LEDGER-WIDE fold moved between
        the current group's rounds (masking a stall / faking progress). The
        GROUP-scoped fold must be INVARIANT under any foreign-record churn.
        """
        ledger = WindowLedger()
        indices = frozenset({"NIFTY"})
        nifty = _key("NIFTY", "1minute")
        adani = _key("ADANIENT", "1minute")
        # Current (indices) group: one EMPTY round so far → scoped streak 1.
        ledger.record(nifty, raw_bars=0, error_type=None, bars_appended=0, now=_NOW)
        # Foreign (stocks) group already carries a streak of 1.
        ledger.record(adani, raw_bars=0, error_type=None, bars_appended=0, now=_NOW)
        scoped_before = ledger.streak_total(symbols=indices)
        wide_before = ledger.streak_total()
        assert (scoped_before, wide_before) == (1, 2)
        # Foreign late EMPTY tail drains → ADANIENT streak bumps to 2.
        ledger.record(adani, raw_bars=0, error_type=None, bars_appended=0, now=_NOW)
        assert ledger.streak_total(symbols=indices) == scoped_before
        assert ledger.streak_total() == wide_before + 1  # the wide fold DID move
        # Foreign late SUCCESS tail drains → ADANIENT streak RESETS to 0.
        ledger.record(adani, raw_bars=7, error_type=None, bars_appended=7, now=_NOW)
        assert ledger.streak_total(symbols=indices) == scoped_before
        assert ledger.streak_total() == scoped_before  # wide collapsed with the reset
        # The scoped fold still tracks the CURRENT group's own progress.
        ledger.record(nifty, raw_bars=0, error_type=None, bars_appended=0, now=_NOW)
        assert ledger.streak_total(symbols=indices) == 2


class TestSuppressSettle:
    """`record(suppress_settle=True)` — the UNTRUSTED empty (breaker tripped)."""

    def test_suppressed_empty_never_fast_settles(self) -> None:
        ledger = WindowLedger()
        status = ledger.record(
            _key(),
            raw_bars=0,
            error_type=None,
            bars_appended=0,
            now=_NOW,
            settle_empty=True,
            suppress_settle=True,
        )
        assert status == EMPTY  # NOT SKIPPED — the fast-settle is gated off
        assert not ledger.is_skippable(_key())
        assert ledger.records()[0].empty_streak == 0  # streak preserved, not bumped

    def test_suppressed_empty_preserves_streak(self) -> None:
        # Two genuine empties first (streak 2, one short of the threshold)…
        ledger = WindowLedger()
        ledger.record(_key(), raw_bars=0, error_type=None, bars_appended=0, now=_NOW)
        ledger.record(_key(), raw_bars=0, error_type=None, bars_appended=0, now=_NOW)
        assert ledger.records()[0].empty_streak == 2
        # …then a throttle-shaped (suppressed) empty: streak stays 2 and the
        # would-be threshold promotion does NOT fire.
        status = ledger.record(
            _key(),
            raw_bars=0,
            error_type=None,
            bars_appended=0,
            now=_LATER,
            suppress_settle=True,
        )
        assert status == EMPTY
        assert ledger.records()[0].empty_streak == 2
        assert not ledger.is_skippable(_key())

    def test_suppress_ignored_for_success(self) -> None:
        ledger = WindowLedger()
        status = ledger.record(
            _key(),
            raw_bars=5,
            error_type=None,
            bars_appended=5,
            now=_NOW,
            suppress_settle=True,
        )
        assert status == SUCCESS  # a data-bearing window settles normally

    def test_unsuppressed_threshold_promotion_still_fires(self) -> None:
        # Sanity: WITHOUT suppression the third genuine empty promotes to SKIPPED.
        ledger = WindowLedger()
        status = ""
        for _ in range(EMPTY_TERMINAL_THRESHOLD):
            status = ledger.record(_key(), raw_bars=0, error_type=None, bars_appended=0, now=_NOW)
        assert status == SKIPPED


class TestNewlySettledSkipped:
    """The per-instance settled-SKIPPED tally (honest-verdict source)."""

    def test_fast_settle_counts(self) -> None:
        ledger = WindowLedger()
        assert ledger.newly_settled_skipped == 0
        ledger.record(
            _key(), raw_bars=0, error_type=None, bars_appended=0, now=_NOW, settle_empty=True
        )
        assert ledger.newly_settled_skipped == 1

    def test_threshold_promotion_counts_once(self) -> None:
        ledger = WindowLedger()
        for _ in range(EMPTY_TERMINAL_THRESHOLD):
            ledger.record(_key(), raw_bars=0, error_type=None, bars_appended=0, now=_NOW)
        assert ledger.newly_settled_skipped == 1  # only the promoting record counts

    def test_known_gap_settle_counts_and_resettle_does_not(self) -> None:
        ledger = WindowLedger()
        ledger.settle_known_gap(_key(), now=_NOW, reason="DH-905")
        assert ledger.newly_settled_skipped == 1
        ledger.settle_known_gap(_key(), now=_LATER, reason="DH-905")  # idempotent re-settle
        assert ledger.newly_settled_skipped == 1  # no transition — not double-counted

    def test_success_and_failed_do_not_count(self) -> None:
        ledger = WindowLedger()
        ledger.record(_key(), raw_bars=5, error_type=None, bars_appended=5, now=_NOW)
        ledger.record(
            _key("BANKNIFTY"), raw_bars=0, error_type="GrowwTimeoutError", bars_appended=0, now=_NOW
        )
        assert ledger.newly_settled_skipped == 0

    def test_fresh_instance_starts_at_zero(self) -> None:
        # The tally is per-instance (per-pull): a round-tripped ledger starts clean.
        ledger = WindowLedger()
        ledger.record(
            _key(), raw_bars=0, error_type=None, bars_appended=0, now=_NOW, settle_empty=True
        )
        restored = WindowLedger.from_rows(ledger.to_rows())
        assert restored.newly_settled_skipped == 0


class TestReopenSkipped:
    """`reopen_skipped` — the un-cement path (DEC-EMPTY-WINDOW-CIRCUIT-BREAKER-001)."""

    def _settled(self) -> WindowLedger:
        ledger = WindowLedger()
        ledger.record(
            _key("NIFTY"),
            raw_bars=0,
            error_type=None,
            bars_appended=0,
            now=_NOW,
            settle_empty=True,
        )
        ledger.record(
            _key("BANKNIFTY"),
            raw_bars=0,
            error_type=None,
            bars_appended=0,
            now=_NOW,
            settle_empty=True,
        )
        return ledger

    def test_reopen_uncements_old_skipped(self) -> None:
        ledger = self._settled()
        assert ledger.is_skippable(_key("NIFTY"))
        count = ledger.reopen_skipped()
        assert count == 2
        assert not ledger.is_skippable(_key("NIFTY"))  # the next pull re-fetches
        assert not ledger.is_skippable(_key("BANKNIFTY"))
        rec = ledger.records()[0]
        assert rec.status == FAILED
        assert rec.empty_streak == 0  # full fresh confirmation budget
        assert rec.last_error == "REOPENED"
        assert rec.attempts == 1  # preserved — no fetch happened here

    def test_reopen_filters_by_symbol(self) -> None:
        ledger = self._settled()
        assert ledger.reopen_skipped(security_id="NIFTY") == 1
        assert not ledger.is_skippable(_key("NIFTY"))
        assert ledger.is_skippable(_key("BANKNIFTY"))  # untouched

    def test_reopen_filters_by_market(self) -> None:
        ledger = self._settled()
        assert ledger.reopen_skipped(market="groww-option") == 0  # no such namespace
        assert ledger.reopen_skipped(market="dhan") == 2  # _key()'s namespace

    def test_reopen_limit_bounds_the_sample(self) -> None:
        ledger = self._settled()
        assert ledger.reopen_skipped(limit=1) == 1  # the bounded re-verify sample
        # Deterministic order: BANKNIFTY sorts before NIFTY.
        assert not ledger.is_skippable(_key("BANKNIFTY"))
        assert ledger.is_skippable(_key("NIFTY"))
        assert ledger.reopen_skipped(limit=0) == 0  # a degenerate bound re-opens nothing

    def test_reopen_skips_non_skipped_rows(self) -> None:
        ledger = WindowLedger()
        ledger.record(_key(), raw_bars=5, error_type=None, bars_appended=5, now=_NOW)
        assert ledger.reopen_skipped() == 0  # SUCCESS rows never re-open
        assert ledger.is_skippable(_key())

    def test_reopen_is_idempotent(self) -> None:
        ledger = self._settled()
        assert ledger.reopen_skipped() == 2
        assert ledger.reopen_skipped() == 0  # FAILED rows no longer match

    def test_reopened_row_round_trips(self) -> None:
        ledger = self._settled()
        ledger.reopen_skipped(limit=1)
        restored = WindowLedger.from_rows(ledger.to_rows())
        assert restored.to_rows() == ledger.to_rows()

    def test_lake_seed_never_resurrects_a_reopened_row(self) -> None:
        # Wave-3 ordering (DEC-PULL-FORENSIC-WAVE3-001 #7) runs the cold-start lake
        # seed BEFORE plan_windows — but seed_lake_success is ABSENT-ONLY, and a
        # deliberately re-opened key always HAS a row (the FAILED demotion), so the
        # seed can never flip it back to a settled SUCCESS behind the operator.
        ledger = self._settled()
        assert ledger.reopen_skipped(security_id="NIFTY") == 1
        assert ledger.seed_lake_success(_key("NIFTY"), bars=42, now=_LATER) is False
        rec = next(r for r in ledger.records() if r.security_id == "NIFTY")
        assert rec.status == FAILED  # the demotion stands — the next pull re-fetches
        assert not ledger.is_skippable(_key("NIFTY"))

    def test_reopened_keys_tombstone_the_demotions(self) -> None:
        # The merge-on-save feed (DEC-WINDOW-LEDGER-RESCUE-001 interplay): every
        # demoted key is snapshot in reopened_keys() so the on-disk SKIPPED copy's
        # more-settled merge rank cannot resurrect it; untouched rows never appear.
        ledger = self._settled()
        assert ledger.reopened_keys() == frozenset()
        assert ledger.reopen_skipped(security_id="NIFTY") == 1
        reopened = ledger.reopened_keys()
        assert len(reopened) == 1
        (key,) = reopened
        assert key[1] == "NIFTY"  # only the demoted key — BANKNIFTY stays settled

    def test_reopened_window_can_resettle_honestly(self) -> None:
        # A re-opened window that is STILL genuinely empty on the next pull re-settles
        # through the normal machinery (here: the old-gap fast-settle) — bounded, not
        # an endless re-open loop.
        ledger = self._settled()
        ledger.reopen_skipped(security_id="NIFTY")
        status = ledger.record(
            _key("NIFTY"),
            raw_bars=0,
            error_type=None,
            bars_appended=0,
            now=_LATER,
            settle_empty=True,
        )
        assert status == SKIPPED


class TestIsExpectedEmptyRecheck:
    """`is_expected_empty_recheck` — the mass-empty breaker's ledger-knowledge hint."""

    def test_prior_empty_row_is_expected(self) -> None:
        # A window a prior pull already saw EMPTY: re-confirming it empty is the
        # expected outcome (the empty-dominated RESUME shape), never throttle evidence.
        ledger = WindowLedger()
        ledger.record(_key(), raw_bars=0, error_type=None, bars_appended=0, now=_NOW)
        assert ledger.records()[0].status == EMPTY
        assert ledger.is_expected_empty_recheck(_key())

    def test_reopened_row_is_expected(self) -> None:
        # A REOPENED-marked row is a deliberate operator re-check (minor M-1):
        # breaker-exempt, so reopen + re-pull can never re-trip endlessly.
        ledger = WindowLedger()
        ledger.record(
            _key(), raw_bars=0, error_type=None, bars_appended=0, now=_NOW, settle_empty=True
        )
        assert ledger.reopen_skipped() == 1
        row = ledger.records()[0]
        assert row.status == FAILED
        assert row.last_error == REOPENED_MARKER
        assert ledger.is_expected_empty_recheck(_key())

    def test_absent_planned_and_ordinary_failed_are_not_expected(self) -> None:
        ledger = WindowLedger()
        assert not ledger.is_expected_empty_recheck(_key())  # absent — informative
        ledger.plan(_key(), now=_NOW)
        assert not ledger.is_expected_empty_recheck(_key())  # PLANNED — never fetched
        ledger.record(
            _key(), raw_bars=0, error_type="GrowwRateLimitError", bars_appended=0, now=_NOW
        )
        # An ordinary FAILED (a 429) coming back empty IS informative — counted.
        assert not ledger.is_expected_empty_recheck(_key())

    def test_success_row_is_not_expected(self) -> None:
        # (A SUCCESS window is skipped before any fetch, but the rule stays honest.)
        ledger = WindowLedger()
        ledger.record(_key(), raw_bars=5, error_type=None, bars_appended=5, now=_NOW)
        assert not ledger.is_expected_empty_recheck(_key())


class TestRescueLedgerSurface:
    """DEC-WINDOW-LEDGER-RESCUE-001 — the load-failed flag + prune tombstones."""

    def test_load_failed_defaults_false(self) -> None:
        assert WindowLedger().load_failed is False

    def test_pruned_keys_empty_by_default(self) -> None:
        assert WindowLedger().pruned_keys() == frozenset()

    def test_prune_records_tombstones(self) -> None:
        # Every pruned key is tombstoned so the merge-on-save can never resurrect
        # it from the on-disk copy (the DEC-WINDOW-TRAILING-GC-001 GC stays won).
        ledger = WindowLedger()
        orphan = _tkey(end=date(2026, 6, 9))
        ledger.record(orphan, raw_bars=5, error_type=None, bars_appended=5, now=_NOW)
        planned = _tkey(end=date(2026, 6, 10))
        ledger.plan(planned, now=_LATER)
        assert ledger.prune_superseded(_plan_index(planned)) == 1
        assert ledger.pruned_keys() == frozenset(
            {
                (
                    orphan.market,
                    orphan.security_id,
                    orphan.interval,
                    orphan.start.isoformat(),
                    orphan.end.isoformat(),
                )
            }
        )

    def test_no_prune_no_tombstones(self) -> None:
        ledger = WindowLedger()
        planned = _tkey(end=date(2026, 6, 10))
        ledger.record(planned, raw_bars=5, error_type=None, bars_appended=5, now=_NOW)
        assert ledger.prune_superseded(_plan_index(planned)) == 0
        assert ledger.pruned_keys() == frozenset()


class TestDirtyTracking:
    """DEC-LEDGER-DELTA-FLUSH-001 — the O(1)-per-mutation dirty-key stamps."""

    @staticmethod
    def _wk(key: WindowKey) -> tuple[str, str, str, str, str]:
        return (
            key.market,
            key.security_id,
            key.interval,
            key.start.isoformat(),
            key.end.isoformat(),
        )

    def test_fresh_and_restored_ledgers_start_clean(self) -> None:
        assert WindowLedger().dirty_rows() == []
        ledger = WindowLedger()
        ledger.record(_key(), raw_bars=1, error_type=None, bars_appended=1, now=_NOW)
        restored = WindowLedger.from_rows(ledger.to_rows())
        assert restored.dirty_rows() == []  # a load stamps nothing dirty

    def test_every_row_writing_mutator_stamps(self) -> None:
        ledger = WindowLedger()
        ledger.plan(_key("A"), now=_NOW)
        ledger.seed_lake_success(_key("B"), bars=5, now=_NOW)
        ledger.record(_key("C"), raw_bars=2, error_type=None, bars_appended=2, now=_NOW)
        ledger.settle_known_gap(_key("D"), now=_NOW, reason="DH-905")
        assert {row[1] for row in ledger.dirty_rows()} == {"A", "B", "C", "D"}
        # An idempotent re-plan of an existing key adds nothing new.
        ledger.clear_dirty(ledger.dirty_keys())
        ledger.plan(_key("A"), now=_LATER)
        assert ledger.dirty_rows() == []

    def test_reopen_demotion_stamps_dirty(self) -> None:
        ledger = WindowLedger()
        ledger.record(
            _key(), raw_bars=0, error_type=None, bars_appended=0, now=_NOW, settle_empty=True
        )
        ledger.clear_dirty(ledger.dirty_keys())
        assert ledger.reopen_skipped() == 1
        rows = ledger.dirty_rows()
        assert len(rows) == 1 and rows[0][5] == FAILED and rows[0][8] == REOPENED_MARKER

    def test_dirty_rows_sorted_and_skip_deleted_keys(self) -> None:
        ledger = WindowLedger()
        ledger.record(_key("ZEE"), raw_bars=1, error_type=None, bars_appended=1, now=_NOW)
        ledger.record(_key("ACME"), raw_bars=1, error_type=None, bars_appended=1, now=_NOW)
        assert [row[1] for row in ledger.dirty_rows()] == ["ACME", "ZEE"]  # key order
        # A dirty key whose row was pruned meanwhile is silently skipped (a
        # delta cannot express a deletion — the pull-end save owns prunes).
        with ledger._lock:  # test-only surgical delete
            del ledger._by_window[self._wk(_key("ACME"))]
        assert [row[1] for row in ledger.dirty_rows()] == ["ZEE"]

    def test_clear_dirty_clears_exactly_the_given_keys(self) -> None:
        ledger = WindowLedger()
        ledger.record(_key("A"), raw_bars=1, error_type=None, bars_appended=1, now=_NOW)
        ledger.record(_key("B"), raw_bars=1, error_type=None, bars_appended=1, now=_NOW)
        ledger.clear_dirty([self._wk(_key("A"))])
        assert [row[1] for row in ledger.dirty_rows()] == ["B"]
        assert ledger.dirty_keys() == frozenset({self._wk(_key("B"))})


class TestAuditReopenSkipped:
    """DEC-SKIPPED-AUDIT-ROUNDROBIN-001 — the mechanized ≤cap round-robin re-open."""

    @staticmethod
    def _skipped_ledger(symbols: list[str]) -> WindowLedger:
        ledger = WindowLedger()
        for symbol in symbols:
            ledger.record(
                _key(symbol),
                raw_bars=0,
                error_type=None,
                bars_appended=0,
                now=_NOW,
                settle_empty=True,
            )
        return ledger

    def test_default_cap_is_the_documented_25(self) -> None:
        from brutex.data.window_ledger import SKIPPED_AUDIT_PER_PULL

        assert SKIPPED_AUDIT_PER_PULL == 25

    def test_head_selection_without_a_cursor(self) -> None:
        ledger = self._skipped_ledger(["A", "B", "C", "D"])
        count, cursor = ledger.audit_reopen_skipped(start_after=None, cap=2)
        assert count == 2
        assert cursor is not None and cursor[1] == "B"  # last selected in sort order
        statuses = {r.security_id: r.status for r in ledger.records()}
        assert statuses == {"A": FAILED, "B": FAILED, "C": SKIPPED, "D": SKIPPED}
        # The demotion is the SAME body as the operator re-open: marker stamped,
        # streak reset, merge tombstones + dirty stamps recorded.
        demoted = [r for r in ledger.records() if r.status == FAILED]
        assert all(r.last_error == REOPENED_MARKER and r.empty_streak == 0 for r in demoted)
        assert len(ledger.reopened_keys()) == 2

    def test_round_robin_advances_then_wraps(self) -> None:
        ledger = self._skipped_ledger(["A", "B", "C"])
        _count, cursor = ledger.audit_reopen_skipped(start_after=None, cap=2)  # A, B
        # Model the NEXT pull: a fresh ledger with the same skipped population.
        nxt = self._skipped_ledger(["A", "B", "C"])
        count, cursor2 = nxt.audit_reopen_skipped(start_after=cursor, cap=2)
        assert count == 2
        statuses = {r.security_id: r.status for r in nxt.records()}
        # C (after the cursor) then WRAP to A — never the same head prefix twice.
        assert statuses == {"A": FAILED, "B": SKIPPED, "C": FAILED}
        assert cursor2 is not None and cursor2[1] == "A"  # last in rotation order

    def test_tail_fills_cap_exactly_without_wrapping(self) -> None:
        # Branch pin (DEC-SKIPPED-AUDIT-ROUNDROBIN-001): when the strictly-after
        # tail alone satisfies the cap, the wrap-to-head block is SKIPPED — no
        # head row is re-selected and the cursor advances within the tail.
        ledger = self._skipped_ledger(["A", "B", "C", "D"])
        _count, cursor = ledger.audit_reopen_skipped(start_after=None, cap=1)
        assert cursor is not None and cursor[1] == "A"
        # Model the NEXT pull: a fresh ledger with the same skipped population.
        nxt = self._skipped_ledger(["A", "B", "C", "D"])
        count, cursor2 = nxt.audit_reopen_skipped(start_after=cursor, cap=2)
        assert count == 2
        statuses = {r.security_id: r.status for r in nxt.records()}
        # Tail [B, C, D] fills cap=2 with [B, C] — A (head) and D untouched.
        assert statuses == {"A": SKIPPED, "B": FAILED, "C": FAILED, "D": SKIPPED}
        assert cursor2 is not None and cursor2[1] == "C"

    def test_cursor_past_the_tail_wraps_to_head(self) -> None:
        ledger = self._skipped_ledger(["A", "B"])
        _c, tail_cursor = ledger.audit_reopen_skipped(start_after=None, cap=2)
        assert tail_cursor is not None and tail_cursor[1] == "B"
        nxt = self._skipped_ledger(["A", "B"])
        count, cursor = nxt.audit_reopen_skipped(start_after=tail_cursor, cap=1)
        assert count == 1
        assert {r.security_id for r in nxt.records() if r.status == FAILED} == {"A"}
        assert cursor is not None and cursor[1] == "A"

    def test_no_skipped_rows_or_nonpositive_cap_is_a_noop(self) -> None:
        empty = WindowLedger()
        assert empty.audit_reopen_skipped(start_after=None, cap=5) == (0, None)
        ledger = self._skipped_ledger(["A"])
        assert ledger.audit_reopen_skipped(start_after=None, cap=0) == (0, None)
        assert ledger.records()[0].status == SKIPPED  # untouched

    def test_reopened_rows_are_breaker_exempt_rechecks(self) -> None:
        ledger = self._skipped_ledger(["A"])
        ledger.audit_reopen_skipped(start_after=None, cap=1)
        assert ledger.is_expected_empty_recheck(_key("A")) is True  # REOPENED marker
        assert ledger.is_skippable(_key("A")) is False  # the next pull re-fetches


class TestBarsNetNew:
    """DEC-INGEST-BOUNDARY-INTEGRITY-001: the persisted deduped net-new count."""

    def test_record_stores_bars_net_new(self) -> None:
        ledger = WindowLedger()
        ledger.record(
            _key(), raw_bars=5, error_type=None, bars_appended=5, bars_net_new=3, now=_NOW
        )
        rec = ledger.records()[0]
        assert rec.bars == 5  # the input count is UNCHANGED (the >0 gates read it)
        assert rec.bars_net_new == 3  # the deduped count is stored alongside

    def test_record_default_is_honest_unknown_none(self) -> None:
        # A caller with no sink result records None — never a fabricated 0.
        ledger = WindowLedger()
        ledger.record(_key(), raw_bars=5, error_type=None, bars_appended=5, now=_NOW)
        assert ledger.records()[0].bars_net_new is None

    def test_round_trip_carries_the_int_and_the_none(self) -> None:
        ledger = WindowLedger()
        ledger.record(
            _key("NIFTY"), raw_bars=5, error_type=None, bars_appended=5, bars_net_new=2, now=_NOW
        )
        ledger.record(_key("SENSEX"), raw_bars=5, error_type=None, bars_appended=5, now=_NOW)
        rows = ledger.to_rows()
        assert [r[13] for r in rows] == [2, None]  # the LAST tuple element
        restored = WindowLedger.from_rows(rows)
        assert restored.to_rows() == rows
        by_symbol = {r.security_id: r for r in restored.records()}
        assert by_symbol["NIFTY"].bars_net_new == 2
        assert by_symbol["SENSEX"].bars_net_new is None

    def test_plan_row_is_known_zero(self) -> None:
        ledger = WindowLedger()
        ledger.plan(_key(), now=_NOW)
        assert ledger.records()[0].bars_net_new == 0  # never appended (mirrors bars=0)

    def test_seed_lake_success_is_honest_unknown(self) -> None:
        # The cold-start seed proves bars EXIST but the original write's dedup
        # count is unrecorded — None, never fabricated.
        ledger = WindowLedger()
        ledger.seed_lake_success(_key(), bars=42, now=_NOW)
        assert ledger.records()[0].bars_net_new is None

    def test_settle_known_gap_preserves_prior_value(self) -> None:
        ledger = WindowLedger()
        ledger.record(
            _key(), raw_bars=5, error_type=None, bars_appended=5, bars_net_new=4, now=_NOW
        )
        ledger.settle_known_gap(_key(), now=_LATER, reason="DH-905")
        assert ledger.records()[0].bars_net_new == 4  # no fetch here — preserved

    def test_settle_known_gap_without_prior_is_none(self) -> None:
        ledger = WindowLedger()
        ledger.settle_known_gap(_key(), now=_NOW, reason="DH-905")
        assert ledger.records()[0].bars_net_new is None

    def test_reopen_skipped_preserves_prior_value(self) -> None:
        ledger = WindowLedger()
        ledger.settle_known_gap(_key(), now=_NOW, reason="DH-905")  # SKIPPED, None
        ledger.record(
            _key("SENSEX"), raw_bars=0, error_type=None, bars_appended=0, bars_net_new=0, now=_NOW
        )
        # Build a SKIPPED row WITH a recorded net-new, then reopen it.
        other = _key("BANKNIFTY")
        for _ in range(EMPTY_TERMINAL_THRESHOLD):
            ledger.record(
                other, raw_bars=0, error_type=None, bars_appended=0, bars_net_new=0, now=_NOW
            )
        assert ledger.reopen_skipped(security_id="BANKNIFTY") == 1
        reopened = {r.security_id: r for r in ledger.records()}["BANKNIFTY"]
        assert reopened.status == FAILED
        assert reopened.bars_net_new == 0  # preserved through the demotion


class TestHasWindow:
    """`has_window` — the cold-start seed's O(1) existence probe
    (DEC-DATA-HONESTY-ALLFILTERED-SEED-001)."""

    def test_absent_key_reads_false(self) -> None:
        assert WindowLedger().has_window(_key()) is False

    def test_any_status_reads_true_and_never_mutates(self) -> None:
        ledger = WindowLedger()
        ledger.plan(_key(), now=_NOW)  # PLANNED
        ledger.record(
            _key("SENSEX"), raw_bars=0, error_type=None, bars_appended=0, now=_NOW
        )  # EMPTY
        ledger.record(
            _key("BANKNIFTY"),
            raw_bars=0,
            error_type="GrowwTimeoutError",
            bars_appended=0,
            now=_NOW,
        )  # FAILED
        before = ledger.records()
        assert ledger.has_window(_key()) is True
        assert ledger.has_window(_key("SENSEX")) is True
        assert ledger.has_window(_key("BANKNIFTY")) is True
        assert ledger.has_window(_key("INDIAVIX")) is False
        assert ledger.records() == before  # a probe never writes

    def test_distinct_interval_is_a_distinct_key(self) -> None:
        ledger = WindowLedger()
        ledger.plan(_key(interval="1minute"), now=_NOW)
        assert ledger.has_window(_key(interval="1day")) is False


class TestDemotedSettleReasons:
    """`DEMOTED_SETTLE_REASONS` — the canonical demotion reason-tag set the
    /coverage label keys on (DEC-DATA-HONESTY-ALLFILTERED-SEED-001)."""

    def test_enumerates_exactly_the_channel_tags_plus_fallback(self) -> None:
        from brutex.data.interior_gap_detect import INTERIOR_GAP
        from brutex.data.partial_window_detect import SUSPECTED_PARTIAL
        from brutex.data.session import ALL_FILTERED

        # The three detector tags PLUS "DEGRADED" — record()'s defensive
        # fallback for a degraded=True call with no reason (latent: both
        # production spines always pass a concrete tag). Enumerated so a
        # reason-less demotion settle can never fall through to the
        # "vendor confirmed no trades" label (wiring-review fold, PR #1158).
        expected = frozenset({ALL_FILTERED, INTERIOR_GAP, SUSPECTED_PARTIAL, "DEGRADED"})
        assert expected == DEMOTED_SETTLE_REASONS

    def test_reasonless_degraded_record_stamps_a_demoted_reason(self) -> None:
        # The fallback stamp itself is IN the set — the invariant the label
        # branch relies on: every degraded-channel last_error ∈ the frozenset.
        ledger = WindowLedger()
        ledger.record(_key(), raw_bars=5, error_type=None, bars_appended=5, now=_NOW, degraded=True)
        rec = ledger.records()[0]
        assert rec.last_error == "DEGRADED"
        assert rec.last_error in DEMOTED_SETTLE_REASONS

    def test_expired_window_stamp_is_not_a_demoted_reason(self) -> None:
        # The two SKIPPED label branches must never collide.
        assert expired_window_settle_reason(1) not in DEMOTED_SETTLE_REASONS


class TestDegradedOldWindowInteraction:
    """The F&O spine passes BOTH `degraded` (ALL_FILTERED) and `old_window` on one
    record call — pin the cross-arm interaction (DEC-DATA-HONESTY-ALLFILTERED-SEED-001,
    refuter F-2 fold; single-confirm settle per DEC-OLD-EMPTY-SINGLE-CONFIRM-001)."""

    def test_all_filtered_demotion_then_old_clean_empty_settles_single_confirm(self) -> None:
        # Pull 1: an all-filtered OLD window demotes FAILED (streak 1) — the
        # old-window hint is IGNORED for a degraded outcome (EMPTY-only arm).
        ledger = WindowLedger()
        s1 = ledger.record(
            _key(),
            raw_bars=3,
            error_type=None,
            bars_appended=0,
            now=_NOW,
            degraded=True,
            degraded_reason="ALL_FILTERED",
            old_window=True,
        )
        assert s1 == FAILED
        assert ledger.records()[0].empty_streak == 1
        # Pull 2: a genuine clean EMPTY on the old window settles SKIPPED off THIS
        # single observation (single-confirm — the degraded bump was never needed).
        s2 = ledger.record(
            _key(),
            raw_bars=0,
            error_type=None,
            bars_appended=0,
            now=_LATER,
            old_window=True,
        )
        assert s2 == SKIPPED
        rec = ledger.records()[0]
        assert is_expired_window_settle_reason(rec.last_error)
        # HONEST-envelope pin (documented in record()): the stamp's clean-attempt
        # COUNT is streak-derived, so the prior DEGRADED bump inflates it to 2
        # even though only ONE genuine clean EMPTY landed — accepted + pinned.
        assert rec.last_error == expired_window_settle_reason(2)

    def test_all_filtered_demotion_alone_never_settles_old_window(self) -> None:
        # A degraded (all-filtered) outcome NEVER rides the old-empty settle arm —
        # three demotions settle via the degraded threshold, not the expired stamp.
        ledger = WindowLedger()
        status = ""
        for _ in range(EMPTY_TERMINAL_THRESHOLD):
            status = ledger.record(
                _key(),
                raw_bars=2,
                error_type=None,
                bars_appended=0,
                now=_NOW,
                degraded=True,
                degraded_reason="ALL_FILTERED",
                old_window=True,
            )
        assert status == SKIPPED
        rec = ledger.records()[0]
        assert rec.last_error == "ALL_FILTERED"  # the demotion reason, never the stamp
        assert not is_expired_window_settle_reason(rec.last_error)


class TestLedgerRecordRow:
    """``ledger_record_row`` — the ONE row-shape source (DEC-SUSPICION-SWEEP-FIXES-001 D3)."""

    def test_to_rows_delegates_to_the_module_function(self) -> None:
        """``to_rows()`` equals a per-record ``ledger_record_row`` projection —
        so the /db grid's cached projection can never drift from the store shape."""
        from brutex.data.window_ledger import ledger_record_row

        ledger = WindowLedger()
        ledger.record(
            _key(),
            raw_bars=5,
            error_type=None,
            bars_appended=5,
            bars_net_new=3,
            now=_NOW,
        )
        records = ledger.records()
        assert ledger.to_rows() == [ledger_record_row(r) for r in records]
        row = ledger_record_row(records[0])
        assert len(row) == 14
        assert row[0] == records[0].market
        assert row[13] == 3  # bars_net_new is the LAST field (back-compat append order)
