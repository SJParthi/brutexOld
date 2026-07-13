"""DEC-DAILY-WINDOW-SANITY-GATE-001 — price-sanity on the daily 1m window read.

The E2-H4 confirmed defect (derived-TF audit, DEC-DERIVED-TF-AUDIT-001):
``make_lake_read_1m_window`` — the injected reader behind EVERY window-read
daily derive (heal-recall 1day re-derive, crossverify day-scoped derive,
ledger daily reconcile) — had NO price-sanity gate, so a corrupt 1m row
(``low=0.0``, internally ordered — the legacy pre-#912 class ``CandleBar``
deliberately permits) that the INTRADAY prebuild loudly quarantined was
silently LAUNDERED into a derived ``1day`` bar (live-reproduced: a stored
1day ``low=0.0``, report ``errored=0``). The reader now runs the SAME COMMON
``validate_frame_price_sanity`` gate the intraday read boundary runs; the
raised ``ResampleInputError`` is already the per-target quarantine class
``derive_daily_for_targets`` counts as ``errored`` — correct-or-absent with
zero wiring changes.

``test_corrupt_low_quarantined_not_laundered`` fails at the merge base (the
fix-reversion evidence): pre-fix it derived a 1day bar with ``low=0.0``.
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
from brutex.data.spot_daily_resample import (
    DailyDeriveTarget,
    derive_daily_for_targets,
    make_lake_read_1m_window,
    make_lake_write_1day,
)

_IST = timezone(timedelta(hours=5, minutes=30))
_EX, _SEG, _SYM = "NSE", "INDEX", "NSE-NIFTY"


def _ist(y: int, mo: int, d: int, h: int, mi: int) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=_IST).astimezone(UTC)


def _bar(ts: datetime, px: float, *, low: float | None = None, vol: int = 100) -> CandleBar:
    low_val = px - 0.5 if low is None else low
    return CandleBar(
        timestamp=ts,
        open=Decimal(str(px)),
        high=Decimal(str(px + 0.5)),
        low=Decimal(str(low_val)),
        close=Decimal(str(px + 0.1)),
        volume=vol,
        open_interest=None,
    )


def _session_bars(*, corrupt_bar7: bool) -> list[CandleBar]:
    """The E2-H4 session verbatim: 15 bars, optionally bar #7 with low=0.0
    (internally ORDERED — the write succeeds; the legacy corrupt class)."""
    bars: list[CandleBar] = []
    for i in range(15):
        mins = 9 * 60 + 15 + i
        px = 100.0 + i * 0.05
        low = 0.0 if (corrupt_bar7 and i == 7) else None
        bars.append(_bar(_ist(2024, 6, 12, mins // 60, mins % 60), px, low=low, vol=100 + i))
    return bars


def _write_1m(lake: Path, bars: list[CandleBar]) -> None:
    sink = ParquetBarSink(writer=ParquetWriter(lake_root=lake))
    sink.append(exchange=_EX, segment=_SEG, symbol=_SYM, interval="1minute", bars=bars)


def _derive_daily(lake: Path):
    """The EXACT wiring the heal-recall / crossverify daily paths use."""
    target = DailyDeriveTarget(
        exchange=_EX, segment=_SEG, symbol=_SYM, start=date(2024, 6, 11), end=date(2024, 6, 13)
    )
    return derive_daily_for_targets(
        targets=[target],
        build_read_1m=lambda t: make_lake_read_1m_window(
            lake_root=lake,
            exchange=t.exchange,
            segment=t.segment,
            symbol=t.symbol,
            start=t.start,
            end=t.end,
        ),
        build_write_1day=lambda t: make_lake_write_1day(
            lake_root=lake, exchange=t.exchange, segment=t.segment, symbol=t.symbol
        ),
        before=date(2024, 6, 13),
    )


class TestDailyWindowReadSanityGate:
    def test_corrupt_low_quarantined_not_laundered(self, tmp_path: Path) -> None:
        """The E2-H4 repro: the corrupt lake is now quarantined (errored=1)
        and NO 1day row is written — correct-or-absent, matching the intraday
        prebuild's verdict on the SAME lake."""
        lake = tmp_path / "lake"
        _write_1m(lake, _session_bars(corrupt_bar7=True))
        report = _derive_daily(lake)
        assert report.errored == 1
        assert report.derived == 0
        assert report.bars == 0
        daily_dir = lake / "bars" / _EX / _SEG / _SYM / "1day"
        assert not daily_dir.exists(), "a laundered 1day partition was written"

    def test_reader_raises_the_quarantine_class(self, tmp_path: Path) -> None:
        """The gate raises the typed ResampleInputError naming the coordinates
        — the SAME per-target quarantine class the intraday prebuild uses."""
        from brutex.data.errors import ResampleInputError

        lake = tmp_path / "lake"
        _write_1m(lake, _session_bars(corrupt_bar7=True))
        read = make_lake_read_1m_window(
            lake_root=lake,
            exchange=_EX,
            segment=_SEG,
            symbol=_SYM,
            start=date(2024, 6, 11),
            end=date(2024, 6, 13),
        )
        with pytest.raises(ResampleInputError, match="daily window read"):
            read()

    def test_clean_lake_derives_and_read_is_byte_identical(self, tmp_path: Path) -> None:
        """Parity pin: on a HEALTHY lake the gated reader's output frame is
        byte-identical to the pre-gate read (a manual raw-read twin), and the
        daily derive succeeds exactly as before."""
        lake = tmp_path / "lake"
        _write_1m(lake, _session_bars(corrupt_bar7=False))
        read = make_lake_read_1m_window(
            lake_root=lake,
            exchange=_EX,
            segment=_SEG,
            symbol=_SYM,
            start=date(2024, 6, 11),
            end=date(2024, 6, 13),
        )
        frame = read()
        # The manual twin — the reader's documented projection, no gate.
        glob = (lake / "bars" / _EX / _SEG / _SYM / "1minute").as_posix() + "/**/*.parquet"
        ist_date = (pl.col("ts_utc") + pl.duration(hours=5, minutes=30)).dt.date()
        expected = (
            pl.read_parquet(glob)
            .rename({"timestamp": "ts_utc"})
            .with_columns(ist_date.alias("_ist_date"))
            .filter(
                (pl.col("_ist_date") >= date(2024, 6, 11))
                & (pl.col("_ist_date") < date(2024, 6, 13))
            )
            .select("ts_utc", "open", "high", "low", "close", "volume")
            .sort("ts_utc")
        )
        assert frame.equals(expected)

        report = _derive_daily(lake)
        assert report.errored == 0
        assert report.derived == 1
        stored = pl.read_parquet(
            (lake / "bars" / _EX / _SEG / _SYM / "1day").as_posix() + "/**/*.parquet"
        )
        assert stored.height == 1
        assert stored.get_column("low").to_list() == [99.5]  # min(px - 0.5)

    def test_empty_window_stays_a_clean_noop(self, tmp_path: Path) -> None:
        """No covered partition → the EMPTY frame contract is untouched (the
        gate no-ops on height-0 frames — never a raise on nothing-to-derive)."""
        read = make_lake_read_1m_window(
            lake_root=tmp_path / "lake",
            exchange=_EX,
            segment=_SEG,
            symbol=_SYM,
            start=date(2024, 6, 11),
            end=date(2024, 6, 13),
        )
        assert read().height == 0
