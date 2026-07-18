"""Dhan Data API v2 adapter — SECOND vendor, LAKE/reference-only, subscription-gated.

DEC-DHAN-ADAPTER-001. A fully-built but **INERT** DhanHQ v2 data-vendor adapter,
mirroring the Groww adapter shape 1:1: pure I/O-free law leaves (constants / errors /
chunks / parser / instruments / capability_matrix), a transport-based client + pool,
AWS-only credentials + read-only token store + single-flight refresher, option-chain
and expired-option (rolling) orchestration, and the market-neutral
:class:`~brutex.adapters.dhan.adapter.DhanMarketAdapter`.

**Scope (Golden Rule #10):** the engine is UNCHANGED (NIFTY/BANKNIFTY/SENSEX spot);
Dhan is LAKE / REFERENCE_ONLY and **GATED ON the ₹499/mo Data-API subscription +
credential provisioning** — no live API calls until the operator provisions the SSM
SecureStrings. Every ``_UNVERIFIED`` fact is a live-probe gate (Golden Rule #1).

**Python-first (not born-Rust) — a CONSCIOUS ADR-0020-grounded scoping** of the
standing born-Rust directive: a vendor adapter is I/O-bound (network + lake write
dominate; the parse/keying CPU is nanosecond-scale), so an FFI hop is a measured
slowdown and a born-Rust kernel has no retained Python oracle to burn-in against
("expected payoff ≈ 0 — conversion is for uniformity, not speed"). The pure law leaves
are import-light + I/O-free so an OPTIONAL ``dhan_*_rust.py`` facade can wrap each 1:1
LATER if a cross-repo uniformity mandate lands — the deferred follow-up
DEC-DHAN-RUST-FACADE-001 (never this PR).
"""

from __future__ import annotations

from brutex.adapters.dhan.adapter import DHAN_CAPABILITIES, DhanMarketAdapter
from brutex.adapters.dhan.capability_matrix import (
    DHAN_CAPABILITY_MATRIX,
    DHAN_UNVERIFIED_PROBE_GATES,
)
from brutex.adapters.dhan.client import DhanClient, DhanClientProtocol
from brutex.adapters.dhan.credentials import DhanCredentials, load_dhan_aws_credentials
from brutex.adapters.dhan.errors import DhanError, from_dhan_response, from_transport_exception
from brutex.adapters.dhan.instruments import DhanInstrumentIndex, parse_scrip_master
from brutex.adapters.dhan.pool import DhanClientPool
from brutex.adapters.dhan.reauth import DhanTokenRefresher
from brutex.adapters.dhan.token_store_aws import DhanParameterStoreTokenStore
from brutex.adapters.dhan.transport import DhanTransport

__all__ = [
    "DHAN_CAPABILITIES",
    "DHAN_CAPABILITY_MATRIX",
    "DHAN_UNVERIFIED_PROBE_GATES",
    "DhanClient",
    "DhanClientPool",
    "DhanClientProtocol",
    "DhanCredentials",
    "DhanError",
    "DhanInstrumentIndex",
    "DhanMarketAdapter",
    "DhanParameterStoreTokenStore",
    "DhanTokenRefresher",
    "DhanTransport",
    "from_dhan_response",
    "from_transport_exception",
    "load_dhan_aws_credentials",
    "parse_scrip_master",
]
