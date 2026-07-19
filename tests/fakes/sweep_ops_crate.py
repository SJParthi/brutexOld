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

import blake3


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


def _audit_entry_hash(
    seq: str,
    prev_hash: str,
    ts: str,
    actor: str,
    action: str,
    subject: str,
    payload_json: str,
) -> str:
    """The sweep_audit BLAKE3 entry-hash law (fake twin — same pip blake3)."""
    hasher = blake3.blake3()
    for index, field in enumerate((seq, prev_hash, ts, actor, action, subject, payload_json)):
        if index:
            hasher.update(b"|")
        hasher.update(field.encode("utf-8"))
    return hasher.hexdigest()


def _chain_verify_step(
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
) -> tuple[str, str, str]:
    """The sweep_audit per-entry verify step (fake twin)."""
    if seq != expected_seq:
        return ("seq_out_of_order", str(expected_seq), str(seq))
    if prev_hash != prev_expected:
        return ("prev_hash_mismatch", prev_expected, prev_hash)
    recomputed = _audit_entry_hash(str(seq), prev_hash, ts, actor, action, subject, payload_json)
    if entry_hash != recomputed:
        return ("entry_hash_mismatch", recomputed, entry_hash)
    return ("ok", recomputed, "")


def _stamp_verdict(
    recomputed: str,
    content_hash: str,
    stage: str,
    expected_stage: str,
    run_id: str,
    expected_run_id: str | None,
    stamped_snapshot: str | None,
    expected_snapshot: str | None,
) -> str:
    """The stage_stamp validate mismatch decision (fake twin)."""
    mismatch = (
        recomputed != content_hash
        or stage != expected_stage
        or (expected_run_id is not None and run_id != expected_run_id)
        or (
            expected_snapshot is not None
            and stamped_snapshot is not None
            and stamped_snapshot != expected_snapshot
        )
    )
    return "hash_mismatch" if mismatch else "ok"


_FAKE_CHAIN_LINKS = ["preflight", "sweep", "stage3", "cascade", "artifacts"]


def _chain_link_state(
    link: str,
    idx: int,
    verdict: str | None,
    running: bool,
    last_stage: str | None,
    terminal: str | None,
    refusal_text: str | None,
    error_type: str | None,
) -> tuple[str, str]:
    """The chain_map per-link state ladder (fake twin)."""
    if verdict in ("hash_mismatch", "malformed"):
        return ("red", f"stamp {verdict}")
    if running and link == last_stage:
        return ("running", "in progress")
    if verdict == "ok":
        return ("green", "stamp valid")
    if running:
        if last_stage is None:
            return ("amber", "running; stage unknown")
        if idx < _FAKE_CHAIN_LINKS.index(last_stage):
            return ("green", "completed (inferred)")
        return ("pending", "not started")
    if terminal in ("complete", "suspect_complete"):
        return ("green", "run complete")
    if terminal == "refused":
        if link == "preflight":
            return ("red", f"refused: {refusal_text or ''}")
        return ("pending", "not started")
    if terminal in ("failed", "halted"):
        if last_stage is None:
            return ("amber", f"run {terminal}; stage unknown")
        if idx < _FAKE_CHAIN_LINKS.index(last_stage):
            return ("green", "completed (inferred)")
        if link == last_stage:
            return ("red", f"run {terminal} at this stage ({error_type or 'unknown error'})")
        return ("pending", "not started")
    return ("pending", "no evidence")


def _count_reconciliation(
    total: int,
    enumerated: int,
    evaluated: int,
    skipped: int,
    survivors: int,
    in_flight: int,
    prev_evaluated: int | None,
    is_done: bool,
) -> str:
    """The run_tripwires D-9 counter violations fold (fake twin)."""
    violations: list[str] = []
    if in_flight < 0:
        violations.append(f"in_flight {in_flight} < 0")
    if enumerated != total:
        violations.append(f"snapshot enumerated {enumerated} != armed total {total}")
    if skipped > evaluated:
        violations.append(f"skipped {skipped} > evaluated {evaluated} (skipped ⊆ evaluated)")
    if survivors > evaluated:
        violations.append(f"survivors {survivors} > evaluated {evaluated}")
    if evaluated + in_flight > total:
        violations.append(f"evaluated {evaluated} + in_flight {in_flight} > enumerated {total}")
    if prev_evaluated is not None and evaluated < prev_evaluated:
        violations.append(f"evaluated went backwards: {evaluated} < {prev_evaluated}")
    if is_done and evaluated != total:
        violations.append(f"run done but evaluated {evaluated} != enumerated {total}")
    return "; ".join(violations)


def build_fake_sweep_ops_crate(*, version: str = "0.1.0-fake") -> SimpleNamespace:
    """Return a fresh behavior-correct fake crate namespace.

    Carries every ``_CRATE_API`` symbol (plus ``__version__`` and the
    ``RUN_DIR_SUBDIR`` twin) — pass it to the facade's importlib patch seam
    and mutate/delete attributes per test as needed.
    """
    return SimpleNamespace(
        __version__=version,
        RUN_DIR_SUBDIR="runs",
        CHAIN_LINKS=list(_FAKE_CHAIN_LINKS),
        join_path_segments=_join_path_segments,
        gate_config_fragment=_gate_config_fragment,
        stall_scaled_timeout=_stall_scaled_timeout,
        progress_derived=_progress_derived,
        sla_decision=_sla_decision,
        rank_plan_decision=_rank_plan_decision,
        preflight_missing=_preflight_missing,
        freshness_stale=_freshness_stale,
        liveness_tail=_liveness_tail,
        audit_entry_hash=_audit_entry_hash,
        chain_verify_step=_chain_verify_step,
        stamp_verdict=_stamp_verdict,
        chain_link_state=_chain_link_state,
        count_reconciliation=_count_reconciliation,
    )
