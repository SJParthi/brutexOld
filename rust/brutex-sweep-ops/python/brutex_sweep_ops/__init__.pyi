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
