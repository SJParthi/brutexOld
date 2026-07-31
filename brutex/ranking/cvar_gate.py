"""CVaR (expected-shortfall) TAIL-RISK rejection gate — DEC-RANK-CVAR-TAIL-GATE-001.

An opt-in, DEFAULT-OFF rejection gate over one candidate's per-trade P&L
list in **int paisa**: a candidate whose expected tail loss (the mean of its
worst ``alpha_bps``-fraction of trades) is strictly worse than an operator
threshold is REJECTED before ranking. The whole decision is INTEGER-EXACT —
no float ever participates — so the optional Rust kernel
(``fno_math.stats_cvar_tail_fold``) is bit-identical to the Python oracle by
construction.

## The law (locked; the Rust kernel transcribes it token-for-token)

Given ``n = len(pnls)`` (per-trade ``pnl_paisa`` ints) and the config's
``alpha_bps`` (basis points of the distribution; 500 = the worst 5%) and
``threshold_paisa`` (the maximum acceptable expected tail LOSS per trade,
a positive int):

* ``n == 0`` → the gate ABSTAINS (never a reject on no evidence);
* ``k = max(1, ceil(n * alpha_bps / 10_000))`` — integer ceiling division
  ``(n * alpha_bps + 9_999) // 10_000``;
* the worst-k tail is the multiset of the ``k`` smallest P&Ls;
  ``var_paisa`` is the k-th smallest (the α-quantile VaR bound) and
  ``tail_sum_paisa`` the exact sum of the worst k;
* REJECT iff ``tail_sum_paisa < -threshold_paisa * k`` — the mean tail loss
  is STRICTLY worse than the threshold (exact integer cross-product, no
  division).

## Seam + activation posture (ships INERT)

The gate config rides ``SweepGridConfig.cvar_gate`` /
``SweepRunConfig.cvar_gate`` (default ``None`` = gate OFF — every default
sweep is byte-identical, frozen-golden-proven) and is applied at the ONE
chunked eval spine (``brutex.engine.sweep_run._iter_mask_metrics``) — the
only production point where each mask's per-trade P&L list exists; the
aggregate :class:`~brutex.metrics.compute.ResultMetrics` that reaches
:func:`brutex.ranking.passes_gates` carries no trade list, which is why this
gate CANNOT live in :data:`~brutex.ranking.GATE_REASON_ORDER` (the
overfit-gate precedent: that vocabulary is the per-``ResultMetrics`` door
set; this gate is counted at its own seam under the :data:`GATE_CVAR_TAIL`
wire token, never fabricated per-row). An ACTIVE config folds into the
sweep identity default-elided
(:func:`brutex.engine.sweep_identity._gate_config_fragment` — the
DEC-SWEEP-TUPLE-DIGEST-GATE-IDENTITY-001 law), so a gated store address can
never alias an ungated one while the default digest stays byte-stable.
ACTIVATION (a non-``None`` production config) requires its own DEC +
operator sign-off on ``docs/measurements/cvar_gate_measurement.md``.

## The optional Rust kernel — ``BRUTEX_RANK_CVAR_RUST`` (default ON per
## DEC-RUST-DEFAULT-ON-WAVE-001, operator directive 2026-07-23)

With the GATE active, the fold runs on the Rust kernel by default;
``BRUTEX_RANK_CVAR_RUST=0`` is the explicit kill switch back to the
pure-Python oracle (tristate via the COMMON
:func:`brutex.engine.sweep_lookahead.env_flag_tristate`, imported LAZILY —
ranking is imported BY the engine, so a module-scope ranking→engine import
would cycle; the :mod:`brutex.metrics.rust_stats` precedent). Wheel
absent/stale, a kernel fault (``ValueError``/``TypeError``/
``OverflowError``), or a shape-invalid result each degrade to the oracle
with one warn-once ``cvar_gate.fallback`` per reason; the first
:data:`CVAR_SELF_CHECK_SAMPLES` kernel-served folds per process are
bit-compared against the oracle and a mismatch SHIPS THE ORACLE TRIPLE +
latches the kernel OFF for the process (``cvar_gate.kernel_autoreverted``
ERROR — the rust_stats burn-in/autorevert template; the latch + memo are
the same documented no-global-mutable-state exception). ``MemoryError``
re-raises BARE (OOM is a global stop, never swallowed into a fallback).

## Honest envelope (Golden Rule #10)

* O(n_trades) per candidate — the kernel is an average-O(n) quickselect,
  the oracle an O(n log n) sort; BOTH are flat in the CANDIDATE count
  (bench-pinned), neither is O(1)-total.
* The parity claim is scoped to int-element inputs (the production
  ``ClosedTrade.pnl_paisa`` contract). A non-int element makes the kernel's
  ``Vec<i64>`` extraction raise ``TypeError`` → the oracle serves the call
  with Python's own semantics — results never depend on wheel state.
* An int element past i64 range likewise degrades (``OverflowError`` →
  oracle, whose Python ints are unbounded); unreachable from the engine
  spine (trade P&L magnitudes sit far inside i64).
* The gate judges the SIGNAL-ONLY per-trade P&L stream (Track 1 — gross of
  the structurally-absent costs; CLAUDE.md §2 Rule 3), exactly the stream
  every ranked metric already folds.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, TypeGuard, cast

import structlog

if TYPE_CHECKING:  # pragma: no cover - import only for static typing
    from collections.abc import Callable, Sequence

_LOG = structlog.get_logger(__name__)

#: The env flag for the OPTIONAL Rust fold kernel (never the gate itself —
#: the gate is armed only by a non-``None`` config). Truthy → kernel ON,
#: falsy → OFF, unset/whitespace-empty → :data:`CVAR_RUST_DEFAULT`, any
#: other value → OFF with one warn-once ``flag_unrecognised``.
CVAR_RUST_ENV: Final[str] = "BRUTEX_RANK_CVAR_RUST"

#: What :func:`cvar_rust_enabled` returns when the flag is unset — DEFAULT
#: ON per DEC-RUST-DEFAULT-ON-WAVE-001 (operator directive 2026-07-23; the
#: ADR-0017 precedent); ``BRUTEX_RANK_CVAR_RUST=0`` stays the explicit
#: kill switch and the revert is this one constant.
CVAR_RUST_DEFAULT: Final[bool] = True

#: The STABLE wire token this gate's rejections are counted/logged under
#: (the ``sweep.cvar_gate`` event + the measurement report). Deliberately
#: NOT a member of :data:`brutex.ranking.GATE_REASON_ORDER` — that
#: vocabulary is the per-``ResultMetrics`` door set :func:`gate_reasons`
#: can actually judge, and this gate needs the per-trade P&L list that
#: never reaches it (the overfit-gate precedent, module docstring).
GATE_CVAR_TAIL: Final[str] = "cvar_tail"

#: The tail-fraction domain bound (basis points): 1 = the worst 0.01%
#: (floored to ≥ 1 trade), 10_000 = the whole distribution.
ALPHA_BPS_MAX: Final[int] = 10_000

#: Burn-in folds per process bit-compared kernel-vs-oracle before the
#: kernel runs unchecked (the rust_stats K=16 cadence — the fold runs once
#: per kept mask, so 16 costs one extra O(n log n) sort each).
CVAR_SELF_CHECK_SAMPLES: Final[int] = 16

#: The one function key for the warn-once memo / latch (single-kernel seam).
_FN: Final[str] = "cvar"

#: The kernel result triple's arity — ``(k, var_paisa, tail_sum_paisa)``.
_FOLD_RESULT_ARITY: Final[int] = 3

#: Memo key suffix for the engaged-once INFO (namespaced apart from reasons).
_ENGAGED_KEY: Final[str] = "__engaged__"

#: The facade name every strict refusal from this module carries.
_STRICT_FACADE: Final[str] = "brutex.ranking.cvar_gate"

#: CLASS-C fall-back reasons — the Rust arm still SERVES (or was never
#: offered the call), so refusing would crash a legitimate path
#: (DEC-RUST-STRICT-NO-FALLBACK-001 §"Scope note"):
#:
#: * ``flag_engine_unavailable`` — the engine package (whose ``__init__``
#:   pulls the ``trading_core`` wheel) is unimportable, so the flag
#:   CLASSIFIER is unavailable. The resolver then returns
#:   :data:`CVAR_RUST_DEFAULT` = ``True``: the RUST arm still serves. This is
#:   an advisory, not a fall-back — the exact
#:   :mod:`brutex.metrics.rust_stats` precedent, whose
#:   ``_STRICT_EXEMPT_REASONS`` carries the same token.
_STRICT_EXEMPT_REASONS: Final[frozenset[str]] = frozenset({"flag_engine_unavailable"})

#: OVERLOADED reason tokens — HELD, not classified (the honest posture the
#: DEC mandates for a token that is genuinely two classes at once). Strict
#: mode does NOT refuse these, because refusing would crash the CLASS-C half:
#:
#: * ``kernel_error`` — emitted by ONE
#:   ``except (ValueError, TypeError, OverflowError)`` around
#:   ``kernel(list(pnls), alpha_bps)``, which catches BOTH:
#:
#:   - **CLASS A/B**: the kernel's own structural ``Err`` arms, mapped to
#:     ``PyValueError`` — ``rust/fno-math/src/stats/cvar.rs:72`` (empty
#:     ``pnls``) and ``:79`` (``alpha_bps`` outside ``[1, 10_000]``). Rust ran
#:     and refused; a quiet Python re-run papers that over.
#:   - **CLASS C**: PyO3's ``Vec<i64>`` / ``u32`` extraction failing at the
#:     FFI edge before ANY kernel code runs — a non-int element
#:     (``TypeError``) or an int past i64 range (``OverflowError``), i.e. the
#:     signature ``stats_cvar_tail_fold(pnls: Vec<i64>, alpha_bps: u32)`` at
#:     ``rust/fno-math/src/stats/cvar.rs:114``. This module's own honest
#:     envelope documents that arm as the intended Python-serves path
#:     ("A non-int element makes the kernel's ``Vec<i64>`` extraction raise
#:     ``TypeError`` → the oracle serves the call with Python's own
#:     semantics").
#:
#:   Splitting the token (a distinct ``kernel_input_refused`` for the
#:   extraction half, e.g. by pre-gating element types Python-side) is the
#:   real fix; guessing a class here would either crash a legal call or hide
#:   a genuine kernel refusal.
_STRICT_UNRESOLVED_REASONS: Final[frozenset[str]] = frozenset({"kernel_error"})

#: Once-per-process-per-reason fallback memo + the engaged-once latch.
#: LOG-VOLUME state ONLY — results never depend on it (the documented
#: no-global-mutable-state exception, module docstring).
_OBSERVABILITY_MEMO: Final[set[str]] = set()

#: Burn-in samples still to run this process (the same documented exception).
_SELF_CHECK_REMAINING: Final[dict[str, int]] = {_FN: CVAR_SELF_CHECK_SAMPLES}

#: The autorevert latch: reason once the kernel is disabled for the process.
_KERNEL_DISABLED_REASON: Final[dict[str, str]] = {}


@dataclass(frozen=True, slots=True)
class CvarTailFold:
    """The integer-exact worst-k tail fold for ONE candidate's trade P&Ls.

    Attributes
    ----------
    n
        Trade count the fold ran over (``>= 1`` — the empty list never
        folds; :func:`cvar_rejects` abstains before folding).
    k
        The tail size: ``max(1, ceil(n * alpha_bps / 10_000))``.
    var_paisa
        The k-th smallest P&L — the α-quantile Value-at-Risk bound (the
        LARGEST member of the worst-k tail).
    tail_sum_paisa
        The exact integer sum of the worst k P&Ls (CVaR × k). The REJECT
        decision reads ONLY this and ``k`` — never a divided mean.
    """

    n: int
    k: int
    var_paisa: int
    tail_sum_paisa: int

    @property
    def tail_mean_x100_paisa(self) -> int:
        """DISPLAY-ONLY mean tail P&L in centi-paisa (floor division).

        ``(tail_sum_paisa * 100) // k`` — exact integer floor toward −∞;
        a losing tail reads negative. The DECISION never consults this
        (it compares the undivided ``tail_sum_paisa`` cross-product), so
        the floor's rounding can never flip a verdict.
        """
        return (self.tail_sum_paisa * 100) // self.k


@dataclass(frozen=True, slots=True)
class CvarGateConfig:
    """Opt-in CVaR tail-risk gate config (``None`` anywhere = gate OFF).

    Attributes
    ----------
    threshold_paisa
        The maximum acceptable expected tail LOSS per trade, in int paisa
        (``> 0``): a candidate whose mean worst-``alpha_bps`` trade loses
        STRICTLY more than this is rejected.
    alpha_bps
        The tail fraction in basis points of the trade distribution
        (default 500 = the worst 5%); ``[1, 10_000]``.

    Raises
    ------
    ValueError
        On a non-positive / non-exact-int ``threshold_paisa`` or an
        out-of-range / non-exact-int ``alpha_bps`` (``bool`` is refused —
        it is int-typed but never an operator intent).
    """

    threshold_paisa: int
    alpha_bps: int = 500

    def __post_init__(self) -> None:
        for name in ("threshold_paisa", "alpha_bps"):
            value = getattr(self, name)
            # Exact-type int (bool excluded): the identity fragment + the
            # FFI + the integer law all assume plain ints; a float/bool/
            # Decimal here is a config bug, refused loudly at construction.
            if type(value) is not int:
                raise ValueError(
                    f"CvarGateConfig.{name} must be an exact int, got {type(value).__name__}"
                )
        if self.threshold_paisa <= 0:
            raise ValueError(
                f"CvarGateConfig.threshold_paisa must be > 0 paisa, got {self.threshold_paisa}"
            )
        if not 1 <= self.alpha_bps <= ALPHA_BPS_MAX:
            raise ValueError(
                f"CvarGateConfig.alpha_bps must be in [1, {ALPHA_BPS_MAX}], got {self.alpha_bps}"
            )


def cvar_rust_enabled() -> bool:
    """Resolve the OPTIONAL-kernel flag over :data:`CVAR_RUST_DEFAULT`.

    The exact :func:`brutex.metrics.rust_stats.stats_rust_enabled` law:
    truthy → ``True``, falsy → ``False``, unset/whitespace-empty → the
    default (ON per DEC-RUST-DEFAULT-ON-WAVE-001), unrecognised → OFF with
    one warn-once
    ``flag_unrecognised``; the tristate vocabulary is the COMMON
    :func:`~brutex.engine.sweep_lookahead.env_flag_tristate`, imported
    LAZILY (ranking is imported BY the engine — a module-scope back-import
    would cycle), and an engine-import failure (absent/stale trading_core
    wheel) degrades a SET flag to the default with one warn-once
    ``flag_engine_unavailable`` — flag resolution never raises.
    """
    raw = os.environ.get(CVAR_RUST_ENV, "")
    if not raw.strip():
        # Short-circuit BEFORE the engine import: the unset default arm
        # never touches brutex.engine (whose __init__ pulls the
        # trading_core wheel) — the rust_stats precedent.
        return CVAR_RUST_DEFAULT
    try:
        from brutex.engine.sweep_lookahead import env_flag_tristate
    except ImportError:
        _warn_fallback_once("flag_engine_unavailable")
        return CVAR_RUST_DEFAULT
    verdict = env_flag_tristate(raw)
    if verdict is not None:
        return verdict
    _warn_fallback_once("flag_unrecognised", value=repr(raw)[:200])
    return False


def cvar_kernel_disabled() -> bool:
    """``True`` iff the burn-in autorevert latch has disabled the kernel."""
    return _FN in _KERNEL_DISABLED_REASON


def reset_cvar_rust_for_tests(*, remaining: int | None = None) -> None:
    """Reset the burn-in budget + autorevert latch (test isolation ONLY).

    ``remaining=None`` restores the fresh-process
    :data:`CVAR_SELF_CHECK_SAMPLES` budget; ``remaining=0`` puts the seam
    in the post-burn-in steady state (bench use). Never called from
    production paths.
    """
    _SELF_CHECK_REMAINING[_FN] = CVAR_SELF_CHECK_SAMPLES if remaining is None else remaining
    _KERNEL_DISABLED_REASON.pop(_FN, None)


def reset_cvar_observability_for_tests() -> None:
    """Clear the warn-once / engaged-once memo (test isolation ONLY)."""
    _OBSERVABILITY_MEMO.clear()


def _refuse_if_strict(reason: str, **context: object) -> None:
    """Refuse a Python fall-back under strict mode, honouring the two sets.

    Function-local import: :mod:`brutex.ranking` is imported BY the engine, so
    a module-scope ``brutex.config`` import here would risk the same cycle
    class the flag resolver already dodges (module docstring).
    """
    from brutex.config.rust_strict import refuse_if_strict

    if reason in _STRICT_EXEMPT_REASONS or reason in _STRICT_UNRESOLVED_REASONS:
        return
    refuse_if_strict(_STRICT_FACADE, reason, fn=_FN, **context)


def _warn_fallback_once(reason: str, **context: object) -> None:
    """Log ``cvar_gate.fallback`` (WARNING) once per process per *reason*.

    The fold runs once per kept mask (thousands per tuple), so a sticky
    reason (a stale wheel, a latched kernel) would flood the logs; repeats
    fall back silently. The fallback itself is bit-exact regardless — the
    Python oracle serves the call.

    Choke point #1 of two. Strict mode (DEC-RUST-STRICT-NO-FALLBACK-001)
    refuses BEFORE the memo write, so an already-warned sticky reason cannot
    slip through the early return.
    """
    _refuse_if_strict(reason, **context)
    key = f"{_FN}:{reason}"
    if key in _OBSERVABILITY_MEMO:
        return
    _OBSERVABILITY_MEMO.add(key)
    _LOG.warning("cvar_gate.fallback", fn=_FN, reason=reason, **context)


def _disable_kernel(reason: str, **context: object) -> None:
    """Latch the kernel OFF for the process (burn-in mismatch autorevert).

    Choke point #2 of two. Strict mode refuses BEFORE the latch write, which
    makes the latched ``kernel_disabled`` early-return in
    :func:`_cvar_tail_fold_rust` structurally unreachable — the wiring is
    complete by construction, not by sampling.
    """
    _refuse_if_strict(reason, **context)
    if _FN not in _KERNEL_DISABLED_REASON:
        _KERNEL_DISABLED_REASON[_FN] = reason
        _LOG.error("cvar_gate.kernel_autoreverted", fn=_FN, reason=reason, **context)


def _log_engaged_once() -> None:
    """Log ``cvar_gate.engaged`` (INFO) on the kernel's first success."""
    key = f"{_FN}:{_ENGAGED_KEY}"
    if key not in _OBSERVABILITY_MEMO:
        _OBSERVABILITY_MEMO.add(key)
        _LOG.info("cvar_gate.engaged", fn=_FN)


def _resolve_kernel() -> Callable[..., object] | None:
    """Return ``fno_math.stats_cvar_tail_fold``, or ``None`` with a warn-once.

    Lazy single-import-site (the rust_stats convention): a wheel-absent
    environment degrades with one ``wheel_missing``; a wheel that imports
    but predates the symbol degrades with ``wheel_stale`` (defense-in-depth
    — the fno_math shim's own freshness handshake normally raises first).
    """
    try:
        import fno_math
    except ImportError:
        _warn_fallback_once("wheel_missing")
        return None
    kernel: object = getattr(fno_math, "stats_cvar_tail_fold", None)
    if not callable(kernel):
        _warn_fallback_once("wheel_stale", attr="stats_cvar_tail_fold")
        return None
    return kernel


def _cvar_tail_fold_python(pnls: Sequence[int], alpha_bps: int) -> CvarTailFold:
    """The VERBATIM pure-Python tail fold — the LIVE parity oracle.

    Retained forever (the operator exception): the Rust kernel transcribes
    THIS law token-for-token and every degrade path serves through it.
    ``sorted`` is O(n log n); the multiset of the k smallest values is
    permutation-invariant, so ties never matter (the kernel's quickselect
    picks the same value multiset by construction).
    """
    n = len(pnls)
    k = max(1, (n * alpha_bps + 9_999) // 10_000)
    worst = sorted(pnls)[:k]
    return CvarTailFold(n=n, k=k, var_paisa=worst[-1], tail_sum_paisa=sum(worst))


def _valid_fold_result(result: object, n: int) -> TypeGuard[tuple[int, int, int]]:
    """Cheap refuse-poison gate on a kernel triple — exact ints, sane shape.

    ``(k, var, tail_sum)`` with each element EXACTLY ``int`` (a bool or an
    int subclass from a byzantine wheel is refused before any comparison
    can dispatch foreign dunders — the rust_stats output-side law),
    ``1 <= k <= n``, and the O(1) CVaR≤VaR sanity ``tail_sum <= var * k``
    (the tail mean can never exceed the tail's largest member). A result
    inside this domain that is nonetheless WRONG is the burn-in compare's
    layer to catch.
    """
    if not (isinstance(result, tuple) and len(result) == _FOLD_RESULT_ARITY):
        return False
    if any(type(v) is not int for v in result):
        return False
    # ``typing.cast`` is a runtime no-op: the exact-type loop above PROVED
    # every element is a plain ``int`` (bool excluded), so the narrowed
    # comparisons below dispatch only int arithmetic.
    k, var_paisa, tail_sum = cast("tuple[int, int, int]", result)
    return 1 <= k <= n and tail_sum <= var_paisa * k


def _fold_via_kernel(pnls: Sequence[int], alpha_bps: int) -> CvarTailFold | None:
    """Run the Rust fold; ``None`` ⇒ the caller serves the Python oracle.

    Degrade ladder (each arm warn-once, results bit-identical either way):
    latched kernel → wheel missing/stale → a kernel fault (``ValueError`` /
    ``TypeError`` / ``OverflowError`` — the kernel's structural arms + PyO3
    conversion failures; any OTHER exception type propagates and
    ``MemoryError`` re-raises bare) → a shape-invalid result (refused
    LOUDLY, ``cvar_gate.kernel_result_refused`` ERROR). During burn-in a
    compare mismatch ships the ORACLE triple and latches the kernel.
    """
    if cvar_kernel_disabled():
        _warn_fallback_once("kernel_disabled")
        return None
    kernel = _resolve_kernel()
    if kernel is None:
        return None
    try:
        result = kernel(list(pnls), alpha_bps)
    except (ValueError, TypeError, OverflowError) as exc:
        _warn_fallback_once("kernel_error", error=repr(exc)[:200])
        return None
    if not _valid_fold_result(result, len(pnls)):
        # CLASS B under strict mode — Rust RAN and returned a structurally
        # refused shape; the Python re-run must not paper over it. Refuse
        # BEFORE the memo write so a repeat cannot slip past.
        _refuse_if_strict("kernel_result_refused", result=repr(result)[:200])
        key = f"{_FN}:kernel_result_refused"
        if key not in _OBSERVABILITY_MEMO:
            _OBSERVABILITY_MEMO.add(key)
            _LOG.error("cvar_gate.kernel_result_refused", fn=_FN, result=repr(result)[:200])
        return None
    k, var_paisa, tail_sum = result
    fold = CvarTailFold(n=len(pnls), k=k, var_paisa=var_paisa, tail_sum_paisa=tail_sum)
    remaining = _SELF_CHECK_REMAINING.get(_FN, 0)
    if remaining > 0:
        expected = _cvar_tail_fold_python(pnls, alpha_bps)
        if fold != expected:
            _disable_kernel(
                "self_check_mismatch",
                got=repr((k, var_paisa, tail_sum))[:200],
                want=repr((expected.k, expected.var_paisa, expected.tail_sum_paisa))[:200],
            )
            return expected
        _SELF_CHECK_REMAINING[_FN] = remaining - 1
    _log_engaged_once()
    return fold


def cvar_tail_fold(pnls: Sequence[int], alpha_bps: int) -> CvarTailFold:
    """The worst-k tail fold (module-docstring law) — Rust-routed when opted in.

    Raises ``ValueError`` on an EMPTY list (callers abstain first —
    :func:`cvar_rejects` handles ``n == 0``) or an out-of-range
    *alpha_bps*; both are validated HERE, Python-side, before any FFI, so
    the kernel's twin arms are defense-in-depth only.
    """
    if len(pnls) < 1:
        raise ValueError("cvar_tail_fold: pnls must be non-empty (the gate abstains on n == 0)")
    if not 1 <= alpha_bps <= ALPHA_BPS_MAX:
        raise ValueError(
            f"cvar_tail_fold: alpha_bps must be in [1, {ALPHA_BPS_MAX}], got {alpha_bps}"
        )
    if cvar_rust_enabled():
        fold = _fold_via_kernel(pnls, alpha_bps)
        if fold is not None:
            return fold
    return _cvar_tail_fold_python(pnls, alpha_bps)


def cvar_rejects(pnls: Sequence[int], cfg: CvarGateConfig) -> bool:
    """The gate VERDICT: ``True`` iff the candidate's tail is too fat.

    ``REJECT iff tail_sum_paisa < -threshold_paisa * k`` — the mean tail
    loss strictly worse than the threshold, decided on exact integers (no
    division, no float). An EMPTY *pnls* ABSTAINS (``False`` — a
    zero-trade candidate carries no tail evidence; the min-trades door
    rejects it downstream on its own law).
    """
    if not pnls:
        return False
    fold = cvar_tail_fold(pnls, cfg.alpha_bps)
    return fold.tail_sum_paisa < -cfg.threshold_paisa * fold.k


__all__ = [
    "ALPHA_BPS_MAX",
    "CVAR_RUST_DEFAULT",
    "CVAR_RUST_ENV",
    "CVAR_SELF_CHECK_SAMPLES",
    "GATE_CVAR_TAIL",
    "CvarGateConfig",
    "CvarTailFold",
    "cvar_kernel_disabled",
    "cvar_rejects",
    "cvar_rust_enabled",
    "cvar_tail_fold",
    "reset_cvar_observability_for_tests",
    "reset_cvar_rust_for_tests",
]
