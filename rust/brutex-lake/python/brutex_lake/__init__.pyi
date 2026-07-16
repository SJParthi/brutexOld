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

def resample_all_ipc(
    ipc_bytes: bytes, target_tf_minutes: list[int], oi_aware: bool
) -> list[bytes]:
    """Batch twin of ``resample_1m_ipc`` — one decode, one payload per TF."""

def daily_restamp_ipc(ipc_bytes: bytes, before_days: int, keep_oi: bool) -> bytes:
    """Filter + midnight-IST-restamp an ALREADY-RESAMPLED daily (tf=375) frame.

    The frame stage of the two daily resamplers
    (DEC-LAKE-RUST-RESAMPLE-SIBLINGS-001): drop sessions whose IST date is
    ``>= before_days`` (days since the Unix epoch — the forming-session
    guard), re-stamp survivors to midnight IST as ``Datetime(us, "UTC")``,
    and select ``ts_utc,open,high,low,close,volume`` (+ ``open_interest``
    when ``keep_oi`` and present). Raises ``ValueError`` on an
    empty/undecodable payload or a polars error; ``RuntimeError`` if the
    compute panicked (caught at the FFI boundary — a panic never crosses
    raw).
    """

def session_cutoff_ipc(ipc_bytes: bytes, target_tf_minutes: int, cutoff_min: int) -> bytes:
    """Drop the forming trailing bucket(s) of an ALREADY-RESAMPLED session frame.

    The ``resample_session_to_tf`` cutoff law
    (DEC-LAKE-RUST-RESAMPLE-SIBLINGS-001): keep a bucket only when its label's
    IST minute-of-day plus ``target_tf_minutes`` is ``<= cutoff_min``; the
    frame passes through otherwise verbatim. Same error/panic contract as
    ``resample_1m_ipc``.
    """
