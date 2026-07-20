"""Engine wiring tests for the CVaR tail-risk gate (DEC-RANK-CVAR-TAIL-GATE-001).

The gate rides ``SweepRunConfig.cvar_gate`` (default ``None`` = OFF) and is
applied at the ONE chunked eval spine
(:func:`brutex.engine.sweep_run._iter_mask_metrics`) — the only production
point where each mask's per-trade P&L list exists. Pinned here:

* the OFF default — ``cvar_gate=None`` runs byte-identically (the frozen
  sweep goldens are the cross-file proof; in-file: a lenient-threshold
  ACTIVE gate that rejects nothing produces the IDENTICAL store content
  hash as the OFF run — the gate's only deltas are the summary counter +
  the emit-on-active INFO);
* the ACTIVE gate rejects fat-tail masks BEFORE ranking (counted on
  ``SweepRunSummary.cvar_rejected`` + the ``sweep.cvar_gate`` INFO under
  the ``cvar_tail`` token);
* the error contract at the spine — a gate raise is a per-combo
  ``SweepComboError`` (the metrics-tail law), ``MemoryError`` re-raises
  BARE;
* the grid→run threading (``SweepGridConfig.cvar_gate`` →
  ``to_run_config``) and the direct-generator ``on_cvar_reject=None`` arm.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import pytest
from brutex.data.models import CandleBar
from brutex.data.option_dedup import OptionDedupIndex
from brutex.engine import sweep_run
from brutex.engine.results_store import ResultsStore
from brutex.engine.rust_trade_sim import ResidentTradeSim
from brutex.engine.sweep_orchestrator import SweepGridConfig, Timeframe, iter_sweep_tuples
from brutex.engine.sweep_run import (
    ComboDedup,
    SweepComboError,
    SweepRunConfig,
    SweepRunSummary,
    run_persisted_sweep,
)
from brutex.orchestrator.audit import AuditWriter
from brutex.ranking import CvarGateConfig
from brutex.strategies.types import Side
from structlog.testing import capture_logs

if TYPE_CHECKING:
    from collections.abc import Sequence

_IST = ZoneInfo("Asia/Kolkata")


def _bar(day: int, minute: int, o: float, c: float) -> CandleBar:
    ts = (
        datetime(2026, 1, 5, 9, 16, tzinfo=_IST) + timedelta(days=day, minutes=minute)
    ).astimezone(UTC)
    hi = max(o, c) + 0.5
    lo = min(o, c) - 0.5
    return CandleBar(
        timestamp=ts,
        open=Decimal(str(round(o, 2))),
        high=Decimal(str(round(hi, 2))),
        low=Decimal(str(round(lo, 2))),
        close=Decimal(str(round(c, 2))),
        volume=100,
        open_interest=None,
    )


def _trending_bars(n_days: int = 45, per_day: int = 120) -> list[CandleBar]:
    """The test_engine_sweep_run drifting fixture: up days + every-5th DOWN day.

    Masks 2/8/16 pass the hard gates with a mixed win/loss trade stream —
    their down-day session-gap losses are exactly the fat tail the CVaR
    gate exists to judge; masks 1/4 stay all-winner (positive tail).
    """
    bars: list[CandleBar] = []
    for d in range(n_days):
        price = 100.0
        down_day = d % 5 == 4
        for m in range(per_day):
            if down_day:
                close = price - 0.6 if m % 3 else price + 0.3
            else:
                close = price + 0.6 if m % 3 else price - 0.3
            bars.append(_bar(d, m, price, close))
            price = close
    return bars


def _make_config(**overrides: object) -> SweepRunConfig:
    base: dict[str, object] = {
        "instrument": "NIFTY",
        "tf": "1m",
        "direction": Side.BUY,
        "mode": "A",
        "condition_bits": [0, 1, 2, 3, 4],
        "k": 1,
        "data_snapshot_hash": "snap0001",
        "indicator_version": "ind-v1",
        "code_commit_sha": "deadbeef",
        "tf_minutes": 1,
        "sl_paisa": None,
        "tp_paisa": None,
    }
    base.update(overrides)
    return SweepRunConfig(**base)  # type: ignore[arg-type]


def _run(
    tmp_path: Path, tag: str, bars: Sequence[CandleBar], cfg: SweepRunConfig
) -> sweep_run.SweepRunSummary:
    store = ResultsStore(path=tmp_path / f"{tag}.parquet")
    with AuditWriter.open(tmp_path / f"{tag}.jsonl") as audit:
        return run_persisted_sweep(
            bars,
            cfg,
            store=store,
            dedup=ComboDedup(OptionDedupIndex(capacity=10_000, fpr_target=1e-6)),
            audit=audit,
        )


@pytest.fixture(scope="module")
def bars() -> list[CandleBar]:
    return _trending_bars()


class TestGateOffDefault:
    def test_default_config_carries_no_gate(self) -> None:
        assert _make_config().cvar_gate is None

    def test_summary_field_defaults_to_zero(self) -> None:
        assert SweepRunSummary.__dataclass_fields__["cvar_rejected"].default == 0

    def test_off_run_counts_nothing_and_emits_nothing(
        self, tmp_path: Path, bars: list[CandleBar]
    ) -> None:
        with capture_logs() as logs:
            summary = _run(tmp_path, "off", bars, _make_config())
        assert summary.cvar_rejected == 0
        assert summary.ranked > 0  # the fixture's gate-passing masks rank
        assert not [e for e in logs if e["event"] == "sweep.cvar_gate"]


class TestGateActive:
    def test_lenient_threshold_is_content_identical_to_off(
        self, tmp_path: Path, bars: list[CandleBar]
    ) -> None:
        # A gate that rejects NOTHING (astronomical threshold) must produce
        # the byte-identical store content: cvar_gate is NOT in the run
        # identity — only the store ADDRESS re-keys (the identity fragment),
        # which run_persisted_sweep does not mint. The only deltas are the
        # summary counter (0) + the emit-on-active INFO.
        off = _run(tmp_path, "off", bars, _make_config())
        lenient = CvarGateConfig(threshold_paisa=10**12)
        with capture_logs() as logs:
            on = _run(tmp_path, "on", bars, _make_config(cvar_gate=lenient))
        assert on.store_content_hash == off.store_content_hash
        assert (on.ranked, on.persisted, on.backtested) == (
            off.ranked,
            off.persisted,
            off.backtested,
        )
        assert on.cvar_rejected == 0
        events = [e for e in logs if e["event"] == "sweep.cvar_gate"]
        assert len(events) == 1
        assert events[0]["rejected"] == 0
        assert events[0]["reason"] == "cvar_tail"
        assert events[0]["threshold_paisa"] == 10**12

    def test_tight_threshold_rejects_fat_tail_masks_before_ranking(
        self, tmp_path: Path, bars: list[CandleBar]
    ) -> None:
        # threshold_paisa=1: any mask whose worst-5% tail loses on average
        # more than 1 paisa/trade is rejected AT THE SPINE. The fixture's
        # rank-passing masks (2/8/16) all carry down-day losses ⇒ rejected
        # (ranked collapses to 0 — the surviving all-winner masks 1/4 fail
        # the profit-factor door on their own); the all-winner masks pass
        # the CVaR gate (positive tail — the accept arm of the SAME run).
        off = _run(tmp_path, "off", bars, _make_config())
        tight = CvarGateConfig(threshold_paisa=1, alpha_bps=500)
        with capture_logs() as logs:
            on = _run(tmp_path, "tight", bars, _make_config(cvar_gate=tight))
        assert on.cvar_rejected >= 3
        assert on.ranked == 0
        assert on.persisted == 0
        assert on.backtested == off.backtested - on.cvar_rejected
        events = [e for e in logs if e["event"] == "sweep.cvar_gate"]
        assert len(events) == 1
        assert events[0]["rejected"] == on.cvar_rejected
        assert events[0]["alpha_bps"] == 500

    def test_gate_error_is_a_per_combo_sweep_combo_error(
        self, tmp_path: Path, bars: list[CandleBar], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _raises(*_a: object, **_k: object) -> bool:
            raise RuntimeError("cvar boom")

        monkeypatch.setattr(sweep_run, "cvar_rejects", _raises)
        cfg = _make_config(cvar_gate=CvarGateConfig(threshold_paisa=1))
        with pytest.raises(SweepComboError, match="cvar boom"):
            _run(tmp_path, "err", bars, cfg)

    def test_gate_memory_error_re_raises_bare(
        self, tmp_path: Path, bars: list[CandleBar], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _oom(*_a: object, **_k: object) -> bool:
            raise MemoryError

        monkeypatch.setattr(sweep_run, "cvar_rejects", _oom)
        cfg = _make_config(cvar_gate=CvarGateConfig(threshold_paisa=1))
        with pytest.raises(MemoryError):
            _run(tmp_path, "oom", bars, cfg)


class TestDirectSpineSeam:
    def test_on_cvar_reject_none_drops_silently(
        self, bars: list[CandleBar], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A direct _iter_mask_metrics caller may pass no counter callback:
        # rejected masks are dropped with NO callback dispatch (the
        # ``on_cvar_reject is not None`` False arm).
        monkeypatch.setattr(sweep_run, "cvar_rejects", lambda *_a, **_k: True)
        cfg = _make_config()
        bar_bits = [1] * len(bars)  # bit 0 always fires — mask 1 trades
        sim = ResidentTradeSim(bars, bar_bits, tf_minutes=1, reference_stamper=None)
        yielded = list(
            sweep_run._iter_mask_metrics(  # pyright: ignore[reportPrivateUsage]
                sim,
                [1],
                bars,
                bar_bits,
                cfg,
                "bull",
                lambda _m: "run-id",
                cvar_gate=CvarGateConfig(threshold_paisa=1),
                on_cvar_reject=None,
            )
        )
        assert yielded == []


class TestGridThreading:
    def _grid(self, **overrides: object) -> SweepGridConfig:
        base: dict[str, object] = {
            "instruments": ["NIFTY"],
            "timeframes": [Timeframe("1minute", 1)],
            "directions": [Side.BUY],
            "k_values": [1],
            "condition_bits": [0, 1, 2, 3, 4],
            "stop_losses": [80],
            "take_profits": [50],
            "proximities": [None],
            "mode": "A",
            "data_snapshot_hash": "snap0001",
            "indicator_version": "ind-v1",
            "code_commit_sha": "deadbeef",
        }
        base.update(overrides)
        return SweepGridConfig(**base)  # type: ignore[arg-type]

    def test_grid_default_is_off(self) -> None:
        assert self._grid().cvar_gate is None

    def test_to_run_config_threads_the_gate(self) -> None:
        gate = CvarGateConfig(threshold_paisa=25_000, alpha_bps=250)
        grid = self._grid(cvar_gate=gate)
        tup = next(iter(iter_sweep_tuples(grid)))
        assert tup.to_run_config(grid).cvar_gate is gate

    def test_to_run_config_default_threads_none(self) -> None:
        grid = self._grid()
        tup = next(iter(iter_sweep_tuples(grid)))
        assert tup.to_run_config(grid).cvar_gate is None
