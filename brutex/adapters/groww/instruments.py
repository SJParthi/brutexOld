"""Groww instrument-master fetch — Surface B (DEC-GROWW-ADAPTER-DESIGN-001 §13.3).

The store (`brutex/data/instrument_store.py`) already PERSISTS instruments;
the missing piece is the **download + parse + normalise** of the Groww
instrument-master CSV (``growwapi-assets.groww.in/instruments/instrument.csv``,
19 columns — see ``docs/groww/api-reference/02-instruments.md``).

:class:`InstrumentMasterFetcher.refresh` does exactly that: GET the CSV →
validate the header → parse each row to a :class:`~brutex.models.Instrument`
→ tag fetch-scope (`universe.is_in_fetch_scope`) → ``upsert_many`` into the
store (transactional, idempotent by content hash).

Worst-case handling (§13.7 rows B1–B6):

* **Schema drift** (a required column missing) → :class:`InstrumentFormatError`,
  abort the refresh — the store keeps its last good snapshot (never half-load).
* **One bad row** (unparseable / fails `Instrument` validation) → quarantine
  that row (counted), keep the rest (one corrupt row ≠ no instruments).
* **Out-of-scope exchange** (MCX etc.) → filtered (not an error).
* **Price unit** — ``tick_size`` is float rupees in the CSV; `normalise_tick_size`
  canonicalises it (CLAUDE.md §6).

TTL-gating + stale-fallback-on-network-failure (§13.3) are the **caller's**
policy (the orchestrator/CLI decides *when* to refresh and what to do if the
download fails), so this module stays pure + testable: ``http_get`` and the
store are injected (the store via a local Protocol — no ``duckdb`` at import,
so the module is standalone-coverage-gateable). :meth:`from_env` wires the
real SSRF-guarded fetch + :class:`InstrumentStore` (``no cover`` — live network).
"""

from __future__ import annotations

import csv
import hashlib
import io
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Final, Protocol

from brutex.models import (
    Exchange,
    Instrument,
    InstrumentFormatError,
    InstrumentType,
    Segment,
    TickSizeFormatError,
    normalise_tick_size,
)
from brutex.universe import build_fno_eligible_underlyings, is_in_fetch_scope

#: The Groww instrument-master CSV (docs/groww/api-reference/02-instruments.md).
DEFAULT_CSV_URL: Final[str] = "https://growwapi-assets.groww.in/instruments/instrument.csv"

#: The host the production fetch is locked to (SSRF guard in ``from_env``).
_ALLOWED_HOST: Final[str] = "growwapi-assets.groww.in"

#: Columns that MUST be present in the CSV header; a missing one = schema drift.
_REQUIRED_COLUMNS: Final[frozenset[str]] = frozenset(
    {
        "exchange",
        "exchange_token",
        "trading_symbol",
        "groww_symbol",
        "name",
        "instrument_type",
        "segment",
        "series",
        "isin",
        "underlying_symbol",
        "underlying_exchange_token",
        "expiry_date",
        "strike_price",
        "lot_size",
        "tick_size",
        "freeze_quantity",
        "is_reserved",
        "buy_allowed",
        "sell_allowed",
    }
)

#: Exchanges in Track 1 scope; any other (MCX, …) is filtered, not quarantined.
_SUPPORTED_EXCHANGES: Final[frozenset[str]] = frozenset({"NSE", "BSE"})

#: CSV cell values that mean "absent" (Groww emits NaN for non-derivative fields).
_BLANK_VALUES: Final[frozenset[str]] = frozenset({"", "nan", "none", "null"})

#: Groww seeds the instrument master with internal *test* instruments — e.g.
#: ``031NSETEST36DECFUT`` / ``061NSETEST…`` (underlying ``…NSETEST``, expiry
#: 2036-11-27, strike ``-0.01``, freeze_quantity 100001). They are not tradable
#: and must never enter the lake. Verified against the operator's real
#: ``instrument.csv`` export (2026-05-29). Match is substring + case-insensitive.
_TEST_INSTRUMENT_MARKER: Final[str] = "NSETEST"

#: Per-row parse failures that quarantine ONE row rather than aborting the file.
_ROW_ERRORS: Final[tuple[type[Exception], ...]] = (
    ValueError,
    KeyError,
    InvalidOperation,
    TickSizeFormatError,
)


def _blank(value: str | None) -> bool:
    """True if *value* is ``None`` or a blank/placeholder CSV cell."""
    return value is None or value.strip().lower() in _BLANK_VALUES


def _opt_str(value: str | None) -> str | None:
    """Parse an optional trimmed string (``None`` when the cell is blank)."""
    return None if _blank(value) else value.strip()  # type: ignore[union-attr]


def _req_int(value: str) -> int:
    """Parse a required int, tolerating float-formatted ints (``"75.0"``)."""
    return int(float(value.strip()))  # tolerate float-formatted ints ("75.0")


def _opt_int(value: str | None) -> int | None:
    """Parse an optional int (``None`` when the cell is blank)."""
    return None if _blank(value) else int(float(value.strip()))  # type: ignore[union-attr]


def _opt_decimal(value: str | None) -> Decimal | None:
    """Parse an optional :class:`~decimal.Decimal` (``None`` when blank)."""
    return None if _blank(value) else Decimal(value.strip())  # type: ignore[union-attr]


def _opt_date(value: str | None) -> date | None:
    """Parse an optional ISO ``date`` (``None`` when the cell is blank)."""
    return None if _blank(value) else date.fromisoformat(value.strip())  # type: ignore[union-attr]


def _to_bool(value: str | None) -> bool:
    """Parse a CSV truthy flag (``1``/``true``/``yes``/``y`` → ``True``)."""
    return not _blank(value) and value.strip().lower() in {"1", "true", "yes", "y"}  # type: ignore[union-attr]


def _is_test_instrument_row(row: dict[str, str]) -> bool:
    """True if *row* is a Groww internal test instrument (never lake-bound).

    Two markers, both observed in the real master (2026-05-29): the
    ``NSETEST`` token in any identifying column, or a negative ``strike_price``
    (invalid for any real contract). Checked on the raw CSV row so a test row
    is skipped before any parse attempt.
    """
    blob = " ".join(
        (row.get(col) or "")
        for col in ("trading_symbol", "groww_symbol", "underlying_symbol", "name")
    ).upper()
    if _TEST_INSTRUMENT_MARKER in blob:
        return True
    return (row.get("strike_price") or "").strip().startswith("-")


def _row_to_instrument(row: dict[str, str]) -> Instrument:
    """Map one CSV row to a validated :class:`Instrument` (raises on a bad row)."""
    return Instrument(
        exchange=Exchange(row["exchange"].strip()),
        exchange_token=_req_int(row["exchange_token"]),
        trading_symbol=row["trading_symbol"].strip(),
        vendor_symbol=row["groww_symbol"].strip(),
        name=_opt_str(row["name"]),
        instrument_type=InstrumentType(row["instrument_type"].strip()),
        segment=Segment(row["segment"].strip()),
        series=_opt_str(row["series"]),
        isin=_opt_str(row["isin"]),
        underlying_symbol=_opt_str(row["underlying_symbol"]),
        underlying_exchange_token=_opt_int(row["underlying_exchange_token"]),
        lot_size=_req_int(row["lot_size"]),
        tick_size=normalise_tick_size(row["tick_size"].strip()),
        freeze_quantity=_opt_int(row["freeze_quantity"]),
        expiry_date=_opt_date(row["expiry_date"]),
        strike_price=_opt_decimal(row["strike_price"]),
        is_reserved=_to_bool(row["is_reserved"]),
        buy_allowed=_to_bool(row["buy_allowed"]),
        sell_allowed=_to_bool(row["sell_allowed"]),
        internal_trading_symbol=_opt_str(row.get("internal_trading_symbol")),
    )


@dataclass(frozen=True, slots=True)
class InstrumentRefreshResult:
    """Outcome of one :meth:`InstrumentMasterFetcher.refresh`.

    Attributes
    ----------
    parsed
        Instruments successfully built + upserted.
    filtered
        Rows skipped as out of scope: a foreign exchange (e.g. MCX) OR a Groww
        internal test instrument (``NSETEST`` / negative strike).
    quarantined
        Rows that failed to parse / validate and were dropped (counted, surfaced).
    csv_sha256
        Content hash of the CSV (the store's idempotence key).
    upsert
        The store's ``UpsertResult`` (rows written/replaced).
    """

    parsed: int
    filtered: int
    quarantined: int
    csv_sha256: str
    upsert: Any


class _InstrumentSink(Protocol):
    """The slice of :class:`~brutex.data.instrument_store.InstrumentStore` used here."""

    def upsert_many(
        self,
        instruments: Sequence[Instrument],
        *,
        snapshot_date: date,
        run_id: str,
        csv_source_sha256: str,
        in_scope_of: Callable[[Instrument], bool] | None = ...,
    ) -> Any:
        """Upsert *instruments* for this snapshot; return the store's result."""
        ...


@dataclass(slots=True)
class InstrumentMasterFetcher:
    """Downloads + parses the Groww instrument-master CSV into the store.

    Parameters
    ----------
    http_get
        ``url -> CSV text``. Real impl is SSRF-guarded (``from_env``); tests
        inject a fake returning canned CSV.
    store
        Where parsed instruments land (duck-types :class:`_InstrumentSink`).
    run_id_factory
        Produces the ``run_id`` stamped on each upsert (audit lineage).
    csv_url
        The master CSV URL.
    clock
        Supplies the snapshot date; injectable for deterministic tests.
    """

    http_get: Callable[[str], str]
    store: _InstrumentSink
    run_id_factory: Callable[[], str]
    csv_url: str = DEFAULT_CSV_URL
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))

    def refresh(self, *, snapshot_date: date | None = None) -> InstrumentRefreshResult:
        """Fetch the CSV, parse in-scope rows, and upsert them into the store.

        Raises :class:`InstrumentFormatError` on header schema drift (the store
        keeps its last good snapshot). A single unparseable row is quarantined,
        not fatal.
        """
        csv_text = self.http_get(self.csv_url)
        reader = csv.DictReader(io.StringIO(csv_text))
        self._validate_header(reader.fieldnames)

        instruments: list[Instrument] = []
        filtered = 0
        quarantined = 0
        for row in reader:
            if (row.get("exchange") or "").strip() not in _SUPPORTED_EXCHANGES:
                filtered += 1
                continue
            if _is_test_instrument_row(row):
                filtered += 1
                continue
            try:
                instruments.append(_row_to_instrument(row))
            except _ROW_ERRORS:
                quarantined += 1

        fno_eligible = build_fno_eligible_underlyings(instruments)
        csv_sha256 = hashlib.sha256(csv_text.encode("utf-8")).hexdigest()
        upsert = self.store.upsert_many(
            instruments,
            snapshot_date=snapshot_date or self.clock().date(),
            run_id=self.run_id_factory(),
            csv_source_sha256=csv_sha256,
            in_scope_of=lambda inst: is_in_fetch_scope(inst, fno_eligible),
        )
        return InstrumentRefreshResult(
            parsed=len(instruments),
            filtered=filtered,
            quarantined=quarantined,
            csv_sha256=csv_sha256,
            upsert=upsert,
        )

    @staticmethod
    def _validate_header(fieldnames: Sequence[str] | None) -> None:
        """Reject the CSV if any required column is missing (schema drift)."""
        present = frozenset(fieldnames or ())
        missing = _REQUIRED_COLUMNS - present
        if missing:
            raise InstrumentFormatError(
                f"instrument CSV missing required columns: {sorted(missing)}"
            )

    @classmethod
    def from_env(cls, store: _InstrumentSink) -> InstrumentMasterFetcher:  # pragma: no cover
        """Wire the real SSRF-guarded HTTP fetch + a uuid run-id."""
        import urllib.request
        import uuid
        from urllib.parse import urlparse

        def _ssrf_guarded_get(url: str) -> str:
            parsed = urlparse(url)
            if parsed.scheme != "https" or parsed.hostname != _ALLOWED_HOST:
                raise InstrumentFormatError(f"refusing non-allowlisted instrument URL: {url!r}")
            with urllib.request.urlopen(  # nosec B310 — scheme==https + host allow-listed above (SSRF-guarded)  # noqa: S310
                url, timeout=30
            ) as resp:
                raw: bytes = resp.read()
            try:
                return raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                # A truncated / non-UTF-8 / binary gateway-error body is a
                # parse failure, not an OSError. Re-raise as the typed parse
                # error so callers (the keyless load-instruments route) map it
                # to HTTP 502 rather than leaking an uncaught 500.
                raise InstrumentFormatError(
                    f"instrument CSV body was not valid UTF-8 ({exc})"
                ) from exc

        return cls(
            http_get=_ssrf_guarded_get,
            store=store,
            run_id_factory=lambda: f"instruments-{uuid.uuid4().hex[:12]}",
        )


__all__ = [
    "DEFAULT_CSV_URL",
    "InstrumentMasterFetcher",
    "InstrumentRefreshResult",
]
