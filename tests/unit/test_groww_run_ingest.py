"""Tests for ``brutex.adapters.groww.run_ingest`` — multi-instrument fan-out.

100% line+branch via a monkeypatched ``ingest_spot``: success fan-out across
instruments × intervals, terminal-error quarantine (run continues), the
aggregate report + its derived ``auth_aborted``, pace threading + the
``_with_paced_client`` wiring, the empty-universe case, ledger / hook threading,
on_progress, logging, the audit chain, and the post-run auto-retry wrapper.
"""

# Whitebox test of the module-private `_with_paced_client`.
# pyright: reportPrivateUsage=none

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal

import pytest
from brutex.adapters.groww import run_ingest as mod
from brutex.adapters.groww.errors import (
    GrowwAuthError,
    GrowwBadRequestError,
    GrowwNotFoundError,
)
from brutex.adapters.groww.ingest import IngestReport
from brutex.adapters.groww.run_ingest import (
    GrowwIngestReport,
    PairFailure,
    SpotIngestWithRetry,
    run_groww_spot_ingest,
    run_groww_spot_ingest_with_retry,
)
from brutex.data.candle_fields import CandleParseError
from brutex.data.errors import LakeWriteHalt
from brutex.models import (
    BruteXError,
    Exchange,
    Instrument,
    InstrumentType,
    OutOfScopeError,
    Segment,
)
from brutex.observability.correlation import pull_id_scope
from brutex.orchestrator.quota_tracker import DailyQuotaExceeded, QuotaInvariantError
from structlog.testing import capture_logs

_START = date(2021, 1, 1)
_END = date(2021, 2, 1)


def _index(symbol: str, *, token: int = 1) -> Instrument:
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=token,
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


def _events(logs: Sequence[Mapping[str, object]], name: str) -> list[Mapping[str, object]]:
    """All captured events with ``event == name`` (in emission order)."""
    return [e for e in logs if e["event"] == name]


def test_fan_out_all_pairs_succeed(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str]] = []

    def _fake_ingest(adapter, sink, *, instrument, interval, start, end, **_kw):
        calls.append((instrument.symbol_name, interval))
        return IngestReport(windows=2, bars_appended=10, transient_failures=1)

    monkeypatch.setattr(mod, "ingest_spot", _fake_ingest)
    report = run_groww_spot_ingest(
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        instruments=[_index("NIFTY"), _index("BANKNIFTY")],
        intervals=["1minute", "1day"],
        start=_START,
        end=_END,
    )
    assert len(report.pairs) == 4
    assert report.failed_pairs == 0
    assert report.windows == 8
    assert report.bars_appended == 40
    assert report.transient_failures == 4
    assert report.auth_aborted is False
    assert set(calls) == {
        ("NIFTY", "1minute"),
        ("NIFTY", "1day"),
        ("BANKNIFTY", "1minute"),
        ("BANKNIFTY", "1day"),
    }


def test_terminal_error_quarantines_pair_and_continues(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_ingest(adapter, sink, *, instrument, interval, start, end, **_kw):
        if instrument.symbol_name == "BAD":
            raise GrowwNotFoundError("unknown security")
        return IngestReport(windows=1, bars_appended=5, transient_failures=0)

    monkeypatch.setattr(mod, "ingest_spot", _fake_ingest)
    report = run_groww_spot_ingest(
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        instruments=[_index("NIFTY"), _index("BAD", token=99)],
        intervals=["1minute"],
        start=_START,
        end=_END,
    )
    assert report.failed_pairs == 1
    assert report.failures == (
        PairFailure(symbol="BAD", interval="1minute", error="unknown security", auth_failure=False),
    )
    assert report.auth_aborted is False
    assert ("NIFTY", "1minute") in report.pairs
    assert report.bars_appended == 5


def test_auth_failure_sets_derived_auth_aborted(monkeypatch: pytest.MonkeyPatch) -> None:
    # Groww divergence: no fail-fast latch — auth_aborted is DERIVED from the
    # quarantined failures' captured auth_failure flag.
    def _fake_ingest(adapter, sink, *, instrument, interval, start, end, **_kw):
        raise GrowwAuthError("token expired")

    monkeypatch.setattr(mod, "ingest_spot", _fake_ingest)
    with capture_logs() as logs:
        report = run_groww_spot_ingest(
            object(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            instruments=[_index("NIFTY")],
            intervals=["1minute"],
            start=_START,
            end=_END,
        )
    assert report.failed_pairs == 1
    assert report.failures[0].auth_failure is True
    assert report.auth_aborted is True
    # A Groww auth failure quarantines like any terminal error (no special alert).
    (quarantine,) = _events(logs, "ingest.quarantine")
    assert quarantine["error"] == "token expired"


def test_pace_threaded_through(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[object] = []

    def _fake_ingest(adapter, sink, *, instrument, interval, start, end, pace=None, **_kw):
        seen.append(pace)
        return IngestReport(windows=0, bars_appended=0, transient_failures=0)

    monkeypatch.setattr(mod, "ingest_spot", _fake_ingest)
    sentinel = object()
    run_groww_spot_ingest(
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        instruments=[_index("NIFTY")],
        intervals=["1minute"],
        start=_START,
        end=_END,
        pace=sentinel,  # type: ignore[arg-type]
    )
    assert seen == [sentinel]


def test_bulk_pull_runs_each_symbols_windows_serially(monkeypatch: pytest.MonkeyPatch) -> None:
    # DEC-SPOT-CROSS-SYMBOL-FANOUT-001: a MULTI-pair pull fans the OUTER pool
    # across symbols and hands each symbol's windows a SERIAL inner runner, so the
    # nested per-symbol window pool (the 720-thread explosion) is gone.
    from brutex.orchestrator.parallel import SerialRunParallel

    seen: list[object] = []

    def _fake_ingest(adapter, sink, *, instrument, interval, start, end, run_parallel=None, **_kw):
        seen.append(run_parallel)
        return IngestReport(windows=1, bars_appended=1, transient_failures=0)

    monkeypatch.setattr(mod, "ingest_spot", _fake_ingest)
    outer = SerialRunParallel()  # stand-in outer runner; must NOT leak into pairs
    run_groww_spot_ingest(
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        instruments=[_index("NIFTY"), _index("BANKNIFTY")],
        intervals=["1day"],
        start=_START,
        end=_END,
        run_parallel=outer,
    )
    assert len(seen) == 2
    assert all(isinstance(r, SerialRunParallel) for r in seen)
    assert all(r is not outer for r in seen)  # a fresh serial inner runner, not the outer


def test_single_pair_pull_hands_its_windows_the_outer_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A SINGLE-pair pull has no symbol-level fan-out to spend the pool on, so the
    # one pair's WINDOWS get the full outer runner (single-symbol window parallelism
    # preserved).
    from brutex.orchestrator.parallel import SerialRunParallel

    seen: list[object] = []

    def _fake_ingest(adapter, sink, *, instrument, interval, start, end, run_parallel=None, **_kw):
        seen.append(run_parallel)
        return IngestReport(windows=1, bars_appended=1, transient_failures=0)

    monkeypatch.setattr(mod, "ingest_spot", _fake_ingest)
    outer = SerialRunParallel()
    run_groww_spot_ingest(
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        instruments=[_index("NIFTY")],
        intervals=["1day"],
        start=_START,
        end=_END,
        run_parallel=outer,
    )
    assert seen == [outer]  # the single pair gets the outer pool for its windows


def test_single_pair_default_runner_is_a_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    # When no outer runner is injected, a single-pair pull falls back to the
    # default bounded pool for its windows (covers the ``len(jobs) <= 1`` arm with
    # ``run_parallel=None``).
    from brutex.orchestrator.parallel import PoolRunParallel

    seen: list[object] = []

    def _fake_ingest(adapter, sink, *, instrument, interval, start, end, run_parallel=None, **_kw):
        seen.append(run_parallel)
        return IngestReport(windows=1, bars_appended=1, transient_failures=0)

    monkeypatch.setattr(mod, "ingest_spot", _fake_ingest)
    run_groww_spot_ingest(
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        instruments=[_index("NIFTY")],
        intervals=["1day"],
        start=_START,
        end=_END,
    )
    assert len(seen) == 1
    assert isinstance(seen[0], PoolRunParallel)


def test_with_paced_client_three_branches() -> None:
    """`_with_paced_client` covers pace None, non-Groww adapter, live Groww adapter."""
    from brutex.adapters.groww.adapter import GrowwMarketAdapter
    from brutex.adapters.groww.client import GrowwClient
    from brutex.adapters.groww.instruments import InstrumentIndex

    class _FakeSDK:
        def get_historical_candles(self, **_kwargs: object) -> Mapping[str, object]:
            return {}

        def get_expiries(self, **_kwargs: object) -> Mapping[str, object]:
            return {}

        def get_contracts(self, **_kwargs: object) -> Mapping[str, object]:
            return {}

    def _pace() -> None:
        pass

    index = InstrumentIndex(by_groww_symbol={}, by_exchange_symbol={}, universe=())
    client = GrowwClient(sdk=_FakeSDK())  # type: ignore[arg-type]
    adapter = GrowwMarketAdapter(client=client, index=index)

    paced = mod._with_paced_client(adapter, _pace)
    assert paced is not adapter
    # `adapter.client` is now typed GrowwClientProtocol (accepts a single client OR a pool),
    # which exposes `with_pace` not the private `pace` attribute — narrow to the concrete
    # GrowwClient to assert the pace was actually rebound (DEC-GROWW-CLIENT-POOL-001).
    assert isinstance(paced, GrowwMarketAdapter)
    assert isinstance(paced.client, GrowwClient) and paced.client.pace is _pace

    assert mod._with_paced_client(adapter, None) is adapter  # pace None → unchanged
    sentinel = object()
    assert mod._with_paced_client(sentinel, _pace) is sentinel  # type: ignore[arg-type]


def test_non_groww_exception_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_ingest(adapter, sink, *, instrument, interval, start, end, **_kw):
        raise ValueError("a real bug, not a vendor error")

    monkeypatch.setattr(mod, "ingest_spot", _fake_ingest)
    with pytest.raises(ValueError, match="a real bug"):
        run_groww_spot_ingest(
            object(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            instruments=[_index("NIFTY")],
            intervals=["1minute"],
            start=_START,
            end=_END,
        )


def test_empty_universe_yields_empty_report() -> None:
    report = run_groww_spot_ingest(
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        instruments=[],
        intervals=["1minute"],
        start=_START,
        end=_END,
    )
    assert report == GrowwIngestReport(pairs={}, failures=())
    assert report.windows == 0
    assert report.bars_appended == 0
    assert report.transient_failures == 0
    assert report.skipped_windows == 0
    assert report.retried_windows == 0
    assert report.still_failed_windows == 0
    assert report.failed_pairs == 0
    assert report.auth_aborted is False


def test_ledger_threaded_and_skipped_windows_aggregated(monkeypatch: pytest.MonkeyPatch) -> None:
    from brutex.data.window_ledger import WindowLedger

    seen_ledger: list[object] = []

    def _fake_ingest(adapter, sink, *, instrument, interval, start, end, ledger=None, **_kw):
        seen_ledger.append(ledger)
        return IngestReport(windows=3, bars_appended=0, transient_failures=0, skipped_windows=3)

    monkeypatch.setattr(mod, "ingest_spot", _fake_ingest)
    led = WindowLedger()
    report = run_groww_spot_ingest(
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        instruments=[_index("NIFTY")],
        intervals=["1minute", "1day"],
        start=_START,
        end=_END,
        ledger=led,
    )
    assert seen_ledger == [led, led]
    assert report.skipped_windows == 6
    assert report.bars_appended == 0


def test_write_pool_wraps_sink_in_write_consumer_sink(monkeypatch: pytest.MonkeyPatch) -> None:
    """DEC-FETCH-WRITE-OVERLAP-LEVER1-001: a non-None write_pool wraps the sink.

    The spine routes every pair's write through a WriteConsumerSink so the lake write is
    handed off to the background pool, while the settle-after-durable ordering is preserved
    by the wrapper (it blocks on the durable result). Asserts the wrapped sink — NOT the raw
    sink — is what reaches ``ingest_spot``.
    """
    from brutex.data.sink import NullBarSink
    from brutex.data.write_consumer import ShardedWriteConsumer, WriteConsumerSink

    seen_sinks: list[object] = []

    def _fake_ingest(adapter, sink, *, instrument, interval, start, end, **_kw):
        seen_sinks.append(sink)
        return IngestReport(windows=1, bars_appended=5, transient_failures=0)

    monkeypatch.setattr(mod, "ingest_spot", _fake_ingest)
    raw_sink = NullBarSink()
    pool = ShardedWriteConsumer(sink=raw_sink, workers=2)
    try:
        run_groww_spot_ingest(
            object(),  # type: ignore[arg-type]
            raw_sink,
            instruments=[_index("NIFTY")],
            intervals=["1minute"],
            start=_START,
            end=_END,
            write_pool=pool,
        )
    finally:
        pool.drain()
    assert len(seen_sinks) == 1
    assert isinstance(seen_sinks[0], WriteConsumerSink)


def test_write_pool_none_leaves_sink_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    """write_pool=None (the default) is byte-identical: the RAW sink reaches ingest_spot."""
    from brutex.data.sink import NullBarSink

    seen_sinks: list[object] = []

    def _fake_ingest(adapter, sink, *, instrument, interval, start, end, **_kw):
        seen_sinks.append(sink)
        return IngestReport(windows=1, bars_appended=5, transient_failures=0)

    monkeypatch.setattr(mod, "ingest_spot", _fake_ingest)
    raw_sink = NullBarSink()
    run_groww_spot_ingest(
        object(),  # type: ignore[arg-type]
        raw_sink,
        instruments=[_index("NIFTY")],
        intervals=["1minute"],
        start=_START,
        end=_END,
    )
    assert seen_sinks == [raw_sink]


def test_with_retry_forwards_write_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    """The retry wrapper forwards write_pool to every pass (the wrapped sink reaches ingest)."""
    from brutex.data.sink import NullBarSink
    from brutex.data.write_consumer import ShardedWriteConsumer, WriteConsumerSink

    seen_sinks: list[object] = []

    def _fake_ingest(adapter, sink, *, instrument, interval, start, end, **_kw):
        seen_sinks.append(sink)
        return IngestReport(windows=1, bars_appended=5, transient_failures=0)

    monkeypatch.setattr(mod, "ingest_spot", _fake_ingest)
    raw_sink = NullBarSink()
    pool = ShardedWriteConsumer(sink=raw_sink, workers=2)
    try:
        result = run_groww_spot_ingest_with_retry(
            object(),  # type: ignore[arg-type]
            raw_sink,
            instruments=[_index("NIFTY")],
            intervals=["1minute"],
            start=_START,
            end=_END,
            write_pool=pool,
        )
    finally:
        pool.drain()
    assert result.retry is None
    assert all(isinstance(s, WriteConsumerSink) for s in seen_sinks)


def test_rate_feedback_hooks_threaded(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[object, object, object]] = []

    def _fake_ingest(
        adapter,
        sink,
        *,
        instrument,
        interval,
        start,
        end,
        on_rate_limit=None,
        on_timeout=None,
        on_success=None,
        **_kw,
    ):
        seen.append((on_rate_limit, on_timeout, on_success))
        return IngestReport(windows=1, bars_appended=1, transient_failures=0)

    monkeypatch.setattr(mod, "ingest_spot", _fake_ingest)
    rl, to, ok = object(), object(), object()
    run_groww_spot_ingest(
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        instruments=[_index("NIFTY")],
        intervals=["1minute", "1day"],
        start=_START,
        end=_END,
        on_rate_limit=rl,  # type: ignore[arg-type]
        on_timeout=to,  # type: ignore[arg-type]
        on_success=ok,  # type: ignore[arg-type]
    )
    assert seen == [(rl, to, ok), (rl, to, ok)]


def test_on_progress_fires_per_pair_success_and_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_ingest(adapter, sink, *, instrument, interval, start, end, **_kw):
        if instrument.symbol_name == "BAD":
            raise GrowwNotFoundError("gone")
        return IngestReport(windows=1, bars_appended=7, transient_failures=0)

    monkeypatch.setattr(mod, "ingest_spot", _fake_ingest)
    events: list[tuple[str, int, bool]] = []
    run_groww_spot_ingest(
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        instruments=[_index("NIFTY"), _index("BAD")],
        intervals=["1minute"],
        start=_START,
        end=_END,
        on_progress=lambda label, bars, failed: events.append((label, bars, failed)),
    )
    assert set(events) == {
        ("NIFTY 1minute", 7, False),
        ("BAD 1minute", 0, True),
    }


class TestRunIngestLogging:
    def test_underlying_rollup_per_instrument(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _fake_ingest(adapter, sink, *, instrument, interval, start, end, **_kw):
            return IngestReport(windows=2, bars_appended=10, transient_failures=0)

        monkeypatch.setattr(mod, "ingest_spot", _fake_ingest)
        with capture_logs() as logs, pull_id_scope("runpull00001"):
            run_groww_spot_ingest(
                object(),  # type: ignore[arg-type]
                object(),  # type: ignore[arg-type]
                instruments=[_index("NIFTY"), _index("BANKNIFTY")],
                intervals=["1minute", "1day"],
                start=_START,
                end=_END,
            )
        rollups = _events(logs, "ingest.underlying")
        assert len(rollups) == 2
        first = rollups[0]
        assert first["log_level"] == "info"
        assert first["pull_id"] == "runpull00001"
        assert first["series_completed"] == 2
        assert first["series_failed"] == 0
        assert first["bars_appended"] == 20
        assert first["windows"] == 4
        assert _events(logs, "ingest.quarantine") == []

    def test_quarantine_event_names_the_cause(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _fake_ingest(adapter, sink, *, instrument, interval, start, end, **_kw):
            if instrument.symbol_name == "BAD":
                raise GrowwNotFoundError("unknown security")
            return IngestReport(windows=1, bars_appended=5, transient_failures=0)

        monkeypatch.setattr(mod, "ingest_spot", _fake_ingest)
        with capture_logs() as logs, pull_id_scope("runpull00002"):
            run_groww_spot_ingest(
                object(),  # type: ignore[arg-type]
                object(),  # type: ignore[arg-type]
                instruments=[_index("NIFTY"), _index("BAD", token=99)],
                intervals=["1minute"],
                start=_START,
                end=_END,
            )
        (quarantine,) = _events(logs, "ingest.quarantine")
        assert quarantine["log_level"] == "warning"
        assert quarantine["pull_id"] == "runpull00002"
        assert quarantine["symbol"] == "BAD"
        assert quarantine["error"] == "unknown security"
        bad_rollup = [e for e in _events(logs, "ingest.underlying") if e["symbol"] == "BAD"]
        assert bad_rollup[0]["series_failed"] == 1
        assert bad_rollup[0]["series_completed"] == 0

    def test_events_carry_empty_pull_id_outside_a_scope(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _fake_ingest(adapter, sink, *, instrument, interval, start, end, **_kw):
            return IngestReport(windows=1, bars_appended=1, transient_failures=0)

        monkeypatch.setattr(mod, "ingest_spot", _fake_ingest)
        with capture_logs() as logs:
            run_groww_spot_ingest(
                object(),  # type: ignore[arg-type]
                object(),  # type: ignore[arg-type]
                instruments=[_index("NIFTY")],
                intervals=["1minute"],
                start=_START,
                end=_END,
            )
        assert _events(logs, "ingest.underlying")[0]["pull_id"] == ""


def test_audit_chain_records_run_and_pairs(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """When an AuditWriter is wired, the spot pull appends a hash-chained
    RUN_START → per-pair → RUN_COMPLETE trail (Golden Rule #8)."""
    from brutex.orchestrator.audit import AuditWriter, read_entries, verify_chain

    def _fake_ingest(adapter, sink, *, instrument, interval, start, end, **_kw):
        if instrument.symbol_name == "FAIL":
            raise GrowwBadRequestError("boom")
        return IngestReport(windows=2, bars_appended=10, transient_failures=0)

    monkeypatch.setattr(mod, "ingest_spot", _fake_ingest)
    path = tmp_path / "ingest-audit.jsonl"
    with pull_id_scope("pull-xyz"), AuditWriter.open(path) as audit:
        report = run_groww_spot_ingest(
            object(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            instruments=[_index("OK"), _index("FAIL")],
            intervals=["1minute"],
            start=_START,
            end=_END,
            audit=audit,
        )

    assert report.failed_pairs == 1
    assert verify_chain(path) == 4
    entries = read_entries(path)
    actions = [e.action for e in entries]
    assert actions[0] == "INGEST_RUN_START"
    assert actions[-1] == "INGEST_RUN_COMPLETE"
    assert "INGEST_PAIR_DONE" in actions
    assert "INGEST_PAIR_FAILED" in actions
    start_entry = entries[0]
    assert start_entry.subject == "pull:pull-xyz"
    assert start_entry.payload["jobs"] == 2
    failed = next(e for e in entries if e.action == "INGEST_PAIR_FAILED")
    assert failed.payload["auth_failure"] is False
    complete = entries[-1]
    assert complete.payload["pairs_ok"] == 1
    assert complete.payload["pairs_failed"] == 1
    assert complete.payload["total_bars"] == 10


def _report(*, still_failed: int = 0, auth: bool = False) -> GrowwIngestReport:
    """Build a GrowwIngestReport with a chosen transient residual / auth state."""
    pairs = {
        ("X", "1minute"): IngestReport(
            windows=1,
            bars_appended=0,
            transient_failures=still_failed,
            still_failed_windows=still_failed,
        )
    }
    failures: tuple[PairFailure, ...] = ()
    if auth:
        failures = (
            PairFailure(symbol="X", interval="1day", error="token expired", auth_failure=True),
        )
    return GrowwIngestReport(pairs=pairs, failures=failures)


class TestRunGrowwSpotIngestWithRetry:
    """`run_groww_spot_ingest_with_retry` — post-run auto-retry wrapper."""

    def _patch_passes(
        self, monkeypatch: pytest.MonkeyPatch, reports: list[GrowwIngestReport]
    ) -> list[int]:
        calls: list[int] = []

        def _fake(*_args: object, **_kw: object) -> GrowwIngestReport:
            idx = len(calls)
            calls.append(idx)
            return reports[idx]

        monkeypatch.setattr(mod, "run_groww_spot_ingest", _fake)
        return calls

    def test_clean_pull_does_not_retry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = self._patch_passes(monkeypatch, [_report(still_failed=0)])
        result = run_groww_spot_ingest_with_retry(
            object(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            instruments=[_index("NIFTY")],
            intervals=["1minute"],
            start=_START,
            end=_END,
            retry_backoff=lambda _i: None,
        )
        assert calls == [0]
        assert result.retry is None
        assert result.still_failed_windows == 0

    def test_auto_retry_off_keeps_single_pass(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = self._patch_passes(monkeypatch, [_report(still_failed=7)])
        result = run_groww_spot_ingest_with_retry(
            object(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            instruments=[_index("NIFTY")],
            intervals=["1minute"],
            start=_START,
            end=_END,
            auto_retry=False,
            retry_backoff=lambda _i: None,
        )
        assert calls == [0]
        assert result.retry is None
        assert result.still_failed_windows == 7

    def test_residual_triggers_bounded_rounds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = self._patch_passes(
            monkeypatch,
            [_report(still_failed=84), _report(still_failed=10), _report(still_failed=0)],
        )
        result = run_groww_spot_ingest_with_retry(
            object(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            instruments=[_index("NIFTY")],
            intervals=["1minute"],
            start=_START,
            end=_END,
            max_retry_rounds=5,
            retry_backoff=lambda _i: None,
        )
        assert calls == [0, 1, 2]
        assert result.retry is not None
        assert result.retry.initial_still_failed == 10
        assert result.retry.final_still_failed == 0
        assert result.retry.clean is True
        assert result.still_failed_windows == 0

    def test_auth_death_halts_retry_loop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The first pull leaves a residual → a round runs; that round reports an
        # auth death (derived auth_aborted) → the shared engine halts the loop.
        calls = self._patch_passes(
            monkeypatch,
            [_report(still_failed=5), _report(still_failed=5, auth=True)],
        )
        result = run_groww_spot_ingest_with_retry(
            object(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            instruments=[_index("NIFTY")],
            intervals=["1minute"],
            start=_START,
            end=_END,
            retry_backoff=lambda _i: None,
        )
        assert calls == [0, 1]  # original + exactly one round (then halted)
        assert result.retry is not None
        assert result.retry.auth_aborted is True

    def test_still_failed_windows_property_prefers_retry(self) -> None:
        from brutex.adapters.ingest_common import RetryOutcome

        original = _report(still_failed=84)
        retry = RetryOutcome(
            initial_still_failed=84,
            final_still_failed=3,
            rounds_run=3,
            rounds=(),
            clean=False,
            auth_aborted=False,
        )
        wrapped = SpotIngestWithRetry(report=original, retry=retry)
        assert wrapped.still_failed_windows == 3
        assert SpotIngestWithRetry(report=original, retry=None).still_failed_windows == 84


def test_candle_parse_error_quarantines_pair_not_crashes_pull(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # DEC-CANDLE-PARTIAL-ABSENT-SKIP-001 Layer 2 (the operator-observed "779 symbols spot failed:
    # CandleParseError: unparseable open price: None"): a CandleParseError on un-storable
    # vendor data must quarantine the ONE symbol and let the run CONTINUE — NEVER crash the
    # whole bulk pull the way a re-raised non-GrowwError did.
    def _fake_ingest(adapter, sink, *, instrument, interval, start, end, **_kw):
        if instrument.symbol_name == "BADDATA":
            raise CandleParseError("unparseable open price: None")
        return IngestReport(windows=1, bars_appended=5, transient_failures=0)

    monkeypatch.setattr(mod, "ingest_spot", _fake_ingest)
    with capture_logs() as logs:
        report = run_groww_spot_ingest(
            object(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            instruments=[_index("NIFTY"), _index("BADDATA", token=77)],
            intervals=["1day"],
            start=_START,
            end=_END,
        )
    assert report.failed_pairs == 1
    assert report.failures == (
        PairFailure(
            symbol="BADDATA",
            interval="1day",
            error="unparseable open price: None",
            auth_failure=False,  # a parse error is never an auth failure
        ),
    )
    assert report.auth_aborted is False
    assert ("NIFTY", "1day") in report.pairs  # the good symbol still landed
    assert report.bars_appended == 5
    (quarantine,) = _events(logs, "ingest.quarantine")
    assert quarantine["error"] == "unparseable open price: None"


def test_out_of_scope_error_quarantines_pair_not_crashes_pull(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # DEC-GROWW-QUARANTINE-BRUTEX-001 (the operator-observed "779 symbols spot failed:
    # OutOfScopeError: symbol not in Groww universe: BAJAJ-AUTO"): an OutOfScopeError — a
    # constituent the niftyindices→Groww match produced that the Groww adapter can't
    # resolve — is a BruteXError, so it quarantines the ONE symbol and the run CONTINUES,
    # never crashing the whole 779-symbol bulk pull (the 8,019/10,906 abort in the operator
    # screenshot).
    def _fake_ingest(adapter, sink, *, instrument, interval, start, end, **_kw):
        if instrument.symbol_name == "BAJAJ-AUTO":
            raise OutOfScopeError("symbol not in Groww universe: BAJAJ-AUTO")
        return IngestReport(windows=1, bars_appended=5, transient_failures=0)

    monkeypatch.setattr(mod, "ingest_spot", _fake_ingest)
    with capture_logs() as logs:
        report = run_groww_spot_ingest(
            object(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            instruments=[_index("NIFTY"), _index("BAJAJ-AUTO", token=88)],
            intervals=["1day"],
            start=_START,
            end=_END,
        )
    assert report.failed_pairs == 1
    assert report.failures == (
        PairFailure(
            symbol="BAJAJ-AUTO",
            interval="1day",
            error="symbol not in Groww universe: BAJAJ-AUTO",
            auth_failure=False,  # a scope error is never an auth failure
        ),
    )
    assert ("NIFTY", "1day") in report.pairs  # the other 778 still pull
    assert report.bars_appended == 5
    (quarantine,) = _events(logs, "ingest.quarantine")
    assert "BAJAJ-AUTO" in str(quarantine["error"])


@pytest.mark.parametrize(
    "quota_exc",
    [
        DailyQuotaExceeded("daily quota of 100000 calls spent"),
        QuotaInvariantError("quota ledger invariant broke"),
        LakeWriteHalt("disk_full"),  # DEC-LAKE-WRITE-HALT-001 — disk full halts the pull
        LakeWriteHalt("out_of_memory"),
    ],
)
def test_global_stop_quota_error_propagates_not_quarantined(
    monkeypatch: pytest.MonkeyPatch, quota_exc: BruteXError
) -> None:
    # DEC-GROWW-QUARANTINE-BRUTEX-001 carve-out: a quota signal is a GLOBAL stop, NOT one
    # symbol's problem — every remaining symbol would fail the same way — so it must
    # PROPAGATE out of run_groww_spot_ingest to the controller's quota handling
    # (DEC-SPOT-QUOTA-MIDROUND-001 → the loud quota_exhausted verdict), NEVER be quarantined
    # per-symbol like an OutOfScopeError. Without the carve-out the broad BruteXError
    # quarantine swallowed DailyQuotaExceeded and the quota verdict never fired — the exact
    # regression (TestQuotaDeathMidRound) this test locks shut.
    def _fake_ingest(adapter, sink, *, instrument, interval, start, end, **_kw):
        raise quota_exc

    monkeypatch.setattr(mod, "ingest_spot", _fake_ingest)
    with pytest.raises(type(quota_exc)):
        run_groww_spot_ingest(
            object(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            instruments=[_index("NIFTY"), _index("BANKNIFTY", token=77)],
            intervals=["1day"],
            start=_START,
            end=_END,
        )


def test_on_window_is_threaded_into_each_pair(monkeypatch: pytest.MonkeyPatch) -> None:
    # DEC-SPOT-WINDOW-PROGRESS-001: the per-window tick must reach ingest_spot for every
    # pair so the dashboard advances per WINDOW.
    seen: list[object] = []

    def _fake_ingest(adapter, sink, *, instrument, interval, start, end, on_window=None, **_kw):
        seen.append(on_window)
        return IngestReport(windows=1, bars_appended=1, transient_failures=0)

    monkeypatch.setattr(mod, "ingest_spot", _fake_ingest)

    def _tick() -> None:
        pass

    run_groww_spot_ingest(
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        instruments=[_index("NIFTY"), _index("BANKNIFTY", token=2)],
        intervals=["1day"],
        start=_START,
        end=_END,
        on_window=_tick,
    )
    assert seen == [_tick, _tick]  # the SAME hook threaded into both pairs


def test_with_retry_threads_on_window(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[object] = []

    def _fake_ingest(adapter, sink, *, instrument, interval, start, end, on_window=None, **_kw):
        seen.append(on_window)
        return IngestReport(windows=1, bars_appended=1, transient_failures=0)

    monkeypatch.setattr(mod, "ingest_spot", _fake_ingest)

    def _tick() -> None:
        pass

    run_groww_spot_ingest_with_retry(
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        instruments=[_index("NIFTY")],
        intervals=["1day"],
        start=_START,
        end=_END,
        auto_retry=False,
        on_window=_tick,
    )
    assert seen == [_tick]


# ── _resolve_spot_window — the opt-in per-symbol window hook (DEC-SPOT-INTERVAL-DECOUPLE-001) ──
#
# The SPOT pull paths always pass ``symbol_windows=None`` since the 1-minute pull is
# fully decoupled from the 1-day coverage (MIGRATION.md #851). These tests cover the
# RETAINED opt-in mechanism (the never-narrow fallback any caller may still supply).


class TestResolveSpotWindow:
    """The pure ``(start, end)`` resolver behind the opt-in per-symbol minute bounds."""

    def test_none_map_is_global_range(self) -> None:
        # ``symbol_windows is None`` (the SPOT path's value since the decouple) → the
        # global range — the 1-minute pull is the full ``[start, end)`` for every symbol.
        assert mod._resolve_spot_window(
            None, symbol="NIFTY", interval="1minute", start=_START, end=_END
        ) == (_START, _END)

    def test_daily_interval_is_never_bounded(self) -> None:
        # The DAILY interval is never bounded — even when the symbol is present in the
        # map (a spot index's daily series must advance to the requested end).
        windows = {"NIFTY": (date(2022, 4, 5), date(2022, 5, 5))}
        assert mod._resolve_spot_window(
            windows, symbol="NIFTY", interval="1day", start=_START, end=_END
        ) == (_START, _END)

    def test_symbol_present_sub_daily_uses_mapped_window(self) -> None:
        # A SUB-DAILY interval of a symbol in an opt-in map → its mapped window
        # (the line-152 branch the leaf coverage was missing).
        bound = (date(2022, 4, 5), date(2022, 5, 5))
        assert (
            mod._resolve_spot_window(
                {"NIFTY": bound}, symbol="NIFTY", interval="1minute", start=_START, end=_END
            )
            == bound
        )

    def test_symbol_absent_falls_back_to_global(self) -> None:
        # A symbol the caller did NOT bound → the never-narrow global fallback.
        windows = {"NIFTY": (date(2022, 4, 5), date(2022, 5, 5))}
        assert mod._resolve_spot_window(
            windows, symbol="BANKNIFTY", interval="1minute", start=_START, end=_END
        ) == (_START, _END)


def test_symbol_windows_threaded_into_ingest(monkeypatch: pytest.MonkeyPatch) -> None:
    """An opt-in ``symbol_windows`` map bounds each sub-daily pair's ``(start, end)``.

    A mapped symbol's 1-minute pull uses its mapped window; its 1-day pull stays the
    global range; an unbounded symbol's 1-minute stays global too. (The SPOT path
    passes ``None`` since DEC-SPOT-INTERVAL-DECOUPLE-001; this exercises the retained
    opt-in fallback.)
    """
    seen: dict[tuple[str, str], tuple[date, date]] = {}

    def _fake_ingest(adapter, sink, *, instrument, interval, start, end, **_kw):
        seen[instrument.symbol_name, interval] = (start, end)
        return IngestReport(windows=1, bars_appended=1, transient_failures=0)

    monkeypatch.setattr(mod, "ingest_spot", _fake_ingest)
    bound = (date(2022, 4, 5), _END)
    run_groww_spot_ingest(
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        instruments=[_index("NIFTY"), _index("BANKNIFTY", token=2)],
        intervals=["1minute", "1day"],
        start=_START,
        end=_END,
        symbol_windows={"NIFTY": bound},
    )
    # NIFTY 1-minute is bounded; NIFTY 1-day is NOT (daily produces the bound).
    assert seen["NIFTY", "1minute"] == bound
    assert seen["NIFTY", "1day"] == (_START, _END)
    # BANKNIFTY is absent from the map → global for both intervals.
    assert seen["BANKNIFTY", "1minute"] == (_START, _END)
    assert seen["BANKNIFTY", "1day"] == (_START, _END)


def test_with_retry_threads_symbol_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """``run_groww_spot_ingest_with_retry`` forwards ``symbol_windows`` to every pass."""
    seen: list[tuple[date, date]] = []

    def _fake_ingest(adapter, sink, *, instrument, interval, start, end, **_kw):
        seen.append((start, end))
        return IngestReport(windows=1, bars_appended=1, transient_failures=0)

    monkeypatch.setattr(mod, "ingest_spot", _fake_ingest)
    bound = (date(2022, 4, 5), _END)
    run_groww_spot_ingest_with_retry(
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        instruments=[_index("NIFTY")],
        intervals=["1minute"],
        start=_START,
        end=_END,
        auto_retry=False,
        symbol_windows={"NIFTY": bound},
    )
    assert seen == [bound]
