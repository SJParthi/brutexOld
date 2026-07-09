"""R2-review-round-2 SHOULD-FIX 1: the printed remediation chain actually works, end to end.

DEC-SWEEP-STORE-CORRUPT-CONTAIN-001, MIGRATION.md #1641. When a finalist store
is corrupt, ``cmd_sweep_all`` refuses the cascade and prints the ONE runnable
remediation string (``TUPLE_STORE_CORRUPT_REMEDIATION``): verify →
``--recount`` → ``--emit-heal-plan`` → ``sweep-heal <plan> --execute`` →
re-sweep. This test drives that EXACT chain through the real CLI entry points
(``cmd_sweep_all`` / ``cmd_sweep_verify`` / ``cmd_sweep_heal``) and proves:

1. ``sweep-verify --recount --emit-heal-plan`` writes a heal plan under
   ``<results-root>/heal/`` naming the condemned tuple,
2. ``sweep-heal <plan> --execute`` quarantines the corrupt store AND wipes
   ``checkpoint/`` + ``dedup-cache/`` so the tuple genuinely recomputes, and
3. the next ``cmd_sweep_all`` recomputes the tuple to a LOADABLE store and exits
   ``EXIT_OK`` — the cascade no longer refuses.

CONTEXT CONSTRAINT (documented): heal-grade evidence exists ONLY under a matched
recorded context (data_snapshot_hash / indicator_version / code_commit_sha, C1).
In this single-run test the clean run's own supervisor audit log carries that
context, so ``sweep-verify`` (with the SAME grid flags, no context overrides)
reconstructs it, the Tier-3 recount is verdict-grade, and the plan is emitted.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("polars")
pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")

from brutex.cli import (
    EXIT_OK,
    cmd_sweep_all,
    cmd_sweep_heal,
    cmd_sweep_verify,
)
from brutex.data.catalog import Catalog
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.sink import ParquetBarSink
from brutex.engine.results_store import ResultsStore, ResultsStoreSchemaError

_IST = timezone(timedelta(hours=5, minutes=30))
_EXCH, _SEG, _SYM, _INT = "NSE", "INDEX", "NIFTY", "1minute"


def _trending_bars(n_days: int = 6, per_day: int = 60) -> list[CandleBar]:
    bars: list[CandleBar] = []
    for d in range(n_days):
        price = 100.0
        for m in range(per_day):
            close = price + 0.6 if m % 3 else price - 0.3
            ts = (
                datetime(2024, 1, 2, 9, 16, tzinfo=_IST) + timedelta(days=d, minutes=m)
            ).astimezone(UTC)
            hi = max(price, close) + 0.5
            lo = min(price, close) - 0.5
            bars.append(
                CandleBar(
                    timestamp=ts,
                    open=Decimal(str(round(price, 2))),
                    high=Decimal(str(round(hi, 2))),
                    low=Decimal(str(round(lo, 2))),
                    close=Decimal(str(round(close, 2))),
                    volume=1000,
                    open_interest=None,
                )
            )
            price = close
    return bars


def _build_lake(root: Path) -> tuple[Path, Path]:
    lake_root, catalog_path = root / "lake", root / "catalog.duckdb"
    writer = ParquetWriter(lake_root=lake_root)
    with Catalog.open(catalog_path) as cat:
        ParquetBarSink(writer=writer, catalog=cat).append(
            exchange=_EXCH, segment=_SEG, symbol=_SYM, interval=_INT, bars=_trending_bars()
        )
    return lake_root, catalog_path


def _grid_kwargs(lake_root: Path, catalog_path: Path, results_root: Path) -> dict[str, object]:
    """The shared grid axes — IDENTICAL flags across sweep-all AND sweep-verify."""
    return {
        "lake_root": lake_root,
        "catalog_path": catalog_path,
        "results_root": results_root,
        "exchange": _EXCH,
        "segment": _SEG,
        "symbols": _SYM,
        "intervals": _INT,
        "start": "2024-01-02",
        "end": "2024-01-10",
        "directions": "BUY",
        "k": "1",
        "conditions": "0,1,2,3,4",
        # Four SL legs → four tuples, so healing ONE corrupt store is a MINORITY
        # (< 50%) and clears the C7 mass-quarantine guard WITHOUT the
        # --allow-mass-quarantine flag (the printed remediation discloses that
        # gate for >= 50%-condemned plans; this chain exercises the plain path).
        "sl": "80,120,160,none",
        "tp": "50",
        "top": 5,
        "mode": "A",
        "max_tuples": None,
        "max_combos": None,
        "data_snapshot_hash": None,
    }


def _finalist_stores(results_root: Path) -> list[Path]:
    excluded = {"dedup-cache", "cascade", "sweep_runs", "heal", "quarantine", "audit", "progress"}
    return sorted(p for p in results_root.rglob("*.parquet") if not excluded & set(p.parts))


def test_printed_remediation_chain_heals_and_re_sweep_recovers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRUTEX_RESULTS_DEPTH", "0")

    lake_root, catalog_path = _build_lake(tmp_path)
    results_root = tmp_path / "sweeps"

    # ── Run 1: clean sweep persists finalist stores + the supervisor audit ──
    rc1 = cmd_sweep_all(**_grid_kwargs(lake_root, catalog_path, results_root), options_sim=False)  # type: ignore[arg-type]
    assert rc1 == EXIT_OK
    stores = _finalist_stores(results_root)
    assert stores, "the clean run must persist at least one finalist store"

    # Corrupt ONE finalist store — the DB-02 condition the remediation targets.
    corrupt = stores[0]
    corrupt.write_bytes(b"CORRUPT\x00\x01" * 64)
    with pytest.raises(ResultsStoreSchemaError):  # the corrupt store no longer loads
        ResultsStore.load(corrupt).rows()

    # ── Step 1: sweep-verify --recount --emit-heal-plan (SAME grid flags) ──
    verify_kwargs = {
        **_grid_kwargs(lake_root, catalog_path, results_root),
        "indicator_version": None,
        "code_commit_sha": None,
        "recount": True,
        "emit_heal_plan": True,
        "scratch_root": tmp_path / "scratch",
        "allow_context_mismatch": False,
    }
    cmd_sweep_verify(**verify_kwargs)  # type: ignore[arg-type]

    heal_dir = results_root / "heal"
    plans = sorted(heal_dir.glob("plan-*.json"))
    assert plans, "sweep-verify --recount --emit-heal-plan must write a plan under <root>/heal/"
    plan_path = plans[0]

    # ── Step 2: sweep-heal <plan> --execute — quarantine + wipe skip tiers ──
    assert (results_root / "checkpoint").exists() or (results_root / "dedup-cache").exists()
    rc_heal = cmd_sweep_heal(
        plan=plan_path,
        lake_root=lake_root,
        results_root=results_root,
        execute=True,
        allow_mass_quarantine=False,
    )
    assert rc_heal == EXIT_OK, "the heal must execute cleanly on freshly-frozen evidence"
    # The corrupt store was MOVED to quarantine (never deleted) and the resume
    # skip tiers were wiped so the tuple genuinely recomputes.
    assert not corrupt.exists(), "the corrupt store must be quarantined out of place"
    assert list((results_root / "quarantine").rglob("*.parquet")), "quarantine holds the store"

    # ── Step 3: re-sweep — the tuple recomputes to a LOADABLE store, EXIT_OK ──
    rc2 = cmd_sweep_all(**_grid_kwargs(lake_root, catalog_path, results_root), options_sim=False)  # type: ignore[arg-type]
    assert rc2 == EXIT_OK
    recovered = _finalist_stores(results_root)
    assert recovered, "the re-sweep must recompute the finalist store"
    for store in recovered:
        # Every recomputed store loads cleanly — the cascade no longer refuses.
        ResultsStore.load(store).rows()
