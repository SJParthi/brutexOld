"""Sustained stress for the Groww option ingest driver (DEC-GROWW-OPTION-INGEST-001).

Proves the SEQUENTIAL multi-contract driver scales linearly without state leak
and that the shared :class:`~brutex.data.window_ledger.WindowLedger` makes a
re-pull idempotent at scale: a large leg-set ingests cleanly on pass 1, then a
second pass over the SAME ledger SKIPS every contract (zero vendor calls) — the
no-mask / no-re-fetch guarantee under load (CLAUDE.md §11 SCALABLE, Golden Rule #7).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime

import pytest
from brutex.adapters.groww.client import GrowwClient
from brutex.adapters.groww.option_ingest import run_groww_option_ingest
from brutex.adapters.protocol import DerivativeContract
from brutex.data.models import CandleBar
from brutex.data.sink import SinkAppendResult
from brutex.data.window_ledger import WindowLedger

# Marker discipline (tests/stress/__init__.py): the nightly (`-m stress`) and
# self-hosted soak (`-m 'stress or soak'`) tiers select BY MARKER — an unmarked
# stress test silently drops out of both tiers (SOAK-R7-1).
pytestmark = pytest.mark.stress

_N = 2_000  # leg-set scale (index strikes × CE/PE × expiries fan out to this order)


def _now() -> datetime:
    return datetime(2026, 6, 13, 8, 0, tzinfo=UTC)


class _FakeSDK:
    def __init__(self) -> None:
        self.calls = 0

    def get_historical_candles(self, **_kw: object) -> Mapping[str, object]:
        self.calls += 1
        return {"candles": [["2024-01-02T09:30:00", 100.0, 110.0, 90.0, 105.0, 5, 1]]}

    def get_expiries(self, **_kw: object) -> Mapping[str, object]:  # pragma: no cover
        return {"expiries": []}

    def get_contracts(self, **_kw: object) -> Mapping[str, object]:  # pragma: no cover
        return {"contracts": []}


class _Sink:
    def append(
        self,
        *,
        exchange: str,
        segment: str,
        symbol: str,
        interval: str,
        bars: Sequence[CandleBar],
        merge_mode: str = "keep-first",
    ) -> SinkAppendResult:
        del exchange, segment, symbol, interval
        return SinkAppendResult(bars_appended=len(bars), partitions_written=1, write_results=())


def _chain(n: int) -> list[DerivativeContract]:
    return [
        DerivativeContract(
            underlying_symbol="NIFTY",
            expiry=date(2024, 1, 25),
            symbol=f"NSE-NIFTY-25Jan24-{20_000 + i * 50}-CE",
            contract_type="CE",
            strike=float(20_000 + i * 50),
        )
        for i in range(n)
    ]


def test_sustained_ingest_then_idempotent_repull() -> None:
    contracts = _chain(_N)
    sdk = _FakeSDK()
    client = GrowwClient(sdk=sdk, sleep=lambda _s: None, max_attempts=1)
    sink, ledger = _Sink(), WindowLedger()

    first = run_groww_option_ingest(
        client,
        sink,
        contracts=contracts,
        intervals=["1minute"],
        start=date(2024, 1, 1),
        end=date(2024, 1, 26),
        ledger=ledger,
        now_ist=_now,
    )
    assert len(first.pairs) == _N and first.failed_pairs == 0
    assert first.bars_appended == _N and first.skipped_windows == 0
    assert sdk.calls == _N  # one vendor call per contract

    calls_after_first = sdk.calls
    second = run_groww_option_ingest(
        client,
        sink,
        contracts=contracts,
        intervals=["1minute"],
        start=date(2024, 1, 1),
        end=date(2024, 1, 26),
        ledger=ledger,
        now_ist=_now,
    )
    # Every window settled SUCCESS on pass 1 → pass 2 skips them all, NO vendor calls.
    assert second.skipped_windows == _N and second.bars_appended == 0
    assert sdk.calls == calls_after_first  # zero new vendor calls
    assert ledger.count == _N  # one ledger row per contract — no duplication/leak
