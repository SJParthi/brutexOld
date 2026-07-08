# pyright: reportPrivateUsage=none
"""Tests for `brutex.nightly.restore` (DEC-SWEEP-NIGHTLY-AUTOMATION-001, R50 read side).

Verify-ALL-before-write (a byte flip → RestoreVerifyError naming the relpath
with the destination tree UNTOUCHED), the deltas-clear law (manifest-
extraneous destination deltas removed; manifest deltas copied; count
returned), ledger-LAST + deltas-before-ledger ordering (recording copy_file),
partial-snapshot + junk-manifest refusals, post-copy re-hash failure,
idempotent re-runs after a mid-restore copy failure, and the legacy
live-audit-chain skip belt (a pre-round-5 snapshot's nightly/audit/ entry is
never copied back over the writer's open inode).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from brutex.nightly.backup import MANIFEST_NAME, PARTIAL_SUFFIX, create_snapshot
from brutex.nightly.restore import (
    RestoreRefused,
    RestoreVerifyError,
    plan_restore,
    restore_snapshot,
    verify_snapshot,
)


def _tree(root: Path) -> dict[str, bytes]:
    """Full byte snapshot of every file under *root* (the mutation oracle)."""
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def _fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Build a small lake+results, snapshot it, return (lake, results, snapshot)."""
    lake = tmp_path / "lake"
    results = lake / "sweeps"
    (lake / "window_ledger" / "deltas").mkdir(parents=True)
    (lake / "window_ledger" / "ledger.parquet").write_bytes(b"LEDGER-BASE")
    (lake / "window_ledger" / "deltas" / "d1.parquet").write_bytes(b"DELTA-1")
    (lake / "registry.duckdb").write_bytes(b"REGISTRY")
    (lake / "instruments.duckdb").write_bytes(b"INSTRUMENTS")
    (results / "NIFTY" / "5minute").mkdir(parents=True)
    (results / "NIFTY" / "5minute" / "BUY_k1_x.parquet").write_bytes(b"TUPLE")
    (results / "audit").mkdir()
    (results / "audit" / "sweep-all.jsonl").write_bytes(b"CHAIN")
    result = create_snapshot(
        lake_root=lake,
        results_root=results,
        backup_root=tmp_path / "bk",
        snapshot_id="snap-1",
        nightly_id=None,
    )
    return lake, results, result.snapshot_path


# ── verify_snapshot ──────────────────────────────────────────────────────


def test_verify_snapshot_clean(tmp_path: Path) -> None:
    _lake, _results, snapshot = _fixture(tmp_path)
    verdict = verify_snapshot(snapshot)
    assert verdict.ok is True
    assert verdict.checked == 6
    assert verdict.mismatches == ()
    assert verdict.manifest is not None
    assert verdict.manifest.snapshot_id == "snap-1"


def test_verify_snapshot_names_flipped_missing_and_resized(tmp_path: Path) -> None:
    _lake, _results, snapshot = _fixture(tmp_path)
    # sha mismatch (same size, different bytes).
    (snapshot / "lake" / "registry.duckdb").write_bytes(b"REGISTRZ")
    # size mismatch.
    (snapshot / "lake" / "instruments.duckdb").write_bytes(b"SHORT")
    # missing file.
    (snapshot / "results" / "audit" / "sweep-all.jsonl").unlink()
    verdict = verify_snapshot(snapshot)
    assert verdict.ok is False
    assert "lake/registry.duckdb (sha256)" in verdict.mismatches
    assert "lake/instruments.duckdb (size)" in verdict.mismatches
    assert "results/audit/sweep-all.jsonl (missing)" in verdict.mismatches


def test_verify_refuses_partial_dir(tmp_path: Path) -> None:
    partial = tmp_path / f"snap{PARTIAL_SUFFIX}"
    partial.mkdir()
    with pytest.raises(RestoreRefused, match="UNSEALED .partial"):
        verify_snapshot(partial)


def test_verify_refuses_missing_junk_and_nonobject_manifest(tmp_path: Path) -> None:
    snap = tmp_path / "snap"
    snap.mkdir()
    with pytest.raises(RestoreRefused, match="cannot read snapshot manifest"):
        verify_snapshot(snap)
    (snap / MANIFEST_NAME).write_text("{truncated", encoding="utf-8")
    with pytest.raises(RestoreRefused, match="is not JSON"):
        verify_snapshot(snap)
    (snap / MANIFEST_NAME).write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(RestoreRefused, match="must be a JSON object"):
        verify_snapshot(snap)
    (snap / MANIFEST_NAME).write_text('{"schema_version": 99}', encoding="utf-8")
    with pytest.raises(RestoreRefused, match="is invalid"):
        verify_snapshot(snap)


# ── plan_restore ─────────────────────────────────────────────────────────


def test_plan_orders_deltas_before_ledger_last(tmp_path: Path) -> None:
    lake, results, snapshot = _fixture(tmp_path)
    plan = plan_restore(snapshot, lake_root=lake, results_root=results)
    keys = [f"{e.namespace}/{e.relpath}" for e in plan.items]
    assert keys[-1] == "lake/window_ledger/ledger.parquet"  # STRICTLY last
    assert keys[-2] == "lake/window_ledger/deltas/d1.parquet"  # deltas precede
    assert all(e.restore_order == 0 for e in plan.items[:-2])
    assert plan.lake_root == lake
    assert plan.results_root == results


# ── restore_snapshot ─────────────────────────────────────────────────────


def test_restore_round_trip_byte_identical(tmp_path: Path) -> None:
    lake, results, snapshot = _fixture(tmp_path)
    before = _tree(lake)
    # Mutilate + delete lake state.
    (lake / "window_ledger" / "ledger.parquet").write_bytes(b"POISONED")
    (lake / "registry.duckdb").unlink()
    (results / "NIFTY" / "5minute" / "BUY_k1_x.parquet").unlink()
    plan = plan_restore(snapshot, lake_root=lake, results_root=results)
    outcome = restore_snapshot(plan)
    assert outcome.restored == 6
    assert outcome.total_bytes == sum(e.size_bytes for e in plan.items)
    assert outcome.ledger_restored_last is True
    assert _tree(lake) == before  # byte-identical recovery


def test_restore_clears_extraneous_deltas_and_counts(tmp_path: Path) -> None:
    lake, results, snapshot = _fixture(tmp_path)
    # A POST-snapshot delta — would fold latest-wins over the restored base.
    (lake / "window_ledger" / "deltas" / "d9-post.parquet").write_bytes(b"POST")
    # A non-parquet bystander must survive the surgical clear.
    (lake / "window_ledger" / "deltas" / "notes.txt").write_bytes(b"KEEP")
    plan = plan_restore(snapshot, lake_root=lake, results_root=results)
    outcome = restore_snapshot(plan)
    assert outcome.deltas_cleared == 1
    assert not (lake / "window_ledger" / "deltas" / "d9-post.parquet").exists()
    assert (lake / "window_ledger" / "deltas" / "d1.parquet").read_bytes() == b"DELTA-1"
    assert (lake / "window_ledger" / "deltas" / "notes.txt").exists()


def test_restore_copy_order_deltas_clear_then_deltas_then_ledger(tmp_path: Path) -> None:
    lake, results, snapshot = _fixture(tmp_path)
    (lake / "window_ledger" / "deltas" / "extraneous.parquet").write_bytes(b"X")
    order: list[str] = []

    def recording_copy(source: Path, dest: Path) -> None:
        rel = source.relative_to(snapshot).as_posix()
        order.append(rel)
        # The extraneous delta must be GONE before the first delta copy.
        if "deltas" in rel:
            assert not (lake / "window_ledger" / "deltas" / "extraneous.parquet").exists()
        dest.write_bytes(source.read_bytes())

    plan = plan_restore(snapshot, lake_root=lake, results_root=results)
    restore_snapshot(plan, copy_file=recording_copy)
    assert order[-1] == "lake/window_ledger/ledger.parquet"
    assert order[-2] == "lake/window_ledger/deltas/d1.parquet"


def test_restore_verify_failure_leaves_destination_untouched(tmp_path: Path) -> None:
    lake, results, snapshot = _fixture(tmp_path)
    # Flip ONE snapshot byte post-seal.
    target = snapshot / "lake" / "registry.duckdb"
    raw = bytearray(target.read_bytes())
    raw[0] ^= 0xFF
    target.write_bytes(bytes(raw))
    # Mutate the live lake so any write would be visible.
    (lake / "registry.duckdb").write_bytes(b"LIVE-STATE")
    before = _tree(lake)
    plan = plan_restore(snapshot, lake_root=lake, results_root=results)
    with pytest.raises(RestoreVerifyError) as excinfo:
        restore_snapshot(plan)
    assert "lake/registry.duckdb (sha256)" in excinfo.value.mismatches
    assert "nothing restored" in str(excinfo.value)
    assert _tree(lake) == before  # destination byte-untouched


def test_restore_verify_error_caps_named_mismatches(tmp_path: Path) -> None:
    lake = tmp_path / "lake"
    results = tmp_path / "results"
    (lake / "audit").mkdir(parents=True)
    for i in range(12):
        (lake / "audit" / f"a{i:02d}.jsonl").write_bytes(b"x")
    results.mkdir()
    snapshot = create_snapshot(
        lake_root=lake,
        results_root=results,
        backup_root=tmp_path / "bk",
        snapshot_id="s",
        nightly_id=None,
    ).snapshot_path
    for file in (snapshot / "lake" / "audit").iterdir():
        file.write_bytes(b"flipped!")
    plan = plan_restore(snapshot, lake_root=lake, results_root=results)
    with pytest.raises(RestoreVerifyError) as excinfo:
        restore_snapshot(plan)
    assert "12 file(s)" in str(excinfo.value)
    assert "(+2 more)" in str(excinfo.value)


def test_restore_unknown_namespace_refused(tmp_path: Path) -> None:
    lake, results, snapshot = _fixture(tmp_path)
    data = json.loads((snapshot / MANIFEST_NAME).read_text(encoding="utf-8"))
    data["entries"] = [dict(data["entries"][0]) | {"namespace": "alien"}]
    # Re-point the entry at real snapshot bytes so Phase A passes: copy the
    # referenced file under the alien namespace.
    entry = data["entries"][0]
    src = snapshot / "lake" / entry["relpath"]
    dest = snapshot / "alien" / entry["relpath"]
    dest.parent.mkdir(parents=True)
    dest.write_bytes(src.read_bytes())
    (snapshot / MANIFEST_NAME).write_text(json.dumps(data), encoding="utf-8")
    plan = plan_restore(snapshot, lake_root=lake, results_root=results)
    with pytest.raises(RestoreRefused, match="unknown namespace"):
        restore_snapshot(plan)


def test_restore_post_copy_rehash_failure(tmp_path: Path) -> None:
    """A copier that lands WRONG destination bytes is caught by Phase D."""
    lake, results, snapshot = _fixture(tmp_path)

    def corrupting_copy(source: Path, dest: Path) -> None:
        raw = source.read_bytes()
        if source.name == "registry.duckdb":
            raw = b"WRONG BYTES"
        dest.write_bytes(raw)

    plan = plan_restore(snapshot, lake_root=lake, results_root=results)
    with pytest.raises(RestoreVerifyError, match="post-restore re-hash FAILED"):
        restore_snapshot(plan, copy_file=corrupting_copy)


def test_restore_post_copy_missing_destination(tmp_path: Path) -> None:
    """A destination vanishing between copy and re-hash reads (missing)."""
    lake, results, snapshot = _fixture(tmp_path)

    def deleting_copy(source: Path, dest: Path) -> None:
        dest.write_bytes(source.read_bytes())

    plan = plan_restore(snapshot, lake_root=lake, results_root=results)

    # Sabotage: after the full copy pass, remove one destination file by
    # copying normally but deleting the FIRST restored file at the LAST copy.
    seen: list[Path] = []

    def tricky_copy(source: Path, dest: Path) -> None:
        deleting_copy(source, dest)
        seen.append(dest)
        if len(seen) == len(plan.items):
            first = plan.items[0]
            (lake if first.namespace == "lake" else results).joinpath(first.relpath).unlink()

    with pytest.raises(RestoreVerifyError) as excinfo:
        restore_snapshot(plan, copy_file=tricky_copy)
    assert any("(missing)" in m for m in excinfo.value.mismatches)


def test_restore_mid_copy_failure_then_rerun_converges(tmp_path: Path) -> None:
    lake, results, snapshot = _fixture(tmp_path)
    before = _tree(lake)
    (lake / "registry.duckdb").write_bytes(b"POISON")
    (lake / "window_ledger" / "ledger.parquet").write_bytes(b"POISON")
    plan = plan_restore(snapshot, lake_root=lake, results_root=results)
    calls = {"n": 0}

    def failing_copy(source: Path, dest: Path) -> None:
        calls["n"] += 1
        if calls["n"] == 3:
            raise OSError(5, "io error at item 3")
        dest.write_bytes(source.read_bytes())

    with pytest.raises(OSError, match="item 3"):
        restore_snapshot(plan, copy_file=failing_copy)
    # Mixed old/new state is the documented crash window; a RE-RUN converges.
    outcome = restore_snapshot(plan)
    assert outcome.restored == len(plan.items)
    assert _tree(lake) == before


def test_restore_without_ledger_family_never_touches_deltas(tmp_path: Path) -> None:
    """A snapshot with NO ledger state must not strip a live lake's deltas."""
    lake = tmp_path / "lake"
    results = tmp_path / "results"
    lake.mkdir()
    (lake / "registry.duckdb").write_bytes(b"R")
    results.mkdir()
    snapshot = create_snapshot(
        lake_root=lake,
        results_root=results,
        backup_root=tmp_path / "bk",
        snapshot_id="no-ledger",
        nightly_id=None,
    ).snapshot_path
    # A LIVE delta appears after the snapshot.
    (lake / "window_ledger" / "deltas").mkdir(parents=True)
    live_delta = lake / "window_ledger" / "deltas" / "live.parquet"
    live_delta.write_bytes(b"LIVE")
    plan = plan_restore(snapshot, lake_root=lake, results_root=results)
    outcome = restore_snapshot(plan)
    assert outcome.deltas_cleared == 0
    assert outcome.ledger_restored_last is False  # no ledger in the snapshot — honest
    assert live_delta.exists()


def test_restore_deltas_clear_with_missing_dest_dir(tmp_path: Path) -> None:
    """A destination without a deltas dir clears zero (no mkdir side effects)."""
    lake, results, snapshot = _fixture(tmp_path)
    import shutil

    shutil.rmtree(lake / "window_ledger")
    plan = plan_restore(snapshot, lake_root=lake, results_root=results)
    outcome = restore_snapshot(plan)
    assert outcome.deltas_cleared == 0
    assert (lake / "window_ledger" / "ledger.parquet").read_bytes() == b"LEDGER-BASE"


def test_restore_skips_legacy_live_audit_chain_entry(tmp_path: Path) -> None:
    """Regression (review round 5 MAJOR): a LEGACY snapshot carrying the shared
    cross-run ``results/nightly/audit/nightly.jsonl`` must NEVER copy it back —
    restore appends RESTORE_START/RESTORE_DONE to that very chain, and an
    ``os.replace`` over the writer's open inode would silently roll the
    forensic chain back to snapshot time and lose both records (Golden
    Rule #8)."""
    import structlog.testing

    from tests.fakes.nightly_snapshot import inject_legacy_audit_entry

    lake, results, snapshot = _fixture(tmp_path)
    key = inject_legacy_audit_entry(snapshot)
    # The DESTINATION live chain — post-snapshot state that must survive.
    live_audit = results / "nightly" / "audit" / "nightly.jsonl"
    live_audit.parent.mkdir(parents=True)
    live_audit.write_bytes(b"LIVE-CHAIN-WITH-POST-SNAPSHOT-ENTRIES\n")

    with structlog.testing.capture_logs() as logs:
        plan = plan_restore(snapshot, lake_root=lake, results_root=results)
    assert plan.skipped_live_audit == (key,)
    assert all(f"{e.namespace}/{e.relpath}" != key for e in plan.items)
    assert any(log["event"] == "nightly.restore.live_audit_skipped" for log in logs)

    outcome = restore_snapshot(plan)
    assert outcome.restored == len(plan.items) == 6  # the audit entry never copied
    # The live chain is byte-untouched (Phase C skipped it; Phase D never
    # re-hashed it against the snapshot copy).
    assert live_audit.read_bytes() == b"LIVE-CHAIN-WITH-POST-SNAPSHOT-ENTRIES\n"
