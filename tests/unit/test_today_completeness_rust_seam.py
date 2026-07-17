"""The ``today_completeness`` Rust-seam routing tests (SLICE T24 PR-2).

DEC-DATA-RUST-INTEGRITY-DETECTORS-001. Stem-matched to
``brutex/data/today_completeness.py`` so the discovery gate folds these
into the module's own 100% field: the flag-OFF inertness spy, the ON-arm
parity for EVERY criteria arm through the fake crate (the existing
suite's catching shapes re-derived flag-ON), the inert-arms-never-consult
negative pin, the payload-factory laws (date-preserving minute ordinals,
class codes, the Python-computed close-edge flag), the materializer, the
burn-in-poison autorevert (a laundered demotion still demotes), the
LOAD-BEARING crossverify gate-seam discipline pin (a faulted crate under
flag-ON ALWAYS resolves to the oracle verdict — never a new exception
class out of the fail-closed publish gate), and the real-wheel re-drives
+ hypothesis differential (``importorskip``-gated).
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import brutex.data.detectors_rust as dr
import pytest
from brutex.calendars.nse import get_session
from brutex.data.models import CandleBar
from brutex.data.today_completeness import (
    InstrumentClass,
    TodayCompletenessVerdict,
    _classify_today_completeness_python,
    classify_today_completeness,
)
from hypothesis import given, settings
from hypothesis import strategies as st
from structlog.testing import capture_logs

from tests.fakes.detectors_crate import fake_detectors_crate

_IST = ZoneInfo("Asia/Kolkata")

#: A regular NSE trading day squarely inside the holiday coverage.
_TODAY = date(2021, 3, 5)  # Friday
_END = _TODAY + timedelta(days=1)

#: A Muhurat evening session (60 minutes — 2025-10-21, 13:45-14:45 IST).
_MUHURAT = date(2025, 10, 21)


def _bar(ts: datetime, *, volume: int = 1, oi: int | None = None) -> CandleBar:
    return CandleBar(
        timestamp=ts,
        open=Decimal("100.00"),
        high=Decimal("110.00"),
        low=Decimal("90.00"),
        close=Decimal("105.00"),
        volume=volume,
        open_interest=oi,
    )


def _minute(day: date, minute_index: int, *, volume: int = 1, oi: int | None = None) -> CandleBar:
    session = get_session(day)
    assert session is not None
    open_dt = datetime.combine(day, session.open_time_ist, tzinfo=_IST)
    return _bar((open_dt + timedelta(minutes=minute_index)).astimezone(UTC), volume=volume, oi=oi)


def _full_day(day: date, *, volume: int = 1, oi: int | None = None) -> list[CandleBar]:
    session = get_session(day)
    assert session is not None
    return [_minute(day, i, volume=volume, oi=oi) for i in range(session.duration_minutes)]


def _classify(
    bars: Sequence[CandleBar],
    *,
    instrument_class: InstrumentClass,
    clamp_today: date = _TODAY,
    window_end: date | None = None,
) -> TodayCompletenessVerdict:
    return classify_today_completeness(
        bars_today=bars,
        clamp_today=clamp_today,
        window_end=window_end if window_end is not None else clamp_today + timedelta(days=1),
        instrument_class=instrument_class,
    )


def _install_crate(monkeypatch: pytest.MonkeyPatch, crate: object) -> None:
    def _import(name: str) -> object:
        assert name == "brutex_lake"
        return crate

    monkeypatch.setattr(dr.importlib, "import_module", _import)


def _bomb_import(monkeypatch: pytest.MonkeyPatch, label: str) -> None:
    def _bomb(name: str) -> object:
        raise AssertionError(f"{label} imported {name!r}")

    monkeypatch.setattr(dr.importlib, "import_module", _bomb)


@pytest.fixture(autouse=True)
def _isolated_facade(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv(dr.LAKE_RUST_TODAY_COMPLETENESS_ENV, raising=False)
    dr.reset_detectors_rust_for_tests()
    yield
    dr.reset_detectors_rust_for_tests()


def _flag_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(dr.LAKE_RUST_TODAY_COMPLETENESS_ENV, "1")


class TestFlagOffInertness:
    def test_off_never_imports_crate_and_matches_oracle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _bomb_import(monkeypatch, "flag-OFF classify")
        bars = _full_day(_TODAY)[:-1]  # 374 — the catching count-short shape
        verdict = _classify(bars, instrument_class=InstrumentClass.INDEX_SPOT)
        assert verdict.incomplete is True
        assert verdict.reason == "count_short"


class TestFlagOnFakeCrate:
    """Every criteria arm re-derived flag-ON must equal the flag-OFF oracle."""

    _ARMS: tuple[tuple[str, dict[str, Any]], ...] = (
        (
            "count_short",
            {"bars": "regular_374", "instrument_class": InstrumentClass.INDEX_SPOT},
        ),
        (
            "complete",
            {"bars": "regular_full", "instrument_class": InstrumentClass.INDEX_SPOT},
        ),
        (
            "count_excess",
            {"bars": "regular_excess", "instrument_class": InstrumentClass.INDEX_SPOT},
        ),
        (
            "last_bar_incomplete",
            {"bars": "regular_ends_early", "instrument_class": InstrumentClass.INDEX_SPOT},
        ),
        (
            "count_short",
            {
                "bars": "muhurat_59",
                "instrument_class": InstrumentClass.INDEX_SPOT,
                "clamp_today": _MUHURAT,
            },
        ),
        (
            "count_short",
            {"bars": "empty", "instrument_class": InstrumentClass.INDEX_SPOT},
        ),
        (
            "no_today_bars",
            {"bars": "empty", "instrument_class": InstrumentClass.CASH_SPOT},
        ),
        (
            "volume_all_zero",
            {"bars": "cash_all_zero", "instrument_class": InstrumentClass.CASH_SPOT},
        ),
        (
            "complete",
            {"bars": "cash_mixed", "instrument_class": InstrumentClass.CASH_SPOT},
        ),
        (
            "oi_null_settling",
            {"bars": "fut_oi_null_and_zero_vol", "instrument_class": InstrumentClass.FUT},
        ),
        (
            "volume_all_zero",
            {"bars": "fut_all_zero_vol", "instrument_class": InstrumentClass.FUT},
        ),
        (
            "complete",
            {"bars": "fut_sparse_settled", "instrument_class": InstrumentClass.FUT},
        ),
        (
            "oi_null_settling",
            {"bars": "option_oi_null", "instrument_class": InstrumentClass.OPTION},
        ),
        (
            "complete",
            {"bars": "option_sparse_zero_vol", "instrument_class": InstrumentClass.OPTION},
        ),
        (
            "count_short",
            {"bars": "regular_dup_minute", "instrument_class": InstrumentClass.INDEX_SPOT},
        ),
    )

    @staticmethod
    def _bars_for(shape: str, day: date) -> list[CandleBar]:
        if shape == "empty":
            return []
        if shape == "regular_374":
            return _full_day(day)[:-1]
        if shape == "regular_full":
            return _full_day(day)
        if shape == "regular_excess":
            bars = _full_day(day)
            session = get_session(day)
            assert session is not None
            close_dt = datetime.combine(day, session.close_time_ist, tzinfo=_IST)
            return [*bars, _bar((close_dt + timedelta(minutes=15)).astimezone(UTC))]
        if shape == "regular_ends_early":
            # Full distinct count (a stray 09:14 pre-open bar tops it up) but
            # the session tail stops at 15:28 — the close edge is never
            # reached (the existing suite's catching shape).
            bars = [_minute(day, i) for i in range(374)]  # 09:15..15:28
            return [
                *bars,
                _bar(datetime.combine(day, time(9, 14), tzinfo=_IST).astimezone(UTC)),
            ]
        if shape == "muhurat_59":
            return _full_day(_MUHURAT)[:-1]
        if shape == "cash_all_zero":
            return [_minute(day, i, volume=0) for i in range(5)]
        if shape == "cash_mixed":
            return [_minute(day, 0, volume=0), _minute(day, 1, volume=7)]
        if shape == "fut_oi_null_and_zero_vol":
            # BOTH shapes present — the arm-ORDER pin (OI-null wins).
            return [_minute(day, 0, volume=0, oi=None), _minute(day, 1, volume=0, oi=None)]
        if shape == "fut_all_zero_vol":
            return [_minute(day, 0, volume=0, oi=9), _minute(day, 1, volume=0, oi=9)]
        if shape == "fut_sparse_settled":
            return [_minute(day, i, volume=3, oi=9) for i in range(12)]
        if shape == "option_oi_null":
            return [_minute(day, 0, volume=1, oi=4), _minute(day, 1, volume=1, oi=None)]
        if shape == "option_sparse_zero_vol":
            return [_minute(day, i, volume=0, oi=4) for i in range(4)]
        if shape == "regular_dup_minute":
            bars = _full_day(day)[:-1]
            return [*bars, _minute(day, 0)]
        raise AssertionError(f"unknown corpus shape {shape!r}")

    @pytest.mark.parametrize(
        ("reason", "spec"),
        _ARMS,
        ids=[f"{spec['bars']}-{reason}" for reason, spec in _ARMS],
    )
    def test_criteria_arm_parity(
        self, reason: str, spec: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clamp_today = spec.get("clamp_today", _TODAY)
        bars = self._bars_for(spec["bars"], clamp_today)
        instrument_class = spec["instrument_class"]
        oracle = _classify(bars, instrument_class=instrument_class, clamp_today=clamp_today)
        _flag_on(monkeypatch)
        _install_crate(monkeypatch, fake_detectors_crate())
        routed = _classify(bars, instrument_class=instrument_class, clamp_today=clamp_today)
        assert routed.reason == reason  # the anti-vacuity tooth per arm
        assert routed == oracle  # field-for-field verdict parity

    def test_fut_sparse_settled_observe_flag_threads(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flag_on(monkeypatch)
        _install_crate(monkeypatch, fake_detectors_crate())
        bars = self._bars_for("fut_sparse_settled", _TODAY)
        routed = _classify(bars, instrument_class=InstrumentClass.FUT)
        assert routed.incomplete is False
        assert routed.count_short_observed is True  # the observe-only INFO signal

    def test_display_fields_survive_the_routed_arm(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # expected_minutes / last_bar_ist / covers_today are Python-computed
        # and must reach the routed verdict verbatim (the materializer law).
        _flag_on(monkeypatch)
        _install_crate(monkeypatch, fake_detectors_crate())
        bars = self._bars_for("regular_374", _TODAY)
        routed = _classify(bars, instrument_class=InstrumentClass.INDEX_SPOT)
        assert routed.covers_today is True
        assert routed.expected_minutes == 375
        assert routed.distinct_minutes == 374
        assert routed.last_bar_ist == "15:28"

    @pytest.mark.parametrize(
        ("reason", "kwargs"),
        [
            ("not_covering", {"window_end": _TODAY}),
            ("abstain_coverage", {"clamp_today": date(2033, 3, 5)}),
            ("non_trading", {"clamp_today": date(2021, 3, 6)}),  # Saturday
        ],
    )
    def test_inert_arms_never_consult_the_crate(
        self, reason: str, kwargs: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The three Python-side arms resolve BEFORE the route — flag ON with
        # a BOMBED import proves the crate is structurally unreachable there.
        _flag_on(monkeypatch)
        _bomb_import(monkeypatch, "inert-arm classify")
        verdict = _classify(
            [_minute(_TODAY, 0)], instrument_class=InstrumentClass.INDEX_SPOT, **kwargs
        )
        assert verdict.reason == reason
        assert verdict.incomplete is False

    def test_burn_in_poison_cannot_launder_a_demotion(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A crate claiming "complete" on a count-short day dies at burn-in:
        # the ORACLE verdict ships — the withhold/demotion still fires.
        _flag_on(monkeypatch)
        _install_crate(
            monkeypatch,
            fake_detectors_crate(classify_today=lambda *a: (False, 0, 375, 0, 0, False)),
        )
        bars = self._bars_for("regular_374", _TODAY)
        with capture_logs() as logs:
            verdict = _classify(bars, instrument_class=InstrumentClass.INDEX_SPOT)
        assert verdict.incomplete is True
        assert verdict.reason == "count_short"
        assert dr.rust_detectors_disabled() is True
        assert any(e["event"] == "detectors_rust.autoreverted" for e in logs)


class TestPayloadFactory:
    def test_minute_ordinals_are_date_preserving(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Two bars at the SAME wall-clock minute on DIFFERENT IST dates must
        # count as TWO distinct minutes (the oracle's minute-floor DATETIME
        # set) — the absolute-minute-ordinal law, pinned through a crate spy.
        captured: list[tuple[Any, ...]] = []

        def _spy(*args: Any) -> Any:
            captured.append(args)
            from tests.fakes.detectors_crate import fake_classify_today_completeness_verdict

            return fake_classify_today_completeness_verdict(*args)

        _flag_on(monkeypatch)
        _install_crate(monkeypatch, fake_detectors_crate(classify_today=_spy))
        # An option judged directly (the crossverify direct-call shape) with
        # a stray prior-day bar at the same HH:MM.
        same_wall_prior = _bar(
            (datetime.combine(_TODAY - timedelta(days=1), time(9, 15), tzinfo=_IST)).astimezone(
                UTC
            ),
            oi=4,
        )
        bars = [same_wall_prior, _minute(_TODAY, 0, oi=4)]
        routed = _classify(bars, instrument_class=InstrumentClass.OPTION)
        assert routed.distinct_minutes == 2  # never a wall-clock collapse
        (call,) = captured
        minute_ordinals = call[2]
        assert len(set(minute_ordinals)) == 2
        # Parity with the oracle on the same multi-date input.
        monkeypatch.delenv(dr.LAKE_RUST_TODAY_COMPLETENESS_ENV)
        assert routed == _classify(bars, instrument_class=InstrumentClass.OPTION)

    def test_payload_carries_class_code_flags_and_close_edge(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: list[tuple[Any, ...]] = []

        def _spy(*args: Any) -> Any:
            captured.append(args)
            from tests.fakes.detectors_crate import fake_classify_today_completeness_verdict

            return fake_classify_today_completeness_verdict(*args)

        _flag_on(monkeypatch)
        _install_crate(monkeypatch, fake_detectors_crate(classify_today=_spy))
        bars = [_minute(_TODAY, 0, volume=0, oi=None), _minute(_TODAY, 1, volume=5, oi=9)]
        _classify(bars, instrument_class=InstrumentClass.FUT)
        (call,) = captured
        class_code, expected_minutes, _minutes, zero_volume, oi_null, last_bar_complete = call
        assert class_code == 2  # FUT
        assert expected_minutes == 375
        assert zero_volume == (True, False)
        assert oi_null == (True, False)
        # Non-INDEX ships the NEUTRAL True — the close-edge consult is
        # INDEX-lazy (round-1 review NIT 1; the crate ignores the bool for
        # every non-INDEX class, so the verdict is identical either way).
        assert last_bar_complete is True

    def test_close_edge_lazy_never_consulted_for_non_index(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Round-1 review NIT 1: the routed arm mirrors the ORACLE's laziness —
        # `session_last_bar_complete` is consulted ONLY for INDEX_SPOT. A
        # BOMBED calendar law + a FUT classify flag-ON must never fire it
        # (the oracle's FUT arm never consults it either, so parity holds).
        import brutex.data.today_completeness as tc

        def _bomb(*_a: object) -> bool:
            raise AssertionError("non-INDEX routed arm consulted session_last_bar_complete")

        monkeypatch.setattr(tc, "session_last_bar_complete", _bomb)
        _flag_on(monkeypatch)
        _install_crate(monkeypatch, fake_detectors_crate())
        bars = [_minute(_TODAY, 0, volume=3, oi=9), _minute(_TODAY, 1, volume=4, oi=9)]
        routed = _classify(bars, instrument_class=InstrumentClass.FUT)
        assert routed.reason == "complete"
        assert routed.last_bar_ist == "09:16"  # the display field stays eager

    def test_close_edge_false_reaches_the_payload_for_index(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The INDEX arm still consults the REAL calendar law (the False pin
        # moved here from the FUT payload test at the round-1 laziness fold):
        # a 374-bar day's last stored bar is 15:28 — the close edge is short.
        captured: list[tuple[Any, ...]] = []

        def _spy(*args: Any) -> Any:
            captured.append(args)
            from tests.fakes.detectors_crate import fake_classify_today_completeness_verdict

            return fake_classify_today_completeness_verdict(*args)

        _flag_on(monkeypatch)
        _install_crate(monkeypatch, fake_detectors_crate(classify_today=_spy))
        _classify(_full_day(_TODAY)[:-1], instrument_class=InstrumentClass.INDEX_SPOT)
        (call,) = captured
        assert call[5] is False  # session_last_bar_complete(15:28) — consulted

    def test_poisoned_class_token_degrades_to_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Round-1 review NIT 3 (end-to-end): a poisoned `_TODAY_CLASS_CODES`
        # map (an unknown class token — structurally unreachable today over
        # the closed enum) raises KeyError from the payload factory; under
        # the flag the classifier must serve the ORACLE verdict with ONE
        # WARN — no raise, no `crate_panic` latch.
        import brutex.data.today_completeness as tc

        _flag_on(monkeypatch)
        _install_crate(monkeypatch, fake_detectors_crate())
        monkeypatch.setattr(tc, "_TODAY_CLASS_CODES", {})
        bars = _full_day(_TODAY)[:-1]
        with capture_logs() as logs:
            verdict = _classify(bars, instrument_class=InstrumentClass.INDEX_SPOT)
        assert verdict.incomplete is True
        assert verdict.reason == "count_short"  # the ORACLE verdict, verbatim
        warns = [e for e in logs if e["event"] == "detectors_rust.fallback"]
        assert [w["reason"] for w in warns] == ["today_completeness_payload_error"]
        assert dr.rust_detectors_disabled() is False  # degrade, never a latch

    def test_close_edge_true_on_a_complete_day(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: list[tuple[Any, ...]] = []

        def _spy(*args: Any) -> Any:
            captured.append(args)
            from tests.fakes.detectors_crate import fake_classify_today_completeness_verdict

            return fake_classify_today_completeness_verdict(*args)

        _flag_on(monkeypatch)
        _install_crate(monkeypatch, fake_detectors_crate(classify_today=_spy))
        _classify(_full_day(_TODAY), instrument_class=InstrumentClass.INDEX_SPOT)
        (call,) = captured
        assert call[5] is True  # session_last_bar_complete — Python-computed

    def test_empty_bars_payload_close_edge_vacuous_true(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: list[tuple[Any, ...]] = []

        def _spy(*args: Any) -> Any:
            captured.append(args)
            from tests.fakes.detectors_crate import fake_classify_today_completeness_verdict

            return fake_classify_today_completeness_verdict(*args)

        _flag_on(monkeypatch)
        _install_crate(monkeypatch, fake_detectors_crate(classify_today=_spy))
        routed = _classify([], instrument_class=InstrumentClass.INDEX_SPOT)
        (call,) = captured
        assert call[2] == ()  # no minute ordinals
        assert call[5] is True  # vacuous — the INDEX arm fails on count first
        assert routed.reason == "count_short"
        assert routed.last_bar_ist is None


class TestGateSeamDiscipline:
    """The LOAD-BEARING crossverify pin (design §2.4): the fail-closed
    publish gate binds ``classify_today_completeness`` DIRECTLY, so a
    faulted crate under flag-ON must ALWAYS resolve to the oracle verdict —
    never a new exception class out of the bound classifier."""

    @staticmethod
    def _bound_classifier() -> Any:
        from brutex.crossverify.gate_seam import bind_production_classifier

        return bind_production_classifier()

    def _drive(self, monkeypatch: pytest.MonkeyPatch, crate: object) -> Any:
        import sys

        _flag_on(monkeypatch)
        # The crate lands via sys.modules (NOT the importlib patch) so the
        # gate seam's own lazy `importlib.import_module(GATE_MODULE)` bind
        # stays live — the production import route end-to-end.
        monkeypatch.setitem(sys.modules, "brutex_lake", crate)
        dr.reset_detectors_rust_for_tests(remaining=0)  # steady state — no burn-in shield
        classifier = self._bound_classifier()
        return classifier(
            bars_today=_full_day(_TODAY)[:-1],
            clamp_today=_TODAY,
            window_end=_END,
            instrument_class="index_spot",
        )

    def test_raising_crate_resolves_to_the_oracle_verdict(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _raises(*_a: object) -> object:
            raise RuntimeError("crate fault")

        verdict = self._drive(monkeypatch, fake_detectors_crate(classify_today=_raises))
        assert verdict.incomplete is True
        assert verdict.reason == "count_short"

    def test_panicking_crate_resolves_to_the_oracle_verdict(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from polars.exceptions import PanicException

        def _panics(*_a: object) -> object:
            raise PanicException("crate panicked")

        verdict = self._drive(monkeypatch, fake_detectors_crate(classify_today=_panics))
        assert verdict.incomplete is True
        assert verdict.reason == "count_short"
        assert dr.rust_detectors_disabled() is True  # latched, still served

    def test_junk_shape_crate_resolves_to_the_oracle_verdict(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        verdict = self._drive(monkeypatch, fake_detectors_crate(classify_today=lambda *a: "junk"))
        assert verdict.incomplete is True
        assert verdict.reason == "count_short"

    def test_unknown_reason_index_refused_to_the_oracle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        verdict = self._drive(
            monkeypatch,
            fake_detectors_crate(classify_today=lambda *a: (True, 99, 374, 0, 0, False)),
        )
        assert verdict.reason == "count_short"  # the oracle's token, never a junk index


class TestRealWheel:
    """Flag-ON re-drives against the REAL built wheel (skipped when absent)."""

    @staticmethod
    def _require_wheel(monkeypatch: pytest.MonkeyPatch) -> None:
        wheel = pytest.importorskip("brutex_lake")
        if not callable(getattr(wheel, "classify_today_completeness_verdict", None)):
            pytest.skip("installed brutex_lake wheel predates the PR-2 law")
        monkeypatch.setenv(dr.LAKE_RUST_TODAY_COMPLETENESS_ENV, "1")
        monkeypatch.setattr(dr.importlib, "import_module", lambda _name: wheel)
        dr.reset_detectors_rust_for_tests(remaining=0)  # steady state

    def test_catching_case_still_catches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._require_wheel(monkeypatch)
        verdict = _classify(_full_day(_TODAY)[:-1], instrument_class=InstrumentClass.INDEX_SPOT)
        assert verdict.incomplete is True
        assert verdict.reason == "count_short"

    @settings(max_examples=120, deadline=None)
    @given(
        instrument_class=st.sampled_from(list(InstrumentClass)),
        minutes=st.lists(st.integers(min_value=0, max_value=374), min_size=0, max_size=40),
        volume_zero=st.lists(st.booleans(), min_size=40, max_size=40),
        oi_null=st.lists(st.booleans(), min_size=40, max_size=40),
    )
    def test_classify_differential_vs_oracle(
        self,
        instrument_class: InstrumentClass,
        minutes: list[int],
        volume_zero: list[bool],
        oi_null: list[bool],
    ) -> None:
        bars = [
            _minute(
                _TODAY,
                m,
                volume=0 if volume_zero[i] else 1 + i,
                oi=None if oi_null[i] else 7,
            )
            for i, m in enumerate(minutes)
        ]
        with pytest.MonkeyPatch.context() as mp:
            self._require_wheel(mp)
            routed = _classify(bars, instrument_class=instrument_class)
            session = get_session(_TODAY)
            assert session is not None
            assert routed == _classify_today_completeness_python(
                bars_today=bars, instrument_class=instrument_class, session=session
            )
        dr.reset_detectors_rust_for_tests()


class TestOracleExtractionPin:
    def test_oracle_is_the_flag_off_law(self) -> None:
        # The extracted verbatim body IS what flag-OFF serves (the Slice-0
        # extraction discipline — one law, two entries).
        session = get_session(_TODAY)
        assert session is not None
        bars = _full_day(_TODAY)
        assert _classify_today_completeness_python(
            bars_today=bars, instrument_class=InstrumentClass.INDEX_SPOT, session=session
        ) == _classify(bars, instrument_class=InstrumentClass.INDEX_SPOT)
