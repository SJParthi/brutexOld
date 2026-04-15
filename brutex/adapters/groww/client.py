"""Groww broker adapter — the L1 bridge wrapping ``growwapi.GrowwAPI``.

This is the ONLY file in BruteX that lazy-imports ``growwapi`` at
runtime. Every other layer talks to this adapter via the
:class:`GrowwClientProtocol` interface, so unit tests can inject a
``FakeGrowwClient`` without needing the real SDK installed.

## Composition

:class:`GrowwAdapter` is a **facade** that glues together:

* :class:`~brutex.orchestrator.rate_limiter.AdaptiveTokenBucket` — 15
  req/s default with auto-downshift on 429 storms (FR-ING-017)
* :class:`~brutex.orchestrator.store.WorkItemStore` — LMDB-backed O(1)
  dedup + persistence (FR-BF-004, NFR-PERF-002)
* :class:`~brutex.orchestrator.audit.AuditWriter` — hash-chained
  tamper-evident log (NFR-OBS-003)
* :mod:`brutex.orchestrator.retry` — Groww exception → Outcome
  classifier (FR-ING-008 refined)
* :mod:`brutex.orchestrator.state` — FSM transitions (FR-ING-014)

Every SDK call funnels through the same private
:meth:`_execute_call` helper which runs the full pipeline: dedup
probe → rate-limit acquire → dispatch → SDK call → classify →
transition → persist → audit. **No SDK call goes anywhere else.**

## Never-skip guarantee

Per ``FR-ING-015``, the adapter NEVER silently drops a call. Every
path ends with the work item in exactly one of ``SUCCESS``,
``NO_DATA``, ``RETRYING``, or ``QUARANTINED`` — never in a "lost"
state. The worker loop that owns the caller is responsible for
handling ``RETRYING`` (sleep + re-dispatch via the FSM).

## Thread safety

The adapter is **safe to share across read-only worker threads** but
writers must serialize through the caller. In the BruteX design, a
single supervisor actor owns the adapter; workers send work items
via a queue, not by direct method calls.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Final, Protocol, runtime_checkable

from brutex.orchestrator.audit import AuditWriter
from brutex.orchestrator.rate_limiter import AdaptiveTokenBucket, RateLimiterEvent
from brutex.orchestrator.retry import (
    UnknownExceptionError,
    classify_exception,
    outcome_severity,
)
from brutex.orchestrator.state import (
    Outcome,
    WorkItem,
    WorkItemState,
    dispatch,
    transition,
)
from brutex.orchestrator.store import WorkItemStore

if TYPE_CHECKING:
    # Type-only imports — growwapi is NEVER imported at runtime by this
    # module unless GrowwAdapter.from_token() is called.
    pass


# ────────────────────────────────────────────────────────────────────────
# Protocol — the minimal shape BruteX needs from a Groww-like client
# ────────────────────────────────────────────────────────────────────────


@runtime_checkable
class GrowwClientProtocol(Protocol):
    """Minimal interface BruteX needs from ``growwapi.GrowwAPI``.

    Real implementation: an instance of ``growwapi.GrowwAPI``.
    Test implementation: :class:`FakeGrowwClient` (in the test file).

    Using ``@runtime_checkable`` lets us ``isinstance()``-check at
    adapter construction time for helpful error messages. It does NOT
    verify the method signatures — that would require introspection
    we don't want at hot-path speed. Signatures are enforced by
    static type checking (mypy --strict in CI).
    """

    def get_all_instruments(self) -> Any:  # returns pandas.DataFrame
        """Return the full instrument master DataFrame from the Groww SDK."""
        ...

    def get_instrument_by_groww_symbol(self, groww_symbol: str) -> dict[str, Any]:
        """Return one instrument record by groww_symbol via the SDK."""
        ...

    def get_expiries(
        self,
        *,
        exchange: str,
        underlying_symbol: str,
        year: int | None = None,
        month: int | None = None,
    ) -> dict[str, list[str]]:
        """Return the expiry dates for ``(exchange, underlying)`` from the SDK."""
        ...

    def get_contracts(
        self,
        *,
        exchange: str,
        underlying_symbol: str,
        expiry_date: str,
    ) -> dict[str, list[str]]:
        """Return contracts for ``(exchange, underlying, expiry)`` from the SDK."""
        ...

    def get_historical_candles(
        self,
        *,
        exchange: str,
        segment: str,
        groww_symbol: str,
        start_time: str,
        end_time: str,
        candle_interval: str,
    ) -> dict[str, Any]:
        """Return historical OHLCV candles for one symbol+interval slice."""
        ...


# ────────────────────────────────────────────────────────────────────────
# Response → Outcome shape detectors
# ────────────────────────────────────────────────────────────────────────

#: Default: treat any response as non-empty if it's truthy.
#: Method-specific detectors below handle the real shapes.
_MethodResponseChecker = Callable[[Any], bool]


def _is_dataframe_empty(response: Any) -> bool:
    """pandas.DataFrame-like check using duck-typed ``.empty`` attr."""
    if response is None:
        return True
    empty_attr = getattr(response, "empty", None)
    if empty_attr is not None:
        return bool(empty_attr)
    try:
        return len(response) == 0
    except TypeError:
        return False


def _is_expiries_response_empty(response: Any) -> bool:
    """``{"expiries": [...]}`` is empty if the list is missing or empty."""
    if not isinstance(response, dict):
        return True
    expiries = response.get("expiries")
    return not expiries  # None or [] → True


def _is_contracts_response_empty(response: Any) -> bool:
    """``{"contracts": [...]}`` — same shape rule."""
    if not isinstance(response, dict):
        return True
    contracts = response.get("contracts")
    return not contracts


def _is_candles_response_empty(response: Any) -> bool:
    """``{"candles": [...], ...}`` — same shape rule."""
    if not isinstance(response, dict):
        return True
    candles = response.get("candles")
    return not candles


# ────────────────────────────────────────────────────────────────────────
# Action names written to the audit chain
# ────────────────────────────────────────────────────────────────────────

#: Audit action names — stable strings written to every audit entry.
#: Dashboard + log queries key off these.
_ACTION_START: Final[str] = "GROWW_CALL_START"
_ACTION_SUCCESS: Final[str] = "GROWW_CALL_SUCCESS"
_ACTION_EMPTY: Final[str] = "GROWW_CALL_EMPTY"
_ACTION_RETRY: Final[str] = "GROWW_CALL_RETRY"
_ACTION_QUARANTINE: Final[str] = "GROWW_CALL_QUARANTINE"
_ACTION_DEDUP_SKIP: Final[str] = "GROWW_CALL_DEDUP_SKIP"


# ────────────────────────────────────────────────────────────────────────
# GrowwAdapter — the actual facade
# ────────────────────────────────────────────────────────────────────────


class GrowwAdapter:
    """Composes rate limiter + store + audit + retry around a
    :class:`GrowwClientProtocol` instance.

    Use :meth:`from_token` for the real-SDK factory, or call the
    constructor directly with a pre-built client + bucket + store +
    audit for tests.
    """

    def __init__(
        self,
        *,
        client: GrowwClientProtocol,
        bucket: AdaptiveTokenBucket,
        store: WorkItemStore,
        audit: AuditWriter,
        run_id: str,
        on_rate_limit_event: Callable[[RateLimiterEvent], None] | None = None,
    ) -> None:
        self._client = client
        self._bucket = bucket
        self._store = store
        self._audit = audit
        self._run_id = run_id
        self._on_rate_limit_event = on_rate_limit_event

    # ── Factories ──────────────────────────────────────────────────

    @classmethod
    def from_token(
        cls,
        *,
        token: str,
        bucket: AdaptiveTokenBucket,
        store: WorkItemStore,
        audit: AuditWriter,
        run_id: str,
        on_rate_limit_event: Callable[[RateLimiterEvent], None] | None = None,
    ) -> GrowwAdapter:
        """Factory that lazy-imports ``growwapi`` and builds a real
        :class:`GrowwAdapter`.

        Parameters
        ----------
        token
            The Groww API auth token (``GROWW_API_AUTH_TOKEN`` env var).
        bucket
            Pre-built :class:`AdaptiveTokenBucket` (caller owns it).
        store
            Pre-opened :class:`WorkItemStore`.
        audit
            Pre-opened :class:`AuditWriter`.
        run_id
            UUID of the current ingest run, stamped on every audit entry.
        on_rate_limit_event
            Optional callback fired on rate-limit events so the caller
            can publish to Telegram / dashboard. If None, events are
            still recorded to the audit log.

        Raises
        ------
        RuntimeError
            If ``growwapi`` is not installed in the environment.
        """
        try:
            # Lazy import — this is the ONLY place growwapi gets imported.
            from growwapi import GrowwAPI
        except ImportError as exc:
            raise RuntimeError(
                "growwapi package is not installed. "
                "Install with `pip install growwapi>=1.5` before using "
                "GrowwAdapter.from_token(). For unit tests, use the "
                "constructor directly with a FakeGrowwClient."
            ) from exc

        client = GrowwAPI(token)
        return cls(
            client=client,
            bucket=bucket,
            store=store,
            audit=audit,
            run_id=run_id,
            on_rate_limit_event=on_rate_limit_event,
        )

    # ── Public API — one method per Groww SDK call ────────────────

    def pull_all_instruments(self, *, work_item: WorkItem) -> tuple[WorkItem, Any]:
        """Phase A call — fetch the full instrument DataFrame.

        The work item's ``content_hash`` should encode the fact that
        this is a "full instrument snapshot for day X" — typically
        ``sha256(method_name + today_iso)``.

        Returns
        -------
        tuple[WorkItem, Any]
            Updated work item + raw pandas DataFrame (or None on
            non-SUCCESS paths).
        """
        return self._execute_call(
            work_item=work_item,
            method_name="get_all_instruments",
            call=self._client.get_all_instruments,
            is_empty=_is_dataframe_empty,
            response_summary=lambda r: {
                "rows": len(r) if r is not None else 0,
            },
        )

    def pull_expiries(
        self,
        *,
        work_item: WorkItem,
        exchange: str,
        underlying_symbol: str,
        year: int | None = None,
        month: int | None = None,
    ) -> tuple[WorkItem, Any]:
        """Phase B call — fetch expiry dates for one underlying+year.

        Returns
        -------
        tuple[WorkItem, Any]
            Updated work item + raw ``{"expiries": [...]}`` dict
            (or None on non-SUCCESS paths).
        """
        return self._execute_call(
            work_item=work_item,
            method_name="get_expiries",
            call=lambda: self._client.get_expiries(
                exchange=exchange,
                underlying_symbol=underlying_symbol,
                year=year,
                month=month,
            ),
            is_empty=_is_expiries_response_empty,
            response_summary=lambda r: {
                "expiries_count": len(r.get("expiries", [])) if isinstance(r, dict) else 0,
                "exchange": exchange,
                "underlying_symbol": underlying_symbol,
                "year": year,
                "month": month,
            },
        )

    def pull_contracts(
        self,
        *,
        work_item: WorkItem,
        exchange: str,
        underlying_symbol: str,
        expiry_date: str,
    ) -> tuple[WorkItem, Any]:
        """Phase C call — fetch contract symbols for one expiry.

        Returns
        -------
        tuple[WorkItem, Any]
            Updated work item + raw ``{"contracts": [...]}`` dict
            (or None on non-SUCCESS paths).
        """
        return self._execute_call(
            work_item=work_item,
            method_name="get_contracts",
            call=lambda: self._client.get_contracts(
                exchange=exchange,
                underlying_symbol=underlying_symbol,
                expiry_date=expiry_date,
            ),
            is_empty=_is_contracts_response_empty,
            response_summary=lambda r: {
                "contracts_count": len(r.get("contracts", [])) if isinstance(r, dict) else 0,
                "exchange": exchange,
                "underlying_symbol": underlying_symbol,
                "expiry_date": expiry_date,
            },
        )

    def pull_historical_candles(
        self,
        *,
        work_item: WorkItem,
        exchange: str,
        segment: str,
        groww_symbol: str,
        start_time: str,
        end_time: str,
        candle_interval: str,
    ) -> tuple[WorkItem, Any]:
        """Phase D call — fetch a candle chunk.

        Returns
        -------
        tuple[WorkItem, Any]
            Updated work item + raw candles dict
            (or None on non-SUCCESS paths).
        """
        return self._execute_call(
            work_item=work_item,
            method_name="get_historical_candles",
            call=lambda: self._client.get_historical_candles(
                exchange=exchange,
                segment=segment,
                groww_symbol=groww_symbol,
                start_time=start_time,
                end_time=end_time,
                candle_interval=candle_interval,
            ),
            is_empty=_is_candles_response_empty,
            response_summary=lambda r: {
                "candles_count": len(r.get("candles", [])) if isinstance(r, dict) else 0,
                "groww_symbol": groww_symbol,
                "start_time": start_time,
                "end_time": end_time,
                "candle_interval": candle_interval,
            },
        )

    # ── Core execution pipeline ────────────────────────────────────

    def _execute_call(
        self,
        *,
        work_item: WorkItem,
        method_name: str,
        call: Callable[[], Any],
        is_empty: _MethodResponseChecker,
        response_summary: Callable[[Any], dict[str, Any]],
    ) -> tuple[WorkItem, Any]:
        """Run the full pipeline for one Groww SDK call.

        Returns
        -------
        tuple
            ``(updated_work_item, raw_response_or_None)``.
            The response is None on exception paths. The caller's
            code is responsible for acting on the raw response when
            ``updated_work_item.state == SUCCESS``.

        Raises
        ------
        UnknownExceptionError
            If the SDK raises an exception type that the retry
            classifier does not recognise. Indicates a bug (the
            adapter should only catch Groww exceptions and these
            should all be in the classifier table).
        """
        # ── 1. Dedup probe — O(1) store lookup ───────────────────
        cached = self._store.get(work_item.phase, work_item.content_hash)
        if cached is not None and cached.state in {
            WorkItemState.SUCCESS,
            WorkItemState.NO_DATA,
        }:
            # Already done — skip the call entirely.
            self._audit.append(
                action=_ACTION_DEDUP_SKIP,
                subject=f"{method_name}:{work_item.item_id}",
                payload={
                    "run_id": self._run_id,
                    "method": method_name,
                    "cached_state": cached.state.value,
                    "content_hash": work_item.content_hash,
                },
            )
            return cached, None

        # Use the freshly-dispatched item OR the cached RETRYING item
        if cached is not None and cached.state == WorkItemState.QUARANTINED:
            # Quarantined and not resolved — skip and return.
            # Caller's phase_complete check will catch it.
            self._audit.append(
                action=_ACTION_DEDUP_SKIP,
                subject=f"{method_name}:{work_item.item_id}",
                payload={
                    "run_id": self._run_id,
                    "method": method_name,
                    "cached_state": cached.state.value,
                    "reason": (
                        cached.quarantine_reason.value if cached.quarantine_reason else "unknown"
                    ),
                },
            )
            return cached, None

        base_item = cached if cached is not None else work_item

        # ── 2. Rate-limit acquire — blocks until a token is free ──
        wait_seconds = self._bucket.acquire()

        # ── 3. Dispatch → IN_FLIGHT ──────────────────────────────
        in_flight = dispatch(base_item)
        self._store.put(in_flight)
        self._audit.append(
            action=_ACTION_START,
            subject=f"{method_name}:{in_flight.item_id}",
            payload={
                "run_id": self._run_id,
                "method": method_name,
                "content_hash": in_flight.content_hash,
                "retry_count": in_flight.retry_count,
                "rate_limit_wait_seconds": wait_seconds,
                "current_rps": self._bucket.current_rps(),
            },
        )

        # ── 4. Call the SDK (with monotonic timing) ──────────────
        t0 = time.monotonic()
        response: Any = None
        outcome: Outcome
        error_name: str | None = None
        error_message: str | None = None

        try:
            response = call()
        except Exception as exc:
            try:
                outcome = classify_exception(exc)
            except UnknownExceptionError:
                # Let unknown exceptions bubble up — they indicate a
                # bug in the adapter (should only catch Groww types).
                raise
            error_name = type(exc).__name__
            error_message = str(exc)[:500]  # cap length for audit sanity

            # Rate-limit hit event bookkeeping
            if outcome == Outcome.RATE_LIMIT:
                event = self._bucket.record_rate_limit_hit()
                if event is not None and self._on_rate_limit_event is not None:
                    self._on_rate_limit_event(event)
        else:
            # Success path — detect empty vs non-empty
            outcome = Outcome.OK_EMPTY if is_empty(response) else Outcome.OK_WITH_DATA
            self._bucket.record_success()

        elapsed_ms = (time.monotonic() - t0) * 1000.0

        # ── 5. Transition the FSM ────────────────────────────────
        new_item = transition(in_flight, outcome)
        self._store.put(new_item)

        # ── 6. Audit the terminal / retry state ──────────────────
        action = self._action_for_state(new_item.state)
        payload: dict[str, Any] = {
            "run_id": self._run_id,
            "method": method_name,
            "content_hash": new_item.content_hash,
            "state": new_item.state.value,
            "outcome": outcome.value,
            "severity": outcome_severity(outcome),
            "elapsed_ms": round(elapsed_ms, 3),
            "retry_count": new_item.retry_count,
        }
        if new_item.quarantine_reason is not None:
            payload["quarantine_reason"] = new_item.quarantine_reason.value
        if error_name is not None:
            payload["error_class"] = error_name
            payload["error_message"] = error_message
        if response is not None:
            try:
                payload["response_summary"] = response_summary(response)
            except Exception:
                payload["response_summary"] = {"_unparseable": True}

        self._audit.append(
            action=action,
            subject=f"{method_name}:{new_item.item_id}",
            payload=payload,
        )

        return new_item, response

    # ── Helpers ────────────────────────────────────────────────────

    @staticmethod
    def _action_for_state(state: WorkItemState) -> str:
        """Map a terminal / retrying state to its audit action name."""
        mapping = {
            WorkItemState.SUCCESS: _ACTION_SUCCESS,
            WorkItemState.NO_DATA: _ACTION_EMPTY,
            WorkItemState.RETRYING: _ACTION_RETRY,
            WorkItemState.QUARANTINED: _ACTION_QUARANTINE,
        }
        if state not in mapping:
            # Should be unreachable — transition() always moves to one
            # of the above states or raises.
            raise RuntimeError(
                f"Unexpected post-call state {state.value} — transition() invariant broken"
            )
        return mapping[state]


__all__ = [
    "GrowwAdapter",
    "GrowwClientProtocol",
]
