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
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from brutex import doctor_autorun as da
from brutex import doctor_live as dl
from brutex.adapters.groww.constants import (
    GROWW_RATE_LIMIT_PER_MIN,
    GROWW_RATE_LIMIT_RPS_CEILING,
)
from brutex.adapters.rate_common import RateController
from brutex.config.credentials_types import CredentialsError, GrowwFileCredentials

from tests.unit.test_doctor_live import (
    _BHAVCOPY_DRIFTED,
    _SECRET,
    _depth_handler,
    make_deps,
)

_NOW = datetime(2026, 7, 2, 9, 30, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _forbid_real_deps(monkeypatch: pytest.MonkeyPatch) -> None:
    """Belt (hostile-review MAJOR-5): NO test here may compose the real deps.

    ``run_kit_stages`` (and therefore the holder's default runners) resolves
    ``build_real_deps`` LATE — a module-global lookup at call time, not a
    default bound at import — so this stub genuinely intercepts any path
    that would otherwise reach the network/AWS/git composition.
    """

    def _forbidden() -> dl.KitDeps:
        raise AssertionError("unit tests must never compose the real network deps")

    monkeypatch.setattr(da, "build_real_deps", _forbidden)


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


def _pass_probes_report() -> dl.KitReport:
    """Mirrors the post-decree autorun probes run: creds→token→spot-1m→vix."""
    return _report(
        _outcome(1, "creds", dl.STATUS_PASS, "PASS creds: ok"),
        _outcome(3, "token", dl.STATUS_PASS, "PASS token: ok"),
        _outcome(5, "spot-1m", dl.STATUS_PASS, "PASS spot-1m: ok"),
        _outcome(7, "vix", dl.STATUS_PASS, "PASS vix: ok"),
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
        # headline-vs-kind cross-check: a VALID headline for the WRONG kind
        # is tampering, not a verdict (hostile-review NIT).
        lambda p: {**p, "headline": da.HEADLINES[da.KIND_FAIL]},
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


def _verdict_of(kind: str, *, checked_at: str | None = None) -> da.AutorunVerdict:
    """A minimal verdict of *kind* stamped at *checked_at* (default ``_NOW``)."""
    return da.AutorunVerdict(
        kind=kind,
        headline=da.HEADLINES[kind],
        checked_at_utc=checked_at if checked_at is not None else _NOW.isoformat(),
        run_id=None,
        lines=(),
        stages=(),
    )


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
    """pass/fresh-warn are settled answers; fail/error/not_applicable re-probe."""
    assert da.cache_usable(_verdict_of(kind), now=_NOW) is usable


def test_cache_usable_warn_expires_after_recheck_days() -> None:
    """m3: a warn verdict older than the re-check window re-probes; pass never."""
    aged = _NOW + timedelta(days=da.AUTORUN_WARN_RECHECK_DAYS, seconds=1)
    at_limit = _NOW + timedelta(days=da.AUTORUN_WARN_RECHECK_DAYS)
    assert da.cache_usable(_verdict_of(da.KIND_WARN), now=at_limit) is True
    assert da.cache_usable(_verdict_of(da.KIND_WARN), now=aged) is False
    # pass stays settled regardless of age (the answer cannot improve).
    assert da.cache_usable(_verdict_of(da.KIND_PASS), now=aged) is True


@pytest.mark.parametrize("stamp", ["not-a-time", "2026-07-02T09:30:00"])
def test_cache_usable_warn_with_bad_stamp_fails_closed(stamp: str) -> None:
    """An unparseable / naive warn timestamp reads as stale → re-probe (bounded)."""
    assert da.cache_usable(_verdict_of(da.KIND_WARN, checked_at=stamp), now=_NOW) is False


def test_cache_usable_default_clock_path() -> None:
    """``now=None`` uses the real UTC clock (a just-stamped warn is fresh)."""
    fresh = _verdict_of(da.KIND_WARN, checked_at=datetime.now(UTC).isoformat())
    assert da.cache_usable(fresh) is True


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
    """A WARN on the required proof stage still counts as proven → warn, not fail.

    Pure-summarizer contract (the ``in (PASS, WARN)`` acceptance branch): a
    required stage that genuinely ran but degraded to WARN never reads as an
    unproven ``fail``.
    """
    warn_line = "WARN vix: degraded evidence"
    report = _report(
        _outcome(1, "creds", dl.STATUS_PASS, "PASS creds: ok"),
        _outcome(7, "vix", dl.STATUS_WARN, warn_line),
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


def test_load_invalid_utf8_cache_is_none_and_feature_stays_alive(tmp_path: Path) -> None:
    """MAJOR-2 repro: invalid BYTES in the cache fail-closed to no-cache.

    A torn write / disk corruption leaves non-UTF-8 bytes; ``read_text``
    raises ``UnicodeDecodeError`` (a ``ValueError``, NOT an ``OSError``) —
    the old code let it escape and KILLED the holder at construction. Now it
    reads as absent, the feature stays alive, and the next save overwrites
    the corrupt file with good JSON.
    """
    lake = tmp_path / "lake"
    path = da.probes_cache_path(lake)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xff\xfe\x00 torn \x80 write")
    assert da.load_verdict(path) is None
    # The holder constructs fine over the corrupt cache (feature alive)…
    holder = _holder(tmp_path, probes_runner=_pass_probes_report)
    assert holder.status()["probes"]["verdict"] is None
    # …re-probes on the next pull, and the save REPLACES the corrupt bytes.
    assert holder.on_real_pull() is True
    reloaded = da.load_verdict(path)
    assert reloaded is not None and reloaded.kind == da.KIND_PASS


def test_load_pathologically_deep_cache_is_none_and_never_raises(tmp_path: Path) -> None:
    """A ~100k-deep nested JSON cache fails closed to no-cache (``RecursionError``).

    The JSON parser raises ``RecursionError`` on pathologically deep
    nesting — neither an ``OSError`` nor a ``ValueError``, so the old catch
    let it escape and the "Never raises" contract of ``load_verdict`` was
    false. A hostile/corrupt cache must read as absent and the holder must
    still construct — never crash.
    """
    lake = tmp_path / "lake"
    path = da.probes_cache_path(lake)
    path.parent.mkdir(parents=True)
    depth = 100_000
    path.write_text("[" * depth + "]" * depth, encoding="utf-8")
    assert da.load_verdict(path) is None
    # The holder constructs fine over the hostile cache (feature alive)…
    holder = _holder(tmp_path, probes_runner=_pass_probes_report)
    assert holder.status()["probes"]["verdict"] is None
    # …and the next pull re-probes and overwrites it with good JSON.
    assert holder.on_real_pull() is True
    reloaded = da.load_verdict(path)
    assert reloaded is not None and reloaded.kind == da.KIND_PASS


def test_save_verdict_is_atomic_against_a_torn_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MAJOR-2: a crash mid-write can never corrupt the previous verdict.

    ``save_verdict`` stages into a same-directory ``.tmp`` sibling and
    ``os.replace``s it over the cache. Here the file write is interrupted
    half-way (half the payload lands, then the volume dies): the REAL cache
    must still hold the previous verdict byte-complete — with the old
    in-place ``write_text`` the cache itself was left torn.
    """
    path = da.probes_cache_path(tmp_path / "lake")
    first = da.error_verdict("first", now=_NOW)
    da.save_verdict(path, first)

    real_write_text = Path.write_text

    def _torn_write(self: Path, data: str, *args: Any, **kwargs: Any) -> int:
        real_write_text(self, data[: len(data) // 2], *args, **kwargs)
        raise OSError("volume died mid-write")

    with monkeypatch.context() as patched:
        patched.setattr(Path, "write_text", _torn_write)
        with pytest.raises(OSError, match="mid-write"):
            da.save_verdict(path, da.error_verdict("second", now=_NOW))
    assert da.load_verdict(path) == first


def test_save_verdict_leaves_no_tmp_sibling_behind(tmp_path: Path) -> None:
    """A successful save renames its staging file away (nothing lingers)."""
    path = da.preflight_cache_path(tmp_path / "lake")
    da.save_verdict(path, da.error_verdict("x", now=_NOW))
    assert path.exists()
    assert list(path.parent.glob("*.tmp")) == []


def test_cache_paths_live_under_the_liveproof_area(tmp_path: Path) -> None:
    """Both caches persist under <lake>/_liveproof/autorun/."""
    lake = tmp_path / "lake"
    assert da.preflight_cache_path(lake) == lake / "_liveproof" / "autorun" / "preflight.json"
    assert da.probes_cache_path(lake) == lake / "_liveproof" / "autorun" / "probes.json"


# ── kit composition ──────────────────────────────────────────────────────


def test_probe_controller_factory_is_bounded() -> None:
    """The probe controller starts at the 1 r/s floor under its 16/min slice."""
    controller = da.probe_controller_factory()
    assert controller.current_rps == da.AUTORUN_PROBE_CEILING_RPS == 1.0
    assert controller.snapshot()["ceiling_per_min"] == da.AUTORUN_PROBE_CEILING_PER_MIN == 16


def test_probe_budget_composes_under_the_account_cap() -> None:
    """MAJOR-3: pull worst-case + probe slice must fit the 500/min account cap.

    The probe controller is a SEPARATE instance from the pull job's — the
    per-minute governors do NOT share state — so composition holds only by
    budget split: pull ≤ 8 r/s → 480/min worst-case, probes ≤ their own
    16/min slice, and the sum may never exceed the operator-confirmed
    account-level cap. This test pins the arithmetic against the production
    constants so any future ceiling bump re-proves the split.
    """
    pull_worst_case_per_min = GROWW_RATE_LIMIT_RPS_CEILING * 60.0
    assert pull_worst_case_per_min + da.AUTORUN_PROBE_CEILING_PER_MIN <= GROWW_RATE_LIMIT_PER_MIN
    # No knife edge: the two rolling windows are independent, so fencepost
    # counting against a fixed wall-clock minute can observe up to ONE extra
    # issue per stream (481 + 17 with the production constants). The old
    # 20/min slice summed to a boundary-countable 502; the split must hold
    # even with both fenceposts counted.
    boundary_worst_case = (pull_worst_case_per_min + 1) + (da.AUTORUN_PROBE_CEILING_PER_MIN + 1)
    assert boundary_worst_case <= GROWW_RATE_LIMIT_PER_MIN
    # And the probe slice itself is a strict LOWERING (Rule 6 — never a raise).
    assert da.AUTORUN_PROBE_CEILING_PER_MIN < GROWW_RATE_LIMIT_PER_MIN


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


def test_run_kit_stages_default_composes_real_deps_late(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``deps_factory=None`` composes ``build_real_deps`` LATE (call-time lookup).

    MAJOR-5 belt seam: the default is resolved through the module global at
    CALL time (not bound at import), so stubbing
    ``doctor_autorun.build_real_deps`` genuinely intercepts the production
    composition — proven here by routing the default onto the offline fakes.
    """
    deps, _box = make_deps(tmp_path)
    calls: list[str] = []

    def _fake_real() -> dl.KitDeps:
        calls.append("composed")
        return deps

    monkeypatch.setattr(da, "build_real_deps", _fake_real)
    report = da.run_kit_stages(da.PREFLIGHT_STAGES, lake_root=tmp_path / "lake")
    assert calls == ["composed"]
    assert [outcome.stage for outcome in report.outcomes] == [0, 1, 2, 3, 4]


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
        return _pass_probes_report()

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


# ── MAJOR-1: a thread-spawn failure never wedges the state or raises ─────


def _flaky_starter(
    failures: dict[str, bool],
) -> Callable[[Callable[[], None], str], None]:
    """A starter that raises while ``failures['on']`` and runs sync after."""

    def _start(fn: Callable[[], None], _name: str) -> None:
        if failures["on"]:
            raise RuntimeError("can't start new thread")
        fn()

    return _start


def test_preflight_spawn_failure_rolls_back_and_records_error(tmp_path: Path) -> None:
    """MAJOR-1: a preflight spawn failure → IDLE + honest error verdict + retry OK."""
    failures = {"on": True}
    holder = _holder(tmp_path, thread_starter=_flaky_starter(failures))
    assert holder.start_preflight() is False  # no run started — honestly reported
    status = holder.status()
    assert status["preflight"]["state"] == da.STATE_IDLE  # never wedged RUNNING
    assert status["preflight"]["verdict"]["kind"] == da.KIND_ERROR
    assert status["preflight"]["verdict"]["lines"] == ["RuntimeError: can't start new thread"]
    cached = da.load_verdict(da.preflight_cache_path(tmp_path / "lake"))
    assert cached is not None and cached.kind == da.KIND_ERROR
    # The failure is not terminal: the next trigger retries and succeeds.
    failures["on"] = False
    assert holder.start_preflight() is True
    assert holder.status()["preflight"]["verdict"]["kind"] == da.KIND_PASS


def test_probes_spawn_failure_rolls_back_and_a_later_pull_retries(tmp_path: Path) -> None:
    """MAJOR-1: an on_real_pull spawn failure never wedges; the NEXT pull retries."""
    failures = {"on": True}
    holder = _holder(
        tmp_path, probes_runner=_pass_probes_report, thread_starter=_flaky_starter(failures)
    )
    assert holder.on_real_pull() is False
    status = holder.status()
    assert status["probes"]["state"] == da.STATE_IDLE  # NOT wedged at RUNNING
    assert status["probes"]["verdict"]["kind"] == da.KIND_ERROR
    cached = da.load_verdict(da.probes_cache_path(tmp_path / "lake"))
    assert cached is not None and cached.kind == da.KIND_ERROR
    # The error verdict is never cache_usable → the next real pull re-probes.
    failures["on"] = False
    assert holder.on_real_pull() is True
    assert holder.status()["probes"]["state"] == da.STATE_DONE
    assert holder.status()["probes"]["verdict"]["kind"] == da.KIND_PASS


def test_spawn_failure_rolls_back_even_when_verdict_construction_raises(
    tmp_path: Path,
) -> None:
    """The rollback to ``idle`` happens BEFORE the error verdict is built.

    ``_spawn_failed`` touches the injected clock (and the scrub belt) to
    construct the honest ``error`` verdict; if THAT raises, the raise still
    surfaces (the app.py belt guards log it) but the state must already be
    rolled back — a construction crash may never re-wedge the flow at
    ``running``, which would permanently refuse every later trigger.
    """
    failures = {"on": True}
    clock_broken = {"on": True}

    def _fragile_clock() -> datetime:
        if clock_broken["on"]:
            raise RuntimeError("clock exploded")
        return _NOW

    holder = da.LiveProofAutorun(
        tmp_path / "lake",
        env={},
        credentials_error=lambda: None,
        preflight_runner=_pass_preflight_report,
        probes_runner=_pass_probes_report,
        now_utc=_fragile_clock,
        thread_starter=_flaky_starter(failures),
    )
    with pytest.raises(RuntimeError, match="clock exploded"):
        holder.start_preflight()
    with pytest.raises(RuntimeError, match="clock exploded"):
        holder.on_real_pull()
    status = holder.status()
    assert status["preflight"]["state"] == da.STATE_IDLE  # NOT wedged at RUNNING
    assert status["probes"]["state"] == da.STATE_IDLE
    # Fully retryable once the machinery recovers.
    failures["on"] = False
    clock_broken["on"] = False
    assert holder.start_preflight() is True
    assert holder.on_real_pull() is True
    assert holder.status()["preflight"]["verdict"]["kind"] == da.KIND_PASS
    assert holder.status()["probes"]["verdict"]["kind"] == da.KIND_PASS


def test_real_thread_start_failure_is_contained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MAJOR-1 repro (reviewer's shape): ``Thread.start`` itself raising is contained.

    With the PRODUCTION ``start_daemon_thread`` seam and ``Thread.start``
    monkeypatched to raise (the classic can't-start-new-thread condition),
    ``on_real_pull`` must swallow the crash — the old code let it escape
    into the pull gate AND left the state wedged at ``running`` forever.
    """
    failures = {"on": True}
    real_start = threading.Thread.start

    def _flaky_start(self: threading.Thread) -> None:
        if failures["on"]:
            raise RuntimeError("can't start new thread")
        real_start(self)

    monkeypatch.setattr(threading.Thread, "start", _flaky_start)
    done = threading.Event()

    def _probes() -> dl.KitReport:
        done.set()
        return _pass_probes_report()

    holder = _holder(tmp_path, probes_runner=_probes, thread_starter=da.start_daemon_thread)
    assert holder.on_real_pull() is False  # contained — did NOT raise
    assert holder.status()["probes"]["state"] == da.STATE_IDLE
    assert holder.status()["probes"]["verdict"]["kind"] == da.KIND_ERROR
    # Threads work again → the next pull genuinely retries on a daemon thread.
    failures["on"] = False
    assert holder.on_real_pull() is True
    assert done.wait(timeout=10.0)


# ── note_credentials_refusal (banner ↔ 503 agreement — RED, never grey) ──


def test_refusal_note_mirrors_the_fix_text_as_fail_red(tmp_path: Path) -> None:
    """MAJOR-4: a refused REAL pull renders FAIL-red with the refusal text.

    Creds broke AFTER a green preflight — an attempted live pull that could
    not proceed IS a failure of the live pipe, so the banner goes RED (kind
    ``fail``) with the relayed fix text, in memory and on disk.
    """
    holder = _holder(tmp_path)
    holder.start_preflight()  # green first — creds then break mid-session
    holder.note_credentials_refusal("credentials refused — fix /tickvault/prod/groww/api-key")
    verdict = holder.status()["preflight"]["verdict"]
    assert verdict["kind"] == da.KIND_FAIL
    assert verdict["headline"] == da.HEADLINES[da.KIND_FAIL]
    assert verdict["lines"] == ["credentials refused — fix /tickvault/prod/groww/api-key"]
    cached = da.load_verdict(da.preflight_cache_path(tmp_path / "lake"))
    assert cached is not None and cached.kind == da.KIND_FAIL


def test_refusal_note_never_downgrades_a_fail_verdict_to_grey(tmp_path: Path) -> None:
    """MAJOR-4 repro: the red→grey overwrite must stay RED.

    A FAILING preflight verdict is on the banner; a credentials refusal then
    arrives. The old code overwrote the red verdict with the grey
    ``not_applicable`` — hiding a genuine failure behind "not applicable".
    Now the verdict stays kind ``fail`` (with the refusal's own fix text).
    """
    failing = _report(_outcome(3, "token", dl.STATUS_FAIL, "FAIL token[R7]: mint refused"))
    holder = _holder(tmp_path, preflight_runner=lambda: failing)
    holder.start_preflight()
    assert holder.status()["preflight"]["verdict"]["kind"] == da.KIND_FAIL
    holder.note_credentials_refusal("credentials refused — fix the SSM parameter")
    verdict = holder.status()["preflight"]["verdict"]
    assert verdict["kind"] == da.KIND_FAIL  # RED stays RED
    assert verdict["lines"] == ["credentials refused — fix the SSM parameter"]
    cached = da.load_verdict(da.preflight_cache_path(tmp_path / "lake"))
    assert cached is not None and cached.kind == da.KIND_FAIL


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
    assert holder.status()["preflight"]["verdict"]["kind"] == da.KIND_FAIL
    captured[0]()
    assert holder.status()["preflight"]["state"] == da.STATE_DONE


# ── no-creds verdicts: grey ONLY without history (MAJOR-4c) ──────────────


def test_creds_vanishing_after_preflight_history_is_fail_red(tmp_path: Path) -> None:
    """MAJOR-4: creds-error over a cache with real history renders FAIL, not grey.

    A previous session's preflight PASSed on this machine (the cache proves
    it); credentials then vanish. The next startup preflight must show a RED
    failure — grey "not applicable" would paper over the breakage.
    """
    lake = tmp_path / "lake"
    da.save_verdict(da.preflight_cache_path(lake), _verdict_of(da.KIND_PASS))
    spy_calls: list[str] = []

    def _spy_runner() -> dl.KitReport:
        spy_calls.append("ran")
        return _pass_preflight_report()

    holder = _holder(
        tmp_path,
        credentials_error=lambda: "Missing Groww credentials: parameter absent",
        preflight_runner=_spy_runner,
    )
    assert holder.start_preflight() is True
    verdict = holder.status()["preflight"]["verdict"]
    assert verdict["kind"] == da.KIND_FAIL
    assert verdict["lines"] == ["Missing Groww credentials: parameter absent"]
    assert spy_calls == []  # still ZERO kit/vendor calls without creds


def test_creds_vanishing_after_probes_history_is_fail_red(tmp_path: Path) -> None:
    """MAJOR-4: the probes flow applies the same history rule (fail, not grey)."""
    lake = tmp_path / "lake"
    da.save_verdict(da.probes_cache_path(lake), _verdict_of(da.KIND_FAIL))
    holder = _holder(
        tmp_path,
        credentials_error=lambda: "Missing Groww credentials: parameter absent",
    )
    assert holder.on_real_pull() is True
    verdict = holder.status()["probes"]["verdict"]
    assert verdict["kind"] == da.KIND_FAIL
    assert verdict["lines"] == ["Missing Groww credentials: parameter absent"]


def test_no_creds_without_history_stays_not_applicable(tmp_path: Path) -> None:
    """MAJOR-4 guard: the never-attempted sandbox context KEEPS the grey verdict."""
    holder = _holder(tmp_path, credentials_error=lambda: "no creds — sandbox")
    holder.start_preflight()
    assert holder.status()["preflight"]["verdict"]["kind"] == da.KIND_NOT_APPLICABLE
    # A prior grey is still "never attempted" — a re-run stays grey too.
    holder.start_preflight()
    assert holder.status()["preflight"]["verdict"]["kind"] == da.KIND_NOT_APPLICABLE


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
    # Operator decree 2026-07-03: depth + bhavcopy are NOT autorun stages.
    assert [row["name"] for row in verdict["stages"]] == [
        "creds",
        "token",
        "spot-1m",
        "vix",
    ]


def test_e2e_probes_never_probe_depth_even_when_the_floor_is_shallow(tmp_path: Path) -> None:
    """Operator decree 2026-07-03 (DEC-DOCTOR-KIT-HONESTY-001 amendment): the
    autorun no longer runs the depth stage AT ALL. A Groww clean-empty at the
    2020 boundary for BSE-SENSEX — the exact case that used to render the red
    depth[R6] banner — now leaves the startup verdict a clean PASS with ZERO
    vendor calls for that symbol (SENSEX is touched ONLY by stage 6) and no
    depth row/line anywhere. FAILS before the fix (the probes ran stage 6 →
    a WARN naming the floor). The stage IMPLEMENTATION is kept for the
    manual CLI (``brutex doctor --live``), pinned via the kit registry."""
    shallow = _depth_handler(date(2021, 6, 14))
    counts: dict[str, int] = {}

    def _deps() -> dl.KitDeps:
        deps, box = make_deps(tmp_path)

        def _sensex_only_shallow(kwargs: dict[str, object]) -> Mapping[str, object]:
            symbol = str(kwargs.get("groww_symbol"))
            counts[symbol] = counts.get(symbol, 0) + 1
            if symbol == "BSE-SENSEX":
                return shallow(kwargs)
            from tests.unit.test_doctor_live import default_handler

            return default_handler(kwargs)

        box["handler"] = _sensex_only_shallow
        return deps

    holder = _e2e_holder(tmp_path, _deps)
    assert holder.on_real_pull() is True
    verdict = holder.status()["probes"]["verdict"]
    # Clean PASS — no red banner, no advisory WARN, no depth lines at all.
    assert verdict["kind"] == da.KIND_PASS
    assert verdict["lines"] == []
    assert all(row["name"] != "depth" for row in verdict["stages"])
    # ZERO depth-probe vendor calls: only stage 6 addresses BSE-SENSEX.
    assert counts.get("BSE-SENSEX", 0) == 0
    # The stage implementation is NOT deleted — the manual CLI (default: all
    # stages) still carries depth (6) + bhavcopy (8).
    registry = {stage: name for stage, name, _fn in dl.STAGES}
    assert registry[6] == "depth"
    assert registry[8] == "bhavcopy"


def test_run_kit_stages_grants_auto_bisect_only_to_depth_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bisect budget extension is scoped to runs that include stage 6
    (:data:`da.DEPTH_STAGE_ID`). Post-decree BOTH autorun stage sets exclude
    stage 6, so BOTH the probes and the preflight runs keep the plain
    40-call budget with ``auto_bisect`` off; only an EXPLICIT stage-6
    selection (the manual/diagnostic path) carries ``auto_bisect`` and the
    40 + 60 = 100 extension — the honest call-budget split."""
    captured: dict[str, object] = {}

    def _capturing_run(deps: dl.KitDeps, options: dl.KitOptions) -> dl.KitReport:
        captured["auto_bisect"] = options.auto_bisect
        captured["budget"] = dl.compute_call_budget(options)
        return dl.run_live_doctor(deps, options, transcript_sink=lambda _line: None)

    deps, _box = make_deps(tmp_path)
    monkeypatch.setattr(da, "run_live_doctor", _capturing_run)
    assert da.DEPTH_STAGE_ID not in da.PROBE_STAGES  # the decree, pinned
    assert da.DEPTH_STAGE_ID not in da.PREFLIGHT_STAGES
    da.run_kit_stages(da.PROBE_STAGES, lake_root=tmp_path / "lake", deps_factory=lambda: deps)
    assert captured == {"auto_bisect": False, "budget": 40}
    da.run_kit_stages(da.PREFLIGHT_STAGES, lake_root=tmp_path / "lake", deps_factory=lambda: deps)
    assert captured == {"auto_bisect": False, "budget": 40}
    # An explicit stage-6 selection still gets the honest bisect budget.
    da.run_kit_stages(
        da.PROBE_STAGES | frozenset({da.DEPTH_STAGE_ID}),
        lake_root=tmp_path / "lake",
        deps_factory=lambda: deps,
    )
    assert captured == {"auto_bisect": True, "budget": 100}


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


def test_e2e_probes_never_fetch_bhavcopy_even_when_drifted(tmp_path: Path) -> None:
    """Operator decree 2026-07-03 (DEC-DOCTOR-KIT-HONESTY-001 amendment): the
    autorun no longer runs the bhavcopy cross-check AT ALL. A derived-vs-NSE
    OHLC drift — the exact case that used to FAIL the probes with the
    M12/M15 line — now leaves the verdict a clean PASS, with ZERO NSE-archive
    fetches (the ``nse_http_get`` seam is only stage 8's). FAILS before the
    fix (the probes ran stage 8 → kind ``fail``). The manual CLI keeps the
    cross-check (stage 8 stays in the kit registry, pinned above)."""
    nse_fetches: list[str] = []

    def _http_drifted(url: str) -> str:
        nse_fetches.append(url)
        return _BHAVCOPY_DRIFTED

    def _deps() -> dl.KitDeps:
        # The NSE-archive getter is the stage-8 seam (KitDeps.nse_http_get,
        # DEC-DOCTOR-KIT-HONESTY-001) — nothing else may call it.
        return make_deps(tmp_path, nse_http_get=_http_drifted)[0]

    holder = _e2e_holder(tmp_path, _deps)
    holder.on_real_pull()
    verdict = holder.status()["probes"]["verdict"]
    assert verdict["kind"] == da.KIND_PASS
    assert verdict["lines"] == []
    assert all(row["name"] != "bhavcopy" for row in verdict["stages"])
    assert nse_fetches == []  # ZERO NSE-archive fetches at startup/first pull


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


# ── secret hygiene (m4) ──────────────────────────────────────────────────


def test_verdict_and_cache_never_carry_secret_values(tmp_path: Path) -> None:
    """The verdict/cache carry diagnostics, never the TOTP secret value."""
    holder = _e2e_holder(tmp_path, lambda: make_deps(tmp_path)[0])
    holder.start_preflight()
    payload = json.dumps(holder.status())
    assert _SECRET not in payload
    cache_text = da.preflight_cache_path(tmp_path / "lake").read_text(encoding="utf-8")
    assert _SECRET not in cache_text


def _raise_creds_missing() -> GrowwFileCredentials:
    raise CredentialsError("Missing Groww credentials: parameter absent")


@pytest.mark.parametrize(
    ("override_key", "override_value"),
    [
        ("clock_offset_s", lambda: 94.0),  # totp FAIL lines on the verdict
        ("resolve_creds", _raise_creds_missing),  # creds FAIL lines on the verdict
    ],
)
def test_failing_run_verdict_and_cache_never_carry_secret_values(
    tmp_path: Path, override_key: str, override_value: Callable[[], object]
) -> None:
    """m4 (strengthened): a FAILING totp/creds run still leaks no secret value.

    The green-run hygiene test above has empty diagnostic lines — the real
    exposure surface is a FAILING run whose totp/creds lines carry stage
    detail. Both failing shapes must keep the secret out of the status
    payload AND the persisted cache.
    """
    holder = _e2e_holder(tmp_path, lambda: make_deps(tmp_path, **{override_key: override_value})[0])
    holder.start_preflight()
    verdict = holder.status()["preflight"]["verdict"]
    assert verdict["kind"] == da.KIND_FAIL
    assert verdict["lines"], "a failing run must carry diagnostic lines"
    payload = json.dumps(holder.status())
    assert _SECRET not in payload
    cache_text = da.preflight_cache_path(tmp_path / "lake").read_text(encoding="utf-8")
    assert _SECRET not in cache_text


def test_scrub_secrets_redacts_known_env_values() -> None:
    """m4: any occurrence of a known credential env VALUE is redacted."""
    env = {"BRUTEX_GROWW_TOTP_SECRET": _SECRET, "BRUTEX_GROWW_API_KEY": "groww-key-123"}
    text = f"mint blew up on {_SECRET} with key groww-key-123"
    scrubbed = da.scrub_secrets(text, env=env)
    assert _SECRET not in scrubbed
    assert "groww-key-123" not in scrubbed
    assert da.REDACTED in scrubbed


def test_scrub_secrets_skips_trivial_env_values() -> None:
    """A too-short env value is never redacted (would false-positive prose)."""
    env = {"BRUTEX_GROWW_API_KEY": "abc"}
    assert da.scrub_secrets("abc stays as prose", env=env) == "abc stays as prose"


def test_scrub_secrets_redacts_assignment_shapes() -> None:
    """m4: the classic repr leak (``api_key='…', totp_secret='…'``) is redacted."""
    text = f"GrowwFileCredentials(api_key='groww-key-123', totp_secret='{_SECRET}')"
    scrubbed = da.scrub_secrets(text, env={})
    assert "groww-key-123" not in scrubbed
    assert _SECRET not in scrubbed
    assert scrubbed.count(da.REDACTED) == 2


def test_scrub_secrets_default_env_reads_the_process_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``env=None`` scrubs against the real process environment."""
    monkeypatch.setenv("BRUTEX_GROWW_TOTP_SECRET", "SECRETVALUE123")
    assert "SECRETVALUE123" not in da.scrub_secrets("x SECRETVALUE123 y")


def test_error_verdict_detail_is_scrubbed_before_persist_and_ui(tmp_path: Path) -> None:
    """m4: an exception message echoing the TOTP secret never reaches cache/UI."""
    env = {"BRUTEX_GROWW_TOTP_SECRET": _SECRET}

    def _boom() -> dl.KitReport:
        raise RuntimeError(f"SDK refused totp_secret='{_SECRET}'")

    holder = _holder(tmp_path, env=env, preflight_runner=_boom)
    holder.start_preflight()
    verdict = holder.status()["preflight"]["verdict"]
    assert verdict["kind"] == da.KIND_ERROR
    payload = json.dumps(holder.status())
    assert _SECRET not in payload
    assert da.REDACTED in payload
    cache_text = da.preflight_cache_path(tmp_path / "lake").read_text(encoding="utf-8")
    assert _SECRET not in cache_text
