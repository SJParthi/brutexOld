"""One-day OHLC cross-check — Groww ``1day`` candles vs. NSE/BSE official OHLC.

DEC-NSE-1DAY-VALIDATION-001. Before any full-history Groww pull is scaled, the
operator wants ONE day's index OHLC cross-verified against an INDEPENDENT
official source, so a systematic vendor data mismatch (rounding, timezone,
adjusted-vs-raw, wrong-symbol) is caught on day one rather than after a
multi-year pull. This module is that harness for the three brute-force indices:
``NSE-NIFTY``, ``NSE-BANKNIFTY`` and ``BSE-SENSEX``.

## Honesty (CLAUDE.md Golden Rule #1 / #10)

* **No hallucinated OHLC.** This module NEVER hardcodes a price. Every number in
  a verdict comes from the injected fetchers (Groww + the official opener). The
  ONLY data baked into code is the index → source MAPPING, and it is DOCUMENTED
  "verify against NSE/BSE; may change" + env-overridable (mirrors
  :mod:`brutex.dashboard.nse_constituents`'s ``INDEX_CSV_FILENAMES`` override).
* **SENSEX is a BSE index — NOT on NSE.** It therefore has NO baked NSE-style
  default source URL: ``BSE-SENSEX`` resolves to a ``None`` URL and the verdict
  degrades to ``unavailable`` with a note telling the operator to supply a
  verified URL via the override env. We will not fabricate a BSE source we
  cannot verify (this build environment network-blocks + bot-blocks both hosts).
* **Tolerance is UNVERIFIED.** The true Groww↔official 2-dp rounding delta is an
  OPEN question — discovering it is the whole point of this harness. The default
  tolerance (:data:`DEFAULT_TOLERANCE_RUPEES_UNVERIFIED`) carries ``_UNVERIFIED``
  in its name so it can never be mistaken for a measured fact. A verdict reports
  the **signed per-field paisa delta** as its primary output; the tolerance only
  labels each field matched/mismatched. The operator tunes it from real
  transcripts (``--tolerance`` / the override env), never the reverse.

## Pure vs. live split (so 100% of the logic is testable)

Every parse / map / compare function below is **pure** (no I/O) and 100%
unit-testable offline. The two ``# pragma: no cover`` functions —
:func:`_default_groww_fetcher` and :func:`_default_official_opener` — are the
only *live* surfaces (real Groww client + real NSE/BSE urllib), which CI cannot
exercise. The live entrypoints (:func:`validate_index_oneday` /
:func:`validate_brute_force_indices`) take BOTH openers injected, so tests fake
them; the defaults are the live fetchers.

## Reliability — degrade, never crash (CLAUDE.md §13 priority law)

The pure functions return ``None`` for a not-found / malformed input; they never
swallow real errors. ONLY the live entrypoints wrap the two injected fetchers in
a try/except that maps ANY network block / timeout / vendor error / malformed
payload to an honest ``unavailable`` verdict — a backtest harness must never
crash an operator's terminal.

## O(1) honesty (Golden Rule #5 / #10)

:func:`compare_quotes` (4 fixed fields) and :func:`resolve_index_source` (two
dict lookups) are O(1). Parsing the official CSV is O(rows); the network fetch is
bounded latency, **NOT O(1)-total** — this is an offline operator verification
tool, never a sweep hot path, so it is exempt from the per-bar O(1) ratio gate.
"""

from __future__ import annotations

import csv
import io
import json
import os
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Final

from brutex.calendars import IST
from brutex.data.candle_fields import CandleParseError, to_price
from brutex.data.models import CandleBar
from brutex.models import BruteXError
from brutex.money import round_money
from brutex.universe import BRUTE_FORCE_INDICES

# ────────────────────────────────────────────────────────────────────────────
# Constants + env-overridable knobs
# ────────────────────────────────────────────────────────────────────────────

#: Base URL for the NSE official daily-indices OHLC CSV. Overridable via
#: ``BRUTEX_NSE_OFFICIAL_BASE_URL`` so an operator behind a mirror/proxy can
#: repoint it without a code change (CLAUDE.md §11 DYNAMIC). BEST-EFFORT — see
#: the module docstring (NSE bot-blocks + this build env network-blocks the host).
NSE_OFFICIAL_BASE_URL: Final[str] = os.environ.get(
    "BRUTEX_NSE_OFFICIAL_BASE_URL",
    "https://nsearchives.nseindia.com",
).rstrip("/")

#: Operator override env (DOCUMENTED "verify against NSE/BSE; may change"): a
#: JSON object ``{engine_symbol: url_template}`` OR ``{engine_symbol: {exchange,
#: groww_symbol, official_symbol, official_url_template}}`` MERGED over
#: :data:`DEFAULT_INDEX_SOURCES`, override winning. The common case is a bare
#: URL string (the verified official-CSV URL we will not bake) — e.g.
#: ``{"SENSEX": "https://.../{ddmmyyyy}.csv"}``. Malformed / absent → ignored.
INDEX_SOURCE_OVERRIDE_ENV: Final[str] = "BRUTEX_NSE_ONEDAY_SOURCES"

#: Operator override env for the official-CSV column labels (NSE/BSE may rename
#: them; verify). JSON object with any of the :class:`CsvQuoteSpec` field names.
CSV_QUOTE_SPEC_OVERRIDE_ENV: Final[str] = "BRUTEX_NSE_ONEDAY_CSV_SPEC"

#: Operator override env for the per-field rupee tolerance (a Decimal string).
TOLERANCE_OVERRIDE_ENV: Final[str] = "BRUTEX_NSE_ONEDAY_TOLERANCE"

#: Default per-field absolute tolerance, in rupees — **UNVERIFIED** (see the
#: module docstring's honesty note). ``0.01`` = exactly one 2-dp tick: both sides
#: are snapped to the 2-dp grid via :func:`~brutex.money.round_money`, so this
#: absorbs a single legitimate ``ROUND_HALF_UP`` boundary disagreement while
#: still flagging any genuine ≥2-tick divergence. The true vendor↔official delta
#: is an OPEN question this harness exists to measure.
DEFAULT_TOLERANCE_RUPEES_UNVERIFIED: Final[Decimal] = Decimal("0.01")

#: The four OHLC fields compared per quote — the single source of truth for the
#: compare + validation loops (CLAUDE.md §11 COMMON).
OHLC_FIELDS: Final[tuple[str, ...]] = ("open", "high", "low", "close")

#: Paisa per rupee — the signed-paisa delta scale (mirrors ``brutex.money``).
_PAISA_PER_RUPEE: Final[Decimal] = Decimal(100)

#: NSE daily-indices snapshot CSV URL template (BEST-EFFORT — verify; may change;
#: env-overridable). ``{ddmmyyyy}`` is substituted with the trade date. Same host
#: family the constituents helper already uses (``nsearchives.nseindia.com``).
_NSE_DAILY_INDICES_URL_TEMPLATE: Final[str] = (
    f"{NSE_OFFICIAL_BASE_URL}/content/indices/ind_close_all_{{ddmmyyyy}}.csv"
)


class VerdictStatus(StrEnum):
    """The honest top-level outcome of one index's one-day cross-check."""

    MATCHED = "matched"  #: All four OHLC fields within tolerance.
    MISMATCHED = "mismatched"  #: At least one field exceeds tolerance.
    UNAVAILABLE = "unavailable"  #: One/both sides unfetchable/unparsable (degrade).


@dataclass(frozen=True, slots=True)
class IndexSource:
    """Where + how to source one brute-force index's official 1-day OHLC.

    DOCUMENTED, env-overridable, **VERIFY AGAINST NSE/BSE — may change**; never a
    hallucinated URL. ``exchange`` / ``groww_symbol`` are HARD-derived from
    :data:`~brutex.universe.BRUTE_FORCE_INDICES`. ``official_url_template`` is
    ``None`` for any feed whose exact URL we cannot verify (e.g. ``BSE-SENSEX``)
    — that resolves to an ``unavailable`` verdict until the operator supplies a
    verified URL via :data:`INDEX_SOURCE_OVERRIDE_ENV`.

    Attributes
    ----------
    engine_symbol
        The bare engine symbol (``"NIFTY"`` / ``"BANKNIFTY"`` / ``"SENSEX"``).
    exchange
        ``"NSE"`` or ``"BSE"`` — SENSEX is BSE, the other two are NSE.
    groww_symbol
        The Groww symbol (``"NSE-NIFTY"`` / ``"BSE-SENSEX"``) the candle fetch uses.
    official_symbol
        The index's row label in the official CSV (best-effort; overridable).
    official_url_template
        A URL with a ``{ddmmyyyy}`` / ``{yyyymmdd}`` / ``{iso}`` date placeholder,
        or ``None`` when no verified official source is configured.
    """

    engine_symbol: str
    exchange: str
    groww_symbol: str
    official_symbol: str
    official_url_template: str | None


@dataclass(frozen=True, slots=True)
class CsvQuoteSpec:
    """Column labels for the official daily-OHLC CSV (one tolerant parser dialect).

    DOCUMENTED, env-overridable (:data:`CSV_QUOTE_SPEC_OVERRIDE_ENV`) — NSE/BSE
    may rename columns; verify. The defaults match the published NSE
    daily-indices snapshot convention; a wrong guess degrades to ``unavailable``
    (the row/columns are simply not found), never a false verdict.
    """

    index_name_column: str
    open_column: str
    high_column: str
    low_column: str
    close_column: str


#: Default official-CSV column labels — BEST-EFFORT (NSE daily-indices snapshot).
DEFAULT_CSV_QUOTE_SPEC: Final[CsvQuoteSpec] = CsvQuoteSpec(
    index_name_column="Index Name",
    open_column="Open Index Value",
    high_column="High Index Value",
    low_column="Low Index Value",
    close_column="Closing Index Value",
)


@dataclass(frozen=True, slots=True)
class OhlcQuote:
    """One index's one-day OHLC as 2-dp Decimal rupees + provenance.

    Both quote builders snap each price through :func:`~brutex.money.round_money`
    (``ROUND_HALF_UP`` — DEC-SPOT-PRICE-2DP-001), so vendor float-noise
    (``14020.7999``) can never leak into a verdict. Prices are deliberately
    UN-ordered-validated (we do NOT reject ``high < low`` official data — a
    disagreement surfaces as a per-field delta, never a crash). Non-finite
    prices ARE rejected — a NaN/Inf can never represent a real bar.

    Attributes
    ----------
    symbol
        The engine symbol this quote is for.
    trade_date
        The IST trading day this OHLC represents.
    open, high, low, close
        2-dp Decimal rupee prices.
    source
        Provenance label (``"groww"`` / ``"nse"`` / ``"bse"``) for the report.
    """

    symbol: str
    trade_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    source: str

    def __post_init__(self) -> None:
        """Reject a non-finite (NaN/Inf) price — it can never represent a real bar."""
        for name, value in (
            ("open", self.open),
            ("high", self.high),
            ("low", self.low),
            ("close", self.close),
        ):
            if not value.is_finite():
                raise ValueError(f"OhlcQuote.{name} must be a finite Decimal, got {value!r}")


@dataclass(frozen=True, slots=True)
class FieldComparison:
    """One OHLC field's two values + signed delta + within-tolerance flag.

    Attributes
    ----------
    field
        ``"open"`` / ``"high"`` / ``"low"`` / ``"close"``.
    groww, official
        The two 2-dp Decimal rupee values being compared.
    delta
        ``round_money(groww - official)`` — signed rupees (Groww minus official).
    delta_paisa
        The signed delta in whole paisa — the harness's primary honest output.
    matched
        ``abs(delta) <= tolerance`` (inclusive).
    """

    field: str
    groww: Decimal
    official: Decimal
    delta: Decimal
    delta_paisa: int
    matched: bool

    def to_dict(self) -> dict[str, object]:
        """JSON-safe view: the two prices + signed delta as 2-dp Decimal strings.

        Decimals serialise as strings (never floats) so the exact 2-dp rupee value
        survives the JSON boundary unrounded; ``delta_paisa`` stays an int.
        """
        return {
            "field": self.field,
            "groww": str(self.groww),
            "official": str(self.official),
            "delta": str(self.delta),
            "delta_paisa": self.delta_paisa,
            "matched": self.matched,
        }


@dataclass(frozen=True, slots=True)
class OneDayVerdict:
    """The honest per-index verdict: status + per-field deltas + provenance + note.

    Attributes
    ----------
    symbol
        The engine symbol.
    trade_date
        The IST trading day.
    status
        :class:`VerdictStatus` — matched / mismatched / unavailable.
    tolerance
        The per-field rupee tolerance actually applied (UNVERIFIED — see docs).
    fields
        Per-field comparisons (empty for an ``unavailable`` verdict).
    groww_source, official_source
        Provenance labels, or ``None`` when that side was never obtained.
    note
        A human reason on ``unavailable`` / ``mismatched`` (else ``None``).
    """

    symbol: str
    trade_date: date
    status: VerdictStatus
    tolerance: Decimal
    fields: tuple[FieldComparison, ...]
    groww_source: str | None
    official_source: str | None
    note: str | None

    @property
    def matched(self) -> bool:
        """True iff every field matched (status is :attr:`VerdictStatus.MATCHED`)."""
        return self.status is VerdictStatus.MATCHED

    @property
    def mismatched_fields(self) -> tuple[FieldComparison, ...]:
        """The fields whose delta exceeded tolerance (empty unless mismatched)."""
        return tuple(field for field in self.fields if not field.matched)

    def to_dict(self) -> dict[str, object]:
        """JSON-safe view of the verdict for the dashboard ``verify-nse`` route.

        ``trade_date`` → ISO string, ``status`` → its enum value, ``tolerance`` →
        a Decimal string (full precision preserved), ``fields`` → per-field
        :meth:`FieldComparison.to_dict`. The single source of truth for the
        verdict's wire shape (CLAUDE.md §11 COMMON).
        """
        return {
            "symbol": self.symbol,
            "trade_date": self.trade_date.isoformat(),
            "status": self.status.value,
            "tolerance": str(self.tolerance),
            "fields": [field.to_dict() for field in self.fields],
            "groww_source": self.groww_source,
            "official_source": self.official_source,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class BatchReport:
    """The honest roll-up over a set of indices for one trade date."""

    trade_date: date
    verdicts: tuple[OneDayVerdict, ...]

    @property
    def matched(self) -> int:
        """Count of indices whose OHLC matched within tolerance."""
        return sum(1 for v in self.verdicts if v.status is VerdictStatus.MATCHED)

    @property
    def mismatched(self) -> int:
        """Count of indices with at least one out-of-tolerance field."""
        return sum(1 for v in self.verdicts if v.status is VerdictStatus.MISMATCHED)

    @property
    def unavailable(self) -> int:
        """Count of indices that could not be cross-checked (degraded)."""
        return sum(1 for v in self.verdicts if v.status is VerdictStatus.UNAVAILABLE)


# ────────────────────────────────────────────────────────────────────────────
# Index → source mapping (PURE; env-overridable)
# ────────────────────────────────────────────────────────────────────────────

#: Per-symbol official metadata overlaid on the universe-derived facts. NSE
#: indices get a BEST-EFFORT documented URL template; BSE-SENSEX gets ``None``
#: (no verified NSE-style daily-OHLC source — operator supplies via the override
#: env). VERIFY AGAINST NSE/BSE; may change.
_OFFICIAL_METADATA: Final[dict[str, tuple[str, str | None]]] = {
    "NIFTY": ("Nifty 50", _NSE_DAILY_INDICES_URL_TEMPLATE),
    "BANKNIFTY": ("Nifty Bank", _NSE_DAILY_INDICES_URL_TEMPLATE),
    "SENSEX": ("SENSEX", None),
}


def _normalize_symbol(symbol: str) -> str:
    """Normalize a symbol to its bare engine form (``"NSE-NIFTY"`` → ``"NIFTY"``)."""
    key = symbol.strip().upper()
    if "-" in key:
        key = key.split("-", 1)[1]
    return key


def _build_default_sources() -> dict[str, IndexSource]:
    """Build the default index→source map from the universe + official metadata."""
    sources: dict[str, IndexSource] = {}
    for spec in BRUTE_FORCE_INDICES:
        engine = spec.underlying_symbol.upper()
        exchange = spec.exchange.value
        official_symbol, template = _OFFICIAL_METADATA.get(engine, (engine, None))
        sources[engine] = IndexSource(
            engine_symbol=engine,
            exchange=exchange,
            groww_symbol=f"{exchange}-{engine}",
            official_symbol=official_symbol,
            official_url_template=template,
        )
    return sources


#: Default index→source map for the 3 brute-force indices, keyed by engine symbol.
DEFAULT_INDEX_SOURCES: Final[dict[str, IndexSource]] = _build_default_sources()


def _override_str(value: object, default: str) -> str:
    """Return *value* trimmed when a non-blank string, else *default* (→ str)."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return default


def _override_optional_str(value: object, default: str | None) -> str | None:
    """Return *value* trimmed when a non-blank string, else *default* (→ str|None)."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return default


def _parse_source_entry(key: object, value: object) -> IndexSource | None:
    """Parse one operator override entry → :class:`IndexSource` (pure, never raises).

    *value* may be a bare URL-template string (the common case — just the
    verified URL) or an object with any of the :class:`IndexSource` fields;
    missing fields fall back to the symbol's built-in default (or a derivation).
    Returns ``None`` for a non-string key, an unusable value, or a blank symbol.
    """
    if not isinstance(key, str):
        return None
    engine = _normalize_symbol(key)
    if not engine:
        return None
    if isinstance(value, str):
        fields: dict[str, object] = {"official_url_template": value}
    elif isinstance(value, dict):
        fields = value
    else:
        return None
    base = DEFAULT_INDEX_SOURCES.get(engine)
    exchange = _override_str(fields.get("exchange"), base.exchange if base else "NSE")
    return IndexSource(
        engine_symbol=engine,
        exchange=exchange,
        groww_symbol=_override_str(
            fields.get("groww_symbol"), base.groww_symbol if base else f"{exchange}-{engine}"
        ),
        official_symbol=_override_str(
            fields.get("official_symbol"), base.official_symbol if base else engine
        ),
        official_url_template=_override_optional_str(
            fields.get("official_url_template"), base.official_url_template if base else None
        ),
    )


def _override_sources() -> dict[str, IndexSource]:
    """Parse the operator's JSON index→source override from the env — pure, never raises.

    Reads :data:`INDEX_SOURCE_OVERRIDE_ENV`. Returns ``{}`` when absent, empty,
    not valid JSON, or not a JSON object; within a valid object only well-formed
    entries are kept. A lost / malformed override costs only the extra indices,
    never a crash (reliability) — exactly like the constituents-helper rule.
    """
    raw = os.environ.get(INDEX_SOURCE_OVERRIDE_ENV)
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    out: dict[str, IndexSource] = {}
    for key, value in parsed.items():
        source = _parse_source_entry(key, value)
        if source is not None:
            out[source.engine_symbol] = source
    return out


def resolve_index_source(symbol: str) -> IndexSource | None:
    """Resolve an engine/groww symbol to its :class:`IndexSource` (override wins).

    Case-insensitive O(1) lookup (two dict ``get``s) against the operator
    override MERGED over :data:`DEFAULT_INDEX_SOURCES`. Accepts both the bare
    engine form (``"NIFTY"``) and the Groww form (``"NSE-NIFTY"``). Returns
    ``None`` for an index with no built-in and no override mapping.
    """
    key = _normalize_symbol(symbol)
    return _override_sources().get(key) or DEFAULT_INDEX_SOURCES.get(key)


def official_csv_url(source: IndexSource, trade_date: date) -> str | None:
    """Render *source*'s official-CSV URL for *trade_date* (pure).

    Substitutes the ``{ddmmyyyy}`` / ``{yyyymmdd}`` / ``{iso}`` date placeholders.
    Returns ``None`` when ``source.official_url_template`` is ``None`` (no
    verified official source configured → the caller degrades to ``unavailable``).
    """
    template = source.official_url_template
    if template is None:
        return None
    return (
        template.replace("{ddmmyyyy}", trade_date.strftime("%d%m%Y"))
        .replace("{yyyymmdd}", trade_date.strftime("%Y%m%d"))
        .replace("{iso}", trade_date.isoformat())
    )


# ────────────────────────────────────────────────────────────────────────────
# Official CSV parsing (PURE)
# ────────────────────────────────────────────────────────────────────────────


def _csv_quote_spec() -> CsvQuoteSpec:
    """Resolve the official-CSV column spec (operator override merged over default)."""
    raw = os.environ.get(CSV_QUOTE_SPEC_OVERRIDE_ENV)
    if not raw:
        return DEFAULT_CSV_QUOTE_SPEC
    try:
        parsed = json.loads(raw)
    except ValueError:
        return DEFAULT_CSV_QUOTE_SPEC
    if not isinstance(parsed, dict):
        return DEFAULT_CSV_QUOTE_SPEC
    default = DEFAULT_CSV_QUOTE_SPEC
    return CsvQuoteSpec(
        index_name_column=_override_str(parsed.get("index_name_column"), default.index_name_column),
        open_column=_override_str(parsed.get("open_column"), default.open_column),
        high_column=_override_str(parsed.get("high_column"), default.high_column),
        low_column=_override_str(parsed.get("low_column"), default.low_column),
        close_column=_override_str(parsed.get("close_column"), default.close_column),
    )


def _resolve_columns(fieldnames: Sequence[str], spec: CsvQuoteSpec) -> dict[str, str] | None:
    """Map the logical columns to the CSV's actual headers (whitespace/case tolerant).

    Returns ``{"index","open","high","low","close"} -> actual_header`` or ``None``
    if any required column is absent (→ the caller degrades to ``unavailable``).
    """
    by_norm = {name.strip().casefold(): name for name in fieldnames}
    wanted = {
        "index": spec.index_name_column,
        "open": spec.open_column,
        "high": spec.high_column,
        "low": spec.low_column,
        "close": spec.close_column,
    }
    resolved: dict[str, str] = {}
    for logical, label in wanted.items():
        actual = by_norm.get(label.strip().casefold())
        if actual is None:
            return None
        resolved[logical] = actual
    return resolved


def parse_official_quote(
    csv_text: str,
    *,
    source: IndexSource,
    trade_date: date,
    spec: CsvQuoteSpec | None = None,
) -> OhlcQuote | None:
    """Parse the official daily CSV → the :class:`OhlcQuote` for *source* (pure).

    Finds the row whose index-name column matches ``source.official_symbol``
    (case-insensitive, trimmed), reads its O/H/L/C via *spec*'s columns
    (defaulting to the env-resolved spec), parses each through the strict
    :func:`~brutex.data.candle_fields.to_price` (rejects non-finite) then snaps to
    2-dp via :func:`~brutex.money.round_money`. Returns ``None`` — never raises —
    for: empty text, no header, a missing required column, the target row absent,
    or a malformed/non-finite price in the matched row. The numbers come ONLY
    from *csv_text* (Golden Rule #1 — never fabricated).
    """
    resolved_spec = spec if spec is not None else _csv_quote_spec()
    reader = csv.DictReader(io.StringIO(csv_text))
    if reader.fieldnames is None:
        return None
    columns = _resolve_columns(reader.fieldnames, resolved_spec)
    if columns is None:
        return None
    target = source.official_symbol.strip().casefold()
    for row in reader:
        if (row.get(columns["index"]) or "").strip().casefold() != target:
            continue
        try:
            return OhlcQuote(
                symbol=source.engine_symbol,
                trade_date=trade_date,
                open=round_money(to_price(row.get(columns["open"]), "open")),
                high=round_money(to_price(row.get(columns["high"]), "high")),
                low=round_money(to_price(row.get(columns["low"]), "low")),
                close=round_money(to_price(row.get(columns["close"]), "close")),
                source=source.exchange.lower(),
            )
        except CandleParseError:
            return None  # found the row but a price is malformed/non-finite → unavailable
    return None


# ────────────────────────────────────────────────────────────────────────────
# Groww candle → quote (PURE; IST-date selection)
# ────────────────────────────────────────────────────────────────────────────


def _bars_on_ist_date(bars: Sequence[CandleBar], trade_date: date) -> list[CandleBar]:
    """Return the bars whose IST-LOCAL calendar date equals *trade_date*.

    ``CandleBar.timestamp`` is stored UTC (Groww IST→UTC); a midnight-IST daily
    bar lands on the PREVIOUS UTC date, so we convert back to IST before matching
    — selecting by the raw UTC date would pick the wrong day. ``IST`` is the
    canonical :data:`brutex.calendars.IST` zone (CLAUDE.md §11 COMMON — one
    source of truth, never a re-declared offset).
    """
    return [bar for bar in bars if bar.timestamp.astimezone(IST).date() == trade_date]


def _quote_from_bar(bar: CandleBar, symbol: str, trade_date: date) -> OhlcQuote:
    """Snap one :class:`CandleBar`'s O/H/L/C to a 2-dp :class:`OhlcQuote`."""
    return OhlcQuote(
        symbol=symbol,
        trade_date=trade_date,
        open=round_money(bar.open),
        high=round_money(bar.high),
        low=round_money(bar.low),
        close=round_money(bar.close),
        source="groww",
    )


def quote_from_groww_candles(
    bars: Sequence[CandleBar], *, symbol: str, trade_date: date
) -> OhlcQuote | None:
    """Build the one-day :class:`OhlcQuote` for *trade_date* from Groww candles (pure).

    Selects the bar whose IST calendar date equals *trade_date* (see
    :func:`_bars_on_ist_date`). Returns the snapped quote ONLY when exactly one
    bar matches; ``None`` for zero (holiday / no-trade / unlisted) or for an
    ambiguous >1 (never silently picks one). For a ``1day`` pull the match is a
    single bar.
    """
    matched = _bars_on_ist_date(bars, trade_date)
    if len(matched) != 1:
        return None
    return _quote_from_bar(matched[0], symbol, trade_date)


# ────────────────────────────────────────────────────────────────────────────
# Compare → verdict (PURE)
# ────────────────────────────────────────────────────────────────────────────


def _resolve_tolerance(explicit: Decimal | None) -> Decimal:
    """Resolve the per-field tolerance: explicit > env > the UNVERIFIED default.

    A non-finite or negative env value is ignored (degrade to the default). Pure
    apart from the single env read (the entrypoint knob), never raises.
    """
    if explicit is not None:
        return explicit
    raw = os.environ.get(TOLERANCE_OVERRIDE_ENV)
    if raw:
        try:
            value = Decimal(raw)
        except InvalidOperation:
            return DEFAULT_TOLERANCE_RUPEES_UNVERIFIED
        if value.is_finite() and value >= 0:
            return value
    return DEFAULT_TOLERANCE_RUPEES_UNVERIFIED


def _mismatch_note(fields: Sequence[FieldComparison], tolerance: Decimal) -> str:
    """Honest one-line summary of a mismatch (signed paisa; tolerance UNVERIFIED)."""
    bad = [field for field in fields if not field.matched]
    worst = max(abs(field.delta_paisa) for field in bad)
    names = ",".join(field.field for field in bad)
    return (
        f"{len(bad)} field(s) [{names}] exceed tolerance ₹{tolerance}; "
        f"max |Δ|={worst} paisa (tolerance is UNVERIFIED — see module docs)"
    )


def compare_quotes(
    groww: OhlcQuote,
    official: OhlcQuote,
    *,
    tolerance: Decimal = DEFAULT_TOLERANCE_RUPEES_UNVERIFIED,
) -> OneDayVerdict:
    """Compare two :class:`OhlcQuote`\\ s field-by-field → :class:`OneDayVerdict` (pure).

    Each of :data:`OHLC_FIELDS` yields a :class:`FieldComparison` with the signed
    ``delta = round_money(groww - official)`` (and its whole-paisa form);
    ``matched`` iff ``abs(delta) <= tolerance`` (inclusive). The verdict is
    ``MATCHED`` iff every field matched, else ``MISMATCHED`` with an honest note.
    """
    comparisons: list[FieldComparison] = []
    all_matched = True
    for field_name in OHLC_FIELDS:
        groww_value: Decimal = getattr(groww, field_name)
        official_value: Decimal = getattr(official, field_name)
        delta = round_money(groww_value - official_value)
        matched = abs(delta) <= tolerance
        all_matched = all_matched and matched
        comparisons.append(
            FieldComparison(
                field=field_name,
                groww=groww_value,
                official=official_value,
                delta=delta,
                delta_paisa=int(delta * _PAISA_PER_RUPEE),
                matched=matched,
            )
        )
    fields = tuple(comparisons)
    status = VerdictStatus.MATCHED if all_matched else VerdictStatus.MISMATCHED
    return OneDayVerdict(
        symbol=groww.symbol,
        trade_date=groww.trade_date,
        status=status,
        tolerance=tolerance,
        fields=fields,
        groww_source=groww.source,
        official_source=official.source,
        note=None if all_matched else _mismatch_note(fields, tolerance),
    )


def _unavailable(
    symbol: str,
    trade_date: date,
    tolerance: Decimal,
    note: str,
    *,
    groww_source: str | None = None,
    official_source: str | None = None,
) -> OneDayVerdict:
    """Build an ``UNAVAILABLE`` verdict carrying the honest *note* (no fields)."""
    return OneDayVerdict(
        symbol=symbol,
        trade_date=trade_date,
        status=VerdictStatus.UNAVAILABLE,
        tolerance=tolerance,
        fields=(),
        groww_source=groww_source,
        official_source=official_source,
        note=note,
    )


def render_report(report: BatchReport) -> str:
    """Render a :class:`BatchReport` as a deterministic plain-text block (pure)."""
    lines = [
        f"NSE/BSE 1-day OHLC cross-check — {report.trade_date.isoformat()} IST",
        f"  matched={report.matched} mismatched={report.mismatched} "
        f"unavailable={report.unavailable}",
        "",
    ]
    for verdict in report.verdicts:
        lines.append(f"[{verdict.status.value.upper()}] {verdict.symbol}")
        for field in verdict.fields:
            flag = "ok" if field.matched else "DIFF"
            lines.append(
                f"    {field.field:<5} groww={field.groww} official={field.official} "
                f"Δ={field.delta} ({field.delta_paisa:+d}p) [{flag}]"
            )
        if verdict.note is not None:
            lines.append(f"    note: {verdict.note}")
    return "\n".join(lines)


# ────────────────────────────────────────────────────────────────────────────
# Default LIVE openers — the ONLY ``# pragma: no cover`` surfaces
# ────────────────────────────────────────────────────────────────────────────

#: Injected Groww fetcher: ``(source, trade_date) -> raw candle bars``. The
#: IST-date selection stays in the PURE :func:`quote_from_groww_candles`, so the
#: fetcher returns raw bars (never an already-selected quote).
GrowwBarFetcher = Callable[[IndexSource, date], Sequence[CandleBar]]
#: Injected official opener: ``(url) -> csv_text`` (mirrors the constituents opener).
OfficialOpener = Callable[[str], str]

#: Bounded retry budget for the best-effort official fetch (mirror nse_constituents).
_FETCH_MAX_RETRIES: Final[int] = 3
#: Read/connect timeout (seconds) for the official fetch.
_FETCH_TIMEOUT_SECONDS: Final[int] = 30
#: Days BEFORE the trade date the live Groww window starts (absorb the IST→UTC
#: shift + a weekend so the target IST day is always inside the pulled window).
_GROWW_WINDOW_BACK_DAYS: Final[int] = 4


def _default_official_opener(url: str) -> str:  # pragma: no cover - live network only
    """Fetch *url* and return its text body (the real urllib opener).

    LIVE ONLY: NSE/BSE bot-block these endpoints and this build environment
    network-blocks both hosts, so this cannot be exercised in CI. A browser-like
    ``User-Agent`` + a generous timeout + a few bounded retries give the
    best-effort fetch its best chance on the operator's machine. Callers inject a
    fake ``opener`` in tests. Mirrors ``nse_constituents._default_opener``.
    """
    import time

    request = urllib.request.Request(  # noqa: S310 - https NSE/BSE host, best-effort
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
            "Accept": "text/csv,application/csv,text/plain,*/*",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    last_exc: Exception | None = None
    for attempt in range(_FETCH_MAX_RETRIES):
        try:
            with urllib.request.urlopen(  # noqa: S310  # nosec B310
                request, timeout=_FETCH_TIMEOUT_SECONDS
            ) as response:
                body: bytes = response.read()
            return body.decode("utf-8")
        except OSError as exc:  # timeout / transport — retry the bounded budget
            last_exc = exc
            if attempt < _FETCH_MAX_RETRIES - 1:
                time.sleep(1.5 * (attempt + 1))
    raise last_exc if last_exc is not None else OSError("official fetch failed")


def _default_groww_fetcher(
    source: IndexSource, trade_date: date
) -> list[CandleBar]:  # pragma: no cover - live network only
    """Pull *source*'s ``1day`` Groww candles around *trade_date* (the real client).

    LIVE ONLY: builds a live :class:`~brutex.adapters.groww.client.GrowwClient`
    from the stored TOTP credentials, pulls a tight window (a few days around
    *trade_date* to absorb the IST→UTC shift + weekends) of ``1day`` candles, and
    parses them with ``drop_volume=True`` (indices carry no volume). Read-only
    use of the Groww adapter — never edits it. The single live Groww surface.
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
            exchange=source.exchange,
            segment="CASH",
            groww_symbol=source.groww_symbol,
            interval=groww_interval,
            start=window.start,
            end=window.end,
        )
        bars.extend(parse_candle_rows(payload, drop_volume=True))
    return bars


# ────────────────────────────────────────────────────────────────────────────
# Live entrypoints (openers injected; degrade-safe orchestration)
# ────────────────────────────────────────────────────────────────────────────

#: The default batch symbols (the 3 brute-force indices, universe order).
_DEFAULT_BATCH_SYMBOLS: Final[tuple[str, ...]] = tuple(
    spec.underlying_symbol for spec in BRUTE_FORCE_INDICES
)


def validate_index_oneday(
    symbol: str,
    trade_date: date,
    *,
    tolerance: Decimal | None = None,
    groww_fetcher: GrowwBarFetcher | None = None,
    official_opener: OfficialOpener | None = None,
    spec: CsvQuoteSpec | None = None,
) -> OneDayVerdict:
    """Cross-verify ONE index's Groww 1-day OHLC vs. the official feed for *trade_date*.

    Resolves the source (unknown / no-URL → ``unavailable``), fetches both sides
    via the INJECTED openers (defaults = the two live fetchers), and degrades to
    an honest ``unavailable`` verdict on ANY vendor error / network block /
    timeout / malformed payload — it never crashes. The actual comparison is the
    pure :func:`compare_quotes`. Tests inject fakes for both openers → 100%
    offline.

    Parameters
    ----------
    symbol
        An engine (``"NIFTY"``) or Groww (``"NSE-NIFTY"``) index symbol.
    trade_date
        The IST trading day to verify.
    tolerance
        Per-field rupee tolerance; ``None`` → env (:data:`TOLERANCE_OVERRIDE_ENV`)
        → the UNVERIFIED default.
    groww_fetcher, official_opener
        Injected fetchers; ``None`` selects the live defaults (``# pragma: no
        cover``). Tests pass fakes.
    spec
        Official-CSV column spec; ``None`` → the env-resolved default.
    """
    tol = _resolve_tolerance(tolerance)
    source = resolve_index_source(symbol)
    if source is None:
        return _unavailable(
            _normalize_symbol(symbol),
            trade_date,
            tol,
            f"unknown index {symbol!r} — no source mapping; set {INDEX_SOURCE_OVERRIDE_ENV}",
        )
    official_label = source.exchange.lower()
    url = official_csv_url(source, trade_date)
    if url is None:
        return _unavailable(
            source.engine_symbol,
            trade_date,
            tol,
            f"no official source URL for {source.groww_symbol} — set "
            f"{INDEX_SOURCE_OVERRIDE_ENV} (e.g. BSE-SENSEX needs an operator-verified URL)",
            official_source=official_label,
        )
    fetch = groww_fetcher if groww_fetcher is not None else _default_groww_fetcher
    open_official = official_opener if official_opener is not None else _default_official_opener

    try:
        bars = fetch(source, trade_date)
    except (BruteXError, OSError, ValueError, ImportError) as exc:
        # BruteXError covers GrowwError + CandleParseError without importing the
        # Groww adapter (keeps this validator import-light + decoupled from
        # structlog). Genuine Python bugs (Type/Key/Attribute) do NOT subclass
        # BruteXError and still propagate — fail-loud on a real defect.
        return _unavailable(
            source.engine_symbol,
            trade_date,
            tol,
            f"Groww fetch failed: {exc}",
            official_source=official_label,
        )
    groww_quote = quote_from_groww_candles(bars, symbol=source.engine_symbol, trade_date=trade_date)
    if groww_quote is None:
        matched = len(_bars_on_ist_date(bars, trade_date))
        reason = "no Groww 1-day bar" if matched == 0 else f"ambiguous: {matched} Groww 1-day bars"
        return _unavailable(
            source.engine_symbol,
            trade_date,
            tol,
            f"{reason} for {trade_date.isoformat()} IST",
            official_source=official_label,
        )

    try:
        csv_text = open_official(url)
    except (OSError, ValueError) as exc:
        return _unavailable(
            source.engine_symbol,
            trade_date,
            tol,
            f"official fetch failed: {exc}",
            groww_source=groww_quote.source,
            official_source=official_label,
        )
    official_quote = parse_official_quote(csv_text, source=source, trade_date=trade_date, spec=spec)
    if official_quote is None:
        return _unavailable(
            source.engine_symbol,
            trade_date,
            tol,
            f"official OHLC for {source.official_symbol!r} not found in source — verify "
            f"row/columns; set {INDEX_SOURCE_OVERRIDE_ENV} / {CSV_QUOTE_SPEC_OVERRIDE_ENV}",
            groww_source=groww_quote.source,
            official_source=official_label,
        )
    return compare_quotes(groww_quote, official_quote, tolerance=tol)


def validate_brute_force_indices(
    trade_date: date,
    *,
    symbols: Sequence[str] | None = None,
    tolerance: Decimal | None = None,
    groww_fetcher: GrowwBarFetcher | None = None,
    official_opener: OfficialOpener | None = None,
    spec: CsvQuoteSpec | None = None,
) -> BatchReport:
    """Cross-verify ALL brute-force indices for *trade_date* → an honest :class:`BatchReport`.

    Loops :func:`validate_index_oneday` over *symbols* (default: the 3 brute-force
    indices). Each index is independently degrade-safe, so one blocked source
    never sinks the batch.
    """
    chosen = symbols if symbols is not None else _DEFAULT_BATCH_SYMBOLS
    verdicts = tuple(
        validate_index_oneday(
            symbol,
            trade_date,
            tolerance=tolerance,
            groww_fetcher=groww_fetcher,
            official_opener=official_opener,
            spec=spec,
        )
        for symbol in chosen
    )
    return BatchReport(trade_date=trade_date, verdicts=verdicts)


# ────────────────────────────────────────────────────────────────────────────
# All-indices snapshot (NSE `ind_close_all` Route A) — the LIVE index universe
# ────────────────────────────────────────────────────────────────────────────
#
# The one-day verify above pulls only the 3 brute-force index ROWS out of the
# `ind_close_all_<DDMMYYYY>.csv` file. That same file is a single daily CSV that
# carries EVERY published NSE index as a row (broad-based + sectoral + thematic +
# strategy). This section exposes the file's full content — REFERENCE_ONLY
# (CLAUDE.md §3): index EOD context + the live index universe, NEVER an engine /
# 52-vocab input. The KEY INSIGHT (per the NSE index-data reference): the
# authoritative live index set is the file's own `Index Name` column — never
# hardcode it. Reuses this module's URL template + column spec + opener
# (CLAUDE.md §11 COMMON — no duplicated NSE plumbing).


@dataclass(frozen=True, slots=True)
class IndexSnapshotRow:
    """One index's one-day EOD OHLC from the NSE all-indices snapshot.

    REFERENCE_ONLY (CLAUDE.md §3) — index EOD context, never an engine/vocab input.
    Prices are 2-dp Decimal rupees (snapped via :func:`~brutex.money.round_money`,
    DEC-SPOT-PRICE-2DP-001). ``index_name`` is the file's ``Index Name`` column,
    trimmed (e.g. ``"Nifty 50"``). Index ``Volume`` is 0 (indices aren't traded),
    so it is deliberately NOT carried here.

    Attributes
    ----------
    index_name
        The published index name (the file's authoritative label).
    trade_date
        The IST trading day this snapshot row represents.
    open, high, low, close
        2-dp Decimal rupee index values.
    """

    index_name: str
    trade_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal


def daily_indices_url(trade_date: date) -> str:
    """Render the NSE all-indices snapshot CSV URL for *trade_date* (Route A).

    ONE CSV holds every published NSE index as a row. The date token is
    **``DDMMYYYY``** (NOT the bhavcopy's ``YYYYMMDD`` — easy to mix up). BEST-EFFORT
    + env-overridable via ``BRUTEX_NSE_OFFICIAL_BASE_URL`` (verify; NSE may change
    the path). Same host family the constituents helper already uses.
    """
    return _NSE_DAILY_INDICES_URL_TEMPLATE.format(ddmmyyyy=trade_date.strftime("%d%m%Y"))


def list_index_names(csv_text: str, *, spec: CsvQuoteSpec | None = None) -> list[str]:
    """Return the unique, sorted index names in an all-indices snapshot (pure).

    The reference's KEY INSIGHT: the authoritative live index set is the file's own
    ``Index Name`` column — derive it, never hardcode, so new/renamed indices flow
    through automatically. Blank names are dropped. Returns ``[]`` for empty /
    header-less text or a missing index-name column (degrade, never raise).
    """
    resolved_spec = spec if spec is not None else _csv_quote_spec()
    reader = csv.DictReader(io.StringIO(csv_text))
    if reader.fieldnames is None:
        return []
    columns = _resolve_columns(reader.fieldnames, resolved_spec)
    if columns is None:
        return []
    names = {(row.get(columns["index"]) or "").strip() for row in reader}
    names.discard("")
    return sorted(names)


def parse_index_snapshot(
    csv_text: str, trade_date: date, *, spec: CsvQuoteSpec | None = None
) -> list[IndexSnapshotRow]:
    """Parse an all-indices snapshot CSV → one :class:`IndexSnapshotRow` per index (pure).

    Reuses the SAME column resolution + strict price parse as
    :func:`parse_official_quote` (CLAUDE.md §11 COMMON). A row with a blank index
    name OR a malformed / non-finite OHLC value is SKIPPED — the whole file is
    never crashed by one bad row, and no number is ever fabricated (Golden Rule
    #1/#10). Returns ``[]`` for empty / header-less text or a missing required
    column. Prices are snapped to the 2-dp grid (``ROUND_HALF_UP``).
    """
    resolved_spec = spec if spec is not None else _csv_quote_spec()
    reader = csv.DictReader(io.StringIO(csv_text))
    if reader.fieldnames is None:
        return []
    columns = _resolve_columns(reader.fieldnames, resolved_spec)
    if columns is None:
        return []
    rows: list[IndexSnapshotRow] = []
    for row in reader:
        name = (row.get(columns["index"]) or "").strip()
        if not name:
            continue
        try:
            rows.append(
                IndexSnapshotRow(
                    index_name=name,
                    trade_date=trade_date,
                    open=round_money(to_price(row.get(columns["open"]), "open")),
                    high=round_money(to_price(row.get(columns["high"]), "high")),
                    low=round_money(to_price(row.get(columns["low"]), "low")),
                    close=round_money(to_price(row.get(columns["close"]), "close")),
                )
            )
        except CandleParseError:
            continue  # malformed/non-finite price → skip this row, never fabricate
    return rows


def fetch_index_snapshot(
    trade_date: date,
    *,
    official_opener: OfficialOpener | None = None,
    spec: CsvQuoteSpec | None = None,
) -> list[IndexSnapshotRow] | None:
    """Fetch + parse the NSE all-indices snapshot for *trade_date* (degrade-safe).

    Route A live entrypoint. The opener defaults to the live NSE opener
    (:func:`_default_official_opener`, ``# pragma: no cover`` — NSE bot-blocks +
    this build env network-blocks the host); tests inject a fake. A fetch failure
    (weekend/holiday ``404``, bot-block, timeout — all surface as ``OSError``)
    returns ``None`` (no snapshot that day), NEVER crashes the caller. On success
    returns the parsed rows (possibly ``[]`` if the file shape was unusable).
    """
    url = daily_indices_url(trade_date)
    opener = official_opener if official_opener is not None else _default_official_opener
    try:
        csv_text = opener(url)
    except OSError:
        return None
    return parse_index_snapshot(csv_text, trade_date, spec=spec)


__all__ = [
    "CSV_QUOTE_SPEC_OVERRIDE_ENV",
    "DEFAULT_CSV_QUOTE_SPEC",
    "DEFAULT_INDEX_SOURCES",
    "DEFAULT_TOLERANCE_RUPEES_UNVERIFIED",
    "INDEX_SOURCE_OVERRIDE_ENV",
    "NSE_OFFICIAL_BASE_URL",
    "OHLC_FIELDS",
    "TOLERANCE_OVERRIDE_ENV",
    "BatchReport",
    "CsvQuoteSpec",
    "FieldComparison",
    "GrowwBarFetcher",
    "IndexSource",
    "OfficialOpener",
    "OhlcQuote",
    "OneDayVerdict",
    "VerdictStatus",
    "compare_quotes",
    "official_csv_url",
    "parse_official_quote",
    "quote_from_groww_candles",
    "render_report",
    "resolve_index_source",
    "validate_brute_force_indices",
    "validate_index_oneday",
]
