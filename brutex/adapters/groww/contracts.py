"""Groww derivative-contract resolution — expiries → contracts → parsed chain.

DEC-GROWW-OPTION-INGEST-001 (K1). The pure resolution leaf of the option ingest
path: it turns the SDK's expired-contract addressing into vendor-neutral
:class:`~brutex.adapters.protocol.DerivativeContract` rows, ready to feed to
:func:`~brutex.adapters.groww.option_ingest.fetch_option_candles`.

The SDK chain (``docs/groww/api-reference/03-backtesting.md:41-134``):

1. :func:`resolve_expiries` — ``groww.get_expiries(exchange, underlying[, year[, month]])``
   → the ``YYYY-MM-DD`` expiry dates. ``year``/``month`` are OPTIONAL (omit → the
   current year / the whole year), so :func:`enumerate_contracts` issues ONE call
   per historical year to cover the full ``available from 2020`` FNO depth.
2. :func:`resolve_contracts` — ``groww.get_contracts(exchange, underlying, expiry)``
   → the listed Groww symbols (ALL listed strikes, NOT relative-to-spot), each
   parsed by :func:`parse_contract_symbol`.

Everything here takes an injected :class:`~brutex.adapters.groww.client.GrowwClient`
(the only network surface; the SDK calls ride its ``pace`` rate gate), so every
branch is unit-testable with a fake — no ``growwapi``, no network. The parser is
pure.

**Scope (CLAUDE.md §3):** the contracts these functions resolve are
**LAKE / REFERENCE_ONLY** (DEC-P0-063) — never the brute-force engine or the
52-condition vocabulary.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from datetime import date
from decimal import Decimal, InvalidOperation

from brutex.adapters.groww.client import GrowwClient
from brutex.adapters.protocol import DerivativeContract

#: The Groww symbol-token suffix → contract type. ``FUT`` carries no strike;
#: ``CE`` / ``PE`` carry one (03-backtesting.md:16-22).
_FUTURE: str = "FUT"
_OPTION_TYPES: frozenset[str] = frozenset({"CE", "PE"})
#: Min token count of a Groww derivative symbol: ``EXCH-SYMBOL-EXPIRY-FUT`` (4)
#: for a future; an option adds a STRIKE token (``…-STRIKE-TYPE``) → 5.
_MIN_FUTURE_TOKENS: int = 4
_MIN_OPTION_TOKENS: int = 5


def parse_contract_symbol(
    symbol: str, *, underlying_symbol: str, expiry: date
) -> DerivativeContract:
    """Parse one Groww contract ``groww_symbol`` into a :class:`DerivativeContract`.

    Groww contract symbols are ``EXCH-SYMBOL-DDMmmYY[-STRIKE]-TYPE`` (e.g.
    ``NSE-NIFTY-30Sep25-24650-CE``, ``NSE-NIFTY-30Sep25-FUT``,
    ``BSE-SENSEX-25Sep25-79500-PE`` — 03-backtesting.md:33-35). Parsed
    **right-anchored** so a (hypothetical) hyphenated underlying never confuses
    the type/strike tokens: the LAST token is the type (``CE`` / ``PE`` / ``FUT``)
    and, for an option, the second-to-last is the strike.

    The ``underlying_symbol`` and ``expiry`` are taken from the **authoritative**
    ``get_contracts`` call arguments (every returned contract belongs to that one
    expiry — 03-backtesting.md:91), NOT re-parsed from the lossy ``DDMmmYY`` token,
    so there is no year-window / locale ambiguity.

    Parameters
    ----------
    symbol
        The full Groww contract symbol.
    underlying_symbol
        The underlying the contract was resolved for (e.g. ``"NIFTY"``).
    expiry
        The authoritative expiry date the contract belongs to.

    Returns
    -------
    DerivativeContract
        ``contract_type`` ``FUT`` (``strike=None``) or ``CE`` / ``PE`` (with the
        parsed ``strike`` as a ``float``). The :class:`DerivativeContract`
        constructor enforces the strike-required-for-options invariant.

    Raises
    ------
    ValueError
        On a non-derivative / malformed symbol: too few tokens, an unknown trailing
        type, or an unparseable strike (fail-loud — Golden Rule #1, never a silent
        skip of a vendor format drift).
    """
    tokens = symbol.split("-")
    if len(tokens) < _MIN_FUTURE_TOKENS:
        raise ValueError(f"not a Groww derivative symbol: {symbol!r}")
    contract_type = tokens[-1]
    if contract_type == _FUTURE:
        strike: float | None = None
    elif contract_type in _OPTION_TYPES:
        if len(tokens) < _MIN_OPTION_TOKENS:
            raise ValueError(f"option symbol missing strike token: {symbol!r}")
        strike = _parse_strike(tokens[-2], symbol=symbol)
    else:
        raise ValueError(f"unknown contract type {contract_type!r} in {symbol!r}")
    return DerivativeContract(
        underlying_symbol=underlying_symbol,
        expiry=expiry,
        symbol=symbol,
        contract_type=contract_type,
        strike=strike,
    )


def _parse_strike(token: str, *, symbol: str) -> float:
    """Parse a strike token to ``float`` via :class:`~decimal.Decimal` (exact, then float).

    Parsed through ``Decimal`` first so a non-numeric strike fails loudly (the
    :class:`DerivativeContract` field is ``float | None``, so the value is stored
    as ``float`` — the strike is metadata; the candle fetch keys on the full
    ``groww_symbol`` string, never the numeric strike).
    """
    try:
        return float(Decimal(token))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"unparseable strike {token!r} in {symbol!r}") from exc


def _expiries_array(payload: Mapping[str, object]) -> list[object]:
    """Extract the ``"expiries"`` array from a ``get_expiries`` payload (lenient empty).

    A MISSING ``"expiries"`` key is treated as "no expiries" (an underlying with no
    derivatives listed for the requested year is legitimate), but a PRESENT-yet-
    non-array value is a format drift and raises (Golden Rule #1).
    """
    raw = payload.get("expiries", [])
    if not isinstance(raw, (list, tuple)):
        raise ValueError(f"Groww 'expiries' must be an array, got {type(raw).__name__}: {raw!r}")
    return list(raw)


def resolve_expiries(
    client: GrowwClient,
    *,
    exchange: str,
    underlying_symbol: str,
    year: int | None = None,
    month: int | None = None,
) -> list[date]:
    """Resolve an underlying's FNO expiry dates for *year*/*month* (sorted, deduped).

    Calls ``client.expiries`` (``year``/``month`` omitted from the SDK call when
    ``None`` — the documented "current year / whole year" defaults) and parses the
    ``YYYY-MM-DD`` strings to :class:`datetime.date`.

    Raises
    ------
    ValueError
        If the ``"expiries"`` value is not an array or any entry is not a parseable
        ISO date string (fail-loud — Golden Rule #1).
    """
    payload = client.expiries(
        exchange=exchange, underlying_symbol=underlying_symbol, year=year, month=month
    )
    out: set[date] = set()
    for item in _expiries_array(payload):
        if not isinstance(item, str):
            raise ValueError(f"Groww expiry must be a string, got {type(item).__name__}: {item!r}")
        try:
            out.add(date.fromisoformat(item))
        except ValueError as exc:
            raise ValueError(f"unparseable Groww expiry: {item!r}") from exc
    return sorted(out)


def resolve_contracts(
    client: GrowwClient, *, exchange: str, underlying_symbol: str, expiry: date
) -> list[DerivativeContract]:
    """Resolve every listed contract for an ``(underlying, expiry)`` (parsed, deduped).

    Calls ``client.contracts`` and parses each returned Groww symbol via
    :func:`parse_contract_symbol`, stamping the authoritative *expiry*. Duplicate
    symbols (first-wins) are dropped via an O(1) ``set`` membership — never a
    nested scan.

    Raises
    ------
    ValueError
        If the ``"contracts"`` value is not an array, an entry is not a string, or
        a symbol fails :func:`parse_contract_symbol` (fail-loud).
    """
    payload = client.contracts(
        exchange=exchange, underlying_symbol=underlying_symbol, expiry_date=expiry.isoformat()
    )
    raw = payload.get("contracts", [])
    if not isinstance(raw, (list, tuple)):
        raise ValueError(f"Groww 'contracts' must be an array, got {type(raw).__name__}: {raw!r}")
    seen: set[str] = set()
    contracts: list[DerivativeContract] = []
    for item in raw:
        if not isinstance(item, str):
            raise ValueError(
                f"Groww contract must be a string, got {type(item).__name__}: {item!r}"
            )
        if item in seen:
            continue
        seen.add(item)
        contracts.append(
            parse_contract_symbol(item, underlying_symbol=underlying_symbol, expiry=expiry)
        )
    return contracts


def option_contracts(contracts: Iterable[DerivativeContract]) -> list[DerivativeContract]:
    """Filter a contract chain to the OPTION (``CE`` / ``PE``) legs only.

    The task feeds **CE/PE** symbols to the candle fetch (futures are resolved for
    completeness but are not the option lake's subject). O(n), order-preserving.
    """
    return [c for c in contracts if c.contract_type in _OPTION_TYPES]


def enumerate_contracts(
    client: GrowwClient,
    *,
    exchange: str,
    underlying_symbol: str,
    years: Iterable[int],
) -> Iterator[DerivativeContract]:
    """Lazily yield EVERY derivative contract for an underlying across *years*.

    For each year, resolve its expiries (one ``get_expiries`` call) and, for each
    expiry not already seen this run, yield its parsed contracts. Expiries are
    deduped across adjacent-year queries via an O(1) ``set`` so a leg-set is never
    fetched twice. A generator (lazy) so a caller can stop early.

    **Honest envelope (Golden Rule #10):** network-bound — one ``get_expiries`` per
    year + one ``get_contracts`` per discovered expiry — NOT O(1)-total; the
    per-call rate gate is the injected ``client.pace``.
    """
    seen_expiries: set[date] = set()
    for year in years:
        for expiry in resolve_expiries(
            client, exchange=exchange, underlying_symbol=underlying_symbol, year=year
        ):
            if expiry in seen_expiries:
                continue
            seen_expiries.add(expiry)
            yield from resolve_contracts(
                client, exchange=exchange, underlying_symbol=underlying_symbol, expiry=expiry
            )


__all__ = [
    "enumerate_contracts",
    "option_contracts",
    "parse_contract_symbol",
    "resolve_contracts",
    "resolve_expiries",
]
