"""Flag-gated Rust sweep-enumeration seam — the fused enum+prune kernel binding.

DEC-SWEEP-ENUM-RUST-001 (MIGRATION.md #1738; umbrella
DEC-RUST-FIRST-MANDATE-001) — slice 1 of the Rust kernel track. The sweep's
mask-source segment (:func:`brutex.engine.combo_mask.iter_masks_k_of_bits`
FUSED with the per-mask :func:`brutex.vocabulary.has_conflict` prune —
``C(60, 5) = 5.46M`` Python iterations per full-vocabulary tuple) is served by
the ``fno_math.sweep_enum_conflict_free`` Rust kernel when
``BRUTEX_SWEEP_RUST_ENUM`` is enabled. This module is the Python side: the
env-flag resolution, the lazy kernel binding, the pre-validation that keeps
the error contract flag-invariant, the count ceiling, the result domain
validation, the burn-in self-check with the autorevert latch, and the
LOUD-but-flood-safe fallback logging. The ONE consumption seam lives in
:mod:`brutex.engine.sweep_run` (digest-path only — the legacy no-digest dedup
arm and the Apriori arm stay pure Python by contract).

## The flag — ``BRUTEX_SWEEP_RUST_ENUM`` over ``SWEEP_RUST_ENUM_DEFAULT``

:func:`sweep_rust_enum_enabled` reads the env per call, exactly like the
sibling :func:`brutex.engine.rust_metrics.sweep_rust_metrics_enabled` (whose
``_TRUTHY`` / ``_FALSY`` literal sets are consumed through the PUBLIC
:func:`~brutex.engine.sweep_lookahead.env_flag_tristate` classifier, not
copied — CLAUDE.md §11 COMMON). Per-call resolution (one ``os.environ`` dict
probe against a once-per-TUPLE seam — nil) was chosen over a once-per-process
memo so that (a) there is no module-global FLAG state, and (b) every
``sweep_parallel`` worker process reads its OWN inherited environment at call
time. Resolution: a ``_TRUTHY`` value forces ON, a ``_FALSY`` value forces
OFF, unset/whitespace-empty falls to :data:`SWEEP_RUST_ENUM_DEFAULT`
(**False — ships inert**; the default-ON flip is a SEPARATE later ADR, the
rust_metrics/ADR-0017 precedent), and any OTHER value fails SAFE to OFF with
one warn-once ``flag_unrecognised`` event — under a default-OFF kernel a
typo'd ENABLE attempt (``0n``, ``enable``) must be VISIBLE, never silently
inert.

## The order law (Golden Rule #4 — why bit-identity is load-bearing)

The kernel returns survivors in the EXACT ``itertools.combinations``
lexicographic order of the Python generator (NOT numeric-ascending mask
order — the two differ from n = 4). Enumeration order feeds the sweep's chunk
boundaries, ``SweepComboError`` attribution, stable-sort tie-breaks and the
trial-stats sidecar counters, so a set-equal but order-drifted list would
produce DIFFERENT durable bytes — and the crash-redo contract requires a
byte-identical rewrite even across a flag flip between attempts.

## Burn-in self-check + autorevert latch (the rust_metrics idiom)

For the first :data:`SELF_CHECK_CALLS` kernel-served calls per process the
wrapper ALSO runs the live Python oracle prefix
(``islice`` over ``iter_masks_k_of_bits`` filtered by ``has_conflict``) and
compares element-for-element — the FULL list when the tuple is small
(``enumerated <= FULL_CHECK_ENUMERATED``), else the first
:data:`PREFIX_CHECK_SURVIVORS` survivors. A mismatch SHIPS THE ORACLE PATH
(the caller falls back to the untouched Python loop) and latches the kernel
OFF for the rest of the process (:func:`disable_kernel`, one
``sweep_rust_enum.kernel_autoreverted`` ERROR); every later call
short-circuits with a warn-once ``kernel_disabled``. The latch/counter are
the same *documented exception* to the no-global-mutable-state rule as the
observability memo; :func:`reset_self_check_for_tests` restores a fresh
process view.

## Fallback + engagement observability (Golden Rule #10)

Every fallback reason logs ``sweep_rust_enum.fallback`` (WARNING) ONCE per
process per reason. A kernel result that fails the domain validation is
REFUSED loudly (``sweep_rust_enum.kernel_result_refused``, ERROR — a refused
result indicates a kernel BUG, never adopted silently). The first successful
kernel call logs ``sweep_rust_enum.engaged`` (INFO) — the observable "the
wired ON path actually runs Rust" signal the golden suite asserts (the
vacuous-gate trap). :func:`reset_observability_memo_for_tests` restores a
fresh process view for test isolation.

## Honest envelope (Golden Rule #10)

* Constant-factor win only: the segment stays ``O(C(n_unique, k) ·
  (k + n_pairs))`` work — no O-class change, no GIL release (the kernel runs
  serial, GIL-held — the recorded crate stance), and NO whole-sweep speedup
  claim (the trade sim dominates a full k=5 1-minute tuple; the enum segment
  is ~1-3% of its wall).
* The O(1) domain gate + the prefix burn-in cannot catch a TAIL-region
  order/set divergence at runtime after burn-in; that layer is owned by the
  property differential + golden + in-crate proptest + mutation gates — the
  same layering ``rust_metrics._valid_drawdown`` documents.
* Invalid input never raises HERE: the pre-validation returns ``None``
  silently and the untouched Python generator raises the canonical
  ``ValueError`` at first ``next()`` — error behaviour is flag-invariant by
  construction (``combo_mask.py`` / ``conflicts.py`` are never edited).
* ``MemoryError`` from the FFI (the kernel's ``try_reserve_exact`` failure)
  re-raises BARE — OOM is a GLOBAL stop (DEC-LAKE-WRITE-HALT-001), never
  swallowed into a fallback.
* An env flip MID-run takes effect on the next tuple (per-call read);
  results are bit-identical either way.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from itertools import islice
from typing import TYPE_CHECKING, Final, TypeGuard

import structlog

from brutex.engine.combo_mask import MAX_VOCAB_BITS, count_combos, iter_masks_k_of_bits

# The truthy + falsy literal sets are defined ONCE on the sweep-flag vocabulary
# home and consumed through its PUBLIC ``env_flag_tristate`` classifier — zero
# copied strip/lower/membership logic (CLAUDE.md §11 COMMON; the rust_metrics
# consumer contract).
from brutex.engine.sweep_lookahead import env_flag_tristate
from brutex.vocabulary import CONFLICT_PAIRS, has_conflict

if TYPE_CHECKING:  # pragma: no cover - import only for static typing
    from collections.abc import Callable, Sequence

_LOG = structlog.get_logger("brutex.engine.rust_enum")

#: The env flag. A ``_TRUTHY`` value forces the Rust enum kernel ON, a
#: ``_FALSY`` value forces it OFF, unset/empty falls to
#: :data:`SWEEP_RUST_ENUM_DEFAULT`, and anything unrecognised fails SAFE to
#: OFF with one warn-once ``flag_unrecognised`` event (a typo'd ENABLE
#: attempt must be visible, never silently inert).
SWEEP_RUST_ENUM_ENV: Final[str] = "BRUTEX_SWEEP_RUST_ENUM"

#: The ONE default-arm constant: what :func:`sweep_rust_enum_enabled` returns
#: when the env flag is unset or whitespace-empty. ``False`` = the kernel
#: SHIPS INERT (the standing default-OFF contract for new Rust kernels); the
#: default-ON flip is a SEPARATE later ADR once the residuals are cleared
#: (the DEC-SWEEP-RUST-METRICS-001 → ADR-0017 lever pattern). Flip = this ONE
#: constant.
SWEEP_RUST_ENUM_DEFAULT: Final[bool] = False

#: FM-12 materialisation ceiling: the wrapper refuses (falls back to the lazy
#: Python generator, warn-once ``count_ceiling``) when ``C(n_unique, k)``
#: exceeds this bound — 2²⁴ ≈ 16.7M masks ≈ 268 MB of u128 survivors, safely
#: above the production worst case ``C(60, 5) = 5,461,512`` and safely below
#: anything that could pressure the §8 RAM envelope. The same value is passed
#: to the kernel as its ``max_masks`` belt (policy lives HERE, once).
ENUM_KERNEL_MAX_MASKS: Final[int] = 1 << 24

#: Kernel-served calls burn-in-compared against the live Python oracle per
#: process (the rust_metrics ``SELF_CHECK_SAMPLES`` idiom — module docstring).
SELF_CHECK_CALLS: Final[int] = 16

#: Burn-in head-region depth: survivors compared element-for-element against
#: the oracle prefix on LARGE tuples (FM-1 — the order law's runtime spot
#: check; the tail region is the property/golden suites' layer).
PREFIX_CHECK_SURVIVORS: Final[int] = 4096

#: Tuples at or below this enumerated count get a FULL burn-in compare (the
#: whole survivor list vs the whole oracle list — catches tail divergence and
#: a dropped-tail kernel on every small tuple).
FULL_CHECK_ENUMERATED: Final[int] = 65_536

#: The conflict-pair table crossing the FFI AS DATA, in deterministic sorted
#: order (``CONFLICT_PAIRS`` is a frozenset — sorting keeps the call
#: byte-reproducible; the SET semantics make order irrelevant to the result).
#: NEVER re-minted in Rust (FM-5); imported from the ONE vocabulary home.
_CONFLICT_PAIRS_SORTED: Final[tuple[tuple[int, int], ...]] = tuple(sorted(CONFLICT_PAIRS))

#: Once-per-process-per-reason fallback memo + the engaged-once latch.
#: LOG-VOLUME state ONLY (module docstring): results never depend on it.
_OBSERVABILITY_MEMO: Final[set[str]] = set()

#: Memo key for the engaged-once INFO (namespaced apart from warn reasons).
_ENGAGED_KEY: Final[str] = "__engaged__"

#: Arity of the kernel result triple ``(survivors, enumerated, conflict_pruned)``.
_RESULT_ARITY: Final[int] = 3

#: Self-check calls still to run this process (single-cell mutable — the
#: documented no-global-mutable-state exception, module docstring).
_SELF_CHECK_REMAINING: Final[list[int]] = [SELF_CHECK_CALLS]

#: The autorevert latch: the mismatch reason once the kernel is disabled for
#: this process, else ``None`` (same documented exception as above).
_KERNEL_DISABLED_REASON: Final[list[str | None]] = [None]


@dataclass(frozen=True, slots=True)
class RustEnumResult:
    """A kernel-served enumeration: the loop-equivalent triple.

    ``survivors`` is the conflict-free mask list in ``itertools.combinations``
    lexicographic order — exactly what the sweep loop's ``survivors`` list
    holds on the digest path; ``enumerated`` / ``conflict_pruned`` are the
    honest tallies the loop would have counted
    (``enumerated == len(survivors) + conflict_pruned`` by the domain gate).
    """

    survivors: list[int]
    enumerated: int
    conflict_pruned: int


def sweep_rust_enum_enabled() -> bool:
    """Resolve the Rust-enum flag over :data:`SWEEP_RUST_ENUM_DEFAULT`.

    * a ``_TRUTHY`` value (``1`` / ``true`` / ``yes`` / ``on``,
      case-insensitive, surrounding whitespace ignored) → ``True``;
    * a ``_FALSY`` value (``0`` / ``false`` / ``no`` / ``off``) → ``False``;
    * unset or whitespace-empty → :data:`SWEEP_RUST_ENUM_DEFAULT` (the
      one-constant flip lever — ``False``, ships inert);
    * any OTHER value → ``False`` with one warn-once ``flag_unrecognised``
      fallback event. Under a default-OFF kernel the unrecognised arm
      COINCIDES with the default, but the warn stays: a typo'd ENABLE
      attempt (``0n``, ``enable``) must be VISIBLE, never silently inert —
      the rust_metrics fail-safe law with the direction flipped.

    The vocabulary is the SAME ``_TRUTHY`` / ``_FALSY`` pair as every sweep
    env flag, classified through the COMMON
    :func:`brutex.engine.sweep_lookahead.env_flag_tristate` (this function
    owns only the default + warn policy for the ``None`` verdict). Read per
    call — see the module docstring for why (no global flag state; parallel
    workers read their own env).
    """
    raw = os.environ.get(SWEEP_RUST_ENUM_ENV, "")
    verdict = env_flag_tristate(raw)
    if verdict is not None:
        return verdict
    if raw.strip():
        warn_fallback_once("flag_unrecognised", value=raw)
        return False
    return SWEEP_RUST_ENUM_DEFAULT


def self_check_pending() -> bool:
    """Return ``True`` while this process still owes burn-in self-check calls."""
    return _SELF_CHECK_REMAINING[0] > 0


def consume_self_check_sample() -> None:
    """Consume one burn-in call after a PASSED kernel-vs-oracle compare.

    Clamped at zero so a racing extra call can never go negative. A FAILED
    compare never consumes — it latches :func:`disable_kernel` instead.
    """
    if _SELF_CHECK_REMAINING[0] > 0:
        _SELF_CHECK_REMAINING[0] -= 1


def kernel_disabled() -> bool:
    """Return ``True`` iff the autorevert latch has disabled the kernel this process."""
    return _KERNEL_DISABLED_REASON[0] is not None


def disable_kernel(reason: str, **context: object) -> None:
    """Latch the kernel OFF for the remainder of the process (the autorevert).

    Called by the burn-in self-check on a survivor-list MISMATCH — a kernel
    order/set bug the O(1) domain validation cannot see. The first call logs
    ``sweep_rust_enum.kernel_autoreverted`` (ERROR — a genuine
    kernel-wrongness signal, never throttled below the operator's view);
    repeat calls are no-ops (the latch is already set). Every subsequent
    :func:`enum_survivors_via_kernel` call short-circuits to the Python
    oracle, so a wrong kernel can poison at most the ONE mismatching call —
    and even that call ships the ORACLE path (the caller's contract).
    """
    if _KERNEL_DISABLED_REASON[0] is None:
        _KERNEL_DISABLED_REASON[0] = reason
        _LOG.error("sweep_rust_enum.kernel_autoreverted", reason=reason, **context)


def reset_self_check_for_tests(*, remaining: int = SELF_CHECK_CALLS) -> None:
    """Reset the burn-in budget + autorevert latch (test isolation ONLY).

    ``remaining=0`` puts the process in the post-burn-in steady state; the
    default restores a fresh-process view. Never called from production code.
    """
    _SELF_CHECK_REMAINING[0] = remaining
    _KERNEL_DISABLED_REASON[0] = None


def reset_observability_memo_for_tests() -> None:
    """Clear the warn-once / engaged-once memo (test isolation ONLY).

    The memo throttles LOG VOLUME across a whole process; tests capturing
    structlog output reset it so each test observes first-occurrence events.
    Never called from production code paths.
    """
    _OBSERVABILITY_MEMO.clear()


def warn_fallback_once(reason: str, **context: object) -> None:
    """Log ``sweep_rust_enum.fallback`` ONCE per process per *reason*.

    A sticky fallback reason (a stale wheel, a persistent ceiling) would
    otherwise log once per TUPLE across a multi-thousand-tuple grid. The
    first occurrence is loud (WARNING); repeats of the same reason fall back
    silently. The fallback itself is bit-exact regardless (the untouched
    Python loop serves the tuple).
    """
    if reason in _OBSERVABILITY_MEMO:
        return
    _OBSERVABILITY_MEMO.add(reason)
    _LOG.warning("sweep_rust_enum.fallback", reason=reason, **context)


def _resolve_kernel() -> Callable[[list[int], int, list[tuple[int, int]], int], object] | None:
    """Return ``fno_math.sweep_enum_conflict_free``, or ``None`` with a warn-once.

    Lazy single-import-site convention (the rust_metrics ``_resolve_kernel``
    template): a wheel-ABSENT environment degrades to the Python loop with a
    warn-once ``wheel_missing``; a STALE wheel that imports but predates the
    slice-1 symbol degrades with ``wheel_stale`` (FM-8) — the sweep never
    fails on wheel state.
    """
    try:
        import fno_math
    except ImportError:
        warn_fallback_once("wheel_missing")
        return None
    kernel: Callable[[list[int], int, list[tuple[int, int]], int], object] | None = getattr(
        fno_math, "sweep_enum_conflict_free", None
    )
    if kernel is None:
        warn_fallback_once("wheel_stale")
        return None
    return kernel


def _validated_unique_bits(bits: Sequence[int], k: int) -> tuple[int, ...] | None:
    """Normalise + pre-validate with the SAME law as ``iter_masks_k_of_bits``.

    Returns the sorted-unique positions, or ``None`` — silently, with NO
    raise and NO warn — on ANY violation (a non-int entry, a position outside
    ``[0, 128)``, ``k`` outside ``[0, n_unique]``): the untouched Python
    generator then raises its canonical error at first ``next()``, so error
    behaviour is flag-invariant by construction (FM-13 —
    ``combo_mask.py`` / ``conflicts.py`` are never edited).
    """
    seen: set[int] = set()
    # Widened view for the runtime type belt below: the isinstance check
    # guards OUT-OF-CONTRACT callers (a float bit), which the declared
    # ``Sequence[int]`` type makes statically unreachable — the widening
    # keeps the belt visible to mypy --strict without a dishonest ignore.
    raw_bits: Sequence[object] = bits
    for pos in raw_bits:
        # bool is an int subclass and shifts like one — accepted, exactly as
        # the Python generator accepts it; any OTHER non-int (a float bit)
        # routes to the generator's own canonical failure.
        if not isinstance(pos, int):
            return None
        seen.add(pos)
    unique = tuple(sorted(seen))
    for pos in unique:
        if pos < 0 or pos >= MAX_VOCAB_BITS:
            return None
    if k < 0 or k > len(unique):
        return None
    return unique


def _valid_enum_result(
    result: object, count: int, k: int, allowed_mask: int
) -> TypeGuard[tuple[list[int], int, int]]:
    """Cheap domain validation of a kernel result — the refuse-poison gate.

    ``O(1)`` (spot-checks only the FIRST and LAST survivor — never an
    ``O(survivors)`` scan, which would erase the win); catches a malformed
    return (wrong arity/types), a counter divergence (``enumerated`` must
    equal the Python-recomputed ``count_combos`` — FM-6, the sidecar/audit
    byte-drift class), a tally inconsistency, and a first/last survivor with
    the wrong popcount, a bit outside the allowed mask, or a conflict pair. A
    result inside this domain that is nonetheless WRONG is the burn-in +
    property/golden suites' layer to catch (module docstring).
    """
    if not isinstance(result, tuple) or len(result) != _RESULT_ARITY:
        return False
    survivors, enumerated, conflict_pruned = result
    if not isinstance(survivors, list):
        return False
    if not isinstance(enumerated, int) or isinstance(enumerated, bool):
        return False
    if not isinstance(conflict_pruned, int) or isinstance(conflict_pruned, bool):
        return False
    if enumerated != count or conflict_pruned < 0:
        return False
    if conflict_pruned != enumerated - len(survivors):
        return False
    spot_checks = (survivors[0], survivors[-1]) if survivors else ()
    for mask in spot_checks:
        if not isinstance(mask, int) or isinstance(mask, bool):
            return False
        if mask.bit_count() != k or mask & ~allowed_mask:
            return False
        if has_conflict(mask):
            return False
    return True


def _burn_in_passes(bits: Sequence[int], k: int, survivors: list[int], enumerated: int) -> bool:
    """Burn-in compare of kernel survivors against the LIVE Python oracle.

    Small tuples (``enumerated <= FULL_CHECK_ENUMERATED``) compare the FULL
    lists (catches tail divergence AND a dropped/extra tail); large tuples
    compare the first :data:`PREFIX_CHECK_SURVIVORS` elements of both sides
    (the head-region order law — a short kernel list inside the window still
    mismatches by length). Plain list ``==`` asserts SET ∧ ORDER at once. On
    a pass the burn-in budget is consumed; on a mismatch the kernel is
    latched OFF (:func:`disable_kernel`) and the caller ships the oracle
    path.
    """
    oracle = (m for m in iter_masks_k_of_bits(bits, k) if not has_conflict(m))
    if enumerated <= FULL_CHECK_ENUMERATED:
        matched = survivors == list(oracle)
    else:
        matched = survivors[:PREFIX_CHECK_SURVIVORS] == list(islice(oracle, PREFIX_CHECK_SURVIVORS))
    if not matched:
        disable_kernel("self_check_mismatch", k=k, enumerated=enumerated)
        return False
    consume_self_check_sample()
    return True


def enum_survivors_via_kernel(bits: Sequence[int], k: int) -> RustEnumResult | None:
    """Run the fused Rust enum+prune kernel; ``None`` ⇒ the Python loop serves.

    The ONE FFI call site. Returns the loop-equivalent
    :class:`RustEnumResult` on success. Returns ``None`` — after a warn-once
    (or, for a refused result / burn-in mismatch, an ERROR) — when the flag
    is off, the kernel is unavailable or latched off, the input fails the
    Python-side pre-validation (silently — the untouched generator owns the
    canonical error), the count exceeds :data:`ENUM_KERNEL_MAX_MASKS`, the
    FFI raises a boundary/conversion error, the result fails the domain
    validation, or the burn-in compare mismatches; the caller then runs the
    verbatim Python loop, so wheel state can never change results or error
    behaviour. The fallback catch is NARROW (``ValueError`` — the kernel's
    own boundary arms; ``TypeError`` / ``OverflowError`` — PyO3
    argument-conversion failures): a truly unexpected exception type
    PROPAGATES (CLAUDE.md §4 — no broad excepts), and ``MemoryError``
    re-raises BARE (OOM is a GLOBAL stop, DEC-LAKE-WRITE-HALT-001 — never
    swallowed into a fallback).
    """
    if not sweep_rust_enum_enabled():
        return None
    if kernel_disabled():
        warn_fallback_once("kernel_disabled")
        return None
    kernel = _resolve_kernel()
    if kernel is None:
        return None
    unique = _validated_unique_bits(bits, k)
    if unique is None:
        return None
    count = count_combos(k, len(unique))
    if count > ENUM_KERNEL_MAX_MASKS:
        warn_fallback_once("count_ceiling", count=count, ceiling=ENUM_KERNEL_MAX_MASKS)
        return None
    try:
        result = kernel(list(unique), k, list(_CONFLICT_PAIRS_SORTED), ENUM_KERNEL_MAX_MASKS)
    except (ValueError, TypeError, OverflowError) as exc:
        # The kernel's boundary ValueError arms + PyO3 argument-conversion
        # TypeError/OverflowError degrade to the Python loop; any OTHER
        # exception type (incl. MemoryError — a GLOBAL stop) propagates.
        warn_fallback_once("kernel_error", error=repr(exc))
        return None
    allowed_mask = 0
    for pos in unique:
        allowed_mask |= 1 << pos
    if not _valid_enum_result(result, count, k, allowed_mask):
        # A domain-invalid result is a kernel BUG: refuse it LOUDLY (never
        # silently adopted) and serve the tuple from the Python loop.
        if "kernel_result_refused" not in _OBSERVABILITY_MEMO:
            _OBSERVABILITY_MEMO.add("kernel_result_refused")
            _LOG.error("sweep_rust_enum.kernel_result_refused", result=repr(result)[:512], k=k)
        return None
    survivors, enumerated, conflict_pruned = result
    if self_check_pending() and not _burn_in_passes(bits, k, survivors, enumerated):
        return None
    if _ENGAGED_KEY not in _OBSERVABILITY_MEMO:
        _OBSERVABILITY_MEMO.add(_ENGAGED_KEY)
        _LOG.info("sweep_rust_enum.engaged", enumerated=enumerated, k=k)
    return RustEnumResult(
        survivors=survivors, enumerated=enumerated, conflict_pruned=conflict_pruned
    )


__all__ = [
    "ENUM_KERNEL_MAX_MASKS",
    "FULL_CHECK_ENUMERATED",
    "PREFIX_CHECK_SURVIVORS",
    "SELF_CHECK_CALLS",
    "SWEEP_RUST_ENUM_DEFAULT",
    "SWEEP_RUST_ENUM_ENV",
    "RustEnumResult",
    "consume_self_check_sample",
    "disable_kernel",
    "enum_survivors_via_kernel",
    "kernel_disabled",
    "reset_observability_memo_for_tests",
    "reset_self_check_for_tests",
    "self_check_pending",
    "sweep_rust_enum_enabled",
    "warn_fallback_once",
]
