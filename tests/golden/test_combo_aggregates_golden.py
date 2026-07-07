"""Golden byte-equality oracle for the WP2 aggregate fold (DEC-SWEEP-RESULTS-DEPTH-001).

The WP1 capture golden's fixed fixture (Fixture-A bars, one three-trade combo
TP → SL → forced-15:20, one zero-trade combo, aligned India-VIX series, the
1-minute index over the same bars) is captured with the REAL
:func:`~brutex.engine.trade_capture.capture_combo`, folded through the REAL
:func:`~brutex.engine.combo_aggregates.aggregate_combo` under a hand-pinned
:class:`~brutex.engine.session_lens.SessionLens` (trend UP + two event flags
on the fixture day), and locked two ways:

1. the sha256 of the canonical JSON serialization of BOTH aggregate records
   (every field, full precision), and
2. the sha256 of the ``<stem>.aggs.parquet`` bytes as written by the REAL
   :func:`~brutex.engine.trade_store.write_tuple_sidecars` (write-twice
   byte identity included).

Any change to the aggregate arithmetic, the JSON rendering law, the record
key set or the Parquet render knobs moves a digest and MUST be a deliberate,
ledgered re-lock (the ``_GOLDEN_CONTENT_HASH`` discipline).

Regenerate (deterministic) after a DELIBERATE change::

    .venv/bin/python -c "import tests.golden.test_combo_aggregates_golden as g; g.print_digests()"
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from brutex.calendars.nse import IST
from brutex.data.models import CandleBar
from brutex.engine.combo_aggregates import ComboAggregate, aggregate_combo
from brutex.engine.minute_path import MinuteIndex
from brutex.engine.reference_series import TradeReferenceStamper
from brutex.engine.results_store import ResultRow
from brutex.engine.session_lens import SessionLens, TrendRegime
from brutex.engine.trade_capture import ComboCapture, capture_combo
from brutex.engine.trade_store import sidecar_paths_for, write_tuple_sidecars

_DAY = date(2024, 1, 2)


def _ts(hh: int, mm: int) -> datetime:
    return datetime(_DAY.year, _DAY.month, _DAY.day, hh, mm, tzinfo=IST).astimezone(UTC)


def _bar(hh: int, mm: int, o: str, h: str, lo: str, c: str) -> CandleBar:
    return CandleBar(
        timestamp=_ts(hh, mm),
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(lo),
        close=Decimal(c),
        volume=0,
        open_interest=None,
    )


_BARS = [
    _bar(9, 15, "100", "100", "100", "100"),
    _bar(9, 16, "100.00", "100.10", "99.95", "100.05"),
    _bar(9, 17, "100.00", "100.60", "99.90", "100.40"),
    _bar(9, 18, "100", "100", "100", "100"),
    _bar(9, 19, "100.00", "100.05", "99.90", "99.95"),
    _bar(9, 20, "100.00", "100.10", "99.60", "99.70"),
    _bar(15, 17, "100", "100", "100", "100"),
    _bar(15, 18, "100.00", "100.20", "99.80", "100.10"),
    _bar(15, 19, "100.00", "100.30", "99.90", "100.20"),
    _bar(15, 20, "100.15", "101.00", "100.00", "100.50"),
]
_BAR_BITS = [1 if i in (0, 3, 6) else 0 for i in range(len(_BARS))]
_VIX_BARS = [
    _bar(9, 16, "15.25", "15.25", "15.25", "15.25"),
    _bar(9, 17, "15.30", "15.30", "15.30", "15.30"),
    _bar(9, 19, "15.40", "15.40", "15.40", "15.40"),
    _bar(9, 20, "15.45", "15.45", "15.45", "15.45"),
    _bar(15, 18, "15.50", "15.50", "15.50", "15.50"),
    _bar(15, 20, "15.60", "15.60", "15.60", "15.60"),
]

_RUN_TRADES = "ab" * 32
_RUN_ZERO = "cd" * 32
_STORE_HASH = "12" * 32
_FP_TRADES = "aa" * 32
_FP_ZERO = "bb" * 32
_START_EQUITY = 10_000_000
_COMPOSITE = 1.2345678901234567  # full-precision pass-through pin

#: The hand-pinned lens: the fixture day trends UP and carries two verified
#: event flags — exercises the trend, event-count and event-P&L folds.
_LENS = SessionLens(
    trend_by_date={_DAY: TrendRegime.UP},
    event_flags_by_date={_DAY: ("expiry", "muhurat")},
)

#: Locked digests (sha256 hex). Regeneration is a deliberate, ledgered act.
_GOLDEN_AGG_CANONICAL_SHA256 = "ef921b8a71070cee8640b35bde3b3b3dfe2b714b08a8060ac4d00f478acbaa53"
_GOLDEN_AGGS_PARQUET_SHA256 = "63d823e9b5f446f412df19af01e680f1f993342663f48872b7b92a5c913fff4e"


def _row(run_id: str, mask: int, pnls: list[int]) -> ResultRow:
    return ResultRow(
        run_id=run_id,
        instrument="NIFTY",
        tf="1minute",
        direction="bull",
        mode="A",
        mask_bits=mask,
        net_pnl_paisa=sum(pnls),
        gross_profit_paisa=sum(p for p in pnls if p > 0),
        gross_loss_paisa=-sum(p for p in pnls if p < 0),
        max_drawdown_paisa=30,
        expectancy_paisa=0,
        total_trades=len(pnls),
        winning_trades=sum(1 for p in pnls if p > 0),
        losing_trades=sum(1 for p in pnls if p < 0),
        sharpe_ratio=0.0,
        sortino_ratio=0.0,
        calmar_ratio=0.0,
        profit_factor=0.0,
        win_rate=0.0,
        max_drawdown_pct=0.0,
        avg_entry_price=Decimal("100.00"),
        avg_exit_price=Decimal("100.00"),
    )


def _fixture() -> list[tuple[ComboAggregate, ComboCapture]]:
    index = MinuteIndex.from_bars(_BARS)
    stamper = TradeReferenceStamper.from_vix_bars(_VIX_BARS)
    row_trades = _row(_RUN_TRADES, 0b1, [50, -30, 15])
    row_zero = _row(_RUN_ZERO, 0b10, [])
    capture_trades = capture_combo(
        row_trades,
        bars=_BARS,
        bar_bits=_BAR_BITS,
        tf_minutes=1,
        sl_paisa=30,
        tp_paisa=50,
        minute_index=index,
        stamper=stamper,
    )
    capture_zero = capture_combo(
        row_zero,
        bars=_BARS,
        bar_bits=_BAR_BITS,
        tf_minutes=1,
        sl_paisa=30,
        tp_paisa=50,
        minute_index=index,
        stamper=stamper,
    )
    agg_trades = aggregate_combo(
        capture_trades,
        row_trades,
        lens=_LENS,
        composite_score=_COMPOSITE,
        fingerprint=_FP_TRADES,
        start_equity_paisa=_START_EQUITY,
    )
    agg_zero = aggregate_combo(
        capture_zero,
        row_zero,
        lens=_LENS,
        composite_score=None,
        fingerprint=_FP_ZERO,
        start_equity_paisa=_START_EQUITY,
    )
    return [(agg_trades, capture_trades), (agg_zero, capture_zero)]


def _canonical_bytes(aggs: list[ComboAggregate]) -> bytes:
    payload = [agg.to_record() for agg in aggs]
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def test_aggregate_canonical_serialization_golden() -> None:
    aggs = [agg for agg, _ in _fixture()]
    digest = hashlib.sha256(_canonical_bytes(aggs)).hexdigest()
    assert digest == _GOLDEN_AGG_CANONICAL_SHA256, (
        f"aggregate canonical digest moved: {digest} — any aggregate-arithmetic "
        "change must be a deliberate, ledgered re-lock"
    )


def test_aggregate_spot_values_hand_verified() -> None:
    """Belt for the digest: the headline numbers re-derived by hand.

    The WP1 fixture's three trades are TP (+50), SL (−30), forced-15:20 (+15)
    — all on the UP-trending, double-flagged fixture day with a full 1-minute
    path and full VIX stamps.
    """
    (agg, capture), (agg_zero, _) = _fixture()
    assert agg.n_trades == 3
    assert agg.parity_ok is True
    assert agg.zero_trades is False
    assert agg.trades_with_1m == 3
    assert agg.equity_status == "ok"
    assert agg.vix_stamped_trades == 3
    assert agg.vix_missing_trades == capture.vix_missing_trades == 0
    assert json.loads(agg.pnl_by_year_json) == {"2024": 35}
    assert json.loads(agg.pnl_by_trend_json) == {"up": 35}
    assert json.loads(agg.event_day_counts_json) == {"expiry": 3, "muhurat": 3}
    assert json.loads(agg.event_day_pnl_json) == {"expiry": 35, "muhurat": 35}
    assert agg.one_regime_wonder is True  # all profit in 2024 / in "up"
    assert agg.regime_wonder_axis == "year"
    assert agg.exits_take_profit == 1
    assert agg.exits_stop_loss == 1
    assert agg.exits_forced_1520 == 1
    assert agg.max_consecutive_losses == 1
    assert agg.max_consecutive_wins == 1
    assert agg.composite_score == _COMPOSITE
    assert agg.recovery_factor == 35 / 30  # row net 35 / row stored DD 30
    assert agg_zero.n_trades == 0
    assert agg_zero.zero_trades is True
    assert agg_zero.mae_avg_paisa is None


def test_aggs_parquet_bytes_golden_and_write_twice_identical(tmp_path: Path) -> None:
    root = tmp_path / "sweeps"
    paths = sidecar_paths_for(root / "NIFTY" / "1minute" / "bull_k1_feedfacefeedface.parquet", root)
    fixture = _fixture()
    captures = [capture for _, capture in fixture]
    records = [agg.to_record() for agg, _ in fixture]
    write_tuple_sidecars(paths, captures, records, store_content_hash=_STORE_HASH)
    first = paths.aggs.read_bytes()
    write_tuple_sidecars(paths, captures, records, store_content_hash=_STORE_HASH)
    assert paths.aggs.read_bytes() == first
    digest = hashlib.sha256(first).hexdigest()
    assert digest == _GOLDEN_AGGS_PARQUET_SHA256, (
        f"aggs parquet digest moved: {digest} — any schema/render change must "
        "be a deliberate, ledgered re-lock"
    )


def print_digests() -> None:
    """Regeneration helper — prints the two locked digests."""
    import tempfile

    aggs = [agg for agg, _ in _fixture()]
    print("canonical:", hashlib.sha256(_canonical_bytes(aggs)).hexdigest())
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "sweeps"
        paths = sidecar_paths_for(
            root / "NIFTY" / "1minute" / "bull_k1_feedfacefeedface.parquet", root
        )
        fixture = _fixture()
        write_tuple_sidecars(
            paths,
            [capture for _, capture in fixture],
            [agg.to_record() for agg, _ in fixture],
            store_content_hash=_STORE_HASH,
        )
        print("aggs_parquet:", hashlib.sha256(paths.aggs.read_bytes()).hexdigest())
