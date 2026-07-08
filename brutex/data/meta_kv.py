"""The ONE ``schema_meta`` key/value persistence Protocol (DEC-FORM-WINNER-PERSIST-001).

Several durable learned-value features persist small per-key strings into the
instrument master's generic ``schema_meta`` sidecar
(:meth:`~brutex.data.instrument_store.InstrumentStore.set_meta` /
:meth:`~brutex.data.instrument_store.InstrumentStore.get_meta` — per-row
``INSERT OR REPLACE``, auto-locked by ``_StoreMeta``, idempotent + rerun-safe):
the learned per-symbol vendor depth floor
(:mod:`brutex.adapters.groww.vendor_floor`, DEC-VENDOR-FLOOR-LAZY-PROBE-001)
and the durable ``&``-symbol form-winner map
(:mod:`brutex.adapters.groww.symbol_form_preference`,
DEC-FORM-WINNER-PERSIST-001). Each had begun growing its OWN structurally
identical two-method Protocol; this module is the single shared shape
(CLAUDE.md §11 COMMON — one source of truth), which
:class:`~brutex.data.instrument_store.InstrumentStore` satisfies structurally.

``@runtime_checkable`` so a test can ``isinstance``-pin that a fake / the real
store satisfies the shape (method-presence only — the honest limit of runtime
Protocol checks).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

__all__ = ["MetaKV"]


@runtime_checkable
class MetaKV(Protocol):
    """A minimal durable string key/value store (the ``schema_meta`` shape)."""

    def set_meta(self, key: str, value: str) -> None:
        """Persist one key/value (idempotent upsert)."""
        raise NotImplementedError  # structural Protocol stub — never executed

    def get_meta(self, key: str) -> str | None:
        """Return a stored value (``None`` if the key is absent)."""
        raise NotImplementedError  # structural Protocol stub — never executed
