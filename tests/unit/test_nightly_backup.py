# pyright: reportPrivateUsage=none
"""Tests for `brutex.nightly.backup` (DEC-SWEEP-NIGHTLY-AUTOMATION-001, R50 write side).

Inventory inclusion/exclusion law (bars/.locks/.rate/.quarantine/progress/
current-nightly + the LIVE cross-run nightly/audit/ chain OUT; dedup-cache +
prior nightly dirs IN; lake/results
namespacing incl. a results root OUTSIDE the lake), absent-member notes,
manifest round-trip (hypothesis) + golden + junk→BackupManifestError,
hash-of-DESTINATION-bytes correctness, `.partial`-never-sealed on ANY injected
failure, the FULL pre-seal verify + its env escape, seal-rename atomicity
(tree oracle), and retention (newest N · protect · stale-partial GC · keep<1
refused).
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest
import structlog.testing
from brutex.nightly.backup import (
    BACKUP_SCHEMA_VERSION,
    MANIFEST_NAME,
    NIGHTLY_AUDIT_REL_PARTS,
    NIGHTLY_BACKUP_KEEP_ENV,
    NIGHTLY_BACKUP_ROOT_ENV,
    NIGHTLY_BACKUP_VERIFY_ENV,
    PARTIAL_SUFFIX,
    BackupEntry,
    BackupItem,
    BackupManifest,
    BackupManifestError,
    _manifest_created_utc,
    backup_inventory,
    create_snapshot,
    default_backup_root,
    estimate_backup_bytes,
    fsync_file,
    prune_snapshots,
    resolve_backup_keep,
    resolve_backup_verify,
    sha256_file,
    sweep_runs_sibling_root,
)
from brutex.nightly.probes import NightlyEnvError
from hypothesis import given
from hypothesis import strategies as st


def _tree(root: Path) -> set[str]:
    """Relative-path snapshot of every file under *root* (the mutation oracle)."""
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


def _make_lake(tmp_path: Path) -> tuple[Path, Path]:
    """A miniature lake + results tree exercising every inventory rule."""
    lake = tmp_path / "lake"
    results = lake / "sweeps"
    (lake / "window_ledger" / "deltas").mkdir(parents=True)
    (lake / "window_ledger" / "ledger.parquet").write_bytes(b"LEDGER")
    (lake / "window_ledger" / "deltas" / "d1.parquet").write_bytes(b"DELTA1")
    (lake / "registry.duckdb").write_bytes(b"REGISTRY")
    (lake / "instruments.duckdb").write_bytes(b"INSTRUMENTS")
    (lake / "catalog.duckdb").write_bytes(b"CATALOG")
    (lake / "audit").mkdir()
    (lake / "audit" / "retry-storm-x.jsonl").write_bytes(b"AUDIT")
    # By-law exclusions living under the lake root — must never be walked.
    (lake / "bars" / "NSE").mkdir(parents=True)
    (lake / "bars" / "NSE" / "big.parquet").write_bytes(b"BARS")
    (lake / ".rate").mkdir()
    (lake / ".rate" / "pull_rate.json").write_bytes(b"{}")
    (lake / ".quarantine").mkdir()
    (lake / ".quarantine" / "q.parquet").write_bytes(b"Q")
    (lake / ".locks").mkdir()
    (lake / ".locks" / "pull_singleflight.lock").write_bytes(b"")
    # Results tree.
    (results / "NIFTY" / "5minute").mkdir(parents=True)
    (results / "NIFTY" / "5minute" / "BUY_k1_abc.parquet").write_bytes(b"TUPLE")
    (results / "NIFTY" / "5minute" / "BUY_k1_abc.parquet.complete.json").write_bytes(b"{}")
    (results / "audit").mkdir()
    (results / "audit" / "sweep-all.jsonl").write_bytes(b"CHAIN")
    (results / "checkpoint").mkdir()
    (results / "checkpoint" / "sweep-all.json").write_bytes(b"{}")
    (results / "dedup-cache").mkdir()
    (results / "dedup-cache" / "data.mdb").write_bytes(b"LMDB")
    (results / "cascade").mkdir()
    (results / "cascade" / "cascade-report.json").write_bytes(b"{}")
    (results / "nightly" / "nightly-OLD").mkdir(parents=True)
    (results / "nightly" / "nightly-OLD" / "report.json").write_bytes(b"{}")
    (results / "nightly" / "nightly-CUR").mkdir(parents=True)
    (results / "nightly" / "nightly-CUR" / "report.json").write_bytes(b"{}")
    # The SHARED cross-run nightly audit chain — LIVE-APPENDED by the very
    # command taking the snapshot; ALWAYS excluded (review round 5 MAJOR).
    (results / "nightly" / "audit").mkdir(parents=True)
    (results / "nightly" / "audit" / "nightly.jsonl").write_bytes(b"AUDIT-CHAIN")
    (results / "progress").mkdir()
    (results / "progress" / "sweep-progress.json").write_bytes(b"{}")
    (results / ".locks").mkdir()
    (results / ".locks" / "sweep_singleflight.lock").write_bytes(b"")
    # Gap-register Rulings 4 + 13: the in-tree run-artifact layout + the
    # estimator calibration store ride the results walk...
    (results / "sweep_runs" / "run-IN").mkdir(parents=True)
    (results / "sweep_runs" / "run-IN" / "top100.json").write_bytes(b"{}")
    (results / "estimator_calibration.json").write_bytes(b"{}")
    # ...and the SHIPPED P3 `trade_store.sweep_run_dir` SIBLING root
    # (`<results_root parent>/sweep_runs/` = `<lake>/sweep_runs/` here) is the
    # `sweep_runs` namespace — beside the results tree, missed by its walk.
    (lake / "sweep_runs" / "grid-abc").mkdir(parents=True)
    (lake / "sweep_runs" / "grid-abc" / "manifest.json").write_bytes(b"{}")
    return lake, results


# ── env resolvers ────────────────────────────────────────────────────────


def test_default_backup_root_env_and_home(monkeypatch: pytest.MonkeyPatch) -> None:
    assert default_backup_root({NIGHTLY_BACKUP_ROOT_ENV: "/x/backups"}) == Path("/x/backups")
    assert default_backup_root({}) == Path.home() / ".brutex" / "backups"
    monkeypatch.setenv(NIGHTLY_BACKUP_ROOT_ENV, "~/bk")
    assert default_backup_root(None) == Path.home() / "bk"


def test_resolve_backup_keep_arms(monkeypatch: pytest.MonkeyPatch) -> None:
    assert resolve_backup_keep({}) == 7
    assert resolve_backup_keep({NIGHTLY_BACKUP_KEEP_ENV: "3"}) == 3
    with pytest.raises(NightlyEnvError, match="integer"):
        resolve_backup_keep({NIGHTLY_BACKUP_KEEP_ENV: "weekly"})
    with pytest.raises(NightlyEnvError, match=">= 1"):
        resolve_backup_keep({NIGHTLY_BACKUP_KEEP_ENV: "0"})
    monkeypatch.setenv(NIGHTLY_BACKUP_KEEP_ENV, "9")
    assert resolve_backup_keep(None) == 9


def test_resolve_backup_verify_arms(monkeypatch: pytest.MonkeyPatch) -> None:
    assert resolve_backup_verify({}) is True
    assert resolve_backup_verify({NIGHTLY_BACKUP_VERIFY_ENV: "1"}) is True
    assert resolve_backup_verify({NIGHTLY_BACKUP_VERIFY_ENV: "0"}) is False
    with pytest.raises(NightlyEnvError, match="'0' or '1'"):
        resolve_backup_verify({NIGHTLY_BACKUP_VERIFY_ENV: "off"})
    monkeypatch.setenv(NIGHTLY_BACKUP_VERIFY_ENV, "0")
    assert resolve_backup_verify(None) is False


def test_sha256_file_matches_hashlib(tmp_path: Path) -> None:
    path = tmp_path / "f.bin"
    path.write_bytes(b"nightly bytes")
    assert sha256_file(path) == hashlib.sha256(b"nightly bytes").hexdigest()


# ── inventory ────────────────────────────────────────────────────────────


def test_inventory_inclusion_exclusion_table(tmp_path: Path) -> None:
    lake, results = _make_lake(tmp_path)
    items, skipped = backup_inventory(lake, results, exclude_nightly_id="nightly-CUR")
    keys = {f"{i.namespace}/{i.relpath}" for i in items}
    assert keys == {
        "lake/window_ledger/ledger.parquet",
        "lake/window_ledger/deltas/d1.parquet",
        "lake/registry.duckdb",
        "lake/instruments.duckdb",
        "lake/catalog.duckdb",
        "lake/audit/retry-storm-x.jsonl",
        "results/NIFTY/5minute/BUY_k1_abc.parquet",
        "results/NIFTY/5minute/BUY_k1_abc.parquet.complete.json",
        "results/audit/sweep-all.jsonl",
        "results/checkpoint/sweep-all.json",
        "results/dedup-cache/data.mdb",
        "results/cascade/cascade-report.json",
        "results/nightly/nightly-OLD/report.json",  # PRIOR nightly dir IS included
        # Rulings 4 + 13: in-tree run artifacts + calibration store included…
        "results/sweep_runs/run-IN/top100.json",
        "results/estimator_calibration.json",
        # …and the SIBLING `<results parent>/sweep_runs/` root as its own namespace.
        "sweep_runs/grid-abc/manifest.json",
    }
    assert skipped == []
    # Restore-order law: ledger LAST (2), deltas 1, everything else 0.
    orders = {f"{i.namespace}/{i.relpath}": i.restore_order for i in items}
    assert orders["lake/window_ledger/ledger.parquet"] == 2
    assert orders["lake/window_ledger/deltas/d1.parquet"] == 1
    assert orders["lake/registry.duckdb"] == 0
    assert orders["results/audit/sweep-all.jsonl"] == 0
    # Deterministic order.
    assert [f"{i.namespace}/{i.relpath}" for i in items] == sorted(
        f"{i.namespace}/{i.relpath}" for i in items
    )


def test_inventory_without_exclude_id_keeps_all_nightly_dirs(tmp_path: Path) -> None:
    lake, results = _make_lake(tmp_path)
    items, _ = backup_inventory(lake, results, exclude_nightly_id=None)
    keys = {f"{i.namespace}/{i.relpath}" for i in items}
    assert "results/nightly/nightly-CUR/report.json" in keys
    assert "results/nightly/nightly-OLD/report.json" in keys
    # The live audit chain stays out even when NO current id is excluded.
    assert "results/nightly/audit/nightly.jsonl" not in keys


def test_inventory_always_excludes_live_nightly_audit_chain(tmp_path: Path) -> None:
    """Regression (review round 5 MAJOR): the shared, actively-appended
    ``results/nightly/audit/nightly.jsonl`` must NEVER enter the backup scope
    — a restored copy would ``os.replace`` the open writer's inode and lose
    RESTORE_START/RESTORE_DONE while rolling the forensic chain back to
    snapshot time (Golden Rule #8)."""
    lake, results = _make_lake(tmp_path)
    for exclude_id in ("nightly-CUR", None):
        items, _ = backup_inventory(lake, results, exclude_nightly_id=exclude_id)
        keys = {f"{i.namespace}/{i.relpath}" for i in items}
        assert not any(k.startswith("results/nightly/audit/") for k in keys)
    # The exclusion parts mirror the ONE audit-path law in report.py.
    from brutex.nightly.report import nightly_audit_path

    rel = nightly_audit_path(results).relative_to(results)
    assert rel.parts[: len(NIGHTLY_AUDIT_REL_PARTS)] == NIGHTLY_AUDIT_REL_PARTS


def test_inventory_absent_members_are_honest_notes(tmp_path: Path) -> None:
    lake = tmp_path / "lake"
    lake.mkdir()
    (lake / "registry.duckdb").write_bytes(b"R")
    items, skipped = backup_inventory(lake, tmp_path / "elsewhere-results", exclude_nightly_id=None)
    assert [f"{i.namespace}/{i.relpath}" for i in items] == ["lake/registry.duckdb"]
    assert "window_ledger" in skipped
    assert "instruments.duckdb" in skipped
    assert "catalog.duckdb" in skipped
    assert "audit" in skipped
    assert any("results root" in s for s in skipped)


def test_inventory_results_root_outside_lake_is_covered(tmp_path: Path) -> None:
    """A custom --results-root outside the lake is backed up correctly (D-8)."""
    lake, _ = _make_lake(tmp_path)
    outside = tmp_path / "external-results"
    (outside / "SENSEX" / "1minute").mkdir(parents=True)
    (outside / "SENSEX" / "1minute" / "SELL_k2_x.parquet").write_bytes(b"T")
    items, _ = backup_inventory(lake, outside, exclude_nightly_id=None)
    keys = {f"{i.namespace}/{i.relpath}" for i in items}
    assert "results/SENSEX/1minute/SELL_k2_x.parquet" in keys


def test_inventory_refuses_results_root_equal_to_lake_root(tmp_path: Path) -> None:
    with pytest.raises(BackupManifestError, match="must not equal"):
        backup_inventory(tmp_path, tmp_path, exclude_nightly_id=None)


def test_estimate_backup_bytes_sums_and_tolerates_vanished(tmp_path: Path) -> None:
    a = tmp_path / "a.bin"
    a.write_bytes(b"12345")
    items = [
        BackupItem(namespace="lake", relpath="a.bin", source=a, restore_order=0),
        BackupItem(
            namespace="lake", relpath="gone.bin", source=tmp_path / "gone.bin", restore_order=0
        ),
    ]
    assert estimate_backup_bytes(items) == 5


# ── manifest golden + round-trip ─────────────────────────────────────────


def _entry(**overrides: object) -> BackupEntry:
    base: dict[str, object] = {
        "namespace": "lake",
        "relpath": "window_ledger/ledger.parquet",
        "size_bytes": 12345,
        "sha256": "ab" * 32,
        "restore_order": 2,
    }
    base.update(overrides)
    return BackupEntry(
        namespace=str(base["namespace"]),
        relpath=str(base["relpath"]),
        size_bytes=int(str(base["size_bytes"])),
        sha256=str(base["sha256"]),
        restore_order=int(str(base["restore_order"])),
    )


def test_manifest_to_dict_golden() -> None:
    manifest = BackupManifest(
        schema_version=BACKUP_SCHEMA_VERSION,
        snapshot_id="nightly-20260708T013000Z-a1b2c3",
        nightly_id="nightly-20260708T013000Z-a1b2c3",
        created_utc=1785086400.0,
        lake_root="/abs/path",
        results_root="/abs/path/sweeps",
        entries=(_entry(),),
        item_count=1,
        total_bytes=12345,
        skipped_absent=("catalog.duckdb",),
        excluded_by_law=("bars/ (re-pullable)",),
    )
    assert manifest.to_dict() == {
        "schema_version": 1,
        "snapshot_id": "nightly-20260708T013000Z-a1b2c3",
        "nightly_id": "nightly-20260708T013000Z-a1b2c3",
        "created_utc": 1785086400.0,
        "lake_root": "/abs/path",
        "results_root": "/abs/path/sweeps",
        "entries": [
            {
                "namespace": "lake",
                "relpath": "window_ledger/ledger.parquet",
                "size_bytes": 12345,
                "sha256": "ab" * 32,
                "restore_order": 2,
            }
        ],
        "item_count": 1,
        "total_bytes": 12345,
        "skipped_absent": ["catalog.duckdb"],
        "excluded_by_law": ["bars/ (re-pullable)"],
    }


@given(
    entries=st.lists(
        st.tuples(
            st.sampled_from(["lake", "results"]),
            st.text(
                alphabet=st.characters(min_codepoint=97, max_codepoint=122), min_size=1, max_size=20
            ),
            st.integers(min_value=0, max_value=2**40),
            st.integers(min_value=0, max_value=2),
        ),
        max_size=8,
    ),
    nightly_id=st.none() | st.text(max_size=12),
    created=st.floats(min_value=0, max_value=4e9, allow_nan=False),
)
def test_manifest_round_trip_property(
    entries: list[tuple[str, str, int, int]],
    nightly_id: str | None,
    created: float,
) -> None:
    manifest = BackupManifest(
        schema_version=BACKUP_SCHEMA_VERSION,
        snapshot_id="snap",
        nightly_id=nightly_id,
        created_utc=created,
        lake_root="/lake",
        results_root="/lake/sweeps",
        entries=tuple(
            BackupEntry(
                namespace=ns, relpath=rel, size_bytes=size, sha256="cd" * 32, restore_order=order
            )
            for ns, rel, size, order in entries
        ),
        item_count=len(entries),
        total_bytes=sum(e[2] for e in entries),
        skipped_absent=(),
        excluded_by_law=(),
    )
    assert BackupManifest.from_dict(manifest.to_dict()) == manifest


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        ({"schema_version": 2}, "schema_version"),
        ({"entries": "nope"}, "must be a list"),
        ({"entries": ["nope"]}, "must be a mapping"),
        ({"created_utc": "not-a-float"}, "structurally invalid"),
        ({"skipped_absent": 7}, "must be a list"),
    ],
)
def test_manifest_from_dict_junk_fails_loud(mutation: dict[str, object], match: str) -> None:
    base = BackupManifest(
        schema_version=BACKUP_SCHEMA_VERSION,
        snapshot_id="s",
        nightly_id=None,
        created_utc=1.0,
        lake_root="/l",
        results_root="/r",
        entries=(),
        item_count=0,
        total_bytes=0,
        skipped_absent=(),
        excluded_by_law=(),
    ).to_dict()
    base.update(mutation)
    with pytest.raises(BackupManifestError, match=match):
        BackupManifest.from_dict(base)


def test_manifest_from_dict_missing_key_fails_loud() -> None:
    with pytest.raises(BackupManifestError, match="structurally invalid"):
        BackupManifest.from_dict({"schema_version": BACKUP_SCHEMA_VERSION})


# ── create_snapshot ──────────────────────────────────────────────────────


def test_create_snapshot_seals_and_hashes_verifiably(tmp_path: Path) -> None:
    lake, results = _make_lake(tmp_path)
    backup_root = tmp_path / "backups"
    result = create_snapshot(
        lake_root=lake,
        results_root=results,
        backup_root=backup_root,
        snapshot_id="nightly-A",
        nightly_id="nightly-CUR",
        now=lambda: 1000.0,
    )
    final = backup_root / "nightly-A"
    assert result.snapshot_path == final
    assert final.is_dir()
    assert not (backup_root / f"nightly-A{PARTIAL_SUFFIX}").exists()
    manifest = BackupManifest.from_dict(
        json.loads((final / MANIFEST_NAME).read_text(encoding="utf-8"))
    )
    assert manifest.snapshot_id == "nightly-A"
    assert manifest.nightly_id == "nightly-CUR"
    assert manifest.created_utc == 1000.0
    assert manifest.item_count == result.file_count == 16
    assert result.total_bytes == manifest.total_bytes > 0
    assert result.pruned == ()
    # Independent test-side re-hash of EVERY entry (hand-rolled sha256 — the
    # surface-trial independence idiom).
    for entry in manifest.entries:
        dest = final / entry.namespace / entry.relpath
        raw = dest.read_bytes()
        assert hashlib.sha256(raw).hexdigest() == entry.sha256
        assert len(raw) == entry.size_bytes
    # The current nightly dir + transient trees never entered the snapshot.
    copied = _tree(final)
    assert not any("nightly-CUR" in p for p in copied)
    assert not any(p.startswith("results/progress/") for p in copied)
    assert not any("bars/" in p for p in copied)
    assert not any(p.startswith("results/nightly/audit/") for p in copied)
    # Exclusion reasons recorded.
    assert "bars/ (re-pullable)" in manifest.excluded_by_law
    assert "results nightly/nightly-CUR/ (live nightly dir)" in manifest.excluded_by_law
    assert "results nightly/audit/ (live cross-run nightly audit chain)" in (
        manifest.excluded_by_law
    )


def test_create_snapshot_hashes_destination_bytes_not_source(tmp_path: Path) -> None:
    """A corrupting copier cannot make the manifest disagree with the snapshot."""
    lake = tmp_path / "lake"
    lake.mkdir()
    (lake / "registry.duckdb").write_bytes(b"TRUE SOURCE")
    results = tmp_path / "results"
    results.mkdir()

    def corrupting_copy(_source: Path, dest: Path) -> None:
        dest.write_bytes(b"DIFFERENT DESTINATION BYTES")

    result = create_snapshot(
        lake_root=lake,
        results_root=results,
        backup_root=tmp_path / "bk",
        snapshot_id="s1",
        nightly_id=None,
        copy_file=corrupting_copy,
    )
    manifest = BackupManifest.from_dict(
        json.loads((result.snapshot_path / MANIFEST_NAME).read_text(encoding="utf-8"))
    )
    [entry] = manifest.entries
    assert entry.sha256 == hashlib.sha256(b"DIFFERENT DESTINATION BYTES").hexdigest()
    assert entry.sha256 != hashlib.sha256(b"TRUE SOURCE").hexdigest()


def test_create_snapshot_copy_failure_leaves_only_partial(tmp_path: Path) -> None:
    lake, results = _make_lake(tmp_path)
    backup_root = tmp_path / "bk"
    calls = {"n": 0}

    def enospc_on_third(source: Path, dest: Path) -> None:
        calls["n"] += 1
        if calls["n"] == 3:
            raise OSError(28, "No space left on device")
        dest.write_bytes(source.read_bytes())

    with pytest.raises(OSError, match="No space left"):
        create_snapshot(
            lake_root=lake,
            results_root=results,
            backup_root=backup_root,
            snapshot_id="s-fail",
            nightly_id=None,
            copy_file=enospc_on_third,
        )
    assert (backup_root / f"s-fail{PARTIAL_SUFFIX}").is_dir()  # staging left for forensics
    assert not (backup_root / "s-fail").exists()  # NEVER sealed
    # The staged partial carries no manifest — restore/retention never consume it.
    assert not (backup_root / f"s-fail{PARTIAL_SUFFIX}" / MANIFEST_NAME).exists()


def test_create_snapshot_refuses_existing_staging_or_final(tmp_path: Path) -> None:
    lake, results = _make_lake(tmp_path)
    backup_root = tmp_path / "bk"
    (backup_root / f"dup{PARTIAL_SUFFIX}").mkdir(parents=True)
    with pytest.raises(BackupManifestError, match="collision is a bug"):
        create_snapshot(
            lake_root=lake,
            results_root=results,
            backup_root=backup_root,
            snapshot_id="dup",
            nightly_id=None,
        )
    (backup_root / "dup2").mkdir(parents=True)
    with pytest.raises(BackupManifestError, match="collision is a bug"):
        create_snapshot(
            lake_root=lake,
            results_root=results,
            backup_root=backup_root,
            snapshot_id="dup2",
            nightly_id=None,
        )


def test_create_snapshot_explicit_keep_below_one_refused(tmp_path: Path) -> None:
    lake, results = _make_lake(tmp_path)
    with pytest.raises(NightlyEnvError, match=">= 1"):
        create_snapshot(
            lake_root=lake,
            results_root=results,
            backup_root=tmp_path / "bk",
            snapshot_id="s",
            nightly_id=None,
            keep=0,
        )


def test_create_snapshot_preseal_verify_catches_post_hash_corruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file whose bytes drift between manifest-hash and verify is caught pre-seal."""
    lake = tmp_path / "lake"
    lake.mkdir()
    (lake / "registry.duckdb").write_bytes(b"R")
    (lake / "catalog.duckdb").write_bytes(b"C")
    results = tmp_path / "results"
    results.mkdir()

    seen: dict[str, int] = {}

    def drifting(path: Path) -> str:
        key = str(path)
        seen[key] = seen.get(key, 0) + 1
        if seen[key] > 1 and path.name == "registry.duckdb":
            return "0" * 64  # the verify pass sees different bytes
        return sha256_file(path)

    monkeypatch.setattr("brutex.nightly.backup.sha256_file", drifting)
    backup_root = tmp_path / "bk"
    with pytest.raises(BackupManifestError, match="pre-seal verify FAILED"):
        create_snapshot(
            lake_root=lake,
            results_root=results,
            backup_root=backup_root,
            snapshot_id="s-verify",
            nightly_id=None,
        )
    assert (backup_root / f"s-verify{PARTIAL_SUFFIX}").is_dir()
    assert not (backup_root / "s-verify").exists()


def test_create_snapshot_verify_unreadable_and_cap_arms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """>10 mismatches are capped with an ellipsis; unreadable files are named."""
    lake = tmp_path / "lake"
    lake.mkdir()
    (lake / "audit").mkdir()
    for i in range(12):
        (lake / "audit" / f"a{i:02d}.jsonl").write_bytes(b"x")
    results = tmp_path / "results"
    results.mkdir()

    seen: dict[str, int] = {}

    def unreadable_on_verify(path: Path) -> str:
        key = str(path)
        seen[key] = seen.get(key, 0) + 1
        if seen[key] > 1:
            raise OSError(5, "io error")
        return sha256_file(path)

    monkeypatch.setattr("brutex.nightly.backup.sha256_file", unreadable_on_verify)
    with pytest.raises(BackupManifestError) as excinfo:
        create_snapshot(
            lake_root=lake,
            results_root=results,
            backup_root=tmp_path / "bk",
            snapshot_id="s-cap",
            nightly_id=None,
        )
    message = str(excinfo.value)
    assert "12 file(s)" in message
    assert "(unreadable)" in message
    assert " …" in message  # capped at 10 names


def test_create_snapshot_verify_escape_skips_and_warns(tmp_path: Path) -> None:
    lake = tmp_path / "lake"
    lake.mkdir()
    (lake / "registry.duckdb").write_bytes(b"R")
    results = tmp_path / "results"
    results.mkdir()
    with structlog.testing.capture_logs() as logs:
        result = create_snapshot(
            lake_root=lake,
            results_root=results,
            backup_root=tmp_path / "bk",
            snapshot_id="s-novrfy",
            nightly_id=None,
            env={NIGHTLY_BACKUP_VERIFY_ENV: "0"},
        )
    assert result.snapshot_path.is_dir()
    assert any(log["event"] == "nightly.backup.verify_skipped" for log in logs)


def test_create_snapshot_retention_prunes_oldest(tmp_path: Path) -> None:
    lake = tmp_path / "lake"
    lake.mkdir()
    (lake / "registry.duckdb").write_bytes(b"R")
    results = tmp_path / "results"
    results.mkdir()
    backup_root = tmp_path / "bk"
    times = iter([100.0, 200.0, 300.0])
    for i, t in zip(range(3), times, strict=False):
        create_snapshot(
            lake_root=lake,
            results_root=results,
            backup_root=backup_root,
            snapshot_id=f"snap-{i}",
            nightly_id=None,
            keep=2,
            now=lambda t=t: t,
        )
    names = {p.name for p in backup_root.iterdir()}
    assert names == {"snap-1", "snap-2"}  # oldest pruned, newest 2 kept


# ── prune_snapshots ──────────────────────────────────────────────────────


def _sealed(backup_root: Path, name: str, created: float) -> Path:
    snap = backup_root / name
    snap.mkdir(parents=True)
    (snap / MANIFEST_NAME).write_text(json.dumps({"created_utc": created}), encoding="utf-8")
    return snap


def test_prune_keeps_newest_n_and_protect(tmp_path: Path) -> None:
    newest = _sealed(tmp_path, "a-new", 300.0)
    middle = _sealed(tmp_path, "b-mid", 200.0)
    oldest = _sealed(tmp_path, "c-old", 100.0)
    pruned = prune_snapshots(tmp_path, keep=1, protect=oldest, now=lambda: 1000.0)
    assert pruned == ("b-mid",)
    assert newest.exists()
    assert oldest.exists()  # protected despite being oldest
    assert not middle.exists()


def test_prune_never_deletes_the_newest_even_at_keep_one(tmp_path: Path) -> None:
    only = _sealed(tmp_path, "only", 100.0)
    assert prune_snapshots(tmp_path, keep=1, protect=None) == ()
    assert only.exists()


def test_prune_ignores_files_and_unsealed_dirs(tmp_path: Path) -> None:
    (tmp_path / "stray.txt").write_text("x")
    (tmp_path / "no-manifest-dir").mkdir()
    assert prune_snapshots(tmp_path, keep=1, protect=None) == ()
    assert (tmp_path / "no-manifest-dir").exists()


def test_prune_gc_stale_partial_keeps_fresh_partial(tmp_path: Path) -> None:
    stale = tmp_path / f"old{PARTIAL_SUFFIX}"
    stale.mkdir(parents=True)
    os.utime(stale, (1000.0, 1000.0))
    fresh = tmp_path / f"new{PARTIAL_SUFFIX}"
    fresh.mkdir(parents=True)
    os.utime(fresh, (90_000.0, 90_000.0))
    pruned = prune_snapshots(tmp_path, keep=1, protect=None, now=lambda: 100_000.0)
    assert pruned == (f"old{PARTIAL_SUFFIX}",)
    assert fresh.exists()


def test_prune_missing_root_and_keep_below_one(tmp_path: Path) -> None:
    assert prune_snapshots(tmp_path / "absent", keep=1, protect=None) == ()
    with pytest.raises(NightlyEnvError, match=">= 1"):
        prune_snapshots(tmp_path, keep=0, protect=None)


def test_manifest_created_utc_fallbacks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    snap = tmp_path / "junk-manifest"
    snap.mkdir()
    (snap / MANIFEST_NAME).write_text("{not json", encoding="utf-8")
    os.utime(snap, (777.0, 777.0))
    assert _manifest_created_utc(snap) == 777.0  # mtime fallback
    # Total-function guard: even a vanished dir yields 0.0, never a raise.
    monkeypatch.setattr(Path, "stat", _raise_oserror)
    assert _manifest_created_utc(tmp_path / "gone") == 0.0


def _raise_oserror(self: Path, **kwargs: object) -> object:
    raise OSError(2, "gone")


# ── gap-register jul-8: sweep_runs sibling root + fsync + exclusions ─────


def test_sweep_runs_sibling_root_law(tmp_path: Path) -> None:
    """The Ruling-4 sibling root mirrors `trade_store.sweep_run_dir`'s law."""
    assert sweep_runs_sibling_root(tmp_path / "lake" / "sweeps") == (
        tmp_path / "lake" / "sweep_runs"
    )


def test_inventory_absent_sibling_root_is_honest_note(tmp_path: Path) -> None:
    lake = tmp_path / "lake"
    lake.mkdir()
    (lake / "registry.duckdb").write_bytes(b"R")
    results = tmp_path / "elsewhere" / "results"
    results.mkdir(parents=True)
    items, skipped = backup_inventory(lake, results, exclude_nightly_id=None)
    assert not any(i.namespace == "sweep_runs" for i in items)
    assert any("sweep_runs sibling root" in s for s in skipped)


def test_inventory_results_root_named_sweep_runs_never_double_walks(tmp_path: Path) -> None:
    """Degenerate identity: results_root itself named `sweep_runs` — the
    sibling root IS the results tree; walking it twice would duplicate every
    entry under two namespaces."""
    lake = tmp_path / "lake"
    lake.mkdir()
    (lake / "registry.duckdb").write_bytes(b"R")
    results = tmp_path / "sweep_runs"
    (results / "run-1").mkdir(parents=True)
    (results / "run-1" / "top100.json").write_bytes(b"{}")
    items, skipped = backup_inventory(lake, results, exclude_nightly_id=None)
    keys = [f"{i.namespace}/{i.relpath}" for i in items]
    assert keys.count("results/run-1/top100.json") == 1
    assert not any(i.namespace == "sweep_runs" for i in items)
    assert not any("sweep_runs sibling root" in s for s in skipped)


def test_excluded_by_law_names_internal_forensics_mirror(tmp_path: Path) -> None:
    """Ruling 15: `~/.brutex/runs/` is an EXPLICIT exclusion — it exists to
    survive the backup volume's own failure, so it must never be dragged in;
    the manifest records the reason + the rsync guidance pointer."""
    lake, results = _make_lake(tmp_path)
    result = create_snapshot(
        lake_root=lake,
        results_root=results,
        backup_root=tmp_path / "bk",
        snapshot_id="s-excl",
        nightly_id=None,
    )
    manifest = BackupManifest.from_dict(
        json.loads((result.snapshot_path / MANIFEST_NAME).read_text(encoding="utf-8"))
    )
    assert any("~/.brutex/runs/" in reason for reason in manifest.excluded_by_law)
    assert any("rsync" in reason for reason in manifest.excluded_by_law)


def test_create_snapshot_fsyncs_every_copied_file_before_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review MINOR: copied file CONTENTS must be fsynced (per tmp file,
    BEFORE the replace) — the pre-fix backup fsynced only the backup-root dir
    at seal time, so a post-seal power loss could lose page-cached bytes while
    the (fsynced) manifest survived, failing the next verify."""
    lake, results = _make_lake(tmp_path)
    synced: list[Path] = []

    def spy_fsync(path: Path) -> None:
        assert path.exists()  # called BEFORE os.replace consumed the tmp
        assert path.name.endswith(".brutex-copy-tmp")
        synced.append(path)

    monkeypatch.setattr("brutex.nightly.backup.fsync_file", spy_fsync)
    result = create_snapshot(
        lake_root=lake,
        results_root=results,
        backup_root=tmp_path / "bk",
        snapshot_id="s-fsync",
        nightly_id=None,
    )
    # nightly_id=None keeps the nightly-CUR dir in scope → 17 files here
    # (the 16-file table + nightly-CUR/report.json); one fsync per copy.
    assert len(synced) == result.file_count == 17


def test_fsync_file_real_and_missing(tmp_path: Path) -> None:
    """The real helper syncs an existing file and swallows a missing one."""
    target = tmp_path / "f.bin"
    target.write_bytes(b"x")
    fsync_file(target)  # no raise
    fsync_file(tmp_path / "absent.bin")  # OSError suppressed
