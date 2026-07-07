"""The shared ``MetaKV`` Protocol (DEC-FORM-WINNER-PERSIST-001).

The ONE ``set_meta``/``get_meta`` persistence shape both durable-learned-value
features ride (the vendor depth floor + the ``&``-form winner map). Pins:

* the back-compat alias — ``VendorFloorMetaStore`` IS ``MetaKV`` (a behavioral
  no-op; Protocols are structural, so every existing vendor-floor suite passes
  unmodified);
* runtime satisfaction — a dict-backed fake AND the REAL
  :class:`~brutex.data.instrument_store.InstrumentStore` both satisfy the
  ``@runtime_checkable`` shape; a shapeless object does not.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from brutex.adapters.groww.vendor_floor import VendorFloorMetaStore
from brutex.data.meta_kv import MetaKV

pytest.importorskip("duckdb", reason="InstrumentStore needs duckdb")


class _Fake:
    def __init__(self) -> None:
        self.rows: dict[str, str] = {}

    def set_meta(self, key: str, value: str) -> None:
        self.rows[key] = value

    def get_meta(self, key: str) -> str | None:
        return self.rows.get(key)


def test_vendor_floor_store_is_the_same_protocol() -> None:
    """Alias identity pin: ONE shape, defined once (CLAUDE.md §11 COMMON)."""
    assert VendorFloorMetaStore is MetaKV


def test_fake_and_shapeless_object_runtime_checks() -> None:
    assert isinstance(_Fake(), MetaKV)
    assert not isinstance(object(), MetaKV)


def test_instrument_store_satisfies_meta_kv(tmp_path: Path) -> None:
    """The production store structurally satisfies the Protocol end-to-end."""
    from brutex.data.instrument_store import InstrumentStore

    with InstrumentStore.open(tmp_path / "instruments.duckdb") as store:
        assert isinstance(store, MetaKV)
        store.set_meta("groww.form_winner.M&M", "M%26M")
        assert store.get_meta("groww.form_winner.M&M") == "M%26M"
        assert store.get_meta("absent.key") is None


def test_public_surface() -> None:
    from brutex.data import meta_kv as meta_kv_module

    assert meta_kv_module.__all__ == ["MetaKV"]
