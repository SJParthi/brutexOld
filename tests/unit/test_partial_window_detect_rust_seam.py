"""The ``partial_window_detect`` Rust-seam routing tests (SLICE T24 PR-1).

DEC-DATA-RUST-INTEGRITY-DETECTORS-001 (MIGRATION.md #1948). Stem-matched to
``brutex/data/partial_window_detect.py`` so the discovery gate folds these
into the module's own 100% field: the flag-OFF inertness spy, the ON-arm
parity for EVERY reason arm through a fake crate (the existing suite's
catching shapes re-derived flag-ON), the payload-factory laws (span floor/
cap, coverage fold, empty short-circuit), the ``_verdict_from_fields``
materializer, the burn-in-poison autorevert (a laundered truncation still
catches), and the real-wheel re-drives + hypothesis differential
(``importorskip``-gated).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta

import brutex.data.detectors_rust as dr
import pytest
from brutex.data.partial_window_detect import (
    NOT_TRUNCATED,
    SUSPECTED_PARTIAL,
    _classify_partial_window_python,
    _partial_window_payload,
    _verdict_from_fields,
    classify_partial_window,
)
from hypothesis import given, settings
from hypothesis import strategies as st

from tests.fakes.detectors_crate import fake_detectors_crate


#: A UTC instant on the given IST date, inside the session.
def _ts(y: int, m: int, d: int, hour: int = 4, minute: int = 5) -> datetime:
    return datetime(y, m, d, hour, minute, tzinfo=UTC)


#: The existing suite's truncated shape: last bar 2024-01-02, a ~30-day
#: window ending 2024-02-01, judged well past the live edge.
_TRUNCATED_KWARGS: dict[str, object] = {
    "last_bar_ts": _ts(2024, 1, 2),
    "window_end": date(2024, 2, 1),
    "now": _ts(2024, 3, 1),
}


@pytest.fixture(autouse=True)
def _isolated_facade(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv(dr.LAKE_RUST_PARTIAL_WINDOW_ENV, raising=False)
    dr.reset_detectors_rust_for_tests()
    yield
    dr.reset_detectors_rust_for_tests()


def _flag_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(dr.LAKE_RUST_PARTIAL_WINDOW_ENV, "1")


def _install_crate(monkeypatch: pytest.MonkeyPatch, crate: object) -> None:
    def _import(name: str) -> object:
        assert name == "brutex_lake"
        return crate

    monkeypatch.setattr(dr.importlib, "import_module", _import)


class TestFlagOffInertness:
    def test_off_never_imports_crate_and_matches_oracle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _bomb(name: str) -> object:
            raise AssertionError(f"flag-OFF classify imported {name!r}")

        monkeypatch.setattr(dr.importlib, "import_module", _bomb)
        verdict = classify_partial_window(**_TRUNCATED_KWARGS)  # type: ignore[arg-type]
        assert verdict.verdict == SUSPECTED_PARTIAL  # the catching shape still catches
        assert verdict.reason == "truncated"


class TestFlagOnFakeCrate:
    """Every reason arm re-derived flag-ON must equal the flag-OFF oracle."""

    _ARMS: tuple[tuple[str, dict[str, object]], ...] = (
        ("truncated", dict(_TRUNCATED_KWARGS)),
        (
            "complete",
            {
                # Bars run to the window's expected end (2024-01-31, a
                # Wednesday session; window_end 2024-02-01 end-exclusive).
                "last_bar_ts": _ts(2024, 1, 31),
                "window_end": date(2024, 2, 1),
                "now": _ts(2024, 3, 1),
            },
        ),
        (
            "sparse_complete",
            {
                # Two trailing sessions short — within the default tolerance.
                "last_bar_ts": _ts(2024, 1, 29),
                "window_end": date(2024, 2, 1),
                "now": _ts(2024, 3, 1),
            },
        ),
        (
            "live_edge",
            {
                # The window end is right at "now" — the vendor may not have
                # published yet.
                "last_bar_ts": _ts(2024, 1, 2),
                "window_end": date(2024, 2, 1),
                "now": _ts(2024, 2, 1),
            },
        ),
        (
            "at_expiry",
            {
                # The contract died 2024-01-25 (a Thursday session) and bars
                # reach it — complete, never a truncation.
                "last_bar_ts": _ts(2024, 1, 25),
                "window_end": date(2024, 2, 1),
                "now": _ts(2024, 3, 1),
                "expiry": date(2024, 1, 25),
            },
        ),
        (
            "empty",
            {
                "last_bar_ts": None,
                "window_end": date(2024, 2, 1),
                "now": _ts(2024, 3, 1),
            },
        ),
        (
            "out_of_calendar_coverage",
            {
                # 2019 predates the NSE holiday-list coverage span.
                "last_bar_ts": _ts(2019, 1, 2),
                "window_end": date(2019, 2, 1),
                "now": _ts(2019, 3, 1),
            },
        ),
    )

    @pytest.mark.parametrize(("reason", "kwargs"), _ARMS, ids=[a[0] for a in _ARMS])
    def test_reason_arm_parity(
        self, reason: str, kwargs: dict[str, object], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _flag_on(monkeypatch)
        _install_crate(monkeypatch, fake_detectors_crate())
        routed = classify_partial_window(**kwargs)  # type: ignore[arg-type]
        assert routed.reason == reason  # the anti-vacuity tooth per arm
        monkeypatch.delenv(dr.LAKE_RUST_PARTIAL_WINDOW_ENV)
        assert routed == classify_partial_window(**kwargs)  # type: ignore[arg-type]

    def test_observed_overlay_threads_into_the_grid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flag_on(monkeypatch)
        _install_crate(monkeypatch, fake_detectors_crate())
        overlay = frozenset({date(2024, 1, 31)})
        kwargs = dict(_TRUNCATED_KWARGS)
        routed = classify_partial_window(observed=overlay, **kwargs)  # type: ignore[arg-type]
        monkeypatch.delenv(dr.LAKE_RUST_PARTIAL_WINDOW_ENV)
        assert routed == classify_partial_window(observed=overlay, **kwargs)  # type: ignore[arg-type]

    def test_burn_in_poison_cannot_launder_a_truncation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _flag_on(monkeypatch)
        _install_crate(
            monkeypatch,
            fake_detectors_crate(classify_partial=lambda *a: ("complete", a[0], a[0], 0)),
        )
        verdict = classify_partial_window(**_TRUNCATED_KWARGS)  # type: ignore[arg-type]
        # The poisoned "complete" claim dies at burn-in: the ORACLE verdict
        # ships — the demotion-driving flag still fires.
        assert verdict.verdict == SUSPECTED_PARTIAL
        assert dr.rust_detectors_disabled() is True

    def test_tolerance_and_live_edge_overrides_thread_to_both_arms(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _flag_on(monkeypatch)
        _install_crate(monkeypatch, fake_detectors_crate())
        kwargs = {
            "last_bar_ts": _ts(2024, 1, 29),
            "window_end": date(2024, 2, 1),
            "now": _ts(2024, 3, 1),
            "tolerance_sessions": 1,
        }
        routed = classify_partial_window(**kwargs)  # type: ignore[arg-type]
        assert routed.verdict == SUSPECTED_PARTIAL  # 2 missing > override 1
        monkeypatch.delenv(dr.LAKE_RUST_PARTIAL_WINDOW_ENV)
        assert routed == classify_partial_window(**kwargs)  # type: ignore[arg-type]


class TestPayloadFactory:
    def test_empty_window_payload_shape(self) -> None:
        payload = _partial_window_payload(
            last_bar_ts=None,
            window_end=date(2024, 2, 1),
            now=_ts(2024, 3, 1),
            expiry=None,
            tolerance=5,
            live_edge=3,
            observed=frozenset(),
        )
        assert payload.last_bar_ordinal is None
        assert payload.trading_flags == ()
        assert payload.coverage_ok is False  # unused — the EMPTY arm returns first

    def test_out_of_coverage_payload_skips_the_grid_walk(self) -> None:
        payload = _partial_window_payload(
            last_bar_ts=_ts(2019, 1, 2),
            window_end=date(2019, 2, 1),
            now=_ts(2019, 3, 1),
            expiry=None,
            tolerance=5,
            live_edge=3,
            observed=frozenset(),
        )
        assert payload.coverage_ok is False
        assert payload.trading_flags == ()

    def test_over_cap_span_ships_an_empty_grid(self) -> None:
        # A far-future expiry (a >9000-day span) must never materialise a
        # giant grid — the crate's first walk abstains → the oracle serves.
        payload = _partial_window_payload(
            last_bar_ts=_ts(2024, 1, 2),
            window_end=date(2024, 2, 1),
            now=_ts(2024, 3, 1),
            expiry=date(2070, 1, 1),
            tolerance=5,
            live_edge=3,
            observed=frozenset(),
        )
        assert payload.coverage_ok is True
        assert payload.trading_flags == ()
        assert payload.expiry_ordinal == date(2070, 1, 1).toordinal()

    def test_normal_grid_spans_every_walk(self) -> None:
        payload = _partial_window_payload(
            last_bar_ts=_ts(2024, 1, 2),
            window_end=date(2024, 2, 1),
            now=_ts(2024, 3, 1),
            expiry=date(2024, 2, 15),
            tolerance=5,
            live_edge=3,
            observed=frozenset(),
        )
        span_low = date(2024, 1, 2) - timedelta(days=45)
        assert payload.span_start_ordinal == span_low.toordinal()
        span_high = date(2024, 3, 1)  # max(today, expiry)
        assert len(payload.trading_flags) == (span_high - span_low).days + 1
        # A known Saturday inside the span is a non-session flag.
        saturday = date(2024, 1, 6)
        assert payload.trading_flags[(saturday - span_low).days] is False
        # A known regular Tuesday is a session flag.
        tuesday = date(2024, 1, 2)
        assert payload.trading_flags[(tuesday - span_low).days] is True

    def test_abstain_arm_end_to_end_serves_the_oracle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The over-cap span routes → empty grid → REAL abstain contract
        # (fake crate walks off the grid → None → oracle serves).
        _flag_on(monkeypatch)
        _install_crate(monkeypatch, fake_detectors_crate())
        kwargs = {
            "last_bar_ts": _ts(2024, 1, 2),
            "window_end": date(2024, 2, 1),
            "now": _ts(2024, 3, 1),
            "expiry": date(2070, 1, 1),
        }
        routed = classify_partial_window(**kwargs)  # type: ignore[arg-type]
        monkeypatch.delenv(dr.LAKE_RUST_PARTIAL_WINDOW_ENV)
        assert routed == classify_partial_window(**kwargs)  # type: ignore[arg-type]


class TestVerdictFromFields:
    def test_truncated_reason_is_the_one_suspected_verdict(self) -> None:
        verdict = _verdict_from_fields("truncated", 738887, 738900, 9)
        assert verdict.verdict == SUSPECTED_PARTIAL
        assert verdict.last_bar_date == date.fromordinal(738887)
        assert verdict.expected_end_date == date.fromordinal(738900)
        assert verdict.missing_sessions == 9

    @pytest.mark.parametrize(
        "reason",
        [
            "empty",
            "out_of_calendar_coverage",
            "at_expiry",
            "live_edge",
            "complete",
            "sparse_complete",
        ],
    )
    def test_every_other_reason_is_not_truncated(self, reason: str) -> None:
        verdict = _verdict_from_fields(reason, None, None, 0)
        assert verdict.verdict == NOT_TRUNCATED
        assert verdict.last_bar_date is None
        assert verdict.expected_end_date is None


class TestRealWheel:
    """Flag-ON re-drives against the REAL built wheel (skipped when absent)."""

    @staticmethod
    def _require_wheel(monkeypatch: pytest.MonkeyPatch) -> None:
        pytest.importorskip("brutex_lake")
        monkeypatch.setenv(dr.LAKE_RUST_PARTIAL_WINDOW_ENV, "1")
        dr.reset_detectors_rust_for_tests(remaining=0)  # steady state

    def test_catching_case_still_catches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._require_wheel(monkeypatch)
        verdict = classify_partial_window(**_TRUNCATED_KWARGS)  # type: ignore[arg-type]
        assert verdict.verdict == SUSPECTED_PARTIAL
        assert verdict.reason == "truncated"

    @settings(max_examples=120, deadline=None)
    @given(
        last_bar_off=st.one_of(st.none(), st.integers(min_value=0, max_value=45)),
        window_days=st.integers(min_value=1, max_value=60),
        now_off=st.integers(min_value=0, max_value=90),
        expiry_off=st.one_of(st.none(), st.integers(min_value=-10, max_value=70)),
        tolerance=st.one_of(st.none(), st.integers(min_value=1, max_value=8)),
        live_edge=st.one_of(st.none(), st.integers(min_value=1, max_value=5)),
    )
    def test_classify_differential_vs_oracle(
        self,
        last_bar_off: int | None,
        window_days: int,
        now_off: int,
        expiry_off: int | None,
        tolerance: int | None,
        live_edge: int | None,
    ) -> None:
        base = date(2024, 1, 2)
        kwargs: dict[str, object] = {
            "last_bar_ts": (
                _ts(2024, 1, 2) + timedelta(days=last_bar_off) if last_bar_off is not None else None
            ),
            "window_end": base + timedelta(days=window_days),
            "now": _ts(2024, 1, 2) + timedelta(days=now_off),
            "expiry": (base + timedelta(days=expiry_off) if expiry_off is not None else None),
            "tolerance_sessions": tolerance,
            "live_edge_sessions": live_edge,
        }
        with pytest.MonkeyPatch.context() as mp:
            self._require_wheel(mp)
            mp.delenv("BRUTEX_PARTIAL_WINDOW_TOLERANCE_SESSIONS", raising=False)
            mp.delenv("BRUTEX_PARTIAL_WINDOW_LIVE_EDGE_SESSIONS", raising=False)
            routed = classify_partial_window(**kwargs)  # type: ignore[arg-type]
        resolved_tolerance = tolerance if tolerance is not None else 5
        resolved_live_edge = live_edge if live_edge is not None else 3
        assert routed == _classify_partial_window_python(
            last_bar_ts=kwargs["last_bar_ts"],  # type: ignore[arg-type]
            window_end=kwargs["window_end"],  # type: ignore[arg-type]
            now=kwargs["now"],  # type: ignore[arg-type]
            expiry=kwargs["expiry"],  # type: ignore[arg-type]
            tolerance=resolved_tolerance,
            live_edge=resolved_live_edge,
            observed=frozenset(),
        )
        dr.reset_detectors_rust_for_tests()
