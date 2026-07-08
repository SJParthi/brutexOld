"""Unit tests for the sweep-verify doctor core (`brutex.engine.sweep_verify`).

100% line + branch coverage of the two bars-free tiers (DEC-SWEEP-VERIFY-001):
every :class:`VerifyFinding` validation arm, the report properties + render,
the recorded-context audit reader (absent / valid / missing-key /
inconsistent / unreadable), every Tier-1 structural arm over a REAL swept
tree with surgically injected damage, and every Tier-2 identity arm including
THE #650 inflation signature (``rows > top_k``), the grid-mismatch refusal,
and the Stage-3 cell attribution arms.
"""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import dataclasses
import shutil
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import ClassVar

import pytest
from brutex.data.models import CandleBar
from brutex.data.option_dedup import OptionDedupIndex
from brutex.engine.reference_series import TradeReferenceStamper
from brutex.engine.results_store import ResultsStore
from brutex.engine.sweep_orchestrator import (
    SweepGridConfig,
    SweepTuple,
    Timeframe,
    iter_sweep_tuples,
    run_sweep_grid,
    tuple_store_path,
)
from brutex.engine.sweep_run import ComboDedup, canonical_trade_params, mask_run_id
from brutex.engine.sweep_verify import (
    RecordedContext,
    SweepVerifyError,
    TreeVerifyReport,
    VerifyFinding,
    _is_pre_vix_schema_migration,
    _non_hash_fields_equal,
    _sentinel_diff_is_the_vix_repair,
    _stores_differ_only_in_vix,
    compare_recount,
    plan_expected_stores,
    recorded_context_from_audit,
    recorded_stage3_from_audit,
    verify_identity,
    verify_structural,
)
from brutex.engine.trial_stats import (
    read_tuple_trial_stats,
    trial_stats_sidecar_path,
    tuple_trial_stats_from_sharpes,
    write_tuple_trial_stats,
)
from brutex.engine.tuple_complete import (
    TupleCompletionSentinel,
    completion_sentinel_path,
    read_completion_sentinel,
    write_completion_sentinel,
)
from brutex.orchestrator.audit import AuditWriter
from brutex.strategies.types import Side
from brutex.vocabulary import has_conflict

_IST = timezone(timedelta(hours=5, minutes=30))


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


def _grid(**overrides: object) -> SweepGridConfig:
    base: dict[str, object] = {
        "instruments": ["NIFTY"],
        "timeframes": [Timeframe("1minute", 1)],
        "directions": [Side.BUY],
        "k_values": [1],
        "condition_bits": [0, 2, 3],
        "stop_losses": [80],
        "take_profits": [50],
        "proximities": [None],
        "mode": "A",
        "data_snapshot_hash": "snap0001",
        "indicator_version": "ind-v1",
        "code_commit_sha": "deadbeef",
        "min_hits": 0,
    }
    base.update(overrides)
    return SweepGridConfig(**base)  # type: ignore[arg-type]


def _sweep(
    root: Path, grid: SweepGridConfig, reference_stamper: TradeReferenceStamper | None = None
) -> None:
    from brutex.engine.sweep_orchestrator import make_store_for

    with AuditWriter.open(root / "audit.jsonl") as audit:
        run_sweep_grid(
            grid,
            bars_for=lambda _i, _t: _trending_bars(),
            store_for=make_store_for(root, grid),
            dedup=ComboDedup(OptionDedupIndex(capacity=10_000, fpr_target=1e-6)),
            audit=audit,
            reference_stamper=reference_stamper,
        )


def _verify_stamper(base: str) -> TradeReferenceStamper:
    """A stamper covering EVERY ``_trending_bars`` instant at *base*-anchored levels.

    Two different *base* values model the sanctioned India-VIX lake movement
    between a recorded sweep and its recount (DEC-VIX-STAMP-WIRE-001): same
    instants, different levels → same run identity, different persisted trio.
    """
    vix_bars = [
        CandleBar(
            timestamp=bar.timestamp,
            open=Decimal(base),
            high=Decimal(base) + Decimal("1"),
            low=Decimal(base) - Decimal("1"),
            close=Decimal(base) + Decimal(i % 7) / Decimal(100),
            volume=0,
            open_interest=None,
        )
        for i, bar in enumerate(_trending_bars())
    ]
    return TradeReferenceStamper.from_vix_bars(vix_bars)


@pytest.fixture(scope="module")
def clean_tree(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """ONE real swept tree (module-scoped); tests copy it before corrupting."""
    root = tmp_path_factory.mktemp("clean") / "results"
    root.mkdir()
    _sweep(root, _grid())
    return root


@pytest.fixture
def tree(clean_tree: Path, tmp_path: Path) -> Path:
    """A per-test private copy of the clean swept tree."""
    dest = tmp_path / "results"
    shutil.copytree(clean_tree, dest)
    return dest


def _store_path(root: Path, grid: SweepGridConfig) -> Path:
    (tuple_,) = iter_sweep_tuples(grid)
    return tuple_store_path(root, tuple_, grid)


def _codes(report: TreeVerifyReport) -> set[str]:
    return {f.code for f in report.findings}


# ── VerifyFinding / TreeVerifyReport ─────────────────────────────────────────


class TestFindingValidation:
    @pytest.mark.parametrize("field", ["path", "code", "detail"])
    def test_empty_field_raises(self, field: str) -> None:
        kwargs = {"path": "p", "code": "c", "severity": "info", "detail": "d"}
        kwargs[field] = ""
        with pytest.raises(SweepVerifyError, match="must be non-empty"):
            VerifyFinding(**kwargs)

    def test_unknown_severity_raises(self) -> None:
        with pytest.raises(SweepVerifyError, match="severity must be one of"):
            VerifyFinding(path="p", code="c", severity="fatal", detail="d")

    def test_report_properties_and_render(self) -> None:
        bad = VerifyFinding(path="a", code="x", severity="corrupt", detail="boom")
        note = VerifyFinding(path="b", code="y", severity="info", detail="fyi")
        report = TreeVerifyReport(
            root="/r", tier="structural", checked_stores=2, findings=(bad, note)
        )
        assert report.corrupt == (bad,)
        assert report.clean is False
        out = report.render()
        assert "CORRUPT (1 finding(s))" in out
        assert "[CORRUPT] a x: boom" in out
        assert "[INFO] b y: fyi" in out
        clean = TreeVerifyReport(root="/r", tier="identity", checked_stores=2, findings=(note,))
        assert clean.clean is True
        assert "verdict: CLEAN" in clean.render()


# ── recorded_context_from_audit ──────────────────────────────────────────────


class TestRecordedContext:
    def test_absent_audit_returns_none(self, tmp_path: Path) -> None:
        assert recorded_context_from_audit(tmp_path / "nope.jsonl") is None

    def test_reads_the_three_strings_from_a_real_sweep(self, tree: Path) -> None:
        ctx = recorded_context_from_audit(tree / "audit.jsonl")
        assert ctx == RecordedContext(
            data_snapshot_hash="snap0001",
            indicator_version="ind-v1",
            code_commit_sha="deadbeef",
        )

    def test_no_sweep_start_lines_returns_none(self, tmp_path: Path) -> None:
        path = tmp_path / "a.jsonl"
        with AuditWriter.open(path) as audit:
            audit.append(action="OTHER", subject="s", payload={})
        assert recorded_context_from_audit(path) is None

    def test_missing_context_key_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "a.jsonl"
        with AuditWriter.open(path) as audit:
            audit.append(action="SWEEP_START", subject="s", payload={"data_snapshot_hash": "x"})
        with pytest.raises(SweepVerifyError, match="missing"):
            recorded_context_from_audit(path)

    def test_consistent_repeat_entries_agree(self, tmp_path: Path) -> None:
        # Two tuples recording the SAME context (the multi-tuple grid shape).
        path = tmp_path / "a.jsonl"
        payload = {
            "data_snapshot_hash": "x",
            "indicator_version": "v",
            "code_commit_sha": "c",
        }
        with AuditWriter.open(path) as audit:
            audit.append(action="SWEEP_START", subject="s1", payload=payload)
            audit.append(action="SWEEP_START", subject="s2", payload=dict(payload))
        ctx = recorded_context_from_audit(path)
        assert ctx is not None
        assert ctx.code_commit_sha == "c"

    def test_inconsistent_context_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "a.jsonl"
        payload = {
            "data_snapshot_hash": "x",
            "indicator_version": "v",
            "code_commit_sha": "c",
        }
        with AuditWriter.open(path) as audit:
            audit.append(action="SWEEP_START", subject="s", payload=payload)
            audit.append(
                action="SWEEP_START", subject="s", payload={**payload, "code_commit_sha": "other"}
            )
        with pytest.raises(SweepVerifyError, match="inconsistent recorded context"):
            recorded_context_from_audit(path)

    def test_unreadable_audit_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "a.jsonl"
        path.write_text("{not jsonl\n", encoding="utf-8")
        with pytest.raises(SweepVerifyError, match="unreadable audit log"):
            recorded_context_from_audit(path)


# ── Tier 1 — verify_structural ───────────────────────────────────────────────


class TestStructural:
    def test_missing_root_raises(self, tmp_path: Path) -> None:
        with pytest.raises(SweepVerifyError, match="results root not found"):
            verify_structural(tmp_path / "nope")

    def test_clean_tree_is_clean(self, tree: Path) -> None:
        report = verify_structural(tree)
        assert report.checked_stores == 1
        assert report.findings == ()
        assert report.clean is True

    def test_unreadable_store(self, tree: Path) -> None:
        store = _store_path(tree, _grid())
        store.write_bytes(b"not a parquet file")
        report = verify_structural(tree)
        assert "store_unreadable" in _codes(report)
        assert not report.clean

    def test_unrecognized_store_name(self, tree: Path) -> None:
        # Both malformed shapes: too few name parts AND a non-hex digest slot.
        for name in ("notes.parquet", "bull_k1_notahexdigest00.parquet"):
            ResultsStore(path=tree / "NIFTY" / "1minute" / name).flush()
        report = verify_structural(tree)
        codes = [f.code for f in report.findings]
        assert codes.count("unrecognized_store_name") == 2
        assert report.clean is True  # warns, not verdicts

    def test_legacy_tree_without_sidecar_warns_only(self, tree: Path) -> None:
        store = _store_path(tree, _grid())
        trial_stats_sidecar_path(store).unlink()
        completion_sentinel_path(store).unlink()  # full pre-#646 shape
        report = verify_structural(tree)
        assert {"legacy_no_sidecar", "no_completion_proof"} <= _codes(report)
        assert report.clean is True

    def test_sidecar_unreadable_is_corrupt(self, tree: Path) -> None:
        store = _store_path(tree, _grid())
        trial_stats_sidecar_path(store).write_bytes(b"{corrupt")
        report = verify_structural(tree)
        assert {"sidecar_unreadable", "sentinel_sidecar_missing"} <= _codes(report)

    def test_sidecar_digest_mismatch(self, tree: Path) -> None:
        store = _store_path(tree, _grid())
        import hashlib

        write_tuple_trial_stats(
            trial_stats_sidecar_path(store),
            tuple_trial_stats_from_sharpes(
                [0.5],
                n_enumerated=1,
                n_conflict_pruned=0,
                n_minhits_dropped=0,
                tuple_digest="0" * 16,
                store_content_hash=hashlib.sha256(store.read_bytes()).hexdigest(),
            ),
        )
        assert "sidecar_digest_mismatch" in _codes(verify_structural(tree))

    def test_store_swap_hits_both_hash_pairings(self, tree: Path) -> None:
        # Replace the store with a DIFFERENT valid store: loadable, but the
        # sidecar AND sentinel both pair to the original bytes.
        store = _store_path(tree, _grid())
        other = ResultsStore(path=store)
        other.flush()  # an empty store — valid Parquet, different bytes
        report = verify_structural(tree)
        assert {"sidecar_store_hash_mismatch", "sentinel_store_hash_mismatch"} <= _codes(report)

    def test_sentinel_unreadable(self, tree: Path) -> None:
        store = _store_path(tree, _grid())
        completion_sentinel_path(store).write_bytes(b"{corrupt")
        assert "sentinel_unreadable" in _codes(verify_structural(tree))

    def test_sentinel_digest_mismatch(self, tree: Path) -> None:
        store = _store_path(tree, _grid())
        recorded = read_completion_sentinel(completion_sentinel_path(store))
        write_completion_sentinel(
            completion_sentinel_path(store),
            TupleCompletionSentinel(
                tuple_digest="0" * 16,
                store_content_hash=recorded.store_content_hash,
                sidecar_written=recorded.sidecar_written,
                n_marked=recorded.n_marked,
            ),
        )
        assert "sentinel_digest_mismatch" in _codes(verify_structural(tree))

    def test_sidecar_tally_remainder_on_sentineled_tuple(self, tree: Path) -> None:
        store = _store_path(tree, _grid())
        import hashlib

        digest = store.stem.split("_")[-1]
        write_tuple_trial_stats(
            trial_stats_sidecar_path(store),
            tuple_trial_stats_from_sharpes(
                [0.5],
                n_enumerated=5,  # 4 masks unaccounted — impossible post-#654
                n_conflict_pruned=0,
                n_minhits_dropped=0,
                tuple_digest=digest,
                store_content_hash=hashlib.sha256(store.read_bytes()).hexdigest(),
            ),
        )
        # Re-pair the sentinel to the (unchanged) store bytes so ONLY the
        # tally invariant fires.
        report = verify_structural(tree)
        assert "sidecar_tally_remainder" in _codes(report)

    def test_orphan_sidecar_and_sentinel(self, tree: Path) -> None:
        store = _store_path(tree, _grid())
        sidecar = trial_stats_sidecar_path(store)
        sentinel = completion_sentinel_path(store)
        gone = store.with_name("bull_k9_" + "ab" * 8 + ".parquet")
        sidecar.rename(gone.parent / (gone.name + ".trials.json"))
        sentinel.rename(gone.parent / (gone.name + ".complete.json"))
        report = verify_structural(tree)
        assert {"orphan_sidecar", "orphan_sentinel"} <= _codes(report)


# ── Tier 2 — verify_identity ─────────────────────────────────────────────────


class TestIdentity:
    def test_missing_root_raises(self, tmp_path: Path) -> None:
        with pytest.raises(SweepVerifyError, match="results root not found"):
            verify_identity(tmp_path / "nope", _grid())

    def test_clean_tree_is_clean(self, tree: Path) -> None:
        report = verify_identity(tree, _grid())
        assert report.checked_stores == 1
        assert report.clean is True
        assert report.findings == ()

    def test_grid_mismatch_refuses_per_file_verdicts(self, tree: Path) -> None:
        wrong = _grid(data_snapshot_hash="ffffffff")
        report = verify_identity(tree, wrong)
        assert _codes(report) == {"grid_mismatch"}
        assert report.checked_stores == 0
        assert not report.clean

    def test_unattributed_store_warns(self, tree: Path) -> None:
        store = _store_path(tree, _grid())
        foreign = store.with_name("bull_k1_" + "f" * 16 + ".parquet")
        shutil.copyfile(store, foreign)
        report = verify_identity(tree, _grid())
        assert "unattributed_store" in _codes(report)
        assert report.clean is True

    def test_missing_expected_store_is_informational(self, tree: Path) -> None:
        store = _store_path(tree, _grid())
        store.unlink()
        report = verify_identity(tree, _grid())
        assert "missing_expected_store" in _codes(report)
        assert report.clean is True

    def test_unreadable_attributed_store_is_corrupt(self, tree: Path) -> None:
        _store_path(tree, _grid()).write_bytes(b"junk")
        assert "store_unreadable" in _codes(verify_identity(tree, _grid()))

    def test_topk_inflation_signature(self, tmp_path: Path) -> None:
        # THE #650 regression at this tier: a top_k=1 store holding MORE than
        # top_k rows under one digest is corrupt — detectable bars-free, even
        # on a pre-#646 tree (delete the sidecar/sentinel to prove it).
        grid = _grid(top_k=1)
        root = tmp_path / "results"
        root.mkdir()
        _sweep(root, grid)
        store_path = _store_path(root, grid)
        store = ResultsStore.load(store_path)
        assert len(store) == 1
        (row,) = store.rows()
        extra = dataclasses.replace(row, run_id="f" * 64)
        store.append(extra)
        store.flush()
        trial_stats_sidecar_path(store_path).unlink()
        completion_sentinel_path(store_path).unlink()
        report = verify_identity(root, grid)
        assert "rows_exceed_top_k" in _codes(report)
        assert not report.clean

    def test_row_label_mismatch(self, tree: Path) -> None:
        store_path = _store_path(tree, _grid())
        (first, *rest) = ResultsStore.load(store_path).rows()
        rebuilt = ResultsStore(path=store_path)
        rebuilt.append(dataclasses.replace(first, instrument="SENSEX"))
        rebuilt.extend(rest)
        rebuilt.flush()
        assert "row_label_mismatch" in _codes(verify_identity(tree, _grid()))

    def test_row_mask_not_in_grid(self, tree: Path) -> None:
        store_path = _store_path(tree, _grid())
        (first, *rest) = ResultsStore.load(store_path).rows()
        rebuilt = ResultsStore(path=store_path)
        rebuilt.append(dataclasses.replace(first, mask_bits=1 << 50))
        rebuilt.extend(rest)
        rebuilt.flush()
        assert "row_mask_not_in_grid" in _codes(verify_identity(tree, _grid()))

    def test_row_run_id_mismatch(self, tree: Path) -> None:
        store_path = _store_path(tree, _grid())
        (first, *rest) = ResultsStore.load(store_path).rows()
        rebuilt = ResultsStore(path=store_path)
        rebuilt.append(dataclasses.replace(first, run_id="f" * 64))
        rebuilt.extend(rest)
        rebuilt.flush()
        assert "row_run_id_mismatch" in _codes(verify_identity(tree, _grid()))

    def test_conflict_mask_check_requires_trust(self, tree: Path) -> None:
        # Pick a REAL conflicting pair from the vocabulary, plant it, and pin
        # that the check fires only under vocabulary_trusted=True.
        pair = next(
            (a, b) for a in range(52) for b in range(a + 1, 52) if has_conflict((1 << a) | (1 << b))
        )
        grid = _grid(condition_bits=list(pair), k_values=[2])
        (tuple_,) = iter_sweep_tuples(grid)
        store_path = tuple_store_path(tree, tuple_, grid)
        mask = (1 << pair[0]) | (1 << pair[1])
        cfg = tuple_.to_run_config(grid)
        run_id = mask_run_id(
            mask=mask,
            direction="bull",
            instrument=cfg.instrument,
            tf=cfg.tf,
            mode=cfg.mode,
            params=canonical_trade_params(
                sl_paisa=cfg.sl_paisa,
                tp_paisa=cfg.tp_paisa,
                prior_n=cfg.prior_n,
                proximity=cfg.proximity,
                start_equity_paisa=cfg.start_equity_paisa,
            ),
            data_snapshot_hash=cfg.data_snapshot_hash,
            indicator_version=cfg.indicator_version,
            code_commit_sha=cfg.code_commit_sha,
        )
        template = ResultsStore.load(_store_path(tree, _grid())).rows()[0]
        planted = ResultsStore(path=store_path)
        planted.append(dataclasses.replace(template, run_id=run_id, mask_bits=mask))
        planted.flush()
        _store_path(tree, _grid()).unlink()  # keep ONE attributed store

        trusted = verify_identity(tree, grid, vocabulary_trusted=True)
        assert "row_conflict_mask" in _codes(trusted)
        untrusted = verify_identity(tree, grid)
        assert "row_conflict_mask" not in _codes(untrusted)


# ── Stage-3 cell attribution (plan_expected_stores) ──────────────────────────


def _stage3_record(**overrides: object) -> object:
    from brutex.engine.cascade_wire import Stage3CellRecord, Stage3ManifestRecord

    cell = Stage3CellRecord(
        instrument="NIFTY",
        tf_label=str(overrides.pop("tf_label", "1minute")),
        direction_label="bull",
        k=1,
        sl_paisa=85,
        tp_paisa=60,
        proximity_bps=(10, 20, 30),
    )
    return Stage3ManifestRecord(enabled=True, train_snapshot_hash="ab" * 32, refined_cells=(cell,))


class TestStage3Plan:
    def test_no_record_plans_grid_only(self, tmp_path: Path) -> None:
        plan = plan_expected_stores(tmp_path, _grid())
        assert len(plan) == 1
        ((_tuple, _grid_owner, is_grid),) = plan.values()
        assert is_grid is True

    def test_disabled_record_plans_grid_only(self, tmp_path: Path) -> None:
        from brutex.engine.cascade_wire import Stage3ManifestRecord

        record = Stage3ManifestRecord(enabled=False, train_snapshot_hash=None, refined_cells=())
        assert len(plan_expected_stores(tmp_path, _grid(), stage3=record)) == 1

    def test_enabled_record_attributes_cells(self, tmp_path: Path) -> None:
        plan = plan_expected_stores(tmp_path, _grid(), stage3=_stage3_record())  # type: ignore[arg-type]
        assert len(plan) == 2
        flags = sorted(is_grid for _, _, is_grid in plan.values())
        assert flags == [False, True]

    def test_cell_with_unknown_timeframe_raises(self, tmp_path: Path) -> None:
        with pytest.raises(SweepVerifyError, match="not in the grid"):
            plan_expected_stores(
                tmp_path,
                _grid(),
                stage3=_stage3_record(tf_label="5minute"),  # type: ignore[arg-type]
            )

    def test_cell_store_is_exempt_from_the_topk_bound(self, tree: Path, tmp_path: Path) -> None:
        # A cell store legitimately accretes rows; the top_k bound must not
        # condemn it (review condition: grid tuples only).
        grid = _grid(top_k=1)
        record = _stage3_record()
        plan = plan_expected_stores(tree, grid, stage3=record)  # type: ignore[arg-type]
        (cell_path,) = [p for p, (_, _, is_grid) in plan.items() if not is_grid]
        template = ResultsStore.load(_store_path(tree, _grid())).rows()[0]
        cell_store = ResultsStore(path=cell_path)
        cell_store.append(dataclasses.replace(template, run_id="a" * 64))
        cell_store.append(dataclasses.replace(template, run_id="b" * 64, mask_bits=1 << 2))
        cell_store.flush()
        # Remove the grid store: the tree was swept with top_k=None, so under
        # this top_k=1 grid its rows would LEGITIMATELY trip the bound — only
        # the cell store's exemption is under test here.
        _store_path(tree, _grid()).unlink()
        report = verify_identity(tree, grid, stage3=record)  # type: ignore[arg-type]
        assert "rows_exceed_top_k" not in _codes(report)


# ── Tier 3 — compare_recount (the shadow re-sweep byte-diff) ─────────────────


class TestRecountCompare:
    def _two_trees(
        self, tmp_path: Path, **grid_overrides: object
    ) -> tuple[Path, Path, SweepGridConfig]:
        """Sweep the SAME grid into two roots — deterministic ⇒ byte-identical."""
        grid = _grid(**grid_overrides)
        original, scratch = tmp_path / "original", tmp_path / "scratch"
        for root in (original, scratch):
            root.mkdir()
            _sweep(root, grid)
        return original, scratch, grid

    def test_missing_roots_and_same_root_raise(self, tmp_path: Path) -> None:
        (tmp_path / "a").mkdir()
        with pytest.raises(SweepVerifyError, match="original_root not found"):
            compare_recount(tmp_path / "nope", tmp_path / "a", {}, context_match=True)
        with pytest.raises(SweepVerifyError, match="scratch_root not found"):
            compare_recount(tmp_path / "a", tmp_path / "nope", {}, context_match=True)
        with pytest.raises(SweepVerifyError, match="must be DISTINCT"):
            compare_recount(tmp_path / "a", tmp_path / "a", {}, context_match=True)

    def test_identical_trees_verify_clean(self, tmp_path: Path) -> None:
        original, scratch, grid = self._two_trees(tmp_path)
        plan = plan_expected_stores(original, grid)
        report = compare_recount(original, scratch, plan, context_match=True)
        assert report.tier == "recount"
        assert report.findings == ()
        assert report.clean is True
        assert report.checked_stores == 3  # store + sidecar + sentinel

    def test_divergence_under_matched_context_is_corrupt(self, tmp_path: Path) -> None:
        original, scratch, grid = self._two_trees(tmp_path)
        store = _store_path(original, grid)
        store.write_bytes(store.read_bytes() + b"tampered")
        plan = plan_expected_stores(original, grid)
        report = compare_recount(original, scratch, plan, context_match=True)
        assert "recount_divergence" in _codes(report)
        assert not report.clean

    def test_divergence_under_mismatch_is_inconclusive_never_condemned(
        self, tmp_path: Path
    ) -> None:
        original, scratch, grid = self._two_trees(tmp_path)
        store = _store_path(original, grid)
        store.write_bytes(store.read_bytes() + b"tampered")
        plan = plan_expected_stores(original, grid)
        report = compare_recount(original, scratch, plan, context_match=False)
        assert "recount_divergence_inconclusive" in _codes(report)
        assert "recount_divergence" not in _codes(report)
        assert report.clean is True  # NOT evidence of corruption

    def test_scratch_absent_is_unverifiable_warn(self, tmp_path: Path) -> None:
        original, scratch, grid = self._two_trees(tmp_path)
        _store_path(scratch, grid).unlink()
        plan = plan_expected_stores(original, grid)
        report = compare_recount(original, scratch, plan, context_match=True)
        assert "recount_scratch_absent" in _codes(report)
        assert report.clean is True

    def test_original_absent_is_pending_warn(self, tmp_path: Path) -> None:
        original, scratch, grid = self._two_trees(tmp_path)
        _store_path(original, grid).unlink()
        plan = plan_expected_stores(original, grid)
        report = compare_recount(original, scratch, plan, context_match=True)
        assert "recount_original_absent" in _codes(report)
        assert report.clean is True

    def test_absent_in_both_worlds_is_silent(self, tmp_path: Path) -> None:
        # A tuple with no artifacts ANYWHERE (e.g. no-bars in both sweeps)
        # contributes nothing — not even a count.
        original, scratch, grid = self._two_trees(tmp_path, directions=[Side.BUY, Side.SELL])
        plan = plan_expected_stores(original, grid)
        assert len(plan) == 2
        for store, (tuple_, _, _) in plan.items():
            if tuple_.direction is Side.SELL:
                for root in (original, scratch):
                    rel = store.relative_to(original)
                    (root / rel).unlink()
                    trial_stats_sidecar_path(root / rel).unlink()
                    completion_sentinel_path(root / rel).unlink()
        report = compare_recount(original, scratch, plan, context_match=True)
        assert report.findings == ()
        assert report.checked_stores == 3  # only the surviving tuple's triple


# ── VIX-only recount divergence (DEC-VIX-STAMP-WIRE-001 round-2 BLOCKER) ─────


class TestRecountVixDivergence:
    """A VIX-lake movement between run and recount is warn-attributed, never CORRUPT.

    The residual envelope of the recount-stamper BLOCKER fix: VIX sits outside
    the recorded context AND the run identity, so a VIX re-pull / 1day
    re-derivation between the original run and the recount byte-diverges the
    trio (and the sentinel's store-hash re-pair) even under a fully matched
    context — the comparator must attribute it precisely instead of
    condemning a provably-reproduced store as heal-grade corruption.
    """

    def _two_trees(
        self,
        tmp_path: Path,
        original_stamper: TradeReferenceStamper | None,
        scratch_stamper: TradeReferenceStamper | None,
    ) -> tuple[Path, Path, SweepGridConfig]:
        grid = _grid()
        original, scratch = tmp_path / "original", tmp_path / "scratch"
        for root, stamper in ((original, original_stamper), (scratch, scratch_stamper)):
            root.mkdir()
            _sweep(root, grid, reference_stamper=stamper)
        return original, scratch, grid

    def test_identical_stamped_trees_verify_clean(self, tmp_path: Path) -> None:
        # THE BLOCKER regression end-state: a recount stamped from the SAME
        # lake state as the run byte-reproduces the whole triple — silence.
        original, scratch, grid = self._two_trees(
            tmp_path, _verify_stamper("14.00"), _verify_stamper("14.00")
        )
        plan = plan_expected_stores(original, grid)
        report = compare_recount(original, scratch, plan, context_match=True)
        assert report.findings == ()
        assert report.clean is True

    def test_vix_moved_lake_is_warn_never_corrupt(self, tmp_path: Path) -> None:
        original, scratch, grid = self._two_trees(
            tmp_path, _verify_stamper("14.00"), _verify_stamper("20.00")
        )
        plan = plan_expected_stores(original, grid)
        report = compare_recount(original, scratch, plan, context_match=True)
        # ALL THREE artifacts land in the VIX family: the store (the trio
        # columns) plus BOTH pairing artifacts (sidecar + sentinel each embed
        # the store's content hash, so they necessarily re-pair).
        assert _codes(report) == {"recount_vix_divergence"}
        assert len(report.findings) == 3
        assert report.clean is True  # warn-grade — NEVER heal-grade
        for finding in report.findings:
            assert finding.severity == "warn"
            assert "India-VIX lake moved" in finding.detail

    def test_store_pair_read_and_hashed_once_per_tuple(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The sentinel + sidecar re-pair checks share ONE memoised store digest pair.

        Fails-before (round-7 NIT): a VIX-diverged tuple's attribution read +
        re-hashed BOTH store files once per pairing artifact —
        ``_hashes_pair_with_stores`` was invoked independently from the
        sentinel AND the sidecar checks — 3 ``read_bytes`` per store (the
        artifact byte-diff + 2 re-pair reads). The digests now ride the
        ``_diff_store`` memo beside the row-level VIX verdict: exactly 2 reads
        per store (the byte-diff + ONE memoised digest computation).
        """
        original, scratch, grid = self._two_trees(
            tmp_path, _verify_stamper("14.00"), _verify_stamper("20.00")
        )
        store_strs = {
            str(_store_path(original, grid)),
            str(_store_path(scratch, grid)),
        }
        counts: dict[str, int] = {}
        real_read_bytes = Path.read_bytes

        def counting_read_bytes(self: Path) -> bytes:
            if str(self) in store_strs:
                counts[str(self)] = counts.get(str(self), 0) + 1
            return real_read_bytes(self)

        monkeypatch.setattr(Path, "read_bytes", counting_read_bytes)
        plan = plan_expected_stores(original, grid)
        report = compare_recount(original, scratch, plan, context_match=True)
        assert _codes(report) == {"recount_vix_divergence"}  # attribution unchanged
        assert len(report.findings) == 3
        assert counts and set(counts.values()) == {2}, counts

    def test_stamped_vs_unstamped_recount_is_vix_warn(self, tmp_path: Path) -> None:
        # A RECORDED value the recount can no longer reproduce (value-vs-None)
        # is a VIX-only divergence of the MOVED shape — the recorded stamps
        # contradict the current lake, so the lake-moved wording is earned.
        original, scratch, grid = self._two_trees(tmp_path, _verify_stamper("14.00"), None)
        plan = plan_expected_stores(original, grid)
        report = compare_recount(original, scratch, plan, context_match=True)
        assert _codes(report) == {"recount_vix_divergence"}
        assert report.clean is True
        for finding in report.findings:
            assert "India-VIX lake moved" in finding.detail

    def test_pre_vix_tree_recount_is_the_backfill_variant_not_lake_moved(
        self, tmp_path: Path
    ) -> None:
        """Round-4 panel MINOR: the all-None-original shape must NOT claim a lake move.

        A results tree whose rows carry NO trio (a store written before the
        VIX columns landed — the committed pre-VIX fixtures prove such trees
        exist — or a VIX lake that has since GAINED coverage) recounted under
        a VIX-carrying lake diverges ONLY as None→value. Nothing recorded was
        contradicted, so the detail must state the backfill shape and the
        plain-redo remedy — the old detail definitively (and falsely) claimed
        "the India-VIX lake moved" and prescribed an over-heavy quarantine.
        """
        original, scratch, grid = self._two_trees(tmp_path, None, _verify_stamper("14.00"))
        plan = plan_expected_stores(original, grid)
        report = compare_recount(original, scratch, plan, context_match=True)
        assert _codes(report) == {"recount_vix_divergence"}
        assert report.clean is True  # same family, same warn grade
        for finding in report.findings:
            assert finding.severity == "warn"
            assert "India-VIX lake moved" not in finding.detail
            assert "original rows carry NO trio values" in finding.detail
            assert "None→value backfill" in finding.detail
            assert "no quarantine needed" in finding.detail

    def test_helper_names_the_backfill_shape_for_an_all_none_original(self, tmp_path: Path) -> None:
        original, scratch, grid = self._two_trees(tmp_path, None, _verify_stamper("14.00"))
        assert (
            _stores_differ_only_in_vix(_store_path(original, grid), _store_path(scratch, grid))
            == "backfill"
        )

    def test_pre_vix_schema_tree_recount_is_the_backfill_variant(self, tmp_path: Path) -> None:
        """Round-5 panel MINOR: the SCHEMA-generation shape must not drop to the ladder.

        A pre-VIX-generation store (no trio COLUMNS on disk) loads an
        all-``None`` trio, so an unstamped recount reproduces every row fully
        EQUAL — yet the recount's reflush appends the three all-null columns
        and the bytes can NEVER match. Pre-fix the helper abstained on
        fully-equal rows and this landed on the generic ladder — under a
        matched context that is verdict-grade ``recount_divergence`` CORRUPT,
        a false condemnation of a provably-reproduced store.
        """
        original, scratch, grid = self._two_trees(tmp_path, None, None)
        store_path = _store_path(original, grid)
        _drop_store_columns(store_path, ["vix_mean", "vix_min", "vix_max"])
        assert store_path.read_bytes() != _store_path(scratch, grid).read_bytes()
        plan = plan_expected_stores(original, grid)
        report = compare_recount(original, scratch, plan, context_match=True)
        assert _codes(report) == {"recount_vix_divergence"}
        assert len(report.findings) == 1  # sidecar+sentinel byte-identical → silent
        assert report.clean is True  # warn-grade — NEVER heal-grade
        finding = report.findings[0]
        assert finding.severity == "warn"
        assert "India-VIX lake moved" not in finding.detail
        assert "original rows carry NO trio values" in finding.detail
        assert "None→value backfill" in finding.detail
        assert "no quarantine needed" in finding.detail

    def test_vix_divergence_under_context_mismatch_keeps_the_precise_code(
        self, tmp_path: Path
    ) -> None:
        # The attribution is exact regardless of the context gate — a proven
        # VIX-only diff is more informative than a generic inconclusive.
        original, scratch, grid = self._two_trees(
            tmp_path, _verify_stamper("14.00"), _verify_stamper("20.00")
        )
        plan = plan_expected_stores(original, grid)
        report = compare_recount(original, scratch, plan, context_match=False)
        assert _codes(report) == {"recount_vix_divergence"}
        assert "recount_divergence_inconclusive" not in _codes(report)

    def _rewrite_store(self, path: Path, rows: tuple[object, ...]) -> None:
        replacement = ResultsStore(path=path)
        for row in rows:
            replacement.append(row)  # type: ignore[arg-type]
        replacement.flush()

    def test_non_vix_row_difference_stays_corrupt(self, tmp_path: Path) -> None:
        original, scratch, grid = self._two_trees(
            tmp_path, _verify_stamper("14.00"), _verify_stamper("14.00")
        )
        store_path = _store_path(original, grid)
        rows = ResultsStore.load(store_path).rows()
        self._rewrite_store(store_path, (dataclasses.replace(rows[0], sharpe_ratio=9.9), *rows[1:]))
        plan = plan_expected_stores(original, grid)
        report = compare_recount(original, scratch, plan, context_match=True)
        assert _codes(report) == {"recount_divergence"}
        assert not report.clean

    def test_run_id_set_drift_stays_corrupt(self, tmp_path: Path) -> None:
        original, scratch, grid = self._two_trees(
            tmp_path, _verify_stamper("14.00"), _verify_stamper("14.00")
        )
        store_path = _store_path(original, grid)
        rows = ResultsStore.load(store_path).rows()
        self._rewrite_store(store_path, (dataclasses.replace(rows[0], run_id="f" * 64), *rows[1:]))
        plan = plan_expected_stores(original, grid)
        report = compare_recount(original, scratch, plan, context_match=True)
        assert "recount_divergence" in _codes(report)
        assert "recount_vix_divergence" not in _codes(report)

    def test_equal_rows_are_never_vix_attributed(self, tmp_path: Path) -> None:
        # Fully-EQUAL rows mean the byte diff (if any) has a non-row cause —
        # the helper honestly abstains instead of guessing VIX.
        original, scratch, grid = self._two_trees(
            tmp_path, _verify_stamper("14.00"), _verify_stamper("14.00")
        )
        assert (
            _stores_differ_only_in_vix(_store_path(original, grid), _store_path(scratch, grid))
            is None
        )

    def test_helper_attributes_a_mixed_equal_and_vix_diff_row_set(self, tmp_path: Path) -> None:
        # One row equal + one row differing ONLY in the trio → attributed.
        original, scratch, grid = self._two_trees(
            tmp_path, _verify_stamper("14.00"), _verify_stamper("14.00")
        )
        store_path = _store_path(original, grid)
        rows = ResultsStore.load(store_path).rows()
        assert len(rows) >= 2  # the fixture persists a multi-row leaderboard
        scratch_store_path = _store_path(scratch, grid)
        scratch_rows = ResultsStore.load(scratch_store_path).rows()
        self._rewrite_store(
            scratch_store_path,
            (dataclasses.replace(scratch_rows[0], vix_max=99.0), *scratch_rows[1:]),
        )
        # value-vs-value on vix_max → the MOVED shape (a recorded value differs).
        assert _stores_differ_only_in_vix(store_path, scratch_store_path) == "moved"

    def test_tampered_sentinel_in_a_vix_moved_pair_stays_on_the_ladder(
        self, tmp_path: Path
    ) -> None:
        original, scratch, grid = self._two_trees(
            tmp_path, _verify_stamper("14.00"), _verify_stamper("20.00")
        )
        sentinel = completion_sentinel_path(_store_path(original, grid))
        sentinel.write_text("{not json", encoding="utf-8")
        plan = plan_expected_stores(original, grid)
        report = compare_recount(original, scratch, plan, context_match=True)
        # The store keeps its precise VIX attribution; the unreadable sentinel
        # is NEVER folded into it — it stays verdict-grade on the ladder.
        assert _codes(report) == {"recount_vix_divergence", "recount_divergence"}
        assert not report.clean

    def test_sentinel_non_hash_field_drift_is_not_the_vix_repair(self, tmp_path: Path) -> None:
        original, scratch, grid = self._two_trees(
            tmp_path, _verify_stamper("14.00"), _verify_stamper("20.00")
        )
        sentinel = completion_sentinel_path(_store_path(original, grid))
        recorded = read_completion_sentinel(sentinel)
        write_completion_sentinel(
            sentinel,
            TupleCompletionSentinel(
                tuple_digest=recorded.tuple_digest,
                store_content_hash=recorded.store_content_hash,
                sidecar_written=recorded.sidecar_written,
                n_marked=recorded.n_marked + 1,
            ),
        )
        plan = plan_expected_stores(original, grid)
        report = compare_recount(original, scratch, plan, context_match=True)
        assert _codes(report) == {"recount_vix_divergence", "recount_divergence"}

    def test_sentinel_stale_hash_is_not_the_vix_repair(self, tmp_path: Path) -> None:
        original, scratch, grid = self._two_trees(
            tmp_path, _verify_stamper("14.00"), _verify_stamper("20.00")
        )
        sentinel = completion_sentinel_path(_store_path(original, grid))
        recorded = read_completion_sentinel(sentinel)
        write_completion_sentinel(
            sentinel,
            TupleCompletionSentinel(
                tuple_digest=recorded.tuple_digest,
                store_content_hash="0" * 64,  # no longer pairs with its own store
                sidecar_written=recorded.sidecar_written,
                n_marked=recorded.n_marked,
            ),
        )
        plan = plan_expected_stores(original, grid)
        report = compare_recount(original, scratch, plan, context_match=True)
        assert _codes(report) == {"recount_vix_divergence", "recount_divergence"}
        # Direct pin of the final pairing arm (digests computed like the
        # _diff_store memo — once per store).
        import hashlib

        store_digests = (
            hashlib.sha256(_store_path(original, grid).read_bytes()).hexdigest(),
            hashlib.sha256(_store_path(scratch, grid).read_bytes()).hexdigest(),
        )
        assert (
            _sentinel_diff_is_the_vix_repair(
                sentinel,
                completion_sentinel_path(_store_path(scratch, grid)),
                store_digests,
            )
            is False
        )

    def test_sentinel_divergence_with_missing_original_store_stays_on_the_ladder(
        self, tmp_path: Path
    ) -> None:
        original, scratch, grid = self._two_trees(
            tmp_path, _verify_stamper("14.00"), _verify_stamper("20.00")
        )
        _store_path(original, grid).unlink()
        plan = plan_expected_stores(original, grid)
        report = compare_recount(original, scratch, plan, context_match=True)
        codes = _codes(report)
        assert "recount_original_absent" in codes  # the store artifact
        assert "recount_divergence" in codes  # the sentinel — never VIX-guessed
        assert "recount_vix_divergence" not in codes

    def test_sidecar_divergence_with_identical_stores_stays_on_the_ladder(
        self, tmp_path: Path
    ) -> None:
        # The store pair is byte-identical → nothing is VIX-attributable; a
        # diverging sidecar is judged on the normal ladder.
        original, scratch, grid = self._two_trees(
            tmp_path, _verify_stamper("14.00"), _verify_stamper("14.00")
        )
        sidecar = trial_stats_sidecar_path(_store_path(original, grid))
        sidecar.write_bytes(sidecar.read_bytes() + b"tampered")
        plan = plan_expected_stores(original, grid)
        report = compare_recount(original, scratch, plan, context_match=True)
        assert _codes(report) == {"recount_divergence"}

    def test_tampered_sidecar_in_a_vix_moved_pair_stays_on_the_ladder(self, tmp_path: Path) -> None:
        original, scratch, grid = self._two_trees(
            tmp_path, _verify_stamper("14.00"), _verify_stamper("20.00")
        )
        sidecar = trial_stats_sidecar_path(_store_path(original, grid))
        sidecar.write_text("{not json", encoding="utf-8")
        plan = plan_expected_stores(original, grid)
        report = compare_recount(original, scratch, plan, context_match=True)
        # store + sentinel keep the precise VIX attribution; the unreadable
        # sidecar is never folded into it.
        assert _codes(report) == {"recount_vix_divergence", "recount_divergence"}

    def test_sidecar_statistic_drift_is_not_the_vix_repair(self, tmp_path: Path) -> None:
        original, scratch, grid = self._two_trees(
            tmp_path, _verify_stamper("14.00"), _verify_stamper("20.00")
        )
        sidecar = trial_stats_sidecar_path(_store_path(original, grid))
        recorded = read_tuple_trial_stats(sidecar)
        write_tuple_trial_stats(
            sidecar, dataclasses.replace(recorded, n_enumerated=recorded.n_enumerated + 1)
        )
        plan = plan_expected_stores(original, grid)
        report = compare_recount(original, scratch, plan, context_match=True)
        assert _codes(report) == {"recount_vix_divergence", "recount_divergence"}

    def test_sidecar_stale_hash_is_not_the_vix_repair(self, tmp_path: Path) -> None:
        original, scratch, grid = self._two_trees(
            tmp_path, _verify_stamper("14.00"), _verify_stamper("20.00")
        )
        sidecar = trial_stats_sidecar_path(_store_path(original, grid))
        recorded = read_tuple_trial_stats(sidecar)
        write_tuple_trial_stats(sidecar, dataclasses.replace(recorded, store_content_hash="0" * 64))
        plan = plan_expected_stores(original, grid)
        report = compare_recount(original, scratch, plan, context_match=True)
        assert _codes(report) == {"recount_vix_divergence", "recount_divergence"}

    def test_cell_vix_divergence_gets_the_cell_code(self, tmp_path: Path) -> None:
        # Stage-3 cell stores persist the trio through the SAME projector —
        # a VIX-moved cell recount gets its OWN non-corrupt code (never
        # aliasing the grid family, condition B2).
        original, scratch, grid = self._two_trees(tmp_path, None, None)
        record = _stage3_record()
        plan = plan_expected_stores(original, grid, stage3=record)  # type: ignore[arg-type]
        (cell_rel,) = [
            p.relative_to(original) for p, (_, _, is_grid) in plan.items() if not is_grid
        ]
        template = ResultsStore.load(_store_path(original, grid)).rows()[0]
        for root, mean in ((original, 14.0), (scratch, 20.0)):
            dest = root / cell_rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            cell_store = ResultsStore(path=dest)
            cell_store.append(
                dataclasses.replace(template, vix_mean=mean, vix_min=mean - 1.0, vix_max=mean + 1.0)
            )
            cell_store.flush()
        report = compare_recount(original, scratch, plan, context_match=True, cell_evidence=True)
        assert _codes(report) == {"cell_recount_vix_divergence"}
        assert report.clean is True


class TestVixFieldsSingleSource:
    """The trio column names have ONE owner (CLAUDE.md §2 Rule 11 COMMON)."""

    def test_sweep_verify_imports_results_store_vix_fields(self) -> None:
        """A fourth field added to ``results_store.VIX_FIELDS`` can never
        silently desync the recount attribution: ``sweep_verify`` binds the
        SAME tuple object, never a mirrored literal (review finding — the old
        ``_VIX_TRIO_COLUMNS`` mirror plus hardcoded ``replace()`` names had no
        cross-module pin)."""
        from brutex.engine import results_store, sweep_verify

        # vars(): sweep_verify deliberately IMPORTS (not re-exports) the name,
        # so attribute access would trip strict mypy's no_implicit_reexport.
        assert vars(sweep_verify)["VIX_FIELDS"] is results_store.VIX_FIELDS
        assert results_store.VIX_FIELDS == ("vix_mean", "vix_min", "vix_max")


class TestNonHashFieldsEqual:
    """The re-pair equality gates are :func:`dataclasses.fields`-driven.

    Review finding: the old hand-enumerated field tuples were exhaustive at
    the time but UNPINNED for later — a field added to either sidecar model
    would silently escape the gate, and a divergence in it could then be
    attributed into the non-corrupt "honest VIX re-pair" family. These tests
    prove the gate is drift-proof by construction: a SUBCLASS field it has
    never heard of is automatically part of the equality check.
    """

    _SENTINEL_KW: ClassVar[dict[str, object]] = {
        "tuple_digest": "ab" * 8,
        "store_content_hash": "cd" * 32,
        "sidecar_written": True,
        "n_marked": 3,
    }
    _STATS_KW: ClassVar[dict[str, object]] = {
        "n_backtested": 5,
        "n_finite": 4,
        "sum_sharpe": 1.5,
        "sum_sharpe_sq": 2.0,
        "n_enumerated": 10,
        "n_conflict_pruned": 3,
        "n_minhits_dropped": 2,
        "tuple_digest": "ab" * 8,
        "store_content_hash": "cd" * 32,
    }

    def test_hash_only_divergence_passes_the_gate(self) -> None:
        ours = TupleCompletionSentinel(**self._SENTINEL_KW)  # type: ignore[arg-type]
        theirs = dataclasses.replace(ours, store_content_hash="ef" * 32)
        assert _non_hash_fields_equal(ours, theirs) is True

    def test_every_existing_non_hash_field_is_gated(self) -> None:
        sentinel = TupleCompletionSentinel(**self._SENTINEL_KW)  # type: ignore[arg-type]
        for change in ({"tuple_digest": "ba" * 8}, {"sidecar_written": False}, {"n_marked": 4}):
            assert _non_hash_fields_equal(sentinel, dataclasses.replace(sentinel, **change)) is (
                False
            )
        stats = tuple_trial_stats_from_sharpes(
            [0.5, 1.0],
            n_enumerated=4,
            n_conflict_pruned=1,
            n_minhits_dropped=1,
            tuple_digest="ab" * 8,
            store_content_hash="cd" * 32,
        )
        assert _non_hash_fields_equal(stats, stats) is True
        assert (
            _non_hash_fields_equal(stats, dataclasses.replace(stats, store_content_hash="ef" * 32))
            is True
        )
        assert (
            _non_hash_fields_equal(
                stats, dataclasses.replace(stats, n_enumerated=stats.n_enumerated + 2)
            )
            is False
        )

    def test_a_future_sentinel_field_cannot_escape_the_gate(self) -> None:
        @dataclasses.dataclass(frozen=True, slots=True)
        class _Extended(TupleCompletionSentinel):
            extra: int = 0

        ours = _Extended(**self._SENTINEL_KW, extra=1)  # type: ignore[arg-type]
        assert _non_hash_fields_equal(ours, _Extended(**self._SENTINEL_KW, extra=1)) is True  # type: ignore[arg-type]
        # The gate sees the field it was never hand-taught about.
        assert _non_hash_fields_equal(ours, _Extended(**self._SENTINEL_KW, extra=2)) is False  # type: ignore[arg-type]

    def test_a_future_sidecar_field_cannot_escape_the_gate(self) -> None:
        from brutex.engine.trial_stats import TupleTrialStats

        @dataclasses.dataclass(frozen=True, slots=True)
        class _Extended(TupleTrialStats):
            extra: int = 0

        ours = _Extended(**self._STATS_KW, extra=1)  # type: ignore[arg-type]
        assert _non_hash_fields_equal(ours, _Extended(**self._STATS_KW, extra=1)) is True  # type: ignore[arg-type]
        assert _non_hash_fields_equal(ours, _Extended(**self._STATS_KW, extra=2)) is False  # type: ignore[arg-type]


def _drop_store_columns(path: Path, columns: list[str]) -> None:
    """Rewrite *path* in place without *columns* — a pre-generation schema stand-in."""
    import polars as pl

    pl.read_parquet(path).drop(columns).write_parquet(path)


_PRE_VIX_FIXTURE = (
    Path(__file__).resolve().parent.parent
    / "golden"
    / "fixtures"
    / "results_store_pre_vix_columns.parquet"
)


class TestPreVixSchemaMigration:
    """Branch coverage of the round-5 schema-generation attribution helper.

    ``_is_pre_vix_schema_migration`` attributes a fully-equal-rows byte diff
    to the trio columns' EXISTENCE iff the only schema difference is the
    missing trio; every less-provable shape abstains (Golden Rule #1).
    """

    def _reflush(self, source: Path, dest: Path) -> None:
        """Reflush *source*'s rows through the CURRENT writer into *dest*."""
        store = ResultsStore(path=dest)
        for row in ResultsStore.load(source).rows():
            store.append(row)
        store.flush()

    def test_committed_pre_vix_fixture_reflushed_is_the_backfill_shape(
        self, tmp_path: Path
    ) -> None:
        """The panel's executed probe: the REAL pre-VIX fixture vs a current reflush.

        The committed fixture is PROVEN byte-identical to the real pre-Stage-C
        writer (its golden suite), so this is exactly what a `sweep-verify
        --recount` of a main-era tree produces over a VIX-less lake: bytes
        differ (the writer appends the three all-null trio columns), rows
        fully equal — the helper must attribute, not abstain.
        """
        original = tmp_path / "original.parquet"
        shutil.copyfile(_PRE_VIX_FIXTURE, original)
        scratch = tmp_path / "scratch.parquet"
        self._reflush(original, scratch)
        assert original.read_bytes() != scratch.read_bytes()  # never byte-identical
        assert _is_pre_vix_schema_migration(original, scratch) is True
        assert _stores_differ_only_in_vix(original, scratch) == "backfill"

    def test_partial_trio_presence_abstains(self, tmp_path: Path) -> None:
        # A store carrying SOME trio columns is not the pre-VIX generation.
        original = tmp_path / "original.parquet"
        scratch = tmp_path / "scratch.parquet"
        self._reflush(_PRE_VIX_FIXTURE, original)
        self._reflush(_PRE_VIX_FIXTURE, scratch)
        _drop_store_columns(original, ["vix_min", "vix_max"])  # vix_mean kept
        assert _is_pre_vix_schema_migration(original, scratch) is False
        assert _stores_differ_only_in_vix(original, scratch) is None

    def test_scratch_without_the_trio_abstains(self, tmp_path: Path) -> None:
        # The recount side must CARRY the trio for the migration to explain
        # the diff — two pre-VIX-generation files are not this shape.
        original = tmp_path / "original.parquet"
        scratch = tmp_path / "scratch.parquet"
        shutil.copyfile(_PRE_VIX_FIXTURE, original)
        shutil.copyfile(_PRE_VIX_FIXTURE, scratch)
        assert _is_pre_vix_schema_migration(original, scratch) is False

    def test_extra_schema_drift_abstains(self, tmp_path: Path) -> None:
        # Missing trio PLUS any other column drift — stripping the trio from
        # the scratch schema no longer reproduces the original: abstain.
        original = tmp_path / "original.parquet"
        scratch = tmp_path / "scratch.parquet"
        self._reflush(_PRE_VIX_FIXTURE, original)
        self._reflush(_PRE_VIX_FIXTURE, scratch)
        _drop_store_columns(original, ["vix_mean", "vix_min", "vix_max", "deflated_sharpe"])
        assert _is_pre_vix_schema_migration(original, scratch) is False

    def test_unreadable_footer_abstains(self, tmp_path: Path) -> None:
        original = tmp_path / "original.parquet"
        original.write_bytes(b"not a parquet footer")
        scratch = tmp_path / "scratch.parquet"
        self._reflush(_PRE_VIX_FIXTURE, scratch)
        assert _is_pre_vix_schema_migration(original, scratch) is False


# ── Stage-3 recount support (DEC-STAGE3-RECOUNT-001) ─────────────────────────


class TestRecordedStage3FromAudit:
    def test_absent_audit_is_no_history(self, tmp_path: Path) -> None:
        record = recorded_stage3_from_audit(tmp_path / "nope.jsonl")
        assert record.knobs is None
        assert record.consistent is True
        assert record.optimizer_versions == ()

    def _pass_payload(self, **overrides: object) -> dict[str, object]:
        base: dict[str, object] = {
            "stage3_top_k": 3,
            "max_fevals": 50,
            "max_restarts": 1,
            "train_snapshot_hash": "ab" * 32,
        }
        base.update(overrides)
        return base

    def test_reads_knobs_and_versions(self, tmp_path: Path) -> None:
        path = tmp_path / "a.jsonl"
        with AuditWriter.open(path) as audit:
            audit.append(action="STAGE_3_PASS_START", subject="s", payload=self._pass_payload())
            audit.append(
                action="STAGE_3_REFINE_START",
                subject="s",
                payload={"pycma_version": "3.4.0", "numpy_version": "2.1.0"},
            )
        record = recorded_stage3_from_audit(path)
        assert record.knobs is not None
        assert record.knobs.stage3_top_k == 3
        assert record.knobs.max_fevals == 50
        assert record.knobs.max_restarts == 1
        assert record.knobs.train_snapshot_hash == "ab" * 32
        assert record.consistent is True
        assert record.optimizer_versions == (("3.4.0", "2.1.0"),)

    def test_agreeing_repeat_lines_stay_consistent(self, tmp_path: Path) -> None:
        path = tmp_path / "a.jsonl"
        with AuditWriter.open(path) as audit:
            audit.append(action="STAGE_3_PASS_START", subject="s", payload=self._pass_payload())
            audit.append(action="STAGE_3_PASS_START", subject="s", payload=self._pass_payload())
        assert recorded_stage3_from_audit(path).consistent is True

    def test_disagreeing_lines_flag_inconsistent(self, tmp_path: Path) -> None:
        path = tmp_path / "a.jsonl"
        with AuditWriter.open(path) as audit:
            audit.append(action="STAGE_3_PASS_START", subject="s", payload=self._pass_payload())
            audit.append(
                action="STAGE_3_PASS_START",
                subject="s",
                payload=self._pass_payload(stage3_top_k=9),
            )
        record = recorded_stage3_from_audit(path)
        assert record.consistent is False  # B4: cells honestly non-reproducible

    def test_missing_knob_key_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "a.jsonl"
        payload = self._pass_payload()
        del payload["max_fevals"]
        with AuditWriter.open(path) as audit:
            audit.append(action="STAGE_3_PASS_START", subject="s", payload=payload)
        with pytest.raises(SweepVerifyError, match="missing"):
            recorded_stage3_from_audit(path)

    def test_unreadable_audit_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "a.jsonl"
        path.write_text("{not jsonl\n", encoding="utf-8")
        with pytest.raises(SweepVerifyError, match="unreadable audit log"):
            recorded_stage3_from_audit(path)


class TestCellRecountCodes:
    """compare_recount routes Stage-3 cell stores to the DISTINCT code family."""

    def _trees_with_cell(
        self, tmp_path: Path
    ) -> tuple[Path, Path, dict[Path, tuple[SweepTuple, SweepGridConfig, bool]], Path]:
        """Two identical swept trees + an identical planted CELL store in each."""
        grid = _grid()
        original, scratch = tmp_path / "original", tmp_path / "scratch"
        for root in (original, scratch):
            root.mkdir()
            _sweep(root, grid)
        record = _stage3_record()
        plan = plan_expected_stores(original, grid, stage3=record)  # type: ignore[arg-type]
        (cell_rel,) = [
            p.relative_to(original) for p, (_, _, is_grid) in plan.items() if not is_grid
        ]
        grid_store = _store_path(original, grid)
        for root in (original, scratch):
            dest = root / cell_rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(root / grid_store.relative_to(original), dest)
        return original, scratch, plan, original / cell_rel

    def test_identical_cells_verify_silently(self, tmp_path: Path) -> None:
        original, scratch, plan, _cell = self._trees_with_cell(tmp_path)
        report = compare_recount(original, scratch, plan, context_match=True, cell_evidence=True)
        assert report.findings == ()

    def test_cell_divergence_with_evidence_is_corrupt(self, tmp_path: Path) -> None:
        original, scratch, plan, cell = self._trees_with_cell(tmp_path)
        cell.write_bytes(cell.read_bytes() + b"tampered")
        report = compare_recount(original, scratch, plan, context_match=True, cell_evidence=True)
        assert _codes(report) == {"cell_recount_divergence"}
        assert not report.clean

    def test_cell_divergence_without_evidence_is_inconclusive(self, tmp_path: Path) -> None:
        original, scratch, plan, cell = self._trees_with_cell(tmp_path)
        cell.write_bytes(cell.read_bytes() + b"tampered")
        report = compare_recount(original, scratch, plan, context_match=True, cell_evidence=False)
        assert _codes(report) == {"cell_recount_divergence_inconclusive"}
        assert report.clean is True  # B1: never condemns without the full gate

    def test_cell_divergence_under_grid_corruption_is_dependent(self, tmp_path: Path) -> None:
        # B5: scratch cells derive from scratch grid stores — collateral only.
        original, scratch, plan, cell = self._trees_with_cell(tmp_path)
        grid_store = _store_path(original, _grid())
        grid_store.write_bytes(grid_store.read_bytes() + b"tampered")
        cell.write_bytes(cell.read_bytes() + b"tampered")
        report = compare_recount(original, scratch, plan, context_match=True, cell_evidence=True)
        codes = _codes(report)
        assert "recount_divergence" in codes  # the grid verdict stands
        assert "cell_recount_divergence_dependent" in codes
        assert "cell_recount_divergence" not in codes  # never independent here

    def test_grid_only_audit_has_no_stage3_history(self, tree: Path) -> None:
        # A real grid-only sweep audit (SWEEP_START/FLUSH/COMPLETE lines, no
        # stage3 pass) reads as the honest no-history record.
        record = recorded_stage3_from_audit(tree / "audit.jsonl")
        assert record.knobs is None
        assert record.consistent is True
        assert record.optimizer_versions == ()
