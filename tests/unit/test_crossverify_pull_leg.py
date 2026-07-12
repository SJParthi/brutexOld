"""Unit tests — :mod:`brutex.crossverify.pull_leg` (spy-pinned call shapes)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pytest
from brutex.crossverify.canonical import canonical_identity
from brutex.crossverify.pull_leg import (
    FUTURES_PULL_UNDERLYINGS,
    PullLegReport,
    _resolve_pullers,
    live_fut_rows,
    run_pull_leg,
    run_settle_retry,
)
from brutex.data.errors import MarketHoursHalt
from brutex.data.registry_store import ContractRow
from brutex.models import Exchange, Instrument, InstrumentType, Segment

if TYPE_CHECKING:
    from pathlib import Path

_DAY = date(2026, 7, 10)
_LOOKBACK = 7


def _inst(symbol: str, *, itype: InstrumentType = InstrumentType.EQ) -> Instrument:
    segment = Segment.INDEX if itype is InstrumentType.IDX else Segment.CASH
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=abs(hash(symbol)) % 10_000_000,
        symbol_name=symbol,
        display_name=symbol,
        instrument=itype,
        segment=segment,
        series=None,
        isin=None,
        underlying_symbol=symbol,
        underlying_security_id=None,
        lot_size=1 if itype is InstrumentType.EQ else 0,
        tick_size=Decimal("0.05") if itype is InstrumentType.EQ else Decimal(0),
        sm_freeze_qty=None,
        sm_expiry_date=None,
        strike_price=None,
    )


def _contract(symbol: str, *, side: str, expiry: date, underlying: str = "NIFTY") -> ContractRow:
    return ContractRow(
        exch="NSE",
        underlying=underlying,
        expiry_date=expiry,
        contract_symbol=symbol,
        strike=None if side == "FUT" else 24000.0,
        side=side,
        candle_status="pending",
        last_run_id="run",
        fetched_at=datetime(2026, 7, 1, tzinfo=UTC),
    )


@dataclass
class _FakeStore:
    instruments: list[Instrument] = field(default_factory=list)

    def fno_eligible_underlyings(self) -> frozenset[tuple[Exchange, str]]:
        return frozenset(
            (inst.exch_id, inst.symbol_name)
            for inst in self.instruments
            if inst.instrument is InstrumentType.EQ
        )

    def list_index_equity_instruments(self) -> list[Instrument]:
        return list(self.instruments)


@dataclass
class _FakeRegistry:
    rows: list[ContractRow] = field(default_factory=list)

    def list_contracts(self, *, underlying: str | None = None) -> list[ContractRow]:
        return [row for row in self.rows if underlying is None or row.underlying == underlying]


@dataclass
class _Spy:
    calls: list[dict[str, Any]] = field(default_factory=list)
    raise_market_hours: bool = False

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append({"args": args, "kwargs": kwargs})
        if self.raise_market_hours:
            raise MarketHoursHalt("blocked window reached")
        return object()


def _default_store() -> _FakeStore:
    return _FakeStore(
        instruments=[
            _inst("NIFTY", itype=InstrumentType.IDX),
            _inst("BANKNIFTY", itype=InstrumentType.IDX),
            _inst("RELIANCE"),
        ]
    )


def _default_registry() -> _FakeRegistry:
    return _FakeRegistry(
        rows=[
            _contract("NSE-NIFTY-30Jul26-FUT", side="FUT", expiry=date(2026, 7, 30)),
            _contract("NSE-NIFTY-25Jun26-FUT", side="FUT", expiry=date(2026, 6, 25)),  # expired
            _contract("NSE-NIFTY-30Jul26-24650-CE", side="CE", expiry=date(2026, 7, 30)),
            _contract(
                "NSE-BANKNIFTY-28Jul26-FUT",
                side="FUT",
                expiry=date(2026, 7, 28),
                underlying="BANKNIFTY",
            ),
        ]
    )


def _run(
    *,
    store: _FakeStore | None = None,
    registry: _FakeRegistry | None = None,
    spot: _Spy | None = None,
    metadata: _Spy | None = None,
    candles: _Spy | None = None,
    tmp_path: Path,
) -> tuple[PullLegReport, _Spy, _Spy, _Spy]:
    spot = spot or _Spy()
    metadata = metadata or _Spy()
    candles = candles or _Spy()
    rate = object()
    market_closed = lambda: False  # noqa: E731
    report = run_pull_leg(
        lake_root=tmp_path,
        day=_DAY,
        lookback_days=_LOOKBACK,
        store=store or _default_store(),  # type: ignore[arg-type]
        registry=registry or _default_registry(),  # type: ignore[arg-type]
        session_rate=rate,  # type: ignore[arg-type]
        market_closed=market_closed,
        spot_puller=spot,
        metadata_puller=metadata,
        candle_puller=candles,
    )
    # ONE controller spans every leg (§2 Rule 6 posture).
    for spy in (spot, metadata, candles):
        for call in spy.calls:
            assert call["kwargs"]["session_rate"] is rate
    return report, spot, metadata, candles


class TestLiveFutRows:
    def test_fut_only_unexpired_alpha_order(self) -> None:
        rows = live_fut_rows(_default_registry(), day=_DAY)  # type: ignore[arg-type]
        assert [r.contract_symbol for r in rows] == [
            "NSE-BANKNIFTY-28Jul26-FUT",
            "NSE-NIFTY-30Jul26-FUT",
        ]
        assert all(r.side == "FUT" and r.expiry_date >= _DAY for r in rows)

    def test_expiry_day_contract_still_lives(self) -> None:
        registry = _FakeRegistry(rows=[_contract("NSE-NIFTY-10Jul26-FUT", side="FUT", expiry=_DAY)])
        assert len(live_fut_rows(registry, day=_DAY)) == 1  # type: ignore[arg-type]

    def test_scope_is_nifty_banknifty(self) -> None:
        assert FUTURES_PULL_UNDERLYINGS == ("NIFTY", "BANKNIFTY")


class TestRunPullLeg:
    def test_spot_leg_explicit_to_date_today(self, tmp_path: Path) -> None:
        _report, spot, _m, _c = _run(tmp_path=tmp_path)
        kwargs = spot.calls[0]["kwargs"]
        # THE mutant killer: the default `to` caps at YESTERDAY — the explicit
        # to_date=day is what captures today's finished session.
        assert kwargs["to_date"] == "2026-07-10"
        assert kwargs["from_date"] == "2026-07-03"  # day − lookback
        assert kwargs["until_clean"] is True
        assert kwargs["auto_retry"] is True
        assert kwargs["dry_run"] is False
        assert kwargs["lake_root"] == tmp_path
        assert kwargs["floor_store"] is not None
        assert kwargs["market_closed"] is not None

    def test_spot_targets_are_the_whole_universe_selection(self, tmp_path: Path) -> None:
        _report, spot, _m, _c = _run(tmp_path=tmp_path)
        targets = spot.calls[0]["args"][0]
        # The nightly enumeration verbatim: indices FIRST (alpha), then stocks.
        assert [t.symbol_name for t in targets] == ["BANKNIFTY", "NIFTY", "RELIANCE"]

    def test_metadata_scoped_to_two_indices_and_this_year(self, tmp_path: Path) -> None:
        _report, _s, metadata, _c = _run(tmp_path=tmp_path)
        call = metadata.calls[0]
        assert sorted(inst.symbol_name for inst in call["args"][0]) == ["BANKNIFTY", "NIFTY"]
        assert call["kwargs"]["years"] == [2026]
        assert call["kwargs"]["dry_run"] is False
        assert call["kwargs"]["market_closed"] is not None

    def test_metadata_missing_index_warns_but_runs(self, tmp_path: Path) -> None:
        store = _FakeStore(instruments=[_inst("NIFTY", itype=InstrumentType.IDX)])
        report, _s, metadata, _c = _run(store=store, tmp_path=tmp_path)
        assert report.metadata_ok is True
        assert [inst.symbol_name for inst in metadata.calls[0]["args"][0]] == ["NIFTY"]

    def test_candle_leg_gets_only_live_fut_rows(self, tmp_path: Path) -> None:
        report, _s, _m, candles = _run(tmp_path=tmp_path)
        rows = candles.calls[0]["args"][0]
        # A CE row and an expired FUT row NEVER reach pull_contract_candles.
        assert [r.contract_symbol for r in rows] == [
            "NSE-BANKNIFTY-28Jul26-FUT",
            "NSE-NIFTY-30Jul26-FUT",
        ]
        kwargs = candles.calls[0]["kwargs"]
        # The candle chain (pull_contract_candles) types date | None — a str
        # here TypeErrors inside its _resolve_range max() on every real run
        # (round-1 hostile-review BLOCKER; the spot chain takes ISO strings).
        assert kwargs["to_date"] == date(2026, 7, 10)
        assert kwargs["from_date"] == date(2026, 7, 3)
        assert kwargs["intervals"] == ("1minute",)
        assert kwargs["until_clean"] is True
        assert report.fut_contracts == tuple(rows)

    def test_clean_report(self, tmp_path: Path) -> None:
        report, _s, _m, _c = _run(tmp_path=tmp_path)
        assert report == PullLegReport(
            spot_ok=True,
            metadata_ok=True,
            futures_ok=True,
            halted=False,
            halt_reason=None,
            fut_contracts=report.fut_contracts,
        )
        assert len(report.fut_contracts) == 2

    def test_market_hours_halt_in_spot_leg(self, tmp_path: Path) -> None:
        report, _spot, metadata, candles = _run(
            spot=_Spy(raise_market_hours=True), tmp_path=tmp_path
        )
        assert report.halted is True
        assert report.halt_reason == "market_hours"
        assert report.spot_ok is False
        assert metadata.calls == [] and candles.calls == []

    def test_market_hours_halt_in_metadata_leg(self, tmp_path: Path) -> None:
        report, _s, _m, candles = _run(metadata=_Spy(raise_market_hours=True), tmp_path=tmp_path)
        assert report.halted is True
        assert (report.spot_ok, report.metadata_ok) == (True, False)
        assert candles.calls == []

    def test_market_hours_halt_in_candle_leg(self, tmp_path: Path) -> None:
        report, _s, _m, _c = _run(candles=_Spy(raise_market_hours=True), tmp_path=tmp_path)
        assert report.halted is True
        assert (report.metadata_ok, report.futures_ok) == (True, False)
        assert report.fut_contracts == ()

    def test_pull_wide_halt_propagates(self, tmp_path: Path) -> None:
        from brutex.data.errors import LakeWriteHalt

        def _halting(*args: Any, **kwargs: Any) -> Any:
            raise LakeWriteHalt("disk_full")

        with pytest.raises(LakeWriteHalt):
            run_pull_leg(
                lake_root=tmp_path,
                day=_DAY,
                lookback_days=_LOOKBACK,
                store=_default_store(),  # type: ignore[arg-type]
                registry=_default_registry(),  # type: ignore[arg-type]
                session_rate=object(),  # type: ignore[arg-type]
                market_closed=None,
                spot_puller=_halting,
                metadata_puller=_Spy(),
                candle_puller=_Spy(),
            )


class TestDateTypeContract:
    """Round-1 BLOCKER regression: the two real chains type their windows
    DIFFERENTLY — ``pull_all_spot(from_date: str | None)`` vs
    ``pull_contract_candles(from_date: date | None)``. These stubs carry the
    REAL annotations and assert the runtime types, so a future str/date
    regression fails here without a live vendor."""

    @staticmethod
    def _typed_candle_stub(calls: list[dict[str, Any]]) -> Any:
        def _stub(
            contracts: Any,
            *,
            lake_root: Any,
            dry_run: bool,
            intervals: Any,
            registry: Any,
            from_date: date | None = None,
            to_date: date | None = None,
            session_rate: Any = None,
            until_clean: bool = False,
        ) -> object:
            _ = (contracts, lake_root, dry_run, intervals, registry, session_rate, until_clean)
            for value in (from_date, to_date):
                assert isinstance(value, date) and not isinstance(value, str)
                assert not isinstance(value, datetime)  # a date, never a datetime
            calls.append({"from_date": from_date, "to_date": to_date})
            return object()

        return _stub

    @staticmethod
    def _typed_spot_stub(calls: list[dict[str, Any]]) -> Any:
        def _stub(
            instruments: Any,
            *,
            lake_root: Any,
            dry_run: bool,
            from_date: str | None = None,
            to_date: str | None = None,
            auto_retry: bool = False,
            until_clean: bool = False,
            floor_store: Any = None,
            session_rate: Any = None,
            market_closed: Any = None,
        ) -> object:
            _ = (
                instruments,
                lake_root,
                dry_run,
                auto_retry,
                until_clean,
                floor_store,
                session_rate,
                market_closed,
            )
            for value in (from_date, to_date):
                assert isinstance(value, str) and not isinstance(value, date)
            calls.append({"from_date": from_date, "to_date": to_date})
            return object()

        return _stub

    def test_run_pull_leg_passes_dates_to_candle_leg_strings_to_spot(self, tmp_path: Path) -> None:
        candle_calls: list[dict[str, Any]] = []
        spot_calls: list[dict[str, Any]] = []
        report = run_pull_leg(
            lake_root=tmp_path,
            day=_DAY,
            lookback_days=_LOOKBACK,
            store=_default_store(),  # type: ignore[arg-type]
            registry=_default_registry(),  # type: ignore[arg-type]
            session_rate=object(),  # type: ignore[arg-type]
            market_closed=None,
            spot_puller=self._typed_spot_stub(spot_calls),
            metadata_puller=_Spy(),
            candle_puller=self._typed_candle_stub(candle_calls),
        )
        assert report.futures_ok is True
        assert spot_calls == [{"from_date": "2026-07-03", "to_date": "2026-07-10"}]
        assert candle_calls == [{"from_date": date(2026, 7, 3), "to_date": date(2026, 7, 10)}]

    def test_run_settle_retry_passes_dates_to_candle_leg(self, tmp_path: Path) -> None:
        candle_calls: list[dict[str, Any]] = []
        spot_calls: list[dict[str, Any]] = []
        index = canonical_identity(exchange="NSE", segment="INDEX", lake_symbol="NIFTY")
        fut = canonical_identity(exchange="NSE", segment="FNO", lake_symbol="NSE-NIFTY-30Jul26-FUT")
        assert index is not None and fut is not None
        report = run_settle_retry(
            lake_root=tmp_path,
            day=_DAY,
            lookback_days=_LOOKBACK,
            affected=[index, fut],
            store=_default_store(),  # type: ignore[arg-type]
            registry=_default_registry(),  # type: ignore[arg-type]
            session_rate=object(),  # type: ignore[arg-type]
            market_closed=None,
            spot_puller=self._typed_spot_stub(spot_calls),
            candle_puller=self._typed_candle_stub(candle_calls),
        )
        assert report.futures_ok is True
        assert spot_calls == [{"from_date": "2026-07-03", "to_date": "2026-07-10"}]
        assert candle_calls == [{"from_date": date(2026, 7, 3), "to_date": date(2026, 7, 10)}]

    def test_real_chain_signature_drift_pins(self) -> None:
        # An UPSTREAM annotation drift on either real chain must fail loudly
        # here — the leg's date-type law is derived from these signatures.
        # (eval_str=True over the WHOLE signature would choke on the chains'
        # TYPE_CHECKING-only names, so only the window params are evaluated.)
        import inspect
        import typing

        from brutex.dashboard.historical_pull import pull_contract_candles
        from brutex.dashboard.pull_controller import pull_all_spot

        namespace = {"date": date, "str": str, "Optional": typing.Optional}

        def _window_annotation(func: Any, param: str) -> object:
            annotation = inspect.signature(func).parameters[param].annotation
            if isinstance(annotation, str):
                # The chains use `from __future__ import annotations`, so the
                # annotation is a string; evaluate ONLY the window param under
                # a minimal namespace (test-controlled input — S307-safe).
                return eval(annotation, namespace)  # noqa: S307
            return annotation

        assert _window_annotation(pull_contract_candles, "from_date") == (date | None)
        assert _window_annotation(pull_contract_candles, "to_date") == (date | None)
        assert _window_annotation(pull_all_spot, "from_date") == (str | None)
        assert _window_annotation(pull_all_spot, "to_date") == (str | None)


class TestResolvePullers:
    def test_defaults_are_the_real_chains(self) -> None:
        from brutex.dashboard.historical_pull import pull_contract_candles
        from brutex.dashboard.metadata_pull import pull_expiries_contracts
        from brutex.dashboard.pull_controller import pull_all_spot

        spot, metadata, candles = _resolve_pullers(None, None, None)
        assert spot is pull_all_spot
        assert metadata is pull_expiries_contracts
        assert candles is pull_contract_candles

    def test_injected_pullers_pass_through(self) -> None:
        a, b, c = _Spy(), _Spy(), _Spy()
        assert _resolve_pullers(a, b, c) == (a, b, c)


class TestRunSettleRetry:
    def _affected(self) -> list[Any]:
        index = canonical_identity(exchange="NSE", segment="INDEX", lake_symbol="NIFTY")
        fut = canonical_identity(exchange="NSE", segment="FNO", lake_symbol="NSE-NIFTY-30Jul26-FUT")
        assert index is not None and fut is not None
        return [index, fut]

    def test_subset_pull_shapes(self, tmp_path: Path) -> None:
        spot, candles = _Spy(), _Spy()
        report = run_settle_retry(
            lake_root=tmp_path,
            day=_DAY,
            lookback_days=_LOOKBACK,
            affected=self._affected(),
            store=_default_store(),  # type: ignore[arg-type]
            registry=_default_registry(),  # type: ignore[arg-type]
            session_rate=object(),  # type: ignore[arg-type]
            market_closed=None,
            spot_puller=spot,
            candle_puller=candles,
        )
        assert [t.symbol_name for t in spot.calls[0]["args"][0]] == ["NIFTY"]
        assert spot.calls[0]["kwargs"]["to_date"] == "2026-07-10"  # spot chain: ISO string
        assert [r.contract_symbol for r in candles.calls[0]["args"][0]] == ["NSE-NIFTY-30Jul26-FUT"]
        # The candle chain gets date objects (the run_pull_leg date-type law).
        assert candles.calls[0]["kwargs"]["to_date"] == date(2026, 7, 10)
        assert candles.calls[0]["kwargs"]["from_date"] == date(2026, 7, 3)
        assert report.halted is False
        assert [r.contract_symbol for r in report.fut_contracts] == ["NSE-NIFTY-30Jul26-FUT"]

    def test_spot_only_subset_skips_candles(self, tmp_path: Path) -> None:
        index = canonical_identity(exchange="NSE", segment="INDEX", lake_symbol="NIFTY")
        assert index is not None
        spot, candles = _Spy(), _Spy()
        report = run_settle_retry(
            lake_root=tmp_path,
            day=_DAY,
            lookback_days=_LOOKBACK,
            affected=[index],
            store=_default_store(),  # type: ignore[arg-type]
            registry=_default_registry(),  # type: ignore[arg-type]
            session_rate=object(),  # type: ignore[arg-type]
            market_closed=None,
            spot_puller=spot,
            candle_puller=candles,
        )
        assert len(spot.calls) == 1
        assert candles.calls == []
        assert report.fut_contracts == ()

    def test_empty_affected_pulls_nothing(self, tmp_path: Path) -> None:
        spot, candles = _Spy(), _Spy()
        report = run_settle_retry(
            lake_root=tmp_path,
            day=_DAY,
            lookback_days=_LOOKBACK,
            affected=[],
            store=_default_store(),  # type: ignore[arg-type]
            registry=_default_registry(),  # type: ignore[arg-type]
            session_rate=object(),  # type: ignore[arg-type]
            market_closed=None,
            spot_puller=spot,
            candle_puller=candles,
        )
        assert spot.calls == [] and candles.calls == []
        assert report.halted is False

    def test_market_hours_halt_in_spot_retry(self, tmp_path: Path) -> None:
        report = run_settle_retry(
            lake_root=tmp_path,
            day=_DAY,
            lookback_days=_LOOKBACK,
            affected=self._affected(),
            store=_default_store(),  # type: ignore[arg-type]
            registry=_default_registry(),  # type: ignore[arg-type]
            session_rate=object(),  # type: ignore[arg-type]
            market_closed=None,
            spot_puller=_Spy(raise_market_hours=True),
            candle_puller=_Spy(),
        )
        assert report.halted is True

    def test_market_hours_halt_in_candle_retry(self, tmp_path: Path) -> None:
        report = run_settle_retry(
            lake_root=tmp_path,
            day=_DAY,
            lookback_days=_LOOKBACK,
            affected=self._affected(),
            store=_default_store(),  # type: ignore[arg-type]
            registry=_default_registry(),  # type: ignore[arg-type]
            session_rate=object(),  # type: ignore[arg-type]
            market_closed=None,
            spot_puller=_Spy(),
            candle_puller=_Spy(raise_market_hours=True),
        )
        assert report.halted is True
        assert report.spot_ok is True
