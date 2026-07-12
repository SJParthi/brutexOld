"""Flag-gated Rust sweep-enumeration seam — the ranged ``enumerate_prefilter`` binding.

DEC-SWEEP-ENUM-RUST-001 (MIGRATION.md #1738; umbrella
DEC-RUST-FIRST-MANDATE-001) — Slice 1 of the Rust sweep-core track, reworked
to the design-of-record ``docs/design/sweep_core/DESIGN_FINAL_RUST_SWEEP_CORE.md``
(branch ``claude/rust-sweep-core`` @ a3c61e9b) §1.3/§1.4/§3/§5/§6. The sweep's
mask-source segment (:func:`brutex.engine.combo_mask.iter_masks_k_of_bits`
FUSED with the per-mask :func:`brutex.vocabulary.has_conflict` prune AND the
Stage-1B min-hits count) is served by the resident
``fno_math.ResidentSweepEnum`` / ``ResidentSweepEnumU128`` kernel substrate in
RANGES when ``BRUTEX_SWEEP_RUST_ENUM`` is enabled. This module is the Python
side: env-flag resolution, width selection, the ranged driver loop, the
work-cap knob, the per-range domain validation (incl. the in-band
``hits_counted`` MIN-HITS SHORT-CIRCUIT pin), the width-keyed mask-count-
floored enum-shadow burn-in with per-width autorevert latches + the
MID-TUPLE CONTINUATION contract, the TOTAL-RECONCILIATION belt, and the
LOUD-but-flood-safe fallback logging. The ONE consumption seam lives in
:mod:`brutex.engine.sweep_run` (digest-path only — the legacy no-digest dedup
arm and the Apriori arm stay pure Python by contract).

## The flag — ``BRUTEX_SWEEP_RUST_ENUM`` over ``SWEEP_RUST_ENUM_DEFAULT``

:func:`sweep_rust_enum_enabled` reads the env per call, exactly like the
sibling :func:`brutex.engine.rust_metrics.sweep_rust_metrics_enabled` (whose
``_TRUTHY`` / ``_FALSY`` literal sets are consumed through the PUBLIC
:func:`~brutex.engine.sweep_lookahead.env_flag_tristate` classifier, not
copied — CLAUDE.md §11 COMMON). Resolution: a ``_TRUTHY`` value forces ON, a
``_FALSY`` value forces OFF, unset/whitespace-empty falls to
:data:`SWEEP_RUST_ENUM_DEFAULT` (**False — ships inert**; the default-ON flip
is a SEPARATE later ADR, the rust_metrics/ADR-0017 precedent), and any OTHER
value fails SAFE to OFF with one warn-once ``flag_unrecognised`` event.

## The order law (Golden Rule #4 — why bit-identity is load-bearing)

The kernel returns kept masks in the EXACT ``itertools.combinations``
lexicographic order of the Python generator (NOT numeric-ascending mask
order — the two differ from n = 4; the kernel's CNS unranking is the
LEX-RANK variant for exactly this reason). Enumeration order feeds the
sweep's chunk boundaries, ``SweepComboError`` attribution, stable-sort
tie-breaks and the store append order, so a set-equal but order-drifted list
would produce DIFFERENT durable bytes.

## Fused Stage-1B min-hits + the SHORT-CIRCUIT LAW (design §1.3, round-6)

``kept = conflict-free AND (min_hits == 0 OR supermask_hits >= min_hits)``
with the STRICT ``>=`` boundary, counted IN-KERNEL on the resident
transposed-bitplane substrate (the same ``(bar & mask) == mask`` predicate as
:func:`~brutex.engine.combo_mask.count_supermasks` — the retained parity
oracle). At ``min_hits == 0`` the per-mask O(n_bars) count MUST NOT run —
exactly today's spine (``if cfg.min_hits > 0`` guards the whole Stage-1B
count). The law is BYTE-OBSERVABLE in-band: every range's ``hits_counted``
must read 0 at ``min_hits == 0`` and ``enumerated − conflict_pruned`` at
``min_hits > 0`` — :func:`_valid_range_result` REFUSES any other value (the
design's mandated Python seam pin; a guard-deleting kernel mutant is
data-identical but flips ``hits_counted`` and dies here).

## Ranged consumption + work caps (design §3)

Per call the driver passes ``max_enumerated = clamp(cells // max(1, n_bars),
4096, 2^20)`` (the DIVISOR-floored repo idiom) and ``max_work_cells = cells``
(default 2^28; env ``BRUTEX_SWEEP_CORE_ENUM_CELLS``, invalid → default + one
loud warn — the #1608 knob precedent — and clamped into [2^16, 2^30] with one
``work_cap_clamped`` warn per direction). The kernel checks caps AFTER each
mask's work (guaranteed ≥1 mask progress; a non-exhausted zero-progress
return is the ``enum_no_progress`` driver belt). The loop terminates on the
IN-BAND ``exhausted`` flag (``next_rank == total``), never on heuristics, and
the exit asserts the TOTAL-RECONCILIATION belt — final rank AND Σ enumerated
must equal the driver's own :func:`~brutex.engine.combo_mask.count_combos`
total (mismatch = the ``enum_total_mismatch`` fallback; today's spine serves
the whole tuple).

## Enum-shadow burn-in — width-keyed, MASK-COUNT-floored (design §5)

The first ranges of each process are shadowed against the live Python oracle
(``islice`` over ``iter_masks_k_of_bits`` + ``has_conflict`` + the injected
``count_hits`` Stage-1B filter — per-range BLOCK-SCOPED counts, the design's
streaming-residual pin (b)) until :data:`SWEEP_ENUM_SELF_CHECK_MASKS` = 2^16
core-enumerated masks have been shadowed **per process PER KERNEL WIDTH**
(u64 / u128 are separately monomorphised code), hard-capped at
:data:`SWEEP_ENUM_SELF_CHECK_RANGES_CAP` = 256 ranges — a stream that cannot
reach the mask floor within the cap warns once
(``enum_shadow_floor_unmet``, a genuine degenerate-stream signal). A
mismatch ships the PYTHON window lists, latches the kernel OFF for the
process (per width; ``sweep_rust_enum.kernel_autoreverted`` ERROR), and the
REMAINDER of the tuple's rank space is served by CONTINUING today's Python
enumeration from the same generator to exhaustion (the design's MID-TUPLE
CONTINUATION contract — the total-reconciliation is defined over the mixed
provenance, exact because both enumerations share the lex order). The
shadow/latch cells are the same *documented exception* to the
no-global-mutable-state rule as the observability memo;
:func:`reset_enum_self_check_for_tests` restores a fresh process view.

## Fallback + engagement observability (Golden Rule #10)

Every fallback reason logs ``sweep_rust_enum.fallback`` (WARNING) ONCE per
process per reason — the design's engagement-condition vocabulary as
applicable to the ENUM arm (arm-condition partition): ``legacy_dedup_path``,
``flag_unrecognised``, ``wheel_missing``, ``wheel_stale``,
``kernel_disabled``, ``kernel_error``, ``bars_length_mismatch``,
``count_overflow``, ``enum_no_progress``, ``enum_total_mismatch``,
``enum_cells_invalid``, ``work_cap_clamped`` (advisory — not a fallback),
``enum_shadow_floor_unmet`` (advisory). A kernel range that fails the domain
validation is REFUSED loudly (``sweep_rust_enum.kernel_result_refused``,
ERROR — never adopted silently). The first fully-kernel-served tuple logs
``sweep_rust_enum.engaged`` (INFO) — the arm discriminator the golden suite
asserts positively AND negatively (the anti-vacuous-gate law).

## Honest envelope (Golden Rule #10)

* Constant-factor win only: the segment stays ``O(C(n_unique, k) ·
  (k + n_pairs))`` enumeration work plus, iff ``min_hits > 0``, the fused
  count's ``O(conflict_free · k · n_bars/64)`` plane words — no O-class
  change, no GIL release (the kernel runs serial, GIL-held; the work cells
  cap bounds each hold), and NO whole-sweep speedup claim.
* Substrate cost: constructing the resident core marshals ``bar_bits`` and
  pays a one-off O(n_bars · set_bits) transpose PER TUPLE — including on
  ``min_hits == 0`` tuples where the planes go unread (the design's
  one-core-per-tuple shape; stated, not hidden).
* The shadow window is bounded to ~2^16 shadowed Python enumerations per
  process per engaged width; the O(1) per-range domain gate cannot catch a
  tail-region order/set divergence after the window closes — that layer is
  owned by the property differential + golden + in-crate proptest + mutation
  gates, plus the ship-time twin-run grids (the design's stated residual:
  in-range kept-set divergence with self-consistent counts, drops OR
  spurious keeps).
* Invalid input never raises HERE: the pre-validation returns ``None``
  silently and the untouched Python generator raises the canonical
  ``ValueError`` at first ``next()`` — error behaviour is flag-invariant by
  construction (``combo_mask.py`` / ``conflicts.py`` are never edited).
* ``MemoryError`` from the FFI (the kernel's ``try_reserve`` failure)
  re-raises BARE — OOM is a GLOBAL stop (DEC-LAKE-WRITE-HALT-001), never
  swallowed into a fallback.
* An env flip MID-run takes effect on the next tuple (per-call read);
  results are bit-identical either way.
* The design's stall-budget guard is DELIBERATELY absent from Slice 1 (it is
  not in the §6 Slice-1 deliverable list; the enum arm's only overhead term —
  the once-per-process shadow — is structurally below every real deadline).
  It lands with the fold arm.
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
    from collections.abc import Callable, Iterator, Sequence

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

#: The enum work-cells knob (design §3): the per-call ``max_work_cells``
#: budget (enumerated × n_bars, the GIL/liveness bound) AND the numerator of
#: the ``max_enumerated`` range sizing. Default 2^28 ≈ 70 ms at the design
#: pack's carried 0.257 ns/bar·mask batch-FFI ``count_supermasks`` rate (the
#: design's re-scaled cap) — this branch's MEASURED fused rate is lower
#: (0.077–0.125 ns, combo_counts.md §3.2), so the real GIL hold is shorter.
ENUM_WORK_CELLS_ENV: Final[str] = "BRUTEX_SWEEP_CORE_ENUM_CELLS"

#: Default ``max_work_cells`` per kernel call (design §3 — 2^28).
DEFAULT_ENUM_WORK_CELLS: Final[int] = 1 << 28

#: Env-override clamps (design §3, rounds 2–3): an override above 2^30
#: (~0.28 s GIL hold) clamps DOWN, one below 2^16 clamps UP — each with one
#: loud ``work_cap_clamped`` warn naming the direction, so a typo'd ``2**44``
#: can never hold the GIL for minutes and a degenerate ``=1`` can never
#: produce a pathological one-mask-per-call stream.
ENUM_WORK_CELLS_UPPER_CLAMP: Final[int] = 1 << 30
ENUM_WORK_CELLS_LOWER_CLAMP: Final[int] = 1 << 16

#: ``max_enumerated`` range-sizing clamps (design §3): ``clamp(cells //
#: max(1, n_bars), 4096, 2^20)`` — the DIVISOR-floored repo idiom.
RANGE_MASKS_FLOOR: Final[int] = 4096
RANGE_MASKS_CAP: Final[int] = 1 << 20

#: Enum-shadow burn-in MASK-COUNT floor per process per kernel width
#: (design §5, external-fold item 4): the window stays open until this many
#: core-enumerated masks have been shadowed against the Python oracle.
SWEEP_ENUM_SELF_CHECK_MASKS: Final[int] = 1 << 16

#: Enum-shadow hard cap in RANGES (design §5, audit C51 — 256 ≥ the ~148
#: ranges the heaviest production yield needs to reach the mask floor, with
#: ~1.7× headroom; a stream that cannot reach the floor within the cap warns
#: ``enum_shadow_floor_unmet`` once — a genuine degenerate-stream signal).
SWEEP_ENUM_SELF_CHECK_RANGES_CAP: Final[int] = 256

#: The two kernel widths — separately monomorphised Rust code, separately
#: burned in + latched (design §5 round-4/round-5 width keying).
_WIDTHS: Final[tuple[str, str]] = ("u64", "u128")

#: Width-selection boundary: any validated condition bit at/above this index
#: routes the tuple to the u128 monomorphisation (bit-index-keyed law).
_U64_WIDTH_BITS: Final[int] = 64

#: The u64 rank-domain ceiling: a tuple whose C(n_unique, k) exceeds this
#: cannot be addressed by the kernel's u64 ranks (production k ≤ 6 over
#: n ≤ 60 tops out at C(60, 6) ≈ 5.0e7 — this is a direct-caller belt).
_U64_MAX: Final[int] = (1 << 64) - 1

#: The conflict-pair table crossing the FFI AS DATA, in deterministic sorted
#: order (``CONFLICT_PAIRS`` is a frozenset — sorting keeps the call
#: byte-reproducible; the SET semantics make order irrelevant to the result).
#: NEVER re-minted in Rust; imported from the ONE vocabulary home.
_CONFLICT_PAIRS_SORTED: Final[tuple[tuple[int, int], ...]] = tuple(sorted(CONFLICT_PAIRS))

#: Once-per-process-per-reason fallback memo + the engaged-once latch.
#: LOG-VOLUME state ONLY (module docstring): results never depend on it.
_OBSERVABILITY_MEMO: Final[set[str]] = set()

#: Memo key for the engaged-once INFO (namespaced apart from warn reasons).
_ENGAGED_KEY: Final[str] = "__engaged__"

#: Arity of the kernel range sextuple ``(enumerated, conflict_pruned,
#: next_rank, exhausted, hits_counted, kept)``.
_RESULT_ARITY: Final[int] = 6

#: Per-width enum-shadow window state: masks still owed to the floor +
#: ranges left under the hard cap (single-cell mutables — the documented
#: no-global-mutable-state exception, module docstring).
_SHADOW_MASKS_REMAINING: Final[dict[str, int]] = dict.fromkeys(_WIDTHS, SWEEP_ENUM_SELF_CHECK_MASKS)
_SHADOW_RANGES_REMAINING: Final[dict[str, int]] = dict.fromkeys(
    _WIDTHS, SWEEP_ENUM_SELF_CHECK_RANGES_CAP
)

#: Per-width autorevert latch: the mismatch reason once the kernel is
#: disabled for this process, else ``None`` (same documented exception).
_KERNEL_DISABLED_REASON: Final[dict[str, str | None]] = dict.fromkeys(_WIDTHS, None)


@dataclass(frozen=True, slots=True)
class RustEnumResult:
    """A kernel-served enumeration: the fused loop+Stage-1B-equivalent bundle.

    ``kept`` is the post-min-hits mask list in ``itertools.combinations``
    lexicographic order — exactly what the sweep loop's ``kept`` holds after
    today's Stage-1B filter on the digest path; ``enumerated`` /
    ``conflict_pruned`` are the honest tallies the loop would have counted;
    ``hits_counted`` is the in-band short-circuit observable (0 at
    ``min_hits == 0``); :attr:`evaluated` derives the pre-min-hits survivor
    count the seam's counters bucket on (``evaluated − len(kept)`` is the
    ``n_minhits_dropped`` sidecar/audit tally — never re-bucketed).
    """

    kept: list[int]
    enumerated: int
    conflict_pruned: int
    hits_counted: int

    @property
    def evaluated(self) -> int:
        """The pre-min-hits survivor count (``enumerated − conflict_pruned``)."""
        return self.enumerated - self.conflict_pruned


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


def enum_shadow_pending(width: str) -> bool:
    """``True`` while *width*'s shadow window is still open this process.

    Open = the 2^16 mask floor unmet AND the 256-range hard cap unconsumed
    (design §5: the floor is the coverage guarantee, the cap bounds a
    degenerate tiny-range stream).
    """
    return _SHADOW_MASKS_REMAINING[width] > 0 and _SHADOW_RANGES_REMAINING[width] > 0


def kernel_disabled(width: str) -> bool:
    """``True`` iff the autorevert latch has disabled *width* this process."""
    return _KERNEL_DISABLED_REASON[width] is not None


def disable_kernel(width: str, reason: str, **context: object) -> None:
    """Latch *width* OFF for the remainder of the process (the autorevert).

    Called by the enum-shadow burn-in on a range MISMATCH — a kernel
    order/set/counter bug the O(1) domain validation cannot see. The first
    call per width logs ``sweep_rust_enum.kernel_autoreverted`` (ERROR — a
    genuine kernel-wrongness signal, never throttled below the operator's
    view); repeat calls are no-ops. Every subsequent
    :func:`enum_prefilter_via_kernel` call on that width short-circuits to
    the Python oracle with a warn-once ``kernel_disabled`` — and even the
    mismatching tuple ships ORACLE values (the mid-tuple continuation). The
    latch is PER WIDTH (design §5 round-5): a u64 defect must never silently
    certify — or block — the u128 kernel and vice versa.
    """
    if _KERNEL_DISABLED_REASON[width] is None:
        _KERNEL_DISABLED_REASON[width] = reason
        _LOG.error("sweep_rust_enum.kernel_autoreverted", width=width, reason=reason, **context)


def reset_enum_self_check_for_tests(
    *,
    width: str | None = None,
    masks_remaining: int = SWEEP_ENUM_SELF_CHECK_MASKS,
    ranges_cap_remaining: int = SWEEP_ENUM_SELF_CHECK_RANGES_CAP,
) -> None:
    """Reset the shadow window + autorevert latch (test isolation ONLY).

    The design's full state-setter signature (§5 external-fold item 4):
    ``width=None`` resets BOTH widths; ``masks_remaining=0`` puts a width in
    the post-burn-in closed state the poison-past-burn-in goldens need; a
    small ``ranges_cap_remaining`` with the mask floor unmet is the cap-trip
    pin's state. Never called from production code.
    """
    for w in _WIDTHS if width is None else (width,):
        _SHADOW_MASKS_REMAINING[w] = masks_remaining
        _SHADOW_RANGES_REMAINING[w] = ranges_cap_remaining
        _KERNEL_DISABLED_REASON[w] = None


def reset_observability_memo_for_tests() -> None:
    """Clear the warn-once / engaged-once memo (test isolation ONLY).

    The memo throttles LOG VOLUME across a whole process; tests capturing
    structlog output reset it so each test observes first-occurrence events.
    Never called from production code paths.
    """
    _OBSERVABILITY_MEMO.clear()


def warn_fallback_once(reason: str, **context: object) -> None:
    """Log ``sweep_rust_enum.fallback`` ONCE per process per *reason*.

    A sticky fallback reason (a stale wheel, a persistent legacy path) would
    otherwise log once per TUPLE across a multi-thousand-tuple grid. The
    first occurrence is loud (WARNING); repeats of the same reason fall back
    silently. The fallback itself is bit-exact regardless (the untouched
    Python loop serves the tuple).
    """
    if reason in _OBSERVABILITY_MEMO:
        return
    _OBSERVABILITY_MEMO.add(reason)
    _LOG.warning("sweep_rust_enum.fallback", reason=reason, **context)


def _warn_advisory_once(key: str, event: str, **context: object) -> None:
    """Log an ADVISORY warning once per process per *key* (not a fallback).

    Used by the knob clamps + the shadow floor-unmet signal — conditions that
    do NOT change which arm serves the tuple, only how it is configured or
    how much burn-in coverage it got.
    """
    if key in _OBSERVABILITY_MEMO:
        return
    _OBSERVABILITY_MEMO.add(key)
    _LOG.warning(event, **context)


def _resolve_enum_work_cells() -> int:
    """Resolve the per-call work-cells budget from the env knob, clamped.

    Unset/blank → :data:`DEFAULT_ENUM_WORK_CELLS`. A non-integer value warns
    once (``enum_cells_invalid``) and serves the default — the #1608
    invalid-numeric-knob precedent (loud, never a mid-grid crash). A valid
    value is clamped into ``[2^16, 2^30]`` with one loud ``work_cap_clamped``
    warn per direction (design §3 rounds 2–3: the upper clamp bounds a typo'd
    GIL hold, the lower clamp bounds a degenerate one-mask-per-call stream).
    """
    raw = os.environ.get(ENUM_WORK_CELLS_ENV, "").strip()
    if not raw:
        return DEFAULT_ENUM_WORK_CELLS
    try:
        value = int(raw)
    except ValueError:
        _warn_advisory_once(
            f"enum_cells_invalid:{raw}",
            "sweep_rust_enum.enum_cells_invalid",
            value=raw,
            default=DEFAULT_ENUM_WORK_CELLS,
        )
        return DEFAULT_ENUM_WORK_CELLS
    if value > ENUM_WORK_CELLS_UPPER_CLAMP:
        _warn_advisory_once(
            "work_cap_clamped:high",
            "sweep_rust_enum.work_cap_clamped",
            direction="high",
            value=value,
            clamped=ENUM_WORK_CELLS_UPPER_CLAMP,
        )
        return ENUM_WORK_CELLS_UPPER_CLAMP
    if value < ENUM_WORK_CELLS_LOWER_CLAMP:
        _warn_advisory_once(
            "work_cap_clamped:low",
            "sweep_rust_enum.work_cap_clamped",
            direction="low",
            value=value,
            clamped=ENUM_WORK_CELLS_LOWER_CLAMP,
        )
        return ENUM_WORK_CELLS_LOWER_CLAMP
    return value


def _resolve_kernel_class(width: str) -> type | None:
    """Return the resident kernel class for *width*, or ``None`` + warn-once.

    Lazy single-import-site convention (the rust_metrics ``_resolve_kernel``
    template): a wheel-ABSENT environment degrades to the Python loop with a
    warn-once ``wheel_missing``; a STALE wheel that imports but predates the
    slice-1 classes degrades with ``wheel_stale`` — the sweep never fails on
    wheel state. The class is resolved through ``getattr`` per call, so the
    goldens' poison seam (monkeypatching the ``fno_math`` attribute) is the
    documented late-bound test seam.
    """
    try:
        import fno_math
    except ImportError:
        warn_fallback_once("wheel_missing")
        return None
    name = "ResidentSweepEnum" if width == "u64" else "ResidentSweepEnumU128"
    cls: type | None = getattr(fno_math, name, None)
    if cls is None:
        warn_fallback_once("wheel_stale", missing=name)
        return None
    return cls


def _validated_unique_bits(bits: Sequence[int], k: int) -> tuple[int, ...] | None:
    """Normalise + pre-validate with the SAME law as ``iter_masks_k_of_bits``.

    Returns the sorted-unique positions, or ``None`` — silently, with NO
    raise and NO warn — on ANY violation (a non-int entry, a position outside
    ``[0, 128)``, ``k`` outside ``[0, n_unique]``): the untouched Python
    generator then raises its canonical error at first ``next()``, so error
    behaviour is flag-invariant by construction
    (``combo_mask.py`` / ``conflicts.py`` are never edited).
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


def _valid_range_result(
    result: object,
    *,
    start_rank: int,
    count: int,
    min_hits: int,
    k: int,
    allowed_mask: int,
) -> TypeGuard[tuple[int, int, int, bool, int, list[int]]]:
    """Cheap domain validation of one kernel RANGE — the refuse-poison gate.

    ``O(1)`` (spot-checks only the FIRST and LAST kept mask — never an
    ``O(kept)`` scan, which would erase the win); enforces, from the return
    tuple's own fields: shape/types; the range-position law ``enumerated ==
    next_rank − start_rank`` with ``next_rank <= count``; tally consistency
    (``0 <= conflict_pruned <= enumerated``; ``len(kept) <= enumerated −
    conflict_pruned``, with EQUALITY at ``min_hits == 0`` — every
    conflict-free mask is kept there); the MIN-HITS SHORT-CIRCUIT law on the
    in-band ``hits_counted`` (``== 0`` at ``min_hits == 0``; ``==
    enumerated − conflict_pruned`` at ``min_hits > 0`` — the design's
    mandated seam pin); and first/last kept-mask sanity (popcount ``k``,
    inside the allowed bit mask, conflict-free). The ``exhausted`` flag is
    deliberately NOT cross-checked here — the design's §1.4 driver asserts
    the TOTAL-RECONCILIATION belt on loop exit (final ``rank == count`` and
    ``Σ enumerated == count``), so a kernel lying "exhausted" early is
    caught by that belt (``enum_total_mismatch``) and a kernel lying
    "not exhausted" at the end is caught by the no-progress belt. A range
    inside this domain that is nonetheless WRONG is the shadow burn-in +
    property/golden suites' layer to catch (module docstring).
    """
    if not isinstance(result, tuple) or len(result) != _RESULT_ARITY:
        return False
    enumerated, conflict_pruned, next_rank, exhausted, hits_counted, kept = result
    for value in (enumerated, conflict_pruned, next_rank, hits_counted):
        if not isinstance(value, int) or isinstance(value, bool):
            return False
    if not isinstance(exhausted, bool) or not isinstance(kept, list):
        return False
    if next_rank > count or enumerated != next_rank - start_rank:
        return False
    if conflict_pruned < 0 or conflict_pruned > enumerated:
        return False
    conflict_free = enumerated - conflict_pruned
    if min_hits == 0:
        # Short-circuit law arm 1: zero counts ran; kept == conflict-free.
        if hits_counted != 0 or len(kept) != conflict_free:
            return False
    # Short-circuit law arm 2: the count ran on EVERY conflict-free mask.
    elif hits_counted != conflict_free or len(kept) > conflict_free:
        return False
    spot_checks = (kept[0], kept[-1]) if kept else ()
    for mask in spot_checks:
        if not isinstance(mask, int) or isinstance(mask, bool):
            return False
        if mask.bit_count() != k or mask & ~allowed_mask:
            return False
        if has_conflict(mask):
            return False
    return True


def _python_window(
    oracle_gen: Iterator[int],
    window_len: int,
    min_hits: int,
    count_hits: Callable[[Sequence[int]], list[int]],
) -> tuple[int, int, list[int]]:
    """One shadow window on the live Python oracle: ``(enumerated, pruned, kept)``.

    Consumes exactly ``window_len`` masks from the shared generator (fewer if
    it exhausts — a length mismatch then fails the compare, the safe
    direction), applies the production ``has_conflict`` prune, and the
    BLOCK-SCOPED Stage-1B filter through the injected ``count_hits`` (the
    seam's ONE shared counter — per-mask independent, so per-window filtering
    composes to the identical whole-tuple kept list; the design's
    streaming-residual pin (b) mandates block scoping).
    """
    window = list(islice(oracle_gen, window_len))
    conflict_free = [m for m in window if not has_conflict(m)]
    pruned = len(window) - len(conflict_free)
    if min_hits > 0 and conflict_free:
        hits = count_hits(conflict_free)
        kept = [m for m, h in zip(conflict_free, hits, strict=True) if h >= min_hits]
    else:
        kept = conflict_free
    return len(window), pruned, kept


def _python_continuation(
    oracle_gen: Iterator[int],
    min_hits: int,
    count_hits: Callable[[Sequence[int]], list[int]],
) -> tuple[int, int, list[int]]:
    """Serve the REMAINDER of the rank space on today's Python enumeration.

    The design's MID-TUPLE CONTINUATION contract (§5 round-6): after a shadow
    mismatch the same generator — positioned exactly after the shipped Python
    window — runs to exhaustion with the production conflict prune + the
    batch Stage-1B filter (bit-identical to per-window filtering: the count
    is per-mask independent).
    """
    enumerated = 0
    pruned = 0
    survivors: list[int] = []
    for mask in oracle_gen:
        enumerated += 1
        if has_conflict(mask):
            pruned += 1
        else:
            survivors.append(mask)
    if min_hits > 0 and survivors:
        hits = count_hits(survivors)
        kept = [m for m, h in zip(survivors, hits, strict=True) if h >= min_hits]
    else:
        kept = survivors
    return enumerated, pruned, kept


def _consume_shadow(width: str, masks: int) -> None:
    """Book one PASSED shadow range against *width*'s window counters.

    Decrements the mask floor by the range's enumerated count (clamped at 0)
    and the range cap by one; when the cap trips with the floor unmet, warns
    once per width (``enum_shadow_floor_unmet`` — the degenerate-stream
    signal; the kernel STAYS engaged: every shadowed range was verified).
    """
    _SHADOW_MASKS_REMAINING[width] = max(0, _SHADOW_MASKS_REMAINING[width] - masks)
    _SHADOW_RANGES_REMAINING[width] -= 1
    if _SHADOW_RANGES_REMAINING[width] <= 0 and _SHADOW_MASKS_REMAINING[width] > 0:
        _warn_advisory_once(
            f"enum_shadow_floor_unmet:{width}",
            "sweep_rust_enum.enum_shadow_floor_unmet",
            width=width,
            masks_unshadowed=_SHADOW_MASKS_REMAINING[width],
        )


def enum_prefilter_via_kernel(
    bits: Sequence[int],
    k: int,
    *,
    min_hits: int,
    bar_bits: Sequence[int],
    count_hits: Callable[[Sequence[int]], list[int]],
    digest_path: bool,
) -> RustEnumResult | None:
    """Serve one tuple's enum+prune+Stage-1B via the ranged Rust kernel.

    The ONE FFI consumption site. Returns the fused
    :class:`RustEnumResult` on success (kernel-served, or MIXED provenance
    after a mid-tuple shadow autorevert — both bit-identical to the Python
    spine by construction). Returns ``None`` — after a warn-once (or, for a
    refused range / shadow mismatch, an ERROR) — when the flag is off
    (silent: the deliberate default state), the tuple rides the legacy
    no-digest dedup arm (``legacy_dedup_path`` — the design's
    engagement-condition listing order names the digest-path condition
    FIRST), the input fails the Python-side pre-validation (silently — the
    untouched generator owns the canonical error), the width's kernel is
    latched off or unavailable, the resident core's ``n_bars`` disagrees
    with the window (``bars_length_mismatch`` — the §1.3 F27 seam
    invariant), the total exceeds the u64 rank domain, the FFI raises a
    boundary/conversion error, a range fails the domain validation, the
    guaranteed-progress or total-reconciliation belts trip; the caller then
    runs the verbatim Python loop, so wheel state can never change results
    or error behaviour. ``MemoryError`` re-raises BARE (OOM is a GLOBAL
    stop, DEC-LAKE-WRITE-HALT-001 — never swallowed into a fallback).

    ``min_hits <= 0`` is served as 0 — the exact ``if cfg.min_hits > 0``
    spine law. The Apriori arm never reaches this function (the seam probes
    only on the brute-force arm; apriori WINS — the frozen
    DEC-SWEEP-APRIORI-TUPLE-001 contract).
    """
    if not sweep_rust_enum_enabled():
        return None
    if not digest_path:
        # FALLBACK-REASON PRECEDENCE (design fix-forward item 11): the
        # digest-path condition is listed first — a legacy-arm tuple with the
        # flag ON names `legacy_dedup_path`, never a later condition.
        warn_fallback_once("legacy_dedup_path")
        return None
    unique = _validated_unique_bits(bits, k)
    if unique is None:
        return None
    eff_min_hits = min_hits if min_hits > 0 else 0
    # Width selection is bit-index-keyed + empty-safe (design §5): any
    # validated bit >= 64 routes to the u128 monomorphisation.
    width = "u128" if unique and unique[-1] >= _U64_WIDTH_BITS else "u64"
    if kernel_disabled(width):
        warn_fallback_once("kernel_disabled", width=width)
        return None
    cls = _resolve_kernel_class(width)
    if cls is None:
        return None
    count = count_combos(k, len(unique))
    if count > _U64_MAX:
        warn_fallback_once("count_overflow", count=str(count))
        return None
    n_bars = len(bar_bits)
    cells = _resolve_enum_work_cells()
    max_enumerated = min(max(cells // max(1, n_bars), RANGE_MASKS_FLOOR), RANGE_MASKS_CAP)
    try:
        core = cls(list(bar_bits))
    except MemoryError:
        raise
    except (ValueError, TypeError, OverflowError) as exc:
        # PyO3 conversion failures (a bar value past the width) + the
        # kernel's own construction refusals degrade to the Python loop; any
        # OTHER exception type propagates (CLAUDE.md §4 — no broad excepts).
        warn_fallback_once("kernel_error", error=repr(exc))
        return None
    core_n_bars = core.n_bars()
    if core_n_bars != n_bars:
        # §1.3 F27 seam invariant: a non-empty Python window must never
        # arrive empty/short across the FFI.
        warn_fallback_once("bars_length_mismatch", core=core_n_bars, python=n_bars)
        return None
    allowed_mask = 0
    for pos in unique:
        allowed_mask |= 1 << pos
    ubits = list(unique)
    pairs = list(_CONFLICT_PAIRS_SORTED)
    kept: list[int] = []
    enumerated = 0
    conflict_pruned = 0
    hits_counted = 0
    rank = 0
    oracle_gen: Iterator[int] | None = None
    while True:
        try:
            raw = core.enumerate_prefilter(
                ubits, k, pairs, eff_min_hits, rank, max_enumerated, cells
            )
        except MemoryError:
            raise
        except (ValueError, TypeError, OverflowError) as exc:
            warn_fallback_once("kernel_error", error=repr(exc))
            return None
        if not _valid_range_result(
            raw,
            start_rank=rank,
            count=count,
            min_hits=eff_min_hits,
            k=k,
            allowed_mask=allowed_mask,
        ):
            # A domain-invalid range is a kernel BUG: refuse it LOUDLY (never
            # silently adopted) and serve the WHOLE tuple from the Python loop.
            if "kernel_result_refused" not in _OBSERVABILITY_MEMO:
                _OBSERVABILITY_MEMO.add("kernel_result_refused")
                _LOG.error(
                    "sweep_rust_enum.kernel_result_refused",
                    result=repr(raw)[:512],
                    k=k,
                    start_rank=rank,
                )
            return None
        r_enum, r_pruned, next_rank, exhausted, r_hits, r_kept = raw
        if not exhausted and next_rank == rank:
            # Structurally unreachable under the kernel's guaranteed-progress
            # law — the driver belt the design mandates anyway.
            warn_fallback_once("enum_no_progress", rank=rank)
            return None
        if enum_shadow_pending(width):
            if oracle_gen is None:
                oracle_gen = iter(iter_masks_k_of_bits(bits, k))
            py_enum, py_pruned, py_kept = _python_window(
                oracle_gen, r_enum, eff_min_hits, count_hits
            )
            if (py_enum, py_pruned, py_kept) != (r_enum, r_pruned, r_kept):
                # Shadow MISMATCH: latch the width, ship the PYTHON window,
                # and serve the REMAINDER of the rank space on today's
                # Python enumeration (the MID-TUPLE CONTINUATION contract).
                disable_kernel(
                    width, "self_check_mismatch", k=k, start_rank=rank, enumerated=r_enum
                )
                kept.extend(py_kept)
                enumerated += py_enum
                conflict_pruned += py_pruned
                cont_enum, cont_pruned, cont_kept = _python_continuation(
                    oracle_gen, eff_min_hits, count_hits
                )
                kept.extend(cont_kept)
                enumerated += cont_enum
                conflict_pruned += cont_pruned
                # Mixed-provenance total reconciliation — exact because both
                # enumerations share the lex order, so the rank partition is
                # exact across the handoff.
                if enumerated != count:
                    warn_fallback_once(
                        "enum_total_mismatch",
                        enumerated=enumerated,
                        expected=count,
                        provenance="mixed",
                    )
                    return None
                return RustEnumResult(
                    kept=kept,
                    enumerated=enumerated,
                    conflict_pruned=conflict_pruned,
                    hits_counted=hits_counted,
                )
            _consume_shadow(width, r_enum)
        kept.extend(r_kept)
        enumerated += r_enum
        conflict_pruned += r_pruned
        hits_counted += r_hits
        rank = next_rank
        if exhausted:
            break
    if rank != count or enumerated != count:
        # TOTAL-RECONCILIATION belt (design §1.3 round-4): the driver's own
        # independent count_combos total converts the whole total-count /
        # rank-partition defect class to deterministic per-tuple detection.
        warn_fallback_once("enum_total_mismatch", rank=rank, enumerated=enumerated, expected=count)
        return None
    if _ENGAGED_KEY not in _OBSERVABILITY_MEMO:
        _OBSERVABILITY_MEMO.add(_ENGAGED_KEY)
        _LOG.info(
            "sweep_rust_enum.engaged",
            width=width,
            enumerated=enumerated,
            k=k,
            min_hits=eff_min_hits,
        )
    return RustEnumResult(
        kept=kept,
        enumerated=enumerated,
        conflict_pruned=conflict_pruned,
        hits_counted=hits_counted,
    )


__all__ = [
    "DEFAULT_ENUM_WORK_CELLS",
    "ENUM_WORK_CELLS_ENV",
    "ENUM_WORK_CELLS_LOWER_CLAMP",
    "ENUM_WORK_CELLS_UPPER_CLAMP",
    "RANGE_MASKS_CAP",
    "RANGE_MASKS_FLOOR",
    "SWEEP_ENUM_SELF_CHECK_MASKS",
    "SWEEP_ENUM_SELF_CHECK_RANGES_CAP",
    "SWEEP_RUST_ENUM_DEFAULT",
    "SWEEP_RUST_ENUM_ENV",
    "RustEnumResult",
    "disable_kernel",
    "enum_prefilter_via_kernel",
    "enum_shadow_pending",
    "kernel_disabled",
    "reset_enum_self_check_for_tests",
    "reset_observability_memo_for_tests",
    "sweep_rust_enum_enabled",
    "warn_fallback_once",
]
