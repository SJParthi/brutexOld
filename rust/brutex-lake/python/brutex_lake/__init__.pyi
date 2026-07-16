# Typing surface for the brutex_lake PyO3 bridge (ADR-0020 PR-1).
# Mirrors rust/brutex-lake/src/lib.rs — keep in lockstep with the
# `#[pymodule]` registration block there.

__version__: str

def render_parquet_from_ipc(ipc_bytes: bytes) -> bytes:
    """Render uncompressed Arrow IPC bytes to parquet bytes.

    Byte-identical to the pinned Python wheel's
    ``DataFrame.write_parquet(use_pyarrow=False)`` under the BruteX
    bars-writer options (zstd level 3, row groups of 100 000, statistics
    off). Raises ``ValueError`` on an empty/undecodable payload or a
    polars render error; ``RuntimeError`` if the render panicked (caught
    at the FFI boundary — a panic never crosses raw).
    """

def merge_decision_from_ipc(
    stored_ipc: bytes, batch_ipc: bytes, keep_first: bool
) -> list[int]:
    """Decide the merge fold — one action code per batch row (ADR-0020 PR-3).

    ``0=skip · 1=insert · 2=overwrite · 3=conflict`` against the stored
    frame's keep-first winners; both frames cross as uncompressed Arrow IPC
    of the 17-column decision schema. Integers only — Python materializes.
    Raises ``ValueError`` on an empty/undecodable payload, a missing
    decision column or a null timestamp; ``RuntimeError`` if the decision
    panicked (caught at the FFI boundary — a panic never crosses raw).
    """

def pinned_polars_version() -> str:
    """The Python polars wheel version this crate byte-matches (e.g. ``"1.42.1"``)."""

def linked_zstd_version() -> str:
    """The libzstd version linked into this build (runtime-probed, e.g. ``"1.5.7"``)."""
