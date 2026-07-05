"""Tests for `brutex.options.expiry_calendar`.

Verifies per-date weekly + monthly expiry day-of-week lookup against
the citation-grounded NSE/BSE/SEBI table in
`docs/track2/TRACK2_OPTIONS_SPEC.md` §2.

Spot-checks per regime transition.
"""

from __future__ import annotations

from datetime import date

import pytest
from brutex.options.expiry_calendar import (
    FRIDAY,
    THURSDAY,
    TUESDAY,
    WEDNESDAY,
    get_monthly_dow,
    get_weekly_dow,
    has_weekly,
    next_monthly_expiry,
    next_weekly_expiry,
)
from brutex.options.lot_size_history import OptionsUnderlying


class TestNiftyWeekly:
    @pytest.mark.parametrize(
        "on_date,expected_dow",
        [
            (date(2021, 1, 1), THURSDAY),
            (date(2024, 7, 15), THURSDAY),
            (date(2025, 8, 28), THURSDAY),  # last Thursday weekly
            (date(2025, 9, 2), TUESDAY),  # first Tuesday weekly
            (date(2026, 5, 16), TUESDAY),  # today
        ],
    )
    def test_nifty_weekly_dow(self, on_date: date, expected_dow: int) -> None:
        assert get_weekly_dow(OptionsUnderlying.NIFTY, on_date) == expected_dow

    def test_nifty_always_has_weekly(self) -> None:
        # NIFTY never had its weekly killed
        for d in [date(2021, 1, 1), date(2024, 11, 14), date(2025, 9, 2), date(2026, 5, 16)]:
            assert has_weekly(OptionsUnderlying.NIFTY, d)


class TestBankniftyWeekly:
    @pytest.mark.parametrize(
        "on_date,expected_dow",
        [
            (date(2021, 1, 1), THURSDAY),
            (date(2023, 9, 3), THURSDAY),  # day before Wednesday transition
            (date(2023, 9, 4), WEDNESDAY),
            (date(2024, 11, 13), WEDNESDAY),  # last day of weekly
        ],
    )
    def test_banknifty_weekly_dow_before_kill(self, on_date: date, expected_dow: int) -> None:
        assert get_weekly_dow(OptionsUnderlying.BANKNIFTY, on_date) == expected_dow

    @pytest.mark.parametrize(
        "on_date",
        [
            date(2024, 11, 14),  # day weekly killed
            date(2025, 1, 1),
            date(2025, 9, 2),
            date(2026, 5, 16),  # today
        ],
    )
    def test_banknifty_weekly_killed(self, on_date: date) -> None:
        assert get_weekly_dow(OptionsUnderlying.BANKNIFTY, on_date) is None
        assert not has_weekly(OptionsUnderlying.BANKNIFTY, on_date)


class TestSensexWeekly:
    @pytest.mark.parametrize(
        "on_date,expected_dow",
        [
            (date(2021, 1, 1), THURSDAY),  # thin pre-2023-05-15
            (date(2023, 5, 12), THURSDAY),
            (date(2023, 5, 15), FRIDAY),  # first Friday weekly
            (date(2024, 12, 31), FRIDAY),
            (date(2025, 1, 1), TUESDAY),
            (date(2025, 9, 3), TUESDAY),
            (date(2025, 9, 4), THURSDAY),  # first Thursday weekly under SEBI rule
            (date(2026, 5, 16), THURSDAY),
        ],
    )
    def test_sensex_weekly_dow(self, on_date: date, expected_dow: int) -> None:
        assert get_weekly_dow(OptionsUnderlying.SENSEX, on_date) == expected_dow


class TestMonthlyDow:
    @pytest.mark.parametrize(
        "instrument,on_date,expected_dow",
        [
            # NIFTY: Thursday → Tuesday (Sep 2, 2025)
            (OptionsUnderlying.NIFTY, date(2021, 1, 1), THURSDAY),
            (OptionsUnderlying.NIFTY, date(2025, 9, 2), TUESDAY),
            # BANKNIFTY: Thursday → Wednesday (Mar 1, 2024) → Thursday (Jan 1, 2025) → Tuesday (Sep 2, 2025)
            (OptionsUnderlying.BANKNIFTY, date(2021, 1, 1), THURSDAY),
            (OptionsUnderlying.BANKNIFTY, date(2024, 2, 29), THURSDAY),
            (OptionsUnderlying.BANKNIFTY, date(2024, 3, 1), WEDNESDAY),
            (OptionsUnderlying.BANKNIFTY, date(2024, 12, 31), WEDNESDAY),
            (OptionsUnderlying.BANKNIFTY, date(2025, 1, 1), THURSDAY),
            (OptionsUnderlying.BANKNIFTY, date(2025, 9, 2), TUESDAY),
            # SENSEX: Thursday → Friday (May 15, 2023) → Tuesday (Jan 1, 2025) → Thursday (Sep 4, 2025)
            (OptionsUnderlying.SENSEX, date(2021, 1, 1), THURSDAY),
            (OptionsUnderlying.SENSEX, date(2023, 5, 15), FRIDAY),
            (OptionsUnderlying.SENSEX, date(2025, 1, 1), TUESDAY),
            (OptionsUnderlying.SENSEX, date(2025, 9, 4), THURSDAY),
        ],
    )
    def test_monthly_dow(
        self, instrument: OptionsUnderlying, on_date: date, expected_dow: int
    ) -> None:
        assert get_monthly_dow(instrument, on_date) == expected_dow


class TestNextWeeklyExpiry:
    def test_nifty_friday_to_tuesday_via_wrap(self) -> None:
        # NIFTY weekly = Tuesday (as of 2026-05-16)
        # Today (Fri 2026-05-15) → next Tuesday = 2026-05-19
        result = next_weekly_expiry(OptionsUnderlying.NIFTY, date(2026, 5, 15))
        assert result == date(2026, 5, 19)

    def test_nifty_on_tuesday_returns_same_day(self) -> None:
        # Tuesday 2026-05-12 → expiry is TODAY (on-or-after semantics)
        result = next_weekly_expiry(OptionsUnderlying.NIFTY, date(2026, 5, 12))
        assert result == date(2026, 5, 12)

    def test_nifty_old_regime_thursday(self) -> None:
        # Mon 2024-07-15 → next Thursday = 2024-07-18
        result = next_weekly_expiry(OptionsUnderlying.NIFTY, date(2024, 7, 15))
        assert result == date(2024, 7, 18)

    def test_banknifty_post_kill_returns_none(self) -> None:
        assert next_weekly_expiry(OptionsUnderlying.BANKNIFTY, date(2026, 5, 16)) is None

    def test_sensex_thursday_under_new_regime(self) -> None:
        # SENSEX weekly = Thursday as of 2025-09-04
        # Today Fri 2026-05-15 → next Thursday = 2026-05-21
        result = next_weekly_expiry(OptionsUnderlying.SENSEX, date(2026, 5, 15))
        assert result == date(2026, 5, 21)


class TestNextMonthlyExpiry:
    def test_nifty_may_2026_last_tuesday(self) -> None:
        # NIFTY monthly = last Tuesday. May 2026 last Tuesday = 2026-05-26.
        # Sat 2026-05-16 → last Tue May = 2026-05-26 (still in May)
        result = next_monthly_expiry(OptionsUnderlying.NIFTY, date(2026, 5, 16))
        assert result == date(2026, 5, 26)

    def test_nifty_last_tue_already_past_rolls_to_next_month(self) -> None:
        # Wed 2026-05-27 → last Tue May (26) past → roll to last Tue June
        # June 2026 last Tuesday = 2026-06-30
        result = next_monthly_expiry(OptionsUnderlying.NIFTY, date(2026, 5, 27))
        assert result == date(2026, 6, 30)

    def test_banknifty_monthly_2024_last_wednesday(self) -> None:
        # BANKNIFTY monthly = Wednesday during 2024-03-01..2024-12-31
        # April 2024: last Wednesday = April 24, 2024
        result = next_monthly_expiry(OptionsUnderlying.BANKNIFTY, date(2024, 4, 1))
        assert result == date(2024, 4, 24)

    def test_year_boundary_december_to_january(self) -> None:
        # NIFTY 2025: monthly = last Thursday until Sep 2, then Tuesday.
        # In 2024 dec, BANKNIFTY = last Wednesday. Last Wed of Dec 2024 = 25? No that's Christmas.
        # Last Wed of Dec 2024 = 2024-12-25.
        # If on_date = 2024-12-26, last Wed already past → next month Jan 2025 last Thursday = 2025-01-30
        result = next_monthly_expiry(OptionsUnderlying.BANKNIFTY, date(2024, 12, 26))
        assert result == date(2025, 1, 30)


class TestNiftyWithdrawnMondayRegime:
    """Test that the WITHDRAWN Monday regime (March-April 2025) is NOT in our table.

    NSE proposed Monday weekly effective 2025-04-04 (early March 2025 circular),
    then withdrew on 2025-03-27 (Zerodha bulletin 407122). NIFTY weekly stayed
    Thursday through 2025-08-28 and jumped directly to Tuesday 2025-09-02.
    """

    def test_april_2025_still_thursday_not_monday(self) -> None:
        # The "Monday window" would have started 2025-04-04. Verify our
        # table still says Thursday on that date.
        assert get_weekly_dow(OptionsUnderlying.NIFTY, date(2025, 4, 4)) == THURSDAY

    def test_august_2025_still_thursday(self) -> None:
        assert get_weekly_dow(OptionsUnderlying.NIFTY, date(2025, 8, 28)) == THURSDAY

    def test_september_2025_jumps_to_tuesday_not_monday(self) -> None:
        assert get_weekly_dow(OptionsUnderlying.NIFTY, date(2025, 9, 2)) == TUESDAY


class Test2020Coverage:
    """DEC-EXPIRY-REGIME-2020-001: coverage extended to the 2020-01-01 pull floor.

    NIFTY + BANKNIFTY 2020 = Thursday weekly / last-Thursday monthly
    (secondary-corroborated across two independent research rounds: NIFTY
    weekly launched 2019-02-11, BANKNIFTY weekly launched May 2016, Thursday
    regime continuous until the 2023 changes). SENSEX 2020 = Thursday is an
    UNVERIFIED-continuity inference (thin/illiquid pre-2023 relaunch; no
    citable 2020 BSE circular reachable) — pinned so a silent regime edit
    can't slip in without revisiting the evidence.
    """

    @pytest.mark.parametrize(
        "instrument",
        [OptionsUnderlying.NIFTY, OptionsUnderlying.BANKNIFTY, OptionsUnderlying.SENSEX],
    )
    def test_weekly_dow_2020_is_thursday(self, instrument: OptionsUnderlying) -> None:
        assert get_weekly_dow(instrument, date(2020, 6, 15)) == THURSDAY

    @pytest.mark.parametrize(
        "instrument",
        [OptionsUnderlying.NIFTY, OptionsUnderlying.BANKNIFTY, OptionsUnderlying.SENSEX],
    )
    def test_monthly_dow_2020_is_thursday(self, instrument: OptionsUnderlying) -> None:
        assert get_monthly_dow(instrument, date(2020, 6, 15)) == THURSDAY

    @pytest.mark.parametrize(
        "instrument",
        [OptionsUnderlying.NIFTY, OptionsUnderlying.BANKNIFTY, OptionsUnderlying.SENSEX],
    )
    def test_has_weekly_across_2020(self, instrument: OptionsUnderlying) -> None:
        for d in [date(2020, 1, 1), date(2020, 6, 15), date(2020, 12, 31)]:
            assert has_weekly(instrument, d)

    @pytest.mark.parametrize(
        "instrument",
        [OptionsUnderlying.NIFTY, OptionsUnderlying.BANKNIFTY],
    )
    def test_next_weekly_expiry_2020(self, instrument: OptionsUnderlying) -> None:
        # Mon 2020-06-15 → next Thursday = 2020-06-18.
        assert next_weekly_expiry(instrument, date(2020, 6, 15)) == date(2020, 6, 18)

    def test_next_weekly_expiry_2020_sensex_unverified_continuity(self) -> None:
        # SENSEX rides the SAME Thursday table row — UNVERIFIED continuity
        # (see class docstring); the pin is deliberate, not a verification.
        assert next_weekly_expiry(OptionsUnderlying.SENSEX, date(2020, 6, 15)) == date(2020, 6, 18)

    @pytest.mark.parametrize(
        "instrument",
        [OptionsUnderlying.NIFTY, OptionsUnderlying.BANKNIFTY],
    )
    def test_next_monthly_expiry_march_2020(self, instrument: OptionsUnderlying) -> None:
        # Last Thursday of March 2020 = 2020-03-26.
        assert next_monthly_expiry(instrument, date(2020, 3, 1)) == date(2020, 3, 26)

    def test_next_monthly_expiry_march_2020_sensex_unverified_continuity(self) -> None:
        assert next_monthly_expiry(OptionsUnderlying.SENSEX, date(2020, 3, 1)) == date(2020, 3, 26)

    def test_pull_floor_start_covered(self) -> None:
        # 2020-01-01 itself (== GROWW_HISTORY_FLOOR) resolves.
        assert get_weekly_dow(OptionsUnderlying.NIFTY, date(2020, 1, 1)) == THURSDAY


class TestPreHistoryRejected:
    def test_before_2020_raises(self) -> None:
        # The coverage floor moved 2021 → 2020 (DEC-EXPIRY-REGIME-2020-001);
        # a pre-floor date STILL refuses rather than guess (Golden Rule #1).
        with pytest.raises(ValueError, match="before recorded history"):
            get_weekly_dow(OptionsUnderlying.NIFTY, date(2019, 12, 31))

    def test_2020_no_longer_raises(self) -> None:
        # Regression: 2020-12-31 raised before the extension.
        assert get_weekly_dow(OptionsUnderlying.NIFTY, date(2020, 12, 31)) == THURSDAY

    @pytest.mark.parametrize(
        "instrument",
        [OptionsUnderlying.NIFTY, OptionsUnderlying.BANKNIFTY, OptionsUnderlying.SENSEX],
    )
    def test_monthly_before_2020_raises(self, instrument: OptionsUnderlying) -> None:
        with pytest.raises(ValueError, match="before recorded history"):
            get_monthly_dow(instrument, date(2019, 12, 31))
