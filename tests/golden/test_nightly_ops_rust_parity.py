# pyright: reportPrivateUsage=none
"""REAL-crate flag-ON parity for the nightly seams (DEC-NIGHTLY-RUST-CORE-001).

Drives the COMPILED `brutex_ops` wheel (``pytest.importorskip`` — the main
CI test job never installs it; the wheel-free seam suites carry the CI
load) through the REAL routed seams in steady state (burn-in ``remaining=0``
so no oracle cushions a divergence) and pins the crate values equal to the
retained ``*_python`` oracles — the plist golden byte-for-byte included.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

brutex_ops = pytest.importorskip("brutex_ops")

from brutex.nightly import nightly_rust  # noqa: E402
from brutex.nightly.chain import (  # noqa: E402
    LinkOutcome,
    LinkStatus,
    NightlyLink,
    NightlyReport,
    Tripwire,
    audit_reconcile_tripwire,
    audit_reconcile_tripwire_python,
    exit_code_for,
    exit_code_for_python,
    first_breaker,
    first_breaker_python,
)
from brutex.nightly.report import _summary_lines_python, summary_lines  # noqa: E402
from brutex.nightly.schedule import (  # noqa: E402
    DEFAULT_LABEL,
    _parse_at_python,
    _pmset_wake_command_python,
    _render_launchd_plist_python,
    _seconds_since_scheduled_fire_python,
    parse_at,
    pmset_wake_command,
    render_launchd_plist,
    seconds_since_scheduled_fire,
)
from structlog.testing import capture_logs  # noqa: E402

if TYPE_CHECKING:
    from collections.abc import Iterator


def _outcome(
    link: NightlyLink,
    status: LinkStatus,
    *,
    attempted: bool = True,
    blocked_by: NightlyLink | None = None,
    exit_hint: int | None = None,
) -> LinkOutcome:
    return LinkOutcome(
        link=link,
        status=status,
        started_utc=10.0 if attempted else None,
        ended_utc=20.0 if attempted else None,
        detail="",
        counts={},
        error_type=None,
        blocked_by=blocked_by,
        exit_hint=exit_hint,
    )


@pytest.fixture(autouse=True)
def _steady_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Flag ON + burn-in EXHAUSTED: the served value is the CRATE's."""
    monkeypatch.setenv(nightly_rust.NIGHTLY_RUST_ENV, "1")
    nightly_rust.reset_nightly_rust_for_tests(remaining=0)
    yield
    nightly_rust.reset_nightly_rust_for_tests()


_RUNS = [
    (),
    (_outcome(NightlyLink.COLLISION, LinkStatus.OK),),
    (
        _outcome(NightlyLink.COLLISION, LinkStatus.OK),
        _outcome(NightlyLink.DISK, LinkStatus.REFUSED),
        _outcome(
            NightlyLink.BACKUP, LinkStatus.BLOCKED, attempted=False, blocked_by=NightlyLink.DISK
        ),
    ),
    (
        _outcome(NightlyLink.PULL, LinkStatus.OK),
        _outcome(NightlyLink.SWEEP, LinkStatus.FAILED, exit_hint=6),
    ),
]


class TestChainParityRealCrate:
    @pytest.mark.parametrize("outcomes", _RUNS, ids=str)
    def test_first_breaker_and_exit_code(self, outcomes: tuple[LinkOutcome, ...]) -> None:
        with capture_logs():
            assert first_breaker(outcomes) is first_breaker_python(outcomes)
            for tripwires in ((), (Tripwire(name="t", link=None, detail="d"),)):
                assert exit_code_for(outcomes, tripwires) == exit_code_for_python(
                    outcomes, tripwires
                )

    @pytest.mark.parametrize(
        "events",
        [
            (
                ("NIGHTLY_START", None),
                ("LINK_START", "pull"),
                ("LINK_DONE", "pull"),
                ("LINK_DONE", "prebuild"),
            ),
            (
                ("NIGHTLY_START", None),
                ("NIGHTLY_START", None),
                ("LINK_START", "sweep"),
                ("LINK_DONE", "pull"),
                ("LINK_DONE", "pull"),
            ),
            (("NIGHTLY_START", None), ("LINK_START", None)),
            (),
        ],
        ids=["clean", "torn", "none-link", "empty"],
    )
    def test_audit_reconcile(self, events: tuple[tuple[str, str | None], ...]) -> None:
        outcomes = (
            _outcome(NightlyLink.PULL, LinkStatus.OK),
            _outcome(NightlyLink.PREBUILD, LinkStatus.OK, attempted=False),
        )
        with capture_logs():
            routed = audit_reconcile_tripwire(nightly_id="n-real", events=events, outcomes=outcomes)
        assert routed == audit_reconcile_tripwire_python(
            nightly_id="n-real", events=events, outcomes=outcomes
        )


class TestReportParityRealCrate:
    def test_summary_lines_full_matrix(self) -> None:
        report = NightlyReport(
            schema_version=1,
            nightly_id="nightly-20260719T013000Z-real12",
            lake_root="/lake",
            results_root="/results",
            backup_root="/backups",
            started_utc=4_000.0,
            ended_utc=7_723.0,
            links=(
                _outcome(NightlyLink.COLLISION, LinkStatus.OK),
                _outcome(NightlyLink.BACKUP, LinkStatus.SKIPPED),
                _outcome(NightlyLink.PULL, LinkStatus.OK),
                _outcome(NightlyLink.PREBUILD, LinkStatus.OK),
                _outcome(NightlyLink.SWEEP, LinkStatus.FAILED, exit_hint=6),
            ),
            tripwires=(Tripwire(name="audit_reconcile", link=None, detail="torn"),),
            counts={"backup_files": None, "pull_bars": 375, "sweep_run": 4},
            snapshot_id=None,
            absent_artifacts=(),
            honesty_notes=("note",),
            dry_run=False,
        )
        with capture_logs():
            assert summary_lines(report) == _summary_lines_python(report)


class TestScheduleParityRealCrate:
    def test_plist_golden_byte_for_byte(self) -> None:
        args = ["/opt/bin/brutex", "nightly", "run"]
        env = {
            "BRUTEX_LAKE_ROOT": "/Volumes/lake",
            "BRUTEX_NIGHTLY_BACKUP_ROOT": "/Volumes/backups",
        }
        log_dir = Path("/Users/op/.brutex/logs")
        with capture_logs():
            rendered = render_launchd_plist(
                label=DEFAULT_LABEL,
                program_arguments=args,
                hour=1,
                minute=30,
                env_vars=env,
                log_dir=log_dir,
            )
        assert rendered == _render_launchd_plist_python(
            label=DEFAULT_LABEL,
            program_arguments=args,
            hour=1,
            minute=30,
            env_vars=env,
            log_dir=log_dir,
        )
        # The absolute golden (the test_nightly_schedule.py expectation) —
        # served by the CRATE in this steady state, byte-for-byte.
        assert rendered.startswith('<?xml version="1.0" encoding="UTF-8"?>\n')
        assert "  <key>Label</key><string>com.brutex.nightly</string>\n" in rendered
        assert (
            "  <key>StandardOutPath</key>"
            "<string>/Users/op/.brutex/logs/nightly-launchd.out.log</string>\n" in rendered
        )

    @pytest.mark.parametrize("text", ["01:30", "23:59", " 9:05 ", "0:00", "20:00"])
    def test_parse_at(self, text: str) -> None:
        with capture_logs():
            assert parse_at(text) == _parse_at_python(text)

    @pytest.mark.parametrize("junk", ["24:00", "1:5", "noon", "", "01:60"])
    def test_parse_at_junk_raises(self, junk: str) -> None:
        with capture_logs(), pytest.raises(ValueError, match="--at must be HH:MM"):
            parse_at(junk)

    def test_pmset_and_seconds_since_fire(self) -> None:
        with capture_logs():
            for hour, minute in [(1, 30), (0, 2), (23, 59)]:
                assert pmset_wake_command(hour, minute) == _pmset_wake_command_python(
                    hour, minute, 5
                )
            for now in [
                datetime(2026, 7, 19, 2, 0, 0),
                datetime(2026, 7, 19, 1, 29, 59, 999_999),
                datetime(2026, 8, 1, 0, 0, 30, 250_000),
            ]:
                assert seconds_since_scheduled_fire(
                    hour=1, minute=30, now_local=now
                ) == _seconds_since_scheduled_fire_python(hour=1, minute=30, now_local=now)


class TestInputRefusedMarkerRealCrate:
    """The CLASS-C marker contract, proven against the COMPILED crate.

    DEC-RUST-STRICT-NO-FALLBACK-001. The wheel-free seam suites drive this
    split through ``tests/fakes/fake_brutex_ops.py``; these pins prove the
    REAL crate mints exactly the same evidence, so the facade's
    ``_degrade_reason`` classification is not a fiction of the fake:

    * every core whose ``Err`` is provably input-only STAMPS
      :data:`~brutex.nightly.nightly_rust._INPUT_REFUSED_MARKER`;
    * every core whose ``Err`` is a facade-contract FAULT leaves it OFF, so
      strict mode refuses it.
    """

    def test_marked_cores_stamp_the_marker(self) -> None:
        marker = nightly_rust._INPUT_REFUSED_MARKER
        cases: list[tuple[str, object]] = [
            (
                "nightly_summary_lines",
                lambda: brutex_ops.nightly_summary_lines(
                    "id", 0.0, float("inf"), [("collision", "ok", None)], [], [], [], False, False
                ),
            ),
            ("nightly_parse_at", lambda: brutex_ops.nightly_parse_at("0٣:05")),
            (
                "nightly_pmset_wake_command",
                lambda: brutex_ops.nightly_pmset_wake_command(2**70, 0, 0),
            ),
            (
                "nightly_seconds_since_fire",
                lambda: brutex_ops.nightly_seconds_since_fire(24, 0, 1, 0, 0, 0),
            ),
        ]
        for name, call in cases:
            with pytest.raises(ValueError) as excinfo:
                call()  # type: ignore[operator]
            assert marker in str(excinfo.value), f"{name} refusal is UNMARKED"

    def test_fault_only_cores_leave_the_marker_off(self) -> None:
        """A length mismatch is a facade BUG — the crate must not mark it."""
        marker = nightly_rust._INPUT_REFUSED_MARKER
        with pytest.raises(ValueError) as excinfo:
            brutex_ops.nightly_exit_code(["ok"], [], False)
        assert marker not in str(excinfo.value)
        with pytest.raises(ValueError) as excinfo:
            brutex_ops.nightly_audit_reconcile_detail("id", [], ["pull"], [], "", "", "", "")
        assert marker not in str(excinfo.value)

    def test_routed_seam_classifies_the_real_crate_refusal(self) -> None:
        """End-to-end: a REAL crate input refusal reads ``*_input_refused``."""
        text = "0٣:05"  # ARABIC-INDIC DIGIT THREE
        with capture_logs() as logs:
            assert parse_at(text) == _parse_at_python(text) == (3, 5)
        warns = [entry for entry in logs if entry["event"] == "nightly_rust.fallback"]
        assert [entry["reason"] for entry in warns] == ["parse_at_input_refused"]
