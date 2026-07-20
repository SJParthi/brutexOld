"""Offline, deterministic tests for ``bhavcopy_tail_study.py``.

Run:  python -m pytest docs/design/options-strategies/11-expiry-day-tail-study/tests_offline.py

Self-contained BY DESIGN: lives beside the harness (NOT under the repo's
``tests/`` tree) so the repo coverage/mutation gates are not dragged in,
and is deliberately NOT wired into ci.yml.  No network is ever touched —
every downloader test injects a dict-backed ``fetch`` callable; synthetic
bhavcopy zips (both format eras) are built at test runtime (no binary
fixtures are committed).
"""

# ruff: noqa: S101, PLR2004, PT018
# Rationale: this suite sits outside tests/**, so the repo's tests/**
# per-file-ignores (assert, magic-number test values, composite asserts)
# are mirrored here file-locally.

from __future__ import annotations

import io
import sys
import zipfile
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bhavcopy_tail_study as hs

# ---------------------------------------------------------------------------
# Fixture builders — synthetic bhavcopy zips, both format eras
# ---------------------------------------------------------------------------

_MON = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")


def _old_expiry(d: date) -> str:
    """Render DD-Mmm-YYYY (title-case month exercises the parser's upper())."""
    return f"{d.day:02d}-{_MON[d.month - 1].title()}-{d.year:04d}"


def _old_csv(rows: list[dict[str, Any]]) -> bytes:
    """Render the pre-~Jul-2024 derivatives bhavcopy CSV byte-realistically
    (incl. the real files' trailing empty column)."""
    header = (
        "INSTRUMENT,SYMBOL,EXPIRY_DT,STRIKE_PR,OPTION_TYP,OPEN,HIGH,LOW,"
        "CLOSE,SETTLE_PR,CONTRACTS,VAL_INLAKH,OPEN_INT,CHG_IN_OI,TIMESTAMP,\r\n"
    )
    out = [header]
    for row in rows:
        kind = row["kind"]
        if kind == "FUT":
            instrument, option_typ, strike = "FUTIDX", "XX", "0"
        else:
            instrument, option_typ, strike = "OPTIDX", kind, str(row["strike"])
        out.append(
            ",".join(
                (
                    instrument,
                    row["symbol"],
                    _old_expiry(row["expiry"]),
                    strike,
                    option_typ,
                    str(row["open"]),
                    str(row["high"]),
                    str(row["low"]),
                    str(row["close"]),
                    str(row.get("settle", row["close"])),
                    "100",
                    "1.23",
                    "5000",
                    "10",
                    _old_expiry(row["trade_date"]),
                )
            )
            + ",\r\n"
        )
    return "".join(out).encode("utf-8")


def _new_csv(rows: list[dict[str, Any]]) -> bytes:
    """Render the ~Jul-2024+ UDiFF bhavcopy CSV (subset + realistic extras)."""
    header = (
        "TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,"
        "XpryDt,FininstrmActlXpryDt,StrkPric,OptnTp,FinInstrmNm,OpnPric,HghPric,"
        "LwPric,ClsPric,LastPric,PrvsClsgPric,UndrlygPric,SttlmPric,OpnIntrst,"
        "ChngInOpnIntrst,TtlTradgVol,TtlTrfVal,TtlNbOfTxsExctd,SsnId,NewBrdLotQty,"
        "Rmks,Rsvd1,Rsvd2,Rsvd3,Rsvd4\r\n"
    )
    out = [header]
    for row in rows:
        kind = row["kind"]
        if kind == "FUT":
            instr_type, optn_tp, strike = "IDF", "", ""
        else:
            instr_type, optn_tp, strike = "IDO", kind, str(row["strike"])
        trade_date: date = row["trade_date"]
        expiry: date = row["expiry"]
        out.append(
            ",".join(
                (
                    trade_date.isoformat(),
                    trade_date.isoformat(),
                    "FO",
                    "NSE",
                    instr_type,
                    "12345",
                    "",
                    row["symbol"],
                    "",
                    expiry.isoformat(),
                    expiry.isoformat(),
                    strike,
                    optn_tp,
                    f"{row['symbol']}{expiry:%d%b%y}{strike}{optn_tp}",
                    str(row["open"]),
                    str(row["high"]),
                    str(row["low"]),
                    str(row["close"]),
                    str(row["close"]),
                    "0",
                    "0",
                    str(row.get("settle", row["close"])),
                    "5000",
                    "10",
                    "100",
                    "1.23",
                    "42",
                    "F1",
                    "75",
                    "",
                    "",
                    "",
                    "",
                    "",
                )
            )
            + "\r\n"
        )
    return "".join(out).encode("utf-8")


def _zip_bytes(member_name: str, csv_bytes: bytes) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(member_name, csv_bytes)
    return buffer.getvalue()


def _row(
    kind: str,
    expiry: date,
    trade_date: date,
    *,
    symbol: str = "NIFTY",
    strike: str = "0",
    open_: str = "0",
    high: str = "0",
    low: str = "0",
    close: str = "0",
    settle: str | None = None,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "symbol": symbol,
        "expiry": expiry,
        "trade_date": trade_date,
        "strike": strike,
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "settle": settle if settle is not None else close,
    }


def _chain_rows(expiry: date, trade_date: date, fut_expiry: date) -> list[dict[str, Any]]:
    """A quiet expiry-day chain around future-open 22000.

    Hand-derivations used by the golden tests:
      * V1 ATM strike nearest 22000 present on both sides -> 22000.
      * V2 CE target is 22000*1.01 = 22220 -> nearest CE strike 22200
        (diff 20 beats 22000's 220 and 22400's 180); PE target 21780 ->
        21800 (diff 20).
      * V3 CE target 22440 -> 22400 (diff 40); PE target 21560 -> 21600.
      * Quiet day: every high sits below its own 20% trigger, so NO SL
        triggers at any grid level and both brackets equal hold-to-close.
        V1 no-SL P&L = (100-50) + (120-70) = 100 points.
    """
    mk = lambda **kw: _row(expiry=expiry, trade_date=trade_date, **kw)  # noqa: E731
    return [
        _row(
            "FUT", fut_expiry, trade_date, open_="22000", high="22100", low="21700", close="21800"
        ),
        mk(kind="CE", strike="22000", open_="100", high="110", low="40", close="50"),
        mk(kind="PE", strike="22000", open_="120", high="130", low="60", close="70"),
        mk(kind="CE", strike="22200", open_="80", high="88", low="30", close="40"),
        mk(kind="PE", strike="21800", open_="90", high="99", low="35", close="45"),
        mk(kind="CE", strike="22400", open_="60", high="66", low="20", close="30"),
        mk(kind="PE", strike="21600", open_="70", high="77", low="25", close="35"),
        # A non-NIFTY symbol: must parse but be filtered out at extraction.
        mk(
            kind="CE",
            symbol="BANKNIFTY",
            strike="48000",
            open_="200",
            high="210",
            low="150",
            close="160",
        ),
    ]


def _dict_fetch(mapping: dict[str, bytes]) -> tuple[hs.Fetcher, list[str]]:
    """A dict-backed fetcher: absent URL == 404.  Records every call."""
    calls: list[str] = []

    def fetch(url: str) -> bytes:
        calls.append(url)
        if url not in mapping:
            raise hs.FetchNotFound(url)
        return mapping[url]

    return fetch, calls


def _boom_fetch(url: str) -> bytes:
    raise AssertionError(f"network layer touched offline: {url}")


# ---------------------------------------------------------------------------
# Parsers — both format eras
# ---------------------------------------------------------------------------


def test_parse_old_format_futures_options_and_dates() -> None:
    trade_date = date(2019, 2, 7)
    rows = _chain_rows(date(2019, 2, 7), trade_date, date(2019, 2, 28))
    parsed = hs.parse_old_format(_old_csv(rows))
    assert len(parsed.futures) == 1
    assert parsed.futures[0].symbol == "NIFTY"
    assert parsed.futures[0].expiry == date(2019, 2, 28)  # DD-Mmm-YYYY parsed
    assert parsed.futures[0].open == Decimal("22000")
    # 6 NIFTY option rows + 1 BANKNIFTY row all parse (filtering is later).
    assert len(parsed.options) == 7
    assert {o.symbol for o in parsed.options} == {"NIFTY", "BANKNIFTY"}
    ce_atm = next(
        o for o in parsed.options if o.strike == Decimal("22000") and o.option_type == "CE"
    )
    assert (ce_atm.open, ce_atm.high, ce_atm.low, ce_atm.close) == (
        Decimal("100"),
        Decimal("110"),
        Decimal("40"),
        Decimal("50"),
    )
    assert parsed.dropped_rows == 0


def test_parse_new_format_futures_options_and_dates() -> None:
    trade_date = date(2025, 3, 6)
    rows = _chain_rows(date(2025, 3, 6), trade_date, date(2025, 3, 27))
    parsed = hs.parse_new_format(_new_csv(rows))
    assert len(parsed.futures) == 1
    assert parsed.futures[0].expiry == date(2025, 3, 27)  # YYYY-MM-DD parsed
    assert len(parsed.options) == 7
    pe_atm = next(
        o for o in parsed.options if o.strike == Decimal("22000") and o.option_type == "PE"
    )
    assert pe_atm.settle == Decimal("70")


def test_sanity_screens_drop_and_count_corrupt_rows() -> None:
    """Golden (e): open<=0 and high<low rows are DROPPED and COUNTED."""
    trade_date = date(2025, 3, 6)
    rows = _chain_rows(date(2025, 3, 6), trade_date, date(2025, 3, 27))
    rows.append(
        _row(
            "CE",
            date(2025, 3, 6),
            trade_date,
            strike="23000",
            open_="0",
            high="5",
            low="1",
            close="2",
        )
    )
    rows.append(
        _row(
            "CE",
            date(2025, 3, 6),
            trade_date,
            strike="23200",
            open_="10",
            high="4",
            low="8",
            close="6",
        )
    )
    parsed = hs.parse_new_format(_new_csv(rows))
    assert parsed.dropped_rows == 2
    strikes = {o.strike for o in parsed.options}
    assert Decimal("23000") not in strikes
    assert Decimal("23200") not in strikes


def test_option_row_unknown_side_dropped_and_counted_old_format() -> None:
    """NIT-1: an OPT row whose OPTION_TYP is not CE/PE is counted in
    ``dropped_rows`` (the builder maps kind "XX" -> OPTIDX + OPTION_TYP XX)."""
    trade_date = date(2019, 2, 7)
    rows = _chain_rows(date(2019, 2, 7), trade_date, date(2019, 2, 28))
    rows.append(
        _row(
            "XX",
            date(2019, 2, 7),
            trade_date,
            strike="22000",
            open_="10",
            high="12",
            low="8",
            close="9",
        )
    )
    parsed = hs.parse_old_format(_old_csv(rows))
    assert parsed.dropped_rows == 1
    assert len(parsed.options) == 7  # the XX row joined neither side
    assert len(parsed.futures) == 1


def test_option_row_unknown_side_dropped_and_counted_new_format() -> None:
    """NIT-1 twin for the UDiFF format (IDO row with OptnTp XX)."""
    trade_date = date(2025, 3, 6)
    rows = _chain_rows(date(2025, 3, 6), trade_date, date(2025, 3, 27))
    rows.append(
        _row(
            "XX",
            date(2025, 3, 6),
            trade_date,
            strike="22000",
            open_="10",
            high="12",
            low="8",
            close="9",
        )
    )
    parsed = hs.parse_new_format(_new_csv(rows))
    assert parsed.dropped_rows == 1
    assert len(parsed.options) == 7
    assert len(parsed.futures) == 1


# ---------------------------------------------------------------------------
# Leg simulation goldens — the fill brackets
# ---------------------------------------------------------------------------


def _opt(
    strike: str, open_: str, high: str, low: str, close: str, settle: str | None = None
) -> hs.OptionRow:
    return hs.OptionRow(
        symbol="NIFTY",
        expiry=date(2025, 3, 6),
        strike=Decimal(strike),
        option_type="CE",
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        settle=Decimal(settle if settle is not None else close),
    )


def test_golden_quiet_day_brackets_equal() -> None:
    """Golden (a): no trigger -> both brackets equal hold-to-close decay.

    CE entry 100, high 110; the tightest trigger is 100*1.20 = 120 > 110,
    so no SL in the grid triggers.  Exit is the close 50 -> +50 points on
    every SL level and on no-SL, identically for both brackets.
    """
    row = _opt("22000", "100", "110", "40", "50")
    hold = hs.simulate_leg(row, None, "hold_to_close")
    assert hold.pnl_points == Decimal("50")
    for sl in hs.SL_GRID:
        opt = hs.simulate_leg(row, sl, "optimistic")
        pess = hs.simulate_leg(row, sl, "pessimistic")
        assert not opt.triggered
        assert not pess.triggered
        assert opt.pnl_points == pess.pnl_points == Decimal("50")


def test_golden_sl_hit_day_optimistic_vs_pessimistic() -> None:
    """Golden (b): the 30% trigger pierced -> trigger fill vs high fill.

    CE entry 100 -> trigger is 100*1.30 = 130; day high 150 >= 130, so
    the leg stops: optimistic exits AT 130 (P&L 100-130 = -30 points),
    pessimistic at the HIGH 150 (P&L 100-150 = -50 points).
    PE entry 80 -> trigger 104; high 90 < 104, so it holds to close 60
    (P&L 80-60 = +20 points) under BOTH brackets.
    Variant totals: optimistic -30+20 = -10 pts; pessimistic -50+20 = -30
    pts.  On 2025-03-06 the NIFTY lot is 75 (repo table: 75 from
    2024-11-20), so per lot: -10*75 = -750 and -30*75 = -2250 rupees.
    """
    sl30 = Decimal("0.30")
    ce = _opt("22000", "100", "150", "80", "120")
    pe = hs.OptionRow(
        symbol="NIFTY",
        expiry=date(2025, 3, 6),
        strike=Decimal("22000"),
        option_type="PE",
        open=Decimal("80"),
        high=Decimal("90"),
        low=Decimal("40"),
        close=Decimal("60"),
        settle=Decimal("60"),
    )
    ce_opt = hs.simulate_leg(ce, sl30, "optimistic")
    ce_pess = hs.simulate_leg(ce, sl30, "pessimistic")
    pe_opt = hs.simulate_leg(pe, sl30, "optimistic")
    pe_pess = hs.simulate_leg(pe, sl30, "pessimistic")
    assert ce_opt.triggered and ce_pess.triggered
    assert ce_opt.exit == Decimal("130")
    assert ce_pess.exit == Decimal("150")
    assert ce_opt.pnl_points == Decimal("-30")
    assert ce_pess.pnl_points == Decimal("-50")
    assert not pe_opt.triggered
    assert pe_opt.pnl_points == pe_pess.pnl_points == Decimal("20")
    lot = hs.nifty_lot_size(date(2025, 3, 6))
    assert lot == 75
    assert (ce_opt.pnl_points + pe_opt.pnl_points) * lot == Decimal("-750")
    assert (ce_pess.pnl_points + pe_pess.pnl_points) * lot == Decimal("-2250")


def test_golden_gap_spike_day_naive_vs_honest_multiple() -> None:
    """Golden (c): high = 5x entry -> the naive-vs-honest gap.

    CE entry 100, high 500.  SL 30% trigger is 130: optimistic loss is
    100-130 = -30 points; pessimistic buys back at the high 500 for
    100-500 = -400 points — 13.33x the naive loss, comfortably beyond
    the 5x gap threshold the summary counts.
    """
    row = _opt("22000", "100", "500", "95", "480")
    opt = hs.simulate_leg(row, Decimal("0.30"), "optimistic")
    pess = hs.simulate_leg(row, Decimal("0.30"), "pessimistic")
    assert opt.pnl_points == Decimal("-30")
    assert pess.pnl_points == Decimal("-400")
    assert pess.pnl_points <= 5 * opt.pnl_points


def test_trigger_boundary_is_inclusive() -> None:
    """A print exactly AT the trigger fills the stop (high >= trigger)."""
    row = _opt("22000", "100", "120", "90", "110")
    leg = hs.simulate_leg(row, Decimal("0.20"), "optimistic")
    assert leg.triggered
    assert leg.exit == Decimal("120")


def test_exit_uses_settle_when_close_missing() -> None:
    row = _opt("22000", "100", "110", "40", "0", settle="45")
    leg = hs.simulate_leg(row, None, "hold_to_close")
    assert leg.exit == Decimal("45")
    assert leg.pnl_points == Decimal("55")


# ---------------------------------------------------------------------------
# Strike selection / chain extraction
# ---------------------------------------------------------------------------


def test_nearest_strike_tie_prefers_lower() -> None:
    strikes = [Decimal("21900"), Decimal("22100")]
    assert hs.nearest_strike(strikes, Decimal("22000")) == Decimal("21900")


def test_select_legs_all_variants() -> None:
    trade_date = date(2025, 3, 6)
    parsed = hs.parse_new_format(
        _new_csv(_chain_rows(date(2025, 3, 6), trade_date, date(2025, 3, 27)))
    )
    chain = hs.extract_chain(parsed, date(2025, 3, 6))
    assert chain is not None
    assert chain.fut_open == Decimal("22000")
    # BANKNIFTY filtered out at extraction.
    assert all(o.symbol == "NIFTY" for o in chain.ce_by_strike.values())
    v1 = hs.select_legs(chain, hs.VARIANTS[0])
    v2 = hs.select_legs(chain, hs.VARIANTS[1])
    v3 = hs.select_legs(chain, hs.VARIANTS[2])
    assert v1 is not None and v2 is not None and v3 is not None
    assert (v1[0].strike, v1[1].strike) == (Decimal("22000"), Decimal("22000"))
    assert (v2[0].strike, v2[1].strike) == (Decimal("22200"), Decimal("21800"))
    assert (v3[0].strike, v3[1].strike) == (Decimal("22400"), Decimal("21600"))


def test_extract_chain_without_future_returns_none() -> None:
    trade_date = date(2025, 3, 6)
    rows = [
        _row(
            "CE",
            date(2025, 3, 6),
            trade_date,
            strike="22000",
            open_="100",
            high="110",
            low="40",
            close="50",
        )
    ]
    parsed = hs.parse_new_format(_new_csv(rows))
    assert hs.extract_chain(parsed, date(2025, 3, 6)) is None


# ---------------------------------------------------------------------------
# Lot-size table + costs
# ---------------------------------------------------------------------------


def test_nifty_lot_size_boundaries() -> None:
    assert hs.nifty_lot_size(date(2019, 2, 1)) == 75
    assert hs.nifty_lot_size(date(2021, 7, 28)) == 75
    assert hs.nifty_lot_size(date(2021, 7, 29)) == 50
    assert hs.nifty_lot_size(date(2024, 4, 26)) == 25
    assert hs.nifty_lot_size(date(2024, 11, 19)) == 25
    assert hs.nifty_lot_size(date(2024, 11, 20)) == 75
    assert hs.nifty_lot_size(date(2026, 1, 1)) == 65
    with pytest.raises(ValueError, match="precedes recorded lot history"):
        hs.nifty_lot_size(date(2018, 12, 31))


def test_golden_costs_refuse_2023_and_price_2025() -> None:
    """Golden (f): pre-2024-10-01 refused; a 2025 trade priced exactly.

    2025-03-06, lot 75, CE entry 100 exit 50, PE entry 120 exit 70:
      brokerage  = 4 orders x Rs20            =  80
      STT (0.10% of sell-side premium)        = (100+120)*75*0.0010 = 16.50
      NSE ETC (0.03503% of both-sides prem.)  = (100+50+120+70)*75*0.0003503
                                              = 25500*0.0003503 = 8.93265
      total = 105.43265 -> 105.43 (paisa, ROUND_HALF_UP)
    """
    refused = hs.variant_costs_rupees(
        date(2023, 6, 1), (Decimal("100"), Decimal("120")), (Decimal("50"), Decimal("70")), 75
    )
    assert refused is None
    priced = hs.variant_costs_rupees(
        date(2025, 3, 6), (Decimal("100"), Decimal("120")), (Decimal("50"), Decimal("70")), 75
    )
    assert priced == Decimal("105.43")


def test_golden_costs_2026_stt_regime() -> None:
    """From 2026-04-01 the STT sell rate is 0.15% (constants.py:77):
    STT = 220*75*0.0015 = 24.75; ETC 8.93265; brokerage 80 ->
    113.68265 -> 113.68."""
    priced = hs.variant_costs_rupees(
        date(2026, 4, 2), (Decimal("100"), Decimal("120")), (Decimal("50"), Decimal("70")), 75
    )
    assert priced == Decimal("113.68")


# ---------------------------------------------------------------------------
# Expiry-day regime + discovery (holiday walk-back)
# ---------------------------------------------------------------------------


def test_weekly_candidate_regime_boundary() -> None:
    # Week of Mon 2025-08-25: Thursday 2025-08-28 precedes the boundary.
    assert hs.weekly_expiry_candidate(date(2025, 8, 25)) == date(2025, 8, 28)
    # Week of Mon 2025-09-01: Tuesday regime -> 2025-09-02.
    assert hs.weekly_expiry_candidate(date(2025, 9, 1)) == date(2025, 9, 2)
    # Deep Thursday era.
    assert hs.weekly_expiry_candidate(date(2019, 2, 4)) == date(2019, 2, 7)


def _new_url(d: date) -> str:
    return hs.bhavcopy_url(d, "new")


def _old_url(d: date) -> str:
    return hs.bhavcopy_url(d, "old")


def test_golden_holiday_walkback_thursday_404_wednesday_found(tmp_path: Path) -> None:
    """Golden (d): Thursday 404s (holiday) -> discovery lands on Wednesday."""
    wednesday = date(2025, 3, 12)
    chain_zip = _zip_bytes(
        "BhavCopy_NSE_FO_0_0_0_20250312_F_0000.csv",
        _new_csv(_chain_rows(wednesday, wednesday, date(2025, 3, 27))),
    )
    fetch, calls = _dict_fetch({_new_url(wednesday): chain_zip})
    loader = hs.DayLoader(cache_dir=tmp_path, fetch=fetch)
    discovery = hs.discover_expiry_day(date(2025, 3, 10), loader)
    assert discovery.expiry_date == wednesday
    assert discovery.fmt == "new"
    # Thursday probed first (both formats 404), then Wednesday found.
    assert calls[0] == _new_url(date(2025, 3, 13))


def test_walkback_when_candidate_file_lacks_expiring_options(tmp_path: Path) -> None:
    """Golden (d) variant: Thursday file EXISTS but its NIFTY options
    expire Wednesday -> walk back one weekday and find them there."""
    thursday = date(2025, 3, 13)
    wednesday = date(2025, 3, 12)
    thursday_zip = _zip_bytes(
        "x.csv", _new_csv(_chain_rows(wednesday, thursday, date(2025, 3, 27)))
    )
    wednesday_zip = _zip_bytes(
        "x.csv", _new_csv(_chain_rows(wednesday, wednesday, date(2025, 3, 27)))
    )
    fetch, _calls = _dict_fetch(
        {_new_url(thursday): thursday_zip, _new_url(wednesday): wednesday_zip}
    )
    loader = hs.DayLoader(cache_dir=tmp_path, fetch=fetch)
    discovery = hs.discover_expiry_day(date(2025, 3, 10), loader)
    assert discovery.expiry_date == wednesday


def test_no_expiry_found_counted_not_fabricated(tmp_path: Path) -> None:
    fetch, calls = _dict_fetch({})
    loader = hs.DayLoader(cache_dir=tmp_path, fetch=fetch)
    discovery = hs.discover_expiry_day(date(2025, 3, 10), loader)
    assert discovery.expiry_date is None
    # Candidate + 3 walk-back weekdays + 2 forward-probe weekdays,
    # 2 format probes each: 6 days x 2 = 12 calls.
    assert len(calls) == 12


def test_forward_probe_recovers_too_early_regime_constant(tmp_path: Path) -> None:
    """MINOR-1 (ii): a slightly-EARLY regime boundary constant is recovered
    by the forward probe.

    Hand-derivation: week of Mon 2025-09-01 is Tuesday-regime, so the
    candidate is Tue 2025-09-02 — but suppose the TRUE expiry is still
    Thu 2025-09-04 (the constant flipped a week early).  The backward
    walk from Tuesday (Mon 9-1, Fri 8-29, Thu 8-28) can never reach a
    LATER day; the forward probe (Wed 9-3, then Thu 9-4) finds the file
    whose NIFTY options expire Thu 2025-09-04.
    """
    thursday = date(2025, 9, 4)
    chain_zip = _zip_bytes("x.csv", _new_csv(_chain_rows(thursday, thursday, date(2025, 9, 30))))
    fetch, calls = _dict_fetch({_new_url(thursday): chain_zip})
    loader = hs.DayLoader(cache_dir=tmp_path, fetch=fetch)
    discovery = hs.discover_expiry_day(date(2025, 9, 1), loader)
    assert discovery.expiry_date == thursday
    # The candidate Tuesday was probed FIRST (backward leg precedence).
    assert calls[0] == _new_url(date(2025, 9, 2))
    # The winning forward probe is the LAST url fetched.
    assert calls[-1] == _new_url(thursday)


def test_duplicate_expiry_across_weeks_processed_once(tmp_path: Path) -> None:
    """MINOR-2: a boundary-week walk-back landing on the PRIOR week's
    expiry is deduped — processed once, counted once.

    Hand-derivation: week 1 (Mon 2025-08-25, Thursday regime) expires
    Thu 2025-08-28 and carries a chain.  Week 2 (Mon 2025-09-01,
    Tuesday regime) candidate Tue 2025-09-02 is absent; the backward
    walk probes Mon 9-1 (absent), Fri 8-29 (absent), then Thu 8-28 —
    the PRIOR week's expiry file, which of course holds NIFTY options
    expiring 8-28.  Without dedupe the same expiry day would be
    simulated twice.
    """
    thursday1 = date(2025, 8, 28)
    chain_zip = _zip_bytes("x.csv", _new_csv(_chain_rows(thursday1, thursday1, date(2025, 9, 30))))
    fetch, _calls = _dict_fetch({_new_url(thursday1): chain_zip})
    config = hs.StudyConfig(
        start=date(2025, 8, 25),
        end=date(2025, 9, 5),
        cache_dir=tmp_path / "cache",
        out_dir=tmp_path / "out",
    )
    output = hs.run_study(config, fetch)
    assert output.counters.weeks_scanned == 2
    assert output.counters.expiry_days_found == 1
    assert output.counters.duplicate_expiry_skipped == 1
    assert output.counters.no_expiry_found == 0
    # 1 day x 3 variants x (1 no-SL + 3 SL x 2 brackets) = 21 rows, once.
    assert len(output.results) == 21
    assert {r.expiry_date for r in output.results} == {thursday1}


# ---------------------------------------------------------------------------
# Downloader / cache
# ---------------------------------------------------------------------------


def test_format_preference_order_and_urls() -> None:
    assert hs.format_preference(date(2019, 2, 7)) == ("old", "new")
    assert hs.format_preference(date(2025, 3, 6)) == ("new", "old")
    assert (
        _old_url(date(2019, 2, 7))
        == "https://archives.nseindia.com/content/historical/DERIVATIVES/2019/FEB/fo07FEB2019bhav.csv.zip"
    )
    assert (
        _new_url(date(2025, 3, 6))
        == "https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_20250306_F_0000.csv.zip"
    )


def test_old_format_tried_first_before_cutover(tmp_path: Path) -> None:
    trade_date = date(2019, 2, 7)
    zip_bytes = _zip_bytes(
        "fo07FEB2019bhav.csv",
        _old_csv(_chain_rows(trade_date, trade_date, date(2019, 2, 28))),
    )
    fetch, calls = _dict_fetch({_old_url(trade_date): zip_bytes})
    loader = hs.DayLoader(cache_dir=tmp_path, fetch=fetch)
    day_file = loader.load(trade_date)
    assert day_file is not None
    assert day_file.fmt == "old"
    assert calls == [_old_url(trade_date)]


def test_cache_round_trip_never_refetches(tmp_path: Path) -> None:
    trade_date = date(2025, 3, 6)
    zip_bytes = _zip_bytes(
        "x.csv", _new_csv(_chain_rows(trade_date, trade_date, date(2025, 3, 27)))
    )
    fetch, calls = _dict_fetch({_new_url(trade_date): zip_bytes})
    loader = hs.DayLoader(cache_dir=tmp_path, fetch=fetch)
    assert loader.load(trade_date) is not None
    assert len(calls) == 1
    # Second load: served from cache — the boom fetcher proves no network.
    loader2 = hs.DayLoader(cache_dir=tmp_path, fetch=_boom_fetch)
    assert loader2.load(trade_date) is not None


def test_404_markers_cached_and_honoured(tmp_path: Path) -> None:
    trade_date = date(2025, 3, 13)
    fetch, calls = _dict_fetch({})
    loader = hs.DayLoader(cache_dir=tmp_path, fetch=fetch)
    assert loader.load(trade_date) is None
    assert len(calls) == 2  # both formats probed once
    marker = tmp_path / "fo_20250313_new.404"
    assert marker.is_file()
    loader2 = hs.DayLoader(cache_dir=tmp_path, fetch=_boom_fetch)
    assert loader2.load(trade_date) is None  # markers honoured, no fetch


def test_require_https_guard() -> None:
    """NIT-3: the https-only claim is ENFORCED — the pure helper the real
    fetcher applies to the request URL and the response's final URL
    (redirect-downgrade guard); the injected-fetcher seam is untouched."""
    url = "https://nsearchives.nseindia.com/content/fo/x.zip"
    assert hs.require_https(url, context="request URL") == url
    with pytest.raises(ValueError, match="non-https URL refused"):
        hs.require_https("http://nsearchives.nseindia.com/x.zip", context="response final URL")
    with pytest.raises(ValueError, match="non-https"):
        hs.require_https("file:///etc/passwd", context="request URL")
    # Both rendered archive templates satisfy the guard by construction.
    assert hs.require_https(_old_url(date(2019, 2, 7)), context="template")
    assert hs.require_https(_new_url(date(2025, 3, 6)), context="template")


def test_from_cache_only_never_touches_network(tmp_path: Path) -> None:
    loader = hs.DayLoader(cache_dir=tmp_path, fetch=_boom_fetch, from_cache_only=True)
    assert loader.load(date(2025, 3, 6)) is None
    loader_none = hs.DayLoader(cache_dir=tmp_path, fetch=None)
    assert loader_none.load(date(2025, 3, 6)) is None


def test_non_zip_payload_skipped_without_marker(tmp_path: Path) -> None:
    trade_date = date(2025, 3, 6)
    fetch, _calls = _dict_fetch({_new_url(trade_date): b"<html>bot wall</html>"})
    loader = hs.DayLoader(cache_dir=tmp_path, fetch=fetch)
    assert loader.load(trade_date) is None
    assert not (tmp_path / "fo_20250306_new.404").is_file()  # transient — retried next run


def test_corrupt_cached_zip_is_cache_miss_and_refetched(tmp_path: Path) -> None:
    """MINOR-3 (b): a corrupt cached zip == cache MISS -> re-download."""
    trade_date = date(2025, 3, 6)
    good_zip = _zip_bytes("x.csv", _new_csv(_chain_rows(trade_date, trade_date, date(2025, 3, 27))))
    # Plant a TRUNCATED zip at the cache path (valid magic, torn payload —
    # the pre-atomic-write crash shape).
    corrupt_path = tmp_path / "fo_20250306_new.zip"
    corrupt_path.write_bytes(good_zip[: len(good_zip) // 2])
    fetch, calls = _dict_fetch({_new_url(trade_date): good_zip})
    loader = hs.DayLoader(cache_dir=tmp_path, fetch=fetch)
    day_file = loader.load(trade_date)
    assert day_file is not None
    assert loader.corrupt_cache == 1
    assert calls == [_new_url(trade_date)]  # re-downloaded exactly once
    # The healed cache now serves offline (boom fetcher never fires).
    loader2 = hs.DayLoader(cache_dir=tmp_path, fetch=_boom_fetch)
    assert loader2.load(trade_date) is not None
    assert loader2.corrupt_cache == 0


def test_corrupt_cached_zip_absent_in_cache_only_mode(tmp_path: Path) -> None:
    """MINOR-3 (b): in from-cache-only mode a corrupt cache reads as
    ABSENT (counted) — never a crash, never a network touch."""
    (tmp_path / "fo_20250306_new.zip").write_bytes(b"PK\x03\x04garbage-not-a-zip")
    loader = hs.DayLoader(cache_dir=tmp_path, fetch=_boom_fetch, from_cache_only=True)
    assert loader.load(date(2025, 3, 6)) is None
    assert loader.corrupt_cache == 1


def test_non_404_fetch_failure_day_absent_no_marker(tmp_path: Path) -> None:
    """MINOR-3 (c), loader level: a persistent non-404 failure (403
    bot-wall shape) -> day absent, counted, NO .404 marker written."""

    def bot_wall(url: str) -> bytes:
        raise OSError(f"HTTP 403 bot wall (survived retries): {url}")

    loader = hs.DayLoader(cache_dir=tmp_path, fetch=bot_wall)
    assert loader.load(date(2025, 3, 6)) is None
    assert loader.fetch_failed == 2  # both format probes failed
    assert not (tmp_path / "fo_20250306_new.404").is_file()
    assert not (tmp_path / "fo_20250306_old.404").is_file()


def test_non_404_fetch_failure_run_continues(tmp_path: Path) -> None:
    """MINOR-3 (c), run level: a raising (non-404) fetcher never crashes
    ``run_study`` — the week counts ``no_expiry_found`` and the failures
    land in ``counters.fetch_failed``."""

    def bot_wall(url: str) -> bytes:
        raise OSError(f"HTTP 403 bot wall: {url}")

    config = hs.StudyConfig(
        start=date(2025, 3, 3),
        end=date(2025, 3, 7),
        cache_dir=tmp_path / "cache",
        out_dir=tmp_path / "out",
    )
    output = hs.run_study(config, bot_wall)
    assert output.counters.no_expiry_found == 1
    # 6 probed days (candidate + 3 back + 2 forward) x 2 formats.
    assert output.counters.fetch_failed == 12
    assert output.results == []


def test_atomic_write_bytes_replaces_and_leaves_no_temp(tmp_path: Path) -> None:
    """MINOR-3 (a): the cache write goes temp-file -> os.replace."""
    target = tmp_path / "fo_20250306_new.zip"
    target.write_bytes(b"old")
    hs.atomic_write_bytes(target, b"new-payload")
    assert target.read_bytes() == b"new-payload"
    assert [p.name for p in tmp_path.iterdir()] == [target.name]  # no stray temp files


# ---------------------------------------------------------------------------
# Stats helpers
# ---------------------------------------------------------------------------


def test_percentile_linear_interpolation() -> None:
    vals = [10.0, 20.0, 30.0, 40.0]
    assert hs.percentile(vals, 0) == 10.0
    assert hs.percentile(vals, 100) == 40.0
    assert hs.percentile(vals, 50) == 25.0  # rank 1.5 -> midpoint of 20 and 30
    assert hs.percentile([7.0], 99) == 7.0
    with pytest.raises(ValueError, match="empty"):
        hs.percentile([], 50)


def test_max_drawdown() -> None:
    # Cumulative: 100, 50, 120, -80 -> peak 120, trough -80 -> DD 200.
    assert hs.max_drawdown([100.0, -50.0, 70.0, -200.0]) == 200.0
    assert hs.max_drawdown([]) == 0.0
    assert hs.max_drawdown([10.0, 20.0]) == 0.0


# ---------------------------------------------------------------------------
# End-to-end study run (offline, dict-fetched, cached)
# ---------------------------------------------------------------------------


def _two_week_universe() -> dict[str, bytes]:
    """Week 1: quiet chain on Thursday 2025-03-06.  Week 2: Thursday
    2025-03-13 is a 404 holiday; Wednesday 2025-03-12 carries the chain."""
    thursday1 = date(2025, 3, 6)
    wednesday2 = date(2025, 3, 12)
    return {
        _new_url(thursday1): _zip_bytes(
            "a.csv", _new_csv(_chain_rows(thursday1, thursday1, date(2025, 3, 27)))
        ),
        _new_url(wednesday2): _zip_bytes(
            "b.csv", _new_csv(_chain_rows(wednesday2, wednesday2, date(2025, 3, 27)))
        ),
    }


def test_end_to_end_study_offline(tmp_path: Path) -> None:
    fetch, _calls = _dict_fetch(_two_week_universe())
    config = hs.StudyConfig(
        start=date(2025, 3, 3),
        end=date(2025, 3, 14),
        cache_dir=tmp_path / "cache",
        out_dir=tmp_path / "out",
    )
    output = hs.run_study(config, fetch)
    assert output.counters.weeks_scanned == 2
    assert output.counters.expiry_days_found == 2
    assert output.counters.no_expiry_found == 0
    # 2 days x 3 variants x (1 no-SL + 3 SL x 2 brackets) = 42 rows.
    assert len(output.results) == 42
    # The quiet chain: V1 no-SL P&L is 100 points -> Rs 7,500 at lot 75.
    v1_hold = [
        r
        for r in output.results
        if r.variant == "V1" and r.sl_pct is None and r.expiry_date == date(2025, 3, 6)
    ]
    assert len(v1_hold) == 1
    assert v1_hold[0].pnl_points == Decimal("100")
    assert v1_hold[0].pnl_rupees_per_lot == Decimal("7500")
    # Outputs render and carry the honesty footer.
    csv_path = tmp_path / "out" / "per_day_results.csv"
    hs.write_per_day_csv(output.results, csv_path)
    lines = csv_path.read_text(encoding="utf-8").strip().splitlines()
    assert lines[0].split(",")[:4] == ["expiry_date", "variant", "sl_pct", "bracket"]
    assert len(lines) == 1 + 42
    summary = hs.build_summary_md(output, config)
    assert "V1 — short ATM straddle" in summary
    assert "DEC-FILL-WORST-CASE-ONLY-001" in summary
    # Re-run fully offline from cache: the boom fetcher must never fire.
    offline = hs.StudyConfig(
        start=config.start,
        end=config.end,
        cache_dir=config.cache_dir,
        out_dir=config.out_dir,
        from_cache_only=True,
    )
    output2 = hs.run_study(offline, _boom_fetch)
    assert output2.counters.expiry_days_found == 2
    assert len(output2.results) == 42


def test_study_counts_no_expiry_weeks(tmp_path: Path) -> None:
    fetch, _calls = _dict_fetch({})
    config = hs.StudyConfig(
        start=date(2025, 3, 3),
        end=date(2025, 3, 7),
        cache_dir=tmp_path,
        out_dir=tmp_path,
    )
    output = hs.run_study(config, fetch)
    assert output.counters.no_expiry_found == 1
    assert output.no_expiry_weeks == [date(2025, 3, 3)]
    assert output.results == []


def test_study_costs_mode_marks_refused_and_priced(tmp_path: Path) -> None:
    """Golden (f) end-to-end: a 2019 day is cost_refused, a 2025 day priced."""
    thursday_2019 = date(2019, 2, 7)
    old_zip = _zip_bytes(
        "fo07FEB2019bhav.csv",
        _old_csv(_chain_rows(thursday_2019, thursday_2019, date(2019, 2, 28))),
    )
    mapping = _two_week_universe()
    mapping[_old_url(thursday_2019)] = old_zip
    fetch, _calls = _dict_fetch(mapping)
    config = hs.StudyConfig(
        start=date(2019, 2, 4),
        end=date(2019, 2, 8),
        cache_dir=tmp_path / "c1",
        out_dir=tmp_path / "o1",
        apply_costs=True,
    )
    output = hs.run_study(config, fetch)
    assert output.counters.expiry_days_found == 1
    assert output.counters.cost_refused_days == 1
    assert all(r.cost_refused and r.pnl_net_rupees is None for r in output.results)

    config25 = hs.StudyConfig(
        start=date(2025, 3, 3),
        end=date(2025, 3, 7),
        cache_dir=tmp_path / "c2",
        out_dir=tmp_path / "o2",
        apply_costs=True,
    )
    output25 = hs.run_study(config25, fetch)
    priced = [r for r in output25.results if r.variant == "V1" and r.sl_pct is None]
    assert len(priced) == 1
    # Hand-derived above: V1 costs on this chain = Rs 105.43; net = 7500 - 105.43.
    assert priced[0].cost_rupees == Decimal("105.43")
    assert priced[0].pnl_net_rupees == Decimal("7394.57")
    summary = hs.build_summary_md(output25, config25)
    assert "Net-of-costs totals" in summary
