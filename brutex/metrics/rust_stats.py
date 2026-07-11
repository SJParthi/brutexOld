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
* The kernel engages only when ``float(min_sharpe)`` is VALUE-exact — an
  inexact conversion (an ``int`` past 2^53, or NaN by the ``!=`` test) is a
  pre-burn-in domain refusal to the oracle (``min_sharpe_inexact``): the
  oracle's int↔float comparison is EXACT where the FFI rounds, and nothing
  is normalized on the OFF arm (it stays byte-identical for every input).
"""

from __future__ import annotations

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
        # (whose ``__init__`` pulls the trading_core wheel) — the leaf
        # metrics modules stay wheel-free-loadable under the default.
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
    # nor a typo'd kill switch silently follow a future ON default.
    warn_fallback_once(_FLAG_FN, "flag_unrecognised", value=raw)
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
    the ORACLE value (the binding's contract).
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

    ``O(1)``: exactly ``int`` (``bool`` excluded — ``True`` is an ``int``
    subtype a buggy wrapper could leak) within ``[0, n_total]``. A result
    inside this domain that is nonetheless WRONG is the burn-in compare's +
    the parity/golden suites' layer to catch.
    """
    return isinstance(result, int) and not isinstance(result, bool) and 0 <= result <= n_total


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
    domain-invalid value, or when ``float(min_sharpe)`` is not value-exact
    (``min_sharpe_inexact`` — the FFI would round what the oracle compares
    EXACTLY; a NaN threshold conservatively takes this arm too, result-
    identical — see the in-body note); the caller then serves the call
    through the bit-exact Python oracle, so wheel state can never change
    results or error behaviour (within the documented exception envelope —
    module docstring). The catch is NARROW (``ValueError`` — the kernel's
    own structural arms; ``TypeError`` / ``OverflowError`` — PyO3
    argument-conversion failures); any OTHER exception type PROPAGATES and
    ``MemoryError`` re-raises BARE (OOM is a GLOBAL stop — never swallowed
    into a fallback). During burn-in a compare MISMATCH ships the ORACLE
    value and latches this function's kernel (module docstring).

    FFI cost (disclosed): three ``O(n_groups)`` list conversions per call —
    48 scalars at the production S=16; negligible next to the
    ``O(C(S, k)·k)`` fold work either engine performs.
    """
    fn = "cpcv"
    if kernel_disabled(fn):
        warn_fallback_once(fn, "kernel_disabled")
        return None
    min_sharpe_f = float(min_sharpe)
    if min_sharpe_f != min_sharpe:
        # Exactness DOMAIN GUARD (refuter-round-1 confirmed defect): the FFI
        # necessarily rounds ``min_sharpe`` through ``float()``, while the
        # Python oracle compares ``sharpe > min_sharpe`` on the ORIGINAL
        # object — and CPython's mixed-type int↔float comparison is EXACT.
        # A type-legal ``int`` threshold past 2^53 whose float image rounds
        # across a fold Sharpe would make the two engines count DIFFERENT
        # passes (ON≠OFF) and false-fire the burn-in autorevert against a
        # FAITHFUL kernel. The kernel therefore engages ONLY when the float
        # conversion is provably value-exact by ``==``; anything else is a
        # pre-burn-in domain refusal to the oracle (no sample consumed, no
        # latch risk — the OFF arm stays byte-identical: nothing normalized).
        # NaN note (documented choice): ``float('nan') != float('nan')`` is
        # True, so a NaN threshold conservatively takes this refusal too —
        # SAFE and result-identical (``sharpe > nan`` is False on BOTH
        # engines, every fold a non-pass, unit-pinned); a degenerate
        # all-non-pass threshold has no kernel win worth a special case.
        warn_fallback_once(fn, "min_sharpe_inexact", min_sharpe=repr(min_sharpe))
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
        # MemoryError — a GLOBAL stop) propagates.
        warn_fallback_once(fn, "kernel_error", error=repr(exc))
        return None
    if not _valid_pass_count(result, n_total):
        # A domain-invalid result is a kernel BUG: refuse it LOUDLY (never
        # silently adopted) and serve the call from the Python oracle.
        key = f"{fn}:kernel_result_refused"
        if key not in _OBSERVABILITY_MEMO:
            _OBSERVABILITY_MEMO.add(key)
            _LOG.error(
                "rust_stats.kernel_result_refused",
                fn=fn,
                result=repr(result),
                n_total=n_total,
            )
        return None
    if _self_check_pending(fn):
        expected = oracle()
        if result != expected:
            # SHIP THE ORACLE VALUE — correctness first — and latch this
            # function's kernel for the rest of the process.
            disable_kernel(fn, "self_check_mismatch", got=result, want=expected)
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
