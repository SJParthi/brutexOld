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
]
