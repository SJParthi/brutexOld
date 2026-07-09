"""Coverage tests for :mod:`brutex.orchestrator.replay` — the L3
audit-log replay oracle closing 2026-05-25 audit row 14.

Per DEC-L3-REPLAY-ORACLE-001 (MIGRATION entry #241). 100% line +
100% branch coverage. Test plan:

* ``TestProjectSubjects`` — action filter, empty filter, duplicates
* ``TestComputeReplayDiff`` — intersection / missing / extra / clean
* ``TestReplayAuditToDiff`` — round-trip via a real audit JSONL file
* ``TestAssertDedupConsistent`` — silence on match, raise on mismatch
"""

from __future__ import annotations

from pathlib import Path

import pytest
from brutex.orchestrator.audit import AuditError, AuditWriter
from brutex.orchestrator.replay import (
    ReplayDiff,
    assert_dedup_consistent,
    compute_replay_diff,
    project_subjects,
    replay_audit_to_diff,
)

# ────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────


def _write_audit(
    *,
    path: Path,
    rows: list[tuple[str, str, dict[str, object]]],
) -> None:
    """Write a synthetic audit log at *path*. *rows* is a list of
    (action, subject, payload) triples."""
    with AuditWriter.open(path, actor="test") as writer:
        for action, subject, payload in rows:
            writer.append(action=action, subject=subject, payload=payload)


# ────────────────────────────────────────────────────────────────────────
# project_subjects
# ────────────────────────────────────────────────────────────────────────


class TestProjectSubjects:
    def test_filters_by_action(self, tmp_path: Path) -> None:
        path = tmp_path / "audit.jsonl"
        _write_audit(
            path=path,
            rows=[
                ("add", "subj-a", {}),
                ("add", "subj-b", {}),
                ("remove", "subj-c", {}),
            ],
        )
        from brutex.orchestrator.audit import read_entries

        entries = read_entries(path)
        subjects = project_subjects(entries, action_filter="add")
        assert subjects == frozenset({"subj-a", "subj-b"})

    def test_empty_filter_returns_all(self, tmp_path: Path) -> None:
        path = tmp_path / "audit.jsonl"
        _write_audit(
            path=path,
            rows=[
                ("add", "subj-a", {}),
                ("remove", "subj-b", {}),
            ],
        )
        from brutex.orchestrator.audit import read_entries

        entries = read_entries(path)
        subjects = project_subjects(entries, action_filter="")
        assert subjects == frozenset({"subj-a", "subj-b"})

    def test_duplicate_subjects_collapse(self, tmp_path: Path) -> None:
        path = tmp_path / "audit.jsonl"
        _write_audit(
            path=path,
            rows=[
                ("add", "subj-dup", {}),
                ("add", "subj-dup", {}),  # idempotent re-add
                ("add", "subj-unique", {}),
            ],
        )
        from brutex.orchestrator.audit import read_entries

        entries = read_entries(path)
        subjects = project_subjects(entries, action_filter="add")
        assert subjects == frozenset({"subj-dup", "subj-unique"})

    def test_no_matching_entries(self, tmp_path: Path) -> None:
        path = tmp_path / "audit.jsonl"
        _write_audit(
            path=path,
            rows=[
                ("remove", "subj-a", {}),
            ],
        )
        from brutex.orchestrator.audit import read_entries

        entries = read_entries(path)
        subjects = project_subjects(entries, action_filter="add")
        assert subjects == frozenset()

    def test_accepts_streaming_iterator(self, tmp_path: Path) -> None:
        """DEC-AUDIT-READERS-STREAM-001: the signature widened
        ``list[AuditEntry]`` → ``Iterable[AuditEntry]`` — a one-shot
        generator (``iter_entries``) projects identically to the
        materialised ``read_entries`` list (equivalence by construction:
        ``read_entries`` IS ``list(iter_entries(path))``)."""
        path = tmp_path / "audit.jsonl"
        _write_audit(
            path=path,
            rows=[
                ("add", "subj-a", {}),
                ("remove", "subj-b", {}),
                ("add", "subj-c", {}),
            ],
        )
        from brutex.orchestrator.audit import iter_entries, read_entries

        streamed = project_subjects(iter_entries(path), action_filter="add")
        materialised = project_subjects(read_entries(path), action_filter="add")
        assert streamed == materialised == frozenset({"subj-a", "subj-c"})


# ────────────────────────────────────────────────────────────────────────
# compute_replay_diff
# ────────────────────────────────────────────────────────────────────────


class TestComputeReplayDiff:
    def test_clean_match_is_consistent(self) -> None:
        diff = compute_replay_diff(
            audit_subjects=frozenset({"a", "b", "c"}),
            live_subjects=frozenset({"a", "b", "c"}),
            replayed_count=3,
        )
        assert diff.is_consistent
        assert diff.intersection == frozenset({"a", "b", "c"})
        assert diff.missing_from_live == frozenset()
        assert diff.extra_in_live == frozenset()
        assert diff.replayed_count == 3

    def test_missing_from_live(self) -> None:
        diff = compute_replay_diff(
            audit_subjects=frozenset({"a", "b"}),
            live_subjects=frozenset({"a"}),
            replayed_count=2,
        )
        assert not diff.is_consistent
        assert diff.missing_from_live == frozenset({"b"})

    def test_extra_in_live(self) -> None:
        diff = compute_replay_diff(
            audit_subjects=frozenset({"a"}),
            live_subjects=frozenset({"a", "rogue"}),
            replayed_count=1,
        )
        assert not diff.is_consistent
        assert diff.extra_in_live == frozenset({"rogue"})

    def test_both_missing_and_extra(self) -> None:
        diff = compute_replay_diff(
            audit_subjects=frozenset({"a", "b"}),
            live_subjects=frozenset({"a", "c"}),
            replayed_count=2,
        )
        assert not diff.is_consistent
        assert diff.intersection == frozenset({"a"})
        assert diff.missing_from_live == frozenset({"b"})
        assert diff.extra_in_live == frozenset({"c"})

    def test_empty_sets(self) -> None:
        diff = compute_replay_diff(
            audit_subjects=frozenset(),
            live_subjects=frozenset(),
            replayed_count=0,
        )
        assert diff.is_consistent
        assert diff.replayed_count == 0


# ────────────────────────────────────────────────────────────────────────
# replay_audit_to_diff
# ────────────────────────────────────────────────────────────────────────


class TestReplayAuditToDiff:
    def test_round_trip_clean(self, tmp_path: Path) -> None:
        path = tmp_path / "audit.jsonl"
        _write_audit(
            path=path,
            rows=[
                ("add", "x", {}),
                ("add", "y", {}),
            ],
        )
        diff = replay_audit_to_diff(
            audit_path=path,
            live_subjects=frozenset({"x", "y"}),
        )
        assert diff.is_consistent
        assert diff.replayed_count == 2

    def test_round_trip_missing(self, tmp_path: Path) -> None:
        path = tmp_path / "audit.jsonl"
        _write_audit(
            path=path,
            rows=[("add", "x", {}), ("add", "y", {})],
        )
        diff = replay_audit_to_diff(
            audit_path=path,
            live_subjects=frozenset({"x"}),  # y missing
        )
        assert not diff.is_consistent
        assert diff.missing_from_live == frozenset({"y"})

    def test_round_trip_action_filter(self, tmp_path: Path) -> None:
        path = tmp_path / "audit.jsonl"
        _write_audit(
            path=path,
            rows=[
                ("add", "x", {}),
                ("remove", "y", {}),
            ],
        )
        # action_filter="remove" — only y is in scope
        diff = replay_audit_to_diff(
            audit_path=path,
            live_subjects=frozenset({"y"}),
            action_filter="remove",
        )
        assert diff.is_consistent

    def test_mid_chain_corruption_raises_audit_error(self, tmp_path: Path) -> None:
        """DEC-AUDIT-READERS-STREAM-001: the streaming reader raises its
        ``AuditError`` lazily, DURING the projection, at the malformed
        committed line — the error contract of the old materialised
        ``read_entries`` (raise before any diff is produced) is preserved."""
        path = tmp_path / "audit.jsonl"
        _write_audit(path=path, rows=[("add", "x", {})])
        with path.open("a", encoding="utf-8") as f:
            f.write("{not jsonl mid-chain\n")  # committed (newline-terminated) corruption
        with pytest.raises(AuditError):
            replay_audit_to_diff(audit_path=path, live_subjects=frozenset({"x"}))

    def test_missing_file_raises_audit_error(self, tmp_path: Path) -> None:
        """``iter_entries`` raises at CALL time on a missing file — the same
        AuditError contract ``read_entries`` had (regression pin for the
        streaming conversion)."""
        with pytest.raises(AuditError, match="not found"):
            replay_audit_to_diff(audit_path=tmp_path / "absent.jsonl", live_subjects=frozenset())

    def test_streams_without_materialising_the_chain(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Memory-shape pin (DEC-AUDIT-READERS-STREAM-001): the replay path
        rides ``iter_entries``, never the whole-chain ``read_entries`` list.
        ``raising=False``: post-fix the module no longer binds
        ``read_entries`` at all; pre-fix the module-global lookup resolved at
        call time, so this spy intercepts and FAILS the pre-fix code."""
        from brutex.orchestrator import replay as replay_mod

        def _boom(_path: Path) -> list[object]:
            raise AssertionError("read_entries called — chain materialised")

        monkeypatch.setattr(replay_mod, "read_entries", _boom, raising=False)
        path = tmp_path / "audit.jsonl"
        _write_audit(path=path, rows=[("add", "x", {})])
        diff = replay_audit_to_diff(audit_path=path, live_subjects=frozenset({"x"}))
        assert diff.is_consistent
        assert diff.replayed_count == 1


# ────────────────────────────────────────────────────────────────────────
# assert_dedup_consistent
# ────────────────────────────────────────────────────────────────────────


class TestAssertDedupConsistent:
    def test_silent_on_match(self, tmp_path: Path) -> None:
        path = tmp_path / "audit.jsonl"
        _write_audit(path=path, rows=[("add", "x", {})])
        # Must not raise
        assert_dedup_consistent(
            audit_path=path,
            live_subjects=frozenset({"x"}),
        )

    def test_raises_on_missing(self, tmp_path: Path) -> None:
        path = tmp_path / "audit.jsonl"
        _write_audit(path=path, rows=[("add", "x", {}), ("add", "y", {})])
        with pytest.raises(AuditError, match="missing_from_live=1"):
            assert_dedup_consistent(
                audit_path=path,
                live_subjects=frozenset({"x"}),
            )

    def test_raises_on_extra(self, tmp_path: Path) -> None:
        path = tmp_path / "audit.jsonl"
        _write_audit(path=path, rows=[("add", "x", {})])
        with pytest.raises(AuditError, match="extra_in_live=1"):
            assert_dedup_consistent(
                audit_path=path,
                live_subjects=frozenset({"x", "rogue"}),
            )

    def test_raises_on_both(self, tmp_path: Path) -> None:
        path = tmp_path / "audit.jsonl"
        _write_audit(path=path, rows=[("add", "x", {}), ("add", "y", {})])
        with pytest.raises(AuditError) as exc_info:
            assert_dedup_consistent(
                audit_path=path,
                live_subjects=frozenset({"x", "rogue"}),
            )
        # Message contains both counts
        msg = str(exc_info.value)
        assert "missing_from_live=1" in msg
        assert "extra_in_live=1" in msg

    def test_first_5_samples_only(self, tmp_path: Path) -> None:
        """When more than 5 missing/extra, the message truncates to 5."""
        path = tmp_path / "audit.jsonl"
        rows = [("add", f"subj-{i:02d}", {}) for i in range(10)]
        _write_audit(path=path, rows=rows)
        with pytest.raises(AuditError) as exc_info:
            assert_dedup_consistent(
                audit_path=path,
                live_subjects=frozenset(),  # nothing live → all missing
            )
        msg = str(exc_info.value)
        # 10 missing, message shows the first 5 (sorted) only
        assert "missing_from_live=10" in msg
        assert "subj-00" in msg
        assert "subj-04" in msg
        assert "subj-09" not in msg  # would be >5


# ────────────────────────────────────────────────────────────────────────
# Dataclass invariants
# ────────────────────────────────────────────────────────────────────────


class TestReplayDiffDataclass:
    def test_is_frozen(self) -> None:
        diff = ReplayDiff(
            intersection=frozenset(),
            missing_from_live=frozenset(),
            extra_in_live=frozenset(),
            replayed_count=0,
        )
        with pytest.raises(AttributeError):
            diff.replayed_count = 1  # type: ignore[misc]
