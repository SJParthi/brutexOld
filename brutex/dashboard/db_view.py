"""Read-only DuckDB viewer — table summaries + sample rows (DEC-DB-VIEWER-001).

Pure, FastAPI-free helpers powering the ``/db`` dashboard page so an
operator can answer "where is the DB, how can I see it, what is the
schema — every nook and corner" from the browser instead of reaching
for the ``duckdb`` CLI. Per CLAUDE.md §11 (COMMON) all real logic lives
here in exactly ONE place; the dashboard FastAPI routes in
:mod:`brutex.dashboard.app` (coverage-excluded, FastAPI-dependent) stay
thin wrappers that delegate here so this surface is 100% unit-testable
offline against a real :class:`~brutex.data.instrument_store.InstrumentStore`.

## Read-only by construction

Every query this module issues is a ``SELECT`` (a ``COUNT(*)`` or a
``SELECT * ... LIMIT ?``). There is no code path that writes, so even a
hostile caller cannot mutate the store through the viewer. The store's
audit + idempotence guarantees (CLAUDE.md §7/§8) are never touched.

## SQL-injection guard (Golden Rule #1 / §4)

DuckDB cannot parameter-bind an *identifier* (a table name), so the
table name is interpolated into the SQL string. To make that safe the
caller-supplied table name is checked for membership in
:data:`KNOWN_TABLES` (a fixed allowlist of the 5 real store tables)
*before* it is ever placed into a query. A name that is not an exact
member raises :class:`ValueError` and NO SQL is executed — so a payload
such as ``"instruments; DROP TABLE contracts"`` is rejected, never run.
The row ``limit`` IS parameter-bound (``LIMIT ?``) and additionally
clamped to ``[1, 500]`` so a hostile / malformed query string can never
trigger an unbounded read.
"""

from __future__ import annotations

import contextlib
import hashlib
import math
import os
import re
import threading
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

# ALIASED on purpose — do NOT "simplify" to `time.sleep`: the bare name `time` in this
# module is `datetime.time` (imported above), so `time.sleep(...)` is a runtime
# AttributeError. The alias keeps the torn-read retry (DEC-WINDOW-TORN-READ-RETRY-001)
# unambiguous and injectable-for-tests via the `retry_sleep` parameters below.
from time import monotonic as _monotonic_clock
from time import sleep as _default_sleep
from typing import Any, Final, Protocol, TypeVar
from zoneinfo import ZoneInfo

import duckdb
import structlog

from brutex.adapters.groww.constants import (
    GROWW_FUTURE_COVERAGE_MARKET,
    GROWW_OPTION_COVERAGE_MARKET,
    TRACK1_SPOT_INTERVAL,
)
from brutex.adapters.ingest_common import GROWW_COVERAGE_MARKET
from brutex.calendars.nse import (
    IST,
    IST_OFFSET_MINUTES,
    MUHURAT_SESSIONS,
    previous_trading_session,
)
from brutex.config.feed_scope import FeedScope
from brutex.config.hardware_envelope import memory_budget_bytes

# NO lake_revision import — module-level OR lazy (AST-pinned by
# ``test_db_view_no_lake_revision_import``): lake_revision lazily imports
# window_ledger_store → coverage_reconcile → db_view, so any db_view →
# lake_revision edge closes an import cycle (CodeQL py/cyclic-import, alert
# 690). The token→generation digest bridge this module needs is therefore
# homed in the movers_cache LEAF (DEC-LEDGER-DELTA-O-PAGE-001).
from brutex.dashboard.movers_cache import MoversSnapshotCache, revision_generation
from brutex.dashboard.movers_resilient import movers_read_skip_errors
from brutex.dashboard.registry_strikes import load_registry_strike_chain
from brutex.data.bar_screen import screen_rows
from brutex.data.errors import CatalogError, LakeWriteHalt
from brutex.data.fno_candle_lake import (
    FNO_CANDLE_COLUMNS,
    FNO_CANDLE_LAKE_SEGMENT,
)
from brutex.data.fno_symbol import parse_fno_symbol
from brutex.data.greeks_provenance import greeks_status_label
from brutex.data.lake_keys import StockDriftMap, spot_recovery_candidates
from brutex.data.lake_manifest import (
    ledger_stat_token,
    load_manifest_datasets,
    rebuild_manifest,
)
from brutex.data.lake_read import (
    DEDUP_PARQUET_SOURCE_SQL,
    DEDUP_STAR_SELECT_SQL,
    dedup_latest_source_sql,
    dedup_qualify_sql,
    distinct_count_sql,
)
from brutex.data.lake_stats_manifest import (
    FALLBACK_REL_LOG_CAP,
    KIND_BARS,
    KIND_OPTIONS,
    TOKEN_SWEEP_MOVED,
    TOKEN_SWEEP_OK,
    DatasetStats,
    StatsFileRow,
    bounded_token_sweep,
    load_valid_dataset_stats_guarded,
    parse_rel_month,
    rel_month_bounds,
    scan_dataset_rels,
    seed_dataset_stats,
    stat_token,
    stats_manifest_enabled,
)
from brutex.data.models import GREEKS_OVERLAY_FIELDS
from brutex.data.moneyness import (
    StepConfidence,
    StrikeStepResult,
    moneyness_view,
    resolve_strike_step,
)
from brutex.data.partition import (
    SPOT_PROBE_SEGMENTS,
    VALID_EXCHANGES,
    VALID_INTERVALS,
    VALID_SEGMENTS,
)
from brutex.data.scope_filter import (
    WINDOW_ADMITTED,
    WINDOW_LEGACY_UNRESOLVED,
    classify_window_row,
    scope_admits_dataset,
    scope_admits_fno_contract,
    scope_admitted_symbol_names,
)
from brutex.data.session import SESSION_OPEN, format_ist_clock
from brutex.data.spot_features import pct_change
from brutex.data.spot_join import utc_instant
from brutex.data.today_completeness import TODAY_INCOMPLETE
from brutex.data.window_ledger import (
    DEMOTED_SETTLE_REASONS,
    EMPTY_TERMINAL_THRESHOLD,
    is_expired_window_settle_reason,
)
from brutex.models import Exchange
from brutex.money import round_money
from brutex.observability.progress_beat import emit_progress_beat

#: Structured logger — surfaces the otherwise-silent coverage-read degradation
#: (a single corrupt / version-incompatible Parquet file is SKIPPED, logged as
#: ``coverage.parquet_skip``, instead of 500-ing the whole endpoint) into the
#: same structlog stream the operator reads on ``/logs``
#: (DEC-COVERAGE-PARQUET-RESILIENT-001).
_LOG = structlog.get_logger("brutex.dashboard.db_view")

#: Memoised-value type for the generation-stamped candle cache
#: (:class:`CandleCountCache`): the SAME injected instance serves both the page
#: ``total`` (an ``int``) and the paging-invariant stored-life span (a
#: ``(start, end)`` tuple) — DEC-DB-UX-PACK-001.
_T = TypeVar("_T")

#: The ONLY tables the viewer may touch — the real DuckDB tables created by
#: :meth:`brutex.data.instrument_store.InstrumentStore.open`. A
#: caller-supplied table name is checked against this allowlist before
#: it is interpolated into any SQL string (SQL-injection guard). The retired
#: ``expiries`` / ``contracts`` tables were dropped in
#: DEC-EXPIRED-OPTIONS-TABLE-001 — the expired-options Parquet lake
#: (``<lake>/options/…``, browsed via :func:`list_option_datasets` +
#: :func:`query_option_candles`) is their replacement.
KNOWN_TABLES: Final = (
    "instruments",
    "instrument_runs",
    "schema_meta",
)

#: Structured-query operators (DEC-DB-QUERY-001). Each maps a stable UI
#: token to a (SQL operator, value-transform) pair. The SQL operator is a
#: hardcoded literal from this fixed table — NEVER caller text — and the
#: comparison value is ALWAYS parameter-bound (``?``), so the WHERE clause
#: carries zero SQL-injection surface. ``contains`` wraps the bound value
#: in ``%…%`` for a substring LIKE; ``like`` passes the operator's pattern
#: through verbatim (the operator types their own ``%``).
ALLOWED_OPERATORS: Final[dict[str, str]] = {
    "eq": "=",
    "ne": "!=",
    "lt": "<",
    "le": "<=",
    "gt": ">",
    "ge": ">=",
    "like": "LIKE",
    "contains": "LIKE",
}

#: The 7 canonical OHLCV columns every candle Parquet partition carries
#: (``brutex/data/parquet_writer.py`` ``_render_bytes`` schema). Used to
#: validate a candle-viewer column selection without touching disk.
CANDLE_COLUMNS: Final = (
    "timestamp",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "open_interest",
)

#: The 10 STORED per-bar Greeks overlay columns an **F&O** candle page may
#: additionally project (DEC-GREEKS-ENGINE-PR3-001) — written at generation time
#: by the Phase-2b enrichment (DEC-GREEKS-ENGINE-PR2B-001,
#: :data:`brutex.data.models.GREEKS_OVERLAY_FIELDS` — the ONE source of truth,
#: re-exported here so the viewer can never drift from the writer schema;
#: CLAUDE.md §11 COMMON). Mapped to each column's DuckDB SQL type so a LEGACY
#: pre-Greeks partition tree (7-column files only — no file carries the overlay)
#: can be projected schema-tolerantly as typed NULLs instead of a binder error:
#: within a MIXED tree ``union_by_name=true`` already inserts NULLs for the
#: files lacking a column, but when NO file has it DuckDB (correctly) refuses
#: the reference, so :func:`query_candles` probes the physical union schema once
#: and substitutes ``CAST(NULL AS <type>) AS <col>`` for the absent ones —
#: honest NULL cells, never a fabricated Greek (Golden Rule #1). Spot pages
#: never accept these columns (the overlay exists only under ``segment="FNO"``).
FNO_GREEKS_COLUMN_TYPES: Final[dict[str, str]] = {
    "iv": "DOUBLE",
    "delta": "DOUBLE",
    "gamma": "DOUBLE",
    "theta": "DOUBLE",
    "vega": "DOUBLE",
    "rho": "DOUBLE",
    "spot_at_bar": "DOUBLE",
    "t_years_used": "DOUBLE",
    "rate_used": "DOUBLE",
    "greeks_provenance_id": "INTEGER",
}

#: The selectable stored-overlay column tuple, in writer order (a verbatim
#: re-export of :data:`brutex.data.models.GREEKS_OVERLAY_FIELDS`).
FNO_GREEKS_COLUMNS: Final = GREEKS_OVERLAY_FIELDS

#: The 18 columns every expired-option Parquet partition carries
#: (``brutex/data/option_parquet_writer.py`` ``_render_df`` schema,
#: DEC-EXPIRED-OPTIONS-TABLE-001): the 7 series-addressing columns promoted
#: from the partition path (``exch_id`` / ``exchange_segment`` /
#: ``security_id`` / ``underlying_symbol`` / ``expiry_flag`` / ``expiry_code``
#: / ``strike_offset``) PLUS the per-bar ``side`` and the OHLCV+OI value set
#: PLUS the per-bar option stamps ``strike`` / ``spot`` / ``iv`` (the
#: rolling-option endpoint surface). Used to validate an option-candle column
#: selection without touching disk.
OPTION_CANDLE_COLUMNS: Final = (
    "exch_id",
    "exchange_segment",
    "security_id",
    "underlying_symbol",
    "expiry_flag",
    "expiry_code",
    "side",
    "strike_offset",
    "timestamp",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "open_interest",
    "strike",
    "spot",
    "iv",
)

#: The subdirectory under the lake root holding the expired-options table
#: (``<lake>/options/<series>/<YYYY>-<MM>.parquet``). Mirrors
#: ``brutex.data.option_parquet_writer._OPTIONS_SUBDIR`` (kept in lockstep) so
#: the read-side viewer never traverses outside ``<lake>/options``.
OPTIONS_DIR_NAME: Final = "options"

#: The F&O expiries+contracts REGISTRY cache file
#: (:class:`brutex.data.registry_store.RegistryStore`,
#: DEC-DERIV-METADATA-HISTORICAL-SPLIT-001) — a SIBLING of the lean instrument
#: master (``instruments.duckdb``), so it lives in the SAME directory (the lake
#: root). :func:`registry_path_for` resolves it from the master's on-disk path
#: the same way the candle/option readers resolve the lake root, keeping the
#: ``/db`` page's path-resolution discipline in one place (CLAUDE.md §11 COMMON).
REGISTRY_DB_NAME: Final = "registry.duckdb"

#: The 7 browsable columns surfaced for the registry ``expiries`` table — the
#: ``expiries`` schema minus ``last_run_id`` (an opaque blake3 run id, noise for
#: the operator), in a stable display order. ``year`` is the derived Phase-1 cell
#: key (:attr:`brutex.data.registry_store.ExpiryRow.year`).
REGISTRY_EXPIRY_COLUMNS: Final = (
    "exch",
    "underlying",
    "expiry_date",
    "year",
    "fetched_at",
)

#: The 8 browsable columns surfaced for the registry ``contracts`` table — the
#: structured chain view the operator asked for (expiry / strike / CE-PE /
#: per-contract historical ``candle_status``), ``last_run_id`` again omitted.
REGISTRY_CONTRACT_COLUMNS: Final = (
    "exch",
    "underlying",
    "expiry_date",
    "contract_symbol",
    "strike",
    "side",
    "candle_status",
    "fetched_at",
)

#: A filesystem-safe option series segment is the writer's ``_safe_segment``
#: output: a ``|``-joined series key with ``|`` rewritten to ``__``. The real
#: parts are alnum + ``.`` + ``-`` + ``+`` (vendor coordinates) joined by
#: ``__``; we allow exactly that grammar so a discovered directory name can
#: never carry a traversal/separator character into the read glob (``..``
#: rejected separately, matching the writer's own guard).
#:
#: ``+`` is admitted because the writer stores it verbatim
#: (DEC-DB-OPTION-DATASET-DECODE-001): the upper half of every strike chain
#: uses the rolling-option ``ATM+N`` selector (``option_universe_ingest``
#: emits ``ATM+1`` … ``ATM+10``), and ``_safe_segment`` only rewrites ``|`` —
#: so the lake holds ``…__ATM+3__CALL`` directories. Before this fix those
#: series were silently SKIPPED by :func:`list_option_datasets` AND refused by
#: :func:`query_option_candles` (the read allowlist dropped what the write
#: path stores). ``+`` is neither a path separator nor a DuckDB-glob
#: metacharacter (``* ? [ ] { }``), so admitting it cannot widen the bound
#: ``read_parquet`` glob or traverse the tree — the same allowlist-consistency
#: class as DEC-PREOPEN-AMPERSAND-001 (``&``) and DEC-PREOPEN-SPACE-SYMBOL-001
#: (space) on the spot side (CLAUDE.md §11 COMMON).
_OPTION_SEGMENT_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+\-]*$")

#: A candle symbol must match this allowlist before it is ever joined into
#: a lake path / ``read_parquet`` glob — defence-in-depth against path
#: traversal even though discovered symbols come from our own directory
#: scan. Vendor symbols are alnum + ``-`` + ``_`` + ``.`` + ``&`` (e.g.
#: ``NSE-NIFTY-30Jan25-28500-CE``, ``M&M``); ``..`` is rejected separately.
#:
#: ``&`` is admitted because real NSE/BSE underlyings legitimately contain it —
#: Mahindra & Mahindra (``M&M`` / ``M&MFIN``) and GE Vernova T&D (``GVT&D``) are
#: F&O-eligible spot symbols the ingest writes verbatim (the partition layer's
#: filesystem denylist in ``brutex.data.partition`` permits ``&``, so the lake
#: holds a ``…/M&M/…`` directory). ``&`` is neither a path separator nor a
#: DuckDB-glob metacharacter (``* ? [ ] { }``), so it cannot widen the bound
#: ``read_parquet`` glob or traverse the tree — admitting it keeps this read
#: allowlist consistent with what the write path stores (CLAUDE.md §11 COMMON)
#: WITHOUT loosening the path-traversal / glob-injection guard. Before this fix
#: the pre-open report 500'd on ``M&M`` (``illegal candle symbol 'M&M'``).
#: DEC-PREOPEN-AMPERSAND-001.
#:
#: A literal **space** is admitted for the same reason (DEC-PREOPEN-SPACE-SYMBOL-001):
#: real INDEX underlyings the lake stores verbatim contain spaces — ``INDIA VIX``,
#: ``NIFTY 50``, ``NIFTY TOTAL MKT``, ``NIFTY MIDCAP 150`` — and a space is neither a
#: path separator nor a DuckDB-glob metacharacter (``* ? [ ] { }``). The symbol is
#: joined via ``pathlib`` into ``<lake>/bars/<exch>/<seg>/<symbol>/<interval>`` and
#: handed to ``read_parquet(?)`` as a **bound parameter** (never string-interpolated),
#: so the space is an inert literal path component — it cannot widen the glob, inject
#: SQL, or traverse (``..`` still rejected). Before this fix the pre-open ``.zip``
#: 500'd on ``INDIA VIX`` (``illegal candle symbol 'INDIA VIX'``), and one bad symbol
#: sank the whole multi-symbol export; admitting the space keeps the read allowlist
#: consistent with what the write path stores (CLAUDE.md §11 COMMON) so no stored
#: data is dropped.
_SYMBOL_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.&\- ]*$")

#: Strict ISO calendar-date shape. ``date.fromisoformat`` (py3.11+) also
#: accepts ``YYYYMMDD`` and ``YYYY-Www-D`` ordinal/week forms; the candle
#: viewer's contract is the plain ``YYYY-MM-DD`` a ``<input type="date">``
#: emits, so anything else is rejected up front.
_ISO_DATE_RE: Final = re.compile(r"^\d{4}-\d{2}-\d{2}$")

#: Lower inclusive bound for the sample-row ``limit``.
_LIMIT_MIN: Final = 1

#: Upper inclusive bound for the sample-row ``limit`` (keeps a hostile
#: ``?limit=999999999`` from materialising a huge result set).
_LIMIT_MAX: Final = 500


def clamp_limit(n: int) -> int:
    """Clamp a requested row limit into the safe ``[1, 500]`` range.

    Parameters
    ----------
    n
        The caller-requested limit (may be ``0``, negative, or absurdly
        large from an untrusted query string).

    Returns
    -------
    int
        ``n`` clamped to ``[_LIMIT_MIN, _LIMIT_MAX]`` — i.e. at least
        ``1`` and at most ``500``.
    """
    return max(_LIMIT_MIN, min(_LIMIT_MAX, n))


#: IST — the market wall-clock the candle viewer renders bar times in. The
#: lake stores bar time as a UTC ``TIMESTAMPTZ``; we display the Indian-market
#: local time explicitly (not the ambient DuckDB session ``TimeZone``) so the
#: rendered string is deterministic regardless of where the server runs. The
#: ONE canonical :data:`brutex.calendars.nse.IST` root
#: (DEC-TIMESTAMP-IST-SINGLESOURCE-001).
_IST: Final[ZoneInfo] = IST

#: The daily-bar partition interval — its bars render DATE-ONLY (no noise
#: ``00:00:00`` suffix) in the candle viewer (DEC-DB-CANDLE-DERIVED-001).
_DAILY_INTERVAL: Final[str] = "1day"

#: The SQL fragment for a bar's **IST wall-clock instant** (a naive TIMESTAMP on
#: the Indian-market clock). Adds the fixed +05:30 IST offset (330 minutes; India
#: has no DST — CLAUDE.md §6) to the UTC wall-clock, so it is independent of the
#: ambient DuckDB session ``TimeZone`` (the operator runs Asia/Kolkata). The
#: minute count is INTERPOLATED from the ONE canonical
#: :data:`brutex.calendars.nse.IST_OFFSET_MINUTES` root
#: (DEC-TIMESTAMP-IST-SINGLESOURCE-001; SQL text byte-identical) —
#: :data:`_IST_DATE_EXPR` derives from this fragment so no second literal can
#: drift (DEC-DAILY-OPEN-SUBSTITUTION-HONESTY-001).
_IST_TS_EXPR: Final[str] = (
    f"((\"timestamp\" AT TIME ZONE 'UTC') + INTERVAL '{IST_OFFSET_MINUTES} minutes')"
)

#: The SQL fragment for a bar's **IST calendar date** — the market wall-clock day
#: the candle grid renders (:func:`_format_candle_timestamp`). Derived from
#: :data:`_IST_TS_EXPR` (the shared fixed-offset instant — ``AT TIME ZONE 'UTC'``
#: BEFORE the +05:30 shift, so the date is independent of the ambient DuckDB
#: session ``TimeZone``; a bare CAST-to-DATE on the TIMESTAMPTZ column resolves
#: in the session zone and split one NSE session into two dates on a
#: negative-offset host — DEC-SPOT-SESSION-DAYS-IST-TZ-001) by truncating to
#: DATE. Used by the derived-1day date filter (DEC-DB-UX-PACK-001 Feature 3) +
#: the F&O full-stored-life span + the coverage per-session aggregate + the
#: ``session_days`` distinct-IST-date count.
_IST_DATE_EXPR: Final[str] = f"{_IST_TS_EXPR}::DATE"

#: The SQL fragment for a bar's **UTC calendar date** — the legacy intraday date
#: bound (byte-identical to the pre-DEC-DB-UX-PACK-001 comparison). Real intraday
#: bars trade 03:45–10:00 UTC where the UTC and IST dates coincide, so intraday
#: filtering keeps this expression (CLAUDE.md §6 store-UTC / display-IST).
_UTC_DATE_EXPR: Final[str] = "(\"timestamp\" AT TIME ZONE 'UTC')::DATE"

#: The Track-1 SPOT data-pull floor (``GROWW_HISTORY_FLOOR`` = 2020-01-01,
#: :mod:`brutex.dashboard.pull_controller`). The ``/db`` SPOT date filter defaults
#: its START to this floor when the operator leaves the "from" input blank — the
#: honest lower bound of what the lake can hold, NEVER "today" (an empty input must
#: not masquerade as a live one-day filter — the inert-filter illusion,
#: DEC-DB-UX-PACK-001 Feature 2). Kept as a local literal (not an import of the
#: heavy pull-controller module) with this citation so the read-only viewer stays
#: import-light; a drift is caught by :func:`test_spot_filter_floor_matches_pull_floor`.
_SPOT_FILTER_FLOOR_ISO: Final[str] = "2020-01-01"

#: The 1-minute base partition — the source of the previous-day close BASELINE
#: for the derived %-change / gap columns. The engine is 1-minute-based, so the
#: baseline is the prior trading day's **last 1-minute close** (the 15:29 IST
#: bar), NOT the 1-day official close — this keeps ``change_pct`` / ``open_gap_pct``
#: internally consistent with the 1-minute feed the engine actually sweeps
#: (DEC-SPOT-PREVCLOSE-1MIN-001; operator 2026-06-03). The 1-day official close
#: (= the NSE settlement / "previous close") is reserved for the separate NSE
#: pre-open Excel report, never the engine store.
_INTRADAY_BASE_INTERVAL: Final[str] = "1minute"

#: 09:15:00 IST — the regular NSE/BSE session open. The overnight ``open_gap_pct``
#: derived column (DEC-DB-CANDLE-DERIVED-001) is computed ONLY for the bar at
#: this instant; DERIVED from the ONE COMMON
#: :data:`brutex.data.session.SESSION_OPEN` root
#: (DEC-TIMESTAMP-IST-SINGLESOURCE-001 — the former ``time(9, 15)`` mirror).
_SESSION_OPEN_IST: Final[time] = SESSION_OPEN

#: The canonical session open as minutes-past-midnight-IST (555), DERIVED from the
#: ONE COMMON :data:`brutex.data.session.SESSION_OPEN` constant — never a fourth
#: hardcoded 09:15 literal (CLAUDE.md §11 COMMON). The per-session coverage
#: aggregate (DEC-DAILY-OPEN-SUBSTITUTION-HONESTY-001, aligned by
#: DEC-DAILY-OPEN-PREDICATE-ALIGN-001) counts a session as *open-substituted*
#: when its FIRST present 1-minute bar is NOT exactly ``ist_date + this many
#: minutes`` — i.e. the derived 1-day open for that session is the earliest
#: PRESENT minute's open (``tf_resampler`` ``.first()``), not the canonical
#: 09:15 IST open price. Muhurat sessions are EXCLUDED
#: (:data:`_MUHURAT_EXCLUSION_SQL`) — their open is off-09:15 BY DESIGN.
_SESSION_OPEN_MINUTES: Final[int] = SESSION_OPEN.hour * 60 + SESSION_OPEN.minute


def _muhurat_exclusion_sql(muhurat_dates: Iterable[date]) -> str:
    """The per-session CTE's Muhurat-abstention SQL fragment — pure, deterministic.

    DEC-DAILY-OPEN-PREDICATE-ALIGN-001: the /completeness open-substitution
    counter shares ONE predicate with the prebuild detector
    (:func:`brutex.data.spot_daily_resample.classify_daily_open` — this CTE
    is its mirror-pinned SQL twin), and that predicate ABSTAINS on a Muhurat
    session: its open is off-09:15 BY DESIGN (evening 18:00/18:15 for
    2020-2024, afternoon 13:45 for 2025), so counting it as a substituted
    daily open would be a false positive on a perfectly healthy lake.

    Renders ``" AND ist_date NOT IN (DATE '…', …)"`` from the STATIC calendar
    constant :data:`brutex.calendars.nse.MUHURAT_SESSIONS` (the ONE source of
    truth both surfaces key off — a future notified Muhurat date lands in both
    with zero edits here), sorted for a deterministic SQL string. An empty
    date set renders ``""`` (SQL ``NOT IN ()`` is invalid — the defensive
    empty-calendar branch). The dates are hardcoded :class:`datetime.date`
    calendar constants rendered via ``isoformat()`` — never user input, so
    the interpolation is injection-safe (the same S608 posture as the CTE's
    other static fragments).
    """
    rendered = ", ".join(f"DATE '{d.isoformat()}'" for d in sorted(muhurat_dates))
    return f" AND ist_date NOT IN ({rendered})" if rendered else ""


#: The Muhurat abstention baked once at import from the calendar constant —
#: see :func:`_muhurat_exclusion_sql` (DEC-DAILY-OPEN-PREDICATE-ALIGN-001).
_MUHURAT_EXCLUSION_SQL: Final[str] = _muhurat_exclusion_sql(MUHURAT_SESSIONS)

#: The server-computed derived columns appended to every spot candle page
#: (DEC-DB-CANDLE-DERIVED-001): previous-day % change + the 09:15 overnight gap +
#: the prev-close CONTIGUITY honesty flag (DEC-DB-PREVCLOSE-CONTIGUITY-MARKER-001,
#: the ``/db`` twin of the ``/movers`` " *" marker DEC-MOVERS-PREVCLOSE-CONTIGUITY-001
#: #1539). ``prev_close_contiguous`` is ``False`` when the %-change baseline is NOT
#: the NSE-calendar-adjacent prior session (a genuine lake hole was bridged — the
#: served number is confidently wrong, so it is MARKED not silently changed),
#: ``True`` when it IS adjacent, and ``None`` when there is no baseline / the
#: calendar abstains past its coverage floor (never a fabricated verdict). They are
#: NOT real Parquet columns (never selectable in the column picker); the viewer
#: computes them per bar from the symbol's prior-day last 1-minute close
#: (DEC-SPOT-PREVCLOSE-1MIN-001).
SPOT_DERIVED_COLUMNS: Final = ("change_pct", "open_gap_pct", "prev_close_contiguous")

#: The two server-computed derived columns appended to every **F&O** candle page on
#: TOP of :data:`SPOT_DERIVED_COLUMNS` (DEC-FNO-MONEYNESS-001): the ONE moneyness cell the
#: viewer shows per bar — the bucket + signed strike-depth as a single label
#: (``"ITM -1"`` / ``"OTM +10"`` / ``"ATM"``), classified against the per-bar underlying
#: spot. NOT a real Parquet column — the viewer derives it per bar from
#: ``(option_type, strike, spot[, strike_step])`` (:mod:`brutex.data.moneyness`), so a
#: fixed-strike contract reads ``OTM +N`` then ``ITM -N`` as the spot moves through the day.
#: A FUTURE reads ``—``; an option whose underlying spot is absent reads ``?`` (never faked).
FNO_MONEYNESS_COLUMNS: Final = ("moneyness",)

#: The per-bar derived VALUE columns appended after ``moneyness`` (DEC-DB-FNO-COLUMNS-001) —
#: the option's worth at a glance, ALL from the SAME per-bar spot join ``moneyness`` already
#: does (no extra read). ``spot``/``intrinsic``/``time_value`` are 2-dp money strings (``None``
#: for a future / when the underlying spot is absent); ``days_to_expiry`` is a pure int (no I/O)
#: resolvable for any decodable contract (option OR future), ``None`` only when undecodable.
FNO_VALUE_COLUMNS: Final = ("spot", "intrinsic", "time_value", "days_to_expiry")

#: The derived HUMAN-READABLE Greeks status column (DEC-DB-FNO-DISPLAY-TRUTH-001 item 3):
#: the stored ``greeks_provenance_id`` stamp's packed status decoded into words at READ
#: time via the ONE COMMON :func:`brutex.data.greeks_provenance.greeks_status_label`
#: (``"ok"`` / ``"below intrinsic — no unique IV"`` / …; ``"not enriched"`` when the bar
#: carries no stamp — the overlay is stored on the ``1minute`` partition only; ``"—"``
#: for a future, which is never enriched by design). Rides the page's own SELECT — no
#: extra I/O; never a bare packed int presented as the only explanation of a NULL Greek.
FNO_GREEKS_STATUS_COLUMNS: Final = ("greeks_status",)

#: The full F&O derived column set in display order: the combined moneyness label, the
#: value columns, then the decoded Greeks status. ``_fno_derived_columns`` returns this
#: for an ``FNO`` page.
FNO_DERIVED_COLUMNS: Final = FNO_MONEYNESS_COLUMNS + FNO_VALUE_COLUMNS + FNO_GREEKS_STATUS_COLUMNS

#: Spot segments probed (in order) to resolve an F&O contract's UNDERLYING spot series —
#: an index underlying rides ``INDEX``, a single-stock underlying rides ``CASH``. The
#: shared :data:`brutex.data.partition.SPOT_PROBE_SEGMENTS` is the ONE source of truth for
#: this order (also used by ``movers_futures._SPOT_SEGMENTS``; CLAUDE.md §11 COMMON).
_SPOT_PROBE_SEGMENTS: Final = SPOT_PROBE_SEGMENTS


def _fno_derived_columns(segment: str) -> tuple[str, ...]:
    """The extra derived columns an F&O candle page carries beyond the spot pair.

    :data:`FNO_DERIVED_COLUMNS` (the combined moneyness label + the value columns
    ``spot``/``intrinsic``/``time_value``/``days_to_expiry``, DEC-DB-FNO-COLUMNS-001) for an
    ``FNO`` partition, else ``()`` — so spot and F&O views keep a stable, segment-appropriate
    column set (DEC-FNO-MONEYNESS-001).
    """
    return FNO_DERIVED_COLUMNS if segment == FNO_CANDLE_LAKE_SEGMENT else ()


def _format_candle_timestamp(value: Any, interval: str) -> Any:
    """Render a candle bar timestamp as a clean IST wall-clock string.

    Drops the ``+05:30`` offset suffix (operator: "it should be clean") — a
    bar stored as ``2026-05-29T03:30:00Z`` renders ``2026-05-29 09:00:00``
    (IST), never ``2026-05-29 09:00:00+05:30``. Converting to :data:`_IST`
    explicitly keeps the output identical whether the DuckDB session runs in
    UTC (CI) or Asia/Kolkata (the operator's box). Non-datetime cells (a
    column subset that excludes ``timestamp`` never reaches here) fall back to
    :func:`_json_safe`.

    For the ``"1day"`` *interval* the bar is rendered **DATE-ONLY**
    (``2026-05-29``) — the ``00:00:00`` time component is noise for a daily bar
    (DEC-DB-CANDLE-DERIVED-001); every intraday interval keeps the full
    ``YYYY-MM-DD HH:MM:SS`` wall-clock.
    """
    if isinstance(value, datetime):
        ist = value.astimezone(_IST) if value.tzinfo is not None else value
        fmt = "%Y-%m-%d" if interval == _DAILY_INTERVAL else "%Y-%m-%d %H:%M:%S"
        return ist.strftime(fmt)
    return _json_safe(value)


def _ist_date(value: Any) -> str | None:
    """Render a bar timestamp as its IST calendar date (``YYYY-MM-DD``), or ``None``.

    The lake stores bar time as a UTC ``TIMESTAMPTZ``; the spot-coverage summary
    reports the Indian-market wall-clock *date* (converting to :data:`_IST`) so
    the first/last coverage date matches the trading day an operator expects —
    the date-level analog of :func:`_format_candle_timestamp`. A non-datetime
    cell (e.g. ``MIN``/``MAX`` over a partition that happens to hold zero rows
    returns SQL ``NULL`` → ``None``) yields ``None``.
    """
    if isinstance(value, datetime):
        ist = value.astimezone(_IST) if value.tzinfo is not None else value
        return ist.date().isoformat()
    return None


def _iso_date(value: Any) -> str | None:
    """Render a DuckDB ``DATE`` cell as ISO ``YYYY-MM-DD``, or ``None`` for SQL NULL.

    The already-IST twin of :func:`_ist_date` for the per-session coverage
    aggregate (DEC-DAILY-OPEN-SUBSTITUTION-HONESTY-001): the folded CTE's
    ``MIN(ist_date)`` / ``MAX(ist_date)`` are ALREADY IST calendar dates (cast via
    :data:`_IST_DATE_EXPR`), so no timezone conversion applies — a
    :class:`~datetime.date` renders as ISO, and the SQL ``NULL`` an empty
    partition aggregates to yields ``None``.
    """
    if isinstance(value, date):
        return value.isoformat()
    return None


def _json_safe(value: Any) -> Any:
    """Coerce one DuckDB cell value into a JSON-serialisable form.

    ``None`` stays ``None`` (renders as JSON ``null``); ``bool`` /
    ``int`` / ``float`` / ``str`` pass through unchanged; everything
    else (dates, datetimes, :class:`~decimal.Decimal`, ``bytes``,
    enums, …) is rendered via :func:`str` so the value is always
    transport-safe without losing human readability.

    Parameters
    ----------
    value
        A single cell value as returned by DuckDB ``fetchall``.

    Returns
    -------
    Any
        A JSON-serialisable representation of *value*.
    """
    if value is None:
        return None
    if isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        # NaN / ±inf are not JSON-safe under Starlette's allow_nan=False;
        # stringify them so the viewer never 500s if a future migration
        # adds a DOUBLE column to an allow-listed table (hostile-review LOW).
        return value if math.isfinite(value) else str(value)
    if isinstance(value, bytes):
        # bytes are not JSON-safe; decode best-effort for display.
        return value.decode("utf-8", errors="replace")
    return str(value)


#: The :func:`table_summary` strict-mode (``raise_on_fault=True``) re-raise
#: set: the store fault types (``duckdb.Error`` / ``OSError`` — the
#: ``universe_cache.STORE_FAULT_TYPES`` members, a wedged volume / unreachable
#: DuckDB / a dropped table on a foreign DB) PLUS
#: :class:`~brutex.data.errors.CatalogError`, because the
#: DEC-STORE-WRITE-DEADLINE-001 wedge latch (``_ensure_not_wedged`` on the
#: store read view's ``execute``) raises CatalogError — a ``DataLakeError``,
#: NOT a ``duckdb.Error``. MUST stay member-equal to the ``/api/v1/db/tables``
#: route's ``_TABLES_PROBE_FAULT_TYPES`` (set-equality pinned in
#: ``test_dashboard_pull_contention_routes.py``) so a strict-mode raise is
#: ALWAYS caught by that route's uncached degrade arm — never a 500
#: (round-8 review MINOR: the probe→compute TOCTOU).
TABLE_SUMMARY_FAULT_TYPES: Final[tuple[type[BaseException], ...]] = (
    CatalogError,
    duckdb.Error,
    OSError,
)


def table_summary(conn: Any, *, raise_on_fault: bool = False) -> list[dict[str, Any]]:
    """Return ``[{"table", "row_count"}]`` for every known table.

    Runs one ``SELECT COUNT(*)`` per table in :data:`KNOWN_TABLES`. By
    default a table that is missing (a partially-initialised or foreign
    DB) — or any other per-table read fault — is tolerated and reported
    with ``row_count == 0`` rather than raising, so a fresh/empty store
    renders cleanly.

    Parameters
    ----------
    conn
        A DuckDB connection — typically
        :attr:`brutex.data.instrument_store.InstrumentStore.connection`.
    raise_on_fault
        ``True`` re-raises a :data:`TABLE_SUMMARY_FAULT_TYPES` member (a
        store fault OR the DEC-STORE-WRITE-DEADLINE-001 wedge latch's
        ``CatalogError``) instead of folding it into a 0-count — the
        strict form the ``/api/v1/db/tables`` CACHED compute uses, so a
        store that wedges AFTER a healthy freshness probe (the round-8
        probe→compute TOCTOU) raises out of ``get_or_compute`` and a
        wedge-degraded all-zero summary is NEVER stored under the
        healthy pre-wedge token. Any exception OUTSIDE that fault set
        still folds to 0 in BOTH modes (the never-a-500 posture is
        unchanged for unexpected defect classes). Default ``False`` —
        every existing caller byte-identical.

    Returns
    -------
    list[dict[str, Any]]
        One row per known table, in :data:`KNOWN_TABLES` order, each
        ``{"table": <name>, "row_count": <int>}``.
    """
    summary: list[dict[str, Any]] = []
    for name in KNOWN_TABLES:
        # name is a hardcoded allowlist literal, never caller input.
        try:
            row = conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()  # noqa: S608  # nosec B608 - name is a hardcoded KNOWN_TABLES literal, never caller input
        except TABLE_SUMMARY_FAULT_TYPES:
            if raise_on_fault:
                raise
            count = 0
        except Exception:
            count = 0
        else:
            count = int(row[0]) if row else 0
        summary.append({"table": name, "row_count": count})
    return summary


#: DuckDB binds ``OFFSET`` as a signed 64-bit integer; a value above this throws
#: ``ConversionException`` ("value out of range for INT64"). FastAPI's ``Query(ge=0)``
#: caps nothing above, so this is the upper guard (DEC-DB-VIEWER-PAGINATE-ALL-001
#: hostile-review MINOR-1).
_OFFSET_MAX: Final = (1 << 63) - 1


def clamp_offset(n: int) -> int:
    """Clamp a requested row offset into DuckDB's bindable ``[0, 2**63 - 1]`` range.

    A hostile / malformed ``?offset=-5`` must never produce a negative ``OFFSET``
    (DuckDB errors); a hostile ``?offset=10000000000000000000`` (above int64) must
    never reach the ``LIMIT ? OFFSET ?`` bind, where DuckDB raises a
    ``ConversionException`` that would surface as an HTTP 500 (the routes promise
    "never a 500"). Both clamp here — a too-large offset becomes the max, which
    DuckDB returns as a clean empty page. This is the COMMON guard for every paged
    reader (tables / candles / options / registry).
    """
    return max(0, min(n, _OFFSET_MAX))


def sample_rows(conn: Any, table: str, *, limit: int, offset: int = 0) -> dict[str, Any]:
    """Return one *limit*-sized page of rows from an allowlisted *table*.

    Read-only and bounded: issues an indexed ``SELECT COUNT(*)`` for the
    total then a single ``SELECT * FROM <table> LIMIT ? OFFSET ?`` with the
    page parameter-bound — it NEVER materialises the whole table (a 89k-row
    legs table would be a page of at most 500 rows). *table* MUST be an exact
    member of :data:`KNOWN_TABLES`; any other value (including an injection
    payload such as ``"instruments; DROP TABLE contracts"``) raises
    :class:`ValueError` before any SQL is built or executed.

    Parameters
    ----------
    conn
        A DuckDB connection (see :func:`table_summary`).
    table
        The table to sample. Must be in :data:`KNOWN_TABLES`.
    limit
        Page size; clamped to ``[1, 500]`` via :func:`clamp_limit`.
    offset
        Page start (0-based); clamped to ``>= 0`` via :func:`clamp_offset`.

    Returns
    -------
    dict[str, Any]
        ``{"table": <name>, "columns": [<col>, ...],
        "rows": [[<cell>, ...], ...], "total": <int>, "limit": <int>,
        "offset": <int>}`` with every cell JSON-safe. ``total`` is the
        indexed full-table row count (for the pager), NOT the page size.

    Raises
    ------
    ValueError
        If *table* is not an exact member of :data:`KNOWN_TABLES`.
    """
    if table not in KNOWN_TABLES:
        raise ValueError(f"unknown table {table!r}; allowed tables are {KNOWN_TABLES}")
    safe_limit = clamp_limit(limit)
    safe_offset = clamp_offset(offset)
    # `table` is validated against the KNOWN_TABLES allowlist above, so
    # interpolating it here is safe; the page bounds are parameter-bound.
    total_row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()  # noqa: S608  # nosec B608 - table is allowlist-validated against KNOWN_TABLES above
    total = int(total_row[0]) if total_row else 0
    cursor = conn.execute(
        f"SELECT * FROM {table} LIMIT ? OFFSET ?",  # noqa: S608  # nosec B608 - table is allowlist-validated against KNOWN_TABLES above; page bounds are parameter-bound
        [safe_limit, safe_offset],
    )
    description = cursor.description or []
    columns = [str(col[0]) for col in description]
    rows = [[_json_safe(cell) for cell in row] for row in cursor.fetchall()]
    return {
        "table": table,
        "columns": columns,
        "rows": rows,
        "total": total,
        "limit": safe_limit,
        "offset": safe_offset,
    }


def _quote_ident(name: str) -> str:
    """Double-quote a SQL identifier, rejecting any embedded double-quote.

    The *name* is always validated against a known column set BEFORE this
    is called, so the reject branch is belt-and-braces: a column literally
    containing ``"`` could otherwise break out of the quoted identifier.
    """
    if '"' in name:
        raise ValueError(f"illegal identifier {name!r}")
    return f'"{name}"'


def list_columns(conn: Any, table: str) -> list[str]:
    """Return the column names of an allowlisted *table*.

    Issues a zero-row ``SELECT * FROM <table> LIMIT 0`` and reads the
    cursor description — no data is materialised. Powers the column
    picker in the structured-query UI.

    Parameters
    ----------
    conn
        A DuckDB connection (see :func:`table_summary`).
    table
        Must be an exact member of :data:`KNOWN_TABLES`.

    Returns
    -------
    list[str]
        Column names in declaration order.

    Raises
    ------
    ValueError
        If *table* is not in :data:`KNOWN_TABLES`.
    """
    if table not in KNOWN_TABLES:
        raise ValueError(f"unknown table {table!r}; allowed tables are {KNOWN_TABLES}")
    cursor = conn.execute(f"SELECT * FROM {table} LIMIT 0")  # noqa: S608  # nosec B608 - table is allowlist-validated above
    columns = [str(col[0]) for col in (cursor.description or [])]
    # CRITICAL (DEC-STORE-LOCK-LEAK-FIX-001): the production connection is a
    # store ``_LockedReadConn`` that holds the store lock from ``execute()``
    # until a ``fetch*()``. Reading ``.description`` alone does NOT release it —
    # so we MUST fetch (the ``LIMIT 0`` result is empty) to free the lock, or
    # the background universe-pull's writes deadlock against this read (the
    # live "Fetch all expiries" 26-minute hang). Harmless on a raw cursor.
    cursor.fetchall()
    return columns


def _build_where(
    filters: tuple[tuple[str, str, str], ...],
    valid_columns: frozenset[str],
) -> tuple[str, list[Any]]:
    """Build a parameter-bound ``WHERE`` clause from validated *filters*.

    Each filter is ``(column, op_token, value)``. The column must be in
    *valid_columns* and the op_token in :data:`ALLOWED_OPERATORS`; the
    value is always returned as a bound parameter (never interpolated).
    Returns ``("", [])`` when *filters* is empty.

    Raises
    ------
    ValueError
        On an unknown column or an unknown operator token.
    """
    if not filters:
        return "", []
    clauses: list[str] = []
    params: list[Any] = []
    for column, op, value in filters:
        if column not in valid_columns:
            raise ValueError(f"unknown filter column {column!r}; allowed: {sorted(valid_columns)}")
        if op not in ALLOWED_OPERATORS:
            raise ValueError(f"unknown operator {op!r}; allowed: {sorted(ALLOWED_OPERATORS)}")
        ident = _quote_ident(column)
        if op == "contains":
            # Literal substring match: escape the LIKE metacharacters (\ % _)
            # in the value + declare ESCAPE, so a value containing % or _ is
            # matched literally rather than as a wildcard (the "like" operator
            # is the escape hatch when the operator wants raw patterns).
            esc_v = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            clauses.append(f"{ident} LIKE ? ESCAPE '\\'")
            params.append(f"%{esc_v}%")
        else:
            clauses.append(f"{ident} {ALLOWED_OPERATORS[op]} ?")
            params.append(value)
    return " WHERE " + " AND ".join(clauses), params


def _build_order_by(
    order_by: str | None,
    descending: bool,
    valid_columns: frozenset[str],
) -> str:
    """Build a validated ``ORDER BY`` clause, or ``""`` when not requested.

    Raises
    ------
    ValueError
        If *order_by* is given but not a member of *valid_columns*.
    """
    if order_by is None:
        return ""
    if order_by not in valid_columns:
        raise ValueError(f"unknown order column {order_by!r}; allowed: {sorted(valid_columns)}")
    direction = "DESC" if descending else "ASC"
    return f" ORDER BY {_quote_ident(order_by)} {direction}"


def _select_list(columns: tuple[str, ...] | None, valid_columns: frozenset[str]) -> str:
    """Return a quoted, validated select list — or ``"*"`` when *columns* is None.

    Raises
    ------
    ValueError
        If any requested column is not a member of *valid_columns*, or if
        *columns* is an empty tuple (a select of zero columns is invalid).
    """
    if columns is None:
        return "*"
    if not columns:
        raise ValueError("columns, when given, must name at least one column")
    for column in columns:
        if column not in valid_columns:
            raise ValueError(f"unknown column {column!r}; allowed: {sorted(valid_columns)}")
    return ", ".join(_quote_ident(c) for c in columns)


def _physical_union_columns(conn: Any, base: Path, *, source: str) -> frozenset[str]:
    """Return the physical union-schema column names of a partition tree.

    One ``DESCRIBE`` over the same resilient ``read_parquet(?, union_by_name=true)``
    source the page query uses — a Parquet-FOOTER read per file (O(files), the same
    order as the page ``COUNT(*)``), no data scan. Consumed only by
    :func:`_greeks_tolerant_select` when a Greeks overlay column is actually
    requested, so a plain OHLCV page never pays it.
    """
    rows = _execute_parquet_resilient(
        conn,
        base,
        "SELECT column_name FROM (DESCRIBE SELECT * FROM read_parquet(?, union_by_name=true))",
        [],
        source=source,
    )
    return frozenset(str(row[0]) for row in rows)


def _greeks_tolerant_select(conn: Any, base: Path, columns: tuple[str, ...], *, source: str) -> str:
    """Build the page select list with typed-NULL substitution for absent overlay columns.

    Schema tolerance for the stored Greeks overlay (DEC-GREEKS-ENGINE-PR3-001):
    within a MIXED legacy/enriched tree ``union_by_name=true`` already inserts
    NULLs for the files lacking an overlay column, but a PURE-legacy tree (no
    file carries it) would make the column reference a binder error and 500 the
    page. So each requested overlay column absent from the physical union
    schema is projected as ``CAST(NULL AS <type>) AS <col>`` — the page keeps a
    stable column set with honest NULL cells, never a fabricated Greek (Golden
    Rule #1). Non-overlay columns pass through verbatim (already validated by
    :func:`_select_list`).
    """
    present = _physical_union_columns(conn, base, source=source)
    parts: list[str] = []
    for column in columns:
        if column in FNO_GREEKS_COLUMN_TYPES and column not in present:
            parts.append(
                f"CAST(NULL AS {FNO_GREEKS_COLUMN_TYPES[column]}) AS {_quote_ident(column)}"
            )
        else:
            parts.append(_quote_ident(column))
    return ", ".join(parts)


def query_table(
    conn: Any,
    table: str,
    *,
    columns: tuple[str, ...] | None = None,
    filters: tuple[tuple[str, str, str], ...] = (),
    order_by: str | None = None,
    descending: bool = False,
    limit: int,
    offset: int = 0,
) -> dict[str, Any]:
    """Structured, read-only query of an allowlisted *table* (DEC-DB-QUERY-001).

    The power-user surface behind the ``/db`` query controls: choose which
    *columns* to show, filter rows with *filters* (``(column, op, value)``
    where ``op`` is an :data:`ALLOWED_OPERATORS` token), *order_by* a
    column, and page with *limit* / *offset*. Safe by construction: the
    table is allowlist-checked, every column / order column / filter column
    is validated against the table's REAL columns (so a non-existent or
    injection-shaped name raises before any SQL runs), operators come from a
    fixed map, and **all values are parameter-bound**. ``total`` reflects the
    filtered row count so the pager is correct.

    Parameters
    ----------
    conn
        A DuckDB connection (see :func:`table_summary`).
    table
        Must be an exact member of :data:`KNOWN_TABLES`.
    columns
        Columns to project, or ``None`` for all (``SELECT *``). An empty
        tuple is rejected.
    filters
        Zero or more ``(column, op_token, value)`` triples, AND-combined.
    order_by, descending
        Optional sort column + direction.
    limit, offset
        Page bounds; clamped via :func:`clamp_limit` / :func:`clamp_offset`.

    Returns
    -------
    dict[str, Any]
        ``{"table", "columns", "rows", "total", "limit", "offset"}`` —
        same shape as :func:`sample_rows`, with the filtered ``total``.

    Raises
    ------
    ValueError
        On an unknown table / column / order column / operator, or an empty
        explicit column tuple.
    """
    valid_columns = frozenset(list_columns(conn, table))
    select = _select_list(columns, valid_columns)
    where_sql, where_params = _build_where(filters, valid_columns)
    order_sql = _build_order_by(order_by, descending, valid_columns)
    safe_limit = clamp_limit(limit)
    safe_offset = clamp_offset(offset)

    # table is allowlisted; select/where/order identifiers are all validated
    # against valid_columns above; every value is parameter-bound.
    total_row = conn.execute(
        f"SELECT COUNT(*) FROM {table}{where_sql}",  # noqa: S608  # nosec B608 - allowlisted table + validated identifiers; values bound
        where_params,
    ).fetchone()
    total = int(total_row[0]) if total_row else 0
    cursor = conn.execute(
        f"SELECT {select} FROM {table}{where_sql}{order_sql} LIMIT ? OFFSET ?",  # noqa: S608  # nosec B608 - allowlisted table + validated identifiers; values bound
        [*where_params, safe_limit, safe_offset],
    )
    description = cursor.description or []
    out_columns = [str(col[0]) for col in description]
    rows = [[_json_safe(cell) for cell in row] for row in cursor.fetchall()]
    return {
        "table": table,
        "columns": out_columns,
        "rows": rows,
        "total": total,
        "limit": safe_limit,
        "offset": safe_offset,
    }


def _validate_candle_coords(exchange: str, segment: str, symbol: str, interval: str) -> None:
    """Validate the four candle-partition coordinates, else raise ValueError.

    Mirrors the partition-layer allowlists (``VALID_EXCHANGES`` /
    ``VALID_SEGMENTS`` / ``VALID_INTERVALS``) and applies a strict symbol
    allowlist (:data:`_SYMBOL_RE`, plus a ``..`` reject) so a coordinate can
    never traverse outside ``<lake>/bars`` when joined into the glob.
    """
    if exchange not in VALID_EXCHANGES:
        raise ValueError(f"unknown exchange {exchange!r}; allowed: {sorted(VALID_EXCHANGES)}")
    if segment not in VALID_SEGMENTS:
        raise ValueError(f"unknown segment {segment!r}; allowed: {sorted(VALID_SEGMENTS)}")
    if interval not in VALID_INTERVALS:
        raise ValueError(f"unknown interval {interval!r}; allowed: {sorted(VALID_INTERVALS)}")
    if ".." in symbol or not _SYMBOL_RE.match(symbol):
        raise ValueError(f"illegal candle symbol {symbol!r}")


def list_candle_datasets(
    lake_root: Path,
    *,
    exclude_segments: frozenset[str] = frozenset(),
    intervals: Collection[str] | None = None,
) -> list[dict[str, str]]:
    """Discover which ``(exchange, segment, symbol, interval)`` candle sets exist.

    MANIFEST-FIRST (DEC-LAKE-MANIFEST-O1-001): when the durable write-time
    dataset manifest (``<lake_root>/.manifest/`` —
    :mod:`brutex.data.lake_manifest`) exists and its integrity token validates
    (window-ledger stat token + row count), the dataset list is served from it
    in **O(manifest-rows)** — independent of the lake's partition/file count
    (the measured 2.36M-file post-prebuild lake paid a 29.7-minute O(files)
    walk on the first post-restart discovery; the manifest serves the SAME
    byte-identical list in seconds). An absent / stale / corrupt manifest
    falls back GRACEFULLY to the legacy walk below (byte-identical results,
    never a 500), and an UNFILTERED walk rebuilds the manifest from its result
    (self-healing: the first discovery after this change — or after a vendor
    pull — pays one last walk, then serves manifest-first until the next
    ledger move / heal invalidation).

    LEGACY WALK: walks ``<lake_root>/bars/<exchange>/<segment>/<symbol>/
    <interval>/`` and returns one entry per interval directory that holds at
    least one ``.parquet`` file. Directory names that are not valid
    coordinates are skipped (a foreign / partially-written tree never raises).
    A missing ``bars`` directory yields ``[]``.

    Parameters
    ----------
    lake_root
        Top-level lake directory (the parent of ``bars/``).
    exclude_segments
        Segment directory names to PRUNE at the walk level — an excluded
        segment's tree is never even descended (DEC-COMPLETENESS-FNO-SKIP-001:
        skipping ``FNO`` here avoids walking the ~10⁵-10⁶ per-contract
        partition dirs a fully-materialised options lake holds). Empty (the
        default) is byte-identical to the unfiltered walk. Applied as a row
        filter on the manifest-served path (same result set).
    intervals
        When given, only these interval directories are listed (e.g. the spot
        verdict pair ``("1minute", "1day")``); ``None`` (the default) lists
        every valid interval.

    Returns
    -------
    list[dict[str, str]]
        ``[{"kind", "exchange", "segment", "symbol", "interval"}]`` sorted by
        the four coordinate keys — the candle-viewer dataset picker.
    """
    manifest_rows = load_manifest_datasets(lake_root)
    if manifest_rows is not None:
        return _datasets_from_manifest(
            manifest_rows, exclude_segments=exclude_segments, intervals=intervals
        )
    # Capture the freshness token BEFORE the walk: a vendor write landing
    # DURING the (potentially long) walk must never let the rebuilt manifest
    # claim freshness the walk did not have (Golden Rule #1).
    pre_walk_token = ledger_stat_token(lake_root)
    found = _walk_candle_datasets(lake_root, exclude_segments=exclude_segments, intervals=intervals)
    if not exclude_segments and intervals is None and pre_walk_token is not None:
        # Only an UNFILTERED walk sees the whole dataset population, so only it
        # may (re)build the manifest — a filtered walk would persist a
        # silently-partial row set. The rebuild ALWAYS stamps the PRE-walk
        # token (`rebuild_manifest`'s contract verbatim — a post-walk stamp
        # could claim freshness the walk did not have and stays REJECTED). On
        # a mid-walk ledger move the stamp is therefore STALE and the
        # load-side token gate refuses to serve it as-is — yet the base is
        # NOT a wasted write: it is the seed `record_dataset`'s write-side
        # convergence layers onto (without a base every prebuild record
        # no-ops — `record_skipped_no_base` — and a manifest-absent lake
        # stays unconvergeable until the next quiescent unfiltered walk
        # re-pays the O(files) discovery the manifest exists to kill).
        # DEC-O1-CLAIMS-HONESTY-001 round-6 review fold: the interim
        # SKIP-on-mismatch design is REVERTED — only the post-walk token
        # RE-READ survives, OBSERVE-ONLY: a moved (or unreadable `None`)
        # post-walk token emits one `db_view.manifest_rebuilt_stale` INFO
        # carrying both tokens. HONEST bounded window (Golden Rule #10): a
        # later `record_dataset` re-stamps the CURRENT token and serves the
        # union of walk + recorded rows, so a dataset created mid-walk whose
        # own record no-oped before this base landed can be UNDER-LISTED
        # until the next ledger move + walk — the same bounded class as
        # lake_manifest's documented cold-walk ∩ active-prebuild window.
        # Best-effort: a rebuild failure leaves the walk result (already
        # correct) untouched.
        post_walk_token = ledger_stat_token(lake_root)
        if post_walk_token != pre_walk_token:
            _LOG.info(
                "db_view.manifest_rebuilt_stale",
                pre_walk_token=pre_walk_token,
                post_walk_token=post_walk_token,
            )
        rebuild_manifest(lake_root, found, ledger_token=pre_walk_token)
    return found


def _datasets_from_manifest(
    rows: list[dict[str, str]],
    *,
    exclude_segments: frozenset[str],
    intervals: Collection[str] | None,
) -> list[dict[str, str]]:
    """Project manifest rows to the walk's exact result shape — O(rows).

    Applies the SAME coordinate validity screens the walk applies at the
    directory level (``VALID_EXCHANGES`` / ``VALID_SEGMENTS`` /
    ``VALID_INTERVALS`` — symbols are deliberately UNfiltered, mirroring the
    walk, so a #911-style drift-key directory like ``NSE-M&M`` stays listed)
    plus the caller's ``exclude_segments`` / ``intervals`` filters, and emits
    the identical ``{"kind": "spot", …}`` dicts in the identical
    four-key sort order (the walk's per-level ``sorted`` is exactly a
    lexicographic sort on the coordinate tuple) — byte-identity is pinned by
    test against the walk on the same lake.
    """
    out: list[dict[str, str]] = []
    for row in sorted(
        rows, key=lambda r: (r["exchange"], r["segment"], r["symbol"], r["interval"])
    ):
        if row["exchange"] not in VALID_EXCHANGES:
            continue
        if row["segment"] not in VALID_SEGMENTS or row["segment"] in exclude_segments:
            continue
        if row["interval"] not in VALID_INTERVALS:
            continue
        if intervals is not None and row["interval"] not in intervals:
            continue
        out.append(
            {
                "kind": "spot",
                "exchange": row["exchange"],
                "segment": row["segment"],
                "symbol": row["symbol"],
                "interval": row["interval"],
            }
        )
    return out


def _walk_candle_datasets(
    lake_root: Path,
    *,
    exclude_segments: frozenset[str],
    intervals: Collection[str] | None,
) -> list[dict[str, str]]:
    """The legacy O(files) ``bars/`` tree walk (the manifest-miss fallback).

    The pre-manifest :func:`list_candle_datasets` body: one entry per interval
    dir holding at least one ``.parquet``, invalid coordinate names skipped,
    missing ``bars/`` → ``[]``. Kept as the fallback + the manifest's rebuild
    source (the lake tree stays the source of truth). Result byte-unchanged;
    one liveness :func:`~brutex.observability.progress_beat.emit_progress_beat`
    fires per walked symbol directory (DEC-GREEKS-BULK-REENRICH-001 fix
    round 2): the walk is the measured 29.7-minute O(files) segment on the
    production 2.36M-file lake — ~3× the 600 s pull stall watchdog timeout —
    and it runs BEFORE any per-contract/per-file scan beat can fire, so
    beat-less it false-tripped a CRITICAL "pull halted" alert on a healthy
    dashboard Greeks backfill whose manifest was absent/stale (a CLI-pulled
    lake / the first discovery after any pull). A no-op outside a dashboard
    job (the documented ``emit_progress_beat`` contract); O(1) per symbol.
    """
    bars = lake_root / "bars"
    if not bars.is_dir():
        return []
    found: list[dict[str, str]] = []
    for exch_dir in sorted(p for p in bars.iterdir() if p.is_dir()):
        if exch_dir.name not in VALID_EXCHANGES:
            continue
        for seg_dir in sorted(p for p in exch_dir.iterdir() if p.is_dir()):
            if seg_dir.name not in VALID_SEGMENTS or seg_dir.name in exclude_segments:
                continue
            for sym_dir in sorted(p for p in seg_dir.iterdir() if p.is_dir()):
                # Liveness beat per walked symbol dir — keeps the 600 s stall
                # watchdog fed through the enumeration phase of an O(files)
                # walk (best-effort; no-op when no beat is registered).
                emit_progress_beat()
                for tf_dir in sorted(p for p in sym_dir.iterdir() if p.is_dir()):
                    if tf_dir.name not in VALID_INTERVALS:
                        continue
                    if intervals is not None and tf_dir.name not in intervals:
                        continue
                    if any(tf_dir.rglob("*.parquet")):
                        found.append(
                            {
                                "kind": "spot",
                                "exchange": exch_dir.name,
                                "segment": seg_dir.name,
                                "symbol": sym_dir.name,
                                "interval": tf_dir.name,
                            }
                        )
    return found


def list_fno_candle_datasets(lake_root: Path) -> list[dict[str, Any]]:
    """Discover the F&O candle datasets, each decoded to its contract coordinates.

    The F&O analog of :func:`list_candle_datasets`: keeps ONLY the ``segment="FNO"``
    partition sets — the futures + options the Groww historical pull writes to
    ``bars/<exch>/FNO/<groww_symbol>/<interval>/…`` (DEC-1MINUTE-ONLY-DERIVE-1DAY-001) —
    and enriches each with the contract identity decoded from its ``groww_symbol`` via
    :func:`~brutex.data.fno_symbol.parse_fno_symbol` (underlying / expiry / strike /
    instrument_type / option_type). This is the first-class "F&O candle lake" surface
    the ``/db`` browser renders, distinct from (and no longer conflated with) the spot
    candle list — the honesty fix of DEC-FNO-CANDLE-LAKE-001: an F&O contract bar is
    NOT spot.

    A dataset whose directory name does NOT decode as a contract symbol is STILL listed
    (with ``None`` identity fields) rather than hidden — the lake is the single source
    of truth and a present-but-undecodable partition is surfaced honestly, never dropped
    (Golden Rule #1). The ``(exchange, segment, symbol, interval)`` coordinates are the
    SAME keys :func:`query_candles` takes, so the viewer queries an F&O contract through
    the identical ``segment="FNO"`` candle path — zero new query surface.

    Parameters
    ----------
    lake_root
        Top-level lake directory (the parent of ``bars/``).

    Returns
    -------
    list[dict[str, Any]]
        One entry per ``(exchange, FNO, groww_symbol, interval)`` set, in
        :func:`list_candle_datasets` order, each shaped::

            {"kind": "fno", "exchange", "segment", "symbol", "interval",
             "underlying": str|None, "expiry": "YYYY-MM-DD"|None,
             "strike": str|None, "instrument_type": str|None, "option_type": str|None}
    """
    out: list[dict[str, Any]] = []
    for dataset in list_candle_datasets(lake_root):
        if dataset["segment"] != FNO_CANDLE_LAKE_SEGMENT:
            continue
        parts = parse_fno_symbol(dataset["symbol"])
        out.append(
            {
                "kind": "fno",
                "exchange": dataset["exchange"],
                "segment": dataset["segment"],
                "symbol": dataset["symbol"],
                "interval": dataset["interval"],
                "underlying": parts.underlying if parts else None,
                "expiry": parts.expiry.isoformat() if parts else None,
                "strike": str(parts.strike) if parts and parts.strike is not None else None,
                "instrument_type": parts.instrument_type if parts else None,
                "option_type": parts.option_type if parts else None,
            }
        )
    return out


def fno_candle_lake_schema() -> list[dict[str, Any]]:
    """Return the F&O candle logical-table schema (the documented column set).

    A JSON-friendly projection of
    :data:`brutex.data.fno_candle_lake.FNO_CANDLE_COLUMNS` — the documented columns
    (the 7 physical OHLCV columns + the partition / symbol identity columns) — the
    programmatic twin of the ``schema_meta`` registration written at store-open
    (DEC-FNO-CANDLE-LAKE-001). Served on the candle-datasets endpoint for API clients;
    the operator-facing surface of the same schema is the browsable ``schema_meta``
    table row in ``/db``. Pure projection — no disk access.
    """
    return [
        {
            "name": c.name,
            "dtype": c.dtype,
            "source": c.source,
            "nullable": c.nullable,
            "doc": c.doc,
        }
        for c in FNO_CANDLE_COLUMNS
    ]


def _execute_parquet_resilient(
    conn: Any,
    directory: Path,
    sql: str,
    extra_params: list[Any],
    *,
    source: str,
    skip_event: str = "db.parquet_skip",
    unreadable_event: str = "db.parquet_unreadable",
    skip_sink: list[bool] | None = None,
    files: Sequence[str] | None = None,
) -> list[tuple[Any, ...]]:
    """Execute *sql* over the ``directory`` Parquet glob, **resilient to an unreadable file**.

    The ONE COMMON probe-and-skip primitive (CLAUDE.md §11 COMMON) for every direct
    ``read_parquet`` read in this module: the coverage summaries
    (:func:`_aggregate_parquet_resilient`) AND the ``/db`` candle pager
    (:func:`query_candles` + its derived-column helpers, DEC-DB-CORRUPT-SKIP-001). *sql*
    MUST reference the source as its FIRST bound ``?`` (e.g.
    ``read_parquet(?, union_by_name=true)``); *extra_params* are the remaining bound
    values (date bounds / page LIMIT-OFFSET / instant bounds), in order.

    Fast path (steady state): bind ``<directory>/**/*.parquet`` to the source ``?`` plus
    *extra_params* and ``fetchall()`` — byte-identical to a direct query, one scan.

    Degraded path: a single corrupt / version-incompatible Parquet file in the partition
    (seen in the wild as DuckDB ``don't know what type: \\r`` / *"No magic bytes found at
    end of file"*) makes the whole-glob scan raise an unreadable-Parquet error, which
    would otherwise 500 the whole page on EVERY paging click until the file is manually
    quarantined. So on that error we probe each matched file, **skip** the unreadable
    one(s) — each logged as *skip_event* with its path + reason so the operator can
    quarantine it — and re-run *sql* over the readable subset (bound as a LIST to the
    SAME source ``?``; DuckDB ``read_parquet`` accepts a path list). When NO file is
    readable we log *unreadable_event* and return ``[]`` (a degraded-but-correct empty
    result, never an exception, never a fabricated bar). Honest envelope (Golden Rule
    #1 / #10): a skipped file is OMITTED, never silently counted. *skip_event* /
    *unreadable_event* let each surface keep its own log namespace (the coverage
    summaries log ``coverage.*``, the candle pager ``db.*``) over the SAME logic.

    *skip_sink* (optional): when a list is passed, ``True`` is appended to it iff the
    degraded path OMITTED at least one file (a real skip OR the all-unreadable case).
    The adjacency-sensitive prior-close baseline readers consume this to emit an honest
    BLANK %-change instead of bridging to a non-adjacent earlier day after a skip
    (DEC-DB-CORRUPT-SKIP-001 BLOCKER fix; Golden Rule #1 — never a fabricated number).
    The fast (no-error) path never touches *skip_sink*.

    *files* (optional, DEC-DB-LATENCY-HARDENING-001): an explicit BOUNDED candidate
    file list (POSIX paths under *directory*) bound to the source ``?`` instead of
    the whole ``/**/*.parquet`` glob — the caller has proven (byte-parity pinned per
    caller) that only these files can hold rows the query can match, so DuckDB opens
    O(candidates) Parquet footers per statement instead of O(all stored months).
    ``None`` (the default) keeps the whole-glob read byte-identical; an EMPTY list
    short-circuits to ``[]`` with ZERO queries (no candidate file ⇒ no matchable
    row). The degraded corrupt-skip path probes ONLY the bounded candidates (a
    corrupt file OUTSIDE them provably cannot hold a matchable row, so it neither
    trips the probe nor the *skip_sink* adjacency guard — a strictly-narrower
    degrade blast radius, named in MIGRATION.md #1719).

    The skip set is the COMMON :func:`brutex.dashboard.movers_resilient.movers_read_skip_errors`
    (``duckdb.Error`` covers a corrupt ``read_parquet``; ``OSError`` covers a file-level
    fault). ``duckdb.Error`` is deliberately BROAD (DuckDB exposes no version-stable
    *unreadable-file* subclass). The per-file probe is SCHEMA-AWARE
    (DEC-ALIEN-SCHEMA-READ-HEAL-001): each file is probed with the REAL *sql* bound to
    that ONE file, so an ALIEN-schema Parquet — readable bytes whose columns/dtypes
    detonate the query (a missing ``timestamp`` binder error, a wrong-dtype
    ``arg_max``/``SUM`` conversion error) — folds into the SAME skip set a corrupt file
    does, instead of riding the old readability-only probe into a re-raise that 500'd
    the surface on EVERY poll until manual cleanup (the sticky-500 class; the
    lake-invariants scanner now emits a ``SchemaAlienFile`` finding for the same file so
    the healer quarantines it for real). The residual union-conflict case (every file
    passes its OWN probe yet the union over the subset still fails — a cross-file dtype
    conflict) degrades to ``[]`` after ONE *unreadable_event* ERROR — never a 500.
    HONEST envelope (Golden Rule #1/#10): this SUPERSEDES the earlier genuine-SQL-bug
    re-raise carve-out — a genuine SQL bug in a call site's constant query now ALSO
    degrades to a LOUDLY-logged empty result instead of a 500 (sanctioned: the constant
    SQL strings are pinned by the per-module 100% coverage gate, which is the bug net;
    the operator-facing alien-DATA sticky-500 outweighs the lab-only raise signal). The
    per-file probing is paid ONLY when a read actually fails; the happy path stays a
    single query (Golden Rule #5 O(1) steady-state).

    *files* (optional, DEC-DB-BASELINE-MANIFEST-ROUTE-001): a PRE-PRUNED file
    list bound to the source ``?`` instead of the whole-directory glob — the
    %-baseline readers pass the manifest/month-grammar-selected page files so
    a deep partition never opens every footer per click. The pruner only ever
    SELECTS files (never changes values), so every in-bounds row reads
    byte-identically to the glob; an EMPTY list short-circuits to ``[]`` with
    ZERO I/O (nothing can match — DuckDB refuses an empty ``read_parquet``
    list). The degraded probe path probes exactly the pruned set.
    """
    if files is not None and not files:
        # A bounded read with ZERO candidate files: no matchable row can exist —
        # the whole-glob read would return the same [] after opening every footer.
        return []
    source_bind: Any = list(files) if files is not None else directory.as_posix() + "/**/*.parquet"
    try:
        return list(conn.execute(sql, [source_bind, *extra_params]).fetchall())
    except movers_read_skip_errors() as whole_glob_error:
        readable, omitted = _readable_parquet_subset(
            conn,
            directory,
            whole_glob_error,
            sql=sql,
            extra_params=extra_params,
            source=source,
            skip_event=skip_event,
            unreadable_event=unreadable_event,
            files=files,
        )
        if omitted and skip_sink is not None:
            skip_sink.append(True)
        if not readable:
            return []
        try:
            return list(conn.execute(sql, [readable, *extra_params]).fetchall())
        except movers_read_skip_errors() as subset_error:
            # Cross-file union conflict: every file passed its OWN real-sql
            # probe, yet the union over the subset still fails (e.g. two files
            # whose shared column carries irreconcilable dtypes). Degrade to an
            # honest empty result after ONE loud ERROR — never a 500, never a
            # fabricated row (DEC-ALIEN-SCHEMA-READ-HEAL-001).
            _LOG.error(unreadable_event, source=source, error=str(subset_error))
            if skip_sink is not None and not omitted:
                skip_sink.append(True)
            return []


#: The readable-subset verdict memo (DEC-WINDOWS-STAT-GUARD-LATCH-001, the
#: C-r2-4 latch): while a corrupt/alien file persists in a partition, EVERY
#: paging click takes the degraded path — pre-latch that re-probed EVERY file
#: (a readability query + the real-sql schema probe per file) per click,
#: O(files) probe queries each time. The memo keys on
#: ``(directory, sha256(sql))`` and stores ``(file-set stat token, readable
#: subset, omitted)`` — the ``_stats_fingerprint`` precedent: any file
#: change (mtime/size/add/remove) moves the token → re-probe; an unchanged
#: set is served with ZERO probe queries. Capped FIFO — the verdict is cheap
#: to recompute, staleness is impossible (stat-token keyed).
_READABLE_SUBSET_CACHE: dict[tuple[str, str], tuple[str, tuple[str, ...], bool]] = {}
_READABLE_SUBSET_CACHE_CAP: Final[int] = 64
_READABLE_SUBSET_LOCK = threading.Lock()


def _reset_readable_subset_cache() -> None:
    """Test seam — drop every memoised readable-subset verdict."""
    with _READABLE_SUBSET_LOCK:
        _READABLE_SUBSET_CACHE.clear()


def _fileset_stat_token(files: Sequence[Path]) -> str:
    """Digest the file set's ``(path, mtime_ns, size)`` triples — O(files) stats.

    The latch's freshness token (the ``_stats_fingerprint`` idiom): a repeat
    degraded click pays these stat syscalls but ZERO probe queries; any
    changed/added/removed/vanished file moves the digest → full re-probe.
    """
    parts: list[str] = []
    for file in files:
        try:
            st = file.stat()
        except OSError:
            parts.append(f"{file}:gone")
        else:
            parts.append(f"{file}:{st.st_mtime_ns}:{st.st_size}")
    return hashlib.sha256(";".join(parts).encode("utf-8")).hexdigest()


def _probe_parquet_file(
    conn: Any, sql: str, candidate: str, extra_params: list[Any]
) -> tuple[str, str] | None:
    """Probe ONE file: ``None`` = passes both stages, else ``(reason, error)``.

    Stage 1 readability (``SELECT 1 ... LIMIT 1`` — the corrupt class), stage
    2 the caller's REAL *sql* over this one file (the alien-schema class,
    DEC-ALIEN-SCHEMA-READ-HEAL-001). The ONE per-file probe seam — the latch
    tests spy call counts through it.
    """
    skip_errors = movers_read_skip_errors()
    try:
        conn.execute("SELECT 1 FROM read_parquet(?) LIMIT 1", [candidate]).fetchone()
    except skip_errors as skip_reason:
        return ("unreadable", str(skip_reason))
    try:
        conn.execute(sql, [[candidate], *extra_params]).fetchall()
    except skip_errors as skip_reason:
        return ("alien_schema", str(skip_reason))
    return None


def _readable_parquet_subset(
    conn: Any,
    directory: Path,
    whole_glob_error: BaseException,
    *,
    sql: str,
    extra_params: list[Any],
    source: str,
    skip_event: str,
    unreadable_event: str,
    files: Sequence[str] | None = None,
) -> tuple[list[str], bool]:
    """Probe each ``*.parquet`` under *directory*; return ``(readable_subset, any_omitted)``.

    The shared degraded-path resolver (CLAUDE.md §11 COMMON) reached ONLY after a
    whole-glob ``read_parquet`` already raised an unreadable-Parquet error
    (*whole_glob_error*). Each file is probed in TWO stages
    (DEC-ALIEN-SCHEMA-READ-HEAL-001): (1) a cheap readability probe (``SELECT 1 ...
    LIMIT 1`` — catches a corrupt / truncated / version-incompatible file), then (2) a
    SCHEMA probe — the caller's REAL *sql* bound to that ONE file (+ *extra_params*
    verbatim; results discarded) — which catches an ALIEN-schema file (missing column /
    wrong dtype) the readability probe is structurally blind to. A file failing either
    stage is logged as *skip_event* (path + ``reason`` ∈ {``unreadable``,
    ``alien_schema``} + error) and OMITTED — never a fabricated bar. An empty return
    (NO file passed both stages) is logged once as *unreadable_event* so the caller
    degrades to an empty result instead of re-raising. The skip set is the COMMON
    :func:`brutex.dashboard.movers_resilient.movers_read_skip_errors`. HONEST cost
    (Golden Rule #10): the stage-2 probe executes the real query per file (an aggregate
    reads that file's rows), so one degraded pass costs about ONE whole-partition scan
    — paid only after the whole-glob read already failed, never on the happy path.

    The second tuple element ``any_omitted`` is ``True`` when at least one file was
    DROPPED from the read (a real skip OR the all-unreadable case). The adjacency-sensitive
    prior-close baseline readers (DEC-DB-CORRUPT-SKIP-001 BLOCKER fix) consume this flag:
    once any file is omitted, the partition is no longer trustworthy for ADJACENCY (the
    true predecessor day's bar may have lived in the dropped file), so they must emit a
    BLANK %-change rather than bridge to a non-adjacent earlier day — never a fabricated
    number (Golden Rule #1).

    LATCHED per file-set state (DEC-WINDOWS-STAT-GUARD-LATCH-001, the C-r2-4
    fix): while a corrupt/alien file persists, EVERY paging click lands here —
    pre-latch each click re-probed EVERY file (O(files) probe queries per
    click). The verdict is now memoised in :data:`_READABLE_SUBSET_CACHE`
    keyed ``(directory, sha256(sql))`` under the file set's
    :func:`_fileset_stat_token`: a repeat click within one file-set state pays
    O(files) ``stat`` syscalls + ZERO probe queries (spy-pinned); any file
    change moves the token → full re-probe. HONEST O (Golden Rule #10): the
    FIRST degraded click per file-set state stays O(files) probes (stage 2
    reads each file's rows — about one whole-partition scan), and the memo
    never makes the fast path cheaper — it bounds only the persistent-corrupt
    repeat-click storm. The all-unreadable ERROR is re-logged per degraded
    request (each empty serve stays loud), from the memo hit too.

    *files* (DEC-DB-BASELINE-MANIFEST-ROUTE-001): when the failed read was
    over a PRE-PRUNED file list, only those files are probed (the memo token
    is computed over the same set, so two prunes of one directory can never
    serve each other's verdict).
    """
    candidates = (
        [Path(f) for f in files] if files is not None else sorted(directory.rglob("*.parquet"))
    )
    token = _fileset_stat_token(candidates)
    cache_key = (directory.as_posix(), hashlib.sha256(sql.encode("utf-8")).hexdigest())
    with _READABLE_SUBSET_LOCK:
        hit = _READABLE_SUBSET_CACHE.get(cache_key)
    if hit is not None and hit[0] == token:
        readable = list(hit[1])
        omitted = hit[2]
        if not readable:
            _LOG.error(unreadable_event, source=source, error=str(whole_glob_error))
        return readable, omitted
    readable = []
    omitted = False
    for path in candidates:
        candidate = path.as_posix()
        verdict = _probe_parquet_file(conn, sql, candidate, extra_params)
        if verdict is None:
            readable.append(candidate)
        else:
            reason, error = verdict
            _LOG.warning(skip_event, source=source, path=candidate, reason=reason, error=error)
            omitted = True
    if not readable:
        _LOG.error(unreadable_event, source=source, error=str(whole_glob_error))
        omitted = True
    with _READABLE_SUBSET_LOCK:
        if cache_key not in _READABLE_SUBSET_CACHE and (
            len(_READABLE_SUBSET_CACHE) >= _READABLE_SUBSET_CACHE_CAP
        ):
            # FIFO eviction — insertion-ordered dict, oldest verdict first.
            _READABLE_SUBSET_CACHE.pop(next(iter(_READABLE_SUBSET_CACHE)))
        _READABLE_SUBSET_CACHE[cache_key] = (token, tuple(readable), omitted)
    return readable, omitted


def _aggregate_parquet_resilient(
    conn: Any, directory: Path, sql: str, *, source: str
) -> list[tuple[Any, ...]]:
    """Run *sql* — one ``read_parquet(?, union_by_name=true)`` aggregate over the
    ``directory`` glob — and return its rows, **resilient to an unreadable file**.

    The coverage-summary caller (:func:`summarize_spot_datasets` /
    :func:`summarize_option_datasets`): an aggregate ``SELECT`` with the source as its
    only bound ``?`` and no further params. A thin wrapper over the COMMON
    :func:`_execute_parquet_resilient` (no extra params, ``coverage.*`` log namespace) —
    the SAME probe-and-skip the ``/db`` candle pager uses (DEC-DB-CORRUPT-SKIP-001), so a
    corrupt file — and, since DEC-ALIEN-SCHEMA-READ-HEAL-001, an ALIEN-schema file the
    schema-aware probe skips — degrades to its readable subset (or 0 rows) here too,
    never a 500.
    """
    return _execute_parquet_resilient(
        conn,
        directory,
        sql,
        [],
        source=source,
        skip_event="coverage.parquet_skip",
        unreadable_event="coverage.parquet_unreadable",
    )


def _execute_candle_page_resilient(
    conn: Any,
    directory: Path,
    sql: str,
    extra_params: list[Any],
    *,
    source: str,
) -> tuple[list[str], list[tuple[Any, ...]]]:
    """Execute a candle-page ``SELECT`` and return ``(out_columns, raw_rows)``, resilient.

    The :func:`query_candles` page cursor needs the cursor *description* (the projected
    column names, which drive every downstream derived-column join) in addition to the
    rows, so it cannot use the row-only :func:`_execute_parquet_resilient`. SAME COMMON
    probe-and-skip (``db.*`` log namespace, DEC-DB-CORRUPT-SKIP-001; schema-aware since
    DEC-ALIEN-SCHEMA-READ-HEAL-001 — an alien-schema file folds into the skip set) via
    the shared :func:`_readable_parquet_subset`: fast path binds the glob; on an
    unreadable-Parquet error the readable subset is re-bound (the SELECT list is
    identical, so the column description is byte-identical to the fast path). When NO
    file passes the probes — or the subset re-run itself fails (the cross-file
    union-conflict residue, logged ONCE as ``db.parquet_unreadable``) — returns
    ``([], [])`` — the caller renders an empty (degraded-but-correct) page, never a 500,
    never a fabricated bar.
    """
    glob = directory.as_posix() + "/**/*.parquet"
    try:
        cursor = conn.execute(sql, [glob, *extra_params])
    except movers_read_skip_errors() as whole_glob_error:
        readable, _omitted = _readable_parquet_subset(
            conn,
            directory,
            whole_glob_error,
            sql=sql,
            extra_params=extra_params,
            source=source,
            skip_event="db.parquet_skip",
            unreadable_event="db.parquet_unreadable",
        )
        if not readable:
            return [], []
        try:
            cursor = conn.execute(sql, [readable, *extra_params])
        except movers_read_skip_errors() as subset_error:
            _LOG.error("db.parquet_unreadable", source=source, error=str(subset_error))
            return [], []
    out_columns = [str(col[0]) for col in (cursor.description or [])]
    return out_columns, list(cursor.fetchall())


def _retry_candle_read_once[T](
    operation: Callable[[], T],
    *,
    source: str,
    retry_sleep: Callable[[float], None] | None,
) -> T:
    """Run one candle count/page read, retrying ONCE on a transient ``duckdb.Error``.

    DEC-DB-CANDLES-TORN-READ-RETRY-001 — the ``/db`` candle-pager twin of the
    window-ledger torn-read guard (DEC-WINDOW-TORN-READ-RETRY-001, see
    :func:`build_window_payload` / :func:`phase_progress_payload`): the candle
    lake writers land atomically (``os.replace``), but a pager request performs
    MULTIPLE independent ``read_parquet`` opens (COUNT + page SELECT + the
    cursor ``fetchall`` streaming read), so a live pull's replace landing
    between/inside them surfaces as a transient ``duckdb.Error`` that
    previously 500'd the page. *operation* is re-run ONCE after
    :data:`_DUCKDB_RETRY_DELAY_S` (via *retry_sleep*, injectable for tests;
    ``None`` = the ALIASED module default ``from time import sleep`` — the bare
    name ``time`` here is ``datetime.time``). What the second attempt re-reads
    has TWO shapes (review fold — the original "every operation re-globs"
    claim was true only of the first): the LEGACY count/page operations
    rebuild their glob / file list from the partition directory per attempt,
    so the retry reads the freshly-replaced files; the #1228 manifest PRUNED
    page (:func:`_stats_pruned_page`) re-plans from the request's captured
    ``_StatsReadContext`` snapshot — a FIXED rel-path list, not a fresh glob —
    which stays correct because an ``os.replace``'d file's CONTENT is re-read
    on the retry and any stat-token movement is caught by
    :func:`_stats_tokens_intact` (⇒ whole-request legacy degrade), so the
    retry never serves a torn or stale manifest page. The retry covers the
    WHOLE execute+fetch — including :func:`_execute_candle_page_resilient`'s
    ``fetchall`` (outside its own corrupt-skip ``try``), the pruned page's
    ``fetchall``, and the corrupt-file degrade path's unguarded re-run — the
    exact windows the corrupt-skip ladder never guarded.

    Honest envelope (Golden Rule #10): retry-once NARROWS the race, it does
    not eliminate it — a SECOND replace landing inside the retry window still
    raises (a genuine SQL bug likewise still propagates after one bounded
    pause, mirroring the ledger guard's contract — never silently swallowed).
    """
    try:
        return operation()
    except duckdb.Error as exc:
        # Torn read (reader-side multi-open race vs the atomic lake writer) —
        # retry ONCE. The retry runs OUTSIDE any lock (the injected count
        # cache computes unlocked and caches NOTHING on a raise — the
        # phase_progress_payload precedent), so the second attempt recomputes
        # cleanly and a retried success is cached normally.
        _LOG.warning(
            "db.candle_read_retry",
            source=source,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        sleeper = _default_sleep if retry_sleep is None else retry_sleep
        sleeper(_DUCKDB_RETRY_DELAY_S)
        return operation()


#: The intervals the SPOT coverage/completeness VERDICTS actually read — the
#: single FETCHED granularity (``1minute``, DEC-1MINUTE-ONLY-DERIVE-1DAY-001)
#: plus the derived ``1day`` daily anchor. The 19 derived intraday TFs the
#: prebuild materialises are display-only derivatives of the same 1-minute
#: base, so scanning them adds nothing to any verdict
#: (DEC-COMPLETENESS-FNO-SKIP-001, refuter-amended register #12).
SPOT_VERDICT_INTERVALS: Final[tuple[str, str]] = ("1minute", "1day")


class ScopedSummaryRows(list[dict[str, Any]]):
    """Census-summary rows + the read-scope exclusion count (#1934).

    A ``list`` subclass (DEC-UNIVERSE-INDEX-ONLY-001 R2/R9) so every existing
    consumer — the SummaryCache SPOT/OPTIONS entries,
    ``build_spot_coverage_payload`` / ``build_option_coverage_payload``, the
    completeness inputs — keeps its exact list contract while the
    ``scope_excluded`` census (how many stored-only symbols/underlyings the
    read scope excluded from THIS scan) rides the SAME object the cache
    stores: a cached serve can never pair rows with a foreign exclusion count
    (Golden Rule #1).
    """

    scope_excluded: int

    def __init__(self, rows: Iterable[dict[str, Any]], *, scope_excluded: int) -> None:
        super().__init__(rows)
        self.scope_excluded = scope_excluded


def summary_rows_scope_excluded(rows: object) -> int:
    """The scope-exclusion census carried by *rows* — ``0`` for a plain list.

    The ONE reader of :class:`ScopedSummaryRows`'s census (CLAUDE.md §11
    COMMON) so the payload sites never re-derive it: an unfiltered scan (a
    plain ``list`` — the ``scope=None`` byte-identical arm) reads ``0``.
    """
    if isinstance(rows, ScopedSummaryRows):
        return rows.scope_excluded
    return 0


def summarize_spot_verdict_datasets(
    conn: Any, lake_root: Path, *, scope: FeedScope | None = None
) -> list[dict[str, Any]]:
    """The FNO-pruned, verdict-interval-bounded spot summary scan — the dashboard's scan.

    DEC-COMPLETENESS-FNO-SKIP-001 (mechanization-sweep register #12,
    refuter-amended): after the pipeline prebuild (DEC-PIPELINE-AUTOPREBUILD-001
    #1145) a fully-materialised lake holds ~10⁵-10⁶ F&O contract × interval
    datasets, and the UNFILTERED :func:`summarize_spot_datasets` walked + read a
    Parquet-footer DISTINCT-timestamp aggregate for EVERY one of them — the
    first ``/completeness`` open blocked for tens of minutes. The SPOT verdicts
    only ever read the ``INDEX``/``CASH`` segments at the fetched ``1minute``
    (+ the derived ``1day`` anchor), so this ONE wrapper (CLAUDE.md §11 COMMON —
    both ``/coverage/spot`` and the completeness inputs call it) prunes the
    ``FNO`` segment at the WALK level (never descended) and bounds the spot
    intervals to :data:`SPOT_VERDICT_INTERVALS`. The spot rows it returns are
    byte-identical to the unfiltered scan's spot ``1minute``/``1day`` rows
    (regression-pinned), so every verdict is unchanged. The ``/coverage`` F&O
    tabs keep their own scans; :func:`summarize_spot_datasets` itself stays
    UNFILTERED by default — the F&O ledger⇄lake cold-start reconcile
    (``load_lake_spans``) depends on the full walk.

    Universe scope (DEC-UNIVERSE-INDEX-ONLY-001 R2, #1934): a BOUNDED *scope*
    (the routes thread ``resolve_read_scope()`` — §2.4 read-default law:
    ``None``/unbounded = the unfiltered byte-identical legacy scan) screens
    each discovered dataset through the §3.5
    :func:`~brutex.data.scope_filter.scope_admits_dataset` predicate AT
    DISCOVERY — an excluded stock dataset also skips its per-dataset DuckDB
    aggregate (the DEC-COMPLETENESS-FNO-SKIP-001 blocked-for-minutes class,
    never re-paid for scoped-out symbols) — and the filtered rows return as a
    :class:`ScopedSummaryRows` carrying the distinct-symbol exclusion census for
    the payloads' emit-on-nonzero echo. ``load_lake_spans`` NEVER threads this
    seam (the twice-pinned unfiltered cold-start contract above).
    """
    prune = frozenset({FNO_CANDLE_LAKE_SEGMENT})
    if scope is None or not scope.bounded():
        return summarize_spot_datasets(
            conn,
            lake_root,
            exclude_segments=prune,
            intervals=SPOT_VERDICT_INTERVALS,
        )
    bound = scope
    excluded: set[tuple[str, str, str]] = set()

    def _admit(exchange: str, segment: str, symbol: str) -> bool:
        if scope_admits_dataset(exchange, segment, symbol, bound):
            return True
        excluded.add((exchange, segment, symbol))
        return False

    rows = summarize_spot_datasets(
        conn,
        lake_root,
        exclude_segments=prune,
        intervals=SPOT_VERDICT_INTERVALS,
        admit=_admit,
    )
    return ScopedSummaryRows(rows, scope_excluded=len(excluded))


def summarize_spot_datasets(
    conn: Any,
    lake_root: Path,
    *,
    session_date_segments: frozenset[str] | None = None,
    exclude_segments: frozenset[str] = frozenset(),
    intervals: Collection[str] | None = None,
    admit: Callable[[str, str, str], bool] | None = None,
) -> list[dict[str, Any]]:
    """Summarise every spot candle dataset in the lake for the coverage page.

    Discovers each ``(exchange, segment, symbol, interval)`` partition set via
    :func:`list_candle_datasets`, then reads ONE aggregate row per set straight
    from the Parquet lake via DuckDB ``read_parquet`` over the partition glob —
    no data duplication, the lake stays the single source of truth (CLAUDE.md
    §11 COMMON). Read-only by construction: a single ``SELECT`` aggregate per
    set, no write path. A click never materialises bar rows — only the
    aggregate scalars.

    For a dataset at the ONE fetched spot granularity
    (:data:`TRACK1_SPOT_INTERVAL`, ``1minute``) in a SCORED spot segment
    (``INDEX``/``CASH`` — :data:`~brutex.data.partition.SPOT_PROBE_SEGMENTS`,
    the only segments the completeness projections join; hostile-review perf
    fold) the aggregate is the folded per-session CTE of
    DEC-DAILY-OPEN-SUBSTITUTION-HONESTY-001 — ONE scan (the SAME O(bars) order
    as the historical 3-scalar aggregate, never a second full-column pass; the
    #920 hot-path guard) that groups the bars by their IST calendar date
    (:data:`_IST_DATE_EXPR`) and derives:

    * ``bars`` = ``SUM`` of the per-session DISTINCT-timestamp counts (equal to
      the historical whole-dataset ``COUNT(DISTINCT "timestamp")`` — a
      timestamp belongs to exactly one IST date, so the dedup of
      DEC-LAKE-READ-UNIQUE-001 is preserved);
    * ``first_date`` / ``last_date`` = ``MIN`` / ``MAX`` of the per-session IST
      dates (equal to the IST date of the ``MIN`` / ``MAX`` timestamp — the
      fixed +05:30 offset is monotone);
    * ``open_substituted_days`` = how many NON-Muhurat sessions' FIRST present
      minute (``MIN`` of :data:`_IST_TS_EXPR`) is NOT exactly the canonical
      09:15 IST session open (:data:`_SESSION_OPEN_MINUTES`, derived from the
      COMMON :data:`brutex.data.session.SESSION_OPEN`) — exactly the sessions
      whose DERIVED 1-day open is the earliest-present-minute substitute
      (``tf_resampler`` aggregates a bucket's open with ``.first()``), not the
      canonical 09:15 price. A session opening exactly at 09:15:00 is NOT
      counted; a stray PRE-09:15 first bar (should not survive the ingest
      session filter) IS (``<>``, never silently trusted); a Muhurat session
      ABSTAINS (its open is off-09:15 BY DESIGN —
      :func:`_muhurat_exclusion_sql`). The rule is the ALIGNED predicate of
      DEC-DAILY-OPEN-PREDICATE-ALIGN-001 — the mirror-pinned SQL twin of
      :func:`brutex.data.spot_daily_resample.classify_daily_open`.
      INFORMATIONAL only — no verdict downgrade;
    * ``session_days`` = the number of distinct IST session dates stored.

    Every NON-``1minute`` dataset (the derived ``1day`` + intraday TFs) AND
    every non-scored-segment ``1minute`` dataset (the REFERENCE_ONLY F&O
    contract partitions — unless a segment is explicitly opted in via
    *session_date_segments*, the DEC-COLDSTART-SEED-INTERIOR-GAP-001
    extensibility path, in which case it takes the CTE and carries real
    counts) keeps the historical 3-scalar aggregate byte-identical and carries
    ``open_substituted_days`` / ``session_days`` as ``None`` — the substitution
    signal is only meaningful on the fetched granularity the 1-day is derived
    FROM, and only surfaced where a projection actually consumes it.

    **Honest envelope (Golden Rule #10):** a dataset PRESENT in the lake counts
    as *pulled*; ``bars`` / ``first_date`` / ``last_date`` describe WHAT is
    stored, NOT whether the full ``2020 → today`` range is complete. What IS
    now audited (DEC-SPOT-PARTIAL-RANGE-CHIP-001): ``session_days`` — the
    per-session CTE's distinct-IST-date count (via the ambient-TZ-robust
    :data:`_IST_DATE_EXPR`, DEC-SPOT-SESSION-DAYS-IST-TZ-001) — feeds the
    ``/completeness`` amber ``settled_partial_range`` session-density verdict.
    The PER-WINDOW trading-day gap audit and the options-side range audit
    remain explicit follow-ups, not claimed.
    ``open_substituted_days`` is a SUBSTITUTION signal, not exclusively "vendor
    lost data": a genuine late/special NON-Muhurat open, a sparse reference
    symbol, and a vendor gap all count — each honestly means the derived daily
    open is not the canonical 09:15 price. A Muhurat session no longer counts
    (DEC-DAILY-OPEN-PREDICATE-ALIGN-001 — its off-09:15 open is BY DESIGN, so
    counting it flagged a healthy lake every year; the prebuild detector
    already abstained, and the two surfaces now share the ONE predicate). The
    former strict-``>`` under-count of a stray BEFORE-open first bar is also
    closed by the aligned ``<>``: a pre-09:15 first bar (out of the CLAUDE.md
    §7 pre-open-exclusion contract, yet still the resampler's ``.first()``
    substitute) now COUNTS, never silently trusted. The ONE remaining
    deliberate divergence from the prebuild detector is SCOPE/CUTOFF: this
    census counts ALL stored sessions of the scored datasets (no
    forming-session cutoff), while the detector applies the ``ist_date <
    before`` cutoff over the SPOT prebuild targets — so the two surfaces can
    differ only on a not-yet-derived forming session (or a dataset outside
    the prebuild target set), NEVER on the predicate itself (pinned by
    ``test_predicate_alignment_pin_vs_honesty_counter_1267``).

    Parameters
    ----------
    conn
        A DuckDB connection (see :func:`table_summary`).
    lake_root
        Top-level lake directory (the parent of ``bars/``).
    session_date_segments
        OPT-IN density enrichment (DEC-COLDSTART-SEED-INTERIOR-GAP-001): when a
        dataset's ``segment`` is in this set AND its ``interval`` is the ONE
        fetched spot granularity (:data:`TRACK1_SPOT_INTERVAL`, ``1minute`` —
        a derived ``1day`` must never carry the density signal, or the
        cold-start seed would leave a never-fetched window permanently
        unseeded), the SAME single per-session CTE additionally
        ``array_agg``-s the dataset's DISTINCT IST bar dates and the row gains
        a ``session_dates`` frozenset — the ledger cold-start's interior-gap
        density cross-check input. The default ``None`` omits the key (the hot
        /coverage + /completeness pollers pass nothing).
    exclude_segments, intervals
        Optional walk-level pruning, threaded verbatim into
        :func:`list_candle_datasets` (DEC-COMPLETENESS-FNO-SKIP-001 — the
        dashboard verdict scans call the pruned
        :func:`summarize_spot_verdict_datasets`). The defaults are
        byte-identical to the unfiltered scan, which the F&O ledger⇄lake
        reconcile (``load_lake_spans``) still requires.
    admit
        OPT-IN dataset predicate (DEC-UNIVERSE-INDEX-ONLY-001 R2, #1934):
        called with ``(exchange, segment, symbol)`` at dataset DISCOVERY —
        a ``False`` skips the dataset AND its per-dataset DuckDB aggregate
        (excluded symbols cost zero Parquet I/O). The default ``None`` is the
        unfiltered byte-identical walk; ONLY the verdict wrapper
        (:func:`summarize_spot_verdict_datasets`) threads it —
        ``load_lake_spans`` (the pull-path cold-start reconcile) deliberately
        never does (the pinned unfiltered contract).

    Returns
    -------
    list[dict[str, Any]]
        One row per discovered dataset, in :func:`list_candle_datasets` order::

            {"exchange", "segment", "symbol", "interval",
             "bars": <int>, "first_date": <YYYY-MM-DD|None>,
             "last_date": <YYYY-MM-DD|None>,
             "open_substituted_days": <int|None>, "session_days": <int|None>
             [, "session_dates": frozenset[date]]}
    """
    rows: list[dict[str, Any]] = []
    for dataset in list_candle_datasets(
        lake_root, exclude_segments=exclude_segments, intervals=intervals
    ):
        exchange = dataset["exchange"]
        segment = dataset["segment"]
        symbol = dataset["symbol"]
        interval = dataset["interval"]
        # Universe-scope screen (DEC-UNIVERSE-INDEX-ONLY-001 R2): an excluded
        # dataset is skipped BEFORE its aggregate — zero Parquet I/O for
        # scoped-out symbols. ``None`` (every legacy caller + load_lake_spans'
        # unfiltered cold-start contract) is byte-identical.
        if admit is not None and not admit(exchange, segment, symbol):
            continue
        is_fetched_minute = interval == TRACK1_SPOT_INTERVAL
        # Belt half of the DEC-COLDSTART-SEED-INTERIOR-GAP-001 REVISION-1 gate
        # (the braces half lives in `seed_enclosed_windows`): session dates are
        # attached ONLY for an opted-in segment at the FETCHED `1minute`
        # granularity, so a derived `1day` row never carries the density signal.
        with_session_dates = (
            session_date_segments is not None
            and segment in session_date_segments
            and is_fetched_minute
        )
        # Hostile-review perf fold (DEC-DAILY-OPEN-SUBSTITUTION-HONESTY-001): the
        # folded per-session CTE runs ONLY for the SCORED spot segments
        # (INDEX/CASH — `SPOT_PROBE_SEGMENTS`, the only segments the completeness
        # projections join) or a segment explicitly opted into the session-dates
        # enrichment; the many REFERENCE_ONLY F&O `1minute` contract partitions
        # keep the historical flat 3-scalar aggregate — their per-session
        # substitution counts were computed then DISCARDED on the #920-sensitive
        # /completeness + /coverage hot path. `with_session_dates` ⇒
        # `use_session_cte`, so an opted-in row can never miss its `array_agg`.
        use_session_cte = is_fetched_minute and (
            segment in _SPOT_PROBE_SEGMENTS or with_session_dates
        )
        # The glob is built from list_candle_datasets coordinates (each already
        # validated against the partition allowlists during discovery), never
        # free user text; the path is given to read_parquet as a bound parameter.
        # Resilient read: a single unreadable Parquet file degrades this dataset
        # to its readable subset (or 0 bars), never a 500 (DEC-COVERAGE-PARQUET-
        # RESILIENT-001) — including an ALIEN-schema file, which the schema-aware
        # probe skips (DEC-ALIEN-SCHEMA-READ-HEAL-001; pre-fix that class
        # RE-RAISED and this claim was false for it).
        # Bars are counted as DISTINCT timestamps (DEC-LAKE-READ-UNIQUE-001) so a
        # poisoned-era duplicated partition cannot inflate the coverage/
        # completeness bar counts above what the deduped readers actually serve.
        # ``session_days`` (DEC-SPOT-PARTIAL-RANGE-CHIP-001) — the CTE's
        # ``COUNT(*)`` of per-session rows, i.e. the DISTINCT IST calendar dates
        # holding at least one bar — additionally feeds the /completeness
        # session-density range audit with ZERO extra passes over the lake. The
        # IST date comes from the COMMON :data:`_IST_DATE_EXPR` (AT TIME ZONE
        # 'UTC' BEFORE the +05:30 shift), so the count is independent of the
        # ambient DuckDB session ``TimeZone`` — a bare CAST-to-DATE on the
        # TIMESTAMPTZ column resolves in the session zone and split one NSE
        # session into two dates on a negative-offset host, inflating
        # ``session_days`` and masking sparsity (DEC-SPOT-SESSION-DAYS-IST-TZ-001).
        if use_session_cte:
            # The ONE folded per-session CTE (DEC-DAILY-OPEN-SUBSTITUTION-
            # HONESTY-001, shared with the (a)-fix session_dates enrichment) —
            # a single O(bars) scan, never a second full-column pass (#920).
            session_expr = ", array_agg(DISTINCT ist_date)" if with_session_dates else ""
            sql = (
                "WITH per_session AS ("  # noqa: S608  # nosec B608 - static fragments from hardcoded constants; path bound
                f"SELECT {_IST_DATE_EXPR} AS ist_date, "
                f"MIN({_IST_TS_EXPR}) AS first_ist, "
                f"{distinct_count_sql(('timestamp',))} AS n "
                "FROM read_parquet(?, union_by_name=true) GROUP BY ist_date) "
                "SELECT SUM(n), MIN(ist_date), MAX(ist_date), "
                "COUNT(*) FILTER (WHERE first_ist <> ist_date "
                f"+ INTERVAL '{_SESSION_OPEN_MINUTES} minutes'{_MUHURAT_EXCLUSION_SQL}), "
                f"COUNT(*){session_expr} FROM per_session"
            )
        else:
            sql = (
                f'SELECT {distinct_count_sql(("timestamp",))}, MIN("timestamp"), MAX("timestamp") '  # noqa: S608  # nosec B608 - static fragment from hardcoded key columns; path bound
                "FROM read_parquet(?, union_by_name=true)"
            )
        agg_rows = _aggregate_parquet_resilient(
            conn,
            lake_root / "bars" / exchange / segment / symbol / interval,
            sql,
            source=f"spot:{exchange}/{segment}/{symbol}/{interval}",
        )
        agg = agg_rows[0] if agg_rows else None
        row: dict[str, Any] = {
            "exchange": exchange,
            "segment": segment,
            "symbol": symbol,
            "interval": interval,
        }
        if use_session_cte:
            # A zero-row partition aggregates the CTE to (NULL, NULL, NULL, 0,
            # 0[, NULL]) and an all-unreadable one yields no aggregate row at
            # all — both honestly degrade to 0 bars / no dates / (0, 0) counts,
            # never a crash / never a fabricated verdict (Golden Rule #1).
            row["bars"] = int(agg[0]) if agg is not None and agg[0] is not None else 0
            row["first_date"] = _iso_date(agg[1]) if agg else None
            row["last_date"] = _iso_date(agg[2]) if agg else None
            row["open_substituted_days"] = int(agg[3]) if agg else 0
            row["session_days"] = int(agg[4]) if agg else 0
        else:
            row["bars"] = int(agg[0]) if agg else 0
            row["first_date"] = _ist_date(agg[1]) if agg else None
            row["last_date"] = _ist_date(agg[2]) if agg else None
            # The substitution signal is COMPUTED only where the per-session CTE
            # ran (the fetched 1-minute granularity in a scored/opted-in
            # segment) — a derived TF row AND an F&O `1minute` contract row each
            # carry an honest None, never 0 (0 would assert "checked, found
            # none").
            row["open_substituted_days"] = None
            row["session_days"] = None
        if with_session_dates:
            # DuckDB `array_agg` over zero rows is SQL NULL (→ None) and an
            # all-unreadable partition yields no aggregate row at all — both
            # degrade to an EMPTY set, over which the seed's density check
            # honestly ABSTAINS (too-few sessions), never a crash / never a
            # fabricated verdict (Golden Rule #1).
            raw_sessions = agg[5] if agg else None
            row["session_dates"] = (
                frozenset(raw_sessions) if raw_sessions is not None else frozenset()
            )
        rows.append(row)
    return rows


def summarize_option_datasets(
    conn: Any, lake_root: Path, *, scope: FeedScope | None = None
) -> list[dict[str, Any]]:
    """Summarise the expired-options lake per underlying for the coverage page.

    The options-side analog of :func:`summarize_spot_datasets`
    (DEC-OPTIONS-COVERAGE-LAKE-001): reads ONE aggregate per
    ``(exch_id, underlying_symbol)`` straight from the expired-options Parquet
    lake (``<lake>/options/**/*.parquet``) via DuckDB ``read_parquet`` — the
    number of distinct series, total bars, and first/last bar date. The inner
    GROUP BY collapses each ``(expiry_flag, expiry_code, strike_offset, side,
    strike)`` series; the outer GROUP BY rolls those up per underlying, so
    ``series`` is a plain ``COUNT(*)`` of the inner rows. Read-only by
    construction — a single aggregate query, no bar rows materialised. A missing
    / empty ``options`` directory yields ``[]`` (mirrors :func:`list_option_series`).

    **Honest envelope (Golden Rule #10):** a series PRESENT in the lake counts
    as *pulled*; ``series`` / ``bars`` / ``first_date`` / ``last_date`` describe
    WHAT is stored, NOT whether the full ``2020 → today`` range is complete —
    partial-range detection is an explicit follow-up, mirroring the spot side.

    Universe scope (DEC-UNIVERSE-INDEX-ONLY-001 R9, #1934): a BOUNDED *scope*
    (the route threads ``resolve_read_scope()``; ``None``/unbounded = the
    unfiltered byte-identical legacy read) applies a POST-AGGREGATE row filter
    on :func:`~brutex.data.scope_filter.scope_admits_fno_contract` over each
    grouped row's ``(exch_id, underlying_symbol)`` — a junk/uncoercible
    ``exch_id`` token is excluded + counted under a bounded scope (the §3.5
    totality law). HONEST cost envelope — NO R2-style economy exists here:
    this is ONE whole-tree aggregate, so every stored stock series' Parquet
    footers are still read regardless of scope; only OUTPUT rows are excluded
    + counted (the :class:`ScopedSummaryRows` census feeds the payload's
    emit-on-nonzero echo). The /db options DISCOVERY surfaces
    (:func:`list_option_datasets` / :func:`list_option_series`) stay
    deliberately UNFILTERED (the R5 raw-viewer law — negative-pinned).

    Parameters
    ----------
    conn
        A DuckDB connection.
    lake_root
        Top-level lake directory (the parent of ``options/``).
    scope
        The read-union scope, or ``None`` (default — unfiltered).

    Returns
    -------
    list[dict[str, Any]]
        One row per underlying, sorted by ``(exchange, underlying)``::

            {"underlying", "exchange", "series": <int>, "bars": <int>,
             "first_date": <YYYY-MM-DD|None>, "last_date": <YYYY-MM-DD|None>}
    """
    options = lake_root / OPTIONS_DIR_NAME
    if not options.is_dir() or not any(options.rglob("*.parquet")):
        return []
    # read_parquet path is parameter-bound; the column list is the fixed
    # addressing set (no free user text). Inner query = one row per series;
    # the outer rolls up per underlying, so COUNT(*) is the series count.
    # Resilient read: one corrupt Parquet file is SKIPPED (logged), never a 500
    # (DEC-COVERAGE-PARQUET-RESILIENT-001) — alien-schema files included via the
    # schema-aware probe (DEC-ALIEN-SCHEMA-READ-HEAL-001; pre-fix that class
    # RE-RAISED and this claim was false for it).
    # The inner per-series bar count is DISTINCT timestamps (the group key
    # already fixes `side`, so `(side, timestamp)` reduces to `timestamp` here) —
    # a poisoned-era duplicate cannot inflate it (DEC-LAKE-READ-UNIQUE-001).
    rows = _aggregate_parquet_resilient(
        conn,
        options,
        "SELECT exch_id, underlying_symbol, COUNT(*) AS series, "  # noqa: S608  # nosec B608 - static SQL + hardcoded-key dedup fragment; path bound
        "SUM(bars) AS bars, MIN(first_ts) AS first_ts, MAX(last_ts) AS last_ts FROM ("
        "  SELECT exch_id, underlying_symbol, expiry_flag, expiry_code, strike_offset, "
        f"         side, strike, {distinct_count_sql(('timestamp',))} AS bars, "
        '         MIN("timestamp") AS first_ts, '
        '         MAX("timestamp") AS last_ts '
        "  FROM read_parquet(?, union_by_name=true) "
        "  GROUP BY exch_id, underlying_symbol, expiry_flag, expiry_code, strike_offset, side, strike"
        ") GROUP BY exch_id, underlying_symbol ORDER BY exch_id, underlying_symbol",
        source="options",
    )
    summarised = [
        {
            "underlying": row[1],
            "exchange": row[0],
            "series": int(row[2]),
            "bars": int(row[3]),
            "first_date": _ist_date(row[4]),
            "last_date": _ist_date(row[5]),
        }
        for row in rows
    ]
    if scope is None or not scope.bounded():
        return summarised
    # R9 post-aggregate scope filter (DEC-UNIVERSE-INDEX-ONLY-001, #1934):
    # exclude + count OUTPUT rows whose (exch_id, underlying) is out of scope —
    # the whole-tree aggregate above already ran (no discovery economy exists
    # on this ONE-query surface; disclosed in the docstring).
    kept = [
        row
        for row in summarised
        if scope_admits_fno_contract(str(row["exchange"]), str(row["underlying"]), scope)
    ]
    excluded = len(summarised) - len(kept)
    if excluded:
        _LOG.info(
            "coverage.options.scope_excluded",
            excluded=excluded,
            kept=len(kept),
            dec="DEC-UNIVERSE-INDEX-ONLY-001",
        )
    return ScopedSummaryRows(kept, scope_excluded=excluded)


def _validate_option_segment(series: str) -> None:
    """Validate a discovered option-series directory name, else raise ValueError.

    The on-disk segment is the writer's filesystem-safe ``_safe_segment`` output
    (``brutex.data.option_parquet_writer``): a ``|``-joined series coordinate
    with ``|`` → ``__``. We re-validate it against :data:`_OPTION_SEGMENT_RE`
    (plus a ``..`` reject) so a hand-crafted / foreign directory name can never
    traverse outside ``<lake>/options`` when joined into the read glob — the
    options-side analog of :func:`_validate_candle_coords`.
    """
    if ".." in series or not _OPTION_SEGMENT_RE.match(series):
        raise ValueError(f"illegal option series {series!r}")


#: The on-disk separator the option writer substitutes for the series key's
#: ``|`` (``option_parquet_writer._SAFE``, kept in lockstep like
#: :data:`OPTIONS_DIR_NAME`).
_OPTION_SEGMENT_SEP: Final = "__"

#: The option series directory segment's part arity: the writer's partition
#: key is ``<exchange_segment>|<security_id>|<expiry_flag>|<expiry_code>|
#: <strike>|<drv_option_type>`` (``option_parquet_writer.partition_key``), so
#: the on-disk segment splits into exactly 6 parts on ``__``.
_OPTION_SERIES_ARITY: Final = 6


def _decode_option_dataset(series: str, symbol_for_id: Mapping[str, str] | None) -> str | None:
    """Decode one on-disk option series segment into a PRECISE display label.

    The dataset-picker twin of :func:`_decode_option_window`
    (DEC-DB-OPTION-DATASET-DECODE-001 — the #629 tracked follow-up): the /db
    expired-options picker rendered the raw directory segment
    (``NSE_FNO__25__MONTH__1__ATM-10__CALL`` — the numeric vendor id, not the
    operator's symbol). Returns a ``"BANKNIFTY · MONTH#1 ATM-10 CALL · 1m"``-style
    label, or ``None`` for any shape that does not parse (wrong part arity, an
    empty part) — the caller keeps the raw segment as the label: fallback,
    never an exception, never a hidden dataset. An unknown id keeps the numeric
    id so the entry is still distinguishable. Display-only: the ``series`` KEY
    the queries address is never touched.
    """
    parts = series.split(_OPTION_SEGMENT_SEP)
    if len(parts) != _OPTION_SERIES_ARITY or not all(parts):
        return None
    _segment, sid, flag, code, strike, opt_type = parts
    label = (symbol_for_id or {}).get(sid, sid)
    return f"{label} · {flag}#{code} {strike} {opt_type} · 1m"


def list_option_datasets(
    lake_root: Path,
    *,
    symbol_for_id: Mapping[str, str] | None = None,
) -> list[dict[str, str]]:
    """Discover which expired-option series exist under ``<lake>/options/``.

    Walks ``<lake_root>/options/<series>/`` (the layout
    :class:`~brutex.data.option_parquet_writer.OptionParquetWriter` writes) and
    returns one entry per series directory that holds at least one ``.parquet``
    file. Directory names that are not safe option segments are skipped (a
    foreign / partially-written tree never raises). A missing ``options``
    directory yields ``[]``.

    The expired-option lake has no ``(exchange, segment, symbol, interval)``
    coordinates the way the spot ``bars/`` tree does — one series IS one
    partition key (``exchange_segment|security_id|expiry_flag|expiry_code|
    strike|drv_option_type``, ``|`` → ``__`` on disk). The whole series rolls a
    1-minute table, so we surface the ``series`` segment as the dataset
    coordinate and stamp ``"1minute"`` as the interval for a uniform picker.

    Each entry additionally carries a display-only ``label``
    (DEC-DB-OPTION-DATASET-DECODE-001): the series decoded to the operator's
    coordinates (``BANKNIFTY · MONTH#1 ATM-10 CALL · 1m``) via
    :func:`_decode_option_dataset`, falling back to the RAW segment for any
    non-parsing shape — a dataset is never hidden and the ``series`` KEY stays
    byte-unchanged (queries keep addressing the on-disk segment).

    Parameters
    ----------
    lake_root
        Top-level lake directory (the parent of ``options/``).
    symbol_for_id
        Optional ``str(SECURITY_ID) → engine symbol`` map (the
        DEC-WINDOW-OPTION-DECODE-001 pattern) so the label shows the
        underlying's symbol (``BANKNIFTY``) instead of the vendor id the
        segment carries (``25``). ``None`` / a missing id keeps the numeric
        id — display-only, never a data change.

    Returns
    -------
    list[dict[str, str]]
        ``[{"kind": "option", "series", "interval", "label"}]`` sorted by
        series — the candle-viewer's option-dataset picker entries.
    """
    options = lake_root / OPTIONS_DIR_NAME
    if not options.is_dir():
        return []
    found: list[dict[str, str]] = []
    for series_dir in sorted(p for p in options.iterdir() if p.is_dir()):
        if ".." in series_dir.name or not _OPTION_SEGMENT_RE.match(series_dir.name):
            continue
        if any(series_dir.glob("*.parquet")):
            found.append(
                {
                    "kind": "option",
                    "series": series_dir.name,
                    "interval": "1minute",
                    "label": _decode_option_dataset(series_dir.name, symbol_for_id)
                    or series_dir.name,
                }
            )
    return found


def list_option_series(
    conn: Any,
    lake_root: Path,
    *,
    underlying_symbol: str | None = None,
) -> list[dict[str, Any]]:
    """Return the DISTINCT expired-option series in the lake (DEC-EXPIRED-OPTIONS-TABLE-001).

    Replaces the dropped ``expiries`` / ``contracts`` DuckDB tables as the
    UI's option-series source: reads the expired-options Parquet lake
    (``<lake>/options/**/*.parquet``) and returns one entry per distinct
    ``(exchange, underlying_symbol, expiry_flag, expiry_code, strike_offset,
    side, strike)`` tuple, derived from the real addressing columns now
    promoted onto every bar. Filters by *underlying_symbol* when supplied.

    A missing / empty ``options`` directory yields ``[]`` (never raises),
    mirroring :func:`list_option_datasets`.

    Parameters
    ----------
    conn
        A DuckDB connection.
    lake_root
        Top-level lake directory (the parent of ``options/``).
    underlying_symbol
        Restrict to one underlying (e.g. ``"NIFTY"``), or ``None`` for all.

    Returns
    -------
    list[dict[str, Any]]
        ``[{"exchange", "underlying_symbol", "expiry_flag", "expiry_code",
        "strike_offset", "side", "strike"}]`` sorted for a stable UI order.
    """
    options = lake_root / OPTIONS_DIR_NAME
    if not options.is_dir() or not any(options.rglob("*.parquet")):
        return []
    glob = options.as_posix() + "/**/*.parquet"
    where_sql = ""
    params: list[Any] = [glob]
    if underlying_symbol is not None:
        where_sql = " WHERE underlying_symbol = ?"
        params.append(underlying_symbol)
    # read_parquet path + every value parameter-bound; the SELECT column list
    # is the fixed addressing-column set (no free user text) — no injection.
    rows = conn.execute(
        "SELECT DISTINCT exch_id, underlying_symbol, expiry_flag, expiry_code, "  # noqa: S608  # nosec B608 - bound path + fixed columns
        "strike_offset, side, strike "
        "FROM read_parquet(?, union_by_name=true)"
        f"{where_sql} "
        "ORDER BY underlying_symbol, expiry_flag, expiry_code, strike_offset, side, strike",
        params,
    ).fetchall()
    return [
        {
            "exchange": row[0],
            "underlying_symbol": row[1],
            "expiry_flag": row[2],
            "expiry_code": int(row[3]) if row[3] is not None else None,
            "strike_offset": row[4],
            "side": row[5],
            "strike": float(row[6]) if row[6] is not None else None,
        }
        for row in rows
    ]


# ─── Lake STATS-manifest read integration (DEC-LAKE-STATS-MANIFEST-001) ──────
#
# The read half of the per-file stats manifest (the #867/#1025 named future
# work, since SHIPPED by DEC-LAKE-STATS-MANIFEST-001 — MIGRATION.md #1614):
# when a request opts in (``use_stats_manifest=True`` — the production
# /db routes do; the pure-function default stays ``False`` so every direct
# caller keeps the byte-identical legacy query pattern) AND the kill switch
# (``BRUTEX_LAKE_STATS_MANIFEST``) is not ``off``, the candle pagers consult
# the dataset's validated stats shard to (a) answer the page TOTAL from the
# stamped per-file DISTINCT-key counts and (b) PRUNE the page query's file list
# to only the files the requested page needs — O(page-files) Parquet work
# plus an O(D) per-request revalidation stat sweep (D = the ONE dataset's
# month files), NOT literal O(page)/O(1) — the MIGRATION.md #1614 honest
# envelope.
# ANY validation/read failure falls back to the byte-identical legacy live
# scan (one ``lake.stats_manifest.fallback`` log naming the clause) and the
# read side reseeds the shard best-effort — the reader NEVER serves a number
# it could not revalidate against the live directory THIS request.

_STATS_INSIDE: Final[str] = "inside"
_STATS_OUTSIDE: Final[str] = "outside"
_STATS_STRADDLE: Final[str] = "straddle"

#: The reader-side fallback event — deliberately the SAME event name the leaf
#: module's validation gate emits, so one grep finds every manifest→legacy
#: demotion with the failed clause named (never a silent fallback).
_STATS_FALLBACK_EVENT: Final[str] = "lake.stats_manifest.fallback"

#: Per-request provenance event (the #1145 stored-first honesty pattern):
#: names whether the page was manifest-served or ran the legacy live scan.
#: Emitted at INFO — production logging defaults to INFO, and an operator must
#: be able to POSITIVELY confirm the O(page-files) path is being taken (a
#: DEBUG-only event made a healthy manifest-served page invisible while only
#: fallbacks surfaced — the silent-degradation gap in reverse).
_STATS_READ_EVENT: Final[str] = "db.stats_manifest.read"

#: Read-side seed skipped/aborted event (best-effort — never an error).
_STATS_SEED_SKIPPED_EVENT: Final[str] = "db.stats_manifest.seed_skipped"


def _stats_file_verdict(
    row: StatsFileRow, start: date | None, end: date | None, *, ist_dates: bool
) -> str:
    """Classify ONE manifest file row against the inclusive date bounds.

    The ONE shared classifier used by BOTH the manifest TOTAL and the page
    planner (so ``total ≡ Σ pages`` by construction). The row's stamped UTC
    bounds are converted to the SAME calendar the SQL ``WHERE`` compares —
    the IST date for the derived ``1day`` interval (:data:`_IST_DATE_EXPR`),
    the UTC date otherwise (:data:`_UTC_DATE_EXPR`). ``timestamp → calendar
    date`` is monotonic under a fixed offset, so the file-level verdict is
    exact (this is how the midnight-IST = prior-day-18:30-UTC ``1day`` skew is
    handled: the IST calendar itself classifies, and a file the bounds cut
    through is demoted to a straddler — scanned, never guessed).

    Returns
    -------
    str
        ``"outside"`` — no bar of the file can match the ``WHERE``
        (contributes 0); ``"inside"`` — every bar matches (contributes the
        stamped distinct count); ``"straddle"`` — the bounds cut through the
        file (its in-bound count must be SCANNED).
    """
    lo = (row.min_ts.astimezone(_IST) if ist_dates else row.min_ts).date()
    hi = (row.max_ts.astimezone(_IST) if ist_dates else row.max_ts).date()
    if (end is not None and lo > end) or (start is not None and hi < start):
        return _STATS_OUTSIDE
    if (start is None or lo >= start) and (end is None or hi <= end):
        return _STATS_INSIDE
    return _STATS_STRADDLE


@dataclass
class _StatsReadContext:
    """Per-request stats-manifest state for ONE dataset + one bounds set.

    Built once per request by :func:`_stats_read_context` (``None`` when the
    feature is off), consumed by the reader's TOTAL closure, the page pruner,
    and the span fold. ``served`` starts ``True`` only when the shard passed
    the FULL anti-illusion validation AND every straddler count succeeded; a
    mid-request failure (pruned-read error / column mismatch / moved stat
    token) flips it ``False`` so every later consumer of the SAME request
    (span fold, provenance) degrades to legacy together (the whole-request
    degrade law).

    Attributes
    ----------
    conn, lake_root, kind, key, base, identity
        The dataset coordinates + the DuckDB connection the request owns.
    stats
        The validated shard, or ``None`` when validation failed (⇒ the read
        side reseeds after the legacy scan — :attr:`should_seed`).
    effective
        Per-file IN-BOUND distinct-key counts in rel-ascending (chronological)
        order: the stamped ``rows`` for a fully-inside file, ``0`` for a fully
        outside one, the live bounded count for a straddler.
    straddlers
        The straddler rows (their counts came from a live scan, so the
        post-query TOCTOU re-stat covers them too).
    served
        Whether the manifest may serve THIS request (see above).
    """

    conn: Any
    lake_root: Path
    kind: str
    key: dict[str, str]
    base: Path
    identity: tuple[str, ...]
    stats: DatasetStats | None
    effective: tuple[tuple[StatsFileRow, int], ...] = ()
    straddlers: tuple[StatsFileRow, ...] = ()
    served: bool = False
    consult_timed_out: bool = False

    @property
    def should_seed(self) -> bool:
        """Whether the read side should rebuild the shard after the legacy scan.

        ``True`` only when the shard failed validation outright (absent /
        corrupt / stale) — a mid-request degrade keeps the existing shard (the
        next request revalidates it; the writers keep it fresh). A TIMED-OUT
        consult (DEC-DASHBOARD-COLD-IO-DEADLINE-001) suppresses the seed: the
        volume is WEDGED, so launching ``_seed_stats_manifest``'s three
        unbounded stat sweeps + grouped scan against it would re-create the
        very hang the deadline just bounded — and because no ``statseed|``
        latch entry is written, a genuinely-absent shard still seeds on the
        next HEALTHY click (recovery not poisoned; the suppression is
        timeout-scoped, never drift-scoped).
        """
        return self.stats is None and not self.consult_timed_out

    @property
    def total(self) -> int:
        """The manifest total for the request bounds — ``Σ`` in-bound counts.

        With no bounds every file is "inside" so this is exactly the shard's
        ``row_count`` (zero Parquet I/O); with bounds it is the hybrid
        stamped + ≤straddler-scanned sum, additive across files because no
        identity key spans two UTC-month files (validated per request by the
        month-bounds clause).
        """
        return sum(count for _row, count in self.effective)

    def ist_span(self) -> tuple[str | None, str | None]:
        """The dataset's stored-life IST-date span from the validated bounds.

        ``timestamp → IST date`` is monotonic, so the fold over per-file
        bounds equals the legacy ``MIN``/``MAX`` over :data:`_IST_DATE_EXPR`
        exactly. ``(None, None)`` when the shard did not validate (the caller
        then runs the legacy scan — kept as a guarded branch, not an assert,
        per the no-bare-assumptions discipline).
        """
        stats = self.stats
        if stats is None:
            return (None, None)
        return (
            stats.min_ts.astimezone(_IST).date().isoformat(),
            stats.max_ts.astimezone(_IST).date().isoformat(),
        )

    def plan_page(
        self, *, descending: bool, limit: int, offset: int
    ) -> tuple[list[StatsFileRow], int]:
        """Translate ``(direction, offset, limit)`` → (file sublist, rebased offset).

        Prefix-sums over the per-file in-bound counts: walk the files in the
        page's own direction (rel-ascending for ASC, reversed for DESC — the
        UTC-month partition law makes file order ≡ timestamp order, enforced
        STRUCTURALLY by the shard loader's clause-1 ASCII-month-grammar +
        strictly-increasing-chronology check, so no validating shard can
        plan a misordered page — endgame round 3), skip the
        files wholly before the window, stop at the first file wholly after
        it, and rebase the ``OFFSET`` to be local to the first selected file.
        The returned sublist is re-sorted rel-ASCENDING regardless of page
        direction (the keep-FIRST ``QUALIFY`` filename order — dedup semantics
        preserved). An out-of-range offset returns an empty sublist (the page
        is empty, exactly like the legacy query past the end).
        """
        walk = tuple(reversed(self.effective)) if descending else self.effective
        selected: list[StatsFileRow] = []
        rebased = 0
        cum = 0
        for row, count in walk:
            if count == 0:
                continue
            file_start = cum
            cum += count
            if cum <= offset:
                continue
            if file_start >= offset + limit:
                break
            if not selected:
                rebased = offset - file_start
            selected.append(row)
        return (sorted(selected, key=lambda r: r.rel), rebased)


def _stats_fingerprint(stats: DatasetStats) -> str:
    """Digest the validated file set's ``(rel, mtime_ns, size)`` triples. O(D).

    Folded into the star-projection memo key so a memoised union-column list
    can NEVER be served for a file state other than the exact one it was
    derived from (any file change → new fingerprint → new key → re-describe);
    rels are month-grammar paths (clause 4) so the ``:``/``;`` join is
    unambiguous.
    """
    token = ";".join(f"{row.rel}:{row.mtime_ns}:{row.size}" for row in stats.files)
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _stats_read_context(
    conn: Any,
    lake_root: Path,
    *,
    kind: str,
    key: dict[str, str],
    base: Path,
    identity: tuple[str, ...],
    where_sql: str,
    where_params: list[Any],
    ist_dates: bool,
    start: str | None,
    end: str | None,
    use_stats_manifest: bool,
) -> _StatsReadContext | None:
    """Build the per-request stats-manifest context, or ``None`` when off.

    ``None`` ⇒ the feature is disabled for this request (the caller left
    ``use_stats_manifest`` at its pure default, or the
    ``BRUTEX_LAKE_STATS_MANIFEST=off`` kill switch is set): no shard is read,
    no fallback is logged, no seed will run — byte-identical legacy behaviour.

    Otherwise the shard goes through the FULL anti-illusion validation
    (:func:`~brutex.data.lake_stats_manifest.load_valid_dataset_stats_guarded`
    — deadline-bounded per DEC-DASHBOARD-COLD-IO-DEADLINE-001: a
    wedged-volume timeout builds a ``consult_timed_out`` context whose
    ``should_seed`` is suppressed) and,
    when it holds, every file is classified against the bounds via the ONE
    shared :func:`_stats_file_verdict`; each straddler pays ONE bounded
    ``COUNT(DISTINCT …)`` over ONLY its own file (the identical WHERE the
    legacy count uses, bound through the same ``read_parquet(?)`` list seam).
    A straddler read failing (corrupt file) demotes the WHOLE request to
    legacy (``served=False``) — never a half-manifest number.
    """
    if not use_stats_manifest or not stats_manifest_enabled():
        return None
    consult = load_valid_dataset_stats_guarded(lake_root, kind=kind, key=key)
    stats = consult.stats
    ctx = _StatsReadContext(
        conn=conn,
        lake_root=lake_root,
        kind=kind,
        key=key,
        base=base,
        identity=identity,
        stats=stats,
        consult_timed_out=consult.timed_out,
    )
    if stats is None:
        return ctx
    start_d = _parse_iso_date(start, "start") if start is not None else None
    end_d = _parse_iso_date(end, "end") if end is not None else None
    effective: list[tuple[StatsFileRow, int]] = []
    straddlers: list[StatsFileRow] = []
    for row in stats.files:
        verdict = _stats_file_verdict(row, start_d, end_d, ist_dates=ist_dates)
        if verdict == _STATS_OUTSIDE:
            effective.append((row, 0))
            continue
        if verdict == _STATS_INSIDE:
            effective.append((row, row.rows))
            continue
        straddlers.append(row)
        sql = f"SELECT {distinct_count_sql(identity)} FROM read_parquet(?, union_by_name=true){where_sql}"  # noqa: S608  # nosec B608 - constant fragments over read_parquet(?) with a bound file list; values bound
        try:
            fetched = conn.execute(sql, [[str(base / row.rel)], *where_params]).fetchone()
        except movers_read_skip_errors() as exc:
            _LOG.info(
                _STATS_FALLBACK_EVENT,
                kind=kind,
                key=dict(key),
                clause="straddler_read",
                rel=row.rel,
                error=str(exc),
            )
            return ctx
        if fetched is None:
            _LOG.info(
                _STATS_FALLBACK_EVENT,
                kind=kind,
                key=dict(key),
                clause="straddler_read",
                rel=row.rel,
            )
            return ctx
        effective.append((row, int(fetched[0])))
    ctx.effective = tuple(effective)
    ctx.straddlers = tuple(straddlers)
    ctx.served = True
    return ctx


def _stats_star_columns(
    ctx: _StatsReadContext,
    *,
    count_cache: CandleCountCache | None,
    generation: int,
) -> list[str] | None:
    """Resolve a star-select page's union column ORDER for the pruned read.

    A ``SELECT *`` page's column set under ``union_by_name=true`` is the union
    of the schemas of the files ACTUALLY read — so a pruned sublist could
    silently present fewer/other columns than the legacy full-glob read on a
    schema-heterogeneous dataset (e.g. a partially Greeks-enriched F&O tree).
    The pruned path therefore resolves the FULL union order first (ONE
    ``DESCRIBE`` over the validated file list — an O(D)-footer read) and
    projects it EXPLICITLY, memoised behind the injected generation cache
    under a key that folds in the validated file-state fingerprint
    (:func:`_stats_fingerprint`) — a memoised order can never be served for a
    file state other than the one it described (no staleness envelope at
    all). ``None`` cache ⇒ the DESCRIBE runs inline (correct, the pruned-read win
    is smaller — the production routes always inject the cache). Returns
    ``None`` on a describe failure (the caller degrades to legacy).
    """
    stats = ctx.stats
    if stats is None:
        return None
    paths = [str(ctx.base / row.rel) for row in stats.files]
    sql = f"SELECT column_name FROM (DESCRIBE SELECT {DEDUP_STAR_SELECT_SQL} FROM {DEDUP_PARQUET_SOURCE_SQL})"  # noqa: S608  # nosec B608 - constant fragments over read_parquet(?) with a bound file list

    def _describe() -> tuple[str, ...]:
        rows = ctx.conn.execute(sql, [paths]).fetchall()
        return tuple(str(row[0]) for row in rows)

    key_token = "|".join(ctx.key[name] for name in sorted(ctx.key))
    memo_key = f"statcols|{ctx.kind}|{key_token}|{_stats_fingerprint(stats)}"
    try:
        if count_cache is None:
            return list(_describe())
        return list(count_cache.get_or_compute(memo_key, generation=generation, compute=_describe))
    except movers_read_skip_errors() as exc:
        _LOG.info(
            _STATS_FALLBACK_EVENT,
            kind=ctx.kind,
            key=dict(ctx.key),
            clause="star_columns",
            error=str(exc),
        )
        return None


def _stats_tokens_intact(ctx: _StatsReadContext, planned: Sequence[StatsFileRow]) -> bool:
    """Post-query TOCTOU re-stat over the request's touched files.

    Re-stats the pruned page sublist (*planned*) AND the straddler-counted
    files (their live bounded counts fed the manifest total, so a straddler
    replaced between its ``COUNT`` and the response must degrade too) via ONE
    deadline-bounded
    :func:`~brutex.data.lake_stats_manifest.bounded_token_sweep` pass
    (DEC-DASHBOARD-COLD-IO-DEADLINE-001 — same gate + key as the consult, so
    a mid-request wedge that latched the consult fails this fast with no
    second daemon). ANY moved token logs the ``stat_token_moved`` fallback
    clause (payload byte-preserved — the FIRST moved rel in today's exact
    iteration order); a wedged-volume timeout logs ``restat_deadline`` (NO
    ``rel`` key — never a fabricated field). Either flips ``ctx.served`` off
    (the whole-request degrade law) and returns ``False`` — the caller
    discards the pruned result and re-runs on the legacy path.
    """
    verdict, rel = bounded_token_sweep(ctx.lake_root, ctx.base, (*planned, *ctx.straddlers))
    if verdict == TOKEN_SWEEP_OK:
        return True
    if verdict == TOKEN_SWEEP_MOVED:
        _LOG.info(
            _STATS_FALLBACK_EVENT,
            kind=ctx.kind,
            key=dict(ctx.key),
            clause="stat_token_moved",
            rel=rel,
        )
    else:
        _LOG.info(
            _STATS_FALLBACK_EVENT,
            kind=ctx.kind,
            key=dict(ctx.key),
            clause="restat_deadline",
        )
    ctx.served = False
    return False


def _stats_pruned_page(
    ctx: _StatsReadContext | None,
    *,
    select: str,
    columns: tuple[str, ...] | None,
    where_sql: str,
    where_params: list[Any],
    qualify_sql: str,
    order_sql: str,
    descending: bool,
    limit: int,
    offset: int,
    count_cache: CandleCountCache | None,
    generation: int,
) -> tuple[list[str], list[tuple[Any, ...]]] | None:
    """Execute the page over ONLY the files it needs — or ``None`` ⇒ run legacy.

    The manifest page path (O(page-files) Parquet work — the plan walk and
    the request's revalidation are each O(D) in the dataset's month files,
    so NOT literal O(page) in partition depth): plan the minimal
    file sublist + rebased local ``OFFSET`` via :meth:`_StatsReadContext.plan_page`,
    then execute the IDENTICAL SQL shape the legacy page uses (same
    SELECT/WHERE/QUALIFY/ORDER/LIMIT — only the ``read_parquet(?)`` binding
    changes from the whole glob to the sublist). Exactness rests on the
    UTC-month partition law (no identity key spans two files), validated per
    request by the month-bounds clause.

    Fail-closed ladder — ANY of the following returns ``None`` and flips
    ``ctx.served`` off so the WHOLE request re-runs on the legacy path (a
    manifest total is never paired with a degraded page):

    * the star-projection resolve fails (:func:`_stats_star_columns`);
    * a resolved column name is un-quotable (contains ``"`` —
      :func:`_quote_ident`'s reject; an out-of-band parquet can carry such a
      name, and the legacy star path never quotes names, so it must degrade,
      never escape as a route-level 400 — review round 4);
    * the pruned execute raises an unreadable-file error;
    * the cursor's column set differs from the expected projection;
    * the post-query TOCTOU re-stat finds ANY touched file's stat token moved
      (the pruned sublist AND the straddler-counted files).

    An empty planned sublist (offset past the end / empty bounded range) skips
    the query and returns the expected columns with zero rows — byte-identical
    to the legacy query returning no rows. It still runs the SAME post-query
    re-stat over the straddler-counted files first (review round 7): their
    live counts fed the manifest total, so a straddler replaced between its
    bounded ``COUNT`` and this response degrades the whole request exactly
    like a non-empty page would.
    """
    if ctx is None or not ctx.served:
        return None
    if columns is not None:
        expected = list(columns)
        page_select = select
    else:
        star = _stats_star_columns(ctx, count_cache=count_cache, generation=generation)
        if star is None:
            ctx.served = False
            return None
        expected = star
        try:
            page_select = ", ".join(_quote_ident(column) for column in star)
        except ValueError as exc:
            # A physical column name carrying a double quote (an out-of-band
            # parquet — exactly the tamper class every other clause fails
            # closed on) previously escaped this ladder as a raw ValueError →
            # a persistent per-click HTTP 400, while the legacy path (star
            # ``SELECT * EXCLUDE`` — never quotes names) renders fine
            # (review round 4). Degrade the WHOLE request instead.
            _LOG.info(
                _STATS_FALLBACK_EVENT,
                kind=ctx.kind,
                key=dict(ctx.key),
                clause="star_columns",
                error=str(exc),
            )
            ctx.served = False
            return None
    planned, rebased = ctx.plan_page(descending=descending, limit=limit, offset=offset)
    if not planned:
        if not _stats_tokens_intact(ctx, planned):
            return None
        return (expected, [])
    paths = [str(ctx.base / row.rel) for row in planned]
    sql = f"SELECT {page_select} FROM {DEDUP_PARQUET_SOURCE_SQL}{where_sql}{qualify_sql}{order_sql} LIMIT ? OFFSET ?"  # noqa: S608  # nosec B608 - validated select + constant fragments; file list and values bound
    try:
        cursor = ctx.conn.execute(sql, [paths, *where_params, limit, rebased])
    except movers_read_skip_errors() as exc:
        _LOG.info(
            _STATS_FALLBACK_EVENT,
            kind=ctx.kind,
            key=dict(ctx.key),
            clause="page_read",
            error=str(exc),
        )
        ctx.served = False
        return None
    out_columns = [str(col[0]) for col in (cursor.description or [])]
    raw_rows = list(cursor.fetchall())
    if out_columns != expected:
        _LOG.info(
            _STATS_FALLBACK_EVENT,
            kind=ctx.kind,
            key=dict(ctx.key),
            clause="page_columns",
            expected=expected,
            got=out_columns,
        )
        ctx.served = False
        return None
    if not _stats_tokens_intact(ctx, planned):
        return None
    return (out_columns, raw_rows)


def _stats_utc_ts(value: Any) -> datetime | None:
    """Normalize a DuckDB ``MIN``/``MAX`` timestamp cell to an aware-UTC datetime.

    The lake stores UTC instants; DuckDB may hand them back tz-aware (a
    ``TIMESTAMPTZ`` column, session-zone offset) or naive (a plain
    ``TIMESTAMP`` — UTC wall time by the lake contract). Anything that is not
    a datetime at all (an empty/NULL aggregate) returns ``None`` — the seed
    aborts rather than guesses.
    """
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _seed_stats_manifest(
    conn: Any,
    lake_root: Path,
    *,
    kind: str,
    key: dict[str, str],
    base: Path,
    identity: tuple[str, ...],
) -> bool:
    """Write-behind rebuild of a dataset's stats shard after a legacy scan.

    The ONLY seeding mechanism (lazy accretion — the writers never create a
    shard): called best-effort after a request whose shard failed validation,
    once the legacy scan has already answered the request. ONE grouped
    ``COUNT(DISTINCT key)`` + ``MIN``/``MAX`` per file over the SAME file set
    (≈ the cost of the count the request just paid); every file's stat token
    is captured BEFORE the scan and re-checked AFTER — ANY movement, stray
    non-month file, unreadable file, empty file, or unexpected result shape
    aborts the seed (logged ``db.stats_manifest.seed_skipped``; the dataset
    simply stays legacy-served — never a wrong shard). Never raises into the
    request — INCLUDING a :class:`~brutex.data.errors.LakeWriteHalt` from the
    shard-lock ``.locks/`` creation on a full disk: this runs on the /db READ
    request thread, where there is no pull to halt and the viewer's pinned
    contract is "never a 500", so the halt is caught here, logged
    (``db.stats_manifest.seed_skipped`` with the halt's reason), and the seed
    is simply skipped (the WRITE hooks' LakeWriteHalt propagation is
    untouched — a full disk still halts the pull). The seeded rows carry
    ``raw_hash=None`` (forensics-only field — hashing every partition would
    defeat the point; the next writer upsert restamps its file's hash).

    Returns
    -------
    bool
        ``True`` iff the rebuilt shard was written.
    """
    try:
        rels = sorted(scan_dataset_rels(lake_root, kind, key))
    except OSError as exc:
        _LOG.info(
            _STATS_SEED_SKIPPED_EVENT, kind=kind, key=key, reason="scan_failed", error=str(exc)
        )
        return False
    if not rels:
        _LOG.info(_STATS_SEED_SKIPPED_EVENT, kind=kind, key=key, reason="no_files")
        return False
    for rel in rels:
        if parse_rel_month(kind, rel) is None:
            _LOG.info(
                _STATS_SEED_SKIPPED_EVENT, kind=kind, key=key, reason="non_month_file", rel=rel
            )
            return False
    pre: dict[str, os.stat_result] = {}
    for rel in rels:
        try:
            pre[rel] = os.stat(base / rel)
        except OSError as exc:
            _LOG.info(
                _STATS_SEED_SKIPPED_EVENT,
                kind=kind,
                key=key,
                reason="stat_failed",
                rel=rel,
                error=str(exc),
            )
            return False
    paths = [str(base / rel) for rel in rels]
    sql = f'SELECT filename, {distinct_count_sql(identity)}, MIN("timestamp"), MAX("timestamp") FROM read_parquet(?, union_by_name=true, filename=true) GROUP BY filename'  # noqa: S608  # nosec B608 - constant fragments over read_parquet(?) with a bound file list
    try:
        grouped = conn.execute(sql, [paths]).fetchall()
    except movers_read_skip_errors() as exc:
        _LOG.info(
            _STATS_SEED_SKIPPED_EVENT, kind=kind, key=key, reason="scan_failed", error=str(exc)
        )
        return False
    rows: list[StatsFileRow] = []
    seen: set[str] = set()
    for filename, count, raw_min, raw_max in grouped:
        rel = os.path.relpath(str(filename), str(base)).replace(os.sep, "/")
        if rel not in pre or rel in seen:
            _LOG.info(
                _STATS_SEED_SKIPPED_EVENT, kind=kind, key=key, reason="unexpected_file", rel=rel
            )
            return False
        seen.add(rel)
        min_ts = _stats_utc_ts(raw_min)
        max_ts = _stats_utc_ts(raw_max)
        if min_ts is None or max_ts is None:
            _LOG.info(_STATS_SEED_SKIPPED_EVENT, kind=kind, key=key, reason="no_bounds", rel=rel)
            return False
        rows.append(
            StatsFileRow(
                rel=rel,
                rows=int(count),
                min_ts=min_ts,
                max_ts=max_ts,
                mtime_ns=pre[rel].st_mtime_ns,
                size=pre[rel].st_size,
                raw_hash=None,
            )
        )
    if seen != set(rels):
        # A rel with NO group row either held ZERO identity rows (an
        # out-of-band 0-row parquet — the production writers never render an
        # empty partition) or vanished/moved mid-scan. Distinguish them by
        # re-statting the missing rels against the pre-scan tokens: all
        # present + unmoved ⇒ the PERMANENT ``empty_file`` condition (the
        # dataset stays legacy-served until the file is removed), anything
        # moved ⇒ the transient ``file_set_changed``.
        missing = sorted(set(rels) - seen)

        def _rel_unmoved(rel: str) -> bool:
            try:
                return stat_token(os.stat(base / rel)) == stat_token(pre[rel])
            except OSError:
                return False

        unmoved = all(_rel_unmoved(rel) for rel in missing)
        # Capped enumeration (review round 2 — the same log-volume guard the
        # leaf module's file_set fallback event carries): frequency is bounded
        # by the per-(dataset, generation) seed latch, but a pathological
        # dataset with thousands of zero-row/vanished files must still never
        # inflate one structured log line to ~100 KB; the honest TRUE total
        # rides ``missing_count``.
        _LOG.info(
            _STATS_SEED_SKIPPED_EVENT,
            kind=kind,
            key=key,
            reason="empty_file" if unmoved else "file_set_changed",
            missing=missing[:FALLBACK_REL_LOG_CAP],
            missing_count=len(missing),
        )
        return False
    for rel in rels:
        try:
            post = os.stat(base / rel)
        except OSError as exc:
            _LOG.info(
                _STATS_SEED_SKIPPED_EVENT,
                kind=kind,
                key=key,
                reason="post_stat_failed",
                rel=rel,
                error=str(exc),
            )
            return False
        if post.st_mtime_ns != pre[rel].st_mtime_ns or post.st_size != pre[rel].st_size:
            _LOG.info(_STATS_SEED_SKIPPED_EVENT, kind=kind, key=key, reason="file_moved", rel=rel)
            return False
    try:
        return seed_dataset_stats(lake_root, kind=kind, key=key, files=rows)
    except LakeWriteHalt as exc:
        # ``seed_dataset_stats`` deliberately lets a LakeWriteHalt from the
        # shard-lock ``.locks/`` creation PROPAGATE (correct for the WRITE
        # hooks — a full disk must halt the pull). This caller runs on the
        # /db READ request thread where there is NO pull to halt and the
        # routes catch only ValueError, so an uncaught halt would 500 a
        # read-only paging click (pre-manifest, /db reads performed zero lake
        # writes — this class could never surface). Skip the write-behind
        # seed instead: the dataset simply stays legacy-served.
        _LOG.warning(_STATS_SEED_SKIPPED_EVENT, kind=kind, key=key, reason=exc.reason)
        return False


def _stats_seed_key(kind: str, key: Mapping[str, str]) -> str:
    """Build the per-dataset cache key for the seed-attempt latch. O(1).

    Keyed on the dataset coordinates ONLY — never bounds / paging (the seed is
    bounds-invariant, so one latch covers every click on the dataset); the
    ``statseed|`` prefix keeps it disjoint from the count / span /
    star-projection keys sharing the SAME injected cache.
    """
    token = "|".join(key[name] for name in sorted(key))
    return f"statseed|{kind}|{token}"


def _seed_stats_once(
    conn: Any,
    lake_root: Path,
    *,
    kind: str,
    key: dict[str, str],
    base: Path,
    identity: tuple[str, ...],
    count_cache: CandleCountCache | None,
    generation: int,
) -> bool:
    """Run the write-behind seed behind a FAILED-attempt latch per generation.

    The raw seed (:func:`_seed_stats_manifest`) pays one full O(dataset-rows)
    grouped scan. For a dataset whose seed is REJECTED every time — a
    foreign-month-poisoned partition awaiting ``lake_heal`` (the G-RACE-1
    class), an out-of-band 0-row parquet, a corrupt file — an unlatched caller
    would repay that doomed scan on EVERY paging click: a permanent ~2×
    per-click regression on exactly the datasets the manifest honestly
    disables itself for, contradicting the "pays today's price ONCE" law. So
    the production path (injected *count_cache*) memoises a small MUTABLE
    latch per ``(dataset, generation)``: the FIRST failed attempt flips it and
    every later click within the generation skips the seed outright (one
    ``db.stats_manifest.seed_skipped`` ``reason="attempt_blocked"`` DEBUG
    event — the rejection itself was already logged loudly by that first
    attempt). A SUCCESSFUL seed never blocks, so an out-of-band invalidation
    within the SAME generation still falls back and reseeds (the
    fallback→reseed healing contract, integration-pinned); the generation
    bump after any pull/heal re-enables a blocked dataset (a healed partition
    is re-attempted naturally). Best-effort envelope: an LRU eviction of the
    latch merely re-attempts once — never a wrong number, only a scan.
    ``count_cache=None`` (the pure default) attempts every time —
    behaviour-identical to before (the purity contract; the production /db
    routes always inject the cache).
    """

    def _attempt() -> bool:
        return _seed_stats_manifest(
            conn, lake_root, kind=kind, key=key, base=base, identity=identity
        )

    if count_cache is None:
        return _attempt()
    latch: dict[str, bool] = count_cache.get_or_compute(
        _stats_seed_key(kind, key), generation=generation, compute=lambda: {"blocked": False}
    )
    if latch.get("blocked"):
        _LOG.debug(_STATS_SEED_SKIPPED_EVENT, kind=kind, key=key, reason="attempt_blocked")
        return False
    seeded = _attempt()
    if not seeded:
        latch["blocked"] = True
    return seeded


class CandleCountCache(Protocol):
    """The minimal generation-stamped cache contract the candle readers need.

    Structural (``Protocol``) so :mod:`brutex.dashboard.db_view` never hard-imports
    a concrete cache class — the dashboard injects the COMMON generation-stamped LRU
    (:class:`brutex.dashboard.movers_cache.MoversSnapshotCache`, reused as the generic
    cache per CLAUDE.md §11, DEC-DB-CANDLE-COUNT-CACHE-001) which satisfies this.
    Generic over the memoised value so the ONE injected instance serves both the
    paging-invariant page ``total`` (an ``int``) and the paging-invariant F&O
    stored-life span (a ``(start, end)`` tuple — the DEC-DB-UX-PACK-001 applied-range
    chip, memoised under :func:`candle_span_key` / :func:`option_candle_span_key` so
    a prev/next paging click never re-runs the MIN/MAX partition scan).
    A ``compute`` that raises propagates cleanly and nothing is cached.
    """

    def get_or_compute(self, key: str, *, generation: int, compute: Callable[[], _T]) -> _T:
        """Return *key*'s cached value for *generation*, running *compute* on a miss."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed


def candle_count_key(
    exchange: str, segment: str, symbol: str, interval: str, start: str | None, end: str | None
) -> str:
    """Build the stable cache key for a candle-page **total** (DEC-DB-CANDLE-COUNT-CACHE-001).

    The total ``COUNT(*)`` depends ONLY on the partition coordinates + the inclusive
    date bounds — NOT on ``limit`` / ``offset`` / ``descending`` / the projected
    ``columns`` (paging, sort, and column subsetting never change how many rows match).
    So every page of the same ``(coords, start, end)`` shares ONE cached total: a
    prev/next click is an O(1) hit instead of re-reading every Parquet footer. ``|``
    cannot appear in a validated coordinate or an ISO date, so the join is
    unambiguous; ``None`` bounds render as an empty segment (``start=None`` keys
    identically to an explicit open lower bound).
    """
    return f"{exchange}|{segment}|{symbol}|{interval}|{start or ''}|{end or ''}"


def candle_span_key(exchange: str, segment: str, symbol: str, interval: str) -> str:
    """Build the stable cache key for an F&O partition's STORED-LIFE span.

    The span (earliest → latest stored bar IST date — the DEC-DB-UX-PACK-001
    Feature 2 applied-range default) is a property of the PARTITION alone: it is
    invariant to the date bounds AND to ``limit`` / ``offset`` / ``descending`` /
    ``columns``, so every default-bounded page of the same coordinates shares ONE
    cached span — a prev/next paging click is an O(1) hit instead of re-running
    the ``MIN``/``MAX`` scan per click (the DEC-LAKE-READ-O1-AUDIT-001 / Golden
    Rule #5 paging discipline; the hostile-review MAJOR on this pack). The
    ``span|`` prefix namespaces the key away from every other key sharing the
    injected cache instance: a spot count key opens with a validated exchange
    token (``NSE``/``BSE``, never ``span``), an option count key with ``opt|``,
    an option span key with ``optspan|`` — collision-free by construction.
    """
    return f"span|{exchange}|{segment}|{symbol}|{interval}"


def option_candle_span_key(series: str) -> str:
    """Build the stable cache key for an expired-option series' STORED-LIFE span.

    The expired-options twin of :func:`candle_span_key`: the series' full stored
    life is invariant to bounds / paging / sort / columns, so one cached span
    serves every default-bounded page of the series. The ``optspan|`` prefix is
    collision-free against ``opt|`` count keys, ``span|`` F&O span keys, and
    exchange-opening spot count keys (see :func:`candle_span_key`).
    """
    return f"optspan|{series}"


def _degraded_total_key(count_key: str) -> str:
    """Cache key for the degrade-law LEGACY recount of a page total. O(1).

    DELIBERATELY disjoint from the plain count key (the ``legacytotal|``
    prefix — a plain spot key opens with a validated exchange token, an
    options key with ``opt|``): under the whole-request degrade law the plain
    key may hold a MANIFEST-served total, which must never be paired with a
    degraded page, so the degraded recount memoises under its OWN key. The
    degraded legacy total is paging-invariant exactly like the plain total
    (same ``(coords, start, end)`` → the same resilient ``COUNT``), so a
    dataset whose pruned page fails PERSISTENTLY — e.g. a schema-heterogeneous
    star union whose sublist cannot bind the full-union projection — pays the
    O(files) legacy recount ONCE per generation instead of on every paging
    click (review round 2: the "pays today's price ONCE" law extended to the
    degraded corner; a pull/heal bumps the generation → recount, never stale).
    """
    return f"legacytotal|{count_key}"


def query_candles(
    conn: Any,
    lake_root: Path,
    *,
    exchange: str,
    segment: str,
    symbol: str,
    interval: str,
    start: str | None = None,
    end: str | None = None,
    columns: tuple[str, ...] | None = None,
    descending: bool = False,
    limit: int,
    offset: int = 0,
    count_cache: CandleCountCache | None = None,
    generation: int = 0,
    registry_path: Path | None = None,
    stock_drift_loader: Callable[[], StockDriftMap | None] | None = None,
    use_stats_manifest: bool = False,
    retry_sleep: Callable[[float], None] | None = None,
) -> dict[str, Any]:
    """Read one page of OHLCV candles for a partition set (DEC-DB-QUERY-001).

    Reads the Parquet lake directly via DuckDB ``read_parquet`` over the
    partition glob ``<lake>/bars/<exch>/<seg>/<symbol>/<interval>/**/*.parquet``
    — no data duplication, the lake stays the single source of truth.
    The glob is scoped to ONE ``(symbol, interval)`` (its whole-year/month
    files only), and the page is ``LIMIT``-bounded — so a click never
    materialises more than ``limit`` rows in PYTHON regardless of how many
    years are stored. *start* / *end* are inclusive ISO ``YYYY-MM-DD`` bounds
    matched against the bar's UTC calendar date (IST for the derived ``1day``
    — see :func:`_date_bounds`). Returns an empty page (never raises) when
    the partition set has no Parquet files yet.

    HONEST complexity (Golden Rule #10 — this is NOT O(page) in partition
    depth; DEC-DB-OPAGE-HONESTY-001, folding the DEC-LATENCY-HONESTY-BATCH-001
    correction of the prior "DuckDB additionally skips row-groups via min/max
    stats" claim, which this query shape DEFEATS): the page DATA query
    performs a FULL columnar scan of every matched partition row before it
    slices the page. The read-side keep-FIRST dedup ``QUALIFY row_number()
    OVER (PARTITION BY timestamp …) = 1`` window (DEC-LAKE-READ-UNIQUE-001)
    hash-partitions the whole matched set, the ``ORDER BY "timestamp"`` sorts
    it, and the date bounds compare a derived ``(timestamp AT TIME ZONE
    …)::DATE`` expression that DuckDB does not prune through against the raw
    ``timestamp`` column's row-group min/max stats — and only THEN do
    ``LIMIT``/``OFFSET`` take the slice. So the DuckDB engine cost is
    **O(total matched partition rows)** (plus one Parquet footer open per
    file), NOT O(page): ``LIMIT`` bounds ONLY how many rows materialise in
    Python — the FIRST page is not cheaper than the whole scan — and a deep
    ``OFFSET`` is O(row-groups up to the offset) on top. The page ``total``
    is a ``COUNT(DISTINCT timestamp)``, itself **O(rows)** on the first
    computation per write-generation and O(1) on a cached repeat (see
    *count_cache*). That scan is flat enough to pass the shared-CI < 8×
    depth-ratio gate (``tests/bench/test_db_candle_query_o1.py``); genuine
    row-group pruning would need stats-compatible raw-column predicates.
    This envelope is the LEGACY arm's: the write-time per-file STATS
    manifest — the #867/#1025 named future work, since SHIPPED by
    DEC-LAKE-STATS-MANIFEST-001 (MIGRATION.md #1614) and wired into THIS
    function via *use_stats_manifest* — prunes a validated page's read to
    O(page-files) Parquet work at an O(D) per-request revalidation stat
    sweep (D = the dataset's month files), NOT literal O(page). This
    mirrors the
    :func:`brutex.data.lake_read.dedup_qualify_sql` /
    :func:`~brutex.data.lake_read.dedup_qualify_latest_sql` honest envelope;
    a prev/next paging click therefore RE-SCANS the partition — flat only in
    that the count + F&O stored-life span are cached, never in the row-scan
    term.

    Parameters
    ----------
    conn
        A DuckDB connection.
    lake_root
        Top-level lake directory (parent of ``bars/``).
    exchange, segment, symbol, interval
        Partition coordinates; validated against the partition allowlists.
    start, end
        Inclusive ISO ``YYYY-MM-DD`` bounds on the bar timestamp, or ``None``.
    columns
        OHLCV columns to project (subset of :data:`CANDLE_COLUMNS`; an F&O page
        may additionally name the stored Greeks overlay columns
        :data:`FNO_GREEKS_COLUMNS` — DEC-GREEKS-ENGINE-PR3-001), or ``None``
        for all. An empty tuple is rejected.
    descending, limit, offset
        Sort direction on ``timestamp`` + page bounds (clamped).
    count_cache, generation
        Optional generation-stamped cache for the page ``total``
        (DEC-DB-CANDLE-COUNT-CACHE-001) + the current write-generation it is keyed
        by. When supplied, the ``COUNT(DISTINCT timestamp)`` total — O(rows) on the
        first computation per write-generation, O(1) on a cached repeat — is
        memoised per ``(coords, start, end)`` so a prev/next paging click within one
        dataset is an O(1) hit; a pull bumps *generation* → recompute (never a stale
        total).
        The SAME cache also memoises the F&O default-range STORED-LIFE span under
        :func:`candle_span_key` (coords-only — the span is bound/paging-invariant),
        so the applied-range chip (DEC-DB-UX-PACK-001 Feature 2) never re-runs its
        MIN/MAX scan on a paging click. ``None`` (the default) keeps the function
        pure — the COUNT and the span run inline, behaviour-identical to before.
    registry_path
        Optional ``registry.duckdb`` path (DEC-MONEYNESS-REGISTRY-STEP-001): an F&O
        option page then derives its ATM±N strike-grid step from the AUTHORITATIVE
        vendor chain (memoised behind the SAME *count_cache* + *generation* under
        :func:`fno_strike_chain_key`) instead of the sparse lake-observed sibling
        ladder; an absent/empty/unreadable registry — or the ``None`` default —
        degrades gracefully to the observed ladder, honestly labelled via the
        ``strike_step_confidence`` payload field.
    use_stats_manifest
        Opt into the per-file lake STATS manifest (DEC-LAKE-STATS-MANIFEST-001):
        the page ``total`` + the default-range span are answered from the
        dataset's VALIDATED stats shard and the page query's file list is
        PRUNED to only the files the page needs — O(page-files) Parquet
        work plus an O(D) per-request revalidation stat sweep (D = the
        dataset's month files), NOT literal O(page). Served ONLY after
        full per-request revalidation (parse + key
        echo + file-set + per-file stat token + month bounds); ANY failure —
        including a mid-request pruned-read error or a moved stat token —
        re-runs the WHOLE request on the byte-identical legacy path (one
        ``lake.stats_manifest.fallback`` log) and reseeds the shard
        best-effort. The production ``/db`` routes pass ``True``; the pure
        default ``False`` keeps every direct caller byte-identical to the
        legacy query pattern, and the ``BRUTEX_LAKE_STATS_MANIFEST=off`` kill
        switch forces legacy even when ``True``.
    retry_sleep
        Injectable sleeper for the torn-read retry-once guard
        (DEC-DB-CANDLES-TORN-READ-RETRY-001, the DEC-WINDOW-TORN-READ-RETRY-001
        idiom): a live pull's atomic ``os.replace`` landing between/inside
        this request's independent ``read_parquet`` opens (COUNT + page
        SELECT + ``fetchall``) surfaces as a transient ``duckdb.Error`` — each
        read is retried ONCE after :data:`_DUCKDB_RETRY_DELAY_S` over a
        freshly-rebuilt glob/file list (see :func:`_retry_candle_read_once`).
        ``None`` (the default) sleeps for real. HONEST envelope: retry-once
        narrows the race, it does not eliminate it — a second replace inside
        the retry window still raises; and a CACHED total served beside a
        LIVE page read can lag a concurrent writer within one generation
        (inherent to the DEC-DB-CANDLE-COUNT-CACHE-001 generation-keyed
        design) — when the page returned rows, the returned ``total`` is
        therefore raised to at least ``offset + len(rows)`` (an OBSERVED
        lower bound); an EMPTY page never adjusts the total (it proves
        nothing — ``offset + 0`` is a bound only for k > 0 rows).

    Returns
    -------
    dict[str, Any]
        ``{"symbol", "interval", "columns", "rows", "total", "limit",
        "offset"}`` with every cell JSON-safe. An F&O page additionally carries
        ``"strike_step_confidence"`` (``"registry"`` / ``"observed"`` / ``None``) —
        the honesty label of the ATM±N strike-grid step source.

    Raises
    ------
    ValueError
        On an invalid coordinate, an unknown column, an empty column tuple,
        or a malformed ISO date bound.
    """
    _validate_candle_coords(exchange, segment, symbol, interval)
    # An F&O page may ALSO project the 10 stored per-bar Greeks overlay columns
    # (DEC-GREEKS-ENGINE-PR3-001) — the Phase-2b enrichment writes them only
    # under segment="FNO", so a spot page still rejects them as unknown.
    valid_columns = frozenset(CANDLE_COLUMNS)
    if segment == FNO_CANDLE_LAKE_SEGMENT:
        valid_columns |= frozenset(FNO_GREEKS_COLUMNS)
    select = _select_list(columns, valid_columns)
    safe_limit = clamp_limit(limit)
    safe_offset = clamp_offset(offset)

    # Compare the bar's calendar date via the shared `_date_bounds`, which pins the
    # comparison to the IST date for the DERIVED 1day interval (so the filter matches
    # the date the grid renders — DEC-DB-UX-PACK-001 Feature 3) and to the UTC date
    # for every intraday interval (byte-identical to before, session-tz independent).
    # Both bounds inclusive (Golden Rule #7). The raw (possibly `None`) `start`/`end`
    # are what filter the rows + key the paging-invariant count cache — the honest
    # applied-range chip metadata is resolved SEPARATELY below (never fed to the WHERE),
    # so the row set + total stay byte-unchanged.
    is_fno = segment == FNO_CANDLE_LAKE_SEGMENT
    where_clauses, where_params = _date_bounds(start, end, interval=interval)
    where_sql = (" WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    # Empty / not-yet-pulled partition set → return an empty page instead of
    # letting read_parquet raise "No files found". The resilient candle reads below
    # rebuild the glob from *base* internally (DEC-DB-CORRUPT-SKIP-001).
    base = lake_root / "bars" / exchange / segment / symbol / interval
    if not (base.is_dir() and any(base.rglob("*.parquet"))):
        empty_cols = list(columns) if columns is not None else list(CANDLE_COLUMNS)
        # An F&O page ALWAYS carries the strike-step honesty field + the spot-join
        # provenance so the frontend's key set is stable across an empty vs populated
        # partition; an empty page presents no step and joins nothing, so both are an
        # honest None (DEC-MONEYNESS-REGISTRY-STEP-001; DEC-DB-FNO-DISPLAY-TRUTH-001
        # item 9).
        empty_step_confidence = (
            {"strike_step_confidence": None, "spot_join_interval": None} if is_fno else {}
        )
        return {
            **empty_step_confidence,
            "symbol": symbol,
            "interval": interval,
            # The two derived %-change columns (DEC-DB-CANDLE-DERIVED-001) are
            # ALWAYS appended so the viewer's column set is stable across an
            # empty vs populated partition; an F&O partition additionally carries the
            # derived moneyness pair (DEC-FNO-MONEYNESS-001) — same column set whether
            # the partition is empty or populated.
            "columns": [*empty_cols, *SPOT_DERIVED_COLUMNS, *_fno_derived_columns(segment)],
            "rows": [],
            "total": 0,
            "limit": safe_limit,
            "offset": safe_offset,
            # An empty partition has no stored span, so F&O defaults to (None, None)
            # — an honest "no stored data"; SPOT still reports [2020-01-01, today]
            # (DEC-DB-UX-PACK-001 Feature 2).
            "applied_range": resolve_applied_range(
                is_fno=is_fno,
                explicit_start=start,
                explicit_end=end,
                stored_start_ist=None,
                stored_end_ist=None,
                today_ist=_today_ist_iso(),
            ),
        }

    # The glob is built from coordinates validated above (no free user text),
    # so interpolating it is safe; the source is given to read_parquet as a
    # bound parameter and every date/page value is parameter-bound. Every read
    # below routes through the resilient probe-and-skip helpers
    # (DEC-DB-CORRUPT-SKIP-001): a single corrupt / version-incompatible Parquet file
    # in this partition no longer 500s the WHOLE page on every paging click — it is
    # logged (``db.parquet_skip``), counted out, and the page degrades to its readable
    # subset (never a fabricated bar).
    source = "read_parquet(?, union_by_name=true)"
    # DETERMINISTIC page order (DEC-LAKE-STATS-MANIFEST-001): the physical
    # ``filename, file_row_number`` tiebreaker (both live in the dedup scan
    # source, never in the projection) pins equal-timestamp rows to their
    # keep-FIRST physical order. Spot bars are timestamp-unique after the
    # QUALIFY dedup, so this is a no-op here — it exists so the ONE shared
    # order shape is total-ordered on the options pager too (where CE+PE share
    # a minute and the prior bare ``ORDER BY timestamp`` tie order was
    # engine-whim — plan-dependent, so a pruned file-list read could legally
    # reorder ties vs the full-glob read; Golden Rule #7).
    order_sql = (
        ' ORDER BY "timestamp" ' + ("DESC" if descending else "ASC") + ", filename, file_row_number"
    )
    skip_source = f"candle:{exchange}/{segment}/{symbol}/{interval}"
    # Read-side dedup gate (DEC-LAKE-READ-UNIQUE-001): a partition poisoned
    # before the write-side uniqueness fix can physically carry duplicate
    # timestamps. The PAGE query collapses them keep-FIRST in the same single
    # scan (QUALIFY over filename + file_row_number order — deterministic), and
    # the TOTAL counts DISTINCT timestamps so it equals what the deduped pages
    # actually serve. A clean partition is byte-identical. HONEST complexity
    # (DEC-DB-OPAGE-HONESTY-001, see the docstring): this QUALIFY window +
    # ``ORDER BY`` is an O(total matched rows) columnar scan per click — the
    # LIMIT/OFFSET below bound only the Python materialisation, NOT the scan.
    # The QUALIFY is NOT skipped on a "clean" partition: no reliable O(1) /
    # footer-only clean signal exists (the only detector is COUNT(*) ==
    # COUNT(DISTINCT ts), itself an O(rows) scan), and skipping the window
    # would be a constant-factor win, not a complexity change — so the dedup
    # correctness guard stays on (deferred follow-up, never a correctness risk
    # for a perf win).
    page_source = DEDUP_PARQUET_SOURCE_SQL
    qualify_sql = dedup_qualify_sql(("timestamp",))

    # A requested Greeks overlay column may be physically ABSENT from a legacy
    # (pre-Greeks) partition tree — substitute a typed NULL projection so the
    # page renders honest blank cells instead of a binder-error 500
    # (DEC-GREEKS-ENGINE-PR3-001; see _greeks_tolerant_select).
    if columns is not None and any(c in FNO_GREEKS_COLUMN_TYPES for c in columns):
        select = _greeks_tolerant_select(conn, base, columns, source=skip_source)

    # Per-request stats-manifest context (DEC-LAKE-STATS-MANIFEST-001): ``None``
    # when the request did not opt in / the kill switch is off (byte-identical
    # legacy — not even a shard read). Built AFTER the empty-partition early
    # return above (an empty dataset has no shard by contract) and BEFORE the
    # total, so the count closure and the page pruner share ONE validation +
    # ONE set of straddler counts (total ≡ Σ pages by construction).
    stats_ctx = _stats_read_context(
        conn,
        lake_root,
        kind=KIND_BARS,
        key={"exchange": exchange, "segment": segment, "symbol": symbol, "interval": interval},
        base=base,
        identity=("timestamp",),
        where_sql=where_sql,
        where_params=list(where_params),
        ist_dates=interval == _DAILY_INTERVAL,
        start=start,
        end=end,
        use_stats_manifest=use_stats_manifest,
    )

    # The page total is a `COUNT(DISTINCT timestamp)` over `read_parquet(glob)` —
    # the deduped row count the keep-FIRST pages serve, NOT a footer `COUNT(*)`. So
    # it is O(rows), NOT O(1): DuckDB scans the whole matched row set to count the
    # distinct timestamps, so on a deep multi-year partition the first computation
    # per write-generation grows with the stored rows on EVERY uncached paging
    # click. The total is
    # invariant across paging / sort / column subsetting of the same
    # (coords, start, end), so the dashboard injects a generation-stamped cache
    # (DEC-DB-CANDLE-COUNT-CACHE-001): a prev/next click within one dataset is an
    # O(1) hit, and a pull/materialize bumps the generation → recompute (the SAME
    # write-invalidation signal the movers cache uses — never a stale total). When
    # no cache is injected (the pure-function default), the COUNT runs inline,
    # behaviour-identical to before. A VALIDATED stats manifest answers the
    # closure from the stamped per-file counts instead (zero footer I/O beyond
    # the ≤few straddler files) — same number, revalidated THIS request.
    def _legacy_count_total() -> int:
        # The COUNT execute+fetch rides the torn-read retry-once guard
        # (DEC-DB-CANDLES-TORN-READ-RETRY-001): the resilient helper rebuilds
        # its glob from `base` per attempt, so the retry re-globs fresh.
        def _run_count() -> list[tuple[Any, ...]]:
            return _execute_parquet_resilient(
                conn,
                base,
                f"SELECT {distinct_count_sql(('timestamp',))} FROM {source}{where_sql}",  # noqa: S608  # nosec B608 - source is read_parquet(?) with a bound path; values bound
                list(where_params),
                source=skip_source,
            )

        rows = _retry_candle_read_once(_run_count, source=skip_source, retry_sleep=retry_sleep)
        return int(rows[0][0]) if rows and rows[0][0] is not None else 0

    def _count_total() -> int:
        if stats_ctx is not None and stats_ctx.served:
            return stats_ctx.total
        return _legacy_count_total()

    if count_cache is None:
        total = _count_total()
    else:
        total = count_cache.get_or_compute(
            candle_count_key(exchange, segment, symbol, interval, start, end),
            generation=generation,
            compute=_count_total,
        )
    # Fetch ONCE: the rendered rows AND the derived %-change columns are both
    # built from the same page (DEC-DB-CANDLE-DERIVED-001).
    # A `*` projection must exclude the two synthetic dedup-ordering columns so
    # the page schema stays byte-identical to the plain read (Golden Rule #7).
    page_select = DEDUP_STAR_SELECT_SQL if select == "*" else select
    # Whether THIS request's total may have been manifest-served (captured
    # BEFORE the page runs — the pruner flips ``served`` off on a mid-request
    # failure, and the degrade law below needs the pre-page verdict).
    manifest_total_candidate = stats_ctx is not None and stats_ctx.served

    # Both page forms ride the torn-read retry-once guard
    # (DEC-DB-CANDLES-TORN-READ-RETRY-001): the pruned form's own fail-closed
    # ladder catches execute-time unreadable-file errors, but its ``fetchall``
    # streaming read sits outside that ladder — and the legacy form's
    # ``fetchall`` + corrupt-skip degrade re-run were likewise unguarded. Each
    # attempt re-plans / re-globs from the partition directory.
    def _run_pruned_page() -> tuple[list[str], list[tuple[Any, ...]]] | None:
        return _stats_pruned_page(
            stats_ctx,
            select=select,
            columns=columns,
            where_sql=where_sql,
            where_params=list(where_params),
            qualify_sql=qualify_sql,
            order_sql=order_sql,
            descending=descending,
            limit=safe_limit,
            offset=safe_offset,
            count_cache=count_cache,
            generation=generation,
        )

    def _run_legacy_page() -> tuple[list[str], list[tuple[Any, ...]]]:
        return _execute_candle_page_resilient(
            conn,
            base,
            f"SELECT {page_select} FROM {page_source}{where_sql}{qualify_sql}{order_sql} LIMIT ? OFFSET ?",  # noqa: S608  # nosec B608 - validated select + bound path + bound values
            [*where_params, safe_limit, safe_offset],
            source=skip_source,
        )

    pruned = _retry_candle_read_once(_run_pruned_page, source=skip_source, retry_sleep=retry_sleep)
    if pruned is not None:
        out_columns, raw_rows = pruned
    else:
        out_columns, raw_rows = _retry_candle_read_once(
            _run_legacy_page, source=skip_source, retry_sleep=retry_sleep
        )
        if manifest_total_candidate:
            # Whole-request degrade law (DEC-LAKE-STATS-MANIFEST-001): the
            # manifest validated but its pruned page path failed mid-request —
            # recompute the total on the SAME legacy resilient path the page
            # just ran, so a manifest total is never paired with a degraded
            # page (the count cache keeps its normal entry; it was valid when
            # computed and the generation bump remains its invalidation). The
            # DEGRADED recount memoises under its own disjoint key so a
            # PERSISTENTLY unprunable page never re-pays the O(files) legacy
            # COUNT per click (review round 2 — see _degraded_total_key).
            if count_cache is None:
                total = _legacy_count_total()
            else:
                total = count_cache.get_or_compute(
                    _degraded_total_key(
                        candle_count_key(exchange, segment, symbol, interval, start, end)
                    ),
                    generation=generation,
                    compute=_legacy_count_total,
                )
    # Honest observed lower bound (DEC-DB-CANDLES-TORN-READ-RETRY-001): the
    # generation-keyed CACHED total can lag a LIVE page read within one
    # generation (a concurrent writer between the COUNT and the page — the
    # skew is inherent to the DEC-DB-CANDLE-COUNT-CACHE-001 design, documented
    # not eliminated). When the page returned rows, the total is raised to at
    # least ``offset + len(rows)`` — an OBSERVED lower bound. An EMPTY page
    # proves nothing (``offset + 0`` is only a bound for k > 0 rows) and must
    # never adjust the total: an unconditional clamp fabricated
    # ``total == offset`` for any offset past the end (review fold).
    if raw_rows:
        total = max(total, safe_offset + len(raw_rows))
    if stats_ctx is not None:
        _LOG.info(
            _STATS_READ_EVENT,
            kind=KIND_BARS,
            symbol=symbol,
            interval=interval,
            validated=stats_ctx.stats is not None,
            page_source="manifest" if pruned is not None else "live_scan",
        )
    # Split-brain screen (DEC-DASHBOARD-READ-SCREEN-001): route the page through
    # the ONE COMMON engine-parity screen so a bad-tick spike / an insane bar the
    # engine + /movers drop is never SERVED raw here — /db == /movers == engine.
    # Runs AFTER the raw page rows are resolved (pruned OR legacy path — the
    # screen is path-agnostic) and AFTER the observed-lower-bound clamp above,
    # which deliberately reads the RAW page length (a tighter bound on the RAW
    # stored count the total describes). Spot/futures candles keep the FULL
    # screen (insane + ×4 bad-tick), seeded with the last KEPT in-session bar
    # before the page's earliest row so the cross-row bad-tick screen is correct
    # at the page's left edge (`_bad_tick_seed_bar`; the deep-cluster residual is
    # documented there). CONTRACT-TYPE-AWARE on the F&O segment: this route ALSO
    # serves CE/PE contract partitions (via the FNO cascade), and option PREMIA
    # legitimately move ≥4×/minute on leverage — so a decoded CE/PE symbol
    # screens INSANE-ONLY (`bad_tick_enabled=False`, no seed — the cross-row arm
    # is off), matching /movers-options + the /db expired-options tab exactly
    # (the same scope rule as `_read_future_session(bad_tick=)`); FUT and
    # unparseable F&O symbols keep the full default-ON screen. Clean pages
    # return the same object (byte-identical; the screen is an O(page-rows)
    # Python pass — honest cost, not zero work). HONEST envelope: the seed read
    # is ONE additional DuckDB query per non-option page over the partition
    # glob — an O(files) Parquet-metadata scan (a constant-factor addition to
    # the per-click query count; the LIMIT-k row read itself is tiny), eager
    # even on clean data; and `total` above is the DATASET-wide
    # DISTINCT-timestamp count (manifest- OR legacy-served), so on a CORRUPT
    # partition it OVER-counts by the number of screened-out rows (the served
    # page shows fewer rows than `total`/`limit` imply) — a bounded honesty gap
    # on corrupt data only, never a fabricated bar.
    is_option_contract = False
    if is_fno:
        parts = parse_fno_symbol(symbol)
        is_option_contract = parts is not None and parts.option_type is not None
        # NIT (disclosed): an UNPARSEABLE F&O symbol falls to the default-ON
        # full screen — unreachable via the product (the FNO cascade offers
        # only parse-decoded contracts) and conservative in direction (a
        # mystery partition is screened like a future, never served a spike).
    raw_rows = screen_rows(
        raw_rows,
        out_columns,
        source=skip_source,
        # Options: no seed (the cross-row bad-tick arm is off, a seed is unused
        # work); spot/FUT/unparseable: the bounded seeded default. `None` (not
        # True) on the default arm preserves the BRUTEX_BAD_TICK_SCREEN
        # kill-switch read — the engine-parity contract.
        seed_bar=(
            None
            if is_option_contract
            else _bad_tick_seed_bar(
                conn, base, raw_rows=raw_rows, out_columns=out_columns, skip_source=skip_source
            )
        ),
        bad_tick_enabled=False if is_option_contract else None,
    )
    # Render the bar timestamp as a clean IST wall-clock (no +05:30 suffix,
    # DATE-ONLY for 1day); every other cell goes through the generic JSON
    # coercion (shared helper).
    rendered = _render_candle_rows(raw_rows, out_columns, interval)
    # Derive the %-change columns WITHIN the shown interval's OWN basis — never
    # mix two price scales (DEC-DB-CANDLE-PERINTERVAL-001): a 1-day view compares
    # against the prior **1-day** close (so a demerger/split-adjusted daily series
    # stays self-consistent), a 1-minute view against the prior day's last
    # **1-minute** close (DEC-SPOT-PREVCLOSE-1MIN-001). Mixing them is what made
    # adjustment-mismatched symbols (e.g. TMPV — adjusted daily ~112.9 vs raw
    # intraday ~186.6) read garbage. The engine store is unaffected — this is the
    # read-only viewer.
    #
    # The baseline reads ONLY what THIS page needs (DEC-DB-UNIVERSE-O1-LATENCY-001):
    # * 1-day view — the whole 1-day partition (intrinsically tiny: ≤ one row per
    #   trading day, ~1.5k rows for 6 years, flat as the lake deepens).
    # * intraday view — page-bounded: the prior-day last-1-minute close is read for
    #   the page's OWN date span plus the single bar immediately before it, NOT the
    #   whole multi-year 1-minute partition. So a prev/next paging click scans ~1–2
    #   trading days of minute bars regardless of how many years are stored — the
    #   per-click cost stays flat as the lake grows. Byte-identical to the prior
    #   full-partition scan for every page date (proven in the db-query tests).
    # Build the prev-close VALUE map AND its predecessor-DATE provenance map from
    # ONE scan (the latter drives the contiguity marker,
    # DEC-DB-PREVCLOSE-CONTIGUITY-MARKER-001).
    if interval == _DAILY_INTERVAL:
        last_close_by_date = _daily_close_by_date(
            conn, lake_root, exchange=exchange, segment=segment, symbol=symbol
        )
    else:
        last_close_by_date = _intraday_last_close_with_predecessor(
            conn,
            lake_root,
            exchange=exchange,
            segment=segment,
            symbol=symbol,
            page_dates=_page_ist_dates(raw_rows, out_columns),
        )
    prev_close = _prev_trading_close(last_close_by_date)
    prev_close_date = _prev_trading_close_date(last_close_by_date)
    derived = _spot_derived_rows(
        raw_rows,
        out_columns,
        prev_close,
        prev_close_date,
        exchange=exchange,
        interval=interval,
    )
    # F&O option pages also carry per-bar moneyness (ITM/ATM/OTM + ATM±N) classified
    # against the underlying spot at each bar (DEC-FNO-MONEYNESS-001); spot pages do not.
    extra_columns: list[str] = [*SPOT_DERIVED_COLUMNS, *_fno_derived_columns(segment)]
    step_confidence: StepConfidence | None = None
    spot_join_interval: str | None = None
    if segment == FNO_CANDLE_LAKE_SEGMENT:
        fno_cells, step_confidence, spot_join_interval = _fno_derived_rows(
            conn,
            lake_root,
            exchange=exchange,
            symbol=symbol,
            interval=interval,
            raw_rows=raw_rows,
            out_columns=out_columns,
            registry_path=registry_path,
            count_cache=count_cache,
            generation=generation,
            # The evidence-grounded stock drift rung for the moneyness spot-join
            # (DEC-DB-MONEYNESS-STOCK-EVIDENCE-001) — default ``None`` at every
            # layer keeps the pure-function callers byte-identical; the dashboard
            # injects its composite-token-memoised loader (see ``build_app``).
            stock_drift_loader=stock_drift_loader,
        )
        extra_cells: list[tuple[Any, ...]] = [
            (*spot_pair, *fno) for spot_pair, fno in zip(derived, fno_cells, strict=True)
        ]
    else:
        extra_cells = list(derived)
    rows = [
        [*rendered_row, *extra] for rendered_row, extra in zip(rendered, extra_cells, strict=True)
    ]
    # Resolve the honest applied-range chip metadata (DEC-DB-UX-PACK-001 Feature 2).
    # SPOT reports [2020-01-01, today]; an F&O contract reports its FULL STORED LIFE
    # (resilient MIN/MAX over the IST-date span) ONLY when a bound is blank — an
    # explicit range needs no scan. The span is paging-invariant (a property of the
    # partition alone), so it is memoised behind the SAME generation-stamped cache
    # as the COUNT (candle_span_key — the hostile-review MAJOR: without this, every
    # prev/next paging click re-ran the MIN/MAX column scan, the exact O(files)-per-
    # click read DEC-LAKE-READ-O1-AUDIT-001 exists to prevent). A pull bumps the
    # generation → re-scan (never stale); no injected cache (the pure default) runs
    # the scan inline, once per query. This is REPORTING metadata; the WHERE above
    # uses the raw bounds, so the row set / total are byte-unchanged.
    if is_fno and (start is None or end is None):

        def _stored_life() -> tuple[str | None, str | None]:
            # Manifest-first span (DEC-LAKE-STATS-MANIFEST-001): the validated
            # per-file bounds fold to the identical MIN/MAX IST dates
            # (timestamp → IST date is monotonic) with zero Parquet I/O; a
            # non-served request (validation failed OR the page degraded
            # mid-request — the whole-request degrade law) runs the legacy
            # resilient scan byte-identically.
            if stats_ctx is not None and stats_ctx.served:
                return stats_ctx.ist_span()
            return _fno_stored_ist_span_resilient(
                conn, base, source=source, skip_source=skip_source
            )

        if count_cache is None:
            stored_start_ist, stored_end_ist = _stored_life()
        else:
            stored_start_ist, stored_end_ist = count_cache.get_or_compute(
                candle_span_key(exchange, segment, symbol, interval),
                generation=generation,
                compute=_stored_life,
            )
    else:
        stored_start_ist, stored_end_ist = (None, None)
    if stats_ctx is not None and stats_ctx.should_seed:
        # Read-side write-behind seed (the ONLY seeding mechanism, lazy
        # accretion): the shard was absent/invalid and the legacy scan just
        # answered this request — rebuild it best-effort so the NEXT click is
        # manifest-served. Never raises; any anomaly aborts the seed, and a
        # FAILED attempt is latched per (dataset, generation) so a
        # permanently-unseedable dataset never repays the grouped scan on
        # every click (see _seed_stats_once).
        _seed_stats_once(
            conn,
            lake_root,
            kind=KIND_BARS,
            key={"exchange": exchange, "segment": segment, "symbol": symbol, "interval": interval},
            base=base,
            identity=("timestamp",),
            count_cache=count_cache,
            generation=generation,
        )
    applied_range = resolve_applied_range(
        is_fno=is_fno,
        explicit_start=start,
        explicit_end=end,
        stored_start_ist=stored_start_ist,
        stored_end_ist=stored_end_ist,
        today_ist=_today_ist_iso(),
    )
    payload: dict[str, Any] = {
        "symbol": symbol,
        "interval": interval,
        "columns": [*out_columns, *extra_columns],
        "rows": rows,
        "total": total,
        "limit": safe_limit,
        "offset": safe_offset,
        "applied_range": applied_range,
    }
    if is_fno:
        # The strike-step honesty label (DEC-MONEYNESS-REGISTRY-STEP-001): "registry"
        # = the AUTHORITATIVE vendor chain sized the ATM±N grid; "observed" = only the
        # sparse lake ladder did (the frontend shows a low-confidence marker so an
        # overestimated step is never presented silently); None = no step at all.
        payload["strike_step_confidence"] = (
            step_confidence.value if step_confidence is not None else None
        )
        # The spot-join provenance (DEC-DB-FNO-DISPLAY-TRUTH-001 item 9): which spot
        # interval served the per-bar moneyness/spot join — "1minute" marks the
        # fallback join on a partially-prebuilt lake (the frontend renders an honest
        # note when it differs from the page interval); None when nothing joined.
        # ALWAYS stamped on an F&O page (the strike_step_confidence stable-key-set
        # precedent).
        payload["spot_join_interval"] = spot_join_interval
    return payload


def _date_bounds(
    start: str | None, end: str | None, *, interval: str
) -> tuple[list[str], list[Any]]:
    """Build the inclusive calendar-date ``WHERE`` fragments shared by both readers.

    Returns ``(clauses, params)`` where each clause compares the bar's *calendar
    date* against a bound ISO date. Which calendar the comparison uses depends on
    the *interval*, so the filter matches the date the grid actually renders:

    * ``"1day"`` (the DERIVED daily anchor) — a daily bar is stamped midnight-IST,
      which the lake stores as the PREVIOUS calendar day ``18:30`` UTC. A UTC-date
      comparison is therefore OFF BY ONE versus the IST date the grid shows
      (:func:`_format_candle_timestamp` renders 1day DATE-ONLY in IST): filtering
      ``2026-02-03`` under the old UTC bound returned the ``02-04`` row and dropped
      the ``02-03`` row. So 1day compares the **IST** calendar date
      (:data:`_IST_DATE_EXPR`) — DEC-DB-UX-PACK-001 Feature 3.
    * every intraday interval — keeps the **UTC** calendar date
      (:data:`_UTC_DATE_EXPR`), byte-identical to the prior behaviour. Real intraday
      bars trade 03:45–10:00 UTC where the UTC and IST dates coincide, so this is
      the same day either way and is independent of the ambient DuckDB session
      ``TimeZone`` (the operator runs Asia/Kolkata; regression-pinned).

    Both bounds inclusive (Golden Rule #7). A ``None`` bound contributes nothing.

    Raises
    ------
    ValueError
        On a malformed ISO date bound (via :func:`_parse_iso_date`).
    """
    date_expr = _IST_DATE_EXPR if interval == _DAILY_INTERVAL else _UTC_DATE_EXPR
    clauses: list[str] = []
    params: list[Any] = []
    if start is not None:
        clauses.append(f"{date_expr} >= ?")
        params.append(_parse_iso_date(start, "start").isoformat())
    if end is not None:
        clauses.append(f"{date_expr} <= ?")
        params.append(_parse_iso_date(end, "end").isoformat())
    return clauses, params


def _today_ist_iso() -> str:
    """Today's IST calendar date as ISO ``YYYY-MM-DD`` — the SPOT filter's default END.

    The ``/db`` SPOT date filter reports ``[2020-01-01, today]`` as its applied
    range when the operator leaves the inputs blank (DEC-DB-UX-PACK-001 Feature 2),
    so a blank "to" shows the honest current day, never an empty-then-faked-live one.
    """
    return datetime.now(_IST).date().isoformat()


def resolve_applied_range(
    *,
    is_fno: bool,
    explicit_start: str | None,
    explicit_end: str | None,
    stored_start_ist: str | None,
    stored_end_ist: str | None,
    today_ist: str,
    spot_floor: str = _SPOT_FILTER_FLOOR_ISO,
) -> dict[str, str | None]:
    """Resolve the HONEST applied date range the ``/db`` filter chip shows.

    An empty operator input is filled with the true DEFAULT for its dataset, never
    "today" (the inert-filter illusion this closes — DEC-DB-UX-PACK-001 Feature 2):

    * SPOT (``is_fno=False``): ``[2020-01-01, today]`` — the pull floor
      (``GROWW_HISTORY_FLOOR``, :data:`_SPOT_FILTER_FLOOR_ISO`) to the current IST day.
    * F&O contract (``is_fno=True``): its FULL STORED LIFE — the earliest → latest
      stored bar IST date; ``None`` on either side when the partition is empty (an
      honest "no stored data" rather than a fabricated span).

    An explicit bound always wins and each side defaults INDEPENDENTLY (an explicit
    ``from`` with a blank ``to`` keeps the explicit start + the default end). Pure —
    the returned range is REPORTING metadata only; the row-filtering ``WHERE`` still
    uses the raw explicit bounds via :func:`_date_bounds`, so the page's rows / total
    and the paging-invariant count cache are byte-unchanged (the resolved range is
    always a superset of the returned rows, so "every returned row lies inside the
    selected range" holds by construction — test-pinned).
    """
    if explicit_start is not None:
        start: str | None = explicit_start
    elif is_fno:
        start = stored_start_ist
    else:
        start = spot_floor
    if explicit_end is not None:
        end: str | None = explicit_end
    elif is_fno:
        end = stored_end_ist
    else:
        end = today_ist
    return {"start": start, "end": end}


def _iso_date_or_none(value: Any) -> str | None:
    """Render a DuckDB ``DATE`` cell as ISO ``YYYY-MM-DD``, or ``None`` for a NULL.

    ``MIN``/``MAX`` over the IST-date expression returns a :class:`datetime.date`
    (or ``None`` for an empty scan); anything else defensively falls back to ``str``.
    """
    if value is None:
        return None
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _fno_stored_ist_span_resilient(
    conn: Any, base: Path, *, source: str, skip_source: str
) -> tuple[str | None, str | None]:
    """The (min, max) IST calendar date of an F&O partition's stored bars, resilient.

    The F&O ``/db`` filter's "full stored life" default (DEC-DB-UX-PACK-001
    Feature 2): one ``MIN``/``MAX`` over :data:`_IST_DATE_EXPR` through the SAME
    corrupt-file-resilient probe (:func:`_execute_parquet_resilient`) every other
    candle read uses, so a dup-poisoned / unreadable partition degrades to
    ``(None, None)`` (an honest "no derivable span") instead of a 500. Returns
    ``(None, None)`` for an empty / all-unreadable scan.
    """
    rows = _execute_parquet_resilient(
        conn,
        base,
        f"SELECT MIN({_IST_DATE_EXPR}), MAX({_IST_DATE_EXPR}) FROM {source}",  # noqa: S608  # nosec B608 - constant expr over read_parquet(?) with a bound path
        [],
        source=skip_source,
    )
    if not rows:  # all files unreadable → an honest "no derivable span"
        return (None, None)
    lo, hi = rows[0]
    # A readable-but-empty scan yields (None, None); _iso_date_or_none maps each None
    # straight through, so no separate empty-guard branch is needed.
    return (_iso_date_or_none(lo), _iso_date_or_none(hi))


def _render_candle_rows(
    raw_rows: list[tuple[Any, ...]], out_columns: list[str], interval: str
) -> list[list[Any]]:
    """Render already-fetched candle/option rows with a clean IST ``timestamp``.

    The ``timestamp`` cell (if projected) is rendered as a clean IST wall-clock
    string (no ``+05:30`` suffix) via :func:`_format_candle_timestamp` — DATE-ONLY
    for the ``"1day"`` *interval* (DEC-DB-CANDLE-DERIVED-001); every other cell
    goes through the generic :func:`_json_safe` coercion. *raw_rows* is the
    already-fetched cursor result (the caller fetches once so the spot reader can
    also derive the %-change columns from the same rows). Shared by
    :func:`query_candles` (spot) and :func:`query_option_candles` (options).
    """
    ts_idx = out_columns.index("timestamp") if "timestamp" in out_columns else -1
    return [
        [
            _format_candle_timestamp(cell, interval) if i == ts_idx else _json_safe(cell)
            for i, cell in enumerate(row)
        ]
        for row in raw_rows
    ]


def _to_decimal(value: Any) -> Decimal:
    """Coerce a DuckDB price cell to an exact :class:`~decimal.Decimal`.

    Spot OHLC is stored Parquet ``Float64`` (DuckDB returns ``float``); a
    pre-existing ``Decimal`` passes through untouched. Used only for the
    derived %-change metrics — full precision, never money-rounded (CLAUDE.md §4).
    """
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _bar_ist_date(ts: datetime) -> date:
    """Return the IST calendar date of *ts* (the bar's trading day).

    IST-anchored (not UTC) so it aligns intraday bars (09:15–15:30 IST) with the
    1d daily bars regardless of whether the daily bar is stamped at IST midnight
    or at a session instant (DEC-DB-CANDLE-DERIVED-001). A naive datetime
    (defensive — DuckDB returns tz-aware) is read as-is.
    """
    return (ts.astimezone(_IST) if ts.tzinfo is not None else ts).date()


def _bar_ist_time(ts: datetime) -> time:
    """Return the IST wall-clock time-of-day of *ts* (for the 09:15 gap anchor)."""
    return (ts.astimezone(_IST) if ts.tzinfo is not None else ts).time()


def _intraday_last_close_by_date(
    conn: Any, lake_root: Path, *, exchange: str, segment: str, symbol: str
) -> dict[date, Decimal]:
    """Read the symbol's LAST 1-minute close per day into a ``{ist_date: close}`` dict.

    The %-change / gap baseline is the prior trading day's **last 1-minute close**
    (the 15:29 IST bar), NOT the 1-day official close — the engine is 1-minute-based,
    so anchoring on the 1-minute feed keeps ``change_pct`` / ``open_gap_pct``
    internally consistent with the bars actually swept (DEC-SPOT-PREVCLOSE-1MIN-001;
    operator 2026-06-03).

    Reads ``<lake>/bars/<exch>/<seg>/<symbol>/1minute/**/*.parquet`` via a bound
    DuckDB ``read_parquet`` glob (the coordinates are already validated by
    :func:`_validate_candle_coords` in the caller — no free user text) and
    aggregates with ``arg_max(close, timestamp)`` grouped by the bar DATE — ONE
    columnar pass returns ~one row per trading day, so the per-bar lookup
    downstream is O(1) (CLAUDE.md §5). Grouped by the bar's IST date via the COMMON
    session-tz-independent :data:`_IST_DATE_EXPR` (NOT a bare ``CAST(timestamp AS
    DATE)``, which resolves in the ambient DuckDB session ``TimeZone`` and splits
    one NSE session across two dates on a negative-offset host —
    DEC-SPOT-SESSION-DAYS-IST-TZ-001), so the key matches :func:`_bar_ist_date`
    used on the viewed bars regardless of host tz. Returns ``{}`` when the symbol
    has no 1-minute partition yet, so the derived columns degrade to blank rather
    than raising (DEC-DB-CANDLE-DERIVED-001).
    """
    base = lake_root / "bars" / exchange / segment / symbol / _INTRADAY_BASE_INTERVAL
    if not (base.is_dir() and any(base.rglob("*.parquet"))):
        return {}
    # glob is built from validated coordinates (no free user text); the path is
    # given to read_parquet as a bound parameter. arg_max(close, timestamp) is
    # the close of the day's LAST bar (the 15:29 IST close). Resilient read
    # (DEC-DB-CORRUPT-SKIP-001): a corrupt 1-minute Parquet degrades the baseline to
    # its readable subset (a blank %-change for the affected bars), never a 500.
    rows = _execute_parquet_resilient(
        conn,
        base,
        f'SELECT {_IST_DATE_EXPR} AS d, arg_max(close, "timestamp") AS last_close '  # noqa: S608  # nosec B608 - _IST_DATE_EXPR is a hardcoded constant; path bound
        "FROM read_parquet(?, union_by_name=true) GROUP BY d",
        [],
        source=f"candle-prevclose:{exchange}/{segment}/{symbol}",
    )
    return {day: _to_decimal(last_close) for day, last_close in rows}


def _prev_trading_close(last_close: dict[date, Decimal]) -> dict[date, Decimal]:
    """Map each trading date to the PREVIOUS trading day's last 1-minute close.

    Built once from the ``{date: last_1min_close}`` dict by walking the dates in
    sorted order, so a per-bar lookup is O(1) (DEC-DB-CANDLE-DERIVED-001). The
    earliest date has no predecessor and is therefore absent from the result
    (its bars get a blank %-change).
    """
    out: dict[date, Decimal] = {}
    prev: date | None = None
    for day in sorted(last_close):
        if prev is not None:
            out[day] = last_close[prev]
        prev = day
    return out


def _prev_trading_close_date(last_close: dict[date, Decimal]) -> dict[date, date]:
    """Map each trading date to the PREVIOUS AVAILABLE trading date in the series.

    The provenance twin of :func:`_prev_trading_close`: it exposes the exact
    predecessor DATE whose close that map used as the baseline, so the
    prev-close contiguity marker (DEC-DB-PREVCLOSE-CONTIGUITY-MARKER-001) can
    compare the baseline's actual source session against the NSE-calendar's
    immediately-preceding session (:func:`previous_trading_session`) — a
    MISMATCH means a real session was bridged (a lake hole), so the %-change is
    marked non-contiguous. Same O(N) single sorted walk; the earliest date has
    no predecessor and is absent (its bars carry no contiguity verdict).
    """
    out: dict[date, date] = {}
    prev: date | None = None
    for day in sorted(last_close):
        if prev is not None:
            out[day] = prev
        prev = day
    return out


def _page_ist_dates(raw_rows: list[tuple[Any, ...]], out_columns: list[str]) -> set[date]:
    """The distinct IST trading dates present in a fetched page's ``timestamp`` column.

    The set of dates the derived %-change baseline must cover for THIS page
    (DEC-DB-UNIVERSE-O1-LATENCY-001). Keyed by :func:`_bar_ist_date` — the SAME
    key :func:`_spot_derived_rows` looks the baseline up by — so the page-bounded
    baseline map aligns bar-for-bar. Empty when ``timestamp`` is not projected
    (the derived columns are then all blank anyway) or the page is empty.
    """
    ts_idx = out_columns.index("timestamp") if "timestamp" in out_columns else -1
    if ts_idx < 0:
        return set()
    return {_bar_ist_date(row[ts_idx]) for row in raw_rows if isinstance(row[ts_idx], datetime)}


#: How many same-session predecessor bars the ``/db`` bad-tick seed reads + screens
#: to resolve the page's left edge (DEC-DASHBOARD-READ-SCREEN-001). Bounds the ROW
#: read to k (the glob's O(files) metadata scan is the honest per-click cost — see
#: `_bad_tick_seed_bar`); a spike cluster deeper than this is the honest
#: documented residual (the whole-day /movers reader is always correct).
_BAD_TICK_SEED_WINDOW: Final[int] = 8


def _bad_tick_seed_bar(
    conn: Any,
    base: Path,
    *,
    raw_rows: list[tuple[Any, ...]],
    out_columns: list[str],
    skip_source: str,
) -> tuple[Any, float, float, float, float] | None:
    """Last KEPT in-session bar before the page's earliest row, or ``None``.

    The bounded prior-bar seed for the ``/db`` cross-row bad-tick screen
    (DEC-DASHBOARD-READ-SCREEN-001), mirroring the predecessor read of
    :func:`_intraday_last_close_with_predecessor`: reads the last
    :data:`_BAD_TICK_SEED_WINDOW` bars STRICTLY BEFORE the page's earliest row AND
    on the same IST session date (the COMMON :data:`_IST_DATE_EXPR` equality —
    never a bare ``CAST(timestamp AS DATE)``, which resolves in the ambient
    DuckDB session TimeZone; DEC-DB-TZ-MONEYNESS-FIXES-001), SCREENS that
    ascending window through the SAME
    :func:`~brutex.data.bar_screen.screen_rows`, and returns the LAST KEPT bar as
    the seed — so the page's leading row is judged against the last GOOD close, not
    a spike sitting immediately before the page (the single-spike / short-cluster
    left-edge case; screening only the raw immediate predecessor would wrongly
    baseline the page's first good row against that spike). The window read is
    ``ORDER BY DESC LIMIT k`` over the partition glob — HONEST cost: the k-row
    read is tiny, but the glob itself is an O(files) Parquet-metadata scan (a
    constant-factor addition per click, NOT O(1) in partition depth). Returns
    ``None`` when the page lacks OHLC/``timestamp`` columns, is empty, has no
    same-session predecessor (a page that starts at 09:15), or the whole window
    screens out. HONEST residual: a spike cluster DEEPER than the window, or a
    spike as the window's own first bar, is not resolved — the whole-day
    ``/movers`` reader is always correct; this bounded seed only narrows the
    ``/db`` page-local left edge.
    """
    if any(name not in out_columns for name in ("timestamp", "open", "high", "low", "close")):
        return None
    ts_idx = out_columns.index("timestamp")
    page_ts = [row[ts_idx] for row in raw_rows if isinstance(row[ts_idx], datetime)]
    if not page_ts:
        return None
    earliest = min(page_ts)
    # The same-session bound is an IST-date equality through the COMMON
    # :data:`_IST_DATE_EXPR` (never a bare ``CAST(... AS DATE)``, which resolves
    # in the ambient DuckDB session TimeZone and would SPLIT an IST session
    # across two local dates under e.g. America/New_York — the
    # DEC-DB-TZ-MONEYNESS-FIXES-001 hazard class; the Python side mirrors it
    # with an explicit ``astimezone(_IST)``, naive timestamps read as UTC per
    # the lake contract).
    earliest_utc = (
        earliest.replace(tzinfo=UTC) if earliest.tzinfo is None else earliest.astimezone(UTC)
    )
    window = _execute_parquet_resilient(
        conn,
        base,
        'SELECT "timestamp", "open", "high", "low", "close" '  # noqa: S608  # nosec B608 - constant expr over read_parquet(?) with bound params
        "FROM read_parquet(?, union_by_name=true) "
        f'WHERE "timestamp" < ? AND {_IST_DATE_EXPR} = ? '
        'ORDER BY "timestamp" DESC LIMIT ?',
        [earliest, earliest_utc.astimezone(_IST).date(), _BAD_TICK_SEED_WINDOW],
        source=skip_source,
    )
    if not window:
        return None
    # Screen the ascending predecessor window; the last SURVIVOR is the seed.
    ascending = list(reversed(window))
    kept = screen_rows(
        ascending,
        ["timestamp", "open", "high", "low", "close"],
        source=skip_source,
    )
    if not kept:
        return None
    ts, open_, high, low, close = kept[-1]
    return (ts, open_, high, low, close)


def _bars_file_windows(
    lake_root: Path,
    base: Path,
    *,
    exchange: str,
    segment: str,
    symbol: str,
    interval: str,
) -> list[tuple[str, datetime, datetime]] | None:
    """Chronological ``(path, window_lo, window_hi_exclusive)`` per month file — or ``None``.

    The %-baseline file PRUNER (DEC-DB-BASELINE-MANIFEST-ROUTE-001): the
    baseline readers previously bound the WHOLE multi-year partition glob per
    paging click — DuckDB prunes row groups by the timestamp predicate but
    still OPENS every file's FOOTER (row-group pruning ≠ file pruning), so
    the per-click cost grew with the stored years. This helper bounds each
    month file by a half-open UTC instant window so the readers bind ONLY the
    files that can hold their rows:

    * **Manifest arm** — a VALIDATED stats shard
      (:func:`~brutex.data.lake_stats_manifest.load_valid_dataset_stats_guarded`,
      full anti-illusion validation; kill switch off / absent / stale shard
      → the grammar arm) gives each file's EXACT stamped ``[min_ts, max_ts]``
      (hi made exclusive with +1µs); rows are already strictly-increasing
      chronological. Used purely as a FILE-LIST pruner — the data is read
      LIVE, so no count-serving TOCTOU re-stat is needed (the residual
      mid-request replace race is the same one the glob bind always had).
    * **Grammar arm** — the directory listing bounded by each file's OWN
      canonical UTC-month name
      (:func:`~brutex.data.lake_stats_manifest.rel_month_bounds` — the
      partition law: a month file holds only its own UTC month's bars). ONE
      O(files) listing, ZERO footer opens. A TIMED-OUT consult
      (DEC-DASHBOARD-COLD-IO-DEADLINE-001) returns ``None`` BEFORE this arm —
      the wedged volume must not pay the next O(files) ``rglob`` listing
      against itself.

    ``None`` = cannot bound (a non-canonical filename in the tree / a listing
    fault / a wedged-volume consult timeout) — the caller degrades to the
    legacy whole-glob bind, byte-identical
    behaviour. HONEST envelope (Golden Rule #10): the grammar arm's bounds
    assume the UTC-month partition invariant; a month-invariant-VIOLATING lake
    (foreign rows — the ``lake_invariants``/``lake_heal`` finding class) may
    prune a foreign row's file out, exactly as the manifest's clause-4
    month-bounds validation refuses such a shard; the invariant scanners are
    the remediation, not this reader.
    """
    consult = load_valid_dataset_stats_guarded(
        lake_root,
        kind=KIND_BARS,
        key={"exchange": exchange, "segment": segment, "symbol": symbol, "interval": interval},
    )
    stats = consult.stats
    if stats is not None:
        return [
            ((base / row.rel).as_posix(), row.min_ts, row.max_ts + timedelta(microseconds=1))
            for row in stats.files
        ]
    if consult.timed_out:
        # The volume is WEDGED: falling through to the grammar arm would run
        # the O(files) ``base.rglob`` listing against the SAME wedged volume
        # (the D1 hole). ``None`` is this function's existing "cannot bound →
        # legacy whole-glob bind" contract.
        return None
    try:
        listed = sorted(base.rglob("*.parquet"))
    except OSError:
        return None
    windows: list[tuple[str, datetime, datetime]] = []
    for file in listed:
        bounds = rel_month_bounds(KIND_BARS, file.relative_to(base).as_posix())
        if bounds is None:
            return None  # a non-canonical name cannot be bounded — degrade to the whole glob
        windows.append((file.as_posix(), bounds[0], bounds[1]))
    windows.sort(key=lambda w: w[1])
    return windows


def _files_intersecting(
    windows: list[tuple[str, datetime, datetime]], lo: datetime, hi: datetime
) -> list[str]:
    """The window files intersecting the half-open instant span ``[lo, hi)``."""
    return [path for (path, w_lo, w_hi) in windows if w_lo < hi and w_hi > lo]


def _predecessor_files(windows: list[tuple[str, datetime, datetime]], lo: datetime) -> list[str]:
    """Files that can hold a bar STRICTLY BEFORE *lo*, newest-first.

    On the manifest arm the FIRST candidate is guaranteed to hold one
    (``min_ts`` is a real bar instant); on the grammar arm a month file whose
    month started before *lo* may still hold only ≥ *lo* bars (data starting
    mid-month), so the caller STEPS BACK through the list until a row lands
    (documented step-back — typically one query, worst-case O(files) queries
    on a pathologically empty-before-*lo* dataset).
    """
    return [path for (path, w_lo, _w_hi) in windows if w_lo < lo][::-1]


def _intraday_last_close_with_predecessor(
    conn: Any,
    lake_root: Path,
    *,
    exchange: str,
    segment: str,
    symbol: str,
    page_dates: set[date],
) -> dict[date, Decimal]:
    """Page-span last-1-minute-close map WITH the single predecessor bar prepended.

    The page-bounded read underneath :func:`_intraday_prev_close_by_page_date`
    (DEC-DB-UNIVERSE-O1-LATENCY-001), exposed as its own helper so BOTH the
    prev-close VALUE map (:func:`_prev_trading_close`) and the prev-close
    predecessor-DATE provenance map (:func:`_prev_trading_close_date`, driving the
    contiguity marker DEC-DB-PREVCLOSE-CONTIGUITY-MARKER-001) can be built from ONE
    scan. Reads only:

    1. the last 1-minute close per day for the page's OWN ``[min, max]`` date span
       (an intraday page covers ~1–2 trading days), and
    2. the single 1-minute bar immediately BEFORE the page's earliest date — the
       one baseline that lives outside the page's own span (the predecessor of the
       first page row).

    Both reads are bounded by the bar ``timestamp`` (IST-midnight instants) AND —
    the DEC-DB-BASELINE-MANIFEST-ROUTE-001 honesty fix — by a PRUNED FILE LIST
    (:func:`_bars_file_windows`: manifest-exact per-file bounds, else the
    canonical UTC-month filename grammar): the span read binds only the page's
    own month file(s) and the predecessor read binds one preceding file (with the
    documented grammar-arm step-back when that file holds no bar before the page).
    HONEST cost (Golden Rule #10; the pre-fix claim "row-group-pruned ⇒ flat" was
    FALSE — DuckDB still opened EVERY file's footer per click): the per-click read
    is now O(page months + 1 predecessor file) footer opens plus one O(files)
    directory listing (grammar arm; the manifest arm pays O(D) stat validation),
    degrading to the legacy whole-glob bind — byte-identical results, pruning
    never changes values — when neither arm can bound the tree. The result is
    **byte-identical** to the prior full-partition scan for every page date: the
    page's dates are contiguous trading days (a page is a ``LIMIT``/``OFFSET``
    slice of timestamp-sorted bars), and prepending their single shared
    predecessor makes :func:`_prev_trading_close` map each page date to the same
    prior-day close the full scan would (proven in
    ``tests/unit/test_dashboard_db_query.py`` + the manifest-on/off parity pins
    in ``tests/unit/test_dashboard_db_baseline_manifest.py``). Returns ``{}``
    (every derived cell blank) when *page_dates* is empty, the symbol has no
    1-minute partition, or a corrupt file was skipped (the adjacency guard — a
    bridged predecessor would baseline a NON-ADJACENT close).

    The baseline ALWAYS reads the ``1minute`` partition (DEC-SPOT-PREVCLOSE-1MIN-001),
    even when a coarser intraday timeframe is being VIEWED — *page_dates* come from
    the viewed bars, the close baseline from the 1-minute feed underneath them.
    """
    if not page_dates:
        return {}
    base = lake_root / "bars" / exchange / segment / symbol / _INTRADAY_BASE_INTERVAL
    if not (base.is_dir() and any(base.rglob("*.parquet"))):
        return {}
    lo, hi = min(page_dates), max(page_dates)
    # IST-midnight instants bound the scan to the page's own span (+ predecessor);
    # the GROUP-BY / projection key is the bar's IST date via the COMMON
    # session-tz-independent :data:`_IST_DATE_EXPR` (the SAME keying as
    # _intraday_last_close_by_date and the _bar_ist_date lookup keys in
    # _spot_derived_rows). A bare CAST(ts AS DATE) resolves in the ambient DuckDB
    # session TimeZone and would roll a 03:45-UTC (09:15 IST) session bar back one
    # calendar day on a negative-offset host, mis-keying the baseline —
    # DEC-SPOT-SESSION-DAYS-IST-TZ-001.
    lo_instant = datetime.combine(lo, time(0, 0), tzinfo=_IST)
    hi_instant = datetime.combine(hi + timedelta(days=1), time(0, 0), tzinfo=_IST)
    skip_source = f"candle-prevclose:{exchange}/{segment}/{symbol}"
    # ADJACENCY GUARD (DEC-DB-CORRUPT-SKIP-001 BLOCKER fix): the prior-day-close baseline
    # is ONLY valid if every CANDIDATE file read cleanly — `_prev_trading_close` maps each
    # page date to its IMMEDIATE predecessor, and the predecessor read (#2 below) finds
    # "the bar just before `lo`". If a bounded-candidate file was skipped (corrupt), the
    # true adjacent predecessor may have lived in the dropped file, so any %-change we
    # computed would be baselined on a NON-ADJACENT earlier day — a confidently-WRONG
    # number. So we record whether either read omitted a candidate and, if so, return {} →
    # every affected bar gets an honest BLANK %-change, never a fabricated one (Golden
    # Rule #1). A corrupt file OUTSIDE the pruned candidate lists provably cannot hold
    # the span's bars or the true predecessor (`_bars_file_windows`'s window bounds —
    # DEC-DB-BASELINE-MANIFEST-ROUTE-001 #1670 / DEC-DB-LATENCY-HARDENING-001 #1719),
    # so it no longer blanks the page (the named strictly-narrower degrade).
    skip_sink: list[bool] = []
    # File-list pruning (DEC-DB-BASELINE-MANIFEST-ROUTE-001): bind ONLY the
    # files whose windows can hold the read's rows; None ⇒ the legacy
    # whole-glob bind (byte-identical — pruning only SELECTS files).
    windows = _bars_file_windows(
        lake_root,
        base,
        exchange=exchange,
        segment=segment,
        symbol=symbol,
        interval=_INTRADAY_BASE_INTERVAL,
    )
    span_files = (
        _files_intersecting(windows, lo_instant, hi_instant) if windows is not None else None
    )
    # 1) last 1-minute close per day, bounded to the page's [lo, hi] span. glob/
    #    file list is built from validated coordinates (no free user text);
    #    path(s) + instants bound.
    span_sql = (
        f'SELECT {_IST_DATE_EXPR} AS d, arg_max(close, "timestamp") AS last_close '  # noqa: S608  # nosec B608 - _IST_DATE_EXPR is a hardcoded constant; path + instants bound
        "FROM read_parquet(?, union_by_name=true) "
        'WHERE "timestamp" >= ? AND "timestamp" < ? GROUP BY d'
    )
    span = _execute_parquet_resilient(
        conn,
        base,
        span_sql,
        [lo_instant, hi_instant],
        source=skip_source,
        skip_sink=skip_sink,
        files=span_files,
    )
    last_close: dict[date, Decimal] = {day: _to_decimal(close) for day, close in span}
    # 2) the single bar immediately BEFORE lo — the predecessor of the earliest
    #    page date. On the pruned arm this is ONE preceding file (manifest-exact),
    #    stepping back file-by-file on the grammar arm until a row lands; the
    #    legacy arm keeps the whole-glob ORDER BY DESC LIMIT 1 (which reads ~one
    #    row-group, not the whole past).
    pred_sql = (
        f"SELECT {_IST_DATE_EXPR} AS d, close "  # noqa: S608  # nosec B608 - _IST_DATE_EXPR is a hardcoded constant; path + instant bound
        "FROM read_parquet(?, union_by_name=true) "
        'WHERE "timestamp" < ? ORDER BY "timestamp" DESC LIMIT 1'
    )
    if windows is None:
        pred_rows = _execute_parquet_resilient(
            conn,
            base,
            pred_sql,
            [lo_instant],
            source=skip_source,
            skip_sink=skip_sink,
        )
    else:
        pred_rows = []
        for pred_file in _predecessor_files(windows, lo_instant):
            pred_rows = _execute_parquet_resilient(
                conn,
                base,
                pred_sql,
                [lo_instant],
                source=skip_source,
                skip_sink=skip_sink,
                files=[pred_file],
            )
            if pred_rows or skip_sink:
                # A row landed — or a file was skipped, which voids adjacency
                # below anyway; either way the step-back stops.
                break
    if skip_sink:
        # A corrupt file was skipped → adjacency is no longer guaranteed → honest blank.
        return {}
    if pred_rows:
        pred = pred_rows[0]
        last_close[pred[0]] = _to_decimal(pred[1])
    return last_close


def _intraday_prev_close_by_page_date(
    conn: Any,
    lake_root: Path,
    *,
    exchange: str,
    segment: str,
    symbol: str,
    page_dates: set[date],
) -> dict[date, Decimal]:
    """Prior-day last-1-minute close for EXACTLY a page's IST dates — page-bounded.

    The flat-latency replacement for ``_prev_trading_close(
    _intraday_last_close_by_date(...))`` on the ``/db`` intraday candle pager
    (DEC-DB-UNIVERSE-O1-LATENCY-001): a thin :func:`_prev_trading_close` over the
    page-bounded :func:`_intraday_last_close_with_predecessor` scan. Byte-identical
    to the prior full-partition scan for every page date; ``{}`` (every derived
    cell blank) when the page is empty / the symbol has no 1-minute partition / a
    corrupt file was skipped.
    """
    return _prev_trading_close(
        _intraday_last_close_with_predecessor(
            conn,
            lake_root,
            exchange=exchange,
            segment=segment,
            symbol=symbol,
            page_dates=page_dates,
        )
    )


def _daily_close_by_date(
    conn: Any, lake_root: Path, *, exchange: str, segment: str, symbol: str
) -> dict[date, Decimal]:
    """Read the symbol's 1-DAY official close per IST date → ``{ist_date: close}``.

    The same-basis baseline for the ``/db`` **1-day** view (and the NSE pre-open
    report's "previous close") — the prior 1-day close, NOT the 1-minute close
    (DEC-DB-CANDLE-PERINTERVAL-001), so a demerger/split-adjusted daily series
    stays self-consistent. Reads ``<lake>/bars/<exch>/<seg>/<symbol>/1day/**``
    via a bound ``read_parquet`` glob; keyed by the bar's IST date (a 1-day bar
    may be stamped at IST midnight or a session instant, so :func:`_bar_ist_date`
    — never a UTC ``CAST`` — is the trading day). ``{}`` when the 1-day partition
    is absent.
    """
    _validate_candle_coords(exchange, segment, symbol, _DAILY_INTERVAL)
    base = lake_root / "bars" / exchange / segment / symbol / _DAILY_INTERVAL
    if not (base.is_dir() and any(base.rglob("*.parquet"))):
        return {}
    # ADJACENCY GUARD (DEC-DB-CORRUPT-SKIP-001 BLOCKER fix): this map feeds
    # `_prev_trading_close`, which maps each day to its IMMEDIATE predecessor by walking
    # the sorted dates. If a corrupt 1-day file is skipped, the walk would BRIDGE across
    # the missing day(s) and baseline a %-change on a NON-ADJACENT earlier close — a
    # confidently-WRONG number. So on ANY skip we return {} → honest BLANK %-change for
    # every bar, never a fabricated one (Golden Rule #1). A clean read is byte-unchanged.
    skip_sink: list[bool] = []
    rows = _execute_parquet_resilient(
        conn,
        base,
        'SELECT "timestamp", close FROM read_parquet(?, union_by_name=true)',
        [],
        source=f"candle-dayclose:{exchange}/{segment}/{symbol}",
        skip_sink=skip_sink,
    )
    if skip_sink:
        return {}
    return {_bar_ist_date(ts): _to_decimal(close) for ts, close in rows}


def _existing_month_files_in_span(base: Path, start: date, end: date) -> list[str]:
    """The EXISTING ``<base>/<yyyy>/<mm>.parquet`` files that can hold a bar whose
    IST date falls in the INCLUSIVE ``[start, end]`` span — pure enumeration.

    DEC-DB-LATENCY-HARDENING-001: the file-list bound for the span-scoped readers.
    The candidate months follow the ±1-calendar-day pad law of
    ``spot_daily_resample.ist_span_partition_months`` (a bar's UTC partition month
    can differ from its IST date's month by at most the 05:30 offset — the pad can
    only ever ADD the immediately-adjacent month, never drop a matchable file),
    saturating at the ``date`` domain edges so a degenerate 0001/9999 span simply
    keeps everything (never an ``OverflowError``). Enumerated from the EXISTING
    tree — honestly a SECOND full rglob BESIDE the readers' own ``any(...)``
    presence-guard listing (immaterial next to the O(all-months) Parquet
    footer opens it removes, but a real added directory walk, stated) — each
    rel parsed by the SAME ASCII month grammar the stats
    manifest pins (:func:`~brutex.data.lake_stats_manifest.parse_rel_month`,
    ``KIND_BARS``); a NON-grammar file (a stray out-of-band parquet whose month is
    unknowable) is conservatively KEPT so the bounded read can never exclude a file
    the whole-glob read would have seen (byte-parity, Golden Rule #1). O(existing
    files) listing per call — this bounds the DuckDB FOOTER OPENS per statement to
    O(span months), NOT the directory listing (stated, never O(1)).
    """
    lo = (start - timedelta(days=1)) if start > date.min else start
    hi = (end + timedelta(days=1)) if end < date.max else end
    lo_key, hi_key = (lo.year, lo.month), (hi.year, hi.month)
    keep: list[str] = []
    for path in sorted(base.rglob("*.parquet")):
        parsed = parse_rel_month(KIND_BARS, f"{path.parent.name}/{path.name}")
        if parsed is None or lo_key <= parsed <= hi_key:
            keep.append(path.as_posix())
    return keep


def _daily_open_by_date(
    conn: Any,
    lake_root: Path,
    *,
    exchange: str,
    segment: str,
    symbol: str,
    span: tuple[date, date] | None = None,
) -> dict[date, Decimal]:
    """Read the symbol's 1-DAY official open per IST date → ``{ist_date: open}``.

    The day's official opening price — on NSE the pre-open auction's indicative
    equilibrium price (IEP) *becomes* the day's 1-day open — used as the IEP for
    the pre-open report (DEC-PREOPEN-REPORT-1DAY-001), on the SAME 1-day basis as
    the report's previous close so the two never mix price scales. Reads the
    1-day partition keyed by :func:`_bar_ist_date`; ``{}`` when absent.

    *span* (DEC-DB-LATENCY-HARDENING-001): the report's own INCLUSIVE
    ``[start, end]`` IST date range. When given, the read binds ONLY the
    existing month files that can hold an in-span bar
    (:func:`_existing_month_files_in_span` — O(span months) DuckDB footer opens
    per click instead of O(all stored months)); the consumer
    (``build_preopen_rows``) uses ONLY in-span days of this map, so the bounded
    map is byte-identical for every consumed key (parity-pinned). ``None`` (the
    default) keeps the whole-partition read byte-identical.
    """
    _validate_candle_coords(exchange, segment, symbol, _DAILY_INTERVAL)
    base = lake_root / "bars" / exchange / segment / symbol / _DAILY_INTERVAL
    if not (base.is_dir() and any(base.rglob("*.parquet"))):
        return {}
    # Resilient read (DEC-DB-CORRUPT-SKIP-001): a corrupt 1-day Parquet degrades the
    # pre-open IEP baseline to its readable subset, never a 500.
    rows = _execute_parquet_resilient(
        conn,
        base,
        'SELECT "timestamp", open FROM read_parquet(?, union_by_name=true)',
        [],
        source=f"candle-dayopen:{exchange}/{segment}/{symbol}",
        files=None if span is None else _existing_month_files_in_span(base, span[0], span[1]),
    )
    return {_bar_ist_date(ts): _to_decimal(open_) for ts, open_ in rows}


def _intraday_open_by_date(
    conn: Any,
    lake_root: Path,
    *,
    exchange: str,
    segment: str,
    symbol: str,
    span: tuple[date, date] | None = None,
) -> dict[date, Decimal]:
    """Read the symbol's 09:15 IST session-open 1-minute OPEN per day → ``{ist_date: open}``.

    The **minute-basis IEP** for the pre-open report (DEC-PREOPEN-MINUTE-BASIS-001):
    the day's FIRST 1-minute bar IS the 09:15:00 IST session open (the intraday
    partition is session-filtered 09:15–15:29 IST), so ``arg_min(open, timestamp)``
    grouped by the bar DATE is the 09:15 open — the true pre-open opening price the
    report header names, on the SAME 1-minute basis as the minute prev-close (the
    prior day's last 1-minute close, :func:`_intraday_last_close_by_date` +
    :func:`_prev_trading_close`). Mirrors :func:`_intraday_last_close_by_date`: a
    bound ``read_parquet`` glob over validated coordinates, ONE columnar pass
    (~one row per trading day → O(1) per-day lookup, CLAUDE.md §5); grouped by the
    bar's IST date via the COMMON session-tz-independent :data:`_IST_DATE_EXPR`
    (NOT a bare ``CAST(timestamp AS DATE)``, which resolves in the ambient DuckDB
    session ``TimeZone`` and would roll a session bar back one calendar day on a
    negative-offset host — DEC-SPOT-SESSION-DAYS-IST-TZ-001), matching
    :func:`_bar_ist_date`. ``{}`` when the symbol has no 1-minute partition.

    *span* (DEC-DB-LATENCY-HARDENING-001): the report's own INCLUSIVE
    ``[start, end]`` IST date range — the exact bound-law + parity contract of
    :func:`_daily_open_by_date` (the profiling evidence measured the minute
    basis binding EVERY 1-minute month file of EVERY symbol — 616 files per
    export click at 103 symbols — where a 3-week report needs ~1–2 per symbol).
    ``None`` (the default) keeps the whole-partition read byte-identical.
    """
    _validate_candle_coords(exchange, segment, symbol, _INTRADAY_BASE_INTERVAL)
    base = lake_root / "bars" / exchange / segment / symbol / _INTRADAY_BASE_INTERVAL
    if not (base.is_dir() and any(base.rglob("*.parquet"))):
        return {}
    # glob built from validated coordinates (no free user text); path is a bound
    # parameter. arg_min(open, timestamp) = the open of the day's FIRST (09:15) bar.
    # Resilient read (DEC-DB-CORRUPT-SKIP-001): a corrupt 1-minute Parquet degrades the
    # minute-basis IEP to its readable subset, never a 500.
    rows = _execute_parquet_resilient(
        conn,
        base,
        f'SELECT {_IST_DATE_EXPR} AS d, arg_min(open, "timestamp") AS first_open '  # noqa: S608  # nosec B608 - _IST_DATE_EXPR is a hardcoded constant; path bound
        "FROM read_parquet(?, union_by_name=true) GROUP BY d",
        [],
        source=f"candle-dayopen-min:{exchange}/{segment}/{symbol}",
        files=None if span is None else _existing_month_files_in_span(base, span[0], span[1]),
    )
    return {day: _to_decimal(first_open) for day, first_open in rows}


def _spot_derived_rows(
    raw_rows: list[tuple[Any, ...]],
    out_columns: list[str],
    prev_close_by_date: dict[date, Decimal],
    prev_close_date_by_date: dict[date, date] | None = None,
    *,
    exchange: str | None = None,
    interval: str = _INTRADAY_BASE_INTERVAL,
) -> list[tuple[Any, Any, Any]]:
    """Compute the per-bar ``(change_pct, open_gap_pct, prev_close_contiguous)`` triple.

    For each bar (DEC-DB-CANDLE-DERIVED-001), baselined on *prev_close_by_date* —
    the prior trading day's close **on the shown interval's own basis**
    (DEC-DB-CANDLE-PERINTERVAL-001): the prior 1-day close for a ``"1day"`` view,
    the prior day's last 1-minute close for an intraday view
    (DEC-SPOT-PREVCLOSE-1MIN-001). The two are never mixed, so an
    adjustment-mismatched symbol (e.g. TMPV) stays precise:

    * ``change_pct = (close − prev_close) / prev_close × 100``;
    * ``open_gap_pct = (open − prev_close) / prev_close × 100`` — for a ``"1day"``
      view computed on EVERY bar (the open-vs-prior-close gap); for an intraday
      view computed ONLY for the day's 09:15:00 IST market-open bar (the overnight
      gap), ``None`` on every other intraday bar.
    * ``prev_close_contiguous`` — the honesty verdict (DEC-DB-PREVCLOSE-CONTIGUITY-MARKER-001,
      the ``/db`` twin of the ``/movers`` #1539 " *" marker): ``True`` when the
      baseline came from the NSE-calendar's IMMEDIATELY-preceding session,
      ``False`` when a real session was BRIDGED (the baseline predecessor DATE in
      *prev_close_date_by_date* is NOT :func:`previous_trading_session` for the
      bar's date — a genuine lake hole, so the served %-change is confidently wrong
      and is MARKED, never silently changed — the #1539 doctrine), and ``None`` when
      there is no baseline, the caller passed no provenance map, or the calendar
      ABSTAINS past its coverage floor (never a fabricated verdict, Golden Rule #1).

    The ``change_pct`` / ``open_gap_pct`` reuse the COMMON
    :func:`brutex.data.spot_features.pct_change` primitive (CLAUDE.md §11) and stay
    full precision — statistical metrics, never money-rounded (CLAUDE.md §4); the
    2dp is a *display*-only concern in ``db.html``. A bar whose IST date is absent
    from *prev_close_by_date* (the first trading day, or a symbol with no baseline
    lake) gets ``(None, None, None)``; a zero previous close yields ``None`` (no
    divide-by-zero, via ``pct_change``). *prev_close_date_by_date* / *exchange* are
    the provenance inputs for the contiguity verdict; omitted (the pre-marker
    signature) → contiguity is uniformly ``None`` (byte-compatible with callers
    that do not request the marker).
    """
    ts_idx = out_columns.index("timestamp") if "timestamp" in out_columns else -1
    open_idx = out_columns.index("open") if "open" in out_columns else -1
    close_idx = out_columns.index("close") if "close" in out_columns else -1
    is_daily = interval == _DAILY_INTERVAL
    # Per-page memo over the COMMON prev-session resolver (mirrors the /movers
    # per-build memo, movers_snapshot.make_expected_session_resolver) so repeated
    # bars on the same IST date resolve the calendar once.
    session_memo: dict[date, date | None] = {}

    def _expected_prev_session(ist_date: date) -> date | None:
        if ist_date not in session_memo:
            session_memo[ist_date] = previous_trading_session(ist_date, exchange=exchange)
        return session_memo[ist_date]

    derived: list[tuple[Any, Any, Any]] = []
    for row in raw_rows:
        change: Decimal | None = None
        gap: Decimal | None = None
        contiguous: bool | None = None
        ts = row[ts_idx] if ts_idx >= 0 else None
        if isinstance(ts, datetime):
            ist_date = _bar_ist_date(ts)
            prev = prev_close_by_date.get(ist_date)
            if prev is not None:
                if close_idx >= 0:
                    change = pct_change(_to_decimal(row[close_idx]), prev)
                # 1-day: the open-vs-prior-close gap on every bar; intraday: only
                # the 09:15 IST session-open bar (DEC-DB-CANDLE-PERINTERVAL-001).
                if open_idx >= 0 and (is_daily or _bar_ist_time(ts) == _SESSION_OPEN_IST):
                    gap = pct_change(_to_decimal(row[open_idx]), prev)
                # Contiguity verdict — only when the caller supplied the predecessor
                # provenance map AND the calendar can name the true prior session
                # (else honest abstain: None, never a guessed marker).
                if prev_close_date_by_date is not None:
                    expected = _expected_prev_session(ist_date)
                    if expected is not None:
                        contiguous = prev_close_date_by_date.get(ist_date) == expected
        derived.append((_json_safe(change), _json_safe(gap), contiguous))
    return derived


# The instant normaliser for cross-series joining is the COMMON
# :func:`brutex.data.spot_join.utc_instant` (lifted per plan §15.4 /
# DEC-GREEKS-ENGINE-PR2B-001 so the /db moneyness join and the post-pull Greeks
# enrichment share ONE join-key contract — CLAUDE.md §11 COMMON).
_utc_instant = utc_instant


def _underlying_spot_close_by_ts(
    conn: Any,
    lake_root: Path,
    *,
    exchange: str,
    underlying: str,
    interval: str,
    ts_values: list[datetime],
    stock_drift_loader: Callable[[], StockDriftMap | None] | None = None,
) -> tuple[dict[datetime, Decimal], str | None]:
    """Read ``{bar_instant: underlying_spot_close}`` for an F&O contract's underlying.

    The per-bar spot needed to classify an option's moneyness (DEC-FNO-MONEYNESS-001).
    Probes the underlying spot under :data:`_SPOT_PROBE_SEGMENTS` (``INDEX`` then
    ``CASH`` — an index underlying rides INDEX, a stock rides CASH) at the SAME
    *interval* as the option bars, so the two series align bar-for-bar by timestamp. The
    read is BOUNDED to the page's ``[min, max]`` instant (``BETWEEN``) AND — the
    DEC-DB-BASELINE-MANIFEST-ROUTE-001 pruning — to the month FILE(S) that can hold
    those instants (:func:`_bars_file_windows`; ``None`` ⇒ the byte-identical legacy
    whole-glob bind), so it never materialises more than the page's worth of spot
    bars NOR opens every stored year's footer per page — O(page months) file opens
    once, then O(1) per-bar lookup downstream (CLAUDE.md §5).

    Returns ``(spots, joined_interval)`` — *joined_interval* names the spot interval
    that actually served the join (the page *interval*, or ``"1minute"`` on the
    fallback below), ``None`` when nothing joined. Returns ``({}, None)`` (→ every bar
    classifies UNKNOWN, never raising) when *ts_values* is empty, the underlying
    coordinate is illegal, or the underlying spot is absent everywhere (Golden Rule #1
    — an honest UNKNOWN, never a fabricated spot).

    When the exact *underlying* probe comes back EMPTY (and only then — zero extra
    I/O and NO log on the hot path), the COMMON spot-key drift-recovery ladder
    (:func:`~brutex.data.lake_keys.spot_recovery_candidates` — index aliases BOTH
    directions + the ``BRUTEX_FNO_INDEX_ALIASES`` env override + the
    ``"<EXCH>-<underlying>"`` #911 form + the ``lake_keys`` #911 rungs + the
    evidence-grounded STOCK drift rung supplied by *stock_drift_loader*) is walked in
    order, returning the FIRST candidate with any joined spot bar and emitting ONE
    ``db_view.spot_key_recovered`` info naming both keys
    (DEC-DB-MONEYNESS-KEY-RECOVER-001 — pre-fix, a verbose-symbol index such as
    ``MIDCPNIFTY`` over a ``NIFTYMIDSELECT``-keyed spot dir read moneyness UNKNOWN on
    every bar even though the spot data existed on disk). Ladder exhaustion returns
    the honest empty map — never a fabricated spot.

    *stock_drift_loader* (DEC-DB-MONEYNESS-STOCK-EVIDENCE-001 — the /db moneyness
    twin of the Greeks ladder's DEC-GREEKS-SPOTJOIN-STOCK-001 stock arm) lazily
    supplies the master-verified :data:`~brutex.data.lake_keys.StockDriftMap`; it is
    invoked AT MOST ONCE per call and ONLY on the already-cold exhausted-primary path
    (the exact-key hot path pays zero extra I/O), so a drifted stock spot dir
    (``BAJAJ-AUTO`` vs the chain's ``BAJAJAUTO``) now classifies moneyness instead of
    reading ``"?"`` on every bar. ``None`` (the default) or a loader returning
    ``None``/empty is byte-identical to the pre-stock ladder (the pinned
    :func:`spot_recovery_candidates` prefix invariant); the one recovery log gains
    the additive ``rung`` field (``"stock_drift"`` / ``"alias_ladder"``), attributed
    exactly as the Greeks twin attributes it (indices past the 2-arg ladder length
    are uniquely stock-sourced).

    **1-minute fallback on a partially-prebuilt lake (DEC-DB-FNO-DISPLAY-TRUTH-001
    item 9):** when the whole walk at the display *interval* joins nothing AND
    ``interval != "1minute"``, the SAME key walk re-runs at
    :data:`_INTRADAY_BASE_INTERVAL` — the always-present base feed, exactly mirroring
    the %CHNG baseline convention (DEC-SPOT-PREVCLOSE-1MIN-001, which ALWAYS reads
    ``1minute`` regardless of the viewed timeframe). The fallback joins ONLY the
    page's own bar instants (a bound-parameter ``IN`` list, ≤ page size — O(page);
    a ``BETWEEN`` at 1minute would materialise every minute in the page's wall
    span). Semantics, honestly disclosed: it serves the 1-minute close AT the bar's
    labelled instant (the bucket-OPEN minute), whereas a prebuilt display-interval
    partition serves the bucket's resampled close (its LAST minute) — both real
    stored data, the drift surfaced via *joined_interval* provenance (the page
    stamps ``spot_join_interval``), never a fabricated value. Honest limit: a
    ``1day`` page's bar instants are midnight-IST — no 1-minute bar exists at those
    instants, so a 1day page cannot fall back (stays ``"?"``). One
    ``db_view.spot_tf_fallback`` info per page marks the fallback arm.
    """
    if not ts_values:
        return {}, None
    min_ts, max_ts = min(ts_values), max(ts_values)

    def _probe(key: str, probe_interval: str) -> dict[datetime, Decimal]:
        for segment in _SPOT_PROBE_SEGMENTS:
            try:
                _validate_candle_coords(exchange, segment, key, probe_interval)
            except ValueError:
                return {}  # a key that is not a legal coordinate → no spot under it.
            base = lake_root / "bars" / exchange / segment / key / probe_interval
            if not (base.is_dir() and any(base.rglob("*.parquet"))):
                continue
            # glob/file list built from validated coordinates (no free user text);
            # path(s) + every bound (BETWEEN pair / IN-list instants) are bound
            # parameters (same SQL/path-injection guard as query_candles). A NULL
            # close is filtered in SQL so a stray null spot bar degrades that
            # instant to UNKNOWN (the no-spot bucket) rather than raising in
            # `_to_decimal(None)` — honouring the "never raises / honest UNKNOWN"
            # contract. Resilient read (DEC-DB-CORRUPT-SKIP-001): a corrupt
            # underlying-spot Parquet degrades the join to its readable subset
            # (the affected option bars classify UNKNOWN), never a 500.
            # File-pruned to the page's instant span
            # (DEC-DB-BASELINE-MANIFEST-ROUTE-001): the BETWEEN is inclusive (and
            # every IN-list instant lies within [min_ts, max_ts]), so the
            # half-open window intersect uses max_ts + 1µs.
            windows = _bars_file_windows(
                lake_root,
                base,
                exchange=exchange,
                segment=segment,
                symbol=key,
                interval=probe_interval,
            )
            span_files = (
                _files_intersecting(
                    windows,
                    _utc_instant(min_ts),
                    _utc_instant(max_ts) + timedelta(microseconds=1),
                )
                if windows is not None
                else None
            )
            if probe_interval == interval:
                where_sql = 'WHERE "timestamp" BETWEEN ? AND ? AND close IS NOT NULL'
                params: list[Any] = [min_ts, max_ts]
            else:
                # The 1minute FALLBACK joins only the page's own bar instants — a
                # bound-parameter IN list keeps the read O(page) instead of an
                # O(minutes-in-span) BETWEEN over the base feed.
                placeholders = ", ".join("?" for _ in ts_values)
                where_sql = f'WHERE "timestamp" IN ({placeholders}) AND close IS NOT NULL'
                params = list(ts_values)
            # S608 suppression justification: the two WHERE shapes are trusted module
            # constants (a literal BETWEEN, or "?" placeholders only); the path +
            # every value are bound parameters — no free text reaches the SQL.
            sql = (
                'SELECT "timestamp", close FROM read_parquet(?, union_by_name=true) '  # noqa: S608  # nosec B608
                + where_sql
            )
            rows = _execute_parquet_resilient(
                conn,
                base,
                sql,
                params,
                source=f"candle-spotjoin:{exchange}/{segment}/{key}/{probe_interval}",
                files=span_files,
            )
            return {_utc_instant(ts): _to_decimal(close) for ts, close in rows}
        return {}

    # The drift map is resolved lazily AT MOST ONCE per call (both _walk passes
    # share the memo), and only on the recovery path — the exact-key hot path
    # never touches the loader (DEC-DB-MONEYNESS-STOCK-EVIDENCE-001).
    drift_memo: list[StockDriftMap | None] = []

    def _drift() -> StockDriftMap | None:
        if not drift_memo:
            drift_memo.append(stock_drift_loader() if stock_drift_loader is not None else None)
        return drift_memo[0]

    def _walk(probe_interval: str) -> dict[datetime, Decimal]:
        spots = _probe(underlying, probe_interval)
        if spots:
            return spots
        stock_drift = _drift()
        candidates = spot_recovery_candidates(underlying, exchange, stock_drift=stock_drift)
        alias_count = len(candidates)
        if stock_drift:
            # The extra 2-arg call runs only on the already-cold exhausted-primary
            # path with a truthy map — pure string ops, O(1)-bounded (the exact
            # rung-attribution mechanism of the Greeks twin,
            # ``greeks_enrich.read_spot_join_with_recovery``).
            alias_count = len(spot_recovery_candidates(underlying, exchange))
        for idx, candidate in enumerate(candidates):
            recovered = _probe(candidate, probe_interval)
            if recovered:
                _LOG.info(
                    "db_view.spot_key_recovered",
                    underlying=underlying,
                    recovered_key=candidate,
                    exchange=exchange,
                    interval=probe_interval,
                    rung="stock_drift" if idx >= alias_count else "alias_ladder",
                )
                return recovered
        return spots

    spots = _walk(interval)
    if spots:
        return spots, interval
    if interval != _INTRADAY_BASE_INTERVAL:
        fallback = _walk(_INTRADAY_BASE_INTERVAL)
        if fallback:
            _LOG.info(
                "db_view.spot_tf_fallback",
                underlying=underlying,
                exchange=exchange,
                interval=interval,
                fallback=_INTRADAY_BASE_INTERVAL,
            )
            return fallback, _INTRADAY_BASE_INTERVAL
    return {}, None


def _fno_strike_step(
    lake_root: Path,
    *,
    exchange: str,
    symbol: str,
    registry_strikes: Sequence[Decimal] | None = None,
) -> StrikeStepResult:
    """Derive the DYNAMIC strike-grid step for an option contract's ``(underlying, expiry)``.

    Globs the SIBLING option partitions sharing this contract's
    ``<exch>-<underlying>-<expiry>`` prefix (``symbol`` minus its trailing
    ``-<strike>-<CE|PE>``), decodes each strike via
    :func:`~brutex.data.fno_symbol.parse_fno_symbol`, and resolves the smallest
    positive gap (:func:`~brutex.data.moneyness.resolve_strike_step`) — preferring
    *registry_strikes*, the AUTHORITATIVE vendor chain for this ``(underlying,
    expiry)`` (DEC-MONEYNESS-REGISTRY-STEP-001), because the on-disk sibling set is
    SPARSE (only pulled strikes) and can overestimate the step. DYNAMIC per contract
    set so it works for EVERY F&O underlying — all ~212 stocks + the indices — never
    a hardcoded NSE grid (Golden Rule #1). ``step=None`` when neither ladder has two
    distinct strikes (the moneyness bucket still resolves; only the ATM±N offset is
    then ``None``); the ``confidence`` labels which ladder produced the step so the
    page never silently presents a possibly-overestimated grid.

    O(siblings) once per query (a directory glob scoped to ONE expiry's strike ladder,
    not the whole F&O tree); the per-bar offset that consumes the step is O(1).
    """
    base = lake_root / "bars" / exchange / FNO_CANDLE_LAKE_SEGMENT
    prefix = symbol.rsplit("-", 2)[0]  # strip "-<strike>-<CE|PE>" → the (underlying,expiry) key
    observed = (
        [
            parts.strike
            for sibling in base.glob(prefix + "-*")
            if sibling.is_dir() and (parts := parse_fno_symbol(sibling.name)) is not None
            if parts.option_type is not None  # options only (a FUT sibling has no strike)
        ]
        if base.is_dir()
        else []
    )
    return resolve_strike_step(observed, authoritative_strikes=registry_strikes)


def fno_strike_chain_key(exchange: str, underlying: str, expiry: date) -> str:
    """Build the stable cache key for one registry strike chain (display step source).

    The AUTHORITATIVE chain (DEC-MONEYNESS-REGISTRY-STEP-001) is a property of the
    ``(exchange, underlying, expiry)`` alone — invariant to paging / sort / column
    subsetting / date bounds — so every page of the same contract (and every sibling
    strike's page) shares ONE cached chain per write-generation; a metadata pull bumps
    the generation → re-read, never stale (Golden Rule #1). The ``strikechain|``
    prefix namespaces the key away from every other key sharing the injected cache
    instance: a spot count key opens with a validated exchange token (``NSE`` /
    ``BSE``), an option count key with ``opt|``, the spans with ``span|`` /
    ``optspan|`` — collision-free by construction (see :func:`candle_span_key`).
    """
    return f"strikechain|{exchange}|{underlying}|{expiry.isoformat()}"


def _registry_strike_chain(
    registry_path: Path | None,
    *,
    exchange: str,
    underlying: str,
    expiry: date | None,
    count_cache: CandleCountCache | None,
    generation: int,
) -> tuple[Decimal, ...] | None:
    """The AUTHORITATIVE registry strike chain for ONE ``(underlying, expiry)``, or ``None``.

    Thin memoised read over :func:`~brutex.dashboard.registry_strikes.
    load_registry_strike_chain` — exactly one chain (the ``/db`` page needs
    only its own contract's ladder — O(one chain), never the whole registry). ``None``
    — the graceful observed-ladder fallback — when no *registry_path* is wired (the
    pure-function default), the *expiry* did not decode, or the registry is
    absent/empty/unreadable. Memoised behind the SAME generation-stamped cache the
    page total uses (:func:`fno_strike_chain_key`) so a prev/next paging click never
    re-opens the registry; no injected cache runs the read inline.
    """
    if registry_path is None or expiry is None:
        return None

    def _load() -> tuple[Decimal, ...] | None:
        return load_registry_strike_chain(
            registry_path, exchange=exchange, underlying=underlying, expiry=expiry
        )

    if count_cache is None:
        return _load()
    return count_cache.get_or_compute(
        fno_strike_chain_key(exchange, underlying, expiry),
        generation=generation,
        compute=_load,
    )


def _option_intrinsic(option_type: str, strike: Decimal, spot: Decimal) -> Decimal:
    """Intrinsic value of an option at *spot*: ``max(0, spot−strike)`` CE / ``max(0, strike−spot)`` PE.

    Pure + O(1); returns ``Decimal(0)`` for an out-of-the-money option (never negative). The
    caller (:func:`_fno_derived_rows`) only reaches here with *strike* and *spot* both present
    (the early-return guard narrows ``strike``; the per-row ``spot is None`` check precedes the
    call), so no ``None`` handling is needed here.
    """
    diff = (spot - strike) if option_type == "CE" else (strike - spot)
    return diff if diff > Decimal(0) else Decimal(0)


def _fno_derived_rows(
    conn: Any,
    lake_root: Path,
    *,
    exchange: str,
    symbol: str,
    interval: str,
    raw_rows: list[tuple[Any, ...]],
    out_columns: list[str],
    registry_path: Path | None = None,
    count_cache: CandleCountCache | None = None,
    generation: int = 0,
    stock_drift_loader: Callable[[], StockDriftMap | None] | None = None,
) -> tuple[
    list[tuple[str, str | None, str | None, str | None, int | None, str]],
    StepConfidence | None,
    str | None,
]:
    """Compute the per-bar F&O derived cells: ``(moneyness, spot, intrinsic, time_value, days_to_expiry, greeks_status)``.

    Matches :data:`FNO_DERIVED_COLUMNS`. The combined ``moneyness`` label (bucket + signed
    strike-depth, ``"ITM -1"`` / ``"OTM +10"`` / ``"ATM"``) is classified against the
    underlying spot at THAT bar's instant (DEC-FNO-MONEYNESS-001) — time-varying, so a
    fixed-strike contract reads ``OTM +N`` then ``ITM -N`` as the spot moves. The VALUE
    columns (DEC-DB-FNO-COLUMNS-001) ride the SAME single per-bar spot join (no extra read):

    * ``spot`` — the underlying spot close at the bar (2-dp money string), ``None`` if absent;
    * ``intrinsic`` — :func:`_option_intrinsic` (2-dp), ``None`` when no spot;
    * ``time_value`` — ``close − intrinsic`` (2-dp), ``None`` when no spot;
    * ``days_to_expiry`` — pure ``(expiry − bar IST date).days`` (no I/O), resolvable for any
      decodable contract (option OR future), ``None`` only when undecodable / no timestamp;
    * ``greeks_status`` — the stored ``greeks_provenance_id`` stamp's packed status decoded
      into words (DEC-DB-FNO-DISPLAY-TRUTH-001 item 3) via the ONE COMMON
      :func:`brutex.data.greeks_provenance.greeks_status_label` — no extra I/O (the pid rides
      the page's own SELECT); a stamp-less bar (legacy 7-column partition, derived-TF page —
      the overlay lives on ``1minute`` — or a ``greeks_provenance_id``-less column
      projection) honestly reads ``"not enriched"``, a future ``"—"``.

    A FUTURE (or an undecodable symbol) reads ``moneyness="—"`` with ``None`` value columns
    and no spot read attempted — but still gets ``days_to_expiry`` when its expiry decodes; an
    option whose underlying spot is absent reads ``moneyness="?"`` with ``None`` values. Never
    raises — a per-bar browse never crashes on one contract whose spot was not pulled
    (Golden Rule #1).

    The per-bar spot join keys on the bar ``timestamp``; a deliberately ``timestamp``-less
    column projection therefore reads ``UNKNOWN``/``None`` on every bar (honest — the join key
    is absent — never a fabricated value), not a crash.

    Returns ``(rows, step_confidence, spot_join_interval)`` — the second element labels
    which ladder produced the ATM±N strike-grid step (DEC-MONEYNESS-REGISTRY-STEP-001):
    :attr:`~brutex.data.moneyness.StepConfidence.REGISTRY` when the AUTHORITATIVE
    vendor chain (read from *registry_path* via the generation-memoised
    :func:`_registry_strike_chain`) supplied it,
    :attr:`~brutex.data.moneyness.StepConfidence.OBSERVED` when only the sparse
    lake-observed sibling ladder did (the page then carries an honest low-confidence
    marker — the observed min gap can overestimate the true base step), and ``None``
    when no step was derivable at all (or the page is a future/undecodable contract).
    The third element is the spot interval that actually served the per-bar join
    (DEC-DB-FNO-DISPLAY-TRUTH-001 item 9 — ``"1minute"`` marks the fallback join on a
    partially-prebuilt lake; ``None`` when nothing joined / no join was attempted).
    """
    parts = parse_fno_symbol(symbol)
    option_type = parts.option_type if parts is not None else None
    strike = parts.strike if parts is not None else None
    underlying = parts.underlying if parts is not None else None
    expiry = parts.expiry if parts is not None else None
    ts_idx = out_columns.index("timestamp") if "timestamp" in out_columns else -1
    close_idx = out_columns.index("close") if "close" in out_columns else -1
    pid_idx = (
        out_columns.index("greeks_provenance_id") if "greeks_provenance_id" in out_columns else -1
    )

    def _dte(ts: Any) -> int | None:
        """Calendar days from the bar's IST date to expiry — pure, ``None`` if unknown."""
        if expiry is None or not isinstance(ts, datetime):
            return None
        return (expiry - _bar_ist_date(ts)).days

    def _greeks_status(row: tuple[Any, ...]) -> str:
        """Decode the row's stored provenance stamp into the display words.

        A missing / non-int cell (legacy partition NULL, derived-TF page, a
        ``greeks_provenance_id``-less column projection) is an honest ``None`` stamp
        → ``"not enriched"`` for an option (mirrors the documented timestamp-less-
        projection behaviour above — never a fabricated status, never a raise).
        """
        pid = row[pid_idx] if pid_idx >= 0 else None
        # DuckDB fetchall() returns a native Python int for INTEGER-family columns
        # (NULL → None) — the driver contract this isinstance narrowing relies on.
        # bool is EXCLUDED explicitly (bool subclasses int, so a stray True would
        # otherwise decode as packed id 1 → a fabricated "ok"); DuckDB never yields
        # bool for an INTEGER column, but Golden Rule #1 forbids relying on that.
        packed = pid if isinstance(pid, int) and not isinstance(pid, bool) else None
        return greeks_status_label(packed, is_option=option_type is not None)

    # A future / undecodable contract (or an option with no decoded strike) has no
    # moneyness/value — "—" + None on every bar, no spot read; days_to_expiry still resolves
    # for a future whose expiry decoded. Narrowing on ``strike`` here lets the option branch
    # below treat it as a definite ``Decimal`` (an option always decodes a strike, else
    # ``parse_fno_symbol`` returns ``None`` → caught by ``underlying is None``).
    if option_type is None or underlying is None or strike is None:
        na_label = moneyness_view(option_type, strike, None, None).label
        return (
            [
                (
                    na_label,
                    None,
                    None,
                    None,
                    _dte(row[ts_idx] if ts_idx >= 0 else None),
                    _greeks_status(row),
                )
                for row in raw_rows
            ],
            None,
            None,
        )

    ts_values = [
        row[ts_idx] for row in raw_rows if ts_idx >= 0 and isinstance(row[ts_idx], datetime)
    ]
    # The AUTHORITATIVE registry chain for this (underlying, expiry), when a registry
    # is wired + readable — else None ⇒ the lake-observed sibling ladder resolves the
    # step with an honest OBSERVED (low-confidence) label
    # (DEC-MONEYNESS-REGISTRY-STEP-001; graceful, never raises, never fabricates).
    registry_strikes = _registry_strike_chain(
        registry_path,
        exchange=exchange,
        underlying=underlying,
        expiry=expiry,
        count_cache=count_cache,
        generation=generation,
    )
    resolved_step = _fno_strike_step(
        lake_root, exchange=exchange, symbol=symbol, registry_strikes=registry_strikes
    )
    step = resolved_step.step
    spot_by_ts, spot_join_interval = _underlying_spot_close_by_ts(
        conn,
        lake_root,
        exchange=exchange,
        underlying=underlying,
        interval=interval,
        ts_values=ts_values,
        # The evidence-grounded stock drift rung (DEC-DB-MONEYNESS-STOCK-EVIDENCE-001)
        # — the injected lazy loader is consulted only on the exhausted-primary
        # recovery path, so a drifted stock spot dir classifies moneyness exactly
        # as the Greeks ladder recovers its spot join; the healthy exact-key page
        # pays zero extra I/O.
        stock_drift_loader=stock_drift_loader,
    )
    rows: list[tuple[str, str | None, str | None, str | None, int | None, str]] = []
    for row in raw_rows:
        ts = row[ts_idx] if ts_idx >= 0 else None
        spot = spot_by_ts.get(_utc_instant(ts)) if isinstance(ts, datetime) else None
        view = moneyness_view(option_type, strike, spot, step)
        if spot is None:
            rows.append((view.label, None, None, None, _dte(ts), _greeks_status(row)))
            continue
        intrinsic = _option_intrinsic(option_type, strike, spot)
        close = row[close_idx] if close_idx >= 0 else None
        time_value = str(round_money(_to_decimal(close) - intrinsic)) if close is not None else None
        rows.append(
            (
                view.label,
                str(round_money(spot)),
                str(round_money(intrinsic)),
                time_value,
                _dte(ts),
                _greeks_status(row),
            )
        )
    return rows, resolved_step.confidence, spot_join_interval


def option_candle_count_key(series: str, start: str | None, end: str | None) -> str:
    """Build the stable cache key for an option-page **total** (DEC-LAKE-READ-O1-AUDIT-001).

    The expired-options twin of :func:`candle_count_key`: the total ``COUNT(*)``
    depends ONLY on the validated series segment + the inclusive date bounds — NOT
    on ``limit`` / ``offset`` / ``descending`` / the projected ``columns`` (paging,
    sort, and column subsetting never change how many rows match). So every page of
    the same ``(series, start, end)`` shares ONE cached total: a prev/next click is
    an O(1) hit instead of re-reading every Parquet footer in the series. ``|`` is
    rejected from a validated series segment (``_OPTION_SEGMENT_RE``) and cannot
    appear in an ISO date, so the join is unambiguous; ``None`` bounds render as an
    empty segment. The ``opt|`` prefix namespaces the key away from the spot
    :func:`candle_count_key` so the two may share one injected cache instance —
    collision-free because a spot key opens with the partition ``exchange`` token,
    which ``_validate_candle_coords`` restricts to ``VALID_EXCHANGES`` (``NSE`` /
    ``BSE``) before any key is built, so a spot key can never begin ``opt|``.
    """
    return f"opt|{series}|{start or ''}|{end or ''}"


def query_option_candles(
    conn: Any,
    lake_root: Path,
    *,
    series: str,
    start: str | None = None,
    end: str | None = None,
    columns: tuple[str, ...] | None = None,
    descending: bool = False,
    limit: int,
    offset: int = 0,
    count_cache: CandleCountCache | None = None,
    generation: int = 0,
    use_stats_manifest: bool = False,
    retry_sleep: Callable[[float], None] | None = None,
) -> dict[str, Any]:
    """Read one page of expired-option bars for a series (DEC-DB-QUERY-001).

    The options-lake analog of :func:`query_candles`: reads the Parquet lake
    directly via DuckDB ``read_parquet`` over the series glob
    ``<lake>/options/<series>/*.parquet`` (the layout
    :class:`~brutex.data.option_parquet_writer.OptionParquetWriter` writes) — no
    data duplication, the lake stays the single source of truth. *series* is the
    filesystem-safe segment from :func:`list_option_datasets` and is validated
    against :func:`_validate_option_segment` (NO free user text in the glob,
    same path/SQL-injection guard as the spot reader).

    HONEST complexity (Golden Rule #10 — this is NOT O(page) in series depth;
    DEC-DB-OPAGE-HONESTY-001): identical envelope to :func:`query_candles`. The
    page DATA query performs a FULL columnar scan of every matched series row —
    the keep-FIRST dedup ``QUALIFY row_number() OVER (PARTITION BY (side,
    timestamp) …) = 1`` window hash-partitions the whole matched set and the
    ``ORDER BY "timestamp"`` sorts it — BEFORE ``LIMIT``/``OFFSET`` slice the
    page. So the engine cost is **O(total matched series rows)** (plus one
    Parquet footer open per file); ``LIMIT`` bounds ONLY the Python
    materialisation and a deep ``OFFSET`` is O(row-groups up to the offset) on
    top. The page ``total`` is a ``COUNT(DISTINCT (side, timestamp))``, itself
    **O(rows)** on the first computation per write-generation and O(1) on a
    cached repeat (see *count_cache*).
    This envelope is the LEGACY arm's: the write-time per-file STATS
    manifest — the #867/#1025 named future work, since SHIPPED by
    DEC-LAKE-STATS-MANIFEST-001 (MIGRATION.md #1614) and wired into THIS
    function via *use_stats_manifest* — prunes a validated page's read to
    O(page-files) Parquet work at an O(D) per-request revalidation stat
    sweep (D = the series' month files), NOT literal O(page).

    Projects the 11 :data:`OPTION_CANDLE_COLUMNS` — the OHLCV+OI set PLUS the
    per-bar option stamps ``side`` / ``strike`` / ``spot`` / ``iv``. *start* /
    *end* are inclusive ISO ``YYYY-MM-DD`` bounds matched against the bar's UTC
    calendar date. Returns an empty page (never raises) when the series has no
    Parquet files yet. All three reads (total / page / stored-life span) route
    through the COMMON resilient probe-and-skip helpers
    (:func:`_execute_parquet_resilient` / :func:`_execute_candle_page_resilient`
    — DEC-ENGINE-MISC-PACK-002, the options twin of DEC-DB-CORRUPT-SKIP-001), so
    one corrupt Parquet file degrades to the readable subset instead of 500ing
    the whole tab.

    Parameters
    ----------
    conn
        A DuckDB connection.
    lake_root
        Top-level lake directory (parent of ``options/``).
    series
        The option-series segment; validated against the safe-segment grammar.
    start, end
        Inclusive ISO ``YYYY-MM-DD`` bounds on the bar timestamp, or ``None``.
    columns
        Option columns to project (subset of :data:`OPTION_CANDLE_COLUMNS`), or
        ``None`` for all. An empty tuple is rejected.
    descending, limit, offset
        Sort direction on ``timestamp`` + page bounds (clamped).
    count_cache, generation
        Optional generation-stamped cache for the page ``total``
        (DEC-LAKE-READ-O1-AUDIT-001) + the current write-generation it is keyed
        on. When supplied, the ``COUNT(DISTINCT (side, timestamp))`` total over the
        whole series glob — O(rows) on the first computation per write-generation,
        O(1) on a cached repeat — is memoised per ``(series, start, end)``
        (paging-invariant) so a prev/next click is an O(1) hit; a pull bumps
        *generation* → recompute (never a stale total).
        The SAME cache also memoises the default-range STORED-LIFE span under
        :func:`option_candle_span_key` (series-only key), so the applied-range chip
        (DEC-DB-UX-PACK-001 Feature 2) never re-runs its MIN/MAX scan on a paging
        click. The expired-options twin of the spot :func:`query_candles` cache.
    use_stats_manifest
        Opt into the per-file lake STATS manifest (DEC-LAKE-STATS-MANIFEST-001)
        — the expired-options twin of the :func:`query_candles` parameter:
        manifest-first total / span / file-list-pruned page after full
        per-request revalidation, ANY failure ⇒ the byte-identical legacy scan
        + best-effort reseed. The production route passes ``True``; the pure
        default ``False`` keeps direct callers byte-identical, and the
        ``BRUTEX_LAKE_STATS_MANIFEST=off`` kill switch forces legacy.
    retry_sleep
        Injectable sleeper for the torn-read retry-once guard — the
        expired-options twin of the :func:`query_candles` parameter
        (DEC-DB-CANDLES-TORN-READ-RETRY-001): this pager runs the SAME
        two-query COUNT + page shape over the series glob, so a live pull's
        atomic ``os.replace`` mid-request tears identically. Each read is
        retried ONCE after :data:`_DUCKDB_RETRY_DELAY_S` over a
        freshly-rebuilt glob/file list (see :func:`_retry_candle_read_once`);
        ``None`` (the default) sleeps for real. Same honest envelope: the
        race is narrowed, not eliminated, and when the page returned rows the
        returned ``total`` is raised to at least ``offset + len(rows)`` (an
        observed lower bound; an EMPTY page never adjusts the total — it
        proves nothing).

    Returns
    -------
    dict[str, Any]
        ``{"series", "interval", "columns", "rows", "total", "limit",
        "offset"}`` with every cell JSON-safe (``iv`` kept full-precision —
        it is a volatility metric, NOT money, mirroring the writer).

    Raises
    ------
    ValueError
        On an illegal series, an unknown column, an empty column tuple, or a
        malformed ISO date bound.
    """
    _validate_option_segment(series)
    valid_columns = frozenset(OPTION_CANDLE_COLUMNS)
    select = _select_list(columns, valid_columns)
    safe_limit = clamp_limit(limit)
    safe_offset = clamp_offset(offset)

    # Options roll a 1-minute table (intraday), so the date bound stays the UTC
    # calendar date — byte-identical to before (DEC-DB-UX-PACK-001 Feature 3 only
    # rewrites the DERIVED 1day interval, which the expired-options lake never holds).
    where_clauses, where_params = _date_bounds(start, end, interval="1minute")
    where_sql = (" WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    base = lake_root / OPTIONS_DIR_NAME / series
    # Empty / not-yet-pulled series → return an empty page instead of letting
    # read_parquet raise "No files found" (mirrors query_candles).
    if not (base.is_dir() and any(base.glob("*.parquet"))):
        return {
            "series": series,
            "interval": "1minute",
            "columns": list(columns) if columns is not None else list(OPTION_CANDLE_COLUMNS),
            "rows": [],
            "total": 0,
            "limit": safe_limit,
            "offset": safe_offset,
            # An expired-option series is an F&O contract → its default range is its
            # FULL STORED LIFE; an empty series has none, so (None, None) — never a
            # fabricated / "today" span (DEC-DB-UX-PACK-001 Feature 2).
            "applied_range": resolve_applied_range(
                is_fno=True,
                explicit_start=start,
                explicit_end=end,
                stored_start_ist=None,
                stored_end_ist=None,
                today_ist=_today_ist_iso(),
            ),
        }

    # The glob is built from the validated series segment (no free user text),
    # so interpolating it is safe; the source is given to read_parquet as a
    # bound parameter and every date/page value is parameter-bound. Every read
    # below routes through the resilient probe-and-skip helpers
    # (DEC-ENGINE-MISC-PACK-002, the options twin of DEC-DB-CORRUPT-SKIP-001): a
    # single corrupt / version-incompatible Parquet file in the series no longer
    # 500s the WHOLE expired-options tab on every paging click — it is logged
    # (``db.parquet_skip``), omitted, and the page degrades to its readable
    # subset (never a fabricated bar).
    source = "read_parquet(?, union_by_name=true)"
    # DETERMINISTIC page order (DEC-LAKE-STATS-MANIFEST-001): a CE + PE pair
    # shares one timestamp, and the prior bare ``ORDER BY timestamp`` left the
    # tie order to the engine's plan (a pruned file-list read could legally
    # order the pair differently than the full-glob read). The physical
    # ``filename, file_row_number`` tiebreaker (dedup-source columns, never
    # projected) pins ties to their keep-FIRST physical order — one total
    # order for BOTH paths (Golden Rule #7).
    order_sql = (
        ' ORDER BY "timestamp" ' + ("DESC" if descending else "ASC") + ", filename, file_row_number"
    )
    skip_source = f"option-candle:{series}"
    # Read-side dedup gate (DEC-LAKE-READ-UNIQUE-001): the expired-options
    # rolling identity is the COMPOSITE ``(side, timestamp)`` — the SAME key the
    # writer itself dedups on (option_parquet_writer). The page collapses
    # poisoned-era duplicates keep-FIRST in one scan; the total counts DISTINCT
    # composite keys so it equals what the deduped pages serve.
    page_source = DEDUP_PARQUET_SOURCE_SQL
    qualify_sql = dedup_qualify_sql(("side", "timestamp"))

    # Per-request stats-manifest context (DEC-LAKE-STATS-MANIFEST-001) — the
    # expired-options twin of the spot wiring; options roll a 1-minute table,
    # so the classifier uses the UTC calendar (never IST dates here).
    stats_ctx = _stats_read_context(
        conn,
        lake_root,
        kind=KIND_OPTIONS,
        key={"series": series},
        base=base,
        identity=("side", "timestamp"),
        where_sql=where_sql,
        where_params=list(where_params),
        ist_dates=False,
        start=start,
        end=end,
        use_stats_manifest=use_stats_manifest,
    )

    def _legacy_count_total() -> int:
        """Run the O(rows) ``COUNT(DISTINCT (side, timestamp))`` deduped-total scan
        over the whole series glob (the deduped composite-key count the keep-FIRST
        pages serve, NOT a footer ``COUNT(*)``; O(1) on a cached repeat).

        Routed through the COMMON :func:`_execute_parquet_resilient` (the series
        dir is FLAT, so its recursive ``**/*.parquet`` glob reads the identical
        file set as the flat glob): a corrupt file is skipped + counted out of
        the total, never a 500 (DEC-ENGINE-MISC-PACK-002).
        """

        # The COUNT execute+fetch rides the torn-read retry-once guard
        # (DEC-DB-CANDLES-TORN-READ-RETRY-001; re-globs fresh per attempt).
        def _run_count() -> list[tuple[Any, ...]]:
            return _execute_parquet_resilient(
                conn,
                base,
                f"SELECT {distinct_count_sql(('side', 'timestamp'))} FROM {source}{where_sql}",  # noqa: S608  # nosec B608 - source is read_parquet(?) with a bound path; values bound
                list(where_params),
                source=skip_source,
            )

        rows = _retry_candle_read_once(_run_count, source=skip_source, retry_sleep=retry_sleep)
        return int(rows[0][0]) if rows and rows[0][0] is not None else 0

    def _count_total() -> int:
        if stats_ctx is not None and stats_ctx.served:
            return stats_ctx.total
        return _legacy_count_total()

    # The page total is paging-invariant, so memoise it per (series, start, end)
    # behind the injected generation-stamped cache (DEC-LAKE-READ-O1-AUDIT-001) —
    # a prev/next click is then an O(1) hit instead of re-reading every Parquet
    # footer in the series. A missing cache keeps the byte-identical direct count.
    if count_cache is None:
        total = _count_total()
    else:
        total = count_cache.get_or_compute(
            option_candle_count_key(series, start, end),
            generation=generation,
            compute=_count_total,
        )
    page_select = DEDUP_STAR_SELECT_SQL if select == "*" else select
    manifest_total_candidate = stats_ctx is not None and stats_ctx.served

    # Both page forms ride the torn-read retry-once guard — the
    # expired-options twin of the query_candles wiring
    # (DEC-DB-CANDLES-TORN-READ-RETRY-001; re-plans / re-globs per attempt).
    def _run_pruned_page() -> tuple[list[str], list[tuple[Any, ...]]] | None:
        return _stats_pruned_page(
            stats_ctx,
            select=select,
            columns=columns,
            where_sql=where_sql,
            where_params=list(where_params),
            qualify_sql=qualify_sql,
            order_sql=order_sql,
            descending=descending,
            limit=safe_limit,
            offset=safe_offset,
            count_cache=count_cache,
            generation=generation,
        )

    def _run_legacy_page() -> tuple[list[str], list[tuple[Any, ...]]]:
        return _execute_candle_page_resilient(
            conn,
            base,
            f"SELECT {page_select} FROM {page_source}{where_sql}{qualify_sql}{order_sql} LIMIT ? OFFSET ?",  # noqa: S608  # nosec B608 - validated select + bound path + bound values
            [*where_params, safe_limit, safe_offset],
            source=skip_source,
        )

    pruned = _retry_candle_read_once(_run_pruned_page, source=skip_source, retry_sleep=retry_sleep)
    if pruned is not None:
        out_columns, raw_rows = pruned
    else:
        out_columns, raw_rows = _retry_candle_read_once(
            _run_legacy_page, source=skip_source, retry_sleep=retry_sleep
        )
        if manifest_total_candidate:
            # Whole-request degrade law: never pair a manifest total with a
            # degraded page; the degraded recount memoises under its own
            # disjoint generation-stamped key (see query_candles /
            # _degraded_total_key — review round 2).
            if count_cache is None:
                total = _legacy_count_total()
            else:
                total = count_cache.get_or_compute(
                    _degraded_total_key(option_candle_count_key(series, start, end)),
                    generation=generation,
                    compute=_legacy_count_total,
                )
    # Honest observed lower bound — the expired-options twin of the
    # query_candles clamp (DEC-DB-CANDLES-TORN-READ-RETRY-001): when the page
    # returned rows the total is raised to at least ``offset + len(rows)``
    # (an OBSERVED lower bound); an EMPTY page proves nothing and never
    # adjusts the total (the unconditional clamp fabricated
    # ``total == offset`` past the end — review fold).
    if raw_rows:
        total = max(total, safe_offset + len(raw_rows))
    if stats_ctx is not None:
        _LOG.info(
            _STATS_READ_EVENT,
            kind=KIND_OPTIONS,
            series=series,
            validated=stats_ctx.stats is not None,
            page_source="manifest" if pruned is not None else "live_scan",
        )
    # Split-brain screen (DEC-DASHBOARD-READ-SCREEN-001), INSANE-ONLY for options:
    # positivity/ordering-impossible option bars are dropped so /db-options never
    # SERVES a corrupt bar, but the ×4 bad-tick screen is DISABLED
    # (`bad_tick_enabled=False`) — option PREMIA legitimately move ≥4×/minute on
    # leverage, so the index-circuit-breaker-calibrated threshold would drop legit
    # bars; the engine never screens options either. This MATCHES /movers-options
    # (which passes bad_tick=False through _read_future_session), so the two agree.
    # No bad-tick seed is needed (the cross-row arm is off). Runs AFTER the raw
    # page rows are resolved (pruned OR legacy — path-agnostic) and AFTER the
    # observed-lower-bound clamp above (which deliberately reads the RAW page
    # length); on a CORRUPT partition the served page shows fewer rows than the
    # RAW total implies — a bounded honesty gap, never a fabricated bar.
    raw_rows = screen_rows(raw_rows, out_columns, source=skip_source, bad_tick_enabled=False)
    # Options roll a 1-minute table, so the bar always renders full HH:MM:SS (no
    # date-only collapse); options carry no spot derived %-columns.
    rows = _render_candle_rows(raw_rows, out_columns, "1minute")

    def _stored_span() -> tuple[str | None, str | None]:
        """The series' full-stored-life IST-date span (DEC-DB-UX-PACK-001 Feature 2).

        One ``MIN``/``MAX`` over the IST-date expression, read ONLY when a bound is
        blank (an explicit range needs no scan) and memoised per series behind the
        injected generation-stamped cache (``option_candle_span_key`` — the
        hostile-review MAJOR: the span is paging-invariant, so a prev/next click
        must be an O(1) hit, never a re-scan). Rides the COMMON
        :func:`_execute_parquet_resilient` probe-and-skip path exactly like the
        F&O span reader (``_fno_stored_ist_span_resilient``) — a corrupt file is
        skipped, an all-unreadable / readable-but-empty series yields
        ``(None, None)`` (``_iso_date_or_none`` maps each ``NULL`` through), never
        a 500, never a fabricated span (DEC-ENGINE-MISC-PACK-002). A VALIDATED,
        non-degraded stats manifest folds the span from the stamped per-file
        bounds instead — identical dates, zero Parquet I/O
        (DEC-LAKE-STATS-MANIFEST-001).
        """
        if stats_ctx is not None and stats_ctx.served:
            return stats_ctx.ist_span()
        rows = _execute_parquet_resilient(
            conn,
            base,
            f"SELECT MIN({_IST_DATE_EXPR}), MAX({_IST_DATE_EXPR}) FROM {source}",  # noqa: S608  # nosec B608 - constant expr over read_parquet(?) with a bound path
            [],
            source=skip_source,
        )
        if not rows:
            return (None, None)
        return (_iso_date_or_none(rows[0][0]), _iso_date_or_none(rows[0][1]))

    if start is None or end is None:
        if count_cache is None:
            stored_start_ist, stored_end_ist = _stored_span()
        else:
            stored_start_ist, stored_end_ist = count_cache.get_or_compute(
                option_candle_span_key(series),
                generation=generation,
                compute=_stored_span,
            )
    else:
        stored_start_ist, stored_end_ist = (None, None)
    if stats_ctx is not None and stats_ctx.should_seed:
        # Read-side write-behind seed — the options twin (see query_candles);
        # same failed-attempt latch per (dataset, generation).
        _seed_stats_once(
            conn,
            lake_root,
            kind=KIND_OPTIONS,
            key={"series": series},
            base=base,
            identity=("side", "timestamp"),
            count_cache=count_cache,
            generation=generation,
        )
    return {
        "series": series,
        "interval": "1minute",
        "columns": out_columns,
        "rows": rows,
        "total": total,
        "limit": safe_limit,
        "offset": safe_offset,
        "applied_range": resolve_applied_range(
            is_fno=True,
            explicit_start=start,
            explicit_end=end,
            stored_start_ist=stored_start_ist,
            stored_end_ist=stored_end_ist,
            today_ist=_today_ist_iso(),
        ),
    }


def _parse_iso_date(value: str, field_name: str) -> date:
    """Parse a strict ``YYYY-MM-DD`` string, re-raising with the field name.

    Rejects the looser forms ``date.fromisoformat`` accepts (``YYYYMMDD``,
    ISO week dates) so the contract matches what the date-picker emits.
    """
    if not _ISO_DATE_RE.match(value):
        raise ValueError(f"{field_name} must be ISO YYYY-MM-DD, got {value!r}")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be ISO YYYY-MM-DD, got {value!r}") from exc


#: The SPOT fetch-window market namespace. MUST MATCH what the Groww spot ingest
#: actually persists — ``GROWW_COVERAGE_MARKET`` (``"groww"``,
#: ``brutex/adapters/groww/ingest.py:106`` ``_MARKET = GROWW_COVERAGE_MARKET``), NOT
#: the legacy vendor-neutral ``COVERAGE_MARKET`` (``"spot"``). Before
#: DEC-COVERAGE-GROWW-NAMESPACE-001 this pointed at ``"spot"``, so
#: :func:`spot_window_rollup`'s EXACT-match filter skipped every real ``"groww"``
#: window and the /coverage spot chips rendered EMPTY after a live pull. The
#: ``_window_market_bucket`` catch-all still folds any other namespace into "spot".
_SPOT_WINDOW_MARKET: Final[str] = GROWW_COVERAGE_MARKET

#: The unified ledger's option-window namespace — MUST MATCH what the Groww option
#: ingest persists: ``GROWW_OPTION_COVERAGE_MARKET`` (``"groww-option"``,
#: ``option_ingest.py:179`` default). Before DEC-COVERAGE-GROWW-NAMESPACE-001 this
#: was the legacy ``"spot-option"`` and never matched a real Groww option window,
#: so options were mislabelled into the "spot" bucket.
_OPTION_WINDOW_MARKET: Final[str] = GROWW_OPTION_COVERAGE_MARKET

#: The unified ledger's FUTURE-window namespace — ``GROWW_FUTURE_COVERAGE_MARKET``
#: (``"groww-future"``, DEC-GROWW-FUTURE-INGEST-001). Without this, ``groww-future``
#: windows fell into the "spot" catch-all (mislabelled); they now get their own
#: "futures" bucket (DEC-COVERAGE-GROWW-NAMESPACE-001).
_FUTURE_WINDOW_MARKET: Final[str] = GROWW_FUTURE_COVERAGE_MARKET

#: The option coverage key's folded-interval arity:
#: ``OPT|<flag>|<code>|<strike>|<type>`` (option_ingest._coverage_key).
_OPTION_INTERVAL_ARITY = 5


def _decode_option_window(
    record: Any, symbol_for_id: Mapping[str, str] | None
) -> tuple[str, str] | None:
    """Decode one option-window ledger row into PRECISE display coordinates.

    The option window key folds the whole series into the reusable
    ``WindowKey`` (DEC-WINDOW-LEDGER-001): ``security_id = "<id>|<segment>"``
    and ``interval = "OPT|<flag>|<code>|<strike>|<type>"``. Rendered raw those
    read ``25|NSE_FNO`` / ``OPT|WEEK|1|ATM+3|CALL`` — vendor ids, not the
    operator's symbols — which is exactly the live /coverage/windows complaint
    (DEC-WINDOW-OPTION-DECODE-001). Returns
    ``("BANKNIFTY · NSE_FNO", "WEEK#1 ATM+3 CALL · 1m")``-style display pairs,
    or ``None`` for any shape that does not parse (the caller keeps the raw
    row — fallback, never an exception; an unknown id keeps the numeric id so
    the row is still distinguishable).
    """
    sid, sep, segment = str(record.security_id).partition("|")
    parts = str(record.interval).split("|")
    if not sep or len(parts) != _OPTION_INTERVAL_ARITY:
        return None
    _opt, flag, code, strike, opt_type = parts
    label = (symbol_for_id or {}).get(sid, sid)
    return (f"{label} · {segment}", f"{flag}#{code} {strike} {opt_type} · 1m")


def window_status_label(status: str, empty_streak: int, last_error: str | None = None) -> str:
    """The honest, spelled-out meaning of a window's bare *status* — pure, O(1).

    DEC-PIPELINE-AUTOPREBUILD-001 chunk 3 (empty-vs-failed honesty on
    ``/coverage/windows``): the bare ``EMPTY`` / ``SKIPPED`` / ``FAILED`` tokens
    read ambiguously — an EMPTY row is a VENDOR-CONFIRMED no-trades answer (the
    fetch succeeded; there was simply nothing to store), while a FAILED row is a
    fetch that ERRORED and WILL be re-pulled. This is the ONE label function both
    payload paths (the DuckDB fast path and the legacy pyarrow path) share via
    :func:`_window_row` (CLAUDE.md §11 COMMON — the two can never drift):

    * ``SKIPPED`` carrying an EXPIRED-WINDOW settle stamp
      (DEC-OLD-EMPTY-SINGLE-CONFIRM-001 — the old-empty single-confirm settle
      stamps :func:`~brutex.data.window_ledger.expired_window_settle_reason`
      as ``last_error``) → that stamp verbatim, suffixed ``— settled``: the
      streak-derived count would LIE (the settle forces the streak to the
      threshold), so the honest ``"vendor confirmed no trades (1 clean
      attempt, expired window) — settled"`` comes from the stamp.
    * ``EMPTY`` → ``"vendor confirmed no trades (N clean attempt(s))"`` — N is
      the row's genuine clean-empty streak, floored at 1 (a legacy record with
      no streak column still had at least the one clean attempt that classified
      it EMPTY).
    * ``SKIPPED`` carrying a data-completeness DEMOTION reason
      (:data:`~brutex.data.window_ledger.DEMOTED_SETTLE_REASONS` —
      ``ALL_FILTERED`` / ``INTERIOR_GAP`` / ``SUSPECTED_PARTIAL`` plus the
      ``"DEGRADED"`` record()-side reason-less fallback, the #1031
      degraded channel's accept-gap settle) → ``"completeness gap accepted
      (<reason>, N flagged attempt(s)) — settled"``
      (DEC-DATA-HONESTY-ALLFILTERED-SEED-001): the generic "vendor confirmed
      no trades" wording is factually WRONG for these rows — the vendor
      RETURNED bars every time; they were boundary-filtered / hole-flagged /
      truncation-flagged, never a clean no-trades answer. HONEST envelope:
      the label keys on the row's FINAL ``last_error`` only — degraded and
      clean-EMPTY bumps share ONE un-provenance'd streak
      (:meth:`~brutex.data.window_ledger.WindowLedger.record`), so a
      MIXED-evidence settle attributes all N attempts to the final channel
      (documented beside the streak arms; a per-channel count needs the named
      streak-provenance follow-up, NOT claimed).
    * any other ``SKIPPED`` (terminally-settled empty) → the same, suffixed
      ``— settled`` (the ledger promoted it; no further re-pull).
    * ``FAILED`` carrying :data:`~brutex.data.today_completeness.TODAY_INCOMPLETE`
      (DEC-PULL-TODAY-COMPLETENESS-GATE-001) → ``"today's session not yet
      settled by vendor (will re-pull)"`` — the pre-settle TODAY gate's
      streak-exempt deferral, honest that the vendor simply has not settled
      today yet (not an error, not a data hole).
    * ``FAILED`` → ``"FAILED (will re-pull)"`` — an error, not evidence of
      no-data; the window stays re-pullable.
    * ``SUCCESS`` / ``PLANNED`` / anything else → the bare status name
      (self-explanatory; unknown statuses degrade to themselves, never raise).
    """
    if status == "EMPTY":
        return f"vendor confirmed no trades ({max(int(empty_streak), 1)} clean attempt(s))"
    if status == "SKIPPED":
        if is_expired_window_settle_reason(last_error):
            return f"{last_error} — settled"
        if last_error in DEMOTED_SETTLE_REASONS:
            return (
                f"completeness gap accepted ({last_error}, "
                f"{max(int(empty_streak), 1)} flagged attempt(s)) — settled"
            )
        return (
            f"vendor confirmed no trades ({max(int(empty_streak), 1)} clean attempt(s) — settled)"
        )
    if status == "FAILED" and last_error == TODAY_INCOMPLETE:
        # DEC-PULL-TODAY-COMPLETENESS-GATE-001: a covers-today window demoted
        # because the vendor has not settled TODAY's session yet — the honest
        # wording (a streak-EXEMPT, time-transient deferral; a SKIPPED settle
        # carrying this tag is structurally unreachable but covered by
        # DEMOTED_SETTLE_REASONS membership above, defensively).
        return "today's session not yet settled by vendor (will re-pull)"
    if status == "FAILED":
        return "FAILED (will re-pull)"
    return status


def _window_row(
    record: Any,
    *,
    first_seen_at: str | None,
    symbol_for_id: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Project one per-window record to the ``/coverage/windows`` JSON row (ONE shape).

    Shared by both sources (the unified ledger + the legacy status log) so the two
    can never drift — CLAUDE.md §11 COMMON. Both record types expose the same
    field names; only the ledger carries ``first_seen_at`` (``None`` for the
    legacy log). Option windows (``_OPTION_WINDOW_MARKET``) are decoded to the operator's
    PRECISE coordinates — underlying symbol + ``FLAG#code strike type`` — via
    :func:`_decode_option_window` (DEC-WINDOW-OPTION-DECODE-001); spot rows are
    byte-unchanged.
    """
    symbol = record.security_id
    interval = record.interval
    if record.market == _OPTION_WINDOW_MARKET:
        decoded = _decode_option_window(record, symbol_for_id)
        if decoded is not None:
            symbol, interval = decoded
    return {
        "market": record.market,
        "symbol": symbol,
        "interval": interval,
        "window_start": record.window_start,
        "window_end": record.window_end,
        "status": record.status,
        # The spelled-out honesty label (ONE shape for BOTH payload paths —
        # DuckDB fast + legacy pyarrow — since both project through this row
        # builder): EMPTY/SKIPPED read "vendor confirmed no trades (N clean
        # attempt(s)[ — settled])", FAILED reads "FAILED (will re-pull)".
        "status_label": window_status_label(
            str(record.status),
            getattr(record, "empty_streak", 0),
            getattr(record, "last_error", None),
        ),
        "bars": record.bars,
        "attempts": record.attempts,
        # The genuine-EMPTY count (window_ledger R1) — surfaces WHY an EMPTY row
        # at attempts 5-6 is not yet SKIPPED: promotion needs `empty_threshold`
        # genuine EMPTYs, and an interleaved FAILED (429/timeout) now PRESERVES
        # the streak rather than resetting it (a rate-limit is not evidence of
        # data — DEC-EMPTY-STREAK-RATELIMIT-001), so only a SUCCESS resets it
        # (DEC-EMPTY-STREAK-VISIBLE-001). The legacy status log has no streak —
        # getattr keeps ONE row shape.
        "empty_streak": getattr(record, "empty_streak", 0),
        # Vendor fetches in the LAST pull (DEC-WINDOW-INPULL-FETCHES-001): >1 means the
        # in-pull empty/transient retry fired — the count that was previously DEBUG-only,
        # so the page now SHOWS that "attempts 1" already meant up to 3 fetches. The
        # legacy status log has no such field — getattr keeps ONE row shape (CLAUDE.md §11).
        "in_pull_fetches": getattr(record, "in_pull_fetches", 0),
        "last_error": record.last_error,
        # Display the stored UTC stamps as clean IST (CLAUDE.md §6 store-UTC /
        # display-IST; §11 COMMON via `format_ist_clock`) so /coverage/windows
        # reads like the candle viewer, not raw `...+00:00`.
        "first_seen_at": format_ist_clock(first_seen_at),
        "last_attempt_at": format_ist_clock(record.last_attempt_at),
    }


#: Hard cap on rows returned by :func:`build_window_payload` (hostile C3,
#: DEC-OPTION-WINDOW-PLAN-001): a pre-planned full-universe options pull holds
#: ~841k ledger rows — the un-capped projection measured 247 MB of JSON and
#: ~22 s per request against the page's 4 s poll (threadpool starvation, tab
#: death). Counts are ALWAYS complete; only the row list is capped.
WINDOW_ROWS_CAP: int = 20_000

#: Resident-entry bound for the DEDICATED /coverage/windows page-row cache
#: (DEC-WINDOWS-MIDPULL-FLOOR-001, hostile-review round-2 MEDIUM fold): a page
#: entry holds up to ``min(limit, WINDOW_ROWS_CAP)`` projected row-dicts (the
#: route's DEFAULT limit is the 20k cap), so the page family must NOT share the
#: 256-cap counts instance — worst-case resident there would be ~256 × 20k-row
#: pages, and page-key churn could LRU-evict the tiny counts entries. A small
#: dedicated instance bounds retained pages to ≤ this capacity ×
#: ``min(limit, WINDOW_ROWS_CAP)`` rows (8 × 20k = 160k row-dicts worst case —
#: stated, not hidden) while the handful of live UI coordinates
#: (market × status × page) stay warm.
WINDOW_PAGE_CACHE_CAPACITY: int = 8

#: Severity-first display order for the capped row list: actionable rows
#: (FAILED, then EMPTY) surface before the bulk statuses, so the cap can never
#: hide what the operator must act on.
_WINDOW_STATUS_ORDER: Final[dict[str, int]] = {
    "FAILED": 0,
    "EMPTY": 1,
    "SUCCESS": 2,
    "SKIPPED": 3,
    "PLANNED": 4,
}

#: Canonical sentinel every NOT-allow-listed ``?status=`` value collapses onto
#: BEFORE cache-key minting (DEC-WINDOWS-MIDPULL-FLOOR-001, round-2 hardening):
#: an unknown status matches nothing on both payload paths (legacy parity), so
#: without the collapse each distinct bogus value would mint a fresh page-cache
#: key AND pay a full fold. The sentinel's SQL arm is the literal ``1 = 0``,
#: never a ``status = <sentinel>`` bind.
_UNKNOWN_WINDOW_STATUS: Final[str] = "__UNKNOWN_STATUS__"


def _ledger_stamp_moved() -> bool:
    """Constant-``True`` 'pull probe' for the shared ledger read cache — O(1).

    DEC-LEDGER-DELTA-O-PAGE-001: the reused
    :class:`~brutex.dashboard.movers_cache.MoversSnapshotCache` gates its #920
    adaptive floor on a pull probe, but this cache's generation IS the ledger
    stamp token — it moves ONLY when a writer flushed (delta append / full save
    / prune — ANY process including a cross-process CLI pull, which an
    in-process ``BackgroundPullJob.running`` probe would be blind to), which IS
    the condition the floor exists for. Steady state never reaches the floor
    branch (the stamp is immobile → every read is a generation hit), so the
    constant ``True`` adds zero steady-state drift; the ONE stamp move of an
    isolated post-pull save is floor-bounded + logged
    (``movers_cache.stale_served`` — never a silent illusion, Golden Rule #1).
    """
    return True


def _compute_cursor(conn: Any) -> Any:
    """A statement-isolated DuckDB cursor for a cached window compute — never a raise.

    DEC-DASHBOARD-PULL-CONTENTION-RESILIENCE-001: the flagged
    (``stale_while_revalidate``) window caches may run a compute on a
    BACKGROUND daemon revalidator, CONCURRENT with the request thread's own
    statements on the SAME shared connection — and DuckDB's execute→fetch is
    per-connection pending-result state, so interleaved statements corrupt
    each other's results (reproduced in-suite: a background counts fold
    fetched the request's 16-column page rows and died on the 3-tuple
    unpack). ``conn.cursor()`` yields an isolated statement holder for BOTH
    production shapes (round-1 BLOCKER fix — the original fallback rationale
    was FALSE for the store conn, which had no ``cursor()`` and silently
    shared one single-thread-per-view instance across the revalidator
    daemons): a raw DuckDB connection returns an independent engine
    connection (its own pending-result state), and the store's
    ``_LockedReadConn`` returns a FRESH locked view over the same read
    cursor + read lock (independent per-view hold state; each compute's
    execute→fetch stays atomic under the store read lock — never a shared
    ``_held`` flag, whose cross-thread corruption leaked the read lock and
    froze every later dashboard read). The inline single-flight computes ride
    the same cursor, which also closes the pre-existing (rare) cross-request
    interleave on these families. A conn without ``cursor()`` (test fakes /
    exotic injections — genuinely single-threaded drivers) — or a raising
    ``cursor()`` — falls back to *conn* itself (isolation is best-effort,
    never a 500). Callers pair every isolated cursor with
    :func:`_close_cursor` so a compute can never strand a held view.
    """
    factory = getattr(conn, "cursor", None)
    if factory is None:
        return conn
    try:
        return factory()
    except Exception:  # pylint: disable=broad-except — isolation is best-effort
        return conn


def _close_cursor(cursor: Any, conn: Any) -> None:
    """Best-effort close of a per-compute isolated cursor — NEVER the shared *conn*.

    DEC-DASHBOARD-PULL-CONTENTION-RESILIENCE-001 round 1: an isolated cursor
    can end its compute still HOLDING state — the legacy-shape probe
    (`_ensure_full_schema`) raises ``ValueError`` after a description-only
    read, which on a ``_LockedReadConn`` view leaves the store read lock
    acquired (an abandoned hold = every later dashboard read deadlocks; the
    request-shared view was previously rebalanced by the NEXT execute, a
    per-compute view never is) — and a raw DuckDB cursor is an engine
    connection worth freeing eagerly rather than at GC. The fallback arm
    (``cursor is conn`` — the caller's own shared connection) is never
    closed; a missing/raising ``close`` is swallowed (cleanup can never mask
    the compute's own outcome or 500 a poll).
    """
    if cursor is conn:
        return
    close = getattr(cursor, "close", None)
    if close is None:
        return
    with contextlib.suppress(Exception):  # cleanup is best-effort
        close()


def _isolated_counts_total(
    conn: Any, source: _LedgerScanSource
) -> tuple[dict[str, dict[str, int]], int]:
    """:func:`_window_counts_total` on a per-compute isolated cursor (closed after).

    The ONE compute body the flagged counts caches run — possibly on a
    background SWR revalidator daemon concurrent with the request thread —
    so it must never touch the caller's shared *conn* directly
    (`_compute_cursor`) and must release its view even on a raise
    (`_close_cursor`).
    """
    cursor = _compute_cursor(conn)
    try:
        return _window_counts_total(cursor, source)
    finally:
        _close_cursor(cursor, conn)


def _isolated_counts_since(
    conn: Any, source: _LedgerScanSource, since_iso: str
) -> tuple[dict[str, dict[str, int]], int]:
    """The per-run twin of :func:`_isolated_counts_total` — same isolation law."""
    cursor = _compute_cursor(conn)
    try:
        return _window_counts_since(cursor, source, since_iso)
    finally:
        _close_cursor(cursor, conn)


def _isolated_counts_total_scoped(
    conn: Any, source: _LedgerScanSource, bind: _WindowScopeBind
) -> tuple[dict[str, dict[str, int]], int, int, int]:
    """The R11 scoped counts fold on an isolated cursor — same isolation law."""
    cursor = _compute_cursor(conn)
    try:
        return _window_counts_total_scoped(cursor, source, bind)
    finally:
        _close_cursor(cursor, conn)


def _isolated_counts_since_scoped(
    conn: Any, source: _LedgerScanSource, since_iso: str, bind: _WindowScopeBind
) -> tuple[dict[str, dict[str, int]], int]:
    """The scoped per-run twin on an isolated cursor — same isolation law."""
    cursor = _compute_cursor(conn)
    try:
        return _window_counts_since_scoped(cursor, source, since_iso, bind)
    finally:
        _close_cursor(cursor, conn)


#: Resident-key bound for the shared ledger read cache: 2 key families
#: (records / spot-rollup — the ``ledger-rows`` family was RETIRED by
#: DEC-MEMORY-FOOTPRINT-HARDENING-001 fix 3: the /db grid rows are now an
#: O(page) on-demand projection over the ONE resident records family, never a
#: second resident whole-ledger tuple) × multi-lake test headroom.
_LEDGER_READ_CACHE_CAPACITY: Final[int] = 32

#: Hard cap on the ledger read cache's adaptive floor — the #920 ``SummaryCache``
#: cap (``summary_cache.MAX_ADAPTIVE_FLOOR_S``), NOT the 600 s movers default:
#: this floor is not pull-probe-scoped (the probe above is constant ``True``),
#: so the tighter cap bounds the worst-case served staleness.
_LEDGER_READ_FLOOR_CAP_S: Final[float] = 120.0

#: Byte watermark for the ledger read cache (DEC-MEMORY-FOOTPRINT-HARDENING-001):
#: the cache retains ONE parsed whole-ledger record TUPLE per lake (~1.0 KB/row
#: measured — a production 841k-row ledger parses to ~0.8 GB resident; fix 3
#: retired the second per-row ``ledger-rows`` family, halving the tracked
#: two-family residency) plus the small spot-rollup dicts, so the 32-entry
#: count bound alone is no byte
#: bound at all. Budget = 1/16 of total RAM via the COMMON `memory_budget_bytes`
#: (3 GiB on the CLAUDE.md §8 48 GiB box); floor 1 GiB when RAM is undetected —
#: generous enough that today's steady state (one lake, two families) never
#: evicts, while a pathological multi-lake session is bounded. No new env var.
_LEDGER_READ_CACHE_FRACTION: Final[float] = 1.0 / 16.0
_LEDGER_READ_CACHE_FLOOR_BYTES: Final[int] = 1 << 30  # 1 GiB

#: Per-record byte ESTIMATE for the ledger sizing hint (hostile-review M1 on
#: DEC-MEMORY-FOOTPRINT-HARDENING-001): RE-MEASURED at the 2026-07-11 #1719
#: merge-reconcile — main's DEC-... #1386 INTERNED ledger decode (shared
#: key-string objects across records) halved the per-record deep size, so the
#: pre-interning ~600 B/record basis (rounded to 640) over-stated the deep
#: walk ~2.1× and tripped the fix-3 residency ratio pin; the residency bench
#: now measures the resident ``ledger-records`` family at ~301 B/record @ 50k
#: rows under the SAME ``deep_size_bytes`` accounting the watermark evicts on
#: (``tests/bench/test_ledger_cache_residency.py``); rounded conservatively UP
#: to 320 so the hint over-states rather than under-states (the watermark then
#: evicts EARLIER than the deep walk would — the safe direction for a byte
#: bound). HONEST (Golden Rule #10): an estimate feeding an estimate — the
#: watermark tracks approximations, never exact RSS; the hint-vs-deep-walk
#: accuracy is pinned within 3× on a real records payload.
_LEDGER_RECORD_EST_BYTES: Final[int] = 320

#: Flat overhead added by the hint for a payload's outer shells (the 2-tuple /
#: dict container + slack) — small beside even a few records.
_LEDGER_HINT_BASE_BYTES: Final[int] = 512

#: The records-family payload's arity: ``(generation, records)`` — named to
#: avoid a PLR2004 magic number (the app.py convention).
_LEDGER_RECORDS_PAYLOAD_ARITY: Final[int] = 2


def _ledger_payload_size_hint(payload: object) -> int | None:
    """O(1)-in-rows sizing hint for the ledger cache's payload families.

    Hostile-review M1 (DEC-MEMORY-FOOTPRINT-HARDENING-001): the fix-2 byte
    watermark ran :func:`~brutex.dashboard.movers_cache.deep_size_bytes` over
    every COLD ledger parse's ``(generation, tuple[WindowLedgerRecord, ...])``
    payload — measured **5.28 s** at the documented 841k-row production scale
    (~25M ``getsizeof``/``getattr`` ops through the ``__slots__`` MRO branch)
    plus a transient ~1.7M-entry ``seen`` id-set (~100+ MB), recurring at pull
    cadence (the stamp moves per delta flush). This hint recognizes THE
    records-family shape — a 2-tuple ``(int generation, tuple of
    WindowLedgerRecord)``, checked on the FIRST element only (O(1); every
    record in a payload is the same type by construction — one
    ``load_window_ledger(...).records()`` materialization) — and returns
    ``len(records) × _LEDGER_RECORD_EST_BYTES + _LEDGER_HINT_BASE_BYTES``.
    Every OTHER shape returns ``None`` → the cache's deep-size fallback; the
    only other resident family, the ``spot-rollup|`` dict, is SMALL by
    construction (≤ spot symbols × fetch intervals cells, ~1.5k — its deep
    walk is sub-millisecond), so it deliberately stays on the accurate walk.
    """
    if (
        isinstance(payload, tuple)
        and len(payload) == _LEDGER_RECORDS_PAYLOAD_ARITY
        and isinstance(payload[0], int)
        and isinstance(payload[1], tuple)
    ):
        from brutex.data.window_ledger import WindowLedgerRecord

        records = payload[1]
        if not records or isinstance(records[0], WindowLedgerRecord):
            return _LEDGER_HINT_BASE_BYTES + len(records) * _LEDGER_RECORD_EST_BYTES
    return None


def _make_ledger_read_cache(
    *,
    clock: Callable[[], float] = _monotonic_clock,
    swr_runner: Callable[[Callable[[], None]], None] | None = None,
) -> MoversSnapshotCache:
    """Construct THE shared stamped ledger read cache — parameters exist ONCE.

    Single-flight + the #920 adaptive floor + the RAM-derived byte watermark
    (DEC-MEMORY-FOOTPRINT-HARDENING-001), all REUSED from the verified
    :class:`~brutex.dashboard.movers_cache.MoversSnapshotCache` contract (no
    third hand-rolled cache — CLAUDE.md §11 COMMON), plus the M1
    ledger-aware ``size_of`` sizing hint so a cold 841k-row parse never pays
    the measured 5.28 s deep-size walk (see :func:`_ledger_payload_size_hint`).
    *clock* / *swr_runner* are injectable so the floor +
    stale-while-revalidate tests are deterministic (production keeps the
    monotonic default and the daemon-thread runner).
    """
    return MoversSnapshotCache(
        capacity=_LEDGER_READ_CACHE_CAPACITY,
        max_bytes=memory_budget_bytes(
            fraction=_LEDGER_READ_CACHE_FRACTION,
            floor_bytes=_LEDGER_READ_CACHE_FLOOR_BYTES,
        ),
        size_of=_ledger_payload_size_hint,
        pull_active_probe=_ledger_stamp_moved,
        adaptive_floor=True,
        adaptive_floor_cap_s=_LEDGER_READ_FLOOR_CAP_S,
        # DEC-DASHBOARD-PULL-CONTENTION-RESILIENCE-001 (G4 widened — the
        # operator's live repro: /db window-ledger rows stuck behind an inline
        # O(rows) re-parse past the floor mid-pull): PAST-floor stamp churn
        # serves the PRIOR stamp's records instantly and re-parses in ONE
        # tokenless background revalidator — bounded staleness (≤ floor + one
        # parse behind the live ledger), every serve logged, converged by the
        # next idle read (the constant-True probe keeps the arm armed exactly
        # while the stamp churns).
        stale_while_revalidate=True,
        swr_runner=swr_runner,
        clock=clock,
    )


#: THE shared stamped ledger read cache (DEC-LEDGER-DELTA-O-PAGE-001 — replaces
#: the three module dict caches ``_WINDOW_CACHE`` / ``_WINDOW_ROWS_CACHE`` /
#: ``_SPOT_ROLLUP_CACHE``): the windows page polls every 4 s and the coverage
#: hub hits the same endpoint; re-reading an 841k-row Parquet per poll measured
#: ~13.6 s. Each consumer keys ``<family>|<ledger path>`` at a generation
#: digested from the composite ledger stamp token (base mtime PLUS the pending
#: delta name set — :func:`~brutex.dashboard.movers_cache.revision_generation`
#: over :func:`_ledger_stamp_token`), so any flush shape — full save, mid-pull
#: delta append/prune, a cross-process CLI write — moves the generation.
#: Concurrent stamp-miss pollers are SINGLE-FLIGHT (one thread pays the parse,
#: the rest wait on the per-key event) and a stamp move within the adaptive
#: floor serves the prior entry stale — stale by at most
#: ``min(max(2 s, its own compute duration), 120 s)`` of ledger movement,
#: possibly spanning several ≥2 s delta flushes (a TIME bound, never a
#: stamp-COUNT bound — the floor branch ages ``computed_at`` only), ALWAYS
#: logged (``movers_cache.stale_served``) — the #920 ≤50%-duty law.
#: The one DERIVED cached family (spot-rollup) is stamped with the generation
#: the records were ACTUALLY served at (:func:`_ledger_records_stamped` embeds
#: it in the cached value), so a projection built from a floor-stale records
#: serve is itself generation-stale — never latched under the fresh generation
#: (the fix-round latch defect, Golden Rule #1). A derived entry then ages
#: under its OWN floor once the records floor passes, so a derived family's
#: worst-case staleness COMPOSES the records floor with its own (each capped
#: at 120 s) — every stale serve logged on the key that served it.
#: RESIDENCY (DEC-MEMORY-FOOTPRINT-HARDENING-001 fix 3): the ``ledger-records``
#: family is THE single resident per-row representation. The former
#: ``ledger-rows`` cached family (a second whole-ledger tuple-per-row payload,
#: ~1.0 KB/row of tracked bytes beside the records' ~1.0 KB/row) is RETIRED —
#: the /db grid projects its rows ON DEMAND from the cached records
#: (:func:`window_ledger_rows_page`, O(page) per request), so exactly one
#: per-row family is resident per lake and the grid's staleness is exactly the
#: records serve (single-phase — the rows half of the composed-floor envelope
#: is gone with the cache entry).
_LEDGER_READ_CACHE: MoversSnapshotCache = _make_ledger_read_cache()


def _reset_ledger_read_cache() -> None:
    """Test seam — replaces the dict-clear idiom of the retired module dicts."""
    global _LEDGER_READ_CACHE  # noqa: PLW0603 — the documented test seam
    _LEDGER_READ_CACHE = _make_ledger_read_cache()


def make_window_payload_caches(
    *,
    clock: Callable[[], float] = _monotonic_clock,
    swr_runner: Callable[[Callable[[], None]], None] | None = None,
) -> tuple[MoversSnapshotCache, MoversSnapshotCache]:
    """Construct the ``(counts_cache, page_cache)`` pair for ``/coverage/windows``.

    DEC-WINDOWS-MIDPULL-FLOOR-001 — the ONE construction point (`app.py` wires
    it verbatim; parameters exist ONCE, the ``_make_ledger_read_cache``
    precedent). Both instances ride the SAME probe/floor shape as the sibling
    :data:`_LEDGER_READ_CACHE` — the constant-``True``
    :func:`_ledger_stamp_moved` probe + the tighter
    :data:`_LEDGER_READ_FLOOR_CAP_S` (120 s) cap — because these caches'
    generation IS the ledger stamp digest: steady state never reaches the
    floor branch (the stamp is immobile → every read is a generation hit), and
    a stamp move from ANY process — an in-process pull's delta flush AND a
    cross-process CLI ``brutex fetch`` (which an in-process
    ``BackgroundPullJob.running`` probe would be blind to) — is floor-bounded
    ``min(max(2 s, the family's own compute duration), 120 s)`` + logged
    (``movers_cache.stale_served``), never silent (hostile-review round-2
    MINOR fold, option (a) — supersedes the round-1 ``_pull_live`` probe whose
    floor never covered CLI churn). The PAGE instance is a SMALL dedicated
    cache (:data:`WINDOW_PAGE_CACHE_CAPACITY`) so retained pages stay bounded
    and page-key churn can never evict the counts entries (round-2 MEDIUM
    fold — see the capacity constant's envelope). *clock* is injectable so the
    floor tests are deterministic (production keeps the monotonic default).
    Both instances ALSO opt into ``stale_while_revalidate``
    (DEC-DASHBOARD-PULL-CONTENTION-RESILIENCE-001 — see the construction
    comment below for the sanctioned-staleness envelope).
    """
    # DEC-DASHBOARD-PULL-CONTENTION-RESILIENCE-001 (G4): BOTH instances opt
    # into stale-while-revalidate — a pull-progress / /coverage/windows poll
    # landing PAST the 120 s-capped floor serves the PRIOR stamp's counts /
    # page instantly and revalidates the O(rows + delta-footers) fold in ONE
    # tokenless background daemon; concurrent pollers never block on the
    # single-flight (the operator's live repro: the windows page stuck at
    # "loading…" mid-pull, and the /universe feed dropping ~154 s recurring).
    # The fold still RUNS (≤1 concurrent, ≤ floor cadence) — this bounds poll
    # LATENCY, it does not eliminate the fold (Golden Rule #10). Because the
    # probe is the constant-True `_ledger_stamp_moved`, the SWR arm also
    # covers CROSS-PROCESS CLI-pull stamp churn: an idle-after-CLI-write read
    # past the floor converges via one background fold instead of one inline
    # fold (≤ seconds delayed freshness, log-labelled — the disclosed
    # sanctioned delta for the counts family).
    counts_cache = MoversSnapshotCache(
        pull_active_probe=_ledger_stamp_moved,
        adaptive_floor=True,
        adaptive_floor_cap_s=_LEDGER_READ_FLOOR_CAP_S,
        stale_while_revalidate=True,
        swr_runner=swr_runner,
        clock=clock,
    )
    page_cache = MoversSnapshotCache(
        capacity=WINDOW_PAGE_CACHE_CAPACITY,
        pull_active_probe=_ledger_stamp_moved,
        adaptive_floor=True,
        adaptive_floor_cap_s=_LEDGER_READ_FLOOR_CAP_S,
        stale_while_revalidate=True,
        swr_runner=swr_runner,
        clock=clock,
    )
    return counts_cache, page_cache


#: The ledger's incremental-delta sub-directory name (DEC-LEDGER-DELTA-FLUSH-001).
#: MUST equal ``brutex.data.window_ledger_store.DELTAS_DIRNAME`` — pinned by
#: test, not imported at module level (the ``lake_revision._SWEEPS_DIRNAME``
#: precedent: this module deliberately carries no module-level
#: ``window_ledger_store`` edge — CodeQL ``py/cyclic-import``).
_LEDGER_DELTAS_DIRNAME: Final[str] = "deltas"

#: The ledger table's directory + filename under the lake root
#: (DEC-DB-LATENCY-HARDENING-001). MUST equal
#: ``window_ledger_store.LEDGER_DIRNAME`` / ``LEDGER_FILENAME`` — pinned by
#: test, not imported (the :data:`_LEDGER_DELTAS_DIRNAME` precedent + the
#: ``phase_progress_payload`` no-import-edge law): even a FUNCTION-local
#: ``window_ledger_store`` import participates in the
#: ``window_ledger_store → coverage_reconcile → db_view`` cycle CodeQL
#: ``py/cyclic-import`` flags, so the new fast path derives the path from
#: these constants instead of importing ``ledger_path``.
_LEDGER_TABLE_DIRNAME: Final[str] = "window_ledger"
_LEDGER_TABLE_FILENAME: Final[str] = "ledger.parquet"


def _ledger_delta_files(ledger_file: Path) -> tuple[Path, ...]:
    """The pending ledger delta Parquets, in append (filename) order — ``()`` if none.

    DEC-LEDGER-DELTA-FLUSH-001: the mid-pull flush now APPENDS small deltas
    beside the base ``ledger.parquet`` instead of rewriting it; every ledger
    reader in this module must see them or the /coverage surfaces freeze for the
    whole pull. Derived from *ledger_file* (``<dir>/deltas/*.parquet``) so no
    ``window_ledger_store`` import edge is added. O(deltas) listing — this probe
    runs on EVERY poll (it is how a stamp move is detected), so it is sorted by
    the NAME string (DEC-WINDOWS-MIDPULL-FLOOR-001): for same-directory entries
    that is order-identical to whole-``Path`` ordering on this POSIX-only lake
    (one shared parent; the zero-padded ``time_ns-pid`` names carry no case),
    while skipping ``Path.__lt__``'s per-comparison part-normalisation — the
    measured hot term of a warm poll at a multi-hour pull's hundreds of pending
    deltas. A listing OSError (a wedged volume — EIO and friends) degrades to
    ``()`` after the latched stat-outage WARN
    (DEC-WINDOWS-STAT-GUARD-LATCH-001), never a raise → 500.
    """
    directory = ledger_file.parent / _LEDGER_DELTAS_DIRNAME
    try:
        files = tuple(sorted(directory.glob("*.parquet"), key=lambda f: f.name))
    except OSError as exc:
        _note_ledger_stat_outcome(source="deltas", degraded=True, path=directory, error=str(exc))
        return ()
    _note_ledger_stat_outcome(source="deltas", degraded=False, path=directory)
    return files


#: One-per-outage-flip latch for the ledger stat-degrade WARN
#: (DEC-WINDOWS-STAT-GUARD-LATCH-001): the ledger surfaces poll every ~4 s,
#: so an unlatched WARN per stat attempt would storm the log for the whole
#: outage. Keyed PER STAT SOURCE (``"base"`` = the ledger-file stat,
#: ``"deltas"`` = the deltas-dir listing — the two independent probes ONE
#: ``_ledger_cache_stamp`` poll runs back-to-back): the prior shared bool let
#: a persistently-faulty deltas dir beside a HEALTHY base flip the latch on
#: EVERY poll (the healthy base reset it, the deltas fault re-armed it) — a
#: WARN+INFO pair per poll, exactly the storm the latch exists to prevent
#: (review fold on DEC-CACHE-SERVE-LAST-KNOWN-001's batch). Each source now
#: flips independently. Plain module dict — a rare race costs at most one
#: duplicate log line, never a wrong verdict.
_LEDGER_STAT_DEGRADED: dict[str, bool] = {}


def _note_ledger_stat_outcome(
    *, source: str, degraded: bool, path: Path, error: str | None = None
) -> None:
    """Flip *source*'s stat-outage latch, logging ONCE per flip (never per poll)."""
    prior = _LEDGER_STAT_DEGRADED.get(source, False)
    if degraded and not prior:
        _LEDGER_STAT_DEGRADED[source] = True
        _LOG.warning("db.ledger_stat_degraded", source=source, path=path.as_posix(), error=error)
    elif not degraded and prior:
        _LEDGER_STAT_DEGRADED[source] = False
        _LOG.info("db.ledger_stat_recovered", source=source, path=path.as_posix())


def _ledger_base_stat(ledger_file: Path) -> tuple[int, bool | None]:
    """One guarded ``stat`` of the ledger base → ``(mtime_ns, present)``.

    DEC-WINDOWS-STAT-GUARD-LATCH-001 — the OSError-classed replacement for the
    bare ``Path.is_file()`` / ``FileNotFoundError``-only stats that made a
    transient EBADF/ELOOP render a SILENT empty grid (pathlib swallows those
    errnos into ``False`` with zero logs) and an EIO raise a route 500.
    ``present`` is a three-state verdict: ``True`` (regular stat succeeded),
    ``False`` (DEFINITIVELY absent — ``FileNotFoundError``/
    ``NotADirectoryError``), or ``None`` (a stat OUTAGE — any other
    ``OSError``: EIO/EBADF/ELOOP/…, logged through the one-per-outage-flip
    :func:`_note_ledger_stat_outcome` latch, NAMED never silent). Both
    non-``True`` arms stamp ``mtime_ns=0`` (the established absent-base
    token), so a healthy↔outage flip moves the cache generation and the next
    healthy poll recomputes — never a latched stale entry.
    """
    try:
        st = ledger_file.stat()
    except (FileNotFoundError, NotADirectoryError):
        _note_ledger_stat_outcome(source="base", degraded=False, path=ledger_file)
        return (0, False)
    except OSError as exc:
        _note_ledger_stat_outcome(source="base", degraded=True, path=ledger_file, error=str(exc))
        return (0, None)
    _note_ledger_stat_outcome(source="base", degraded=False, path=ledger_file)
    return (st.st_mtime_ns, True)


def _ledger_cache_stamp(ledger_file: Path) -> tuple[int, tuple[str, ...]]:
    """The composite freshness stamp for the ledger read caches — O(deltas).

    ``(base mtime_ns, delta filenames)``: a full save moves the base mtime; a
    delta append/prune changes the (immutable-once-written) delta name set — so
    any flush shape invalidates. A missing base (a first pull's mid-flight
    delta-only checkpoint) stamps ``0``, never raises; a stat OUTAGE
    (EIO/EBADF/ELOOP — previously an unguarded raise → 500 through
    ``_ledger_records_stamped``) ALSO stamps ``0`` after the latched WARN
    (:func:`_ledger_base_stat`, DEC-WINDOWS-STAT-GUARD-LATCH-001).
    """
    base_mtime, _present = _ledger_base_stat(ledger_file)
    return (base_mtime, tuple(f.name for f in _ledger_delta_files(ledger_file)))


#: The window-ledger fold identity — the leading 5 columns of the ledger schema
#: (the ``row[0:5]`` key ``load_window_ledger``'s Python dict fold uses). MUST
#: equal ``window_ledger_store._COLUMNS[:5]`` — pinned by test, not imported
#: (the :data:`_LEDGER_DELTAS_DIRNAME` precedent: this module deliberately
#: carries no module-level ``window_ledger_store`` edge).
_WINDOW_KEY_COLUMNS: Final[tuple[str, ...]] = (
    "market",
    "security_id",
    "interval",
    "window_start",
    "window_end",
)


@dataclass(frozen=True)
class _LedgerScanSource:
    """ONE assembled DuckDB scan source for the window-ledger fast paths.

    DEC-LEDGER-DELTA-O-PAGE-001: the SINGLE assembly point every fast-path
    consumer (:func:`_window_counts_total`, :func:`_window_counts_since`, the
    page query) scans FROM, so the file-list + base-marker bind order can never
    drift between them. ``from_sql`` is either the steady-state
    ``"read_parquet(?)"`` literal (no pending deltas — SQL text byte-identical
    to the pre-fold fast path) or the latest-wins fold subquery
    (:func:`~brutex.data.lake_read.dedup_latest_source_sql`); ``params`` are its
    binds in order (source params come FIRST in every consuming query);
    ``stamp_token`` is the composite content token the count-cache keys ride.
    """

    from_sql: str
    params: tuple[Any, ...]
    stamp_token: str


def _stamp_token_text(base_mtime: int, delta_names: tuple[str, ...]) -> str:
    """Render one ledger stamp as the composite count-cache token — pure, O(deltas).

    ``"<base mtime_ns>.<delta count>.<sha256(delta names)[:16]>"`` — bounded key
    length regardless of how many deltas a multi-hour pull accumulates,
    collision-resistant with no epoch reasoning: a full save moves the base
    mtime, an append/prune moves the (immutable-once-written) delta name set,
    and a CLI cross-process write moves both — never stale (Golden Rule #1). A
    missing base stamps ``0`` (mirrors :func:`_ledger_cache_stamp`).
    """
    digest = hashlib.sha256("\n".join(delta_names).encode("utf-8")).hexdigest()[:16]
    return f"{base_mtime}.{len(delta_names)}.{digest}"


def _ledger_stamp_token(ledger_file: Path) -> str:
    """The composite ledger content token for *ledger_file* — one stat + one listing."""
    base_mtime, delta_names = _ledger_cache_stamp(ledger_file)
    return _stamp_token_text(base_mtime, delta_names)


def _ledger_scan_source(ledger_file: Path) -> _LedgerScanSource:
    """Assemble the fast-path scan source from ONE fresh look at the ledger dir.

    Re-listed on EVERY call (the callers rebuild it per retry attempt), so the
    save-time flock prune race self-heals: a delta unlinked mid-query surfaces
    as a ``duckdb.Error`` and the existing retry recomputes over a fresh listing
    that sees the subsumed base-only state. Exactly ONE delta listing + one base
    stat per call; the stamp token is computed from the SAME listing epoch as
    the file list. With no pending deltas the source is the byte-identical
    steady-state ``"read_parquet(?)"`` literal with the single base-path bind
    (SQL-text spy-pinned); with deltas it is the latest-wins fold subquery whose
    base-path marker is ALWAYS bound — an absent base then matches nothing and
    the deltas order among themselves (one code path, live-verified).
    """
    deltas = _ledger_delta_files(ledger_file)  # the ONE listing this call performs
    # OSError-classed stat (DEC-WINDOWS-STAT-GUARD-LATCH-001): a definite
    # absence (False) excludes the base from the fold list as before; a stat
    # OUTAGE (None — EIO/EBADF/ELOOP) keeps the base IN the list — excluding a
    # file that exists would present a delta-only view as truth (a fabricated
    # partial ledger), while including it lets the READ decide: an unreadable
    # base surfaces as a duckdb error into the existing retry/legacy-fold
    # degrade ladders, never a silent wrong grid.
    base_mtime, present = _ledger_base_stat(ledger_file)
    base_present = present is not False
    token = _stamp_token_text(base_mtime, tuple(f.name for f in deltas))
    base = ledger_file.as_posix()
    if not deltas:
        return _LedgerScanSource(from_sql="read_parquet(?)", params=(base,), stamp_token=token)
    files = ([base] if base_present else []) + [f.as_posix() for f in deltas]
    return _LedgerScanSource(
        from_sql=dedup_latest_source_sql(_WINDOW_KEY_COLUMNS),
        params=(files, base),
        stamp_token=token,
    )


def _ledger_records_stamped(lake_root: Path) -> tuple[int, tuple[Any, ...]]:
    """Load the ledger's records ONCE per ledger stamp, WITH their true epoch.

    Rides THE shared :data:`_LEDGER_READ_CACHE` (DEC-LEDGER-DELTA-O-PAGE-001),
    keyed ``ledger-records|<path>`` at the stamp-token generation — base mtime
    PLUS the pending delta name set (DEC-LEDGER-DELTA-FLUSH-001), so a mid-pull
    delta append OR a cross-process CLI write refreshes it exactly as a full
    save always did. The underlying ``load_window_ledger`` folds the deltas
    newer-file-wins.

    Returns ``(generation, records)`` where *generation* is the stamp-token
    generation the records were actually COMPUTED at — the cached value EMBEDS
    its own epoch, so an adaptive-floor STALE serve (which returns the PRIOR
    entry verbatim) hands the caller the OLD generation, never the fresh one.
    The derived cached key family (``spot-rollup``) MUST stamp
    its projection with THIS served generation: a projection built from
    floor-stale records is then cached generation-STALE itself and recomputes
    once the records floor passes AND its OWN #920 floor permits (a derived
    entry ages under its own floor after the records refresh — the two floors
    COMPOSE; see the derived consumers' envelopes) — never latched under the
    fresh generation serving pre-flush content indefinitely (the fix-round
    latch defect: a derived value may never be cached at a generation newer
    than the records it was built from — Golden Rule #1).

    HONEST envelope (Golden Rule #10): concurrent stamp-miss pollers pay ONE
    parse (single-flight); a stamp move within ``min(max(2 s, the last parse's
    own duration), 120 s)`` serves the prior entry STALE — stale by at most
    that TIME bound of ledger movement, possibly spanning several ≥2 s delta
    flushes (never a stamp-count bound), always logged
    (``movers_cache.stale_served``); the FIRST parse per stamp stays O(rows).
    """
    from brutex.data.window_ledger_store import ledger_path, load_window_ledger

    path = ledger_path(lake_root)
    generation = revision_generation(_ledger_stamp_token(path))
    return _LEDGER_READ_CACHE.get_or_compute(
        f"ledger-records|{path.as_posix()}",
        generation=generation,
        compute=lambda: (generation, tuple(load_window_ledger(lake_root).records())),
    )


def _ledger_records_cached(lake_root: Path) -> tuple[Any, ...]:
    """Load the ledger's records ONCE per ledger stamp (the C3 read path).

    Thin unwrap of :func:`_ledger_records_stamped` for the consumers that fold
    the records directly (the legacy window-payload path, the phase-progress
    double-failure fallback) and so cannot latch — a later call re-keys on the
    stamp itself. Derived CACHED projections must use the stamped variant.
    """
    return _ledger_records_stamped(lake_root)[1]


def window_ledger_rows_cached(lake_root: Path) -> tuple[tuple[Any, ...], ...]:
    """The WHOLE window-ledger row projection, parsed ONCE per ledger stamp.

    DEC-SUSPICION-SWEEP-FIXES-001 (D3): the ``/api/v1/db/window-ledger`` route
    previously ran an UNCACHED ``load_window_ledger(...).to_rows()`` — a full
    Parquet re-parse of the whole ledger (a pre-planned full-universe options
    pull holds ~841k rows, the measured shape behind :data:`WINDOW_ROWS_CAP`) on
    EVERY paging click. This helper routes the read through the SAME stamp-keyed
    ``_ledger_records_cached`` single parse the other ledger readers share, then
    builds the row projection (the canonical
    :func:`~brutex.data.window_ledger.ledger_record_row` per record — byte-equal
    to ``WindowLedger.to_rows`` by construction, ONE source of the shape)
    ON DEMAND. RESIDENCY (DEC-MEMORY-FOOTPRINT-HARDENING-001 fix 3): the
    projection is NO LONGER cached — the retired ``ledger-rows|<path>`` family
    held a SECOND whole-ledger per-row payload beside the records family
    (~1.0 KB/row of tracked bytes EACH, measured — ~0.8 GB apiece at the
    production 841k-row ledger), so the shared cache now retains exactly ONE
    per-row representation per lake and the transient projection's leaf objects
    are SHARED with the resident records (tuple shells only, garbage after the
    caller drops them). The ``/db`` grid route rides the O(page)
    :func:`window_ledger_rows_page` instead; this whole-projection surface is
    kept for parity/compat consumers with its exact historical output. A lake
    with no ledger Parquet AND no pending deltas returns ``()`` — the same
    empty grid the uncached read produced; a base-less DELTA-ONLY lake (a first
    pull's mid-flight checkpoint) renders its live rows like every other ledger
    reader.
    HONEST envelope (Golden Rule #10): EVERY call now pays the O(rows) tuple
    projection (the parse itself stays one-per-stamp behind the records cache)
    — callers that only need a page must use :func:`window_ledger_rows_page`.
    Staleness is exactly the records serve, SINGLE-phase since fix 3 (the
    projection has no cache entry, so no second #920 floor exists): a stamp
    move within the records adaptive floor (``min(max(2 s, the parse's own
    duration), 120 s)``) serves rows built from the prior records — possibly
    spanning several ≥2 s delta flushes (a time bound, never a stamp-count
    bound), logged (``movers_cache.stale_served``) on the ``ledger-records``
    key that served it — and the first read past the floor projects the
    on-disk truth immediately (no rows-floor lag to compose); true O(page)
    totals need a write-time manifest, NOT claimed (the #867/#1025 envelope).
    """
    from brutex.data.window_ledger import ledger_record_row
    from brutex.data.window_ledger_store import ledger_path

    path = ledger_path(lake_root)
    # OSError-classed presence gate (DEC-WINDOWS-STAT-GUARD-LATCH-001): the
    # prior bare ``path.is_file()`` swallowed a transient EBADF/ELOOP into
    # ``False`` — a lake that HAS a ledger rendered a SILENT empty grid
    # (total=0, zero logs) — and RAISED on EIO → a route 500. Now: a
    # DEFINITIVELY absent base (+ no deltas) keeps the honest empty grid; a
    # stat OUTAGE proceeds to the read attempt (the stat glitch may be
    # transient while the read works), NAMED by the latched
    # ``db.ledger_stat_degraded`` WARN — never silent.
    _mtime, present = _ledger_base_stat(path)
    if present is False and not _ledger_delta_files(path):
        return ()
    try:
        # The records read is the ONE cached representation (fix 3 — the
        # ledger-rows| projection family is RETIRED); the whole-projection
        # here is rebuilt per call from that resident snapshot.
        _generation, records = _ledger_records_stamped(lake_root)
        return tuple(ledger_record_row(r) for r in records)
    except movers_read_skip_errors() as exc:
        # A wedged volume can still fail the READ itself (EIO inside
        # ``load_window_ledger``'s own stat — its never-raises contract does
        # not cover a raising ``Path.is_file``). Honest NAMED degrade: one
        # WARN + an empty grid this poll — never a 500; nothing is cached
        # (the raising compute stores nothing), so the next poll retries.
        _LOG.warning("db.window_ledger_read_degraded", path=path.as_posix(), error=str(exc))
        return ()


def window_ledger_rows_page(
    lake_root: Path, *, offset: int, limit: int
) -> tuple[int, tuple[tuple[Any, ...], ...]]:
    """ONE ``(total, page)`` slice of the window-ledger grid rows — O(page).

    DEC-MEMORY-FOOTPRINT-HARDENING-001 fix 3 — the ``/api/v1/db/window-ledger``
    route's read: slice the CACHED records FIRST, then project ONLY the page's
    records through the canonical
    :func:`~brutex.data.window_ledger.ledger_record_row` (projection is
    per-record pure, so slice-then-project is byte-identical to the retired
    cached whole-projection's ``rows[offset : offset + limit]``). A paging
    click within one ledger stamp is an O(1) records-cache handoff + an
    O(page ≤ *limit*) tuple projection — never a second resident whole-ledger
    rows family (the residency this fix collapses) and never an O(rows)
    re-projection per click. *offset* is clamped ``>= 0`` via the COMMON
    :func:`clamp_offset` (the route validates ``ge=0`` anyway); an offset past
    the end is a clean empty page with the real *total* (Python slice
    semantics, exactly the route's historical behaviour). A lake with no
    ledger Parquet AND no pending deltas returns ``(0, ())``.
    HONEST envelope (Golden Rule #10): the FIRST read per stamp still pays one
    O(rows) parse (the records cache's documented cost); staleness is exactly
    the records serve (see :func:`window_ledger_rows_cached` — single-phase,
    logged on the ``ledger-records`` key).
    """
    from brutex.data.window_ledger import ledger_record_row

    # Path derived from the pinned module-local constants (the #1431 fast-path
    # pattern — see :data:`_LEDGER_TABLE_DIRNAME`): even a FUNCTION-local
    # ``window_ledger_store`` import participates in the
    # ``window_ledger_store → coverage_reconcile → db_view`` cycle CodeQL
    # ``py/cyclic-import`` flags; the constants are test-pinned equal to
    # ``window_ledger_store.LEDGER_DIRNAME`` / ``LEDGER_FILENAME``.
    path = lake_root / _LEDGER_TABLE_DIRNAME / _LEDGER_TABLE_FILENAME
    # The SAME OSError-classed presence gate + named read degrade as
    # :func:`window_ledger_rows_cached` (DEC-WINDOWS-STAT-GUARD-LATCH-001,
    # merge-composed — the /db grid route rides THIS function, so the #1386
    # hardening must cover it too).
    _mtime, present = _ledger_base_stat(path)
    if present is False and not _ledger_delta_files(path):
        return (0, ())
    try:
        _generation, records = _ledger_records_stamped(lake_root)
    except movers_read_skip_errors() as exc:
        _LOG.warning("db.window_ledger_read_degraded", path=path.as_posix(), error=str(exc))
        return (0, ())
    safe_offset = clamp_offset(offset)
    page = records[safe_offset : safe_offset + limit]
    return (len(records), tuple(ledger_record_row(r) for r in page))


def _window_market_bucket(market: str) -> str:
    """Map a ledger ``market`` namespace to the page's display/filter bucket.

    Three buckets (DEC-COVERAGE-GROWW-NAMESPACE-001): ``"options"``
    (``groww-option``), ``"futures"`` (``groww-future`` — previously mislabelled),
    and ``"spot"`` (the ``groww`` spot namespace + any other/legacy catch-all).
    """
    if market == _OPTION_WINDOW_MARKET:
        return "options"
    if market == _FUTURE_WINDOW_MARKET:
        return "futures"
    return "spot"


def _window_counts_from_records(records: Iterable[Any]) -> dict[str, dict[str, int]]:
    """Fold ledger records into the per-bucket status counts — pure, O(records).

    The Python twin of :func:`_window_counts_total`'s SQL ``GROUP BY`` (ONE fold
    shape, CLAUDE.md §11 COMMON) — shared by the legacy
    :func:`build_window_payload` path and :func:`phase_progress_payload`'s
    double-failure fallback (DEC-LEDGER-DELTA-O-PAGE-001 — the resilient
    Python-fold degrade that survives a persistently malformed delta).
    """
    counts: dict[str, dict[str, int]] = {"spot": {}, "options": {}, "futures": {}}
    for r in records:
        bucket = counts[_window_market_bucket(r.market)]
        bucket[r.status] = bucket.get(r.status, 0) + 1
    return counts


def spot_window_rollup(lake_root: Path) -> dict[tuple[str, str], tuple[int, int, int]]:
    """Group the spot fetch-window ledger into ``(symbol, interval) → (resolved, pending, failed)``.

    The completeness source for the /coverage spot chips
    (DEC-SPOT-COVERAGE-COMPLETENESS-001 → DEC-COMPLETENESS-HONEST-EMPTY-001): counts come
    ONLY from the unified window ledger — ``resolved`` is the ledger's SKIP/SETTLE
    authority (:data:`~brutex.data.window_ledger.SETTLED_STATUSES` = SUCCESS+SKIPPED:
    a window with real data, OR a TERMINALLY-empty window — one promoted to SKIPPED
    because it was seen empty ``EMPTY_TERMINAL_THRESHOLD`` times or fast-settled as an
    old pre-listing gap), ``failed`` is ``FAILED``, and ``pending`` is ``PLANNED`` **OR
    a NON-terminal ``EMPTY``** — both are outstanding work a re-pull WILL attempt.

    DEC-COMPLETENESS-HONEST-EMPTY-001 (supersedes the chip half of
    DEC-EMPTY-RESOLVED-REPORTING-001): a NON-terminal ``EMPTY`` (status still ``EMPTY``,
    below the terminal threshold — typically a RECENT daily/option window the vendor
    has not published yet, which a later pull DID fill) now counts ``pending``, NOT
    resolved, so the chip never reads "full" while recent data is genuinely missing.
    An all-empty PRE-LISTING dataset still reads complete because its old windows
    fast-settle to ``SKIPPED`` (terminal → resolved). The LIVE progress-bar numerator
    (``RESOLVED_STATUSES``, window_ledger.py — still SUCCESS+SKIPPED+EMPTY so the bar
    never freezes mid-pull) is deliberately UNCHANGED: completion-chip honesty and
    progress-bar liveness are intentionally different authorities. The SKIP authority
    (``SETTLED_STATUSES``) is also unchanged — a below-threshold EMPTY is still
    re-fetched next pull; only what the CHIP counts changed. Option rows
    (``market == _OPTION_WINDOW_MARKET``) are excluded by the EXACT spot-market filter.

    A lake with NO ledger Parquet returns ``{}`` (every fetch-interval chip
    renders "no ledger record") — deliberately NO ``window_status.json``
    fallback, unlike ``build_window_payload``: that sidecar is a frozen,
    write-retired snapshot (DEC-STATUS-SIDECAR-WRITE-RETIRE-001) and a stale
    completeness claim is worse than an honest unknown.

    O(rows) once per ledger stamp — memoised as the ``spot-rollup|<path>`` key
    family on THE shared :data:`_LEDGER_READ_CACHE` (DEC-LEDGER-DELTA-O-PAGE-001;
    the stamp token = base mtime + the pending delta name set,
    DEC-LEDGER-DELTA-FLUSH-001, so a mid-pull delta append refreshes the chips
    exactly as a full save always did); O(1) per cached call, single-flight on a
    concurrent cold stamp. The rollup is stamped with the SERVED records
    generation (:func:`_ledger_records_stamped` — never the fresh stamp a
    floor-stale records serve did not reach), so a rollup built from floor-stale
    records stays generation-stale and recomputes once the records floor passes
    and this entry's own floor permits — never latched (the fix-round latch
    defect, Golden Rule #1). HONEST envelope (Golden Rule #10): staleness after
    a stamp move is TWO-phase — the records adaptive floor (``min(max(2 s, the
    parse's own duration), 120 s)``) serves the prior rollup first, and once it
    passes this ``spot-rollup`` entry serves stale under its OWN #920 floor
    (``min(max(2 s, the grouping's own duration), 120 s)``), so the two floors
    COMPOSE: worst-case staleness = records floor + rollup floor, EACH capped
    at 120 s (possibly spanning several ≥2 s delta flushes — a time bound,
    never a stamp-count bound). EVERY stale serve is logged
    (``movers_cache.stale_served``) on the key that actually served it —
    ``ledger-records`` in the first phase, ``spot-rollup`` in the second.
    """
    from brutex.data.window_ledger import EMPTY, FAILED, SETTLED_STATUSES
    from brutex.data.window_ledger_store import ledger_path

    path = ledger_path(lake_root)
    if not path.is_file() and not _ledger_delta_files(path):
        return {}
    # Records first (O(1) on a hit): the grouping is stamped with the generation
    # the records were ACTUALLY served at and closes over that exact snapshot.
    generation, records = _ledger_records_stamped(lake_root)

    def _compute() -> dict[tuple[str, str], tuple[int, int, int]]:
        counts: dict[tuple[str, str], list[int]] = {}
        for record in records:
            if record.market != _SPOT_WINDOW_MARKET:
                continue
            cell = counts.setdefault((record.security_id, record.interval), [0, 0, 0])
            if record.status in SETTLED_STATUSES:
                # SUCCESS + SKIPPED — terminally settled (real data, or genuinely-no-data)
                cell[0] += 1
            elif record.status == FAILED:
                cell[2] += 1
            elif record.status == EMPTY:
                # DEC-COMPLETENESS-HONEST-EMPTY-001: a NON-terminal EMPTY (status still
                # EMPTY, i.e. below the terminal threshold — typically a recent window the
                # vendor has not published yet) is OUTSTANDING work the next pull WILL
                # re-fetch, so the completeness CHIP counts it PENDING, never "full". This
                # is the chip fix ONLY; the live progress-bar numerator (RESOLVED_STATUSES,
                # which still counts EMPTY as progress so the bar never freezes mid-pull)
                # is deliberately UNCHANGED. A genuinely-OLD empty fast-settles to SKIPPED
                # (terminal → resolved above), so an all-empty PRE-LISTING dataset still
                # reads complete — only a recent maybe-late empty reads pending (the fix).
                cell[1] += 1
            else:  # PLANNED only — genuinely-unfetched work a re-pull WILL attempt
                cell[1] += 1
        return {k: (v[0], v[1], v[2]) for k, v in counts.items()}

    return _LEDGER_READ_CACHE.get_or_compute(
        f"spot-rollup|{path.as_posix()}",
        generation=generation,
        compute=_compute,
    )


#: Delay before the single torn-read retry (DEC-WINDOW-TORN-READ-RETRY-001). A reader-side
#: multi-open race against the atomic ledger writer (``os.replace`` landing between the
#: fast path's independent ``read_parquet`` opens — log-observed as a thrift
#: ``TProtocolException`` surfacing as a ``duckdb.Error``) clears as soon as every open
#: sees the SAME replaced file; ~100 ms comfortably outlasts one ``os.replace``.
_DUCKDB_RETRY_DELAY_S: Final[float] = 0.1


def _ledger_stat_fields(ledger_file: Path) -> dict[str, Any]:
    """Best-effort ledger-Parquet forensics for the torn-read fallback logs.

    Returns ``{"ledger_size": st_size, "ledger_mtime_ns": st_mtime_ns}`` from ONE
    ``stat`` of *ledger_file*, so a ``window_payload.duckdb_fallback`` /
    ``phase_progress.duckdb_fallback`` event records WHICH on-disk ledger revision
    the failed read saw (a mid-pull ``os.replace`` shows up as a moved mtime).
    NEVER raises — a stat failure (file replaced/removed between the read and the
    log) yields honest ``None`` fields instead of turning the diagnostic log into
    a new crash surface (Golden Rule #1).

    Parameters
    ----------
    ledger_file
        The resolved window-ledger Parquet path.

    Returns
    -------
    dict[str, Any]
        ``ledger_size`` / ``ledger_mtime_ns`` ints, or both ``None`` on ``OSError``.
    """
    try:
        st = ledger_file.stat()
    except OSError:
        return {"ledger_size": None, "ledger_mtime_ns": None}
    return {"ledger_size": st.st_size, "ledger_mtime_ns": st.st_mtime_ns}


_WindowCacheT = TypeVar("_WindowCacheT")


class WindowCountsCache(Protocol):
    """Generation-stamped cache contract for the ``/coverage/windows`` ledger reads.

    Structural (``Protocol``) so this module never hard-imports a concrete cache — the
    dashboard injects the COMMON generation-stamped LRU
    (:class:`~brutex.dashboard.movers_cache.MoversSnapshotCache`, reused per CLAUDE.md
    §11, DEC-COVERAGE-WINDOWS-O1-001). Mirrors :class:`CandleCountCache` but memoises
    whole values, not a bare int: the ``(bucketed_counts, total)`` GROUP BY tuple AND
    — since DEC-WINDOWS-MIDPULL-FLOOR-001 — the projected page-row list (the
    ``window-page|…`` key family), hence the method-level type variable. The name is
    kept for caller back-compat (every injector already wires this Protocol).
    """

    def get_or_compute(
        self,
        key: str,
        *,
        generation: int,
        compute: Callable[[], _WindowCacheT],
    ) -> _WindowCacheT:
        """Return *key*'s cached value for *generation*, computing on a miss."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed


def _window_severity_order_sql() -> str:
    """Build the severity-first ``ORDER BY`` CASE from :data:`_WINDOW_STATUS_ORDER`.

    ONE source of truth for the row order (FAILED, then EMPTY, … — actionable rows can
    never be hidden by the page cap), shared with the legacy Python sort. The status
    literals are TRUSTED module constants (never user input), so interpolating them is
    injection-safe.
    """
    whens = " ".join(
        f"WHEN '{status}' THEN {rank}"
        for status, rank in sorted(_WINDOW_STATUS_ORDER.items(), key=lambda kv: kv[1])
    )
    return f"CASE status {whens} ELSE 9 END"


def _fold_status_counts(
    grouped_rows: Iterable[tuple[Any, Any, Any]],
) -> tuple[dict[str, dict[str, int]], int]:
    """Fold ``(market, status, n)`` GROUP BY rows into the bucketed counts + total.

    The ONE fold (CLAUDE.md §11 COMMON) shared by :func:`_window_counts_total`
    (lifetime) and :func:`_window_counts_since` (per-run,
    DEC-TELEMETRY-SCOPE-BUDGET-RUNCOUNTS-001): each ``market`` namespace folds to
    the page's spot/options/futures bucket via the shared
    :func:`_window_market_bucket`. No rows → all-zero buckets + ``total=0``.
    """
    counts: dict[str, dict[str, int]] = {"spot": {}, "options": {}, "futures": {}}
    total = 0
    for market_value, status_value, n in grouped_rows:
        count = int(n)
        total += count
        bucket = counts[_window_market_bucket(str(market_value))]
        bucket[str(status_value)] = bucket.get(str(status_value), 0) + count
    return counts, total


#: The legacy ``sid|segment`` window key's sid extractor (DuckDB ``split_part``
#: is 1-based; the ``|`` is a literal in LIKE — no metacharacter).
_LEGACY_SID_EXPR: Final[str] = "split_part(security_id, '|', 1)"


@dataclass(frozen=True)
class _WindowScopeBind:
    """The R11 window-census scope predicate, bound for SQL (#1934).

    DEC-UNIVERSE-INDEX-ONLY-001 design §3 R11: ONE admit fragment applied
    IDENTICALLY to the page query, ``_window_counts_total`` and
    ``_window_counts_since`` — spot keys by the enumerated recovered forms
    (``TRIM`` + the exact/own-prefix variants of the admitted names), modern
    F&O contract keys by ``LIKE '<EXCH>-<U>-%'`` prefixes over the scope's
    F&O underlyings, legacy ``sid|segment`` keys by the admitted-sid list
    from the route's ``symbol_for_id`` map. The SQL predicate may be
    admit-side WIDER than the Python §3.5 form (a pathological non-vendor
    ``NSE-NIFTY-<junk>`` name over-admits into the census — the harmless
    direction; it NEVER excludes an in-scope row; stated + pinned). The
    ``legacy_unresolved`` fragment names the DISTINCT sub-count: a
    legacy-shape row whose sid the map does not know at all — never silently
    dropped, never misattributed purely to the DEC.
    """

    admit_sql: str
    admit_params: tuple[str, ...]
    legacy_unresolved_sql: str
    legacy_params: tuple[str, ...]
    #: Content-derived cache-key segment: digests the scope's admitted pairs +
    #: the sid map, so a §2.5 registration widening (scope content moves) AND
    #: a master reload (map content moves) both re-key the counts/page memos
    #: with NO generation bump (the #1650 fold law — content-keyed, which
    #: subsumes the design's master_revision+scope_revision fold: any move
    #: that changes the bound predicate moves the token).
    key_token: str


def _window_scope_bind(
    scope: FeedScope | None, symbol_for_id: Mapping[str, str] | None
) -> _WindowScopeBind | None:
    """Build the R11 SQL bind for a BOUNDED scope — ``None`` under unbounded.

    ``None`` (a defaulted caller / a ``full`` union) composes NO WHERE: the
    SQL text stays BYTE-identical to the pre-#1934 queries (the steady-state
    SQL-text byte pin + every direct-call window suite pass unchanged — §6
    Law 0).
    """
    if scope is None or not scope.bounded():
        return None
    names = scope_admitted_symbol_names(scope)
    spot_forms: list[str] = []
    for name in sorted(names):
        spot_forms.append(name)
        spot_forms.extend(f"{exchange.value}-{name}" for exchange in Exchange)
    fno_prefixes = [
        f"{exchange.value}-{symbol}-%"
        for exchange, symbol in sorted(
            scope.fno_underlyings or frozenset(), key=lambda pair: (pair[0].value, pair[1])
        )
    ]
    sid_map = dict(symbol_for_id or {})
    known_sids = sorted(sid_map)
    admitted_sids = sorted(sid for sid, symbol in sid_map.items() if symbol in names)
    arms: list[str] = []
    params: list[str] = []
    if spot_forms:
        arms.append(f"TRIM(security_id) IN ({', '.join('?' for _ in spot_forms)})")
        params.extend(spot_forms)
    arms.extend("security_id LIKE ?" for _ in fno_prefixes)
    params.extend(fno_prefixes)
    if admitted_sids:
        arms.append(
            f"(security_id LIKE '%|%' AND {_LEGACY_SID_EXPR} "
            f"IN ({', '.join('?' for _ in admitted_sids)}))"
        )
        params.extend(admitted_sids)
    admit_sql = "(" + " OR ".join(arms) + ")" if arms else "(1 = 0)"
    if known_sids:
        legacy_sql = (
            f"(security_id LIKE '%|%' AND {_LEGACY_SID_EXPR} "
            f"NOT IN ({', '.join('?' for _ in known_sids)}))"
        )
        legacy_params = tuple(known_sids)
    else:
        legacy_sql = "(security_id LIKE '%|%')"
        legacy_params = ()
    basis = "\x1e".join(
        (
            ",".join(
                sorted(f"{e.value}:{s}" for e, s in scope.admitted_underlyings or frozenset())
            ),
            ",".join(sorted(f"{e.value}:{s}" for e, s in scope.fno_underlyings or frozenset())),
            _window_symbol_map_token(symbol_for_id),
        )
    )
    return _WindowScopeBind(
        admit_sql=admit_sql,
        admit_params=tuple(params),
        legacy_unresolved_sql=legacy_sql,
        legacy_params=legacy_params,
        key_token="s=" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16],
    )


def _window_scope_key_token(bind: _WindowScopeBind | None) -> str:
    """The counts/page cache-key scope segment — ``"unfiltered"`` for ``None``.

    A defaulted caller keys SEPARATELY from the threaded routes (Rev 5 — one
    cache entry can never serve two divergent computes); the two production
    routes both thread the SAME scope + map and keep sharing ONE entry (the
    documented shared-entry economy, pinned).
    """
    return "unfiltered" if bind is None else bind.key_token


def _window_counts_total_scoped(
    conn: Any, source: _LedgerScanSource, bind: _WindowScopeBind
) -> tuple[dict[str, dict[str, int]], int, int, int]:
    """The SCOPED counts fold — ONE scan carrying the exclusion census.

    The SAME single ``GROUP BY`` scan as :func:`_window_counts_total` with the
    bind's predicate riding it as a classification CASE (admitted /
    legacy-unresolved / out-of-scope) — zero extra scans, zero Python folds
    over rows (design §3 R11: "the WHERE rides the same scan"). Returns
    ``(counts, total, out_of_scope, legacy_unresolved)`` where ``counts`` /
    ``total`` cover ONLY admitted windows (matched/capped derive from them)
    and ``out_of_scope`` INCLUDES the distinct ``legacy_unresolved``
    sub-count.
    """
    cursor = conn.execute(
        f"SELECT market, status, CASE WHEN {bind.admit_sql} THEN 'a' "  # noqa: S608  # nosec B608 — from_sql + bind fragments are trusted module-built constants; all values bound
        f"WHEN {bind.legacy_unresolved_sql} THEN 'l' ELSE 'x' END AS scope_class, "
        f"COUNT(*) FROM {source.from_sql} GROUP BY market, status, scope_class",
        [*bind.admit_params, *bind.legacy_params, *source.params],
    )
    admitted_rows: list[tuple[Any, Any, Any]] = []
    out_of_scope = 0
    legacy_unresolved = 0
    for market_value, status_value, scope_class, n in cursor.fetchall():
        if str(scope_class) == "a":
            admitted_rows.append((market_value, status_value, n))
            continue
        out_of_scope += int(n)
        if str(scope_class) == "l":
            legacy_unresolved += int(n)
    counts, total = _fold_status_counts(admitted_rows)
    return counts, total, out_of_scope, legacy_unresolved


def _window_counts_since_scoped(
    conn: Any, source: _LedgerScanSource, since_iso: str, bind: _WindowScopeBind
) -> tuple[dict[str, dict[str, int]], int]:
    """The per-run twin under a bounded scope — the SAME admit fragment as a WHERE."""
    cursor = conn.execute(
        f"SELECT market, status, COUNT(*) FROM {source.from_sql} "  # noqa: S608  # nosec B608 — from_sql + bind fragments are trusted module-built constants; all values bound
        "WHERE status != 'PLANNED' AND last_attempt_at IS NOT NULL "
        "AND last_attempt_at != '' AND last_attempt_at >= ? "
        f"AND {bind.admit_sql} GROUP BY market, status",
        [*source.params, since_iso, *bind.admit_params],
    )
    return _fold_status_counts(cursor.fetchall())


def _window_counts_total(
    conn: Any, source: _LedgerScanSource
) -> tuple[dict[str, dict[str, int]], int]:
    """ONE DuckDB ``GROUP BY market, status`` → bucketed counts + grand total.

    Replaces the O(N) Python count loop with a single columnar aggregate over the
    assembled ledger *source* (DEC-LEDGER-DELTA-O-PAGE-001: the steady-state
    ``read_parquet(?)`` literal, or mid-pull the latest-wins base+delta fold —
    O(total physical rows + delta footers) per execution, memoised by the caller
    behind the composite stamp token). The ``market`` namespace is folded to the
    page's spot/options/futures bucket via the shared :func:`_fold_status_counts`
    (CLAUDE.md §11 COMMON). An empty Parquet yields all-zero buckets + ``total=0``
    (the GROUP BY returns no rows).
    """
    cursor = conn.execute(
        f"SELECT market, status, COUNT(*) FROM {source.from_sql} GROUP BY market, status",  # noqa: S608  # nosec B608 — from_sql is a trusted module-built constant; all values bound
        list(source.params),
    )
    return _fold_status_counts(cursor.fetchall())


def _window_counts_since(
    conn: Any, source: _LedgerScanSource, since_iso: str
) -> tuple[dict[str, dict[str, int]], int]:
    """The PER-RUN twin of :func:`_window_counts_total` — windows attempted since *since_iso*.

    DEC-TELEMETRY-SCOPE-BUDGET-RUNCOUNTS-001. The lifetime GROUP BY answers "88 EMPTY
    ever"; this answers "24 EMPTY THIS run" by filtering to rows whose
    ``last_attempt_at`` (the ledger's per-window last-fetch stamp) is on/after the
    current pull's journal ``started`` instant. ATTEMPTED statuses only: ``PLANNED``
    rows are excluded explicitly AND carry a NULL/empty ``last_attempt_at`` anyway
    (never fetched — a planned row is not "this run's work done"). The comparison is
    lexicographic over the SAME aware-UTC ``datetime.isoformat()`` shape BOTH writers
    produce (the ledger stamps ``now.isoformat()`` from ``datetime.now(UTC)``; the
    journal stamps ``datetime.now(UTC).isoformat()``), which orders chronologically
    for that fixed shape. The WHERE predicates apply OUTSIDE the fold subquery
    (never a resurrected superseded row); source params bind FIRST, then
    *since_iso*. One columnar aggregate — the same envelope as the lifetime query,
    memoised by the caller behind the SAME stamp-token cache key family (plus the
    pull-start token).
    """
    cursor = conn.execute(
        f"SELECT market, status, COUNT(*) FROM {source.from_sql} "  # noqa: S608  # nosec B608 — from_sql is a trusted module-built constant; all values bound
        "WHERE status != 'PLANNED' AND last_attempt_at IS NOT NULL "
        "AND last_attempt_at != '' AND last_attempt_at >= ? "
        "GROUP BY market, status",
        [*source.params, since_iso],
    )
    return _fold_status_counts(cursor.fetchall())


#: The canonical 16-column ledger row projection, in ``LedgerRowTuple`` order
#: (DEC-DB-LATENCY-HARDENING-001). MUST equal ``window_ledger_store._COLUMNS``
#: — pinned by test, not imported (the :data:`_WINDOW_KEY_COLUMNS` precedent:
#: this module deliberately carries no module-level ``window_ledger_store``
#: edge — CodeQL ``py/cyclic-import``).
_LEDGER_ROW_COLUMNS: Final[tuple[str, ...]] = (
    *_WINDOW_KEY_COLUMNS,
    "status",
    "bars",
    "attempts",
    "last_error",
    "first_seen_at",
    "last_attempt_at",
    "empty_streak",
    "in_pull_fetches",
    "bars_net_new",
    "empty_retries",
    "transient_retries",
)


def _ledger_page_row(raw: Sequence[Any]) -> tuple[Any, ...]:
    """Coerce ONE DuckDB ledger page row to the canonical ``LedgerRowTuple`` shape.

    DEC-DB-LATENCY-HARDENING-001: the EXACT per-cell coercion the legacy Python
    parse applies (``window_ledger_store._rows_to_records`` — ``str``/``int`` on
    the required cells, ``None``-passthrough on the optional trio), so the fast
    path's tuples are byte-equal to :func:`window_ledger_rows_cached`'s. A
    ``None`` in ANY column the legacy parse either requires or DERIVES
    (``empty_streak`` / ``in_pull_fetches`` carry legacy per-row derivation
    rules this projection deliberately does not duplicate) raises ``ValueError``
    so :func:`query_window_ledger_page` defers the WHOLE request to the
    byte-identical legacy parse — never a divergent cell (Golden Rule #1).
    """
    (
        market,
        security_id,
        interval,
        window_start,
        window_end,
        status,
        bars,
        attempts,
        last_error,
        first_seen_at,
        last_attempt_at,
        empty_streak,
        in_pull_fetches,
        bars_net_new,
        empty_retries,
        transient_retries,
    ) = raw
    required = (
        market,
        security_id,
        interval,
        window_start,
        window_end,
        status,
        bars,
        attempts,
        first_seen_at,
        empty_streak,
        in_pull_fetches,
    )
    if any(cell is None for cell in required):
        raise ValueError(
            "window-ledger fast path: NULL in a required/derivable column — "
            "defer to the legacy Python parse"
        )
    return (
        str(market),
        str(security_id),
        str(interval),
        str(window_start),
        str(window_end),
        str(status),
        int(bars),
        int(attempts),
        None if last_error is None else str(last_error),
        str(first_seen_at),
        None if last_attempt_at is None else str(last_attempt_at),
        int(empty_streak),
        int(in_pull_fetches),
        None if bars_net_new is None else int(bars_net_new),
        None if empty_retries is None else int(empty_retries),
        None if transient_retries is None else int(transient_retries),
    )


def query_window_ledger_page(
    conn: Any,
    lake_root: Path,
    *,
    limit: int,
    offset: int,
    count_cache: WindowCountsCache | None = None,
    page_cache: WindowCountsCache | None = None,
    retry_sleep: Callable[[float], None] | None = None,
) -> tuple[tuple[tuple[Any, ...], ...], int]:
    """ONE ``/db`` window-ledger page + the real total — DuckDB-first, legacy-safe.

    DEC-DB-LATENCY-HARDENING-001 (MIGRATION.md #1719): the ``/api/v1/db/window-ledger``
    route previously served a 100-row page by paying ONE full
    ``load_window_ledger`` Python parse per ledger stamp move (measured
    7,177.5 ms of a 7,628 ms request at 400k rows — and the stamp moves
    repeatedly MID-PULL, so the "first read per stamp" cost recurred every few
    seconds). The page is now a DuckDB ``LIMIT``/``OFFSET`` projection over the
    SAME assembled :func:`_ledger_scan_source` the ``/coverage/windows`` fast
    paths scan (steady state = the bare ``read_parquet(?)`` literal; mid-pull =
    the :func:`~brutex.data.lake_read.dedup_latest_source_sql` latest-wins
    base+delta fold — provably the ``load_window_ledger`` fold), ordered by the
    5-part window key exactly as :meth:`WindowLedger.records` sorts (DuckDB's
    binary VARCHAR collation == Python's code-point ``str`` order over the
    UTF-8 key columns; a NULL in a KEY column would diverge — DuckDB orders
    NULLS LAST while the legacy parse coerces ``str(None)`` and sorts it
    lexicographically — but that shape is writer-UNREACHABLE today: the key
    columns are ``pa.string()`` cells rendered from non-null ``WindowKey``
    fields, and a page CONTAINING such a row defers via the NULL-cell guard
    anyway), with the total from ONE ``COUNT(*)`` over the fold.
    Both are memoised on the injected caches (``wledger-total|…`` /
    ``wledger-page|…`` key families) at the composite-stamp-token generation —
    a repeat click within one ledger stamp is an O(1) handoff.

    BYTE-PARITY + degrade law (the fast-equals-legacy pin,
    ``tests/unit/test_dashboard_db_view_window_ledger_page.py``): the projected
    tuples are cell-for-cell equal to ``window_ledger_rows_cached``'s slice. A
    shape the legacy parse handles with DERIVATION rules this query cannot
    reproduce — a legacy pre-R1 base missing any of the 5 optional columns
    (probed via one metadata-only ``LIMIT 0`` describe over the BASE file, the
    :func:`_build_window_payload_duckdb` precedent), or a NULL in a
    required/derivable cell — defers the WHOLE request to the byte-identical
    legacy path. A transient ``duckdb.Error`` (the torn-read replace race)
    retries ONCE after :data:`_DUCKDB_RETRY_DELAY_S`; a second failure (e.g. a
    persistently malformed delta, which the multi-file SQL read cannot skip
    file-granularly but ``load_window_ledger`` can) degrades to the legacy
    parse too — never a 500, never a divergent row. ``conn=None`` (no DuckDB
    connection wired) is the legacy path outright.

    HONEST envelope (Golden Rule #10): the page render is O(page) but each
    FRESH-stamp query is ONE O(total physical rows + pending delta footers)
    columnar scan (the ``query_candles`` envelope — NOT literal O(page); true
    O(page) needs a write-time row manifest, NOT claimed); what this fix
    removes is the O(rows) PYTHON record fold + full-tuple projection per
    stamp (the measured 7.2–8.8 s term), replaced by the columnar scan the
    coverage paths already pay. Repeat clicks within a stamp are O(1) hits.
    """
    # Path derived from the pinned module-local constants — deliberately NO
    # ``window_ledger_store`` import, not even function-local: the lazy import
    # still participates in the ``window_ledger_store → coverage_reconcile →
    # db_view`` cycle CodeQL ``py/cyclic-import`` flags (the
    # ``phase_progress_payload`` no-import-edge precedent).
    path = lake_root / _LEDGER_TABLE_DIRNAME / _LEDGER_TABLE_FILENAME
    if not path.is_file() and not _ledger_delta_files(path):
        return (), 0

    def _fast() -> tuple[tuple[tuple[Any, ...], ...], int]:
        """One fast-path attempt — re-assembles the scan source per attempt."""
        # Re-assembled per attempt (fresh delta listing), so the retry below
        # self-heals the save-time flock prune race (DEC-LEDGER-DELTA-O-PAGE-001).
        source = _ledger_scan_source(path)
        ledger_glob = path.as_posix()
        probed = False

        def _ensure_full_schema(c: Any) -> None:
            """Legacy-shape probe — LAZY, so a warm (fully-cached) click runs
            ZERO SQL. The _build_window_payload_duckdb precedent, but STRICTER:
            the /db grid projects ALL 16 columns, so ANY absent optional column
            defers to the legacy parse, whose per-row derivation rules this
            query deliberately does not duplicate. Metadata-only LIMIT 0
            describe over the BASE file ONLY — never the union_by_name'd delta
            list, whose NULL-fill would mask a legacy base's missing columns.
            Base-ABSENT (a first pull's delta-only checkpoint) skips it: every
            delta is written by `append_ledger_delta` with the full current
            schema (writer-pinned). Run at most once per fresh attempt; *c* is
            the calling compute's statement-isolated cursor (`_compute_cursor`
            — the flagged caches may run a compute on a background revalidator,
            concurrent with the request thread's statements; a production
            store conn isolates via `_LockedReadConn.cursor()`), closed by the
            compute's `finally` (`_close_cursor`) so the legacy-shape raise
            below — thrown AFTER a description-only read — can never strand a
            held read-lock view (round-1 BLOCKER hygiene)."""
            nonlocal probed
            if probed or not path.is_file():
                return
            available = {
                str(col[0])
                for col in (
                    c.execute("SELECT * FROM read_parquet(?) LIMIT 0", [ledger_glob]).description
                    or []
                )
            }
            if not set(_LEDGER_ROW_COLUMNS).issubset(available):
                raise ValueError("legacy-shape ledger Parquet — defer to the Python parse path")
            probed = True

        generation = revision_generation(source.stamp_token)
        select_sql = ", ".join(f'"{name}"' for name in _LEDGER_ROW_COLUMNS)
        order_sql = ", ".join(f'"{name}"' for name in _WINDOW_KEY_COLUMNS)

        def _total() -> int:
            c = _compute_cursor(conn)
            try:
                _ensure_full_schema(c)
                # COUNT(*) over the fold = the number of DISTINCT window keys (the
                # QUALIFY keeps exactly one row per key; a steady-state base holds
                # unique keys by construction — save writes the folded to_rows()).
                row = c.execute(
                    f"SELECT COUNT(*) FROM {source.from_sql}",  # noqa: S608  # nosec B608 — from_sql is a trusted module-built constant; all values bound
                    list(source.params),
                ).fetchone()
                return int(row[0]) if row else 0
            finally:
                # The legacy-shape probe raises AFTER a description-only read —
                # on a `_LockedReadConn` view that hold must be released here or
                # the store read lock leaks forever (round-1 BLOCKER hygiene).
                _close_cursor(c, conn)

        def _page() -> tuple[tuple[Any, ...], ...]:
            c = _compute_cursor(conn)
            try:
                _ensure_full_schema(c)
                # ORDER/LIMIT apply OUTSIDE the fold subquery (ghost-row safe); the
                # source params bind FIRST (the subquery precedes them in the text).
                cursor = c.execute(
                    f"SELECT {select_sql} FROM {source.from_sql} "  # noqa: S608  # nosec B608 — trusted module-built fragments; page bounds bound
                    f"ORDER BY {order_sql} LIMIT ? OFFSET ?",
                    [*source.params, limit, offset],
                )
                return tuple(_ledger_page_row(raw) for raw in cursor.fetchall())
            finally:
                _close_cursor(c, conn)

        if count_cache is None:
            total = _total()
        else:
            total = count_cache.get_or_compute(
                f"wledger-total|{ledger_glob}", generation=generation, compute=_total
            )
        if page_cache is None:
            rows = _page()
        else:
            # The page key folds (limit, offset) via one tuple repr (the
            # window-page precedent — unambiguous, no bare `|` joins).
            rows = page_cache.get_or_compute(
                f"wledger-page|{ledger_glob}|{(limit, offset)!r}",
                generation=generation,
                compute=_page,
            )
        return rows, total

    if conn is not None:
        try:
            return _fast()
        except ValueError as exc:
            # Legacy shape / NULL-in-derivable-cell — the legacy parse owns the
            # derivation rules; defer the whole request (byte-identical output).
            _LOG.warning(
                "window_ledger_page.fallback",
                fallback="legacy_shape",
                error=str(exc),
                **_ledger_stat_fields(path),
            )
        except duckdb.Error as exc:
            # Torn read (reader-side multi-open race vs the atomic writer) —
            # retry ONCE, then degrade to the legacy parse (which skips a
            # persistently malformed delta file-granularly).
            _LOG.warning(
                "window_ledger_page.duckdb_retry",
                error=str(exc),
                error_type=type(exc).__name__,
                **_ledger_stat_fields(path),
            )
            sleeper = _default_sleep if retry_sleep is None else retry_sleep
            sleeper(_DUCKDB_RETRY_DELAY_S)
            try:
                return _fast()
            except (duckdb.Error, ValueError) as exc2:
                _LOG.warning(
                    "window_ledger_page.duckdb_fallback",
                    fallback="rows",
                    error=str(exc2),
                    error_type=type(exc2).__name__,
                    **_ledger_stat_fields(path),
                )
    # LEGACY degrade — byte-identical rows to the pre-fix route (ONE row-shape
    # source, ``ledger_record_row``): the stamp-cached records sliced FIRST,
    # then ONLY the page projected (``window_ledger_rows_page`` —
    # DEC-MEMORY-FOOTPRINT-HARDENING-001 fix 3 merge-compose: the degrade arm
    # no longer pays the O(rows) whole-tuple projection per request). Serves
    # the no-conn caller, the legacy-shape ledger, and the double-failure
    # torn-read/bad-delta arm.
    total, page = window_ledger_rows_page(lake_root, offset=offset, limit=limit)
    return page, total


#: The three window-phase buckets, in pull order (spot → futures → options), the
#: SAME namespaces :func:`_window_counts_total` folds via :func:`_window_market_bucket`.
#: The historical F&O phase pulls FUTURES before OPTIONS (DEC-FNO-PULL-ORDER-LOOKBACK-001),
#: so this order matches the wire order the ledger fills.
WINDOW_PHASES: Final[tuple[str, ...]] = ("spot", "futures", "options")


def phase_window_progress(counts_for_bucket: Mapping[str, int]) -> dict[str, int]:
    """Project ONE segment's status counts into the live per-phase progress shape.

    *counts_for_bucket* is a single bucket of :func:`_window_counts_total`'s output —
    ``counts["spot"]`` / ``counts["futures"]`` / ``counts["options"]`` — a
    ``{STATUS: n}`` map (STATUS in PLANNED / SUCCESS / EMPTY / FAILED / SKIPPED). It
    returns the display tallies the Universe pull's per-phase rows read:

    * ``done`` — settled windows (``SUCCESS + SKIPPED``, the completeness authority,
      the SAME SUCCESS+SKIPPED set :data:`brutex.data.window_ledger.SETTLED_STATUSES`
      defines; re-declared inline as string literals so this pure display projection
      needs no window_ledger import).
    * ``total`` — every recorded window in the bucket (all five statuses summed), so
      a phase with NO windows honestly reads ``total == 0`` (never a fake value).
    * ``success`` / ``empty`` / ``failed`` — the honest per-status breakdown; a
      ``FAILED`` phase surfaces its ``failed`` count instead of being masked.
    * ``pending`` — ``PLANNED`` windows not yet fetched (the options flush-lag reads
      here as "pending", never a fabricated 0).

    O(1) — a handful of ``dict.get`` over at most five status keys, no scan. A missing
    status defaults to ``0`` (an all-zero / empty bucket → all-zero tallies).
    """
    success = counts_for_bucket.get("SUCCESS", 0)
    skipped = counts_for_bucket.get("SKIPPED", 0)
    empty = counts_for_bucket.get("EMPTY", 0)
    failed = counts_for_bucket.get("FAILED", 0)
    pending = counts_for_bucket.get("PLANNED", 0)
    done = success + skipped
    total = done + empty + failed + pending
    return {
        "done": done,
        "total": total,
        "success": success,
        "empty": empty,
        "failed": failed,
        "pending": pending,
    }


def fno_current_phase_from_counts(counts: Mapping[str, Mapping[str, int]]) -> str:
    """Refine the historical F&O phase's LIVE sub-phase (``futures`` vs ``options``) from counts.

    The pipeline sequencer knows only the COARSE phase (spot → metadata → historical);
    the historical phase fills FUTURES windows before OPTIONS (DEC-FNO-PULL-ORDER-LOOKBACK-001).
    Rather than thread a segment tag through the F&O spine's in-flight hook, derive the
    live sub-phase from the SAME cached ledger counts the rows already read (the mapping
    doc's lower-touch path): futures is "current" while it still has ``pending`` (PLANNED)
    work OR no windows have been planned yet, otherwise options has taken over. O(1) —
    two :func:`phase_window_progress` projections.
    """
    futures = phase_window_progress(counts.get("futures", {}))
    if futures["pending"] > 0 or futures["total"] == 0:
        return "futures"
    return "options"


def build_phase_progress(
    counts: Mapping[str, Mapping[str, int]], *, current_phase: str
) -> list[dict[str, Any]]:
    """Assemble the Universe pull's live per-phase progress rows — pure, O(1), COMMON.

    Builds ONE row per :data:`WINDOW_PHASES` segment (spot / futures / options, pull
    order) from *counts* (:func:`_window_counts_total`'s bucketed output) via
    :func:`phase_window_progress`, and flags the row that is currently in flight:

    * during the SPOT phase (*current_phase* ``== "spot"``) the spot row is current;
    * during the historical F&O phase (*current_phase* ``in {"futures", "options",
      "historical"}``) the current sub-phase is REFINED from the counts via
      :func:`fno_current_phase_from_counts` (futures until it drains, then options);
    * during the METADATA phase (or any other / idle / empty label) NO window row is
      current — metadata writes no windows, so the three rows stay honestly "pending"
      rather than fabricating a metadata phase row.

    Extracted here (not inlined in the route) so the whole assembly is unit-testable
    with plain dicts and ZERO network (CLAUDE.md §11 COMMON + the 100% gate). Returns
    a JSON-friendly list; each row carries ``phase``, ``current``, and the
    :func:`phase_window_progress` tallies.
    """
    if current_phase in ("futures", "options", "historical"):
        highlighted = fno_current_phase_from_counts(counts)
    elif current_phase == "spot":
        highlighted = "spot"
    else:
        # metadata / idle / unknown label — no window phase is in flight.
        highlighted = ""
    rows: list[dict[str, Any]] = []
    for phase in WINDOW_PHASES:
        row: dict[str, Any] = {"phase": phase, "current": phase == highlighted}
        row.update(phase_window_progress(counts.get(phase, {})))
        rows.append(row)
    return rows


def phase_progress_payload(
    conn: Any,
    ledger_file: Path,
    *,
    current_phase: str,
    count_cache: WindowCountsCache | None = None,
    generation: int = 0,
    retry_sleep: Callable[[float], None] | None = None,
    scope: FeedScope | None = None,
    symbol_for_id: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """The Universe pull's LIVE per-phase progress rows — REUSES the cached window counts.

    DEC-UNIVERSE-PER-PHASE-PROGRESS-001. Reads the per-segment window status counts
    through the SAME stamp-token-keyed ``_window_counts_total`` cache the
    ``/coverage/windows`` page uses (NO new scan — a repeat poll within one ledger
    stamp is an O(1) hit; two routes now share one cached entry), then projects
    them into the spot/futures/options phase rows via
    :func:`build_phase_progress` (highlighting the *current_phase*).

    *ledger_file* is the RESOLVED window-ledger Parquet path (the caller resolves it
    via :func:`brutex.data.window_ledger_store.ledger_path` — the SAME resolver the
    ``/coverage/windows`` route uses). Taking the resolved path (rather than importing
    the store's ``ledger_path`` here) keeps this module free of a
    ``db_view → window_ledger_store`` import edge (CodeQL ``py/cyclic-import``; the
    same reason :func:`_build_window_payload_duckdb` defers legacy derivation).

    None-safe by construction: a lake with NO ledger Parquet AND no pending deltas
    (a dry-run / a pull that has planned nothing) returns ``[]`` — the caller then
    serves an honest empty ``phases`` list, never a fabricated window count. When
    *count_cache* is ``None`` the counts are computed directly (used by the
    pure-fast-path tests); the production route always injects the cache. O(1) on a
    cache hit; on a miss it runs ONE ``GROUP BY market, status``
    (:func:`_window_counts_total`). The cache key is the STABLE
    ``window-counts|<path>`` family stamped at a generation DIGESTED from the
    composite ledger stamp token (:func:`~brutex.dashboard.movers_cache.revision_generation`
    — DEC-WINDOWS-MIDPULL-FLOOR-001, restructured from the prior
    stamp-token-IN-key / fixed-``generation=0`` shape of
    DEC-LEDGER-DELTA-O-PAGE-001 so the injected cache's opt-in #920 adaptive
    floor can actually engage: a floor needs a generation MISMATCH on one
    stable key, which a stamp-in-key layout structurally never produces). The
    stamp token stays the COMPLETE content signal — mid-pull ``mark_dirty``
    fires per window tick, far more often than the ≥2 s ledger flush, and a
    full save / delta append / cross-process CLI write each move the token →
    the generation (never stale past the floor, Golden Rule #1). The
    production cache (``app.py``, built by :func:`make_window_payload_caches`)
    rides the SAME probe/floor shape as the sibling :data:`_LEDGER_READ_CACHE`
    — the constant-``True`` :func:`_ledger_stamp_moved` probe + the 120 s
    :data:`_LEDGER_READ_FLOOR_CAP_S` cap — so ANY stamp churn (an in-process
    pull's delta flush AND a cross-process CLI pull's) may serve the PRIOR
    stamp's counts for at most ``min(max(2 s, the count's own compute
    duration), 120 s)`` per the #920 ≤50%-duty law, every stale serve logged
    ``movers_cache.stale_served`` — bounded + visible, never silent; steady
    state (immobile stamp) never reaches the floor branch. The *generation*
    parameter is retained for signature compatibility.

    **Mid-pull deltas (DEC-LEDGER-DELTA-O-PAGE-001, supersedes the O(records)
    Python branch of DEC-LEDGER-DELTA-FLUSH-001):** pending deltas are folded
    latest-wins IN SQL via the shared :func:`_ledger_scan_source` — the SAME
    counts query, scanning the fold subquery instead of the bare base file.
    HONEST envelope (Golden Rule #10): mid-pull the folded source is ONE O(total
    physical rows incl. every delta file's footer) columnar hash-partition per
    fresh stamp (the ≥2 s flush cadence bounds stamp movement), O(1)-cached
    between stamps via the composite stamp token; NOT literal O(page) — the same
    envelope ``query_candles`` already accepts. The O(delta-file-count)
    footer-open term grows over a multi-hour pull until the pull-end save
    subsumes the deltas; delta compaction is NAMED future work, not claimed.
    Steady state (no deltas) is byte-identical to before.

    TORN-READ resilience (DEC-WINDOW-TORN-READ-RETRY-001): a concurrent pull's atomic
    ``os.replace`` of the ledger Parquet landing between this reader's independent
    ``read_parquet`` opens — or a save-time flock prune unlinking a listed delta
    mid-query — surfaces as a transient ``duckdb.Error``. The counts read RETRIES
    ONCE after :data:`_DUCKDB_RETRY_DELAY_S` (via *retry_sleep*, injectable for
    deterministic tests; ``None`` → the real aliased :func:`time.sleep`) — the
    scan source is re-listed inside ``_counts()``, so the retry sees the pruned
    (subsumed) state. A SECOND ``duckdb.Error`` logs an enriched
    ``phase_progress.duckdb_fallback`` event and degrades HONESTLY: with deltas
    still pending it serves the resilient Python fold
    (:func:`_ledger_records_cached` → :func:`_window_counts_from_records`, which
    skips a persistently malformed delta file-granularly — the multi-file SQL
    read cannot), so a bad delta can never blank the pull-progress phases for the
    whole pull; base-only keeps today's honest ``[]``. Only ``duckdb.Error`` is
    guarded — no row projection exists here, so KeyError/ValueError stay
    genuine-bug propagation.
    """
    if not ledger_file.is_file() and not _ledger_delta_files(ledger_file):
        return []

    # R11 disposition (DEC-UNIVERSE-INDEX-ONLY-001, #1934): the pull-progress
    # route threads the SAME scope + sid-map components as /coverage/windows,
    # so the two routes KEEP sharing one cached counts entry and the phase
    # totals narrow WITH the census (a residual pre-cut stock PLANNED
    # population can no longer pin the futures sub-phase via phantom pending).
    scope_bind = _window_scope_bind(scope, symbol_for_id)
    scope_token = _window_scope_key_token(scope_bind)

    def _counts() -> dict[str, dict[str, int]]:
        """ONE counts read — direct or through the injected stamp-keyed cache."""
        # Re-assembled per attempt (fresh delta listing) so the torn-read retry
        # below self-heals the save-time prune race (DEC-LEDGER-DELTA-O-PAGE-001).
        source = _ledger_scan_source(ledger_file)
        if count_cache is None:
            if scope_bind is None:
                counts, _total = _window_counts_total(conn, source)
            else:
                counts, _total, _out, _legacy = _window_counts_total_scoped(
                    conn, source, scope_bind
                )
        else:
            # STABLE key + stamp-digest generation (DEC-WINDOWS-MIDPULL-FLOOR-001):
            # the composite stamp token (base mtime + delta name set) still carries
            # the COMPLETE content signal — a CROSS-PROCESS write (a CLI `brutex
            # fetch` while the dashboard is open) AND a mid-pull delta append both
            # move the generation — but riding it as the GENERATION of one stable
            # key (instead of inside the key) lets the injected cache's opt-in
            # #920 adaptive floor throttle mid-pull recomputes. The SAME key
            # `_build_window_payload_duckdb` uses, so the two routes share one
            # warm entry.
            # The compute runs on a statement-isolated cursor, closed after
            # (`_isolated_counts_total`): the flagged cache may execute it on a
            # background revalidator, concurrent with this thread's own
            # statements — and a production store conn's isolation rides
            # `_LockedReadConn.cursor()` (a fresh per-compute view; the shared
            # single-thread view corrupted its hold flag cross-thread and
            # leaked the store read lock — round-1 BLOCKER).
            counts_key = f"window-counts|{ledger_file.as_posix()}|{scope_token}"
            stamp_generation = revision_generation(source.stamp_token)
            if scope_bind is None:
                counts, _total = count_cache.get_or_compute(
                    counts_key,
                    generation=stamp_generation,
                    compute=lambda: _isolated_counts_total(conn, source),
                )
            else:
                bound_bind = scope_bind
                counts, _total, _out, _legacy = count_cache.get_or_compute(
                    counts_key,
                    generation=stamp_generation,
                    compute=lambda: _isolated_counts_total_scoped(conn, source, bound_bind),
                )
        return counts

    sleeper = _default_sleep if retry_sleep is None else retry_sleep
    try:
        counts = _counts()
    except duckdb.Error as exc:
        # Torn read (reader-side multi-open race vs the atomic writer) — retry ONCE.
        # The retry runs OUTSIDE any lock (the injected cache computes unlocked and
        # caches NOTHING on a raise — verified against MoversSnapshotCache), so the
        # second attempt recomputes cleanly and a retried success is cached normally.
        _LOG.warning(
            "phase_progress.duckdb_retry",
            error=str(exc),
            error_type=type(exc).__name__,
            **_ledger_stat_fields(ledger_file),
        )
        sleeper(_DUCKDB_RETRY_DELAY_S)
        try:
            counts = _counts()
        except duckdb.Error as exc2:
            if _ledger_delta_files(ledger_file):
                # Deltas still pending after two failures — a persistently
                # malformed delta, which the multi-file SQL read cannot skip
                # file-granularly. Serve the resilient Python fold instead of
                # blanking the phases mid-pull (DEC-LEDGER-DELTA-O-PAGE-001;
                # `load_window_ledger` skips the bad delta with
                # `window_ledger.delta_load_failed`). ``ledger_file`` is
                # ``<lake>/window_ledger/ledger.parquet``, so the lake root is
                # two levels up.
                _LOG.warning(
                    "phase_progress.duckdb_fallback",
                    fallback="records",
                    error=str(exc2),
                    error_type=type(exc2).__name__,
                    **_ledger_stat_fields(ledger_file),
                )
                records = _ledger_records_cached(ledger_file.parent.parent)
                if scope is not None and scope.bounded():
                    # The SAME §3.5 window predicate as the payload fold —
                    # the degraded phases never widen past the census (R11).
                    records = tuple(
                        r
                        for r in records
                        if classify_window_row(
                            str(r.security_id), scope, admitted_sids=symbol_for_id
                        )
                        == WINDOW_ADMITTED
                    )
                return build_phase_progress(
                    _window_counts_from_records(records), current_phase=current_phase
                )
            _LOG.warning(
                "phase_progress.duckdb_fallback",
                fallback="empty",
                error=str(exc2),
                error_type=type(exc2).__name__,
                **_ledger_stat_fields(ledger_file),
            )
            return []
    return build_phase_progress(counts, current_phase=current_phase)


def _window_symbol_map_token(symbol_for_id: Mapping[str, str] | None) -> str:
    """Digest the id→symbol display map into a stable page-cache key segment.

    DEC-WINDOWS-MIDPULL-FLOOR-001: the cached page ROWS embed the decoded option
    symbols (:func:`_window_row` via *symbol_for_id*), so the page key must move
    when the map's CONTENT moves (an instrument-master reload mid-session) or a
    cached page would keep serving the old display names — a stale illusion
    (Golden Rule #1). Pure + deterministic: an order-independent SHA-256 over the
    sorted items, ``"0"`` for ``None``/empty (the two key identically — both
    project raw coordinates). O(map) per call (~750 entries ≈ tens of µs —
    dwarfed by the O(instruments) store read that builds the map each request).
    ``\\x1f``/``\\x1e`` separators make ``(key, value)`` pair boundaries
    unambiguous.
    """
    if not symbol_for_id:
        return "0"
    digest = hashlib.sha256()
    for key in sorted(symbol_for_id):
        digest.update(key.encode("utf-8"))
        digest.update(b"\x1f")
        digest.update(symbol_for_id[key].encode("utf-8"))
        digest.update(b"\x1e")
    return digest.hexdigest()[:16]


def _build_window_payload_duckdb(
    conn: Any,
    ledger_file: Path,
    *,
    symbol_for_id: Mapping[str, str] | None,
    market: str | None,
    status: str | None,
    limit: int,
    offset: int,
    count_cache: WindowCountsCache | None,
    generation: int,
    since: str | None = None,
    page_cache: WindowCountsCache | None = None,
    scope: FeedScope | None = None,
) -> dict[str, Any]:
    """The ``/coverage/windows`` payload via DuckDB — stamp-cached counts AND page rows.

    TRUE envelope first (Golden Rule #10 — DEC-WINDOWS-MIDPULL-FLOOR-001
    corrects the prior "O(page)-on-repeat" headline, which was stale for the
    mid-pull ACCUMULATION regime): the counts are stamp-cached
    (:func:`_window_counts_total`, ONE ``GROUP BY`` per fresh stamp) and the row
    PAGE is now ALSO stamp-cached (the ``window-page|…`` key family below), so a
    repeat poll WITHIN one ledger stamp is an O(1) cache handoff for both — but
    every FRESH stamp still pays ONE O(total physical rows + every pending delta
    file's Parquet footer) columnar fold PER cached family, and mid-pull the
    pending-delta count grows ~1 file/2 s until the pull-end save prunes them
    (a multi-hour pull reaches thousands — measured 2026-07-09: the warm poll
    grew 33 ms @ 50 deltas → 158 ms @ 400 pre-fix because the page query re-ran
    per poll). NOT literal O(page); delta COMPACTION stays named future work.

    Counts come from ONE stamp-cached ``GROUP BY`` (:func:`_window_counts_total`);
    the row page is a ``WHERE``-filtered, severity-ordered ``LIMIT``/``OFFSET`` query — so
    a tab/filter click reads only the page instead of re-running an O(N) Python
    count+filter+sort over every window on every click + 4 s poll (the root cause). The
    ``matched`` total is DERIVED from the cached counts (no extra scan); the per-row
    projection reuses the shared :func:`_window_row` (ONE shape with the legacy path).
    Returns the same payload dict shape + values as the pyarrow path — severity-first,
    with within-a-status-tier rows ordered deterministically by ``(security_id,
    interval, window_start, window_end)``.

    **Mid-pull deltas (DEC-LEDGER-DELTA-O-PAGE-001 + DEC-WINDOWS-MIDPULL-FLOOR-001):**
    pending ledger deltas are folded latest-wins IN SQL — every query scans the
    ONE assembled :func:`_ledger_scan_source` (the steady-state
    ``read_parquet(?)`` literal when no deltas exist — SQL text byte-identical,
    spy-pinned — else the :func:`~brutex.data.lake_read.dedup_latest_source_sql`
    fold subquery). The cache keys are STABLE per family
    (``window-counts|<path>`` / ``window-counts-since|<path>|<since>`` /
    ``window-page|<path>|<filters+page+symbol-map token>``) and stamped at a
    generation DIGESTED from the composite stamp token
    (:func:`~brutex.dashboard.movers_cache.revision_generation` — restructured
    from the prior stamp-token-IN-key / fixed-``generation=0`` shape so the
    injected cache's opt-in #920 adaptive floor can engage: a floor needs a
    generation MISMATCH on one stable key). The stamp token stays the COMPLETE
    content signal (a full save moves the base mtime, an append/prune moves the
    delta name set, a CLI cross-process write moves both); mid-pull
    ``mark_dirty`` fires per window tick, far more often than the ≥2 s ledger
    flush, so the app-side write generation is deliberately NOT used here. The
    *generation* parameter is retained for signature compatibility. The counts
    ride the injected *count_cache*; the page rows ride the injected
    *page_cache* — a SMALL DEDICATED instance
    (:data:`WINDOW_PAGE_CACHE_CAPACITY`; a page entry holds up to ``min(limit,
    WINDOW_ROWS_CAP)`` projected row-dicts, so sharing the 256-cap counts
    instance risked GB-scale retention and page-churn eviction of the counts
    entries). Both production instances (``app.py``, built by
    :func:`make_window_payload_caches`) ride the SAME probe/floor shape as the
    sibling :data:`_LEDGER_READ_CACHE` — the constant-``True``
    :func:`_ledger_stamp_moved` probe + the 120 s
    :data:`_LEDGER_READ_FLOOR_CAP_S` cap — so ANY stamp churn (an in-process
    pull's delta flush AND a cross-process CLI pull's) may serve a family's
    PRIOR-stamp entry for at most ``min(max(2 s, that family's own compute
    duration), 120 s)`` (the #920 ≤50%-duty law), every stale serve logged
    ``movers_cache.stale_served`` — bounded + visible, never silent; steady
    state (immobile stamp) never reaches the floor branch. The families floor
    independently, so one bounded-stale poll may mix counts and rows from
    adjacent stamps (≤ one floor window apart, self-heals on the next
    past-floor poll).

    *since* (DEC-TELEMETRY-SCOPE-BUDGET-RUNCOUNTS-001): the current pull's journal
    ``started`` wall-UTC instant, or ``None`` when no real pull-start is known.
    When set, the payload ADDITIONALLY carries ``counts_since_pull_start`` (the
    :func:`_window_counts_since` per-run buckets, memoised in the SAME cache keyed
    stamp-token+pull-start) and echoes it as ``pull_start_utc``. ``None`` adds
    ZERO keys and ZERO queries — the payload stays byte-identical (never a
    fabricated per-run figure, Golden Rule #1).
    """
    from types import SimpleNamespace

    # Re-assembled per invocation (fresh delta listing), so build_window_payload's
    # retry-once machinery re-lists after a save-time flock prune race
    # (DEC-LEDGER-DELTA-O-PAGE-001) — the second attempt sees the subsumed state.
    source = _ledger_scan_source(ledger_file)
    ledger_glob = ledger_file.as_posix()
    if ledger_file.is_file():
        # A LEGACY pre-R1 Parquet lacks the empty_streak/in_pull_fetches columns; the legacy
        # pyarrow path DERIVES them faithfully (window_ledger_store derive helpers), so DEFER
        # the whole payload to it — the raise lands in build_window_payload's fallback — rather
        # than re-deriving here, which would add a db_view → window_ledger_store import cycle
        # (CodeQL py/cyclic-import). The probe is a metadata-only ``LIMIT 0`` describe over the
        # BASE file ONLY — never the union_by_name'd delta list, whose NULL-fill would mask a
        # legacy base's missing columns until the int() projection below crashed. A wrong-shape
        # Parquet (missing a core column) also fails the subset → defers. When the base is
        # ABSENT (a first pull's delta-only checkpoint) the probe is SKIPPED — every delta is
        # written by `append_ledger_delta` with the full 14-column schema (writer-pinned), so
        # the residual union NULL-fill is `bars_net_new`-only on a base that predates that
        # optional column — equal to the Python fold's honest ``None``, never fed to ``int()``.
        # ISOLATED cursor + close (DEC-DASHBOARD-PULL-CONTENTION-RESILIENCE-001
        # deadlock fix): this description-only probe was the LAST remaining
        # statement on the request's SHARED ``_LockedReadConn`` view — with no
        # fetch and no close it left the store read RLock HELD by the request's
        # worker thread at request end (pre-round-1 the later counts/page
        # statements on the SAME shared view rebalanced it via ``execute``'s
        # leading ``_release()``; the round-1 per-compute ``cursor()`` isolation
        # moved them off the shared view, so nothing rebalanced), and the NEXT
        # request dispatched to a DIFFERENT worker thread deadlocked forever on
        # ``_lock.acquire()`` (reproduced: the CI unit-gate hang at
        # test_windows_ledger_fast_path_through_real_connection; py-spy-traced
        # to the instrument_store ``_LockedReadConn.execute`` acquire). The
        # probe now follows the same ``_compute_cursor``/``_close_cursor``
        # discipline as every other statement in this function — its view
        # releases the lock in the ``finally`` even on the legacy-shape raise
        # below.
        probe = _compute_cursor(conn)
        try:
            available = {
                str(col[0])
                for col in (
                    probe.execute(
                        "SELECT * FROM read_parquet(?) LIMIT 0", [ledger_glob]
                    ).description
                    or []
                )
            }
        finally:
            _close_cursor(probe, conn)
        if not {"empty_streak", "in_pull_fetches"}.issubset(available):
            raise ValueError("legacy-shape ledger Parquet — defer to the pyarrow derive path")
    counts_since: dict[str, dict[str, int]] | None = None
    # STATUS ALLOW-LIST (DEC-WINDOWS-MIDPULL-FLOOR-001, round-2 hardening):
    # an unknown ?status= value matches nothing on BOTH payload paths (legacy
    # parity), but each DISTINCT bogus value would otherwise mint a fresh
    # page-cache key + pay a full fold — so every not-known status collapses
    # onto ONE canonical sentinel BEFORE key minting, and the sentinel's page
    # arm is the literal `1 = 0` (never `status = <sentinel>` — airtight even
    # against a hypothetical stored row equal to the sentinel string).
    wanted_status_raw = status.upper() if status else None
    wanted_status = (
        _UNKNOWN_WINDOW_STATUS
        if wanted_status_raw is not None and wanted_status_raw not in _WINDOW_STATUS_ORDER
        else wanted_status_raw
    )
    # STABLE keys + a stamp-digest generation (DEC-WINDOWS-MIDPULL-FLOOR-001,
    # restructured from stamp-token-IN-key at generation=0): the composite stamp
    # token (base mtime + the pending delta name set) still carries the COMPLETE
    # content signal — a CROSS-PROCESS write (a CLI `brutex fetch` while the
    # dashboard is open — which does NOT bump the dashboard's own in-process
    # generation) AND a mid-pull delta append both move the generation, while a
    # mark_dirty storm at an unchanged ledger never thrashes the entry — but
    # riding it as the GENERATION of one stable key lets the injected cache's
    # opt-in #920 adaptive floor throttle mid-pull recomputes (a floor needs a
    # generation mismatch on the SAME key). A repeat click within one stamp
    # stays an O(1) hit.
    stamp_generation = revision_generation(source.stamp_token)
    # R11 scope bind (DEC-UNIVERSE-INDEX-ONLY-001, #1934): ONE admit fragment
    # applied identically to the counts + since + page queries; a None bind
    # (defaulted caller / unbounded union) composes NO WHERE — SQL text
    # byte-identical — and keys under the DISTINCT "unfiltered" token.
    scope_bind = _window_scope_bind(scope, symbol_for_id)
    scope_token = _window_scope_key_token(scope_bind)
    out_of_scope = 0
    legacy_unresolved = 0
    if count_cache is None:
        if scope_bind is None:
            counts, total = _window_counts_total(conn, source)
        else:
            counts, total, out_of_scope, legacy_unresolved = _window_counts_total_scoped(
                conn, source, scope_bind
            )
        if since is not None:
            if scope_bind is None:
                counts_since, _since_total = _window_counts_since(conn, source, since)
            else:
                counts_since, _since_total = _window_counts_since_scoped(
                    conn, source, since, scope_bind
                )
    else:
        # Computes run on a statement-isolated cursor, closed after
        # (`_isolated_counts_total`): the flagged caches may execute them on a
        # background revalidator daemon, concurrent with this thread's own
        # statements — a production store conn isolates via
        # `_LockedReadConn.cursor()` (fresh per-compute view, round-1 BLOCKER).
        if scope_bind is None:
            counts, total = count_cache.get_or_compute(
                f"window-counts|{ledger_glob}|{scope_token}",
                generation=stamp_generation,
                compute=lambda: _isolated_counts_total(conn, source),
            )
        else:
            bound_bind = scope_bind
            counts, total, out_of_scope, legacy_unresolved = count_cache.get_or_compute(
                f"window-counts|{ledger_glob}|{scope_token}",
                generation=stamp_generation,
                compute=lambda: _isolated_counts_total_scoped(conn, source, bound_bind),
            )
        if since is not None:
            # SAME memo pattern as the lifetime counts (REUSE, no new cache class —
            # DEC-TELEMETRY-SCOPE-BUDGET-RUNCOUNTS-001): the pull-start token joins the
            # key so a NEW pull (new journal `started`) can never be served the prior
            # run's per-run figures even across the LRU's retained stamps.
            since_key = f"window-counts-since|{ledger_glob}|{since}|{scope_token}"
            if scope_bind is None:
                counts_since, _since_total = count_cache.get_or_compute(
                    since_key,
                    generation=stamp_generation,
                    compute=lambda: _isolated_counts_since(conn, source, since),
                )
            else:
                since_bind = scope_bind
                counts_since, _since_total = count_cache.get_or_compute(
                    since_key,
                    generation=stamp_generation,
                    compute=lambda: _isolated_counts_since_scoped(conn, source, since, since_bind),
                )

    valid_buckets = ("spot", "options", "futures")
    if market is None:
        buckets = list(valid_buckets)
    elif market in valid_buckets:
        buckets = [market]
    else:
        # An unrecognized non-null market matches NO bucket — mirror the legacy filter
        # (which compares each record's bucket to `market`) so matched=0 + no rows, rather
        # than the fast path silently returning every row (hostile-review MINOR).
        buckets = []
    if wanted_status:
        matched = sum(counts[b].get(wanted_status, 0) for b in buckets)
    else:
        # PLANNED rows ship ONLY on an explicit ?status=PLANNED (they are the bulk of a
        # freshly-planned pull and carry no per-row info beyond their count).
        matched = sum(v for b in buckets for s, v in counts[b].items() if s != "PLANNED")

    rows: list[dict[str, Any]] = []
    # Counts-only mode (DEC-WINDOWS-COUNTS-ONLY-001, ``limit=0``): the consumers
    # that render ONLY counts/meta (coverage.html's windows KPI card +
    # universe.html's all-segments line) SKIP the row-page query entirely — not
    # even a ``LIMIT 0`` plan against the (mid-pull delta-folded) scan source —
    # and (pack-train fold) the page memo below (DEC-WINDOWS-MIDPULL-FLOOR-001)
    # is neither read nor key-minted at ``limit=0``. ``matched`` above is
    # already derived from the stamp-cached counts, so the counts-only payload
    # costs ZERO row-page work; ``capped`` below stays the honest
    # ``matched > limit`` (matching rows exist, none shipped).
    if limit > 0:

        def _page_rows() -> list[dict[str, Any]]:
            """Run the ONE page query + row projection — byte-identical to the
            pre-cache inline body (pinned by
            ``test_page_cache_fresh_compute_byte_identical``); cached per stamp
            below so a repeat poll skips the O(total rows + delta footers) fold."""
            where: list[str] = []
            where_params: list[Any] = []
            if market == "options":
                where.append("market = ?")
                where_params.append(_OPTION_WINDOW_MARKET)
            elif market == "futures":
                where.append("market = ?")
                where_params.append(_FUTURE_WINDOW_MARKET)
            elif market == "spot":
                where.append("market NOT IN (?, ?)")
                where_params.extend((_OPTION_WINDOW_MARKET, _FUTURE_WINDOW_MARKET))
            elif market is not None:
                where.append("1 = 0")  # unrecognized market → no rows (legacy parity)
            if wanted_status == _UNKNOWN_WINDOW_STATUS:
                where.append("1 = 0")  # unknown status → no rows (legacy parity, ONE key)
            elif wanted_status:
                where.append("status = ?")
                where_params.append(wanted_status)
            else:
                where.append("status != ?")
                where_params.append("PLANNED")
            if scope_bind is not None:
                # The SAME admit fragment as the counts scan (R11 — one
                # predicate, three queries; the page can never disagree with
                # matched/counts on which rows are in scope).
                where.append(scope_bind.admit_sql)
                where_params.extend(scope_bind.admit_params)
            where_sql = (" WHERE " + " AND ".join(where)) if where else ""
            order_sql = (
                f" ORDER BY {_window_severity_order_sql()}, "
                '"security_id", "interval", "window_start", "window_end"'
            )
            # WHERE / ORDER / LIMIT apply OUTSIDE the fold subquery (ghost-row safe —
            # a superseded row version can never resurrect through a filter); the
            # source params bind FIRST (the subquery precedes them in the SQL text).
            # Statement-isolated cursor, closed after: this compute may run on the
            # flagged page cache's background revalidator, concurrent with the
            # request thread's own statements — a production store conn isolates
            # via `_LockedReadConn.cursor()` (fresh per-compute view; the shared
            # single-thread view leaked the store read lock — round-1 BLOCKER).
            params: list[Any] = [*source.params, *where_params, limit, offset]
            isolated = _compute_cursor(conn)
            try:
                cursor = isolated.execute(
                    f"SELECT * FROM {source.from_sql}{where_sql}{order_sql} LIMIT ? OFFSET ?",  # noqa: S608  # nosec B608 - SELECT * over a module-built trusted source; filters/page/order are bound or trusted constants
                    params,
                )
                out_columns = [str(col[0]) for col in (cursor.description or [])]
                raw_rows = cursor.fetchall()
            finally:
                _close_cursor(isolated, conn)
            page: list[dict[str, Any]] = []
            for raw in raw_rows:
                # DuckDB returns native Python values (str for the string columns, int for the
                # int64 ones, None for a null) — pass them straight to the shared `_window_row`
                # exactly as the legacy path forwards the `WindowLedgerRecord` attributes (ONE
                # projection shape, CLAUDE.md §11 COMMON; `_decode_option_window`/`format_ist_clock`
                # already str()/None-handle each field). This page always carries
                # empty_streak/in_pull_fetches: a legacy pre-R1 BASE that lacks them was already
                # deferred to the pyarrow derive path by the base-only probe, and a base-LESS
                # delta-only scan reads files `append_ledger_delta` writes with the full
                # 14-column schema (writer-pinned) — so read them directly.
                cell = dict(zip(out_columns, raw, strict=True))
                record = SimpleNamespace(
                    market=cell["market"],
                    security_id=cell["security_id"],
                    interval=cell["interval"],
                    window_start=cell["window_start"],
                    window_end=cell["window_end"],
                    status=cell["status"],
                    bars=cell["bars"],
                    attempts=cell["attempts"],
                    last_error=cell["last_error"],
                    last_attempt_at=cell["last_attempt_at"],
                    empty_streak=int(cell["empty_streak"]),
                    in_pull_fetches=int(cell["in_pull_fetches"]),
                )
                page.append(
                    _window_row(
                        record, first_seen_at=cell.get("first_seen_at"), symbol_for_id=symbol_for_id
                    )
                )
            return page

        if page_cache is None:
            rows = _page_rows()
        else:
            # The page-row cache (DEC-WINDOWS-MIDPULL-FLOOR-001): the row-page query
            # re-ran the WHOLE O(total rows + delta footers) fold on EVERY poll even
            # within one stamp (only the counts were cached — measured 33 ms @ 50
            # pending deltas → 158 ms @ 400 per warm poll, 2026-07-09). Memoise the
            # projected rows per (stamp, filters, page, symbol-map content) on the
            # injected DEDICATED page instance (the SAME MoversSnapshotCache class —
            # no new cache class, CLAUDE.md §11 — but its OWN small-capacity
            # instance, `make_window_payload_caches`: a page entry holds up to
            # min(limit, WINDOW_ROWS_CAP) row-dicts, so sharing the 256-cap counts
            # instance risked GB-scale retention AND page-key churn evicting the
            # tiny counts entries — round-2 MEDIUM fold): a repeat poll within one
            # stamp is an O(1) handoff; a fresh stamp recomputes ONCE per page
            # coordinate; the small LRU bounds retained pages. The filter/page
            # segment rides one tuple `repr` (unambiguous — a client-supplied
            # `market`/`status` string can never forge a key collision through a
            # bare `|` join; unknown statuses are already collapsed onto the ONE
            # sentinel above); the symbol-map token keeps a master reload from
            # serving stale display names. Counts-only calls (``limit=0``,
            # DEC-WINDOWS-COUNTS-ONLY-001) never reach this branch — no page-cache
            # read and no key mint (the pack-train fold nests the memo inside the
            # ``limit > 0`` guard).
            page_key = (
                f"window-page|{ledger_glob}|"
                f"{(market, wanted_status, limit, offset, _window_symbol_map_token(symbol_for_id))!r}"
                f"|{scope_token}"
            )
            rows = page_cache.get_or_compute(
                page_key, generation=stamp_generation, compute=_page_rows
            )
    payload: dict[str, Any] = {
        "source": "ledger",
        "count": total,
        "counts": counts,
        "empty_threshold": EMPTY_TERMINAL_THRESHOLD,
        "matched": matched,
        "returned": len(rows),
        "capped": matched > limit,
        "windows": rows,
    }
    if out_of_scope:
        # EMIT-ON-NONZERO census (DEC-UNIVERSE-INDEX-ONLY-001 R11): the
        # excluded window count naming the DEC, with the DISTINCT
        # legacy-unresolved sub-count (an unmapped legacy sid is never
        # silently dropped, never misattributed purely to the DEC).
        payload["out_of_scope"] = {
            "windows": out_of_scope,
            "legacy_unresolved": legacy_unresolved,
            "dec": "DEC-UNIVERSE-INDEX-ONLY-001",
        }
    if counts_since is not None:
        # Per-run honesty keys (DEC-TELEMETRY-SCOPE-BUDGET-RUNCOUNTS-001) — present
        # ONLY when a real pull-start is known; an unknown start OMITS them entirely
        # (the page then shows the lifetime figure alone, never a fabricated 0).
        payload["counts_since_pull_start"] = counts_since
        payload["pull_start_utc"] = since
    return payload


def last_pull_start_utc(lake_root: Path) -> str | None:
    """The wall-UTC instant the LAST real (non-dry-run) pull STARTED — or ``None``.

    DEC-TELEMETRY-SCOPE-BUDGET-RUNCOUNTS-001. Reads the durable pull journal
    (``<lake_root>/journal/pull_journal.jsonl`` — written by the dashboard
    spot/pipeline pull's ``record_pull_start``, DEC-DOCTOR-LIVE-PROOF-KIT-001) and
    returns the LATEST ``started`` record's ``wall_utc``, SKIPPING dry-run starts
    (a dry-run touches no ledger row, so anchoring the per-run window on it would
    zero the figure right after a real pull). HONEST ``None`` when no journal /
    no real ``started`` record exists — the journal is written ONLY by
    ``pull_all_spot`` today (an F&O-only historical pull and the CLI ``brutex
    fetch`` write none), and the caller then OMITS the per-run figure entirely,
    never fabricating one (Golden Rule #1). O(journal bytes) per call — the
    tolerant reader skips crash-truncated lines; lazy import keeps this module's
    import surface unchanged.
    """
    from brutex.observability.pull_journal import EVENT_STARTED, read_journal

    for event in reversed(read_journal(lake_root)):
        if event.event == EVENT_STARTED and event.dry_run is not True:
            return event.wall_utc
    return None


def build_window_payload(
    lake_root: Path,
    *,
    symbol_for_id: Mapping[str, str] | None = None,
    market: str | None = None,
    status: str | None = None,
    limit: int = WINDOW_ROWS_CAP,
    offset: int = 0,
    conn: Any = None,
    count_cache: WindowCountsCache | None = None,
    generation: int = 0,
    since: str | None = None,
    retry_sleep: Callable[[float], None] | None = None,
    page_cache: WindowCountsCache | None = None,
    scope: FeedScope | None = None,
) -> dict[str, Any]:
    """Project the per-window state for ``/coverage/windows`` — the unified ledger if present.

    *since* (DEC-TELEMETRY-SCOPE-BUDGET-RUNCOUNTS-001): the current pull's journal
    ``started`` wall-UTC instant (see :func:`last_pull_start_utc`). Honoured on the
    DuckDB fast path ONLY — the payload then adds ``counts_since_pull_start`` +
    ``pull_start_utc`` (per-run attempted-window counts). The legacy pyarrow /
    ``window_status.json`` paths OMIT the per-run figure (honest omission — no
    fabricated per-run count on a backing that cannot answer it); ``None`` (the
    default, and every pre-existing caller) is byte-identical everywhere.

    DEC-WINDOW-LEDGER-001 (PR-3). Reads the durable ledger Parquet
    (``<lake>/window_ledger/ledger.parquet``) when it exists — surfacing EVERY
    window with its full status (``PLANNED`` / ``SUCCESS`` / ``EMPTY`` / ``FAILED``
    / ``SKIPPED``) + ``first_seen_at`` — and otherwise FALLS BACK to the legacy
    ``window_status.json`` sidecar so the page keeps working before the
    ingest-switch slice (PR-4) writes the ledger. READ-ONLY; never raises (both
    loaders degrade to an empty record set). The ``source`` field tells the UI
    which backing answered.

    Server-side scale guards (DEC-OPTION-WINDOW-PLAN-001, hostile C3 — measured
    at the pre-planned universe scale of ~841k rows): ``counts`` (status ×
    spot/options/futures bucket — DEC-COVERAGE-GROWW-NAMESPACE-001) are ALWAYS
    computed over EVERY record; the ROW list is
    filtered (*market*/*status*), ordered severity-first
    (:data:`_WINDOW_STATUS_ORDER` — FAILED/EMPTY can never be hidden by the
    cap), and truncated to *limit*. ``PLANNED`` rows are returned ONLY when
    explicitly requested via ``status="PLANNED"`` — they are the bulk of a
    freshly-planned pull and carry no per-row information beyond their count.
    The ledger read is cached keyed on the Parquet's mtime (any flush
    invalidates), so the 4 s page poll stops re-paying the full load.

    Parameters
    ----------
    lake_root
        The lake directory holding the ledger / legacy sidecar.
    symbol_for_id
        Optional ``str(SECURITY_ID) → engine symbol`` map
        (DEC-WINDOW-OPTION-DECODE-001) so option windows display the
        underlying's PRECISE symbol (``BANKNIFTY``) instead of the vendor id
        the coverage key carries (``25|NSE_FNO``). ``None`` / a missing id
        keeps the raw coordinate — display-only, never a data change.
    market
        ``"spot"`` / ``"options"`` / ``"futures"`` row filter (``None`` = all).
    status
        Exact status row filter (``"FAILED"`` …). ``"PLANNED"`` is the ONLY way
        PLANNED rows enter the row list.
    limit
        Row-list cap (clamped to ``[1, WINDOW_ROWS_CAP]``). ``0`` = COUNTS-ONLY
        (DEC-WINDOWS-COUNTS-ONLY-001): the payload ships the identical
        ``counts``/``count``/``matched``/``empty_threshold`` meta with
        ``windows=[]`` and ``returned=0``, and the fast path SKIPS the row-page
        query entirely — the cheap mode for consumers that render only the
        stamp-cached counts (coverage.html's KPI card, universe.html's
        all-segments line).
    retry_sleep
        Injectable sleeper for the torn-read retry (DEC-WINDOW-TORN-READ-RETRY-001;
        deterministic tests). ``None`` → the real aliased :func:`time.sleep`. A
        transient ``duckdb.Error`` on the fast path (the reader-side multi-open race
        against the atomic ledger writer) is retried ONCE after
        :data:`_DUCKDB_RETRY_DELAY_S`; a second failure — or a deterministic
        ``KeyError``/``ValueError`` shape error, which is never retried — falls back
        to the legacy pyarrow path exactly as before. HONEST envelope (Golden Rule
        #10): the retry bounds ONE transient race; a persistently corrupt ledger
        still costs the legacy fallback as today.

    Returns
    -------
    dict[str, Any]
        ``{"source", "count", "counts", "empty_threshold", "matched",
        "returned", "capped", "windows"}`` — ``count`` is the TOTAL recorded
        (back-compat), ``matched`` the post-filter row count, ``returned`` the
        rows shipped, ``empty_threshold`` the R1 EMPTY→SKIPPED promotion bound.
    """
    from brutex.data.window_ledger_store import ledger_path

    # ``limit=0`` = COUNTS-ONLY (DEC-WINDOWS-COUNTS-ONLY-001): counts + meta with
    # ``windows=[]`` — every other value keeps the historical ``[1, WINDOW_ROWS_CAP]``
    # clamp byte-identically (a negative direct-caller limit still clamps UP to 1;
    # the route already refuses negatives via ``ge=0``).
    limit = 0 if int(limit) == 0 else max(1, min(int(limit), WINDOW_ROWS_CAP))
    ledger_file = ledger_path(lake_root)
    ledger_deltas = _ledger_delta_files(ledger_file)
    # FAST PATH (DEC-COVERAGE-WINDOWS-O1-001): with a DuckDB *conn* injected and a
    # LIVE ledger present (the base Parquet, pending deltas, or both), push
    # count/filter/sort/page DOWN into DuckDB — page rows + a stamp-cached GROUP BY —
    # instead of the O(N) Python count+filter+sort that ran on EVERY click/poll (the
    # 1.5–4 s root cause at 340k windows). PENDING DELTAS are folded latest-wins IN
    # SQL (DEC-LEDGER-DELTA-O-PAGE-001 — supersedes the defer-to-legacy stance of
    # DEC-LEDGER-DELTA-FLUSH-001; steady state stays SQL-text byte-identical). The
    # legacy pyarrow path below remains ONLY for: a ``None`` *conn* (back-compat
    # callers), a legacy-shape base the probe defers wholesale, a double
    # ``duckdb.Error`` fallback (which skips a persistently malformed delta
    # file-granularly via ``load_window_ledger``), and the frozen
    # ``window_status.json`` fallback when no ledger exists at all.
    if conn is not None and (ledger_file.is_file() or ledger_deltas):
        sleeper = _default_sleep if retry_sleep is None else retry_sleep

        def _fast_path() -> dict[str, Any]:
            return _build_window_payload_duckdb(
                conn,
                ledger_file,
                symbol_for_id=symbol_for_id,
                market=market,
                status=status,
                limit=limit,
                offset=clamp_offset(offset),
                count_cache=count_cache,
                generation=generation,
                since=since,
                page_cache=page_cache,
                scope=scope,
            )

        try:
            return _fast_path()
        except (KeyError, ValueError) as exc:
            # A wrong-shape / legacy-shape ledger Parquet (a missing core column → the
            # explicit probe ValueError, or a KeyError on the row projection) is
            # DETERMINISTIC — a retry would fail identically, so fall straight THROUGH
            # to the legacy pyarrow path below, which guards the core columns and
            # degrades to an empty ledger (the route's "never a 500" promise — the
            # fidelity the fast path would otherwise lose, hostile-review MINOR).
            _LOG.warning(
                "window_payload.duckdb_fallback",
                error=str(exc),
                error_type=type(exc).__name__,
                **_ledger_stat_fields(ledger_file),
            )
        except duckdb.Error as exc:
            # TORN READ (DEC-WINDOW-TORN-READ-RETRY-001): the fast path performs
            # MULTIPLE independent `read_parquet` opens per request; a concurrent
            # pull's atomic `os.replace` landing between them yields a footer/data
            # mismatch surfacing as a duckdb.Error (log-observed as a thrift
            # TProtocolException — reader-side only, does NOT contradict the #901
            # atomic-writer guarantee). Transient by nature → retry ONCE after
            # ~100 ms; the retry runs OUTSIDE any lock (the injected count cache
            # computes unlocked and caches nothing on a raise), so the second
            # attempt recomputes cleanly. A second failure falls back to the legacy
            # path EXACTLY as before — retry-once bounds one race, it is not a
            # corrupt-ledger cure (Golden Rule #10).
            _LOG.warning(
                "window_payload.duckdb_retry",
                error=str(exc),
                error_type=type(exc).__name__,
                **_ledger_stat_fields(ledger_file),
            )
            sleeper(_DUCKDB_RETRY_DELAY_S)
            try:
                return _fast_path()
            except (duckdb.Error, KeyError, ValueError) as exc2:
                _LOG.warning(
                    "window_payload.duckdb_fallback",
                    error=str(exc2),
                    error_type=type(exc2).__name__,
                    **_ledger_stat_fields(ledger_file),
                )
    if ledger_file.is_file() or ledger_deltas:
        # A base-less lake with pending deltas (a first pull's mid-flight delta
        # checkpoint) is a LIVE ledger — the fold-aware read reconstructs it
        # (DEC-LEDGER-DELTA-FLUSH-001), never the frozen status_log sidecar.
        source = "ledger"
        records: tuple[Any, ...] = _ledger_records_cached(lake_root)
    else:
        from brutex.data.window_status_store import load_window_status

        source = "status_log"
        records = tuple(load_window_status(lake_root).records())

    # R11 Python-fold parity (DEC-UNIVERSE-INDEX-ONLY-001, #1934): the legacy
    # pyarrow + window_status.json fallbacks share this ONE fold body — under a
    # bounded scope the §3.5 window predicate classifies each record (the
    # SQL≡Python equivalence pin rides the fixture corpus), excluded rows are
    # dropped from counts AND selection, and the census mirrors the fast path.
    fold_out_of_scope = 0
    fold_legacy_unresolved = 0
    if scope is not None and scope.bounded():
        kept_records: list[Any] = []
        for record in records:
            verdict = classify_window_row(
                str(record.security_id), scope, admitted_sids=symbol_for_id
            )
            if verdict == WINDOW_ADMITTED:
                kept_records.append(record)
                continue
            fold_out_of_scope += 1
            if verdict == WINDOW_LEGACY_UNRESOLVED:
                fold_legacy_unresolved += 1
        records = tuple(kept_records)

    counts = _window_counts_from_records(records)

    wanted_status = status.upper() if status else None
    selected = [
        r
        for r in records
        if (market is None or _window_market_bucket(r.market) == market)
        and (wanted_status == r.status if wanted_status else r.status != "PLANNED")
    ]
    selected.sort(key=lambda r: _WINDOW_STATUS_ORDER.get(r.status, 9))
    # The legacy page honours *offset* exactly like the DuckDB fast path (which binds
    # `LIMIT ? OFFSET ?`) — pre-fix this slice was `selected[:limit]`, so on a legacy
    # backing (None conn / legacy-shape base / double duckdb.Error / status_log) every
    # "Next ›" click silently re-served page 1 (DEC-DB-FNO-DISPLAY-TRUTH-001 item 2).
    # Same COMMON clamp_offset guard as every other pager; a past-the-end offset
    # honestly returns an empty page ("returned" possibly 0), `matched` unchanged.
    safe_offset = clamp_offset(offset)
    rows = [
        # The legacy status log has no first_seen_at — getattr keeps ONE shape.
        _window_row(r, first_seen_at=getattr(r, "first_seen_at", None), symbol_for_id=symbol_for_id)
        for r in selected[safe_offset : safe_offset + limit]
    ]
    fold_payload: dict[str, Any] = {
        "source": source,
        "count": len(records),
        "counts": counts,
        # The R1 promotion threshold (EMPTY → SKIPPED after this many
        # CONSECUTIVE clean EMPTYs) so the page can render each EMPTY row's
        # `empty_streak`/threshold progress without hardcoding the constant.
        "empty_threshold": EMPTY_TERMINAL_THRESHOLD,
        "matched": len(selected),
        "returned": len(rows),
        "capped": len(selected) > limit,
        "windows": rows,
    }
    if fold_out_of_scope:
        # EMIT-ON-NONZERO census — the fast path's exact key shape (R11).
        fold_payload["out_of_scope"] = {
            "windows": fold_out_of_scope,
            "legacy_unresolved": fold_legacy_unresolved,
            "dec": "DEC-UNIVERSE-INDEX-ONLY-001",
        }
    return fold_payload


# ── F&O expiries+contracts REGISTRY browser (DEC-DERIV-REGISTRY-DB-VIEW-001) ──
# The registry is a SEPARATE `registry.duckdb` cache (PR-1,
# DEC-DERIV-METADATA-HISTORICAL-SPLIT-001) holding the actual expiry DATES +
# contract SYMBOLS the lean instrument master discards. Surfacing it here makes a
# pulled chain browsable on /db, structured by expiry / strike / CE-PE — the
# structured-table view the operator wants. It is EMPTY until a later PR
# populates it; a missing file degrades to an honest empty result + note (never
# an invented row — Golden Rule #1), mirroring the expired-options "empty" path.

#: The honest note returned when no ``registry.duckdb`` exists yet — the registry
#: is written by the (separate) "Pull expiries + contracts" button, so before the
#: first metadata pull there is simply no cache to browse.
_REGISTRY_MISSING_NOTE: Final = "no registry yet — pull expiries + contracts first"


def registry_path_for(store_path: Path) -> Path:
    """Resolve the ``registry.duckdb`` path as a SIBLING of the instrument master.

    The registry cache lives in the SAME directory as ``instruments.duckdb`` (the
    lean master) — a sibling file, NOT inside it (DEC-DERIV-METADATA-HISTORICAL-
    SPLIT-001). Given the master's on-disk path (the dashboard wires it as
    ``store.path``), the registry is ``<that dir>/registry.duckdb``. Keeping this
    one resolver here means the ``/db`` routes never re-derive the path
    (CLAUDE.md §11 COMMON).

    Parameters
    ----------
    store_path
        The instrument store's on-disk path (``<lake_root>/instruments.duckdb``).

    Returns
    -------
    Path
        The sibling ``<lake_root>/registry.duckdb``.
    """
    return store_path.parent / REGISTRY_DB_NAME


def _expiry_grid_row(row: Any) -> list[Any]:
    """Project one :class:`~brutex.data.registry_store.ExpiryRow` to a JSON-safe grid row.

    Columns follow :data:`REGISTRY_EXPIRY_COLUMNS`: the ``expiry_date`` is rendered
    as an ISO ``YYYY-MM-DD`` string and ``fetched_at`` as clean IST (CLAUDE.md §6
    store-UTC / display-IST); ``year`` is the derived Phase-1 cell key.
    """
    return [
        row.exch,
        row.underlying,
        row.expiry_date.isoformat(),
        row.year,
        format_ist_clock(row.fetched_at.isoformat()),
    ]


def _contract_grid_row(row: Any) -> list[Any]:
    """Project one :class:`~brutex.data.registry_store.ContractRow` to a grid row.

    Columns follow :data:`REGISTRY_CONTRACT_COLUMNS`: the structured chain view
    (expiry / strike / CE-PE / per-contract historical ``candle_status``). The
    ``strike`` stays full numeric precision (it is not money — never rounded,
    CLAUDE.md §4) and is ``None`` for a ``FUT`` leg; ``expiry_date`` is an ISO
    string, ``fetched_at`` clean IST.
    """
    return [
        row.exch,
        row.underlying,
        row.expiry_date.isoformat(),
        row.contract_symbol,
        _json_safe(row.strike),
        row.side,
        row.candle_status,
        format_ist_clock(row.fetched_at.isoformat()),
    ]


def registry_count_key(
    kind: str,
    *,
    exch: str | None = None,
    underlying: str | None = None,
    expiry_date: str | None = None,
) -> str:
    """Build the stable cache key for a registry COUNT (DEC-O1-GUARANTEE-HARDEN-001 H1).

    The registry pagers' ``total`` is a FILTERED ``COUNT(*)`` over the
    monotonically-growing ``registry.duckdb`` tables — paging-invariant (the
    :func:`candle_count_key` law: ``limit`` / ``offset`` never change how many
    rows match), so every page of the same ``(kind, filters)`` shares ONE
    cached total per registry content revision. *kind* names the counted
    surface (``"exp"`` expiries / ``"con"`` contracts / ``"counts"`` the
    whole-registry nav-badge dict). The filter triple is folded via ``repr``
    (never a bare ``|`` join): the registry filters are UNVALIDATED query
    strings, so a literal ``|`` inside one must not collide two distinct
    filter shapes into one key.
    """
    return f"reg|{kind}|{(exch, underlying, expiry_date)!r}"


def _registry_total(
    count_cache: CandleCountCache | None,
    *,
    generation: int,
    key: str,
    compute: Callable[[], int],
) -> int:
    """One registry COUNT — memoised when a cache is injected, direct otherwise.

    The ONE cache-or-compute chokepoint for the registry pagers' totals
    (§11 COMMON — both pagers ride it). ``count_cache=None`` is the
    byte-identical legacy path (every click recounts, the pre-H1 behaviour).
    """
    if count_cache is None:
        return compute()
    return count_cache.get_or_compute(key, generation=generation, compute=compute)


def registry_counts(
    registry_path: Path,
    *,
    count_cache: CandleCountCache | None = None,
    generation: int = 0,
) -> dict[str, Any]:
    """Return ``{"expiries", "contracts", "exists", "note"}`` for the registry cache.

    A MISSING ``registry.duckdb`` degrades honestly to ``{"expiries": 0,
    "contracts": 0, "exists": False, "note": <missing note>}`` — never opens /
    creates a file just to count it (so a count never side-effects a brand-new
    empty cache) and never invents rows (Golden Rule #1). When the file exists the
    counts are read read-only via :class:`~brutex.data.registry_store.RegistryStore`
    (which is closed before returning), and ``note`` is ``None``.

    With a *count_cache* injected (DEC-O1-GUARANTEE-HARDEN-001 H1) the two
    whole-table ``COUNT(*)`` reads are memoised under
    ``registry_count_key("counts")`` at *generation* (the registry-content
    stat-token digest the route derives) — a repeat nav-badge read within one
    registry revision is an O(1) dict handoff instead of a store open + two
    COUNTs; the FIRST read per revision still pays the real store read (honest
    O(1)-on-repeat, never bare O(1)). ``count_cache=None`` (the default) is
    the byte-identical legacy path.

    Parameters
    ----------
    registry_path
        The ``registry.duckdb`` path (see :func:`registry_path_for`).
    count_cache, generation
        Optional injected COUNT memo + the registry-content generation
        (:func:`candle_count_key` precedent — the COMMON
        :class:`~brutex.dashboard.movers_cache.MoversSnapshotCache` satisfies
        the Protocol).

    Returns
    -------
    dict[str, Any]
        ``{"expiries": <int>, "contracts": <int>, "exists": <bool>,
        "note": <str|None>}`` — always a FRESH dict (a memoised hit is copied,
        so a caller mutating the payload never poisons the cache).
    """
    if not registry_path.is_file():
        return {"expiries": 0, "contracts": 0, "exists": False, "note": _REGISTRY_MISSING_NOTE}

    def _read() -> dict[str, Any]:
        """Open the store read-only and count both tables (the legacy body)."""
        from brutex.data.registry_store import RegistryStore

        with RegistryStore.open(registry_path) as store:
            return {
                "expiries": store.count_expiries(),
                "contracts": store.count_contracts(),
                "exists": True,
                "note": None,
            }

    if count_cache is None:
        return _read()
    cached = count_cache.get_or_compute(
        registry_count_key("counts"), generation=generation, compute=_read
    )
    return dict(cached)


def list_registry_expiries(
    registry_path: Path,
    *,
    exch: str | None = None,
    underlying: str | None = None,
    limit: int = _LIMIT_MAX,
    offset: int = 0,
    count_cache: CandleCountCache | None = None,
    generation: int = 0,
) -> dict[str, Any]:
    """List the registry's persisted expiry DATES as a browsable grid (``/db``).

    Reads the SEPARATE ``registry.duckdb`` cache read-only via
    :meth:`~brutex.data.registry_store.RegistryStore.list_expiries` (static
    ``(? IS NULL OR col = ?)`` filters — parameterized values only) and returns the
    grid shape the ``/db`` page renders (``{columns, rows, total, limit, offset}``),
    each row JSON-safe (``expiry_date`` ISO string, ``fetched_at`` clean IST). The
    page is SQL-bounded (``LIMIT``/``OFFSET``) and ``total`` is a cheap FILTERED
    ``COUNT(*)`` — so a registry tab loads O(page), not every persisted expiry
    (DEC-DB-VIEWER-PAGINATE-ALL-001). A MISSING registry file degrades to an empty
    grid plus an honest ``note`` (never an invented row — Golden Rule #1), mirroring
    the expired-options "empty" path.

    Parameters
    ----------
    registry_path
        The ``registry.duckdb`` path (see :func:`registry_path_for`).
    exch, underlying
        Optional exact-match filters (``None`` = no filter), passed straight to
        the store's parameterized SELECT.
    limit, offset
        Page bounds (clamped to ``[1, 500]`` / ``[0, ∞)``).
    count_cache, generation
        Optional injected COUNT memo + the registry-content generation
        (DEC-O1-GUARANTEE-HARDEN-001 H1 — the :func:`candle_count_key` idiom):
        the FILTERED ``total`` is paging-invariant, so with a cache injected a
        repeat/paging click within one registry revision is an O(1) memo hit
        instead of an O(filtered rows) ``COUNT(*)`` per click; the FIRST count
        per revision still runs the real query (honest O(1)-on-repeat). The
        default ``None`` is the byte-identical legacy path. The page DATA
        query below stays a per-click ``LIMIT``/``OFFSET`` SELECT (already
        O(page)) — only the COUNT is memoised.

    Returns
    -------
    dict[str, Any]
        ``{"columns": REGISTRY_EXPIRY_COLUMNS, "rows": [...], "total": <int>,
        "limit": <int>, "offset": <int>, "note": <str|None>}`` — ``total`` is the
        FILTERED row count; ``note`` is the missing-registry message when the cache
        file does not exist yet, else ``None``.
    """
    columns = list(REGISTRY_EXPIRY_COLUMNS)
    safe_limit = clamp_limit(limit)
    safe_offset = clamp_offset(offset)
    if not registry_path.is_file():
        return {
            "columns": columns,
            "rows": [],
            "total": 0,
            "limit": safe_limit,
            "offset": safe_offset,
            "note": _REGISTRY_MISSING_NOTE,
        }
    from brutex.data.registry_store import RegistryStore

    store = RegistryStore.open(registry_path)
    try:
        total = _registry_total(
            count_cache,
            generation=generation,
            key=registry_count_key("exp", exch=exch, underlying=underlying),
            compute=lambda: store.count_expiries(exch=exch, underlying=underlying),
        )
        rows = [
            _expiry_grid_row(r)
            for r in store.list_expiries(
                exch=exch, underlying=underlying, limit=safe_limit, offset=safe_offset
            )
        ]
    finally:
        store.close()
    return {
        "columns": columns,
        "rows": rows,
        "total": total,
        "limit": safe_limit,
        "offset": safe_offset,
        "note": None,
    }


def list_registry_contracts(
    registry_path: Path,
    *,
    exch: str | None = None,
    underlying: str | None = None,
    expiry_date: str | None = None,
    limit: int = _LIMIT_MAX,
    offset: int = 0,
    count_cache: CandleCountCache | None = None,
    generation: int = 0,
) -> dict[str, Any]:
    """List the registry's persisted contract SYMBOLS as a browsable grid (``/db``).

    The structured chain view the operator asked for: one row per contract leg
    (expiry / strike / CE-PE / per-contract historical ``candle_status``), read
    read-only via :meth:`~brutex.data.registry_store.RegistryStore.list_contracts`
    (static parameterized filters). The page is SQL-bounded (``LIMIT``/``OFFSET``)
    and ``total`` is a cheap FILTERED ``COUNT(*)`` — so a chain with tens of thousands
    of legs loads O(page), not every leg (DEC-DB-VIEWER-PAGINATE-ALL-001).
    *expiry_date*, when given, is a strict ISO ``YYYY-MM-DD`` string (a
    ``<input type="date">`` value), parsed via the shared :func:`_parse_iso_date`
    (a malformed bound raises ``ValueError`` → HTTP 400 at the route). A MISSING
    registry file degrades to an empty grid plus an honest ``note`` (never an
    invented row — Golden Rule #1).

    Parameters
    ----------
    registry_path
        The ``registry.duckdb`` path (see :func:`registry_path_for`).
    exch, underlying
        Optional exact-match filters (``None`` = no filter).
    expiry_date
        Optional ISO ``YYYY-MM-DD`` expiry filter (``None`` = no filter); a
        malformed value raises ``ValueError``.
    limit, offset
        Page bounds (clamped to ``[1, 500]`` / ``[0, ∞)``).
    count_cache, generation
        Optional injected COUNT memo + the registry-content generation
        (DEC-O1-GUARANTEE-HARDEN-001 H1; see :func:`list_registry_expiries` —
        the same O(1)-on-repeat law over the FILTERED leg count, keyed with
        the validated ISO *expiry_date* string; ``None`` = byte-identical
        legacy recount per click).

    Returns
    -------
    dict[str, Any]
        ``{"columns": REGISTRY_CONTRACT_COLUMNS, "rows": [...], "total": <int>,
        "limit": <int>, "offset": <int>, "note": <str|None>}`` — ``total`` is the
        FILTERED leg count.

    Raises
    ------
    ValueError
        On a malformed *expiry_date* bound (not strict ISO ``YYYY-MM-DD``).
    """
    columns = list(REGISTRY_CONTRACT_COLUMNS)
    safe_limit = clamp_limit(limit)
    safe_offset = clamp_offset(offset)
    # Parse the date BEFORE the file check so a malformed bound is rejected
    # consistently whether or not the cache exists yet (no silent empty page).
    parsed_expiry = _parse_iso_date(expiry_date, "expiry_date") if expiry_date is not None else None
    if not registry_path.is_file():
        return {
            "columns": columns,
            "rows": [],
            "total": 0,
            "limit": safe_limit,
            "offset": safe_offset,
            "note": _REGISTRY_MISSING_NOTE,
        }
    from brutex.data.registry_store import RegistryStore

    store = RegistryStore.open(registry_path)
    try:
        total = _registry_total(
            count_cache,
            generation=generation,
            key=registry_count_key(
                "con",
                exch=exch,
                underlying=underlying,
                expiry_date=None if parsed_expiry is None else parsed_expiry.isoformat(),
            ),
            compute=lambda: store.count_contracts(
                exch=exch, underlying=underlying, expiry_date=parsed_expiry
            ),
        )
        rows = [
            _contract_grid_row(r)
            for r in store.list_contracts(
                exch=exch,
                underlying=underlying,
                expiry_date=parsed_expiry,
                limit=safe_limit,
                offset=safe_offset,
            )
        ]
    finally:
        store.close()
    return {
        "columns": columns,
        "rows": rows,
        "total": total,
        "limit": safe_limit,
        "offset": safe_offset,
        "note": None,
    }


__all__ = [
    "ALLOWED_OPERATORS",
    "CANDLE_COLUMNS",
    "FNO_DERIVED_COLUMNS",
    "FNO_GREEKS_COLUMNS",
    "FNO_GREEKS_COLUMN_TYPES",
    "FNO_MONEYNESS_COLUMNS",
    "FNO_VALUE_COLUMNS",
    "KNOWN_TABLES",
    "OPTIONS_DIR_NAME",
    "OPTION_CANDLE_COLUMNS",
    "REGISTRY_CONTRACT_COLUMNS",
    "REGISTRY_DB_NAME",
    "REGISTRY_EXPIRY_COLUMNS",
    "SPOT_DERIVED_COLUMNS",
    "SPOT_VERDICT_INTERVALS",
    "TABLE_SUMMARY_FAULT_TYPES",
    "WINDOW_PHASES",
    "ScopedSummaryRows",
    "build_phase_progress",
    "build_window_payload",
    "clamp_limit",
    "clamp_offset",
    "fno_candle_lake_schema",
    "fno_current_phase_from_counts",
    "list_candle_datasets",
    "list_columns",
    "list_fno_candle_datasets",
    "list_option_datasets",
    "list_option_series",
    "list_registry_contracts",
    "list_registry_expiries",
    "phase_progress_payload",
    "phase_window_progress",
    "query_candles",
    "query_option_candles",
    "query_table",
    "registry_count_key",
    "registry_counts",
    "registry_path_for",
    "resolve_applied_range",
    "sample_rows",
    "spot_window_rollup",
    "summarize_option_datasets",
    "summarize_spot_datasets",
    "summarize_spot_verdict_datasets",
    "summary_rows_scope_excluded",
    "table_summary",
    "window_ledger_rows_cached",
    "window_ledger_rows_page",
    "window_status_label",
]
