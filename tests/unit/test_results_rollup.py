"""Unit battery for the persisted `/results` top-K rollup (DEC-RESULTS-TOPK-ROLLUP-001).

Drives :mod:`brutex.engine.results_rollup` to 100% line+branch: the sibling-root
path law, the import-time K ⊇ MAX_TOP_N guard, the DERIVED union-axis
enumeration + its ``RollupRefusalError`` teeth, the ``code_token`` invalidation
teeth (a ``MAX_TOP_N`` bump / a ``_preset_rank_record`` source edit moves the
token), the exact top-K union trim, the ``AuditCursor`` tail-only era resume
(torn tail / blank line / link mismatch / shrink → full-rescan degrade), the
stamp round-trip + every fail-closed parse arm, the V1-V5 serve-classification
ladder reason-for-reason, the write path (stamp-LAST + the sibling-root
revision-invariance pin), the fenced ``RollupMergeHook`` (seed-from-disk /
full-fold / catch-up / idempotent re-merge / conflict latch + ``conflict_suspect``
stamp / the KeyboardInterrupt carve-out), the ``LogGate`` LRU dedupe, the
verify-insulation pins, and the mutation-workflow wiring pin.
"""

# The teeth tests reach module-private helpers deliberately (house pattern).
# pyright: reportPrivateUsage=none

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("polars", reason="the rollup persists ResultsStore Parquet files")

import brutex.engine.results_rollup as rr
import structlog.testing
from brutex.dashboard import sweep_results_view as view
from brutex.engine.results_rollup import (
    AUDIT_RELPATH,
    CURSOR_START,
    ROLLUP_K,
    AuditCursor,
    AuditCursorMismatch,
    LogGate,
    RollupMergeHook,
    RollupRefusalError,
    RollupStamp,
    RollupVerdict,
    classify_rollup,
    code_token,
    rebuild_rollup,
    rebuild_rollup_from_scan_best_effort,
    rollup_parquet_path,
    rollup_root,
    rollup_stamp_path,
    snapshot_eras,
    snapshot_eras_with_cursor,
    trim_to_union,
    union_axes,
)
from brutex.engine.results_store import ResultRow, ResultsStore
from brutex.orchestrator.audit import AuditError, AuditWriter

# ────── shared fixtures ──────


def _run_id(seed: int) -> str:
    return f"{seed:064x}"


def _row(
    *,
    seed: int,
    instrument: str = "NIFTY",
    tf: str = "5m",
    direction: str = "bull",
    net_pnl_paisa: int = 100_00,
    sharpe: float = 1.0,
    max_drawdown_pct: float = 0.05,
    mask_bits: int = 1,
    total_trades: int = 10,
    vix_mean: float | None = None,
) -> ResultRow:
    """A valid :class:`ResultRow` with distinct-able values on the union axes."""
    return ResultRow(
        run_id=_run_id(seed),
        instrument=instrument,
        tf=tf,
        direction=direction,
        mode="A",
        mask_bits=mask_bits,
        net_pnl_paisa=net_pnl_paisa,
        gross_profit_paisa=200_00 + seed,
        gross_loss_paisa=100_00 + seed,
        max_drawdown_paisa=50_00 + seed,
        expectancy_paisa=10_00 + seed,
        total_trades=total_trades,
        winning_trades=6,
        losing_trades=4,
        sharpe_ratio=sharpe,
        sortino_ratio=1.2 + seed / 1000,
        calmar_ratio=0.9 + seed / 1000,
        profit_factor=2.0 + seed / 1000,
        win_rate=0.6,
        max_drawdown_pct=max_drawdown_pct,
        avg_entry_price=Decimal("100.00"),
        avg_exit_price=Decimal("101.00"),
        vix_mean=vix_mean,
    )


def _write_store(
    sweeps_root: Path,
    rows: list[ResultRow],
    *,
    instrument: str = "NIFTY",
    tf: str = "5m",
    direction: str = "bull",
    digest: str = "a" * 16,
) -> Path:
    dest = sweeps_root / instrument / tf / f"{direction}_k1_{digest}.parquet"
    dest.parent.mkdir(parents=True, exist_ok=True)
    store = ResultsStore(path=dest)
    store.extend(rows)
    store.flush()
    return dest


def _start_payload(*, snapshot: str, tf: str = "5m", k: int = 1) -> dict[str, object]:
    return {
        "instrument": "NIFTY",
        "tf": tf,
        "direction": "bull",
        "mode": "A",
        "k": k,
        "data_snapshot_hash": snapshot,
    }


def _write_audit(sweeps_root: Path, payloads: list[dict[str, object]]) -> Path:
    audit_path = sweeps_root.joinpath(*AUDIT_RELPATH)
    with AuditWriter.open(audit_path, resume=audit_path.exists()) as writer:
        for payload in payloads:
            writer.append(action="SWEEP_START", subject="sweep:test", payload=payload)
    return audit_path


def _rev(lake_root: Path) -> str:
    from brutex.dashboard.lake_revision import sweep_results_revision

    return sweep_results_revision(lake_root) or ""


@pytest.fixture
def sweeps_root(tmp_path: Path) -> Path:
    root = tmp_path / "sweeps"
    root.mkdir()
    return root


def _seed_tree(sweeps_root: Path) -> None:
    """Two stores + one SWEEP_START audit entry — the standard fixture tree."""
    _write_store(sweeps_root, [_row(seed=1, sharpe=2.0), _row(seed=2, sharpe=1.0)])
    _write_store(
        sweeps_root,
        [_row(seed=3, instrument="BANKNIFTY", sharpe=3.0)],
        instrument="BANKNIFTY",
        digest="b" * 16,
    )
    _write_audit(sweeps_root, [_start_payload(snapshot="snap-a")])


# ────── paths + drift pins ──────


class TestPaths:
    def test_rollup_root_is_a_sibling_of_the_sweeps_tree(self, tmp_path: Path) -> None:
        sweeps = tmp_path / "sweeps"
        assert rollup_root(sweeps) == tmp_path / "sweeps_rollup"
        assert rollup_parquet_path(sweeps) == tmp_path / "sweeps_rollup" / "topk.parquet"
        assert rollup_stamp_path(sweeps) == tmp_path / "sweeps_rollup" / "topk.stamp.json"

    def test_audit_relpath_matches_the_view_convention(self) -> None:
        """Drift pin: the rollup and the view agree on the ONE audit-chain path."""
        assert AUDIT_RELPATH == ("audit", "sweep-all.jsonl")
        assert view._AUDIT_RELPATH == AUDIT_RELPATH

    def test_store_glob_matches_the_view_discovery(self, sweeps_root: Path) -> None:
        """Drift pin: ``current_store_relpaths`` finds exactly the view's stores."""
        _seed_tree(sweeps_root)
        expected = sorted(
            p.relative_to(sweeps_root).as_posix() for p in view._iter_store_paths(sweeps_root)
        )
        assert list(rr.current_store_relpaths(sweeps_root)) == expected
        assert len(expected) == 2

    def test_current_store_relpaths_empty_when_root_missing(self, tmp_path: Path) -> None:
        assert rr.current_store_relpaths(tmp_path / "nope") == ()

    def test_rollup_artifact_never_appears_as_a_store(self, sweeps_root: Path) -> None:
        """Sibling-root law: the rollup Parquet is invisible to store discovery."""
        _seed_tree(sweeps_root)
        assert rebuild_rollup(sweeps_root) is True
        rels = rr.current_store_relpaths(sweeps_root)
        assert all("sweeps_rollup" not in rel and "topk" not in rel for rel in rels)


class TestImportGuard:
    def test_k_covers_max_top_n(self) -> None:
        assert ROLLUP_K >= view.MAX_TOP_N

    def test_guard_refuses_a_too_shallow_union(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(view, "MAX_TOP_N", ROLLUP_K + 1)
        with pytest.raises(RollupRefusalError, match="MAX_TOP_N"):
            rr._require_k_covers_max_top_n()

    def test_guard_passes_at_the_boundary(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(view, "MAX_TOP_N", ROLLUP_K)
        rr._require_k_covers_max_top_n()  # exactly-at-boundary is legal


# ────── the derived union axes + code token ──────


@pytest.fixture
def reset_derivation() -> Any:
    """Reset the axis/token memos around a test that perturbs their inputs."""
    rr._reset_derivation_caches()
    yield
    rr._reset_derivation_caches()


class TestUnionAxes:
    def test_axes_are_the_14_rankables_desc_plus_min_dd_asc(self) -> None:
        axes = union_axes()
        assert len(axes) == len(view.RANKABLE_COLUMNS) + 1
        by_field = {(a.field, a.descending) for a in axes}
        for col in view.RANKABLE_COLUMNS:
            assert (col, True) in by_field
        assert ("max_drawdown_pct", False) in by_field  # the min_dd_first primary

    def test_axes_are_memoised(self) -> None:
        assert union_axes() is union_axes()

    def test_probe_directions(self) -> None:
        # min_dd_first: a LOWER max_drawdown_pct ranks first → ascending axis.
        assert rr._probe_preset_axis_direction("min_dd_first", "max_drawdown_pct") is False
        # profit_factor preset: a HIGHER profit_factor ranks first → descending.
        assert rr._probe_preset_axis_direction("profit_factor", "profit_factor") is True
        # composite degenerates to run_id on /results rows — insensitive to
        # profit_factor perturbation.
        assert rr._probe_preset_axis_direction("composite", "profit_factor") is None

    def test_refusal_on_a_non_inert_non_persisted_axis(
        self, reset_derivation: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """De-inerting ``composite_score`` (not a ResultRow field) must refuse LOUDLY."""
        monkeypatch.setattr(view, "PRESET_INERT_AXES", ("max_consecutive_losses",))
        with pytest.raises(RollupRefusalError, match="composite_score"):
            union_axes()

    def test_lost_derivation_race_returns_the_winner(
        self, reset_derivation: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The double-checked memo: a concurrent winner's axes are adopted."""
        winner = (rr.UnionAxis(field="sharpe_ratio", descending=True),)
        real = rr._probe_preset_axis_direction

        def _racing(preset_name: str, field_name: str) -> bool | None:
            if not rr._UNION_AXES_MEMO:
                rr._UNION_AXES_MEMO.append(winner)  # the simulated racing thread
            return real(preset_name, field_name)

        monkeypatch.setattr(rr, "_probe_preset_axis_direction", _racing)
        assert union_axes() is winner


class TestCodeToken:
    def test_token_is_hex_and_memoised(self) -> None:
        token = code_token()
        assert len(token) == 64
        int(token, 16)
        assert code_token() == token

    def test_max_top_n_bump_moves_the_token(
        self, reset_derivation: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        baseline = code_token()
        rr._reset_derivation_caches()
        monkeypatch.setattr(view, "MAX_TOP_N", 400)
        assert code_token() != baseline

    def test_rank_record_projection_edit_moves_the_token(
        self, reset_derivation: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        baseline = code_token()
        rr._reset_derivation_caches()
        monkeypatch.setattr(view, "preset_rank_record_source", lambda: "def edited(): ...")
        assert code_token() != baseline

    def test_token_is_stable_across_resets(self, reset_derivation: None) -> None:
        first = code_token()
        rr._reset_derivation_caches()
        assert code_token() == first

    def test_lost_token_race_returns_the_winner(
        self, reset_derivation: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The double-checked memo: a concurrent winner's token is adopted."""
        real = view.preset_rank_record_source

        def _racing() -> str:
            if not rr._CODE_TOKEN_MEMO:
                rr._CODE_TOKEN_MEMO.append("f" * 64)  # the simulated racing thread
            return real()

        monkeypatch.setattr(view, "preset_rank_record_source", _racing)
        assert code_token() == "f" * 64


# ────── the trim ──────


class TestTrimToUnion:
    def test_small_set_returns_all_run_id_sorted(self) -> None:
        rows = [_row(seed=3), _row(seed=1), _row(seed=2)]
        kept = trim_to_union(rows, 10)
        assert [r.run_id for r in kept] == sorted(r.run_id for r in rows)

    def test_union_contains_every_axis_top_k_prefix(self) -> None:
        rows = [
            _row(seed=i, sharpe=float(i), net_pnl_paisa=1000 - i, max_drawdown_pct=i / 100)
            for i in range(1, 9)
        ]
        k = 2
        kept = trim_to_union(rows, k)
        kept_ids = {r.run_id for r in kept}
        base = sorted(rows, key=lambda r: r.run_id)
        # The run_id-ASC prefix rides unconditionally.
        assert {r.run_id for r in base[:k]} <= kept_ids
        for axis in union_axes():
            ordered = sorted(base, key=lambda r: getattr(r, axis.field), reverse=axis.descending)
            assert {r.run_id for r in ordered[:k]} <= kept_ids

    def test_top_n_over_union_equals_top_n_over_full(self) -> None:
        """The prefix property — the rollup's core correctness law."""
        rows = [
            _row(seed=i, sharpe=float(i % 5), net_pnl_paisa=10_000 - 7 * i) for i in range(1, 30)
        ]
        k = 5
        kept_list = list(trim_to_union(rows, k))
        for axis in union_axes():
            for pool in (rows, kept_list):
                pool.sort(key=lambda r: r.run_id)
                pool.sort(key=lambda r: getattr(r, axis.field), reverse=axis.descending)
            assert [r.run_id for r in rows[:k]] == [r.run_id for r in kept_list[:k]]


# ────── era scan + AuditCursor ──────


class TestSnapshotEras:
    def test_absent_chain_is_empty(self, sweeps_root: Path) -> None:
        assert snapshot_eras(sweeps_root) == {}
        assert snapshot_eras_with_cursor(sweeps_root) == ({}, CURSOR_START)

    def test_full_scan_matches_the_cursor_variant(self, sweeps_root: Path) -> None:
        path = _write_audit(
            sweeps_root,
            [_start_payload(snapshot="s1"), _start_payload(snapshot="s2", tf="15m")],
        )
        eras = snapshot_eras(sweeps_root)
        eras_c, cursor = snapshot_eras_with_cursor(sweeps_root)
        assert eras == eras_c
        assert eras[("NIFTY", "5m", "bull", "A", 1)] == frozenset({"s1"})
        assert cursor.offset == path.stat().st_size
        assert cursor.last_hash != CURSOR_START.last_hash

    def test_non_sweep_start_and_malformed_payloads_are_skipped(self, sweeps_root: Path) -> None:
        audit_path = sweeps_root.joinpath(*AUDIT_RELPATH)
        with AuditWriter.open(audit_path) as writer:
            writer.append(action="OTHER", subject="s", payload={"x": 1})
            writer.append(action="SWEEP_START", subject="s", payload={"instrument": "N"})
            writer.append(action="SWEEP_START", subject="s", payload=_start_payload(snapshot="ok"))
        eras = snapshot_eras(sweeps_root)
        assert eras == {("NIFTY", "5m", "bull", "A", 1): frozenset({"ok"})}

    def test_committed_corruption_degrades_to_empty(self, sweeps_root: Path) -> None:
        path = _write_audit(sweeps_root, [_start_payload(snapshot="s1")])
        with open(path, "ab") as f:
            f.write(b"not json\n")
        with structlog.testing.capture_logs() as logs:
            assert snapshot_eras(sweeps_root) == {}
            eras, cursor = snapshot_eras_with_cursor(sweeps_root)
        assert eras == {}
        assert cursor == CURSOR_START
        assert sum(1 for e in logs if e["event"] == "sweep_results.era_scan_failed") == 2

    def test_view_snapshot_eras_delegates_here(self, sweeps_root: Path) -> None:
        """§11 COMMON pin: the view's era scan is THIS module's function."""
        _write_audit(sweeps_root, [_start_payload(snapshot="s1")])
        assert view._snapshot_eras(sweeps_root) == snapshot_eras(sweeps_root)
        source = Path(view.__file__).read_text(encoding="utf-8")
        assert "from brutex.engine.results_rollup import snapshot_eras" in source


class TestAuditCursor:
    def test_resume_reads_only_the_tail(self, sweeps_root: Path) -> None:
        _write_audit(sweeps_root, [_start_payload(snapshot="s1")])
        _, cursor1 = snapshot_eras_with_cursor(sweeps_root)
        path = _write_audit(sweeps_root, [_start_payload(snapshot="s2")])
        tail = list(rr._iter_entries_with_cursor(path, cursor1))
        assert len(tail) == 1
        entry, cursor2 = tail[0]
        assert entry.payload["data_snapshot_hash"] == "s2"
        assert cursor2.offset == path.stat().st_size

    def test_link_mismatch_raises(self, sweeps_root: Path) -> None:
        path = _write_audit(
            sweeps_root, [_start_payload(snapshot="s1"), _start_payload(snapshot="s2")]
        )
        _, end = snapshot_eras_with_cursor(sweeps_root)
        first = next(rr._iter_entries_with_cursor(path, CURSOR_START))
        bad = AuditCursor(offset=first[1].offset, last_hash="0" * 64)
        assert bad.last_hash != first[1].last_hash  # genesis ≠ entry-1 hash
        with pytest.raises(AuditCursorMismatch, match="does not link"):
            list(rr._iter_entries_with_cursor(path, bad))
        assert end.offset == path.stat().st_size

    def test_torn_tail_is_stopped_at_not_consumed(self, sweeps_root: Path) -> None:
        path = _write_audit(sweeps_root, [_start_payload(snapshot="s1")])
        committed = path.stat().st_size
        with open(path, "ab") as f:
            f.write(b'{"torn')  # no newline — the uncommitted tail
        entries = list(rr._iter_entries_with_cursor(path, CURSOR_START))
        assert len(entries) == 1
        assert entries[0][1].offset == committed

    def test_blank_committed_line_is_skipped(self, sweeps_root: Path) -> None:
        path = _write_audit(
            sweeps_root, [_start_payload(snapshot="s1"), _start_payload(snapshot="s2")]
        )
        line1, line2 = path.read_bytes().splitlines(keepends=True)
        path.write_bytes(line1 + b"\n" + line2)
        entries = list(rr._iter_entries_with_cursor(path, CURSOR_START))
        assert [e.payload["data_snapshot_hash"] for e, _ in entries] == ["s1", "s2"]

    def test_undecodable_committed_line_raises_audit_error(self, sweeps_root: Path) -> None:
        path = _write_audit(sweeps_root, [_start_payload(snapshot="s1")])
        with open(path, "ab") as f:
            f.write(b"\xff\xfe\n")
        with pytest.raises(AuditError, match="undecodable"):
            list(rr._iter_entries_with_cursor(path, CURSOR_START))


# ────── the stamp ──────


def _stamp_payload(sweeps_root: Path) -> dict[str, Any]:
    return json.loads(rollup_stamp_path(sweeps_root).read_bytes())


def _rewrite_stamp(sweeps_root: Path, payload: dict[str, Any]) -> None:
    rollup_stamp_path(sweeps_root).write_text(rr._dumps(payload) + "\n", encoding="utf-8")


class TestStampParse:
    def test_round_trip(self, sweeps_root: Path) -> None:
        _seed_tree(sweeps_root)
        assert rebuild_rollup(sweeps_root) is True
        raw = rollup_stamp_path(sweeps_root).read_bytes()
        stamp = rr._parse_stamp(raw)
        assert stamp is not None
        # Re-render → identical bytes (the ONE deterministic JSON law).
        assert (rr._dumps(stamp.to_payload()) + "\n").encode("utf-8") == raw
        assert stamp.eras[("NIFTY", "5m", "bull", "A", 1)] == frozenset({"snap-a"})

    @pytest.mark.parametrize(
        ("mutate", "note"),
        [
            (lambda p: b"not json", "invalid json"),
            (lambda p: b"[1,2]", "non-dict root"),
            (lambda p: rr._dumps({**p, "stage": "other"}).encode(), "wrong stage"),
            (
                lambda p: rr._dumps({k: v for k, v in p.items() if k != "aggregates"}).encode(),
                "missing aggregates",
            ),
            (lambda p: rr._dumps({**p, "aggregates": 3}).encode(), "aggregates non-dict"),
            (lambda p: rr._dumps({**p, "flags": []}).encode(), "flags non-dict"),
            (lambda p: rr._dumps({**p, "eras": {}}).encode(), "eras non-list"),
            (lambda p: rr._dumps({**p, "stores": []}).encode(), "stores non-dict"),
            (lambda p: rr._dumps({**p, "eras": [[1, 2]]}).encode(), "era wrong length"),
            (
                lambda p: rr._dumps({**p, "eras": [["a", "b", "c", "d", 1, "x"]]}).encode(),
                "era hashes non-list",
            ),
            (lambda p: rr._dumps({**p, "eras": ["x"]}).encode(), "era item non-list"),
            (lambda p: rr._dumps({**p, "schema_version": "junk"}).encode(), "int coercion"),
        ],
    )
    def test_fail_closed_arms(
        self,
        sweeps_root: Path,
        mutate: Any,
        note: str,
    ) -> None:
        _seed_tree(sweeps_root)
        rebuild_rollup(sweeps_root)
        payload = _stamp_payload(sweeps_root)
        assert rr._parse_stamp(mutate(payload)) is None, note


# ────── the V1-V5 classification ladder ──────


class TestClassifyRollup:
    def _fresh_tree(self, sweeps_root: Path) -> str:
        _seed_tree(sweeps_root)
        assert rebuild_rollup(sweeps_root) is True
        return _rev(sweeps_root.parent)

    def test_fresh(self, sweeps_root: Path) -> None:
        rev = self._fresh_tree(sweeps_root)
        verdict = classify_rollup(sweeps_root, current_rev=rev)
        assert verdict.outcome == "fresh"
        assert verdict.reason == "fresh"
        assert verdict.behind_stores == 0
        assert verdict.rows is not None and len(verdict.rows) == 3
        assert verdict.stamp is not None and verdict.stamp.total_rows_exact is True

    def test_fresh_never_restats_the_stores(
        self, sweeps_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Mutant-killer: the fresh arm trusts the rev + set (no O(|stamp|) re-stat)."""
        rev = self._fresh_tree(sweeps_root)
        calls: list[Path] = []
        real = rr._stat_token

        def _spy(path: Path) -> str:
            calls.append(path)
            return real(path)

        monkeypatch.setattr(rr, "_stat_token", _spy)
        assert classify_rollup(sweeps_root, current_rev=rev).outcome == "fresh"
        assert not any(p.suffix == ".parquet" and "sweeps_rollup" not in str(p) for p in calls)

    def test_absent(self, sweeps_root: Path) -> None:
        verdict = classify_rollup(sweeps_root, current_rev="r")
        assert (verdict.outcome, verdict.reason) == ("rebuild", "absent")
        assert verdict.rows is None and verdict.stamp is None

    def test_stamp_unreadable(self, sweeps_root: Path) -> None:
        self._fresh_tree(sweeps_root)
        stamp = rollup_stamp_path(sweeps_root)
        stamp.unlink()
        stamp.mkdir()  # read_bytes → IsADirectoryError (OSError, not FileNotFound)
        verdict = classify_rollup(sweeps_root, current_rev="r")
        assert verdict.reason == "stamp_unreadable"

    def test_stamp_invalid(self, sweeps_root: Path) -> None:
        self._fresh_tree(sweeps_root)
        rollup_stamp_path(sweeps_root).write_bytes(b"junk")
        assert classify_rollup(sweeps_root, current_rev="r").reason == "stamp_invalid"

    def test_schema_version_mismatch(self, sweeps_root: Path) -> None:
        self._fresh_tree(sweeps_root)
        payload = _stamp_payload(sweeps_root)
        payload["schema_version"] = rr.ROLLUP_SCHEMA_VERSION + 1
        _rewrite_stamp(sweeps_root, payload)
        assert classify_rollup(sweeps_root, current_rev="r").reason == "schema_version"

    def test_code_token_mismatch(self, sweeps_root: Path) -> None:
        self._fresh_tree(sweeps_root)
        payload = _stamp_payload(sweeps_root)
        payload["code_token"] = "0" * 64
        _rewrite_stamp(sweeps_root, payload)
        assert classify_rollup(sweeps_root, current_rev="r").reason == "code_token"

    def test_artifact_unreadable(self, sweeps_root: Path) -> None:
        self._fresh_tree(sweeps_root)
        rollup_parquet_path(sweeps_root).unlink()
        assert classify_rollup(sweeps_root, current_rev="r").reason == "artifact_unreadable"

    def test_content_hash_mismatch_reads_as_absent(self, sweeps_root: Path) -> None:
        """The stamp-LAST crash law: bytes newer than the stamp ⇒ rebuild."""
        self._fresh_tree(sweeps_root)
        with open(rollup_parquet_path(sweeps_root), "ab") as f:
            f.write(b"x")
        assert classify_rollup(sweeps_root, current_rev="r").reason == "content_hash"

    def test_artifact_undecodable(self, sweeps_root: Path) -> None:
        self._fresh_tree(sweeps_root)
        garbage = b"not a parquet file"
        rollup_parquet_path(sweeps_root).write_bytes(garbage)
        payload = _stamp_payload(sweeps_root)
        payload["content_hash"] = hashlib.sha256(garbage).hexdigest()
        _rewrite_stamp(sweeps_root, payload)
        assert classify_rollup(sweeps_root, current_rev="r").reason == "artifact_undecodable"

    def test_store_removed_rebuilds(self, sweeps_root: Path) -> None:
        """A removal is the ONE event that can promote a trimmed row — rebuild."""
        rev = self._fresh_tree(sweeps_root)
        removed = next(iter(rr.current_store_relpaths(sweeps_root)))
        (sweeps_root / removed).unlink()
        verdict = classify_rollup(sweeps_root, current_rev=rev)
        assert (verdict.outcome, verdict.reason) == ("rebuild", "store_removed")

    def test_additions_only_stale_serves_with_behind_count(self, sweeps_root: Path) -> None:
        self._fresh_tree(sweeps_root)
        _write_store(sweeps_root, [_row(seed=9, tf="15m")], tf="15m", digest="c" * 16)
        verdict = classify_rollup(sweeps_root, current_rev=_rev(sweeps_root.parent))
        assert (verdict.outcome, verdict.reason) == ("stale", "behind")
        assert verdict.behind_stores == 1
        assert verdict.rows is not None  # the LAST-VALID rollup still serves

    def test_revision_drift_stale_serves_at_zero_behind(self, sweeps_root: Path) -> None:
        """Set-equal + rev moved + tokens intact — the audit-append shape."""
        self._fresh_tree(sweeps_root)
        _write_audit(sweeps_root, [_start_payload(snapshot="snap-b")])
        verdict = classify_rollup(sweeps_root, current_rev=_rev(sweeps_root.parent))
        assert (verdict.outcome, verdict.reason) == ("stale", "revision_drift")
        assert verdict.behind_stores == 0

    def test_store_rewritten_rebuilds(self, sweeps_root: Path) -> None:
        """A rewrite must never stale-serve superseded rows (the matrix nuance)."""
        self._fresh_tree(sweeps_root)
        rel = next(iter(rr.current_store_relpaths(sweeps_root)))
        store = ResultsStore.load(sweeps_root / rel)
        store.append(_row(seed=77, sharpe=9.0))
        store.flush()
        verdict = classify_rollup(sweeps_root, current_rev=_rev(sweeps_root.parent))
        assert (verdict.outcome, verdict.reason) == ("rebuild", "store_rewritten")

    def test_store_stat_failure_rebuilds(
        self, sweeps_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._fresh_tree(sweeps_root)

        def _boom(path: Path) -> str:
            raise PermissionError(f"denied: {path}")

        monkeypatch.setattr(rr, "_stat_token", _boom)
        verdict = classify_rollup(sweeps_root, current_rev="moved-rev")
        assert (verdict.outcome, verdict.reason) == ("rebuild", "store_stat_failed")

    def test_conflict_suspect_stale_serves(self, sweeps_root: Path) -> None:
        rev = self._fresh_tree(sweeps_root)
        payload = _stamp_payload(sweeps_root)
        payload["flags"]["conflict_suspect"] = True
        _rewrite_stamp(sweeps_root, payload)
        verdict = classify_rollup(sweeps_root, current_rev=rev)
        assert (verdict.outcome, verdict.reason) == ("stale", "conflict_suspect")
        assert verdict.rows is not None


# ────── the write path + Hooks B/C ──────


class TestWriteAndRebuild:
    def test_rebuild_writes_parquet_then_stamp(self, sweeps_root: Path) -> None:
        _seed_tree(sweeps_root)
        assert rebuild_rollup(sweeps_root) is True
        parquet = rollup_parquet_path(sweeps_root)
        stamp = rr._parse_stamp(rollup_stamp_path(sweeps_root).read_bytes())
        assert stamp is not None
        assert stamp.content_hash == hashlib.sha256(parquet.read_bytes()).hexdigest()
        assert stamp.code_token == code_token()
        assert stamp.rev == _rev(sweeps_root.parent)
        assert stamp.total_rows == 3
        assert stamp.total_rows_exact is True
        assert stamp.store_count == 2
        assert stamp.conflict_suspect is False
        assert set(stamp.stores) == set(rr.current_store_relpaths(sweeps_root))
        chain = sweeps_root.joinpath(*AUDIT_RELPATH)
        assert stamp.audit_offset == chain.stat().st_size
        assert stamp.audit_chain_token == rr._stat_token(chain)
        assert stamp.instruments == ("BANKNIFTY", "NIFTY")
        assert stamp.snapshot_hashes == ("snap-a",)

    def test_zero_store_scan_writes_nothing(self, sweeps_root: Path) -> None:
        assert rebuild_rollup(sweeps_root) is False
        assert not rollup_root(sweeps_root).exists()

    def test_rollup_write_never_moves_the_sweeps_revision(self, sweeps_root: Path) -> None:
        """THE sibling-root pin: a rollup write cannot self-invalidate."""
        _seed_tree(sweeps_root)
        before = _rev(sweeps_root.parent)
        assert rebuild_rollup(sweeps_root) is True
        assert _rev(sweeps_root.parent) == before
        # And a REBUILD over the existing rollup is equally invisible.
        assert rebuild_rollup(sweeps_root) is True
        assert _rev(sweeps_root.parent) == before

    def test_rebuild_is_deterministic(self, sweeps_root: Path) -> None:
        _seed_tree(sweeps_root)
        rebuild_rollup(sweeps_root)
        first = rollup_parquet_path(sweeps_root).read_bytes()
        first_stamp = rr._parse_stamp(rollup_stamp_path(sweeps_root).read_bytes())
        rebuild_rollup(sweeps_root)
        assert rollup_parquet_path(sweeps_root).read_bytes() == first
        second_stamp = rr._parse_stamp(rollup_stamp_path(sweeps_root).read_bytes())
        assert first_stamp is not None and second_stamp is not None
        assert replace(first_stamp, produced_at_utc="") == replace(second_stamp, produced_at_utc="")

    def test_raced_away_store_is_omitted_from_the_stamp(
        self, sweeps_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A store deleted between the glob and its stat is honestly absent."""
        _seed_tree(sweeps_root)
        scan = view.load_sweep_scan_tree(sweeps_root)
        victim = next(rel for rel in rr.current_store_relpaths(sweeps_root) if "BANKNIFTY" in rel)
        real = rr._stat_token

        def _racing(path: Path) -> str:
            if path == sweeps_root / victim:
                return rr._ABSENT_TOKEN  # raced away mid-fold
            return real(path)

        monkeypatch.setattr(rr, "_stat_token", _racing)
        state = rr._state_from_scan(sweeps_root, scan)
        assert victim not in state.stores
        assert len(state.stores) == 1

    def test_current_rev_no_signal_stamps_empty(
        self, sweeps_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from brutex.dashboard import lake_revision

        monkeypatch.setattr(lake_revision, "sweep_results_revision", lambda _root: None)
        assert rr._current_rev(sweeps_root) == ""

    def test_best_effort_rebuild_success(self, sweeps_root: Path) -> None:
        _seed_tree(sweeps_root)
        scan = view.load_sweep_scan_tree(sweeps_root)
        rebuild_rollup_from_scan_best_effort(sweeps_root, scan)
        assert rollup_parquet_path(sweeps_root).exists()

    def test_best_effort_rebuild_failure_is_fenced(
        self, sweeps_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_tree(sweeps_root)
        scan = view.load_sweep_scan_tree(sweeps_root)

        def _boom(root: Path, s: Any) -> bool:
            raise OSError("disk gone")

        monkeypatch.setattr(rr, "rebuild_rollup_from_scan", _boom)
        with structlog.testing.capture_logs() as logs:
            rebuild_rollup_from_scan_best_effort(sweeps_root, scan)  # never raises
        assert any(e["event"] == "results_rollup.rebuild_failed" for e in logs)

    def test_best_effort_rebuild_carve_out_propagates(
        self, sweeps_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_tree(sweeps_root)
        scan = view.load_sweep_scan_tree(sweeps_root)

        def _interrupt(root: Path, s: Any) -> bool:
            raise KeyboardInterrupt

        monkeypatch.setattr(rr, "rebuild_rollup_from_scan", _interrupt)
        with pytest.raises(KeyboardInterrupt):
            rebuild_rollup_from_scan_best_effort(sweeps_root, scan)


# ────── Hook A: the fenced merge hook ──────


def _grid() -> Any:
    from brutex.engine.sweep_orchestrator import SweepGridConfig, Timeframe
    from brutex.strategies.types import Side

    return SweepGridConfig(
        instruments=["NIFTY"],
        timeframes=[Timeframe("1minute", 1)],
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
    )


class TestRollupMergeHook:
    def test_seed_full_fold_then_incremental_matches_rebuild(self, sweeps_root: Path) -> None:
        """Incremental Hook-A bytes == the Hook-B full-rebuild bytes."""
        _seed_tree(sweeps_root)
        new = _write_store(sweeps_root, [_row(seed=5, tf="15m")], tf="15m", digest="d" * 16)
        hook = RollupMergeHook(sweeps_root=sweeps_root, grid=_grid())
        with structlog.testing.capture_logs() as logs:
            hook.merge_paths([new])
        assert hook.enabled is True
        assert any(e["event"] == "results_rollup.seed_full_fold" for e in logs)
        incremental = rollup_parquet_path(sweeps_root).read_bytes()
        stamp = rr._parse_stamp(rollup_stamp_path(sweeps_root).read_bytes())
        assert stamp is not None
        assert stamp.total_rows == 4 and stamp.total_rows_exact is True
        assert rebuild_rollup(sweeps_root) is True
        assert rollup_parquet_path(sweeps_root).read_bytes() == incremental

    def test_seed_from_disk_skips_the_full_fold(
        self, sweeps_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_tree(sweeps_root)
        assert rebuild_rollup(sweeps_root) is True

        def _forbidden(root: Path) -> Any:
            raise AssertionError("full fold must not run on a valid intact rollup")

        monkeypatch.setattr(view, "load_sweep_scan_tree", _forbidden)
        new = _write_store(sweeps_root, [_row(seed=6, tf="15m")], tf="15m", digest="e" * 16)
        hook = RollupMergeHook(sweeps_root=sweeps_root, grid=_grid())
        hook.merge_paths([new])
        assert hook.enabled is True
        stamp = rr._parse_stamp(rollup_stamp_path(sweeps_root).read_bytes())
        assert stamp is not None and stamp.total_rows == 4

    def test_seed_from_disk_catches_up_unstamped_stores(self, sweeps_root: Path) -> None:
        _seed_tree(sweeps_root)
        rebuild_rollup(sweeps_root)
        # An out-of-band store lands AFTER the stamp (not merged directly).
        _write_store(sweeps_root, [_row(seed=7, tf="30m")], tf="30m", digest="f" * 16)
        new = _write_store(sweeps_root, [_row(seed=8, tf="15m")], tf="15m", digest="e" * 16)
        hook = RollupMergeHook(sweeps_root=sweeps_root, grid=_grid())
        hook.merge_paths([new])
        stamp = rr._parse_stamp(rollup_stamp_path(sweeps_root).read_bytes())
        assert stamp is not None
        assert stamp.total_rows == 5 and stamp.store_count == 4

    def test_seed_from_disk_unsound_after_rewrite_falls_back_to_full_fold(
        self, sweeps_root: Path
    ) -> None:
        _seed_tree(sweeps_root)
        rebuild_rollup(sweeps_root)
        rel = next(iter(rr.current_store_relpaths(sweeps_root)))
        store = ResultsStore.load(sweeps_root / rel)
        store.append(_row(seed=42, sharpe=8.0))
        store.flush()
        new = _write_store(sweeps_root, [_row(seed=9, tf="15m")], tf="15m", digest="e" * 16)
        hook = RollupMergeHook(sweeps_root=sweeps_root, grid=_grid())
        with structlog.testing.capture_logs() as logs:
            hook.merge_paths([new])
        assert any(e["event"] == "results_rollup.seed_full_fold" for e in logs)
        stamp = rr._parse_stamp(rollup_stamp_path(sweeps_root).read_bytes())
        assert stamp is not None and stamp.total_rows == 5

    def test_seed_from_disk_unsound_after_removal(self, sweeps_root: Path) -> None:
        _seed_tree(sweeps_root)
        rebuild_rollup(sweeps_root)
        removed = next(iter(rr.current_store_relpaths(sweeps_root)))
        (sweeps_root / removed).unlink()
        new = _write_store(sweeps_root, [_row(seed=9, tf="15m")], tf="15m", digest="e" * 16)
        hook = RollupMergeHook(sweeps_root=sweeps_root, grid=_grid())
        with structlog.testing.capture_logs() as logs:
            hook.merge_paths([new])
        assert any(e["event"] == "results_rollup.seed_full_fold" for e in logs)

    def test_idempotent_re_merge_is_a_no_op(self, sweeps_root: Path) -> None:
        _seed_tree(sweeps_root)
        new = _write_store(sweeps_root, [_row(seed=5, tf="15m")], tf="15m", digest="d" * 16)
        hook = RollupMergeHook(sweeps_root=sweeps_root, grid=_grid())
        hook.merge_paths([new])
        first = rr._parse_stamp(rollup_stamp_path(sweeps_root).read_bytes())
        hook.merge_paths([new])  # byte-unchanged store → the idempotent short-circuit
        second = rr._parse_stamp(rollup_stamp_path(sweeps_root).read_bytes())
        assert first is not None and second is not None
        assert second.total_rows == first.total_rows == 4
        assert second.total_rows_exact is True

    def test_known_store_reflush_is_re_merged_without_exactness_loss(
        self, sweeps_root: Path
    ) -> None:
        """A KNOWN store re-flushed with a new row re-merges; exact stays True."""
        _seed_tree(sweeps_root)
        new = _write_store(sweeps_root, [_row(seed=5, tf="15m")], tf="15m", digest="d" * 16)
        hook = RollupMergeHook(sweeps_root=sweeps_root, grid=_grid())
        hook.merge_paths([new])
        _write_store(
            sweeps_root,
            [_row(seed=5, tf="15m"), _row(seed=15, tf="15m")],
            tf="15m",
            digest="d" * 16,
        )
        hook.merge_paths([new])
        stamp = rr._parse_stamp(rollup_stamp_path(sweeps_root).read_bytes())
        assert stamp is not None
        assert stamp.total_rows == 5 and stamp.total_rows_exact is True

    def test_cross_store_identical_collision_flips_exact_off(self, sweeps_root: Path) -> None:
        """The documented §h conservative signal — never a fabricated exactness."""
        shared = _row(seed=1, sharpe=2.0)
        first = _write_store(sweeps_root, [shared])
        hook = RollupMergeHook(sweeps_root=sweeps_root, grid=_grid())
        hook.merge_paths([first])  # the seed fold sees ONLY the first store
        dup = _write_store(sweeps_root, [shared], tf="15m", digest="d" * 16)
        hook.merge_paths([dup])  # an UNKNOWN store colliding on a KEPT run_id
        assert hook.enabled is True  # identical rows are the append no-op branch
        stamp = rr._parse_stamp(rollup_stamp_path(sweeps_root).read_bytes())
        assert stamp is not None
        assert stamp.total_rows == 1
        assert stamp.total_rows_exact is False

    def test_vix_backfill_branch_merges_without_conflict(self, sweeps_root: Path) -> None:
        base = _row(seed=1, vix_mean=None)
        _write_store(sweeps_root, [base])
        hook = RollupMergeHook(sweeps_root=sweeps_root, grid=_grid())
        enriched = _write_store(
            sweeps_root, [replace(base, vix_mean=14.5)], tf="15m", digest="d" * 16
        )
        hook.merge_paths([enriched])
        assert hook.enabled is True
        rows = ResultsStore.load(rollup_parquet_path(sweeps_root)).rows()
        assert len(rows) == 1
        assert rows[0].vix_mean == 14.5  # the None→value backfill union

    def test_conflict_latches_the_fence_and_stamps_suspect(self, sweeps_root: Path) -> None:
        """BLOCKER-1: a cross-store conflict NEVER raises into the grid."""
        _write_store(sweeps_root, [_row(seed=1, net_pnl_paisa=100)])
        hook = RollupMergeHook(sweeps_root=sweeps_root, grid=_grid())
        first = _write_store(sweeps_root, [_row(seed=2)], tf="30m", digest="e" * 16)
        hook.merge_paths([first])  # a valid rollup lands first
        conflicting = _write_store(
            sweeps_root, [_row(seed=1, net_pnl_paisa=999)], tf="15m", digest="d" * 16
        )
        with structlog.testing.capture_logs() as logs:
            hook.merge_paths([conflicting])  # must NOT raise
        failures = [e for e in logs if e["event"] == "results_rollup.merge_failed"]
        assert len(failures) == 1
        assert failures[0]["kind"] == "conflict"
        assert hook.enabled is False
        stamp = rr._parse_stamp(rollup_stamp_path(sweeps_root).read_bytes())
        assert stamp is not None and stamp.conflict_suspect is True
        # The latched hook is a pure no-op thereafter (no second failure log).
        with structlog.testing.capture_logs() as logs2:
            hook.merge_paths([conflicting])
        assert not any(e["event"] == "results_rollup.merge_failed" for e in logs2)
        # /results then stale-serves with the conflict note — never a blackout.
        verdict = classify_rollup(sweeps_root, current_rev=_rev(sweeps_root.parent))
        assert (verdict.outcome, verdict.reason) == ("stale", "conflict_suspect")

    def test_missing_store_is_a_conflict_kind_failure(self, sweeps_root: Path) -> None:
        _seed_tree(sweeps_root)
        hook = RollupMergeHook(sweeps_root=sweeps_root, grid=_grid())
        with structlog.testing.capture_logs() as logs:
            hook.merge_paths([sweeps_root / "NIFTY" / "5m" / "missing.parquet"])
        failures = [e for e in logs if e["event"] == "results_rollup.merge_failed"]
        assert len(failures) == 1 and failures[0]["kind"] == "conflict"
        assert hook.enabled is False

    def test_generic_failure_kind_error(
        self, sweeps_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_tree(sweeps_root)
        new = _write_store(sweeps_root, [_row(seed=5, tf="15m")], tf="15m", digest="d" * 16)

        def _boom(root: Path, state: Any) -> None:
            raise OSError("flock timeout")

        monkeypatch.setattr(rr, "_write_state", _boom)
        hook = RollupMergeHook(sweeps_root=sweeps_root, grid=_grid())
        with structlog.testing.capture_logs() as logs:
            hook.merge_paths([new])
        failures = [e for e in logs if e["event"] == "results_rollup.merge_failed"]
        assert len(failures) == 1 and failures[0]["kind"] == "error"
        assert hook.enabled is False

    def test_carve_out_propagates_and_does_not_latch(
        self, sweeps_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_tree(sweeps_root)
        new = _write_store(sweeps_root, [_row(seed=5, tf="15m")], tf="15m", digest="d" * 16)

        def _interrupt(root: Path, state: Any) -> None:
            raise KeyboardInterrupt

        monkeypatch.setattr(rr, "_write_state", _interrupt)
        hook = RollupMergeHook(sweeps_root=sweeps_root, grid=_grid())
        with pytest.raises(KeyboardInterrupt):
            hook.merge_paths([new])
        assert hook.enabled is True  # the carve-out re-raises BEFORE the latch

    def test_merge_tuple_resolves_the_store_path(self, sweeps_root: Path) -> None:
        from brutex.engine.sweep_orchestrator import SweepTuple as Tuple_
        from brutex.engine.sweep_orchestrator import Timeframe, tuple_store_path
        from brutex.strategies.types import Side

        grid = _grid()
        tuple_ = Tuple_(
            instrument="NIFTY",
            tf=Timeframe("1minute", 1),
            direction=Side.BUY,
            k=1,
            sl_paisa=80,
            tp_paisa=50,
            proximity=None,
        )
        dest = tuple_store_path(sweeps_root, tuple_, grid)
        dest.parent.mkdir(parents=True, exist_ok=True)
        store = ResultsStore(path=dest)
        store.extend([_row(seed=11, tf="1minute")])
        store.flush()
        hook = RollupMergeHook(sweeps_root=sweeps_root, grid=grid)
        hook.merge_tuple(tuple_)
        assert hook.enabled is True
        rows = ResultsStore.load(rollup_parquet_path(sweeps_root)).rows()
        assert [r.run_id for r in rows] == [_run_id(11)]

    def test_mark_conflict_suspect_without_a_rollup_is_a_no_op(self, sweeps_root: Path) -> None:
        hook = RollupMergeHook(sweeps_root=sweeps_root, grid=_grid())
        hook._mark_conflict_suspect()  # loaded.stamp is None → return
        assert not rollup_root(sweeps_root).exists()

    def test_mark_conflict_suspect_write_failure_is_fenced(
        self, sweeps_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_tree(sweeps_root)
        rebuild_rollup(sweeps_root)

        def _boom(dest: Path, write: Any) -> None:
            raise OSError("enospc")

        monkeypatch.setattr(rr, "atomic_write_simple", _boom)
        hook = RollupMergeHook(sweeps_root=sweeps_root, grid=_grid())
        with structlog.testing.capture_logs() as logs:
            hook._mark_conflict_suspect()
        assert any(e["event"] == "results_rollup.conflict_stamp_failed" for e in logs)

    def test_mark_conflict_suspect_carve_out_propagates(
        self, sweeps_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _oom(root: Path) -> Any:
            raise MemoryError

        monkeypatch.setattr(rr, "_load_rollup", _oom)
        hook = RollupMergeHook(sweeps_root=sweeps_root, grid=_grid())
        with pytest.raises(MemoryError):
            hook._mark_conflict_suspect()


class TestMergeHookEraFold:
    def test_new_audit_entries_fold_via_the_tail_read_only(
        self, sweeps_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """MINOR-1 teeth: the incremental fold never re-runs the full era scan."""
        _seed_tree(sweeps_root)
        first = _write_store(sweeps_root, [_row(seed=5, tf="15m")], tf="15m", digest="d" * 16)
        hook = RollupMergeHook(sweeps_root=sweeps_root, grid=_grid())
        hook.merge_paths([first])  # seeds via the full fold (chain consumed once)
        _write_audit(sweeps_root, [_start_payload(snapshot="snap-tail")])
        # If the fold fell back to a FULL rescan it would see this stub's junk.
        monkeypatch.setattr(rr, "snapshot_eras_with_cursor", lambda _root: ({}, CURSOR_START))
        second = _write_store(sweeps_root, [_row(seed=6, tf="30m")], tf="30m", digest="e" * 16)
        hook.merge_paths([second])
        assert hook.enabled is True
        stamp = rr._parse_stamp(rollup_stamp_path(sweeps_root).read_bytes())
        assert stamp is not None
        assert stamp.eras[("NIFTY", "5m", "bull", "A", 1)] == frozenset({"snap-a", "snap-tail"})
        assert "snap-tail" in stamp.snapshot_hashes

    def test_unchanged_chain_skips_era_work(
        self, sweeps_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_tree(sweeps_root)
        first = _write_store(sweeps_root, [_row(seed=5, tf="15m")], tf="15m", digest="d" * 16)
        hook = RollupMergeHook(sweeps_root=sweeps_root, grid=_grid())
        hook.merge_paths([first])

        def _forbidden(path: Path, cursor: AuditCursor) -> Any:
            raise AssertionError("an unchanged chain must not be re-read")

        monkeypatch.setattr(rr, "_iter_entries_with_cursor", _forbidden)
        second = _write_store(sweeps_root, [_row(seed=6, tf="30m")], tf="30m", digest="e" * 16)
        hook.merge_paths([second])
        assert hook.enabled is True

    def test_shrunk_chain_degrades_to_the_full_rescan(self, sweeps_root: Path) -> None:
        _seed_tree(sweeps_root)
        _write_audit(sweeps_root, [_start_payload(snapshot="snap-extra")])
        first = _write_store(sweeps_root, [_row(seed=5, tf="15m")], tf="15m", digest="d" * 16)
        hook = RollupMergeHook(sweeps_root=sweeps_root, grid=_grid())
        hook.merge_paths([first])
        chain = sweeps_root.joinpath(*AUDIT_RELPATH)
        first_line = chain.read_bytes().splitlines(keepends=True)[0]
        chain.write_bytes(first_line)  # shrink below the stamped offset
        second = _write_store(sweeps_root, [_row(seed=6, tf="30m")], tf="30m", digest="e" * 16)
        with structlog.testing.capture_logs() as logs:
            hook.merge_paths([second])
        assert any(e["event"] == "results_rollup.era_tail_rescan" for e in logs)
        assert hook.enabled is True
        stamp = rr._parse_stamp(rollup_stamp_path(sweeps_root).read_bytes())
        assert stamp is not None
        assert stamp.eras[("NIFTY", "5m", "bull", "A", 1)] == frozenset({"snap-a"})

    def test_replaced_chain_degrades_to_the_full_rescan(self, sweeps_root: Path) -> None:
        """A same-or-longer FOREIGN chain fails linkage → warn + full rescan."""
        _seed_tree(sweeps_root)
        first = _write_store(sweeps_root, [_row(seed=5, tf="15m")], tf="15m", digest="d" * 16)
        hook = RollupMergeHook(sweeps_root=sweeps_root, grid=_grid())
        hook.merge_paths([first])
        chain = sweeps_root.joinpath(*AUDIT_RELPATH)
        chain.unlink()
        _write_audit(
            sweeps_root,
            [
                _start_payload(snapshot="fresh-1"),
                _start_payload(snapshot="fresh-2"),
                _start_payload(snapshot="fresh-3"),
            ],
        )
        second = _write_store(sweeps_root, [_row(seed=6, tf="30m")], tf="30m", digest="e" * 16)
        with structlog.testing.capture_logs() as logs:
            hook.merge_paths([second])
        assert any(e["event"] == "results_rollup.era_tail_rescan" for e in logs)
        stamp = rr._parse_stamp(rollup_stamp_path(sweeps_root).read_bytes())
        assert stamp is not None
        assert stamp.eras[("NIFTY", "5m", "bull", "A", 1)] == frozenset(
            {"fresh-1", "fresh-2", "fresh-3"}
        )

    def test_deleted_chain_resets_the_eras(self, sweeps_root: Path) -> None:
        _seed_tree(sweeps_root)
        first = _write_store(sweeps_root, [_row(seed=5, tf="15m")], tf="15m", digest="d" * 16)
        hook = RollupMergeHook(sweeps_root=sweeps_root, grid=_grid())
        hook.merge_paths([first])
        sweeps_root.joinpath(*AUDIT_RELPATH).unlink()
        second = _write_store(sweeps_root, [_row(seed=6, tf="30m")], tf="30m", digest="e" * 16)
        hook.merge_paths([second])
        stamp = rr._parse_stamp(rollup_stamp_path(sweeps_root).read_bytes())
        assert stamp is not None
        assert stamp.eras == {}
        assert stamp.audit_offset == 0


# ────── the fence through a REAL grid driver ──────


class TestFenceThroughRealGrid:
    def test_injected_merge_failure_never_aborts_the_grid(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """BLOCKER-1 regression: a mid-grid merge failure completes the grid
        with byte-identical sweep results and a latched, once-logged fence."""
        from datetime import UTC, datetime, timedelta, timezone

        from brutex.data.models import CandleBar
        from brutex.data.option_dedup import OptionDedupIndex
        from brutex.engine.sweep_orchestrator import make_store_for, run_sweep_grid
        from brutex.engine.sweep_run import ComboDedup

        ist = timezone(timedelta(hours=5, minutes=30))

        def _bar(day: int, minute: int, o: float, c: float) -> CandleBar:
            ts = (
                datetime(2026, 1, 5, 9, 16, tzinfo=ist) + timedelta(days=day, minutes=minute)
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

        bars: list[CandleBar] = []
        for d in range(3):
            price = 100.0
            for m in range(60):
                close = price + 0.6 if m % 3 else price - 0.3
                bars.append(_bar(d, m, price, close))
                price = close

        grid = _grid()

        def _run(root: Path, hook: RollupMergeHook | None) -> Any:
            with AuditWriter.open(root / "audit" / "sweep-all.jsonl") as audit:
                return run_sweep_grid(
                    grid,
                    bars_for=lambda _i, _t: bars,
                    store_for=make_store_for(root, grid),
                    dedup=ComboDedup(OptionDedupIndex(capacity=10_000, fpr_target=1e-6)),
                    audit=audit,
                    rollup_hook=hook.merge_tuple if hook is not None else None,
                )

        control_root = tmp_path / "control"
        control_root.mkdir()
        control = _run(control_root, None)
        assert control.persisted > 0

        poisoned_root = tmp_path / "poisoned"
        poisoned_root.mkdir()

        def _boom(root: Path, state: Any) -> None:
            raise OSError("injected mid-grid merge failure")

        monkeypatch.setattr(rr, "_write_state", _boom)
        hook = RollupMergeHook(sweeps_root=poisoned_root, grid=grid)
        with structlog.testing.capture_logs() as logs:
            poisoned = _run(poisoned_root, hook)
        failures = [e for e in logs if e["event"] == "results_rollup.merge_failed"]
        assert len(failures) == 1  # latched after the FIRST failure — once, ever
        assert hook.enabled is False
        # The grid COMPLETED with identical sweep results (store bytes equal).
        assert poisoned.persisted == control.persisted
        control_stores = sorted(
            p.relative_to(control_root) for p in control_root.rglob("*.parquet")
        )
        for rel in control_stores:
            assert (poisoned_root / rel).read_bytes() == (control_root / rel).read_bytes()

    def test_healthy_hook_through_the_real_grid_writes_the_rollup(self, tmp_path: Path) -> None:
        from datetime import UTC, datetime, timedelta, timezone

        from brutex.data.models import CandleBar
        from brutex.data.option_dedup import OptionDedupIndex
        from brutex.engine.sweep_orchestrator import make_store_for, run_sweep_grid
        from brutex.engine.sweep_run import ComboDedup

        ist = timezone(timedelta(hours=5, minutes=30))
        bars: list[CandleBar] = []
        for d in range(3):
            price = 100.0
            for m in range(60):
                close = price + 0.6 if m % 3 else price - 0.3
                ts = (
                    datetime(2026, 1, 5, 9, 16, tzinfo=ist) + timedelta(days=d, minutes=m)
                ).astimezone(UTC)
                bars.append(
                    CandleBar(
                        timestamp=ts,
                        open=Decimal(str(round(price, 2))),
                        high=Decimal(str(round(max(price, close) + 0.5, 2))),
                        low=Decimal(str(round(min(price, close) - 0.5, 2))),
                        close=Decimal(str(round(close, 2))),
                        volume=100,
                        open_interest=None,
                    )
                )
                price = close
        grid = _grid()
        root = tmp_path / "sweeps"
        root.mkdir()
        hook = RollupMergeHook(sweeps_root=root, grid=grid)
        with AuditWriter.open(root / "audit" / "sweep-all.jsonl") as audit:
            summary = run_sweep_grid(
                grid,
                bars_for=lambda _i, _t: bars,
                store_for=make_store_for(root, grid),
                dedup=ComboDedup(OptionDedupIndex(capacity=10_000, fpr_target=1e-6)),
                audit=audit,
                rollup_hook=hook.merge_tuple,
            )
        assert summary.persisted > 0
        assert hook.enabled is True
        rows = ResultsStore.load(rollup_parquet_path(root)).rows()
        assert len(rows) == summary.persisted


# ────── the CLI Hook-B reconcile fence ──────


class TestCliReconcile:
    def test_success_prints_reconciled(
        self, sweeps_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from brutex.cli import _run_results_rollup_reconcile

        _seed_tree(sweeps_root)
        assert _run_results_rollup_reconcile(out_root=sweeps_root) is True
        assert "reconciled" in capsys.readouterr().out
        assert rollup_parquet_path(sweeps_root).exists()

    def test_zero_store_prints_skipped(
        self, sweeps_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from brutex.cli import _run_results_rollup_reconcile

        assert _run_results_rollup_reconcile(out_root=sweeps_root) is False
        assert "skipped (no stores)" in capsys.readouterr().out

    def test_failure_is_fenced_and_loud(
        self, sweeps_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from brutex.cli import _run_results_rollup_reconcile

        def _boom(root: Path) -> bool:
            raise OSError("disk gone")

        with structlog.testing.capture_logs() as logs:
            assert _run_results_rollup_reconcile(out_root=sweeps_root, rebuild=_boom) is False
        assert "rollup reconcile FAILED" in capsys.readouterr().err
        assert any(e["event"] == "cli.results_rollup.failed" for e in logs)

    def test_carve_out_propagates(self, sweeps_root: Path) -> None:
        from brutex.cli import _run_results_rollup_reconcile

        def _interrupt(root: Path) -> bool:
            raise KeyboardInterrupt

        with pytest.raises(KeyboardInterrupt):
            _run_results_rollup_reconcile(out_root=sweeps_root, rebuild=_interrupt)


# ────── LogGate ──────


class TestLogGate:
    def test_once_per_key(self) -> None:
        gate = LogGate()
        assert gate.should_emit(("a", "b")) is True
        assert gate.should_emit(("a", "b")) is False
        assert gate.should_emit(("a", "c")) is True

    def test_lru_eviction_re_emits(self) -> None:
        gate = LogGate(capacity=2)
        assert gate.should_emit(("k1",)) is True
        assert gate.should_emit(("k2",)) is True
        assert gate.should_emit(("k1",)) is False  # refreshed (move_to_end)
        assert gate.should_emit(("k3",)) is True  # evicts k2 (the LRU)
        assert gate.should_emit(("k2",)) is True  # evicted → re-emits (evicts k1)
        assert gate.should_emit(("k3",)) is False  # still resident
        assert gate.should_emit(("k1",)) is True  # evicted by k2's re-entry

    def test_clear_degraded_reports_once(self) -> None:
        gate = LogGate()
        assert gate.clear_degraded() is False
        gate.should_emit(("x",))
        assert gate.clear_degraded() is True
        assert gate.clear_degraded() is False

    def test_capacity_validation(self) -> None:
        with pytest.raises(ValueError, match="capacity"):
            LogGate(capacity=0)


# ────── verify-insulation + workflow-wiring pins ──────


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


class TestVerifyInsulation:
    def test_sweep_verify_walks_the_sweeps_dir_never_the_rollup(self) -> None:
        """The verify root is the sweeps dir; the sibling rollup is invisible
        to its ``**/*.parquet`` walks (DEC-RESULTS-TOPK-ROLLUP-001 pin vs the
        ``sweep_verify.py`` glob sites)."""
        source = (_repo_root() / "brutex" / "engine" / "sweep_verify.py").read_text(
            encoding="utf-8"
        )
        assert 'glob("**/*.parquet")' in source
        assert "sweeps_rollup" not in source

    def test_rollup_files_live_outside_any_sweeps_recursive_glob(self, sweeps_root: Path) -> None:
        _seed_tree(sweeps_root)
        rebuild_rollup(sweeps_root)
        walked = {p.name for p in sweeps_root.glob("**/*.parquet")}
        assert "topk.parquet" not in walked


class TestMutationWorkflowWiring:
    def test_results_rollup_pair_is_wired(self) -> None:
        """The weekly mutation loop names this module + its killing suite
        (the #1614 ``TestMutationWorkflowWiring`` precedent)."""
        workflow = (_repo_root() / ".github" / "workflows" / "mutation.yml").read_text(
            encoding="utf-8"
        )
        assert "brutex/engine/results_rollup.py:tests/unit/test_results_rollup.py:" in workflow

    def test_ci_static_gate_is_wired(self) -> None:
        ci = (_repo_root() / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        assert "--cov=brutex.engine.results_rollup" in ci


# ────── misc verdict shape ──────


def test_rollup_verdict_is_frozen() -> None:
    verdict = RollupVerdict(outcome="fresh", reason="fresh", rows=(), stamp=None, behind_stores=0)
    with pytest.raises(AttributeError):
        verdict.reason = "x"  # type: ignore[misc]


def test_stamp_dataclass_holds_the_section_b_shape(sweeps_root: Path) -> None:
    _seed_tree(sweeps_root)
    rebuild_rollup(sweeps_root)
    payload = _stamp_payload(sweeps_root)
    assert payload["stage"] == rr.ROLLUP_STAGE
    assert payload["k"] == ROLLUP_K
    assert set(payload) == {
        "stage",
        "schema_version",
        "k",
        "content_hash",
        "produced_at_utc",
        "code_token",
        "rev",
        "audit_chain_token",
        "audit_offset",
        "audit_last_hash",
        "stores",
        "aggregates",
        "eras",
        "flags",
    }
    assert isinstance(RollupStamp, type)
