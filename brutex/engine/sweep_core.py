"""Flag-gated Rust sweep-core seam — Slice 1: the enumeration + prefilter arm.

DESIGN_FINAL_RUST_SWEEP_CORE.md §1.2/§5/§6 (Slice 1). This module is the
Python side of the ``fno_math.ResidentSweepCore`` / ``ResidentSweepCoreU128``
``enumerate_prefilter`` kernel: env-flag resolution, the engagement condition
(the §5 ARM-CONDITION PARTITION's ENUM column), the per-tuple range driver
with its belts (``enum_no_progress`` + ``enum_total_mismatch``), the
work-bounded ENUMERATION BURN-IN SHADOW with its per-width autorevert latch
and MID-TUPLE CONTINUATION contract, and the fallback observability. Every
fallback reason lands on the bit-identical Python spine (the caller runs
today's ``iter_masks_k_of_bits`` loop verbatim when this seam returns
``None``), warned once per (tuple, reason) — the seam is invoked once per
tuple, so a per-refusal warn IS the per-(tuple, reason) law.

## The flag — ``BRUTEX_SWEEP_RUST_ENUM`` (default OFF)

Resolved ONCE per :func:`brutex.engine.sweep_run.run_persisted_sweep`
invocation via :func:`resolve_sweep_rust_enum_plan` (a structural loop-shape
decision per §5 — parallel workers read their OWN env at tuple start; the
documented, results-identical delta vs ``rust_metrics``' per-call read).
Vocabulary via the COMMON
:func:`brutex.engine.sweep_lookahead.env_flag_tristate` (imported, never
copied); an unrecognised non-empty value fails SAFE to OFF with one warn-once
``flag_unrecognised`` event. With the flag OFF (the default) the plan is
inert: the kernel module is never imported, no knob is parsed, and the caller
is byte-identical to today (spy-pinned).

## The enum-cells knob — ``BRUTEX_SWEEP_CORE_ENUM_CELLS``

The §3 dual work bounds derive from ONE cells budget (default ``2**28``):
``max_work_cells = cells`` and ``max_enumerated = clamp(cells // max(1,
n_bars), 4096, 2**20)`` (the DIVISOR-floored repo idiom). The env override is
fail-LOUD (junk → ``ValueError`` naming the env — the
``BRUTEX_SWEEP_PER_TUPLE_SECONDS`` exemplar, §12.2) and DUAL-CLAMPED (upper
``2**30``, lower ``2**16``) with one loud ``work_cap_clamped`` warn-once
carrying a ``direction`` field. Parsed EAGERLY at plan resolution (never
first-parsed mid-tuple) — and ONLY when the arm flag is ON, so an OFF-arm
process with a junk knob stays byte-identical to today.

## Engagement condition (the §5 ENUM column, in FALLBACK-REASON PRECEDENCE
## LAW listing order)

1. digest path (``bypass_dedup`` True) — else ``legacy_dedup_path``;
2. flag ON (own flag; ``flag_unrecognised`` at resolve);
3. wheel symbols present — ``wheel_missing`` (module unimportable) /
   ``wheel_stale`` (module importable, sweep-core symbols absent);
4. ABI version match — ``abi_version_mismatch``;
5. kernel WIDTH selected by the EMPTY-SAFE bit-index-keyed expression
   ``max(bit_length(max(bar_bits)), max(condition_bits, default=-1) + 1,
   max(pair indices, default=-1) + 1)`` → u64 / u128 / ``width_beyond_u128``
   refusal (the §1.3 in-core per-index refusal remains the backstop);
6. per-width autorevert latch clear — ``kernel_disabled``;
7. the STALL-BUDGET GUARD with the ENUM-ARM TERMS ONLY (the enum-shadow
   term; the fold-arm oracle-walk terms cannot execute on an enum-only
   pass) — ``stall_budget_exceeded`` / ``stall_guard_unavailable`` (the
   guard evaluation is TOTAL: any escape refuses, never crashes a tuple);
8. core construction from the SAME single-source columns
   (:func:`brutex.engine.rust_trade_sim.derive_kernel_inputs`) under the
   narrow ``kernel_error`` catch ``(ValueError, TypeError, OverflowError)``;
9. the §1.3 F27 window-length seam invariant ``core.n_bars() == len(bars)``
   — ``bars_length_mismatch``.

The byteorder / itemsize / exit-certificate / finite-``top_k`` conditions are
CORE/fold-arm-only rows of the partition table and deliberately do NOT gate
this arm (§5 fix-forward item 13 — no warn ever names fold machinery that
cannot run; Slice 2 adds its own column).

## Enumeration burn-in shadow (§5 — mask-count-floored, width-keyed)

While a width's shadow window is open (until
:data:`SWEEP_ENUM_SELF_CHECK_MASKS` = 2**16 core-enumerated masks have been
shadowed per process per width, hard-capped at
:data:`SWEEP_ENUM_SELF_CHECK_RANGES_CAP` = 256 ranges), every
``enumerate_prefilter`` range is compared against ``itertools.islice`` of
today's Python enumerate/conflict/prefilter over the SAME rank window —
``(enumerated, conflict_pruned, kept_list)`` exactly (order included). A
mismatch ships the PYTHON lists for the shadowed window, latches the enum
kernel OFF for the process (per width; one
``sweep_rust_enum.kernel_autoreverted`` ERROR), and serves the REMAINDER of
the tuple's rank space by CONTINUING the same Python ``iter_masks`` stream
from rank ``start_rank + len(python_window)`` to exhaustion (the MID-TUPLE
CONTINUATION contract) — the §1.3 total reconciliation is defined over the
resulting MIXED provenance. A stream that cannot reach the mask floor within
the ranges cap warns once per width (``enum_shadow_floor_unmet``) and closes
the window (the genuine degenerate-stream signal). The window state + latch
are the documented no-global-mutable-state exceptions (the ``rust_metrics``
precedent); :func:`reset_enum_self_check_for_tests` restores a fresh view.

## Mask serialization (§12 MASK-SERIALIZER LAW)

Every mask this NEW surface serializes (log events) is the 32-nibble hex
string via :func:`mask_hex` — never a bare int (grep-pinned).

## Honest envelope (Golden Rule #10)

* Deletes the per-enumerated-mask PYTHON cost on the engaged digest path; at
  production 1m windows the retained O(n_bars) prefilter floor governs total
  cost (§0/§9) — no wall-clock speedup is claimed here, the twin-run
  byte-identity gates are the ship condition.
* The shadow costs ~2**16 shadowed Python enumerations per process per
  engaged width (seconds-scale, priced by the stall guard's enum term).
* Core construction derives the full 7-column trade-sim inputs (the frozen
  §1.3 constructor contract — Slice 2's ``fold_block`` consumes them), an
  O(N_bars) cost per engaged tuple; a session-calendar failure inside that
  derivation propagates exactly as the trade-sim construction for kept masks
  would later raise it (documented in IMPLEMENTATION_NOTES — not a silent
  fallback, never misattributed to the kernel).
"""

from __future__ import annotations

import itertools
import math
import os
import struct
import sys
from array import array
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, fields
from typing import TYPE_CHECKING, Final, Protocol, cast

import structlog

from brutex.engine.combo_mask import count_supermasks, iter_masks_k_of_bits, mask_to_entry_signals
from brutex.engine.exit_index import bars_strictly_increasing
from brutex.engine.rust_trade_sim import derive_kernel_inputs
from brutex.engine.sweep_lookahead import env_flag_tristate
from brutex.metrics.compute import (
    Moments,
    ResultMetrics,
    TradeFolds,
    combine_metrics_from_folds,
    compute_metrics,
    compute_metrics_from_scalars,
)
from brutex.vocabulary import CONFLICT_PAIRS, has_conflict

if TYPE_CHECKING:  # pragma: no cover - import only for static typing
    from brutex.data.models import CandleBar
    from brutex.strategies.types import Side

__all__ = [
    "DEFAULT_ENUM_WORK_CELLS",
    "DEFAULT_FOLD_WORK_CELLS",
    "DEFAULT_ORACLE_NS_PER_BAR",
    "ENUM_CELLS_LOWER_CLAMP",
    "ENUM_CELLS_UPPER_CLAMP",
    "ENUM_SHADOW_NS",
    "EXPECTED_SWEEP_CORE_ABI_VERSION",
    "FOLD_CELLS_LOWER_CLAMP",
    "FOLD_CELLS_UPPER_CLAMP",
    "FOLD_FLAG_PYTHON_REQUIRED",
    "FOLD_FLAG_RESERVED_MASK",
    "FOLD_FLAG_ZERO_TRADE",
    "FOLD_FLOAT_FIELDS",
    "FOLD_FLOAT_STRIDE",
    "FOLD_INT_FIELDS",
    "FOLD_INT_STRIDE",
    "ORACLE_NS_UPPER_FACTOR",
    "PREFILTER_NS_PER_BAR",
    "SWEEP_CORE_CHUNK",
    "SWEEP_CORE_ENUM_CELLS_ENV",
    "SWEEP_CORE_ORACLE_NS_ENV",
    "SWEEP_CORE_SELF_CHECK",
    "SWEEP_CORE_SELF_CHECK_CAP",
    "SWEEP_CORE_SELF_CHECK_NONTRIVIAL",
    "SWEEP_CORE_SPOT_CANDIDATES",
    "SWEEP_CORE_SPOT_CHECK",
    "SWEEP_CORE_STALL_BUDGET_FRACTION",
    "SWEEP_CORE_WORK_CELLS_ENV",
    "SWEEP_ENUM_SELF_CHECK_MASKS",
    "SWEEP_ENUM_SELF_CHECK_RANGES_CAP",
    "SWEEP_RUST_CORE_BELT_ENV",
    "SWEEP_RUST_CORE_DEFAULT",
    "SWEEP_RUST_CORE_ENV",
    "SWEEP_RUST_ENUM_DEFAULT",
    "SWEEP_RUST_ENUM_ENV",
    "CoreDecodeRefused",
    "CoreFoldContext",
    "CorePlan",
    "DecodedFoldBlock",
    "EnumOutcome",
    "EnumPlan",
    "consume_fold_burnin",
    "core_enumerate_kept",
    "core_fold_context",
    "decode_fold_block",
    "disambiguate_core_mismatch",
    "enum_shadow_masks_remaining",
    "fold_burnin_open",
    "fold_burnin_total_remaining",
    "fold_chunk_masks",
    "fold_latch_reason",
    "fold_row_metrics",
    "fold_span_years",
    "is_kernel_panic",
    "latch_fold_kernel",
    "log_core_engaged",
    "mask_hex",
    "max_enumerated_for",
    "metrics_compare_key",
    "metrics_equal",
    "reset_enum_self_check_for_tests",
    "reset_observability_memo_for_tests",
    "reset_self_check_for_tests",
    "resolve_sweep_rust_core_plan",
    "resolve_sweep_rust_enum_plan",
    "spot_check_candidates",
    "warn_belt_preexisting",
    "warn_core_refusal",
    "zero_trade_metrics",
]

_LOG = structlog.get_logger("brutex.engine.sweep_core")

#: The Slice-1 arm flag (§5 kill switches). Default OFF; flips default-ON only
#: in its own later ADR (the #1186→#1532 two-step).
SWEEP_RUST_ENUM_ENV: Final[str] = "BRUTEX_SWEEP_RUST_ENUM"

#: The ONE default-arm constant (revert lever). OFF until the Slice-4 flip ADR.
SWEEP_RUST_ENUM_DEFAULT: Final[bool] = False

#: The enum work-cells knob (§3/§12.2) — fail-loud parse + dual clamp.
SWEEP_CORE_ENUM_CELLS_ENV: Final[str] = "BRUTEX_SWEEP_CORE_ENUM_CELLS"

#: Default ``max_work_cells`` (enumerated × n_bars, §3 — ~70 ms at the
#: measured 0.257 ns/bar·mask prefilter rate; the GIL/liveness knob).
DEFAULT_ENUM_WORK_CELLS: Final[int] = 1 << 28

#: §3 round-2 upper clamp for the enum-cells override (~0.28 s of GIL hold).
ENUM_CELLS_UPPER_CLAMP: Final[int] = 1 << 30

#: §3 round-3 lower clamp (a numeric-but-degenerate override must never
#: produce a sub-one-mask budget; the guaranteed-progress law backstops it).
ENUM_CELLS_LOWER_CLAMP: Final[int] = 1 << 16

#: ``max_enumerated`` clamp floor / ceiling (§3: ``clamp(cells // max(1,
#: n_bars), 4096, 2**20)``).
MAX_ENUMERATED_FLOOR: Final[int] = 4096
MAX_ENUMERATED_CEIL: Final[int] = 1 << 20

#: The sweep-core ABI version this seam decodes (§1.3). A wheel exporting a
#: different value refuses engagement (``abi_version_mismatch``).
EXPECTED_SWEEP_CORE_ABI_VERSION: Final[int] = 1

#: Enumeration burn-in shadow: the MASK-COUNT floor per process PER KERNEL
#: WIDTH (§5, external fold fix-forward item 4).
SWEEP_ENUM_SELF_CHECK_MASKS: Final[int] = 1 << 16

#: The ranges hard cap (§5, Verifier B checkpoint-1 audit C51 — 256 gives
#: ~1.7× headroom over the ⌈2**16 / 444⌉ = 148 ranges the heaviest production
#: yield needs to meet the mask floor).
SWEEP_ENUM_SELF_CHECK_RANGES_CAP: Final[int] = 256

#: Stall-guard pricing constants (§5 — the enum-arm terms): ~2.4 µs per
#: shadowed Python enumeration + the kept-list prefilter arm priced as if
#: EVERY shadowed mask were conflict-free with the count running
#: (pessimistic; min_hits == 0 grids actually pay less per the §1.3 skip law).
ENUM_SHADOW_NS: Final[float] = 2_400.0
PREFILTER_NS_PER_BAR: Final[float] = 0.257

#: The stall-budget fraction (§5): refuse engagement when the priced overhead
#: exceeds this fraction of the tuple's recomputed effective stall deadline.
SWEEP_CORE_STALL_BUDGET_FRACTION: Final[float] = 0.5

#: The two kernel widths (bit-index-keyed selection, §5).
_WIDTH_U64: Final[int] = 64
_WIDTH_U128: Final[int] = 128

#: Once-per-process engaged-once INFO memo (LOG-VOLUME state only — the
#: documented no-global-mutable-state exception, the rust_metrics precedent).
_OBSERVABILITY_MEMO: Final[set[str]] = set()

#: Per-width shadow window state: masks still to shadow / ranges still
#: allowed / the autorevert latch reason (None = kernel live). Documented
#: exceptions to the no-global-mutable-state rule (module docstring).
_SHADOW_MASKS_REMAINING: Final[dict[int, int]] = {
    _WIDTH_U64: SWEEP_ENUM_SELF_CHECK_MASKS,
    _WIDTH_U128: SWEEP_ENUM_SELF_CHECK_MASKS,
}
_SHADOW_RANGES_REMAINING: Final[dict[int, int]] = {
    _WIDTH_U64: SWEEP_ENUM_SELF_CHECK_RANGES_CAP,
    _WIDTH_U128: SWEEP_ENUM_SELF_CHECK_RANGES_CAP,
}
_ENUM_LATCH: Final[dict[int, str | None]] = {_WIDTH_U64: None, _WIDTH_U128: None}


def mask_hex(mask: int) -> str:
    """Serialize a mask as the 32-nibble hex string (§12 MASK-SERIALIZER LAW).

    Every NEW conversion-introduced surface serializes masks this way — never
    a bare int (the serde_json u64-aliasing hazard class; grep-pinned).
    """
    return f"{mask:032x}"


@dataclass(frozen=True, slots=True)
class EnumPlan:
    """The once-per-invocation resolution of the enum arm's flag + knob.

    ``requested`` is the flag verdict; ``enum_cells`` the clamped work-cells
    budget (meaningful only when ``requested`` — an OFF plan never parsed the
    knob, so it carries the default).
    """

    requested: bool
    enum_cells: int


@dataclass(frozen=True, slots=True)
class EnumOutcome:
    """A completed core-arm enumeration for one tuple.

    ``evaluated == enumerated - conflict_pruned`` (the conflict-free survivor
    count today's spine reads as ``len(survivors)``); ``kept`` is the
    post-min-hits survivor list in POSITION-LEX emission order.
    ``hits_counted`` is the §1.3 in-band short-circuit oracle summed over the
    tuple's ranges (observability/test data only — never persisted, never in
    any identity).
    """

    enumerated: int
    conflict_pruned: int
    evaluated: int
    kept: list[int]
    hits_counted: int


class _SweepCoreLike(Protocol):
    """The kernel surface the driver consumes (both width pyclasses)."""

    def n_bars(self) -> int:
        """Resident bar count (the F27 seam-invariant probe)."""
        ...

    def enumerate_prefilter(
        self,
        condition_bits: list[int],
        k: int,
        conflict_pairs: list[tuple[int, int]],
        min_hits: int,
        start_rank: int,
        max_enumerated: int,
        max_work_cells: int,
    ) -> tuple[int, int, int, bool, int, list[int]]:
        """One §1.3 rank range → the 6-tuple."""
        ...


def resolve_sweep_rust_enum_plan() -> EnumPlan:
    """Resolve the enum arm's flag + work-cells knob (once per invocation).

    Flag: ``_TRUTHY`` → ON; ``_FALSY`` → OFF; unset/whitespace-empty →
    :data:`SWEEP_RUST_ENUM_DEFAULT`; unrecognised non-empty → OFF with one
    warn-once ``flag_unrecognised`` (fail-SAFE — §12.2 tristate-arm policy).

    Knob: parsed ONLY when the flag lands ON (an OFF-arm process with a junk
    knob is byte-identical to today); junk raises ``ValueError`` naming the
    env (fail-LOUD, the exemplar rule); a numeric value is dual-clamped into
    ``[2**16, 2**30]`` with one loud ``work_cap_clamped`` warn per direction.
    """
    raw = os.environ.get(SWEEP_RUST_ENUM_ENV, "")
    verdict = env_flag_tristate(raw)
    if verdict is None and raw.strip():
        _warn_once("flag_unrecognised", value=raw)
        return EnumPlan(requested=False, enum_cells=DEFAULT_ENUM_WORK_CELLS)
    requested = SWEEP_RUST_ENUM_DEFAULT if verdict is None else verdict
    if not requested:
        return EnumPlan(requested=False, enum_cells=DEFAULT_ENUM_WORK_CELLS)
    return EnumPlan(requested=True, enum_cells=_resolve_enum_cells())


def _resolve_enum_cells() -> int:
    """Parse + dual-clamp the enum work-cells knob (fail-loud, §3/§12.2)."""
    raw = os.environ.get(SWEEP_CORE_ENUM_CELLS_ENV, "")
    if not raw.strip():
        return DEFAULT_ENUM_WORK_CELLS
    try:
        value = int(raw.strip())
    except ValueError as exc:
        msg = (
            f"{SWEEP_CORE_ENUM_CELLS_ENV}={raw!r} is not an integer — set a cells "
            f"budget in [{ENUM_CELLS_LOWER_CLAMP}, {ENUM_CELLS_UPPER_CLAMP}] or unset it"
        )
        raise ValueError(msg) from exc
    if value > ENUM_CELLS_UPPER_CLAMP:
        _warn_clamp(direction="upper", requested=value, clamped=ENUM_CELLS_UPPER_CLAMP)
        return ENUM_CELLS_UPPER_CLAMP
    if value < ENUM_CELLS_LOWER_CLAMP:
        _warn_clamp(direction="lower", requested=value, clamped=ENUM_CELLS_LOWER_CLAMP)
        return ENUM_CELLS_LOWER_CLAMP
    return value


def _warn_clamp(
    *, direction: str, requested: int, clamped: int, env: str = SWEEP_CORE_ENUM_CELLS_ENV
) -> None:
    """One loud ``work_cap_clamped`` warn per process per (env, direction) (§3)."""
    key = f"work_cap_clamped:{env}:{direction}"
    if key in _OBSERVABILITY_MEMO:
        return
    _OBSERVABILITY_MEMO.add(key)
    _LOG.warning(
        "sweep_core.work_cap_clamped",
        env=env,
        direction=direction,
        requested=requested,
        clamped=clamped,
    )


def max_enumerated_for(n_bars: int, enum_cells: int) -> int:
    """The §3 range bound: ``clamp(cells // max(1, n_bars), 4096, 2**20)``.

    The DIVISOR-floored repo idiom (``sweep_run.py`` chunk-floor precedent) —
    total on ``n_bars == 0`` (unit-pinned; never a ``ZeroDivisionError``).
    """
    by_cells = enum_cells // max(1, n_bars)
    return max(MAX_ENUMERATED_FLOOR, min(MAX_ENUMERATED_CEIL, by_cells))


def enum_shadow_masks_remaining(width: int) -> int:
    """Masks this process still owes the enum shadow at ``width`` (§5).

    Consumed by the stall-budget guard's enum term (priced directly in
    masks). A closed window (floor met or ranges cap exhausted) reads 0.
    """
    if _SHADOW_RANGES_REMAINING.get(width, 0) <= 0:
        return 0
    return max(0, _SHADOW_MASKS_REMAINING.get(width, 0))


def reset_enum_self_check_for_tests(
    *,
    width: int | None = None,
    masks_remaining: int = SWEEP_ENUM_SELF_CHECK_MASKS,
    ranges_cap_remaining: int = SWEEP_ENUM_SELF_CHECK_RANGES_CAP,
    latched: str | None = None,
) -> None:
    """Reset the per-width shadow window + latch (test isolation ONLY).

    ``width=None`` resets BOTH widths to the given state; a specific width
    resets only that width (the §5 round-5/round-6 width-keyed test seam).
    Never called from production code paths.
    """
    widths = (_WIDTH_U64, _WIDTH_U128) if width is None else (width,)
    for w in widths:
        _SHADOW_MASKS_REMAINING[w] = masks_remaining
        _SHADOW_RANGES_REMAINING[w] = ranges_cap_remaining
        _ENUM_LATCH[w] = latched


def reset_observability_memo_for_tests() -> None:
    """Clear the warn-once / engaged-once memo (test isolation ONLY)."""
    _OBSERVABILITY_MEMO.clear()


def _warn_once(reason: str, **context: object) -> None:
    """Log ``sweep_rust_enum.fallback`` once per process per *reason*.

    Flag/knob resolution reasons (``flag_unrecognised``) resolve once per
    invocation and would otherwise re-warn per tuple on a grid; the per-tuple
    refusal reasons go through :func:`_warn_refusal` instead (loud per tuple
    — the §5 "warned once per (tuple, reason)" law).
    """
    if reason in _OBSERVABILITY_MEMO:
        return
    _OBSERVABILITY_MEMO.add(reason)
    _LOG.warning("sweep_rust_enum.fallback", reason=reason, **context)


def _warn_refusal(reason: str, **context: object) -> None:
    """Log one ``sweep_rust_enum.fallback`` WARNING for this tuple's refusal.

    The seam runs once per tuple, so an unconditional warn here IS the
    per-(tuple, reason) law (§5) — loud per tuple, deliberately not memoised.
    """
    _LOG.warning("sweep_rust_enum.fallback", reason=reason, **context)


def _log_engaged(**context: object) -> None:
    """Log ``sweep_rust_enum.engaged`` (INFO) once per process.

    The observable "the wired ON path actually runs Rust" signal the golden
    suite asserts (the vacuous-gate trap; the rust_metrics precedent).
    """
    key = "__engaged__"
    if key in _OBSERVABILITY_MEMO:
        return
    _OBSERVABILITY_MEMO.add(key)
    _LOG.info("sweep_rust_enum.engaged", **context)


def _load_kernel_module() -> object | None:
    """Import ``fno_math`` lazily, or ``None`` when unimportable.

    A separate function so the OFF-arm spy pin can assert the kernel module
    is never even loaded when the flag is unset (module docstring).
    """
    try:
        import fno_math
    except ImportError:
        return None
    return fno_math


def _select_width(
    bar_bits: Sequence[int],
    condition_bits: Sequence[int],
    pair_indices: Sequence[int],
) -> int:
    """The §5 EMPTY-SAFE bit-index-keyed width expression.

    ``max(bit_length(max(bar_bits)), max(condition_bits, default=-1) + 1,
    max(pair_indices, default=-1) + 1)`` — total on the config-legal EMPTY
    ``condition_bits`` (``bits=[]``/``k=0`` engages and serves the §1.3 k=0
    vector); ``bar_bits`` is never empty (``run_persisted_sweep`` refuses
    empty bars pre-FFI). Returns the needed BIT width (the caller maps to a
    kernel width or refuses past 128).
    """
    bars_term = max(bar_bits).bit_length() if bar_bits else 0
    bits_term = max(condition_bits, default=-1) + 1
    pairs_term = max(pair_indices, default=-1) + 1
    return max(bars_term, bits_term, pairs_term)


def _effective_stall_deadline(n_masks: int, n_bars: int) -> float | None:
    """Recompute this tuple's #1291 effective stall deadline (§5).

    Reads the ``sweep_parallel`` symbols via ``getattr`` on a FUNCTION-SCOPE
    lazy import (cycle-safe; ZERO edits to the not-ours file). Symbol-present
    trees get the work-scaled formula ``max(stall_seconds, n_masks × n_bars /
    mask_bars_per_sec)`` from the SAME env resolvers the parent uses; a
    pre-#1291 / reverted tree degrades to the FLAT ``DEFAULT_STALL_SECONDS``
    (the exact deadline that parent actually enforces — the §5 round-6
    ABSENT-SYMBOL arm). ``None`` = unresolvable → the caller refuses with
    ``stall_guard_unavailable``.
    """
    from brutex.engine import sweep_parallel

    resolve_stall = getattr(sweep_parallel, "resolve_stall_seconds", None)
    resolve_rate = getattr(sweep_parallel, "resolve_stall_min_mask_bars_per_sec", None)
    if resolve_stall is not None and resolve_rate is not None:
        stall_seconds = float(cast("float", resolve_stall()))
        rate = float(cast("float", resolve_rate()))
        return max(stall_seconds, (float(n_masks) * float(n_bars)) / rate)
    default_stall = getattr(sweep_parallel, "DEFAULT_STALL_SECONDS", None)
    if default_stall is None:
        return None
    return float(cast("float", default_stall))


def _shadow_open(width: int) -> bool:
    """True while ``width``'s burn-in shadow window is still open (§5)."""
    return _SHADOW_MASKS_REMAINING.get(width, 0) > 0 and _SHADOW_RANGES_REMAINING.get(width, 0) > 0


def _consume_shadow_range(width: int, shadowed_masks: int) -> None:
    """Account one shadowed range; warn ``enum_shadow_floor_unmet`` on a
    ranges-cap exhaustion that leaves the mask floor unmet (§5 audit C51)."""
    _SHADOW_MASKS_REMAINING[width] = max(0, _SHADOW_MASKS_REMAINING[width] - shadowed_masks)
    _SHADOW_RANGES_REMAINING[width] = max(0, _SHADOW_RANGES_REMAINING[width] - 1)
    if _SHADOW_RANGES_REMAINING[width] == 0 and _SHADOW_MASKS_REMAINING[width] > 0:
        _warn_once(
            "enum_shadow_floor_unmet",
            width=width,
            masks_unmet=_SHADOW_MASKS_REMAINING[width],
            ranges_cap=SWEEP_ENUM_SELF_CHECK_RANGES_CAP,
        )


def _latch_enum(width: int, **context: object) -> None:
    """Flip the per-width autorevert latch (once) — a shadow mismatch is a
    kernel defect; the Python lists ship and the process stops trusting the
    enum kernel at this width (§5)."""
    if _ENUM_LATCH[width] is None:
        _ENUM_LATCH[width] = "enum_shadow_mismatch"
        _LOG.error("sweep_rust_enum.kernel_autoreverted", width=width, **context)


def _python_prefilter(
    window: Sequence[int],
    bar_bits: Sequence[int],
    min_hits: int,
) -> tuple[int, list[int]]:
    """Today's spine's conflict-prune + Stage-1B prefilter over ``window``.

    Byte-identical semantics to the ``run_persisted_sweep`` loop: conflict
    prune via the COMMON :func:`has_conflict`, then (``min_hits > 0`` only)
    the supermask-hits count via :func:`count_supermasks` with the strict
    ``hits >= min_hits`` keep law. Returns ``(conflict_pruned, kept)``.
    """
    pruned = 0
    survivors: list[int] = []
    for mask in window:
        if has_conflict(mask):
            pruned += 1
        else:
            survivors.append(mask)
    if min_hits > 0 and survivors:
        hits = count_supermasks(bar_bits, survivors)
        kept = [mask for mask, hit in zip(survivors, hits, strict=True) if hit >= min_hits]
    else:
        kept = survivors
    return pruned, kept


def _drain_python_remainder(
    oracle: Iterator[int],
    bar_bits: Sequence[int],
    min_hits: int,
    chunk: int,
) -> tuple[int, int, list[int]]:
    """The §5 MID-TUPLE CONTINUATION: serve the rest of the rank space from
    the SAME Python ``iter_masks`` stream, in bounded chunks, to exhaustion.

    Returns ``(enumerated, conflict_pruned, kept)`` for the drained tail.
    """
    enumerated = 0
    pruned_total = 0
    kept: list[int] = []
    step = max(1, chunk)
    while True:
        window = list(itertools.islice(oracle, step))
        if not window:
            return enumerated, pruned_total, kept
        pruned, window_kept = _python_prefilter(window, bar_bits, min_hits)
        enumerated += len(window)
        pruned_total += pruned
        kept.extend(window_kept)


def core_enumerate_kept(
    *,
    bars: Sequence[CandleBar],
    bar_bits: Sequence[int],
    condition_bits: Sequence[int],
    k: int,
    min_hits: int,
    tf_minutes: int,
    n_masks: int,
    digest_path: bool,
    plan: EnumPlan,
) -> EnumOutcome | None:
    """Run one tuple's enumeration + prefilter on the Rust core, or refuse.

    Returns an :class:`EnumOutcome` on a completed core-arm (or latched
    mixed-provenance) enumeration whose counters + kept list are EXACT vs
    today's spine, or ``None`` on ANY refusal/fallback (warned; the caller
    serves the tuple on today's byte-identical Python loop).
    """
    if not plan.requested:
        return None
    # 1. digest path (§5 partition row 1 — the precedence-law first check).
    if not digest_path:
        _warn_refusal("legacy_dedup_path")
        return None
    # 2-3. wheel symbols.
    module = _load_kernel_module()
    if module is None:
        _warn_refusal("wheel_missing")
        return None
    core_cls = getattr(module, "ResidentSweepCore", None)
    core_cls_u128 = getattr(module, "ResidentSweepCoreU128", None)
    abi_version = getattr(module, "SWEEP_CORE_ABI_VERSION", None)
    if core_cls is None or core_cls_u128 is None or abi_version is None:
        _warn_refusal("wheel_stale")
        return None
    # 4. ABI version.
    if abi_version != EXPECTED_SWEEP_CORE_ABI_VERSION:
        _warn_refusal(
            "abi_version_mismatch",
            wheel_abi=abi_version,
            expected_abi=EXPECTED_SWEEP_CORE_ABI_VERSION,
        )
        return None
    # 5. width selection (bit-index-keyed, empty-safe).
    bits_sorted = sorted(set(condition_bits))
    pairs_sorted = sorted(CONFLICT_PAIRS)
    pair_indices = [index for pair in pairs_sorted for index in pair]
    width_bits = _select_width(bar_bits, bits_sorted, pair_indices)
    if width_bits <= _WIDTH_U64:
        width = _WIDTH_U64
        chosen_cls = core_cls
    elif width_bits <= _WIDTH_U128:
        width = _WIDTH_U128
        chosen_cls = core_cls_u128
    else:
        _warn_refusal("width_beyond_u128", width_bits=width_bits)
        return None
    # 6. per-width autorevert latch.
    if _ENUM_LATCH[width] is not None:
        _warn_refusal("kernel_disabled", width=width, latch=_ENUM_LATCH[width])
        return None
    # 7. STALL-BUDGET GUARD — enum-arm terms only; TOTAL evaluation (§5:
    # any escape refuses with `stall_guard_unavailable`, never a per-tuple
    # crash — the guard runs OUTSIDE the narrow kernel_error catch).
    n_bars = len(bars)
    try:
        deadline = _effective_stall_deadline(n_masks, n_bars)
    except Exception as exc:
        _warn_refusal("stall_guard_unavailable", error=repr(exc))
        return None
    if deadline is None:
        _warn_refusal("stall_guard_unavailable", error="stall symbols unresolvable")
        return None
    overhead_s = (
        enum_shadow_masks_remaining(width) * (ENUM_SHADOW_NS + n_bars * PREFILTER_NS_PER_BAR) * 1e-9
    )
    budget = SWEEP_CORE_STALL_BUDGET_FRACTION * deadline
    if overhead_s > budget:
        _warn_refusal(
            "stall_budget_exceeded",
            overhead_s=overhead_s,
            budget_s=budget,
            deadline_s=deadline,
        )
        return None
    # 8. core construction — the SAME single-source columns ResidentTradeSim
    # derives (§1.2); the FFI validation errors ride the narrow kernel_error
    # catch. A session-calendar derivation failure propagates (module
    # docstring — the same raise the kept-mask trade sim would surface).
    open_p, high_p, low_p, close_p, forced, session_days = derive_kernel_inputs(bars, tf_minutes)
    try:
        core = cast(
            "_SweepCoreLike",
            chosen_cls(open_p, high_p, low_p, close_p, forced, session_days, list(bar_bits)),
        )
    except (ValueError, TypeError, OverflowError) as exc:
        _warn_refusal("kernel_error", stage="construct", error=repr(exc))
        return None
    # 9. F27 window-length seam invariant.
    core_bars = core.n_bars()
    if core_bars != n_bars:
        _warn_refusal("bars_length_mismatch", core_n_bars=core_bars, python_n_bars=n_bars)
        return None
    _log_engaged(width=width, n_bars=n_bars)
    return _drive(
        core,
        bits_sorted=bits_sorted,
        k=k,
        pairs_sorted=pairs_sorted,
        min_hits=min_hits,
        bar_bits=bar_bits,
        n_bars=n_bars,
        n_masks=n_masks,
        width=width,
        enum_cells=plan.enum_cells,
    )


def _drive(
    core: _SweepCoreLike,
    *,
    bits_sorted: list[int],
    k: int,
    pairs_sorted: list[tuple[int, int]],
    min_hits: int,
    bar_bits: Sequence[int],
    n_bars: int,
    n_masks: int,
    width: int,
    enum_cells: int,
) -> EnumOutcome | None:
    """The §1.4 range-driver loop: shadow → belts → tallies → kept.

    Loops on the IN-BAND ``exhausted`` flag (the §1.3 exhaustion contract),
    never a "returned empty" heuristic; the ≥1-mask progress law makes every
    non-exhausted call advance rank (belted by ``enum_no_progress``). On loop
    exit the §1.3 TOTAL-RECONCILIATION belt asserts against the driver's
    INDEPENDENT ``count_combos`` total (``n_masks``) — defined over MIXED
    provenance after a mid-tuple latch.
    """
    max_enum = max_enumerated_for(n_bars, enum_cells)
    rank = 0
    enumerated = 0
    conflict_pruned = 0
    hits_counted = 0
    kept: list[int] = []
    oracle: Iterator[int] | None = None
    mixed = False
    while True:
        try:
            (
                enum_r,
                pruned_r,
                next_rank,
                exhausted,
                hits_counted_r,
                masks,
            ) = core.enumerate_prefilter(
                bits_sorted, k, pairs_sorted, min_hits, rank, max_enum, enum_cells
            )
        except (ValueError, TypeError, OverflowError) as exc:
            _warn_refusal("kernel_error", stage="enumerate", error=repr(exc))
            return None
        # `enum_no_progress` belt (§1.3 round-3 — structurally unreachable by
        # the guaranteed-progress law; a stubbed violation must fall back,
        # never busy-loop).
        if not exhausted and next_rank <= rank:
            _warn_refusal("enum_no_progress", start_rank=rank, next_rank=next_rank)
            return None
        if _shadow_open(width):
            if oracle is None:
                oracle = iter_masks_k_of_bits(bits_sorted, k)
            window = list(itertools.islice(oracle, enum_r))
            py_pruned, py_kept = _python_prefilter(window, bar_bits, min_hits)
            if len(window) != enum_r or py_pruned != pruned_r or py_kept != masks:
                _latch_enum(
                    width,
                    start_rank=rank,
                    core_enumerated=enum_r,
                    python_enumerated=len(window),
                    core_conflict_pruned=pruned_r,
                    python_conflict_pruned=py_pruned,
                    first_core_kept=mask_hex(masks[0]) if masks else None,
                    first_python_kept=mask_hex(py_kept[0]) if py_kept else None,
                )
                # Ship the PYTHON lists for the shadowed window, then the
                # MID-TUPLE CONTINUATION from rank + len(window) — the
                # PYTHON-side window length, never the untrusted core
                # next_rank (§5 round-6).
                enumerated += len(window)
                conflict_pruned += py_pruned
                kept.extend(py_kept)
                tail_enum, tail_pruned, tail_kept = _drain_python_remainder(
                    oracle, bar_bits, min_hits, max_enum
                )
                enumerated += tail_enum
                conflict_pruned += tail_pruned
                kept.extend(tail_kept)
                mixed = True
                break
            _consume_shadow_range(width, enum_r)
        rank = next_rank
        enumerated += enum_r
        conflict_pruned += pruned_r
        hits_counted += hits_counted_r
        kept.extend(masks)
        if exhausted:
            break
    # TOTAL-RECONCILIATION belt (§1.3 round-4): Σ enumerated — and, on the
    # pure-core path, the final rank — must equal the driver's independent
    # count_combos total; a mismatch serves the WHOLE tuple from the spine.
    if enumerated != n_masks or (not mixed and rank != n_masks):
        _warn_refusal(
            "enum_total_mismatch",
            enumerated=enumerated,
            final_rank=rank,
            n_masks=n_masks,
            mixed_provenance=mixed,
        )
        return None
    return EnumOutcome(
        enumerated=enumerated,
        conflict_pruned=conflict_pruned,
        evaluated=enumerated - conflict_pruned,
        kept=kept,
        hits_counted=hits_counted,
    )


# ═════════════════════════════════════════════════════════════════════════
# Slice 2 — the CORE/fold arm (``BRUTEX_SWEEP_RUST_CORE``): fold_block seam
# primitives (DESIGN_FINAL_RUST_SWEEP_CORE.md §1.3/§4/§5/§6). The generator +
# belt + restart WIRING lives in ``sweep_run.py``; this module owns the flag /
# knob resolution, the layout law, the decode refusal law, the width-keyed
# fold burn-in state, the mechanised-pure oracle, the bit-compare, and the
# fold-arm engagement chain (the §5 ARM-CONDITION PARTITION's CORE column).
# ═════════════════════════════════════════════════════════════════════════

#: The Slice-2 arm flag (§5 kill switches). Default OFF; flips default-ON only
#: in the Slice-4 ADR (the #1186→#1532 two-step).
SWEEP_RUST_CORE_ENV: Final[str] = "BRUTEX_SWEEP_RUST_CORE"

#: The ONE default-arm constant (revert lever).
SWEEP_RUST_CORE_DEFAULT: Final[bool] = False

#: The finalist-belt flag (§5) — default ON with the INVERSE fail-ON parse:
#: an unrecognised value must never silently disable the oracle verification
#: of persisted bytes while the core stays engaged (``belt_flag_unrecognised``).
SWEEP_RUST_CORE_BELT_ENV: Final[str] = "BRUTEX_SWEEP_RUST_CORE_BELT"

#: The fold work-cells knob (§3/§12.2) — fail-loud parse + dual clamp.
SWEEP_CORE_WORK_CELLS_ENV: Final[str] = "BRUTEX_SWEEP_CORE_WORK_CELLS"

#: Default fold ``masks × n_bars`` cells per ``fold_block`` call — re-derived
#: ON-BRANCH from the §9.1 PINNED rate (≈6.2 ns/mask·bar walk + ~2.1-2.7
#: ns/cell drawdown ⇒ ~8-9 ns/cell): 2**23 ≈ 70-75 ms/call nominal GIL hold
#: (the §3 "safe direction: LOWER the default cap — e.g. 2^23" instruction).
DEFAULT_FOLD_WORK_CELLS: Final[int] = 1 << 23

#: §3 fold-cells override clamps (upper 2**27 ≈ 1.1-1.2 s at the pinned rate;
#: lower 2**16 — the shared degenerate-override floor).
FOLD_CELLS_UPPER_CLAMP: Final[int] = 1 << 27
FOLD_CELLS_LOWER_CLAMP: Final[int] = 1 << 16

#: The driver-side chunk cap in MASKS (§3 — bounded return ≈ 800 KB).
SWEEP_CORE_CHUNK: Final[int] = 4096

#: The stall-guard oracle pricing knob (§5) — a PESSIMISTIC pure-Python
#: walker+tail per-bar rate, MEASURED on-branch 2026-07-10 (best-of-5
#: 3124.9 ns/bar over a 20k-bar synthetic window: ``simulate_trades`` +
#: ``build_equity_curve`` + ``compute_metrics``), committed with headroom.
#: Env override ``BRUTEX_SWEEP_CORE_ORACLE_NS_PER_BAR`` is fail-loud +
#: RAISE-ONLY (lower clamp AT this default — §5 round-6 clamp spec) with an
#: upper clamp at 1000× (one ``stall_guard_clamped`` warn per direction).
DEFAULT_ORACLE_NS_PER_BAR: Final[float] = 4000.0
SWEEP_CORE_ORACLE_NS_ENV: Final[str] = "BRUTEX_SWEEP_CORE_ORACLE_NS_PER_BAR"
ORACLE_NS_UPPER_FACTOR: Final[float] = 1000.0

#: §1.3 ONE layout law — the Python mirror of the Rust consts (the wheel
#: exports ``SWEEP_CORE_FOLD_{INT,FLOAT}_FIELDS``; the layout golden pins
#: literal tuple == tuple, and the engagement chain re-checks at runtime).
FOLD_INT_FIELDS: Final[tuple[str, ...]] = (
    "n_trades",
    "net_pnl",
    "gross_profit",
    "gross_loss_abs",
    "winners",
    "losers",
    "sum_mfe",
    "sum_mae",
    "sum_holding",
    "max_holding",
    "largest_loss",
    "max_consecutive_losses",
    "max_consecutive_wins",
    "max_dd_paisa",
    "max_dd_bars",
)
FOLD_FLOAT_FIELDS: Final[tuple[str, ...]] = (
    "mean",
    "m2",
    "m3",
    "m4",
    "sum_sq_downside",
    "p95",
    "p5",
    "max_dd_pct",
    "ulcer",
)
FOLD_INT_STRIDE: Final[int] = len(FOLD_INT_FIELDS)  # 15
FOLD_FLOAT_STRIDE: Final[int] = len(FOLD_FLOAT_FIELDS)  # 9

#: The decode law's hardcoded item width (CPython guarantees 'q'/'d' are a
#: MINIMUM of 8 bytes — the engagement chain probes for exactly 8).
_NATIVE_ITEMSIZE: Final[int] = 8

#: §1.3 flags law: bit0 zero_trade · bit1 python_required · bits 2-7 RESERVED
#: (a set reserved bit REFUSES the whole block — never a silent plain row).
FOLD_FLAG_ZERO_TRADE: Final[int] = 0x01
FOLD_FLAG_PYTHON_REQUIRED: Final[int] = 0x02
FOLD_FLAG_RESERVED_MASK: Final[int] = 0xFC

#: §5 fold burn-in (width-keyed, process-global — the #1531 precedent):
#: window closure needs BOTH ≥64 total core-served compares AND ≥16 PLAIN
#: (non-flag-served) rows, hard-capped at 512 total compares.
SWEEP_CORE_SELF_CHECK: Final[int] = 64
SWEEP_CORE_SELF_CHECK_NONTRIVIAL: Final[int] = 16
SWEEP_CORE_SELF_CHECK_CAP: Final[int] = 512

#: §5 sampled population spot-check: 16 deterministically-drawn retained
#: candidates; the first 4 non-finalists are served-vs-pure-oracle compared.
SWEEP_CORE_SPOT_CANDIDATES: Final[int] = 16
SWEEP_CORE_SPOT_CHECK: Final[int] = 4

#: Per-width fold burn-in state (documented no-global-mutable-state
#: exceptions — the enum shadow's siblings; ``reset_self_check_for_tests``).
_FOLD_TOTAL_REMAINING: Final[dict[int, int]] = {
    _WIDTH_U64: SWEEP_CORE_SELF_CHECK,
    _WIDTH_U128: SWEEP_CORE_SELF_CHECK,
}
_FOLD_PLAIN_REMAINING: Final[dict[int, int]] = {
    _WIDTH_U64: SWEEP_CORE_SELF_CHECK_NONTRIVIAL,
    _WIDTH_U128: SWEEP_CORE_SELF_CHECK_NONTRIVIAL,
}
_FOLD_CAP_REMAINING: Final[dict[int, int]] = {
    _WIDTH_U64: SWEEP_CORE_SELF_CHECK_CAP,
    _WIDTH_U128: SWEEP_CORE_SELF_CHECK_CAP,
}
_FOLD_LATCH: Final[dict[int, str | None]] = {_WIDTH_U64: None, _WIDTH_U128: None}


@dataclass(frozen=True, slots=True)
class CorePlan:
    """The once-per-invocation resolution of the fold arm's flag + knobs.

    ``requested`` = the arm flag verdict (tristate fail-SAFE OFF); ``belt`` =
    the finalist-belt flag (INVERSE fail-ON — §5); ``fold_cells`` the clamped
    per-call work budget; ``oracle_ns_per_bar`` the stall-guard pricing rate.
    An OFF plan never parsed any knob (defaults carried inert).
    """

    requested: bool
    belt: bool
    fold_cells: int
    oracle_ns_per_bar: float


class CoreDecodeRefused(Exception):
    """A ``fold_block`` return failed the §1.3 decode refusal law.

    ``reason`` is the NAMED ``kernel_result_refused`` sub-reason
    (``abi_version_mismatch`` / ``row_count_mismatch`` /
    ``ints_length_mismatch`` / ``floats_length_mismatch`` /
    ``reserved_flag_bits``). The caller treats it as a whole-tuple core
    fallback (the sweep_run tuple-restart path) — never a silent row drop.
    """

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason


@dataclass(frozen=True, slots=True)
class DecodedFoldBlock:
    """One decoded ``fold_block`` return: validated flags + columnar rows.

    ``ints``/``floats`` are the row-major stride-15/-9 columns; ``flags`` one
    byte per mask; ``scans_run`` the in-band zero-trade short-circuit oracle
    (§1.3 — test/observability data, never persisted).
    """

    scans_run: int
    ints: array[int]
    floats: array[float]
    flags: bytes


@dataclass(frozen=True, slots=True)
class CoreFoldContext:
    """An ENGAGED fold arm for one tuple (every §5 CORE-column condition met).

    Carries the constructed kernel + the per-tuple constants the metrics
    generator needs. ``span_years`` mirrors the shipping kernel-arm span
    expression exactly (``_metrics_from_trades_via_kernel``).
    """

    core: _SweepCoreFoldLike
    width: int
    n_bars: int
    span_years: float
    chunk_masks: int
    plan: CorePlan


class _SweepCoreFoldLike(Protocol):
    """The fold surface the Slice-2 seam consumes (both width pyclasses)."""

    def n_bars(self) -> int:
        """Resident bar count (the F27 seam-invariant probe)."""
        ...

    def fold_block(
        self,
        masks: list[int],
        is_buy: bool,
        sl_paisa: int | None,
        tp_paisa: int | None,
        sl_next_open: bool,
        tp_next_open: bool,
        tp_first: bool,
        start_equity_paisa: int,
    ) -> tuple[int, int, bytes, bytes, bytes]:
        """One §1.3 fold chunk → ``(abi, scans_run, ints_le, floats_le, flags)``."""
        ...


def resolve_sweep_rust_core_plan() -> CorePlan:
    """Resolve the fold arm's flag + belt + knobs (once per invocation).

    Arm flag: tristate fail-SAFE OFF (unrecognised non-empty → OFF + one
    warn-once ``flag_unrecognised`` on the core event). Belt flag: INVERSE
    fail-ON (unrecognised non-empty → ON + one warn-once
    ``belt_flag_unrecognised`` — a typo must never silently disable the
    oracle verification of persisted bytes, §5). Knobs parsed ONLY when the
    arm flag lands ON (an OFF-arm process with junk knobs is byte-identical
    to today); junk raises ``ValueError`` naming the env (§12.2 exemplar).
    """
    raw = os.environ.get(SWEEP_RUST_CORE_ENV, "")
    verdict = env_flag_tristate(raw)
    if verdict is None and raw.strip():
        _warn_core_once("flag_unrecognised", value=raw)
        return CorePlan(
            requested=False,
            belt=True,
            fold_cells=DEFAULT_FOLD_WORK_CELLS,
            oracle_ns_per_bar=DEFAULT_ORACLE_NS_PER_BAR,
        )
    requested = SWEEP_RUST_CORE_DEFAULT if verdict is None else verdict
    if not requested:
        return CorePlan(
            requested=False,
            belt=True,
            fold_cells=DEFAULT_FOLD_WORK_CELLS,
            oracle_ns_per_bar=DEFAULT_ORACLE_NS_PER_BAR,
        )
    belt_raw = os.environ.get(SWEEP_RUST_CORE_BELT_ENV, "")
    belt_verdict = env_flag_tristate(belt_raw)
    if belt_verdict is None:
        if belt_raw.strip():
            # INVERSE fail-ON: a typo'd belt flag must still BELT (§5).
            _warn_core_once("belt_flag_unrecognised", value=belt_raw)
        belt = True
    else:
        belt = belt_verdict
    return CorePlan(
        requested=True,
        belt=belt,
        fold_cells=_resolve_fold_cells(),
        oracle_ns_per_bar=_resolve_oracle_ns_per_bar(),
    )


def _resolve_fold_cells() -> int:
    """Parse + dual-clamp the fold work-cells knob (fail-loud, §3/§12.2)."""
    raw = os.environ.get(SWEEP_CORE_WORK_CELLS_ENV, "")
    if not raw.strip():
        return DEFAULT_FOLD_WORK_CELLS
    try:
        value = int(raw.strip())
    except ValueError as exc:
        msg = (
            f"{SWEEP_CORE_WORK_CELLS_ENV}={raw!r} is not an integer — set a cells "
            f"budget in [{FOLD_CELLS_LOWER_CLAMP}, {FOLD_CELLS_UPPER_CLAMP}] or unset it"
        )
        raise ValueError(msg) from exc
    if value > FOLD_CELLS_UPPER_CLAMP:
        _warn_clamp(
            direction="upper",
            requested=value,
            clamped=FOLD_CELLS_UPPER_CLAMP,
            env=SWEEP_CORE_WORK_CELLS_ENV,
        )
        return FOLD_CELLS_UPPER_CLAMP
    if value < FOLD_CELLS_LOWER_CLAMP:
        _warn_clamp(
            direction="lower",
            requested=value,
            clamped=FOLD_CELLS_LOWER_CLAMP,
            env=SWEEP_CORE_WORK_CELLS_ENV,
        )
        return FOLD_CELLS_LOWER_CLAMP
    return value


def _resolve_oracle_ns_per_bar() -> float:
    """Parse + clamp the stall-guard oracle pricing rate (§5 round-6 spec).

    Fail-loud parse; the LOWER clamp is AT the committed measured default —
    the override may only RAISE the priced rate (more pessimism ⇒ more
    refusals, the safe direction; the legitimate un-refuse lever is
    ``BRUTEX_SWEEP_STALL_SECONDS``); the UPPER clamp is 1000× the default.
    Each clamp emits one ``stall_guard_clamped`` warn-once per direction.
    """
    raw = os.environ.get(SWEEP_CORE_ORACLE_NS_ENV, "")
    if not raw.strip():
        return DEFAULT_ORACLE_NS_PER_BAR
    try:
        value = float(raw.strip())
    except ValueError as exc:
        msg = (
            f"{SWEEP_CORE_ORACLE_NS_ENV}={raw!r} is not a number — set a ns/bar "
            f"rate >= {DEFAULT_ORACLE_NS_PER_BAR} or unset it"
        )
        raise ValueError(msg) from exc
    if not math.isfinite(value):
        raise ValueError(
            f"{SWEEP_CORE_ORACLE_NS_ENV}={raw!r} must be finite — set a ns/bar "
            f"rate >= {DEFAULT_ORACLE_NS_PER_BAR} or unset it"
        )
    upper = DEFAULT_ORACLE_NS_PER_BAR * ORACLE_NS_UPPER_FACTOR
    if value > upper:
        _warn_stall_clamp(direction="upper", requested=value, clamped=upper)
        return upper
    if value < DEFAULT_ORACLE_NS_PER_BAR:
        _warn_stall_clamp(direction="lower", requested=value, clamped=DEFAULT_ORACLE_NS_PER_BAR)
        return DEFAULT_ORACLE_NS_PER_BAR
    return value


def _warn_stall_clamp(*, direction: str, requested: float, clamped: float) -> None:
    """One ``stall_guard_clamped`` warn per process per direction (§5)."""
    key = f"stall_guard_clamped:{direction}"
    if key in _OBSERVABILITY_MEMO:
        return
    _OBSERVABILITY_MEMO.add(key)
    _LOG.warning(
        "sweep_core.stall_guard_clamped",
        env=SWEEP_CORE_ORACLE_NS_ENV,
        direction=direction,
        requested=requested,
        clamped=clamped,
    )


def _warn_core_once(reason: str, **context: object) -> None:
    """Log ``sweep_rust_core.fallback`` once per process per *reason*."""
    key = f"core:{reason}"
    if key in _OBSERVABILITY_MEMO:
        return
    _OBSERVABILITY_MEMO.add(key)
    _LOG.warning("sweep_rust_core.fallback", reason=reason, **context)


def warn_core_refusal(reason: str, **context: object) -> None:
    """Log one ``sweep_rust_core.fallback`` WARNING for this tuple's refusal.

    The fold seam engages once per tuple, so an unconditional warn IS the
    per-(tuple, reason) law (§5) — loud per tuple, deliberately not memoised.
    """
    _LOG.warning("sweep_rust_core.fallback", reason=reason, **context)


def log_core_engaged(**context: object) -> None:
    """Log ``sweep_rust_core.engaged`` (INFO) once per process.

    The observable "the wired ON path actually runs fold_block" signal the
    arm-combination goldens count in BOTH directions (§7.1 negative pins).
    """
    key = "__core_engaged__"
    if key in _OBSERVABILITY_MEMO:
        return
    _OBSERVABILITY_MEMO.add(key)
    _LOG.info("sweep_rust_core.engaged", **context)


def warn_belt_preexisting(**context: object) -> None:
    """One ``belt_preexisting_divergence`` ERROR per process (§5).

    OFF-arm == core ≠ pure-oracle: the core faithfully reproduced today's
    shipping bytes and a PRE-EXISTING spine-vs-oracle divergence exists — the
    core (= spine) values ship, no restart, no latch; the belt's mirror of
    the burn-in's oracle-vs-oracle tripwire branch.
    """
    key = "__belt_preexisting__"
    if key in _OBSERVABILITY_MEMO:
        return
    _OBSERVABILITY_MEMO.add(key)
    _LOG.error("sweep_rust_core.belt_preexisting_divergence", **context)


def is_kernel_panic(exc: BaseException) -> bool:
    """True for a pyo3 ``PanicException`` surfacing as a caught exception.

    The seam maps it to the per-tuple ``SweepComboError`` path with reason
    ``kernel_panic_exception`` (§3 — the caught-exception panic shape; the
    ``panic="abort"`` profile makes the SIGABRT shape a worker death handled
    by DEC-SWEEP-STALL-OOM-GUARDS-001, not this predicate).
    """
    return type(exc).__name__ == "PanicException"


# ── fold burn-in state (width-keyed, §5) ─────────────────────────────────


def fold_burnin_open(width: int) -> bool:
    """True while ``width``'s fold burn-in window is still open (§5).

    Closure needs BOTH the 64-total AND 16-plain floors met, hard-capped at
    512 total compares (the cap trip warns ``burnin_nontrivial_floor_unmet``
    once and closes the window with the core still engaged).
    """
    if _FOLD_CAP_REMAINING.get(width, 0) <= 0:
        return False
    return _FOLD_TOTAL_REMAINING.get(width, 0) > 0 or _FOLD_PLAIN_REMAINING.get(width, 0) > 0


def fold_burnin_total_remaining(width: int) -> int:
    """The stall guard's burn-in term: the remaining 64-TOTAL requirement.

    Fix-forward item 7: priced at the 64-floor remaining count, NEVER the
    512 cap (the cap is the pathological zero-trade extension ceiling; those
    extra compares are walk-only-cheap — the under-pricing is disclosed and
    refuses-safe). A closed window reads 0.
    """
    if not fold_burnin_open(width):
        return 0
    return max(0, _FOLD_TOTAL_REMAINING.get(width, 0))


def consume_fold_burnin(width: int, *, plain: bool) -> None:
    """Account one core-served burn-in compare (flag-served rows included).

    ``plain`` = a non-flag-served row (exercises the float folds — the §5
    round-3 NON-TRIVIAL floor). A cap trip with the plain floor unmet warns
    ``burnin_nontrivial_floor_unmet`` once per width; the core stays engaged.
    """
    _FOLD_TOTAL_REMAINING[width] = max(0, _FOLD_TOTAL_REMAINING[width] - 1)
    _FOLD_CAP_REMAINING[width] = max(0, _FOLD_CAP_REMAINING[width] - 1)
    if plain:
        _FOLD_PLAIN_REMAINING[width] = max(0, _FOLD_PLAIN_REMAINING[width] - 1)
    if _FOLD_CAP_REMAINING[width] == 0 and _FOLD_PLAIN_REMAINING[width] > 0:
        _warn_core_once(
            "burnin_nontrivial_floor_unmet",
            width=width,
            plain_unmet=_FOLD_PLAIN_REMAINING[width],
            cap=SWEEP_CORE_SELF_CHECK_CAP,
        )


def fold_latch_reason(width: int) -> str | None:
    """The fold arm's per-width autorevert latch reason (None = live)."""
    return _FOLD_LATCH.get(width)


def latch_fold_kernel(width: int, reason: str, **context: object) -> None:
    """Flip the per-width fold autorevert latch (once) — a core defect.

    Logs ``sweep_rust_core.kernel_autoreverted`` (ERROR) on the first flip;
    the process stops trusting the fold kernel at this width (§5).
    """
    if _FOLD_LATCH[width] is None:
        _FOLD_LATCH[width] = reason
        _LOG.error("sweep_rust_core.kernel_autoreverted", width=width, reason=reason, **context)


def reset_self_check_for_tests(
    *,
    width: int | None = None,
    total_remaining: int = SWEEP_CORE_SELF_CHECK,
    plain_remaining: int = SWEEP_CORE_SELF_CHECK_NONTRIVIAL,
    cap_remaining: int = SWEEP_CORE_SELF_CHECK_CAP,
    latched: str | None = None,
) -> None:
    """Reset the per-width fold burn-in window + latch (test isolation ONLY).

    The fold twin of :func:`reset_enum_self_check_for_tests` (the #1531
    ``reset_self_check_for_tests`` naming precedent). ``width=None`` resets
    BOTH widths. Never called from production code paths.
    """
    widths = (_WIDTH_U64, _WIDTH_U128) if width is None else (width,)
    for w in widths:
        _FOLD_TOTAL_REMAINING[w] = total_remaining
        _FOLD_PLAIN_REMAINING[w] = plain_remaining
        _FOLD_CAP_REMAINING[w] = cap_remaining
        _FOLD_LATCH[w] = latched


# ── decode (§1.3 decode refusal law) ─────────────────────────────────────


def _host_byteorder() -> str:
    """The host byte order (patchable probe — the engagement precondition)."""
    return sys.byteorder


def _int_itemsize() -> int:
    """``array('q').itemsize`` (patchable probe — CPython guarantees ≥8, not ==8)."""
    return array("q").itemsize


def _float_itemsize() -> int:
    """``array('d').itemsize`` (patchable probe)."""
    return array("d").itemsize


def decode_fold_block(
    block: tuple[int, int, bytes, bytes, bytes], chunk_len: int
) -> DecodedFoldBlock:
    """Validate + decode one ``fold_block`` return (§1.3 decode refusal law).

    Validates — BEFORE yielding any row — the in-band ABI version, then
    ``len(flags) == chunk_len``, both buffer lengths against the stride × 8
    arithmetic, and the per-row reserved-bits check. Any failure raises
    :class:`CoreDecodeRefused` with the NAMED ``kernel_result_refused``
    sub-reason (the caller's whole-tuple fallback), never a silent partial
    or reinterpreted row.
    """
    abi, scans_run, ints_le, floats_le, flags = block
    if abi != EXPECTED_SWEEP_CORE_ABI_VERSION:
        raise CoreDecodeRefused(
            "abi_version_mismatch",
            f"fold_block returned ABI {abi}, expected {EXPECTED_SWEEP_CORE_ABI_VERSION}",
        )
    if len(flags) != chunk_len:
        raise CoreDecodeRefused(
            "row_count_mismatch",
            f"{len(flags)} flag rows for a chunk of {chunk_len} masks",
        )
    if len(ints_le) != chunk_len * FOLD_INT_STRIDE * 8:
        raise CoreDecodeRefused(
            "ints_length_mismatch",
            f"{len(ints_le)} int bytes for {chunk_len} rows × {FOLD_INT_STRIDE} × 8",
        )
    if len(floats_le) != chunk_len * FOLD_FLOAT_STRIDE * 8:
        raise CoreDecodeRefused(
            "floats_length_mismatch",
            f"{len(floats_le)} float bytes for {chunk_len} rows × {FOLD_FLOAT_STRIDE} × 8",
        )
    for index, flag in enumerate(flags):
        if flag & FOLD_FLAG_RESERVED_MASK:
            raise CoreDecodeRefused(
                "reserved_flag_bits",
                f"row {index} flags byte 0x{flag:02x} has reserved bits 2-7 set",
            )
    ints: array[int] = array("q")
    ints.frombytes(ints_le)
    floats: array[float] = array("d")
    floats.frombytes(floats_le)
    return DecodedFoldBlock(scans_run=scans_run, ints=ints, floats=floats, flags=flags)


# ── row → metrics (the §4 layer law: libm/None finishes stay Python) ─────


def fold_span_years(bars: Sequence[CandleBar], tf_minutes: int) -> float:
    """The shipping kernel-arm span expression, mirrored token-for-token.

    ``_metrics_from_trades_via_kernel``'s ``span_years`` (§11 COMMON is the
    EXPRESSION, single-sourced by the drift pin test): wall-clock span in
    minutes-per-year units, floored to one bar's worth for a degenerate
    zero-span window. Always > 0 for the config-legal ``tf_minutes >= 1``.
    """
    from brutex.engine.sweep_driver import _MINUTES_PER_YEAR

    span_seconds = (bars[-1].timestamp - bars[0].timestamp).total_seconds()
    span_years = span_seconds / (_MINUTES_PER_YEAR * 60.0)
    if span_years <= 0.0:
        span_years = tf_minutes / _MINUTES_PER_YEAR
    return span_years


def fold_row_metrics(
    decoded: DecodedFoldBlock,
    row: int,
    *,
    total_bars: int,
    start_equity_paisa: int,
    span_years: float,
) -> ResultMetrics:
    """Combine one PLAIN fold row into today's ``ResultMetrics`` (§1.4).

    Builds the Slice-0 :class:`~brutex.metrics.compute.TradeFolds` from the
    row's 15 ints + 9 floats (the §1.3 pairing — design suffix-less names ↔
    the repo ``_paisa``/``_bars`` spellings, pinned by the pairing test) and
    calls the verbatim Python combine tail — every libm / None-boundary /
    floor-div expression stays today's literal tokens (§4 layer law).
    """
    i0 = row * FOLD_INT_STRIDE
    f0 = row * FOLD_FLOAT_STRIDE
    ints = decoded.ints
    floats = decoded.floats
    folds = TradeFolds(
        n_trades=ints[i0 + 0],
        net_pnl_paisa=ints[i0 + 1],
        gross_profit_paisa=ints[i0 + 2],
        gross_loss_abs_paisa=ints[i0 + 3],
        winners=ints[i0 + 4],
        losers=ints[i0 + 5],
        sum_mfe_paisa=ints[i0 + 6],
        sum_mae_paisa=ints[i0 + 7],
        sum_holding_bars=ints[i0 + 8],
        max_holding_bars=ints[i0 + 9],
        largest_loss_paisa=ints[i0 + 10],
        max_consecutive_losses=ints[i0 + 11],
        max_consecutive_wins=ints[i0 + 12],
        moments=Moments(
            n=ints[i0 + 0],
            mean=floats[f0 + 0],
            m2=floats[f0 + 1],
            m3=floats[f0 + 2],
            m4=floats[f0 + 3],
        ),
        downside_sum_sq=floats[f0 + 4],
        p95=floats[f0 + 5],
        p5=floats[f0 + 6],
    )
    drawdown = (ints[i0 + 13], floats[f0 + 7], ints[i0 + 14], floats[f0 + 8])
    return combine_metrics_from_folds(
        folds,
        total_bars=total_bars,
        benchmark_return_pct=0.0,
        start_equity_paisa=start_equity_paisa,
        span_years=span_years,
        drawdown=drawdown,
    )


def zero_trade_metrics(
    *, total_bars: int, start_equity_paisa: int, span_years: float
) -> ResultMetrics:
    """Today's zero-trade row, byte-identical (§1.4 ``zero_trade`` arm).

    The literal shipping call — ``compute_metrics_from_scalars`` over an
    empty trade tuple with the analytically-exact ``FLAT_DRAWDOWN_STATS``
    (the ``_empty_metrics`` composition root; +0.0 signs Python-owned).
    """
    from brutex.engine.rust_metrics import FLAT_DRAWDOWN_STATS

    return compute_metrics_from_scalars(
        (),
        total_bars,
        start_equity_paisa=start_equity_paisa,
        span_years=span_years,
        drawdown=FLAT_DRAWDOWN_STATS,
    )


# ── bit-compare + mechanised-pure oracle (§5 burn-in arms) ───────────────


def metrics_compare_key(metrics: ResultMetrics) -> tuple[object, ...]:
    """The §5 bit-compare key: floats via ``struct.pack('<d')``, None-bits exact.

    Signed zeros and every ULP distinguished; ints exact; ``None`` compares
    only equal to ``None``. Field order = the dataclass declaration order.
    """
    key: list[object] = []
    for field in fields(metrics):
        value = getattr(metrics, field.name)
        if isinstance(value, float):
            key.append(struct.pack("<d", value))
        else:
            key.append(value)
    return tuple(key)


def metrics_equal(a: ResultMetrics, b: ResultMetrics) -> bool:
    """Bit-equality of two metrics rows under :func:`metrics_compare_key`."""
    return metrics_compare_key(a) == metrics_compare_key(b)


def disambiguate_core_mismatch(
    served: ResultMetrics, off_arm: ResultMetrics, pure: ResultMetrics
) -> str:
    """The §5 three-way verdict over (served, OFF-arm spine, pure oracle).

    Returns ``"clean"`` (all three agree), ``"preexisting"`` (OFF == served ≠
    pure — a pre-existing spine-vs-oracle divergence the core faithfully
    reproduced), ``"core_defect"`` (OFF == pure ≠ served — the two oracles
    agree and indict the kernel), or ``"threeway"`` (anything else —
    CONSERVATIVE, treated as a defect-with-unclean-evidence by the caller).
    """
    off_vs_pure = metrics_equal(off_arm, pure)
    off_vs_served = metrics_equal(off_arm, served)
    if off_vs_pure and off_vs_served:
        return "clean"
    if off_vs_served and not off_vs_pure:
        return "preexisting"
    if off_vs_pure and not off_vs_served:
        return "core_defect"
    return "threeway"


def _oracle_metrics_pure(
    bars: Sequence[CandleBar],
    bar_bits: Sequence[int],
    mask: int,
    *,
    direction: Side,
    tf_minutes: int,
    stop_loss_paisa: int | None,
    take_profit_paisa: int | None,
    start_equity_paisa: int,
) -> ResultMetrics:
    """The §5 MECHANISED-PURE oracle: zero ``fno_math`` involvement.

    The pure-Python walker (:func:`brutex.engine.trade_sim.simulate_trades`)
    composed DIRECTLY with :func:`~brutex.engine.sweep_driver.build_equity_curve`
    + the curve entry :func:`~brutex.metrics.compute.compute_metrics` (whose
    ``drawdown=None`` default runs the Python ``_drawdown_stats`` tail). It
    deliberately does NOT call ``metrics_for_mask``/``metrics_from_trades`` —
    under the ADR-0017 default those route the drawdown through the SAME
    ``fno_math`` fold body the new core consumes in-crate. Independence is
    MECHANISED by the closed-set AST pin + the runtime poison pin (§5).
    """
    from brutex.engine.sweep_driver import build_equity_curve
    from brutex.engine.trade_sim import simulate_trades

    entry_signals = mask_to_entry_signals(bar_bits, mask)
    trades = simulate_trades(
        bars,
        entry_signals,
        direction=direction,
        tf_minutes=tf_minutes,
        stop_loss_paisa=stop_loss_paisa,
        take_profit_paisa=take_profit_paisa,
    )
    curve = build_equity_curve(
        bars, trades, start_equity_paisa=start_equity_paisa, tf_minutes=tf_minutes
    )
    return compute_metrics(trades, curve, len(bars))


def spot_check_candidates(kept: Sequence[int], tuple_digest: str) -> list[int]:
    """Draw the ≤16 spot-check candidate masks DETERMINISTICALLY (§5).

    Seeded from the tuple digest over the kept list (arm-identical, so the
    draw is reproducible on every engine); ≤16 kept masks = the whole list.
    Flag-served masks are deliberately INCLUDED (a wrongly-flagged
    ``zero_trade`` mask is a candidate — the §5 round-2 pin).
    """
    if len(kept) <= SWEEP_CORE_SPOT_CANDIDATES:
        return list(kept)
    import random

    # Determinism, not cryptography — the draw must be reproducible from
    # the tuple digest on every engine/arm (the §5 seeded-draw law).
    rng = random.Random(int(tuple_digest, 16))  # noqa: S311
    positions = sorted(rng.sample(range(len(kept)), SWEEP_CORE_SPOT_CANDIDATES))
    return [kept[position] for position in positions]


def fold_chunk_masks(n_bars: int, fold_cells: int) -> int:
    """Driver-side fold chunk sizing (§3 guaranteed-progress law).

    ``min(SWEEP_CORE_CHUNK, max(1, cells // max(1, n_bars)))`` — the
    DIVISOR-floored repo idiom VERBATIM; total on ``n_bars == 0`` and never
    below one mask (a hostile cap degrades to one-mask-per-call slowness,
    never a hang and never an empty result).
    """
    return min(SWEEP_CORE_CHUNK, max(1, fold_cells // max(1, n_bars)))


def core_fold_context(
    *,
    bars: Sequence[CandleBar],
    bar_bits: Sequence[int],
    condition_bits: Sequence[int],
    tf_minutes: int,
    n_masks: int,
    digest_path: bool,
    top_k: int | None,
    plan: CorePlan,
) -> CoreFoldContext | None:
    """Engage the fold arm for one tuple, or refuse (the §5 CORE column).

    Conditions evaluated — and the ONE fallback reason named — in the §5
    listing order (the FALLBACK-REASON PRECEDENCE LAW): digest path → wheel
    → ABI → byteorder → itemsize → exit-index certificate → finite top_k →
    width → per-width latch → stall guard (full formula) → construction →
    the F27 window-length invariant. Every refusal returns ``None`` (warned
    once per tuple+reason); the caller serves today's byte-identical spine.
    """
    if not plan.requested:
        return None
    # 1. digest path.
    if not digest_path:
        warn_core_refusal("legacy_dedup_path")
        return None
    # 2. wheel symbols (incl. the exported layout tuples — the runtime arm of
    # the layout golden's tuple == tuple law).
    module = _load_kernel_module()
    if module is None:
        warn_core_refusal("wheel_missing")
        return None
    core_cls = getattr(module, "ResidentSweepCore", None)
    core_cls_u128 = getattr(module, "ResidentSweepCoreU128", None)
    abi_version = getattr(module, "SWEEP_CORE_ABI_VERSION", None)
    wheel_int_fields = getattr(module, "SWEEP_CORE_FOLD_INT_FIELDS", None)
    wheel_float_fields = getattr(module, "SWEEP_CORE_FOLD_FLOAT_FIELDS", None)
    if (
        core_cls is None
        or core_cls_u128 is None
        or abi_version is None
        or wheel_int_fields is None
        or wheel_float_fields is None
        or tuple(wheel_int_fields) != FOLD_INT_FIELDS
        or tuple(wheel_float_fields) != FOLD_FLOAT_FIELDS
    ):
        warn_core_refusal("wheel_stale")
        return None
    # 3. ABI version.
    if abi_version != EXPECTED_SWEEP_CORE_ABI_VERSION:
        warn_core_refusal(
            "abi_version_mismatch",
            wheel_abi=abi_version,
            expected_abi=EXPECTED_SWEEP_CORE_ABI_VERSION,
        )
        return None
    # 4. byte order (the LE byte transport's precondition).
    if _host_byteorder() != "little":
        warn_core_refusal("big_endian_host", byteorder=_host_byteorder())
        return None
    # 5. native widths (the decode law's ×8 arithmetic).
    if _int_itemsize() != _NATIVE_ITEMSIZE or _float_itemsize() != _NATIVE_ITEMSIZE:
        warn_core_refusal(
            "native_itemsize_mismatch",
            int_itemsize=_int_itemsize(),
            float_itemsize=_float_itemsize(),
        )
        return None
    # 6. per-tuple exit-index certificate (§4 row 7 — whole-tuple fallback).
    if not bars_strictly_increasing(bars):
        warn_core_refusal("ts_uncertified")
        return None
    # 7. finite top_k (the belt-bound refusal — top_k=None would degenerate
    # the default-ON belt to a full-population pure re-walk per tuple).
    if top_k is None:
        warn_core_refusal("unbounded_top_k")
        return None
    # 8. width selection (bit-index-keyed, empty-safe — shared with the enum
    # arm; evaluated OUTSIDE the narrow kernel_error catch).
    bits_sorted = sorted(set(condition_bits))
    pairs_sorted = sorted(CONFLICT_PAIRS)
    pair_indices = [index for pair in pairs_sorted for index in pair]
    width_bits = _select_width(bar_bits, bits_sorted, pair_indices)
    if width_bits <= _WIDTH_U64:
        width = _WIDTH_U64
        chosen_cls = core_cls
    elif width_bits <= _WIDTH_U128:
        width = _WIDTH_U128
        chosen_cls = core_cls_u128
    else:
        warn_core_refusal("width_beyond_u128", width_bits=width_bits)
        return None
    # 9. per-width fold autorevert latch.
    if _FOLD_LATCH[width] is not None:
        warn_core_refusal("kernel_disabled", width=width, latch=_FOLD_LATCH[width])
        return None
    # 10. STALL-BUDGET GUARD — the FULL fold-arm formula (§5): the burn-in /
    # belt / spot-candidate oracle walks priced at oracle_ns_per_bar plus the
    # enum shadow term. TOTAL evaluation: any escape refuses, never crashes.
    n_bars = len(bars)
    try:
        deadline = _effective_stall_deadline(n_masks, n_bars)
    except Exception as exc:
        warn_core_refusal("stall_guard_unavailable", error=repr(exc))
        return None
    if deadline is None:
        warn_core_refusal("stall_guard_unavailable", error="stall symbols unresolvable")
        return None
    oracle_masks = fold_burnin_total_remaining(width) + top_k + SWEEP_CORE_SPOT_CANDIDATES
    overhead_s = (
        oracle_masks * n_bars * plan.oracle_ns_per_bar
        + enum_shadow_masks_remaining(width) * (ENUM_SHADOW_NS + n_bars * PREFILTER_NS_PER_BAR)
    ) * 1e-9
    budget = SWEEP_CORE_STALL_BUDGET_FRACTION * deadline
    if overhead_s > budget:
        warn_core_refusal(
            "stall_budget_exceeded",
            overhead_s=overhead_s,
            budget_s=budget,
            deadline_s=deadline,
        )
        return None
    # 11. core construction (the narrow kernel_error catch; a pyo3
    # PanicException surfacing here is mapped by the caller's tuple path).
    open_p, high_p, low_p, close_p, forced, session_days = derive_kernel_inputs(bars, tf_minutes)
    try:
        core = cast(
            "_SweepCoreFoldLike",
            chosen_cls(open_p, high_p, low_p, close_p, forced, session_days, list(bar_bits)),
        )
    except (ValueError, TypeError, OverflowError) as exc:
        warn_core_refusal("kernel_error", stage="construct", error=repr(exc))
        return None
    # 12. F27 window-length seam invariant.
    core_bars = core.n_bars()
    if core_bars != n_bars:
        warn_core_refusal("bars_length_mismatch", core_n_bars=core_bars, python_n_bars=n_bars)
        return None
    log_core_engaged(width=width, n_bars=n_bars)
    return CoreFoldContext(
        core=core,
        width=width,
        n_bars=n_bars,
        span_years=fold_span_years(bars, tf_minutes),
        chunk_masks=fold_chunk_masks(n_bars, plan.fold_cells),
        plan=plan,
    )
