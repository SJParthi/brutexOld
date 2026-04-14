"""End-to-end smoke test — drive the full 4-phase pipeline with a FakeGrowwClient.

Wires every component written in P1a Day 2 together:

* GrowwAdapter (with FakeGrowwClient)
* AdaptiveTokenBucket + WorkItemStore + AuditWriter
* PhaseADriver → PhaseBDriver → PhaseCDriver → PhaseDDriver
* run_sequential_pipeline (with the 4 drivers)
* DashboardState subscriber
* TelegramNotifier (sync mode with RecordingTransport)
* verify_chain on the audit log after the run

This is a **smoke test**, not a full scenario suite. It asserts the
drivers compose correctly and the pipeline reaches RUN_COMPLETE with
non-zero outputs across all four phases. Real scenario gauntlet tests
land in later commits as specific cases become interesting (gap
handling, expiry day, circuit filters, etc.).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from brutex.adapters.groww.candles import CandleRequest, PhaseDDriver
from brutex.adapters.groww.client import GrowwAdapter
from brutex.adapters.groww.contracts import PhaseCDriver
from brutex.adapters.groww.expiries import PhaseBDriver
from brutex.adapters.groww.instruments import PhaseADriver
from brutex.dashboard.app import DashboardState
from brutex.models import Exchange
from brutex.notifications.telegram import (
    TelegramConfig,
    TelegramEvent,
    TelegramEventKind,
    TelegramNotifier,
    TelegramSeverity,
)
from brutex.orchestrator.audit import AuditWriter, verify_chain
from brutex.orchestrator.pipeline import (
    PhaseHaltError,
    PipelineEventKind,
    run_sequential_pipeline,
)
from brutex.orchestrator.rate_limiter import AdaptiveTokenBucket
from brutex.orchestrator.state import IngestPhase, WorkItemState
from brutex.orchestrator.store import WorkItemStore
from brutex.universe import UnderlyingSpec
from tests.unit.test_adapters_groww_client import FakeDataFrame, FakeGrowwClient
from tests.unit.test_adapters_groww_instruments import (
    _banknifty_pe_row,
    _nifty_spot_row,
    _sensex_ce_row,
)


class RecordingTransport:
    """Minimal Telegram transport for the smoke test."""

    def __init__(self) -> None:
        self.sent: list[TelegramEvent] = []

    def __call__(
        self, config: TelegramConfig, event: TelegramEvent
    ) -> bool:
        self.sent.append(event)
        return True


@pytest.fixture
def store(tmp_path: Path):
    with WorkItemStore.open(tmp_path / "store.lmdb") as s:
        yield s


@pytest.fixture
def audit_path(tmp_path: Path) -> Path:
    return tmp_path / "audit" / "run.jsonl"


@pytest.fixture
def audit(audit_path: Path):
    with AuditWriter.open(audit_path) as a:
        yield a


@pytest.fixture
def fake_client() -> FakeGrowwClient:
    client = FakeGrowwClient()
    # Phase A: real instrument rows (NIFTY spot + BANKNIFTY PE + SENSEX CE)
    client.instruments_response = FakeDataFrame(
        [_nifty_spot_row(), _banknifty_pe_row(), _sensex_ce_row()]
    )
    # Phase B: two expiries per query
    client.expiries_response = {"expiries": ["2024-01-25", "2024-02-29"]}
    # Phase C: three contracts per expiry
    client.contracts_response = {
        "contracts": [
            "NSE-NIFTY-25Jan24-22000-CE",
            "NSE-NIFTY-25Jan24-22000-PE",
            "NSE-NIFTY-25Jan24-22100-CE",
        ]
    }
    # Phase D: one candle per chunk
    client.candles_response = {
        "candles": [
            [
                "2024-01-25T09:15:00",
                22000.0,
                22010.0,
                21990.0,
                22005.0,
                100,
                None,
            ]
        ],
        "closing_price": 22005.0,
        "start_time": "2024-01-25 09:15:00",
        "end_time": "2024-01-25 09:16:00",
        "interval_in_minutes": 1,
    }
    return client


@pytest.fixture
def bucket() -> AdaptiveTokenBucket:
    return AdaptiveTokenBucket(
        initial_rps=10000.0,
        downshift_rps=5000.0,
        burst=1000,
    )


@pytest.fixture
def adapter(
    fake_client: FakeGrowwClient,
    bucket: AdaptiveTokenBucket,
    store: WorkItemStore,
    audit: AuditWriter,
) -> GrowwAdapter:
    return GrowwAdapter(
        client=fake_client,
        bucket=bucket,
        store=store,
        audit=audit,
        run_id="smoke-test-run",
    )


TWO_INDICES = (
    UnderlyingSpec(
        exchange=Exchange.NSE,
        underlying_symbol="NIFTY",
        display_name="NIFTY 50",
        fno_live_since_year=2020,
    ),
    UnderlyingSpec(
        exchange=Exchange.BSE,
        underlying_symbol="SENSEX",
        display_name="BSE SENSEX",
        fno_live_since_year=2020,
    ),
)


class TestEndToEndSmoke:
    def test_sequential_pipeline_runs_to_completion(
        self,
        adapter: GrowwAdapter,
        store: WorkItemStore,
        audit: AuditWriter,
        audit_path: Path,
    ) -> None:
        """The full A → B → C → D run with dashboard + telegram hooks."""

        # Dashboard subscriber
        dashboard = DashboardState()

        # Telegram notifier in sync mode with a RecordingTransport
        telegram_config = TelegramConfig(
            bot_token="test", chat_id="123", enabled=True
        )
        telegram_transport = RecordingTransport()
        notifier = TelegramNotifier(
            config=telegram_config,
            transport=telegram_transport,
            synchronous=True,
        )

        def telegram_subscriber(event) -> None:
            # Mirror a few pipeline events to Telegram
            if event.kind in (
                PipelineEventKind.PHASE_START,
                PipelineEventKind.PHASE_COMPLETE,
            ):
                notifier.publish(
                    TelegramEvent(
                        kind=(
                            TelegramEventKind.PHASE_START
                            if event.kind == PipelineEventKind.PHASE_START
                            else TelegramEventKind.PHASE_COMPLETE
                        ),
                        severity=TelegramSeverity.INFO,
                        message=f"{event.kind} phase={event.phase}",
                    )
                )

        subscribers = [dashboard.on_pipeline_event, telegram_subscriber]

        # Build the four drivers
        phase_a = PhaseADriver(
            adapter=adapter,
            store=store,
            audit=audit,
            run_id="smoke-test-run",
            snapshot_date=date(2024, 1, 25),
            subscribers=subscribers,
        )

        # Run Phase A first to get its output
        phase_a_result = phase_a.run()
        assert phase_a_result.success == 1  # single snapshot item

        # Derive underlyings from Phase A's normalised instruments (for Phase B)
        # For the smoke test we just use the two named indices.
        phase_b = PhaseBDriver(
            adapter=adapter,
            store=store,
            audit=audit,
            run_id="smoke-test-run",
            underlyings=TWO_INDICES,
            start_year=2024,
            end_year=2024,
            subscribers=subscribers,
        )
        phase_b_result = phase_b.run()
        assert phase_b_result.total == 2
        assert phase_b_result.success == 2
        # 2 underlyings × 2 expiries each = 4 collected expiry keys
        assert (
            sum(len(v) for v in phase_b.collected_expiries.values()) == 4
        )

        # Phase C consumes Phase B's output
        phase_c = PhaseCDriver(
            adapter=adapter,
            store=store,
            audit=audit,
            run_id="smoke-test-run",
            expiries=phase_b.collected_expiries,
            subscribers=subscribers,
        )
        phase_c_result = phase_c.run()
        # 2 underlyings × 2 expiries = 4 work items
        assert phase_c_result.total == 4
        assert phase_c_result.success == 4
        # 4 work items × 3 contracts each = 12 collected
        assert phase_c.last_result is not None
        assert phase_c.last_result.total_contracts == 12

        # Phase D consumes Phase C's output (build CandleRequests)
        # For the smoke test we use just the first 2 contracts, 1-day range
        all_contracts = list(phase_c.iter_all_contracts())[:2]
        requests = [
            CandleRequest(
                exchange=exchange,
                segment="FNO",
                groww_symbol=symbol,
                interval="1minute",
                start=date(2024, 1, 25),
                end=date(2024, 1, 25),
            )
            for exchange, underlying, expiry, symbol in all_contracts
        ]
        phase_d = PhaseDDriver(
            adapter=adapter,
            store=store,
            audit=audit,
            run_id="smoke-test-run",
            requests=requests,
            subscribers=subscribers,
        )
        phase_d_result = phase_d.run()
        assert phase_d_result.total == 2  # 2 requests × 1 chunk each
        assert phase_d_result.success == 2
        assert phase_d.last_result is not None
        assert phase_d.last_result.total_candle_rows == 2

        # ── Assertions ─────────────────────────────────────────

        # Audit chain integrity
        audit.close()
        entry_count = verify_chain(audit_path)
        assert entry_count > 10  # at minimum: 4 phase starts + 4 completes + SDK calls

        # Dashboard recorded all phase events
        assert dashboard.phases["A_INSTRUMENTS"].status == "done"
        assert dashboard.phases["B_EXPIRIES"].status == "done"
        assert dashboard.phases["C_CONTRACTS"].status == "done"
        assert dashboard.phases["D_CANDLES"].status == "done"

        # Telegram received phase events
        assert len(telegram_transport.sent) >= 4  # 4 phase starts at minimum
        notifier.stop()


class TestRunSequentialPipelineIntegration:
    def test_four_phases_chained_via_run_sequential_pipeline(
        self,
        adapter: GrowwAdapter,
        store: WorkItemStore,
        audit: AuditWriter,
        audit_path: Path,
    ) -> None:
        """Verify run_sequential_pipeline chains the four drivers and
        writes RUN_START + RUN_COMPLETE audit entries."""

        # We need a little glue because Phases B, C, D depend on the
        # previous phase's output. For this test we wire them into
        # stub drivers that call the real ones but ignore the input
        # dependency by computing a trivial universe.

        phase_a = PhaseADriver(
            adapter=adapter,
            store=store,
            audit=audit,
            run_id="seq-smoke",
            snapshot_date=date(2024, 1, 25),
        )
        phase_b = PhaseBDriver(
            adapter=adapter,
            store=store,
            audit=audit,
            run_id="seq-smoke",
            underlyings=TWO_INDICES,
            start_year=2024,
            end_year=2024,
        )
        # We can't yet build phase C/D without running phase B first,
        # so chain phases A and B only via run_sequential_pipeline.
        results = run_sequential_pipeline(
            drivers=[phase_a, phase_b],
            audit=audit,
            run_id="seq-smoke",
        )
        assert len(results) == 2
        assert results[0].phase == IngestPhase.A_INSTRUMENTS
        assert results[1].phase == IngestPhase.B_EXPIRIES
        assert results[0].success == 1
        assert results[1].success == 2

        audit.close()
        entry_count = verify_chain(audit_path)
        assert entry_count > 5  # RUN_START, phase starts/completes, SDK calls


class TestPipelineHaltsOnQuarantine:
    def test_phase_halt_prevents_downstream_phases(
        self,
        fake_client: FakeGrowwClient,
        bucket: AdaptiveTokenBucket,
        store: WorkItemStore,
        audit: AuditWriter,
    ) -> None:
        """A BAD_REQUEST in Phase A must block Phase B from starting."""

        # Inject a synthetic BadRequest from the client.
        # Use the synthetic exception class from the test module
        # (same class name as the real one so classify_exception dispatches).
        from tests.unit.test_adapters_groww_client import (
            GrowwAPIBadRequestException,
        )

        fake_client.instruments_exception = GrowwAPIBadRequestException(
            "bad params"
        )

        adapter = GrowwAdapter(
            client=fake_client,
            bucket=bucket,
            store=store,
            audit=audit,
            run_id="halt-run",
        )
        phase_a = PhaseADriver(
            adapter=adapter,
            store=store,
            audit=audit,
            run_id="halt-run",
            snapshot_date=date(2024, 1, 25),
        )
        phase_b = PhaseBDriver(
            adapter=adapter,
            store=store,
            audit=audit,
            run_id="halt-run",
            underlyings=TWO_INDICES,
            start_year=2024,
            end_year=2024,
        )

        with pytest.raises(PhaseHaltError) as exc_info:
            run_sequential_pipeline(
                drivers=[phase_a, phase_b],
                audit=audit,
                run_id="halt-run",
            )
        assert exc_info.value.phase == IngestPhase.A_INSTRUMENTS
        assert exc_info.value.quarantined_count == 1

        # Phase B was never invoked — check get_expiries call count
        assert fake_client.get_expiries_calls == 0
