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
        """REGRESSION (DEC-MOVERS-GATE-PREBUILD-SCOPE-001; fail-before proven):
        on origin/main ``compute_spot_candidates`` has no ``prefer_resample``
        parameter (this test errors with ``TypeError``), and behaviourally the
        aligned stored partition ALWAYS won — the ``prefer_resample=False`` arm
        below pins exactly that old behaviour serving the POISONED stale close.
        With ``prefer_resample=True`` (the route's pull-active switch) the read
        bypasses the stale stored partition and reproduces the fresh 1-minute
        resample byte-identically."""
        _seed_spot(tmp_path, "NIFTY", 20000)
        # The truth: what the fresh 1-minute data resamples to (no stored TF yet).
        reference = compute_spot_candidates(
            lake_root=tmp_path, interval="5minute", date=_DAY.isoformat(), time="09:19"
        )
        assert (reference.stored_reads, reference.resampled_reads) == (0, 1)
        # A STALE stored partition lands (prior pull's prebuild, poisoned close).
        _poison_stored_5minute(tmp_path)
        stored_first = compute_spot_candidates(
            lake_root=tmp_path, interval="5minute", date=_DAY.isoformat(), time="09:19"
        )
        assert (stored_first.stored_reads, stored_first.resampled_reads) == (1, 0)
        assert stored_first.snapshots != reference.snapshots  # the stale poison is visible
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
