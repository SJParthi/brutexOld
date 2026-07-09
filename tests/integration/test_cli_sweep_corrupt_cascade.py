"""R2-review-round-2 MUST-FIX: a CascadeWireError at the cascade tail REFUSES cleanly.

``cmd_sweep_all`` runs the AUTOMATIC Stage-4 cascade after the grid. A
``CascadeWireError`` can surface there from TWO real classes:

* a CORRUPT / old-schema finalist store reaching ``build_stage2_cascade_specs``
  (DB-02, DEC-SWEEP-STORE-CORRUPT-CONTAIN-001, MIGRATION.md #1641), and
* a disk-full (ENOSPC) at the atomic manifest / report write
  (DEC-CASCADE-SIDECAR-ATOMIC-001, MIGRATION.md #1643).

Both are now mapped at the ``cmd_sweep_all`` CLI boundary to a clean
``[brutex] REFUSED: …`` on stderr + ``EXIT_USER_ERROR`` — exactly like the
sibling ``LmdbCorruptEnvError`` / ``CatalogError`` refusals — so ``brutex
sweep-all`` on the shell never surfaces a RAW TRACEBACK for a cascade fault.

FAILS-BEFORE: on origin the ``except CascadeWireError`` at the cascade block is
absent, so both vectors escape ``cmd_sweep_all`` and (in-process) RAISE past the
function boundary — a bare CLI process would print a Python traceback.

The nightly SWEEP leg reads the ``EXIT_USER_ERROR`` return as a chain-breaking
REFUSED (``sweep_binding.sweep_link_result``), a cron-red exit — no raw
exception escapes ``cmd_sweep_all``.
"""

from __future__ import annotations

import errno
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("polars")
pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")

from brutex.cli import EXIT_USER_ERROR, cmd_sweep_all
from brutex.data.catalog import Catalog
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.sink import ParquetBarSink

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


def _sweep_kwargs(lake_root: Path, catalog_path: Path, results_root: Path) -> dict[str, object]:
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
        "sl": "80,none",
        "tp": "50",
        "top": 5,
        "mode": "A",
        "max_tuples": None,
        "max_combos": None,
        "data_snapshot_hash": None,
        "options_sim": False,
    }


def _finalist_stores(results_root: Path) -> list[Path]:
    """Every per-tuple finalist store parquet (never the dedup-cache / cascade / sweep_runs)."""
    excluded = {"dedup-cache", "cascade", "sweep_runs"}
    return sorted(p for p in results_root.rglob("*.parquet") if not excluded & set(p.parts))


def test_corrupt_finalist_store_reaching_cascade_refuses_clean(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(a) A corrupt finalist store reaching Stage-2 → clean REFUSED, exit 1, no traceback."""
    # Keep the results-depth run-end pass out of this test's surface.
    monkeypatch.setenv("BRUTEX_RESULTS_DEPTH", "0")

    lake_root, catalog_path = _build_lake(tmp_path)
    results_root = tmp_path / "sweeps"

    from brutex.cli import EXIT_OK

    # Run-1 clean: persists the finalist stores + the cascade manifest.
    assert cmd_sweep_all(**_sweep_kwargs(lake_root, catalog_path, results_root)) == EXIT_OK  # type: ignore[arg-type]
    stores = _finalist_stores(results_root)
    assert stores, "the clean run must persist at least one finalist store to corrupt"

    # Corrupt every finalist store on disk so the re-run's cascade Stage-2 read
    # (`ResultsStore.load(...).rows()`) raises ResultsStoreSchemaError → a typed
    # CascadeWireError naming the file.
    for store in stores:
        store.write_bytes(b"CORRUPT\x00\x01" * 64)

    # Run-2 on the SAME results root: the tuples resume from the intact dedup
    # cache / checkpoint (never recomputed), so the corruption survives to the
    # cascade tail — which now REFUSES cleanly at the CLI boundary.
    rc = cmd_sweep_all(**_sweep_kwargs(lake_root, catalog_path, results_root))  # type: ignore[arg-type]
    assert rc == EXIT_USER_ERROR
    err = capsys.readouterr().err
    assert "[brutex] REFUSED:" in err
    assert "UNREADABLE" in err  # the DB-02 cascade wrap names the corrupt store
    assert "Traceback" not in err


def test_enospc_cascade_manifest_write_refuses_clean(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(b) ENOSPC at the atomic cascade manifest write → same clean REFUSED, exit 1."""
    monkeypatch.setenv("BRUTEX_RESULTS_DEPTH", "0")

    lake_root, catalog_path = _build_lake(tmp_path)
    results_root = tmp_path / "sweeps"

    # Fail ONLY the cascade sidecar atomic write (write_manifest / write_report
    # both go through cascade_wire's module-level atomic_write_simple binding);
    # the sweep's own store writes use a DIFFERENT namespace binding and stay
    # clean, so build_stage2_cascade_specs succeeds and the fault lands exactly
    # at the manifest write — the #1643 ENOSPC → CascadeWireError path.
    from brutex.engine import cascade_wire

    def _enospc(*_args: object, **_kwargs: object) -> object:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(cascade_wire, "atomic_write_simple", _enospc)

    rc = cmd_sweep_all(**_sweep_kwargs(lake_root, catalog_path, results_root))  # type: ignore[arg-type]
    assert rc == EXIT_USER_ERROR
    err = capsys.readouterr().err
    assert "[brutex] REFUSED:" in err
    assert "cascade manifest" in err  # the #1643 write-manifest ENOSPC wrap
    assert "Traceback" not in err
