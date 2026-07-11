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

## The flag — ``BRUTEX_STATS_RUST`` over ``STATS_RUST_DEFAULT``

:func:`stats_rust_enabled` reads the env per call through the COMMON
:func:`brutex.engine.sweep_lookahead.env_flag_tristate` classifier (imported
LAZILY inside the function body so this metrics module never creates a
metrics→engine import-time cycle; CLAUDE.md §11 COMMON — the truthy/falsy
vocabulary is never copied). Resolution: a truthy value forces ON, a falsy
value forces OFF, unset/whitespace-empty falls to :data:`STATS_RUST_DEFAULT`
(**False — default-OFF**; the flip to ON-by-default is a SEPARATE later ADR,
the DEC-SWEEP-RUST-METRICS-001 → ADR-0017 precedent), and any OTHER value
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
  ``MemoryError`` re-raises BARE — the same global-stop carve-out as the
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
from typing import TYPE_CHECKING, Final, TypeGuard

import structlog

if TYPE_CHECKING:  # pragma: no cover - import only for static typing
    from collections.abc import Callable, Mapping

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
#: the env flag is unset or whitespace-empty. ``False`` = DEFAULT-OFF — every
#: caller stays on the verbatim pure-Python path unless the operator opts in
#: with ``BRUTEX_STATS_RUST=1``; the flip to ON-by-default is a SEPARATE
#: later ADR (the ADR-0017 precedent), licensed only by the PR-6 evidence
#: bar of the slice design.
STATS_RUST_DEFAULT: Final[bool] = False

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
    * unset or whitespace-empty → :data:`STATS_RUST_DEFAULT` (``False`` —
      the one-constant flip lever);
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
        # production-default OFF arm free of the ``brutex.engine`` package
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


def disable_kernel(fn: str, reason: str, **context: object) -> None:
    """Latch *fn*'s kernel OFF for the remainder of the process (autorevert).

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
    """
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
    is the burn-in compare's + the parity/golden suites' layer to catch.
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
    * ``MemoryError`` re-raises BARE — the documented global-stop
      carve-out, the same law as the kernel call's narrow catch (OOM is
      never swallowed into a log placeholder; the one deliberate ON-only
      raise this helper keeps, inside the module's disclosed exception
      envelope).
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


__all__ = [
    "SELF_CHECK_SAMPLES",
    "STATS_RUST_DEFAULT",
    "STATS_RUST_ENV",
    "cpcv_pass_count_via_kernel",
    "disable_kernel",
    "kernel_disabled",
    "reset_observability_memo_for_tests",
    "reset_self_check_for_tests",
    "stats_rust_enabled",
    "warn_fallback_once",
]
