"""Dhan client — neutral wrapper over the ``dhanhq`` historical-data SDK.

DEC-DHAN-SPOT-PULL-001 (ADR-0022). Wraps the ``dhanhq`` read-path SDK
(``intraday_minute_data``) into ONE bounded-retry ``_call`` funnel that:

* inspects the SDK's FLAT response envelope (``{"status", "data" | "remarks"}``,
  VERIFIED-live: ``dhan_http.py``) — a ``"success"`` returns the ``data`` column
  dict; a ``"failure"`` classifies ``remarks`` into a neutral
  :class:`~brutex.adapters.dhan.errors.DhanError`;
* on a DH-901 (:class:`~brutex.adapters.dhan.errors.DhanAuthError`) drives a
  bounded READ-ONLY RE-READ via the injected
  :class:`~brutex.adapters.dhan.reauth.DhanSdkRefresher` (never a mint);
* fast-retries a transient failure (throttle / empty body / timeout) under a
  bounded budget, and fires the shared-controller hooks
  (``pace`` / ``on_rate_limit`` / ``on_success``).

The three ``client_self_*`` predicates return ``False`` for a :class:`DhanClient`
(:func:`client_self_paces` etc.): the Dhan client's hooks are DEFENCE-IN-DEPTH
no-ops by default, and the spot spine owns per-window pacing + rate feedback (so the
default feed binding leaves the client hooks unbound — no double-admission). All
boundary types are plain data / mappings, so a future PyO3 Rust core can satisfy the
same :class:`DhanClientProtocol` with zero binding change.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Protocol, cast

from brutex.adapters.dhan.constants import (
    DHAN_INTERVAL_1MIN,
    INSTRUMENT_INDEX,
    SEGMENT_INDEX,
)
from brutex.adapters.dhan.errors import (
    DhanAuthError,
    DhanError,
    DhanTokenStillStaleError,
    classify_dhan_failure,
    from_dhan_exception,
    is_rate_limit,
    is_retryable,
)
from brutex.adapters.dhan.reauth import DhanSdk, DhanSdkRefresher

#: Bounded in-call fast-retry budget for a transient failure (O(1)).
DHAN_MAX_ATTEMPTS: int = 6

#: Base linear back-off (seconds) between in-call retries.
DHAN_BACKOFF_SECONDS: float = 0.5

#: Bounded mid-call RE-READ budget for a DH-901 (one re-read heals a rotation).
DHAN_MAX_REAUTHS: int = 1


def _noop_pace() -> None:
    """Default no-op pace hook (the spine owns per-window admission)."""


def _noop_sleep(_seconds: float) -> None:
    """Default no-op sleeper (a test injects a real / recording one)."""


def _noop_hook() -> None:
    """Default no-op rate-feedback hook (the spine owns rate feedback)."""


def _classify_response(raw: Mapping[str, object]) -> DhanError | None:
    """Return the neutral error for a FAILURE envelope, or ``None`` on success.

    A ``status == "success"`` yields ``None``; anything else classifies the
    ``remarks`` sub-dict (missing / malformed ``remarks`` → an empty dict, which
    classifies to the terminal base :class:`DhanError`).
    """
    if str(raw.get("status", "")).strip().lower() == "success":
        return None
    remarks = raw.get("remarks")
    remarks_map: Mapping[str, object] = remarks if isinstance(remarks, Mapping) else {}
    return classify_dhan_failure(remarks_map)


def _extract_data(raw: Mapping[str, object]) -> Mapping[str, object]:
    """Return the ``data`` column dict of a SUCCESS envelope (empty when absent/wrong-shape)."""
    data = raw.get("data")
    return data if isinstance(data, Mapping) else {}


class DhanRollingSdk(Protocol):
    """The read-path ``dhanhq`` SDK surface for the rolling-ATM option fetch.

    Deliberately SEPARATE from :class:`~brutex.adapters.dhan.reauth.DhanSdk` (the
    spot ``intraday_minute_data`` surface) — the rolling-ATM track calls the SDK's
    ``expired_options_data`` (``POST /v2/charts/rollingoption``) instead. Keeping it a
    distinct Protocol leaves ``DhanSdk`` byte-untouched (the DEC-DHAN-SPOT-PULL-001
    client/reauth surface + its tests are unaffected), while :meth:`DhanClient.\
    rolling_option_data` funnels the rolling call through the SAME bounded-retry /
    DH-901 re-read :meth:`DhanClient._call`: the refresher's live SDK (the real
    ``dhanhq`` object) satisfies BOTH Protocols, so the one funnel casts to this
    surface at the rolling call site. Returns the SDK's flat response envelope
    (``{"status", "data" | "remarks"}``).
    """

    def expired_options_data(self, **kwargs: object) -> Mapping[str, object]:
        """Fetch one rolling-ATM option window (the flat response envelope).

        Kept ``**kwargs`` (not a pinned signature) because the VERBATIM camelCase
        request body is assembled upstream by
        :func:`~brutex.adapters.dhan.rolling_option_adapter.build_rolling_option_body`
        and handed through unchanged — the fake satisfies it in tests, the real SDK in
        production, with no shared import.
        """
        ...


class DhanDailySdk(Protocol):
    """The read-path ``dhanhq`` SDK surface for the NATIVE daily fetch.

    DEC-DHAN-NATIVE-DAILY-001 (implementing DEC-SCOPE-MINIMAL-CORE-001).
    Deliberately SEPARATE from :class:`~brutex.adapters.dhan.reauth.DhanSdk` (the
    spot ``intraday_minute_data`` surface) — the native-daily track calls the SDK's
    ``historical_daily_data`` (``POST /v2/charts/historical``; VERIFIED-SDK: the
    official ``dhanhq`` 2.2.0 wheel's ``_historical_data.py``). Keeping it a
    distinct Protocol leaves ``DhanSdk`` byte-untouched (the :class:`DhanRollingSdk`
    precedent), while :meth:`DhanClient.daily_candles` funnels the daily call
    through the SAME bounded-retry / DH-901 re-read :meth:`DhanClient._call`: the
    refresher's live SDK (the real ``dhanhq`` object) satisfies BOTH Protocols, so
    the one funnel casts to this surface at the daily call site. Returns the SDK's
    flat response envelope (``{"status", "data" | "remarks"}``).
    """

    def historical_daily_data(
        self,
        security_id: int,
        exchange_segment: str,
        instrument_type: str,
        from_date: str,
        to_date: str,
        expiry_code: int = ...,
        oi: bool = ...,
    ) -> Mapping[str, object]:
        """Fetch one daily-candle window (the flat response envelope).

        The signature mirrors the VERIFIED wheel's ``historical_daily_data``
        exactly; ``expiry_code`` is deliberately never passed by the client
        (the SDK default ``0`` — a spot index has no expiry).
        """
        ...


@dataclass(frozen=True, slots=True)
class DhanClient:
    """Neutral wrapper over an injected :class:`~brutex.adapters.dhan.reauth.DhanSdkRefresher`.

    Attributes
    ----------
    refresher
        Provides the live read-path SDK (``current_sdk``) + the read-only RE-READ on
        a DH-901 (``reauth``). The refresher is the ONE token-lifecycle owner.
    pace, sleep, on_rate_limit, on_success
        Defence-in-depth hooks — no-ops by default (the spot spine owns pacing +
        rate feedback; the default binding leaves these unbound, so there is no
        double-admission — :func:`client_self_paces` reads ``False``).
    max_attempts, backoff_seconds, max_reauths
        The bounded in-call retry / re-read budgets.
    """

    refresher: DhanSdkRefresher
    pace: Callable[[], None] = _noop_pace
    sleep: Callable[[float], None] = field(default=_noop_sleep)
    on_rate_limit: Callable[[], None] = field(default=_noop_hook)
    on_success: Callable[[], None] = field(default=_noop_hook)
    max_attempts: int = DHAN_MAX_ATTEMPTS
    backoff_seconds: float = DHAN_BACKOFF_SECONDS
    max_reauths: int = DHAN_MAX_REAUTHS

    def intraday_candles(
        self,
        *,
        security_id: int,
        from_date: str,
        to_date: str,
        exchange_segment: str = SEGMENT_INDEX,
        instrument: str = INSTRUMENT_INDEX,
        interval: int = DHAN_INTERVAL_1MIN,
        oi: bool = False,
    ) -> Mapping[str, object]:
        """Fetch one 1-minute intraday window (the parallel-column ``data`` payload).

        Calls ``intraday_minute_data`` on the live SDK; returns the success ``data``
        column dict (empty on a success with no columns — an honest no-data span).
        """
        return self._call(
            lambda sdk: sdk.intraday_minute_data(
                security_id=security_id,
                exchange_segment=exchange_segment,
                instrument_type=instrument,
                from_date=from_date,
                to_date=to_date,
                interval=interval,
                oi=oi,
            )
        )

    def daily_candles(
        self,
        *,
        security_id: int,
        from_date: str,
        to_date: str,
        exchange_segment: str = SEGMENT_INDEX,
        instrument: str = INSTRUMENT_INDEX,
    ) -> Mapping[str, object]:
        """Fetch one NATIVE daily-candle window (the parallel-column ``data`` payload).

        DEC-DHAN-NATIVE-DAILY-001: the daily twin of :meth:`intraday_candles` —
        drives the SDK's ``historical_daily_data`` (``POST /v2/charts/historical``)
        through the SAME bounded-retry / DH-901 re-read :meth:`_call` funnel (same
        error classification via ``errors.py`` over both remark spellings, same
        terminal-stop propagation, same ``pace``/``on_rate_limit``/``on_success``
        hooks) — zero duplication. The live SDK satisfies :class:`DhanDailySdk`,
        so the funnel casts to that surface at the call site (``DhanSdk`` is
        deliberately left untouched — the :class:`DhanRollingSdk` precedent).
        ``expiry_code`` is never passed (SDK default ``0`` — a spot index has no
        expiry); ``oi=False`` always (a spot index carries no open interest).
        Returns the success ``data`` column dict (empty on a success with no
        columns — an honest no-data span), or raises the classified terminal
        :class:`~brutex.adapters.dhan.errors.DhanError`.
        """
        return self._call(
            lambda sdk: cast("DhanDailySdk", sdk).historical_daily_data(
                security_id=security_id,
                exchange_segment=exchange_segment,
                instrument_type=instrument,
                from_date=from_date,
                to_date=to_date,
                oi=False,
            )
        )

    def rolling_option_data(self, *, body: Mapping[str, object]) -> Mapping[str, object]:
        """Fetch one rolling-ATM option window (the parallel-column ``data`` payload).

        The rolling-ATM twin of :meth:`intraday_candles`: it drives the SDK's
        ``expired_options_data`` (``POST /v2/charts/rollingoption``) through the SAME
        bounded-retry / DH-901 re-read :meth:`_call` funnel, so the rolling track reuses
        the whole DEC-DHAN-SPOT-PULL-001 retry/reauth machinery with ZERO duplication.
        *body* is the VERBATIM camelCase request body assembled by
        :func:`~brutex.adapters.dhan.rolling_option_adapter.build_rolling_option_body`
        (no ``dhanClientId`` — the ``client-id`` travels only in the header, SETTLED);
        it is handed to the live SDK unchanged. The live SDK (the real ``dhanhq``
        object) satisfies :class:`DhanRollingSdk`, so the funnel casts to that surface
        at the call site (``DhanSdk`` is deliberately left untouched). Returns the
        success ``data`` column dict (empty on a success with no columns — an honest
        no-data span), or raises the classified terminal
        :class:`~brutex.adapters.dhan.errors.DhanError`.

        This makes a :class:`DhanClient` a drop-in
        :class:`~brutex.adapters.dhan.rolling_option_adapter.RollingOptionClientProtocol`
        (the injected client the rolling adapter fetches through).
        """
        return self._call(lambda sdk: cast("DhanRollingSdk", sdk).expired_options_data(**body))

    def _call(self, invoke: Callable[[DhanSdk], object]) -> Mapping[str, object]:
        """Invoke *invoke* on the live SDK with bounded retry + DH-901 re-read.

        Raises the classified terminal :class:`DhanError` (after the bounded budget /
        on a non-retryable failure); a retryable failure is fast-retried, a DH-901 is
        healed by a bounded read-only re-read (:meth:`DhanSdkRefresher.reauth`), and a
        clean call fires ``on_success``.
        """
        reauths = 0
        generation = self.refresher.current_generation()
        last_error: DhanError = DhanError("no attempt made")
        for attempt in range(self.max_attempts):
            self.pace()
            try:
                raw = invoke(self.refresher.current_sdk())
            except Exception as exc:  # pylint: disable=broad-exception-caught — map any transport raise
                last_error = from_dhan_exception(exc)
            else:
                raw_map: Mapping[str, object] = raw if isinstance(raw, Mapping) else {}
                error = _classify_response(raw_map)
                if error is None:
                    self.on_success()
                    return _extract_data(raw_map)
                last_error = error
            # A DH-901 (a genuine auth rejection, NOT the retryable still-stale) drives a
            # bounded READ-ONLY re-read; `reauth` may raise a retryable stale / timeout,
            # which propagates so the spine re-pulls the window.
            if (
                isinstance(last_error, DhanAuthError)
                and not isinstance(last_error, DhanTokenStillStaleError)
                and reauths < self.max_reauths
            ):
                reauths += 1
                generation = self.refresher.reauth(generation)
                continue
            if is_rate_limit(last_error):
                self.on_rate_limit()
            if is_retryable(last_error) and attempt < self.max_attempts - 1:
                self.sleep(self.backoff_seconds * (attempt + 1))
                continue
            raise last_error
        raise last_error

    def with_rate_hooks(
        self,
        *,
        pace: Callable[[], None] | None = None,
        on_rate_limit: Callable[[], None] | None = None,
        on_success: Callable[[], None] | None = None,
    ) -> DhanClient:
        """Return a copy with the shared controller's hook triple rebound (O(1), immutable).

        The uniform seam mirroring :meth:`GrowwClient.with_rate_hooks`. A ``None`` hook
        keeps the current binding (never downgrades a bound hook back to the no-op).
        Frozen dataclass → :func:`~dataclasses.replace`. The default spot binding does
        NOT bind these (the spine paces per-window; :func:`client_self_paces` is
        ``False``), so client-side pacing is opt-in.
        """
        return replace(
            self,
            pace=pace if pace is not None else self.pace,
            on_rate_limit=on_rate_limit if on_rate_limit is not None else self.on_rate_limit,
            on_success=on_success if on_success is not None else self.on_success,
        )


def client_self_paces(client: object) -> bool:
    """Return ``False`` — a :class:`DhanClient` does NOT self-pace (the spine paces).

    Mirrors the Groww ``getattr(..., "self_paces", False)`` predicate; a
    :class:`DhanClient` exposes no truthy ``self_paces`` attribute, so the spot spine
    keeps the window-level pace as the single admission point (no double-admission).
    """
    return bool(getattr(client, "self_paces", False))


def client_self_reports_rate_limits(client: object) -> bool:
    """Return ``False`` — a :class:`DhanClient` does NOT self-report 429s (the spine does)."""
    return bool(getattr(client, "self_reports_rate_limits", False))


def client_self_reports_successes(client: object) -> bool:
    """Return ``False`` — a :class:`DhanClient` does NOT self-report successes (the spine does)."""
    return bool(getattr(client, "self_reports_successes", False))


class DhanClientProtocol(Protocol):
    """The client surface the :class:`~brutex.adapters.dhan.adapter.DhanMarketAdapter` uses."""

    def intraday_candles(
        self,
        *,
        security_id: int,
        from_date: str,
        to_date: str,
        exchange_segment: str = ...,
        instrument: str = ...,
        interval: int = ...,
        oi: bool = ...,
    ) -> Mapping[str, object]:
        """Fetch one 1-minute intraday window (the parallel-column ``data`` payload)."""
        ...


__all__ = [
    "DHAN_BACKOFF_SECONDS",
    "DHAN_MAX_ATTEMPTS",
    "DHAN_MAX_REAUTHS",
    "DhanClient",
    "DhanClientProtocol",
    "DhanDailySdk",
    "DhanRollingSdk",
    "client_self_paces",
    "client_self_reports_rate_limits",
    "client_self_reports_successes",
]
