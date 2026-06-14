"""Selection-based HISTORICAL pull — the separate "Pull historical" engine.

DEC-DERIV-PULL-HISTORICAL-001 (PR-5, the capstone of the metadata/historical split,
ADR-0010). The HEAVY half of the metadata/historical split: the metadata-only "Pull
expiries + contracts" button (PR-3, DEC-DERIV-METADATA-ROUTE-001) caches every expiry
DATE + contract SYMBOL into the separate ``registry.duckdb`` (PR-1, #732) WITHOUT a
single candle. This module is the twin button — download the OHLC candles for the
operator-SELECTED registry contracts (a single contract / one expiry's chain / a whole
underlying / all cached), now that the chain metadata is already cached.

## Why a selection-based pull (not a re-enumeration)

The retired fused bulk chain pull (decommissioned by
DEC-DERIV-DECOMMISSION-FUSED-001) RE-ENUMERATED the whole chain (``get_expiries`` ×
``get_contracts``) AND downloaded every contract's candles on every click — the
runaway "pull everything" shape that wasted the network when the operator wanted
ONLY a handful of already-cached contracts. This module takes a concrete
:class:`~brutex.data.registry_store.ContractRow` selection, rebuilds each into a
:class:`~brutex.adapters.protocol.DerivativeContract` via
:func:`~brutex.adapters.groww.contracts.parse_contract_symbol`, and feeds that RAW
contracts list straight to
:func:`~brutex.adapters.groww.option_ingest.run_groww_option_ingest` — so it pulls the
SPECIFIC contracts WITHOUT re-enumerating the chain (the whole point; the metadata is
already in the registry).

## Path A — the verified candle lake (NOT the OptionBar lake)

The candles land on **PATH A**: the standard verified candle lake
(``bars/<exch>/FNO/<groww_symbol>/<interval>/`` via
:class:`~brutex.data.sink.ParquetBarSink`), the SAME lake the bulk derivatives pull
writes — the spine
:func:`~brutex.adapters.groww.option_ingest.run_groww_option_ingest` already uses. It
is **NOT** the ``options/`` OptionBar lake (PATH B): an OptionBar needs ``iv`` / ``spot``
/ ``ATM±N`` enrichment that ``get_historical_candles`` does NOT return — fabricating
those would violate Golden Rule #1. Historical OHLCV+OI is exactly what the candle lake
stores, so Path A is the honest home.

## candle_status — the per-contract historical marker

AFTER a contract's candles land clean (every ``(contract, interval)`` pair succeeded —
no quarantine, no transient residual) the contract's registry
:meth:`~brutex.data.registry_store.RegistryStore.set_candle_status` is set to
``complete`` — the per-contract marker the ``/db`` chain view reads (the metadata pull
leaves it ``none``). A contract that quarantined or left a transient window is left
``none`` (honest — never marked complete on a partial pull).

## Honest envelope (Golden Rule #1 + #10)

* **Honest empty selection** — an EMPTY contract list pulls 0 bars + an honest note
  (never an invented contract). The route resolves the selection to registry rows; an
  empty registry / no-match is the route's honest 400 / "pull metadata first" payload.
* **Path A, not Path B** — candle-only lake; no fabricated ``iv``/``spot``/``ATM``.
* **until-clean** (DEC-GROWW-DERIV-UNTIL-CLEAN-001 #730) — when ``until_clean=True`` the
  pull runs BOUNDED re-pull rounds over the SHARED WindowLedger until the transient
  residual drains, the token dies, or the budget / a no-progress round halts it
  (:func:`~brutex.adapters.ingest_common.run_until_clean`, ``stop_when_stuck=True``).
  Each round re-ingests the SAME fixed contract list; the ledger SKIPS already-`SUCCESS`
  windows and RE-ATTEMPTS the prior round's `FAILED` (transient) ones.
* **Network-bound, NOT O(1)-total** — the per-contract fetch tiles each contract's
  range (the heavy minute fill for a full underlying's chain is genuinely weeks-scale).
  The WindowLedger makes every window rerun-safe (idempotent). Unlike the chain pull, a
  round here is ``O(failed windows)`` (NO re-enumeration — the contract list is fixed),
  the same shape as the spot round.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Final

import structlog

from brutex.adapters.groww.client import GrowwClient
from brutex.adapters.groww.constants import (
    GROWW_FUTURE_COVERAGE_MARKET,
    GROWW_OPTION_COVERAGE_MARKET,
    OPTION_SEGMENT,
    TRACK1_DAILY_INTERVAL,
    TRACK1_SPOT_INTERVAL,
)
from brutex.adapters.groww.contract_bounds import (
    contract_daily_coverage,
    coverage_to_minute_window,
)
from brutex.adapters.groww.contracts import parse_contract_symbol
from brutex.adapters.groww.option_ingest import (
    ContractWindows,
    GrowwOptionIngestReport,
    ProgressHook,
    run_groww_option_ingest,
)
from brutex.adapters.ingest_common import (
    AUTO_RESUME_MAX_ROUNDS,
    UntilCleanRound,
    run_until_clean,
)
from brutex.adapters.protocol import DerivativeContract
from brutex.data.catalog import Catalog
from brutex.data.errors import CatalogError
from brutex.data.registry_store import (
    CANDLE_STATUS_COMPLETE,
    ContractRow,
    RegistryStore,
)
from brutex.data.sink import BarSink, NullBarSink
from brutex.data.window_ledger import WindowLedger
from brutex.data.window_ledger_store import (
    begin_pull_ledger,
    end_pull_ledger,
    make_live_ledger_flush,
)

if TYPE_CHECKING:
    from brutex.dashboard.pull_controller import SessionRateController

#: Phase-1 interval — the MANDATORY ``1day`` pull that runs FIRST for EVERY selected
#: contract, regardless of the operator's interval choice (even a ``1minute``-only
#: request). Its sole F&O job is to PIN each contract's precise listing/start date so
#: the heavy Phase-2 minute fill begins at the right date (DEC-HISTORICAL-1DAY-FIRST-001;
#: an F&O 1-day pull is NOT for bhavcopy matching — that is the spot check).
_PHASE1_DAILY: Final[str] = TRACK1_DAILY_INTERVAL

#: Phase-2 interval — the HEAVY ``1minute`` fill, attempted ONLY when the operator
#: requested it AND the global Phase-1 barrier passed (every selected contract's 1-day
#: pull settled clean — no quarantine, no still-failing window).
_PHASE2_MINUTE: Final[str] = TRACK1_SPOT_INTERVAL

#: Structured logger — the start-date discovery degrade paths (catalog unavailable /
#: per-contract skip) are logged LOUDLY (Golden Rule #10 observability), never silent.
_LOG = structlog.get_logger("brutex.dashboard.historical_pull")

#: FNO history floor — Groww FNO data is "available from 2020" (CLAUDE.md §6); the
#: historical pull mirrors the derivatives pull's floor so the candle range agrees.
GROWW_HISTORICAL_FLOOR: Final[date] = date(2020, 1, 1)

#: One calendar day — the inclusive→exclusive end shift + the previous-day default.
_ONE_DAY: Final[timedelta] = timedelta(days=1)

#: The ``FUT`` leg side → the futures ledger namespace; everything else (``CE`` / ``PE``)
#: is an option leg. Keeping option/future windows in DISTINCT ledger namespaces is the
#: SAME discipline the bulk pull uses (DEC-GROWW-FUTURE-INGEST-001) so the two never
#: collide on the same expiry.
_FUTURE_SIDE: Final[str] = "FUT"

#: Factory: ``(lake_root) -> (client, sink)`` — injected in tests; the default picks the
#: honest dry-run pair or the live ``(GrowwClient, ParquetBarSink)``. Re-uses the SAME
#: shape the bulk derivatives pull declares (CLAUDE.md §11 COMMON).
HistoricalClientFactory = Callable[[Path], tuple[GrowwClient, BarSink]]

#: A rebuilt selection entry: the authoritative registry row + its parsed contract.
_RebuiltContract = tuple[ContractRow, DerivativeContract]


@dataclass(frozen=True, slots=True)
class HistoricalPullReport:
    """The honest outcome of one selection-based historical candle pull (display + audit).

    Two-phase shape (DEC-HISTORICAL-1DAY-FIRST-001): the F&O historical pull ALWAYS runs
    ``1day`` FIRST (Phase 1) for every selected contract, and only proceeds to the heavy
    ``1minute`` fill (Phase 2) when the WHOLE Phase-1 pull cleaned. The phase fields make
    that barrier auditable:

    * :attr:`phase1_clean` — did the ENTIRE 1-day phase settle clean (the global barrier)?
    * :attr:`phase1_complete` — the contract symbols whose 1-day pull landed clean.
    * :attr:`start_dates` — each contract's discovered earliest 1-day bar date (the date
      the Phase-2 minute fill STARTS from; a contract with no 1-day data is ABSENT).
    * :attr:`minute_requested` — did the operator request ``1minute`` at all?
    * :attr:`minute_skipped` — was Phase 2 SKIPPED because the barrier did not pass?

    The aggregate :attr:`bars_appended` / :attr:`windows` / :attr:`failed_pairs` /
    :attr:`still_failed_windows` / :attr:`auth_aborted` SUM/fold across BOTH phases (so the
    top-line counts describe the whole pull); :attr:`contracts_completed` is the count
    marked ``candle_status=complete`` (a contract is complete iff its 1-day phase cleaned —
    Phase 1 is the gate the marker reads).
    """

    dry_run: bool
    intervals: tuple[str, ...]
    contracts_selected: int
    contracts_completed: int
    bars_appended: int
    windows: int
    failed_pairs: int
    still_failed_windows: int
    auth_aborted: bool
    note: str
    phase1_clean: bool
    phase1_complete: tuple[str, ...]
    start_dates: dict[str, str]
    minute_requested: bool
    minute_skipped: bool

    def to_dict(self) -> dict[str, object]:
        """JSON-safe view for the ``pull-historical`` route + the live panel."""
        return {
            "dry_run": self.dry_run,
            "intervals": list(self.intervals),
            "contracts_selected": self.contracts_selected,
            "contracts_completed": self.contracts_completed,
            "bars_appended": self.bars_appended,
            "windows": self.windows,
            "failed_pairs": self.failed_pairs,
            "still_failed_windows": self.still_failed_windows,
            "auth_aborted": self.auth_aborted,
            "phase1_clean": self.phase1_clean,
            "phase1_complete": list(self.phase1_complete),
            "start_dates": dict(self.start_dates),
            "minute_requested": self.minute_requested,
            "minute_skipped": self.minute_skipped,
            "note": self.note,
        }


class _EmptyCandleSDK:
    """An honest dry-run SDK: no creds ⇒ no real candles, so the candle fetch is EMPTY.

    Refuses to fabricate OHLC for a REAL listed contract (Golden Rule #1 — unlike a
    synthetic spot preview, an expired option's candles are real vendor data). Carries
    the three methods the ingest spine may touch so the path runs offline; the contract
    list is the operator's real registry selection, so the dry-run still reports the
    honest selected count (it just appends 0 bars).
    """

    def get_expiries(self, **_kwargs: object) -> dict[str, object]:
        """No expiries without live creds (never reached — the contracts are pre-resolved)."""
        return {"expiries": []}

    def get_contracts(self, **_kwargs: object) -> dict[str, object]:
        """No contracts without live creds (never reached — the contracts are pre-resolved)."""
        return {"contracts": []}

    def get_historical_candles(self, **_kwargs: object) -> dict[str, object]:
        """No candles without live creds (the honest dry-run — 0 bars, never fabricated)."""
        return {"candles": []}


def _build_dry_run_client(_lake_root: Path) -> tuple[GrowwClient, BarSink]:
    """Honest dry-run pair: an empty-candle client + a :class:`NullBarSink`."""
    return GrowwClient(sdk=_EmptyCandleSDK()), NullBarSink()


def _build_live_client(  # pragma: no cover - live Groww TOTP token + SDK + network
    lake_root: Path,
    *,
    pace: Callable[[], None] | None = None,
    on_rate_limit: Callable[[], None] | None = None,
    on_success: Callable[[], None] | None = None,
) -> tuple[GrowwClient, BarSink]:
    """Build the LIVE ``(GrowwClient, ParquetBarSink)`` from stored TOTP credentials.

    LIVE ONLY (network + TOTP); tests inject a fake ``client_factory``. The same creds
    chain the spot/metadata pulls use — the Path-A candle lake sink + the rate hooks at
    the one choke point so EVERY candle call is paced + retried + decrement/recover
    (DEC-GROWW-RATELIMIT-RETRY-001).
    """
    from brutex.adapters.groww.auth import GrowwCredentials
    from brutex.adapters.groww.live import build_live_client
    from brutex.config.credentials import resolve_groww_file_credentials
    from brutex.data.parquet_writer import ParquetWriter
    from brutex.data.sink import ParquetBarSink

    file_creds = resolve_groww_file_credentials()
    creds = GrowwCredentials(api_key=file_creds.api_key, totp_secret=file_creds.totp_secret)
    client = build_live_client(creds, pace=pace, on_rate_limit=on_rate_limit, on_success=on_success)
    sink = ParquetBarSink(writer=ParquetWriter(lake_root=lake_root))
    return client, sink


def _resolve_client_factory(
    *,
    dry_run: bool,
    pace: Callable[[], None] | None,
    on_rate_limit: Callable[[], None] | None,
    on_success: Callable[[], None] | None,
) -> HistoricalClientFactory:
    """Pick the default ``(lake_root) -> (client, sink)`` factory, wiring the live limiter.

    Dry-run → the honest empty-candle pair. Live → a factory that injects the shared rate
    controller's pace / decrement-on-429 / recover-on-success hooks into the
    :class:`GrowwClient`, so the candle pull can NEVER bypass the limiter
    (DEC-GROWW-RATELIMIT-RETRY-001).
    """
    if dry_run:
        return _build_dry_run_client

    def _live(lake_root: Path) -> tuple[GrowwClient, BarSink]:  # pragma: no cover - live network
        return _build_live_client(
            lake_root, pace=pace, on_rate_limit=on_rate_limit, on_success=on_success
        )

    return _live


def _rate_hooks(
    session_rate: SessionRateController | None, *, dry_run: bool
) -> tuple[
    Callable[[], None] | None,
    Callable[[], None] | None,
    Callable[[], None] | None,
    Callable[[], None] | None,
]:
    """Resolve ``(pace, on_rate_limit, on_timeout, on_success)`` from the session rate.

    Dry-run or no session controller → all ``None`` (no pacing offline). Live with a
    :class:`~brutex.dashboard.pull_controller.SessionRateController` → its single
    persistent controller's hooks, so the 500/min governor spans the whole pull (the
    candle calls ride the SAME bucket as the spot/derivatives pulls).
    """
    if dry_run or session_rate is None:
        return None, None, None, None
    controller = session_rate.get()
    return (
        controller.pace,
        controller.on_rate_limit,
        controller.on_timeout,
        controller.on_success,
    )


def _contract_market(row: ContractRow) -> str:
    """The ledger market namespace for a contract row's side (option vs future).

    A ``FUT`` leg rides :data:`GROWW_FUTURE_COVERAGE_MARKET`; a ``CE`` / ``PE`` leg rides
    :data:`GROWW_OPTION_COVERAGE_MARKET` — the SAME split the bulk pull uses so a
    contract's historical windows never collide across leg type (DEC-GROWW-FUTURE-INGEST-001).
    """
    return (
        GROWW_FUTURE_COVERAGE_MARKET if row.side == _FUTURE_SIDE else GROWW_OPTION_COVERAGE_MARKET
    )


def _resolve_range(
    *, from_date: date | None, to_date: date | None, now: datetime
) -> tuple[date, date]:
    """Resolve the half-open ``[start, end)`` candle window (clamped to ``[floor, today]``).

    ``from_date`` defaults to :data:`GROWW_HISTORICAL_FLOOR` (2020-01-01); ``to_date``
    defaults to the **previous day** (a routine pull never grabs the in-progress
    session). Both bounds are day-INCLUSIVE, so ``end`` is the midnight that STARTS the
    day after ``to_date`` (the ingest treats ``end`` as exclusive). An empty range raises
    ``ValueError``.
    """
    today = now.astimezone(UTC).date()
    floor = GROWW_HISTORICAL_FLOOR
    start = max(from_date or floor, floor)
    last = min(to_date or (today - _ONE_DAY), today)
    if last < start:
        raise ValueError(
            f"nothing to pull: resolved range [{start.isoformat()}, {last.isoformat()}] is empty"
        )
    return start, last + _ONE_DAY


def _rebuild_contracts(contracts: Sequence[ContractRow]) -> list[_RebuiltContract]:
    """Rebuild each :class:`ContractRow` into a :class:`DerivativeContract` (parse-or-raise).

    Uses :func:`~brutex.adapters.groww.contracts.parse_contract_symbol` with the row's
    AUTHORITATIVE ``underlying`` + ``expiry_date`` (never re-parsed from the lossy
    ``DDMmmYY`` token), so the rebuilt contract is bit-faithful to what the metadata pull
    persisted. A malformed registry symbol fails loud (Golden Rule #1 — never a silent
    skip). Returns ``(row, contract)`` pairs so the candle_status marker keys on the
    row's PK after its candles land.
    """
    return [
        (
            row,
            parse_contract_symbol(
                row.contract_symbol, underlying_symbol=row.underlying, expiry=row.expiry_date
            ),
        )
        for row in contracts
    ]


def _clean_symbols(report: GrowwOptionIngestReport) -> frozenset[str]:
    """The contract symbols this pass landed CLEAN (every pair SUCCESS, no still-failed).

    A symbol is clean iff it has at least one recorded pair AND no quarantined failure AND
    none of its pairs left a transient ``still_failed_windows`` residual — only then is its
    historical coverage truly complete and the ``candle_status=complete`` marker honest.
    """
    quarantined = {f.symbol for f in report.failures}
    still_failing = {
        symbol for (symbol, _interval), pair in report.pairs.items() if pair.still_failed_windows
    }
    landed = {symbol for (symbol, _interval) in report.pairs}
    return frozenset(landed - quarantined - still_failing)


@dataclass(frozen=True, slots=True)
class _PassResult:
    """One full ingest pass's aggregate counts + the contracts that landed CLEAN.

    ``bars`` / ``windows`` SUM across rounds (round N re-appends only newly-fetched
    windows — the keep-first sink + WindowLedger skip make double-counting impossible);
    ``failed_pairs`` / ``still_failed_windows`` / ``auth_aborted`` describe THIS pass's
    residual (the loop reports the LAST round's). ``clean_symbols`` is the set of contract
    symbols whose every ``(contract, interval)`` pair this pass succeeded with NO transient
    residual + NO quarantine — the contracts safe to mark ``complete``.
    """

    bars: int
    windows: int
    failed_pairs: int
    still_failed_windows: int
    auth_aborted: bool
    clean_symbols: frozenset[str]


@dataclass(frozen=True, slots=True)
class _PhaseResult:
    """One PHASE's folded outcome — its until-clean passes summed + the clean-landed set.

    A phase is ONE interval's full pull (Phase 1 = ``1day``, Phase 2 = ``1minute``) over
    the SAME fixed market buckets, run as the single-pass or until-clean loop. ``bars`` /
    ``windows`` SUM across the phase's rounds; ``failed_pairs`` / ``still_failed_windows`` /
    ``auth_aborted`` reflect the LAST round (the residual the operator must re-pull);
    ``complete`` is the union of every round's clean symbols (once-clean stays clean — the
    keep-first ledger). ``clean`` is the BARRIER flag: True iff EVERY selected contract
    landed clean this phase (no quarantine, no still-failing, no auth death).
    """

    bars: int
    windows: int
    failed_pairs: int
    still_failed_windows: int
    auth_aborted: bool
    complete: frozenset[str]
    clean: bool


def _build_note(
    *,
    dry_run: bool,
    contracts_selected: int,
    contracts_completed: int,
    windows: int,
    failed_pairs: int,
    still_failed_windows: int,
    auth_aborted: bool,
    minute_skipped: bool = False,
) -> str:
    """Honest one-line outcome note for the report / live panel.

    The ``minute_skipped`` arm (DEC-HISTORICAL-1DAY-FIRST-001) takes priority over the
    generic quarantine/transient notes: when the global 1-day barrier did NOT clear, the
    operator's headline is "1-day incomplete → 1-minute skipped", not the raw residual.
    """
    if contracts_selected == 0:
        return (
            "no contracts selected — pull expiries + contracts first (the registry is "
            "empty for this selection), then pick a contract / chain / underlying to pull"
        )
    if dry_run:
        return (
            "dry-run: set Groww credentials to download the selected contracts' OHLC "
            "(an expired contract's candles are real vendor data, never fabricated offline)"
        )
    if auth_aborted:
        return "auth token expired mid-pull — re-set Groww credentials and re-pull (idempotent)"
    if minute_skipped:
        return (
            f"1-day phase INCOMPLETE — {contracts_completed}/{contracts_selected} contract(s) "
            "cleaned 1-day; 1-minute SKIPPED (global barrier). Re-pull to drain the 1-day "
            "residual first (1-minute needs each contract's discovered start date)"
        )
    if failed_pairs:
        return (
            f"pulled {windows} windows; {failed_pairs} contract/interval pair(s) quarantined — "
            "re-pull to retry the deferred windows (WindowLedger skips the done ones)"
        )
    if still_failed_windows:
        return (
            f"pulled {windows} windows; {still_failed_windows} transient window(s) still "
            "failing after the re-pull rounds — re-pull to drain them (WindowLedger skips the done)"
        )
    return (
        f"pulled {windows} windows across {contracts_completed}/{contracts_selected} contract(s) "
        "into the candle lake (rerun-safe — WindowLedger dedups)"
    )


def _mark_complete(
    registry: RegistryStore,
    rebuilt: Sequence[_RebuiltContract],
    completed_symbols: frozenset[str],
) -> int:
    """Mark each clean-landed contract ``candle_status=complete``; return the count updated.

    Keys the registry update on the row's authoritative ``(exch, underlying, contract_symbol)``
    PK (never a re-parse). A symbol absent from *completed_symbols* (it quarantined or left a
    transient residual) is LEFT ``none`` — honest, never marked complete on a partial pull.
    On a dry-run the empty-candle client appends 0 bars but the pair still records SUCCESS
    (an EMPTY window is a settled, clean window), so the selected contracts ARE marked
    complete — the registry status reflects coverage of the SELECTED range, which the
    dry-run honestly settled as empty.
    """
    marked = 0
    for row, _contract in rebuilt:
        if row.contract_symbol in completed_symbols:
            registry.set_candle_status(
                row.exch, row.underlying, row.contract_symbol, CANDLE_STATUS_COMPLETE
            )
            marked += 1
    return marked


def _run_phase(
    *,
    interval: str,
    by_market: dict[str, list[_RebuiltContract]],
    selected_symbols: frozenset[str],
    client: GrowwClient,
    sink: BarSink,
    lake_root: Path,
    dry_run: bool,
    start: date,
    end: date,
    pace: Callable[[], None] | None,
    on_rate_limit: Callable[[], None] | None,
    on_timeout: Callable[[], None] | None,
    on_success: Callable[[], None] | None,
    on_progress: ProgressHook | None,
    session_rate: SessionRateController | None,
    until_clean: bool,
    until_clean_rounds: int,
    contract_windows: ContractWindows | None,
) -> _PhaseResult:
    """Run ONE interval's full pull over *by_market* and fold its until-clean passes.

    The single ingest call site (CLAUDE.md §11 COMMON) — BOTH phases funnel through here,
    differing only by *interval* and the optional per-contract *contract_windows* bounds.
    Opens its OWN pull-ledger (each phase's windows are namespaced by interval already), runs
    the one-shot path OR the bounded until-clean loop, and returns the folded
    :class:`_PhaseResult`. The barrier flag :attr:`_PhaseResult.clean` is True iff EVERY
    selected contract landed clean with no quarantine / still-failing / auth death.
    """
    ledger: WindowLedger | None = begin_pull_ledger(lake_root, dry_run=dry_run)
    flush = make_live_ledger_flush(ledger, lake_root) if ledger is not None else None

    def _progress(label: str, bars: int, failed: bool) -> None:
        """Forward to the caller's hook + flush the ledger (either side optional)."""
        if on_progress is not None:
            on_progress(label, bars, failed)
        if flush is not None:
            flush()

    passes: list[_PassResult] = []

    def _one_pass() -> _PassResult:
        """Run ONE full ingest pass over EVERY market bucket at *interval*; collect clean.

        Re-attempts the SHARED ledger's prior-round FAILED windows (the ledger skips the
        done ones). Stops early on a dead auth token. The per-contract *contract_windows*
        bounds the heavy sub-daily fetch to each contract's discovered listing→expiry span
        (never the ``1day`` interval — the spine ignores the map for the daily coverage).
        """
        pass_bars = 0
        pass_windows = 0
        pass_failed_pairs = 0
        pass_still_failed = 0
        pass_auth = False
        clean: set[str] = set()
        for market, bucket in by_market.items():
            report: GrowwOptionIngestReport = run_groww_option_ingest(
                client,
                sink,
                contracts=[contract for _row, contract in bucket],
                intervals=(interval,),
                start=start,
                end=end,
                pace=pace,
                ledger=ledger,
                on_rate_limit=on_rate_limit,
                on_timeout=on_timeout,
                on_success=on_success,
                on_progress=_progress,
                market=market,
                contract_windows=contract_windows,
            )
            pass_bars += report.bars_appended
            pass_windows += report.windows
            pass_failed_pairs += report.failed_pairs
            pass_still_failed += report.still_failed_windows
            pass_auth = pass_auth or report.auth_aborted
            clean |= _clean_symbols(report)
            if pass_auth:
                break
        result = _PassResult(
            bars=pass_bars,
            windows=pass_windows,
            failed_pairs=pass_failed_pairs,
            still_failed_windows=pass_still_failed,
            auth_aborted=pass_auth,
            clean_symbols=frozenset(clean),
        )
        passes.append(result)
        return result

    try:
        if until_clean:

            def _round_runner() -> UntilCleanRound:
                res = _one_pass()
                # Groww derives auth_aborted from quarantined failures and carries no
                # auth_cause string (the documented divergence — mirrors the spot/chain
                # paths), so auth_cause stays at its None default.
                return UntilCleanRound(
                    still_failed_windows=res.still_failed_windows,
                    auth_aborted=res.auth_aborted,
                )

            # Groww backtesting has NO daily quota (DEC-GROWW-NO-DAILY-QUOTA-001), so this
            # brake is dormant; wired for parity with the spot/chain paths. Offline /
            # dry-run pulls have no controller → no hook (never the limit).
            quota_hook = (
                session_rate.get().quota_exhausted
                if (session_rate is not None and not dry_run)
                else None
            )
            run_until_clean(
                _round_runner,
                max_rounds=until_clean_rounds,
                quota_exhausted=quota_hook,
                stop_when_stuck=True,
            )
        else:
            _one_pass()
    finally:
        end_pull_ledger(ledger, lake_root, dry_run=dry_run)

    # bars/windows SUM across rounds (round N re-appends only newly-fetched windows — the
    # keep-first sink + ledger skip make double-counting impossible); the residual reflects
    # the LAST round. ``until_clean_rounds <= 0`` runs zero rounds → empty passes → all-zero.
    complete: frozenset[str] = (
        frozenset().union(*(p.clean_symbols for p in passes)) if passes else frozenset()
    )
    # The GLOBAL barrier for this phase: EVERY selected contract landed clean (so the union
    # of clean symbols covers the whole selection) — an empty selection clears vacuously.
    barrier_clean = selected_symbols <= complete
    return _PhaseResult(
        bars=sum(p.bars for p in passes),
        windows=sum(p.windows for p in passes),
        failed_pairs=passes[-1].failed_pairs if passes else 0,
        still_failed_windows=passes[-1].still_failed_windows if passes else 0,
        auth_aborted=passes[-1].auth_aborted if passes else False,
        complete=complete,
        clean=barrier_clean,
    )


def _discover_start_dates(
    *,
    rebuilt: Sequence[_RebuiltContract],
    lake_root: Path,
    start: date,
    end: date,
) -> dict[str, tuple[date, date]]:
    """Read each contract's earliest 1-day bar date from the JUST-WRITTEN lake (Phase 1→2).

    The crux of DEC-HISTORICAL-1DAY-FIRST-001: after the 1-day phase landed clean, the lake
    holds each contract's REAL 1-day coverage, so its earliest 1-day bar date PINS where the
    heavy 1-minute fill must begin (never blindly from 2020). Reconciles the catalog ONCE
    over the FNO/``1day`` space (the blessed reconcile-once → read-many pattern), then reads
    each contract's :func:`~brutex.adapters.groww.contract_bounds.contract_daily_coverage`
    and converts it to a minute window via
    :func:`~brutex.adapters.groww.contract_bounds.coverage_to_minute_window` — whose ``start``
    IS the discovered listing date and whose ``end`` clamps to the contract's expiry, bounded
    WITHIN the operator's resolved ``[start, end)`` range.

    Unlike the optimisation-only
    :func:`~brutex.adapters.groww.contract_bounds.derive_contract_minute_windows` (which also
    OMITS a contract whose window collapses to the full range), this keeps EVERY contract
    that has ANY 1-day coverage — because here the map is the AUTHORITATIVE Phase-2 contract
    set, not a best-effort bound: a contract with real data but a full-range window must
    still be pulled, just from its discovered start. A contract with ZERO 1-day bars (it never
    traded in range) is OMITTED — the caller EXCLUDES it from Phase 2 (its 1-minute would be
    empty too), honestly reported. The per-contract date is read from the contract's own 1-day
    coverage, never fabricated. A missing/locked catalog or a malformed contract degrades that
    contract (or the whole pass) to ABSENT — every excluded contract is reported; the honest
    conservative choice for a barrier-passed but coverage-unreadable lake (Golden Rule #10).

    Returns
    -------
    dict[str, tuple[date, date]]
        ``{contract.symbol: (discovered_start, expiry_end)}`` for every contract whose 1-day
        coverage the lake holds; symbols with no coverage are absent.
    """
    try:
        catalog = Catalog.open(lake_root / "catalog.duckdb")
    except CatalogError:
        # The data-lake deps are missing / the catalog file is unopenable — degrade to an
        # EMPTY map (Phase 2 excludes every contract), loud, never silent (Golden Rule #10).
        _LOG.warning("historical.start_discovery.catalog_unavailable", lake_root=str(lake_root))
        return {}
    windows: dict[str, tuple[date, date]] = {}
    try:
        from brutex.data.catalog_reconcile import reconcile_catalog_from_lake

        reconcile_catalog_from_lake(
            lake_root=lake_root, catalog=catalog, segment=OPTION_SEGMENT, interval=_PHASE1_DAILY
        )
        for _row, contract in rebuilt:
            try:
                coverage = contract_daily_coverage(catalog, contract=contract)
            except (CatalogError, ValueError) as exc:
                # A malformed contract symbol / mid-loop catalog error excludes THIS contract
                # from Phase 2 only (loud, per-contract) — never blanks the whole map.
                _LOG.warning(
                    "historical.start_discovery.contract_skipped",
                    symbol=contract.symbol,
                    error=str(exc),
                )
                continue
            if coverage is None:
                # No 1-day data for this contract — it never traded in range, so its
                # 1-minute would be empty too: EXCLUDE it from Phase 2 (honestly reported).
                continue
            windows[contract.symbol] = coverage_to_minute_window(
                coverage, full_start=start, full_end=end, expiry=contract.expiry
            )
    except CatalogError:
        # The reconcile itself failed (busy lake / missing deps) — degrade to EMPTY, loud.
        _LOG.warning("historical.start_discovery.reconcile_unavailable", lake_root=str(lake_root))
        return {}
    finally:
        catalog.close()
    return windows


def pull_contract_candles(
    contracts: Sequence[ContractRow],
    *,
    lake_root: Path,
    dry_run: bool,
    intervals: Sequence[str],
    registry: RegistryStore,
    from_date: date | None = None,
    to_date: date | None = None,
    now: datetime | None = None,
    client_factory: HistoricalClientFactory | None = None,
    on_progress: ProgressHook | None = None,
    session_rate: SessionRateController | None = None,
    until_clean: bool = False,
    until_clean_rounds: int = AUTO_RESUME_MAX_ROUNDS,
) -> HistoricalPullReport:
    """Download OHLC candles for the SELECTED registry *contracts* — Path A, no re-enumeration.

    Rebuilds each :class:`~brutex.data.registry_store.ContractRow` into a
    :class:`~brutex.adapters.protocol.DerivativeContract` (via
    :func:`~brutex.adapters.groww.contracts.parse_contract_symbol`) and feeds that RAW
    contracts list to :func:`~brutex.adapters.groww.option_ingest.run_groww_option_ingest`
    — so it pulls the SPECIFIC contracts WITHOUT re-enumerating the chain (the metadata is
    already cached in the registry). The candles land on the verified candle lake
    (``bars/<exch>/FNO/<symbol>/<interval>/`` via :class:`~brutex.data.sink.ParquetBarSink`
    — PATH A), NOT the ``options/`` OptionBar lake (PATH B needs ``iv``/``spot``/``ATM±N``
    that ``get_historical_candles`` does not return — fabricating them would violate Golden
    Rule #1). AFTER a contract's 1-day phase lands CLEAN its registry
    :meth:`~brutex.data.registry_store.RegistryStore.set_candle_status` is set to
    ``complete``.

    **1-day-first GLOBAL barrier (DEC-HISTORICAL-1DAY-FIRST-001).** For F&O the pull is
    TWO phases regardless of how many contracts are selected:

    1. **Phase 1 — 1-day ONLY first, ALWAYS.** Every selected contract's ``1day`` interval
       is pulled FIRST (the existing until-clean loop on 1-day alone) — EVEN IF the operator
       requested only ``1minute`` (1-day is mandatory). For F&O the 1-day OHLCV is NOT a
       bhavcopy cross-check (that is the spot path) — its sole job is to PIN each contract's
       precise listing/start date for the minute fill.
    2. **GLOBAL barrier.** Phase 2 starts ONLY when the ENTIRE 1-day phase cleaned — every
       selected contract, every window settled (no quarantine / no still-failing / no auth
       death). If 1-day did NOT fully clean, the pull STOPS: 1-minute is NOT started and the
       report says 1-day incomplete (which contracts failed), 1-minute skipped.
    3. **Phase 2 — 1-minute (only if requested AND the barrier passed).** Each contract's
       minute fill STARTS from the precise earliest 1-day bar date its own Phase-1 pull
       revealed (read from the just-written 1-day lake via
       :func:`~brutex.adapters.groww.contract_bounds.contract_daily_coverage`), never blindly
       from 2020. A contract with ZERO 1-day bars is EXCLUDED from Phase 2 (its 1-minute
       would be empty too), honestly reported in ``start_dates``'s absence.

    With ``until_clean=True`` (DEC-GROWW-DERIV-UNTIL-CLEAN-001) EACH phase runs BOUNDED
    re-pull rounds over its SHARED WindowLedger until the transient residual drains, the
    token dies, or the budget / a no-progress round stops it
    (:func:`~brutex.adapters.ingest_common.run_until_clean`, ``stop_when_stuck=True``).
    Each round re-ingests the SAME fixed contract list; the ledger SKIPS already-`SUCCESS`
    windows and RE-ATTEMPTS the prior round's `FAILED` (transient) ones. Default
    ``until_clean=False`` is one single pass per phase.

    Honest envelope (Golden Rule #10): an EMPTY *contracts* selection appends 0 bars + an
    honest note (never an invented contract). The per-contract fetch tiles each range
    (network-bound, NOT O(1)-total) — the phasing OPTIMISES the 1-minute window set (it
    starts at each contract's real date), it does NOT make the pull O(1)-total. Unlike the
    chain pull, a round here is ``O(failed windows)`` — the contract list is fixed (no
    re-enumeration). Re-runs are rate-safe (the SAME 500/min bucket) + idempotent (the
    keep-first sink + WindowLedger).

    Parameters
    ----------
    contracts
        The selected registry rows whose candles to pull (single / chain / underlying /
        all); EMPTY ⇒ an honest 0-bar no-op.
    lake_root
        The lake root the Path-A candle sink writes under.
    dry_run
        When ``True``, the default factory uses the honest empty-candle client (0 bars,
        never a fabricated OHLC) + a :class:`NullBarSink`.
    intervals
        The operator's requested intervals (``1day`` / ``1minute`` / both). ``1day`` is
        ALWAYS pulled first (Phase 1); ``1minute`` triggers Phase 2 only after the barrier.
    registry
        The registry whose ``candle_status`` is marked ``complete`` per landed contract.
    from_date, to_date
        Optional inclusive date bounds; default ``[2020-01-01, previous day]``.
    now
        Injected clock (defaults to ``datetime.now(UTC)``).
    client_factory
        Injected ``(lake_root) -> (client, sink)`` (tests pass a fake); ``None`` selects
        the dry-run / live default.
    on_progress
        Per ``(contract, interval)`` progress hook ``(label, bars, failed)``.
    session_rate
        The dashboard's ``SessionRateController`` (live pacing); ``None`` offline.
    until_clean
        When ``True``, run bounded re-pull rounds until the transient residual drains
        (default ``False`` — one single pass).
    until_clean_rounds
        The OUTER round budget for ``until_clean`` (default
        :data:`~brutex.adapters.ingest_common.AUTO_RESUME_MAX_ROUNDS`); ``<= 0`` runs
        zero rounds (a safe no-op).
    """
    now = now or datetime.now(UTC)
    start, end = _resolve_range(from_date=from_date, to_date=to_date, now=now)
    pace, on_rate_limit, on_timeout, on_success = _rate_hooks(session_rate, dry_run=dry_run)
    if client_factory is None:
        client_factory = _resolve_client_factory(
            dry_run=dry_run, pace=pace, on_rate_limit=on_rate_limit, on_success=on_success
        )
    client, sink = client_factory(lake_root)
    rebuilt = _rebuild_contracts(contracts)
    selected_symbols = frozenset(row.contract_symbol for row, _contract in rebuilt)
    # Option + future legs ride DISTINCT ledger namespaces (DEC-GROWW-FUTURE-INGEST-001),
    # so split the selection by market and ingest each bucket under its own market.
    by_market: dict[str, list[_RebuiltContract]] = {}
    for row, contract in rebuilt:
        by_market.setdefault(_contract_market(row), []).append((row, contract))

    # ``1minute`` was requested iff it appears anywhere in the operator's interval list;
    # Phase 1 ALWAYS pulls ``1day`` first regardless (DEC-HISTORICAL-1DAY-FIRST-001), so the
    # raw *intervals* only decide whether Phase 2 runs (de-duped — 1day is never pulled twice).
    minute_requested = _PHASE2_MINUTE in intervals

    def _phase(interval: str, contract_windows: ContractWindows | None) -> _PhaseResult:
        """Run ONE phase (one interval) over the shared market buckets — the single call site."""
        return _run_phase(
            interval=interval,
            by_market=by_market,
            selected_symbols=selected_symbols,
            client=client,
            sink=sink,
            lake_root=lake_root,
            dry_run=dry_run,
            start=start,
            end=end,
            pace=pace,
            on_rate_limit=on_rate_limit,
            on_timeout=on_timeout,
            on_success=on_success,
            on_progress=on_progress,
            session_rate=session_rate,
            until_clean=until_clean,
            until_clean_rounds=until_clean_rounds,
            contract_windows=contract_windows,
        )

    # ── Phase 1 — 1-day ONLY first, ALWAYS (even for a 1-minute-only request) ──────────
    phase1 = _phase(_PHASE1_DAILY, None)

    # The GLOBAL barrier: Phase 2 (1-minute) runs ONLY when the WHOLE 1-day phase cleaned —
    # every selected contract, every window settled (no quarantine, no still-failing, no auth
    # death). If 1-day did NOT fully clean, STOP: do not start 1-minute (DEC-HISTORICAL-1DAY-
    # FIRST-001). The 1-day completion set IS the candle_status marker (Phase 1 is the gate).
    start_dates: dict[str, tuple[date, date]] = {}
    phase2: _PhaseResult | None = None
    minute_skipped = False
    if minute_requested:
        if phase1.clean:
            # Pin each contract's REAL start date from the just-written 1-day lake; a contract
            # with ZERO 1-day bars is ABSENT → EXCLUDED from Phase 2 (its 1-minute is empty too).
            start_dates = _discover_start_dates(
                rebuilt=rebuilt, lake_root=lake_root, start=start, end=end
            )
            phase2_buckets = {
                market: [(row, c) for row, c in bucket if c.symbol in start_dates]
                for market, bucket in by_market.items()
            }
            phase2 = _run_phase(
                interval=_PHASE2_MINUTE,
                by_market={m: b for m, b in phase2_buckets.items() if b},
                selected_symbols=frozenset(start_dates),
                client=client,
                sink=sink,
                lake_root=lake_root,
                dry_run=dry_run,
                start=start,
                end=end,
                pace=pace,
                on_rate_limit=on_rate_limit,
                on_timeout=on_timeout,
                on_success=on_success,
                on_progress=on_progress,
                session_rate=session_rate,
                until_clean=until_clean,
                until_clean_rounds=until_clean_rounds,
                contract_windows=start_dates,
            )
        else:
            # Barrier FAILED — 1-minute is SKIPPED entirely, reported (which contracts failed).
            minute_skipped = True

    # Aggregate BOTH phases for the top-line counts. ``candle_status=complete`` keys on the
    # Phase-1 completion set (a contract is "historically complete" once its 1-day pins land —
    # Phase 2 is the optional heavy minute fill on top, not the completion gate).
    contracts_completed = _mark_complete(registry, rebuilt, phase1.complete)
    bars_appended = phase1.bars + (phase2.bars if phase2 is not None else 0)
    windows = phase1.windows + (phase2.windows if phase2 is not None else 0)
    # The residual (the operator's "re-pull this" signal) is the LAST phase actually run:
    # Phase 2 when it ran, else Phase 1 (the barrier-failed / 1-day-only case).
    last = phase2 if phase2 is not None else phase1
    failed_pairs = last.failed_pairs
    still_failed_windows = last.still_failed_windows
    auth_aborted = phase1.auth_aborted or (phase2.auth_aborted if phase2 is not None else False)

    note = _build_note(
        dry_run=dry_run,
        contracts_selected=len(rebuilt),
        contracts_completed=contracts_completed,
        windows=windows,
        failed_pairs=failed_pairs,
        still_failed_windows=still_failed_windows,
        auth_aborted=auth_aborted,
        minute_skipped=minute_skipped,
    )
    return HistoricalPullReport(
        dry_run=dry_run,
        intervals=tuple(intervals),
        contracts_selected=len(rebuilt),
        contracts_completed=contracts_completed,
        bars_appended=bars_appended,
        windows=windows,
        failed_pairs=failed_pairs,
        still_failed_windows=still_failed_windows,
        auth_aborted=auth_aborted,
        note=note,
        phase1_clean=phase1.clean,
        phase1_complete=tuple(sorted(phase1.complete)),
        start_dates={sym: win[0].isoformat() for sym, win in sorted(start_dates.items())},
        minute_requested=minute_requested,
        minute_skipped=minute_skipped,
    )


__all__ = [
    "GROWW_HISTORICAL_FLOOR",
    "HistoricalClientFactory",
    "HistoricalPullReport",
    "pull_contract_candles",
]
