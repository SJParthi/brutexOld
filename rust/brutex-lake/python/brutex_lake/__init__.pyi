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

def lock_try_exclusive(fd: int) -> bool:
    """One non-blocking exclusive ``flock`` attempt on a caller-owned raw fd.

    ADR-0020 PR-4 (DEC-LAKE-RUST-LOCKS-ATOMIC-001). ``True`` = acquired,
    ``False`` = a peer holds it (the ``BlockingIOError`` shape); any other
    failure raises a real ``OSError`` with the errno intact. The bounded
    retry loop / poll / progress-beat / ``PartitionLockTimeout`` law stay
    on the Python facade. ``RuntimeError`` if the attempt panicked (caught
    at the FFI boundary).
    """

def lock_release(fd: int) -> None:
    """Release the advisory lock on *fd* and close it (``LOCK_UN`` + ``close``).

    Ownership of the fd transfers to this call — on return (success or
    error) the fd must not be reused. A close failure raises a real
    ``OSError`` with the errno intact; ``RuntimeError`` on a panic.
    """

def atomic_commit_replace(tmp_path: str, dest: str, chmod: int | None = None) -> None:
    """The chmod→``fsync``→``rename`` commit of an already-written temp file.

    Order-exact twin of ``atomic_io._atomic_write_core``'s commit tail
    (``os.replace`` semantics — atomically replaces an existing *dest*).
    On ANY error the temp is left in place (the Python core's ``finally``
    owns cleanup) and a real ``OSError`` with the errno intact raises —
    ``ENOSPC``/``EDQUOT`` reach ``lake_write_guard`` unchanged.
    ``RuntimeError`` on a panic.
    """

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

def raw_integrity_blake2b_hex(data: bytes, digest_size: int) -> str:
    """The unkeyed blake2b digest of *data* as lowercase hex (SLICE T24 PR-1).

    The ``hashlib.blake2b(data, digest_size=n).hexdigest()`` twin (RFC 7693,
    in-crate). ``ValueError`` for a ``digest_size`` outside ``1..=64``;
    ``RuntimeError`` on a caught panic.
    """

def interior_barless_runs(
    covered: tuple[int, ...] | list[int],
    interior_sessions: tuple[int, ...] | list[int],
) -> list[tuple[int, int, int]]:
    """Maximal bar-less interior session runs over date ordinals (T24 PR-1).

    Ints in, ``(run_first, run_last, run_sessions)`` 3-tuples out (bounds
    inclusive, ascending). ``RuntimeError`` on a caught panic.
    """

def classify_partial_window_verdict(
    last_bar_ordinal: int | None,
    window_end_ordinal: int,
    today_ordinal: int,
    expiry_ordinal: int | None,
    coverage_ok: bool,
    tolerance: int,
    live_edge: int,
    span_start_ordinal: int,
    trading_flags: tuple[bool, ...] | list[bool],
) -> tuple[str, int | None, int | None, int] | None:
    """The partial-window truncation verdict over a session grid (T24 PR-1).

    ``None`` = a session walk stepped off the grid (the ABSTAIN contract —
    the facade serves the Python oracle). ``RuntimeError`` on a caught
    panic.
    """

def classify_today_completeness_verdict(
    instrument_class: int,
    expected_minutes: int,
    minute_ordinals: tuple[int, ...] | list[int],
    zero_volume: tuple[bool, ...] | list[bool],
    oi_null: tuple[bool, ...] | list[bool],
    last_bar_complete: bool,
) -> tuple[bool, int, int, int, int, bool]:
    """The pre-settle TODAY minute-completeness verdict (SLICE T24 PR-2).

    The per-class criteria tree of
    ``brutex/data/today_completeness.py::classify_today_completeness``
    (#1774) over ints/bools: returns ``(incomplete, reason_idx,
    distinct_minutes, oi_null_count, zero_volume_count,
    count_short_observed)`` — the reason crosses as an index into the
    facade's frozen reason tuple. ``ValueError`` on a column-length
    divergence or an unknown class code; ``RuntimeError`` on a caught
    panic.
    """
