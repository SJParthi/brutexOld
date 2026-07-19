"""W-6a ENDGAME read gate — Rust-arm determinism + frozen expectations.

THE FLIP LANDED (DEC-RUST-LAKE-WAVED-001 / Wave-D design §E-5): the Python
``_collect`` read core is DELETED, so the pre-endgame flag-ON≡flag-OFF
differential has no Python arm left. This suite is the read seam's
real-wheel verification in the endgame shape: lakes are built through the
PRODUCTION writers (:class:`~brutex.data.sink.ParquetBarSink` — plus raw
poison plants for the gate arms), read through the ONE Rust-backed
``make_lake_read_1m``, and pinned at

* DTYPE-EXACT frame determinism (two reads → ``.equals`` + schema-equal);
* FROZEN frame expectations (heights, columns, the bad-tick WARN's exact
  field values — captured while the Python-parity differential was green);
* exception type+message strength on the gate arms (ts-range poison →
  the quarantining ``ResampleInputError``; price-insane → likewise;
  never-pulled → the typed no-source refusal);
* derived-partition BYTE determinism (twin identical lakes through the
  full ``materialize_lake_timeframes`` → byte-identical derived trees);
* the drift-pinned crate twins (``waved_has_stored_1m_bars`` /
  ``waved_month_partition_files``) vs their KEPT public Python helpers.

Requires the built ``brutex_lake`` wheel — a production PREREQUISITE since
W-6a; skips honestly when absent (the unit suites own the wheel-free
typed-refusal story).
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
if not callable(getattr(_crate, "waved_endgame_witness", None)):
    pytest.skip(
        "installed brutex_lake wheel predates the W-6a endgame API", allow_module_level=True
    )

from brutex.data import lake_rust_waved as lrw  # noqa: E402
from brutex.data.errors import (  # noqa: E402
    LakeRustWavedFlagRemovedError,
    ResampleInputError,
)
from brutex.data.models import CandleBar  # noqa: E402
from brutex.data.parquet_writer import ParquetWriter  # noqa: E402
from brutex.data.sidecar_read import NotRegularFileError  # noqa: E402
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
def _endgame_arm(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The ONE Rust path: retired flag unset, module state reset each test."""
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


def _assert_deterministic(reader: Any) -> Any:
    """Two reads through the ONE Rust path → schema-equal + ``.equals``."""
    first = reader()
    second = reader()
    assert first.schema == second.schema, f"schema drift: {first.schema} != {second.schema}"
    assert first.equals(second), "the Rust read is not deterministic"
    return second


class TestReadFrameDeterminism:
    def test_whole_tree_spot_read(self, tmp_path: Path) -> None:
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
        frame = _assert_deterministic(_reader(tmp_path))
        # FROZEN: 35 rows, ascending timestamps, no open_interest column.
        assert frame.height == 35
        assert frame["ts_utc"].is_sorted()
        assert "open_interest" not in frame.columns
        assert float(frame["close"][0]) == 100.0

    def test_months_arm_read(self, tmp_path: Path) -> None:
        _write_bars(tmp_path, [_bar(i) for i in range(10)])
        frame = _assert_deterministic(_reader(tmp_path, months=[(2021, 1)]))
        assert frame.height == 10
        # A months window with no partition is the honest LOUD refusal.
        empty_months = _reader(tmp_path, months=[(2020, 12)])
        with pytest.raises(ValueError, match="no stored 1minute bars"):
            empty_months()

    def test_fno_open_interest_read(self, tmp_path: Path) -> None:
        bars = [_bar(i, oi=1_000 + i) for i in range(3)]
        bars.append(_bar(3, oi=None))  # null OI carried, never fabricated
        _write_bars(tmp_path, bars, segment="FNO", symbol="NSE-NIFTY-29Jul21-FUT")
        frame = _assert_deterministic(
            _reader(
                tmp_path,
                segment="FNO",
                symbol="NSE-NIFTY-29Jul21-FUT",
                with_open_interest=True,
            )
        )
        # FROZEN: the OI column rides along with the null preserved.
        assert "open_interest" in frame.columns
        assert frame["open_interest"].to_list() == [1000, 1001, 1002, None]


class TestRetiredFlagAtTheReader:
    @pytest.mark.parametrize("raw", ["0", "1", "junk"])
    def test_set_flag_refuses_before_any_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw: str
    ) -> None:
        """The retired-flag law at the READ seam: ANY set value refuses."""
        _write_bars(tmp_path, [_bar(0)])
        monkeypatch.setenv(lrw.LAKE_WAVED_RUST_ENV, raw)
        with pytest.raises(LakeRustWavedFlagRemovedError, match="no longer flag-gated"):
            _reader(tmp_path)()


class TestGateExceptions:
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

    def test_ts_range_poison_raises_the_quarantining_error(self, tmp_path: Path) -> None:
        """The #1919 gate through the crate: a 2**62-µs stamp is the typed
        ``ResampleInputError`` — deterministic message, never a panic."""
        ts = pl.Series("timestamp", [2**62], dtype=pl.Int64).cast(
            pl.Datetime(time_unit="us", time_zone="UTC")
        )
        self._plant_raw(tmp_path, "POISONTS", self._poison_frame(ts))
        reader = _reader(tmp_path, symbol="POISONTS")
        with pytest.raises(ResampleInputError) as first:
            reader()
        with pytest.raises(ResampleInputError) as second:
            reader()
        assert str(second.value) == str(first.value)
        assert "outside the" in str(second.value)

    def test_price_insane_poison_raises_the_quarantining_error(self, tmp_path: Path) -> None:
        ts = pl.Series("timestamp", [_SESSION_OPEN_UTC.replace(tzinfo=None)]).cast(
            pl.Datetime(time_unit="us", time_zone="UTC")
        )
        poison = self._poison_frame(ts).with_columns(
            pl.lit(90.0).alias("high")  # high < low — broken ordering
        )
        self._plant_raw(tmp_path, "POISONPRICE", poison)
        reader = _reader(tmp_path, symbol="POISONPRICE")
        with pytest.raises(ResampleInputError) as first:
            reader()
        with pytest.raises(ResampleInputError) as second:
            reader()
        assert str(second.value) == str(first.value)
        assert "price-insane" in str(second.value)

    def test_never_pulled_symbol_raises_the_no_source_error(self, tmp_path: Path) -> None:
        # The source decision runs BEFORE the seam — the #1792 typed arm.
        from brutex.data.timeframe_materialize import NoStored1mBarsError

        reader = _reader(tmp_path, symbol="NEVERPULLED")
        with pytest.raises(NoStored1mBarsError) as first:
            reader()
        with pytest.raises(NoStored1mBarsError) as second:
            reader()
        assert str(second.value) == str(first.value)


class TestInodeGateWholeTree:
    """D-32: a ``*.parquet``-NAMED DIRECTORY on the whole-tree arm refuses
    with the typed :class:`NotRegularFileError`
    (DEC-LAKE-READ-INODE-GUARD-001/-002 — the pre-lane loud posture) instead
    of being descended into and silently serving its member files (the
    pre-fix walker's polars-dataset-dir emulation)."""

    def _plant_dir(self, lake_root: Path) -> Path:
        plant = (
            lake_root / "bars" / _EXCHANGE / _SEGMENT / _SYMBOL / "1minute" / "2021" / "02.parquet"
        )
        plant.mkdir(parents=True)
        return plant

    def test_planted_parquet_named_dir_refuses_typed_and_cause_chained(
        self, tmp_path: Path
    ) -> None:
        _write_bars(tmp_path, [_bar(0)])
        self._plant_dir(tmp_path)
        reader = _reader(tmp_path)
        with pytest.raises(NotRegularFileError) as excinfo:
            reader()
        err = excinfo.value
        # The crate message verbatim — the D-31 real-OSError channel.
        assert "not a regular file" in str(err)
        assert "02.parquet" in str(err)
        # Cause-chained: the crate crossed a REAL OSError (the type-level
        # token); the facade re-raise carries its message byte-for-byte.
        assert isinstance(err.__cause__, OSError)
        assert not isinstance(err.__cause__, NotRegularFileError)
        assert str(err) == str(err.__cause__)

    def test_dir_with_member_files_refuses_never_serves_members(self, tmp_path: Path) -> None:
        # Pre-fix the walker DESCENDED into the plant and served this valid
        # member as the symbol's own bars — the silent-serve now refuses.
        _write_bars(tmp_path, [_bar(0)])
        plant = self._plant_dir(tmp_path)
        member = pl.DataFrame(
            {
                "timestamp": pl.Series("timestamp", [_SESSION_OPEN_UTC.replace(tzinfo=None)]).cast(
                    pl.Datetime(time_unit="us", time_zone="UTC")
                ),
                "open": [999.0],
                "high": [999.0],
                "low": [999.0],
                "close": [999.0],
                "volume": [1],
                "open_interest": [0],
            }
        )
        member.write_parquet(plant / "member.parquet")
        with pytest.raises(NotRegularFileError, match="not a regular file"):
            _reader(tmp_path)()


class TestBadTickWarnFrozen:
    def test_spike_lake_drops_and_warns_with_frozen_fields(self, tmp_path: Path) -> None:
        # 100 → 1000 (x10 spike) → 101: the spike drops, the successor is
        # judged against the HELD baseline 100 — the WARN's exact field
        # values are the frozen pre-delete parity record.
        bars = [
            _bar(0, close=100.0),
            _bar(1, close=1_000.0),
            _bar(2, close=101.0),
        ]
        _write_bars(tmp_path, bars)
        reader = _reader(tmp_path)
        with capture_logs() as logs:
            frame = reader()
        assert frame.height == 2, "the x10 spike must be screened out"
        warns = [e for e in logs if e["event"] == "feed.bad_tick_skipped"]
        assert len(warns) == 1
        assert warns[0]["source"] == f"prebuild:{_EXCHANGE}/{_SEGMENT}/{_SYMBOL}/1minute"
        assert warns[0]["session"] == "2021-01-04"
        assert warns[0]["skipped"] == 1
        assert warns[0]["first_prev_close"] == 100.0
        assert warns[0]["first_close"] == 1000.0

    def test_kill_switch_off_keeps_the_spike(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bars = [_bar(0, close=100.0), _bar(1, close=1_000.0)]
        _write_bars(tmp_path, bars)
        monkeypatch.setenv("BRUTEX_BAD_TICK_SCREEN", "0")
        frame = _assert_deterministic(_reader(tmp_path))
        assert frame.height == 2, "the ONE kill switch is honored at the crate read"


class TestMaterializeByteDeterminism:
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
        self, tmp_path: Path
    ) -> None:
        # Twin lakes, identical 1-minute input, both materialised through
        # the ONE Rust-backed read — every derived partition (19 intraday
        # TFs + the 1day) must be BYTE-identical across the twin builds.
        bars = [_bar(i) for i in range(60)]
        lake_a = tmp_path / "a"
        lake_b = tmp_path / "b"
        _write_bars(lake_a, bars)
        _write_bars(lake_b, bars)

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

        _materialise(lake_a)
        _materialise(lake_b)
        state_a = self._derived_tree_state(lake_a)
        state_b = self._derived_tree_state(lake_b)
        assert state_a, "the materialise must have written derived partitions"
        assert state_a == state_b


class TestDriftPinnedTwins:
    def test_has_stored_twin_matches_python_over_fixture_shapes(self, tmp_path: Path) -> None:
        # Absent / empty / populated / parquet-named-dir / file-at-base —
        # the crate twin must agree with the Python walk on every shape.
        base = tmp_path / "bars" / _EXCHANGE / _SEGMENT / _SYMBOL / "1minute"
        assert _crate.waved_has_stored_1m_bars(str(base)) is has_stored_1m_bars(
            tmp_path, _EXCHANGE, _SEGMENT, _SYMBOL
        )
        _write_bars(tmp_path, [_bar(0)])
        assert _crate.waved_has_stored_1m_bars(str(base)) is has_stored_1m_bars(
            tmp_path, _EXCHANGE, _SEGMENT, _SYMBOL
        )
        plant = tmp_path / "bars" / _EXCHANGE / _SEGMENT / "PLANT" / "1minute"
        (plant / "2021" / "01.parquet").mkdir(parents=True)  # dataset-dir plant
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
