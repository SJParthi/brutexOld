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

from brutex.adapters.groww.constants import INSTRUMENT_CSV_COLUMNS
from brutex.dashboard.instrument_loader import load_instrument_master
from brutex.data.instrument_store import InstrumentStore
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from brutex.universe import (
    OptionUnderlyingGap,
    diagnose_unmatched_option_underlyings,
    normalize_underlying_symbol,
)
from structlog.testing import capture_logs

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
