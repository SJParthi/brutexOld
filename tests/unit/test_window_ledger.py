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
    EMPTY_TERMINAL_THRESHOLD,
    PLANNED,
    SETTLED_STATUSES,
    SKIPPED,
    SUCCESS,
    WindowLedger,
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

    def test_a_non_empty_outcome_resets_the_streak(self) -> None:
        ledger = WindowLedger()
        ledger.record(_key(), raw_bars=0, error_type=None, bars_appended=0, now=_NOW)  # EMPTY 1
        ledger.record(_key(), raw_bars=0, error_type=None, bars_appended=0, now=_NOW)  # EMPTY 2
        # A transient FAIL between empties RESETS the streak to 0.
        ledger.record(_key(), raw_bars=0, error_type="DhanTimeoutError", bars_appended=0, now=_NOW)
        assert ledger.records()[0].empty_streak == 0
        # So the next EMPTY is streak 1 again — three MORE consecutive empties needed.
        status = ledger.record(_key(), raw_bars=0, error_type=None, bars_appended=0, now=_NOW)
        assert status == "EMPTY"
        assert ledger.records()[0].empty_streak == 1

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
