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
* the ``from_vix_bars`` / ``from_bars`` constructors + the series name.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import structlog
from brutex.data.models import CandleBar
from brutex.engine.reference_series import (
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
