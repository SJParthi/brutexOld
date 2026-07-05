"""Tests for `brutex.options.risk_free` (DEC-GREEKS-ENGINE-PR1-001).

Phase-1 gate of the Greeks engine plan (`docs/GREEKS_ENGINE_PLAN.md` §11):
100% line + branch on the module, the ``r``-source + version stamped,
standalone with NO Greeks dependency.
"""

from __future__ import annotations

import dataclasses
import math
from datetime import date
from pathlib import Path

import pytest
from brutex.options.risk_free import (
    DAY_COUNT_BASE,
    DEFAULT_FIXED_RATE,
    RISK_FREE_SOURCE_VERSION,
    CsvRateSource,
    FixedRateSource,
    RiskFreeError,
    RiskFreeRate,
    RiskFreeSource,
    load_risk_free_csv,
    money_market_to_continuous,
)
from hypothesis import given
from hypothesis import strategies as st

_D = date(2026, 7, 2)


def _write_csv(tmp_path: Path, body: str, name: str = "risk_free_daily.csv") -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


class TestMoneyMarketToContinuous:
    def test_golden_91_day_tbill(self) -> None:
        # Independently computed via log1p: (365/91)*log1p(0.065*91/365).
        assert money_market_to_continuous(0.065, 91) == pytest.approx(
            0.06447894369221821, abs=1e-15
        )

    def test_zero_yield_is_zero(self) -> None:
        assert money_market_to_continuous(0.0, 91) == 0.0

    def test_full_year_tenor_is_log_one_plus_y(self) -> None:
        assert money_market_to_continuous(0.07, DAY_COUNT_BASE) == pytest.approx(
            0.06765864847381486, abs=1e-15
        )

    @pytest.mark.parametrize("tenor", [0, -1, -365])
    def test_non_positive_tenor_rejected(self, tenor: int) -> None:
        with pytest.raises(RiskFreeError, match="tenor_days must be >= 1"):
            money_market_to_continuous(0.065, tenor)

    @pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
    def test_non_finite_yield_rejected(self, bad: float) -> None:
        with pytest.raises(RiskFreeError, match="must be finite"):
            money_market_to_continuous(bad, 91)

    @pytest.mark.parametrize("y", [-1.0, -4.5])
    def test_non_positive_growth_rejected(self, y: float) -> None:
        with pytest.raises(RiskFreeError, match="non-positive growth"):
            money_market_to_continuous(y, DAY_COUNT_BASE)

    @given(
        y=st.floats(min_value=0.0, max_value=0.5),
        n=st.integers(min_value=1, max_value=365),
    )
    def test_property_continuous_never_exceeds_simple(self, y: float, n: int) -> None:
        # ln(1+x) <= x  =>  0 <= r_cc <= y for y >= 0.
        r_cc = money_market_to_continuous(y, n)
        assert 0.0 <= r_cc <= y + 1e-12

    @given(
        y=st.floats(min_value=0.0, max_value=0.5),
        n=st.integers(min_value=1, max_value=365),
    )
    def test_property_round_trip_recovers_simple_yield(self, y: float, n: int) -> None:
        r_cc = money_market_to_continuous(y, n)
        y_back = (math.exp(r_cc * n / DAY_COUNT_BASE) - 1.0) * DAY_COUNT_BASE / n
        assert y_back == pytest.approx(y, abs=1e-9)

    @given(
        y1=st.floats(min_value=0.0, max_value=0.4),
        bump=st.floats(min_value=1e-6, max_value=0.1),
        n=st.integers(min_value=1, max_value=365),
    )
    def test_property_monotone_in_yield(self, y1: float, bump: float, n: int) -> None:
        assert money_market_to_continuous(y1 + bump, n) > money_market_to_continuous(y1, n)


class TestFixedRateSource:
    def test_default_is_plan_default_and_stamped(self) -> None:
        src = FixedRateSource()
        out = src.rate_on(_D)
        assert out == RiskFreeRate(
            rate=DEFAULT_FIXED_RATE, source="fixed", version=RISK_FREE_SOURCE_VERSION
        )
        assert out.rate == 0.065

    def test_custom_rate(self) -> None:
        assert FixedRateSource(rate=0.071).rate_on(_D).rate == 0.071

    def test_zero_rate_boundary_allowed(self) -> None:
        assert FixedRateSource(rate=0.0).rate_on(_D).rate == 0.0

    def test_date_independent(self) -> None:
        src = FixedRateSource()
        assert src.rate_on(date(2020, 1, 1)) == src.rate_on(date(2030, 12, 31))

    @pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
    def test_non_finite_rejected(self, bad: float) -> None:
        with pytest.raises(RiskFreeError, match="must be finite"):
            FixedRateSource(rate=bad)

    @pytest.mark.parametrize("bad", [-0.001, 1.0, 2.5])
    def test_out_of_range_rejected(self, bad: float) -> None:
        with pytest.raises(RiskFreeError, match=r"must lie in \[0.0, 1.0\)"):
            FixedRateSource(rate=bad)

    def test_frozen(self) -> None:
        src = FixedRateSource()
        with pytest.raises(dataclasses.FrozenInstanceError):
            src.rate = 0.1  # type: ignore[misc]

    def test_satisfies_protocol(self) -> None:
        assert isinstance(FixedRateSource(), RiskFreeSource)


class TestRiskFreeRateStamp:
    def test_frozen_value_object(self) -> None:
        stamp = RiskFreeRate(rate=0.065, source="fixed", version=1)
        with pytest.raises(dataclasses.FrozenInstanceError):
            stamp.rate = 0.1  # type: ignore[misc]


_HAPPY_CSV = (
    "date,yield,tenor_days\n"
    "2026-03-02,0.070,91\n"  # deliberately unsorted — sorted at load
    "2026-01-01,0.065,91\n"
    "\n"  # a blank interior line is tolerated
    "2026-06-01,0.060,182\n"
)


class TestLoadRiskFreeCsv:
    def test_happy_path_step_function(self, tmp_path: Path) -> None:
        src = load_risk_free_csv(_write_csv(tmp_path, _HAPPY_CSV))
        assert src.source == "csv:risk_free_daily.csv"
        assert src.dates == (date(2026, 1, 1), date(2026, 3, 2), date(2026, 6, 1))
        # Before the first row: HONEST None, never extrapolated backwards.
        assert src.rate_on(date(2025, 12, 31)) is None
        # Exact first date.
        first = src.rate_on(date(2026, 1, 1))
        assert first is not None
        assert first.rate == pytest.approx(money_market_to_continuous(0.065, 91))
        assert first.source == "csv:risk_free_daily.csv"
        assert first.version == RISK_FREE_SOURCE_VERSION
        # Between rows: the step holds the PREVIOUS row (no interpolation).
        mid = src.rate_on(date(2026, 2, 15))
        assert mid is not None
        assert mid.rate == pytest.approx(money_market_to_continuous(0.065, 91))
        # On the second row's date the step advances.
        second = src.rate_on(date(2026, 3, 2))
        assert second is not None
        assert second.rate == pytest.approx(money_market_to_continuous(0.070, 91))
        # After the last row: the last step extends forward.
        tail = src.rate_on(date(2027, 1, 1))
        assert tail is not None
        assert tail.rate == pytest.approx(money_market_to_continuous(0.060, 182))

    def test_whitespace_tolerated(self, tmp_path: Path) -> None:
        body = " date , yield , tenor_days \n 2026-01-01 , 0.065 , 91 \n"
        src = load_risk_free_csv(_write_csv(tmp_path, body))
        got = src.rate_on(date(2026, 1, 2))
        assert got is not None
        assert got.rate == pytest.approx(money_market_to_continuous(0.065, 91))

    def test_missing_file_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(RiskFreeError, match="cannot read risk-free CSV"):
            load_risk_free_csv(tmp_path / "absent.csv")

    def test_empty_file_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(RiskFreeError, match="must start with header"):
            load_risk_free_csv(_write_csv(tmp_path, ""))

    def test_wrong_header_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(RiskFreeError, match="must start with header"):
            load_risk_free_csv(_write_csv(tmp_path, "day,rate\n2026-01-01,0.065\n"))

    def test_header_only_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(RiskFreeError, match="no data rows"):
            load_risk_free_csv(_write_csv(tmp_path, "date,yield,tenor_days\n"))

    def test_wrong_cell_count_rejected(self, tmp_path: Path) -> None:
        body = "date,yield,tenor_days\n2026-01-01,0.065\n"
        with pytest.raises(RiskFreeError, match="expected 3 cells"):
            load_risk_free_csv(_write_csv(tmp_path, body))

    def test_bad_date_rejected(self, tmp_path: Path) -> None:
        body = "date,yield,tenor_days\n01/01/2026,0.065,91\n"
        with pytest.raises(RiskFreeError, match="bad date"):
            load_risk_free_csv(_write_csv(tmp_path, body))

    def test_bad_yield_rejected(self, tmp_path: Path) -> None:
        body = "date,yield,tenor_days\n2026-01-01,six-percent,91\n"
        with pytest.raises(RiskFreeError, match="bad yield"):
            load_risk_free_csv(_write_csv(tmp_path, body))

    def test_bad_tenor_rejected(self, tmp_path: Path) -> None:
        body = "date,yield,tenor_days\n2026-01-01,0.065,91.5\n"
        with pytest.raises(RiskFreeError, match="bad tenor_days"):
            load_risk_free_csv(_write_csv(tmp_path, body))

    def test_duplicate_date_rejected(self, tmp_path: Path) -> None:
        body = "date,yield,tenor_days\n2026-01-01,0.065,91\n2026-01-01,0.066,91\n"
        with pytest.raises(RiskFreeError, match="duplicate date 2026-01-01"):
            load_risk_free_csv(_write_csv(tmp_path, body))

    def test_negative_converted_rate_rejected(self, tmp_path: Path) -> None:
        # A negative money-market yield converts to a negative r_cc, which
        # the [0.0, 1.0) band rejects with the row context.
        body = "date,yield,tenor_days\n2026-01-01,-0.01,91\n"
        with pytest.raises(RiskFreeError, match=r"row 2: rate must lie in"):
            load_risk_free_csv(_write_csv(tmp_path, body))

    def test_absurd_converted_rate_rejected(self, tmp_path: Path) -> None:
        # 500% simple yield converts to ln(6) ≈ 1.79 >= 1.0 → rejected.
        body = "date,yield,tenor_days\n2026-01-01,5.0,365\n"
        with pytest.raises(RiskFreeError, match=r"row 2: rate must lie in"):
            load_risk_free_csv(_write_csv(tmp_path, body))

    def test_satisfies_protocol(self, tmp_path: Path) -> None:
        src = load_risk_free_csv(_write_csv(tmp_path, _HAPPY_CSV))
        assert isinstance(src, RiskFreeSource)

    @given(
        offsets=st.lists(st.integers(min_value=0, max_value=720), min_size=1, max_size=20),
        probe=st.integers(min_value=-30, max_value=760),
    )
    def test_property_step_lookup_matches_linear_scan(self, offsets: list[int], probe: int) -> None:
        # The bisect step-function must agree with the obvious O(rows) scan.
        base = date(2024, 1, 1).toordinal()
        dates = tuple(date.fromordinal(base + off) for off in sorted(set(offsets)))
        rates = tuple(0.01 + 0.0001 * i for i in range(len(dates)))
        src = CsvRateSource(dates=dates, rates=rates, source="csv:prop")
        query = date.fromordinal(base + probe)
        expected: float | None = None
        for d, r in zip(dates, rates, strict=True):
            if d <= query:
                expected = r
        got = src.rate_on(query)
        if expected is None:
            assert got is None
        else:
            assert got is not None
            assert got.rate == expected
            assert got.source == "csv:prop"
            assert got.version == RISK_FREE_SOURCE_VERSION


class TestCsvRateSourceConstructionValidation:
    """DEC-GREEKS-CORRECTNESS-AUDIT-001 RF-2: construction-time validation.

    A hand-built :class:`CsvRateSource` used to bypass the ``[0, 1)`` band
    entirely (only the blessed :func:`load_risk_free_csv` factory validated),
    so a rogue ``rate=-0.5`` flowed into the kernel and was STORED. The
    ``__post_init__`` now makes the documented "can never silently reach a
    Greeks solve" promise true for EVERY construction path.
    """

    def test_out_of_band_rate_rejected_at_construction(self) -> None:
        with pytest.raises(RiskFreeError, match=r"rates\[0\].*must lie in"):
            CsvRateSource(dates=(date(2026, 1, 1),), rates=(-0.5,), source="csv:rogue")

    def test_non_finite_rate_rejected_at_construction(self) -> None:
        with pytest.raises(RiskFreeError, match=r"rates\[1\].*must be finite"):
            CsvRateSource(
                dates=(date(2026, 1, 1), date(2026, 1, 2)),
                rates=(0.06, math.nan),
                source="csv:rogue",
            )

    def test_length_mismatch_rejected(self) -> None:
        with pytest.raises(RiskFreeError, match="1 dates but 2 rates"):
            CsvRateSource(dates=(date(2026, 1, 1),), rates=(0.06, 0.07), source="csv:rogue")

    def test_unsorted_dates_rejected(self) -> None:
        with pytest.raises(RiskFreeError, match="strictly ascending"):
            CsvRateSource(
                dates=(date(2026, 1, 2), date(2026, 1, 1)),
                rates=(0.06, 0.07),
                source="csv:rogue",
            )

    def test_duplicate_dates_rejected(self) -> None:
        with pytest.raises(RiskFreeError, match="strictly ascending"):
            CsvRateSource(
                dates=(date(2026, 1, 1), date(2026, 1, 1)),
                rates=(0.06, 0.07),
                source="csv:rogue",
            )

    def test_blessed_loader_output_still_constructs(self, tmp_path: Path) -> None:
        # load_risk_free_csv output already satisfies the invariants — the
        # validation is a no-behaviour-change for the blessed path.
        src = load_risk_free_csv(_write_csv(tmp_path, _HAPPY_CSV))
        rebuilt = CsvRateSource(dates=src.dates, rates=src.rates, source=src.source)
        assert rebuilt.rate_on(_D) == src.rate_on(_D)


class TestCsvEncodingFailure:
    """DEC-GREEKS-CORRECTNESS-AUDIT-001 RF-3: UnicodeDecodeError → RiskFreeError.

    A UTF-16 / binary CSV (an Excel/PowerShell save) used to escape the
    documented ``Raises RiskFreeError`` contract with a raw
    :class:`UnicodeDecodeError`; the loader now maps the encoding failure into
    the same loud :class:`RiskFreeError` as every other broken-CSV shape.
    """

    def test_utf16_csv_raises_riskfree_error(self, tmp_path: Path) -> None:
        path = tmp_path / "utf16.csv"
        path.write_text("date,yield,tenor_days\n2026-01-01,0.065,91\n", encoding="utf-16")
        with pytest.raises(RiskFreeError, match="cannot read risk-free CSV"):
            load_risk_free_csv(path)

    def test_binary_garbage_raises_riskfree_error(self, tmp_path: Path) -> None:
        path = tmp_path / "binary.csv"
        path.write_bytes(b"date,yield,tenor_days\n2026-01-01,0.06\xe9,91\n")
        with pytest.raises(RiskFreeError, match="encoding"):
            load_risk_free_csv(path)
