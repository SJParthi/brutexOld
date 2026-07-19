"""Tests for the F&O-index symbol-mismatch diagnostic (DEC-FNO-INDEX-MATCH-DIAG-001).

An OBSERVE-ONLY diagnostic: it surfaces F&O indices being silently dropped to
REFERENCE_ONLY (loses the OPT badge) because the vendor stamps a different
``underlying_symbol`` on the option legs than the index's ``IDX`` row
``trading_symbol`` (``symbol_name``). :func:`is_spot_only_index` keys the
OPT-vs-REF badge on an EXACT ``(exchange, symbol_name)`` match
(DEC-NON-OPTION-INDEX-REFTAG-001), so any spelling drift mis-classifies the
index. The diagnostic REPORTS the real strings; it changes NO classification.

Two layers are tested here:

* the pure :func:`brutex.universe.diagnose_unmatched_option_underlyings` +
  :func:`brutex.universe.normalize_underlying_symbol` (4 master shapes: an
  exact-matched index, a case/space-only mismatch, a genuine-abbreviation
  mismatch, and an F&O-eligible equity that must NOT be reported); and
* the loader wiring — :func:`brutex.dashboard.instrument_loader.load_instrument_master`
  emits ONE ``universe.load.fno_index_unmatched`` structlog line with the right
  gaps (asserted via :func:`structlog.testing.capture_logs`, mirroring
  ``tests/unit/test_dashboard_pull_controller.py``), and is SILENT when there
  is no gap.

CSV fixtures mirror ``tests/unit/test_dashboard_instrument_loader.py`` (the
21-column Groww master, fake ``http_get``, a REAL temp ``InstrumentStore``).
NO Groww token / creds anywhere — the master is a public CSV.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.adapters.groww.constants import INSTRUMENT_CSV_COLUMNS
from brutex.adapters.groww.instruments import build_index
from brutex.config.feed_scope import FEED_SCOPE_ENV as _FEED_SCOPE_ENV_1934
from brutex.dashboard.instrument_loader import (
    FNO_INDEX_ALIASES_OVERRIDE_ENV,
    _classify_option_pullable,
    _groww_symbol_index_bridge,
    _has_genuine_numeric_token,
    _option_underlying_security_ids,
    _resolve_fno_index_aliases,
    load_instrument_master,
)
from brutex.data.instrument_store import InstrumentStore
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from brutex.universe import (
    OptionUnderlyingGap,
    build_options_eligible_underlyings,
    diagnose_unmatched_option_underlyings,
    is_spot_only_index,
    normalize_underlying_symbol,
)
from structlog.testing import capture_logs


@pytest.fixture(autouse=True)
def _full_scope_arm(monkeypatch: pytest.MonkeyPatch) -> None:
    """EXPLICIT full arm (DEC-UNIVERSE-INDEX-ONLY-001 — the §6 migration law).

    This suite pins the pre-#1934 verbose-index bridging + FNO-index-gap
    DIAGNOSTIC mechanics over MIDCPNIFTY/FINNIFTY/NIFTYNXT50 fixtures —
    indices outside the shipped ``index4`` scope; under
    ``BRUTEX_FEED_SCOPE=full`` those legacy rules are byte-identical (the
    parity half of the §6 widen-path pin). The shipped index4-default guard
    arms live in ``test_universe_index_only_scope.py`` + the scope-gate
    batteries (migration Law 1)."""
    monkeypatch.setenv(_FEED_SCOPE_ENV_1934, "full")


# ────────────────────────────────────────────────────────────────────────
# Instrument fixtures for the pure-function layer
# ────────────────────────────────────────────────────────────────────────


def _idx(symbol: str, *, exchange: Exchange = Exchange.NSE) -> Instrument:
    """Build a minimal IDX-type Instrument with ``symbol_name == symbol``."""
    return Instrument(
        exch_id=exchange,
        security_id=abs(hash(("idx", symbol))) % 100_000,
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
    )


def _equity(symbol: str, *, exchange: Exchange = Exchange.NSE) -> Instrument:
    """Build a cash-equity Instrument with ``symbol_name == symbol``."""
    return Instrument(
        exch_id=exchange,
        security_id=abs(hash(("eq", symbol))) % 100_000,
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
    )


def _option(
    underlying: str,
    *,
    exchange: Exchange = Exchange.NSE,
    option_type: InstrumentType = InstrumentType.CE,
) -> Instrument:
    """Build a CE/PE option leg whose ``underlying_symbol == underlying``."""
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
        underlying_security_id=None,
        lot_size=50,
        tick_size=Decimal("0.05"),
        sm_freeze_qty=None,
        sm_expiry_date=date(2026, 6, 16),
        strike_price=Decimal("100"),
        asm_gsm_flag=False,
    )


# ────────────────────────────────────────────────────────────────────────
# normalize_underlying_symbol
# ────────────────────────────────────────────────────────────────────────


class TestNormalizeUnderlyingSymbol:
    """Uppercase + strip spaces/hyphens; NEVER an alias map (Golden Rule #1)."""

    def test_strips_spaces_and_uppercases(self) -> None:
        assert normalize_underlying_symbol("NIFTY NEXT 50") == "NIFTYNEXT50"

    def test_strips_hyphens(self) -> None:
        assert normalize_underlying_symbol("NIFTY-IT") == "NIFTYIT"

    def test_lowercase_folds_to_upper(self) -> None:
        assert normalize_underlying_symbol("Nifty Bank") == "NIFTYBANK"

    def test_already_normal_is_idempotent(self) -> None:
        assert normalize_underlying_symbol("MIDCPNIFTY") == "MIDCPNIFTY"

    def test_genuine_abbreviation_does_not_fold(self) -> None:
        # Normalization must NOT bridge a real abbreviation — that is the whole
        # point of the nearest=None signal.
        assert normalize_underlying_symbol("MIDCPNIFTY") != normalize_underlying_symbol(
            "NIFTY MIDCAP SELECT"
        )


# ────────────────────────────────────────────────────────────────────────
# diagnose_unmatched_option_underlyings
# ────────────────────────────────────────────────────────────────────────


class TestDiagnoseUnmatchedOptionUnderlyings:
    """The four master shapes the diagnostic must distinguish."""

    def test_exact_matched_index_is_not_reported(self) -> None:
        # FINNIFTY's IDX symbol_name == its option underlying_symbol → matched.
        instruments = [_idx("FINNIFTY"), _option("FINNIFTY")]
        assert diagnose_unmatched_option_underlyings(instruments) == []

    def test_case_space_mismatch_reports_with_nearest(self) -> None:
        # Leg underlying "NIFTY NEXT 50" vs IDX "NIFTYNEXT50": a pure case/space
        # mismatch → reported, nearest_index resolved by normalization.
        instruments = [_idx("NIFTYNEXT50"), _option("NIFTY NEXT 50")]
        (gap,) = diagnose_unmatched_option_underlyings(instruments)
        assert gap == OptionUnderlyingGap(
            exchange=Exchange.NSE,
            underlying_symbol="NIFTY NEXT 50",
            matched_index_symbol_name=None,
            nearest_index_symbol_name="NIFTYNEXT50",
        )

    def test_genuine_abbreviation_reports_with_nearest_none(self) -> None:
        # Leg underlying "MIDCPNIFTY" but the IDX is spelled "NIFTY MIDCAP
        # SELECT": normalization cannot bridge it → reported, nearest_index None.
        instruments = [_idx("NIFTY MIDCAP SELECT"), _option("MIDCPNIFTY")]
        (gap,) = diagnose_unmatched_option_underlyings(instruments)
        assert gap.underlying_symbol == "MIDCPNIFTY"
        assert gap.matched_index_symbol_name is None
        assert gap.nearest_index_symbol_name is None

    def test_fno_eligible_equity_is_not_reported(self) -> None:
        # RELIANCE option underlying names an EQ row → it is a STOCK, not an
        # index → must NOT be reported (its spot row is the EQ row).
        instruments = [_equity("RELIANCE"), _option("RELIANCE")]
        assert diagnose_unmatched_option_underlyings(instruments) == []

    def test_put_leg_also_qualifies(self) -> None:
        # A PE leg (not just CE) is an option leg → its underlying is scanned.
        instruments = [_idx("NIFTYNEXT50"), _option("NIFTY NEXT 50", option_type=InstrumentType.PE)]
        (gap,) = diagnose_unmatched_option_underlyings(instruments)
        assert gap.underlying_symbol == "NIFTY NEXT 50"

    def test_mixed_master_sorted_deterministically(self) -> None:
        # All four shapes in one master, fed out of order → only the two
        # index-like mismatches reported, sorted by (exchange, underlying).
        instruments = [
            _option("MIDCPNIFTY"),
            _idx("NIFTY MIDCAP SELECT"),
            _option("NIFTY NEXT 50"),
            _idx("NIFTYNEXT50"),
            _idx("FINNIFTY"),
            _option("FINNIFTY"),
            _equity("RELIANCE"),
            _option("RELIANCE"),
        ]
        gaps = diagnose_unmatched_option_underlyings(instruments)
        assert [g.underlying_symbol for g in gaps] == ["MIDCPNIFTY", "NIFTY NEXT 50"]
        assert [g.nearest_index_symbol_name for g in gaps] == [None, "NIFTYNEXT50"]

    def test_non_nse_option_leg_is_ignored(self) -> None:
        # The diagnostic only scans NSE option legs (mirrors
        # build_options_eligible_underlyings); a BSE option leg is skipped.
        instruments = [_option("SOMEIDX", exchange=Exchange.BSE)]
        assert diagnose_unmatched_option_underlyings(instruments) == []

    def test_empty_iterable(self) -> None:
        assert diagnose_unmatched_option_underlyings([]) == []

    def test_nse_future_leg_is_skipped(self) -> None:
        # A FUT leg is an NSE derivative but NOT an option (is_option is False) →
        # it reaches the option branch, fails it, and contributes no underlying.
        # (Covers the elif's False arm — the row is neither IDX, EQ, nor option.)
        future = Instrument(
            exch_id=Exchange.NSE,
            security_id=42,
            symbol_name="NIFTYNEXT50-FUT",
            display_name="NIFTY NEXT 50",
            instrument=InstrumentType.FUT,
            segment=Segment.FNO,
            series=None,
            isin=None,
            underlying_symbol="NIFTY NEXT 50",
            underlying_security_id=None,
            lot_size=25,
            tick_size=Decimal("0.05"),
            sm_freeze_qty=None,
            sm_expiry_date=date(2026, 6, 16),
            strike_price=None,
            asm_gsm_flag=False,
        )
        # No option leg on the underlying → the FUT alone yields NO gap.
        assert diagnose_unmatched_option_underlyings([future]) == []

    def test_duplicate_option_legs_report_one_gap(self) -> None:
        # Two CE legs (different strikes) on the SAME unmatched underlying →
        # ONE gap (distinct underlyings only).
        instruments = [_option("NIFTY NEXT 50"), _option("NIFTY NEXT 50")]
        gaps = diagnose_unmatched_option_underlyings(instruments)
        assert len(gaps) == 1
        assert gaps[0].underlying_symbol == "NIFTY NEXT 50"


# ────────────────────────────────────────────────────────────────────────
# Loader wiring — the universe.load.fno_index_unmatched structlog line
# ────────────────────────────────────────────────────────────────────────

_HEADER = ",".join(INSTRUMENT_CSV_COLUMNS)


def _row(**over: str) -> str:
    """Build one 21-field Groww CSV row; every column defaults blank."""
    cells = dict.fromkeys(INSTRUMENT_CSV_COLUMNS, "")
    cells.update(over)
    return ",".join(cells[col] for col in INSTRUMENT_CSV_COLUMNS)


def _csv(rows: list[str]) -> str:
    """Join *rows* under the canonical Groww header into a CSV text blob."""
    return "\n".join([_HEADER, *rows]) + "\n"


def _empty_opener(_url: str) -> str:
    """A constituent opener that returns nothing (no NTM admission)."""
    return ""


def _store(tmp_path: Path) -> InstrumentStore:
    """Open a fresh, empty real :class:`InstrumentStore` in *tmp_path*."""
    return InstrumentStore.open(tmp_path / "instruments.duckdb")


def _events(logs: Sequence[Mapping[str, object]], name: str) -> list[Mapping[str, object]]:
    """All captured events with ``event == name`` (in emission order)."""
    return [e for e in logs if e["event"] == name]


# Brute-force indices so the lean universe persists (keeps the load realistic).
_NIFTY = _row(
    exchange="NSE",
    exchange_token="NIFTY",
    trading_symbol="NIFTY",
    groww_symbol="NSE-NIFTY",
    name="NIFTY 50",
    instrument_type="IDX",
    segment="CASH",
)
# An IDX whose trading_symbol ("NIFTYNEXT50") differs from the underlying_symbol
# the vendor stamps on its option leg ("NIFTY NEXT 50") by CASE/SPACE only →
# reported with nearest_index="NIFTYNEXT50".
_NIFTYNEXT50 = _row(
    exchange="NSE",
    exchange_token="NIFTYNEXT50",
    trading_symbol="NIFTYNEXT50",
    groww_symbol="NSE-NIFTYNEXT50",
    name="NIFTY NEXT 50",
    instrument_type="IDX",
    segment="CASH",
)
_NIFTYNEXT50_CE = _row(
    exchange="NSE",
    exchange_token="70001",
    trading_symbol="NIFTYNEXT502661670000CE",
    groww_symbol="NSE-NIFTYNEXT50-16Jun26-70000-CE",
    instrument_type="CE",
    segment="FNO",
    underlying_symbol="NIFTY NEXT 50",
    underlying_exchange_token="70002",
    expiry_date="2026-06-16",
    strike_price="70000",
    lot_size="25",
    tick_size="0.05",
)
# An option leg whose underlying ("MIDCPNIFTY") has NO IDX spot row at all
# (genuine abbreviation, no normalization bridge) → reported nearest_index=None.
_MIDCP_CE = _row(
    exchange="NSE",
    exchange_token="71001",
    trading_symbol="MIDCPNIFTY2661612000CE",
    groww_symbol="NSE-MIDCPNIFTY-16Jun26-12000-CE",
    instrument_type="CE",
    segment="FNO",
    underlying_symbol="MIDCPNIFTY",
    underlying_exchange_token="71002",
    expiry_date="2026-06-16",
    strike_price="12000",
    lot_size="120",
    tick_size="0.05",
)
# A FINNIFTY index whose option leg underlying matches EXACTLY → no gap.
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
# An F&O-eligible STOCK whose option underlying names its EQ row → never a gap.
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
    trading_symbol="WIPRO2661630000CE",
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


class TestLoaderEmitsFnoIndexGapLine:
    """The load emits ONE ``universe.load.fno_index_unmatched`` line when gaps exist."""

    def test_emits_one_line_with_the_right_gaps(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        rows = [
            _NIFTY,
            _NIFTYNEXT50,
            _NIFTYNEXT50_CE,
            _MIDCP_CE,
            _FINNIFTY,
            _FINNIFTY_CE,
            _WIPRO,
            _WIPRO_CE,
        ]
        with capture_logs() as logs:
            load_instrument_master(
                store,
                http_get=lambda _url: _csv(rows),
                run_id_factory=lambda: "diag-load",
                constituent_opener=_empty_opener,
                constituent_cache_dir=tmp_path / "cache",
            )
        (line,) = _events(logs, "universe.load.fno_index_unmatched")
        # MIDCPNIFTY (abbrev → None) + NIFTY NEXT 50 (case/space → NIFTYNEXT50);
        # FINNIFTY (exact) + WIPRO (stock) are NOT reported. Sorted by underlying.
        assert line["count"] == 2
        assert line["gaps"] == [
            {"underlying": "MIDCPNIFTY", "nearest_index": None, "exchange": "NSE"},
            {"underlying": "NIFTY NEXT 50", "nearest_index": "NIFTYNEXT50", "exchange": "NSE"},
        ]

    def test_silent_when_no_gap(self, tmp_path: Path) -> None:
        # Only an exactly-matched index + an F&O stock → no gap → the line is
        # NEVER emitted (count > 0 guard).
        store = _store(tmp_path)
        rows = [_NIFTY, _FINNIFTY, _FINNIFTY_CE, _WIPRO, _WIPRO_CE]
        with capture_logs() as logs:
            load_instrument_master(
                store,
                http_get=lambda _url: _csv(rows),
                run_id_factory=lambda: "clean-load",
                constituent_opener=_empty_opener,
                constituent_cache_dir=tmp_path / "cache",
            )
        assert _events(logs, "universe.load.fno_index_unmatched") == []

    def test_classification_unchanged_finnifty_still_pullable(self, tmp_path: Path) -> None:
        # ZERO behavior change: the matched FINNIFTY keeps its OPT badge (NOT
        # reference_only) and the diagnostic is purely additive.
        store = _store(tmp_path)
        rows = [_NIFTY, _NIFTYNEXT50, _NIFTYNEXT50_CE, _FINNIFTY, _FINNIFTY_CE]
        load_instrument_master(
            store,
            http_get=lambda _url: _csv(rows),
            run_id_factory=lambda: "badge-load",
            constituent_opener=_empty_opener,
            constituent_cache_dir=tmp_path / "cache",
        )
        refs = store.reference_only_security_ids()
        finnifty = store.get("FINNIFTY")
        assert finnifty is not None
        # FINNIFTY (exact option match) is NOT tagged reference_only — unchanged.
        assert finnifty.security_id not in refs


# ════════════════════════════════════════════════════════════════════════
# DEC-FNO-INDEX-GROWW-SYMBOL-MATCH-001 — the groww_symbol bridge
# ════════════════════════════════════════════════════════════════════════
#
# The REAL vendor shape the live ``make run`` exposed: the option legs name the
# compact F&O symbol ("MIDCPNIFTY" / "NIFTYNXT50") while the IDX spot row carries
# a VERBOSE trading_symbol ("NIFTY MID SELECT" / "NIFTY NEXT 50") — so the
# symbol_name-only badge dropped the index to REF (count=2, nearest_index=None).
# The IDX's own groww_symbol ("NSE-MIDCPNIFTY") carries the F&O symbol, which IS
# the option legs' underlying_symbol → the vendor-canonical bridge.

# A MIDCPNIFTY index spot row: groww_symbol's F&O symbol "MIDCPNIFTY" matches the
# option leg, but its VERBOSE trading_symbol does NOT (the real-world shape).
_MIDCP_IDX = _row(
    exchange="NSE",
    exchange_token="MIDCPNIFTY",
    trading_symbol="NIFTY MID SELECT",
    groww_symbol="NSE-MIDCPNIFTY",
    name="Nifty Midcap Select",
    instrument_type="IDX",
    segment="CASH",
)
_MIDCP_IDX_CE = _row(
    exchange="NSE",
    exchange_token="71001",
    trading_symbol="MIDCPNIFTY2661612000CE",
    groww_symbol="NSE-MIDCPNIFTY-16Jun26-12000-CE",
    instrument_type="CE",
    segment="FNO",
    underlying_symbol="MIDCPNIFTY",
    underlying_exchange_token="71002",
    expiry_date="2026-06-16",
    strike_price="12000",
    lot_size="120",
    tick_size="0.05",
)
# A NIFTY NEXT 50 index spot row: F&O symbol "NIFTYNXT50" (abbreviated NXT) vs
# verbose trading_symbol "NIFTY NEXT 50" — normalization cannot bridge NXT↔NEXT.
_NXT50_IDX = _row(
    exchange="NSE",
    exchange_token="NIFTYNXT50",
    trading_symbol="NIFTY NEXT 50",
    groww_symbol="NSE-NIFTYNXT50",
    name="Nifty Next 50",
    instrument_type="IDX",
    segment="CASH",
)
_NXT50_IDX_CE = _row(
    exchange="NSE",
    exchange_token="72001",
    trading_symbol="NIFTYNXT502661670000CE",
    groww_symbol="NSE-NIFTYNXT50-16Jun26-70000-CE",
    instrument_type="CE",
    segment="FNO",
    underlying_symbol="NIFTYNXT50",
    underlying_exchange_token="72002",
    expiry_date="2026-06-16",
    strike_price="70000",
    lot_size="25",
    tick_size="0.05",
)


class TestDiagnoseAliasBridgeParam:
    """The ``index_alias_symbols`` arg suppresses an underlying the bridge resolved."""

    def test_bridged_underlying_is_not_reported(self) -> None:
        # IDX symbol_name "NIFTY MID SELECT" ≠ option underlying "MIDCPNIFTY"
        # (normalization fails) → WITHOUT the alias it is a gap…
        instruments = [_idx("NIFTY MID SELECT"), _option("MIDCPNIFTY")]
        assert diagnose_unmatched_option_underlyings(instruments) != []
        # …but with the groww-symbol alias (NSE, "MIDCPNIFTY") it is bridged → no gap.
        aliases = frozenset({(Exchange.NSE, "MIDCPNIFTY")})
        assert diagnose_unmatched_option_underlyings(instruments, aliases) == []

    def test_residual_gap_still_reported_when_alias_does_not_cover(self) -> None:
        # An alias for a DIFFERENT index does not suppress an unrelated orphan.
        instruments = [_option("ORPHANIDX")]
        aliases = frozenset({(Exchange.NSE, "MIDCPNIFTY")})
        (gap,) = diagnose_unmatched_option_underlyings(instruments, aliases)
        assert gap.underlying_symbol == "ORPHANIDX"
        assert gap.nearest_index_symbol_name is None

    def test_none_default_preserves_symbol_name_only_behaviour(self) -> None:
        # Passing None (the default) is identical to omitting the arg.
        instruments = [_idx("NIFTY MID SELECT"), _option("MIDCPNIFTY")]
        assert diagnose_unmatched_option_underlyings(
            instruments, None
        ) == diagnose_unmatched_option_underlyings(instruments)


class TestGrowwSymbolIndexBridge:
    """``_groww_symbol_index_bridge`` reads the vendor groww_symbol identity."""

    def _index(self, rows: list[Instrument], groww_symbols: list[str]) -> object:
        """Build a real InstrumentIndex from explicit (groww_symbol, Instrument) pairs."""
        return build_index(zip(groww_symbols, rows, strict=True))

    def test_verbose_index_is_bridged_into_options_eligible(self) -> None:
        # The MIDCPNIFTY IDX (verbose symbol_name) + its option leg.
        midcap = _idx("NIFTY MID SELECT")
        leg = _option("MIDCPNIFTY")
        index = self._index(
            [midcap, leg],
            ["NSE-MIDCPNIFTY", "NSE-MIDCPNIFTY-16Jun26-12000-CE"],
        )
        options_eligible = build_options_eligible_underlyings(index.universe)  # type: ignore[attr-defined]
        bridged, aliases = _groww_symbol_index_bridge(index, options_eligible)  # type: ignore[arg-type]
        # The verbose IDX symbol_name is now in the bridged eligible set → its
        # unchanged is_spot_only_index symbol_name check passes (NOT spot-only).
        assert (Exchange.NSE, "NIFTY MID SELECT") in bridged
        assert is_spot_only_index(midcap, bridged) is False
        # Every IDX groww-symbol F&O alias is surfaced for the diagnostic.
        assert (Exchange.NSE, "MIDCPNIFTY") in aliases

    def test_non_idx_rows_are_ignored(self) -> None:
        # An equity + an option leg (no IDX) contribute no alias / bridge.
        index = self._index(
            [_equity("RELIANCE"), _option("RELIANCE")],
            ["NSE-RELIANCE", "NSE-RELIANCE-16Jun26-3000-CE"],
        )
        options_eligible = build_options_eligible_underlyings(index.universe)  # type: ignore[attr-defined]
        bridged, aliases = _groww_symbol_index_bridge(index, options_eligible)  # type: ignore[arg-type]
        assert bridged == options_eligible  # nothing bridged
        assert aliases == frozenset()

    def test_index_without_options_adds_alias_but_no_bridge(self) -> None:
        # A spot-only sectoral IDX (no option leg) → its alias is surfaced but it
        # is NOT bridged into the eligible set (stays spot-only).
        nifty_it = _idx("NIFTYIT")
        index = self._index([nifty_it], ["NSE-NIFTYIT"])
        bridged, aliases = _groww_symbol_index_bridge(index, frozenset())  # type: ignore[arg-type]
        assert bridged == frozenset()
        assert aliases == frozenset({(Exchange.NSE, "NIFTYIT")})

    def test_dashless_groww_symbol_is_skipped(self) -> None:
        # Defensive: a malformed groww_symbol with no "-" yields an empty F&O
        # symbol → skipped (no alias, no bridge), never raises.
        index = self._index([_idx("WEIRD")], ["WEIRD"])
        bridged, aliases = _groww_symbol_index_bridge(index, frozenset())  # type: ignore[arg-type]
        assert bridged == frozenset()
        assert aliases == frozenset()


class TestLoaderBridgesVerboseIndex:
    """End-to-end: a verbose-named F&O index keeps its OPT badge + is not a gap."""

    def test_midcpnifty_and_niftynxt50_are_pullable_and_silent(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        rows = [_NIFTY, _MIDCP_IDX, _MIDCP_IDX_CE, _NXT50_IDX, _NXT50_IDX_CE]
        with capture_logs() as logs:
            load_instrument_master(
                store,
                http_get=lambda _url: _csv(rows),
                run_id_factory=lambda: "bridge-load",
                constituent_opener=_empty_opener,
                constituent_cache_dir=tmp_path / "cache",
            )
        refs = store.reference_only_security_ids()
        midcap = store.get("NIFTY MID SELECT")
        nxt50 = store.get("NIFTY NEXT 50")
        assert midcap is not None and nxt50 is not None
        # Both verbose-named F&O indices keep their OPT badge (NOT reference_only).
        assert midcap.security_id not in refs
        assert nxt50.security_id not in refs
        # The diagnostic is SILENT — the bridge resolved both, no residual gap.
        assert _events(logs, "universe.load.fno_index_unmatched") == []

    def test_residual_unbridged_index_still_logged(self, tmp_path: Path) -> None:
        # An orphan option underlying with NO IDX spot row at all is NOT
        # bridgeable → the diagnostic still reports it (retains its value).
        store = _store(tmp_path)
        orphan_ce = _row(
            exchange="NSE",
            exchange_token="73001",
            trading_symbol="ORPHANIDX2661650000CE",
            groww_symbol="NSE-ORPHANIDX-16Jun26-50000-CE",
            instrument_type="CE",
            segment="FNO",
            underlying_symbol="ORPHANIDX",
            underlying_exchange_token="73002",
            expiry_date="2026-06-16",
            strike_price="50000",
            lot_size="40",
            tick_size="0.05",
        )
        rows = [_NIFTY, _MIDCP_IDX, _MIDCP_IDX_CE, orphan_ce]
        with capture_logs() as logs:
            load_instrument_master(
                store,
                http_get=lambda _url: _csv(rows),
                run_id_factory=lambda: "residual-load",
                constituent_opener=_empty_opener,
                constituent_cache_dir=tmp_path / "cache",
            )
        (line,) = _events(logs, "universe.load.fno_index_unmatched")
        # MIDCPNIFTY bridged (not reported); the orphan remains a gap.
        assert line["count"] == 1
        assert line["gaps"] == [
            {"underlying": "ORPHANIDX", "nearest_index": None, "exchange": "NSE"},
        ]


# ════════════════════════════════════════════════════════════════════════
# DEC-FNO-INDEX-ALIAS-001 (#5) — comprehensive OPT/REF classification
# ════════════════════════════════════════════════════════════════════════
#
# Synthetic 21-col rows / Instruments mirroring the REAL schema (no live master is
# reachable in CI). Live behaviour is operator-verified via ``make run``; these tests
# pin the three bridges (token-join 5a, alias 5b, symbol-bridge) + the audit (5c).


def _eq_with_token(
    symbol: str,
    *,
    security_id: int,
    feed_token: str | None,
    exchange: Exchange = Exchange.NSE,
) -> Instrument:
    """Build a cash-equity Instrument with an explicit ``security_id`` + ``feed_token``."""
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


def _idx_with_token(
    symbol: str,
    *,
    security_id: int,
    feed_token: str | None,
    exchange: Exchange = Exchange.NSE,
) -> Instrument:
    """Build an IDX Instrument with an explicit ``security_id`` + ``feed_token``."""
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


def _option_with_underlying_id(
    underlying: str,
    *,
    underlying_security_id: int | None,
    exchange: Exchange = Exchange.NSE,
    option_type: InstrumentType = InstrumentType.CE,
) -> Instrument:
    """Build a CE/PE leg carrying an explicit numeric ``underlying_security_id``."""
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


class TestHasGenuineNumericToken:
    """A numeric ``feed_token`` ⇒ real vendor token; non-numeric / None ⇒ surrogate."""

    def test_numeric_feed_token_is_genuine(self) -> None:
        inst = _eq_with_token("RELIANCE", security_id=2885, feed_token="2885")
        assert _has_genuine_numeric_token(inst) is True

    def test_non_numeric_feed_token_is_surrogate(self) -> None:
        # An NSE index carries a STRING token (e.g. "NIFTYMIDSELECT") → surrogate.
        inst = _idx_with_token("NIFTYMIDSELECT", security_id=999, feed_token="NIFTYMIDSELECT")
        assert _has_genuine_numeric_token(inst) is False

    def test_missing_feed_token_is_surrogate(self) -> None:
        inst = _eq_with_token("RELIANCE", security_id=2885, feed_token=None)
        assert _has_genuine_numeric_token(inst) is False


class TestOptionUnderlyingSecurityIds:
    """The set of numeric ``underlying_security_id`` named by NSE option legs."""

    def test_collects_nse_option_underlying_ids(self) -> None:
        index = build_index(
            [
                (
                    "NSE-RELIANCE-16Jun26-3000-CE",
                    _option_with_underlying_id("RELIANCE", underlying_security_id=2885),
                ),
                (
                    "NSE-TCS-16Jun26-4000-PE",
                    _option_with_underlying_id(
                        "TCS", underlying_security_id=11536, option_type=InstrumentType.PE
                    ),
                ),
            ]
        )
        assert _option_underlying_security_ids(index) == frozenset({2885, 11536})

    def test_skips_bse_legs_and_none_ids(self) -> None:
        index = build_index(
            [
                (
                    "BSE-X-16Jun26-1-CE",
                    _option_with_underlying_id(
                        "X", underlying_security_id=7, exchange=Exchange.BSE
                    ),
                ),
                (
                    "NSE-Y-16Jun26-1-CE",
                    _option_with_underlying_id("Y", underlying_security_id=None),
                ),
                ("NSE-Z-16Jun26-1-CE", _option_with_underlying_id("Z", underlying_security_id=42)),
            ]
        )
        # BSE leg excluded; the None-id NSE leg excluded; only Z's id survives.
        assert _option_underlying_security_ids(index) == frozenset({42})


_SOURCED_DEFAULTS = {"NIFTYJR": "NIFTYNXT50", "NIFTYMIDSELECT": "MIDCPNIFTY"}


class TestResolveFnoIndexAliases:
    """Sourced defaults MERGED with the defensively-parsed env override."""

    def test_defaults_when_env_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(FNO_INDEX_ALIASES_OVERRIDE_ENV, raising=False)
        assert _resolve_fno_index_aliases() == {
            "NIFTYJR": "NIFTYNXT50",
            "NIFTYMIDSELECT": "MIDCPNIFTY",
        }

    def test_env_override_merges_on_top(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(FNO_INDEX_ALIASES_OVERRIDE_ENV, '{"newidx": "NEWFNO"}')
        aliases = _resolve_fno_index_aliases()
        assert aliases["NEWIDX"] == "NEWFNO"  # key upper-cased + trimmed
        assert aliases["NIFTYJR"] == "NIFTYNXT50"  # sourced default preserved

    def test_env_override_wins_over_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(FNO_INDEX_ALIASES_OVERRIDE_ENV, '{"NIFTYJR": "OVERRIDDEN"}')
        assert _resolve_fno_index_aliases()["NIFTYJR"] == "OVERRIDDEN"

    def test_blank_env_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(FNO_INDEX_ALIASES_OVERRIDE_ENV, "")
        assert _resolve_fno_index_aliases() == dict(_SOURCED_DEFAULTS)

    def test_invalid_json_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(FNO_INDEX_ALIASES_OVERRIDE_ENV, "{not json")
        assert _resolve_fno_index_aliases() == dict(_SOURCED_DEFAULTS)

    def test_non_object_json_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(FNO_INDEX_ALIASES_OVERRIDE_ENV, '["not", "an", "object"]')
        assert _resolve_fno_index_aliases() == dict(_SOURCED_DEFAULTS)

    def test_non_str_and_blank_pairs_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # int value, blank value, and blank key are each dropped; the good pair stays.
        monkeypatch.setenv(
            FNO_INDEX_ALIASES_OVERRIDE_ENV,
            '{"GOODIDX": "GOODFNO", "BADINT": 5, "BLANKVAL": "  ", "  ": "X"}',
        )
        aliases = _resolve_fno_index_aliases()
        assert aliases["GOODIDX"] == "GOODFNO"
        assert "BADINT" not in aliases
        assert "BLANKVAL" not in aliases
        assert "" not in aliases


class TestClassifyOptionPullable:
    """The comprehensive fold: token-join + alias + symbol-bridge + the audit."""

    def test_eq_token_join_surfaces_diagnostic_and_persist_id(self) -> None:
        # A stock whose own symbol does NOT name its option leg's underlying_symbol
        # (spelling drift, e.g. BAJAJ-AUTO vs BAJAJAUTO), but whose GENUINE numeric
        # vendor token equals the leg's underlying_security_id. The EQ token-join does
        # NOT touch the INDEX-only bridged set (is_spot_only_index is False for non-IDX)
        # nor the index reclassification buckets — it is STILL surfaced in
        # token_join_diagnostic for the operator audit. But its security_id is now
        # returned in the 4th element (the PERSIST set), which is_universe_underlying
        # uses to keep the stock as a TRADABLE underlying
        # (DEC-FNO-EQ-TOKEN-JOIN-PERSIST-001, the follow-up to the audit-only split).
        stock = _eq_with_token("BAJAJ-AUTO", security_id=16669, feed_token="16669")
        leg = _option_with_underlying_id("BAJAJAUTO", underlying_security_id=16669)
        index = build_index([("NSE-BAJAJ-AUTO", stock), ("NSE-BAJAJAUTO-16Jun26-9000-CE", leg)])
        options_eligible = build_options_eligible_underlyings(index.universe)
        bridged, _aliases, rescued, eq_ids = _classify_option_pullable(index, options_eligible)
        # Index classification UNCHANGED: the EQ pair is NOT in the (index-only) bridged
        # set, the token_join bucket stays empty — only the honest diagnostic carries it.
        assert (Exchange.NSE, "BAJAJ-AUTO") not in bridged
        assert bridged == options_eligible
        assert rescued["token_join"] == []
        assert rescued["token_join_diagnostic"] == ["NSE:BAJAJ-AUTO"]
        assert rescued["alias"] == []
        assert rescued["symbol_bridge"] == []
        # NEW: the symbol-mismatched F&O stock's security_id is in the persist set.
        assert eq_ids == frozenset({16669})

    def test_bse_equity_token_collision_excluded_from_persist_set(self) -> None:
        # Defense-in-depth (DEC-FNO-EQ-TOKEN-JOIN-PERSIST-001): a BSE EQ whose numeric
        # token coincidentally equals an NSE option leg's underlying_security_id is NOT an
        # F&O stock (no BSE derivatives in scope), so the producer's NSE gate must EXCLUDE
        # it from BOTH the persist set and the diagnostic (the downstream
        # is_universe_underlying NSE gate would also reject it).
        bse = _eq_with_token("FOO", security_id=16669, feed_token="16669", exchange=Exchange.BSE)
        leg = _option_with_underlying_id("FOO", underlying_security_id=16669)  # an NSE leg
        index = build_index([("BSE-FOO", bse), ("NSE-FOO-16Jun26-9000-CE", leg)])
        options_eligible = build_options_eligible_underlyings(index.universe)
        bridged, _aliases, rescued, eq_ids = _classify_option_pullable(index, options_eligible)
        assert eq_ids == frozenset()  # the BSE collision is NOT persisted
        assert rescued["token_join_diagnostic"] == []  # nor surfaced
        assert (Exchange.BSE, "FOO") not in bridged

    def test_idx_token_join_is_a_real_rescue(self) -> None:
        # An IDX whose own GENUINE numeric vendor token matches an NSE option leg's
        # underlying_security_id IS a real rescue: is_spot_only_index reads the bridged
        # set for IDX rows, so the token-join flips it REF→OPT. It lands in the real
        # token_join bucket AND the bridged set — distinct from the EQ diagnostic path
        # (DEC-FNO-INDEX-EQ-TOKEN-JOIN-AUDIT-001). (NSE legs are the token-join evidence
        # source, so the IDX carries a numeric feed_token to be genuinely token-joinable.)
        idx = _idx_with_token("VERBOSEIDX", security_id=51, feed_token="51")
        leg = _option_with_underlying_id("COMPACTIDX", underlying_security_id=51)
        index = build_index([("NSE-VERBOSEIDX", idx), ("NSE-COMPACTIDX-16Jun26-1-CE", leg)])
        options_eligible = build_options_eligible_underlyings(index.universe)
        bridged, _aliases, rescued, _eq_ids = _classify_option_pullable(index, options_eligible)
        assert (Exchange.NSE, "VERBOSEIDX") in bridged
        assert rescued["token_join"] == ["NSE:VERBOSEIDX"]
        assert rescued["token_join_diagnostic"] == []

    def test_surrogate_token_index_is_not_token_joined(self) -> None:
        # An NSE index with a STRING feed_token (surrogate security_id) must NOT
        # spuriously token-join even if its surrogate id collides with a leg id.
        idx = _idx_with_token("NIFTY MIDCAP 100", security_id=42, feed_token="NIFTYMIDCAP100")
        leg = _option_with_underlying_id("SOMETHING", underlying_security_id=42)
        index = build_index([("NSE-NIFTYMIDCAP100", idx), ("NSE-SOMETHING-16Jun26-1-CE", leg)])
        options_eligible = build_options_eligible_underlyings(index.universe)
        bridged, _aliases, rescued, _eq_ids = _classify_option_pullable(index, options_eligible)
        # NOT token-joined (surrogate token) → stays spot-only (REF).
        assert (Exchange.NSE, "NIFTY MIDCAP 100") not in bridged
        assert rescued["token_join"] == []

    def test_alias_rescues_niftyjr_and_niftymidselect(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The 2 real NSE indices: spot trading_symbol NIFTYJR / NIFTYMIDSELECT, whose
        # OWN groww_symbol carries the spot spelling (so the symbol bridge misses),
        # but whose ALIASED F&O symbol (NIFTYNXT50 / MIDCPNIFTY) names the option leg.
        monkeypatch.delenv(FNO_INDEX_ALIASES_OVERRIDE_ENV, raising=False)
        jr = _idx_with_token("NIFTYJR", security_id=101, feed_token="NIFTYJR")
        jr_leg = _option_with_underlying_id("NIFTYNXT50", underlying_security_id=26013)
        mid = _idx_with_token("NIFTYMIDSELECT", security_id=102, feed_token="NIFTYMIDSELECT")
        mid_leg = _option_with_underlying_id("MIDCPNIFTY", underlying_security_id=26074)
        index = build_index(
            [
                ("NSE-NIFTYJR", jr),
                ("NSE-NIFTYNXT50-16Jun26-70000-CE", jr_leg),
                ("NSE-NIFTYMIDSELECT", mid),
                ("NSE-MIDCPNIFTY-16Jun26-12000-CE", mid_leg),
            ]
        )
        options_eligible = build_options_eligible_underlyings(index.universe)
        bridged, aliases, rescued, _eq_ids = _classify_option_pullable(index, options_eligible)
        assert (Exchange.NSE, "NIFTYJR") in bridged
        assert (Exchange.NSE, "NIFTYMIDSELECT") in bridged
        assert rescued["alias"] == ["NSE:NIFTYJR", "NSE:NIFTYMIDSELECT"]
        # The aliased F&O symbols are surfaced so the diagnostic stops reporting them.
        assert (Exchange.NSE, "NIFTYNXT50") in aliases
        assert (Exchange.NSE, "MIDCPNIFTY") in aliases
        # And the residual gap diagnostic now reports ZERO gaps for the two.
        assert diagnose_unmatched_option_underlyings(index.universe, aliases) == []

    def test_symbol_bridge_rescues_verbose_groww_symbol_index(self) -> None:
        # The existing groww_symbol bridge: an IDX whose verbose trading_symbol
        # differs from the leg, but whose groww_symbol F&O component matches.
        midcap = _idx_with_token("NIFTY MID SELECT", security_id=200, feed_token="MIDCPNIFTY")
        leg = _option_with_underlying_id("MIDCPNIFTY", underlying_security_id=300)
        index = build_index([("NSE-MIDCPNIFTY", midcap), ("NSE-MIDCPNIFTY-16Jun26-12000-CE", leg)])
        options_eligible = build_options_eligible_underlyings(index.universe)
        bridged, _aliases, rescued, _eq_ids = _classify_option_pullable(index, options_eligible)
        assert (Exchange.NSE, "NIFTY MID SELECT") in bridged
        assert rescued["symbol_bridge"] == ["NSE:NIFTY MID SELECT"]
        assert rescued["alias"] == []

    def test_direct_symbol_match_is_not_a_rescue(self) -> None:
        # NIFTY's own symbol_name IS the option underlying_symbol → already OPT, not
        # counted as rescued; nothing else fires for it.
        nifty = _idx_with_token("NIFTY", security_id=13, feed_token="NIFTY")
        leg = _option_with_underlying_id("NIFTY", underlying_security_id=13)
        index = build_index([("NSE-NIFTY", nifty), ("NSE-NIFTY-16Jun26-25000-CE", leg)])
        options_eligible = build_options_eligible_underlyings(index.universe)
        _bridged, _aliases, rescued, _eq_ids = _classify_option_pullable(index, options_eligible)
        assert rescued == {
            "token_join": [],
            "alias": [],
            "symbol_bridge": [],
            "token_join_diagnostic": [],
        }

    def test_spot_only_index_is_not_rescued(self) -> None:
        # A genuine spot-only index (no option legs at all) is rescued by NONE of
        # the three mechanisms → stays REF.
        sectoral = _idx_with_token("NIFTY MIDCAP 100", security_id=55, feed_token="NIFTYMIDCAP100")
        index = build_index([("NSE-NIFTYMIDCAP100", sectoral)])
        options_eligible = build_options_eligible_underlyings(index.universe)
        bridged, _aliases, rescued, _eq_ids = _classify_option_pullable(index, options_eligible)
        assert bridged == options_eligible
        assert rescued == {
            "token_join": [],
            "alias": [],
            "symbol_bridge": [],
            "token_join_diagnostic": [],
        }

    def test_derivative_legs_are_not_rescued(self) -> None:
        # Only EQ / IDX spot rows are candidates; the option leg row itself is
        # skipped by the instrument-type guard (covers the continue arm).
        nifty = _idx_with_token("NIFTY", security_id=13, feed_token="NIFTY")
        leg = _option_with_underlying_id("NIFTY", underlying_security_id=13)
        index = build_index([("NSE-NIFTY", nifty), ("NSE-NIFTY-16Jun26-25000-CE", leg)])
        options_eligible = build_options_eligible_underlyings(index.universe)
        _bridged, _aliases, rescued, _eq_ids = _classify_option_pullable(index, options_eligible)
        # The CE leg contributed no rescue entry (it is not EQ/IDX).
        assert "NSE:NIFTY-OPT" not in rescued["token_join"]
