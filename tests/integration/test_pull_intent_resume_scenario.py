# pyright: reportPrivateUsage=none
"""End-to-end pull-intent resume scenarios (DEC-PULL-INTENT-RESUME-001).

The crash-recovery story through REAL ``build_app`` instances + ``TestClient``
with offline fake pull spines (the real report dataclasses — never a network
call), a REAL ``PullIntentStore`` and the REAL account-wide single-flight
``fcntl.flock`` on a tmp lake root:

* a SIGKILL-survivor intent (the durable file a dead process leaves behind —
  the kernel released its ``flock``) re-fires the SAME (symbols, from, to)
  quadruple on a FRESH app, and the resumed run is the FULL pipeline: the
  automatic timeframe PREBUILD stage runs, the movers warm fires after it, the
  ``/api/v1/movers`` screen ends NOT gated, and the intent clears on completion
  — the correction-(b) success criteria, never a "movers gated" placeholder;
* while a PEER app's pull genuinely holds the flock, a sibling boot's resume
  refuses WITHOUT burning a restart token — and once the peer completes (its
  ``on_finally`` clears the shared intent), the next boot finds nothing to
  resume (the work was done).
"""

from __future__ import annotations

import importlib
import json
import threading
import time
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi", reason="dashboard routes need the [ui] extra")
pytest.importorskip("duckdb", reason="the wired-store routes need duckdb")

from brutex.config.credentials import ENV_CREDENTIALS_FILE
from brutex.dashboard.app import DashboardState, build_app
from brutex.dashboard.movers_warm import MoversWarmResult
from brutex.dashboard.pipeline_prebuild import PrebuildOutcome
from brutex.dashboard.pull_intent import PullIntent, PullIntentStore
from brutex.data.instrument_store import InstrumentStore
from fastapi.testclient import TestClient

from tests.unit.test_dashboard_pull_intent_route import (
    _fake_historical,
    _fake_metadata,
    _fake_spot,
    _nifty,
    _patch_spines,
    _pin_creds_present,
    _wait_for,
    _wait_for_job,
)

app_module = importlib.import_module("brutex.dashboard.app")


@pytest.fixture
def store(tmp_path: Path) -> Iterator[InstrumentStore]:
    instrument_store = InstrumentStore.open(tmp_path / "instruments.duckdb")
    instrument_store.upsert_many(
        [_nifty()], snapshot_date=date(2024, 1, 2), run_id="r1", csv_source_sha256="0" * 64
    )
    yield instrument_store
    instrument_store.close()


@pytest.fixture
def intent_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> PullIntentStore:
    lake = tmp_path / "lake"
    lake.mkdir()
    monkeypatch.setenv("BRUTEX_LAKE_ROOT", str(lake))
    monkeypatch.setenv(ENV_CREDENTIALS_FILE, str(tmp_path / "credentials.json"))
    return PullIntentStore(lake)


def _fresh_client(store: InstrumentStore, intent_store: PullIntentStore) -> TestClient:
    app = build_app(DashboardState(), instrument_store=store, pull_intent_store=intent_store)
    return TestClient(app)


def test_sigkill_survivor_intent_resumes_full_pipeline_on_fresh_app(
    store: InstrumentStore, intent_store: PullIntentStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole story: a dead pull's intent → a fresh boot re-fires the FULL
    pipeline (prebuild ran, movers warmed after it, /movers NOT gated, same
    quadruple, intent cleared) — the correction-(b) success criteria."""
    _pin_creds_present(monkeypatch)
    spot_calls: list[dict[str, Any]] = []
    prebuilds: list[Any] = []
    warmed: list[int] = []

    def _spot(*args: Any, **kwargs: Any) -> Any:
        spot_calls.append(dict(kwargs))
        return _fake_spot(*args, **kwargs)

    def _spy_prebuild(**kwargs: Any) -> PrebuildOutcome:
        prebuilds.append(kwargs)
        return PrebuildOutcome(symbols_built=1, total_bars=9, failed=(), failed_count=0, ok=True)

    def _spy_warm(jobs: Any, **_kwargs: Any) -> MoversWarmResult:
        # DEC-MOVERS-WARM-PROGRESS-001 (MIGRATION.md #1290 pack): the real
        # warm_movers_snapshots takes segment/labels/on_progress kwargs and
        # returns the per-coordinate tally, not an int.
        warmed.append(len(list(jobs)))
        return MoversWarmResult(warmed=0, failed=0, total=0)

    monkeypatch.setattr(app_module, "pull_all_spot", _spot)
    monkeypatch.setattr(app_module, "pull_expiries_contracts", _fake_metadata)
    monkeypatch.setattr(app_module, "pull_historical_for_underlyings", _fake_historical)
    monkeypatch.setattr(app_module, "run_pipeline_prebuild_guarded", _spy_prebuild)
    monkeypatch.setattr(app_module, "warm_movers_snapshots", _spy_warm)
    # An empty tmp lake has no spot day — pin the warm-target date so the warm
    # closures reach the (spied) warmer instead of early-returning 0.
    monkeypatch.setattr(app_module, "latest_movers_date", lambda _root: "2024-01-15")

    # The SIGKILL survivor: the durable file a dead pull left behind (its
    # in-process on_finally never ran; the kernel released its flock).
    intent_store.write(
        PullIntent(symbols="NIFTY", symbol=None, from_date="2024-01-01", to_date="2024-02-01")
    )
    with _fresh_client(store, intent_store) as client:
        client.app.state.brutex_resume_pipeline()  # type: ignore[attr-defined]
        assert _wait_for_job(client)["state"] == "done"
        # (b) success criteria — the resumed run is the FULL pipeline:
        assert len(prebuilds) == 1, "the automatic timeframe PREBUILD stage must run"
        assert len(warmed) >= 1, "the movers warm must fire AFTER the prebuild"
        movers = client.get("/api/v1/movers").json()
        assert movers.get("gated") is not True, "the resumed run must never end movers-gated"
        # The SAME persisted quadruple reached the spot phase.
        assert spot_calls[0]["from_date"] == "2024-01-01"
        assert spot_calls[0]["to_date"] == "2024-02-01"
        assert spot_calls[0]["dry_run"] is False
        # The completed resume cleared the durable intent.
        _wait_for(lambda: not intent_store.path.exists())
        assert intent_store.banner.snapshot() is None


def test_peer_running_pull_defers_resume_then_completion_clears_the_intent(
    store: InstrumentStore, intent_store: PullIntentStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """While a PEER app's pull holds the real flock, a sibling boot's resume is
    refused with NO token burnt; the peer's completion clears the shared
    intent, so the next boot honestly finds nothing to resume."""
    _pin_creds_present(monkeypatch)
    gate = threading.Event()

    def _blocking_spot(*args: Any, **kwargs: Any) -> Any:
        opened = gate.wait(timeout=10.0)
        assert opened
        return _fake_spot(*args, **kwargs)

    monkeypatch.setattr(app_module, "pull_all_spot", _blocking_spot)
    monkeypatch.setattr(app_module, "pull_expiries_contracts", _fake_metadata)
    monkeypatch.setattr(app_module, "pull_historical_for_underlyings", _fake_historical)

    with _fresh_client(store, intent_store) as peer:
        peer_resp = peer.post("/api/v1/universe/pull-pipeline", params={"symbols": "NIFTY"})
        assert peer_resp.json()["started"] is True
        # The peer's pull is mid-flight: the intent is durable + the flock held.
        assert intent_store.path.exists()
        with _fresh_client(store, intent_store) as sibling:
            sibling.app.state.brutex_resume_pipeline()  # type: ignore[attr-defined]
            surviving = intent_store.load()
            assert surviving is not None
            assert surviving.restarts == 0, "a live peer must not cost a restart token"
            assert sibling.get("/api/v1/universe/pull-progress").json()["state"] == "idle"
        gate.set()
        assert _wait_for_job(peer)["state"] == "done"
        _wait_for(lambda: not intent_store.path.exists())

    # The next boot: nothing to resume — the work was completed by the peer.
    with _fresh_client(store, intent_store) as next_boot:
        _patch_spines(monkeypatch)
        next_boot.app.state.brutex_resume_pipeline()  # type: ignore[attr-defined]
        time.sleep(0.05)
        assert next_boot.get("/api/v1/universe/pull-progress").json()["state"] == "idle"
        assert intent_store.banner.snapshot() is None


def test_spot_only_intent_resumes_spot_only_never_widened(
    store: InstrumentStore, intent_store: PullIntentStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B M1 (DEC-PULL-SCOPED-ALL-ONECLICK-001): a persisted SPOT-ONLY intent
    resumes SPOT-ONLY — the two F&O spines are NEVER called on the resumed
    run (a crash-resume must never silently widen into a full F&O pull), the
    finish detail carries the spot-only surgery note, and the intent clears."""
    _pin_creds_present(monkeypatch)
    fno_calls: list[str] = []

    def _spy_metadata(*args: Any, **kwargs: Any) -> Any:
        fno_calls.append("metadata")  # pragma: no cover — must never run
        return _fake_metadata(*args, **kwargs)

    def _spy_historical(*args: Any, **kwargs: Any) -> Any:
        fno_calls.append("historical")  # pragma: no cover — must never run
        return _fake_historical(*args, **kwargs)

    monkeypatch.setattr(app_module, "pull_all_spot", _fake_spot)
    monkeypatch.setattr(app_module, "pull_expiries_contracts", _spy_metadata)
    monkeypatch.setattr(app_module, "pull_historical_for_underlyings", _spy_historical)

    from brutex.dashboard.pull_scope import CrossverifyTailOutcome

    def _tail() -> CrossverifyTailOutcome:
        return CrossverifyTailOutcome(
            status="started", human="producer started (poll /api/v1/crossverify/progress)"
        )

    # The durable spot-only survivor (whole universe — symbols None).
    intent_store.write(
        PullIntent(symbols=None, symbol=None, from_date=None, to_date=None, scope="spot-only")
    )
    app = build_app(
        DashboardState(),
        instrument_store=store,
        pull_intent_store=intent_store,
        crossverify_tail=_tail,
    )
    with TestClient(app) as client:
        client.app.state.brutex_resume_pipeline()  # type: ignore[attr-defined]
        snap = _wait_for_job(client)
        assert snap["state"] == "done"
        assert fno_calls == []  # NEVER widened into the F&O phases
        assert snap["detail"].startswith("spot-only pipeline complete — ")
        assert ", F&O skipped by design (spot-only scope)" in snap["detail"]
        assert "Greeks: not run — no options in this pull" in snap["detail"]
        _wait_for(lambda: not intent_store.path.exists())


def test_refused_full_scope_click_never_clobbers_running_spot_only_intent(
    store: InstrumentStore, intent_store: PullIntentStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refuter round 3 F-1 (fails-before-proven at 08d1bb43): while a SPOT-ONLY
    pull genuinely RUNS (blocked mid-spot), a FULL-scope click is REFUSED by
    the single-flight — its pre-start intent persist must NOT clobber the
    running pull's persisted scope, and a SIGKILL inside that window
    (simulated with the exact on-disk artifact the crash leaves) must resume
    SPOT-ONLY through the production boot-resume seam: the F&O spines stay at
    ZERO calls. Pre-fix the clobbered intent read ``scope=null`` and the
    crash-replay entered ``pull_expiries_contracts`` (the executed probe)."""
    _pin_creds_present(monkeypatch)
    gate = threading.Event()
    fno_calls: list[str] = []

    def _blocking_spot(*args: Any, **kwargs: Any) -> Any:
        opened = gate.wait(timeout=10.0)
        assert opened
        return _fake_spot(*args, **kwargs)

    def _spy_metadata(*args: Any, **kwargs: Any) -> Any:
        fno_calls.append("metadata")  # pragma: no cover — must never run
        return _fake_metadata(*args, **kwargs)

    def _spy_historical(*args: Any, **kwargs: Any) -> Any:
        fno_calls.append("historical")  # pragma: no cover — must never run
        return _fake_historical(*args, **kwargs)

    monkeypatch.setattr(app_module, "pull_all_spot", _blocking_spot)
    monkeypatch.setattr(app_module, "pull_expiries_contracts", _spy_metadata)
    monkeypatch.setattr(app_module, "pull_historical_for_underlyings", _spy_historical)

    from brutex.dashboard.pull_scope import CrossverifyTailOutcome

    def _tail() -> CrossverifyTailOutcome:
        # Injected fake tail (the sibling-test idiom): the REAL default tail
        # would start the crossverify producer, whose held lake flock would
        # make the boot resume read peer_active — out of this pin's scope.
        return CrossverifyTailOutcome(status="started", human="producer started (probe tail)")

    def _client() -> TestClient:
        app = build_app(
            DashboardState(),
            instrument_store=store,
            pull_intent_store=intent_store,
            crossverify_tail=_tail,
        )
        return TestClient(app)

    with _client() as client:
        started = client.post(
            "/api/v1/universe/pull-pipeline", params={"scope": "spot-only"}
        ).json()
        assert started["started"] is True
        mid = intent_store.load()
        assert mid is not None and mid.scope == "spot-only"
        # The REFUSED full-scope click while the winner is blocked mid-spot.
        refused = client.post("/api/v1/universe/pull-pipeline")
        assert refused.status_code == 200
        assert refused.json()["started"] is False
        after = intent_store.load()
        assert after is not None, "the running pull's intent must survive the refused click"
        assert after.scope == "spot-only", (
            "a REFUSED full-scope click must never clobber the RUNNING "
            "spot-only pull's persisted intent scope (refuter r3 F-1)"
        )
        # The SIGKILL artifact: the on-disk intent a crash INSIDE the window
        # would leave behind (the winner's on_finally clear never runs).
        crash_raw = intent_store.path.read_text(encoding="utf-8")
        gate.set()
        assert _wait_for_job(client)["state"] == "done"
        _wait_for(lambda: not intent_store.path.exists())

    # Crash replay: restore the captured window artifact, then drive the
    # PRODUCTION boot-resume seam on a fresh app.
    intent_store.path.write_text(crash_raw, encoding="utf-8")
    monkeypatch.setattr(app_module, "pull_all_spot", _fake_spot)
    with _client() as boot:
        boot.app.state.brutex_resume_pipeline()  # type: ignore[attr-defined]
        snap = _wait_for_job(boot)
        assert snap["state"] == "done"
        assert fno_calls == [], (
            "the crash-replay resume must stay SPOT-ONLY — pre-fix the "
            "clobbered intent resumed FULL scope and entered the F&O spines"
        )
        assert snap["detail"].startswith("spot-only pipeline complete — ")
        _wait_for(lambda: not intent_store.path.exists())


# ---------------------------------------------------------------------------
# Refuter round 4 (G-1 + I R4-1, DEC-PULL-SCOPED-ALL-ONECLICK-001 #1779): the
# round-3 snapshot-and-restore was per-request and non-transactional, so two
# CONCURRENT launcher threads could defeat it. Both executed race repros ship
# here as regression tests, orchestration keyed on the pause-injection store
# seams the probes used (event-synchronized pauses at points a thread
# scheduler could pause anyway — NO sleeps) plus a launch-mutex contention
# signal, so each test is deterministic in BOTH worlds: pre-fix (613c52ee,
# no mutex) the second click genuinely progresses and the race assertion
# FAILS; post-fix it blocks on the held launch mutex and the invariant holds.
# ---------------------------------------------------------------------------


class _SignalOnContention:
    """Launch-mutex wrapper: a CONTENDED acquire sets *event* before blocking.

    The deterministic "the launcher serialized us" tell for the race
    scenarios below — exactly one of {second click progressed, second click
    blocked on the mutex} occurs in any world, so the shared event always
    fires and no orchestration step ever sleeps.
    """

    def __init__(self, inner: Any, event: threading.Event) -> None:
        self._inner = inner
        self._event = event

    def __enter__(self) -> object:
        if not self._inner.acquire(blocking=False):
            self._event.set()
            self._inner.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self._inner.release()


def _arm_contention_signal(store: PullIntentStore, event: threading.Event) -> None:
    """Wrap ``store.launch_mutex`` (when present) with the contention signal.

    On a pre-fix tree (the fails-before verification run) the attribute does
    not exist — the wrapper is skipped and the scenarios' OTHER signal arm
    (the second click genuinely progressing) fires instead.
    """
    mutex = getattr(store, "launch_mutex", None)
    if mutex is not None:
        store.launch_mutex = _SignalOnContention(mutex, event)


def _intent_scope_token_on_disk(path: Path) -> str | None:
    """The on-disk intent's scope as the route token, or ``None`` when ABSENT.

    Raw file read (never ``store.load()``) so the armed race-store seams are
    bypassed and the main test thread can never be misclassified as a racer.
    """
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    scope = payload.get("scope")
    return str(scope) if scope is not None else "full"


def _probe_tail() -> Any:
    """Injected fake crossverify tail (the sibling-test idiom)."""
    from brutex.dashboard.pull_scope import CrossverifyTailOutcome

    return CrossverifyTailOutcome(status="started", human="producer started (probe tail)")


class _PauseFirstFullPersistStore(PullIntentStore):
    """Refuter-round-4 G-1 seam: pause the FIRST full-scope intent persist.

    Forces the loser-persist-before-winner-persist sub-ordering of two
    simultaneous clicks: the full-scope click persists its intent then pauses
    (pre-``start()``), letting the spot-only click race past it pre-fix. The
    restore write of a full-scope prior also lands here with ``scope=None`` —
    the once-flag keeps it pause-free.
    """

    def __init__(
        self, lake_root: Path, *, persisted: threading.Event, resume: threading.Event
    ) -> None:
        super().__init__(lake_root)
        self._persisted = persisted
        self._resume = resume
        self._paused_once = False

    def write(self, intent: PullIntent) -> bool:
        ok = super().write(intent)
        if intent.scope is None and not self._paused_once:
            self._paused_once = True
            self._persisted.set()
            assert self._resume.wait(timeout=30.0), "race orchestration wedged (loser pause)"
        return ok


def test_simultaneous_clicks_refused_loser_never_orphans_the_started_pulls_intent(
    store: InstrumentStore, intent_store: PullIntentStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refuter round 4 G-1 (fails-before-proven at 613c52ee): two simultaneous
    clicks on an intent-less lake, the LOSER's snapshot (``prior=None``)
    landing BEFORE the winner's persist. Pre-mutex the refused loser's restore
    took the orphan-CLEAR arm off its stale snapshot and DELETED the RUNNING
    winner's freshly-persisted intent (disk ABSENT for the winner's whole
    remaining runtime — a SIGKILL then left no boot resume). The launch mutex
    serializes the whole snapshot→persist→start→restore transaction, so the
    started click's durable intent always survives the refused sibling."""
    _pin_creds_present(monkeypatch)
    gate = threading.Event()

    def _blocking_spot(*args: Any, **kwargs: Any) -> Any:
        opened = gate.wait(timeout=10.0)
        assert opened
        return _fake_spot(*args, **kwargs)

    monkeypatch.setattr(app_module, "pull_all_spot", _blocking_spot)
    monkeypatch.setattr(app_module, "pull_expiries_contracts", _fake_metadata)
    monkeypatch.setattr(app_module, "pull_historical_for_underlyings", _fake_historical)

    loser_persisted = threading.Event()
    loser_resume = threading.Event()
    second_click_reached = threading.Event()
    race_store = _PauseFirstFullPersistStore(
        intent_store.lake_root, persisted=loser_persisted, resume=loser_resume
    )
    _arm_contention_signal(race_store, second_click_reached)
    app = build_app(
        DashboardState(),
        instrument_store=store,
        pull_intent_store=race_store,
        crossverify_tail=_probe_tail,
    )
    results: dict[str, dict[str, Any]] = {}
    with TestClient(app) as client:

        def _post(tag: str, params: dict[str, str] | None) -> None:
            results[tag] = client.post("/api/v1/universe/pull-pipeline", params=params).json()

        def _spot_click() -> None:
            _post("spot", {"scope": "spot-only"})
            second_click_reached.set()  # the pre-fix arm: the click PROGRESSED

        full_click = threading.Thread(target=_post, args=("full", None), daemon=True)
        full_click.start()
        assert loser_persisted.wait(timeout=10.0), "the full click never persisted"
        spot_click = threading.Thread(target=_spot_click, daemon=True)
        spot_click.start()
        # Deterministic in both worlds: pre-fix the spot click runs to its
        # response (sets the event); post-fix it blocks on the held launch
        # mutex (the contended acquire sets the event).
        assert second_click_reached.wait(timeout=10.0), "the spot click never progressed/blocked"
        loser_resume.set()
        full_click.join(timeout=10.0)
        spot_click.join(timeout=10.0)
        assert not full_click.is_alive() and not spot_click.is_alive(), "a click wedged"
        started = [tag for tag, body in results.items() if body.get("started") is True]
        refused = [tag for tag, body in results.items() if body.get("started") is False]
        assert len(started) == 1 and len(refused) == 1, results
        # THE G-1 KILL: while the started pull genuinely RUNS (blocked
        # mid-spot), its durable intent must be on disk — pre-fix the refused
        # sibling's stale prior=None snapshot orphan-CLEARED it (file ABSENT).
        on_disk = _intent_scope_token_on_disk(race_store.path)
        assert on_disk is not None, (
            "the started pull's durable intent must survive the refused "
            "sibling click (refuter r4 G-1 — the orphan-clear off a stale "
            "prior=None snapshot deleted the RUNNING winner's crash record)"
        )
        assert on_disk == results[started[0]]["scope"], (
            "the surviving on-disk intent must be the STARTED click's own record"
        )
        gate.set()
        assert _wait_for_job(client)["state"] == "done"
        _wait_for(lambda: not race_store.path.exists())


class _TwoLoserInterleaveStore(PullIntentStore):
    """Refuter-round-4 I R4-1 seam: the two-refused-clicks interleave.

    Armed only after the winner runs. Pauses L1 (the first refused click)
    after its PERSIST and L2 (the second) after its SNAPSHOT, forcing the
    executed interleave the per-request restore cannot survive::

        L1 snapshot prior1=W ; L1 persist L1(full)
        L2 snapshot prior2=L1                      # POISONED prior
        L1 refused -> restore prior1 = W           # disk back to W (good)
        L2 refused -> restore prior2 = L1          # disk = FULL while W runs

    Post-mutex L2's snapshot cannot land inside L1's held span — L2 blocks on
    the launch mutex (the contention signal) and both restores write W back.
    Real file I/O intact; roles tracked per thread ident like the probe.
    """

    def __init__(
        self,
        lake_root: Path,
        *,
        l1_persisted: threading.Event,
        l1_resume: threading.Event,
        l2_reached: threading.Event,
        l2_resume: threading.Event,
    ) -> None:
        super().__init__(lake_root)
        self.armed = False
        self._roles_lock = threading.Lock()
        self._l1_tid: int | None = None
        self._l2_tid: int | None = None
        self._l1_writes = 0
        self._l1_persisted = l1_persisted
        self._l1_resume = l1_resume
        self._l2_reached = l2_reached
        self._l2_resume = l2_resume

    def load(self) -> PullIntent | None:
        value = super().load()
        if not self.armed:
            return value
        tid = threading.get_ident()
        first_l2_snapshot = False
        with self._roles_lock:
            if self._l1_tid is None:
                self._l1_tid = tid  # L1's snapshot
            elif tid != self._l1_tid and self._l2_tid is None:
                self._l2_tid = tid  # L2's snapshot — the poisoned prior read
                first_l2_snapshot = True
        if first_l2_snapshot:
            self._l2_reached.set()
            assert self._l2_resume.wait(timeout=30.0), "race orchestration wedged (L2 pause)"
        return value

    def write(self, intent: PullIntent) -> bool:
        ok = super().write(intent)
        if not self.armed:
            return ok
        pause = False
        with self._roles_lock:
            if threading.get_ident() == self._l1_tid:
                self._l1_writes += 1
                pause = self._l1_writes == 1  # L1's PERSIST only, never its restore
        if pause:
            self._l1_persisted.set()
            assert self._l1_resume.wait(timeout=30.0), "race orchestration wedged (L1 pause)"
        return ok


def test_two_refused_clicks_interleave_keeps_winner_scope_and_replays_spot_only(
    store: InstrumentStore, intent_store: PullIntentStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refuter round 4 I R4-1 (fails-before-proven at 613c52ee): TWO refused
    full-scope clicks whose store ops interleave — pre-mutex the later loser
    re-installed the earlier loser's clobber, leaving a FULL-scope intent on
    disk for the spot-only winner's whole remaining runtime (a crash then
    resumed the WRONG scope: the F&O metadata spine entered). Post-mutex the
    final on-disk intent stays the WINNER's spot-only record, and the
    crash-replay of the window artifact resumes SPOT-ONLY with the F&O spines
    at ZERO through the production boot-resume seam."""
    _pin_creds_present(monkeypatch)
    gate = threading.Event()
    fno_calls: list[str] = []

    def _blocking_spot(*args: Any, **kwargs: Any) -> Any:
        opened = gate.wait(timeout=10.0)
        assert opened
        return _fake_spot(*args, **kwargs)

    def _spy_metadata(*args: Any, **kwargs: Any) -> Any:
        fno_calls.append("metadata")  # pragma: no cover — must never run
        return _fake_metadata(*args, **kwargs)

    def _spy_historical(*args: Any, **kwargs: Any) -> Any:
        fno_calls.append("historical")  # pragma: no cover — must never run
        return _fake_historical(*args, **kwargs)

    monkeypatch.setattr(app_module, "pull_all_spot", _blocking_spot)
    monkeypatch.setattr(app_module, "pull_expiries_contracts", _spy_metadata)
    monkeypatch.setattr(app_module, "pull_historical_for_underlyings", _spy_historical)

    l1_persisted = threading.Event()
    l1_resume = threading.Event()
    l2_reached = threading.Event()
    l2_resume = threading.Event()
    race_store = _TwoLoserInterleaveStore(
        intent_store.lake_root,
        l1_persisted=l1_persisted,
        l1_resume=l1_resume,
        l2_reached=l2_reached,
        l2_resume=l2_resume,
    )
    _arm_contention_signal(race_store, l2_reached)

    def _client() -> TestClient:
        app = build_app(
            DashboardState(),
            instrument_store=store,
            pull_intent_store=race_store,
            crossverify_tail=_probe_tail,
        )
        return TestClient(app)

    results: dict[str, dict[str, Any]] = {}
    with _client() as client:
        winner = client.post("/api/v1/universe/pull-pipeline", params={"scope": "spot-only"}).json()
        assert winner["started"] is True
        assert _intent_scope_token_on_disk(race_store.path) == "spot-only"
        race_store.armed = True

        def _post(tag: str) -> None:
            results[tag] = client.post("/api/v1/universe/pull-pipeline").json()

        l1 = threading.Thread(target=_post, args=("L1",), daemon=True)
        l1.start()
        assert l1_persisted.wait(timeout=10.0), "L1 never persisted"
        l2 = threading.Thread(target=_post, args=("L2",), daemon=True)
        l2.start()
        # Deterministic in both worlds: pre-fix L2's snapshot lands inside
        # L1's persist→restore span (the load seam sets the event); post-fix
        # L2 blocks on the held launch mutex (the contended acquire sets it).
        assert l2_reached.wait(timeout=10.0), "L2 never progressed/blocked"
        l1_resume.set()
        l1.join(timeout=10.0)
        assert not l1.is_alive(), "L1 wedged"
        assert results["L1"].get("started") is False
        # L1's restore put the winner's record back — true in both worlds.
        assert _intent_scope_token_on_disk(race_store.path) == "spot-only"
        l2_resume.set()
        l2.join(timeout=10.0)
        assert not l2.is_alive(), "L2 wedged"
        assert results["L2"].get("started") is False
        race_store.armed = False
        # THE R4-1 KILL: after BOTH restores the on-disk intent must still be
        # the running WINNER's spot-only record — pre-fix L2's stale prior
        # (L1's full-scope intent) re-installed the clobber.
        assert _intent_scope_token_on_disk(race_store.path) == "spot-only", (
            "after two interleaved refused clicks the durable intent must "
            "stay the RUNNING winner's scope (refuter r4 R4-1 — the later "
            "loser re-installed the earlier loser's full-scope clobber)"
        )
        # The SIGKILL artifact: what a crash inside the window would replay.
        crash_raw = race_store.path.read_text(encoding="utf-8")
        gate.set()
        assert _wait_for_job(client)["state"] == "done"
        _wait_for(lambda: not race_store.path.exists())

    # Crash replay through the PRODUCTION boot-resume seam on a fresh app.
    race_store.path.write_text(crash_raw, encoding="utf-8")
    monkeypatch.setattr(app_module, "pull_all_spot", _fake_spot)
    with _client() as boot:
        boot.app.state.brutex_resume_pipeline()  # type: ignore[attr-defined]
        snap = _wait_for_job(boot)
        assert snap["state"] == "done"
        assert fno_calls == [], (
            "the crash-replay resume must stay SPOT-ONLY — the interleave "
            "artifact resumed FULL scope pre-fix (F&O metadata spine entered)"
        )
        assert snap["detail"].startswith("spot-only pipeline complete — ")
        _wait_for(lambda: not race_store.path.exists())
