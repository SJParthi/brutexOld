# pyright: reportPrivateUsage=none
"""Dashboard BYTE-IDENTITY pins for the DEC-CLI-GREEKS-ENRICH-001 delegation refactor.

``historical_pull._enrich_greeks_from_minute`` became a THIN DELEGATOR into the
COMMON :func:`brutex.data.greeks_enrich_pass.run_greeks_enrich_pass` (the fence
MOVED, not copied). These tests pin that the dashboard wrapper is OBSERVABLY
IDENTICAL to its pre-refactor self:

* alias identity — ``hp._GreeksEnrichOutcome`` IS the common
  ``GreeksEnrichOutcome`` class and ``hp._GREEKS_NOT_RUN`` IS the common
  ``GREEKS_NOT_RUN`` sentinel (same objects, so every pre-refactor consumer —
  report kwargs, note builder, equality in older tests — keeps working);
* the ``fno.greeks_enrich.done`` event carries the EXACT 13 driver-report
  fields in the pre-refactor order, on the DASHBOARD logger
  (``brutex.dashboard.historical_pull`` via the injected ``log=_LOG``);
* the delegator's events + outcome are EQUAL to a direct
  ``run_greeks_enrich_pass`` invocation over the same targets (moved-code
  equivalence, success AND failure legs);
* the target-build skip rules survive (windowless skip, ``contract_exchange``
  ``ValueError`` skip, FUT still PASSED for the driver to tally-skip — the
  dashboard/CLI counter asymmetry the design documents), the fence order
  (rate → build → drive) survives, and empty ``rebuilt`` still drives the
  driver (no new early-out);
* ``LakeWriteHalt`` / ``GreeksEnrichPoolBroken`` RE-RAISE from within the
  dashboard wrapper (zero fenced events), ``KeyboardInterrupt`` propagates raw;
* the pull flow (``pull_contract_candles``) consumes the COMMON outcome
  dataclass — the dry-run report carries the common sentinel's fields and a
  live report's Greeks fields are the delegator's own returned instance.

Every path runs FULLY OFFLINE (fake SDK / recording sink / fake driver);
tests that enter the real fence ``importorskip("trading_core")`` like the
sibling ``test_pull_historical.py`` suites.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import cast

import pytest
from brutex.adapters.protocol import DerivativeContract
from brutex.dashboard import historical_pull as hp
from brutex.data import greeks_enrich_pass as gep
from brutex.data.errors import GreeksEnrichPoolBroken, LakeWriteHalt
from brutex.data.sink import SinkAppendResult
from structlog.testing import capture_logs
from structlog.typing import EventDict

_NOW = datetime(2026, 6, 14, 9, 15, tzinfo=UTC)
_RUN_ID = "greeks-identity-test"
_FETCHED = datetime(2026, 1, 1, tzinfo=UTC)
_FROM = date(2024, 1, 1)
_TO = date(2024, 1, 20)
_EXPIRY = date(2024, 1, 25)
_WINDOW = (date(2024, 1, 1), date(2024, 1, 20))

#: The pre-refactor ``fno.greeks_enrich.done`` payload — the 13 driver-report
#: counters in the EXACT emission order (the byte-identity contract), followed
#: by the two keys ``structlog.testing.capture_logs`` appends.
_DONE_FIELD_ORDER = (
    "enriched",
    "already_current",
    "futures_skipped",
    "undecodable",
    "no_bars",
    "errored",
    "bars_written",
    "bars_solved",
    "bars_null",
    "bars_reused",
    "spot_reads",
    "spot_hits",
    "bars_skipped",
    "event",
    "log_level",
)


# ── offline helpers (mirroring the test_pull_historical.py fakes) ─────────────


def _contract(
    symbol: str, contract_type: str, strike: float | None = 21000.0
) -> DerivativeContract:
    """One NIFTY derivative contract row shaped like the registry rebuild."""
    return DerivativeContract(
        underlying_symbol="NIFTY",
        expiry=_EXPIRY,
        symbol=symbol,
        contract_type=contract_type,
        strike=strike,
    )


def _rebuilt(*contracts: DerivativeContract) -> list[hp._RebuiltContract]:
    """Wrap contracts as ``(row, contract)`` pairs the delegator iterates."""
    return cast("list[hp._RebuiltContract]", [(None, contract) for contract in contracts])


def _distinct_report(ge_mod: object, **overrides: int) -> object:
    """A ``GreeksEnrichReport`` whose 13 counters are all-distinct by default.

    Distinct values (1..13) make a swapped/dropped ``done``-event field
    IMPOSSIBLE to miss — the field-order mutation net.
    """
    fields: dict[str, int] = {
        "enriched": 1,
        "already_current": 2,
        "futures_skipped": 3,
        "undecodable": 4,
        "no_bars": 5,
        "errored": 6,
        "bars_written": 7,
        "bars_solved": 8,
        "bars_null": 9,
        "bars_reused": 10,
        "spot_reads": 11,
        "spot_hits": 12,
        "bars_skipped": 13,
    }
    fields.update(overrides)
    return ge_mod.GreeksEnrichReport(**fields)  # type: ignore[attr-defined]


def _enrich_events(logs: Iterable[EventDict]) -> list[EventDict]:
    """Every captured ``fno.greeks_enrich.*`` event, in emission order."""
    return [r for r in logs if str(r["event"]).startswith("fno.greeks_enrich")]


# ── alias identity ────────────────────────────────────────────────────────────


class TestAliasIdentity:
    """The dashboard names ARE the common-module objects (never copies)."""

    def test_outcome_type_sentinel_and_pass_are_the_common_objects(self) -> None:
        """`hp._GreeksEnrichOutcome` / `_GREEKS_NOT_RUN` / the pass alias the common module.

        The alias-identity regression net: a future re-declaration (a copied
        dataclass, a second sentinel) would silently fork the outcome type —
        equality across the two modules and `is`-checks in the pull flow would
        break. Identity, not equality, is the pin.
        """
        assert hp._GreeksEnrichOutcome is gep.GreeksEnrichOutcome
        assert hp._GREEKS_NOT_RUN is gep.GREEKS_NOT_RUN
        assert hp.run_greeks_enrich_pass is gep.run_greeks_enrich_pass

    def test_not_run_sentinel_field_semantics(self) -> None:
        """The sentinel keeps the pre-refactor "did not run" shape verbatim."""
        assert hp._GREEKS_NOT_RUN.ok is None
        assert hp._GREEKS_NOT_RUN.error is None
        assert hp._GREEKS_NOT_RUN.errored_targets == 0
        # Cross-module construction compares equal — the SAME class object.
        assert gep.GreeksEnrichOutcome(ok=None, error=None, errored_targets=0) == hp._GREEKS_NOT_RUN
        assert hp._GreeksEnrichOutcome(
            ok=True, error=None, errored_targets=2
        ) == gep.GreeksEnrichOutcome(ok=True, error=None, errored_targets=2)


# ── delegator threading (the new seam) ────────────────────────────────────────


class TestDelegatorThreading:
    """`_enrich_greeks_from_minute` injects the dashboard logger + its target loop."""

    def test_dashboard_logger_name_is_the_module_logger(self) -> None:
        """`hp._LOG` is named ``brutex.dashboard.historical_pull`` — the byte-identity
        of the pass events' logger metadata rides `log=_LOG`, so the NAME is pinned here
        (structlog's lazy proxy records its factory args)."""
        assert getattr(hp._LOG, "_logger_factory_args", None) == (
            "brutex.dashboard.historical_pull",
        )

    def test_delegator_threads_logger_lake_root_builder_and_returns_outcome(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The delegator passes ``log=hp._LOG`` + the lake root, its builder yields
        ONLY windowed exchange-resolvable targets, and the pass outcome is returned
        UNCHANGED (the same object — no rewrapping)."""
        pytest.importorskip("trading_core")
        from brutex.data.greeks_enrich import GreeksEnrichTarget

        sentinel = gep.GreeksEnrichOutcome(ok=True, error=None, errored_targets=7)
        recorded: dict[str, object] = {}

        def _recorder(
            *,
            build_targets: gep.TargetBuilder,
            lake_root: Path,
            log: object = None,
        ) -> gep.GreeksEnrichOutcome:
            recorded["targets"] = list(build_targets())
            recorded["lake_root"] = lake_root
            recorded["log"] = log
            return sentinel

        monkeypatch.setattr(hp, "run_greeks_enrich_pass", _recorder)
        good = _contract("NSE-NIFTY-25Jan24-21000-CE", "CE")
        windowless = _contract("NSE-NIFTY-25Jan24-21000-PE", "PE")
        no_exchange = _contract("BADSYMBOL", "FUT", strike=None)  # no `-` → ValueError
        outcome = hp._enrich_greeks_from_minute(
            rebuilt=_rebuilt(good, windowless, no_exchange),
            windows={good.symbol: _WINDOW, no_exchange.symbol: _WINDOW},
            lake_root=tmp_path,
        )
        assert outcome is sentinel  # returned unchanged — no rewrap, no copy
        assert recorded["log"] is hp._LOG  # the dashboard logger, byte-identical name
        assert recorded["lake_root"] == tmp_path
        # The moved target loop: windowless PE skipped, `-`-less FUT skipped
        # (contract_exchange ValueError), the good CE mapped field-for-field.
        assert recorded["targets"] == [
            GreeksEnrichTarget(
                exchange="NSE",
                symbol=good.symbol,
                start=_WINDOW[0],
                end=_WINDOW[1],
            )
        ]


# ── observable identity through the REAL fence ────────────────────────────────


class TestDelegatorObservableIdentity:
    """The delegator's events + outcome equal the pre-refactor observables."""

    def test_done_event_thirteen_fields_exact_order_and_values(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Exactly one ``fno.greeks_enrich.done`` with the 13 counters VERBATIM,
        in the pre-refactor emission order, at info level."""
        pytest.importorskip("trading_core")
        from brutex.data import greeks_enrich as ge

        monkeypatch.delenv("BRUTEX_RISK_FREE_CSV", raising=False)

        def _fake_enrich(*, targets: object, lake_root: Path, rate_source: object) -> object:
            del targets, lake_root, rate_source
            return _distinct_report(ge)

        monkeypatch.setattr(ge, "enrich_greeks_for_targets", _fake_enrich)
        contract = _contract("NSE-NIFTY-25Jan24-21000-CE", "CE")
        with capture_logs() as logs:
            outcome = hp._enrich_greeks_from_minute(
                rebuilt=_rebuilt(contract),
                windows={contract.symbol: _WINDOW},
                lake_root=tmp_path,
            )
        done = [r for r in logs if r["event"] == "fno.greeks_enrich.done"]
        assert len(done) == 1
        assert list(done[0].keys()) == list(_DONE_FIELD_ORDER)
        assert done[0]["log_level"] == "info"
        for position, field in enumerate(_DONE_FIELD_ORDER[:13], start=1):
            assert done[0][field] == position  # each counter verbatim, none swapped
        assert [r for r in logs if r["event"] == "fno.greeks_enrich.error"] == []
        # errored_targets mirrors the report's `errored` (=6 in the distinct grid).
        assert outcome == gep.GreeksEnrichOutcome(ok=True, error=None, errored_targets=6)

    def test_done_and_outcome_identical_between_delegator_and_common_pass(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The dashboard wrapper and a direct ``run_greeks_enrich_pass`` over the
        SAME target produce EQUAL captured events, EQUAL outcomes, and hand the
        driver EQUAL target lists — the moved-code equivalence pin."""
        pytest.importorskip("trading_core")
        from brutex.data import greeks_enrich as ge
        from brutex.data.greeks_enrich import GreeksEnrichTarget

        monkeypatch.delenv("BRUTEX_RISK_FREE_CSV", raising=False)
        seen_targets: list[list[object]] = []

        def _fake_enrich(*, targets: object, lake_root: Path, rate_source: object) -> object:
            del lake_root, rate_source
            seen_targets.append(list(cast("Sequence[object]", targets)))
            return _distinct_report(ge)

        monkeypatch.setattr(ge, "enrich_greeks_for_targets", _fake_enrich)
        contract = _contract("NSE-NIFTY-25Jan24-21000-CE", "CE")
        with capture_logs() as dashboard_logs:
            dashboard_outcome = hp._enrich_greeks_from_minute(
                rebuilt=_rebuilt(contract),
                windows={contract.symbol: _WINDOW},
                lake_root=tmp_path,
            )
        target = GreeksEnrichTarget(
            exchange="NSE", symbol=contract.symbol, start=_WINDOW[0], end=_WINDOW[1]
        )
        with capture_logs() as common_logs:
            common_outcome = gep.run_greeks_enrich_pass(
                build_targets=lambda: [target], lake_root=tmp_path
            )
        assert _enrich_events(dashboard_logs) == _enrich_events(common_logs)
        assert dashboard_outcome == common_outcome
        assert seen_targets[0] == seen_targets[1] == [target]

    def test_error_event_and_outcome_identical_between_delegator_and_common_pass(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A fenced driver failure reads identically on both paths: one
        ``fno.greeks_enrich.error`` (error level, message only), no ``done``,
        outcome ``ok=False`` + ``"RuntimeError: boom"``."""
        pytest.importorskip("trading_core")
        from brutex.data import greeks_enrich as ge
        from brutex.data.greeks_enrich import GreeksEnrichTarget

        monkeypatch.delenv("BRUTEX_RISK_FREE_CSV", raising=False)

        def _boom(*, targets: object, lake_root: Path, rate_source: object) -> object:
            del targets, lake_root, rate_source
            raise RuntimeError("boom")

        monkeypatch.setattr(ge, "enrich_greeks_for_targets", _boom)
        contract = _contract("NSE-NIFTY-25Jan24-21000-CE", "CE")
        with capture_logs() as dashboard_logs:
            dashboard_outcome = hp._enrich_greeks_from_minute(
                rebuilt=_rebuilt(contract),
                windows={contract.symbol: _WINDOW},
                lake_root=tmp_path,
            )
        target = GreeksEnrichTarget(
            exchange="NSE", symbol=contract.symbol, start=_WINDOW[0], end=_WINDOW[1]
        )
        with capture_logs() as common_logs:
            common_outcome = gep.run_greeks_enrich_pass(
                build_targets=lambda: [target], lake_root=tmp_path
            )
        errors = [r for r in dashboard_logs if r["event"] == "fno.greeks_enrich.error"]
        assert len(errors) == 1
        assert list(errors[0].keys()) == ["error", "event", "log_level"]
        assert errors[0]["error"] == "boom"
        assert errors[0]["log_level"] == "error"
        assert [r for r in dashboard_logs if r["event"] == "fno.greeks_enrich.done"] == []
        assert dashboard_outcome == gep.GreeksEnrichOutcome(
            ok=False, error="RuntimeError: boom", errored_targets=0
        )
        assert _enrich_events(dashboard_logs) == _enrich_events(common_logs)
        assert dashboard_outcome == common_outcome

    def test_fence_order_rate_then_build_then_drive(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The pre-refactor fence ORDER survives the delegation: risk-free source
        resolved FIRST, the dashboard target loop built second (its
        ``contract_exchange`` call is the probe), the driver invoked last."""
        pytest.importorskip("trading_core")
        from brutex.data import greeks_enrich as ge

        order: list[str] = []

        def _rate() -> object:
            order.append("rate")
            return object()

        def _exchange(contract: DerivativeContract) -> str:
            del contract
            order.append("build")
            return "NSE"

        def _drive(*, targets: object, lake_root: Path, rate_source: object) -> object:
            del targets, lake_root, rate_source
            order.append("drive")
            return _distinct_report(ge)

        monkeypatch.setattr(ge, "resolve_risk_free_source", _rate)
        monkeypatch.setattr(hp, "contract_exchange", _exchange)
        monkeypatch.setattr(ge, "enrich_greeks_for_targets", _drive)
        contract = _contract("NSE-NIFTY-25Jan24-21000-CE", "CE")
        outcome = hp._enrich_greeks_from_minute(
            rebuilt=_rebuilt(contract),
            windows={contract.symbol: _WINDOW},
            lake_root=tmp_path,
        )
        assert order == ["rate", "build", "drive"]
        assert outcome.ok is True

    def test_missing_wheel_is_fenced_through_the_delegator(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A missing ``trading_core`` wheel (poisoned ``brutex.data.greeks_enrich``)
        degrades to ``ok=False`` + one error event through the dashboard wrapper —
        the pre-refactor never-crash-the-pull wheel discipline."""
        monkeypatch.setitem(sys.modules, "brutex.data.greeks_enrich", None)
        contract = _contract("NSE-NIFTY-25Jan24-21000-CE", "CE")
        with capture_logs() as logs:
            outcome = hp._enrich_greeks_from_minute(
                rebuilt=_rebuilt(contract),
                windows={contract.symbol: _WINDOW},
                lake_root=tmp_path,
            )
        assert outcome.ok is False
        assert outcome.error is not None
        # The fence stamps the CONCRETE type name — a poisoned sys.modules entry
        # raises ModuleNotFoundError (the ImportError subclass real wheel absence
        # raises too), and the pre-refactor f"{type(exc).__name__}: {exc}" format
        # carries it verbatim.
        assert outcome.error.startswith("ModuleNotFoundError:")
        assert "brutex.data.greeks_enrich" in outcome.error
        assert outcome.errored_targets == 0
        assert len([r for r in logs if r["event"] == "fno.greeks_enrich.error"]) == 1
        assert [r for r in logs if r["event"] == "fno.greeks_enrich.done"] == []


# ── target-build skip rules (the moved loop) ──────────────────────────────────


class TestDelegatorSkipRules:
    """The dashboard target loop's skip rules survive the move verbatim."""

    def test_builder_skips_windowless_and_bad_exchange_targets_full_fields(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Through the REAL fence: only the windowed, exchange-resolvable contract
        reaches the driver, mapped field-for-field (exchange from the symbol's
        leading token, the window endpoints verbatim)."""
        pytest.importorskip("trading_core")
        from brutex.data import greeks_enrich as ge
        from brutex.data.greeks_enrich import GreeksEnrichTarget

        monkeypatch.delenv("BRUTEX_RISK_FREE_CSV", raising=False)
        captured: list[object] = []

        def _fake_enrich(*, targets: object, lake_root: Path, rate_source: object) -> object:
            del lake_root, rate_source
            captured.extend(cast("Sequence[object]", targets))
            return _distinct_report(ge)

        monkeypatch.setattr(ge, "enrich_greeks_for_targets", _fake_enrich)
        good = _contract("NSE-NIFTY-25Jan24-21000-CE", "CE")
        windowless = _contract("NSE-NIFTY-25Jan24-21000-PE", "PE")
        no_exchange = _contract("BADSYMBOL", "FUT", strike=None)
        hp._enrich_greeks_from_minute(
            rebuilt=_rebuilt(good, windowless, no_exchange),
            windows={good.symbol: _WINDOW, no_exchange.symbol: _WINDOW},
            lake_root=tmp_path,
        )
        assert captured == [
            GreeksEnrichTarget(
                exchange="NSE",
                symbol=good.symbol,
                start=_WINDOW[0],
                end=_WINDOW[1],
            )
        ]

    def test_fut_with_window_is_still_passed_for_the_driver_to_tally_skip(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A windowed, exchange-resolvable FUT is STILL handed to the driver
        (which tally-skips it as ``futures_skipped``) — the dashboard must NOT
        inherit the CLI discovery's CE/PE-only filter, or its
        ``futures_skipped`` counter would silently zero (the documented
        dashboard/CLI asymmetry)."""
        pytest.importorskip("trading_core")
        from brutex.data import greeks_enrich as ge

        monkeypatch.delenv("BRUTEX_RISK_FREE_CSV", raising=False)
        captured: list[str] = []

        def _fake_enrich(*, targets: object, lake_root: Path, rate_source: object) -> object:
            del lake_root, rate_source
            captured.extend(t.symbol for t in cast("Sequence[object]", targets))  # type: ignore[attr-defined]
            return _distinct_report(ge)

        monkeypatch.setattr(ge, "enrich_greeks_for_targets", _fake_enrich)
        fut = _contract("NSE-NIFTY-25Jan24-FUT", "FUT", strike=None)
        hp._enrich_greeks_from_minute(
            rebuilt=_rebuilt(fut),
            windows={fut.symbol: _WINDOW},
            lake_root=tmp_path,
        )
        assert captured == [fut.symbol]

    def test_empty_rebuilt_still_drives_and_logs_done(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Empty ``rebuilt`` still calls the driver and logs ``done`` — the
        pre-refactor shape had NO early-out, and the delegation must not add one."""
        pytest.importorskip("trading_core")
        from brutex.data import greeks_enrich as ge

        monkeypatch.delenv("BRUTEX_RISK_FREE_CSV", raising=False)
        calls: list[list[object]] = []

        def _fake_enrich(*, targets: object, lake_root: Path, rate_source: object) -> object:
            del lake_root, rate_source
            calls.append(list(cast("Sequence[object]", targets)))
            return _distinct_report(
                ge,
                enriched=0,
                already_current=0,
                futures_skipped=0,
                undecodable=0,
                no_bars=0,
                errored=0,
                bars_written=0,
                bars_solved=0,
                bars_null=0,
                bars_reused=0,
                spot_reads=0,
                spot_hits=0,
                bars_skipped=0,
            )

        monkeypatch.setattr(ge, "enrich_greeks_for_targets", _fake_enrich)
        with capture_logs() as logs:
            outcome = hp._enrich_greeks_from_minute(
                rebuilt=_rebuilt(), windows={}, lake_root=tmp_path
            )
        assert calls == [[]]  # the driver ran, with an honestly-empty target list
        done = [r for r in logs if r["event"] == "fno.greeks_enrich.done"]
        assert len(done) == 1
        assert done[0]["enriched"] == 0
        assert outcome == gep.GreeksEnrichOutcome(ok=True, error=None, errored_targets=0)


# ── halt re-raise from within the dashboard wrapper ───────────────────────────


class TestDelegatorHaltReRaise:
    """The two pull-wide HOST halts re-raise out of the WRAPPER itself (not
    only out of ``pull_contract_candles`` — the sibling suite pins that leg)."""

    def _raise_through_wrapper(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, exc: BaseException
    ) -> list[EventDict]:
        pytest.importorskip("trading_core")
        from brutex.data import greeks_enrich as ge

        monkeypatch.delenv("BRUTEX_RISK_FREE_CSV", raising=False)

        def _halt(*, targets: object, lake_root: Path, rate_source: object) -> object:
            del targets, lake_root, rate_source
            raise exc

        monkeypatch.setattr(ge, "enrich_greeks_for_targets", _halt)
        contract = _contract("NSE-NIFTY-25Jan24-21000-CE", "CE")
        with capture_logs() as logs, pytest.raises(type(exc)):
            hp._enrich_greeks_from_minute(
                rebuilt=_rebuilt(contract),
                windows={contract.symbol: _WINDOW},
                lake_root=tmp_path,
            )
        return _enrich_events(logs)

    def test_lake_write_halt_reraises_with_zero_fenced_events(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        assert self._raise_through_wrapper(monkeypatch, tmp_path, LakeWriteHalt("disk_full")) == []

    def test_pool_broken_reraises_with_zero_fenced_events(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        assert (
            self._raise_through_wrapper(
                monkeypatch, tmp_path, GreeksEnrichPoolBroken("a worker died")
            )
            == []
        )

    def test_keyboard_interrupt_propagates_unfenced(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """``KeyboardInterrupt`` is ``BaseException`` — the pre-refactor fence never
        swallowed it and the moved fence must not either (zero fenced events)."""
        assert self._raise_through_wrapper(monkeypatch, tmp_path, KeyboardInterrupt()) == []


# ── the pull flow consumes the COMMON outcome dataclass ───────────────────────


class TestOutcomeConsumedByPullFlow:
    """``pull_contract_candles`` consumes the common outcome objects end-to-end."""

    @pytest.fixture
    def registry(self, tmp_path: Path) -> object:
        """A real temp :class:`RegistryStore` seeded with the NIFTY chain."""
        pytest.importorskip("duckdb", reason="RegistryStore needs duckdb")
        from brutex.data.registry_store import RegistryStore

        store = RegistryStore.open(tmp_path / "registry.duckdb")
        rows = [
            ("NSE-NIFTY-25Jan24-21000-CE", "CE", 21000.0),
            ("NSE-NIFTY-25Jan24-21000-PE", "PE", 21000.0),
            ("NSE-NIFTY-25Jan24-FUT", "FUT", None),
        ]
        for symbol, side, strike in rows:
            store.upsert_contract(
                "NSE",
                "NIFTY",
                _EXPIRY,
                symbol,
                side=side,
                strike=strike,
                run_id=_RUN_ID,
                fetched_at=_FETCHED,
            )
        return store

    @pytest.fixture
    def tmp_lake(self, tmp_path: Path) -> Path:
        """A real temp lake root for the live-shaped (``dry_run=False``) path."""
        root = tmp_path / "lake"
        root.mkdir()
        return root

    class _FakeSDK:
        """A minimal ``GrowwSDK``-shaped fake returning one canned candle."""

        def get_expiries(self, **_kw: object) -> dict[str, object]:
            """Never expected — the contracts come from the registry."""
            return {"expiries": []}

        def get_contracts(self, **_kw: object) -> dict[str, object]:
            """Never expected — the contracts come from the registry."""
            return {"contracts": []}

        def get_historical_candles(self, **_kw: object) -> dict[str, object]:
            """One 1-day bar (row-of-arrays Groww shape: ts, o, h, l, c, v, oi)."""
            return {"candles": [["2024-01-15T09:15:00", 100.0, 110.0, 90.0, 105.0, 50, 7]]}

    class _RecordingSink:
        """A minimal recording :class:`~brutex.data.sink.BarSink`."""

        def __init__(self) -> None:
            self.appends: list[tuple[str, str, str, int]] = []

        def append(
            self,
            *,
            exchange: str,
            segment: str,
            symbol: str,
            interval: str,
            bars: Sequence[object],
            merge_mode: str = "keep-first",
        ) -> SinkAppendResult:
            """Record ``(segment, symbol, interval, n_bars)``; report all appended."""
            del exchange, merge_mode
            self.appends.append((segment, symbol, interval, len(bars)))
            return SinkAppendResult(bars_appended=len(bars), partitions_written=1, write_results=())

    def _client_factory(self) -> object:
        """A fixed offline ``(client, sink)`` factory (no-sleep, single-attempt)."""
        from brutex.adapters.groww.client import GrowwClient

        client = GrowwClient(sdk=self._FakeSDK(), sleep=lambda _s: None, max_attempts=1)
        sink = self._RecordingSink()
        return lambda _root: (client, sink)

    def test_dry_run_report_carries_the_common_not_run_sentinel(
        self, registry: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A dry-run NEVER invokes the delegator, and the report's Greeks fields
        are exactly the COMMON ``GREEKS_NOT_RUN`` sentinel's fields — the pull
        flow initialises from the aliased common object."""
        from brutex.data.registry_store import RegistryStore

        def _never(**_kwargs: object) -> object:
            raise AssertionError("the delegator must not run on a dry-run")

        monkeypatch.setattr(hp, "_enrich_greeks_from_minute", _never)
        store = cast("RegistryStore", registry)
        report = hp.pull_contract_candles(
            store.list_contracts(underlying="NIFTY"),
            lake_root=Path("/unused"),
            dry_run=True,
            intervals=("1day",),
            registry=store,
            from_date=_FROM,
            to_date=_TO,
            client_factory=self._client_factory(),  # type: ignore[arg-type]
            now=_NOW,
        )
        assert report.greeks_enrich_ok is gep.GREEKS_NOT_RUN.ok
        assert report.greeks_enrich_error is gep.GREEKS_NOT_RUN.error
        assert report.greeks_targets_errored == gep.GREEKS_NOT_RUN.errored_targets

    def test_live_report_consumes_the_common_outcome_instance(
        self, registry: object, tmp_lake: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A live pull's report Greeks fields ARE the delegator's returned
        common-dataclass instance's fields — spied at the real seam, with the
        real fence + a fake driver (``errored=5`` reaches the report)."""
        pytest.importorskip("trading_core")
        from brutex.data import greeks_enrich as ge
        from brutex.data.registry_store import RegistryStore

        monkeypatch.delenv("BRUTEX_GROWW_FETCH_PIPELINE", raising=False)
        monkeypatch.delenv("BRUTEX_RISK_FREE_CSV", raising=False)

        def _fake_enrich(*, targets: object, lake_root: Path, rate_source: object) -> object:
            del targets, lake_root, rate_source
            return _distinct_report(ge, errored=5)

        monkeypatch.setattr(ge, "enrich_greeks_for_targets", _fake_enrich)
        real_delegator = hp._enrich_greeks_from_minute
        seen: list[object] = []

        def _spy(**kwargs: object) -> object:
            outcome = real_delegator(**kwargs)  # type: ignore[arg-type]
            seen.append(outcome)
            return outcome

        monkeypatch.setattr(hp, "_enrich_greeks_from_minute", _spy)
        store = cast("RegistryStore", registry)
        report = hp.pull_contract_candles(
            store.list_contracts(underlying="NIFTY"),
            lake_root=tmp_lake,
            dry_run=False,
            intervals=("1day",),
            registry=store,
            from_date=_FROM,
            to_date=_TO,
            client_factory=self._client_factory(),  # type: ignore[arg-type]
            now=_NOW,
        )
        assert len(seen) == 1  # the delegator ran exactly once, post-drain
        outcome = seen[0]
        assert isinstance(outcome, gep.GreeksEnrichOutcome)  # the COMMON dataclass
        assert outcome == gep.GreeksEnrichOutcome(ok=True, error=None, errored_targets=5)
        # The report's Greeks fields are the instance's own fields, last-mile.
        assert report.greeks_enrich_ok is outcome.ok
        assert report.greeks_enrich_error is outcome.error
        assert report.greeks_targets_errored == outcome.errored_targets
