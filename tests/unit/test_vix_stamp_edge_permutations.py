"""Edge-permutation matrix for the India-VIX stamp chain (DEC-VIX-STAMP-WIRE-001 Stage D).

One suite pinning the hardening permutations of the lake → stamper →
trade-stamp → aggregate → store chain in a single place (the Stage-A/B/C
suites own the per-module 100% coverage; this file owns the CROSS-CUTTING
edge shapes named by the spec):

* **empty VIX lake** — no partition dir / a bar-less partition dir / bars
  outside the window → honest ``None`` stamper, observe-only WARN, never a
  fabricated series (and the missing-dir arm never creates a catalog);
* **partial coverage** — a series covering only some instants stamps hits and
  honest ``None`` misses in ONE trade, and the aggregate pools only the
  non-``None`` stamps;
* **duplicate lake timestamps** — a poisoned VIX partition keeps the FIRST
  bar through the whole lake→stamper chain (L1 heal + keep-FIRST dedup);
* **tz edges** — an IST-aware lookup hits the UTC-stored instant (same
  instant, different tzinfo), the midnight-IST ``1day`` anchor equals its
  18:30Z UTC form, and a NAIVE timestamp honestly misses (never a false hit);
* **all-miss** — a stamper with zero coverage projects the honest
  ``(None, None, None)`` trio onto the persisted row;
* **single-trade** — the trio degenerates correctly (two stamps, one stamp,
  entry-only);
* **Decimal extremes** — full-precision Decimal levels survive the stamp and
  the float aggregate keeps the order law;
* **old store part-null** — a legacy (pre-VIX-column) store loads the honest
  ``None`` trio and a part-null redo backfills per-field, directionally.
"""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

pytest.importorskip("polars")
pytest.importorskip("pyarrow")
pytest.importorskip("duckdb", reason="the lake permutations need the DuckDB catalog")

from brutex.data.models import CandleBar
from brutex.engine import sweep_driver as driver_mod
from brutex.engine.reference_series import TradeReferenceStamper
from brutex.engine.reference_wire import aggregate_trade_vix, build_vix_stamper
from brutex.engine.results_store import ResultRow, ResultsStore
from brutex.engine.stage1a import build_bar_bits
from brutex.engine.sweep_run import ranked_combo_to_result_row
from brutex.metrics.types import ClosedTrade
from brutex.ranking import RankedCombo
from brutex.strategies.types import Side
from structlog.testing import capture_logs

if TYPE_CHECKING:  # pragma: no cover - typing only
    pass

_IST = timezone(timedelta(hours=5, minutes=30))

#: A UTC instant inside the Monday 2026-01-05 IST session.
_SESSION_TS = datetime(2026, 1, 5, 9, 16, tzinfo=_IST).astimezone(UTC)
#: The midnight-IST daily anchor of session 2026-01-05 (18:30Z of Jan 4).
_DAILY_ANCHOR = datetime(2026, 1, 4, 18, 30, tzinfo=UTC)


# ────────────────────────── local fixtures ──────────────────────────


def _vix_bar(ts: datetime, close: str) -> CandleBar:
    """A flat VIX bar at *ts* closing at *close* (full-precision Decimal)."""
    value = Decimal(close)
    return CandleBar(
        timestamp=ts,
        open=value,
        high=value,
        low=value,
        close=value,
        volume=0,
        open_interest=None,
    )


def _trade(
    entry_ts: datetime,
    exit_ts: datetime,
    stamper: TradeReferenceStamper,
) -> ClosedTrade:
    """One stamped round-trip trade whose VIX fields come from *stamper*."""
    vix_at_entry, vix_at_exit = stamper.stamp(entry_ts, exit_ts)
    return ClosedTrade(
        pnl_paisa=100,
        mfe_paisa=150,
        mae_paisa=50,
        holding_bars=1,
        entry_ts=entry_ts,
        exit_ts=exit_ts,
        direction=Side.BUY,
        vix_at_entry=vix_at_entry,
        vix_at_exit=vix_at_exit,
    )


def _plant_vix_partition(
    lake_root: Path,
    interval: str,
    rows: Sequence[tuple[datetime, float]],
    *,
    year: int = 2026,
    month: int = 1,
) -> Path:
    """Write a RAW pyarrow NSE/INDEX/INDIAVIX partition (dups plantable)."""
    import pyarrow as pa
    import pyarrow.parquet as pq
    from brutex.data.partition import build_partition_path

    dest = build_partition_path(
        lake_root=lake_root,
        exchange="NSE",
        segment="INDEX",
        symbol="INDIAVIX",
        interval=interval,
        year=year,
        month=month,
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    prices = [price for _, price in rows]
    table = pa.table(
        {
            "timestamp": pa.array([ts for ts, _ in rows], type=pa.timestamp("us", tz="UTC")),
            "open": pa.array(prices, type=pa.float64()),
            "high": pa.array(prices, type=pa.float64()),
            "low": pa.array(prices, type=pa.float64()),
            "close": pa.array(prices, type=pa.float64()),
            "volume": pa.array([0] * len(rows), type=pa.int64()),
            "open_interest": pa.array([None] * len(rows), type=pa.int64()),
        }
    )
    pq.write_table(table, dest)
    return dest


def _bar(day: int, minute: int, o: float, c: float) -> CandleBar:
    """One IST-session minute bar (stored UTC) with Decimal OHLC."""
    ts = (
        datetime(2026, 1, 5, 9, 16, tzinfo=_IST) + timedelta(days=day, minutes=minute)
    ).astimezone(UTC)
    hi = max(o, c) + 0.5
    lo = min(o, c) - 0.5
    return CandleBar(
        timestamp=ts,
        open=Decimal(str(round(o, 2))),
        high=Decimal(str(round(hi, 2))),
        low=Decimal(str(round(lo, 2))),
        close=Decimal(str(round(c, 2))),
        volume=100,
        open_interest=None,
    )


def _trending_bars(n_days: int = 4, per_day: int = 60) -> list[CandleBar]:
    """A multi-day upward-drifting session — BUY combos win via the TP leg."""
    bars: list[CandleBar] = []
    for d in range(n_days):
        price = 100.0
        for m in range(per_day):
            close = price + 0.6 if m % 3 else price - 0.3
            bars.append(_bar(d, m, price, close))
            price = close
    return bars


_BARS = _trending_bars()


# ────────────────────────── empty VIX lake ──────────────────────────


class TestEmptyVixLake:
    def test_missing_partition_degrades_to_none_without_catalog_io(self, tmp_path: Path) -> None:
        with capture_logs() as logs:
            stamper = build_vix_stamper(
                lake_root=tmp_path,
                start=_SESSION_TS - timedelta(days=1),
                end=_SESSION_TS + timedelta(days=1),
            )
        assert stamper is None
        assert any(e["event"] == "reference_wire.vix_lake_missing" for e in logs)
        assert not (tmp_path / "catalog.duckdb").exists()  # ZERO catalog I/O

    def test_partition_dir_without_parquet_degrades_to_none(self, tmp_path: Path) -> None:
        (tmp_path / "bars" / "NSE" / "INDEX" / "INDIAVIX" / "1minute" / "2026").mkdir(parents=True)
        with capture_logs() as logs:
            stamper = build_vix_stamper(
                lake_root=tmp_path,
                start=_SESSION_TS - timedelta(days=1),
                end=_SESSION_TS + timedelta(days=1),
            )
        assert stamper is None
        assert any(e["event"] == "reference_wire.vix_lake_missing" for e in logs)

    def test_bars_outside_the_window_degrade_to_vix_lake_empty(self, tmp_path: Path) -> None:
        _plant_vix_partition(tmp_path, "1minute", [(_SESSION_TS, 14.25)])
        with capture_logs() as logs:
            stamper = build_vix_stamper(
                lake_root=tmp_path,
                start=_SESSION_TS + timedelta(days=30),
                end=_SESSION_TS + timedelta(days=31),
            )
        assert stamper is None
        assert any(e["event"] == "reference_wire.vix_lake_empty" for e in logs)


# ────────────────────────── partial coverage ──────────────────────────


class TestPartialCoverage:
    def test_one_trade_stamps_the_hit_and_honestly_misses_the_gap(self) -> None:
        stamper = TradeReferenceStamper.from_vix_bars([_vix_bar(_SESSION_TS, "14.25")])
        with capture_logs() as logs:
            trade = _trade(_SESSION_TS, _SESSION_TS + timedelta(minutes=5), stamper)
        assert trade.vix_at_entry == Decimal("14.25")
        assert trade.vix_at_exit is None  # the gap is None, never carried forward
        assert stamper.miss_count == 1
        assert any(e["event"] == "reference_series.vix_miss" for e in logs)

    def test_aggregate_pools_only_the_non_none_stamps(self) -> None:
        stamper = TradeReferenceStamper.from_vix_bars(
            [_vix_bar(_SESSION_TS, "14.00"), _vix_bar(_SESSION_TS + timedelta(minutes=9), "16.00")]
        )
        with capture_logs():
            covered = _trade(_SESSION_TS, _SESSION_TS + timedelta(minutes=9), stamper)
            gap = _trade(
                _SESSION_TS + timedelta(minutes=1), _SESSION_TS + timedelta(minutes=2), stamper
            )
        assert (gap.vix_at_entry, gap.vix_at_exit) == (None, None)
        # The all-miss trade contributes NOTHING — the trio is exactly the
        # covered trade's two stamps.
        assert aggregate_trade_vix([covered, gap]) == (15.0, 14.0, 16.0)


# ────────────────────────── duplicate lake timestamps ──────────────────────────


class TestDuplicateLakeTimestamps:
    def test_poisoned_vix_partition_keeps_first_through_the_chain(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The lake→stamper chain resolves a dup instant to the FIRST bar.

        DEC-LAKE-HEAL-001 L1 heal (keep-FIRST rewrite) + the keep-FIRST read
        dedup both defend the same law, so whichever fires, the stamped level
        is the first physical row's close — never the poisoned re-statement.
        """
        monkeypatch.delenv("BRUTEX_LAKE_HEAL", raising=False)  # default = auto heal
        _plant_vix_partition(
            tmp_path,
            "1minute",
            [
                (_SESSION_TS, 14.25),  # keep-FIRST winner
                (_SESSION_TS, 99.75),  # poisoned duplicate re-statement
                (_SESSION_TS + timedelta(minutes=1), 14.30),
            ],
        )
        stamper = build_vix_stamper(
            lake_root=tmp_path,
            start=_SESSION_TS - timedelta(days=1),
            end=_SESSION_TS + timedelta(days=1),
        )
        assert stamper is not None
        assert stamper.vix.level_at(_SESSION_TS) == Decimal("14.25")
        assert stamper.vix.level_at(_SESSION_TS + timedelta(minutes=1)) == Decimal("14.3")


# ────────────────────────── tz edges ──────────────────────────


class TestTzEdges:
    def test_ist_aware_lookup_hits_the_utc_stored_instant(self) -> None:
        stamper = TradeReferenceStamper.from_vix_bars([_vix_bar(_SESSION_TS, "14.25")])
        ist_form = _SESSION_TS.astimezone(_IST)
        assert ist_form == _SESSION_TS  # same instant, different tzinfo
        entry, exit_ = stamper.stamp(ist_form, ist_form)
        assert entry == exit_ == Decimal("14.25")
        assert stamper.miss_count == 0

    def test_midnight_ist_daily_anchor_equals_its_1830z_form(self) -> None:
        stamper = TradeReferenceStamper.from_vix_bars([_vix_bar(_DAILY_ANCHOR, "13.50")])
        midnight_ist = datetime(2026, 1, 5, 0, 0, tzinfo=_IST)
        assert midnight_ist == _DAILY_ANCHOR
        assert stamper.vix.level_at(midnight_ist) == Decimal("13.50")

    def test_naive_timestamp_misses_honestly_never_a_false_hit(self) -> None:
        stamper = TradeReferenceStamper.from_vix_bars([_vix_bar(_SESSION_TS, "14.25")])
        naive = _SESSION_TS.replace(tzinfo=None)
        with capture_logs() as logs:
            entry, exit_ = stamper.stamp(naive, naive)
        assert (entry, exit_) == (None, None)  # a naive ts can never match aware keys
        assert stamper.miss_count == 2
        assert any(e["event"] == "reference_series.vix_miss" for e in logs)


# ────────────────────────── all-miss ──────────────────────────


def _ranked_combo() -> RankedCombo:
    """A gates-independent ranked combo over the local fixture (mask 0)."""
    metrics = driver_mod.evaluate_combo(
        _BARS, 0, direction=Side.BUY, tf_minutes=1, take_profit_paisa=50
    )
    return RankedCombo(mask=0, metrics=metrics, composite_score=0.0, deflated_sharpe=None)


class TestAllMiss:
    def test_all_miss_stamper_projects_the_none_trio_onto_the_row(self) -> None:
        empty_stamper = TradeReferenceStamper.from_vix_bars([])
        with capture_logs():
            row = ranked_combo_to_result_row(
                _ranked_combo(),
                run_id="0" * 64,
                instrument="NIFTY",
                tf="1m",
                direction="bull",
                mode="A",
                bars=_BARS,
                bar_bits=build_bar_bits(_BARS),
                tf_minutes=1,
                sl_paisa=None,
                tp_paisa=50,
                reference_stamper=empty_stamper,
            )
        assert row.total_trades > 0  # trades happened …
        assert (row.vix_mean, row.vix_min, row.vix_max) == (None, None, None)  # … all unstamped
        assert empty_stamper.miss_count == 2 * row.total_trades  # every instant missed

    def test_aggregate_over_all_none_stamps_is_the_none_trio(self) -> None:
        empty_stamper = TradeReferenceStamper.from_vix_bars([])
        with capture_logs():
            trades = [
                _trade(_SESSION_TS, _SESSION_TS + timedelta(minutes=1), empty_stamper),
                _trade(
                    _SESSION_TS + timedelta(minutes=2),
                    _SESSION_TS + timedelta(minutes=3),
                    empty_stamper,
                ),
            ]
        assert aggregate_trade_vix(trades) == (None, None, None)

    def test_aggregate_over_zero_trades_is_the_none_trio(self) -> None:
        assert aggregate_trade_vix([]) == (None, None, None)


# ────────────────────────── single trade ──────────────────────────


class TestSingleTrade:
    def test_two_distinct_stamps_give_mean_between_min_and_max(self) -> None:
        stamper = TradeReferenceStamper.from_vix_bars(
            [_vix_bar(_SESSION_TS, "14.00"), _vix_bar(_SESSION_TS + timedelta(minutes=1), "15.00")]
        )
        trade = _trade(_SESSION_TS, _SESSION_TS + timedelta(minutes=1), stamper)
        assert aggregate_trade_vix([trade]) == (14.5, 14.0, 15.0)

    def test_identical_entry_exit_level_degenerates_to_a_flat_trio(self) -> None:
        stamper = TradeReferenceStamper.from_vix_bars([_vix_bar(_SESSION_TS, "14.25")])
        trade = _trade(_SESSION_TS, _SESSION_TS, stamper)
        assert aggregate_trade_vix([trade]) == (14.25, 14.25, 14.25)

    def test_entry_only_stamp_is_the_whole_pool(self) -> None:
        stamper = TradeReferenceStamper.from_vix_bars([_vix_bar(_SESSION_TS, "14.25")])
        with capture_logs():
            trade = _trade(_SESSION_TS, _SESSION_TS + timedelta(minutes=1), stamper)
        assert trade.vix_at_exit is None
        assert aggregate_trade_vix([trade]) == (14.25, 14.25, 14.25)


# ────────────────────────── Decimal extremes ──────────────────────────


class TestDecimalExtremes:
    def test_extreme_levels_stamp_at_full_decimal_precision(self) -> None:
        huge = Decimal("1E+300")
        tiny = Decimal("1E-300")
        precise = Decimal("14.123456789012345678901234567890")
        stamper = TradeReferenceStamper.from_vix_bars(
            [
                _vix_bar(_SESSION_TS, str(huge)),
                _vix_bar(_SESSION_TS + timedelta(minutes=1), str(tiny)),
                _vix_bar(_SESSION_TS + timedelta(minutes=2), str(precise)),
            ]
        )
        # The stamp IS the stored Decimal — full precision, no float round-trip.
        assert stamper.stamp(_SESSION_TS, _SESSION_TS + timedelta(minutes=1)) == (huge, tiny)
        entry, _exit = stamper.stamp(
            _SESSION_TS + timedelta(minutes=2), _SESSION_TS + timedelta(minutes=2)
        )
        assert entry == precise  # every digit preserved

    def test_extreme_aggregate_stays_finite_and_ordered(self) -> None:
        stamper = TradeReferenceStamper.from_vix_bars(
            [
                _vix_bar(_SESSION_TS, "1E+300"),
                _vix_bar(_SESSION_TS + timedelta(minutes=1), "1E-300"),
            ]
        )
        trade = _trade(_SESSION_TS, _SESSION_TS + timedelta(minutes=1), stamper)
        mean, low, high = aggregate_trade_vix([trade])
        assert mean is not None and low is not None and high is not None
        assert (low, high) == (1e-300, 1e300)
        assert low <= mean <= high  # the order law holds at the float extremes

    def test_fsum_overflow_grade_level_aggregates_without_aborting(self) -> None:
        """Round-5 panel MINOR: the FULL screens → stamp → aggregate path at 1.5E+308.

        ``Decimal("1.5E+308")`` is positive, Float64-REPRESENTABLE (it
        round-trips the stored lake, unlike the round-4 ``1E+400`` residual)
        and passes BOTH ``from_bars`` screens (``is_finite()`` AND
        ``math.isfinite(float(...))``) — so it stamps normally, and ONE trade
        whose entry+exit both hit it made ``math.fsum`` raise
        ``OverflowError: intermediate overflow in fsum`` inside the finalist
        projection, aborting the whole launch outside the tuple-fence. The
        pre-scaled fallback now degrades gracefully: total, finite, ordered.
        """
        corrupt = Decimal("1.5E+308")
        assert math.isfinite(float(corrupt))  # the reachability precondition
        stamper = TradeReferenceStamper.from_vix_bars(
            [
                _vix_bar(_SESSION_TS, str(corrupt)),
                _vix_bar(_SESSION_TS + timedelta(minutes=1), str(corrupt)),
            ]
        )
        trade = _trade(_SESSION_TS, _SESSION_TS + timedelta(minutes=1), stamper)
        assert (trade.vix_at_entry, trade.vix_at_exit) == (corrupt, corrupt)
        mean, low, high = aggregate_trade_vix([trade])  # pre-fix: OverflowError
        assert (mean, low, high) == (1.5e308, 1.5e308, 1.5e308)


# ────────────────────────── old store part-null ──────────────────────────


def _store_row(**overrides: object) -> ResultRow:
    """A valid ResultRow for the store permutations (unstamped by default)."""
    base: dict[str, object] = {
        "run_id": "1" * 64,
        "instrument": "NIFTY",
        "tf": "5m",
        "direction": "bull",
        "mode": "A",
        "mask_bits": 0b101,
        "net_pnl_paisa": 1_000,
        "gross_profit_paisa": 1_500,
        "gross_loss_paisa": 500,
        "max_drawdown_paisa": 250,
        "expectancy_paisa": 25,
        "total_trades": 40,
        "winning_trades": 25,
        "losing_trades": 15,
        "sharpe_ratio": 1.5,
        "sortino_ratio": 2.0,
        "calmar_ratio": 1.25,
        "profit_factor": 1.8,
        "win_rate": 0.625,
        "max_drawdown_pct": 12.5,
        "avg_entry_price": Decimal("14020.50"),
        "avg_exit_price": Decimal("14100.25"),
    }
    base.update(overrides)
    return ResultRow(**base)  # type: ignore[arg-type]


class TestOldStorePartNull:
    def _legacy_store(self, tmp_path: Path) -> Path:
        """Flush a modern store then strip the VIX trio → a legacy-shape file."""
        import polars as pl

        modern = tmp_path / "modern.parquet"
        legacy = tmp_path / "legacy.parquet"
        store = ResultsStore(path=modern)
        store.append(_store_row())
        store.flush()
        pl.read_parquet(modern).drop("vix_mean", "vix_min", "vix_max").write_parquet(legacy)
        return legacy

    def test_legacy_store_loads_the_honest_none_trio(self, tmp_path: Path) -> None:
        loaded = ResultsStore.load(self._legacy_store(tmp_path))
        (row,) = loaded.rows()
        assert (row.vix_mean, row.vix_min, row.vix_max) == (None, None, None)

    def test_part_null_redo_backfills_field_by_field(self, tmp_path: Path) -> None:
        """An old-store row upgrades directionally: each field fills at most once."""
        legacy = self._legacy_store(tmp_path)
        loaded = ResultsStore.load(legacy)
        # A part-null redo (only min+max known) upgrades exactly those fields …
        assert loaded.append(_store_row(vix_min=12.25, vix_max=15.75)) is True
        (row,) = loaded.rows()
        assert (row.vix_mean, row.vix_min, row.vix_max) == (None, 12.25, 15.75)
        # … a later redo fills the remaining gap without touching the rest …
        assert loaded.append(_store_row(vix_mean=13.5)) is True
        (row,) = loaded.rows()
        assert (row.vix_mean, row.vix_min, row.vix_max) == (13.5, 12.25, 15.75)
        # … and the part-null trio round-trips flush → load unchanged.
        loaded.flush()
        (reloaded,) = ResultsStore.load(legacy).rows()
        assert (reloaded.vix_mean, reloaded.vix_min, reloaded.vix_max) == (13.5, 12.25, 15.75)
