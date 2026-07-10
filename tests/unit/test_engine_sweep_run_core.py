"""Slice-2 fold-arm WIRING integration — DESIGN_FINAL_RUST_SWEEP_CORE.md §5/§7.

Drives ``run_persisted_sweep`` end-to-end over the four arm combinations
(OFF / ENUM / CORE / BOTH — byte-identical stores, positive AND zero-count
engaged-event pins), the disabled-arm POISON goldens, the domain-VALID
poisoned-fold twins (belt-off hash MOVES; belt-on pinned bytes + autorevert +
tuple restart with 3 audit lines + tallies byte-equal to the pure arm;
during-burn-in ``self_check_mismatch``), the §1.3 decode-refusal poison, the
``kernel_panic_exception`` / ``fold_work_ceiling`` maps, the flag routing
(``zero_trade`` / ``python_required``), the bounded-rank arm (position carry
+ serve reconciliation running byte-identically), the burn-in / belt
disambiguation branches, the spot-check sampled counts, and a cross-arm
hypothesis differential over the full metrics column set.
"""

from __future__ import annotations

from collections.abc import Iterator, MutableMapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import fno_math
import pytest
from brutex.data.models import CandleBar
from brutex.data.option_dedup import OptionDedupIndex
from brutex.engine import sweep_core, sweep_run
from brutex.engine.results_store import ResultsStore
from brutex.engine.sweep_core import (
    SWEEP_RUST_CORE_BELT_ENV,
    SWEEP_RUST_CORE_ENV,
    SWEEP_RUST_ENUM_ENV,
    metrics_equal,
    reset_enum_self_check_for_tests,
    reset_observability_memo_for_tests,
    reset_self_check_for_tests,
)
from brutex.engine.sweep_kernel import ResidentSweepKernel
from brutex.engine.sweep_run import (
    ComboDedup,
    SweepComboError,
    SweepRunConfig,
    SweepRunSummary,
    run_persisted_sweep,
)
from brutex.engine.sweep_stream import SurvivorColumns, rank_survivor_columns
from brutex.metrics.compute import ResultMetrics
from brutex.orchestrator.audit import AuditWriter
from brutex.strategies.types import Side
from hypothesis import given, settings
from hypothesis import strategies as st
from structlog.testing import capture_logs

_IST = timezone(timedelta(hours=5, minutes=30))
_DIGEST = "00c0ffee00c0ffee"


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for env in (SWEEP_RUST_CORE_ENV, SWEEP_RUST_ENUM_ENV, SWEEP_RUST_CORE_BELT_ENV):
        monkeypatch.delenv(env, raising=False)
    reset_enum_self_check_for_tests()
    reset_self_check_for_tests()
    reset_observability_memo_for_tests()
    yield
    reset_enum_self_check_for_tests()
    reset_self_check_for_tests()
    reset_observability_memo_for_tests()


def _bar(day: int, minute: int, o: float, c: float) -> CandleBar:
    ts = (
        datetime(2026, 1, 5, 9, 16, tzinfo=_IST) + timedelta(days=day, minutes=minute)
    ).astimezone(UTC)
    return CandleBar(
        timestamp=ts,
        open=Decimal(str(round(o, 2))),
        high=Decimal(str(round(max(o, c) + 0.5, 2))),
        low=Decimal(str(round(min(o, c) - 0.5, 2))),
        close=Decimal(str(round(c, 2))),
        volume=100,
        open_interest=None,
    )


def _bars(n_days: int = 3, per_day: int = 80) -> list[CandleBar]:
    bars: list[CandleBar] = []
    for d in range(n_days):
        price = 100.0
        for m in range(per_day):
            close = price + (0.6 if m % 3 else -0.3)
            bars.append(_bar(d, m, price, close))
            price = close
    return bars


def _crafted_kernel(
    bars: Sequence[CandleBar], n_bits: int = 5, dead_bit: int | None = None
) -> ResidentSweepKernel:
    """A real kernel with HAND-CRAFTED bar_bits: bit b fires every (b+2) bars;
    *dead_bit* (if given) never fires — the zero-trade routing fixture."""
    kernel = ResidentSweepKernel(bars)
    kernel.bar_bits[:] = [
        sum(1 << b for b in range(n_bits) if b != dead_bit and i % (b + 2) == 0)
        for i in range(len(bars))
    ]
    return kernel


def _cfg(**overrides: object) -> SweepRunConfig:
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
        "sl_paisa": 80,
        "tp_paisa": 50,
        "min_hits": 0,
        "top_k": 3,
    }
    base.update(overrides)
    return SweepRunConfig(**base)  # type: ignore[arg-type]


def _run(
    tmp_path: Path,
    tag: str,
    bars: Sequence[CandleBar],
    cfg: SweepRunConfig,
    *,
    kernel: ResidentSweepKernel | None = None,
) -> tuple[SweepRunSummary, list[MutableMapping[str, Any]], Path]:
    store = ResultsStore(path=tmp_path / f"{tag}.parquet")
    audit_path = tmp_path / f"{tag}.jsonl"
    with capture_logs() as logs, AuditWriter.open(audit_path) as audit:
        summary = run_persisted_sweep(
            bars,
            cfg,
            store=store,
            dedup=ComboDedup(OptionDedupIndex(capacity=10_000, fpr_target=1e-6)),
            audit=audit,
            kernel=kernel,
            trial_stats_digest=_DIGEST,
        )
    return summary, logs, audit_path


def _events(logs: Sequence[MutableMapping[str, Any]], name: str) -> list[MutableMapping[str, Any]]:
    return [e for e in logs if e["event"] == name]


def _tallies(summary: SweepRunSummary) -> tuple[int, ...]:
    return (
        summary.enumerated,
        summary.conflict_pruned,
        summary.skipped_dedup,
        summary.evaluated,
        summary.ranked,
        summary.persisted,
        summary.backtested,
        summary.minhits_dropped,
    )


def _proxy_module(core_factory: type | object) -> object:
    class _Module:
        ResidentSweepCore = core_factory
        ResidentSweepCoreU128 = core_factory
        SWEEP_CORE_ABI_VERSION = fno_math.SWEEP_CORE_ABI_VERSION
        SWEEP_CORE_FOLD_INT_FIELDS = tuple(fno_math.SWEEP_CORE_FOLD_INT_FIELDS)
        SWEEP_CORE_FOLD_FLOAT_FIELDS = tuple(fno_math.SWEEP_CORE_FOLD_FLOAT_FIELDS)

    return _Module()


class _WrappedCore:
    """A real ResidentSweepCore proxy — subclasses poison one surface."""

    def __init__(self, *args: Any) -> None:
        self._real = fno_math.ResidentSweepCore(*args)

    def n_bars(self) -> int:
        return self._real.n_bars()

    def enumerate_prefilter(self, *args: Any) -> object:
        return self._real.enumerate_prefilter(*args)

    def fold_block(self, *args: Any) -> tuple[int, int, bytes, bytes, bytes]:
        return self._real.fold_block(*args)


class _NetPnlPoisonCore(_WrappedCore):
    """Domain-VALID poison: the first PLAIN row's net_pnl int +1 (shapes clean)."""

    def fold_block(self, *args: Any) -> tuple[int, int, bytes, bytes, bytes]:
        from array import array

        abi, scans, ints_le, floats_le, flags = self._real.fold_block(*args)
        ints = array("q")
        ints.frombytes(ints_le)
        for row, flag in enumerate(flags):
            if flag == 0:
                ints[row * 15 + 1] += 1
                break
        return abi, scans, ints.tobytes(), floats_le, flags


class _TruncatedFlagsCore(_WrappedCore):
    """Domain-INVALID poison: one flag row dropped (§1.3 row_count_mismatch)."""

    def fold_block(self, *args: Any) -> tuple[int, int, bytes, bytes, bytes]:
        abi, scans, ints_le, floats_le, flags = self._real.fold_block(*args)
        return abi, scans, ints_le, floats_le, flags[:-1]


class _PanicCore(_WrappedCore):
    def fold_block(self, *args: Any) -> tuple[int, int, bytes, bytes, bytes]:
        panic_cls = type("PanicException", (Exception,), {})
        raise panic_cls("explicit panic")


class _CeilingCore(_WrappedCore):
    def fold_block(self, *args: Any) -> tuple[int, int, bytes, bytes, bytes]:
        raise ValueError("fold_block: fold_work_ceiling (2**28 cells exceeded)")


class _PoisonedEnumCore(_WrappedCore):
    """Enum poison twin: enumerate_prefilter must never be consumed ENUM=0."""

    def enumerate_prefilter(self, *args: Any) -> object:
        raise AssertionError("enumerate_prefilter consumed with ENUM off")


def _close_burnin() -> None:
    reset_self_check_for_tests(total_remaining=0, plain_remaining=0)


class TestArmCombinationGoldens:
    """§7.1: four arms byte-identical; engaged events pinned BOTH directions."""

    def test_four_arms_byte_identical_with_negative_event_pins(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bars = _bars()
        cfg = _cfg()
        arms = {
            "off": ("0", "0"),
            "enum": ("1", "0"),
            "core": ("0", "1"),
            "both": ("1", "1"),
        }
        results: dict[str, tuple[SweepRunSummary, list[MutableMapping[str, Any]]]] = {}
        for tag, (enum_flag, core_flag) in arms.items():
            reset_enum_self_check_for_tests()
            reset_self_check_for_tests()
            reset_observability_memo_for_tests()
            monkeypatch.setenv(SWEEP_RUST_ENUM_ENV, enum_flag)
            monkeypatch.setenv(SWEEP_RUST_CORE_ENV, core_flag)
            summary, logs, _ = _run(tmp_path, tag, bars, cfg)
            results[tag] = (summary, logs)
        baseline = results["off"][0]
        for tag, (summary, logs) in results.items():
            assert summary.store_content_hash == baseline.store_content_hash, tag
            assert _tallies(summary) == _tallies(baseline), tag
            enum_engaged = len(_events(logs, "sweep_rust_enum.engaged"))
            core_engaged = len(_events(logs, "sweep_rust_core.engaged"))
            expect_enum = 1 if tag in ("enum", "both") else 0
            expect_core = 1 if tag in ("core", "both") else 0
            assert (enum_engaged, core_engaged) == (expect_enum, expect_core), tag
            assert not _events(logs, "sweep_rust_core.kernel_autoreverted"), tag
            assert not _events(logs, "sweep_rust_enum.kernel_autoreverted"), tag
        # Per-tuple observability: the core arm's path event fires with
        # core_active=True; the OFF arm reads core_active=False.
        core_path = _events(results["core"][1], "sweep_run.sweep_core_path")
        assert len(core_path) == 1 and core_path[0]["core_active"] is True
        off_path = _events(results["off"][1], "sweep_run.sweep_core_path")
        assert len(off_path) == 1 and off_path[0]["core_active"] is False

    def test_default_off_never_loads_kernel_module(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _spy() -> None:
            raise AssertionError("kernel module loaded on the default-OFF path")

        monkeypatch.setattr(sweep_core, "_load_kernel_module", _spy)
        summary, logs, _ = _run(tmp_path, "defaultoff", _bars(), _cfg())
        assert summary.persisted > 0
        assert not _events(logs, "sweep_rust_core.engaged")
        assert not _events(logs, "sweep_rust_enum.engaged")

    def test_disabled_arm_poison_goldens(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # §7.1 the runbook's teeth: a poisoned fold with CORE=0/ENUM=1 (and a
        # poisoned enum with ENUM=0/CORE=1) reproduce the literals byte-for-byte.
        bars = _bars()
        cfg = _cfg()
        baseline, _, _ = _run(tmp_path, "base", bars, cfg)

        monkeypatch.setenv(SWEEP_RUST_ENUM_ENV, "1")
        monkeypatch.setenv(SWEEP_RUST_CORE_ENV, "0")
        monkeypatch.setattr(
            sweep_core, "_load_kernel_module", lambda: _proxy_module(_NetPnlPoisonCore)
        )
        reset_enum_self_check_for_tests(masks_remaining=0)  # shadow closed
        poisoned_fold, logs, _ = _run(tmp_path, "enumonly", bars, cfg)
        assert poisoned_fold.store_content_hash == baseline.store_content_hash
        assert not _events(logs, "sweep_rust_core.engaged")

        reset_observability_memo_for_tests()
        monkeypatch.setenv(SWEEP_RUST_ENUM_ENV, "0")
        monkeypatch.setenv(SWEEP_RUST_CORE_ENV, "1")
        monkeypatch.setattr(
            sweep_core, "_load_kernel_module", lambda: _proxy_module(_PoisonedEnumCore)
        )
        _close_burnin()
        poisoned_enum, logs, _ = _run(tmp_path, "coreonly", bars, cfg)
        assert poisoned_enum.store_content_hash == baseline.store_content_hash
        assert len(_events(logs, "sweep_rust_core.engaged")) == 1


class TestPoisonedFoldTwins:
    """§7.1 inverted teeth: the seam genuinely consumes the kernel."""

    def test_belt_off_poison_past_burnin_moves_hash(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bars = _bars()
        cfg = _cfg()
        baseline, _, _ = _run(tmp_path, "clean", bars, cfg)
        monkeypatch.setenv(SWEEP_RUST_CORE_ENV, "1")
        monkeypatch.setenv(SWEEP_RUST_CORE_BELT_ENV, "0")
        monkeypatch.setattr(
            sweep_core, "_load_kernel_module", lambda: _proxy_module(_NetPnlPoisonCore)
        )
        _close_burnin()
        poisoned, logs, _ = _run(tmp_path, "poisoned", bars, cfg)
        assert poisoned.store_content_hash != baseline.store_content_hash
        assert len(_events(logs, "sweep_rust_core.engaged")) == 1
        assert not _events(logs, "sweep_rust_core.kernel_autoreverted")

    def test_belt_on_poison_restarts_with_pinned_bytes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bars = _bars()
        cfg = _cfg()
        baseline, _, baseline_audit = _run(tmp_path, "clean", bars, cfg)
        monkeypatch.setenv(SWEEP_RUST_CORE_ENV, "1")
        monkeypatch.setattr(
            sweep_core, "_load_kernel_module", lambda: _proxy_module(_NetPnlPoisonCore)
        )
        _close_burnin()
        poisoned, logs, audit_path = _run(tmp_path, "belted", bars, cfg)
        assert poisoned.store_content_hash == baseline.store_content_hash
        assert _tallies(poisoned) == _tallies(baseline)
        assert len(_events(logs, "sweep_rust_core.kernel_autoreverted")) == 1
        restarts = _events(logs, "sweep_rust_core.tuple_restart")
        assert [str(e["reason"]) for e in restarts] == ["belt_mismatch"]
        # Restart-shape pins: exactly 3 audit lines, SWEEP_START never re-emitted.
        lines = [ln for ln in audit_path.read_text().splitlines() if ln.strip()]
        base_lines = [ln for ln in baseline_audit.read_text().splitlines() if ln.strip()]
        assert len(lines) == len(base_lines) == 3
        path_events = _events(logs, "sweep_run.sweep_core_path")
        assert len(path_events) == 1
        assert path_events[0]["restart"] is True
        assert path_events[0]["core_active"] is False

    def test_poison_during_burnin_autoreverts_and_ships_oracle(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bars = _bars()
        cfg = _cfg()
        baseline, _, _ = _run(tmp_path, "clean", bars, cfg)
        monkeypatch.setenv(SWEEP_RUST_CORE_ENV, "1")
        monkeypatch.setattr(
            sweep_core, "_load_kernel_module", lambda: _proxy_module(_NetPnlPoisonCore)
        )
        # burn-in OPEN (default reset) — the first poisoned plain row must
        # mismatch, latch with reason self_check_mismatch, ship the oracle,
        # and serve the remainder mid-tuple on the spine: bytes pinned.
        poisoned, logs, _ = _run(tmp_path, "burnin", bars, cfg)
        assert poisoned.store_content_hash == baseline.store_content_hash
        reverts = _events(logs, "sweep_rust_core.kernel_autoreverted")
        assert [str(e["reason"]) for e in reverts] == ["self_check_mismatch"]
        assert not _events(logs, "sweep_rust_core.tuple_restart")

    def test_decode_refusal_poison_restarts_with_pinned_bytes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bars = _bars()
        cfg = _cfg()
        baseline, _, _ = _run(tmp_path, "clean", bars, cfg)
        monkeypatch.setenv(SWEEP_RUST_CORE_ENV, "1")
        monkeypatch.setattr(
            sweep_core, "_load_kernel_module", lambda: _proxy_module(_TruncatedFlagsCore)
        )
        _close_burnin()
        poisoned, logs, _ = _run(tmp_path, "decode", bars, cfg)
        assert poisoned.store_content_hash == baseline.store_content_hash
        refusals = [
            e
            for e in logs
            if e["event"] == "sweep_rust_core.fallback"
            and str(e.get("reason")) == "kernel_result_refused"
        ]
        assert len(refusals) == 1
        assert refusals[0]["refusal"] == "row_count_mismatch"
        assert [str(e["reason"]) for e in _events(logs, "sweep_rust_core.tuple_restart")] == [
            "kernel_result_refused:row_count_mismatch"
        ]

    def test_kernel_panic_exception_fails_the_tuple(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(SWEEP_RUST_CORE_ENV, "1")
        monkeypatch.setattr(sweep_core, "_load_kernel_module", lambda: _proxy_module(_PanicCore))
        _close_burnin()
        with pytest.raises(SweepComboError, match="kernel_panic_exception"):
            _run(tmp_path, "panic", _bars(), _cfg())

    def test_fold_work_ceiling_maps_to_restart(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bars = _bars()
        cfg = _cfg()
        baseline, _, _ = _run(tmp_path, "clean", bars, cfg)
        monkeypatch.setenv(SWEEP_RUST_CORE_ENV, "1")
        monkeypatch.setattr(sweep_core, "_load_kernel_module", lambda: _proxy_module(_CeilingCore))
        _close_burnin()
        summary, logs, _ = _run(tmp_path, "ceiling", bars, cfg)
        assert summary.store_content_hash == baseline.store_content_hash
        assert [str(e["reason"]) for e in _events(logs, "sweep_rust_core.tuple_restart")] == [
            "fold_work_ceiling"
        ]


class TestFlagRouting:
    """§1.4 row routing: zero_trade + python_required arms, byte-identical."""

    def test_zero_trade_mask_byte_identical(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bars = _bars()
        cfg = _cfg()
        kernel = _crafted_kernel(bars, dead_bit=4)  # mask 1<<4 → zero trades
        baseline, _, _ = _run(tmp_path, "off", bars, cfg, kernel=kernel)
        monkeypatch.setenv(SWEEP_RUST_CORE_ENV, "1")
        core, logs, _ = _run(tmp_path, "core", bars, cfg, kernel=kernel)
        assert core.store_content_hash == baseline.store_content_hash
        assert len(_events(logs, "sweep_rust_core.engaged")) == 1

    def test_python_required_envelope_byte_identical(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bars = _bars()
        cfg = _cfg(start_equity_paisa=(1 << 52) - 1)
        baseline, _, _ = _run(tmp_path, "off", bars, cfg)
        monkeypatch.setenv(SWEEP_RUST_CORE_ENV, "1")
        core, logs, _ = _run(tmp_path, "core", bars, cfg)
        assert core.store_content_hash == baseline.store_content_hash
        assert len(_events(logs, "sweep_rust_core.engaged")) == 1


class TestBurninAndBeltDisambiguation:
    def test_burnin_oracle_disagreement_fails_the_tuple(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Poison the PURE oracle arm: served == off-arm ≠ pure ⇒ class (1).
        monkeypatch.setenv(SWEEP_RUST_CORE_ENV, "1")
        real = sweep_run._pure_oracle_one

        def _perturbed(*args: object, **kwargs: object) -> ResultMetrics:
            metrics = real(*args, **kwargs)  # type: ignore[arg-type]
            return replace(metrics, net_pnl_paisa=metrics.net_pnl_paisa + 1)

        monkeypatch.setattr(sweep_run, "_pure_oracle_one", _perturbed)
        with pytest.raises(SweepComboError, match="burnin_oracle_disagreement"):
            _run(tmp_path, "disagree", _bars(), _cfg())

    def test_belt_preexisting_divergence_ships_without_restart(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bars = _bars()
        cfg = _cfg()
        baseline, _, _ = _run(tmp_path, "off", bars, cfg)
        monkeypatch.setenv(SWEEP_RUST_CORE_ENV, "1")
        real = sweep_run._pure_oracle_one

        def _perturbed(*args: object, **kwargs: object) -> ResultMetrics:
            metrics = real(*args, **kwargs)  # type: ignore[arg-type]
            return replace(metrics, net_pnl_paisa=metrics.net_pnl_paisa + 1)

        monkeypatch.setattr(sweep_run, "_pure_oracle_one", _perturbed)
        _close_burnin()
        summary, logs, _ = _run(tmp_path, "preexist", bars, cfg)
        assert summary.store_content_hash == baseline.store_content_hash
        assert len(_events(logs, "sweep_rust_core.belt_preexisting_divergence")) >= 1
        assert not _events(logs, "sweep_rust_core.tuple_restart")
        assert not _events(logs, "sweep_rust_core.kernel_autoreverted")


class TestSpotCheckSampling:
    def test_spot_check_samples_four_non_finalists(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bars = _bars()
        kernel = _crafted_kernel(bars, n_bits=6)
        cfg = _cfg(condition_bits=[0, 1, 2, 3, 4, 5], k=2, top_k=3)
        monkeypatch.setenv(SWEEP_RUST_CORE_ENV, "1")
        _close_burnin()
        summary, logs, _ = _run(tmp_path, "spot", bars, cfg, kernel=kernel)
        path_events = _events(logs, "sweep_run.sweep_core_path")
        assert len(path_events) == 1
        assert path_events[0]["spot_check_sampled"] == 4
        assert summary.persisted >= 1

    def test_empty_pool_samples_zero(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # §5 degenerate pool: kept == 0 (min_hits kills all) → sampled 0,
        # a real covered branch — never a crash, never a silent no-op.
        monkeypatch.setenv(SWEEP_RUST_CORE_ENV, "1")
        _close_burnin()
        summary, logs, _ = _run(tmp_path, "emptypool", _bars(), _cfg(min_hits=10**6))
        path_events = _events(logs, "sweep_run.sweep_core_path")
        assert path_events[0]["spot_check_sampled"] == 0
        assert path_events[0]["core_active"] is False
        assert summary.backtested == 0


class TestBoundedArm:
    def test_bounded_core_byte_identical_and_reconciled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bars = _bars()
        cfg = _cfg()
        baseline, _, _ = _run(tmp_path, "batchoff", bars, cfg)
        monkeypatch.setenv("BRUTEX_SWEEP_BOUNDED_RANK", "on")
        bounded_off, _, _ = _run(tmp_path, "boundedoff", bars, cfg)
        assert bounded_off.store_content_hash == baseline.store_content_hash
        monkeypatch.setenv(SWEEP_RUST_CORE_ENV, "1")
        _close_burnin()
        bounded_core, logs, _ = _run(tmp_path, "boundedcore", bars, cfg)
        assert bounded_core.store_content_hash == baseline.store_content_hash
        assert len(_events(logs, "sweep_rust_core.engaged")) == 1
        assert not _events(logs, "sweep_rust_core.tuple_restart")

    def test_bounded_finalist_position_carry(self) -> None:
        # §7.4: mask_at(position) == mask; the field is write-only for ranking.
        bars = _bars()
        cfg = _cfg()
        from brutex.engine.stage1a import build_bar_bits
        from brutex.engine.sweep_driver import metrics_for_mask

        bar_bits = build_bar_bits(
            bars, proximity=cfg.proximity, prior_n=cfg.prior_n, fib_anchor=cfg.fib_anchor
        )
        columns = SurvivorColumns(with_overfit=False)
        for mask in (1 << b for b in range(5)):
            metrics = metrics_for_mask(
                bars,
                bar_bits,
                mask,
                direction=Side.BUY,
                tf_minutes=1,
                stop_loss_paisa=80,
                take_profit_paisa=50,
                start_equity_paisa=10_000_000,
            )
            columns.append(mask, metrics)
        result = rank_survivor_columns(columns, overfit=None, top_k=2)
        assert result.finalists  # the arm-golden fixture ranks >= 1 mask
        for finalist in result.finalists:
            assert finalist.position >= 0
            assert columns.mask_at(finalist.position) == finalist.mask


class TestCrossArmProperty:
    """§7.3: the full metrics column set is arm-invariant (hypothesis)."""

    @settings(max_examples=8, deadline=None)
    @given(seed=st.integers(min_value=0, max_value=2**16))
    def test_core_generator_matches_spine(self, seed: int) -> None:
        import random

        rng = random.Random(seed)
        bars: list[CandleBar] = []
        price = 100.0
        for m in range(60):
            close = max(1.0, price + rng.uniform(-0.8, 0.9))
            bars.append(_bar(0, m, price, close))
            price = close
        bar_bits = [rng.randrange(16) for _ in range(len(bars))]
        cfg = _cfg()
        from brutex.engine.rust_trade_sim import ResidentTradeSim

        sim = ResidentTradeSim(bars, bar_bits, tf_minutes=1)
        masks = [0b0001, 0b0010, 0b0100, 0b1000, 0b0011]
        reset_self_check_for_tests()
        ctx = sweep_core.core_fold_context(
            bars=bars,
            bar_bits=bar_bits,
            condition_bits=[0, 1, 2, 3],
            tf_minutes=1,
            n_masks=4,
            digest_path=True,
            top_k=3,
            plan=sweep_core.CorePlan(
                requested=True,
                belt=True,
                fold_cells=sweep_core.DEFAULT_FOLD_WORK_CELLS,
                oracle_ns_per_bar=sweep_core.DEFAULT_ORACLE_NS_PER_BAR,
            ),
        )
        assert ctx is not None
        spine = list(
            sweep_run._iter_mask_metrics(sim, masks, bars, bar_bits, cfg, "bull", lambda m: "rid")
        )
        core = list(
            sweep_run._iter_mask_metrics_core(
                ctx,
                sim,
                masks,
                bars,
                bar_bits,
                cfg,
                "bull",
                lambda m: "rid",
                retained={},
                candidates=frozenset(),
            )
        )
        assert [m for m, _ in core] == [m for m, _ in spine] == masks
        for (_, a), (_, b) in zip(core, spine, strict=True):
            assert metrics_equal(a, b)

    def test_chunk_size_invariance(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # §3 dual-knob matrix (fold side): chunk sizing never changes results.
        bars = _bars(1, 60)
        kernel = _crafted_kernel(bars)
        bar_bits = list(kernel.bar_bits)
        cfg = _cfg()
        from brutex.engine.rust_trade_sim import ResidentTradeSim

        sim = ResidentTradeSim(bars, bar_bits, tf_minutes=1)
        masks = [1 << b for b in range(5)]
        outputs: list[list[tuple[int, ResultMetrics]]] = []
        for cells in (1 << 16, 1 << 18, 1 << 23):
            reset_self_check_for_tests(total_remaining=0, plain_remaining=0)
            ctx = sweep_core.core_fold_context(
                bars=bars,
                bar_bits=bar_bits,
                condition_bits=[0, 1, 2, 3, 4],
                tf_minutes=1,
                n_masks=5,
                digest_path=True,
                top_k=3,
                plan=sweep_core.CorePlan(
                    requested=True,
                    belt=True,
                    fold_cells=cells,
                    oracle_ns_per_bar=sweep_core.DEFAULT_ORACLE_NS_PER_BAR,
                ),
            )
            assert ctx is not None
            outputs.append(
                list(
                    sweep_run._iter_mask_metrics_core(
                        ctx,
                        sim,
                        masks,
                        bars,
                        bar_bits,
                        cfg,
                        "bull",
                        lambda m: "rid",
                        retained={},
                        candidates=frozenset(),
                    )
                )
            )
        first = outputs[0]
        for other in outputs[1:]:
            assert [m for m, _ in other] == [m for m, _ in first]
            for (_, a), (_, b) in zip(other, first, strict=True):
                assert metrics_equal(a, b)


class _MemoryErrorCore(_WrappedCore):
    def fold_block(self, *args: Any) -> tuple[int, int, bytes, bytes, bytes]:
        raise MemoryError


class _TypeErrorCore(_WrappedCore):
    def fold_block(self, *args: Any) -> tuple[int, int, bytes, bytes, bytes]:
        raise TypeError("marshalling defect")


class _OnceMeanPoisonCore(_WrappedCore):
    """NONDETERMINISTIC poison: the FIRST fold_block call perturbs the first
    plain row's streamed mean (a rank-bearing sharpe input); every later call
    (the finalist rebuild) folds clean — the §5 round-4 serve-reconciliation
    class the position carry exists to catch."""

    def __init__(self, *args: Any) -> None:
        super().__init__(*args)
        self._calls = 0

    def fold_block(self, *args: Any) -> tuple[int, int, bytes, bytes, bytes]:
        from array import array

        self._calls += 1
        abi, scans, ints_le, floats_le, flags = self._real.fold_block(*args)
        if self._calls == 1:
            floats = array("d")
            floats.frombytes(floats_le)
            for row, flag in enumerate(flags):
                if flag == 0:
                    floats[row * 9 + 0] += 0.25
                    break
            floats_le = floats.tobytes()
        return abi, scans, ints_le, floats_le, flags


class TestGeneratorErrorContracts:
    """MemoryError bare at every level; _combo_error fingerprints; kernel_error."""

    def test_fold_block_memory_error_reraises_bare(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(SWEEP_RUST_CORE_ENV, "1")
        monkeypatch.setattr(
            sweep_core, "_load_kernel_module", lambda: _proxy_module(_MemoryErrorCore)
        )
        _close_burnin()
        with pytest.raises(MemoryError):
            _run(tmp_path, "oom", _bars(), _cfg())

    def test_fold_block_type_error_restarts_as_kernel_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bars = _bars()
        cfg = _cfg()
        baseline, _, _ = _run(tmp_path, "clean", bars, cfg)
        monkeypatch.setenv(SWEEP_RUST_CORE_ENV, "1")
        monkeypatch.setattr(
            sweep_core, "_load_kernel_module", lambda: _proxy_module(_TypeErrorCore)
        )
        _close_burnin()
        summary, logs, _ = _run(tmp_path, "kerr", bars, cfg)
        assert summary.store_content_hash == baseline.store_content_hash
        assert [str(e["reason"]) for e in _events(logs, "sweep_rust_core.tuple_restart")] == [
            "kernel_error"
        ]

    def test_row_metrics_failure_wraps_combo_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(SWEEP_RUST_CORE_ENV, "1")
        _close_burnin()

        def _boom(*args: object, **kwargs: object) -> ResultMetrics:
            raise RuntimeError("combine detonated")

        monkeypatch.setattr(sweep_run, "fold_row_metrics", _boom)
        with pytest.raises(SweepComboError, match="combine detonated"):
            _run(tmp_path, "rowerr", _bars(), _cfg())

    def test_row_metrics_memory_error_reraises_bare(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(SWEEP_RUST_CORE_ENV, "1")
        _close_burnin()

        def _boom(*args: object, **kwargs: object) -> ResultMetrics:
            raise MemoryError

        monkeypatch.setattr(sweep_run, "fold_row_metrics", _boom)
        with pytest.raises(MemoryError):
            _run(tmp_path, "rowoom", _bars(), _cfg())

    def test_burnin_compare_arm_failure_wraps_combo_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(SWEEP_RUST_CORE_ENV, "1")

        def _boom(*args: object, **kwargs: object) -> ResultMetrics:
            raise RuntimeError("oracle arm detonated")

        monkeypatch.setattr(sweep_run, "_pure_oracle_one", _boom)
        with pytest.raises(SweepComboError, match="oracle arm detonated"):
            _run(tmp_path, "armfail", _bars(), _cfg())

    def test_burnin_compare_arm_memory_error_reraises_bare(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(SWEEP_RUST_CORE_ENV, "1")

        def _boom(*args: object, **kwargs: object) -> ResultMetrics:
            raise MemoryError

        monkeypatch.setattr(sweep_run, "_pure_oracle_one", _boom)
        with pytest.raises(MemoryError):
            _run(tmp_path, "armoom", _bars(), _cfg())


class TestBeltPrePassUnits:
    """Direct §5 belt/reconciliation branch drives (fake-free real ctx + sim)."""

    def _fixture(self) -> tuple[Any, Any, list[CandleBar], list[int], SweepRunConfig]:
        from brutex.engine.rust_trade_sim import ResidentTradeSim

        bars = _bars(1, 60)
        kernel = _crafted_kernel(bars)
        bar_bits = list(kernel.bar_bits)
        cfg = _cfg()
        sim = ResidentTradeSim(bars, bar_bits, tf_minutes=1)
        ctx = sweep_core.core_fold_context(
            bars=bars,
            bar_bits=bar_bits,
            condition_bits=[0, 1, 2, 3, 4],
            tf_minutes=1,
            n_masks=5,
            digest_path=True,
            top_k=3,
            plan=sweep_core.CorePlan(
                requested=True,
                belt=True,
                fold_cells=sweep_core.DEFAULT_FOLD_WORK_CELLS,
                oracle_ns_per_bar=sweep_core.DEFAULT_ORACLE_NS_PER_BAR,
            ),
        )
        assert ctx is not None
        return ctx, sim, bars, bar_bits, cfg

    def _pure(self, bars: list[CandleBar], bar_bits: list[int], mask: int) -> ResultMetrics:
        return sweep_run._pure_oracle_one(bars, bar_bits, mask, _cfg())

    def _belt(
        self,
        ctx: Any,
        sim: Any,
        finalists: list[Any],
        bars: list[CandleBar],
        bar_bits: list[int],
        cfg: SweepRunConfig,
        **kwargs: Any,
    ) -> int:
        base: dict[str, Any] = {
            "bounded": None,
            "columns": None,
            "retained": {},
            "candidates": [],
        }
        base.update(kwargs)
        return sweep_run._core_belt_pre_pass(
            ctx,
            sim,
            finalists,
            bars=bars,
            bar_bits=bar_bits,
            cfg=cfg,
            direction_label="bull",
            run_id_of=lambda m: "rid",
            **base,
        )

    def _rc(self, mask: int, metrics: ResultMetrics) -> Any:
        from brutex.ranking import RankedCombo

        return RankedCombo(mask=mask, metrics=metrics, composite_score=0.0, deflated_sharpe=None)

    def test_clean_batch_belt_passes(self) -> None:
        ctx, sim, bars, bar_bits, cfg = self._fixture()
        mask = 0b00001
        pure = self._pure(bars, bar_bits, mask)
        from brutex.ranking import passes_gates

        if not passes_gates(pure):
            pytest.skip("fixture mask does not rank — belt input needs a gate-passer")
        sampled = self._belt(ctx, sim, [self._rc(mask, pure)], bars, bar_bits, cfg)
        assert sampled == 0

    def test_gate_reverdict_failure_restarts(self) -> None:
        ctx, sim, bars, bar_bits, cfg = self._fixture()
        from brutex.engine.sweep_driver import metrics_from_trades

        zero = metrics_from_trades(bars, [], start_equity_paisa=10_000_000, tf_minutes=1)
        with pytest.raises(sweep_run._CoreTupleRestart, match="stream_rebuild_divergence"):
            self._belt(ctx, sim, [self._rc(0b00001, zero)], bars, bar_bits, cfg)

    def test_candidate_finalist_serve_mismatch_restarts(self) -> None:
        ctx, sim, bars, bar_bits, cfg = self._fixture()
        mask = 0b00001
        pure = self._pure(bars, bar_bits, mask)
        from brutex.ranking import passes_gates

        if not passes_gates(pure):
            pytest.skip("fixture mask does not rank")
        served = replace(pure, net_pnl_paisa=pure.net_pnl_paisa + 1)
        sweep_core.reset_self_check_for_tests()
        with pytest.raises(sweep_run._CoreTupleRestart, match="stream_rebuild_divergence"):
            self._belt(
                ctx,
                sim,
                [self._rc(mask, pure)],
                bars,
                bar_bits,
                cfg,
                retained={mask: served},
                candidates=[mask],
            )

    def test_belt_threeway_divergence_restarts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ctx, sim, bars, bar_bits, cfg = self._fixture()
        mask = 0b00001
        pure = self._pure(bars, bar_bits, mask)
        from brutex.ranking import passes_gates

        if not passes_gates(pure):
            pytest.skip("fixture mask does not rank")
        served = replace(pure, net_pnl_paisa=pure.net_pnl_paisa + 1)
        third = replace(pure, net_pnl_paisa=pure.net_pnl_paisa + 2)
        monkeypatch.setattr(sweep_run, "_spine_metrics_one", lambda *a, **k: third)
        with pytest.raises(sweep_run._CoreTupleRestart, match="belt_threeway_divergence"):
            self._belt(ctx, sim, [self._rc(mask, served)], bars, bar_bits, cfg)

    def test_belt_disambiguation_recompute_error_contracts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx, sim, bars, bar_bits, cfg = self._fixture()
        mask = 0b00001
        pure = self._pure(bars, bar_bits, mask)
        from brutex.ranking import passes_gates

        if not passes_gates(pure):
            pytest.skip("fixture mask does not rank")
        served = replace(pure, net_pnl_paisa=pure.net_pnl_paisa + 1)

        def _oom(*args: object, **kwargs: object) -> ResultMetrics:
            raise MemoryError

        monkeypatch.setattr(sweep_run, "_spine_metrics_one", _oom)
        with pytest.raises(MemoryError):
            self._belt(ctx, sim, [self._rc(mask, served)], bars, bar_bits, cfg)

        def _boom(*args: object, **kwargs: object) -> ResultMetrics:
            raise RuntimeError("spine detonated")

        monkeypatch.setattr(sweep_run, "_spine_metrics_one", _boom)
        with pytest.raises(SweepComboError, match="spine detonated"):
            self._belt(ctx, sim, [self._rc(mask, served)], bars, bar_bits, cfg)

    def test_belt_oracle_error_contracts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ctx, sim, bars, bar_bits, cfg = self._fixture()
        mask = 0b00001
        pure = self._pure(bars, bar_bits, mask)
        from brutex.ranking import passes_gates

        if not passes_gates(pure):
            pytest.skip("fixture mask does not rank")

        def _oom(*args: object, **kwargs: object) -> ResultMetrics:
            raise MemoryError

        monkeypatch.setattr(sweep_run, "_pure_oracle_one", _oom)
        with pytest.raises(MemoryError):
            self._belt(ctx, sim, [self._rc(mask, pure)], bars, bar_bits, cfg)

        def _boom(*args: object, **kwargs: object) -> ResultMetrics:
            raise RuntimeError("oracle detonated")

        monkeypatch.setattr(sweep_run, "_pure_oracle_one", _boom)
        with pytest.raises(SweepComboError, match="oracle detonated"):
            self._belt(ctx, sim, [self._rc(mask, pure)], bars, bar_bits, cfg)

    def _bounded_fixture(
        self, *, with_overfit: bool
    ) -> tuple[
        Any, Any, list[CandleBar], list[int], SweepRunConfig, ResultMetrics, int, SurvivorColumns
    ]:
        ctx, sim, bars, bar_bits, cfg = self._fixture()
        mask = 0b00001
        pure = self._pure(bars, bar_bits, mask)
        from brutex.ranking import passes_gates

        if not passes_gates(pure):
            pytest.skip("fixture mask does not rank")
        columns = SurvivorColumns(with_overfit=with_overfit)
        columns.append(mask, pure)
        return ctx, sim, bars, bar_bits, cfg, pure, mask, columns

    def _bounded_result(self, mask: int, position: int) -> Any:
        from brutex.engine.sweep_stream import BoundedFinalist, BoundedRankResult

        return BoundedRankResult(
            finalists=(
                BoundedFinalist(
                    mask=mask, composite_score=0.0, deflated_sharpe=None, position=position
                ),
            ),
            n_ranked=1,
        )

    def test_bounded_clean_reconciliation_passes(self) -> None:
        ctx, sim, bars, bar_bits, cfg, pure, mask, columns = self._bounded_fixture(
            with_overfit=True
        )
        sampled = self._belt(
            ctx,
            sim,
            [self._rc(mask, pure)],
            bars,
            bar_bits,
            cfg,
            bounded=self._bounded_result(mask, 0),
            columns=columns,
        )
        assert sampled == 0

    def test_bounded_position_carry_mismatch_restarts(self) -> None:
        ctx, sim, bars, bar_bits, cfg, pure, mask, columns = self._bounded_fixture(
            with_overfit=False
        )
        columns.append(mask + 1, pure)  # a second entry so position 1 exists
        with pytest.raises(sweep_run._CoreTupleRestart, match="stream_rebuild_divergence"):
            self._belt(
                ctx,
                sim,
                [self._rc(mask, pure)],
                bars,
                bar_bits,
                cfg,
                bounded=self._bounded_result(mask, 1),
                columns=columns,
            )

    def test_bounded_rank_column_mismatch_restarts(self) -> None:
        ctx, sim, bars, bar_bits, cfg, pure, mask, columns = self._bounded_fixture(
            with_overfit=False
        )
        rebuilt = replace(pure, sharpe_ratio=(pure.sharpe_ratio or 0.0) + 0.5)
        with pytest.raises(sweep_run._CoreTupleRestart, match="stream_rebuild_divergence"):
            self._belt(
                ctx,
                sim,
                [self._rc(mask, rebuilt)],
                bars,
                bar_bits,
                cfg,
                bounded=self._bounded_result(mask, 0),
                columns=columns,
            )

    def test_bounded_overfit_column_mismatch_restarts(self) -> None:
        ctx, sim, bars, bar_bits, cfg, pure, mask, columns = self._bounded_fixture(
            with_overfit=True
        )
        rebuilt = replace(pure, kurtosis=(pure.kurtosis or 0.0) + 0.5)
        with pytest.raises(sweep_run._CoreTupleRestart, match="stream_rebuild_divergence"):
            self._belt(
                ctx,
                sim,
                [self._rc(mask, rebuilt)],
                bars,
                bar_bits,
                cfg,
                bounded=self._bounded_result(mask, 0),
                columns=columns,
            )

    def test_spot_check_skips_unretained_and_caps(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ctx, sim, bars, bar_bits, cfg = self._fixture()
        # 6 non-finalist candidates, 5 retained → sampled caps at 4; the
        # unretained candidate is skipped, and a mismatching retained one
        # rides the disambiguation (preexisting → ships).
        masks = [1 << b for b in range(5)]
        retained = {m: self._pure(bars, bar_bits, m) for m in masks}
        candidates = [0b111, *masks]  # 0b111 not retained
        sampled = self._belt(
            ctx,
            sim,
            [],
            bars,
            bar_bits,
            cfg,
            retained=retained,
            candidates=candidates,
        )
        assert sampled == 4

    def test_spot_check_mismatch_disambiguates_core_defect(self) -> None:
        ctx, sim, bars, bar_bits, cfg = self._fixture()
        mask = 0b00001
        pure = self._pure(bars, bar_bits, mask)
        served = replace(pure, net_pnl_paisa=pure.net_pnl_paisa + 1)
        with pytest.raises(sweep_run._CoreTupleRestart, match="belt_mismatch"):
            self._belt(
                ctx,
                sim,
                [],
                bars,
                bar_bits,
                cfg,
                retained={mask: served},
                candidates=[mask],
            )

    def test_spot_check_oracle_error_contracts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ctx, sim, bars, bar_bits, cfg = self._fixture()
        mask = 0b00001
        pure = self._pure(bars, bar_bits, mask)

        def _oom(*args: object, **kwargs: object) -> ResultMetrics:
            raise MemoryError

        monkeypatch.setattr(sweep_run, "_pure_oracle_one", _oom)
        with pytest.raises(MemoryError):
            self._belt(ctx, sim, [], bars, bar_bits, cfg, retained={mask: pure}, candidates=[mask])

        def _boom(*args: object, **kwargs: object) -> ResultMetrics:
            raise RuntimeError("spot oracle detonated")

        monkeypatch.setattr(sweep_run, "_pure_oracle_one", _boom)
        with pytest.raises(SweepComboError, match="spot oracle detonated"):
            self._belt(ctx, sim, [], bars, bar_bits, cfg, retained={mask: pure}, candidates=[mask])


class TestBoundedServeReconciliationEndToEnd:
    def test_stream_only_divergence_caught_via_position_carry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The §5 round-4 class: a nondeterministic core poisons a finalist's
        # STREAMED sharpe input; the rebuild folds clean — clause (1) catches
        # it at the carried position and the tuple restarts to pinned bytes.
        bars = _bars()
        cfg = _cfg()
        baseline, _, _ = _run(tmp_path, "clean", bars, cfg)
        monkeypatch.setenv("BRUTEX_SWEEP_BOUNDED_RANK", "on")
        monkeypatch.setenv(SWEEP_RUST_CORE_ENV, "1")
        monkeypatch.setattr(
            sweep_core, "_load_kernel_module", lambda: _proxy_module(_OnceMeanPoisonCore)
        )
        _close_burnin()
        summary, logs, _ = _run(tmp_path, "streampoison", bars, cfg)
        assert summary.store_content_hash == baseline.store_content_hash
        restarts = [str(e["reason"]) for e in _events(logs, "sweep_rust_core.tuple_restart")]
        assert restarts and all(
            r in ("stream_rebuild_divergence", "belt_mismatch") for r in restarts
        )


class _InterruptCore(_WrappedCore):
    def fold_block(self, *args: Any) -> tuple[int, int, bytes, bytes, bytes]:
        raise KeyboardInterrupt


class TestRemainingBranches:
    def test_non_panic_base_exception_reraises_bare(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(SWEEP_RUST_CORE_ENV, "1")
        monkeypatch.setattr(
            sweep_core, "_load_kernel_module", lambda: _proxy_module(_InterruptCore)
        )
        _close_burnin()
        with pytest.raises(KeyboardInterrupt):
            _run(tmp_path, "intr", _bars(), _cfg())

    def test_engagement_refusal_serves_spine_with_no_candidates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # CORE=1 + top_k=None: the fold arm refuses (`unbounded_top_k`) and
        # the tuple serves on the spine — belt skipped, bytes equal OFF.
        bars = _bars()
        cfg = _cfg(top_k=None)
        baseline, _, _ = _run(tmp_path, "off", bars, cfg)
        monkeypatch.setenv(SWEEP_RUST_CORE_ENV, "1")
        summary, logs, _ = _run(tmp_path, "refused", bars, cfg)
        assert summary.store_content_hash == baseline.store_content_hash
        reasons = [str(e["reason"]) for e in logs if e["event"] == "sweep_rust_core.fallback"]
        assert reasons == ["unbounded_top_k"]
        assert _events(logs, "sweep_run.belt_skipped_non_core")
