"""Typed surface of the brutex_sweep_ops wheel (DEC-RUST-SWEEP-OPS-001)."""

from collections.abc import Sequence

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
