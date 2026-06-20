"""Tests for ``brutex.dashboard.instrument_loader`` — keyless Groww load.

PR-J4b vendor cutover (DEC-DHAN-GROWW-REMIGRATE-001, ADR-0009 §5): the keyless
instrument-master load was repointed from the Dhan 31-column detailed scrip
master to the Groww 21-column instrument master. These tests inject a fake
``http_get`` returning a small valid **Groww instrument master** CSV against a
REAL temp :class:`~brutex.data.instrument_store.InstrumentStore` and assert the
loader parses/filters correctly, persists the LEAN universe (IDX + F&O-eligible
EQ + admitted REFERENCE_ONLY NTM constituents; legs dropped), re-summarises the
stored snapshot into ``index_count`` / ``stock_count``, aborts on a malformed
header, and handles an empty CSV.

NTM re-enablement (DEC-NTM-GROWW-REPOINT-001): the NIFTY-Total-Market
constituents are fetched from niftyindices (a fake ``constituent_opener`` here),
matched to Groww NSE cash-equity rows by ISIN / ticker, and the non-F&O matches
admitted as REFERENCE_ONLY. The fake openers keep every path offline; the live
niftyindices opener is the only ``# pragma: no cover`` surface.

NO Groww token / creds anywhere — the master is a public CSV (DEC-VENDOR-PIVOT-001).
CSV patterns follow ``tests/unit/test_groww_instruments.py``; the real store
usage follows ``tests/integration/test_universe_explorer_store.py``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest
from brutex.adapters.groww.constants import INSTRUMENT_CSV_COLUMNS
from brutex.dashboard.instrument_loader import (
    FNO_INDEX_ALIASES_OVERRIDE_ENV,
    load_instrument_master,
)
from brutex.dashboard.ntm_cache import load_cached_constituent_csv, save_constituent_csv
from brutex.data.instrument_store import InstrumentStore
from brutex.models import InstrumentFormatError
from brutex.universe import is_option_pullable
from structlog.testing import capture_logs

_HEADER = ",".join(INSTRUMENT_CSV_COLUMNS)


def _events(logs: Sequence[Mapping[str, object]], name: str) -> list[Mapping[str, object]]:
    """All captured events with ``event == name`` (in emission order)."""
    return [e for e in logs if e["event"] == name]


def _row(**over: str) -> str:
    """Build one 21-field Groww CSV row; every column defaults blank."""
    cells = dict.fromkeys(INSTRUMENT_CSV_COLUMNS, "")
    cells.update(over)
    return ",".join(cells[col] for col in INSTRUMENT_CSV_COLUMNS)


# Two NSE indices (IDX, lot/tick blank → 0), one F&O-eligible stock (WIPRO) made
# eligible by a WIPRO option leg, plus a WIPRO CE leg, and one out-of-scope MCX
# COMMODITY row that the Groww parser skips BEFORE the index. The lean universe
# persists the 2 indices + WIPRO (3); the option leg is dropped; MCX is filtered.
_NIFTY = _row(
    exchange="NSE",
    exchange_token="NIFTY",
    trading_symbol="NIFTY",
    groww_symbol="NSE-NIFTY",
    name="NIFTY 50",
    instrument_type="IDX",
    segment="CASH",
)
_BANKNIFTY = _row(
    exchange="NSE",
    exchange_token="BANKNIFTY",
    trading_symbol="BANKNIFTY",
    groww_symbol="NSE-BANKNIFTY",
    name="NIFTY Bank",
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
_WIPRO_CE = _row(
    exchange="NSE",
    exchange_token="50467",
    trading_symbol="WIPRO2661621800CE",
    groww_symbol="NSE-WIPRO-16Jun26-300-CE",
    instrument_type="CE",
    segment="FNO",
    underlying_symbol="WIPRO",
    underlying_exchange_token="3787",
    expiry_date="2026-06-16",
    strike_price="300",
    lot_size="1500",
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
# A non-F&O NSE cash equity (no derivative names it): dropped by the lean ingest
# UNLESS it is admitted as a REFERENCE_ONLY NTM constituent.
_SMALLCO = _row(
    exchange="NSE",
    exchange_token="9999",
    trading_symbol="SMALLCO",
    groww_symbol="NSE-SMALLCO",
    name="SmallCo Ltd",
    instrument_type="EQ",
    segment="CASH",
    series="EQ",
    isin="INE999A01010",
    lot_size="1",
    tick_size="0.05",
)

# India VIX: a reference-only NSE index (DEC-NIFTY-TOTAL-MARKET-REFTAG-001). It is
# tagged reference_only via the index-level ``is_reference_only`` flag, NOT via the
# NTM constituent ids — so it proves the custom ``reference_only_of`` predicate keeps
# the VIX tag while also admitting NTM constituents.
_VIX = _row(
    exchange="NSE",
    exchange_token="INDIAVIX",
    trading_symbol="INDIAVIX",
    groww_symbol="NSE-INDIAVIX",
    name="India VIX",
    instrument_type="IDX",
    segment="CASH",
)

# SENSEX: a BSE brute-force index — always individually tradable (option-pullable),
# never tagged reference_only, even though it has no NSE option leg
# (DEC-NON-OPTION-INDEX-REFTAG-001).
_SENSEX = _row(
    exchange="BSE",
    exchange_token="SENSEX",
    trading_symbol="SENSEX",
    groww_symbol="BSE-SENSEX",
    name="BSE SENSEX",
    instrument_type="IDX",
    segment="CASH",
)
# FINNIFTY: an NSE index with a listed option chain (the FINNIFTY CE leg names it),
# so it is options-eligible → NOT spot-only → keeps its OPT badge.
_FINNIFTY = _row(
    exchange="NSE",
    exchange_token="FINNIFTY",
    trading_symbol="FINNIFTY",
    groww_symbol="NSE-FINNIFTY",
    name="NIFTY FIN SERVICE",
    instrument_type="IDX",
    segment="CASH",
)
_FINNIFTY_CE = _row(
    exchange="NSE",
    exchange_token="60001",
    trading_symbol="FINNIFTY2661620000CE",
    groww_symbol="NSE-FINNIFTY-16Jun26-20000-CE",
    instrument_type="CE",
    segment="FNO",
    underlying_symbol="FINNIFTY",
    underlying_exchange_token="60002",
    expiry_date="2026-06-16",
    strike_price="20000",
    lot_size="65",
    tick_size="0.05",
)
# NIFTY IT: an NSE sectoral index with NO option leg → spot-only → tagged
# reference_only → NOT option-pullable (the bug this closes).
_NIFTYIT = _row(
    exchange="NSE",
    exchange_token="NIFTYIT",
    trading_symbol="NIFTYIT",
    groww_symbol="NSE-NIFTYIT",
    name="NIFTY IT",
    instrument_type="IDX",
    segment="CASH",
)

_ROWS = [_NIFTY, _BANKNIFTY, _WIPRO, _WIPRO_CE, _COMMODITY]

#: ``security_id``s the Groww parser assigns from the ``exchange_token`` column.
_WIPRO_ID = 3787
_SMALLCO_ID = 9999


def _csv(rows: list[str]) -> str:
    """Join *rows* under the canonical Groww header into a CSV text blob."""
    return "\n".join([_HEADER, *rows]) + "\n"


_CONSTITUENT_HEADER = "Company Name,Industry,Symbol,Series,ISIN Code"


def _constituents_csv(rows: list[tuple[str, str]]) -> str:
    """Build a niftyindices-shaped constituent CSV from ``(symbol, isin)`` rows."""
    lines = [_CONSTITUENT_HEADER]
    lines += [f"{sym} Ltd,Misc,{sym},EQ,{isin}" for sym, isin in rows]
    return "\n".join(lines) + "\n"


def _empty_opener(_url: str) -> str:
    """A constituent opener that returns nothing (live source has no CSV)."""
    return ""


def _boom_opener(_url: str) -> str:
    """A constituent opener that fails like a niftyindices timeout / bot-block."""
    raise OSError("niftyindices read timed out")


def _store(tmp_path: Path) -> InstrumentStore:
    """Open a fresh, empty real :class:`InstrumentStore` in *tmp_path*."""
    return InstrumentStore.open(tmp_path / "instruments.duckdb")


def test_loads_and_summarises_keyless(tmp_path: Path) -> None:
    """A valid CSV parses, filters MCX, and re-summarises into index/stock counts."""
    store = _store(tmp_path)
    result = load_instrument_master(
        store,
        http_get=lambda _url: _csv(_ROWS),
        run_id_factory=lambda: "test-load",
        constituent_opener=_empty_opener,  # no live NTM CSV → no admission
        constituent_cache_dir=tmp_path / "cache",
    )
    # NIFTY idx + BANKNIFTY idx + WIPRO eq + WIPRO CE = 4 in-scope rows parsed;
    # the MCX COMMODITY row is out-of-scope → skipped by the Groww parser.
    assert result["parsed"] == 4
    assert result["filtered"] == 0  # Groww parser skips out-of-scope before the index
    assert result["quarantined"] == 0  # Groww fails closed — no quarantine bucket
    # Lean ingest (DEC-INSTRUMENT-UNIVERSE-FAST-001): NIFTY + BANKNIFTY (IDX)
    # + WIPRO (F&O-eligible EQ) persist (3); the WIPRO CE option leg is dropped.
    assert result["persisted"] == 3
    assert result["legs_dropped"] == 1
    # Summary over the persisted universe underlyings: 2 indices, 1 stock.
    assert result["index_count"] == 2
    assert result["stock_count"] == 1
    assert result["total"] == 3
    # No live NTM constituent CSV → an honest no-admission note (not a crash).
    assert result["ntm"]["admitted"] is False
    assert "constituent CSV available" in result["ntm"]["reason"]


def test_default_run_id_factory(tmp_path: Path) -> None:
    """Omitting ``run_id_factory`` uses the built-in default and still loads."""
    store = _store(tmp_path)
    result = load_instrument_master(
        store,
        http_get=lambda _url: _csv(_ROWS),
        constituent_opener=_empty_opener,
        constituent_cache_dir=tmp_path / "cache",
    )
    assert result["parsed"] == 4
    assert result["total"] == 3


def test_malformed_header_raises(tmp_path: Path) -> None:
    """A CSV with a drifted Groww header aborts with InstrumentFormatError."""
    store = _store(tmp_path)
    bad_header = _HEADER.replace("tick_size,", "")  # drop a required column
    with pytest.raises(InstrumentFormatError, match="header drift"):
        load_instrument_master(store, http_get=lambda _url: bad_header + "\n")


def test_empty_csv_yields_zero_counts(tmp_path: Path) -> None:
    """An empty (header-only) CSV loads zero instruments → all counts 0.

    With no NSE cash equities to match, NTM enrichment is skipped WITHOUT any
    network call (no ``constituent_opener`` needed).
    """
    store = _store(tmp_path)
    result = load_instrument_master(store, http_get=lambda _url: _csv([]))
    ntm = result.pop("ntm")
    assert ntm["admitted"] is False
    assert "no NSE cash equities" in ntm["reason"]
    assert result == {
        "parsed": 0,
        "persisted": 0,
        "legs_dropped": 0,
        "filtered": 0,
        "quarantined": 0,
        "index_count": 0,
        "stock_count": 0,
        "total": 0,
    }


def test_non_fno_equity_dropped_from_lean_universe(tmp_path: Path) -> None:
    """A non-F&O equity that is NOT an NTM constituent is NOT persisted (lean ingest).

    Mirrors DEC-INSTRUMENT-UNIVERSE-FAST-001: only F&O-eligible equities join the
    indices; a cash equity with no option/future leg AND no NTM admission is parsed
    (so it is counted in ``parsed``) but dropped from the persisted universe
    (``legs_dropped``).
    """
    store = _store(tmp_path)
    result = load_instrument_master(
        store,
        http_get=lambda _url: _csv([*_ROWS, _SMALLCO]),
        constituent_opener=_empty_opener,  # SMALLCO is not admitted as a constituent
        constituent_cache_dir=tmp_path / "cache",
    )
    # 5 in-scope rows parsed (NIFTY, BANKNIFTY, WIPRO, WIPRO CE, SMALLCO).
    assert result["parsed"] == 5
    # WIPRO CE leg + the non-F&O SMALLCO equity are both dropped from the lean set.
    assert result["legs_dropped"] == 2
    assert result["persisted"] == 3
    # SMALLCO never reaches the explorer — still 2 indices + 1 stock (WIPRO).
    assert result["index_count"] == 2
    assert result["stock_count"] == 1


def test_fno_lot_freeze_folded_from_legs(tmp_path: Path) -> None:
    """An F&O underlying is stamped with its leg's tradable lot; a blank leg freeze
    leaves the row's own freeze intact (DEC-GROWW-LEG-ECONOMICS-001)."""
    store = _store(tmp_path)
    load_instrument_master(
        store,
        http_get=lambda _url: _csv(_ROWS),
        constituent_opener=_empty_opener,
        constituent_cache_dir=tmp_path / "cache",
    )
    wipro = store.get("WIPRO")
    assert wipro is not None
    # WIPRO's only leg (WIPRO_CE) carries lot 1500 → folded over the cash lot 1.
    assert wipro.lot_size == 1500
    # WIPRO_CE has no freeze → the equity keeps its own (blank → None) freeze.
    assert wipro.sm_freeze_qty is None
    # An index with no leg in this master keeps its own (blank → 0) lot.
    nifty = store.get("NIFTY")
    assert nifty is not None and nifty.lot_size == 0
    # The fold lifts the only F&O equity above placeholder lot 1 → no auto-heal loop.
    assert store.has_placeholder_stock_economics() is False


def test_ntm_admits_non_fno_constituents_as_reference(tmp_path: Path) -> None:
    """An NTM constituent that is non-F&O is admitted as a REFERENCE_ONLY stock.

    DEC-NTM-GROWW-REPOINT-001: SMALLCO (no derivative names it) is matched by ISIN
    and admitted — so the universe grows to include it as a REFERENCE_ONLY row,
    while WIPRO (also an NTM constituent but F&O-eligible) stays tradable
    (``reference_only=FALSE``, option-pullable). An unlisted constituent is reported
    in ``unmatched`` (never invented — Golden Rule #1).
    """
    store = _store(tmp_path)
    constituents = _constituents_csv(
        [
            ("WIPRO", "INE075A01022"),  # F&O-eligible → stays tradable, not reference
            ("SMALLCO", "INE999A01010"),  # non-F&O → admitted REFERENCE_ONLY
            ("UNLISTED", "INE000X01010"),  # not in the Groww master → unmatched
        ]
    )
    result = load_instrument_master(
        store,
        http_get=lambda _url: _csv([*_ROWS, _SMALLCO]),
        run_id_factory=lambda: "ntm",
        constituent_opener=lambda _url: constituents,
        constituent_cache_dir=tmp_path / "cache",
    )
    # NIFTY + BANKNIFTY (IDX) + WIPRO (F&O) + SMALLCO (REFERENCE_ONLY) persist (4);
    # only the WIPRO CE leg is dropped now that SMALLCO is admitted.
    assert result["persisted"] == 4
    assert result["legs_dropped"] == 1
    assert result["index_count"] == 2
    # Both WIPRO and the reference-only SMALLCO count toward the universe size.
    assert result["stock_count"] == 2
    assert result["total"] == 4
    ntm = result["ntm"]
    assert ntm["admitted"] is True
    assert ntm["constituents"] == 3
    assert ntm["matched"] == 2  # WIPRO + SMALLCO matched the master
    assert ntm["reference"] == 1  # only the non-F&O SMALLCO becomes a reference row
    assert ntm["unmatched"] == 1  # UNLISTED
    assert ntm["from_cache"] is False
    # The lake tags exactly SMALLCO reference_only; WIPRO keeps its option chain.
    refs = store.reference_only_security_ids()
    assert refs == frozenset({_SMALLCO_ID})
    assert _WIPRO_ID not in refs  # the F&O constituent is excluded from the reference set
    wipro = store.get("WIPRO")
    smallco = store.get("SMALLCO")
    assert wipro is not None and smallco is not None
    assert wipro.security_id == _WIPRO_ID  # parsed from the exchange_token column
    assert is_option_pullable(wipro, reference_only_ids=refs) is True
    assert is_option_pullable(smallco, reference_only_ids=refs) is False


def test_ntm_successful_live_fetch_persists_cache_for_next_outage(tmp_path: Path) -> None:
    """A SUCCESSFUL live niftyindices fetch WRITES the last-good cache to disk.

    The resilience cornerstone (DEC-NTM-CONSTITUENT-CACHE-001): cache-on-success is
    what lets the NEXT transient 404 fall back to ~750 constituents instead of
    cratering to the ~211 F&O base set. This pins that a clean fetch persists the
    exact CSV under ``ntm_constituents.csv`` so the subsequent outage finds it.
    """
    cache_dir = tmp_path / "cache"
    assert load_cached_constituent_csv(cache_dir=cache_dir) is None  # no cache before
    constituents = _constituents_csv([("WIPRO", "INE075A01022"), ("SMALLCO", "INE999A01010")])
    result = load_instrument_master(
        store := _store(tmp_path),
        http_get=lambda _url: _csv([*_ROWS, _SMALLCO]),
        constituent_opener=lambda _url: constituents,
        constituent_cache_dir=cache_dir,
    )
    assert result["ntm"]["from_cache"] is False  # served from the live fetch
    # The clean fetch persisted the cache verbatim — the next outage now has a fallback.
    assert load_cached_constituent_csv(cache_dir=cache_dir) == constituents
    assert store.reference_only_security_ids() == frozenset({_SMALLCO_ID})


def test_ntm_fno_constituent_stays_tradable(tmp_path: Path) -> None:
    """An NTM basket whose only match is F&O-eligible adds NO reference rows.

    WIPRO is an NTM constituent AND F&O-eligible, so it is matched but EXCLUDED from
    the reference set (it keeps its option chain). With no non-F&O match the load
    admits nothing new (``admitted`` False, ``reference`` 0) yet still reports the
    honest match count.
    """
    store = _store(tmp_path)
    constituents = _constituents_csv([("WIPRO", "INE075A01022")])
    result = load_instrument_master(
        store,
        http_get=lambda _url: _csv(_ROWS),
        constituent_opener=lambda _url: constituents,
        constituent_cache_dir=tmp_path / "cache",
    )
    assert result["persisted"] == 3  # 2 idx + WIPRO; nothing new admitted
    assert result["stock_count"] == 1
    ntm = result["ntm"]
    assert ntm["admitted"] is False
    assert ntm["matched"] == 1
    assert ntm["reference"] == 0
    assert ntm["unmatched"] == 0
    # WIPRO is never tagged reference_only (no India VIX in this fixture either).
    assert store.reference_only_security_ids() == frozenset()


def test_india_vix_and_ntm_constituent_both_reference_only(tmp_path: Path) -> None:
    """The custom ``reference_only_of`` keeps India VIX tagged AND admits constituents.

    Replacing the store's default ``is_reference_only`` predicate (to tag the NTM
    constituents) must NOT drop the India-VIX reference flag. This pins both arms of
    the predicate at once: India VIX via the index-level
    :func:`brutex.universe.is_reference_only` flag, SMALLCO via its admitted
    ``security_id``.
    """
    store = _store(tmp_path)
    constituents = _constituents_csv([("SMALLCO", "INE999A01010")])
    result = load_instrument_master(
        store,
        http_get=lambda _url: _csv([*_ROWS, _VIX, _SMALLCO]),
        constituent_opener=lambda _url: constituents,
        constituent_cache_dir=tmp_path / "cache",
    )
    # NIFTY + BANKNIFTY + INDIA VIX are indices; WIPRO + SMALLCO are stocks.
    assert result["index_count"] == 3
    assert result["stock_count"] == 2
    vix = store.get("INDIAVIX")
    assert vix is not None
    refs = store.reference_only_security_ids()
    assert refs == frozenset({vix.security_id, _SMALLCO_ID})
    # India VIX has no option chain; SMALLCO is a reference-only constituent.
    assert is_option_pullable(vix, reference_only_ids=refs) is False


def test_spot_only_indices_tagged_reference_options_indices_pullable(tmp_path: Path) -> None:
    """A non-option index is tagged reference_only (no OPT); option indices keep it.

    DEC-NON-OPTION-INDEX-REFTAG-001 — the bug fix: every non-F&O index used to show
    "OPT" + survive the dashboard "F&O only" filter because only India VIX + the NTM
    stocks were tagged reference_only. Now a spot-only index (NIFTY IT — no option
    leg, not brute-force) is also tagged, while an options-bearing index (FINNIFTY —
    a CE leg names it) and the brute-force trio (NIFTY / BANKNIFTY / SENSEX) stay
    option-pullable.
    """
    store = _store(tmp_path)
    rows = [_NIFTY, _BANKNIFTY, _SENSEX, _FINNIFTY, _FINNIFTY_CE, _NIFTYIT, _WIPRO, _WIPRO_CE]
    load_instrument_master(
        store,
        http_get=lambda _url: _csv(rows),
        constituent_opener=_empty_opener,  # no NTM admission — isolate the index rule
        constituent_cache_dir=tmp_path / "cache",
    )
    refs = store.reference_only_security_ids()
    nifty = store.get("NIFTY")
    banknifty = store.get("BANKNIFTY")
    sensex = store.get("SENSEX")
    finnifty = store.get("FINNIFTY")
    nifty_it = store.get("NIFTYIT")
    assert None not in (nifty, banknifty, sensex, finnifty, nifty_it)
    assert nifty is not None and banknifty is not None and sensex is not None
    assert finnifty is not None and nifty_it is not None
    # The spot-only sectoral index is the ONLY reference row here.
    assert refs == frozenset({nifty_it.security_id})
    # NIFTY IT has no options → NOT pullable (no "OPT" badge, filtered by "F&O only").
    assert is_option_pullable(nifty_it, reference_only_ids=refs) is False
    # FINNIFTY has a listed option chain → pullable.
    assert is_option_pullable(finnifty, reference_only_ids=refs) is True
    # The 3 brute-force indices are always individually tradable → pullable
    # (SENSEX too, even though its options live on BSE, not the NSE-only set).
    assert is_option_pullable(nifty, reference_only_ids=refs) is True
    assert is_option_pullable(banknifty, reference_only_ids=refs) is True
    assert is_option_pullable(sensex, reference_only_ids=refs) is True


def test_ntm_uses_cache_when_live_empty(tmp_path: Path) -> None:
    """An empty live fetch falls back to the last-good cache (universe stays full)."""
    cache_dir = tmp_path / "cache"
    constituents = _constituents_csv([("SMALLCO", "INE999A01010")])
    assert save_constituent_csv(constituents, cache_dir=cache_dir) is True
    store = _store(tmp_path)
    result = load_instrument_master(
        store,
        http_get=lambda _url: _csv([*_ROWS, _SMALLCO]),
        constituent_opener=_empty_opener,  # live returns nothing → cache covers it
        constituent_cache_dir=cache_dir,
    )
    ntm = result["ntm"]
    assert ntm["admitted"] is True
    assert ntm["reference"] == 1
    assert ntm["from_cache"] is True
    assert "last-good cache" in ntm["constituent_note"]
    assert store.reference_only_security_ids() == frozenset({_SMALLCO_ID})


def test_ntm_uses_cache_when_live_raises(tmp_path: Path) -> None:
    """A niftyindices timeout falls back to the last-good cache, not the base set."""
    cache_dir = tmp_path / "cache"
    constituents = _constituents_csv([("SMALLCO", "INE999A01010")])
    save_constituent_csv(constituents, cache_dir=cache_dir)
    store = _store(tmp_path)
    result = load_instrument_master(
        store,
        http_get=lambda _url: _csv([*_ROWS, _SMALLCO]),
        constituent_opener=_boom_opener,  # live raises → cache covers it
        constituent_cache_dir=cache_dir,
    )
    ntm = result["ntm"]
    assert ntm["admitted"] is True
    assert ntm["reference"] == 1
    assert ntm["from_cache"] is True
    assert store.reference_only_security_ids() == frozenset({_SMALLCO_ID})


def test_ntm_degrades_when_unreachable_and_no_cache(tmp_path: Path) -> None:
    """No live source AND no cache → degrade HONESTLY to the base set (no crash)."""
    store = _store(tmp_path)
    result = load_instrument_master(
        store,
        http_get=lambda _url: _csv([*_ROWS, _SMALLCO]),
        constituent_opener=_boom_opener,  # raises; the cache dir is empty
        constituent_cache_dir=tmp_path / "cache",
    )
    # SMALLCO is NOT admitted — the universe falls back to the F&O-eligible base.
    assert result["persisted"] == 3
    assert result["stock_count"] == 1
    ntm = result["ntm"]
    assert ntm["admitted"] is False
    assert "unreachable" in ntm["reason"]
    assert store.reference_only_security_ids() == frozenset()


# ════════════════════════════════════════════════════════════════════════
# DEC-FNO-INDEX-ALIAS-001 (#5) — comprehensive OPT/REF classification (E2E)
# ════════════════════════════════════════════════════════════════════════
#
# The 2 genuine NSE F&O indices whose Groww spot trading_symbol differs from their
# option-leg underlying_symbol AND whose own groww_symbol carries the SPOT spelling
# (so the groww_symbol bridge cannot reach them — the real ``make run`` gap):
#   spot NIFTYJR  ↔ F&O NIFTYNXT50;  spot NIFTYMIDSELECT ↔ F&O MIDCPNIFTY.
# These mirror the live master schema; live behaviour is operator-verified.
_NIFTYJR = _row(
    exchange="NSE",
    exchange_token="NIFTYJR",  # STRING token → surrogate security_id (no token-join)
    trading_symbol="NIFTYJR",
    groww_symbol="NSE-NIFTYJR",  # spot spelling → groww_symbol bridge misses
    name="Nifty Next 50",
    instrument_type="IDX",
    segment="CASH",
)
_NIFTYJR_CE = _row(
    exchange="NSE",
    exchange_token="80001",
    trading_symbol="NIFTYNXT502661670000CE",
    groww_symbol="NSE-NIFTYNXT50-16Jun26-70000-CE",
    instrument_type="CE",
    segment="FNO",
    underlying_symbol="NIFTYNXT50",  # the F&O symbol the alias maps NIFTYJR to
    underlying_exchange_token="26013",
    expiry_date="2026-06-16",
    strike_price="70000",
    lot_size="25",
    tick_size="0.05",
)
_NIFTYMIDSELECT = _row(
    exchange="NSE",
    exchange_token="NIFTYMIDSELECT",  # STRING token → surrogate (no token-join)
    trading_symbol="NIFTYMIDSELECT",
    groww_symbol="NSE-NIFTYMIDSELECT",  # spot spelling → groww_symbol bridge misses
    name="Nifty Midcap Select",
    instrument_type="IDX",
    segment="CASH",
)
_NIFTYMIDSELECT_CE = _row(
    exchange="NSE",
    exchange_token="81001",
    trading_symbol="MIDCPNIFTY2661612000CE",
    groww_symbol="NSE-MIDCPNIFTY-16Jun26-12000-CE",
    instrument_type="CE",
    segment="FNO",
    underlying_symbol="MIDCPNIFTY",  # the F&O symbol the alias maps NIFTYMIDSELECT to
    underlying_exchange_token="26074",
    expiry_date="2026-06-16",
    strike_price="12000",
    lot_size="120",
    tick_size="0.05",
)
# A stock whose own trading_symbol (BAJAJ-AUTO, hyphenated) differs from its option
# leg's underlying_symbol (BAJAJAUTO) by spelling, but whose GENUINE numeric vendor
# token (16669) == the leg's underlying_exchange_token → token-joined → OPT.
_BAJAJ = _row(
    exchange="NSE",
    exchange_token="16669",  # numeric token → token-join is valid
    trading_symbol="BAJAJ-AUTO",
    groww_symbol="NSE-BAJAJ-AUTO",
    name="Bajaj Auto",
    instrument_type="EQ",
    segment="CASH",
    series="EQ",
    isin="INE917I01010",
    lot_size="1",
    tick_size="0.05",
)
_BAJAJ_CE = _row(
    exchange="NSE",
    exchange_token="82001",
    trading_symbol="BAJAJAUTO2661690000CE",
    groww_symbol="NSE-BAJAJAUTO-16Jun26-9000-CE",
    instrument_type="CE",
    segment="FNO",
    underlying_symbol="BAJAJAUTO",  # spelling differs from the spot symbol_name
    underlying_exchange_token="16669",  # == the spot's numeric security_id
    expiry_date="2026-06-16",
    strike_price="9000",
    lot_size="375",
    tick_size="0.05",
)


def test_alias_rescues_niftyjr_and_niftymidselect_to_opt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 2 NSE indices NIFTYJR / NIFTYMIDSELECT classify OPT via the sourced alias.

    DEC-FNO-INDEX-ALIAS-001 (5b): their spot trading_symbol ≠ the option-leg
    underlying_symbol and their groww_symbol carries the spot spelling, so the
    token-join (surrogate id) and the groww_symbol bridge BOTH miss them. The
    sourced alias map rescues them → NOT reference_only → option-pullable, and the
    residual ``fno_index_unmatched`` diagnostic now reports ZERO gaps.
    """
    monkeypatch.delenv(FNO_INDEX_ALIASES_OVERRIDE_ENV, raising=False)
    store = _store(tmp_path)
    rows = [_NIFTY, _BANKNIFTY, _NIFTYJR, _NIFTYJR_CE, _NIFTYMIDSELECT, _NIFTYMIDSELECT_CE]
    with capture_logs() as logs:
        load_instrument_master(
            store,
            http_get=lambda _url: _csv(rows),
            constituent_opener=_empty_opener,
            constituent_cache_dir=tmp_path / "cache",
        )
    refs = store.reference_only_security_ids()
    jr = store.get("NIFTYJR")
    mid = store.get("NIFTYMIDSELECT")
    assert jr is not None and mid is not None
    # Both NSE F&O indices keep their OPT badge (NOT reference_only).
    assert jr.security_id not in refs
    assert mid.security_id not in refs
    assert is_option_pullable(jr, reference_only_ids=refs) is True
    assert is_option_pullable(mid, reference_only_ids=refs) is True
    # The residual gap diagnostic is SILENT — both NSE indices are resolved.
    assert _events(logs, "universe.load.fno_index_unmatched") == []
    # The reconciliation audit attributes both to the alias mechanism.
    (audit,) = _events(logs, "universe.load.opt_reclassified")
    assert audit["alias"] == ["NSE:NIFTYJR", "NSE:NIFTYMIDSELECT"]
    assert audit["count"] == 2
    assert audit["still_unmatched"] == 0


def test_eq_token_join_surfaced_as_diagnostic_not_reclassified(tmp_path: Path) -> None:
    """A symbol-mismatched STOCK token-join is surfaced for audit but NOT reclassified.

    DEC-FNO-INDEX-EQ-TOKEN-JOIN-AUDIT-001 (the honesty split of 5a): BAJAJ-AUTO's spot
    trading_symbol differs from its option leg's underlying_symbol (BAJAJAUTO), but its
    GENUINE numeric vendor token equals the leg's underlying_exchange_token. The
    token-join does NOT change a STOCK's OPT/REF classification (is_spot_only_index is
    False for non-IDX; a stock's pullability is fno_eligible membership, a symbol-keyed
    gate that is the out-of-scope follow-up). So the audit must surface BAJAJ-AUTO in the
    SEPARATE token_join_diagnostic field — NOT the real token_join rescue bucket — and
    must NOT inflate ``count`` (no flag flipped).
    """
    store = _store(tmp_path)
    rows = [_NIFTY, _BANKNIFTY, _BAJAJ, _BAJAJ_CE]
    with capture_logs() as logs:
        load_instrument_master(
            store,
            http_get=lambda _url: _csv(rows),
            constituent_opener=_empty_opener,
            constituent_cache_dir=tmp_path / "cache",
        )
    (audit,) = _events(logs, "universe.load.opt_reclassified")
    # Surfaced for audit in the honest diagnostic bucket — never claimed as a rescue.
    assert audit["token_join_diagnostic"] == ["NSE:BAJAJ-AUTO"]
    assert audit["token_join"] == []
    assert audit["alias"] == []
    assert audit["symbol_bridge"] == []
    # No flag flipped, so the reclassified count stays 0 (honesty — Golden Rule #10).
    assert audit["count"] == 0


def test_env_override_adds_a_new_index_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An operator ``BRUTEX_FNO_INDEX_ALIASES`` entry rescues a NEW index (no code change).

    DEC-FNO-INDEX-ALIAS-001 (DYNAMIC): a future NSE index whose spot symbol differs
    from its F&O symbol is rescued purely by the env override merged on top of the
    sourced defaults.
    """
    monkeypatch.setenv(FNO_INDEX_ALIASES_OVERRIDE_ENV, '{"NEWSPOT": "NEWFNO"}')
    newspot = _row(
        exchange="NSE",
        exchange_token="NEWSPOT",
        trading_symbol="NEWSPOT",
        groww_symbol="NSE-NEWSPOT",
        name="New Index",
        instrument_type="IDX",
        segment="CASH",
    )
    newspot_ce = _row(
        exchange="NSE",
        exchange_token="83001",
        trading_symbol="NEWFNO2661610000CE",
        groww_symbol="NSE-NEWFNO-16Jun26-10000-CE",
        instrument_type="CE",
        segment="FNO",
        underlying_symbol="NEWFNO",
        underlying_exchange_token="90000",
        expiry_date="2026-06-16",
        strike_price="10000",
        lot_size="50",
        tick_size="0.05",
    )
    store = _store(tmp_path)
    rows = [_NIFTY, _BANKNIFTY, newspot, newspot_ce]
    load_instrument_master(
        store,
        http_get=lambda _url: _csv(rows),
        constituent_opener=_empty_opener,
        constituent_cache_dir=tmp_path / "cache",
    )
    new_idx = store.get("NEWSPOT")
    assert new_idx is not None
    refs = store.reference_only_security_ids()
    # The override rescued the new index → option-pullable (not reference_only).
    assert is_option_pullable(new_idx, reference_only_ids=refs) is True


def test_spot_only_index_stays_reference_and_audit_reports_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A genuine spot-only index (no option legs) stays REF; the audit emits 0 rescues.

    Preserves the guarantee that NIFTY IT (no option leg) is NOT rescued by any of
    the three mechanisms, and the reconciliation audit honestly reports ``count=0``
    when nothing is rescued (the steady-state signal).
    """
    monkeypatch.delenv(FNO_INDEX_ALIASES_OVERRIDE_ENV, raising=False)
    store = _store(tmp_path)
    rows = [_NIFTY, _BANKNIFTY, _NIFTYIT]
    with capture_logs() as logs:
        load_instrument_master(
            store,
            http_get=lambda _url: _csv(rows),
            constituent_opener=_empty_opener,
            constituent_cache_dir=tmp_path / "cache",
        )
    refs = store.reference_only_security_ids()
    nifty_it = store.get("NIFTYIT")
    assert nifty_it is not None
    # The spot-only sectoral index is reference_only (no OPT badge) — unchanged.
    assert is_option_pullable(nifty_it, reference_only_ids=refs) is False
    (audit,) = _events(logs, "universe.load.opt_reclassified")
    assert audit["count"] == 0
    assert audit["token_join"] == []
    assert audit["alias"] == []
    assert audit["symbol_bridge"] == []
    assert audit["token_join_diagnostic"] == []
