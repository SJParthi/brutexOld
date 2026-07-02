"""Unit tests for :mod:`brutex.doctor_autorun` — the zero-command autorun engine.

DEC-DOCTOR-LIVE-AUTORUN-001. Everything runs OFFLINE: the kit runs ride the
SAME fake :class:`~brutex.doctor_live.KitDeps` seams as the kit's own suite
(``tests.unit.test_doctor_live.make_deps`` — one fake composition, CLAUDE.md
§11 COMMON), and the verbatim-diagnostics guarantee is proven by BYTE-EQUALITY
against a direct :func:`~brutex.doctor_live.run_live_doctor` run over
identical fakes. The live path itself stays honestly UNVERIFIED until the
operator's next normal start on the Mac — that is the autorun's purpose.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from brutex import doctor_autorun as da
from brutex import doctor_live as dl
from brutex.adapters.groww.constants import GROWW_RATE_LIMIT_PER_MIN
from brutex.adapters.rate_common import RateController
from brutex.config.credentials_types import CredentialsError, GrowwFileCredentials

from tests.unit.test_doctor_live import (
    _BHAVCOPY_DRIFTED,
    _SECRET,
    make_deps,
)

_NOW = datetime(2026, 7, 2, 9, 30, tzinfo=UTC)


def _now() -> datetime:
    return _NOW


def _sync_starter(fn: Callable[[], None], _name: str) -> None:
    """Run the worker synchronously — deterministic tests, no threads."""
    fn()


def _outcome(stage: int, name: str, status: str, *lines: str) -> dl.StageOutcome:
    return dl.StageOutcome(stage=stage, name=name, status=status, lines=tuple(lines))


def _report(*outcomes: dl.StageOutcome, run_id: str = "lp-test") -> dl.KitReport:
    return dl.KitReport(
        run_id=run_id, transcript_path=Path("/tmp/t.jsonl"), outcomes=tuple(outcomes)
    )


def _pass_preflight_report() -> dl.KitReport:
    return _report(
        _outcome(0, "env", dl.STATUS_PASS, "PASS env: ok"),
        _outcome(1, "creds", dl.STATUS_PASS, "PASS creds: ok"),
        _outcome(2, "totp", dl.STATUS_PASS, "PASS totp: ok"),
        _outcome(3, "token", dl.STATUS_PASS, "PASS token: ok"),
        _outcome(4, "master", dl.STATUS_PASS, "PASS master: ok"),
    )


def _holder(
    tmp_path: Path,
    *,
    env: Mapping[str, str] | None = None,
    credentials_error: Callable[[], str | None] | None = None,
    preflight_runner: Callable[[], dl.KitReport] | None = None,
    probes_runner: Callable[[], dl.KitReport] | None = None,
    thread_starter: Callable[[Callable[[], None], str], None] = _sync_starter,
    **kwargs: Any,
) -> da.LiveProofAutorun:
    """A test holder: creds OK + sync worker + fake runners by default."""
    return da.LiveProofAutorun(
        tmp_path / "lake",
        env=env if env is not None else {},
        credentials_error=(credentials_error if credentials_error is not None else lambda: None),
        preflight_runner=(
            preflight_runner if preflight_runner is not None else _pass_preflight_report
        ),
        probes_runner=probes_runner if probes_runner is not None else _pass_preflight_report,
        now_utc=_now,
        thread_starter=thread_starter,
        **kwargs,
    )


# ── autorun_enabled (the escape hatch — default ON) ─────────────────────


@pytest.mark.parametrize("env", [{}, {da.AUTORUN_ENV: "1"}, {da.AUTORUN_ENV: ""}])
def test_autorun_enabled_defaults_on(env: dict[str, str]) -> None:
    """Unset / truthy / blank all leave the zero-command autorun ON."""
    assert da.autorun_enabled(env) is True


@pytest.mark.parametrize("value", ["0", "false", "NO", " Off "])
def test_autorun_enabled_explicit_falsy_disables(value: str) -> None:
    """Only an explicit falsy value disables (case-insensitive, stripped)."""
    assert da.autorun_enabled({da.AUTORUN_ENV: value}) is False


def test_autorun_enabled_garbage_stays_on() -> None:
    """An unrecognised value never silently disables the health proof."""
    assert da.autorun_enabled({da.AUTORUN_ENV: "banana"}) is True


# ── verdict model + payload round-trip ──────────────────────────────────


def test_verdict_round_trips_through_payload() -> None:
    """to_dict → verdict_from_payload is loss-free (the cache contract)."""
    verdict = da.AutorunVerdict(
        kind=da.KIND_FAIL,
        headline=da.HEADLINES[da.KIND_FAIL],
        checked_at_utc=_NOW.isoformat(),
        run_id="lp-1",
        lines=("FAIL creds[R1]: nope",),
        stages=((1, "creds", "FAIL"),),
    )
    rebuilt = da.verdict_from_payload(verdict.to_dict())
    assert rebuilt == verdict
    assert verdict.to_dict()["schema"] == da.VERDICT_SCHEMA


def _valid_payload() -> dict[str, Any]:
    return da.AutorunVerdict(
        kind=da.KIND_PASS,
        headline=da.HEADLINES[da.KIND_PASS],
        checked_at_utc=_NOW.isoformat(),
        run_id=None,
        lines=(),
        stages=((0, "env", "PASS"),),
    ).to_dict()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: "not a dict",
        lambda p: {**p, "schema": 999},
        lambda p: {**p, "kind": "greenish"},
        lambda p: {**p, "kind": 5},
        lambda p: {**p, "headline": 7},
        lambda p: {**p, "checked_at_utc": None},
        lambda p: {**p, "run_id": 12},
        lambda p: {**p, "lines": "oops"},
        lambda p: {**p, "lines": ["ok", 3]},
        lambda p: {**p, "stages": "oops"},
        lambda p: {**p, "stages": ["not-a-dict"]},
        lambda p: {**p, "stages": [{"stage": "0", "name": "env", "status": "PASS"}]},
        lambda p: {**p, "stages": [{"stage": 0, "name": 1, "status": "PASS"}]},
        lambda p: {**p, "stages": [{"stage": 0, "name": "env", "status": None}]},
    ],
)
def test_verdict_from_payload_fails_closed(
    mutate: Callable[[dict[str, Any]], object],
) -> None:
    """ANY shape/schema drift reads as no-cache — never a mis-parsed verdict."""
    assert da.verdict_from_payload(mutate(_valid_payload())) is None


def test_verdict_from_payload_accepts_string_run_id() -> None:
    """A string run_id (the kit's lp-… id) round-trips."""
    payload = {**_valid_payload(), "run_id": "lp-20260702-093000"}
    rebuilt = da.verdict_from_payload(payload)
    assert rebuilt is not None and rebuilt.run_id == "lp-20260702-093000"


@pytest.mark.parametrize(
    ("kind", "usable"),
    [
        (da.KIND_PASS, True),
        (da.KIND_WARN, True),
        (da.KIND_FAIL, False),
        (da.KIND_ERROR, False),
        (da.KIND_NOT_APPLICABLE, False),
    ],
)
def test_cache_usable_policy(kind: str, usable: bool) -> None:
    """pass/warn are settled answers; fail/error/not_applicable re-probe."""
    verdict = da.AutorunVerdict(
        kind=kind,
        headline=da.HEADLINES[kind],
        checked_at_utc=_NOW.isoformat(),
        run_id=None,
        lines=(),
        stages=(),
    )
    assert da.cache_usable(verdict) is usable


# ── summarize_report (pure kind policy + verbatim pass-through) ─────────


def test_summarize_all_pass_is_green_with_no_lines() -> None:
    """All stages PASS incl. the required vendor-proof stage → pass, no lines."""
    verdict = da.summarize_report(
        _pass_preflight_report(), required=da.PREFLIGHT_REQUIRED, now=_NOW
    )
    assert verdict.kind == da.KIND_PASS
    assert verdict.headline == da.HEADLINES[da.KIND_PASS]
    assert verdict.lines == ()
    assert verdict.run_id == "lp-test"
    assert verdict.checked_at_utc == _NOW.isoformat()
    assert verdict.stages == (
        (0, "env", "PASS"),
        (1, "creds", "PASS"),
        (2, "totp", "PASS"),
        (3, "token", "PASS"),
        (4, "master", "PASS"),
    )


def test_summarize_fail_carries_the_failing_lines_verbatim() -> None:
    """A FAIL stage → kind fail; its kit lines pass through byte-identical."""
    line = dl.render_diagnostic("creds.failed", reason="boom")
    note = dl.render_diagnostic("creds.synthetic_note")
    report = _report(
        _outcome(0, "env", dl.STATUS_PASS, "PASS env: ok"),
        _outcome(1, "creds", dl.STATUS_FAIL, line, note),
        _outcome(3, "token", dl.STATUS_SKIP, "SKIP token: stage 1 did not run"),
    )
    verdict = da.summarize_report(report, required=da.PREFLIGHT_REQUIRED, now=_NOW)
    assert verdict.kind == da.KIND_FAIL
    assert verdict.lines == (line, note, "SKIP token: stage 1 did not run")


def test_summarize_required_stage_skip_is_fail_not_green() -> None:
    """No FAIL anywhere but the required proof stage SKIPped → still fail."""
    report = _report(
        _outcome(0, "env", dl.STATUS_PASS, "PASS env: ok"),
        _outcome(3, "token", dl.STATUS_SKIP, "SKIP token: no credentials"),
    )
    verdict = da.summarize_report(report, required=da.PREFLIGHT_REQUIRED, now=_NOW)
    assert verdict.kind == da.KIND_FAIL
    assert verdict.lines == ("SKIP token: no credentials",)


def test_summarize_required_stage_missing_is_fail() -> None:
    """A report that never reached the required stage cannot read green."""
    report = _report(_outcome(0, "env", dl.STATUS_PASS, "PASS env: ok"))
    verdict = da.summarize_report(report, required=da.PREFLIGHT_REQUIRED, now=_NOW)
    assert verdict.kind == da.KIND_FAIL


def test_summarize_warn_stage_is_warn_kind() -> None:
    """No FAIL + a WARN (required stage PASS) → warn with the warn lines."""
    warn_line = dl.render_diagnostic("env.behind_origin", sha="abc", behind=3)
    report = _report(
        _outcome(0, "env", dl.STATUS_WARN, warn_line),
        _outcome(3, "token", dl.STATUS_PASS, "PASS token: ok"),
    )
    verdict = da.summarize_report(report, required=da.PREFLIGHT_REQUIRED, now=_NOW)
    assert verdict.kind == da.KIND_WARN
    assert verdict.headline == da.HEADLINES[da.KIND_WARN]
    assert verdict.lines == (warn_line,)


def test_summarize_required_warn_counts_as_proven() -> None:
    """A WARN-degradable required stage (bhavcopy unreachable) → warn, not fail."""
    warn_line = dl.render_diagnostic("bhavcopy.unreachable", error="timeout")
    report = _report(
        _outcome(6, "depth", dl.STATUS_PASS, "PASS depth: ok"),
        _outcome(7, "vix", dl.STATUS_PASS, "PASS vix: ok"),
        _outcome(8, "bhavcopy", dl.STATUS_WARN, warn_line),
    )
    verdict = da.summarize_report(report, required=da.PROBES_REQUIRED, now=_NOW)
    assert verdict.kind == da.KIND_WARN
    assert verdict.lines == (warn_line,)


def test_not_applicable_verdict_carries_fix_text_verbatim() -> None:
    """The no-credentials verdict relays the CredentialsError text unchanged."""
    verdict = da.not_applicable_verdict("Missing Groww credentials: seed SSM …", now=_NOW)
    assert verdict.kind == da.KIND_NOT_APPLICABLE
    assert verdict.headline == "live-proof: not applicable (no credentials)"
    assert verdict.lines == ("Missing Groww credentials: seed SSM …",)
    assert verdict.run_id is None
    assert verdict.stages == ()


def test_error_verdict_shape() -> None:
    """An autorun-internal crash renders the honest error verdict."""
    verdict = da.error_verdict("KitInternalError: disk", now=_NOW)
    assert verdict.kind == da.KIND_ERROR
    assert verdict.headline == da.HEADLINES[da.KIND_ERROR]
    assert verdict.lines == ("KitInternalError: disk",)


# ── cache persistence ────────────────────────────────────────────────────


def test_save_then_load_round_trips(tmp_path: Path) -> None:
    """The disk cache is a loss-free JSON round-trip (timestamped)."""
    path = da.preflight_cache_path(tmp_path / "lake")
    verdict = da.not_applicable_verdict("fix it", now=_NOW)
    da.save_verdict(path, verdict)
    assert da.load_verdict(path) == verdict
    assert json.loads(path.read_text(encoding="utf-8"))["checked_at_utc"] == _NOW.isoformat()


def test_load_missing_cache_is_none(tmp_path: Path) -> None:
    """No cache file reads as no verdict (first run)."""
    assert da.load_verdict(tmp_path / "absent.json") is None


def test_load_corrupt_json_is_none(tmp_path: Path) -> None:
    """Invalid JSON reads as no cache — the next run re-computes."""
    path = tmp_path / "corrupt.json"
    path.write_text("{not json", encoding="utf-8")
    assert da.load_verdict(path) is None


def test_load_schema_drift_is_none(tmp_path: Path) -> None:
    """A schema-drifted cache is treated as absent, never mis-parsed."""
    path = tmp_path / "drift.json"
    payload = {**_valid_payload(), "schema": da.VERDICT_SCHEMA + 1}
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert da.load_verdict(path) is None


def test_load_unreadable_path_is_none(tmp_path: Path) -> None:
    """An OSError on read (path is a directory) degrades to no cache."""
    assert da.load_verdict(tmp_path) is None


def test_cache_paths_live_under_the_liveproof_area(tmp_path: Path) -> None:
    """Both caches persist under <lake>/_liveproof/autorun/."""
    lake = tmp_path / "lake"
    assert da.preflight_cache_path(lake) == lake / "_liveproof" / "autorun" / "preflight.json"
    assert da.probes_cache_path(lake) == lake / "_liveproof" / "autorun" / "probes.json"


# ── kit composition ──────────────────────────────────────────────────────


def test_probe_controller_factory_is_bounded() -> None:
    """The probe controller starts at the 1 r/s floor under the 500/min governor."""
    controller = da.probe_controller_factory()
    assert controller.current_rps == da.AUTORUN_PROBE_CEILING_RPS == 1.0
    assert controller.snapshot()["ceiling_per_min"] == GROWW_RATE_LIMIT_PER_MIN


def _fast_controller() -> RateController:
    """A no-sleep controller for offline kit runs (mirrors the kit suite)."""
    clock = {"mono": 0.0}

    def _monotonic() -> float:
        clock["mono"] += 0.05
        return clock["mono"]

    return RateController(clock=_monotonic, sleep=lambda _s: None, ceiling_rps=10_000.0)


def test_run_kit_stages_runs_only_the_selected_stages(tmp_path: Path) -> None:
    """The preflight subset runs stages 0-4 through the REAL runner, keep-going."""
    deps, _box = make_deps(tmp_path)
    report = da.run_kit_stages(
        da.PREFLIGHT_STAGES, lake_root=tmp_path / "lake", deps_factory=lambda: deps
    )
    assert [outcome.stage for outcome in report.outcomes] == [0, 1, 2, 3, 4]
    assert all(outcome.status == dl.STATUS_PASS for outcome in report.outcomes)


def test_run_kit_stages_controller_override_is_applied(tmp_path: Path) -> None:
    """A controller_factory override replaces the deps' own pacing."""
    deps, _box = make_deps(tmp_path)
    built: list[RateController] = []

    def _factory() -> RateController:
        controller = _fast_controller()
        built.append(controller)
        return controller

    report = da.run_kit_stages(
        frozenset({1, 3}),
        lake_root=tmp_path / "lake",
        deps_factory=lambda: deps,
        controller_factory=_factory,
    )
    assert built, "the override factory must build the run's controller"
    assert [outcome.name for outcome in report.outcomes] == ["creds", "token"]


def test_start_daemon_thread_runs_the_worker() -> None:
    """The production thread starter really runs fn on a daemon thread."""
    done = threading.Event()
    da.start_daemon_thread(done.set, "test-liveproof")
    assert done.wait(timeout=5.0)


# ── LiveProofAutorun — preflight ─────────────────────────────────────────


def test_preflight_pass_publishes_and_persists(tmp_path: Path) -> None:
    """A green preflight lands in memory + on disk with the timestamp."""
    holder = _holder(tmp_path)
    assert holder.start_preflight() is True
    status = holder.status()
    assert status["enabled"] is True
    assert status["preflight"]["state"] == da.STATE_DONE
    assert status["preflight"]["verdict"]["kind"] == da.KIND_PASS
    cached = da.load_verdict(da.preflight_cache_path(tmp_path / "lake"))
    assert cached is not None and cached.kind == da.KIND_PASS


def test_preflight_disabled_by_escape_hatch(tmp_path: Path) -> None:
    """BRUTEX_AUTORUN_LIVEPROOF=0 → nothing starts, nothing is written."""
    holder = _holder(tmp_path, env={da.AUTORUN_ENV: "0"})
    assert holder.start_preflight() is False
    assert holder.on_real_pull() is False
    assert holder.status()["enabled"] is False
    assert holder.status()["preflight"]["state"] == da.STATE_IDLE
    assert not da.preflight_cache_path(tmp_path / "lake").exists()


def test_preflight_single_flight_while_running(tmp_path: Path) -> None:
    """A second start during a run is refused; the captured worker finishes it."""
    captured: list[Callable[[], None]] = []
    holder = _holder(tmp_path, thread_starter=lambda fn, _name: captured.append(fn))
    assert holder.start_preflight() is True
    assert holder.status()["preflight"]["state"] == da.STATE_RUNNING
    assert holder.start_preflight() is False
    captured[0]()
    assert holder.status()["preflight"]["state"] == da.STATE_DONE


def test_preflight_no_credentials_makes_zero_kit_calls(tmp_path: Path) -> None:
    """No creds → not_applicable with the fix text; the kit runner is NEVER called.

    The spy proves the no-vendor-calls invariant: the short-circuit happens
    BEFORE any kit stage, so the sandbox/dry-run mode cannot touch the vendor.
    """
    spy_calls: list[str] = []

    def _spy_runner() -> dl.KitReport:
        spy_calls.append("ran")
        return _pass_preflight_report()

    holder = _holder(
        tmp_path,
        credentials_error=lambda: "Missing Groww credentials: seed /tickvault/prod/groww/*",
        preflight_runner=_spy_runner,
        probes_runner=_spy_runner,
    )
    assert holder.start_preflight() is True
    assert spy_calls == []
    verdict = holder.status()["preflight"]["verdict"]
    assert verdict["kind"] == da.KIND_NOT_APPLICABLE
    assert verdict["headline"] == "live-proof: not applicable (no credentials)"
    assert verdict["lines"] == ["Missing Groww credentials: seed /tickvault/prod/groww/*"]
    # The probes trigger takes the same short-circuit (still zero kit calls).
    assert holder.on_real_pull() is True
    assert spy_calls == []
    assert holder.status()["probes"]["verdict"]["kind"] == da.KIND_NOT_APPLICABLE


def test_preflight_creds_seam_crash_is_error_verdict(tmp_path: Path) -> None:
    """An unexpected creds-seam crash becomes an honest error verdict."""

    def _boom() -> str | None:
        raise RuntimeError("aws sdk exploded")

    holder = _holder(tmp_path, credentials_error=_boom)
    holder.start_preflight()
    verdict = holder.status()["preflight"]["verdict"]
    assert verdict["kind"] == da.KIND_ERROR
    assert verdict["lines"] == ["RuntimeError: aws sdk exploded"]


def test_preflight_kit_crash_is_error_verdict(tmp_path: Path) -> None:
    """A kit-internal crash becomes an honest error verdict, never a fake green."""

    def _boom() -> dl.KitReport:
        raise dl.KitInternalError("transcript volume unwritable")

    holder = _holder(tmp_path, preflight_runner=_boom)
    holder.start_preflight()
    verdict = holder.status()["preflight"]["verdict"]
    assert verdict["kind"] == da.KIND_ERROR
    assert verdict["lines"] == ["KitInternalError: transcript volume unwritable"]


def test_persist_failure_keeps_in_memory_verdict(tmp_path: Path) -> None:
    """An unwritable cache volume logs; the in-memory verdict still serves."""
    lake = tmp_path / "lake"
    lake.mkdir()
    # Make the autorun dir path unusable: a FILE where a parent dir must go.
    (lake / "_liveproof").write_text("not a directory", encoding="utf-8")
    holder = _holder(tmp_path)
    assert holder.start_preflight() is True
    assert holder.status()["preflight"]["verdict"]["kind"] == da.KIND_PASS
    assert not da.preflight_cache_path(lake).exists()


def test_default_clock_is_used_when_not_injected(tmp_path: Path) -> None:
    """The default now_utc stamps a parseable UTC timestamp."""
    holder = da.LiveProofAutorun(
        tmp_path / "lake",
        env={},
        credentials_error=lambda: "no creds",
        thread_starter=_sync_starter,
    )
    holder.start_preflight()
    stamp = holder.status()["preflight"]["verdict"]["checked_at_utc"]
    assert datetime.fromisoformat(stamp).tzinfo is not None


# ── LiveProofAutorun — probes trigger + cache discipline ─────────────────


def test_first_real_pull_runs_probes_once_then_caches(tmp_path: Path) -> None:
    """First real pull probes; the second pull reads the cached verdict."""
    runs: list[str] = []

    def _probes() -> dl.KitReport:
        runs.append("probe")
        return _report(
            _outcome(6, "depth", dl.STATUS_PASS, "PASS depth: ok"),
            _outcome(7, "vix", dl.STATUS_PASS, "PASS vix: ok"),
            _outcome(8, "bhavcopy", dl.STATUS_PASS, "PASS bhavcopy: ok"),
        )

    holder = _holder(tmp_path, probes_runner=_probes)
    assert holder.on_real_pull() is True
    assert runs == ["probe"]
    assert holder.status()["probes"]["verdict"]["kind"] == da.KIND_PASS
    assert da.load_verdict(da.probes_cache_path(tmp_path / "lake")) is not None
    # Second pull: settled verdict → NO re-probe.
    assert holder.on_real_pull() is False
    assert runs == ["probe"]


def test_cached_pass_from_disk_skips_probes_on_next_startup(tmp_path: Path) -> None:
    """A persisted pass verdict from a previous session suppresses re-probing."""
    lake = tmp_path / "lake"
    da.save_verdict(
        da.probes_cache_path(lake),
        da.AutorunVerdict(
            kind=da.KIND_PASS,
            headline=da.HEADLINES[da.KIND_PASS],
            checked_at_utc=_NOW.isoformat(),
            run_id="lp-old",
            lines=(),
            stages=(),
        ),
    )
    runs: list[str] = []

    def _probes() -> dl.KitReport:
        runs.append("probe")
        return _pass_preflight_report()

    holder = _holder(tmp_path, probes_runner=_probes)
    assert holder.on_real_pull() is False
    assert runs == []
    assert holder.status()["probes"]["verdict"]["run_id"] == "lp-old"


def test_failed_cache_reprobes_on_next_pull_bounded(tmp_path: Path) -> None:
    """A failed verdict re-probes on the next pull — once per trigger."""
    lake = tmp_path / "lake"
    da.save_verdict(
        da.probes_cache_path(lake),
        da.error_verdict("boom", now=_NOW),
    )
    runs: list[str] = []

    def _probes() -> dl.KitReport:
        runs.append("probe")
        return _report(_outcome(7, "vix", dl.STATUS_FAIL, "FAIL vix[R5]: absent"))

    holder = _holder(tmp_path, probes_runner=_probes)
    assert holder.on_real_pull() is True
    assert runs == ["probe"]
    # Still failing → the NEXT pull may retry (bounded to one run per trigger)…
    assert holder.on_real_pull() is True
    assert runs == ["probe", "probe"]


def test_corrupt_probes_cache_reads_as_absent_and_reprobes(tmp_path: Path) -> None:
    """A corrupt cache file never blocks (or fakes) the probes."""
    lake = tmp_path / "lake"
    path = da.probes_cache_path(lake)
    path.parent.mkdir(parents=True)
    path.write_text("garbage", encoding="utf-8")
    holder = _holder(tmp_path)
    assert holder.status()["probes"]["verdict"] is None
    assert holder.on_real_pull() is True


def test_probes_single_flight_while_running(tmp_path: Path) -> None:
    """A pull during a probe run never doubles the probes."""
    captured: list[Callable[[], None]] = []
    holder = _holder(tmp_path, thread_starter=lambda fn, _name: captured.append(fn))
    assert holder.on_real_pull() is True
    assert holder.status()["probes"]["state"] == da.STATE_RUNNING
    assert holder.on_real_pull() is False
    captured[0]()
    assert holder.status()["probes"]["state"] == da.STATE_DONE


# ── note_credentials_refusal (banner ↔ 503 agreement) ────────────────────


def test_refusal_note_mirrors_the_fix_text(tmp_path: Path) -> None:
    """The refusal fix text lands on the banner verdict + the disk cache."""
    holder = _holder(tmp_path)
    holder.start_preflight()  # green first — creds then break mid-session
    holder.note_credentials_refusal("credentials refused — fix /tickvault/prod/groww/api-key")
    verdict = holder.status()["preflight"]["verdict"]
    assert verdict["kind"] == da.KIND_NOT_APPLICABLE
    assert verdict["lines"] == ["credentials refused — fix /tickvault/prod/groww/api-key"]
    cached = da.load_verdict(da.preflight_cache_path(tmp_path / "lake"))
    assert cached is not None and cached.kind == da.KIND_NOT_APPLICABLE


def test_refusal_note_is_noop_when_disabled(tmp_path: Path) -> None:
    """The escape hatch silences the banner mirror too (no state, no writes)."""
    holder = _holder(tmp_path, env={da.AUTORUN_ENV: "off"})
    holder.note_credentials_refusal("fix text")
    assert holder.status()["preflight"]["verdict"] is None
    assert not da.preflight_cache_path(tmp_path / "lake").exists()


def test_refusal_note_does_not_flip_a_running_state(tmp_path: Path) -> None:
    """A refusal during an in-flight preflight stamps the verdict, not the state."""
    captured: list[Callable[[], None]] = []
    holder = _holder(tmp_path, thread_starter=lambda fn, _name: captured.append(fn))
    holder.start_preflight()
    holder.note_credentials_refusal("fix text")
    assert holder.status()["preflight"]["state"] == da.STATE_RUNNING
    assert holder.status()["preflight"]["verdict"]["kind"] == da.KIND_NOT_APPLICABLE
    captured[0]()
    assert holder.status()["preflight"]["state"] == da.STATE_DONE


# ── end-to-end through the REAL kit runner over the kit suite's fakes ────


def _kit_lines(
    deps_builder: Callable[[], dl.KitDeps], stages: frozenset[int], lake_root: Path
) -> tuple[str, ...]:
    """The expected verbatim lines: a DIRECT kit run over identical fakes."""
    report = dl.run_live_doctor(
        deps_builder(),
        dl.KitOptions(lake_root=lake_root, stages=stages, keep_going=True),
        transcript_sink=lambda _line: None,
    )
    return tuple(
        line
        for outcome in report.outcomes
        if outcome.status != dl.STATUS_PASS
        for line in outcome.lines
    )


def _e2e_holder(tmp_path: Path, deps_builder: Callable[[], dl.KitDeps]) -> da.LiveProofAutorun:
    """A holder whose DEFAULT runners ride the kit suite's fake deps."""
    return da.LiveProofAutorun(
        tmp_path / "lake",
        env={},
        credentials_error=lambda: None,
        deps_factory=deps_builder,
        probe_controller=_fast_controller,
        now_utc=_now,
        thread_starter=_sync_starter,
    )


def test_e2e_preflight_green_over_benign_fakes(tmp_path: Path) -> None:
    """The default preflight composition passes end-to-end over benign fakes."""
    holder = _e2e_holder(tmp_path, lambda: make_deps(tmp_path)[0])
    assert holder.start_preflight() is True
    verdict = holder.status()["preflight"]["verdict"]
    assert verdict["kind"] == da.KIND_PASS
    assert [row["name"] for row in verdict["stages"]] == [
        "env",
        "creds",
        "totp",
        "token",
        "master",
    ]


def test_e2e_probes_green_over_benign_fakes(tmp_path: Path) -> None:
    """The default probes composition (bounded controller) passes end-to-end."""
    holder = _e2e_holder(tmp_path, lambda: make_deps(tmp_path)[0])
    assert holder.on_real_pull() is True
    verdict = holder.status()["probes"]["verdict"]
    assert verdict["kind"] == da.KIND_PASS
    assert [row["name"] for row in verdict["stages"]] == [
        "creds",
        "token",
        "spot-1m",
        "depth",
        "vix",
        "bhavcopy",
    ]


@pytest.mark.parametrize(
    ("override_key", "override_value", "expected_code"),
    [
        ("growwapi_version", lambda: None, "env.growwapi_missing"),
        ("clock_offset_s", lambda: 94.0, "totp.clock_skew"),
    ],
)
def test_e2e_preflight_failing_stage_lines_are_byte_exact(
    tmp_path: Path,
    override_key: str,
    override_value: Callable[[], object],
    expected_code: str,
) -> None:
    """Each failing cheap stage's banner lines == a direct kit run's lines.

    Byte-equality against :func:`run_live_doctor` over IDENTICAL fakes proves
    the autorun reuses the golden catalog verbatim (no re-wording anywhere).
    """

    def _deps() -> dl.KitDeps:
        return make_deps(tmp_path, **{override_key: override_value})[0]

    holder = _e2e_holder(tmp_path, _deps)
    holder.start_preflight()
    verdict = holder.status()["preflight"]["verdict"]
    assert verdict["kind"] == da.KIND_FAIL
    assert tuple(verdict["lines"]) == _kit_lines(_deps, da.PREFLIGHT_STAGES, tmp_path / "lake")
    assert any(
        line.startswith(dl.DIAGNOSTICS[expected_code].split("{", 1)[0]) for line in verdict["lines"]
    )


def test_e2e_preflight_creds_failure_relays_the_kit_diagnostics(tmp_path: Path) -> None:
    """Stage-1 CredentialsError → the kit's R1 lines land on the banner verbatim."""

    def _raise_creds() -> GrowwFileCredentials:
        raise CredentialsError("Missing Groww credentials: parameter absent")

    def _deps() -> dl.KitDeps:
        return make_deps(tmp_path, resolve_creds=_raise_creds)[0]

    holder = _e2e_holder(tmp_path, _deps)
    holder.start_preflight()
    verdict = holder.status()["preflight"]["verdict"]
    assert verdict["kind"] == da.KIND_FAIL
    assert (
        dl.render_diagnostic("creds.failed", reason="Missing Groww credentials: parameter absent")
        in verdict["lines"]
    )
    assert dl.render_diagnostic("creds.synthetic_note") in verdict["lines"]
    assert tuple(verdict["lines"]) == _kit_lines(_deps, da.PREFLIGHT_STAGES, tmp_path / "lake")


def test_e2e_preflight_bad_totp_secret_line(tmp_path: Path) -> None:
    """Stage-2 non-base32 secret → the exact M3 catalog line."""

    def _deps() -> dl.KitDeps:
        return make_deps(
            tmp_path,
            resolve_creds=lambda: GrowwFileCredentials(api_key="key", totp_secret="ABC 8DEF"),
        )[0]

    holder = _e2e_holder(tmp_path, _deps)
    holder.start_preflight()
    verdict = holder.status()["preflight"]["verdict"]
    problem = dl.base32_problem("ABC 8DEF")
    assert problem is not None
    assert dl.render_diagnostic("totp.bad_base32", problem=problem) in verdict["lines"]


def test_e2e_preflight_token_absent_line(tmp_path: Path) -> None:
    """Stage-3 SSM token absent → the exact R7 catalog line, byte-equal run."""
    from tests.fakes.fake_aws_client import FakeAwsClient

    def _deps() -> dl.KitDeps:
        return make_deps(tmp_path, aws_client_factory=lambda: FakeAwsClient(parameters={}))[0]

    holder = _e2e_holder(tmp_path, _deps)
    holder.start_preflight()
    verdict = holder.status()["preflight"]["verdict"]
    assert verdict["kind"] == da.KIND_FAIL
    assert tuple(verdict["lines"]) == _kit_lines(_deps, da.PREFLIGHT_STAGES, tmp_path / "lake")
    assert any("FAIL token[R7]: SSM token genuinely ABSENT" in line for line in verdict["lines"])


def test_e2e_preflight_master_unreachable_line(tmp_path: Path) -> None:
    """Stage-4 master fetch failure → the exact R8/G4 catalog line."""

    def _http_boom(_url: str) -> str:
        raise TimeoutError("proxy hang")

    def _deps() -> dl.KitDeps:
        return make_deps(tmp_path, http_get=_http_boom)[0]

    holder = _e2e_holder(tmp_path, _deps)
    holder.start_preflight()
    verdict = holder.status()["preflight"]["verdict"]
    assert (
        dl.render_diagnostic("master.unreachable", error="TimeoutError: proxy hang")
        in verdict["lines"]
    )


def test_e2e_probes_vix_absent_is_fail_with_the_hard_gate_line(tmp_path: Path) -> None:
    """The VIX hard gate (R5) fails the probes with the exact catalog line."""

    def _deps() -> dl.KitDeps:
        deps, box = make_deps(tmp_path)
        benign = box["handler"]

        def _no_vix(kwargs: dict[str, object]) -> Mapping[str, object]:
            if "VIX" in str(kwargs.get("groww_symbol", "")):
                return {"candles": []}
            return benign(kwargs)

        box["handler"] = _no_vix
        return deps

    holder = _e2e_holder(tmp_path, _deps)
    holder.on_real_pull()
    verdict = holder.status()["probes"]["verdict"]
    assert verdict["kind"] == da.KIND_FAIL
    assert any(line.startswith("FAIL vix[R5]:") for line in verdict["lines"])
    assert tuple(verdict["lines"]) == _kit_lines(_deps, da.PROBE_STAGES, tmp_path / "lake")


def test_e2e_probes_bhavcopy_mismatch_is_fail(tmp_path: Path) -> None:
    """A derived-vs-NSE OHLC drift fails the probes with the M12/M15 line."""

    def _http_drifted(url: str) -> str:
        if "ind_close_all" in url:
            return _BHAVCOPY_DRIFTED
        from tests.unit.test_doctor_live import _master_csv

        return _master_csv()

    def _deps() -> dl.KitDeps:
        return make_deps(tmp_path, http_get=_http_drifted)[0]

    holder = _e2e_holder(tmp_path, _deps)
    holder.on_real_pull()
    verdict = holder.status()["probes"]["verdict"]
    assert verdict["kind"] == da.KIND_FAIL
    assert any(line.startswith("FAIL bhavcopy[M12/M15]:") for line in verdict["lines"])


def test_e2e_probes_persist_and_second_pull_skips(tmp_path: Path) -> None:
    """The probe verdict persists; a rebuilt holder (new session) reads it back."""
    holder = _e2e_holder(tmp_path, lambda: make_deps(tmp_path)[0])
    assert holder.on_real_pull() is True
    # A fresh holder (a NEW dashboard session over the same lake) reads the
    # cached verdict and never re-probes.
    rebuilt = _e2e_holder(tmp_path, _unreachable_deps)
    assert rebuilt.on_real_pull() is False
    assert rebuilt.status()["probes"]["verdict"]["kind"] == da.KIND_PASS


def _unreachable_deps() -> dl.KitDeps:
    raise AssertionError("a cached settled verdict must never re-run the kit")


# ── secret hygiene ───────────────────────────────────────────────────────


def test_verdict_and_cache_never_carry_secret_values(tmp_path: Path) -> None:
    """The verdict/cache carry diagnostics, never the TOTP secret value."""
    holder = _e2e_holder(tmp_path, lambda: make_deps(tmp_path)[0])
    holder.start_preflight()
    payload = json.dumps(holder.status())
    assert _SECRET not in payload
    cache_text = da.preflight_cache_path(tmp_path / "lake").read_text(encoding="utf-8")
    assert _SECRET not in cache_text
