"""One-day equity OHLCV cross-check — Groww ``1day`` candles vs. the NSE bhavcopy.

DEC-NSE-EQUITY-VERIFY-001. The stock twin of the index harness
(:mod:`brutex.data.validation.nse_oneday`, DEC-NSE-VERIFY-ROUTE-001). Before a
full-history Groww EQUITY pull is scaled, the operator wants ONE day's stock data
cross-verified against the INDEPENDENT official NSE security bhavcopy — and for a
STOCK that means the **full OHLCV, Volume included** (unlike an index, which
carries no volume). A systematic vendor mismatch (rounding, timezone, adjusted vs
raw, wrong-series, or a volume-convention difference) is then caught on day one
rather than after a multi-year pull.

## What's different from the index harness (Volume)

A spot INDEX has no traded volume (CLAUDE.md §6 / DEC-CANDLE-PARSE-RESILIENCE-001
maps an index's null volume → 0), so the index harness compares four OHLC fields.
A STOCK trades real volume, so this harness adds a fifth comparison: the Groww
``1day`` bar's ``volume`` vs the bhavcopy's total-traded-quantity column. The
**Groww↔NSE volume delta is UNVERIFIED** — vendor and exchange may count volume
differently (e.g. block/auction inclusion), so the default volume tolerance is a
documented :data:`DEFAULT_VOLUME_TOLERANCE_UNVERIFIED` (``0`` shares = exact) that
carries ``_UNVERIFIED`` in its name. A verdict's primary honest output is the
**signed share delta**; the tolerance only labels the field matched/mismatched.
The operator tunes it from real transcripts, never the reverse (Golden Rule #1).

## Reuse (CLAUDE.md §11 COMMON)

The OHLC comparison delegates wholesale to the index harness's pure
:func:`~brutex.data.validation.nse_oneday.compare_quotes` (one source of truth for
the per-field signed-paisa delta + tolerance flag); this module only adds the
volume axis + the equity-bhavcopy parsing on top. The live official opener is the
SAME ``# pragma: no cover`` urllib surface the index harness already ships.

## Pure vs. live split + degrade-never-crash (mirrors nse_oneday)

Every parse / map / compare function is pure + 100% unit-testable offline. The two
``# pragma: no cover`` functions are the only live surfaces (real Groww client +
real NSE urllib), which CI cannot exercise (NSE bot-blocks + this build env
network-blocks the host). The live entrypoint
(:func:`validate_equity_oneday`) takes both openers injected, degrades ANY vendor
error / network block / malformed payload to an honest ``unavailable`` verdict,
and never crashes an operator's terminal.

## O(1) honesty (Golden Rule #5 / #10)

:func:`compare_equity_quotes` (5 fixed fields) is O(1). Parsing the bhavcopy CSV is
O(rows); the network fetch is bounded latency, **NOT O(1)-total** — this is an
offline operator verification tool, never a sweep hot path, so it is exempt from
the per-bar O(1) ratio gate.
"""

from __future__ import annotations

import csv
import io
import json
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Final

from brutex.calendars import IST
from brutex.data.candle_fields import CandleParseError, to_price
from brutex.data.models import CandleBar
from brutex.data.validation.nse_oneday import (
    NSE_OFFICIAL_BASE_URL,
    FieldComparison,
    OhlcQuote,
    VerdictStatus,
    _default_official_opener,
    compare_quotes,
)
from brutex.models import BruteXError
from brutex.money import round_money

# ────────────────────────────────────────────────────────────────────────────
# Constants + env-overridable knobs
# ────────────────────────────────────────────────────────────────────────────

#: NSE full security bhavcopy URL template — **verified 2026-06-13** against the
#: live file format (``nsearchives.nseindia.com/products/content/sec_bhavdata_full_DDMMYYYY.csv``;
#: date = ``%d%m%Y``, e.g. ``…_20012025.csv``), corroborated by independent mirrors
#: (the chartiny/nse-sec-bhavdata-full daily archive) + community parsers. This is
#: the security-wise "full bhavdata" report (OHLC + ``TTL_TRD_QNTY`` volume +
#: delivery) — NOT the ``cm`` cash bhavcopy NSE retired July-2024 for CM-UDiFF, so
#: it is the correct still-live file for a Volume cross-check. Still
#: ``{ddmmyyyy}``-templated + env-overridable (NSE may relocate it; a wrong guess
#: degrades to ``unavailable``, never a false verdict — Golden Rule #1). The older
#: ``archives.nseindia.com`` host serves the same file as a fallback.
_NSE_BHAVCOPY_URL_TEMPLATE: Final[str] = (
    f"{NSE_OFFICIAL_BASE_URL}/products/content/sec_bhavdata_full_{{ddmmyyyy}}.csv"
)

#: Operator override env for the bhavcopy URL template (a single URL string with a
#: ``{ddmmyyyy}`` / ``{yyyymmdd}`` / ``{iso}`` date placeholder). Malformed/absent
#: → the built-in default. DOCUMENTED "verify against NSE; may change".
BHAVCOPY_URL_OVERRIDE_ENV: Final[str] = "BRUTEX_NSE_EQUITY_BHAVCOPY_URL"

#: Operator override env for the bhavcopy column labels (NSE may rename / pad them;
#: verify). JSON object with any of the :class:`EquityCsvSpec` field names.
EQUITY_CSV_SPEC_OVERRIDE_ENV: Final[str] = "BRUTEX_NSE_EQUITY_CSV_SPEC"

#: Operator override env for the per-field rupee OHLC tolerance (a Decimal string).
PRICE_TOLERANCE_OVERRIDE_ENV: Final[str] = "BRUTEX_NSE_EQUITY_PRICE_TOLERANCE"

#: Operator override env for the volume tolerance, in whole shares (an int string).
VOLUME_TOLERANCE_OVERRIDE_ENV: Final[str] = "BRUTEX_NSE_EQUITY_VOLUME_TOLERANCE"

#: The official bhavcopy series to match (cash equity = ``EQ``); env-overridable.
EQUITY_SERIES_OVERRIDE_ENV: Final[str] = "BRUTEX_NSE_EQUITY_SERIES"

#: Default per-field absolute OHLC tolerance, in rupees — **UNVERIFIED** (mirrors
#: the index harness): ``0.01`` = one 2-dp tick, absorbing a single legitimate
#: ``ROUND_HALF_UP`` boundary disagreement while flagging any ≥2-tick divergence.
DEFAULT_PRICE_TOLERANCE_RUPEES_UNVERIFIED: Final[Decimal] = Decimal("0.01")

#: Default volume tolerance, in whole shares — **UNVERIFIED**. ``0`` = exact match;
#: the true Groww↔NSE volume-convention delta is the OPEN question this harness
#: exists to measure. The ``_UNVERIFIED`` suffix carries the honesty requirement.
DEFAULT_VOLUME_TOLERANCE_UNVERIFIED: Final[int] = 0

#: Default cash-equity series label in the bhavcopy.
DEFAULT_EQUITY_SERIES: Final[str] = "EQ"


class EquityCsvSpec:
    """Column labels for the NSE security bhavcopy (one tolerant parser dialect).

    DOCUMENTED, env-overridable (:data:`EQUITY_CSV_SPEC_OVERRIDE_ENV`) — NSE may
    rename / whitespace-pad columns; verify. The defaults match the published
    "sec_bhavdata_full" convention; a wrong guess degrades to ``unavailable`` (the
    row/columns are simply not found), never a false verdict. A plain class (not a
    frozen dataclass) so the field set is obvious + cheap to override-merge.
    """

    __slots__ = (
        "close_column",
        "high_column",
        "low_column",
        "open_column",
        "series_column",
        "symbol_column",
        "volume_column",
    )

    def __init__(
        self,
        *,
        symbol_column: str,
        series_column: str,
        open_column: str,
        high_column: str,
        low_column: str,
        close_column: str,
        volume_column: str,
    ) -> None:
        """Store each logical→CSV column label (keyword-only, all required)."""
        self.symbol_column = symbol_column
        self.series_column = series_column
        self.open_column = open_column
        self.high_column = high_column
        self.low_column = low_column
        self.close_column = close_column
        self.volume_column = volume_column


#: Default bhavcopy column labels — **verified 2026-06-13** against the live
#: ``sec_bhavdata_full`` header
#: ``SYMBOL, SERIES, DATE1, PREV_CLOSE, OPEN_PRICE, HIGH_PRICE, LOW_PRICE,
#: LAST_PRICE, CLOSE_PRICE, AVG_PRICE, TTL_TRD_QNTY, …``. Every column AFTER
#: ``SYMBOL`` carries a LEADING SPACE (``" SERIES"``, ``" OPEN_PRICE"``) and every
#: VALUE is leading-space-padded with NO thousands commas (``" 311.15"``,
#: ``" 133716"``); the whitespace/case-tolerant resolver + ``Decimal`` /
#: ``_parse_volume`` strip handle both. Still env-overridable should NSE rename a
#: column (a wrong label degrades to ``unavailable``, never a false verdict).
DEFAULT_EQUITY_CSV_SPEC: Final[EquityCsvSpec] = EquityCsvSpec(
    symbol_column="SYMBOL",
    series_column="SERIES",
    open_column="OPEN_PRICE",
    high_column="HIGH_PRICE",
    low_column="LOW_PRICE",
    close_column="CLOSE_PRICE",
    volume_column="TTL_TRD_QNTY",
)


@dataclass(frozen=True, slots=True)
class EquityQuote:
    """One stock's one-day OHLCV as 2-dp Decimal rupees + integer volume + provenance.

    OHLC are snapped through :func:`~brutex.money.round_money` (``ROUND_HALF_UP`` —
    DEC-SPOT-PRICE-2DP-001) so vendor float-noise can never leak into a verdict.
    Non-finite prices + negative volume ARE rejected (they can never be a real
    bar). Prices are deliberately NOT ordered-validated (a ``high < low`` official
    row surfaces as a per-field delta, never a crash).

    Attributes
    ----------
    symbol
        The bare engine stock symbol (e.g. ``"RELIANCE"``).
    trade_date
        The IST trading day this OHLCV represents.
    open, high, low, close
        2-dp Decimal rupee prices.
    volume
        Integer traded volume (shares).
    source
        Provenance label (``"groww"`` / ``"nse"``) for the report.
    """

    symbol: str
    trade_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    source: str

    def __post_init__(self) -> None:
        """Reject a non-finite price (NaN/Inf) or a negative volume — never a real bar."""
        for name, value in (
            ("open", self.open),
            ("high", self.high),
            ("low", self.low),
            ("close", self.close),
        ):
            if not value.is_finite():
                raise ValueError(f"EquityQuote.{name} must be a finite Decimal, got {value!r}")
        if self.volume < 0:
            raise ValueError(f"EquityQuote.volume must be non-negative, got {self.volume}")

    def as_ohlc_quote(self) -> OhlcQuote:
        """Project to the index harness's :class:`OhlcQuote` (drops volume) for OHLC reuse."""
        return OhlcQuote(
            symbol=self.symbol,
            trade_date=self.trade_date,
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            source=self.source,
        )


@dataclass(frozen=True, slots=True)
class VolumeComparison:
    """The Groww-vs-official volume pair + signed share delta + within-tolerance flag.

    Attributes
    ----------
    groww, official
        The two integer volumes being compared (shares).
    delta
        ``groww - official`` — signed shares (the harness's primary honest output).
    matched
        ``abs(delta) <= tolerance`` (inclusive).
    """

    groww: int
    official: int
    delta: int
    matched: bool

    def to_dict(self) -> dict[str, object]:
        """JSON-safe view of the volume comparison (all ints)."""
        return {
            "groww": self.groww,
            "official": self.official,
            "delta": self.delta,
            "matched": self.matched,
        }


@dataclass(frozen=True, slots=True)
class EquityVerdict:
    """The honest per-stock verdict: status + per-field OHLC deltas + volume + note.

    Attributes
    ----------
    symbol
        The bare engine stock symbol.
    trade_date
        The IST trading day.
    status
        :class:`~brutex.data.validation.nse_oneday.VerdictStatus` — matched /
        mismatched / unavailable.
    price_tolerance
        The per-field rupee OHLC tolerance applied (UNVERIFIED — see docs).
    volume_tolerance
        The whole-share volume tolerance applied (UNVERIFIED — see docs).
    fields
        Per-OHLC-field comparisons (empty for an ``unavailable`` verdict).
    volume
        The volume comparison, or ``None`` for an ``unavailable`` verdict.
    groww_source, official_source
        Provenance labels, or ``None`` when that side was never obtained.
    note
        A human reason on ``unavailable`` / ``mismatched`` (else ``None``).
    """

    symbol: str
    trade_date: date
    status: VerdictStatus
    price_tolerance: Decimal
    volume_tolerance: int
    fields: tuple[FieldComparison, ...]
    volume: VolumeComparison | None
    groww_source: str | None
    official_source: str | None
    note: str | None

    @property
    def matched(self) -> bool:
        """True iff every OHLC field AND the volume matched (status is MATCHED)."""
        return self.status is VerdictStatus.MATCHED

    @property
    def mismatched_fields(self) -> tuple[FieldComparison, ...]:
        """The OHLC fields whose delta exceeded tolerance (empty unless mismatched)."""
        return tuple(field for field in self.fields if not field.matched)

    def to_dict(self) -> dict[str, object]:
        """JSON-safe view of the verdict for the dashboard ``verify-nse`` route.

        Mirrors the index :meth:`~brutex.data.validation.nse_oneday.OneDayVerdict.to_dict`
        wire shape (so the existing renderer reads ``symbol`` / ``trade_date`` /
        ``status`` / ``fields``) and ADDS the ``volume`` object + the
        ``volume_tolerance``. Decimals serialise as strings (never floats).
        """
        return {
            "symbol": self.symbol,
            "trade_date": self.trade_date.isoformat(),
            "status": self.status.value,
            "tolerance": str(self.price_tolerance),
            "volume_tolerance": self.volume_tolerance,
            "fields": [field.to_dict() for field in self.fields],
            "volume": None if self.volume is None else self.volume.to_dict(),
            "groww_source": self.groww_source,
            "official_source": self.official_source,
            "note": self.note,
        }


# ────────────────────────────────────────────────────────────────────────────
# Small pure helpers (env override / symbol normalize)
# ────────────────────────────────────────────────────────────────────────────


def _override_str(value: object, default: str) -> str:
    """Return *value* trimmed when a non-blank string, else *default* (mirror nse_oneday)."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return default


def normalize_equity_symbol(symbol: str) -> str:
    """Normalize a stock symbol to its bare upper form (``"NSE-RELIANCE"`` → ``"RELIANCE"``)."""
    key = symbol.strip().upper()
    if "-" in key:
        key = key.split("-", 1)[1]
    return key


def _equity_series() -> str:
    """Resolve the bhavcopy series to match (operator override over ``EQ``)."""
    return _override_str(os.environ.get(EQUITY_SERIES_OVERRIDE_ENV), DEFAULT_EQUITY_SERIES)


def bhavcopy_url(trade_date: date) -> str:
    """Render the bhavcopy URL for *trade_date* (operator override over the default).

    Substitutes the ``{ddmmyyyy}`` / ``{yyyymmdd}`` / ``{iso}`` date placeholders.
    The override env value wins when a non-blank string.
    """
    template = _override_str(os.environ.get(BHAVCOPY_URL_OVERRIDE_ENV), _NSE_BHAVCOPY_URL_TEMPLATE)
    return (
        template.replace("{ddmmyyyy}", trade_date.strftime("%d%m%Y"))
        .replace("{yyyymmdd}", trade_date.strftime("%Y%m%d"))
        .replace("{iso}", trade_date.isoformat())
    )


def _equity_csv_spec() -> EquityCsvSpec:
    """Resolve the bhavcopy column spec (operator override merged over the default)."""
    raw = os.environ.get(EQUITY_CSV_SPEC_OVERRIDE_ENV)
    if not raw:
        return DEFAULT_EQUITY_CSV_SPEC
    try:
        parsed = json.loads(raw)
    except ValueError:
        return DEFAULT_EQUITY_CSV_SPEC
    if not isinstance(parsed, dict):
        return DEFAULT_EQUITY_CSV_SPEC
    d = DEFAULT_EQUITY_CSV_SPEC
    return EquityCsvSpec(
        symbol_column=_override_str(parsed.get("symbol_column"), d.symbol_column),
        series_column=_override_str(parsed.get("series_column"), d.series_column),
        open_column=_override_str(parsed.get("open_column"), d.open_column),
        high_column=_override_str(parsed.get("high_column"), d.high_column),
        low_column=_override_str(parsed.get("low_column"), d.low_column),
        close_column=_override_str(parsed.get("close_column"), d.close_column),
        volume_column=_override_str(parsed.get("volume_column"), d.volume_column),
    )


def _resolve_columns(fieldnames: Sequence[str], spec: EquityCsvSpec) -> dict[str, str] | None:
    """Map the 7 logical columns to the CSV's actual headers (whitespace/case tolerant).

    Returns ``{"symbol","series","open","high","low","close","volume"} -> header``
    or ``None`` if any required column is absent (→ caller degrades to ``unavailable``).
    """
    by_norm = {name.strip().casefold(): name for name in fieldnames}
    wanted = {
        "symbol": spec.symbol_column,
        "series": spec.series_column,
        "open": spec.open_column,
        "high": spec.high_column,
        "low": spec.low_column,
        "close": spec.close_column,
        "volume": spec.volume_column,
    }
    resolved: dict[str, str] = {}
    for logical, label in wanted.items():
        actual = by_norm.get(label.strip().casefold())
        if actual is None:
            return None
        resolved[logical] = actual
    return resolved


def _parse_volume(raw: object) -> int | None:
    """Parse a bhavcopy volume cell → non-negative int, or ``None`` when unusable.

    Tolerates surrounding whitespace + thousands commas; rejects a blank, a
    non-numeric, a fractional, or a negative value (never fabricates a count).
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip().replace(",", "")
    if not text:
        return None
    try:
        value = int(text)
    except ValueError:
        return None
    return value if value >= 0 else None


# ────────────────────────────────────────────────────────────────────────────
# Official bhavcopy parsing (PURE)
# ────────────────────────────────────────────────────────────────────────────


def parse_bhavcopy_quote(
    csv_text: str,
    *,
    symbol: str,
    trade_date: date,
    series: str | None = None,
    spec: EquityCsvSpec | None = None,
) -> EquityQuote | None:
    """Parse the NSE security bhavcopy → the :class:`EquityQuote` for *symbol* (pure).

    Finds the row whose SYMBOL matches *symbol* (case-insensitive, trimmed) AND
    whose SERIES matches the configured cash-equity series (default ``EQ``), reads
    O/H/L/C via *spec* (defaulting to the env-resolved spec) through the strict
    :func:`~brutex.data.candle_fields.to_price` (rejects non-finite) snapped to 2-dp
    via :func:`~brutex.money.round_money`, and the volume via :func:`_parse_volume`.
    Returns ``None`` — never raises — for: empty text, no header, a missing required
    column, the target row absent, or a malformed price/volume in the matched row.
    Every number comes ONLY from *csv_text* (Golden Rule #1 — never fabricated).
    """
    resolved_spec = spec if spec is not None else _equity_csv_spec()
    target_series = (series if series is not None else _equity_series()).strip().casefold()
    reader = csv.DictReader(io.StringIO(csv_text))
    if reader.fieldnames is None:
        return None
    columns = _resolve_columns(reader.fieldnames, resolved_spec)
    if columns is None:
        return None
    target_symbol = normalize_equity_symbol(symbol).casefold()
    for row in reader:
        if (row.get(columns["symbol"]) or "").strip().casefold() != target_symbol:
            continue
        if (row.get(columns["series"]) or "").strip().casefold() != target_series:
            continue
        volume = _parse_volume(row.get(columns["volume"]))
        if volume is None:
            return None  # found the row but its volume is malformed → unavailable
        try:
            return EquityQuote(
                symbol=normalize_equity_symbol(symbol),
                trade_date=trade_date,
                open=round_money(to_price(row.get(columns["open"]), "open")),
                high=round_money(to_price(row.get(columns["high"]), "high")),
                low=round_money(to_price(row.get(columns["low"]), "low")),
                close=round_money(to_price(row.get(columns["close"]), "close")),
                volume=volume,
                source="nse",
            )
        except CandleParseError:
            return None  # found the row but a price is malformed/non-finite → unavailable
    return None


# ────────────────────────────────────────────────────────────────────────────
# Groww candle → equity quote (PURE; IST-date selection, volume kept)
# ────────────────────────────────────────────────────────────────────────────


def quote_from_groww_equity_candles(
    bars: Sequence[CandleBar], *, symbol: str, trade_date: date
) -> EquityQuote | None:
    """Build the one-day :class:`EquityQuote` for *trade_date* from Groww candles (pure).

    Selects the bar whose IST-LOCAL calendar date equals *trade_date* (the daily bar
    is stored UTC, so it is converted back through the canonical
    :data:`~brutex.calendars.IST` zone before matching — mirrors the index harness's
    rule). Returns the 2-dp-snapped quote **with volume kept** ONLY when exactly one
    bar matches; ``None`` for zero (holiday / unlisted) or an ambiguous >1.
    """
    matched = [bar for bar in bars if bar.timestamp.astimezone(IST).date() == trade_date]
    if len(matched) != 1:
        return None
    bar = matched[0]
    return EquityQuote(
        symbol=normalize_equity_symbol(symbol),
        trade_date=trade_date,
        open=round_money(bar.open),
        high=round_money(bar.high),
        low=round_money(bar.low),
        close=round_money(bar.close),
        volume=bar.volume,
        source="groww",
    )


# ────────────────────────────────────────────────────────────────────────────
# Compare → verdict (PURE) — OHLC reuses nse_oneday.compare_quotes
# ────────────────────────────────────────────────────────────────────────────


def _volume_note(volume: VolumeComparison, fields: Sequence[FieldComparison]) -> str:
    """Honest one-line summary of a mismatch (OHLC field names + signed volume delta)."""
    bad = [f.field for f in fields if not f.matched]
    parts: list[str] = []
    if bad:
        parts.append(f"OHLC [{','.join(bad)}]")
    if not volume.matched:
        parts.append(f"volume Δ={volume.delta:+d} shares")
    return f"{' + '.join(parts)} exceed tolerance (tolerances are UNVERIFIED — see module docs)"


def compare_equity_quotes(
    groww: EquityQuote,
    official: EquityQuote,
    *,
    price_tolerance: Decimal = DEFAULT_PRICE_TOLERANCE_RUPEES_UNVERIFIED,
    volume_tolerance: int = DEFAULT_VOLUME_TOLERANCE_UNVERIFIED,
) -> EquityVerdict:
    """Compare two :class:`EquityQuote`\\ s (OHLC + Volume) → :class:`EquityVerdict` (pure).

    The four OHLC fields delegate to the index harness's
    :func:`~brutex.data.validation.nse_oneday.compare_quotes` (the single source of
    truth for the signed-paisa delta + tolerance flag). The volume adds a fifth
    axis: ``delta = groww.volume - official.volume``, ``matched`` iff
    ``abs(delta) <= volume_tolerance`` (inclusive). The verdict is ``MATCHED`` iff
    every OHLC field AND the volume matched, else ``MISMATCHED`` with an honest note.
    """
    ohlc = compare_quotes(
        groww.as_ohlc_quote(), official.as_ohlc_quote(), tolerance=price_tolerance
    )
    volume_delta = groww.volume - official.volume
    volume = VolumeComparison(
        groww=groww.volume,
        official=official.volume,
        delta=volume_delta,
        matched=abs(volume_delta) <= volume_tolerance,
    )
    all_matched = ohlc.matched and volume.matched
    status = VerdictStatus.MATCHED if all_matched else VerdictStatus.MISMATCHED
    return EquityVerdict(
        symbol=groww.symbol,
        trade_date=groww.trade_date,
        status=status,
        price_tolerance=price_tolerance,
        volume_tolerance=volume_tolerance,
        fields=ohlc.fields,
        volume=volume,
        groww_source=groww.source,
        official_source=official.source,
        note=None if all_matched else _volume_note(volume, ohlc.fields),
    )


def _unavailable(
    symbol: str,
    trade_date: date,
    price_tolerance: Decimal,
    volume_tolerance: int,
    note: str,
    *,
    groww_source: str | None = None,
    official_source: str | None = None,
) -> EquityVerdict:
    """Build an ``UNAVAILABLE`` equity verdict carrying the honest *note* (no fields)."""
    return EquityVerdict(
        symbol=symbol,
        trade_date=trade_date,
        status=VerdictStatus.UNAVAILABLE,
        price_tolerance=price_tolerance,
        volume_tolerance=volume_tolerance,
        fields=(),
        volume=None,
        groww_source=groww_source,
        official_source=official_source,
        note=note,
    )


# ────────────────────────────────────────────────────────────────────────────
# Tolerance resolution (PURE apart from the single env read)
# ────────────────────────────────────────────────────────────────────────────


def _resolve_price_tolerance(explicit: Decimal | None) -> Decimal:
    """Resolve the OHLC rupee tolerance: explicit > env > the UNVERIFIED default."""
    if explicit is not None:
        return explicit
    raw = os.environ.get(PRICE_TOLERANCE_OVERRIDE_ENV)
    if raw:
        try:
            value = Decimal(raw)
        except ArithmeticError:
            return DEFAULT_PRICE_TOLERANCE_RUPEES_UNVERIFIED
        if value.is_finite() and value >= 0:
            return value
    return DEFAULT_PRICE_TOLERANCE_RUPEES_UNVERIFIED


def _resolve_volume_tolerance(explicit: int | None) -> int:
    """Resolve the volume share tolerance: explicit > env > the UNVERIFIED default."""
    if explicit is not None:
        return explicit
    raw = os.environ.get(VOLUME_TOLERANCE_OVERRIDE_ENV)
    if raw:
        try:
            value = int(raw.strip())
        except ValueError:
            return DEFAULT_VOLUME_TOLERANCE_UNVERIFIED
        if value >= 0:
            return value
    return DEFAULT_VOLUME_TOLERANCE_UNVERIFIED


# ────────────────────────────────────────────────────────────────────────────
# Default LIVE fetchers — the ONLY ``# pragma: no cover`` surfaces
# ────────────────────────────────────────────────────────────────────────────

#: Injected Groww equity fetcher: ``(symbol, trade_date) -> raw candle bars``.
GrowwEquityFetcher = Callable[[str, date], Sequence[CandleBar]]
#: Injected official opener: ``(url) -> csv_text`` (reuses the index harness opener).
OfficialOpener = Callable[[str], str]

#: Days BEFORE the trade date the live Groww window starts (absorb IST→UTC + weekend).
_GROWW_WINDOW_BACK_DAYS: Final[int] = 4


def _default_groww_equity_fetcher(
    symbol: str, trade_date: date
) -> list[CandleBar]:  # pragma: no cover - live network only
    """Pull *symbol*'s ``1day`` Groww EQUITY candles around *trade_date* (the real client).

    LIVE ONLY: builds a live Groww client from the stored TOTP credentials, pulls a
    tight window of ``1day`` CASH candles for ``NSE-<symbol>``, and parses them with
    ``drop_volume=False`` (a stock carries real volume — the whole point). Read-only
    use of the Groww adapter. Mirrors ``nse_oneday._default_groww_fetcher`` but for
    the cash-equity segment + with volume kept.
    """
    from datetime import datetime, time, timedelta

    from brutex.adapters.groww.auth import resolve_groww_credentials
    from brutex.adapters.groww.chunks import plan_candle_windows
    from brutex.adapters.groww.constants import INTERVAL_TO_GROWW, TRACK1_DAILY_INTERVAL
    from brutex.adapters.groww.live import build_live_client
    from brutex.adapters.groww.parser import parse_candle_rows

    client = build_live_client(resolve_groww_credentials())
    start = trade_date - timedelta(days=_GROWW_WINDOW_BACK_DAYS)
    end = trade_date + timedelta(days=1)
    groww_interval = INTERVAL_TO_GROWW[TRACK1_DAILY_INTERVAL]
    bars: list[CandleBar] = []
    for window in plan_candle_windows(
        datetime.combine(start, time()),
        datetime.combine(end, time()),
        interval=TRACK1_DAILY_INTERVAL,
    ):
        payload = client.historical_candles(
            exchange="NSE",
            segment="CASH",
            groww_symbol=f"NSE-{normalize_equity_symbol(symbol)}",
            interval=groww_interval,
            start=window.start,
            end=window.end,
        )
        bars.extend(parse_candle_rows(payload, drop_volume=False))
    return bars


# ────────────────────────────────────────────────────────────────────────────
# Live entrypoint (openers injected; degrade-safe orchestration)
# ────────────────────────────────────────────────────────────────────────────


def validate_equity_oneday(
    symbol: str,
    trade_date: date,
    *,
    price_tolerance: Decimal | None = None,
    volume_tolerance: int | None = None,
    series: str | None = None,
    groww_fetcher: GrowwEquityFetcher | None = None,
    official_opener: OfficialOpener | None = None,
    spec: EquityCsvSpec | None = None,
) -> EquityVerdict:
    """Cross-verify ONE stock's Groww 1-day OHLCV vs. the NSE bhavcopy for *trade_date*.

    Fetches both sides via the INJECTED openers (defaults = the two live fetchers),
    degrades to an honest ``unavailable`` verdict on ANY vendor error / network
    block / timeout / malformed payload — it never crashes. The comparison is the
    pure :func:`compare_equity_quotes` (OHLC reused from the index harness + the
    volume axis). Tests inject fakes for both openers → 100% offline.

    Parameters
    ----------
    symbol
        An engine (``"RELIANCE"``) or Groww (``"NSE-RELIANCE"``) stock symbol.
    trade_date
        The IST trading day to verify.
    price_tolerance
        Per-field rupee OHLC tolerance; ``None`` → env → the UNVERIFIED default.
    volume_tolerance
        Whole-share volume tolerance; ``None`` → env → the UNVERIFIED default.
    series
        Bhavcopy series to match; ``None`` → env → ``EQ``.
    groww_fetcher, official_opener
        Injected fetchers; ``None`` selects the live defaults (``# pragma: no
        cover``). Tests pass fakes.
    spec
        Bhavcopy column spec; ``None`` → the env-resolved default.
    """
    price_tol = _resolve_price_tolerance(price_tolerance)
    volume_tol = _resolve_volume_tolerance(volume_tolerance)
    engine_symbol = normalize_equity_symbol(symbol)
    fetch = groww_fetcher if groww_fetcher is not None else _default_groww_equity_fetcher
    open_official = official_opener if official_opener is not None else _default_official_opener

    try:
        bars = fetch(engine_symbol, trade_date)
    except (BruteXError, OSError, ValueError, ImportError) as exc:
        return _unavailable(
            engine_symbol,
            trade_date,
            price_tol,
            volume_tol,
            f"Groww fetch failed: {exc}",
            official_source="nse",
        )
    groww_quote = quote_from_groww_equity_candles(bars, symbol=engine_symbol, trade_date=trade_date)
    if groww_quote is None:
        matched = sum(1 for bar in bars if bar.timestamp.astimezone(IST).date() == trade_date)
        reason = "no Groww 1-day bar" if matched == 0 else f"ambiguous: {matched} Groww 1-day bars"
        return _unavailable(
            engine_symbol,
            trade_date,
            price_tol,
            volume_tol,
            f"{reason} for {trade_date.isoformat()} IST",
            official_source="nse",
        )

    url = bhavcopy_url(trade_date)
    try:
        csv_text = open_official(url)
    except (OSError, ValueError) as exc:
        return _unavailable(
            engine_symbol,
            trade_date,
            price_tol,
            volume_tol,
            f"official bhavcopy fetch failed: {exc}",
            groww_source=groww_quote.source,
            official_source="nse",
        )
    official_quote = parse_bhavcopy_quote(
        csv_text, symbol=engine_symbol, trade_date=trade_date, series=series, spec=spec
    )
    if official_quote is None:
        return _unavailable(
            engine_symbol,
            trade_date,
            price_tol,
            volume_tol,
            f"official bhavcopy row for {engine_symbol!r} ({_equity_series()}) not found — "
            f"verify symbol/series/columns; set {EQUITY_CSV_SPEC_OVERRIDE_ENV} / "
            f"{BHAVCOPY_URL_OVERRIDE_ENV}",
            groww_source=groww_quote.source,
            official_source="nse",
        )
    return compare_equity_quotes(
        groww_quote,
        official_quote,
        price_tolerance=price_tol,
        volume_tolerance=volume_tol,
    )


__all__ = [
    "BHAVCOPY_URL_OVERRIDE_ENV",
    "DEFAULT_EQUITY_CSV_SPEC",
    "DEFAULT_EQUITY_SERIES",
    "DEFAULT_PRICE_TOLERANCE_RUPEES_UNVERIFIED",
    "DEFAULT_VOLUME_TOLERANCE_UNVERIFIED",
    "EQUITY_CSV_SPEC_OVERRIDE_ENV",
    "EQUITY_SERIES_OVERRIDE_ENV",
    "PRICE_TOLERANCE_OVERRIDE_ENV",
    "VOLUME_TOLERANCE_OVERRIDE_ENV",
    "EquityCsvSpec",
    "EquityQuote",
    "EquityVerdict",
    "GrowwEquityFetcher",
    "OfficialOpener",
    "VolumeComparison",
    "bhavcopy_url",
    "compare_equity_quotes",
    "normalize_equity_symbol",
    "parse_bhavcopy_quote",
    "quote_from_groww_equity_candles",
    "validate_equity_oneday",
]
