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

# --- auto_pull_gate (Wave 2D) ---
# The --auto gate decision plane of brutex/calendars/auto_pull_gate.py:
# caller-resolved calendar flags + microsecond-of-day instants in; reason /
# verdict codes out (Python composes every human string and keeps the
# fail-loud env raise). Out-of-range inputs raise ValueError (the facade
# degrades to the oracle).

def auto_gate_decision(
    trading_day: bool,
    coverage_available: bool,
    muhurat_listed: bool,
    now_us_of_day: int,
    settle_us_of_day: int,
) -> tuple[int, bool]: ...
def muhurat_window_active(
    now_us_of_day: int,
    open_us_of_day: int,
    close_us_of_day: int,
    pre_margin_us: int,
    post_margin_us: int,
) -> bool: ...
def settle_token_verdict(token: str, block_end_us: int) -> tuple[int, int]: ...
