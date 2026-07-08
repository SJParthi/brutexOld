"""Production ARMING-site wiring pins for the error-storm breaker (DEC-VENDOR-DOWN-BREAKER-001).

The 4-agent hostile review flagged the exact dormant-shipping failure mode
DEC-PULL-HEALTH-WIRING-001 (#935) / DEC-EMPTY-DETECTOR-PRODUCTION-WIRING-001
(#1014) previously had to fix post-hoc: the four production arming sites
(``pull_controller.pull_all_spot`` / ``pull_controller.pull_candles`` /
``historical_pull._run_phase`` / ``cli.cmd_fetch``) each carry a
``None if dry_run else resolve_error_storm_breaker()`` single-statement ternary,
so 100% line+branch held even if the constructed breaker never reached the spine
kwarg — deleting ``storm_breaker=_storm_breaker`` at any site left the whole
suite green and shipped the feature DORMANT.

These tests pin each site BEHAVIORALLY: patch ``resolve_error_storm_breaker`` in
the site's namespace to return a SENTINEL breaker, spy the spine entry the site
calls, and assert (a) a REAL (non-dry-run) pull threads the SENTINEL into the
spine's ``storm_breaker`` kwarg, and (b) a dry-run threads ``None`` AND never
even calls the resolver (the arming gate).
"""

# Whitebox test of the module-private `_run_phase` arming site (sanctioned).
# pyright: reportPrivateUsage=none

from __future__ import annotations

import importlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from brutex import cli
from brutex.adapters.error_storm_breaker import ErrorStormBreaker
from brutex.adapters.groww.option_ingest import GrowwOptionIngestReport
from brutex.adapters.ingest_common import StepOrderMonitor, SyntheticSpotAdapter
from brutex.adapters.protocol import DerivativeContract, MarketAdapter
from brutex.dashboard import historical_pull, pull_controller
from brutex.dashboard.pull_controller import pull_all_spot, pull_candles
from brutex.data.registry_store import ContractRow
from brutex.data.sink import BarSink, NullBarSink
from brutex.models import Exchange, Instrument, InstrumentType, Segment

_NOW = datetime(2026, 5, 30, 12, 0, tzinfo=UTC)


def _index(symbol: str = "13") -> Instrument:
    """A minimal spot index :class:`Instrument` (mirrors the pull-controller suite)."""
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=1,
        symbol_name=symbol,
        display_name=None,
        instrument=InstrumentType.IDX,
        segment=Segment.CASH,
        series=None,
        isin=None,
        underlying_symbol=None,
        underlying_security_id=None,
        lot_size=0,
        tick_size=Decimal("0"),
        sm_freeze_qty=None,
        sm_expiry_date=None,
        strike_price=None,
        asm_gsm_flag=False,
    )


def _sentinel() -> ErrorStormBreaker:
    """A distinguishable real breaker instance — the identity the sites must thread."""
    return ErrorStormBreaker()


def _never_resolve(**_kw: object) -> ErrorStormBreaker:
    """A resolver that must NEVER run on the dry-run arm of the arming ternary."""
    raise AssertionError("resolve_error_storm_breaker must not be called for a dry-run")


# ────────────────────────────────────────────────────────────────────────
# Site 1 — dashboard pull_all_spot (the bulk one-click pull)
# ────────────────────────────────────────────────────────────────────────


class TestPullAllSpotArming:
    def test_live_pull_threads_the_resolved_breaker(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sentinel = _sentinel()
        captured: list[object] = []
        real = pull_controller.run_groww_spot_ingest_with_retry

        def spy(*args: Any, **kwargs: Any) -> Any:
            captured.append(kwargs.get("storm_breaker"))
            return real(*args, **kwargs)

        monkeypatch.setattr(pull_controller, "run_groww_spot_ingest_with_retry", spy)
        monkeypatch.setattr(pull_controller, "resolve_error_storm_breaker", lambda: sentinel)

        def _factory(
            lake_root: Path, _insts: Sequence[Instrument]
        ) -> tuple[MarketAdapter, BarSink]:
            del lake_root
            return SyntheticSpotAdapter(), NullBarSink()

        pull_all_spot(
            [_index("13")],
            lake_root=tmp_path,
            dry_run=False,
            now=_NOW,
            adapter_factory=_factory,
        )
        assert captured, "the spine was never reached"
        assert all(b is sentinel for b in captured)

    def test_dry_run_threads_none_and_never_resolves(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: list[object] = []
        real = pull_controller.run_groww_spot_ingest_with_retry

        def spy(*args: Any, **kwargs: Any) -> Any:
            captured.append(kwargs.get("storm_breaker"))
            return real(*args, **kwargs)

        monkeypatch.setattr(pull_controller, "run_groww_spot_ingest_with_retry", spy)
        monkeypatch.setattr(pull_controller, "resolve_error_storm_breaker", _never_resolve)
        pull_all_spot([_index("13")], lake_root=tmp_path, dry_run=True, now=_NOW)
        assert captured, "the spine was never reached"
        assert all(b is None for b in captured)


# ────────────────────────────────────────────────────────────────────────
# Site 2 — dashboard pull_candles (the single-symbol pull)
# ────────────────────────────────────────────────────────────────────────


class TestPullCandlesArming:
    def test_live_pull_threads_the_resolved_breaker(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sentinel = _sentinel()
        captured: list[object] = []
        real = pull_controller.run_groww_spot_ingest

        def spy(*args: Any, **kwargs: Any) -> Any:
            captured.append(kwargs.get("storm_breaker"))
            return real(*args, **kwargs)

        monkeypatch.setattr(pull_controller, "run_groww_spot_ingest", spy)
        monkeypatch.setattr(pull_controller, "resolve_error_storm_breaker", lambda: sentinel)
        pull_candles(
            _index(),
            lake_root=tmp_path,
            from_date="2024-03-01",
            to_date="2024-03-31",
            dry_run=False,
            adapter_factory=lambda _root, _inst: (SyntheticSpotAdapter(), NullBarSink()),
        )
        assert captured, "the spine was never reached"
        assert all(b is sentinel for b in captured)

    def test_dry_run_threads_none_and_never_resolves(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: list[object] = []
        real = pull_controller.run_groww_spot_ingest

        def spy(*args: Any, **kwargs: Any) -> Any:
            captured.append(kwargs.get("storm_breaker"))
            return real(*args, **kwargs)

        monkeypatch.setattr(pull_controller, "run_groww_spot_ingest", spy)
        monkeypatch.setattr(pull_controller, "resolve_error_storm_breaker", _never_resolve)
        pull_candles(
            _index(),
            lake_root=tmp_path,
            from_date="2024-03-01",
            to_date="2024-03-31",
            dry_run=True,
        )
        assert captured, "the spine was never reached"
        assert all(b is None for b in captured)


# ────────────────────────────────────────────────────────────────────────
# Site 3 — dashboard historical_pull._run_phase (the F&O phase driver)
# ────────────────────────────────────────────────────────────────────────


def _fno_bucket() -> dict[tuple[str, bool], list[tuple[ContractRow, DerivativeContract]]]:
    row = ContractRow(
        exch="NSE",
        underlying="NIFTY",
        expiry_date=date(2025, 1, 2),
        contract_symbol="NSE-NIFTY-02Jan25-20000-CE",
        strike=20000.0,
        side="CE",
        candle_status="",
        last_run_id="",
        fetched_at=datetime(2025, 1, 1, tzinfo=UTC),
    )
    contract = DerivativeContract(
        underlying_symbol="NIFTY",
        expiry=date(2025, 1, 2),
        symbol="NSE-NIFTY-02Jan25-20000-CE",
        contract_type="CE",
        strike=20000.0,
    )
    # DEC-PULL-SIX-STEP-ORDER-001: the phase now takes (market, is_index) buckets.
    return {("groww-option", False): [(row, contract)]}


def _run_fno_phase(*, lake_root: Path, dry_run: bool) -> None:
    historical_pull._run_phase(
        interval="1minute",
        by_bucket=_fno_bucket(),
        selected_symbols=frozenset({"NSE-NIFTY-02Jan25-20000-CE"}),
        step_monitor=StepOrderMonitor(),
        client=object(),  # type: ignore[arg-type] — never touched, the ingest spine is a spy
        sink=NullBarSink(),
        lake_root=lake_root,
        dry_run=dry_run,
        start=date(2024, 12, 1),
        end=date(2025, 1, 2),
        pace=None,
        on_rate_limit=None,
        on_timeout=None,
        on_success=None,
        on_progress=None,
        session_rate=None,
        until_clean=False,
        until_clean_rounds=1,
        contract_windows=None,
        empty_retry_by_symbol=None,
        run_parallel=None,
        write_pool=None,
        fetch_pipeline=None,
    )


class TestHistoricalPullPhaseArming:
    def test_live_phase_threads_the_resolved_breaker(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sentinel = _sentinel()
        captured: list[object] = []

        def spy(*_args: Any, **kwargs: Any) -> GrowwOptionIngestReport:
            captured.append(kwargs.get("storm_breaker"))
            return GrowwOptionIngestReport(pairs={}, failures=())

        monkeypatch.setattr(historical_pull, "run_groww_option_ingest", spy)
        monkeypatch.setattr(historical_pull, "resolve_error_storm_breaker", lambda: sentinel)
        _run_fno_phase(lake_root=tmp_path, dry_run=False)
        assert captured, "the spine was never reached"
        assert all(b is sentinel for b in captured)

    def test_dry_run_threads_none_and_never_resolves(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: list[object] = []

        def spy(*_args: Any, **kwargs: Any) -> GrowwOptionIngestReport:
            captured.append(kwargs.get("storm_breaker"))
            return GrowwOptionIngestReport(pairs={}, failures=())

        monkeypatch.setattr(historical_pull, "run_groww_option_ingest", spy)
        monkeypatch.setattr(historical_pull, "resolve_error_storm_breaker", _never_resolve)
        _run_fno_phase(lake_root=tmp_path, dry_run=True)
        assert captured, "the spine was never reached"
        assert all(b is None for b in captured)


# ────────────────────────────────────────────────────────────────────────
# Site 4 — CLI cmd_fetch (the `brutex fetch` live pull)
# ────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _FakeIngestReport:
    """Duck-typed ``GrowwIngestReport`` surface the fetch summary printing reads."""

    windows: int = 1
    bars_appended: int = 0
    failed_pairs: int = 0
    transient_failures: int = 0
    still_failed_windows: int = 0
    pairs: dict[tuple[str, str], Any] = field(default_factory=dict)


@dataclass(frozen=True)
class _FakeIngestResult:
    """Duck-typed ``SpotIngestWithRetry`` — a clean pull (no retry ran)."""

    report: _FakeIngestReport = field(default_factory=_FakeIngestReport)
    retry: None = None

    @property
    def still_failed_windows(self) -> int:
        return self.report.still_failed_windows


def _wire_cli_fakes(
    monkeypatch: pytest.MonkeyPatch, captured: list[object], *, kwarg: str = "storm_breaker"
) -> None:
    """Fake every network-shaped CLI seam + spy the spine's *kwarg* breaker kwarg."""
    monkeypatch.setattr(cli, "_resolve_brute_force_instruments", lambda _lake: [_index("NIFTY")])
    credentials = importlib.import_module("brutex.config.credentials")
    monkeypatch.setattr(credentials, "groww_credentials_error", lambda: None)
    monkeypatch.setattr(
        cli, "_build_live_groww_spot", lambda _lake, catalog=None: (object(), object())
    )
    run_ingest = importlib.import_module("brutex.adapters.groww.run_ingest")

    def fake_ingest(*_args: Any, **kwargs: Any) -> _FakeIngestResult:
        captured.append(kwargs.get(kwarg))
        return _FakeIngestResult()

    monkeypatch.setattr(run_ingest, "run_groww_spot_ingest_with_retry", fake_ingest)


class TestCliCmdFetchArming:
    def test_live_pull_threads_the_resolved_breaker(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pytest.importorskip("polars")
        pytest.importorskip("duckdb")
        pytest.importorskip("pyarrow")
        sentinel = _sentinel()
        captured: list[object] = []
        _wire_cli_fakes(monkeypatch, captured)
        esb = importlib.import_module("brutex.adapters.error_storm_breaker")
        monkeypatch.setattr(esb, "resolve_error_storm_breaker", lambda: sentinel)
        rc = cli.cmd_fetch(
            tmp_path,
            "2024-01-02",
            "2024-01-03",
            "1m",
            window_days=None,
            dry_run=False,
            inspect=True,  # persistence-free live wiring (NullBarSink, no ledger)
            rows_per_chunk=5,
        )
        assert rc == cli.EXIT_OK
        assert captured, "the spine was never reached"
        assert all(b is sentinel for b in captured)

    def test_dry_run_threads_none_and_never_resolves(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pytest.importorskip("polars")
        pytest.importorskip("duckdb")
        pytest.importorskip("pyarrow")
        captured: list[object] = []
        _wire_cli_fakes(monkeypatch, captured)
        esb = importlib.import_module("brutex.adapters.error_storm_breaker")
        monkeypatch.setattr(esb, "resolve_error_storm_breaker", _never_resolve)
        rc = cli.cmd_fetch(
            tmp_path,
            "2024-01-02",
            "2024-01-03",
            "1m",
            window_days=None,
            dry_run=True,
            inspect=True,
            rows_per_chunk=5,
        )
        assert rc == cli.EXIT_OK
        assert captured, "the spine was never reached"
        assert all(b is None for b in captured)


# ────────────────────────────────────────────────────────────────────────
# Mass-empty breaker CANARY arming — historical_pull._run_phase
# (DEC-EMPTY-BREAKER-CANARY-001; the same dormant-shipping pin as above)
# ────────────────────────────────────────────────────────────────────────


class TestBreakerCanaryArming:
    def test_live_phase_threads_the_built_canary_probe(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A REAL pull builds the canary from THIS phase's client + pace and
        threads it into the spine's ``breaker_health_probe`` kwarg."""

        def _sentinel_probe() -> bool:  # pragma: no cover — identity only
            return True

        built: list[tuple[object, object]] = []

        def _fake_builder(client: object, *, pace: object = None) -> object:
            built.append((client, pace))
            return _sentinel_probe

        captured: list[object] = []

        def spy(*_args: Any, **kwargs: Any) -> GrowwOptionIngestReport:
            captured.append(kwargs.get("breaker_health_probe"))
            return GrowwOptionIngestReport(pairs={}, failures=())

        monkeypatch.setattr(historical_pull, "run_groww_option_ingest", spy)
        monkeypatch.setattr(historical_pull, "make_breaker_canary_probe", _fake_builder)
        _run_fno_phase(lake_root=tmp_path, dry_run=False)
        assert len(built) == 1, "the canary is built ONCE per phase"
        assert captured, "the spine was never reached"
        assert all(p is _sentinel_probe for p in captured)

    def test_dry_run_threads_none_and_never_builds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A dry-run never builds a canary (no vendor call can exist) — None."""

        def _never_build(_client: object, *, pace: object = None) -> object:
            raise AssertionError("dry-run must never build a canary probe")

        captured: list[object] = []

        def spy(*_args: Any, **kwargs: Any) -> GrowwOptionIngestReport:
            captured.append(kwargs.get("breaker_health_probe"))
            return GrowwOptionIngestReport(pairs={}, failures=())

        monkeypatch.setattr(historical_pull, "run_groww_option_ingest", spy)
        monkeypatch.setattr(historical_pull, "make_breaker_canary_probe", _never_build)
        _run_fno_phase(lake_root=tmp_path, dry_run=True)
        assert captured, "the spine was never reached"
        assert all(p is None for p in captured)


# ────────────────────────────────────────────────────────────────────────
# Mass ALL-FILTERED breaker — the PULL-scope resolve law
# (DEC-ALLFILTERED-MASS-BREAKER-001 review round 4: each production pull
# entrypoint resolves ONE breaker and threads the SAME instance into every
# pass / phase / until-clean round, so a trip stays LATCHED for the pull —
# the superseded per-pass resolve let a sub-floor retry pass settle windows
# the trip alarm promised were halted. Unlike the storm breaker there is NO
# dry-run gate: the instance is resolved unconditionally and stays inert on
# a dry-run because its windows are ineligible by construction.)
# ────────────────────────────────────────────────────────────────────────


class TestAllFilteredBreakerPullScope:
    def test_pull_all_spot_resolves_once_and_threads_it_everywhere(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from brutex.adapters.all_filtered_breaker import AllFilteredBreaker

        sentinel = AllFilteredBreaker()
        resolves: list[object] = []

        def _resolve_once() -> AllFilteredBreaker:
            resolves.append(sentinel)
            return sentinel

        captured: list[object] = []
        real = pull_controller.run_groww_spot_ingest_with_retry

        def spy(*args: Any, **kwargs: Any) -> Any:
            captured.append(kwargs.get("all_filtered_breaker"))
            return real(*args, **kwargs)

        monkeypatch.setattr(pull_controller, "run_groww_spot_ingest_with_retry", spy)
        monkeypatch.setattr(pull_controller, "resolve_all_filtered_breaker", _resolve_once)

        def _factory(
            lake_root: Path, _insts: Sequence[Instrument]
        ) -> tuple[MarketAdapter, BarSink]:
            del lake_root
            return SyntheticSpotAdapter(), NullBarSink()

        pull_all_spot(
            [_index("13")],
            lake_root=tmp_path,
            dry_run=False,
            now=_NOW,
            adapter_factory=_factory,
        )
        assert captured, "the spine was never reached"
        assert all(b is sentinel for b in captured)
        assert len(resolves) == 1, "ONE breaker per pull — the round-4 latch scope"

    def test_pull_candles_resolves_once_and_threads_both_phases(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from brutex.adapters.all_filtered_breaker import AllFilteredBreaker

        sentinel = AllFilteredBreaker()
        resolves: list[object] = []

        def _resolve_once() -> AllFilteredBreaker:
            resolves.append(sentinel)
            return sentinel

        captured: list[object] = []
        real = pull_controller.run_groww_spot_ingest

        def spy(*args: Any, **kwargs: Any) -> Any:
            captured.append(kwargs.get("all_filtered_breaker"))
            return real(*args, **kwargs)

        monkeypatch.setattr(pull_controller, "run_groww_spot_ingest", spy)
        monkeypatch.setattr(pull_controller, "resolve_all_filtered_breaker", _resolve_once)
        pull_candles(
            _index(),
            lake_root=tmp_path,
            from_date="2024-03-01",
            to_date="2024-03-31",
            dry_run=False,
            adapter_factory=lambda _root, _inst: (SyntheticSpotAdapter(), NullBarSink()),
        )
        assert captured, "the spine was never reached"
        assert all(b is sentinel for b in captured)
        assert len(resolves) == 1, "ONE breaker per pull, shared by every phase"

    def test_pull_all_spot_live_arms_detect_and_feeds_the_breaker(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A REAL bulk pull's windows actually REACH the breaker (review round 5).

        FAIL-BEFORE: ``pull_all_spot`` never passed ``detect_completeness_gaps``
        (default ``False``), so the spot tail's eligibility gate made EVERY bulk
        window unobservable — the round-4 pull-scoped wiring was structurally
        inert on the primary production path (the /pull-pipeline spot phase +
        the one-click "Pull ALL"). Post-fix the bulk pull threads
        ``detect_completeness_gaps=not dry_run`` (the CLI ``cmd_fetch`` pattern)
        and a 3-series weekend-start mass all-filtered event TRIPS the breaker.
        """
        from brutex.adapters.all_filtered_breaker import AllFilteredBreaker

        # SPREAD floor 3 (min_observations 100 keeps the FRACTION rule out of
        # the way) — the 3-index vendor-day shape must trip on the 3rd series.
        sentinel = AllFilteredBreaker(
            spread_series_limit=3,
            fraction_limit=0.5,
            min_observations=100,
            fraction_min_series=3,
        )
        detect_seen: list[object] = []
        real = pull_controller.run_groww_spot_ingest_with_retry

        def spy(*args: Any, **kwargs: Any) -> Any:
            detect_seen.append(kwargs.get("detect_completeness_gaps"))
            return real(*args, **kwargs)

        monkeypatch.setattr(pull_controller, "run_groww_spot_ingest_with_retry", spy)
        monkeypatch.setattr(pull_controller, "resolve_all_filtered_breaker", lambda: sentinel)

        def _factory(
            lake_root: Path, _insts: Sequence[Instrument]
        ) -> tuple[MarketAdapter, BarSink]:
            del lake_root
            return SyntheticSpotAdapter(), NullBarSink()

        # 2024-03-02 is a Saturday: every window's synthetic bars anchor on the
        # window-START day, so the calendar-aware boundary filter drops them ALL
        # — three genuinely all-filtered series, the mass vendor-day shape.
        pull_all_spot(
            [_index("A1"), _index("A2"), _index("A3")],
            lake_root=tmp_path,
            dry_run=False,
            now=_NOW,
            from_date="2024-03-02",
            to_date="2024-03-03",
            adapter_factory=_factory,
        )
        assert detect_seen and all(d is True for d in detect_seen)
        assert sentinel.counts() == (3, 3, 3)  # fail-before: (0, 0, 0)
        assert sentinel.tripped

    def test_pull_all_spot_dry_run_threads_detect_false_and_never_observes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A dry-run bulk pull threads ``detect_completeness_gaps=False`` — the
        resolved breaker stays inert (the comment's "ineligible by construction"
        claim, now enforced by explicit wiring instead of a missing kwarg)."""
        from brutex.adapters.all_filtered_breaker import AllFilteredBreaker

        sentinel = AllFilteredBreaker()
        detect_seen: list[object] = []
        real = pull_controller.run_groww_spot_ingest_with_retry

        def spy(*args: Any, **kwargs: Any) -> Any:
            detect_seen.append(kwargs.get("detect_completeness_gaps"))
            return real(*args, **kwargs)

        monkeypatch.setattr(pull_controller, "run_groww_spot_ingest_with_retry", spy)
        monkeypatch.setattr(pull_controller, "resolve_all_filtered_breaker", lambda: sentinel)
        pull_all_spot(
            [_index("A1")],
            lake_root=tmp_path,
            dry_run=True,
            now=_NOW,
            from_date="2024-03-02",
            to_date="2024-03-03",
        )
        assert detect_seen and all(d is False for d in detect_seen)
        assert sentinel.counts() == (0, 0, 0)
        assert not sentinel.tripped

    def test_pull_candles_dry_run_threads_detect_false_and_never_observes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A dry-run ``pull_candles`` never feeds the breaker (review round 5).

        FAIL-BEFORE: this site hardcoded ``detect_completeness_gaps=True`` (a
        #1230-era leftover), so a dry-run's weekend-start synthetic window WAS
        observed as all-filtered — counts ``(2, 1, 1)`` over 2024-03 (the
        2024-03-31 Sunday-start tile) — contradicting the documented
        dry-run-never-feeds contract. Post-fix: ``not dry_run`` → ``(0, 0, 0)``.
        """
        from brutex.adapters.all_filtered_breaker import AllFilteredBreaker

        sentinel = AllFilteredBreaker()
        detect_seen: list[object] = []
        real = pull_controller.run_groww_spot_ingest

        def spy(*args: Any, **kwargs: Any) -> Any:
            detect_seen.append(kwargs.get("detect_completeness_gaps"))
            return real(*args, **kwargs)

        monkeypatch.setattr(pull_controller, "run_groww_spot_ingest", spy)
        monkeypatch.setattr(pull_controller, "resolve_all_filtered_breaker", lambda: sentinel)
        pull_candles(
            _index(),
            lake_root=tmp_path,
            from_date="2024-03-01",
            to_date="2024-03-31",
            dry_run=True,
        )
        assert detect_seen and all(d is False for d in detect_seen)
        assert sentinel.counts() == (0, 0, 0)  # fail-before: (2, 1, 1)
        assert not sentinel.tripped

    def test_pull_candles_live_still_arms_detect(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The round-5 ``not dry_run`` flip keeps the REAL single-symbol pull armed."""
        detect_seen: list[object] = []
        real = pull_controller.run_groww_spot_ingest

        def spy(*args: Any, **kwargs: Any) -> Any:
            detect_seen.append(kwargs.get("detect_completeness_gaps"))
            return real(*args, **kwargs)

        monkeypatch.setattr(pull_controller, "run_groww_spot_ingest", spy)
        pull_candles(
            _index(),
            lake_root=tmp_path,
            from_date="2024-03-01",
            to_date="2024-03-31",
            dry_run=False,
            adapter_factory=lambda _root, _inst: (SyntheticSpotAdapter(), NullBarSink()),
        )
        assert detect_seen and all(d is True for d in detect_seen)

    def test_cli_cmd_fetch_resolves_and_threads_the_pull_breaker(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pytest.importorskip("polars")
        pytest.importorskip("duckdb")
        pytest.importorskip("pyarrow")
        from brutex.adapters.all_filtered_breaker import AllFilteredBreaker

        sentinel = AllFilteredBreaker()
        resolves: list[object] = []

        def _resolve_once() -> AllFilteredBreaker:
            resolves.append(sentinel)
            return sentinel

        captured: list[object] = []
        _wire_cli_fakes(monkeypatch, captured, kwarg="all_filtered_breaker")
        afb = importlib.import_module("brutex.adapters.all_filtered_breaker")
        monkeypatch.setattr(afb, "resolve_all_filtered_breaker", _resolve_once)
        rc = cli.cmd_fetch(
            tmp_path,
            "2024-01-02",
            "2024-01-03",
            "1m",
            window_days=None,
            dry_run=False,
            inspect=True,  # persistence-free live wiring (NullBarSink, no ledger)
            rows_per_chunk=5,
        )
        assert rc == cli.EXIT_OK
        assert captured, "the spine was never reached"
        assert all(b is sentinel for b in captured)
        assert len(resolves) == 1, "ONE breaker per pull — the round-4 latch scope"
