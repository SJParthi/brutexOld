"""Integration tests: the bounded rank + memory-budget guard inside the sweep runner.

DEC-SWEEP-STREAMING-RANK-001 (part 2) through the REAL
:func:`brutex.engine.sweep_run.run_persisted_sweep`:

* Dual-mode bit-identity — the bounded columnar run (``BRUTEX_SWEEP_BOUNDED_RANK=on``)
  produces BYTE-IDENTICAL artifacts (store Parquet, ``store_content_hash``,
  trial-stats sidecar, tallies) to the batch run over the same inputs.
* Default preservation — the ``auto`` default below budget never engages
  (no ``bounded_rank_engaged`` log) and stays byte-identical to ``off``.
* Auto-engage — past budget, ``auto`` engages LOUDLY and still completes with
  the correct byte-identical artifacts (hard invariant (c)).
* Graceful refusals — mode ``off`` past budget raises the typed
  :class:`~brutex.engine.sweep_run.SweepMemoryBudgetError` BEFORE any
  evaluation (nothing persisted, no ``SWEEP_START``), naming both knobs;
  the auto-mode ``top_k=None`` / legacy-path ineligibilities refuse likewise;
  mode ``on`` structural misconfigs raise ``ValueError``.
* Watermark — a bounded run whose EXACT owned column bytes cross the budget
  raises the typed error mid-flight (never a raw ``MemoryError``), nothing
  persisted.
* Tuple-skip contract — a grid run turns the refusal into ``TUPLE_FAILED`` +
  ``tuples_failed`` with zero supervisor change, and a sentinel-complete tuple
  short-circuits BEFORE the budget guard (a finished tuple is never refused).
"""

# Whitebox: `_rebuild_bounded_finalists` is exercised directly for its defensive
# arms (empty selection / missing-sim / order-contract belt).
# pyright: reportPrivateUsage=none

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from brutex.data.models import CandleBar
from brutex.data.option_dedup import OptionDedupIndex
from brutex.engine.results_store import ResultsStore
from brutex.engine.sweep_driver import metrics_from_trades
from brutex.config.hardware_envelope import UNDETECTED_RAM, reset_hardware_envelope_cache
from brutex.engine.sweep_mem_budget import (
    BATCH_RANK_BYTES_PER_KEPT,
    BOUNDED_RANK_MODE_ENV,
    BUDGET_WORKERS_ENV,
    MEM_BUDGET_ENV,
)
from brutex.engine.sweep_run import (
    ComboDedup,
    SweepComboError,
    SweepMemoryBudgetError,
    SweepRunConfig,
    SweepRunSummary,
    _rebuild_bounded_finalists,
    run_persisted_sweep,
)
from brutex.engine.sweep_stream import BoundedFinalist, BoundedRankResult
from brutex.orchestrator.audit import AuditWriter
from brutex.strategies.types import Side
from structlog.testing import capture_logs

_IST = timezone(timedelta(hours=5, minutes=30))

#: A budget comfortably ABOVE this fixture's 5-mask projection (5 × 1024 B).
_BIG_BUDGET = str(1024 * 1024)
#: A budget BELOW the 5-mask projection (forces the over-budget arms).
_TINY_BUDGET = str(BATCH_RANK_BYTES_PER_KEPT * 5 - 1)


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


def _trending_bars(n_days: int = 15, per_day: int = 120) -> list[CandleBar]:
    bars: list[CandleBar] = []
    for d in range(n_days):
        price = 100.0
        for m in range(per_day):
            close = price + 0.6 if m % 3 else price - 0.3
            bars.append(_bar(d, m, price, close))
            price = close
    return bars


_BARS = _trending_bars()


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
        "sl_paisa": 80,
        "tp_paisa": 50,
        "top_k": 3,
    }
    base.update(overrides)
    return SweepRunConfig(**base)  # type: ignore[arg-type]


def _run(
    tmp_path: Path,
    tag: str,
    cfg: SweepRunConfig,
    *,
    digest: str | None = "cafe0123cafe0123",
) -> SweepRunSummary:
    store = ResultsStore(path=tmp_path / f"{tag}.parquet")
    dedup = ComboDedup(OptionDedupIndex(capacity=10_000, fpr_target=1e-6))
    with AuditWriter.open(tmp_path / f"{tag}.jsonl") as audit:
        return run_persisted_sweep(
            _BARS, cfg, store=store, dedup=dedup, audit=audit, trial_stats_digest=digest
        )


def _set_env(monkeypatch: pytest.MonkeyPatch, mode: str | None, budget: str | None) -> None:
    if mode is None:
        monkeypatch.delenv(BOUNDED_RANK_MODE_ENV, raising=False)
    else:
        monkeypatch.setenv(BOUNDED_RANK_MODE_ENV, mode)
    if budget is None:
        monkeypatch.delenv(MEM_BUDGET_ENV, raising=False)
    else:
        monkeypatch.setenv(MEM_BUDGET_ENV, budget)


pytestmark = pytest.mark.usefixtures("_polars")


@pytest.fixture(name="_polars")
def _polars_fixture() -> None:
    pytest.importorskip("polars")  # flush needs the [data] extra


# ────── dual-mode bit-identity + default preservation ──────


class TestDualModeBitIdentity:
    def test_bounded_on_equals_batch_off_byte_for_byte(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Hard invariant (a) end-to-end: store bytes + hash + sidecar + tallies."""
        _set_env(monkeypatch, "off", _BIG_BUDGET)
        batch = _run(tmp_path, "batch", _make_config())
        _set_env(monkeypatch, "on", _BIG_BUDGET)
        bounded = _run(tmp_path, "bounded", _make_config())

        assert bounded.store_content_hash == batch.store_content_hash
        assert (tmp_path / "bounded.parquet").read_bytes() == (
            tmp_path / "batch.parquet"
        ).read_bytes()
        # The sidecar JSON is byte-deterministic EXCEPT the embedded
        # store_content_hash pairing — equal hashes ⇒ compare whole bytes.
        batch_sidecar = (tmp_path / "batch.parquet.trials.json").read_bytes()
        bounded_sidecar = (tmp_path / "bounded.parquet.trials.json").read_bytes()
        assert bounded_sidecar == batch_sidecar
        assert bounded == batch

    def test_bounded_on_with_overfit_equals_batch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from brutex.ranking import OverfitGateConfig

        overfit = OverfitGateConfig(dsr_min=0.0)
        _set_env(monkeypatch, "off", _BIG_BUDGET)
        batch = _run(tmp_path, "batch", _make_config(overfit=overfit))
        _set_env(monkeypatch, "on", _BIG_BUDGET)
        bounded = _run(tmp_path, "bounded", _make_config(overfit=overfit))
        assert bounded == batch
        assert (tmp_path / "bounded.parquet").read_bytes() == (
            tmp_path / "batch.parquet"
        ).read_bytes()

    def test_default_auto_below_budget_never_engages(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Hard invariant (b): the unset default is the batch path, bit-identical."""
        _set_env(monkeypatch, None, None)  # pristine default env
        with capture_logs() as logs:
            default = _run(tmp_path, "default", _make_config())
        assert not [e for e in logs if e["event"] == "sweep_run.bounded_rank_engaged"]
        _set_env(monkeypatch, "off", _BIG_BUDGET)
        batch = _run(tmp_path, "batch", _make_config())
        assert default == batch
        assert (tmp_path / "default.parquet").read_bytes() == (
            tmp_path / "batch.parquet"
        ).read_bytes()

    def test_auto_over_budget_engages_loudly_and_matches_batch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Hard invariant (c): auto engages past budget, logs, completes correctly."""
        _set_env(monkeypatch, "off", _BIG_BUDGET)
        batch = _run(tmp_path, "batch", _make_config())
        _set_env(monkeypatch, "auto", _TINY_BUDGET)
        with capture_logs() as logs:
            bounded = _run(tmp_path, "auto", _make_config())
        engaged = [e for e in logs if e["event"] == "sweep_run.bounded_rank_engaged"]
        assert len(engaged) == 1
        assert engaged[0]["log_level"] == "warning"  # LOUD by design in auto
        assert engaged[0]["mode"] == "auto"
        assert engaged[0]["budget_bytes"] == int(_TINY_BUDGET)
        assert bounded == batch
        assert (tmp_path / "auto.parquet").read_bytes() == (tmp_path / "batch.parquet").read_bytes()

    def test_forced_on_logs_at_info(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _set_env(monkeypatch, "on", _BIG_BUDGET)
        with capture_logs() as logs:
            _run(tmp_path, "on", _make_config())
        engaged = [e for e in logs if e["event"] == "sweep_run.bounded_rank_engaged"]
        assert len(engaged) == 1
        assert engaged[0]["log_level"] == "info"

    def test_bounded_zero_survivor_tuple_matches_batch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """min_hits prunes everything → empty bounded rank, empty batch rank."""
        cfg = _make_config(min_hits=10**9)
        _set_env(monkeypatch, "off", _BIG_BUDGET)
        batch = _run(tmp_path, "batch", cfg)
        _set_env(monkeypatch, "on", _BIG_BUDGET)
        bounded = _run(tmp_path, "bounded", cfg)
        assert bounded == batch
        assert bounded.ranked == 0
        assert bounded.persisted == 0


# ────── graceful refusals (pre-flight) ──────


class TestBudgetRefusals:
    def test_off_over_budget_raises_typed_error_before_any_work(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Hard invariant (d): a graceful typed error naming the knobs, nothing persisted."""
        _set_env(monkeypatch, "off", _TINY_BUDGET)
        with pytest.raises(SweepMemoryBudgetError) as excinfo:
            _run(tmp_path, "refused", _make_config())
        message = str(excinfo.value)
        assert MEM_BUDGET_ENV in message
        assert BOUNDED_RANK_MODE_ENV in message
        assert "remedies" in message
        assert "sweep:NIFTY:1m:bull:k=1" in message
        assert not (tmp_path / "refused.parquet").exists()
        # Refused BEFORE the audit START line — the tuple did zero work.
        audit_lines = (tmp_path / "refused.jsonl").read_text().splitlines()
        assert not [line for line in audit_lines if '"SWEEP_START"' in line]

    def test_refusal_is_a_sweep_combo_error_subclass(self) -> None:
        # The tuple-skip contract rides subclassing — zero supervisor change.
        assert issubclass(SweepMemoryBudgetError, SweepComboError)

    def test_auto_over_budget_top_k_none_refuses_naming_top_k(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_env(monkeypatch, "auto", _TINY_BUDGET)
        with pytest.raises(SweepMemoryBudgetError, match="top_k=None"):
            _run(tmp_path, "refused", _make_config(top_k=None))

    def test_auto_over_budget_legacy_path_refuses(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_env(monkeypatch, "auto", _TINY_BUDGET)
        with pytest.raises(SweepMemoryBudgetError, match="LEGACY"):
            _run(tmp_path, "refused", _make_config(), digest=None)

    def test_on_legacy_path_is_a_config_valueerror(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_env(monkeypatch, "on", _BIG_BUDGET)
        with pytest.raises(ValueError, match="LEGACY no-digest"):
            _run(tmp_path, "refused", _make_config(), digest=None)

    def test_on_top_k_none_is_a_config_valueerror(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_env(monkeypatch, "on", _BIG_BUDGET)
        with pytest.raises(ValueError, match="requires a finite top_k"):
            _run(tmp_path, "refused", _make_config(top_k=None))

    def test_sentinel_complete_tuple_short_circuits_before_the_guard(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A finished tuple resumes even under an over-budget off-mode env."""
        _set_env(monkeypatch, "off", _BIG_BUDGET)
        first = _run(tmp_path, "t", _make_config())
        assert first.sentinel_written is True
        _set_env(monkeypatch, "off", _TINY_BUDGET)
        (tmp_path / "t.jsonl").unlink()  # fresh audit chain for the resume run
        resumed = _run(tmp_path, "t", _make_config())
        assert resumed.resumed_complete is True
        assert resumed.store_content_hash == first.store_content_hash


# ────── mid-flight watermark ──────


class TestWatermark:
    def test_bounded_columns_over_budget_raise_typed_error_not_memoryerror(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Mode ON engages bounded regardless of the projection; a budget smaller
        # than a handful of column entries trips the EXACT owned-bytes watermark
        # on the first append — the graceful typed tuple-skip, never a raw crash.
        _set_env(monkeypatch, "on", "64")
        with pytest.raises(SweepMemoryBudgetError) as excinfo:
            _run(tmp_path, "wm", _make_config())
        message = str(excinfo.value)
        assert "watermark breached mid-flight" in message
        assert f"{MEM_BUDGET_ENV}=64" in message
        assert not (tmp_path / "wm.parquet").exists()


# ────── bounded finalist recompute error contract ──────


class TestBoundedFinalistRecompute:
    def test_empty_selection_returns_empty_without_a_sim(self) -> None:
        result = _rebuild_bounded_finalists(
            BoundedRankResult(finalists=(), n_ranked=0),
            None,
            _BARS,
            [0] * len(_BARS),
            _make_config(),
            "bull",
            lambda mask: "0" * 64,
        )
        assert result == []

    def test_missing_sim_with_finalists_fails_loud_as_tuple_skip(self) -> None:
        selected = BoundedRankResult(
            finalists=(BoundedFinalist(mask=1, composite_score=0.0, deflated_sharpe=None),),
            n_ranked=1,
        )
        with pytest.raises(SweepComboError, match="no trade-sim was built"):
            _rebuild_bounded_finalists(
                selected,
                None,
                _BARS,
                [0] * len(_BARS),
                _make_config(),
                "bull",
                lambda mask: "0" * 64,
            )

    def test_reordered_metrics_stream_fails_loud(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The positional-pairing belt trips if the eval spine ever reorders."""

        def _reordering_iter(*args: Any, **kwargs: Any) -> Any:
            yield 999, None  # a mask that was never requested

        monkeypatch.setattr("brutex.engine.sweep_run._iter_mask_metrics", _reordering_iter)
        selected = BoundedRankResult(
            finalists=(BoundedFinalist(mask=1, composite_score=0.0, deflated_sharpe=None),),
            n_ranked=1,
        )
        sim = object()  # never touched — the patched spine ignores it
        with pytest.raises(SweepComboError, match="order contract violated"):
            _rebuild_bounded_finalists(
                selected,
                sim,  # type: ignore[arg-type]
                _BARS,
                [0] * len(_BARS),
                _make_config(),
                "bull",
                lambda mask: "0" * 64,
            )

    def test_metrics_tail_failure_during_recompute_keeps_the_fingerprint(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A recompute-phase failure raises the standard SweepComboError fingerprint."""
        calls = {"n": 0}

        def _tail(*args: Any, **kwargs: Any) -> Any:
            calls["n"] += 1
            if calls["n"] > 5:  # the 5 streamed masks pass; the recompute fails
                raise RuntimeError("recompute boom")
            return metrics_from_trades(*args, **kwargs)

        monkeypatch.setattr("brutex.engine.sweep_run.metrics_from_trades", _tail)
        _set_env(monkeypatch, "on", _BIG_BUDGET)
        with pytest.raises(SweepComboError, match=r"combo 0x[0-9a-f]{32} \(run_id=") as excinfo:
            _run(tmp_path, "boom", _make_config())
        assert "recompute boom" in str(excinfo.value)


# ────── grid tuple-skip integration ──────


class TestGridTupleSkip:
    def test_refused_tuple_is_counted_and_audited_nothing_persisted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from brutex.engine.sweep_orchestrator import SweepGridConfig, Timeframe, run_sweep_grid

        _set_env(monkeypatch, "off", _TINY_BUDGET)
        grid = SweepGridConfig(
            instruments=["NIFTY"],
            timeframes=[Timeframe(label="1m", minutes=1)],
            directions=[Side.BUY],
            k_values=[1],
            condition_bits=[0, 1, 2, 3, 4],
            stop_losses=[80],
            take_profits=[50],
            proximities=[None],
            mode="A",
            data_snapshot_hash="snap0001",
            indicator_version="ind-v1",
            code_commit_sha="deadbeef",
            top_k=3,
        )
        results_root = tmp_path / "results"
        from brutex.engine.sweep_orchestrator import make_store_for

        with AuditWriter.open(tmp_path / "grid.jsonl") as audit:
            summary = run_sweep_grid(
                grid,
                bars_for=lambda instrument, tf: _BARS,
                store_for=make_store_for(results_root, grid),
                dedup=ComboDedup(OptionDedupIndex(capacity=10_000, fpr_target=1e-6)),
                audit=audit,
            )
        assert summary.tuples_failed == 1
        assert not list(results_root.rglob("*.parquet"))
        audit_lines = [
            json.loads(line) for line in (tmp_path / "grid.jsonl").read_text().splitlines()
        ]
        failed = [line for line in audit_lines if line["action"] == "TUPLE_FAILED"]
        assert len(failed) == 1
        assert MEM_BUDGET_ENV in failed[0]["payload"]["error"]


# ── worker-aware memory budget — the DEC-SWEEP-SCALE-GUARDS-001 e2e flip pins,
# re-pinned onto main's LANDED divisor (DEC-SWEEP-MEM-BUDGET-WORKERS-WIRE-001) ──


def _simulate_ram(monkeypatch: pytest.MonkeyPatch, total_bytes: int) -> None:
    """Force RAM detection to *total_bytes* so the DERIVED default is divided.

    Main's landed contract makes the explicit ``BRUTEX_SWEEP_MEM_BUDGET_BYTES``
    override PER-WORKER (never divided), so these e2e pins drive the divisor
    through the RAM-derived default + the ``BRUTEX_SWEEP_BUDGET_WORKERS`` env
    seam — exactly the production pool-initializer shape.
    """
    monkeypatch.setattr(
        "brutex.config.hardware_envelope._sysconf_total_ram_bytes", lambda: total_bytes
    )
    monkeypatch.setattr(
        "brutex.config.hardware_envelope._sysctl_total_ram_bytes", lambda: UNDETECTED_RAM
    )
    reset_hardware_envelope_cache()


@pytest.fixture(name="_fresh_ram_cache")
def _fresh_ram_cache_fixture() -> Iterator[None]:
    """Isolate from the process-global RAM detection cache (DEC-RAM-BUDGETS-001)."""
    reset_hardware_envelope_cache()
    yield
    reset_hardware_envelope_cache()


@pytest.mark.usefixtures("_fresh_ram_cache")
class TestWorkerAwareBudget:
    """End-to-end: the worker divisor reaches the plan seam inside the run.

    The whole point of the OOM fix — the SAME grid + SAME simulated machine
    flips from "fits" (one eval worker) to "refused/bounded" (2 workers via
    the ``BRUTEX_SWEEP_BUDGET_WORKERS`` pool-initializer seam), proving the
    divided budget reaches ``plan_rank_memory`` through
    ``run_persisted_sweep``'s bare ``resolve_mem_budget_bytes()`` call.
    (T2's ``sweep_concurrency=`` kwarg delivery was DEDUPED to this landed
    env-seam mechanism at the 2026-07-11 fold.)
    """

    def _run_workers(
        self,
        tmp_path: Path,
        tag: str,
        monkeypatch: pytest.MonkeyPatch,
        *,
        workers: int,
    ) -> SweepRunSummary:
        if workers == 1:
            monkeypatch.delenv(BUDGET_WORKERS_ENV, raising=False)
        else:
            monkeypatch.setenv(BUDGET_WORKERS_ENV, str(workers))
        store = ResultsStore(path=tmp_path / f"{tag}.parquet")
        dedup = ComboDedup(OptionDedupIndex(capacity=10_000, fpr_target=1e-6))
        with AuditWriter.open(tmp_path / f"{tag}.jsonl") as audit:
            return run_persisted_sweep(
                _BARS,
                _make_config(),
                store=store,
                dedup=dedup,
                audit=audit,
                trial_stats_digest="cafe0123cafe0123",
            )

    def test_single_worker_fits_but_concurrent_refuses_same_machine(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Simulated RAM 48 000 B → derived total 48 000 × 1/6 = 8 000 B, between
        # the per-tuple projection (5 masks × 1024 = 5 120 B) and 2× it: at
        # workers=1 the tuple fits (5 120 < 8 000); at workers=2 the divided
        # budget (8 000 // 2 = 4 000) is below the projection → the graceful
        # typed refusal fires — the OOM fix in miniature.
        _set_env(monkeypatch, "off", None)
        _simulate_ram(monkeypatch, 48_000)
        # workers=1 completes (byte-for-byte the pre-change behavior).
        summary = self._run_workers(tmp_path, "single", monkeypatch, workers=1)
        assert isinstance(summary, SweepRunSummary)
        # workers=2 is refused BEFORE any evaluation (nothing persisted).
        with pytest.raises(SweepMemoryBudgetError, match=MEM_BUDGET_ENV):
            self._run_workers(tmp_path, "concurrent", monkeypatch, workers=2)

    def test_concurrent_engages_bounded_rank_under_auto(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Under mode `auto`, the divided budget ENGAGES the bounded columnar rank
        # (loudly) instead of refusing — and still completes byte-correctly.
        _set_env(monkeypatch, "auto", None)
        _simulate_ram(monkeypatch, 48_000)
        with capture_logs() as logs:
            summary = self._run_workers(tmp_path, "auto_conc", monkeypatch, workers=2)
        assert isinstance(summary, SweepRunSummary)
        assert any(e["event"] == "sweep_run.bounded_rank_engaged" for e in logs)
