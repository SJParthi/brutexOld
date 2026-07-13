"""Golden byte-equality oracle for the WP1 capture core (DEC-SWEEP-RESULTS-DEPTH-001).

A fixed, fully hand-specified fixture — the trade-sim golden's Fixture A bars,
one three-trade combo (TP → SL → forced-15:20), one zero-trade combo, an
aligned India-VIX series, and the 1-minute index over the SAME bars — is
captured and persisted, and the results are locked two ways:

1. the sha256 of a canonical JSON serialization of the captured trades +
   equity (every field, full precision), and
2. the sha256 of each sidecar Parquet's bytes as written by
   :func:`~brutex.engine.trade_store.write_tuple_sidecars` (byte-determinism
   proof: writing twice must yield identical bytes, and those bytes must equal
   the locked digests).

Any change to the capture arithmetic, the wire schema, the column order or the
Parquet render knobs moves a digest and MUST be a deliberate, ledgered re-lock
(the ``_GOLDEN_CONTENT_HASH`` discipline).

Regenerate (deterministic) after a DELIBERATE schema change::

    .venv/bin/python -c "import tests.golden.test_engine_trade_capture_golden as g; g.print_digests()"
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from brutex.calendars.nse import IST
from brutex.data.models import CandleBar
from brutex.engine.minute_path import MinuteIndex
from brutex.engine.reference_series import TradeReferenceStamper
from brutex.engine.results_store import ResultRow
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
_GRID_RUN = "grid-feedfacefeedface"

#: Locked digests (sha256 hex). Regeneration is a deliberate, ledgered act.
#: Re-locked 2026-07-08 for gap-register Ruling 18 (lots/costs_paisa became
#: honest NULL-by-design None — the only diff vs the prior lock is exactly
#: those two fields; aggs/equity digests unchanged).
#: Re-locked (AGGS digest ONLY) 2026-07-11 for
#: DEC-TRADESIM-OPEN-SESSION-GUARD-001 (MIGRATION.md #1743): the aggs sidecar
#: SCHEMA gained the ``exits_session_gap`` INT64 column (this fixture writes
#: ZERO agg records, so the diff is the embedded parquet schema alone);
#: canonical/trades/equity digests verified byte-UNCHANGED — captured-trade
#: rows and reason VALUES are untouched on this single-session fixture.
_GOLDEN_CANONICAL_SHA256 = "8cc59fde89b641dffa6f34756e03fb6a2ea4fbcbb848a582097448995c89a2d3"
_GOLDEN_TRADES_SHA256 = "aa75371589cbd59493fdf844ff26be9792e79455a498e3c42fa7309494257183"
_GOLDEN_AGGS_SHA256 = "7f213569cdfc7340d98f7d26620ecb4b916a1a52d8ce404b2544c2b57d02b554"
_GOLDEN_EQUITY_SHA256 = "ed44220996f6f23167b23db37e551da038faf0e1e1d859ba8b71965b782961c2"


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
        max_drawdown_paisa=0,
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


def _capture_fixture() -> list[ComboCapture]:
    index = MinuteIndex.from_bars(_BARS)
    stamper = TradeReferenceStamper.from_vix_bars(_VIX_BARS)
    with_trades = capture_combo(
        _row(_RUN_TRADES, 0b1, [50, -30, 15]),
        bars=_BARS,
        bar_bits=_BAR_BITS,
        tf_minutes=1,
        sl_paisa=30,
        tp_paisa=50,
        minute_index=index,
        stamper=stamper,
    )
    zero = capture_combo(
        _row(_RUN_ZERO, 0b10, []),
        bars=_BARS,
        bar_bits=_BAR_BITS,
        tf_minutes=1,
        sl_paisa=30,
        tp_paisa=50,
        minute_index=index,
        stamper=stamper,
    )
    return [with_trades, zero]


def _canonical_bytes(captures: list[ComboCapture]) -> bytes:
    payload = [
        {
            "run_id": capture.run_id,
            "parity_ok": capture.parity_ok,
            "equity_status": capture.equity_status.value,
            "vix_missing_trades": capture.vix_missing_trades,
            "trades": [
                {
                    "entry_ts": trade.entry_ts.isoformat(),
                    "entry_px": str(trade.entry_px),
                    "exit_ts": trade.exit_ts.isoformat(),
                    "exit_px": str(trade.exit_px),
                    "direction": trade.direction.value,
                    "lots": trade.lots,
                    "exit_reason": trade.exit_reason,
                    "pnl_paisa": trade.pnl_paisa,
                    "costs_paisa": trade.costs_paisa,
                    "mae_paisa": trade.mae_paisa,
                    "mfe_paisa": trade.mfe_paisa,
                    "mae_1m_paisa": trade.mae_1m_paisa,
                    "mfe_1m_paisa": trade.mfe_1m_paisa,
                    "path_1m_status": trade.path_1m_status.value,
                    "vix_entry": None if trade.vix_entry is None else str(trade.vix_entry),
                    "vix_exit": None if trade.vix_exit is None else str(trade.vix_exit),
                }
                for trade in capture.trades
            ],
            "equity": [
                [
                    point.session_date.isoformat(),
                    point.equity_close_paisa,
                    point.equity_intraday_low_paisa,
                    point.trades_closed,
                ]
                for point in capture.equity
            ],
        }
        for capture in captures
    ]
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def test_capture_canonical_serialization_golden() -> None:
    digest = hashlib.sha256(_canonical_bytes(_capture_fixture())).hexdigest()
    assert digest == _GOLDEN_CANONICAL_SHA256, (
        f"captured-trade canonical digest moved: {digest} — any capture-arithmetic "
        "change must be a deliberate, ledgered re-lock"
    )


def test_sidecar_bytes_golden_and_write_twice_identical(tmp_path: Path) -> None:
    root = tmp_path / "sweeps"
    paths = sidecar_paths_for(
        root / "NIFTY" / "1minute" / "bull_k1_feedfacefeedface.parquet",
        root,
        run_id=_GRID_RUN,
    )
    captures = _capture_fixture()
    write_tuple_sidecars(paths, captures, [], store_content_hash=_STORE_HASH)
    first = (
        paths.trades.read_bytes(),
        paths.aggs.read_bytes(),
        paths.equity.read_bytes(),
    )
    write_tuple_sidecars(paths, captures, [], store_content_hash=_STORE_HASH)
    second = (
        paths.trades.read_bytes(),
        paths.aggs.read_bytes(),
        paths.equity.read_bytes(),
    )
    assert first == second, "re-writing the same captures must be byte-identical"
    digests = tuple(hashlib.sha256(content).hexdigest() for content in first)
    assert digests == (
        _GOLDEN_TRADES_SHA256,
        _GOLDEN_AGGS_SHA256,
        _GOLDEN_EQUITY_SHA256,
    ), f"sidecar Parquet bytes moved: {digests} — schema/render changes need a ledgered re-lock"


def print_digests() -> None:  # pragma: no cover - regeneration helper, not a test
    """Print the four digests for a deliberate golden re-lock."""
    import tempfile

    captures = _capture_fixture()
    print("canonical:", hashlib.sha256(_canonical_bytes(captures)).hexdigest())
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "sweeps"
        paths = sidecar_paths_for(
            root / "NIFTY" / "1minute" / "bull_k1_feedfacefeedface.parquet",
            root,
            run_id=_GRID_RUN,
        )
        write_tuple_sidecars(paths, captures, [], store_content_hash=_STORE_HASH)
        for name, path in (
            ("trades", paths.trades),
            ("aggs", paths.aggs),
            ("equity", paths.equity),
        ):
            print(f"{name}:", hashlib.sha256(path.read_bytes()).hexdigest())
