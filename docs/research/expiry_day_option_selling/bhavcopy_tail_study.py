"""Expiry-day option-selling TAIL STUDY harness over NSE F&O bhavcopy files.

Purpose
-------
Measure — from public NSE end-of-day derivatives bhavcopy files — the
per-expiry-day P&L distribution of three daily-bar-bracketable short-premium
variants on NIFTY weekly options, with per-leg stop-losses resolved under TWO
fill brackets:

* OPTIMISTIC (naive): a pierced SL fills AT the trigger price.
* PESSIMISTIC (the repo's DEC-FILL-WORST-CASE-ONLY-001 decision-bar law,
  MIGRATION.md #1825): a pierced SL fills at the day bar's adverse extreme
  (the HIGH, for a short option being bought back).

The spread between the two brackets on the same day IS the measured
naive-vs-honest gap this study exists to quantify.

Honest limits (Golden Rule #10)
-------------------------------
* DAILY bars only. A daily bar cannot order intraday events, so anything
  beyond bracket-able logic (trailing SL, take-profit, entry-time variants,
  re-entries) is OUT OF SCOPE here and requires 1-minute data (the Groww
  pull on the operator box).
* Bhavcopy OHLC are TRADED prices. A strike that never traded after the
  open carries stale extremes, so the measured tail is an UNDERSTATEMENT
  of the true adverse tail — stated in every output.
* The NSE archive URL stems + column layouts below are drawn from public
  documentation and could NOT be network-verified from the build container
  (org egress proxy blocks the NSE hosts — verified 2026-07-20). The
  parser is fixture-verified only. The FIRST run on an open-egress box
  MUST be ``--smoke <date>`` once per format era before the full pull.
* Costs are NOT applied by default (gross premium points): the repo's
  honest exchange-charge window starts 2024-10-01 (pre-boundary windows
  are explicit refusal rows — DEC-ETC-REGIME-HISTORY-001, MIGRATION.md
  #1524; see ``brutex/costs/constants.py``), and the tail conclusion is
  cost-insensitive (tail losses are orders of magnitude above per-trade
  costs). ``--costs post2024`` overlays verified costs on trades dated
  >= 2024-10-01 only and marks earlier trades ``cost_refused``.
* SENSEX is excluded pending a BSE bhavcopy probe.

Standalone-tool posture
-----------------------
This file lives under ``docs/research/`` (the house research-probe
precedent — see ``docs/groww/probe/postclose_settle_probe.py``): it is
stdlib-only, imports NOTHING from ``brutex/`` (values copied with
file:line citations instead), and is deliberately outside the repo's
coverage/mutation gates. Its offline test suite is
``tests_offline.py`` in this directory. CLI output uses a single
``print``-based helper (no structlog dependency in a standalone
operator tool).

Run (operator box — open egress)
--------------------------------
    python bhavcopy_tail_study.py --smoke 2024-06-06     # old-format probe
    python bhavcopy_tail_study.py --smoke 2025-03-06     # new-format probe
    python bhavcopy_tail_study.py --start 2019-02-01     # full pull
    python bhavcopy_tail_study.py --from-cache-only      # offline re-run
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
import time
import urllib.error
import urllib.request
import zipfile
from bisect import bisect_right
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Final, Literal

# ---------------------------------------------------------------------------
# Constants — URL stems, regimes, grids
# ---------------------------------------------------------------------------

#: Old NSE derivatives bhavcopy (pre ~July-2024).  UNVERIFIED-until-smoke:
#: drawn from public documentation; this container is egress-blocked to NSE.
OLD_URL_TEMPLATE: Final[str] = (
    "https://archives.nseindia.com/content/historical/DERIVATIVES/"
    "{yyyy}/{mon}/fo{dd}{mon}{yyyy}bhav.csv.zip"
)

#: New NSE UDiFF bhavcopy (from ~July-2024).  UNVERIFIED-until-smoke.
NEW_URL_TEMPLATE: Final[str] = (
    "https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{yyyymmdd}_F_0000.csv.zip"
)

#: Format-preference boundary.  The TRUE old->new cutover is UNVERIFIED
#: (approximately July-2024); before this date the OLD format is tried
#: first, from this date the NEW format is tried first — and on a 404 the
#: OTHER format is probed, so an inexact boundary only costs one extra
#: request per day near the cutover, never a lost day.
FORMAT_PREFERENCE_BOUNDARY: Final[date] = date(2024, 8, 1)

#: NIFTY weekly-expiry weekday regime boundary.  Thursday historically;
#: Tuesday after NSE moved its weekly expiry day in 2025.  The exact
#: effective date is UNVERIFIED from this container — encoded as
#: 2025-09-01 per public reporting; the holiday/absence walk-back below
#: self-heals a boundary error of a few weeks (a wrong candidate weekday
#: simply finds no expiring options and walks/logs honestly).
WEEKLY_EXPIRY_TUESDAY_FROM: Final[date] = date(2025, 9, 1)

_THURSDAY: Final[int] = 3  # date.weekday() value
_TUESDAY: Final[int] = 1

#: Walk BACK (earlier weekdays) at most this many steps from the candidate
#: expiry weekday when the candidate's file is absent (holiday) or holds
#: no NIFTY options expiring that day.
MAX_WALKBACK_STEPS: Final[int] = 3

_MONTH_ABBREVIATIONS: Final[tuple[str, ...]] = (
    "JAN",
    "FEB",
    "MAR",
    "APR",
    "MAY",
    "JUN",
    "JUL",
    "AUG",
    "SEP",
    "OCT",
    "NOV",
    "DEC",
)
_MONTH_BY_ABBREVIATION: Final[dict[str, int]] = {
    name: index + 1 for index, name in enumerate(_MONTH_ABBREVIATIONS)
}

_OLD_DATE_PARTS: Final[int] = 3  # DD-MMM-YYYY

#: Per-leg stop-loss grid: trigger = entry * (1 + sl_pct).
SL_GRID: Final[tuple[Decimal, ...]] = (
    Decimal("0.20"),
    Decimal("0.30"),
    Decimal("0.50"),
)

Bracket = Literal["optimistic", "pessimistic", "hold_to_close"]

_SL_BRACKETS: Final[tuple[Bracket, ...]] = ("optimistic", "pessimistic")

TABLE_FOOTER: Final[str] = (
    "_fills bracketed per DEC-FILL-WORST-CASE-ONLY-001 decision-bar law; "
    "daily-bar resolution; gross of costs (see README)._"
)


@dataclass(frozen=True)
class Variant:
    """One strategy variant of the daily-bar-bracketable grid."""

    key: str
    label: str
    #: Strike offset from the future-open spot proxy: 0 = ATM straddle
    #: (one common strike), 0.01 = +-1% strangle, 0.02 = +-2% strangle.
    offset_pct: Decimal


VARIANTS: Final[tuple[Variant, ...]] = (
    Variant("V1", "short ATM straddle", Decimal("0")),
    Variant("V2", "short strangle +-1%", Decimal("0.01")),
    Variant("V3", "short strangle +-2%", Decimal("0.02")),
)

# ---------------------------------------------------------------------------
# NIFTY lot-size history (per-lot rupee columns)
# ---------------------------------------------------------------------------
# Repo-sourced entries come verbatim from brutex/options/lot_size_history.py
# (_LOT_HISTORY_NIFTY, lines 58-64 at branch time).  The repo table starts
# 2021-01-01; this study starts 2019-02-01, so the FIRST span is extended
# back — EXTERNAL-UNVERIFIED: public history has NIFTY at lot 75 from the
# Nov-2015 SEBI minimum-contract-value revision through the Jul-2021
# rebasing, which is CONSISTENT with the repo's own "pre-Jul-2021 default
# 75" but was not independently re-verified from this container.

NIFTY_LOT_HISTORY: Final[tuple[tuple[date, int], ...]] = (
    (date(2019, 1, 1), 75),  # EXTERNAL-UNVERIFIED back-extension (see above)
    (date(2021, 7, 29), 50),  # repo lot_size_history.py:61 — Jul-2021 rebasing
    (date(2024, 4, 26), 25),  # repo lot_size_history.py:62 — NSE FAOP circular
    (date(2024, 11, 20), 75),  # repo lot_size_history.py:63 — SEBI Rs 15L rule
    (date(2026, 1, 1), 65),  # repo lot_size_history.py:64 — Jan-2026 circular
)


def nifty_lot_size(on_date: date) -> int:
    """Lot size in effect on ``on_date`` (latest transition <= date).

    Raises:
        ValueError: if ``on_date`` precedes the recorded history — a lot
            size is never fabricated (Golden Rule #1).
    """
    if on_date < NIFTY_LOT_HISTORY[0][0]:
        msg = (
            f"nifty_lot_size: {on_date} precedes recorded lot history "
            f"start {NIFTY_LOT_HISTORY[0][0]} — refusing to fabricate"
        )
        raise ValueError(msg)
    dates_only = [entry[0] for entry in NIFTY_LOT_HISTORY]
    return NIFTY_LOT_HISTORY[bisect_right(dates_only, on_date) - 1][1]


# ---------------------------------------------------------------------------
# Costs (--costs post2024) — verified repo rates, cited per constant
# ---------------------------------------------------------------------------
# All rates mirror brutex/costs/constants.py (line refs at branch time).
# DEVIATION FROM THE TASK BRIEF (recorded): the brief named sell-side STT
# 0.0625% for >=2024-10-01 trades, but the repo's verified table says
# 0.0625% is the PRE-2024-10-01 rate and 0.10% applies FROM 2024-10-01
# (constants.py:70-77, Finance Act 2024); the brief also says "verify from
# constants.py" — repo wins.  0.15% applies from 2026-04-01
# (constants.py:77,94 — Finance Act 2026).

COSTS_VERIFIED_START: Final[date] = date(2024, 10, 1)
"""Earlier trade dates are cost-REFUSED (marked ``cost_refused``): the
pre-boundary NSE exchange-charge windows are explicit refusal rows
(DEC-ETC-REGIME-HISTORY-001; constants.py:431-453)."""

BROKERAGE_RUPEES_PER_ORDER: Final[Decimal] = Decimal("20")
"""Groww flat Rs 20 per executed order (constants.py:45,
GROWW_BROKERAGE_PAISA = 2_000 paise)."""

STT_SELL_RATE_FROM_2024_10: Final[Decimal] = Decimal("0.0010")
"""STT 0.10% on sell-side options premium, 2024-10-01 to 2026-03-31
(constants.py:73, STT_OPTIONS_OCT_2024_TO_MAR_2026_BPS_X100 = 10_000)."""

STT_SELL_RATE_FROM_2026_04: Final[Decimal] = Decimal("0.0015")
"""STT 0.15% on sell-side options premium, from 2026-04-01
(constants.py:77, STT_OPTIONS_POST_APR_2026_BPS_X100 = 15_000)."""

STT_RATE_2026_BOUNDARY: Final[date] = date(2026, 4, 1)
"""Finance Act 2026 STT boundary (constants.py:94)."""

NSE_ETC_RATE: Final[Decimal] = Decimal("0.0003503")
"""NSE exchange transaction charge 0.03503% on options premium, BOTH
sides, effective 2024-10-01 (constants.py:150,
NSE_ETC_OPTIONS_BPS_X100 = 3_503; NSE circular FA64232)."""

_ORDERS_PER_TWO_LEG_VARIANT: Final[int] = 4
"""Two legs x (one sell entry + one buy exit) = 4 executed orders."""

_PAISA_QUANTUM: Final[Decimal] = Decimal("0.01")


def variant_costs_rupees(
    trade_date: date,
    entry_premia: Sequence[Decimal],
    exit_premia: Sequence[Decimal],
    lot_size: int,
) -> Decimal | None:
    """Per-variant round-trip costs in rupees, or ``None`` when refused.

    ``None`` == the trade date precedes :data:`COSTS_VERIFIED_START`
    (the honest refusal window — never a fabricated rate).

    Components (rates cited above): 4 x Rs 20 brokerage; STT on the
    SELL-side (entry) premium turnover; NSE ETC on premium turnover of
    BOTH sides.  Result is quantized to the paisa, ROUND_HALF_UP.
    """
    if trade_date < COSTS_VERIFIED_START:
        return None
    stt_rate = (
        STT_SELL_RATE_FROM_2026_04
        if trade_date >= STT_RATE_2026_BOUNDARY
        else STT_SELL_RATE_FROM_2024_10
    )
    lot = Decimal(lot_size)
    sell_turnover = sum(entry_premia, Decimal(0)) * lot
    both_sides_turnover = (sum(entry_premia, Decimal(0)) + sum(exit_premia, Decimal(0))) * lot
    brokerage = BROKERAGE_RUPEES_PER_ORDER * _ORDERS_PER_TWO_LEG_VARIANT
    total = brokerage + sell_turnover * stt_rate + both_sides_turnover * NSE_ETC_RATE
    return total.quantize(_PAISA_QUANTUM, rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# Parsed-row models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OptionRow:
    """One option row from a bhavcopy (already symbol-agnostic)."""

    symbol: str
    expiry: date
    strike: Decimal
    option_type: str  # "CE" | "PE"
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    settle: Decimal


@dataclass(frozen=True)
class FutureRow:
    """One index/stock future row from a bhavcopy."""

    symbol: str
    expiry: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    settle: Decimal


@dataclass(frozen=True)
class ParsedDay:
    """The screened contents of one day's bhavcopy.

    ``dropped_rows`` counts rows removed by the data-sanity screens
    (open <= 0, or high < low) — counted, never silently vanished.
    """

    futures: tuple[FutureRow, ...]
    options: tuple[OptionRow, ...]
    dropped_rows: int


def _dec(raw: str | None) -> Decimal:
    """Parse a bhavcopy numeric cell; blank/junk parses to 0 (screened later)."""
    if raw is None:
        return Decimal(0)
    text = raw.strip()
    if not text or text == "-":
        return Decimal(0)
    try:
        return Decimal(text)
    except InvalidOperation:
        return Decimal(0)


def _parse_old_expiry(raw: str) -> date | None:
    """Parse DD-MMM-YYYY (e.g. ``27-FEB-2019``) locale-independently.

    Deliberately NOT ``strptime %b`` — that is LC_TIME-locale-dependent
    (the repo's DEC-DATA-RUST-PURELAW-001 locale lesson); a hand month
    map is deterministic everywhere.
    """
    parts = raw.strip().upper().split("-")
    if len(parts) != _OLD_DATE_PARTS:
        return None
    day_text, mon_text, year_text = parts
    month = _MONTH_BY_ABBREVIATION.get(mon_text)
    if month is None:
        return None
    try:
        return date(int(year_text), month, int(day_text))
    except ValueError:
        return None


def _parse_iso_date(raw: str) -> date | None:
    try:
        return date.fromisoformat(raw.strip())
    except ValueError:
        return None


def _price_sane(open_: Decimal, high: Decimal, low: Decimal) -> bool:
    """The data-sanity screen: drop open <= 0 or high < low (counted)."""
    return open_ > 0 and high >= low


def parse_old_format(csv_bytes: bytes) -> ParsedDay:
    """Parse the pre-~Jul-2024 NSE derivatives bhavcopy CSV.

    Columns used: INSTRUMENT, SYMBOL, EXPIRY_DT (DD-MMM-YYYY), STRIKE_PR,
    OPTION_TYP (CE/PE/XX), OPEN, HIGH, LOW, CLOSE, SETTLE_PR.  Extra
    columns (CONTRACTS, OPEN_INT, TIMESTAMP, the trailing blank) are
    ignored.  Fixture-verified only — see the module honesty note.
    """
    futures: list[FutureRow] = []
    options: list[OptionRow] = []
    dropped = 0
    reader = csv.DictReader(io.StringIO(csv_bytes.decode("utf-8", errors="replace")))
    for row in reader:
        instrument = (row.get("INSTRUMENT") or "").strip().upper()
        if not instrument.startswith(("FUT", "OPT")):
            continue
        expiry = _parse_old_expiry(row.get("EXPIRY_DT") or "")
        if expiry is None:
            dropped += 1
            continue
        symbol = (row.get("SYMBOL") or "").strip().upper()
        open_ = _dec(row.get("OPEN"))
        high = _dec(row.get("HIGH"))
        low = _dec(row.get("LOW"))
        close = _dec(row.get("CLOSE"))
        settle = _dec(row.get("SETTLE_PR"))
        option_type = (row.get("OPTION_TYP") or "").strip().upper()
        if not _price_sane(open_, high, low):
            dropped += 1
            continue
        if instrument.startswith("OPT") and option_type in {"CE", "PE"}:
            options.append(
                OptionRow(
                    symbol=symbol,
                    expiry=expiry,
                    strike=_dec(row.get("STRIKE_PR")),
                    option_type=option_type,
                    open=open_,
                    high=high,
                    low=low,
                    close=close,
                    settle=settle,
                )
            )
        elif instrument.startswith("FUT"):
            futures.append(
                FutureRow(
                    symbol=symbol,
                    expiry=expiry,
                    open=open_,
                    high=high,
                    low=low,
                    close=close,
                    settle=settle,
                )
            )
    return ParsedDay(futures=tuple(futures), options=tuple(options), dropped_rows=dropped)


def parse_new_format(csv_bytes: bytes) -> ParsedDay:
    """Parse the ~Jul-2024+ NSE UDiFF bhavcopy CSV (subset of columns).

    Columns used: TckrSymb, FinInstrmTp (IDO/STO options, IDF/STF
    futures), XpryDt (YYYY-MM-DD), StrkPric, OptnTp, OpnPric, HghPric,
    LwPric, ClsPric, SttlmPric.  Fixture-verified only.
    """
    futures: list[FutureRow] = []
    options: list[OptionRow] = []
    dropped = 0
    reader = csv.DictReader(io.StringIO(csv_bytes.decode("utf-8", errors="replace")))
    for row in reader:
        instr_type = (row.get("FinInstrmTp") or "").strip().upper()
        if instr_type not in {"IDO", "STO", "IDF", "STF"}:
            continue
        expiry = _parse_iso_date(row.get("XpryDt") or "")
        if expiry is None:
            dropped += 1
            continue
        symbol = (row.get("TckrSymb") or "").strip().upper()
        open_ = _dec(row.get("OpnPric"))
        high = _dec(row.get("HghPric"))
        low = _dec(row.get("LwPric"))
        close = _dec(row.get("ClsPric"))
        settle = _dec(row.get("SttlmPric"))
        option_type = (row.get("OptnTp") or "").strip().upper()
        if not _price_sane(open_, high, low):
            dropped += 1
            continue
        if instr_type in {"IDO", "STO"} and option_type in {"CE", "PE"}:
            options.append(
                OptionRow(
                    symbol=symbol,
                    expiry=expiry,
                    strike=_dec(row.get("StrkPric")),
                    option_type=option_type,
                    open=open_,
                    high=high,
                    low=low,
                    close=close,
                    settle=settle,
                )
            )
        elif instr_type in {"IDF", "STF"}:
            futures.append(
                FutureRow(
                    symbol=symbol,
                    expiry=expiry,
                    open=open_,
                    high=high,
                    low=low,
                    close=close,
                    settle=settle,
                )
            )
    return ParsedDay(futures=tuple(futures), options=tuple(options), dropped_rows=dropped)


# ---------------------------------------------------------------------------
# Download / cache layer — the ONLY network-touching code paths
# ---------------------------------------------------------------------------

Fetcher = Callable[[str], bytes]
"""Injectable fetch seam: url -> raw response bytes; raises
:class:`FetchNotFound` on a 404.  Tests inject a dict-backed fetcher so
the network layer is provably never touched offline."""


class FetchNotFound(Exception):
    """The URL answered 404 (an NSE holiday / absent file)."""


_USER_AGENT: Final[str] = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

_ZIP_MAGIC: Final[bytes] = b"PK\x03\x04"


def make_http_fetcher(
    *,
    sleep_seconds: float = 0.75,
    retries: int = 3,
    timeout_seconds: float = 30.0,
) -> Fetcher:
    """Build the real network fetcher (polite: UA header, backoff, sleep).

    Sleeps ``sleep_seconds`` before EVERY request (pull etiquette against
    a public archive), retries transient failures ``retries`` times with
    exponential backoff, and raises :class:`FetchNotFound` on a 404
    immediately (no retry — a holiday is not transient).
    """

    def fetch(url: str) -> bytes:
        time.sleep(sleep_seconds)
        last_error: Exception | None = None
        for attempt in range(retries):
            request = urllib.request.Request(  # noqa: S310 — https-only NSE archive hosts (templates above)
                url, headers={"User-Agent": _USER_AGENT}
            )
            try:
                with urllib.request.urlopen(  # noqa: S310 — same https-only allowlist
                    request, timeout=timeout_seconds
                ) as response:
                    return bytes(response.read())
            except urllib.error.HTTPError as exc:
                if exc.code == 404:  # noqa: PLR2004 — HTTP status literal
                    raise FetchNotFound(url) from exc
                last_error = exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = exc
            time.sleep(2.0**attempt)
        assert last_error is not None  # noqa: S101 — loop ran >= 1 attempt
        raise last_error

    return fetch


def bhavcopy_url(trade_date: date, fmt: str) -> str:
    """Render the archive URL for a date in the given format era."""
    if fmt == "old":
        mon = _MONTH_ABBREVIATIONS[trade_date.month - 1]
        return OLD_URL_TEMPLATE.format(
            yyyy=f"{trade_date.year:04d}", mon=mon, dd=f"{trade_date.day:02d}"
        )
    if fmt == "new":
        return NEW_URL_TEMPLATE.format(yyyymmdd=trade_date.strftime("%Y%m%d"))
    msg = f"bhavcopy_url: unknown format {fmt!r}"
    raise ValueError(msg)


def format_preference(trade_date: date) -> tuple[str, str]:
    """Format try-order for a date (both are always probed on 404)."""
    if trade_date < FORMAT_PREFERENCE_BOUNDARY:
        return ("old", "new")
    return ("new", "old")


def unzip_first_csv(zip_bytes: bytes) -> bytes:
    """Extract the first ``.csv`` member (or the first member) of a zip."""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        names = archive.namelist()
        if not names:
            msg = "unzip_first_csv: empty zip archive"
            raise ValueError(msg)
        csv_names = [name for name in names if name.lower().endswith(".csv")]
        return archive.read(csv_names[0] if csv_names else names[0])


@dataclass(frozen=True)
class DayFile:
    """One successfully-loaded bhavcopy: which format era + the CSV bytes."""

    fmt: str
    csv_bytes: bytes


@dataclass
class DayLoader:
    """Cached, format-auto-selecting bhavcopy loader.

    Caches raw ZIP bytes per (date, format) under ``cache_dir`` so re-runs
    are fully offline; 404s are cached as ``.404`` marker files.  With
    ``from_cache_only=True`` (or ``fetch is None``) the network is never
    touched — un-cached days simply load as absent (counted upstream).
    """

    cache_dir: Path
    fetch: Fetcher | None
    from_cache_only: bool = False
    fetch_calls: int = 0

    def _zip_path(self, trade_date: date, fmt: str) -> Path:
        return self.cache_dir / f"fo_{trade_date.strftime('%Y%m%d')}_{fmt}.zip"

    def _marker_path(self, trade_date: date, fmt: str) -> Path:
        return self.cache_dir / f"fo_{trade_date.strftime('%Y%m%d')}_{fmt}.404"

    def load(self, trade_date: date) -> DayFile | None:
        """Load one day's file, trying both format eras; ``None`` = absent."""
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        for fmt in format_preference(trade_date):
            zip_path = self._zip_path(trade_date, fmt)
            if zip_path.is_file():
                return DayFile(fmt=fmt, csv_bytes=unzip_first_csv(zip_path.read_bytes()))
            if self._marker_path(trade_date, fmt).is_file():
                continue
            if self.from_cache_only or self.fetch is None:
                continue
            try:
                self.fetch_calls += 1
                payload = self.fetch(bhavcopy_url(trade_date, fmt))
            except FetchNotFound:
                self._marker_path(trade_date, fmt).write_bytes(b"")
                continue
            if not payload.startswith(_ZIP_MAGIC):
                # An HTML error/interstitial page served with HTTP 200 —
                # treated as absent WITHOUT a marker (could be transient
                # bot-gating; a re-run retries it).
                _say(f"  WARNING: non-zip payload for {trade_date} [{fmt}] — skipped")
                continue
            zip_path.write_bytes(payload)
            return DayFile(fmt=fmt, csv_bytes=unzip_first_csv(payload))
        return None


def parse_day_file(day_file: DayFile) -> ParsedDay:
    """Dispatch a loaded file to its format parser."""
    if day_file.fmt == "old":
        return parse_old_format(day_file.csv_bytes)
    return parse_new_format(day_file.csv_bytes)


# ---------------------------------------------------------------------------
# Expiry-day discovery
# ---------------------------------------------------------------------------


def weekly_expiry_candidate(week_monday: date) -> date:
    """The candidate NIFTY weekly-expiry date for the week of ``week_monday``.

    Thursday for weeks whose Thursday precedes
    :data:`WEEKLY_EXPIRY_TUESDAY_FROM`, Tuesday thereafter.
    """
    thursday = week_monday + timedelta(days=_THURSDAY)
    if thursday >= WEEKLY_EXPIRY_TUESDAY_FROM:
        return week_monday + timedelta(days=_TUESDAY)
    return thursday


def _previous_weekday(day: date) -> date:
    out = day - timedelta(days=1)
    while out.weekday() >= 5:  # noqa: PLR2004 — 5/6 are Sat/Sun
        out -= timedelta(days=1)
    return out


@dataclass(frozen=True)
class ExpiryDiscovery:
    """Outcome of expiry-day discovery for one ISO week."""

    week_monday: date
    expiry_date: date | None
    parsed: ParsedDay | None
    fmt: str | None


def discover_expiry_day(week_monday: date, loader: DayLoader) -> ExpiryDiscovery:
    """Find the week's actual NIFTY weekly expiry day.

    Starts at the regime candidate weekday; if the day's file is absent
    (holiday) OR holds no NIFTY options expiring THAT day, walks back one
    weekday at a time (max :data:`MAX_WALKBACK_STEPS`) until a file
    contains NIFTY options expiring on the probed day.  Weeks with none
    found are reported ``expiry_date=None`` — logged + counted upstream,
    never fabricated.
    """
    probe = weekly_expiry_candidate(week_monday)
    for _step in range(MAX_WALKBACK_STEPS + 1):
        day_file = loader.load(probe)
        if day_file is not None:
            parsed = parse_day_file(day_file)
            if any(
                option.symbol == "NIFTY" and option.expiry == probe for option in parsed.options
            ):
                return ExpiryDiscovery(
                    week_monday=week_monday,
                    expiry_date=probe,
                    parsed=parsed,
                    fmt=day_file.fmt,
                )
        probe = _previous_weekday(probe)
    return ExpiryDiscovery(week_monday=week_monday, expiry_date=None, parsed=None, fmt=None)


def iter_week_mondays(start: date, end: date) -> Iterator[date]:
    """Mondays of every ISO week intersecting [start, end]."""
    monday = start - timedelta(days=start.weekday())
    while monday <= end:
        yield monday
        monday += timedelta(days=7)


# ---------------------------------------------------------------------------
# Chain extraction
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChainDay:
    """The tradable NIFTY expiry-day chain extracted from one bhavcopy."""

    expiry_date: date
    fut_expiry: date
    fut_open: Decimal
    fut_close: Decimal
    ce_by_strike: dict[Decimal, OptionRow]
    pe_by_strike: dict[Decimal, OptionRow]
    dropped_rows: int


def extract_chain(parsed: ParsedDay, expiry_day: date) -> ChainDay | None:
    """Extract the expiry-day NIFTY chain + the future-open spot proxy.

    Spot proxy choice (documented): the OPEN of the nearest-expiry NIFTY
    index FUTURE (min expiry >= expiry_day) — the closest tradable
    same-file proxy for the 09:15-ish index level; the underlying index
    itself is not in the derivatives bhavcopy.  Returns ``None`` when no
    such future exists (counted upstream as ``no_future``).
    """
    candidates = [
        row for row in parsed.futures if row.symbol == "NIFTY" and row.expiry >= expiry_day
    ]
    if not candidates:
        return None
    future = min(candidates, key=lambda row: row.expiry)
    ce_by_strike: dict[Decimal, OptionRow] = {}
    pe_by_strike: dict[Decimal, OptionRow] = {}
    for option in parsed.options:
        if option.symbol != "NIFTY" or option.expiry != expiry_day:
            continue
        side = ce_by_strike if option.option_type == "CE" else pe_by_strike
        # Duplicate strikes should not occur; keep-first deterministically.
        side.setdefault(option.strike, option)
    return ChainDay(
        expiry_date=expiry_day,
        fut_expiry=future.expiry,
        fut_open=future.open,
        fut_close=future.close,
        ce_by_strike=ce_by_strike,
        pe_by_strike=pe_by_strike,
        dropped_rows=parsed.dropped_rows,
    )


def nearest_strike(strikes: Sequence[Decimal], target: Decimal) -> Decimal | None:
    """The strike nearest ``target``; exact ties prefer the LOWER strike
    (deterministic tie-break, documented)."""
    if not strikes:
        return None
    return min(strikes, key=lambda strike: (abs(strike - target), strike))


def select_legs(chain: ChainDay, variant: Variant) -> tuple[OptionRow, OptionRow] | None:
    """Pick the (CE, PE) legs for a variant, or ``None`` if unbuildable.

    V1 (ATM straddle): ONE strike nearest the future open, present on
    BOTH sides.  V2/V3 (strangles): CE strike nearest open*(1+pct) among
    CE strikes; PE strike nearest open*(1-pct) among PE strikes.
    """
    if variant.offset_pct == 0:
        common = sorted(set(chain.ce_by_strike) & set(chain.pe_by_strike))
        strike = nearest_strike(common, chain.fut_open)
        if strike is None:
            return None
        return (chain.ce_by_strike[strike], chain.pe_by_strike[strike])
    ce_strike = nearest_strike(
        sorted(chain.ce_by_strike), chain.fut_open * (1 + variant.offset_pct)
    )
    pe_strike = nearest_strike(
        sorted(chain.pe_by_strike), chain.fut_open * (1 - variant.offset_pct)
    )
    if ce_strike is None or pe_strike is None:
        return None
    return (chain.ce_by_strike[ce_strike], chain.pe_by_strike[pe_strike])


# ---------------------------------------------------------------------------
# Leg / variant simulation — the fill brackets
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LegResult:
    """One short-option leg's simulated round trip (premium points/share)."""

    strike: Decimal
    entry: Decimal
    exit: Decimal
    triggered: bool

    @property
    def pnl_points(self) -> Decimal:
        """Short-premium P&L: sold at entry, bought back at exit."""
        return self.entry - self.exit


def _exit_base(row: OptionRow) -> Decimal:
    """Hold-to-close exit price: CLOSE, or SETTLE_PR when close missing/0."""
    return row.close if row.close > 0 else row.settle


def simulate_leg(row: OptionRow, sl_pct: Decimal | None, bracket: Bracket) -> LegResult:
    """Simulate one short leg sold at OPEN under one SL + fill bracket.

    * ``sl_pct is None`` (or bracket ``hold_to_close``): no stop — buy
      back at the hold-to-close price.
    * SL trigger = entry * (1 + sl_pct); pierced when the day's HIGH >=
      trigger (boundary INCLUSIVE — a print at the trigger fills a stop).
    * OPTIMISTIC pierced fill: AT the trigger.  PESSIMISTIC pierced fill:
      at the day's HIGH — the decision-bar adverse extreme per
      DEC-FILL-WORST-CASE-ONLY-001 (daily bar == the decision bar here).
    """
    entry = row.open
    if sl_pct is None or bracket == "hold_to_close":
        return LegResult(strike=row.strike, entry=entry, exit=_exit_base(row), triggered=False)
    trigger = entry * (1 + sl_pct)
    if row.high >= trigger:
        exit_price = trigger if bracket == "optimistic" else row.high
        return LegResult(strike=row.strike, entry=entry, exit=exit_price, triggered=True)
    return LegResult(strike=row.strike, entry=entry, exit=_exit_base(row), triggered=False)


@dataclass(frozen=True)
class VariantDayResult:
    """One (expiry day, variant, SL, bracket) row of the study."""

    expiry_date: date
    variant: str
    sl_pct: Decimal | None  # None == the NO-SL hold-to-close baseline
    bracket: Bracket
    ce: LegResult
    pe: LegResult
    lot_size: int
    fut_open: Decimal
    fut_close: Decimal
    cost_rupees: Decimal | None  # None == costs disabled OR cost_refused
    cost_refused: bool

    @property
    def pnl_points(self) -> Decimal:
        return self.ce.pnl_points + self.pe.pnl_points

    @property
    def pnl_rupees_per_lot(self) -> Decimal:
        return self.pnl_points * self.lot_size

    @property
    def pnl_net_rupees(self) -> Decimal | None:
        if self.cost_rupees is None:
            return None
        return self.pnl_rupees_per_lot - self.cost_rupees

    @property
    def day_move_pct(self) -> Decimal:
        if self.fut_open == 0:
            return Decimal(0)
        return (self.fut_close - self.fut_open) / self.fut_open * 100


def simulate_chain_day(
    chain: ChainDay,
    *,
    apply_costs: bool,
) -> tuple[list[VariantDayResult], list[str]]:
    """All variant x SL x bracket rows for one expiry day.

    Returns ``(rows, skipped_variant_keys)`` — a variant whose legs
    cannot be built (missing side) is skipped + reported, never faked.
    """
    rows: list[VariantDayResult] = []
    skipped: list[str] = []
    lot = nifty_lot_size(chain.expiry_date)
    for variant in VARIANTS:
        legs = select_legs(chain, variant)
        if legs is None:
            skipped.append(variant.key)
            continue
        ce_row, pe_row = legs
        combos: list[tuple[Decimal | None, Bracket]] = [(None, "hold_to_close")]
        combos.extend((sl, bracket) for sl in SL_GRID for bracket in _SL_BRACKETS)
        for sl_pct, bracket in combos:
            ce = simulate_leg(ce_row, sl_pct, bracket)
            pe = simulate_leg(pe_row, sl_pct, bracket)
            cost: Decimal | None = None
            refused = False
            if apply_costs:
                cost = variant_costs_rupees(
                    chain.expiry_date, (ce.entry, pe.entry), (ce.exit, pe.exit), lot
                )
                refused = cost is None
            rows.append(
                VariantDayResult(
                    expiry_date=chain.expiry_date,
                    variant=variant.key,
                    sl_pct=sl_pct,
                    bracket=bracket,
                    ce=ce,
                    pe=pe,
                    lot_size=lot,
                    fut_open=chain.fut_open,
                    fut_close=chain.fut_close,
                    cost_rupees=cost,
                    cost_refused=refused,
                )
            )
    return rows, skipped


# ---------------------------------------------------------------------------
# Study driver
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StudyConfig:
    """Full-run configuration (CLI-mapped)."""

    start: date
    end: date
    cache_dir: Path
    out_dir: Path
    from_cache_only: bool = False
    apply_costs: bool = False


@dataclass
class StudyCounters:
    """Honest bookkeeping — every skipped/dropped thing is counted."""

    weeks_scanned: int = 0
    expiry_days_found: int = 0
    no_expiry_found: int = 0
    no_future: int = 0
    dropped_rows: int = 0
    variant_skips: dict[str, int] = field(default_factory=dict)
    cost_refused_days: int = 0


@dataclass
class StudyOutput:
    """Everything a caller (CLI or test) needs from one run."""

    results: list[VariantDayResult]
    counters: StudyCounters
    no_expiry_weeks: list[date]


def run_study(config: StudyConfig, fetch: Fetcher | None) -> StudyOutput:
    """Run discovery -> extraction -> simulation over [start, end].

    ``fetch`` is the injectable network seam (``None`` == offline/cache
    only); tests prove the network layer is never touched by injecting a
    raising fetcher.
    """
    loader = DayLoader(
        cache_dir=config.cache_dir,
        fetch=fetch,
        from_cache_only=config.from_cache_only,
    )
    counters = StudyCounters()
    results: list[VariantDayResult] = []
    no_expiry_weeks: list[date] = []
    for monday in iter_week_mondays(config.start, config.end):
        candidate = weekly_expiry_candidate(monday)
        if candidate < config.start or candidate > config.end:
            continue
        counters.weeks_scanned += 1
        discovery = discover_expiry_day(monday, loader)
        if discovery.expiry_date is None or discovery.parsed is None:
            counters.no_expiry_found += 1
            no_expiry_weeks.append(monday)
            _say(f"  week of {monday}: no_expiry_found (candidate {candidate})")
            continue
        chain = extract_chain(discovery.parsed, discovery.expiry_date)
        if chain is None:
            counters.no_future += 1
            _say(f"  {discovery.expiry_date}: no NIFTY future row — day skipped")
            continue
        counters.expiry_days_found += 1
        counters.dropped_rows += chain.dropped_rows
        day_rows, skipped = simulate_chain_day(chain, apply_costs=config.apply_costs)
        for key in skipped:
            counters.variant_skips[key] = counters.variant_skips.get(key, 0) + 1
        if config.apply_costs and any(row.cost_refused for row in day_rows):
            counters.cost_refused_days += 1
        results.extend(day_rows)
    return StudyOutput(results=results, counters=counters, no_expiry_weeks=no_expiry_weeks)


# ---------------------------------------------------------------------------
# Outputs — per_day_results.csv + summary.md
# ---------------------------------------------------------------------------

CSV_HEADER: Final[tuple[str, ...]] = (
    "expiry_date",
    "variant",
    "sl_pct",
    "bracket",
    "ce_strike",
    "pe_strike",
    "ce_entry",
    "pe_entry",
    "ce_trigger_hit",
    "pe_trigger_hit",
    "ce_exit",
    "pe_exit",
    "pnl_points",
    "lot_size",
    "pnl_rupees_per_lot",
    "fut_open",
    "fut_close",
    "day_move_pct",
    "cost_rupees",
    "pnl_net_rupees",
    "cost_refused",
)


def write_per_day_csv(results: Sequence[VariantDayResult], out_path: Path) -> None:
    """Write the flat per-(day, variant, SL, bracket) results CSV."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_HEADER)
        for row in sorted(
            results, key=lambda r: (r.expiry_date, r.variant, str(r.sl_pct), r.bracket)
        ):
            writer.writerow(
                (
                    row.expiry_date.isoformat(),
                    row.variant,
                    "" if row.sl_pct is None else str(row.sl_pct),
                    row.bracket,
                    str(row.ce.strike),
                    str(row.pe.strike),
                    str(row.ce.entry),
                    str(row.pe.entry),
                    int(row.ce.triggered),
                    int(row.pe.triggered),
                    str(row.ce.exit),
                    str(row.pe.exit),
                    str(row.pnl_points),
                    row.lot_size,
                    str(row.pnl_rupees_per_lot),
                    str(row.fut_open),
                    str(row.fut_close),
                    f"{row.day_move_pct:.4f}",
                    "" if row.cost_rupees is None else str(row.cost_rupees),
                    "" if row.pnl_net_rupees is None else str(row.pnl_net_rupees),
                    int(row.cost_refused),
                )
            )


def percentile(sorted_values: Sequence[float], p: float) -> float:
    """Linear-interpolation percentile (numpy 'linear' method) over a
    pre-SORTED ascending sequence.  Deterministic + documented so the
    summary tables are reproducible bit-for-bit."""
    if not sorted_values:
        msg = "percentile: empty input"
        raise ValueError(msg)
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = (p / 100.0) * (len(sorted_values) - 1)
    lower = int(rank)
    upper = min(lower + 1, len(sorted_values) - 1)
    weight = rank - lower
    return sorted_values[lower] * (1.0 - weight) + sorted_values[upper] * weight


def max_drawdown(pnls_in_date_order: Sequence[float]) -> float:
    """Max drawdown (>= 0) of the cumulative curve of per-day P&Ls."""
    peak = 0.0
    cumulative = 0.0
    worst = 0.0
    for pnl in pnls_in_date_order:
        cumulative += pnl
        peak = max(peak, cumulative)
        worst = max(worst, peak - cumulative)
    return worst


_PERCENTILE_POINTS: Final[tuple[float, ...]] = (1, 5, 25, 50, 75, 95, 99)


def _sl_label(sl_pct: Decimal | None) -> str:
    if sl_pct is None:
        return "no-SL"
    return f"SL {int(sl_pct * 100)}%"


def _fmt(value: float) -> str:
    return f"{value:,.0f}"


def build_summary_md(output: StudyOutput, config: StudyConfig) -> str:
    """Render summary.md — the tail-first GitHub-markdown tables."""
    lines: list[str] = []
    counters = output.counters
    lines.append("# Expiry-day option-selling tail study — summary")
    lines.append("")
    lines.append(
        f"Window: **{config.start} → {config.end}** · expiry days found: "
        f"**{counters.expiry_days_found}** · weeks scanned: {counters.weeks_scanned} · "
        f"`no_expiry_found` weeks: {counters.no_expiry_found} · "
        f"days with no future row: {counters.no_future} · "
        f"sanity-dropped rows: {counters.dropped_rows} · "
        f"variant skips: {counters.variant_skips or '{}'}"
    )
    if config.apply_costs:
        lines.append(
            f"Costs mode: `post2024` — days cost-refused (pre-2024-10-01): "
            f"{counters.cost_refused_days}. Summary tables below stay GROSS; "
            "net totals for the priced subwindow are in the final section."
        )
    lines.append("")

    # Group rows: variant -> sl_pct(None first) -> bracket -> date-ordered rows.
    grouped: dict[str, dict[Decimal | None, dict[Bracket, list[VariantDayResult]]]] = {}
    for row in sorted(output.results, key=lambda r: r.expiry_date):
        grouped.setdefault(row.variant, {}).setdefault(row.sl_pct, {}).setdefault(
            row.bracket, []
        ).append(row)

    for variant in VARIANTS:
        by_sl = grouped.get(variant.key)
        if not by_sl:
            continue
        lines.append(f"## {variant.key} — {variant.label}")
        lines.append("")

        # -- Percentiles ---------------------------------------------------
        lines.append("### P&L percentiles (₹ per lot, gross)")
        lines.append("")
        header = "| SL | bracket | " + " | ".join(f"p{int(p)}" for p in _PERCENTILE_POINTS) + " |"
        lines.append(header)
        lines.append("|" + "---|" * (2 + len(_PERCENTILE_POINTS)))
        for sl_pct in [None, *SL_GRID]:
            for bracket, rows in sorted((by_sl.get(sl_pct) or {}).items(), key=lambda kv: kv[0]):
                vals = sorted(float(r.pnl_rupees_per_lot) for r in rows)
                pct_cells = " | ".join(_fmt(percentile(vals, p)) for p in _PERCENTILE_POINTS)
                lines.append(f"| {_sl_label(sl_pct)} | {bracket} | {pct_cells} |")
        lines.append("")
        lines.append(TABLE_FOOTER)
        lines.append("")

        # -- Summary stats -------------------------------------------------
        lines.append("### Summary (₹ per lot, gross)")
        lines.append("")
        lines.append("| SL | bracket | days | total | win rate | avg win | avg loss | max DD |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for sl_pct in [None, *SL_GRID]:
            for bracket, rows in sorted((by_sl.get(sl_pct) or {}).items(), key=lambda kv: kv[0]):
                pnls = [float(r.pnl_rupees_per_lot) for r in rows]
                wins = [p for p in pnls if p > 0]
                losses = [p for p in pnls if p < 0]
                win_rate = 100.0 * len(wins) / len(pnls) if pnls else 0.0
                avg_win = sum(wins) / len(wins) if wins else 0.0
                avg_loss = sum(losses) / len(losses) if losses else 0.0
                lines.append(
                    f"| {_sl_label(sl_pct)} | {bracket} | {len(pnls)} | "
                    f"{_fmt(sum(pnls))} | {win_rate:.1f}% | {_fmt(avg_win)} | "
                    f"{_fmt(avg_loss)} | {_fmt(max_drawdown(pnls))} |"
                )
        lines.append("")
        lines.append(TABLE_FOOTER)
        lines.append("")

        # -- Worst-10 days per SL (both brackets side-by-side) -------------
        for sl_pct in SL_GRID:
            brackets = by_sl.get(sl_pct) or {}
            opt_rows = brackets.get("optimistic", [])
            pess_rows = brackets.get("pessimistic", [])
            pess_by_date = {r.expiry_date: r for r in pess_rows}
            opt_by_date = {r.expiry_date: r for r in opt_rows}
            worst = sorted(pess_rows, key=lambda r: r.pnl_rupees_per_lot)[:10]
            if not worst:
                continue
            lines.append(f"### Worst 10 days — {_sl_label(sl_pct)} (by pessimistic ₹/lot)")
            lines.append("")
            lines.append(
                "| expiry | day move % | optimistic ₹/lot | pessimistic ₹/lot | pess/opt loss ratio |"
            )
            lines.append("|---|---|---|---|---|")
            for pess in worst:
                opt = opt_by_date.get(pess.expiry_date)
                opt_pnl = float(opt.pnl_rupees_per_lot) if opt else 0.0
                pess_pnl = float(pess.pnl_rupees_per_lot)
                ratio = (
                    f"{pess_pnl / opt_pnl:.1f}x"
                    if opt is not None and opt_pnl < 0 and pess_pnl < 0
                    else "—"
                )
                lines.append(
                    f"| {pess.expiry_date} | {pess.day_move_pct:.2f} | "
                    f"{_fmt(opt_pnl)} | {_fmt(pess_pnl)} | {ratio} |"
                )
            gap_days = sum(
                1
                for pess in pess_by_date.values()
                if (opt := opt_by_date.get(pess.expiry_date)) is not None
                and float(opt.pnl_rupees_per_lot) < 0
                and float(pess.pnl_rupees_per_lot) <= 5.0 * float(opt.pnl_rupees_per_lot)
            )
            lines.append("")
            lines.append(
                f"Days where the pessimistic loss ≥ 5× the optimistic loss: **{gap_days}**"
            )
            lines.append("")
            lines.append(TABLE_FOOTER)
            lines.append("")

        # -- Per-year subtable ---------------------------------------------
        lines.append("### Per-year totals (₹ per lot, gross)")
        lines.append("")
        sl_columns: list[tuple[Decimal | None, Bracket]] = [(None, "hold_to_close")]
        sl_columns.extend((sl, b) for sl in SL_GRID for b in _SL_BRACKETS)
        head = (
            "| year | days | "
            + " | ".join(
                f"{_sl_label(sl)} {b[:4]}" if sl is not None else "no-SL" for sl, b in sl_columns
            )
            + " |"
        )
        lines.append(head)
        lines.append("|" + "---|" * (2 + len(sl_columns)))
        years = sorted(
            {
                r.expiry_date.year
                for rows_by_bracket in by_sl.values()
                for rows in rows_by_bracket.values()
                for r in rows
            }
        )
        for year in years:
            cells: list[str] = []
            day_count = 0
            for sl, bracket in sl_columns:
                rows = (by_sl.get(sl) or {}).get(bracket, [])
                year_rows = [r for r in rows if r.expiry_date.year == year]
                day_count = max(day_count, len(year_rows))
                cells.append(_fmt(sum(float(r.pnl_rupees_per_lot) for r in year_rows)))
            lines.append(f"| {year} | {day_count} | " + " | ".join(cells) + " |")
        lines.append("")
        lines.append(TABLE_FOOTER)
        lines.append("")

    # -- Net-of-costs subwindow (only when enabled) ------------------------
    if config.apply_costs:
        lines.append("## Net-of-costs totals — priced subwindow (≥ 2024-10-01 only)")
        lines.append("")
        lines.append("| variant | SL | bracket | days priced | net total ₹ |")
        lines.append("|---|---|---|---|---|")
        for variant in VARIANTS:
            by_sl = grouped.get(variant.key) or {}
            for sl_pct in [None, *SL_GRID]:
                for bracket, rows in sorted(
                    (by_sl.get(sl_pct) or {}).items(), key=lambda kv: kv[0]
                ):
                    priced = [r for r in rows if r.pnl_net_rupees is not None]
                    if not priced:
                        continue
                    net_total = sum(float(r.pnl_net_rupees or 0) for r in priced)
                    lines.append(
                        f"| {variant.key} | {_sl_label(sl_pct)} | {bracket} | "
                        f"{len(priced)} | {_fmt(net_total)} |"
                    )
        lines.append("")
        lines.append(TABLE_FOOTER)
        lines.append("")

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _say(message: str) -> None:
    """CLI progress output.

    A standalone stdlib-only operator tool deliberately has no structlog
    dependency; this single audited call site is the one print.
    """
    print(message, flush=True)  # noqa: T201 — standalone docs/ research tool (see docstring)


def run_smoke(smoke_date: date, cache_dir: Path, fetch: Fetcher) -> int:
    """--smoke: download ONE day's file, print format + parsed samples.

    The mandatory first-run probe on the operator box: the URL stems +
    column layouts are UNVERIFIED from the build container, so one file
    per format era must be eyeballed before the full pull.
    """
    loader = DayLoader(cache_dir=cache_dir, fetch=fetch)
    day_file = loader.load(smoke_date)
    if day_file is None:
        _say(f"SMOKE {smoke_date}: NOT FOUND in either format (holiday? URL drift?)")
        return 2
    parsed = parse_day_file(day_file)
    _say(f"SMOKE {smoke_date}: format={day_file.fmt}")
    _say(
        f"  parsed: futures={len(parsed.futures)} options={len(parsed.options)} "
        f"dropped={parsed.dropped_rows}"
    )
    nifty_futures = [f for f in parsed.futures if f.symbol == "NIFTY"]
    if nifty_futures:
        near = min(nifty_futures, key=lambda f: f.expiry)
        _say(f"  nearest NIFTY future: expiry={near.expiry} open={near.open} close={near.close}")
    else:
        _say("  WARNING: no NIFTY future row parsed — inspect the columns!")
    sample = [o for o in parsed.options if o.symbol == "NIFTY"][:5]
    for option in sample:
        _say(
            f"  NIFTY {option.option_type} {option.strike} exp {option.expiry}: "
            f"O={option.open} H={option.high} L={option.low} C={option.close} S={option.settle}"
        )
    if not sample:
        _say("  WARNING: no NIFTY option rows parsed — inspect the columns!")
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Expiry-day option-selling tail study over NSE F&O bhavcopy "
            "(daily bars; optimistic-vs-pessimistic SL fill brackets)."
        )
    )
    parser.add_argument("--start", type=date.fromisoformat, default=date(2019, 2, 1))
    parser.add_argument("--end", type=date.fromisoformat, default=date.today())
    parser.add_argument("--cache-dir", type=Path, default=Path("bhavcopy_cache"))
    parser.add_argument("--out-dir", type=Path, default=Path("results"))
    parser.add_argument(
        "--from-cache-only",
        action="store_true",
        help="Never touch the network; un-cached days count as absent.",
    )
    parser.add_argument(
        "--smoke",
        type=date.fromisoformat,
        default=None,
        metavar="DATE",
        help="Download ONE day's file, print parsed samples, exit.",
    )
    parser.add_argument(
        "--costs",
        choices=("post2024",),
        default=None,
        help=(
            "Overlay verified costs (Rs20/order brokerage + sell-side STT "
            "+ NSE ETC) on trades >= 2024-10-01; earlier trades are "
            "cost_refused (see module docstring)."
        ),
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=0.75,
        help="Polite sleep (seconds) before each network request.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point."""
    args = build_arg_parser().parse_args(argv)
    fetch: Fetcher | None = None
    if not args.from_cache_only:
        fetch = make_http_fetcher(sleep_seconds=args.sleep)
    if args.smoke is not None:
        if fetch is None:
            _say("--smoke requires network access (drop --from-cache-only)")
            return 2
        return run_smoke(args.smoke, args.cache_dir, fetch)
    config = StudyConfig(
        start=args.start,
        end=args.end,
        cache_dir=args.cache_dir,
        out_dir=args.out_dir,
        from_cache_only=args.from_cache_only,
        apply_costs=args.costs == "post2024",
    )
    _say(f"Running study {config.start} → {config.end} (costs={args.costs or 'off'})")
    output = run_study(config, fetch)
    config.out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = config.out_dir / "per_day_results.csv"
    write_per_day_csv(output.results, csv_path)
    summary_path = config.out_dir / "summary.md"
    summary_path.write_text(build_summary_md(output, config), encoding="utf-8")
    counters_path = config.out_dir / "counters.json"
    counters_path.write_text(
        json.dumps(
            {
                "weeks_scanned": output.counters.weeks_scanned,
                "expiry_days_found": output.counters.expiry_days_found,
                "no_expiry_found": output.counters.no_expiry_found,
                "no_future": output.counters.no_future,
                "dropped_rows": output.counters.dropped_rows,
                "variant_skips": output.counters.variant_skips,
                "cost_refused_days": output.counters.cost_refused_days,
                "no_expiry_weeks": [d.isoformat() for d in output.no_expiry_weeks],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    _say(
        f"Done: {output.counters.expiry_days_found} expiry days → "
        f"{csv_path} + {summary_path} + {counters_path}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
