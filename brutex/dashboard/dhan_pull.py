"""Dhan SPOT pull orchestration — the dashboard-route + CLI pull core (feeds/dhan/).

DEC-DHAN-PULL-SURFACE-001 (ADR-0022) + DEC-DHAN-NATIVE-DAILY-001 (implementing
`DEC-SCOPE-MINIMAL-CORE-001`). The SURFACE glue that drives the standalone
Dhan SPOT feed end-to-end into its own ``feeds/dhan/`` lake namespace:

1. **Native-daily ingest (FIRST)** — :func:`run_dhan_spot_daily_pull` pulls the
   index series' 1-DAY bars NATIVELY from the vendor's daily endpoint
   (:func:`~brutex.adapters.dhan.daily_ingest.run_dhan_spot_daily`) into the
   ``1day`` partitions. A FAILED daily stage (``failed_windows > 0`` or a raised
   halt) STOPS the chain BEFORE any 1-minute vendor call (the gate lives in
   :func:`default_dhan_spot_pull`, surfaced as an honest stopped-note).
2. **Spot ingest** — :func:`run_dhan_spot_pull` builds the Dhan feed sink through the
   ONE sanctioned constructor (:func:`~brutex.feeds.binding.build_feed_sink` over
   :func:`~brutex.feeds.dhan_binding.build_dhan_binding`, so the tree nests under
   ``<lake_root>/feeds/dhan/`` — path-structural attribution, Groww bytes untouched)
   and runs the lean :func:`~brutex.adapters.dhan.ingest.run_dhan_spot_ingest_with_retry`.
3. **Prebuild (INTRADAY ONLY)** — :func:`run_dhan_prebuild` materialises the 19
   derived intraday timeframes for each pulled index partition, bound to
   ``dhan_root``, best-effort + crash-guarded per target. ``include_daily=False``
   is HARDCODED at the Dhan materialise call site (structural — not
   caller-flippable): the Dhan ``1day`` partition is NATIVE vendor truth and the
   prebuild must never overwrite it; the Groww law
   `DEC-1MINUTE-ONLY-DERIVE-1DAY-001` (derive 1day from 1m) stays Groww-only.
4. **Consistency observer (observe-only)** — after both spot stages the
   :mod:`~brutex.adapters.dhan.daily_consistency` observer compares native 1day
   closes vs the stored 1-minute aggregate for THIS pull's overlapping days
   (WARN-only, crash-guarded, kill switch ``BRUTEX_DHAN_DAILY_CONSISTENCY``).

**Why NOT ``run_full_pipeline``** (DEC-FEED-SPINE-GENERALIZE-001, option b — the
CORE recorded deferral): the Groww sequencer is coupled to the Groww
:class:`~brutex.dashboard.pull_controller.BulkSpotReport` (``spot_phase_ok`` reads
``auth_aborted`` / ``quota_exhausted`` / ``mass_empty_cause``), which the lean Dhan
:class:`~brutex.adapters.dhan.ingest.DhanIngestReport` does not carry. Adapting one
to the other would fork a fragile Groww-specific surface; instead the Dhan SPOT
track drives the binding fields directly from this focused spot-only body (the
full Groww+Dhan spine unification stays the named follow-up
DEC-FEED-SPINE-GENERALIZE-001). Movers-warm is deferred with it (the movers read
path is TOP-lake-bound; a feed-aware movers reader is the follow-up
DEC-DHAN-MOVERS-FEED-AWARE-001 — warming the TOP lake after a Dhan pull would warm
GROWW's movers, which would be dishonest, so a Dhan pull warms nothing).

**Testability.** The pull + prebuild cores are fully fake-driven (an injected fetch
adapter + a temp lake root + an injected ``materialize`` seam), so this module is
100% line+branch with no boto3 / dhanhq / network. The production seam
(:func:`default_dhan_spot_pull`) takes an injectable ``clients_factory`` +
``materialize`` seam so its resolve → daily → gate → pull → prebuild-gate →
observer orchestration (every gate branch) is fake-driven too; the ONLY
``# pragma: no cover`` site is the real credential + SDK client build
(:func:`build_dhan_spot_clients`), which needs AWS SSM + the ``dhanhq`` SDK.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import structlog

from brutex.adapters.dhan.constants import (
    DHAN_FETCH_INTERVAL,
    DHAN_SPOT_INDEX_SYMBOLS,
)
from brutex.adapters.dhan.daily_consistency import (
    consistency_enabled,
    observe_daily_minute_consistency_guarded,
)
from brutex.adapters.dhan.daily_ingest import DailyCandles, run_dhan_spot_daily
from brutex.adapters.dhan.ingest import (
    SPOT_SEGMENT,
    DhanIngestReport,
    FetchOhlcv,
    dhan_index_exchange,
    run_dhan_spot_ingest_with_retry,
)
from brutex.adapters.dhan.scrip_master import (
    ScripResolution,
    resolve_security_ids,
)
from brutex.data.feed_root import feed_lake_root
from brutex.data.session import filter_to_trading_session
from brutex.data.timeframe_materialize import (
    MaterializeResult,
    PrebuildKind,
    materialize_lake_timeframes,
)
from brutex.feeds.binding import build_feed_sink
from brutex.feeds.dhan_binding import build_dhan_binding

_LOG = structlog.get_logger("brutex.dashboard.dhan_pull")


def _noop_pace() -> None:
    """Default no-op pace hook (a production controller's ``pace`` is injected)."""


@dataclass(frozen=True, slots=True)
class DhanPullRequest:
    """The persistable request surface for one Dhan SPOT pull.

    Attributes
    ----------
    lake_root
        The TOP lake root — :func:`~brutex.feeds.binding.build_feed_sink` nests the
        write tree under ``<lake_root>/feeds/dhan/`` via the binding's
        ``lake_root_for``; the prebuild binds ``feed_lake_root(lake_root, feed="dhan")``.
    start, end
        The HALF-OPEN ``[start, end)`` fetch range (``end`` exclusive — an inclusive
        operator range passes ``end + 1 day`` at the surface).
    dry_run
        Whether this is the explicit synthetic offline pass (no real credentials /
        no vendor call — honestly writes nothing real, Golden Rule #1).
    today
        The IST date the rolling floor keys on (DEC-DHAN-ROLLING-FLOOR-001) —
        resolved ONCE per launch at the route (``now_ist().date()``) and threaded
        so a pull never re-reads the clock mid-plan. ``None`` (a headless/legacy
        caller) resolves once inside the spine.
    """

    lake_root: Path
    start: date
    end: date
    dry_run: bool
    today: date | None = None


@dataclass(frozen=True, slots=True)
class DhanPrebuildTarget:
    """One Dhan index partition the prebuild materialises (``bars/<exch>/INDEX/<sym>/``)."""

    exchange: str
    symbol: str


@dataclass(frozen=True, slots=True)
class DhanPrebuildOutcome:
    """The outcome of the Dhan prebuild stage — per-target success / honest degrade.

    Attributes
    ----------
    materialized
        ``{symbol → tf_count}`` for every target whose derived timeframes were
        materialised (the number of TF partitions written).
    failed
        ``{symbol → error_type}`` for every target whose materialise RAISED (best-effort,
        crash-guarded — a derive race never fails the already-successful pull).
    """

    materialized: Mapping[str, int]
    failed: Mapping[str, str]

    @property
    def ok(self) -> bool:
        """``True`` iff every attempted target materialised (no crash)."""
        return not self.failed

    def to_dict(self) -> dict[str, object]:
        """A JSON-safe summary for the route payload."""
        return {
            "materialized": dict(self.materialized),
            "failed": dict(self.failed),
            "ok": self.ok,
        }


def _ingest_block(report: DhanIngestReport) -> dict[str, object]:
    """The ONE JSON projection of a stage's :class:`DhanIngestReport` (daily AND spot)."""
    return {
        "total_bars": report.total_bars,
        "failed_windows": report.failed_windows,
        "resolved_symbols": list(report.resolved_symbols),
        "unresolved_symbols": list(report.unresolved_symbols),
    }


@dataclass(frozen=True, slots=True)
class DhanPullReport:
    """The result of a Dhan pull chain (daily → spot → prebuild) — the route payload source.

    Attributes
    ----------
    spot
        The lean 1-minute ingest report (per-window outcomes + honest degrade
        tallies). On a chain STOPPED at the daily stage this is an empty
        zero-outcome report (the 1-minute stage was never attempted —
        ``stopped_after`` is the honest marker, never a fabricated result).
    prebuild
        The derived-timeframe prebuild outcome (``None`` when the spot phase failed
        or the chain stopped — the prebuild is gated on a clean spot pull).
    dry_run
        Whether this was the synthetic offline pass.
    daily
        The NATIVE-daily stage report (DEC-DHAN-NATIVE-DAILY-001 — the FIRST
        chain stage). ``None`` on a dry-run (short-circuits before both stages)
        and for legacy constructions.
    stopped_after
        ``"daily"`` when a FAILED daily stage stopped the chain BEFORE any
        1-minute vendor call; ``None`` on a full chain.
    """

    spot: DhanIngestReport
    prebuild: DhanPrebuildOutcome | None
    dry_run: bool
    daily: DhanIngestReport | None = None
    stopped_after: str | None = None

    @property
    def daily_phase_ok(self) -> bool:
        """``True`` iff the native-daily stage (if run) left no FAILED window."""
        return self.daily is None or self.daily.failed_windows == 0

    @property
    def spot_phase_ok(self) -> bool:
        """``True`` iff the 1-minute phase RAN and left no re-pullable FAILED window.

        A chain STOPPED at the daily stage reads ``False`` (the empty stopped
        report's ``failed_windows == 0`` must never read as a clean 1-minute
        phase — Golden Rule #1).
        """
        return self.stopped_after is None and self.spot.failed_windows == 0

    @property
    def floor_clamped(self) -> bool:
        """``True`` iff the rolling floor genuinely MOVED the start (DEC-DHAN-ROLLING-FLOOR-001)."""
        return (
            self.spot.effective_start is not None
            and self.spot.requested_start is not None
            and self.spot.effective_start != self.spot.requested_start
        )

    @property
    def note(self) -> str:
        """The completion headline (the job's ``_finish_detail`` note contract).

        Un-clamped pulls read the byte-identical ``"dhan spot pipeline"``; a
        clamped pull names the clamp; a wholly-pre-floor pull reads the honest
        nothing-to-pull wording (DEC-DHAN-ROLLING-FLOOR-001). A dry-run
        short-circuits BEFORE planning, so its stamps are ``None`` and the note
        stays the plain headline (it echoes the REQUESTED range only). A chain
        STOPPED at the daily stage reads the honest stopped-note NAMING the
        daily stage (DEC-DHAN-NATIVE-DAILY-001 — it takes precedence over every
        other wording).
        """
        if self.stopped_after is not None:
            failed = 0 if self.daily is None else self.daily.failed_windows
            return (
                "dhan spot pipeline — STOPPED after the native-daily stage "
                f"({failed} failed daily window(s)); the 1-minute stage was "
                "not attempted"
            )
        if self.spot.entirely_pre_floor and self.spot.floor is not None:
            return (
                "nothing to pull (requested range wholly before the Dhan rolling "
                f"floor {self.spot.floor.isoformat()})"
            )
        requested = self.spot.requested_start
        effective = self.spot.effective_start
        if requested is not None and effective is not None and effective != requested:
            return (
                "dhan spot pipeline — start clamped "
                f"{requested.isoformat()} → {effective.isoformat()} "
                "(Dhan rolling 5y floor)"
            )
        return "dhan spot pipeline"

    def to_dict(self) -> dict[str, object]:
        """A JSON-safe payload dict for the ``/pull-pipeline-dhan`` route.

        The ``floor`` / ``effective_start`` keys are EMIT-ON-CLAMP additive
        (DEC-DHAN-ROLLING-FLOOR-001): an un-clamped pull's payload carries only the
        pre-existing keys plus the ``note`` headline, byte-compatible. The
        ``daily`` / ``daily_phase_ok`` / ``stopped_after`` keys are the
        DEC-DHAN-NATIVE-DAILY-001 chain state (``daily`` ``None`` on a dry-run;
        ``stopped_after`` ``None`` on a full chain).
        """
        payload: dict[str, object] = {
            "feed": "dhan",
            "dry_run": self.dry_run,
            "daily": None if self.daily is None else _ingest_block(self.daily),
            "daily_phase_ok": self.daily_phase_ok,
            "spot": _ingest_block(self.spot),
            "spot_phase_ok": self.spot_phase_ok,
            "stopped_after": self.stopped_after,
            "prebuild": None if self.prebuild is None else self.prebuild.to_dict(),
            "note": self.note,
        }
        if (self.floor_clamped or self.spot.entirely_pre_floor) and self.spot.floor is not None:
            payload["floor"] = self.spot.floor.isoformat()
            if self.spot.effective_start is not None:
                payload["effective_start"] = self.spot.effective_start.isoformat()
        return payload


def resolve_dhan_spot_security_ids(
    symbols: Sequence[str] = DHAN_SPOT_INDEX_SYMBOLS,
) -> ScripResolution:
    """Resolve the Dhan spot index symbols to securityIds from the hardcoded map, else omit.

    A thin wrapper over :func:`~brutex.adapters.dhan.scrip_master.resolve_security_ids`
    (the hardcoded verified-pending map + honest-omission core); an unresolved symbol is
    OMITTED (the loud-skip path in the ingest spine), never a blind id.
    """
    return resolve_security_ids(symbols)


def run_dhan_spot_pull(
    *,
    lake_root: Path,
    adapter: FetchOhlcv,
    security_ids: Mapping[str, int],
    start: date,
    end: date,
    symbols: Sequence[str] = DHAN_SPOT_INDEX_SYMBOLS,
    interval: str = DHAN_FETCH_INTERVAL,
    pace: Callable[[], None] = _noop_pace,
    session_filter: Callable[..., object] = filter_to_trading_session,
    max_rounds: int = 3,
    code_commit: str | None = None,
    today: date | None = None,
) -> DhanIngestReport:
    """Run the Dhan SPOT ingest into ``<lake_root>/feeds/dhan/`` and return the report.

    Builds the Dhan feed sink through the ONE sanctioned constructor
    (:func:`~brutex.feeds.binding.build_feed_sink` over :func:`build_dhan_binding` —
    the tree nests under ``feeds/dhan/`` + is wrapped in a
    :class:`~brutex.data.feed_audit.FeedAuditSink` for the Golden-Rule-#8 hash chain),
    then runs the lean bounded-retry spot spine
    (:func:`~brutex.adapters.dhan.ingest.run_dhan_spot_ingest_with_retry`).

    Parameters
    ----------
    lake_root
        The TOP lake root; the sink resolves ``<lake_root>/feeds/dhan/`` internally.
    adapter, security_ids
        The injected fetch adapter (a :class:`~brutex.adapters.dhan.adapter.DhanMarketAdapter`
        or a fake) and the resolved ``{symbol → securityId}`` map.
    start, end
        The half-open ``[start, end)`` fetch range.
    symbols, interval, pace, session_filter, max_rounds
        The index set (default the Dhan spot index set), the interval (1-minute), the
        controller pace hook, the calendar session filter, and the retry-round budget.
    code_commit
        The optional commit stamp threaded into the feed audit chain.
    today
        The IST date the rolling floor keys on (DEC-DHAN-ROLLING-FLOOR-001),
        threaded verbatim into the spine — the returned report's
        ``requested_start`` / ``effective_start`` / ``floor`` stamps are computed
        from this SAME date.
    """
    sink = build_feed_sink(
        lake_root=lake_root,
        binding=build_dhan_binding(),
        code_commit=code_commit,
    )
    return run_dhan_spot_ingest_with_retry(
        symbols=symbols,
        adapter=adapter,
        sink=sink,
        security_ids=security_ids,
        start=start,
        end=end,
        interval=interval,
        session_filter=session_filter,
        pace=pace,
        max_rounds=max_rounds,
        today=today,
    )


def run_dhan_spot_daily_pull(
    *,
    lake_root: Path,
    client: DailyCandles,
    security_ids: Mapping[str, int],
    start: date,
    end: date,
    symbols: Sequence[str] = DHAN_SPOT_INDEX_SYMBOLS,
    pace: Callable[[], None] = _noop_pace,
    max_rounds: int = 3,
    code_commit: str | None = None,
    today: date | None = None,
) -> DhanIngestReport:
    """Run the Dhan NATIVE-daily ingest into ``<lake_root>/feeds/dhan/`` — the FIRST stage.

    DEC-DHAN-NATIVE-DAILY-001 (implementing `DEC-SCOPE-MINIMAL-CORE-001`): the
    ``1day`` bars are pulled from the vendor's daily endpoint, never derived
    from 1-minute. Builds the SAME Dhan feed sink as :func:`run_dhan_spot_pull`
    (:func:`~brutex.feeds.binding.build_feed_sink` over :func:`build_dhan_binding`)
    and runs the native-daily spine
    (:func:`~brutex.adapters.dhan.daily_ingest.run_dhan_spot_daily` — the FIXED
    ``DHAN_DAILY_HISTORY_FLOOR`` + the `DEC-DHAN-TODAY-CLAMP-001` prev-day end
    clamp + trading-DATE session filtering + the terminal-stop carve-out).

    Parameters mirror :func:`run_dhan_spot_pull`; *client* is the narrow
    :class:`~brutex.adapters.dhan.daily_ingest.DailyCandles` surface (the
    production :class:`~brutex.adapters.dhan.client.DhanClient` satisfies it).
    """
    sink = build_feed_sink(
        lake_root=lake_root,
        binding=build_dhan_binding(),
        code_commit=code_commit,
    )
    return run_dhan_spot_daily(
        symbols=symbols,
        client=client,
        sink=sink,
        security_ids=security_ids,
        start=start,
        end=end,
        pace=pace,
        max_rounds=max_rounds,
        today=today,
    )


def dhan_prebuild_targets(security_ids: Mapping[str, int]) -> list[DhanPrebuildTarget]:
    """Build the per-index prebuild targets from the RESOLVED securityId map.

    One target per resolved symbol, with the SAME exchange attribution the ingest
    used (:func:`~brutex.adapters.dhan.ingest.dhan_index_exchange` — SENSEX → BSE,
    else NSE), so the derived-TF partitions land beside the pulled 1-minute bars.
    """
    return [
        DhanPrebuildTarget(exchange=dhan_index_exchange(symbol).value, symbol=symbol)
        for symbol in security_ids
    ]


def run_dhan_prebuild(
    *,
    dhan_root: Path,
    targets: Sequence[DhanPrebuildTarget],
    materialize: Callable[..., MaterializeResult] = materialize_lake_timeframes,
) -> DhanPrebuildOutcome:
    """Materialise the derived INTRADAY timeframes for each Dhan index partition, best-effort.

    Loops the SPOT index targets, calling the SHARED segment-aware
    :func:`~brutex.data.timeframe_materialize.materialize_lake_timeframes` (CLAUDE.md
    §11 COMMON) bound to *dhan_root* — the 19 derived intraday TFs ONLY.
    ``include_daily=False`` is HARDCODED at the call site below (STRUCTURAL — it is
    deliberately NOT a parameter, so no caller can flip it): the Dhan ``1day``
    partition holds NATIVE vendor daily bars pulled FIRST in the chain
    (DEC-DHAN-NATIVE-DAILY-001, implementing `DEC-SCOPE-MINIMAL-CORE-001`) and the
    prebuild must NEVER overwrite them with a 1-minute derivation — the Groww law
    `DEC-1MINUTE-ONLY-DERIVE-1DAY-001` (derive ``1day`` from the stored 1-minute)
    stays Groww-only. Crash-guarded per target: a materialise that
    RAISES is logged (``dhan.prebuild.target_failed``) + counted, never a raise that
    could kill the already-successful spot pull (the Groww prebuild crash-guard
    posture). ``dense_symbol=True`` for a SPOT INDEX (the daily-open-substitution WARN
    gate).
    """
    materialized: dict[str, int] = {}
    failed: dict[str, str] = {}
    for target in targets:
        try:
            result = materialize(
                lake_root=dhan_root,
                exchange=target.exchange,
                segment=SPOT_SEGMENT,
                symbol=target.symbol,
                kind=PrebuildKind.SPOT,
                # THE LAW (DEC-DHAN-NATIVE-DAILY-001, implementing
                # `DEC-SCOPE-MINIMAL-CORE-001`): the Dhan prebuild derives
                # INTRADAY TFs only — the native-daily 1day partition is vendor
                # truth, NEVER re-derived/overwritten from 1-minute. Hardcoded
                # (no parameter) so the flip is structural, not caller-choosable.
                include_daily=False,
                dense_symbol=True,
            )
        except Exception as exc:  # pylint: disable=broad-exception-caught — best-effort, never fail the pull
            _LOG.warning(
                "dhan.prebuild.target_failed",
                symbol=target.symbol,
                exchange=target.exchange,
                error_type=type(exc).__name__,
            )
            failed[target.symbol] = type(exc).__name__
            continue
        materialized[target.symbol] = sum(result.written.values())
    return DhanPrebuildOutcome(materialized=materialized, failed=failed)


def dhan_credentials_error(*, client: object | None = None) -> str | None:
    """Return the Dhan credential fix text if the read-only SSM creds do not resolve, else ``None``.

    The Dhan twin of :func:`~brutex.config.credentials.groww_credentials_error`, wired
    into the SHARED :func:`~brutex.adapters.ingest_common.gate_pull_credentials` so a
    real Dhan pull with missing/unreachable SSM credentials is a LOUD refusal (HTTP 503
    / non-zero exit) carrying the actionable fix text, never a silent synthetic
    downgrade (DEC-PULL-LOUD-CREDENTIAL-REFUSAL-001). *client* is an injected
    :class:`~brutex.adapters.groww.aws_client.AwsClient` for tests; ``None`` builds the
    real boto3 client lazily inside :func:`~brutex.config.credentials_dhan_aws.\
load_dhan_aws_credentials`.
    """
    from brutex.config.credentials_dhan_aws import load_dhan_aws_credentials
    from brutex.config.credentials_types import CredentialsError

    try:
        load_dhan_aws_credentials(client=client)  # type: ignore[arg-type]
    except CredentialsError as exc:
        return str(exc)
    return None


@dataclass(frozen=True, slots=True)
class DhanSpotClients:
    """The pair of vendor surfaces ONE Dhan spot pull chain needs.

    Attributes
    ----------
    fetch
        The 1-minute :class:`~brutex.adapters.dhan.ingest.FetchOhlcv` adapter.
    daily
        The narrow :class:`~brutex.adapters.dhan.daily_ingest.DailyCandles`
        surface for the native-daily stage. In production BOTH ride the SAME
        underlying :class:`~brutex.adapters.dhan.client.DhanClient` (one
        refresher, one ``_call`` funnel — built once by
        :func:`build_dhan_spot_clients`).
    """

    fetch: FetchOhlcv
    daily: DailyCandles


def build_dhan_spot_clients(
    *,
    security_ids: Mapping[str, int],
    client: object | None = None,
) -> DhanSpotClients:  # pragma: no cover — real AWS SSM + dhanhq SDK build
    """Build the production Dhan client pair (read-only SSM creds → dhanhq SDK).

    Loads the READ-ONLY Dhan credentials from AWS SSM
    (:func:`~brutex.config.credentials_dhan_aws.load_dhan_aws_credentials`), builds the
    single-flight read-only token refresher
    (:class:`~brutex.adapters.dhan.reauth.DhanSdkRefresher` — the local-mint arm is
    structurally absent; it RE-READS SSM on a DH-901), wraps it in ONE
    :class:`~brutex.adapters.dhan.client.DhanClient` (which serves BOTH the
    native-daily stage — it satisfies :class:`~brutex.adapters.dhan.daily_ingest.\
DailyCandles` — and, composed into the
    :class:`~brutex.adapters.dhan.adapter.DhanMarketAdapter`, the 1-minute stage).
    ``# pragma: no cover`` — it needs real AWS + the ``dhanhq`` SDK; the
    pull/prebuild cores are fake-driven.
    """
    from brutex.adapters.dhan.adapter import DhanMarketAdapter
    from brutex.adapters.dhan.client import DhanClient
    from brutex.adapters.dhan.reauth import DhanSdkRefresher
    from brutex.adapters.dhan.token_store_aws import build_readonly_dhan_token_store_with
    from brutex.adapters.groww.aws_client import build_real_aws_client
    from brutex.adapters.groww.constants import resolve_token_broker_region
    from brutex.config.credentials_dhan_aws import load_dhan_aws_credentials

    creds = load_dhan_aws_credentials(client=client)  # type: ignore[arg-type]
    aws_client = (
        client if client is not None else build_real_aws_client(resolve_token_broker_region())
    )
    token_store = build_readonly_dhan_token_store_with(aws_client)  # type: ignore[arg-type]
    refresher = DhanSdkRefresher(client_id=creds.client_id, token_reread=token_store.load)
    dhan_client = DhanClient(refresher=refresher)
    return DhanSpotClients(
        fetch=DhanMarketAdapter(client=dhan_client, security_ids=security_ids),
        daily=dhan_client,
    )


def default_dhan_spot_pull(
    request: DhanPullRequest,
    *,
    pace: Callable[[], None] = _noop_pace,
    clients_factory: Callable[..., DhanSpotClients] = build_dhan_spot_clients,
    materialize: Callable[..., MaterializeResult] = materialize_lake_timeframes,
) -> DhanPullReport:
    """The production Dhan pull seam: resolve ids → NATIVE-daily FIRST → 1-minute → prebuild.

    Resolves the index securityIds from the hardcoded verified-pending map (honest
    omission for an unknown symbol), builds the Dhan client pair via *clients_factory*
    (the production default :func:`build_dhan_spot_clients` — a ``# pragma: no
    cover`` build needing real AWS SSM + the ``dhanhq`` SDK), then runs the chain
    (DEC-DHAN-NATIVE-DAILY-001, implementing `DEC-SCOPE-MINIMAL-CORE-001`):

    1. the NATIVE-daily stage (:func:`run_dhan_spot_daily_pull`) runs FIRST;
    2. **THE GATE** — a FAILED daily stage (``failed_windows > 0``) STOPS the
       chain BEFORE any 1-minute vendor call: one loud ``dhan.daily_stage_failed``
       WARNING, an honest stopped-note naming the daily stage
       (``stopped_after="daily"``), ``prebuild=None``, and an EMPTY zero-outcome
       spot report (never a fabricated 1-minute result). A RAISED halt (the
       :func:`~brutex.adapters.dhan.errors.is_terminal_pull_stop` carve-out)
       propagates out of the daily stage and equally stops the chain — the job
       layer reports it as the pull error;
    3. the 1-minute spot ingest runs into ``feeds/dhan/``;
    4. only on a CLEAN spot phase (``failed_windows == 0``) the INTRADAY-only
       prebuild runs bound to ``dhan_root`` (``include_daily=False`` structural —
       :func:`run_dhan_prebuild`);
    5. the observe-only daily-vs-minute consistency observer runs over THIS
       pull's range (crash-guarded — an observer failure never fails the pull;
       kill switch ``BRUTEX_DHAN_DAILY_CONSISTENCY``, resolved ONCE at entry so
       junk fails loud BEFORE any vendor call).

    **Dry-run is HONEST (Golden Rule #1).** When ``request.dry_run`` is set the seam
    short-circuits AFTER resolving the securityId map: it does **NOT** build the real
    clients (no AWS SSM / ``dhanhq`` SDK), issues **NO** vendor call, and writes
    **NOTHING** (neither daily/spot bars nor derived timeframes), so the
    :class:`DhanPullRequest` ``dry_run`` docstring's "no vendor call — honestly writes
    nothing real" is literally true. The returned :class:`DhanPullReport` carries a
    zero-outcome spot report (``total_bars=0``, ``failed_windows=0``) with the
    resolved / unresolved symbols the run WOULD pull, ``daily=None`` (the daily
    stage never planned), and ``prebuild=None`` (nothing derived), so the route
    payload reads "would pull the resolved indices over ``[start, end)``; no
    vendor call, nothing written". *clients_factory* and
    *materialize* are never invoked on this path. Because the short-circuit runs
    BEFORE planning, the dry-run report echoes the REQUESTED range only — its
    rolling-floor stamps (``requested_start`` / ``effective_start`` / ``floor``)
    stay ``None`` and the note stays the plain headline
    (DEC-DHAN-ROLLING-FLOOR-001; the route's start payload still names the clamp).

    The resolve → daily → GATE → pull → prebuild-GATE → prebuild → observer
    orchestration itself is fully fake-driven: a test injects *clients_factory*
    (a daily+fetch fake pair) + *materialize* (a derive-seam fake) over a temp
    lake root, so every gate branch is covered offline; only the real client
    build stays uncovered.

    Parameters
    ----------
    request
        The persistable pull request (lake root + half-open ``[start, end)`` + dry-run).
    pace
        The controller pace hook threaded into BOTH vendor stages.
    clients_factory
        Builds the :class:`DhanSpotClients` pair from the resolved
        ``{symbol → securityId}`` map (default the real SSM/``dhanhq`` build).
        Never called on the ``dry_run`` path.
    materialize
        The derived-timeframe materialise seam threaded into the prebuild (default the
        SHARED :func:`~brutex.data.timeframe_materialize.materialize_lake_timeframes`).
        Never called on the ``dry_run`` path.
    """
    # Resolve the observer kill switch ONCE at entry (fail-loud junk BEFORE any
    # vendor call — the resolve_* law; DEC-DHAN-NATIVE-DAILY-001 item 7).
    consistency = consistency_enabled()
    resolution = resolve_dhan_spot_security_ids()
    if request.dry_run:
        # HONEST offline pass: resolve the ids ONLY — no client build, no vendor call,
        # no write, no prebuild, no observer (Golden Rule #1). Zero outcomes ⇒
        # total_bars=0, failed_windows=0; the resolved / unresolved symbols carry the
        # "would pull" plan. clients_factory + materialize are never invoked here.
        dry_spot = DhanIngestReport(
            outcomes=(),
            resolved_symbols=tuple(resolution.security_ids),
            unresolved_symbols=resolution.unresolved,
        )
        _LOG.info(
            "dhan.spot.pull_dry_run",
            resolved=len(resolution.security_ids),
            unresolved=len(resolution.unresolved),
            start=request.start.isoformat(),
            end=request.end.isoformat(),
            dry_run=True,
        )
        return DhanPullReport(spot=dry_spot, prebuild=None, dry_run=True)
    clients = clients_factory(security_ids=resolution.security_ids)
    daily = run_dhan_spot_daily_pull(
        lake_root=request.lake_root,
        client=clients.daily,
        security_ids=resolution.security_ids,
        start=request.start,
        end=request.end,
        pace=pace,
        today=request.today,
    )
    if daily.failed_windows > 0:
        # THE GATE (DEC-DHAN-NATIVE-DAILY-001): a FAILED daily stage stops the
        # chain BEFORE any 1-minute vendor call — the stored 1day partition is
        # the chain's vendor-truth foundation, so a broken daily pull must never
        # be papered over by a healthy-looking 1-minute run. The spot report is
        # an EMPTY zero-outcome record (the stage never ran) with
        # ``stopped_after="daily"`` as the honest marker.
        _LOG.warning(
            "dhan.daily_stage_failed",
            failed_windows=daily.failed_windows,
            detail="chain stopped — the 1-minute stage was not attempted",
        )
        stopped_spot = DhanIngestReport(
            outcomes=(),
            resolved_symbols=daily.resolved_symbols,
            unresolved_symbols=daily.unresolved_symbols,
        )
        return DhanPullReport(
            spot=stopped_spot,
            prebuild=None,
            dry_run=request.dry_run,
            daily=daily,
            stopped_after="daily",
        )
    spot = run_dhan_spot_pull(
        lake_root=request.lake_root,
        adapter=clients.fetch,
        security_ids=resolution.security_ids,
        start=request.start,
        end=request.end,
        pace=pace,
        today=request.today,
    )
    dhan_root = feed_lake_root(request.lake_root, feed="dhan")
    targets = dhan_prebuild_targets(resolution.security_ids)
    prebuild: DhanPrebuildOutcome | None = None
    if spot.failed_windows == 0:
        prebuild = run_dhan_prebuild(
            dhan_root=dhan_root,
            targets=targets,
            materialize=materialize,
        )
    if consistency:
        # Observe-only + crash-guarded (DEC-DHAN-NATIVE-DAILY-001 item 7): compare
        # the native 1day closes vs the stored 1-minute aggregate for THIS pull's
        # overlapping days — WARNs only, never rewrites either side, never fails
        # the pull.
        observe_daily_minute_consistency_guarded(
            dhan_root=dhan_root,
            targets=[(target.exchange, target.symbol) for target in targets],
            start=request.start,
            end=request.end,
        )
    return DhanPullReport(spot=spot, prebuild=prebuild, dry_run=request.dry_run, daily=daily)


#: The route seam type — a callable that runs one Dhan SPOT pull (+ prebuild). The
#: production default is :func:`default_dhan_spot_pull`; an integration test injects a
#: fake that writes synthetic bars into ``feeds/dhan/`` with no network.
DhanSpotPullFn = Callable[[DhanPullRequest], DhanPullReport]


__all__ = [
    "DhanPrebuildOutcome",
    "DhanPrebuildTarget",
    "DhanPullReport",
    "DhanPullRequest",
    "DhanSpotClients",
    "DhanSpotPullFn",
    "build_dhan_spot_clients",
    "default_dhan_spot_pull",
    "dhan_credentials_error",
    "dhan_prebuild_targets",
    "resolve_dhan_spot_security_ids",
    "run_dhan_prebuild",
    "run_dhan_spot_daily_pull",
    "run_dhan_spot_pull",
]
