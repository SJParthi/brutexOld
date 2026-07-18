"""Dhan option-chain orchestration — fetch (via injected client) → parse.

DEC-DHAN-ADAPTER-001. Thin fetch→parse glue over the injected
:class:`~brutex.adapters.dhan.client.DhanClientProtocol`: it resolves the expiry
list and one option-chain snapshot, delegating the pure parse to
:mod:`brutex.adapters.dhan.parser`. **LAKE / reference-only + GATED ON the Data-API
subscription** — nothing production wires a live client into it.

The option-chain endpoint carries the SPECIAL 1-request/3-seconds rate limit; the
client's ``option_chain`` method already applies its ``option_chain_pace`` pacer, so
this module just sequences the calls.
"""

from __future__ import annotations

from datetime import date

from brutex.adapters.dhan.client import DhanClientProtocol
from brutex.adapters.dhan.parser import (
    OptionChainSnapshot,
    parse_expiry_list,
    parse_option_chain,
)


def fetch_expiry_list(
    client: DhanClientProtocol, *, underlying_scrip: int, underlying_seg: str
) -> list[date]:
    """Fetch + parse the expiry list for an underlying (``POST /v2/optionchain/expirylist``)."""
    payload = client.expiry_list(underlying_scrip=underlying_scrip, underlying_seg=underlying_seg)
    return parse_expiry_list(payload)


def fetch_option_chain(
    client: DhanClientProtocol,
    *,
    underlying_scrip: int,
    underlying_seg: str,
    expiry: date,
) -> OptionChainSnapshot:
    """Fetch + parse one option-chain snapshot (``POST /v2/optionchain``).

    *expiry* is formatted ``YYYY-MM-DD`` (the vendor's expiry token). Honours the
    special 1-req/3s pacer via the client's ``option_chain`` method.
    """
    payload = client.option_chain(
        underlying_scrip=underlying_scrip,
        underlying_seg=underlying_seg,
        expiry=expiry.isoformat(),
    )
    return parse_option_chain(payload)


__all__ = ["fetch_expiry_list", "fetch_option_chain"]
