"""Point-in-time "movers" snapshot core for the 🎯 **Options** tab (DEC-MOVERS-OPTIONS-001).

Milestone 3 of the redesigned ``/movers`` page: the Options twin of the Spot
(:mod:`brutex.dashboard.movers_snapshot`) + Futures
(:mod:`brutex.dashboard.movers_futures`) readers. It answers the same single
question — *"who are the movers as of one date, on one timeframe, at one
time?"* — but over the **individual option contracts** the operator pulled into
the lake. Unlike the Futures reader (which collapses each underlying to ONE
near-month row), an options screen lists **one row per CONTRACT** — every strike
× CE/PE × expiry found in the lake — exactly as the broker markets-screen
Options tab renders them.

## Reuse, not re-implementation (CLAUDE.md §11 COMMON)

The session-time vocabulary + the raw-1-minute / prior-day readers live in
exactly ONE place each. This module IMPORTS:

* from :mod:`brutex.dashboard.movers_snapshot` —
  :func:`~brutex.dashboard.movers_snapshot.interval_minutes`,
  :func:`~brutex.dashboard.movers_snapshot.parse_snapshot_date`,
  :func:`~brutex.dashboard.movers_snapshot._cutoff_minute`,
  :data:`~brutex.dashboard.movers_snapshot._DAILY_MIN_FLOOR`, and the prior-day
  1-day close resolver :func:`~brutex.dashboard.movers_snapshot.resolve_prev_close`
  (its canonical home — the Futures reader re-imports the same one);
* from :mod:`brutex.dashboard.movers_futures` — the GENERIC raw-1-minute
  session reader :func:`~brutex.dashboard.movers_futures._read_future_session`
  (it returns ``(ltp, volume, value_paisa, oi)`` for ANY FNO contract, futures OR
  options — reused for an option contract; since DEC-MOVERS-OPTIONS-VALUE-DEDUP-001
  options uses the ``value_paisa`` field DIRECTLY, so the option's session frame is
  read ONCE — no separate value reader), the prior-day OI resolver
  :func:`~brutex.dashboard.movers_futures.resolve_prev_oi`, and the underlying spot
  resolver :func:`~brutex.dashboard.movers_futures._underlying_spot_ltp`.

It adds only what options need: the contract-symbol parser (strike + CE/PE), the
options ranking (highest OI / OI gainers / OI losers / volume / value / price
gainers / price losers), and the options payload.

## The locked calculation rules (operator-confirmed 2026-06-13)

For each option contract (strike × CE/PE × expiry) found in the lake:

* **LTP / volume / OI** come from the contract's own session bars (raw 1-minute,
  sliced to the selected ``time`` by the SAME minute math the Spot reader uses).
  ``ltp`` is the last bar's close (2dp money), ``volume`` is the cumulative
  fresh-session quantity, ``oi`` is the last bar's open interest (``None``-safe) —
  the as-of cutoff SNAPSHOT (corrected_point #1b, DEC-FNO-TF-DERIVE-OI-001), NOT a
  single resampled TF bucket. The TRUE per-TF resampled option bar (last-of-bucket
  OI, trailing forming bucket dropped) is the COMMON
  :func:`brutex.data.fno_resample.resample_session_to_tf`.
* **Value (₹)** is the cumulative traded value (turnover) over those session bars
  (``Σ round_money((high+low+close)/3) × volume`` — the per-bar HLC3 typical-price ×
  volume VWAP-proxy turnover of DEC-MOVERS-VWAP-VALUE-001), kept as int64 paisa. It
  comes from the SAME ``_read_future_session`` read as ltp/volume/oi (the ``value_paisa``
  field), so the option's frame is read once (DEC-MOVERS-OPTIONS-VALUE-DEDUP-001); that
  shared reader computes it via ``movers_snapshot.session_traded_value_paisa``.
* **% change** is vs the contract's OWN previous trading-day 1-day close
  (``None`` when there is no prior day).
* **OI change** is ``oi − prev_oi`` (a signed int) and ``oi_chg_pct`` is the
  full-precision pct change vs the contract's prior 1-day open interest
  (``None`` when ``oi`` / ``prev_oi`` is missing or ``prev_oi`` is zero).
* **Spot** is the UNDERLYING's spot LTP (its INDEX or CASH 1-minute bars,
  resolved the SAME way the Futures reader resolves it). ``None`` when absent.

## The ``expiry`` (Near/Next/Far) filter (DEC-MOVERS-OPTIONS-EXPIRY-001)

The Options tab's ``?expiry=`` selector keeps ONLY the contracts on the chosen
expiry, resolved PER underlying as-of the snapshot date: ``near`` = the front
(closest) live expiry ``>= date``, ``next`` = the one after, ``far`` = the third
(:func:`_tier_expiry_by_underlying`). So picking a date shows that underlying's
nearest-relevant expiry, and a near-dated vs a far-dated ``24000 CE`` never mix in
one ranking. An underlying without that serial expiry simply drops out (honest —
that month is not listed yet); a blank / unknown selector falls back to ``near``.

## The ``track`` (All / Monthly / Weekly) cadence (DEC-MOVERS-OPTIONS-TRACK-001)

The ``?track=`` selector chooses the cadence the Near/Next/Far tier ranks WITHIN:
``all`` (default — every live expiry, the prior behaviour), ``monthly`` (only each
underlying's last expiry per calendar month — the monthly settlement), or
``weekly`` (the rest). The split is purely STRUCTURAL — ``monthly`` = the
last-in-calendar-month expiry for that underlying (:func:`_monthly_expiries`),
derived from the dates actually in the lake, NEVER from a hardcoded NSE
expiry-weekday rule (Golden Rule #1). That is what makes it handle every
permutation: a single stock or a weekly-less index has one expiry per month (all
``monthly``, an empty ``weekly`` track), an index with weeklies has several per
month (the last is the monthly, the earlier ones the weeklies), and an NSE cadence
change (weekday shift, an index gaining/losing weeklies) just changes which dates
appear — no code change. A blank / unknown selector falls back to ``all``.

## Honesty (Golden Rule #1 / #10)

REFERENCE_ONLY over the **ingested lake** (CLAUDE.md §3) — never a live feed
(Track-1 has none — CLAUDE.md §1). Money is 2dp (``DEC-MONEY-2DP-001``); the
% / OI-% metrics keep FULL precision (a statistical metric, never money-rounded
— CLAUDE.md §4). An empty / options-less lake (or an unknown interval) yields an
honest "pull the F&O options first" payload — never a 500, never a fabricated
mover.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date as date_cls
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

import structlog

from brutex.dashboard.movers_futures import (
    _read_future_session,
    _underlying_spot_ltp,
    format_contract_date,
    resolve_prev_oi,
)
from brutex.dashboard.movers_pcr import PcrLeg, UnderlyingPcr, compute_pcr_rows, pcr_row_json
from brutex.dashboard.movers_resilient import log_movers_skip, movers_read_skip_errors
from brutex.dashboard.movers_snapshot import (
    _DAILY_MIN_FLOOR,
    _calendar_exchange,
    _cutoff_minute,
    _NoncontiguousWarnState,
    info_prev_baseline_muhurat,
    interval_minutes,
    make_expected_session_resolver,
    normalize_min_volume,
    parse_snapshot_date,
    prev_baseline_provenance,
    resolve_candle_datasets,
    resolve_prev_close,
    value_meets_volume_floor,
    warn_prev_baseline_noncontiguous,
)
from brutex.data.fno_symbol import parse_fno_symbol
from brutex.data.moneyness import (
    Moneyness,
    StepConfidence,
    StrikeStepResult,
    moneyness_view,
    resolve_strike_step,
)
from brutex.data.registry_store import RegistryError, RegistryStore
from brutex.data.spot_features import pct_change
from brutex.money import format_inr, round_money, rupees_to_paisa

_LOG = structlog.get_logger(__name__)

__all__ = [
    "DEFAULT_OPTIONS_INTERVAL",
    "DEFAULT_OPTIONS_TOP_N",
    "OptionMover",
    "OptionsCandidates",
    "build_options_payload",
    "compute_option_mover",
    "compute_options_candidates",
    "load_registry_expiries",
    "normalize_options_selectors",
    "parse_option_symbol",
    "rank_highest_oi",
    "rank_oi_gainers",
    "rank_oi_losers",
    "rank_price_gainers",
    "rank_price_losers",
    "rank_value",
    "rank_volume",
    "run_options_snapshot",
]

#: The Options tab opens on the coarse 1-day timeframe (cheapest, always
#: populated if ANY option was pulled) and a generous depth — mirrors the Spot /
#: Futures tab defaults (CLAUDE.md §11 COMMON keeps the pages aligned).
DEFAULT_OPTIONS_INTERVAL: Final[str] = "1day"
#: 100 default (DEC-MOVERS-COMPLETENESS-001 — operator-directed "minimum 100 always");
#: mirrors the Spot / Futures tab defaults (CLAUDE.md §11 COMMON keeps the pages aligned).
DEFAULT_OPTIONS_TOP_N: Final[int] = 100

#: The FNO segment every option (and futures) contract partition lives under in
#: the lake (``bars/<exch>/FNO/<contract>/<interval>/...``). Mirrors the
#: instrument-master backtesting segment (CLAUDE.md §6).
_FNO_SEGMENT: Final[str] = "FNO"

#: The honest "no strike grid" resolution (DEC-MONEYNESS-REGISTRY-STEP-001) — the
#: defensive default for the per-contract step lookup (no step, nothing to qualify).
_NO_STEP: Final[StrikeStepResult] = StrikeStepResult(None, None)

#: The ``?expiry=`` selector → the 0-based rank of the chosen expiry among an
#: underlying's DISTINCT live (``>= snapshot date``) expiries, sorted ascending:
#: ``near`` = the front (closest) expiry, ``next`` = the one after, ``far`` = the
#: third. An unknown / blank selector falls back to ``near``.
_EXPIRY_TIERS: Final[dict[str, int]] = {"near": 0, "next": 1, "far": 2}

#: The ``?track=`` selector → which expiry CADENCE the Near/Next/Far tier ranks
#: WITHIN (DEC-MOVERS-OPTIONS-TRACK-001): ``all`` (default — every live expiry,
#: the prior behaviour), ``monthly`` (only each underlying's last expiry per
#: calendar month — the monthly settlement), or ``weekly`` (the rest — the
#: non-terminal weeklies). The classification is purely STRUCTURAL — derived from
#: the expiry dates actually present in the lake (:func:`_monthly_expiries`),
#: NEVER from a hardcoded NSE expiry-weekday rule (Golden Rule #1). So a stock or
#: a weekly-less index simply has one expiry per month (all ``monthly``, an empty
#: ``weekly`` track) and an NSE cadence change only changes which dates appear —
#: no code change. An unknown / blank selector falls back to ``all``.
_EXPIRY_TRACKS: Final[frozenset[str]] = frozenset({"all", "weekly", "monthly"})


def parse_option_symbol(symbol: str) -> tuple[str, date_cls, Decimal, str] | None:
    """Parse an OPTION contract groww_symbol into its parts, or ``None``.

    A thin options-only adapter over the canonical
    :func:`brutex.data.fno_symbol.parse_fno_symbol` (CLAUDE.md §11 COMMON — one
    parser, shared with the ``/db`` F&O candle browser): the contract groww_symbol
    is ``EXCH-UNDERLYING-DDMmmYY-STRIKE-CE|PE`` (hyphen-joined), e.g.
    ``"NSE-NIFTY-30Jun25-24000-CE"`` → ``("NIFTY", date(2025, 6, 30),
    Decimal("24000"), "CE")``. Returns ``None`` for any non-option symbol — a
    FUTURE (``-FUT``) is excluded here even though the shared parser decodes it —
    and for any junk / unparseable expiry / non-positive strike, so the caller
    simply skips it (never raising — Golden Rule #1).

    Parameters
    ----------
    symbol
        A candidate contract groww_symbol from the lake's FNO datasets.

    Returns
    -------
    tuple[str, datetime.date, decimal.Decimal, str] | None
        ``(underlying, expiry_date, strike, option_type)`` when *symbol* is a
        well-formed OPTION contract, else ``None``.
    """
    parts = parse_fno_symbol(symbol)
    if parts is None or parts.option_type is None or parts.strike is None:
        return None
    return parts.underlying, parts.expiry, parts.strike, parts.option_type


@dataclass(frozen=True, slots=True)
class OptionMover:
    """One option contract's as-of-snapshot metrics (the locked rules, full precision).

    Attributes
    ----------
    underlying
        The underlying symbol (``NIFTY`` / ``RELIANCE``).
    expiry
        The contract's expiry date.
    strike
        The contract's strike price.
    opt_type
        ``"CE"`` (Call) or ``"PE"`` (Put).
    symbol
        The contract groww_symbol (``NSE-NIFTY-30Jun25-24000-CE``) — the
        deterministic tiebreak key, never shown.
    exchange, segment
        The contract's lake partition coordinates (segment is always ``FNO``);
        used only for the stable ``(symbol, exchange, segment)`` tiebreak.
    ltp
        The contract's last traded price — the selected bar's close, 2dp money.
    spot
        The underlying's spot LTP (2dp money), or ``None`` when the underlying
        spot is absent from the lake.
    pct_change
        ``(ltp − prev_close) / prev_close × 100`` at FULL precision (a statistical
        metric, never money-rounded — CLAUDE.md §4), or ``None`` when there is no
        prior day.
    oi
        The selected bar's open interest, or ``None`` when the bar omits it.
    oi_chg
        ``oi − prev_oi`` (a signed int), or ``None`` when ``oi`` / ``prev_oi`` is
        missing.
    oi_chg_pct
        OI % change vs the contract's prior 1-day open interest at full precision,
        or ``None`` when there is no prior day / a zero-or-missing prior OI / a
        missing current OI.
    volume
        Cumulative fresh-session traded quantity 09:15 → the selected bar.
    value_paisa
        Cumulative traded value (turnover) over those session bars as int64 paisa
        (``Σ round_money((high+low+close)/3) × volume`` — the HLC3 typical-price ×
        volume VWAP-proxy turnover, DEC-MOVERS-VWAP-VALUE-001).
    moneyness
        The contract's moneyness vs the underlying spot as of the snapshot
        (:class:`~brutex.data.moneyness.Moneyness` — ITM/ATM/OTM, or UNKNOWN when
        the spot is absent). DERIVED, never stored (DEC-FNO-MONEYNESS-001).
    atm_offset
        The signed ATM±N strike-steps from spot (``0`` = the at-the-money rung,
        ``+N`` above, ``-N`` below), or ``None`` when the spot / strike grid is
        unknown.
    moneyness_label
        The single trader-facing string the screen shows — bucket + signed depth:
        ``"ITM -1"`` / ``"OTM +10"`` / ``"ATM"`` · ``"ITM"`` / ``"OTM"`` (no grid) ·
        ``"—"`` (a future) · ``"?"`` (spot absent). DEC-FNO-MONEYNESS-001.
    iv, delta
        The contract's STORED per-bar Greeks as of the snapshot — the LAST kept
        session bar's ``iv`` / ``delta`` overlay values written at generation
        time by the Phase-2b enrichment (DEC-GREEKS-ENGINE-PR2B-001), read off
        the SAME session frame as ltp/volume/oi (zero extra I/O, never
        recomputed at render — DEC-GREEKS-ENGINE-PR3B-001). FULL precision
        (statistical values, never money-rounded — CLAUDE.md §4). ``None`` when
        the partition pre-dates the enrichment (legacy 7-column), when the
        enrichment stamped an honest §15.8 NULL (no spot / no solution / …),
        or for a never-enriched series — never a fabricated Greek.
    strike_step_confidence
        The honesty label of the strike-grid step behind ``atm_offset`` /
        ``moneyness_label`` (DEC-MONEYNESS-REGISTRY-STEP-001):
        :attr:`~brutex.data.moneyness.StepConfidence.REGISTRY` when the
        AUTHORITATIVE vendor chain sized the grid,
        :attr:`~brutex.data.moneyness.StepConfidence.OBSERVED` when only the sparse
        lake-observed ladder did (the screen shows a low-confidence marker — the
        observed min gap can overestimate the true base step), or ``None`` when no
        step was presented at all.
    prev_close_session, prev_close_contiguous
        Calendar-contiguity provenance of the ``pct_change`` baseline
        (DEC-MOVERS-PREVCLOSE-CONTIGUITY-001) — the ISO session date the prior
        close actually came from + whether it IS the immediately-preceding NSE
        session (``False`` = the baseline provably skipped a nearer REGULAR
        session, or a stray non-session bar served it — a lake defect either
        way; ``None`` = no baseline / calendar abstained). Additive +
        defaulted; the row projector emits the pair ONLY on ``False``.
    oi_baseline_session, oi_baseline_contiguous
        The INDEPENDENT OI twin — a null-OI 1-day bar makes the OI baseline
        fall to the minute fallback on a possibly DIFFERENT day than the close
        baseline, so the two flags never share a verdict.
    prev_close_muhurat, oi_baseline_muhurat
        ``True`` iff that baseline is a legitimate Diwali **Muhurat** session
        close after the prior REGULAR session (the day-after-Diwali boundary —
        a healthy lake, never a hole; DEC-PREVSESSION-MUHURAT-COHERENCE-001).
        Judged INDEPENDENTLY per baseline like the contiguity pair; emitted
        ON TRUE ONLY. The engine's day anchors deliberately skip the Muhurat
        session instead (#1616) — the recorded two-law design.
    """

    underlying: str
    expiry: date_cls
    strike: Decimal
    opt_type: str
    symbol: str
    exchange: str
    segment: str
    ltp: Decimal
    spot: Decimal | None
    pct_change: Decimal | None
    oi: int | None
    oi_chg: int | None
    oi_chg_pct: Decimal | None
    volume: int
    value_paisa: int
    moneyness: Moneyness
    atm_offset: int | None
    moneyness_label: str
    iv: float | None = None
    delta: float | None = None
    strike_step_confidence: StepConfidence | None = None
    prev_close_session: str | None = None
    prev_close_contiguous: bool | None = None
    oi_baseline_session: str | None = None
    oi_baseline_contiguous: bool | None = None
    prev_close_muhurat: bool = False
    oi_baseline_muhurat: bool = False

    @property
    def name(self) -> str:
        """The contract's human display name, e.g. ``"NIFTY 30-Jun-2025 24000 CE"``.

        Joins the underlying, the deterministic ``DD-Mon-YYYY`` expiry
        (:func:`~brutex.dashboard.movers_futures.format_contract_date` — the ONE
        contract-date formatter, shared with the Futures tab so both read alike;
        DEC-MOVERS-FULL-CONTRACT-NAME-001), the strike (rendered without a trailing
        ``.0`` for a whole number), and the option type — the markets-screen Options
        row label. The full dated/strike label disambiguates a near-dated vs a
        far-dated ``24000 CE`` of the same underlying. The parts are all parsed,
        non-optional fields, so the label is always well-formed (never blank).
        """
        strike = self.strike.normalize()
        # `normalize()` can yield exponent form (``2.4E+4``) for a whole strike —
        # `quantize` back to an integer when there is no fractional part so the
        # label reads ``24000`` not ``2.4E+4``.
        if strike == strike.to_integral_value():
            strike = strike.quantize(Decimal(1))
        return f"{self.underlying} {format_contract_date(self.expiry)} {strike} {self.opt_type}"

    @property
    def ltp_inr(self) -> str:
        """The contract's LTP as a 2dp ``₹`` display string."""
        return format_inr(rupees_to_paisa(self.ltp))

    @property
    def spot_inr(self) -> str | None:
        """The underlying spot LTP as a 2dp ``₹`` string, or ``None`` if absent."""
        return format_inr(rupees_to_paisa(self.spot)) if self.spot is not None else None


def _sort_key(mover: OptionMover) -> tuple[str, str, str]:
    """Deterministic tiebreak so equal metrics never reorder run-to-run (Rule #4)."""
    return (mover.symbol, mover.exchange, mover.segment)


def rank_highest_oi(movers: Sequence[OptionMover], top_n: int) -> list[OptionMover]:
    """Top-``n`` contracts by open interest (``oi`` present), descending."""
    pool = [m for m in movers if m.oi is not None]
    pool.sort(key=lambda m: (-(m.oi or 0), _sort_key(m)))
    return pool[:top_n]


def rank_oi_gainers(movers: Sequence[OptionMover], top_n: int) -> list[OptionMover]:
    """Top-``n`` contracts with ``oi_chg_pct > 0``, descending (stable tiebreak)."""
    pool = [m for m in movers if m.oi_chg_pct is not None and m.oi_chg_pct > 0]
    pool.sort(key=lambda m: (-(m.oi_chg_pct or Decimal(0)), _sort_key(m)))
    return pool[:top_n]


def rank_oi_losers(movers: Sequence[OptionMover], top_n: int) -> list[OptionMover]:
    """Top-``n`` contracts with ``oi_chg_pct < 0``, ascending (most negative first)."""
    pool = [m for m in movers if m.oi_chg_pct is not None and m.oi_chg_pct < 0]
    pool.sort(key=lambda m: ((m.oi_chg_pct or Decimal(0)), _sort_key(m)))
    return pool[:top_n]


def rank_volume(
    movers: Sequence[OptionMover], top_n: int, *, min_volume: int = 0
) -> list[OptionMover]:
    """Top-``n`` contracts by cumulative fresh-session volume, descending.

    With the default ``min_volume == 0`` the pool is byte-identical to the prior
    ``volume > 0`` filter; a positive OPT-IN floor (DEC-MOVERS-MIN-VOLUME-001) drops
    contracts below it, via the COMMON
    :func:`~brutex.dashboard.movers_snapshot.value_meets_volume_floor` predicate.
    """
    pool = [m for m in movers if value_meets_volume_floor(m.volume, min_volume)]
    pool.sort(key=lambda m: (-m.volume, _sort_key(m)))
    return pool[:top_n]


def rank_value(movers: Sequence[OptionMover], top_n: int) -> list[OptionMover]:
    """Top-``n`` contracts by cumulative fresh-session traded value, descending."""
    pool = [m for m in movers if m.value_paisa > 0]
    pool.sort(key=lambda m: (-m.value_paisa, _sort_key(m)))
    return pool[:top_n]


def rank_price_gainers(movers: Sequence[OptionMover], top_n: int) -> list[OptionMover]:
    """Top-``n`` contracts with ``pct_change > 0``, descending (stable tiebreak)."""
    pool = [m for m in movers if m.pct_change is not None and m.pct_change > 0]
    pool.sort(key=lambda m: (-(m.pct_change or Decimal(0)), _sort_key(m)))
    return pool[:top_n]


def rank_price_losers(movers: Sequence[OptionMover], top_n: int) -> list[OptionMover]:
    """Top-``n`` contracts with ``pct_change < 0``, ascending (most negative first)."""
    pool = [m for m in movers if m.pct_change is not None and m.pct_change < 0]
    pool.sort(key=lambda m: ((m.pct_change or Decimal(0)), _sort_key(m)))
    return pool[:top_n]


#: Options category key → ranking function (the Options tab's pill set, in the
#: template's order). Mirrors ``movers_futures._FUTURES_CATEGORIES``.
_OPTIONS_CATEGORIES: Final[dict[str, Any]] = {
    "highest_oi": rank_highest_oi,
    "oi_gainers": rank_oi_gainers,
    "oi_losers": rank_oi_losers,
    "volume": rank_volume,
    "value": rank_value,
    "price_gainers": rank_price_gainers,
    "price_losers": rank_price_losers,
}


def compute_option_mover(
    *,
    underlying: str,
    expiry: date_cls,
    strike: Decimal,
    opt_type: str,
    symbol: str,
    exchange: str,
    ltp: Decimal,
    volume: int,
    value_paisa: int,
    oi: int | None,
    prev_close: Decimal | None,
    spot_ltp: Decimal | None,
    prev_oi: int | None,
    strike_step: Decimal | None = None,
    strike_step_confidence: StepConfidence | None = None,
    iv: float | None = None,
    delta: float | None = None,
) -> OptionMover:
    """Reduce one option contract's reads to an :class:`OptionMover` (pure).

    *ltp* / *volume* / *value_paisa* / *oi* are the contract's session reduction
    (last close, cumulative volume, cumulative traded value, last OI).
    *prev_close* is the contract's prior 1-day close (``None`` ⇒ no % change).
    *spot_ltp* is the underlying's spot LTP (``None`` ⇒ no spot column). *prev_oi*
    is the contract's prior 1-day OI. The money columns are snapped; the % / OI-%
    metrics keep full precision (CLAUDE.md §4).

    Moneyness (ITM/ATM/OTM + signed ATM±N) is DERIVED against *spot_ltp* at the
    snapshot instant (DEC-FNO-MONEYNESS-001) — never stored. *strike_step* is the
    DYNAMIC grid step for this ``(underlying, expiry)`` (derived by the caller from
    the screened strike ladder, so it works for EVERY F&O underlying, not just the
    3 indices); ``None`` ⇒ no ATM±N offset but the ITM/ATM/OTM bucket still resolves.
    A missing spot ⇒ :attr:`~brutex.data.moneyness.Moneyness.UNKNOWN`, never faked.

    Parameters
    ----------
    underlying, expiry, strike, opt_type
        The parsed contract coordinates (the display name's parts).
    symbol, exchange
        The contract groww_symbol + exchange (segment is always ``FNO``).
    ltp, volume, value_paisa, oi
        The session LTP (rupees), cumulative volume, cumulative value (paisa),
        and last OI (or ``None``).
    prev_close
        The contract's previous trading-day 1-day close (rupees), or ``None``.
    spot_ltp
        The underlying's spot LTP (rupees), or ``None`` when absent.
    prev_oi
        The contract's previous trading-day 1-day open interest, or ``None``.
    strike_step
        The ``(underlying, expiry)`` strike-grid step (rupees), or ``None``.
    strike_step_confidence
        The honesty label of *strike_step*'s source
        (DEC-MONEYNESS-REGISTRY-STEP-001 — ``REGISTRY`` = the authoritative vendor
        chain, ``OBSERVED`` = the sparse lake ladder only). Surfaced on the row ONLY
        when a *strike_step* is actually present (no step → nothing to qualify →
        ``None``, Golden Rule #1); a present step passed WITHOUT a label defaults to
        the honest ``OBSERVED`` floor so the pair can never split into
        "grid shown, confidence null".
    iv, delta
        The last kept session bar's STORED Greeks overlay values
        (DEC-GREEKS-ENGINE-PR3B-001 — sourced from the SAME session read, never
        recomputed here), or ``None`` (legacy partition / stamped-NULL bar /
        never-enriched). Passed through at FULL precision — a statistical
        value, never money-rounded (CLAUDE.md §4).

    Returns
    -------
    OptionMover
        The fully-computed row (money 2dp, statistical metrics full-precision).
    """
    snapped_ltp = round_money(ltp)
    snapped_spot = round_money(spot_ltp) if spot_ltp is not None else None
    snapped_prev = round_money(prev_close) if prev_close is not None else None
    pct = pct_change(snapped_ltp, snapped_prev) if snapped_prev is not None else None
    oi_chg = (oi - prev_oi) if (oi is not None and prev_oi is not None) else None
    oi_chg_pct = (
        pct_change(Decimal(oi), Decimal(prev_oi))
        if (oi is not None and prev_oi is not None and prev_oi != 0)
        else None
    )
    # Moneyness vs the snapshot spot (the snapped 2dp spot keeps it consistent with
    # the displayed spot/strike); UNKNOWN when the spot is absent (Golden Rule #1).
    view = moneyness_view(opt_type, strike, snapped_spot, strike_step)
    # The confidence label qualifies a PRESENT step only — with no step there is
    # nothing displayed to qualify, so it is honestly None (never a dangling flag).
    # The REVERSE split is also guarded (DEC-FNO-HONESTY-PACK-001): a caller passing
    # a real step WITHOUT a label gets the honest LOW-confidence floor (OBSERVED) —
    # a displayed grid can never carry a `null` confidence, which the payload doc
    # defines as "no grid presented" (Golden Rule #1).
    if strike_step is None:
        step_confidence = None
    elif strike_step_confidence is None:
        step_confidence = StepConfidence.OBSERVED
    else:
        step_confidence = strike_step_confidence
    return OptionMover(
        underlying=underlying,
        expiry=expiry,
        strike=strike,
        opt_type=opt_type,
        symbol=symbol,
        exchange=exchange,
        segment=_FNO_SEGMENT,
        ltp=snapped_ltp,
        spot=snapped_spot,
        pct_change=pct,
        oi=oi,
        oi_chg=oi_chg,
        oi_chg_pct=oi_chg_pct,
        volume=volume,
        value_paisa=value_paisa,
        moneyness=view.moneyness,
        atm_offset=view.atm_offset,
        moneyness_label=view.label,
        iv=iv,
        delta=delta,
        strike_step_confidence=step_confidence,
    )


def _option_row(mover: OptionMover) -> dict[str, Any]:
    """Project one mover to a markets-screen Options row (money 2dp, metrics full-precision)."""
    row: dict[str, Any] = {
        "name": mover.name,
        "symbol": mover.symbol,
        "exchange": mover.exchange,
        "segment": mover.segment,
        "spot": mover.spot_inr,
        "ltp": mover.ltp_inr,
        "pct_change": str(mover.pct_change) if mover.pct_change is not None else None,
        "oi": mover.oi,
        "oi_chg": mover.oi_chg,
        "oi_chg_pct": str(mover.oi_chg_pct) if mover.oi_chg_pct is not None else None,
        "volume": mover.volume,
        # The ONE moneyness cell the screen shows — bucket + signed depth as a single
        # string ("ITM -1" / "OTM +10" / "ATM"); DEC-FNO-MONEYNESS-001. The raw bucket
        # + signed ATM±N stay in the payload for sorting / programmatic use.
        "moneyness": mover.moneyness_label,
        "moneyness_bucket": mover.moneyness.value,
        "atm_offset": mover.atm_offset,
        # The strike-grid honesty label (DEC-MONEYNESS-REGISTRY-STEP-001): "registry"
        # = the AUTHORITATIVE vendor chain sized the ATM±N grid; "observed" = only
        # the sparse lake ladder did — the screen renders a low-confidence marker so
        # a possibly-overestimated step is never presented silently; null = no grid.
        "strike_step_confidence": (
            mover.strike_step_confidence.value if mover.strike_step_confidence is not None else None
        ),
        # STORED Greeks as of the snapshot (DEC-GREEKS-ENGINE-PR3B-001) — full
        # precision floats (never rounded — CLAUDE.md §4); null = honest NA
        # (legacy partition / stamped-NULL bar), the screen renders "—".
        "iv": mover.iv,
        "delta": mover.delta,
    }
    # EMIT-ON-FALSE, each baseline INDEPENDENT (DEC-MOVERS-PREVCLOSE-CONTIGUITY-001):
    # the additive provenance keys appear ONLY when the calendar PROVED that
    # baseline non-contiguous — contiguous and abstained rows stay byte-identical.
    if mover.prev_close_contiguous is False:
        row["prev_close_session"] = mover.prev_close_session
        row["prev_close_contiguous"] = mover.prev_close_contiguous
    if mover.oi_baseline_contiguous is False:
        row["oi_baseline_session"] = mover.oi_baseline_session
        row["oi_baseline_contiguous"] = mover.oi_baseline_contiguous
    # EMIT-ON-TRUE-ONLY, each baseline INDEPENDENT (DEC-PREVSESSION-MUHURAT-
    # COHERENCE-001): the baseline is the legitimate Diwali Muhurat session's
    # close/OI — a healthy lake, never a hole; non-Muhurat rows byte-identical.
    if mover.prev_close_muhurat:
        row["prev_close_muhurat"] = True
    if mover.oi_baseline_muhurat:
        row["oi_baseline_muhurat"] = True
    return row


def build_options_payload(
    movers: Sequence[OptionMover],
    *,
    interval: str,
    date: str,
    time: str | None,
    top_n: int,
    skipped: int,
    expiry_tier: str = "near",
    expiry_track: str = "all",
    min_volume: int | None = None,
    calendar_only_expiry: bool = False,
    pcr: Sequence[UnderlyingPcr] = (),
    latest_time: str | None = None,
) -> dict[str, Any]:
    """Assemble the markets-screen Options ``/api/v1/movers`` JSON from computed movers.

    Builds one ranked list **per options category** (``highest_oi`` /
    ``oi_gainers`` / ``oi_losers`` / ``volume`` / ``value`` / ``price_gainers`` /
    ``price_losers`` — the Options tab's pills), keyed by the category name so the
    page's ``j[category]`` lookup picks the active one. Adds the echo of the
    chosen ``date`` / ``interval`` / ``time`` and an honest ``note`` naming the
    selected ``expiry_tier`` (Near/Next/Far). Every value is JSON-native (money is
    a ``₹`` string, statistical metrics are full-precision strings, OI / volume are
    ints) so Starlette's ``JSONResponse`` never chokes on a ``Decimal``.

    Parameters
    ----------
    movers
        The computed option-contract rows (one per strike × CE/PE × expiry).
    interval, date, time, top_n
        Echoed snapshot coordinates.
    skipped
        Count of contracts dropped — no session bar on the selected day/time,
        OR an unreadable Parquet file (logged as ``movers.parquet_skip``;
        DEC-MOVERS-CORRUPT-SKIP-001) — reported honestly, never hidden
        (Golden Rule #1).
    expiry_tier
        The selected ``near`` / ``next`` / ``far`` expiry tier, echoed in the note.
    expiry_track
        The selected ``all`` / ``monthly`` / ``weekly`` cadence track, echoed in the
        note (appended to the tier label, e.g. ``Near Monthly``) unless ``all``.
    min_volume
        The OPT-IN "Top Volume" floor (DEC-MOVERS-MIN-VOLUME-001) applied at RANK
        time to the ``volume`` ranking only; ``None`` / ``0`` ⇒ the byte-identical
        no-floor default.
    calendar_only_expiry
        When ``True`` (from :attr:`OptionsCandidates.calendar_only_expiry`,
        DEC-MOVERS-CALENDAR-TIERS-001) the AUTHORITATIVE vendor calendar positioned a
        valid tier expiry but NO contract candles were pulled for it, so the note
        honestly says "no data pulled for this expiry" instead of implying the expiry
        does not exist.
    pcr
        The per-underlying Put-Call Ratio rows (DEC-MOVERS-OPTIONS-PCR-001,
        from :attr:`OptionsCandidates.pcr`), serialized to the top-level
        ``"pcr"`` payload key. A per-UNDERLYING aggregate over the FULL tier
        chain — a panel, not a row cell — so the rank-time ``top_n`` /
        ``min_volume`` never touch it. Defaulted ``()`` so every existing
        caller stays valid (``"pcr": []``).
    latest_time
        The ADDITIVE display-only echo of the resolved latest intraday minute
        (``"HH:MM"`` IST, DEC-MOVERS-ASOF-LATEST-DEFAULT-001) — shown as
        ``latest (HH:MM)`` while the default cut stays whole-day; ``None`` for an
        explicit *time* / daily interval / empty lake. Never affects a number.

    Returns
    -------
    dict[str, Any]
        The Options-tab payload: the seven ranked category lists + the echo +
        ``instrument_count`` / ``skipped_count`` / ``note`` / ``pcr``.
    """
    floor = normalize_min_volume(min_volume)
    ranked: dict[str, list[dict[str, Any]]] = {
        category: [
            _option_row(m)
            for m in (
                rank_volume(movers, top_n, min_volume=floor)
                if category == "volume"
                else rank_fn(movers, top_n)
            )
        ]
        for category, rank_fn in _OPTIONS_CATEGORIES.items()
    }
    # The tier label, with the cadence track appended (``Near Monthly``) unless the
    # default ``all`` track (then just ``Near`` — the prior wording is preserved).
    track_suffix = "" if expiry_track == "all" else f" {expiry_track.capitalize()}"
    tier_label = f"{expiry_tier.capitalize()}{track_suffix}"
    instrument_count = len(movers) + skipped
    floor_note = f" Min volume ≥ {floor:,}." if floor > 0 else ""
    if instrument_count == 0 and calendar_only_expiry:
        # HONEST NO-DATA (DEC-MOVERS-CALENDAR-TIERS-001): the vendor calendar KNOWS the
        # tier expiry but no contract candles were pulled for it — say so, never imply
        # the expiry does not exist (the tier was positioned correctly from the calendar).
        note = (
            f"The {tier_label} expiry is on the vendor calendar for {date}, but NO option "
            "contract candles have been pulled for it yet — pull the F&O options for this "
            "expiry on the Coverage page. (Reference-only lake view, CLAUDE.md §3.)"
        )
    elif instrument_count == 0:
        note = (
            f"No {tier_label}-expiry option contracts in the lake for {date} — pull the F&O "
            "options first on the Coverage page, or pick another expiry/date. "
            "(Reference-only lake view, CLAUDE.md §3.)"
        )
    elif not any(ranked.values()):
        note = (
            f"{instrument_count} {tier_label}-expiry option contract(s) in the lake, but none "
            f"had a bar at {date} {time or '(daily)'} on {interval} — pick another time/day."
            + floor_note
        )
    else:
        note = (
            f"As-of {date} {time or '(daily)'} · {interval} · {tier_label} expiry — "
            "reference-only lake view "
            "(CLAUDE.md §3); the contract's expiry nearest the selected date "
            "(Near/Next/Far) on the chosen cadence track (All/Monthly/Weekly); "
            "% is vs the contract's previous close, OI change is vs its prior day." + floor_note
        )
    return {
        "interval": interval,
        "date": date,
        "time": time,
        "top_n": top_n,
        "min_volume": floor,
        "instrument_count": instrument_count,
        "skipped_count": skipped,
        "latest_time": latest_time,
        "note": note,
        "pcr": [pcr_row_json(row) for row in pcr],
        **ranked,
    }


# ────────────────────────────────────────────────────────────────────────
# Live orchestration — reads the lake (raw 1m for OI + value), snapshots
# ────────────────────────────────────────────────────────────────────────


def _discover_options(
    lake_root: Path,
    *,
    exchange: str | None = None,
    datasets: list[dict[str, str]] | None = None,
) -> list[tuple[str, str, str, date_cls, Decimal, str]]:
    """List the lake's option contracts as ``(symbol, exchange, underlying, expiry, strike, type)``.

    Walks :func:`~brutex.dashboard.db_view.list_candle_datasets`, keeps ONLY the
    ``FNO`` ``1minute`` datasets whose symbol ends ``-CE`` / ``-PE`` (a future
    ends ``-FUT`` and is excluded), parses each into its contract coordinates (a
    non-parsing symbol is skipped), and returns one tuple per contract — every
    strike × CE/PE × expiry. When *exchange* is given only that exchange's
    contracts are kept (an exact per-exchange ranking). Returns ``[]`` when no
    options are present.

    Parameters
    ----------
    lake_root
        Top-level lake directory (the parent of ``bars/``).
    exchange
        Restrict to one exchange (``NSE`` / ``BSE``), or ``None`` for all.
    datasets
        Pre-discovered candle datasets (the route's memoised list, the O(1)-latency
        seam) or ``None`` to scan the lake now (see
        :func:`~brutex.dashboard.movers_snapshot.resolve_candle_datasets`).

    Returns
    -------
    list[tuple[str, str, str, datetime.date, decimal.Decimal, str]]
        ``(contract_symbol, exchange, underlying, expiry, strike, opt_type)`` per
        option contract found in the lake.
    """
    contracts: list[tuple[str, str, str, date_cls, Decimal, str]] = []
    for dataset in resolve_candle_datasets(lake_root, datasets):
        if dataset["segment"] != _FNO_SEGMENT or dataset["interval"] != "1minute":
            continue
        if exchange is not None and dataset["exchange"] != exchange:
            continue
        symbol = dataset["symbol"]
        parsed = parse_option_symbol(symbol)
        if parsed is None:
            continue
        underlying, expiry, strike, opt_type = parsed
        contracts.append((symbol, dataset["exchange"], underlying, expiry, strike, opt_type))
    return contracts


def load_registry_expiries(
    registry_path: Path, *, exchange: str | None = None
) -> dict[str, set[date_cls]] | None:
    """Read the FULL vendor expiry calendar per underlying from ``registry.duckdb``, or ``None``.

    The Near/Next/Far + Weekly/Monthly tier classification is only correct when fed
    the COMPLETE vendor expiry calendar — a sparsely-pulled lake (only some expiries
    have contract candles on disk) mislabels the tiers, because a missing 21-Jul /
    28-Jul makes the lake-partition set jump straight to 04-Aug for "Far"
    (DEC-MOVERS-CALENDAR-TIERS-001). The full calendar already lives at read time in
    the metadata-pull registry (:meth:`RegistryStore.list_expiries`, populated by
    :func:`~brutex.dashboard.metadata_pull.pull_expiries_contracts` from the vendor
    ``get_expiries`` list), so this resolves the AUTHORITATIVE expiry set per
    underlying from there.

    GRACEFUL FALLBACK (never break a CLI-only-pulled lake — Golden Rule #1): a
    registry that is ABSENT (no file — a lake pulled only with ``brutex fetch``,
    which seeds no registry), EMPTY (the metadata phase never ran), or UNREADABLE
    (a stale / version-incompatible cache) yields ``None``, so the caller falls back
    to the CURRENT lake-partition tier derivation. Best-effort: a
    :class:`RegistryError` is logged + swallowed → ``None`` (the page never 500s on
    a bad cache; it degrades to the lake-partition set).

    Parameters
    ----------
    registry_path
        The ``<lake_root>/registry.duckdb`` path
        (:func:`~brutex.dashboard.db_view.registry_path_for`).
    exchange
        Restrict to one exchange (``NSE`` / ``BSE``) to mirror the screened-contract
        scope, or ``None`` for every exchange.

    Returns
    -------
    dict[str, set[datetime.date]] | None
        ``underlying → its full set of vendor expiry dates`` (every expiry, NOT
        date-filtered — the tier resolver applies the as-of-date cut), or ``None``
        when the registry is absent / empty / unreadable.
    """
    if not registry_path.exists():
        return None
    try:
        with RegistryStore.open(registry_path) as registry:
            rows = registry.list_expiries(exch=exchange)
    except RegistryError as exc:
        # A stale / version-incompatible / locked registry must never 500 the page —
        # degrade to the lake-partition derivation (Golden Rule #1 / #10).
        _LOG.warning("movers.options.registry_unreadable", error=str(exc))
        return None
    if not rows:
        return None
    calendar: dict[str, set[date_cls]] = {}
    for row in rows:
        calendar.setdefault(row.underlying, set()).add(row.expiry_date)
    return calendar


def _monthly_expiries(expiries: set[date_cls]) -> set[date_cls]:
    """The subset of *expiries* that are a monthly settlement (last-in-calendar-month).

    Groups the distinct expiry dates by ``(year, month)`` and keeps the **max** of
    each group — the LAST expiry in that calendar month, which on the NSE settles
    as the monthly contract. Every other date is a (non-terminal) weekly. This is
    purely structural: it reads the dates actually present in the lake, NEVER a
    hardcoded expiry-weekday rule (Golden Rule #1), so it stays correct under any
    NSE cadence change and yields the right answer for every permutation — a stock
    or weekly-less index has one expiry per month (that one date is its monthly, an
    empty weekly track), an index with weeklies has several per month (the last is
    the monthly, the earlier ones the weeklies). An empty input yields an empty set.

    Parameters
    ----------
    expiries
        The distinct expiry dates to classify (one underlying's live expiries).

    Returns
    -------
    set[datetime.date]
        The dates that are the last expiry of their calendar month.
    """
    by_month: dict[tuple[int, int], list[date_cls]] = {}
    for expiry in expiries:
        by_month.setdefault((expiry.year, expiry.month), []).append(expiry)
    return {max(group) for group in by_month.values()}


def _track_pool(expiries: set[date_cls], track: str) -> set[date_cls]:
    """Keep only *expiries* on the chosen ``track`` (``all`` / ``monthly`` / ``weekly``).

    ``monthly`` keeps the last-in-month settlements (:func:`_monthly_expiries`);
    ``weekly`` keeps the rest (the non-terminal weeklies); ``all`` — and any other
    value — returns the set unchanged (the prior behaviour). The caller
    :func:`run_options_snapshot` already normalises an unknown selector to ``all``,
    so the catch-all return is the honest belt-and-suspenders fallback.
    """
    if track == "monthly":
        return _monthly_expiries(expiries)
    if track == "weekly":
        return expiries - _monthly_expiries(expiries)
    return expiries


def _tier_expiry_by_underlying(
    contracts: Sequence[tuple[str, str, str, date_cls, Decimal, str]],
    *,
    on_date: date_cls,
    tier: str,
    track: str = "all",
    calendar_expiries: Mapping[str, set[date_cls]] | None = None,
) -> dict[str, date_cls]:
    """Map each underlying → its Near/Next/Far expiry on/after *on_date*, within *track*.

    For each underlying the DISTINCT expiries that are ``>= on_date`` are first
    narrowed to the chosen ``track`` (``all`` / ``monthly`` / ``weekly`` via
    :func:`_track_pool`), then sorted ascending; the *tier* (``near`` / ``next`` /
    ``far`` via :data:`_EXPIRY_TIERS`, an unknown / blank tier falling back to
    ``near``) picks the 1st / 2nd / 3rd of THAT track. So ``monthly`` + ``next``
    is "the second monthly from the snapshot date", ``weekly`` + ``near`` is "the
    nearest weekly", etc. An underlying that has fewer live expiries on the track
    than the tier needs is OMITTED — that serial month is simply not listed yet, so
    it contributes nothing (honest, never faked — Golden Rule #1). Already-expired
    contracts (``expiry < on_date``) are excluded.

    AUTHORITATIVE CALENDAR (DEC-MOVERS-CALENDAR-TIERS-001): when *calendar_expiries*
    supplies an underlying's FULL vendor expiry calendar (from
    :func:`load_registry_expiries`), THAT set — not the on-disk lake-partition set —
    is the expiry universe the tier/track classify over. This fixes the
    sparsely-pulled-lake mislabel: with the full July ``{07,14,21,28-Jul}`` calendar,
    ``_monthly_expiries`` correctly settles ``28-Jul`` as the monthly + ``07/14/21``
    as weeklies, so Weekly + Near/Next/Far = ``07/14/21-Jul`` even when only some of
    those expiries have contract candles on disk. An underlying ABSENT from the
    calendar (or a ``None`` calendar — a registry-less lake) falls back to its
    lake-partition set, so a CLI-only lake is never broken. The returned tier expiry
    can be a calendar date with ZERO pulled contracts; the caller surfaces that
    honestly (an empty tier with a "no data pulled for this expiry" note) rather than
    skipping to a wrong serial expiry.

    Parameters
    ----------
    contracts
        The lake's option contracts as ``(symbol, exchange, underlying, expiry,
        strike, type)`` (the :func:`_discover_options` shape).
    on_date
        The snapshot date the expiry tier is resolved as-of.
    tier
        The ``?expiry=`` selector, lower-cased (``near`` / ``next`` / ``far``).
    track
        The ``?track=`` selector, lower-cased (``all`` / ``monthly`` / ``weekly``;
        an unknown value behaves as ``all`` via :func:`_track_pool`).
    calendar_expiries
        The AUTHORITATIVE per-underlying vendor expiry calendar (every expiry, not
        date-filtered) from :func:`load_registry_expiries`, or ``None`` to derive the
        tiers from the lake-partition contract set (the graceful fallback).

    Returns
    -------
    dict[str, datetime.date]
        ``underlying → the tier's expiry on the track`` for every underlying that
        has one (a calendar tier may name an expiry with no pulled contracts).
    """
    idx = _EXPIRY_TIERS.get(tier, 0)
    live: dict[str, set[date_cls]] = {}
    for _symbol, _exchange, underlying, expiry, _strike, _opt in contracts:
        if expiry >= on_date:
            live.setdefault(underlying, set()).add(expiry)
    target: dict[str, date_cls] = {}
    for underlying, lake_expiries in live.items():
        # AUTHORITATIVE: the full vendor calendar (>= on_date) for this underlying when
        # supplied; else the on-disk lake set — the graceful registry-less fallback.
        universe = _tier_universe(
            underlying, lake_expiries, on_date=on_date, calendar_expiries=calendar_expiries
        )
        ordered = sorted(_track_pool(universe, track))
        if idx < len(ordered):
            target[underlying] = ordered[idx]
    return target


def _tier_universe(
    underlying: str,
    lake_expiries: set[date_cls],
    *,
    on_date: date_cls,
    calendar_expiries: Mapping[str, set[date_cls]] | None,
) -> set[date_cls]:
    """The expiry universe the tier classifies over — the vendor calendar or the lake set.

    Returns the underlying's FULL vendor calendar restricted to ``>= on_date`` when
    *calendar_expiries* carries it (the AUTHORITATIVE source — DEC-MOVERS-CALENDAR-
    TIERS-001), else the *lake_expiries* it was discovered with (the graceful
    fallback for a registry-less lake or an underlying the registry never enumerated).
    """
    if calendar_expiries is not None:
        calendar = calendar_expiries.get(underlying)
        if calendar:
            return {expiry for expiry in calendar if expiry >= on_date}
    return lake_expiries


def normalize_options_selectors(
    expiry_tier: str | None, expiry_track: str | None
) -> tuple[str, str]:
    """Normalise the raw ``?expiry=`` / ``?track=`` selectors to canonical tokens.

    Lower-cases both; a blank / ``None`` tier ⇒ ``near``; a blank / ``None`` /
    unknown track ⇒ ``all`` (the structural-cadence default). The single source of
    truth for the normalisation so the cache key and the computed candidate set
    agree on the canonical tier/track (CLAUDE.md §11 COMMON) — an unknown track
    such as ``"foo"`` keys identically to ``all``, never a stale split.

    Returns
    -------
    tuple[str, str]
        ``(tier, track)`` — canonical lower-case tokens.
    """
    tier = (expiry_tier or "near").lower()
    track = (expiry_track or "all").lower()
    if track not in _EXPIRY_TRACKS:
        track = "all"
    return tier, track


@dataclass(frozen=True, slots=True)
class OptionsCandidates:
    """The cacheable options candidate set for one snapshot coordinate (pre-ranking).

    The expensive, ``top_n``-independent product of one lake scan — the per-contract
    :class:`OptionMover` list + the skipped count + the normalised snapshot date +
    the canonical expiry tier/track the set was computed for. Caching THIS (keyed by
    ``(exchange, interval, date, time, tier, track)``) makes a sub-filter / ``top_n``
    change an O(1) hit + a cheap re-rank (:func:`build_options_payload`), never a
    re-scan (DEC-MOVERS-O1-LATENCY-001).

    Attributes
    ----------
    movers
        The computed option-contract rows (one per strike × CE/PE on the tier expiry).
    skipped
        Count of contracts dropped (no session bar on the selected day/time,
        or an unreadable Parquet file logged as ``movers.parquet_skip``).
    date
        The normalised ISO ``YYYY-MM-DD`` snapshot day.
    expiry_tier, expiry_track
        The canonical tier / cadence the set was computed for (echoed to the payload).
    calendar_only_expiry
        ``True`` when the AUTHORITATIVE vendor calendar resolved a tier expiry for at
        least one underlying but NO contract candles were pulled for ANY of the
        resolved tier expiries (so the screen is honestly empty for a calendar-known
        expiry — DEC-MOVERS-CALENDAR-TIERS-001 — never silently skipped to a wrong
        serial expiry). ``False`` when the lake had at least one matching contract, or
        when no tier expiry resolved at all.
    pcr
        The per-underlying Put-Call Ratio rows folded from the SAME session
        reads that built :attr:`movers` (DEC-MOVERS-OPTIONS-PCR-001 — zero new
        I/O; one row per lake-DISCOVERED tier-universe underlying, including
        an all-skipped ``empty`` chain). HONEST ENVELOPE: an underlying whose
        CALENDAR tier expiry has zero pulled contracts is dropped by the tier
        filter BEFORE discovery counting, so it yields no row — mirroring the
        movers table exactly (panel/table agreement; the whole-payload case is
        :attr:`calendar_only_expiry` — round-7 hostile-review fold). Defaulted
        LAST so every existing constructor call site stays valid; the
        early-return paths carry the honest ``()``.
    """

    movers: tuple[OptionMover, ...]
    skipped: int
    date: str
    expiry_tier: str
    expiry_track: str
    calendar_only_expiry: bool = False
    pcr: tuple[UnderlyingPcr, ...] = ()


def compute_options_candidates(
    *,
    lake_root: Path,
    interval: str,
    date: str | None,
    time: str | None,
    expiry_tier: str = "near",
    expiry_track: str = "all",
    exchange: str | None = None,
    names: Mapping[str, str] | None = None,
    datasets: list[dict[str, str]] | None = None,
    calendar_expiries: Mapping[str, set[date_cls]] | None = None,
    registry_chain_for: Callable[[str, date_cls], Sequence[Decimal] | None] | None = None,
) -> OptionsCandidates:
    """Scan the lake → the :class:`OptionsCandidates` for one snapshot coordinate.

    The expensive, cacheable core extracted from :func:`run_options_snapshot`:
    discovers EVERY option contract in the lake (every strike × CE/PE × expiry —
    NOT collapsed per underlying; optionally scoped to *exchange*), keeps only the
    contracts on the selected Near/Next/Far expiry within the chosen cadence track,
    reads each contract's selected-day session (raw 1-minute, OI + cumulative value
    preserved) up to *time*, takes the contract's prior 1-day close + OI and its
    underlying spot's LTP, and computes the per-contract :class:`OptionMover`.
    Ranking is the cheap, ``top_n``-dependent step done by
    :func:`build_options_payload`. An empty / options-less lake (or an unknown
    interval) yields an empty :class:`OptionsCandidates` (Golden Rule #1).

    Parameters
    ----------
    lake_root, interval, date, time, expiry_tier, expiry_track, names
        See :func:`run_options_snapshot`.
    exchange
        Restrict the screened contracts to one exchange (``NSE`` / ``BSE``) for an
        exact per-exchange ranking; ``None`` screens every exchange.
    calendar_expiries
        The AUTHORITATIVE per-underlying vendor expiry calendar from
        :func:`load_registry_expiries` (DEC-MOVERS-CALENDAR-TIERS-001) — when present
        the Near/Next/Far + Weekly/Monthly tiers classify over the FULL calendar, so a
        sparsely-pulled lake no longer mislabels a tier. ``None`` derives the tiers
        from the lake-partition contract set (the graceful fallback).
    registry_chain_for
        The LAZY ``(underlying, expiry) → strike chain`` loader over the
        AUTHORITATIVE vendor registry
        (:func:`~brutex.dashboard.registry_strikes.make_registry_chain_loader`,
        DEC-MONEYNESS-REGISTRY-STEP-001) — invoked ONLY for the snapshot's own
        tier-target keys, so a growing registry never costs a whole-table read
        (Golden Rule #5, DEC-FNO-HONESTY-PACK-001). When a key's chain is present
        the ATM±N strike-grid step derives from the FULL chain (confidence
        ``registry``), so a sparsely-pulled expiry no longer overestimates the
        step. ``None`` — or a per-key ``None`` (absent registry, or a
        registry↔lake underlying-naming mismatch, honest graceful degrade) —
        falls back to the screened lake ladder with the low-confidence
        ``observed`` label.

    Returns
    -------
    OptionsCandidates
        The contract rows + skipped count + normalised date + canonical tier/track +
        the ``calendar_only_expiry`` honest-no-data flag + the per-underlying PCR
        rows (DEC-MOVERS-OPTIONS-PCR-001).

    Raises
    ------
    ValueError
        On a malformed *date* / *time* (the route maps it to a 400). The PCR
        fold's fail-loud law can raise too (DEC-MOVERS-OPTIONS-PCR-001): a
        negative OI/volume cell (out-of-band lake VALUE corruption — the
        ingest boundary clamps negatives, so only a tampered Parquet reaches
        it) raises :class:`~brutex.data.pcr.PcrCorruptionError` (a
        ``ValueError`` subclass the route maps to an honest 500, never the
        client-error 400), and a PCR coverage/side wiring mismatch raises a
        plain ``ValueError`` (unreachable by construction; unit-pinned).
    """
    minutes = interval_minutes(interval)
    day = parse_snapshot_date(date)
    date_str = day.isoformat()
    cutoff_min = None if (minutes is None or minutes >= _DAILY_MIN_FLOOR) else _cutoff_minute(time)
    resolved_names = names or {}
    tier, track = normalize_options_selectors(expiry_tier, expiry_track)
    contracts = _discover_options(lake_root, exchange=exchange, datasets=datasets)
    # Keep ONLY the contracts on the selected Near/Next/Far expiry — resolved per
    # underlying as-of the snapshot day over the AUTHORITATIVE vendor calendar (when
    # supplied) WITHIN the chosen cadence track; an underlying without that serial
    # expiry on the track drops out honestly. The calendar fixes the sparsely-pulled
    # lake mislabel (DEC-MOVERS-CALENDAR-TIERS-001).
    target_expiry = _tier_expiry_by_underlying(
        contracts, on_date=day, tier=tier, track=track, calendar_expiries=calendar_expiries
    )
    contracts = [c for c in contracts if target_expiry.get(c[2]) == c[3]]
    # HONEST NO-DATA: a tier expiry the vendor calendar KNOWS but for which NO contract
    # candles were pulled yields zero matching contracts — flag it (only when the
    # interval itself is valid) so the payload says "no data pulled for this expiry"
    # rather than the screen looking like the expiry does not exist (the calendar
    # correctly positioned the tier; the data is just absent — Golden Rule #1).
    calendar_only_expiry = minutes is not None and bool(target_expiry) and not contracts
    if minutes is None or not contracts:
        return OptionsCandidates(
            movers=(),
            skipped=0,
            date=date_str,
            expiry_tier=tier,
            expiry_track=track,
            calendar_only_expiry=calendar_only_expiry,
        )

    # PCR harvest maps (DEC-MOVERS-OPTIONS-PCR-001), built BEFORE any read can
    # fail, all keyed by the PARSED underlying ``c[2]`` (rename-proof — the
    # display transform is carried separately so the panel label provably
    # equals the table's). ``pcr_legs_total`` is the honest "m" denominator —
    # the tier-filtered chain size counted PRE-read, so an unreadable/empty leg
    # still counts in m. Every tier-filtered contract's underlying has a
    # ``target_expiry`` entry by construction of the tier filter above; if that
    # invariant ever breaks, the dict-comp KeyError trips loudly HERE — never a
    # fabricated row downstream (Golden Rule #1). O(contracts) — no quadratic.
    pcr_legs_total: dict[str, int] = dict(Counter(c[2] for c in contracts))
    pcr_expiry: dict[str, date_cls] = {u: target_expiry[u] for u in pcr_legs_total}
    pcr_display: dict[str, str] = {u: resolved_names.get(u, u) for u in pcr_legs_total}
    pcr_legs: list[PcrLeg] = []

    # Resolve the DYNAMIC strike-grid step ONCE per (underlying, expiry) — the min
    # positive gap over the AUTHORITATIVE registry chain when the LAZY loader yields
    # one (DEC-MONEYNESS-REGISTRY-STEP-001; invoked only for THESE tier-target keys —
    # the bounded read, Golden Rule #5), else over the screened lake ladder (CE + PE
    # share the grid) with an honest low-confidence label. This works for EVERY F&O
    # underlying (all stocks + indices), never a hardcoded NSE rule (Golden Rule #1).
    # O(K log K) once here; the per-contract ATM±N is O(1).
    strikes_by_key: dict[tuple[str, date_cls], list[Decimal]] = {}
    for _sym, _exch, c_underlying, c_expiry, c_strike, _opt in contracts:
        strikes_by_key.setdefault((c_underlying, c_expiry), []).append(c_strike)
    step_by_key = {
        key: resolve_strike_step(
            strikes,
            authoritative_strikes=(
                registry_chain_for(key[0], key[1]) if registry_chain_for is not None else None
            ),
        )
        for key, strikes in strikes_by_key.items()
    }

    movers: list[OptionMover] = []
    skipped = 0
    skip_errors = movers_read_skip_errors()
    warn_state = _NoncontiguousWarnState()
    expected_session_for = make_expected_session_resolver(day)
    for symbol, contract_exchange, underlying, expiry, strike, opt_type in contracts:
        try:
            read = _read_future_session(
                lake_root,
                exchange=contract_exchange,
                segment=_FNO_SEGMENT,
                symbol=symbol,
                day=day,
                cutoff_min=cutoff_min,
            )
            if read is None:  # no session bar on the selected day/time → skip, never faked.
                skipped += 1
                continue
            # `_read_future_session` returns the HLC3 turnover as its 3rd field for ANY
            # FNO contract (DEC-MOVERS-VWAP-VALUE-001); options uses it DIRECTLY so the
            # contract's session frame is read ONCE (DEC-MOVERS-OPTIONS-VALUE-DEDUP-001).
            # The trailing iv/delta are the LAST kept bar's STORED Greeks overlay
            # (DEC-GREEKS-ENGINE-PR3B-001) off that SAME single read — never a second
            # I/O, never recomputed at render; None for a legacy pre-Greeks partition
            # or a stamped-NULL bar (honest NA, Golden Rule #1).
            ltp, volume, value_paisa, oi, iv, delta = read
            close_baseline = resolve_prev_close(
                lake_root, exchange=contract_exchange, segment=_FNO_SEGMENT, symbol=symbol, day=day
            )
            oi_baseline = resolve_prev_oi(
                lake_root, exchange=contract_exchange, segment=_FNO_SEGMENT, symbol=symbol, day=day
            )
            # The underlying spot is probed under the PARSED underlying (NIFTY); the
            # row's display name keeps the parsed underlying (the strike/type are in
            # `OptionMover.name`), optionally renamed via `names`.
            spot_ltp = _underlying_spot_ltp(
                lake_root,
                exchange=contract_exchange,
                underlying=underlying,
                day=day,
                cutoff_min=cutoff_min,
            )
        except skip_errors as exc:
            # A corrupt / unreadable Parquet for ONE option must not 500 the screen —
            # skip it (logged + counted), never fabricate (DEC-MOVERS-CORRUPT-SKIP-001).
            log_movers_skip(source="options", symbol=symbol, error=str(exc))
            skipped += 1
            continue
        # PCR leg capture (DEC-MOVERS-OPTIONS-PCR-001): from the PARSED loop
        # variables (never the display-renamed mover), off the SAME single
        # session read — zero new I/O. The two skip paths above append nothing:
        # those legs live only in the pre-read ``pcr_legs_total`` denominator.
        pcr_legs.append(PcrLeg(underlying=underlying, opt_type=opt_type, oi=oi, volume=volume))
        mover = compute_option_mover(
            underlying=resolved_names.get(underlying, underlying),
            expiry=expiry,
            strike=strike,
            opt_type=opt_type,
            symbol=symbol,
            exchange=contract_exchange,
            ltp=ltp,
            volume=volume,
            value_paisa=value_paisa,
            oi=oi,
            prev_close=close_baseline.value,
            spot_ltp=spot_ltp,
            prev_oi=oi_baseline.value,
            strike_step=step_by_key.get((underlying, expiry), _NO_STEP).step,
            strike_step_confidence=step_by_key.get((underlying, expiry), _NO_STEP).confidence,
            iv=iv,
            delta=delta,
        )
        # Baseline calendar-contiguity provenance, close + OI judged INDEPENDENTLY
        # (DEC-MOVERS-PREVCLOSE-CONTIGUITY-001 — mirrors the Futures loop). Served
        # values are untouched; each strictly-False verdict gets ONE capped WARN.
        # A legitimate day-after-Diwali Muhurat baseline is contiguous-True +
        # flagged per baseline with ONE per-build INFO instead (never a false
        # WARN — DEC-PREVSESSION-MUHURAT-COHERENCE-001).
        cal_exchange = _calendar_exchange(contract_exchange)
        expected_session = expected_session_for(cal_exchange)
        close_iso, close_contig, close_muhurat = prev_baseline_provenance(
            close_baseline.found_date, day, exchange=cal_exchange, expected_session=expected_session
        )
        oi_iso, oi_contig, oi_muhurat = prev_baseline_provenance(
            oi_baseline.found_date, day, exchange=cal_exchange, expected_session=expected_session
        )
        mover = replace(
            mover,
            prev_close_session=close_iso,
            prev_close_contiguous=close_contig,
            oi_baseline_session=oi_iso,
            oi_baseline_contiguous=oi_contig,
            prev_close_muhurat=close_muhurat,
            oi_baseline_muhurat=oi_muhurat,
        )
        if close_iso is not None and close_contig is False:
            warn_prev_baseline_noncontiguous(
                warn_state,
                kind="prev_close",
                exchange=contract_exchange,
                segment=_FNO_SEGMENT,
                symbol=symbol,
                snapshot_day=day,
                found_session=close_iso,
                expected_session=expected_session,
            )
        if oi_iso is not None and oi_contig is False:
            warn_prev_baseline_noncontiguous(
                warn_state,
                kind="oi",
                exchange=contract_exchange,
                segment=_FNO_SEGMENT,
                symbol=symbol,
                snapshot_day=day,
                found_session=oi_iso,
                expected_session=expected_session,
            )
        if close_muhurat and close_iso is not None:
            info_prev_baseline_muhurat(
                warn_state,
                kind="prev_close",
                exchange=contract_exchange,
                segment=_FNO_SEGMENT,
                symbol=symbol,
                snapshot_day=day,
                found_session=close_iso,
            )
        if oi_muhurat and oi_iso is not None:
            info_prev_baseline_muhurat(
                warn_state,
                kind="oi",
                exchange=contract_exchange,
                segment=_FNO_SEGMENT,
                symbol=symbol,
                snapshot_day=day,
                found_session=oi_iso,
            )
        movers.append(mover)

    return OptionsCandidates(
        movers=tuple(movers),
        skipped=skipped,
        date=date_str,
        expiry_tier=tier,
        expiry_track=track,
        pcr=compute_pcr_rows(
            pcr_legs,
            legs_total=pcr_legs_total,
            target_expiry=pcr_expiry,
            display_names=pcr_display,
        ),
    )


def run_options_snapshot(
    *,
    lake_root: Path,
    interval: str,
    date: str | None,
    time: str | None,
    top_n: int,
    expiry_tier: str = "near",
    expiry_track: str = "all",
    exchange: str | None = None,
    names: Mapping[str, str] | None = None,
    min_volume: int | None = None,
    calendar_expiries: Mapping[str, set[date_cls]] | None = None,
    registry_chain_for: Callable[[str, date_cls], Sequence[Decimal] | None] | None = None,
) -> dict[str, Any]:
    """Compute the options movers snapshot over the lake → the ``/api/v1/movers`` payload.

    The thin compose of :func:`compute_options_candidates` (the expensive, cacheable
    scan) + :func:`build_options_payload` (the cheap, ``top_n``-dependent rank).
    Behaviour is unchanged from the pre-split version.

    The ``expiry_tier`` (``near`` / ``next`` / ``far``) keeps ONLY the contracts on
    the selected expiry — resolved PER underlying as the 1st / 2nd / 3rd distinct
    live expiry on/after the snapshot date (:func:`_tier_expiry_by_underlying`),
    WITHIN the chosen ``expiry_track`` cadence (``all`` / ``monthly`` / ``weekly``)
    — so a near-dated and a far-dated ``24000 CE`` never mix in one ranking, and
    ``monthly`` + ``near`` ranks the nearest monthly settlement only.

    Parameters
    ----------
    lake_root
        Top-level lake directory (the parent of ``bars/``).
    interval, date, time, top_n
        The snapshot coordinates (mirrors :func:`run_movers_snapshot`).
    expiry_tier
        The ``?expiry=`` selector (``near`` / ``next`` / ``far``; blank / unknown ⇒
        ``near``) — which serial expiry, as-of *date*, to rank.
    expiry_track
        The ``?track=`` selector (``all`` / ``monthly`` / ``weekly``; blank /
        unknown ⇒ ``all``) — the cadence the Near/Next/Far tier ranks within. The
        classification is structural (last-in-month = monthly), so it needs no NSE
        weekday rule (Golden Rule #1).
    exchange
        Restrict the screened contracts to one exchange (``NSE`` / ``BSE``); ``None``
        screens every exchange.
    names
        Optional ``underlying → display name`` map; defaults to the underlying
        symbol itself when absent (the contract name keeps the strike + type).
    min_volume
        The OPT-IN "Top Volume" floor (DEC-MOVERS-MIN-VOLUME-001); ``None`` / ``0`` ⇒
        the byte-identical no-floor default.
    calendar_expiries
        The AUTHORITATIVE per-underlying vendor expiry calendar from
        :func:`load_registry_expiries` (DEC-MOVERS-CALENDAR-TIERS-001), or ``None`` to
        derive the tiers from the lake-partition contract set.
    registry_chain_for
        The LAZY per-``(underlying, expiry)`` AUTHORITATIVE strike-chain loader
        (DEC-MONEYNESS-REGISTRY-STEP-001 — see :func:`compute_options_candidates`),
        or ``None`` for the lake-ladder step with its honest ``observed`` label.

    Returns
    -------
    dict[str, Any]
        The Options-tab ``/api/v1/movers`` payload.

    Raises
    ------
    ValueError
        On a malformed *date* / *time* (the route maps it to a 400) — plus
        the PCR fold's raise surface documented on
        :func:`compute_options_candidates` (corruption →
        :class:`~brutex.data.pcr.PcrCorruptionError` → route 500).
    """
    candidates = compute_options_candidates(
        lake_root=lake_root,
        interval=interval,
        date=date,
        time=time,
        expiry_tier=expiry_tier,
        expiry_track=expiry_track,
        exchange=exchange,
        names=names,
        calendar_expiries=calendar_expiries,
        registry_chain_for=registry_chain_for,
    )
    return build_options_payload(
        candidates.movers,
        interval=interval,
        date=candidates.date,
        time=time,
        top_n=top_n,
        skipped=candidates.skipped,
        expiry_tier=candidates.expiry_tier,
        expiry_track=candidates.expiry_track,
        min_volume=min_volume,
        calendar_only_expiry=candidates.calendar_only_expiry,
        pcr=candidates.pcr,
    )
