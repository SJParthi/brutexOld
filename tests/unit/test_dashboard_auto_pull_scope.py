"""The §5.1 auto-heal probe + trigger assembly — DEC-UNIVERSE-INDEX-ONLY-001.

The :mod:`brutex.dashboard.auto_pull` half of the index4 scope cut: the pure
SYMMETRIC ``store_scope_mismatch`` arms (narrow / widen / gdfl-no-loop), the
covered ``evaluate_master_reload_trigger`` assembly (env-resolved AND
explicit-``scope=`` arms) over a light fake store, and the
``cli._autoload_instrument_master`` call-site source pin (the #1645 idiom).

HOMED in this stem-mapped file (``test_dashboard_auto_pull*.py``) so the
per-module discovery coverage gate (``scripts/ci/coverage_gate_discovery.py``)
measures the new §5.1 code under ``brutex/dashboard/auto_pull.py``'s OWN
mapped suite — the loader-chain SYSTEM tests (probe → reload → converged
store) stay in ``tests/unit/test_instrument_loader_scope.py``, the
instrument-loader stem.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.config.feed_scope import (
    FEED_SCOPE_ENV,
    INDEX4_UNDERLYINGS,
    SCOPE_FULL,
    FeedScope,
    _scope_for_token,
    resolve_feed_scope,
)
from brutex.dashboard.auto_pull import (
    evaluate_master_reload_trigger,
    should_reload_instrument_master,
    store_scope_mismatch,
)
from brutex.models import Exchange, Instrument, InstrumentType, Segment


def _index4() -> FeedScope:
    return resolve_feed_scope("groww")


def _inst(
    symbol: str,
    *,
    itype: InstrumentType = InstrumentType.IDX,
    exchange: Exchange = Exchange.NSE,
) -> Instrument:
    """A minimal IDX/EQ instrument row for the trigger-assembly fake store."""
    return Instrument(
        exch_id=exchange,
        security_id=abs(hash((exchange, symbol))) % 10_000_000,
        symbol_name=symbol,
        display_name=symbol,
        instrument=itype,
        segment=Segment.INDEX if itype is InstrumentType.IDX else Segment.CASH,
        series=None,
        isin=None,
        underlying_symbol=None,
        underlying_security_id=None,
        lot_size=1,
        tick_size=Decimal("0.05"),
        sm_freeze_qty=None,
        sm_expiry_date=None,
        strike_price=None,
    )


def _admitted_four() -> list[Instrument]:
    return [
        _inst("NIFTY"),
        _inst("BANKNIFTY"),
        _inst("SENSEX", exchange=Exchange.BSE),
        _inst("INDIAVIX"),
    ]


@dataclass
class _FakeStore:
    """The 5-method store surface ``evaluate_master_reload_trigger`` reads."""

    instruments: list[Instrument]
    eligible: frozenset[tuple[Exchange, str]] = field(default_factory=frozenset)

    def list_index_equity_instruments(self) -> list[Instrument]:
        return list(self.instruments)

    def fno_eligible_underlyings(self) -> frozenset[tuple[Exchange, str]]:
        return self.eligible

    def has_derivative_legs(self) -> bool:
        return False

    def has_placeholder_stock_economics(self) -> bool:
        return False

    def has_stale_option_eligibility(self) -> bool:
        return False


class TestStoreScopeMismatch:
    """§5.1 — the SYMMETRIC probe, pure arms."""

    def test_narrow_arm_fires_on_eq_rows(self) -> None:
        assert store_scope_mismatch(scope=_index4(), index_rows=[], eq_row_count=1) is True

    def test_narrow_arm_fires_on_out_of_scope_idx(self) -> None:
        assert (
            store_scope_mismatch(
                scope=_index4(),
                index_rows=[(Exchange.NSE, "NIFTYIT")],
                eq_row_count=0,
            )
            is True
        )

    def test_narrow_arm_quiet_on_admitted_store(self) -> None:
        rows = list(INDEX4_UNDERLYINGS)
        assert store_scope_mismatch(scope=_index4(), index_rows=rows, eq_row_count=0) is False

    def test_widen_arm_fires_on_zero_eq_under_full(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(FEED_SCOPE_ENV, "full")
        full = resolve_feed_scope("groww")
        assert (
            store_scope_mismatch(scope=full, index_rows=list(INDEX4_UNDERLYINGS), eq_row_count=0)
            is True
        )
        # A populated wide store reads False — ONE reload, no loop.
        assert (
            store_scope_mismatch(scope=full, index_rows=list(INDEX4_UNDERLYINGS), eq_row_count=212)
            is False
        )

    def test_gdfl_no_loop_pin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """§6#11 — a wide NON-groww binding can NEVER arm the widen arm: the

        probe keys on the LOADER's OWN feed scope (groww, still index4), so a
        4-row store reads False — zero reloads, no per-boot loop."""
        monkeypatch.setenv(FEED_SCOPE_ENV, "gdfl=full")
        loader_scope = resolve_feed_scope("groww")
        assert loader_scope.bounded()
        assert (
            store_scope_mismatch(
                scope=loader_scope, index_rows=list(INDEX4_UNDERLYINGS), eq_row_count=0
            )
            is False
        )

    def test_scope_mismatch_flag_reaches_verdict(self) -> None:
        assert (
            should_reload_instrument_master(index_count=4, stock_count=0, scope_mismatch=True)
            is True
        )
        assert (
            should_reload_instrument_master(index_count=4, stock_count=0, scope_mismatch=False)
            is False
        )


class TestEvaluateMasterReloadTrigger:
    """§5.1 — the covered trigger assembly over a light fake store."""

    def test_admitted_four_row_store_reads_false(self) -> None:
        """A converged 4-row store under the shipped env default: no reload."""
        should, summary = evaluate_master_reload_trigger(_FakeStore(_admitted_four()))
        assert should is False
        assert summary["index_count"] == 4
        assert summary["stock_count"] == 0

    def test_stale_wide_store_fires_narrow_arm(self) -> None:
        """A stale EQ row over the index4 default arms the narrow probe —
        while the EQ row itself classifies ``None`` under the scope-aware
        classifier (honest summary counts, never a phantom stock)."""
        store = _FakeStore(
            [*_admitted_four(), _inst("RELIANCE", itype=InstrumentType.EQ)],
            eligible=frozenset({(Exchange.NSE, "RELIANCE")}),
        )
        should, summary = evaluate_master_reload_trigger(store)
        assert should is True
        assert summary["stock_count"] == 0

    def test_explicit_scope_kwarg_bypasses_env_resolution(self) -> None:
        """The ``scope is None`` False arm: an EXPLICIT ``scope=`` is honored
        verbatim — the SAME 4-row zero-EQ store reads False under the env
        default (index4 — narrow arm quiet) yet True under an injected
        ``full`` scope (widen arm fires), so a dropped kwarg cannot ship."""
        store = _FakeStore(_admitted_four())
        assert evaluate_master_reload_trigger(store)[0] is False
        full = _scope_for_token("groww", SCOPE_FULL)
        assert evaluate_master_reload_trigger(store, scope=full)[0] is True


class TestCliAutoloadWiringPin:
    """§5.1 — the call-site source pin (the #1645 idiom): the pragma-no-cover

    daemon body must consume the COVERED trigger helper, never a hand-built
    kwargs assembly a dropped kwarg could silently ship through."""

    def test_autoload_daemon_uses_covered_trigger_helper(self) -> None:
        source = (Path(__file__).resolve().parents[2] / "brutex" / "cli.py").read_text(
            encoding="utf-8"
        )
        start = source.index("def _autoload_instrument_master(")
        end = source.index("def _start_instrument_autoload(")
        body = source[start:end]
        assert "evaluate_master_reload_trigger(store)" in body
        assert "should_reload_instrument_master(" not in body  # no hand assembly
        assert "load_instrument_master(store)" in body
        # The §5.6 divergence visibility: the autoload log names the scope.
        assert 'scope=counts.get("scope")' in body
