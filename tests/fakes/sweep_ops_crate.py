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

import math
from collections.abc import Callable, Sequence
from types import SimpleNamespace
from typing import TypeVar

import blake3

_T = TypeVar("_T")


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
    max_sl_hit_rate: float | None,
    cvar: tuple[int, int] | None,
    stage3_enable: bool,
    top_k: int,
    max_fevals: int,
    max_restarts: int,
) -> tuple[
    tuple[float, float, float, float] | None,
    float | None,
    tuple[int, int] | None,
    tuple[int, int, int] | None,
]:
    """The sweep_identity gate-config fragment emission law (fake twin).

    Four legs since DEC-RUST-SWEEP-OPS-GATE-FRAGMENT-WIDEN-001 — the
    ``max_sl_hit_rate`` + ``cvar`` legs ride through verbatim (the facade's
    exact-type gates guarantee a bit-exact f64/i64 roundtrip).
    """
    stage3 = (top_k, max_fevals, max_restarts) if stage3_enable else None
    return (overfit, max_sl_hit_rate, cvar, stage3)


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


_FAKE_RUN_STATE_TOKENS = [
    "running",
    "complete",
    "failed",
    "refused",
    "halted",
    "suspect_complete",
]


def _exit_status_resolution(
    state: str,
    prior_started: float | None,
    now: float,
    suspect: bool | None,
    prior_suspect: bool | None,
) -> tuple[float, float | None, bool]:
    """The sweep_exit_status wall-clock + suspect resolution (fake twin)."""
    if state not in _FAKE_RUN_STATE_TOKENS:
        raise ValueError(f"unknown run state token: {state!r}")
    started = prior_started if prior_started is not None else now
    ended = None if state == "running" else now
    if state == "suspect_complete":
        resolved = True
    elif suspect is not None:
        resolved = suspect
    else:
        resolved = prior_suspect if prior_suspect is not None else False
    return (started, ended, resolved)


def _quarantine_next_ordinal(names: list[str], prefix: str, suffix: str) -> int:
    """The sweep_quarantine monotonic-ordinal fold (fake twin — ASCII-exact)."""
    if not (prefix.isascii() and suffix.isascii()):
        raise ValueError("next_ordinal: non-ASCII prefix/suffix")
    highest = 0
    for name in names:
        if not name.isascii():
            raise ValueError(f"next_ordinal: non-ASCII name {name!r}")
        if not (name.startswith(prefix) and name.endswith(suffix)):
            continue
        core_end = len(name) - len(suffix)
        if core_end < len(prefix):
            continue
        core = name[len(prefix) : core_end]
        if not core or not all(char in "0123456789" for char in core):
            continue
        highest = max(highest, int(core))
    return highest + 1


def _checkpoint_record_decision(completed_index: int, last_index: int, total_combos: int) -> str:
    """The sweep_checkpoint monotonic-advance decision (fake twin)."""
    if completed_index < 0:
        return "reject_below_zero"
    if completed_index >= total_combos:
        return "reject_beyond_total"
    if completed_index <= last_index:
        return "noop"
    return "advance"


def _checkpoint_adoption_verdict(
    store_sweep_id: str, store_total: int, cp_sweep_id: str, cp_total: int
) -> str:
    """The sweep_checkpoint load-tail adoption verdict (fake twin)."""
    if cp_sweep_id != store_sweep_id:
        return "foreign"
    if cp_total != store_total:
        return "total_mismatch"
    return "adopt"


_FAKE_RUN_VERDICT_TOKENS = ["running", "stale", "not_running", "unknown"]


def _detach_probe_verdict(
    has_progress: bool,
    handle_pid_alive: bool,
    verdict_token: str | None,
    pids_match: bool,
) -> str:
    """The sweep_detach probe cross-check fold (fake twin)."""
    if not has_progress:
        return "unknown" if handle_pid_alive else "not_running"
    if verdict_token is None:
        raise ValueError("verdict token required when a progress payload is readable")
    if verdict_token not in _FAKE_RUN_VERDICT_TOKENS:
        raise ValueError(f"unknown run-verdict token: {verdict_token!r}")
    if verdict_token == "running" and not pids_match:
        return "unknown"
    return verdict_token


def _heal_mismatch_decision(
    src: list[str | None],
    plan: list[str | None],
    dest: list[str | None],
) -> str:
    """The sweep_heal assess mismatch-tail decision (fake twin)."""
    for name, values in (("src", src), ("plan", plan), ("dest", dest)):
        if len(values) != 3:
            raise ValueError(f"{name} must carry exactly 3 artifact slots, got {len(values)}")
    if all(value is None for value in src) and dest == plan:
        return "already_quarantined"
    sidecars_consistent = all(src[i] is None or src[i] == plan[i] for i in (1, 2))
    any_sidecar_present = any(src[i] is not None for i in (1, 2))
    if src[0] is None and sidecars_consistent and any_sidecar_present:
        return "complete_partial"
    return "refuse"


def _schedule_next_fire(
    is_interval: bool,
    interval_seconds: int,
    second_of_day: int,
    tz_offset_seconds: int,
    now: float,
    last_fire: float | None,
) -> float:
    """The sweep_scheduler next-fire clock arithmetic (fake twin — f64-shaped)."""
    if not math.isfinite(now):
        raise ValueError("now must be finite")
    if last_fire is not None and not math.isfinite(last_fire):
        raise ValueError("last_fire must be finite")
    if is_interval:
        anchor = now if last_fire is None else last_fire
        candidate = anchor + float(interval_seconds)
        return candidate if candidate > now else now
    tz = float(tz_offset_seconds)
    local_now = now + tz
    day_start = math.floor(local_now / 86_400.0) * 86_400.0
    today_fire = day_start + float(second_of_day) - tz
    candidate = today_fire if today_fire >= now else today_fire + 86_400.0
    if last_fire is not None and candidate <= last_fire:
        candidate += 86_400.0
    return candidate


def _resume_decision(
    exists: bool,
    sweep_id_matches: bool,
    next_index: int,
    total_combos: int,
) -> tuple[str, int]:
    """The sweep_scheduler resume classification (fake twin)."""
    if not exists:
        return ("fresh_absent", 0)
    if not sweep_id_matches:
        return ("fresh_foreign", 0)
    if next_index >= total_combos:
        return ("already_complete", total_combos)
    return ("resume", next_index)


_FAKE_RECOUNT_VERDICT_TOKENS = [
    "vix_divergence",
    "grid_corrupt",
    "grid_inconclusive",
    "cell_dependent",
    "cell_corrupt",
    "cell_inconclusive",
]


def _recount_verdict(
    vix_attributable: bool,
    is_grid_tuple: bool,
    context_match: bool,
    cell_evidence: bool,
    grid_corrupt_leaderboard: bool,
) -> str:
    """The sweep_verify recount post-byte-diff classification tail (fake twin)."""
    if vix_attributable:
        return "vix_divergence"
    if is_grid_tuple:
        return "grid_corrupt" if context_match else "grid_inconclusive"
    if grid_corrupt_leaderboard:
        return "cell_dependent"
    if context_match and cell_evidence:
        return "cell_corrupt"
    return "cell_inconclusive"


def _apriori_join_prune(
    frontier: list[tuple[int, ...]],
    drop_conflicts: bool,
    conflict_masks: list[int] | tuple[int, ...],
    n_bars: int,
    limit: int,
    stride: int,
) -> tuple[list[list[int]], int | None]:
    """The sweep_apriori join + subset-prune level fold (fake twin)."""
    if stride < 1:
        raise ValueError("stride must be >= 1")
    for tuple_ in frontier:
        if not tuple_:
            raise ValueError("frontier tuples must be non-empty")
        for bit in tuple_:
            if not 0 <= bit < 64:
                raise ValueError(f"frontier bit {bit} out of range [0, 64)")
    frontier_masks = {_fake_mask_of(tuple_) for tuple_ in frontier}
    buckets: dict[tuple[int, ...], list[int]] = {}
    for tuple_ in frontier:
        buckets.setdefault(tuple(tuple_[:-1]), []).append(tuple_[-1])
    candidates: list[list[int]] = []
    count = 0
    for prefix, lasts in buckets.items():
        for i in range(len(lasts)):
            for jj in range(i + 1, len(lasts)):
                candidate = [*prefix, lasts[i], lasts[jj]]
                full = _fake_mask_of(candidate)
                if any(full & ~(1 << drop) not in frontier_masks for drop in candidate):
                    continue
                if drop_conflicts and any(full & pm == pm for pm in conflict_masks):
                    continue
                candidates.append(candidate)
                count += 1
                if count % stride == 0 and count * n_bars > limit:
                    return ([], count)
    candidates.sort()
    return (candidates, None)


def _fake_mask_of(positions: Sequence[int]) -> int:
    mask = 0
    for position in positions:
        mask |= 1 << position
    return mask


def _tuple_record_stride(total_tuples: int) -> int:
    """The D-9 decimation-stride twin (f64-mirrored true division)."""
    return max(1, math.ceil(max(0, total_tuples) / 10_000))


def _per_tuple_diffcheck_budget(k_per_run: int, total_tuples: int) -> int:
    """The R45 K-distribution twin."""
    if k_per_run <= 0:
        return 0
    return max(1, k_per_run // max(1, total_tuples))


def _grid_tuple_count(axes: Sequence[int]) -> int:
    """The outer-product twin — raises past u64 like the crate's checked mul."""
    product = 1
    for value in axes:
        product *= value
    if product >= 2**64:
        raise ValueError("axis product exceeds u64")
    return product


def routed_fallback_reasons(call: Callable[[], _T]) -> tuple[_T, list[str], int]:
    """Run *call* with the FAKE crate ARMED; return ``(result, reasons, engaged)``.

    The ONE §11-COMMON probe every "the CLASS-C fall-back is unreachable"
    regression pin drives (DEC-RUST-STRICT-NO-FALLBACK-001). It answers the
    only question those pins ask: did the routed call reach the crate, or did
    the facade's exact-type gate bounce it to the Python oracle and announce?

    * ``reasons`` — every ``sweep_ops_rust.fallback`` reason token emitted
      during *call*, sorted. An empty list is the pin's assertion.
    * ``engaged`` — the count of ``sweep_ops_rust.engaged`` INFO events. It
      is the ANTI-VACUITY half: without it, a fake that failed the symbol
      handshake (latching ``crate_stale`` for the process) or a flag that
      never armed would also emit zero *fallback* events and the pin would
      pass while proving nothing. Assert ``engaged == 1``.

    The facade is reset to the POST-burn-in steady state
    (``remaining=0``) so the routed call is genuinely served by the crate
    rather than by the burn-in's shadow-compare arm.

    Environment discipline: the flag env + the ``importlib`` patch are
    installed inside a NESTED :meth:`pytest.MonkeyPatch.context` that unwinds
    before this helper returns — never a module-level autouse fixture (that
    pattern leaked env across test modules).
    """
    import pytest
    import structlog
    from structlog.testing import LogCapture

    from brutex.engine import sweep_ops_rust

    crate = build_fake_sweep_ops_crate(version="fake")
    capture = LogCapture()
    saved = structlog.get_config()
    try:
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(
                sweep_ops_rust,
                "importlib",
                SimpleNamespace(import_module=lambda name: crate),
            )
            patch.setenv(sweep_ops_rust.SWEEP_OPS_RUST_ENV, "1")
            sweep_ops_rust.reset_sweep_ops_rust_for_tests(remaining=0)
            structlog.configure(processors=[capture])
            result = call()
    finally:
        structlog.configure(**saved)
        sweep_ops_rust.reset_sweep_ops_rust_for_tests()
    reasons = sorted(
        str(entry.get("reason"))
        for entry in capture.entries
        if entry.get("event") == "sweep_ops_rust.fallback"
    )
    engaged = sum(1 for entry in capture.entries if entry.get("event") == "sweep_ops_rust.engaged")
    return result, reasons, engaged


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
        RUN_STATE_TOKENS=list(_FAKE_RUN_STATE_TOKENS),
        exit_status_resolution=_exit_status_resolution,
        quarantine_next_ordinal=_quarantine_next_ordinal,
        checkpoint_record_decision=_checkpoint_record_decision,
        checkpoint_adoption_verdict=_checkpoint_adoption_verdict,
        RUN_VERDICT_TOKENS=list(_FAKE_RUN_VERDICT_TOKENS),
        detach_probe_verdict=_detach_probe_verdict,
        heal_mismatch_decision=_heal_mismatch_decision,
        schedule_next_fire=_schedule_next_fire,
        resume_decision=_resume_decision,
        RECOUNT_VERDICT_TOKENS=list(_FAKE_RECOUNT_VERDICT_TOKENS),
        recount_verdict=_recount_verdict,
        apriori_join_prune=_apriori_join_prune,
        tuple_record_stride=_tuple_record_stride,
        per_tuple_diffcheck_budget=_per_tuple_diffcheck_budget,
        grid_tuple_count=_grid_tuple_count,
    )
