"""Stored-first movers reads over prebuilt partitions (DEC-PIPELINE-AUTOPREBUILD-001, chunk 2).

Against a REAL Parquet lake (``tmp_path`` + :class:`~brutex.data.parquet_writer.ParquetWriter`)
+ the REAL prebuild materialiser
(:func:`~brutex.data.timeframe_materialize.materialize_lake_timeframes`):

* **SPOT — the equality contract**: after the prebuild materialises a symbol's
  derived timeframes, the intraday movers candidates computed from the STORED
  interval partition are byte-identical to the on-the-fly 1-minute resample
  (the same fixed-arithmetic session-anchored resampler wrote them), and the
  candidates' ``stored_reads`` / ``resampled_reads`` provenance counts flip.
* **SPOT — the honest fallback**: with no stored partition (a CLI-only /
  never-prebuilt lake) the resampled path serves the bars and says so.
* **FUTURES + OPTIONS — materialisation invariance**: their session readers are
  the LOCKED 1-minute-native as-of snapshot (DEC-FNO-TF-DERIVE-OI-001 — NOT a
  resampled TF bucket; it also carries the stored Greeks overlay off the same
  1-minute frame, DEC-GREEKS-ENGINE-PR3B-001), so the prebuilt TF partitions
  must NOT change their candidates — pinned before/after a full materialise.
  The way F&O movers DO read the prebuilt partitions is their prior-day
  baseline: ``_prev_day_close`` / ``_prev_day_oi`` read the derived ``1day``
  FIRST (the 1-minute walk-back is only the fallback) — pinned by asserting the
  fallback is never touched once the prebuild has written the ``1day``.

Skipped when polars / pyarrow are absent (the lake deps).
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import structlog.testing

if TYPE_CHECKING:
    import polars as pl

pytest.importorskip("polars")
pytest.importorskip("pyarrow")

from brutex.dashboard.movers_futures import compute_futures_candidates
from brutex.dashboard.movers_options import compute_options_candidates
from brutex.dashboard.movers_snapshot import compute_spot_candidates
from brutex.dashboard.movers_stored_read import read_stored_day_frame
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.timeframe_materialize import PrebuildKind, materialize_lake_timeframes

_DAY = date(2024, 6, 13)
_PRIOR = date(2024, 6, 12)
#: Daily-derive cutoff — past both sessions so the prebuild writes BOTH days'
#: derived ``1day`` bars (a still-forming session is never emitted).
_DAILY_BEFORE = date(2024, 6, 20)

_FUT = "NSE-NIFTY-27Jun24-FUT"
_OPT = "NSE-NIFTY-27Jun24-24000-CE"


def _bar(
    *, day: date, hour: int, minute: int, close: str, volume: int, oi: int | None = None
) -> CandleBar:
    """A 1-minute bar at ``HH:MM`` IST on *day* (IST = UTC + 05:30, no DST)."""
    c = Decimal(close)
    total = hour * 60 + minute - (5 * 60 + 30)
    return CandleBar(
        timestamp=datetime(day.year, day.month, day.day, total // 60, total % 60, tzinfo=UTC),
        open=c - Decimal("1"),
        high=c + Decimal("2"),
        low=c - Decimal("3"),
        close=c,
        volume=volume,
        open_interest=oi,
    )


def _session(day: date, base: int, *, oi: int | None = None) -> list[CandleBar]:
    """Ten 1-minute bars 09:15–09:24 IST with varying closes/volumes on *day*."""
    return [
        _bar(
            day=day,
            hour=9,
            minute=15 + i,
            close=str(base + i * 10),
            volume=100 + i,
            oi=None if oi is None else oi + i,
        )
        for i in range(10)
    ]


def _write(lake: Path, *, segment: str, symbol: str, interval: str, bars: list[CandleBar]) -> None:
    ParquetWriter(lake_root=lake).write(
        exchange="NSE", segment=segment, symbol=symbol, interval=interval, bars=bars
    )


def _seed_spot(lake: Path, symbol: str, base: int) -> None:
    """1-minute sessions on both days + the stored ``1day`` prior-close bar."""
    _write(lake, segment="INDEX", symbol=symbol, interval="1minute", bars=_session(_PRIOR, base))
    _write(lake, segment="INDEX", symbol=symbol, interval="1minute", bars=_session(_DAY, base + 5))
    close = Decimal(str(base + 90))
    _write(
        lake,
        segment="INDEX",
        symbol=symbol,
        interval="1day",
        bars=[
            CandleBar(
                timestamp=datetime(_PRIOR.year, _PRIOR.month, _PRIOR.day, 3, 45, tzinfo=UTC),
                open=close - 10,
                high=close,
                low=close - 20,
                close=close,
                volume=1000,
                open_interest=None,
            )
        ],
    )


def _materialize_spot(lake: Path, symbol: str) -> None:
    result = materialize_lake_timeframes(
        lake_root=lake,
        exchange="NSE",
        segment="INDEX",
        symbol=symbol,
        kind=PrebuildKind.SPOT,
    )
    assert result.total_bars > 0  # the prebuild really wrote stored TF partitions.


# ── SPOT: stored-read ≡ on-the-fly resample (the equality pin) ────────────


class TestSpotStoredReadEquality:
    def test_spot_stored_read_equals_on_the_fly_resample(self, tmp_path: Path) -> None:
        """The equality contract: prebuilt partitions were written by the SAME
        resampler, so the stored-first read yields byte-identical candidates —
        only the provenance counts flip (resampled → stored). The as-of 09:19
        sits on the 5-minute grid's bucket-FINAL minute (09:15–09:19), so every
        kept bucket is fully elapsed and the stored FULL-bucket read reproduces
        the unified raw-1m as-of cut exactly (DEC-HARDENING-LEFTOVERS-001)."""
        _seed_spot(tmp_path, "NIFTY", 20000)
        before = compute_spot_candidates(
            lake_root=tmp_path, interval="5minute", date=_DAY.isoformat(), time="09:19"
        )
        assert before.snapshots, "seed must produce a snapshot"
        assert (before.stored_reads, before.resampled_reads) == (0, 1)

        _materialize_spot(tmp_path, "NIFTY")
        after = compute_spot_candidates(
            lake_root=tmp_path, interval="5minute", date=_DAY.isoformat(), time="09:19"
        )
        assert after.snapshots == before.snapshots  # byte-identical metrics
        assert after.skipped == before.skipped
        assert (after.stored_reads, after.resampled_reads) == (1, 0)

    def test_spot_partially_elapsed_bucket_falls_back_to_resample(self, tmp_path: Path) -> None:
        """A MID-bucket as-of cannot be served from stored FULL buckets — the
        read honestly bypasses the stored partition and falls back to the
        1-minute resample, whose final bucket aggregates ONLY the elapsed
        minutes (the unified as-of semantic, DEC-HARDENING-LEFTOVERS-001 —
        never a forming-bucket leak of minutes past the as-of instant)."""
        from brutex.dashboard.movers_snapshot import _read_intraday_bars

        _seed_spot(tmp_path, "NIFTY", 20000)
        _materialize_spot(tmp_path, "NIFTY")
        bars, from_stored = _read_intraday_bars(
            tmp_path,
            exchange="NSE",
            segment="INDEX",
            symbol="NIFTY",
            interval="5minute",
            day=_DAY,
            minutes=5,
            cutoff_min=9 * 60 + 22,  # as of 09:22 IST — inside the 09:20 bucket
        )
        assert from_stored is False  # stored partition present, honestly bypassed
        assert len(bars) == 2
        # The partial 09:20 bucket ends at the as-of instant: 09:20–09:22 only
        # (a stored/leaking read would close the bucket at 09:24 → 20095).
        assert bars[-1].close == Decimal("20075")
        assert bars[-1].volume == 105 + 106 + 107
        # And the payload provenance reports the resample fallback honestly.
        cands = compute_spot_candidates(
            lake_root=tmp_path, interval="5minute", date=_DAY.isoformat(), time="09:22"
        )
        assert (cands.stored_reads, cands.resampled_reads) == (0, 1)

    def test_spot_stored_read_equality_whole_day_no_cutoff(self, tmp_path: Path) -> None:
        # The cutoff-free intraday permutation (time=None ⇒ whole session).
        _seed_spot(tmp_path, "NIFTY", 20000)
        before = compute_spot_candidates(
            lake_root=tmp_path, interval="15minute", date=_DAY.isoformat(), time=None
        )
        _materialize_spot(tmp_path, "NIFTY")
        after = compute_spot_candidates(
            lake_root=tmp_path, interval="15minute", date=_DAY.isoformat(), time=None
        )
        assert after.snapshots == before.snapshots
        assert (after.stored_reads, after.resampled_reads) == (1, 0)

    def test_spot_mixed_provenance_counts_per_symbol(self, tmp_path: Path) -> None:
        # Only NIFTY is prebuilt → NIFTY serves stored, BANKNIFTY falls back to
        # the resample — the counts expose the mixed provenance honestly.
        # (09:19 = bucket-final-aligned, so the stored read is eligible.)
        _seed_spot(tmp_path, "NIFTY", 20000)
        _seed_spot(tmp_path, "BANKNIFTY", 45000)
        _materialize_spot(tmp_path, "NIFTY")
        cands = compute_spot_candidates(
            lake_root=tmp_path, interval="5minute", date=_DAY.isoformat(), time="09:19"
        )
        assert len(cands.snapshots) == 2
        assert (cands.stored_reads, cands.resampled_reads) == (1, 1)

    def test_spot_fallback_when_no_stored_partition(self, tmp_path: Path) -> None:
        # A never-prebuilt lake (CLI-only): the on-the-fly resample serves the
        # bars and the payload provenance says so (the honest fallback).
        _seed_spot(tmp_path, "NIFTY", 20000)
        cands = compute_spot_candidates(
            lake_root=tmp_path, interval="5minute", date=_DAY.isoformat(), time="09:20"
        )
        assert len(cands.snapshots) == 1
        assert (cands.stored_reads, cands.resampled_reads) == (0, 1)

    def test_spot_daily_path_counts_as_stored(self, tmp_path: Path) -> None:
        # The 1day movers path reads the stored (derived) 1day partition by
        # construction — it counts in stored_reads (and is otherwise unchanged).
        _seed_spot(tmp_path, "NIFTY", 20000)
        _write(
            tmp_path,
            segment="INDEX",
            symbol="NIFTY",
            interval="1day",
            bars=[
                CandleBar(
                    timestamp=datetime(_DAY.year, _DAY.month, _DAY.day, 3, 45, tzinfo=UTC),
                    open=Decimal("20000"),
                    high=Decimal("20100"),
                    low=Decimal("19900"),
                    close=Decimal("20095"),
                    volume=5000,
                    open_interest=None,
                )
            ],
        )
        cands = compute_spot_candidates(
            lake_root=tmp_path, interval="1day", date=_DAY.isoformat(), time=None
        )
        assert len(cands.snapshots) == 1
        assert (cands.stored_reads, cands.resampled_reads) == (1, 0)

    def test_spot_stored_read_falls_back_while_derive_lock_held(self, tmp_path: Path) -> None:
        """DEC-ENGINE-MISC-PACK-001: a mid-flight cross-process derive is never read.

        While a writer holds the exclusive ``<lake>/.locks/derive.lock`` (an
        flock — a second fd in this process contends exactly like a second
        process), the stored-first branch probes ``derive_in_progress`` and
        HONESTLY bypasses the possibly-half-refreshed stored partition: the
        on-the-fly 1-minute resample serves BYTE-IDENTICAL candidates (the
        pinned equality reference) counted under ``resampled_reads``; once the
        lock is released, the stored path resumes.
        """
        from brutex.data.derive_lock import derive_write_lock

        _seed_spot(tmp_path, "NIFTY", 20000)
        _materialize_spot(tmp_path, "NIFTY")
        stored = compute_spot_candidates(
            lake_root=tmp_path, interval="5minute", date=_DAY.isoformat(), time="09:19"
        )
        assert (stored.stored_reads, stored.resampled_reads) == (1, 0)
        with derive_write_lock(tmp_path):
            gated = compute_spot_candidates(
                lake_root=tmp_path, interval="5minute", date=_DAY.isoformat(), time="09:19"
            )
        # Byte-equal ranking off the resample reference — only provenance flips.
        assert gated.snapshots == stored.snapshots
        assert gated.skipped == stored.skipped
        assert (gated.stored_reads, gated.resampled_reads) == (0, 1)
        # Lock released → the stored partition serves again.
        released = compute_spot_candidates(
            lake_root=tmp_path, interval="5minute", date=_DAY.isoformat(), time="09:19"
        )
        assert (released.stored_reads, released.resampled_reads) == (1, 0)

    def test_materialize_holds_derive_lock_while_writing(self, tmp_path: Path) -> None:
        """The WRITER side: `materialize_lake_timeframes` holds the EX lock.

        Pinned by observing ``derive_in_progress`` from INSIDE the injected
        write path (the reader probe contends on a separate fd, so this is the
        cross-process observation), and released after the call returns.
        """
        from brutex.data.derive_lock import derive_in_progress
        from brutex.data.timeframe_materialize import materialize_lake_timeframes

        _seed_spot(tmp_path, "NIFTY", 20000)
        observed: list[bool] = []
        real_write = ParquetWriter.write

        def _spy_write(self: ParquetWriter, **kwargs: object) -> object:
            observed.append(derive_in_progress(tmp_path))
            return real_write(self, **kwargs)  # type: ignore[arg-type]

        ParquetWriter.write = _spy_write  # type: ignore[method-assign, assignment]
        try:
            materialize_lake_timeframes(
                lake_root=tmp_path,
                exchange="NSE",
                segment="INDEX",
                symbol="NIFTY",
                kind=PrebuildKind.SPOT,
                tfs=("5m",),
            )
        finally:
            ParquetWriter.write = real_write  # type: ignore[method-assign]
        assert observed and all(observed), "every lake write must run under the EX derive lock"
        assert derive_in_progress(tmp_path) is False  # released after the call.

    def test_spot_1minute_interval_serves_from_stored_base(self, tmp_path: Path) -> None:
        # interval=1minute: the stored partition IS the fetched base — the
        # stored path serves it (no resample) with identical bars.
        _seed_spot(tmp_path, "NIFTY", 20000)
        cands = compute_spot_candidates(
            lake_root=tmp_path, interval="1minute", date=_DAY.isoformat(), time="09:18"
        )
        assert len(cands.snapshots) == 1
        assert (cands.stored_reads, cands.resampled_reads) == (1, 0)
        # 09:15..09:18 inclusive → 4 bars.
        assert cands.snapshots[0].bar_count == 4


# ── prefer_resample under a live pull (DEC-MOVERS-GATE-PREBUILD-SCOPE-001) ─


def _poison_stored_5minute(lake: Path, symbol: str = "NIFTY") -> None:
    """A STALE stored 5-minute partition whose close the fresh 1-minute can never yield.

    Simulates the mid-fetch hazard the ``prefer_resample`` switch closes: the
    stored TF partitions are rewritten only by the pipeline's FINAL prebuild,
    so during a fetch they can predate the freshly-landing 1-minute bars.
    """
    _write(
        lake,
        segment="INDEX",
        symbol=symbol,
        interval="5minute",
        bars=[_bar(day=_DAY, hour=9, minute=15, close="99999", volume=1)],
    )


class TestPreferResampleUnderPull:
    def test_prefer_resample_bypasses_stale_stored_partition(self, tmp_path: Path) -> None:
        """DELIBERATELY FLIPPED default arm (DEC-MOVERS-STORED-FRESHNESS-001).

        Historically (DEC-MOVERS-GATE-PREBUILD-SCOPE-001) the aligned stored
        partition ALWAYS won, so the default arm pinned the POISONED stale close
        being served and ``prefer_resample`` was the only escape. The stored-first
        read is now freshness-guarded: the poison (one 09:15 bucket, close 99999)
        is NOT the resample of the day's 1-minute base, so the VERIFY-EQUALITY
        guard reads it STALE and the DEFAULT read falls back to the fresh
        1-minute resample byte-identically with ONE
        ``movers.stored_stale_fallback`` WARNING. ``prefer_resample`` remains the
        mid-pull belt (its arm below is byte-unchanged). This flip is the
        bugfix-reversion-check satisfier: it fails with the guard reverted, by
        construction."""
        _seed_spot(tmp_path, "NIFTY", 20000)
        # The truth: what the fresh 1-minute data resamples to (no stored TF yet).
        reference = compute_spot_candidates(
            lake_root=tmp_path, interval="5minute", date=_DAY.isoformat(), time="09:19"
        )
        assert (reference.stored_reads, reference.resampled_reads) == (0, 1)
        # A STALE stored partition lands (prior pull's prebuild, poisoned close).
        _poison_stored_5minute(tmp_path)
        with structlog.testing.capture_logs() as logs:
            stored_first = compute_spot_candidates(
                lake_root=tmp_path, interval="5minute", date=_DAY.isoformat(), time="09:19"
            )
        # The freshness guard catches the stale partition — the read falls back to
        # the 1-minute resample (provenance flips) and serves the TRUTH.
        assert (stored_first.stored_reads, stored_first.resampled_reads) == (0, 1)
        assert stored_first.snapshots == reference.snapshots
        stale_events = [e for e in logs if e["event"] == "movers.stored_stale_fallback"]
        assert len(stale_events) == 1
        assert stale_events[0]["symbol"] == "NIFTY"
        assert stale_events[0]["interval"] == "5minute"
        # The pull-active switch: the resample wins — fresh, byte-identical to truth.
        fresh = compute_spot_candidates(
            lake_root=tmp_path,
            interval="5minute",
            date=_DAY.isoformat(),
            time="09:19",
            prefer_resample=True,
        )
        assert (fresh.stored_reads, fresh.resampled_reads) == (0, 1)
        assert fresh.snapshots == reference.snapshots

    def test_read_intraday_bars_prefer_resample_skips_stored(self, tmp_path: Path) -> None:
        # Even with a stored partition present AND a bucket-final-aligned cutoff
        # (the exact case the stored read normally serves), prefer_resample=True
        # takes the 1-minute resample — from_stored is honestly False.
        from brutex.dashboard.movers_snapshot import _read_intraday_bars

        _seed_spot(tmp_path, "NIFTY", 20000)
        _materialize_spot(tmp_path, "NIFTY")
        bars, from_stored = _read_intraday_bars(
            tmp_path,
            exchange="NSE",
            segment="INDEX",
            symbol="NIFTY",
            interval="5minute",
            day=_DAY,
            minutes=5,
            cutoff_min=9 * 60 + 19,
            prefer_resample=True,
        )
        assert from_stored is False
        assert bars  # the resample served the same session
        # And the default (no pull) still serves the stored partition — the
        # stored-first behaviour is byte-identical when the switch is off.
        _, default_from_stored = _read_intraday_bars(
            tmp_path,
            exchange="NSE",
            segment="INDEX",
            symbol="NIFTY",
            interval="5minute",
            day=_DAY,
            minutes=5,
            cutoff_min=9 * 60 + 19,
        )
        assert default_from_stored is True


# ── REFUTER ROUND 1: the interior-hole re-pull + dual-exchange warn ────────
# (DEC-MOVERS-STORED-FRESHNESS-001 — ports of the executed round-1 repro scripts
# attack_a1_hole_heal.py / attack_a1b_cutoff_ltp.py / attack_a2_a3_warn.py, all
# through PRODUCTION writers only.)


def _seed_hole_heal_lake(lake: Path) -> None:
    """The #1031 interior-hole re-pull producer chain, production writers only.

    1. A HOLEY 1-minute pull lands: 09:15 + 09:25..09:29 (vendor hole
       09:16..09:24 — the #1031 interior-gap class, stored legitimately).
    2. The REAL pipeline prebuild materialises the stored TF partitions FROM the
       holey base (5minute buckets 09:15 [one minute] + 09:25 [five minutes]).
    3. The hole is RE-PULLED (the #1031 self-heal — e.g. a CLI ``brutex fetch``,
       which never derives TFs). The 1-minute base is now complete 09:15..09:29;
       the stored 5minute partition is STALE with the divergence in the MIDDLE of
       the day (bucket 09:15 wrong OHLCV, bucket 09:20 MISSING) while its day-MAX
       still reads ahead of the base (565 + 5 = 570 > 569) — the shape that
       refuted the round-1 day-max predicate.
    """
    writer = ParquetWriter(lake_root=lake)
    holey = [_bar(day=_DAY, hour=9, minute=15, close="20000", volume=100)] + [
        _bar(day=_DAY, hour=9, minute=25 + i, close=str(20100 + i * 10), volume=200 + i)
        for i in range(5)
    ]
    writer.write(exchange="NSE", segment="INDEX", symbol="NIFTY", interval="1minute", bars=holey)
    result = materialize_lake_timeframes(
        lake_root=lake, exchange="NSE", segment="INDEX", symbol="NIFTY", kind=PrebuildKind.SPOT
    )
    assert result.total_bars > 0  # the prebuild really wrote the stale-to-be TFs.
    heal = [
        _bar(day=_DAY, hour=9, minute=16 + i, close=str(20010 + i * 10), volume=110 + i)
        for i in range(9)
    ]
    writer.write(exchange="NSE", segment="INDEX", symbol="NIFTY", interval="1minute", bars=heal)


class TestInteriorHoleHealFreshness:
    def test_whole_day_serves_fresh_resample_with_one_warn(self, tmp_path: Path) -> None:
        """attack_a1 port: the hole-heal lake serves FRESH values + exactly ONE WARN.

        Fails pre-fix (the round-1 day-max guard): ``from_stored=True`` with stale
        volume 1110 beside the fresh 1m-sourced turnover (reference volume 2136)
        and ZERO warns.
        """
        from brutex.dashboard.movers_snapshot import _read_intraday_bars, _read_session_bars

        _seed_hole_heal_lake(tmp_path)
        bars, from_stored = _read_intraday_bars(
            tmp_path,
            exchange="NSE",
            segment="INDEX",
            symbol="NIFTY",
            interval="5minute",
            day=_DAY,
            minutes=5,
            cutoff_min=None,
        )
        reference = _read_session_bars(
            tmp_path,
            exchange="NSE",
            segment="INDEX",
            symbol="NIFTY",
            day=_DAY,
            minutes=5,
            cutoff_min=None,
        )
        assert from_stored is False
        assert bars == reference
        assert len(bars) == 3  # 09:15 + 09:20 + 09:25 — the healed truth.
        # The full compute path: fresh values, honest provenance, ONE warn.
        with structlog.testing.capture_logs() as logs:
            cands = compute_spot_candidates(
                lake_root=tmp_path, interval="5minute", date=_DAY.isoformat(), time=""
            )
            ref_cands = compute_spot_candidates(
                lake_root=tmp_path,
                interval="5minute",
                date=_DAY.isoformat(),
                time="",
                prefer_resample=True,
            )
        assert (cands.stored_reads, cands.resampled_reads) == (0, 1)
        assert cands.snapshots == ref_cands.snapshots
        snap = cands.snapshots[0]
        assert snap.volume == 100 + sum(110 + i for i in range(9)) + sum(
            200 + i for i in range(5)
        )  # 2136 — never the stale 1110.
        stale_events = [e for e in logs if e["event"] == "movers.stored_stale_fallback"]
        assert len(stale_events) == 1
        assert (stale_events[0]["exchange"], stale_events[0]["symbol"]) == ("NSE", "NIFTY")

    def test_aligned_cutoff_serves_fresh_ltp_with_warn(self, tmp_path: Path) -> None:
        """attack_a1b port: at the bucket-final ALIGNED 09:19 cutoff the LTP is FRESH.

        Fails pre-fix: the served LTP itself was stale (₹20,000 — the truncated
        09:15 bucket's pre-heal close — vs the fresh ₹20,040), silently,
        ``stored_reads=1``, zero warns.
        """
        _seed_hole_heal_lake(tmp_path)
        with structlog.testing.capture_logs() as logs:
            served = compute_spot_candidates(
                lake_root=tmp_path, interval="5minute", date=_DAY.isoformat(), time="09:19"
            )
            reference = compute_spot_candidates(
                lake_root=tmp_path,
                interval="5minute",
                date=_DAY.isoformat(),
                time="09:19",
                prefer_resample=True,
            )
        assert (served.stored_reads, served.resampled_reads) == (0, 1)
        assert served.snapshots == reference.snapshots
        assert served.snapshots[0].ltp == Decimal("20040")  # 09:19's healed close.
        stale_events = [e for e in logs if e["event"] == "movers.stored_stale_fallback"]
        assert len(stale_events) == 1


class TestDualExchangeStaleWarns:
    def test_dual_exchange_stale_symbol_both_exchanges_warned(self, tmp_path: Path) -> None:
        """attack_a2 port (REFUTER ROUND 1 defect 2): NSE + BSE stale ⇒ TWO warns.

        The same symbol carries a stale stored 5minute partition on BOTH exchanges;
        one ``compute_spot_candidates(exchange=None)`` build falls back on both.
        Pre-fix the ``(symbol, interval)`` dedupe key emitted ONE warn — the second
        exchange's stale lake partition was never named.
        """
        for exchange in ("NSE", "BSE"):
            writer = ParquetWriter(lake_root=tmp_path)
            writer.write(
                exchange=exchange,
                segment="INDEX",
                symbol="RELIANCE",
                interval="1minute",
                bars=_session(_DAY, 20000)[:5],
            )
            materialize_lake_timeframes(
                lake_root=tmp_path,
                exchange=exchange,
                segment="INDEX",
                symbol="RELIANCE",
                kind=PrebuildKind.SPOT,
            )
            # The base grows AFTER the prebuild — the stored TFs are now stale.
            writer.write(
                exchange=exchange,
                segment="INDEX",
                symbol="RELIANCE",
                interval="1minute",
                bars=[
                    _bar(day=_DAY, hour=9, minute=20 + i, close=str(21000 + i), volume=300 + i)
                    for i in range(5)
                ],
            )
        with structlog.testing.capture_logs() as logs:
            cands = compute_spot_candidates(
                lake_root=tmp_path,
                interval="5minute",
                date=_DAY.isoformat(),
                time="",
                exchange=None,
            )
        assert len(cands.snapshots) == 2
        assert (cands.stored_reads, cands.resampled_reads) == (0, 2)
        warns = [e for e in logs if e["event"] == "movers.stored_stale_fallback"]
        assert sorted((e["exchange"], e["symbol"]) for e in warns) == [
            ("BSE", "RELIANCE"),
            ("NSE", "RELIANCE"),
        ]


# ── dtype-INSENSITIVE `.equals` semantics pin (REFUTER ROUND 7 H1-1) ──


class TestDtypeInsensitiveEqualsPin:
    def test_dtype_recast_stored_partition_serves_fresh_zero_warns(self, tmp_path: Path) -> None:
        """h1_a2_dtype_drift.py port: the documented ``.equals`` semantics pin.

        polars ``DataFrame.equals`` is dtype-INSENSITIVE (1.42.1: Float64 ≡
        Float32 / Int64 ≡ Int32 when the VALUES are equal), so a stored TF
        partition hand-recast to narrower dtypes with IDENTICAL values reads
        FRESH — served ``from_stored`` with ZERO stale warns, value-equal to the
        ``prefer_resample`` reference (value-exactness is the load-bearing
        property; no real writer emits a drifted schema — writer schema fixed;
        precision-LOSING casts compare unequal ⇒ stale ⇒ fallback). If a future
        polars bump flips ``.equals`` to dtype-sensitive, this pin fails and
        forces a conscious docs + behavior decision instead of silent drift.
        """
        import polars as pl
        from brutex.dashboard.movers_snapshot import _read_intraday_bars, _StoredStaleWarnState

        _seed_spot(tmp_path, "NIFTY", 20000)
        _materialize_spot(tmp_path, "NIFTY")
        stored_file = tmp_path / "bars/NSE/INDEX/NIFTY/5minute/2024/06.parquet"
        frame = pl.read_parquet(stored_file)
        frame.with_columns(
            pl.col("close").cast(pl.Float32), pl.col("volume").cast(pl.Int32)
        ).write_parquet(stored_file)

        state = _StoredStaleWarnState()
        with structlog.testing.capture_logs() as logs:
            served, from_stored = _read_intraday_bars(
                tmp_path,
                exchange="NSE",
                segment="INDEX",
                symbol="NIFTY",
                interval="5minute",
                day=_DAY,
                minutes=5,
                cutoff_min=None,
                stale_warn_state=state,
            )
        reference, _ = _read_intraday_bars(
            tmp_path,
            exchange="NSE",
            segment="INDEX",
            symbol="NIFTY",
            interval="5minute",
            day=_DAY,
            minutes=5,
            cutoff_min=None,
            prefer_resample=True,
        )
        assert from_stored is True  # dtype pun with equal values reads FRESH
        assert served, "the recast partition must still serve bars"
        assert [(b.close, b.volume) for b in served] == [
            (b.close, b.volume) for b in reference
        ]  # value-exactness: the served values ARE the reference-resample values
        assert not [e for e in logs if e["event"].startswith("movers.stored_stale")]


# ── month-edge: prior-UTC-month-filed bucket labels (REFUTER ROUND 2 B2) ──

#: Monday, a real NSE trading day, the 1st of an IST month — the ONLY day
#: shape on which a sub-05:30-IST bucket label crosses the UTC month FILE
#: boundary (DEC-MOVERS-STORED-FRESHNESS-001 round-2 fold).
_EDGE_DAY = date(2024, 7, 1)


def _seed_month_edge_lake(lake: Path) -> ParquetWriter:
    """The refuter-B2 month-edge producer chain, production writers only.

    Base 1m for IST day 2024-07-01 (1st of the month) holds a stray 09:00
    pre-open bar (the acknowledged legacy stored shape —
    DEC-DAILY-OPEN-PREDICATE-ALIGN-001's "stray pre-09:15 first bar") plus
    normal 09:15..09:19 session bars; the REAL prebuild materialises the
    derived TFs. The 09:00 bar's 4hour bucket label is 05:15 IST — the SAME
    IST day but 23:45 UTC of June 30 — so the prebuild itself files that
    bucket under ``4hour/2024/06.parquet`` (the PRIOR UTC month's file).
    """
    writer = ParquetWriter(lake_root=lake)
    bars = [_bar(day=_EDGE_DAY, hour=9, minute=0, close="20000", volume=7)] + [
        _bar(day=_EDGE_DAY, hour=9, minute=15 + i, close=str(20010 + i * 10), volume=100 + i)
        for i in range(5)
    ]
    writer.write(exchange="NSE", segment="INDEX", symbol="NIFTY", interval="1minute", bars=bars)
    result = materialize_lake_timeframes(
        lake_root=lake, exchange="NSE", segment="INDEX", symbol="NIFTY", kind=PrebuildKind.SPOT
    )
    assert result.total_bars > 0  # the prebuild really wrote the stored TFs.
    # The producer shape proof: the 4hour partition spans BOTH month files.
    fourh = lake / "bars" / "NSE" / "INDEX" / "NIFTY" / "4hour"
    files = sorted(p.relative_to(fourh).as_posix() for p in fourh.rglob("*.parquet"))
    assert files == ["2024/06.parquet", "2024/07.parquet"]
    return writer


class TestMonthEdgeBucketRouting:
    def test_fresh_month_edge_lake_serves_stored_complete_no_warn(self, tmp_path: Path) -> None:
        """attack_b2_monthedge port: a GENUINELY FRESH month-edge lake serves stored.

        Fails pre-fix TWICE over: (a) `read_stored_day_frame` (month-bounded
        only) missed the prior-month-filed 05:15-IST bucket — 1 row instead of
        2 (the pre-existing silent-drop read-completeness bug: pre-guard, on
        main, the bucket-MISSING frame was served silently); (b) the
        verify-equality guard then read the just-prebuilt partition as STALE —
        a permanent false-stale WARN per build that re-running the prebuild
        could never heal (the prebuild itself produces the routing).
        """
        from brutex.dashboard.movers_snapshot import _read_intraday_bars, _read_session_bars

        _seed_month_edge_lake(tmp_path)
        stored = read_stored_day_frame(
            tmp_path,
            exchange="NSE",
            segment="INDEX",
            symbol="NIFTY",
            interval="4hour",
            day=_EDGE_DAY,
        )
        assert stored is not None
        # BOTH buckets — the June-filed 05:15-IST label (23:45 UTC Jun 30) AND
        # the 09:15 session bucket (03:45 UTC Jul 1) — sorted, day-scoped.
        assert stored.get_column("ts_utc").to_list() == [
            datetime(2024, 6, 30, 23, 45, tzinfo=UTC),
            datetime(2024, 7, 1, 3, 45, tzinfo=UTC),
        ]
        bars, from_stored = _read_intraday_bars(
            tmp_path,
            exchange="NSE",
            segment="INDEX",
            symbol="NIFTY",
            interval="4hour",
            day=_EDGE_DAY,
            minutes=240,
            cutoff_min=None,
        )
        reference = _read_session_bars(
            tmp_path,
            exchange="NSE",
            segment="INDEX",
            symbol="NIFTY",
            day=_EDGE_DAY,
            minutes=240,
            cutoff_min=None,
        )
        assert from_stored is True
        assert bars == reference
        assert len(bars) == 2  # the complete frame — never the bucket-dropped 1.
        # Full compute path: stored provenance, snapshots == the resample
        # reference, ZERO stale warns on a genuinely fresh lake.
        with structlog.testing.capture_logs() as logs:
            cands = compute_spot_candidates(
                lake_root=tmp_path, interval="4hour", date=_EDGE_DAY.isoformat(), time=""
            )
            ref_cands = compute_spot_candidates(
                lake_root=tmp_path,
                interval="4hour",
                date=_EDGE_DAY.isoformat(),
                time="",
                prefer_resample=True,
            )
        assert (cands.stored_reads, cands.resampled_reads) == (1, 0)
        assert cands.snapshots == ref_cands.snapshots
        assert [e for e in logs if e["event"].startswith("movers.stored_stale")] == []

    def test_stale_prior_month_filed_bucket_still_caught(self, tmp_path: Path) -> None:
        """A genuinely STALE prior-month-filed bucket still reads stale post-widening.

        The 09:00 stray bar is healed AWAY from the 1-minute base (production
        ``heal_partition``) with NO re-prebuild: the stored 4hour partition
        still holds the June-filed 05:15-IST bucket the base no longer
        supports. The widened read SURFACES that bucket, the equality guard
        reads STALE, and the fallback serves the base-derived truth + ONE warn
        — the widening must never blind the guard to prior-month-filed
        staleness (pre-widening this shape compared two bucket-blind frames
        and served stored silently with no signal of the orphaned bucket).
        """
        writer = _seed_month_edge_lake(tmp_path)
        stray_ts = datetime(2024, 7, 1, 3, 30, tzinfo=UTC)  # 09:00 IST.
        dest = tmp_path / "bars" / "NSE" / "INDEX" / "NIFTY" / "1minute" / "2024" / "07.parquet"
        result = writer.heal_partition(
            dest, remove_timestamps={stray_ts}, quarantine_to=tmp_path / ".quarantine" / "edge"
        )
        assert result.dropped_removed == 1
        with structlog.testing.capture_logs() as logs:
            cands = compute_spot_candidates(
                lake_root=tmp_path, interval="4hour", date=_EDGE_DAY.isoformat(), time=""
            )
            ref_cands = compute_spot_candidates(
                lake_root=tmp_path,
                interval="4hour",
                date=_EDGE_DAY.isoformat(),
                time="",
                prefer_resample=True,
            )
        assert (cands.stored_reads, cands.resampled_reads) == (0, 1)
        assert cands.snapshots == ref_cands.snapshots  # base-derived truth served.
        stale_events = [e for e in logs if e["event"] == "movers.stored_stale_fallback"]
        assert len(stale_events) == 1
        assert (stale_events[0]["exchange"], stale_events[0]["symbol"]) == ("NSE", "NIFTY")

    def test_prior_month_probe_fires_only_on_qualifying_coordinates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The probe condition, spy-pinned: healthy-path read-count UNCHANGED.

        ``read_day_month_frame`` is spied in the ``movers_stored_read``
        namespace (the probe's OWN reference — the month read inside
        ``lake_day_glob.read_day_intraday_frame`` uses lake_day_glob's and is
        deliberately NOT counted): a non-1st day / the 1minute interval / a
        session-exempt symbol each fire ZERO probes; a 1st-of-month derived-TF
        session-filtered read fires EXACTLY ONE, aimed at the prior month —
        and degrades cleanly when the prior month's file is absent.
        """
        from brutex.dashboard import movers_stored_read as msr

        _seed_month_edge_lake(tmp_path)
        calls: list[date] = []
        real = msr.read_day_month_frame

        def spy(base: Path, day: date) -> pl.DataFrame | None:
            calls.append(day)
            return real(base, day)

        monkeypatch.setattr(msr, "read_day_month_frame", spy)

        def read(*, interval: str, day: date, symbol: str = "NIFTY") -> pl.DataFrame | None:
            return read_stored_day_frame(
                tmp_path,
                exchange="NSE",
                segment="INDEX",
                symbol=symbol,
                interval=interval,
                day=day,
            )

        # (a) non-1st day, derived TF — ZERO probes (the healthy path).
        assert read(interval="5minute", day=_DAY) is None  # no June session seeded.
        assert calls == []
        # (b) 1st of month, 1minute — ZERO probes (bar times never pre-05:30).
        frame_1m = read(interval="1minute", day=_EDGE_DAY)
        assert frame_1m is not None and frame_1m.height == 6
        assert calls == []
        # (c) 1st of month, derived TF, session-EXEMPT — ZERO probes (the
        # whole-partition read already spans every month file).
        assert read(interval="4hour", day=_EDGE_DAY, symbol="GIFTNIFTY") is None
        assert calls == []
        # (d) 1st of month, derived TF, session-filtered — EXACTLY ONE probe,
        # aimed at the prior month (2024-06-30 → 06.parquet), rows served.
        frame_4h = read(interval="4hour", day=_EDGE_DAY)
        assert frame_4h is not None and frame_4h.height == 2
        assert calls == [date(2024, 6, 30)]
        # (e) qualifying read whose PRIOR month file is ABSENT (2minute labels
        # never left July here) — one more probe, clean month-only serve.
        frame_2m = read(interval="2minute", day=_EDGE_DAY)
        assert frame_2m is not None and frame_2m.height > 0
        assert calls == [date(2024, 6, 30), date(2024, 6, 30)]

    def test_date_min_gates_probe_off_no_overflow(self, tmp_path: Path) -> None:
        """REFUTER ROUND 3 defect R3-N1: ``date.min`` must never overflow the probe.

        ``0001-01-01`` is the ONE well-formed ISO date where the probe body's
        ``day - timedelta(days=1)`` raises ``OverflowError`` — neither a
        ``PolarsError`` nor an ``OSError``, so pre-fix it ESCAPED the
        per-symbol ``movers_read_skip_errors()`` isolation and 500'd the
        route (pre-probe code returned ``None`` — the silent fallback).
        ``date.min`` has no prior day AT ALL, hence no prior-month file: the
        probe is definitionally inapplicable — the predicate reads False and
        the read serves an honest ``None`` with NO raise. Fails pre-fix
        (OverflowError at the read level) with the ``day > date.min`` gate
        reverted.
        """
        from brutex.dashboard.movers_stored_read import _prior_month_label_probe_applies

        # The predicate gates OFF at date.min on an otherwise-qualifying shape
        # (1st of a month × derived TF × session-filtered symbol)...
        assert not _prior_month_label_probe_applies(
            interval="5minute", day=date.min, symbol="NIFTY"
        )
        # ...while every OTHER 1st-of-month keeps qualifying (the gate is
        # exactly one date wide — year 1, month 2 already subtracts fine).
        assert _prior_month_label_probe_applies(
            interval="5minute", day=date(1, 2, 1), symbol="NIFTY"
        )
        # Read level: a real month-edge lake read at date.min is an honest
        # None (no bars can exist on a year-1 date), never an OverflowError.
        _seed_month_edge_lake(tmp_path)
        assert (
            read_stored_day_frame(
                tmp_path,
                exchange="NSE",
                segment="INDEX",
                symbol="NIFTY",
                interval="5minute",
                day=date.min,
            )
            is None
        )


# ── read_stored_day_frame edges ───────────────────────────────────────────


class TestReadStoredDayFrame:
    def test_absent_partition_is_none(self, tmp_path: Path) -> None:
        assert (
            read_stored_day_frame(
                tmp_path,
                exchange="NSE",
                segment="INDEX",
                symbol="NIFTY",
                interval="5minute",
                day=_DAY,
            )
            is None
        )

    def test_month_file_present_but_day_empty_is_none(self, tmp_path: Path) -> None:
        # The stored month file exists (the prebuild ran) but holds no bar on
        # the asked day → None (the caller falls back honestly).
        _seed_spot(tmp_path, "NIFTY", 20000)
        _materialize_spot(tmp_path, "NIFTY")
        assert (
            read_stored_day_frame(
                tmp_path,
                exchange="NSE",
                segment="INDEX",
                symbol="NIFTY",
                interval="5minute",
                day=date(2024, 6, 14),
            )
            is None
        )

    def test_day_rows_returned_sorted_and_shaped(self, tmp_path: Path) -> None:
        _seed_spot(tmp_path, "NIFTY", 20000)
        _materialize_spot(tmp_path, "NIFTY")
        frame = read_stored_day_frame(
            tmp_path,
            exchange="NSE",
            segment="INDEX",
            symbol="NIFTY",
            interval="5minute",
            day=_DAY,
        )
        assert frame is not None
        assert frame.columns == ["ts_utc", "open", "high", "low", "close", "volume"]
        ts = frame.get_column("ts_utc").to_list()
        assert ts == sorted(ts)
        assert len(ts) == 2  # 09:15 + 09:20 buckets of the 10-minute session.


# ── FUTURES + OPTIONS: materialisation invariance + prebuilt 1day baseline ─


def _seed_future(lake: Path) -> None:
    _write(
        lake, segment="FNO", symbol=_FUT, interval="1minute", bars=_session(_PRIOR, 20100, oi=500)
    )
    _write(lake, segment="FNO", symbol=_FUT, interval="1minute", bars=_session(_DAY, 20150, oi=600))
    # The underlying spot (premium column) — INDEX segment, 1-minute.
    _seed_spot(lake, "NIFTY", 20000)


def _seed_option(lake: Path) -> None:
    _write(lake, segment="FNO", symbol=_OPT, interval="1minute", bars=_session(_PRIOR, 150, oi=900))
    _write(lake, segment="FNO", symbol=_OPT, interval="1minute", bars=_session(_DAY, 180, oi=950))
    _seed_spot(lake, "NIFTY", 20000)


def _materialize_contract(lake: Path, symbol: str, kind: PrebuildKind) -> None:
    result = materialize_lake_timeframes(
        lake_root=lake,
        exchange="NSE",
        segment="FNO",
        symbol=symbol,
        kind=kind,
        include_daily=True,
        daily_before=_DAILY_BEFORE,
    )
    assert result.written.get("1day", 0) > 0  # the derived 1day landed on disk.


class TestFuturesOptionsMaterialisationInvariance:
    """The F&O session read is the LOCKED 1-minute as-of snapshot — the prebuilt
    TF/1day partitions must never change the candidates (the honest equality pin
    for the futures/options arm; see the module docstring for why a stored-TF
    session read is semantics-incompatible there)."""

    def test_futures_candidates_unchanged_by_prebuilt_partitions(self, tmp_path: Path) -> None:
        _seed_future(tmp_path)
        before = compute_futures_candidates(
            lake_root=tmp_path, interval="5minute", date=_DAY.isoformat(), time="09:20"
        )
        assert len(before.movers) == 1
        _materialize_contract(tmp_path, _FUT, PrebuildKind.FUTURES)
        after = compute_futures_candidates(
            lake_root=tmp_path, interval="5minute", date=_DAY.isoformat(), time="09:20"
        )
        assert after.movers == before.movers  # byte-identical rows
        assert after.skipped == before.skipped

    def test_options_candidates_unchanged_by_prebuilt_partitions(self, tmp_path: Path) -> None:
        _seed_option(tmp_path)
        before = compute_options_candidates(
            lake_root=tmp_path, interval="5minute", date=_DAY.isoformat(), time="09:20"
        )
        assert len(before.movers) == 1
        _materialize_contract(tmp_path, _OPT, PrebuildKind.OPTIONS)
        after = compute_options_candidates(
            lake_root=tmp_path, interval="5minute", date=_DAY.isoformat(), time="09:20"
        )
        assert after.movers == before.movers
        assert after.skipped == before.skipped

    def test_futures_prev_day_baseline_served_from_prebuilt_1day(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AFTER the prebuild, the F&O prior-day close/OI baselines read the
        derived ``1day`` partition — the bounded 1-minute walk-back fallback is
        never touched (this is HOW the F&O movers read the prebuilt partitions)."""
        from brutex.dashboard import movers_futures, movers_snapshot

        _seed_future(tmp_path)
        _materialize_contract(tmp_path, _FUT, PrebuildKind.FUTURES)
        # ALSO prebuild the underlying spot's 1day (the premium leg's baseline).
        materialize_lake_timeframes(
            lake_root=tmp_path,
            exchange="NSE",
            segment="INDEX",
            symbol="NIFTY",
            kind=PrebuildKind.SPOT,
            include_daily=True,
            daily_before=_DAILY_BEFORE,
        )

        def _must_not_walk_back(*args: object, **kwargs: object) -> None:
            raise AssertionError(
                "the 1-minute walk-back fallback must not run once the prebuild "
                "has written the derived 1day partition"
            )

        # Patch BOTH bound names (movers_futures imported the helper by value).
        # Since DEC-MOVERS-PREVCLOSE-CONTIGUITY-001 the baselines resolve through
        # movers_snapshot.resolve_prev_close / movers_futures.resolve_prev_oi —
        # each resolver's ONLY fallback is this walk-back helper (looked up at
        # call time from its module globals), so patching the two module names
        # still intercepts every fallback beneath the new resolver level and the
        # 1day-first / fallback-only-when-no-1day law keeps its teeth.
        monkeypatch.setattr(movers_snapshot, "_prior_session_last_from_minute", _must_not_walk_back)
        monkeypatch.setattr(movers_futures, "_prior_session_last_from_minute", _must_not_walk_back)
        cands = compute_futures_candidates(
            lake_root=tmp_path, interval="5minute", date=_DAY.isoformat(), time="09:20"
        )
        assert len(cands.movers) == 1
        mover = cands.movers[0]
        # The baselines resolved from the STORED derived 1day (prior session's
        # last 1-minute close / OI, byte-equal to the walk-back it replaced).
        assert mover.pct_change is not None
        assert mover.oi_chg_pct is not None
