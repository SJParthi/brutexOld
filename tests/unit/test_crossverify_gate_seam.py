"""Unit tests — :mod:`brutex.crossverify.gate_seam` (fail-closed gate binding)."""

from __future__ import annotations

import enum
import types
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Any

import pytest
from brutex.crossverify.gate_seam import (
    GATE_MODULE,
    CompletenessGateUnavailable,
    TodayCompletenessClassifier,
    VerdictLike,
    bind_production_classifier,
)

from tests.fakes.fake_completeness_gate import FakeVerdict, ScriptedClassifier

if TYPE_CHECKING:
    from collections.abc import Sequence


class _InstrumentClass(enum.Enum):
    INDEX_SPOT = "index_spot"
    CASH_SPOT = "cash_spot"
    FUT = "fut"
    OPTION = "option"


@dataclass(frozen=True)
class _Verdict:
    covers_today: bool
    incomplete: bool
    reason: str
    expected_minutes: int | None
    distinct_minutes: int | None
    last_bar_ist: str | None


def _fake_gate_module(calls: list[dict[str, Any]]) -> types.ModuleType:
    module = types.ModuleType("fake_today_completeness")

    def classify_today_completeness(
        *,
        bars_today: Sequence[object],
        clamp_today: date,
        window_end: date,
        instrument_class: _InstrumentClass,
    ) -> _Verdict:
        calls.append(
            {
                "bars": len(bars_today),
                "clamp_today": clamp_today,
                "window_end": window_end,
                "instrument_class": instrument_class,
            }
        )
        return _Verdict(
            covers_today=True,
            incomplete=False,
            reason="complete",
            expected_minutes=375,
            distinct_minutes=375,
            last_bar_ist="15:29",
        )

    module.classify_today_completeness = classify_today_completeness  # type: ignore[attr-defined]
    module.InstrumentClass = _InstrumentClass  # type: ignore[attr-defined]
    return module


class TestBindProductionClassifier:
    def test_binds_and_adapts_str_token_to_enum_by_value(self) -> None:
        calls: list[dict[str, Any]] = []
        gate = _fake_gate_module(calls)

        def importer(name: str) -> types.ModuleType:
            assert name == GATE_MODULE == "brutex.data.today_completeness"
            return gate

        classifier = bind_production_classifier(importer)
        verdict = classifier(
            bars_today=[],
            clamp_today=date(2026, 7, 10),
            window_end=date(2026, 7, 10),
            instrument_class="index_spot",
        )
        assert verdict.reason == "complete"
        assert verdict.expected_minutes == 375
        assert calls == [
            {
                "bars": 0,
                "clamp_today": date(2026, 7, 10),
                "window_end": date(2026, 7, 10),
                # BY VALUE — the "index_spot" STR token maps to the enum member.
                "instrument_class": _InstrumentClass.INDEX_SPOT,
            }
        ]

    def test_every_class_token_maps(self) -> None:
        calls: list[dict[str, Any]] = []
        classifier = bind_production_classifier(lambda _name: _fake_gate_module(calls))
        for token, member in [
            ("index_spot", _InstrumentClass.INDEX_SPOT),
            ("cash_spot", _InstrumentClass.CASH_SPOT),
            ("fut", _InstrumentClass.FUT),
        ]:
            classifier(
                bars_today=[],
                clamp_today=date(2026, 7, 10),
                window_end=date(2026, 7, 10),
                instrument_class=token,
            )
            assert calls[-1]["instrument_class"] is member

    def test_missing_module_is_fail_closed(self) -> None:
        def importer(name: str) -> types.ModuleType:
            raise ImportError(f"No module named {name!r}")

        with pytest.raises(CompletenessGateUnavailable, match="fail-closed"):
            bind_production_classifier(importer)

    @pytest.mark.parametrize("missing", ["classify_today_completeness", "InstrumentClass"])
    def test_missing_attribute_is_fail_closed_naming_it(self, missing: str) -> None:
        # Round-3 X3 (refuter R2-2): a PARTIAL/renamed sibling module — the
        # import succeeds but a required attribute is gone — previously
        # escaped the fail-closed exit-1+CRITICAL law as a raw
        # AttributeError. Now: the SAME CompletenessGateUnavailable refusal,
        # its message NAMING the missing attribute.
        gate = _fake_gate_module([])
        delattr(gate, missing)
        with pytest.raises(CompletenessGateUnavailable, match=missing):
            bind_production_classifier(lambda _name: gate)

    def test_enum_value_drift_folds_to_type_error_healthy_unchanged(self) -> None:
        # Round-4 polish P2 (future-only belt): a sibling enum-VALUE rename
        # ("index_spot" no longer a member value) makes the seam's
        # InstrumentClass(token) conversion raise a raw ValueError — which
        # day_export's drift net deliberately does NOT catch (a bare
        # ValueError there would mask gate-internal bugs). The seam folds
        # it into TypeError so the export loop's EXISTING except withholds
        # the symbol as `gate_signature_drift` instead of crashing mid-run.
        calls: list[dict[str, Any]] = []
        gate = _fake_gate_module(calls)

        class _Renamed(enum.Enum):
            INDEX_SPOT = "idx_spot"  # the VALUE drifted away from "index_spot"
            CASH_SPOT = "cash_spot"
            FUT = "fut"

        gate.InstrumentClass = _Renamed  # type: ignore[attr-defined]
        classifier = bind_production_classifier(lambda _name: gate)
        with pytest.raises(TypeError, match="gate enum drift") as excinfo:
            classifier(
                bars_today=[],
                clamp_today=date(2026, 7, 10),
                window_end=date(2026, 7, 10),
                instrument_class="index_spot",
            )
        assert isinstance(excinfo.value.__cause__, ValueError)  # chained, never swallowed
        assert calls == []  # the drifted token never reached classify
        # Healthy path UNCHANGED on the SAME classifier: an un-drifted
        # token still maps by value and classifies normally.
        verdict = classifier(
            bars_today=[],
            clamp_today=date(2026, 7, 10),
            window_end=date(2026, 7, 10),
            instrument_class="cash_spot",
        )
        assert verdict.reason == "complete"
        assert calls[-1]["instrument_class"] is _Renamed.CASH_SPOT

    def test_default_importer_state_matches_tree(self) -> None:
        # Honest either way: with the sibling module absent this is the
        # fail-closed arm through the REAL importlib; once #1774 merges the
        # bind succeeds — both outcomes satisfy the seam's contract.
        try:
            classifier = bind_production_classifier()
        except CompletenessGateUnavailable:
            import importlib.util

            assert importlib.util.find_spec(GATE_MODULE) is None
        else:
            assert callable(classifier)


class TestProtocolConformance:
    def test_fake_verdict_is_verdict_like(self) -> None:
        verdict: VerdictLike = FakeVerdict()
        assert verdict.covers_today is True
        assert verdict.incomplete is False
        assert verdict.reason == "complete"
        assert verdict.expected_minutes is None
        assert verdict.distinct_minutes is None
        assert verdict.last_bar_ist is None

    def test_scripted_classifier_is_classifier(self) -> None:
        classifier: TodayCompletenessClassifier = ScriptedClassifier()
        verdict = classifier(
            bars_today=[],
            clamp_today=date(2026, 7, 10),
            window_end=date(2026, 7, 10),
            instrument_class="fut",
        )
        assert verdict.reason == "complete"
