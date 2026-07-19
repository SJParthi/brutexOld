"""The FENCED independent movers recompute — NO production compute imports.

DEC-OPERATOR-ACCEPTANCE-VERIFY-001 (MIGRATION.md #1958) — the ONE sanctioned
independent-recompute surface (the DEC-SURFACE-TRIAL-001 precedent). Every
compared ``/movers`` cell is re-derived here from the RAW month parquet files
with hand-rolled math, so a regression in the production compute is CAUGHT,
not mirrored:

* instrument enumeration by direct ``os.scandir`` of ``bars/<exch>/<seg>/``;
* bare ``pl.read_parquet`` of ``.../1minute/<yyyy>/<mm>.parquet`` (hand path
  grammar, lexicographic file order);
* keep-first dedup replicated as a LAW (stable file/row order, drop duplicate
  timestamps keep first — DEC-LAKE-READ-UNIQUE-001 semantics re-stated);
* hand IST(+05:30 fixed) as-of predicate incl. the #1601 bucket-anchor forward
  resolution (anchor + tf − 1 minutes, capped 15:29) re-derived from the law
  TEXT (DEC-MOVERS-EXPLICIT-BUCKET-ASOF-001) — never from the production seam;
* ``fractions.Fraction`` %CHNG + hand ROUND_HALF_UP 2dp (never ``decimal``,
  never production helpers);
* turnover Σ half-up-2dp((H+L+C)/3) × volume in integer paisa
  (DEC-MOVERS-VWAP-VALUE-001 law text);
* prev-close from the prior real NSE session's last 1-minute close, the
  session found by a HAND back-walk over calendar DATA constants
  (data-not-compute — the fence test asserts every import is non-callable);
* independent rank: single-pass negated-key sort, alphabetical tie.

FENCE (enforced by ``tests/unit/test_acceptance_independent_fence.py``): no
``brutex.*`` import except ``^[A-Z_0-9]+$`` DATA names from
``brutex.calendars.nse`` / ``brutex.calendars.observed_sessions``; no
``tests.*`` import; third-party = stdlib + polars/duckdb/pyarrow only.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone
from fractions import Fraction
from pathlib import Path
from typing import Final

import polars as pl

from brutex.calendars.nse import (
    ALL_HOLIDAYS,
    MUHURAT_SESSIONS,
    SPECIAL_WEEKEND_TRADING_SESSIONS,
)

#: Monday-indexed weekday ordinal of Saturday.
_SATURDAY: Final[int] = 5

#: Fixed IST offset — hand constant, never a tz database lookup.
IST: Final = timezone(timedelta(hours=5, minutes=30))

#: NSE regular session bounds as minutes-of-day (09:15 → 15:30 exclusive).
SESSION_OPEN_MIN: Final[int] = 9 * 60 + 15
SESSION_CLOSE_MIN: Final[int] = 15 * 60 + 30

#: A TF at/over the whole session length (375 min) is a DAILY coordinate.
DAILY_TF_MIN_FLOOR: Final[int] = 375

#: ``<EXCH>-<UND...>-<DDMmmYY>-FUT`` needs at least 4 hyphen-split parts.
_FUT_MIN_PARTS: Final[int] = 4

#: The ``DDMmmYY`` expiry token is exactly 7 characters.
_EXPIRY_TOKEN_LEN: Final[int] = 7


def half_up_2dp(x: Fraction) -> Fraction:
    """Half-up to 2 decimals, AWAY from zero on negatives, on exact rationals.

    ``sign(x) · floor(|x|·100 + 1/2) / 100`` — exact on Fractions so ±0.005
    lands on ±0.01 by construction, never by float luck.
    """
    sign = -1 if x < 0 else 1
    return Fraction(sign * math.floor(abs(x) * 100 + Fraction(1, 2)), 100)


def format_inr_2dp(rupees: Fraction) -> str:
    """Hand ₹ display twin: half-up 2dp + western thousands grouping.

    Mirrors the production display LAW (``₹27,932.80``; sign after the ₹)
    without importing it.
    """
    q = half_up_2dp(rupees)
    sign = "-" if q < 0 else ""
    cents = abs(q.numerator * 100 // q.denominator)
    whole, frac = divmod(cents, 100)
    grouped = f"{whole:,}"
    return f"₹{sign}{grouped}.{frac:02d}"


def pct_chng(prev_close: Fraction, close: Fraction) -> Fraction | None:
    """Exact %CHNG vs the previous close (None on a zero/absent baseline)."""
    if prev_close == 0:
        return None
    return (close - prev_close) * 100 / prev_close


def ist_minute_of_day(ts_utc: datetime) -> int:
    """Minute-of-day of a UTC timestamp's IST wall time (hand +05:30 shift)."""
    ist = (
        ts_utc.astimezone(IST)
        if ts_utc.tzinfo is not None
        else ts_utc + timedelta(hours=5, minutes=30)
    )
    return ist.hour * 60 + ist.minute


def ist_date_of(ts_utc: datetime) -> date:
    """IST calendar date of a UTC timestamp (hand +05:30 shift)."""
    ist = (
        ts_utc.astimezone(IST)
        if ts_utc.tzinfo is not None
        else ts_utc + timedelta(hours=5, minutes=30)
    )
    return ist.date() if isinstance(ist, datetime) else ist


def bucket_asof_cutoff_minute(tf_minutes: int, hhmm: str | None) -> int | None:
    """The #1601 bucket-anchor forward resolution, re-derived from the law TEXT.

    Blank time or a daily TF ⇒ ``None`` (whole-day cut). A picked ``HH:MM``
    that lands on a 09:15-anchored bucket ANCHOR of *tf_minutes* resolves to
    ``min(anchor + tf − 1, 15:29)`` — the completed bucket's final minute. A
    non-anchor pick keeps the raw minute (honest mid-bucket as-of).
    """
    if hhmm is None or tf_minutes >= DAILY_TF_MIN_FLOOR:
        return None
    hh, mm = hhmm.split(":")
    minute = int(hh) * 60 + int(mm)
    if (minute - SESSION_OPEN_MIN) % tf_minutes == 0 and minute >= SESSION_OPEN_MIN:
        return min(minute + tf_minutes - 1, SESSION_CLOSE_MIN - 1)
    return minute


@dataclass(frozen=True)
class Bar1m:
    """One raw 1-minute bar (rupee prices as exact Fractions, from raw floats)."""

    ts_utc: datetime
    open: Fraction
    high: Fraction
    low: Fraction
    close: Fraction
    volume: int


def _to_fraction(x: float) -> Fraction:
    """Exact rational of a stored 2dp-snapped float via its shortest repr."""
    return Fraction(repr(float(x)))


def read_1m_bars(symbol_dir: Path, *, months: set[tuple[int, int]] | None = None) -> list[Bar1m]:
    """Read + keep-first-dedup a symbol's raw 1-minute bars, hand path grammar.

    Files are read in lexicographic ``<yyyy>/<mm>.parquet`` order; within the
    concatenated stream, duplicate timestamps keep the FIRST physical row
    (the DEC-LAKE-READ-UNIQUE-001 law re-stated), then a stable sort by ts.
    *months* (a ``{(year, month), …}`` set) bounds the read by FILENAME.
    """
    root = symbol_dir / "1minute"
    files: list[Path] = []
    if root.is_dir():
        for year in sorted(p for p in root.iterdir() if p.is_dir()):
            for file in sorted(year.glob("*.parquet")):
                if months is not None:
                    try:
                        cell = (int(year.name), int(file.stem))
                    except ValueError:
                        continue
                    if cell not in months:
                        continue
                files.append(file)
    seen: set[datetime] = set()
    bars: list[Bar1m] = []
    for file in files:
        # glob=False — a `[`/`*`/`?`-named symbol dir must read its LITERAL
        # file, never a glob-matched sibling (the #1594-R15 class; round 1 M2).
        frame = pl.read_parquet(file, glob=False)
        for row in frame.iter_rows(named=True):
            ts = row["timestamp"]
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            if ts in seen:
                continue
            seen.add(ts)
            bars.append(
                Bar1m(
                    ts_utc=ts,
                    open=_to_fraction(row["open"]),
                    high=_to_fraction(row["high"]),
                    low=_to_fraction(row["low"]),
                    close=_to_fraction(row["close"]),
                    volume=int(row["volume"]),
                )
            )
    bars.sort(key=lambda b: b.ts_utc)
    return bars


def session_bars(bars: list[Bar1m], *, day: date, cutoff_minute: int | None) -> list[Bar1m]:
    """The day's bars up to (and including) the as-of cutoff minute."""
    out = [b for b in bars if ist_date_of(b.ts_utc) == day]
    if cutoff_minute is not None:
        out = [b for b in out if ist_minute_of_day(b.ts_utc) <= cutoff_minute]
    return out


def hlc3_value_paisa(bars: list[Bar1m]) -> int:
    """Turnover law: Σ half-up-2dp((H+L+C)/3) × volume, in integer paisa."""
    total = 0
    for b in bars:
        typical = half_up_2dp((b.high + b.low + b.close) / 3)
        total += int(typical * 100) * b.volume
    return total


def value_crore_2dp(value_paisa: int) -> str:
    """Value (Cr) display cell: paisa / 1e9, half-up 2dp, plain string."""
    q = half_up_2dp(Fraction(value_paisa, 10**9))
    cents = abs(q.numerator * 100 // q.denominator)
    sign = "-" if q < 0 else ""
    whole, frac = divmod(cents, 100)
    return f"{sign}{whole}.{frac:02d}"


def enumerate_spot_symbol_dirs(lake_root: Path) -> list[tuple[str, str, str, Path]]:
    """Hand ``os.scandir`` enumeration of ``bars/<exch>/<seg>/<sym>`` (no FNO)."""
    out: list[tuple[str, str, str, Path]] = []
    bars = lake_root / "bars"
    if not bars.is_dir():
        return out
    for exch in sorted(os.scandir(bars), key=lambda e: e.name):
        if not exch.is_dir():
            continue
        for seg in sorted(os.scandir(exch.path), key=lambda e: e.name):
            if not seg.is_dir() or seg.name == "FNO":
                continue
            for sym in sorted(os.scandir(seg.path), key=lambda e: e.name):
                if sym.is_dir():
                    out.append((exch.name, seg.name, sym.name, Path(sym.path)))
    return out


def is_trading_day_hand(d: date) -> bool:
    """Hand DISPLAY-session predicate over calendar DATA constants only.

    Muhurat evenings + special weekend sessions ARE sessions for the movers
    prev-close baseline law (#1649b) even when the date is a listed holiday /
    weekend; every other holiday/weekend is not.
    """
    if d in MUHURAT_SESSIONS or d in SPECIAL_WEEKEND_TRADING_SESSIONS:
        return True
    if d in ALL_HOLIDAYS:
        return False
    return d.weekday() < _SATURDAY


def previous_session_hand(d: date, *, max_walk: int = 30) -> date | None:
    """Hand back-walk to the prior real NSE session (Muhurat = a real session

    for the DISPLAY baseline law — the stored prior bar the page legitimately
    anchors on). ``None`` when the walk exhausts *max_walk* days.
    """
    probe = d
    for _ in range(max_walk):
        probe = probe - timedelta(days=1)
        if is_trading_day_hand(probe):
            return probe
    return None


def prev_close_hand(bars: list[Bar1m], *, day: date) -> tuple[Fraction | None, date | None]:
    """The nearest stored prior day's last 1-minute close + its date.

    The page's DISPLAY baseline anchors on the nearest stored prior bar; the
    contiguity verdict (stored date vs the hand back-walk's expected session)
    is the caller's compare, not folded in here.
    """
    prior_days = sorted({ist_date_of(b.ts_utc) for b in bars if ist_date_of(b.ts_utc) < day})
    if not prior_days:
        return None, None
    anchor = prior_days[-1]
    day_bars = [b for b in bars if ist_date_of(b.ts_utc) == anchor]
    return day_bars[-1].close, anchor


@dataclass(frozen=True)
class IndependentSpotRow:
    """One symbol's independently recomputed movers cells."""

    symbol: str
    close: Fraction
    prev_close: Fraction | None
    prev_close_date: date | None
    chng: Fraction | None
    pct: Fraction | None
    volume: int
    value_paisa: int

    @property
    def ltp_display(self) -> str:
        """The hand ₹ display cell for the LTP column."""
        return format_inr_2dp(self.close)

    @property
    def value_cr_display(self) -> str:
        """The hand Value (Cr) display cell."""
        return value_crore_2dp(self.value_paisa)


def independent_spot_row(
    symbol: str, bars: list[Bar1m], *, day: date, cutoff_minute: int | None
) -> IndependentSpotRow | None:
    """Recompute one symbol's spot movers row from raw bars (None when no bar)."""
    cut = session_bars(bars, day=day, cutoff_minute=cutoff_minute)
    if not cut:
        return None
    close = cut[-1].close
    prev, prev_date = prev_close_hand(bars, day=day)
    chng = None if prev is None else close - prev
    pct = None if prev is None else pct_chng(prev, close)
    return IndependentSpotRow(
        symbol=symbol,
        close=close,
        prev_close=prev,
        prev_close_date=prev_date,
        chng=chng,
        pct=pct,
        volume=sum(b.volume for b in cut),
        value_paisa=hlc3_value_paisa(cut),
    )


#: Hand month-token map for the vendor contract-symbol grammar (DDMmmYY).
_MONTHS: Final[dict[str, int]] = {
    "JAN": 1,
    "FEB": 2,
    "MAR": 3,
    "APR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AUG": 8,
    "SEP": 9,
    "OCT": 10,
    "NOV": 11,
    "DEC": 12,
}


def parse_fut_symbol_hand(name: str) -> tuple[str, date] | None:
    """Hand right-anchored parse of ``<EXCH>-<UNDERLYING>-<DDMmmYY>-FUT``.

    Returns ``(underlying, expiry)`` or ``None`` for a non-FUT / unparsable
    name. Right-anchored so a hyphenated underlying keeps its full name (the
    #1646 law re-stated by hand — never the production grammar).
    """
    parts = name.split("-")
    if len(parts) < _FUT_MIN_PARTS or parts[-1] != "FUT":
        return None
    token = parts[-2]
    if len(token) < _EXPIRY_TOKEN_LEN:
        return None
    month = _MONTHS.get(token[2:5].upper())
    if month is None:
        return None
    try:
        expiry = date(2000 + int(token[5:]), month, int(token[:2]))
    except ValueError:
        return None
    return "-".join(parts[1:-2]), expiry


def near_month_fut_hand(contract_names: list[str], *, on_date: date) -> dict[str, str]:
    """Per-underlying nearest UNEXPIRED FUT contract name (hand selection law)."""
    best: dict[str, tuple[date, str]] = {}
    for name in contract_names:
        parsed = parse_fut_symbol_hand(name)
        if parsed is None:
            continue
        underlying, expiry = parsed
        if expiry < on_date:
            continue
        current = best.get(underlying)
        if current is None or expiry < current[0]:
            best[underlying] = (expiry, name)
    return {u: n for u, (_e, n) in best.items()}


def rank_rows(rows: list[IndependentSpotRow], *, key: str, top_n: int) -> list[IndependentSpotRow]:
    """Independent rank: single-pass negated-key sort, alphabetical tie.

    ``key`` ∈ {"volume", "value", "gainers", "losers"} — gainers/losers rank
    the exact-Fraction %CHNG (rows with no baseline excluded, mirroring the
    page's law); volume/value rank their exact integers.
    """
    if key in {"gainers", "losers"}:
        scored = [r for r in rows if r.pct is not None]
        sign = 1 if key == "gainers" else -1
        scored.sort(key=lambda r: (-(sign * (r.pct or 0)), r.symbol))
        return scored[:top_n]
    attr = "value_paisa" if key == "value" else "volume"
    ranked = sorted(rows, key=lambda r: (-getattr(r, attr), r.symbol))
    return ranked[:top_n]
