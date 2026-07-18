"""The ``interior_gap_detect`` Rust-seam routing tests (SLICE T24 PR-1).

DEC-DATA-RUST-INTEGRITY-DETECTORS-001 (MIGRATION.md #1948). Stem-matched to
``brutex/data/interior_gap_detect.py`` so the discovery gate folds these
into the module's own 100% field: the flag-OFF inertness spy, the
too-few-covered short-circuit (never engages the crate), the ON-arm
payload-factory law (the ONE calendar walk — covered/interior ordinals),
the catching-case parity matrix through a fake crate (the existing suite's
CATCHING shapes re-derived flag-ON), the burn-in-poison autorevert, and
the real-wheel re-drives + hypothesis differential
(``importorskip``-gated).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta

import brutex.data.detectors_rust as dr
import pytest
from brutex.data.interior_gap_detect import (
    INTERIOR_GAP,
    NO_INTERIOR_GAP,
    _interior_barless_runs,
    _interior_barless_runs_python,
    classify_interior_gap,
    classify_interior_gap_dates,
    interior_gap_runs,
)
from hypothesis import given, settings
from hypothesis import strategies as st

from tests.fakes.detectors_crate import fake_detectors_crate


def _ts(y: int, m: int, d: int) -> datetime:
    """An in-session UTC timestamp on the given IST trading date."""
    return datetime(y, m, d, 4, 5, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _isolated_facade(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv(dr.LAKE_RUST_INTERIOR_GAP_ENV, raising=False)
    dr.reset_detectors_rust_for_tests()
    yield
    dr.reset_detectors_rust_for_tests()


def _flag_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(dr.LAKE_RUST_INTERIOR_GAP_ENV, "1")


def _install_crate(monkeypatch: pytest.MonkeyPatch, crate: object) -> None:
    def _import(name: str) -> object:
        assert name == "brutex_lake"
        return crate

    monkeypatch.setattr(dr.importlib, "import_module", _import)


def _bomb_import(monkeypatch: pytest.MonkeyPatch, label: str) -> None:
    def _bomb(name: str) -> object:
        raise AssertionError(f"{label} imported {name!r}")

    monkeypatch.setattr(dr.importlib, "import_module", _bomb)


class TestFlagOffInertness:
    def test_off_never_imports_crate_and_matches_oracle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _bomb_import(monkeypatch, "flag-OFF interior-gap classify")
        verdict = classify_interior_gap(bar_timestamps=[_ts(2021, 3, 1), _ts(2021, 3, 22)])
        assert verdict.verdict == INTERIOR_GAP  # the catching shape still catches
        covered = [date(2021, 3, 1), date(2021, 3, 22)]
        assert _interior_barless_runs(covered) == _interior_barless_runs_python(covered)

    def test_too_few_covered_never_engages_the_crate_even_flag_on(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _flag_on(monkeypatch)
        _bomb_import(monkeypatch, "too-few-covered walker")
        assert _interior_barless_runs([]) == []
        assert _interior_barless_runs([date(2021, 3, 1)]) == []


class TestFlagOnFakeCrate:
    """The existing suite's CATCHING shapes re-derived flag-ON (fake crate)."""

    def _parity(self, monkeypatch: pytest.MonkeyPatch, **kwargs: object) -> None:
        _flag_on(monkeypatch)
        _install_crate(monkeypatch, fake_detectors_crate())
        routed = classify_interior_gap(**kwargs)  # type: ignore[arg-type]
        monkeypatch.delenv(dr.LAKE_RUST_INTERIOR_GAP_ENV)
        assert routed == classify_interior_gap(**kwargs)  # type: ignore[arg-type]

    def test_large_interior_gap_still_flagged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flag_on(monkeypatch)
        _install_crate(monkeypatch, fake_detectors_crate())
        verdict = classify_interior_gap(bar_timestamps=[_ts(2021, 3, 1), _ts(2021, 3, 22)])
        assert verdict.verdict == INTERIOR_GAP
        assert verdict.reason == "interior_gap"
        assert verdict.largest_gap_sessions > 5

    def test_continuous_and_small_gap_parity(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._parity(
            monkeypatch, bar_timestamps=[_ts(2021, 3, 1), _ts(2021, 3, 2), _ts(2021, 3, 3)]
        )
        self._parity(monkeypatch, bar_timestamps=[_ts(2021, 3, 1), _ts(2021, 3, 4)])

    def test_out_of_calendar_coverage_abstains_before_the_walker(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _flag_on(monkeypatch)
        _install_crate(monkeypatch, fake_detectors_crate())
        verdict = classify_interior_gap(bar_timestamps=[_ts(2019, 3, 1), _ts(2019, 3, 22)])
        assert verdict.verdict == NO_INTERIOR_GAP
        assert verdict.reason == "out_of_calendar_coverage"

    def test_observed_overlay_threads_into_the_payload(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # An observed overlay day is NOT a session — it must vanish from the
        # interior-session payload exactly as the oracle skips it.
        _flag_on(monkeypatch)
        _install_crate(monkeypatch, fake_detectors_crate())
        covered = [date(2021, 3, 1), date(2021, 3, 10)]
        overlay = {date(2021, 3, 4)}
        routed = _interior_barless_runs(covered, frozenset(overlay))
        assert routed == _interior_barless_runs_python(covered, frozenset(overlay))
        # And the overlay genuinely shrinks the run vs the bare calendar.
        bare = _interior_barless_runs_python(covered)
        assert routed[0][2] == bare[0][2] - 1

    def test_interior_gap_runs_bounds_parity(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flag_on(monkeypatch)
        _install_crate(monkeypatch, fake_detectors_crate())
        covered = (date(2021, 3, 1), date(2021, 3, 22))
        routed = interior_gap_runs(covered_dates=covered)
        monkeypatch.delenv(dr.LAKE_RUST_INTERIOR_GAP_ENV)
        assert routed == interior_gap_runs(covered_dates=covered)
        assert routed is not None
        assert len(routed) == 1  # the ONE confirmed >tolerance run

    def test_classify_dates_boundary_start_hole_parity(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A hole starting at the very first interior session (the
        # boundary-start shape the #1794 demotions catch).
        _flag_on(monkeypatch)
        _install_crate(monkeypatch, fake_detectors_crate())
        covered = (date(2021, 3, 1), date(2021, 3, 15), date(2021, 3, 16))
        routed = classify_interior_gap_dates(covered_dates=covered)
        monkeypatch.delenv(dr.LAKE_RUST_INTERIOR_GAP_ENV)
        assert routed == classify_interior_gap_dates(covered_dates=covered)
        assert routed.verdict == INTERIOR_GAP

    def test_burn_in_poison_ships_the_oracle_runs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flag_on(monkeypatch)
        _install_crate(monkeypatch, fake_detectors_crate(interior_runs=lambda c, i: []))
        # A poisoned crate claiming "no runs" can never launder a hole:
        # burn-in ships the ORACLE runs — the verdict still catches.
        verdict = classify_interior_gap(bar_timestamps=[_ts(2021, 3, 1), _ts(2021, 3, 22)])
        assert verdict.verdict == INTERIOR_GAP
        assert dr.rust_detectors_disabled() is True

    def test_adjacent_covered_dates_yield_empty_interior_payload(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _flag_on(monkeypatch)
        _install_crate(monkeypatch, fake_detectors_crate())
        covered = [date(2021, 3, 1), date(2021, 3, 2)]
        assert _interior_barless_runs(covered) == []


class TestRealWheel:
    """Flag-ON re-drives against the REAL built wheel (skipped when absent)."""

    @staticmethod
    def _require_wheel(monkeypatch: pytest.MonkeyPatch) -> None:
        pytest.importorskip("brutex_lake")
        monkeypatch.setenv(dr.LAKE_RUST_INTERIOR_GAP_ENV, "1")
        dr.reset_detectors_rust_for_tests(remaining=0)  # steady state

    def test_catching_case_still_catches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # HONEST framing (round-1 review): _require_wheel pins remaining=0,
        # so THIS re-drive genuinely ships the Rust value — unlike the
        # parity lane's flag-ON leg over the EXISTING suites, which is a
        # BELT only (burn-in autorevert can leave the ORACLE catching
        # there). The genuine parity gates are this suite's remaining=0
        # differential below + the cargo frozen vectors.
        self._require_wheel(monkeypatch)
        verdict = classify_interior_gap(bar_timestamps=[_ts(2021, 3, 1), _ts(2021, 3, 22)])
        assert verdict.verdict == INTERIOR_GAP
        assert verdict.largest_gap_sessions > 5

    @settings(max_examples=120, deadline=None)
    @given(
        offsets=st.sets(st.integers(min_value=0, max_value=40), min_size=0, max_size=12),
        overlay_offsets=st.sets(st.integers(min_value=0, max_value=40), max_size=4),
    )
    def test_walker_differential_vs_oracle(
        self, offsets: set[int], overlay_offsets: set[int]
    ) -> None:
        base = date(2024, 1, 1)
        covered = sorted(base + timedelta(days=o) for o in offsets)
        observed = frozenset(base + timedelta(days=o) for o in overlay_offsets)
        with pytest.MonkeyPatch.context() as mp:
            self._require_wheel(mp)
            routed = _interior_barless_runs(covered, observed)
        assert routed == _interior_barless_runs_python(covered, observed)
        dr.reset_detectors_rust_for_tests()
