# Type stub for the brutex_pull_state PyO3 wheel (DEC-RUST-PULL-STATE-001).
#
# INTEGERS ONLY across the FFI: `pull_allowed_us` takes microsecond-of-day
# (i64 in [0, 86_400_000_000)) + the caller-resolved trading-day flag and
# returns the market-hours guard verdict. An out-of-range instant raises
# ValueError (the facade degrades to the Python oracle); no float, string,
# or timestamp object ever crosses (the structural bit-identity guarantee).

SOURCE_DIGEST: str
__version__: str
__build_crate_version__: str
__source_digest__: str

def pull_allowed_us(us_of_day: int, is_trading_day: bool) -> bool: ...

# --- pull_outcome (Wave 1A) ---
# The last-pull OUTCOME decision codes (outcome, tone, headline_code,
# detail_code) — machine tokens only; Python composes every human string and
# keeps the verdict_banner colour authority.

def classify_pull_outcome(
    state: str,
    detail: str,
    failed: int,
    stalled: bool,
    wedged: bool,
    has_verdict: bool,
) -> tuple[str, str, str, str]: ...
def verdict_correlates(retained_id: str, verdict_id: str) -> bool: ...

# --- pull_scope (Wave 1B) — note-surgery + scope-token laws ---
# Strings cross this edge (the ONE sanctioned exception to the integers-only
# discipline — the surgery outputs ARE strings; byte-exactness IS the
# contract, drift-pinned by the committed goldens).

def scope_token(token: str | None) -> int: ...
def market_hours_note_rewrite(note: str, phases_replacement: str) -> str: ...
def spot_only_note_surgery(base_note: str) -> tuple[str, bool, bool]: ...
def fno_only_note_surgery(base_note: str) -> tuple[str, bool, bool]: ...

# --- pull_stall (Wave 1C) ---
# The wedge/alive decision plane of brutex/dashboard/pull_stall.py: bools /
# f64 ages (ONE caller clock read) / short revision tokens in; the arm token
# (0..=5) or the due bool out. Both TOTAL over the full f64 plane (NaN/inf
# IEEE-faithful) — no error arm.

def wedge_decision(
    hung: bool,
    already_fired: bool,
    hook_bound: bool,
    baseline_captured: bool,
    last_beat_age_s: float,
    wedge_window_s: float,
    restart_age_s: float | None,
    baseline: str | None,
    current: str | None,
) -> int: ...
def alive_log_due(since_last_s: float, interval_s: float) -> bool: ...

# --- pull_progress (Wave 2F) ---
# The derived-metrics plane of brutex/dashboard/pull_progress.py
# (DEC-PULL-PROGRESS-HONESTY-001): counts as ints (facade-gated to the
# spike-proven <= 2**53 exactness domain; out-of-domain raises ValueError),
# the caller's injected-clock instants as floats (the kernel is clockless).
# Returns (percent, elapsed_seconds, units_per_sec, eta_present,
# eta_seconds) — percent reproduces CPython round(x, 1) HALF-EVEN
# bit-exactly; Python folds (eta_present, eta_seconds) back to float | None.

def progress_derived(
    state_running: bool,
    open_ended: bool,
    total: int,
    completed: int,
    phase_base_completed: int,
    updated_at: float,
    started_at: float,
    phase_started_at: float,
) -> tuple[float, float, float, bool, float]: ...
