"""Independent-leg primitives vs hand-computed vectors + hypothesis references.

DEC-OPERATOR-ACCEPTANCE-VERIFY-001 (MIGRATION.md #1797). The fenced module is
the ONE independent-recompute surface — these pins prove its hand math against
brute-force references, incl. the ±0.005 half-up boundary, the HLC3
318660≠320000 golden (#899) and the #1601 bucket-anchor 15:27→15:29 law.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction
from pathlib import Path

import pytest
from brutex.acceptance import independent as ind
from hypothesis import given
from hypothesis import strategies as st

IST = timedelta(hours=5, minutes=30)


class TestHalfUp2dp:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("0.005", "0.01"),
            ("-0.005", "-0.01"),
            ("1.004999", "1.00"),
            ("1.005", "1.01"),
            ("27932.799", "27932.80"),
            ("0", "0.00"),
        ],
    )
    def test_boundaries(self, raw: str, expected: str) -> None:
        got = ind.half_up_2dp(Fraction(raw))
        assert got == Fraction(expected)

    @given(st.fractions(min_value=-10_000, max_value=10_000))
    def test_matches_decimal_reference(self, x: Fraction) -> None:
        # Independent oracle: Decimal ROUND_HALF_UP over the EXACT rational.
        reference = (Decimal(x.numerator) / Decimal(x.denominator)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        assert ind.half_up_2dp(x) == Fraction(str(reference))


class TestInrFormat:
    @pytest.mark.parametrize(
        ("rupees", "expected"),
        [
            (Fraction("27932.80"), "₹27,932.80"),
            (Fraction("-123.45"), "₹-123.45"),
            (Fraction(0), "₹0.00"),
            (Fraction("1000000000.75"), "₹1,000,000,000.75"),
            (Fraction("0.05"), "₹0.05"),
        ],
    )
    def test_matches_production_law(self, rupees: Fraction, expected: str) -> None:
        # The vectors above are the PRODUCTION format_inr outputs (verified
        # samples) — the hand formatter must reproduce them without importing it.
        assert ind.format_inr_2dp(rupees) == expected


class TestPct:
    def test_pct_exact(self) -> None:
        assert ind.pct_chng(Fraction(100), Fraction(110)) == Fraction(10)

    def test_zero_baseline_none(self) -> None:
        assert ind.pct_chng(Fraction(0), Fraction(1)) is None


class TestIstHelpers:
    def test_aware(self) -> None:
        ts = datetime(2026, 7, 9, 3, 45, tzinfo=UTC)
        assert ind.ist_minute_of_day(ts) == 555  # 09:15
        assert ind.ist_date_of(ts) == date(2026, 7, 9)

    def test_naive_treated_utc(self) -> None:
        ts = datetime(2026, 7, 9, 3, 45)
        assert ind.ist_minute_of_day(ts) == 555
        assert ind.ist_date_of(ts) == date(2026, 7, 9)


class TestBucketAsofLaw:
    def test_1601_completed_bucket(self) -> None:
        # 3-minute @ 15:27 (anchor) resolves FORWARD to 15:29 — the completed
        # bucket's final minute, capped at the session's last minute.
        assert ind.bucket_asof_cutoff_minute(3, "15:27") == 15 * 60 + 29

    def test_anchor_mid_session(self) -> None:
        assert ind.bucket_asof_cutoff_minute(3, "09:21") == 9 * 60 + 23

    def test_non_anchor_keeps_raw_minute(self) -> None:
        assert ind.bucket_asof_cutoff_minute(3, "09:22") == 9 * 60 + 22

    def test_one_minute_identity(self) -> None:
        assert ind.bucket_asof_cutoff_minute(1, "10:00") == 10 * 60

    def test_blank_and_daily_none(self) -> None:
        assert ind.bucket_asof_cutoff_minute(3, None) is None
        assert ind.bucket_asof_cutoff_minute(375, "09:15") is None

    def test_pre_open_pick_is_not_an_anchor(self) -> None:
        assert ind.bucket_asof_cutoff_minute(3, "09:00") == 9 * 60


def _write_1m(dir_: Path, rows: list[tuple[datetime, float, float, float, float, int]]) -> None:
    import polars as pl

    by_month: dict[tuple[int, int], list] = {}
    for ts, o, h, lo, c, v in rows:
        by_month.setdefault((ts.year, ts.month), []).append((ts, o, h, lo, c, v))
    for (year, month), cell in by_month.items():
        out = dir_ / "1minute" / f"{year:04d}"
        out.mkdir(parents=True, exist_ok=True)
        pl.DataFrame(
            {
                "timestamp": [r[0] for r in cell],
                "open": [r[1] for r in cell],
                "high": [r[2] for r in cell],
                "low": [r[3] for r in cell],
                "close": [r[4] for r in cell],
                "volume": [r[5] for r in cell],
            }
        ).write_parquet(out / f"{month:02d}.parquet")


class TestRead1mBars:
    def test_keep_first_dedup_law(self, tmp_path: Path) -> None:
        ts = datetime(2026, 7, 9, 3, 45, tzinfo=UTC)
        _write_1m(
            tmp_path,
            [
                (ts, 100.0, 101.0, 99.0, 100.5, 10),
                (ts, 999.0, 999.0, 999.0, 999.0, 99),  # dup — the FIRST wins
                (ts + timedelta(minutes=1), 100.5, 101.5, 99.5, 101.0, 20),
            ],
        )
        bars = ind.read_1m_bars(tmp_path)
        assert len(bars) == 2
        assert bars[0].close == Fraction("100.5")

    def test_months_bound_by_filename(self, tmp_path: Path) -> None:
        _write_1m(
            tmp_path,
            [
                (datetime(2026, 6, 10, 3, 45, tzinfo=UTC), 1.0, 1.0, 1.0, 1.0, 1),
                (datetime(2026, 7, 9, 3, 45, tzinfo=UTC), 2.0, 2.0, 2.0, 2.0, 2),
            ],
        )
        bars = ind.read_1m_bars(tmp_path, months={(2026, 7)})
        assert [b.volume for b in bars] == [2]

    def test_junk_month_name_skipped_under_bound(self, tmp_path: Path) -> None:
        out = tmp_path / "1minute" / "2026"
        out.mkdir(parents=True)
        (out / "junk.parquet").write_bytes(b"")
        assert ind.read_1m_bars(tmp_path, months={(2026, 7)}) == []

    def test_missing_dir_empty(self, tmp_path: Path) -> None:
        assert ind.read_1m_bars(tmp_path) == []

    def test_naive_timestamps_normalised(self, tmp_path: Path) -> None:
        _write_1m(tmp_path, [(datetime(2026, 7, 9, 3, 45), 1.0, 1.0, 1.0, 1.0, 1)])
        bars = ind.read_1m_bars(tmp_path)
        assert bars[0].ts_utc.tzinfo is not None


class TestHlc3Golden:
    def test_899_golden_318660_not_320000(self, tmp_path: Path) -> None:
        """The DEC-MOVERS-VWAP-VALUE-001 golden: HLC3 turnover ≠ close×volume."""
        day = date(2026, 7, 9)
        rows = [
            (datetime.combine(day, time(3, 45), tzinfo=UTC), 100.0, 101.0, 99.0, 100.0, 10),
            (
                datetime.combine(day, time(3, 46), tzinfo=UTC),
                100.0,
                104.0,
                100.0,
                103.0,
                21,
            ),
        ]
        _write_1m(tmp_path, rows)
        bars = ind.read_1m_bars(tmp_path)
        got = ind.hlc3_value_paisa(bars)
        # bar1 HLC3 = (101+99+100)/3 = 100.00 → 10000 paisa × 10
        # bar2 HLC3 = (104+100+103)/3 = 102.333.. → half-up 102.33 → 10233 × 21
        assert got == 10000 * 10 + 10233 * 21 == 314893
        naive = sum(int(b.close * 100) * b.volume for b in bars)
        assert got != naive  # the close×volume trap the golden exists to kill

    def test_value_crore_display(self) -> None:
        assert ind.value_crore_2dp(318660) == "0.00"
        assert ind.value_crore_2dp(3_186_600_000) == "3.19"
        assert ind.value_crore_2dp(-3_186_600_000) == "-3.19"


class TestSessionAndRows:
    def _bars(self, day: date) -> list[ind.Bar1m]:
        rows = []
        for i in range(5):
            ts = datetime.combine(day, time(3, 45), tzinfo=UTC) + timedelta(minutes=i)
            px = 100.0 + i
            rows.append((ts, px, px + 1, px - 1, px + 0.5, 10))
        return [
            ind.Bar1m(
                ts_utc=r[0],
                open=Fraction(repr(r[1])),
                high=Fraction(repr(r[2])),
                low=Fraction(repr(r[3])),
                close=Fraction(repr(r[4])),
                volume=r[5],
            )
            for r in rows
        ]

    def test_session_bars_cutoff(self) -> None:
        day = date(2026, 7, 9)
        bars = self._bars(day)
        cut = ind.session_bars(bars, day=day, cutoff_minute=555 + 2)
        assert len(cut) == 3

    def test_independent_row_and_prev_close(self) -> None:
        d1, d2 = date(2026, 7, 8), date(2026, 7, 9)
        bars = [*self._bars(d1), *self._bars(d2)]
        row = ind.independent_spot_row("X", bars, day=d2, cutoff_minute=None)
        assert row is not None
        assert row.prev_close_date == d1
        assert row.prev_close == Fraction("104.5")
        assert row.chng == row.close - Fraction("104.5")
        assert row.pct is not None
        assert row.volume == 50
        assert row.ltp_display.startswith("₹")
        assert row.value_cr_display

    def test_no_bar_returns_none(self) -> None:
        bars = self._bars(date(2026, 7, 8))
        assert ind.independent_spot_row("X", bars, day=date(2026, 7, 9), cutoff_minute=None) is None

    def test_no_prior_day_none_baseline(self) -> None:
        d = date(2026, 7, 9)
        row = ind.independent_spot_row("X", self._bars(d), day=d, cutoff_minute=None)
        assert row is not None
        assert row.prev_close is None
        assert row.pct is None
        assert row.chng is None


class TestRank:
    def _row(self, symbol: str, pct: str | None, volume: int, value: int) -> ind.IndependentSpotRow:
        return ind.IndependentSpotRow(
            symbol=symbol,
            close=Fraction(100),
            prev_close=None if pct is None else Fraction(100),
            prev_close_date=None,
            chng=None,
            pct=None if pct is None else Fraction(pct),
            volume=volume,
            value_paisa=value,
        )

    def test_gainers_losers_exclude_no_baseline(self) -> None:
        rows = [
            self._row("A", "5", 1, 1),
            self._row("B", "-3", 1, 1),
            self._row("C", None, 1, 1),
        ]
        assert [r.symbol for r in ind.rank_rows(rows, key="gainers", top_n=5)] == ["A", "B"]
        assert [r.symbol for r in ind.rank_rows(rows, key="losers", top_n=5)] == ["B", "A"]

    def test_volume_value_alphabetical_tie(self) -> None:
        rows = [self._row("B", None, 5, 9), self._row("A", None, 5, 9), self._row("C", None, 9, 1)]
        assert [r.symbol for r in ind.rank_rows(rows, key="volume", top_n=3)] == ["C", "A", "B"]
        assert [r.symbol for r in ind.rank_rows(rows, key="value", top_n=2)] == ["A", "B"]


class TestCalendarHand:
    def test_trading_day_arms(self) -> None:
        assert ind.is_trading_day_hand(date(2026, 7, 9)) is True  # Thursday
        assert ind.is_trading_day_hand(date(2026, 7, 11)) is False  # Saturday
        assert ind.is_trading_day_hand(date(2026, 1, 26)) is False  # Republic Day
        # 2026-02-01 Sunday Budget special session (verified constant)
        assert ind.is_trading_day_hand(date(2026, 2, 1)) is True

    def test_previous_session_hand(self) -> None:
        assert ind.previous_session_hand(date(2026, 7, 13)) == date(2026, 7, 10)

    def test_previous_session_exhausts(self) -> None:
        assert ind.previous_session_hand(date(2026, 7, 13), max_walk=1) is None

    def test_muhurat_counts_as_display_session(self) -> None:
        muhurat = sorted(ind.MUHURAT_SESSIONS)[-1]
        assert ind.is_trading_day_hand(muhurat) is True


class TestFutSymbolHand:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("NSE-NIFTY-30Sep25-FUT", ("NIFTY", date(2025, 9, 30))),
            ("NSE-BAJAJ-AUTO-30Sep25-FUT", ("BAJAJ-AUTO", date(2025, 9, 30))),
            ("NSE-NIFTY-30Sep25-24000-CE", None),
            ("NSE-NIFTY-FUT", None),
            ("NSE-NIFTY-99Zzz25-FUT", None),
            ("NSE-NIFTY-9x9925-FUT", None),
            ("NSE-NIFTY-3xSep25-FUT", None),
            ("NSE-NIFTY-3аSep2-FUT", None),
        ],
    )
    def test_parse_arms(self, name: str, expected: object) -> None:
        assert ind.parse_fut_symbol_hand(name) == expected

    def test_near_month_selection(self) -> None:
        names = [
            "NSE-NIFTY-30Sep25-FUT",
            "NSE-NIFTY-28Oct25-FUT",
            "NSE-NIFTY-26Aug25-FUT",  # expired vs on_date below
            "junk",
        ]
        got = ind.near_month_fut_hand(names, on_date=date(2025, 9, 1))
        assert got == {"NIFTY": "NSE-NIFTY-30Sep25-FUT"}


class TestEnumerate:
    def test_scandir_grammar(self, tmp_path: Path) -> None:
        for seg, sym in (("INDEX", "NIFTY"), ("FNO", "NSE-NIFTY-30Sep25-FUT")):
            (tmp_path / "bars" / "NSE" / seg / sym).mkdir(parents=True)
        (tmp_path / "bars" / "NSE" / "INDEX" / "stray.txt").write_text("x", encoding="utf-8")
        (tmp_path / "bars" / "stray-exchange.txt").write_text("x", encoding="utf-8")
        got = ind.enumerate_spot_symbol_dirs(tmp_path)
        assert [(e, s, y) for e, s, y, _ in got] == [("NSE", "INDEX", "NIFTY")]

    def test_missing_tree(self, tmp_path: Path) -> None:
        assert ind.enumerate_spot_symbol_dirs(tmp_path) == []


class TestGlobMetacharLiteralRead:
    def test_bracket_named_dir_reads_its_literal_file(self, tmp_path: Path) -> None:
        # Round 1 M2 regression (the #1594-R15 class): with real twin dirs
        # X1(111.0) / X2(222.0) / X[12](999.0), the oracle read of X[12] must
        # return the LITERAL file's close — never a glob-matched sibling's.
        import polars as pl

        seg = tmp_path / "bars" / "NSE" / "INDEX"
        stamp = datetime(2026, 7, 9, 3, 45, tzinfo=UTC)
        for name, close in (("X1", 111.0), ("X2", 222.0), ("X[12]", 999.0)):
            root = seg / name / "1minute" / "2026"
            root.mkdir(parents=True)
            pl.DataFrame(
                {
                    "timestamp": [stamp.replace(tzinfo=None)],
                    "open": [close],
                    "high": [close],
                    "low": [close],
                    "close": [close],
                    "volume": [1],
                }
            ).write_parquet(root / "07.parquet")
        bars = ind.read_1m_bars(seg / "X[12]")
        assert [float(b.close) for b in bars] == [999.0]
