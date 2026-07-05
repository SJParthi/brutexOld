# pyright: reportPrivateUsage=none
"""Tests for ``brutex.dashboard.historical_pull`` (DEC-DERIV-PULL-HISTORICAL-001).

PR-5 of the metadata/historical split (the capstone): the SEPARATE "Pull historical"
engine — download OHLC candles for the operator-SELECTED registry contracts WITHOUT
re-enumerating the chain (the metadata is already cached by PR-3's metadata button).

Every path runs FULLY OFFLINE: a fake ``GrowwSDK`` (a canned candle payload, NO
expiry/contract enumeration even reachable for the selection — the contracts come
straight from the registry), a recording :class:`BarSink`, a REAL temp
:class:`RegistryStore` seeded with contracts, and an injected ``now``. The make-or-break
invariants are asserted directly:

* the resolved contracts list fed to ``run_groww_option_ingest`` is EXACTLY the registry
  selection (NOT a re-enumeration of the chain);
* candles land on PATH A (the candle ``BarSink``) — the recording sink captures the
  ``segment="FNO"`` appends; the module imports neither the OptionBar lake nor a
  fabricated ``iv``/``spot``;
* ``set_candle_status(..., "complete")`` is called per LANDED contract;
* the selection grammar (single / multi / chain / underlying / all / empty→honest), the
  ``until_clean`` default, and the dry-run empty no-op.

100% line+branch on ``brutex.dashboard.historical_pull``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest
from brutex.adapters.groww.client import GrowwClient
from brutex.adapters.groww.constants import (
    GROWW_FUTURE_COVERAGE_MARKET,
    GROWW_OPTION_COVERAGE_MARKET,
    OPTION_SEGMENT,
)
from brutex.adapters.groww.ingest import IngestReport
from brutex.adapters.groww.option_ingest import GrowwOptionIngestReport
from brutex.adapters.protocol import DerivativeContract
from brutex.dashboard import historical_pull as hp
from brutex.dashboard.historical_pull import (
    GROWW_HISTORICAL_FLOOR,
    HistoricalPullReport,
    pull_contract_candles,
)
from brutex.data.registry_store import (
    CANDLE_STATUS_COMPLETE,
    CANDLE_STATUS_NONE,
    ContractRow,
    RegistryStore,
)
from brutex.data.sink import BarSink, SinkAppendResult
from brutex.orchestrator.parallel import SerialRunParallel
from brutex.orchestrator.universe_phases import ItemResult
from structlog.testing import capture_logs

pytest.importorskip("duckdb", reason="RegistryStore needs duckdb")

_NOW = datetime(2026, 6, 14, 9, 15, tzinfo=UTC)
_RUN_ID = "hist-test"
_FETCHED = datetime(2026, 1, 1, tzinfo=UTC)

#: A NARROW pull window — short enough that the 1-day tier (180-day chunks) tiles each
#: contract's range into exactly ONE window, so the per-contract bar/window counts are
#: deterministic (one canned bar per contract per interval). The full default range tiles
#: into ~14 windows, which is correct ingest behaviour but not a clean unit count.
_FROM = date(2024, 1, 1)
_TO = date(2024, 1, 20)


# ── offline fakes ────────────────────────────────────────────────────────────


def _candle_payload() -> dict[str, object]:
    """One 1-day bar (row-of-arrays Groww shape: ts, o, h, l, c, v, oi)."""
    return {"candles": [["2024-01-15T09:15:00", 100.0, 110.0, 90.0, 105.0, 50, 7]]}


class _FakeSDK:
    """A ``GrowwSDK``-shaped fake: a canned candle payload + a raise-for map.

    Carries ``get_expiries`` / ``get_contracts`` (the Protocol surface) but they MUST
    NOT be called — the contracts come from the registry, not a re-enumeration; the test
    asserts ``calls`` never records them. ``raise_for`` maps a contract ``groww_symbol``
    to an exception so a terminal/transient fetch can be exercised offline.
    """

    def __init__(self, *, raise_for: dict[str, Exception] | None = None) -> None:
        self._raise_for = dict(raise_for or {})
        self.calls: list[str] = []
        self.candle_symbols: list[str] = []
        self.candle_intervals: list[str] = []
        self.starts: list[tuple[str, str]] = []

    def get_expiries(self, **_kw: object) -> dict[str, object]:
        """Record + return empty — never expected to be called (no re-enumeration)."""
        self.calls.append("get_expiries")
        return {"expiries": []}

    def get_contracts(self, **_kw: object) -> dict[str, object]:
        """Record + return empty — never expected to be called (no re-enumeration)."""
        self.calls.append("get_contracts")
        return {"contracts": []}

    def get_historical_candles(self, **kw: object) -> dict[str, object]:
        """Return the canned candle bar, or raise the symbol's mapped error."""
        symbol = str(kw["groww_symbol"])
        self.candle_symbols.append(symbol)
        self.candle_intervals.append(str(kw["candle_interval"]))
        self.starts.append((symbol, str(kw["start_time"])))
        exc = self._raise_for.get(symbol)
        if exc is not None:
            raise exc
        return _candle_payload()


class _RecordingSink:
    """A minimal recording :class:`BarSink` capturing every append's segment + symbol."""

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
        """Record ``(segment, symbol, interval, n_bars)`` and report all bars appended."""
        self.appends.append((segment, symbol, interval, len(bars)))
        return SinkAppendResult(bars_appended=len(bars), partitions_written=1, write_results=())


def _client(sdk: _FakeSDK) -> GrowwClient:
    """Wrap *sdk* in a no-sleep, single-attempt :class:`GrowwClient` (offline)."""
    return GrowwClient(sdk=sdk, sleep=lambda _s: None, max_attempts=1)


def _factory(sdk: _FakeSDK, sink: BarSink) -> tuple[GrowwClient, BarSink]:
    """Return a fixed ``(client, sink)`` pair the ``client_factory`` hands back."""
    return _client(sdk), sink


# ── Groww-shaped exception NAMES the error-mapper recognises ──────────────────


class GrowwAPIBadRequestException(Exception):
    """A terminal (non-auth) Groww error — quarantines the pair (leaves status none)."""


class GrowwAPIAuthenticationException(Exception):
    """A terminal auth error — sets ``auth_aborted`` and halts."""


# ── registry seeding ─────────────────────────────────────────────────────────


@pytest.fixture
def registry(tmp_path: Path) -> RegistryStore:
    """A real temp :class:`RegistryStore` seeded with a NIFTY chain + a future."""
    store = RegistryStore.open(tmp_path / "registry.duckdb")
    expiry = date(2024, 1, 25)
    rows = [
        ("NSE-NIFTY-25Jan24-21000-CE", "CE", 21000.0, expiry),
        ("NSE-NIFTY-25Jan24-21000-PE", "PE", 21000.0, expiry),
        ("NSE-NIFTY-25Jan24-FUT", "FUT", None, expiry),
    ]
    for symbol, side, strike, exp in rows:
        store.upsert_contract(
            "NSE",
            "NIFTY",
            exp,
            symbol,
            side=side,
            strike=strike,
            run_id=_RUN_ID,
            fetched_at=_FETCHED,
        )
    # A second underlying + expiry, to prove the underlying / expiry filters.
    store.upsert_contract(
        "NSE",
        "RELIANCE",
        date(2024, 2, 29),
        "NSE-RELIANCE-29Feb24-2800-CE",
        side="CE",
        strike=2800.0,
        run_id=_RUN_ID,
        fetched_at=_FETCHED,
    )
    return store


@pytest.fixture
def tmp_lake(tmp_path: Path) -> Path:
    """A real temp lake root for the LIVE-shaped (``dry_run=False``) ledger paths."""
    root = tmp_path / "lake"
    root.mkdir()
    return root


def _statuses(registry: RegistryStore) -> dict[str, str]:
    """Map each persisted contract symbol → its current ``candle_status``."""
    return {row.contract_symbol: row.candle_status for row in registry.list_contracts()}


# ── happy path: the selection IS what is pulled (no re-enumeration) ───────────


def test_pull_marks_landed_contracts_complete_path_a(registry: RegistryStore) -> None:
    sdk = _FakeSDK()
    sink = _RecordingSink()
    selected = registry.list_contracts(underlying="NIFTY")
    report = pull_contract_candles(
        selected,
        lake_root=Path("/unused"),
        dry_run=True,
        intervals=("1day",),
        registry=registry,
        from_date=_FROM,
        to_date=_TO,
        client_factory=lambda _root: _factory(sdk, sink),
        now=_NOW,
    )
    # We pass a REAL sdk via the injected factory, so this exercises the LIVE-shaped
    # ingest fully offline (the dry_run flag only steers the DEFAULT factory).
    assert isinstance(report, HistoricalPullReport)
    # 3 NIFTY contracts × 1 interval, all landed → all marked complete.
    assert report.contracts_selected == 3
    assert report.contracts_completed == 3
    # The candle fetch was hit EXACTLY for the 3 selected contracts — never the chain
    # enumeration (no get_expiries / get_contracts).
    assert sdk.calls == []
    assert set(sdk.candle_symbols) == {r.contract_symbol for r in selected}
    # PATH A: every append is on the candle lake under segment FNO (NOT options/).
    assert {seg for seg, *_ in sink.appends} == {OPTION_SEGMENT}
    # candle_status flipped to complete for exactly the NIFTY legs (RELIANCE untouched).
    statuses = _statuses(registry)
    assert statuses["NSE-NIFTY-25Jan24-21000-CE"] == CANDLE_STATUS_COMPLETE
    assert statuses["NSE-NIFTY-25Jan24-FUT"] == CANDLE_STATUS_COMPLETE
    assert statuses["NSE-RELIANCE-29Feb24-2800-CE"] == CANDLE_STATUS_NONE


def test_option_and_future_split_distinct_markets(registry: RegistryStore) -> None:
    """CE/PE ride groww-option; FUT rides groww-future — distinct ledger namespaces."""
    sdk = _FakeSDK()
    sink = _RecordingSink()
    selected = registry.list_contracts(underlying="NIFTY")
    report = pull_contract_candles(
        selected,
        lake_root=Path("/unused"),
        dry_run=True,
        intervals=("1day",),
        registry=registry,
        from_date=_FROM,
        to_date=_TO,
        client_factory=lambda _root: _factory(sdk, sink),
        now=_NOW,
    )
    assert report.bars_appended == 3  # one canned bar per contract (one window each)
    assert report.windows == 3


def _empty_ingest_report() -> GrowwOptionIngestReport:
    """A clean (no pairs, no failures) ingest report — inert for an order spy."""
    return GrowwOptionIngestReport(pairs={}, failures=())


def test_futures_market_pulled_before_options(
    registry: RegistryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FUT (groww-future) is ingested BEFORE CE/PE (groww-option) — futures-first order.

    A NIFTY selection spans BOTH market buckets (CE/PE → groww-option, FUT →
    groww-future). A spy records the ``market`` kwarg of every
    ``run_groww_option_ingest`` call in invocation order; the futures-market call MUST
    precede the options-market call regardless of dict insertion order.
    """
    markets_seen: list[str] = []

    def _spy_ingest(*_args: object, market: str, **_kw: object) -> GrowwOptionIngestReport:
        markets_seen.append(market)
        return _empty_ingest_report()

    monkeypatch.setattr(hp, "run_groww_option_ingest", _spy_ingest)
    sdk = _FakeSDK()
    sink = _RecordingSink()
    selected = registry.list_contracts(underlying="NIFTY")
    pull_contract_candles(
        selected,
        lake_root=Path("/unused"),
        dry_run=True,
        intervals=("1day",),
        registry=registry,
        from_date=_FROM,
        to_date=_TO,
        client_factory=lambda _root: _factory(sdk, sink),
        now=_NOW,
    )
    assert markets_seen == [GROWW_FUTURE_COVERAGE_MARKET, GROWW_OPTION_COVERAGE_MARKET]
    assert markets_seen.index(GROWW_FUTURE_COVERAGE_MARKET) < markets_seen.index(
        GROWW_OPTION_COVERAGE_MARKET
    )


def test_live_pull_binds_capture_hook_factory(
    registry: RegistryStore, monkeypatch: pytest.MonkeyPatch, tmp_lake: Path
) -> None:
    """The LIVE (dry_run=False) F&O pull threads a NON-None capture_hook_factory.

    DEC-RAW-DATA-INTEGRITY-001 FIX 1 (option path): a spy captures the
    ``capture_hook_factory`` kwarg ``run_groww_option_ingest`` receives — on the live
    path it MUST be a real factory (so every F&O window records a raw-payload hash).
    """
    factories: list[object] = []

    def _spy_ingest(
        *_args: object, capture_hook_factory: object = None, **_kw: object
    ) -> GrowwOptionIngestReport:
        factories.append(capture_hook_factory)
        return _empty_ingest_report()

    monkeypatch.setattr(hp, "run_groww_option_ingest", _spy_ingest)
    sdk = _FakeSDK()
    sink = _RecordingSink()
    selected = registry.list_contracts(underlying="NIFTY")
    pull_contract_candles(
        selected,
        lake_root=tmp_lake,
        dry_run=False,
        intervals=("1day",),
        registry=registry,
        from_date=_FROM,
        to_date=_TO,
        client_factory=lambda _root: _factory(sdk, sink),
        now=_NOW,
    )
    assert factories  # at least one ingest call
    assert all(f is not None for f in factories)  # every call carried a live factory


def test_dry_run_pull_has_no_capture_hook_factory(
    registry: RegistryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dry-run pull threads capture_hook_factory=None (no real vendor bytes to attest)."""
    factories: list[object] = []

    def _spy_ingest(
        *_args: object, capture_hook_factory: object = None, **_kw: object
    ) -> GrowwOptionIngestReport:
        factories.append(capture_hook_factory)
        return _empty_ingest_report()

    monkeypatch.setattr(hp, "run_groww_option_ingest", _spy_ingest)
    sdk = _FakeSDK()
    sink = _RecordingSink()
    selected = registry.list_contracts(underlying="NIFTY")
    pull_contract_candles(
        selected,
        lake_root=Path("/unused"),
        dry_run=True,
        intervals=("1day",),
        registry=registry,
        from_date=_FROM,
        to_date=_TO,
        client_factory=lambda _root: _factory(sdk, sink),
        now=_NOW,
    )
    assert factories
    assert all(f is None for f in factories)


def test_fno_pull_threads_write_pool_when_flag_on(
    registry: RegistryStore, monkeypatch: pytest.MonkeyPatch, tmp_lake: Path
) -> None:
    """The F&O (futures+options) pull threads the batched write_pool when BRUTEX_WRITE_BATCH=1.

    DEC-WRITE-BATCH-COALESCE-001 (MAJOR-2 wiring): the operator requires the batched-write
    speedup for spot AND futures AND options. A LIVE (dry_run=False) F&O pull with the flag
    ON MUST hand every ``run_groww_option_ingest`` call a live
    :class:`~brutex.data.write_consumer.BatchingWriteConsumer` as ``write_pool`` (the SAME
    background writer the spot spine uses) — so the F&O lake write is also coalesced off the
    fetch lane. A spy captures the ``write_pool`` kwarg each ingest call receives.
    """
    from brutex.data.write_consumer import BatchingWriteConsumer

    monkeypatch.setenv("BRUTEX_WRITE_BATCH", "1")
    # The fetch_pipeline SUBSUMES the Lever-1 write_pool (DEC-FNO-ASYNC-PIPELINE-WIRE-001), so the
    # batched write_pool is built ONLY when the pipeline is OFF — exercise the write_pool fallback
    # by disabling the (default-ON) pipeline via its kill-switch.
    monkeypatch.setenv("BRUTEX_GROWW_FETCH_PIPELINE", "off")
    pools_seen: list[object] = []

    def _spy_ingest(
        *_args: object, write_pool: object = None, **_kw: object
    ) -> GrowwOptionIngestReport:
        pools_seen.append(write_pool)
        return _empty_ingest_report()

    monkeypatch.setattr(hp, "run_groww_option_ingest", _spy_ingest)
    sdk = _FakeSDK()
    sink = _RecordingSink()
    selected = registry.list_contracts(underlying="NIFTY")
    pull_contract_candles(
        selected,
        lake_root=tmp_lake,
        dry_run=False,
        intervals=("1day",),
        registry=registry,
        from_date=_FROM,
        to_date=_TO,
        client_factory=lambda _root: _factory(sdk, sink),
        now=_NOW,
    )
    assert pools_seen  # at least one ingest call (futures + options buckets)
    # EVERY F&O ingest call received the SAME live batched consumer (one per pull, drained
    # in the finally so no thread leaks).
    assert all(isinstance(p, BatchingWriteConsumer) for p in pools_seen)
    assert len({id(p) for p in pools_seen}) == 1


def test_fno_pull_write_pool_none_when_flag_off(
    registry: RegistryStore, monkeypatch: pytest.MonkeyPatch, tmp_lake: Path
) -> None:
    """With the flag OFF (default) the F&O pull keeps the byte-identical synchronous path.

    Every ``run_groww_option_ingest`` call receives ``write_pool=None`` (no background
    writer), so the F&O write path is unchanged unless the operator opts in.
    """
    monkeypatch.delenv("BRUTEX_WRITE_BATCH", raising=False)
    # Disable the (default-ON) fetch_pipeline too, isolating the write_pool=None decision to the
    # batch flag being off (the pipeline would also force write_pool=None, but this proves the
    # batch-flag-off path itself; DEC-FNO-ASYNC-PIPELINE-WIRE-001).
    monkeypatch.setenv("BRUTEX_GROWW_FETCH_PIPELINE", "off")
    pools_seen: list[object] = []

    def _spy_ingest(
        *_args: object, write_pool: object = None, **_kw: object
    ) -> GrowwOptionIngestReport:
        pools_seen.append(write_pool)
        return _empty_ingest_report()

    monkeypatch.setattr(hp, "run_groww_option_ingest", _spy_ingest)
    sdk = _FakeSDK()
    sink = _RecordingSink()
    selected = registry.list_contracts(underlying="NIFTY")
    pull_contract_candles(
        selected,
        lake_root=tmp_lake,
        dry_run=False,
        intervals=("1day",),
        registry=registry,
        from_date=_FROM,
        to_date=_TO,
        client_factory=lambda _root: _factory(sdk, sink),
        now=_NOW,
    )
    assert pools_seen
    assert all(p is None for p in pools_seen)


def test_fno_pull_constructs_passes_and_drains_fetch_pipeline(
    registry: RegistryStore, monkeypatch: pytest.MonkeyPatch, tmp_lake: Path
) -> None:
    """The F&O pull CONSTRUCTS + PASSES + DRAINS the async fetch_pipeline (DEFAULT ON).

    DEC-FNO-ASYNC-PIPELINE-WIRE-001 — the bulk of the 67k-window backfill runs through the F&O
    historical pull, which previously NEVER built a fetch_pipeline (fully synchronous tail on the
    fetch worker, ~75% pool idle). This wires the SAME async fetch/process separation the spot
    spine uses (DEC-GROWW-FETCH-ASYNC-SEPARATION-001). A LIVE (dry_run=False) pull with the
    pipeline ON (the default) MUST:
      1. build ONE :class:`~brutex.adapters.groww.fetch_pipeline.FetchPipeline` via the COMMON
         ``brutex.adapters.groww.pull_pipeline.build_fetch_pipeline`` helper,
      2. forward that SAME pipeline into EVERY ``run_groww_option_ingest`` call,
      3. DRAIN it once at pull end (before the ledger persist + 1-day derivation).
    Spies on the COMMON helpers + the ingest call capture the construct / pass / drain.
    """
    from brutex.adapters.groww.fetch_pipeline import FetchPipeline
    from brutex.adapters.groww.pull_pipeline import (
        build_fetch_pipeline as _real_build_fetch_pipeline,
    )
    from brutex.adapters.groww.pull_pipeline import (
        drain_fetch_pipeline as _real_drain_fetch_pipeline,
    )

    monkeypatch.delenv("BRUTEX_GROWW_FETCH_PIPELINE", raising=False)  # default ON
    built: list[FetchPipeline] = []
    drained: list[object] = []
    pipelines_seen: list[object] = []

    def _spy_build(*, dry_run: bool, lake_root: Path) -> FetchPipeline | None:
        pipeline = _real_build_fetch_pipeline(dry_run=dry_run, lake_root=lake_root)
        if pipeline is not None:
            built.append(pipeline)
        return pipeline

    def _spy_drain(pipeline: FetchPipeline | None) -> None:
        drained.append(pipeline)
        _real_drain_fetch_pipeline(pipeline)

    def _spy_ingest(
        *_args: object, fetch_pipeline: object = None, **_kw: object
    ) -> GrowwOptionIngestReport:
        pipelines_seen.append(fetch_pipeline)
        return _empty_ingest_report()

    monkeypatch.setattr("brutex.adapters.groww.pull_pipeline.build_fetch_pipeline", _spy_build)
    monkeypatch.setattr("brutex.adapters.groww.pull_pipeline.drain_fetch_pipeline", _spy_drain)
    monkeypatch.setattr(hp, "run_groww_option_ingest", _spy_ingest)
    sdk = _FakeSDK()
    sink = _RecordingSink()
    selected = registry.list_contracts(underlying="NIFTY")
    pull_contract_candles(
        selected,
        lake_root=tmp_lake,
        dry_run=False,
        intervals=("1day",),
        registry=registry,
        from_date=_FROM,
        to_date=_TO,
        client_factory=lambda _root: _factory(sdk, sink),
        now=_NOW,
    )
    # ONE pipeline built (default ON), forwarded to every ingest call, and DRAINED before return.
    assert len(built) == 1
    the_pipeline = built[0]
    assert pipelines_seen  # at least one ingest call (futures + options buckets)
    assert all(p is the_pipeline for p in pipelines_seen)
    # Drained (idempotently) — once inside ``_run_phase`` BEFORE the per-phase ledger persists
    # (DEC-FNO-ASYNC-PIPELINE-WIRE-001: every consumer tail must ledger.record before the ledger
    # is written to disk) AND once again in the caller's ``finally`` as a safety net. The real
    # ``drain()`` is idempotent (a second call with no live threads is a no-op), so >= 1 call,
    # all targeting the SAME pipeline.
    assert drained, "the pipeline must be drained before pull_contract_candles returns"
    assert all(p is the_pipeline for p in drained)


class _EmptyCandleSDK:
    """A ``GrowwSDK``-shaped fake whose every candle fetch returns a CLEAN EMPTY 200.

    The silent-vanish failure mode (BAJAJ-AUTO / M&M): every contract fetches cleanly
    but yields ZERO bars, so each pair lands SUCCESS-but-empty and the whole underlying
    stores nothing — indistinguishable from a legit no-data gap without the detector.
    """

    def __init__(self) -> None:
        self.candle_symbols: list[str] = []

    def get_historical_candles(self, **kw: object) -> dict[str, object]:
        """Record the symbol + return a clean empty candle list (no bars)."""
        self.candle_symbols.append(str(kw["groww_symbol"]))
        return {"candles": []}


def test_silent_vanish_underlying_warns_in_production_pipeline_pull(
    registry: RegistryStore, monkeypatch: pytest.MonkeyPatch, tmp_lake: Path
) -> None:
    """The PRODUCTION (pipeline-wired) F&O pull EMITS the all-empty WARNING on a silent vanish.

    DEC-EMPTY-DETECTOR-PRODUCTION-WIRING-001 — the regression that proves the
    whole-underlying silent-empty detector is no longer DEAD in a real pull. Before the
    fix the only ``scan_underlying_all_empty`` call site (inside ``run_groww_option_ingest``)
    was gated ``if fetch_pipeline is None:`` and the production pull ALWAYS wires a
    non-None pipeline, so the detector never ran. This LIVE (``dry_run=False``,
    pipeline default-ON) pull over a NIFTY chain whose every contract fetches CLEANLY
    EMPTY must now surface exactly ONE ``pull.underlying_all_empty`` WARNING naming
    NIFTY, fired POST-DRAIN over the reconciled per-pair counts.
    """
    monkeypatch.delenv("BRUTEX_GROWW_FETCH_PIPELINE", raising=False)  # default ON
    sdk = _EmptyCandleSDK()
    sink = _RecordingSink()
    selected = registry.list_contracts(underlying="NIFTY")  # 3 contracts (CE/PE/FUT)
    with capture_logs() as logs:
        report = pull_contract_candles(
            selected,
            lake_root=tmp_lake,
            dry_run=False,
            intervals=("1day",),
            registry=registry,
            from_date=_FROM,
            to_date=_TO,
            client_factory=lambda _root: _factory(cast("_FakeSDK", sdk), sink),
            now=_NOW,
        )
    # The whole chain fetched cleanly but stored ZERO bars.
    assert report.bars_appended == 0
    # All 3 contracts attempted (each re-fetched a few times by the in-pull empty retry).
    assert set(sdk.candle_symbols) == {r.contract_symbol for r in selected}
    # EXACTLY one observe-only WARNING, naming NIFTY — the detector is ALIVE in production.
    warns = [r for r in logs if r["event"] == "pull.underlying_all_empty"]
    assert len(warns) == 1
    assert warns[0]["underlying"] == repr("NIFTY")
    assert warns[0]["attempted_pairs"] == 3
    assert warns[0]["stored_bars"] == 0
    # No scan-error event (the guarded scan ran cleanly).
    assert [r for r in logs if r["event"] == "pull.underlying_all_empty_error"] == []
    # Tier A (3 clean-empty pairs) — not the low-leg tier (DEC-SILENT-VANISH-LOWLEG-001).
    assert warns[0]["low_leg"] is False


def test_silent_vanish_low_leg_underlying_warns_in_production_pipeline_pull(
    registry: RegistryStore, monkeypatch: pytest.MonkeyPatch, tmp_lake: Path
) -> None:
    """The PRODUCTION post-drain scan ALSO flags a 1-2-leg all-clean-empty underlying.

    DEC-SILENT-VANISH-LOWLEG-001 — the low-leg twin of the Tier A production regression
    above: selecting only 2 of the NIFTY contracts makes a 2-leg chain whose every leg
    fetches CLEANLY EMPTY, which the pre-fix ``attempted >= 3`` rule could NEVER flag.
    The post-drain scan (fed ``minute.failure_symbols`` — empty here) must emit exactly
    ONE ``pull.underlying_all_empty`` WARNING marked ``low_leg=True``.
    """
    monkeypatch.delenv("BRUTEX_GROWW_FETCH_PIPELINE", raising=False)  # default ON
    sdk = _EmptyCandleSDK()
    sink = _RecordingSink()
    selected = registry.list_contracts(underlying="NIFTY")[:2]  # a 2-leg chain
    with capture_logs() as logs:
        report = pull_contract_candles(
            selected,
            lake_root=tmp_lake,
            dry_run=False,
            intervals=("1day",),
            registry=registry,
            from_date=_FROM,
            to_date=_TO,
            client_factory=lambda _root: _factory(cast("_FakeSDK", sdk), sink),
            now=_NOW,
        )
    assert report.bars_appended == 0
    assert set(sdk.candle_symbols) == {r.contract_symbol for r in selected}
    warns = [r for r in logs if r["event"] == "pull.underlying_all_empty"]
    assert len(warns) == 1
    assert warns[0]["underlying"] == repr("NIFTY")
    assert warns[0]["attempted_pairs"] == 2
    assert warns[0]["stored_bars"] == 0
    assert warns[0]["low_leg"] is True
    assert [r for r in logs if r["event"] == "pull.underlying_all_empty_error"] == []


class _EmptyOrRaiseSDK(_EmptyCandleSDK):
    """An ``_EmptyCandleSDK`` whose mapped symbols raise a TERMINAL error instead.

    Exercises the quarantined-sibling shape at the PRODUCTION post-drain seam: one leg
    fetches CLEANLY EMPTY, the sibling terminally quarantines (never enters ``pairs``).
    """

    def __init__(self, *, raise_for: dict[str, Exception]) -> None:
        super().__init__()
        self._raise_for = dict(raise_for)

    def get_historical_candles(self, **kw: object) -> dict[str, object]:
        """Raise the symbol's mapped terminal error, else the clean empty payload."""
        exc = self._raise_for.get(str(kw["groww_symbol"]))
        if exc is not None:
            self.candle_symbols.append(str(kw["groww_symbol"]))
            raise exc
        return super().get_historical_candles(**kw)


def test_pipeline_pull_quarantined_sibling_suppresses_low_leg_flag(
    registry: RegistryStore, monkeypatch: pytest.MonkeyPatch, tmp_lake: Path
) -> None:
    """The PRODUCTION post-drain scan threads the REAL quarantine set — wrong-set regression pin.

    DEC-SILENT-VANISH-LOWLEG-001: the one production line
    (``failed_symbols=minute.failure_symbols`` in ``pull_contract_candles``) must carry the
    phase's terminally-quarantined contract symbols into the Tier B rule. A 2-leg NIFTY
    chain with 1 clean-empty leg + 1 terminally-quarantined leg must emit NO
    ``pull.underlying_all_empty`` WARNING — without the threading (or with a wrong set)
    the chain is indistinguishable from a genuine 1-leg all-clean-empty chain and would
    falsely flag ``low_leg=True`` (the sync-driver twin is
    ``test_run_ingest_quarantined_sibling_suppresses_low_leg_flag``).
    """
    monkeypatch.delenv("BRUTEX_GROWW_FETCH_PIPELINE", raising=False)  # default ON
    selected = registry.list_contracts(underlying="NIFTY")[:2]  # a 2-leg chain
    bad = selected[1].contract_symbol
    sdk = _EmptyOrRaiseSDK(raise_for={bad: GrowwAPIBadRequestException("nope")})
    sink = _RecordingSink()
    with capture_logs() as logs:
        report = pull_contract_candles(
            selected,
            lake_root=tmp_lake,
            dry_run=False,
            intervals=("1day",),
            registry=registry,
            from_date=_FROM,
            to_date=_TO,
            client_factory=lambda _root: _factory(cast("_FakeSDK", sdk), sink),
            now=_NOW,
        )
    assert report.bars_appended == 0
    assert set(sdk.candle_symbols) == {r.contract_symbol for r in selected}  # both attempted
    # The quarantined sibling disqualifies the low-leg tier → NO all-empty warning.
    assert [r for r in logs if r["event"] == "pull.underlying_all_empty"] == []
    assert [r for r in logs if r["event"] == "pull.underlying_all_empty_error"] == []


def test_fno_pull_write_pool_none_when_fetch_pipeline_on(
    registry: RegistryStore, monkeypatch: pytest.MonkeyPatch, tmp_lake: Path
) -> None:
    """The fetch_pipeline SUBSUMES write_pool — never BOTH (no double-write / lost-write).

    DEC-FNO-ASYNC-PIPELINE-WIRE-001: the pipeline owns the ENTIRE tail (incl. ``sink.append``),
    so the Lever-1 batched ``write_pool`` is built ONLY when the pipeline is OFF. Even with
    ``BRUTEX_WRITE_BATCH=1`` set, a default-ON pipeline forces ``write_pool=None`` at EVERY
    ingest call (the pipeline's consumer is the single writer) — proving the mutual exclusion
    that guards against a double background write hop.
    """
    monkeypatch.delenv("BRUTEX_GROWW_FETCH_PIPELINE", raising=False)  # default ON
    monkeypatch.setenv(
        "BRUTEX_WRITE_BATCH", "1"
    )  # would build a write_pool if the pipeline were off
    seen: list[tuple[object, object]] = []

    def _spy_ingest(
        *_args: object,
        write_pool: object = None,
        fetch_pipeline: object = None,
        **_kw: object,
    ) -> GrowwOptionIngestReport:
        seen.append((write_pool, fetch_pipeline))
        return _empty_ingest_report()

    monkeypatch.setattr(hp, "run_groww_option_ingest", _spy_ingest)
    sdk = _FakeSDK()
    sink = _RecordingSink()
    selected = registry.list_contracts(underlying="NIFTY")
    pull_contract_candles(
        selected,
        lake_root=tmp_lake,
        dry_run=False,
        intervals=("1day",),
        registry=registry,
        from_date=_FROM,
        to_date=_TO,
        client_factory=lambda _root: _factory(sdk, sink),
        now=_NOW,
    )
    assert seen
    # The pipeline is built (not None) and write_pool is ALWAYS None — never both.
    assert all(write_pool is None for write_pool, _fp in seen)
    assert all(fp is not None for _wp, fp in seen)


def test_reconcile_phase_with_pipeline_folds_deferred_totals() -> None:
    """The post-drain reconcile folds the consumer's per-series totals back into the phase.

    DEC-FNO-ASYNC-PIPELINE-WIRE-001 completion-count fix: in async mode the producer returns a
    DEFERRED per-pair report with ``bars_appended == 0`` and a tail-transient is folded onto the
    pipeline, so the phase result reads 0 bars and would falsely mark a tail-transient-deferred
    contract complete. The reconcile OVERRIDES bars/still-failed from the consumer totals and
    RE-DERIVES the clean set (excluding a contract with a tail-transient residual).
    """
    from brutex.adapters.groww.fetch_pipeline import FetchPipeline

    pipeline = FetchPipeline(workers=1)
    try:
        # SYM-A: durable bars folded → clean. SYM-B: a tail transient → still failing → NOT clean.
        pipeline.fold_totals(bars_appended=30, conflicts=0, series_key=("SYM-A", "1minute"))
        pipeline.fold_tail_transient(("SYM-B", "1minute"))
        phase = hp._PhaseResult(
            bars=0,  # deferred — the producer reported 0
            windows=4,
            failed_pairs=0,
            still_failed_windows=0,
            auth_aborted=False,
            complete=frozenset({"SYM-A", "SYM-B"}),  # producer falsely thought both clean
            clean=True,
            merged_pairs={
                ("SYM-A", "1minute"): IngestReport(
                    windows=2, bars_appended=0, transient_failures=0
                ),
                ("SYM-B", "1minute"): IngestReport(
                    windows=2, bars_appended=0, transient_failures=0
                ),
            },
            failure_symbols=frozenset(),
        )
        out = hp._reconcile_phase_with_pipeline(
            phase, pipeline, selected_symbols=frozenset({"SYM-A", "SYM-B"})
        )
        assert out.bars == 30  # folded from the consumer totals
        assert out.still_failed_windows == 1  # SYM-B tail transient
        assert out.complete == frozenset({"SYM-A"})  # SYM-B excluded (still failing)
        assert out.clean is False  # the barrier breaks: not every selected contract is clean
    finally:
        pipeline.drain()


def test_reconcile_phase_with_pipeline_synchronous_is_noop() -> None:
    """A ``None`` pipeline (synchronous path) returns the phase byte-identically (same object)."""
    phase = hp._PhaseResult(
        bars=12,
        windows=3,
        failed_pairs=0,
        still_failed_windows=0,
        auth_aborted=False,
        complete=frozenset({"SYM-A"}),
        clean=True,
        merged_pairs={
            ("SYM-A", "1minute"): IngestReport(windows=3, bars_appended=12, transient_failures=0)
        },
        failure_symbols=frozenset(),
    )
    assert hp._reconcile_phase_with_pipeline(phase, None, selected_symbols=frozenset()) is phase


def test_reconcile_phase_with_pipeline_empty_totals_is_noop() -> None:
    """A pipeline that folded NOTHING (no consumer ran) returns the phase unchanged."""
    from brutex.adapters.groww.fetch_pipeline import FetchPipeline

    pipeline = FetchPipeline(workers=1)
    try:
        phase = hp._PhaseResult(
            bars=0,
            windows=1,
            failed_pairs=0,
            still_failed_windows=0,
            auth_aborted=False,
            complete=frozenset(),
            clean=True,
            merged_pairs={},
            failure_symbols=frozenset(),
        )
        assert (
            hp._reconcile_phase_with_pipeline(phase, pipeline, selected_symbols=frozenset())
            is phase
        )
    finally:
        pipeline.drain()


def test_reconcile_phase_with_pipeline_unmatched_series_kept() -> None:
    """A merged pair with NO folded total (every window a producer skip) keeps its report."""
    from brutex.adapters.groww.fetch_pipeline import FetchPipeline

    pipeline = FetchPipeline(workers=1)
    try:
        pipeline.fold_totals(bars_appended=5, conflicts=0, series_key=("SYM-A", "1minute"))
        kept = IngestReport(windows=2, bars_appended=0, transient_failures=0, skipped_windows=2)
        phase = hp._PhaseResult(
            bars=0,
            windows=4,
            failed_pairs=0,
            still_failed_windows=0,
            auth_aborted=False,
            complete=frozenset({"SYM-A", "SYM-B"}),
            clean=True,
            merged_pairs={
                ("SYM-A", "1minute"): IngestReport(
                    windows=2, bars_appended=0, transient_failures=0
                ),
                ("SYM-B", "1minute"): kept,  # no folded total → kept as-is
            },
            failure_symbols=frozenset(),
        )
        out = hp._reconcile_phase_with_pipeline(
            phase, pipeline, selected_symbols=frozenset({"SYM-A", "SYM-B"})
        )
        assert out.bars == 5  # only SYM-A folded; SYM-B's kept report has 0 bars
        assert out.merged_pairs[("SYM-B", "1minute")] is kept
        assert out.complete == frozenset({"SYM-A", "SYM-B"})  # neither still-failing
        assert out.clean is True
    finally:
        pipeline.drain()


def test_reconcile_round1_transient_then_round2_success_is_complete() -> None:
    """MAJOR-1: a contract that transient'd in round 1 but DURABLY SUCCEEDS in round 2 is COMPLETE.

    DEC-FNO-ASYNC-PIPELINE-WIRE-001 MAJOR-1 regression (direction 1). The cumulative-transient bug
    left the series ``transient == 1`` forever, so the reconcile EXCLUDED it from ``complete`` and
    reported ``still_failed_windows=1, clean=False`` even though its 50 bars were durably in the
    lake — a MISLEADING verdict + a needless re-pull. With the LAST-WRITE-WINS fix the round-2
    success clears the flag → ``complete``, ``still_failed_windows=0``, ``clean=True``.
    """
    from brutex.adapters.groww.fetch_pipeline import FetchPipeline

    key = ("ABC25JAN20000CE", "1minute")
    pipeline = FetchPipeline(workers=1)
    try:
        # Round 1: a PartitionLockTimeout deferred this contract's window (0 bars).
        pipeline.fold_tail_transient(key)
        # Round 2: the SAME window is re-pulled and durably succeeds (50 bars).
        pipeline.fold_totals(bars_appended=50, conflicts=0, series_key=key)
        phase = hp._PhaseResult(
            bars=0,  # deferred — the producer reported 0 each round
            windows=2,
            failed_pairs=0,
            still_failed_windows=0,
            auth_aborted=False,
            complete=frozenset({"ABC25JAN20000CE"}),
            clean=True,
            merged_pairs={key: IngestReport(windows=1, bars_appended=0, transient_failures=0)},
            failure_symbols=frozenset(),
        )
        out = hp._reconcile_phase_with_pipeline(
            phase, pipeline, selected_symbols=frozenset({"ABC25JAN20000CE"})
        )
        assert out.bars == 50  # durable bars folded
        assert out.still_failed_windows == 0  # NOT a stale cumulative transient
        assert out.complete == frozenset({"ABC25JAN20000CE"})  # resolved → complete
        assert out.clean is True
    finally:
        pipeline.drain()


def test_reconcile_round1_success_then_round2_transient_stays_failing() -> None:
    """MAJOR-1 (direction 2): a success FOLLOWED by a transient is NOT falsely marked complete.

    DEC-FNO-ASYNC-PIPELINE-WIRE-001 MAJOR-1 regression — the case the NAIVE ``still = transient if
    bars==0 else 0`` shortcut would BREAK (the accumulated round-1 bars would zero out the round-2
    transient → false complete). LAST-WRITE-WINS keeps the newest (transient) outcome, so the
    series is correctly ``still_failing`` / excluded from ``complete`` even with durable bars.
    """
    from brutex.adapters.groww.fetch_pipeline import FetchPipeline

    key = ("ABC25JAN20000CE", "1minute")
    pipeline = FetchPipeline(workers=1)
    try:
        # Round 1: a durable success (50 bars).
        pipeline.fold_totals(bars_appended=50, conflicts=0, series_key=key)
        # Round 2: the newest outcome is a tail transient (a later window deferred).
        pipeline.fold_tail_transient(key)
        phase = hp._PhaseResult(
            bars=0,
            windows=2,
            failed_pairs=0,
            still_failed_windows=0,
            auth_aborted=False,
            complete=frozenset({"ABC25JAN20000CE"}),
            clean=True,
            merged_pairs={key: IngestReport(windows=1, bars_appended=0, transient_failures=0)},
            failure_symbols=frozenset(),
        )
        out = hp._reconcile_phase_with_pipeline(
            phase, pipeline, selected_symbols=frozenset({"ABC25JAN20000CE"})
        )
        assert out.bars == 50  # the round-1 bars still accumulate
        assert out.still_failed_windows == 1  # newest outcome is a transient
        assert out.complete == frozenset()  # NOT falsely complete
        assert out.clean is False  # the barrier breaks
    finally:
        pipeline.drain()


def test_market_pull_order_sorts_unknown_market_last() -> None:
    """The order key sorts the two known markets futures-first, an unknown market LAST.

    Covers the ``.get(..., 99)`` deterministic tie-break: an unrecognised market namespace
    sorts after both known ones, and equal-rank markets fall back to a stable name sort.
    """
    buckets = {
        GROWW_OPTION_COVERAGE_MARKET: 1,
        "zzz-unknown-market": 2,
        GROWW_FUTURE_COVERAGE_MARKET: 0,
    }
    ordered = [
        market
        for market, _rank in sorted(
            buckets.items(),
            key=lambda kv: (hp._MARKET_PULL_ORDER.get(kv[0], 99), kv[0]),
        )
    ]
    assert ordered == [
        GROWW_FUTURE_COVERAGE_MARKET,
        GROWW_OPTION_COVERAGE_MARKET,
        "zzz-unknown-market",
    ]


# ── empty selection: honest 0-bar no-op ──────────────────────────────────────


def test_empty_selection_is_honest_no_op(registry: RegistryStore) -> None:
    sdk = _FakeSDK()
    sink = _RecordingSink()
    report = pull_contract_candles(
        [],
        lake_root=Path("/unused"),
        dry_run=True,
        intervals=("1day",),
        registry=registry,
        client_factory=lambda _root: _factory(sdk, sink),
        now=_NOW,
    )
    assert report.contracts_selected == 0
    assert report.contracts_completed == 0
    assert report.bars_appended == 0
    assert sink.appends == []
    assert sdk.candle_symbols == []
    assert "no contracts selected" in report.note


# ── dry-run via the default factory: empty candles, but still settles complete ──


def test_dry_run_default_factory_empty_candles(registry: RegistryStore) -> None:
    """No injected factory + dry_run=True → the empty-candle client (0 bars), honest note."""
    selected = registry.list_contracts(underlying="NIFTY")
    report = pull_contract_candles(
        selected,
        lake_root=Path("/unused"),
        dry_run=True,
        intervals=("1day",),
        registry=registry,
        now=_NOW,
    )
    assert report.dry_run is True
    assert report.bars_appended == 0
    # An EMPTY window is still a SETTLED clean window, so the selected contracts ARE
    # marked complete (coverage of the selected range — honestly empty offline).
    assert report.contracts_completed == 3
    assert "dry-run" in report.note


# ── terminal quarantine: a bad-request leaves the contract status none ────────


def test_terminal_quarantine_leaves_status_none(registry: RegistryStore, tmp_lake: Path) -> None:
    sdk = _FakeSDK(raise_for={"NSE-NIFTY-25Jan24-21000-CE": GrowwAPIBadRequestException("bad")})
    sink = _RecordingSink()
    selected = registry.list_contracts(underlying="NIFTY")
    # dry_run=False (the LIVE-shaped path) so a real failing SDK exercises the quarantine
    # note branch; the injected factory bypasses live client/network construction.
    report = pull_contract_candles(
        selected,
        lake_root=tmp_lake,
        dry_run=False,
        intervals=("1day",),
        registry=registry,
        from_date=_FROM,
        to_date=_TO,
        client_factory=lambda _root: _factory(sdk, sink),
        now=_NOW,
    )
    assert report.failed_pairs == 1
    statuses = _statuses(registry)
    # The quarantined CE stays none; the clean PE + FUT are marked complete.
    assert statuses["NSE-NIFTY-25Jan24-21000-CE"] == CANDLE_STATUS_NONE
    assert statuses["NSE-NIFTY-25Jan24-21000-PE"] == CANDLE_STATUS_COMPLETE
    assert report.contracts_completed == 2
    assert "quarantined" in report.note


def test_auth_abort_halts_and_reports(registry: RegistryStore, tmp_lake: Path) -> None:
    sdk = _FakeSDK(
        raise_for={"NSE-NIFTY-25Jan24-21000-CE": GrowwAPIAuthenticationException("dead")}
    )
    sink = _RecordingSink()
    selected = registry.list_contracts(underlying="NIFTY")
    report = pull_contract_candles(
        selected,
        lake_root=tmp_lake,
        dry_run=False,
        intervals=("1day",),
        registry=registry,
        from_date=_FROM,
        to_date=_TO,
        client_factory=lambda _root: _factory(sdk, sink),
        now=_NOW,
    )
    assert report.auth_aborted is True
    assert "auth token expired" in report.note


def test_retryable_auth_shape_escaping_spine_propagates_unchanged(
    registry: RegistryStore, tmp_lake: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DEC-GROWW-AUTH-HALT-001 carve-out (F&O twin): a RETRYABLE auth-family error
    (a stale read-only re-read) escaping the option spine re-raises UNCHANGED out
    of ``pull_contract_candles`` — never mapped to the graceful terminal-auth abort."""
    from brutex.adapters.groww.errors import GrowwTokenStillStaleError

    def _raise_still_stale(*_a: object, **_k: object) -> GrowwOptionIngestReport:
        raise GrowwTokenStillStaleError("same dead token", safe_message="stale")

    monkeypatch.setattr(hp, "run_groww_option_ingest", _raise_still_stale)
    sdk = _FakeSDK()
    sink = _RecordingSink()
    selected = registry.list_contracts(underlying="NIFTY")
    with pytest.raises(GrowwTokenStillStaleError):
        pull_contract_candles(
            selected,
            lake_root=tmp_lake,
            dry_run=False,
            intervals=("1day",),
            registry=registry,
            from_date=_FROM,
            to_date=_TO,
            client_factory=lambda _root: _factory(sdk, sink),
            now=_NOW,
        )


# ── until-clean: bounded re-pull rounds (default-off vs on) ───────────────────


def test_until_clean_default_is_single_pass(registry: RegistryStore) -> None:
    """``until_clean`` defaults to False — exactly one pass per market bucket."""
    sdk = _FakeSDK()
    sink = _RecordingSink()
    selected = registry.list_contracts(underlying="NIFTY")
    pull_contract_candles(
        selected,
        lake_root=Path("/unused"),
        dry_run=True,
        intervals=("1day",),
        registry=registry,
        from_date=_FROM,
        to_date=_TO,
        client_factory=lambda _root: _factory(sdk, sink),
        now=_NOW,
    )
    # One candle call per contract (single pass, one window each — no re-pull rounds).
    assert len(sdk.candle_symbols) == 3
    assert set(sdk.candle_symbols) == {r.contract_symbol for r in selected}


def test_until_clean_runs_to_clean(registry: RegistryStore) -> None:
    """``until_clean=True`` drains a clean selection in one productive round."""
    sdk = _FakeSDK()
    sink = _RecordingSink()
    selected = registry.list_contracts(underlying="NIFTY")
    report = pull_contract_candles(
        selected,
        lake_root=Path("/unused"),
        dry_run=True,
        intervals=("1day",),
        registry=registry,
        client_factory=lambda _root: _factory(sdk, sink),
        until_clean=True,
        now=_NOW,
    )
    assert report.still_failed_windows == 0
    assert report.contracts_completed == 3


def test_until_clean_zero_rounds_is_safe_no_op(registry: RegistryStore) -> None:
    """``until_clean_rounds=0`` runs the loop zero times → an honest all-zero report."""
    sdk = _FakeSDK()
    sink = _RecordingSink()
    selected = registry.list_contracts(underlying="NIFTY")
    report = pull_contract_candles(
        selected,
        lake_root=Path("/unused"),
        dry_run=True,
        intervals=("1day",),
        registry=registry,
        client_factory=lambda _root: _factory(sdk, sink),
        until_clean=True,
        until_clean_rounds=0,
        now=_NOW,
    )
    assert report.bars_appended == 0
    assert report.windows == 0
    assert report.contracts_completed == 0
    assert sdk.candle_symbols == []


# ── progress hook + range resolution ─────────────────────────────────────────


def test_progress_hook_is_forwarded(registry: RegistryStore) -> None:
    sdk = _FakeSDK()
    sink = _RecordingSink()
    events: list[tuple[str, int, bool]] = []
    selected = registry.list_contracts(underlying="NIFTY")
    pull_contract_candles(
        selected,
        lake_root=Path("/unused"),
        dry_run=True,
        intervals=("1day",),
        registry=registry,
        client_factory=lambda _root: _factory(sdk, sink),
        on_progress=lambda label, bars, failed: events.append((label, bars, failed)),
        now=_NOW,
    )
    assert len(events) == 3
    assert all(not failed for _label, _bars, failed in events)


def test_explicit_range_is_honoured(registry: RegistryStore) -> None:
    sdk = _FakeSDK()
    sink = _RecordingSink()
    selected = registry.list_contracts(underlying="NIFTY")
    report = pull_contract_candles(
        selected,
        lake_root=Path("/unused"),
        dry_run=True,
        intervals=("1day",),
        registry=registry,
        from_date=date(2023, 1, 1),
        to_date=date(2023, 12, 31),
        client_factory=lambda _root: _factory(sdk, sink),
        now=_NOW,
    )
    assert report.contracts_completed == 3


def test_inverted_range_raises(registry: RegistryStore) -> None:
    sdk = _FakeSDK()
    sink = _RecordingSink()
    selected = registry.list_contracts(underlying="NIFTY")
    with pytest.raises(ValueError, match="empty"):
        pull_contract_candles(
            selected,
            lake_root=Path("/unused"),
            dry_run=True,
            intervals=("1day",),
            registry=registry,
            from_date=date(2024, 6, 1),
            to_date=date(2024, 1, 1),
            client_factory=lambda _root: _factory(sdk, sink),
            now=_NOW,
        )


def test_from_date_below_floor_is_clamped(registry: RegistryStore) -> None:
    """A ``from_date`` before the 2020 floor is clamped (never widens scope)."""
    start, end = hp._resolve_range(from_date=date(2018, 1, 1), to_date=date(2021, 1, 1), now=_NOW)
    assert start == GROWW_HISTORICAL_FLOOR
    assert end == date(2021, 1, 2)


def test_default_range_is_floor_to_prev_day() -> None:
    """No bounds → ``[2020-01-01, previous day)`` (never the in-progress session)."""
    start, end = hp._resolve_range(from_date=None, to_date=None, now=_NOW)
    assert start == GROWW_HISTORICAL_FLOOR
    assert end == date(2026, 6, 14)  # prev day (2026-06-13) + 1 → exclusive


# ── to_dict surface ──────────────────────────────────────────────────────────


def test_report_to_dict_is_json_safe(registry: RegistryStore) -> None:
    sdk = _FakeSDK()
    sink = _RecordingSink()
    selected = registry.list_contracts(underlying="NIFTY")
    report = pull_contract_candles(
        selected,
        lake_root=Path("/unused"),
        dry_run=True,
        intervals=("1day",),
        registry=registry,
        client_factory=lambda _root: _factory(sdk, sink),
        now=_NOW,
    )
    payload = report.to_dict()
    assert payload["contracts_selected"] == 3
    assert payload["contracts_completed"] == 3
    assert payload["intervals"] == ["1day"]
    assert isinstance(payload["note"], str)


# ── DerivativeContract reconstruction fidelity ───────────────────────────────


def test_rebuild_is_bit_faithful_to_registry(registry: RegistryStore) -> None:
    """The rebuilt DerivativeContract carries the registry's authoritative fields."""
    rows = registry.list_contracts(underlying="NIFTY")
    rebuilt = hp._rebuild_contracts(rows)
    by_symbol = {row.contract_symbol: contract for row, contract in rebuilt}
    ce = by_symbol["NSE-NIFTY-25Jan24-21000-CE"]
    assert isinstance(ce, DerivativeContract)
    assert ce.contract_type == "CE"
    assert ce.strike == 21000.0
    assert ce.underlying_symbol == "NIFTY"
    assert ce.expiry == date(2024, 1, 25)
    fut = by_symbol["NSE-NIFTY-25Jan24-FUT"]
    assert fut.contract_type == "FUT"
    assert fut.strike is None


def test_strike_is_not_money_full_precision(registry: RegistryStore) -> None:
    """Strike stays a plain float (never money-rounded) — Decimal-equal to the registry."""
    rows = registry.list_contracts(underlying="RELIANCE")
    rebuilt = hp._rebuild_contracts(rows)
    _row, contract = rebuilt[0]
    assert Decimal(str(contract.strike)) == Decimal("2800.0")


# ── direct helper coverage (the branches not reached end-to-end) ──────────────


def test_empty_candle_sdk_is_inert() -> None:
    """The dry-run SDK returns empty for every surface (no fabricated chain/candle)."""
    sdk = hp._EmptyCandleSDK()
    assert sdk.get_expiries() == {"expiries": []}
    assert sdk.get_contracts() == {"contracts": []}
    assert sdk.get_historical_candles() == {"candles": []}


def test_dry_run_factory_builds_null_sink() -> None:
    """``_build_dry_run_client`` returns a real GrowwClient + a NullBarSink (0 bars)."""
    from brutex.data.sink import NullBarSink

    client, sink = hp._build_dry_run_client(Path("/unused"))
    assert isinstance(client, GrowwClient)
    assert isinstance(sink, NullBarSink)


def test_resolve_client_factory_dry_run_vs_live() -> None:
    """dry_run → the empty-candle factory; live → the (pragma'd) live factory closure."""
    dry = hp._resolve_client_factory(dry_run=True, pace=None, on_rate_limit=None, on_success=None)
    assert dry is hp._build_dry_run_client
    live = hp._resolve_client_factory(dry_run=False, pace=None, on_rate_limit=None, on_success=None)
    # The live branch returns a distinct closure (not the dry-run factory).
    assert live is not hp._build_dry_run_client


def test_rate_hooks_offline_are_all_none() -> None:
    """Dry-run / no controller → all-None hooks (no pacing offline)."""
    assert hp._rate_hooks(None, dry_run=True) == (None, None, None, None)
    assert hp._rate_hooks(None, dry_run=False) == (None, None, None, None)


def test_rate_hooks_live_returns_controller_hooks() -> None:
    """Live + a SessionRateController → its single controller's pace/limit/timeout/success."""
    from brutex.dashboard.pull_controller import SessionRateController

    session = SessionRateController()
    pace, on_rl, on_to, on_ok = hp._rate_hooks(session, dry_run=False)
    controller = session.get()
    assert pace == controller.pace
    assert on_rl == controller.on_rate_limit
    assert on_to == controller.on_timeout
    assert on_ok == controller.on_success


class _CountingRunner:
    """A ``RunParallel`` that COUNTS its uses, delegating to a deterministic serial runner.

    Proves an explicit ``run_parallel`` is forwarded all the way to
    ``run_groww_option_ingest`` (used once per market bucket) without changing behaviour.
    """

    def __init__(self) -> None:
        self.calls = 0
        self._inner = SerialRunParallel()

    def __call__[ItemT, ResultT](
        self,
        fn: Callable[[ItemT], ResultT],
        items: Sequence[ItemT],
    ) -> Iterable[ItemResult[ItemT, ResultT]]:
        self.calls += 1
        yield from self._inner(fn, items)


def test_option_run_parallel_offline_is_none() -> None:
    """Dry-run / no controller → None (the option ingest keeps its serial default)."""
    from brutex.dashboard.pull_controller import SessionRateController

    assert hp._option_run_parallel(dry_run=True, session_rate=None) is None
    # A dry-run WITH a controller is still None (no network → deterministic serial).
    assert hp._option_run_parallel(dry_run=True, session_rate=SessionRateController()) is None
    # Live but NO session controller → None too.
    assert hp._option_run_parallel(dry_run=False, session_rate=None) is None


def test_option_run_parallel_live_is_pool() -> None:
    """Live (a session controller present, not a dry-run) → a bounded PoolRunParallel."""
    from brutex.dashboard.pull_controller import SessionRateController
    from brutex.orchestrator.parallel import PoolRunParallel

    runner = hp._option_run_parallel(dry_run=False, session_rate=SessionRateController())
    assert isinstance(runner, PoolRunParallel)


def test_pull_contract_candles_forwards_explicit_run_parallel(registry: RegistryStore) -> None:
    """An explicit *run_parallel* WINS and is forwarded to the option ingest per bucket."""
    sdk = _FakeSDK()
    sink = _RecordingSink()
    runner = _CountingRunner()
    selected = registry.list_contracts(underlying="NIFTY")
    report = pull_contract_candles(
        selected,
        lake_root=Path("/unused"),
        dry_run=True,
        intervals=("1day",),
        registry=registry,
        from_date=_FROM,
        to_date=_TO,
        client_factory=lambda _root: _factory(sdk, sink),
        now=_NOW,
        run_parallel=runner,
    )
    # NIFTY spans 2 market buckets (option CE/PE + future FUT); the INJECTED runner drained
    # each bucket's phase-1 pass — never the auto-derived default.
    assert runner.calls >= 1
    assert report.contracts_completed == 3


def test_pull_contract_candles_live_derives_pool(registry: RegistryStore, tmp_lake: Path) -> None:
    """Live (session controller, not dry-run, no explicit runner) → derives + uses the pool."""
    from brutex.dashboard.pull_controller import SessionRateController

    sdk = _FakeSDK()
    sink = _RecordingSink()
    selected = registry.list_contracts(underlying="NIFTY")
    report = pull_contract_candles(
        selected,
        lake_root=tmp_lake,
        dry_run=False,
        intervals=("1day",),
        registry=registry,
        from_date=_FROM,
        to_date=_TO,
        client_factory=lambda _root: _factory(sdk, sink),
        now=_NOW,
        session_rate=SessionRateController(),
    )
    # The run_parallel-is-None live branch built a PoolRunParallel; the pull still completes
    # correctly through the real thread pool (offline fakes), all 3 NIFTY legs landed.
    assert report.contracts_completed == 3


def test_contract_market_split() -> None:
    """A FUT row maps to the future market; CE/PE map to the option market."""
    fetched = datetime(2026, 1, 1, tzinfo=UTC)
    fut = ContractRow(
        "NSE",
        "NIFTY",
        date(2024, 1, 25),
        "NSE-NIFTY-25Jan24-FUT",
        None,
        "FUT",
        "none",
        "r",
        fetched,
    )
    ce = ContractRow("NSE", "NIFTY", date(2024, 1, 25), "x-CE", 1.0, "CE", "none", "r", fetched)
    assert hp._contract_market(fut) == GROWW_FUTURE_COVERAGE_MARKET
    assert hp._contract_market(ce) == GROWW_OPTION_COVERAGE_MARKET


def test_build_note_branches() -> None:
    """Every ``_build_note`` arm renders an honest, distinct one-liner."""
    empty = hp._build_note(
        dry_run=False,
        contracts_selected=0,
        contracts_completed=0,
        windows=0,
        failed_pairs=0,
        still_failed_windows=0,
        auth_aborted=False,
    )
    assert "no contracts selected" in empty

    dry = hp._build_note(
        dry_run=True,
        contracts_selected=3,
        contracts_completed=0,
        windows=0,
        failed_pairs=0,
        still_failed_windows=0,
        auth_aborted=False,
    )
    assert "dry-run" in dry

    auth = hp._build_note(
        dry_run=False,
        contracts_selected=3,
        contracts_completed=0,
        windows=0,
        failed_pairs=0,
        still_failed_windows=0,
        auth_aborted=True,
    )
    assert "auth token expired" in auth

    quarantine = hp._build_note(
        dry_run=False,
        contracts_selected=3,
        contracts_completed=2,
        windows=2,
        failed_pairs=1,
        still_failed_windows=0,
        auth_aborted=False,
    )
    assert "quarantined" in quarantine

    transient = hp._build_note(
        dry_run=False,
        contracts_selected=3,
        contracts_completed=2,
        windows=2,
        failed_pairs=0,
        still_failed_windows=1,
        auth_aborted=False,
    )
    assert "transient window(s) still" in transient

    clean = hp._build_note(
        dry_run=False,
        contracts_selected=3,
        contracts_completed=3,
        windows=3,
        failed_pairs=0,
        still_failed_windows=0,
        auth_aborted=False,
    )
    assert "into the candle lake" in clean


def test_build_note_not_yet_in_window_breakdown() -> None:
    """DEC-FNO-FAR-EXPIRY-WINDOW-001 display honesty: a skip never reads as a failure.

    A clean pull whose selection included not-yet-open contracts would render
    "8/12 contract(s)" — indistinguishable from 4 failures — so the clean line
    carries the honest breakdown; zero skips renders byte-identically to before.
    """
    with_skips = hp._build_note(
        dry_run=False,
        contracts_selected=12,
        contracts_completed=8,
        windows=8,
        failed_pairs=0,
        still_failed_windows=0,
        auth_aborted=False,
        contracts_not_yet_in_window=4,
    )
    assert "8/12 contract(s) (4 not yet in window)" in with_skips

    without_skips = hp._build_note(
        dry_run=False,
        contracts_selected=12,
        contracts_completed=12,
        windows=12,
        failed_pairs=0,
        still_failed_windows=0,
        auth_aborted=False,
    )
    assert "not yet in window" not in without_skips
    assert "12/12 contract(s) into the candle lake" in without_skips


def test_clean_symbols_excludes_quarantine_and_still_failing() -> None:
    """``_clean_symbols`` returns only the pairs that succeeded with no residual."""
    from brutex.adapters.groww.option_ingest import GrowwOptionIngestReport, OptionPairFailure

    report = GrowwOptionIngestReport(
        pairs={
            ("CLEAN", "1day"): IngestReport(windows=1, bars_appended=1, transient_failures=0),
            ("STUCK", "1day"): IngestReport(
                windows=1, bars_appended=0, transient_failures=1, still_failed_windows=1
            ),
        },
        failures=(OptionPairFailure(symbol="QUAR", interval="1day", error="bad"),),
    )
    assert hp._clean_symbols(report) == frozenset({"CLEAN"})


# ── 1-minute ONLY, 1-day DERIVED (DEC-1MINUTE-ONLY-DERIVE-1DAY-001) ────────────
#
# 1-minute is the ONLY granularity fetched from Groww for F&O; 1-day is DERIVED
# from it (never vendor-fetched), so the prior two-phase 1-day-first barrier is
# RETIRED. These tests assert the single 1-minute phase, the per-contract default
# window (SPLIT by type — FUT expiry − 60 days → expiry, CE/PE expiry − 30 days →
# expiry; DEC-FUTURES-LOOKBACK-60D-001), and the explicit-dates override.


def test_fno_pulls_minute_only_never_day(registry: RegistryStore) -> None:
    """1-minute is the ONLY vendor granularity fetched — ``1day`` is NEVER requested."""
    sdk = _FakeSDK()
    sink = _RecordingSink()
    selected = registry.list_contracts(underlying="NIFTY")
    report = pull_contract_candles(
        selected,
        lake_root=Path("/unused"),
        dry_run=True,
        intervals=("1day", "1minute"),  # even ``both`` → 1-minute ONLY is fetched
        registry=registry,
        from_date=_FROM,
        to_date=_TO,
        client_factory=lambda _root: _factory(sdk, sink),
        now=_NOW,
    )
    # The vendor was called ONLY at 1-minute, for every selected contract, never 1-day.
    assert set(sdk.candle_intervals) == {"1minute"}
    assert "1day" not in sdk.candle_intervals
    assert set(sdk.candle_symbols) == {r.contract_symbol for r in selected}
    # The repurposed single-phase report fields.
    assert report.minute_requested is True
    assert report.minute_skipped is False
    assert report.phase1_clean is True


def test_minute_window_defaults_split_futures_60_options_30(registry: RegistryStore) -> None:
    """No explicit dates → a FUTURE starts at expiry − 60 days, an OPTION at − 30 days.

    DEC-FUTURES-LOOKBACK-60D-001: the per-contract default lookback is SPLIT by type —
    the FUT contract (expiry 2024-01-25) starts a wider 60 days back (2023-11-26) while
    the CE/PE options stay on the tighter 30-day default (2023-12-26).
    """
    sdk = _FakeSDK()
    sink = _RecordingSink()
    selected = registry.list_contracts(underlying="NIFTY")
    report = pull_contract_candles(
        selected,
        lake_root=Path("/unused"),
        dry_run=True,
        intervals=("1minute",),
        registry=registry,
        # NO explicit dates → the per-contract default kicks in.
        from_date=None,
        to_date=None,
        client_factory=lambda _root: _factory(sdk, sink),
        now=_NOW,
    )
    # expiry 2024-01-25 − 60 days = 2023-11-26 (FUT); − 30 days = 2023-12-26 (CE/PE).
    for r in selected:
        expected = "2023-11-26" if r.side == "FUT" else "2023-12-26"
        assert report.start_dates[r.contract_symbol] == expected
    # The EARLIEST recorded fetch start is the futures 60-day window start (2023-11-26),
    # never blindly 2020; every chunk-start is within a window.
    starts = [start[:10] for _sym, start in sdk.starts]
    assert min(starts) == "2023-11-26"
    assert all("2023-11-26" <= s <= "2024-01-25" for s in starts)


def test_explicit_dates_override_default_window(registry: RegistryStore) -> None:
    """Explicit from/to dates apply to EVERY contract (override the per-contract default)."""
    sdk = _FakeSDK()
    sink = _RecordingSink()
    selected = registry.list_contracts(underlying="NIFTY")
    report = pull_contract_candles(
        selected,
        lake_root=Path("/unused"),
        dry_run=True,
        intervals=("1minute",),
        registry=registry,
        from_date=date(2024, 1, 1),
        to_date=date(2024, 1, 20),
        client_factory=lambda _root: _factory(sdk, sink),
        now=_NOW,
    )
    # All contracts share the explicit start (the default expiry−30d is OVERRIDDEN).
    assert set(report.start_dates.values()) == {"2024-01-01"}
    assert {start[:10] for _sym, start in sdk.starts} == {"2024-01-01"}


def test_dry_run_minute_request_is_honest_empty(registry: RegistryStore) -> None:
    """A dry-run 1-minute pull settles empty-clean; start_dates is the resolved windows."""
    selected = registry.list_contracts(underlying="NIFTY")
    report = pull_contract_candles(
        selected,
        lake_root=Path("/unused"),
        dry_run=True,
        intervals=("1minute",),
        registry=registry,
        from_date=_FROM,
        to_date=_TO,
        now=_NOW,
    )
    assert report.dry_run is True
    assert report.bars_appended == 0  # the empty-candle dry-run client appends 0 bars
    assert report.phase1_clean is True  # an EMPTY window is a settled, clean window
    assert report.minute_requested is True
    assert report.minute_skipped is False
    # start_dates is now POPULATED (resolved windows for every contract), not lake-discovered.
    assert report.start_dates == {r.contract_symbol: "2024-01-01" for r in selected}


def test_resolve_minute_windows_explicit_uses_full_range(registry: RegistryStore) -> None:
    """``_resolve_minute_windows(explicit_dates=True)`` → the resolved range for all."""
    rebuilt = hp._rebuild_contracts(registry.list_contracts(underlying="NIFTY"))
    out = hp._resolve_minute_windows(
        rebuilt=rebuilt, explicit_dates=True, start=date(2024, 1, 1), end=date(2024, 1, 21)
    )
    assert set(out.values()) == {(date(2024, 1, 1), date(2024, 1, 21))}
    assert set(out) == {c.symbol for _r, c in rebuilt}


def test_resolve_minute_windows_default_splits_futures_60_options_30(
    registry: RegistryStore,
) -> None:
    """``_resolve_minute_windows(explicit_dates=False)`` → FUT expiry−60d, CE/PE expiry−30d.

    DEC-FUTURES-LOOKBACK-60D-001: the NIFTY chain is CE + PE + FUT (expiry 2024-01-25);
    the FUT window starts 60 days back (2023-11-26) while the CE/PE windows start the
    tighter 30 days back (2023-12-26). This proves ``is_option`` routes correctly.
    """
    rebuilt = hp._rebuild_contracts(registry.list_contracts(underlying="NIFTY"))
    out = hp._resolve_minute_windows(
        rebuilt=rebuilt, explicit_dates=False, start=date(2020, 1, 1), end=date(2026, 6, 13)
    )
    by_symbol = {c.symbol: c for _r, c in rebuilt}
    # FUT → [2023-11-26, 2024-01-26) (a 60-day lookback).
    assert out[next(s for s, c in by_symbol.items() if c.contract_type == "FUT")] == (
        date(2023, 11, 26),
        date(2024, 1, 26),
    )
    # CE / PE → [2023-12-26, 2024-01-26) (the 30-day lookback, UNCHANGED).
    assert out[next(s for s, c in by_symbol.items() if c.contract_type == "CE")] == (
        date(2023, 12, 26),
        date(2024, 1, 26),
    )
    assert out[next(s for s, c in by_symbol.items() if c.contract_type == "PE")] == (
        date(2023, 12, 26),
        date(2024, 1, 26),
    )


def _seed_far_expiry_pair(registry: RegistryStore) -> date:
    """Seed a Sep-2026 CE + FUT pair (far beyond the test envelopes) — returns the expiry."""
    far_expiry = date(2026, 9, 29)
    registry.upsert_contract(
        "NSE",
        "NIFTY",
        far_expiry,
        "NSE-NIFTY-29Sep26-21000-CE",
        side="CE",
        strike=21000.0,
        run_id=_RUN_ID,
        fetched_at=_FETCHED,
    )
    registry.upsert_contract(
        "NSE",
        "NIFTY",
        far_expiry,
        "NSE-NIFTY-29Sep26-FUT",
        side="FUT",
        strike=None,
        run_id=_RUN_ID,
        fetched_at=_FETCHED,
    )
    return far_expiry


def test_resolve_minute_windows_far_expiry_not_open_skipped_and_logged(
    registry: RegistryStore,
) -> None:
    """A NOT-YET-OPEN contract is OMITTED from the window map and logged — never full range.

    DEC-FNO-FAR-EXPIRY-WINDOW-001 (operator law; fails pre-fix): a Sep-2026 monthly
    whose strict ``expiry − lookback`` window lies AFTER the resolved ``end`` used to
    degrade to the FULL 2020→today range (~80 × 30-day chunks of dead history per
    contract). Now BOTH the CE (opens 2026-08-30) and the FUT (opens 2026-07-31) get
    NO map entry — the pull skips them with zero vendor calls — and each skip emits
    one honest ``fno.window_not_open`` line naming the contract + its opens_on.
    """
    far_expiry = _seed_far_expiry_pair(registry)
    rebuilt = hp._rebuild_contracts(
        [r for r in registry.list_contracts(underlying="NIFTY") if r.expiry_date == far_expiry]
    )
    with capture_logs() as logs:
        out = hp._resolve_minute_windows(
            rebuilt=rebuilt, explicit_dates=False, start=date(2020, 1, 1), end=date(2026, 7, 1)
        )
    assert out == {}  # both not-yet-open contracts omitted — no substitute window
    events = [e for e in logs if e["event"] == "fno.window_not_open"]
    by_contract = {e["contract"]: e for e in events}
    assert set(by_contract) == {"NSE-NIFTY-29Sep26-21000-CE", "NSE-NIFTY-29Sep26-FUT"}
    assert by_contract["NSE-NIFTY-29Sep26-21000-CE"]["opens_on"] == "2026-08-30"  # expiry−30d
    assert by_contract["NSE-NIFTY-29Sep26-FUT"]["opens_on"] == "2026-07-31"  # expiry−60d
    assert all(e["expiry"] == "2026-09-29" for e in events)


def test_resolve_minute_windows_open_contract_start_is_stable_across_days(
    registry: RegistryStore,
) -> None:
    """Once OPEN, the default window start == expiry−lookback and NEVER moves.

    The near-expiry NIFTY chain (expiry 2024-01-25) resolved on three consecutive
    simulated days keeps the SAME expiry-anchored start while only the end advances
    — no moving anchor exists (the ledger's trailing-END GC applies as usual).
    """
    rebuilt = hp._rebuild_contracts(registry.list_contracts(underlying="NIFTY"))
    ce_symbol = next(c.symbol for _r, c in rebuilt if c.contract_type == "CE")
    starts = []
    for offset in range(3):
        out = hp._resolve_minute_windows(
            rebuilt=rebuilt,
            explicit_dates=False,
            start=date(2020, 1, 1),
            end=date(2024, 1, 10) + timedelta(days=offset),
        )
        starts.append(out[ce_symbol][0])
        assert out[ce_symbol][1] == date(2024, 1, 10) + timedelta(days=offset)
    assert starts == [date(2023, 12, 26)] * 3  # expiry−30d, fixed forever


def test_pull_skips_not_yet_open_contracts_no_fetch_no_ledger(
    registry: RegistryStore, tmp_lake: Path
) -> None:
    """DEC-FNO-FAR-EXPIRY-WINDOW-001 end-to-end: a not-yet-open contract is fetched NOT AT ALL.

    LIVE-shaped (dry_run=False) DEFAULT pull over the near NIFTY chain PLUS a far
    Sep-2026 CE+FUT pair whose windows open only on 2026-08-30 / 2026-07-31 (the
    envelope ends 2026-06-13): the vendor sees ZERO calls for the far pair (the near
    chain is fetched normally), the persisted window ledger holds ZERO rows for it,
    the report counts it honestly, and each skip is logged. Fails pre-fix: the pair
    was pulled over the FULL 2020→today range (~80 chunks each).
    """
    from brutex.data.window_ledger_store import load_window_ledger

    _seed_far_expiry_pair(registry)
    sdk = _FakeSDK()
    sink = _RecordingSink()
    selected = registry.list_contracts(underlying="NIFTY")  # 3 near + 2 far
    with capture_logs() as logs:
        report = pull_contract_candles(
            selected,
            lake_root=tmp_lake,
            dry_run=False,
            intervals=("1day",),
            registry=registry,
            client_factory=lambda _root: _factory(sdk, sink),
            now=_NOW,
        )
    far = {"NSE-NIFTY-29Sep26-21000-CE", "NSE-NIFTY-29Sep26-FUT"}
    near = {r.contract_symbol for r in selected} - far
    # ZERO vendor calls for the far pair; the near chain fetched normally.
    assert set(sdk.candle_symbols) == near
    # ZERO ledger rows for the far pair; the near chain has real persisted rows.
    ledger_symbols = {rec.security_id for rec in load_window_ledger(tmp_lake).records()}
    assert ledger_symbols & far == set()
    assert near <= ledger_symbols
    # The report is honest about the exclusion.
    assert report.contracts_selected == 5
    assert report.contracts_not_yet_in_window == 2
    assert set(report.start_dates) == near
    assert far & set(report.phase1_complete) == set()
    assert report.to_dict()["contracts_not_yet_in_window"] == 2
    assert "3/5 contract(s) (2 not yet in window)" in report.note  # display honesty
    # Each skip logged loudly with its opens_on date.
    events = {e["contract"] for e in logs if e["event"] == "fno.window_not_open"}
    assert events == far


def test_pull_all_contracts_not_yet_open_is_a_clean_noop(
    registry: RegistryStore, tmp_lake: Path
) -> None:
    """An ALL-not-yet-open selection completes cleanly: zero fetches, zero rows, no crash."""
    from brutex.data.window_ledger_store import load_window_ledger

    far_expiry = _seed_far_expiry_pair(registry)
    sdk = _FakeSDK()
    sink = _RecordingSink()
    selected = [
        r for r in registry.list_contracts(underlying="NIFTY") if r.expiry_date == far_expiry
    ]
    report = pull_contract_candles(
        selected,
        lake_root=tmp_lake,
        dry_run=False,
        intervals=("1day",),
        registry=registry,
        client_factory=lambda _root: _factory(sdk, sink),
        now=_NOW,
    )
    assert sdk.candle_symbols == []  # ZERO vendor calls
    assert sink.appends == []  # nothing written
    assert report.contracts_selected == 2
    assert report.contracts_not_yet_in_window == 2
    assert report.contracts_completed == 0
    assert report.bars_appended == 0
    assert report.windows == 0
    assert report.start_dates == {}
    assert load_window_ledger(tmp_lake).records() == []  # zero ledger rows


def test_resolve_minute_windows_explicit_overrides_far_expiry(registry: RegistryStore) -> None:
    """Explicit operator dates stay BYTE-IDENTICAL even for a FAR-EXPIRY contract.

    DEC-FNO-FAR-EXPIRY-WINDOW-001 (operator law): the strict-window skip applies to
    the DEFAULT path only — with ``explicit_dates=True`` the operator can still pull
    ANY range they name: a Sep-2026 contract gets the resolved envelope verbatim,
    never a skip and never a lookback-derived window.
    """
    registry.upsert_contract(
        "NSE",
        "NIFTY",
        date(2026, 9, 29),
        "NSE-NIFTY-29Sep26-21000-CE",
        side="CE",
        strike=21000.0,
        run_id=_RUN_ID,
        fetched_at=_FETCHED,
    )
    rebuilt = hp._rebuild_contracts(registry.list_contracts(underlying="NIFTY"))
    out = hp._resolve_minute_windows(
        rebuilt=rebuilt, explicit_dates=True, start=date(2026, 6, 1), end=date(2026, 7, 1)
    )
    assert out["NSE-NIFTY-29Sep26-21000-CE"] == (date(2026, 6, 1), date(2026, 7, 1))
    assert set(out.values()) == {(date(2026, 6, 1), date(2026, 7, 1))}


def test_live_pull_writes_no_daily_partition_mid_pull(
    registry: RegistryStore, tmp_lake: Path
) -> None:
    """DEC-PIPELINE-AUTOPREBUILD-001: the F&O pull writes NO derived data mid-pull.

    REGRESSION FLIP of the removed ``_derive_daily_from_minute`` suite: the
    always-on post-drain F&O 1-day derive is REMOVED — the derived ``1day``
    (and every intraday TF) is written ONLY by the pipeline's final prebuild
    phase. A LIVE-shaped pull with a REAL ParquetBarSink lands the 1-minute
    bars in the lake but leaves NO ``1day`` partition behind.
    """
    from brutex.data.parquet_writer import ParquetWriter
    from brutex.data.sink import ParquetBarSink

    sdk = _FakeSDK()
    sink = ParquetBarSink(writer=ParquetWriter(lake_root=tmp_lake))
    selected = [
        r for r in registry.list_contracts(underlying="NIFTY") if r.contract_symbol.endswith("FUT")
    ]
    pull_contract_candles(
        selected,
        lake_root=tmp_lake,
        dry_run=False,
        intervals=("1minute",),
        registry=registry,
        from_date=_FROM,
        to_date=_TO,
        client_factory=lambda _root: _factory(sdk, sink),
        now=_NOW,
    )
    # The 1-minute bars ARE durably in the lake (the pull did its job) ...
    assert sorted(tmp_lake.glob("bars/*/FNO/*/1minute/*/*.parquet"))
    # ... but NO derived 1day partition exists — derived data is written ONLY
    # by the pipeline prebuild (DEC-PIPELINE-AUTOPREBUILD-001), never mid-pull.
    assert not list(tmp_lake.glob("bars/*/FNO/*/1day/**")), (
        "the F&O pull must not write a derived 1day partition mid-pull"
    )


# ── DEC-FNO-LEDGER-LAKE-RECONCILE-001: F&O ledger↔lake cold-start reconcile ───────────


#: The WIDE seed window — deliberately a few days PAST the (narrower) second-pull window so
#: the stored lake span's ``last_date`` FULLY ENCLOSES the second pull's exclusive window end
#: (a window ``[start, end)`` is enclosed iff ``last_date >= end``, the SAME conservative
#: rule the spot reconcile uses — the last stored bar must prove coverage through the end).
_SEED_TO = date(2024, 1, 25)


def _full_window_payload() -> dict[str, object]:
    """1-day bars spanning the WIDE seed window ``[2024-01-01, 2024-01-25]`` (both edges + past).

    The single-bar ``_candle_payload`` sits mid-window (2024-01-15) so its stored span does
    NOT enclose a fetch window; this payload's bars span from the window START to a date PAST
    the second pull's exclusive end (2024-01-21), so the seeded lake span fully encloses the
    second pull's ``[2024-01-01, 2024-01-21)`` window and the cold-start reconcile seeds it.
    """
    return {
        "candles": [
            ["2024-01-01T09:15:00", 100.0, 110.0, 90.0, 105.0, 50, 7],
            ["2024-01-23T09:15:00", 100.0, 110.0, 90.0, 105.0, 50, 7],
            ["2024-01-25T09:15:00", 100.0, 110.0, 90.0, 105.0, 50, 7],
        ]
    }


class _FullWindowSDK(_FakeSDK):
    """A ``_FakeSDK`` whose candle payload spans the full window (for lake seeding)."""

    def get_historical_candles(self, **kw: object) -> dict[str, object]:
        symbol = str(kw["groww_symbol"])
        self.candle_symbols.append(symbol)
        self.candle_intervals.append(str(kw["candle_interval"]))
        self.starts.append((symbol, str(kw["start_time"])))
        exc = self._raise_for.get(symbol)
        if exc is not None:
            raise exc
        return _full_window_payload()


def _real_sink_factory(sdk: _FakeSDK, lake_root: Path) -> tuple[GrowwClient, BarSink]:
    """A ``client_factory`` handing back a real ``ParquetBarSink`` writing to *lake_root*."""
    from brutex.data.parquet_writer import ParquetWriter
    from brutex.data.sink import ParquetBarSink

    return _client(sdk), ParquetBarSink(writer=ParquetWriter(lake_root=lake_root))


def _delete_ledger(lake_root: Path) -> None:
    """Simulate a LOST / orphaned ledger (fresh clone / deleted file / new lake root)."""
    from brutex.data.window_ledger_store import ledger_path

    path = ledger_path(lake_root)
    assert path.exists(), "the first pull must have persisted a ledger to delete"
    path.unlink()


def _seed_lake_and_orphan_ledger(registry: RegistryStore, tmp_lake: Path) -> _FakeSDK:
    """First pull → real F&O bars + a settled ledger; then delete the ledger (orphaned).

    Seeds over the WIDE ``[_FROM, _SEED_TO]`` window so the stored lake span reaches past the
    narrower second-pull window end and can fully enclose it (see :data:`_SEED_TO`).
    """
    seed_sdk = _FullWindowSDK()
    selected = registry.list_contracts(underlying="NIFTY")
    pull_contract_candles(
        selected,
        lake_root=tmp_lake,
        dry_run=False,
        intervals=("1day",),
        registry=registry,
        from_date=_FROM,
        to_date=_SEED_TO,
        client_factory=lambda root: _real_sink_factory(seed_sdk, root),
        now=_NOW,
    )
    # The lake now holds F&O bars for every NIFTY contract; the first pull fetched them all.
    assert set(seed_sdk.candle_symbols) == {r.contract_symbol for r in selected}
    _delete_ledger(tmp_lake)
    return seed_sdk


def test_fno_reconcile_skips_lake_present_windows_on_orphaned_ledger(
    registry: RegistryStore, tmp_lake: Path
) -> None:
    """PASSES-AFTER: a lake with F&O bars + an EMPTY ledger → the pull SKIPS every window.

    The cold-start reconcile seeds SUCCESS from the lake spans BEFORE planning, so the
    second pull re-fetches NOTHING (``get_historical_candles`` is never called). Without the
    fix those windows would re-plan PLANNED (not skippable) → re-fetch the whole chain.
    """
    _seed_lake_and_orphan_ledger(registry, tmp_lake)
    # Second pull against the SAME lake with a FRESH (empty) ledger + a spy SDK.
    spy_sdk = _FullWindowSDK()
    selected = registry.list_contracts(underlying="NIFTY")
    report = pull_contract_candles(
        selected,
        lake_root=tmp_lake,
        dry_run=False,
        intervals=("1day",),
        registry=registry,
        from_date=_FROM,
        to_date=_TO,
        client_factory=lambda root: _real_sink_factory(spy_sdk, root),
        now=_NOW,
    )
    # EVERY window was skipped by the reconcile — ZERO vendor candle fetches.
    assert spy_sdk.candle_symbols == []
    assert report.bars_appended == 0


def test_fno_orphaned_ledger_refetches_without_reconcile(
    registry: RegistryStore, tmp_lake: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FAILS-BEFORE: with the reconcile DISABLED, the same orphaned ledger re-fetches everything.

    Monkeypatching ``reconcile_fno_ledger_windows`` to a no-op reproduces the pre-fix
    behaviour: no SUCCESS is seeded, every window re-plans PLANNED (not skippable), and the
    whole NIFTY chain is re-fetched from Groww — the exact redundant multi-day re-pull the
    fix prevents.
    """
    _seed_lake_and_orphan_ledger(registry, tmp_lake)
    monkeypatch.setattr(hp, "reconcile_fno_ledger_windows", lambda *a, **k: 0)
    spy_sdk = _FullWindowSDK()
    selected = registry.list_contracts(underlying="NIFTY")
    pull_contract_candles(
        selected,
        lake_root=tmp_lake,
        dry_run=False,
        intervals=("1day",),
        registry=registry,
        from_date=_FROM,
        to_date=_TO,
        client_factory=lambda root: _real_sink_factory(spy_sdk, root),
        now=_NOW,
    )
    # WITHOUT the reconcile every NIFTY contract is re-fetched (the bug this PR fixes).
    assert set(spy_sdk.candle_symbols) == {r.contract_symbol for r in selected}


def test_fno_reconcile_dry_run_does_not_scan_lake(
    registry: RegistryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dry-run pull (ledger=None) never scans the lake for the F&O reconcile."""
    scanned = False

    def _spy_spans(_root: Path) -> list[dict[str, object]]:
        nonlocal scanned
        scanned = True
        return []

    monkeypatch.setattr(hp, "load_lake_spans", _spy_spans)
    sdk = _FakeSDK()
    sink = _RecordingSink()
    pull_contract_candles(
        registry.list_contracts(underlying="NIFTY"),
        lake_root=Path("/unused"),
        dry_run=True,
        intervals=("1day",),
        registry=registry,
        from_date=_FROM,
        to_date=_TO,
        client_factory=lambda _root: _factory(sdk, sink),
        now=_NOW,
    )
    assert scanned is False, "a dry-run must not spend the O(datasets) span scan"


# ── Greeks enrichment wiring (DEC-GREEKS-ENGINE-PR2B-001) ─────────────────────


class TestGreeksEnrichWiring:
    """``greeks_enrich_after_pull`` runs at the post-drain seam, fully automatic."""

    def test_live_pull_stores_greeks_for_options_and_leaves_fut_raw(
        self, registry: RegistryStore, monkeypatch: pytest.MonkeyPatch, tmp_lake: Path
    ) -> None:
        """A LIVE (pipeline-wired) pull enriches the just-written option 1-minute.

        End-to-end through the REAL seam: the fake vendor lands one 1-minute bar
        per contract on a REAL ParquetBarSink lake, spot is already in the lake
        (the run_full_pipeline SPOT → HISTORICAL ordering guarantee), and the
        post-pull hook joins + solves + STORES the Greeks overlay — the CE/PE
        partitions gain non-NULL ``iv`` while the FUT partition stays raw
        7-column ("never a bogus Greek for a future", plan §15.8).
        """
        pl = pytest.importorskip("polars")
        pytest.importorskip("trading_core")
        from brutex.data.models import CandleBar
        from brutex.data.parquet_writer import ParquetWriter
        from brutex.data.sink import ParquetBarSink

        monkeypatch.delenv("BRUTEX_GROWW_FETCH_PIPELINE", raising=False)  # default ON
        monkeypatch.delenv("BRUTEX_RISK_FREE_CSV", raising=False)  # the fixed-6.5% default
        # Spot at the SAME minute the canned candle lands (2024-01-15T09:15 IST).
        spot_ts = datetime(2024, 1, 15, 3, 45, tzinfo=UTC)
        sink = ParquetBarSink(writer=ParquetWriter(lake_root=tmp_lake))
        sink.append(
            exchange="NSE",
            segment="INDEX",
            symbol="NIFTY",
            interval="1minute",
            bars=[
                CandleBar(
                    timestamp=spot_ts,
                    open=Decimal("21000"),
                    high=Decimal("21010"),
                    low=Decimal("20990"),
                    close=Decimal("21000"),
                    volume=0,
                    open_interest=None,
                )
            ],
        )
        sdk = _FakeSDK()
        selected = registry.list_contracts(underlying="NIFTY")  # CE + PE + FUT
        with capture_logs() as logs:
            report = pull_contract_candles(
                selected,
                lake_root=tmp_lake,
                dry_run=False,
                intervals=("1day",),
                registry=registry,
                from_date=_FROM,
                to_date=_TO,
                client_factory=lambda _root: _factory(sdk, sink),
                now=_NOW,
            )
        assert report.contracts_completed == 3
        done = [r for r in logs if r["event"] == "fno.greeks_enrich.done"]
        assert len(done) == 1
        assert done[0]["futures_skipped"] == 1
        assert done[0]["enriched"] >= 1
        # DEC-GREEKS-SPOT-MEMO-001: the CE + PE share ONE (underlying, window)
        # spot coordinate → exactly one performed read, one memo hit, measured
        # in the done event (the FUT is skipped before the spot read).
        assert done[0]["spot_reads"] == 1
        assert done[0]["spot_hits"] == 1
        assert [r for r in logs if r["event"] == "fno.greeks_enrich.error"] == []
        # The CE premium (105 vs spot 21000, strike 21000) SOLVES and is STORED.
        ce = pl.read_parquet(
            tmp_lake / "bars/NSE/FNO/NSE-NIFTY-25Jan24-21000-CE/1minute/2024/01.parquet"
        )
        assert "iv" in ce.columns
        assert ce["iv"].null_count() == 0
        assert ce["spot_at_bar"].to_list() == [21000.0]
        # The FUT partition is untouched raw 7-column — never a bogus Greek.
        fut = pl.read_parquet(
            tmp_lake / "bars/NSE/FNO/NSE-NIFTY-25Jan24-FUT/1minute/2024/01.parquet"
        )
        assert fut.width == 7

    def test_dry_run_never_enriches(self, registry: RegistryStore) -> None:
        """A dry-run writes nothing, so the enrich hook must not run at all."""
        sdk = _FakeSDK()
        sink = _RecordingSink()
        with capture_logs() as logs:
            pull_contract_candles(
                registry.list_contracts(underlying="NIFTY"),
                lake_root=Path("/unused"),
                dry_run=True,
                intervals=("1day",),
                registry=registry,
                from_date=_FROM,
                to_date=_TO,
                client_factory=lambda _root: _factory(sdk, sink),
                now=_NOW,
            )
        assert [r for r in logs if str(r["event"]).startswith("fno.greeks_enrich")] == []

    def test_enrich_failure_never_fails_the_pull(
        self, registry: RegistryStore, monkeypatch: pytest.MonkeyPatch, tmp_lake: Path
    ) -> None:
        """A broken rate config (or any enrich error) logs ONE error and the pull succeeds."""
        pytest.importorskip("trading_core")
        import brutex.data.greeks_enrich as ge

        monkeypatch.delenv("BRUTEX_GROWW_FETCH_PIPELINE", raising=False)

        def _boom() -> object:
            raise RuntimeError("misconfigured risk-free source")

        monkeypatch.setattr(ge, "resolve_risk_free_source", _boom)
        sdk = _FakeSDK()
        sink = _RecordingSink()
        with capture_logs() as logs:
            report = pull_contract_candles(
                registry.list_contracts(underlying="NIFTY"),
                lake_root=tmp_lake,
                dry_run=False,
                intervals=("1day",),
                registry=registry,
                from_date=_FROM,
                to_date=_TO,
                client_factory=lambda _root: _factory(sdk, sink),
                now=_NOW,
            )
        assert report.contracts_completed == 3  # the pull itself is UNTOUCHED
        errors = [r for r in logs if r["event"] == "fno.greeks_enrich.error"]
        assert len(errors) == 1
        assert "misconfigured" in str(errors[0]["error"])
        assert [r for r in logs if r["event"] == "fno.greeks_enrich.done"] == []

    def test_target_building_skips_windowless_and_bad_exchange(
        self, monkeypatch: pytest.MonkeyPatch, tmp_lake: Path
    ) -> None:
        """`_enrich_greeks_from_minute` targets ONLY windowed, exchange-resolvable contracts."""
        pytest.importorskip("trading_core")
        import brutex.data.greeks_enrich as ge

        captured: list[str] = []

        def _fake_enrich(*, targets: object, lake_root: Path, rate_source: object) -> object:
            del lake_root, rate_source
            captured.extend(t.symbol for t in targets)  # type: ignore[attr-defined]
            return ge.GreeksEnrichReport(
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
            )

        monkeypatch.setattr(ge, "enrich_greeks_for_targets", _fake_enrich)
        expiry = date(2024, 1, 25)
        good = DerivativeContract(
            underlying_symbol="NIFTY",
            expiry=expiry,
            symbol="NSE-NIFTY-25Jan24-21000-CE",
            contract_type="CE",
            strike=21000.0,
        )
        windowless = DerivativeContract(
            underlying_symbol="NIFTY",
            expiry=expiry,
            symbol="NSE-NIFTY-25Jan24-21000-PE",
            contract_type="PE",
            strike=21000.0,
        )
        no_exchange = DerivativeContract(
            underlying_symbol="NIFTY",
            expiry=expiry,
            symbol="BADSYMBOL",  # no `-` → contract_exchange raises ValueError
            contract_type="FUT",
        )
        window = (date(2024, 1, 1), date(2024, 1, 20))
        rebuilt = [
            (None, good),
            (None, windowless),
            (None, no_exchange),
        ]
        with capture_logs() as logs:
            hp._enrich_greeks_from_minute(
                rebuilt=cast("list[hp._RebuiltContract]", rebuilt),
                windows={good.symbol: window, no_exchange.symbol: window},
                lake_root=tmp_lake,
            )
        assert captured == [good.symbol]
        assert [r for r in logs if r["event"] == "fno.greeks_enrich.done"] != []


# ── DEC-WINDOW-LEDGER-RESCUE-001: the lost-ledger rescue actually rescues ─────


class _DenseSDK(_FakeSDK):
    """A fake SDK returning bars that span each contract's WHOLE default window.

    Two bars per call — one on/before every default window's start (2023-11-24,
    ≤ the FUT ``expiry − 60d`` start) and one ON the expiry day (2024-01-25) — so
    the stored lake span fully covers every contract's default ``[start,
    expiry + 1 day)`` window: ``first_date <= start`` and the bar-inclusive
    ``last_date == expiry == window_end − 1``.
    """

    def get_historical_candles(self, **kw: object) -> dict[str, object]:
        symbol = str(kw["groww_symbol"])
        self.candle_symbols.append(symbol)
        self.candle_intervals.append(str(kw["candle_interval"]))
        self.starts.append((symbol, str(kw["start_time"])))
        return {
            "candles": [
                ["2023-11-24T09:30:00", 100.0, 110.0, 90.0, 105.0, 50, 7],
                ["2024-01-25T09:30:00", 101.0, 111.0, 91.0, 106.0, 60, 8],
            ]
        }


def _parquet_sink(lake_root: Path) -> BarSink:
    """A REAL lake sink so run 1's bars land on disk for run 2's reconcile."""
    from brutex.data.parquet_writer import ParquetWriter
    from brutex.data.sink import ParquetBarSink

    return ParquetBarSink(writer=ParquetWriter(lake_root=lake_root))


def test_lost_ledger_second_pull_makes_zero_vendor_calls(
    registry: RegistryStore, tmp_lake: Path
) -> None:
    """REGRESSION (b) — FAILS-BEFORE: the ledger↔lake rescue was DEAD CODE.

    Run 1 pulls the NIFTY chain over each contract's DEFAULT window (no explicit
    dates — end = ``expiry + 1 day``, fetch-exclusive) into a REAL lake; the durable
    ledger is then DELETED (the operator's observed loss). Run 2 must make ZERO
    F&O vendor calls for the fully-stored expired contracts: the cold-start
    ``reconcile_fno_ledger_windows`` seeds every lake-enclosed window SUCCESS. On
    unfixed main the enclosure test (``expiry + 1 <= expiry``) could never pass, so
    run 2 wholesale re-fetched the whole chain.
    """
    from brutex.data.window_ledger_store import ledger_path

    selected = registry.list_contracts(underlying="NIFTY")
    sdk1 = _DenseSDK()
    report1 = pull_contract_candles(
        selected,
        lake_root=tmp_lake,
        dry_run=False,  # live-shaped: the ledger + lake are REAL (factory-injected SDK)
        intervals=("1minute",),
        registry=registry,
        client_factory=lambda _root: _factory(sdk1, _parquet_sink(tmp_lake)),
        now=_NOW,
    )
    assert report1.contracts_completed == 3
    assert len(sdk1.candle_symbols) > 0  # run 1 really hit the vendor
    # THE LOSS: the durable ledger vanishes between the two pulls.
    ledger_file = ledger_path(tmp_lake)
    assert ledger_file.is_file()
    ledger_file.unlink()

    sdk2 = _DenseSDK()
    report2 = pull_contract_candles(
        selected,
        lake_root=tmp_lake,
        dry_run=False,
        intervals=("1minute",),
        registry=registry,
        client_factory=lambda _root: _factory(sdk2, _parquet_sink(tmp_lake)),
        now=_NOW,
    )
    # THE RESCUE: zero vendor calls — every window was seeded from the lake + skipped
    # (the PRIMARY behavioural assert — on unfixed main this lists the wholesale
    # re-fetch of the entire chain).
    assert sdk2.candle_symbols == []
    assert report2.windows == 3
    assert report1.windows_skipped == 0  # nothing was skippable on run 1's fresh lake
    assert report2.windows_skipped == 3
    assert report2.contracts_completed == 3
    # A MISSING ledger is a fresh start, not a reload FAILURE (honest distinction).
    assert report2.ledger_load_failed is False
    assert "skipped 3 already-pulled window(s) (no vendor hit)" in report2.note
    assert report2.to_dict()["windows_skipped"] == 3


def test_corrupt_ledger_reload_failure_is_announced(
    registry: RegistryStore, tmp_lake: Path
) -> None:
    """REGRESSION (d) — a corrupt (unreadable) ledger is LOUD, never silent.

    The store degrades to an empty ledger (never crashes the pull) but the report
    + note must SAY the reload failed and that a full re-verify pull ran.
    """
    from brutex.data.window_ledger_store import ledger_path

    ledger_file = ledger_path(tmp_lake)
    ledger_file.parent.mkdir(parents=True, exist_ok=True)
    ledger_file.write_bytes(b"not a parquet file at all")
    sdk = _DenseSDK()
    selected = registry.list_contracts(underlying="NIFTY")
    with capture_logs() as logs:
        report = pull_contract_candles(
            selected,
            lake_root=tmp_lake,
            dry_run=False,
            intervals=("1minute",),
            registry=registry,
            client_factory=lambda _root: _factory(sdk, _parquet_sink(tmp_lake)),
            now=_NOW,
        )
    assert report.ledger_load_failed is True
    assert report.note.startswith(
        "window ledger reload FAILED — running full re-verify pull (no skip); "
    )
    assert report.to_dict()["ledger_load_failed"] is True
    assert any(e["event"] == "historical.pull.ledger_reload_failed" for e in logs)
    # The pull itself still ran (full re-verify — the vendor WAS hit, never a crash).
    assert len(sdk.candle_symbols) > 0
    assert report.contracts_completed == 3


class TestMassEmptyHaltVerdictFnoRoute:
    """DEC-EMPTY-WINDOW-CIRCUIT-BREAKER-001 minor M-5 — the F&O bulk route's polished halt."""

    def test_halt_produces_polished_verdict_and_fires_alert(
        self, registry: RegistryStore, tmp_lake: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # An all-empty vendor (the R2 throttle-from-START shape) with an env-injected
        # trip-at-the-2nd-observation FRACTION breaker: the FUT bucket's single window
        # settles (its per-bucket breaker sees only 1 observation), then the options
        # bucket's 2nd empty TRIPS. The halt is CAUGHT inside `_run_phase` and folded
        # into the POLISHED mass_empty_halt verdict (the spot route's twin) instead of
        # escaping as a generic job error — and the CRITICAL Telegram pull-halt alert
        # fires from the catch site (M-3 parity).
        monkeypatch.setenv("BRUTEX_GROWW_FETCH_PIPELINE", "off")  # synchronous tails
        monkeypatch.setenv("BRUTEX_EMPTY_BREAKER_CONSECUTIVE", "0")
        monkeypatch.setenv("BRUTEX_EMPTY_BREAKER_MIN_OBSERVATIONS", "2")
        monkeypatch.setenv("BRUTEX_EMPTY_BREAKER_FRACTION", "1.0")
        monkeypatch.setenv("BRUTEX_EMPTY_BREAKER_FRACTION_MIN_SERIES", "1")
        alerts: list[Exception] = []
        monkeypatch.setattr("brutex.dashboard.historical_pull.emit_pull_halt_alert", alerts.append)
        sdk = _EmptyCandleSDK()
        sink = _RecordingSink()
        selected = registry.list_contracts(underlying="NIFTY")  # FUT + CE + PE
        report = pull_contract_candles(
            selected,
            lake_root=tmp_lake,
            dry_run=False,
            intervals=("1day",),
            registry=registry,
            from_date=_FROM,
            to_date=_TO,
            client_factory=lambda _root: _factory(cast("_FakeSDK", sdk), sink),
            now=_NOW,
        )
        assert report.mass_empty_cause is not None
        verdict = report.verdict
        assert verdict.kind == "mass_empty_halt"
        assert verdict.needs_operator is True
        # The halt's count-naming message is relayed verbatim into the action…
        assert verdict.action.startswith("Mass-empty circuit breaker TRIPPED")
        assert "reopen-skipped" in verdict.action
        # …and the settles that landed BEFORE the trip (the FUT window + the first
        # option window — old + confirmed-empty → fast-settled) are counted + named.
        assert report.settled_skipped == 2
        assert "2 window(s)" in verdict.action
        # The one-line note names the halt; the route JSON carries the full verdict.
        assert "mass-empty circuit breaker HALTED" in report.note
        assert report.to_dict()["verdict"]["kind"] == "mass_empty_halt"  # type: ignore[index]
        # A halted phase is never the clean barrier.
        assert report.phase1_clean is False
        # M-3 parity: exactly one CRITICAL pull-halt alert from the catch site.
        assert len(alerts) == 1
        from brutex.adapters.empty_circuit_breaker import MassEmptySettleHalt

        assert isinstance(alerts[0], MassEmptySettleHalt)

    def test_clean_pull_reports_clean_verdict(
        self, registry: RegistryStore, tmp_lake: Path
    ) -> None:
        # The polished verdict rides EVERY F&O bulk report, not only halts: a clean
        # LIVE-shaped pull reads `clean` with no operator flag.
        sdk = _FakeSDK()
        sink = _RecordingSink()
        selected = registry.list_contracts(underlying="NIFTY")
        report = pull_contract_candles(
            selected,
            lake_root=tmp_lake,
            dry_run=False,
            intervals=("1day",),
            registry=registry,
            from_date=_FROM,
            to_date=_TO,
            client_factory=lambda _root: _factory(sdk, sink),
            now=_NOW,
        )
        assert report.mass_empty_cause is None
        assert report.verdict.kind == "clean"
        assert report.verdict.needs_operator is False
        assert report.to_dict()["mass_empty_cause"] is None


# ── DEC-FNO-EXPIRY-SCOPE-001: expiry-level date scoping ───────────────────────────────


def _seed_feb_weeklies(registry: RegistryStore) -> dict[date, str]:
    """Seed a NIFTY February 2024 weekly + monthly CE calendar; return expiry → symbol."""
    seeded: dict[date, str] = {}
    for expiry in (
        date(2024, 2, 1),
        date(2024, 2, 8),
        date(2024, 2, 15),
        date(2024, 2, 22),
        date(2024, 2, 29),  # the monthly
    ):
        symbol = f"NSE-NIFTY-{expiry.strftime('%d%b%y')}-22000-CE"
        registry.upsert_contract(
            "NSE",
            "NIFTY",
            expiry,
            symbol,
            side="CE",
            strike=22000.0,
            run_id=_RUN_ID,
            fetched_at=_FETCHED,
        )
        seeded[expiry] = symbol
    return seeded


class TestExpiryScope:
    """DEC-FNO-EXPIRY-SCOPE-001 — the pull-level expiry-scoping permutations."""

    def test_scope_expiry_equal_to_end_pulled_with_default_window_law(
        self, registry: RegistryStore
    ) -> None:
        """Permutation (a): ``expiry == to_date`` is IN — and keeps the 30d/60d window law.

        The NIFTY chain (expiry 2024-01-25 == scope_end) is pulled with its NORMAL
        per-type default window (CE/PE ``expiry − 30d``, FUT ``expiry − 60d`` — the
        scope selects WHICH expiries, it never reshapes windows); the RELIANCE
        2024-02-29 expiry is out.
        """
        sdk = _FakeSDK()
        sink = _RecordingSink()
        selected = registry.list_contracts()  # 3 NIFTY legs + 1 RELIANCE leg
        report = pull_contract_candles(
            selected,
            lake_root=Path("/unused"),
            dry_run=True,
            intervals=("1minute",),
            registry=registry,
            client_factory=lambda _root: _factory(sdk, sink),
            now=_NOW,
            scope_start=date(2024, 1, 1),
            scope_end=date(2024, 1, 25),  # == the NIFTY chain's expiry (inclusive IN)
        )
        nifty = {r.contract_symbol for r in selected if r.underlying == "NIFTY"}
        assert set(sdk.candle_symbols) == nifty  # RELIANCE never fetched
        # The WINDOW LAW is untouched by the scope: expiry−30d for the option,
        # expiry−60d for the future (never a scope-reshaped window).
        assert report.start_dates["NSE-NIFTY-25Jan24-21000-CE"] == "2023-12-26"
        assert report.start_dates["NSE-NIFTY-25Jan24-FUT"] == "2023-11-26"
        assert report.contracts_selected == 3
        assert report.expiries_out_of_scope == 1
        assert report.contracts_out_of_scope == 1
        assert ", 1 expiries out of scope skipped" in report.note

    def test_scope_expiry_one_day_past_end_zero_calls_zero_ledger_one_log(
        self, registry: RegistryStore, tmp_lake: Path
    ) -> None:
        """Permutation (b): ``expiry == to_date + 1 day`` is OUT — fetched not at all.

        LIVE-shaped pull: the RELIANCE 2024-02-29 expiry (scope_end 2024-02-28 + 1
        day) costs ZERO vendor calls, leaves ZERO window-ledger rows (nothing
        PLANNED — a stray row would make every later pull re-attempt it), and is
        logged EXACTLY once as ``fno.expiry_out_of_scope``.
        """
        from brutex.data.window_ledger_store import load_window_ledger

        sdk = _FakeSDK()
        sink = _RecordingSink()
        selected = registry.list_contracts()
        with capture_logs() as logs:
            report = pull_contract_candles(
                selected,
                lake_root=tmp_lake,
                dry_run=False,
                intervals=("1minute",),
                registry=registry,
                client_factory=lambda _root: _factory(sdk, sink),
                now=_NOW,
                scope_end=date(2024, 2, 28),  # RELIANCE expiry 2024-02-29 == to+1 → OUT
            )
        reliance = "NSE-RELIANCE-29Feb24-2800-CE"
        assert reliance not in sdk.candle_symbols  # zero vendor calls
        nifty = {r.contract_symbol for r in selected if r.underlying == "NIFTY"}
        assert set(sdk.candle_symbols) == nifty
        ledger_symbols = {rec.security_id for rec in load_window_ledger(tmp_lake).records()}
        assert reliance not in ledger_symbols  # zero ledger rows (no stray PLANNED)
        assert nifty <= ledger_symbols
        events = [e for e in logs if e["event"] == "fno.expiry_out_of_scope"]
        assert len(events) == 1  # exactly ONE honest line for the one expiry group
        assert events[0]["underlying"] == "RELIANCE"
        assert events[0]["expiry"] == "2024-02-29"
        assert events[0]["scope_start"] is None
        assert events[0]["scope_end"] == "2024-02-28"
        assert events[0]["contracts_skipped"] == 1
        assert report.expiries_out_of_scope == 1
        assert report.contracts_out_of_scope == 1
        assert report.to_dict()["expiries_out_of_scope"] == 1
        assert report.to_dict()["contracts_out_of_scope"] == 1

    def test_scope_weekly_monthly_mix_pulls_exactly_the_in_range_subset(
        self, registry: RegistryStore
    ) -> None:
        """Permutation (c): weeklies + the monthly in one month, scope a subset.

        February 2024 carries 4 weeklies + the 29-Feb monthly; the
        ``[2024-02-05, 2024-02-16]`` scope pulls EXACTLY the 08-Feb + 15-Feb
        weeklies. Out groups: the Jan-25 base chain + 01-Feb + 22-Feb + 29-Feb.
        """
        seeded = _seed_feb_weeklies(registry)
        sdk = _FakeSDK()
        sink = _RecordingSink()
        selected = registry.list_contracts(underlying="NIFTY")  # 3 Jan legs + 5 Feb legs
        report = pull_contract_candles(
            selected,
            lake_root=Path("/unused"),
            dry_run=True,
            intervals=("1minute",),
            registry=registry,
            client_factory=lambda _root: _factory(sdk, sink),
            now=_NOW,
            scope_start=date(2024, 2, 5),
            scope_end=date(2024, 2, 16),
        )
        expected = {seeded[date(2024, 2, 8)], seeded[date(2024, 2, 15)]}
        assert set(sdk.candle_symbols) == expected  # exactly the in-range subset
        assert report.contracts_selected == 2
        assert report.expiries_out_of_scope == 4  # Jan-25 chain + Feb 01/22/29
        assert report.contracts_out_of_scope == 6  # 3 Jan legs + 3 Feb legs

    def test_scope_single_expiry_selection_pulls_exactly_that_expiry(
        self, registry: RegistryStore
    ) -> None:
        """Permutation (f): ``from == to == expiry`` selects exactly that expiry's chain."""
        sdk = _FakeSDK()
        sink = _RecordingSink()
        selected = registry.list_contracts()
        report = pull_contract_candles(
            selected,
            lake_root=Path("/unused"),
            dry_run=True,
            intervals=("1minute",),
            registry=registry,
            client_factory=lambda _root: _factory(sdk, sink),
            now=_NOW,
            scope_start=date(2024, 1, 25),
            scope_end=date(2024, 1, 25),
        )
        nifty = {r.contract_symbol for r in selected if r.underlying == "NIFTY"}
        assert set(sdk.candle_symbols) == nifty  # the 2024-01-25 chain, nothing else
        assert report.contracts_selected == 3
        assert report.expiries_out_of_scope == 1  # RELIANCE 2024-02-29

    def test_scope_start_after_all_expiries_clean_noop_with_honest_note(
        self, registry: RegistryStore, tmp_lake: Path
    ) -> None:
        """Permutation (g): a scope starting AFTER every expiry pulls NOTHING, cleanly.

        Zero vendor calls, zero ledger rows, an HONEST all-out-of-scope note
        (never the misleading "no contracts selected — pull expiries + contracts
        first"), and the report carries NO failure signal (the pipeline gate
        still reads it ok).
        """
        from brutex.data.window_ledger_store import load_window_ledger

        sdk = _FakeSDK()
        sink = _RecordingSink()
        selected = registry.list_contracts()
        report = pull_contract_candles(
            selected,
            lake_root=tmp_lake,
            dry_run=False,
            intervals=("1minute",),
            registry=registry,
            client_factory=lambda _root: _factory(sdk, sink),
            now=_NOW,
            scope_start=date(2030, 1, 1),
        )
        assert sdk.candle_symbols == []  # zero vendor calls
        assert sink.appends == []  # nothing written
        assert load_window_ledger(tmp_lake).records() == []  # zero ledger rows
        assert report.contracts_selected == 0
        assert report.contracts_out_of_scope == 4
        assert report.expiries_out_of_scope == 2  # (NIFTY, Jan-25) + (RELIANCE, Feb-29)
        # No failure signal — the pull is a clean, honest no-op.
        assert report.failed_pairs == 0
        assert report.still_failed_windows == 0
        assert report.auth_aborted is False
        assert report.note == (
            "all 4 selected contract(s) across 2 expiry(ies) fall outside the "
            "requested expiry scope — nothing pulled (already-stored bars untouched)"
        )

    def test_default_scope_plans_byte_identical_windows(
        self, registry: RegistryStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Permutation (h): no dates ⇒ ALL contracts in scope, SAME windows as before.

        A spy captures every ``run_groww_option_ingest`` call's contracts +
        ``contract_windows``: with the default (unbounded) scope the union of
        ingested contracts is the WHOLE selection and the per-contract window map
        equals the pre-change ``_resolve_minute_windows`` law exactly.
        """
        captured: list[tuple[frozenset[str], dict[str, tuple[date, date]] | None]] = []

        def _spy_ingest(
            *_args: object,
            contracts: Sequence[DerivativeContract],
            contract_windows: dict[str, tuple[date, date]] | None = None,
            **_kw: object,
        ) -> GrowwOptionIngestReport:
            captured.append((frozenset(c.symbol for c in contracts), contract_windows))
            return _empty_ingest_report()

        monkeypatch.setattr(hp, "run_groww_option_ingest", _spy_ingest)
        sdk = _FakeSDK()
        sink = _RecordingSink()
        selected = registry.list_contracts()
        report = pull_contract_candles(
            selected,
            lake_root=Path("/unused"),
            dry_run=True,
            intervals=("1minute",),
            registry=registry,
            client_factory=lambda _root: _factory(sdk, sink),
            now=_NOW,
        )
        # The union of ingested contracts == the WHOLE selection (nothing scoped out).
        ingested = frozenset().union(*(symbols for symbols, _ in captured))
        assert ingested == {r.contract_symbol for r in selected}
        # The planned windows == the pre-change default law, byte-identical
        # (envelope [2020-01-01, prev-day+1) resolved from the injected _NOW).
        expected = hp._resolve_minute_windows(
            rebuilt=hp._rebuild_contracts(selected),
            explicit_dates=False,
            start=date(2020, 1, 1),
            end=date(2026, 6, 14),
        )
        for _symbols, windows in captured:
            assert windows == expected
        assert report.expiries_out_of_scope == 0
        assert report.contracts_out_of_scope == 0
        assert "out of scope" not in report.note

    def test_out_of_scope_expiry_already_stored_bars_survive(
        self, registry: RegistryStore, tmp_lake: Path
    ) -> None:
        """Permutation (j): scoping OUT an expiry NEVER deletes its stored lake bars.

        Pull 1 (unscoped) lands real RELIANCE 1-minute bars on a REAL
        ``ParquetBarSink``; pull 2 scopes the RELIANCE expiry OUT — after it, the
        RELIANCE partition files still exist byte-for-byte and were never
        re-fetched.
        """
        import hashlib

        from brutex.data.parquet_writer import ParquetWriter
        from brutex.data.sink import ParquetBarSink

        sink = ParquetBarSink(writer=ParquetWriter(lake_root=tmp_lake))
        reliance_rows = list(registry.list_contracts(underlying="RELIANCE"))
        seed_sdk = _FakeSDK()
        pull_contract_candles(
            reliance_rows,
            lake_root=tmp_lake,
            dry_run=False,
            intervals=("1minute",),
            registry=registry,
            from_date=_FROM,
            to_date=_TO,
            client_factory=lambda _root: _factory(seed_sdk, sink),
            now=_NOW,
        )
        stored = sorted(tmp_lake.glob("bars/*/FNO/NSE-RELIANCE-29Feb24-2800-CE/**/*.parquet"))
        assert stored, "the seed pull must land real RELIANCE bars"
        before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in stored}

        scoped_sdk = _FakeSDK()
        pull_contract_candles(
            registry.list_contracts(),
            lake_root=tmp_lake,
            dry_run=False,
            intervals=("1minute",),
            registry=registry,
            client_factory=lambda _root: _factory(scoped_sdk, sink),
            now=_NOW,
            scope_start=date(2024, 1, 1),
            scope_end=date(2024, 1, 31),  # RELIANCE 2024-02-29 OUT
        )
        assert "NSE-RELIANCE-29Feb24-2800-CE" not in scoped_sdk.candle_symbols
        after = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in stored}
        assert after == before, "an out-of-scope expiry's stored bars must never change"


def _write_overlay_sidecar(lake_root: Path, day: str = "2026-05-12") -> frozenset[date]:
    """Write a minimal valid observed-sessions overlay sidecar under *lake_root*."""
    import json

    from brutex.calendars.observed_sessions import (
        OBSERVED_LABEL,
        OBSERVED_OVERLAY_VERSION,
        overlay_path,
    )

    path = overlay_path(lake_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": OBSERVED_OVERLAY_VERSION,
                "entries": [
                    {
                        "date": day,
                        "detected_at": "2026-07-04T00:00:00+00:00",
                        "month": day[:7],
                        "label": OBSERVED_LABEL,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return frozenset({date.fromisoformat(day)})


def test_live_pull_threads_observed_holidays(
    registry: RegistryStore, monkeypatch: pytest.MonkeyPatch, tmp_lake: Path
) -> None:
    """DEC-CALENDAR-OBSERVED-SESSIONS-001: the LIVE F&O pull loads the overlay
    sidecar ONCE and threads it into ``run_groww_option_ingest`` (whose futures
    partial-window observe consumes it — margin arithmetic only, never planning)."""
    expected = _write_overlay_sidecar(tmp_lake)
    seen: list[object] = []

    def _spy_ingest(
        *_args: object, observed_holidays: object = frozenset(), **_kw: object
    ) -> GrowwOptionIngestReport:
        seen.append(observed_holidays)
        return _empty_ingest_report()

    monkeypatch.setattr(hp, "run_groww_option_ingest", _spy_ingest)
    sdk = _FakeSDK()
    sink = _RecordingSink()
    selected = registry.list_contracts(underlying="NIFTY")
    pull_contract_candles(
        selected,
        lake_root=tmp_lake,
        dry_run=False,
        intervals=("1day",),
        registry=registry,
        from_date=_FROM,
        to_date=_TO,
        client_factory=lambda _root: _factory(sdk, sink),
        now=_NOW,
    )
    assert seen
    assert all(observed == expected for observed in seen)


def test_dry_run_pull_threads_empty_observed_holidays(
    registry: RegistryStore, monkeypatch: pytest.MonkeyPatch, tmp_lake: Path
) -> None:
    """A dry-run threads the EMPTY overlay even when a sidecar exists on disk."""
    _write_overlay_sidecar(tmp_lake)
    seen: list[object] = []

    def _spy_ingest(
        *_args: object, observed_holidays: object = frozenset(), **_kw: object
    ) -> GrowwOptionIngestReport:
        seen.append(observed_holidays)
        return _empty_ingest_report()

    monkeypatch.setattr(hp, "run_groww_option_ingest", _spy_ingest)
    sdk = _FakeSDK()
    sink = _RecordingSink()
    selected = registry.list_contracts(underlying="NIFTY")
    pull_contract_candles(
        selected,
        lake_root=tmp_lake,
        dry_run=True,
        intervals=("1day",),
        registry=registry,
        from_date=_FROM,
        to_date=_TO,
        client_factory=lambda _root: _factory(sdk, sink),
        now=_NOW,
    )
    assert seen
    assert all(observed == frozenset() for observed in seen)
