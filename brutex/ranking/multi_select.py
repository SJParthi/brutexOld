"""Multi-key persist selection — ε-Pareto frontier + per-axis quotas + union.

LANE W1 of the winning-completeness P0 ruling (operator, 2026-07-18 —
"Approve all dude okay"; DEC-RANK-PARETO-PERSIST-001 /
DEC-RANK-QUOTA-PERSIST-1000-001). This module is the **Python parity
ORACLE** of the born-Rust kernels in ``rust/fno-math/src/rank_select.rs``
(the BORN-RUST law: new selection mechanics land in Rust first; the oracle
here is the live burn-in reference the seam
:mod:`brutex.engine.rust_rank_select` bit-compares against) — plus the pure
union/tagging composer both arms share.

## The selection law (item 1 + item 2)

Over ONE tuple's gate-passing ranked pool (the ``rank_combos`` output, in
``(-composite, mask)`` order), the PERSIST SET is the dedup-union of:

* (a) the composite top-``top_k`` under the existing ``(-composite, mask)``
  law — index prefix ``[0, top_k)`` of the composite-ordered pool;
* (b) the reserved per-axis quota: the top-:data:`AXIS_QUOTA_M` under each of
  the FIVE quota axes ``{composite DESC, max_drawdown_pct ASC,
  losing_trades ASC, win_rate DESC, expectancy_paisa DESC}``, non-composite
  axes tie-broken ``(axis value, -composite, mask)``;
* (c) the additive-ε Pareto frontier over the FOUR W1 axes
  ``{net_pnl_paisa MAX, win_rate MAX, max_drawdown_pct MIN,
  losing_trades MIN}``, capped at :data:`PARETO_FRONTIER_CAP` best by
  ``(-composite, mask)`` with the drop counted + warned (never silent).

Every selected row is tagged ``selection_basis`` (comma-joined tokens from
:data:`BASIS_ORDER`, canonical fixed order — never insertion order) and
``pareto_member`` (frontier membership) — computed ONCE per pool, BEFORE the
single append per run_id (the ``results_store.py`` run-identity conflict law).

## ε-dominance (the pinned W1 definition)

``x`` ε-dominates ``y`` iff BOTH: (within) on EVERY axis ``x`` is
better-or-equal-within-ε — MAX axes ``x + eps >= y``, MIN axes
``x - eps <= y``; AND (strict) on AT LEAST ONE axis ``x`` beats ``y`` by
MORE than ε — MAX ``x > y + eps``, MIN ``x < y - eps``. The EXPRESSION FORMS
are load-bearing (``x + eps >= y`` and ``x >= y - eps`` round differently in
IEEE-754) and are pinned identical in the Rust kernel. At ε = 0 this reduces
to CLASSICAL Pareto dominance. HONEST properties (Golden Rule #10, verified
by construction + property-pinned): the relation is ANTISYMMETRIC but NOT
transitive and NOT monotone in ε (a larger ε can both create and destroy
dominations — frontier(ε₂) ⊄ frontier(ε₁) in general, so NO containment
property is asserted); an EMPTY frontier on a non-empty pool is tolerated
(the union always contains the composite top-K).

## W1 defaults (disclosed — standing in for un-ruled operator constants)

The design map pinned the MECHANISMS but left the ε values, the quota M, the
frontier bound and the tie-breaks unspecified; the values below are the
LANE-W1 defaults, each a named constant, disclosed in MIGRATION.md
#2059-#2060 + the PR body for operator re-ruling. The pinned 5th Pareto axis
``sl_hit_rate`` does NOT exist yet (it is LANE W2's item-4 column); the axis
tables here are one-row-extensible and the deferral is LOUD (ledger + PR).

## Honest envelope (Golden Rule #10)

* The frontier oracle is **O(n²) worst case** (full pairwise exclusion —
  ε-dominance is not transitive, so incremental frontier maintenance would
  be unsound); the quota oracle is O(A·n log n); nothing here is O(1).
* This module is a stdlib-only leaf (structlog aside) with ZERO engine
  imports — the same cycle constraint as :mod:`brutex.ranking.presets`.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import structlog

if TYPE_CHECKING:  # pragma: no cover - import only for static typing
    from collections.abc import Callable, Mapping, Sequence

_LOG = structlog.get_logger("brutex.ranking.multi_select")

__all__ = [
    "AXIS_QUOTA_M",
    "BASIS_COMPOSITE",
    "BASIS_DD",
    "BASIS_EXPECTANCY",
    "BASIS_FRONTIER",
    "BASIS_LOSING_TRADES",
    "BASIS_ORDER",
    "BASIS_WIN_RATE",
    "DEFAULT_PERSIST_TOP_K",
    "PARETO_EPS_LOSING_TRADES",
    "PARETO_EPS_MAX_DD_PCT",
    "PARETO_EPS_NET_PNL_PAISA",
    "PARETO_EPS_WIN_RATE",
    "PARETO_FRONTIER_CAP",
    "QUOTA_AXES",
    "SelectionInputs",
    "UnionSelection",
    "axis_topm_indices",
    "compute_union_selection",
    "join_basis_tokens",
    "pareto_frontier_indices",
    "union_persist_bound",
]

#: The REAL persist top-K default (item 2 — "mint the real 1000 default"):
#: the spec's Stage-4 filter #1 production sizing, previously help-text prose
#: over a coded ``--top`` default of 20 (``cli.py`` /
#: ``dashboard/sweep_runner.py`` consume THIS constant — import, never copy;
#: CLAUDE.md §11 COMMON). DISCLOSED: 20→1000 re-keys every production tuple's
#: STORE ADDRESS (``top_k`` folds into ``_tuple_digest`` when non-None) —
#: sanctioned under the greenfield ruling ("no backtests ever run"),
#: MIGRATION.md #2060.
DEFAULT_PERSIST_TOP_K: Final[int] = 1000

#: Per-axis reserved quota M (item 2) — W1 default, disclosed.
AXIS_QUOTA_M: Final[int] = 200

#: Frontier size bound (item 1 — "bounded frontier"; the bound VALUE is the
#: W1 default, disclosed). Over-cap keeps the best by ``(-composite, mask)``
#: with the drop counted + warned.
PARETO_FRONTIER_CAP: Final[int] = 500

#: The quota-select axis count (composite · dd · losing_trades · win_rate ·
#: expectancy) — the ONE arity the quota kernels return and the persist
#: bound below multiplies by.
QUOTA_AXES: Final[int] = 5

# ── The W1-default per-axis ε values (disclosed operator-constant stand-ins) ──
#: net P&L tolerance: ₹10 (1,000 paisa) — two combos within ₹10 net P&L are
#: "equal" on the P&L axis.
PARETO_EPS_NET_PNL_PAISA: Final[int] = 1_000
#: Win-rate tolerance: 0.5 percentage points (win_rate is a fraction).
PARETO_EPS_WIN_RATE: Final[float] = 0.005
#: Max-drawdown tolerance: 0.25 percentage points (max_drawdown_pct is ×100).
PARETO_EPS_MAX_DD_PCT: Final[float] = 0.25
#: Losing-trades tolerance: exact (integer axis, ε = 0).
PARETO_EPS_LOSING_TRADES: Final[int] = 0

# ── The selection_basis token vocabulary, in CANONICAL join order ────────────
BASIS_COMPOSITE: Final[str] = "composite"
BASIS_DD: Final[str] = "dd"
BASIS_LOSING_TRADES: Final[str] = "losing_trades"
BASIS_WIN_RATE: Final[str] = "win_rate"
BASIS_EXPECTANCY: Final[str] = "expectancy"
BASIS_FRONTIER: Final[str] = "frontier"
#: The FIXED vocabulary order ``selection_basis`` tokens are joined in —
#: never insertion order (a multi-basis row's tag is deterministic
#: regardless of which basis selected it first).
BASIS_ORDER: Final[tuple[str, ...]] = (
    BASIS_COMPOSITE,
    BASIS_DD,
    BASIS_LOSING_TRADES,
    BASIS_WIN_RATE,
    BASIS_EXPECTANCY,
    BASIS_FRONTIER,
)


def join_basis_tokens(tokens: Sequence[str]) -> str:
    """Join basis *tokens* in the canonical :data:`BASIS_ORDER`, deduped.

    Raises ``ValueError`` on a token outside the vocabulary — a poisoned tag
    must never persist (the store validates against the same vocabulary).
    """
    unknown = sorted(set(tokens) - set(BASIS_ORDER))
    if unknown:
        raise ValueError(f"multi_select: unknown selection_basis token(s) {unknown}")
    present = set(tokens)
    return ",".join(token for token in BASIS_ORDER if token in present)


def union_persist_bound(
    top_k: int,
    *,
    quota_m: int = AXIS_QUOTA_M,
    frontier_cap: int = PARETO_FRONTIER_CAP,
) -> int:
    """The MAXIMUM row count the persist union can legitimately select.

    The composite prefix (≤ *top_k*) plus the union-only extras, which are a
    subset of the :data:`QUOTA_AXES` quota lists (each ≤ *quota_m*) united
    with the capped frontier (≤ *frontier_cap*) — so
    ``top_k + QUOTA_AXES·quota_m + frontier_cap`` is a sound upper bound on
    any legitimate store's row count. The ONE law ``sweep-verify``'s
    ``rows_exceed_top_k`` inflation check consumes (review round 1 gate
    re-run: the pre-W1 ``rows > top_k`` check would false-flag every
    legitimate union store as #650-corrupt). Deliberately LOOSE, never
    exact — the exact union size depends on the pool's axis overlaps; the
    inflation signature only needs "impossible legitimately".
    """
    return top_k + QUOTA_AXES * quota_m + frontier_cap


def pareto_frontier_indices(
    net_pnl_paisa: Sequence[int],
    win_rate: Sequence[float],
    max_dd_pct: Sequence[float],
    losing_trades: Sequence[int],
    *,
    eps_net_pnl_paisa: int = PARETO_EPS_NET_PNL_PAISA,
    eps_win_rate: float = PARETO_EPS_WIN_RATE,
    eps_max_dd_pct: float = PARETO_EPS_MAX_DD_PCT,
    eps_losing_trades: int = PARETO_EPS_LOSING_TRADES,
) -> list[int]:
    """The ε-Pareto frontier — ascending indices of the non-ε-dominated rows.

    The PYTHON ORACLE of ``fno_math.rank_pareto_frontier`` (bit-identical:
    integer results; the float comparisons use the SAME pinned expression
    forms — see the module docstring). O(n²) worst case, disclosed.

    Raises ``ValueError`` on a column length mismatch or a negative ε
    (mirroring the kernel's refusals; non-finite floats are the caller's
    contract — ``ResultRow``/``ResultMetrics`` guarantee finite axes).
    """
    n = len(net_pnl_paisa)
    if not (len(win_rate) == len(max_dd_pct) == len(losing_trades) == n):
        raise ValueError(
            "multi_select: pareto axis column length mismatch — "
            f"net_pnl={n} win_rate={len(win_rate)} max_dd_pct={len(max_dd_pct)} "
            f"losing_trades={len(losing_trades)}"
        )
    if eps_net_pnl_paisa < 0 or eps_win_rate < 0.0 or eps_max_dd_pct < 0.0 or eps_losing_trades < 0:
        raise ValueError("multi_select: every pareto epsilon must be >= 0")

    def dominates(x: int, y: int) -> bool:
        # within-ε on EVERY axis (MAX: x + eps >= y; MIN: x - eps <= y) …
        within = (
            net_pnl_paisa[x] + eps_net_pnl_paisa >= net_pnl_paisa[y]
            and win_rate[x] + eps_win_rate >= win_rate[y]
            and max_dd_pct[x] - eps_max_dd_pct <= max_dd_pct[y]
            and losing_trades[x] - eps_losing_trades <= losing_trades[y]
        )
        if not within:
            return False
        # … AND strictly-beyond-ε on AT LEAST ONE (MAX: x > y + eps; MIN:
        # x < y - eps) — the pinned expression forms (module docstring).
        return (
            net_pnl_paisa[x] > net_pnl_paisa[y] + eps_net_pnl_paisa
            or win_rate[x] > win_rate[y] + eps_win_rate
            or max_dd_pct[x] < max_dd_pct[y] - eps_max_dd_pct
            or losing_trades[x] < losing_trades[y] - eps_losing_trades
        )

    return [y for y in range(n) if not any(x != y and dominates(x, y) for x in range(n))]


def axis_topm_indices(
    m: int,
    composite: Sequence[float],
    masks: Sequence[int],
    max_dd_pct: Sequence[float],
    losing_trades: Sequence[int],
    win_rate: Sequence[float],
    expectancy_paisa: Sequence[int],
) -> tuple[list[int], list[int], list[int], list[int], list[int]]:
    """Per-axis best-``m`` indices over the five quota axes — the Python oracle.

    Returns five best-first index lists in the FIXED quota-axis order
    ``(composite DESC, max_dd_pct ASC, losing_trades ASC, win_rate DESC,
    expectancy_paisa DESC)``; non-composite axes tie-break
    ``(axis value, -composite, mask)``; the composite axis keeps the existing
    ``(-composite, mask)`` law byte-identically. ``heapq.nsmallest`` on a
    strict total-order key — documented equivalent to ``sorted(...)[:m]``.

    O(A·n·log m). Raises ``ValueError`` on a column length mismatch or a
    negative ``m``.
    """
    n = len(composite)
    if not (
        len(masks)
        == len(max_dd_pct)
        == len(losing_trades)
        == len(win_rate)
        == len(expectancy_paisa)
        == n
    ):
        raise ValueError("multi_select: quota axis column length mismatch")
    if m < 0:
        raise ValueError(f"multi_select: quota m must be >= 0, got {m}")
    indices = range(n)

    def topm(key: Callable[[int], tuple[float, float, int]]) -> list[int]:
        return heapq.nsmallest(m, indices, key=key)

    by_composite = topm(lambda i: (-composite[i], -composite[i], masks[i]))
    by_dd = topm(lambda i: (max_dd_pct[i], -composite[i], masks[i]))
    by_losing = topm(lambda i: (float(losing_trades[i]), -composite[i], masks[i]))
    by_win_rate = topm(lambda i: (-win_rate[i], -composite[i], masks[i]))
    by_expectancy = topm(lambda i: (-float(expectancy_paisa[i]), -composite[i], masks[i]))
    return (by_composite, by_dd, by_losing, by_win_rate, by_expectancy)


@dataclass(frozen=True, slots=True)
class SelectionInputs:
    """Parallel axis columns of ONE tuple's ranked pool, in COMPOSITE order.

    The rows MUST be ordered by the composite ranking law
    ``(-composite_score, mask)`` ascending — exactly the order
    :func:`brutex.ranking.rank_combos` returns — because the composite
    top-K is taken as the index prefix ``[0, top_k)`` and the frontier
    over-cap keep is "first :data:`PARETO_FRONTIER_CAP` in input order"
    (== best by ``(-composite, mask)``). Masks must be unique (the ranked
    pool's law).
    """

    masks: tuple[int, ...]
    composites: tuple[float, ...]
    net_pnl_paisa: tuple[int, ...]
    win_rate: tuple[float, ...]
    max_dd_pct: tuple[float, ...]
    losing_trades: tuple[int, ...]
    expectancy_paisa: tuple[int, ...]

    def __post_init__(self) -> None:
        n = len(self.masks)
        for name in (
            "composites",
            "net_pnl_paisa",
            "win_rate",
            "max_dd_pct",
            "losing_trades",
            "expectancy_paisa",
        ):
            if len(getattr(self, name)) != n:
                raise ValueError(
                    f"SelectionInputs.{name} has {len(getattr(self, name))} rows, "
                    f"masks has {n} — parallel columns must match"
                )


@dataclass(frozen=True, slots=True)
class UnionSelection:
    """The computed persist union + tags of one tuple's ranked pool.

    Attributes
    ----------
    order
        The selected indices in PERSIST order: the composite top-K prefix
        first (input order), then the union-only extras in input order
        (== ``(-composite, mask)`` — deterministic, discovery-order-free).
    basis_by_index
        ``index → selection_basis`` (canonical-order comma-joined tokens)
        for every selected index.
    frontier
        The CAPPED frontier membership (``pareto_member`` truth set).
    truncated
        Gate-passers NOT selected (``n - len(order)``) — the
        ``gated_pass_truncated`` manifest counter's numerator.
    frontier_dropped
        Frontier members dropped by the :data:`PARETO_FRONTIER_CAP` bound
        (0 when the frontier fit; every drop is also WARN-logged).
    """

    order: tuple[int, ...]
    basis_by_index: Mapping[int, str]
    frontier: frozenset[int]
    truncated: int
    frontier_dropped: int


#: Injectable kernel-seam signatures (the engine seam substitutes the
#: burn-in-checked Rust kernels; ``None`` = the in-module Python oracle).
type FrontierFn = Callable[
    [Sequence[int], Sequence[float], Sequence[float], Sequence[int]], list[int]
]
type QuotaFn = Callable[
    [
        int,
        Sequence[float],
        Sequence[int],
        Sequence[float],
        Sequence[int],
        Sequence[float],
        Sequence[int],
    ],
    tuple[list[int], list[int], list[int], list[int], list[int]],
]


def _oracle_frontier(
    net_pnl: Sequence[int],
    win_rate: Sequence[float],
    max_dd_pct: Sequence[float],
    losing_trades: Sequence[int],
) -> list[int]:
    """Default frontier binding — the in-module oracle at the W1 ε defaults."""
    return pareto_frontier_indices(net_pnl, win_rate, max_dd_pct, losing_trades)


def compute_union_selection(
    inputs: SelectionInputs,
    *,
    top_k: int | None,
    quota_m: int = AXIS_QUOTA_M,
    frontier_cap: int = PARETO_FRONTIER_CAP,
    frontier_fn: FrontierFn | None = None,
    quota_fn: QuotaFn | None = None,
) -> UnionSelection:
    """Compute the persist union + tags over one composite-ordered pool.

    Pure + deterministic: a function of *inputs* + the knobs only, so a
    ``sweep-verify --recount`` re-derives byte-identical tags (the whole
    selection lives INSIDE ``run_persisted_sweep`` — never in the CLI shim).

    Parameters
    ----------
    inputs
        The pool's axis columns in composite order (:class:`SelectionInputs`).
    top_k
        The composite persist depth (``cfg.top_k``); ``None`` selects the
        WHOLE pool on the composite basis (the pre-W1 behaviour — the union
        adds tags, never changes the row set).
    quota_m, frontier_cap
        The per-axis quota + frontier bound (W1 defaults).
    frontier_fn, quota_fn
        Kernel injection seams (the engine's burn-in-checked Rust bindings);
        ``None`` = the Python oracles in this module.
    """
    n = len(inputs.masks)
    if n == 0:
        return UnionSelection(
            order=(), basis_by_index={}, frontier=frozenset(), truncated=0, frontier_dropped=0
        )
    frontier_select = frontier_fn if frontier_fn is not None else _oracle_frontier
    quota_select = quota_fn if quota_fn is not None else axis_topm_indices

    composite_depth = n if top_k is None else min(top_k, n)
    tokens_by_index: dict[int, set[str]] = {}

    def tag(index: int, token: str) -> None:
        tokens_by_index.setdefault(index, set()).add(token)

    for index in range(composite_depth):
        tag(index, BASIS_COMPOSITE)

    by_composite, by_dd, by_losing, by_win_rate, by_expectancy = quota_select(
        quota_m,
        inputs.composites,
        inputs.masks,
        inputs.max_dd_pct,
        inputs.losing_trades,
        inputs.win_rate,
        inputs.expectancy_paisa,
    )
    # The composite quota folds into the SAME "composite" basis token (its
    # law is the composite law — a distinct token would be noise).
    for index in by_composite:
        tag(index, BASIS_COMPOSITE)
    for index in by_dd:
        tag(index, BASIS_DD)
    for index in by_losing:
        tag(index, BASIS_LOSING_TRADES)
    for index in by_win_rate:
        tag(index, BASIS_WIN_RATE)
    for index in by_expectancy:
        tag(index, BASIS_EXPECTANCY)

    raw_frontier = frontier_select(
        inputs.net_pnl_paisa, inputs.win_rate, inputs.max_dd_pct, inputs.losing_trades
    )
    frontier_dropped = 0
    if len(raw_frontier) > frontier_cap:
        # Over-cap: keep the best frontier_cap by (-composite, mask) — the
        # input order — counted + WARNED, never silent (item 1's bound law).
        frontier_dropped = len(raw_frontier) - frontier_cap
        kept_frontier = sorted(raw_frontier)[:frontier_cap]
        _LOG.warning(
            "multi_select.frontier_capped",
            frontier_size=len(raw_frontier),
            cap=frontier_cap,
            dropped=frontier_dropped,
        )
    else:
        kept_frontier = sorted(raw_frontier)
    for index in kept_frontier:
        tag(index, BASIS_FRONTIER)

    # Persist order: the composite prefix first (input order), then the
    # union-only extras in input order (== (-composite, mask)).
    extras = sorted(index for index in tokens_by_index if index >= composite_depth)
    order = tuple(range(composite_depth)) + tuple(extras)
    basis_by_index = {index: join_basis_tokens(tuple(tokens_by_index[index])) for index in order}
    return UnionSelection(
        order=order,
        basis_by_index=basis_by_index,
        frontier=frozenset(kept_frontier),
        truncated=n - len(order),
        frontier_dropped=frontier_dropped,
    )
