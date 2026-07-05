"""Expiry-level date scoping for the F&O historical candle pull — pure.

DEC-FNO-EXPIRY-SCOPE-001 (operator requirement #30). The operator's from/to date
picker now also SCOPES the F&O historical pull at the EXPIRY level: an expiry is
IN SCOPE iff it lies within ``[scope_start, scope_end]`` (BOTH endpoints
inclusive; a ``None`` bound is unbounded on that side, so the ``(None, None)``
default keeps every expiry in scope — byte-identical to the unscoped pull).

The scope selects **WHICH expiries are pulled — it never reshapes windows**: an
in-scope contract keeps its normal per-type default window law (option
``expiry − 30d`` / future ``expiry − 60d``, clamped into the standard
``2020-01-01 → prev-day`` envelope —
:func:`~brutex.adapters.groww.contract_bounds.default_fno_minute_window`), while
an out-of-scope expiry is skipped ENTIRELY: zero vendor calls, zero window-ledger
rows (the partition runs strictly BEFORE
:func:`~brutex.adapters.groww.option_ingest.plan_contract_windows` seeds any
``PLANNED`` row — a stray ``PLANNED`` row would make every later pull re-attempt
it), and ONE honest ``fno.expiry_out_of_scope`` log line per
``(underlying, expiry)`` group. Already-stored lake bars for an out-of-scope
expiry are NEVER touched (this module reads and writes nothing — pure selection).

Consumed by :func:`~brutex.dashboard.historical_pull.pull_contract_candles`
(the ONE chokepoint through which both the metadata-driven contract rebuild and
the window planning flow), threaded from the ``/pull-pipeline`` route's operator
from/to via :func:`~brutex.dashboard.composite_pull.pull_historical_for_underlyings`.

Honest envelope (Golden Rule #10): O(contracts) one pass, no I/O; the log helper
is the only side effect. Scope is a pull-PLANNING filter — no lake-schema /
stored-bar / rate / vocab / engine change (ENGINE stays NIFTY/BANKNIFTY/SENSEX;
the F&O lake stays REFERENCE_ONLY).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

import structlog

from brutex.data.registry_store import ContractRow

#: Structured logger — one ``fno.expiry_out_of_scope`` line per skipped
#: ``(underlying, expiry)`` group (Golden Rule #10 observability, never silent).
_LOG = structlog.get_logger("brutex.adapters.groww.expiry_scope")

__all__ = [
    "ScopePartition",
    "expiry_in_scope",
    "log_out_of_scope_expiries",
    "normalize_expiry_scope",
    "partition_contracts_by_scope",
]


def normalize_expiry_scope(
    scope_start: date | None,
    scope_end: date | None,
    *,
    envelope_end: date,
    floor: date,
) -> tuple[date | None, date | None]:
    """Lift a DEFAULT-shaped scope bound to unbounded — pure except one log, O(1).

    DEC-EXPIRY-SCOPE-LIVE-EDGE-001 (amends the DEC-FNO-EXPIRY-SCOPE-001 law at
    the LIVE edge). The ``/universe`` date pickers PRE-FILL the canonical
    default ``[GROWW_HISTORY_FLOOR → previous day]`` and the frontend sent them
    whenever non-blank — ALWAYS — so every default one-click pull carried
    ``scope_end = previous day``, which scoped OUT every live/unexpired
    contract (an expiry strictly after yesterday failed
    :func:`expiry_in_scope`). This normalizer is the BACKEND-authoritative fix:

    * ``scope_end >= envelope_end`` (the request-time previous day, derived by
      the caller from the SAME ``resolve_candle_range`` law that pre-fills the
      picker) ⇒ the operator kept (or raised) the default upper bound, which
      expresses "everything up to now" — NOT "exclude the live expiries" — so
      the effective ``scope_end`` becomes ``None`` (unbounded above; live
      contracts stay in scope).
    * ``scope_start <= floor`` (``GROWW_HISTORY_FLOOR``) ⇒ the default lower
      bound ⇒ ``None`` (unbounded below — behaviour-identical, since no lake
      expiry predates the floor, but the scope log stops naming a phantom
      bound).
    * A GENUINELY narrowed bound (``floor < scope_start`` /
      ``scope_end < envelope_end``) passes through UNCHANGED — the
      DEC-FNO-EXPIRY-SCOPE-001 selection law is untouched for it.

    Emits ONE ``fno.expiry_scope.live_edge_unbounded`` log naming the raw vs
    effective bounds whenever anything was lifted (never silent — Golden Rule
    #10). ``(None, None)`` in ⇒ ``(None, None)`` out with NO log (the blank
    pull is byte-identical).

    Parameters
    ----------
    scope_start, scope_end
        The operator's raw inclusive expiry-scope bounds (``None`` = unbounded).
    envelope_end
        The request-time DEFAULT upper bound (the inclusive previous day the
        blank picker resolves to — ``default_date_range()[1]``).
    floor
        The Track-1 history floor (``GROWW_HISTORY_FLOOR`` = 2020-01-01,
        CLAUDE.md §3), injected by the caller (this adapter module must not
        import the dashboard layer).

    Returns
    -------
    tuple[date | None, date | None]
        ``(effective_start, effective_end)`` — each ``None`` when lifted.
    """
    effective_start = None if scope_start is not None and scope_start <= floor else scope_start
    effective_end = None if scope_end is not None and scope_end >= envelope_end else scope_end
    if (effective_start, effective_end) != (scope_start, scope_end):
        _LOG.info(
            "fno.expiry_scope.live_edge_unbounded",
            raw_start=scope_start.isoformat() if scope_start is not None else None,
            raw_end=scope_end.isoformat() if scope_end is not None else None,
            effective_start=effective_start.isoformat() if effective_start is not None else None,
            effective_end=effective_end.isoformat() if effective_end is not None else None,
            envelope_end=envelope_end.isoformat(),
            floor=floor.isoformat(),
            note=(
                "default-shaped scope bound(s) lifted to unbounded — live/unexpired "
                "expiries stay in scope (DEC-EXPIRY-SCOPE-LIVE-EDGE-001)"
            ),
        )
    return effective_start, effective_end


def expiry_in_scope(expiry: date, *, scope_start: date | None, scope_end: date | None) -> bool:
    """Return ``True`` iff *expiry* lies within ``[scope_start, scope_end]`` — pure, O(1).

    BOTH endpoints are INCLUSIVE (``expiry == scope_end`` is IN; ``scope_end +
    1 day`` is OUT). A ``None`` bound is unbounded on that side; both ``None``
    (the default pull) puts EVERYTHING in scope.

    Parameters
    ----------
    expiry
        The contract's expiry date (its hard last tradable day).
    scope_start, scope_end
        The operator's inclusive from/to expiry-scope bounds, or ``None`` for
        unbounded on that side.
    """
    if scope_start is not None and expiry < scope_start:
        return False
    return not (scope_end is not None and expiry > scope_end)


@dataclass(frozen=True, slots=True)
class ScopePartition:
    """The result of one expiry-scope partition over a contract selection.

    Attributes
    ----------
    in_scope
        The contracts whose expiry lies within the scope, in the input order —
        the ONLY contracts the pull may rebuild / plan / fetch.
    out_by_expiry
        ``(underlying, expiry) → contracts skipped`` for every OUT-of-scope
        expiry group — the one-log-line-per-expiry grouping (and the honest
        report tallies below). Empty for an unbounded scope.
    """

    in_scope: tuple[ContractRow, ...]
    out_by_expiry: dict[tuple[str, date], int]

    @property
    def contracts_out_of_scope(self) -> int:
        """Total contract legs excluded by the scope (the report tally)."""
        return sum(self.out_by_expiry.values())

    @property
    def expiries_out_of_scope(self) -> int:
        """Distinct ``(underlying, expiry)`` groups excluded by the scope."""
        return len(self.out_by_expiry)


def partition_contracts_by_scope(
    contracts: Sequence[ContractRow],
    *,
    scope_start: date | None,
    scope_end: date | None,
) -> ScopePartition:
    """Split *contracts* into in-scope and out-of-scope-by-expiry — pure, O(contracts).

    Each row's AUTHORITATIVE registry ``expiry_date`` is tested via
    :func:`expiry_in_scope` (both bounds inclusive; ``None`` = unbounded). The
    default ``(None, None)`` scope returns EVERY contract in scope with an empty
    ``out_by_expiry`` — byte-identical to the unscoped pull (regression-pinned).
    Order is preserved for the in-scope rows (the pull's downstream ordering is
    untouched).
    """
    in_scope: list[ContractRow] = []
    out_by_expiry: dict[tuple[str, date], int] = {}
    for row in contracts:
        if expiry_in_scope(row.expiry_date, scope_start=scope_start, scope_end=scope_end):
            in_scope.append(row)
        else:
            key = (row.underlying, row.expiry_date)
            out_by_expiry[key] = out_by_expiry.get(key, 0) + 1
    return ScopePartition(in_scope=tuple(in_scope), out_by_expiry=out_by_expiry)


def log_out_of_scope_expiries(
    partition: ScopePartition,
    *,
    scope_start: date | None,
    scope_end: date | None,
) -> None:
    """Emit ONE ``fno.expiry_out_of_scope`` log line per skipped ``(underlying, expiry)``.

    Deterministic (sorted by ``(underlying, expiry)``); each line names the
    underlying, the expiry, the scope bounds and how many contract legs the
    group skipped — so a scoped-out expiry is always VISIBLE, never a silent
    hole (Golden Rule #10). A no-skip partition emits nothing.
    """
    for (underlying, expiry), skipped in sorted(partition.out_by_expiry.items()):
        _LOG.info(
            "fno.expiry_out_of_scope",
            underlying=underlying,
            expiry=expiry.isoformat(),
            scope_start=scope_start.isoformat() if scope_start is not None else None,
            scope_end=scope_end.isoformat() if scope_end is not None else None,
            contracts_skipped=skipped,
        )
