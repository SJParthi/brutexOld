"""The rank-time temporal gate wiring in ``sweep_run`` (DEC-SWEEP-TEMPORAL-ROBUSTNESS-001).

The engine-truth suites: the displacement regression (the operator demand as
executable truth, judged by an INDEPENDENT test-side per-year fold — the #1154
oracle discipline), observe-mode counters, the whole-gate abstention WARN, the
DSR bit-identity fatal-flaw pin, the gate-OFF zero-work spy, the audit-payload
only-when-configured law, the identity pins, batch ≡ bounded parity with the
gate ON, and the ``_TemporalRuntime`` fold internals (fast/demote/bounds
belts). The frozen 3-year fixture lives in
``tests/golden/test_sweep_temporal_gate_golden.py`` (ONE home — imported).
"""

from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path

import pytest
from brutex.data.option_dedup import OptionDedupIndex
from brutex.engine import sweep_run as sweep_run_mod
from brutex.engine.combo_mask import mask_to_entry_signals
from brutex.engine.period_stats import decode_period_stats
from brutex.engine.results_store import ResultsStore
from brutex.engine.session_lens import ist_trade_date
from brutex.engine.stage1a import build_bar_bits
from brutex.engine.sweep_mem_budget import BOUNDED_RANK_MODE_ENV, MEM_BUDGET_ENV
from brutex.engine.sweep_orchestrator import (
    SweepGridConfig,
    SweepTuple,
    Timeframe,
    _tuple_digest,  # pyright: ignore[reportPrivateUsage] — the identity pin
)
from brutex.engine.sweep_run import (
    ComboDedup,
    SweepComboError,
    SweepRunConfig,
    SweepRunSummary,
    _build_temporal_runtime,  # pyright: ignore[reportPrivateUsage]
    canonical_trade_params,
    ranked_combo_to_result_row,
    run_persisted_sweep,
)
from brutex.engine.trade_sim import simulate_trades
from brutex.metrics.types import ClosedTrade
from brutex.orchestrator.audit import AuditWriter, iter_entries
from brutex.ranking import RankedCombo
from brutex.ranking.temporal import MODE_OBSERVE, TemporalRobustnessConfig
from brutex.strategies.types import Side
from structlog.testing import capture_logs

from tests.golden.test_sweep_temporal_gate_golden import (
    GOLDEN_TEMPORAL,
    bars_3y,
    golden_config,
)

pytestmark = pytest.mark.usefixtures("_polars")


@pytest.fixture(name="_polars")
def _polars_fixture() -> None:
    pytest.importorskip("polars")  # flush needs the [data] extra


@pytest.fixture(name="_batch_env")
def _batch_env_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the batch rank arm (the bounded arm has its own parity test)."""
    monkeypatch.delenv(BOUNDED_RANK_MODE_ENV, raising=False)
    monkeypatch.delenv(MEM_BUDGET_ENV, raising=False)


_BARS = bars_3y()


def _run(
    tmp_path: Path, tag: str, cfg: SweepRunConfig, *, digest: str | None = None
) -> SweepRunSummary:
    store = ResultsStore(path=tmp_path / f"{tag}.parquet")
    dedup = ComboDedup(OptionDedupIndex(capacity=10_000, fpr_target=1e-6))
    with AuditWriter.open(tmp_path / f"{tag}.jsonl") as audit:
        return run_persisted_sweep(
            _BARS,
            cfg,
            store=store,
            dedup=dedup,
            audit=audit,
            exec_bars_1m=_BARS,
            trial_stats_digest=digest,
        )


def _oracle_yearly_pnl(mask: int) -> dict[int, int]:
    """INDEPENDENT test-side fold: per-IST-exit-year pnl via simulate_trades."""
    bar_bits = build_bar_bits(_BARS)
    trades = simulate_trades(
        _BARS,
        mask_to_entry_signals(bar_bits, mask),
        direction=Side.BUY,
        tf_minutes=1,
        exec_bars_1m=_BARS,
        stop_loss_paisa=80,
        take_profit_paisa=50,
    )
    yearly: dict[int, int] = {}
    for trade in trades:
        year = ist_trade_date(trade.exit_ts).year
        yearly[year] = yearly.get(year, 0) + trade.pnl_paisa
    return yearly


def _oracle_gate_passes(yearly: dict[int, int], judged: tuple[int, ...]) -> bool:
    """An INDEPENDENT restatement of P1 ∧ P2 at the golden config's thresholds."""
    positives = [yearly.get(y, 0) for y in judged if yearly.get(y, 0) > 0]
    p1 = Fraction(len(positives), len(judged)) >= Fraction("0.5")
    p2 = not positives or Fraction(max(positives), sum(positives)) <= Fraction("0.6")
    return p1 and p2


@pytest.mark.usefixtures("_batch_env")
class TestGateOffZeroWork:
    def test_temporal_machinery_never_invoked_without_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The mechanical golden-safety proof: with temporal=None the runtime
        builder is NEVER constructed (and hence no year table, no sink, no
        predicate)."""

        def _boom(*args: object, **kwargs: object) -> object:
            raise AssertionError("temporal runtime built on the gate-OFF arm")

        monkeypatch.setattr(sweep_run_mod, "_build_temporal_runtime", _boom)
        summary = _run(tmp_path, "off", golden_config(temporal=None))
        assert summary.persisted == 3  # the three hard-door survivors
        assert summary.temporal_gated == 0
        assert summary.temporal_judged_years == 0

    def test_gate_off_store_has_no_temporal_columns(self, tmp_path: Path) -> None:
        import polars as pl

        _run(tmp_path, "off", golden_config(temporal=None))
        assert not any(
            name.startswith(("stats_by_", "temporal_"))
            for name in pl.read_parquet(tmp_path / "off.parquet").columns
        )


@pytest.mark.usefixtures("_batch_env")
class TestDisplacement:
    """Graft 7 — the operator demand as executable truth."""

    def test_gate_on_excludes_exactly_the_oracle_predicted_kills(self, tmp_path: Path) -> None:
        judged = (2021, 2022, 2023)
        expected_survivors = set()
        hard_door_survivors = {1 << 0, 1 << 2, 1 << 4}  # from the gate-OFF run below
        for mask in hard_door_survivors:
            if _oracle_gate_passes(_oracle_yearly_pnl(mask), judged):
                expected_survivors.add(mask)
        _run(tmp_path, "off", golden_config(temporal=None))
        on = _run(tmp_path, "on", golden_config())
        off_masks = {r.mask_bits for r in ResultsStore.load(tmp_path / "off.parquet").rows()}
        on_masks = {r.mask_bits for r in ResultsStore.load(tmp_path / "on.parquet").rows()}
        assert off_masks == hard_door_survivors
        assert on_masks == expected_survivors
        assert on.temporal_gated == len(off_masks) - len(on_masks)
        # TEETH: the fixture genuinely kills some and keeps some.
        assert 0 < len(on_masks) < len(off_masks)

    def test_top_slot_displacement(self, tmp_path: Path) -> None:
        """top_k=1: gate OFF hands the ONE slot to the share-concentrated mask
        (1<<2 — highest composite, 61% of its profit in one year); gate ON
        hands it to the steady mask (1<<0) — A out, B in."""
        _run(tmp_path, "off", golden_config(temporal=None, top_k=1))
        _run(tmp_path, "on", golden_config(top_k=1))
        off_rows = ResultsStore.load(tmp_path / "off.parquet").rows()
        on_rows = ResultsStore.load(tmp_path / "on.parquet").rows()
        assert [r.mask_bits for r in off_rows] == [1 << 2]
        assert [r.mask_bits for r in on_rows] == [1 << 0]


@pytest.mark.usefixtures("_batch_env")
class TestObserveMode:
    def test_observe_filters_nothing_and_counts(self, tmp_path: Path) -> None:
        observe = golden_config(
            temporal=TemporalRobustnessConfig(min_edge_year_sessions=1, mode=MODE_OBSERVE)
        )
        summary = _run(tmp_path, "obs", observe)
        masks = {r.mask_bits for r in ResultsStore.load(tmp_path / "obs.parquet").rows()}
        assert masks == {1 << 0, 1 << 2, 1 << 4}  # nothing filtered
        assert summary.temporal_observed_kills == 2
        assert summary.temporal_gated == 0
        assert summary.temporal_judged_years == 3

    def test_observe_still_captures_the_columns(self, tmp_path: Path) -> None:
        observe = golden_config(
            temporal=TemporalRobustnessConfig(min_edge_year_sessions=1, mode=MODE_OBSERVE)
        )
        _run(tmp_path, "obs", observe)
        for row in ResultsStore.load(tmp_path / "obs.parquet").rows():
            assert row.stats_by_year_json is not None
            assert row.temporal_judged_years_json == '["2021","2022","2023"]'


@pytest.mark.usefixtures("_batch_env")
class TestStoredCellsMatchTheOracle:
    def test_stored_year_cells_equal_the_independent_fold(self, tmp_path: Path) -> None:
        """#1154 discipline: every persisted row's stored per-year pnl cells
        equal a from-scratch simulate_trades fold done test-side."""
        _run(tmp_path, "on", golden_config())
        for row in ResultsStore.load(tmp_path / "on.parquet").rows():
            stored = decode_period_stats(str(row.stats_by_year_json), kind="year")
            oracle = _oracle_yearly_pnl(row.mask_bits)
            assert {key: cell[1] for key, cell in stored.items()} == {
                f"{year:04d}": pnl for year, pnl in oracle.items()
            }


@pytest.mark.usefixtures("_batch_env")
class TestAbstention:
    def test_sub_floor_judged_years_abstains_loudly(self, tmp_path: Path) -> None:
        """At the PRODUCTION 120-session edge floor this 4-session-per-year
        fixture judges ONLY the interior 2022 (edge years miss the floor) —
        1 < min_judged_years → whole-gate abstention: nothing filtered, one
        WARN, columns still captured with the honest 1-year denominator."""
        cfg = golden_config(temporal=TemporalRobustnessConfig())  # the real default
        with capture_logs() as logs:
            summary = _run(tmp_path, "abs", cfg)
        warns = [e for e in logs if e["event"] == "sweep_run.temporal_gate"]
        assert len(warns) == 1
        assert warns[0]["abstained"] is True
        assert warns[0]["judged_years"] == 1
        assert summary.temporal_abstained is True
        assert summary.temporal_gated == 0
        assert summary.temporal_judged_years == 1
        rows = ResultsStore.load(tmp_path / "abs.parquet").rows()
        assert {r.mask_bits for r in rows} == {1 << 0, 1 << 2, 1 << 4}
        assert all(r.temporal_judged_years_json == '["2022"]' for r in rows)


@pytest.mark.usefixtures("_batch_env")
class TestDsrBitIdentity:
    def test_survivors_deflated_sharpe_is_bit_identical_gate_on_vs_off(
        self, tmp_path: Path
    ) -> None:
        """THE fatal-flaw pin end-to-end: the DSR pool (N + Sharpe column) is
        the FULL kept population in both arms — a surviving combo's persisted
        deflated_sharpe never moves when the temporal gate engages."""
        from brutex.ranking import OverfitGateConfig

        overfit = OverfitGateConfig(dsr_min=0.0)
        _run(tmp_path, "off", golden_config(temporal=None, overfit=overfit))
        _run(tmp_path, "on", golden_config(overfit=overfit))
        off_rows = {r.run_id: r for r in ResultsStore.load(tmp_path / "off.parquet").rows()}
        on_rows = ResultsStore.load(tmp_path / "on.parquet").rows()
        assert on_rows  # the gate keeps at least one survivor
        for row in on_rows:
            assert row.deflated_sharpe is not None
            assert row.deflated_sharpe == off_rows[row.run_id].deflated_sharpe


@pytest.mark.usefixtures("_batch_env")
class TestAuditPayloads:
    @staticmethod
    def _payloads(path: Path) -> dict[str, dict[str, object]]:
        by_action: dict[str, dict[str, object]] = {}
        for entry in iter_entries(path):
            by_action[entry.action] = dict(entry.payload)
        return by_action

    def test_temporal_keys_emitted_only_when_configured(self, tmp_path: Path) -> None:
        _run(tmp_path, "off", golden_config(temporal=None))
        _run(tmp_path, "on", golden_config())
        off = self._payloads(tmp_path / "off.jsonl")
        on = self._payloads(tmp_path / "on.jsonl")
        assert "temporal_gate" not in off["SWEEP_START"]
        assert not any(key.startswith("temporal_") for key in off["SWEEP_COMPLETE"])
        assert on["SWEEP_START"]["temporal_gate"] == {
            "mode": "gate",
            "min_profitable_years_fraction": 0.5,
            "max_single_year_pnl_share": 0.6,
            "min_edge_year_sessions": 1,
            "min_judged_years": 2,
        }
        assert on["SWEEP_COMPLETE"]["temporal_gated"] == 2
        assert on["SWEEP_COMPLETE"]["temporal_observed_kills"] == 0
        assert on["SWEEP_COMPLETE"]["temporal_abstained"] is False
        assert on["SWEEP_COMPLETE"]["temporal_judged_years"] == 3
        # The DEFAULT payload key set is byte-for-byte the pre-change one.
        assert set(off["SWEEP_START"]) == set(on["SWEEP_START"]) - {"temporal_gate"}


class TestIdentityPins:
    def test_canonical_trade_params_carries_no_temporal_key(self) -> None:
        params = canonical_trade_params(
            sl_paisa=80, tp_paisa=50, prior_n=3, proximity=None, start_equity_paisa=10_000_000
        )
        assert not any("temporal" in key for key in params)
        assert json.dumps(params, sort_keys=True)  # canonicalisable, unchanged shape

    def test_tuple_digest_is_temporal_blind(self) -> None:
        """Two grids differing ONLY in temporal share every store address —
        the identity ruling (a rejection gate re-key would force full
        2-4-day re-sweeps on threshold tweaks)."""
        base: dict[str, object] = {
            "instruments": ["NIFTY"],
            "timeframes": [Timeframe("1m", 1)],
            "directions": [Side.BUY],
            "k_values": [1],
            "condition_bits": [0, 1, 2],
            "stop_losses": [80],
            "take_profits": [50],
            "proximities": [None],
            "mode": "A",
            "data_snapshot_hash": "snap0001",
            "indicator_version": "ind-v1",
            "code_commit_sha": "deadbeef",
        }
        plain = SweepGridConfig(**base)  # type: ignore[arg-type]
        gated = SweepGridConfig(temporal=GOLDEN_TEMPORAL, **base)  # type: ignore[arg-type]
        tuple_ = SweepTuple(
            instrument="NIFTY",
            tf=Timeframe("1m", 1),
            direction=Side.BUY,
            k=1,
            sl_paisa=80,
            tp_paisa=50,
            proximity=None,
        )
        assert _tuple_digest(tuple_, plain) == _tuple_digest(tuple_, gated)
        assert gated.temporal is GOLDEN_TEMPORAL
        assert tuple_.to_run_config(gated).temporal is GOLDEN_TEMPORAL  # threading pin


class TestBoundedParity:
    def test_bounded_equals_batch_with_the_gate_on(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Batch ≡ bounded BIT-FOR-BIT with temporal ON (extends the dual-mode
        law): same store bytes, same kill counters."""
        cfg = golden_config(top_k=3)
        monkeypatch.setenv(BOUNDED_RANK_MODE_ENV, "off")
        monkeypatch.setenv(MEM_BUDGET_ENV, str(1 << 40))
        batch = _run(tmp_path, "batch", cfg, digest="cafe0123cafe0123")
        monkeypatch.setenv(BOUNDED_RANK_MODE_ENV, "on")
        bounded = _run(tmp_path, "bounded", cfg, digest="cafe0123cafe0123")
        assert bounded.store_content_hash == batch.store_content_hash
        assert (tmp_path / "bounded.parquet").read_bytes() == (
            tmp_path / "batch.parquet"
        ).read_bytes()
        assert bounded.temporal_gated == batch.temporal_gated == 2
        assert bounded.temporal_judged_years == batch.temporal_judged_years == 3


class TestTemporalRuntimeInternals:
    def test_census_counts_distinct_ist_sessions(self) -> None:
        runtime = _build_temporal_runtime(_BARS, GOLDEN_TEMPORAL)
        assert runtime.judged == (2021, 2022, 2023)
        assert runtime.n_judged == 3
        assert len(runtime.year_of_bar) == len(_BARS)
        assert runtime.base_year == 2021

    def test_empty_bars_yield_the_empty_census(self) -> None:
        runtime = _build_temporal_runtime([], GOLDEN_TEMPORAL)
        assert runtime.judged == ()
        assert runtime.n_judged == 0

    def test_sink_fast_and_demote_arms_agree(self) -> None:
        """The exit-index consistency belt's demote law: the fast (indexed)
        arm and the exit_ts demote arm reduce to the SAME triple."""
        runtime_fast = _build_temporal_runtime(_BARS, GOLDEN_TEMPORAL)
        runtime_demote = _build_temporal_runtime(_BARS, GOLDEN_TEMPORAL)
        trades = [
            ClosedTrade(
                pnl_paisa=pnl,
                mfe_paisa=max(pnl, 0),
                mae_paisa=max(-pnl, 0),
                holding_bars=1,
                entry_ts=_BARS[idx].timestamp,
                exit_ts=_BARS[idx].timestamp,
                direction=Side.BUY,
            )
            for idx, pnl in ((10, 500), (500, -200), (1_000, 900))
        ]
        runtime_fast.sink(1, trades, [10, 500, 1_000])
        runtime_demote.sink(1, trades, None)  # the demote arm
        assert runtime_fast.take_last(1) == runtime_demote.take_last(1)

    def test_sink_length_mismatch_demotes(self) -> None:
        runtime = _build_temporal_runtime(_BARS, GOLDEN_TEMPORAL)
        trades = [
            ClosedTrade(
                pnl_paisa=700,
                mfe_paisa=700,
                mae_paisa=0,
                holding_bars=1,
                entry_ts=_BARS[10].timestamp,
                exit_ts=_BARS[10].timestamp,
                direction=Side.BUY,
            )
        ]
        runtime.sink(1, trades, [10, 11])  # wrong length → demote, same answer
        demoted = runtime.take_last(1)
        runtime.sink(1, trades, [10])
        assert demoted == runtime.take_last(1)

    def test_sink_out_of_bounds_index_demotes(self) -> None:
        runtime = _build_temporal_runtime(_BARS, GOLDEN_TEMPORAL)
        trades = [
            ClosedTrade(
                pnl_paisa=700,
                mfe_paisa=700,
                mae_paisa=0,
                holding_bars=1,
                entry_ts=_BARS[10].timestamp,
                exit_ts=_BARS[10].timestamp,
                direction=Side.BUY,
            )
        ]
        runtime.sink(1, trades, [len(_BARS)])  # out of range → demote via exit_ts
        assert runtime.take_last(1).profitable_judged_years == 1

    def test_take_last_without_a_fold_fails_loud(self) -> None:
        runtime = _build_temporal_runtime(_BARS, GOLDEN_TEMPORAL)
        with pytest.raises(SweepComboError, match="temporal fold missing"):
            runtime.take_last(0b101)


class TestProjectorCapture:
    def test_zero_trade_finalist_stores_none(self) -> None:
        """A Stage-3-style zero-trade evaluation under an ACTIVE temporal
        config stores honest None — never fabricated empty maps."""
        from brutex.engine.sweep_driver import evaluate_combo

        bar_bits = build_bar_bits(_BARS)
        never_fires = 1 << 59  # choch_bearish never fires on this fixture
        metrics = evaluate_combo(_BARS, never_fires, direction=Side.BUY, tf_minutes=1)
        assert metrics.total_trades == 0
        row = ranked_combo_to_result_row(
            RankedCombo(mask=never_fires, metrics=metrics, composite_score=0.0),
            run_id="ab" * 32,
            instrument="NIFTY",
            tf="1m",
            direction="bull",
            mode="A",
            bars=_BARS,
            bar_bits=bar_bits,
            tf_minutes=1,
            sl_paisa=80,
            tp_paisa=50,
            temporal=GOLDEN_TEMPORAL,
            temporal_judged_years=(2021, 2022, 2023),
        )
        assert row.stats_by_year_json is None
        assert row.temporal_judged_years_json is None
