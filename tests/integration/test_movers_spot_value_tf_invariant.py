"""Integration golden: TF-INVARIANT SPOT "Top Value" (DEC-MOVERS-SPOT-VALUE-TF-INVARIANT-001).

Against a REAL Parquet lake (:class:`~brutex.data.parquet_writer.ParquetWriter`) +
the REAL prebuild materialiser
(:func:`~brutex.data.timeframe_materialize.materialize_lake_timeframes`) driven
through the REAL :func:`~brutex.dashboard.movers_snapshot.compute_spot_candidates`
and the REAL ``GET /api/v1/movers`` route (FastAPI ``TestClient``):

* **The headline golden**: spot "Top Value" (``value_paisa`` / ``value_cr``) is
  BYTE-identical across ``interval ∈ {1minute, 5minute, 1day}`` at a shared
  whole-day as-of, and equals an INDEPENDENT test-side hand computation of
  ``Σ round_money((h+l+c)/3) × vol`` over the RAW 1-minute frame. TEETH: the OLD
  per-5min-bucket / per-day-bar divergent numbers do NOT appear (a revert to the
  resampled-bar reduction FAILS this pin).
* **stored-first ≡ resample display path**: the value is identical whether the
  intraday display bars are served from the prebuilt stored partition or the
  on-the-fly resample, while LTP / volume (from the display bars) stay
  byte-identical (the DEC-PIPELINE-AUTOPREBUILD-001 stored-vs-resample contract).
* **corrupt-1m skip**: a daily read whose ``1day`` display partition is fine but
  whose ``1minute`` value partition is unreadable is SKIPPED (never a 500) — the
  new value read is inside the same ``try`` as the bar read.

Skipped when polars / pyarrow are absent (the lake deps).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

import pytest

pytest.importorskip("polars")
pytest.importorskip("pyarrow")

from brutex.dashboard.app import DashboardState, build_app
from brutex.dashboard.movers_snapshot import compute_spot_candidates, value_crore_str
from brutex.data.instrument_store import InstrumentStore
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.timeframe_materialize import PrebuildKind, materialize_lake_timeframes
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from fastapi.testclient import TestClient

_DAY = date(2024, 1, 2)
_PRIOR = date(2023, 12, 29)

#: The reproduction shape: ten 1-minute bars 09:15–09:24 whose per-5min-bucket
#: HLC3 ≠ per-minute HLC3 (high=close+2, low=close-3), so a revert to the
#: resampled-bar value reduction re-diverges across display timeframes.
_CLOSES = ["1000", "1010", "1020", "1005", "1030", "1040", "1015", "1050", "1025", "1060"]
_VOLS = [100, 110, 120, 105, 130, 140, 115, 150, 125, 160]

#: The three quantities, hand-derived off the SAME bars (see the module test that
#: recomputes them independently with :class:`~fractions.Fraction`):
_RAW_1M_PAISA = 129006085  # Σ round_money(HLC3(1-minute bar)) × vol — the TF-invariant answer
_OLD_5M_BUCKET_PAISA = 129693585  # the OLD per-5min-bucket-HLC3 sum (must NOT appear)
_OLD_1DAY_BAR_PAISA = 130478585  # the OLD per-day-bar-HLC3 value (must NOT appear)


def _bar(*, day: date, hour: int, minute: int, close: str, volume: int) -> CandleBar:
    c = Decimal(close)
    total = hour * 60 + minute - (5 * 60 + 30)
    return CandleBar(
        timestamp=datetime(day.year, day.month, day.day, total // 60, total % 60, tzinfo=UTC),
        open=c - Decimal("1"),
        high=c + Decimal("2"),
        low=c - Decimal("3"),
        close=c,
        volume=volume,
        open_interest=None,
    )


def _session(day: date) -> list[CandleBar]:
    return [
        _bar(day=day, hour=9, minute=15 + i, close=c, volume=v)
        for i, (c, v) in enumerate(zip(_CLOSES, _VOLS, strict=True))
    ]


def _round_half_up_2dp(x: Fraction) -> Fraction:
    scaled = x * 100
    q, r = divmod(scaled.numerator, scaled.denominator)
    if 2 * r >= scaled.denominator:
        q += 1
    return Fraction(q, 100)


def _hand_raw_1m_paisa(bars: list[CandleBar]) -> int:
    total = Fraction(0)
    for b in bars:
        total += _round_half_up_2dp(Fraction(b.high + b.low + b.close) / 3) * b.volume
    return int(total * 100)


def _write(lake: Path, *, symbol: str, interval: str, bars: list[CandleBar]) -> None:
    ParquetWriter(lake_root=lake).write(
        exchange="NSE", segment="INDEX", symbol=symbol, interval=interval, bars=bars
    )


def _seed_and_materialize(lake: Path, symbol: str = "NIFTY") -> None:
    """1-minute _DAY + a prior-day session, then materialise the derived 1day + intraday TFs."""
    _write(lake, symbol=symbol, interval="1minute", bars=_session(_PRIOR))
    _write(lake, symbol=symbol, interval="1minute", bars=_session(_DAY))
    result = materialize_lake_timeframes(
        lake_root=lake,
        exchange="NSE",
        segment="INDEX",
        symbol=symbol,
        kind=PrebuildKind.SPOT,
        include_daily=True,
    )
    assert result.total_bars > 0  # stored TF partitions + derived 1day really written


def _value_of(lake: Path, interval: str) -> int:
    cands = compute_spot_candidates(
        lake_root=lake, interval=interval, date=_DAY.isoformat(), time=None
    )
    assert len(cands.snapshots) == 1, f"expected one snapshot for {interval}"
    return cands.snapshots[0].value_paisa


class TestSpotValueTfInvariantGolden:
    def test_value_byte_identical_across_1m_5m_1day(self, tmp_path: Path) -> None:
        _seed_and_materialize(tmp_path)
        v1 = _value_of(tmp_path, "1minute")
        v5 = _value_of(tmp_path, "5minute")
        vd = _value_of(tmp_path, "1day")
        # (1) All three display timeframes agree — a true session property now.
        assert v1 == v5 == vd
        # (2) …and equal the INDEPENDENT raw-1m HLC3 hand computation (no repo helper).
        assert v1 == _hand_raw_1m_paisa(_session(_DAY)) == _RAW_1M_PAISA
        # (3) TEETH — the OLD divergent per-bucket / per-day numbers never appear
        # (a revert to the resampled-bar reduction would resurrect one of these).
        assert {v1, v5, vd}.isdisjoint({_OLD_5M_BUCKET_PAISA, _OLD_1DAY_BAR_PAISA})

    def test_1minute_value_is_the_unchanged_reference(self, tmp_path: Path) -> None:
        """value(1minute) is UNCHANGED by the fix (the 1-minute path already summed
        over the raw 1-minute reference — ``resample_1m_to_tf(df, 1)`` is a no-op),
        so it pins the reference the coarser TFs were moved ONTO."""
        _seed_and_materialize(tmp_path)
        assert _value_of(tmp_path, "1minute") == _RAW_1M_PAISA

    def test_mid_session_shared_cutoff_agrees_across_intraday_tfs(self, tmp_path: Path) -> None:
        """At a shared intraday as-of, value is identical across intraday TFs
        (re-sourced from the raw 1-minute frame cut at the SAME minute)."""
        _seed_and_materialize(tmp_path)
        expected = _hand_raw_1m_paisa(_session(_DAY)[:5])  # bars 09:15–09:19
        assert expected == 57273855
        values: set[int] = set()
        for interval in ("1minute", "5minute", "15minute"):
            cands = compute_spot_candidates(
                lake_root=tmp_path, interval=interval, date=_DAY.isoformat(), time="09:19"
            )
            assert len(cands.snapshots) == 1
            values.add(cands.snapshots[0].value_paisa)
        assert values == {expected}

    def test_stored_first_and_resample_agree_and_ltp_volume_byte_identical(
        self, tmp_path: Path
    ) -> None:
        """The value is identical on BOTH display read paths (resample fallback vs
        the prebuilt stored partition), and the display-derived LTP / volume stay
        byte-identical across them (DEC-PIPELINE-AUTOPREBUILD-001)."""
        # Pre-materialise → the 5-minute read falls back to the on-the-fly resample.
        _write(tmp_path, symbol="NIFTY", interval="1minute", bars=_session(_PRIOR))
        _write(tmp_path, symbol="NIFTY", interval="1minute", bars=_session(_DAY))
        before = compute_spot_candidates(
            lake_root=tmp_path, interval="5minute", date=_DAY.isoformat(), time=None
        )
        assert (before.stored_reads, before.resampled_reads) == (0, 1)
        # Materialise → the same read is now served from the STORED 5-minute partition.
        materialize_lake_timeframes(
            lake_root=tmp_path,
            exchange="NSE",
            segment="INDEX",
            symbol="NIFTY",
            kind=PrebuildKind.SPOT,
            include_daily=True,
        )
        after = compute_spot_candidates(
            lake_root=tmp_path, interval="5minute", date=_DAY.isoformat(), time=None
        )
        assert (after.stored_reads, after.resampled_reads) == (1, 0)
        b, a = before.snapshots[0], after.snapshots[0]
        # Value identical on both paths AND equal to the raw-1m answer.
        assert b.value_paisa == a.value_paisa == _RAW_1M_PAISA
        # LTP + volume (from the display bars) byte-identical across the two paths.
        assert (b.ltp, b.volume, b.bar_count) == (a.ltp, a.volume, a.bar_count)


class TestSpotValueCorruptSkip:
    def test_corrupt_1minute_value_partition_is_skipped_not_500(self, tmp_path: Path) -> None:
        """A DAILY read whose ``1day`` display bars are fine but whose ``1minute``
        VALUE partition is unreadable is skipped (the new value read sits inside the
        same ``try`` as the bar read — DEC-MOVERS-CORRUPT-SKIP-001)."""
        _seed_and_materialize(tmp_path, "NIFTY")
        # Corrupt only the 1-minute partition (the value source); the derived 1day
        # display partition stays intact, so bars + prev_close still read fine.
        one_min = tmp_path / "bars" / "NSE" / "INDEX" / "NIFTY" / "1minute"
        for pq_file in one_min.rglob("*.parquet"):
            pq_file.write_bytes(b"not a parquet file at all")
        cands = compute_spot_candidates(
            lake_root=tmp_path, interval="1day", date=_DAY.isoformat(), time=None
        )
        assert cands.snapshots == ()  # the one symbol skipped, never a fabricated value
        assert cands.skipped == 1


# ── end-to-end route: Top Value byte-identical across ?interval= ───────────


def _idx(symbol: str) -> Instrument:
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=abs(hash(symbol)) % 100_000,
        symbol_name=symbol,
        display_name=symbol,
        instrument=InstrumentType.IDX,
        segment=Segment.CASH,
        series=None,
        isin=None,
        underlying_symbol=None,
        underlying_security_id=None,
        lot_size=1,
        tick_size=Decimal("0.05"),
        sm_freeze_qty=None,
        sm_expiry_date=None,
        strike_price=None,
        asm_gsm_flag=False,
    )


@pytest.fixture
def store(tmp_path: Path) -> Iterator[InstrumentStore]:
    s = InstrumentStore.open(tmp_path / "instruments.duckdb")
    s.upsert_many([_idx("NIFTY")], snapshot_date=_DAY, run_id="r1", csv_source_sha256="0" * 64)
    yield s
    s.close()


class TestMoversRouteTopValueInvariant:
    def test_top_value_byte_identical_across_intervals(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        # store.path is tmp_path/instruments.duckdb → lake root is tmp_path.
        _seed_and_materialize(tmp_path, "NIFTY")
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        seen: set[str] = set()
        for interval in ("1minute", "5minute", "1day"):
            body = client.get(
                "/api/v1/movers",
                params={
                    "asset": "spot",
                    "interval": interval,
                    "date": _DAY.isoformat(),
                    "top_n": 5,
                },
            ).json()
            rows = body["value"]
            assert rows, f"expected a Top Value row for {interval}"
            assert rows[0]["symbol"] == "NIFTY"
            seen.add(rows[0]["value_cr"])
        # ONE distinct value across all three timeframes = the TF-invariant turnover.
        assert seen == {value_crore_str(_RAW_1M_PAISA)}
