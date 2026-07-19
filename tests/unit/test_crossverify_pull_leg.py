"""Unit tests — :mod:`brutex.crossverify.pull_leg` (spy-pinned call shapes).

SPOT-ONLY since DEC-CROSSVERIFY-SPOT-ONLY-001 (MIGRATION.md #1788): the
metadata + futures-candle legs are GONE (their absence is import-pinned) and
the whole-spot-universe enumeration — indices + F&O stocks + reference-only
NTM names — is pinned so it can never silently narrow WITHIN a scope.
Since DEC-UNIVERSE-INDEX-ONLY-001 (#1934, supersedes-in-part #1788's
enumeration clause) that whole-universe law holds under a STOCK-ADMITTING
scope — the suite runs full-arm (the autouse fixture below) with the shipped
index4-default narrowing pinned in ``TestIndex4DefaultEnumeration``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pytest
from brutex.config.feed_scope import FEED_SCOPE_ENV as _FEED_SCOPE_ENV_1934
from brutex.crossverify.canonical import canonical_identity
from brutex.crossverify.pull_leg import (
    PullLegReport,
    _resolve_spot_puller,
    run_pull_leg,
    run_settle_retry,
    whole_universe_spot_targets,
)
from brutex.data.errors import MarketHoursHalt
from brutex.models import Exchange, Instrument, InstrumentType, Segment

if TYPE_CHECKING:
    from pathlib import Path

_DAY = date(2026, 7, 10)
_LOOKBACK = 7

#: The reference-only NTM constituent's fixture SECURITY_ID (not F&O-eligible).
_NTM_SID = 424242


@pytest.fixture(autouse=True)
def _full_scope_arm(monkeypatch: pytest.MonkeyPatch) -> None:
    """EXPLICIT full arm (DEC-UNIVERSE-INDEX-ONLY-001 — the §6 migration law).

    This suite pins the #1788 whole-spot-universe enumeration MECHANICS over
    stock/sectoral fixtures; the crossverify enumeration rides the SAME
    scope-aware ``classify_instrument`` law, so under the conftest-forced
    ``index4`` default those fixtures legitimately narrow. Running the suite
    under ``BRUTEX_FEED_SCOPE=full`` IS the §6 widen-path pin — the legacy
    enumeration is byte-identical there. The shipped index4-default guard arm
    lives in ``TestIndex4DefaultEnumeration`` below (which re-narrows in-body,
    winning over this fixture)."""
    monkeypatch.setenv(_FEED_SCOPE_ENV_1934, "full")


def _inst(
    symbol: str, *, itype: InstrumentType = InstrumentType.EQ, security_id: int | None = None
) -> Instrument:
    segment = Segment.INDEX if itype is InstrumentType.IDX else Segment.CASH
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=security_id if security_id is not None else abs(hash(symbol)) % 10_000_000,
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


@dataclass
class _FakeStore:
    """The store surface the leg reads — eligible/reference sets injectable."""

    instruments: list[Instrument] = field(default_factory=list)
    eligible: frozenset[tuple[Exchange, str]] | None = None
    reference_ids: frozenset[int] = frozenset()

    def fno_eligible_underlyings(self) -> frozenset[tuple[Exchange, str]]:
        if self.eligible is not None:
            return self.eligible
        return frozenset(
            (inst.exch_id, inst.symbol_name)
            for inst in self.instruments
            if inst.instrument is InstrumentType.EQ
        )

    def reference_only_security_ids(self) -> frozenset[int]:
        return self.reference_ids

    def list_index_equity_instruments(self) -> list[Instrument]:
        return list(self.instruments)


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


def _run(
    *,
    store: _FakeStore | None = None,
    spot: _Spy | None = None,
    tmp_path: Path,
) -> tuple[PullLegReport, _Spy]:
    spot = spot or _Spy()
    rate = object()
    market_closed = lambda: False  # noqa: E731
    report = run_pull_leg(
        lake_root=tmp_path,
        day=_DAY,
        lookback_days=_LOOKBACK,
        store=store or _default_store(),  # type: ignore[arg-type]
        session_rate=rate,  # type: ignore[arg-type]
        market_closed=market_closed,
        spot_puller=spot,
    )
    # The ONE controller spans the leg (§2 Rule 6 posture).
    for call in spot.calls:
        assert call["kwargs"]["session_rate"] is rate
    return report, spot


class TestWholeUniverseSpotTargets:
    def test_covers_index_fno_stock_and_reference_only_ntm_stock(self) -> None:
        """The #1788 whole-universe pin: ALL three admitted tiers enumerate —
        an index, an F&O-eligible stock AND a reference-only NTM constituent
        (by SECURITY_ID) — so the spot scope can never silently narrow back
        to the F&O-eligible subset or the engine trio."""
        index = _inst("FINNIFTY", itype=InstrumentType.IDX)
        fno_stock = _inst("RELIANCE")
        ntm_stock = _inst("HONASA", security_id=_NTM_SID)
        store = _FakeStore(
            instruments=[index, fno_stock, ntm_stock],
            eligible=frozenset({(Exchange.NSE, "RELIANCE")}),
            reference_ids=frozenset({_NTM_SID}),
        )
        targets = whole_universe_spot_targets(store)  # type: ignore[arg-type]
        assert [t.symbol_name for t in targets] == ["FINNIFTY", "HONASA", "RELIANCE"]

    def test_indices_first_then_stocks_each_alpha(self) -> None:
        store = _FakeStore(
            instruments=[
                _inst("ZEEL"),
                _inst("NIFTY", itype=InstrumentType.IDX),
                _inst("ACC"),
                _inst("BANKNIFTY", itype=InstrumentType.IDX),
            ]
        )
        targets = whole_universe_spot_targets(store)  # type: ignore[arg-type]
        assert [t.symbol_name for t in targets] == ["BANKNIFTY", "NIFTY", "ACC", "ZEEL"]

    def test_unclassifiable_rows_are_dropped(self) -> None:
        # A non-eligible, non-reference EQ row classifies None → never pulled.
        stray = _inst("STRAY")
        store = _FakeStore(
            instruments=[stray, _inst("NIFTY", itype=InstrumentType.IDX)],
            eligible=frozenset(),
        )
        targets = whole_universe_spot_targets(store)  # type: ignore[arg-type]
        assert [t.symbol_name for t in targets] == ["NIFTY"]


class TestIndex4DefaultEnumeration:
    """The shipped index4-default guard arm (DEC-UNIVERSE-INDEX-ONLY-001 Law 1):

    the crossverify pull enumeration narrows WITH the scope law — a sectoral
    index, an F&O stock and a reference-only NTM name all drop; only the
    admitted pairs enumerate. Re-narrows in-body (winning over the module's
    full-arm autouse fixture)."""

    def test_enumeration_narrows_to_admitted_indices(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(_FEED_SCOPE_ENV_1934, "index4")
        store = _FakeStore(
            instruments=[
                _inst("NIFTY", itype=InstrumentType.IDX),
                _inst("INDIAVIX", itype=InstrumentType.IDX),
                _inst("FINNIFTY", itype=InstrumentType.IDX),  # sectoral — excluded
                _inst("RELIANCE"),  # F&O stock — excluded
                _inst("HONASA", security_id=_NTM_SID),  # NTM reference — excluded
            ],
            eligible=frozenset({(Exchange.NSE, "RELIANCE")}),
            reference_ids=frozenset({_NTM_SID}),
        )
        targets = whole_universe_spot_targets(store)  # type: ignore[arg-type]
        assert [t.symbol_name for t in targets] == ["INDIAVIX", "NIFTY"]


class TestRunPullLeg:
    def test_spot_leg_explicit_to_date_today(self, tmp_path: Path) -> None:
        _report, spot = _run(tmp_path=tmp_path)
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
        _report, spot = _run(tmp_path=tmp_path)
        targets = spot.calls[0]["args"][0]
        # The whole-universe enumeration: indices FIRST (alpha), then stocks.
        assert [t.symbol_name for t in targets] == ["BANKNIFTY", "NIFTY", "RELIANCE"]

    def test_clean_report(self, tmp_path: Path) -> None:
        report, _spot = _run(tmp_path=tmp_path)
        assert report == PullLegReport(spot_ok=True, halted=False, halt_reason=None)

    def test_exactly_one_leg_no_metadata_no_candles(self, tmp_path: Path) -> None:
        """#1788: the spot pull is the ONLY vendor leg — one call, no more."""
        _report, spot = _run(tmp_path=tmp_path)
        assert len(spot.calls) == 1

    def test_market_hours_halt_in_spot_leg(self, tmp_path: Path) -> None:
        report, _spot = _run(spot=_Spy(raise_market_hours=True), tmp_path=tmp_path)
        assert report.halted is True
        assert report.halt_reason == "market_hours"
        assert report.spot_ok is False

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
                session_rate=object(),  # type: ignore[arg-type]
                market_closed=None,
                spot_puller=_halting,
            )


class TestSpotDateTypeContract:
    """The spot chain's window law: ``pull_all_spot`` types its window as ISO
    **strings** (``str | None``) — the leg must never hand it date objects.
    (The futures/candle date-type dual-shape law left with the futures legs —
    DEC-CROSSVERIFY-SPOT-ONLY-001.)"""

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

    def test_run_pull_leg_passes_iso_strings_to_spot(self, tmp_path: Path) -> None:
        spot_calls: list[dict[str, Any]] = []
        report = run_pull_leg(
            lake_root=tmp_path,
            day=_DAY,
            lookback_days=_LOOKBACK,
            store=_default_store(),  # type: ignore[arg-type]
            session_rate=object(),  # type: ignore[arg-type]
            market_closed=None,
            spot_puller=self._typed_spot_stub(spot_calls),
        )
        assert report.spot_ok is True
        assert spot_calls == [{"from_date": "2026-07-03", "to_date": "2026-07-10"}]

    def test_run_settle_retry_passes_iso_strings_to_spot(self, tmp_path: Path) -> None:
        spot_calls: list[dict[str, Any]] = []
        index = canonical_identity(exchange="NSE", segment="INDEX", lake_symbol="NIFTY")
        assert index is not None
        report = run_settle_retry(
            lake_root=tmp_path,
            day=_DAY,
            lookback_days=_LOOKBACK,
            affected=[index],
            store=_default_store(),  # type: ignore[arg-type]
            session_rate=object(),  # type: ignore[arg-type]
            market_closed=None,
            spot_puller=self._typed_spot_stub(spot_calls),
        )
        assert report.spot_ok is True
        assert spot_calls == [{"from_date": "2026-07-03", "to_date": "2026-07-10"}]

    def test_real_chain_signature_drift_pin(self) -> None:
        # An UPSTREAM annotation drift on the spot chain must fail loudly
        # here — the leg's ISO-string window law derives from this signature.
        import inspect
        import typing

        from brutex.dashboard.pull_controller import pull_all_spot

        namespace = {"date": date, "str": str, "Optional": typing.Optional}

        def _window_annotation(func: Any, param: str) -> object:
            annotation = inspect.signature(func).parameters[param].annotation
            if isinstance(annotation, str):
                # The chain uses `from __future__ import annotations`, so the
                # annotation is a string; evaluate ONLY the window param under
                # a minimal namespace (test-controlled input — S307-safe).
                return eval(annotation, namespace)  # noqa: S307
            return annotation

        assert _window_annotation(pull_all_spot, "from_date") == (str | None)
        assert _window_annotation(pull_all_spot, "to_date") == (str | None)


class TestResolveSpotPuller:
    def test_default_is_the_real_chain(self) -> None:
        from brutex.dashboard.pull_controller import pull_all_spot

        assert _resolve_spot_puller(None) is pull_all_spot

    def test_injected_puller_passes_through(self) -> None:
        spy = _Spy()
        assert _resolve_spot_puller(spy) is spy


class TestFuturesLegsRemoved:
    """DEC-CROSSVERIFY-SPOT-ONLY-001 negative pins: no futures/metadata leg
    machinery survives in the module — a re-introduction fails loudly here."""

    def test_no_futures_or_metadata_symbols(self) -> None:
        from brutex.crossverify import pull_leg

        for gone in ("live_fut_rows", "FUTURES_PULL_UNDERLYINGS", "_resolve_pullers"):
            assert not hasattr(pull_leg, gone), gone

    def test_report_carries_no_futures_fields(self) -> None:
        import dataclasses

        names = {f.name for f in dataclasses.fields(PullLegReport)}
        assert names == {"spot_ok", "halted", "halt_reason"}


class TestRunSettleRetry:
    def test_subset_pull_shape_spot_only(self, tmp_path: Path) -> None:
        index = canonical_identity(exchange="NSE", segment="INDEX", lake_symbol="NIFTY")
        assert index is not None
        spot = _Spy()
        report = run_settle_retry(
            lake_root=tmp_path,
            day=_DAY,
            lookback_days=_LOOKBACK,
            affected=[index],
            store=_default_store(),  # type: ignore[arg-type]
            session_rate=object(),  # type: ignore[arg-type]
            market_closed=None,
            spot_puller=spot,
        )
        assert [t.symbol_name for t in spot.calls[0]["args"][0]] == ["NIFTY"]
        assert spot.calls[0]["kwargs"]["to_date"] == "2026-07-10"  # spot chain: ISO string
        assert report.halted is False

    def test_non_spot_identity_is_ignored_never_pulled(self, tmp_path: Path) -> None:
        """A directly-constructed FNO identity (structurally absent from the
        spot-only G2 belt) is filtered — zero pulls, never a crash."""
        fut = canonical_identity(exchange="NSE", segment="FNO", lake_symbol="NSE-NIFTY-30Jul26-FUT")
        assert fut is not None
        spot = _Spy()
        report = run_settle_retry(
            lake_root=tmp_path,
            day=_DAY,
            lookback_days=_LOOKBACK,
            affected=[fut],
            store=_default_store(),  # type: ignore[arg-type]
            session_rate=object(),  # type: ignore[arg-type]
            market_closed=None,
            spot_puller=spot,
        )
        assert spot.calls == []
        assert report.halted is False

    def test_empty_affected_pulls_nothing(self, tmp_path: Path) -> None:
        spot = _Spy()
        report = run_settle_retry(
            lake_root=tmp_path,
            day=_DAY,
            lookback_days=_LOOKBACK,
            affected=[],
            store=_default_store(),  # type: ignore[arg-type]
            session_rate=object(),  # type: ignore[arg-type]
            market_closed=None,
            spot_puller=spot,
        )
        assert spot.calls == []
        assert report.halted is False

    def test_market_hours_halt_in_spot_retry(self, tmp_path: Path) -> None:
        index = canonical_identity(exchange="NSE", segment="INDEX", lake_symbol="NIFTY")
        assert index is not None
        report = run_settle_retry(
            lake_root=tmp_path,
            day=_DAY,
            lookback_days=_LOOKBACK,
            affected=[index],
            store=_default_store(),  # type: ignore[arg-type]
            session_rate=object(),  # type: ignore[arg-type]
            market_closed=None,
            spot_puller=_Spy(raise_market_hours=True),
        )
        assert report.halted is True
        assert report.halt_reason == "market_hours"
