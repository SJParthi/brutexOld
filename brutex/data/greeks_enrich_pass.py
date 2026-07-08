"""The COMMON fenced Greeks-enrichment pass + wheel-free lake target discovery.

DEC-CLI-GREEKS-ENRICH-001 — ONE source of truth for the fence/telemetry
contract run by BOTH the dashboard F&O pull tail
(:func:`brutex.dashboard.historical_pull._enrich_greeks_from_minute`, now a
thin delegator into :func:`run_greeks_enrich_pass`) and the CLI (the
``brutex fetch`` post-pull tail + the ``brutex enrich-greeks`` subcommand).
Closes the DEC-GREEKS-CORRECTNESS-AUDIT-001 (#1188) named follow-up "a
CLI-pulled lake never enriches".

Wheel discipline
----------------
This module NEVER imports :mod:`brutex.data.greeks_enrich` (which transitively
imports the ``trading_core`` PyO3 wheel via :mod:`brutex.options.greeks`) at
module top — the wheel import lives INSIDE the fence ``try`` of
:func:`run_greeks_enrich_pass` (and inside the :func:`lake_target_builder`
closure), so a missing wheel degrades to the honest ``ok=False`` outcome,
never a crash (the ``historical_pull`` fence convention). It also never
imports ``brutex.dashboard.*`` — this is a data-layer module; the dashboard
imports IT.

Discovery honesty (Golden Rule #10)
-----------------------------------
:func:`discover_option_windows` reads directory METADATA only — zero Parquet
opens, zero rows read — but it is NOT O(1): the walk is O(option-contract
directories + their ``1minute/`` month entries), which on a fully-materialised
options lake (10⁴–10⁶ contract dirs) is minutes of metadata I/O, and on a
pure-spot CLI lake is ~2 readdirs (:func:`has_option_candles` is the cheap
pre-gate for that dominant CLI case). The enrichment pass itself is O(scoped
stored option bars) READ on every run — the driver's ``already_current`` stamp
gate skips WRITES and SOLVES on re-runs, not the per-bar reads.

Windows are CHAIN-UNION month spans: contracts grouped by
``(exchange, underlying, expiry)`` share ONE window spanning the union of the
group's stored months (widened to whole months, clamped to the optional
``[start, end)``), deliberately reproducing the dashboard's
one-window-per-chain shape so the driver's capacity-2 spot-join memo
(``greeks_enrich._SpotJoinMemo``, keyed ``(exchange, underlying, start,
end)``) collapses spot I/O to ~one read per chain. Stored-month spans — not
the expiry window law — are used because explicit-date dashboard pulls can
store bars outside ``[expiry − lookback → expiry]``, and disk truth never
silently skips stored bars (Golden Rule #1).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Final

import structlog

from brutex.data.errors import GreeksEnrichPoolBroken, LakeWriteHalt
from brutex.data.fno_candle_lake import FNO_CANDLE_LAKE_SEGMENT
from brutex.data.fno_symbol import OPTION_TYPES, parse_fno_symbol
from brutex.data.greeks_revision import touch_greeks_revision_marker
from brutex.data.partition import (
    BAR_DIR_NAME,
    MAX_PARTITION_YEAR,
    MIN_PARTITION_YEAR,
    MONTHS_PER_YEAR,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from structlog.typing import FilteringBoundLogger

    from brutex.data.greeks_enrich import GreeksEnrichTarget

_LOG = structlog.get_logger("brutex.data.greeks_enrich_pass")

#: Env gate for the ``brutex fetch`` post-pull stage. unset/``""``/``"1"``/
#: ``"on"``/``"true"`` (case-insensitive) → ON (the default); ``"0"``/``"off"``/
#: ``"false"`` → OFF; anything else → fail-loud :class:`ValueError` (the
#: DEC-RAM-BUDGETS-001 env-override convention: junk is never silently coerced).
CLI_GREEKS_ENRICH_ENV: Final[str] = "BRUTEX_CLI_GREEKS_ENRICH"

#: Stripped/lowercased env tokens that ENABLE the fetch-path stage (unset and
#: blank included — default-ON, the Golden-Rule-#1 grounding of the decision).
_ENV_ON_TOKENS: Final[frozenset[str]] = frozenset({"", "1", "on", "true"})

#: Stripped/lowercased env tokens that DISABLE the fetch-path stage.
_ENV_OFF_TOKENS: Final[frozenset[str]] = frozenset({"0", "off", "false"})

#: The one fetched granularity — the only interval the Greeks pass enriches
#: (DEC-1MINUTE-ONLY-DERIVE-1DAY-001; derived TFs carry no Greeks overlay).
_MINUTE_INTERVAL: Final[str] = "1minute"

#: Month partition file suffix under ``<contract>/1minute/<yyyy>/``.
_PARQUET_SUFFIX: Final[str] = ".parquet"

#: Digit width of a canonical year partition dir (the writer's ``{year:04d}``).
_YEAR_DIR_WIDTH: Final[int] = 4

#: Digit width of a canonical month file stem (the writer's ``{month:02d}``).
_MONTH_STEM_WIDTH: Final[int] = 2


@dataclass(frozen=True, slots=True)
class GreeksEnrichOutcome:
    """The honest outcome of one post-pull Greeks-enrichment pass.

    MOVED (semantics intact) from the dashboard's private
    ``historical_pull._GreeksEnrichOutcome`` (DEC-GREEKS-ENRICH-OBSERVE-001 →
    DEC-CLI-GREEKS-ENRICH-001): the tiny value the fully-guarded
    :func:`run_greeks_enrich_pass` fence returns so each caller — the
    dashboard's :func:`~brutex.dashboard.historical_pull.pull_contract_candles`
    report/note and the CLI's post-fetch / ``enrich-greeks`` summaries — can
    surface the pass instead of one swallowed log line.

    Attributes
    ----------
    ok
        ``True`` = the pass COMPLETED (per-target quarantines ride
        :attr:`errored_targets`, they never flip ``ok`` — one corrupt contract
        must not read as a whole-pass failure); ``False`` = the WHOLE pass
        failed (see :attr:`error`); ``None`` = the pass did not run (dry-run /
        the spot-only pre-gate skip).
    error
        ``f"{type(exc).__name__}: {exc}"`` on a whole-pass failure; ``None``
        otherwise.
    errored_targets
        The driver's per-target quarantine tally (the success branch's
        ``report.errored``); ``0`` on a whole-pass failure / not-run.
    """

    ok: bool | None
    error: str | None
    errored_targets: int


#: The "pass did not run" outcome (dry-run / pre-gate skip — nothing enriched).
GREEKS_NOT_RUN: Final[GreeksEnrichOutcome] = GreeksEnrichOutcome(
    ok=None, error=None, errored_targets=0
)

#: Zero-arg target factory, invoked INSIDE the fence of
#: :func:`run_greeks_enrich_pass` — a discovery/build error (or a
#: missing-wheel ``GreeksEnrichTarget`` import) degrades to ``ok=False``,
#: never a crash.
type TargetBuilder = Callable[[], Sequence[GreeksEnrichTarget]]


def run_greeks_enrich_pass(
    *,
    build_targets: TargetBuilder,
    lake_root: Path,
    log: FilteringBoundLogger | None = None,
) -> GreeksEnrichOutcome:
    """Run one fenced Greeks-enrichment pass — best-effort, never a crash.

    The fence MOVED statement-for-statement from the dashboard's
    ``historical_pull._enrich_greeks_from_minute`` (which is now a thin
    delegator into this function): the ``trading_core``-wheel-heavy
    :mod:`brutex.data.greeks_enrich` import, the risk-free source resolution
    (the locked PR-1 seam — fixed 6.5% default, ``BRUTEX_RISK_FREE_CSV``
    override), the target build, and the driver call ALL run inside one
    ``try`` — in that order — so a missing wheel, a broken rate CSV, a
    discovery error, or an unexpected driver error each log ONE
    ``fno.greeks_enrich.error`` and return ``ok=False``; those can never fail
    the host pull. TWO deliberate exceptions
    (DEC-GREEKS-PARALLEL-001, the #1188 follow-up (a)):
    :class:`~brutex.data.errors.LakeWriteHalt` (disk full / OOM at a lake
    write) and :class:`~brutex.data.errors.GreeksEnrichPoolBroken` (a Greeks
    pool worker died abruptly — a host-resource condition) RE-RAISE past the
    fence so they reach the caller's pull-halt handling + the CRITICAL
    Telegram alert. ``KeyboardInterrupt``/``SystemExit`` are
    :class:`BaseException` — NOT fenced, they propagate raw (deliberate).

    On success the pass logs one ``fno.greeks_enrich.done`` carrying the
    driver report's 13 counters verbatim, and — when the driver WROTE overlay
    bars — touches the SHARED cross-process Greeks-overlay freshness marker
    (:func:`~brutex.data.greeks_revision.touch_greeks_revision_marker`,
    best-effort) so a concurrently-running dashboard's ``lake_revision``-probed
    generation caches serve the new iv/delta on the next poll instead of the
    pre-enrich values until an unrelated pull or a restart (the pass moves no
    ``lake_revision`` component of its own — DEC-GREEKS-BULK-REENRICH-001 fix
    round 8, the round-2 backfill fix's sibling surface). Per-contract errors
    are quarantined INSIDE the driver (``report.errored`` →
    :attr:`GreeksEnrichOutcome.errored_targets`), so one bad contract never
    sinks the pass. The authoritative 1-minute candles are already durable in
    the lake; the Greeks overlay is a re-derivable convenience layer the next
    pass re-attempts (stamp-driven, idempotent — Golden Rule #7/#10).

    Parameters
    ----------
    build_targets
        Zero-arg factory yielding the pass's
        :class:`~brutex.data.greeks_enrich.GreeksEnrichTarget` rows — invoked
        INSIDE the fence, strictly AFTER rate-source resolution (the
        dashboard's order, preserved). The dashboard injects its rebuilt-
        contract loop; the CLI injects :func:`lake_target_builder`.
    lake_root
        The lake root the driver reads/writes under.
    log
        Injectable structlog logger — the dashboard delegator passes its own
        ``brutex.dashboard.historical_pull`` logger so even the logger-NAME
        metadata on the two pass events stays byte-identical; ``None`` (the
        CLI) uses this module's logger. Event names + fields are identical
        either way.

    Returns
    -------
    GreeksEnrichOutcome
        ``ok=True`` + the driver's per-target ``errored`` tally when the pass
        COMPLETED (per-target quarantines never flip ``ok``), or ``ok=False``
        + ``"ErrorType: message"`` when the whole pass failed.

    Raises
    ------
    LakeWriteHalt
        Re-raised unfenced — a pull-wide host condition (disk full / OOM).
    GreeksEnrichPoolBroken
        Re-raised unfenced — the parallel pool died (a host condition).
    """
    logger = _LOG if log is None else log
    try:
        from brutex.data.greeks_enrich import (
            enrich_greeks_for_targets,
            resolve_risk_free_source,
        )

        rate_source = resolve_risk_free_source()
        targets = list(build_targets())
        report = enrich_greeks_for_targets(
            targets=targets, lake_root=lake_root, rate_source=rate_source
        )
        logger.info(
            "fno.greeks_enrich.done",
            enriched=report.enriched,
            already_current=report.already_current,
            futures_skipped=report.futures_skipped,
            undecodable=report.undecodable,
            no_bars=report.no_bars,
            errored=report.errored,
            bars_written=report.bars_written,
            bars_solved=report.bars_solved,
            bars_null=report.bars_null,
            bars_reused=report.bars_reused,
            spot_reads=report.spot_reads,
            spot_hits=report.spot_hits,
            bars_skipped=report.bars_skipped,
        )
        if report.bars_written > 0:
            # The pass rewrote option iv/delta overlay partitions yet moves
            # NEITHER the window ledger NOR any other lake_revision component
            # — on the standalone `brutex enrich-greeks` path (and the `brutex
            # fetch` tail, which runs AFTER the final end_pull_ledger save) a
            # running dashboard's generation-keyed caches kept serving the
            # PRE-enrich Greeks indefinitely, the exact cross-process
            # staleness class the backfill's fix round 2 closed. Touch the
            # SHARED Greeks-overlay freshness marker (best-effort — the
            # helper fences OSError into one WARN) so the dashboard's
            # revision probe sees the write on its next poll
            # (DEC-GREEKS-BULK-REENRICH-001 fix round 8, refuter-confirmed).
            touch_greeks_revision_marker(
                lake_root,
                fields={"writer": "greeks_enrich_pass", "bars_written": report.bars_written},
                failure_event="fno.greeks_enrich.revision_marker_failed",
            )
        return GreeksEnrichOutcome(ok=True, error=None, errored_targets=report.errored)
    except (LakeWriteHalt, GreeksEnrichPoolBroken):
        # A pull-wide HOST condition (disk full / OOM / a dead pool worker) —
        # re-raised past the fence so it reaches the caller's halt handling +
        # the CRITICAL Telegram alert (DEC-GREEKS-PARALLEL-001). Everything
        # else stays fenced below.
        raise
    except Exception as exc:
        logger.error("fno.greeks_enrich.error", error=str(exc))
        return GreeksEnrichOutcome(
            ok=False, error=f"{type(exc).__name__}: {exc}", errored_targets=0
        )


@dataclass(frozen=True, slots=True)
class DiscoveredOptionWindow:
    """One discovered option contract + its chain-union enrichment window.

    A WHEEL-FREE discovery row — target construction is deferred to
    :func:`lake_target_builder` so ``brutex enrich-greeks --dry-run`` can
    count contracts/chains with zero ``trading_core`` imports, zero writes.

    Attributes
    ----------
    exchange
        The ``bars/<exchange>/`` tree dir name — where the files PHYSICALLY
        live (deliberately the tree name, not the symbol-decoded token, so a
        drift-keyed partition is enriched where it actually sits).
    symbol
        The full contract ``groww_symbol`` dir name
        (``NSE-NIFTY-30Sep25-24650-CE``).
    underlying
        The decoded bare underlying token (``NIFTY`` / ``BAJAJ-AUTO``).
    expiry
        The decoded contract expiry — the chain-union group key component.
    start
        Half-open IST window start — the group's chain-union month span,
        clamped to the caller's optional ``[start, end)``.
    end
        Half-open IST window end (exclusive).
    """

    exchange: str
    symbol: str
    underlying: str
    expiry: date
    start: date
    end: date


def has_option_candles(lake_root: Path) -> bool:
    """Cheap pre-gate: does this lake hold ANY F&O candle partition at all?

    ``True`` iff any ``bars/<exchange>/FNO/`` dir exists and contains at least
    one entry. The dominant CLI case — a pure-spot lake — answers ``False`` in
    O(exchange dirs) + one ``next(iterdir())`` per ``FNO`` dir, letting the
    fetch seam skip discovery, the wheel import, and the rate resolution
    entirely. NEVER raises: any :class:`OSError` (an unreadable tree) returns
    ``False`` — the pass is then honestly skipped and re-attempted on the next
    pull (the overlay is re-derivable, never load-bearing).

    Parameters
    ----------
    lake_root
        The lake root to probe.

    Returns
    -------
    bool
        ``True`` when at least one F&O contract directory exists.
    """
    try:
        bars_root = lake_root / BAR_DIR_NAME
        if not bars_root.is_dir():
            return False
        for exchange_dir in bars_root.iterdir():
            if not exchange_dir.is_dir():
                continue
            fno_dir = exchange_dir / FNO_CANDLE_LAKE_SEGMENT
            if not fno_dir.is_dir():
                continue
            if next(fno_dir.iterdir(), None) is not None:
                return True
        return False
    except OSError:
        return False


def _is_canonical_digits(token: str, width: int) -> bool:
    """``True`` iff *token* is EXACTLY *width* ASCII decimal digits.

    ``str.isdigit`` alone is a junk-tolerance trap (review round-1 fix,
    DEC-CLI-GREEKS-ENRICH-001): a NON-decimal Unicode digit (superscript
    ``"²024"``) passes ``isdigit()`` yet makes ``int()`` raise ``ValueError``
    — which would propagate out of discovery and flip the WHOLE pass to
    ``ok=False`` — and a non-ASCII DECIMAL digit (Arabic-Indic ``"٢٠٢٤"``)
    passes both and would be silently accepted as a canonical year. The lake
    writer only ever emits ASCII ``{year:04d}`` / ``{month:02d}`` names, so
    anything non-ASCII is junk to SKIP silently, never to parse.

    Parameters
    ----------
    token
        The candidate year-dir name or month-file stem.
    width
        The exact canonical digit width (:data:`_YEAR_DIR_WIDTH` /
        :data:`_MONTH_STEM_WIDTH`).

    Returns
    -------
    bool
        ``True`` when ``int(token)`` is safe AND the name is canonical.
    """
    return len(token) == width and token.isascii() and token.isdigit()


def _stored_months(interval_dir: Path) -> frozenset[tuple[int, int]]:
    """Collect a contract's stored ``(year, month)`` set from its month files.

    One bounded readdir pass over ``<contract>/1minute/<yyyy>/<mm>.parquet``:
    4-digit ASCII-numeric year dirs (:func:`_is_canonical_digits` — plain
    ``isdigit`` admits non-decimal Unicode digits that make ``int()`` raise)
    bounded to the canonical partition-year window
    ``[MIN_PARTITION_YEAR, MAX_PARTITION_YEAR]`` (the writer's own validation
    window — anything outside is junk, and the bound keeps the month-after
    span endpoint constructible), 2-digit ASCII ``NN.parquet`` stems in
    ``01..12``; junk names are skipped silently (the
    :func:`parse_fno_symbol` tolerance convention). A missing ``1minute/``
    dir yields the empty set.

    Parameters
    ----------
    interval_dir
        The contract's ``1minute/`` partition directory.

    Returns
    -------
    frozenset[tuple[int, int]]
        The stored ``(year, month)`` pairs — empty when none.
    """
    if not interval_dir.is_dir():
        return frozenset()
    months: set[tuple[int, int]] = set()
    for year_dir in interval_dir.iterdir():
        year_name = year_dir.name
        if not year_dir.is_dir() or not _is_canonical_digits(year_name, _YEAR_DIR_WIDTH):
            continue
        year = int(year_name)
        if not MIN_PARTITION_YEAR <= year <= MAX_PARTITION_YEAR:
            continue
        for month_file in year_dir.iterdir():
            month_name = month_file.name
            if not month_name.endswith(_PARQUET_SUFFIX):
                continue
            stem = month_name[: -len(_PARQUET_SUFFIX)]
            if not _is_canonical_digits(stem, _MONTH_STEM_WIDTH):
                continue
            month = int(stem)
            if 1 <= month <= MONTHS_PER_YEAR:
                months.add((year, month))
    return frozenset(months)


def _first_day_of_next_month(year: int, month: int) -> date:
    """Return the first day of the month AFTER ``(year, month)``.

    The half-open month-span end for a stored ``(year, month)`` maximum;
    ``year`` is pre-bounded ``<= MAX_PARTITION_YEAR`` by :func:`_stored_months`
    so the December rollover is always constructible.

    Parameters
    ----------
    year
        Calendar year of the last stored month.
    month
        Calendar month (``1..12``) of the last stored month.

    Returns
    -------
    datetime.date
        ``date(year, month + 1, 1)`` — or ``date(year + 1, 1, 1)`` after
        December.
    """
    if month == MONTHS_PER_YEAR:
        return date(year + 1, 1, 1)
    return date(year, month + 1, 1)


def discover_option_windows(
    lake_root: Path,
    *,
    underlyings: frozenset[str] | None = None,
    start: date | None = None,
    end: date | None = None,
) -> tuple[DiscoveredOptionWindow, ...]:
    """Discover stored option contracts + chain-union windows from the lake tree.

    One bounded directory traversal of
    ``bars/<exchange>/FNO/<contract>/1minute/<yyyy>/<mm>.parquet`` — zero
    Parquet opens, zero rows read (honest cost: O(option-contract dirs +
    their month entries) in directory-metadata operations, NOT O(1)). Each
    contract dir name is decoded by the COMMON :func:`parse_fno_symbol`
    (junk → skipped silently); ONLY ``CE``/``PE`` legs are enumerated (``FUT``
    is filtered here, so a CLI pass's ``futures_skipped`` counter honestly
    reads 0 — a documented asymmetry with the dashboard path, which still
    passes FUT targets for the driver to tally-skip).

    CHAIN-UNION windows: survivors are grouped by
    ``(exchange, underlying, expiry)``; the group window spans the UNION of
    the group's stored months — ``[date(min_y, min_m, 1),
    first-day-after(max_y, max_m))`` — intersected with the optional
    ``[start, end)`` clamp (an empty intersection drops the whole group).
    EVERY contract in the group carries the GROUP window, reproducing the
    dashboard's one-window-per-chain shape so the driver's spot-join memo
    collapses spot reads to ~one per chain. Month-granular windows lose
    nothing: the driver's reader enumerates only EXISTING month files and
    month-bounds each to its own UTC month, so a chain-union window over a
    sparse contract reads exactly that contract's real files.

    Parameters
    ----------
    lake_root
        The lake root to walk. A missing ``bars/`` tree yields ``()``.
    underlyings
        Optional filter — decoded ``parts.underlying`` must be IN this set
        (callers pass the union of ``underlying_match_tokens``, which carries
        bare AND exchange-prefixed forms); ``None`` = all underlyings.
    start
        Optional half-open clamp start (IST date, inclusive).
    end
        Optional half-open clamp end (IST date, exclusive).

    Returns
    -------
    tuple[DiscoveredOptionWindow, ...]
        One row per surviving contract, deterministically sorted by
        ``(exchange, underlying, expiry, symbol)`` — independent of directory
        creation order (the driver re-sorts to its own key anyway).
    """
    bars_root = lake_root / BAR_DIR_NAME
    if not bars_root.is_dir():
        return ()
    groups: dict[tuple[str, str, date], tuple[set[tuple[int, int]], list[str]]] = {}
    for exchange_dir in sorted(bars_root.iterdir()):
        if not exchange_dir.is_dir():
            continue
        fno_dir = exchange_dir / FNO_CANDLE_LAKE_SEGMENT
        if not fno_dir.is_dir():
            continue
        for contract_dir in sorted(fno_dir.iterdir()):
            if not contract_dir.is_dir():
                continue
            parts = parse_fno_symbol(contract_dir.name)
            if parts is None or parts.instrument_type not in OPTION_TYPES:
                continue
            if underlyings is not None and parts.underlying not in underlyings:
                continue
            months = _stored_months(contract_dir / _MINUTE_INTERVAL)
            if not months:
                continue
            key = (exchange_dir.name, parts.underlying, parts.expiry)
            month_union, symbols = groups.setdefault(key, (set(), []))
            month_union.update(months)
            symbols.append(contract_dir.name)
    rows: list[DiscoveredOptionWindow] = []
    for (exchange, underlying, expiry), (month_union, symbols) in groups.items():
        min_year, min_month = min(month_union)
        max_year, max_month = max(month_union)
        span_start = date(min_year, min_month, 1)
        span_end = _first_day_of_next_month(max_year, max_month)
        if start is not None and start > span_start:
            span_start = start
        if end is not None and end < span_end:
            span_end = end
        if span_start >= span_end:
            continue
        rows.extend(
            DiscoveredOptionWindow(
                exchange=exchange,
                symbol=symbol,
                underlying=underlying,
                expiry=expiry,
                start=span_start,
                end=span_end,
            )
            for symbol in symbols
        )
    rows.sort(key=lambda row: (row.exchange, row.underlying, row.expiry, row.symbol))
    return tuple(rows)


def lake_target_builder(
    lake_root: Path,
    *,
    underlyings: frozenset[str] | None = None,
    start: date | None = None,
    end: date | None = None,
) -> TargetBuilder:
    """Build a fence-safe target factory over the lake tree.

    Returns a zero-arg closure for :func:`run_greeks_enrich_pass`'s
    ``build_targets`` seam. BOTH the wheel import
    (:class:`~brutex.data.greeks_enrich.GreeksEnrichTarget` transitively
    imports the ``trading_core`` wheel) AND the
    :func:`discover_option_windows` walk run INSIDE the closure — i.e. inside
    the fence — so a missing wheel or a discovery :class:`OSError` degrades to
    the ``ok=False`` outcome, never a crash; CONSTRUCTING the builder is free
    and wheel-free (the ``enrich-greeks --dry-run`` enabler).

    Parameters
    ----------
    lake_root
        The lake root to discover under (also the driver's read/write root —
        the caller passes the SAME root to :func:`run_greeks_enrich_pass`).
    underlyings
        Optional underlying token filter (see :func:`discover_option_windows`).
    start
        Optional half-open clamp start.
    end
        Optional half-open clamp end (exclusive).

    Returns
    -------
    TargetBuilder
        A closure mapping each :class:`DiscoveredOptionWindow` to a
        ``GreeksEnrichTarget`` field-for-field.
    """

    def _build() -> Sequence[GreeksEnrichTarget]:
        from brutex.data.greeks_enrich import GreeksEnrichTarget

        return [
            GreeksEnrichTarget(
                exchange=window.exchange,
                symbol=window.symbol,
                start=window.start,
                end=window.end,
            )
            for window in discover_option_windows(
                lake_root, underlyings=underlyings, start=start, end=end
            )
        ]

    return _build


def resolve_cli_greeks_enrich(env: Mapping[str, str] | None = None) -> bool:
    """Resolve the ``BRUTEX_CLI_GREEKS_ENRICH`` fetch-path gate — fail-loud.

    unset / ``""`` / ``"1"`` / ``"on"`` / ``"true"`` (case-insensitive,
    whitespace-stripped) → ``True`` (default-ON: a CLI spot re-pull changes
    the spot input to Greeks, and the #1188 value-hardened reuse gate must see
    it — opt-in would leave stale ``spot_at_bar``-mismatched Greeks behind
    every CLI spot revision, the illusion class Golden Rule #1 forbids);
    ``"0"`` / ``"off"`` / ``"false"`` → ``False``. Anything else raises —
    junk is never silently coerced to a default (the DEC-RAM-BUDGETS-001
    fail-loud env convention), and the CLI resolves this BEFORE any vendor
    work so a junk value can never fail an already-completed pull.

    Parameters
    ----------
    env
        Injectable environment mapping (tests); ``None`` reads ``os.environ``.

    Returns
    -------
    bool
        ``True`` when the fetch-path enrichment stage is enabled.

    Raises
    ------
    ValueError
        If the value is set to anything outside the two token sets.
    """
    mapping = os.environ if env is None else env
    raw = mapping.get(CLI_GREEKS_ENRICH_ENV, "")
    token = raw.strip().lower()
    if token in _ENV_ON_TOKENS:
        return True
    if token in _ENV_OFF_TOKENS:
        return False
    raise ValueError(
        f"{CLI_GREEKS_ENRICH_ENV}={raw!r} is not a recognised switch value; "
        f"expected one of '1', 'on', 'true' (enable — the default when unset) "
        f"or '0', 'off', 'false' (disable)"
    )
