"""Groww contract discovery — Surface D (DEC-GROWW-ADAPTER-DESIGN-001 §13.5).

`get_contracts` answers "what option/future symbols exist for this
(underlying, expiry)?" (``docs/groww/api-reference/03-backtesting.md``). The
store has the `ContractRecord` schema + `upsert_contracts`; this is the SDK
wrapper.

The catch (honest): `get_contracts` returns **only the symbol strings** —
``["NSE-NIFTY-30Jan25-24650-CE", ...]`` — but `ContractRecord` also needs
``lot_size`` + ``tick_size`` + ``freeze_quantity``, which the response does NOT
carry. So this fetcher:

* parses ``instrument_type`` (CE/PE/FUT) + ``strike`` straight from the symbol;
* enriches ``lot_size`` / ``tick_size`` / ``freeze_quantity`` via an injected
  ``metadata_lookup`` (production = the instrument-master store, Surface B);
* a symbol whose metadata is unavailable (e.g. an expired historical contract
  not in today's master) is skipped + counted — its CANDLES are still
  fetchable later (the candle fetcher needs only the symbol), but a complete
  `ContractRecord` can't be written without the metadata. The skip is surfaced
  in :class:`ContractFetchResult` (Phase-0b follow-up: a historical-metadata
  source).

Same pace → call → translate spine as the candle client + expiry fetcher.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Final, NamedTuple, Protocol

from brutex.adapters.groww.client import Pacer, bad_request, is_rate_limit, to_neutral_exc
from brutex.data.instrument_store import ContractRecord
from brutex.models import InstrumentType

#: Contract-symbol suffixes that carry no strike (futures) vs option legs.
_FUTURE_SUFFIX = "FUT"
_OPTION_SUFFIXES = frozenset({"CE", "PE"})

#: Minimum ``-``-split tokens for a valid contract symbol
#: (``EXCHANGE-UNDERLYING-EXPIRY-FUT`` = 4).
_MIN_CONTRACT_TOKENS: Final[int] = 4


def _utc_now() -> datetime:
    """Return the current UTC instant (audit/snapshot timestamp)."""
    return datetime.now(UTC)


def _new_run_id() -> str:
    """Mint a short, unique ``contracts-<hex>`` audit run id."""
    return f"contracts-{uuid.uuid4().hex[:12]}"


class ContractMeta(NamedTuple):
    """The per-contract economics the symbol can't supply (from the master)."""

    lot_size: int
    tick_size: Decimal
    freeze_quantity: int | None


def parse_contract_symbol(symbol: str) -> tuple[InstrumentType, Decimal | None] | None:
    """Parse ``(instrument_type, strike)`` from a Groww contract symbol.

    ``EXCHANGE-UNDERLYING-EXPIRY-FUT`` → ``(FUT, None)``;
    ``EXCHANGE-UNDERLYING-EXPIRY-STRIKE-CE/PE`` → ``(CE|PE, strike)``.
    Returns ``None`` for an unrecognised shape (caller quarantines it).
    """
    tokens = symbol.split("-")
    if len(tokens) < _MIN_CONTRACT_TOKENS:
        return None
    suffix = tokens[-1]
    if suffix == _FUTURE_SUFFIX:
        return (InstrumentType.FUT, None)
    if suffix in _OPTION_SUFFIXES:
        try:
            strike = Decimal(tokens[-2])
        except (InvalidOperation, IndexError):
            return None
        return (InstrumentType(suffix), strike)
    return None


#: Minimum ``-``-split tokens to recover the underlying vendor_symbol
#: (``EXCHANGE-UNDERLYING`` = 2) from a contract symbol.
_MIN_UNDERLYING_TOKENS: Final[int] = 2


def underlying_symbol_of(contract_symbol: str) -> str | None:
    """Return the underlying's vendor_symbol from a *contract_symbol*, or ``None``.

    A Groww contract symbol is ``EXCHANGE-UNDERLYING-EXPIRY-…`` (e.g.
    ``NSE-NIFTY-30Jan25-24650-CE`` or ``NSE-NIFTY-30Jan25-FUT``); its
    underlying instrument-master row is keyed by the ``EXCHANGE-UNDERLYING``
    prefix (``NSE-NIFTY``). This recovers that prefix so the contract can
    inherit the underlying's economics now that the legs themselves are no
    longer persisted (DEC-INSTRUMENT-UNIVERSE-FAST-001).

    Returns ``None`` for a symbol with too few tokens to carry an underlying.
    """
    tokens = contract_symbol.split("-")
    if len(tokens) < _MIN_UNDERLYING_TOKENS:
        return None
    return "-".join(tokens[:_MIN_UNDERLYING_TOKENS])


class _MetadataStore(Protocol):
    """The slice of :class:`~brutex.data.instrument_store.InstrumentStore` a
    store-backed :func:`store_metadata_lookup` reads (one point lookup)."""

    def get(self, vendor_symbol: str, *, as_of: date | None = ...) -> Any:
        """Return the instrument-master row for *vendor_symbol*, or ``None``."""
        ...


def store_metadata_lookup(
    store: _MetadataStore,
    *,
    as_of: date | None = None,
) -> Callable[[str], ContractMeta | None]:
    """Build a ``metadata_lookup`` that inherits a leg's economics from its underlying.

    Since the lean ingest (DEC-INSTRUMENT-UNIVERSE-FAST-001) no longer stores
    the FUT / CE / PE legs, a contract's ``lot_size`` / ``tick_size`` /
    ``freeze_quantity`` can no longer be read from the leg's own row. They are
    instead inherited from the UNDERLYING's instrument-master row, which IS
    persisted (an F&O-eligible equity, or an index). The returned callable:

    * recovers the underlying vendor_symbol via :func:`underlying_symbol_of`;
    * does ONE :meth:`InstrumentStore.get` point lookup (O(1) on the PK);
    * returns the underlying's ``(lot_size, tick_size, freeze_quantity)`` as a
      :class:`ContractMeta`, or ``None`` when the symbol has no recoverable
      underlying / the underlying is absent from the master (the
      :class:`ContractFetcher` then skips + counts that leg, exactly as
      before).

    Parameters
    ----------
    store
        Anything exposing :meth:`InstrumentStore.get` (the real store, or a
        fake in tests).
    as_of
        Optional temporal cutoff forwarded to :meth:`InstrumentStore.get`.
    """

    def _lookup(contract_symbol: str) -> ContractMeta | None:
        underlying = underlying_symbol_of(contract_symbol)
        if underlying is None:
            return None
        row = store.get(underlying, as_of=as_of)
        if row is None:
            return None
        return ContractMeta(
            lot_size=row.lot_size,
            tick_size=row.tick_size,
            freeze_quantity=row.freeze_quantity,
        )

    return _lookup


class _ContractSink(Protocol):
    """The slice of :class:`~brutex.data.instrument_store.InstrumentStore` used here."""

    def upsert_contracts(self, contracts: Sequence[ContractRecord]) -> int:
        """Upsert *contracts* into the store; return the row count written."""
        ...


@dataclass(frozen=True, slots=True)
class ContractFetchResult:
    """Outcome of one :meth:`ContractFetcher.fetch`."""

    records: list[ContractRecord]
    skipped_malformed: int
    skipped_no_metadata: int


@dataclass(slots=True)
class ContractFetcher:
    """Wraps ``growwapi.get_contracts`` → enriched :class:`ContractRecord` rows."""

    sdk: Any
    bucket: Pacer
    base_exception: type[BaseException]
    store: _ContractSink
    metadata_lookup: Callable[[str], ContractMeta | None]
    run_id_factory: Callable[[], str] = _new_run_id
    clock: Callable[[], datetime] = _utc_now

    def fetch(
        self,
        underlying: str,
        exchange: str,
        expiry_date: date,
        *,
        snapshot_date: date | None = None,
    ) -> ContractFetchResult:
        """Pull the contract symbols for *underlying* on *expiry_date* + enrich."""
        try:
            exchange_const = getattr(self.sdk, f"EXCHANGE_{exchange}")
        except AttributeError as exc:
            raise bad_request(f"unknown exchange {exchange!r}") from exc

        self.bucket.acquire()
        try:
            response = self.sdk.get_contracts(
                exchange=exchange_const,
                underlying_symbol=underlying,
                expiry_date=expiry_date.isoformat(),
            )
            raw = response["contracts"]
        except self.base_exception as exc:
            neutral = to_neutral_exc(exc)
            if is_rate_limit(neutral):
                self.bucket.record_rate_limit_hit()
            raise neutral from exc
        except (KeyError, TypeError) as exc:
            raise bad_request(f"malformed contracts response for {underlying!r}") from exc
        self.bucket.record_success()

        snapshot = snapshot_date or self.clock().date()
        fetched_at = self.clock()
        run_id = self.run_id_factory()
        records: list[ContractRecord] = []
        skipped_malformed = 0
        skipped_no_metadata = 0
        for symbol in raw:
            parsed = parse_contract_symbol(str(symbol))
            if parsed is None:
                skipped_malformed += 1
                continue
            instrument_type, strike = parsed
            meta = self.metadata_lookup(str(symbol))
            if meta is None:
                skipped_no_metadata += 1  # expired/unknown contract — symbol kept for candles
                continue
            records.append(
                ContractRecord(
                    exchange=exchange,
                    underlying_symbol=underlying,
                    expiry_date=expiry_date,
                    contract_symbol=str(symbol),
                    snapshot_date=snapshot,
                    instrument_type=instrument_type,
                    strike_price=strike,
                    lot_size=meta.lot_size,
                    freeze_quantity=meta.freeze_quantity,
                    tick_size=meta.tick_size,
                    fetched_at=fetched_at,
                    run_id=run_id,
                )
            )
        self.store.upsert_contracts(records)
        return ContractFetchResult(
            records=records,
            skipped_malformed=skipped_malformed,
            skipped_no_metadata=skipped_no_metadata,
        )


__all__ = [
    "ContractFetchResult",
    "ContractFetcher",
    "ContractMeta",
    "parse_contract_symbol",
    "store_metadata_lookup",
    "underlying_symbol_of",
]
