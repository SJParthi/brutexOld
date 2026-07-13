"""DEC-DERIVED-OI-NULL-HONEST-001 — derived intraday OI: null preserved, never 0.

The E2-H1 confirmed defect (derived-TF audit, DEC-DERIVED-TF-AUDIT-001): the
derived-intraday writer (``make_lake_write_tf`` → ``frame_to_candle_bars`` with
the legacy default ``absent_open_interest=0``) FABRICATED ``open_interest=0``
— a REAL market value — for every null-OI bucket, while the canonical
resampler deliberately emitted null, the 1-minute base stored null, and the
derived ``1day`` preserved null (``tf_resampler``'s pledge: "null … NEVER
fabricated as 0"). Same session, three answers on disk.

``TestDerivedIntradayOiNull`` fails at the merge base (the fix-reversion
evidence): pre-fix, the stored 5-minute OI read ``[1004, 0, 0]``.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("polars")
pytest.importorskip("pyarrow")

import polars as pl
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.sink import ParquetBarSink
from brutex.data.tf_resampler import resample_1m_to_tf
from brutex.data.timeframe_materialize import PrebuildKind, materialize_lake_timeframes

_IST = timezone(timedelta(hours=5, minutes=30))


def _ist(y: int, mo: int, d: int, h: int, mi: int) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=_IST).astimezone(UTC)


def _bar(ts: datetime, px: float, *, vol: int, oi: int | None) -> CandleBar:
    return CandleBar(
        timestamp=ts,
        open=Decimal(str(px)),
        high=Decimal(str(px + 0.5)),
        low=Decimal(str(px - 0.5)),
        close=Decimal(str(px + 0.1)),
        volume=vol,
        open_interest=oi,
    )


def _e2_h1_fno_bars() -> list[CandleBar]:
    """The E2-H1 session verbatim: 15 bars 09:15–09:29 IST on 2024-06-12.

    5m buckets: b1 OI present on all bars (1000..1004 — honest last 1004);
    b2 all-null OI (honest last: null); b3 OI 1010 then nulls (strict-last:
    null — the E1-proven docstring-literal OI semantic).
    """
    bars: list[CandleBar] = []
    for i in range(15):
        mins = 9 * 60 + 15 + i
        oi: int | None
        if i < 5:
            oi = 1000 + i
        elif i < 10:
            oi = None
        else:
            oi = 1010 if i == 10 else None
        bars.append(
            _bar(_ist(2024, 6, 12, mins // 60, mins % 60), 100.0 + i * 0.05, vol=100 + i, oi=oi)
        )
    return bars


def _write_1m(lake: Path, exchange: str, segment: str, symbol: str, bars: list[CandleBar]) -> None:
    sink = ParquetBarSink(writer=ParquetWriter(lake_root=lake))
    sink.append(exchange=exchange, segment=segment, symbol=symbol, interval="1minute", bars=bars)


def _read(lake: Path, exchange: str, segment: str, symbol: str, interval: str) -> pl.DataFrame:
    glob = (lake / "bars" / exchange / segment / symbol / interval).as_posix() + "/**/*.parquet"
    return pl.read_parquet(glob).sort("timestamp")


class TestDerivedIntradayOiNull:
    """The E2-H1 repro shape through the REAL writers + prebuild."""

    def test_fno_null_oi_buckets_store_null_across_every_tf(self, tmp_path: Path) -> None:
        lake = tmp_path / "lake"
        ex, seg, sym = "NSE", "FNO", "NSE-NIFTY-27Jun24-FUT"
        _write_1m(lake, ex, seg, sym, _e2_h1_fno_bars())
        materialize_lake_timeframes(
            lake_root=lake,
            exchange=ex,
            segment=seg,
            symbol=sym,
            kind=PrebuildKind.FUTURES,
            tfs=("5m", "15m"),
            include_daily=True,
            daily_before=date(2024, 6, 13),
            incremental=False,
        )
        # The 1-minute base stores null (the honest unknown).
        assert _read(lake, ex, seg, sym, "1minute").get_column("open_interest").null_count() == 9
        # Derived 5m: [1004, None, None] — exactly what the resampler emitted
        # (strict-last OI; pre-fix the writer stored [1004, 0, 0]).
        assert _read(lake, ex, seg, sym, "5minute").get_column("open_interest").to_list() == [
            1004,
            None,
            None,
        ]
        # Derived 15m: ONE bucket whose last 1m OI is null → null (pre-fix: 0).
        assert _read(lake, ex, seg, sym, "15minute").get_column("open_interest").to_list() == [None]
        # Derived 1day: preserved null before AND after the fix.
        assert _read(lake, ex, seg, sym, "1day").get_column("open_interest").to_list() == [None]

    def test_fno_present_oi_values_still_stored_verbatim(self, tmp_path: Path) -> None:
        """A bucket whose LAST 1m bar carries real OI stores that value — the
        fix changes only the null mapping, never a present value."""
        lake = tmp_path / "lake"
        ex, seg, sym = "NSE", "FNO", "NSE-NIFTY-27Jun24-FUT"
        bars = [
            _bar(_ist(2024, 6, 12, 9, 15 + i), 100.0 + i * 0.05, vol=100 + i, oi=2000 + i)
            for i in range(10)
        ]
        _write_1m(lake, ex, seg, sym, bars)
        materialize_lake_timeframes(
            lake_root=lake,
            exchange=ex,
            segment=seg,
            symbol=sym,
            kind=PrebuildKind.FUTURES,
            tfs=("5m",),
            include_daily=False,
            incremental=False,
        )
        assert _read(lake, ex, seg, sym, "5minute").get_column("open_interest").to_list() == [
            2004,
            2009,
        ]

    def test_spot_derived_intraday_oi_is_null_not_zero(self, tmp_path: Path) -> None:
        """The spot arm: a spot frame has no OI column, so every derived
        intraday bar's OI is honestly null — matching the spot 1-minute base
        (which stores null) and the derived spot 1day (null). The pre-fix "0
        is the spot convention" claim was never true of the base itself."""
        lake = tmp_path / "lake"
        ex, seg, sym = "NSE", "INDEX", "NSE-NIFTY"
        bars = [
            _bar(_ist(2024, 6, 12, 9, 15 + i), 100.0 + i * 0.05, vol=100 + i, oi=None)
            for i in range(15)
        ]
        _write_1m(lake, ex, seg, sym, bars)
        materialize_lake_timeframes(
            lake_root=lake,
            exchange=ex,
            segment=seg,
            symbol=sym,
            kind=PrebuildKind.SPOT,
            tfs=("5m",),
            include_daily=True,
            daily_before=date(2024, 6, 13),
            incremental=False,
        )
        assert (
            _read(lake, ex, seg, sym, "1minute").get_column("open_interest").to_list()
            == [None] * 15
        )
        assert (
            _read(lake, ex, seg, sym, "5minute").get_column("open_interest").to_list() == [None] * 3
        )
        assert _read(lake, ex, seg, sym, "1day").get_column("open_interest").to_list() == [None]

    def test_ohlcv_bytes_identical_across_the_oi_flip(self, tmp_path: Path) -> None:
        """The OI flip touches ONLY the open_interest column: every stored
        derived OHLCV value equals the canonical resampler's output over the
        stored 1-minute base (the write path adds only the 2-dp snap, and
        these fixtures are already on the 2-dp grid)."""
        lake = tmp_path / "lake"
        ex, seg, sym = "NSE", "FNO", "NSE-NIFTY-27Jun24-FUT"
        _write_1m(lake, ex, seg, sym, _e2_h1_fno_bars())
        materialize_lake_timeframes(
            lake_root=lake,
            exchange=ex,
            segment=seg,
            symbol=sym,
            kind=PrebuildKind.FUTURES,
            tfs=("5m",),
            include_daily=False,
            incremental=False,
        )
        stored_1m = (
            _read(lake, ex, seg, sym, "1minute")
            .rename({"timestamp": "ts_utc"})
            .select("ts_utc", "open", "high", "low", "close", "volume", "open_interest")
        )
        expected = resample_1m_to_tf(stored_1m, 5).select(
            "ts_utc", "open", "high", "low", "close", "volume"
        )
        stored_5m = (
            _read(lake, ex, seg, sym, "5minute")
            .rename({"timestamp": "ts_utc"})
            .select("ts_utc", "open", "high", "low", "close", "volume")
        )
        assert stored_5m.cast(expected.schema).equals(expected)


class TestWriterConventionRederive:
    """The stamp bump: a legacy (pre-audit) lake re-derives ONCE, then skips."""

    def test_legacy_stamp_forces_one_full_rederive_then_skips(self, tmp_path: Path) -> None:
        import json

        from brutex.data.derive_stamp import derive_stamp_path, load_derive_stamp

        lake = tmp_path / "lake"
        ex, seg, sym = "NSE", "FNO", "NSE-NIFTY-27Jun24-FUT"
        _write_1m(lake, ex, seg, sym, _e2_h1_fno_bars())

        def _materialize():
            return materialize_lake_timeframes(
                lake_root=lake,
                exchange=ex,
                segment=seg,
                symbol=sym,
                kind=PrebuildKind.FUTURES,
                tfs=("5m",),
                include_daily=False,
                incremental=True,
            )

        # First (incremental) run derives + stamps the CURRENT convention.
        first = _materialize()
        assert first.unchanged is False
        stamp_file = derive_stamp_path(lake, ex, seg, sym)
        stamp = load_derive_stamp(stamp_file)
        assert stamp is not None and stamp.writer_convention == 1

        # Rewrite the stamp as a LEGACY pre-audit one (no writer_convention
        # field) beside legacy 0-OI derived bytes — the on-disk state a
        # pre-fix prebuild left behind.
        raw = json.loads(stamp_file.read_text(encoding="utf-8"))
        del raw["writer_convention"]
        stamp_file.write_text(json.dumps(raw), encoding="utf-8")
        p5 = lake / "bars" / ex / seg / sym / "5minute" / "2024" / "06.parquet"
        legacy = pl.read_parquet(p5).with_columns(
            pl.col("open_interest").fill_null(0).alias("open_interest")
        )
        legacy.write_parquet(p5)
        assert _read(lake, ex, seg, sym, "5minute").get_column("open_interest").to_list() == [
            1004,
            0,
            0,
        ]

        # The next incremental prebuild does NOT skip (legacy convention →
        # full re-derive) and latest-wins rewrites the honest nulls.
        second = _materialize()
        assert second.unchanged is False
        assert _read(lake, ex, seg, sym, "5minute").get_column("open_interest").to_list() == [
            1004,
            None,
            None,
        ]

        # A THIRD run under the fresh current-convention stamp skips (the
        # one-time cost is exactly one full re-derive).
        third = _materialize()
        assert third.unchanged is True
