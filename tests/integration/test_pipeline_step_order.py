# pyright: reportPrivateUsage=none
"""THE six-step ordering regression net (DEC-PULL-SIX-STEP-ORDER-001) — T7.

A log-shaped integration fixture modelled on the operator's real 2026-07-05
production pull (06:44-07:01 IST), which ingested stock ADANIENT spot BEFORE
index NIFTY at 06:44:40, interleaved futures at 06:45:14-26, and ran ADANIENT
options at 06:45:19 BEFORE NIFTY options at 06:45:28. The full sequencer
(:func:`~brutex.dashboard.pipeline_pull.run_full_pipeline`) is driven with the
REAL ``pull_all_spot`` (recording synthetic adapter), the REAL
``pull_expiries_contracts`` (fake vendor client into a real registry) and the
REAL ``pull_contract_candles`` (recording fake client), asserting:

* the EXACT ``pull.step.started`` sequence 1→2→3→4→5→6 (empty groups skipped);
* every ``pull.step.settled(N)`` strictly precedes ``pull.step.started(N+1)``;
* every post-step (Greeks enrichment → prebuild → movers warm) fires strictly
  AFTER the last ``pull.step.settled``;
* every NIFTY spot fetch issues before ADANIENT's first spot fetch, and every
  index-option fetch before any stock-option fetch (the exact 2026-07-05
  inversions);
* ONE :class:`~brutex.adapters.ingest_common.StepOrderMonitor` with
  ``raise_on_violation=True`` spans BOTH spines as the hard rank oracle.

Variants cover the design's edge cases 1/2/3/5/7/10/11/12 (the remaining
edges 4/6/8/9/13 are pinned in T1/T3/T5/T6). Fully offline — synthetic
adapters + fake SDKs, zero network, zero credentials.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator, Mapping, MutableMapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.adapters.groww.client import GrowwClient
from brutex.adapters.ingest_common import StepOrderMonitor, SyntheticSpotAdapter
from brutex.adapters.protocol import (
    DerivativeContract,
    InstrumentPage,
    MarketCapabilities,
)
from brutex.dashboard.composite_pull import (
    CompositePullReport,
    _underlying_kind_map,
)
from brutex.dashboard.historical_pull import pull_contract_candles
from brutex.dashboard.metadata_pull import MetadataPullReport, pull_expiries_contracts
from brutex.dashboard.movers_warm import MoversWarmResult
from brutex.dashboard.pipeline_prebuild import PrebuildOutcome
from brutex.dashboard.pipeline_pull import (
    PipelinePullReport,
    PriorStepsVerdict,
    run_full_pipeline,
)
from brutex.dashboard.pull_controller import BulkSpotReport, pull_all_spot
from brutex.data.coverage_ledger import CoverageRecord
from brutex.data.models import CandleBar
from brutex.data.registry_store import RegistryStore
from brutex.data.sink import BarSink
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from brutex.orchestrator.universe_phases import ItemResult
from structlog.testing import capture_logs

pytest.importorskip("duckdb", reason="RegistryStore needs duckdb")

_NOW = datetime(2026, 6, 14, 9, 15, tzinfo=UTC)
_TODAY = date(2026, 6, 14)
_SPOT_FROM = "2024-01-02"
_SPOT_TO = "2024-01-03"
_FNO_FROM = date(2024, 1, 1)
_FNO_TO = date(2024, 1, 20)

_NIFTY_FUT = "NSE-NIFTY-25Jan24-FUT"
_NIFTY_CE = "NSE-NIFTY-25Jan24-21000-CE"
_ADANI_FUT = "NSE-ADANIENT-25Jan24-FUT"
_ADANI_CE = "NSE-ADANIENT-25Jan24-3000-CE"

#: The decreed full six-step label sequence (DEC-PULL-SIX-STEP-ORDER-001).
#: The captured-log event shape (structlog ``capture_logs`` yields these).
type _LogEvent = MutableMapping[str, object]

_SIX_STEPS = (
    "indices-spot",
    "stocks-spot",
    "indices-futures",
    "stocks-futures",
    "indices-options",
    "stocks-options",
)


# ── minimal offline fixtures ──────────────────────────────────────────────────


def _instrument(symbol: str, kind: InstrumentType, security_id: int) -> Instrument:
    """A minimal spot instrument of the given *kind*."""
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=security_id,
        symbol_name=symbol,
        display_name=symbol,
        instrument=kind,
        segment=Segment.INDEX if kind == InstrumentType.IDX else Segment.CASH,
        series=None,
        isin=None,
        underlying_symbol=None,
        underlying_security_id=None,
        lot_size=0 if kind == InstrumentType.IDX else 500,
        tick_size=Decimal("0") if kind == InstrumentType.IDX else Decimal("0.05"),
        sm_freeze_qty=None,
        sm_expiry_date=None,
        strike_price=None,
        asm_gsm_flag=False,
    )


def _nifty() -> Instrument:
    return _instrument("NIFTY", InstrumentType.IDX, 13)


def _adanient() -> Instrument:
    return _instrument("ADANIENT", InstrumentType.EQ, 25)


class _RecordingSpotAdapter:
    """A synthetic spot adapter recording every fetched symbol (thread-safe)."""

    def __init__(self) -> None:
        self._inner = SyntheticSpotAdapter()
        self.fetched: list[str] = []
        self._lock = threading.Lock()

    @property
    def capabilities(self) -> MarketCapabilities:
        """Delegate the capability surface."""
        return self._inner.capabilities

    def list_instruments(self, *, cursor: str | None = None) -> InstrumentPage:
        """Delegate the instrument page."""
        return self._inner.list_instruments(cursor=cursor)

    def list_derivatives(
        self, *, underlying_symbol: str, exchange: Exchange
    ) -> Iterator[DerivativeContract]:
        """Delegate the (empty) derivatives iterator."""
        return self._inner.list_derivatives(underlying_symbol=underlying_symbol, exchange=exchange)

    def fetch_ohlcv(
        self, *, symbol: str, exchange: Exchange, interval: str, start: date, end: date
    ) -> list[CandleBar]:
        """Record the symbol, then return the synthetic bars."""
        with self._lock:
            self.fetched.append(symbol)
        return self._inner.fetch_ohlcv(
            symbol=symbol, exchange=exchange, interval=interval, start=start, end=end
        )


class _FakeSDK:
    """A ``GrowwSDK``-shaped fake serving metadata AND candles off one client."""

    def __init__(
        self,
        *,
        expiries: Mapping[tuple[str, str, int], Sequence[str]] | None = None,
        contracts: Mapping[tuple[str, str, str], Sequence[str]] | None = None,
    ) -> None:
        self._expiries = dict(expiries or {})
        self._contracts = dict(contracts or {})
        self.candle_symbols: list[str] = []

    def get_expiries(self, **kw: object) -> Mapping[str, object]:
        key = (str(kw["exchange"]), str(kw["underlying_symbol"]), int(str(kw["year"])))
        return {"expiries": list(self._expiries.get(key, ()))}

    def get_contracts(self, **kw: object) -> Mapping[str, object]:
        key = (str(kw["exchange"]), str(kw["underlying_symbol"]), str(kw["expiry_date"]))
        return {"contracts": list(self._contracts.get(key, ()))}

    def get_historical_candles(self, **kw: object) -> dict[str, object]:
        self.candle_symbols.append(str(kw["groww_symbol"]))
        # 2024-01-19 (a regular NSE Friday) sits inside every window this file
        # fetches, so the outcome is a genuine stored-bar SUCCESS (never an
        # all-filtered demotion).
        return {"candles": [["2024-01-19T09:15:00", 100.0, 110.0, 90.0, 105.0, 50, 7]]}


def _mixed_chain_sdk() -> _FakeSDK:
    """The 2026-07-05 log-shaped chain: NIFTY + ADANIENT, each FUT + CE."""
    return _FakeSDK(
        expiries={
            ("NSE", "NIFTY", 2024): ["2024-01-25"],
            ("NSE", "ADANIENT", 2024): ["2024-01-25"],
        },
        contracts={
            ("NSE", "NIFTY", "2024-01-25"): [_NIFTY_CE, _NIFTY_FUT],
            ("NSE", "ADANIENT", "2024-01-25"): [_ADANI_CE, _ADANI_FUT],
        },
    )


class _CoverageSink:
    """A tiny in-memory ``coverage_ledger`` rollup sink (PK upsert, list)."""

    def __init__(self) -> None:
        self._by_cell: dict[tuple[str, str, int], CoverageRecord] = {}

    def upsert_coverage(self, record: CoverageRecord) -> None:
        """Upsert one per-(underlying, year) rollup row."""
        self._by_cell[(record.exch_id, record.underlying_symbol, record.year)] = record

    def list_coverage(self) -> list[CoverageRecord]:
        """All rollup rows."""
        return list(self._by_cell.values())


class _SyncRunParallel:
    """A synchronous, deterministic ``RunParallel`` fake — zero threads."""

    def __call__[ItemT, ResultT](
        self, fn: Callable[[ItemT], ResultT], items: Sequence[ItemT]
    ) -> list[ItemResult[ItemT, ResultT]]:
        """Run *fn* over *items* serially, capturing per-item exceptions."""
        out: list[ItemResult[ItemT, ResultT]] = []
        for item in items:
            try:
                result = fn(item)
            except Exception as exc:  # mirror the pool's capture-not-propagate
                out.append(ItemResult(item=item, ok=False, result=None, exc=exc))
            else:
                out.append(ItemResult(item=item, ok=True, result=result, exc=None))
        return out


_sync = _SyncRunParallel()


def _real_sink(lake_root: Path) -> BarSink:
    """A real catalog-free ``ParquetBarSink`` so ledger + lake are exercised."""
    from brutex.data.parquet_writer import ParquetWriter
    from brutex.data.sink import ParquetBarSink

    return ParquetBarSink(writer=ParquetWriter(lake_root=lake_root))


def _events(logs: Sequence[_LogEvent], name: str) -> list[_LogEvent]:
    """All captured events with ``event == name`` (in emission order)."""
    return [e for e in logs if e["event"] == name]


class _PostStepProbe:
    """Fake prebuild + movers-warm callables capturing WHEN they fired.

    Each callable records ``len(logs)`` at invocation — the number of structlog
    events already emitted — so the barrier assertion "post-steps fire strictly
    after the last ``pull.step.settled``" is positional, not inferential.
    """

    def __init__(self, logs: Sequence[_LogEvent]) -> None:
        self._logs = logs
        self.prebuild_at: int | None = None
        self.warm_at: int | None = None

    def run_prebuild(self, exclude_symbols: frozenset[str]) -> PrebuildOutcome:
        """Record the position + the exclusion set; report a clean prebuild."""
        del exclude_symbols
        self.prebuild_at = len(self._logs)
        return PrebuildOutcome(symbols_built=1, total_bars=1, failed=(), failed_count=0, ok=True)

    def warm(self) -> MoversWarmResult:
        """Record the movers-warm position; report one warmed snapshot."""
        self.warm_at = len(self._logs)
        return MoversWarmResult(warmed=1, failed=0, total=1)


def _run_pipeline(
    tmp_path: Path,
    instruments: list[Instrument],
    sdk: _FakeSDK,
    *,
    spot_adapter: _RecordingSpotAdapter | None = None,
    reference_only_ids: frozenset[int] = frozenset(),
    monitor: StepOrderMonitor | None = None,
    probe: _PostStepProbe | None = None,
    fno_dates: bool = True,
    seed_registry: Callable[[RegistryStore], None] | None = None,
) -> PipelinePullReport:
    """Drive the REAL sequencer + spines over the offline fixtures.

    ``run_spot`` is the real :func:`pull_all_spot` (recording synthetic
    adapter); ``run_metadata`` the real :func:`pull_expiries_contracts` (fake
    vendor client into a real registry); ``run_historical`` the real
    :func:`pull_contract_candles` over the freshly-persisted registry rows —
    with ONE shared :class:`StepOrderMonitor` spanning both spines (the hard
    rank oracle when ``raise_on_violation=True``) and the ``prior_steps``
    verdict threaded exactly as the production route does.
    """
    lake_root = tmp_path / "lake"
    registry = RegistryStore.open(tmp_path / "registry.duckdb")
    tradable = [i for i in instruments if i.security_id not in reference_only_ids]
    adapter = spot_adapter or _RecordingSpotAdapter()
    step_monitor = monitor or StepOrderMonitor(raise_on_violation=True)
    if seed_registry is not None:
        seed_registry(registry)

    def _run_spot() -> BulkSpotReport:
        return pull_all_spot(
            instruments,
            lake_root=lake_root,
            dry_run=False,
            now=_NOW,
            from_date=_SPOT_FROM,
            to_date=_SPOT_TO,
            adapter_factory=lambda root, _insts: (adapter, _real_sink(root)),
            step_monitor=step_monitor,
        )

    def _run_metadata() -> MetadataPullReport:
        return pull_expiries_contracts(
            tradable,
            registry=registry,
            coverage_store=_CoverageSink(),
            years=[2024],
            dry_run=False,
            run_parallel=_sync,
            client_factory=lambda: GrowwClient(sdk=sdk, sleep=lambda _s: None, max_attempts=1),
            now=_NOW,
            today=_TODAY,
        )

    def _run_historical(prior_steps: PriorStepsVerdict) -> CompositePullReport:
        historical = pull_contract_candles(
            registry.list_contracts(),
            lake_root=lake_root,
            dry_run=False,
            intervals=("1minute",),
            registry=registry,
            from_date=_FNO_FROM if fno_dates else None,
            to_date=_FNO_TO if fno_dates else None,
            now=_NOW,
            client_factory=lambda _root: (
                GrowwClient(sdk=sdk, sleep=lambda _s: None, max_attempts=1),
                _real_sink(lake_root),
            ),
            run_parallel=_sync,
            underlying_kinds=_underlying_kind_map(tradable),
            step_monitor=step_monitor,
            prior_steps=prior_steps,
        )
        return CompositePullReport(
            dry_run=False,
            requested=tuple(i.symbol_name for i in instruments),
            tradable=tuple(i.symbol_name for i in tradable),
            skipped_reference_only=tuple(
                i.symbol_name for i in instruments if i.security_id in reference_only_ids
            ),
            metadata=None,
            historical=historical,
            note="integration harness",
        )

    return run_full_pipeline(
        run_spot=_run_spot,
        run_metadata=_run_metadata,
        run_historical=_run_historical,
        warm_spot_movers=probe.warm if probe is not None else None,
        run_prebuild=probe.run_prebuild if probe is not None else None,
    )


# ── THE ordering regression (the 2026-07-05 log shape; edge 4) ────────────────


class TestSixStepOrderRegression:
    """The exact event sequence + fetch inversions of the motivating defect."""

    @pytest.fixture(autouse=True)
    def _sync_writes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Synchronous write tails: deterministic settle ordering (the async
        # producer-side settle envelope is documented + separately pinned).
        monkeypatch.setenv("BRUTEX_GROWW_FETCH_PIPELINE", "off")

    def test_mixed_pull_runs_exact_six_step_sequence(self, tmp_path: Path) -> None:
        adapter = _RecordingSpotAdapter()
        sdk = _mixed_chain_sdk()
        with capture_logs() as logs:
            probe = _PostStepProbe(logs)
            report = _run_pipeline(
                tmp_path,
                # Deliberately STOCK-FIRST input — the 2026-07-05 defect shape.
                [_adanient(), _nifty()],
                sdk,
                spot_adapter=adapter,
                monitor=StepOrderMonitor(raise_on_violation=True),
                probe=probe,
            )
        assert report.ok is True
        started = _events(logs, "pull.step.started")
        settled = _events(logs, "pull.step.settled")
        # (a) The EXACT decreed sequence 1→2→3→4→5→6.
        assert tuple(e["step"] for e in started) == _SIX_STEPS
        assert [e["ordinal"] for e in started] == [1, 2, 3, 4, 5, 6]
        assert tuple(e["step"] for e in settled) == _SIX_STEPS
        # (b) settled(N) strictly precedes started(N+1) — the settle barrier.
        for k in range(len(_SIX_STEPS) - 1):
            assert logs.index(settled[k]) < logs.index(started[k + 1]), _SIX_STEPS[k]
        # (c) The 2026-07-05 inversions can never recur: every NIFTY spot fetch
        # precedes ADANIENT's first spot fetch…
        assert adapter.fetched
        last_nifty_spot = max(i for i, s in enumerate(adapter.fetched) if s == "NIFTY")
        first_adani_spot = min(i for i, s in enumerate(adapter.fetched) if s == "ADANIENT")
        assert last_nifty_spot < first_adani_spot, adapter.fetched
        # …and every index-option fetch precedes any stock-option fetch (plus
        # the futures steps never interleave with options).
        assert sdk.candle_symbols.index(_NIFTY_CE) < sdk.candle_symbols.index(_ADANI_CE)
        assert sdk.candle_symbols == [_NIFTY_FUT, _ADANI_FUT, _NIFTY_CE, _ADANI_CE]
        # (d) POST-steps fire strictly AFTER the last pull.step.settled:
        last_settle_at = logs.index(settled[-1])
        greeks_done = _events(logs, "fno.greeks_enrich.done")
        assert len(greeks_done) == 1
        assert logs.index(greeks_done[0]) > last_settle_at
        assert probe.prebuild_at is not None and probe.prebuild_at > last_settle_at
        assert probe.warm_at is not None and probe.warm_at > last_settle_at
        # Greeks → prebuild → movers, in that order (the post-step chain).
        assert logs.index(greeks_done[0]) < probe.prebuild_at < probe.warm_at
        warm_started = _events(logs, "pipeline.movers_warm.started")
        assert len(warm_started) == 1
        assert logs.index(warm_started[0]) > last_settle_at
        # The observe-only monitor saw no rank regression (hard oracle passed)
        # and counted no late writes on the synchronous path.
        assert report.post_steps_strict_skipped is False


# ── variant edges (1 / 2 / 3+11 / 5 / 7 / 12) ────────────────────────────────


class TestStepOrderVariants:
    """Selection-shape variants: empty groups skipped, spot always first."""

    @pytest.fixture(autouse=True)
    def _sync_writes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRUTEX_GROWW_FETCH_PIPELINE", "off")

    def test_indices_only_runs_steps_1_3_5(self, tmp_path: Path) -> None:
        """Edge 1: an indices-only selection runs 1→3→5; stock steps never exist."""
        sdk = _FakeSDK(
            expiries={("NSE", "NIFTY", 2024): ["2024-01-25"]},
            contracts={("NSE", "NIFTY", "2024-01-25"): [_NIFTY_CE, _NIFTY_FUT]},
        )
        with capture_logs() as logs:
            report = _run_pipeline(tmp_path, [_nifty()], sdk)
        assert report.ok is True
        started = _events(logs, "pull.step.started")
        assert [e["step"] for e in started] == [
            "indices-spot",
            "indices-futures",
            "indices-options",
        ]
        assert [e["ordinal"] for e in started] == [1, 3, 5]

    def test_stocks_only_runs_steps_2_4_6_spot_first(self, tmp_path: Path) -> None:
        """Edges 2 + 5: stocks-only runs 2→4→6, and SPOT still settles first."""
        sdk = _FakeSDK(
            expiries={("NSE", "ADANIENT", 2024): ["2024-01-25"]},
            contracts={("NSE", "ADANIENT", "2024-01-25"): [_ADANI_CE, _ADANI_FUT]},
        )
        with capture_logs() as logs:
            report = _run_pipeline(tmp_path, [_adanient()], sdk)
        assert report.ok is True
        started = _events(logs, "pull.step.started")
        settled = _events(logs, "pull.step.settled")
        assert [e["step"] for e in started] == [
            "stocks-spot",
            "stocks-futures",
            "stocks-options",
        ]
        assert [e["ordinal"] for e in started] == [2, 4, 6]
        # Edge 5 (F&O-only trivial-spot): the spot step SETTLES before the
        # first F&O step starts — spot is always first, never interleaved.
        assert logs.index(settled[0]) < logs.index(started[1])

    def test_reference_only_selection_spot_only_post_steps_fire(self, tmp_path: Path) -> None:
        """Edges 3 + 11: an all-reference-only selection pulls SPOT only; the
        F&O steps are skipped entirely and the post-steps still fire (the
        settle-based barrier — a no-F&O pull is settled, never blocked)."""
        vix = _instrument("INDIAVIX", InstrumentType.IDX, 21)
        sdk = _FakeSDK()  # never consulted — the F&O side is reference-only
        with capture_logs() as logs:
            probe = _PostStepProbe(logs)
            report = _run_pipeline(
                tmp_path,
                [vix],
                sdk,
                reference_only_ids=frozenset({vix.security_id}),
                probe=probe,
            )
        assert report.ok is True
        started = _events(logs, "pull.step.started")
        assert [e["step"] for e in started] == ["indices-spot"]
        assert sdk.candle_symbols == []  # zero vendor F&O calls
        assert probe.prebuild_at is not None  # post-steps FIRE
        assert probe.warm_at is not None
        assert report.post_steps_strict_skipped is False

    def test_far_expiry_zero_bar_success_gates_post_steps_open(self, tmp_path: Path) -> None:
        """Edge 7: a far-expiry all-skipped F&O phase is a settled SUCCESS —
        the post-steps fire on the settle predicate, never a ``bars > 0`` gate."""

        def _seed(registry: RegistryStore) -> None:
            registry.upsert_contract(
                "NSE",
                "NIFTY",
                date(2030, 12, 31),  # window opens ~2030-12-01 — not yet open
                "NSE-NIFTY-31Dec30-FUT",
                side="FUT",
                strike=None,
                run_id="t7",
                fetched_at=datetime(2026, 1, 1, tzinfo=UTC),
            )

        sdk = _FakeSDK()  # the metadata phase finds no 2024 chain; candles never fetched
        with capture_logs() as logs:
            probe = _PostStepProbe(logs)
            report = _run_pipeline(
                tmp_path,
                [_nifty()],
                sdk,
                probe=probe,
                fno_dates=False,  # the strict default-window law must apply
                seed_registry=_seed,
            )
        hist = report.historical.historical if report.historical is not None else None
        assert hist is not None
        assert hist.contracts_not_yet_in_window == 1
        assert hist.bars_appended == 0  # zero bars…
        assert sdk.candle_symbols == []  # …zero vendor calls…
        assert report.ok is True  # …and STILL a settled, clean chain
        started = _events(logs, "pull.step.started")
        assert [e["step"] for e in started] == ["indices-spot"]  # no F&O bucket exists
        assert probe.prebuild_at is not None  # post-steps OPEN (settle-based)
        assert probe.warm_at is not None

    def test_dry_run_inert_order_applied_no_ledger(self, tmp_path: Path) -> None:
        """Edge 12: a dry-run applies the order — vendor-free, no durable ledger."""
        with capture_logs() as logs:
            report = pull_all_spot(
                [_adanient(), _nifty()],
                lake_root=tmp_path,
                dry_run=True,
                now=_NOW,
                from_date=_SPOT_FROM,
                to_date=_SPOT_TO,
                step_monitor=StepOrderMonitor(raise_on_violation=True),
            )
        assert report.dry_run is True
        started = _events(logs, "pull.step.started")
        assert [e["step"] for e in started] == ["indices-spot", "stocks-spot"]
        assert not (tmp_path / "window_ledger").exists()  # no durable ledger


# ── edge 10: an empty/unknown selection is a 400 with ZERO step events ────────


class TestUnknownSelectionFailsClosed:
    """The fail-closed selection law is untouched; no step ever starts."""

    def test_unknown_selection_400_zero_step_events(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fastapi_testclient = pytest.importorskip(
            "fastapi.testclient", reason="the route gate needs the [ui] extra"
        )
        # ONE import style for the app module (CodeQL py/import-and-import-from):
        # the module object is what the monkeypatch needs, so every name rides it.
        import brutex.dashboard.app as app_module
        from brutex.config.credentials import ENV_CREDENTIALS_FILE
        from brutex.data.instrument_store import InstrumentStore

        monkeypatch.setenv(ENV_CREDENTIALS_FILE, str(tmp_path / "credentials.json"))
        # Pin the credential gate OPEN (its own suite owns the 503 refusal) so
        # the SELECTION validation is what this test reaches.
        monkeypatch.setattr(app_module, "groww_credentials_error", lambda: None)
        # The store is a context manager — `with` over try/finally-close
        # (CodeQL py/should-use-with; __exit__ closes, idempotent).
        with InstrumentStore.open(tmp_path / "instruments.duckdb") as store:
            store.upsert_many(
                [_nifty()],
                snapshot_date=date(2024, 1, 2),
                run_id="t7",
                csv_source_sha256="0" * 64,
            )
            with (
                fastapi_testclient.TestClient(
                    app_module.build_app(app_module.DashboardState(), instrument_store=store)
                ) as client,
                capture_logs() as logs,
            ):
                resp = client.post("/api/v1/universe/pull-pipeline", params={"symbols": "99999"})
            assert resp.status_code == 400
            assert "99999" in resp.json()["detail"]
            # ZERO step events: the sequencer never launched (fail-CLOSED).
            assert _events(logs, "pull.step.started") == []
            assert _events(logs, "pull.step.settled") == []
