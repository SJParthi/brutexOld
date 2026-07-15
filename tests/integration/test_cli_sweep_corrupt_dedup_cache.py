"""R2-D2 CLI byte-parity tooth: a corrupt dedup-cache LMDB self-heals; results stay identical.

DEC-SWEEP-LMDB-QUARANTINE-REBUILD-001, MIGRATION.md #1795. The Bloom→LMDB combo
dedup cache is a losslessly-rebuildable ACCELERATOR (Golden Rule #7): a corrupt
``data.mdb`` is quarantined aside + rebuilt EMPTY, so the affected combos merely
recompute — the persisted finalist stores are byte-identical to a
never-corrupted run.

VECTOR (reproducer_catalog): run-1 clean ``cmd_sweep_all`` builds the dedup
cache; XOR the first 4096 bytes of ``sweeps/dedup-cache/exact/data.mdb``; re-run
``cmd_sweep_all`` on the SAME results root → it recovers + exits ``EXIT_OK`` with
the finalist parquet bytes matching a separate never-corrupted run.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("polars")
pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")

from brutex.cli import EXIT_OK, cmd_sweep_all
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


def _finalist_hashes(results_root: Path) -> dict[str, str]:
    """{tuple-store rel-path → sha256} for every finalist parquet under *results_root*."""
    out: dict[str, str] = {}
    for p in sorted(results_root.rglob("*.parquet")):
        if "dedup-cache" in p.parts:
            continue
        out[str(p.relative_to(results_root))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def test_corrupt_dedup_cache_recovers_and_results_byte_identical(tmp_path: Path) -> None:
    # A separate, never-corrupted reference run.
    ref_root = tmp_path / "ref"
    ref_lake, ref_catalog = _build_lake(ref_root)
    assert cmd_sweep_all(**_sweep_kwargs(ref_lake, ref_catalog, ref_root / "sweeps")) == EXIT_OK  # type: ignore[arg-type]
    ref_hashes = _finalist_hashes(ref_root / "sweeps")
    assert ref_hashes  # the reference run persisted at least one finalist store

    # The test run: build the same lake, run-1 clean, then corrupt the dedup cache.
    test_root = tmp_path / "test"
    test_lake, test_catalog = _build_lake(test_root)
    results_root = test_root / "sweeps"
    assert cmd_sweep_all(**_sweep_kwargs(test_lake, test_catalog, results_root)) == EXIT_OK  # type: ignore[arg-type]

    data_mdb = results_root / "dedup-cache" / "exact" / "data.mdb"
    assert data_mdb.exists()
    raw = bytearray(data_mdb.read_bytes())
    for i in range(min(4096, len(raw))):
        raw[i] ^= 0xFF
    data_mdb.write_bytes(bytes(raw))

    # Run-2 on the SAME results root: the corrupt cache self-heals (quarantine +
    # rebuild empty), the sweep completes, and the finalist parquet bytes match
    # the never-corrupted reference run exactly (idempotent recompute).
    assert cmd_sweep_all(**_sweep_kwargs(test_lake, test_catalog, results_root)) == EXIT_OK  # type: ignore[arg-type]
    assert _finalist_hashes(results_root) == ref_hashes
    # The corrupt env was quarantined aside (bytes preserved, never deleted).
    assert list((results_root / "dedup-cache").glob("exact.corrupt-*"))


def test_audit_writer_open_failure_still_closes_dedup_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review round 1 MINOR-2 (3-reviewer consensus): no LMDB env leak.

    ``cmd_sweep_all`` opens the dedup ``SweepCache`` BEFORE the audit writers;
    the cache is entered FIRST in the with-tuple so a raise from either
    audit-writer open still closes the already-open LMDB env (the pre-change
    ExitStack-era guarantee). Fails-before: with the cache entered LAST, a
    raising ``AuditWriter.open`` left the env open — the close spy stayed empty.
    """
    from brutex.engine.sweep_cache import SweepCache
    from brutex.orchestrator.audit import AuditWriter

    lake_root, catalog_path = _build_lake(tmp_path)
    closed: list[bool] = []
    orig_close = SweepCache.close

    def _spy_close(self: SweepCache) -> None:
        closed.append(True)
        orig_close(self)

    def _audit_boom(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("simulated audit-writer open failure")

    monkeypatch.setattr(SweepCache, "close", _spy_close)
    monkeypatch.setattr(AuditWriter, "open", _audit_boom)
    with pytest.raises(RuntimeError, match="simulated audit-writer open failure"):
        cmd_sweep_all(**_sweep_kwargs(lake_root, catalog_path, tmp_path / "sweeps"))  # type: ignore[arg-type]
    assert closed, "the already-open SweepCache LMDB env must be closed on entry failure"


def test_raw_lmdb_error_from_cache_open_is_clean_refusal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Review round 1 NIT belt: a raw ``lmdb.Error`` escaping ``SweepCache.open``.

    The attempt-2 mid-Bloom-rebuild window can surface a raw ``lmdb.Error``
    (not the typed ``LmdbCorruptEnvError``); the CLI catch is widened to
    ``(LmdbCorruptEnvError, lmdb.Error)`` so it reads a clean ``[brutex]
    REFUSED:`` + ``EXIT_USER_ERROR``, never a traceback.
    """
    import lmdb
    from brutex.cli import EXIT_USER_ERROR
    from brutex.engine.sweep_cache import SweepCache

    lake_root, catalog_path = _build_lake(tmp_path)

    def _boom(*_args: object, **_kwargs: object) -> object:
        raise lmdb.Error("simulated raw lmdb fault escaping the attempt-2 reopen")

    monkeypatch.setattr(SweepCache, "open", _boom)
    rc = cmd_sweep_all(**_sweep_kwargs(lake_root, catalog_path, tmp_path / "sweeps"))  # type: ignore[arg-type]
    assert rc == EXIT_USER_ERROR
    err = capsys.readouterr().err
    assert "[brutex] REFUSED:" in err
    assert "simulated raw lmdb fault" in err
