# Type stub for the brutex_observability PyO3 wheel
# (DEC-OBS-RUST-DETECTOR-CORE-001).
#
# SCALARS ONLY across the FFI: floats / ints / bools / strs / None and
# tuples thereof — decide-in-Rust, materialize-in-Python. Argument-conversion
# faults (non-float, negative/huge int, lone-surrogate str) raise
# TypeError / OverflowError / ValueError at the pyo3 edge — the facade's
# degrade set serves the Python oracle.

SOURCE_DIGEST: str
__version__: str
__build_crate_version__: str
__source_digest__: str

class PanicException(BaseException):
    """THIS wheel's own pyo3 panic class (a per-extension-module static)."""

def loop_watchdog_check_once(
    now: float,
    last_beat: float,
    tier: str,
    stall_started_beat: float,
    last_logged_at: float,
    last_alerted_at: float,
    episodes: int,
    warn_after_s: float,
    critical_after_s: float,
    relog_every_s: float,
    realert_every_s: float,
) -> tuple[str, float, float, float, int, float, bool, bool, bool, bool, bool, float]: ...
def watchdog_decide(
    now: float,
    last_beat: float,
    timeout_seconds: float,
    prev_hung: bool,
) -> tuple[float, bool, bool]: ...
def sla_decide(
    predicate: str,
    value: float,
    threshold: float,
    prev_breach: bool,
) -> tuple[bool, bool]: ...
def stamp_shape_index(
    root_is_dict: bool,
    stage_is_str: bool,
    run_id_is_str: bool,
    hash_is_str: bool,
    produced_ok: bool,
    extra_ok: bool,
) -> int: ...
def stamp_verdict(
    recomputed: str,
    content_hash: str,
    stage: str,
    expected_stage: str,
    run_id: str,
    expected_run_id: str | None,
    stamped_snapshot: str | None,
    expected_snapshot: str | None,
) -> str: ...
