"""R2-OC-1 regression net: the CLI options-sim lake reader honours the expiry.

``brutex.cli._LakeOptionBarSource`` (the ``brutex options-sim`` production
``OptionBarSource``) used to ``del expiry_date`` and match bars on ONLY
``(underlying, side, strike, timestamp)`` — so two CONCURRENT rolling-lake
series (e.g. a weekly and a monthly) at the same strike COLLIDED and
``_row_at`` silently priced trades from an arbitrary ``row(0)``.

These tests pin the fix with REAL fixture lakes written by the production
:class:`~brutex.data.option_parquet_writer.OptionParquetWriter` (never
hand-rolled parquet):

* the expiry-series resolution (:func:`brutex.cli._expiry_series_candidates`)
  maps an absolute expiry onto the lake's ``(expiry_flag, expiry_code)``
  vocabulary via the SAME :mod:`brutex.options.expiry_calendar` that generated
  the request;
* the reader prices each requested expiry from the CORRECT series (THE
  regression — fails on the pre-fix expiry-blind code);
* an unresolvable expiry or a genuine cross-series ambiguity returns an honest
  ``None`` → the executor's NO_DATA reject, never an arbitrary row;
* the normal single-series happy path is byte-for-byte unchanged.

Whitebox: drives the private ``_LakeOptionBarSource`` / ``_row_at`` seam, so
per the DEC-P0-017 convention the file silences ``reportPrivateUsage``.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

pytest.importorskip("polars")
pytest.importorskip("pyarrow")

import polars as pl
from brutex.cli import (
    _EXPIRY_FLAG_MONTHLY,
    _EXPIRY_FLAG_WEEKLY,
    _expiry_series_candidates,
    _LakeOptionBarSource,
)
from brutex.data.models import OptionBar
from brutex.data.option_parquet_writer import OptionParquetWriter, OptionSeriesCoordinate
from brutex.options.executor import (
    MaterializedTrade,
    RejectedTrade,
    RejectReason,
    SpotSignal,
    materialize_trade,
)
from brutex.options.lot_size_history import OptionsUnderlying
from brutex.options.strike_rules import RULES_BY_NAME, TradeDirection

_IST = ZoneInfo("Asia/Kolkata")

# Calendar-verified NIFTY dates (brutex.options.expiry_calendar, 2025 regime:
# weekly = Thursday, monthly = last Thursday):
_WEEKLY_EXPIRY = date(2025, 1, 2)  # Thursday
_MONTHLY_EXPIRY = date(2025, 1, 30)  # last Thursday of Jan 2025
_ENTRY = datetime(2025, 1, 2, 9, 16, tzinfo=_IST)
_EXIT = datetime(2025, 1, 2, 15, 20, tzinfo=_IST)

_STRIKE = 24000


def _bar(
    ts_ist: datetime,
    premium: str,
    *,
    side: str = "CE",
    open_interest: int | None = 1000,
    spot: Decimal | None = None,
    iv: Decimal | None = None,
) -> OptionBar:
    p = Decimal(premium)
    return OptionBar(
        timestamp=ts_ist.astimezone(UTC),
        open=p,
        high=p,
        low=p,
        close=p,
        volume=10,
        open_interest=open_interest,
        side=side,
        strike=Decimal(_STRIKE),
        spot=spot,
        iv=iv,
    )


def _coordinate(expiry_flag: str, expiry_code: int) -> OptionSeriesCoordinate:
    return OptionSeriesCoordinate(
        exch_id="NSE",
        exchange_segment="NSE_FNO",
        security_id=13,
        underlying_symbol="NIFTY",
        expiry_flag=expiry_flag,
        expiry_code=expiry_code,
        strike_offset="ATM",
    )


def _write_series(
    lake_root: Path, expiry_flag: str, expiry_code: int, bars: list[OptionBar]
) -> None:
    """Write one rolling-lake series with the REAL production writer."""
    OptionParquetWriter(lake_root=lake_root).write(
        partition_key=f"NSE_FNO|13|{expiry_flag}|{expiry_code}|ATM|CALL",
        coordinate=_coordinate(expiry_flag, expiry_code),
        bars=bars,
    )


def _signal(expiry: date, *, entry: datetime = _ENTRY, exit_: datetime = _EXIT) -> SpotSignal:
    return SpotSignal(
        instrument=OptionsUnderlying.NIFTY,
        direction=TradeDirection.BULL,  # → buy CE
        entry_ts=entry,
        exit_ts=exit_,
        spot_at_entry=Decimal(_STRIKE),  # ATM = 24000 on the 50-step grid
        expiry_date=expiry,
    )


def _load(
    lake_root: Path,
    expiry: date,
    *,
    entry: datetime = _ENTRY,
    exit_: datetime = _EXIT,
    strike: int = _STRIKE,
) -> object:
    return _LakeOptionBarSource(lake_root=lake_root).load_pair(
        instrument=OptionsUnderlying.NIFTY,
        expiry_date=expiry,
        strike=strike,
        option_type="CE",
        entry_ts=entry,
        exit_ts=exit_,
    )


# ────────────────────────────────────────────────────────────────────────
# _expiry_series_candidates — the pure expiry → (flag, code) resolution
# ────────────────────────────────────────────────────────────────────────


class TestExpirySeriesCandidates:
    def test_weekly_near(self) -> None:
        got = _expiry_series_candidates(
            OptionsUnderlying.NIFTY, _WEEKLY_EXPIRY, on_date=date(2025, 1, 2)
        )
        assert got == ((_EXPIRY_FLAG_WEEKLY, 0),)

    def test_weekly_next_and_far_codes(self) -> None:
        # From the Jan-2 expiry day, Jan-9 is the NEXT weekly and Jan-16 the FAR.
        assert _expiry_series_candidates(
            OptionsUnderlying.NIFTY, date(2025, 1, 9), on_date=date(2025, 1, 2)
        ) == ((_EXPIRY_FLAG_WEEKLY, 1),)
        assert _expiry_series_candidates(
            OptionsUnderlying.NIFTY, date(2025, 1, 16), on_date=date(2025, 1, 2)
        ) == ((_EXPIRY_FLAG_WEEKLY, 2),)

    def test_monthly_near(self) -> None:
        got = _expiry_series_candidates(
            OptionsUnderlying.NIFTY, _MONTHLY_EXPIRY, on_date=date(2025, 1, 2)
        )
        assert got == ((_EXPIRY_FLAG_MONTHLY, 0),)

    def test_monthly_next_code(self) -> None:
        # Feb-27 2025 (last Thursday of Feb) is the NEXT monthly from Jan-3;
        # the weekly walk exhausts its Near/Next/Far codes without a match.
        got = _expiry_series_candidates(
            OptionsUnderlying.NIFTY, date(2025, 2, 27), on_date=date(2025, 1, 3)
        )
        assert got == ((_EXPIRY_FLAG_MONTHLY, 1),)

    def test_monthly_that_is_also_a_weekly_matches_both(self) -> None:
        # Jan-30 (the monthly) is ALSO the near weekly as seen from Jan-27 —
        # the SAME physical contract addressed by two series.
        got = _expiry_series_candidates(
            OptionsUnderlying.NIFTY, _MONTHLY_EXPIRY, on_date=date(2025, 1, 27)
        )
        assert got == ((_EXPIRY_FLAG_WEEKLY, 0), (_EXPIRY_FLAG_MONTHLY, 0))

    def test_banknifty_post_weekly_kill_resolves_monthly_only(self) -> None:
        # SEBI killed the BANKNIFTY weekly cycle EOD 2024-11-13 — the weekly
        # walk yields None and only the monthly series matches.
        got = _expiry_series_candidates(
            OptionsUnderlying.BANKNIFTY, date(2025, 1, 30), on_date=date(2025, 1, 6)
        )
        assert got == ((_EXPIRY_FLAG_MONTHLY, 0),)

    def test_non_expiry_date_is_unresolvable(self) -> None:
        # Jan-7 2025 is a Tuesday — not a NIFTY expiry under the 2025 regime.
        got = _expiry_series_candidates(
            OptionsUnderlying.NIFTY, date(2025, 1, 7), on_date=date(2025, 1, 6)
        )
        assert got == ()

    def test_expiry_beyond_far_selector_is_unresolvable(self) -> None:
        # Feb-6 is a real weekly but the 4th out from Jan-3 — beyond the
        # lake's Near/Next/Far vocabulary; the monthly walk overshoots.
        got = _expiry_series_candidates(
            OptionsUnderlying.NIFTY, date(2025, 2, 6), on_date=date(2025, 1, 3)
        )
        assert got == ()

    def test_far_monthly_beyond_selector_is_unresolvable(self) -> None:
        # Jun-26 2025 is a real monthly but the 6th out from Jan-3 — BOTH
        # walks exhaust their Near/Next/Far codes without a match.
        got = _expiry_series_candidates(
            OptionsUnderlying.NIFTY, date(2025, 6, 26), on_date=date(2025, 1, 3)
        )
        assert got == ()

    def test_on_date_before_calendar_history_is_unresolvable(self) -> None:
        # The expiry calendar's recorded history starts 2021-01-01; a 2020
        # signal must resolve to () (honest reject), never raise or guess.
        got = _expiry_series_candidates(
            OptionsUnderlying.NIFTY, date(2020, 6, 4), on_date=date(2020, 6, 1)
        )
        assert got == ()


# ────────────────────────────────────────────────────────────────────────
# _LakeOptionBarSource — THE R2-OC-1 regression + edge cases
# ────────────────────────────────────────────────────────────────────────


@pytest.fixture
def two_series_lake(tmp_path: Path) -> Path:
    """Two CONCURRENT series colliding on (underlying, side, strike, ts).

    The weekly (WEEK/0, expiring Jan-2) and the monthly (MONTH/0, expiring
    Jan-30) carry the SAME strike at the SAME timestamps with DIFFERENT
    premiums — the exact collision the expiry-blind reader mispriced.
    """
    _write_series(
        tmp_path,
        _EXPIRY_FLAG_WEEKLY,
        0,
        [
            _bar(
                _ENTRY, "150.00", open_interest=None, spot=Decimal("24010.55"), iv=Decimal("0.1523")
            ),
            _bar(_EXIT, "210.00"),
        ],
    )
    _write_series(
        tmp_path,
        _EXPIRY_FLAG_MONTHLY,
        0,
        [_bar(_ENTRY, "300.00"), _bar(_EXIT, "320.00")],
    )
    return tmp_path


class TestExpirySeriesSelection:
    def test_regression_each_expiry_prices_from_its_own_series(self, two_series_lake: Path) -> None:
        """THE R2-OC-1 regression — FAILS on the pre-fix expiry-blind reader.

        The two series demonstrably DISAGREE at the shared timestamp, so an
        expiry-blind ``row(0)`` cannot satisfy both requests: pre-fix, both
        expiries returned the SAME arbitrary bars (one request was priced from
        the WRONG series).
        """
        # The collision is real: at the shared entry ts the two series differ.
        opts = two_series_lake / "options"
        weekly_df = pl.read_parquet(opts / "NSE_FNO__13__WEEK__0__ATM__CALL" / "2025-01.parquet")
        monthly_df = pl.read_parquet(opts / "NSE_FNO__13__MONTH__0__ATM__CALL" / "2025-01.parquet")
        entry_utc = _ENTRY.astimezone(UTC)
        weekly_open = weekly_df.filter(pl.col("timestamp") == entry_utc)["open"][0]
        monthly_open = monthly_df.filter(pl.col("timestamp") == entry_utc)["open"][0]
        assert weekly_open != monthly_open  # same underlying+side+strike+ts, different price

        weekly_pair = _load(two_series_lake, _WEEKLY_EXPIRY)
        monthly_pair = _load(two_series_lake, _MONTHLY_EXPIRY)
        assert weekly_pair is not None and monthly_pair is not None
        # Each request is priced from ITS OWN series...
        assert weekly_pair.entry_bar.open == Decimal("150.00")  # type: ignore[attr-defined]
        assert weekly_pair.exit_bar.open == Decimal("210.00")  # type: ignore[attr-defined]
        assert monthly_pair.entry_bar.open == Decimal("300.00")  # type: ignore[attr-defined]
        assert monthly_pair.exit_bar.open == Decimal("320.00")  # type: ignore[attr-defined]
        # ...so the two requests CANNOT return the same bars (they did pre-fix).
        assert weekly_pair.entry_bar.open != monthly_pair.entry_bar.open  # type: ignore[attr-defined]

    def test_regression_end_to_end_through_the_executor(self, two_series_lake: Path) -> None:
        source = _LakeOptionBarSource(lake_root=two_series_lake)
        weekly = materialize_trade(_signal(_WEEKLY_EXPIRY), RULES_BY_NAME["atm"], source=source)
        monthly = materialize_trade(_signal(_MONTHLY_EXPIRY), RULES_BY_NAME["atm"], source=source)
        assert isinstance(weekly, MaterializedTrade)
        assert isinstance(monthly, MaterializedTrade)
        assert weekly.entry_open_paisa == 15000  # ₹150 — the weekly series
        assert monthly.entry_open_paisa == 30000  # ₹300 — the monthly series

    def test_optional_bar_fields_round_trip(self, two_series_lake: Path) -> None:
        # The weekly entry bar carries spot/iv but no OI; the exit bar the
        # inverse — both None and non-None arms of the row build are exercised.
        pair = _load(two_series_lake, _WEEKLY_EXPIRY)
        assert pair is not None
        entry_bar = pair.entry_bar  # type: ignore[attr-defined]
        exit_bar = pair.exit_bar  # type: ignore[attr-defined]
        assert entry_bar.open_interest is None
        assert entry_bar.spot == Decimal("24010.55")
        assert entry_bar.iv == Decimal("0.1523")
        assert entry_bar.timestamp == _ENTRY.astimezone(UTC)
        assert exit_bar.open_interest == 1000
        assert exit_bar.spot is None
        assert exit_bar.iv is None

    def test_single_series_happy_path_unchanged(self, tmp_path: Path) -> None:
        # The normal one-series lake: the weekly request prices exactly as the
        # pre-fix reader did (byte-for-byte preservation).
        _write_series(
            tmp_path, _EXPIRY_FLAG_WEEKLY, 0, [_bar(_ENTRY, "150.00"), _bar(_EXIT, "210.00")]
        )
        out = materialize_trade(
            _signal(_WEEKLY_EXPIRY),
            RULES_BY_NAME["atm"],
            source=_LakeOptionBarSource(lake_root=tmp_path),
        )
        assert isinstance(out, MaterializedTrade)
        assert out.entry_open_paisa == 15000
        assert out.exit_open_paisa == 21000
        assert out.iv_at_entry is None

    def test_aliased_series_with_identical_rows_is_priced(self, tmp_path: Path) -> None:
        # Jan-30 is BOTH the near weekly (from Jan-27) and the monthly — the
        # same physical contract stored under two series with IDENTICAL bars
        # is benign (keep-first), never a false ambiguity reject.
        entry = datetime(2025, 1, 27, 9, 16, tzinfo=_IST)
        exit_ = datetime(2025, 1, 27, 15, 20, tzinfo=_IST)
        bars = [_bar(entry, "180.00"), _bar(exit_, "190.00")]
        _write_series(tmp_path, _EXPIRY_FLAG_WEEKLY, 0, bars)
        _write_series(tmp_path, _EXPIRY_FLAG_MONTHLY, 0, bars)
        pair = _load(tmp_path, _MONTHLY_EXPIRY, entry=entry, exit_=exit_)
        assert pair is not None
        assert pair.entry_bar.open == Decimal("180.00")  # type: ignore[attr-defined]

    def test_aliased_series_with_conflicting_rows_is_ambiguous_none(self, tmp_path: Path) -> None:
        # Same aliasing, but the two series DISAGREE at the shared ts — a
        # genuine cross-series ambiguity → honest None → NO_DATA (never an
        # arbitrary row), with the structlog event naming the collision.
        from structlog.testing import capture_logs

        entry = datetime(2025, 1, 27, 9, 16, tzinfo=_IST)
        exit_ = datetime(2025, 1, 27, 15, 20, tzinfo=_IST)
        _write_series(
            tmp_path, _EXPIRY_FLAG_WEEKLY, 0, [_bar(entry, "100.00"), _bar(exit_, "110.00")]
        )
        _write_series(
            tmp_path, _EXPIRY_FLAG_MONTHLY, 0, [_bar(entry, "999.00"), _bar(exit_, "110.00")]
        )
        with capture_logs() as logs:
            pair = _load(tmp_path, _MONTHLY_EXPIRY, entry=entry, exit_=exit_)
        assert pair is None
        events = [log for log in logs if log["event"] == "options_sim.ambiguous_option_rows"]
        assert len(events) == 1
        assert events[0]["n_rows"] == 2

        out = materialize_trade(
            _signal(_MONTHLY_EXPIRY, entry=entry, exit_=exit_),
            RULES_BY_NAME["atm"],
            source=_LakeOptionBarSource(lake_root=tmp_path),
        )
        assert isinstance(out, RejectedTrade)
        assert out.reason == RejectReason.NO_DATA

    def test_unresolvable_expiry_is_none_no_data(self, two_series_lake: Path) -> None:
        # Jan-7 2025 (Tuesday) is not a NIFTY expiry — the reader logs the
        # unresolved series and rejects honestly instead of guessing a series.
        from structlog.testing import capture_logs

        entry = datetime(2025, 1, 6, 9, 16, tzinfo=_IST)
        exit_ = datetime(2025, 1, 6, 15, 20, tzinfo=_IST)
        with capture_logs() as logs:
            pair = _load(two_series_lake, date(2025, 1, 7), entry=entry, exit_=exit_)
        assert pair is None
        events = [log for log in logs if log["event"] == "options_sim.expiry_series_unresolved"]
        assert len(events) == 1
        assert events[0]["instrument"] == "NIFTY"

        out = materialize_trade(
            _signal(date(2025, 1, 7), entry=entry, exit_=exit_),
            RULES_BY_NAME["atm"],
            source=_LakeOptionBarSource(lake_root=two_series_lake),
        )
        assert isinstance(out, RejectedTrade)
        assert out.reason == RejectReason.NO_DATA

    def test_exit_past_expiry_week_is_unresolved_none(self, two_series_lake: Path) -> None:
        # Entry resolves (Jan-30 is the near weekly + monthly on Jan-30) but
        # the exit date is PAST the expiry — the exit lookup has no series.
        from structlog.testing import capture_logs

        entry = datetime(2025, 1, 30, 9, 16, tzinfo=_IST)
        exit_ = datetime(2025, 2, 3, 15, 20, tzinfo=_IST)
        with capture_logs() as logs:
            pair = _load(two_series_lake, _MONTHLY_EXPIRY, entry=entry, exit_=exit_)
        assert pair is None
        assert any(log["event"] == "options_sim.expiry_series_unresolved" for log in logs)

    def test_empty_lake_is_none(self, tmp_path: Path) -> None:
        assert _load(tmp_path, _WEEKLY_EXPIRY) is None

    def test_no_partition_for_requested_months_is_none(self, two_series_lake: Path) -> None:
        # The lake holds only 2025-01 partitions; a March lookup finds no files.
        entry = datetime(2025, 3, 5, 9, 16, tzinfo=_IST)
        exit_ = datetime(2025, 3, 5, 15, 20, tzinfo=_IST)
        assert _load(two_series_lake, date(2025, 3, 6), entry=entry, exit_=exit_) is None

    def test_no_bar_at_requested_ts_is_none(self, two_series_lake: Path) -> None:
        # The entry bar exists but the requested exit minute was never stored.
        exit_ = datetime(2025, 1, 2, 15, 19, tzinfo=_IST)
        assert _load(two_series_lake, _WEEKLY_EXPIRY, exit_=exit_) is None

    def test_absent_strike_is_none(self, two_series_lake: Path) -> None:
        assert _load(two_series_lake, _WEEKLY_EXPIRY, strike=25000) is None

    def test_cross_month_pair_concats_partitions(self, tmp_path: Path) -> None:
        # One series spanning a month boundary (entry Jan-31, exit Feb-3, both
        # inside the Feb-6 weekly) → two partition files → the concat path.
        entry = datetime(2025, 1, 31, 9, 16, tzinfo=_IST)
        exit_ = datetime(2025, 2, 3, 15, 20, tzinfo=_IST)
        _write_series(
            tmp_path, _EXPIRY_FLAG_WEEKLY, 0, [_bar(entry, "120.00"), _bar(exit_, "130.00")]
        )
        pair = _load(tmp_path, date(2025, 2, 6), entry=entry, exit_=exit_)
        assert pair is not None
        assert pair.entry_bar.open == Decimal("120.00")  # type: ignore[attr-defined]
        assert pair.exit_bar.open == Decimal("130.00")  # type: ignore[attr-defined]

    def test_row_at_normalizes_naive_timestamps(self) -> None:
        # A tz-naive stored timestamp is stamped UTC on the way out (the
        # defensive ternary in the row build).
        naive_ts = datetime(2025, 1, 2, 3, 46)  # naive == already-UTC wall time
        df = pl.DataFrame(
            {
                "expiry_flag": [_EXPIRY_FLAG_WEEKLY],
                "expiry_code": [0],
                "timestamp": [naive_ts],
                "open": [150.0],
                "high": [150.0],
                "low": [150.0],
                "close": [150.0],
                "volume": [10],
                "open_interest": [1000],
                "strike": [24000.0],
                "spot": [None],
                "iv": [None],
            }
        ).with_columns(
            pl.col("open_interest").cast(pl.Int64),
            pl.col("spot").cast(pl.Float64),
            pl.col("iv").cast(pl.Float64),
        )
        bar = _LakeOptionBarSource._row_at(df, naive_ts, "CE", series=((_EXPIRY_FLAG_WEEKLY, 0),))
        assert bar is not None
        assert bar.timestamp == naive_ts.replace(tzinfo=UTC)
