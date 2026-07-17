"""Unit tests — :mod:`brutex.crossverify.prebuild_leg` (day-scoped derive composition)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pytest
from brutex.crossverify.canonical import CanonicalIdentity, canonical_identity
from brutex.crossverify.prebuild_leg import (
    FAILED_NAMES_CAP,
    DeriveOutcome,
    run_prebuild_leg,
)
from brutex.data.errors import LakeWriteHalt

if TYPE_CHECKING:
    from pathlib import Path

_DAY = date(2026, 7, 10)
_NOW_IST = lambda: datetime(2026, 7, 10, 16, 30, tzinfo=UTC)  # noqa: E731 — injectable clock


def _identity(segment: str = "INDEX", symbol: str = "NIFTY") -> CanonicalIdentity:
    identity = canonical_identity(exchange="NSE", segment=segment, lake_symbol=symbol)
    assert identity is not None
    return identity


_FUT = _identity("FNO", "NSE-NIFTY-30Jul26-FUT")


@dataclass
class _Spies:
    materialize_calls: list[dict[str, Any]] = field(default_factory=list)
    derive_calls: list[dict[str, Any]] = field(default_factory=list)
    read_factory_calls: list[dict[str, Any]] = field(default_factory=list)
    write_factory_calls: list[dict[str, Any]] = field(default_factory=list)

    def materialize(self, **kwargs: Any) -> object:
        self.materialize_calls.append(kwargs)
        return object()

    def derive_daily(self, **kwargs: Any) -> object:
        self.derive_calls.append(kwargs)
        # Exercise the builders so the read/write factory spies see the shapes.
        for target in kwargs["targets"]:
            kwargs["build_read_1m"](target)
            kwargs["build_write_1day"](target)
        return object()

    def read_window_factory(self, **kwargs: Any) -> object:
        self.read_factory_calls.append(kwargs)
        return lambda: None

    def write_factory(self, **kwargs: Any) -> object:
        self.write_factory_calls.append(kwargs)
        return lambda bars: 0


def _run(coordinates: list[CanonicalIdentity], spies: _Spies | None = None) -> DeriveOutcome:
    spies = spies or _Spies()
    return run_prebuild_leg(
        lake_root=_LAKE,
        day=_DAY,
        coordinates=coordinates,
        now_ist=_NOW_IST,
        materialize=spies.materialize,
        derive_daily=spies.derive_daily,
        read_window_factory=spies.read_window_factory,
        write_daily_factory=spies.write_factory,
    )


_LAKE: Path  # bound by the fixture below


@pytest.fixture(autouse=True)
def _bind_lake(tmp_path: Path) -> None:
    global _LAKE  # noqa: PLW0603 — test-local module binding
    _LAKE = tmp_path


class TestCallShapes:
    def test_materialize_pins_months_no_daily_no_incremental(self) -> None:
        from brutex.data.timeframe_materialize import MATERIALIZABLE_TFS, PrebuildKind

        spies = _Spies()
        outcome = _run([_identity()], spies)
        assert outcome == DeriveOutcome(derived=1, failed=0, failed_names=())
        call = spies.materialize_calls[0]
        # months= structurally forces no-skip/no-stamp upstream (the partial-
        # derive-must-never-stamp invariant BY CONSTRUCTION); include_daily
        # must be False (months= + include_daily=True RAISES upstream).
        assert call["months"] == {(2026, 7)}
        assert call["include_daily"] is False
        assert call["incremental"] is False
        assert call["tfs"] == MATERIALIZABLE_TFS
        assert call["kind"] is PrebuildKind.SPOT
        assert (call["exchange"], call["segment"], call["symbol"]) == ("NSE", "INDEX", "NIFTY")

    def test_fut_coordinate_uses_futures_kind_and_oi(self) -> None:
        from brutex.data.fno_daily_resample import resample_1m_to_fno_daily_positional
        from brutex.data.timeframe_materialize import PrebuildKind

        spies = _Spies()
        _run([_FUT], spies)
        assert spies.materialize_calls[0]["kind"] is PrebuildKind.FUTURES
        assert spies.read_factory_calls[0]["include_oi"] is True
        assert spies.derive_calls[0]["resample_fn"] is resample_1m_to_fno_daily_positional

    def test_spot_coordinate_no_oi_no_fno_resampler(self) -> None:
        spies = _Spies()
        _run([_identity("CASH", "RELIANCE")], spies)
        assert spies.read_factory_calls[0]["include_oi"] is False
        assert spies.derive_calls[0]["resample_fn"] is None

    def test_daily_target_padded_one_day_each_side(self) -> None:
        spies = _Spies()
        _run([_identity()], spies)
        target = spies.derive_calls[0]["targets"][0]
        assert target.start == _DAY - timedelta(days=1)
        assert target.end == _DAY + timedelta(days=1)
        assert spies.read_factory_calls[0]["start"] == target.start
        assert spies.read_factory_calls[0]["end"] == target.end

    def test_daily_cutoff_from_injected_clock(self) -> None:
        from brutex.calendars.nse import daily_derive_cutoff

        spies = _Spies()
        _run([_identity()], spies)
        assert spies.derive_calls[0]["before"] == daily_derive_cutoff(_NOW_IST())


class TestFailureIsolation:
    def test_per_symbol_failure_counted_never_blocks(self) -> None:
        spies = _Spies()
        boom = _identity("CASH", "BOOM")

        def _materialize(**kwargs: Any) -> object:
            if kwargs["symbol"] == "BOOM":
                raise RuntimeError("one bad symbol")
            return spies.materialize(**kwargs)

        outcome = run_prebuild_leg(
            lake_root=_LAKE,
            day=_DAY,
            coordinates=[boom, _identity()],
            now_ist=_NOW_IST,
            materialize=_materialize,
            derive_daily=spies.derive_daily,
            read_window_factory=spies.read_window_factory,
            write_daily_factory=spies.write_factory,
        )
        assert outcome.derived == 1
        assert outcome.failed == 1
        assert outcome.failed_names == ("BOOM",)

    def test_failed_names_capped_and_warns_capped(self) -> None:
        from brutex.crossverify.prebuild_leg import MAX_FAILURE_WARNS

        # More failures than BOTH caps: the report list caps at 10 names, the
        # WARN stream caps at 20 (the gated-prebuild posture) — counts stay true.
        coords = [_identity("CASH", f"S{i:02d}") for i in range(MAX_FAILURE_WARNS + 2)]

        def _materialize(**kwargs: Any) -> object:
            raise RuntimeError("all bad")

        outcome = run_prebuild_leg(
            lake_root=_LAKE,
            day=_DAY,
            coordinates=coords,
            now_ist=_NOW_IST,
            materialize=_materialize,
            derive_daily=lambda **kw: None,
            read_window_factory=lambda **kw: lambda: None,
            write_daily_factory=lambda **kw: lambda bars: 0,
        )
        assert outcome.failed == len(coords)
        assert len(outcome.failed_names) == FAILED_NAMES_CAP

    def test_lake_write_halt_reraises(self) -> None:
        def _materialize(**kwargs: Any) -> object:
            raise LakeWriteHalt("disk_full")

        with pytest.raises(LakeWriteHalt):
            run_prebuild_leg(
                lake_root=_LAKE,
                day=_DAY,
                coordinates=[_identity()],
                now_ist=_NOW_IST,
                materialize=_materialize,
                derive_daily=lambda **kw: None,
                read_window_factory=lambda **kw: lambda: None,
                write_daily_factory=lambda **kw: lambda bars: 0,
            )


class TestRealComposition:
    """The §6 composition against the REAL primitives on a tiny real lake."""

    def _seed(self, lake: Path, identity: CanonicalIdentity, *, oi: bool) -> None:
        from brutex.data.models import CandleBar
        from brutex.data.parquet_writer import ParquetWriter
        from brutex.data.sink import ParquetBarSink

        base = datetime(2026, 7, 10, 3, 45, tzinfo=UTC)  # 09:15 IST
        bars = [
            CandleBar(
                timestamp=base + timedelta(minutes=i),
                open=Decimal("100.00"),
                high=Decimal("101.00"),
                low=Decimal("99.00"),
                close=Decimal("100.50"),
                volume=10 + i,
                open_interest=500 if oi else None,
            )
            for i in range(3)
        ]
        sink = ParquetBarSink(writer=ParquetWriter(lake_root=lake))
        sink.append(
            exchange=identity.exchange,
            segment=identity.lake_segment,
            symbol=identity.lake_symbol,
            interval="1minute",
            bars=bars,
        )

    def test_real_derive_writes_tfs_and_1day_and_never_stamps(self, tmp_path: Path) -> None:
        pytest.importorskip("polars")
        spot = _identity()
        fut = _FUT
        self._seed(tmp_path, spot, oi=False)
        self._seed(tmp_path, fut, oi=True)
        outcome = run_prebuild_leg(
            lake_root=tmp_path,
            day=_DAY,
            coordinates=[spot, fut],
            now_ist=_NOW_IST,
        )
        assert outcome == DeriveOutcome(derived=2, failed=0, failed_names=())
        # Derived intraday TFs landed month-bounded…
        assert (tmp_path / "bars/NSE/INDEX/NIFTY/2minute/2026/07.parquet").exists()
        assert (tmp_path / "bars/NSE/FNO/NSE-NIFTY-30Jul26-FUT/2minute/2026/07.parquet").exists()
        # …the derived 1day landed (the completed session is before the
        # post-close cutoff)…
        assert (tmp_path / "bars/NSE/INDEX/NIFTY/1day/2026/07.parquet").exists()
        # …and NO derive stamp was written (a partial derive must never stamp).
        assert not (tmp_path / ".derive_stamps").exists()


class TestConversionPanicQuarantine:
    """DEC-SPOT-DERIVE-TS-RANGE-HARDENING-001 — the #1594 round-18 guard idiom.

    FAILS-BEFORE: ``pyo3_runtime.PanicException`` (a BaseException — polars'
    out-of-datetime-range Rust→Python conversion panic) sailed past the bare
    ``except Exception`` per-symbol quarantine and aborted the WHOLE leg
    (executed repro: every symbol after the poisoned one was skipped).
    """

    def test_panic_exception_is_quarantined_per_symbol(self) -> None:
        pl = pytest.importorskip("polars")
        spies = _Spies()
        poisoned = _identity("INDEX", "NIFTY")
        healthy = _identity("CASH", "RELIANCE")

        def _materialize(**kwargs: Any) -> object:
            if kwargs["symbol"] == "NIFTY":
                raise pl.exceptions.PanicException(
                    "called `Result::unwrap()` on an `Err` value: OverflowError"
                )
            return spies.materialize(**kwargs)

        outcome = run_prebuild_leg(
            lake_root=_LAKE,
            day=_DAY,
            coordinates=[poisoned, healthy],
            now_ist=_NOW_IST,
            materialize=_materialize,
            derive_daily=spies.derive_daily,
            read_window_factory=spies.read_window_factory,
            write_daily_factory=spies.write_factory,
        )
        # The ONE symbol is counted; the leg COMPLETES; the later symbol runs.
        assert outcome.derived == 1
        assert outcome.failed == 1
        assert outcome.failed_names == ("NIFTY",)
        assert spies.materialize_calls[0]["symbol"] == "RELIANCE"

    def test_keyboard_interrupt_still_propagates(self) -> None:
        """The guard is Exception + the two conversion escapes — NEVER a
        blanket BaseException: an operator Ctrl-C must stop the leg."""

        def _materialize(**kwargs: Any) -> object:
            raise KeyboardInterrupt

        with pytest.raises(KeyboardInterrupt):
            run_prebuild_leg(
                lake_root=_LAKE,
                day=_DAY,
                coordinates=[_identity()],
                now_ist=_NOW_IST,
                materialize=_materialize,
                derive_daily=lambda **kw: None,
                read_window_factory=lambda **kw: lambda: None,
                write_daily_factory=lambda **kw: lambda bars: 0,
            )

    def test_guard_members_pin(self) -> None:
        pl = pytest.importorskip("polars")

        from brutex.crossverify.prebuild_leg import (
            _quarantine_guard_errors,  # pyright: ignore[reportPrivateUsage]
        )

        members = _quarantine_guard_errors()
        assert Exception in members
        assert OverflowError in members
        assert pl.exceptions.PanicException in members
        assert not issubclass(KeyboardInterrupt, members)
        assert not issubclass(SystemExit, members)

    def test_guard_wheel_free_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """With polars ABSENT the guard falls back to (Exception,) — nothing
        lost: no polars ⇒ no conversion panic can ever be raised (the
        gated-prebuild fallback posture)."""
        import sys

        from brutex.crossverify.prebuild_leg import (
            _quarantine_guard_errors,  # pyright: ignore[reportPrivateUsage]
        )

        monkeypatch.setitem(sys.modules, "polars", None)  # import polars → ModuleNotFoundError
        assert _quarantine_guard_errors() == (Exception,)


class TestRealCompositionOutOfRangeTs:
    """The executed repro as a permanent regression — a REAL lake, the REAL
    primitives, ONE planted out-of-datetime-range stored timestamp.

    FAILS-BEFORE (executed at merge base ecb37cc4): ``pyo3_runtime.
    PanicException`` escaped the leg — the WHOLE leg aborted, the healthy
    later coordinate was never processed. NOW: the read-boundary ts gate
    refuses the poisoned frame as the quarantining ``ResampleInputError``,
    the ONE symbol is counted, the leg completes, the healthy coordinate
    derives (DEC-SPOT-DERIVE-TS-RANGE-HARDENING-001).
    """

    def _seed(self, lake: Path, identity: CanonicalIdentity) -> Path:
        from brutex.data.models import CandleBar
        from brutex.data.parquet_writer import ParquetWriter
        from brutex.data.sink import ParquetBarSink

        base = datetime(2026, 7, 10, 3, 45, tzinfo=UTC)  # 09:15 IST
        bars = [
            CandleBar(
                timestamp=base + timedelta(minutes=i),
                open=Decimal("100.00"),
                high=Decimal("101.00"),
                low=Decimal("99.00"),
                close=Decimal("100.50"),
                volume=10 + i,
                open_interest=None,
            )
            for i in range(5)
        ]
        ParquetBarSink(writer=ParquetWriter(lake_root=lake)).append(
            exchange=identity.exchange,
            segment=identity.lake_segment,
            symbol=identity.lake_symbol,
            interval="1minute",
            bars=bars,
        )
        return (
            lake
            / "bars"
            / identity.exchange
            / identity.lake_segment
            / identity.lake_symbol
            / "1minute"
            / "2026"
            / "07.parquet"
        )

    def test_poisoned_ts_quarantines_one_symbol_leg_completes(self, tmp_path: Path) -> None:
        pl = pytest.importorskip("polars")
        poisoned = _identity("INDEX", "NIFTY")
        healthy = _identity("CASH", "RELIANCE")
        month_file = self._seed(tmp_path, poisoned)
        self._seed(tmp_path, healthy)
        # Append ONE raw row whose stored timestamp physical value is 2**62 us
        # (~year 148108 — within i64, far past datetime.max). No production
        # writer can mint it (a CandleBar timestamp is a Python datetime).
        df = pl.read_parquet(month_file)
        poisoned_row = df.tail(1).with_columns(
            pl.lit(2**62)
            .cast(pl.Int64)
            .cast(pl.Datetime(time_unit="us", time_zone="UTC"))
            .alias("timestamp")
        )
        pl.concat([df, poisoned_row]).write_parquet(month_file)

        outcome = run_prebuild_leg(
            lake_root=tmp_path,
            day=_DAY,
            coordinates=[poisoned, healthy],
            now_ist=_NOW_IST,
        )
        assert outcome == DeriveOutcome(derived=1, failed=1, failed_names=("NIFTY",))
        # The healthy LATER coordinate was processed (fails-before: never
        # reached — the panic aborted the leg first)…
        assert (tmp_path / "bars/NSE/CASH/RELIANCE/2minute/2026/07.parquet").exists()
        # …and the poisoned symbol wrote NO derived TF (correct-or-absent).
        assert not (tmp_path / "bars/NSE/INDEX/NIFTY/2minute").exists()
