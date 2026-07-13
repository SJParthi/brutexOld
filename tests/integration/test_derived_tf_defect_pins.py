"""Derived-TF audit RESIDUAL defect pins (DEC-DERIVED-TF-AUDIT-001).

Behavior-CAPTURE pins for the audit's confirmed-but-DEFERRED defects: each
test asserts TODAY's exact behavior (never xfail) and names the follow-up DEC
that will FLIP it — the flip is that future PR's fails-before evidence.

| pin | residual | follow-up DEC |
|---|---|---|
| R1 test_prebuild_bad_tick_defect_pin | the 1m sweep skips the x10 bar the prebuild absorbs (E2-H2 — the TOP residual by blast radius) | DEC-PREBUILD-BAD-TICK-001 |
| R2 test_stale_stored_served_defect_pin | movers stored-first serves drifted derived bytes the gate flags (E2-H5a) | DEC-DERIVED-SERVE-FRESHNESS-001 |
| R3 test_dup_ts_three_posture_defect_pin | dup-ts: engine keep-first vs prebuild quarantine vs compare-reader dedup (E2-H6) | DEC-PREBUILD-DEDUP-CONVERGE-001 |
| R4 test_muhurat_off_session_labels_stored_defect_pin | Muhurat off-session bucket labels stored verbatim (E1 B-S1) | DEC-MUHURAT-TF-ANCHOR-001 |
| R5 test_volume_overflow_defect_pin | silent Int64 volume wrap (E1 M2/M3 — unreachable: ~2.5e16 vol/min) | DEC-RESAMPLE-VOLUME-GUARD-001 |
| R6 test_bad_tick_close_only_rule_defect_pin | close-only bad-tick rule passes a corrupt extreme | DEC-BADTICK-EXTREMES-002 |
| R7 test_pre_open_prior_date_label_defect_pin | a pre-09:15 bar labels on the PRIOR IST date's grid (E1 P2 — dormant, ingest-filtered) | DEC-SESSION-EXEMPT-RESAMPLE-001 |
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("polars")
pytest.importorskip("pyarrow")
pytest.importorskip("duckdb")

import polars as pl
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.sink import ParquetBarSink
from brutex.data.timeframe_materialize import PrebuildKind, materialize_lake_timeframes
from structlog.testing import capture_logs

_IST = timezone(timedelta(hours=5, minutes=30))
_EX, _SEG, _SYM = "NSE", "INDEX", "NSE-NIFTY"


def _ist(y: int, mo: int, d: int, h: int, mi: int) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=_IST).astimezone(UTC)


def _bar(ts: datetime, o: float, h: float, low: float, c: float, *, vol: int = 100) -> CandleBar:
    return CandleBar(
        timestamp=ts,
        open=Decimal(str(o)),
        high=Decimal(str(h)),
        low=Decimal(str(low)),
        close=Decimal(str(c)),
        volume=vol,
        open_interest=None,
    )


def _session_bars(*, spike_bar7: bool) -> list[CandleBar]:
    """The E2-H2 session: 15 bars, bar #7 optionally a self-consistent x10 spike."""
    bars: list[CandleBar] = []
    for i in range(15):
        mins = 9 * 60 + 15 + i
        ts = _ist(2024, 6, 12, mins // 60, mins % 60)
        if spike_bar7 and i == 7:
            bars.append(_bar(ts, 1000.0, 1005.0, 995.0, 1001.0, vol=107))
        else:
            px = 100.0 + i * 0.05
            bars.append(_bar(ts, px, px + 0.5, px - 0.5, px + 0.1, vol=100 + i))
    return bars


def _write_1m(lake: Path, bars: list[CandleBar], *, seg: str = _SEG, sym: str = _SYM) -> None:
    sink = ParquetBarSink(writer=ParquetWriter(lake_root=lake))
    sink.append(exchange=_EX, segment=seg, symbol=sym, interval="1minute", bars=bars)


def _materialize(lake: Path, *, seg: str = _SEG, sym: str = _SYM) -> None:
    materialize_lake_timeframes(
        lake_root=lake,
        exchange=_EX,
        segment=seg,
        symbol=sym,
        kind=PrebuildKind.SPOT,
        tfs=("5m",),
        include_daily=True,
        daily_before=date(2024, 6, 13),
        incremental=False,
    )


def _engine_read(lake: Path, interval: str, *, sym: str = _SYM) -> list[CandleBar]:
    """The REAL engine mouth: reconciled Catalog → get_bars → arrow_table_to_bars."""
    from brutex.data.catalog import Catalog
    from brutex.data.catalog_reconcile import reconcile_catalog_from_lake
    from brutex.feed.replay import arrow_table_to_bars

    with Catalog.open(lake / "catalog.duckdb") as cat:
        reconcile_catalog_from_lake(lake_root=lake, catalog=cat)
        table = cat.get_bars(
            lake_root=lake,
            exchange=_EX,
            segment=_SEG,
            symbol=sym,
            interval=interval,
            start=datetime(2024, 6, 12, tzinfo=UTC),
            end=datetime(2024, 6, 13, tzinfo=UTC),
        )
    return arrow_table_to_bars(table, source=f"{_EX}/{_SEG}/{sym}/{interval}")


def _stored(lake: Path, interval: str, *, seg: str = _SEG, sym: str = _SYM) -> pl.DataFrame:
    glob = (lake / "bars" / _EX / seg / sym / interval).as_posix() + "/**/*.parquet"
    return pl.read_parquet(glob).sort("timestamp")


def test_prebuild_bad_tick_defect_pin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """R1 (E2-H2, the TOP residual): the 1m sweep never sees the x10 spike
    (BadTickScreen, default ON, skips it at the engine mouth) while the
    prebuild ABSORBS it — the derived 5m + 1day store high=1005.0 and a 5m
    sweep of the SAME lake consumes it (inconsistent worlds; the derived
    1day is the PDH/PDL anchor source). Fix = DEC-PREBUILD-BAD-TICK-001
    (the one-law lake screen at every 1m mouth + writer_convention bump 2);
    its landing FLIPS this pin — that is its fails-before evidence."""
    monkeypatch.delenv("BRUTEX_BAD_TICK_SCREEN", raising=False)
    lake = tmp_path / "lake"
    _write_1m(lake, _session_bars(spike_bar7=True))

    # Price-sanity passes the self-consistent spike everywhere.
    from brutex.data.price_sanity import count_price_violations

    df_1m = _stored(lake, "1minute").rename({"timestamp": "ts_utc"})
    assert count_price_violations(df_1m) == 0

    # The engine 1m mouth SKIPS the spike (14 of 15 served).
    engine_1m = _engine_read(lake, "1minute")
    assert len(engine_1m) == 14
    assert 1001.0 not in [float(b.close) for b in engine_1m]
    assert max(float(b.high) for b in engine_1m) == 101.2

    # The prebuild ABSORBS it: derived 5m + 1day carry the spike high.
    _materialize(lake)
    assert 1005.0 in _stored(lake, "5minute").get_column("high").to_list()
    assert 1005.0 in _stored(lake, "1day").get_column("high").to_list()

    # And a 5m sweep of the SAME lake consumes the spiked bucket (the
    # TF-level screen tests bucket CLOSES — the spiked bucket's close is
    # sane, so it passes).
    engine_5m = _engine_read(lake, "5minute")
    assert 1005.0 in [float(b.high) for b in engine_5m]


def test_stale_stored_served_defect_pin(tmp_path: Path) -> None:
    """R2 (E2-H5a): a drifted stored 5minute partition is FLAGGED stale by
    the sweep-preflight gate — yet the movers stored-first read (the default
    display path) serves the drifted bytes with NO serve-time check. Fix =
    DEC-DERIVED-SERVE-FRESHNESS-001 (an O(1) derive-stamp/1m stat-token
    compare before a stored-first serve — never a re-derive per poll)."""
    from brutex.dashboard.movers_snapshot import _read_intraday_bars, _stored_serves_cutoff
    from brutex.data.tf_autoderive import lake_derived_freshness_verdicts

    lake = tmp_path / "lake"
    _write_1m(lake, _session_bars(spike_bar7=False))
    _materialize(lake)
    p5 = lake / "bars" / _EX / _SEG / _SYM / "5minute" / "2024" / "06.parquet"
    df5 = pl.read_parquet(p5)
    shift = 999.99 - float(df5.get_column("close")[1])
    drifted = df5.with_columns(
        [
            pl.when(pl.int_range(pl.len()) == 1)
            .then(pl.col(c) + shift)
            .otherwise(pl.col(c))
            .alias(c)
            for c in ("open", "high", "low", "close")
        ]
    )
    drifted.write_parquet(p5)

    # The gate FLAGS the drift …
    verdicts = lake_derived_freshness_verdicts(
        lake, exchange=_EX, segment=_SEG, symbols=[_SYM], intervals=["5minute"]
    )
    assert verdicts == {(_SYM, "5minute"): "stale"}

    # … and the movers stored-first default path SERVES it anyway.
    assert _stored_serves_cutoff(5, None) is True
    served, from_stored = _read_intraday_bars(
        lake,
        exchange=_EX,
        segment=_SEG,
        symbol=_SYM,
        interval="5minute",
        day=date(2024, 6, 12),
        minutes=5,
        cutoff_min=None,
    )
    assert from_stored is True
    assert 999.99 in [float(b.close) for b in served]


def test_dup_ts_three_posture_defect_pin(tmp_path: Path) -> None:
    """R3 (E2-H6): ONE poisoned lake (a physical duplicate-ts 1m row — the
    documented pre-DEC-LAKE-WRITE-UNIQUE-001 era state), THREE postures:
    the engine mouth keep-first dedups + CONTINUES (WARN), the prebuild
    hard-QUARANTINES (ResampleInputError — derived TFs freeze), and the
    freshness-compare reader silently dedups. Each side is documented +
    loud; the pinned defect is the DIVERGENCE itself (a 1m sweep advances
    while every derived-TF consumer is frozen at the last good derive).
    Any drift in any posture is a conscious DEC —
    DEC-PREBUILD-DEDUP-CONVERGE-001."""
    from brutex.data.errors import ResampleInputError
    from brutex.data.tf_autoderive import read_lake_bars_frame

    lake = tmp_path / "lake"
    _write_1m(lake, _session_bars(spike_bar7=False))
    p1m = lake / "bars" / _EX / _SEG / _SYM / "1minute" / "2024" / "06.parquet"
    df = pl.read_parquet(p1m)
    dup = df[7].with_columns(pl.lit(555.55).alias("close"), pl.lit(555.55).alias("high"))
    pl.concat([df, dup]).write_parquet(p1m)
    assert pl.read_parquet(p1m).height == 16

    # Posture 1 — the engine mouth: keep-first (original close wins) + WARN.
    with capture_logs() as logs:
        engine_bars = _engine_read(lake, "1minute")
    assert len(engine_bars) == 15
    dup_ts = _session_bars(spike_bar7=False)[7].timestamp
    assert [float(b.close) for b in engine_bars if b.timestamp == dup_ts] == [100.45]
    assert [e for e in logs if e["event"] == "lake.read.duplicates_dropped"]

    # Posture 2 — the prebuild: quarantine, NO derived TFs written.
    with pytest.raises(ResampleInputError, match="STRICTLY increasing"):
        _materialize(lake)
    assert not (lake / "bars" / _EX / _SEG / _SYM / "5minute").exists()

    # Posture 3 — the freshness-compare reader: silent keep-first dedup.
    frame = read_lake_bars_frame(lake / "bars" / _EX / _SEG / _SYM, "1minute")
    assert frame.height == 15


def test_muhurat_off_session_labels_stored_defect_pin(tmp_path: Path) -> None:
    """R4 (E1 B-S1): on the calendar-verified 2024-11-01 Muhurat session
    (18:00→19:00 IST) the 30minute grid — fixed 09:15-IST arithmetic — puts
    the first in-session bucket label at 17:45 IST, BEFORE the session open,
    and the real prebuild writer stores it verbatim (the 1m session filter
    is never re-applied to bucket labels; same-IST-date always holds, so
    month routing is unaffected). An anchor change is a coordinated DEC
    across resampler + _stored_serves_cutoff + resolve_asof_cutoff + a
    lake-wide re-derive — DEC-MUHURAT-TF-ANCHOR-001, never a drive-by."""
    from brutex.calendars.nse import get_session_checked

    session = get_session_checked(date(2024, 11, 1))
    assert session is not None and session.open_time_ist.hour == 18  # calendar-verified

    lake = tmp_path / "lake"
    sym = "NSE-NIFTY-28Nov24-FUT"
    bars = []
    for i in range(60):
        px = 100.0 + i * 0.05
        bars.append(
            _bar(_ist(2024, 11, 1, 18 + (i // 60), i % 60), px, px + 0.5, px - 0.5, px + 0.1)
        )
    _write_1m(lake, bars, seg="FNO", sym=sym)
    materialize_lake_timeframes(
        lake_root=lake,
        exchange=_EX,
        segment="FNO",
        symbol=sym,
        kind=PrebuildKind.FUTURES,
        tfs=("30m",),
        include_daily=False,
        incremental=False,
    )
    stored = _stored(lake, "30minute", seg="FNO", sym=sym)
    labels_ist = [
        ts.astimezone(_IST).strftime("%H:%M") for ts in stored.get_column("timestamp").to_list()
    ]
    # The off-session 17:45 label (aggregating 18:00–18:14 bars) is STORED.
    assert labels_ist == ["17:45", "18:15", "18:45"]
    # Same IST date on every label — month routing unaffected.
    assert {ts.astimezone(_IST).date() for ts in stored.get_column("timestamp").to_list()} == {
        date(2024, 11, 1)
    }


def test_volume_overflow_defect_pin() -> None:
    """R5 (E1 M2/M3): polars .sum() on Int64 wraps mod 2^64 SILENTLY —
    2·2^62 lands at Int64-min, 4·2^62 wraps to ZERO (stored silently); a
    NEGATIVE wrap reaching the daily CandleBar conversion raises an UNTYPED
    ValueError (rides the generic per-symbol quarantine, not
    ResampleInputError). Practically unreachable (a 375-min bucket
    overflows only at ~2.5e16 volume/min; every production volume is
    parse-capped at int64); pinned on polars 1.42.1 — this test doubles as
    the polars-upgrade canary. Fix = DEC-RESAMPLE-VOLUME-GUARD-001."""
    from brutex.data.spot_daily_resample import resample_1m_to_daily
    from brutex.data.tf_resampler import resample_1m_to_tf

    base = datetime(2024, 6, 12, 3, 45)

    def _frame(vols: list[int]) -> pl.DataFrame:
        n = len(vols)
        return pl.DataFrame(
            {
                "ts_utc": [base + timedelta(minutes=i) for i in range(n)],
                "open": [100.0] * n,
                "high": [100.5] * n,
                "low": [99.5] * n,
                "close": [100.1] * n,
                "volume": vols,
            },
            schema_overrides={"volume": pl.Int64},
        )

    # M2 — the silent wrap values, captured verbatim on polars 1.42.1:
    assert resample_1m_to_tf(_frame([2**62, 2**62]), 5).get_column("volume").to_list() == [-(2**63)]
    # … including the wrap-to-ZERO (passes every non-negative check, would
    # be stored silently):
    assert resample_1m_to_tf(_frame([2**62] * 4), 5).get_column("volume").to_list() == [0]
    # M3 — a negative wrap reaching the daily CandleBar conversion raises
    # the UNTYPED ValueError (not ResampleInputError):
    with pytest.raises(ValueError, match="volume must be non-negative") as exc:
        resample_1m_to_daily(_frame([2**62, 2**62]), before=date(2024, 6, 13))
    from brutex.data.errors import ResampleInputError

    assert not isinstance(exc.value, ResampleInputError)


def test_bad_tick_close_only_rule_defect_pin() -> None:
    """R6: the bad-tick rule judges the CLOSE only — a bar with a sane close
    and a corrupt ×10 HIGH passes ``is_bad_tick`` (the clamp-untriggered
    corrupt-extreme class; the DEC-OHLC-CLAMP-BOUNDED-001 parse-time refusal
    covers only the clamp-TRIGGERED subset). Fix =
    DEC-BADTICK-EXTREMES-002 (judge the extremes too)."""
    from brutex.data.bad_tick import is_bad_tick

    # prev in-session close 100.4; this bar: sane close 100.45 (passes) even
    # though its (unjudged) high is 1005.0 — the rule sees closes only.
    assert is_bad_tick(100.4, 100.45) is False
    # Control: the x10 CLOSE itself is caught.
    assert is_bad_tick(100.4, 1001.0) is True


def test_pre_open_prior_date_label_defect_pin() -> None:
    """R7 (E1 P2 — dormant): a whole-minute bar BEFORE 09:15 IST labels on
    the SAME fixed grid arithmetic, which floors NEGATIVE offsets — a 00:05
    IST bar @30m labels 2024-01-01 23:45 IST, the PRIOR IST date. The
    contract prose 'labels share the bar's IST trading date' was corrected
    to name this (see tf_resampler's Anchor section); production ingest
    session-filters pre-open bars, so the shape is unreachable today —
    session-exempt symbols would hit it. Fix =
    DEC-SESSION-EXEMPT-RESAMPLE-001."""
    from brutex.data.tf_resampler import resample_1m_to_tf

    # 00:05 IST on 2024-01-02 == 18:35 UTC on 2024-01-01.
    ts = datetime(2024, 1, 2, 0, 5, tzinfo=_IST).astimezone(UTC).replace(tzinfo=None)
    frame = pl.DataFrame(
        {
            "ts_utc": [ts],
            "open": [100.0],
            "high": [100.5],
            "low": [99.5],
            "close": [100.1],
            "volume": [10],
        }
    )
    label = resample_1m_to_tf(frame, 30).get_column("ts_utc").to_list()[0]
    label_ist = label.replace(tzinfo=UTC).astimezone(_IST)
    # The prior IST date's 23:45 grid slot — captured verbatim.
    assert label_ist == datetime(2024, 1, 1, 23, 45, tzinfo=_IST)
