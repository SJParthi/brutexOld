"""Dhan rolling-ATM option pull orchestration — the dashboard-route + CLI pull core.

DEC-DHAN-ROLLING-OPTIONS-001 (ADR-0022). The SURFACE glue that drives the standalone
Dhan **rolling-ATM** option track end-to-end into the ``feeds/dhan/`` lake namespace —
the options sibling of :mod:`brutex.dashboard.dhan_pull` (spot). It:

1. builds the honest rolling-ATM writer bound to the Dhan FEED root
   (:func:`~brutex.data.feed_root.feed_lake_root` → ``<lake_root>/feeds/dhan/``, so the
   rolling lake nests under ``feeds/dhan/rolling_options/`` — path-structural
   attribution, Groww bytes untouched);
2. enumerates the PURE cartesian of 1008 rolling-ATM keys (NO metadata phase — Dhan
   serves NO per-expired-contract candles, only an ATM-relative rolling series); and
3. runs the lean bounded-retry rolling spine
   (:func:`~brutex.adapters.dhan.run_dhan_rolling_option_ingest.\
run_dhan_rolling_option_ingest_with_retry`).

**HONESTY (binding, §5).** Every label here is **rolling-ATM** (ATM±N per bar), NEVER
"per-contract / per-expiry / per-strike" — Dhan serves no option chain. A key Dhan does
not serve degrades to an honest EMPTY (clean-empty, re-pullable), never a fabricated
bar; a DH-905 servability refusal (UNVERIFIED) surfaces as a re-pullable FAILED window.

**Testability.** The pull core is fully fake-driven (an injected
:class:`~brutex.adapters.dhan.rolling_option_adapter.RollingOptionAdapter` over a fake
:class:`~brutex.adapters.dhan.rolling_option_adapter.RollingOptionClientProtocol` + a
temp lake root), and :func:`default_dhan_rolling_option_pull` takes an injectable
``adapter_factory`` so its enumerate → build-writer → ingest orchestration is
fake-driven too. The ONLY ``# pragma: no cover`` is the real credential + SDK adapter
build (:func:`build_dhan_rolling_option_adapter`), which needs AWS SSM + the ``dhanhq``
SDK — the exact spot precedent (:func:`~brutex.dashboard.dhan_pull.build_dhan_spot_adapter`).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import structlog

from brutex.adapters.dhan.rolling_option_adapter import RollingOptionAdapter
from brutex.adapters.dhan.rolling_option_constants import (
    DHAN_ROLLING_FETCH_INTERVAL,
    DHAN_ROLLING_STRIKE_BAND,
)
from brutex.adapters.dhan.rolling_option_plan import (
    RollingOptionKey,
    enumerate_rolling_option_keys,
)
from brutex.adapters.dhan.rolling_option_writer import RollingOptionWriter
from brutex.adapters.dhan.run_dhan_rolling_option_ingest import (
    RollingOptionIngestReport,
    run_dhan_rolling_option_ingest_with_retry,
)
from brutex.dashboard.dhan_pull import DhanPullRequest
from brutex.data.feed_root import feed_lake_root

_LOG = structlog.get_logger("brutex.dashboard.dhan_rolling_pull")

#: The dataset label — every surface says "rolling-ATM", NEVER "per-contract" (§5).
DHAN_ROLLING_DATASET: str = "rolling-options"


def _noop_pace() -> None:
    """Default no-op pace hook (a production controller's ``pace`` is injected)."""


@dataclass(frozen=True, slots=True)
class DhanRollingOptionPullReport:
    """The result of a Dhan rolling-ATM option pull — the route payload source.

    Attributes
    ----------
    ingest
        The lean rolling-ATM ingest report (per-window outcomes + honest tallies).
    dry_run
        Whether this was the synthetic offline pass (no real credentials / no vendor
        call — honestly writes nothing real, Golden Rule #1).
    """

    ingest: RollingOptionIngestReport
    dry_run: bool

    @property
    def total_bars(self) -> int:
        """Total NEW rolling-ATM bars merged across all SUCCESS windows."""
        return self.ingest.total_bars

    @property
    def failed_windows(self) -> int:
        """Number of windows that ended FAILED (re-pullable — e.g. a DH-905 refusal)."""
        return self.ingest.failed_windows

    @property
    def keys_planned(self) -> int:
        """Number of rolling-ATM keys the run planned (the pure cartesian)."""
        return len(self.ingest.keys)

    def to_dict(self) -> dict[str, object]:
        """A JSON-safe payload dict for the ``/pull-rolling-options-dhan`` route.

        Labelled ``dataset="rolling-ATM"`` (the HONESTY LAW — never "per-contract").
        """
        return {
            "feed": "dhan",
            "dataset": "rolling-ATM",  # ATM±N per bar — NOT per-contract/per-expiry/per-strike
            "dry_run": self.dry_run,
            "keys_planned": self.keys_planned,
            "total_bars": self.total_bars,
            "failed_windows": self.failed_windows,
            "market": self.ingest.market,
        }


def run_dhan_rolling_option_pull(
    *,
    lake_root: Path,
    adapter: RollingOptionAdapter,
    keys: Sequence[RollingOptionKey],
    start: date,
    end: date,
    interval: str = DHAN_ROLLING_FETCH_INTERVAL,
    pace: Callable[[], None] = _noop_pace,
    max_rounds: int = 3,
) -> RollingOptionIngestReport:
    """Run the Dhan rolling-ATM option ingest into ``<lake_root>/feeds/dhan/`` and report.

    Builds the honest rolling-ATM writer bound to the Dhan FEED root
    (``feed_lake_root(lake_root, feed="dhan")`` — the tree nests under
    ``feeds/dhan/rolling_options/``, so Groww bytes are untouched), then runs the lean
    bounded-retry rolling spine over *keys*. Each key IS the partition / dedup identity
    — NO metadata phase (Dhan serves no chain).

    Parameters
    ----------
    lake_root
        The TOP lake root; the writer resolves ``<lake_root>/feeds/dhan/`` internally.
    adapter, keys
        The injected rolling-ATM fetch adapter (a
        :class:`~brutex.adapters.dhan.rolling_option_adapter.RollingOptionAdapter` over a
        real :class:`~brutex.adapters.dhan.client.DhanClient` or a fake) and the pure
        cartesian of rolling keys to pull.
    start, end
        The half-open ``[start, end)`` fetch range (``end`` exclusive — an inclusive
        operator range passes ``end + 1 day`` at the surface).
    interval, pace, max_rounds
        The interval (1-minute — the only rolling interval), the controller pace hook,
        and the retry-round budget.
    """
    dhan_root = feed_lake_root(lake_root, feed="dhan")
    writer = RollingOptionWriter(lake_root=dhan_root)
    return run_dhan_rolling_option_ingest_with_retry(
        keys=keys,
        adapter=adapter,
        writer=writer,
        start=start,
        end=end,
        interval=interval,
        pace=pace,
        max_rounds=max_rounds,
    )


def build_dhan_rolling_option_adapter(
    *,
    client: object | None = None,
) -> RollingOptionAdapter:  # pragma: no cover — real AWS SSM + dhanhq SDK build
    """Build the production Dhan rolling-ATM fetch adapter (read-only SSM creds → dhanhq).

    Loads the READ-ONLY Dhan credentials from AWS SSM
    (:func:`~brutex.config.credentials_dhan_aws.load_dhan_aws_credentials`), builds the
    single-flight read-only token refresher
    (:class:`~brutex.adapters.dhan.reauth.DhanSdkRefresher` — the local-mint arm is
    structurally absent; it RE-READS SSM on a DH-901), wraps it in a
    :class:`~brutex.adapters.dhan.client.DhanClient` (which drives the rolling-ATM
    ``expired_options_data`` call through the SAME bounded-retry / DH-901 re-read funnel
    the spot pull uses — so a :class:`DhanClient` is a drop-in
    :class:`~brutex.adapters.dhan.rolling_option_adapter.RollingOptionClientProtocol`),
    and composes the
    :class:`~brutex.adapters.dhan.rolling_option_adapter.RollingOptionAdapter`.
    ``# pragma: no cover`` — it needs real AWS + the ``dhanhq`` SDK; the pull core is
    fake-driven. Mirrors :func:`~brutex.dashboard.dhan_pull.build_dhan_spot_adapter`.
    """
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
    return RollingOptionAdapter(client=dhan_client)


def default_dhan_rolling_option_pull(
    request: DhanPullRequest,
    *,
    pace: Callable[[], None] = _noop_pace,
    adapter_factory: Callable[..., RollingOptionAdapter] = build_dhan_rolling_option_adapter,
    band: int = DHAN_ROLLING_STRIKE_BAND,
    max_rounds: int = 3,
) -> DhanRollingOptionPullReport:
    """The production Dhan rolling-ATM pull seam: enumerate keys → build adapter → pull.

    Enumerates the PURE cartesian of rolling-ATM keys
    (:func:`~brutex.adapters.dhan.rolling_option_plan.enumerate_rolling_option_keys` —
    1008 at the default ``band=10``; NO vendor metadata call), builds the rolling-ATM
    fetch adapter via *adapter_factory* (the production default
    :func:`build_dhan_rolling_option_adapter` — the SOLE ``# pragma: no cover``, needing
    real AWS SSM + the ``dhanhq`` SDK), and runs the rolling ingest into ``feeds/dhan/``.

    The enumerate → build → ingest orchestration is fully fake-driven: a test injects
    *adapter_factory* (a fake-client adapter) over a temp lake root, so this whole seam
    is covered offline; only the real adapter build stays uncovered.

    Parameters
    ----------
    request
        The persistable pull request (lake root + half-open ``[start, end)`` + dry-run).
    pace
        The controller pace hook threaded into the rolling spine.
    adapter_factory
        Builds the rolling-ATM :class:`RollingOptionAdapter` (default the real
        SSM/``dhanhq`` build).
    band
        The ATM strike-band half-width (default 10 → the 1008-key cartesian).
    max_rounds
        The bounded retry-round budget for the rolling spine.
    """
    keys = enumerate_rolling_option_keys(band=band)
    adapter = adapter_factory()
    ingest = run_dhan_rolling_option_pull(
        lake_root=request.lake_root,
        adapter=adapter,
        keys=keys,
        start=request.start,
        end=request.end,
        pace=pace,
        max_rounds=max_rounds,
    )
    _LOG.info(
        "dhan.rolling_option.pull_done",
        keys=len(keys),
        total_bars=ingest.total_bars,
        failed_windows=ingest.failed_windows,
        dry_run=request.dry_run,
    )
    return DhanRollingOptionPullReport(ingest=ingest, dry_run=request.dry_run)


#: The route seam type — a callable that runs one Dhan rolling-ATM option pull. The
#: production default is :func:`default_dhan_rolling_option_pull`; an integration test
#: injects a fake that writes synthetic rolling bars into ``feeds/dhan/`` with no network.
DhanRollingOptionPullFn = Callable[[DhanPullRequest], DhanRollingOptionPullReport]


__all__ = [
    "DHAN_ROLLING_DATASET",
    "DhanRollingOptionPullFn",
    "DhanRollingOptionPullReport",
    "build_dhan_rolling_option_adapter",
    "default_dhan_rolling_option_pull",
    "run_dhan_rolling_option_pull",
]
