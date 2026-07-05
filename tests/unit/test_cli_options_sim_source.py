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


def _seam_frame(ts: datetime, *, opens: list[float]) -> pl.DataFrame:
    """A hand-rolled expired-options frame at the ``_row_at`` seam.

    One row per entry of *opens*, every row at *ts* under the SAME series key
    ``(WEEK, 0)`` — the shape the production writer can never emit (it dedups
    the composite key on write), used to pin the read-side duplicate gate.
    """
    n = len(opens)
    return pl.DataFrame(
        {
            "expiry_flag": [_EXPIRY_FLAG_WEEKLY] * n,
            "expiry_code": [0] * n,
            "timestamp": [ts] * n,
            "open": opens,
            "high": opens,
            "low": opens,
            "close": opens,
            "volume": [10] * n,
            "open_interest": [1000] * n,
            "strike": [24000.0] * n,
            "spot": [None] * n,
            "iv": [None] * n,
        }
    ).with_columns(
        pl.col("open_interest").cast(pl.Int64),
        pl.col("spot").cast(pl.Float64),
        pl.col("iv").cast(pl.Float64),
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
        # The expiry calendar's recorded history starts 2020-01-01
        # (DEC-EXPIRY-REGIME-2020-001, MIGRATION.md #1178(c) — extended back
        # from the 2021-01-01 floor this test originally probed); a
        # pre-2020 signal must resolve to () (honest reject), never raise
        # or guess.
        got = _expiry_series_candidates(
            OptionsUnderlying.NIFTY, date(2019, 6, 6), on_date=date(2019, 6, 3)
        )
        assert got == ()

    def test_2020_date_resolves_under_extended_calendar_floor(self) -> None:
        # DEC-EXPIRY-REGIME-2020-001 (MIGRATION.md #1178(c)) extended the
        # expiry-regime tables to the 2020-01-01 pull floor, so a 2020
        # signal now RESOLVES (2020-06-04 is a Thursday — the near weekly
        # as seen from 2020-06-01) instead of the old pre-history reject.
        got = _expiry_series_candidates(
            OptionsUnderlying.NIFTY, date(2020, 6, 4), on_date=date(2020, 6, 1)
        )
        assert got == ((_EXPIRY_FLAG_WEEKLY, 0),)


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
        df = _seam_frame(naive_ts, opens=[150.0])
        bar = _LakeOptionBarSource._row_at(df, naive_ts, "CE", series=((_EXPIRY_FLAG_WEEKLY, 0),))
        assert bar is not None
        assert bar.timestamp == naive_ts.replace(tzinfo=UTC)

    def test_same_series_physical_duplicate_keeps_first_and_warns(self) -> None:
        # A poisoned-era physical duplicate of ONE series key with IDENTICAL
        # values keeps the FIRST physical row and is WARNED via the COMMON
        # read gate — never silent (DEC-LAKE-READ-UNIQUE-001 composed with
        # the R2-OC-1 series filter). The production writer dedups the
        # composite key on write, so the fixture is hand-rolled at the
        # `_row_at` seam.
        from brutex.data.lake_read import DUPLICATES_DROPPED_EVENT
        from structlog.testing import capture_logs

        naive_ts = datetime(2025, 1, 2, 3, 46)
        df = _seam_frame(naive_ts, opens=[150.0, 150.0])
        with capture_logs() as logs:
            bar = _LakeOptionBarSource._row_at(
                df, naive_ts, "CE", series=((_EXPIRY_FLAG_WEEKLY, 0),)
            )
        assert bar is not None
        assert bar.open == Decimal("150.0")
        events = [log for log in logs if log["event"] == DUPLICATES_DROPPED_EVENT]
        assert len(events) == 1
        assert events[0]["dropped"] == 1
        assert events[0]["conflict_count"] == 0

    def test_same_series_conflicting_duplicate_is_ambiguous_none(self) -> None:
        # A poisoned duplicate whose values DISAGREE takes the ambiguity
        # reject (None → NO_DATA) — never keep-first pricing from an
        # arbitrary row(0) (R2-OC-1 precedence over the read gate's
        # keep-first: a disagreeing survivor is untrustworthy either way).
        from structlog.testing import capture_logs

        naive_ts = datetime(2025, 1, 2, 3, 46)
        df = _seam_frame(naive_ts, opens=[150.0, 999.0])
        with capture_logs() as logs:
            bar = _LakeOptionBarSource._row_at(
                df, naive_ts, "CE", series=((_EXPIRY_FLAG_WEEKLY, 0),)
            )
        assert bar is None
        assert any(log["event"] == "options_sim.ambiguous_option_rows" for log in logs)


# ────────────────────────────────────────────────────────────────────────
# Hostile-review MINOR (DEC-OPTIONS-EXEC-COST-AUDIT-R2-001): the ambiguity
# key uses PRICING columns only — nullable annotation drift never rejects
# ────────────────────────────────────────────────────────────────────────


class TestAliasedAnnotationDrift:
    """Aliased series that AGREE on pricing but drift on spot/iv annotations.

    Pre-fix, ``_OPTION_VALUE_COLUMNS`` folded the nullable ``spot``/``iv``
    annotation columns (+ the constant ``strike``) into the ambiguity key, so
    a monthly-that-is-also-weekly pair with identical OHLCV but per-pull
    annotation drift (one series captured spot, the other stored None) was
    over-rejected as ambiguous → NO_DATA, silently dropping a priceable trade.
    """

    # Jan-30 2025 is BOTH the near weekly (from Jan-27) and the monthly.
    _ALIAS_ENTRY = datetime(2025, 1, 27, 9, 16, tzinfo=_IST)
    _ALIAS_EXIT = datetime(2025, 1, 27, 15, 20, tzinfo=_IST)

    def test_regression_annotation_drift_prices_normally(self, tmp_path: Path) -> None:
        """THE fix regression — FAILS on the pre-fix annotation-keyed reader.

        Identical OHLCV under both aliased series; the monthly rows carry
        spot/iv, the weekly rows None. The pair must price normally (same
        physical contract), silently — no ambiguity WARN, no duplicates WARN
        (two DISTINCT series keys → dropped == 0).
        """
        from brutex.data.lake_read import DUPLICATES_DROPPED_EVENT
        from structlog.testing import capture_logs

        _write_series(
            tmp_path,
            _EXPIRY_FLAG_WEEKLY,
            0,
            [_bar(self._ALIAS_ENTRY, "180.00"), _bar(self._ALIAS_EXIT, "190.00")],
        )
        _write_series(
            tmp_path,
            _EXPIRY_FLAG_MONTHLY,
            0,
            [
                _bar(
                    self._ALIAS_ENTRY,
                    "180.00",
                    spot=Decimal("24010.55"),
                    iv=Decimal("0.1523"),
                ),
                _bar(self._ALIAS_EXIT, "190.00", spot=Decimal("24020.10")),
            ],
        )
        with capture_logs() as logs:
            pair = _load(tmp_path, _MONTHLY_EXPIRY, entry=self._ALIAS_ENTRY, exit_=self._ALIAS_EXIT)
        assert pair is not None
        assert pair.entry_bar.open == Decimal("180.00")  # type: ignore[attr-defined]
        assert pair.exit_bar.open == Decimal("190.00")  # type: ignore[attr-defined]
        # The aliased-agreeing pair prices SILENTLY (no ambiguity, no dedup WARN).
        assert not any(log["event"] == "options_sim.ambiguous_option_rows" for log in logs)
        assert not any(log["event"] == DUPLICATES_DROPPED_EVENT for log in logs)

    def test_most_annotated_agreeing_survivor_is_kept(self, tmp_path: Path) -> None:
        # Among price-agreeing survivors the reader keeps the MOST-annotated
        # row (non-None spot/iv), deterministically — never an arbitrary
        # glob-order row(0) that could drop the annotations.
        _write_series(
            tmp_path,
            _EXPIRY_FLAG_WEEKLY,
            0,
            [_bar(self._ALIAS_ENTRY, "180.00"), _bar(self._ALIAS_EXIT, "190.00")],
        )
        _write_series(
            tmp_path,
            _EXPIRY_FLAG_MONTHLY,
            0,
            [
                _bar(
                    self._ALIAS_ENTRY,
                    "180.00",
                    spot=Decimal("24010.55"),
                    iv=Decimal("0.1523"),
                ),
                _bar(self._ALIAS_EXIT, "190.00"),
            ],
        )
        pair = _load(tmp_path, _MONTHLY_EXPIRY, entry=self._ALIAS_ENTRY, exit_=self._ALIAS_EXIT)
        assert pair is not None
        assert pair.entry_bar.spot == Decimal("24010.55")  # type: ignore[attr-defined]
        assert pair.entry_bar.iv == Decimal("0.1523")  # type: ignore[attr-defined]

    def test_end_to_end_annotation_drift_materializes(self, tmp_path: Path) -> None:
        # Through the REAL executor: the annotation-drifted aliased pair is a
        # MaterializedTrade (pre-fix: RejectedTrade NO_DATA).
        _write_series(
            tmp_path,
            _EXPIRY_FLAG_WEEKLY,
            0,
            [_bar(self._ALIAS_ENTRY, "180.00"), _bar(self._ALIAS_EXIT, "190.00")],
        )
        _write_series(
            tmp_path,
            _EXPIRY_FLAG_MONTHLY,
            0,
            [
                _bar(self._ALIAS_ENTRY, "180.00", spot=Decimal("24010.55")),
                _bar(self._ALIAS_EXIT, "190.00"),
            ],
        )
        out = materialize_trade(
            _signal(_MONTHLY_EXPIRY, entry=self._ALIAS_ENTRY, exit_=self._ALIAS_EXIT),
            RULES_BY_NAME["atm"],
            source=_LakeOptionBarSource(lake_root=tmp_path),
        )
        assert isinstance(out, MaterializedTrade)
        assert out.entry_open_paisa == 18000

    def test_close_only_disagreement_is_still_ambiguous_none(self) -> None:
        # The narrowed key still has TEETH: two aliased rows agreeing on
        # open/high/low/volume (and every annotation) but differing on CLOSE
        # alone are a genuine pricing ambiguity → honest None + the WARN.
        from structlog.testing import capture_logs

        naive_ts = datetime(2025, 1, 27, 3, 46)  # naive == already-UTC wall time

        def _row(flag: str, close: float) -> pl.DataFrame:
            return pl.DataFrame(
                {
                    "expiry_flag": [flag],
                    "expiry_code": [0],
                    "timestamp": [naive_ts],
                    "open": [180.0],
                    "high": [182.0],
                    "low": [178.0],
                    "close": [close],
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

        df = pl.concat([_row(_EXPIRY_FLAG_WEEKLY, 180.0), _row(_EXPIRY_FLAG_MONTHLY, 181.0)])
        with capture_logs() as logs:
            bar = _LakeOptionBarSource._row_at(
                df,
                naive_ts,
                "CE",
                series=((_EXPIRY_FLAG_WEEKLY, 0), (_EXPIRY_FLAG_MONTHLY, 0)),
            )
        assert bar is None
        events = [log for log in logs if log["event"] == "options_sim.ambiguous_option_rows"]
        assert len(events) == 1
        assert events[0]["n_rows"] == 2

    def test_same_series_agreeing_duplicate_with_annotation_drift_still_warns(self) -> None:
        # Composition guard: a POISONED same-series physical duplicate that
        # agrees on pricing (annotations aside) still takes the keep-first +
        # ONE aggregated WARN path (DEC-LAKE-READ-UNIQUE-001) — the narrowed
        # ambiguity key must not silence the duplicate accounting.
        from brutex.data.lake_read import DUPLICATES_DROPPED_EVENT
        from structlog.testing import capture_logs

        naive_ts = datetime(2025, 1, 2, 3, 46)
        df = _seam_frame(naive_ts, opens=[150.0, 150.0]).with_columns(
            pl.Series("spot", [24010.55, None], dtype=pl.Float64)
        )
        with capture_logs() as logs:
            bar = _LakeOptionBarSource._row_at(
                df, naive_ts, "CE", series=((_EXPIRY_FLAG_WEEKLY, 0),)
            )
        assert bar is not None
        assert bar.open == Decimal("150.0")
        assert bar.spot == Decimal("24010.55")  # most-annotated survivor kept
        events = [log for log in logs if log["event"] == DUPLICATES_DROPPED_EVENT]
        assert len(events) == 1
        assert events[0]["dropped"] == 1


# ────────────────────────────────────────────────────────────────────────
# Rolling multi-day regression (hostile-review NIT): the SAME absolute
# expiry resolves to DIFFERENT series codes at entry vs exit
# ────────────────────────────────────────────────────────────────────────


class TestRollingSeriesResolution:
    """Entry WEEK/1 → exit WEEK/0 for ONE absolute expiry, with decoys.

    The strongest expression of the R2-OC-1 fix: the rolling lake stores a
    bar under the Near/Next/Far rank the contract held AS OF that bar's
    trading date, so a multi-day trade on the Jan-9 weekly enters while it is
    the NEXT weekly (WEEK/1, seen from Thu Jan-2) and exits after it has
    ROLLED to the near weekly (WEEK/0, seen from Mon Jan-6). Entry and exit
    must be resolved INDEPENDENTLY — a refactor collapsing entry_series /
    exit_series to one shared resolution silently regresses this.
    """

    _EXPIRY = date(2025, 1, 9)  # Thursday — the Jan-9 NIFTY weekly
    _ROLL_ENTRY = datetime(2025, 1, 2, 9, 16, tzinfo=_IST)  # Jan-9 is WEEK/1 here
    _ROLL_EXIT = datetime(2025, 1, 6, 15, 20, tzinfo=_IST)  # Jan-9 is WEEK/0 here

    @pytest.fixture
    def rolling_lake(self, tmp_path: Path) -> Path:
        # The Jan-9 contract's REAL bars, stored under the rank current at
        # each bar's own date (rolling accretion):
        _write_series(tmp_path, _EXPIRY_FLAG_WEEKLY, 1, [_bar(self._ROLL_ENTRY, "250.00")])
        _write_series(tmp_path, _EXPIRY_FLAG_WEEKLY, 0, [_bar(self._ROLL_EXIT, "275.00")])
        # Same-strike DECOYS present at BOTH lookup timestamps:
        # (a) the Jan-2 weekly's own bar at the entry ts under WEEK/0;
        # (b) the Jan-30 monthly's bar at the exit ts under MONTH/0.
        _write_series(tmp_path, _EXPIRY_FLAG_WEEKLY, 0, [_bar(self._ROLL_ENTRY, "90.00")])
        _write_series(tmp_path, _EXPIRY_FLAG_MONTHLY, 0, [_bar(self._ROLL_EXIT, "500.00")])
        return tmp_path

    def test_series_codes_differ_at_entry_and_exit(self) -> None:
        # The premise, pinned: ONE absolute expiry, two different codes.
        assert _expiry_series_candidates(
            OptionsUnderlying.NIFTY, self._EXPIRY, on_date=self._ROLL_ENTRY.date()
        ) == ((_EXPIRY_FLAG_WEEKLY, 1),)
        assert _expiry_series_candidates(
            OptionsUnderlying.NIFTY, self._EXPIRY, on_date=self._ROLL_EXIT.date()
        ) == ((_EXPIRY_FLAG_WEEKLY, 0),)

    def test_rolling_pair_prices_each_leg_from_its_own_code(self, rolling_lake: Path) -> None:
        pair = _load(rolling_lake, self._EXPIRY, entry=self._ROLL_ENTRY, exit_=self._ROLL_EXIT)
        assert pair is not None
        assert pair.entry_bar.open == Decimal("250.00")  # type: ignore[attr-defined]
        assert pair.exit_bar.open == Decimal("275.00")  # type: ignore[attr-defined]

    def test_rolling_pair_end_to_end_through_the_executor(self, rolling_lake: Path) -> None:
        out = materialize_trade(
            _signal(self._EXPIRY, entry=self._ROLL_ENTRY, exit_=self._ROLL_EXIT),
            RULES_BY_NAME["atm"],
            source=_LakeOptionBarSource(lake_root=rolling_lake),
        )
        assert isinstance(out, MaterializedTrade)
        assert out.entry_open_paisa == 25000  # ₹250 — WEEK/1, never the 90.00 decoy
        assert out.exit_open_paisa == 27500  # ₹275 — WEEK/0, never the 500.00 decoy
