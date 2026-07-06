"""InstrumentStore DuckDB thread-bounding tests (DEC-THREAD-BUDGET-BOUNDS-001).

``InstrumentStore.open`` bounds its connection's thread pool at creation —
DuckDB defaults to ALL logical cores PER CONNECTION, so the dashboard's
concurrently-live connections (instrument master + registry + ad-hoc readers)
would otherwise stack several full-width pools. The bound changes query
PARALLELISM only; results are byte-identical (pinned here by a real read
through the bounded connection).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from brutex.config.hardware_envelope import duckdb_thread_budget
from brutex.data.instrument_store import InstrumentStore

if TYPE_CHECKING:
    from pathlib import Path


def _threads_setting(store: InstrumentStore) -> int:
    row = store.connection.execute("SELECT current_setting('threads')").fetchone()
    assert row is not None
    return int(row[0])


class TestInstrumentStoreThreadBound:
    def test_open_defaults_to_envelope_budget(self, tmp_path: Path) -> None:
        with InstrumentStore.open(tmp_path / "master.duckdb") as store:
            assert _threads_setting(store) == duckdb_thread_budget()

    def test_open_honours_explicit_threads(self, tmp_path: Path) -> None:
        with InstrumentStore.open(tmp_path / "master.duckdb", threads=1) as store:
            assert _threads_setting(store) == 1
            # The bound changes parallelism only — a real query still works.
            row = store.connection.execute("SELECT COUNT(*) FROM instruments").fetchone()
            assert row is not None
            assert row[0] == 0
