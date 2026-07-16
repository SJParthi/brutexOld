"""Unit suite for :mod:`brutex.dashboard.health_hub` (DEC-OPERATOR-HEALTH-TRUTH-001).

100% line+branch: every lane state — green / red / amber / unknown /
not-wired / corrupt-state-file NAMED degrade — plus the fold law and the TTL
memo. All file lanes are driven through REAL durable state files written by
the REAL writers (``brutex.backup.state.save_state`` / a real
``report.json``), never hand-rolled parallel formats.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from brutex.backup.state import empty_backup_state, save_state
from brutex.dashboard import health_hub
from brutex.dashboard.health_hub import (
    DEFAULT_TTL_S,
    LANE_BACKUP,
    LANE_DOCTOR,
    LANE_MUTATION,
    LANE_NIGHTLY,
    LANE_PULL,
    MAX_REPORT_BYTES,
    NIGHTLY_STALE_HOURS,
    VERDICT_BAD,
    VERDICT_IDLE,
    VERDICT_NOT_WIRED,
    VERDICT_OK,
    VERDICT_UNKNOWN,
    VERDICT_WARN,
    HealthHub,
    LaneStatus,
    _read_report_json,
    backup_deadman_lane,
    fold_verdict,
    nightly_lane,
)
from brutex.dashboard.pull_outcome import LastPullOutcome
from brutex.nightly.report import NIGHTLY_DIRNAME, REPORT_FILENAME

_NOW = 1_760_000_000.0  # a fixed epoch instant — every test derives from it


def _lane(verdict: str) -> LaneStatus:
    """A minimal lane carrying only the verdict (fold-law inputs)."""
    return LaneStatus(
        lane="x", verdict=verdict, headline="", detail="", checked_at=None, age_s=None
    )


class TestFoldLaw:
    """bad > (warn | unknown) > ok; idle/not_wired neutral (module docstring)."""

    def test_any_bad_folds_bad(self) -> None:
        assert fold_verdict([_lane(VERDICT_OK), _lane(VERDICT_WARN), _lane(VERDICT_BAD)]) == (
            VERDICT_BAD
        )

    def test_warn_folds_warn(self) -> None:
        assert fold_verdict([_lane(VERDICT_OK), _lane(VERDICT_WARN)]) == VERDICT_WARN

    def test_unknown_folds_warn(self) -> None:
        """An unreadable signal must LOOK degraded — never green."""
        assert fold_verdict([_lane(VERDICT_OK), _lane(VERDICT_UNKNOWN)]) == VERDICT_WARN

    def test_idle_and_not_wired_never_degrade(self) -> None:
        assert fold_verdict([_lane(VERDICT_OK), _lane(VERDICT_IDLE), _lane(VERDICT_NOT_WIRED)]) == (
            VERDICT_OK
        )

    def test_fold_headline_without_not_wired_lanes(self) -> None:
        """The 'not wired' suffix appears ONLY when such lanes exist."""
        from brutex.dashboard.health_hub import _fold_headline

        text = _fold_headline([_lane(VERDICT_OK), _lane(VERDICT_BAD)])
        assert text == "1 bad · 1 ok"
        assert "not wired" not in text


class TestPullLaneProjection:
    """Pull-outcome tone → lane verdict (via the HealthHub payload)."""

    @pytest.mark.parametrize(
        ("tone", "verdict"),
        [("ok", VERDICT_OK), ("warn", VERDICT_WARN), ("bad", VERDICT_BAD), ("idle", VERDICT_IDLE)],
    )
    def test_tone_maps_one_to_one(self, tmp_path: Path, tone: str, verdict: str) -> None:
        hub = _hub(tmp_path, tone=tone)
        payload = hub.payload()
        pull = _lane_by_name(payload, LANE_PULL)
        assert pull["verdict"] == verdict
        assert pull["headline"] == f"headline-{tone}"
        assert pull["detail"] == f"detail-{tone}"
        assert pull["checked_at"] is None
        assert pull["age_s"] is None


def _report(
    *,
    ok: Any = True,
    suspect: Any = False,
    blocker: Any = None,
    ended_utc: Any = _NOW - 3600.0,
    dry_run: bool = False,
) -> dict[str, Any]:
    """A report.json-shaped dict (only the fields the lane reads + extras)."""
    return {
        "schema": "brutex-nightly-report-v1",
        "nightly_id": "nightly-20261008T120000Z-abc123",
        "ok": ok,
        "suspect": suspect,
        "blocker": blocker,
        "ended_utc": ended_utc,
        "dry_run": dry_run,
        "exit_code": 0 if ok is True else 2,
    }


def _write_report(results_root: Path, nightly_id: str, report: dict[str, Any]) -> Path:
    run_dir = results_root / NIGHTLY_DIRNAME / nightly_id
    run_dir.mkdir(parents=True)
    dest = run_dir / REPORT_FILENAME
    dest.write_text(json.dumps(report), encoding="utf-8")
    return dest


class TestNightlyLane:
    """not_wired / unknown-corrupt / ok / overdue / suspect / failed."""

    def test_no_results_root_is_not_wired(self) -> None:
        lane = nightly_lane(None, now=_NOW)
        assert lane.verdict == VERDICT_NOT_WIRED
        assert "no lake root" in lane.headline

    def test_missing_nightly_dir_is_not_wired(self, tmp_path: Path) -> None:
        lane = nightly_lane(tmp_path / "sweeps", now=_NOW)
        assert lane.verdict == VERDICT_NOT_WIRED
        assert "no nightly report" in lane.headline

    def test_empty_nightly_dir_is_not_wired(self, tmp_path: Path) -> None:
        root = tmp_path / "sweeps"
        (root / NIGHTLY_DIRNAME / "audit").mkdir(parents=True)  # non-"nightly-" entry skipped
        (root / NIGHTLY_DIRNAME / "nightly-notadir").write_text("x")  # file skipped (not a dir)
        lane = nightly_lane(root, now=_NOW)
        assert lane.verdict == VERDICT_NOT_WIRED

    def test_ok_report_is_green_with_age(self, tmp_path: Path) -> None:
        root = tmp_path / "sweeps"
        _write_report(root, "nightly-20261008T120000Z-abc123", _report())
        lane = nightly_lane(root, now=_NOW)
        assert lane.verdict == VERDICT_OK
        assert "nightly ok" in lane.headline
        assert lane.checked_at == _NOW - 3600.0
        assert lane.age_s == 3600.0

    def test_latest_report_wins_lexicographic_utc(self, tmp_path: Path) -> None:
        root = tmp_path / "sweeps"
        _write_report(root, "nightly-20261007T120000Z-zzz999", _report(ok=False, blocker="pull"))
        _write_report(root, "nightly-20261008T120000Z-abc123", _report())
        lane = nightly_lane(root, now=_NOW)
        assert lane.verdict == VERDICT_OK  # the NEWER (ok) report wins

    def test_dry_run_ok_carries_honest_note(self, tmp_path: Path) -> None:
        root = tmp_path / "sweeps"
        _write_report(root, "nightly-20261008T120000Z-abc123", _report(dry_run=True))
        lane = nightly_lane(root, now=_NOW)
        assert lane.verdict == VERDICT_OK
        assert "(dry run)" in lane.headline

    def test_stale_ok_report_is_overdue_warn(self, tmp_path: Path) -> None:
        root = tmp_path / "sweeps"
        old = _NOW - (NIGHTLY_STALE_HOURS + 5.0) * 3600.0
        _write_report(root, "nightly-20261008T120000Z-abc123", _report(ended_utc=old))
        lane = nightly_lane(root, now=_NOW)
        assert lane.verdict == VERDICT_WARN
        assert "OVERDUE" in lane.headline

    def test_ok_without_numeric_ended_utc_stays_green_no_fabricated_age(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "sweeps"
        _write_report(root, "nightly-20261008T120000Z-abc123", _report(ended_utc="soon"))
        lane = nightly_lane(root, now=_NOW)
        assert lane.verdict == VERDICT_OK
        assert lane.checked_at is None
        assert lane.age_s is None

    def test_suspect_report_is_warn(self, tmp_path: Path) -> None:
        root = tmp_path / "sweeps"
        _write_report(root, "nightly-20261008T120000Z-abc123", _report(ok=False, suspect=True))
        lane = nightly_lane(root, now=_NOW)
        assert lane.verdict == VERDICT_WARN
        assert "SUSPECT" in lane.headline

    def test_failed_report_is_bad_naming_blocker(self, tmp_path: Path) -> None:
        root = tmp_path / "sweeps"
        _write_report(root, "nightly-20261008T120000Z-abc123", _report(ok=False, blocker="pull"))
        lane = nightly_lane(root, now=_NOW)
        assert lane.verdict == VERDICT_BAD
        assert "blocker: pull" in lane.headline

    def test_suspect_with_blocker_is_bad(self, tmp_path: Path) -> None:
        """A breaker + tripwires reads BAD (the breaker outranks SUSPECT)."""
        root = tmp_path / "sweeps"
        _write_report(
            root, "nightly-20261008T120000Z-abc123", _report(ok=False, suspect=True, blocker="pull")
        )
        lane = nightly_lane(root, now=_NOW)
        assert lane.verdict == VERDICT_BAD

    def test_failed_report_without_blocker_names_unknown_link(self, tmp_path: Path) -> None:
        root = tmp_path / "sweeps"
        _write_report(root, "nightly-20261008T120000Z-abc123", _report(ok=False))
        lane = nightly_lane(root, now=_NOW)
        assert lane.verdict == VERDICT_BAD
        assert "unknown link" in lane.headline

    def test_missing_report_json_is_unknown(self, tmp_path: Path) -> None:
        root = tmp_path / "sweeps"
        (root / NIGHTLY_DIRNAME / "nightly-20261008T120000Z-abc123").mkdir(parents=True)
        lane = nightly_lane(root, now=_NOW)
        assert lane.verdict == VERDICT_UNKNOWN
        assert "no report.json" in lane.detail

    def test_corrupt_json_is_unknown_named(self, tmp_path: Path) -> None:
        root = tmp_path / "sweeps"
        run_dir = root / NIGHTLY_DIRNAME / "nightly-20261008T120000Z-abc123"
        run_dir.mkdir(parents=True)
        (run_dir / REPORT_FILENAME).write_text("{not json", encoding="utf-8")
        lane = nightly_lane(root, now=_NOW)
        assert lane.verdict == VERDICT_UNKNOWN
        assert "corrupt" in lane.detail

    def test_non_utf8_report_is_unknown_named(self, tmp_path: Path) -> None:
        root = tmp_path / "sweeps"
        run_dir = root / NIGHTLY_DIRNAME / "nightly-20261008T120000Z-abc123"
        run_dir.mkdir(parents=True)
        (run_dir / REPORT_FILENAME).write_bytes(b"\xff\xfe{}")
        lane = nightly_lane(root, now=_NOW)
        assert lane.verdict == VERDICT_UNKNOWN

    def test_non_object_report_is_unknown_named(self, tmp_path: Path) -> None:
        root = tmp_path / "sweeps"
        run_dir = root / NIGHTLY_DIRNAME / "nightly-20261008T120000Z-abc123"
        run_dir.mkdir(parents=True)
        (run_dir / REPORT_FILENAME).write_text("[1, 2]", encoding="utf-8")
        lane = nightly_lane(root, now=_NOW)
        assert lane.verdict == VERDICT_UNKNOWN
        assert "not a JSON object" in lane.detail

    def test_over_cap_report_is_named_oversized(self, tmp_path: Path) -> None:
        """ONE byte past the cap → the named oversized degrade (never parsed)."""
        root = tmp_path / "sweeps"
        run_dir = root / NIGHTLY_DIRNAME / "nightly-20261008T120000Z-abc123"
        run_dir.mkdir(parents=True)
        (run_dir / REPORT_FILENAME).write_bytes(b"x" * (MAX_REPORT_BYTES + 1))
        lane = nightly_lane(root, now=_NOW)
        assert lane.verdict == VERDICT_UNKNOWN
        assert "oversized" in lane.detail

    def test_exactly_at_cap_report_still_parses(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """m4 boundary: a report of EXACTLY the cap loads whole (no off-by-one
        truncation from the bounded read); one byte more is oversized."""
        monkeypatch.setattr(health_hub, "MAX_REPORT_BYTES", 512)
        root = tmp_path / "sweeps"
        run_dir = root / NIGHTLY_DIRNAME / "nightly-20261008T120000Z-abc123"
        run_dir.mkdir(parents=True)
        body = dict(_report())
        raw = json.dumps(body).encode("utf-8")
        pad = 512 - len(raw) - len('"pad": ""') - 2  # splice a pad key to hit the cap exactly
        body["pad"] = "p" * pad
        raw = json.dumps(body).encode("utf-8")
        assert len(raw) == 512
        (run_dir / REPORT_FILENAME).write_bytes(raw)
        lane = nightly_lane(root, now=_NOW)
        assert lane.verdict == VERDICT_OK
        (run_dir / REPORT_FILENAME).write_bytes(raw + b"\n")
        lane = nightly_lane(root, now=_NOW)
        assert lane.verdict == VERDICT_UNKNOWN
        assert "oversized" in lane.detail

    def test_report_read_is_bounded_at_cap_plus_probe_byte(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """m4 teeth: an out-of-band report FAR past the cap costs at most
        cap + 1 read bytes — the ``read(cap)`` + ONE-byte over-probe idiom
        (#1614), never a whole-file ``read_bytes()``.  Counted through a
        read-counting handle wrapped around the REAL guarded opener
        (DEC-LAKE-READ-INODE-GUARD-001 moved the open onto
        ``open_regular_binary``), so a revert to the unbounded read fails
        here."""
        monkeypatch.setattr(health_hub, "MAX_REPORT_BYTES", 256)
        big = tmp_path / "report.json"
        big.write_bytes(b"x" * (256 + 4096))
        bytes_read = 0

        class _CountingHandle:
            def __init__(self, inner: Any) -> None:
                self._inner = inner

            def __enter__(self) -> _CountingHandle:
                return self

            def __exit__(self, *exc_info: object) -> None:
                self._inner.close()

            def read(self, n: int = -1) -> bytes:
                nonlocal bytes_read
                data: bytes = self._inner.read(n)
                bytes_read += len(data)
                return data

        real_open = health_hub.open_regular_binary

        def _counting_open(path: Path) -> _CountingHandle:
            return _CountingHandle(real_open(path))

        monkeypatch.setattr(health_hub, "open_regular_binary", _counting_open)
        verdict = _read_report_json(big)
        assert isinstance(verdict, str) and "oversized" in verdict
        assert bytes_read <= 256 + 1

    def test_deep_nested_report_is_unknown_never_a_crash(self, tmp_path: Path) -> None:
        """Hostile-review M1: a 60k-bracket nesting bomb makes ``json.loads``
        raise ``RecursionError`` — previously UNCAUGHT (it escaped the parse
        clause and 500'd the hub route); now the named corrupt degrade."""
        root = tmp_path / "sweeps"
        run_dir = root / NIGHTLY_DIRNAME / "nightly-20261008T120000Z-abc123"
        run_dir.mkdir(parents=True)
        (run_dir / REPORT_FILENAME).write_bytes(b"[" * 60_000)
        lane = nightly_lane(root, now=_NOW)
        assert lane.verdict == VERDICT_UNKNOWN
        assert "RecursionError" in lane.detail

    def test_report_json_as_directory_is_unknown(self, tmp_path: Path) -> None:
        """An unreadable inode (a DIRECTORY at report.json) degrades named."""
        root = tmp_path / "sweeps"
        run_dir = root / NIGHTLY_DIRNAME / "nightly-20261008T120000Z-abc123"
        (run_dir / REPORT_FILENAME).mkdir(parents=True)  # report.json is a DIR
        lane = nightly_lane(root, now=_NOW)
        assert lane.verdict == VERDICT_UNKNOWN
        assert "unreadable" in lane.detail

    def test_schema_drift_missing_ok_flag_is_unknown(self, tmp_path: Path) -> None:
        root = tmp_path / "sweeps"
        report = _report()
        del report["ok"]
        _write_report(root, "nightly-20261008T120000Z-abc123", report)
        lane = nightly_lane(root, now=_NOW)
        assert lane.verdict == VERDICT_UNKNOWN
        assert "schema drift" in lane.detail

    def test_nightly_dir_listing_error_is_not_wired(self, tmp_path: Path) -> None:
        """A FILE where the nightly dir should be → iterdir OSError → not_wired."""
        root = tmp_path / "sweeps"
        root.mkdir()
        (root / NIGHTLY_DIRNAME).write_text("not a dir", encoding="utf-8")
        lane = nightly_lane(root, now=_NOW)
        assert lane.verdict == VERDICT_NOT_WIRED


def _backup_env(tmp_path: Path, *, repo: bool = True) -> dict[str, str]:
    env = {"BRUTEX_BACKUP_STATE_DIR": str(tmp_path / "backup-state")}
    if repo:
        env["BRUTEX_BACKUP_REPO"] = "s3:s3.example/brutex-backup"
    return env


def _write_backup_state(tmp_path: Path, **overrides: Any) -> None:
    state_dir = tmp_path / "backup-state"
    state_dir.mkdir(parents=True, exist_ok=True)
    save_state(state_dir / "state.json", replace(empty_backup_state(), **overrides))


class TestBackupDeadmanLane:
    """not_wired / FRESH / RUNNING / alarms / corrupt state — via the REAL core."""

    def test_unconfigured_is_not_wired(self, tmp_path: Path) -> None:
        lane = backup_deadman_lane(now=_NOW, env=_backup_env(tmp_path, repo=False))
        assert lane.verdict == VERDICT_NOT_WIRED
        assert "not configured" in lane.headline

    def test_repo_configured_but_never_succeeded_is_bad(self, tmp_path: Path) -> None:
        """Installed-and-silent = alarm (the checker's own silence law)."""
        lane = backup_deadman_lane(now=_NOW, env=_backup_env(tmp_path))
        assert lane.verdict == VERDICT_BAD
        assert "never_succeeded" in lane.headline

    def test_fresh_success_is_green(self, tmp_path: Path) -> None:
        _write_backup_state(tmp_path, last_success_at=datetime.fromtimestamp(_NOW - 3600.0, UTC))
        # Deliberately NO repo in env: a state file is a positive local signal
        # even when the poller's env lacks the repo secret config.
        lane = backup_deadman_lane(now=_NOW, env=_backup_env(tmp_path, repo=False))
        assert lane.verdict == VERDICT_OK
        assert "fresh" in lane.headline
        assert lane.age_s == pytest.approx(3600.0)
        assert lane.checked_at == pytest.approx(_NOW - 3600.0)

    def test_running_is_green_nonalarming(self, tmp_path: Path) -> None:
        _write_backup_state(
            tmp_path,
            running_since=datetime.fromtimestamp(_NOW - 600.0, UTC),
            last_success_at=datetime.fromtimestamp(_NOW - 7200.0, UTC),
        )
        lane = backup_deadman_lane(now=_NOW, env=_backup_env(tmp_path))
        assert lane.verdict == VERDICT_OK
        assert "running" in lane.headline

    def test_overdue_is_bad(self, tmp_path: Path) -> None:
        _write_backup_state(
            tmp_path, last_success_at=datetime.fromtimestamp(_NOW - 40 * 3600.0, UTC)
        )
        lane = backup_deadman_lane(now=_NOW, env=_backup_env(tmp_path))
        assert lane.verdict == VERDICT_BAD
        assert "overdue" in lane.headline

    def test_clock_skew_is_bad(self, tmp_path: Path) -> None:
        _write_backup_state(tmp_path, last_success_at=datetime.fromtimestamp(_NOW + 7200.0, UTC))
        lane = backup_deadman_lane(now=_NOW, env=_backup_env(tmp_path))
        assert lane.verdict == VERDICT_BAD
        assert "clock_skew" in lane.headline

    def test_corrupt_state_file_is_named_alarm_never_a_crash(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "backup-state"
        state_dir.mkdir(parents=True)
        (state_dir / "state.json").write_text("{torn", encoding="utf-8")
        lane = backup_deadman_lane(now=_NOW, env=_backup_env(tmp_path, repo=False))
        assert lane.verdict == VERDICT_BAD
        assert "state_corrupt" in lane.headline
        assert "state.json" in lane.detail


def _hub(
    tmp_path: Path,
    *,
    tone: str = "idle",
    lake_root: Path | None = None,
    ttl_s: float = DEFAULT_TTL_S,
    clock: Any = None,
) -> HealthHub:
    """A hub over a deterministic probe + an isolated backup env."""
    outcome = LastPullOutcome(
        outcome="x", tone=tone, headline=f"headline-{tone}", detail=f"detail-{tone}"
    )
    ticks = {"now": _NOW}

    def _clock() -> float:
        return ticks["now"]

    hub = HealthHub(
        lake_root_resolver=lambda: lake_root,
        pull_outcome_probe=lambda: outcome,
        env=_backup_env(tmp_path, repo=False),
        clock=clock if clock is not None else _clock,
        ttl_s=ttl_s,
    )
    hub._test_ticks = ticks  # type: ignore[attr-defined]  # test-only handle
    return hub


def _lane_by_name(payload: dict[str, Any], lane: str) -> dict[str, Any]:
    matches = [entry for entry in payload["lanes"] if entry["lane"] == lane]
    assert len(matches) == 1, f"expected exactly one {lane} lane"
    return matches[0]


class TestHealthHubPayload:
    """Payload shape, lane order, the not-wired constants, fold + TTL memo."""

    def test_payload_shape_and_lane_order(self, tmp_path: Path) -> None:
        payload = _hub(tmp_path).payload()
        assert set(payload) == {"fold", "lanes", "generated_at", "ttl_s"}
        assert [entry["lane"] for entry in payload["lanes"]] == [
            LANE_PULL,
            LANE_NIGHTLY,
            LANE_BACKUP,
            LANE_DOCTOR,
            LANE_MUTATION,
        ]
        for entry in payload["lanes"]:
            assert set(entry) == {"lane", "verdict", "headline", "detail", "checked_at", "age_s"}
        assert payload["generated_at"] == _NOW
        assert payload["ttl_s"] == DEFAULT_TTL_S

    def test_doctor_and_mutation_lanes_read_not_wired_never_green(self, tmp_path: Path) -> None:
        payload = _hub(tmp_path).payload()
        doctor = _lane_by_name(payload, LANE_DOCTOR)
        mutation = _lane_by_name(payload, LANE_MUTATION)
        assert doctor["verdict"] == VERDICT_NOT_WIRED
        assert "doctor-daily.yml" in doctor["detail"]
        assert mutation["verdict"] == VERDICT_NOT_WIRED
        assert "mutation.yml" in mutation["detail"]

    def test_all_neutral_lanes_fold_ok_with_honest_headline(self, tmp_path: Path) -> None:
        payload = _hub(tmp_path).payload()
        assert payload["fold"]["verdict"] == VERDICT_OK
        assert "not wired" in payload["fold"]["headline"]
        assert "idle" in payload["fold"]["headline"]

    def test_bad_pull_lane_folds_red(self, tmp_path: Path) -> None:
        payload = _hub(tmp_path, tone="bad").payload()
        assert payload["fold"]["verdict"] == VERDICT_BAD
        assert "1 bad" in payload["fold"]["headline"]

    def test_nightly_lane_reads_real_report_via_lake_root(self, tmp_path: Path) -> None:
        lake = tmp_path / "lake"
        _write_report(lake / "sweeps", "nightly-20261008T120000Z-abc123", _report())
        payload = _hub(tmp_path, lake_root=lake).payload()
        nightly = _lane_by_name(payload, LANE_NIGHTLY)
        assert nightly["verdict"] == VERDICT_OK

    def test_ttl_memo_serves_file_lanes_then_refreshes(self, tmp_path: Path) -> None:
        lake = tmp_path / "lake"
        hub = _hub(tmp_path, lake_root=lake, ttl_s=5.0)
        ticks = hub._test_ticks  # type: ignore[attr-defined]
        first = hub.payload()
        assert _lane_by_name(first, LANE_NIGHTLY)["verdict"] == VERDICT_NOT_WIRED
        # A report lands; within the TTL the memoised lane still serves.
        _write_report(lake / "sweeps", "nightly-20261008T120000Z-abc123", _report())
        ticks["now"] = _NOW + 1.0
        within = hub.payload()
        assert _lane_by_name(within, LANE_NIGHTLY)["verdict"] == VERDICT_NOT_WIRED
        # Past the TTL the lane recomputes and sees the new report.
        ticks["now"] = _NOW + 6.0
        after = hub.payload()
        assert _lane_by_name(after, LANE_NIGHTLY)["verdict"] == VERDICT_OK

    def test_contended_lock_serves_last_known_without_blocking(self, tmp_path: Path) -> None:
        """The non-blocking follower law: a held memo lock serves LAST-KNOWN."""
        lake = tmp_path / "lake"
        hub = _hub(tmp_path, lake_root=lake, ttl_s=0.0)  # every poll would refresh
        first = hub.payload()
        assert _lane_by_name(first, LANE_NIGHTLY)["verdict"] == VERDICT_NOT_WIRED
        # A report lands; with the lock HELD (a refresh in flight elsewhere)
        # the follower serves the memoised lanes instead of blocking/refreshing.
        _write_report(lake / "sweeps", "nightly-20261008T120000Z-abc123", _report())
        assert hub._lock.acquire(blocking=False)
        try:
            follower = hub.payload()
        finally:
            hub._lock.release()
        assert _lane_by_name(follower, LANE_NIGHTLY)["verdict"] == VERDICT_NOT_WIRED
        # Lock released → the next poll refreshes and sees the report.
        after = hub.payload()
        assert _lane_by_name(after, LANE_NIGHTLY)["verdict"] == VERDICT_OK

    def test_contended_lock_cold_serves_honest_pending_unknown(self, tmp_path: Path) -> None:
        """Before the FIRST refresh ever lands, a contended poll reads pending unknown."""
        hub = _hub(tmp_path)
        assert hub._lock.acquire(blocking=False)
        try:
            payload = hub.payload()
        finally:
            hub._lock.release()
        nightly = _lane_by_name(payload, LANE_NIGHTLY)
        backup = _lane_by_name(payload, LANE_BACKUP)
        assert nightly["verdict"] == VERDICT_UNKNOWN
        assert backup["verdict"] == VERDICT_UNKNOWN
        assert "in flight" in nightly["headline"]
        # unknown folds WARN — a pending probe never reads green.
        assert payload["fold"]["verdict"] == VERDICT_WARN

    def test_degrade_log_emits_on_change_only_and_recovers(self, tmp_path: Path) -> None:
        """ONE `health_hub.lane_degraded` WARN per transition (never per poll);
        all-clear logs ONE `health_hub.lanes_recovered` INFO (the CodeQL-flagged
        `_LOG` binding is genuinely load-bearing)."""
        import structlog

        lake = tmp_path / "lake"
        run_dir = lake / "sweeps" / NIGHTLY_DIRNAME / "nightly-20261008T120000Z-abc123"
        run_dir.mkdir(parents=True)
        report_file = run_dir / REPORT_FILENAME
        report_file.write_text("{torn", encoding="utf-8")  # a corrupt report → unknown
        hub = _hub(tmp_path, lake_root=lake, ttl_s=0.0)  # every poll refreshes
        with structlog.testing.capture_logs() as logs:
            hub.payload()  # transition healthy(∅) → degraded: ONE WARN
            hub.payload()  # SAME degraded signature: no new log
        degraded_events = [e for e in logs if e["event"] == "health_hub.lane_degraded"]
        assert len(degraded_events) == 1
        assert degraded_events[0]["lanes"][0]["lane"] == LANE_NIGHTLY
        # The report heals → ONE recovery INFO, then steady healthy logs nothing.
        report_file.write_text(json.dumps(_report()), encoding="utf-8")
        with structlog.testing.capture_logs() as logs:
            hub.payload()
            hub.payload()
        recovered = [e for e in logs if e["event"] == "health_hub.lanes_recovered"]
        assert len(recovered) == 1
        assert not [e for e in logs if e["event"] == "health_hub.lane_degraded"]

    def test_degrade_log_headline_is_repr_escaped_for_hostile_dir_names(
        self, tmp_path: Path
    ) -> None:
        """Hostile-review n6: a control-char-named nightly dir flows RAW into
        the lane headline (``nightly report unreadable — <dir>``) — the degrade
        LOG's ``headline`` field rides ``repr()`` so no forged log line lands
        (the #1594 rounds-7/8 discipline); the WIRE payload stays raw
        (textContent-rendered client-side)."""
        import structlog

        lake = tmp_path / "lake"
        evil = "nightly-20261008T120000Z-evil\nFAKE-LOG-LINE"
        run_dir = lake / "sweeps" / NIGHTLY_DIRNAME / evil
        run_dir.mkdir(parents=True)
        # A corrupt report → the UNKNOWN arm, whose headline embeds the dir name.
        (run_dir / REPORT_FILENAME).write_text("{torn", encoding="utf-8")
        hub = _hub(tmp_path, lake_root=lake, ttl_s=0.0)
        with structlog.testing.capture_logs() as logs:
            payload = hub.payload()
        degraded = [e for e in logs if e["event"] == "health_hub.lane_degraded"]
        assert len(degraded) == 1
        logged = next(
            entry["headline"] for entry in degraded[0]["lanes"] if entry["lane"] == LANE_NIGHTLY
        )
        assert "\n" not in logged  # repr-escaped — no raw newline forge channel
        assert "\\n" in logged
        # The wire payload keeps the RAW headline for the textContent renderer.
        assert "\n" in _lane_by_name(payload, LANE_NIGHTLY)["headline"]

    def test_healthy_refreshes_log_nothing(self, tmp_path: Path) -> None:
        """A never-degraded hub emits ZERO degrade/recovery events."""
        import structlog

        hub = _hub(tmp_path, ttl_s=0.0)  # not_wired lanes only — neutral, not degraded
        with structlog.testing.capture_logs() as logs:
            hub.payload()
            hub.payload()
        assert not [e for e in logs if str(e["event"]).startswith("health_hub.")]

    def test_pull_lane_is_never_ttl_delayed(self, tmp_path: Path) -> None:
        outcomes = [
            LastPullOutcome(outcome="running", tone="ok", headline="pull running", detail=""),
            LastPullOutcome(outcome="halted_disk_full", tone="bad", headline="HALTED", detail=""),
        ]
        hub = HealthHub(
            lake_root_resolver=lambda: None,
            pull_outcome_probe=lambda: outcomes[0],
            env=_backup_env(tmp_path, repo=False),
            clock=lambda: _NOW,
            ttl_s=3600.0,
        )
        assert _lane_by_name(hub.payload(), LANE_PULL)["verdict"] == VERDICT_OK
        outcomes[0] = outcomes[1]
        assert _lane_by_name(hub.payload(), LANE_PULL)["verdict"] == VERDICT_BAD
