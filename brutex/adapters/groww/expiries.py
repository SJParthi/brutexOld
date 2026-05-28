"""Groww expiry discovery — Surface C (DEC-GROWW-ADAPTER-DESIGN-001 §13.4).

`get_expiries` answers "what expiry dates exist for this F&O underlying?"
(``docs/groww/api-reference/03-backtesting.md`` — FNO data from 2020). The
store already has the `ExpiryRecord` schema + `upsert_expiries`; this is the
missing SDK wrapper.

:class:`ExpiryFetcher.fetch` mirrors the candle client's pace → call →
translate pattern (reusing :func:`~brutex.adapters.groww.client.to_neutral_exc`
so a Groww error routes the same neutral way), validates the year range up
front (a request below 2020 is rejected locally — no wasted quota), and turns
the ``{"expiries": [...]}`` response into validated :class:`ExpiryRecord` rows.
A single malformed date string is skipped + counted (never aborts the whole
underlying); the count is surfaced in :class:`ExpiryFetchResult`.

Pure-ish: ``sdk`` + ``bucket`` + ``store`` are injected (the store via a local
Protocol), so it imports + unit-tests with no SDK + no DuckDB.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Final, Protocol

from brutex.adapters.groww.client import Pacer, bad_request, is_rate_limit, to_neutral_exc
from brutex.data.instrument_store import ExpiryRecord

#: Groww serves FNO data from 2020 (``03-backtesting.md``); earlier is invalid.
_MIN_YEAR: Final[int] = 2020


def _utc_now() -> datetime:
    """Return the current UTC instant (audit/snapshot timestamp)."""
    return datetime.now(UTC)


def _new_run_id() -> str:
    """Mint a short, unique ``expiries-<hex>`` audit run id."""
    return f"expiries-{uuid.uuid4().hex[:12]}"


class _ExpirySink(Protocol):
    """The slice of :class:`~brutex.data.instrument_store.InstrumentStore` used here."""

    def upsert_expiries(self, expiries: Sequence[ExpiryRecord]) -> int:
        """Upsert *expiries* into the store; return the row count written."""
        ...


@dataclass(frozen=True, slots=True)
class ExpiryFetchResult:
    """Outcome of one :meth:`ExpiryFetcher.fetch`."""

    records: list[ExpiryRecord]
    skipped_dates: int


@dataclass(slots=True)
class ExpiryFetcher:
    """Wraps ``growwapi.get_expiries`` → validated :class:`ExpiryRecord` rows."""

    sdk: Any
    bucket: Pacer
    base_exception: type[BaseException]
    store: _ExpirySink
    run_id_factory: Callable[[], str] = _new_run_id
    clock: Callable[[], datetime] = _utc_now

    def fetch(
        self,
        underlying: str,
        exchange: str,
        *,
        year: int,
        month: int | None = None,
        snapshot_date: date | None = None,
    ) -> ExpiryFetchResult:
        """Pull the expiry dates for *underlying* in *year* (optionally *month*)."""
        current_year = self.clock().year
        if not (_MIN_YEAR <= year <= current_year + 1):
            raise bad_request(f"expiry year {year} out of [{_MIN_YEAR}, {current_year + 1}]")
        try:
            exchange_const = getattr(self.sdk, f"EXCHANGE_{exchange}")
        except AttributeError as exc:
            raise bad_request(f"unknown exchange {exchange!r}") from exc

        self.bucket.acquire()
        try:
            response = self.sdk.get_expiries(
                exchange=exchange_const,
                underlying_symbol=underlying,
                year=year,
                month=month,
            )
            raw = response["expiries"]
        except self.base_exception as exc:
            neutral = to_neutral_exc(exc)
            if is_rate_limit(neutral):
                self.bucket.record_rate_limit_hit()
            raise neutral from exc
        except (KeyError, TypeError) as exc:
            raise bad_request(f"malformed expiries response for {underlying!r}") from exc
        self.bucket.record_success()

        snapshot = snapshot_date or self.clock().date()
        fetched_at = self.clock()
        run_id = self.run_id_factory()
        records: list[ExpiryRecord] = []
        skipped = 0
        for item in raw:
            try:
                expiry = date.fromisoformat(str(item))
            except ValueError:
                skipped += 1  # malformed date string — skip + count, never poison the row set
                continue
            records.append(
                ExpiryRecord(
                    exchange=exchange,
                    underlying_symbol=underlying,
                    expiry_date=expiry,
                    snapshot_date=snapshot,
                    pulled_year=year,
                    fetched_at=fetched_at,
                    run_id=run_id,
                )
            )
        self.store.upsert_expiries(records)
        return ExpiryFetchResult(records=records, skipped_dates=skipped)


__all__ = [
    "ExpiryFetchResult",
    "ExpiryFetcher",
]
