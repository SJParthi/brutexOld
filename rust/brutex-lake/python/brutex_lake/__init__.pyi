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

def resample_1m_ipc(ipc_bytes: bytes, target_tf_minutes: int, oi_aware: bool) -> bytes:
    """Resample a 1-minute frame (uncompressed Arrow IPC bytes) to ONE TF.

    Returns the derived frame's uncompressed Arrow IPC bytes under the
    09:15-IST session-open-anchored bucket law (Wave B, slice G5 —
    DEC-LAKE-RUST-RESAMPLE-001). Raises ``ValueError`` on an
    empty/undecodable payload or a polars error; ``RuntimeError`` if the
    compute panicked (caught at the FFI boundary — a panic never crosses
    raw).
    """

def resample_all_ipc(ipc_bytes: bytes, target_tf_minutes: list[int], oi_aware: bool) -> list[bytes]:
    """Batch twin of ``resample_1m_ipc`` — one decode, one payload per TF."""

def render_stats_shard_json(
    version: int,
    kind: str,
    key: tuple[tuple[str, str], ...],
    identity_key: tuple[str, ...],
    row_count: int,
    min_ts_iso: str,
    max_ts_iso: str,
    files: tuple[tuple[str, int, str, str, int, int, str | None], ...],
    updated_utc: str,
) -> str:
    """Render the canonical stats-shard JSON (DEC-LAKE-RUST-STATS-MANIFEST-001).

    Byte-identical to ``lake_stats_manifest.render_shard``'s
    ``json.dumps(payload, sort_keys=True, separators=(",", ":"))`` for the
    same pre-formatted payload (ints + isoformat strings only — no floats
    ever cross this boundary). Raises ``TypeError``/``OverflowError``/
    ``UnicodeEncodeError`` on extraction failures and ``RuntimeError`` if
    the assembly panicked (caught at the FFI boundary).
    """

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
