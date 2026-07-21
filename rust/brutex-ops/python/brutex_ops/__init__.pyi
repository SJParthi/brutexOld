# Type stubs for the brutex_ops PyO3 bridge (the unified ops crate:
# DEC-BACKUP-RUST-OPS-001 + DEC-CROSSVERIFY-RUST-CORE-001 +
# DEC-NIGHTLY-RUST-CORE-001). Primitives-only FFI surface — see
# python/brutex_ops/__init__.py; keep in lockstep with the `#[pymodule]`
# registration blocks in rust/brutex-ops/src/python.rs.

SOURCE_DIGEST: str
__version__: str
__build_crate_version__: str
__source_digest__: str

def version() -> str: ...

# ── backup lane (DEC-BACKUP-RUST-OPS-001) ──────────────────────────────
def evaluate_deadman_decision(
    state_error_present: bool,
    state_present: bool,
    now_minus_success_s: float | None,
    now_minus_running_s: float | None,
    now_minus_alert_s: float | None,
    threshold_hours: float,
    realert_hours: float,
    skew_tolerance_s: float,
    max_runtime_hours: float,
) -> tuple[int, str, bool, float | None, float]: ...
def render_backup_plist() -> str: ...
def render_deadman_plist() -> str: ...
def render_drill_plist() -> str: ...
def render_cron_lines() -> str: ...
def parse_backup_summary_snapshot_id(stdout: str) -> tuple[int, str | None]: ...
def parse_snapshots_latest_decision(stdout: str) -> tuple[int, str | None, str | None]: ...
def parse_ls_file_paths(stdout: str) -> list[str] | None: ...
def parse_ls_snapshot_time_decision(stdout: str) -> tuple[int, str | None]: ...
def build_backup_argv(
    binary: str,
    paths: list[str],
    excludes: list[str],
    tags: list[str],
) -> list[str]: ...

# ── crossverify lane (DEC-CROSSVERIFY-RUST-CORE-001) ───────────────────
def crossverify_render_csv(
    header: str,
    rows: list[list[str]],
) -> tuple[bytes | None, tuple[int, int, int] | None]:
    """The pinned CSV byte assembly — ``(bytes, None)`` or ``(None, (code, row, cell))``."""

def crossverify_canonical_identity(
    exchange: str,
    segment: str,
    lake_symbol: str,
    fno_underlying: str | None,
    fno_expiry_iso: str | None,
    fno_is_future: bool,
    fno_exchange: str | None,
) -> tuple[str, str, str, str, str, str] | None:
    """The deny-by-default canonical-identity mapping over Python-parsed FNO fields."""

def crossverify_s3_key(
    prefix: str,
    day_iso: str,
    segment_token: str,
    canonical_symbol: str,
) -> str:
    """The ONE S3 key law — ``<prefix>/<day_iso>/<segment_token>/<symbol>.csv``."""

# ── nightly lane (DEC-NIGHTLY-RUST-CORE-001) ───────────────────────────
def nightly_first_breaker_index(statuses: list[str]) -> int | None:
    """Index of the first ``refused``/``failed`` status, else ``None``.

    The ``chain.first_breaker`` loop projected to the breaker's index (the
    facade maps it back onto the original ``LinkOutcome``).
    """

def nightly_exit_code(
    statuses: list[str], exit_hints: list[int | None], has_tripwires: bool
) -> int:
    """The ``chain.exit_code_for`` law over flat outcome rows.

    REFUSED breaker → 1; FAILED breaker → its hint when set else 2; all
    green → 0; any tripwire upgrades to ``max(base, 2)``. Raises
    ``ValueError`` on a statuses/hints length mismatch (facade-bug rail —
    degrades to the oracle).
    """

def nightly_audit_reconcile_detail(
    nightly_id: str,
    events: list[tuple[str, str | None]],
    outcome_links: list[str],
    outcome_attempted: list[bool],
    link_starts_repr: str,
    attempted_sorted_repr: str,
    terminal_repr: str,
    declared_repr: str,
) -> str | None:
    """The T2 audit-reconcile decision — the tripwire DETAIL string or ``None``.

    The four ``*_repr`` strings are CPython ``repr()`` renderings of the
    derived lists, PRE-FORMATTED Python-side (both arms consume identical
    strings — no Rust re-derivation of CPython's ``repr`` exists). Raises
    ``ValueError`` on a links/attempted length mismatch.
    """

def nightly_count_text(value: int | None) -> str:
    """``report._count_text`` — ``"?"`` for ``None``, never a fabricated 0."""

def nightly_verdict(statuses: list[str], suspect: bool) -> str:
    """``report._verdict`` — ``OK``/``SUSPECT``/``REFUSED``/``FAILED``."""

def nightly_summary_lines(
    nightly_id: str,
    started_utc: float,
    ended_utc: float,
    links: list[tuple[str, str, str | None]],
    counts: list[tuple[str, int | None]],
    tripwires: list[tuple[str, str]],
    honesty_notes: list[str],
    dry_run: bool,
    suspect: bool,
) -> tuple[list[str], str]:
    """``report.summary_lines`` — the pure Telegram body ``(lines, verdict)``.

    Raises ``ValueError`` when the duration cannot be mirrored faithfully
    (non-finite / ≥ 2^53 — the oracle then raises Python's own error).
    """

def nightly_xml_escape(text: str) -> str:
    """``schedule._xml_escape`` — the chained-replace law (``&`` first)."""

def nightly_render_launchd_plist(
    label: str,
    program_arguments: list[str],
    hour: int,
    minute: int,
    env_vars: list[tuple[str, str]],
    out_log: str,
    err_log: str,
) -> str:
    """``schedule.render_launchd_plist`` — golden-pinned plist bytes.

    ``out_log``/``err_log`` arrive pre-formatted (``str(log_dir / ...)``) so
    pathlib semantics stay Python-owned; ``env_vars`` arrives as raw
    ``.items()`` pairs and is sorted in-crate (the ``sorted(...)`` law).
    """

def nightly_parse_at(text: str) -> tuple[int, int] | None:
    """``schedule.parse_at``'s decision core — ``(hour, minute)`` or ``None``.

    ``None`` = clean no-match (the facade raises Python's exact
    ``ValueError``). Raises ``ValueError`` on non-ASCII input (CPython's
    un-flagged ``\\d`` matches Unicode digits; the oracle decides those).
    """

def nightly_pmset_wake_command(hour: int, minute: int, lead_minutes: int) -> str:
    """``schedule.pmset_wake_command`` — the midnight-wrapping wake command.

    Raises ``ValueError`` on i64 overflow (facade-unreachable rail).
    """

def nightly_seconds_since_fire(
    hour: int,
    minute: int,
    now_hour: int,
    now_minute: int,
    now_second: int,
    now_microsecond: int,
) -> float:
    """``schedule.seconds_since_scheduled_fire`` over intraday components.

    Raises ``ValueError`` on an out-of-range component (Python's
    ``datetime.replace`` would raise there — the oracle decides).
    """
