"""Typed surface of the brutex_sweep_ops wheel (DEC-RUST-SWEEP-OPS-001)."""

from collections.abc import Sequence

CHAIN_LINKS: list[str]
RUN_DIR_SUBDIR: str
__version__: str
__build_crate_version__: str

def join_path_segments(base: str, segments: Sequence[str]) -> str: ...
def gate_config_fragment(
    overfit: tuple[float, float, float, float] | None,
    stage3_enable: bool,
    stage3_top_k: int,
    stage3_max_fevals: int,
    stage3_max_restarts: int,
) -> tuple[tuple[float, float, float, float] | None, tuple[int, int, int] | None]: ...
def stall_scaled_timeout(factor: float, estimate: float, base: float) -> tuple[float, bool]: ...
def progress_derived(
    enumerated: int,
    evaluated: int,
    survivors: int,
    started_at: float,
    updated_at: float,
) -> tuple[float, float, float, float | None, float]: ...
def sla_decision(
    tuple_count: int,
    per_tuple_seconds: float,
    envelope_seconds: float,
    acknowledge: bool,
) -> tuple[float, str]: ...
def rank_plan_decision(
    n_masks: int,
    budget_bytes: int,
    mode: str,
    has_top_k: bool,
    digest_path: bool,
) -> tuple[int, str]: ...
def preflight_missing(
    sizes: Sequence[tuple[str, str, int]],
    symbols: Sequence[str],
    intervals: Sequence[str],
) -> list[tuple[str, str]]: ...
def freshness_stale(
    verdicts: Sequence[tuple[str, str, str]],
) -> list[tuple[str, str]]: ...
def liveness_tail(
    pid_ok: bool,
    now_wall: float,
    wall: float,
    fresh_seconds: float,
) -> str: ...
def audit_entry_hash(
    seq: str,
    prev_hash: str,
    ts: str,
    actor: str,
    action: str,
    subject: str,
    payload_json: str,
) -> str: ...
def chain_verify_step(
    expected_seq: int,
    prev_expected: str,
    seq: int,
    prev_hash: str,
    ts: str,
    actor: str,
    action: str,
    subject: str,
    payload_json: str,
    entry_hash: str,
) -> tuple[str, str, str]: ...
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
def chain_link_state(
    link: str,
    idx: int,
    verdict: str | None,
    running: bool,
    last_stage: str | None,
    terminal: str | None,
    refusal_text: str | None,
    error_type: str | None,
) -> tuple[str, str]: ...
def count_reconciliation(
    total: int,
    enumerated: int,
    evaluated: int,
    skipped: int,
    survivors: int,
    in_flight: int,
    prev_evaluated: int | None,
    is_done: bool,
) -> str: ...

# ── slice 3 — the durable-artifacts family ──────────────────────────
RUN_STATE_TOKENS: list[str]

def exit_status_resolution(
    state: str,
    prior_started: float | None,
    now: float,
    suspect: bool | None,
    prior_suspect: bool | None,
) -> tuple[float, float | None, bool]: ...
def quarantine_next_ordinal(
    names: list[str],
    prefix: str,
    suffix: str,
) -> int: ...
def checkpoint_record_decision(
    completed_index: int,
    last_index: int,
    total_combos: int,
) -> str: ...
def checkpoint_adoption_verdict(
    store_sweep_id: str,
    store_total: int,
    cp_sweep_id: str,
    cp_total: int,
) -> str: ...
