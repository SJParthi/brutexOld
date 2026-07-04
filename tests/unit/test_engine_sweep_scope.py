"""Unit tests — ``brutex.engine.sweep_scope`` (DEC-ENGINE-SCOPE-GUARD-001 Layer 2).

The pure coordinate-law preflight every production sweep mouth runs BEFORE any
lake I/O. Classification itself is the ONE canonical
:func:`brutex.universe.classify_engine_sweep_target` (tested in
``tests/unit/test_universe.py``); here we pin the report semantics — ok /
refused preservation / override / message content.
"""

from __future__ import annotations

from brutex.engine.sweep_scope import ScopeReport, SymbolVerdict, check_sweep_scope

_SPOT_NSE = {"exchange": "NSE", "segment": "INDEX"}


class TestCheckSweepScope:
    def test_all_in_scope_is_ok_with_no_refusals(self) -> None:
        report = check_sweep_scope(["NIFTY", "BANKNIFTY"], allow_out_of_scope=False, **_SPOT_NSE)
        assert report.ok is True
        assert report.refused == ()
        assert report.overridden is False

    def test_sensex_on_bse_index_is_ok(self) -> None:
        report = check_sweep_scope(
            ["SENSEX"], exchange="BSE", segment="INDEX", allow_out_of_scope=False
        )
        assert report.ok is True
        assert report.refused == ()

    def test_out_of_scope_symbol_refuses(self) -> None:
        report = check_sweep_scope(["NIFTY", "RELIANCE"], allow_out_of_scope=False, **_SPOT_NSE)
        assert report.ok is False
        assert report.overridden is False
        assert [v.symbol for v in report.refused] == ["RELIANCE"]
        assert "engine scope" in report.refused[0].reason

    def test_reference_only_vix_refuses_with_specific_reason(self) -> None:
        report = check_sweep_scope(["INDIAVIX"], allow_out_of_scope=False, **_SPOT_NSE)
        assert report.ok is False
        assert "REFERENCE_ONLY" in report.refused[0].reason

    def test_override_flips_ok_but_preserves_refused(self) -> None:
        report = check_sweep_scope(["INDIAVIX"], allow_out_of_scope=True, **_SPOT_NSE)
        assert report.ok is True
        assert report.overridden is True
        assert [v.symbol for v in report.refused] == ["INDIAVIX"]

    def test_override_with_all_in_scope_is_not_marked_overridden(self) -> None:
        # The flag alone never stamps an override — only actually-refused
        # symbols do (so an in-scope grid never carries the unsafe flag).
        report = check_sweep_scope(["NIFTY"], allow_out_of_scope=True, **_SPOT_NSE)
        assert report.ok is True
        assert report.refused == ()
        assert report.overridden is False

    def test_empty_symbols_is_ok(self) -> None:
        # Emptiness is the launcher's own validation concern, not the scope law's.
        report = check_sweep_scope([], allow_out_of_scope=False, **_SPOT_NSE)
        assert report.ok is True
        assert report.refused == ()

    def test_tuple_input_accepted(self) -> None:
        report = check_sweep_scope(("NIFTY",), allow_out_of_scope=False, **_SPOT_NSE)
        assert report.ok is True


class TestScopeReportMessages:
    def _refusing(self) -> ScopeReport:
        return check_sweep_scope(["INDIAVIX", "RELIANCE"], allow_out_of_scope=False, **_SPOT_NSE)

    def test_refusal_message_names_every_symbol_law_and_remedy(self) -> None:
        message = self._refusing().refusal_message()
        assert "sweep REFUSED" in message
        assert "INDIAVIX" in message
        assert "RELIANCE" in message
        assert "NSE/INDEX" in message
        assert "CLAUDE.md §2 Rule 3" in message
        assert "ADR-0012" in message
        assert "--allow-out-of-scope" in message

    def test_override_warning_names_the_symbols_and_the_audit_stamp(self) -> None:
        report = check_sweep_scope(["INDIAVIX", "RELIANCE"], allow_out_of_scope=True, **_SPOT_NSE)
        warning = report.override_warning()
        assert "WARNING" in warning
        assert "INDIAVIX, RELIANCE" in warning
        assert "scope_override=True" in warning
        assert "DEC-ENGINE-SCOPE-GUARD-001" in warning

    def test_symbol_verdict_carries_symbol_and_reason(self) -> None:
        verdict = self._refusing().refused[0]
        assert isinstance(verdict, SymbolVerdict)
        assert verdict.symbol == "INDIAVIX"
        assert verdict.reason
