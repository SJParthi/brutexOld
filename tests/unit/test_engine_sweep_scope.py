"""Unit tests — ``brutex.engine.sweep_scope`` (DEC-ENGINE-SCOPE-GUARD-001 Layer 2).

The pure coordinate-law preflight every production sweep mouth runs BEFORE any
lake I/O. Classification itself is the ONE canonical
:func:`brutex.universe.classify_engine_sweep_target` (tested in
``tests/unit/test_universe.py``); here we pin the report semantics — ok /
refused preservation / override / message content — plus the corporate-action
extension (DEC-CORP-ACTION-GATE-001 / ADR-0016): the hard refusal carries the
corp-action rationale + both DEC ids, and the override path emits ONE loud
``sweep.scope_override_hazard`` structlog WARNING, never a silent escape.
"""

from __future__ import annotations

import structlog
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
        # The launch pair is operator-typed → repr-quoted (round-8 finding 2).
        assert "'NSE'/'INDEX'" in message
        assert "CLAUDE.md §2 Rule 3" in message
        assert "ADR-0012" in message
        assert "--allow-out-of-scope" in message

    def test_override_warning_names_the_symbols_and_the_audit_stamp(self) -> None:
        report = check_sweep_scope(["INDIAVIX", "RELIANCE"], allow_out_of_scope=True, **_SPOT_NSE)
        warning = report.override_warning()
        assert "WARNING" in warning
        # repr-quoted since round-4 finding 3 (stderr/log-forging guard).
        assert "'INDIAVIX', 'RELIANCE'" in warning
        assert "scope_override=True" in warning
        assert "DEC-ENGINE-SCOPE-GUARD-001" in warning

    def test_symbol_verdict_carries_symbol_and_reason(self) -> None:
        verdict = self._refusing().refused[0]
        assert isinstance(verdict, SymbolVerdict)
        assert verdict.symbol == "INDIAVIX"
        assert verdict.reason

    def test_hostile_symbol_is_repr_quoted_never_raw(self) -> None:
        # Round-4 finding 3: `--symbols $'EVIL\n[brutex] FORGED \x1b[31m'` must
        # not forge a '[brutex]' stderr line or inject ANSI — both rendered
        # surfaces repr-quote the operator-typed name (#907/#919 convention).
        hostile = "EVIL\n[brutex] FORGED line \x1b[31m"
        refused = check_sweep_scope([hostile], allow_out_of_scope=False, **_SPOT_NSE)
        message = refused.refusal_message()
        assert repr(hostile) in message  # escaped, quoted form
        assert "\x1b" not in message  # no RAW ESC byte
        assert "\n[brutex]" not in message  # no forged line start
        overridden = check_sweep_scope([hostile], allow_out_of_scope=True, **_SPOT_NSE)
        warning = overridden.override_warning()
        assert repr(hostile) in warning
        assert "\x1b" not in warning
        assert "\n" not in warning  # the warning is one line — repr keeps it so

    def test_hostile_exchange_and_segment_are_repr_quoted_never_raw(self) -> None:
        # Round-8 finding 2: the round-4 fix repr-quoted the SYMBOL but left
        # the operator-typed --exchange/--segment raw in the SAME messages —
        # `--segment $'FNO\n[brutex] sweep OK'` forged a status line in
        # CI-captured stderr. Both rendered surfaces now repr-quote the pair.
        forged_segment = "FNO\n[brutex] sweep OK"
        report = check_sweep_scope(
            ["RELIANCE"],
            exchange="NSE",
            segment=forged_segment,
            allow_out_of_scope=False,
        )
        message = report.refusal_message()
        assert repr(forged_segment) in message  # escaped, quoted form
        assert "\n[brutex] sweep OK" not in message  # no forged line
        overridden = check_sweep_scope(
            ["RELIANCE"],
            exchange="NSE\x1b[31m",
            segment=forged_segment,
            allow_out_of_scope=True,
        )
        warning = overridden.override_warning()
        assert repr(forged_segment) in warning
        assert repr("NSE\x1b[31m") in warning
        assert "\x1b" not in warning
        assert "\n" not in warning  # one line — repr keeps it so


class TestCorporateActionExtension:
    """DEC-CORP-ACTION-GATE-001 / ADR-0016 — the extension of DEC-ENGINE-SCOPE-GUARD-001.

    BruteX has ZERO corporate-action adjustment code, so (a) the hard refusal
    must carry that rationale + BOTH decision ids, and (b) the research
    escape must emit one loud structlog hazard WARNING — no override can
    silently produce split-corrupted results.
    """

    def test_refusal_message_carries_the_corp_action_rationale_and_both_dec_ids(self) -> None:
        message = check_sweep_scope(
            ["RELIANCE"], allow_out_of_scope=False, **_SPOT_NSE
        ).refusal_message()
        assert "corporate-action adjustment layer missing" in message
        assert "DEC-CORP-ACTION-GATE-001" in message
        assert "DEC-ENGINE-SCOPE-GUARD-001" in message
        assert "ADR-0016" in message
        assert "silently corrupt" in message

    def test_override_emits_exactly_one_hazard_warning_naming_the_symbols(self) -> None:
        with structlog.testing.capture_logs() as logs:
            report = check_sweep_scope(
                ["INDIAVIX", "RELIANCE"], allow_out_of_scope=True, **_SPOT_NSE
            )
        assert report.overridden is True
        events = [e for e in logs if e["event"] == "sweep.scope_override_hazard"]
        assert len(events) == 1
        assert events[0]["symbols"] == ["INDIAVIX", "RELIANCE"]
        assert events[0]["exchange"] == "NSE"
        assert events[0]["segment"] == "INDEX"
        assert "corporate-action adjustment layer missing" in events[0]["hazard"]
        assert "DEC-CORP-ACTION-GATE-001" in events[0]["hazard"]
        assert "ADR-0016" in events[0]["hazard"]

    def test_refusal_without_the_flag_does_not_emit_the_hazard_warning(self) -> None:
        # The hard refusal is the message's job — the hazard WARNING belongs
        # to the let-through path only (it stamps an actual override).
        with structlog.testing.capture_logs() as logs:
            report = check_sweep_scope(["RELIANCE"], allow_out_of_scope=False, **_SPOT_NSE)
        assert report.ok is False
        assert not any(e["event"] == "sweep.scope_override_hazard" for e in logs)

    def test_in_scope_launch_with_the_flag_does_not_emit_the_hazard_warning(self) -> None:
        # The flag alone never warns — only actually-refused symbols do
        # (mirrors the ``overridden`` stamp semantics).
        with structlog.testing.capture_logs() as logs:
            report = check_sweep_scope(["NIFTY"], allow_out_of_scope=True, **_SPOT_NSE)
        assert report.ok is True
        assert not any(e["event"] == "sweep.scope_override_hazard" for e in logs)
