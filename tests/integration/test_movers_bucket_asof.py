"""Explicit bucket-anchor as-of picks resolve to the COMPLETED bucket (DEC-MOVERS-EXPLICIT-BUCKET-ASOF-001).

The operator's thrice-reported ``/movers`` defect (2026-07-07, NIFTYIT): the
TIME dropdown on a coarse timeframe offers only bucket-ANCHOR minutes (the
09:15-step grid — on 3-minute the last slot is 15:27), but the backend treated
the picked time as an as-of INSTANT, so picking the last 3-minute slot cut the
raw 1-minute frame at 15:27 inclusive and the "bucket" held ONLY the 15:27
minute bar (LTP 27,932.80) — while the broker's chart shows the COMPLETED
candle labelled 15:27 (15:27 + 15:28 + 15:29 → close 27,940.85 = 2min @ 15:29
= 1min @ latest). Picking the last coarse slot could NEVER match the broker.

The fix is ONE COMMON as-of seam (CLAUDE.md §11 —
:func:`brutex.dashboard.movers_snapshot.resolve_asof_cutoff`, consumed by ALL
three tabs): an EXPLICIT pick that is a bucket ANCHOR for the selected
timeframe resolves the effective cut FORWARD to the bucket's final minute
(``anchor + TF − 1``), clamped to the session's last minute 15:29 AND — via the
downstream ``<= cutoff`` :func:`filter_to_ist_minute` — to the day's actual
latest available minute (no bar past the completed bucket is ever included, so
NO look-ahead). Everything else is byte-unchanged: TF = 1minute (anchor ==
end), blank / Latest (DEC-MOVERS-ASOF-LATEST-DEFAULT-001), a NON-anchor
explicit time (as-of-instant semantics kept), and the prev-close %CHNG
baseline.

The PREBUILT derived-TF lake partitions were always CORRECT (the stored
3-minute 15:27 bucket already aggregates the FULL bucket) — the defect was ONLY
the read-time pick semantics.

Skipped when polars / pyarrow are absent (the lake deps).
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("polars")
pytest.importorskip("pyarrow")

from brutex.dashboard.movers_futures import compute_futures_candidates
from brutex.dashboard.movers_options import build_options_payload, compute_options_candidates
from brutex.dashboard.movers_snapshot import (
    MoverSnapshot,
    ScreenInstrument,
    _read_session_bars,
    asof_through_label,
    build_snapshot_payload,
    compute_spot_candidates,
    resolve_asof_cutoff,
)
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter

_DAY = date(2026, 7, 7)
_EXCH = "NSE"

#: The operator's reproduction: the LAST 3-minute bucket of the session,
#: anchored at 15:27 = the three 1-minute bars 15:27 / 15:28 / 15:29 with
#: DISTINCT closes. A partial cut at 15:27 → 27,932.80 (bar-1); the completed
#: bucket close is the 15:29 bar → 27,940.85 (bar-3, = the broker's).
_B1_CLOSE = Decimal("27932.80")  # 15:27 — the bucket-open minute (pre-fix LTP)
_B2_CLOSE = Decimal("27936.00")  # 15:28
_B3_CLOSE = Decimal("27940.85")  # 15:29 — the completed bucket close (post-fix LTP)


def _bar(*, hour: int, minute: int, close: Decimal, volume: int = 100) -> CandleBar:
    """A 1-minute bar at ``HH:MM`` IST on ``_DAY`` (IST = UTC + 05:30, no DST).

    ``high``/``low`` straddle ``close`` so the bar is a genuine range bar; only
    ``close`` is load-bearing for the LTP assertions.
    """
    total = hour * 60 + minute - (5 * 60 + 30)
    return CandleBar(
        timestamp=datetime(_DAY.year, _DAY.month, _DAY.day, total // 60, total % 60, tzinfo=UTC),
        open=close - Decimal("1"),
        high=close + Decimal("2"),
        low=close - Decimal("3"),
        close=close,
        volume=volume,
        open_interest=None,
    )


def _last_bucket_bars() -> list[CandleBar]:
    """The three 1-minute bars of the final 3-minute bucket (15:27 / 15:28 / 15:29)."""
    return [
        _bar(hour=15, minute=27, close=_B1_CLOSE),
        _bar(hour=15, minute=28, close=_B2_CLOSE),
        _bar(hour=15, minute=29, close=_B3_CLOSE),
    ]


def _write(lake: Path, symbol: str, bars: list[CandleBar], *, segment: str = "INDEX") -> None:
    ParquetWriter(lake_root=lake).write(
        exchange=_EXCH, segment=segment, symbol=symbol, interval="1minute", bars=bars
    )


# ── the pure seam: resolve_asof_cutoff ────────────────────────────────────


class TestResolveAsofCutoff:
    def test_anchor_pick_resolves_to_bucket_end(self) -> None:
        # 15:27 (927) is a 3-minute anchor → the bucket's final minute 15:29 (929).
        assert resolve_asof_cutoff("3minute", "15:27") == 15 * 60 + 29

    def test_blank_time_is_whole_day(self) -> None:
        # Latest / blank cut is unchanged (DEC-MOVERS-ASOF-LATEST-DEFAULT-001).
        assert resolve_asof_cutoff("3minute", None) is None
        assert resolve_asof_cutoff("3minute", "") is None

    def test_one_minute_is_identity(self) -> None:
        # TF = 1minute: anchor == end, so the resolved cut is the picked minute.
        assert resolve_asof_cutoff("1minute", "15:27") == 15 * 60 + 27

    def test_mid_day_anchor_resolves_to_its_bucket_end(self) -> None:
        # 10:00 is a 5-minute anchor → 10:04 (the completed [10:00, 10:05) bucket).
        assert resolve_asof_cutoff("5minute", "10:00") == 10 * 60 + 4

    def test_first_session_anchor(self) -> None:
        assert resolve_asof_cutoff("5minute", "09:15") == 9 * 60 + 19

    def test_bucket_end_clamps_to_session_last_minute(self) -> None:
        # 1hour anchor 15:15 → 15:15 + 59 = 16:14, clamped to the session's last
        # minute 15:29 (929) — never past the regular-session close.
        assert resolve_asof_cutoff("1hour", "15:15") == 15 * 60 + 29

    def test_daily_interval_is_whole_day(self) -> None:
        assert resolve_asof_cutoff("1day", "15:27") is None

    def test_unknown_interval_is_whole_day(self) -> None:
        assert resolve_asof_cutoff("bogus", "15:27") is None

    def test_non_anchor_pick_stays_the_raw_minute(self) -> None:
        # A hand-crafted mid-bucket time (the dropdown never emits one) keeps the
        # pre-fix as-of-instant semantics: 15:28 is NOT a 3-minute grid anchor.
        assert resolve_asof_cutoff("3minute", "15:28") == 15 * 60 + 28

    def test_pre_session_anchor_multiple_stays_raw(self) -> None:
        # 09:00 (540) is BELOW the session-open guard, so it is NOT treated as an
        # anchor even though it is a 5-minute multiple offset → left raw.
        assert resolve_asof_cutoff("5minute", "09:00") == 9 * 60

    def test_malformed_time_raises(self) -> None:
        with pytest.raises(ValueError, match="malformed time"):
            resolve_asof_cutoff("3minute", "99x99")


class TestAsofThroughLabel:
    def test_extended_anchor_reports_the_bucket_end(self) -> None:
        assert asof_through_label("3minute", "15:27") == "15:29"
        assert asof_through_label("5minute", "10:00") == "10:04"

    def test_identity_and_whole_day_report_none(self) -> None:
        assert asof_through_label("1minute", "15:27") is None  # anchor == end
        assert asof_through_label("3minute", None) is None  # latest
        assert asof_through_label("1day", "15:27") is None  # daily
        assert asof_through_label("3minute", "15:28") is None  # non-anchor (no extension)


# ── the read layer: before/after at a fixed cutoff (PROVES the fix) ────────


class TestReadLayerBeforeAfter:
    def test_raw_anchor_cut_yields_bar_one_the_pre_fix_ltp(self, tmp_path: Path) -> None:
        # The OLD behaviour: cutting at the picked anchor 15:27 (927) kept only the
        # bucket-OPEN minute → the partial bucket's close is bar-1 (27,932.80).
        _write(tmp_path, "NIFTYIT", _last_bucket_bars())
        bars = _read_session_bars(
            tmp_path,
            exchange=_EXCH,
            segment="INDEX",
            symbol="NIFTYIT",
            day=_DAY,
            minutes=3,
            cutoff_min=15 * 60 + 27,
        )
        assert bars[-1].close == _B1_CLOSE

    def test_bucket_end_cut_yields_bar_three_the_completed_close(self, tmp_path: Path) -> None:
        # The FIXED behaviour: cutting at the resolved bucket end 15:29 (929) keeps
        # all three minutes → the completed bucket's close is bar-3 (27,940.85).
        _write(tmp_path, "NIFTYIT", _last_bucket_bars())
        bars = _read_session_bars(
            tmp_path,
            exchange=_EXCH,
            segment="INDEX",
            symbol="NIFTYIT",
            day=_DAY,
            minutes=3,
            cutoff_min=15 * 60 + 29,
        )
        assert bars[-1].close == _B3_CLOSE


# ── end to end through compute_spot_candidates (the shipped path) ─────────


class TestSpotBucketAsof:
    def test_anchor_pick_shows_the_completed_bucket_close(self, tmp_path: Path) -> None:
        # The operator repro, end to end: 3-minute @ 15:27 → the COMPLETED bucket
        # close 27,940.85 (bar-3 = the broker's), NOT the partial 27,932.80.
        _write(tmp_path, "NIFTYIT", _last_bucket_bars())
        cands = compute_spot_candidates(
            lake_root=tmp_path, interval="3minute", date=_DAY.isoformat(), time="15:27"
        )
        assert len(cands.snapshots) == 1
        assert cands.snapshots[0].ltp == _B3_CLOSE

    def test_one_minute_pick_is_the_picked_minute(self, tmp_path: Path) -> None:
        # TF = 1minute @ 15:27 is identity → the 15:27 close (27,932.80), unchanged.
        _write(tmp_path, "NIFTYIT", _last_bucket_bars())
        cands = compute_spot_candidates(
            lake_root=tmp_path, interval="1minute", date=_DAY.isoformat(), time="15:27"
        )
        assert cands.snapshots[0].ltp == _B1_CLOSE

    def test_latest_blank_matches_the_completed_bucket(self, tmp_path: Path) -> None:
        # Blank / Latest whole-day cut on 3-minute also lands on the completed
        # last bucket (27,940.85) — the operator's "= 1min @ latest".
        _write(tmp_path, "NIFTYIT", _last_bucket_bars())
        cands = compute_spot_candidates(
            lake_root=tmp_path, interval="3minute", date=_DAY.isoformat(), time=None
        )
        assert cands.snapshots[0].ltp == _B3_CLOSE

    def test_mid_day_anchor_completes_its_bucket_without_lookahead(self, tmp_path: Path) -> None:
        # 10:00 @ 5-minute → the completed [10:00, 10:05) bucket close (10:04),
        # and the 10:05 bar (the NEXT bucket) is EXCLUDED — no look-ahead.
        bars = [
            _bar(hour=10, minute=0, close=Decimal("100.00")),
            _bar(hour=10, minute=1, close=Decimal("101.00")),
            _bar(hour=10, minute=2, close=Decimal("102.00")),
            _bar(hour=10, minute=3, close=Decimal("103.00")),
            _bar(hour=10, minute=4, close=Decimal("104.00")),
            _bar(hour=10, minute=5, close=Decimal("999.00")),  # next bucket — must NOT leak
        ]
        _write(tmp_path, "NIFTYIT", bars)
        cands = compute_spot_candidates(
            lake_root=tmp_path, interval="5minute", date=_DAY.isoformat(), time="10:00"
        )
        assert cands.snapshots[0].ltp == Decimal("104.00")

    def test_bucket_end_clamps_to_session_close(self, tmp_path: Path) -> None:
        # 1hour @ 15:15 resolves through 15:29 (clamped), reading the 15:29 bar.
        bars = [
            _bar(hour=15, minute=15, close=Decimal("100.00")),
            _bar(hour=15, minute=29, close=Decimal("200.00")),
        ]
        _write(tmp_path, "NIFTYIT", bars)
        cands = compute_spot_candidates(
            lake_root=tmp_path, interval="1hour", date=_DAY.isoformat(), time="15:15"
        )
        assert cands.snapshots[0].ltp == Decimal("200.00")

    def test_day_ends_mid_bucket_clamps_to_latest_available(self, tmp_path: Path) -> None:
        # The day's data ENDS at 15:28 (no 15:29 bar). Picking 15:27 @ 3-minute
        # resolves through 15:29, but the ``<= cutoff`` filter caps at the latest
        # AVAILABLE minute (15:28) — the partial-because-the-day-ended bucket's
        # last real close, never a fabricated 15:29 (Golden Rule #1).
        _write(
            tmp_path,
            "NIFTYIT",
            [
                _bar(hour=15, minute=27, close=_B1_CLOSE),
                _bar(hour=15, minute=28, close=_B2_CLOSE),
            ],
        )
        cands = compute_spot_candidates(
            lake_root=tmp_path, interval="3minute", date=_DAY.isoformat(), time="15:27"
        )
        assert cands.snapshots[0].ltp == _B2_CLOSE


# ── all three tabs resolve at the ONE seam (parity) ───────────────────────


class TestFuturesOptionsParity:
    def test_futures_anchor_pick_shows_completed_bucket(self, tmp_path: Path) -> None:
        fut = "NSE-NIFTY-30Jul26-FUT"  # expiry strictly after _DAY (front month)
        _write(tmp_path, fut, _last_bucket_bars(), segment="FNO")
        _write(tmp_path, "NIFTY", _last_bucket_bars())  # underlying spot (premium)
        cands = compute_futures_candidates(
            lake_root=tmp_path, interval="3minute", date=_DAY.isoformat(), time="15:27"
        )
        assert len(cands.movers) == 1
        # Same completed-bucket close as spot — the shared resolve_asof_cutoff seam.
        assert cands.movers[0].ltp == _B3_CLOSE

    def test_options_anchor_pick_shows_completed_bucket(self, tmp_path: Path) -> None:
        ce = "NSE-NIFTY-30Jul26-24000-CE"
        _write(tmp_path, ce, _last_bucket_bars(), segment="FNO")
        _write(tmp_path, "NIFTY", _last_bucket_bars())  # underlying spot (moneyness)
        cands = compute_options_candidates(
            lake_root=tmp_path,
            interval="3minute",
            date=_DAY.isoformat(),
            time="15:27",
            expiry_tier="near",
            expiry_track="all",
        )
        assert len(cands.movers) == 1
        assert cands.movers[0].ltp == _B3_CLOSE


# ── honest provenance in the payload / note (additive) ────────────────────


class TestPayloadProvenance:
    @staticmethod
    def _gainer() -> MoverSnapshot:
        """One ranked spot gainer so the success ("As-of …") note branch is reached."""
        return MoverSnapshot(
            instrument=ScreenInstrument(
                symbol="NIFTYIT", exchange=_EXCH, segment="INDEX", display_name="NIFTYIT"
            ),
            ltp=_B3_CLOSE,
            prev_close=Decimal("27000.00"),
            change=Decimal("940.85"),
            pct_change=Decimal("3.48"),
            volume=300,
            value_paisa=1_000_000,
            bar_count=3,
        )

    def test_spot_note_and_field_carry_the_bucket_through(self) -> None:
        payload = build_snapshot_payload(
            [self._gainer()],
            interval="3minute",
            date=_DAY.isoformat(),
            time="15:27",
            top_n=10,
            skipped=0,
            resolved_through="15:29",
        )
        assert payload["asof_through"] == "15:29"
        assert "15:27 bucket · through 15:29" in payload["note"]

    def test_spot_none_through_is_byte_identical_note(self) -> None:
        payload = build_snapshot_payload(
            [self._gainer()],
            interval="3minute",
            date=_DAY.isoformat(),
            time="15:27",
            top_n=10,
            skipped=0,
        )
        assert payload["asof_through"] is None
        assert "bucket · through" not in payload["note"]

    def test_options_payload_echoes_asof_through(self, tmp_path: Path) -> None:
        # Real computed option movers so the success note branch is reached, then
        # rebuilt with resolved_through (the exact route wiring).
        ce = "NSE-NIFTY-30Jul26-24000-CE"
        _write(tmp_path, ce, _last_bucket_bars(), segment="FNO")
        _write(tmp_path, "NIFTY", _last_bucket_bars())
        cands = compute_options_candidates(
            lake_root=tmp_path,
            interval="3minute",
            date=_DAY.isoformat(),
            time="15:27",
            expiry_tier="near",
            expiry_track="all",
        )
        payload = build_options_payload(
            cands.movers,
            interval="3minute",
            date=cands.date,
            time="15:27",
            top_n=10,
            skipped=cands.skipped,
            resolved_through="15:29",
        )
        assert payload["asof_through"] == "15:29"
        assert "15:27 bucket · through 15:29" in payload["note"]

    def test_label_helper_matches_the_wire(self) -> None:
        # The route derives the echo from asof_through_label — pin the coupling.
        assert asof_through_label("3minute", "15:27") == "15:29"
