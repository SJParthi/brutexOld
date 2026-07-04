"""DEC-PULL-PROGRESS-HONESTY-001 — market/interval-filtered ``progress_counts``.

Fails-before framing: the ledger is CROSS-RUN and CROSS-MARKET (spot + F&O share
one durable ledger), and ``progress_counts()`` folded EVERY record — so a live F&O
progress fold would have counted thousands of spot windows in its denominator (the
pinned regression below proves the pollution). The new keyword-only ``markets`` /
``intervals`` filters scope the fold; ``None`` defaults stay byte-identical.

Also covers the ``make_live_sync(markets=..., intervals=...)`` forwarding twin in
``pull_controller`` (the COMMON throttle both spines share).
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from brutex.dashboard.pull_controller import make_live_sync
from brutex.data.fetch_coverage import WindowKey
from brutex.data.window_ledger import WindowLedger

_NOW = datetime(2026, 7, 4, 10, 0, tzinfo=UTC)


def _key(market: str, symbol: str, interval: str) -> WindowKey:
    return WindowKey(
        market=market,
        security_id=symbol,
        interval=interval,
        start=date(2026, 6, 1),
        end=date(2026, 6, 30),
    )


def _mixed_ledger() -> WindowLedger:
    """Spot + option + future rows in ONE ledger — the real cross-market shape."""
    ledger = WindowLedger()
    # Spot (the pollution source): 2 settled SUCCESS + 1 PLANNED.
    ledger.record(
        _key("groww", "NIFTY", "1minute"),
        raw_bars=100,
        error_type=None,
        bars_appended=100,
        now=_NOW,
    )
    ledger.record(
        _key("groww", "SENSEX", "1minute"), raw_bars=50, error_type=None, bars_appended=50, now=_NOW
    )
    ledger.plan(_key("groww", "BANKNIFTY", "1minute"), now=_NOW)
    # F&O: one option SUCCESS, one option PLANNED, one future FAILED.
    ledger.record(
        _key("groww-option", "NSE-NIFTY-24Jul25-22000-CE", "1minute"),
        raw_bars=42,
        error_type=None,
        bars_appended=42,
        now=_NOW,
    )
    ledger.plan(_key("groww-option", "NSE-NIFTY-24Jul25-22100-CE", "1minute"), now=_NOW)
    ledger.record(
        _key("groww-future", "NSE-NIFTY-24Jul25-FUT", "1minute"),
        raw_bars=0,
        error_type="GrowwTimeoutError",
        bars_appended=0,
        now=_NOW,
    )
    # A derived-interval F&O row that an interval filter must exclude.
    ledger.record(
        _key("groww-option", "NSE-NIFTY-24Jul25-22000-CE", "1day"),
        raw_bars=7,
        error_type=None,
        bars_appended=7,
        now=_NOW,
    )
    return ledger


class TestProgressCountsFilters:
    def test_unfiltered_default_is_byte_identical(self) -> None:
        ledger = _mixed_ledger()
        assert ledger.progress_counts() == ledger.progress_counts(markets=None, intervals=None)
        assert ledger.progress_counts().total == 7

    def test_market_filter_excludes_spot_pollution(self) -> None:
        # Fails-before: the unfiltered fold reports total=7 (3 spot rows polluting
        # the F&O denominator); the F&O-scoped fold must see ONLY its own 4 rows.
        p = _mixed_ledger().progress_counts(markets=frozenset({"groww-option", "groww-future"}))
        assert p.total == 4
        assert p.resolved == 2  # the option SUCCESS (1m) + the option 1day row
        assert p.failed == 1  # the future transient
        assert p.planned == 1
        assert p.bars == 49  # 42 + 7 — never the spot 150

    def test_market_and_interval_filter_scope_the_pull(self) -> None:
        # The production F&O phase scope: F&O markets × the fetched 1minute only.
        p = _mixed_ledger().progress_counts(
            markets=frozenset({"groww-option", "groww-future"}), intervals=("1minute",)
        )
        assert p.total == 3  # the 1day derived row is out of scope
        assert p.resolved == 1
        assert p.bars == 42

    def test_current_label_comes_from_the_filtered_subset_only(self) -> None:
        ledger = WindowLedger()
        later = datetime(2026, 7, 4, 11, 0, tzinfo=UTC)
        ledger.record(
            _key("groww-option", "OPT", "1minute"),
            raw_bars=1,
            error_type=None,
            bars_appended=1,
            now=_NOW,
        )
        # The spot row is attempted LATER — unfiltered it wins the current label …
        ledger.record(
            _key("groww", "SPOT", "1minute"),
            raw_bars=1,
            error_type=None,
            bars_appended=1,
            now=later,
        )
        assert ledger.progress_counts().current == "SPOT 1minute"
        # … but the F&O-scoped fold must never name a spot symbol as "current".
        scoped = ledger.progress_counts(markets=frozenset({"groww-option"}))
        assert scoped.current == "OPT 1minute"

    def test_filter_matching_nothing_is_all_zero(self) -> None:
        p = _mixed_ledger().progress_counts(markets=frozenset({"no-such-market"}))
        assert (p.total, p.resolved, p.failed, p.bars, p.current) == (0, 0, 0, 0, "")


class TestMakeLiveSyncFilters:
    def test_filters_are_forwarded_to_the_ledger_fold(self) -> None:
        ledger = _mixed_ledger()
        pushes: list[tuple[int, int, int, int, str]] = []

        def _on_sync(resolved: int, total: int, bars: int, failed: int, current: str) -> None:
            pushes.append((resolved, total, bars, failed, current))

        sync = make_live_sync(
            ledger,
            lambda: None,
            _on_sync,
            min_interval_s=0.0,
            clock=lambda: 0.0,
            markets=frozenset({"groww-option", "groww-future"}),
            intervals=("1minute",),
        )
        sync()
        assert pushes == [(1, 3, 42, 1, pushes[0][4])]

    def test_default_none_filters_fold_everything(self) -> None:
        ledger = _mixed_ledger()
        pushes: list[tuple[int, int, int, int, str]] = []

        def _on_sync(resolved: int, total: int, bars: int, failed: int, current: str) -> None:
            pushes.append((resolved, total, bars, failed, current))

        sync = make_live_sync(ledger, lambda: None, _on_sync, min_interval_s=0.0, clock=lambda: 0.0)
        sync()
        assert pushes[0][1] == 7  # the unfiltered cross-market total — byte-identical
