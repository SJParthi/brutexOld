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

import hashlib
import math
import re
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
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
from brutex.calendars.nse import MUHURAT_SESSIONS

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
from brutex.data.fno_candle_lake import (
    FNO_CANDLE_COLUMNS,
    FNO_CANDLE_LAKE_SEGMENT,
)
from brutex.data.fno_symbol import parse_fno_symbol
from brutex.data.lake_keys import spot_recovery_candidates
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
from brutex.data.session import SESSION_OPEN, format_ist_clock
from brutex.data.spot_features import pct_change
from brutex.data.spot_join import utc_instant
from brutex.data.window_ledger import (
    DEMOTED_SETTLE_REASONS,
    EMPTY_TERMINAL_THRESHOLD,
    is_expired_window_settle_reason,
)
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
#: rendered string is deterministic regardless of where the server runs.
_IST: Final[ZoneInfo] = ZoneInfo("Asia/Kolkata")

#: The daily-bar partition interval — its bars render DATE-ONLY (no noise
#: ``00:00:00`` suffix) in the candle viewer (DEC-DB-CANDLE-DERIVED-001).
_DAILY_INTERVAL: Final[str] = "1day"

#: The SQL fragment for a bar's **IST wall-clock instant** (a naive TIMESTAMP on
#: the Indian-market clock). Adds the fixed +05:30 IST offset (330 minutes; India
#: has no DST — CLAUDE.md §6) to the UTC wall-clock, so it is independent of the
#: ambient DuckDB session ``TimeZone`` (the operator runs Asia/Kolkata). The ONE
#: place the 330-minute offset lives — :data:`_IST_DATE_EXPR` derives from it so
#: no second literal can drift (DEC-DAILY-OPEN-SUBSTITUTION-HONESTY-001).
_IST_TS_EXPR: Final[str] = "((\"timestamp\" AT TIME ZONE 'UTC') + INTERVAL '330 minutes')"

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
#: this instant; mirrors ``brutex.data.spot_features._SESSION_OPEN`` (§11 COMMON).
_SESSION_OPEN_IST: Final[time] = time(9, 15)

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

#: The two server-computed derived columns appended to every spot candle page
#: (DEC-DB-CANDLE-DERIVED-001): previous-day % change + the 09:15 overnight gap.
#: They are NOT real Parquet columns (never selectable in the column picker);
#: the viewer computes them per bar from the symbol's prior-day last 1-minute
#: close (DEC-SPOT-PREVCLOSE-1MIN-001).
SPOT_DERIVED_COLUMNS: Final = ("change_pct", "open_gap_pct")

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

#: The full F&O derived column set in display order: the combined moneyness label, then the
#: value columns. ``_fno_derived_columns`` returns this for an ``FNO`` page.
FNO_DERIVED_COLUMNS: Final = FNO_MONEYNESS_COLUMNS + FNO_VALUE_COLUMNS

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


def table_summary(conn: Any) -> list[dict[str, Any]]:
    """Return ``[{"table", "row_count"}]`` for every known table.

    Runs one ``SELECT COUNT(*)`` per table in :data:`KNOWN_TABLES`. A
    table that is missing (a partially-initialised or foreign DB) is
    tolerated and reported with ``row_count == 0`` rather than raising,
    so a fresh/empty store renders cleanly.

    Parameters
    ----------
    conn
        A DuckDB connection — typically
        :attr:`brutex.data.instrument_store.InstrumentStore.connection`.

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

    The skip set is the COMMON :func:`brutex.dashboard.movers_resilient.movers_read_skip_errors`
    (``duckdb.Error`` covers a corrupt ``read_parquet``; ``OSError`` covers a file-level
    fault). ``duckdb.Error`` is deliberately BROAD (DuckDB exposes no version-stable
    *unreadable-file* subclass), but a genuine SQL bug is STILL never swallowed: the
    per-file probe finds every file readable (``omitted`` stays ``False``), so the
    degraded re-run over the SAME files RE-RAISES the original error — it propagates as a
    500, not a silent empty result (proven in the tests). The per-file probing is paid
    ONLY when a read actually fails; the happy path stays a single query (Golden Rule #5
    O(1) steady-state).
    """
    glob = directory.as_posix() + "/**/*.parquet"
    try:
        return list(conn.execute(sql, [glob, *extra_params]).fetchall())
    except movers_read_skip_errors() as whole_glob_error:
        readable, omitted = _readable_parquet_subset(
            conn,
            directory,
            whole_glob_error,
            source=source,
            skip_event=skip_event,
            unreadable_event=unreadable_event,
        )
        if omitted and skip_sink is not None:
            skip_sink.append(True)
        if not readable:
            return []
        return list(conn.execute(sql, [readable, *extra_params]).fetchall())


def _readable_parquet_subset(
    conn: Any,
    directory: Path,
    whole_glob_error: BaseException,
    *,
    source: str,
    skip_event: str,
    unreadable_event: str,
) -> tuple[list[str], bool]:
    """Probe each ``*.parquet`` under *directory*; return ``(readable_subset, any_omitted)``.

    The shared degraded-path resolver (CLAUDE.md §11 COMMON) reached ONLY after a
    whole-glob ``read_parquet`` already raised an unreadable-Parquet error
    (*whole_glob_error*). Each file is probed with a cheap ``SELECT 1 ... LIMIT 1``; an
    unreadable one is logged as *skip_event* (path + reason) and OMITTED — never a
    fabricated bar. An empty return (NO readable file) is logged once as
    *unreadable_event* so the caller degrades to an empty result instead of re-raising.
    The skip set is the COMMON
    :func:`brutex.dashboard.movers_resilient.movers_read_skip_errors`.

    The second tuple element ``any_omitted`` is ``True`` when at least one file was
    DROPPED from the read (a real skip OR the all-unreadable case). The adjacency-sensitive
    prior-close baseline readers (DEC-DB-CORRUPT-SKIP-001 BLOCKER fix) consume this flag:
    once any file is omitted, the partition is no longer trustworthy for ADJACENCY (the
    true predecessor day's bar may have lived in the dropped file), so they must emit a
    BLANK %-change rather than bridge to a non-adjacent earlier day — never a fabricated
    number (Golden Rule #1).
    """
    skip_errors = movers_read_skip_errors()
    readable: list[str] = []
    omitted = False
    for path in sorted(directory.rglob("*.parquet")):
        candidate = path.as_posix()
        try:
            conn.execute("SELECT 1 FROM read_parquet(?) LIMIT 1", [candidate]).fetchone()
        except skip_errors as skip_reason:
            _LOG.warning(skip_event, source=source, path=candidate, error=str(skip_reason))
            omitted = True
        else:
            readable.append(candidate)
    if not readable:
        _LOG.error(unreadable_event, source=source, error=str(whole_glob_error))
        omitted = True
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
    corrupt file degrades to its readable subset (or 0 rows) here too, never a 500.
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
    probe-and-skip (``db.*`` log namespace, DEC-DB-CORRUPT-SKIP-001) via the shared
    :func:`_readable_parquet_subset`: fast path binds the glob; on an unreadable-Parquet
    error the readable subset is re-bound (the SELECT list is identical, so the column
    description is byte-identical to the fast path). When NO file is readable, returns
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
            source=source,
            skip_event="db.parquet_skip",
            unreadable_event="db.parquet_unreadable",
        )
        if not readable:
            return [], []
        cursor = conn.execute(sql, [readable, *extra_params])
    out_columns = [str(col[0]) for col in (cursor.description or [])]
    return out_columns, list(cursor.fetchall())


#: The intervals the SPOT coverage/completeness VERDICTS actually read — the
#: single FETCHED granularity (``1minute``, DEC-1MINUTE-ONLY-DERIVE-1DAY-001)
#: plus the derived ``1day`` daily anchor. The 19 derived intraday TFs the
#: prebuild materialises are display-only derivatives of the same 1-minute
#: base, so scanning them adds nothing to any verdict
#: (DEC-COMPLETENESS-FNO-SKIP-001, refuter-amended register #12).
SPOT_VERDICT_INTERVALS: Final[tuple[str, str]] = ("1minute", "1day")


def summarize_spot_verdict_datasets(conn: Any, lake_root: Path) -> list[dict[str, Any]]:
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
    """
    return summarize_spot_datasets(
        conn,
        lake_root,
        exclude_segments=frozenset({FNO_CANDLE_LAKE_SEGMENT}),
        intervals=SPOT_VERDICT_INTERVALS,
    )


def summarize_spot_datasets(
    conn: Any,
    lake_root: Path,
    *,
    session_date_segments: frozenset[str] | None = None,
    exclude_segments: frozenset[str] = frozenset(),
    intervals: Collection[str] | None = None,
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
        # RESILIENT-001).
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


def summarize_option_datasets(conn: Any, lake_root: Path) -> list[dict[str, Any]]:
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

    Parameters
    ----------
    conn
        A DuckDB connection.
    lake_root
        Top-level lake directory (the parent of ``options/``).

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
    # (DEC-COVERAGE-PARQUET-RESILIENT-001).
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
    return [
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
) -> dict[str, Any]:
    """Read one page of OHLCV candles for a partition set (DEC-DB-QUERY-001).

    Reads the Parquet lake directly via DuckDB ``read_parquet`` over the
    partition glob ``<lake>/bars/<exch>/<seg>/<symbol>/<interval>/**/*.parquet``
    — no data duplication, the lake stays the single source of truth.
    The glob is scoped to ONE ``(symbol, interval)`` (its whole-year/month
    files only), and the page is ``LIMIT``-bounded — so a click never
    materialises more than ``limit`` rows in Python regardless of how many
    years are stored (DuckDB additionally skips row-groups via min/max stats;
    this is engine cost, not directory pruning). *start* / *end* are inclusive
    ISO ``YYYY-MM-DD`` bounds matched against the bar's UTC calendar date.
    Returns an empty
    page (never raises) when the partition set has no Parquet files yet.

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
        by. When supplied, the O(files) Parquet-footer ``COUNT(*)`` is memoised per
        ``(coords, start, end)`` so a prev/next paging click within one dataset is
        an O(1) hit; a pull bumps *generation* → recompute (never a stale total).
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
        # An F&O page ALWAYS carries the strike-step honesty field so the frontend's
        # key set is stable across an empty vs populated partition; an empty page
        # presents no step, so the label is an honest None
        # (DEC-MONEYNESS-REGISTRY-STEP-001).
        empty_step_confidence = {"strike_step_confidence": None} if is_fno else {}
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
    order_sql = ' ORDER BY "timestamp" ' + ("DESC" if descending else "ASC")
    skip_source = f"candle:{exchange}/{segment}/{symbol}/{interval}"
    # Read-side dedup gate (DEC-LAKE-READ-UNIQUE-001): a partition poisoned
    # before the write-side uniqueness fix can physically carry duplicate
    # timestamps. The PAGE query collapses them keep-FIRST in the same single
    # scan (QUALIFY over filename + file_row_number order — deterministic), and
    # the TOTAL counts DISTINCT timestamps so it equals what the deduped pages
    # actually serve. A clean partition is byte-identical.
    page_source = DEDUP_PARQUET_SOURCE_SQL
    qualify_sql = dedup_qualify_sql(("timestamp",))

    # A requested Greeks overlay column may be physically ABSENT from a legacy
    # (pre-Greeks) partition tree — substitute a typed NULL projection so the
    # page renders honest blank cells instead of a binder-error 500
    # (DEC-GREEKS-ENGINE-PR3-001; see _greeks_tolerant_select).
    if columns is not None and any(c in FNO_GREEKS_COLUMN_TYPES for c in columns):
        select = _greeks_tolerant_select(conn, base, columns, source=skip_source)

    # The page total is a `COUNT(*)` over `read_parquet(glob)` — DuckDB answers it
    # from each Parquet file's FOOTER metadata, so it is O(files), NOT O(1): on a
    # deep multi-year partition it re-reads every monthly file's footer on EVERY
    # paging click (~8 ms at 6 years, growing as the lake deepens). The total is
    # invariant across paging / sort / column subsetting of the same
    # (coords, start, end), so the dashboard injects a generation-stamped cache
    # (DEC-DB-CANDLE-COUNT-CACHE-001): a prev/next click within one dataset is an
    # O(1) hit, and a pull/materialize bumps the generation → recompute (the SAME
    # write-invalidation signal the movers cache uses — never a stale total). When
    # no cache is injected (the pure-function default), the COUNT runs inline,
    # behaviour-identical to before.
    def _count_total() -> int:
        rows = _execute_parquet_resilient(
            conn,
            base,
            f"SELECT {distinct_count_sql(('timestamp',))} FROM {source}{where_sql}",  # noqa: S608  # nosec B608 - source is read_parquet(?) with a bound path; values bound
            list(where_params),
            source=skip_source,
        )
        return int(rows[0][0]) if rows and rows[0][0] is not None else 0

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
    out_columns, raw_rows = _execute_candle_page_resilient(
        conn,
        base,
        f"SELECT {page_select} FROM {page_source}{where_sql}{qualify_sql}{order_sql} LIMIT ? OFFSET ?",  # noqa: S608  # nosec B608 - validated select + bound path + bound values
        [*where_params, safe_limit, safe_offset],
        source=skip_source,
    )
    # Split-brain screen (DEC-DASHBOARD-READ-SCREEN-001): route the page through
    # the ONE COMMON engine-parity screen so a bad-tick spike / an insane bar the
    # engine + /movers drop is never SERVED raw here — /db == /movers == engine.
    # Spot/futures candles keep the FULL screen (insane + ×4 bad-tick), seeded with
    # the last KEPT in-session bar before the page's earliest row so the cross-row
    # bad-tick screen is correct at the page's left edge (`_bad_tick_seed_bar`; the
    # deep-cluster residual is documented there). CONTRACT-TYPE-AWARE on the F&O
    # segment: this route ALSO serves CE/PE contract partitions (via the FNO
    # cascade), and option PREMIA legitimately move ≥4×/minute on leverage — so a
    # decoded CE/PE symbol screens INSANE-ONLY (`bad_tick_enabled=False`, no seed —
    # the cross-row arm is off), matching /movers-options + the /db expired-options
    # tab exactly (the same scope rule as `_read_future_session(bad_tick=)`); FUT
    # and unparseable F&O symbols keep the full default-ON screen. Clean pages
    # return the same object (byte-identical; the screen is an O(page-rows)
    # Python pass — honest cost, not zero work). HONEST envelope: the seed read
    # is ONE additional DuckDB query per non-option page over the partition
    # glob — an O(files) Parquet-metadata scan (a constant-factor addition to
    # the per-click query count; the LIMIT-k row read itself is tiny), eager
    # even on clean data; and `total` above is the DATASET-wide
    # DISTINCT-timestamp count, so on a CORRUPT partition it OVER-counts by the
    # number of screened-out rows (the served page shows fewer rows than
    # `total`/`limit` imply) — a bounded honesty gap on corrupt data only, never a
    # fabricated bar.
    is_option_contract = False
    if is_fno:
        parts = parse_fno_symbol(symbol)
        is_option_contract = parts is not None and parts.option_type is not None
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
    if interval == _DAILY_INTERVAL:
        prev_close = _prev_trading_close(
            _daily_close_by_date(conn, lake_root, exchange=exchange, segment=segment, symbol=symbol)
        )
    else:
        prev_close = _intraday_prev_close_by_page_date(
            conn,
            lake_root,
            exchange=exchange,
            segment=segment,
            symbol=symbol,
            page_dates=_page_ist_dates(raw_rows, out_columns),
        )
    derived = _spot_derived_rows(raw_rows, out_columns, prev_close, interval=interval)
    # F&O option pages also carry per-bar moneyness (ITM/ATM/OTM + ATM±N) classified
    # against the underlying spot at each bar (DEC-FNO-MONEYNESS-001); spot pages do not.
    extra_columns: list[str] = [*SPOT_DERIVED_COLUMNS, *_fno_derived_columns(segment)]
    step_confidence: StepConfidence | None = None
    if segment == FNO_CANDLE_LAKE_SEGMENT:
        fno_cells, step_confidence = _fno_derived_rows(
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
    downstream is O(1) (CLAUDE.md §5). Session-filtered intraday bars (09:15–15:29
    IST) never cross the IST/UTC date boundary, so ``CAST(timestamp AS DATE)`` IS
    the IST trading date (matches :func:`_bar_ist_date` used on the viewed bars).
    Returns ``{}`` when the symbol has no 1-minute partition yet, so the derived
    columns degrade to blank rather than raising (DEC-DB-CANDLE-DERIVED-001).
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
        'SELECT CAST("timestamp" AS DATE) AS d, arg_max(close, "timestamp") AS last_close '
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
    :func:`_intraday_prev_close_by_page_date`: reads the last
    :data:`_BAD_TICK_SEED_WINDOW` bars STRICTLY BEFORE the page's earliest row AND
    on the same UTC/session date (``CAST(timestamp AS DATE)`` — an NSE session
    never crosses a UTC date), SCREENS that ascending window through the SAME
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
    window = _execute_parquet_resilient(
        conn,
        base,
        'SELECT "timestamp", "open", "high", "low", "close" '
        "FROM read_parquet(?, union_by_name=true) "
        'WHERE "timestamp" < ? AND CAST("timestamp" AS DATE) = CAST(? AS DATE) '
        'ORDER BY "timestamp" DESC LIMIT ?',
        [earliest, earliest, _BAD_TICK_SEED_WINDOW],
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
    (DEC-DB-UNIVERSE-O1-LATENCY-001). Instead of aggregating the WHOLE multi-year
    1-minute partition on every paging click, it reads only:

    1. the last 1-minute close per day for the page's OWN ``[min, max]`` date span
       (an intraday page covers ~1–2 trading days), and
    2. the single 1-minute bar immediately BEFORE the page's earliest date — the
       one baseline that lives outside the page's own span (the predecessor of the
       first page row).

    Both reads are bounded by the bar ``timestamp`` (IST-midnight instants), which
    DuckDB row-group-prunes, so the per-click cost is O(page date-span) — flat as
    the lake deepens, never O(all stored years). The result is **byte-identical**
    to the prior full-partition scan for every page date: the page's dates are
    contiguous trading days (a page is a ``LIMIT``/``OFFSET`` slice of
    timestamp-sorted bars), and prepending their single shared predecessor makes
    ``_prev_trading_close`` map each page date to the same prior-day close the full
    scan would (proven in ``tests/unit/test_dashboard_db_query.py``). Returns
    ``{}`` (every derived cell blank) when *page_dates* is empty or the symbol has
    no 1-minute partition — matching :func:`_intraday_last_close_by_date`.

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
    # session bars (09:15–15:29 IST) never fall before 09:15, so CAST(ts AS DATE)
    # for a bar in [lo 00:00 IST, (hi+1) 00:00 IST) is its IST trading date — the
    # SAME keying as _intraday_last_close_by_date (session-tz-independent here).
    lo_instant = datetime.combine(lo, time(0, 0), tzinfo=_IST)
    hi_instant = datetime.combine(hi + timedelta(days=1), time(0, 0), tzinfo=_IST)
    skip_source = f"candle-prevclose:{exchange}/{segment}/{symbol}"
    # ADJACENCY GUARD (DEC-DB-CORRUPT-SKIP-001 BLOCKER fix): the prior-day-close baseline
    # is ONLY valid if the partition read in full — `_prev_trading_close` maps each page
    # date to its IMMEDIATE predecessor, and the predecessor read (#2 below) finds "the
    # bar just before `lo`". If ANY file in this 1-minute partition was skipped (corrupt),
    # the true adjacent predecessor may have lived in the dropped file, so any %-change we
    # computed would be baselined on a NON-ADJACENT earlier day — a confidently-WRONG
    # number. So we record whether either read omitted a file and, if so, return {} → every
    # affected bar gets an honest BLANK %-change, never a fabricated one (Golden Rule #1).
    skip_sink: list[bool] = []
    # 1) last 1-minute close per day, bounded to the page's [lo, hi] span. glob is
    #    built from validated coordinates (no free user text); path + instants bound.
    span = _execute_parquet_resilient(
        conn,
        base,
        'SELECT CAST("timestamp" AS DATE) AS d, arg_max(close, "timestamp") AS last_close '
        "FROM read_parquet(?, union_by_name=true) "
        'WHERE "timestamp" >= ? AND "timestamp" < ? GROUP BY d',
        [lo_instant, hi_instant],
        source=skip_source,
        skip_sink=skip_sink,
    )
    last_close: dict[date, Decimal] = {day: _to_decimal(close) for day, close in span}
    # 2) the single bar immediately BEFORE lo — the predecessor of the earliest
    #    page date (ORDER BY DESC LIMIT 1 reads ~one row-group, not the whole past).
    pred_rows = _execute_parquet_resilient(
        conn,
        base,
        'SELECT CAST("timestamp" AS DATE) AS d, close '
        "FROM read_parquet(?, union_by_name=true) "
        'WHERE "timestamp" < ? ORDER BY "timestamp" DESC LIMIT 1',
        [lo_instant],
        source=skip_source,
        skip_sink=skip_sink,
    )
    if skip_sink:
        # A corrupt file was skipped → adjacency is no longer guaranteed → honest blank.
        return {}
    if pred_rows:
        pred = pred_rows[0]
        last_close[pred[0]] = _to_decimal(pred[1])
    return _prev_trading_close(last_close)


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


def _daily_open_by_date(
    conn: Any, lake_root: Path, *, exchange: str, segment: str, symbol: str
) -> dict[date, Decimal]:
    """Read the symbol's 1-DAY official open per IST date → ``{ist_date: open}``.

    The day's official opening price — on NSE the pre-open auction's indicative
    equilibrium price (IEP) *becomes* the day's 1-day open — used as the IEP for
    the pre-open report (DEC-PREOPEN-REPORT-1DAY-001), on the SAME 1-day basis as
    the report's previous close so the two never mix price scales. Reads the
    1-day partition keyed by :func:`_bar_ist_date`; ``{}`` when absent.
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
    )
    return {_bar_ist_date(ts): _to_decimal(open_) for ts, open_ in rows}


def _intraday_open_by_date(
    conn: Any, lake_root: Path, *, exchange: str, segment: str, symbol: str
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
    (~one row per trading day → O(1) per-day lookup, CLAUDE.md §5); session bars
    never cross the IST/UTC date boundary so ``CAST(timestamp AS DATE)`` IS the IST
    trading date. ``{}`` when the symbol has no 1-minute partition.
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
        'SELECT CAST("timestamp" AS DATE) AS d, arg_min(open, "timestamp") AS first_open '
        "FROM read_parquet(?, union_by_name=true) GROUP BY d",
        [],
        source=f"candle-dayopen-min:{exchange}/{segment}/{symbol}",
    )
    return {day: _to_decimal(first_open) for day, first_open in rows}


def _spot_derived_rows(
    raw_rows: list[tuple[Any, ...]],
    out_columns: list[str],
    prev_close_by_date: dict[date, Decimal],
    *,
    interval: str = _INTRADAY_BASE_INTERVAL,
) -> list[tuple[Any, Any]]:
    """Compute the per-bar ``(change_pct, open_gap_pct)`` derived pair.

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

    Both reuse the COMMON :func:`brutex.data.spot_features.pct_change` primitive
    (CLAUDE.md §11) and stay full precision — statistical metrics, never
    money-rounded (CLAUDE.md §4); the 2dp is a *display*-only concern in
    ``db.html``. A bar whose IST date is absent from *prev_close_by_date* (the
    first trading day, or a symbol with no baseline lake) gets ``(None, None)``;
    a zero previous close yields ``None`` (no divide-by-zero, via ``pct_change``).
    """
    ts_idx = out_columns.index("timestamp") if "timestamp" in out_columns else -1
    open_idx = out_columns.index("open") if "open" in out_columns else -1
    close_idx = out_columns.index("close") if "close" in out_columns else -1
    is_daily = interval == _DAILY_INTERVAL
    derived: list[tuple[Any, Any]] = []
    for row in raw_rows:
        change: Decimal | None = None
        gap: Decimal | None = None
        ts = row[ts_idx] if ts_idx >= 0 else None
        if isinstance(ts, datetime):
            prev = prev_close_by_date.get(_bar_ist_date(ts))
            if prev is not None:
                if close_idx >= 0:
                    change = pct_change(_to_decimal(row[close_idx]), prev)
                # 1-day: the open-vs-prior-close gap on every bar; intraday: only
                # the 09:15 IST session-open bar (DEC-DB-CANDLE-PERINTERVAL-001).
                if open_idx >= 0 and (is_daily or _bar_ist_time(ts) == _SESSION_OPEN_IST):
                    gap = pct_change(_to_decimal(row[open_idx]), prev)
        derived.append((_json_safe(change), _json_safe(gap)))
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
) -> dict[datetime, Decimal]:
    """Read ``{bar_instant: underlying_spot_close}`` for an F&O contract's underlying.

    The per-bar spot needed to classify an option's moneyness (DEC-FNO-MONEYNESS-001).
    Probes the underlying spot under :data:`_SPOT_PROBE_SEGMENTS` (``INDEX`` then
    ``CASH`` — an index underlying rides INDEX, a stock rides CASH) at the SAME
    *interval* as the option bars, so the two series align bar-for-bar by timestamp. The
    read is BOUNDED to the page's ``[min, max]`` instant (``BETWEEN``), so it never
    materialises more than the page's worth of spot bars regardless of how many years
    are stored — O(page) once, then O(1) per-bar lookup downstream (CLAUDE.md §5).

    Returns ``{}`` (→ every bar classifies UNKNOWN, never raising) when *ts_values* is
    empty, the underlying coordinate is illegal, or the underlying spot is absent from
    both segments (Golden Rule #1 — an honest UNKNOWN, never a fabricated spot).

    When the exact *underlying* probe comes back EMPTY (and only then — zero extra
    I/O and NO log on the hot path), the COMMON spot-key drift-recovery ladder
    (:func:`~brutex.data.lake_keys.spot_recovery_candidates` — index aliases BOTH
    directions + the ``BRUTEX_FNO_INDEX_ALIASES`` env override + the
    ``"<EXCH>-<underlying>"`` #911 form + the ``lake_keys`` #911 rungs) is walked in
    order, returning the FIRST candidate with any joined spot bar and emitting ONE
    ``db_view.spot_key_recovered`` info naming both keys
    (DEC-DB-MONEYNESS-KEY-RECOVER-001 — pre-fix, a verbose-symbol index such as
    ``MIDCPNIFTY`` over a ``NIFTYMIDSELECT``-keyed spot dir read moneyness UNKNOWN on
    every bar even though the spot data existed on disk). Ladder exhaustion returns
    the honest empty map — never a fabricated spot.
    """
    if not ts_values:
        return {}
    min_ts, max_ts = min(ts_values), max(ts_values)

    def _probe(key: str) -> dict[datetime, Decimal]:
        for segment in _SPOT_PROBE_SEGMENTS:
            try:
                _validate_candle_coords(exchange, segment, key, interval)
            except ValueError:
                return {}  # a key that is not a legal coordinate → no spot under it.
            base = lake_root / "bars" / exchange / segment / key / interval
            if not (base.is_dir() and any(base.rglob("*.parquet"))):
                continue
            # glob built from validated coordinates (no free user text); path + both
            # bounds are bound parameters (same SQL/path-injection guard as
            # query_candles). A NULL close is filtered in SQL so a stray null spot bar
            # degrades that instant to UNKNOWN (the no-spot bucket) rather than raising
            # in `_to_decimal(None)` — honouring the "never raises / honest UNKNOWN"
            # contract. Resilient read (DEC-DB-CORRUPT-SKIP-001): a corrupt
            # underlying-spot Parquet degrades the join to its readable subset (the
            # affected option bars classify UNKNOWN), never a 500.
            rows = _execute_parquet_resilient(
                conn,
                base,
                'SELECT "timestamp", close FROM read_parquet(?, union_by_name=true) '
                'WHERE "timestamp" BETWEEN ? AND ? AND close IS NOT NULL',
                [min_ts, max_ts],
                source=f"candle-spotjoin:{exchange}/{segment}/{key}/{interval}",
            )
            return {_utc_instant(ts): _to_decimal(close) for ts, close in rows}
        return {}

    spots = _probe(underlying)
    if spots:
        return spots
    for candidate in spot_recovery_candidates(underlying, exchange):
        recovered = _probe(candidate)
        if recovered:
            _LOG.info(
                "db_view.spot_key_recovered",
                underlying=underlying,
                recovered_key=candidate,
                exchange=exchange,
                interval=interval,
            )
            return recovered
    return spots


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
) -> tuple[list[tuple[str, str | None, str | None, str | None, int | None]], StepConfidence | None]:
    """Compute the per-bar F&O derived cells: ``(moneyness, spot, intrinsic, time_value, days_to_expiry)``.

    Matches :data:`FNO_DERIVED_COLUMNS`. The combined ``moneyness`` label (bucket + signed
    strike-depth, ``"ITM -1"`` / ``"OTM +10"`` / ``"ATM"``) is classified against the
    underlying spot at THAT bar's instant (DEC-FNO-MONEYNESS-001) — time-varying, so a
    fixed-strike contract reads ``OTM +N`` then ``ITM -N`` as the spot moves. The VALUE
    columns (DEC-DB-FNO-COLUMNS-001) ride the SAME single per-bar spot join (no extra read):

    * ``spot`` — the underlying spot close at the bar (2-dp money string), ``None`` if absent;
    * ``intrinsic`` — :func:`_option_intrinsic` (2-dp), ``None`` when no spot;
    * ``time_value`` — ``close − intrinsic`` (2-dp), ``None`` when no spot;
    * ``days_to_expiry`` — pure ``(expiry − bar IST date).days`` (no I/O), resolvable for any
      decodable contract (option OR future), ``None`` only when undecodable / no timestamp.

    A FUTURE (or an undecodable symbol) reads ``moneyness="—"`` with ``None`` value columns
    and no spot read attempted — but still gets ``days_to_expiry`` when its expiry decodes; an
    option whose underlying spot is absent reads ``moneyness="?"`` with ``None`` values. Never
    raises — a per-bar browse never crashes on one contract whose spot was not pulled
    (Golden Rule #1).

    The per-bar spot join keys on the bar ``timestamp``; a deliberately ``timestamp``-less
    column projection therefore reads ``UNKNOWN``/``None`` on every bar (honest — the join key
    is absent — never a fabricated value), not a crash.

    Returns ``(rows, step_confidence)`` — the second element labels which ladder
    produced the ATM±N strike-grid step (DEC-MONEYNESS-REGISTRY-STEP-001):
    :attr:`~brutex.data.moneyness.StepConfidence.REGISTRY` when the AUTHORITATIVE
    vendor chain (read from *registry_path* via the generation-memoised
    :func:`_registry_strike_chain`) supplied it,
    :attr:`~brutex.data.moneyness.StepConfidence.OBSERVED` when only the sparse
    lake-observed sibling ladder did (the page then carries an honest low-confidence
    marker — the observed min gap can overestimate the true base step), and ``None``
    when no step was derivable at all (or the page is a future/undecodable contract).
    """
    parts = parse_fno_symbol(symbol)
    option_type = parts.option_type if parts is not None else None
    strike = parts.strike if parts is not None else None
    underlying = parts.underlying if parts is not None else None
    expiry = parts.expiry if parts is not None else None
    ts_idx = out_columns.index("timestamp") if "timestamp" in out_columns else -1
    close_idx = out_columns.index("close") if "close" in out_columns else -1

    def _dte(ts: Any) -> int | None:
        """Calendar days from the bar's IST date to expiry — pure, ``None`` if unknown."""
        if expiry is None or not isinstance(ts, datetime):
            return None
        return (expiry - _bar_ist_date(ts)).days

    # A future / undecodable contract (or an option with no decoded strike) has no
    # moneyness/value — "—" + None on every bar, no spot read; days_to_expiry still resolves
    # for a future whose expiry decoded. Narrowing on ``strike`` here lets the option branch
    # below treat it as a definite ``Decimal`` (an option always decodes a strike, else
    # ``parse_fno_symbol`` returns ``None`` → caught by ``underlying is None``).
    if option_type is None or underlying is None or strike is None:
        na_label = moneyness_view(option_type, strike, None, None).label
        return [
            (na_label, None, None, None, _dte(row[ts_idx] if ts_idx >= 0 else None))
            for row in raw_rows
        ], None

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
    spot_by_ts = _underlying_spot_close_by_ts(
        conn,
        lake_root,
        exchange=exchange,
        underlying=underlying,
        interval=interval,
        ts_values=ts_values,
    )
    rows: list[tuple[str, str | None, str | None, str | None, int | None]] = []
    for row in raw_rows:
        ts = row[ts_idx] if ts_idx >= 0 else None
        spot = spot_by_ts.get(_utc_instant(ts)) if isinstance(ts, datetime) else None
        view = moneyness_view(option_type, strike, spot, step)
        if spot is None:
            rows.append((view.label, None, None, None, _dte(ts)))
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
            )
        )
    return rows, resolved_step.confidence


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
) -> dict[str, Any]:
    """Read one page of expired-option bars for a series (DEC-DB-QUERY-001).

    The options-lake analog of :func:`query_candles`: reads the Parquet lake
    directly via DuckDB ``read_parquet`` over the series glob
    ``<lake>/options/<series>/*.parquet`` (the layout
    :class:`~brutex.data.option_parquet_writer.OptionParquetWriter` writes) — no
    data duplication, the lake stays the single source of truth. *series* is the
    filesystem-safe segment from :func:`list_option_datasets` and is validated
    against :func:`_validate_option_segment` (NO free user text in the glob,
    same path/SQL-injection guard as the spot reader). The page is
    ``LIMIT``-bounded so a click never materialises more than *limit* rows.

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
        on. When supplied, the ``COUNT(*)`` over the whole series glob is memoised
        per ``(series, start, end)`` (paging-invariant) so a prev/next click is an
        O(1) hit; a pull bumps *generation* → recompute (never a stale total).
        The SAME cache also memoises the default-range STORED-LIFE span under
        :func:`option_candle_span_key` (series-only key), so the applied-range chip
        (DEC-DB-UX-PACK-001 Feature 2) never re-runs its MIN/MAX scan on a paging
        click. The expired-options twin of the spot :func:`query_candles` cache.

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
    order_sql = ' ORDER BY "timestamp" ' + ("DESC" if descending else "ASC")
    skip_source = f"option-candle:{series}"
    # Read-side dedup gate (DEC-LAKE-READ-UNIQUE-001): the expired-options
    # rolling identity is the COMPOSITE ``(side, timestamp)`` — the SAME key the
    # writer itself dedups on (option_parquet_writer). The page collapses
    # poisoned-era duplicates keep-FIRST in one scan; the total counts DISTINCT
    # composite keys so it equals what the deduped pages serve.
    page_source = DEDUP_PARQUET_SOURCE_SQL
    qualify_sql = dedup_qualify_sql(("side", "timestamp"))

    def _count_total() -> int:
        """Run the O(files) deduped-total footer-scan over the whole series glob.

        Routed through the COMMON :func:`_execute_parquet_resilient` (the series
        dir is FLAT, so its recursive ``**/*.parquet`` glob reads the identical
        file set as the flat glob): a corrupt file is skipped + counted out of
        the total, never a 500 (DEC-ENGINE-MISC-PACK-002).
        """
        rows = _execute_parquet_resilient(
            conn,
            base,
            f"SELECT {distinct_count_sql(('side', 'timestamp'))} FROM {source}{where_sql}",  # noqa: S608  # nosec B608 - source is read_parquet(?) with a bound path; values bound
            list(where_params),
            source=skip_source,
        )
        return int(rows[0][0]) if rows and rows[0][0] is not None else 0

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
    out_columns, raw_rows = _execute_candle_page_resilient(
        conn,
        base,
        f"SELECT {page_select} FROM {page_source}{where_sql}{qualify_sql}{order_sql} LIMIT ? OFFSET ?",  # noqa: S608  # nosec B608 - validated select + bound path + bound values
        [*where_params, safe_limit, safe_offset],
        source=skip_source,
    )
    # Split-brain screen (DEC-DASHBOARD-READ-SCREEN-001), INSANE-ONLY for options:
    # positivity/ordering-impossible option bars are dropped so /db-options never
    # SERVES a corrupt bar, but the ×4 bad-tick screen is DISABLED
    # (`bad_tick_enabled=False`) — option PREMIA legitimately move ≥4×/minute on
    # leverage, so the index-circuit-breaker-calibrated threshold would drop legit
    # bars; the engine never screens options either. This MATCHES /movers-options
    # (which passes bad_tick=False through _read_future_session), so the two agree.
    # No bad-tick seed is needed (the cross-row arm is off).
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
        a 500, never a fabricated span (DEC-ENGINE-MISC-PACK-002).
        """
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


#: Resident-key bound for the shared ledger read cache: 3 key families
#: (records / rows / spot-rollup) × multi-lake test headroom.
_LEDGER_READ_CACHE_CAPACITY: Final[int] = 32

#: Hard cap on the ledger read cache's adaptive floor — the #920 ``SummaryCache``
#: cap (``summary_cache.MAX_ADAPTIVE_FLOOR_S``), NOT the 600 s movers default:
#: this floor is not pull-probe-scoped (the probe above is constant ``True``),
#: so the tighter cap bounds the worst-case served staleness.
_LEDGER_READ_FLOOR_CAP_S: Final[float] = 120.0


def _make_ledger_read_cache(
    *, clock: Callable[[], float] = _monotonic_clock
) -> MoversSnapshotCache:
    """Construct THE shared stamped ledger read cache — parameters exist ONCE.

    Single-flight + the #920 adaptive floor, both REUSED from the verified
    :class:`~brutex.dashboard.movers_cache.MoversSnapshotCache` contract (no
    third hand-rolled cache — CLAUDE.md §11 COMMON). *clock* is injectable so
    the floor tests are deterministic (production keeps the monotonic default).
    """
    return MoversSnapshotCache(
        capacity=_LEDGER_READ_CACHE_CAPACITY,
        pull_active_probe=_ledger_stamp_moved,
        adaptive_floor=True,
        adaptive_floor_cap_s=_LEDGER_READ_FLOOR_CAP_S,
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
#: The DERIVED families (rows / spot-rollup) are stamped with the generation
#: the records were ACTUALLY served at (:func:`_ledger_records_stamped` embeds
#: it in the cached value), so a projection built from a floor-stale records
#: serve is itself generation-stale — never latched under the fresh generation
#: (the fix-round latch defect, Golden Rule #1). A derived entry then ages
#: under its OWN floor once the records floor passes, so a derived family's
#: worst-case staleness COMPOSES the records floor with its own (each capped
#: at 120 s) — every stale serve logged on the key that served it.
_LEDGER_READ_CACHE: MoversSnapshotCache = _make_ledger_read_cache()


def _reset_ledger_read_cache() -> None:
    """Test seam — replaces the dict-clear idiom of the retired module dicts."""
    global _LEDGER_READ_CACHE  # noqa: PLW0603 — the documented test seam
    _LEDGER_READ_CACHE = _make_ledger_read_cache()


#: The ledger's incremental-delta sub-directory name (DEC-LEDGER-DELTA-FLUSH-001).
#: MUST equal ``brutex.data.window_ledger_store.DELTAS_DIRNAME`` — pinned by
#: test, not imported at module level (the ``lake_revision._SWEEPS_DIRNAME``
#: precedent: this module deliberately carries no module-level
#: ``window_ledger_store`` edge — CodeQL ``py/cyclic-import``).
_LEDGER_DELTAS_DIRNAME: Final[str] = "deltas"


def _ledger_delta_files(ledger_file: Path) -> tuple[Path, ...]:
    """The pending ledger delta Parquets, in append (filename) order — ``()`` if none.

    DEC-LEDGER-DELTA-FLUSH-001: the mid-pull flush now APPENDS small deltas
    beside the base ``ledger.parquet`` instead of rewriting it; every ledger
    reader in this module must see them or the /coverage surfaces freeze for the
    whole pull. Derived from *ledger_file* (``<dir>/deltas/*.parquet``) so no
    ``window_ledger_store`` import edge is added. O(deltas) listing.
    """
    return tuple(sorted((ledger_file.parent / _LEDGER_DELTAS_DIRNAME).glob("*.parquet")))


def _ledger_cache_stamp(ledger_file: Path) -> tuple[int, tuple[str, ...]]:
    """The composite freshness stamp for the ledger read caches — O(deltas).

    ``(base mtime_ns, delta filenames)``: a full save moves the base mtime; a
    delta append/prune changes the (immutable-once-written) delta name set — so
    any flush shape invalidates. A missing base (a first pull's mid-flight
    delta-only checkpoint) stamps ``0``, never raises.
    """
    try:
        base_mtime = ledger_file.stat().st_mtime_ns
    except FileNotFoundError:
        base_mtime = 0
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
    try:
        base_mtime = ledger_file.stat().st_mtime_ns
        base_present = True
    except FileNotFoundError:
        base_mtime = 0
        base_present = False
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
    The derived key families (``ledger-rows`` / ``spot-rollup``) MUST stamp
    their projections with THIS served generation: a projection built from
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
    """The /db window-ledger grid's row tuples, parsed ONCE per ledger stamp.

    DEC-SUSPICION-SWEEP-FIXES-001 (D3): the ``/api/v1/db/window-ledger`` route
    previously ran an UNCACHED ``load_window_ledger(...).to_rows()`` — a full
    Parquet re-parse of the whole ledger (a pre-planned full-universe options
    pull holds ~841k rows, the measured shape behind :data:`WINDOW_ROWS_CAP`) on
    EVERY paging click. This helper routes the read through the SAME stamp-keyed
    ``_ledger_records_cached`` single parse the other ledger readers share, then
    memoises the row projection (the canonical
    :func:`~brutex.data.window_ledger.ledger_record_row` per record — byte-equal
    to ``WindowLedger.to_rows`` by construction, ONE source of the shape) as a
    SEPARATE key family (``ledger-rows|<path>``) on THE shared
    :data:`_LEDGER_READ_CACHE` (DEC-LEDGER-DELTA-O-PAGE-001) — a grid poll never
    rebuilds the O(rows) tuple projection either. A paging click within one
    ledger stamp is an O(1) cache handoff; the composite stamp token (base mtime
    PLUS the pending delta name set, DEC-LEDGER-DELTA-FLUSH-001) re-keys it on a
    full save OR a mid-pull delta append. A lake with no ledger Parquet AND no
    pending deltas returns ``()`` — the same empty grid the uncached read
    produced; a base-less DELTA-ONLY lake (a first pull's mid-flight checkpoint)
    now renders its live rows like every other ledger reader (the fixed
    base-less inconsistency — ``_ledger_records_cached`` always read it, this
    projection returned ``()``). The projection is stamped with the SERVED
    records generation (:func:`_ledger_records_stamped` — never the fresh stamp
    a floor-stale records serve did not reach), so rows built from floor-stale
    records stay generation-stale and recompute once the records floor passes
    and this entry's own floor permits — never latched (the fix-round latch
    defect, Golden Rule #1).
    HONEST envelope (Golden Rule #10): the first read per stamp is still O(rows)
    (parse + projection), and staleness after a stamp move is TWO-phase — the
    records adaptive floor (``min(max(2 s, the parse's own duration), 120 s)``)
    serves the prior rows first, and once it passes this ``ledger-rows`` entry
    serves stale under its OWN #920 floor (``min(max(2 s, the projection's own
    duration), 120 s)``), so the two floors COMPOSE: worst-case staleness =
    records floor + rows floor, EACH capped at 120 s (possibly spanning several
    ≥2 s delta flushes — a time bound, never a stamp-count bound). EVERY stale
    serve is logged (``movers_cache.stale_served``) on the key that actually
    served it — ``ledger-records`` in the first phase, ``ledger-rows`` in the
    second; only the REPEAT read is O(1) — true O(page) needs a write-time
    manifest, NOT claimed (the #867/#1025 envelope).
    """
    from brutex.data.window_ledger import ledger_record_row
    from brutex.data.window_ledger_store import ledger_path

    path = ledger_path(lake_root)
    if not path.is_file() and not _ledger_delta_files(path):
        return ()
    # The records read runs FIRST (O(1) on a hit) so the projection is stamped
    # with the generation the records were ACTUALLY served at — the compute
    # closes over that exact snapshot, never re-reading under a fresher stamp.
    generation, records = _ledger_records_stamped(lake_root)
    return _LEDGER_READ_CACHE.get_or_compute(
        f"ledger-rows|{path.as_posix()}",
        generation=generation,
        compute=lambda: tuple(ledger_record_row(r) for r in records),
    )


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


class WindowCountsCache(Protocol):
    """Generation-stamped cache contract for the ``/coverage/windows`` GROUP BY counts.

    Structural (``Protocol``) so this module never hard-imports a concrete cache — the
    dashboard injects the COMMON generation-stamped LRU
    (:class:`~brutex.dashboard.movers_cache.MoversSnapshotCache`, reused per CLAUDE.md
    §11, DEC-COVERAGE-WINDOWS-O1-001). Mirrors :class:`CandleCountCache` but memoises
    the ``(bucketed_counts, total)`` tuple, not a bare int.
    """

    def get_or_compute(
        self,
        key: str,
        *,
        generation: int,
        compute: Callable[[], tuple[dict[str, dict[str, int]], int]],
    ) -> tuple[dict[str, dict[str, int]], int]:
        """Return *key*'s cached ``(counts, total)`` for *generation*, computing on a miss."""
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
    (:func:`_window_counts_total`). The cache key rides the composite ledger stamp
    token at a FIXED ``generation=0`` (DEC-LEDGER-DELTA-O-PAGE-001 — mid-pull
    ``mark_dirty`` fires per window tick, far more often than the ≥2 s ledger
    flush, so generation-keying would miss every poll; the stamp token is the
    COMPLETE content signal — never stale, Golden Rule #1). The *generation*
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

    def _counts() -> dict[str, dict[str, int]]:
        """ONE counts read — direct or through the injected stamp-keyed cache."""
        # Re-assembled per attempt (fresh delta listing) so the torn-read retry
        # below self-heals the save-time prune race (DEC-LEDGER-DELTA-O-PAGE-001).
        source = _ledger_scan_source(ledger_file)
        if count_cache is None:
            counts, _total = _window_counts_total(conn, source)
        else:
            # The composite stamp token keys the cache (base mtime + delta name
            # set), so a CROSS-PROCESS write (a CLI `brutex fetch` while the
            # dashboard is open) AND a mid-pull delta append both invalidate —
            # the SAME key `_build_window_payload_duckdb` uses, so the two
            # routes share one warm entry.
            counts, _total = count_cache.get_or_compute(
                f"window-counts|{ledger_file.as_posix()}|{source.stamp_token}",
                generation=0,
                compute=lambda: _window_counts_total(conn, source),
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
) -> dict[str, Any]:
    """O(page)-on-repeat ``/coverage/windows`` payload via DuckDB (DEC-COVERAGE-WINDOWS-O1-001).

    Counts come from ONE stamp-cached ``GROUP BY`` (:func:`_window_counts_total`);
    the row page is a ``WHERE``-filtered, severity-ordered ``LIMIT``/``OFFSET`` query — so
    a tab/filter click reads only the page instead of re-running an O(N) Python
    count+filter+sort over every window on every click + 4 s poll (the root cause). The
    ``matched`` total is DERIVED from the cached counts (no extra scan); the per-row
    projection reuses the shared :func:`_window_row` (ONE shape with the legacy path).
    Returns the same payload dict shape + values as the pyarrow path — severity-first,
    with within-a-status-tier rows ordered deterministically by ``(security_id,
    interval, window_start, window_end)``.

    **Mid-pull deltas (DEC-LEDGER-DELTA-O-PAGE-001):** pending ledger deltas are
    folded latest-wins IN SQL — every query scans the ONE assembled
    :func:`_ledger_scan_source` (the steady-state ``read_parquet(?)`` literal
    when no deltas exist — SQL text byte-identical, spy-pinned — else the
    :func:`~brutex.data.lake_read.dedup_latest_source_sql` fold subquery), and
    the count-cache keys ride the composite stamp token at a FIXED
    ``generation=0`` (mid-pull ``mark_dirty`` fires per window tick, far more
    often than the ≥2 s ledger flush, so generation-keying would miss every
    poll; the stamp token is the COMPLETE content signal — a full save moves the
    base mtime, an append/prune moves the delta name set, a CLI cross-process
    write moves both — never stale, Golden Rule #1). The *generation* parameter
    is retained for signature compatibility. HONEST envelope (Golden Rule #10):
    mid-pull, the folded source is ONE O(total physical rows incl. every delta
    file's footer) columnar hash-partition per fresh stamp (the ≥2 s flush
    cadence bounds stamp movement), O(1)-cached between stamps via the composite
    stamp token; NOT literal O(page) — the same envelope ``query_candles``
    already accepts. The O(delta-file-count) footer-open term grows over a
    multi-hour pull until the pull-end save subsumes the deltas; delta
    compaction is NAMED future work, not claimed.

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
        available = {
            str(col[0])
            for col in (
                conn.execute("SELECT * FROM read_parquet(?) LIMIT 0", [ledger_glob]).description
                or []
            )
        }
        if not {"empty_streak", "in_pull_fetches"}.issubset(available):
            raise ValueError("legacy-shape ledger Parquet — defer to the pyarrow derive path")
    counts_since: dict[str, dict[str, int]] | None = None
    if count_cache is None:
        counts, total = _window_counts_total(conn, source)
        if since is not None:
            counts_since, _since_total = _window_counts_since(conn, source, since)
    else:
        # Key the cache on the composite stamp token (base mtime + the pending delta
        # name set) at a FIXED generation=0 — so a CROSS-PROCESS write (a CLI `brutex
        # fetch` while the dashboard is open — which does NOT bump the dashboard's own
        # in-process generation) AND a mid-pull delta append both invalidate, while a
        # mark_dirty storm at an unchanged ledger never thrashes the entry. A repeat
        # click within one stamp stays an O(1) hit.
        counts, total = count_cache.get_or_compute(
            f"window-counts|{ledger_glob}|{source.stamp_token}",
            generation=0,
            compute=lambda: _window_counts_total(conn, source),
        )
        if since is not None:
            # SAME memo pattern as the lifetime counts (REUSE, no new cache class —
            # DEC-TELEMETRY-SCOPE-BUDGET-RUNCOUNTS-001): the pull-start token joins the
            # key so a NEW pull (new journal `started`) can never be served the prior
            # run's per-run figures even within one stamp.
            since_key = f"window-counts-since|{ledger_glob}|{source.stamp_token}|{since}"
            counts_since, _since_total = count_cache.get_or_compute(
                since_key,
                generation=0,
                compute=lambda: _window_counts_since(conn, source, since),
            )

    wanted_status = status.upper() if status else None
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
    if wanted_status:
        where.append("status = ?")
        where_params.append(wanted_status)
    else:
        where.append("status != ?")
        where_params.append("PLANNED")
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    order_sql = (
        f" ORDER BY {_window_severity_order_sql()}, "
        '"security_id", "interval", "window_start", "window_end"'
    )
    # WHERE / ORDER / LIMIT apply OUTSIDE the fold subquery (ghost-row safe —
    # a superseded row version can never resurrect through a filter); the
    # source params bind FIRST (the subquery precedes them in the SQL text).
    params: list[Any] = [*source.params, *where_params, limit, offset]
    cursor = conn.execute(
        f"SELECT * FROM {source.from_sql}{where_sql}{order_sql} LIMIT ? OFFSET ?",  # noqa: S608  # nosec B608 - SELECT * over a module-built trusted source; filters/page/order are bound or trusted constants
        params,
    )
    out_columns = [str(col[0]) for col in (cursor.description or [])]
    rows: list[dict[str, Any]] = []
    for raw in cursor.fetchall():
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
        rows.append(
            _window_row(
                record, first_seen_at=cell.get("first_seen_at"), symbol_for_id=symbol_for_id
            )
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
        Row-list cap (clamped to ``[1, WINDOW_ROWS_CAP]``).
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

    limit = max(1, min(int(limit), WINDOW_ROWS_CAP))
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

    counts = _window_counts_from_records(records)

    wanted_status = status.upper() if status else None
    selected = [
        r
        for r in records
        if (market is None or _window_market_bucket(r.market) == market)
        and (wanted_status == r.status if wanted_status else r.status != "PLANNED")
    ]
    selected.sort(key=lambda r: _WINDOW_STATUS_ORDER.get(r.status, 9))
    rows = [
        # The legacy status log has no first_seen_at — getattr keeps ONE shape.
        _window_row(r, first_seen_at=getattr(r, "first_seen_at", None), symbol_for_id=symbol_for_id)
        for r in selected[:limit]
    ]
    return {
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


def registry_counts(registry_path: Path) -> dict[str, Any]:
    """Return ``{"expiries", "contracts", "exists", "note"}`` for the registry cache.

    A MISSING ``registry.duckdb`` degrades honestly to ``{"expiries": 0,
    "contracts": 0, "exists": False, "note": <missing note>}`` — never opens /
    creates a file just to count it (so a count never side-effects a brand-new
    empty cache) and never invents rows (Golden Rule #1). When the file exists the
    counts are read read-only via :class:`~brutex.data.registry_store.RegistryStore`
    (which is closed before returning), and ``note`` is ``None``.

    Parameters
    ----------
    registry_path
        The ``registry.duckdb`` path (see :func:`registry_path_for`).

    Returns
    -------
    dict[str, Any]
        ``{"expiries": <int>, "contracts": <int>, "exists": <bool>,
        "note": <str|None>}``.
    """
    if not registry_path.is_file():
        return {"expiries": 0, "contracts": 0, "exists": False, "note": _REGISTRY_MISSING_NOTE}
    from brutex.data.registry_store import RegistryStore

    store = RegistryStore.open(registry_path)
    try:
        return {
            "expiries": store.count_expiries(),
            "contracts": store.count_contracts(),
            "exists": True,
            "note": None,
        }
    finally:
        store.close()


def list_registry_expiries(
    registry_path: Path,
    *,
    exch: str | None = None,
    underlying: str | None = None,
    limit: int = _LIMIT_MAX,
    offset: int = 0,
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
        total = store.count_expiries(exch=exch, underlying=underlying)
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
        total = store.count_contracts(exch=exch, underlying=underlying, expiry_date=parsed_expiry)
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
    "WINDOW_PHASES",
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
    "registry_counts",
    "registry_path_for",
    "resolve_applied_range",
    "sample_rows",
    "spot_window_rollup",
    "summarize_option_datasets",
    "summarize_spot_datasets",
    "summarize_spot_verdict_datasets",
    "table_summary",
    "window_ledger_rows_cached",
    "window_status_label",
]
