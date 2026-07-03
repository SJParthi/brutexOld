"""Unit tests for ``brutex.adapters.groww.instruments`` (DEC-GROWW-ADAPTER-LEAF-001 / PR-D)."""

from __future__ import annotations

import hashlib
from datetime import date
from decimal import Decimal

import pytest
from brutex.adapters.groww import instruments as gi
from brutex.adapters.groww.constants import INSTRUMENT_CSV_COLUMNS
from brutex.models import (
    Exchange,
    Instrument,
    InstrumentFormatError,
    InstrumentType,
    Segment,
)
from structlog.testing import capture_logs

_HEADER = ",".join(INSTRUMENT_CSV_COLUMNS)


def _row(**over: str) -> str:
    """Build one 21-field CSV row; every column defaults blank, override by name."""
    cells = dict.fromkeys(INSTRUMENT_CSV_COLUMNS, "")
    cells.update(over)
    return ",".join(cells[col] for col in INSTRUMENT_CSV_COLUMNS)


# Representative in-scope + out-of-scope rows.
_NIFTY = _row(
    exchange="NSE",
    exchange_token="NIFTY",
    trading_symbol="NIFTY",
    groww_symbol="NSE-NIFTY",
    name="NIFTY 50",
    instrument_type="IDX",
    segment="CASH",
)
_VIX = _row(
    exchange="NSE",
    exchange_token="INDIAVIX",
    trading_symbol="INDIAVIX",
    groww_symbol="NSE-INDIAVIX",
    name="India Vix",
    instrument_type="IDX",
    segment="CASH",
)
_WIPRO = _row(
    exchange="NSE",
    exchange_token="3787",
    trading_symbol="WIPRO",
    groww_symbol="NSE-WIPRO",
    name="Wipro",
    instrument_type="EQ",
    segment="CASH",
    series="EQ",
    isin="INE075A01022",
    lot_size="1",
    tick_size="0.05",
)
_FUT = _row(
    exchange="NSE",
    exchange_token="53001",
    trading_symbol="NIFTY25Aug26FUT",
    groww_symbol="NSE-NIFTY-25Aug26-FUT",
    instrument_type="FUT",
    segment="FNO",
    underlying_symbol="NIFTY",
    underlying_exchange_token="26000",
    expiry_date="2026-08-25",
    lot_size="65",
    tick_size="0.05",
    freeze_quantity="1800",
)
_CE = _row(
    exchange="NSE",
    exchange_token="50467",
    trading_symbol="NIFTY2661621800CE",
    groww_symbol="NSE-NIFTY-16Jun26-21800-CE",
    instrument_type="CE",
    segment="FNO",
    underlying_symbol="NIFTY",
    underlying_exchange_token="26000",
    expiry_date="2026-06-16",
    strike_price="21800",
    lot_size="65",
    tick_size="0.05",
)
_COMMODITY = _row(
    exchange="MCX",
    exchange_token="1",
    trading_symbol="GOLD",
    groww_symbol="MCX-GOLD",
    instrument_type="FUT",
    segment="COMMODITY",
    underlying_symbol="GOLD",
    expiry_date="2026-08-25",
    lot_size="100",
    tick_size="1",
)
_UNKNOWN = _row(
    exchange="NSE",
    exchange_token="999",
    trading_symbol="ETFX",
    groww_symbol="NSE-ETFX",
    instrument_type="ETF",
    segment="CASH",
    lot_size="1",
    tick_size="0.05",
)
_NIFTY_DUP = _row(
    exchange="NSE",
    exchange_token="NIFTY",
    trading_symbol="NIFTY",
    groww_symbol="NSE-NIFTY",
    name="DUP",
    instrument_type="IDX",
    segment="CASH",
)


def _csv(*rows: str) -> str:
    return "\n".join([_HEADER, *rows]) + "\n"


# ── parse_instrument_csv + build_index (integration) ────────────────────────
def test_full_csv_parses_in_scope_only_and_dedups() -> None:
    text = _csv(_NIFTY, _VIX, _WIPRO, _FUT, _CE, _COMMODITY, _UNKNOWN, _NIFTY_DUP)
    pairs = gi.parse_instrument_csv(text)
    # COMMODITY (MCX) + ETF excluded; the two NIFTY rows both parse (dedup is the index's job)
    assert len(pairs) == 6
    index = gi.build_index(pairs)
    assert len(index.universe) == 5  # NIFTY (dedup) + VIX + WIPRO + FUT + CE


def test_index_resolves_o1_by_both_keys() -> None:
    index = gi.load_index_from_csv(_csv(_NIFTY, _WIPRO))
    assert gi.resolve(index, "NSE-NIFTY").symbol_name == "NIFTY"
    assert gi.resolve_by_pair(index, Exchange.NSE, "WIPRO").display_name == "Wipro"


def test_index_resolve_miss_raises_keyerror() -> None:
    index = gi.load_index_from_csv(_csv(_NIFTY))
    with pytest.raises(KeyError):
        gi.resolve(index, "NSE-MISSING")
    with pytest.raises(KeyError):
        gi.resolve_by_pair(index, Exchange.BSE, "NIFTY")


def test_index_dedup_first_wins() -> None:
    index = gi.load_index_from_csv(_csv(_NIFTY, _NIFTY_DUP))
    assert len(index.universe) == 1
    assert gi.resolve(index, "NSE-NIFTY").display_name == "NIFTY 50"  # first wins, not "DUP"


# ── field mapping per instrument kind ───────────────────────────────────────
def test_index_row_maps_to_index_segment_zero_lot_surrogate_id() -> None:
    inst = gi.load_index_from_csv(_csv(_NIFTY)).by_groww_symbol["NSE-NIFTY"]
    assert inst.instrument is InstrumentType.IDX
    assert inst.segment is Segment.INDEX  # IDX → INDEX, not CASH
    assert inst.lot_size == 0 and inst.tick_size == Decimal(0)
    assert inst.is_index is True
    # string token → deterministic surrogate from the groww_symbol
    expected = int.from_bytes(hashlib.blake2b(b"NSE-NIFTY", digest_size=4).digest(), "big")
    assert inst.security_id == expected
    # but feed_token preserves the RAW CSV token "NIFTY" (the live-feed identity,
    # 07-feed.md:132) — NOT the surrogate; this is what the live manifest emits.
    assert inst.feed_token == "NIFTY"


def test_vix_present_and_parses() -> None:
    inst = gi.load_index_from_csv(_csv(_VIX)).by_groww_symbol["NSE-INDIAVIX"]
    assert inst.instrument is InstrumentType.IDX
    assert inst.display_name == "India Vix"


def test_equity_row_numeric_security_id() -> None:
    inst = gi.load_index_from_csv(_csv(_WIPRO)).by_groww_symbol["NSE-WIPRO"]
    assert inst.security_id == 3787  # numeric token used directly
    assert inst.feed_token == "3787"  # the raw CSV token (== str(security_id) here)
    assert inst.segment is Segment.CASH
    assert inst.lot_size == 1 and inst.tick_size == Decimal("0.05")
    assert inst.isin == "INE075A01022"


def test_future_row() -> None:
    inst = gi.load_index_from_csv(_csv(_FUT)).by_groww_symbol["NSE-NIFTY-25Aug26-FUT"]
    assert inst.is_derivative is True
    assert inst.underlying_symbol == "NIFTY"
    assert inst.sm_expiry_date == date(2026, 8, 25)
    assert inst.segment is Segment.FNO


def test_option_row() -> None:
    inst = gi.load_index_from_csv(_csv(_CE)).by_groww_symbol["NSE-NIFTY-16Jun26-21800-CE"]
    assert inst.is_option is True
    assert inst.strike_price == Decimal("21800")
    assert inst.option_type == "CE"


# ── failure modes ───────────────────────────────────────────────────────────
def test_header_drift_raises() -> None:
    bad = "exchange,wrong_col\nNSE,x\n"
    with pytest.raises(InstrumentFormatError, match="header drift"):
        gi.parse_instrument_csv(bad)


def test_missing_groww_symbol_raises() -> None:
    with pytest.raises(InstrumentFormatError, match="missing groww_symbol"):
        gi.parse_instrument_row(dict.fromkeys(INSTRUMENT_CSV_COLUMNS, ""))


def test_malformed_lot_raises_at_the_row_level() -> None:
    # The ROW-level contract still fail-louds (parse_instrument_csv catches this
    # per-row — see the resilience tests below — but parse_instrument_row, the
    # single-row API, must surface the malformed cell so callers can see why).
    bad = {
        **dict.fromkeys(INSTRUMENT_CSV_COLUMNS, ""),
        "exchange": "NSE",
        "exchange_token": "1",
        "trading_symbol": "X",
        "groww_symbol": "NSE-X",
        "instrument_type": "EQ",
        "segment": "CASH",
        "lot_size": "abc",
        "tick_size": "0.05",
    }
    with pytest.raises(InstrumentFormatError, match="unparseable lot_size"):
        gi.parse_instrument_row(bad)


# ── resilience: one junk/stale row never aborts the whole master ─────────────
# DEC-INSTRUMENT-SKIP-NO-GSYM-001. The dashboard "Load instruments" route turned
# a single un-parseable in-scope row into an HTTP 502 + an empty universe. The
# parser now isolates+skips ANY malformed/stale in-scope row (the two DATA-error
# families in gi._ROW_PARSE_ERRORS) while loading every valid row; the header
# schema contract still fail-louds (see test_header_drift_raises above).
def test_real_world_blank_groww_symbol_idx_is_skipped_not_fatal() -> None:
    # The EXACT observed vendor row: "BSE FOCUSED IT", BSE/IDX/CASH, token 93,
    # BLANK groww_symbol + BLANK trading_symbol — un-pullable (every Groww call
    # keys on the groww_symbol). It must be skipped, and every valid row loads.
    focused_it = _row(
        exchange="BSE",
        exchange_token="93",
        name="BSE FOCUSED IT",
        instrument_type="IDX",
        segment="CASH",
    )
    pairs = gi.parse_instrument_csv(_csv(_NIFTY, focused_it, _WIPRO))
    gsyms = {gsym for gsym, _ in pairs}
    assert gsyms == {"NSE-NIFTY", "NSE-WIPRO"}  # junk row dropped, valid rows kept


def test_stale_blank_lot_size_equity_is_skipped() -> None:
    # A stale EQ row whose lot_size cell is blank parses to lot_size=0, which the
    # Instrument invariant rejects with ValueError (NOT InstrumentFormatError) —
    # the second arm of _ROW_PARSE_ERRORS. It must be skipped, not abort the load.
    stale_eq = _row(
        exchange="NSE",
        exchange_token="404",
        trading_symbol="STALE",
        groww_symbol="NSE-STALE",
        instrument_type="EQ",
        segment="CASH",
        tick_size="0.05",
        # lot_size intentionally blank → 0 → ValueError("lot_size must be positive")
    )
    pairs = gi.parse_instrument_csv(_csv(_WIPRO, stale_eq))
    assert {gsym for gsym, _ in pairs} == {"NSE-WIPRO"}


def test_all_in_scope_rows_malformed_yields_empty_not_raise() -> None:
    # Out-of-box: a master where EVERY in-scope row is junk must NOT raise — it
    # returns an empty list (the dashboard then surfaces "0 instruments", a
    # recoverable state, never a 502). Header is still valid here.
    focused_it = _row(exchange="BSE", exchange_token="93", instrument_type="IDX", segment="CASH")
    assert gi.parse_instrument_csv(_csv(focused_it, _COMMODITY)) == []


# ── is_in_scope_row branches ────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("row", "in_scope"),
    [
        ({"exchange": "NSE", "segment": "CASH", "instrument_type": "IDX"}, True),
        ({"exchange": "MCX", "segment": "COMMODITY", "instrument_type": "FUT"}, False),
        ({"exchange": "NSE", "segment": "COMMODITY", "instrument_type": "FUT"}, False),
        ({"exchange": "NSE", "segment": "CASH", "instrument_type": "ETF"}, False),
    ],
)
def test_is_in_scope_row(row: dict[str, str], in_scope: bool) -> None:
    assert gi.is_in_scope_row(row) is in_scope


# ── helper branch coverage (paths unreachable via well-formed CSV) ──────────
def test_clean_handles_none_and_whitespace() -> None:
    assert gi._clean(None) is None
    assert gi._clean("   ") is None
    assert gi._clean("  x ") == "x"


def test_security_id_blank_token_uses_surrogate() -> None:
    expected = int.from_bytes(hashlib.blake2b(b"NSE-FOO", digest_size=4).digest(), "big")
    assert gi._security_id(None, gsym="NSE-FOO") == expected


def test_opt_int_and_int_or_zero_branches() -> None:
    assert gi._opt_int("", field="f", gsym="g") is None
    assert gi._opt_int("7", field="f", gsym="g") == 7
    assert gi._int_or_zero("", field="f", gsym="g") == 0
    with pytest.raises(InstrumentFormatError):
        gi._opt_int("x", field="f", gsym="g")


def test_opt_decimal_branches() -> None:
    assert gi._opt_decimal("", field="f", gsym="g") is None
    assert gi._opt_decimal("1.5", field="f", gsym="g") == Decimal("1.5")
    with pytest.raises(InstrumentFormatError, match="unparseable"):
        gi._opt_decimal("x", field="f", gsym="g")
    with pytest.raises(InstrumentFormatError, match="non-finite"):
        gi._opt_decimal("inf", field="f", gsym="g")


def test_opt_date_branches() -> None:
    assert gi._opt_date("", field="f", gsym="g") is None
    assert gi._opt_date("2026-08-25", field="f", gsym="g") == date(2026, 8, 25)
    with pytest.raises(InstrumentFormatError):
        gi._opt_date("nope", field="f", gsym="g")


def test_segment_for_unmappable_raises() -> None:
    with pytest.raises(InstrumentFormatError, match="unmappable segment"):
        gi._segment_for(InstrumentType.FUT, "WEIRD", gsym="g")


# ── index surrogate-collision detection ─────────────────────────────────────
# DEC-INDEX-SURROGATE-COLLISION-DETECT-001. Two DISTINCT-gsym indices whose
# blake2b-4byte surrogate security_ids collide both entered the index, then one
# was SILENTLY last-wins-overwritten at the store's (security_id, exch_id,
# segment) identity dedup — order-dependently, zero warning. build_index now
# keep-firsts the collision at the single COMMON chokepoint + WARNs naming both.
def _hand_instrument(
    *,
    security_id: int,
    symbol_name: str,
    instrument: InstrumentType = InstrumentType.IDX,
    segment: Segment = Segment.INDEX,
    lot_size: int = 0,
    tick_size: Decimal = Decimal(0),
) -> Instrument:
    """Hand-build a minimal valid Instrument with a FORGED security_id."""
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=security_id,
        symbol_name=symbol_name,
        display_name=None,
        instrument=instrument,
        segment=segment,
        series=None,
        isin=None,
        underlying_symbol=None,
        underlying_security_id=None,
        lot_size=lot_size,
        tick_size=tick_size,
        sm_freeze_qty=None,
        sm_expiry_date=None,
        strike_price=None,
    )


def test_index_surrogate_collision_keeps_first_and_warns_regression() -> None:
    # FAIL-BEFORE/PASS-AFTER: before the fix both colliding indices entered the
    # index (universe of 2, no warning) and the store later dropped one SILENTLY
    # (last-wins, order-dependent). Now: keep-first + ONE loud WARN naming both.
    forged = 0xDEADBEEF
    pairs = [
        ("NSE-IDXA", _hand_instrument(security_id=forged, symbol_name="IDXA")),
        ("NSE-IDXB", _hand_instrument(security_id=forged, symbol_name="IDXB")),
    ]
    with capture_logs() as logs:
        index = gi.build_index(pairs)
    collisions = [
        entry for entry in logs if entry["event"] == "instrument_master.index_security_id_collision"
    ]
    assert len(collisions) == 1
    assert collisions[0]["kept"] == "NSE-IDXA"
    assert collisions[0]["dropped"] == "NSE-IDXB"
    assert collisions[0]["security_id"] == forged
    assert collisions[0]["exchange"] == "NSE"
    assert collisions[0]["log_level"] == "warning"
    assert len(index.universe) == 1
    assert index.by_groww_symbol["NSE-IDXA"].symbol_name == "IDXA"  # first wins
    assert "NSE-IDXB" not in index.by_groww_symbol  # dropped, not silently masked


def test_non_index_identical_security_ids_both_kept_no_warning() -> None:
    # Pins the `segment is Segment.INDEX` False path: only index rows carry a
    # hashed surrogate, so two CASH equities sharing a forged security_id are
    # outside the detector's scope — both kept, ZERO collision warning.
    forged = 424242
    pairs = [
        (
            "NSE-EQA",
            _hand_instrument(
                security_id=forged,
                symbol_name="EQA",
                instrument=InstrumentType.EQ,
                segment=Segment.CASH,
                lot_size=1,
                tick_size=Decimal("0.05"),
            ),
        ),
        (
            "NSE-EQB",
            _hand_instrument(
                security_id=forged,
                symbol_name="EQB",
                instrument=InstrumentType.EQ,
                segment=Segment.CASH,
                lot_size=1,
                tick_size=Decimal("0.05"),
            ),
        ),
    ]
    with capture_logs() as logs:
        index = gi.build_index(pairs)
    assert not [
        entry for entry in logs if entry["event"] == "instrument_master.index_security_id_collision"
    ]
    assert len(index.universe) == 2
    assert "NSE-EQA" in index.by_groww_symbol and "NSE-EQB" in index.by_groww_symbol


def test_distinct_index_surrogates_no_false_positive() -> None:
    # Pins the owner-is-None assign path over REAL index rows: two distinct
    # indices (distinct surrogates) both load with no collision warning.
    with capture_logs() as logs:
        index = gi.load_index_from_csv(_csv(_NIFTY, _VIX))
    assert not [
        entry for entry in logs if entry["event"] == "instrument_master.index_security_id_collision"
    ]
    assert len(index.universe) == 2
    assert "NSE-NIFTY" in index.by_groww_symbol and "NSE-INDIAVIX" in index.by_groww_symbol


def test_engine_index_surrogates_are_mutually_distinct() -> None:
    # The three brute-force engine indices must produce three DISTINCT 32-bit
    # surrogates today (no latent collision in the locked engine universe).
    surrogates = {
        gi._security_id(None, gsym=gsym) for gsym in ("NSE-NIFTY", "NSE-BANKNIFTY", "BSE-SENSEX")
    }
    assert len(surrogates) == 3


# ── index_symbol_of (DEC-FNO-INDEX-GROWW-SYMBOL-MATCH-001) ──────────────────
@pytest.mark.parametrize(
    ("groww_symbol", "expected"),
    [
        ("NSE-NIFTY", "NIFTY"),
        ("NSE-MIDCPNIFTY", "MIDCPNIFTY"),
        ("NSE-NIFTYNXT50", "NIFTYNXT50"),
        ("BSE-SENSEX", "SENSEX"),
        ("NSE-INDIAVIX", "INDIAVIX"),
    ],
)
def test_index_symbol_of_extracts_the_fno_symbol(groww_symbol: str, expected: str) -> None:
    # The SYMBOL component (after the exchange prefix) is the vendor's F&O
    # symbol — the SAME string the option legs name as underlying_symbol.
    assert gi.index_symbol_of(groww_symbol) == expected


def test_index_symbol_of_without_dash_is_empty() -> None:
    # A malformed key with no "-" yields "" (matches nothing — safe, never raises).
    assert gi.index_symbol_of("NIFTY") == ""
