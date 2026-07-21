"""RollingOptionAdapter — the Dhan rolling-ATM option fetch surface.

DEC-DHAN-ROLLING-OPTIONS-001 (ADR-0022). Fetches the Dhan **rolling-ATM** option
series via the SDK ``expired_options_data`` (``POST /v2/charts/rollingoption``). The
adapter assembles the VERBATIM request body, tiles the range into 30-day windows
(:func:`~brutex.adapters.dhan.rolling_option_chunks.plan_rolling_option_windows`),
fetches each via an injected :class:`RollingOptionClientProtocol`, and parses the
parallel-column payload
(:func:`~brutex.adapters.dhan.rolling_option_parser.parse_rolling_option_columns`).

**Body shape (SETTLED honesty facts).** The VERBATIM body is
``{securityId, exchangeSegment, instrument, expiryFlag, expiryCode, strike,
drvOptionType, requiredData, fromDate, toDate, interval}`` — the SDK does NOT put
``dhanClientId`` in the body (``client-id`` travels only in the header). ``instrument``
is :data:`~brutex.adapters.dhan.rolling_option_constants.DHAN_ROLLING_INSTRUMENT`
(``"INDEX"``); the alternate ``OPTIDX`` is UNVERIFIED and behind a disambiguation
probe — the adapter always requests ``INDEX`` and LOUD-skips on a reject, NEVER a
silent switch (§7 gate). Enums are validated (fail-loud — Golden Rule #1).

**Honest deviation from the COMMON ``MarketAdapter`` waist (Golden Rule #10).** The
spot :class:`~brutex.adapters.dhan.adapter.DhanMarketAdapter` returns
:class:`~brutex.data.models.CandleBar`; a rolling-ATM row carries DRIFTING per-bar
resolved metadata (``resolved_strike`` / ``resolved_expiry_date``) that the
``CandleBar``-typed ``fetch_ohlcv`` waist structurally cannot carry, so this adapter
exposes :meth:`fetch_rolling_option` (returning
:class:`~brutex.adapters.dhan.rolling_option_parser.RollingOptionBar`) mirroring the
spot adapter's STRUCTURE, not its literal signature.

**Injected client seam (CORE scope).** The concrete client that drives the SDK
through the shared bounded-retry / DH-901 re-read funnel (reusing
:class:`~brutex.adapters.dhan.client.DhanClient`) is wired with the surface work; the
CORE builds + tests the adapter against a fake :class:`RollingOptionClientProtocol`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Final, Protocol

from brutex.adapters.dhan.constants import DHAN_RATE_LIMIT_RPS, NATIVE_TIMEZONE
from brutex.adapters.dhan.rolling_option_chunks import plan_rolling_option_windows
from brutex.adapters.dhan.rolling_option_constants import (
    DHAN_ROLLING_EXPIRY_CODES,
    DHAN_ROLLING_EXPIRY_FLAGS,
    DHAN_ROLLING_FETCH_INTERVAL,
    DHAN_ROLLING_HISTORY_FLOOR_REQUESTED,
    DHAN_ROLLING_INSTRUMENT,
    DHAN_ROLLING_INTERVAL,
    DHAN_ROLLING_OPTION_TYPES,
    DHAN_ROLLING_REQUIRED_DATA,
)
from brutex.adapters.dhan.rolling_option_parser import (
    DeriveExpiry,
    RollingOptionBar,
    parse_rolling_option_columns,
)
from brutex.adapters.dhan.rolling_option_plan import RollingOptionKey
from brutex.adapters.protocol import MarketCapabilities
from brutex.models import Exchange, OutOfScopeError

#: The capabilities of the Dhan rolling-ATM option adapter. ``earliest_history`` is
#: the operator's REQUESTED reachable-today start (recorded, non-load-bearing — the
#: honest history floor is ``None``/UNVERIFIED and drives planning, never this field).
DHAN_ROLLING_CAPABILITIES: Final[MarketCapabilities] = MarketCapabilities(
    market_id="dhan-rolling-options",
    display_name="Dhan (NSE/BSE rolling-ATM index options)",
    exchanges=frozenset({Exchange.NSE, Exchange.BSE}),
    has_derivatives=True,
    has_expiries=False,  # NO fixed-expiry contracts — an ATM-relative rolling series
    has_option_chains=False,  # NO chain — rolling-ATM selectors, not a CE/PE strike grid
    supports_intraday=True,
    earliest_history=DHAN_ROLLING_HISTORY_FLOOR_REQUESTED,
    native_timezone=NATIVE_TIMEZONE,
    rate_limit_rps=DHAN_RATE_LIMIT_RPS,
    supported_intervals=frozenset({DHAN_ROLLING_FETCH_INTERVAL}),
)


class RollingOptionClientProtocol(Protocol):
    """The client surface :class:`RollingOptionAdapter` uses (one window fetch).

    The concrete production client reuses the shared
    :class:`~brutex.adapters.dhan.client.DhanClient` bounded-retry / DH-901 re-read
    funnel; a fake satisfies it in tests with no live API.
    """

    def rolling_option_data(self, *, body: Mapping[str, object]) -> Mapping[str, object]:
        """Fetch one rolling-option window; return the parallel-column ``data`` payload."""
        ...


def build_rolling_option_body(
    *,
    key: RollingOptionKey,
    from_date: str,
    to_date: str,
    instrument: str = DHAN_ROLLING_INSTRUMENT,
    required_data: tuple[str, ...] = DHAN_ROLLING_REQUIRED_DATA,
    interval: int = DHAN_ROLLING_INTERVAL,
) -> dict[str, object]:
    """Assemble the VERBATIM ``POST /v2/charts/rollingoption`` request body.

    Body keys VERBATIM (camelCase): ``securityId, exchangeSegment, instrument,
    expiryFlag, expiryCode, strike, drvOptionType, requiredData, fromDate, toDate,
    interval``. **NO ``dhanClientId``** — ``client-id`` travels only in the header
    (SETTLED). Enums are validated fail-loud (Golden Rule #1). ``strike`` is the
    ATM-relative selector LABEL (e.g. ``"ATM+1"``).

    Raises
    ------
    ValueError
        An out-of-set ``expiryFlag`` / ``expiryCode`` / ``drvOptionType`` / empty
        ``instrument`` (never a fabricated selector).
    """
    if key.expiry_flag not in DHAN_ROLLING_EXPIRY_FLAGS:
        raise ValueError(
            f"expiryFlag must be one of {sorted(DHAN_ROLLING_EXPIRY_FLAGS)}, "
            f"got {key.expiry_flag!r}"
        )
    if key.expiry_code not in DHAN_ROLLING_EXPIRY_CODES:
        raise ValueError(
            f"expiryCode must be one of {list(DHAN_ROLLING_EXPIRY_CODES)}, got {key.expiry_code!r}"
        )
    if key.option_type not in DHAN_ROLLING_OPTION_TYPES:
        raise ValueError(
            f"drvOptionType must be one of {sorted(DHAN_ROLLING_OPTION_TYPES)}, "
            f"got {key.option_type!r}"
        )
    if not instrument:
        raise ValueError("instrument must be a non-empty token")
    return {
        "securityId": key.security_id,
        "exchangeSegment": key.exchange_segment,
        "instrument": instrument,
        "expiryFlag": key.expiry_flag,
        "expiryCode": key.expiry_code,
        "strike": key.strike_offset,
        "drvOptionType": key.option_type,
        "requiredData": list(required_data),
        "fromDate": from_date,
        "toDate": to_date,
        "interval": interval,
    }


@dataclass(frozen=True, slots=True)
class RollingOptionAdapter:
    """Fetch surface over an injected rolling-option client + an expiry deriver seam.

    Attributes
    ----------
    client
        The injected :class:`RollingOptionClientProtocol` (production: a
        :class:`~brutex.adapters.dhan.client.DhanClient`-backed client; tests: a fake).
    derive_expiry
        Optional NSE-calendar expiry deriver (§7 OPEN — the CORE ships none, so
        absent a vendor echo the row's ``resolved_expiry_source`` is ``unresolved``,
        never a fabricated date).
    instrument
        The requested ``instrument`` token (default ``INDEX``; the ``OPTIDX``
        alternate is behind a probe — never auto-switched here).
    required_data
        The ``requiredData`` payload list (default the full rolling set).
    """

    client: RollingOptionClientProtocol
    derive_expiry: DeriveExpiry | None = None
    instrument: str = DHAN_ROLLING_INSTRUMENT
    required_data: tuple[str, ...] = field(default=DHAN_ROLLING_REQUIRED_DATA)

    @property
    def capabilities(self) -> MarketCapabilities:
        """Stable Dhan rolling-ATM option capabilities (read once by the orchestrator)."""
        return DHAN_ROLLING_CAPABILITIES

    def fetch_rolling_option(
        self, *, key: RollingOptionKey, start: date, end: date
    ) -> list[RollingOptionBar]:
        """Fetch 1-minute rolling-ATM option bars for *key* over half-open ``[start, end)``.

        Tiles the range into 30-day windows, fetches each via the injected client with
        the VERBATIM body, and concatenates the parsed
        :class:`~brutex.adapters.dhan.rolling_option_parser.RollingOptionBar` rows. The
        range is HALF-OPEN ``[start, end)`` (the ``plan_windows`` convention; a caller
        with an inclusive operator range passes ``end + 1 day``).

        A *key* carrying no ``securityId`` (``<= 0``) is out of scope
        (:class:`~brutex.models.OutOfScopeError` — defensive; the enumerator only
        emits resolved ids, so this never fires in a healthy pull, Golden Rule #1).
        """
        if key.security_id <= 0:
            raise OutOfScopeError(
                f"rolling-option key has no resolvable securityId: {key.underlying!r}"
            )
        windows = plan_rolling_option_windows(
            datetime.combine(start, time.min),
            datetime.combine(end, time.min),
            interval=DHAN_ROLLING_FETCH_INTERVAL,
        )
        bars: list[RollingOptionBar] = []
        for window in windows:
            body = build_rolling_option_body(
                key=key,
                from_date=window.start[:10],  # ISO "YYYY-MM-DDTHH:MM:SS" → "YYYY-MM-DD"
                to_date=window.end[:10],
                instrument=self.instrument,
                required_data=self.required_data,
            )
            payload = self.client.rolling_option_data(body=body)
            bars.extend(
                parse_rolling_option_columns(
                    payload,
                    expiry_flag=key.expiry_flag,
                    expiry_code=key.expiry_code,
                    derive_expiry=self.derive_expiry,
                )
            )
        return bars


__all__ = [
    "DHAN_ROLLING_CAPABILITIES",
    "RollingOptionAdapter",
    "RollingOptionClientProtocol",
    "build_rolling_option_body",
]
