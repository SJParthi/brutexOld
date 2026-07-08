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
    recorded_audit_facts,
    recorded_context_from_audit,
    recorded_roll_schedules_from_audit,
    recorded_stage3_from_audit,
    stale_roll_schedule_refusal,
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
                        "roll_schedule": list(schedule if schedule is not None else self._SCHEDULE)
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
        import brutex.orchestrator.audit as audit_mod

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
                    "params": {"roll_schedule": list(self._SCHEDULE)},
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
        import brutex.orchestrator.audit as audit_mod

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
        grid = _grid(instruments=["NSE-NIFTY-FUT"])
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
