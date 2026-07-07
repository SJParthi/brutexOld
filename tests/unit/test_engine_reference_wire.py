"""Unit tests for the India-VIX reference-stamp wiring (``brutex.engine.reference_wire``).

100% line + branch coverage of :mod:`brutex.engine.reference_wire`
(DEC-VIX-STAMP-WIRE-001):

* :func:`aggregate_trade_vix` — the pure per-combo mean/min/max fold (both
  stamps pooled; ``None`` skipped, never imputed; empty/all-``None`` →
  ``(None, None, None)``);
* :func:`warn_stale_vix_derived` — the observe-only derived-``1day`` freshness
  check (fresh / stale / every honest abstain arm);
* :func:`build_vix_stamper` — lake-driven over a REAL tmp lake (missing
  partition → honest ``None`` with ZERO catalog I/O; empty window → ``None``;
  1-minute + midnight-IST daily anchors both stamped; a daily-only lake still
  builds);
* :func:`vix_stamper_provider` — lazy + once-per-run cached (a ``None``
  outcome is cached too, never retried per call);
* the ``VIX_LAKE_SYMBOL`` ↔ :data:`brutex.universe.REFERENCE_ONLY_INDICES`
  single-source pin (REFERENCE_ONLY scope law, CLAUDE.md §2 Rule 3).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import structlog
from brutex.data.models import CandleBar
from brutex.engine.reference_wire import (
    VIX_LAKE_EXCHANGE,
    VIX_LAKE_SEGMENT,
    VIX_LAKE_SYMBOL,
    VIX_STAMPER_INTERVALS,
    aggregate_trade_vix,
    build_vix_stamper,
    vix_stamper_provider,
    warn_stale_vix_derived,
)
from brutex.metrics.types import ClosedTrade
from brutex.strategies.types import Side
from structlog.typing import EventDict

_T0 = datetime(2024, 6, 3, 3, 45, tzinfo=UTC)  # Mon 3 Jun 2024, 09:15 IST


def _trade(entry_vix: str | None, exit_vix: str | None) -> ClosedTrade:
    """A minimal valid trade carrying the given VIX stamps."""
    return ClosedTrade(
        pnl_paisa=0,
        mfe_paisa=0,
        mae_paisa=0,
        holding_bars=1,
        entry_ts=_T0,
        exit_ts=_T0 + timedelta(minutes=1),
        direction=Side.BUY,
        vix_at_entry=None if entry_vix is None else Decimal(entry_vix),
        vix_at_exit=None if exit_vix is None else Decimal(exit_vix),
    )


def _bar(ts: datetime, o: str, h: str, low: str, c: str, volume: int = 0) -> CandleBar:
    return CandleBar(
        timestamp=ts,
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(low),
        close=Decimal(c),
        volume=volume,
        open_interest=None,
    )


def _session_minutes(day: int, closes: Sequence[str]) -> list[CandleBar]:
    """One in-session VIX minute bar per *closes* entry on June *day* 2024 (flat OHLC)."""
    start = datetime(2024, 6, day, 3, 45, tzinfo=UTC)
    return [
        _bar(start + timedelta(minutes=i), close, close, close, close)
        for i, close in enumerate(closes)
    ]


def _daily_ts(day: int) -> datetime:
    """Midnight-IST daily anchor for June *day* 2024 (= 18:30 UTC of the prior day)."""
    return datetime(2024, 6, day - 1, 18, 30, tzinfo=UTC)


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


# ────────────────────────────────────────────────────────────────────────
# REFERENCE_ONLY scope pin
# ────────────────────────────────────────────────────────────────────────


class TestVixLakeCoordinatesPin:
    def test_symbol_is_the_one_reference_only_universe_index(self) -> None:
        """ONE source of truth: the lake symbol IS the universe's reference-only spec."""
        from brutex.universe import REFERENCE_ONLY_INDICES

        assert len(REFERENCE_ONLY_INDICES) == 1
        spec = REFERENCE_ONLY_INDICES[0]
        assert spec.reference_only is True
        assert spec.underlying_symbol == VIX_LAKE_SYMBOL == "INDIAVIX"
        assert spec.exchange == VIX_LAKE_EXCHANGE == "NSE"

    def test_segment_and_intervals(self) -> None:
        assert VIX_LAKE_SEGMENT == "INDEX"
        assert VIX_STAMPER_INTERVALS == ("1minute", "1day")


# ────────────────────────────────────────────────────────────────────────
# aggregate_trade_vix (pure)
# ────────────────────────────────────────────────────────────────────────


class TestAggregateTradeVix:
    def test_empty_trades_is_all_none(self) -> None:
        assert aggregate_trade_vix([]) == (None, None, None)

    def test_all_none_stamps_is_all_none(self) -> None:
        """Unstamped trades yield the honest unknown trio — never a fabricated 0.0."""
        assert aggregate_trade_vix([_trade(None, None), _trade(None, None)]) == (
            None,
            None,
            None,
        )

    def test_pools_both_entry_and_exit_stamps(self) -> None:
        mean, low, high = aggregate_trade_vix([_trade("10", "20"), _trade("30", None)])
        assert (mean, low, high) == (20.0, 10.0, 30.0)

    def test_single_stamp_collapses_to_itself(self) -> None:
        assert aggregate_trade_vix([_trade(None, "15.25")]) == (15.25, 15.25, 15.25)

    def test_full_precision_never_rounded(self) -> None:
        """VIX is a metric, not money — the mean keeps full float precision."""
        mean, low, high = aggregate_trade_vix([_trade("10.01", "10.02")])
        assert mean == (10.01 + 10.02) / 2
        assert (low, high) == (10.01, 10.02)


# ────────────────────────────────────────────────────────────────────────
# warn_stale_vix_derived (pure, observe-only)
# ────────────────────────────────────────────────────────────────────────

pytest.importorskip("polars")


def _three_day_minutes() -> list[CandleBar]:
    """Two flat minute bars per day over Jun 3-5 2024 (day 4 is the interior day)."""
    return [
        *_session_minutes(3, ["10.00", "10.50"]),
        *_session_minutes(4, ["11.00", "11.25"]),
        *_session_minutes(5, ["12.00", "12.75"]),
    ]


def _fresh_daily(day: int, first: str, last: str) -> CandleBar:
    """The daily bar :func:`resample_1m_to_daily` derives from two flat minutes."""
    low, high = sorted([Decimal(first), Decimal(last)])
    return _bar(_daily_ts(day), first, str(high), str(low), last)


class TestWarnStaleVixDerived:
    def test_empty_minutes_abstains(self) -> None:
        assert warn_stale_vix_derived([], [_fresh_daily(4, "11.00", "11.25")]) == 0

    def test_empty_daily_abstains(self) -> None:
        assert warn_stale_vix_derived(_three_day_minutes(), []) == 0

    def test_fresh_interior_day_passes_silently(self) -> None:
        with _capture_logs() as logs:
            stale = warn_stale_vix_derived(
                _three_day_minutes(), [_fresh_daily(4, "11.00", "11.25")]
            )
        assert stale == 0
        assert [e for e in logs if e["event"] == "reference_wire.vix_daily_stale"] == []

    def test_stale_interior_day_warns_once(self) -> None:
        """A drifted stored daily (close ≠ re-derivation) is counted + WARNed."""
        drifted = _bar(_daily_ts(4), "11.00", "11.30", "11.00", "11.30")
        with _capture_logs() as logs:
            stale = warn_stale_vix_derived(_three_day_minutes(), [drifted])
        assert stale == 1
        warns = [e for e in logs if e["event"] == "reference_wire.vix_daily_stale"]
        assert len(warns) == 1
        assert warns[0]["stale_days"] == 1
        assert warns[0]["sample"] == [_daily_ts(4).isoformat()]

    def test_window_edge_days_abstain_even_when_mismatched(self) -> None:
        """The first + last covered IST dates are unprovable (possible truncation)."""
        drifted_first = _bar(_daily_ts(3), "1.00", "1.00", "1.00", "1.00")
        drifted_last = _bar(_daily_ts(5), "1.00", "1.00", "1.00", "1.00")
        with _capture_logs() as logs:
            stale = warn_stale_vix_derived(_three_day_minutes(), [drifted_first, drifted_last])
        assert stale == 0
        assert [e for e in logs if e["event"] == "reference_wire.vix_daily_stale"] == []

    def test_interior_day_without_minute_base_abstains(self) -> None:
        """A stored daily whose session the minute window lacks is unprovable."""
        minutes = [*_session_minutes(3, ["10.00"]), *_session_minutes(5, ["12.00"])]
        orphan_daily = _bar(_daily_ts(4), "1.00", "1.00", "1.00", "1.00")
        assert warn_stale_vix_derived(minutes, [orphan_daily]) == 0


# ────────────────────────────────────────────────────────────────────────
# build_vix_stamper (lake-driven)
# ────────────────────────────────────────────────────────────────────────

pytest.importorskip("pyarrow")
pytest.importorskip("duckdb", reason="Catalog needs duckdb")


def _plant_partition(
    dest: Path, rows: Sequence[tuple[datetime, float, float, float, float, int]]
) -> None:
    """Write a raw pyarrow partition with full OHLCV rows."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    dest.parent.mkdir(parents=True, exist_ok=True)
    table = pa.table(
        {
            "timestamp": pa.array([r[0] for r in rows], type=pa.timestamp("us", tz="UTC")),
            "open": pa.array([r[1] for r in rows], type=pa.float64()),
            "high": pa.array([r[2] for r in rows], type=pa.float64()),
            "low": pa.array([r[3] for r in rows], type=pa.float64()),
            "close": pa.array([r[4] for r in rows], type=pa.float64()),
            "volume": pa.array([r[5] for r in rows], type=pa.int64()),
            "open_interest": pa.array([None] * len(rows), type=pa.int64()),
        }
    )
    pq.write_table(table, dest)


def _vix_dest(lake: Path, interval: str) -> Path:
    from brutex.data.partition import build_partition_path

    return build_partition_path(
        lake_root=lake,
        exchange=VIX_LAKE_EXCHANGE,
        segment=VIX_LAKE_SEGMENT,
        symbol=VIX_LAKE_SYMBOL,
        interval=interval,
        year=2024,
        month=6,
    )


def _seed_vix_lake(lake: Path) -> None:
    """Plant a June-2024 VIX 1-minute partition + its derived 1-day partition."""
    _plant_partition(
        _vix_dest(lake, "1minute"),
        [
            (_T0, 10.0, 10.0, 10.0, 10.0, 0),
            (_T0 + timedelta(minutes=1), 10.5, 10.5, 10.5, 10.5, 0),
        ],
    )
    # The derived daily for Mon 3 Jun 2024, midnight-IST-stamped (18:30Z Sun 2 Jun).
    _plant_partition(_vix_dest(lake, "1day"), [(_daily_ts(3), 10.0, 10.5, 10.0, 10.5, 0)])


_WINDOW = (datetime(2024, 6, 1, tzinfo=UTC), datetime(2024, 6, 30, tzinfo=UTC))


class TestBuildVixStamper:
    def test_missing_vix_partition_degrades_to_none_with_zero_catalog_io(
        self, tmp_path: Path
    ) -> None:
        """No VIX on disk → observe-only WARN + None; the catalog is never touched."""
        with _capture_logs() as logs:
            stamper = build_vix_stamper(lake_root=tmp_path, start=_WINDOW[0], end=_WINDOW[1])
        assert stamper is None
        assert not (tmp_path / "catalog.duckdb").exists()
        warns = [e for e in logs if e["event"] == "reference_wire.vix_lake_missing"]
        assert len(warns) == 1
        assert warns[0]["symbol"] == VIX_LAKE_SYMBOL

    def test_vix_dir_without_parquet_degrades_to_none(self, tmp_path: Path) -> None:
        _vix_dest(tmp_path, "1minute").parent.mkdir(parents=True, exist_ok=True)
        with _capture_logs() as logs:
            stamper = build_vix_stamper(lake_root=tmp_path, start=_WINDOW[0], end=_WINDOW[1])
        assert stamper is None
        assert len([e for e in logs if e["event"] == "reference_wire.vix_lake_missing"]) == 1

    def test_builds_union_stamper_minute_and_daily_anchor(self, tmp_path: Path) -> None:
        """The stamper serves BOTH a session minute AND the midnight-IST daily anchor."""
        _seed_vix_lake(tmp_path)
        with _capture_logs() as logs:
            stamper = build_vix_stamper(lake_root=tmp_path, start=_WINDOW[0], end=_WINDOW[1])
        assert stamper is not None
        assert stamper.vix.level_at(_T0) == Decimal("10.0")
        assert stamper.vix.level_at(_daily_ts(3)) == Decimal("10.5")
        built = [e for e in logs if e["event"] == "reference_wire.vix_stamper_built"]
        assert len(built) == 1
        assert built[0]["minute_bars"] == 2
        assert built[0]["daily_bars"] == 1

    def test_window_with_no_vix_bar_degrades_to_none(self, tmp_path: Path) -> None:
        """Partitions exist but the run window holds no VIX bar → honest None."""
        _seed_vix_lake(tmp_path)
        with _capture_logs() as logs:
            stamper = build_vix_stamper(
                lake_root=tmp_path,
                start=datetime(2025, 1, 1, tzinfo=UTC),
                end=datetime(2025, 1, 31, tzinfo=UTC),
            )
        assert stamper is None
        empty = [e for e in logs if e["event"] == "reference_wire.vix_lake_empty"]
        assert len(empty) == 1
        assert empty[0]["symbol"] == VIX_LAKE_SYMBOL

    def test_daily_only_lake_still_builds(self, tmp_path: Path) -> None:
        """A lake with only the 1day partition builds a daily-anchor-only stamper."""
        _plant_partition(_vix_dest(tmp_path, "1day"), [(_daily_ts(3), 10.0, 10.5, 10.0, 10.5, 0)])
        stamper = build_vix_stamper(lake_root=tmp_path, start=_WINDOW[0], end=_WINDOW[1])
        assert stamper is not None
        assert stamper.vix.level_at(_daily_ts(3)) == Decimal("10.5")
        assert stamper.vix.level_at(_T0) is None  # no minute base — honest miss

    def test_explicit_catalog_path_is_used(self, tmp_path: Path) -> None:
        _seed_vix_lake(tmp_path)
        catalog_path = tmp_path / "elsewhere" / "cat.duckdb"
        catalog_path.parent.mkdir(parents=True)
        stamper = build_vix_stamper(
            lake_root=tmp_path, start=_WINDOW[0], end=_WINDOW[1], catalog_path=catalog_path
        )
        assert stamper is not None
        assert catalog_path.exists()
        assert not (tmp_path / "catalog.duckdb").exists()


# ────────────────────────────────────────────────────────────────────────
# vix_stamper_provider (lazy + cached)
# ────────────────────────────────────────────────────────────────────────


class TestVixStamperProvider:
    def test_lazy_builds_once_and_caches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from brutex.engine import reference_wire
        from brutex.engine.reference_series import TradeReferenceStamper

        sentinel = TradeReferenceStamper.from_vix_bars([])
        calls: list[tuple[object, ...]] = []

        def _fake_build(
            *, lake_root: Path, start: datetime, end: datetime, catalog_path: Path | None = None
        ) -> TradeReferenceStamper:
            calls.append((lake_root, start, end, catalog_path))
            return sentinel

        monkeypatch.setattr(reference_wire, "build_vix_stamper", _fake_build)
        provider = vix_stamper_provider(
            lake_root=Path("/nowhere"), start=_WINDOW[0], end=_WINDOW[1]
        )
        assert calls == []  # lazy — nothing read until first call
        assert provider() is sentinel
        assert provider() is sentinel
        assert calls == [(Path("/nowhere"), _WINDOW[0], _WINDOW[1], None)]

    def test_none_outcome_is_cached_never_retried(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from brutex.engine import reference_wire

        calls: list[None] = []

        def _fake_build(**_kwargs: object) -> None:
            calls.append(None)

        monkeypatch.setattr(reference_wire, "build_vix_stamper", _fake_build)
        provider = vix_stamper_provider(
            lake_root=Path("/nowhere"), start=_WINDOW[0], end=_WINDOW[1]
        )
        assert provider() is None
        assert provider() is None
        assert len(calls) == 1


# ────────────────────────────────────────────────────────────────────────
# Engine re-export surface
# ────────────────────────────────────────────────────────────────────────


class TestEngineReexports:
    def test_public_surface_reexported(self) -> None:
        from brutex import engine

        assert engine.load_lake_slice is not None
        assert engine.VIX_LAKE_SYMBOL == VIX_LAKE_SYMBOL
        assert engine.aggregate_trade_vix is aggregate_trade_vix
        assert engine.vix_stamper_provider is vix_stamper_provider
        assert engine.warn_stale_vix_derived is warn_stale_vix_derived

    def test_trade_reference_stamper_not_reexported(self) -> None:
        """The stamper class stays in reference_series — consumers take the provider."""
        from brutex import engine

        assert "TradeReferenceStamper" not in engine.__all__
