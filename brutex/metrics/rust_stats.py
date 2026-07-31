"""Flag-gated Rust Stage-3/4 statistics seam — per-function kernel bindings.

DEC-METRICS-RUST-STATS-001 (slice 6, PR-1). The Python side of the Stage-3/4
statistics Rust conversion: the ``BRUTEX_STATS_RUST`` env-flag resolution, the
per-function kernel bindings with result validation, the burn-in self-check +
per-function autorevert latch, and the LOUD-but-flood-safe fallback logging.
The faithful instantiation of the proven :mod:`brutex.engine.rust_metrics`
template, generalized to PER-FUNCTION latches (a poisoned ``cpcv`` kernel must
never disable a future ``pbo`` kernel and vice versa). PR-1 ships ONE binding:
:func:`cpcv_pass_count_via_kernel` — the ``C(n_groups, n_test_groups)``
Chan/Pébay fold loop of :func:`brutex.metrics.cpcv.cpcv_pass_rate` (claim
class STRUCTURAL: the float op order is fixed by the Python source itself,
transcribed operation-for-operation in ``rust/fno-math/src/stats/cpcv.rs``).
PR-2 (DEC-METRICS-RUST-STATS-002 — the stats-tails continuation) APPENDS
four more bindings on the same per-function machinery:
:func:`psr_via_kernel` + :func:`expected_max_sharpe_via_kernel` (the DSR
pair of :mod:`brutex.metrics.deflated_sharpe` — STRUCTURAL transcription
with ``erf`` bound to the SAME system-libm symbol CPython's ``math.erf``
calls and the AS241 inverse-CDF transcribed from the CPython source; the
``rust/fno-math/src/stats/norm.rs`` law), :func:`pbo_cscv_via_kernel` (the
CSCV split-enumeration logit loop of :mod:`brutex.metrics.pbo` —
STRUCTURAL) and :func:`wfo_pass_count_via_kernel` (the per-walk test-window
Sharpe of :mod:`brutex.metrics.wfo` — REPLICATED: the numpy pairwise
reduction mirrored, probe-proven per platform, burn-in-fenced).

## The flag — ``BRUTEX_STATS_RUST`` over ``STATS_RUST_DEFAULT``

:func:`stats_rust_enabled` reads the env per call through the COMMON
:func:`brutex.engine.sweep_lookahead.env_flag_tristate` classifier (imported
LAZILY inside the function body so this metrics module never creates a
metrics→engine import-time cycle; CLAUDE.md §11 COMMON — the truthy/falsy
vocabulary is never copied). Resolution: a truthy value forces ON, a falsy
value forces OFF, unset/whitespace-empty falls to :data:`STATS_RUST_DEFAULT`
(**True — default-ON** per DEC-RUST-DEFAULT-ON-WAVE-001, operator directive
2026-07-23; the revert is that one constant), and any OTHER value
fails SAFE to OFF with one warn-once ``flag_unrecognised`` event. Flag
resolution itself NEVER raises: a SET flag in an environment where
``brutex.engine`` cannot import (its ``__init__`` pulls the trading_core
wheel) degrades to :data:`STATS_RUST_DEFAULT` with one warn-once
``flag_engine_unavailable`` event — so wheel state (fno_math OR
trading_core) changes only WHICH engine computes, never results or error
behaviour, within the documented exception envelope: the two disclosed
carve-outs are the NARROW-catch law (any exception type outside
``ValueError``/``TypeError``/``OverflowError`` from a byzantine wheel
PROPAGATES — see :func:`cpcv_pass_count_via_kernel`) and the
``panic=abort`` allocation-failure abort bullet in the honest envelope
below. OFF keeps
every caller on the verbatim pure-Python path, byte-identical by
construction — the Python originals are RETAINED FOREVER as the parity
oracles (operator exception).

## Burn-in self-check + per-function autorevert latch

For the first :data:`SELF_CHECK_SAMPLES` kernel-served calls per function per
process, the binding ALSO runs the Python oracle and compares EXACTLY (the
CPCV result is an integer count — compared ``==``; float-returning later
bindings compare ``struct.pack('<d', …)`` bit patterns). A mismatch — a
kernel numerical bug the domain validation cannot see — SHIPS THE ORACLE
VALUE (correctness first, never a wrong number persisted) and latches THAT
function's kernel OFF for the rest of the process (:func:`disable_kernel`,
one ``rust_stats.kernel_autoreverted`` ERROR); every later call
short-circuits to the Python oracle. The latch and the remaining-samples
counters are the same *documented exception* to the no-global-mutable-state
rule as :mod:`brutex.engine.rust_metrics`'s — a mismatch never changes
RESULTS, only which engine computes them; :func:`reset_self_check_for_tests`
restores a fresh process view. Plain dict writes (no lock): a racing thread
can at worst run a handful of extra self-checks — never a wrong result.
A fork-context child inherits this state verbatim — the fail-safe direction
(an inherited latch keeps the child on the oracle; an inherited exhausted
burn-in runs the child's kernel unchecked on the same already-validated
process image); production pools SPAWN, so each child runs its own fresh
burn-in (refuter round 4, verified empirically on both start methods).

## Fallback + engagement observability (Golden Rule #10)

Every fallback reason logs ``rust_stats.fallback`` (WARNING) ONCE per process
per ``(function, reason)``. A kernel result that fails the domain validation
is REFUSED loudly (``rust_stats.kernel_result_refused``, ERROR) and the call
is served by the Python oracle. The first successful kernel call per function
logs ``rust_stats.engaged`` (INFO) — the observable "the wired ON path
actually runs Rust" signal the ON-arm parity/golden suites ASSERT (the
vacuous-gate trap: a wheel-absent silent fallback must FAIL the parity suite,
never pass vacuously). :func:`reset_observability_memo_for_tests` restores a
fresh view for test isolation.

## Honest envelope (Golden Rule #10)

* Constant-factor win only: the kernel keeps the SAME asymptotics as the
  Python loop it replaces (CPCV stays ``O(C(S, k) · k)`` per call) — nothing
  here is O(1)-total.
* The first kernel call per process pays the lazy ``import fno_math`` wheel
  import; the burn-in DOUBLES the cost of the first
  :data:`SELF_CHECK_SAMPLES` calls per function (the oracle runs too) — a
  bounded, amortised-nil startup cost excluded from the steady-state bench.
* FFI copies per call are O(inputs) and disclosed on each binding.
* Never-rounded law: no Sharpe/DSR/p-value/CPCV statistic is rounded or
  formatted here; integers cross exactly, floats cross as raw f64.
* A Rust-side allocation failure under the crate's ``panic=abort`` profile
  is a process abort the Python ``MemoryError`` contract cannot catch (the
  CPCV kernel's scratch is O(n_groups) — ~16 machine words in production).
* The kernel call holds the GIL with no signal check between folds, so at a
  pathological ``n_groups``/``n_test_groups`` the ``C(S, k)`` fold loop is
  non-interruptible (Ctrl-C deferred, other Python threads blocked) for the
  call's duration, where the Python oracle stays interruptible between
  bytecodes — sub-ms at the production C(16, 8), so immaterial there.
* An env flip MID-process takes effect on the next call (per-call read);
  results are bit-identical either way.
* The kernel engages only for an exact-TYPE ``float``/``int``/``bool``
  ``min_sharpe`` whose f64 image is VALUE-exact (refuter rounds 1 + 2): a
  NaN, an ``int`` past 2^53, or an ``int`` past float range (where
  ``float()`` would raise OverflowError — caught, never propagated) is a
  pre-burn-in ``min_sharpe_inexact`` refusal to the oracle, whose int↔float
  comparison is EXACT where the FFI rounds; ANY other type — a ``float``
  subclass (whose own ordering dispatch could contradict the kernel's f64
  compare), Decimal/Fraction, numpy scalars, junk — is a
  ``min_sharpe_foreign_type`` refusal, so the oracle applies the ORIGINAL
  object's own comparison/raising semantics identically on BOTH arms
  (exact value and exception parity). Round 3 made both refusal paths'
  warn CONTEXTS total too: the inexact context's ``repr`` is
  digit-limit-guarded (``int.__repr__`` is itself PARTIAL — CPython ≥3.11
  raises ValueError past ``sys.get_int_max_str_digits()``, so a
  type-legal >4300-digit int crashed the ON arm pre-fix) and the foreign
  context's type NAME is read through ``type``'s C-level getset
  descriptor, bypassing a hostile metaclass ``__name__`` property — the
  seam dispatches no operation to the foreign INSTANCE
  (dunder-instrumentation-verified) and reads type metadata only through
  C-level slots, never metaclass-controlled attribute hooks. Round 4
  generalized that totality to the KERNEL-OUTPUT side and made it
  STRUCTURAL: every value the seam does not fully control (kernel results,
  kernel exceptions, the raw env flag value, the burn-in got/want pair)
  enters a log context ONLY through the guarded, length-bounded
  :func:`_safe_context` (a raising or str-subclass-returning ``repr``
  degrades to a typed placeholder; output clamped at
  :data:`_CONTEXT_MAX_CHARS` with an honest truncation marker;
  ``MemoryError`` re-raises BARE and every non-``Exception``
  ``BaseException`` — KeyboardInterrupt/SystemExit/GeneratorExit —
  likewise propagates bare, the same global-stop carve-out as the
  kernel call's narrow catch), kernel results are exact-TYPE validated
  (``type(result) is int`` — an ``int``-SUBCLASS result from a byzantine
  wheel is REFUSED before any comparison can dispatch its foreign
  ``__ge__``/``__ne__``, the output-side twin of the input gate), and the
  unit suite's AST structural pin forbids any future bare
  ``repr``/``str``/f-string context from bypassing the helpers. Nothing is
  normalized on the OFF arm
  (it stays byte-identical for every input). SANCTIONED NARROWING (round
  2): ``np.float64``/``np.int64`` thresholds previously engaged the kernel
  and now refuse to the oracle — ON≡OFF holds either way, and both in-repo
  production callers pass plain Python floats (performance-neutral).
"""

from __future__ import annotations

import math
import os
import struct
from typing import TYPE_CHECKING, Final, TypeGuard

import structlog

# DEC-RUST-STRICT-NO-FALLBACK-001. Safe at module scope: ``rust_strict`` is a
# LEAF (stdlib + ``brutex.config.env_flags`` + ``brutex.models``) and pulls
# NEITHER ``brutex.engine`` nor the trading_core wheel, so the cycle guard
# documented in :func:`stats_rust_enabled` is untouched.
from brutex.config.rust_strict import refuse_if_strict

if TYPE_CHECKING:  # pragma: no cover - import only for static typing
    from collections.abc import Callable, Mapping, Sequence

    import numpy as np
    import numpy.typing as npt

# ``__name__`` (not a string literal): identical logger name, and the
# mutation gate cannot silently rename it (the lake_stats_manifest idiom).
_LOG = structlog.get_logger(__name__)

#: The env flag. A truthy value forces the Rust statistics kernels ON, a
#: falsy value forces them OFF, unset/empty falls to
#: :data:`STATS_RUST_DEFAULT`, and anything unrecognised fails SAFE to OFF
#: with one warn-once ``flag_unrecognised`` event. ONE flag for the whole
#: slice — the per-function autorevert latch is the surgical lever, not
#: per-module env sub-flags.
STATS_RUST_ENV: Final[str] = "BRUTEX_STATS_RUST"

#: The ONE default-arm constant: what :func:`stats_rust_enabled` returns when
#: the env flag is unset or whitespace-empty. ``True`` = DEFAULT-ON per
#: DEC-RUST-DEFAULT-ON-WAVE-001 (operator directive 2026-07-23; the
#: ADR-0017 precedent); ``BRUTEX_STATS_RUST=0`` stays the explicit kill
#: switch and the revert is this one constant.
STATS_RUST_DEFAULT: Final[bool] = True

#: Burn-in samples per FUNCTION per process, sized by call cadence × oracle
#: cost (slice design ruling R8): cpcv runs per candidate with a ~55-59 ms
#: oracle ⇒ K=16 ≈ 0.9 s per process; the pool-level functions get smaller
#: budgets because their oracles cost seconds. Only ``cpcv`` ships in PR-1 —
#: the other keys pre-register the R8 budgets for the later slice PRs.
SELF_CHECK_SAMPLES: Final[Mapping[str, int]] = {
    "cpcv": 16,
    "mc": 8,
    "pbo": 4,
    "spa": 2,
    "wfo": 16,
    # DEC-METRICS-RUST-STATS-002 (PR-2): the DSR pair — cheap (~µs) scalar
    # oracles at high call cadence (once per leaderboard candidate), so the
    # cpcv-class K=16 budget applies to both.
    "psr": 16,
    "sr_max": 16,
}

#: Memo key suffix for the per-function engaged-once INFO (namespaced apart
#: from warn reasons).
_ENGAGED_KEY: Final[str] = "__engaged__"

#: Pseudo-function key for flag-level observability events (the flag is
#: resolved before any function is chosen).
_FLAG_FN: Final[str] = "flag"

#: Once-per-process-per-(function, reason) fallback memo + the per-function
#: engaged-once latches. LOG-VOLUME state ONLY: results never depend on it.
_OBSERVABILITY_MEMO: Final[set[str]] = set()

#: Self-check samples still to run this process, PER FUNCTION (the
#: documented no-global-mutable-state exception — module docstring).
_SELF_CHECK_REMAINING: Final[dict[str, int]] = dict(SELF_CHECK_SAMPLES)

#: The per-function autorevert latches: mismatch reason once THAT function's
#: kernel is disabled for this process (same documented exception).
_KERNEL_DISABLED_REASON: Final[dict[str, str]] = {}

#: Hard cap (chars) on any single log-context value the seam emits. The
#: LENGTH half of the round-4 context-totality law: a context that cannot
#: raise can still be enormous (a reprable ~4000-digit int under the default
#: digit cap, an arbitrarily long hostile type name) — every context string
#: rides :func:`_bounded_text`, so one refusal can never bloat the log
#: stream. Chosen well above every healthy context (float reprs, short
#: kernel error strings) and well below log-bloat territory.
_CONTEXT_MAX_CHARS: Final[int] = 200


def stats_rust_enabled() -> bool:
    """Resolve the Rust-statistics flag over :data:`STATS_RUST_DEFAULT`.

    * a truthy value (``1`` / ``true`` / ``yes`` / ``on``, case-insensitive,
      surrounding whitespace ignored) → ``True`` — the operator opt-in;
    * a falsy value (``0`` / ``false`` / ``no`` / ``off``) → ``False`` — the
      explicit kill switch, honoured regardless of the default;
    * unset or whitespace-empty → :data:`STATS_RUST_DEFAULT` (``True`` —
      ON by default per DEC-RUST-DEFAULT-ON-WAVE-001; the one-constant
      revert lever);
    * any OTHER value → ``False`` with one warn-once ``flag_unrecognised``
      fallback event (never a silently-ignored operator intent).

    The vocabulary is classified through the COMMON
    :func:`brutex.engine.sweep_lookahead.env_flag_tristate`, imported LAZILY
    inside this body — :mod:`brutex.metrics` must never import
    :mod:`brutex.engine` at module scope (import-time cycle guard); this
    function owns only the default + warn policy for the ``None`` verdict.
    If that engine import itself fails (``brutex.engine.__init__`` eagerly
    pulls the trading_core wheel — absent/stale wheels raise ImportError),
    the SET flag degrades to :data:`STATS_RUST_DEFAULT` with one warn-once
    ``flag_engine_unavailable`` event instead of propagating — flag
    resolution never raises. Read per call (one ``os.environ`` probe) so
    parallel workers read their own inherited environment and no
    module-global flag state exists.
    """
    raw = os.environ.get(STATS_RUST_ENV, "")
    if not raw.strip():
        # Unset/whitespace-empty short-circuits BEFORE the engine import:
        # ``env_flag_tristate`` would classify it ``None`` → the default
        # anyway (behavior-identical), and skipping the import keeps the
        # production-default ON arm free of the ``brutex.engine`` package
        # (whose ``__init__`` pulls the trading_core wheel) — precisely:
        # the default arm adds no TRADING_CORE-wheel requirement. (Loading
        # ``brutex.metrics`` at all has ALWAYS transitively required the
        # fno_math wheel via its parent package — ``__init__`` → compute →
        # types → strategies — on base and branch alike; this seam adds no
        # wheel dependency to any arm. The refuter-round-3 wording fix:
        # the previous "leaf metrics modules stay wheel-free-loadable"
        # phrasing over-claimed — it is true only of the trading_core
        # wheel this comment is scoped to.)
        return STATS_RUST_DEFAULT
    # Lazy by design: the engine package imports brutex.ranking →
    # brutex.metrics.cascade, so a module-scope metrics→engine import here
    # would be a cycle hazard. The body-level import is a sys.modules dict
    # hit after the first flagged call.
    try:
        from brutex.engine.sweep_lookahead import env_flag_tristate
    except ImportError:
        # ``brutex.engine``'s own import chain pulls the trading_core wheel
        # (``engine/__init__`` eagerly imports stage1a → trading_core), so a
        # SET flag in a wheel-absent/stale environment must degrade to the
        # default with one warn-once — an ImportError must never propagate
        # out of a statistics call (the "wheel state can never change
        # results or error behaviour" contract — scoped to its documented
        # exception envelope, module docstring — covers BOTH wheels).
        warn_fallback_once(_FLAG_FN, "flag_engine_unavailable")
        return STATS_RUST_DEFAULT

    verdict = env_flag_tristate(raw)
    if verdict is not None:
        return verdict
    # An unrecognised NON-EMPTY value fails SAFE to OFF, loudly (the
    # rust_metrics precedent) — a typo'd opt-in must never silently arm
    # nor a typo'd kill switch silently follow a future ON default. The
    # offending value is an ENV value the seam does not control, so it
    # rides the total, bounded ``_safe_context`` (repr-quoted — control
    # characters escaped — and length-clamped; round 4).
    warn_fallback_once(_FLAG_FN, "flag_unrecognised", value=_safe_context(raw))
    return False


def kernel_disabled(fn: str) -> bool:
    """Return ``True`` iff the autorevert latch has disabled *fn*'s kernel."""
    return fn in _KERNEL_DISABLED_REASON


#: CLASS-C reasons EXEMPT from the strict-mode refusal
#: (DEC-RUST-STRICT-NO-FALLBACK-001). Enumerated + reviewable here, never
#: buried in an inline condition. Everything NOT listed refuses.
#:
#: **The input-envelope family.** These arms fire when the INPUT is outside
#: what the FFI can carry losslessly — a value the kernel never claimed, whose
#: arbitrary-precision Python oracle was ALWAYS the intended handler. Refusing
#: them would crash a legitimate, type-legal caller; the honest fix is to widen
#: the crate (or the FFI) until each arm is unreachable, then delete the entry.
#:
#: * ``min_sharpe_inexact``     — the CPCV threshold is a NaN / an int outside
#:   exact f64 range (2^53+k, >4300-digit); an f64 round-trip would change the
#:   fold verdict.
#: * ``min_sharpe_foreign_type``— the threshold is a float SUBCLASS / Decimal /
#:   Fraction / numpy scalar whose own ``__gt__`` owns the compare.
#: * ``arg_inexact``            — a scalar argument is not exactly f64-representable.
#: * ``arg_foreign_type``       — an argument (scalar or a moments-matrix entry)
#:   is an exact-type mismatch (int subclass, float subclass, …).
#: * ``arg_unsupported``        — an integer argument is below the law's minimum
#:   or past ``i64`` — outside the kernel's declared domain.
#: * ``custom_evaluator``       — the PBO caller supplied its OWN evaluator, so
#:   there is no kernel to call: Python is the only implementation.
#: * ``combination_cap``        — the CSCV combination count exceeds the kernel's
#:   documented allocation cap; the oracle streams it instead.
#:
#: **The one non-input exemption** —
#: * ``flag_engine_unavailable`` — this is NOT a Python-serving fall-back at
#:   all: the flag resolver failed to import ``brutex.engine``'s tristate helper
#:   and returns :data:`STATS_RUST_DEFAULT` (``True``), i.e. it stays ON and the
#:   Rust arm still serves. Refusing here would turn a WORKING Rust path into a
#:   crash, the exact inverse of this DEC's intent.
_STRICT_EXEMPT_REASONS: Final[frozenset[str]] = frozenset(
    {
        "min_sharpe_inexact",
        "min_sharpe_foreign_type",
        "arg_inexact",
        "arg_foreign_type",
        "arg_unsupported",
        "custom_evaluator",
        "combination_cap",
        "flag_engine_unavailable",
    }
)


def disable_kernel(fn: str, reason: str, **context: object) -> None:
    """Latch *fn*'s kernel OFF for the remainder of the process (autorevert).

    DEC-RUST-STRICT-NO-FALLBACK-001: refuses under ``BRUTEX_RUST_STRICT``. The
    only reason routed here is the burn-in ``mismatch`` — CLASS B, the kernel
    disagreed with the oracle — so it is never exempt. The refusal fires BEFORE
    the latch is written, so under strict mode the latch is never set and every
    ``kernel_disabled`` fall-back below is structurally unreachable. Strict-OFF
    is byte-identical to the prior behaviour.

    Called on a burn-in compare MISMATCH — a kernel numerical bug the domain
    validation cannot see. The first call per function logs
    ``rust_stats.kernel_autoreverted`` (ERROR — a genuine kernel-wrongness
    signal, never throttled below the operator's view); repeat calls are
    no-ops. The latch is PER FUNCTION: a poisoned ``cpcv`` kernel never
    disables a sibling function's kernel. Even the mismatching call ships
    the ORACLE value (the binding's contract). Context values follow the
    same pre-stringified-total contract as :func:`warn_fallback_once` —
    any value the seam does not control goes through :func:`_safe_context`
    at the call site (enforced in-module by the unit suite's AST pin).
    """
    refuse_if_strict("brutex.metrics.rust_stats", reason, fn=fn, **context)
    if fn not in _KERNEL_DISABLED_REASON:
        _KERNEL_DISABLED_REASON[fn] = reason
        _LOG.error("rust_stats.kernel_autoreverted", fn=fn, reason=reason, **context)


def warn_fallback_once(fn: str, reason: str, **context: object) -> None:
    """Log ``rust_stats.fallback`` ONCE per process per ``(fn, reason)``.

    The cpcv binding runs once per CANDIDATE (thousands per production
    cascade pool), so a sticky fallback reason (a stale wheel, a latched
    kernel) would flood the logs; the first occurrence is loud (WARNING),
    repeats fall back silently. The fallback itself is bit-exact regardless
    (the Python oracle serves the call).

    The memo keys on ``(fn, reason)`` ONLY — deliberate flood-safety
    (refuter-round-4 INFO, documented): a later occurrence carrying a
    DIFFERENT context value (e.g. a different unrecognised flag string) is
    silent; only the first occurrence's value is ever logged. Context
    values must be pre-stringified TOTAL text — any value the seam does not
    control goes through :func:`_safe_context` at the call site (enforced
    in-module by the unit suite's AST structural pin).

    DEC-RUST-STRICT-NO-FALLBACK-001: refuses under ``BRUTEX_RUST_STRICT``
    unless *reason* is in :data:`_STRICT_EXEMPT_REASONS` (the input-envelope
    family + ``flag_engine_unavailable`` — see that constant's comment for the
    per-reason justification). The refused set is CLASS A
    (``flag_unrecognised``, ``wheel_missing``, ``wheel_stale``,
    ``kernel_error``) and CLASS B (``kernel_disabled`` — a prior burn-in
    mismatch latched this ``fn``). Strict-OFF is byte-identical to the prior
    behaviour.
    """
    if reason not in _STRICT_EXEMPT_REASONS:
        refuse_if_strict("brutex.metrics.rust_stats", reason, fn=fn, **context)
    key = f"{fn}:{reason}"
    if key in _OBSERVABILITY_MEMO:
        return
    _OBSERVABILITY_MEMO.add(key)
    _LOG.warning("rust_stats.fallback", fn=fn, reason=reason, **context)


def reset_self_check_for_tests(fn: str | None = None, *, remaining: int | None = None) -> None:
    """Reset burn-in budgets + autorevert latches (test isolation ONLY).

    ``fn=None`` restores the EXACT fresh-process view — extraneous budget
    keys inserted by prior TARGETED resets (a never-registered *fn*) and
    latches on never-registered names are CLEARED too, never left behind as
    a superset. A named *fn* resets just that one (budget + latch).
    ``remaining=0`` puts the target(s) in the post-burn-in steady state
    (used by the speedup bench); ``remaining=None`` restores each target's
    :data:`SELF_CHECK_SAMPLES` default. Never called from production paths.
    """
    if fn is None:
        _SELF_CHECK_REMAINING.clear()
        _KERNEL_DISABLED_REASON.clear()
    targets = list(SELF_CHECK_SAMPLES) if fn is None else [fn]
    for target in targets:
        default = SELF_CHECK_SAMPLES.get(target, 0)
        _SELF_CHECK_REMAINING[target] = default if remaining is None else remaining
        _KERNEL_DISABLED_REASON.pop(target, None)


def reset_observability_memo_for_tests() -> None:
    """Clear the warn-once / engaged-once memo (test isolation ONLY).

    The memo throttles LOG VOLUME across a whole process; tests capturing
    structlog output reset it so each test observes first-occurrence events.
    Never called from production code paths.
    """
    _OBSERVABILITY_MEMO.clear()


def _resolve_kernel(fn: str, attr: str) -> Callable[..., object] | None:
    """Return ``fno_math.<attr>``, or ``None`` with a warn-once.

    Lazy single-import-site (the :func:`brutex.engine.rust_metrics.
    _resolve_kernel` convention): a wheel-ABSENT environment degrades to the
    Python oracle with one ``wheel_missing`` warning per function; a STALE
    wheel that imports but predates the slice-6 symbol degrades with
    ``wheel_stale``. Either way results are bit-identical — the oracle
    serves — and the ON-arm parity suites assert engagement so a silently
    wheel-less CI can never pass vacuously.

    Reachability honesty (round-3 refuter INFO): in an INSTALLED
    environment a truly absent fno_math fails EARLIER — the
    ``brutex.metrics`` package import itself (its parent chain has always
    required the wheel) or the flag resolution's
    ``flag_engine_unavailable`` degrade — so the ``wheel_missing`` arm is
    belt-covering defense-in-depth (unit-driven, production-unreachable
    today); ``wheel_stale`` is the arm a real deployment can hit (an old
    wheel predating the slice-6 symbol).
    """
    try:
        import fno_math
    except ImportError:
        warn_fallback_once(fn, "wheel_missing")
        return None
    kernel: object = getattr(fno_math, attr, None)
    if not callable(kernel):
        # Absent (None) OR a non-callable stand-in — either way the wheel
        # predates (or corrupts) the slice-6 symbol. The ``object``
        # annotation + ``callable`` narrowing is deliberate: an
        # annotation-only ``X | None`` local is runtime-unevaluated, so its
        # mutants are provably unkillable (the #1614 PEP-526 lesson) — this
        # shape carries none.
        warn_fallback_once(fn, "wheel_stale", attr=attr)
        return None
    return kernel


def _valid_pass_count(result: object, n_total: int) -> TypeGuard[int]:
    """Cheap domain validation of a kernel pass-count — the refuse-poison gate.

    ``O(1)``: EXACTLY ``int`` — ``type(result) is int``, the OUTPUT-side
    twin of the ``min_sharpe`` exact-TYPE gate (round-4 completeness
    sweep; SANCTIONED NARROWING — the previous ``isinstance`` accepted an
    ``int`` SUBCLASS, whose range check ``0 <= result`` dispatched the
    subclass's own ``__ge__`` FIRST, so a byzantine wheel returning a
    hostile-comparison subclass raised ON-only, and an adopted benign
    subclass would have dispatched its foreign ``__ne__``/``__truediv__``
    downstream; the real Rust kernel returns an exact ``int``, and a
    refused subclass is served by the oracle — ON≡OFF either way).
    ``bool`` is excluded for free (``type(True)`` is ``bool``, not
    ``int``); the ``[0, n_total]`` range check on the proven-exact int is
    C-level total. A result inside this domain that is nonetheless WRONG
    is the burn-in compare's + the parity/golden suites' layer to catch —
    a layer with 1-ULP teeth: the round-5 ULP-boundary discriminator pins
    (nextafter-bracketed thresholds at achieved fold Sharpes over
    NON-power-of-2 group counts) fail on any kernel whose fold arithmetic
    drifts by even one ULP on those fixtures, the class the value-level
    parity cases alone were proven blind to (MIGRATION.md #1928 round 5).
    """
    return type(result) is int and 0 <= result <= n_total


def _type_name(tp: type) -> str:
    """*tp*'s real name via ``type``'s C-level getset descriptor.

    Round-3 refuter finding (fix round 4): ``tp.__name__`` is an ordinary
    attribute lookup dispatched through *tp*'s METACLASS MRO — a hostile
    metaclass defining ``__name__`` as a raising property made the
    foreign-type refusal raise ON-only on every call (the same defect
    class as the round-2 hostile-``__repr__`` NIT that refusal already
    avoids at the instance level). ``type.__dict__['__name__']`` is the
    C-level getset descriptor itself; invoking its ``__get__`` directly
    reads the type object's real name slot and can never dispatch
    foreign (metaclass-defined) code.
    """
    name: str = type.__dict__["__name__"].__get__(tp)
    return name


def _bounded_text(text: str) -> str:
    """Clamp *text* to :data:`_CONTEXT_MAX_CHARS` with an honest marker.

    The LENGTH half of the round-4 context-totality law: a context value
    that cannot raise can still be enormous (a reprable ~4000-digit int
    under the default digit cap, a hostile million-char type name) — every
    context string the seam emits rides this clamp, so one refusal can
    never bloat the log stream. The marker names the exact omitted char
    count (never a silent cut). Callers pass an EXACT ``str`` (the
    :func:`_safe_context` gate refuses str subclasses before this runs).
    """
    if len(text) <= _CONTEXT_MAX_CHARS:
        return text
    return f"{text[:_CONTEXT_MAX_CHARS]}<+{len(text) - _CONTEXT_MAX_CHARS} chars truncated>"


def _safe_context(value: object) -> str:
    """TOTAL, BOUNDED ``repr`` for a log-context value the seam does not own.

    Round-4 refuter finding (fix round 5): the kernel-OUTPUT-side warn
    contexts (``kernel_error``'s ``error=repr(exc)``,
    ``kernel_result_refused``'s ``result=repr(result)``) bare-``repr``'d a
    KERNEL-controlled object, so a byzantine wheel returning a >4300-digit
    int (``int.__repr__`` raises ValueError past the digit cap), raising
    ``ValueError(10**5000)``, or raising an exception subclass with a
    hostile ``__repr__`` made the seam's OWN refusal logging raise ON-only
    where OFF returns a rate — the exact defect class round 3 closed on
    the ``min_sharpe`` INPUT contexts, left un-generalized on the output
    side. This helper is the ONE total mechanism every not-seam-controlled
    context value now rides (kernel results, kernel exceptions, the raw
    env flag value, the burn-in got/want pair):

    * a ``repr`` that raises (ANY ``Exception``) degrades to a typed
      placeholder built from C-level type metadata only (:func:`_type_name`
      — no foreign dispatch, no second failure mode);
    * a ``repr`` returning a ``str`` SUBCLASS is refused into the same
      placeholder (a hostile subclass could smuggle lying
      ``__len__``/slicing dispatch into the clamp or the renderer);
    * the output is length-clamped via :func:`_bounded_text`;
    * the guard catches ``Exception`` ONLY, so the FULL global-stop family
      propagates BARE (the documented carve-out, the same law as the
      kernel call's narrow catch): ``MemoryError`` — an ``Exception``
      subclass — is re-raised explicitly (OOM is never swallowed into a
      log placeholder), and every non-``Exception`` ``BaseException``
      (``KeyboardInterrupt`` / ``SystemExit`` / ``GeneratorExit`` / a raw
      ``BaseException``) from a byzantine ``__repr__`` passes the
      ``except Exception`` arm untouched — these are the deliberate
      ON-only raises this helper keeps, inside the module's disclosed
      exception envelope (refuter round 5 completed the enumeration: the
      pre-round-5 wording named only MemoryError).
    """
    try:
        text: object = repr(value)
    except MemoryError:
        raise
    except Exception:  # pylint: disable=broad-exception-caught — totality IS the contract here
        text = None
    if type(text) is str:
        return _bounded_text(text)
    return _bounded_text(f"<{_type_name(type(value))} repr-failed>")


def _inexact_threshold_context(min_sharpe: float) -> str:
    """Digit-limit-safe ``repr`` of an exact-type refusal threshold.

    Round-3 refuter finding (fix round 4): ``int.__repr__`` is itself
    PARTIAL — CPython ≥3.11 caps int→str conversion at
    ``sys.get_int_max_str_digits()`` (default 4300), so the OverflowError
    refusal arm's own warn-context ``repr`` raised ValueError ON-only for
    a type-legal exact ``int`` past ~4300 digits (where OFF returns a
    rate). A refusal's context must never out-raise the refusal: the
    digit-limit ValueError degrades to a bounded placeholder built from
    ``bit_length()`` (O(1), conversion-free). Only exact-type
    ``float``/``int``/``bool`` values reach this helper (the gate's
    earlier arms) — never a foreign object's ``__repr__``; float repr is
    total, so the placeholder arm is reachable only for huge ints. The
    reprable arm rides the round-4 :func:`_bounded_text` length clamp (a
    type-legal ~4000-digit int under the default digit cap reprs FINE at
    ~4000 chars — bounded, never a log-bloating context); this helper is
    the exact-type-specialized sibling of :func:`_safe_context`, kept
    separate for its ``bit_length`` forensics.
    """
    try:
        return _bounded_text(repr(min_sharpe))
    except ValueError:
        bits = int(min_sharpe).bit_length()
        return f"<int past the int->str digit limit; bit_length={bits}>"


def _self_check_pending(fn: str) -> bool:
    """Return ``True`` while *fn* still owes burn-in self-check samples."""
    return _SELF_CHECK_REMAINING.get(fn, 0) > 0


def _consume_self_check_sample(fn: str) -> None:
    """Consume one burn-in sample after a PASSED kernel-vs-oracle compare.

    Clamped at zero so a racing extra call can never go negative. A FAILED
    compare never consumes — it latches :func:`disable_kernel` instead.
    """
    if _SELF_CHECK_REMAINING.get(fn, 0) > 0:
        _SELF_CHECK_REMAINING[fn] -= 1


def _log_engaged_once(fn: str) -> None:
    """Log ``rust_stats.engaged`` (INFO) on *fn*'s first kernel success."""
    key = f"{fn}:{_ENGAGED_KEY}"
    if key not in _OBSERVABILITY_MEMO:
        _OBSERVABILITY_MEMO.add(key)
        _LOG.info("rust_stats.engaged", fn=fn)


def cpcv_pass_count_via_kernel(
    counts: npt.NDArray[np.intp],
    means: npt.NDArray[np.float64],
    m2s: npt.NDArray[np.float64],
    n_test_groups: int,
    min_sharpe: float,
    *,
    n_total: int,
    oracle: Callable[[], int],
) -> int | None:
    """Run the Rust CPCV fold pass-count kernel; ``None`` ⇒ use Python.

    The ONE FFI call site for the ``cpcv`` binding. *counts*/*means*/*m2s*
    are the per-group moment triples ``_group_moments`` computed (numpy
    stays the moments producer — the seam boundary of slice-6 ruling R2);
    *n_total* is ``math.comb(n_groups, n_test_groups)`` for domain
    validation; *oracle* is the extracted Python fold loop
    (:func:`brutex.metrics.cpcv._fold_pass_count` closed over the same
    inputs), invoked ONLY for the burn-in compare or by the caller on a
    ``None`` return.

    Returns the integer pass-count on success. Returns ``None`` — after a
    warn-once (or, for a refused result, an ERROR) — when the kernel is
    latched, unavailable, raises a boundary/conversion error, returns a
    domain-invalid value, or when *min_sharpe* fails the exact-type domain
    gate: only an exact-TYPE ``float``/``int``/``bool`` whose f64 image is
    value-exact engages — a NaN or a non-representable/unconvertible
    ``int`` (incl. past float range, where ``float()``'s OverflowError is
    caught into the refusal, never raised) refuses as
    ``min_sharpe_inexact`` (whose warn context is itself digit-limit-safe
    — :func:`_inexact_threshold_context` guards the ``repr`` a
    >4300-digit int would crash, round 3), and ANY other type (float
    subclass / Decimal / numpy scalar / junk) refuses as
    ``min_sharpe_foreign_type`` (type name via the metaclass-safe
    :func:`_type_name`) so the
    oracle applies the original object's own comparison semantics on both
    arms — see the in-body note; the caller then serves the call
    through the bit-exact Python oracle, so wheel state can never change
    results or error behaviour (within the documented exception envelope —
    module docstring). The catch is NARROW (``ValueError`` — the kernel's
    own structural arms; ``TypeError`` / ``OverflowError`` — PyO3
    argument-conversion failures); any OTHER exception type PROPAGATES and
    ``MemoryError`` re-raises BARE (OOM is a GLOBAL stop — never swallowed
    into a fallback). Every fallback/refusal CONTEXT the kernel can
    influence (the caught exception, the refused result + ``n_total``, the
    burn-in got/want pair) rides the total, bounded :func:`_safe_context`,
    so a hostile ``repr`` can never out-raise the refusal that exists to
    serve the call (round 4). During burn-in a compare MISMATCH ships the
    ORACLE value and latches this function's kernel (module docstring).

    FFI cost (disclosed): three ``O(n_groups)`` list conversions per call —
    48 scalars at the production S=16; negligible next to the
    ``O(C(S, k)·k)`` fold work either engine performs.
    """
    fn = "cpcv"
    if kernel_disabled(fn):
        warn_fallback_once(fn, "kernel_disabled")
        return None
    # min_sharpe EXACT-TYPE + exactness DOMAIN GUARD (refuter rounds 1 + 2).
    # The FFI necessarily feeds the kernel a plain f64, while the Python
    # oracle dispatches ``sharpe > min_sharpe`` to the ORIGINAL threshold
    # OBJECT — so the kernel may engage ONLY when the value crosses the FFI
    # boundary exactly AND the object's comparison semantics are provably
    # CPython's own float/int ordering (which matches the kernel's f64
    # compare wherever the conversion is value-exact). Round 1 caught the
    # VALUE half (an ``int`` past 2^53 rounds through ``float()`` while the
    # oracle's mixed-type int↔float compare is EXACT — ON≠OFF + a burn-in
    # false autorevert). Round 2 caught the SEMANTICS half (a type-legal
    # ``float`` SUBCLASS with reversed ordering but honest equality passes
    # any value-exactness test yet flips the oracle's ``>``) and the
    # CONVERSION half (``float(10**400)`` raised OverflowError out of the
    # ON arm where OFF returns a rate — the hoisted conversion sat outside
    # the narrow catch). The gate is therefore exact-TYPE first; every
    # refusal is pre-burn-in (no sample consumed, no latch risk) and the
    # OFF arm stays byte-identical: nothing is ever normalized.
    # ``type[object]`` (not the inferred ``type[float]``): the param is
    # ANNOTATED ``float`` but the PEP-484 numeric tower + hostile callers
    # deliver ints/subclasses/foreign objects at runtime — the widened
    # annotation keeps the int/bool arm reachable for the type checkers.
    ms_type: type[object] = type(min_sharpe)
    if ms_type is float:
        if math.isnan(min_sharpe):
            # NaN note (documented round-1 choice, retained): a NaN
            # threshold conservatively refuses to the oracle — SAFE and
            # result-identical (``sharpe > nan`` is False on BOTH engines,
            # every fold a non-pass, unit-pinned); a degenerate
            # all-non-pass threshold has no kernel win worth a special
            # case. ``math.isnan`` on the exact-type float — the gate
            # above guarantees no subclass dispatch reaches the seam.
            warn_fallback_once(
                fn, "min_sharpe_inexact", min_sharpe=_inexact_threshold_context(min_sharpe)
            )
            return None
        min_sharpe_f = min_sharpe
    elif ms_type is int or ms_type is bool:
        # Exact ``int`` (and ``bool`` — an exact, unsubclassable int
        # subtype carrying int's own ordering; documented choice: it
        # engages like the exact 0/1 it is) engages IFF ``float()`` is
        # value-exact. The conversion is GUARDED: an int past float range
        # makes ``float()`` raise OverflowError — the same
        # not-float-representable class as an inexact 2^53+k, so it takes
        # the SAME refusal, never a raise (the round-2 D1 regression:
        # pre-guard this crashed the ON arm where OFF returns a rate).
        # The warn context rides the digit-limit-guarded
        # ``_inexact_threshold_context`` — a bare ``repr`` here was the
        # round-3 residual of D1: ``int.__repr__`` itself raises
        # ValueError past ``sys.get_int_max_str_digits()``, so a
        # type-legal >4300-digit int crashed the refusal that exists to
        # never raise.
        try:
            min_sharpe_f = float(min_sharpe)
        except OverflowError:
            warn_fallback_once(
                fn, "min_sharpe_inexact", min_sharpe=_inexact_threshold_context(min_sharpe)
            )
            return None
        if min_sharpe_f != min_sharpe:
            warn_fallback_once(
                fn, "min_sharpe_inexact", min_sharpe=_inexact_threshold_context(min_sharpe)
            )
            return None
    else:
        # ANY other type — a ``float`` subclass (its own ``__lt__``/``__gt__``
        # take dispatch priority over ``float.__gt__`` in the oracle's
        # compare), Decimal/Fraction, numpy scalars, strings, arbitrary
        # objects — refuses to the oracle, which applies the ORIGINAL
        # object's own comparison/raising semantics identically on BOTH
        # arms (exact value AND exception parity — the round-2 D1
        # exception-parity regression + the D2 subclass ordering attack).
        # SANCTIONED NARROWING vs fix round 2: ``np.float64``/``np.int64``
        # thresholds previously engaged the kernel and now refuse — ON≡OFF
        # holds either way (the oracle serves), and both in-repo
        # production callers pass plain Python floats. The warn context
        # carries the TYPE NAME only, never ``repr`` — a hostile
        # ``__repr__`` must not raise ON-only (round-2 NIT closed) — and
        # the name itself is fetched through ``type``'s C-level getset
        # (:func:`_type_name`): a bare ``ms_type.__name__`` dispatched
        # through the foreign-controlled METACLASS MRO, where a raising
        # ``__name__`` property crashed the ON arm (round-3 fix). The
        # scoped claim: the seam dispatches NO operation to the foreign
        # INSTANCE (dunder-instrumentation-verified) and reads type
        # metadata only through C-level slots, never metaclass-controlled
        # attribute hooks. The name is additionally length-clamped
        # (round 4): a type NAME is attacker-sized (``type("T" * 10**6,
        # (float,), {})``), so even the total lookup rides the bound.
        warn_fallback_once(
            fn, "min_sharpe_foreign_type", min_sharpe_type=_bounded_text(_type_name(ms_type))
        )
        return None
    kernel = _resolve_kernel(fn, "stats_cpcv_pass_count")
    if kernel is None:
        return None
    try:
        result = kernel(
            [int(x) for x in counts],
            [float(x) for x in means],
            [float(x) for x in m2s],
            int(n_test_groups),
            min_sharpe_f,
        )
    except (ValueError, TypeError, OverflowError) as exc:
        # The kernel's structural ValueError arms + PyO3 conversion errors
        # degrade to the bit-exact oracle; any OTHER exception type (incl.
        # MemoryError — a GLOBAL stop) propagates. The caught exception is
        # KERNEL-controlled — its context rides the guarded, bounded
        # ``_safe_context`` (round 4: a byzantine ``ValueError(10**5000)``
        # or a hostile exception ``__repr__`` previously detonated the
        # seam's OWN fallback logging ON-only, where OFF returns a rate).
        warn_fallback_once(fn, "kernel_error", error=_safe_context(exc))
        return None
    if not _valid_pass_count(result, n_total):
        # A domain-invalid result is a kernel BUG: refuse it LOUDLY (never
        # silently adopted) and serve the call from the Python oracle. The
        # refused object is KERNEL-controlled and ``n_total`` is
        # caller-sized (``math.comb`` can exceed the int→str digit cap at
        # adversarial shapes) — both contexts ride ``_safe_context``
        # (round 4: a >4300-digit refused result previously detonated the
        # refusal's own ``repr`` ON-only).
        # DEC-RUST-STRICT-NO-FALLBACK-001: this arm logs its own ERROR rather
        # than routing through ``warn_fallback_once``, so it needs the strict
        # gate explicitly. CLASS B (a kernel BUG) — refused, never exempt.
        refuse_if_strict(
            "brutex.metrics.rust_stats",
            "kernel_result_refused",
            fn=fn,
            result=_safe_context(result),
        )
        key = f"{fn}:kernel_result_refused"
        if key not in _OBSERVABILITY_MEMO:
            _OBSERVABILITY_MEMO.add(key)
            _LOG.error(
                "rust_stats.kernel_result_refused",
                fn=fn,
                result=_safe_context(result),
                n_total=_safe_context(n_total),
            )
        return None
    if _self_check_pending(fn):
        expected = oracle()
        if result != expected:
            # SHIP THE ORACLE VALUE — correctness first — and latch this
            # function's kernel for the rest of the process. got/want are
            # kernel-/oracle-produced counts — bounded totals via
            # ``_safe_context`` (round 4: a huge-but-in-domain count under
            # an adversarial ``n_total`` would detonate structlog's own
            # render of a raw int context past the digit cap).
            disable_kernel(
                fn,
                "self_check_mismatch",
                got=_safe_context(result),
                want=_safe_context(expected),
            )
            return expected
        _consume_self_check_sample(fn)
    _log_engaged_once(fn)
    return result


# ─────────────────────────────────────────────────────────────────────────
# DEC-METRICS-RUST-STATS-002 (slice 6, PR-2 — the stats tails). APPENDED
# bindings mirroring the cpcv PR-1 pattern per function: latch check →
# input exact-type/exactness gates → lazy kernel resolve → NARROW-catch FFI
# call → exact-type result domain validation → burn-in bit-compare with
# ship-the-oracle autorevert → engaged-once. The cpcv binding above is
# byte-untouched (its inline ``min_sharpe`` gate predates the shared
# :func:`_numeric_as_f64` law below; the unit suite cross-pins the two
# behaviorally equal so they can never drift — folding the inline copy onto
# the helper is deliberately NOT done here, the append-only discipline).
# ─────────────────────────────────────────────────────────────────────────

#: i64 range ceiling for integer FFI arguments (PyO3 converts to Rust i64;
#: a larger exact int would raise OverflowError at the boundary — the gates
#: refuse it pre-call instead so the oracle serves).
_I64_MAX: Final[int] = 2**63 - 1

#: The PBO combination cap — mirrors the kernel-side
#: ``stats::pbo::PBO_MAX_COMBINATIONS`` (a larger split enumeration would
#: allocate an unbounded logits Vec under ``panic = "abort"``; the seam
#: refuses FIRST so the interruptible Python oracle serves the shape —
#: behavior identical to flag-OFF). Production is C(16, 8) = 12,870.
_PBO_MAX_COMBINATIONS: Final[int] = 2_000_000


def _f64_bits(value: float) -> bytes:
    """The burn-in compare unit for float results: the raw IEEE-754 bit
    pattern (``struct.pack('<d', …)``) — signed zeros and NaN payloads
    distinguished exactly (the rust_metrics burn-in convention). Callers
    pass values already proven exact-type ``float`` by the result gates."""
    return struct.pack("<d", value)


def _numeric_as_f64(fn: str, value: float, *, arg: str) -> float | None:
    """The exact-TYPE + value-exactness numeric-argument law, generalized
    from the cpcv binding's inline ``min_sharpe`` gate (refuter rounds
    1-4 of PR-1 — see :func:`cpcv_pass_count_via_kernel`'s in-body notes;
    the unit suite cross-pins the two equal). Annotated ``float`` but the
    PEP-484 numeric tower + hostile callers deliver ints / subclasses /
    foreign objects at runtime:

    * exact-type ``float``: engages unless NaN (NaN refuses to the oracle
      — for a threshold slot the oracle's ``> NaN`` semantics must own the
      call; for a value slot the refusal is a conservative, disclosed
      narrowing — ON≡OFF either way);
    * exact ``int`` / ``bool``: engages IFF ``float()`` is value-exact
      (OverflowError caught into the refusal, never raised);
    * ANY other type: ``arg_foreign_type`` refusal — the oracle applies the
      ORIGINAL object's own arithmetic/comparison semantics identically on
      both arms (exact value AND exception parity).

    Every warn context rides the total, bounded helpers (the round-3/4
    context-totality law — enforced by the unit suite's AST pin).
    """
    vt: type[object] = type(value)
    if vt is float:
        if math.isnan(value):
            warn_fallback_once(fn, "arg_inexact", arg=arg, value=_inexact_threshold_context(value))
            return None
        return value
    if vt is int or vt is bool:
        try:
            as_f = float(value)
        except OverflowError:
            warn_fallback_once(fn, "arg_inexact", arg=arg, value=_inexact_threshold_context(value))
            return None
        if as_f != value:
            warn_fallback_once(fn, "arg_inexact", arg=arg, value=_inexact_threshold_context(value))
            return None
        return as_f
    warn_fallback_once(fn, "arg_foreign_type", arg=arg, value_type=_bounded_text(_type_name(vt)))
    return None


def _count_as_i64(fn: str, value: object, *, arg: str, minimum: int) -> int | None:
    """Exact-type integer-argument gate for the i64 FFI slots.

    Engages ONLY an exact ``int`` (or ``bool`` — the unsubclassable exact
    int subtype, the cpcv precedent) inside ``[minimum, 2**63 - 1]``; every
    other shape — a float-typed count, an int subclass, a foreign object,
    an out-of-range value — refuses to the oracle, which applies the
    original object's own semantics on both arms.
    """
    vt: type[object] = type(value)
    if not isinstance(value, int) or (vt is not int and vt is not bool):
        # ``isinstance`` narrows for the type checker; the exact-type clause
        # is the actual law (an int SUBCLASS is still refused — its foreign
        # dunders must keep owning the call on both arms).
        warn_fallback_once(
            fn, "arg_foreign_type", arg=arg, value_type=_bounded_text(_type_name(vt))
        )
        return None
    as_int = int(value)  # exact int/bool proven above — normalizes bool → int
    if as_int < minimum or as_int > _I64_MAX:
        warn_fallback_once(fn, "arg_unsupported", arg=arg, value=_safe_context(value))
        return None
    return as_int


def _flat_exact_floats(fn: str, rows: Sequence[Sequence[float]], *, arg: str) -> list[float] | None:
    """Row-major flatten of a moments matrix, gated exact-type ``float``.

    ``_block_moments`` rows are exact floats by construction (``0.0 +``
    accumulation), so the production path always engages; a hostile
    float-SUBCLASS entry (whose reflected ops could have poisoned the
    accumulation into arbitrary objects) refuses to the oracle. O(S·N) —
    the same order as the FFI copy it precedes, disclosed on the binding.
    """
    flat: list[float] = []
    for row in rows:
        for value in row:
            vt: type[object] = type(value)
            if vt is not float:
                warn_fallback_once(
                    fn, "arg_foreign_type", arg=arg, value_type=_bounded_text(_type_name(vt))
                )
                return None
            flat.append(value)
    return flat


def _refuse_result_once(fn: str, result: object) -> None:
    """Log ``rust_stats.kernel_result_refused`` (ERROR) once per function.

    The output-side refuse-poison logging shared by the PR-2 bindings (the
    cpcv binding carries its own inline copy — append-only discipline).
    The refused object is KERNEL-controlled — its context rides the total,
    bounded :func:`_safe_context`.

    DEC-RUST-STRICT-NO-FALLBACK-001: refuses under ``BRUTEX_RUST_STRICT`` —
    CLASS B (a kernel BUG), never exempt. Strict-OFF is byte-identical.
    """
    refuse_if_strict(
        "brutex.metrics.rust_stats",
        "kernel_result_refused",
        fn=fn,
        result=_safe_context(result),
    )
    key = f"{fn}:kernel_result_refused"
    if key not in _OBSERVABILITY_MEMO:
        _OBSERVABILITY_MEMO.add(key)
        _LOG.error("rust_stats.kernel_result_refused", fn=fn, result=_safe_context(result))


def _valid_float_result(result: object) -> TypeGuard[float]:
    """Exact-type ``float`` result gate (the output-side twin of the input
    law): a byzantine wheel returning a float SUBCLASS is refused before
    any comparison can dispatch its foreign ordering; the real kernels
    return exact PyO3 ``f64`` → ``float``. NO range/finiteness demand —
    a legitimately-NaN result (e.g. a NaN-variance SR_0) must flow, and
    wrongness inside the domain is the burn-in compare's layer."""
    return type(result) is float


#: The PBO kernel's result arity — exactly ``(overfit_count, logits)``.
_PBO_RESULT_ARITY: Final = 2


def _valid_pbo_result(result: object, n_total: int) -> TypeGuard[tuple[int, list[float]]]:
    """Domain validation of the PBO kernel's ``(overfit_count, logits)``.

    Exact-type ``tuple`` of length 2; ``overfit_count`` an exact ``int`` in
    ``[0, n_total]``; ``logits`` an exact ``list`` of exactly ``n_total``
    exact-type ``float`` entries. O(n_total) — the same order as the fold
    work it validates.
    """
    if type(result) is not tuple or len(result) != _PBO_RESULT_ARITY:
        return False
    count, logits = result
    if type(count) is not int or not 0 <= count <= n_total:
        return False
    if type(logits) is not list or len(logits) != n_total:
        return False
    return all(type(value) is float for value in logits)


def psr_via_kernel(
    observed_sharpe: float,
    benchmark_sharpe: float,
    sample_length: int,
    skew: float,
    kurtosis: float,
    *,
    oracle: Callable[[], float],
) -> float | None:
    """Run the Rust PSR kernel; ``None`` ⇒ use the Python oracle.

    The ONE FFI call site for the ``psr`` binding
    (``fno_math.stats_psr`` — DEC-METRICS-RUST-STATS-002). Inputs arrive
    PRE-VALIDATED by :func:`brutex.metrics.deflated_sharpe.
    probabilistic_sharpe_ratio` (finite floats, ``sample_length >= 2``,
    radicand > 0 — every raise stays Python-side); *oracle* is the
    extracted ``_psr_tail`` closed over the same inputs. The four numeric
    arguments ride :func:`_numeric_as_f64` and ``sample_length`` rides
    :func:`_count_as_i64` — a foreign-typed / inexact argument refuses to
    the oracle (original-object semantics on both arms). Kernel errors
    take the NARROW catch (ValueError / TypeError / OverflowError);
    ``MemoryError`` and every other type propagate (the global-stop law).
    Burn-in compares the raw f64 BIT PATTERN (:func:`_f64_bits` — signed
    zeros/NaN payloads exact); a mismatch ships the ORACLE value and
    latches this function's kernel.

    FFI cost (disclosed): five scalars per call — nil.
    """
    fn = "psr"
    if kernel_disabled(fn):
        warn_fallback_once(fn, "kernel_disabled")
        return None
    observed_f = _numeric_as_f64(fn, observed_sharpe, arg="observed_sharpe")
    if observed_f is None:
        return None
    benchmark_f = _numeric_as_f64(fn, benchmark_sharpe, arg="benchmark_sharpe")
    if benchmark_f is None:
        return None
    skew_f = _numeric_as_f64(fn, skew, arg="skew")
    if skew_f is None:
        return None
    kurtosis_f = _numeric_as_f64(fn, kurtosis, arg="kurtosis")
    if kurtosis_f is None:
        return None
    length_i = _count_as_i64(fn, sample_length, arg="sample_length", minimum=2)
    if length_i is None:
        return None
    kernel = _resolve_kernel(fn, "stats_psr")
    if kernel is None:
        return None
    try:
        result = kernel(observed_f, benchmark_f, length_i, skew_f, kurtosis_f)
    except (ValueError, TypeError, OverflowError) as exc:
        warn_fallback_once(fn, "kernel_error", error=_safe_context(exc))
        return None
    if not _valid_float_result(result):
        _refuse_result_once(fn, result)
        return None
    if _self_check_pending(fn):
        expected = oracle()
        if _f64_bits(result) != _f64_bits(expected):
            disable_kernel(
                fn,
                "self_check_mismatch",
                got=_safe_context(result),
                want=_safe_context(expected),
            )
            return expected
        _consume_self_check_sample(fn)
    _log_engaged_once(fn)
    return result


def expected_max_sharpe_via_kernel(
    n_trials: int,
    trial_sharpe_variance: float,
    *,
    oracle: Callable[[], float],
) -> float | None:
    """Run the Rust expected-max-Sharpe (``SR_0``) kernel; ``None`` ⇒ Python.

    The ONE FFI call site for the ``sr_max`` binding
    (``fno_math.stats_expected_max_sharpe``). The caller dispatches only
    AFTER its ``n_trials == 1`` early return, so ``minimum=2`` here; a NaN
    ``trial_sharpe_variance`` (which legitimately flows to a NaN ``SR_0``
    on both engines) is a conservative :func:`_numeric_as_f64` refusal to
    the oracle — a disclosed narrowing, ON≡OFF either way. A kernel
    ``ValueError`` from the AS241 raise domain (``1 - 1/N`` rounding to
    1.0 at astronomical ``N``) refuses to the oracle, which raises the
    identical ``StatisticsError`` — exception parity by construction.

    FFI cost (disclosed): two scalars per call — nil.
    """
    fn = "sr_max"
    if kernel_disabled(fn):
        warn_fallback_once(fn, "kernel_disabled")
        return None
    trials_i = _count_as_i64(fn, n_trials, arg="n_trials", minimum=2)
    if trials_i is None:
        return None
    variance_f = _numeric_as_f64(fn, trial_sharpe_variance, arg="trial_sharpe_variance")
    if variance_f is None:
        return None
    kernel = _resolve_kernel(fn, "stats_expected_max_sharpe")
    if kernel is None:
        return None
    try:
        result = kernel(trials_i, variance_f)
    except (ValueError, TypeError, OverflowError) as exc:
        warn_fallback_once(fn, "kernel_error", error=_safe_context(exc))
        return None
    if not _valid_float_result(result):
        _refuse_result_once(fn, result)
        return None
    if _self_check_pending(fn):
        expected = oracle()
        if _f64_bits(result) != _f64_bits(expected):
            disable_kernel(
                fn,
                "self_check_mismatch",
                got=_safe_context(result),
                want=_safe_context(expected),
            )
            return expected
        _consume_self_check_sample(fn)
    _log_engaged_once(fn)
    return result


def pbo_cscv_via_kernel(
    block_sum: Sequence[Sequence[float]],
    block_sumsq: Sequence[Sequence[float]],
    *,
    n_splits: int,
    n_strat: int,
    is_count: int,
    n_total: int,
    evaluator_is_default: bool,
    oracle: Callable[[], tuple[int, list[float]]],
) -> tuple[int, list[float]] | None:
    """Run the Rust CSCV logit-loop kernel; ``None`` ⇒ use the Python oracle.

    The ONE FFI call site for the ``pbo`` binding
    (``fno_math.stats_pbo_cscv``). *block_sum*/*block_sumsq* are the
    per-block moment rows ``_block_moments`` computed (Python stays the
    moments producer — the cpcv seam-boundary ruling); *n_total* is
    ``math.comb(n_splits, n_splits // 2)`` for domain validation and the
    combination cap; *evaluator_is_default* is ``evaluator is
    sharpe_from_moments`` — a CUSTOM evaluator callable refuses
    (``custom_evaluator``) because only the default statistic is
    transcribed. Beyond :data:`_PBO_MAX_COMBINATIONS` the seam refuses
    pre-call (``combination_cap``) so the kernel's logits allocation can
    never abort under ``panic = "abort"`` — the interruptible oracle
    serves, behavior identical to flag-OFF. Burn-in compares the overfit
    COUNT (``==``) and EVERY logit's raw bit pattern.

    FFI cost (disclosed): two ``O(n_splits · n_strat)`` flatten+copy passes
    in, one ``O(n_total)`` logits list out — bounded by the fold work
    either engine performs.
    """
    fn = "pbo"
    if kernel_disabled(fn):
        warn_fallback_once(fn, "kernel_disabled")
        return None
    if not evaluator_is_default:
        warn_fallback_once(fn, "custom_evaluator")
        return None
    if n_total > _PBO_MAX_COMBINATIONS:
        warn_fallback_once(fn, "combination_cap")
        return None
    splits_i = _count_as_i64(fn, n_splits, arg="n_splits", minimum=2)
    if splits_i is None:
        return None
    strat_i = _count_as_i64(fn, n_strat, arg="n_strat", minimum=2)
    if strat_i is None:
        return None
    count_i = _count_as_i64(fn, is_count, arg="is_count", minimum=1)
    if count_i is None:
        return None
    flat_sum = _flat_exact_floats(fn, block_sum, arg="block_sum")
    if flat_sum is None:
        return None
    flat_sumsq = _flat_exact_floats(fn, block_sumsq, arg="block_sumsq")
    if flat_sumsq is None:
        return None
    kernel = _resolve_kernel(fn, "stats_pbo_cscv")
    if kernel is None:
        return None
    try:
        result = kernel(flat_sum, flat_sumsq, splits_i, strat_i, count_i)
    except (ValueError, TypeError, OverflowError) as exc:
        warn_fallback_once(fn, "kernel_error", error=_safe_context(exc))
        return None
    if not _valid_pbo_result(result, n_total):
        _refuse_result_once(fn, result)
        return None
    if _self_check_pending(fn):
        expected = oracle()
        got_count, got_logits = result
        expected_count, expected_logits = expected
        mismatch = (
            got_count != expected_count
            or len(got_logits) != len(expected_logits)
            or any(
                _f64_bits(got) != _f64_bits(want)
                for got, want in zip(got_logits, expected_logits, strict=True)
            )
        )
        if mismatch:
            disable_kernel(
                fn,
                "self_check_mismatch",
                got=_safe_context(result),
                want=_safe_context(expected),
            )
            return expected
        _consume_self_check_sample(fn)
    _log_engaged_once(fn)
    return result


def wfo_pass_count_via_kernel(
    returns: npt.NDArray[np.float64],
    test_starts: Sequence[int],
    test_stops: Sequence[int],
    min_sharpe: float,
    *,
    n_walks: int,
    oracle: Callable[[], int],
) -> int | None:
    """Run the Rust WFO pass-count kernel; ``None`` ⇒ use the Python oracle.

    The ONE FFI call site for the ``wfo`` binding
    (``fno_math.stats_wfo_pass_count`` — the numpy-pairwise REPLICATED
    per-walk Sharpe, the mod.rs claim taxonomy). *returns* is the
    already-validated float64 array ``wfo_pass_rate`` built;
    *test_starts*/*test_stops* are the walks' half-open test windows
    (internal ints from ``walk_forward_splits``); *min_sharpe* rides the
    SAME exact-type threshold law as the cpcv binding via
    :func:`_numeric_as_f64` (NaN → the oracle's ``> NaN`` all-non-pass
    semantics, foreign types → original-object comparison semantics on
    both arms); *n_walks* bounds the result domain (the reused
    :func:`_valid_pass_count` law).

    FFI cost (disclosed): one ``O(n)`` returns copy + two ``O(walks)``
    bound lists per call — bounded by the ``O(Σ window sizes)`` Sharpe
    work either engine performs.
    """
    fn = "wfo"
    if kernel_disabled(fn):
        warn_fallback_once(fn, "kernel_disabled")
        return None
    min_sharpe_f = _numeric_as_f64(fn, min_sharpe, arg="min_sharpe")
    if min_sharpe_f is None:
        return None
    kernel = _resolve_kernel(fn, "stats_wfo_pass_count")
    if kernel is None:
        return None
    try:
        result = kernel(
            [float(x) for x in returns],
            [int(x) for x in test_starts],
            [int(x) for x in test_stops],
            min_sharpe_f,
        )
    except (ValueError, TypeError, OverflowError) as exc:
        warn_fallback_once(fn, "kernel_error", error=_safe_context(exc))
        return None
    if not _valid_pass_count(result, n_walks):
        _refuse_result_once(fn, result)
        return None
    if _self_check_pending(fn):
        expected = oracle()
        if result != expected:
            disable_kernel(
                fn,
                "self_check_mismatch",
                got=_safe_context(result),
                want=_safe_context(expected),
            )
            return expected
        _consume_self_check_sample(fn)
    _log_engaged_once(fn)
    return result


__all__ = [
    "SELF_CHECK_SAMPLES",
    "STATS_RUST_DEFAULT",
    "STATS_RUST_ENV",
    "cpcv_pass_count_via_kernel",
    "disable_kernel",
    "expected_max_sharpe_via_kernel",
    "kernel_disabled",
    "pbo_cscv_via_kernel",
    "psr_via_kernel",
    "reset_observability_memo_for_tests",
    "reset_self_check_for_tests",
    "stats_rust_enabled",
    "warn_fallback_once",
    "wfo_pass_count_via_kernel",
]
