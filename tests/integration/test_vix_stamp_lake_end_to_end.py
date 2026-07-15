"""Lake-driven end-to-end integration of the India-VIX stamp chain (DEC-VIX-STAMP-WIRE-001 Stage D).

The full production path over a REAL tmp lake, no seams monkeypatched:

    raw NSE/INDEX/INDIAVIX parquet partitions (1minute + derived 1day)
        → :func:`brutex.engine.reference_wire.build_vix_stamper`
          (catalog-reconcile → L1 heal → window-bounded feed → keep-FIRST
          dedup, over the ONE shared ``[start, end]`` window)
        → :func:`brutex.engine.sweep_run.run_persisted_sweep`
        → persisted finalist rows carrying the ``vix_mean/min/max`` trio.

Plus the ANCHOR-ALIGNMENT pin: a ``1day``-swept trade's timestamps are
midnight-IST daily anchors (18:30 UTC of the prior calendar day — e.g.
``2026-01-04T18:30Z`` for session 2026-01-05), which only the ``1day``
partition carries; ONE stamper build over ONE shared window must serve BOTH
the intraday minute instants (from the 1-minute partition) AND the daily
anchor (from the 1-day partition), and the two partitions can never collide
on a timestamp (a session minute never falls at midnight IST).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("polars")
pytest.importorskip("pyarrow")
pytest.importorskip("duckdb", reason="the lake catalog needs duckdb")

from brutex.data.models import CandleBar
from brutex.data.option_dedup import OptionDedupIndex
from brutex.engine.reference_wire import build_vix_stamper
from brutex.engine.results_store import ResultsStore
from brutex.engine.sweep_run import ComboDedup, SweepRunConfig, run_persisted_sweep
from brutex.execution.fill_mode import FillMode
from brutex.orchestrator.audit import AuditWriter
from brutex.strategies.types import Side
from structlog.testing import capture_logs

_IST = timezone(timedelta(hours=5, minutes=30))

#: The midnight-IST ``1day`` anchor of session Mon 2026-01-05 (18:30Z Jan 4) —
#: the exact instant the anchor-alignment pin covers.
_DAILY_ANCHOR = datetime(2026, 1, 4, 18, 30, tzinfo=UTC)
#: A second daily anchor (session Tue 2026-01-06) so an anchor-to-anchor trade
#: has two distinct instants.
_DAILY_ANCHOR_NEXT = datetime(2026, 1, 5, 18, 30, tzinfo=UTC)


def _bar(day: int, minute: int, o: float, c: float) -> CandleBar:
    """One IST-session minute bar (stored UTC) with Decimal OHLC."""
    ts = (
        datetime(2026, 1, 5, 9, 16, tzinfo=_IST) + timedelta(days=day, minutes=minute)
    ).astimezone(UTC)
    hi = max(o, c) + 0.5
    lo = min(o, c) - 0.5
    return CandleBar(
        timestamp=ts,
        open=Decimal(str(round(o, 2))),
        high=Decimal(str(round(hi, 2))),
        low=Decimal(str(round(lo, 2))),
        close=Decimal(str(round(c, 2))),
        volume=100,
        open_interest=None,
    )


def _trending_bars(n_days: int, per_day: int) -> list[CandleBar]:
    """A multi-day upward-drifting session — BUY combos clear the rank gates."""
    bars: list[CandleBar] = []
    for d in range(n_days):
        price = 100.0
        for m in range(per_day):
            close = price + 0.6 if m % 3 else price - 0.3
            bars.append(_bar(d, m, price, close))
            price = close
    return bars


#: The swept index window (all of January 2026 — one lake month file).
_INDEX_BARS = _trending_bars(n_days=15, per_day=120)

#: Deterministic per-instant VIX levels: 14.00 + (i % 7)/100 — every level the
#: sweep can stamp lies in [14.00, 14.06], checkable on the persisted trio.
_VIX_LEVELS = [14.0 + (i % 7) / 100 for i in range(len(_INDEX_BARS))]


def _plant_vix_partition(
    lake_root: Path, interval: str, rows: Sequence[tuple[datetime, float]]
) -> None:
    """Write one RAW pyarrow NSE/INDEX/INDIAVIX month partition (2026-01)."""
    import pyarrow as pa
    import pyarrow.parquet as pq
    from brutex.data.partition import build_partition_path

    dest = build_partition_path(
        lake_root=lake_root,
        exchange="NSE",
        segment="INDEX",
        symbol="INDIAVIX",
        interval=interval,
        year=2026,
        month=1,
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    prices = [price for _, price in rows]
    table = pa.table(
        {
            "timestamp": pa.array([ts for ts, _ in rows], type=pa.timestamp("us", tz="UTC")),
            "open": pa.array(prices, type=pa.float64()),
            "high": pa.array(prices, type=pa.float64()),
            "low": pa.array(prices, type=pa.float64()),
            "close": pa.array(prices, type=pa.float64()),
            "volume": pa.array([0] * len(rows), type=pa.int64()),
            "open_interest": pa.array([None] * len(rows), type=pa.int64()),
        }
    )
    pq.write_table(table, dest)


def _plant_vix_lake(lake_root: Path) -> None:
    """Plant the 1-minute series (every index-bar instant) + two 1day anchors."""
    _plant_vix_partition(
        lake_root,
        "1minute",
        [(bar.timestamp, level) for bar, level in zip(_INDEX_BARS, _VIX_LEVELS, strict=True)],
    )
    _plant_vix_partition(lake_root, "1day", [(_DAILY_ANCHOR, 13.5), (_DAILY_ANCHOR_NEXT, 13.75)])


def _shared_window() -> tuple[datetime, datetime]:
    """ONE run window covering the daily anchors AND every session minute."""
    return _DAILY_ANCHOR, _INDEX_BARS[-1].timestamp


def _run_cfg() -> SweepRunConfig:
    return SweepRunConfig(
        instrument="NIFTY",
        tf="1m",
        direction=Side.BUY,
        mode="A",
        condition_bits=[0, 1, 2],
        k=1,
        data_snapshot_hash="snap0001",
        indicator_version="ind-v1",
        code_commit_sha="deadbeef",
        tf_minutes=1,
        sl_paisa=80,
        tp_paisa=50,
        # LEGACY-MODE PIN (DEC-FILL-WORST-CASE-001): pin PRECISE explicitly.
        fill_mode=FillMode.PRECISE,
    )


class TestVixStampLakeEndToEnd:
    def test_lake_built_stamper_lands_the_trio_on_persisted_rows(self, tmp_path: Path) -> None:
        """Real lake → build_vix_stamper → run_persisted_sweep → stamped rows."""
        lake = tmp_path / "lake"
        _plant_vix_lake(lake)
        start, end = _shared_window()

        with capture_logs() as logs:
            stamper = build_vix_stamper(lake_root=lake, start=start, end=end)
        assert stamper is not None
        built = [e for e in logs if e["event"] == "reference_wire.vix_stamper_built"]
        assert len(built) == 1
        assert built[0]["minute_bars"] == len(_INDEX_BARS)
        assert built[0]["daily_bars"] == 2

        store = ResultsStore(path=tmp_path / "results.parquet")
        with AuditWriter.open(tmp_path / "audit.jsonl") as audit:
            summary = run_persisted_sweep(
                _INDEX_BARS,
                _run_cfg(),
                store=store,
                dedup=ComboDedup(OptionDedupIndex(capacity=10_000, fpr_target=1e-6)),
                audit=audit,
                reference_stamper=stamper,
            )
        assert summary.persisted > 0
        rows = ResultsStore.load(tmp_path / "results.parquet").rows()
        assert len(rows) == summary.persisted
        for row in rows:
            # Every finalist row carries a REAL trio from the planted lake series.
            assert row.vix_mean is not None
            assert row.vix_min is not None
            assert row.vix_max is not None
            assert row.vix_min <= row.vix_mean <= row.vix_max
            # All planted 1-minute levels lie in [14.00, 14.06] — the persisted
            # trio must come from THAT series, never a fabricated level.
            assert 14.0 <= row.vix_min <= row.vix_max <= 14.06

    def test_shared_window_serves_both_minute_instants_and_the_daily_anchor(
        self, tmp_path: Path
    ) -> None:
        """The ANCHOR-ALIGNMENT pin: one build serves 1minute AND 1day instants.

        A ``1day``-swept trade timestamps at midnight-IST anchors
        (``2026-01-04T18:30Z`` for session 2026-01-05); those instants exist
        ONLY in the 1-day partition, while intraday trades stamp from the
        1-minute partition — one ``build_vix_stamper`` call over the ONE
        shared window must serve both.
        """
        lake = tmp_path / "lake"
        _plant_vix_lake(lake)
        start, end = _shared_window()
        stamper = build_vix_stamper(lake_root=lake, start=start, end=end)
        assert stamper is not None

        # The midnight-IST daily anchor stamps from the 1day partition …
        assert datetime(2026, 1, 5, 0, 0, tzinfo=_IST) == _DAILY_ANCHOR  # 18:30Z == 00:00 IST
        entry, exit_ = stamper.stamp(_DAILY_ANCHOR, _DAILY_ANCHOR_NEXT)
        assert (entry, exit_) == (Decimal("13.5"), Decimal("13.75"))

        # … an intraday instant stamps from the 1-minute partition …
        minute_ts = _INDEX_BARS[0].timestamp
        assert stamper.vix.level_at(minute_ts) == Decimal("14.0")

        # … and the anchor level could ONLY have come from the 1day partition:
        # no 1-minute bar exists at midnight IST (a session minute never does).
        minute_instants = {bar.timestamp for bar in _INDEX_BARS}
        assert _DAILY_ANCHOR not in minute_instants
        assert stamper.miss_count == 0  # every lookup above was a real hit
