"""RegistryStore DuckDB thread-bounding tests (DEC-THREAD-BUDGET-BOUNDS-001).

``RegistryStore.open`` bounds its connection's thread pool at creation —
DuckDB defaults to ALL logical cores PER CONNECTION; see the sibling
``test_instrument_store_threads.py`` for the rationale. Parallelism only —
results are byte-identical.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from brutex.config.hardware_envelope import duckdb_thread_budget
from brutex.data.registry_store import RegistryStore

if TYPE_CHECKING:
    from pathlib import Path


def _threads_setting(store: RegistryStore) -> int:
    # The registry deliberately exposes no public connection — probing the
    # session setting is a test-only white-box read.
    row = store._conn.execute("SELECT current_setting('threads')").fetchone()
    assert row is not None
    return int(row[0])


class TestRegistryStoreThreadBound:
    def test_open_defaults_to_envelope_budget(self, tmp_path: Path) -> None:
        store = RegistryStore.open(tmp_path / "registry.duckdb")
        try:
            assert _threads_setting(store) == duckdb_thread_budget()
        finally:
            store.close()

    def test_open_honours_explicit_threads(self, tmp_path: Path) -> None:
        store = RegistryStore.open(tmp_path / "registry.duckdb", threads=1)
        try:
            assert _threads_setting(store) == 1
        finally:
            store.close()
