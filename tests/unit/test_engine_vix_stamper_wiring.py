"""Wiring tests for the India-VIX ``reference_stamper`` seam (DEC-VIX-STAMP-WIRE-001).

Stage B of the VIX-stamp wiring: the ``reference_stamper`` kwarg is threaded
through EVERY production ``simulate_trades`` path and BOUND at every path whose
trades are OBSERVABLE — persisted finalist rows (grid + stage-3 refined cells,
via the finalist projection's own re-simulation), the WF/K-of-N oracle
re-simulation, and the sweep-verify recount. (The CLI options-sim replay and
the whole cascade chain are since sanctioned-UNSTAMPED: round-2 removed the
replay binds — stamps provably discarded at the SpotSignal conversion — and
round-6 deleted the cascade stamper seam outright, its stamps discarded at the
CandidateInput scalar conversion.) Each test here proves ONE bind or thread is
real — a monkeypatched capture on the exact seam the production call crosses,
or a real end-to-end run whose trades carry the stamps — so no bind can
silently regress.

Companion suite: ``tests/unit/test_vix_stamper_binding_pin.py`` (the AST pin
enumerating the sanctioned literal-``None`` discarded-stream sites — FIVE
after the round-6 review — plus the omission + positive-presence laws).
Persisted-bytes honesty (updated for Stage C — the nullable
``vix_mean``/``vix_min``/``vix_max`` columns now land on the persisted rows):
the stamped-vs-unstamped store regression in :class:`TestSweepRunSeam` pins
that stamping changes ONLY the three nullable VIX columns — every ``run_id``
(the BLAKE3 run identity) and every non-VIX column is byte-identical with or
without the stamper.
"""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import hashlib
import pickle
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from dataclasses import replace as dc_replace
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
import pytest
from brutex.data.models import CandleBar
from brutex.data.option_dedup import OptionDedupIndex
from brutex.engine import candidate as candidate_mod
from brutex.engine import cascade_wire as cw
from brutex.engine import robustness as robustness_mod
from brutex.engine import stage3_wire as stage3_mod
from brutex.engine import sweep_driver as driver_mod
from brutex.engine import sweep_orchestrator as orch_mod
from brutex.engine import sweep_parallel as parallel_mod
from brutex.engine import sweep_report as report_mod
from brutex.engine import sweep_run as run_mod
from brutex.engine import sweep_supervisor as supervisor_mod
from brutex.engine import trade_sim as trade_sim_mod
from brutex.engine import validation as validation_mod
from brutex.engine.exec_grid import build_exec_grid_map
from brutex.engine.reference_series import TradeReferenceStamper
from brutex.engine.reference_wire import aggregate_trade_vix
from brutex.engine.results_store import ResultsStore
from brutex.engine.rust_trade_sim import ResidentTradeSim
from brutex.engine.stage1a import build_bar_bits
from brutex.engine.sweep_audit import SweepAuditWriter
from brutex.engine.sweep_cache import SweepCache
from brutex.engine.sweep_checkpoint import SweepCheckpointStore
from brutex.engine.sweep_progress import SweepProgress
from brutex.engine.sweep_run import (
    ComboDedup,
    SweepComboError,
    SweepRunConfig,
    ranked_combo_to_result_row,
    run_persisted_sweep,
)
from brutex.engine.sweep_supervisor import SweepGuards, grid_identity, run_supervised_grid
from brutex.engine.sweep_watchdog import SweepWatchdog
from brutex.orchestrator.audit import AuditWriter
from brutex.ranking import RankedCombo
from brutex.strategies.types import Side
from brutex.vocabulary.bits import BIT_BAR_BULLISH
from structlog.testing import capture_logs

if TYPE_CHECKING:  # pragma: no cover - typing only (lazy numpy, see rust_trade_sim)
    from brutex.metrics.compute import ResultMetrics
    from brutex.metrics.types import ClosedTrade

_IST = timezone(timedelta(hours=5, minutes=30))


# ────────────────────────── shared fixtures ──────────────────────────


def _bar(day: int, minute: int, o: float, c: float) -> CandleBar:
    """Build one IST-session minute bar (stored UTC) with Decimal OHLC."""
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


def _trending_bars(n_days: int = 4, per_day: int = 60) -> list[CandleBar]:
    """A mostly-up drift with every-5th-day DOWN sessions (the sweep_run twin).

    Under the ONE worst-case fill law (DEC-FILL-WORST-CASE-ONLY-001) the
    ranked fixtures ride EOD-only exits (``sl/tp = None``): up days win via
    the #1743 session_gap close despite the entry-at-HIGH haircut, DOWN days
    keep ``profit_factor`` finite, and 45 days clears MIN_TRADES=30.
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


_BARS = _trending_bars()

#: The persisted-sweep fixture: big enough that BUY combos clear the hard rank
#: gates (mirrors tests/unit/test_engine_sweep_run.py's fixture sizing).
_BARS_BIG = _trending_bars(n_days=45, per_day=120)


def _vix_stamper(bars: Sequence[CandleBar]) -> TradeReferenceStamper:
    """A stamper whose VIX series covers EVERY bar timestamp of *bars*.

    Each instant's VIX close is a deterministic function of its position, so a
    stamp is checkable back against the expected level.
    """
    vix_bars = [
        CandleBar(
            timestamp=bar.timestamp,
            open=Decimal("14.00"),
            high=Decimal("15.00"),
            low=Decimal("13.00"),
            close=Decimal("14.00") + Decimal(i % 7) / Decimal(100),
            volume=0,
            open_interest=None,
        )
        for i, bar in enumerate(bars)
    ]
    return TradeReferenceStamper.from_vix_bars(vix_bars)


_STAMPER = _vix_stamper(_BARS)
_STAMPER_BIG = _vix_stamper(_BARS_BIG)


def _assert_trades_stamped(trades: Sequence[ClosedTrade]) -> None:
    """Every trade's stamps equal the _STAMPER series at its own instants."""
    assert trades, "fixture must produce trades"
    for trade in trades:
        assert trade.vix_at_entry == _STAMPER.vix.level_at(trade.entry_ts)
        assert trade.vix_at_exit == _STAMPER.vix.level_at(trade.exit_ts)
        assert trade.vix_at_entry is not None
        assert trade.vix_at_exit is not None


@contextmanager
def _capture_kwarg(
    monkeypatch: pytest.MonkeyPatch, module: object, name: str
) -> Generator[list[object], None, None]:
    """Record the ``reference_stamper`` kwarg of every call to ``module.name``.

    The wrapped original still runs, so the production chain stays real — only
    the seam crossing is observed.
    """
    captured: list[object] = []
    original = getattr(module, name)

    def _wrapper(*args: Any, **kwargs: Any) -> Any:
        captured.append(kwargs.get("reference_stamper", "OMITTED"))
        return original(*args, **kwargs)

    monkeypatch.setattr(module, name, _wrapper)
    yield captured


# ────────────────────────── ResidentTradeSim (the Rust seam) ──────────────────────────


class TestResidentTradeSimStamping:
    def test_default_reference_stamper_is_none(self) -> None:
        bar_bits = build_bar_bits(_BARS)
        sim = ResidentTradeSim(_BARS, bar_bits, tf_minutes=1, exec_bars_1m=_BARS)
        assert sim.reference_stamper is None

    def test_property_returns_the_bound_stamper(self) -> None:
        bar_bits = build_bar_bits(_BARS)
        sim = ResidentTradeSim(
            _BARS, bar_bits, tf_minutes=1, exec_bars_1m=_BARS, reference_stamper=_STAMPER
        )
        assert sim.reference_stamper is _STAMPER

    def test_rust_batch_trades_are_stamped(self) -> None:
        bar_bits = build_bar_bits(_BARS)
        sim = ResidentTradeSim(
            _BARS, bar_bits, tf_minutes=1, exec_bars_1m=_BARS, reference_stamper=_STAMPER
        )
        assert sim.rust_active  # the compiled kernel serves; the stamp is Python-side
        (trades,) = sim.simulate_masks([0], direction=Side.BUY, take_profit_paisa=50)
        _assert_trades_stamped(trades)

    def test_rust_batch_without_stamper_stamps_none(self) -> None:
        bar_bits = build_bar_bits(_BARS)
        sim = ResidentTradeSim(_BARS, bar_bits, tf_minutes=1, exec_bars_1m=_BARS)
        (trades,) = sim.simulate_masks([0], direction=Side.BUY, take_profit_paisa=50)
        assert trades
        assert all(t.vix_at_entry is None and t.vix_at_exit is None for t in trades)

    def test_fallback_arm_stamps_identically_to_rust_arm(self) -> None:
        bar_bits = build_bar_bits(_BARS)
        rust_sim = ResidentTradeSim(
            _BARS, bar_bits, tf_minutes=1, exec_bars_1m=_BARS, reference_stamper=_STAMPER
        )
        py_sim = ResidentTradeSim(
            _BARS, bar_bits, tf_minutes=1, exec_bars_1m=_BARS, reference_stamper=_STAMPER
        )
        py_sim._rust = None  # force the pure-Python oracle arm
        (rust_trades,) = rust_sim.simulate_masks([0], direction=Side.BUY, take_profit_paisa=50)
        (py_trades,) = py_sim.simulate_masks([0], direction=Side.BUY, take_profit_paisa=50)
        assert rust_trades == py_trades
        _assert_trades_stamped(py_trades)

    def test_stamped_output_otherwise_byte_identical(self) -> None:
        bar_bits = build_bar_bits(_BARS)
        plain = ResidentTradeSim(_BARS, bar_bits, tf_minutes=1, exec_bars_1m=_BARS)
        stamped = ResidentTradeSim(
            _BARS, bar_bits, tf_minutes=1, exec_bars_1m=_BARS, reference_stamper=_STAMPER
        )
        (plain_trades,) = plain.simulate_masks([0], direction=Side.BUY, take_profit_paisa=50)
        (stamped_trades,) = stamped.simulate_masks([0], direction=Side.BUY, take_profit_paisa=50)
        assert len(plain_trades) == len(stamped_trades)
        for p, s in zip(plain_trades, stamped_trades, strict=True):
            assert (p.pnl_paisa, p.mfe_paisa, p.mae_paisa, p.holding_bars) == (
                s.pnl_paisa,
                s.mfe_paisa,
                s.mae_paisa,
                s.holding_bars,
            )
            assert (p.entry_ts, p.exit_ts, p.direction) == (s.entry_ts, s.exit_ts, s.direction)

    def test_pair_method_stamps_both_arms_and_keeps_exit_indices(self) -> None:
        """The ADR-0017 merge-reconciliation pin: the exit-idx-native batch
        spine (``simulate_masks_with_exit_indices``, the method the sweep's
        chunk loop actually calls since DEC-SWEEP-EXIT-INDEX-FASTPATH-001)
        routes through the STAMPED converter on BOTH arms — the Rust batch
        post-stamps via ``_to_trade_and_exit`` and the fallback walker stamps
        natively — with the pairs' trades bit-identical across arms AND the
        certified exit-index stream intact and IDENTICAL to an unstamped run
        (stamping is a pure trade-field decoration, never an index input).
        """
        bar_bits = build_bar_bits(_BARS)
        rust_sim = ResidentTradeSim(
            _BARS, bar_bits, tf_minutes=1, exec_bars_1m=_BARS, reference_stamper=_STAMPER
        )
        py_sim = ResidentTradeSim(
            _BARS, bar_bits, tf_minutes=1, exec_bars_1m=_BARS, reference_stamper=_STAMPER
        )
        py_sim._rust = None  # force the pure-Python oracle arm
        plain_sim = ResidentTradeSim(_BARS, bar_bits, tf_minutes=1, exec_bars_1m=_BARS)
        assert rust_sim.rust_active
        ((rust_trades, rust_idx),) = rust_sim.simulate_masks_with_exit_indices(
            [0], direction=Side.BUY, take_profit_paisa=50
        )
        ((py_trades, py_idx),) = py_sim.simulate_masks_with_exit_indices(
            [0], direction=Side.BUY, take_profit_paisa=50
        )
        ((_, plain_idx),) = plain_sim.simulate_masks_with_exit_indices(
            [0], direction=Side.BUY, take_profit_paisa=50
        )
        _assert_trades_stamped(rust_trades)
        assert rust_trades == py_trades
        # The certified window emits TRUE indices on both arms, unmoved by
        # the stamper (byte-equal to the unstamped run's stream).
        assert rust_idx is not None and rust_idx == py_idx == plain_idx
        assert [t.exit_ts for t in rust_trades] == [_BARS[i].timestamp for i in rust_idx]

    def test_missing_vix_bar_stamps_none_never_fabricated(self) -> None:
        # A stamper covering only the FIRST bar: every real trade's instants
        # miss → both stamps None (Golden Rule #1 — never fabricated).
        sparse = TradeReferenceStamper.from_vix_bars(
            [
                CandleBar(
                    timestamp=_BARS[0].timestamp,
                    open=Decimal("14"),
                    high=Decimal("14"),
                    low=Decimal("14"),
                    close=Decimal("14"),
                    volume=0,
                    open_interest=None,
                )
            ]
        )
        bar_bits = build_bar_bits(_BARS)
        sim = ResidentTradeSim(
            _BARS, bar_bits, tf_minutes=1, exec_bars_1m=_BARS, reference_stamper=sparse
        )
        with capture_logs() as logs:
            (trades,) = sim.simulate_masks([0], direction=Side.BUY, take_profit_paisa=50)
        assert trades
        assert all(t.vix_at_entry is None and t.vix_at_exit is None for t in trades)
        assert any(e["event"] == "reference_series.vix_miss" for e in logs)


# ────────────────────────── structural oracle ≡ batch ──────────────────────────


class TestOracleEqualsBatchStructural:
    """The chunk-failure oracle replay reads the stamper FROM the sim itself."""

    def _cfg(self) -> SweepRunConfig:
        return SweepRunConfig(
            instrument="NIFTY",
            tf="1m",
            direction=Side.BUY,
            mode="A",
            condition_bits=[0, 1, 2],
            k=1,
            data_snapshot_hash="snap0001",
            indicator_version="ind-v1",
            code_commit_sha="deadbeef",
            tf_minutes=1,
            sl_paisa=80,
            tp_paisa=50,
        )

    def test_iter_mask_metrics_hands_sim_stamper_to_reattribution(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bar_bits = build_bar_bits(_BARS)
        sim = ResidentTradeSim(
            _BARS, bar_bits, tf_minutes=1, exec_bars_1m=_BARS, reference_stamper=_STAMPER
        )
        # The batch spine is `simulate_masks_with_exit_indices` since the
        # ADR-0017 exit-idx-native rewrite (`simulate_masks` is a thin
        # delegate over it) — the chunk loop calls the PAIR method, so the
        # failure injection targets it directly.
        monkeypatch.setattr(
            ResidentTradeSim,
            "simulate_masks_with_exit_indices",
            lambda self, *a, **kw: (_ for _ in ()).throw(ValueError("boom")),
        )
        captured: list[object] = []

        def _fake_reattribute(*args: Any, **kwargs: Any) -> Any:
            captured.append(kwargs["reference_stamper"])
            raise SweepComboError("stop")

        monkeypatch.setattr(run_mod, "_reattribute_chunk_failure", _fake_reattribute)
        with pytest.raises(SweepComboError, match="stop"):
            list(
                run_mod._iter_mask_metrics(
                    sim, [0], _BARS, bar_bits, self._cfg(), "bull", lambda mask: "0" * 64
                )
            )
        assert captured == [_STAMPER]

    def test_reattribution_oracle_replay_binds_the_handed_stamper(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bar_bits = build_bar_bits(_BARS)
        with (
            _capture_kwarg(monkeypatch, run_mod, "metrics_for_mask") as captured,
            pytest.raises(SweepComboError, match="batch/kernel-level failure"),
        ):
            run_mod._reattribute_chunk_failure(
                _BARS,
                bar_bits,
                [0, 1],
                self._cfg(),
                "bull",
                ValueError("batch boom"),
                lambda mask: "0" * 64,
                exec_bars_1m=_BARS,  # tf==1m fixed point
                exec_map=None,
                reference_stamper=_STAMPER,
            )
        # Every oracle replay of the chunk carried the sim's stamper.
        assert captured == [_STAMPER, _STAMPER]


# ────────────────────────── sweep_driver seam ──────────────────────────


class TestSweepDriverSeam:
    def _metrics(self, **kwargs: Any) -> ResultMetrics:
        bar_bits = build_bar_bits(_BARS)
        return driver_mod.metrics_for_mask(
            _BARS,
            bar_bits,
            0,
            direction=Side.BUY,
            tf_minutes=1,
            exec_bars_1m=_BARS,  # tf==1m fixed point
            stop_loss_paisa=80,
            take_profit_paisa=50,
            start_equity_paisa=10_000_000,
            **kwargs,
        )

    def test_metrics_for_mask_threads_the_stamper(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with _capture_kwarg(
            monkeypatch, driver_mod, "simulate_trades_with_exit_indices"
        ) as captured:
            self._metrics(reference_stamper=_STAMPER)
        assert captured == [_STAMPER]

    def test_metrics_for_mask_default_is_unstamped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with _capture_kwarg(
            monkeypatch, driver_mod, "simulate_trades_with_exit_indices"
        ) as captured:
            self._metrics()
        assert captured == [None]

    def test_evaluate_combo_threads_the_stamper(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with _capture_kwarg(
            monkeypatch, driver_mod, "simulate_trades_with_exit_indices"
        ) as captured:
            driver_mod.evaluate_combo(
                _BARS,
                0,
                direction=Side.BUY,
                tf_minutes=1,
                exec_bars_1m=_BARS,  # tf==1m fixed point
                take_profit_paisa=50,
                reference_stamper=_STAMPER,
            )
        assert captured == [_STAMPER]

    def test_sweep_masks_threads_the_stamper(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with _capture_kwarg(
            monkeypatch, driver_mod, "simulate_trades_with_exit_indices"
        ) as captured:
            driver_mod.sweep_masks(
                _BARS,
                [0, 1 << BIT_BAR_BULLISH],
                direction=Side.BUY,
                tf_minutes=1,
                exec_bars_1m=_BARS,  # tf==1m fixed point
                take_profit_paisa=50,
                reference_stamper=_STAMPER,
            )
        assert captured == [_STAMPER, _STAMPER]

    def test_rank_sweep_threads_the_stamper(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with _capture_kwarg(
            monkeypatch, driver_mod, "simulate_trades_with_exit_indices"
        ) as captured:
            driver_mod.rank_sweep(
                _BARS,
                [0],
                direction=Side.BUY,
                tf_minutes=1,
                exec_bars_1m=_BARS,  # tf==1m fixed point
                take_profit_paisa=50,
                reference_stamper=_STAMPER,
            )
        assert captured == [_STAMPER]

    def test_rank_sweep_over_bits_is_a_pure_conduit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with _capture_kwarg(monkeypatch, driver_mod, "sweep_masks") as captured:
            driver_mod.rank_sweep_over_bits(
                _BARS,
                [0, 1],
                1,
                direction=Side.BUY,
                tf_minutes=1,
                exec_bars_1m=_BARS,  # tf==1m fixed point
                take_profit_paisa=50,
                min_hits=0,
                reference_stamper=_STAMPER,
            )
        assert captured == [_STAMPER]

    def test_walk_forward_paths_thread_the_stamper(self, monkeypatch: pytest.MonkeyPatch) -> None:
        bars = _trending_bars(n_days=6, per_day=40)
        with _capture_kwarg(
            monkeypatch, driver_mod, "simulate_trades_with_exit_indices"
        ) as captured:
            driver_mod.evaluate_combo_walk_forward(
                bars,
                0,
                direction=Side.BUY,
                tf_minutes=1,
                exec_bars_1m=bars,  # tf==1m fixed point
                take_profit_paisa=50,
                train_days=3,
                test_days=1,
                step_days=1,
                reference_stamper=_STAMPER,
            )
        assert captured and all(c is _STAMPER for c in captured)

    def test_walk_forward_sweep_threads_the_stamper(self, monkeypatch: pytest.MonkeyPatch) -> None:
        bars = _trending_bars(n_days=6, per_day=40)
        with _capture_kwarg(
            monkeypatch, driver_mod, "simulate_trades_with_exit_indices"
        ) as captured:
            driver_mod.walk_forward_sweep(
                bars,
                [0],
                direction=Side.BUY,
                tf_minutes=1,
                exec_bars_1m=bars,  # tf==1m fixed point
                take_profit_paisa=50,
                train_days=3,
                test_days=1,
                step_days=1,
                reference_stamper=_STAMPER,
            )
        assert captured and all(c is _STAMPER for c in captured)


# ────────────────────────── validation / robustness / candidate ──────────────────────────


class TestStage3ValidatorsSeam:
    def test_validate_out_of_sample_threads_to_both_halves(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with _capture_kwarg(monkeypatch, validation_mod, "metrics_for_mask") as captured:
            validation_mod.validate_out_of_sample(
                _BARS,
                0,
                direction=Side.BUY,
                tf_minutes=1,
                exec_bars_1m=_BARS,  # tf==1m fixed point
                take_profit_paisa=50,
                reference_stamper=_STAMPER,
            )
        assert captured == [_STAMPER, _STAMPER]  # train + test

    def test_combo_returns_threads_the_stamper(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with _capture_kwarg(monkeypatch, robustness_mod, "simulate_trades") as captured:
            robustness_mod.combo_returns(
                _BARS,
                0,
                direction=Side.BUY,
                tf_minutes=1,
                exec_bars_1m=_BARS,  # tf==1m fixed point
                take_profit_paisa=50,
                reference_stamper=_STAMPER,
            )
        assert captured == [_STAMPER]

    def test_validate_candidate_threads_to_oos_and_cpcv(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with (
            _capture_kwarg(monkeypatch, candidate_mod, "validate_out_of_sample") as oos_captured,
            _capture_kwarg(monkeypatch, candidate_mod, "combo_returns") as returns_captured,
        ):
            candidate_mod.validate_candidate(
                _BARS,
                0,
                [],
                direction=Side.BUY,
                tf_minutes=1,
                exec_bars_1m=_BARS,  # tf==1m fixed point
                take_profit_paisa=50,
                reference_stamper=_STAMPER,
            )
        assert oos_captured == [_STAMPER]
        assert returns_captured == [_STAMPER]


def _ranked_combo() -> RankedCombo:
    """A gates-independent ranked combo over the small fixture (mask 0)."""
    metrics = driver_mod.evaluate_combo(
        _BARS, 0, direction=Side.BUY, tf_minutes=1, exec_bars_1m=_BARS, take_profit_paisa=50
    )
    return RankedCombo(mask=0, metrics=metrics, composite_score=0.0, deflated_sharpe=None)


# ────────────────────────── sweep_report seam ──────────────────────────


class TestSweepReportSeam:
    def test_sweep_report_threads_to_the_ranked_sweep(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with _capture_kwarg(monkeypatch, report_mod, "rank_sweep_over_bits") as captured:
            report_mod.sweep_report(
                _BARS,
                instrument="NIFTY",
                tf_label="1m",
                tf_minutes=1,
                exec_bars_1m=_BARS,  # tf==1m fixed point
                direction=Side.BUY,
                condition_bits=[0, 1],
                k=1,
                take_profit_paisa=50,
                min_hits=0,
                reference_stamper=_STAMPER,
            )
        assert captured == [_STAMPER]

    def test_sweep_report_bytes_identical_stamped_vs_unstamped(self) -> None:
        """The pin cited at ``cmd_sweep``'s sanctioned literal-``None`` site.

        ``brutex sweep`` renders a TEXT leaderboard and persists nothing — its
        literal ``reference_stamper=None`` is sanctioned BECAUSE the report
        surface carries no VIX field, so a stamp there is provably
        unobservable. This is the actual byte-equality regression that claim
        rides on (round-3 panel: the claim shipped in cli.py / MIGRATION.md
        #1621 / CLAUDE.md §2 Rule 3 citing a test that did not exist — the
        cascade ``serialize_wire_report`` A/B pinned a DIFFERENT surface).
        A future VIX column in the report text fails HERE and forces the
        conscious revert-lever decision (bind a stamper at ``cmd_sweep``).
        """
        stamped = report_mod.sweep_report(
            _BARS,
            instrument="NIFTY",
            tf_label="1m",
            tf_minutes=1,
            exec_bars_1m=_BARS,  # tf==1m fixed point
            direction=Side.BUY,
            condition_bits=[0, 1],
            k=1,
            take_profit_paisa=50,
            min_hits=0,
            validate_oos=True,  # the widest report surface: ranked table + OOS section
            reference_stamper=_STAMPER,
        )
        plain = report_mod.sweep_report(
            _BARS,
            instrument="NIFTY",
            tf_label="1m",
            tf_minutes=1,
            exec_bars_1m=_BARS,  # tf==1m fixed point
            direction=Side.BUY,
            condition_bits=[0, 1],
            k=1,
            take_profit_paisa=50,
            min_hits=0,
            validate_oos=True,
            reference_stamper=None,
        )
        assert stamped == plain

    def test_oos_section_threads_the_stamper(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ranked = [_ranked_combo()]
        with _capture_kwarg(monkeypatch, report_mod, "validate_out_of_sample") as captured:
            report_mod.oos_section(
                _BARS,
                ranked,
                direction=Side.BUY,
                tf_minutes=1,
                exec_bars_1m=_BARS,  # tf==1m fixed point
                stop_loss_paisa=None,
                take_profit_paisa=50,
                start_equity_paisa=10_000_000,
                proximity=None,
                prior_n=3,
                reference_stamper=_STAMPER,
            )
        assert captured == [_STAMPER] * len(ranked)

    def test_filter_oos_held_threads_the_stamper(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ranked = [_ranked_combo()]
        with _capture_kwarg(monkeypatch, report_mod, "validate_out_of_sample") as captured:
            report_mod.filter_oos_held(
                _BARS,
                ranked,
                direction=Side.BUY,
                tf_minutes=1,
                exec_bars_1m=_BARS,  # tf==1m fixed point
                stop_loss_paisa=None,
                take_profit_paisa=50,
                start_equity_paisa=10_000_000,
                proximity=None,
                prior_n=3,
                reference_stamper=_STAMPER,
            )
        assert captured == [_STAMPER] * len(ranked)


# ────────────────────────── sweep_run seam ──────────────────────────


def _run_cfg(**overrides: object) -> SweepRunConfig:
    base: dict[str, object] = {
        "instrument": "NIFTY",
        "tf": "1m",
        "direction": Side.BUY,
        "mode": "A",
        "condition_bits": [0, 1, 2],
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


def _run_sweep(
    tmp: Path, name: str, reference_stamper: TradeReferenceStamper | None
) -> tuple[Any, bytes]:
    """One persisted sweep run over the BIG fixture → (summary, parquet bytes)."""
    tmp.mkdir(parents=True, exist_ok=True)
    store = ResultsStore(path=tmp / f"{name}.parquet")
    with AuditWriter.open(tmp / f"{name}-audit.jsonl") as audit:
        summary = run_persisted_sweep(
            _BARS_BIG,
            _run_cfg(),
            store=store,
            dedup=ComboDedup(OptionDedupIndex(capacity=10_000, fpr_target=1e-6)),
            audit=audit,
            exec_bars_1m=_BARS_BIG,  # tf==1m fixed point
            reference_stamper=reference_stamper,
        )
    return summary, (tmp / f"{name}.parquet").read_bytes()


class TestSweepRunSeam:
    def test_ranked_combo_to_result_row_binds_the_stamper(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bar_bits = build_bar_bits(_BARS)
        with _capture_kwarg(monkeypatch, run_mod, "simulate_trades") as captured:
            ranked_combo_to_result_row(
                _ranked_combo(),
                run_id="0" * 64,
                instrument="NIFTY",
                tf="1m",
                direction="bull",
                mode="A",
                bars=_BARS,
                bar_bits=bar_bits,
                tf_minutes=1,
                exec_bars_1m=_BARS,  # tf==1m fixed point
                sl_paisa=None,
                tp_paisa=50,
                reference_stamper=_STAMPER,
            )
        assert captured == [_STAMPER]

    def test_run_persisted_sweep_binds_stamper_at_finalist_projection_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        finalist_stampers: list[object] = []
        inner_stampers: list[object] = []
        real_row = run_mod.ranked_combo_to_result_row
        real_build = run_mod._build_trade_sim

        def _spy_row(*args: Any, **kwargs: Any) -> Any:
            finalist_stampers.append(kwargs.get("reference_stamper", "OMITTED"))
            return real_row(*args, **kwargs)

        def _spy_build(*args: Any, **kwargs: Any) -> Any:
            inner_stampers.append(kwargs["reference_stamper"])
            return real_build(*args, **kwargs)

        monkeypatch.setattr(run_mod, "ranked_combo_to_result_row", _spy_row)
        monkeypatch.setattr(run_mod, "_build_trade_sim", _spy_build)
        summary, _ = _run_sweep(tmp_path, "bind", _STAMPER_BIG)
        assert summary.persisted > 0
        # Every persisted finalist bound the RUN's stamper …
        assert finalist_stampers == [_STAMPER_BIG] * summary.persisted
        # … while the discarded K-of-N inner stream stayed EXPLICITLY unstamped.
        assert inner_stampers == [None]

    def test_trade_sim_path_logs_vix_stamped_false_for_the_inner_stream(
        self, tmp_path: Path
    ) -> None:
        with capture_logs() as logs:
            _run_sweep(tmp_path, "log", _STAMPER_BIG)
        paths = [e for e in logs if e["event"] == "sweep_run.trade_sim_path"]
        assert paths and all(e["vix_stamped"] is False for e in paths)

    def test_build_trade_sim_logs_vix_stamped_true_when_bound(self) -> None:
        bar_bits = build_bar_bits(_BARS)
        with capture_logs() as logs:
            sim = run_mod._build_trade_sim(
                _BARS,
                bar_bits,
                _run_cfg(),
                "bull",
                exec_bars_1m=_BARS,  # tf==1m fixed point
                exec_map=None,
                reference_stamper=_STAMPER,
            )
        assert sim.reference_stamper is _STAMPER
        paths = [e for e in logs if e["event"] == "sweep_run.trade_sim_path"]
        assert paths == [paths[0]] and paths[0]["vix_stamped"] is True

    def test_persisted_rows_differ_only_in_the_vix_trio_stamped_vs_unstamped(
        self, tmp_path: Path
    ) -> None:
        """THE persisted-honesty pin, consciously updated for Stage C.

        Stage B pinned stamping to ZERO persisted-byte change; Stage C
        (DEC-VIX-STAMP-WIRE-001 ResultRow columns) deliberately moves the
        bytes — the finalist rows now CARRY the aggregated stamps — so the
        pin becomes: stamping changes the three nullable VIX columns and
        NOTHING else. Run identity stays byte-invariant (the spec's
        run-identity invariance regression: VIX is never a run-identity
        input, CLAUDE.md §2 Rule 3).
        """
        stamped_summary, stamped_bytes = _run_sweep(tmp_path / "a", "s", _STAMPER_BIG)
        plain_summary, plain_bytes = _run_sweep(tmp_path / "b", "s", None)
        assert stamped_summary.persisted == plain_summary.persisted > 0
        # The trio now lands on the stamped rows — the bytes MUST differ …
        assert stamped_summary.store_content_hash != plain_summary.store_content_hash
        assert hashlib.sha256(stamped_bytes).hexdigest() != hashlib.sha256(plain_bytes).hexdigest()
        # … but ONLY in the VIX trio: run_ids identical (identity invariance),
        # every other column identical, unstamped rows honestly all-None.
        stamped_rows = ResultsStore.load(tmp_path / "a" / "s.parquet").rows()
        plain_rows = ResultsStore.load(tmp_path / "b" / "s.parquet").rows()
        assert [r.run_id for r in stamped_rows] == [r.run_id for r in plain_rows]
        for stamped, plain in zip(stamped_rows, plain_rows, strict=True):
            assert (plain.vix_mean, plain.vix_min, plain.vix_max) == (None, None, None)
            assert stamped.vix_mean is not None  # _STAMPER_BIG covers every bar ts
            assert stamped.vix_min is not None
            assert stamped.vix_max is not None
            assert stamped.vix_min <= stamped.vix_mean <= stamped.vix_max
            assert dc_replace(stamped, vix_mean=None, vix_min=None, vix_max=None) == plain

    def test_finalist_row_trio_equals_aggregate_trade_vix_over_its_own_trades(
        self,
    ) -> None:
        """The attached trio IS the ONE COMMON fold over the projector's trades."""
        bar_bits = build_bar_bits(_BARS)
        rc = _ranked_combo()
        row = ranked_combo_to_result_row(
            rc,
            run_id="0" * 64,
            instrument="NIFTY",
            tf="1m",
            direction="bull",
            mode="A",
            bars=_BARS,
            bar_bits=bar_bits,
            tf_minutes=1,
            exec_bars_1m=_BARS,  # tf==1m fixed point
            sl_paisa=None,
            tp_paisa=50,
            reference_stamper=_STAMPER,
        )
        trades = trade_sim_mod.simulate_trades(
            _BARS,
            run_mod.mask_to_entry_signals(bar_bits, rc.mask),
            direction=Side.BUY,
            tf_minutes=1,
            exec_bars_1m=_BARS,  # tf==1m fixed point
            stop_loss_paisa=None,
            take_profit_paisa=50,
            reference_stamper=_STAMPER,
        )
        _assert_trades_stamped(trades)
        assert (row.vix_mean, row.vix_min, row.vix_max) == aggregate_trade_vix(trades)
        assert row.vix_mean is not None


# ────────────────────────── orchestrator / supervisor / parallel ──────────────────────────


def _grid(**overrides: object) -> orch_mod.SweepGridConfig:
    base: dict[str, object] = {
        "instruments": ["NIFTY"],
        "timeframes": [orch_mod.Timeframe("1minute", 1)],
        "directions": [Side.BUY],
        "k_values": [1],
        "condition_bits": [0, 1, 2],
        "stop_losses": [None],
        "take_profits": [None],
        "proximities": [None],
        "mode": "A",
        "data_snapshot_hash": "snap0001",
        "indicator_version": "ind-v1",
        "code_commit_sha": "deadbeef",
    }
    base.update(overrides)
    return orch_mod.SweepGridConfig(**base)  # type: ignore[arg-type]


def _bars_for(_instrument: str, _tf: str) -> list[CandleBar]:
    return _BARS


@contextmanager
def _guards(tmp_path: Path, grid: orch_mod.SweepGridConfig) -> Generator[SweepGuards, None, None]:
    audit = SweepAuditWriter.open(tmp_path / "sup.jsonl")
    cache = SweepCache.open(tmp_path / "cache", bloom_capacity=10_000)
    checkpoint = SweepCheckpointStore(
        path=tmp_path / "ckpt.json",
        sweep_id=grid_identity(grid),
        total_combos=orch_mod.count_sweep_tuples(grid),
    )
    guards = SweepGuards(
        audit=audit,
        progress=SweepProgress(),
        checkpoint=checkpoint,
        cache=cache,
        watchdog=SweepWatchdog(),
    )
    try:
        yield guards
    finally:
        audit.close()
        cache.close()


def _tree(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


class TestGridDriversSeam:
    def test_run_one_tuple_forwards_the_stamper(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        grid = _grid()
        (tuple_,) = list(orch_mod.iter_sweep_tuples(grid))
        with (
            _capture_kwarg(monkeypatch, orch_mod, "run_persisted_sweep") as captured,
            AuditWriter.open(tmp_path / "a.jsonl") as audit,
        ):
            orch_mod.run_one_tuple(
                grid,
                tuple_,
                bars_for=_bars_for,
                store_for=orch_mod.make_store_for(tmp_path / "out", grid),
                dedup=ComboDedup(OptionDedupIndex(capacity=10_000, fpr_target=1e-6)),
                audit=audit,
                reference_stamper=_STAMPER,
            )
        assert captured == [_STAMPER]

    def test_run_sweep_grid_forwards_the_stamper(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        grid = _grid()
        with (
            _capture_kwarg(monkeypatch, orch_mod, "run_one_tuple") as captured,
            AuditWriter.open(tmp_path / "a.jsonl") as audit,
        ):
            orch_mod.run_sweep_grid(
                grid,
                bars_for=_bars_for,
                store_for=orch_mod.make_store_for(tmp_path / "out", grid),
                dedup=ComboDedup(OptionDedupIndex(capacity=10_000, fpr_target=1e-6)),
                audit=audit,
                reference_stamper=_STAMPER,
            )
        assert captured == [_STAMPER]

    def test_run_supervised_grid_forwards_the_stamper(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        grid = _grid()
        with (
            _capture_kwarg(monkeypatch, supervisor_mod, "run_one_tuple") as captured,
            AuditWriter.open(tmp_path / "ingest.jsonl") as ingest,
            _guards(tmp_path, grid) as guards,
        ):
            run_supervised_grid(
                grid,
                bars_for=_bars_for,
                store_for=orch_mod.make_store_for(tmp_path / "out", grid),
                dedup=ComboDedup(OptionDedupIndex(capacity=10_000, fpr_target=1e-6)),
                ingest_audit=ingest,
                guards=guards,
                reference_stamper=_STAMPER,
            )
        assert captured == [_STAMPER]


class TestParallelSeam:
    def test_worker_context_with_stamper_is_spawn_picklable(self, tmp_path: Path) -> None:
        ctx = parallel_mod._WorkerContext(grid=_grid(), root=tmp_path, reference_stamper=_STAMPER)
        restored = pickle.loads(pickle.dumps(ctx))  # noqa: S301 — self-produced bytes
        assert restored.reference_stamper is not None
        # The pickled stamper resolves the SAME levels — value-preserving.
        ts = _BARS[0].timestamp
        assert restored.reference_stamper.vix.level_at(ts) == _STAMPER.vix.level_at(ts)

    def test_worker_context_stamper_defaults_to_none(self, tmp_path: Path) -> None:
        ctx = parallel_mod._WorkerContext(grid=_grid(), root=tmp_path)
        assert ctx.reference_stamper is None

    def test_run_tuple_in_worker_binds_the_context_stamper(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        grid = _grid()
        ctx = parallel_mod._WorkerContext(grid=grid, root=tmp_path, reference_stamper=_STAMPER)
        monkeypatch.setattr(parallel_mod, "_WORKER_CTX", ctx)
        (tuple_,) = list(orch_mod.iter_sweep_tuples(grid))
        with _capture_kwarg(monkeypatch, parallel_mod, "run_one_tuple") as captured:
            parallel_mod._run_tuple_in_worker(0, tuple_, _BARS, _BARS)
        assert captured == [_STAMPER]

    def test_two_worker_stamped_tree_equals_stamped_sequential_tree(self, tmp_path: Path) -> None:
        """THE spawn-parity pin (consciously updated for Stage C): a STAMPED
        parallel run over TWO spawn eval workers (``workers=3`` —
        ``ParallelGridDriver.run`` reserves one for the parent, so
        ``pool_size = workers - 1 = 2``; the round-7 review caught the prior
        ``workers=2`` claiming "2-spawn-worker" while spawning ONE) writes
        the byte-identical per-tuple Parquet tree a STAMPED sequential run
        writes — the stamper pickles into every spawn worker and stamps
        deterministically across genuinely interleaved workers.
        (Stage B compared stamped-vs-UNstamped trees, valid only while the
        stamp touched zero persisted bytes; the ResultRow VIX columns now
        carry it, so the honest cross-arm baseline is stamped-vs-stamped.)
        """
        grid = _grid(k_values=[1, 2])
        seq_tmp = tmp_path / "seq"
        seq_tmp.mkdir()
        with (
            AuditWriter.open(seq_tmp / "ingest.jsonl") as ingest,
            _guards(seq_tmp, grid) as guards,
        ):
            run_supervised_grid(
                grid,
                bars_for=_bars_for,
                store_for=orch_mod.make_store_for(seq_tmp / "out", grid),
                dedup=ComboDedup(OptionDedupIndex(capacity=10_000, fpr_target=1e-6)),
                ingest_audit=ingest,
                guards=guards,
                reference_stamper=_STAMPER,
            )
        par_tmp = tmp_path / "par"
        par_tmp.mkdir()
        with (
            AuditWriter.open(par_tmp / "ingest.jsonl") as ingest,
            _guards(par_tmp, grid) as guards,
        ):
            parallel_mod.run_parallel_grid(
                grid,
                bars_for=_bars_for,
                root=par_tmp / "out",
                ingest_audit=ingest,
                guards=guards,
                workers=3,  # pool_size = workers - 1 = TWO spawn eval workers
                reference_stamper=_STAMPER,
            )
        seq_tree = _tree(seq_tmp / "out")
        par_tree = _tree(par_tmp / "out")
        assert seq_tree and par_tree == seq_tree


# ────────────────────────── VIX-conflict tuple-skip (round-2 MAJOR) ──────────────────────────


def _alt_stamper() -> TradeReferenceStamper:
    """A stamper over the SAME instants as ``_STAMPER_BIG`` at DIFFERENT levels.

    Models the sanctioned VIX-lake movement between two runs of the same grid
    (a re-pull backfilled bars / the derived 1day was re-derived latest-wins):
    the redo re-aggregates a different trio under an UNCHANGED run identity.
    """
    vix_bars = [
        CandleBar(
            timestamp=bar.timestamp,
            open=Decimal("20.00"),
            high=Decimal("21.00"),
            low=Decimal("19.00"),
            close=Decimal("20.00") + Decimal(i % 5) / Decimal(100),
            volume=0,
            open_interest=None,
        )
        for i, bar in enumerate(_BARS_BIG)
    ]
    return TradeReferenceStamper.from_vix_bars(vix_bars)


def _bars_for_big(_instrument: str, _tf: str) -> list[CandleBar]:
    return _BARS_BIG


class TestVixConflictTupleSkip:
    """A VIX-lake-moved redo skips ONE tuple — never a whole-grid halt.

    Round-2 panel MAJOR (DEC-VIX-STAMP-WIRE-001): a sanctioned redo over an
    EXISTING per-tuple store (``make_store_for`` preloads it) after the VIX
    lake legitimately moved recomputes a different trio under the SAME
    ``run_id``. The typed :class:`VixTrioConflictError` must ride the #904
    tuple-skip contract in ALL THREE grid drivers — a raw
    ``ResultsStoreError`` sibling would escape ``except SweepComboError`` and
    halt the entire grid over a REFERENCE-ONLY annotation.

    Each test deletes the tuple's COMPLETION SENTINEL between the two runs:
    a sentineled tuple short-circuits as ``resumed_complete`` without ever
    re-simulating, so the genuine conflict window is the crash-window redo
    (store flushed, sentinel not yet written — exactly the state a mid-sweep
    kill leaves behind).
    """

    @staticmethod
    def _unsentinel(root: Path) -> Path:
        from brutex.engine.tuple_complete import completion_sentinel_path

        (store_path,) = sorted(root.rglob("*.parquet"))
        completion_sentinel_path(store_path).unlink()
        return store_path

    def test_run_sweep_grid_skips_the_conflicted_tuple_and_completes(self, tmp_path: Path) -> None:
        grid = _grid()
        root = tmp_path / "out"

        def _run(stamper: TradeReferenceStamper, audit_name: str) -> Any:
            with AuditWriter.open(tmp_path / audit_name) as audit:
                return orch_mod.run_sweep_grid(
                    grid,
                    bars_for=_bars_for_big,
                    store_for=orch_mod.make_store_for(root, grid),
                    dedup=ComboDedup(OptionDedupIndex(capacity=10_000, fpr_target=1e-6)),
                    audit=audit,
                    reference_stamper=stamper,
                )

        first = _run(_STAMPER_BIG, "a1.jsonl")
        assert first.tuples_failed == 0
        assert first.persisted > 0
        store_path = self._unsentinel(root)
        before = store_path.read_bytes()

        second = _run(_alt_stamper(), "a2.jsonl")
        # The ONE tuple is skipped + counted; the grid COMPLETES (no raise).
        assert second.tuples_failed == 1
        assert second.tuples_run == 0
        # The stored results are untouched (the raise precedes any flush).
        assert store_path.read_bytes() == before
        # The TUPLE_FAILED audit line names the typed error.
        audit_text = (tmp_path / "a2.jsonl").read_text(encoding="utf-8")
        assert "TUPLE_FAILED" in audit_text
        assert "VixTrioConflictError" in audit_text

    def test_run_supervised_grid_skips_the_conflicted_tuple(self, tmp_path: Path) -> None:
        grid = _grid()
        root = tmp_path / "out"

        def _run(stamper: TradeReferenceStamper, leg: str) -> Any:
            guards_tmp = tmp_path / leg
            guards_tmp.mkdir()
            with (
                AuditWriter.open(guards_tmp / "ingest.jsonl") as ingest,
                _guards(guards_tmp, grid) as guards,
            ):
                return run_supervised_grid(
                    grid,
                    bars_for=_bars_for_big,
                    store_for=orch_mod.make_store_for(root, grid),
                    dedup=ComboDedup(OptionDedupIndex(capacity=10_000, fpr_target=1e-6)),
                    ingest_audit=ingest,
                    guards=guards,
                    reference_stamper=stamper,
                )

        first = _run(_STAMPER_BIG, "g1")
        assert first.tuples_failed == 0
        assert first.persisted > 0
        self._unsentinel(root)
        # Fresh guards (fresh tuple cache/checkpoint) → the redo genuinely
        # re-runs the tuple instead of skipping it as cached.
        second = _run(_alt_stamper(), "g2")
        assert second.tuples_failed == 1
        assert second.tuples_run == 0

    def test_run_tuple_in_worker_returns_the_failed_marker_on_vix_conflict(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        grid = _grid()
        (tuple_,) = list(orch_mod.iter_sweep_tuples(grid))
        ctx_first = parallel_mod._WorkerContext(
            grid=grid, root=tmp_path / "out", reference_stamper=_STAMPER_BIG
        )
        monkeypatch.setattr(parallel_mod, "_WORKER_CTX", ctx_first)
        first = parallel_mod._run_tuple_in_worker(0, tuple_, _BARS_BIG, _BARS_BIG)
        assert first.failed_error is None
        assert first.summary is not None and first.summary.persisted > 0
        self._unsentinel(tmp_path / "out")

        ctx_redo = parallel_mod._WorkerContext(
            grid=grid, root=tmp_path / "out", reference_stamper=_alt_stamper()
        )
        monkeypatch.setattr(parallel_mod, "_WORKER_CTX", ctx_redo)
        second = parallel_mod._run_tuple_in_worker(1, tuple_, _BARS_BIG, _BARS_BIG)
        # The failed MARKER comes back — the future never breaks, so the
        # parent pool skips this tuple instead of tearing the whole grid down.
        assert second.summary is None
        assert second.failed_error is not None
        assert second.failed_error.startswith("VixTrioConflictError:")

    def test_unstamped_redo_over_a_stamped_store_stays_a_clean_noop(self, tmp_path: Path) -> None:
        """The directional law at the GRID level: value → None never conflicts."""
        grid = _grid()
        root = tmp_path / "out"

        def _run(stamper: TradeReferenceStamper | None, audit_name: str) -> Any:
            with AuditWriter.open(tmp_path / audit_name) as audit:
                return orch_mod.run_sweep_grid(
                    grid,
                    bars_for=_bars_for_big,
                    store_for=orch_mod.make_store_for(root, grid),
                    dedup=ComboDedup(OptionDedupIndex(capacity=10_000, fpr_target=1e-6)),
                    audit=audit,
                    reference_stamper=stamper,
                )

        first = _run(_STAMPER_BIG, "a1.jsonl")
        assert first.persisted > 0
        store_path = self._unsentinel(root)
        before = store_path.read_bytes()
        second = _run(None, "a2.jsonl")
        assert second.tuples_failed == 0
        assert second.tuples_run == 1
        assert store_path.read_bytes() == before  # byte-identical re-flush


# ────────────────────────── stage3_wire seam ──────────────────────────


class TestStage3WireSeam:
    def test_disabled_pass_never_invokes_stamper_for(self, tmp_path: Path) -> None:
        def _explodes() -> TradeReferenceStamper | None:
            raise AssertionError("stamper_for must not be invoked on a disabled pass")

        with AuditWriter.open(tmp_path / "a.jsonl") as audit:
            report = stage3_mod.run_stage3_pass(
                _grid(stage3_enable=False),
                results_root=tmp_path,
                bars_for=_bars_for,
                audit=audit,
                exchange="NSE",
                segment="INDEX",
                start="2026-01-01",
                end="2026-02-01",
                stamper_for=_explodes,
            )
        assert report.enabled is False

    def test_enabled_pass_resolves_stamper_for_exactly_once(self, tmp_path: Path) -> None:
        # Pre-import cma with its matplotlib-less UserWarning suppressed (the
        # repo's filterwarnings=error would otherwise escalate the import-time
        # warning; the stage3 suites do this via module-level importorskip).
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            pytest.importorskip("cma")
        calls: list[int] = []

        def _counting() -> TradeReferenceStamper | None:
            calls.append(1)
            return _STAMPER

        with AuditWriter.open(tmp_path / "a.jsonl") as audit:
            report = stage3_mod.run_stage3_pass(
                # stage3 requires Mode-A grid sl/tp ints (ADR-0007 condition 3).
                _grid(
                    stage3_enable=True,
                    stage3_max_fevals=4,
                    stage3_max_restarts=1,
                    stop_losses=[30],
                    take_profits=[50],
                ),
                results_root=tmp_path / "empty",  # no persisted stores → zero survivors
                bars_for=_bars_for,
                audit=audit,
                exchange="NSE",
                segment="INDEX",
                start="2026-01-01",
                end="2026-02-01",
                stamper_for=_counting,
            )
        assert report.enabled is True
        assert calls == [1]

    def test_make_evaluator_binds_the_stamper(self, monkeypatch: pytest.MonkeyPatch) -> None:
        bar_bits = build_bar_bits(_BARS)
        with _capture_kwarg(monkeypatch, stage3_mod, "metrics_for_mask") as captured:
            evaluate = stage3_mod._make_evaluator(
                bars=_BARS,
                bar_bits=bar_bits,
                mask=0,
                side=Side.BUY,
                tf_minutes=1,
                exec_bars_1m=_BARS,  # tf==1m fixed point
                exec_map=build_exec_grid_map(_BARS, _BARS, tf_minutes=1),
                start_equity_paisa=10_000_000,
                reference_stamper=_STAMPER,
                # LEGACY-MODE PIN (DEC-FILL-WORST-CASE-ONLY-001): the stamper wiring
            )
            evaluate(80, 50)
        assert captured == [_STAMPER]


# ────────────────────────── cascade_wire seam ──────────────────────────


class TestCascadeStreamsUnstamped:
    """The cascade chain carries NO stamper seam (round-6 review).

    Both cascade trade streams are DISCARDED: the candidate re-simulation's
    trades survive only as the :class:`CandidateInput` scalars/returns tuples
    (no trade object, and no VIX field, crosses the conversion) and the
    filter-#9 Monte-Carlo permutations are the thrown-away null distribution.
    Stamping either was pure wasted work with zero observable output — the
    manual replays even paid a full window-bounded VIX lake read per
    replay/click to feed it. These tests pin the deletion: every cascade
    ``simulate_trades`` call is an EXPLICIT literal-``None`` (the AST pin owns
    the exact counts) and no cascade entry point accepts a stamper kwarg, so
    the seam cannot silently return without a conscious review (the revert
    lever: add a VIX surface to the cascade report FIRST).
    """

    def _candidate_kwargs(self) -> dict[str, Any]:
        return {
            "run_id": "0" * 64,
            "mask": 0,
            "bars": _BARS,
            "bar_bits": build_bar_bits(_BARS),
            "direction_label": "bull",
            "tf_minutes": 1,
            "exec_bars_1m": _BARS,  # tf==1m fixed point
            "sl_paisa": None,
            "tp_paisa": 50,
            "start_equity_paisa": 10_000_000,
        }

    def test_candidate_re_sim_is_explicitly_unstamped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with _capture_kwarg(monkeypatch, trade_sim_mod, "simulate_trades") as captured:
            cw.build_candidate_input(**self._candidate_kwargs())
        # An explicit literal None — never an OMITTED silent default.
        assert captured == [None]

    def test_mc_permutation_stream_is_explicitly_unstamped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Build INSIDE the capture: the MC closure binds simulate_trades at
        # construction, so the spy must already be in place.
        with _capture_kwarg(monkeypatch, trade_sim_mod, "simulate_trades") as captured:
            candidate = cw.build_candidate_input(**self._candidate_kwargs())
            candidate.mc_evaluate(np.array([0, 3, 6], dtype=np.intp))
        assert captured == [None, None]

    def test_no_cascade_entry_point_accepts_a_stamper_kwarg(self) -> None:
        """Re-adding the dead seam (or a partial re-add) fails loudly here."""
        import inspect

        for fn in (
            cw.build_candidate_input,
            cw.candidates_for_spec,
            cw.run_cascade_over_specs,
            cw.run_and_write_from_specs,
            cw.run_and_write_from_manifest,
        ):
            params = inspect.signature(fn).parameters
            assert "reference_stamper" not in params, fn.__qualname__
            assert "stamper_for" not in params, fn.__qualname__

    def test_deleted_manifest_stamper_builder_stays_deleted(self) -> None:
        """``vix_stamper_for_manifest`` (a full VIX lake read per manual
        replay/click, feeding provably-discarded stamps) must not resurrect
        under the same name without a conscious review."""
        assert not hasattr(cw, "vix_stamper_for_manifest")
        assert "vix_stamper_for_manifest" not in cw.__all__


class TestManifestWindowDt:
    def test_manifest_window_dt_naive_is_utc(self) -> None:
        parsed = cw._manifest_window_dt("2026-01-05")
        assert parsed == datetime(2026, 1, 5, tzinfo=UTC)

    def test_manifest_window_dt_aware_is_preserved(self) -> None:
        parsed = cw._manifest_window_dt("2026-01-05T09:15:00+05:30")
        assert parsed.utcoffset() == timedelta(hours=5, minutes=30)
