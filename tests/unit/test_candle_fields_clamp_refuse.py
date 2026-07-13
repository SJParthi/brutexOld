"""DEC-OHLC-CLAMP-BOUNDED-001 — the magnitude-bounded vendor-OHLC clamp.

The E2-H3 confirmed defect (derived-TF audit, DEC-DERIVED-TF-AUDIT-001): the
unbounded ``assemble_candle`` clamp converted a corrupt ×10 vendor OPEN
(``open=1000.55`` beside a sane ``high=100.85``) into a STORED fabricated
``high=1000.55`` that passed price-sanity (ordering-only), passed the
close-only bad-tick screen, reached the 1m sweep, and poisoned every derived
bucket + the 1day PDH anchor. The bounded clamp refuses such a row (graceful
counted skip — the partial-absent class) instead of fabricating the extreme;
a within-4× clamp (the 1-tick under-report the clamp was built for, even on a
genuinely wide-range bar) stores byte-identically.

Every test in ``TestE2H3PayloadRefused`` fails at the merge base (the
fix-reversion evidence): pre-fix, row #7 parsed with ``high=1000.55``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.adapters.groww.parser import parse_candle_rows
from brutex.data.candle_fields import (
    OHLC_CLAMP_REFUSE_ENV,
    EmptyCandleRow,
    assemble_candle,
    resolve_clamp_refuse_enabled,
)
from structlog.testing import capture_logs


def _parse_ts(raw: object) -> datetime:
    return datetime.fromisoformat(str(raw)).replace(tzinfo=UTC)


def _assemble(
    open_: float,
    high: float,
    low: float,
    close: float,
    *,
    clamp_refuse: bool = True,
    on_clamp=None,
    on_clamp_refused=None,
):
    return assemble_candle(
        ts_raw="2024-06-12T09:22:00",
        parse_timestamp=_parse_ts,
        open_=open_,
        high=high,
        low=low,
        close=close,
        volume=100,
        open_interest=None,
        on_clamp=on_clamp,
        clamp_refuse=clamp_refuse,
        on_clamp_refused=on_clamp_refused,
    )


def _e2_h3_rows() -> list[list[object]]:
    """The E2-H3 vendor payload verbatim: row #7 carries a corrupt ×10 OPEN."""
    rows: list[list[object]] = []
    for i in range(15):
        mins = 9 * 60 + 15 + i
        ts = f"2024-06-12T{mins // 60:02d}:{mins % 60:02d}:00"
        px = 100.0 + i * 0.05
        if i == 7:
            rows.append([ts, 1000.55, px + 0.5, px - 0.5, px + 0.1, 107, None])
        else:
            rows.append([ts, px, px + 0.5, px - 0.5, px + 0.1, 100 + i, None])
    return rows


class TestResolveClampRefuseEnabled:
    """The fail-loud env switch (the BRUTEX_TODAY_COMPLETENESS_GATE template)."""

    @pytest.mark.parametrize("token", ["", "1", "on", "true", "yes", " ON ", "True"])
    def test_on_tokens_enable(self, token: str) -> None:
        assert resolve_clamp_refuse_enabled({OHLC_CLAMP_REFUSE_ENV: token}) is True

    def test_unset_is_enabled(self) -> None:
        assert resolve_clamp_refuse_enabled({}) is True

    @pytest.mark.parametrize("token", ["0", "off", "false", "no", " Off "])
    def test_off_tokens_disable(self, token: str) -> None:
        assert resolve_clamp_refuse_enabled({OHLC_CLAMP_REFUSE_ENV: token}) is False

    def test_junk_fails_loud(self) -> None:
        with pytest.raises(ValueError, match="BRUTEX_OHLC_CLAMP_REFUSE"):
            resolve_clamp_refuse_enabled({OHLC_CLAMP_REFUSE_ENV: "banana"})

    def test_none_reads_process_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(OHLC_CLAMP_REFUSE_ENV, "off")
        assert resolve_clamp_refuse_enabled() is False
        monkeypatch.delenv(OHLC_CLAMP_REFUSE_ENV)
        assert resolve_clamp_refuse_enabled() is True


class TestAssembleCandleBoundedClamp:
    """The refusal law at the assembler: strict >4× MOVE refuses; ≤4× stores."""

    def test_corrupt_x10_open_is_refused(self) -> None:
        with pytest.raises(EmptyCandleRow, match="DEC-OHLC-CLAMP-BOUNDED-001"):
            _assemble(1000.55, 100.85, 99.85, 100.45)

    def test_one_tick_clamp_on_wide_range_bar_still_stores(self) -> None:
        """The D2-semantics killer: a deep-OTM bar with a GENUINE ≥4× range
        (low 0.05, high 0.25) plus a 1-tick vendor under-report (open 0.26)
        NEEDS the clamp and must STORE — the clamp MOVE (0.25 → 0.26) is tiny
        even though the resulting range is wide. A result-RANGE test would
        wrongly refuse this healthy bar; the clamp-MOVE test keeps it."""
        clamps: list[int] = []
        bar = _assemble(0.26, 0.25, 0.05, 0.10, on_clamp=lambda: clamps.append(1))
        assert bar.high == Decimal("0.26")
        assert bar.low == Decimal("0.05")
        assert clamps == [1]

    def test_boundary_exactly_4x_high_move_still_stores(self) -> None:
        """The boundary is STRICT: a clamp landing at EXACTLY 4× the vendor
        high stores (clamped_high == high · 4 fails the strict ``>``)."""
        bar = _assemble(400.0, 100.0, 99.0, 100.0)
        assert bar.high == Decimal("400.0")

    def test_just_past_4x_high_move_is_refused(self) -> None:
        with pytest.raises(EmptyCandleRow):
            _assemble(400.01, 100.0, 99.0, 100.0)

    def test_boundary_exactly_4x_low_move_still_stores(self) -> None:
        """Low side: clamped_low · 4 == low exactly → stored (strict ``<``)."""
        bar = _assemble(25.0, 101.0, 100.0, 100.5)
        assert bar.low == Decimal("25.0")

    def test_just_past_4x_low_move_is_refused(self) -> None:
        with pytest.raises(EmptyCandleRow):
            _assemble(24.99, 101.0, 100.0, 100.5)

    def test_refusal_fires_hook_before_raise_and_skips_on_clamp(self) -> None:
        """A refused row counts via on_clamp_refused and must NOT count as a
        clamp (no bar was built — the clamp never fired)."""
        clamps: list[int] = []
        refused: list[int] = []
        with pytest.raises(EmptyCandleRow):
            _assemble(
                1000.55,
                100.85,
                99.85,
                100.45,
                on_clamp=lambda: clamps.append(1),
                on_clamp_refused=lambda: refused.append(1),
            )
        assert refused == [1]
        assert clamps == []

    def test_refusal_without_hook_still_refuses(self) -> None:
        with pytest.raises(EmptyCandleRow):
            _assemble(1000.55, 100.85, 99.85, 100.45, on_clamp_refused=None)

    def test_kill_switch_off_restores_legacy_clamp_store(self) -> None:
        """clamp_refuse=False is the LEGACY unbounded clamp, byte-identical:
        the corrupt open is legitimized into the widened high (the pre-fix
        behaviour, kept reachable for a forensic replay only)."""
        clamps: list[int] = []
        refused: list[int] = []
        bar = _assemble(
            1000.55,
            100.85,
            99.85,
            100.45,
            clamp_refuse=False,
            on_clamp=lambda: clamps.append(1),
            on_clamp_refused=lambda: refused.append(1),
        )
        assert bar.high == Decimal("1000.55")
        assert clamps == [1]
        assert refused == []


class TestE2H3PayloadRefused:
    """The E2-H3 payload through the REAL Groww parser (fails at merge-base)."""

    def test_row7_refused_counted_and_warned_others_stored(self) -> None:
        rows = _e2_h3_rows()
        with capture_logs() as logs:
            bars = parse_candle_rows({"candles": rows})
        assert len(bars) == 14  # 15 rows, row #7 refused
        assert all(float(b.high) < 200.0 for b in bars)  # no fabricated extreme
        refuse_events = [e for e in logs if e["event"] == "groww.parse.ohlc_clamp_refused"]
        assert len(refuse_events) == 1  # ONE WARNING per window
        event = refuse_events[0]
        assert event["log_level"] == "warning"
        assert event["refused_bars"] == 1
        assert event["total_rows"] == 15
        offender = event["first_offender"]
        row7 = rows[7]
        assert offender["ts"] == repr("2024-06-12T09:22:00")
        assert offender["open"] == repr(1000.55)
        assert offender["high"] == repr(row7[2])
        assert offender["low"] == repr(row7[3])
        assert offender["close"] == repr(row7[4])
        # The refused row never rides the ohlc_clamped counter (no clamp fired).
        assert not [e for e in logs if e["event"] == "groww.parse.ohlc_clamped"]

    def test_two_refused_rows_keep_the_first_offender(self) -> None:
        rows = _e2_h3_rows()
        rows[9] = ["2024-06-12T09:24:00", 999.99, 100.95, 99.95, 100.55, 109, None]
        with capture_logs() as logs:
            bars = parse_candle_rows({"candles": rows})
        assert len(bars) == 13
        (event,) = [e for e in logs if e["event"] == "groww.parse.ohlc_clamp_refused"]
        assert event["refused_bars"] == 2
        assert event["first_offender"]["ts"] == repr("2024-06-12T09:22:00")

    def test_healthy_payload_byte_identical_no_refuse_warning(self) -> None:
        rows = [r for i, r in enumerate(_e2_h3_rows()) if i != 7]
        with capture_logs() as logs:
            bars = parse_candle_rows({"candles": rows})
        assert len(bars) == 14
        assert not [e for e in logs if e["event"] == "groww.parse.ohlc_clamp_refused"]

    def test_kill_switch_off_parses_the_legacy_clamped_bar(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(OHLC_CLAMP_REFUSE_ENV, "0")
        with capture_logs() as logs:
            bars = parse_candle_rows({"candles": _e2_h3_rows()})
        assert len(bars) == 15  # legacy: row #7 stored with the widened high
        assert float(bars[7].high) == 1000.55
        assert [e for e in logs if e["event"] == "groww.parse.ohlc_clamped"]
        assert not [e for e in logs if e["event"] == "groww.parse.ohlc_clamp_refused"]

    def test_junk_env_fails_loud_at_window_start(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(OHLC_CLAMP_REFUSE_ENV, "banana")
        with pytest.raises(ValueError, match="BRUTEX_OHLC_CLAMP_REFUSE"):
            parse_candle_rows({"candles": _e2_h3_rows()})


class TestE2H3EndToEnd:
    """Parse → real sink → real prebuild: ZERO fabricated highs at 1m/5m/1day."""

    def test_no_fabricated_extreme_reaches_any_stored_timeframe(self, tmp_path: Path) -> None:
        pl = pytest.importorskip("polars")
        pytest.importorskip("pyarrow")
        from datetime import date

        from brutex.data.parquet_writer import ParquetWriter
        from brutex.data.sink import ParquetBarSink
        from brutex.data.timeframe_materialize import (
            PrebuildKind,
            materialize_lake_timeframes,
        )

        lake = tmp_path / "lake"
        bars = parse_candle_rows({"candles": _e2_h3_rows()})
        sink = ParquetBarSink(writer=ParquetWriter(lake_root=lake))
        sink.append(
            exchange="NSE", segment="INDEX", symbol="NSE-NIFTY", interval="1minute", bars=bars
        )
        materialize_lake_timeframes(
            lake_root=lake,
            exchange="NSE",
            segment="INDEX",
            symbol="NSE-NIFTY",
            kind=PrebuildKind.SPOT,
            tfs=("5m",),
            include_daily=True,
            daily_before=date(2024, 6, 13),
            incremental=False,
        )
        for interval in ("1minute", "5minute", "1day"):
            glob = (
                lake / "bars" / "NSE" / "INDEX" / "NSE-NIFTY" / interval
            ).as_posix() + "/**/*.parquet"
            highs = pl.read_parquet(glob).get_column("high").to_list()
            assert highs, f"{interval}: nothing stored"
            assert 1000.55 not in highs, f"{interval}: fabricated high stored"
            assert max(highs) < 200.0, f"{interval}: fabricated extreme stored"
