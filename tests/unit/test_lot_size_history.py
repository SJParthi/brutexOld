"""Tests for `brutex.options.lot_size_history`.

Verifies per-date lookup against the citation-grounded table in
`docs/track2/TRACK2_OPTIONS_SPEC.md` §8.3.

8 historical transitions tested:
  Jan 2021     → NIFTY 75, BANKNIFTY 25, SENSEX 10  (start)
  Jul 2021     → NIFTY 50
  Jun 2023     → BANKNIFTY 15
  Apr 2024     → NIFTY 25
  Nov 2024     → NIFTY 75, BANKNIFTY 30, SENSEX 20 (SEBI Rs 15L rule)
  Jul 2025     → BANKNIFTY 35 (transitional)
  Dec 2025     → BANKNIFTY 30
  Jan 2026     → NIFTY 65 (current)
"""

# Whitebox pins on the private Python oracles (DEC-OPTCOST-TAILS-RUST-001):
# pyright: reportPrivateUsage=none

from __future__ import annotations

from datetime import date

import pytest
from brutex.options.lot_size_history import (
    LotSizeChange,
    OptionsUnderlying,
    all_current_lot_sizes,
    all_transitions,
    get_lot_size,
)


class TestNiftyHistory:
    @pytest.mark.parametrize(
        "on_date,expected_lot",
        [
            (date(2021, 1, 1), 75),  # pre-Jul 2021
            (date(2021, 7, 28), 75),  # one day before transition
            (date(2021, 7, 29), 50),  # Jul 2021 expiry rebasing
            (date(2024, 4, 25), 50),  # one day before next transition
            (date(2024, 4, 26), 25),  # 26-Apr-2024
            (date(2024, 11, 19), 25),  # one day before SEBI Rs 15L
            (date(2024, 11, 20), 75),  # 20-Nov-2024 SEBI rule
            (date(2025, 12, 31), 75),  # still 75
            (date(2026, 1, 1), 65),  # Jan 2026 current
            (date(2026, 5, 16), 65),  # today
        ],
    )
    def test_nifty_lot_by_date(self, on_date: date, expected_lot: int) -> None:
        assert get_lot_size(OptionsUnderlying.NIFTY, on_date) == expected_lot


class TestBankniftyHistory:
    @pytest.mark.parametrize(
        "on_date,expected_lot",
        [
            (date(2021, 1, 1), 25),
            (date(2023, 6, 29), 25),
            (date(2023, 6, 30), 15),
            (date(2024, 11, 19), 15),
            (date(2024, 11, 20), 30),
            (date(2025, 6, 30), 30),
            (date(2025, 7, 1), 35),  # transitional bump
            (date(2025, 12, 29), 35),
            (date(2025, 12, 30), 30),  # revert
            (date(2026, 5, 16), 30),
        ],
    )
    def test_banknifty_lot_by_date(self, on_date: date, expected_lot: int) -> None:
        assert get_lot_size(OptionsUnderlying.BANKNIFTY, on_date) == expected_lot


class TestSensexHistory:
    @pytest.mark.parametrize(
        "on_date,expected_lot",
        [
            (date(2021, 1, 1), 10),
            (date(2024, 11, 19), 10),
            (date(2024, 11, 20), 20),
            (date(2026, 5, 16), 20),
        ],
    )
    def test_sensex_lot_by_date(self, on_date: date, expected_lot: int) -> None:
        assert get_lot_size(OptionsUnderlying.SENSEX, on_date) == expected_lot


class TestPreHistoryRejected:
    def test_before_2021_raises(self) -> None:
        with pytest.raises(ValueError, match="before recorded history"):
            get_lot_size(OptionsUnderlying.NIFTY, date(2020, 12, 31))


class TestAllTransitions:
    def test_nifty_transitions_count(self) -> None:
        # 5 entries: pre-Jul-2021, Jul-2021, Apr-2024, Nov-2024, Jan-2026
        transitions = all_transitions(OptionsUnderlying.NIFTY)
        assert len(transitions) == 5

    def test_banknifty_transitions_count(self) -> None:
        # 5 entries: pre-Jun-2023, Jun-2023, Nov-2024, Jul-2025, Dec-2025
        transitions = all_transitions(OptionsUnderlying.BANKNIFTY)
        assert len(transitions) == 5

    def test_sensex_transitions_count(self) -> None:
        # 2 entries: pre-Nov-2024, Nov-2024
        transitions = all_transitions(OptionsUnderlying.SENSEX)
        assert len(transitions) == 2

    def test_transitions_sorted_ascending(self) -> None:
        for inst in OptionsUnderlying:
            transitions = all_transitions(inst)
            for i in range(len(transitions) - 1):
                assert transitions[i].effective_date < transitions[i + 1].effective_date

    def test_transition_dataclass_fields(self) -> None:
        transitions = all_transitions(OptionsUnderlying.NIFTY)
        first = transitions[0]
        assert isinstance(first, LotSizeChange)
        assert first.instrument == OptionsUnderlying.NIFTY
        assert first.effective_date == date(2021, 1, 1)
        assert first.lot_size == 75


class TestAllCurrentLotSizes:
    def test_current_today(self) -> None:
        # As of today (2026-05-16): NIFTY=65, BANKNIFTY=30, SENSEX=20
        current = all_current_lot_sizes(date(2026, 5, 16))
        assert current[OptionsUnderlying.NIFTY] == 65
        assert current[OptionsUnderlying.BANKNIFTY] == 30
        assert current[OptionsUnderlying.SENSEX] == 20

    def test_current_at_old_date(self) -> None:
        # At Jul 2022: NIFTY=50, BANKNIFTY=25, SENSEX=10
        current = all_current_lot_sizes(date(2022, 7, 1))
        assert current[OptionsUnderlying.NIFTY] == 50
        assert current[OptionsUnderlying.BANKNIFTY] == 25
        assert current[OptionsUnderlying.SENSEX] == 10


class TestSEBI20Nov2024_BigRebase:
    """The 20-Nov-2024 SEBI Rs 15L notional rule was the largest single-date
    rebasing. All 3 instruments changed on the same day."""

    def test_all_3_change_same_day(self) -> None:
        # Day before: old lots
        before = all_current_lot_sizes(date(2024, 11, 19))
        assert before[OptionsUnderlying.NIFTY] == 25
        assert before[OptionsUnderlying.BANKNIFTY] == 15
        assert before[OptionsUnderlying.SENSEX] == 10
        # On the day: new lots
        on_day = all_current_lot_sizes(date(2024, 11, 20))
        assert on_day[OptionsUnderlying.NIFTY] == 75
        assert on_day[OptionsUnderlying.BANKNIFTY] == 30
        assert on_day[OptionsUnderlying.SENSEX] == 20


# ── DEC-OPTCOST-TAILS-RUST-001: the flag-ON seam (ON == OFF, refusal bytes) ──


class TestOptcostRustSeamOn:
    """`get_lot_size` under `BRUTEX_OPTCOST_TAILS_RUST=1` — kernel-routed
    answers match the Python oracle across every transition boundary, and
    the pre-history ValueError keeps its exact message bytes."""

    @pytest.fixture(autouse=True)
    def _seam_on(self, monkeypatch: pytest.MonkeyPatch):
        import brutex.optcost_rust as ocr

        pytest.importorskip("fno_math")
        monkeypatch.setenv(ocr.OPTCOST_TAILS_RUST_ENV, "1")
        ocr.reset_optcost_rust_for_tests()  # burn-in armed — oracle runs beside
        yield
        ocr.reset_optcost_rust_for_tests()

    def test_every_transition_boundary_matches_the_oracle(self) -> None:
        from datetime import timedelta

        import brutex.optcost_rust as ocr
        from brutex.options.lot_size_history import (
            _HISTORY_BY_UNDERLYING,
            _get_lot_size_python,
        )

        for instrument, history in _HISTORY_BY_UNDERLYING.items():
            for boundary, _lot in history:
                for delta in (0, 1):
                    on = boundary + timedelta(days=delta)
                    assert get_lot_size(instrument, on) == _get_lot_size_python(
                        history, on, instrument
                    )
        assert not ocr.optcost_rust_disabled()

    def test_prehistory_refusal_bytes_match_the_oracle(self) -> None:
        on = date(2020, 12, 31)
        with pytest.raises(ValueError) as seam_exc:
            get_lot_size(OptionsUnderlying.NIFTY, on)
        assert str(seam_exc.value) == (
            f"on_date {on} is before recorded history start (2021-01-01 for NIFTY)"
        )
