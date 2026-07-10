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
carrying a ``direction`` field. Parsed ONLY when the arm flag is ON, so an
OFF-arm process with a junk knob stays byte-identical to today. The §12.2
LAUNCH-eager arm lives in ``cmd_sweep_all`` (the ``check_sweep_sla``
exemplar position): the plan is resolved once there, BEFORE the grid's bar
preload, so a junk knob is a clean ``EXIT_USER_ERROR`` one-liner at launch —
never a raw mid-grid ``ValueError`` after the preload. ``run_persisted_sweep``
then re-resolves per invocation (same env, same verdict — eager relative to
the TUPLE, before any per-mask/FFI work; spawn workers inherit the
launch-validated env).

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
8. input derivation + core construction from the SAME single-source columns
   (:func:`brutex.engine.rust_trade_sim.derive_kernel_inputs`) — the
   derivation is TOTAL at the seam (ANY failure, e.g. a session-calendar
   ``HolidayDataUnavailableError`` on out-of-coverage bar dates, refuses
   with ``input_derivation_error`` so the byte-identical Python spine
   reproduces today's exact behavior: the #904 per-tuple ``SweepComboError``
   skip when the kept set is non-empty, clean completion when it is empty —
   never a raw grid-wide abort past the ``SweepComboError``-only
   quarantine; ``MemoryError`` re-raises BARE, the engine OOM global-stop
   law); construction rides the narrow ``kernel_error`` catch
   ``(ValueError, TypeError, OverflowError)``;
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
the ranges cap warns once per width (``enum_shadow_floor_unmet``, memoised
per WIDTH — a u64 floor-unmet close never suppresses the later u128 one)
under its own ``sweep_rust_enum.shadow_floor_unmet`` event — deliberately
NOT the ``fallback`` event, because no fallback occurs (the kernel's results
still ship; the window merely closes — the genuine degenerate-stream
signal). The window state + latch
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
  engaged width PLUS up to one range's overshoot: the final open range is
  compared WHOLE (the range is the §5 comparison unit), so on a small-bar
  window (``max_enumerated`` at the 2**20 ceiling) the actual shadowed count
  can exceed the floor by up to ``max_enumerated − 1`` masks (~seconds-scale
  once per process per width). The stall guard's enum term prices
  ``remaining + (max_enumerated − 1)`` — the worst case, erring toward
  refusing (§5).
* Core construction derives the full 7-column trade-sim inputs (the frozen
  §1.3 constructor contract — Slice 2's ``fold_block`` consumes them), an
  O(N_bars) cost per engaged tuple; the derivation is TOTAL at the seam — a
  session-calendar failure there refuses (``input_derivation_error``) to the
  byte-identical Python spine, which reproduces today's exact per-tuple
  behavior (never a raw grid-wide abort; ``MemoryError`` re-raises bare).
"""

from __future__ import annotations

import itertools
import os
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Protocol, cast

import structlog

from brutex.engine.combo_mask import count_supermasks, iter_masks_k_of_bits
from brutex.engine.rust_trade_sim import derive_kernel_inputs
from brutex.engine.sweep_lookahead import env_flag_tristate
from brutex.vocabulary import CONFLICT_PAIRS, has_conflict

if TYPE_CHECKING:  # pragma: no cover - import only for static typing
    from brutex.data.models import CandleBar

__all__ = [
    "DEFAULT_ENUM_WORK_CELLS",
    "ENUM_CELLS_LOWER_CLAMP",
    "ENUM_CELLS_UPPER_CLAMP",
    "ENUM_SHADOW_NS",
    "EXPECTED_SWEEP_CORE_ABI_VERSION",
    "PREFILTER_NS_PER_BAR",
    "SWEEP_CORE_ENUM_CELLS_ENV",
    "SWEEP_CORE_STALL_BUDGET_FRACTION",
    "SWEEP_ENUM_SELF_CHECK_MASKS",
    "SWEEP_ENUM_SELF_CHECK_RANGES_CAP",
    "SWEEP_RUST_ENUM_DEFAULT",
    "SWEEP_RUST_ENUM_ENV",
    "EnumOutcome",
    "EnumPlan",
    "core_enumerate_kept",
    "enum_shadow_masks_remaining",
    "mask_hex",
    "max_enumerated_for",
    "reset_enum_self_check_for_tests",
    "reset_observability_memo_for_tests",
    "resolve_sweep_rust_enum_plan",
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


def _warn_clamp(*, direction: str, requested: int, clamped: int) -> None:
    """One loud ``work_cap_clamped`` warn per process per direction (§3)."""
    key = f"work_cap_clamped:{direction}"
    if key in _OBSERVABILITY_MEMO:
        return
    _OBSERVABILITY_MEMO.add(key)
    _LOG.warning(
        "sweep_core.work_cap_clamped",
        env=SWEEP_CORE_ENUM_CELLS_ENV,
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
    """Account one shadowed range; warn ``enum_shadow_floor_unmet`` once per
    WIDTH on a ranges-cap exhaustion that leaves the mask floor unmet (§5
    audit C51 — the width-keyed degenerate-stream signal feeding the flip-ADR
    residual list, so a u64 close must never suppress the u128 one). Emitted
    under its own ``sweep_rust_enum.shadow_floor_unmet`` event — deliberately
    NOT ``fallback``: no fallback occurs (the kernel's results still ship)."""
    _SHADOW_MASKS_REMAINING[width] = max(0, _SHADOW_MASKS_REMAINING[width] - shadowed_masks)
    _SHADOW_RANGES_REMAINING[width] = max(0, _SHADOW_RANGES_REMAINING[width] - 1)
    if _SHADOW_RANGES_REMAINING[width] == 0 and _SHADOW_MASKS_REMAINING[width] > 0:
        key = f"enum_shadow_floor_unmet:{width}"
        if key in _OBSERVABILITY_MEMO:
            return
        _OBSERVABILITY_MEMO.add(key)
        _LOG.warning(
            "sweep_rust_enum.shadow_floor_unmet",
            reason="enum_shadow_floor_unmet",
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
    # crash — the guard runs OUTSIDE the narrow kernel_error catch;
    # MemoryError re-raises BARE, the engine OOM global-stop law).
    n_bars = len(bars)
    try:
        deadline = _effective_stall_deadline(n_masks, n_bars)
    except MemoryError:
        raise
    except Exception as exc:
        _warn_refusal("stall_guard_unavailable", error=repr(exc))
        return None
    if deadline is None:
        _warn_refusal("stall_guard_unavailable", error="stall symbols unresolvable")
        return None
    # The enum term prices the WORST-case shadow cost: the remaining floor
    # PLUS one range's overshoot — the final open range is compared WHOLE
    # (the §5 comparison unit), so the actual shadowed count can exceed the
    # floor by up to `max_enumerated − 1` masks (module docstring, honest
    # envelope). Erring toward refusing, per §5's own pricing law.
    max_enum = max_enumerated_for(n_bars, plan.enum_cells)
    shadow_masks = enum_shadow_masks_remaining(width)
    priced_masks = shadow_masks + (max_enum - 1) if shadow_masks > 0 else 0
    overhead_s = priced_masks * (ENUM_SHADOW_NS + n_bars * PREFILTER_NS_PER_BAR) * 1e-9
    budget = SWEEP_CORE_STALL_BUDGET_FRACTION * deadline
    if overhead_s > budget:
        _warn_refusal(
            "stall_budget_exceeded",
            overhead_s=overhead_s,
            budget_s=budget,
            deadline_s=deadline,
        )
        return None
    # 8. input derivation + core construction — the SAME single-source
    # columns ResidentTradeSim derives (§1.2). The derivation is TOTAL at
    # the seam: ANY failure (e.g. a session-calendar
    # HolidayDataUnavailableError on out-of-coverage bar dates — a real
    # production shape once lake bars pass the calendar coverage span, per
    # the #1294 ingest abstain-and-KEEP law) refuses to the byte-identical
    # Python spine, which reproduces today's EXACT behavior for the tuple:
    # `_build_trade_sim` converts the same raise into the #904 per-tuple
    # SweepComboError skip when `kept` is non-empty, and a kept-empty tuple
    # completes clean — never a raw grid-wide abort past the parallel/grid
    # drivers' SweepComboError-only quarantine (§5 law (d): every
    # mis-application errs toward the spine). MemoryError re-raises BARE.
    try:
        open_p, high_p, low_p, close_p, forced, session_days = derive_kernel_inputs(
            bars, tf_minutes
        )
    except MemoryError:
        raise
    except Exception as exc:
        _warn_refusal("input_derivation_error", error=repr(exc))
        return None
    # The FFI validation errors ride the narrow kernel_error catch.
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
        n_masks=n_masks,
        width=width,
        enum_cells=plan.enum_cells,
        max_enum=max_enum,
    )


def _drive(
    core: _SweepCoreLike,
    *,
    bits_sorted: list[int],
    k: int,
    pairs_sorted: list[tuple[int, int]],
    min_hits: int,
    bar_bits: Sequence[int],
    n_masks: int,
    width: int,
    enum_cells: int,
    max_enum: int,
) -> EnumOutcome | None:
    """The §1.4 range-driver loop: shadow → belts → tallies → kept.

    Loops on the IN-BAND ``exhausted`` flag (the §1.3 exhaustion contract),
    never a "returned empty" heuristic; the ≥1-mask progress law makes every
    non-exhausted call advance rank (belted by ``enum_no_progress``). On loop
    exit the §1.3 TOTAL-RECONCILIATION belt asserts against the driver's
    INDEPENDENT ``count_combos`` total (``n_masks``) — defined over MIXED
    provenance after a mid-tuple latch. ``max_enum`` is the caller's
    :func:`max_enumerated_for` bound — computed ONCE (the stall guard prices
    its one-range shadow slop from the SAME value).
    """
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
