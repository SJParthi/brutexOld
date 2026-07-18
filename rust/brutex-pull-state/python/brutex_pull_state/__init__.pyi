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
_EXPECTED_COMPILED_SYMBOLS: tuple[str, ...]
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

# --- pull_intent (Wave 2E) ---
# The pull-intent parse-shape + resume-decision laws of
# brutex/dashboard/pull_intent.py (DEC-PULL-INTENT-RESUME-001).  Codes only:
# the seam classifies the ALREADY-Python-decoded payload's concrete types
# into small integers (the JSON decode — and CPython's whole failure
# taxonomy — stays Python; taxonomy parity by construction), and every warn
# string / the frozen PullIntent dataclass stay Python.  The one string edge
# is intent_resume_enabled's raw env token (the strip+lower+falsy-set law).

def intent_shape_verdict(
    root_mapping: bool,
    symbols: int,
    symbol: int,
    from_date: int,
    to_date: int,
    scope: int,
    restarts_kind: int,
    restarts_value: int,
    created_at_kind: int,
) -> int: ...
def intent_resume_decision(
    has_intent: bool,
    probe_acquirable: bool,
    restarts: int,
    max_restarts: int,
) -> int: ...
def intent_refused_restore_decision(
    dry_run: bool,
    started: bool,
    current_present: bool,
    prior_present: bool,
) -> int: ...
def intent_resume_enabled(raw: str | None) -> bool: ...

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

# --- auto_pull_scheduler (Wave 3G) ---
# The tick-decision ladder + durable-state-parse verdict of
# brutex/dashboard/auto_pull_scheduler.py: caller-resolved booleans /
# classified type-tag codes in, ONE ladder token / verdict code out (Python
# composes every log line, ValueError message and the AutoPullState itself;
# the ticker thread + store I/O never cross).

def auto_tick_decision(
    armed: bool,
    succeeded_today: bool,
    attempted_today: bool,
    gate_allowed: bool,
    launcher_bound: bool,
) -> int: ...
def auto_state_verdict(
    root_mapping: bool,
    schema_kind: int,
    schema_i64: int,
    schema_f64_bits: int,
    armed_is_bool: bool,
    armed_at_kind: int,
    last_attempt_kind: int,
    last_success_kind: int,
) -> int: ...

# --- pull_census (Wave 3G) ---
# The written-vs-settled census fold of brutex/dashboard/pull_census.py:
# scoped (symbol, interval, is_success) rows + scoped bars>0 pairs in,
# (settled_unwritten, written_unsettled, capped (kind, symbol, interval)
# sample triples) out — the repr() renders + `scanned` stay Python.

def census_fold(
    rows: list[tuple[str, str, bool]],
    written_pairs: list[tuple[str, str]],
) -> tuple[int, int, list[tuple[int, str, str]]]: ...

# --- auto_pull (Wave 3G) ---
# The instrument-master reload predicate of brutex/dashboard/auto_pull.py:
# five caller-resolved facts in, one bool out (the store probes stay Python).

def should_reload_master(
    index_count: int,
    stock_count: int,
    legs_present: bool,
    stale_economics: bool,
    stale_eligibility: bool,
) -> bool: ...

# --- rate_bridge + rate_bridge_cache (Wave 3H) ---
# The rate-bridge payload verdict (brutex/data/rate_bridge.py) + the
# rate-bridge-cache memo laws (brutex/dashboard/rate_bridge_cache.py).
# Classified codes / pre-computed ages cross in (the JSON decode, the
# CPython float()/int() coercion probes and the monotonic clock stay
# Python); a (verdict, pull_active_arm) pair / an arm token / a bool cross
# out. The token pair is the one string edge (production tokens are ASCII
# mtime:size:ino triples).

def bridge_payload_verdict(
    root_mapping: bool,
    schema_kind: int,
    schema_int: int,
    schema_float: float,
    field_kinds: list[int],
    pa_kind: int,
) -> tuple[int, int]: ...
def memo_serve_arm(age_s: float | None, ttl_s: float, inflight: bool) -> int: ...
def token_revalidates(token: str | None, prior: str | None) -> bool: ...
