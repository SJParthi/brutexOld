# pyright: reportPrivateUsage=none
"""Tests for `brutex.nightly.report` (DEC-SWEEP-NIGHTLY-AUTOMATION-001).

Id format/uniqueness, the atomic report + bundle writers (contents + the env
allowlist redaction), the resuming ``actor="nightly"`` audit opener, and the
pure ``summary_lines`` builder (SUSPECT, the skip-backup nag, ``"?"`` for
unknown counts, blocked(<link>) verdicts, duration formatting).
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Mapping
from pathlib import Path

import pytest
from brutex import __version__
from brutex.engine.sweep_audit import iter_entries, verify_chain
from brutex.nightly.chain import (
    LINK_ORDER,
    LinkOutcome,
    LinkStatus,
    NightlyLink,
    NightlyReport,
    Tripwire,
)
from brutex.nightly.report import (
    NIGHTLY_AUDIT_FILENAME,
    NIGHTLY_DIRNAME,
    REPORT_FILENAME,
    _allowlisted_env,
    _code_commit_sha,
    mint_nightly_id,
    nightly_audit_path,
    nightly_dir,
    open_nightly_audit,
    summary_lines,
    write_bundle,
    write_report,
)


def _outcome(
    link: NightlyLink,
    status: LinkStatus = LinkStatus.OK,
    *,
    blocked_by: NightlyLink | None = None,
) -> LinkOutcome:
    return LinkOutcome(
        link=link,
        status=status,
        started_utc=None if status is LinkStatus.BLOCKED else 10.0,
        ended_utc=None if status is LinkStatus.BLOCKED else 20.0,
        detail="",
        counts={},
        error_type=None,
        blocked_by=blocked_by,
        exit_hint=None,
    )


def _report(
    *,
    links: tuple[LinkOutcome, ...] | None = None,
    tripwires: tuple[Tripwire, ...] = (),
    counts: Mapping[str, int | None] | None = None,
    dry_run: bool = False,
    honesty_notes: tuple[str, ...] = (),
    ended: float = 3661.0,
) -> NightlyReport:
    return NightlyReport(
        schema_version=1,
        nightly_id="nightly-20260708T013000Z-abc123",
        lake_root="/lake",
        results_root="/lake/sweeps",
        backup_root="/backups",
        started_utc=0.0,
        ended_utc=ended,
        links=links if links is not None else tuple(_outcome(link) for link in LINK_ORDER),
        tripwires=tripwires,
        counts=counts
        if counts is not None
        else {
            "backup_files": 12,
            "pull_bars": 345,
            "prebuild_built": 6,
            "sweep_run": 7,
            "sweep_skipped_cache": 8,
            "sweep_failed": 0,
        },
        snapshot_id=None,
        absent_artifacts=(),
        honesty_notes=honesty_notes,
        dry_run=dry_run,
    )


# ── id + paths ───────────────────────────────────────────────────────────


def test_mint_nightly_id_format_and_uniqueness() -> None:
    """Format golden + the uniqueness LAW, both DETERMINISTIC.

    The uniqueness law is "distinct entropy within one second ⇒ distinct ids",
    so it is asserted by INJECTING a counter entropy source through the seam
    the minter already exposes. The previous shape minted 64 ids off REAL
    ``os.urandom`` in one second and asserted all 64 unique — a probabilistic
    claim asserted deterministically: with a 24-bit suffix,
    P(collision) ≈ C(64,2)/2**24 ≈ 1.2e-4 per run, which randomly redded
    repo-wide CI (run 29141165245, ``assert 63 == 64``) — the defect class
    this pins away (DEC-NIGHTLY-ID-FLAKY-TEST-001, MIGRATION.md #1737).
    """
    minted = mint_nightly_id(now=lambda: 1785086400.0, entropy=lambda n: b"\xa1\xb2\xc3"[:n])
    assert minted == "nightly-20260726T172000Z-a1b2c3"
    counter = iter(range(64))
    ids = {
        mint_nightly_id(
            now=lambda: 1785086400.0,
            entropy=lambda n: next(counter).to_bytes(3, "big")[:n],
        )
        for _ in range(64)
    }
    assert len(ids) == 64  # distinct entropy ⇒ distinct ids within ONE second
    assert all(i.startswith("nightly-20260726T172000Z-") and "/" not in i for i in ids)


def test_mint_nightly_id_default_seams_format() -> None:
    """The real defaults (``time.time`` + ``os.urandom``) mint the pinned shape.

    Deliberately NO uniqueness assertion here — over real 24-bit entropy that
    would be the probabilistic-claim-asserted-deterministically flake
    test_mint_nightly_id_format_and_uniqueness documents (MIGRATION.md #1737).
    """
    minted = mint_nightly_id()
    assert re.fullmatch(r"nightly-\d{8}T\d{6}Z-[0-9a-f]{6}", minted)
    assert "/" not in minted  # filesystem-safe: it names the report dir


def test_paths_are_nightly_owned(tmp_path: Path) -> None:
    assert nightly_dir(tmp_path, "n-1") == tmp_path / NIGHTLY_DIRNAME / "n-1"
    assert nightly_audit_path(tmp_path) == (
        tmp_path / NIGHTLY_DIRNAME / "audit" / NIGHTLY_AUDIT_FILENAME
    )
    assert not (tmp_path / NIGHTLY_DIRNAME).exists()  # computed only


# ── audit opener ─────────────────────────────────────────────────────────


def test_open_nightly_audit_fresh_then_resume(tmp_path: Path) -> None:
    with open_nightly_audit(tmp_path) as audit:
        audit.append(action="NIGHTLY_START", subject="nightly:n-1", payload={})
    # Re-open RESUMES the same chain (no overwrite; hash chain stays green).
    with open_nightly_audit(tmp_path) as audit:
        audit.append(action="NIGHTLY_DONE", subject="nightly:n-1", payload={})
    path = nightly_audit_path(tmp_path)
    assert verify_chain(path) == 2
    entries = list(iter_entries(path))
    assert [e.action for e in entries] == ["NIGHTLY_START", "NIGHTLY_DONE"]
    assert all(e.actor == "nightly" for e in entries)


# ── report writer ────────────────────────────────────────────────────────


def test_write_report_atomic_and_parseable(tmp_path: Path) -> None:
    report = _report()
    dest = write_report(tmp_path, report)
    assert dest == nightly_dir(tmp_path, report.nightly_id) / REPORT_FILENAME
    assert json.loads(dest.read_text(encoding="utf-8")) == report.to_dict()


# ── bundle writer ────────────────────────────────────────────────────────


def test_write_bundle_contents_and_redaction(tmp_path: Path) -> None:
    report = _report()
    nightly_audit = tmp_path / "nightly.jsonl"
    nightly_audit.write_text(
        f'{{"subject": "nightly:{report.nightly_id}", "action": "NIGHTLY_START"}}\n'
        '{"subject": "nightly:OTHER-ID", "action": "NIGHTLY_START"}\n',
        encoding="utf-8",
    )
    sweep_audit = tmp_path / "sweep-all.jsonl"
    sweep_audit.write_text("".join(f'{{"seq": {i}}}\n' for i in range(250)), encoding="utf-8")
    progress = tmp_path / "sweep-progress.json"
    progress.write_text('{"state": "done"}', encoding="utf-8")
    env = {
        "BRUTEX_LAKE_ROOT": "/lake",
        "BRUTEX_NIGHTLY_BACKUP_ROOT": "/backups",
        "BRUTEX_PIPELINE_STRICT_GATE": "1",
        "BRUTEX_SWEEP_WORKERS": "1",
        "BRUTEX_TELEGRAM_BOT_TOKEN": "SECRET-NEVER-COPIED",
        "AWS_SECRET_ACCESS_KEY": "SECRET-NEVER-COPIED",
        "PATH": "/usr/bin",
    }
    bundle = write_bundle(
        tmp_path,
        report,
        sweep_audit_path=sweep_audit,
        nightly_audit_path=nightly_audit,
        progress_path=progress,
        env=env,
        code_commit=lambda: "deadbeef",
    )
    assert bundle == nightly_dir(tmp_path, report.nightly_id) / "bundle"
    # Report copy.
    assert json.loads((bundle / REPORT_FILENAME).read_text(encoding="utf-8")) == report.to_dict()
    # Own-run audit tail: ONLY this nightly's lines.
    tail = (bundle / "nightly-audit-tail.jsonl").read_text(encoding="utf-8")
    assert report.nightly_id in tail
    assert "OTHER-ID" not in tail
    # Sweep tail bounded to the last 200 lines.
    sweep_tail = (bundle / "sweep-audit-tail.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(sweep_tail) == 200
    assert sweep_tail[0] == '{"seq": 50}'
    # Progress copy present.
    assert (bundle / "sweep-progress.json").read_text(encoding="utf-8") == '{"state": "done"}'
    # env.json: allowlist ONLY — never token/key material.
    env_copy = json.loads((bundle / "env.json").read_text(encoding="utf-8"))
    assert env_copy == {
        "BRUTEX_LAKE_ROOT": "/lake",
        "BRUTEX_NIGHTLY_BACKUP_ROOT": "/backups",
        "BRUTEX_PIPELINE_STRICT_GATE": "1",
        "BRUTEX_SWEEP_WORKERS": "1",
    }
    assert "SECRET-NEVER-COPIED" not in (bundle / "env.json").read_text(encoding="utf-8")
    # versions.json (injected commit) + timings.json per link + honest README.
    versions = json.loads((bundle / "versions.json").read_text(encoding="utf-8"))
    assert versions["brutex"] == __version__
    assert versions["code_commit_sha"] == "deadbeef"
    timings = json.loads((bundle / "timings.json").read_text(encoding="utf-8"))
    assert set(timings) == {link.value for link in LINK_ORDER}
    assert timings["pull"] == {"started_utc": 10.0, "ended_utc": 20.0, "status": "ok"}
    assert "P2's full R40" in (bundle / "README.txt").read_text(encoding="utf-8")


def test_write_bundle_missing_members_are_skipped(tmp_path: Path) -> None:
    """Absent audit/progress files never break the bundle (forensics only)."""
    report = _report()
    bundle = write_bundle(
        tmp_path,
        report,
        sweep_audit_path=tmp_path / "absent-sweep.jsonl",
        nightly_audit_path=tmp_path / "absent-nightly.jsonl",
        progress_path=tmp_path / "absent-progress.json",
        env={},
        code_commit=lambda: "sha",
    )
    assert not (bundle / "nightly-audit-tail.jsonl").exists()
    assert not (bundle / "sweep-audit-tail.jsonl").exists()
    assert not (bundle / "sweep-progress.json").exists()
    assert (bundle / REPORT_FILENAME).exists()
    assert (bundle / "env.json").read_text(encoding="utf-8") == "{}"


def test_write_bundle_empty_audits_write_empty_tails(tmp_path: Path) -> None:
    report = _report()
    empty_a = tmp_path / "a.jsonl"
    empty_a.write_text("", encoding="utf-8")
    empty_b = tmp_path / "b.jsonl"
    empty_b.write_text("", encoding="utf-8")
    bundle = write_bundle(
        tmp_path,
        report,
        sweep_audit_path=empty_a,
        nightly_audit_path=empty_b,
        progress_path=tmp_path / "absent.json",
        env={},
        code_commit=lambda: "sha",
    )
    assert (bundle / "nightly-audit-tail.jsonl").read_text(encoding="utf-8") == ""
    assert (bundle / "sweep-audit-tail.jsonl").read_text(encoding="utf-8") == ""


def test_code_commit_sha_arms(monkeypatch: pytest.MonkeyPatch) -> None:
    # Injected runner wins.
    assert _code_commit_sha(lambda: "abc123") == "abc123"

    # Injected runner raising falls back honestly.
    def boom() -> str:
        raise RuntimeError("no git")

    assert _code_commit_sha(boom) == f"brutex-{__version__}"
    # The real subprocess path inside THIS repo returns a 40-hex sha.
    sha = _code_commit_sha()
    assert len(sha) == 40

    # subprocess failure arms.
    def raising_run(*args: object, **kwargs: object) -> object:
        raise OSError("git missing")

    monkeypatch.setattr("brutex.nightly.report.subprocess.run", raising_run)
    assert _code_commit_sha() == f"brutex-{__version__}"

    def failing_run(*args: object, **kwargs: object) -> object:
        return subprocess.CompletedProcess(args=["git"], returncode=1, stdout="", stderr="x")

    monkeypatch.setattr("brutex.nightly.report.subprocess.run", failing_run)
    assert _code_commit_sha() == f"brutex-{__version__}"


def test_allowlisted_env_is_sorted_and_exact() -> None:
    out = _allowlisted_env({"BRUTEX_NIGHTLY_Z": "z", "BRUTEX_LAKE_ROOT": "/l", "HOME": "/root"})
    assert list(out) == ["BRUTEX_LAKE_ROOT", "BRUTEX_NIGHTLY_Z"]


# ── summary_lines ────────────────────────────────────────────────────────


def test_summary_green_golden() -> None:
    lines, verdict = summary_lines(_report())
    assert verdict == "OK"
    assert lines[0] == "🌙 Nightly nightly-20260708T013000Z-abc123 — ✅ OK in 1:01."
    assert lines[1] == (
        "collision ok · disk ok · backup ok (12 files) · pull ok (345 bars) · "
        "prebuild ok (6 built) · sweep ok (7 run · 8 skipped-cache · 0 failed)"
    )
    assert len(lines) == 2


def test_summary_unknown_counts_render_question_marks() -> None:
    lines, _ = summary_lines(
        _report(
            counts={
                "backup_files": None,
                "pull_bars": None,
                "prebuild_built": None,
                "sweep_run": None,
                "sweep_skipped_cache": None,
                "sweep_failed": None,
            }
        )
    )
    assert "backup ok (? files)" in lines[1]
    assert "sweep ok (? run · ? skipped-cache · ? failed)" in lines[1]
    assert "0" not in lines[1]  # NEVER a fabricated zero


def test_summary_refused_with_blocked_links() -> None:
    links = (
        _outcome(NightlyLink.COLLISION),
        _outcome(NightlyLink.DISK, LinkStatus.REFUSED),
        _outcome(NightlyLink.BACKUP, LinkStatus.BLOCKED, blocked_by=NightlyLink.DISK),
        _outcome(NightlyLink.PULL, LinkStatus.BLOCKED, blocked_by=NightlyLink.DISK),
        _outcome(NightlyLink.PREBUILD, LinkStatus.BLOCKED, blocked_by=NightlyLink.DISK),
        _outcome(NightlyLink.SWEEP, LinkStatus.BLOCKED, blocked_by=NightlyLink.DISK),
    )
    lines, verdict = summary_lines(_report(links=links))
    assert verdict == "REFUSED"
    assert "⛔ REFUSED at disk" in lines[0]
    assert "backup blocked(disk)" in lines[1]
    assert "sweep blocked(disk)" in lines[1]
    assert "(12 files)" not in lines[1]  # a blocked link never fakes counts


def test_summary_failed_and_suspect_and_notes() -> None:
    links = tuple(
        _outcome(link, LinkStatus.FAILED if link is NightlyLink.PULL else LinkStatus.OK)
        for link in LINK_ORDER
    )
    wires = (Tripwire(name="audit_reconcile", link=None, detail="off by one"),)
    lines, verdict = summary_lines(
        _report(links=links, tripwires=wires, honesty_notes=("R48 deferred",))
    )
    assert verdict == "FAILED"  # a breaker outranks SUSPECT in the headline
    assert "❌ FAILED at pull" in lines[0]
    assert any("tripwire audit_reconcile: off by one" in line for line in lines)
    assert any(line == "ℹ️ R48 deferred" for line in lines)


def test_summary_suspect_headline_when_all_green() -> None:
    lines, verdict = summary_lines(
        _report(tripwires=(Tripwire(name="snapshot_unreadable", link=None, detail="x"),))
    )
    assert verdict == "SUSPECT"
    assert "⚠️ SUSPECT" in lines[0]


def test_summary_skip_backup_nag_and_dry_run_suppression() -> None:
    links = tuple(
        _outcome(link, LinkStatus.SKIPPED if link is NightlyLink.BACKUP else LinkStatus.OK)
        for link in LINK_ORDER
    )
    lines, _ = summary_lines(_report(links=links))
    assert "⚠️ backup SKIPPED by operator flag — stores unprotected tonight." in lines
    dry_lines, _ = summary_lines(_report(links=links, dry_run=True))
    assert not any("unprotected tonight" in line for line in dry_lines)


def test_summary_negative_duration_clamps_to_zero() -> None:
    lines, _ = summary_lines(_report(ended=-5.0))
    assert "in 0:00." in lines[0]
