"""Flag-gated Rust selection seam — the multi-key persist kernels' binding.

LANE W1 (DEC-RANK-PARETO-PERSIST-001 / DEC-RANK-QUOTA-PERSIST-1000-001):
the Python side of the born-Rust selection kernels
(``rust/fno-math/src/rank_select.rs``) — env-flag resolution, the 16-call
burn-in bit-compare against the Python oracle
(:mod:`brutex.ranking.multi_select`), the ship-the-ORACLE autorevert latch,
the refuse-poison result validation, and the warn-once fallback logging.
The leaf shape is the :mod:`brutex.engine.rust_metrics` template
(DEC-SWEEP-EXIT-INDEX-FASTPATH-001) copied deliberately — one reviewed
pattern, not a new invention.

## The flag — ``BRUTEX_RANK_RUST_SELECT`` over ``RANK_RUST_SELECT_DEFAULT``

Same vocabulary + policy as ``BRUTEX_SWEEP_RUST_METRICS`` (the COMMON
:func:`~brutex.engine.sweep_lookahead.env_flag_tristate` classifier —
imported, never copied): a ``_TRUTHY`` value forces the kernels ON, a
``_FALSY`` value forces OFF (the operator kill switch), unset/empty falls to
:data:`RANK_RUST_SELECT_DEFAULT` (the ONE-constant flip lever — see its
docstring for the current state + the 2026-07-18 greenfield flip-ON ruling;
the revert is that one constant, and ``BRUTEX_RANK_RUST_SELECT=0`` is the
per-process kill switch), and an unrecognised value fails SAFE to OFF with
one warn-once event.

## Burn-in + autorevert (the honesty net behind a default-ON kernel)

For the first :data:`SELF_CHECK_SAMPLES` kernel-served calls per FUNCTION
per process, the seam ALSO runs the Python oracle and compares the results
EXACTLY (index lists — integers, so ``==`` is the bit-compare). A mismatch
SHIPS THE ORACLE RESULT and latches the kernels OFF for the process (one
``rust_rank_select.kernel_autoreverted`` ERROR; later calls short-circuit to
the oracle with a warn-once fallback). Bit-identity is ENFORCED by that
16-call burn-in bit-compare plus the refuse-poison shape gates — a kernel
that turns membership-wrong only AFTER the burn-in exhausts is the standard
accepted burn-in envelope (the DEC-SWEEP-RUST-METRICS-DEFAULT-ON-001
precedent, same as rust_metrics / greeks_batch / feed_rust), guarded by the
cargo tests + the CI parity suites. Order-wrong kernel output is additionally
harmless — the composer consumes the index lists set-based (and re-sorts the
frontier itself).

## Honest envelope (Golden Rule #10)

* The frontier is O(n²) worst case in BOTH arms (the definition demands full
  pairwise exclusion — module docs in the kernel + oracle); the quota select
  is O(A·n log M) — nothing here is O(1); the win is a constant factor on a
  once-per-tuple persist-time path, NOT a hot-loop claim.
* The burn-in doubles the cost of the first 16 kernel calls per function —
  bounded, amortised-nil.
* ``MemoryError`` re-raises BARE (OOM is a GLOBAL stop,
  DEC-LAKE-WRITE-HALT-001); the fallback catch is NARROW
  (``ValueError`` / ``TypeError`` / ``OverflowError``).
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Final, TypeGuard

import structlog

from brutex.config.rust_strict import refuse_if_strict
from brutex.engine.sweep_lookahead import env_flag_tristate
from brutex.ranking.multi_select import (
    PARETO_EPS_LOSING_TRADES,
    PARETO_EPS_MAX_DD_PCT,
    PARETO_EPS_NET_PNL_PAISA,
    PARETO_EPS_WIN_RATE,
    SelectionInputs,
    UnionSelection,
    axis_topm_indices,
    compute_union_selection,
    pareto_frontier_indices,
)

if TYPE_CHECKING:  # pragma: no cover - import only for static typing
    from collections.abc import Callable, Sequence

_LOG = structlog.get_logger("brutex.engine.rust_rank_select")

#: The env flag over :data:`RANK_RUST_SELECT_DEFAULT` (module docstring).
RANK_RUST_SELECT_ENV: Final[str] = "BRUTEX_RANK_RUST_SELECT"

#: The ONE default-arm constant (the flip lever) — **ON** since the LANE-W1
#: final commit per the 2026-07-18 greenfield flip-ON ruling
#: (coordinator-relayed over the operator's born-Rust/greenfield decrees —
#: "no backtests ever run"; the DEC-SWEEP-RUST-METRICS-DEFAULT-ON-001
#: one-constant-flip precedent; disclosed in MIGRATION.md #2059/#2060 + the
#: PR body). Revert = this one constant; ``BRUTEX_RANK_RUST_SELECT=0`` is
#: the per-process operator kill switch. Selection output is bit-identical
#: either arm (the 16-call burn-in bit-compare + ship-the-ORACLE autorevert
#: at runtime; the parity suites in CI) — the flip changes WHICH arm serves,
#: never what is selected.
RANK_RUST_SELECT_DEFAULT: Final[bool] = True

#: Kernel-served calls bit-compared against the oracle per function per
#: process (the burn-in self-check).
SELF_CHECK_SAMPLES: Final[int] = 16

#: Remaining burn-in samples per function (mutable cells — the documented
#: no-global-mutable-state exception, the rust_metrics template).
_SELF_CHECK_REMAINING: Final[dict[str, int]] = {
    "frontier": SELF_CHECK_SAMPLES,
    "quota": SELF_CHECK_SAMPLES,
}

#: The autorevert latch (mismatch reason once disabled, else ``None``).
_KERNEL_DISABLED_REASON: Final[list[str | None]] = [None]

#: Once-per-process-per-reason fallback memo (log-volume state only).
_OBSERVABILITY_MEMO: Final[set[str]] = set()

#: Memo key for the engaged-once INFO.
_ENGAGED_KEY: Final[str] = "__engaged__"

#: u64 limb mask for the u128 mask → two-limb FFI split.
_U64_MASK: Final[int] = (1 << 64) - 1

#: The quota kernel's result arity (one index list per quota axis).
_QUOTA_AXES_ARITY: Final[int] = 5

#: The quota result's axis order (the ``axis_topm_indices`` tuple order) —
#: diagnostic labels for the quota autorevert payload (review round 1: the
#: quota mismatch must name the first differing axis + both index prefixes,
#: the frontier arm's kernel=/oracle= discipline).
_QUOTA_AXIS_NAMES: Final[tuple[str, ...]] = (
    "composite",
    "dd",
    "losing_trades",
    "win_rate",
    "expectancy",
)

#: Bounded diagnostic prefix length for autorevert payloads (both arms).
_AUTOREVERT_PREFIX: Final[int] = 8


def rank_rust_select_enabled() -> bool:
    """Resolve the flag over :data:`RANK_RUST_SELECT_DEFAULT`.

    ``_TRUTHY`` → ``True``; ``_FALSY`` → ``False`` (the kill switch);
    unset/whitespace-empty → the default; anything unrecognised → ``False``
    with one warn-once ``flag_unrecognised`` event (a typo'd kill switch must
    still KILL under a default-ON kernel — the rust_metrics law).
    """
    raw = os.environ.get(RANK_RUST_SELECT_ENV, "")
    verdict = env_flag_tristate(raw)
    if verdict is not None:
        return verdict
    if raw.strip():
        warn_fallback_once("flag_unrecognised", value=raw)
        return False
    return RANK_RUST_SELECT_DEFAULT


def kernel_disabled() -> bool:
    """``True`` iff the autorevert latch has disabled the kernels this process."""
    return _KERNEL_DISABLED_REASON[0] is not None


def disable_kernel(reason: str, **context: object) -> None:
    """Refuse under strict mode; else latch the kernels OFF for the process.

    DEC-RUST-STRICT-NO-FALLBACK-001. The reasons routed here
    (``frontier_mismatch`` / ``quota_mismatch``) are CLASS B — the kernel's
    selection DISAGREED with the oracle's. None is class C, so none is exempt.
    The refusal fires BEFORE the latch is written, so under strict mode the
    latch is never set and the ``kernel_disabled`` early-returns become
    structurally unreachable. Strict-OFF is byte-identical to the prior
    behaviour.

    First call logs ``rust_rank_select.kernel_autoreverted`` (ERROR); repeats
    are no-ops. The mismatching call ships the ORACLE result either way.
    """
    refuse_if_strict("brutex.engine.rust_rank_select", reason, **context)
    if _KERNEL_DISABLED_REASON[0] is None:
        _KERNEL_DISABLED_REASON[0] = reason
        _LOG.error("rust_rank_select.kernel_autoreverted", reason=reason, **context)


def reset_self_check_for_tests(*, remaining: int = SELF_CHECK_SAMPLES) -> None:
    """Reset the burn-in budget + autorevert latch (test isolation ONLY)."""
    for key in _SELF_CHECK_REMAINING:
        _SELF_CHECK_REMAINING[key] = remaining
    _KERNEL_DISABLED_REASON[0] = None


def reset_observability_memo_for_tests() -> None:
    """Clear the warn-once / engaged-once memo (test isolation ONLY)."""
    _OBSERVABILITY_MEMO.clear()


def warn_fallback_once(reason: str, **context: object) -> None:
    """Refuse under strict mode; else log ``rust_rank_select.fallback`` warn-once.

    DEC-RUST-STRICT-NO-FALLBACK-001. Reasons routed here are CLASS A
    (``flag_unrecognised``, ``wheel_missing``, ``wheel_stale``,
    ``kernel_error``) or CLASS B (``kernel_disabled`` — a prior mismatch
    latched the kernels off). None is class C: both selection kernels accept
    the full survivor domain, so there is no out-of-envelope arm to exempt.
    Strict-OFF is byte-identical to the prior behaviour.
    """
    refuse_if_strict("brutex.engine.rust_rank_select", reason, **context)
    if reason in _OBSERVABILITY_MEMO:
        return
    _OBSERVABILITY_MEMO.add(reason)
    _LOG.warning("rust_rank_select.fallback", reason=reason, **context)


def _mark_engaged() -> None:
    if _ENGAGED_KEY not in _OBSERVABILITY_MEMO:
        _OBSERVABILITY_MEMO.add(_ENGAGED_KEY)
        _LOG.info("rust_rank_select.engaged")


def _resolve_kernel(name: str) -> Callable[..., object] | None:
    """Resolve one ``fno_math`` kernel, or ``None`` with a warn-once."""
    try:
        import fno_math
    except ImportError:
        warn_fallback_once("wheel_missing")
        return None
    kernel: Callable[..., object] | None = getattr(fno_math, name, None)
    if kernel is None:
        warn_fallback_once("wheel_stale", symbol=name)
        return None
    return kernel


def _valid_index_list(result: object, n: int, *, ascending: bool) -> TypeGuard[list[int]]:
    """Refuse-poison gate: a kernel index list must be in-range ints, no dups.

    ``ascending=True`` additionally demands strictly-ascending order (the
    frontier contract). O(len) — never re-runs the oracle. A ``TypeGuard`` so
    the callers' post-gate ``result`` narrows to ``list[int]`` (no ignores).
    """
    if not isinstance(result, list):
        return False
    previous = -1
    seen: set[int] = set()
    for value in result:
        if isinstance(value, bool) or not isinstance(value, int):
            return False
        if not 0 <= value < n:
            return False
        if ascending:
            if value <= previous:
                return False
            previous = value
        elif value in seen:
            return False
        else:
            seen.add(value)
    return True


def _consume_burn_in(name: str) -> bool:
    """``True`` iff this call must ALSO run the oracle (budget consumed on pass)."""
    return _SELF_CHECK_REMAINING[name] > 0


def _burn_in_passed(name: str) -> None:
    if _SELF_CHECK_REMAINING[name] > 0:
        _SELF_CHECK_REMAINING[name] -= 1


def frontier_select(
    net_pnl_paisa: Sequence[int],
    win_rate: Sequence[float],
    max_dd_pct: Sequence[float],
    losing_trades: Sequence[int],
) -> list[int]:
    """The ε-Pareto frontier at the W1 ε defaults — kernel-or-oracle.

    Matches :type:`brutex.ranking.multi_select.FrontierFn`. Bit-identical
    output either arm: the kernel result is burn-in-compared and any
    kernel-side failure serves the oracle. ``MemoryError`` re-raises BARE.
    """

    def oracle() -> list[int]:
        return pareto_frontier_indices(net_pnl_paisa, win_rate, max_dd_pct, losing_trades)

    if not rank_rust_select_enabled():
        return oracle()
    if kernel_disabled():
        warn_fallback_once("kernel_disabled")
        return oracle()
    kernel = _resolve_kernel("rank_pareto_frontier")
    if kernel is None:
        return oracle()
    try:
        result = kernel(
            list(net_pnl_paisa),
            list(win_rate),
            list(max_dd_pct),
            list(losing_trades),
            PARETO_EPS_NET_PNL_PAISA,
            PARETO_EPS_WIN_RATE,
            PARETO_EPS_MAX_DD_PCT,
            PARETO_EPS_LOSING_TRADES,
        )
    except (ValueError, TypeError, OverflowError) as exc:
        warn_fallback_once("kernel_error", function="frontier", error=repr(exc))
        return oracle()
    if not _valid_index_list(result, len(net_pnl_paisa), ascending=True):
        # DEC-RUST-STRICT-NO-FALLBACK-001: this refuse-poison arm logs its own
        # ERROR rather than routing through ``warn_fallback_once``, so it needs
        # the strict gate explicitly. CLASS B (the kernel returned a
        # domain-invalid index list) — refused, never exempt.
        refuse_if_strict(
            "brutex.engine.rust_rank_select", "kernel_result_refused", function="frontier"
        )
        if "kernel_result_refused_frontier" not in _OBSERVABILITY_MEMO:
            _OBSERVABILITY_MEMO.add("kernel_result_refused_frontier")
            _LOG.error("rust_rank_select.kernel_result_refused", function="frontier")
        return oracle()
    kernel_indices = [int(value) for value in result]
    if _consume_burn_in("frontier"):
        expected = oracle()
        if kernel_indices != expected:
            disable_kernel(
                "frontier_mismatch",
                kernel=kernel_indices[:_AUTOREVERT_PREFIX],
                oracle=expected[:_AUTOREVERT_PREFIX],
            )
            return expected
        _burn_in_passed("frontier")
    _mark_engaged()
    return kernel_indices


def axis_quota_select(
    m: int,
    composite: Sequence[float],
    masks: Sequence[int],
    max_dd_pct: Sequence[float],
    losing_trades: Sequence[int],
    win_rate: Sequence[float],
    expectancy_paisa: Sequence[int],
) -> tuple[list[int], list[int], list[int], list[int], list[int]]:
    """The per-axis bounded top-M select — kernel-or-oracle.

    Matches :type:`brutex.ranking.multi_select.QuotaFn`. The u128 masks are
    split into two u64 limb columns at the FFI edge (ints only cross).
    """

    def oracle() -> tuple[list[int], list[int], list[int], list[int], list[int]]:
        return axis_topm_indices(
            m, composite, masks, max_dd_pct, losing_trades, win_rate, expectancy_paisa
        )

    if not rank_rust_select_enabled():
        return oracle()
    if kernel_disabled():
        warn_fallback_once("kernel_disabled")
        return oracle()
    kernel = _resolve_kernel("rank_axis_topm")
    if kernel is None:
        return oracle()
    try:
        result = kernel(
            m,
            list(composite),
            [mask & _U64_MASK for mask in masks],
            [mask >> 64 for mask in masks],
            list(max_dd_pct),
            list(losing_trades),
            list(win_rate),
            list(expectancy_paisa),
        )
    except (ValueError, TypeError, OverflowError) as exc:
        warn_fallback_once("kernel_error", function="quota", error=repr(exc))
        return oracle()
    n = len(composite)
    if not (
        isinstance(result, tuple)
        and len(result) == _QUOTA_AXES_ARITY
        and all(
            _valid_index_list(axis, n, ascending=False) and len(axis) <= max(m, 0)
            for axis in result
        )
    ):
        # DEC-RUST-STRICT-NO-FALLBACK-001: same explicit gate as the frontier
        # arm above — CLASS B, refused, never exempt.
        refuse_if_strict(
            "brutex.engine.rust_rank_select", "kernel_result_refused", function="quota"
        )
        if "kernel_result_refused_quota" not in _OBSERVABILITY_MEMO:
            _OBSERVABILITY_MEMO.add("kernel_result_refused_quota")
            _LOG.error("rust_rank_select.kernel_result_refused", function="quota")
        return oracle()
    kernel_lists = tuple([int(value) for value in axis] for axis in result)
    if _consume_burn_in("quota"):
        expected = oracle()
        if kernel_lists != expected:
            # Review round 1: carry the frontier arm's diagnostic discipline —
            # name the FIRST differing axis + both bounded index prefixes.
            axis_pos = next(
                index
                for index, (kernel_axis, oracle_axis) in enumerate(
                    zip(kernel_lists, expected, strict=True)
                )
                if kernel_axis != oracle_axis
            )
            disable_kernel(
                "quota_mismatch",
                axis=_QUOTA_AXIS_NAMES[axis_pos],
                kernel=kernel_lists[axis_pos][:_AUTOREVERT_PREFIX],
                oracle=expected[axis_pos][:_AUTOREVERT_PREFIX],
            )
            return expected
        _burn_in_passed("quota")
    _mark_engaged()
    return kernel_lists  # type: ignore[return-value]


def union_selection(inputs: SelectionInputs, *, top_k: int | None) -> UnionSelection:
    """The persist union + tags with the burn-in-checked kernels injected.

    The ONE production entry both persist arms (batch ``sweep_run`` +
    bounded ``sweep_stream``) call — the composer itself is the shared pure
    :func:`brutex.ranking.multi_select.compute_union_selection`; this
    binding only substitutes the kernel-backed frontier/quota selectors.
    """
    return compute_union_selection(
        inputs,
        top_k=top_k,
        frontier_fn=frontier_select,
        quota_fn=axis_quota_select,
    )


__all__ = [
    "RANK_RUST_SELECT_DEFAULT",
    "RANK_RUST_SELECT_ENV",
    "SELF_CHECK_SAMPLES",
    "axis_quota_select",
    "disable_kernel",
    "frontier_select",
    "kernel_disabled",
    "rank_rust_select_enabled",
    "reset_observability_memo_for_tests",
    "reset_self_check_for_tests",
    "union_selection",
    "warn_fallback_once",
]
