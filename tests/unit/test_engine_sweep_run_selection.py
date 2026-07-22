"""LANE-W1 persist-union tests for the sweep runner selection stage.

DEC-RANK-PARETO-PERSIST-001 / DEC-RANK-QUOTA-PERSIST-1000-001: the persist
seam in :func:`brutex.engine.sweep_run.run_persisted_sweep` now selects the
UNION of the composite top-``top_k`` prefix, the per-axis reserved quotas and
the ε-Pareto frontier — every persisted row tagged ``selection_basis`` +
``pareto_member``, computed ONCE per pool BEFORE the single append per run_id
(the ``results_store.py`` run-identity conflict law).

Covered here (the seam-level laws — the composer's own math lives in
``tests/unit/test_ranking_multi_select.py``):

* [FB] the motivating regression — a gate-passer OUTSIDE the composite
  top-``top_k`` prefix (quota-admitted, e.g. DD-quota) IS persisted with an
  honest non-composite ``selection_basis``; at the pre-W1 merge-base source
  it was silently truncated (executed fails-before evidence, build log).
* the ``top_k=None`` law — the union adds TAGS, never rows.
* batch-arm ≡ bounded-arm parity — same input ⇒ byte-identical stores
  (row set + tags + order), the ONE-composer-by-construction proof.
* the canonical tag-order law on STORED rows.
* the ``gated_pass_truncated`` / ``frontier_dropped`` counter wiring into
  ``SweepRunSummary`` + the flush audit payload.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.data.models import CandleBar
from brutex.data.option_dedup import OptionDedupIndex
from brutex.engine.results_store import ResultsStore
from brutex.engine.sweep_mem_budget import BOUNDED_RANK_MODE_ENV, MEM_BUDGET_ENV
from brutex.engine.sweep_run import (
    ComboDedup,
    SweepRunConfig,
    SweepRunSummary,
    ranked_combo_to_result_row,
    run_persisted_sweep,
)
from brutex.orchestrator.audit import AuditWriter
from brutex.ranking import rank_combos
from brutex.ranking.multi_select import (
    BASIS_COMPOSITE,
    BASIS_DD,
    BASIS_FRONTIER,
    SelectionInputs,
    UnionSelection,
    join_basis_tokens,
)
from brutex.strategies.types import Side

_IST = timezone(timedelta(hours=5, minutes=30))

pytestmark = pytest.mark.usefixtures("_polars")


@pytest.fixture(name="_polars")
def _polars_fixture() -> None:
    pytest.importorskip("polars")  # flush needs the [data] extra


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
    # The sweep_run suite's worst-case-rankable recipe
    # (DEC-FILL-WORST-CASE-ONLY-001): mostly-up drift, every 5th day DOWN so
    # profit_factor stays finite; EOD-only exits (sl/tp None below). Masks
    # 2/8/16 pass gates — a deterministic 3-gate-passer ranked pool.
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
            trial_stats_digest=digest,
            # tf=1minute — the decision stream IS the execution stream
            # (DEC-FILL-EXEC-GRID-1M-001 §6 fixed point: one list, one key).
            exec_bars_1m=_BARS,
        )


def _stored_rows(tmp_path: Path, tag: str) -> list[object]:
    return list(ResultsStore.load(tmp_path / f"{tag}.parquet").rows())


class TestUnionPersist:
    def test_quota_row_beyond_composite_prefix_is_persisted_and_tagged(
        self, tmp_path: Path
    ) -> None:
        """[FB] The motivating winning-completeness regression (item 2).

        At ``top_k=1`` the pre-W1 seam persisted EXACTLY the composite #1 —
        every other gate-passer (however DD-optimal) died at the scalar cut.
        The union now persists the quota-admitted extras with an honest
        non-composite ``selection_basis``. Fails-before EXECUTED at the
        stash-reverted merge-base source (``persisted == 1``, no extras) —
        evidence recorded in the W1 build log.
        """
        summary = _run(tmp_path, "fb", _make_config(top_k=1))
        assert summary.ranked == 3
        # THE TEETH: two rows BEYOND the composite top-1 prefix persist (at
        # merge base: persisted == 1, the extras silently truncated).
        assert summary.persisted == 3
        rows = _stored_rows(tmp_path, "fb")
        assert len(rows) == 3
        for row in rows:
            basis = row.selection_basis  # type: ignore[attr-defined]
            assert basis  # every persisted row is tagged, never None/empty
            tokens = set(basis.split(","))
            # The quota axes visibly admitted each row (DD is one of the five;
            # at this 3-row pool every row is within every axis's top-200 —
            # NOTE the quota's composite axis folds into the SAME "composite"
            # token, so token-presence does NOT distinguish prefix vs extras;
            # membership beyond top_k==1 is the distinguishing fact above).
            assert BASIS_DD in tokens
            assert BASIS_COMPOSITE in tokens
            # A CONCRETE pareto_member verdict — never the honest-unknown None.
            assert row.pareto_member is not None  # type: ignore[attr-defined]

    def test_top_k_none_adds_tags_never_rows(self, tmp_path: Path) -> None:
        # The pre-W1 behaviour boundary: at top_k=None the composite prefix IS
        # the whole pool, so the union changes NO row membership — it only
        # stamps provenance.
        summary = _run(tmp_path, "all", _make_config(top_k=None))
        assert summary.persisted == summary.ranked == 3
        assert summary.gated_pass_truncated == 0
        rows = _stored_rows(tmp_path, "all")
        for row in rows:
            tokens = (row.selection_basis or "").split(",")  # type: ignore[attr-defined]
            assert BASIS_COMPOSITE in tokens  # depth == n ⇒ every row is prefix
            assert row.pareto_member is not None  # type: ignore[attr-defined]

    def test_stored_tags_are_canonical_order(self, tmp_path: Path) -> None:
        # The stable-order law: a stored selection_basis is ALWAYS the
        # canonical BASIS_ORDER join of its own tokens — never insertion /
        # discovery order (deterministic across redo by construction).
        _run(tmp_path, "canon", _make_config(top_k=1))
        for row in _stored_rows(tmp_path, "canon"):
            basis = row.selection_basis  # type: ignore[attr-defined]
            assert basis is not None
            assert basis == join_basis_tokens(tuple(basis.split(",")))
            # frontier membership ⇔ the frontier token (one law, two views).
            assert (BASIS_FRONTIER in basis.split(",")) == bool(row.pareto_member)  # type: ignore[attr-defined]

    def test_redo_reproduces_identical_tags_and_bytes(self, tmp_path: Path) -> None:
        # Tags are a pure function of the tuple's ranked pool (the whole
        # selection lives INSIDE run_persisted_sweep) ⇒ an identical redo is
        # byte-identical, tags included.
        cfg = _make_config(top_k=1)
        first = _run(tmp_path, "redo1", cfg)
        second = _run(tmp_path, "redo2", cfg)
        assert first.store_content_hash == second.store_content_hash
        assert (tmp_path / "redo1.parquet").read_bytes() == (
            tmp_path / "redo2.parquet"
        ).read_bytes()


class TestBatchBoundedParity:
    def test_bounded_arm_matches_batch_bytes_with_union_extras(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The ONE-composer proof at a top_k where the union genuinely adds
        # extras (top_k=1 → 2 quota extras): the bounded columnar arm
        # (BRUTEX_SWEEP_BOUNDED_RANK=on) must produce the byte-identical
        # store — row SET, ORDER and TAGS — because both arms delegate to the
        # SAME `union_selection` binding (CLAUDE.md §11 COMMON).
        cfg = _make_config(top_k=1)
        digest = "cafe0123cafe0123"  # the bounded arm is a digest-path feature
        monkeypatch.delenv(BOUNDED_RANK_MODE_ENV, raising=False)
        monkeypatch.delenv(MEM_BUDGET_ENV, raising=False)
        batch = _run(tmp_path, "batch", cfg, digest=digest)
        monkeypatch.setenv(BOUNDED_RANK_MODE_ENV, "on")
        monkeypatch.setenv(MEM_BUDGET_ENV, str(1024 * 1024))
        bounded = _run(tmp_path, "bounded", cfg, digest=digest)
        assert bounded.persisted == batch.persisted == 3
        assert bounded.gated_pass_truncated == batch.gated_pass_truncated == 0
        assert bounded.frontier_dropped == batch.frontier_dropped == 0
        assert bounded.store_content_hash == batch.store_content_hash
        assert (tmp_path / "bounded.parquet").read_bytes() == (
            tmp_path / "batch.parquet"
        ).read_bytes()


class TestCounterWiring:
    def test_truncated_and_frontier_counters_reach_summary_and_audit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Wiring spy: the summary + flush-audit counters are EXACTLY the
        # composer's verdicts (the composer's own truncation math is pinned in
        # test_ranking_multi_select.py — driving >AXIS_QUOTA_M=200 real
        # gate-passers through a unit sweep would be a minutes-scale fixture
        # for the same law; the wrapper keeps the REAL selection row set and
        # perturbs only the two counters).
        from brutex.engine import sweep_run as sweep_run_module
        from brutex.engine.rust_rank_select import union_selection as real_union

        def _wrapped(inputs: SelectionInputs, *, top_k: int | None) -> UnionSelection:
            real = real_union(inputs, top_k=top_k)
            return dataclasses.replace(real, truncated=7, frontier_dropped=5)

        monkeypatch.setattr(sweep_run_module, "union_selection", _wrapped)
        summary = _run(tmp_path, "wire", _make_config(top_k=1))
        assert summary.gated_pass_truncated == 7
        assert summary.frontier_dropped == 5
        from brutex.orchestrator.audit import read_entries

        flush_payloads = [
            e.payload for e in read_entries(tmp_path / "wire.jsonl") if e.action == "SWEEP_FLUSH"
        ]
        assert len(flush_payloads) == 1
        assert flush_payloads[0]["gated_pass_truncated"] == 7
        assert flush_payloads[0]["frontier_dropped"] == 5

    def test_result_row_projection_defaults_stay_untagged(self) -> None:
        # Pre-W1 caller law: ranked_combo_to_result_row WITHOUT the new
        # kwargs stamps None/None (the honest unknown) — additive-only.
        from brutex.engine.stage1a import build_bar_bits
        from brutex.engine.sweep_driver import metrics_for_mask

        bar_bits = build_bar_bits(_BARS)
        pairs = [
            (
                mask,
                metrics_for_mask(
                    _BARS,
                    bar_bits,
                    mask,
                    direction=Side.BUY,
                    tf_minutes=1,
                    exec_bars_1m=_BARS,
                    stop_loss_paisa=None,
                    take_profit_paisa=None,
                    start_equity_paisa=10_000_000,
                ),
            )
            for mask in (1, 2, 4, 8, 16)
        ]
        ranked = rank_combos(pairs)
        assert ranked
        row = ranked_combo_to_result_row(
            ranked[0],
            run_id="a" * 64,
            instrument="NIFTY",
            tf="1m",
            direction="bull",
            mode="A",
            bars=_BARS,
            bar_bits=bar_bits,
            tf_minutes=1,
            # tf=1minute — the decision stream IS the execution stream
            # (DEC-FILL-EXEC-GRID-1M-001 §6 fixed point).
            exec_bars_1m=_BARS,
            sl_paisa=None,
            tp_paisa=None,
        )
        assert row.selection_basis is None
        assert row.pareto_member is None
