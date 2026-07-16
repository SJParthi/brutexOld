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

def pinned_polars_version() -> str:
    """The Python polars wheel version this crate byte-matches (e.g. ``"1.42.1"``)."""

def linked_zstd_version() -> str:
    """The libzstd version linked into this build (runtime-probed, e.g. ``"1.5.7"``)."""

def classify_window_status(raw_bars: int, error_type: str | None, degraded: bool) -> str:
    """The window-ledger status law (ADR-0020 Wave C-3 / G8).

    Mirrors ``brutex.data.window_status.classify_window_status`` token for
    token: error present → ``"FAILED"``; ``raw_bars > 0`` → ``"FAILED"`` if
    ``degraded`` else ``"SUCCESS"``; else ``"EMPTY"``. Pure + infallible over
    the ``i64`` domain (an out-of-``i64`` int raises ``OverflowError`` at the
    PyO3 conversion — the facade's degrade set).
    """

def date_isoformat(year: int, month: int, day: int) -> str:
    """``datetime.date.isoformat()`` mirror — zero-padded ``YYYY-MM-DD``.

    Raises ``ValueError`` on an out-of-range component (year 1..=9999,
    month 1..=12, day 1..=31 — structure only; production-unreachable
    through the facade, which passes real ``date`` fields).
    """
