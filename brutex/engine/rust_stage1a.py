"""Flag-gated batch Rust Stage-1A bar-bits seam (``DEC-STAGE1A-RUST-BATCH-001``).

The G1 Rust-conversion slice: :func:`brutex.engine.stage1a.build_bar_bits`
gains a default-ON Rust arm (flipped by DEC-RUST-DEFAULT-ON-WAVE-001,
operator directive 2026-07-23; see §"The flag" below) that replaces the
per-bar Python driver loop
(one ``evaluate_with_levels`` + ``VwapState.update`` +
``MarketStructureState.update`` FFI call PER BAR) with ONE batch
``trading_core.stage1a_bar_bits_batch`` call per ``(instrument, tf)`` build.
This module owns the Python side of that seam: the env-flag resolution, the
burn-in whole-series self-check with the autorevert latch, the warn-once
fallback observability, and the plain-columns :class:`Stage1ABatchInputs`
carrier the ``Stage1AMatrixBuilder.collect_batch_inputs`` prepass fills.

## The flag — ``BRUTEX_SWEEP_RUST_STAGE1A`` over ``STAGE1A_RUST_DEFAULT``

:func:`stage1a_rust_enabled` reads the env per call, exactly like the sibling
:func:`brutex.engine.rust_metrics.sweep_rust_metrics_enabled` (the
``TRUTHY_LITERALS`` / ``FALSY_LITERALS`` vocabulary — PROMOTED to the
wheel-free :mod:`brutex.config.env_flags` leaf by
DEC-PREBUILD-FRAME-WRITE-001 — is consumed through the PUBLIC
:func:`brutex.engine.sweep_lookahead.env_flag_tristate` re-export, never
copied — CLAUDE.md §11 COMMON; per-call resolution means no module-global flag
state and every spawn worker reads its OWN inherited environment). Resolution:
a ``TRUTHY_LITERALS`` value forces ON, a ``FALSY_LITERALS`` value forces OFF, unset /
whitespace-empty falls to :data:`STAGE1A_RUST_DEFAULT` (**True — ON by
default** per DEC-RUST-DEFAULT-ON-WAVE-001, operator directive 2026-07-23;
the revert is that one constant), and any unrecognised value fails SAFE to
OFF with one warn-once ``flag_unrecognised`` fallback event.

## Burn-in self-check + autorevert latch

The first :data:`SELF_CHECK_BUILDS` engaged builds per process are ALSO
computed by the Python oracle (the frozen ``Stage1AMatrixBuilder``) and the
two ``list[int]`` matrices compared for FULL equality. **4 samples** — each
sample is a WHOLE-SERIES compare (a production build is ~10⁵-10⁶ u128 masks,
orders of magnitude more compared bits than the rust_metrics burn-in's 16
scalar 4-tuples) and each costs one full Python oracle build (~11 s at 606k
bars), so 16 would burn minutes per spawn worker; 4 bounds the startup tax
while still covering multiple real multi-year boundary corpora. A mismatch
SHIPS THE ORACLE LIST (correctness first — a wrong mask never reaches
Stage-1B), latches the kernel OFF for the process
(``rust_stage1a.kernel_autoreverted`` ERROR carrying bounded context: the
lengths, the first differing index and the two masks' hex — never the whole
matrix), and every later build short-circuits to the oracle.

## The fallback law

ANY problem — wheel missing / stale symbol, an FFI boundary refusal
(``ValueError``) or a PyO3 conversion failure (``TypeError`` /
``OverflowError``) — returns ``None`` after one warn-once
``rust_stage1a.fallback`` event; the caller then serves the build through the
UNTOUCHED Python builder, which raises or computes exactly today's behaviour
(wheel state can never change results or error behaviour). ``MemoryError``
re-raises BARE (OOM is a GLOBAL stop — DEC-LAKE-WRITE-HALT-001). The first
successful kernel-served build logs ``rust_stage1a.engaged`` (INFO) once per
process — the anti-vacuous-gate signal the golden suite asserts.

## Strict mode — the CLASS split (DEC-RUST-STRICT-NO-FALLBACK-001)

The batch boundary raises ONE ``ValueError`` class for two very different
populations, so :func:`_kernel_degrade_reason` classifies it from a marker the
CRATE stamps at its own raise site (:data:`_INPUT_REFUSED_MARKER`), never from
the exception type:

* ``kernel_input_refused`` (**CLASS C**, exempt) — a per-bar price/level/VWAP
  operand or bps knob outside the crate's overflow-safe envelope. Provably
  safe to degrade: the flag-OFF arm hits the SAME guards per bar, so the
  oracle re-raises the IDENTICAL error.
* ``kernel_refused`` (**CLASS A/B**, refused) — the UNMARKED remainder: a
  column-shape violation from ``stage1a_batch::validate_structure``, an
  ``Err`` from the batch compute core, or a PyO3 conversion failure. Rust ran
  and said no about our own marshalling; a quiet Python re-run would paper
  that over.

Nothing is HELD any more — :data:`_STRICT_UNRESOLVED_REASONS` is EMPTY.

## Honest envelope (Golden Rule #10)

* Constant-factor win only — the build stays ``O(N_bars)``; NO O-class change
  and never a "Rust ⇒ O(1)" claim.
* The engaged arm still pays the Python prepass (tz/snap/roll/day/anchor
  resolution + the EMA columns) — the amortised win is the removal of the
  per-bar 21-arg FFI marshal + snapshot/dataclass construction.
* Burn-in tax: ≤ :data:`SELF_CHECK_BUILDS` full oracle builds per process.
* The latch / counters / memo are single-cell mutable module state — the same
  documented exception to the no-global-mutable-state rule as
  :mod:`brutex.engine.rust_metrics` (results never depend on them; a racing
  thread can at worst run an extra self-check, never a wrong result).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import structlog

# The truthy/falsy literal sets live ONCE in the wheel-free
# ``brutex.config.env_flags`` leaf (the vocabulary home since
# DEC-PREBUILD-FRAME-WRITE-001) and are consumed through the sweep-flag
# module's PUBLIC re-exported classifier — never copied (CLAUDE.md §11).
from brutex.config.rust_strict import refuse_if_strict
from brutex.engine.sweep_lookahead import env_flag_tristate

if TYPE_CHECKING:  # pragma: no cover - import only for static typing
    from collections.abc import Callable

_LOG = structlog.get_logger("brutex.engine.rust_stage1a")

#: The env flag. ``TRUTHY_LITERALS`` forces the batch Rust arm ON, ``FALSY_LITERALS`` forces
#: it OFF, unset/empty falls to :data:`STAGE1A_RUST_DEFAULT`, anything
#: unrecognised fails SAFE to OFF with one warn-once ``flag_unrecognised``.
STAGE1A_RUST_ENV: Final[str] = "BRUTEX_SWEEP_RUST_STAGE1A"

#: The ONE default-arm constant — **True: the kernel is ON by default**
#: per DEC-RUST-DEFAULT-ON-WAVE-001 (operator directive 2026-07-23,
#: flipping DEC-STAGE1A-RUST-BATCH-001's inert-ship default), licensed by
#: the burn-in + golden parity evidence; the revert is this one constant.
STAGE1A_RUST_DEFAULT: Final[bool] = True

#: Kernel-served builds whole-series-compared against the Python oracle per
#: process (module docstring for the 4-vs-16 sizing rationale).
SELF_CHECK_BUILDS: Final[int] = 4

#: Once-per-process-per-reason fallback memo + the engaged-once latch.
#: LOG-VOLUME state only — results never depend on it.
_OBSERVABILITY_MEMO: Final[set[str]] = set()

#: Memo key for the engaged-once INFO (namespaced apart from warn reasons).
_ENGAGED_KEY: Final[str] = "__engaged__"

#: Burn-in samples still to run this process (single-cell mutable — the
#: documented no-global-mutable-state exception, module docstring).
_SELF_CHECK_REMAINING: Final[list[int]] = [SELF_CHECK_BUILDS]

#: The autorevert latch: the mismatch reason once the kernel is disabled for
#: this process, else ``None`` (same documented exception).
# ``# pragma: no mutate`` (the DEC-MUTATION-RUNTIME-001 rate_limiter.py
# precedent): mutmut's ``str | None`` → ``str & None`` ANNOTATION mutant is
# unkillable by any behavioral test under ``from __future__ import
# annotations`` (module-level annotations are never evaluated). Disclosed
# co-skip (the pragma removes EVERY mutant on the line): the ``= [None]`` →
# ``= None`` VALUE mutant leaves the catalog too — it was KILLED in the
# round-4 61-mutant run before the pragma landed (the autouse
# ``_fresh_seam_state`` reset indexes the cell, so ``None[0]`` raises
# ``TypeError`` in effectively every covering test), so no live teeth are
# lost.
_KERNEL_DISABLED_REASON: Final[list[str | None]] = [None]  # pragma: no mutate


@dataclass(frozen=True, slots=True)
class Stage1ABatchInputs:
    """Plain columns + day tables + config scalars for ONE batch build.

    Filled by ``Stage1AMatrixBuilder.collect_batch_inputs`` (the prepass that
    drives the builder's OWN roll/day/anchor machinery) and consumed 1:1 by
    ``trading_core.stage1a_bar_bits_batch``. Per-bar columns share one length
    ``N``; the day tables carry ``12·D`` / ``19·D`` flattened evaluator-ABI
    rows plus per-day warm flags. The three ``*_mask`` fields are the POSITIVE
    Python constants (``stage1a.ANCHOR_DEPENDENT_MASK`` /
    ``stage1a.FIB_DEPENDENT_MASK`` / ``bits.APPLIES_TO_1DAY_MASK``) — Rust
    complements in-width (a negative Python int cannot cross the u128 FFI
    edge; G-1).
    """

    open_paisa: list[int]
    high_paisa: list[int]
    low_paisa: list[int]
    close_paisa: list[int]
    typical_paisa: list[int]
    volume: list[int]
    tod_seconds: list[int]
    pre_open: list[bool]
    session_key: list[int]
    session_day: list[int]
    contract_reset: list[bool]
    day_index: list[int]
    ema20_paisa: list[int | None]
    ema200_paisa: list[int | None]
    supertrend_x2_paisa: list[int | None]
    day_pivot_levels: list[int]
    day_fib_levels: list[int]
    day_has_pivot: list[bool]
    day_has_fib: list[bool]
    # Group-15 X1 day tables (DEC-GAP-MIDPOINT-CONDITIONS-001): the prior
    # session's last-TF-bar high/low per day row (the `day_pivot_levels`
    # pattern — Muhurat skip + first-day/post-roll cold start stay PREPASS
    # decisions), plus the `near_gap_mid` bps knob. The values are (0, 0)
    # placeholders wherever `day_has_x1` is False — never read there.
    day_x1_hi_paisa: list[int]
    day_x1_lo_paisa: list[int]
    day_has_x1: list[bool]
    pivot_bps: int
    pdh_pdl_bps: int
    fib_bps: int
    gap_mid_bps: int
    #: Swing-proximity band for the near-swing bits 72/73
    #: (DEC-FIB-TEMPLATE-EXPRESS-001) — beside the sibling bps scalars.
    swing_bps: int
    prior_n: int
    fractal_k: int
    apply_1day_mask: bool
    anchor_dependent_mask: int
    fib_dependent_mask: int
    applies_to_1day_mask: int


def stage1a_rust_enabled() -> bool:
    """Resolve the batch-Rust flag over :data:`STAGE1A_RUST_DEFAULT`.

    * a ``TRUTHY_LITERALS`` value (``1/true/yes/on``) → ``True``;
    * a ``FALSY_LITERALS`` value (``0/false/no/off``) → ``False`` — the explicit
      operator kill switch, honoured regardless of the default;
    * unset or whitespace-empty → :data:`STAGE1A_RUST_DEFAULT` (**True** —
      ON by default per DEC-RUST-DEFAULT-ON-WAVE-001; the one-constant
      revert lever);
    * any OTHER value → ``False`` with one warn-once ``flag_unrecognised``
      fallback event (fail-safe OFF — a typo'd value must never silently arm
      an experimental kernel, and once the default flips ON a typo'd kill
      switch must still KILL).

    Read per call (one ``os.environ`` probe) — no global flag state; every
    ``sweep_parallel`` spawn worker reads its own inherited environment.
    """
    raw = os.environ.get(STAGE1A_RUST_ENV, "")
    verdict = env_flag_tristate(raw)
    if verdict is not None:
        return verdict
    if raw.strip():
        warn_fallback_once("flag_unrecognised", value=raw)
        return False
    return STAGE1A_RUST_DEFAULT


def self_check_pending() -> bool:
    """``True`` while this process still owes burn-in whole-series compares."""
    return _SELF_CHECK_REMAINING[0] > 0


def consume_self_check_sample() -> None:
    """Consume one burn-in sample after a PASSED kernel-vs-oracle compare.

    Clamped at zero so a racing extra call can never go negative. A FAILED
    compare never consumes — it latches :func:`disable_kernel` instead.
    """
    if _SELF_CHECK_REMAINING[0] > 0:
        _SELF_CHECK_REMAINING[0] -= 1


def kernel_disabled() -> bool:
    """``True`` iff the autorevert latch has disabled the kernel this process."""
    return _KERNEL_DISABLED_REASON[0] is not None


def disable_kernel(reason: str, **context: object) -> None:
    """Latch the kernel OFF for the remainder of the process (the autorevert).

    Called on a burn-in whole-series MISMATCH — a kernel/prepass divergence
    the structural validation cannot see. The first call logs
    ``rust_stage1a.kernel_autoreverted`` (ERROR — a genuine wrongness signal,
    never throttled below the operator's view) with the caller's bounded
    context; repeat calls are no-ops. Every subsequent
    :func:`rust_bar_bits_or_none` call short-circuits to the Python oracle,
    so a wrong kernel can poison ZERO shipped builds (even the mismatching
    build ships the ORACLE list).
    """
    if _KERNEL_DISABLED_REASON[0] is None:
        _KERNEL_DISABLED_REASON[0] = reason
        _LOG.error("rust_stage1a.kernel_autoreverted", reason=reason, **context)


#: The crate's input-VALUE-envelope refusal marker — the stable fragment
#: ``rust/trading-core/src/python.rs::INPUT_REFUSED_MARKER`` stamps on every
#: refusal raised by its three VALUE-envelope guards (``check_evaluate_inputs``
#: / ``vwap_update_guard`` / the day-table level-magnitude loop in
#: ``check_stage1a_batch_values``). Its presence is what separates CLASS C
#: (keep degrading, even under strict mode) from CLASS A/B (refuse) inside the
#: single ``ValueError`` class the batch boundary raises for both. The SAME
#: literal is used by ``rust/brutex-strategies`` /
#: :data:`brutex.strategies.combo_rust._INPUT_REFUSED_MARKER`, so the two
#: facades classify on ONE shared fragment; cross-language drift is pinned by
#: the unit suite against the real wheel.
_INPUT_REFUSED_MARKER: Final[str] = "input outside the crate envelope"

#: Fall-back reasons strict mode must NOT refuse — the CLASS-C exemption
#: (DEC-RUST-STRICT-NO-FALLBACK-001).
#:
#: * ``kernel_input_refused`` — a per-bar price/level/VWAP operand or a bps
#:   knob outside the crate's private overflow-safe envelope
#:   (``MAX_BAR_PAISA = i64::MAX / 65536`` / ``MAX_BPS``,
#:   ``rust/trading-core/src/python.rs``). The Python oracle was always the
#:   intended handler here, and the class is *provable* rather than guessed:
#:   the flag-OFF arm reaches the SAME three guards per bar (through
#:   ``evaluate_with_levels`` / ``VwapState.update``), so degrading re-raises
#:   the IDENTICAL ``ValueError`` — pinned by
#:   ``test_out_of_envelope_price_raises_identical_error_both_arms``. Refusing
#:   would replace that precise ``"out of the overflow-safe range"`` diagnosis
#:   with a generic :class:`~brutex.config.rust_strict.RustFallbackRefused`.
_STRICT_EXEMPT_REASONS: Final[frozenset[str]] = frozenset({"kernel_input_refused"})

#: EMPTY — nothing is held out of the taxonomy any more.
#:
#: ``kernel_refused`` used to live here because ONE ``except ValueError``
#: around ``trading_core.stage1a_bar_bits_batch`` bundled the kernel's own
#: STRUCTURAL refusals (CLASS A/B) together with an out-of-envelope input
#: (CLASS C), indistinguishable at this choke point — and the crate's
#: ``MAX_BAR_PAISA`` is private, so a Python-side pre-screen would have
#: duplicated a private constant and invited drift. That is resolved: the
#: crate now MARKS every input-envelope refusal it raises
#: (:data:`_INPUT_REFUSED_MARKER`), :func:`_kernel_degrade_reason` routes
#: those to ``kernel_input_refused`` in :data:`_STRICT_EXEMPT_REASONS` above,
#: and ``kernel_refused`` retains exactly the UNMARKED remainder — a column
#: -shape violation from ``stage1a_batch::validate_structure``, an ``Err``
#: from the batch compute core, or a PyO3 argument-conversion failure — which
#: is CLASS A/B and DOES refuse.
#:
#: The set is kept (empty) rather than deleted so a future "held pending a
#: ruling" reason has a named, reviewable home instead of being quietly
#: folded into the ruled exemption (the ``combo_rust`` precedent).
_STRICT_UNRESOLVED_REASONS: Final[frozenset[str]] = frozenset()

#: The reasons strict mode does NOT refuse — the ruled CLASS-C exemption plus
#: the (currently empty) unresolved-pending-ruling set, unioned at ONE point
#: so the two provenances stay separately reviewable above.
_STRICT_NOT_REFUSED: Final[frozenset[str]] = _STRICT_EXEMPT_REASONS | _STRICT_UNRESOLVED_REASONS


def _kernel_degrade_reason(exc: BaseException) -> str:
    """Classify a batch-FFI degrade as CLASS C (input) or CLASS A/B (fault).

    The classification reads a marker the CRATE minted at its own raise site
    — never a guess from the exception class, which is the single
    ``ValueError`` both populations share:

    * :data:`_INPUT_REFUSED_MARKER` present → ``kernel_input_refused`` (a
      per-bar price/level/VWAP operand or bps knob outside the crate's
      overflow-safe envelope; the oracle raises the identical error);
    * absent → ``kernel_refused``, the genuine-fault remainder (a
      ``validate_structure`` column-shape violation, an ``Err`` from the
      batch compute core, or a PyO3 ``TypeError`` / ``OverflowError``
      argument-conversion failure) which strict mode REFUSES.
    """
    if _INPUT_REFUSED_MARKER in str(exc):
        return "kernel_input_refused"
    return "kernel_refused"


def warn_fallback_once(reason: str, **context: object) -> None:
    """Refuse under strict mode; else log ``rust_stage1a.fallback`` warn-once.

    This is the ONE choke point every Stage-1A fall-back funnels through, so
    :func:`~brutex.config.rust_strict.refuse_if_strict` here is a *complete*
    block on the Python oracle — there is no unannounced path around it
    (DEC-RUST-STRICT-NO-FALLBACK-001).

    Four of the five reasons this facade can emit (``flag_unrecognised`` /
    ``kernel_disabled`` / ``wheel_missing`` / ``kernel_refused``) mean Rust is
    absent, latched, mis-configured or genuinely broke, so refusing them is
    exactly the intent. The fifth, ``kernel_input_refused``, is the ruled
    CLASS-C exemption in :data:`_STRICT_EXEMPT_REASONS` — see that constant
    for the proof that degrading there is byte-equal, not a paper-over.

    With strict mode OFF the historic behaviour is byte-identical: the seam
    runs once per kernel BUILD (up to thousands per grid sweep), so a sticky
    reason (a stale wheel, a latched kernel) would otherwise flood the logs,
    and the fallback itself is bit-exact — the Python oracle serves the build.
    """
    if reason not in _STRICT_NOT_REFUSED:
        refuse_if_strict("brutex.engine.rust_stage1a", reason, **context)
    if reason in _OBSERVABILITY_MEMO:
        return
    _OBSERVABILITY_MEMO.add(reason)
    _LOG.warning("rust_stage1a.fallback", reason=reason, **context)


def note_engaged_once(n_bars: int) -> None:
    """Log ``rust_stage1a.engaged`` (INFO) on the first kernel-served build.

    The observable "the wired ON path actually runs Rust" signal the golden
    suite asserts fires EXACTLY ONCE (the vacuous-gate trap teeth — Golden
    Rule #10).
    """
    if _ENGAGED_KEY in _OBSERVABILITY_MEMO:
        return
    _OBSERVABILITY_MEMO.add(_ENGAGED_KEY)
    _LOG.info("rust_stage1a.engaged", n_bars=n_bars)


def reset_self_check_for_tests(*, remaining: int = SELF_CHECK_BUILDS) -> None:
    """Reset the burn-in budget + autorevert latch (test isolation ONLY).

    ``remaining=0`` puts the process in the post-burn-in steady state; the
    default restores a fresh-process view. Never called from production code.
    """
    _SELF_CHECK_REMAINING[0] = remaining
    _KERNEL_DISABLED_REASON[0] = None


def reset_observability_memo_for_tests() -> None:
    """Clear the warn-once / engaged-once memo (test isolation ONLY)."""
    _OBSERVABILITY_MEMO.clear()


def _first_diff_index(got: list[int], expected: list[int]) -> int:
    """Index of the first differing mask (or the shorter length on a size skew).

    Bounded context for the G-3 autorevert ERROR — never the whole matrix.
    """
    for i, (g, e) in enumerate(zip(got, expected, strict=False)):
        if g != e:
            return i
    return min(len(got), len(expected))


def rust_bar_bits_or_none(
    inputs: Stage1ABatchInputs,
    *,
    oracle: Callable[[], list[int]],
) -> list[int] | None:
    """Run the batch Rust arm; ``None`` ⇒ the caller serves the Python oracle.

    The ONE FFI call site for ``trading_core.stage1a_bar_bits_batch``.
    Returns the full ``list[int]`` mask matrix on success (burn-in-compared
    against *oracle()* for the first :data:`SELF_CHECK_BUILDS` engaged builds
    per process; a mismatch ships the ORACLE list + latches the autorevert).
    Returns ``None`` — after a warn-once — when the kernel is latched, the
    wheel is missing/stale, or the FFI refuses the inputs
    (``ValueError`` — the boundary's own guards; ``TypeError`` /
    ``OverflowError`` — PyO3 argument-conversion failures, e.g. a paisa value
    beyond i64): the caller then builds through the UNTOUCHED Python builder,
    which raises today's exact error or computes — wheel state can never
    change results or error behaviour. ``MemoryError`` re-raises BARE (OOM is
    a GLOBAL stop); any other exception type PROPAGATES (a genuine code bug
    is never swallowed into a fallback — CLAUDE.md §4).

    Parameters
    ----------
    inputs
        The prepass-resolved columns/tables
        (``Stage1AMatrixBuilder.collect_batch_inputs``).
    oracle
        Zero-arg closure computing the frozen Python builder's matrix over
        the SAME bars — invoked only while burn-in samples are pending.

    Returns
    -------
    list[int] | None
        The mask matrix (kernel-served, or the oracle list on a burn-in
        mismatch), or ``None`` for a named fallback.
    """
    if kernel_disabled():
        warn_fallback_once("kernel_disabled")
        return None
    try:
        import trading_core

        batch_fn = trading_core.stage1a_bar_bits_batch
    except (ImportError, AttributeError):
        # The engine hard-imports trading_core at module scope elsewhere, so
        # the production trigger here is a STALE wheel lacking the batch
        # symbol (the AttributeError arm), not a truly absent wheel.
        warn_fallback_once("wheel_missing")
        return None
    try:
        result: list[int] = batch_fn(
            inputs.open_paisa,
            inputs.high_paisa,
            inputs.low_paisa,
            inputs.close_paisa,
            inputs.typical_paisa,
            inputs.volume,
            inputs.tod_seconds,
            inputs.pre_open,
            inputs.session_key,
            inputs.session_day,
            inputs.contract_reset,
            inputs.day_index,
            inputs.ema20_paisa,
            inputs.ema200_paisa,
            inputs.day_pivot_levels,
            inputs.day_fib_levels,
            inputs.day_has_pivot,
            inputs.day_has_fib,
            inputs.pivot_bps,
            inputs.pdh_pdl_bps,
            inputs.fib_bps,
            inputs.swing_bps,
            inputs.prior_n,
            inputs.anchor_dependent_mask,
            inputs.fib_dependent_mask,
            inputs.applies_to_1day_mask,
            inputs.apply_1day_mask,
            inputs.fractal_k,
            inputs.supertrend_x2_paisa,
            # Group-15 gap-midpoint (appended-last FFI law,
            # DEC-GAP-MIDPOINT-CONDITIONS-001): the bps knob + the X1 day
            # tables the batch core mints the per-day level from.
            inputs.gap_mid_bps,
            inputs.day_x1_hi_paisa,
            inputs.day_x1_lo_paisa,
            inputs.day_has_x1,
        )
    except (ValueError, TypeError, OverflowError) as exc:
        # The boundary's own guard ValueErrors + PyO3 conversion failures
        # degrade to the oracle (which raises today's exact error or
        # computes); MemoryError and every other type propagate. The reason
        # is CLASSIFIED from the crate's own marker, so strict mode can refuse
        # a genuine kernel fault without crashing an out-of-envelope input.
        warn_fallback_once(_kernel_degrade_reason(exc), error=repr(exc))
        return None
    if self_check_pending():
        expected = oracle()
        if result != expected:
            diff = _first_diff_index(result, expected)
            disable_kernel(
                "mask_mismatch",
                n_bars=len(inputs.open_paisa),
                got_len=len(result),
                expected_len=len(expected),
                first_diff_index=diff,
                expected_hex=hex(expected[diff]) if diff < len(expected) else None,
                got_hex=hex(result[diff]) if diff < len(result) else None,
            )
            return expected  # correctness first: the ORACLE list ships
        consume_self_check_sample()
    note_engaged_once(len(inputs.open_paisa))
    return result


__all__ = [
    "SELF_CHECK_BUILDS",
    "STAGE1A_RUST_DEFAULT",
    "STAGE1A_RUST_ENV",
    "Stage1ABatchInputs",
    "consume_self_check_sample",
    "disable_kernel",
    "kernel_disabled",
    "note_engaged_once",
    "reset_observability_memo_for_tests",
    "reset_self_check_for_tests",
    "rust_bar_bits_or_none",
    "self_check_pending",
    "stage1a_rust_enabled",
    "warn_fallback_once",
]
