"""Golden pins for the results-store optional-column back-compat fixtures.

DEC-VIX-STAMP-WIRE-001 (Stage C): the committed pre-column Parquet corpora in
``fixtures/`` (written by ``generate_results_store_backcompat_fixtures.py``)
freeze the two HISTORICAL results-store layouts, and this suite pins three
contracts against the REAL legacy bytes:

1. **Byte-freeze** — each committed fixture's SHA-256 matches the manifest
   (any unintended drift of the committed files fails here), and the
   generator re-renders the exact committed bytes (regenerate-safe: the
   script and the files can never silently diverge).
2. **The optional-column reader law** — :meth:`ResultsStore.load` reads a
   store written BEFORE the ``vix_mean`` / ``vix_min`` / ``vix_max`` trio
   (and, one generation older, before ``deflated_sharpe``) as ``None`` — the
   honest unknown, NEVER a fabricated ``0.0`` (Golden Rule #1).
3. **Forward migration** — re-flushing a legacy load writes the CURRENT
   schema (the trio present as nulls) and round-trips losslessly.

The generator's byte-fidelity to the real pre-Stage-C writer was proven at
generation time by rendering the same rows through the actual pre-change
``ResultsStore._render_bytes`` (git ``HEAD`` before Stage C) — identical
SHA-256 ``da9b0e89…`` — so these fixtures ARE the historical format, not a
reconstruction. Like every store-bytes golden (see
``test_engine_sweep_run_golden.py``), the byte pins are polars/zstd-build
sensitive; the load-law assertions are the version-robust contract.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Final

import pytest
from brutex.engine.results_store import ResultsStore

from tests.golden.generate_results_store_backcompat_fixtures import (
    PRE_DEFLATED_FIXTURE,
    PRE_VIX_FIXTURE,
    fixture_rows,
    render_legacy_store_bytes,
)

#: SHA-256 manifest of the committed fixture bytes (drift fails loudly).
_EXPECTED_SHA256: Final[dict[str, str]] = {
    "results_store_pre_vix_columns.parquet": (
        "da9b0e89fb052d8608f7181028718f4121b37bdf10cb0ad8800999aec73292ca"
    ),
    "results_store_pre_deflated_sharpe.parquet": (
        "e346e0f4b3394910c6dfc1448dbdff0d1ade0dde665ee4841223335dc1250a87"
    ),
}


def test_committed_fixture_bytes_match_manifest() -> None:
    """Byte-freeze: each committed fixture hashes to its pinned SHA-256."""
    for path in (PRE_VIX_FIXTURE, PRE_DEFLATED_FIXTURE):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        assert digest == _EXPECTED_SHA256[path.name], path.name


def test_generator_reproduces_the_committed_bytes() -> None:
    """Regenerate-safe: the generator re-renders the exact committed bytes."""
    pytest.importorskip("polars")  # rendering needs the [data] extra
    rows = fixture_rows()
    assert render_legacy_store_bytes(rows, with_deflated_sharpe=True) == (
        PRE_VIX_FIXTURE.read_bytes()
    )
    assert render_legacy_store_bytes(rows, with_deflated_sharpe=False) == (
        PRE_DEFLATED_FIXTURE.read_bytes()
    )


def test_pre_vix_store_loads_with_none_trio() -> None:
    """A pre-VIX-column store loads: trio ``None``, ``deflated_sharpe`` intact."""
    pytest.importorskip("polars")
    loaded = ResultsStore.load(PRE_VIX_FIXTURE)
    assert len(loaded) == 2
    for row in loaded.rows():
        assert (row.vix_mean, row.vix_min, row.vix_max) == (None, None, None)
    # deflated_sharpe (already a column in this generation) round-trips both arms.
    assert [row.deflated_sharpe for row in loaded.rows()] == [0.25, None]


def test_pre_deflated_store_loads_with_none_deflated_and_none_trio() -> None:
    """The older generation loads: ``deflated_sharpe`` AND the trio ``None``."""
    pytest.importorskip("polars")
    loaded = ResultsStore.load(PRE_DEFLATED_FIXTURE)
    assert len(loaded) == 2
    for row in loaded.rows():
        assert row.deflated_sharpe is None
        assert (row.vix_mean, row.vix_min, row.vix_max) == (None, None, None)


def test_legacy_load_reflushes_to_the_current_schema_losslessly(tmp_path: Path) -> None:
    """Load-old → flush-new → load-new round-trips every row unchanged.

    The re-flushed file carries the CURRENT schema — the VIX trio + the
    growth trio (DEC-METRICS-LOSSRATE-FIRSTCLASS-001 /
    DEC-METRICS-KELLY-GROWTH-001) present as Parquet nulls (checked
    structurally, not by byte pin) — so a migrated store is immediately
    backfillable by a stamped redo.
    """
    pl = pytest.importorskip("polars")
    legacy = ResultsStore.load(PRE_VIX_FIXTURE)
    migrated_path = tmp_path / "migrated.parquet"
    migrated = ResultsStore(path=migrated_path)
    migrated_count = migrated.extend(legacy.rows())
    assert migrated_count == 2
    migrated.flush()
    assert ResultsStore.load(migrated_path).rows() == legacy.rows()
    schema = pl.read_parquet_schema(migrated_path)
    # Ruling-5 appended-LAST order: the VIX trio, then the growth trio.
    assert [*schema][-6:] == [
        "vix_mean",
        "vix_min",
        "vix_max",
        "loss_rate",
        "kelly_fraction",
        "log_growth_at_half_kelly",
    ]
