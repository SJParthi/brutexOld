"""Bulk DERIVATIVES pull — the "Pull expiries + contracts" engine (DEC-PULL-DERIVATIVES-001).

The derivatives twin of :mod:`brutex.dashboard.pull_controller`'s spot pull. For a
selected set of underlyings it enumerates the **entire F&O chain** — every expiry,
every listed strike — and ingests the option (CE/PE) and/or future (FUT) candles
into the lake, reusing the verified per-contract ingest spine
(:func:`~brutex.adapters.groww.option_ingest.ingest_underlying_options` /
:func:`~brutex.adapters.groww.option_ingest.ingest_underlying_futures`), the
WindowLedger O(1) skip/record dedup, and the session rate controller.

Two deliberate design choices the operator asked for:

* **`1day` FIRST, then `1minute`** (:data:`GROWW_DERIVATIVE_INTERVALS_ORDER`). The
  ``1day`` skeleton is cheap (~one bar/day), verifiable against NSE (the 🛡 verify
  anchor, DEC-NSE-VERIFY-ROUTE-001), and lands the chain's shape in minutes; the
  ``1minute`` fill is the heavy pass and is only worth launching once the cheap
  skeleton is trusted. The interval is a SELECTABLE knob (``1day`` / ``1minute`` /
  ``both`` — and ``both`` runs ``1day`` before ``1minute``).
* **Honest dry-run** (Golden Rule #1): without live Groww credentials the option
  CHAIN itself — which strikes / expiries actually exist — is unknown REAL vendor
  data we refuse to fabricate (unlike a known spot symbol's synthetic OHLC). So the
  dry-run path enumerates an EMPTY chain and reports ``0`` contracts with an honest
  "set credentials" note — it never invents strikes.

Honest envelope (Golden Rule #10): the per-contract ingest + the WindowLedger
skip/record are O(1) per window, but enumerating the full chain across all expiries
is a NETWORK pull (``get_expiries`` × ``get_contracts``) and the ``1minute`` fill for
all-strikes × all-expiries is genuinely weeks-scale — NOT O(1)-total, and not
claimed. The WindowLedger makes every window rerun-safe (idempotent).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Final

from brutex.adapters.groww.client import GrowwClient
from brutex.adapters.groww.option_ingest import (
    GrowwOptionIngestReport,
    ingest_underlying_futures,
    ingest_underlying_options,
)
from brutex.adapters.groww.run_ingest import ProgressHook
from brutex.data.sink import BarSink, NullBarSink
from brutex.data.window_ledger import WindowLedger
from brutex.data.window_ledger_store import (
    begin_pull_ledger,
    end_pull_ledger,
    make_live_ledger_flush,
)
from brutex.models import Instrument

if TYPE_CHECKING:
    from brutex.dashboard.pull_controller import SessionRateController

#: FNO history floor — Groww FNO data is "available from 2020" (CLAUDE.md §6),
#: a year earlier than the spot ``GROWW_HISTORY_FLOOR`` (2021).
GROWW_DERIVATIVE_HISTORY_FLOOR: Final[date] = date(2020, 1, 1)

#: The ordered intervals for a ``both`` pull — ``1day`` BEFORE ``1minute`` so the
#: cheap, NSE-verifiable skeleton lands before the heavy minute fill (operator
#: decree, DEC-PULL-DERIVATIVES-001).
GROWW_DERIVATIVE_INTERVALS_ORDER: Final[tuple[str, str]] = ("1day", "1minute")

#: Contract-type tokens.
OPTION_KIND: Final[str] = "option"
FUTURE_KIND: Final[str] = "future"

#: Factory: ``(lake_root) -> (client, sink)`` — injected in tests; the default
#: picks the honest empty-chain dry-run client or the live client.
DerivativeClientFactory = Callable[[Path], tuple[GrowwClient, BarSink]]


def resolve_pull_intervals(choice: str) -> tuple[str, ...]:
    """Map an interval *choice* to the ordered intervals to pull.

    ``"1day"`` → ``("1day",)``; ``"1minute"`` → ``("1minute",)``; ``"both"`` →
    :data:`GROWW_DERIVATIVE_INTERVALS_ORDER` (``1day`` first). An unknown choice
    raises ``ValueError`` (surfaced as a 400 by the route).
    """
    if choice == "1day":
        return ("1day",)
    if choice == "1minute":
        return ("1minute",)
    if choice == "both":
        return GROWW_DERIVATIVE_INTERVALS_ORDER
    raise ValueError(f"interval must be one of '1day' / '1minute' / 'both', got {choice!r}")


def resolve_contract_types(choice: str) -> tuple[str, ...]:
    """Map a contract-type *choice* to the ordered kinds to pull.

    ``"option"`` → ``("option",)``; ``"future"`` → ``("future",)``; ``"both"`` →
    ``("option", "future")``. An unknown choice raises ``ValueError``.
    """
    if choice == OPTION_KIND:
        return (OPTION_KIND,)
    if choice == FUTURE_KIND:
        return (FUTURE_KIND,)
    if choice == "both":
        return (OPTION_KIND, FUTURE_KIND)
    raise ValueError(f"contract_type must be one of 'option' / 'future' / 'both', got {choice!r}")


def _parse_iso(value: str, field: str) -> date:
    """Parse an ISO ``YYYY-MM-DD`` *value* → :class:`date` (``ValueError` on junk)."""
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"invalid {field} {value!r} — expected ISO YYYY-MM-DD ({exc})") from exc


def resolve_derivative_range(
    *, from_date: str | None, to_date: str | None, now: datetime
) -> tuple[date, date]:
    """Resolve the half-open ``[start, end)`` date window a derivatives pull covers.

    ``from_date`` defaults to :data:`GROWW_DERIVATIVE_HISTORY_FLOOR` (2020-01-01);
    ``to_date`` defaults to the **previous day** (so a routine pull never grabs the
    in-progress session). Both ISO bounds are day-INCLUSIVE, so ``end`` is the
    midnight that STARTS the day after ``to_date`` (the ingest treats ``end`` as
    exclusive). The pair is clamped to ``[floor, today]``; an empty range raises.
    """
    today = now.astimezone(UTC).date()
    floor = GROWW_DERIVATIVE_HISTORY_FLOOR
    start = _parse_iso(from_date, "from_date") if from_date else floor
    last = _parse_iso(to_date, "to_date") if to_date else today - timedelta(days=1)
    start = max(start, floor)
    last = min(last, today)
    if last < start:
        raise ValueError(
            f"nothing to pull: resolved range [{start.isoformat()}, {last.isoformat()}] is empty"
        )
    return start, last + timedelta(days=1)


def _years_for(start: date, end_exclusive: date) -> list[int]:
    """Inclusive list of calendar years touched by ``[start, end)`` (for expiry enum)."""
    return list(range(start.year, (end_exclusive - timedelta(days=1)).year + 1))


@dataclass(frozen=True, slots=True)
class DerivativesPullReport:
    """The honest outcome of one bulk derivatives pull (display + audit)."""

    dry_run: bool
    start_date: date
    end_date: date
    intervals: tuple[str, ...]
    contract_types: tuple[str, ...]
    underlyings: tuple[str, ...]
    bars_appended: int
    windows: int
    failed_pairs: int
    auth_aborted: bool
    note: str

    def to_dict(self) -> dict[str, object]:
        """JSON-safe view for the ``pull-derivatives`` route + the live panel."""
        return {
            "dry_run": self.dry_run,
            "start_date": self.start_date.isoformat(),
            "end_date": self.end_date.isoformat(),
            "intervals": list(self.intervals),
            "contract_types": list(self.contract_types),
            "underlyings": list(self.underlyings),
            "bars_appended": self.bars_appended,
            "windows": self.windows,
            "failed_pairs": self.failed_pairs,
            "auth_aborted": self.auth_aborted,
            "note": self.note,
        }


class _EmptyChainSDK:
    """An honest dry-run SDK: the F&O chain is unknown without creds, so it is EMPTY.

    Refuses to fabricate strikes/expiries (Golden Rule #1). Satisfies the
    ``GrowwSDK`` Protocol (the three methods the ingest calls) so the enumerate →
    ingest path runs offline and yields zero contracts — never an invented one.
    """

    def get_expiries(self, **_kwargs: object) -> Mapping[str, object]:
        """No expiries without live creds."""
        return {"expiries": []}

    def get_contracts(self, **_kwargs: object) -> Mapping[str, object]:
        """No contracts without live creds."""
        return {"contracts": []}

    def get_historical_candles(self, **_kwargs: object) -> Mapping[str, object]:
        """No candles without live creds (never reached with an empty chain)."""
        return {"candles": []}


def _build_dry_run_derivative_client(_lake_root: Path) -> tuple[GrowwClient, BarSink]:
    """Honest dry-run pair: an empty-chain client + a :class:`NullBarSink`."""
    return GrowwClient(sdk=_EmptyChainSDK()), NullBarSink()


def _build_live_derivative_client(  # pragma: no cover - live Groww TOTP token + SDK + network
    lake_root: Path,
    *,
    pace: Callable[[], None] | None = None,
    on_rate_limit: Callable[[], None] | None = None,
    on_success: Callable[[], None] | None = None,
) -> tuple[GrowwClient, BarSink]:
    """Build the LIVE ``(GrowwClient, ParquetBarSink)`` from stored TOTP credentials.

    LIVE ONLY (network + TOTP); tests inject a fake ``client_factory``. Mirrors
    ``pull_controller._build_live_spot_adapter`` — same creds chain, same lake sink.
    The rate-controller hooks are passed into the client so EVERY chain call
    (expiries / contracts / candles) is paced + retried + decrement/recover at the
    one choke point (DEC-GROWW-RATELIMIT-RETRY-001).
    """
    from brutex.adapters.groww.auth import GrowwCredentials
    from brutex.adapters.groww.live import build_live_client
    from brutex.config.credentials import resolve_groww_file_credentials
    from brutex.data.parquet_writer import ParquetWriter
    from brutex.data.sink import ParquetBarSink

    file_creds = resolve_groww_file_credentials()
    creds = GrowwCredentials(api_key=file_creds.api_key, totp_secret=file_creds.totp_secret)
    client = build_live_client(
        creds, pace=pace, on_rate_limit=on_rate_limit, on_success=on_success
    )
    sink = ParquetBarSink(writer=ParquetWriter(lake_root=lake_root))
    return client, sink


def _resolve_client_factory(
    *,
    dry_run: bool,
    pace: Callable[[], None] | None,
    on_rate_limit: Callable[[], None] | None,
    on_success: Callable[[], None] | None,
) -> DerivativeClientFactory:
    """Pick the default ``(lake_root) -> (client, sink)`` factory, wiring the live limiter.

    Dry-run → the honest empty-chain pair. Live → a factory that injects the shared
    rate controller's pace / decrement-on-429 / recover-on-success hooks into the
    :class:`GrowwClient`, so the chain pull can NEVER bypass the limiter
    (DEC-GROWW-RATELIMIT-RETRY-001). The returned factory keeps the
    ``Callable[[Path], ...]`` contract so an injected test factory is unaffected.
    """
    if dry_run:
        return _build_dry_run_derivative_client

    def _live(lake_root: Path) -> tuple[GrowwClient, BarSink]:  # pragma: no cover - live network
        return _build_live_derivative_client(
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
    persistent controller's hooks, so the 500/min governor spans the whole pull.
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


def _make_progress(
    on_progress: ProgressHook | None, flush: Callable[[], None] | None
) -> ProgressHook:
    """Build the per-window progress callback: forward to *on_progress* + flush the ledger.

    Either side is optional (``None`` → skipped), so the four combinations are all
    valid; extracted to a module function so every branch is directly testable.
    """

    def _progress(label: str, bars: int, failed: bool) -> None:
        if on_progress is not None:
            on_progress(label, bars, failed)
        if flush is not None:
            flush()

    return _progress


def pull_all_derivatives(
    instruments: Sequence[Instrument],
    *,
    lake_root: Path,
    dry_run: bool,
    contract_types: Sequence[str],
    intervals: Sequence[str],
    from_date: str | None = None,
    to_date: str | None = None,
    now: datetime | None = None,
    client_factory: DerivativeClientFactory | None = None,
    on_progress: ProgressHook | None = None,
    session_rate: SessionRateController | None = None,
) -> DerivativesPullReport:
    """Pull the FULL F&O chain (all expiries × all strikes) for *instruments*.

    For every underlying × every requested ``contract_types`` kind, enumerates the
    chain across the resolved year span and ingests each contract's *intervals*
    candles into the lake via the verified per-contract spine — the WindowLedger
    makes every window rerun-safe (idempotent). Stops early on a dead auth token
    (re-pulling is futile). Reuses the shared ledger lifecycle + the session rate
    controller; tests inject ``client_factory`` for a 100%-offline run.

    Parameters
    ----------
    instruments
        The underlyings whose chains to pull (indices and/or F&O-eligible stocks).
    lake_root
        The lake root the sink writes under.
    dry_run
        When ``True``, the default factory uses the honest empty-chain client (no
        fabricated chain) + a :class:`NullBarSink`.
    contract_types
        Ordered kinds — any of ``"option"`` / ``"future"`` (see
        :func:`resolve_contract_types`).
    intervals
        Ordered intervals — ``1day`` before ``1minute`` for a ``both`` pull (see
        :func:`resolve_pull_intervals`).
    from_date, to_date
        Optional ISO ``YYYY-MM-DD`` bounds; default ``[2020-01-01, previous day]``.
    now
        Injected clock (defaults to ``datetime.now(UTC)``).
    client_factory
        Injected ``(lake_root) -> (client, sink)`` (tests pass a fake); ``None``
        selects the dry-run / live default.
    on_progress
        Per ``(contract, interval)`` progress hook ``(label, bars, failed)``.
    session_rate
        The dashboard's ``SessionRateController`` (live pacing); ``None`` offline.
    """
    now = now or datetime.now(UTC)
    start, end = resolve_derivative_range(from_date=from_date, to_date=to_date, now=now)
    years = _years_for(start, end)
    pace, on_rate_limit, on_timeout, on_success = _rate_hooks(session_rate, dry_run=dry_run)
    if client_factory is None:
        client_factory = _resolve_client_factory(
            dry_run=dry_run, pace=pace, on_rate_limit=on_rate_limit, on_success=on_success
        )
    client, sink = client_factory(lake_root)
    ledger: WindowLedger | None = begin_pull_ledger(lake_root, dry_run=dry_run)
    flush = make_live_ledger_flush(ledger, lake_root) if ledger is not None else None
    progress = _make_progress(on_progress, flush)

    bars_appended = 0
    windows = 0
    failed_pairs = 0
    auth_aborted = False
    try:
        for instrument in instruments:
            exchange = str(instrument.exch_id)
            for kind in contract_types:
                # The wrappers set their own ledger namespace internally —
                # ``ingest_underlying_options`` → ``groww-option``,
                # ``ingest_underlying_futures`` → ``groww-future`` — so option and
                # future windows of the same expiry never collide.
                ingest = (
                    ingest_underlying_options if kind == OPTION_KIND else ingest_underlying_futures
                )
                report: GrowwOptionIngestReport = ingest(
                    client,
                    sink,
                    exchange=exchange,
                    underlying_symbol=instrument.symbol_name,
                    years=years,
                    intervals=intervals,
                    start=start,
                    end=end,
                    pace=pace,
                    ledger=ledger,
                    on_rate_limit=on_rate_limit,
                    on_timeout=on_timeout,
                    on_success=on_success,
                    on_progress=progress,
                )
                bars_appended += report.bars_appended
                windows += report.windows
                failed_pairs += report.failed_pairs
                auth_aborted = auth_aborted or report.auth_aborted
                if auth_aborted:
                    break
            if auth_aborted:
                break
    finally:
        end_pull_ledger(ledger, lake_root, dry_run=dry_run)

    note = _build_note(
        dry_run=dry_run, auth_aborted=auth_aborted, windows=windows, failed_pairs=failed_pairs
    )
    return DerivativesPullReport(
        dry_run=dry_run,
        start_date=start,
        end_date=end,
        intervals=tuple(intervals),
        contract_types=tuple(contract_types),
        underlyings=tuple(instrument.symbol_name for instrument in instruments),
        bars_appended=bars_appended,
        windows=windows,
        failed_pairs=failed_pairs,
        auth_aborted=auth_aborted,
        note=note,
    )


def _build_note(*, dry_run: bool, auth_aborted: bool, windows: int, failed_pairs: int) -> str:
    """Honest one-line outcome note for the report / live panel."""
    if dry_run:
        return (
            "dry-run: set Groww credentials to enumerate + pull the live F&O chain "
            "(the chain's strikes/expiries are real vendor data, never fabricated offline)"
        )
    if auth_aborted:
        return "auth token expired mid-pull — re-set Groww credentials and re-pull (idempotent)"
    if failed_pairs:
        return (
            f"pulled {windows} windows; {failed_pairs} contract/interval pair(s) quarantined — "
            "re-pull to retry the deferred windows (WindowLedger skips the done ones)"
        )
    return f"pulled {windows} windows across the chain (rerun-safe — WindowLedger dedups)"
