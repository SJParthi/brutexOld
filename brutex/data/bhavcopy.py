"""NSE index daily-snapshot ("bhavcopy") fetch + parse + OHLC cross-check (G7).

DEC-DOCTOR-LIVE-PROOF-KIT-001, kit stage 8. CLAUDE.md's VERIFY-FIRST intent —
cross-check the derived spot ``1day`` bar against the official exchange
numbers — had ZERO implementation at origin/main (register row G7: it was
always a manual, untooled step). This module is the first real one: it fetches
the NSE indices daily closing snapshot from the public archive, parses the
per-index OHLC row, and compares it to a locally derived daily bar within the
2-decimal tick tolerance.

Source: ``https://archives.nseindia.com/content/indices/ind_close_all_DDMMYYYY.csv``
— the NSE "Indices daily snapshot" published after each trading day, carrying
one row per index with Open/High/Low/Close index values. **Honest envelope
(Golden Rule #1):** the exact column headers of that CSV are repo-UNVERIFIED
(the sandbox has no NSE network); the parser therefore matches headers
tolerantly (case/spacing-insensitive, keyword-based) and FAILS LOUD with the
observed header when nothing matches — it never guesses a value. The first
Mac run of ``brutex doctor --live`` records the real header to the transcript.

Only the two NSE brute-force indexes map to this archive (NIFTY 50 /
NIFTY BANK); BSE-SENSEX is a BSE index with a different archive — honestly
reported as unmapped, never silently passed.

SSRF discipline: the fetch refuses any URL whose host is not on the fixed NSE
archive allowlist (mirrors ``instrument_loader._check_url``). The HTTP getter
is injected (tests use fixtures; the real shim is the only uncovered line).
"""

from __future__ import annotations

import csv
import io
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Final
from urllib.parse import urlparse

from brutex.data.models import CandleBar
from brutex.models import BruteXError

#: Hosts the bhavcopy fetch may touch — the NSE public archive only.
BHAVCOPY_ALLOWED_HOSTS: Final[frozenset[str]] = frozenset(
    {"archives.nseindia.com", "nsearchives.nseindia.com"}
)

#: URL template of the NSE indices daily snapshot (``DDMMYYYY`` date stamp).
BHAVCOPY_URL_TEMPLATE: Final[str] = (
    "https://archives.nseindia.com/content/indices/ind_close_all_{stamp}.csv"
)

#: ``groww_symbol`` → NSE archive index name (normalised lowercase). Only NSE
#: indexes exist in this archive; BSE-SENSEX has no row here (honest unmapped).
GROWW_SYMBOL_TO_NSE_INDEX: Final[Mapping[str, str]] = {
    "NSE-NIFTY": "nifty 50",
    "NSE-BANKNIFTY": "nifty bank",
}

#: Default comparison tolerance — one 2-decimal tick (DEC-SPOT-PRICE-2DP-001).
DEFAULT_TICK_TOLERANCE: Final[Decimal] = Decimal("0.05")

#: An injected keyless HTTP text getter (mirrors ``instrument_loader.HttpGet``).
HttpGet = Callable[[str], str]

#: Whole-download timeout (seconds) for :func:`urllib_http_get_nse` (mirrors
#: ``instrument_loader._DOWNLOAD_TIMEOUT`` — the sibling Groww shim's bound).
_DOWNLOAD_TIMEOUT: Final[float] = 60.0


class BhavcopyError(BruteXError):
    """A bhavcopy fetch/parse failure (network, SSRF refusal, or header drift)."""


def bhavcopy_url(day: date) -> str:
    """The NSE indices daily-snapshot URL for *day* (``DDMMYYYY`` stamp)."""
    return BHAVCOPY_URL_TEMPLATE.format(stamp=day.strftime("%d%m%Y"))


def _check_url(url: str) -> None:
    """Refuse any URL off the NSE archive allowlist (SSRF discipline)."""
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in BHAVCOPY_ALLOWED_HOSTS:
        raise BhavcopyError(
            f"refusing non-allowlisted bhavcopy URL {url!r} — allowed hosts: "
            f"{sorted(BHAVCOPY_ALLOWED_HOSTS)} over https"
        )


def urllib_http_get_nse(url: str) -> str:  # pragma: no cover — real network shim
    """SSRF-guarded ``urllib`` GET for the NSE archive hosts (the live default getter).

    The NSE twin of :func:`brutex.adapters.groww.instrument_loader.urllib_http_get`
    — it REUSES this module's :func:`_check_url` / :data:`BHAVCOPY_ALLOWED_HOSTS`
    allowlist (never a copy, CLAUDE.md §11 COMMON), so the doctor kit's stage-8
    bhavcopy cross-check is never blocked by the GROWW shim's allowlist (which
    admits only ``growwapi-assets.groww.in`` — the mislabelled "external
    dependency" failure DEC-DOCTOR-KIT-HONESTY-001 closes). A plain browser-ish
    ``User-Agent`` is sent because the NSE archive is known to refuse default
    library agents; any network/HTTP failure propagates for the caller to map
    to its honest "cross-check skipped, NOT passed" WARN.
    """
    import urllib.request

    _check_url(url)
    request = urllib.request.Request(  # noqa: S310 — https + host allowlisted by _check_url
        url, headers={"User-Agent": "Mozilla/5.0 (BruteX doctor bhavcopy cross-check)"}
    )
    with urllib.request.urlopen(request, timeout=_DOWNLOAD_TIMEOUT) as resp:  # nosec B310 — https + host allowlisted by _check_url  # noqa: S310
        return str(resp.read().decode("utf-8"))


@dataclass(frozen=True, slots=True)
class IndexBhavcopyRow:
    """One index's official OHLC from the NSE daily snapshot."""

    index_name: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal


def _match_column(fieldnames: list[str], *keywords: str) -> str | None:
    """The first header containing ALL *keywords* (case/spacing-insensitive)."""
    for name in fieldnames:
        folded = name.strip().lower()
        if all(keyword in folded for keyword in keywords):
            return name
    return None


def _decimal_or_none(raw: str | None) -> Decimal | None:
    """Parse an archive numeric cell (tolerating thousands separators), else ``None``."""
    if raw is None:
        return None
    text = raw.strip().replace(",", "").replace('"', "")
    if not text or text == "-":
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def parse_index_bhavcopy(text: str) -> dict[str, IndexBhavcopyRow]:
    """Parse the indices snapshot CSV → ``{normalised index name: row}``.

    Header matching is keyword-based ("index name", "open", "high", "low",
    "clos") so archive cosmetic drift (spacing, "Closing Index Value" vs
    "Close") does not brick the check; a header where NO OHLC column matches
    raises :class:`BhavcopyError` naming the observed header (fail loud, never
    guess — Golden Rule #1). Rows with unparseable numerics are skipped.
    """
    reader = csv.DictReader(io.StringIO(text))
    fieldnames = [name for name in (reader.fieldnames or []) if name]
    name_col = _match_column(fieldnames, "index", "name")
    open_col = _match_column(fieldnames, "open")
    high_col = _match_column(fieldnames, "high")
    low_col = _match_column(fieldnames, "low")
    close_col = _match_column(fieldnames, "clos")
    if (
        name_col is None
        or open_col is None
        or high_col is None
        or low_col is None
        or close_col is None
    ):
        raise BhavcopyError(
            f"unrecognised NSE indices snapshot header {fieldnames!r} — expected columns "
            f"matching 'Index Name' + Open/High/Low/Clos* index values (archive drift?)"
        )
    rows: dict[str, IndexBhavcopyRow] = {}
    for row in reader:
        raw_name = row.get(name_col)
        if raw_name is None or not raw_name.strip():
            continue
        open_ = _decimal_or_none(row.get(open_col))
        high = _decimal_or_none(row.get(high_col))
        low = _decimal_or_none(row.get(low_col))
        close = _decimal_or_none(row.get(close_col))
        if open_ is None or high is None or low is None or close is None:
            continue
        rows[raw_name.strip().lower()] = IndexBhavcopyRow(
            index_name=raw_name.strip(), open=open_, high=high, low=low, close=close
        )
    return rows


def fetch_index_bhavcopy(
    http_get: HttpGet, day: date, *, url: str | None = None
) -> dict[str, IndexBhavcopyRow]:
    """Fetch + parse the NSE indices snapshot for *day* via the injected getter.

    A network failure propagates as whatever *http_get* raises (the kit stage
    maps it to the honest WARN "cross-check skipped, NOT passed"); an
    off-allowlist URL or an unrecognised header raises :class:`BhavcopyError`.
    """
    target = url if url is not None else bhavcopy_url(day)
    _check_url(target)
    return parse_index_bhavcopy(http_get(target))


def nse_index_name_for(groww_symbol: str) -> str | None:
    """The normalised NSE archive index name for *groww_symbol* (``None`` = unmapped)."""
    return GROWW_SYMBOL_TO_NSE_INDEX.get(groww_symbol)


@dataclass(frozen=True, slots=True)
class BhavcopyComparison:
    """Outcome of comparing a derived daily bar to the official exchange row."""

    matches: bool
    max_delta: Decimal
    deltas: Mapping[str, Decimal]
    tolerance: Decimal


def compare_daily_bar(
    bar: CandleBar,
    official: IndexBhavcopyRow,
    *,
    tolerance: Decimal = DEFAULT_TICK_TOLERANCE,
) -> BhavcopyComparison:
    """Compare a derived ``1day`` bar's OHLC to *official* within *tolerance*.

    A per-field absolute delta within one tick (default ``0.05``) is a match —
    the derived bar is resampled from vendor 1-minute bars, so exact byte
    equality with the exchange's official computation is not the contract; a
    larger delta means the vendor's 1-minute stream disagrees with the
    exchange (thinned / mis-stamped session bars upstream — register M12/M15).
    """
    deltas = {
        "open": abs(bar.open - official.open),
        "high": abs(bar.high - official.high),
        "low": abs(bar.low - official.low),
        "close": abs(bar.close - official.close),
    }
    max_delta = max(deltas.values())
    return BhavcopyComparison(
        matches=max_delta <= tolerance,
        max_delta=max_delta,
        deltas=deltas,
        tolerance=tolerance,
    )


__all__ = [
    "BHAVCOPY_ALLOWED_HOSTS",
    "BHAVCOPY_URL_TEMPLATE",
    "DEFAULT_TICK_TOLERANCE",
    "GROWW_SYMBOL_TO_NSE_INDEX",
    "BhavcopyComparison",
    "BhavcopyError",
    "IndexBhavcopyRow",
    "bhavcopy_url",
    "compare_daily_bar",
    "fetch_index_bhavcopy",
    "nse_index_name_for",
    "parse_index_bhavcopy",
    "urllib_http_get_nse",
]
