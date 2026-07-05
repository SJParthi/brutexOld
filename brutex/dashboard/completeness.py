"""Data Completeness Scoreboard — per-symbol + aggregate, across all three dimensions.

``DEC-COMPLETENESS-SCOREBOARD-001``. One honest answer to "of all our symbols, how many
have the COMPLETE 6-year (2020→today) dataset?" — split **indices vs equities-cash,
SEPARATELY**, across three dimensions:

1. **1-minute spot** — EXACT %: the window ledger knows every expected trading-day window, so
   completeness is ``resolved ÷ total`` (reuses the chip classification already proven by
   :func:`~brutex.dashboard.coverage_view.build_spot_coverage_payload`). 1-minute is the
   SINGLE granularity fetched from Groww (DEC-1MINUTE-ONLY-DERIVE-1DAY-001); 1-day + the 19
   intraday TFs are DERIVED from it (their fetch-ledger has no windows), so they are NOT
   scored — only the one fetched dimension is.
2. **Option EXPIRIES** — there is **no knowable expected total** (Groww only returns the set
   that EXISTS when asked), so this is NOT a fake percentage; it is the pulled-count + a
   **stable / growing** signal: ``stable`` = every currently-listed expiry pulled AND the
   last re-pull found 0 new (the honest "complete" for options — made trustworthy by the
   ``get_expiries`` empty-retry of DEC-EMPTY-RETRY-EVERYWHERE-001).
3. **Option CONTRACTS** — same: pulled-count + status; expected total unknowable.

This module is a **pure re-aggregation** over inputs the route already computes (the
annotated spot rows from :func:`build_spot_coverage_payload`, a ``symbol → category`` map,
and the option :class:`~brutex.data.coverage_ledger.CoverageRecord` rows), so the whole
surface is 100% unit-testable offline — no lake, no store, no network (CLAUDE.md §11 COMMON:
it REUSES the ledger classification rather than re-deriving it). O(rows): one pass to group
by symbol, one pass to fold options; O(1) per symbol lookup via dicts.

**Honest envelope (Golden Rule #1 / #10):** spot completeness is exact (ledger-backed);
option "stable" is the only honest "complete" given the vendor exposes no total — surfaced
as ``stable``/``growing``/``partial``/``failed``, never a fabricated %. A delisted / no-longer
-listed symbol is judged on the windows it HAS (the ledger never planned windows past its
real lifetime), so it reads complete-up-to-lifetime, not penalised.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Final

import structlog

from brutex.dashboard._csv_utils import FormulaSafeWriter as _FormulaSafeWriter
from brutex.dashboard.coverage_view import (
    COMPLETENESS_FAILED,
    COMPLETENESS_PENDING,
    COMPLETENESS_SETTLED,
)
from brutex.data.lake_keys import resolve_spot_key
from brutex.data.partition import SPOT_PROBE_SEGMENTS

_LOG = structlog.get_logger("brutex.dashboard.completeness")

#: The ONE physically-fetched spot dimension the scoreboard reports. 1-minute is the SINGLE
#: granularity fetched from Groww (DEC-1MINUTE-ONLY-DERIVE-1DAY-001); ``1day`` (the PDH/PDL
#: anchor) AND the 19 intraday TFs are DERIVED from the stored 1-minute, never separately
#: pulled — so they are NOT scored. 1-day was dropped from the scoreboard view because its
#: fetch-ledger has no 1-day windows (it is derived), which made the column always read
#: "none" even though the derived 1-day exists in the lake.
SPOT_DIMENSIONS: Final[tuple[str]] = ("1minute",)

#: Instrument categories — counted SEPARATELY per the operator requirement.
CATEGORY_INDEX: Final[str] = "index"
CATEGORY_EQUITY: Final[str] = "equity"
CATEGORY_REFERENCE: Final[str] = "reference"
CATEGORIES: Final[tuple[str, str, str]] = (CATEGORY_INDEX, CATEGORY_EQUITY, CATEGORY_REFERENCE)

#: Per-dimension symbol verdicts (a symbol's standing on ONE dimension).
DIM_FULL: Final[str] = "full"  # ledger-settled: every expected window resolved (no pending/failed)
DIM_PARTIAL: Final[str] = "partial"  # has unfetched PLANNED windows still to pull
DIM_FAILED: Final[str] = "failed"  # has FAILED windows (transient — a re-pull should retry)
DIM_NONE: Final[str] = "none"  # no ledger record yet (never pulled)

#: Per-underlying OPTION verdicts (the honest "stable=complete", no fake %).
OPT_STABLE: Final[str] = "stable"  # all attempted years COMPLETE — the honest "complete"
OPT_PARTIAL: Final[str] = "partial"  # expiries captured, contracts not yet (expiries_only)
OPT_GROWING: Final[str] = "growing"  # a year still PENDING — chain still filling in
OPT_FAILED: Final[str] = "failed"  # a year FAILED — needs a re-pull
OPT_NONE: Final[str] = "none"  # no coverage record (no options / never attempted)

#: Honest annotation for the misleading ``contracts_pulled = 0`` on an
#: ``expiries_only`` underlying (DEC-METADATA-LIVE-PROGRESS-001). The lean universe
#: PARSES then DISCARDS the ~89k option legs (DEC-INSTRUMENT-UNIVERSE-FAST-001) — the
#: metadata pull persists only the chain SHAPE (expiry dates + contract symbols), so
#: ``0`` here is BY DESIGN, not a broken/failed pull. Surfaced as a row note so the
#: operator reads "metadata-only" instead of "0 = broken". Empty for every other case
#: (a row with persisted contracts, or a non-``expiries_only`` verdict) so no false
#: claim is ever attached.
OPT_CONTRACTS_BY_DESIGN_NOTE: Final[str] = (
    "metadata-only by design — option legs are discarded "
    "(DEC-INSTRUMENT-UNIVERSE-FAST-001), only the chain shape is kept"
)

#: The coverage-ledger cell statuses this module BRANCHES on (mirror of the
#: ``coverage_ledger.STATUS_*`` strings, duplicated here to keep this pure module
#: import-light — a drift test pins them equal). ``complete`` is intentionally NOT
#: duplicated: it is the fall-through of :func:`_option_verdict` (every year complete →
#: ``stable``), so naming a constant for it would be dead.
_CELL_EXPIRIES_ONLY: Final[str] = "expiries_only"
_CELL_PENDING: Final[str] = "pending"
_CELL_FAILED: Final[str] = "failed"

#: Exclusive upper bound for the "has SOLID 2020-floor data" test (DEC-COMPLETENESS-UNIVERSE-001,
#: tightened by DEC-SPOT-COMPLETENESS-2020-FLOOR-001 #868). A dimension's earliest bar date
#: ``< 2020-02-01`` ⇒ its first bar falls in JANUARY 2020 ⇒ the symbol has data from the START of
#: the ``GROWW_HISTORY_FLOOR`` year (CLAUDE.md §3 / DEC-PULL-FLOOR-2020-001), i.e. the solid full
#: 2020→today history the deep-drill brute-force backtest needs. A symbol whose first bar is LATER
#: — even later WITHIN 2020 (TATACONSUM 2020-02-27, MAXHEALTH 2020-08-21) — is missing the start
#: of 2020 and is honestly NOT "solid 2020" (operator decree 2026-06-21: "must have Jan-2020
#: data"). Month-granular (not exact-day) on purpose: robust to which exact first-trading-day of
#: January 2020 the vendor serves (Jan-1 is a market holiday; the first session varies), never a
#: brittle single-date magic threshold.
_FROM_2020_EXCLUSIVE_UPPER: Final[str] = "2020-02-01"

#: Cap on the number of offending symbols inlined into the
#: ``completeness.symbol_reconcile_mismatch`` structlog WARNING so one badly-diverged
#: master can never flood the log. The full counts are always emitted; only the inlined
#: SAMPLE lists are truncated to this many entries (DEC-COMPLETENESS-RECONCILE-DIAG-001).
_RECONCILE_SAMPLE_CAP: Final[int] = 10

#: The structlog event name for the reconciliation diagnostic — the SINGLE WARNING that
#: PINPOINTS a lake-symbol-key ↔ scored-symbol-key divergence (e.g. ``M&M`` reading ``none``
#: while its lake partition keys on a divergent form like ``"M&M "`` / ``"NSE-M&M"``).
RECONCILE_MISMATCH_EVENT: Final[str] = "completeness.symbol_reconcile_mismatch"


@dataclass(frozen=True, slots=True)
class SymbolReconcileReport:
    """OBSERVE-ONLY reconciliation of lake-coverage keys ↔ scored-universe keys.

    DEC-COMPLETENESS-RECONCILE-DIAG-001 — the structured result of
    :func:`reconcile_symbol_coverage`. It SURFACES the silent divergence the scoreboard
    otherwise hides two ways (and never repairs it — Golden Rule #1):

    * a LAKE coverage scan whose ``symbol`` key is NOT in the scored ``categories`` map is
      dropped by :func:`_per_symbol_spot` (``category = categories.get(symbol); if category
      is None: continue``) — it has data but is INVISIBLE on the board (a *stray lake
      partition*); and
    * conversely a scored symbol with NO matching lake row reads ``none`` even though a lake
      partition for a near-identical key exists — the ``M&M``-empty symptom the operator
      reported.

    HONEST (Golden Rule #1/#10): the EXACT divergent on-disk form is UNVERIFIED — the
    production pull keys BOTH the lake partition and the scored map on the same stripped
    ``instrument.symbol_name`` (``adapters/groww/ingest.py`` / ``instruments.py``), so a
    same-pull divergence should not occur; a real drift would come from a legacy / foreign /
    regression partition or an encoding artefact. This diagnostic does NOT assert a specific
    form (e.g. a trailing-space ``"M&M "`` or an exchange-prefixed ``"NSE-M&M"`` — those are
    ILLUSTRATIVE) — it SURFACES whatever drift actually exists, and the ``repr()`` of the
    offending keys in the WARNING names the true on-disk form in the live box.

    Either side alone is invisible; together they PINPOINT the exact key drift. The fields
    carry the full COUNTS plus capped SAMPLE lists (≤ :data:`_RECONCILE_SAMPLE_CAP`) so the
    log line can name the offending keys without flooding.

    Attributes
    ----------
    stray_lake_symbols
        Lake-scan keys present in the coverage scan but absent from the scored map — they
        HAVE data yet are not scored. Sorted, capped to :data:`_RECONCILE_SAMPLE_CAP`.
    stray_lake_count
        The FULL count of stray-lake keys (may exceed ``len(stray_lake_symbols)``).
    unmatched_scored_symbols
        Scored symbols with NO lake-coverage key at all — they read ``none`` on the board.
        Sorted, capped to :data:`_RECONCILE_SAMPLE_CAP`.
    unmatched_scored_count
        The FULL count of unmatched scored symbols.
    """

    stray_lake_symbols: tuple[str, ...] = ()
    stray_lake_count: int = 0
    unmatched_scored_symbols: tuple[str, ...] = ()
    unmatched_scored_count: int = 0

    def has_mismatch(self) -> bool:
        """True iff EITHER side diverged (a stray lake key OR an unmatched scored symbol).

        The healthy steady state — every lake key scored AND every scored symbol backed by a
        lake key — returns False, so the diagnostic stays SILENT (no log line).
        """
        return self.stray_lake_count > 0 or self.unmatched_scored_count > 0


def reconcile_symbol_coverage(
    *, lake_symbols: Iterable[str], scored_symbols: Iterable[str]
) -> SymbolReconcileReport:
    """Reconcile lake-coverage keys against scored-universe keys → a structured report.

    DEC-COMPLETENESS-RECONCILE-DIAG-001 — a PURE, OBSERVE-ONLY diagnostic (it changes no
    scoreboard output; see :func:`_log_symbol_reconcile_mismatch` for the sink). O(symbols)
    via two :class:`set` builds + two difference scans; every membership test is O(1).

    *lake_symbols* are the ``symbol`` keys the lake coverage scan emits (the keys
    :func:`_per_symbol_spot` looks UP in *categories*); *scored_symbols* are the keys of the
    scored ``categories`` map (the engine ``symbol_name`` the board iterates). The report
    carries:

    * ``stray_lake_symbols`` — lake keys NOT in the scored set (data present, not scored);
    * ``unmatched_scored_symbols`` — scored keys with NO lake key (they read ``none``).

    Both sides are needed: when ``M&M`` reads ``none`` because its lake partition keys on a
    divergent form (``"M&M "`` / ``"NSE-M&M"``), the lake form appears in
    ``stray_lake_symbols`` AND ``"M&M"`` appears in ``unmatched_scored_symbols`` — the pair
    PINPOINTS the exact drift. Samples are sorted + capped to :data:`_RECONCILE_SAMPLE_CAP`;
    the counts are always the FULL totals. Duplicates collapse via the sets.
    """
    lake_set = {str(symbol) for symbol in lake_symbols}
    scored_set = {str(symbol) for symbol in scored_symbols}
    stray = sorted(lake_set - scored_set)
    unmatched = sorted(scored_set - lake_set)
    return SymbolReconcileReport(
        stray_lake_symbols=tuple(stray[:_RECONCILE_SAMPLE_CAP]),
        stray_lake_count=len(stray),
        unmatched_scored_symbols=tuple(unmatched[:_RECONCILE_SAMPLE_CAP]),
        unmatched_scored_count=len(unmatched),
    )


def _log_symbol_reconcile_mismatch(report: SymbolReconcileReport) -> None:
    """Emit ONE :data:`RECONCILE_MISMATCH_EVENT` WARNING when *report* has a mismatch.

    DEC-COMPLETENESS-RECONCILE-DIAG-001 — the observe-only SINK for
    :func:`reconcile_symbol_coverage`. When either side diverged it logs ONE structured
    ``structlog`` WARNING carrying the full counts plus the ``repr()`` of the capped sample
    keys on BOTH sides, so whitespace / encoding differences are visible — for the
    ``M&M``-empty symptom the line would read e.g. ``stray_lake_symbols=["'M&M '"]`` (an
    ILLUSTRATIVE trailing space) AND ``unmatched_scored_symbols=["'M&M'"]``, PINPOINTING the
    exact (live-box-`repr()`-named, not assumed) divergence. ``repr()`` is
    used on every sampled key so an invisible trailing space / non-ASCII byte is rendered
    literally; the counts are the full totals (the samples are truncated, not the counts).
    Nothing is logged when *report* has no mismatch — the healthy steady state stays SILENT.

    It changes NO classification and adds / removes / alters NO scoreboard row (Golden Rule
    #1) — pure observability. The caller :func:`log_symbol_reconcile_from_board` wraps this
    so a diagnostic failure can never break the page.
    """
    if not report.has_mismatch():
        return
    _LOG.warning(
        RECONCILE_MISMATCH_EVENT,
        stray_lake_count=report.stray_lake_count,
        unmatched_scored_count=report.unmatched_scored_count,
        stray_lake_symbols=[repr(symbol) for symbol in report.stray_lake_symbols],
        unmatched_scored_symbols=[repr(symbol) for symbol in report.unmatched_scored_symbols],
    )


def log_symbol_reconcile_from_board(
    spot_rows: Iterable[Mapping[str, Any]], spot_categories: Mapping[str, str]
) -> SymbolReconcileReport:
    """Reconcile the scoreboard's OWN spot inputs + log ONE WARNING on a mismatch.

    DEC-COMPLETENESS-RECONCILE-DIAG-001 — the COMMON wiring point the build path calls. It
    derives the lake-symbol set from the SAME annotated *spot_rows* the scoreboard scores
    (filtered to the single fetched :data:`SPOT_DIMENSIONS` AND to the spot segments
    :data:`~brutex.data.partition.SPOT_PROBE_SEGMENTS` (``INDEX`` / ``CASH``) — a derived
    intraday-TF row is NOT a distinct lake symbol, and an F&O contract partition is ALSO
    stored at ``1minute`` but is NEVER a scored spot underlying, so it must be excluded or it
    would mis-count every backfilled F&O contract as a stray and drown the real signal) and
    the scored set from *spot_categories* (the engine ``symbol_name`` map the board iterates),
    runs :func:`reconcile_symbol_coverage`, and emits the diagnostic via
    :func:`_log_symbol_reconcile_mismatch`. Returns the report (so a caller / test can inspect
    it). It is BEST-EFFORT + OBSERVE-ONLY: the whole body is wrapped so it NEVER raises into,
    nor mutates, the scoreboard payload — a malformed row logs ONE
    ``completeness.symbol_reconcile_error`` WARNING and returns an empty report rather than
    breaking the page. O(spot_rows).
    """
    try:
        lake_symbols = [
            str(row["symbol"])
            for row in spot_rows
            if str(row["interval"]) in SPOT_DIMENSIONS
            and str(row.get("segment", "")) in SPOT_PROBE_SEGMENTS
        ]
        report = reconcile_symbol_coverage(
            lake_symbols=lake_symbols, scored_symbols=spot_categories.keys()
        )
        _log_symbol_reconcile_mismatch(report)
        return report
    except Exception as exc:
        # Observe-only diagnostic must NEVER break the page: any error (e.g. a malformed row)
        # is caught, logged ONCE, and an empty report returned instead of propagating.
        _LOG.warning("completeness.symbol_reconcile_error", error=str(exc))
        return SymbolReconcileReport()


def _spot_dimension_verdict(completeness: str | None) -> str:
    """Map a spot row's ``completeness`` chip to the scoreboard's per-dimension verdict.

    ``settled`` (every window resolved) → ``full``; ``pending`` → ``partial``; ``failed`` →
    ``failed``; a missing / no-record / derived / ambiguous row → ``none`` (no positive claim).
    """
    if completeness == COMPLETENESS_SETTLED:
        return DIM_FULL
    if completeness == COMPLETENESS_PENDING:
        return DIM_PARTIAL
    if completeness == COMPLETENESS_FAILED:
        return DIM_FAILED
    # no_ledger_record / derived / ambiguous / absent → no positive completeness claim.
    return DIM_NONE


def _overall_spot(one_minute: str) -> str:
    """A symbol's overall spot verdict — the 1-MINUTE verdict alone.

    1-minute is the SINGLE granularity fetched (DEC-1MINUTE-ONLY-DERIVE-1DAY-001); 1-day +
    the intraday TFs are DERIVED from it, so the overall spot standing IS the 1-minute
    standing (``full`` / ``partial`` / ``failed`` / ``none``) — there is no second dimension
    to AND against.
    """
    return one_minute


#: Spot dimension verdicts ranked best→worst, so a merge can keep the STRICTLY-better cell
#: when two lake partitions resolve to the same canonical symbol (the collision the key-drift
#: recovery introduces — DEC-COMPLETENESS-SPOT-KEY-RECOVER-001).
_SPOT_VERDICT_RANK: Final[dict[str, int]] = {
    DIM_NONE: 0,
    DIM_FAILED: 1,
    DIM_PARTIAL: 2,
    DIM_FULL: 3,
}


def _spot_cell_supersedes(
    new_status: str, new_start: str | None, cur_status: str, cur_start: str | None
) -> bool:
    """True if a ``(status, start)`` spot cell should REPLACE the currently-stored one.

    DEC-COMPLETENESS-SPOT-KEY-RECOVER-001 — once a drifted lake key is recovered to its
    canonical symbol, a clean partition and a stale legacy fragment can BOTH land on the same
    ``(symbol, interval)``. A blind last-write would let whichever the lake scan emits LAST win
    — and the scan is sorted, so the drifted ``"NSE-M&M"`` fragment sorts AFTER the clean
    ``"M&M"`` and would clobber a real ``full`` with a stale ``none`` (a Golden-Rule-#1 honesty
    regression). This makes the merge ORDER-INDEPENDENT: rank verdicts
    ``full > partial > failed > none`` and, on a tie, prefer the EARLIER start (the wider real
    coverage; ISO date strings sort chronologically, a ``None`` start ranks worst). So the
    surfaced cell is always the best real data that physically exists, regardless of scan order.
    """
    new_rank = _SPOT_VERDICT_RANK.get(new_status, 0)
    cur_rank = _SPOT_VERDICT_RANK.get(cur_status, 0)
    if new_rank != cur_rank:
        return new_rank > cur_rank
    if new_start is None:
        return False
    if cur_start is None:
        return True
    return new_start < cur_start


def _resolve_spot_category(
    symbol: str, exchange: str, categories: Mapping[str, str]
) -> tuple[str, str] | None:
    """Resolve a lake-partition spot symbol to its ``(canonical scored key, category)``.

    DEC-COMPLETENESS-SPOT-KEY-RECOVER-001 — the read-side FIX for the key drift the #907
    diagnostic (DEC-COMPLETENESS-RECONCILE-DIAG-001) only OBSERVED. A lake row's ``symbol``
    is the on-disk partition DIRECTORY NAME (``db_view`` reads ``sym_dir.name``). In the
    steady state the pull writes that directory from the stripped ``instrument.symbol_name``
    (``adapters/groww/ingest.py`` → ``instruments.py``), so it matches the scored
    *categories* key EXACTLY and this returns on the first lookup — byte-identical to the
    prior direct ``categories.get`` for every clean symbol. But a LEGACY / foreign /
    regression partition can carry a DRIFTED key; the two documented drift classes are a
    surrounding-whitespace form (``"M&M "``) or an exchange-PREFIXED groww_symbol form
    (``"NSE-M&M"`` instead of the bare ``"M&M"``). Without recovery such a partition is
    SILENTLY dropped (the ``categories.get`` miss → invisible), so its REAL on-disk data
    never shows on the scoreboard.

    Recovery tries ONLY those deterministic classes, in order: (1) exact, (2)
    whitespace-stripped, (3) the row's OWN *exchange* prefix stripped — DATA-DRIVEN off the
    partition's exchange, never a hardcoded exchange list, so a hyphenated symbol like
    ``BAJAJ-AUTO`` is untouched (it does not start with ``"<exch>-"``). Returns the CANONICAL
    scored key (so the row is grouped + displayed under the clean symbol) + its category, or
    ``None`` if no class matches — a genuinely unscored stray STAYS dropped, never fabricated
    onto the board (Golden Rule #1: this only surfaces data that physically exists on disk).

    DEC-LAKE-HEAL-001: the ladder body moved verbatim to the COMMON pure
    :func:`brutex.data.lake_keys.resolve_spot_key` (so the data-side lake
    scanner/healer share it without a ``data → dashboard`` import); this is now a
    thin byte-equivalent delegating wrapper (the ``cli._emit_pull_halt_alert`` →
    ``sweep_alerts.emit_pull_halt_alert`` pattern, #889).
    """
    return resolve_spot_key(symbol, exchange, categories)


def _per_symbol_spot(
    spot_rows: Iterable[Mapping[str, Any]], categories: Mapping[str, str]
) -> list[dict[str, Any]]:
    """Group annotated spot rows by symbol → one row per symbol with the 1-minute verdict.

    *spot_rows* are the annotated rows from
    :func:`~brutex.dashboard.coverage_view.build_spot_coverage_payload` (each carries
    ``symbol`` · ``interval`` · ``completeness``). *categories* maps a symbol to its
    :data:`CATEGORIES` bucket; an unmapped symbol is excluded (it is not in the scored
    universe) UNLESS its key is a recoverable drift of a scored symbol (see
    :func:`_resolve_spot_category` — the row is then grouped under the CANONICAL scored key).
    1-day is DERIVED from 1-minute (DEC-1MINUTE-ONLY-DERIVE-1DAY-001) so it is not scored —
    only the single fetched 1-minute dimension is. Symbols are returned sorted
    (symbol, exchange) for a stable view.
    """
    by_symbol: dict[str, dict[str, Any]] = {}
    for row in spot_rows:
        exchange = str(row.get("exchange", ""))
        resolved = _resolve_spot_category(str(row["symbol"]), exchange, categories)
        if resolved is None:
            continue  # not a scored universe symbol (e.g. a stray lake partition)
        symbol, category = resolved  # symbol is the CANONICAL scored key (drift recovered)
        interval = str(row["interval"])
        if interval not in SPOT_DIMENSIONS:
            continue  # a derived TF (incl. 1-day) — not the separately-pulled dimension
        entry = by_symbol.setdefault(
            symbol,
            {
                "symbol": symbol,
                "exchange": exchange,
                "category": category,
                "1minute": DIM_NONE,
                # DEC-COMPLETENESS-HONEST-EMPTY-001: the actually-covered date range for the
                # 1-minute dimension (the lake's earliest/latest bar) so a human sees in ONE
                # view what span it holds — None until a 1-minute row is seen.
                "1minute_start": None,
                "1minute_end": None,
                # Daily-open substitution honesty (DEC-DAILY-OPEN-SUBSTITUTION-HONESTY-001):
                # 0/0 until a 1-minute row is seen — the honest "nothing observed" default.
                "open_substituted_days": 0,
                "session_days": 0,
            },
        )
        first = row.get("first_date")
        last = row.get("last_date")
        first_s = str(first) if first else None
        # 2020-floor honesty downgrade: a settled-but-late-start 1-minute dimension reads
        # ``partial``, not ``full`` (DEC-SPOT-COMPLETENESS-2020-FLOOR-001) — the start date
        # below shows the operator WHEN it actually begins.
        new_verdict = _spot_dimension_verdict_with_floor(row.get("completeness"), first_s)
        # Rank-merge (DEC-COMPLETENESS-SPOT-KEY-RECOVER-001): when a clean + a drifted partition
        # both resolve to this canonical symbol, keep the STRICTLY-better cell so a stale
        # fragment can never clobber real data (order-independent — see _spot_cell_supersedes).
        if _spot_cell_supersedes(new_verdict, first_s, entry[interval], entry[f"{interval}_start"]):
            entry[interval] = new_verdict
            entry[f"{interval}_start"] = first_s
            entry[f"{interval}_end"] = str(last) if last else None
            # The substitution count travels WITH the rank-merge WINNING cell
            # (DEC-DAILY-OPEN-SUBSTITUTION-HONESTY-001) — a stale drifted
            # fragment can never clobber the winning partition's count.
            entry["open_substituted_days"] = int(row.get("open_substituted_days") or 0)
            entry["session_days"] = int(row.get("session_days") or 0)
    for entry in by_symbol.values():
        entry["overall"] = _overall_spot(entry["1minute"])
    return sorted(by_symbol.values(), key=lambda e: (e["symbol"], e["exchange"]))


def _option_verdict(cells: list[tuple[str, int]]) -> str:
    """Fold one underlying's per-year ``(status, expiries_pulled)`` cells into the honest verdict.

    DEC-OPTIONS-COMPLETENESS-REAL-YEARS-001 — a requested year that returned ZERO expiries (a
    pre-listing year of a late-listed underlying — e.g. JIOFIN before 2023, ETERNAL before 2024 —
    or a gap) is NOT a "still-filling chain": it must NOT force ``growing`` (the prior bug left
    such an underlying perpetually ``growing`` because its empty pre-listing years stayed
    ``pending``). The verdict, in order:

    * any ``failed`` year → ``failed`` (a re-pull should retry);
    * else NO year has ANY expiries → ``none`` (the underlying has no option history in the grid);
    * else any year that HAS expiries but is still ``pending`` → ``growing`` (DEFENSIVE /
      forward-compat — see the honesty note below);
    * else any requested year with ZERO expiries → ``partial`` (the option history does NOT span
      the full requested range — a late-listed start or a gap; HONEST, never a fabricated %);
    * else any ``expiries_only`` year → ``partial`` (expiries captured, contracts not yet);
    * else (every requested year ``complete`` AND non-empty) → ``stable`` — the honest "complete".

    Only years with ``expiries_pulled > 0`` count as real option-history; empty years are weighed
    here for the ``partial`` signal but are EXCLUDED from the displayed ``years`` span (the caller
    appends a year only when it has expiries), so a late-listed underlying shows only its real
    expiry years, never the full requested 2020→now range.

    Honesty note (Golden Rule #10, hostile-review #859): the ``growing`` arm is currently
    UNREACHABLE in production — the ledger's :func:`brutex.data.coverage_ledger.classify_cell`
    invariant is ``pending ⟺ expiries_pulled == 0`` (a year with discovered expiries is
    ``expiries_only`` or ``complete``, never ``pending``), so a ``(pending, n>0)`` cell cannot
    be emitted by the real pull. Under today's pipeline a still-being-filled current year reads
    ``expiries_only`` → ``partial`` until it completes; ``growing`` therefore never fires. The
    arm (and its synthetic ``(pending, n>0)`` unit fixtures) is retained as a DEFENSIVE /
    forward-compat fold in case the ledger ever gains a true "chain still filling" status —
    mirroring the existing defensive ``OPT_NONE`` aggregate case. It is NOT claimed as a live
    verdict. (Consequently the ``growing_or_partial`` aggregate bucket is, in real data,
    effectively partial-only.)
    """
    if not cells:
        return OPT_NONE
    with_expiries = [(status, n) for (status, n) in cells if n > 0]
    if any(status == _CELL_FAILED for status, _n in cells):
        return OPT_FAILED
    if not with_expiries:
        return OPT_NONE
    if any(status == _CELL_PENDING for status, _n in with_expiries):
        # DEFENSIVE / forward-compat (UNREACHABLE in production): the ledger invariant is
        # ``pending ⟺ expiries_pulled == 0`` (classify_cell), so no real cell is (pending, n>0).
        # Retained in case a true "chain still filling" status is ever added (see the docstring
        # honesty note + hostile-review #859); exercised only by synthetic unit fixtures.
        return OPT_GROWING
    if any(n == 0 for _status, n in cells):
        return OPT_PARTIAL
    if any(status == _CELL_EXPIRIES_ONLY for status, _n in with_expiries):
        return OPT_PARTIAL
    return OPT_STABLE  # every attempted year is COMPLETE


def _contracts_note(status: str, contracts_pulled: int) -> str:
    """Honest note for a ``contracts_pulled = 0`` row — empty unless it is by-design.

    Returns :data:`OPT_CONTRACTS_BY_DESIGN_NOTE` ONLY when the verdict is
    :data:`OPT_PARTIAL` (every captured year is ``expiries_only``) AND no contract was
    persisted — the lean-universe metadata-only state where ``0`` is intended, not
    broken (DEC-METADATA-LIVE-PROGRESS-001 / DEC-INSTRUMENT-UNIVERSE-FAST-001). Any
    other verdict, or any row that DID persist contracts, gets ``""`` so the page never
    attaches a false "by design" claim. O(1).
    """
    if status == OPT_PARTIAL and contracts_pulled == 0:
        return OPT_CONTRACTS_BY_DESIGN_NOTE
    return ""


def _per_underlying_options(
    records: Iterable[Any], categories: Mapping[str, str]
) -> list[dict[str, Any]]:
    """Roll option :class:`CoverageRecord` rows up per underlying → one row with the verdict.

    Sums ``expiries_pulled`` / ``contracts_pulled`` across all attempted years and folds the
    per-year statuses into one :func:`_option_verdict`. *categories* maps the underlying to
    its bucket (unmapped underlyings are skipped). Returned sorted by ``(underlying, exchange)``.
    """
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for rec in records:
        underlying = str(rec.underlying_symbol)
        category = categories.get(underlying)
        if category is None:
            continue
        exch = str(rec.exch_id)
        cell = grouped.setdefault(
            (underlying, exch),
            {
                "underlying": underlying,
                "exchange": exch,
                "category": category,
                "expiries_pulled": 0,
                "contracts_pulled": 0,
                "years": [],
                "_cells": [],
            },
        )
        expiries = int(rec.expiries_pulled)
        cell["expiries_pulled"] += expiries
        cell["contracts_pulled"] += int(rec.contracts_pulled)
        cell["_cells"].append((str(rec.status), expiries))
        # Only a year with REAL expiries enters the displayed span — a 0-expiry pre-listing
        # year (e.g. JIOFIN before 2023) is NEVER shown as covered (DEC-OPTIONS-COMPLETENESS
        # -REAL-YEARS-001); it is still weighed by _option_verdict for the partial signal.
        if expiries > 0:
            cell["years"].append(int(rec.year))
    out: list[dict[str, Any]] = []
    for cell in grouped.values():
        cells = cell.pop("_cells")
        cell["years"] = sorted(cell["years"])
        cell["status"] = _option_verdict(cells)
        # Honest annotation: a 0-contract expiries_only row is metadata-only BY DESIGN
        # (legs discarded), not broken (DEC-METADATA-LIVE-PROGRESS-001).
        cell["contracts_note"] = _contracts_note(cell["status"], cell["contracts_pulled"])
        out.append(cell)
    return sorted(out, key=lambda e: (e["underlying"], e["exchange"]))


def _from_2020(first_date: str | None) -> bool:
    """True iff *first_date* (an ISO ``YYYY-MM-DD`` or ``None``) has SOLID 2020-floor data.

    The honest "has data from the START of 2020" flag for the per-symbol view — True iff the
    earliest bar falls in JANUARY 2020 (``< 2020-02-01``), i.e. the symbol holds the full
    2020→today history from the GROWW_HISTORY_FLOOR. A symbol whose earliest bar is later — a late
    listing OR a gap, even one later WITHIN 2020 (TATACONSUM 2020-02-27, MAXHEALTH 2020-08-21) — is
    flagged False so the operator SEES it (the explicit dates disambiguate). Tightened from the
    prior calendar-2020 test by DEC-SPOT-COMPLETENESS-2020-FLOOR-001 #868 (operator decree).
    """
    return first_date is not None and first_date < _FROM_2020_EXCLUSIVE_UPPER


def _spot_dimension_verdict_with_floor(completeness: str | None, first_date: str | None) -> str:
    """The spot 1-minute verdict WITH the 2020-floor honesty downgrade applied.

    DEC-SPOT-COMPLETENESS-2020-FLOOR-001 — the SPOT analog of the options real-years fix
    (DEC-OPTIONS-COMPLETENESS-REAL-YEARS-001, #859). The pull start is the
    ``GROWW_HISTORY_FLOOR`` (2020-01-01), so the window ledger PLANS a window for every
    trading day from 2020. A LATE-listed / late-data symbol — JIOFIN 2023-08-21,
    SHRIRAMFIN 2022-12-20, TMPV 2025-10-24, and even one that starts later WITHIN 2020 like
    MAXHEALTH 2020-08-21 or TATACONSUM 2020-02-27 — has NO vendor bars before its first bar;
    those pre-start windows still RESOLVE (the vendor honestly returns empty), so every
    planned window is settled and the base verdict reads ``full``. But ``full`` must mean
    SOLID 2020-floor coverage — data from the START of 2020, the strong evidence the
    deep-drill brute-force backtest needs. So a ``full`` whose covered range does NOT begin in
    JANUARY 2020 (``from_2020`` is False — the earliest bar is after January 2020, OR there
    are no bars at all) is HONESTLY downgraded to ``partial`` (the symbol HAS 1-minute data,
    just not from the floor). ``partial`` / ``failed`` / ``none`` already carry no positive
    2020 claim and pass through unchanged. One source of truth (CLAUDE.md §11 COMMON) for BOTH
    the scoreboard rollup and the per-symbol detail.
    """
    verdict = _spot_dimension_verdict(completeness)
    if verdict == DIM_FULL and not _from_2020(first_date):
        return DIM_PARTIAL
    return verdict


def _spot_dimension_detail(row: Mapping[str, Any]) -> dict[str, Any]:
    """One spot dimension's verdict + EXPLICIT first/last bar dates + the ``from_2020`` flag.

    The verdict honours the 2020-floor downgrade (DEC-SPOT-COMPLETENESS-2020-FLOOR-001): a
    settled-but-late-start dimension reads ``partial``, not ``full`` — the ``from_2020`` flag
    and the explicit first/last dates disambiguate WHY.

    Daily-open substitution honesty (DEC-DAILY-OPEN-SUBSTITUTION-HONESTY-001): the detail
    additionally carries ``open_substituted_days`` (how many stored sessions' first present
    1-minute bar is strictly after the 09:15 IST canonical open — so their DERIVED 1-day open
    is the earliest-present-minute substitute, not the canonical open price),
    ``session_days`` (the stored-session denominator) and the ``open_substituted`` flag
    (``days > 0``). INFORMATIONAL only — the verdict is NEVER downgraded by a substitution
    (the data exists; only its daily open is a substitute). A row without the counts (a
    hand-built / legacy shape) defaults to the honest 0/0/False.
    """
    first = row.get("first_date")
    last = row.get("last_date")
    first_s = str(first) if first else None
    open_substituted_days = int(row.get("open_substituted_days") or 0)
    return {
        "status": _spot_dimension_verdict_with_floor(row.get("completeness"), first_s),
        "first_date": first_s,
        "last_date": str(last) if last else None,
        "from_2020": _from_2020(first_s),
        "open_substituted_days": open_substituted_days,
        "session_days": int(row.get("session_days") or 0),
        "open_substituted": open_substituted_days > 0,
    }


def _new_per_symbol_entry(symbol: str, exchange: str, category: str) -> dict[str, Any]:
    """A per-symbol completeness entry pre-seeded to the honest "no data" defaults."""
    none_dim: dict[str, Any] = {
        "status": DIM_NONE,
        "first_date": None,
        "last_date": None,
        "from_2020": False,
        # Daily-open substitution honesty (DEC-DAILY-OPEN-SUBSTITUTION-HONESTY-001):
        # no data ⇒ nothing observed ⇒ the honest 0/0/False defaults.
        "open_substituted_days": 0,
        "session_days": 0,
        "open_substituted": False,
    }
    return {
        "symbol": symbol,
        "exchange": exchange,
        "category": category,
        "spot": {"1minute": dict(none_dim), "overall": DIM_NONE},
        "options": {
            "status": OPT_NONE,
            "expiries_pulled": 0,
            "contracts_pulled": 0,
            # OPT_NONE never carries the by-design note (no chain attempted) — empty.
            "contracts_note": "",
            "years": [],
            "first_year": None,
            "last_year": None,
        },
    }


def per_symbol_completeness(
    spot_rows: Iterable[Mapping[str, Any]],
    spot_categories: Mapping[str, str],
    option_records: Iterable[Any],
    option_categories: Mapping[str, str],
) -> dict[str, dict[str, Any]]:
    """Per-symbol completeness map (``symbol_name → detail``) WITH explicit first/last dates.

    DEC-COMPLETENESS-UNIVERSE-001 — the data behind the ``/universe`` per-row completeness strip
    that answers, at pull time, *does THIS symbol have the entire 2020→today set?* For each
    symbol it carries, for the single fetched spot dimension (``1minute``), the verdict + the
    REAL earliest/latest bar date + the ``from_2020`` flag; and per option underlying the honest
    ``stable``/``growing``/``partial``/``failed`` status + pulled counts + the year span. Keyed
    by ``symbol_name`` — the SAME key the ``/universe`` rows carry (``i.symbol``), the spot
    datasets carry (``symbol``), and the option records carry (``underlying_symbol``) — so the
    page joins it O(1) per row.

    Pure re-aggregation over the route-supplied inputs (no lake/store/network), so 100%
    unit-testable offline; reuses the shared verdict helpers (no re-derivation, CLAUDE.md §11
    COMMON). A symbol with only spot OR only option data gets the absent side's honest defaults;
    an unmapped symbol / a derived intraday TF is skipped. O(spot_rows + option_records).
    """
    out: dict[str, dict[str, Any]] = {}
    for row in spot_rows:
        exchange = str(row.get("exchange", ""))
        # Recover a drifted lake key to its canonical scored symbol (the SAME COMMON resolver
        # the /completeness scoreboard uses — DEC-COMPLETENESS-SPOT-KEY-RECOVER-001), so the
        # /universe per-row strip ALSO surfaces a drifted partition instead of reading empty.
        resolved = _resolve_spot_category(str(row["symbol"]), exchange, spot_categories)
        if resolved is None:
            continue  # not a scored universe symbol
        symbol, category = resolved  # canonical scored key (drift recovered)
        interval = str(row["interval"])
        if interval not in SPOT_DIMENSIONS:
            continue  # a derived intraday TF — not a separately-pulled dimension
        entry = out.setdefault(symbol, _new_per_symbol_entry(symbol, exchange, category))
        detail = _spot_dimension_detail(row)
        existing = entry["spot"][interval]
        # Rank-merge the recovered-drift collision (DEC-COMPLETENESS-SPOT-KEY-RECOVER-001): keep
        # the strictly-better cell so a stale drifted fragment never clobbers real data here too.
        if _spot_cell_supersedes(
            detail["status"], detail["first_date"], existing["status"], existing["first_date"]
        ):
            entry["spot"][interval] = detail
    for entry in out.values():
        entry["spot"]["overall"] = _overall_spot(entry["spot"]["1minute"]["status"])
    _fold_per_symbol_options(out, option_records, option_categories)
    return out


def _fold_per_symbol_options(
    out: dict[str, dict[str, Any]],
    option_records: Iterable[Any],
    option_categories: Mapping[str, str],
) -> None:
    """Fold option coverage records into the per-symbol map (status + counts + year span)."""
    # Grouped by ``underlying`` ALONE (intentionally NOT ``(underlying, exch)`` like the
    # scoreboard's :func:`_per_underlying_options`): the ``/universe`` strip joins one row per
    # ``symbol_name`` (``COV[i.symbol]``), so a per-symbol rollup is the right granularity here.
    # If an underlying ever lists options on two exchanges, the counts sum across both and the
    # first record's ``exch_id`` is kept (exchange is display-only on the strip) — harmless.
    grouped: dict[str, dict[str, Any]] = {}
    for rec in option_records:
        underlying = str(rec.underlying_symbol)
        category = option_categories.get(underlying)
        if category is None:
            continue
        acc = grouped.setdefault(
            underlying,
            {
                "exchange": str(rec.exch_id),
                "category": category,
                "expiries": 0,
                "contracts": 0,
                "years": [],
                "cells": [],
            },
        )
        expiries = int(rec.expiries_pulled)
        acc["expiries"] += expiries
        acc["contracts"] += int(rec.contracts_pulled)
        acc["cells"].append((str(rec.status), expiries))
        # Only REAL-expiry years enter the displayed span (DEC-OPTIONS-COMPLETENESS-REAL-YEARS
        # -001) — a 0-expiry pre-listing year is excluded, so a late-listed underlying shows
        # only its real years, never the full requested range.
        if expiries > 0:
            acc["years"].append(int(rec.year))
    for underlying, acc in grouped.items():
        # ``years`` may be EMPTY when every requested year returned zero expiries (an underlying
        # with no option history in the grid) — first/last_year then stay None (the verdict is
        # OPT_NONE), mirroring the absent-options default.
        years = sorted(acc["years"])
        entry = out.setdefault(
            underlying, _new_per_symbol_entry(underlying, acc["exchange"], acc["category"])
        )
        status = _option_verdict(acc["cells"])
        entry["options"] = {
            "status": status,
            "expiries_pulled": acc["expiries"],
            "contracts_pulled": acc["contracts"],
            # Honest annotation: a 0-contract expiries_only underlying is metadata-only
            # BY DESIGN (legs discarded), not broken (DEC-METADATA-LIVE-PROGRESS-001).
            "contracts_note": _contracts_note(status, acc["contracts"]),
            "years": years,
            "first_year": years[0] if years else None,
            "last_year": years[-1] if years else None,
        }


def _aggregate_spot(per_symbol: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """Count, per category, how many symbols are FULL on the 1-minute spot dimension.

    1-minute is the single fetched granularity (DEC-1MINUTE-ONLY-DERIVE-1DAY-001) so there is
    one ``full_1minute`` count per category; the prior ``full_1day`` / ``full_both`` buckets
    were dropped with the derived 1-day column. Additionally sums the per-symbol
    ``open_substituted_days`` into the category bucket
    (DEC-DAILY-OPEN-SUBSTITUTION-HONESTY-001) — the informational "how many stored sessions
    across this category carry a substituted derived daily open" tally, never a verdict input.
    """
    agg: dict[str, dict[str, int]] = {
        cat: {"total": 0, "full_1minute": 0, "open_substituted_days": 0} for cat in CATEGORIES
    }
    for entry in per_symbol:
        bucket = agg[entry["category"]]
        bucket["total"] += 1
        bucket["open_substituted_days"] += int(entry.get("open_substituted_days") or 0)
        if entry["1minute"] == DIM_FULL:
            bucket["full_1minute"] += 1
    return agg


def _aggregate_options(per_underlying: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """Count, per category, how many underlyings have a STABLE (complete) option chain."""
    agg: dict[str, dict[str, int]] = {
        cat: {"total": 0, "stable": 0, "growing_or_partial": 0, "failed": 0} for cat in CATEGORIES
    }
    for entry in per_underlying:
        bucket = agg[entry["category"]]
        bucket["total"] += 1
        status = entry["status"]
        if status == OPT_STABLE:
            bucket["stable"] += 1
        elif status == OPT_FAILED:
            bucket["failed"] += 1
        elif status in (OPT_GROWING, OPT_PARTIAL):
            bucket["growing_or_partial"] += 1
    return agg


def categorize_instruments(
    instruments: Iterable[Any],
    *,
    is_index: Callable[[Any], bool],
    is_reference: Callable[[Any], bool],
) -> dict[str, str]:
    """Map each spot instrument's primary symbol → its scoreboard category.

    Keyed by ``symbol_name`` — the engine's primary symbol, which IS the spot window ledger's
    ``security_id`` (the key the annotated spot rows AND the option coverage records carry),
    so one map serves both. The classification predicates are INJECTED (the route passes
    :func:`~brutex.universe.is_in_brute_force_scope` + :func:`~brutex.universe.is_reference_only`)
    so this stays pure + 100% unit-testable without constructing a full
    :class:`~brutex.models.Instrument`. Classification is checked in PRECEDENCE order
    (DEC-COMPLETENESS-INDEX-CATEGORY-001): a **reference-only** row (India VIX / NTM
    constituent / spot-only index) → :data:`CATEGORY_REFERENCE` FIRST; else an **index**
    (the injected *is_index* — now the by-TYPE :func:`~brutex.universe.is_index_instrument`,
    so FINNIFTY and every non-brute-forced index is correctly an index, not a cash equity)
    → :data:`CATEGORY_INDEX`; otherwise (an F&O-eligible or cash equity) →
    :data:`CATEGORY_EQUITY`. O(instruments).

    Reference is checked BEFORE index because a reference-only index (India VIX) satisfies
    BOTH predicates by type — reference must win so VIX stays REFERENCE, never INDEX. For
    the legacy brute-force *is_index* the two sets are disjoint (the engine-isolation
    invariant), so this precedence is a behaviour-preserving no-op there.
    """
    out: dict[str, str] = {}
    for inst in instruments:
        if is_reference(inst):
            out[inst.symbol_name] = CATEGORY_REFERENCE
        elif is_index(inst):
            out[inst.symbol_name] = CATEGORY_INDEX
        else:
            out[inst.symbol_name] = CATEGORY_EQUITY
    return out


def build_completeness_scoreboard(
    spot_rows: Iterable[Mapping[str, Any]],
    spot_categories: Mapping[str, str],
    option_records: Iterable[Any],
    option_categories: Mapping[str, str],
) -> dict[str, Any]:
    """Compose the full Data Completeness Scoreboard payload (aggregate + per-symbol).

    DEC-COMPLETENESS-SCOREBOARD-001. Pure projection — every input is supplied by the route
    (annotated spot rows, the symbol→category maps, the option coverage records), so this is
    100% unit-testable offline.

    Parameters
    ----------
    spot_rows
        Annotated rows from
        :func:`~brutex.dashboard.coverage_view.build_spot_coverage_payload` (``symbol`` ·
        ``interval`` · ``completeness`` · ``exchange``).
    spot_categories
        ``symbol → {index|equity|reference}`` for every scored spot symbol.
    option_records
        :class:`~brutex.data.coverage_ledger.CoverageRecord` rows (per underlying + year).
    option_categories
        ``underlying → {index|equity|reference}`` for every scored option underlying.

    Returns
    -------
    dict[str, Any]
        ``{"spot": {"by_category", "per_symbol"}, "options": {"by_category", "per_underlying"}}``
        where ``by_category`` is the aggregate count table and the lists are the drill-down rows.
    """
    per_symbol = _per_symbol_spot(spot_rows, spot_categories)
    per_underlying = _per_underlying_options(option_records, option_categories)
    return {
        "spot": {"by_category": _aggregate_spot(per_symbol), "per_symbol": per_symbol},
        "options": {
            "by_category": _aggregate_options(per_underlying),
            "per_underlying": per_underlying,
        },
    }


def _csv_date(value: object) -> str:
    """An ISO date string, or ``''`` for a missing/None bar date (a clean empty Excel cell)."""
    return str(value) if value else ""


def _csv_yes_no(flag: object) -> str:
    """Human ``yes``/``no`` for a ``from_2020`` boolean (``no`` for a falsy/absent dimension)."""
    return "yes" if flag else "no"


def scoreboard_to_csv(
    board: Mapping[str, Any],
    per_symbol: Mapping[str, Mapping[str, Any]] | None = None,
) -> str:
    """Render a :func:`build_completeness_scoreboard` payload as a downloadable CSV report.

    DEC-COMPLETENESS-PAGE-001 — the "even a human can understand it in Excel" report
    behind the ``/api/v1/completeness/scoreboard.csv`` download. A single pure
    string-builder over the already-composed scoreboard (no lake / store / network), so it
    is 100% unit-testable offline and adds no new derivation (CLAUDE.md §11 COMMON — it only
    re-serialises the engine's own output).

    The file is a **four-section human report** (each section a self-describing title row +
    its own header, blank-line separated) that answers the operator's question directly —
    *how many symbols from indices vs equities-cash, SEPARATELY, have the full dataset* — and
    then the per-symbol / per-underlying drill-down:

    1. ``SUMMARY — spot`` — per category: ``total``, and how many symbols are FULL on 1-minute
       (EXACT, window-ledger backed; 1-day is DERIVED from 1-minute so it is not scored).
    2. ``SUMMARY — options`` — per category: ``total``, ``stable`` (the honest "complete"),
       ``growing_or_partial``, ``failed`` (NO fabricated %, per CLAUDE.md §6).
    3. ``DETAIL — spot`` — one row per symbol with its 1-minute / overall verdict;
       when *per_symbol* (a :func:`per_symbol_completeness` map) is supplied the row is
       ENRICHED with the explicit first→last bar date + ``from_2020`` flag for the 1-minute
       dimension (DEC-COMPLETENESS-CSV-READABLE-001 — the SAME rich info the ``/universe``
       strip shows, so the CSV is never less informative than the page); ``None`` keeps the
       compact verdict-only columns (backward-compatible).
    4. ``DETAIL — options`` — one row per underlying with status + pulled counts + the years.

    Uses :mod:`csv` so any symbol with a comma / quote is escaped correctly (an extreme-input
    guard); ``\\n`` line terminator for deterministic, platform-stable output. Every cell is
    additionally run through :class:`_FormulaSafeWriter` so a value starting with ``= + - @``
    (or a leading TAB / CR) can never execute as a spreadsheet formula when the report is
    opened in Excel / Sheets (DEC-CSV-FORMULA-INJECTION-001).
    """
    buf = io.StringIO()
    writer = _FormulaSafeWriter(csv.writer(buf, lineterminator="\n"))

    writer.writerow(["BruteX — Data Completeness Scoreboard"])
    writer.writerow(
        [
            "spot completeness is EXACT (window-ledger); option status is "
            "stable/growing/partial/failed — no fabricated % (vendor exposes no expected total)"
        ]
    )

    spot_by_cat = board["spot"]["by_category"]
    writer.writerow([])
    writer.writerow(["SUMMARY — spot: how many symbols have the FULL dataset per asset class"])
    writer.writerow(["Asset class", "Symbols", "Full 1-minute"])
    for cat in CATEGORIES:
        bucket = spot_by_cat[cat]
        writer.writerow([cat, bucket["total"], bucket["full_1minute"]])

    opt_by_cat = board["options"]["by_category"]
    writer.writerow([])
    writer.writerow(["SUMMARY — options: underlyings per chain status per asset class"])
    writer.writerow(
        ["Asset class", "Underlyings", "Stable (complete)", "Growing or partial", "Failed"]
    )
    for cat in CATEGORIES:
        bucket = opt_by_cat[cat]
        writer.writerow(
            [cat, bucket["total"], bucket["stable"], bucket["growing_or_partial"], bucket["failed"]]
        )

    writer.writerow([])
    if per_symbol is None:
        writer.writerow(["DETAIL — spot (per symbol)"])
        writer.writerow(
            [
                "Asset class",
                "Symbol",
                "Exchange",
                "1-minute complete?",
                # Daily-open substitution honesty (DEC-DAILY-OPEN-SUBSTITUTION-HONESTY-001):
                # informational count of sessions whose derived 1-day open is a substituted
                # (first-present-minute) price — never a verdict downgrade.
                "1-minute open-substituted days",
                "Overall",
            ]
        )
        for row in board["spot"]["per_symbol"]:
            writer.writerow(
                [
                    row["category"],
                    row["symbol"],
                    row["exchange"],
                    row["1minute"],
                    int(row.get("open_substituted_days") or 0),
                    row["overall"],
                ]
            )
    else:
        writer.writerow(["DETAIL — spot (per symbol with first→last bar dates)"])
        writer.writerow(
            [
                "Asset class",
                "Symbol",
                "Exchange",
                "1-minute complete?",
                "1-minute first bar",
                "1-minute last bar",
                "1-minute from 2020?",
                # Daily-open substitution honesty (DEC-DAILY-OPEN-SUBSTITUTION-HONESTY-001):
                # N substituted-open sessions out of M stored sessions — informational only.
                "1-minute open-substituted days",
                "1-minute sessions",
                "Overall",
            ]
        )
        for row in board["spot"]["per_symbol"]:
            spot = per_symbol.get(row["symbol"], {}).get("spot", {})
            one_min = spot.get("1minute", {})
            writer.writerow(
                [
                    row["category"],
                    row["symbol"],
                    row["exchange"],
                    row["1minute"],
                    _csv_date(one_min.get("first_date")),
                    _csv_date(one_min.get("last_date")),
                    _csv_yes_no(one_min.get("from_2020")),
                    int(one_min.get("open_substituted_days") or 0),
                    int(one_min.get("session_days") or 0),
                    row["overall"],
                ]
            )

    writer.writerow([])
    writer.writerow(["DETAIL — options (per underlying)"])
    writer.writerow(
        [
            "Asset class",
            "Underlying",
            "Exchange",
            "Chain status",
            "Expiries pulled",
            "Contracts pulled",
            "Years",
            "Notes",
        ]
    )
    for row in board["options"]["per_underlying"]:
        writer.writerow(
            [
                row["category"],
                row["underlying"],
                row["exchange"],
                row["status"],
                row["expiries_pulled"],
                row["contracts_pulled"],
                " ".join(str(year) for year in row["years"]),
                # Honest "0 contracts = metadata-only by design" note when applicable
                # (DEC-METADATA-LIVE-PROGRESS-001); empty otherwise.
                row.get("contracts_note", ""),
            ]
        )

    return buf.getvalue()


__all__ = [
    "CATEGORIES",
    "CATEGORY_EQUITY",
    "CATEGORY_INDEX",
    "CATEGORY_REFERENCE",
    "DIM_FAILED",
    "DIM_FULL",
    "DIM_NONE",
    "DIM_PARTIAL",
    "OPT_CONTRACTS_BY_DESIGN_NOTE",
    "OPT_FAILED",
    "OPT_GROWING",
    "OPT_NONE",
    "OPT_PARTIAL",
    "OPT_STABLE",
    "RECONCILE_MISMATCH_EVENT",
    "SPOT_DIMENSIONS",
    "SymbolReconcileReport",
    "build_completeness_scoreboard",
    "categorize_instruments",
    "log_symbol_reconcile_from_board",
    "per_symbol_completeness",
    "reconcile_symbol_coverage",
    "scoreboard_to_csv",
]
