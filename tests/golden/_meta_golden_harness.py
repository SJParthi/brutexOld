"""Shared deterministic fixture builder for the meta-annotation golden
(DEC-META-LABEL-ANNOTATE-001 + DEC-ABSTAIN-GATE-STREAM-001).

ONE builder feeds BOTH the generator (`generate_meta_annotation_golden.py`)
and the byte-equality test (`test_meta_annotation_golden.py`), so the
committed fixture and the test's freshly-built tree can never drift apart
structurally.

The golden capture is DELIBERATELY degenerate-small (12 trades <
``MIN_TRADES_FOR_FIT`` = 30): the OOF fit honestly abstains (every
``p_win`` ``None``), so the frozen ``.meta.json`` bytes carry ZERO
libm/BLAS-dependent floats — the golden pins the SCHEMA + canonical
serialization + reason vocabulary + abstain-block shape, byte-stable on
every box. The float parity surface is pinned separately by the frozen
scorer/abstain vectors in ``fixtures/meta_scorer_vectors_v1.json``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from brutex.engine.minute_path import PathStatus
from brutex.engine.trade_capture import CapturedTrade, ComboCapture
from brutex.engine.trade_store import sidecar_paths_for, write_tuple_sidecars
from brutex.strategies.types import Side

GOLDEN_RUN_ID = "meta-golden-run"
GOLDEN_COMBO = "combo-" + "f" * 10
GOLDEN_STEM = "meta-golden-tuple"
GOLDEN_INSTRUMENT = "NSE-NIFTY"
GOLDEN_TF = "5minute"
N_GOLDEN_TRADES = 12  # < MIN_TRADES_FOR_FIT — the degenerate float-free arm.

FIXTURE_DIRNAME = "meta_annotation_v1"
FIXTURE_META_NAME = f"{GOLDEN_STEM}.meta.json"
FIXTURE_SUMMARY_NAME = "meta_annotation.json"

_BASE_ENTRY = datetime(2024, 1, 2, 4, 15, tzinfo=UTC)  # 09:45 IST


def golden_capture() -> ComboCapture:
    """The frozen 12-trade capture: mixed wins/losses, VIX present + absent."""
    trades = []
    for i in range(N_GOLDEN_TRADES):
        entry = _BASE_ENTRY + timedelta(days=i, minutes=11 * (i % 4))
        win = i % 3 != 0
        trades.append(
            CapturedTrade(
                entry_ts=entry,
                entry_px=Decimal("100.00"),
                exit_ts=entry + timedelta(minutes=25 + i),
                exit_px=Decimal("101.50") if win else Decimal("99.25"),
                direction=Side.BUY,
                lots=None,
                exit_reason="take_profit" if win else "stop_loss",
                pnl_paisa=750 if win else -600,
                costs_paisa=None,
                mae_paisa=15,
                mfe_paisa=40,
                mae_1m_paisa=None,
                mfe_1m_paisa=None,
                path_1m_status=PathStatus.NO_1M_DATA,
                vix_entry=Decimal("13.75") if i % 4 else None,
                vix_exit=None,
            )
        )
    return ComboCapture(
        run_id=GOLDEN_COMBO,
        trades=tuple(trades),
        equity=(),
        equity_status=PathStatus.NO_1M_DATA,
        vix_missing_trades=sum(1 for t in trades if t.vix_entry is None),
        parity_ok=True,
        parity_detail="",
    )


def seed_golden_run(lake: Path) -> Path:
    """Write the golden run's REAL sidecar trio; return the sweeps root."""
    sweeps = lake / "sweeps"
    store = sweeps / GOLDEN_INSTRUMENT / GOLDEN_TF / f"{GOLDEN_STEM}.parquet"
    store.parent.mkdir(parents=True, exist_ok=True)
    store.touch()
    paths = sidecar_paths_for(store, sweeps, run_id=GOLDEN_RUN_ID)
    write_tuple_sidecars(
        paths,
        [golden_capture()],
        [],
        store_content_hash="f" * 64,
    )
    return sweeps


def golden_artifact_paths(lake: Path) -> tuple[Path, Path]:
    """(meta sidecar path, run summary path) for the seeded golden run."""
    run_dir = lake / "sweep_runs" / GOLDEN_RUN_ID
    meta = run_dir / "results-depth" / GOLDEN_INSTRUMENT / GOLDEN_TF / FIXTURE_META_NAME
    return meta, run_dir / FIXTURE_SUMMARY_NAME
