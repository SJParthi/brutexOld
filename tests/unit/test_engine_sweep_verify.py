"""Unit tests for the sweep-verify doctor core (`brutex.engine.sweep_verify`).

100% line + branch coverage of the two bars-free tiers (DEC-SWEEP-VERIFY-001):
every :class:`VerifyFinding` validation arm, the report properties + render,
the recorded-context audit reader (absent / valid / missing-key /
inconsistent / unreadable), every Tier-1 structural arm over a REAL swept
tree with surgically injected damage, and every Tier-2 identity arm including
THE #650 inflation signature (``rows > top_k``), the grid-mismatch refusal,
and the Stage-3 cell attribution arms.
"""

from __future__ import annotations

import dataclasses
import shutil
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.data.models import CandleBar
from brutex.data.option_dedup import OptionDedupIndex
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
    compare_recount,
    plan_expected_stores,
    recorded_context_from_audit,
    recorded_stage3_from_audit,
    verify_identity,
    verify_structural,
)
from brutex.engine.trial_stats import (
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


def _sweep(root: Path, grid: SweepGridConfig) -> None:
    from brutex.engine.sweep_orchestrator import make_store_for

    with AuditWriter.open(root / "audit.jsonl") as audit:
        run_sweep_grid(
            grid,
            bars_for=lambda _i, _t: _trending_bars(),
            store_for=make_store_for(root, grid),
            dedup=ComboDedup(OptionDedupIndex(capacity=10_000, fpr_target=1e-6)),
            audit=audit,
        )


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

    def test_mid_chain_corruption_after_valid_start_still_maps(self, tmp_path: Path) -> None:
        """DEC-AUDIT-READERS-STREAM-001: the streaming reader raises its
        AuditError lazily MID-iteration — a malformed committed line AFTER a
        successfully-folded ``SWEEP_START`` must still map to
        ``SweepVerifyError`` (the try spans the loop, not just the call)."""
        path = tmp_path / "a.jsonl"
        with AuditWriter.open(path) as audit:
            audit.append(
                action="SWEEP_START",
                subject="s",
                payload={
                    "data_snapshot_hash": "x",
                    "indicator_version": "v",
                    "code_commit_sha": "c",
                },
            )
        with path.open("a", encoding="utf-8") as f:
            f.write("{not jsonl mid-chain\n")  # committed (newline-terminated) corruption
        with pytest.raises(SweepVerifyError, match="unreadable audit log"):
            recorded_context_from_audit(path)

    def test_streams_without_materialising_the_chain(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Memory-shape pin (DEC-AUDIT-READERS-STREAM-001): the reader must
        ride ``iter_entries``, never the whole-chain ``read_entries`` list —
        the pre-fix code resolved ``read_entries`` at call time (function-
        local import), so this spy FAILS on the pre-fix implementation."""
        import brutex.orchestrator.audit as audit_mod

        def _boom(_path: Path) -> list[object]:
            raise AssertionError("read_entries called — chain materialised")

        monkeypatch.setattr(audit_mod, "read_entries", _boom)
        path = tmp_path / "a.jsonl"
        with AuditWriter.open(path) as audit:
            audit.append(
                action="SWEEP_START",
                subject="s",
                payload={
                    "data_snapshot_hash": "x",
                    "indicator_version": "v",
                    "code_commit_sha": "c",
                },
            )
        ctx = recorded_context_from_audit(path)
        assert ctx is not None
        assert ctx.data_snapshot_hash == "x"


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

    def test_mid_chain_corruption_after_valid_pass_still_maps(self, tmp_path: Path) -> None:
        """DEC-AUDIT-READERS-STREAM-001: a malformed committed line AFTER a
        successfully-folded ``STAGE_3_PASS_START`` still maps to
        ``SweepVerifyError`` — the try spans the streaming iteration."""
        path = tmp_path / "a.jsonl"
        with AuditWriter.open(path) as audit:
            audit.append(action="STAGE_3_PASS_START", subject="s", payload=self._pass_payload())
        with path.open("a", encoding="utf-8") as f:
            f.write("{not jsonl mid-chain\n")  # committed (newline-terminated) corruption
        with pytest.raises(SweepVerifyError, match="unreadable audit log"):
            recorded_stage3_from_audit(path)

    def test_streams_without_materialising_the_chain(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Memory-shape pin: the reader rides ``iter_entries``, never the
        whole-chain ``read_entries`` list (spy fails on the pre-fix code)."""
        import brutex.orchestrator.audit as audit_mod

        def _boom(_path: Path) -> list[object]:
            raise AssertionError("read_entries called — chain materialised")

        monkeypatch.setattr(audit_mod, "read_entries", _boom)
        path = tmp_path / "a.jsonl"
        with AuditWriter.open(path) as audit:
            audit.append(action="STAGE_3_PASS_START", subject="s", payload=self._pass_payload())
        record = recorded_stage3_from_audit(path)
        assert record.knobs is not None
        assert record.knobs.stage3_top_k == 3


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
