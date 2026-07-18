"""Dhan capability matrix — a structured, honest verified-vs-unverified capability object.

DEC-DHAN-ADAPTER-001 + Golden Rule #10 (honest limits). A pure, I/O-free LEAF holding
the adapter's per-endpoint capability record as DATA (not prose), so the truth is
machine-checkable (a golden test pins it) and the ``_UNVERIFIED`` probe gates are
enumerable. Every ``verified`` flag traces to the DhanHQ v2 docs; every ``False`` is a
live-probe gate named in :data:`DHAN_UNVERIFIED_PROBE_GATES`.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class EndpointCapability:
    """One Dhan Data-API endpoint's honest capability record.

    Attributes
    ----------
    name
        Short capability id (e.g. ``"historical_daily"``).
    endpoint
        The REST path.
    verb
        HTTP verb.
    verified
        ``True`` iff the endpoint's SHAPE + limits are VERIFIED against the docs.
    coverage
        Human-readable coverage note (depth / granularity).
    limits
        Human-readable rate/range limits note.
    unverified
        The ``_UNVERIFIED`` aspects a live probe must close (empty when fully verified).
    """

    name: str
    endpoint: str
    verb: str
    verified: bool
    coverage: str
    limits: str
    unverified: tuple[str, ...] = field(default_factory=tuple)


#: The Dhan Data-API capability matrix — the honest per-endpoint record.
DHAN_CAPABILITY_MATRIX: tuple[EndpointCapability, ...] = (
    EndpointCapability(
        name="historical_daily",
        endpoint="/v2/charts/historical",
        verb="POST",
        verified=True,
        coverage="Daily OHLC from inception (Data-API subscription required)",
        limits="Data 5/s + 100,000/day",
        unverified=("daily max-range-per-call (no documented cap)",),
    ),
    EndpointCapability(
        name="intraday",
        endpoint="/v2/charts/intraday",
        verb="POST",
        verified=True,
        coverage="1/5/15/25/60-minute intraday candles (history DEPTH undocumented)",
        limits="Data 5/s + 100,000/day; 90 days per call",
        unverified=(
            "candle price unit (rupees-vs-paisa)",
            "intraday history depth (undocumented — the earliest-history probe gate)",
        ),
    ),
    EndpointCapability(
        name="option_chain",
        endpoint="/v2/optionchain",
        verb="POST",
        verified=True,
        coverage="Per-strike OI/Greeks/volume/top bid-ask/price snapshot",
        limits="SPECIAL 1 request / 3 seconds",
        unverified=("exact per-strike Greek field names",),
    ),
    EndpointCapability(
        name="expiry_list",
        endpoint="/v2/optionchain/expirylist",
        verb="POST",
        verified=True,
        coverage="Expiry dates for an underlying",
        limits="Data 5/s + 100,000/day",
        unverified=(),
    ),
    EndpointCapability(
        name="rolling_option",
        endpoint="/v2/charts/rollingoption",
        verb="POST",
        verified=True,
        coverage="Expired Index+Stock OPTIONS, minute-level, last 5 years, ATM-relative "
        "(≤10 strikes above/below ATM). Expired FUTURES history is UNVERIFIED-absent.",
        limits="Data 5/s + 100,000/day; 30 days per call",
        unverified=("expired-futures history existence (undocumented — no path provided)",),
    ),
    EndpointCapability(
        name="renew_token",
        endpoint="/v2/RenewToken",
        verb="POST",
        verified=True,
        coverage="Refresh an ACTIVE 24h token (renewing an EXPIRED token errors)",
        limits="Non-Trading 20/s",
        unverified=(),
    ),
    EndpointCapability(
        name="validate_token",
        endpoint="/v2/profile",
        verb="GET",
        verified=True,
        coverage="Validate the current token",
        limits="Non-Trading 20/s",
        unverified=(),
    ),
    EndpointCapability(
        name="scrip_master",
        endpoint="https://images.dhan.co/api-data/api-scrip-master.csv",
        verb="GET",
        verified=True,
        coverage="Instrument master CSV (compact + detailed)",
        limits="static file",
        unverified=("exact SEM_-prefixed column names + order",),
    ),
)

#: The full ``_UNVERIFIED`` probe-gate register — every aspect a live credentialed
#: probe must close (Golden Rule #1). Ordered, deduplicated, machine-enumerable;
#: mirrors ADR-0024 §4's EIGHT gates 1:1 (the round-1 review caught gate #8 —
#: the earliest-history floor — missing from this register while the ADR named it).
DHAN_UNVERIFIED_PROBE_GATES: tuple[str, ...] = (
    "candle price unit (rupees-vs-paisa)",
    "daily max-range-per-call (no documented cap)",
    "option-chain per-strike Greek field names",
    "exact scrip-master SEM_-prefixed column names + order",
    "DH-902 / DH-903 / DH-904 / DH-907 error-code meanings",
    "expired-futures history existence (undocumented)",
    "subscription + credential provisioning (₹499/mo Data-API add-on + SSM SecureStrings)",
    "earliest-history floor (daily inception + intraday depth — "
    "EARLIEST_HISTORY_UNVERIFIED placeholder)",
)


def verified_capabilities() -> tuple[EndpointCapability, ...]:
    """Return the fully-verified endpoints (no ``_UNVERIFIED`` aspects)."""
    return tuple(cap for cap in DHAN_CAPABILITY_MATRIX if not cap.unverified)


def unverified_capabilities() -> tuple[EndpointCapability, ...]:
    """Return the endpoints carrying at least one ``_UNVERIFIED`` aspect."""
    return tuple(cap for cap in DHAN_CAPABILITY_MATRIX if cap.unverified)


__all__ = [
    "DHAN_CAPABILITY_MATRIX",
    "DHAN_UNVERIFIED_PROBE_GATES",
    "EndpointCapability",
    "unverified_capabilities",
    "verified_capabilities",
]
