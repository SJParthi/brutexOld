"""``lake_heal._quarantine_move`` ↔ lake-manifest invalidation (DEC-LAKE-MANIFEST-O1-001).

The heal batch's ONE file-removal chokepoint (`_quarantine_move` — used by the
whole-file quarantine AND the intraday-TF delete-and-rebuild) must invalidate
the durable dataset manifest, so a removal can never leave the manifest
over-listing a now-empty dataset.

Whitebox per the DEC-P0-017 convention (drives the private ``_BatchState`` +
``_quarantine_move`` directly for a deterministic, isolated pin — the public
``heal_findings`` routes are already exercised end-to-end by
``test_lake_heal.py``), so ``reportPrivateUsage`` is silenced.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

from pathlib import Path

import pytest
from brutex.data import lake_manifest as lm
from brutex.data.lake_heal import _BatchState, _quarantine_move
from brutex.data.option_parquet_writer import OptionParquetWriter
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.window_ledger_store import ledger_path


@pytest.fixture(autouse=True)
def _python_oracle_arm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin this suite to the EXPLICIT flag-OFF (Python oracle) arm.

    Conscious W-5 reconciliation (recorded in the Wave-D lane scratchpad):
    since the ``BRUTEX_LAKE_WAVED_RUST`` default flipped ON, an unset env
    routes the heal through the crate core — but THIS suite is the verbatim
    PYTHON core's verification (the live degrade oracle every out-of-envelope
    shape still serves; its 100% ci-gate coverage is the oracle's gate), so
    it runs under the operator kill switch. The crate arm's gates are the
    heal differential (``tests/integration/test_waved_heal_differential.py``,
    default-ON) + the seam suite's explicit flag-ON tests.
    """
    monkeypatch.setenv("BRUTEX_LAKE_WAVED_RUST", "0")


def test_quarantine_move_invalidates_manifest(tmp_path: Path) -> None:
    """A quarantine-MOVE removes a stored file → the manifest is invalidated."""
    lake = tmp_path
    ledger = ledger_path(lake)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_bytes(b"ledger-v1")
    token = lm.ledger_stat_token(lake)
    assert token is not None
    row = {"exchange": "NSE", "segment": "INDEX", "symbol": "NIFTY", "interval": "1minute"}
    assert lm.rebuild_manifest(lake, [row], ledger_token=token) is True
    assert lm.load_manifest_datasets(lake) == [row]

    source = lake / "bars" / "NSE" / "INDEX" / "NIFTY" / "1minute" / "2024" / "06.parquet"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"poisoned-partition-bytes")
    state = _BatchState(
        lake_root=lake,
        quarantine_root=lake / ".quarantine" / "20260705T000000Z",
        audit=None,
        catalog=None,
        writer=ParquetWriter(lake_root=lake),
        option_writer=OptionParquetWriter(lake_root=lake),
    )
    dest = _quarantine_move(state, source)

    assert not source.exists()  # moved away
    assert dest.read_bytes() == b"poisoned-partition-bytes"  # quarantine preserved
    assert state.quarantined_files == 1
    # The manifest is INVALIDATED — the next discovery walks + rebuilds
    # instead of over-listing the possibly-emptied dataset.
    assert not lm.manifest_meta_path(lake).exists()
    assert not lm.manifest_data_path(lake).exists()
    assert lm.load_manifest_datasets(lake) is None
