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
from brutex.engine.results_store import VIX_FIELDS, ResultsStore
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
    _APPENDED_NULLABLE_FIELDS,
    RecordedContext,
    SweepVerifyError,
    TreeVerifyReport,
    VerifyFinding,
    _census_fields_only_differ,
    _is_pre_vix_schema_migration,
    _non_hash_fields_equal,
    _sentinel_diff_is_the_vix_repair,
    _sidecar_diff_is_the_mode_census_residue,
    _stores_differ_only_in_vix,
    compare_recount,
    plan_expected_stores,
    recorded_apriori_from_audit,
    recorded_audit_facts,
    recorded_context_from_audit,
    recorded_overfit_from_audit,
    recorded_roll_schedules_from_audit,
    recorded_stage3_from_audit,
    stale_roll_schedule_refusal,
    verify_identity,
    verify_structural,
)
from brutex.engine.trial_stats import (
    MODE_CENSUS_FIELDS,
    TupleTrialStats,
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
from brutex.ranking import OverfitGateConfig
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
    # The worst-case-rankable gap-up recipe (DEC-FILL-WORST-CASE-ONLY-001):
    # narrow gap-up bars (range 0.50 < the SL 0.80 — no same-bar stop-out;
    # the next gap-up's low clears the TP level → +50 wins), a shallow
    # bearish bar each cycle and a DEEP bearish bar (range 0.85) every 4th
    # cycle whose stop-out keeps profit_factor finite — this suite's masks
    # (bits 0/2/3, k=1) all clear the hard rank gates under sl=80/tp=50.
    bars: list[CandleBar] = []
    cycle = 0
    for d in range(n_days):
        price = 100.0
        for m in range(per_day):
            k = m % 7
            if k == 0:
                cycle += 1
            if k == 3:  # shallow bearish bar (below-EMA bits fire)
                o = price
                c, h, lo = o - 0.3, o + 0.1, o - 0.4
            elif k == 4 and cycle % 4 == 0:  # deep bearish loss carrier
                o = price
                c, h, lo = o - 0.65, o + 0.1, o - 0.75
            else:  # narrow gap-up win bar
                o = price + 3.0
                c, h, lo = o + 0.3, o + 0.4, o - 0.1
            ts = (
                datetime(2026, 1, 5, 9, 16, tzinfo=_IST) + timedelta(days=d, minutes=m)
            ).astimezone(UTC)
            bars.append(
                CandleBar(
                    timestamp=ts,
                    open=Decimal(str(round(o, 2))),
                    high=Decimal(str(round(h, 2))),
                    low=Decimal(str(round(lo, 2))),
                    close=Decimal(str(round(c, 2))),
                    volume=100,
                    open_interest=None,
                )
            )
            price = c
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


def _alt_gate_store_path(root: Path, grid: SweepGridConfig) -> Path:
    """THIS single tuple's store address under the ONE divergent overfit family.

    The digest-precise (a)-floor probe's exact target
    (DEC-SWEEP-OVERFIT-RECOUNT-RECOVERY-001 review fix round): ``overfit=None``
    when *grid* is gated, the default :class:`OverfitGateConfig` when ungated —
    re-planned through the SAME ``tuple_store_path`` address law.
    """
    (tuple_,) = iter_sweep_tuples(grid)
    alternate = None if grid.overfit is not None else OverfitGateConfig()
    return tuple_store_path(root, tuple_, dataclasses.replace(grid, overfit=alternate))


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

    def test_shared_list_parse_maps_unreadable_audit(self, tmp_path: Path) -> None:
        """The VERIFIED seam's shared list parse keeps its AuditError mapping.

        Club reconcile pin: the streaming conversion moved
        ``recorded_context_from_audit`` off ``_entries_from_audit``, so the
        shared list parse's unreadable arm (rode by
        ``_verified_entries_from_audit`` after ``verify_chain`` — e.g. a file
        replaced between the verify and the parse) needs its own teeth.
        """
        from brutex.engine.sweep_verify import _entries_from_audit

        path = tmp_path / "a.jsonl"
        path.write_text("{not jsonl\n", encoding="utf-8")
        with pytest.raises(SweepVerifyError, match="unreadable audit log"):
            _entries_from_audit(path)

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
        from brutex.orchestrator import audit as audit_mod

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


# ── Recorded fill mode (DEC-FILL-WORST-CASE-ONLY-001) ──


class TestWorstCaseGenerationRefusal:
    """DEC-FILL-WORST-CASE-ONLY-001: `_require_worst_case_entries` via the
    facts seam — a scoped generation whose recorded params lack
    ``fill_mode == "worst_case"`` refuses LOUDLY at fact-derivation time
    (before any tier), naming the DEC; the remediation is a fresh sweep.
    Generation-scoping (round-5 finding 6) is PRESERVED: only the ONE scoped
    generation's context entries are judged.
    """

    _CTX: ClassVar[dict[str, str]] = {
        "data_snapshot_hash": "snap0001",
        "indicator_version": "ind-v1",
        "code_commit_sha": "deadbeef",
    }

    def _append(
        self,
        audit: AuditWriter,
        *,
        params: dict[str, object] | None,
        snapshot: str = "snap0001",
        action: str = "SWEEP_START",
    ) -> None:
        payload: dict[str, object] = {
            **self._CTX,
            "data_snapshot_hash": snapshot,
            "instrument": "NIFTY",
        }
        if params is not None:
            payload["params"] = params
        audit.append(action=action, subject="s", payload=payload)

    _REFUSAL = r"precise-era sweep tree refused \(DEC-FILL-WORST-CASE-ONLY-001\)"

    def test_absent_audit_returns_empty_facts_without_refusal(self, tmp_path: Path) -> None:
        # Nothing recorded → nothing to refuse (the caller supplies flags).
        assert recorded_audit_facts(tmp_path / "nope.jsonl") == (None, {}, ())

    def test_no_key_generation_refuses_as_precise_era(self, tmp_path: Path) -> None:
        # Fails-before pin: a pre-DEC audit (params without fill_mode — the
        # retired elide law recorded PRECISE by key ABSENCE) refuses loudly.
        path = tmp_path / "a.jsonl"
        with AuditWriter.open(path) as audit:
            self._append(audit, params={"sl_paisa": 80})
        with pytest.raises(SweepVerifyError, match=self._REFUSAL):
            recorded_audit_facts(path)

    def test_paramsless_entry_refuses_as_precise_era(self, tmp_path: Path) -> None:
        path = tmp_path / "a.jsonl"
        with AuditWriter.open(path) as audit:
            self._append(audit, params=None)
        with pytest.raises(SweepVerifyError, match=self._REFUSAL):
            recorded_audit_facts(path)

    def test_literal_precise_token_refuses(self, tmp_path: Path) -> None:
        path = tmp_path / "a.jsonl"
        with AuditWriter.open(path) as audit:
            self._append(audit, params={"fill_mode": "precise"})
        with pytest.raises(SweepVerifyError, match=self._REFUSAL):
            recorded_audit_facts(path)

    def test_junk_token_refuses_identically(self, tmp_path: Path) -> None:
        path = tmp_path / "a.jsonl"
        with AuditWriter.open(path) as audit:
            self._append(audit, params={"fill_mode": "worst"})
        with pytest.raises(SweepVerifyError, match=self._REFUSAL):
            recorded_audit_facts(path)

    def test_worst_case_generation_reads_clean(self, tmp_path: Path) -> None:
        path = tmp_path / "a.jsonl"
        with AuditWriter.open(path) as audit:
            self._append(audit, params={"fill_mode": "worst_case", "sl_paisa": 80})
            self._append(audit, params={"fill_mode": "worst_case"})
        context, _schedules, others = recorded_audit_facts(path)
        assert context is not None
        assert context.data_snapshot_hash == "snap0001"
        assert others == ()

    def test_refusal_is_generation_scoped(self, tmp_path: Path) -> None:
        # Generation A is precise-era (no key), generation B is worst-case:
        # the scoped B read succeeds, the scoped A read refuses, and the
        # unscoped read (latest = B) succeeds — the refusal never folds the
        # whole file (the round-5 scoping law preserved).
        path = tmp_path / "a.jsonl"
        with AuditWriter.open(path) as audit:
            self._append(audit, params={"sl_paisa": 80}, snapshot="snap0001")
            self._append(audit, params={"fill_mode": "worst_case"}, snapshot="snap0002")
        context, _schedules, others = recorded_audit_facts(path, snapshot="snap0002")
        assert context is not None
        assert context.data_snapshot_hash == "snap0002"
        assert others == ("snap0001",)
        with pytest.raises(SweepVerifyError, match=self._REFUSAL):
            recorded_audit_facts(path, snapshot="snap0001")
        assert recorded_audit_facts(path)[0] is not None  # unscoped = latest = B

    def test_non_context_action_params_stay_inert(self, tmp_path: Path) -> None:
        # A NON-context action's params are never consulted (junk stays inert).
        path = tmp_path / "a.jsonl"
        with AuditWriter.open(path) as audit:
            audit.append(action="OTHER", subject="s", payload={"params": {"fill_mode": "junk"}})
            self._append(audit, params={"fill_mode": "worst_case"})
        assert recorded_audit_facts(path)[0] is not None

    def test_corrupt_chain_refuses(self, tmp_path: Path) -> None:
        # The verdict-grade read is chain-verified FIRST (round-8 finding 12
        # of the facts seam), so trailing junk refuses at verification.
        path = tmp_path / "a.jsonl"
        with AuditWriter.open(path) as audit:
            self._append(audit, params={"fill_mode": "worst_case"})
        path.write_text(path.read_text(encoding="utf-8") + "not json\n", encoding="utf-8")
        with pytest.raises(SweepVerifyError, match="failed hash-chain verification"):
            recorded_audit_facts(path)

    def test_real_swept_tree_records_worst_case_and_verifies(self, tmp_path: Path) -> None:
        # End-to-end: the CURRENT writers record the unconditional token, so
        # a real swept tree derives facts refusal-free AND verifies clean.
        root = tmp_path / "results"
        root.mkdir()
        grid = _grid()
        _sweep(root, grid)
        context, _schedules, others = recorded_audit_facts(root / "audit.jsonl")
        assert context is not None
        assert others == ()
        report = verify_identity(root, grid)
        assert report.checked_stores == 1
        assert not report.corrupt, report.findings


# ── Recorded roll schedules (ADR-0012 / DEC-NIFTY-FUTURES-SWEEP-LAUNCH-001 F16) ──


class TestRecordedRollSchedules:
    """`recorded_roll_schedules_from_audit` — the tree-describes-itself channel."""

    _SCHEDULE: tuple[int, ...] = (739428, 739463)

    def _payload(self, **overrides: object) -> dict[str, object]:
        base: dict[str, object] = {
            "instrument": "NSE-NIFTY-FUT",
            "params": {"roll_schedule": list(self._SCHEDULE)},
        }
        base.update(overrides)
        return base

    def test_absent_audit_returns_empty(self, tmp_path: Path) -> None:
        assert recorded_roll_schedules_from_audit(tmp_path / "nope.jsonl") == {}

    def test_reads_the_recorded_schedule(self, tmp_path: Path) -> None:
        path = tmp_path / "a.jsonl"
        with AuditWriter.open(path) as audit:
            audit.append(action="SWEEP_START", subject="s", payload=self._payload())
        assert recorded_roll_schedules_from_audit(path) == {"NSE-NIFTY-FUT": tuple(self._SCHEDULE)}

    def test_spot_tree_without_schedules_returns_empty(self, tmp_path: Path) -> None:
        # Non-SWEEP_START entries + SWEEP_START without params/roll_schedule
        # are all skipped — every spot tree reads {} (never a fabricated map).
        path = tmp_path / "a.jsonl"
        with AuditWriter.open(path) as audit:
            audit.append(action="OTHER", subject="s", payload=self._payload())
            audit.append(action="SWEEP_START", subject="s", payload={"instrument": "NIFTY"})
            audit.append(
                action="SWEEP_START",
                subject="s",
                payload={"instrument": "NIFTY", "params": {"sl_paisa": 80}},
            )
        assert recorded_roll_schedules_from_audit(path) == {}

    def test_consistent_repeat_entries_agree(self, tmp_path: Path) -> None:
        path = tmp_path / "a.jsonl"
        with AuditWriter.open(path) as audit:
            audit.append(action="SWEEP_START", subject="s1", payload=self._payload())
            audit.append(action="SWEEP_START", subject="s2", payload=self._payload())
        assert recorded_roll_schedules_from_audit(path) == {"NSE-NIFTY-FUT": tuple(self._SCHEDULE)}

    def test_inconsistent_schedules_raise(self, tmp_path: Path) -> None:
        path = tmp_path / "a.jsonl"
        with AuditWriter.open(path) as audit:
            audit.append(action="SWEEP_START", subject="s", payload=self._payload())
            audit.append(
                action="SWEEP_START",
                subject="s",
                payload=self._payload(params={"roll_schedule": [739428]}),
            )
        with pytest.raises(SweepVerifyError, match="inconsistent recorded roll_schedule"):
            recorded_roll_schedules_from_audit(path)

    @pytest.mark.parametrize(
        "payload",
        [
            # Missing instrument — a schedule with no owner is unverifiable.
            {"params": {"roll_schedule": [739428]}},
            # Not a list.
            {"instrument": "NSE-NIFTY-FUT", "params": {"roll_schedule": "739428"}},
            # Non-int items (bool is an int subclass — explicitly rejected).
            {"instrument": "NSE-NIFTY-FUT", "params": {"roll_schedule": [True]}},
            # SHAPE law (round-5 finding 1 — the `_roll_schedules_from_payload`
            # manifest twin): non-empty, strictly increasing, non-negative.
            # Pre-fix a type-valid-but-shape-invalid hand-edit passed this
            # reader (the manifest boundary refused the same bytes).
            {"instrument": "NSE-NIFTY-FUT", "params": {"roll_schedule": []}},
            {"instrument": "NSE-NIFTY-FUT", "params": {"roll_schedule": [739463, 739428]}},
            {"instrument": "NSE-NIFTY-FUT", "params": {"roll_schedule": [739428, 739428]}},
            {"instrument": "NSE-NIFTY-FUT", "params": {"roll_schedule": [-1, 739428]}},
        ],
    )
    def test_malformed_schedule_raises(self, tmp_path: Path, payload: dict[str, object]) -> None:
        path = tmp_path / "a.jsonl"
        with AuditWriter.open(path) as audit:
            audit.append(action="SWEEP_START", subject="s", payload=payload)
        with pytest.raises(SweepVerifyError, match="malformed recorded roll_schedule"):
            recorded_roll_schedules_from_audit(path)

    def test_unreadable_audit_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "a.jsonl"
        path.write_text("{not jsonl\n", encoding="utf-8")
        with pytest.raises(SweepVerifyError, match="unreadable audit log"):
            recorded_roll_schedules_from_audit(path)

    def test_non_alias_instrument_with_schedule_refuses(self, tmp_path: Path) -> None:
        """Round-8 finding 11 (fails-before/passes-after): presence ⟺ alias.

        Pre-fix a well-formed hand-edited line stamping a roll_schedule on a
        SPOT instrument was ACCEPTED here; downstream `_verify_store_rows`
        then hit the SweepRunConfig invariant ValueError and demoted that
        store to a warn-grade `row_identity_underivable` EARLY-RETURN —
        skipping the row_run_id/label/top_k corruption checks, so ONE forged
        line laundered a corrupt spot store into a CLEAN Tier-2 verdict. The
        parse boundary now refuses exactly like its manifest twin
        (`_roll_schedules_from_payload`).
        """
        path = tmp_path / "a.jsonl"
        with AuditWriter.open(path) as audit:
            audit.append(
                action="SWEEP_START",
                subject="s",
                payload=self._payload(instrument="NSE-NIFTY"),
            )
        with pytest.raises(SweepVerifyError, match="not the NIFTY-futures series alias"):
            recorded_roll_schedules_from_audit(path)


# ── recorded_audit_facts (review round-4 finding 5) ──────────────────────────


class TestRecordedAuditFacts:
    """`recorded_audit_facts` — cmd_sweep_verify's ONE shared audit parse.

    Round-4 finding 5: a production futures audit holds one SWEEP_START line
    per grid tuple (thousands), and cmd_sweep_verify parsed the whole JSONL
    once for the context and AGAIN for the F16 schedules — the exact double
    parse the round-3 gate fix eliminated at the launch mouth.

    Round-5 finding 6: the facts are SCOPED to ONE generation. The sanctioned
    sweep-daemon root accretes a NEW context (and a GROWN futures schedule)
    every moved-snapshot cycle, so the pre-fix whole-file derivation raised
    'inconsistent recorded context' on the flagship production tree — locking
    every sweep-verify tier (incl. the Tier-3 recount oracle) out of it, with
    the explicit context flags unable to rescue (the raise preceded them).
    """

    _SCHEDULE: tuple[int, ...] = (739428, 739463)

    def _write(
        self,
        path: Path,
        *,
        snapshot: str = "snap0001",
        code_sha: str = "deadbeef",
        schedule: tuple[int, ...] | None = None,
    ) -> None:
        with AuditWriter.open(path, resume=path.exists()) as audit:
            audit.append(
                action="SWEEP_START",
                subject="s",
                payload={
                    "data_snapshot_hash": snapshot,
                    "indicator_version": "ind-v1",
                    "code_commit_sha": code_sha,
                    "instrument": "NSE-NIFTY-FUT",
                    "params": {
                        "fill_mode": "worst_case",
                        "roll_schedule": list(schedule if schedule is not None else self._SCHEDULE),
                    },
                },
            )

    def test_absent_audit_returns_none_and_empty(self, tmp_path: Path) -> None:
        assert recorded_audit_facts(tmp_path / "nope.jsonl") == (None, {}, ())

    def test_derives_both_facts_matching_the_single_fact_readers(self, tmp_path: Path) -> None:
        path = tmp_path / "a.jsonl"
        self._write(path)
        context, schedules, others = recorded_audit_facts(path)
        assert context == recorded_context_from_audit(path)
        assert schedules == recorded_roll_schedules_from_audit(path)
        assert context is not None
        assert context.data_snapshot_hash == "snap0001"
        assert schedules == {"NSE-NIFTY-FUT": self._SCHEDULE}
        assert others == ()  # a single-generation audit names no other generation

    def test_reads_the_audit_exactly_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The regression teeth: pre-fix cmd_sweep_verify's two single-fact
        # readers each ran their own full O(entries) read+parse.
        from brutex.orchestrator import audit as audit_mod

        path = tmp_path / "a.jsonl"
        self._write(path)
        real_read = audit_mod.read_entries
        calls: list[Path] = []

        def counting_read(p: Path) -> object:
            calls.append(p)
            return real_read(p)

        monkeypatch.setattr(audit_mod, "read_entries", counting_read)
        context, schedules, _others = recorded_audit_facts(path)
        assert context is not None
        assert schedules
        assert calls == [path]  # ONE parse for BOTH derivations

    def test_unreadable_audit_raises(self, tmp_path: Path) -> None:
        # Round-8 finding 12: the verdict-grade read is chain-verified FIRST,
        # so a malformed line surfaces as a chain-verification failure.
        path = tmp_path / "a.jsonl"
        path.write_text("{not jsonl\n", encoding="utf-8")
        with pytest.raises(SweepVerifyError, match="failed hash-chain verification"):
            recorded_audit_facts(path)

    def test_tampered_chain_refuses_verdict_grade_facts(self, tmp_path: Path) -> None:
        """Round-8 finding 12 (fails-before/passes-after): chain-verify FIRST.

        Pre-fix `recorded_audit_facts` parsed the audit WITHOUT verifying the
        hash chain while its docstrings pointed at a `verify_chain` call
        nobody made — one well-formed hand-edited payload steered the Tier-2
        context, the F16 schedules, and the Tier-3 context_match gate.
        """
        path = tmp_path / "a.jsonl"
        self._write(path)
        # Tamper ONE recorded fact, keeping well-formed JSONL: the entry_hash
        # no longer matches, so the verified read must refuse.
        text = path.read_text(encoding="utf-8")
        assert "snap0001" in text
        path.write_text(text.replace("snap0001", "snap0002"), encoding="utf-8")
        with pytest.raises(SweepVerifyError, match="failed hash-chain verification"):
            recorded_audit_facts(path)

    # ── round-5 finding 6: ONE-generation scoping of the daemon-root audit ──

    def _daemon_audit(self, path: Path) -> None:
        """Two moved-snapshot daemon cycles APPENDED to one audit — cycle 2's
        futures schedule legitimately GREW with the new front-month contract."""
        self._write(path, snapshot="snap0001", code_sha="deadbeef", schedule=(739428,))
        self._write(path, snapshot="snap0002", code_sha="cafef00d", schedule=self._SCHEDULE)

    def test_daemon_root_scopes_to_the_latest_generation(self, tmp_path: Path) -> None:
        # Pre-fix teeth: the whole-file derivation raised 'inconsistent
        # recorded context' (and 'inconsistent recorded roll_schedule') on
        # this exact sanctioned shape.
        path = tmp_path / "a.jsonl"
        self._daemon_audit(path)
        context, schedules, others = recorded_audit_facts(path)
        assert context == RecordedContext(
            data_snapshot_hash="snap0002",
            indicator_version="ind-v1",
            code_commit_sha="cafef00d",
        )
        assert schedules == {"NSE-NIFTY-FUT": self._SCHEDULE}
        assert others == ("snap0001",)  # the other generation NAMED, not condemned

    def test_explicit_snapshot_scopes_that_generation(self, tmp_path: Path) -> None:
        # --data-snapshot-hash picks the OLDER generation: its own context +
        # its own (shorter) schedule come back, the newer one is the 'other'.
        path = tmp_path / "a.jsonl"
        self._daemon_audit(path)
        context, schedules, others = recorded_audit_facts(path, snapshot="snap0001")
        assert context == RecordedContext(
            data_snapshot_hash="snap0001",
            indicator_version="ind-v1",
            code_commit_sha="deadbeef",
        )
        assert schedules == {"NSE-NIFTY-FUT": (739428,)}
        assert others == ("snap0002",)

    def test_explicit_unrecorded_snapshot_returns_no_facts(self, tmp_path: Path) -> None:
        # The explicit-flags path: an unrecorded generation derives NOTHING
        # from the audit (never a mixed-generation context) — the caller's
        # flags must supply the full context.
        path = tmp_path / "a.jsonl"
        self._daemon_audit(path)
        context, schedules, others = recorded_audit_facts(path, snapshot="snap9999")
        assert context is None
        assert schedules == {}
        assert others == ("snap0001", "snap0002")  # first-appearance order

    # ── round-7 finding 7: a generation is the FULL context TRIPLE ──

    def test_same_snapshot_code_upgrade_scopes_to_the_last_triple(self, tmp_path: Path) -> None:
        # One SNAPSHOT does not imply one LAUNCH: a code-upgrade re-launch
        # into the same root (any git commit lands, the lake does not move —
        # the launch gate ACCEPTS it silently) records a SECOND
        # code_commit_sha under the SAME snapshot. Pre-fix the snapshot-keyed
        # scoping raised 'inconsistent recorded context' — every tier (incl.
        # the Tier-3 recount oracle) permanently locked out, with the
        # explicit flags unable to rescue (the raise preceded them). Now the
        # two launches are two GENERATIONS; facts scope to the newest triple.
        path = tmp_path / "a.jsonl"
        self._write(path, snapshot="snap0001", code_sha="deadbeef")
        self._write(path, snapshot="snap0001", code_sha="cafef00d")
        context, schedules, others = recorded_audit_facts(path)
        assert context == RecordedContext(
            data_snapshot_hash="snap0001",
            indicator_version="ind-v1",
            code_commit_sha="cafef00d",
        )
        assert schedules == {"NSE-NIFTY-FUT": self._SCHEDULE}
        # The other generation is still NAMED (its snapshot equals the scoped
        # one — the honest note counts triples, not snapshot strings).
        assert others == ("snap0001",)

    def test_explicit_code_sha_narrows_within_a_shared_snapshot(self, tmp_path: Path) -> None:
        # The flags NARROW within the candidates: --code-commit-sha picks the
        # OLDER same-snapshot generation (the rescue path finding 7 proved
        # unreachable pre-fix).
        path = tmp_path / "a.jsonl"
        self._write(path, snapshot="snap0001", code_sha="deadbeef", schedule=(739428,))
        self._write(path, snapshot="snap0001", code_sha="cafef00d")
        context, schedules, _others = recorded_audit_facts(
            path, snapshot="snap0001", code_commit_sha="deadbeef"
        )
        assert context == RecordedContext(
            data_snapshot_hash="snap0001",
            indicator_version="ind-v1",
            code_commit_sha="deadbeef",
        )
        assert schedules == {"NSE-NIFTY-FUT": (739428,)}

    def test_explicit_indicator_version_narrows_candidates(self, tmp_path: Path) -> None:
        # The third axis of the triple: an unmatched --indicator-version
        # derives NOTHING (the explicit-flags path), a matched one scopes.
        path = tmp_path / "a.jsonl"
        self._write(path, snapshot="snap0001", code_sha="deadbeef")
        context, schedules, others = recorded_audit_facts(path, indicator_version="ind-v9")
        assert context is None
        assert schedules == {}
        assert others == ("snap0001",)
        context, _schedules, _others = recorded_audit_facts(path, indicator_version="ind-v1")
        assert context is not None
        assert context.indicator_version == "ind-v1"

    def test_within_generation_schedule_inconsistency_still_raises(self, tmp_path: Path) -> None:
        # The raise the triple keying KEEPS: two entries of ONE full context
        # triple disagreeing on an instrument's schedule is real corruption
        # (the launch resume gate refuses a schedule-only drift up front, so
        # no sanctioned cadence produces this shape).
        path = tmp_path / "a.jsonl"
        self._write(path, snapshot="snap0001", code_sha="deadbeef", schedule=(739428,))
        self._write(path, snapshot="snap0001", code_sha="deadbeef", schedule=self._SCHEDULE)
        with pytest.raises(SweepVerifyError, match="inconsistent recorded roll_schedule"):
            recorded_audit_facts(path)

    def test_sweep_start_less_audit_returns_no_facts(self, tmp_path: Path) -> None:
        # Readable but SWEEP_START-less (a copytruncate rotation): no
        # generation exists to scope to — honest empties, never a raise.
        path = tmp_path / "a.jsonl"
        with AuditWriter.open(path) as audit:
            audit.append(action="OTHER", subject="s", payload={})
        assert recorded_audit_facts(path) == (None, {}, ())

    def test_sweep_launch_line_supplies_context_and_schedule(self, tmp_path: Path) -> None:
        # Round-7 finding 1: the parent's pre-dispatch SWEEP_LAUNCH line is a
        # context-bearing entry for EVERY generation-scoped reader — a
        # crashed parallel launch's audit (launch line only, every buffered
        # per-tuple SWEEP_START lost) still describes its generation.
        path = tmp_path / "a.jsonl"
        with AuditWriter.open(path) as audit:
            audit.append(
                action="SWEEP_LAUNCH",
                subject="sweep-all:launch:NSE-NIFTY-FUT",
                payload={
                    "instrument": "NSE-NIFTY-FUT",
                    "data_snapshot_hash": "snap0001",
                    "indicator_version": "ind-v1",
                    "code_commit_sha": "deadbeef",
                    "params": {"fill_mode": "worst_case", "roll_schedule": list(self._SCHEDULE)},
                },
            )
        context, schedules, others = recorded_audit_facts(path)
        assert context == RecordedContext(
            data_snapshot_hash="snap0001",
            indicator_version="ind-v1",
            code_commit_sha="deadbeef",
        )
        assert schedules == {"NSE-NIFTY-FUT": self._SCHEDULE}
        assert others == ()


# ── The resume-honesty gate (ADR-0012, review round-2 finding 11) ────────────


class TestStaleRollScheduleRefusal:
    """`stale_roll_schedule_refusal` — schedule-only drift against a resumed tree.

    The crack it closes: `roll_schedule` enters the run identity but NOT the
    tuple-store/sentinel digest, and the schedule can grow while the snapshot
    stays byte-identical (a sessionless-span successor contract). A re-launch
    would then resume S1-minted rows at the sentinels while Stage-3/cascade
    recovery re-derives under S2 → a misattributed grid-mismatch crash. The
    gate compares recorded-vs-fresh UP FRONT, refusing only on a PROVEN
    snapshot-identical mismatch.
    """

    _S1: tuple[int, ...] = (739428,)
    _S2: tuple[int, ...] = (739428, 739463)

    def _write_audit(
        self,
        path: Path,
        *,
        snapshot: str = "snap0001",
        schedule: tuple[int, ...] | None = None,
    ) -> None:
        """Record one launch's SWEEP_START — APPENDS to an existing audit.

        `resume=path.exists()` mirrors `cmd_sweep_all`'s `AuditWriter.open(...,
        resume=audit_path.exists())` exactly, so calling this twice reproduces
        the sanctioned multi-launch same-root audit shape (the monthly
        grown-lake cadence — review round-4 findings 1/8/11).
        """
        payload: dict[str, object] = {
            "data_snapshot_hash": snapshot,
            "indicator_version": "ind-v1",
            "code_commit_sha": "deadbeef",
            "instrument": "NSE-NIFTY-FUT",
        }
        if schedule is not None:
            payload["params"] = {"roll_schedule": list(schedule)}
        with AuditWriter.open(path, resume=path.exists()) as audit:
            audit.append(action="SWEEP_START", subject="s", payload=payload)

    def test_absent_audit_abstains(self, tmp_path: Path) -> None:
        # A fresh results root has nothing to drift from.
        assert (
            stale_roll_schedule_refusal(
                tmp_path / "nope.jsonl",
                fresh_snapshot="snap0001",
                fresh_indicator_version="ind-v1",
                fresh_code_commit_sha="deadbeef",
                roll_schedule_by_instrument={"NSE-NIFTY-FUT": self._S2},
            )
            is None
        )

    def test_snapshot_identical_schedule_drift_refuses_with_evidence(self, tmp_path: Path) -> None:
        path = tmp_path / "a.jsonl"
        self._write_audit(path, schedule=self._S1)
        message = stale_roll_schedule_refusal(
            path,
            fresh_snapshot="snap0001",
            fresh_indicator_version="ind-v1",
            fresh_code_commit_sha="deadbeef",
            roll_schedule_by_instrument={"NSE-NIFTY-FUT": self._S2},
        )
        assert message is not None
        assert str(list(self._S1)) in message  # the RECORDED schedule named
        assert str(list(self._S2)) in message  # the FRESH schedule named
        assert "UNCHANGED" in message
        assert "contract set changed" in message
        assert "--results-root" in message  # the remediation

    def test_matching_schedule_passes(self, tmp_path: Path) -> None:
        # The normal crash-resume: same lake, same schedule — never refused.
        path = tmp_path / "a.jsonl"
        self._write_audit(path, schedule=self._S2)
        assert (
            stale_roll_schedule_refusal(
                path,
                fresh_snapshot="snap0001",
                fresh_indicator_version="ind-v1",
                fresh_code_commit_sha="deadbeef",
                roll_schedule_by_instrument={"NSE-NIFTY-FUT": self._S2},
            )
            is None
        )

    def test_moved_snapshot_abstains(self, tmp_path: Path) -> None:
        # A CHANGED snapshot re-keys every tuple digest (stores/sentinels
        # re-address), so the legit monthly grown-lake re-launch never refuses.
        path = tmp_path / "a.jsonl"
        self._write_audit(path, schedule=self._S1)
        assert (
            stale_roll_schedule_refusal(
                path,
                fresh_snapshot="snap0002",
                fresh_indicator_version="ind-v1",
                fresh_code_commit_sha="deadbeef",
                roll_schedule_by_instrument={"NSE-NIFTY-FUT": self._S2},
            )
            is None
        )

    def test_tree_without_recorded_schedule_abstains(self, tmp_path: Path) -> None:
        # A rotated audit / spot-era tree records no schedule: nothing to
        # compare — the sentinel pairing is sweep-verify's diagnosis, not a
        # launch blocker.
        path = tmp_path / "a.jsonl"
        self._write_audit(path, schedule=None)
        assert (
            stale_roll_schedule_refusal(
                path,
                fresh_snapshot="snap0001",
                fresh_indicator_version="ind-v1",
                fresh_code_commit_sha="deadbeef",
                roll_schedule_by_instrument={"NSE-NIFTY-FUT": self._S2},
            )
            is None
        )

    def test_tree_without_sweep_start_abstains(self, tmp_path: Path) -> None:
        path = tmp_path / "a.jsonl"
        with AuditWriter.open(path) as audit:
            audit.append(action="OTHER", subject="s", payload={})
        assert (
            stale_roll_schedule_refusal(
                path,
                fresh_snapshot="snap0001",
                fresh_indicator_version="ind-v1",
                fresh_code_commit_sha="deadbeef",
                roll_schedule_by_instrument={"NSE-NIFTY-FUT": self._S2},
            )
            is None
        )

    def test_unreadable_audit_abstains_never_blocks(self, tmp_path: Path) -> None:
        # A best-effort gate: an inconsistent/corrupt audit is sweep-verify's
        # territory — the launch is never blocked on a diagnostic failure.
        path = tmp_path / "a.jsonl"
        path.write_text("{not jsonl\n", encoding="utf-8")
        assert (
            stale_roll_schedule_refusal(
                path,
                fresh_snapshot="snap0001",
                fresh_indicator_version="ind-v1",
                fresh_code_commit_sha="deadbeef",
                roll_schedule_by_instrument={"NSE-NIFTY-FUT": self._S2},
            )
            is None
        )

    # ── round-3 finding 8: sentinels-without-a-readable-schedule refusal ──

    def _plant_sentinel(self, root: Path) -> Path:
        """A futures tuple completion sentinel at the orchestrator's layout."""
        tf_dir = root / "NSE-NIFTY-FUT" / "1minute"
        tf_dir.mkdir(parents=True)
        sentinel = tf_dir / "bull_k3_0123456789abcdef.parquet.complete.json"
        sentinel.write_text("{}", encoding="utf-8")
        return sentinel

    def test_absent_audit_with_futures_sentinels_refuses(self, tmp_path: Path) -> None:
        # The gate's own drift class recurs with the audit rotated/lost: the
        # sentinels short-circuit rows minted under an UNVERIFIABLE schedule,
        # then Stage-3 recovery crashes blaming the grid. Refuse UP FRONT.
        root = tmp_path / "results"
        sentinel = self._plant_sentinel(root)
        message = stale_roll_schedule_refusal(
            root / "audit" / "sweep-all.jsonl",
            fresh_snapshot="snap0001",
            fresh_indicator_version="ind-v1",
            fresh_code_commit_sha="deadbeef",
            roll_schedule_by_instrument={"NSE-NIFTY-FUT": self._S2},
            results_root=root,
        )
        assert message is not None
        assert str(sentinel) in message  # the evidence named
        assert "ABSENT" in message
        assert "--results-root" in message  # the remediation

    def test_unreadable_audit_with_futures_sentinels_refuses(self, tmp_path: Path) -> None:
        root = tmp_path / "results"
        self._plant_sentinel(root)
        audit = root / "audit" / "sweep-all.jsonl"
        audit.parent.mkdir(parents=True)
        audit.write_text("{not jsonl\n", encoding="utf-8")
        message = stale_roll_schedule_refusal(
            audit,
            fresh_snapshot="snap0001",
            fresh_indicator_version="ind-v1",
            fresh_code_commit_sha="deadbeef",
            roll_schedule_by_instrument={"NSE-NIFTY-FUT": self._S2},
            results_root=root,
        )
        assert message is not None
        assert "UNREADABLE" in message

    def test_absent_audit_with_root_but_no_sentinels_abstains(self, tmp_path: Path) -> None:
        # A fresh tree (instrument dir exists, nothing completed) — the
        # normal first futures launch into a pre-created root is never blocked.
        root = tmp_path / "results"
        (root / "NSE-NIFTY-FUT" / "1minute").mkdir(parents=True)
        assert (
            stale_roll_schedule_refusal(
                root / "audit" / "sweep-all.jsonl",
                fresh_snapshot="snap0001",
                fresh_indicator_version="ind-v1",
                fresh_code_commit_sha="deadbeef",
                roll_schedule_by_instrument={"NSE-NIFTY-FUT": self._S2},
                results_root=root,
            )
            is None
        )

    def test_absent_audit_without_instrument_dir_abstains(self, tmp_path: Path) -> None:
        # No futures subtree at all (a brand-new root): nothing to protect.
        root = tmp_path / "results"
        root.mkdir()
        assert (
            stale_roll_schedule_refusal(
                root / "audit" / "sweep-all.jsonl",
                fresh_snapshot="snap0001",
                fresh_indicator_version="ind-v1",
                fresh_code_commit_sha="deadbeef",
                roll_schedule_by_instrument={"NSE-NIFTY-FUT": self._S2},
                results_root=root,
            )
            is None
        )

    # ── round-4 findings 1/8/11: launch-generation scoping of multi-launch audits ──

    _S3: tuple[int, ...] = (739428, 739463, 739491)

    def _two_launch_audit(self, root: Path) -> Path:
        """The sanctioned same-root shape: launch 1 (snap0001/S1), then the
        moved-snapshot monthly re-launch (snap0002/S2) APPENDED to the SAME
        `resume=True` audit — exactly what cmd_sweep_all produces."""
        audit = root / "audit" / "sweep-all.jsonl"
        audit.parent.mkdir(parents=True, exist_ok=True)
        self._write_audit(audit, snapshot="snap0001", schedule=self._S1)
        self._write_audit(audit, snapshot="snap0002", schedule=self._S2)
        return audit

    def test_multi_launch_moved_snapshot_next_launch_abstains(self, tmp_path: Path) -> None:
        # Findings 1/8: cycle 3 of the monthly cadence (a THIRD snapshot over a
        # two-generation audit) must ABSTAIN — every recorded sentinel's tuple
        # digest folded a foreign snapshot, so none can pair with this launch.
        # Pre-fix the whole-file context derivation raised 'inconsistent
        # recorded context' and misattributed 'UNREADABLE/inconsistent'.
        root = tmp_path / "results"
        self._plant_sentinel(root)
        audit = self._two_launch_audit(root)
        assert (
            stale_roll_schedule_refusal(
                audit,
                fresh_snapshot="snap0003",
                fresh_indicator_version="ind-v1",
                fresh_code_commit_sha="deadbeef",
                roll_schedule_by_instrument={"NSE-NIFTY-FUT": self._S3},
                results_root=root,
            )
            is None
        )

    def test_multi_launch_crash_resume_of_matching_launch_passes(self, tmp_path: Path) -> None:
        # Finding 11: a crash-resume of launch 2 — IDENTICAL snapshot and
        # IDENTICAL schedule as its own recorded generation — is never
        # refused; the sentinel/checkpoint crash-safe-resume guarantee stands.
        root = tmp_path / "results"
        self._plant_sentinel(root)
        audit = self._two_launch_audit(root)
        assert (
            stale_roll_schedule_refusal(
                audit,
                fresh_snapshot="snap0002",
                fresh_indicator_version="ind-v1",
                fresh_code_commit_sha="deadbeef",
                roll_schedule_by_instrument={"NSE-NIFTY-FUT": self._S2},
                results_root=root,
            )
            is None
        )

    def test_multi_launch_schedule_drift_within_matching_launch_refuses(
        self, tmp_path: Path
    ) -> None:
        # The gate's core law survives the scoping: schedule-only drift against
        # the MATCHING generation still refuses with BOTH schedules named —
        # the foreign launch-1 generation is ignored, never condemned.
        path = tmp_path / "a.jsonl"
        self._write_audit(path, snapshot="snap0001", schedule=self._S1)
        self._write_audit(path, snapshot="snap0002", schedule=self._S1)
        message = stale_roll_schedule_refusal(
            path,
            fresh_snapshot="snap0002",
            fresh_indicator_version="ind-v1",
            fresh_code_commit_sha="deadbeef",
            roll_schedule_by_instrument={"NSE-NIFTY-FUT": self._S2},
        )
        assert message is not None
        assert str(list(self._S1)) in message  # the RECORDED (matching-launch) schedule
        assert str(list(self._S2)) in message  # the FRESH schedule

    # ── round-4 finding 10: readable but SWEEP_START-less audits ──

    def test_empty_audit_with_futures_sentinels_refuses(self, tmp_path: Path) -> None:
        # A truncated-to-empty (copytruncate-rotated) audit leaves the schedule
        # the sentinels' rows were minted under exactly as unverifiable as an
        # ABSENT audit — refuse, never abstain into the sentinel short-circuit.
        root = tmp_path / "results"
        sentinel = self._plant_sentinel(root)
        audit = root / "audit" / "sweep-all.jsonl"
        audit.parent.mkdir(parents=True)
        audit.write_text("", encoding="utf-8")
        message = stale_roll_schedule_refusal(
            audit,
            fresh_snapshot="snap0001",
            fresh_indicator_version="ind-v1",
            fresh_code_commit_sha="deadbeef",
            roll_schedule_by_instrument={"NSE-NIFTY-FUT": self._S2},
            results_root=root,
        )
        assert message is not None
        assert "no SWEEP_START context" in message
        assert str(sentinel) in message  # the evidence named

    def test_sweep_start_less_audit_with_sentinels_refuses(self, tmp_path: Path) -> None:
        # The chain-valid variant: real committed entries, zero SWEEP_START.
        root = tmp_path / "results"
        self._plant_sentinel(root)
        audit = root / "audit" / "sweep-all.jsonl"
        audit.parent.mkdir(parents=True)
        with AuditWriter.open(audit) as writer:
            writer.append(action="OTHER", subject="s", payload={})
        message = stale_roll_schedule_refusal(
            audit,
            fresh_snapshot="snap0001",
            fresh_indicator_version="ind-v1",
            fresh_code_commit_sha="deadbeef",
            roll_schedule_by_instrument={"NSE-NIFTY-FUT": self._S2},
            results_root=root,
        )
        assert message is not None
        assert "no SWEEP_START context" in message

    def test_empty_audit_without_sentinels_abstains(self, tmp_path: Path) -> None:
        # No sentinels → nothing a stale schedule could pair with — abstain.
        root = tmp_path / "results"
        (root / "NSE-NIFTY-FUT" / "1minute").mkdir(parents=True)
        audit = root / "audit" / "sweep-all.jsonl"
        audit.parent.mkdir(parents=True)
        audit.write_text("", encoding="utf-8")
        assert (
            stale_roll_schedule_refusal(
                audit,
                fresh_snapshot="snap0001",
                fresh_indicator_version="ind-v1",
                fresh_code_commit_sha="deadbeef",
                roll_schedule_by_instrument={"NSE-NIFTY-FUT": self._S2},
                results_root=root,
            )
            is None
        )

    def test_sweep_start_missing_context_key_with_sentinels_refuses(self, tmp_path: Path) -> None:
        # A malformed SWEEP_START (context key missing) makes the recorded
        # snapshot underivable — with sentinels present, fail closed.
        root = tmp_path / "results"
        self._plant_sentinel(root)
        audit = root / "audit" / "sweep-all.jsonl"
        audit.parent.mkdir(parents=True)
        with AuditWriter.open(audit) as writer:
            writer.append(
                action="SWEEP_START",
                subject="s",
                payload={"data_snapshot_hash": "snap0001"},  # indicator/sha MISSING
            )
        message = stale_roll_schedule_refusal(
            audit,
            fresh_snapshot="snap0001",
            fresh_indicator_version="ind-v1",
            fresh_code_commit_sha="deadbeef",
            roll_schedule_by_instrument={"NSE-NIFTY-FUT": self._S2},
            results_root=root,
        )
        assert message is not None
        assert "UNREADABLE/inconsistent" in message

    # ── round-5 finding 11: the digest-aware fresh-sentinel probe ──

    def test_lossy_audit_with_fresh_digest_sentinel_refuses(self, tmp_path: Path) -> None:
        # The mid-file-lossy shape: a matching-generation SPOT SWEEP_START
        # survived (schedule-less), the futures SWEEP_STARTs were lost, and
        # the tree holds a sentinel at one of THIS launch's expected paths —
        # its rows' minted schedule is exactly as unverifiable as with the
        # audit absent. Pre-fix the per-instrument loop silently fell through
        # to the abstain (proven by execution in the round-5 review).
        root = tmp_path / "results"
        sentinel = self._plant_sentinel(root)
        audit = root / "audit" / "sweep-all.jsonl"
        audit.parent.mkdir(parents=True)
        with AuditWriter.open(audit) as writer:
            writer.append(
                action="SWEEP_START",
                subject="s",
                payload={
                    "data_snapshot_hash": "snap0001",
                    "indicator_version": "ind-v1",
                    "code_commit_sha": "deadbeef",
                    "instrument": "NIFTY",  # the surviving SPOT start — no schedule
                },
            )
        message = stale_roll_schedule_refusal(
            audit,
            fresh_snapshot="snap0001",
            fresh_indicator_version="ind-v1",
            fresh_code_commit_sha="deadbeef",
            roll_schedule_by_instrument={"NSE-NIFTY-FUT": self._S2},
            results_root=root,
            fresh_sentinel_paths={"NSE-NIFTY-FUT": [sentinel]},
        )
        assert message is not None
        assert str(sentinel) in message  # the evidence named
        assert "record NO roll_schedule" in message
        assert "mid-file-lossy" in message
        assert "--results-root" in message  # the remediation

    @pytest.mark.parametrize("mapping", [{}, {"NSE-NIFTY-FUT": []}])
    def test_lossy_audit_without_a_fresh_digest_sentinel_abstains(
        self, tmp_path: Path, mapping: dict[str, list[Path]]
    ) -> None:
        # The zero-false-positive law: FOREIGN-generation sentinels (their
        # digests folded another snapshot, so none sits at a fresh expected
        # path) must NOT refuse — the legitimate crash-resume of a launch that
        # completed only spot tuples over an old futures tree proceeds.
        root = tmp_path / "results"
        self._plant_sentinel(root)  # a foreign-digest sentinel IS on disk
        audit = root / "audit" / "sweep-all.jsonl"
        audit.parent.mkdir(parents=True)
        self._write_audit(audit, snapshot="snap0001", schedule=None)
        assert (
            stale_roll_schedule_refusal(
                audit,
                fresh_snapshot="snap0001",
                fresh_indicator_version="ind-v1",
                fresh_code_commit_sha="deadbeef",
                roll_schedule_by_instrument={"NSE-NIFTY-FUT": self._S2},
                results_root=root,
                fresh_sentinel_paths=mapping,
            )
            is None
        )

    def test_lossy_audit_fresh_path_not_on_disk_abstains(self, tmp_path: Path) -> None:
        # An expected fresh path that does NOT exist is no evidence — the
        # probe checks existence, never the mapping alone.
        root = tmp_path / "results"
        self._plant_sentinel(root)
        audit = root / "audit" / "sweep-all.jsonl"
        audit.parent.mkdir(parents=True)
        self._write_audit(audit, snapshot="snap0001", schedule=None)
        missing = (
            root / "NSE-NIFTY-FUT" / "1minute" / "bull_k3_feedfacefeedface.parquet.complete.json"
        )
        assert (
            stale_roll_schedule_refusal(
                audit,
                fresh_snapshot="snap0001",
                fresh_indicator_version="ind-v1",
                fresh_code_commit_sha="deadbeef",
                roll_schedule_by_instrument={"NSE-NIFTY-FUT": self._S2},
                results_root=root,
                fresh_sentinel_paths={"NSE-NIFTY-FUT": [missing]},
            )
            is None
        )

    # ── round-6 finding 4: the ZERO-matching-entries arm probes too ──

    def test_generation_lost_audit_with_fresh_digest_sentinel_refuses(self, tmp_path: Path) -> None:
        # The round-5 finding-11 sibling: the audit lost its ENTIRE fresh-
        # generation tail (line-boundary truncation, or a pre-launch backup
        # restore — the gate's own remediation text invites one), leaving only
        # an OLDER generation's SWEEP_STARTs. `matching` is empty, so pre-fix
        # the gate returned None BEFORE the digest-aware probe — yet the tree
        # holds a sentinel at one of THIS launch's exact expected paths, whose
        # SWEEP_START was provably appended before the tuple ran: its total
        # absence is audit loss, and the resume would pair its rows' LOST
        # recorded schedule with the freshly resolved one.
        root = tmp_path / "results"
        sentinel = self._plant_sentinel(root)
        audit = root / "audit" / "sweep-all.jsonl"
        audit.parent.mkdir(parents=True)
        self._write_audit(audit, snapshot="snap0000", schedule=self._S1)  # older gen only
        message = stale_roll_schedule_refusal(
            audit,
            fresh_snapshot="snap0001",
            fresh_indicator_version="ind-v1",
            fresh_code_commit_sha="deadbeef",
            roll_schedule_by_instrument={"NSE-NIFTY-FUT": self._S2},
            results_root=root,
            fresh_sentinel_paths={"NSE-NIFTY-FUT": [sentinel]},
        )
        assert message is not None
        assert str(sentinel) in message  # the evidence named
        assert "NO SWEEP_START under this launch's context generation" in message
        assert "tail-lossy" in message
        assert "--results-root" in message  # the remediation

    @pytest.mark.parametrize("mapping", [{}, {"NSE-NIFTY-FUT": []}])
    def test_moved_snapshot_with_no_fresh_digest_sentinel_still_abstains(
        self, tmp_path: Path, mapping: dict[str, list[Path]]
    ) -> None:
        # The zero-false-positive law on the new arm: a LEGITIMATE moved
        # snapshot (the monthly grown-lake re-launch) holds only foreign-
        # generation sentinels — none at a fresh expected path — and must
        # keep resuming.
        root = tmp_path / "results"
        self._plant_sentinel(root)  # a foreign-digest sentinel IS on disk
        audit = root / "audit" / "sweep-all.jsonl"
        audit.parent.mkdir(parents=True)
        self._write_audit(audit, snapshot="snap0000", schedule=self._S1)
        assert (
            stale_roll_schedule_refusal(
                audit,
                fresh_snapshot="snap0001",
                fresh_indicator_version="ind-v1",
                fresh_code_commit_sha="deadbeef",
                roll_schedule_by_instrument={"NSE-NIFTY-FUT": self._S2},
                results_root=root,
                fresh_sentinel_paths=mapping,
            )
            is None
        )

    def test_moved_snapshot_fresh_path_not_on_disk_abstains(self, tmp_path: Path) -> None:
        # An expected fresh path that does NOT exist is no evidence on the
        # zero-matching-entries arm either — existence is probed, never the
        # mapping alone.
        root = tmp_path / "results"
        self._plant_sentinel(root)
        audit = root / "audit" / "sweep-all.jsonl"
        audit.parent.mkdir(parents=True)
        self._write_audit(audit, snapshot="snap0000", schedule=self._S1)
        missing = (
            root / "NSE-NIFTY-FUT" / "1minute" / "bull_k3_feedfacefeedface.parquet.complete.json"
        )
        assert (
            stale_roll_schedule_refusal(
                audit,
                fresh_snapshot="snap0001",
                fresh_indicator_version="ind-v1",
                fresh_code_commit_sha="deadbeef",
                roll_schedule_by_instrument={"NSE-NIFTY-FUT": self._S2},
                results_root=root,
                fresh_sentinel_paths={"NSE-NIFTY-FUT": [missing]},
            )
            is None
        )

    # ── round-7 finding 1: the pre-dispatch SWEEP_LAUNCH line on the parallel driver ──

    def _write_launch_line(
        self, path: Path, *, snapshot: str = "snap0001", schedule: tuple[int, ...] | None = None
    ) -> None:
        """The parent's fsynced pre-dispatch context line — exactly what
        ``cmd_sweep_all`` appends per futures instrument BEFORE grid dispatch."""
        with AuditWriter.open(path, resume=path.exists()) as audit:
            audit.append(
                action="SWEEP_LAUNCH",
                subject="sweep-all:launch:NSE-NIFTY-FUT",
                payload={
                    "instrument": "NSE-NIFTY-FUT",
                    "data_snapshot_hash": snapshot,
                    "indicator_version": "ind-v1",
                    "code_commit_sha": "deadbeef",
                    "params": {
                        "roll_schedule": list(schedule if schedule is not None else self._S2)
                    },
                },
            )

    def test_crashed_parallel_launch_with_launch_line_resumes(self, tmp_path: Path) -> None:
        # Round-7 finding 1 (MAJOR): on the PARALLEL driver the EVAL worker
        # durably writes the store + completion sentinel while the tuple's
        # SWEEP_START reaches the audit only via the parent's in-order apply
        # cursor — a parent crash in the launch window left fresh-digest
        # sentinels with ZERO fresh-generation entries, and the round-6 arm
        # condemned the sanctioned crash-resume as a "tail-lossy/restored
        # audit" (a PERMANENT wrong-diagnosis refusal: the gate runs before
        # the audit can ever gain the fresh SWEEP_START). The pre-dispatch
        # SWEEP_LAUNCH line now records the generation's schedule BEFORE any
        # tuple can complete: recorded == fresh → the resume proceeds.
        root = tmp_path / "results"
        sentinel = self._plant_sentinel(root)  # the worker-durable completed tuple
        audit = root / "audit" / "sweep-all.jsonl"
        audit.parent.mkdir(parents=True)
        self._write_launch_line(audit, schedule=self._S2)  # the ONLY surviving line
        assert (
            stale_roll_schedule_refusal(
                audit,
                fresh_snapshot="snap0001",
                fresh_indicator_version="ind-v1",
                fresh_code_commit_sha="deadbeef",
                roll_schedule_by_instrument={"NSE-NIFTY-FUT": self._S2},
                results_root=root,
                fresh_sentinel_paths={"NSE-NIFTY-FUT": [sentinel]},
            )
            is None
        )

    def test_crashed_parallel_launch_schedule_drift_still_refuses(self, tmp_path: Path) -> None:
        # The launch line is a RECORDED schedule, not a gate bypass: a lake
        # whose contract set drifted since the crash (snapshot unchanged)
        # still refuses with BOTH schedules named.
        root = tmp_path / "results"
        sentinel = self._plant_sentinel(root)
        audit = root / "audit" / "sweep-all.jsonl"
        audit.parent.mkdir(parents=True)
        self._write_launch_line(audit, schedule=self._S1)
        message = stale_roll_schedule_refusal(
            audit,
            fresh_snapshot="snap0001",
            fresh_indicator_version="ind-v1",
            fresh_code_commit_sha="deadbeef",
            roll_schedule_by_instrument={"NSE-NIFTY-FUT": self._S2},
            results_root=root,
            fresh_sentinel_paths={"NSE-NIFTY-FUT": [sentinel]},
        )
        assert message is not None
        assert str(list(self._S1)) in message  # the RECORDED (launch-line) schedule
        assert str(list(self._S2)) in message  # the FRESH schedule
        assert "contract set changed" in message

    def test_recorded_schedule_present_never_probes_sentinels(self, tmp_path: Path) -> None:
        # A matching-generation RECORDED schedule owns the verdict: identical
        # → pass (no probe fires even with a fresh sentinel mapped).
        root = tmp_path / "results"
        sentinel = self._plant_sentinel(root)
        audit = root / "audit" / "sweep-all.jsonl"
        audit.parent.mkdir(parents=True)
        self._write_audit(audit, snapshot="snap0001", schedule=self._S2)
        assert (
            stale_roll_schedule_refusal(
                audit,
                fresh_snapshot="snap0001",
                fresh_indicator_version="ind-v1",
                fresh_code_commit_sha="deadbeef",
                roll_schedule_by_instrument={"NSE-NIFTY-FUT": self._S2},
                results_root=root,
                fresh_sentinel_paths={"NSE-NIFTY-FUT": [sentinel]},
            )
            is None
        )

    # ── round-3 finding 4: ONE shared audit parse for both derivations ──

    def test_gate_reads_the_audit_exactly_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Pre-fix the gate invoked recorded_context_from_audit AND
        # recorded_roll_schedules_from_audit, each re-reading + re-chain-
        # verifying the WHOLE audit log — a doubled O(entries) launch cost.
        from brutex.orchestrator import audit as audit_mod

        path = tmp_path / "a.jsonl"
        self._write_audit(path, schedule=self._S1)
        real_read = audit_mod.read_entries
        calls: list[Path] = []

        def counting_read(p: Path) -> object:
            calls.append(p)
            return real_read(p)

        monkeypatch.setattr(audit_mod, "read_entries", counting_read)
        message = stale_roll_schedule_refusal(
            path,
            fresh_snapshot="snap0001",
            fresh_indicator_version="ind-v1",
            fresh_code_commit_sha="deadbeef",
            roll_schedule_by_instrument={"NSE-NIFTY-FUT": self._S2},
        )
        assert message is not None  # the drift verdict is unchanged
        assert calls == [path]  # ONE parse, not two

    # ── round-8 finding 10: a GENERATION is the FULL context triple ──

    @pytest.mark.parametrize(
        ("fresh_indicator", "fresh_sha"),
        [("ind-v1", "cafebabe"), ("ind-v2", "deadbeef")],
    )
    def test_code_upgrade_same_snapshot_generation_abstains(
        self, tmp_path: Path, fresh_indicator: str, fresh_sha: str
    ) -> None:
        """Round-8 finding 10 (fails-before/passes-after): a sessionless
        schedule drift under a byte-identical snapshot, then a code-upgrade
        (or indicator-bump) re-launch into the SAME sanctioned root. Every
        tuple digest folds `code_commit_sha`/`indicator_version`, so the C1
        generation's sentinels re-address under the C2 digests and can never
        pair with this launch — yet snapshot-ONLY matching picked up C1's
        recorded schedule, compared it against the fresh one, and permanently
        refused the root ('re-sweep into a fresh --results-root' for a hazard
        that cannot occur). Full-triple matching abstains like any moved
        generation."""
        root = tmp_path / "results"
        self._plant_sentinel(root)  # a C1 sentinel — NOT at any fresh C2 path
        audit = root / "audit" / "sweep-all.jsonl"
        audit.parent.mkdir(parents=True)
        self._write_audit(audit, snapshot="snap0001", schedule=self._S1)  # the C1 generation
        assert (
            stale_roll_schedule_refusal(
                audit,
                fresh_snapshot="snap0001",  # byte-identical lake snapshot
                fresh_indicator_version=fresh_indicator,
                fresh_code_commit_sha=fresh_sha,
                roll_schedule_by_instrument={"NSE-NIFTY-FUT": self._S2},
                results_root=root,
                fresh_sentinel_paths={
                    "NSE-NIFTY-FUT": [
                        root / "NSE-NIFTY-FUT" / "1minute" / "fresh.parquet.complete.json"
                    ]
                },
            )
            is None
        )

    # ── round-8 finding 12: the launch gate chain-verifies the audit ──

    def test_tampered_matching_schedule_cannot_sneak_past_the_gate(self, tmp_path: Path) -> None:
        """Round-8 finding 12 (fails-before/passes-after): the gate's recorded
        schedule is a verdict-grade fact, so the read is chain-verified. An
        attacker (or a bad hand-restore) rewriting the RECORDED schedule to
        match the fresh one — hiding a real drift — previously parsed clean
        and returned None (gate bypassed); the broken chain now lands in the
        UNREADABLE arm, refusing over a tree that holds futures sentinels."""
        root = tmp_path / "results"
        sentinel = self._plant_sentinel(root)
        audit = root / "audit" / "sweep-all.jsonl"
        audit.parent.mkdir(parents=True)
        self._write_audit(audit, snapshot="snap0001", schedule=self._S1)  # the TRUE record
        text = audit.read_text(encoding="utf-8")
        forged = text.replace(
            f'"roll_schedule":[{self._S1[0]}]',
            f'"roll_schedule":[{self._S2[0]},{self._S2[1]}]',
        )
        assert forged != text  # the tamper actually landed
        audit.write_text(forged, encoding="utf-8")
        message = stale_roll_schedule_refusal(
            audit,
            fresh_snapshot="snap0001",
            fresh_indicator_version="ind-v1",
            fresh_code_commit_sha="deadbeef",
            roll_schedule_by_instrument={"NSE-NIFTY-FUT": self._S2},
            results_root=root,
        )
        assert message is not None
        assert str(sentinel) in message  # the evidence named
        assert "UNREADABLE/inconsistent" in message


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

    def test_no_fill_modes_seam_survives(self) -> None:
        # DEC-FILL-WORST-CASE-ONLY-001 killed-token pin: the round-3 mode-SET
        # union planning is DELETED — verify_identity carries NO fill_modes
        # kwarg (worst-case is the only fill model; precise-era generations
        # refuse at fact-derivation, before any plan exists).
        import inspect

        assert "fill_modes" not in inspect.signature(verify_identity).parameters

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

    def test_futures_tree_with_and_without_recorded_schedule(self, tmp_path: Path) -> None:
        # ADR-0012 / DEC-NIFTY-FUTURES-SWEEP-LAUNCH-001 (F16): Tier 2
        # re-derives a futures tuple's identity from the tree's RECORDED
        # schedule; with NO schedule the run config cannot be rebuilt (the
        # instrument⟺schedule invariant raises) — an honest WARN finding,
        # never a crash, and the tree stays otherwise verifiable.
        from datetime import date

        from brutex.engine.sweep_orchestrator import make_store_for

        schedule = (date(2026, 3, 31).toordinal(),)
        # DEC-ENGINE-SPOT-ONLY-001: the futures verify machinery below the
        # gate is RETAINED — constructed via the sanctioned research flag.
        grid = _grid(instruments=["NSE-NIFTY-FUT"], unsafe_allow_out_of_scope_instruments=True)
        root = tmp_path / "results"
        root.mkdir()
        with AuditWriter.open(root / "audit.jsonl") as audit:
            run_sweep_grid(
                grid,
                bars_for=lambda _i, _t: _trending_bars(),
                store_for=make_store_for(root, grid),
                dedup=ComboDedup(OptionDedupIndex(capacity=10_000, fpr_target=1e-6)),
                audit=audit,
                roll_schedule_by_instrument={"NSE-NIFTY-FUT": schedule},
            )
        with_map = verify_identity(
            root, grid, roll_schedule_by_instrument={"NSE-NIFTY-FUT": schedule}
        )
        assert "row_identity_underivable" not in _codes(with_map)
        assert with_map.clean is True
        # The recorded schedule really is the audit's own (the F16 channel).
        assert recorded_roll_schedules_from_audit(root / "audit.jsonl") == {
            "NSE-NIFTY-FUT": schedule
        }
        without_map = verify_identity(root, grid)
        assert "row_identity_underivable" in _codes(without_map)
        assert without_map.clean is True  # warn — Tier-1 verifiability stands


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

    # ── the (a) honesty floor (DEC-SWEEP-OVERFIT-RECOUNT-RECOVERY-001) ──

    def test_original_absent_with_mixed_gate_sibling_reads_unverifiable(
        self, tmp_path: Path
    ) -> None:
        """A GRID tuple absent at the planned (recovered gate-config) address
        while THIS EXACT tuple's store exists at the DIVERGENT gate-config
        address (digest-PRECISE — the review fix round's MAJOR) is a
        MIXED-gate-config tree — the honest
        ``recount_mixed_mode_unverifiable`` WARN, never the pending-tuple
        ``recount_original_absent`` read (which would let a whole digest
        family pass as CLEAN-by-silence)."""
        original, scratch, grid = self._two_trees(tmp_path)
        store = _store_path(original, grid)
        # THIS tuple's store at the ONE divergent gate-config address (the
        # recovered grid is UNGATED, so the divergent family is default-gated).
        _alt_gate_store_path(original, grid).write_bytes(store.read_bytes())
        store.unlink()
        plan = plan_expected_stores(original, grid)
        report = compare_recount(original, scratch, plan, context_match=True)
        codes = _codes(report)
        assert "recount_mixed_mode_unverifiable" in codes
        assert "recount_original_absent" not in codes
        assert report.clean is True  # honesty (rc-4 via cmd_verify), never condemned

    def test_incomplete_single_config_sibling_stays_original_absent(self, tmp_path: Path) -> None:
        """THE review-fix-round MAJOR regression (fails-before on the
        prefix-glob probe): ``_tuple_digest`` folds the FULL tuple — a
        single-config grid with ≥2 SL values legitimately writes TWO
        same-``{direction}_k{k}_`` different-digest stores. Deleting ONE of
        them is a benign INCOMPLETE tree, NOT mixed gate configs: the verdict
        must stay the pending-tuple ``recount_original_absent``, never the
        rc-4 ``recount_mixed_mode_unverifiable`` false positive."""
        original, scratch, grid = self._two_trees(tmp_path, stop_losses=[80, 120])
        plan = plan_expected_stores(original, grid)
        assert len(plan) == 2  # two SL tuples — naturally same-prefix siblings
        victim = sorted(plan)[0]
        prefix = victim.name.rsplit("_", 1)[0]
        # The surviving sibling genuinely shares the {direction}_k{k}_ prefix
        # (the exact shape the pre-fix glob mis-read as mixed gate configs).
        assert any(p != victim and p.name.startswith(f"{prefix}_") for p in plan)
        victim.unlink()
        trial_stats_sidecar_path(victim).unlink()
        completion_sentinel_path(victim).unlink()
        report = compare_recount(original, scratch, plan, context_match=True)
        codes = _codes(report)
        assert "recount_original_absent" in codes
        assert "recount_mixed_mode_unverifiable" not in codes
        assert report.clean is True

    def test_original_absent_without_divergent_gate_store_stays_original_absent(
        self, tmp_path: Path
    ) -> None:
        """An absent original with NO store at the divergent gate-config
        address (the pairing ``….trials.json`` / ``….complete.json``
        artifacts still beside it) keeps the pending-tuple
        ``recount_original_absent`` read — the digest-precise probe checks
        for exactly ONE file and it is absent."""
        original, scratch, grid = self._two_trees(tmp_path)
        store = _store_path(original, grid)
        store.unlink()
        # The pairing artifacts are genuinely still in the partition dir.
        assert sorted(p.name for p in store.parent.glob("*.json")) == [
            f"{store.name}.complete.json",
            f"{store.name}.trials.json",
        ]
        assert not _alt_gate_store_path(original, grid).exists()
        plan = plan_expected_stores(original, grid)
        report = compare_recount(original, scratch, plan, context_match=True)
        codes = _codes(report)
        assert "recount_original_absent" in codes
        assert "recount_mixed_mode_unverifiable" not in codes

    def test_all_three_absent_artifacts_consult_the_sibling_memo_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The divergent-address probe is memoised PER TUPLE: when all THREE
        artifacts (store + trial-stats sidecar + completion sentinel) are
        absent from the original tree beside the divergent-gate store, each
        artifact reads ``recount_mixed_mode_unverifiable`` — but the
        alternate address is RE-PLANNED exactly ONCE (the second and third
        consultations return the cached verdict, the ``mixed_sibling_memo``
        short-circuit)."""
        from brutex.engine import sweep_verify as sweep_verify_module

        original, scratch, grid = self._two_trees(tmp_path)
        store = _store_path(original, grid)
        _alt_gate_store_path(original, grid).write_bytes(store.read_bytes())
        trial_stats_sidecar_path(store).unlink()
        completion_sentinel_path(store).unlink()
        store.unlink()
        plan = plan_expected_stores(original, grid)  # BEFORE the spy — plan calls it too
        probe_calls: list[Path] = []
        real_tuple_store_path = sweep_verify_module.tuple_store_path

        def counting_tuple_store_path(
            root: Path, tuple_: SweepTuple, grid_: SweepGridConfig
        ) -> Path:
            probe_calls.append(root)
            return real_tuple_store_path(root, tuple_, grid_)

        monkeypatch.setattr(sweep_verify_module, "tuple_store_path", counting_tuple_store_path)
        report = compare_recount(original, scratch, plan, context_match=True)
        code_list = [f.code for f in report.findings]
        assert code_list.count("recount_mixed_mode_unverifiable") == 3
        assert "recount_original_absent" not in code_list
        assert probe_calls == [original]  # ONE re-plan per tuple
        assert report.clean is True

    def test_cell_original_absent_with_sibling_stays_original_absent(self, tmp_path: Path) -> None:
        """``is_grid_tuple`` gates the (a) floor OFF for Stage-3 CELL stores —
        a cell address rides the stage-3 knob fold, not the gate-config fold,
        so even a store planted at the tuple's DIVERGENT gate-config address
        (the exact target a grid-arm probe would find) must keep the
        pending-tuple ``recount_original_absent`` read for an absent cell —
        the ``is_grid_tuple and`` guard's mutant-killer."""
        original, scratch, grid = self._two_trees(tmp_path)
        record = _stage3_record()
        plan = plan_expected_stores(original, grid, stage3=record)  # type: ignore[arg-type]
        (cell_rel,) = [
            p.relative_to(original) for p, (_, _, is_grid) in plan.items() if not is_grid
        ]
        template = ResultsStore.load(_store_path(scratch, grid)).rows()[0]
        dest = scratch / cell_rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        cell_store = ResultsStore(path=dest)
        cell_store.append(template)
        cell_store.flush()
        # Plant the store the (a)-floor probe would target for this tuple —
        # a dropped ``is_grid_tuple`` guard would fire mixed-gate on the cell.
        (cell_tuple, cell_grid, _) = plan[original / cell_rel]
        alt = tuple_store_path(
            original, cell_tuple, dataclasses.replace(cell_grid, overfit=OverfitGateConfig())
        )
        alt.parent.mkdir(parents=True, exist_ok=True)
        alt.write_bytes(dest.read_bytes())
        report = compare_recount(original, scratch, plan, context_match=True, cell_evidence=True)
        codes = _codes(report)
        assert "recount_original_absent" in codes
        assert "recount_mixed_mode_unverifiable" not in codes

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
        # DEC-LAKE-READ-INODE-GUARD-001: sweep_verify reads store bytes via
        # the guarded module-level seam, not Path.read_bytes — count there via
        # the string-target monkeypatch (the #1810 CodeQL convention). The
        # delegate is guarded_read's identity-pinned object — the SAME function
        # sweep_verify binds (`from brutex.data.guarded_read import ...`).
        from brutex.data.guarded_read import read_regular_bytes as real_read_bytes

        def counting_read_bytes(path: Path) -> bytes:
            if str(path) in store_strs:
                counts[str(path)] = counts.get(str(path), 0) + 1
            return real_read_bytes(path)

        monkeypatch.setattr("brutex.engine.sweep_verify.read_regular_bytes", counting_read_bytes)
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
            assert "carry NO values on the diverging nullable columns" in finding.detail
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
        # A faithful pre-VIX-generation stand-in lacks the trio AND every
        # strictly-later appended nullable family (growth / exit-quality /
        # recency) — a real pre-VIX writer predates them all.
        _drop_store_columns(store_path, sorted(_APPENDED_NULLABLE_FIELDS))
        assert store_path.read_bytes() != _store_path(scratch, grid).read_bytes()
        plan = plan_expected_stores(original, grid)
        report = compare_recount(original, scratch, plan, context_match=True)
        assert _codes(report) == {"recount_vix_divergence"}
        assert len(report.findings) == 1  # sidecar+sentinel byte-identical → silent
        assert report.clean is True  # warn-grade — NEVER heal-grade
        finding = report.findings[0]
        assert finding.severity == "warn"
        assert "India-VIX lake moved" not in finding.detail
        assert "carry NO values on the diverging nullable columns" in finding.detail
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

    def test_recorded_later_family_value_divergence_abstains(self, tmp_path: Path) -> None:
        # A recorded NON-None growth/exit-quality/recency value the recount
        # cannot reproduce is a genuine identity-envelope divergence — the
        # attribution must abstain (None → the corrupt ladder), never read
        # it as a VIX backfill/moved shape.
        original, scratch, grid = self._two_trees(
            tmp_path, _verify_stamper("14.00"), _verify_stamper("14.00")
        )
        store_path = _store_path(original, grid)
        rows = ResultsStore.load(store_path).rows()
        assert rows[0].loss_rate is not None  # the current writer computes it
        self._rewrite_store(
            store_path,
            (dataclasses.replace(rows[0], loss_rate=0.987654321), *rows[1:]),
        )
        assert _stores_differ_only_in_vix(store_path, _store_path(scratch, grid)) is None

    def test_pre_extension_none_later_family_rows_are_the_backfill_shape(
        self, tmp_path: Path
    ) -> None:
        # The ROW shape of a pre-extension store recounted under current
        # code: the original rows hold None on every later-family nullable
        # metric while the recount computes real values — the sanctioned
        # directional None→value backfill, attributed never condemned.
        original, scratch, grid = self._two_trees(
            tmp_path, _verify_stamper("14.00"), _verify_stamper("14.00")
        )
        store_path = _store_path(original, grid)
        rows = ResultsStore.load(store_path).rows()
        later_nulled: dict[str, None] = dict.fromkeys(
            sorted(_APPENDED_NULLABLE_FIELDS - set(VIX_FIELDS))
        )
        self._rewrite_store(
            store_path,
            tuple(dataclasses.replace(row, **later_nulled) for row in rows),  # type: ignore[arg-type]
        )
        assert _stores_differ_only_in_vix(store_path, _store_path(scratch, grid)) == "backfill"

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
        # Missing appended nullable columns PLUS any other column drift —
        # stripping the appended nullable columns from the scratch schema no
        # longer reproduces the original: abstain (the final strip-compare
        # arm, reached with both presence gates passing).
        original = tmp_path / "original.parquet"
        scratch = tmp_path / "scratch.parquet"
        self._reflush(_PRE_VIX_FIXTURE, original)
        self._reflush(_PRE_VIX_FIXTURE, scratch)
        _drop_store_columns(original, [*sorted(_APPENDED_NULLABLE_FIELDS), "deflated_sharpe"])
        assert _is_pre_vix_schema_migration(original, scratch) is False
        assert _stores_differ_only_in_vix(original, scratch) is None

    def test_later_family_presence_abstains(self, tmp_path: Path) -> None:
        # A store carrying a LATER appended nullable column (sl_hit_rate)
        # while missing the trio matches NO real writer generation — the
        # widened presence gate abstains, never a guessed attribution.
        original = tmp_path / "original.parquet"
        scratch = tmp_path / "scratch.parquet"
        self._reflush(_PRE_VIX_FIXTURE, original)
        self._reflush(_PRE_VIX_FIXTURE, scratch)
        _drop_store_columns(original, sorted(_APPENDED_NULLABLE_FIELDS - {"sl_hit_rate"}))
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
        from brutex.orchestrator import audit as audit_mod

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


class TestRecordedAprioriFromAudit:
    """DEC-SWEEP-APRIORI-DEFAULT-001 (MIGRATION.md #1752): the tree's OWN
    recorded exploration mode, read off the first mode-carrying START record
    (EMIT-ON-apriori law: key absent == legacy — pre-flip trees classify
    correctly for free; no record == None abstain)."""

    @staticmethod
    def _write_start(root: Path, chain: str, action: str, payload: dict[str, object]) -> None:
        path = root / "audit" / chain
        path.parent.mkdir(parents=True, exist_ok=True)
        with AuditWriter.open(path) as audit:
            audit.append(action=action, subject="sweep-all", payload=payload)

    def test_no_audit_files_abstains(self, tmp_path: Path) -> None:
        assert recorded_apriori_from_audit(tmp_path) is None

    def test_apriori_supervisor_start_reads_true(self, tmp_path: Path) -> None:
        from brutex.engine.sweep_audit import SweepAuditWriter

        path = tmp_path / "audit" / "sweep-all-supervisor.jsonl"
        path.parent.mkdir(parents=True)
        with SweepAuditWriter.open(path) as sup:
            sup.append(
                action="SUPERVISOR_START",
                subject="sweep-all",
                payload={"apriori": {"enabled": True, "max_mask_bars": 42}},
            )
        assert recorded_apriori_from_audit(tmp_path) is True

    def test_start_without_key_reads_legacy_false(self, tmp_path: Path) -> None:
        # The EMIT-ON-apriori law: absence == legacy — every pre-flip tree.
        self._write_start(tmp_path, "sweep-all.jsonl", "GRID_SWEEP_START", {"sweep_id": "x"})
        assert recorded_apriori_from_audit(tmp_path) is False

    def test_grid_start_with_key_reads_true(self, tmp_path: Path) -> None:
        self._write_start(
            tmp_path,
            "sweep-all.jsonl",
            "GRID_SWEEP_START",
            {"apriori": {"enabled": True, "max_mask_bars": 7}},
        )
        assert recorded_apriori_from_audit(tmp_path) is True

    def test_disabled_or_malformed_key_reads_false(self, tmp_path: Path) -> None:
        # A present-but-not-enabled key is NOT an apriori tree (never a guess).
        self._write_start(
            tmp_path,
            "sweep-all.jsonl",
            "GRID_SWEEP_START",
            {"apriori": {"enabled": False}},
        )
        assert recorded_apriori_from_audit(tmp_path) is False

    def test_non_mapping_key_reads_false(self, tmp_path: Path) -> None:
        self._write_start(tmp_path, "sweep-all.jsonl", "GRID_SWEEP_START", {"apriori": "yes"})
        assert recorded_apriori_from_audit(tmp_path) is False

    def test_per_tuple_start_lines_are_not_the_mode_family(self, tmp_path: Path) -> None:
        # A chain holding ONLY per-tuple SWEEP_START lines carries no mode —
        # the scan falls through to the supervisor chain.
        self._write_start(tmp_path, "sweep-all.jsonl", "SWEEP_START", {"k": 2})
        from brutex.engine.sweep_audit import SweepAuditWriter

        sup_path = tmp_path / "audit" / "sweep-all-supervisor.jsonl"
        with SweepAuditWriter.open(sup_path) as sup:
            sup.append(action="SUPERVISOR_START", subject="sweep-all", payload={"w": 2})
        assert recorded_apriori_from_audit(tmp_path) is False

    def test_no_mode_record_in_either_chain_abstains(self, tmp_path: Path) -> None:
        self._write_start(tmp_path, "sweep-all.jsonl", "SWEEP_START", {"k": 2})
        assert recorded_apriori_from_audit(tmp_path) is None

    @staticmethod
    def _write_both_chains(root: Path, *, ingest_ts: float, supervisor_ts: float) -> None:
        """An apriori GRID_SWEEP_START (ingest chain) + a mode-LESS legacy
        SUPERVISOR_START (supervisor chain), each at an explicit ``ts_utc``."""
        from brutex.engine.sweep_audit import SweepAuditWriter

        path = root / "audit" / "sweep-all.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with AuditWriter.open(path) as audit:
            audit.append(
                action="GRID_SWEEP_START",
                subject="sweep-all",
                payload={"apriori": {"enabled": True, "max_mask_bars": 9}},
                ts_utc=ingest_ts,
            )
        sup_path = root / "audit" / "sweep-all-supervisor.jsonl"
        with SweepAuditWriter.open(sup_path) as sup:
            sup.append(
                action="SUPERVISOR_START",
                subject="sweep-all",
                payload={"w": 2},
                ts_utc=supervisor_ts,
            )

    def test_newer_supervisor_start_wins_over_an_older_ingest_start(self, tmp_path: Path) -> None:
        """Review fix round 2 probe D, direction 1: an OLD library
        ``GRID_SWEEP_START`` (apriori) must never shadow the strictly NEWER
        production ``SUPERVISOR_START`` (legacy) — the winner is the latest
        ``ts_utc`` ACROSS chains, regardless of chain (the pre-fix per-chain
        ingest-first precedence permanently pinned this tree apriori)."""
        self._write_both_chains(tmp_path, ingest_ts=100.0, supervisor_ts=200.0)
        assert recorded_apriori_from_audit(tmp_path) is False

    def test_newer_ingest_start_wins_over_an_older_supervisor_start(self, tmp_path: Path) -> None:
        """Probe D, direction 2 (the reverse): a strictly NEWER ingest-chain
        mode START outranks an older supervisor record."""
        self._write_both_chains(tmp_path, ingest_ts=200.0, supervisor_ts=100.0)
        assert recorded_apriori_from_audit(tmp_path) is True

    def test_exact_ts_tie_keeps_the_supervisor_record(self, tmp_path: Path) -> None:
        """An exact ``ts_utc`` tie keeps the SUPERVISOR chain's record (the
        production launchers' chain) — the ingest chain overrides only on a
        STRICTLY later launch."""
        self._write_both_chains(tmp_path, ingest_ts=150.0, supervisor_ts=150.0)
        assert recorded_apriori_from_audit(tmp_path) is False

    def test_unreadable_audit_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "audit" / "sweep-all.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text("{not jsonl\n", encoding="utf-8")
        with pytest.raises(SweepVerifyError, match="unreadable audit log"):
            recorded_apriori_from_audit(tmp_path)

    def test_last_mode_start_in_a_chain_wins_after_a_mode_flip(self, tmp_path: Path) -> None:
        """Review fix round 1 M2 — the multi-launch daemon root: a LEGACY tree
        whose supervisor chain later accretes an extinction-cycle
        SUPERVISOR_START (appended via the REAL ``SweepAuditWriter`` resume,
        exactly like a second daemon launch) must read the LATEST launch's
        mode — the first-record-wins read permanently downgraded every
        recount after the flip."""
        from brutex.engine.sweep_audit import SweepAuditWriter

        sup_path = tmp_path / "audit" / "sweep-all-supervisor.jsonl"
        sup_path.parent.mkdir(parents=True)
        # Launch 1: legacy (mode-less START — the EMIT-ON-apriori law).
        with SweepAuditWriter.open(sup_path) as sup:
            sup.append(action="SUPERVISOR_START", subject="sweep-all", payload={"w": 2})
        assert recorded_apriori_from_audit(tmp_path) is False
        # Launch 2: the extinction cycle appends onto the SAME chain.
        with SweepAuditWriter.open(sup_path, resume=True) as sup:
            sup.append(
                action="SUPERVISOR_START",
                subject="sweep-all",
                payload={"apriori": {"enabled": True, "max_mask_bars": 42}},
            )
        assert recorded_apriori_from_audit(tmp_path) is True

    def test_mode_flip_back_to_legacy_reads_legacy(self, tmp_path: Path) -> None:
        # The symmetric flip: extinction then legacy → the LAST (legacy) wins.
        from brutex.engine.sweep_audit import SweepAuditWriter

        sup_path = tmp_path / "audit" / "sweep-all-supervisor.jsonl"
        sup_path.parent.mkdir(parents=True)
        with SweepAuditWriter.open(sup_path) as sup:
            sup.append(
                action="SUPERVISOR_START",
                subject="sweep-all",
                payload={"apriori": {"enabled": True, "max_mask_bars": 42}},
            )
        with SweepAuditWriter.open(sup_path, resume=True) as sup:
            sup.append(action="SUPERVISOR_START", subject="sweep-all", payload={"w": 2})
        assert recorded_apriori_from_audit(tmp_path) is False

    def test_each_chain_is_streamed_exactly_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Review fix round 1 m4: ONE streaming ``iter_entries`` pass per present
        chain — never a materialised ``read_entries`` list, never a second
        pass (the last-wins fold happens in-stream)."""
        from brutex.orchestrator import audit as audit_mod

        self._write_start(tmp_path, "sweep-all.jsonl", "SWEEP_START", {"k": 2})
        from brutex.engine.sweep_audit import SweepAuditWriter

        sup_path = tmp_path / "audit" / "sweep-all-supervisor.jsonl"
        with SweepAuditWriter.open(sup_path) as sup:
            sup.append(action="SUPERVISOR_START", subject="sweep-all", payload={"w": 2})

        iter_calls: list[Path] = []
        real_iter = audit_mod.iter_entries

        def _spy_iter(path: Path) -> object:
            iter_calls.append(path)
            return real_iter(path)

        def _no_read(path: Path) -> object:
            raise AssertionError("recorded_apriori_from_audit must stream, never materialise")

        monkeypatch.setattr(audit_mod, "iter_entries", _spy_iter)
        monkeypatch.setattr(audit_mod, "read_entries", _no_read)
        assert recorded_apriori_from_audit(tmp_path) is False
        assert iter_calls.count(tmp_path / "audit" / "sweep-all.jsonl") == 1
        assert iter_calls.count(sup_path) == 1


class TestRecordedOverfitFromAudit:
    """DEC-SWEEP-OVERFIT-RECOUNT-RECOVERY-001: the tree's OWN recorded
    OVERFIT gate config, read off the last START record per chain (latest
    ``ts_utc`` across chains wins; the supervisor chain keeps an exact tie —
    the ``recorded_apriori_from_audit`` law), with the PER-CHAIN TOLERANT
    read: an unreadable chain is skipped WHOLE with one loud warning, never a
    raise — the recovery is consulted UNCONDITIONALLY by ``cmd_verify``, so a
    raise would hard-fail EVERY verify over one bit-rotted chain (the m2
    survival law). The surviving READABLE chain supplies the config when it
    carries a START record (GRID_SWEEP_START is minted by the LIBRARY
    ``run_sweep_grid`` entry; a ``cmd_sweep_all`` tree carries the config on
    SUPERVISOR_START only — its corrupt-supervisor shape is the honest
    config-unrecoverable ``None``, pinned in the e2e m2 test)."""

    _LEG: ClassVar[list[float]] = [0.9, 0.07, 0.1, 0.2]

    @staticmethod
    def _write_grid_start(
        root: Path, payload: dict[str, object], *, ts_utc: float | None = None
    ) -> None:
        path = root / "audit" / "sweep-all.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with AuditWriter.open(path, resume=path.exists()) as audit:
            audit.append(
                action="GRID_SWEEP_START", subject="sweep-all", payload=payload, ts_utc=ts_utc
            )

    @staticmethod
    def _write_supervisor_start(
        root: Path, payload: dict[str, object], *, ts_utc: float | None = None
    ) -> None:
        from brutex.engine.sweep_audit import SweepAuditWriter

        path = root / "audit" / "sweep-all-supervisor.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with SweepAuditWriter.open(path, resume=path.exists()) as sup:
            sup.append(
                action="SUPERVISOR_START", subject="sweep-all", payload=payload, ts_utc=ts_utc
            )

    def test_no_audit_files_returns_none(self, tmp_path: Path) -> None:
        assert recorded_overfit_from_audit(tmp_path) is None

    def test_supervisor_gate_config_recovers_in_fragment_order(self, tmp_path: Path) -> None:
        # The [dsr_min, weight, default_skew, default_excess_kurtosis]
        # fragment order is pinned FIELD-FOR-FIELD (kills order-swap mutants).
        self._write_supervisor_start(tmp_path, {"gate_config": {"overfit": self._LEG}})
        cfg = recorded_overfit_from_audit(tmp_path)
        assert cfg is not None
        assert cfg.dsr_min == 0.9
        assert cfg.weight == 0.07
        assert cfg.default_skew == 0.1
        assert cfg.default_excess_kurtosis == 0.2

    def test_grid_gate_config_recovers(self, tmp_path: Path) -> None:
        self._write_grid_start(tmp_path, {"gate_config": {"overfit": self._LEG}})
        cfg = recorded_overfit_from_audit(tmp_path)
        assert cfg is not None
        assert cfg.dsr_min == 0.9

    def test_start_without_gate_config_returns_none(self, tmp_path: Path) -> None:
        # The EMIT-ON-active law: absence == an ungated / pre-channel tree.
        self._write_grid_start(tmp_path, {"sweep_id": "x"})
        assert recorded_overfit_from_audit(tmp_path) is None

    def test_non_mapping_gate_config_reads_none(self, tmp_path: Path) -> None:
        self._write_grid_start(tmp_path, {"gate_config": "junk"})
        assert recorded_overfit_from_audit(tmp_path) is None

    def test_per_tuple_start_lines_are_not_the_start_family(self, tmp_path: Path) -> None:
        path = tmp_path / "audit" / "sweep-all.jsonl"
        path.parent.mkdir(parents=True)
        with AuditWriter.open(path) as audit:
            audit.append(action="SWEEP_START", subject="sweep-all", payload={"k": 2})
        assert recorded_overfit_from_audit(tmp_path) is None

    def test_newer_ungated_start_wins_over_an_older_gated_start(self, tmp_path: Path) -> None:
        self._write_grid_start(tmp_path, {"gate_config": {"overfit": self._LEG}}, ts_utc=100.0)
        self._write_supervisor_start(tmp_path, {"w": 2}, ts_utc=200.0)
        assert recorded_overfit_from_audit(tmp_path) is None

    def test_newer_gated_start_wins_over_an_older_ungated_start(self, tmp_path: Path) -> None:
        self._write_supervisor_start(tmp_path, {"w": 2}, ts_utc=100.0)
        self._write_grid_start(tmp_path, {"gate_config": {"overfit": self._LEG}}, ts_utc=200.0)
        cfg = recorded_overfit_from_audit(tmp_path)
        assert cfg is not None
        assert cfg.dsr_min == 0.9

    def test_exact_ts_tie_keeps_the_supervisor_record(self, tmp_path: Path) -> None:
        self._write_supervisor_start(tmp_path, {"w": 2}, ts_utc=150.0)
        self._write_grid_start(tmp_path, {"gate_config": {"overfit": self._LEG}}, ts_utc=150.0)
        assert recorded_overfit_from_audit(tmp_path) is None

    def test_last_start_in_a_chain_wins_after_a_gate_flip(self, tmp_path: Path) -> None:
        # Launch 1 gated, launch 2 ungated on the SAME chain → the LAST wins.
        self._write_grid_start(tmp_path, {"gate_config": {"overfit": self._LEG}})
        self._write_grid_start(tmp_path, {"w": 2})
        assert recorded_overfit_from_audit(tmp_path) is None

    def test_corrupt_supervisor_chain_is_skipped_and_the_grid_chain_supplies_the_config(
        self, tmp_path: Path
    ) -> None:
        """THE tolerant-read pin (fails-before: the pre-tolerant read RAISED
        ``SweepVerifyError: unreadable audit log`` here): a bit-rotted
        supervisor chain is skipped with ONE loud warning and an intact grid
        chain carrying a GRID_SWEEP_START gate_config (the LIBRARY
        ``run_sweep_grid`` mint) still supplies the recovered config; no
        raise, so a verify never hard-fails on the corrupt chain."""
        from structlog.testing import capture_logs

        sup_path = tmp_path / "audit" / "sweep-all-supervisor.jsonl"
        sup_path.parent.mkdir(parents=True)
        sup_path.write_text("{not jsonl\n", encoding="utf-8")
        self._write_grid_start(tmp_path, {"gate_config": {"overfit": self._LEG}})
        with capture_logs() as logs:
            cfg = recorded_overfit_from_audit(tmp_path)
        assert cfg is not None
        assert cfg.dsr_min == 0.9
        warned = [e for e in logs if e["event"] == "sweep_verify.overfit_recovery_chain_unreadable"]
        assert len(warned) == 1
        assert warned[0]["log_level"] == "warning"
        assert str(sup_path) == warned[0]["audit_path"]

    def test_both_chains_unreadable_returns_none_with_a_warning_per_chain(
        self, tmp_path: Path
    ) -> None:
        from structlog.testing import capture_logs

        audit_dir = tmp_path / "audit"
        audit_dir.mkdir(parents=True)
        (audit_dir / "sweep-all-supervisor.jsonl").write_text("{not jsonl\n", encoding="utf-8")
        (audit_dir / "sweep-all.jsonl").write_text("{not jsonl\n", encoding="utf-8")
        with capture_logs() as logs:
            assert recorded_overfit_from_audit(tmp_path) is None
        warned = [e for e in logs if e["event"] == "sweep_verify.overfit_recovery_chain_unreadable"]
        assert len(warned) == 2

    def test_unreadable_chain_partial_records_are_discarded_whole(self, tmp_path: Path) -> None:
        # A gated START followed by mid-chain corruption: the chain is skipped
        # WHOLE — a broken hash chain is never trusted piecemeal, so the
        # pre-corruption gated record must NOT be adopted.
        from structlog.testing import capture_logs

        self._write_grid_start(tmp_path, {"gate_config": {"overfit": self._LEG}})
        path = tmp_path / "audit" / "sweep-all.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            fh.write("{not jsonl\n")
        with capture_logs() as logs:
            assert recorded_overfit_from_audit(tmp_path) is None
        warned = [e for e in logs if e["event"] == "sweep_verify.overfit_recovery_chain_unreadable"]
        assert len(warned) == 1

    def test_malformed_leg_shape_raises(self, tmp_path: Path) -> None:
        self._write_grid_start(tmp_path, {"gate_config": {"overfit": [0.9, 0.05]}})
        with pytest.raises(SweepVerifyError, match="malformed gate_config.overfit leg"):
            recorded_overfit_from_audit(tmp_path)

    def test_string_leg_raises(self, tmp_path: Path) -> None:
        # A str IS a Sequence — the explicit str/bytes exclusion must refuse it.
        self._write_grid_start(tmp_path, {"gate_config": {"overfit": "junk"}})
        with pytest.raises(SweepVerifyError, match="malformed gate_config.overfit leg"):
            recorded_overfit_from_audit(tmp_path)

    def test_bool_leg_member_raises(self, tmp_path: Path) -> None:
        # bool is an int subclass — the explicit bool exclusion must refuse it.
        self._write_grid_start(tmp_path, {"gate_config": {"overfit": [True, 0.05, 0.0, 0.0]}})
        with pytest.raises(SweepVerifyError, match="malformed gate_config.overfit leg"):
            recorded_overfit_from_audit(tmp_path)

    def test_out_of_range_leg_values_raise(self, tmp_path: Path) -> None:
        # OverfitGateConfig.__post_init__'s ValueError is wrapped, never raw.
        self._write_grid_start(tmp_path, {"gate_config": {"overfit": [2.0, 0.05, 0.0, 0.0]}})
        with pytest.raises(SweepVerifyError, match="invalid recorded overfit gate config"):
            recorded_overfit_from_audit(tmp_path)

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_leg_member_raises(self, tmp_path: Path, bad: float) -> None:
        """MINOR-1 (review fix round): the supervisor chain's ``json.dumps``
        rides the ``allow_nan=True`` default, so a ``NaN``/``Infinity`` leg
        member is REACHABLE end-to-end through a hash-valid chain — and
        ``OverfitGateConfig.__post_init__`` never finiteness-refuses the
        ``weight`` slot, so an adopted non-finite member would crash
        ``cmd_verify`` later at its ``json.dumps(allow_nan=False)`` summary
        (an uncaught ``ValueError``). The leg predicate's ``math.isfinite``
        must refuse it LOUDLY here instead."""
        self._write_supervisor_start(tmp_path, {"gate_config": {"overfit": [0.9, bad, 0.0, 0.0]}})
        with pytest.raises(SweepVerifyError, match="malformed gate_config.overfit leg"):
            recorded_overfit_from_audit(tmp_path)


# ── recount_mode_divergence (DEC-SWEEP-APRIORI-DEFAULT-001 review fix round 2) ──


class TestRecountModeDivergence:
    """The MIXED-MODE census residue is warn-attributed, never CORRUPT.

    DEC-SWEEP-APRIORI-DEFAULT-001 (MIGRATION.md #1752) review fix round 2:
    the mode-blind tuple digest legitimately resume-skips cross-mode tuples,
    so a matched-mode recount of a MIXED tree byte-diverges the trial-stats
    sidecar on the exploration-mode census triple over a byte-identical store
    — the comparator must attribute the CONFINED shape precisely instead of
    condemning a provably-reproduced tuple as heal-grade corruption. Anything
    beyond the confined shape stays verdict-grade on the normal ladder.
    """

    def _two_identical_trees(self, tmp_path: Path) -> tuple[Path, Path, SweepGridConfig]:
        grid = _grid()
        original, scratch = tmp_path / "original", tmp_path / "scratch"
        for root in (original, scratch):
            root.mkdir()
            _sweep(root, grid)
        return original, scratch, grid

    @staticmethod
    def _mutate_sidecar(store_path: Path, **deltas: int) -> None:
        """Rewrite the sidecar with per-field integer DELTAS (invariant-safe)."""
        sidecar = trial_stats_sidecar_path(store_path)
        stats = read_tuple_trial_stats(sidecar)
        changes = {name: getattr(stats, name) + delta for name, delta in deltas.items()}
        write_tuple_trial_stats(sidecar, dataclasses.replace(stats, **changes))

    def test_confined_triple_divergence_is_warn_never_corrupt(self, tmp_path: Path) -> None:
        original, scratch, grid = self._two_identical_trees(tmp_path)
        self._mutate_sidecar(_store_path(scratch, grid), n_enumerated=9)
        plan = plan_expected_stores(original, grid)
        report = compare_recount(original, scratch, plan, context_match=True)
        assert _codes(report) == {"recount_mode_divergence"}
        assert len(report.findings) == 1
        assert report.clean is True  # warn-grade — NEVER heal-grade
        (finding,) = report.findings
        assert finding.severity == "warn"
        assert finding.detail.startswith(
            "ONLY the exploration-MODE-dependent census triple "
            "(n_enumerated/n_conflict_pruned/n_minhits_dropped) diverge"
        )
        assert "MIXED-mode" in finding.detail
        assert "never heal-grade" in finding.detail

    def test_every_triple_field_divergence_is_attributed(self, tmp_path: Path) -> None:
        original, scratch, grid = self._two_identical_trees(tmp_path)
        self._mutate_sidecar(
            _store_path(scratch, grid), n_enumerated=9, n_conflict_pruned=1, n_minhits_dropped=1
        )
        plan = plan_expected_stores(original, grid)
        report = compare_recount(original, scratch, plan, context_match=True)
        assert _codes(report) == {"recount_mode_divergence"}
        assert report.clean is True

    def test_mode_divergence_under_context_mismatch_keeps_the_precise_code(
        self, tmp_path: Path
    ) -> None:
        # The VIX-precedent law: a PROVEN confined attribution is more
        # informative than a generic inconclusive, regardless of the context
        # gate — the --allow-context-mismatch legacy-recount-of-an-apriori-tree
        # path reads the SAME precise code.
        original, scratch, grid = self._two_identical_trees(tmp_path)
        self._mutate_sidecar(_store_path(scratch, grid), n_enumerated=9)
        plan = plan_expected_stores(original, grid)
        report = compare_recount(original, scratch, plan, context_match=False)
        assert _codes(report) == {"recount_mode_divergence"}
        assert "recount_divergence_inconclusive" not in _codes(report)

    def test_triple_plus_mode_invariant_field_stays_corrupt(self, tmp_path: Path) -> None:
        # Condition (c) violated: n_backtested is mode-INVARIANT, so a triple
        # + n_backtested divergence is never the census residue.
        original, scratch, grid = self._two_identical_trees(tmp_path)
        self._mutate_sidecar(_store_path(scratch, grid), n_enumerated=9, n_backtested=1)
        plan = plan_expected_stores(original, grid)
        report = compare_recount(original, scratch, plan, context_match=True)
        assert _codes(report) == {"recount_divergence"}
        assert not report.clean

    def test_tampered_store_byte_stays_corrupt(self, tmp_path: Path) -> None:
        # Condition (a) violated: the store-byte equality is the proof
        # obligation — a tampered store byte keeps EVERYTHING verdict-grade.
        original, scratch, grid = self._two_identical_trees(tmp_path)
        self._mutate_sidecar(_store_path(scratch, grid), n_enumerated=9)
        store = _store_path(original, grid)
        store.write_bytes(store.read_bytes() + b"x")
        plan = plan_expected_stores(original, grid)
        report = compare_recount(original, scratch, plan, context_match=True)
        assert _codes(report) == {"recount_divergence"}
        assert "recount_mode_divergence" not in _codes(report)
        assert not report.clean

    @pytest.mark.parametrize("which", ["original", "scratch"])
    def test_absent_store_stays_on_the_ladder(self, tmp_path: Path, which: str) -> None:
        # Condition (a) unprovable: with either world's store ABSENT the
        # sidecar divergence can never be mode-attributed.
        original, scratch, grid = self._two_identical_trees(tmp_path)
        self._mutate_sidecar(_store_path(scratch, grid), n_enumerated=9)
        root = original if which == "original" else scratch
        _store_path(root, grid).unlink()
        plan = plan_expected_stores(original, grid)
        report = compare_recount(original, scratch, plan, context_match=True)
        codes = _codes(report)
        assert "recount_mode_divergence" not in codes
        assert "recount_divergence" in codes  # the sidecar stays verdict-grade
        assert f"recount_{which}_absent" in codes  # the store's own honest arm

    def test_sentinel_byte_drift_stays_corrupt(self, tmp_path: Path) -> None:
        # Condition (b) violated: a sentinel drift is never mode-attributable.
        original, scratch, grid = self._two_identical_trees(tmp_path)
        self._mutate_sidecar(_store_path(scratch, grid), n_enumerated=9)
        sentinel = completion_sentinel_path(_store_path(scratch, grid))
        sentinel.write_bytes(sentinel.read_bytes() + b" ")
        plan = plan_expected_stores(original, grid)
        report = compare_recount(original, scratch, plan, context_match=True)
        codes = _codes(report)
        assert "recount_mode_divergence" not in codes
        assert "recount_divergence" in codes
        assert not report.clean

    def test_one_sided_sentinel_stays_corrupt(self, tmp_path: Path) -> None:
        # Condition (b) violated the other way: a one-sided sentinel is a
        # MISMATCH (present vs absent), never "both absent".
        original, scratch, grid = self._two_identical_trees(tmp_path)
        self._mutate_sidecar(_store_path(scratch, grid), n_enumerated=9)
        completion_sentinel_path(_store_path(scratch, grid)).unlink()
        plan = plan_expected_stores(original, grid)
        report = compare_recount(original, scratch, plan, context_match=True)
        codes = _codes(report)
        assert "recount_mode_divergence" not in codes
        assert "recount_divergence" in codes

    def test_both_absent_sentinels_still_attribute(self, tmp_path: Path) -> None:
        # Both-absent sentinels MATCH by definition (a mid-sweep PENDING tuple
        # in both worlds) — the confined sidecar diff still attributes.
        original, scratch, grid = self._two_identical_trees(tmp_path)
        self._mutate_sidecar(_store_path(scratch, grid), n_enumerated=9)
        for root in (original, scratch):
            completion_sentinel_path(_store_path(root, grid)).unlink()
        plan = plan_expected_stores(original, grid)
        report = compare_recount(original, scratch, plan, context_match=True)
        assert _codes(report) == {"recount_mode_divergence"}
        assert len(report.findings) == 1
        assert report.clean is True

    def test_unreadable_scratch_sidecar_stays_corrupt(self, tmp_path: Path) -> None:
        original, scratch, grid = self._two_identical_trees(tmp_path)
        trial_stats_sidecar_path(_store_path(scratch, grid)).write_bytes(b"{garbage")
        plan = plan_expected_stores(original, grid)
        report = compare_recount(original, scratch, plan, context_match=True)
        assert _codes(report) == {"recount_divergence"}
        assert not report.clean

    def test_equal_stats_byte_only_diff_stays_on_the_ladder(self, tmp_path: Path) -> None:
        # Fully-EQUAL parsed stats over byte-different files: the diff has an
        # unknown non-field cause — NEVER attributed (no census field differs).
        original, scratch, grid = self._two_identical_trees(tmp_path)
        sidecar = trial_stats_sidecar_path(_store_path(scratch, grid))
        sidecar.write_bytes(sidecar.read_bytes() + b"\n")
        assert read_tuple_trial_stats(sidecar) is not None  # still readable
        plan = plan_expected_stores(original, grid)
        report = compare_recount(original, scratch, plan, context_match=True)
        assert _codes(report) == {"recount_divergence"}
        assert not report.clean

    def test_cell_sidecar_confined_diff_keeps_the_cell_ladder(self, tmp_path: Path) -> None:
        # The gate is GRID-only: a Stage-3 CELL sidecar's confined diff stays
        # on the DISTINCT cell code family (the heal consumes only the grid
        # code, and cells carry no mode census contract).
        original, scratch, grid = self._two_identical_trees(tmp_path)
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
            shutil.copyfile(
                trial_stats_sidecar_path(root / grid_store.relative_to(original)),
                trial_stats_sidecar_path(dest),
            )
        self._mutate_sidecar(scratch / cell_rel, n_enumerated=9)
        report = compare_recount(original, scratch, plan, context_match=True, cell_evidence=True)
        codes = _codes(report)
        assert "recount_mode_divergence" not in codes
        assert "cell_recount_divergence" in codes

    def test_census_fields_only_differ_arms(self) -> None:
        """The pure helper: census-only diff → True; equal / mode-invariant
        diff / mixed diff → False (the must-equal side is fields()-driven)."""
        base = TupleTrialStats(
            n_backtested=2,
            n_finite=2,
            sum_sharpe=1.0,
            sum_sharpe_sq=1.0,
            n_enumerated=8,
            n_conflict_pruned=1,
            n_minhits_dropped=1,
            tuple_digest="ab" * 8,
            store_content_hash="cd" * 32,
        )
        assert _census_fields_only_differ(base, base) is False  # nothing differs
        assert _census_fields_only_differ(base, dataclasses.replace(base, n_enumerated=9)) is True
        assert (
            _census_fields_only_differ(
                base,
                dataclasses.replace(base, n_conflict_pruned=2, n_minhits_dropped=2),
            )
            is True
        )
        # A mode-INVARIANT field difference fails the must-equal side — alone
        # AND alongside a census diff.
        assert _census_fields_only_differ(base, dataclasses.replace(base, sum_sharpe=2.0)) is False
        assert (
            _census_fields_only_differ(
                base, dataclasses.replace(base, n_enumerated=9, sum_sharpe=2.0)
            )
            is False
        )

    def test_sidecar_residue_helper_reads_and_gates(self, tmp_path: Path) -> None:
        """The IO half: an unreadable sidecar is never attributable; a valid
        confined pair is; a valid non-confined pair is not."""
        base = TupleTrialStats(
            n_backtested=2,
            n_finite=2,
            sum_sharpe=1.0,
            sum_sharpe_sq=1.0,
            n_enumerated=5,
            n_conflict_pruned=1,
            n_minhits_dropped=1,
            tuple_digest="ab" * 8,
            store_content_hash="cd" * 32,
        )
        ours = tmp_path / "ours.trials.json"
        theirs = tmp_path / "theirs.trials.json"
        write_tuple_trial_stats(ours, base)
        write_tuple_trial_stats(theirs, dataclasses.replace(base, n_enumerated=9))
        assert _sidecar_diff_is_the_mode_census_residue(ours, theirs) is True
        garbage = tmp_path / "garbage.trials.json"
        garbage.write_bytes(b"{not json")
        assert _sidecar_diff_is_the_mode_census_residue(ours, garbage) is False
        write_tuple_trial_stats(theirs, dataclasses.replace(base, n_finite=1, sum_sharpe=0.5))
        assert _sidecar_diff_is_the_mode_census_residue(ours, theirs) is False


class TestModeCensusFieldsSingleSource:
    """MODE_CENSUS_FIELDS is the ONE cross-module owner (the VIX_FIELDS law)."""

    def test_constant_value(self) -> None:
        assert MODE_CENSUS_FIELDS == ("n_conflict_pruned", "n_enumerated", "n_minhits_dropped")

    def test_exported_in_all(self) -> None:
        from brutex.engine import trial_stats as trial_stats_mod

        assert "MODE_CENSUS_FIELDS" in trial_stats_mod.__all__

    def test_every_census_field_is_a_real_sidecar_field(self) -> None:
        # fields()-consistency: a renamed sidecar field can never silently
        # orphan the census constant (the gate would stop matching anything).
        field_names = {field.name for field in dataclasses.fields(TupleTrialStats)}
        assert set(MODE_CENSUS_FIELDS) <= field_names


class TestSweepOpsRouting:
    """Flag-ON routing parity for the recount post-diff verdict tail
    (DEC-RUST-SWEEP-OPS-001 slice 5).

    Wheel-free: the shared behavior-correct FAKE crate is injected by patching
    the facade's ``importlib`` binding. Flag ON must be behavior-identical to
    the flag-OFF Python law (which every OTHER recount test in this file
    drives) — the byte-diff, the run-identity seams
    (``canonical_trade_params``/``mask_run_id``), the finding codes,
    severities and detail BYTES all stay Python on both arms; only the pure
    post-diff classification tail routes.
    """

    @staticmethod
    def _arm_fake_crate(monkeypatch: pytest.MonkeyPatch) -> None:
        from types import SimpleNamespace

        from brutex.engine import sweep_ops_rust

        from tests.fakes.sweep_ops_crate import build_fake_sweep_ops_crate

        crate = build_fake_sweep_ops_crate(version="fake")
        monkeypatch.setattr(
            sweep_ops_rust,
            "importlib",
            SimpleNamespace(import_module=lambda name: crate),
        )
        monkeypatch.setenv(sweep_ops_rust.SWEEP_OPS_RUST_ENV, "1")
        sweep_ops_rust.reset_sweep_ops_rust_for_tests()

    @staticmethod
    def _reset_facade() -> None:
        from brutex.engine.sweep_ops_rust import reset_sweep_ops_rust_for_tests

        reset_sweep_ops_rust_for_tests()

    @staticmethod
    def _findings(report: TreeVerifyReport) -> list[tuple[str, str, str]]:
        return [(f.code, f.severity, f.detail) for f in report.findings]

    def test_flag_on_matches_flag_off_over_the_whole_ladder(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One tampered grid store + one tampered cell store, driven under
        every (context_match, cell_evidence) combination — finding triples
        (code, severity, detail bytes) identical ON vs OFF over the SAME
        trees (``compare_recount`` is read-only)."""
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
        # Tamper BOTH the grid store and the cell store in the original tree.
        grid_store.write_bytes(grid_store.read_bytes() + b"tampered")
        cell = original / cell_rel
        cell.write_bytes(cell.read_bytes() + b"tampered")

        combos = [(True, True), (True, False), (False, True), (False, False)]

        def _drive() -> list[list[tuple[str, str, str]]]:
            return [
                self._findings(
                    compare_recount(original, scratch, plan, context_match=cm, cell_evidence=ce)
                )
                for cm, ce in combos
            ]

        off = _drive()
        self._arm_fake_crate(monkeypatch)
        try:
            on = _drive()
        finally:
            self._reset_facade()
        assert on == off
        # Anti-vacuous: the ladder genuinely branched — grid corrupt +
        # cell dependent under (True, *); inconclusive under (False, *).
        assert {c for c, _, _ in off[0]} == {
            "recount_divergence",
            "cell_recount_divergence_dependent",
        }
        assert "recount_divergence_inconclusive" in {c for c, _, _ in off[2]}
