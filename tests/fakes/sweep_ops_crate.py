"""Behavior-correct fake ``brutex_sweep_ops`` crate (DEC-RUST-SWEEP-OPS-001).

The ONE wheel-free fake every sweep-ops routing test arms (CLAUDE.md §11
COMMON — the slice-1 inline per-file fakes are consolidated here):
:func:`build_fake_sweep_ops_crate` returns a fresh ``SimpleNamespace``
carrying EVERY ``sweep_ops_rust._CRATE_API`` symbol with the Python laws
re-implemented, so the facade's symbol handshake passes and individual
tests can freely mutate/break single attributes on their own instance.

Extending ``_CRATE_API`` in a later slice means extending THIS builder in
the same commit — a fake missing any handshake symbol latches
``crate_stale`` for the process and silently turns every routing test
VACUOUS (the assertions still pass against the oracle). The facade unit
suite pins the builder's surface against ``_CRATE_API`` so that drift fails
loudly instead.
"""

from __future__ import annotations

from types import SimpleNamespace


def _join_path_segments(base: str, segments: list[str]) -> str:
    """The run_layout POSIX path-join law (fake twin)."""
    acc = base
    for segment in segments:
        if acc and not acc.endswith("/"):
            acc += "/"
        acc += segment
    return acc


def _gate_config_fragment(
    overfit: tuple[float, float, float, float] | None,
    stage3_enable: bool,
    top_k: int,
    max_fevals: int,
    max_restarts: int,
) -> tuple[
    tuple[float, float, float, float] | None,
    tuple[int, int, int] | None,
]:
    """The sweep_identity gate-config fragment emission law (fake twin)."""
    stage3 = (top_k, max_fevals, max_restarts) if stage3_enable else None
    return (overfit, stage3)


def _stall_scaled_timeout(factor: float, estimate: float, base: float) -> tuple[float, bool]:
    """The sweep_stall_policy scaled-timeout tail (fake twin)."""
    scaled = factor * estimate
    if scaled > base:
        return (scaled, True)
    return (base, False)


def _progress_derived(
    enumerated: int,
    evaluated: int,
    survivors: int,
    started_at: float,
    updated_at: float,
) -> tuple[float, float, float, float | None, float]:
    """The sweep_progress snapshot derived-metrics cluster (fake twin)."""
    percent = 0.0 if enumerated <= 0 else min(100.0, round(evaluated / enumerated * 100.0, 1))
    raw_elapsed = updated_at - started_at
    elapsed = raw_elapsed if raw_elapsed > 0.0 else 0.0
    rate = 0.0 if elapsed <= 0.0 or evaluated <= 0 else evaluated / elapsed
    eta: float | None
    if rate <= 0.0 or enumerated <= 0:
        eta = None
    else:
        remaining = enumerated - evaluated
        eta = 0.0 if remaining <= 0 else remaining / rate
    survivor_rate = 0.0 if evaluated <= 0 else survivors / evaluated
    return (float(percent), elapsed, float(rate), eta, float(survivor_rate))


def _sla_decision(
    tuple_count: int,
    per_tuple_seconds: float,
    envelope_seconds: float,
    acknowledge: bool,
) -> tuple[float, str]:
    """The sweep_sla_guard projection decision (fake twin)."""
    projected = tuple_count * per_tuple_seconds
    if projected <= envelope_seconds:
        return (projected, "allowed")
    if acknowledge:
        return (projected, "acknowledged")
    return (projected, "refused")


def _rank_plan_decision(
    n_masks: int,
    budget_bytes: int,
    mode: str,
    has_top_k: bool,
    digest_path: bool,
) -> tuple[int, str]:
    """The sweep_mem_budget rank-memory decision table (fake twin)."""
    projected = n_masks * 1024
    if mode == "on":
        return (projected, "bounded")
    if projected <= budget_bytes:
        return (projected, "batch")
    if mode == "off":
        return (projected, "refuse_off")
    if not has_top_k:
        return (projected, "refuse_no_top_k")
    if not digest_path:
        return (projected, "refuse_no_digest")
    return (projected, "bounded")


def _preflight_missing(
    sizes: list[tuple[str, str, int]],
    symbols: list[str],
    intervals: list[str],
) -> list[tuple[str, str]]:
    """The sweep_preflight zero-bar pair fold (fake twin)."""
    lookup = {(symbol, interval): count for symbol, interval, count in sizes}
    return [
        (symbol, interval)
        for symbol in symbols
        for interval in intervals
        if lookup.get((symbol, interval), 0) == 0
    ]


def _freshness_stale(verdicts: list[tuple[str, str, str]]) -> list[tuple[str, str]]:
    """The sweep_preflight derived-freshness stale fold (fake twin)."""
    return sorted(
        (symbol, interval) for symbol, interval, verdict in verdicts if verdict != "fresh"
    )


def _liveness_tail(pid_ok: bool, now_wall: float, wall: float, fresh_seconds: float) -> str:
    """The sweep_liveness verdict tail (fake twin)."""
    if not pid_ok:
        return "not_running"
    if now_wall - wall > fresh_seconds:
        return "stale"
    return "running"


def build_fake_sweep_ops_crate(*, version: str = "0.1.0-fake") -> SimpleNamespace:
    """Return a fresh behavior-correct fake crate namespace.

    Carries every ``_CRATE_API`` symbol (plus ``__version__`` and the
    ``RUN_DIR_SUBDIR`` twin) — pass it to the facade's importlib patch seam
    and mutate/delete attributes per test as needed.
    """
    return SimpleNamespace(
        __version__=version,
        RUN_DIR_SUBDIR="runs",
        join_path_segments=_join_path_segments,
        gate_config_fragment=_gate_config_fragment,
        stall_scaled_timeout=_stall_scaled_timeout,
        progress_derived=_progress_derived,
        sla_decision=_sla_decision,
        rank_plan_decision=_rank_plan_decision,
        preflight_missing=_preflight_missing,
        freshness_stale=_freshness_stale,
        liveness_tail=_liveness_tail,
    )
