"""R2-review-round-2 MUST-FIX: a CascadeWireError at the cascade tail REFUSES cleanly.

``cmd_sweep_all`` runs the AUTOMATIC Stage-4 cascade after the grid. A
``CascadeWireError`` can surface there from TWO real classes:

* a CORRUPT / old-schema finalist store reaching ``build_stage2_cascade_specs``
  (DB-02, DEC-SWEEP-STORE-CORRUPT-CONTAIN-001, MIGRATION.md #1921), and
* a disk-full (ENOSPC) at the atomic manifest / report write
  (DEC-CASCADE-SIDECAR-ATOMIC-001, MIGRATION.md #1923).

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
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("polars")
pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")

from brutex.calendars.nse import get_session
from brutex.cli import EXIT_USER_ERROR, cmd_sweep_all
from brutex.data.catalog import Catalog
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.sink import ParquetBarSink

_IST = timezone(timedelta(hours=5, minutes=30))
_EXCH, _SEG, _SYM, _INT = "NSE", "INDEX", "NIFTY", "1minute"


def _trading_days(start: date, n: int) -> list[date]:
    """The first *n* REAL NSE trading days on/after *start* (calendar-true)."""
    out: list[date] = []
    d = start
    while len(out) < n:
        if get_session(d) is not None:
            out.append(d)
        d += timedelta(days=1)
    return out


def _trending_bars(n_days: int = 40, per_day: int = 12) -> list[CandleBar]:
    """The worst-case-rankable gap-up drift: BUY masks pass the 5 hard gates.

    DEC-FILL-WORST-CASE-ONLY-001 (#1825) re-craft, mirroring the proven
    ``test_sweep_full_chain_rehearsal._bars`` fixture (the original pre-#1825
    zigzag exited every worst-case entry at a NEGATIVE fill — persisted=0 →
    ``mark_cascade_absent()`` — so the cascade manifest write was never
    reached and the ENOSPC bomb in test (b) never fired): narrow gap-up bars
    (range 0.50 < the SL 0.80; the next gap-up's low clears the TP level →
    +50 wins), a shallow bearish bar each 7-bar cycle, and a DEEP bearish bar
    (range 0.85) every 4th cycle whose stop-out keeps profit_factor finite.
    """
    out: list[CandleBar] = []
    cycle = 0
    for day in _trading_days(date(2024, 1, 1), n_days):
        price = 100.0
        for m in range(per_day):
            k = m % 7
            if k == 0:
                cycle += 1
            if k == 3:  # shallow bearish bar
                o = price
                c, h, lo = o - 0.3, o + 0.1, o - 0.4
            elif k == 4 and cycle % 4 == 0:  # deep bearish loss carrier
                o = price
                c, h, lo = o - 0.65, o + 0.1, o - 0.75
            else:  # narrow gap-up win bar
                o = price + 3.0
                c, h, lo = o + 0.3, o + 0.4, o - 0.1
            ts = datetime(day.year, day.month, day.day, 9, 16, tzinfo=_IST) + timedelta(minutes=m)
            out.append(
                CandleBar(
                    timestamp=ts.astimezone(UTC),
                    open=Decimal(str(round(o, 2))),
                    high=Decimal(str(round(h, 2))),
                    low=Decimal(str(round(lo, 2))),
                    close=Decimal(str(round(c, 2))),
                    volume=1000,
                    open_interest=None,
                )
            )
            price = c
    return out


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
        "start": "2024-01-01",
        "end": "2024-03-05",
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
    # at the manifest write — the #1923 ENOSPC → CascadeWireError path.
    from brutex.engine import cascade_wire

    def _enospc(*_args: object, **_kwargs: object) -> object:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(cascade_wire, "atomic_write_simple", _enospc)

    rc = cmd_sweep_all(**_sweep_kwargs(lake_root, catalog_path, results_root))  # type: ignore[arg-type]
    assert rc == EXIT_USER_ERROR
    err = capsys.readouterr().err
    assert "[brutex] REFUSED:" in err
    assert "cascade manifest" in err  # the #1923 write-manifest ENOSPC wrap
    assert "Traceback" not in err

    # ANTI-ROT fixture sanity (the #1825 fill-law rot class): the refusal above
    # is meaningful only because the fixture genuinely ranked >= 1 gate-passing
    # finalist UNDER WORST-CASE FILLS — with zero finalists cmd_sweep_all takes
    # the mark_cascade_absent() branch and never reaches the manifest write, so
    # the ENOSPC bomb would sit unexercised. The finalist stores were written
    # BEFORE the cascade tail through results_store's OWN atomic binding (the
    # monkeypatch above hits only cascade_wire's), so they are readable here; a
    # future fill/gate-law change that rots this fixture fails LOUDLY at the
    # fixture, never a vacuous pass.
    from brutex.engine.results_store import ResultsStore

    finalist_rows = [
        row for store in _finalist_stores(results_root) for row in ResultsStore.load(store).rows()
    ]
    assert finalist_rows, (
        "fixture rot: no gate-passing finalist persisted under worst-case fills — "
        "the cascade manifest write (and the ENOSPC bomb) was never reachable"
    )
