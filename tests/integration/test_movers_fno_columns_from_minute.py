"""End-to-end regression pin: F&O CHANGE / CHANGE% / OI columns from the REAL 1-minute table.

DEC-PIPELINE-SEGMENT-MOVERS-001 (Part 2). The operator's screenshot showed the
``/movers`` Futures + Options tabs rendering the **CHANGE**, **CHANGE %**, and the
**OI** (gainers / losers) columns as ``—`` (blank) while VALUE + VOLUME populated.

## What this pins (and why it is a pin, not a new fix)

The underlying helper fix — recovering the prior trading day's close + open-interest
from the stored **1-minute** bars when the derived ``1day`` partition is absent (or
its OI is null, which the spot-daily resample always writes) — already landed in
``movers_snapshot._prior_session_last_from_minute`` + ``movers_futures._prev_day_oi``
(MIGRATION.md #877). The helper-level behaviour is covered in
``tests/unit/test_movers_tab_integrity.py``.

This module pins the symptom the operator actually saw — the END-TO-END
``compute_futures_candidates`` / ``compute_options_candidates`` output — over a lake
that has ONLY the real ``1minute`` partitions (NO ``1day`` partition at all):

* the futures **CHANGE %** (``pct_change``) + **OI gainers/losers** (``oi_chg_pct``)
  are NON-blank, computed precisely from the prior session's real 1-minute close + OI;
* the options **CHANGE %** + **OI change** (``oi_chg`` / ``oi_chg_pct``) are NON-blank;
* a contract whose prior trading day is GENUINELY absent shows an HONEST blank
  (``None``), never a fabricated number (Golden Rule #1);
* **NO materialised view / pre-materialised table is created** — every read goes to
  the real ``bars/.../1minute`` Parquet partitions (the operator's hard rule: never a
  materialised view; the only acceleration is the transient generation-invalidated
  ``MoversSnapshotCache``, which this path does not even build).

Skipped when ``polars`` / ``pyarrow`` are absent.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("polars")
pytest.importorskip("pyarrow")

from brutex.dashboard.movers_futures import (
    compute_futures_candidates,
    rank_oi_gainers,
    rank_price_gainers,
)
from brutex.dashboard.movers_options import (
    compute_options_candidates,
)
from brutex.dashboard.movers_options import (
    rank_oi_gainers as opt_rank_oi_gainers,
)
from brutex.dashboard.movers_options import (
    rank_oi_losers as opt_rank_oi_losers,
)
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter

_EXCHANGE = "NSE"
#: The snapshot day + its prior trading day (both within the SAME UTC month, so a
#: session-filtered symbol's bars share the one ``2024/06.parquet`` — exactly how the
#: production sink stores them: appended, not overwritten).
_PRIOR_DAY = date(2024, 6, 12)
_SNAP_DAY = date(2024, 6, 13)
#: A near-month NIFTY future + a NIFTY ATM call, both expiring strictly after the snap.
_FUT = "NSE-NIFTY-27Jun24-FUT"
_OPT = "NSE-NIFTY-27Jun24-20000-CE"


def _min_bar(
    *, day: date, hour: int, minute: int, close: str, volume: int, oi: int | None
) -> CandleBar:
    """A 1-minute bar at IST ``HH:MM`` on *day* (IST = UTC + 05:30), carrying OI."""
    c = Decimal(close)
    total = hour * 60 + minute - (5 * 60 + 30)
    return CandleBar(
        timestamp=datetime(day.year, day.month, day.day, total // 60, total % 60, tzinfo=UTC),
        open=c,
        high=c,
        low=c,
        close=c,
        volume=volume,
        open_interest=oi,
    )


def _write(tmp_path: Path, *, segment: str, symbol: str, bars: list[CandleBar]) -> None:
    """Write *bars* to the real ``1minute`` lake partition (the production interval)."""
    ParquetWriter(lake_root=tmp_path).write(
        exchange=_EXCHANGE, segment=segment, symbol=symbol, interval="1minute", bars=bars
    )


def _seed_minute_only_lake(tmp_path: Path, *, with_prior: bool = True) -> None:
    """Seed a 1-minute-ONLY F&O lake (NO ``1day`` partition anywhere).

    Future: prior-day close 20000 / OI 1000 → snap-day close 20200 / OI 1200
    (pct +1.0%, OI +20%). Option: prior close 100 / OI 500 → snap close 130 /
    OI 650 (pct +30%, OI change +150, OI% +30%). The underlying NIFTY spot rides
    INDEX. When *with_prior* is False the prior-day bars are omitted, so the prior
    baseline is GENUINELY absent (the honest-blank case).
    """
    fut_bars = [
        _min_bar(day=_SNAP_DAY, hour=9, minute=15, close="20100", volume=100, oi=1150),
        _min_bar(day=_SNAP_DAY, hour=9, minute=16, close="20200", volume=300, oi=1200),
    ]
    opt_bars = [
        _min_bar(day=_SNAP_DAY, hour=9, minute=15, close="125", volume=20, oi=600),
        _min_bar(day=_SNAP_DAY, hour=9, minute=16, close="130", volume=30, oi=650),
    ]
    if with_prior:
        fut_bars.insert(
            0, _min_bar(day=_PRIOR_DAY, hour=15, minute=29, close="20000", volume=50, oi=1000)
        )
        opt_bars.insert(
            0, _min_bar(day=_PRIOR_DAY, hour=15, minute=29, close="100", volume=10, oi=500)
        )
    _write(tmp_path, segment="FNO", symbol=_FUT, bars=fut_bars)
    _write(tmp_path, segment="FNO", symbol=_OPT, bars=opt_bars)
    # Underlying NIFTY spot under INDEX (snap-day LTP 20040) — also 1-minute only.
    _write(
        tmp_path,
        segment="INDEX",
        symbol="NIFTY",
        bars=[_min_bar(day=_SNAP_DAY, hour=9, minute=16, close="20040", volume=10, oi=None)],
    )


# ── futures: CHANGE % + OI gainers from the REAL 1-minute prior session ─────


class TestFuturesColumnsFromMinute:
    def test_pct_change_and_oi_pct_non_blank(self, tmp_path: Path) -> None:
        # No 1day partition exists — the prior close + OI come straight from the real
        # 1-minute table. CHANGE % and OI % must be NON-blank.
        _seed_minute_only_lake(tmp_path)
        cands = compute_futures_candidates(
            lake_root=tmp_path, interval="1day", date=_SNAP_DAY.isoformat(), time=None
        )
        assert len(cands.movers) == 1
        mover = cands.movers[0]
        assert mover.pct_change == Decimal("1.0")  # (20200 − 20000)/20000 × 100
        assert mover.oi == 1200
        assert mover.oi_chg_pct == Decimal("20.0")  # (1200 − 1000)/1000 × 100
        # The ranked Price-Gainers + OI-Gainers screens both surface the row (not blank).
        assert rank_price_gainers(cands.movers, 10)[0].underlying == "NIFTY"
        assert rank_oi_gainers(cands.movers, 10)[0].underlying == "NIFTY"

    def test_honest_blank_when_prior_day_absent(self, tmp_path: Path) -> None:
        # No prior-day bars at all → an HONEST blank, never a fabricated number.
        _seed_minute_only_lake(tmp_path, with_prior=False)
        cands = compute_futures_candidates(
            lake_root=tmp_path, interval="1day", date=_SNAP_DAY.isoformat(), time=None
        )
        assert len(cands.movers) == 1
        mover = cands.movers[0]
        assert mover.pct_change is None
        assert mover.oi_chg_pct is None
        assert mover.oi == 1200  # the CURRENT OI still reads from the real minute bar


# ── options: CHANGE % + OI change/gainers from the REAL 1-minute prior session ─


class TestOptionsColumnsFromMinute:
    def test_pct_change_oi_change_and_oi_pct_non_blank(self, tmp_path: Path) -> None:
        _seed_minute_only_lake(tmp_path)
        cands = compute_options_candidates(
            lake_root=tmp_path, interval="1day", date=_SNAP_DAY.isoformat(), time=None
        )
        assert len(cands.movers) == 1
        mover = cands.movers[0]
        assert mover.pct_change == Decimal("30.0")  # (130 − 100)/100 × 100
        assert mover.oi == 650
        assert mover.oi_chg == 150  # 650 − 500
        assert mover.oi_chg_pct == Decimal("30.0")  # (650 − 500)/500 × 100
        # The OI-Gainers screen surfaces it; OI-Losers is empty (a gainer, not a loser).
        assert opt_rank_oi_gainers(cands.movers, 10)[0].oi_chg_pct == Decimal("30.0")
        assert opt_rank_oi_losers(cands.movers, 10) == []

    def test_honest_blank_when_prior_day_absent(self, tmp_path: Path) -> None:
        _seed_minute_only_lake(tmp_path, with_prior=False)
        cands = compute_options_candidates(
            lake_root=tmp_path, interval="1day", date=_SNAP_DAY.isoformat(), time=None
        )
        assert len(cands.movers) == 1
        mover = cands.movers[0]
        assert mover.pct_change is None
        assert mover.oi_chg is None
        assert mover.oi_chg_pct is None
        assert mover.oi == 650  # current OI from the real minute bar


# ── the operator's HARD rule: NO materialised view / table, only real partitions ─


class TestNoMaterialisedView:
    def test_reads_only_real_minute_partitions_no_view_created(self, tmp_path: Path) -> None:
        # Snapshot the whole lake tree BEFORE the read, run BOTH F&O snapshots, then
        # assert the on-disk tree gained NOTHING but the dataset-DISCOVERY manifest
        # sidecar — the readers create NO materialised view / pre-materialised table;
        # every VALUE is computed live from the real 1-minute Parquet partitions
        # (the operator's hard rule).
        _seed_minute_only_lake(tmp_path)
        before = {p.relative_to(tmp_path) for p in tmp_path.rglob("*") if p.is_file()}

        compute_futures_candidates(
            lake_root=tmp_path, interval="1day", date=_SNAP_DAY.isoformat(), time=None
        )
        compute_options_candidates(
            lake_root=tmp_path, interval="1day", date=_SNAP_DAY.isoformat(), time=None
        )

        after = {p.relative_to(tmp_path) for p in tmp_path.rglob("*") if p.is_file()}
        # The ONLY read-side addition allowed: the durable dataset-DISCOVERY
        # manifest sidecar (DEC-LAKE-MANIFEST-O1-001) — the first unfiltered
        # discovery REBUILDS it so a restart stops paying the O(files) walk.
        # It lives OUTSIDE bars/ (the ``.locks/`` placement rule) and holds
        # dataset COORDINATES only — proven below — so the operator's hard
        # rule (no materialised view / table of DATA) still holds bar-for-bar.
        manifest_sidecar = {
            Path(".manifest/datasets.jsonl"),
            Path(".manifest/datasets.meta.json"),
        }
        assert after - manifest_sidecar == before  # no OTHER new file — no view, no table
        # TEETH: the manifest sidecar carries coordinates + bookkeeping ONLY —
        # never an OHLCV/OI value (it is a directory listing, not a data view).
        import json

        allowed_row_keys = {"exchange", "segment", "symbol", "interval", "first_seen"}
        manifest_body = (tmp_path / ".manifest" / "datasets.jsonl").read_text(encoding="utf-8")
        for line in manifest_body.splitlines():
            assert set(json.loads(line)) <= allowed_row_keys, line
        # Every DATA file is a real 1-minute partition Parquet — no derived 1day
        # partition, no view, nothing pre-materialised. (The ``.locks/`` sidecars
        # are the write-time partition flock of DEC-LAKE-PARTITION-FLOCK-001,
        # created by the SEED writes; the ``.manifest/`` sidecar is the discovery
        # manifest above — both metadata, both excluded here.)
        # ``.lake_heal/`` carries the write-epoch marker every writer mutation
        # refreshes (DEC-LAKE-HEAL-STARTUP-EPOCH-001, commit bbd319d) —
        # metadata like the two siblings, never a materialised view.
        data_files = {
            rel for rel in after if not str(rel).startswith((".locks", ".manifest", ".lake_heal"))
        }
        assert data_files, sorted(str(r) for r in after)
        assert all("1minute" in str(rel) and rel.suffix == ".parquet" for rel in data_files), (
            sorted(str(r) for r in data_files)
        )
