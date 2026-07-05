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

import threading

# Whitebox tests of the cache-first TTL helper (_ntm_cache_ttl_seconds) read
# module-private names by design (DEC-P0-017 convention).
# pyright: reportPrivateUsage=false
import json
from collections.abc import Callable, Iterator, Mapping, Sequence
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.adapters.groww.constants import INSTRUMENT_CSV_COLUMNS
from brutex.adapters.groww.instruments import build_index
from brutex.dashboard import instrument_loader as il
from brutex.dashboard.instrument_loader import (
    FNO_INDEX_ALIASES_OVERRIDE_ENV,
    _classify_option_pullable,
    _groww_symbol_index_bridge,
    load_instrument_master,
    resolve_fno_index_aliases,
)
from brutex.dashboard.ntm_cache import load_cached_constituent_csv, save_constituent_csv
from brutex.data.instrument_store import InstrumentStore
from brutex.models import Exchange, Instrument, InstrumentFormatError, InstrumentType, Segment
from brutex.universe import build_options_eligible_underlyings, is_option_pullable
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


def _must_not_fetch_opener(_url: str) -> str:
    """A constituent opener that FAILS LOUDLY if called.

    Pins the cache-first hot path (DEC-NTM-CACHE-FIRST-001): a fresh cache must be
    served with ZERO network calls, so this raising opener proves the live fetch was
    never invoked. ``AssertionError`` (not ``OSError``) so a regression that DID call
    the network surfaces as a hard test failure, never silently caught by a
    fail-soft handler.
    """
    raise AssertionError("live niftyindices fetch must not be called when the cache is fresh")


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
    # A clean master → ZERO write-boundary identity collisions on the report
    # (DEC-MAPPING-COLLISION-HONESTY-001).
    assert result["security_id_collisions"] == 0
    # Master-integrity audit (DEC-INSTRUMENT-MASTER-INTEGRITY-001) ran END-TO-END against the
    # full leg-bearing universe BEFORE the lean discard — it SAW the WIPRO CE leg (legs > 0)
    # and resolved it to its WIPRO spot, so the verdict is green. This pins the
    # audit-before-discard ordering through the real load_instrument_master path.
    mi = result["master_integrity"]
    assert mi["type_counts"]["legs"] == 1  # the WIPRO CE leg was audited (not yet discarded)
    assert mi["type_counts"]["ce"] == 1
    assert mi["is_green"] is True and mi["gating_failures"] == 0
    assert store.get_meta("master_integrity") is not None  # persisted to schema_meta
    # Economics audit also ran end-to-end (DEC-INSTRUMENT-ECONOMICS-INTEGRITY-001): the lone
    # WIPRO CE leg is trivially lot-uniform → green; persisted to schema_meta.
    ei = result["economics_integrity"]
    assert ei["is_green"] is True and ei["total_legs"] == 1
    assert store.get_meta("economics_integrity") is not None
    # Parse-skip honesty (DEC-PARSE-SKIP-REPORT-001): a clean master reads green with
    # zero skips over the 4 in-scope rows; persisted to schema_meta for the panel.
    ps = result["parse_skips"]
    assert ps["is_green"] is True and ps["skipped_count"] == 0
    assert ps["in_scope_rows"] == 4 and ps["samples"] == []
    assert store.get_meta("instrument_parse_skips.report") is not None


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


def test_load_report_carries_security_id_collisions(tmp_path: Path) -> None:
    """A DISTINCT-name identity collision surfaces on the LOADER report.

    Two NSE CASH equities share the numeric ``exchange_token`` ``7777`` — the
    same ``(security_id, exch_id, segment)`` store identity (the operator-observed
    "13, NSE, CASH" collision class) — under DIFFERENT trading_symbols, each made
    F&O-eligible by its own CE leg so both are admitted to the lean universe. The
    store's write-boundary dedup drops one (last-wins + a per-drop WARNING) and the
    count rides the load report END-TO-END: ``UpsertResult.security_id_collisions``
    → ``result["security_id_collisions"]`` + the ``universe.load.groww`` counts line
    (fail-before: the loader DISCARDED the UpsertResult, so the count was write-only
    — DEC-MAPPING-COLLISION-HONESTY-001 review fold).

    Reconciled at the DEC-DATA-SAFETY-LEFTOVERS-001 merge: the original fixture used
    two IDX rows, but an INDEX identity collision is now intercepted UPSTREAM at
    ``build_index`` (keep-first, DEC-INDEX-SURROGATE-COLLISION-DETECT-001) and never
    reaches the store — the IDX arm is pinned separately below; the store→report
    plumbing is exercised here via the CASH segment fix (d) deliberately scopes out.
    """
    collide_a = _row(
        exchange="NSE",
        exchange_token="7777",
        trading_symbol="COLLIDEA",
        groww_symbol="NSE-COLLIDEA",
        name="Collide A",
        instrument_type="EQ",
        segment="CASH",
        series="EQ",
        lot_size="1",
        tick_size="0.05",
    )
    collide_b = _row(
        exchange="NSE",
        exchange_token="7777",  # SAME numeric token → same store identity …
        trading_symbol="COLLIDEB",  # … DIFFERENT symbol_name → a counted collision
        groww_symbol="NSE-COLLIDEB",
        name="Collide B",
        instrument_type="EQ",
        segment="CASH",
        series="EQ",
        lot_size="1",
        tick_size="0.05",
    )
    leg_a = _row(
        exchange="NSE",
        exchange_token="50801",
        trading_symbol="COLLIDEA2661621800CE",
        groww_symbol="NSE-COLLIDEA-16Jun26-300-CE",
        instrument_type="CE",
        segment="FNO",
        underlying_symbol="COLLIDEA",
        underlying_exchange_token="7777",
        expiry_date="2026-06-16",
        strike_price="300",
        lot_size="1500",
        tick_size="0.05",
    )
    leg_b = _row(
        exchange="NSE",
        exchange_token="50802",
        trading_symbol="COLLIDEB2661621800CE",
        groww_symbol="NSE-COLLIDEB-16Jun26-300-CE",
        instrument_type="CE",
        segment="FNO",
        underlying_symbol="COLLIDEB",
        underlying_exchange_token="7777",
        expiry_date="2026-06-16",
        strike_price="300",
        lot_size="1500",
        tick_size="0.05",
    )
    store = _store(tmp_path)
    with capture_logs() as logs:
        result = load_instrument_master(
            store,
            http_get=lambda _url: _csv([collide_a, collide_b, leg_a, leg_b]),
            run_id_factory=lambda: "test-collide",
            constituent_opener=_empty_opener,
            constituent_cache_dir=tmp_path / "cache",
        )
    # The LOADER report carries the collision count (was write-only before).
    assert result["security_id_collisions"] == 1
    # The store's per-drop WARNING still fires (the diagnostic payload) …
    assert len(_events(logs, "instrument_store.security_id_collision")) == 1
    # … and the aggregate rides the loader's counts log line too.
    groww_events = _events(logs, "universe.load.groww")
    assert len(groww_events) == 1
    assert groww_events[0]["security_id_collisions"] == 1


def test_load_persists_parse_skip_report_for_dirty_master(tmp_path: Path) -> None:
    """A master with malformed in-scope rows persists an HONEST parse-skip report.

    DEC-PARSE-SKIP-REPORT-001 regression: before the report, the skip accounting
    (DEC-INSTRUMENT-SKIP-NO-GSYM-001) was logged then DISCARDED — the load result
    and schema_meta carried nothing, so master rot was invisible outside /logs.
    One junk row (blank groww_symbol) stays GREEN (within max(10, 0.1%)); the
    count + sample reason land on BOTH the load result and the persisted snapshot.
    """
    junk = _row(
        exchange="BSE",
        exchange_token="93",
        name="BSE FOCUSED IT",
        instrument_type="IDX",
        segment="CASH",
    )
    store = _store(tmp_path)
    with capture_logs() as logs:
        result = load_instrument_master(
            store,
            http_get=lambda _url: _csv([*_ROWS, junk]),
            run_id_factory=lambda: "test-skips",
            constituent_opener=_empty_opener,
            constituent_cache_dir=tmp_path / "cache",
        )
    ps = result["parse_skips"]
    assert ps["skipped_count"] == 1
    assert ps["in_scope_rows"] == 5  # 4 parsed + 1 skipped
    assert ps["threshold"] == 10
    assert ps["is_green"] is True  # 1 <= max(10, ceil(0.1% of 5))
    assert len(ps["samples"]) == 1 and "groww_symbol" in ps["samples"][0]
    assert ps["run_id"] == "test-skips"
    # Persisted to schema_meta (read by GET /api/v1/universe/parse-skips).
    raw = store.get_meta("instrument_parse_skips.report")
    assert raw is not None
    assert json.loads(raw)["skipped_count"] == 1
    # One structlog counts line from the persist hook.
    events = _events(logs, "universe.load.parse_skips")
    assert len(events) == 1 and events[0]["is_green"] is True


def test_idx_same_token_collision_intercepted_at_build_index(tmp_path: Path) -> None:
    """An IDX identity collision is caught UPSTREAM — the store never sees it.

    Two NSE IDX rows sharing token ``7777`` collide on the ``(exch_id,
    security_id)`` INDEX identity: since DEC-INDEX-SURROGATE-COLLISION-DETECT-001
    the ``build_index`` chokepoint keep-first-drops the second with its own loud
    ``instrument_master.index_security_id_collision`` WARNING, so the STORE-level
    counter honestly reads 0 (nothing reached the write boundary) and only the
    FIRST index is persisted — deterministic, never the store's last-wins.
    Pins the combined post-merge semantics of DEC-MAPPING-COLLISION-HONESTY-001
    + fix (d) (DEC-DATA-SAFETY-LEFTOVERS-001 merge reconciliation).
    """
    collide_a = _row(
        exchange="NSE",
        exchange_token="7777",
        trading_symbol="COLLIDEA",
        groww_symbol="NSE-COLLIDEA",
        name="Collide A",
        instrument_type="IDX",
        segment="CASH",
    )
    collide_b = _row(
        exchange="NSE",
        exchange_token="7777",
        trading_symbol="COLLIDEB",
        groww_symbol="NSE-COLLIDEB",
        name="Collide B",
        instrument_type="IDX",
        segment="CASH",
    )
    store = _store(tmp_path)
    with capture_logs() as logs:
        result = load_instrument_master(
            store,
            http_get=lambda _url: _csv([collide_a, collide_b]),
            run_id_factory=lambda: "test-collide-idx",
            constituent_opener=_empty_opener,
            constituent_cache_dir=tmp_path / "cache",
        )
    # Intercepted at build_index: keep-first + the build-time WARNING …
    build_warns = _events(logs, "instrument_master.index_security_id_collision")
    assert len(build_warns) == 1
    assert build_warns[0]["kept"] == "NSE-COLLIDEA"
    assert build_warns[0]["dropped"] == "NSE-COLLIDEB"
    # … so the STORE-level collision surfaces honestly as ZERO (never reached).
    assert result["security_id_collisions"] == 0
    assert len(_events(logs, "instrument_store.security_id_collision")) == 0
    # Only the FIRST index is persisted (agrees with the in-memory index).
    symbols = {inst.symbol_name for inst in store.list_index_equity_instruments()}
    assert "COLLIDEA" in symbols
    assert "COLLIDEB" not in symbols


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
    # The empty master audits green with all-zero counts (DEC-INSTRUMENT-MASTER-INTEGRITY-001).
    mi = result.pop("master_integrity")
    assert mi["is_green"] is True
    assert mi["type_counts"]["legs"] == 0 and mi["gating_failures"] == 0
    # Empty master → economics audit green with zero legs (DEC-INSTRUMENT-ECONOMICS-INTEGRITY-001).
    ei = result.pop("economics_integrity")
    assert ei["is_green"] is True and ei["total_legs"] == 0
    # Empty master → zero skips over zero in-scope rows, green (DEC-PARSE-SKIP-REPORT-001).
    ps = result.pop("parse_skips")
    assert ps["is_green"] is True and ps["skipped_count"] == 0 and ps["in_scope_rows"] == 0
    assert result == {
        "parsed": 0,
        "persisted": 0,
        "legs_dropped": 0,
        "filtered": 0,
        "quarantined": 0,
        "security_id_collisions": 0,
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


def test_ntm_fresh_cache_served_without_any_network(tmp_path: Path) -> None:
    """A FRESH cache is served instantly with ZERO network calls (the fast hot path).

    DEC-NTM-CACHE-FIRST-001 — the operator's "why is this slow EVERY time" fix. The
    just-saved cache is younger than the (default 7-day) TTL, so the load serves it
    WITHOUT touching niftyindices: the opener raises ``AssertionError`` if called, so
    a regression that re-introduced LIVE-FIRST fetching fails this test loudly. The
    universe still grows to include the cached SMALLCO constituent.
    """
    cache_dir = tmp_path / "cache"
    constituents = _constituents_csv([("SMALLCO", "INE999A01010")])
    assert save_constituent_csv(constituents, cache_dir=cache_dir) is True
    store = _store(tmp_path)
    result = load_instrument_master(
        store,
        http_get=lambda _url: _csv([*_ROWS, _SMALLCO]),
        constituent_opener=_must_not_fetch_opener,  # fresh cache → live MUST NOT be called
        constituent_cache_dir=cache_dir,
    )
    ntm = result["ntm"]
    assert ntm["admitted"] is True
    assert ntm["reference"] == 1
    assert ntm["from_cache"] is True
    assert ntm["source"] == "cache_fresh"
    assert "fast path" in ntm["constituent_note"]
    assert store.reference_only_security_ids() == frozenset({_SMALLCO_ID})


def test_ntm_stale_cache_served_instantly_then_background_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A POST-TTL cache is served IMMEDIATELY; the refresh runs in the BACKGROUND.

    DEC-NTM-STALE-WHILE-REVALIDATE-001 (supersedes the synchronous post-TTL arm of
    DEC-NTM-CACHE-FIRST-001): with TTL=0 (every cache post-TTL) the load serves the
    STALE list instantly — the live opener is NEVER called on the request thread
    (the captured job is not run yet) — and the captured single-flight background
    job then refreshes the cache, so the NEXT load is a ``cache_fresh`` hit on the
    refreshed data.
    """
    monkeypatch.setenv("BRUTEX_NTM_CACHE_TTL_SECONDS", "0")
    cache_dir = tmp_path / "cache"
    stale = _constituents_csv([("OLDCO", "INE111A01011")])  # the now-stale cache
    assert save_constituent_csv(stale, cache_dir=cache_dir) is True
    fresh = _constituents_csv([("SMALLCO", "INE999A01010")])  # what the live source returns
    opener_calls: list[str] = []

    def _opener(url: str) -> str:
        opener_calls.append(url)
        return fresh

    captured: list[Callable[[], None]] = []
    store = _store(tmp_path)
    result = load_instrument_master(
        store,
        http_get=lambda _url: _csv([*_ROWS, _SMALLCO]),
        constituent_opener=_opener,
        constituent_cache_dir=cache_dir,
        ntm_revalidate_runner=captured.append,  # capture — do NOT run on the request path
    )
    ntm = result["ntm"]
    assert ntm["source"] == "cache_stale_revalidating"
    assert ntm["from_cache"] is True
    assert "stale-while-revalidate" in ntm["constituent_note"]
    assert opener_calls == []  # the request thread NEVER waited on niftyindices
    assert load_cached_constituent_csv(cache_dir=cache_dir) == stale  # not yet refreshed
    # Run the captured background job: the cache is atomically refreshed.
    assert len(captured) == 1
    captured[0]()
    assert opener_calls != []
    assert load_cached_constituent_csv(cache_dir=cache_dir) == fresh
    # The NEXT load picks up the refreshed cache... (TTL=0 keeps revalidating —
    # pinned semantics: the cache is ALWAYS served instantly, never a sync re-fetch.)
    result2 = load_instrument_master(
        store,
        http_get=lambda _url: _csv([*_ROWS, _SMALLCO]),
        constituent_opener=_opener,
        constituent_cache_dir=cache_dir,
        ntm_revalidate_runner=lambda job: job(),  # inline — deterministic
    )
    assert result2["ntm"]["source"] == "cache_stale_revalidating"  # TTL=0 ⇒ always post-TTL
    assert result2["ntm"]["reference"] == 1  # SMALLCO from the refreshed cache
    assert store.reference_only_security_ids() == frozenset({_SMALLCO_ID})


def test_ntm_stale_cache_covers_background_live_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stale cache + an EMPTY background refresh: the stale list still serves (full universe)."""
    monkeypatch.setenv("BRUTEX_NTM_CACHE_TTL_SECONDS", "0")
    cache_dir = tmp_path / "cache"
    constituents = _constituents_csv([("SMALLCO", "INE999A01010")])
    assert save_constituent_csv(constituents, cache_dir=cache_dir) is True
    store = _store(tmp_path)
    result = load_instrument_master(
        store,
        http_get=lambda _url: _csv([*_ROWS, _SMALLCO]),
        constituent_opener=_empty_opener,  # the background refresh finds nothing
        constituent_cache_dir=cache_dir,
        ntm_revalidate_runner=lambda job: job(),  # inline — deterministic
    )
    ntm = result["ntm"]
    assert ntm["admitted"] is True
    assert ntm["reference"] == 1
    assert ntm["from_cache"] is True
    assert ntm["source"] == "cache_stale_revalidating"
    # The empty refresh left the last-good cache untouched (never clobbered).
    assert load_cached_constituent_csv(cache_dir=cache_dir) == constituents
    assert store.reference_only_security_ids() == frozenset({_SMALLCO_ID})


def test_ntm_stale_cache_covers_background_live_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stale cache + a niftyindices timeout in the BACKGROUND refresh: swallowed + logged.

    The request path already returned the stale list; the background worker's
    ``OSError`` is logged (``ntm.revalidate_failed``) and swallowed — never a crash,
    never a crater to the base set, and the in-flight flag is cleared so a later
    load can revalidate again.
    """
    monkeypatch.setenv("BRUTEX_NTM_CACHE_TTL_SECONDS", "0")
    cache_dir = tmp_path / "cache"
    constituents = _constituents_csv([("SMALLCO", "INE999A01010")])
    save_constituent_csv(constituents, cache_dir=cache_dir)
    store = _store(tmp_path)
    with capture_logs() as logs:
        result = load_instrument_master(
            store,
            http_get=lambda _url: _csv([*_ROWS, _SMALLCO]),
            constituent_opener=_boom_opener,  # the background refresh raises
            constituent_cache_dir=cache_dir,
            ntm_revalidate_runner=lambda job: job(),  # inline — deterministic
        )
    ntm = result["ntm"]
    assert ntm["admitted"] is True
    assert ntm["reference"] == 1
    assert ntm["from_cache"] is True
    assert ntm["source"] == "cache_stale_revalidating"
    assert store.reference_only_security_ids() == frozenset({_SMALLCO_ID})
    assert any(e["event"] == "ntm.revalidate_failed" for e in logs)
    # The flag was cleared in the worker's finally — a later spawn is possible.
    assert il._NTM_REVALIDATE_STATE["in_flight"] is False


def test_ntm_no_cache_empty_live_degrades_to_base(tmp_path: Path) -> None:
    """No cache AND an empty live fetch → honest degrade to the base set (source 'none').

    DEC-NTM-CACHE-FIRST-001 — the ``("", "none")`` branch: an empty cache dir with a
    live source that returns nothing admits no constituents and notes the honest "no
    CSV available" reason, never a crash and never invented constituents (Golden Rule #1).
    """
    store = _store(tmp_path)
    result = load_instrument_master(
        store,
        http_get=lambda _url: _csv([*_ROWS, _SMALLCO]),
        constituent_opener=_empty_opener,  # no cache + empty live → "none"
        constituent_cache_dir=tmp_path / "cache",
    )
    assert result["persisted"] == 3  # SMALLCO not admitted — base set only
    assert result["stock_count"] == 1
    ntm = result["ntm"]
    assert ntm["admitted"] is False
    assert "no NIFTY Total Market constituent CSV available" in ntm["reason"]
    assert store.reference_only_security_ids() == frozenset()


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


def test_eq_token_join_persisted_tradable_and_surfaced_in_diagnostic(tmp_path: Path) -> None:
    """A symbol-mismatched F&O STOCK is now PERSISTED as tradable AND audited.

    DEC-FNO-EQ-TOKEN-JOIN-PERSIST-001 (the follow-up to the audit-only split
    DEC-FNO-INDEX-EQ-TOKEN-JOIN-AUDIT-001): BAJAJ-AUTO's spot trading_symbol differs
    from its option leg's underlying_symbol (BAJAJAUTO), so the ticker key
    ``fno_eligible`` misses it — but its GENUINE numeric vendor token equals the leg's
    underlying_exchange_token (the token-join). The stock is now:

    * **persisted** as a universe underlying (``is_universe_underlying`` admits it via
      the id-keyed token-join set), counted as a STOCK; and
    * **tradable** (``reference_only=FALSE`` → ``is_option_pullable`` True), keeping its
      real option chain — closing the prior gap where it was dropped from the lean store.

    It is STILL surfaced in the honest ``token_join_diagnostic`` audit bucket and STILL
    excluded from the index reclassification ``count`` (no INDEX flag flipped).
    """
    store = _store(tmp_path)
    rows = [_NIFTY, _BANKNIFTY, _BAJAJ, _BAJAJ_CE]
    with capture_logs() as logs:
        result = load_instrument_master(
            store,
            http_get=lambda _url: _csv(rows),
            constituent_opener=_empty_opener,
            constituent_cache_dir=tmp_path / "cache",
        )
    # PERSISTED: 2 indices + BAJAJ-AUTO (the symbol-mismatched F&O stock); CE leg dropped.
    assert result["persisted"] == 3
    assert result["legs_dropped"] == 1
    assert result["index_count"] == 2
    assert result["stock_count"] == 1  # self-heal: counted as a stock at query time
    # TRADABLE: not reference_only, so its option chain is pullable.
    bajaj = store.get("BAJAJ-AUTO")
    assert bajaj is not None
    refs = store.reference_only_security_ids()
    assert bajaj.security_id not in refs
    assert is_option_pullable(bajaj, reference_only_ids=refs) is True
    # STILL surfaced in the honest diagnostic bucket; index reclass count unchanged.
    (audit,) = _events(logs, "universe.load.opt_reclassified")
    assert audit["token_join_diagnostic"] == ["NSE:BAJAJ-AUTO"]
    assert audit["token_join"] == []
    assert audit["alias"] == []
    assert audit["symbol_bridge"] == []
    assert audit["count"] == 0


def test_token_join_fno_stock_tradable_even_when_also_ntm_constituent(tmp_path: Path) -> None:
    """Precedence: a symbol-mismatched F&O stock that is ALSO an NTM constituent stays tradable.

    DEC-FNO-EQ-TOKEN-JOIN-PERSIST-001 — the precedence rule (F&O-tradable WINS over
    NTM-reference). BAJAJ-AUTO is token-join F&O-eligible AND matches an NTM constituent
    by ISIN. The ``_ntm_admission`` subtraction removes its ``security_id`` from the
    reference set, so it is persisted ``reference_only=FALSE`` (keeps its option chain),
    NOT tagged a reference constituent — even though the NTM basket matched it.
    """
    store = _store(tmp_path)
    rows = [_NIFTY, _BANKNIFTY, _BAJAJ, _BAJAJ_CE]
    constituents = _constituents_csv([("BAJAJ-AUTO", "INE917I01010")])  # == _BAJAJ's ISIN
    result = load_instrument_master(
        store,
        http_get=lambda _url: _csv(rows),
        constituent_opener=lambda _url: constituents,
        constituent_cache_dir=tmp_path / "cache",
    )
    assert result["persisted"] == 3
    assert result["stock_count"] == 1
    bajaj = store.get("BAJAJ-AUTO")
    assert bajaj is not None
    refs = store.reference_only_security_ids()
    # Tradable wins: the NTM match did NOT tag it reference_only.
    assert refs == frozenset()
    assert bajaj.security_id not in refs
    assert is_option_pullable(bajaj, reference_only_ids=refs) is True
    # The NTM basket matched it but admitted ZERO reference rows (the F&O stock is excluded).
    ntm = result["ntm"]
    assert ntm["matched"] == 1
    assert ntm["reference"] == 0
    assert ntm["admitted"] is False


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


# ── _ntm_cache_ttl_seconds (DEC-NTM-CACHE-FIRST-001) ──────────────────────────
class TestNtmCacheTtlSeconds:
    """The env-tunable NTM cache freshness TTL (PURE; never crashes on a bad value)."""

    def test_absent_env_returns_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BRUTEX_NTM_CACHE_TTL_SECONDS", raising=False)
        assert il._ntm_cache_ttl_seconds() == il._NTM_CACHE_TTL_DEFAULT_SECONDS

    def test_valid_override_is_used(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRUTEX_NTM_CACHE_TTL_SECONDS", "3600")
        assert il._ntm_cache_ttl_seconds() == 3600.0

    def test_non_numeric_falls_back_to_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRUTEX_NTM_CACHE_TTL_SECONDS", "never")
        assert il._ntm_cache_ttl_seconds() == il._NTM_CACHE_TTL_DEFAULT_SECONDS

    def test_non_positive_returned_as_is_to_disable_fast_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A non-positive TTL is returned verbatim so the cache-first comparison treats
        # every (non-negative-age) cache as stale → always refresh.
        monkeypatch.setenv("BRUTEX_NTM_CACHE_TTL_SECONDS", "0")
        assert il._ntm_cache_ttl_seconds() == 0.0
        monkeypatch.setenv("BRUTEX_NTM_CACHE_TTL_SECONDS", "-1")
        assert il._ntm_cache_ttl_seconds() == -1.0


class TestNtmStaleWhileRevalidate:
    """DEC-NTM-STALE-WHILE-REVALIDATE-001 — the single-flight background refresh."""

    @pytest.fixture(autouse=True)
    def _reset_single_flight(self) -> Iterator[None]:
        il._NTM_REVALIDATE_STATE["in_flight"] = False
        yield
        il._NTM_REVALIDATE_STATE["in_flight"] = False

    def test_no_cache_boot_stays_synchronous_live(self, tmp_path: Path) -> None:
        """Truly-no-cache boot: the bounded SYNCHRONOUS fetch runs and seeds the cache."""
        cache_dir = tmp_path / "cache"
        fresh = _constituents_csv([("SMALLCO", "INE999A01010")])
        spawned: list[Callable[[], None]] = []
        text, source = il._resolve_constituent_csv(
            lambda _url: fresh, cache_dir=cache_dir, revalidate_runner=spawned.append
        )
        assert (text, source) == (fresh, "live")
        assert spawned == []  # nothing to serve meanwhile — no background job
        assert load_cached_constituent_csv(cache_dir=cache_dir) == fresh  # seeded

    def test_no_cache_boot_oserror_propagates(self, tmp_path: Path) -> None:
        """No cache + unreachable live source: the OSError reaches the fail-soft handler."""
        with pytest.raises(OSError, match="timed out"):
            il._resolve_constituent_csv(_boom_opener, cache_dir=tmp_path / "cache")

    def test_second_post_ttl_call_never_double_spawns(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Single-flight: while a refresh is in flight, a second load spawns NOTHING."""
        monkeypatch.setenv("BRUTEX_NTM_CACHE_TTL_SECONDS", "0")
        cache_dir = tmp_path / "cache"
        stale = _constituents_csv([("OLDCO", "INE111A01011")])
        assert save_constituent_csv(stale, cache_dir=cache_dir) is True
        captured: list[Callable[[], None]] = []
        first = il._resolve_constituent_csv(
            _empty_opener, cache_dir=cache_dir, revalidate_runner=captured.append
        )
        second = il._resolve_constituent_csv(
            _empty_opener, cache_dir=cache_dir, revalidate_runner=captured.append
        )
        assert first == (stale, "cache_stale_revalidating")
        assert second == (stale, "cache_stale_revalidating")  # still served instantly
        assert len(captured) == 1  # ONE background job — the slow host is never doubled
        captured[0]()  # the in-flight job finishes → the flag clears
        third = il._resolve_constituent_csv(
            _empty_opener, cache_dir=cache_dir, revalidate_runner=captured.append
        )
        assert third == (stale, "cache_stale_revalidating")
        assert len(captured) == 2  # a NEW cycle may revalidate again

    def test_spawn_failure_clears_the_flag_and_never_breaks_the_load(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A raising runner (spawn failure) is swallowed + logged; the flag is cleared."""
        monkeypatch.setenv("BRUTEX_NTM_CACHE_TTL_SECONDS", "0")
        cache_dir = tmp_path / "cache"
        stale = _constituents_csv([("OLDCO", "INE111A01011")])
        assert save_constituent_csv(stale, cache_dir=cache_dir) is True

        def _bad_runner(_job: Callable[[], None]) -> None:
            raise RuntimeError("thread spawn failed")

        with capture_logs() as logs:
            text, source = il._resolve_constituent_csv(
                _empty_opener, cache_dir=cache_dir, revalidate_runner=_bad_runner
            )
        assert (text, source) == (stale, "cache_stale_revalidating")  # load unaffected
        assert any(e["event"] == "ntm.revalidate_spawn_failed" for e in logs)
        assert il._NTM_REVALIDATE_STATE["in_flight"] is False  # a later load can revalidate again

    def test_default_runner_spawns_a_daemon_thread(self) -> None:
        """The production spawner runs the job on a named daemon thread."""
        done = threading.Event()
        il._default_revalidate_runner(done.set)
        assert done.wait(timeout=30.0)


# ── OPT-vs-REF bridge primitives (DEC-FNO-INDEX-ALIAS-001) ────────────────────
# These direct-call unit tests pin the per-branch behaviour of the three bridge
# helpers IN THE MAPPED test file for this module, so the auto-discovery coverage
# gate (DEC-CICD-GATE-HONESTY-001) — which runs only the import-verified
# test_*instrument_loader*.py suite — measures instrument_loader.py at 100% line +
# branch. The same branches are also covered end-to-end by
# tests/unit/test_fno_index_diagnostic.py (a sibling, COMMON behaviour — no logic
# duplicated, only the per-file gate's view restored).
_SOURCED_FNO_INDEX_ALIASES = {"NIFTYJR": "NIFTYNXT50", "NIFTYMIDSELECT": "MIDCPNIFTY"}


def _idx_inst(
    symbol: str,
    *,
    security_id: int = 0,
    feed_token: str | None = None,
    exchange: Exchange = Exchange.NSE,
) -> Instrument:
    """Build an IDX Instrument (spot index row)."""
    return Instrument(
        exch_id=exchange,
        security_id=security_id,
        symbol_name=symbol,
        display_name=symbol,
        instrument=InstrumentType.IDX,
        segment=Segment.INDEX,
        series=None,
        isin=None,
        underlying_symbol=symbol,
        underlying_security_id=None,
        lot_size=0,
        tick_size=Decimal("0"),
        sm_freeze_qty=None,
        sm_expiry_date=None,
        strike_price=None,
        asm_gsm_flag=False,
        feed_token=feed_token,
    )


def _eq_inst(
    symbol: str,
    *,
    security_id: int,
    feed_token: str | None,
    exchange: Exchange = Exchange.NSE,
) -> Instrument:
    """Build a cash-equity Instrument carrying an explicit security_id + feed_token."""
    return Instrument(
        exch_id=exchange,
        security_id=security_id,
        symbol_name=symbol,
        display_name=symbol,
        instrument=InstrumentType.EQ,
        segment=Segment.CASH,
        series="EQ",
        isin=None,
        underlying_symbol=symbol,
        underlying_security_id=None,
        lot_size=1,
        tick_size=Decimal("0.05"),
        sm_freeze_qty=None,
        sm_expiry_date=None,
        strike_price=None,
        asm_gsm_flag=False,
        feed_token=feed_token,
    )


def _opt_inst(
    underlying: str,
    *,
    underlying_security_id: int | None,
    exchange: Exchange = Exchange.NSE,
    option_type: InstrumentType = InstrumentType.CE,
) -> Instrument:
    """Build a CE/PE leg naming *underlying* with an explicit underlying_security_id."""
    return Instrument(
        exch_id=exchange,
        security_id=abs(hash(("opt", underlying, option_type))) % 100_000,
        symbol_name=f"{underlying}-OPT",
        display_name=underlying,
        instrument=option_type,
        segment=Segment.FNO,
        series=None,
        isin=None,
        underlying_symbol=underlying,
        underlying_security_id=underlying_security_id,
        lot_size=50,
        tick_size=Decimal("0.05"),
        sm_freeze_qty=None,
        sm_expiry_date=date(2026, 6, 16),
        strike_price=Decimal("100"),
        asm_gsm_flag=False,
        feed_token=None,
    )


class TestResolveFnoIndexAliasesEnvParse:
    """``resolve_fno_index_aliases`` parses the env override defensively (never crashes)."""

    def test_invalid_json_falls_back_to_sourced_defaults(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Malformed JSON → the ``except ValueError`` arm returns the sourced defaults.
        monkeypatch.setenv(FNO_INDEX_ALIASES_OVERRIDE_ENV, "{not json")
        assert resolve_fno_index_aliases() == dict(_SOURCED_FNO_INDEX_ALIASES)

    def test_non_object_json_falls_back_to_sourced_defaults(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Valid JSON that is not a dict (a list) → the ``not isinstance(parsed, dict)``
        # arm returns the sourced defaults.
        monkeypatch.setenv(FNO_INDEX_ALIASES_OVERRIDE_ENV, '["x", "y"]')
        assert resolve_fno_index_aliases() == dict(_SOURCED_FNO_INDEX_ALIASES)

    def test_non_str_and_blank_pairs_are_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A non-str value, a blank value, and a blank key each fail the pair guard
        # (the loop branch that does NOT add); only the good pair is kept.
        monkeypatch.setenv(
            FNO_INDEX_ALIASES_OVERRIDE_ENV,
            '{"GOODIDX": "GOODFNO", "BADINT": 5, "BLANKVAL": "  ", "  ": "X"}',
        )
        aliases = resolve_fno_index_aliases()
        assert aliases["GOODIDX"] == "GOODFNO"
        assert "BADINT" not in aliases
        assert "BLANKVAL" not in aliases
        assert "" not in aliases
        # The sourced defaults are still present (merged, not replaced).
        assert aliases["NIFTYJR"] == "NIFTYNXT50"


class TestGrowwSymbolIndexBridgeDashless:
    """``_groww_symbol_index_bridge`` skips a malformed dashless groww_symbol."""

    def test_dashless_groww_symbol_is_skipped(self) -> None:
        # A groww_symbol with no "-" → index_symbol_of() returns empty → the row is
        # skipped (no alias, no bridge), never raises.
        index = build_index([("WEIRD", _idx_inst("WEIRD"))])
        bridged, aliases = _groww_symbol_index_bridge(index, frozenset())
        assert bridged == frozenset()
        assert aliases == frozenset()


class TestClassifyOptionPullableBranches:
    """Per-mechanism arms of ``_classify_option_pullable`` (token_join / symbol_bridge)."""

    def test_idx_token_join_is_a_real_rescue(self) -> None:
        # An IDX whose GENUINE numeric vendor token equals an NSE option leg's
        # underlying_security_id IS a real rescue → mechanism "token_join", added to
        # the (index-only) bridged set.
        idx = _idx_inst("VERBOSEIDX", security_id=51, feed_token="51")
        leg = _opt_inst("COMPACTIDX", underlying_security_id=51)
        index = build_index([("NSE-VERBOSEIDX", idx), ("NSE-COMPACTIDX-16Jun26-1-CE", leg)])
        options_eligible = build_options_eligible_underlyings(index.universe)
        bridged, _aliases, rescued, _eq_ids = _classify_option_pullable(index, options_eligible)
        assert (Exchange.NSE, "VERBOSEIDX") in bridged
        assert rescued["token_join"] == ["NSE:VERBOSEIDX"]
        assert rescued["token_join_diagnostic"] == []

    def test_bse_equity_token_collision_is_excluded(self) -> None:
        # A BSE EQ whose numeric token coincidentally equals an NSE option leg's
        # underlying_security_id is NOT an F&O stock (no BSE derivatives) → the else
        # arm continues: not persisted, not surfaced.
        bse = _eq_inst("FOO", security_id=16669, feed_token="16669", exchange=Exchange.BSE)
        leg = _opt_inst("FOO", underlying_security_id=16669)  # an NSE leg
        index = build_index([("BSE-FOO", bse), ("NSE-FOO-16Jun26-9000-CE", leg)])
        options_eligible = build_options_eligible_underlyings(index.universe)
        bridged, _aliases, rescued, eq_ids = _classify_option_pullable(index, options_eligible)
        assert eq_ids == frozenset()
        assert rescued["token_join_diagnostic"] == []
        assert (Exchange.BSE, "FOO") not in bridged

    def test_symbol_bridge_rescues_verbose_idx(self) -> None:
        # A verbose-named IDX whose groww_symbol F&O symbol IS options-eligible, with NO
        # genuine numeric token (so it is reached via the groww_symbol bridge, mechanism
        # "symbol_bridge", not token_join).
        midcap = _idx_inst("NIFTY MID SELECT")  # surrogate id 0, no numeric feed_token
        leg = _opt_inst("MIDCPNIFTY", underlying_security_id=None)
        index = build_index([("NSE-MIDCPNIFTY", midcap), ("NSE-MIDCPNIFTY-16Jun26-12000-CE", leg)])
        options_eligible = build_options_eligible_underlyings(index.universe)
        bridged, _aliases, rescued, _eq_ids = _classify_option_pullable(index, options_eligible)
        assert (Exchange.NSE, "NIFTY MID SELECT") in bridged
        assert rescued["symbol_bridge"] == ["NSE:NIFTY MID SELECT"]
        assert rescued["token_join"] == []
