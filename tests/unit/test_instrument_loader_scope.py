"""The index4-default loader + auto-heal CHAIN arms — DEC-UNIVERSE-INDEX-ONLY-001.

The shipped-default (Law-1) guard arms for the A5/A6 loader narrowing over a
REAL master fixture (exactly-4-rows, NTM dormancy with ZERO network, the
report scope keys), the §5.1 auto-heal SYSTEM chain (probe → reload →
converged store — loop-freedom both directions incl. env-divergence over a
REAL store), and the P1 pull-selection narrowing. The PURE
``store_scope_mismatch`` arms + the ``evaluate_master_reload_trigger``
assembly + the cli wiring pin live in the auto_pull stem suite
(``tests/unit/test_dashboard_auto_pull_scope.py`` — the discovery-gate
mapping law). Fails-before proofs executed manually against the merge-base
tree (recorded in the PR body); these are the permanent net.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("duckdb", reason="the loader chain needs the data extras")

from brutex.adapters.groww.constants import INSTRUMENT_CSV_COLUMNS
from brutex.config.feed_scope import (
    FEED_SCOPE_ENV,
    INDEX4_UNDERLYINGS,
    SCOPE_FULL,
    SCOPE_INDEX4,
)
from brutex.dashboard.auto_pull import evaluate_master_reload_trigger
from brutex.dashboard.instrument_loader import load_instrument_master
from brutex.dashboard.universe_pull import select_pull_instruments
from brutex.data.instrument_store import InstrumentStore

_HEADER = ",".join(INSTRUMENT_CSV_COLUMNS)


def _row(**over: str) -> str:
    cells = dict.fromkeys(INSTRUMENT_CSV_COLUMNS, "")
    cells.update(over)
    return ",".join(cells[col] for col in INSTRUMENT_CSV_COLUMNS)


def _idx_row(symbol: str, exchange: str = "NSE") -> str:
    return _row(
        exchange=exchange,
        exchange_token=symbol,
        trading_symbol=symbol,
        groww_symbol=f"{exchange}-{symbol}",
        name=symbol,
        instrument_type="IDX",
        segment="CASH",
    )


def _eq_row(symbol: str, token: str, isin: str) -> str:
    return _row(
        exchange="NSE",
        exchange_token=token,
        trading_symbol=symbol,
        groww_symbol=f"NSE-{symbol}",
        name=symbol,
        instrument_type="EQ",
        segment="CASH",
        series="EQ",
        isin=isin,
        lot_size="1",
        tick_size="0.05",
    )


def _ce_row(
    underlying: str,
    underlying_token: str,
    token: str,
    *,
    lot_size: str = "65",
    freeze_quantity: str = "",
) -> str:
    return _row(
        exchange="NSE",
        exchange_token=token,
        trading_symbol=f"{underlying}2661620000CE",
        groww_symbol=f"NSE-{underlying}-16Jun26-20000-CE",
        instrument_type="CE",
        segment="FNO",
        underlying_symbol=underlying,
        underlying_exchange_token=underlying_token,
        expiry_date="2026-06-16",
        strike_price="20000",
        lot_size=lot_size,
        tick_size="0.05",
        freeze_quantity=freeze_quantity,
    )


#: The pre-cut-shaped master: the 4 V1 indices + a sectoral index + an
#: F&O-eligible stock (WIPRO, made eligible by its CE leg) + the leg — plus a
#: NIFTY leg (r4 item-4 pin) so the index4-default arm genuinely exercises the
#: leg-economics fold over an ADMITTED underlying (a non-empty intersection).
_MASTER_ROWS = [
    _idx_row("NIFTY"),
    _idx_row("BANKNIFTY"),
    _idx_row("SENSEX", "BSE"),
    _idx_row("INDIAVIX"),
    _idx_row("NIFTYIT"),
    _eq_row("WIPRO", "3787", "INE075A01022"),
    _ce_row("WIPRO", "3787", "60007"),
    # A numeric underlying token (the parser refuses non-numeric leg tokens);
    # the fold keys on (exchange, underlying_symbol), never the token.
    _ce_row("NIFTY", "13", "60009", lot_size="75", freeze_quantity="1800"),
]


def _csv() -> str:
    return "\n".join([_HEADER, *_MASTER_ROWS]) + "\n"


def _must_not_fetch(_url: str) -> str:
    raise AssertionError("the NTM constituent fetch must be DORMANT under index4")


def _store(tmp_path: Path) -> InstrumentStore:
    return InstrumentStore.open(tmp_path / "instruments.duckdb")


def _load(store: InstrumentStore, tmp_path: Path) -> dict[str, object]:
    return load_instrument_master(
        store,
        http_get=lambda _url: _csv(),
        run_id_factory=lambda: "scope-load",
        constituent_opener=_must_not_fetch,
        constituent_cache_dir=tmp_path / "cache",
    )


class TestIndex4LoaderDefault:
    """A5/A6 under the shipped default (fails-before: merge-base persists 6)."""

    def test_writes_exactly_four_rows(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        result = _load(store, tmp_path)
        assert result["persisted"] == 4
        stored = {
            (inst.exch_id, inst.symbol_name) for inst in store.list_index_equity_instruments()
        }
        assert stored == INDEX4_UNDERLYINGS
        assert result["index_count"] == 4
        assert result["stock_count"] == 0

    def test_ntm_admission_dormant_zero_network(self, tmp_path: Path) -> None:
        """A5 — the raising opener proves ZERO NTM network under index4."""
        store = _store(tmp_path)
        result = _load(store, tmp_path)
        ntm = result["ntm"]
        assert isinstance(ntm, dict)
        assert ntm["admitted"] is False
        assert "DEC-UNIVERSE-INDEX-ONLY-001" in ntm["reason"]
        assert "dormant" in ntm["reason"]

    def test_report_scope_keys(self, tmp_path: Path) -> None:
        """A6 — the acting scope token + the emit-on-nonzero exclusion counts."""
        store = _store(tmp_path)
        result = _load(store, tmp_path)
        assert result["scope"] == SCOPE_INDEX4
        # NIFTYIT (legacy-admissible IDX) + WIPRO (legacy-admissible EQ).
        assert result["scope_excluded_idx"] == 1
        assert result["scope_excluded_eq"] == 1

    def test_full_arm_report_omits_exclusion_keys(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Emit-on-nonzero: the full arm carries NO scope_excluded_* keys."""
        monkeypatch.setenv(FEED_SCOPE_ENV, "full")
        store = _store(tmp_path)
        result = load_instrument_master(
            store,
            http_get=lambda _url: _csv(),
            constituent_opener=lambda _url: "",
            constituent_cache_dir=tmp_path / "cache",
        )
        assert result["scope"] == SCOPE_FULL
        assert "scope_excluded_idx" not in result
        assert "scope_excluded_eq" not in result
        # The widen half: legacy admission persists the sectoral + the stock.
        assert result["persisted"] == 6

    def test_vix_row_survives_reference_only(self, tmp_path: Path) -> None:
        """Directive E pin (i): INDIAVIX persists, tagged reference-only."""
        store = _store(tmp_path)
        _load(store, tmp_path)
        vix = [
            inst for inst in store.list_index_equity_instruments() if inst.symbol_name == "INDIAVIX"
        ]
        assert len(vix) == 1
        assert vix[0].security_id in store.reference_only_security_ids()

    def test_index_fno_rows_stay_pullable(self, tmp_path: Path) -> None:
        """Directive E pin (ii): the 3 F&O indices stay NON-reference (pullable)."""
        store = _store(tmp_path)
        _load(store, tmp_path)
        reference_ids = store.reference_only_security_ids()
        by_symbol = {inst.symbol_name: inst for inst in store.list_index_equity_instruments()}
        for symbol in ("NIFTY", "BANKNIFTY", "SENSEX"):
            assert by_symbol[symbol].security_id not in reference_ids, symbol

    def test_admitted_index_leg_economics_folded(self, tmp_path: Path) -> None:
        """The r4 item-4 pin — the index4-default FOLD COMPOSITION.

        The loader harvests ``build_underlying_economics`` from the UNFILTERED
        ``index.universe`` leg set and folds it onto the scope-narrowed lean
        rows, so the admitted NIFTY row persists its leg lot + freeze under
        the shipped default. A scope-motivated refactor that filters the leg
        set BEFORE the harvest (the natural-looking "why build economics for
        rows we drop" cleanup) would silently zero these while every other
        arm stays green — ``has_placeholder_stock_economics`` checks STOCKS
        only, so no auto-heal would fire (the silent-wrong-data class).
        """
        store = _store(tmp_path)
        _load(store, tmp_path)
        nifty = next(
            inst for inst in store.list_index_equity_instruments() if inst.symbol_name == "NIFTY"
        )
        assert nifty.lot_size == 75  # the leg's tradable lot, never the IDX placeholder 0
        assert nifty.sm_freeze_qty == 1800

    def test_p1_stock_selection_empty(self, tmp_path: Path) -> None:
        """P1 [FB]: select_pull_instruments("stock") == [] over the loaded store."""
        store = _store(tmp_path)
        _load(store, tmp_path)
        snapshot = store.list_index_equity_instruments()
        eligible = store.fno_eligible_underlyings()
        assert select_pull_instruments(snapshot, kind="stock", fno_eligible=eligible) == []
        indices = select_pull_instruments(snapshot, kind="index", fno_eligible=eligible)
        assert {inst.symbol_name for inst in indices} == {
            "NIFTY",
            "BANKNIFTY",
            "SENSEX",
            "INDIAVIX",
        }


class TestAutoHealChain:
    """§6#11 — the SYSTEM chain: probe → reload → converged store → False."""

    def test_narrow_chain_converges(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A stale wide store fires ONE narrowing reload; the pruned store is quiet."""
        # Seed a WIDE (pre-cut-shaped) store under a full env...
        monkeypatch.setenv(FEED_SCOPE_ENV, "full")
        store = _store(tmp_path)
        result = load_instrument_master(
            store,
            http_get=lambda _url: _csv(),
            constituent_opener=lambda _url: "",
            constituent_cache_dir=tmp_path / "cache",
        )
        assert result["persisted"] == 6
        # ... then boot NARROW (the §5.1(d) env-divergence direction — the
        # documented per-boot reversion, log-visible via scope=<token>).
        monkeypatch.setenv(FEED_SCOPE_ENV, "index4")
        should, _summary = evaluate_master_reload_trigger(store)
        assert should is True  # the narrow arm fires
        reload_result = _load(store, tmp_path)
        assert reload_result["persisted"] == 4
        # Converged: the probe reads False — no loop.
        should_again, _ = evaluate_master_reload_trigger(store)
        assert should_again is False

    def test_widen_chain_converges(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """env=full over a pruned 4-row store → ONE widening reload → False."""
        store = _store(tmp_path)
        _load(store, tmp_path)  # the pruned 4-row store (index4)
        monkeypatch.setenv(FEED_SCOPE_ENV, "full")
        should, _summary = evaluate_master_reload_trigger(store)
        assert should is True  # the widen arm fires
        wide_result = load_instrument_master(
            store,
            http_get=lambda _url: _csv(),
            constituent_opener=lambda _url: "",
            constituent_cache_dir=tmp_path / "cache",
        )
        assert wide_result["persisted"] == 6
        should_again, _ = evaluate_master_reload_trigger(store)
        assert should_again is False

    def test_gdfl_full_never_reloads(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The gdfl-no-loop pin over a REAL store: zero reload triggers."""
        store = _store(tmp_path)
        _load(store, tmp_path)
        monkeypatch.setenv(FEED_SCOPE_ENV, "gdfl=full")
        should, _summary = evaluate_master_reload_trigger(store)
        assert should is False
