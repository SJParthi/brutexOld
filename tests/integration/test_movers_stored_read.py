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

import pytest

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
        # RE-SEEDED for DEC-MOVERS-STORED-FRESHNESS-001: the hand-written 1day
        # bar now carries the DERIVE-CONSISTENT values of the seeded 1-minute
        # session (base 20005: open = first open 20004, high = max close+2 =
        # 20097, low = min close−3 = 20002, close = 20095, volume = Σ100..109 =
        # 1045), so this pin keeps pinning "consistent daily → STORED (1, 0)";
        # the OLD inconsistent bar moved to the stale sibling below.
        _seed_spot(tmp_path, "NIFTY", 20000)
        _write(
            tmp_path,
            segment="INDEX",
            symbol="NIFTY",
            interval="1day",
            bars=[
                CandleBar(
                    timestamp=datetime(_DAY.year, _DAY.month, _DAY.day, 3, 45, tzinfo=UTC),
                    open=Decimal("20004"),
                    high=Decimal("20097"),
                    low=Decimal("20002"),
                    close=Decimal("20095"),
                    volume=1045,
                    open_interest=None,
                )
            ],
        )
        cands = compute_spot_candidates(
            lake_root=tmp_path, interval="1day", date=_DAY.isoformat(), time=None
        )
        assert len(cands.snapshots) == 1
        assert (cands.stored_reads, cands.resampled_reads) == (1, 0)

    def test_spot_daily_stale_bar_falls_back_to_derive(self, tmp_path: Path) -> None:
        """DEC-MOVERS-STORED-FRESHNESS-001, the 1day arm: a stored 1day bar that
        provably drifts from the day's raw 1-minute session (the OLD
        inconsistent fixture values of the pin above) is NOT served — the
        canonical ``resample_1m_to_daily`` re-derive serves instead (counted
        ``resampled_reads``, one ``movers.stored_stale_fallback`` WARN with
        ``interval="1day"``) and the daily LTP converges onto the 1-minute
        truth (the 21700-vs-21750.55 repro convergence shape)."""
        from structlog.testing import capture_logs

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
                    close=Decimal("20095"),  # close matches; open/high/low/volume drift
                    volume=5000,
                    open_interest=None,
                )
            ],
        )
        with capture_logs() as logs:
            cands = compute_spot_candidates(
                lake_root=tmp_path, interval="1day", date=_DAY.isoformat(), time=None
            )
        assert len(cands.snapshots) == 1
        assert (cands.stored_reads, cands.resampled_reads) == (0, 1)
        snap = cands.snapshots[0]
        # The derived bar serves: close matched (ltp 20095) but volume is now
        # the TRUE session sum (Σ100..109 = 1045), not the stale 5000.
        assert snap.ltp == Decimal("20095")
        assert snap.volume == 1045
        # Convergence: the daily LTP equals the 1minute-interval path's LTP.
        minute = compute_spot_candidates(
            lake_root=tmp_path, interval="1minute", date=_DAY.isoformat(), time=None
        )
        assert snap.ltp == minute.snapshots[0].ltp
        warns = [e for e in logs if e["event"] == "movers.stored_stale_fallback"]
        assert len(warns) == 1
        assert warns[0]["symbol"] == "NIFTY"
        assert warns[0]["interval"] == "1day"

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
        """CONSCIOUSLY FLIPPED default arm (DEC-MOVERS-STORED-FRESHNESS-001;
        the prior DEC-MOVERS-GATE-PREBUILD-SCOPE-001 form of this pin ACCEPTED
        the default read serving the POISONED stale close — ``(1, 0)`` +
        ``snapshots != reference``): the always-on freshness guard now judges
        the stored day slice against the raw-1m base, so the DEFAULT read also
        falls back to the fresh resample (byte-identical to the reference) with
        exactly ONE ``movers.stored_stale_fallback`` WARN. The
        ``prefer_resample=True`` arm is retained byte-unchanged — it bypasses
        the stored partition WITHOUT even paying the compare (spy-pinned)."""
        from structlog.testing import capture_logs

        _seed_spot(tmp_path, "NIFTY", 20000)
        # The truth: what the fresh 1-minute data resamples to (no stored TF yet).
        reference = compute_spot_candidates(
            lake_root=tmp_path, interval="5minute", date=_DAY.isoformat(), time="09:19"
        )
        assert (reference.stored_reads, reference.resampled_reads) == (0, 1)
        # A STALE stored partition lands (prior pull's prebuild, poisoned close).
        _poison_stored_5minute(tmp_path)
        with capture_logs() as logs:
            stored_first = compute_spot_candidates(
                lake_root=tmp_path, interval="5minute", date=_DAY.isoformat(), time="09:19"
            )
        # THE FLIP: the guard catches the drift → the fallback serves the fresh
        # resample byte-identically; the poisoned 99999 close is no longer visible.
        assert (stored_first.stored_reads, stored_first.resampled_reads) == (0, 1)
        assert stored_first.snapshots == reference.snapshots
        stale_warns = [e for e in logs if e["event"] == "movers.stored_stale_fallback"]
        assert len(stale_warns) == 1
        assert stale_warns[0]["symbol"] == "NIFTY"
        assert stale_warns[0]["interval"] == "5minute"
        # The pull-active switch: the resample wins — fresh, byte-identical to
        # truth — and the compare is never even paid (the zero-cost mid-pull arm).
        import brutex.dashboard.movers_snapshot as ms

        compare_calls: list[str] = []
        real_is_stale = ms.stored_day_frame_is_stale

        def _spy_is_stale(*args: object, **kwargs: object) -> bool:
            compare_calls.append("called")
            return real_is_stale(*args, **kwargs)  # type: ignore[arg-type]

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(ms, "stored_day_frame_is_stale", _spy_is_stale)
            fresh = compute_spot_candidates(
                lake_root=tmp_path,
                interval="5minute",
                date=_DAY.isoformat(),
                time="09:19",
                prefer_resample=True,
            )
        assert (fresh.stored_reads, fresh.resampled_reads) == (0, 1)
        assert fresh.snapshots == reference.snapshots
        assert compare_calls == []  # prefer_resample never pays the freshness compare

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
        # stored-first behaviour is byte-identical when the switch is off (a
        # FRESH prebuilt lake: the freshness guard verifies + agrees).
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


# ── the stored-read freshness guard (DEC-MOVERS-STORED-FRESHNESS-001) ─────


def _stored_5m_bar(
    *, minute: int, open_: str, high: str, low: str, close: str, volume: int
) -> CandleBar:
    """A stored 5-minute bucket bar at ``09:<minute>`` IST on ``_DAY``."""
    total = 9 * 60 + minute - (5 * 60 + 30)
    return CandleBar(
        timestamp=datetime(_DAY.year, _DAY.month, _DAY.day, total // 60, total % 60, tzinfo=UTC),
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=volume,
        open_interest=None,
    )


class TestStoredFreshnessGuard:
    """The always-on stored-vs-base freshness verdict + the honest fallback."""

    def test_cli_fetch_after_prebuild_converges_onto_the_1m_truth(self, tmp_path: Path) -> None:
        """N1 — the realistic stale producer: a prebuild materialises the TF
        partitions, then a CLI ``brutex fetch`` APPENDS later 1-minute bars (the
        CLI runs NO prebuild — #1145), leaving the stored TFs provably stale.
        Pre-fix repro shape: 1minute=21374.00 vs 3minute=21365.00 (stale
        stored) vs 5minute=21374.00 (resample fallback) — the default Latest
        view showed DIFFERENT LTPs per timeframe. Post-fix: the guard reroutes
        every drifted TF to the fresh resample, so 1m/3m/5m LTPs all agree and
        the payload equals the forced-resample reference byte-for-byte."""
        from structlog.testing import capture_logs

        _seed_spot(tmp_path, "NIFTY", 20000)
        _materialize_spot(tmp_path, "NIFTY")
        # The CLI fetch lands three later 1-minute bars; no prebuild follows.
        _write(
            tmp_path,
            segment="INDEX",
            symbol="NIFTY",
            interval="1minute",
            bars=[
                _bar(day=_DAY, hour=9, minute=25 + i, close=str(20105 + i * 10), volume=110 + i)
                for i in range(3)
            ],
        )
        reference = compute_spot_candidates(
            lake_root=tmp_path,
            interval="3minute",
            date=_DAY.isoformat(),
            time=None,
            prefer_resample=True,
        )
        with capture_logs() as logs:
            guarded = compute_spot_candidates(
                lake_root=tmp_path, interval="3minute", date=_DAY.isoformat(), time=None
            )
        assert guarded.snapshots == reference.snapshots  # byte-equal to the 1m truth
        assert (guarded.stored_reads, guarded.resampled_reads) == (0, 1)
        warns = [e for e in logs if e["event"] == "movers.stored_stale_fallback"]
        assert len(warns) == 1
        assert warns[0]["interval"] == "3minute"
        # Convergence across timeframes: the default Latest LTP is ONE value.
        ltps = {
            compute_spot_candidates(
                lake_root=tmp_path, interval=tf, date=_DAY.isoformat(), time=None
            )
            .snapshots[0]
            .ltp
            for tf in ("1minute", "3minute", "5minute")
        }
        assert ltps == {Decimal("20125")}

    def test_cli_new_day_self_heals_via_absent_day_fallback(self, tmp_path: Path) -> None:
        """A1 — the audited SAFE case pinned (reachable-set honesty, fold-in
        A): a CLI ``brutex fetch`` landing a whole NEW day's 1-minute bars
        (the stored TF partitions end at the prebuild's last day) is NOT a
        stale-serve state — the stored read finds no rows for the new day and
        the pre-existing absent-day fallback serves the fresh 1-minute
        resample, with ZERO ``movers.stored_stale_fallback`` events (a
        self-heal, never the warned stale path)."""
        from structlog.testing import capture_logs

        new_day = date(2024, 6, 14)  # the next NSE session after _DAY (a Friday)
        _seed_spot(tmp_path, "NIFTY", 20000)
        _materialize_spot(tmp_path, "NIFTY")  # prebuild covers only up to _DAY
        # The CLI fetch lands the NEW day's session; no prebuild follows (#1145).
        _write(
            tmp_path,
            segment="INDEX",
            symbol="NIFTY",
            interval="1minute",
            bars=_session(new_day, 21000),
        )
        with capture_logs() as logs:
            cands = compute_spot_candidates(
                lake_root=tmp_path, interval="5minute", date=new_day.isoformat(), time=None
            )
            base = compute_spot_candidates(
                lake_root=tmp_path, interval="1minute", date=new_day.isoformat(), time=None
            )
        assert len(cands.snapshots) == 1
        assert (cands.stored_reads, cands.resampled_reads) == (0, 1)  # the honest fallback
        # Fresh new-day bytes — the 5minute LTP equals the 1minute LTP (one truth).
        assert cands.snapshots[0].ltp == base.snapshots[0].ltp == Decimal("21090")
        assert [e for e in logs if e["event"] == "movers.stored_stale_fallback"] == []

    def test_value_drift_same_shape_is_caught(self, tmp_path: Path) -> None:
        """N2 — a stored partition with the CORRECT bucket timestamps + height
        but ONE drifted close still falls back (full-equality teeth beyond the
        row-count / grid checks)."""
        from structlog.testing import capture_logs

        _seed_spot(tmp_path, "NIFTY", 20000)
        # The two true 5-minute buckets of the seeded session (base 20005),
        # except bucket 2's close drifts 20095 → 20090.
        _write(
            tmp_path,
            segment="INDEX",
            symbol="NIFTY",
            interval="5minute",
            bars=[
                _stored_5m_bar(
                    minute=15, open_="20004", high="20047", low="20002", close="20045", volume=510
                ),
                _stored_5m_bar(
                    minute=20, open_="20054", high="20097", low="20052", close="20090", volume=535
                ),
            ],
        )
        with capture_logs() as logs:
            cands = compute_spot_candidates(
                lake_root=tmp_path, interval="5minute", date=_DAY.isoformat(), time=None
            )
        assert (cands.stored_reads, cands.resampled_reads) == (0, 1)
        assert cands.snapshots[0].ltp == Decimal("20095")  # the 1m truth, not 20090
        assert [e for e in logs if e["event"] == "movers.stored_stale_fallback"]

    def test_fresh_lake_is_a_byte_identical_no_op_with_zero_events(self, tmp_path: Path) -> None:
        """N3 — the steady-state no-op teeth: on a REAL prebuilt (fresh) lake
        the guarded payloads byte-equal the forced-resample reference, ZERO
        ``movers.stored_stale_fallback`` events fire across a default-grid
        sweep, and each contributing intraday candidate pays exactly ONE
        ``_read_cut_1m_frame`` + ONE ``read_stored_day_frame`` call (the
        no-double-read tooth — the #1538 fallback double-read collapsed)."""
        import brutex.dashboard.movers_snapshot as ms
        from structlog.testing import capture_logs

        _seed_spot(tmp_path, "NIFTY", 20000)
        _seed_spot(tmp_path, "BANKNIFTY", 45000)
        _materialize_spot(tmp_path, "NIFTY")
        _materialize_spot(tmp_path, "BANKNIFTY")
        with capture_logs() as logs:
            for tf in ("1minute", "3minute", "5minute", "15minute"):
                reference = compute_spot_candidates(
                    lake_root=tmp_path,
                    interval=tf,
                    date=_DAY.isoformat(),
                    time=None,
                    prefer_resample=True,
                )
                guarded = compute_spot_candidates(
                    lake_root=tmp_path, interval=tf, date=_DAY.isoformat(), time=None
                )
                assert guarded.snapshots == reference.snapshots, tf
                assert (guarded.stored_reads, guarded.resampled_reads) == (2, 0), tf
        assert [e for e in logs if e["event"] == "movers.stored_stale_fallback"] == []
        # The read-count tooth: ONE base read + ONE stored read per candidate.
        cut_calls: list[str] = []
        stored_calls: list[str] = []
        real_cut = ms._read_cut_1m_frame
        real_stored = ms.read_stored_day_frame

        def _count_cut(*args: object, **kwargs: object) -> object:
            cut_calls.append(str(kwargs.get("symbol")))
            return real_cut(*args, **kwargs)  # type: ignore[arg-type]

        def _count_stored(*args: object, **kwargs: object) -> object:
            stored_calls.append(str(kwargs.get("symbol")))
            return real_stored(*args, **kwargs)  # type: ignore[arg-type]

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(ms, "_read_cut_1m_frame", _count_cut)
            mp.setattr(ms, "read_stored_day_frame", _count_stored)
            cands = compute_spot_candidates(
                lake_root=tmp_path, interval="5minute", date=_DAY.isoformat(), time=None
            )
        assert len(cands.snapshots) == 2
        assert sorted(cut_calls) == ["BANKNIFTY", "NIFTY"]
        assert sorted(stored_calls) == ["BANKNIFTY", "NIFTY"]

    def test_abstain_direct_no_base_frame_serves_stored(self, tmp_path: Path) -> None:
        """N4a — a caller-proven-absent base (``base_frame=None``) with stored
        rows present serves the stored bytes (the honest abstain: the verdict
        is unknowable without the 1-minute base) with NO stale event."""
        from brutex.dashboard.movers_snapshot import _read_intraday_bars
        from structlog.testing import capture_logs

        _seed_spot(tmp_path, "NIFTY", 20000)
        _materialize_spot(tmp_path, "NIFTY")
        with capture_logs() as logs:
            bars, from_stored = _read_intraday_bars(
                tmp_path,
                exchange="NSE",
                segment="INDEX",
                symbol="NIFTY",
                interval="5minute",
                day=_DAY,
                minutes=5,
                cutoff_min=None,
                base_frame=None,
            )
        assert from_stored is True
        assert len(bars) == 2  # the stored 09:15 + 09:20 buckets served as-is
        assert [e for e in logs if e["event"] == "movers.stored_stale_fallback"] == []

    def test_abstain_route_level_when_1minute_month_absent(self, tmp_path: Path) -> None:
        """N4b — stored TF day rows present but NO 1-minute month file for the
        day (the base month never pulled; discovery stays alive off a prior
        month's 1-minute): the compute-level read abstains to the stored bytes
        (stored_reads == 1) with no stale event — a stored-TF lake with no 1m
        base for the day still serves."""
        from structlog.testing import capture_logs

        # 1-minute exists only in a PRIOR month (keeps the symbol discovered);
        # the _DAY (June) month file is absent → the base read returns None.
        _write(
            tmp_path,
            segment="INDEX",
            symbol="NIFTY",
            interval="1minute",
            bars=_session(date(2024, 5, 15), 20000),
        )
        _write(
            tmp_path,
            segment="INDEX",
            symbol="NIFTY",
            interval="5minute",
            bars=[
                _stored_5m_bar(
                    minute=15, open_="20004", high="20047", low="20002", close="20045", volume=510
                ),
            ],
        )
        with capture_logs() as logs:
            cands = compute_spot_candidates(
                lake_root=tmp_path, interval="5minute", date=_DAY.isoformat(), time=None
            )
        assert len(cands.snapshots) == 1
        assert (cands.stored_reads, cands.resampled_reads) == (1, 0)
        assert cands.snapshots[0].ltp == Decimal("20045")  # the stored bytes
        assert [e for e in logs if e["event"] == "movers.stored_stale_fallback"] == []

    def test_abstain_daily_when_no_1minute_base(self, tmp_path: Path) -> None:
        """N4c — a 1day-only lake (no 1-minute at all): the daily verdict
        abstains and the stored 1day bar serves unchanged (the contiguity
        goldens' lake shape — pre-fix bytes)."""
        from structlog.testing import capture_logs

        close = Decimal("20095")
        _write(
            tmp_path,
            segment="INDEX",
            symbol="NIFTY",
            interval="1day",
            bars=[
                CandleBar(
                    timestamp=datetime(_DAY.year, _DAY.month, _DAY.day, 3, 45, tzinfo=UTC),
                    open=close - 10,
                    high=close,
                    low=close - 20,
                    close=close,
                    volume=1000,
                    open_interest=None,
                )
            ],
        )
        with capture_logs() as logs:
            cands = compute_spot_candidates(
                lake_root=tmp_path, interval="1day", date=_DAY.isoformat(), time=None
            )
        assert len(cands.snapshots) == 1
        assert (cands.stored_reads, cands.resampled_reads) == (1, 0)
        assert cands.snapshots[0].ltp == close
        assert [e for e in logs if e["event"] == "movers.stored_stale_fallback"] == []

    def test_daily_stale_repro_converges_route_shaped(self, tmp_path: Path) -> None:
        """N5 — the 1day repro numbers (1day=21700.00 vs 1minute=21750.55):
        the payload-level daily read now serves the canonical re-derive —
        LTP ₹21,750.55 — counted resampled, ONE warn with interval='1day'."""
        from brutex.dashboard.movers_snapshot import run_movers_snapshot
        from structlog.testing import capture_logs

        _write(
            tmp_path,
            segment="INDEX",
            symbol="NIFTY",
            interval="1minute",
            bars=[
                _bar(day=_DAY, hour=9, minute=15, close="21740", volume=100),
                _bar(day=_DAY, hour=9, minute=16, close="21750.55", volume=200),
            ],
        )
        _write(
            tmp_path,
            segment="INDEX",
            symbol="NIFTY",
            interval="1day",
            bars=[
                CandleBar(
                    timestamp=datetime(_DAY.year, _DAY.month, _DAY.day, 3, 45, tzinfo=UTC),
                    open=Decimal("21700"),
                    high=Decimal("21700"),
                    low=Decimal("21600"),
                    close=Decimal("21700"),  # the stale repro close
                    volume=999,
                    open_interest=None,
                )
            ],
        )
        with capture_logs() as logs:
            payload = run_movers_snapshot(
                lake_root=tmp_path, interval="1day", date=_DAY.isoformat(), time=None, top_n=5
            )
        (row,) = payload["volume"]
        assert row["ltp"] == "₹21,750.55"  # the 1-minute truth, not the stale 21700
        assert (payload["stored_reads"], payload["resampled_reads"]) == (0, 1)
        warns = [e for e in logs if e["event"] == "movers.stored_stale_fallback"]
        assert len(warns) == 1
        assert warns[0]["interval"] == "1day"

    def test_daily_derive_abstains_at_date_max(self, tmp_path: Path) -> None:
        """The ``date.max`` saturation: a degenerate ``?date=9999-12-31`` daily
        read abstains (no derive possible without overflowing) and serves the
        stored bar — never an ``OverflowError``, never a warn. The lake writer
        itself refuses year 9999 (``MAX_PARTITION_YEAR``), so the fixture
        hand-writes the parquet files (the raw-write idiom of
        ``test_movers_resilient_poison_skip``) — the READ side must stay
        graceful on such a lake regardless."""
        import pyarrow as pa
        import pyarrow.parquet as pq
        from structlog.testing import capture_logs

        last = date(9999, 12, 31)

        def _raw_write(interval: str, stamps: list[datetime], closes: list[float]) -> None:
            dest = tmp_path / "bars" / "NSE" / "INDEX" / "NIFTY" / interval / "9999"
            dest.mkdir(parents=True, exist_ok=True)
            table = pa.table(
                {
                    "timestamp": pa.array(stamps, type=pa.timestamp("us", tz="UTC")),
                    "open": pa.array(closes, type=pa.float64()),
                    "high": pa.array(closes, type=pa.float64()),
                    "low": pa.array(closes, type=pa.float64()),
                    "close": pa.array(closes, type=pa.float64()),
                    "volume": pa.array([10] * len(stamps), type=pa.int64()),
                    "open_interest": pa.array([None] * len(stamps), type=pa.int64()),
                }
            )
            pq.write_table(table, dest / "12.parquet")

        # A 09:15-IST 1-minute bar + a DRIFTING stored 1day bar on 9999-12-31.
        _raw_write("1minute", [datetime(9999, 12, 31, 3, 45, tzinfo=UTC)], [100.0])
        _raw_write("1day", [datetime(9999, 12, 31, 3, 45, tzinfo=UTC)], [105.0])
        with capture_logs() as logs:
            cands = compute_spot_candidates(
                lake_root=tmp_path, interval="1day", date=last.isoformat(), time=None
            )
        assert len(cands.snapshots) == 1
        assert (cands.stored_reads, cands.resampled_reads) == (1, 0)
        assert cands.snapshots[0].ltp == Decimal("105")  # served despite the 1m drift
        assert [e for e in logs if e["event"] == "movers.stored_stale_fallback"] == []

    def test_legacy_epoch_anchored_grid_is_stale(self, tmp_path: Path) -> None:
        """N6 — a pre-DEC-RESAMPLE-ANCHOR-002 stored grid (buckets off the
        09:15 session anchor) is timestamp-drift and falls back."""
        from structlog.testing import capture_logs

        _seed_spot(tmp_path, "NIFTY", 20000)
        # Same aggregates as the true buckets but on a 09:16-anchored grid.
        _write(
            tmp_path,
            segment="INDEX",
            symbol="NIFTY",
            interval="5minute",
            bars=[
                _stored_5m_bar(
                    minute=16, open_="20004", high="20047", low="20002", close="20045", volume=510
                ),
                _stored_5m_bar(
                    minute=21, open_="20054", high="20097", low="20052", close="20095", volume=535
                ),
            ],
        )
        with capture_logs() as logs:
            cands = compute_spot_candidates(
                lake_root=tmp_path, interval="5minute", date=_DAY.isoformat(), time=None
            )
        assert (cands.stored_reads, cands.resampled_reads) == (0, 1)
        assert [e for e in logs if e["event"] == "movers.stored_stale_fallback"]

    def test_warn_cap_and_suppressed_summary_over_21_stale_symbols(self, tmp_path: Path) -> None:
        """N7 — a whole-lake stale prebuild: >20 stale symbols emit exactly 20
        ``movers.stored_stale_fallback`` + ONE ``..._suppressed`` summary."""
        from structlog.testing import capture_logs

        for i in range(21):
            symbol = f"SYM{i:02d}"
            _seed_spot(tmp_path, symbol, 1000 + i * 100)
            _poison_stored_5minute(tmp_path, symbol)
        with capture_logs() as logs:
            cands = compute_spot_candidates(
                lake_root=tmp_path, interval="5minute", date=_DAY.isoformat(), time=None
            )
        assert len(cands.snapshots) == 21
        assert (cands.stored_reads, cands.resampled_reads) == (0, 21)
        warns = [e for e in logs if e["event"] == "movers.stored_stale_fallback"]
        suppressed = [e for e in logs if e["event"] == "movers.stored_stale_fallback_suppressed"]
        assert len(warns) == 20
        assert len(suppressed) == 1
        assert suppressed[0]["cap"] == 20

    def test_direct_call_without_stale_state_falls_back_silently(self, tmp_path: Path) -> None:
        """N7b — the ``stale_state=None`` (direct/unit caller) arm: the stale
        fallback still reroutes, with NO log emitted."""
        from brutex.dashboard.movers_snapshot import _read_intraday_bars
        from structlog.testing import capture_logs

        _seed_spot(tmp_path, "NIFTY", 20000)
        _poison_stored_5minute(tmp_path)
        with capture_logs() as logs:
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
        assert from_stored is False
        assert bars and bars[-1].close == Decimal("20095")  # the 1m truth
        assert [e for e in logs if e["event"].startswith("movers.stored_stale")] == []

    def test_sentinel_resolves_base_at_most_once(self, tmp_path: Path) -> None:
        """N8 — the ``...`` legacy-caller sentinel resolves the base lazily AT
        MOST ONCE per call, on both the stale-compare and the fallback arm."""
        import brutex.dashboard.movers_snapshot as ms
        from brutex.dashboard.movers_snapshot import _read_intraday_bars

        _seed_spot(tmp_path, "NIFTY", 20000)
        _poison_stored_5minute(tmp_path)
        calls: list[str] = []
        real_cut = ms._read_cut_1m_frame

        def _count(*args: object, **kwargs: object) -> object:
            calls.append(str(kwargs.get("symbol")))
            return real_cut(*args, **kwargs)  # type: ignore[arg-type]

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(ms, "_read_cut_1m_frame", _count)
            # Stored present + stale: resolved once for the compare, REUSED by
            # the fallback resample.
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
            assert from_stored is False and bars
            assert calls == ["NIFTY"]
            # Stored absent (a different interval): resolved once on the
            # fallback arm only.
            calls.clear()
            bars2, from_stored2 = _read_intraday_bars(
                tmp_path,
                exchange="NSE",
                segment="INDEX",
                symbol="NIFTY",
                interval="15minute",
                day=_DAY,
                minutes=15,
                cutoff_min=None,
            )
            assert from_stored2 is False and bars2
            assert calls == ["NIFTY"]

    def test_given_frame_and_none_pay_zero_reads(self, tmp_path: Path) -> None:
        """N8b — a threading caller's frame (or proven-``None``) is used
        verbatim: ``_read_cut_1m_frame`` is NEVER called inside the read."""
        import brutex.dashboard.movers_snapshot as ms
        from brutex.dashboard.movers_snapshot import _read_cut_1m_frame, _read_intraday_bars

        _seed_spot(tmp_path, "NIFTY", 20000)
        _poison_stored_5minute(tmp_path)
        frame = _read_cut_1m_frame(
            tmp_path, exchange="NSE", segment="INDEX", symbol="NIFTY", day=_DAY, cutoff_min=None
        )
        assert frame is not None

        def _must_not_read(*args: object, **kwargs: object) -> object:
            raise AssertionError("a threading caller's base frame must be reused, never re-read")

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(ms, "_read_cut_1m_frame", _must_not_read)
            # A frame + a stale stored partition → fallback resamples the frame.
            bars, from_stored = _read_intraday_bars(
                tmp_path,
                exchange="NSE",
                segment="INDEX",
                symbol="NIFTY",
                interval="5minute",
                day=_DAY,
                minutes=5,
                cutoff_min=None,
                base_frame=frame,
            )
            assert from_stored is False
            assert bars and bars[-1].close == Decimal("20095")
            # prefer_resample + a frame → the resample of the frame, zero reads.
            bars_pr, from_stored_pr = _read_intraday_bars(
                tmp_path,
                exchange="NSE",
                segment="INDEX",
                symbol="NIFTY",
                interval="5minute",
                day=_DAY,
                minutes=5,
                cutoff_min=None,
                prefer_resample=True,
                base_frame=frame,
            )
            assert from_stored_pr is False
            assert bars_pr == bars
            # A proven-None base with NO stored partition → honest empty.
            bars_none, from_stored_none = _read_intraday_bars(
                tmp_path,
                exchange="NSE",
                segment="INDEX",
                symbol="NIFTY",
                interval="15minute",
                day=_DAY,
                minutes=15,
                cutoff_min=None,
                base_frame=None,
            )
            assert (bars_none, from_stored_none) == ([], False)


# ── fold-in C: missing-1day-bar coherence (DEC-MOVERS-STORED-FRESHNESS-001) ─


class TestDailyMissingBarCoherence:
    """A day with REAL 1-minute but NO stored ``1day`` bar serves the canonical
    re-derive (silent, counted ``resampled_reads``) — the same evidence the
    drift arm already serves must not VANISH when the stored bar is merely
    absent (the mid-pull lifecycle state: ``1day`` only materialises at the
    pull-END prebuild)."""

    def test_daily_missing_bar_derives_from_1m_silently(self, tmp_path: Path) -> None:
        """C5 — the coherence arm: `_seed_spot` stores a 1day bar ONLY for the
        PRIOR day; the selected day has a full 1-minute session and no stored
        bar. The snapshot is PRESENT (previously: silently skipped), its bar is
        the canonical ``resample_1m_to_daily`` of the day's real 1m (converges
        with the 1minute tab), counted ``resampled_reads``, with NO
        ``movers.stored_stale_fallback`` event (the silent-arm pin), the
        TF-invariant value coherent, and the prev-close baseline resolved from
        the stored PRIOR 1day bar — calendar-contiguous (Wed → Thu)."""
        from structlog.testing import capture_logs

        _seed_spot(tmp_path, "NIFTY", 20000)  # 1day bar for _PRIOR only; 1m both days
        with capture_logs() as logs:
            cands = compute_spot_candidates(
                lake_root=tmp_path, interval="1day", date=_DAY.isoformat(), time=None
            )
        assert len(cands.snapshots) == 1
        assert (cands.stored_reads, cands.resampled_reads) == (0, 1)
        snap = cands.snapshots[0]
        # The derived day-so-far bar of the seeded session (closes 20005..20095,
        # volumes 100..109) — byte-what-a-fresh-prebuild-would-write.
        assert snap.ltp == Decimal("20095")
        assert snap.volume == 1045
        assert snap.bar_count == 1
        # Convergence + value coherence with the 1minute tab on the SAME lake.
        minute = compute_spot_candidates(
            lake_root=tmp_path, interval="1minute", date=_DAY.isoformat(), time=None
        )
        assert snap.ltp == minute.snapshots[0].ltp
        assert snap.value_paisa == minute.snapshots[0].value_paisa
        # Prev-close baseline from the stored PRIOR 1day bar (base+90 = 20090),
        # calendar-contiguous (2024-06-12 Wed → 2024-06-13 Thu) → no stamp.
        assert snap.prev_close == Decimal("20090")
        assert snap.prev_close_contiguous is not False
        # SILENT by design: absent-bar derive is the normal mid-pull state.
        assert [e for e in logs if e["event"] == "movers.stored_stale_fallback"] == []

    def test_daily_no_1m_and_no_bar_stays_honest_skip(self, tmp_path: Path) -> None:
        # A date with NEITHER a stored 1day bar NOR 1-minute (weekend/holiday/
        # never-pulled) stays the honest skip — nothing is fabricated.
        _seed_spot(tmp_path, "NIFTY", 20000)
        cands = compute_spot_candidates(
            lake_root=tmp_path,
            interval="1day",
            date="2024-06-15",
            time=None,  # Saturday
        )
        assert cands.snapshots == ()
        assert cands.skipped == 1
        assert (cands.stored_reads, cands.resampled_reads) == (0, 0)

    def test_daily_missing_bar_at_date_max_abstains(self, tmp_path: Path) -> None:
        """The ``date.max`` saturation on the NEW arm: a missing stored bar +
        real 1m ON ``9999-12-31`` cannot derive (``_derive_daily_bars``
        abstains rather than overflow) → the honest skip, never an
        ``OverflowError``, never a warn. Raw-write fixture — the lake writer
        itself refuses year 9999 (``MAX_PARTITION_YEAR``)."""
        import pyarrow as pa
        import pyarrow.parquet as pq
        from structlog.testing import capture_logs

        last = date(9999, 12, 31)

        def _raw_write(interval: str, stamps: list[datetime], closes: list[float]) -> None:
            dest = tmp_path / "bars" / "NSE" / "INDEX" / "NIFTY" / interval / "9999"
            dest.mkdir(parents=True, exist_ok=True)
            table = pa.table(
                {
                    "timestamp": pa.array(stamps, type=pa.timestamp("us", tz="UTC")),
                    "open": pa.array(closes, type=pa.float64()),
                    "high": pa.array(closes, type=pa.float64()),
                    "low": pa.array(closes, type=pa.float64()),
                    "close": pa.array(closes, type=pa.float64()),
                    "volume": pa.array([10] * len(stamps), type=pa.int64()),
                    "open_interest": pa.array([None] * len(stamps), type=pa.int64()),
                }
            )
            pq.write_table(table, dest / "12.parquet")

        # The 1day partition exists (discovery alive) with a bar on 12-30 ONLY;
        # 1-minute exists on 12-31 → bars=[] + base present + derive abstain.
        _raw_write("1day", [datetime(9999, 12, 30, 3, 45, tzinfo=UTC)], [100.0])
        _raw_write("1minute", [datetime(9999, 12, 31, 3, 45, tzinfo=UTC)], [105.0])
        with capture_logs() as logs:
            cands = compute_spot_candidates(
                lake_root=tmp_path, interval="1day", date=last.isoformat(), time=None
            )
        assert cands.snapshots == ()
        assert cands.skipped == 1
        assert [e for e in logs if e["event"] == "movers.stored_stale_fallback"] == []


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
