"""Per-period finalist stats — canonical JSON, key grammar, cross-sum validators
(DEC-SWEEP-TEMPORAL-ROBUSTNESS-001).

THE one COMMON home (CLAUDE.md §11) for the Tier-1 per-period statistics behind
the temporal-robustness feature: the ``stats_by_{year,quarter,month}_json`` +
``temporal_judged_years_json`` optional :class:`~brutex.engine.results_store.
ResultRow` columns. Consumed by

* the finalist projector (``sweep_run.ranked_combo_to_result_row``) — the fold
  producer (:func:`fold_period_stats`, the engine-wiring commit);
* :class:`~brutex.engine.results_store.ResultRow` construction/load — the
  cross-sum VALIDATORS (:func:`validate_period_stats`), so a fabricated or torn
  stored ``n``/``pnl``/``wins`` cell is structurally impossible to load
  silently (per-period ``max_dd`` is bounded ``>= 0`` int64 ONLY — non-rollup
  by design, unre-derivable at load without the trade stream, so it is NOT
  cross-validated and a tampered dd value alone is not detected); and
* the dashboard parse — stored values only, decoded through the SAME
  :func:`decode_period_stats` / :func:`decode_judged_years`.

Cell law (pinned): every period cell is the 4-int array
``[n_trades, net_pnl_paisa, wins, max_dd_paisa]`` — ints ONLY (exact int64
paisa; ``win_rate`` is derivable and never stored — no float drift). Zero-trade
buckets are ABSENT keys; with ``temporal_judged_years_json`` a reader
distinguishes zero-trade (judged year, absent key) from out-of-window (not in
the judged list).

Cross-sum law (pinned at construction/load): Σ month ``n`` == the row's
``total_trades``; Σ month ``pnl`` == ``net_pnl_paisa``; Σ month ``wins`` ==
``winning_trades``; each quarter/year cell equals the sum of ITS months for
``n``/``pnl``/``wins``. ``max_dd`` deliberately does NOT roll up — a
boundary-spanning drawdown exceeds every sub-period's (see the dd semantics
below), which is exactly why all three horizons are stored first-class.

Per-period ``max_dd_paisa`` semantics (ships verbatim — Golden Rule #10):
(1) basis = realized-at-exit equity (the ``build_equity_curve`` law — no
mark-to-market: a quarter spent underwater on one open trade reads dd 0 until
exit; intraday truth is the Tier-2 minute path only); (2) trade-close granular;
(3) the peak RESETS at each period start — a boundary-spanning drawdown is
SPLIT and systematically UNDERSTATED per-period; neither the sum nor the max of
per-period dds reproduces the row's global ``max_drawdown_paisa``, which
remains the ONLY authoritative risk figure; (4) intra-period reset is chosen
over a carried peak (a carried peak mis-attributes a prior year's high to a
flat year).

Canonical JSON: ``sort_keys=True, separators=(",", ":"), allow_nan=False`` —
byte-deterministic; the validators RE-ENCODE canonically so an in-memory row is
always canonical regardless of the stored spelling (the price-snap philosophy).
Errors are :class:`ValueError` with the offending key named — the store wraps
them into its own typed error at its boundary (the G14 law).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from itertools import pairwise
from typing import TYPE_CHECKING, Final

from brutex.engine.session_lens import ist_trade_date

if TYPE_CHECKING:  # pragma: no cover - import only for static typing
    from collections.abc import Iterable, Mapping, Sequence

    from brutex.metrics.types import ClosedTrade

__all__ = [
    "PERIOD_CELL_LEN",
    "PeriodCell",
    "PeriodStats",
    "decode_judged_years",
    "decode_period_stats",
    "encode_judged_years",
    "encode_period_stats",
    "fold_period_stats",
    "month_key",
    "quarter_key",
    "quarter_of_month",
    "validate_period_stats",
    "year_key",
]

#: One period cell: ``(n_trades, net_pnl_paisa, wins, max_dd_paisa)`` — exact ints.
PeriodCell = tuple[int, int, int, int]

#: The pinned cell arity (see the module docstring's cell law).
PERIOD_CELL_LEN: Final[int] = 4

# ── Signed int64 bounds (the Parquet/paisa storage law — results_store mirrors) ──
_INT64_MIN: Final[int] = -(2**63)
_INT64_MAX: Final[int] = 2**63 - 1

# ── Canonical key grammar (pinned wire shapes) ──
_YEAR_KEY_RE: Final[re.Pattern[str]] = re.compile(r"^\d{4}$")
_QUARTER_KEY_RE: Final[re.Pattern[str]] = re.compile(r"^(\d{4})Q([1-4])$")
_MONTH_KEY_RE: Final[re.Pattern[str]] = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")

#: kind → (grammar, human name) for :func:`decode_period_stats`.
_KEY_GRAMMAR: Final[dict[str, tuple[re.Pattern[str], str]]] = {
    "year": (_YEAR_KEY_RE, "YYYY"),
    "quarter": (_QUARTER_KEY_RE, "YYYYQn"),
    "month": (_MONTH_KEY_RE, "YYYY-MM"),
}


def year_key(year: int) -> str:
    """The canonical year bucket key — ``"YYYY"`` (zero-padded 4 digits)."""
    return f"{year:04d}"


def quarter_key(year: int, quarter: int) -> str:
    """The canonical quarter bucket key — ``"YYYYQn"`` (``n`` in 1..4)."""
    return f"{year:04d}Q{quarter}"


def month_key(year: int, month: int) -> str:
    """The canonical month bucket key — ``"YYYY-MM"`` (zero-padded)."""
    return f"{year:04d}-{month:02d}"


def quarter_of_month(month: int) -> int:
    """The calendar quarter (1..4) a month (1..12) belongs to."""
    return (month - 1) // 3 + 1


def _dumps(payload: object) -> str:
    """THE canonical JSON encode (sorted keys, tight separators, no NaN)."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _loads(payload: str, *, column: str) -> object:
    """``json.loads`` with the nesting bomb folded into ``ValueError``.

    ``RecursionError``: a pathologically nested valid-JSON payload (e.g. 60k
    nested arrays) is parser-level corruption — the same contract as malformed
    JSON, and the house convention at every sibling ``json.loads`` boundary
    (the ``sweep_checkpoint`` precedent) — so a tampered/foreign store column
    surfaces through the typed-damage law (G14) instead of escaping as a raw
    ``RecursionError`` past :meth:`ResultsStore.load`'s except tuple.
    """
    try:
        return json.loads(payload)  # json.JSONDecodeError ⊂ ValueError
    except RecursionError as exc:
        raise ValueError(
            f"{column} payload is pathologically nested JSON (nesting bomb): {exc}"
        ) from exc


def _require_cell_int(value: object, *, key: str, position: int) -> int:
    """One validated cell int — bool/float/str rejected, int64-bounded."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(
            f"period cell {key!r}[{position}] must be an int, got {value!r} "
            f"({type(value).__name__})"
        )
    if not _INT64_MIN <= value <= _INT64_MAX:
        raise ValueError(f"period cell {key!r}[{position}] = {value} is out of int64 range")
    return value


def encode_period_stats(cells: Mapping[str, PeriodCell]) -> str:
    """Encode ``{period key: cell}`` to the canonical JSON column string.

    Tuples become the pinned 4-int JSON arrays; keys sort canonically. The
    producer's write path — the decode/validate twin round-trips it exactly.
    """
    return _dumps({key: list(cell) for key, cell in cells.items()})


def decode_period_stats(payload: str, *, kind: str) -> dict[str, PeriodCell]:
    """Decode + STRICTLY validate one stored period-stats JSON column.

    Parameters
    ----------
    payload
        The stored JSON string.
    kind
        ``"year"`` / ``"quarter"`` / ``"month"`` — selects the key grammar.

    Returns
    -------
    dict[str, PeriodCell]
        ``{canonical key: (n_trades, net_pnl_paisa, wins, max_dd_paisa)}``.

    Raises
    ------
    ValueError
        On non-JSON / non-object / pathologically-nested (nesting-bomb)
        payloads, a malformed key, a wrong-arity or non-int cell,
        ``wins > n``, a negative count/dd, or an int64-out-of-range value —
        always naming the offending key. A fabricated cell is structurally
        unloadable (Golden Rule #1).
    """
    grammar, shape = _KEY_GRAMMAR[kind]
    decoded = _loads(payload, column=f"stats_by_{kind}")
    if not isinstance(decoded, dict):
        raise ValueError(f"stats_by_{kind} payload must be a JSON object, got {decoded!r}")
    out: dict[str, PeriodCell] = {}
    for key, raw_cell in decoded.items():
        if not grammar.match(key):
            raise ValueError(f"stats_by_{kind} key {key!r} is not canonical {shape}")
        if not isinstance(raw_cell, list) or len(raw_cell) != PERIOD_CELL_LEN:
            raise ValueError(
                f"period cell {key!r} must be a {PERIOD_CELL_LEN}-int array "
                f"[n_trades, net_pnl_paisa, wins, max_dd_paisa], got {raw_cell!r}"
            )
        n, pnl, wins, dd = [
            _require_cell_int(value, key=key, position=position)
            for position, value in enumerate(raw_cell)
        ]
        if n < 0 or wins < 0 or dd < 0:
            raise ValueError(
                f"period cell {key!r}: n_trades/wins/max_dd must be >= 0, "
                f"got [n={n}, wins={wins}, dd={dd}]"
            )
        if wins > n:
            raise ValueError(f"period cell {key!r}: wins {wins} exceeds n_trades {n}")
        out[key] = (n, pnl, wins, dd)
    return out


def encode_judged_years(years: Iterable[int]) -> str:
    """Encode the judged-year set to its canonical JSON column string.

    Sorted, de-duplicated ``"YYYY"`` strings — the serve-time denominator
    column (zero-trade vs out-of-window becomes distinguishable).
    """
    return _dumps([year_key(year) for year in sorted(set(years))])


def decode_judged_years(payload: str) -> tuple[int, ...]:
    """Decode + STRICTLY validate the stored judged-years JSON column.

    Raises
    ------
    ValueError
        On a non-list / pathologically-nested (nesting-bomb) payload, a
        non-``"YYYY"`` entry, or a non-strictly-ascending sequence (the
        canonical order — duplicates included).
    """
    decoded = _loads(payload, column="temporal_judged_years")
    if not isinstance(decoded, list):
        raise ValueError(f"temporal_judged_years payload must be a JSON array, got {decoded!r}")
    years: list[int] = []
    for entry in decoded:
        if not isinstance(entry, str) or not _YEAR_KEY_RE.match(entry):
            raise ValueError(f"temporal_judged_years entry {entry!r} is not canonical YYYY")
        years.append(int(entry))
    for previous, current in pairwise(years):
        if current <= previous:
            raise ValueError(
                f"temporal_judged_years must be strictly ascending, got {previous} before {current}"
            )
    return tuple(years)


def _group_sums(
    months: Mapping[str, PeriodCell],
) -> tuple[dict[str, tuple[int, int, int]], dict[str, tuple[int, int, int]]]:
    """Sum month cells into their (year, quarter) groups for n/pnl/wins.

    ``max_dd`` is deliberately excluded — it does not roll up (module law).
    """
    by_year: dict[str, tuple[int, int, int]] = {}
    by_quarter: dict[str, tuple[int, int, int]] = {}
    for key, (n, pnl, wins, _dd) in months.items():
        # decode_period_stats guaranteed the canonical "YYYY-MM" grammar, so a
        # positional parse is exact (no regex re-match, no dead defensive branch).
        year = int(key[0:4])
        month = int(key[5:7])
        for group, group_key in (
            (by_year, year_key(year)),
            (by_quarter, quarter_key(year, quarter_of_month(month))),
        ):
            acc_n, acc_pnl, acc_wins = group.get(group_key, (0, 0, 0))
            group[group_key] = (acc_n + n, acc_pnl + pnl, acc_wins + wins)
    return by_year, by_quarter


def _check_group(
    label: str,
    cells: Mapping[str, PeriodCell],
    sums: Mapping[str, tuple[int, int, int]],
) -> None:
    """One rollup identity: the stored *cells* equal the month-sum *sums* exactly."""
    if set(cells) != set(sums):
        raise ValueError(
            f"stats_by_{label} keys {sorted(cells)} do not match the month-derived "
            f"groups {sorted(sums)}"
        )
    for key, (n, pnl, wins, _dd) in cells.items():
        if (n, pnl, wins) != sums[key]:
            raise ValueError(
                f"stats_by_{label} cell {key!r} [n={n}, pnl={pnl}, wins={wins}] does not "
                f"equal the sum of its months {sums[key]} (max_dd is deliberately not "
                f"compared — it does not roll up)"
            )


def validate_period_stats(
    *,
    stats_by_year_json: str,
    stats_by_quarter_json: str,
    stats_by_month_json: str,
    temporal_judged_years_json: str,
    total_trades: int,
    net_pnl_paisa: int,
    winning_trades: int,
) -> tuple[str, str, str, str]:
    """Decode, cross-sum-validate, and canonically re-encode the four columns.

    THE construction/load validator (graft 6): fabricated ``n``/``pnl``/
    ``wins`` buckets are structurally impossible — every month cell must sum
    to the row's own ``total_trades`` / ``net_pnl_paisa`` / ``winning_trades``,
    and every quarter/year cell must equal the sum of ITS months (n/pnl/wins;
    ``max_dd`` exempt — non-rollup by design, bounded ``>= 0`` int64 only, NOT
    cross-validated). The judged-years column is grammar- and
    order-validated (no cross-constraint with the buckets: a zero-trade judged
    year has no bucket, and an UNJUDGED sliver year may still carry one — the
    judged list is exactly what makes that distinction readable).

    Returns
    -------
    tuple[str, str, str, str]
        The four CANONICAL re-encodes ``(year, quarter, month, judged)`` — the
        caller stores these, so an in-memory row is byte-canonical regardless
        of the input spelling.

    Raises
    ------
    ValueError
        On any grammar / cell / cross-sum violation, naming the offender.
    """
    years = decode_period_stats(stats_by_year_json, kind="year")
    quarters = decode_period_stats(stats_by_quarter_json, kind="quarter")
    months = decode_period_stats(stats_by_month_json, kind="month")
    judged = decode_judged_years(temporal_judged_years_json)

    totals = (
        sum(cell[0] for cell in months.values()),
        sum(cell[1] for cell in months.values()),
        sum(cell[2] for cell in months.values()),
    )
    expected = (total_trades, net_pnl_paisa, winning_trades)
    if totals != expected:
        raise ValueError(
            f"month cells sum to [n={totals[0]}, pnl={totals[1]}, wins={totals[2]}] but the "
            f"row carries [total_trades={expected[0]}, net_pnl_paisa={expected[1]}, "
            f"winning_trades={expected[2]}]"
        )
    by_year, by_quarter = _group_sums(months)
    _check_group("year", years, by_year)
    _check_group("quarter", quarters, by_quarter)

    return (
        encode_period_stats(years),
        encode_period_stats(quarters),
        encode_period_stats(months),
        encode_judged_years(judged),
    )


# ── The finalist fold + segmented-dd walk (the producer side) ────────────────


class _PeriodTracker:
    """One horizon's running fold: cells + the intra-period-reset dd walk."""

    __slots__ = ("cells", "current_key", "max_dd", "n", "peak", "pnl", "wins")

    def __init__(self) -> None:
        self.cells: dict[str, PeriodCell] = {}
        self.current_key: str | None = None
        self.n = 0
        self.pnl = 0
        self.wins = 0
        self.peak = 0
        self.max_dd = 0

    def roll(self, key: str, equity_before: int) -> None:
        """Enter *key* — finalize the previous period, reset the peak."""
        if key == self.current_key:
            return
        self.finalize()
        self.current_key = key
        self.n = 0
        self.pnl = 0
        self.wins = 0
        # The period's opening equity IS its initial peak (peak reset at the
        # boundary — a drawdown running into this period is SPLIT here).
        self.peak = equity_before
        self.max_dd = 0

    def add(self, pnl_paisa: int, equity_after: int) -> None:
        """Fold one trade closed inside the current period."""
        self.n += 1
        self.pnl += pnl_paisa
        if pnl_paisa > 0:
            self.wins += 1
        drawdown = self.peak - equity_after
        self.max_dd = max(self.max_dd, drawdown)
        self.peak = max(self.peak, equity_after)

    def finalize(self) -> None:
        """Write the open period's cell (no-op before the first roll)."""
        if self.current_key is not None:
            self.cells[self.current_key] = (self.n, self.pnl, self.wins, self.max_dd)


@dataclass(frozen=True, slots=True)
class PeriodStats:
    """One finalist's per-period cells at all three stored horizons.

    Produced by :func:`fold_period_stats`; cross-sum-consistent BY CONSTRUCTION
    (all three horizons fold the same trades), so encoding these maps always
    satisfies :func:`validate_period_stats` against the same trades' totals.
    """

    by_year: dict[str, PeriodCell]
    by_quarter: dict[str, PeriodCell]
    by_month: dict[str, PeriodCell]


def fold_period_stats(trades: Sequence[ClosedTrade], *, start_equity_paisa: int) -> PeriodStats:
    """Fold a finalist's closed trades into month/quarter/year cells — O(trades).

    ONE chronological pass (trades ordered by ``exit_ts`` — re-sorted stably
    here so the walk is deterministic for any input order) over the
    realized-at-exit equity sequence, segmented CONCURRENTLY by IST month /
    quarter / year with a peak reset at each period boundary — the per-period
    ``max_dd_paisa`` semantics of the module docstring (boundary-SPLIT,
    non-rollup, trade-close granular). Attribution is EXIT-year/-month: a
    boundary-straddling trade books wholly to its exit period (the
    ``combo_aggregates.aggregate_combo`` law — property-pinned equal).

    Parameters
    ----------
    trades
        The finalist's re-simulated :class:`~brutex.metrics.types.ClosedTrade`
        stream (may be empty ⇒ three empty maps).
    start_equity_paisa
        The equity base anchoring the walk (the ``build_equity_curve`` law).
        Per-period dd is INVARIANT to this constant offset (peak − equity
        subtracts it away) — it is threaded for basis-law fidelity, never a
        tunable.
    """
    ordered = sorted(trades, key=lambda trade: trade.exit_ts)
    trackers = (_PeriodTracker(), _PeriodTracker(), _PeriodTracker())
    running = start_equity_paisa
    for trade in ordered:
        exit_date = ist_trade_date(trade.exit_ts)
        keys = (
            year_key(exit_date.year),
            quarter_key(exit_date.year, quarter_of_month(exit_date.month)),
            month_key(exit_date.year, exit_date.month),
        )
        for tracker, key in zip(trackers, keys, strict=True):
            tracker.roll(key, running)
        running += trade.pnl_paisa
        for tracker in trackers:
            tracker.add(trade.pnl_paisa, running)
    for tracker in trackers:
        tracker.finalize()
    return PeriodStats(
        by_year=trackers[0].cells,
        by_quarter=trackers[1].cells,
        by_month=trackers[2].cells,
    )
