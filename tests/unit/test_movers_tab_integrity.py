"""Tab-integrity tests for the ``/movers`` screen — strict spot/futures/options
separation + the bounded 1-minute prior-session fallback.

Covers the two correctness fixes (operator-reported 2026-06-23):

* **Spot is SPOT only** — :func:`~brutex.dashboard.movers_snapshot.compute_spot_candidates`
  must whitelist the cash/index segments (``INDEX``/``CASH``) so an ``FNO``
  contract partition (a future / option) NEVER leaks into the Spot tab.
* **Prior-day baseline from 1-minute** —
  :func:`~brutex.dashboard.movers_snapshot._prior_session_last_from_minute` recovers
  the prior trading day's last close + OI from the stored 1-minute bars (bounded
  back-walk) when the derived ``1day`` partition is absent, so the futures/options
  Price-Gainers/Losers + OI-Gainers/Losers compute on a lake whose 1-day derive
  never ran. The derived 1-day close == the session's last 1-minute close, so the
  recovered value is byte-equal to the 1-day value it substitutes.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from brutex.dashboard.movers_futures import _prev_day_oi
from brutex.dashboard.movers_snapshot import (
    _NONCONTIGUOUS_WARN_CAP,
    _PREV_SESSION_MAX_BACK_DAYS,
    PrevDayBaseline,
    _calendar_exchange,
    _NoncontiguousWarnState,
    _prev_day_close,
    _prior_session_last_from_minute,
    compute_spot_candidates,
    prev_baseline_provenance,
    resolve_prev_close,
    warn_prev_baseline_noncontiguous,
)
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from structlog.testing import capture_logs

_IST = ZoneInfo("Asia/Kolkata")


def _min_bar(
    day: date, *, hh: int, mm: int, close: str, oi: int | None = None, volume: int = 10
) -> CandleBar:
    """A 1-minute bar at IST ``hh:mm`` on *day*, stored as a UTC instant."""
    c = Decimal(close)
    ist_dt = datetime(day.year, day.month, day.day, hh, mm, tzinfo=_IST)
    return CandleBar(
        timestamp=ist_dt.astimezone(UTC),
        open=c,
        high=c,
        low=c,
        close=c,
        volume=volume,
        open_interest=oi,
    )


def _day_bar(day: date, *, close: str, oi: int | None = None) -> CandleBar:
    """A 1-day bar labelled 09:15 IST on *day* (the derived-1day shape)."""
    c = Decimal(close)
    ist_dt = datetime(day.year, day.month, day.day, 9, 15, tzinfo=_IST)
    return CandleBar(
        timestamp=ist_dt.astimezone(UTC),
        open=c,
        high=c,
        low=c,
        close=c,
        volume=1,
        open_interest=oi,
    )


def _write(
    tmp_path: Path, *, segment: str, symbol: str, interval: str, bars: list[CandleBar]
) -> None:
    ParquetWriter(lake_root=tmp_path).write(
        exchange="NSE", segment=segment, symbol=symbol, interval=interval, bars=bars
    )


_FUT = "NSE-NIFTY-30Sep25-FUT"


# ── _prior_session_last_from_minute ────────────────────────────────────────


class TestPriorSessionFromMinute:
    def test_no_minute_dir_is_none(self, tmp_path: Path) -> None:
        assert (
            _prior_session_last_from_minute(
                tmp_path, exchange="NSE", segment="FNO", symbol=_FUT, day=date(2025, 9, 1)
            )
            is None
        )

    def test_returns_prior_day_last_close_and_oi(self, tmp_path: Path) -> None:
        # 2025-08-29 (Fri) has two bars; the LAST (15:29 IST) carries the close+OI.
        _write(
            tmp_path,
            segment="FNO",
            symbol=_FUT,
            interval="1minute",
            bars=[
                _min_bar(date(2025, 8, 29), hh=9, mm=15, close="100.00", oi=500),
                _min_bar(date(2025, 8, 29), hh=15, mm=29, close="101.50", oi=1234),
            ],
        )
        got = _prior_session_last_from_minute(
            tmp_path, exchange="NSE", segment="FNO", symbol=_FUT, day=date(2025, 9, 1)
        )
        # 3-tuple since DEC-MOVERS-PREVCLOSE-CONTIGUITY-001: the found IST date is
        # APPENDED (index-append — [0]/[1] keep their historical meaning).
        assert got == (Decimal("101.50"), 1234, date(2025, 8, 29))

    def test_skips_empty_days_back_to_prior_trading_day(self, tmp_path: Path) -> None:
        # day=Mon 2025-09-01 → walks back over Sun 08-31 + Sat 08-30 (no data) to Fri 08-29.
        _write(
            tmp_path,
            segment="FNO",
            symbol=_FUT,
            interval="1minute",
            bars=[_min_bar(date(2025, 8, 29), hh=15, mm=29, close="99.95", oi=7)],
        )
        got = _prior_session_last_from_minute(
            tmp_path, exchange="NSE", segment="FNO", symbol=_FUT, day=date(2025, 9, 1)
        )
        # The appended date is the FOUND prior day (Fri 08-29), not day-1.
        assert got == (Decimal("99.95"), 7, date(2025, 8, 29))

    def test_null_oi_returns_close_with_none_oi(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            segment="FNO",
            symbol=_FUT,
            interval="1minute",
            bars=[_min_bar(date(2025, 8, 29), hh=15, mm=29, close="55.25", oi=None)],
        )
        got = _prior_session_last_from_minute(
            tmp_path, exchange="NSE", segment="FNO", symbol=_FUT, day=date(2025, 9, 1)
        )
        assert got == (Decimal("55.25"), None, date(2025, 8, 29))

    def test_beyond_back_bound_is_none(self, tmp_path: Path) -> None:
        # Only data far past the bound → not found within _PREV_SESSION_MAX_BACK_DAYS.
        far = date(2025, 9, 1) - timedelta(days=_PREV_SESSION_MAX_BACK_DAYS + 3)
        _write(
            tmp_path,
            segment="FNO",
            symbol=_FUT,
            interval="1minute",
            bars=[_min_bar(far, hh=15, mm=29, close="10.00", oi=1)],
        )
        assert (
            _prior_session_last_from_minute(
                tmp_path, exchange="NSE", segment="FNO", symbol=_FUT, day=date(2025, 9, 1)
            )
            is None
        )

    def test_only_same_day_data_is_none(self, tmp_path: Path) -> None:
        # A bar ON `day` is not a PRIOR session → None.
        _write(
            tmp_path,
            segment="FNO",
            symbol=_FUT,
            interval="1minute",
            bars=[_min_bar(date(2025, 9, 1), hh=15, mm=29, close="42.00", oi=3)],
        )
        assert (
            _prior_session_last_from_minute(
                tmp_path, exchange="NSE", segment="FNO", symbol=_FUT, day=date(2025, 9, 1)
            )
            is None
        )


# ── _prev_day_close: 1day primary, 1-minute fallback ───────────────────────


class TestPrevDayCloseFallback:
    def test_uses_1day_when_present(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            segment="INDEX",
            symbol="NIFTY",
            interval="1day",
            bars=[_day_bar(date(2025, 8, 29), close="24000.00", oi=None)],
        )
        # A DIFFERENT 1-minute value must NOT be consulted while 1day has a prior bar.
        _write(
            tmp_path,
            segment="INDEX",
            symbol="NIFTY",
            interval="1minute",
            bars=[_min_bar(date(2025, 8, 29), hh=15, mm=29, close="99999.00")],
        )
        got = _prev_day_close(
            tmp_path, exchange="NSE", segment="INDEX", symbol="NIFTY", day=date(2025, 9, 1)
        )
        assert got == Decimal("24000.00")

    def test_falls_back_to_minute_when_no_1day(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            segment="FNO",
            symbol=_FUT,
            interval="1minute",
            bars=[_min_bar(date(2025, 8, 29), hh=15, mm=29, close="250.75", oi=11)],
        )
        got = _prev_day_close(
            tmp_path, exchange="NSE", segment="FNO", symbol=_FUT, day=date(2025, 9, 1)
        )
        assert got == Decimal("250.75")

    def test_falls_back_when_1day_present_but_no_prior_bar(self, tmp_path: Path) -> None:
        # 1day exists only for the SAME day (no prior) → fall back to 1-minute prior.
        _write(
            tmp_path,
            segment="FNO",
            symbol=_FUT,
            interval="1day",
            bars=[_day_bar(date(2025, 9, 1), close="300.00", oi=None)],
        )
        _write(
            tmp_path,
            segment="FNO",
            symbol=_FUT,
            interval="1minute",
            bars=[_min_bar(date(2025, 8, 29), hh=15, mm=29, close="298.40", oi=5)],
        )
        got = _prev_day_close(
            tmp_path, exchange="NSE", segment="FNO", symbol=_FUT, day=date(2025, 9, 1)
        )
        assert got == Decimal("298.40")

    def test_none_when_neither_source(self, tmp_path: Path) -> None:
        assert (
            _prev_day_close(
                tmp_path, exchange="NSE", segment="FNO", symbol=_FUT, day=date(2025, 9, 1)
            )
            is None
        )


# ── futures _prev_day_oi: 1-minute fallback ────────────────────────────────


class TestPrevDayOiFallback:
    def test_falls_back_to_minute_oi_when_no_1day(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            segment="FNO",
            symbol=_FUT,
            interval="1minute",
            bars=[_min_bar(date(2025, 8, 29), hh=15, mm=29, close="100.00", oi=4321)],
        )
        got = _prev_day_oi(
            tmp_path, exchange="NSE", segment="FNO", symbol=_FUT, day=date(2025, 9, 1)
        )
        assert got == 4321

    def test_1day_oi_takes_precedence(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            segment="FNO",
            symbol=_FUT,
            interval="1day",
            bars=[_day_bar(date(2025, 8, 29), close="100.00", oi=999)],
        )
        _write(
            tmp_path,
            segment="FNO",
            symbol=_FUT,
            interval="1minute",
            bars=[_min_bar(date(2025, 8, 29), hh=15, mm=29, close="100.00", oi=4321)],
        )
        got = _prev_day_oi(
            tmp_path, exchange="NSE", segment="FNO", symbol=_FUT, day=date(2025, 9, 1)
        )
        assert got == 999

    def test_1day_present_but_null_oi_falls_back_to_minute(self, tmp_path: Path) -> None:
        # The universal real-world F&O case: a derived 1-day bar EXISTS (with a real
        # close) but its open_interest is null (the derive writes OI=None), so the
        # OI must be recovered from the prior session's last 1-minute bar.
        _write(
            tmp_path,
            segment="FNO",
            symbol=_FUT,
            interval="1day",
            bars=[_day_bar(date(2025, 8, 29), close="100.00", oi=None)],
        )
        _write(
            tmp_path,
            segment="FNO",
            symbol=_FUT,
            interval="1minute",
            bars=[_min_bar(date(2025, 8, 29), hh=15, mm=29, close="100.00", oi=8765)],
        )
        got = _prev_day_oi(
            tmp_path, exchange="NSE", segment="FNO", symbol=_FUT, day=date(2025, 9, 1)
        )
        assert got == 8765


# ── compute_spot_candidates: SPOT-only segment whitelist ───────────────────


class TestSpotSegmentWhitelist:
    def test_fno_contract_excluded_from_spot(self, tmp_path: Path) -> None:
        snap_day = date(2025, 9, 1)
        # A real spot index (INDEX) + an F&O future (FNO) — both with 1-minute on the day.
        _write(
            tmp_path,
            segment="INDEX",
            symbol="NIFTY",
            interval="1minute",
            bars=[_min_bar(snap_day, hh=9, mm=15, close="24000.00", volume=100)],
        )
        _write(
            tmp_path,
            segment="FNO",
            symbol=_FUT,
            interval="1minute",
            bars=[_min_bar(snap_day, hh=9, mm=15, close="24050.00", volume=100)],
        )
        candidates = compute_spot_candidates(
            lake_root=tmp_path, interval="1minute", date=snap_day.isoformat(), time=None
        )
        symbols = {s.instrument.symbol for s in candidates.snapshots}
        assert "NIFTY" in symbols
        assert _FUT not in symbols  # the future must NOT pollute the Spot tab
        assert all(s.instrument.segment in {"INDEX", "CASH"} for s in candidates.snapshots)


# ── resolve_prev_close: the enriched baseline (value + found session date) ──


class TestResolvePrevClose:
    def test_1day_path_carries_the_prior_bars_ist_date(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            segment="INDEX",
            symbol="NIFTY",
            interval="1day",
            bars=[_day_bar(date(2025, 8, 29), close="24000.00", oi=None)],
        )
        got = resolve_prev_close(
            tmp_path, exchange="NSE", segment="INDEX", symbol="NIFTY", day=date(2025, 9, 1)
        )
        assert got == PrevDayBaseline(value=Decimal("24000.00"), found_date=date(2025, 8, 29))

    def test_minute_fallback_carries_the_recovered_date(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            segment="FNO",
            symbol=_FUT,
            interval="1minute",
            bars=[_min_bar(date(2025, 8, 29), hh=15, mm=29, close="250.75", oi=11)],
        )
        got = resolve_prev_close(
            tmp_path, exchange="NSE", segment="FNO", symbol=_FUT, day=date(2025, 9, 1)
        )
        assert got == PrevDayBaseline(value=Decimal("250.75"), found_date=date(2025, 8, 29))

    def test_neither_source_is_none_none(self, tmp_path: Path) -> None:
        got = resolve_prev_close(
            tmp_path, exchange="NSE", segment="FNO", symbol=_FUT, day=date(2025, 9, 1)
        )
        assert got == PrevDayBaseline(value=None, found_date=None)

    def test_wrapper_value_seam_is_byte_identical(self, tmp_path: Path) -> None:
        """_prev_day_close(...) == resolve_prev_close(...).value on every branch
        (the DEC-MOVERS-PREVCLOSE-CONTIGUITY-001 value-seam law)."""
        # Branch 1: no data at all.
        assert _prev_day_close(
            tmp_path, exchange="NSE", segment="FNO", symbol=_FUT, day=date(2025, 9, 1)
        ) == (
            resolve_prev_close(
                tmp_path, exchange="NSE", segment="FNO", symbol=_FUT, day=date(2025, 9, 1)
            ).value
        )
        # Branch 2: minute fallback only.
        _write(
            tmp_path,
            segment="FNO",
            symbol=_FUT,
            interval="1minute",
            bars=[_min_bar(date(2025, 8, 29), hh=15, mm=29, close="250.75", oi=11)],
        )
        assert _prev_day_close(
            tmp_path, exchange="NSE", segment="FNO", symbol=_FUT, day=date(2025, 9, 1)
        ) == Decimal("250.75")
        # Branch 3: 1day primary wins.
        _write(
            tmp_path,
            segment="FNO",
            symbol=_FUT,
            interval="1day",
            bars=[_day_bar(date(2025, 8, 29), close="251.00", oi=None)],
        )
        assert _prev_day_close(
            tmp_path, exchange="NSE", segment="FNO", symbol=_FUT, day=date(2025, 9, 1)
        ) == Decimal("251.00")


# ── prev_baseline_provenance: the four verdicts ─────────────────────────────


class TestPrevBaselineProvenance:
    def test_no_baseline_is_none_none(self) -> None:
        assert prev_baseline_provenance(None, date(2025, 9, 29), exchange="NSE") == (None, None)

    def test_contiguous_true_across_a_weekend(self) -> None:
        # Snapshot Mon 2025-09-29; found Fri 2025-09-26 = the immediately-preceding
        # NSE session → True.
        got = prev_baseline_provenance(date(2025, 9, 26), date(2025, 9, 29), exchange="NSE")
        assert got == ("2025-09-26", True)

    def test_noncontiguous_false_on_a_lake_hole(self) -> None:
        # The confirmed-defect repro geometry: found Thu 2025-09-25 while the
        # calendar expects Fri 2025-09-26 → strictly False.
        got = prev_baseline_provenance(date(2025, 9, 25), date(2025, 9, 29), exchange="NSE")
        assert got == ("2025-09-25", False)

    def test_out_of_coverage_abstains_to_null_verdict(self) -> None:
        # A pre-2020 snapshot day: previous_trading_session abstains (None) →
        # the session is still reported, contiguity honestly UNKNOWN (None) —
        # the value keeps being served, no False, no marker.
        got = prev_baseline_provenance(date(2019, 5, 31), date(2019, 6, 3), exchange=None)
        assert got == ("2019-05-31", None)


class TestCalendarExchange:
    def test_known_exchanges_pass_through(self) -> None:
        assert _calendar_exchange("NSE") == "NSE"
        assert _calendar_exchange("BSE") == "BSE"

    def test_unknown_lake_exchange_abstains_to_none(self) -> None:
        # A nonstandard lake exchange dir must NEVER 500 the read-only screen
        # via the calendar's eager ValueError — it abstains to the union.
        assert _calendar_exchange("MCX") is None
        assert _calendar_exchange("") is None


# ── warn_prev_baseline_noncontiguous: cap + dedupe + reset seam ─────────────


def _warn(state: _NoncontiguousWarnState, *, symbol: str, kind: str = "prev_close") -> None:
    warn_prev_baseline_noncontiguous(
        state,
        kind=kind,
        exchange="NSE",
        segment="INDEX",
        symbol=symbol,
        snapshot_day=date(2025, 9, 29),
        found_session="2025-09-25",
        expected_session=date(2025, 9, 26),
    )


class TestNoncontiguousWarnCap:
    def test_cap_emits_exactly_20_plus_one_suppressed_summary(self) -> None:
        state = _NoncontiguousWarnState()
        with capture_logs() as logs:
            for i in range(_NONCONTIGUOUS_WARN_CAP + 5):  # 25 distinct symbols
                _warn(state, symbol=f"SYM{i:03d}")
        emitted = [e for e in logs if e["event"] == "movers.prev_baseline_noncontiguous"]
        suppressed = [
            e for e in logs if e["event"] == "movers.prev_baseline_noncontiguous_suppressed"
        ]
        assert len(emitted) == _NONCONTIGUOUS_WARN_CAP == 20
        assert len(suppressed) == 1  # the summary fires ONCE, not per drop
        assert suppressed[0]["cap"] == _NONCONTIGUOUS_WARN_CAP
        assert state.emitted == 20
        assert state.suppressed == 5

    def test_deduped_per_symbol_kind(self) -> None:
        state = _NoncontiguousWarnState()
        with capture_logs() as logs:
            _warn(state, symbol="NIFTY", kind="prev_close")
            _warn(state, symbol="NIFTY", kind="prev_close")  # dropped (dupe)
            _warn(state, symbol="NIFTY", kind="oi")  # DIFFERENT kind → emits
        emitted = [e for e in logs if e["event"] == "movers.prev_baseline_noncontiguous"]
        assert len(emitted) == 2
        assert {(e["symbol"], e["kind"]) for e in emitted} == {
            ("NIFTY", "prev_close"),
            ("NIFTY", "oi"),
        }

    def test_warn_payload_names_both_sessions(self) -> None:
        state = _NoncontiguousWarnState()
        with capture_logs() as logs:
            _warn(state, symbol="NIFTY")
        (event,) = [e for e in logs if e["event"] == "movers.prev_baseline_noncontiguous"]
        assert event["found_session"] == "2025-09-25"
        assert event["expected_session"] == "2025-09-26"
        assert event["snapshot_day"] == "2025-09-29"

    def test_null_expected_session_is_logged_as_none(self) -> None:
        # Defensive: the emitter tolerates a None expected_session (typed
        # optional) — logged as null, never a crash.
        state = _NoncontiguousWarnState()
        with capture_logs() as logs:
            warn_prev_baseline_noncontiguous(
                state,
                kind="prev_close",
                exchange="NSE",
                segment="INDEX",
                symbol="NIFTY",
                snapshot_day=date(2025, 9, 29),
                found_session="2025-09-25",
                expected_session=None,
            )
        (event,) = [e for e in logs if e["event"] == "movers.prev_baseline_noncontiguous"]
        assert event["expected_session"] is None

    def test_reset_seam_reopens_the_cap(self) -> None:
        state = _NoncontiguousWarnState()
        for i in range(_NONCONTIGUOUS_WARN_CAP + 1):
            _warn(state, symbol=f"SYM{i:03d}")
        assert state.suppressed == 1
        state.reset()
        assert (state.emitted, state.suppressed, state.seen) == (0, 0, set())
        with capture_logs() as logs:
            _warn(state, symbol="SYM000")  # previously-seen symbol emits again
        assert [e["event"] for e in logs] == ["movers.prev_baseline_noncontiguous"]
