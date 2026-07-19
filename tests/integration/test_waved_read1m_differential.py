"""Wave-D W-1 read differential — flag-ON ≡ flag-OFF on real-writer lakes.

The real-wheel parity gate for the ``BRUTEX_LAKE_WAVED_RUST`` read seam
(Wave-D design §E-5): lakes are built through the PRODUCTION writers
(:class:`~brutex.data.sink.ParquetBarSink` — plus raw poison plants for the
gate arms), then the SAME ``make_lake_read_1m`` reader runs flag-OFF (the
Python oracle) and flag-ON in the post-burn-in steady state (the crate frame
served RAW — ``reset_lake_rust_waved_for_tests(remaining=0)``, so an armed
shape gate cannot cushion a divergence by shipping the oracle frame) and the
results are compared at DTYPE-EXACT frame strength, exception
type+message strength (the gate arms), WARN-event strength (the bad-tick
arm), and derived-partition BYTE strength (the whole-materialise A/B).

Skips cleanly when the installed wheel predates the Wave-D API (the
lockstep law: crate and repo move together).
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

pl = pytest.importorskip("polars")
pytest.importorskip("pyarrow")
_crate = pytest.importorskip("brutex_lake")
if not callable(getattr(_crate, "waved_read_1m_ipc", None)):
    pytest.skip("installed brutex_lake wheel lacks the Wave-D API", allow_module_level=True)

from brutex.data import lake_rust_waved as lrw  # noqa: E402
from brutex.data.errors import ResampleInputError  # noqa: E402
from brutex.data.models import CandleBar  # noqa: E402
from brutex.data.parquet_writer import ParquetWriter  # noqa: E402
from brutex.data.sink import ParquetBarSink  # noqa: E402
from brutex.data.timeframe_materialize import (  # noqa: E402
    PrebuildKind,
    has_stored_1m_bars,
    make_lake_read_1m,
    materialize_lake_timeframes,
    month_partition_files,
)
from structlog.testing import capture_logs  # noqa: E402

if TYPE_CHECKING:
    from collections.abc import Iterator

_EXCHANGE = "NSE"
_SEGMENT = "INDEX"
_SYMBOL = "NIFTY"

#: 09:15 IST == 03:45 UTC — the session open (a Monday, 2021-01-04).
_SESSION_OPEN_UTC = datetime(2021, 1, 4, 3, 45, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _flag_off_and_fresh(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Default every test to flag OFF + a fresh facade view."""
    monkeypatch.delenv(lrw.LAKE_WAVED_RUST_ENV, raising=False)
    lrw.reset_lake_rust_waved_for_tests()
    yield
    lrw.reset_lake_rust_waved_for_tests()


def _bar(i: int, *, close: float | None = None, oi: int | None = 0) -> CandleBar:
    price = Decimal(str(100.0 + i if close is None else close))
    return CandleBar(
        timestamp=_SESSION_OPEN_UTC + timedelta(minutes=i),
        open=price,
        high=price,
        low=price,
        close=price,
        volume=10 + i,
        open_interest=oi,
    )


def _write_bars(
    lake_root: Path,
    bars: list[CandleBar],
    *,
    segment: str = _SEGMENT,
    symbol: str = _SYMBOL,
) -> None:
    sink = ParquetBarSink(writer=ParquetWriter(lake_root=lake_root))
    sink.append(exchange=_EXCHANGE, segment=segment, symbol=symbol, interval="1minute", bars=bars)


def _reader(
    lake_root: Path,
    *,
    segment: str = _SEGMENT,
    symbol: str = _SYMBOL,
    with_open_interest: bool = False,
    months: list[tuple[int, int]] | None = None,
) -> Any:
    return make_lake_read_1m(
        lake_root=lake_root,
        exchange=_EXCHANGE,
        segment=segment,
        symbol=symbol,
        with_open_interest=with_open_interest,
        months=months,
    )


def _flag_on_steady(monkeypatch: pytest.MonkeyPatch) -> None:
    """Arm the seam in the post-burn-in steady state (crate frames served raw)."""
    monkeypatch.setenv(lrw.LAKE_WAVED_RUST_ENV, "1")
    lrw.reset_lake_rust_waved_for_tests(remaining=0)


def _assert_frames_dtype_exact(off: Any, on: Any) -> None:
    assert off.schema == on.schema, f"schema drift: {off.schema} != {on.schema}"
    assert off.equals(on), "flag-ON frame diverges from the flag-OFF oracle"


class TestReadFrameParity:
    def test_whole_tree_spot_read_is_frame_identical(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Two months of bars so the tree walk spans partitions.
        bars = [_bar(i) for i in range(30)]
        bars += [
            CandleBar(
                timestamp=_SESSION_OPEN_UTC + timedelta(days=31, minutes=i),
                open=Decimal("200"),
                high=Decimal("201"),
                low=Decimal("199"),
                close=Decimal("200"),
                volume=5,
                open_interest=0,
            )
            for i in range(5)
        ]
        _write_bars(tmp_path, bars)
        reader = _reader(tmp_path)
        off = reader()
        _flag_on_steady(monkeypatch)
        on = reader()
        _assert_frames_dtype_exact(off, on)
        assert on.height == 35
        assert lrw.rust_waved_disabled() is False

    def test_months_arm_read_is_frame_identical(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_bars(tmp_path, [_bar(i) for i in range(10)])
        reader = _reader(tmp_path, months=[(2021, 1)])
        off = reader()
        _flag_on_steady(monkeypatch)
        on = reader()
        _assert_frames_dtype_exact(off, on)

    def test_fno_open_interest_read_is_frame_identical(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bars = [_bar(i, oi=1_000 + i) for i in range(3)]
        bars.append(_bar(3, oi=None))  # null OI carried, never fabricated
        _write_bars(tmp_path, bars, segment="FNO", symbol="NSE-NIFTY-29Jul21-FUT")
        reader = _reader(
            tmp_path,
            segment="FNO",
            symbol="NSE-NIFTY-29Jul21-FUT",
            with_open_interest=True,
        )
        off = reader()
        _flag_on_steady(monkeypatch)
        on = reader()
        _assert_frames_dtype_exact(off, on)
        assert "open_interest" in on.columns

    def test_burn_in_first_read_compares_clean_and_decrements(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_bars(tmp_path, [_bar(i) for i in range(5)])
        monkeypatch.setenv(lrw.LAKE_WAVED_RUST_ENV, "1")
        lrw.reset_lake_rust_waved_for_tests()  # fresh burn-in budget
        frame = _reader(tmp_path)()
        assert frame.height == 5
        assert lrw.rust_waved_disabled() is False, "real-lake burn-in must not autorevert"
        assert lrw._BURN_IN_REMAINING[lrw.SURFACE_READ_1M] == lrw.BURN_IN_SAMPLES - 1


class TestFlagOffNeverRoutes:
    def test_flag_off_never_enters_the_seam(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The never-routes spy (the PR-1 bomb idiom): flag OFF must not call
        # the facade routing function at all — the builder resolves the
        # flag per read and takes the verbatim Python arm.
        def _bomb(**_kwargs: object) -> Any:
            raise AssertionError("flag OFF must never route through the waved seam")

        monkeypatch.setattr(lrw, "read_1m_via_rust", _bomb)
        _write_bars(tmp_path, [_bar(i) for i in range(3)])
        frame = _reader(tmp_path)()
        assert frame.height == 3


class TestGateExceptionParity:
    def _plant_raw(self, lake_root: Path, symbol: str, frame: Any) -> None:
        dest = lake_root / "bars" / _EXCHANGE / _SEGMENT / symbol / "1minute" / "2021"
        dest.mkdir(parents=True)
        frame.write_parquet(dest / "01.parquet")

    def _poison_frame(self, ts: Any) -> Any:
        n = ts.len()
        return pl.DataFrame(
            {
                "timestamp": ts,
                "open": [100.0] * n,
                "high": [101.0] * n,
                "low": [99.0] * n,
                "close": [100.5] * n,
                "volume": [10] * n,
                "open_interest": [0] * n,
            }
        )

    def test_ts_range_poison_raises_identically_on_both_arms(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ts = pl.Series("timestamp", [2**62], dtype=pl.Int64).cast(
            pl.Datetime(time_unit="us", time_zone="UTC")
        )
        self._plant_raw(tmp_path, "POISONTS", self._poison_frame(ts))
        reader = _reader(tmp_path, symbol="POISONTS")
        with pytest.raises(ResampleInputError) as off_exc:
            reader()
        _flag_on_steady(monkeypatch)
        with pytest.raises(ResampleInputError) as on_exc:
            reader()
        assert str(on_exc.value) == str(off_exc.value)
        assert "outside the" in str(on_exc.value)

    def test_price_insane_poison_raises_identically_on_both_arms(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ts = pl.Series("timestamp", [_SESSION_OPEN_UTC.replace(tzinfo=None)]).cast(
            pl.Datetime(time_unit="us", time_zone="UTC")
        )
        poison = self._poison_frame(ts).with_columns(
            pl.lit(90.0).alias("high")  # high < low — broken ordering
        )
        self._plant_raw(tmp_path, "POISONPRICE", poison)
        reader = _reader(tmp_path, symbol="POISONPRICE")
        with pytest.raises(ResampleInputError) as off_exc:
            reader()
        _flag_on_steady(monkeypatch)
        with pytest.raises(ResampleInputError) as on_exc:
            reader()
        assert str(on_exc.value) == str(off_exc.value)
        assert "price-insane" in str(on_exc.value)

    def test_empty_partition_raises_identically_before_the_seam(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The source decision runs BEFORE the seam — one shared raise site.
        reader = _reader(tmp_path, symbol="NEVERPULLED")
        with pytest.raises(ValueError) as off_exc:
            reader()
        _flag_on_steady(monkeypatch)
        with pytest.raises(ValueError) as on_exc:
            reader()
        assert str(on_exc.value) == str(off_exc.value)
        assert type(on_exc.value) is type(off_exc.value)


class TestBadTickWarnParity:
    def test_spike_lake_drops_and_warns_identically(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 100 → 1000 (x10 spike) → 101: the spike drops, the successor is
        # judged against the HELD baseline 100 — both arms, same WARN fields.
        bars = [
            _bar(0, close=100.0),
            _bar(1, close=1_000.0),
            _bar(2, close=101.0),
        ]
        _write_bars(tmp_path, bars)
        reader = _reader(tmp_path)
        with capture_logs() as off_logs:
            off = reader()
        _flag_on_steady(monkeypatch)
        with capture_logs() as on_logs:
            on = reader()
        _assert_frames_dtype_exact(off, on)
        assert on.height == 2, "the x10 spike must be screened out"
        off_warns = [e for e in off_logs if e["event"] == "feed.bad_tick_skipped"]
        on_warns = [e for e in on_logs if e["event"] == "feed.bad_tick_skipped"]
        assert off_warns == on_warns
        assert len(on_warns) == 1
        assert on_warns[0]["source"] == f"prebuild:{_EXCHANGE}/{_SEGMENT}/{_SYMBOL}/1minute"
        assert on_warns[0]["session"] == "2021-01-04"
        assert on_warns[0]["skipped"] == 1
        assert on_warns[0]["first_prev_close"] == 100.0
        assert on_warns[0]["first_close"] == 1000.0

    def test_kill_switch_off_keeps_the_spike_on_both_arms(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bars = [_bar(0, close=100.0), _bar(1, close=1_000.0)]
        _write_bars(tmp_path, bars)
        monkeypatch.setenv("BRUTEX_BAD_TICK_SCREEN", "0")
        reader = _reader(tmp_path)
        off = reader()
        _flag_on_steady(monkeypatch)
        on = reader()
        _assert_frames_dtype_exact(off, on)
        assert on.height == 2, "the ONE kill switch flips both arms together"


class TestMaterializeByteParity:
    @staticmethod
    def _derived_tree_state(lake_root: Path) -> dict[str, str]:
        """``relpath → sha256`` over every derived (non-1minute) partition."""
        out: dict[str, str] = {}
        for path in sorted((lake_root / "bars").rglob("*.parquet")):
            rel = path.relative_to(lake_root).as_posix()
            if "/1minute/" in rel:
                continue
            out[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
        return out

    def test_full_materialise_writes_byte_identical_derived_partitions(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Twin lakes, identical 1-minute input; OFF materialises lake A,
        # ON (steady state) materialises lake B — every derived partition
        # (19 intraday TFs + the 1day) must be BYTE-identical.
        bars = [_bar(i) for i in range(60)]
        lake_off = tmp_path / "off"
        lake_on = tmp_path / "on"
        _write_bars(lake_off, bars)
        _write_bars(lake_on, bars)

        def _materialise(lake_root: Path) -> None:
            result = materialize_lake_timeframes(
                lake_root=lake_root,
                exchange=_EXCHANGE,
                segment=_SEGMENT,
                symbol=_SYMBOL,
                kind=PrebuildKind.SPOT,
                include_daily=True,
                daily_before=date(2021, 1, 5),
                incremental=False,
            )
            assert result.total_bars > 0

        _materialise(lake_off)
        _flag_on_steady(monkeypatch)
        _materialise(lake_on)
        assert lrw.rust_waved_disabled() is False
        off_state = self._derived_tree_state(lake_off)
        on_state = self._derived_tree_state(lake_on)
        assert off_state, "the materialise must have written derived partitions"
        assert off_state == on_state


class TestDriftPinnedTwins:
    def test_has_stored_twin_matches_python_over_fixture_shapes(self, tmp_path: Path) -> None:
        # Absent / empty / populated / parquet-named-dir / file-at-base —
        # the crate twin must agree with the Python walk on every shape.
        base = tmp_path / "bars" / _EXCHANGE / _SEGMENT / _SYMBOL / "1minute"
        shapes: list[Path] = [base]
        assert _crate.waved_has_stored_1m_bars(str(base)) is has_stored_1m_bars(
            tmp_path, _EXCHANGE, _SEGMENT, _SYMBOL
        )
        _write_bars(tmp_path, [_bar(0)])
        assert _crate.waved_has_stored_1m_bars(str(base)) is has_stored_1m_bars(
            tmp_path, _EXCHANGE, _SEGMENT, _SYMBOL
        )
        plant = tmp_path / "bars" / _EXCHANGE / _SEGMENT / "PLANT" / "1minute"
        (plant / "2021" / "01.parquet").mkdir(parents=True)  # dataset-dir plant
        shapes.append(plant)
        assert _crate.waved_has_stored_1m_bars(str(plant)) is has_stored_1m_bars(
            tmp_path, _EXCHANGE, _SEGMENT, "PLANT"
        )
        file_base = tmp_path / "bars" / _EXCHANGE / _SEGMENT / "FILEBASE" / "1minute"
        file_base.parent.mkdir(parents=True)
        file_base.write_bytes(b"x")
        assert _crate.waved_has_stored_1m_bars(str(file_base)) is has_stored_1m_bars(
            tmp_path, _EXCHANGE, _SEGMENT, "FILEBASE"
        )

    def test_month_files_twin_matches_python(self, tmp_path: Path) -> None:
        _write_bars(tmp_path, [_bar(0)])
        base = tmp_path / "bars" / _EXCHANGE / _SEGMENT / _SYMBOL / "1minute"
        months = [(2021, 1), (2021, 1), (2021, 2), (2020, 12)]
        assert _crate.waved_month_partition_files(str(base), months) == month_partition_files(
            base, months
        )
        assert _crate.waved_month_partition_files(str(base), []) == month_partition_files(base, [])
