"""Unit tests — :mod:`brutex.crossverify.day_export` (read → gate → decide → render)."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

pytest.importorskip("polars")

import polars as pl
from brutex.calendars.nse import SessionType, TradingSession
from brutex.crossverify.canonical import CanonicalIdentity, canonical_identity
from brutex.crossverify.day_export import (
    CANONICAL_COLUMNS,
    SETTLE_RETRY_REASONS_INDEX,
    _decide,
    export_symbol,
    make_day_frame_reader,
    read_day_bars,
)
from brutex.data.models import CandleBar

from tests.fakes.fake_completeness_gate import FakeVerdict, ScriptedClassifier

if TYPE_CHECKING:
    from brutex.crossverify.day_export import DayFrameReader

_DAY = date(2026, 7, 10)
_SESSION = TradingSession(
    date=_DAY,
    session_type=SessionType.REGULAR,
    open_time_ist=time(9, 15),
    close_time_ist=time(15, 30),
)


def _identity(
    segment: str = "INDEX", symbol: str = "NIFTY", exch: str = "NSE"
) -> CanonicalIdentity:
    identity = canonical_identity(exchange=exch, segment=segment, lake_symbol=symbol)
    assert identity is not None
    return identity


_FUT = _identity("FNO", "NSE-NIFTY-30Jul26-FUT")


def _minute_utc(day: date, hh: int, mm: int) -> datetime:
    ist_minutes = hh * 60 + mm - 330
    return datetime(day.year, day.month, day.day, tzinfo=UTC) + timedelta(minutes=ist_minutes)


def _bar(hh: int, mm: int, *, close: str = "100.00", oi: int | None = None) -> CandleBar:
    price = Decimal(close)
    return CandleBar(
        timestamp=_minute_utc(_DAY, hh, mm),
        open=price,
        high=price,
        low=price,
        close=price,
        volume=10,
        open_interest=oi,
    )


def _seed_month(
    lake: Path,
    identity: CanonicalIdentity,
    rows: list[tuple[datetime, float, int, int | None]],
    *,
    extra_overlay: bool = False,
) -> Path:
    """Write a real month parquet with the canonical 7 columns (+ optional overlay)."""
    target = (
        lake
        / "bars"
        / identity.exchange
        / identity.lake_segment
        / identity.lake_symbol
        / "1minute"
        / f"{rows[0][0].year:04d}"
        / f"{rows[0][0].month:02d}.parquet"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, object] = {
        "timestamp": [r[0] for r in rows],
        "open": [r[1] for r in rows],
        "high": [r[1] for r in rows],
        "low": [r[1] for r in rows],
        "close": [r[1] for r in rows],
        "volume": [r[2] for r in rows],
        "open_interest": [r[3] for r in rows],
    }
    if extra_overlay:  # Greeks-overlay shape: extra nullable columns after OI
        data["iv"] = [None for _ in rows]
        data["delta"] = [None for _ in rows]
    frame = pl.DataFrame(data).with_columns(
        pl.col("timestamp").dt.replace_time_zone(None).dt.replace_time_zone("UTC"),
        pl.col("volume").cast(pl.Int64),
        pl.col("open_interest").cast(pl.Int64),
    )
    frame.write_parquet(target)
    return target


class TestReadDayBars:
    def test_absent_partition_reads_empty(self, tmp_path: Path) -> None:
        assert (
            read_day_bars(
                lake_root=tmp_path, exchange="NSE", segment="INDEX", symbol="NIFTY", day=_DAY
            )
            == []
        )

    def test_reads_seven_columns_by_name_with_greeks_overlay(self, tmp_path: Path) -> None:
        identity = _FUT
        _seed_month(
            tmp_path,
            identity,
            [(_minute_utc(_DAY, 9, 15), 101.5, 7, 42)],
            extra_overlay=True,
        )
        bars = read_day_bars(
            lake_root=tmp_path,
            exchange=identity.exchange,
            segment=identity.lake_segment,
            symbol=identity.lake_symbol,
            day=_DAY,
        )
        assert len(bars) == 1
        assert bars[0].open == Decimal("101.5")
        assert bars[0].volume == 7
        assert bars[0].open_interest == 42
        assert CANONICAL_COLUMNS == (
            "timestamp",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "open_interest",
        )

    def test_ist_day_filter_excludes_other_days(self, tmp_path: Path) -> None:
        identity = _identity()
        _seed_month(
            tmp_path,
            identity,
            [
                (_minute_utc(_DAY, 9, 15), 100.0, 1, None),
                (_minute_utc(_DAY - timedelta(days=1), 9, 15), 99.0, 1, None),
            ],
        )
        bars = read_day_bars(
            lake_root=tmp_path, exchange="NSE", segment="INDEX", symbol="NIFTY", day=_DAY
        )
        assert [b.open for b in bars] == [Decimal("100.0")]

    def test_keep_first_dedup(self, tmp_path: Path) -> None:
        identity = _identity()
        ts = _minute_utc(_DAY, 9, 15)
        _seed_month(tmp_path, identity, [(ts, 100.0, 1, None), (ts, 999.0, 9, None)])
        bars = read_day_bars(
            lake_root=tmp_path, exchange="NSE", segment="INDEX", symbol="NIFTY", day=_DAY
        )
        assert len(bars) == 1
        assert bars[0].close == Decimal("100.0")  # FIRST physical copy wins

    def test_naive_stored_timestamp_is_retagged_utc(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from brutex.data import lake_day_glob

        naive_frame = pl.DataFrame(
            {
                "timestamp": [_minute_utc(_DAY, 9, 15).replace(tzinfo=None)],
                "open": [100.0],
                "high": [100.0],
                "low": [100.0],
                "close": [100.0],
                "volume": [1],
                "open_interest": [None],
            }
        ).with_columns(pl.col("open_interest").cast(pl.Int64))
        monkeypatch.setattr(
            lake_day_glob, "read_day_intraday_frame", lambda base, day, *, symbol: naive_frame
        )
        bars = read_day_bars(
            lake_root=tmp_path, exchange="NSE", segment="INDEX", symbol="NIFTY", day=_DAY
        )
        assert bars[0].timestamp.tzinfo is UTC

    def test_make_day_frame_reader_binds_lake_root(self, tmp_path: Path) -> None:
        identity = _identity()
        _seed_month(tmp_path, identity, [(_minute_utc(_DAY, 9, 15), 100.0, 1, None)])
        reader = make_day_frame_reader(tmp_path)
        bars = reader(exchange="NSE", segment="INDEX", symbol="NIFTY", day=_DAY)
        assert len(bars) == 1


def _seed_corrupt_month(
    lake: Path, identity: CanonicalIdentity, *, null_open: bool = False, null_volume: bool = False
) -> None:
    """Write a real month parquet with ONE corrupt (null-celled) stored row."""
    target = (
        lake
        / "bars"
        / identity.exchange
        / identity.lake_segment
        / identity.lake_symbol
        / "1minute"
        / f"{_DAY.year:04d}"
        / f"{_DAY.month:02d}.parquet"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    frame = pl.DataFrame(
        {
            "timestamp": [_minute_utc(_DAY, 9, 15)],
            "open": [None if null_open else 100.0],
            "high": [100.0],
            "low": [100.0],
            "close": [100.0],
            "volume": [None if null_volume else 1],
            "open_interest": [None],
        }
    ).with_columns(
        pl.col("timestamp").dt.replace_time_zone(None).dt.replace_time_zone("UTC"),
        pl.col("open").cast(pl.Float64),
        pl.col("volume").cast(pl.Int64),
        pl.col("open_interest").cast(pl.Int64),
    )
    frame.write_parquet(target)


class TestCorruptStoredRowIsolation:
    """F3 (round-1 contract review): ONE corrupt stored row (a null OHLC
    cell → ``decimal.InvalidOperation``; a null volume → ``TypeError``)
    withholds THAT symbol as ``read_error`` — the run continues, never a
    whole-~750-symbol crash."""

    def test_null_open_cell_raises_invalid_operation(self, tmp_path: Path) -> None:
        from decimal import InvalidOperation

        _seed_corrupt_month(tmp_path, _identity(), null_open=True)
        with pytest.raises(InvalidOperation):
            read_day_bars(
                lake_root=tmp_path, exchange="NSE", segment="INDEX", symbol="NIFTY", day=_DAY
            )

    def test_null_volume_cell_raises_type_error(self, tmp_path: Path) -> None:
        _seed_corrupt_month(tmp_path, _identity(), null_volume=True)
        with pytest.raises(TypeError):
            read_day_bars(
                lake_root=tmp_path, exchange="NSE", segment="INDEX", symbol="NIFTY", day=_DAY
            )

    @pytest.mark.parametrize("corruption", ["null_open", "null_volume"])
    def test_corrupt_row_withholds_symbol_only_run_continues(
        self, tmp_path: Path, corruption: str
    ) -> None:
        corrupt = _identity()  # NIFTY carries the corrupt row
        _seed_corrupt_month(tmp_path, corrupt, **{corruption: True})
        healthy = _identity("CASH", "RELIANCE")  # the sibling stays healthy
        _seed_month(tmp_path, healthy, [(_minute_utc(_DAY, 9, 15), 100.0, 1, None)])
        reader = make_day_frame_reader(tmp_path)
        withheld = export_symbol(
            identity=corrupt,
            day=_DAY,
            session=_SESSION,
            reader=reader,
            classifier=ScriptedClassifier(),
        )
        assert withheld.disposition == "withheld"
        assert withheld.reason == "read_error"
        assert withheld.csv_bytes is None
        # The run continues: the healthy sibling still publishes.
        published = export_symbol(
            identity=healthy,
            day=_DAY,
            session=_SESSION,
            reader=reader,
            classifier=ScriptedClassifier(default=FakeVerdict(distinct_minutes=1)),
        )
        assert published.disposition == "publish"
        assert published.csv_bytes is not None


class TestDecide:
    @pytest.mark.parametrize(
        ("instrument_class", "reason", "incomplete", "covers_today", "expected"),
        [
            ("index_spot", "complete", False, True, "publish"),
            ("cash_spot", "complete", False, True, "publish"),
            ("fut", "complete", False, True, "publish"),
            # Round-2 S4 defense-in-depth: a contract-violating "complete"
            # verdict (no today-coverage, or flagged incomplete) is WITHHELD.
            ("index_spot", "complete", False, False, "withheld"),
            ("cash_spot", "complete", True, True, "withheld"),
            ("fut", "complete", True, False, "withheld"),
            ("index_spot", "no_today_bars", False, True, "blocked"),
            ("cash_spot", "no_today_bars", False, True, "nothing"),
            ("fut", "no_today_bars", False, True, "nothing"),
            ("index_spot", "non_trading", False, False, "nothing"),
            ("index_spot", "abstain_coverage", False, True, "withheld"),
            ("index_spot", "count_short", True, True, "withheld"),
            ("index_spot", "count_excess", True, True, "withheld"),
            ("index_spot", "last_bar_incomplete", True, True, "withheld"),
            ("cash_spot", "volume_all_zero", True, True, "withheld"),
            ("fut", "oi_null_settling", True, True, "withheld"),
            ("fut", "volume_all_zero", True, True, "withheld"),
            # Forward-compat arms — never publish on an unknown state.
            ("fut", "some_future_reason", True, True, "withheld"),
            ("fut", "some_future_reason", False, True, "nothing"),
        ],
    )
    def test_decision_table(
        self,
        instrument_class: str,
        reason: str,
        incomplete: bool,
        covers_today: bool,
        expected: str,
    ) -> None:
        assert (
            _decide(
                instrument_class=instrument_class,
                reason=reason,
                incomplete=incomplete,
                covers_today=covers_today,
            )
            == expected
        )

    def test_settle_retry_reason_sets(self) -> None:
        # INDEX only — the FUT twin left with the futures legs
        # (DEC-CROSSVERIFY-SPOT-ONLY-001, MIGRATION.md #1788).
        assert frozenset({"count_short", "last_bar_incomplete"}) == SETTLE_RETRY_REASONS_INDEX
        from brutex.crossverify import day_export

        assert not hasattr(day_export, "SETTLE_RETRY_REASONS_FUT")


def _static_reader(bars: list[CandleBar]) -> DayFrameReader:
    def _reader(*, exchange: str, segment: str, symbol: str, day: date) -> list[CandleBar]:
        _ = (exchange, segment, symbol, day)
        return bars

    return _reader


class TestExportSymbol:
    def test_publish_path_populates_bytes_and_sha(self) -> None:
        bars = [_bar(9, 15), _bar(9, 16)]
        classifier = ScriptedClassifier(default=FakeVerdict(distinct_minutes=2))
        export = export_symbol(
            identity=_identity(),
            day=_DAY,
            session=_SESSION,
            reader=_static_reader(bars),
            classifier=classifier,
        )
        assert export.disposition == "publish"
        assert export.reason == "complete"
        assert export.csv_bytes is not None
        assert export.sha256 is not None
        assert export.row_count == 2
        assert classifier.calls == [(2, "index_spot")]

    @pytest.mark.parametrize(
        ("verdict", "expected_disposition", "expected_reason"),
        [
            (FakeVerdict(reason="no_today_bars", incomplete=False), "blocked", "no_today_bars"),
            (FakeVerdict(reason="count_short", incomplete=True), "withheld", "count_short"),
        ],
    )
    def test_non_publish_paths_carry_no_bytes(
        self, verdict: FakeVerdict, expected_disposition: str, expected_reason: str
    ) -> None:
        export = export_symbol(
            identity=_identity(),
            day=_DAY,
            session=_SESSION,
            reader=_static_reader([]),
            classifier=ScriptedClassifier(default=verdict),
        )
        assert export.disposition == expected_disposition
        assert export.reason == expected_reason
        # The never-partial law: csv_bytes is None ⇔ disposition != "publish".
        assert export.csv_bytes is None
        assert export.sha256 is None
        assert export.row_count == 0

    @pytest.mark.parametrize(
        "raiser",
        [
            OSError("disk gone"),
            pl.exceptions.ComputeError("corrupt parquet"),
            ValueError("malformed stored bar"),
        ],
    )
    def test_reader_raise_withholds_read_error(self, raiser: Exception) -> None:
        def _bad_reader(*, exchange: str, segment: str, symbol: str, day: date) -> list[CandleBar]:
            raise raiser

        export = export_symbol(
            identity=_identity(),
            day=_DAY,
            session=_SESSION,
            reader=_bad_reader,
            classifier=ScriptedClassifier(),
        )
        assert export.disposition == "withheld"
        assert export.reason == "read_error"
        assert export.csv_bytes is None

    def test_classifier_type_error_withholds_gate_signature_drift(self) -> None:
        # Round-3 X3 (refuter R2-2): a signature-drifted gate module's
        # classify raises TypeError from the call — fail-closed PER SYMBOL:
        # withheld under the NAMED reason + one WARN naming the symbol,
        # never an escaping crash that kills the whole ~750-symbol run.
        import structlog.testing

        def _drifted(**kwargs: object) -> FakeVerdict:
            raise TypeError("classify_today_completeness() got an unexpected keyword 'bars_today'")

        with structlog.testing.capture_logs() as logs:
            export = export_symbol(
                identity=_identity(),
                day=_DAY,
                session=_SESSION,
                reader=_static_reader([_bar(9, 15)]),
                classifier=_drifted,  # type: ignore[arg-type]
            )
        assert export.disposition == "withheld"
        assert export.reason == "gate_signature_drift"
        assert export.csv_bytes is None
        assert export.sha256 is None
        events = [e for e in logs if e["event"] == "crossverify.gate_signature_drift"]
        assert len(events) == 1
        assert events[0]["symbol"] == repr("NIFTY")

    def test_signature_drifted_real_shaped_module_withholds_not_crashes(self) -> None:
        # The end-to-end X3 shape: bind_production_classifier over a module
        # whose classify SIGNATURE drifted (renamed kwarg) — the adapter's
        # classify call raises TypeError and export_symbol withholds the
        # symbol as gate_signature_drift instead of crashing mid-run.
        import enum
        import types

        from brutex.crossverify.gate_seam import bind_production_classifier

        class _Cls(enum.Enum):
            INDEX_SPOT = "index_spot"
            CASH_SPOT = "cash_spot"
            FUT = "fut"

        module = types.ModuleType("drifted_gate")

        def classify_today_completeness(*, bars: object, **rest: object) -> object:
            raise AssertionError("unreachable — the kwarg mismatch raises first")

        module.classify_today_completeness = classify_today_completeness  # type: ignore[attr-defined]
        module.InstrumentClass = _Cls  # type: ignore[attr-defined]
        classifier = bind_production_classifier(lambda _name: module)
        export = export_symbol(
            identity=_identity(),
            day=_DAY,
            session=_SESSION,
            reader=_static_reader([_bar(9, 15)]),
            classifier=classifier,
        )
        assert export.disposition == "withheld"
        assert export.reason == "gate_signature_drift"

    def test_enum_value_drifted_module_withholds_gate_signature_drift(self) -> None:
        # Round-4 polish P2, the end-to-end shape: bind over a module whose
        # enum VALUES renamed ("index_spot" gone) — pre-fix the seam's
        # InstrumentClass("index_spot") raised a raw ValueError PAST this
        # loop's TypeError drift net (an escaping mid-run crash); the seam
        # now folds it to TypeError, so the symbol is withheld as
        # gate_signature_drift. day_export's except is deliberately NOT
        # widened to bare ValueError — a gate-internal bug must propagate.
        import enum
        import types

        from brutex.crossverify.gate_seam import bind_production_classifier

        class _Renamed(enum.Enum):
            INDEX_SPOT = "idx_spot"  # the VALUE drifted away from "index_spot"
            CASH_SPOT = "cash_spot"
            FUT = "fut"

        module = types.ModuleType("value_drifted_gate")

        def classify_today_completeness(**kwargs: object) -> object:
            raise AssertionError("unreachable — the enum conversion raises first")

        module.classify_today_completeness = classify_today_completeness  # type: ignore[attr-defined]
        module.InstrumentClass = _Renamed  # type: ignore[attr-defined]
        classifier = bind_production_classifier(lambda _name: module)
        export = export_symbol(
            identity=_identity(),
            day=_DAY,
            session=_SESSION,
            reader=_static_reader([_bar(9, 15)]),
            classifier=classifier,
        )
        assert export.disposition == "withheld"
        assert export.reason == "gate_signature_drift"
        assert export.csv_bytes is None

    def test_genuine_code_bug_propagates(self) -> None:
        def _buggy_reader(
            *, exchange: str, segment: str, symbol: str, day: date
        ) -> list[CandleBar]:
            raise KeyError("a code bug, not a data problem")

        with pytest.raises(KeyError):
            export_symbol(
                identity=_identity(),
                day=_DAY,
                session=_SESSION,
                reader=_buggy_reader,
                classifier=ScriptedClassifier(),
            )

    @pytest.mark.parametrize(
        "verdict",
        [
            FakeVerdict(reason="complete", covers_today=False, incomplete=False),
            FakeVerdict(reason="complete", covers_today=True, incomplete=True),
        ],
    )
    def test_contract_violating_complete_verdict_withheld_named(self, verdict: FakeVerdict) -> None:
        # Round-2 S4: `covers_today` consulted as a defense-in-depth belt —
        # a gate promising "complete" while flagging no-coverage /
        # incompleteness is WITHHELD under the NAMED reason (never a publish,
        # never a confusing "complete" on a withheld record).
        export = export_symbol(
            identity=_identity(),
            day=_DAY,
            session=_SESSION,
            reader=_static_reader([_bar(9, 15)]),
            classifier=ScriptedClassifier(default=verdict),
        )
        assert export.disposition == "withheld"
        assert export.reason == "gate_contract_violation"
        assert export.csv_bytes is None
        assert export.sha256 is None

    def test_covering_complete_verdict_still_publishes(self) -> None:
        # The other S4 arm: the honest complete verdict (covers_today=True,
        # incomplete=False) publishes exactly as before the belt.
        export = export_symbol(
            identity=_identity(),
            day=_DAY,
            session=_SESSION,
            reader=_static_reader([_bar(9, 15)]),
            classifier=ScriptedClassifier(
                default=FakeVerdict(reason="complete", covers_today=True, distinct_minutes=1)
            ),
        )
        assert export.disposition == "publish"
        assert export.reason == "complete"
        assert export.csv_bytes is not None

    def test_distinct_minutes_belt_refuses_mismatch(self) -> None:
        export = export_symbol(
            identity=_identity(),
            day=_DAY,
            session=_SESSION,
            reader=_static_reader([_bar(9, 15)]),
            classifier=ScriptedClassifier(default=FakeVerdict(distinct_minutes=375)),
        )
        assert export.disposition == "withheld"
        assert export.reason.startswith("render_refused:")
        assert "distinct_minutes" in export.reason

    def test_index_expected_minutes_belt_refuses_mismatch(self) -> None:
        export = export_symbol(
            identity=_identity(),
            day=_DAY,
            session=_SESSION,
            reader=_static_reader([_bar(9, 15)]),
            classifier=ScriptedClassifier(
                default=FakeVerdict(distinct_minutes=1, expected_minutes=375)
            ),
        )
        assert export.disposition == "withheld"
        assert "expected_minutes" in export.reason

    def test_distinct_none_skips_belt_then_cash_publishes(self) -> None:
        export = export_symbol(
            identity=_identity("CASH", "RELIANCE"),
            day=_DAY,
            session=_SESSION,
            reader=_static_reader([_bar(9, 15)]),
            classifier=ScriptedClassifier(default=FakeVerdict(distinct_minutes=None)),
        )
        assert export.disposition == "publish"
        assert export.row_count == 1

    def test_render_refusal_null_oi_fut_withheld(self) -> None:
        # The kill-switched-gate tripwire: a lying "complete" verdict over a
        # NULL-OI futures day still never publishes.
        export = export_symbol(
            identity=_FUT,
            day=_DAY,
            session=_SESSION,
            reader=_static_reader([_bar(9, 15, oi=None)]),
            classifier=ScriptedClassifier(default=FakeVerdict(distinct_minutes=1)),
        )
        assert export.disposition == "withheld"
        assert export.reason.startswith("render_refused:")
        assert "NULL oi" in export.reason

    def test_admitted_but_empty_rows_refuse(self) -> None:
        export = export_symbol(
            identity=_identity("CASH", "RELIANCE"),
            day=_DAY,
            session=_SESSION,
            reader=_static_reader([]),
            classifier=ScriptedClassifier(default=FakeVerdict(distinct_minutes=None)),
        )
        assert export.disposition == "withheld"
        assert export.reason.startswith("render_refused:")

    def test_fut_publish_with_real_oi(self) -> None:
        export = export_symbol(
            identity=_FUT,
            day=_DAY,
            session=_SESSION,
            reader=_static_reader([_bar(9, 15, oi=0), _bar(9, 16, oi=100)]),
            classifier=ScriptedClassifier(default=FakeVerdict(distinct_minutes=2)),
        )
        assert export.disposition == "publish"
        assert export.csv_bytes is not None
        assert b",0\n" in export.csv_bytes  # oi=0 renders 0
