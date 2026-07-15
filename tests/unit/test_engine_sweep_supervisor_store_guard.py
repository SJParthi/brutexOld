"""Regression suite for the sequential resume STORE GUARD (DEC-SWEEP-RESUME-STORE-GUARD-001).

THE CONFIRMED S5b DEFECT: both supervisor skip tiers — checkpoint
(``index < resume_from``) and cache (``guards.cache.seen``) — fired before ANY
store touch, so deleting SOME tuple store Parquets (keeping
checkpoint/dedup-cache/sentinels) and re-running the SAME grid produced
``skipped_resume=N, tuples_run=0``, a clean exit, and the deleted finalists
PERMANENTLY missing (the operator acceptance scenario "massive pull → delete →
re-run must never silently lose data", proven by the external
``repro_5b.py`` / ``repro_5b_cacheonly.py`` reproducers this suite mirrors).

The tests drive the REAL :func:`run_supervised_grid` over tmp_path roots with
real stores/sentinels/sidecars/checkpoints/caches (the reproducers' exact call
shape — ``store_for=make_store_for(root, grid)`` and nothing else, so the
``TupleStoreProvider`` auto-activation is what engages the guard):

* T1/T2 — the checkpoint-tier and cache-tier recovery regressions
  (fail-before/pass-after: with the guard kill-switched OFF the same fixtures
  reproduce the loss, pinned in T10's falsy arms);
* T3 — the NEGATIVE CONTROL: an intact tree still skips byte-identically
  (zero re-runs, zero store opens, the resume audit payload byte-identical to
  pre-guard, chain verifies) — the gate must not defeat resume;
* T4 — the ZERO-FINALIST edge: a completed-empty tuple persists all THREE
  artifacts (schema-only 0-row Parquet + sidecar + sentinel), so it keeps
  SKIPPING on every resume — no per-resume re-run tax;
* T5/T6 — sentinel-only / sidecar-only deletions force a full byte-identical
  redo (plus the sentinel⟺sidecar co-write invariant that licenses the third
  stat probe);
* T7 — the DISCLOSED no-bars shift: a checkpointed no-bars tuple (zero
  artifacts by construction) re-probes each resume and re-settles as no-bars;
* T8/T9/T14/T16 — mid-prefix damage accounting, the both-tier matrix, the
  capped WARN discipline, and the failed-forced-tuple interaction.
"""

# pyright: reportPrivateUsage=false

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from brutex.data.models import CandleBar
from brutex.data.option_dedup import OptionDedupIndex
from brutex.engine.sweep_audit import SweepAuditWriter, read_entries, verify_chain
from brutex.engine.sweep_cache import SweepCache
from brutex.engine.sweep_checkpoint import SweepCheckpointStore
from brutex.engine.sweep_orchestrator import (
    SweepGridConfig,
    Timeframe,
    count_sweep_tuples,
    iter_sweep_tuples,
    make_store_for,
    tuple_store_path,
)
from brutex.engine.sweep_progress import SweepProgress
from brutex.engine.sweep_run import ComboDedup, SweepComboError
from brutex.engine.sweep_supervisor import (
    RESUME_STORE_GUARD_WARN_CAP,
    SweepGuards,
    grid_identity,
    run_supervised_grid,
)
from brutex.engine.sweep_watchdog import SweepWatchdog
from brutex.engine.trial_stats import trial_stats_sidecar_path
from brutex.engine.tuple_complete import completion_sentinel_path
from brutex.orchestrator.audit import AuditWriter
from brutex.strategies.types import Side
from structlog.testing import capture_logs

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Sequence

    from brutex.engine.results_store import ResultsStore
    from brutex.engine.sweep_orchestrator import SweepTuple
    from brutex.engine.sweep_supervisor import SupervisedGridSummary

pytest.importorskip("polars")

_IST = timezone(timedelta(hours=5, minutes=30))
GUARD_ENV = "BRUTEX_SWEEP_RESUME_STORE_GUARD"


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


def _make_bars(n_days: int, per_day: int) -> list[CandleBar]:
    # The sweep_run suite's worst-case-rankable recipe
    # (DEC-FILL-WORST-CASE-ONLY-001): mostly-up drift, every 5th day DOWN so
    # profit_factor stays finite; EOD-only exits (sl/tp None below) because a
    # tight SL/TP fills at the adverse extreme and gate-rejects every combo.
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


#: Module-shared immutable bar window (built once — the tests never mutate it).
_BARS: list[CandleBar] = _make_bars(45, 100)


def _grid(**overrides: object) -> SweepGridConfig:
    base: dict[str, object] = {
        "instruments": ["NIFTY"],
        "timeframes": [Timeframe("1minute", 1)],
        "directions": [Side.BUY],
        "k_values": [1, 2, 3],
        "condition_bits": [0, 1, 2, 3, 4],
        "stop_losses": [None],
        "take_profits": [None],
        "proximities": [None],
        "mode": "A",
        "data_snapshot_hash": "snap0001",
        "indicator_version": "ind-v1",
        "code_commit_sha": "deadbeef",
    }
    base.update(overrides)
    return SweepGridConfig(**base)  # type: ignore[arg-type]


def _dedup() -> ComboDedup:
    return ComboDedup(OptionDedupIndex(capacity=10_000, fpr_target=1e-6))


@contextmanager
def _open_guards(base: Path, grid: SweepGridConfig) -> Generator[SweepGuards, None, None]:
    """Open the five real G&A objects on RE-OPENABLE durable paths."""
    sup_path = base / "audit" / "sup.jsonl"
    sup_path.parent.mkdir(parents=True, exist_ok=True)
    audit = SweepAuditWriter.open(sup_path, resume=sup_path.exists())
    cache = SweepCache.open(base / "dedup-cache", bloom_capacity=10_000)
    checkpoint = SweepCheckpointStore(
        path=base / "checkpoint" / "sweep-all.json",
        sweep_id=grid_identity(grid),
        total_combos=count_sweep_tuples(grid),
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


def _run(
    base: Path,
    grid: SweepGridConfig,
    *,
    bars_for: Callable[[str, str], Sequence[CandleBar]] | None = None,
    store_for: Callable[[SweepTuple], ResultsStore] | None = None,
    **kwargs: Any,
) -> SupervisedGridSummary:
    """The reproducers' EXACT call shape — store_for=make_store_for(root, grid)."""
    ing = base / "audit" / "ingest.jsonl"
    ing.parent.mkdir(parents=True, exist_ok=True)
    with (
        _open_guards(base, grid) as guards,
        AuditWriter.open(ing, resume=ing.exists()) as ingest,
    ):
        return run_supervised_grid(
            grid,
            bars_for=bars_for if bars_for is not None else (lambda _i, _t: _BARS),
            store_for=store_for if store_for is not None else make_store_for(base, grid),
            dedup=_dedup(),
            ingest_audit=ingest,
            guards=guards,
            **kwargs,
        )


def _trio(store: Path) -> tuple[Path, Path, Path]:
    return store, completion_sentinel_path(store), trial_stats_sidecar_path(store)


def _snapshot(paths: list[Path]) -> dict[Path, bytes]:
    out: dict[Path, bytes] = {}
    for store in paths:
        for p in _trio(store):
            out[p] = p.read_bytes()
    return out


def _store_paths(base: Path, grid: SweepGridConfig) -> list[Path]:
    return [tuple_store_path(base, t, grid) for t in iter_sweep_tuples(grid)]


class TestCheckpointTier:
    def test_deleted_stores_are_regenerated_byte_identical(self, tmp_path: Path) -> None:
        """T1 — the in-tree twin of repro_5b.py: partial store deletion heals.

        Run 1 completes 3 tuples; store Parquets for tuples 0 and 2 are
        DELETED (checkpoint/cache/sentinels/sidecars KEPT); run 2 forces
        exactly those two back through run_one_tuple and regenerates their
        Parquets BYTE-IDENTICAL (the ``_sentinel_resume`` redo law); run 3
        then skips everything — the self-heal CONVERGES (no permanent re-run
        loop). Both audit chains verify end-to-end after the heal (T19).
        """
        grid = _grid()
        s1 = _run(tmp_path, grid)
        assert s1.tuples_run == 3
        assert s1.persisted > 0  # real finalists — deletions below LOSE data pre-fix
        paths = _store_paths(tmp_path, grid)
        snap = _snapshot(paths)
        for i in (0, 2):
            paths[i].unlink()
            assert completion_sentinel_path(paths[i]).is_file()  # sentinels kept

        s2 = _run(tmp_path, grid)
        assert s2.tuples_run == 2
        assert s2.skipped_resume == 1
        assert s2.store_guard_forced == 2
        assert s2.tuples_failed == 0
        for store in paths:
            for p in _trio(store):
                assert p.read_bytes() == snap[p]  # byte-identical regeneration

        s3 = _run(tmp_path, grid)
        assert s3.tuples_run == 0
        assert s3.skipped_resume == 3
        assert s3.store_guard_forced == 0
        assert verify_chain(tmp_path / "audit" / "sup.jsonl") > 0

    def test_full_trio_deletion_and_mid_prefix_damage(self, tmp_path: Path) -> None:
        """T8 — (a) a full-trio deletion regenerates all three byte-identical;
        (b) mid-prefix damage: only the damaged index runs, progress ticks ==
        total (no double advance), and the resume audit payload carries the
        additive ``store_guard_forced`` keys."""
        grid = _grid(stop_losses=[80, 90])  # 3 k-values × 2 sl = 6 tuples
        s1 = _run(tmp_path, grid)
        assert s1.tuples_run == 6
        paths = _store_paths(tmp_path, grid)
        snap = _snapshot(paths)
        for p in _trio(paths[2]):
            p.unlink()  # full-trio deletion at mid-prefix index 2

        ing = tmp_path / "audit" / "ingest.jsonl"
        with (
            _open_guards(tmp_path, grid) as guards,
            AuditWriter.open(ing, resume=True) as ingest,
        ):
            s2 = run_supervised_grid(
                grid,
                bars_for=lambda _i, _t: _BARS,
                store_for=make_store_for(tmp_path, grid),
                dedup=_dedup(),
                ingest_audit=ingest,
                guards=guards,
            )
            # Progress ticks reconcile to the full grid — the forced tuple
            # advanced ONCE on its run path, never batch-advanced too.
            snapshot = guards.progress.snapshot()
            assert snapshot.evaluated == 6
        assert s2.tuples_run == 1
        assert s2.skipped_resume == 5
        assert s2.store_guard_forced == 1
        for store in paths:
            for p in _trio(store):
                assert p.read_bytes() == snap[p]
        entries = read_entries(tmp_path / "audit" / "sup.jsonl")
        resumes = [e for e in entries if e.action == "TUPLE_SKIPPED_RESUME"]
        forced_line = resumes[-1]
        assert forced_line.payload["resumed_count"] == 5
        assert forced_line.payload["store_guard_forced"] == 1
        assert forced_line.payload["store_guard_forced_indices"] == [2]
        completes = [e for e in entries if e.action == "SUPERVISOR_COMPLETE"]
        assert "store_guard_forced" not in completes[0].payload  # run 1: emit-on-nonzero
        assert completes[-1].payload["store_guard_forced"] == 1
        assert verify_chain(tmp_path / "audit" / "sup.jsonl") == len(entries)


class TestCacheTier:
    def test_cache_tier_deleted_stores_are_regenerated(self, tmp_path: Path) -> None:
        """T2 — the in-tree twin of repro_5b_cacheonly.py: with the checkpoint
        file ALSO deleted (resume_from == 0), the cache tier is the only skip
        tier — the guard gates it independently."""
        grid = _grid()
        s1 = _run(tmp_path, grid)
        assert s1.tuples_run == 3
        paths = _store_paths(tmp_path, grid)
        snap = _snapshot(paths)
        for i in (0, 2):
            paths[i].unlink()
        (tmp_path / "checkpoint" / "sweep-all.json").unlink()

        s2 = _run(tmp_path, grid)
        assert s2.tuples_run == 2
        assert s2.skipped_cache == 1  # only the intact tuple
        assert s2.skipped_resume == 0
        assert s2.store_guard_forced == 2
        for store in paths:
            for p in _trio(store):
                assert p.read_bytes() == snap[p]

    @pytest.mark.parametrize("surviving", ["both", "cache_only", "checkpoint_only"])
    def test_both_tier_matrix_recovers(self, tmp_path: Path, surviving: str) -> None:
        """T9 — every durable-skip-state arm recovers the deleted store."""
        grid = _grid(k_values=[1, 2])
        s1 = _run(tmp_path, grid)
        assert s1.tuples_run == 2
        paths = _store_paths(tmp_path, grid)
        snap = _snapshot(paths)
        paths[0].unlink()
        if surviving == "cache_only":
            (tmp_path / "checkpoint" / "sweep-all.json").unlink()
        if surviving == "checkpoint_only":
            # A FRESH cache dir: only the checkpoint can skip.
            import shutil

            shutil.rmtree(tmp_path / "dedup-cache")
        s2 = _run(tmp_path, grid)
        assert s2.tuples_run == 1
        assert s2.store_guard_forced == 1
        assert paths[0].read_bytes() == snap[paths[0]]


class TestNegativeControl:
    def test_intact_tree_skips_byte_identically(self, tmp_path: Path) -> None:
        """T3 — the gate must not defeat resume: on a fully intact tree the
        re-run does ZERO work (no run_one_tuple, no store open), the store
        bytes are untouched, the resume audit payload is BYTE-IDENTICAL to
        pre-guard (no guard key at zero), and the chain verifies."""
        grid = _grid()
        s1 = _run(tmp_path, grid)
        assert s1.tuples_run == 3
        paths = _store_paths(tmp_path, grid)
        snap = _snapshot(paths)

        from brutex.engine.sweep_orchestrator import TupleStoreProvider

        provider = TupleStoreProvider(root=tmp_path, grid=grid)
        calls = {"store_for": 0, "run_one_tuple": 0}

        class _CountingProvider:
            """store_for shim that counts opens but keeps the store_path seam."""

            def store_path(self, tuple_: SweepTuple) -> Path:
                return provider.store_path(tuple_)

            def __call__(self, tuple_: SweepTuple) -> ResultsStore:
                calls["store_for"] += 1
                return provider(tuple_)

        def _no_run(*_args: object, **_kwargs: object) -> object:
            calls["run_one_tuple"] += 1
            raise AssertionError("run_one_tuple must not run on an intact resume")

        import brutex.engine.sweep_supervisor as sup_mod

        original = sup_mod.run_one_tuple
        try:
            sup_mod.run_one_tuple = _no_run  # type: ignore[assignment]
            s2 = _run(tmp_path, grid, store_for=_CountingProvider())
        finally:
            sup_mod.run_one_tuple = original  # type: ignore[assignment]

        assert s2.tuples_run == 0
        assert s2.skipped_resume == 3
        assert s2.store_guard_forced == 0
        assert calls == {"store_for": 0, "run_one_tuple": 0}
        for store in paths:
            for p in _trio(store):
                assert p.read_bytes() == snap[p]
        entries = read_entries(tmp_path / "audit" / "sup.jsonl")
        resume = [e for e in entries if e.action == "TUPLE_SKIPPED_RESUME"][-1]
        # BYTE-IDENTICAL pre-guard payload — no additive key at zero forced.
        assert resume.payload == {"resumed_count": 3, "resume_from": 3}
        complete = [e for e in entries if e.action == "SUPERVISOR_COMPLETE"][-1]
        assert "store_guard_forced" not in complete.payload
        assert verify_chain(tmp_path / "audit" / "sup.jsonl") == len(entries)


class TestZeroFinalistTuple:
    def test_completed_empty_tuple_keeps_skipping_then_heals_when_deleted(
        self, tmp_path: Path
    ) -> None:
        """T4 — the zero-row edge: a tuple with bars but ZERO ranked finalists
        persists ALL THREE artifacts (a schema-only 0-row Parquet included),
        so the predicate reads it INTACT and it SKIPS on every resume — no
        per-resume re-run tax. Deleting its (empty) Parquet then regenerates
        it byte-identically."""
        import polars as pl

        grid = _grid(k_values=[1], min_hits=10**6)  # no combo reaches min_hits
        s1 = _run(tmp_path, grid)
        assert s1.tuples_run == 1
        assert s1.persisted == 0  # a LEGAL completed outcome — not no-bars
        (store,) = _store_paths(tmp_path, grid)
        for p in _trio(store):
            assert p.is_file()  # the on-disk completed-empty shape
        assert len(pl.read_parquet(store)) == 0
        snap = _snapshot([store])

        for _ in range(2):  # double resume — never forced, never re-run
            s = _run(tmp_path, grid)
            assert s.tuples_run == 0
            assert s.skipped_resume == 1
            assert s.store_guard_forced == 0

        store.unlink()
        s4 = _run(tmp_path, grid)
        assert s4.tuples_run == 1
        assert s4.store_guard_forced == 1
        for p in _trio(store):
            assert p.read_bytes() == snap[p]


class TestSiblingArtifactDeletions:
    def test_sentinel_only_deleted_forces_full_redo(self, tmp_path: Path) -> None:
        """T5 — sentinel gone (store+sidecar kept): forced fall-through;
        ``_sentinel_resume`` returns None → full redo rewrites all three
        byte-identically (idempotent identical-row append); next resume skips."""
        grid = _grid(k_values=[1])
        _run(tmp_path, grid)
        (store,) = _store_paths(tmp_path, grid)
        snap = _snapshot([store])
        completion_sentinel_path(store).unlink()

        s2 = _run(tmp_path, grid)
        assert s2.tuples_run == 1
        assert s2.store_guard_forced == 1
        for p in _trio(store):
            assert p.read_bytes() == snap[p]
        s3 = _run(tmp_path, grid)
        assert s3.tuples_run == 0
        assert s3.store_guard_forced == 0

    def test_sidecar_only_deleted_forces_redo_and_cowrite_invariant(self, tmp_path: Path) -> None:
        """T6 — the 3rd probe's teeth + the invariant licensing it: after any
        completed digest-path run, sentinel-present ⟺ sidecar-present (both are
        written under the SAME ``trial_stats_digest is not None`` condition),
        so a missing sidecar is always a DELETION; the redo rewrites it
        byte-identically."""
        grid = _grid(k_values=[1, 2])
        _run(tmp_path, grid)
        paths = _store_paths(tmp_path, grid)
        for store in paths:  # the co-write invariant
            assert completion_sentinel_path(store).is_file()
            assert trial_stats_sidecar_path(store).is_file()
        snap = _snapshot(paths)
        trial_stats_sidecar_path(paths[1]).unlink()

        s2 = _run(tmp_path, grid)
        assert s2.tuples_run == 1
        assert s2.skipped_resume == 1
        assert s2.store_guard_forced == 1
        for store in paths:
            for p in _trio(store):
                assert p.read_bytes() == snap[p]
            assert completion_sentinel_path(store).is_file()
            assert trial_stats_sidecar_path(store).is_file()


class TestNoBarsPrefixTuple:
    def test_no_bars_tuple_reprobes_each_resume_guard_on(self, tmp_path: Path) -> None:
        """T7 — the DISCLOSED count shift: a checkpointed no-bars tuple has
        zero artifacts (run_one_tuple returns None BEFORE store_for is ever
        called), so under the guard it falls through on every resume and
        re-settles cheaply as no-bars — skipped_resume excludes it, no store
        is fabricated, the checkpoint never rewinds, the run exits clean."""
        grid = _grid(
            instruments=["NIFTY", "BANKNIFTY"],
            k_values=[1],
            unsafe_allow_out_of_scope_instruments=False,
        )

        def _bars_for(instrument: str, _t: str) -> Sequence[CandleBar]:
            return [] if instrument == "NIFTY" else _BARS

        s1 = _run(tmp_path, grid, bars_for=_bars_for)
        assert s1.tuples_run == 1
        assert s1.tuples_skipped_no_bars == 1
        # The no-bars tuple persisted NOTHING — checkpointed with zero artifacts.
        paths = _store_paths(tmp_path, grid)
        assert not paths[0].exists()

        s2 = _run(tmp_path, grid, bars_for=_bars_for)
        assert s2.tuples_skipped_no_bars == 1  # re-probed + re-settled no-bars
        assert s2.skipped_resume == 1  # only the artifact-carrying tuple
        assert s2.store_guard_forced == 1
        assert s2.tuples_run == 0
        assert not paths[0].exists()  # still nothing fabricated

    def test_no_bars_tuple_skips_as_resumed_guard_off(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """T7 (legacy arm) — with the kill switch OFF the no-bars tuple is
        resume-skipped exactly as pre-guard (both directions pinned)."""
        grid = _grid(
            instruments=["NIFTY", "BANKNIFTY"],
            k_values=[1],
        )

        def _bars_for(instrument: str, _t: str) -> Sequence[CandleBar]:
            return [] if instrument == "NIFTY" else _BARS

        s1 = _run(tmp_path, grid, bars_for=_bars_for)
        assert s1.tuples_skipped_no_bars == 1
        monkeypatch.setenv(GUARD_ENV, "0")
        s2 = _run(tmp_path, grid, bars_for=_bars_for)
        assert s2.skipped_resume == 2
        assert s2.tuples_skipped_no_bars == 0
        assert s2.store_guard_forced == 0


class TestKillSwitch:
    @pytest.mark.parametrize("raw", ["0", "false", "off"])
    def test_falsy_restores_prefix_loss_mode(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw: str
    ) -> None:
        """T10 — the honest OFF: deleted parquets STAY missing (the pre-fix
        behaviour, byte-identical — this doubles as the bench baseline arm)."""
        grid = _grid(k_values=[1, 2])
        _run(tmp_path, grid)
        paths = _store_paths(tmp_path, grid)
        paths[0].unlink()
        monkeypatch.setenv(GUARD_ENV, raw)
        s2 = _run(tmp_path, grid)
        assert s2.tuples_run == 0
        assert s2.skipped_resume == 2
        assert s2.store_guard_forced == 0
        assert not paths[0].exists()  # the S5b loss mode, knowingly restored

    @pytest.mark.parametrize("raw", [None, "1", "true", "yes", "on"])
    def test_truthy_or_unset_arms_the_guard(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw: str | None
    ) -> None:
        grid = _grid(k_values=[1])
        _run(tmp_path, grid)
        (store,) = _store_paths(tmp_path, grid)
        store.unlink()
        if raw is None:
            monkeypatch.delenv(GUARD_ENV, raising=False)
        else:
            monkeypatch.setenv(GUARD_ENV, raw)
        s2 = _run(tmp_path, grid)
        assert s2.tuples_run == 1
        assert s2.store_guard_forced == 1
        assert store.exists()

    def test_junk_fails_loud_before_any_tuple_work(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """T10 — junk raises ValueError naming var+value BEFORE progress/audit
        (no START line, no abort line — a clean pre-loop config error)."""
        grid = _grid(k_values=[1])
        monkeypatch.setenv(GUARD_ENV, "maybe")
        with pytest.raises(ValueError, match=GUARD_ENV):
            _run(tmp_path, grid)
        assert read_entries(tmp_path / "audit" / "sup.jsonl") == []


class TestActivationSeams:
    def test_bespoke_lambda_store_for_is_inert_and_logged(self, tmp_path: Path) -> None:
        """T11 — a plain-lambda store_for (no ``store_path`` seam, no kwarg)
        leaves the guard honestly INERT: byte-identical pre-guard behaviour
        (the deleted store stays missing) + one ``resume_store_guard_inert``
        INFO — back-compat for bespoke library callers, pinned as deliberate."""
        from brutex.engine.sweep_orchestrator import load_or_create_store

        grid = _grid(k_values=[1])
        bespoke = lambda t: load_or_create_store(tuple_store_path(tmp_path, t, grid))  # noqa: E731
        _run(tmp_path, grid, store_for=bespoke)
        (store,) = _store_paths(tmp_path, grid)
        store.unlink()
        with capture_logs() as logs:
            s2 = _run(tmp_path, grid, store_for=bespoke)
        assert s2.tuples_run == 0
        assert s2.skipped_resume == 1
        assert s2.store_guard_forced == 0
        assert not store.exists()
        inert = [x for x in logs if x["event"] == "sweep_supervisor.resume_store_guard_inert"]
        assert len(inert) == 1

    def test_explicit_store_path_for_kwarg_overrides_the_provider(self, tmp_path: Path) -> None:
        """T12 — the explicit kwarg wins over the duck-typed provider seam
        (a counting shim proves the kwarg resolver is the one consulted)."""
        grid = _grid(k_values=[1])
        _run(tmp_path, grid)
        (store,) = _store_paths(tmp_path, grid)
        store.unlink()
        kwarg_calls = {"n": 0}

        def _kwarg_path_for(tuple_: SweepTuple) -> Path:
            kwarg_calls["n"] += 1
            return tuple_store_path(tmp_path, tuple_, grid)

        s2 = _run(tmp_path, grid, store_path_for=_kwarg_path_for)
        assert s2.tuples_run == 1
        assert kwarg_calls["n"] > 0  # the kwarg resolver was consulted
        assert store.exists()


class TestWarnDiscipline:
    def test_warn_cap_with_suppressed_summary(self, tmp_path: Path) -> None:
        """T14 — 25 deleted tuples → exactly RESUME_STORE_GUARD_WARN_CAP (20)
        per-tuple WARNs naming index/label/path/missing/tier + ONE suppressed
        summary carrying the overflow; the COUNTER stays exact at 25."""
        grid = _grid(
            instruments=[f"I{i}" for i in range(25)],
            k_values=[1],
            unsafe_allow_out_of_scope_instruments=True,
        )
        small_bars = _make_bars(4, 60)
        s1 = _run(tmp_path, grid, bars_for=lambda _i, _t: small_bars)
        assert s1.tuples_run == 25
        paths = _store_paths(tmp_path, grid)
        for p in paths:
            p.unlink()
        with capture_logs() as logs:
            s2 = _run(tmp_path, grid, bars_for=lambda _i, _t: small_bars)
        assert s2.store_guard_forced == 25
        assert s2.tuples_run == 25
        warns = [x for x in logs if x["event"] == "sweep_supervisor.resume_store_guard_forced"]
        assert len(warns) == RESUME_STORE_GUARD_WARN_CAP
        first = warns[0]
        assert first["tier"] == "checkpoint"
        assert first["missing"] == ["store"]
        assert "store_path" in first and "tuple" in first and "index" in first
        summaries = [
            x for x in logs if x["event"] == "sweep_supervisor.resume_store_guard_forced_suppressed"
        ]
        assert len(summaries) == 1
        assert summaries[0]["suppressed"] == 25 - RESUME_STORE_GUARD_WARN_CAP
        assert summaries[0]["total_forced"] == 25

    def test_cache_tier_warn_carries_cache_tier_label(self, tmp_path: Path) -> None:
        grid = _grid(k_values=[1])
        _run(tmp_path, grid)
        (store,) = _store_paths(tmp_path, grid)
        store.unlink()
        (tmp_path / "checkpoint" / "sweep-all.json").unlink()
        with capture_logs() as logs:
            s2 = _run(tmp_path, grid)
        assert s2.store_guard_forced == 1
        warns = [x for x in logs if x["event"] == "sweep_supervisor.resume_store_guard_forced"]
        assert len(warns) == 1
        assert warns[0]["tier"] == "cache"


class TestFailedForcedTuple:
    def test_forced_tuple_that_fails_is_reforced_next_resume(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """T16 — the C5 lesson under the gate: a forced tuple that raises
        SweepComboError on its re-run counts tuples_failed, rewrites NOTHING,
        and is re-forced on the NEXT resume (which then heals)."""
        grid = _grid(k_values=[1, 2])
        _run(tmp_path, grid)
        paths = _store_paths(tmp_path, grid)
        snap = _snapshot(paths)
        paths[0].unlink()

        import brutex.engine.sweep_supervisor as sup_mod

        real_run_one = sup_mod.run_one_tuple

        def _maybe_raise(grid_: object, tuple_: object, **kwargs: object) -> object:
            if tuple_.k == 1:  # type: ignore[attr-defined]  # the forced tuple
                raise SweepComboError("combo 0x01 (run_id=dead) boom")
            return real_run_one(grid_, tuple_, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(sup_mod, "run_one_tuple", _maybe_raise)
        s2 = _run(tmp_path, grid)
        assert s2.tuples_failed == 1
        assert s2.store_guard_forced == 1
        assert not paths[0].exists()  # still missing — nothing half-written

        monkeypatch.setattr(sup_mod, "run_one_tuple", real_run_one)
        s3 = _run(tmp_path, grid)
        assert s3.tuples_run == 1
        assert s3.store_guard_forced == 1
        assert paths[0].read_bytes() == snap[paths[0]]
