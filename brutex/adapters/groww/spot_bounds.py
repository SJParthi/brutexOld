"""Per-symbol minute-pull window type for the SPOT ingest spine.

DEC-SPOT-INTERVAL-DECOUPLE-001 (MIGRATION.md entry #851) — the SPOT 1-minute
pull is now **fully decoupled** from the 1-day coverage. The 1-minute phase
pulls its full ``[start, end]`` range (the configured floor → today) for EVERY
requested symbol — no coverage discovery, no per-symbol filtering, and no
per-symbol minute-start bounding. 1-minute is now the ONLY fetched granularity
and the ``1day`` series is DERIVED from it
(``DEC-1MINUTE-ONLY-DERIVE-1DAY-001``); only the historical 1-minute↔1-day
COUPLING is removed here.

This SUPERSEDES the 1-minute-bounded-to-1-day-coverage coupling of
DEC-SPOT-1DAY-FIRST-001 (MIGRATION.md #836). The coupled discovery driver
(``discover_spot_minute_windows``), the pure coverage→window mapper
(``spot_coverage_to_minute_window``), and the lake-read helper
(``spot_daily_coverage``) were all deleted in the decouple cutover — the minute
phase no longer reads 1-day coverage at all.

What survives is the :data:`SpotWindows` type alias: the OPTIONAL per-symbol
``{symbol_name: (start, end)}`` map threaded through the ingest spine
(:func:`~brutex.adapters.groww.run_ingest._resolve_spot_window`) and the window
ledger (:func:`~brutex.data.window_ledger_store.WindowLedger.plan_windows`). The
SPOT pull paths now always pass ``None`` (the decoupled, full-range mode), so a
sub-daily pull runs over the unbounded ``[start, end)`` for every symbol; the
alias is kept because the spine + ledger signatures still accept the opt-in map
(the never-narrow fallback when a non-spot caller supplies one).

Scope (CLAUDE.md §3)
--------------------
This lives in the adapter/data layer and NEVER touches the brute-force engine or
the shared condition vocabulary.
"""

from __future__ import annotations

from datetime import date

__all__ = [
    "SpotWindows",
]

#: Per-symbol ``{symbol_name: (start, end)}`` minute-window map — the spot analog
#: of options' ``contract_windows``. With DEC-SPOT-INTERVAL-DECOUPLE-001 the SPOT
#: pull paths pass ``None`` (the full-range decoupled mode); the alias is retained
#: because the ingest spine + window ledger still accept the opt-in map (the
#: never-narrow fallback for any caller that does supply one).
SpotWindows = dict[str, tuple[date, date]]
