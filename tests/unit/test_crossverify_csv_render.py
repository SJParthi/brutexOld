"""Unit tests — :mod:`brutex.crossverify.csv_render` (the pinned byte contract)."""

from __future__ import annotations

from datetime import UTC, datetime, time
from decimal import Decimal

import pytest
from brutex.calendars.nse import SessionType, TradingSession
from brutex.crossverify.canonical import CanonicalIdentity, canonical_identity
from brutex.crossverify.csv_render import (
    CSV_HEADER,
    LINE_TERMINATOR,
    ExportContractError,
    ExportRow,
    render_day_csv,
    render_oi_cell,
    render_price_cell,
    render_timestamp_ist,
)

_DAY_SESSION = TradingSession(
    date=datetime(2026, 7, 10).date(),
    session_type=SessionType.REGULAR,
    open_time_ist=time(9, 15),
    close_time_ist=time(15, 30),
)


def _identity(segment: str = "INDEX", symbol: str = "NIFTY") -> CanonicalIdentity:
    identity = canonical_identity(exchange="NSE", segment=segment, lake_symbol=symbol)
    assert identity is not None
    return identity


_FUT_IDENTITY = canonical_identity(
    exchange="NSE", segment="FNO", lake_symbol="NSE-NIFTY-30Jul26-FUT"
)
assert _FUT_IDENTITY is not None


def _utc(hh: int, mm: int, *, ss: int = 0, us: int = 0) -> datetime:
    """The UTC instant whose IST wall clock on 2026-07-10 is hh:mm (IST=UTC+5:30)."""
    ist_minutes = hh * 60 + mm - 330
    return datetime(2026, 7, 10, ist_minutes // 60, ist_minutes % 60, ss, us, tzinfo=UTC)


def _row(
    hh: int = 9,
    mm: int = 15,
    *,
    price: str = "100.00",
    volume: int = 10,
    oi: int | None = None,
    ss: int = 0,
    us: int = 0,
) -> ExportRow:
    p = Decimal(price)
    return ExportRow(
        ts_utc=_utc(hh, mm, ss=ss, us=us), open=p, high=p, low=p, close=p, volume=volume, oi=oi
    )


class TestCellRenderers:
    def test_price_lossless_post_snap(self) -> None:
        assert render_price_cell(14020.8) == "14020.80"

    def test_price_legacy_noise_reproduces_canonical_2dp(self) -> None:
        assert render_price_cell(14020.799999999999) == "14020.80"

    def test_price_half_boundary_half_up(self) -> None:
        # The bxFixed2 class: string-based HALF_UP, never IEEE f-string.
        assert render_price_cell(Decimal("1.005")) == "1.01"
        assert f"{1.005:.2f}" == "1.00"  # the raw-float trap this law avoids

    def test_price_decimal_passthrough(self) -> None:
        assert render_price_cell(Decimal("24258")) == "24258.00"

    def test_oi_none_renders_empty(self) -> None:
        assert render_oi_cell(None) == ""

    def test_oi_zero_renders_zero(self) -> None:
        assert render_oi_cell(0) == "0"

    def test_oi_int(self) -> None:
        assert render_oi_cell(12345) == "12345"

    def test_timestamp_ist_render(self) -> None:
        assert render_timestamp_ist(_utc(9, 15)) == "2026-07-10 09:15:00"


class TestRenderDayCsv:
    def test_header_bytes_exact(self) -> None:
        body = render_day_csv(identity=_identity(), rows=[_row()], session=_DAY_SESSION)
        assert body.split(b"\n")[0] == b"symbol,timestamp_ist,open,high,low,close,volume,oi"
        assert CSV_HEADER == "symbol,timestamp_ist,open,high,low,close,volume,oi"

    def test_lf_terminator_and_trailing_newline(self) -> None:
        body = render_day_csv(
            identity=_identity(), rows=[_row(), _row(9, 16)], session=_DAY_SESSION
        )
        assert LINE_TERMINATOR == "\n"
        assert b"\r" not in body
        assert body.endswith(b"\n")
        assert not body.endswith(b"\n\n")  # no blank final line
        assert body.count(b"\n") == 3  # header + 2 rows

    def test_no_bom(self) -> None:
        body = render_day_csv(identity=_identity(), rows=[_row()], session=_DAY_SESSION)
        assert not body.startswith(b"\xef\xbb\xbf")

    def test_spot_row_bytes(self) -> None:
        body = render_day_csv(
            identity=_identity(),
            rows=[_row(9, 15, price="24258.35", volume=0)],
            session=_DAY_SESSION,
        )
        assert body == (
            b"symbol,timestamp_ist,open,high,low,close,volume,oi\n"
            b"NIFTY,2026-07-10 09:15:00,24258.35,24258.35,24258.35,24258.35,0,\n"
        )

    def test_fut_row_bytes_with_zero_oi(self) -> None:
        assert _FUT_IDENTITY is not None
        body = render_day_csv(
            identity=_FUT_IDENTITY,
            rows=[_row(9, 15, price="24300.00", volume=5, oi=0)],
            session=_DAY_SESSION,
        )
        assert body.endswith(b"NIFTY-2026-07-30-FUT,2026-07-10 09:15:00,24300.00,24300.00,24300.00,24300.00,5,0\n")

    def test_no_quote_ever(self) -> None:
        body = render_day_csv(
            identity=_identity(segment="CASH", symbol="M&M"),
            rows=[_row(9, 15)],
            session=_DAY_SESSION,
        )
        assert b'"' not in body
        assert b"M&M," in body


class TestRefusals:
    def test_empty_rows(self) -> None:
        with pytest.raises(ExportContractError, match="empty row set"):
            render_day_csv(identity=_identity(), rows=[], session=_DAY_SESSION)

    def test_duplicate_minutes(self) -> None:
        with pytest.raises(ExportContractError, match="strictly increasing"):
            render_day_csv(identity=_identity(), rows=[_row(), _row()], session=_DAY_SESSION)

    def test_non_increasing_minutes(self) -> None:
        with pytest.raises(ExportContractError, match="strictly increasing"):
            render_day_csv(
                identity=_identity(), rows=[_row(9, 16), _row(9, 15)], session=_DAY_SESSION
            )

    def test_naive_timestamp(self) -> None:
        row = ExportRow(
            ts_utc=datetime(2026, 7, 10, 3, 45),  # noqa: DTZ001 — the refusal under test
            open=Decimal("1"),
            high=Decimal("1"),
            low=Decimal("1"),
            close=Decimal("1"),
            volume=0,
            oi=None,
        )
        with pytest.raises(ExportContractError, match="naive timestamp"):
            render_day_csv(identity=_identity(), rows=[row], session=_DAY_SESSION)

    @pytest.mark.parametrize("bad", ["0.00", "-1.00", "NaN", "Infinity"])
    def test_bad_price(self, bad: str) -> None:
        with pytest.raises(ExportContractError, match="finite positive price"):
            render_day_csv(
                identity=_identity(), rows=[_row(price=bad)], session=_DAY_SESSION
            )

    def test_negative_volume(self) -> None:
        with pytest.raises(ExportContractError, match="negative volume"):
            render_day_csv(
                identity=_identity(), rows=[_row(volume=-1)], session=_DAY_SESSION
            )

    def test_spot_with_oi_refused(self) -> None:
        with pytest.raises(ExportContractError, match="spot row carries oi"):
            render_day_csv(identity=_identity(), rows=[_row(oi=5)], session=_DAY_SESSION)

    def test_fut_null_oi_refused(self) -> None:
        # The §1.1 oi law tripwire: a NULL-OI futures row must NEVER become
        # an empty cell in a published futures file.
        assert _FUT_IDENTITY is not None
        with pytest.raises(ExportContractError, match="NULL oi"):
            render_day_csv(identity=_FUT_IDENTITY, rows=[_row(oi=None)], session=_DAY_SESSION)

    def test_fut_negative_oi_refused(self) -> None:
        assert _FUT_IDENTITY is not None
        with pytest.raises(ExportContractError, match="negative oi"):
            render_day_csv(identity=_FUT_IDENTITY, rows=[_row(oi=-1)], session=_DAY_SESSION)

    def test_pre_open_row_refused(self) -> None:
        with pytest.raises(ExportContractError, match="outside the session window"):
            render_day_csv(identity=_identity(), rows=[_row(9, 14)], session=_DAY_SESSION)

    def test_at_close_row_refused(self) -> None:
        # 15:30 is the EXCLUSIVE close — the last legal minute-open is 15:29.
        with pytest.raises(ExportContractError, match="outside the session window"):
            render_day_csv(identity=_identity(), rows=[_row(15, 30)], session=_DAY_SESSION)

    def test_last_legal_minute_accepted(self) -> None:
        body = render_day_csv(identity=_identity(), rows=[_row(15, 29)], session=_DAY_SESSION)
        assert b"15:29:00" in body

    def test_wrong_day_row_refused(self) -> None:
        row = ExportRow(
            ts_utc=_utc(9, 15).replace(day=9),
            open=Decimal("1"),
            high=Decimal("1"),
            low=Decimal("1"),
            close=Decimal("1"),
            volume=0,
            oi=None,
        )
        with pytest.raises(ExportContractError, match="outside the session window"):
            render_day_csv(identity=_identity(), rows=[row], session=_DAY_SESSION)

    def test_off_minute_grid_seconds_refused(self) -> None:
        with pytest.raises(ExportContractError, match="outside the session window"):
            render_day_csv(identity=_identity(), rows=[_row(ss=30)], session=_DAY_SESSION)

    def test_off_minute_grid_microseconds_refused(self) -> None:
        with pytest.raises(ExportContractError, match="outside the session window"):
            render_day_csv(identity=_identity(), rows=[_row(us=1)], session=_DAY_SESSION)

    def test_csv_safe_mutable_symbol_refused_never_mutated(self) -> None:
        # A synthetic `=cmd` symbol REFUSES — the file never carries `'=cmd`.
        hostile = CanonicalIdentity(
            exchange="NSE",
            lake_segment="CASH",
            lake_symbol="=cmd",
            canonical_symbol="=cmd",
            segment_token="NSE_EQ",
            instrument_class="cash_spot",
        )
        with pytest.raises(ExportContractError, match="formula-injection"):
            render_day_csv(identity=hostile, rows=[_row()], session=_DAY_SESSION)

    def test_csv_metachar_cell_refused(self) -> None:
        hostile = CanonicalIdentity(
            exchange="NSE",
            lake_segment="CASH",
            lake_symbol="A,B",
            canonical_symbol="A,B",
            segment_token="NSE_EQ",
            instrument_class="cash_spot",
        )
        with pytest.raises(ExportContractError, match="CSV metacharacter"):
            render_day_csv(identity=hostile, rows=[_row()], session=_DAY_SESSION)


class TestMuhuratSessionWindow:
    def test_muhurat_evening_rows_render_against_their_own_session(self) -> None:
        # The renderer judges rows against the SESSION given — a Muhurat
        # evening bar is in-session for its own 18:00-19:00 window. (The
        # runner never reaches here on Muhurat — whole-run skip — but the
        # renderer's law is session-shaped, not hardcoded 09:15-15:30.)
        session = TradingSession(
            date=datetime(2026, 7, 10).date(),
            session_type=SessionType.MUHURAT,
            open_time_ist=time(18, 0),
            close_time_ist=time(19, 0),
        )
        body = render_day_csv(identity=_identity(), rows=[_row(18, 0)], session=session)
        assert b"18:00:00" in body
