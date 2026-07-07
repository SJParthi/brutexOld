"""Unit tests for the generic reference-series trade stamper (``brutex.engine.reference_series``).

100% line + branch coverage of :class:`~brutex.engine.reference_series.ReferenceSeries`
and :class:`~brutex.engine.reference_series.TradeReferenceStamper` — the COMMON ·
DYNAMIC mechanism that stamps the India VIX level on every trade (REFERENCE
ONLY, never a signal input; DEC-VIX-TRADE-STAMP-001). Covers:

* O(1) ``timestamp → close`` lookup (present + absent instant);
* duplicate-timestamp keep-FIRST collapse + warning (DEC-LAKE-READ-UNIQUE-001);
* :meth:`TradeReferenceStamper.stamp` with VIX present at both ends, missing at
  entry, missing at exit, missing at both (→ ``None`` + a logged warning, never
  a fabricated value);
* the ``from_vix_bars`` / ``from_bars`` constructors + the series name;
* the MEMOISED + CAPPED miss-warning scheme (DEC-VIX-STAMP-WIRE-001 Stage D):
  per-distinct-instant memo, the :data:`MISS_WARN_DETAIL_CAP` detail cap, the
  one ``vix_miss_capped`` sentinel, the honest ``miss_count`` tally, bounded
  memo memory, and spawn-pool picklability.
"""

from __future__ import annotations

import pickle
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import structlog
from brutex.data.models import CandleBar
from brutex.engine.reference_series import (
    MISS_WARN_DETAIL_CAP,
    VIX_SERIES_NAME,
    ReferenceSeries,
    TradeReferenceStamper,
)
from structlog.typing import EventDict

_T0 = datetime(2024, 1, 2, 3, 45, tzinfo=UTC)  # 09:15 IST


def _vix_bar(minute: int, close: str) -> CandleBar:
    """A flat VIX bar ``minute`` minutes after the 09:15 anchor, closing at *close*."""
    value = Decimal(close)
    return CandleBar(
        timestamp=_T0 + timedelta(minutes=minute),
        open=value,
        high=value,
        low=value,
        close=value,
        volume=0,
        open_interest=None,
    )


# ────────────────────────────────────────────────────────────────────────
# ReferenceSeries
# ────────────────────────────────────────────────────────────────────────


class TestReferenceSeries:
    def test_from_bars_builds_timestamp_close_map(self) -> None:
        series = ReferenceSeries.from_bars(
            VIX_SERIES_NAME, [_vix_bar(0, "15.10"), _vix_bar(1, "15.20")]
        )
        assert series.name == VIX_SERIES_NAME
        assert series.close_by_ts == {
            _T0: Decimal("15.10"),
            _T0 + timedelta(minutes=1): Decimal("15.20"),
        }

    def test_level_at_present_returns_close(self) -> None:
        series = ReferenceSeries.from_bars(VIX_SERIES_NAME, [_vix_bar(0, "15.10")])
        assert series.level_at(_T0) == Decimal("15.10")

    def test_level_at_absent_returns_none(self) -> None:
        series = ReferenceSeries.from_bars(VIX_SERIES_NAME, [_vix_bar(0, "15.10")])
        assert series.level_at(_T0 + timedelta(minutes=99)) is None

    def test_duplicate_timestamp_keeps_first_and_warns(self) -> None:
        """A repeated timestamp keeps the FIRST bar's close + warns (keep-FIRST).

        DEC-LAKE-READ-UNIQUE-001 flipped the old silent LAST-wins tie-break: a
        poisoned-era duplicate re-statement must never silently replace the
        real close, and the collapse is warned via the COMMON read gate.
        """
        with _capture_logs() as logs:
            series = ReferenceSeries.from_bars(
                VIX_SERIES_NAME, [_vix_bar(0, "15.10"), _vix_bar(0, "15.99")]
            )
        assert series.level_at(_T0) == Decimal("15.10")
        dedup = [e for e in logs if e["event"] == "lake.read.duplicates_dropped"]
        assert len(dedup) == 1
        assert dedup[0]["dropped"] == 1
        assert dedup[0]["conflict_count"] == 1
        assert dedup[0]["source"] == f"reference_series:{VIX_SERIES_NAME}"

    def test_empty_series_is_all_misses(self) -> None:
        series = ReferenceSeries.from_bars(VIX_SERIES_NAME, [])
        assert series.close_by_ts == {}
        assert series.level_at(_T0) is None


# ────────────────────────────────────────────────────────────────────────
# TradeReferenceStamper
# ────────────────────────────────────────────────────────────────────────


class TestTradeReferenceStamper:
    def test_from_vix_bars_names_the_vix_series(self) -> None:
        stamper = TradeReferenceStamper.from_vix_bars([_vix_bar(0, "15.10")])
        assert stamper.vix.name == VIX_SERIES_NAME

    def test_stamp_both_present(self) -> None:
        stamper = TradeReferenceStamper.from_vix_bars([_vix_bar(0, "15.10"), _vix_bar(5, "15.50")])
        entry, exit_ = stamper.stamp(_T0, _T0 + timedelta(minutes=5))
        assert entry == Decimal("15.10")
        assert exit_ == Decimal("15.50")

    def test_stamp_missing_entry_warns_and_returns_none(self) -> None:
        stamper = TradeReferenceStamper.from_vix_bars([_vix_bar(5, "15.50")])
        with _capture_logs() as logs:
            entry, exit_ = stamper.stamp(_T0, _T0 + timedelta(minutes=5))
        assert entry is None
        assert exit_ == Decimal("15.50")
        misses = [e for e in logs if e["event"] == "reference_series.vix_miss"]
        assert len(misses) == 1
        assert misses[0]["at"] == "entry"
        assert misses[0]["series"] == VIX_SERIES_NAME

    def test_stamp_missing_exit_warns_and_returns_none(self) -> None:
        stamper = TradeReferenceStamper.from_vix_bars([_vix_bar(0, "15.10")])
        with _capture_logs() as logs:
            entry, exit_ = stamper.stamp(_T0, _T0 + timedelta(minutes=5))
        assert entry == Decimal("15.10")
        assert exit_ is None
        misses = [e for e in logs if e["event"] == "reference_series.vix_miss"]
        assert len(misses) == 1
        assert misses[0]["at"] == "exit"

    def test_stamp_missing_both_warns_twice(self) -> None:
        stamper = TradeReferenceStamper.from_vix_bars([])
        with _capture_logs() as logs:
            entry, exit_ = stamper.stamp(_T0, _T0 + timedelta(minutes=5))
        assert entry is None
        assert exit_ is None
        misses = [e for e in logs if e["event"] == "reference_series.vix_miss"]
        assert {m["at"] for m in misses} == {"entry", "exit"}
        assert len(misses) == 2


# ────────────────────────────────────────────────────────────────────────
# Miss-WARN memo + cap (DEC-VIX-STAMP-WIRE-001 Stage D)
# ────────────────────────────────────────────────────────────────────────


def _miss_at(minute: int) -> datetime:
    """A distinct instant guaranteed ABSENT from any fixture series here."""
    return _T0 + timedelta(days=365, minutes=minute)


class TestMissWarnMemoCap:
    def test_miss_count_starts_at_zero_and_hits_never_count(self) -> None:
        stamper = TradeReferenceStamper.from_vix_bars([_vix_bar(0, "15.10")])
        assert stamper.miss_count == 0
        stamper.stamp(_T0, _T0)  # both ends hit
        assert stamper.miss_count == 0

    def test_repeated_miss_at_the_same_instant_warns_once_but_counts_each(self) -> None:
        stamper = TradeReferenceStamper.from_vix_bars([])
        with _capture_logs() as logs:
            stamper.stamp(_miss_at(0), _miss_at(0))  # entry + exit, SAME instant
            stamper.stamp(_miss_at(0), _miss_at(0))  # re-missed on a later trade
        misses = [e for e in logs if e["event"] == "reference_series.vix_miss"]
        assert len(misses) == 1  # memoised per distinct instant
        assert misses[0]["at"] == "entry"  # the FIRST occurrence carries the detail
        assert stamper.miss_count == 4  # …but every occurrence is tallied

    def test_distinct_misses_up_to_the_cap_each_warn_in_detail(self) -> None:
        stamper = TradeReferenceStamper.from_vix_bars([])
        with _capture_logs() as logs:
            for i in range(MISS_WARN_DETAIL_CAP):
                stamper.stamp(_miss_at(i), _miss_at(i))
        misses = [e for e in logs if e["event"] == "reference_series.vix_miss"]
        assert [m["ts"] for m in misses] == [_miss_at(i) for i in range(MISS_WARN_DETAIL_CAP)]
        assert not [e for e in logs if e["event"] == "reference_series.vix_miss_capped"]

    def test_first_distinct_miss_past_the_cap_emits_one_sentinel(self) -> None:
        stamper = TradeReferenceStamper.from_vix_bars([])
        with _capture_logs() as logs:
            for i in range(MISS_WARN_DETAIL_CAP + 1):
                stamper.stamp(_miss_at(i), _miss_at(i))
        capped = [e for e in logs if e["event"] == "reference_series.vix_miss_capped"]
        assert len(capped) == 1
        assert capped[0]["series"] == VIX_SERIES_NAME
        assert capped[0]["distinct_misses"] == MISS_WARN_DETAIL_CAP
        total_warns = [e for e in logs if str(e["event"]).startswith("reference_series.vix_miss")]
        assert len(total_warns) == MISS_WARN_DETAIL_CAP + 1  # the log-volume bound

    def test_post_sentinel_misses_are_silent_but_counted(self) -> None:
        stamper = TradeReferenceStamper.from_vix_bars([])
        n_distinct = MISS_WARN_DETAIL_CAP + 10
        with _capture_logs() as logs:
            for i in range(n_distinct):
                stamper.stamp(_miss_at(i), _miss_at(i))
            stamper.stamp(_miss_at(0), _miss_at(0))  # post-cap re-miss of a warned instant
        assert len(logs) == MISS_WARN_DETAIL_CAP + 1  # cap details + one sentinel, nothing else
        assert stamper.miss_count == 2 * (n_distinct + 1)

    def test_memo_memory_is_bounded_at_the_cap(self) -> None:
        stamper = TradeReferenceStamper.from_vix_bars([])
        with _capture_logs():
            for i in range(MISS_WARN_DETAIL_CAP * 5):
                stamper.stamp(_miss_at(i), _miss_at(i))
        assert len(stamper._miss_memo.warned_instants) == MISS_WARN_DETAIL_CAP

    def test_equality_ignores_memo_state(self) -> None:
        fresh = TradeReferenceStamper.from_vix_bars([_vix_bar(0, "15.10")])
        missed = TradeReferenceStamper.from_vix_bars([_vix_bar(0, "15.10")])
        with _capture_logs():
            missed.stamp(_miss_at(0), _miss_at(0))
        assert missed.miss_count == 2
        assert fresh == missed  # the memo is bookkeeping, not identity

    def test_stamper_pickles_with_memo_state_and_keeps_working(self) -> None:
        """Spawn-pool picklability (sweep_parallel workers) survives the memo."""
        stamper = TradeReferenceStamper.from_vix_bars([_vix_bar(0, "15.10")])
        with _capture_logs():
            stamper.stamp(_miss_at(0), _miss_at(0))
        clone = pickle.loads(pickle.dumps(stamper))  # noqa: S301 — self-produced bytes
        assert isinstance(clone, TradeReferenceStamper)
        assert clone.miss_count == 2  # memo state travels with the stamper
        assert clone.vix.level_at(_T0) == Decimal("15.10")
        with _capture_logs() as logs:
            clone.stamp(_miss_at(0), _miss_at(1))  # memoised + one new detail
        assert [e["event"] for e in logs] == ["reference_series.vix_miss"]
        assert clone.miss_count == 4


# ────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────


class _capture_logs:
    """Context manager capturing structlog events into a list (configure/restore)."""

    def __init__(self) -> None:
        self._cap = structlog.testing.LogCapture()
        self._saved: dict[str, object] | None = None

    def __enter__(self) -> list[EventDict]:
        self._saved = structlog.get_config()
        structlog.configure(processors=[self._cap])
        return self._cap.entries

    def __exit__(self, *_exc: object) -> None:
        assert self._saved is not None
        structlog.configure(**self._saved)  # type: ignore[arg-type]
