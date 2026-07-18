"""The pull_intent ↔ ``brutex_pull_state`` Rust seam (DEC-RUST-PULL-STATE-001, Wave 2E).

Covers all four seam arcs on every consult site (flag off · flag on + served
· flag on + degrade · real-wheel parity), the facade accessors' refuse-poison
+ site-consistency gates, and the CLASSIFIER DRIFT PIN: the production
``_classify_payload_for_kernel`` must reproduce the frozen golden fixture's
kernel-input codes for EVERY parse case (the taxonomy-parity chokepoint —
the JSON decode stays Python, and every decode failure folds into the ONE
not-a-Mapping classification, so serde/hand-rolled taxonomy drift is
structurally impossible; design risk #3).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from brutex import pull_state_rust
from brutex.dashboard import pull_intent as pi
from brutex.dashboard.pull_intent import (
    PullIntent,
    PullIntentStore,
    ResumeAction,
    resolve_pull_intent_resume,
    restore_intent_after_refused_start,
    run_auto_resume,
)
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from structlog.testing import capture_logs

from tests.golden._pull_intent_harness import (
    PARSE_CASES,
    _decoded_payload,  # pyright: ignore[reportPrivateUsage] — the harness projection
    classify_parsed_payload,
    materialize_parse_bytes,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

_REPO_ROOT = Path(__file__).resolve().parents[2]
_JSON_FIXTURE = _REPO_ROOT / "tests" / "golden" / "fixtures" / "pull_intent_verdicts.json"

_FLAG = "BRUTEX_PULL_STATE_RUST"
_RESUME_ENV = "BRUTEX_PULL_INTENT_RESUME"

_VALID_PAYLOAD: dict[str, object] = {
    "version": 1,
    "symbols": "NSE-NIFTY",
    "symbol": None,
    "from_date": "2024-01-01",
    "to_date": None,
    "restarts": 1,
    "created_at": "2026-07-18T00:00:00+00:00",
    "scope": "spot-only",
}

_PRIOR = PullIntent(
    symbols="PRIOR",
    symbol=None,
    from_date=None,
    to_date=None,
    restarts=1,
    created_at="2026-07-17T00:00:00+00:00",
    scope="spot-only",
)


@pytest.fixture(autouse=True)
def _clean_seam_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv(_RESUME_ENV, raising=False)
    pull_state_rust.reset_for_tests()
    yield
    pull_state_rust.reset_for_tests()


class _RecordingBomb:
    """A ``sys.modules`` plant that RECORDS every attribute touch.

    ``__getattr__`` raises :class:`AttributeError` (the special-method-
    appropriate type), so the TEETH are the recorded ``touched`` list: the
    flag-OFF pins assert it stays EMPTY, and :class:`TestBombTeeth` proves a
    flag-ON consult genuinely records (the spy cannot go silently blind).
    """

    def __init__(self) -> None:
        self.touched: list[str] = []

    def __getattr__(self, name: str) -> object:
        object.__getattribute__(self, "touched").append(name)
        raise AttributeError(name)


def _fake_kernel(**functions: object) -> SimpleNamespace:
    return SimpleNamespace(**functions)


def _store_with_intent(tmp_path: Path, intent: PullIntent | None = None) -> PullIntentStore:
    store = PullIntentStore(tmp_path)
    if intent is not None:
        store.write(intent)
    return store


# ── flag OFF: byte-identical + never imports (the bomb spy) ──────────────────


class TestFlagOffNeverTouchesKernel:
    def test_all_four_sites_flag_unset(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv(_FLAG, raising=False)
        bomb = _RecordingBomb()
        monkeypatch.setitem(sys.modules, "brutex_pull_state", bomb)
        # site 1: the env-token law
        assert resolve_pull_intent_resume({}) is True
        assert resolve_pull_intent_resume({_RESUME_ENV: "off"}) is False
        # site 2: from_payload (valid + corrupt shapes)
        assert PullIntent.from_payload(_VALID_PAYLOAD) is not None
        assert PullIntent.from_payload({"restarts": True}) is None
        # site 3: the whole resume decision (fired arm — probe+cap+bump+launch)
        store = _store_with_intent(tmp_path, _PRIOR)
        launched: list[PullIntent] = []
        action = run_auto_resume(
            store,
            launch=launched.append,
            probe_singleflight=lambda: True,
            alert=lambda intent: None,
        )
        assert action is ResumeAction.FIRED
        assert launched[0].restarts == 2
        # site 4: the refused-start restore (restore-prior arm)
        store2 = _store_with_intent(tmp_path / "b", _PRIOR)
        assert (
            restore_intent_after_refused_start(
                store2, prior=_PRIOR, dry_run=False, started=False
            )
            is True
        )
        assert bomb.touched == []

    def test_explicit_falsy_flag_also_short_circuits(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv(_FLAG, "0")
        bomb = _RecordingBomb()
        monkeypatch.setitem(sys.modules, "brutex_pull_state", bomb)
        assert PullIntent.from_payload(None) is None
        assert resolve_pull_intent_resume({_RESUME_ENV: "1"}) is True
        store = _store_with_intent(tmp_path)
        assert (
            run_auto_resume(store, launch=lambda intent: None, probe_singleflight=lambda: True)
            is ResumeAction.NO_INTENT
        )
        assert (
            restore_intent_after_refused_start(store, prior=None, dry_run=True, started=False)
            is False
        )
        assert bomb.touched == []


class TestBombTeeth:
    def test_flag_on_consult_records_the_touch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The spy has TEETH: a flag-ON consult genuinely records (so the
        flag-OFF empty-list assertions above can never pass vacuously)."""
        monkeypatch.setenv(_FLAG, "1")
        bomb = _RecordingBomb()
        monkeypatch.setitem(sys.modules, "brutex_pull_state", bomb)
        # The AttributeError-raising bomb reads as a stale wheel → the
        # oracle still serves (never a crash) — AND the touch is recorded.
        assert PullIntent.from_payload(_VALID_PAYLOAD) is not None
        assert "intent_shape_verdict" in bomb.touched
        assert resolve_pull_intent_resume({}) is True
        assert "intent_resume_enabled" in bomb.touched


# ── flag ON: the kernel verdict is SERVED (deliberately-wrong fakes) ─────────


class TestFlagOnServesKernel:
    def test_shape_invalid_verdict_served(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(_FLAG, "1")
        monkeypatch.setitem(
            sys.modules,
            "brutex_pull_state",
            _fake_kernel(intent_shape_verdict=lambda *args: 7),
        )
        # A deliberately-WRONG invalid verdict on a VALID payload is served —
        # proving the kernel arm is genuinely routed.
        assert PullIntent.from_payload(_VALID_PAYLOAD) is None

    def test_shape_valid_verdict_falls_to_the_oracle_construction(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_FLAG, "1")
        monkeypatch.setitem(
            sys.modules,
            "brutex_pull_state",
            _fake_kernel(intent_shape_verdict=lambda *args: 0),
        )
        intent = PullIntent.from_payload(_VALID_PAYLOAD)
        assert intent is not None
        assert intent.scope == "spot-only"
        # fail-safe: a drifted VALID verdict on a CORRUPT payload still ends
        # at the oracle's refusal (the valid arm falls through by design)
        assert PullIntent.from_payload({"restarts": True}) is None

    def test_resume_enabled_verdict_served(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(_FLAG, "1")
        monkeypatch.setitem(
            sys.modules,
            "brutex_pull_state",
            _fake_kernel(intent_resume_enabled=lambda raw: False),
        )
        # Unset env would be True on the oracle — the wrong fake is served.
        assert resolve_pull_intent_resume({}) is False

    def test_resume_arm_peer_active_served(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv(_FLAG, "1")
        monkeypatch.setitem(
            sys.modules,
            "brutex_pull_state",
            _fake_kernel(intent_resume_decision=lambda *args: 1),
        )
        store = _store_with_intent(tmp_path, _PRIOR)
        launched: list[PullIntent] = []
        # probe True + under cap would be FIRED on the oracle — the wrong
        # PEER_ACTIVE fake is served: no bump, no launch, intent untouched.
        action = run_auto_resume(
            store, launch=launched.append, probe_singleflight=lambda: True
        )
        assert action is ResumeAction.PEER_ACTIVE
        assert launched == []
        loaded = store.load()
        assert loaded is not None
        assert loaded.restarts == _PRIOR.restarts

    def test_resume_arm_over_cap_served(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv(_FLAG, "1")
        monkeypatch.setitem(
            sys.modules,
            "brutex_pull_state",
            _fake_kernel(intent_resume_decision=lambda *args: 2),
        )
        store = _store_with_intent(tmp_path, _PRIOR)
        alerts: list[PullIntent] = []
        action = run_auto_resume(
            store,
            launch=lambda intent: None,
            probe_singleflight=lambda: True,
            alert=alerts.append,
        )
        assert action is ResumeAction.OVER_CAP
        assert len(alerts) == 1
        assert store.banner.snapshot() is not None

    def test_resume_arm_proceed_served_over_the_cap(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv(_FLAG, "1")
        monkeypatch.setitem(
            sys.modules,
            "brutex_pull_state",
            _fake_kernel(intent_resume_decision=lambda *args: 3),
        )
        over_cap = PullIntent(
            symbols=None,
            symbol=None,
            from_date=None,
            to_date=None,
            restarts=99,
            created_at="",
            scope=None,
        )
        store = _store_with_intent(tmp_path, over_cap)
        launched: list[PullIntent] = []
        action = run_auto_resume(
            store, launch=launched.append, probe_singleflight=lambda: True
        )
        # The wrong PROCEED fake bumps + fires despite the cap — proving the
        # kernel arm owns the decision when served.
        assert action is ResumeAction.FIRED
        assert launched[0].restarts == 100

    def test_restore_clear_orphan_served_over_prior(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv(_FLAG, "1")
        monkeypatch.setitem(
            sys.modules,
            "brutex_pull_state",
            _fake_kernel(intent_refused_restore_decision=lambda *args: 2),
        )
        store = _store_with_intent(tmp_path, _PRIOR)
        # prior present would be RESTORE_PRIOR on the oracle — the wrong
        # CLEAR_ORPHAN fake is served (file gone, never re-written).
        assert (
            restore_intent_after_refused_start(
                store, prior=_PRIOR, dry_run=False, started=False
            )
            is True
        )
        assert store.load() is None

    def test_restore_leave_absent_served(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv(_FLAG, "1")
        monkeypatch.setitem(
            sys.modules,
            "brutex_pull_state",
            _fake_kernel(intent_refused_restore_decision=lambda *args: 1),
        )
        store = _store_with_intent(tmp_path, _PRIOR)
        assert (
            restore_intent_after_refused_start(
                store, prior=_PRIOR, dry_run=False, started=False
            )
            is False
        )
        # served LEAVE_ABSENT touched nothing — the current file survives
        assert store.load() is not None

    def test_restore_prior_vote_with_no_prior_takes_the_structural_belt(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A drifted wheel voting RESTORE_PRIOR with ``prior=None`` must
        CLEAR the orphan (the oracle's own arm for that input) — never raise
        out of the never-raises boundary."""
        monkeypatch.setenv(_FLAG, "1")
        monkeypatch.setitem(
            sys.modules,
            "brutex_pull_state",
            _fake_kernel(intent_refused_restore_decision=lambda *args: 3),
        )
        store = _store_with_intent(tmp_path, _PRIOR)
        assert (
            restore_intent_after_refused_start(
                store, prior=None, dry_run=False, started=False
            )
            is True
        )
        assert store.load() is None


# ── flag ON: degrade shapes fall to the byte-identical oracle ────────────────


class TestFlagOnDegradesToOracle:
    def test_raising_kernel_degrades_every_site(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        def _boom(*args: object) -> object:
            raise RuntimeError("poisoned wheel")

        monkeypatch.setenv(_FLAG, "1")
        monkeypatch.setitem(
            sys.modules,
            "brutex_pull_state",
            _fake_kernel(
                intent_shape_verdict=_boom,
                intent_resume_decision=_boom,
                intent_refused_restore_decision=_boom,
                intent_resume_enabled=_boom,
            ),
        )
        with capture_logs() as logs:
            assert PullIntent.from_payload(_VALID_PAYLOAD) is not None
            assert resolve_pull_intent_resume({_RESUME_ENV: "no"}) is False
            store = _store_with_intent(tmp_path, _PRIOR)
            action = run_auto_resume(
                store, launch=lambda intent: None, probe_singleflight=lambda: False
            )
            assert action is ResumeAction.PEER_ACTIVE
            store2 = _store_with_intent(tmp_path / "b", _PRIOR)
            assert (
                restore_intent_after_refused_start(
                    store2, prior=None, dry_run=False, started=False
                )
                is True
            )
        assert any(
            entry["event"] == pull_state_rust.PULL_STATE_RUST_FALLBACK_EVENT
            and entry.get("reason") == "kernel_error"
            for entry in logs
        )

    def test_degraded_parse_verdicts_match_flag_off_over_the_corpus(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A wheel-less flag-ON box serves the EXACT flag-OFF verdict for
        every frozen corpus case (the store pipeline end-to-end)."""
        frozen = json.loads(_JSON_FIXTURE.read_text(encoding="utf-8"))
        monkeypatch.setenv(_FLAG, "1")
        monkeypatch.setitem(sys.modules, "brutex_pull_state", _RecordingBomb())
        for row in frozen["parse"]:
            case_id = str(row["id"])
            recipe = dict(next(r for i, r in PARSE_CASES if i == case_id))
            lake_root = tmp_path / case_id
            lake_root.mkdir()
            store = PullIntentStore(lake_root)
            store.path.write_bytes(materialize_parse_bytes(recipe))
            intent = store.load()
            assert (None if intent is None else intent.to_payload()) == row["intent"], case_id


# ── the facade accessors' refuse-poison + consistency gates ──────────────────


class TestAccessorGates:
    def test_shape_verdict_gates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(_FLAG, "1")
        for poison in ("7", 7.0, True, None, 255, 9, -1):
            pull_state_rust.reset_for_tests()
            monkeypatch.setitem(
                sys.modules,
                "brutex_pull_state",
                _fake_kernel(intent_shape_verdict=lambda *args, p=poison: p),
            )
            with capture_logs() as logs:
                assert (
                    pull_state_rust.rust_intent_shape_verdict(1, 2, 1, 2, 2, 2, 3, 1, 2)
                    is None
                )
            assert any(entry.get("reason") == "kernel_invalid" for entry in logs), poison
        # every served code passes the gate
        for code in range(9):
            monkeypatch.setitem(
                sys.modules,
                "brutex_pull_state",
                _fake_kernel(intent_shape_verdict=lambda *args, c=code: c),
            )
            assert (
                pull_state_rust.rust_intent_shape_verdict(1, 2, 1, 2, 2, 2, 3, 1, 2) == code
            )

    def test_resume_decision_gates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(_FLAG, "1")
        for poison in ("1", 1.0, False, None, 4, -1):
            pull_state_rust.reset_for_tests()
            monkeypatch.setitem(
                sys.modules,
                "brutex_pull_state",
                _fake_kernel(intent_resume_decision=lambda *args, p=poison: p),
            )
            with capture_logs() as logs:
                assert (
                    pull_state_rust.rust_intent_resume_decision(
                        has_intent=True, probe_acquirable=True, restarts=0, max_restarts=2
                    )
                    is None
                )
            assert any(entry.get("reason") == "kernel_invalid" for entry in logs), poison

    def test_resume_decision_site_consistency_gate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_FLAG, "1")
        # NO_INTENT with an intent present = a stale wheel — degrade.
        monkeypatch.setitem(
            sys.modules,
            "brutex_pull_state",
            _fake_kernel(intent_resume_decision=lambda *args: 0),
        )
        with capture_logs() as logs:
            assert (
                pull_state_rust.rust_intent_resume_decision(
                    has_intent=True, probe_acquirable=True, restarts=0, max_restarts=2
                )
                is None
            )
        assert any(entry.get("reason") == "kernel_invalid" for entry in logs)
        # …and a NON-zero arm without an intent degrades symmetrically.
        pull_state_rust.reset_for_tests()
        monkeypatch.setitem(
            sys.modules,
            "brutex_pull_state",
            _fake_kernel(intent_resume_decision=lambda *args: 3),
        )
        assert (
            pull_state_rust.rust_intent_resume_decision(
                has_intent=False, probe_acquirable=True, restarts=0, max_restarts=2
            )
            is None
        )
        # the consistent answers serve
        monkeypatch.setitem(
            sys.modules,
            "brutex_pull_state",
            _fake_kernel(intent_resume_decision=lambda *args: 0),
        )
        assert (
            pull_state_rust.rust_intent_resume_decision(
                has_intent=False, probe_acquirable=True, restarts=0, max_restarts=2
            )
            == 0
        )

    def test_resume_decision_beyond_u64_stays_python_without_a_kernel_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_FLAG, "1")
        bomb = _RecordingBomb()
        monkeypatch.setitem(sys.modules, "brutex_pull_state", bomb)
        assert (
            pull_state_rust.rust_intent_resume_decision(
                has_intent=True, probe_acquirable=True, restarts=2**100, max_restarts=2
            )
            is None
        )
        assert (
            pull_state_rust.rust_intent_resume_decision(
                has_intent=True, probe_acquirable=True, restarts=0, max_restarts=-1
            )
            is None
        )
        assert bomb.touched == []

    def test_restore_decision_gates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(_FLAG, "1")
        for poison in ("2", 2.0, True, None, 4):
            pull_state_rust.reset_for_tests()
            monkeypatch.setitem(
                sys.modules,
                "brutex_pull_state",
                _fake_kernel(intent_refused_restore_decision=lambda *args, p=poison: p),
            )
            with capture_logs() as logs:
                assert (
                    pull_state_rust.rust_intent_refused_restore_decision(
                        dry_run=False, started=False, current_present=True, prior_present=True
                    )
                    is None
                )
            assert any(entry.get("reason") == "kernel_invalid" for entry in logs), poison

    def test_restore_decision_site_consistency_gate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_FLAG, "1")
        monkeypatch.setitem(
            sys.modules,
            "brutex_pull_state",
            _fake_kernel(intent_refused_restore_decision=lambda *args: 0),
        )
        # NO_ACTION on a live (non-dry, non-started) input = stale wheel.
        assert (
            pull_state_rust.rust_intent_refused_restore_decision(
                dry_run=False, started=False, current_present=True, prior_present=True
            )
            is None
        )
        # …but NO_ACTION on a dry-run input is the consistent answer.
        assert (
            pull_state_rust.rust_intent_refused_restore_decision(
                dry_run=True, started=False, current_present=True, prior_present=True
            )
            == 0
        )
        # a NON-zero arm on a short-circuit input degrades symmetrically
        pull_state_rust.reset_for_tests()
        monkeypatch.setitem(
            sys.modules,
            "brutex_pull_state",
            _fake_kernel(intent_refused_restore_decision=lambda *args: 3),
        )
        assert (
            pull_state_rust.rust_intent_refused_restore_decision(
                dry_run=False, started=True, current_present=True, prior_present=True
            )
            is None
        )

    def test_resume_enabled_gates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(_FLAG, "1")
        for poison in (1, 0, "true", None):
            pull_state_rust.reset_for_tests()
            monkeypatch.setitem(
                sys.modules,
                "brutex_pull_state",
                _fake_kernel(intent_resume_enabled=lambda raw, p=poison: p),
            )
            with capture_logs() as logs:
                assert pull_state_rust.rust_intent_resume_enabled("0") is None
            assert any(entry.get("reason") == "kernel_invalid" for entry in logs), poison
        monkeypatch.setitem(
            sys.modules,
            "brutex_pull_state",
            _fake_kernel(intent_resume_enabled=lambda raw: True),
        )
        assert pull_state_rust.rust_intent_resume_enabled(None) is True

    def test_wheel_missing_and_stale_degrade(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(_FLAG, "1")
        monkeypatch.setitem(sys.modules, "brutex_pull_state", _fake_kernel())
        with capture_logs() as logs:
            assert pull_state_rust.rust_intent_resume_enabled("x") is None
        assert any(entry.get("reason") == "wheel_stale" for entry in logs)


# ── the classifier drift pin (production ≡ harness ≡ frozen fixture) ─────────


class TestClassifierDriftPin:
    def test_production_classifier_matches_the_frozen_fixture_codes(self) -> None:
        frozen = json.loads(_JSON_FIXTURE.read_text(encoding="utf-8"))
        by_id = dict(PARSE_CASES)
        for row in frozen["parse"]:
            recipe = by_id[str(row["id"])]
            payload = _decoded_payload(materialize_parse_bytes(recipe))
            production = pi._classify_payload_for_kernel(payload)  # pyright: ignore[reportPrivateUsage]
            assert list(production) == row["codes"], row["id"]
            assert production == classify_parsed_payload(payload), row["id"]

    def test_classifier_mapping_get_contains_coherence(self) -> None:
        """The classifier reads via ``in`` + ``[]`` while the oracle reads
        via ``.get`` — coherent for every json.loads product (dict)."""
        payload = {"symbols": "X", "restarts": 0}
        codes = pi._classify_payload_for_kernel(payload)  # pyright: ignore[reportPrivateUsage]
        assert codes[0] == 1
        assert codes[1] == pull_state_rust.INTENT_KIND_STR
        assert codes[2] == pull_state_rust.INTENT_KIND_ABSENT


# ── real-wheel parity (skips until the integrator's merged wheel) ────────────


def _require_wave2e_wheel() -> object:
    wheel = pytest.importorskip("brutex_pull_state", reason="brutex_pull_state wheel not installed")
    if not hasattr(wheel, "intent_shape_verdict"):
        pytest.skip("installed brutex_pull_state wheel predates Wave 2E (no intent_shape_verdict)")
    return wheel


class TestRealWheelParity:
    """Flag-ON vs the frozen golden corpus + hypothesis differentials —
    importorskip'd + capability-probed, so these SKIP until the integrator
    ships the merged Wave-2E wheel."""

    def test_golden_corpus_parity_flag_on(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _require_wave2e_wheel()
        frozen = json.loads(_JSON_FIXTURE.read_text(encoding="utf-8"))
        monkeypatch.setenv(_FLAG, "1")
        by_id = dict(PARSE_CASES)
        for row in frozen["parse"]:
            case_id = str(row["id"])
            lake_root = tmp_path / case_id
            lake_root.mkdir()
            store = PullIntentStore(lake_root)
            store.path.write_bytes(materialize_parse_bytes(by_id[case_id]))
            intent = store.load()
            assert (None if intent is None else intent.to_payload()) == row["intent"], case_id
            assert (not store.path.exists()) == row["file_deleted"], case_id
        for row in frozen["env"]:
            env = {} if row["raw"] is None else {_RESUME_ENV: str(row["raw"])}
            assert resolve_pull_intent_resume(env) == row["enabled"], row["id"]

    def test_golden_resume_and_restore_parity_flag_on(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _require_wave2e_wheel()
        frozen = json.loads(_JSON_FIXTURE.read_text(encoding="utf-8"))
        monkeypatch.setenv(_FLAG, "1")
        for row in frozen["resume"]:
            store = PullIntentStore(tmp_path / str(row["id"]))
            store.lake_root.mkdir()
            if row["has_intent"]:
                store.write(
                    PullIntent(
                        symbols="NSE-NIFTY",
                        symbol=None,
                        from_date=None,
                        to_date=None,
                        restarts=int(row["restarts"]),
                        created_at="2026-07-18T00:00:00+00:00",
                        scope=None,
                    )
                )
            launched: list[PullIntent] = []
            action = run_auto_resume(
                store,
                launch=launched.append,
                probe_singleflight=lambda ok=bool(row["probe"]): ok,
                alert=lambda intent: None,
                max_restarts=int(row["max_restarts"]),
            )
            assert action.value == row["action"], row["id"]
            assert (launched[0].restarts if launched else None) == row["launch_restarts"], row[
                "id"
            ]
        current = PullIntent(
            symbols="CURRENT",
            symbol=None,
            from_date=None,
            to_date=None,
            restarts=3,
            created_at="2026-07-18T01:00:00+00:00",
            scope=None,
        )
        for row in frozen["restore"]:
            store = PullIntentStore(tmp_path / str(row["id"]))
            store.lake_root.mkdir()
            if row["current_present"]:
                store.write(current)
            returned = restore_intent_after_refused_start(
                store,
                prior=_PRIOR if row["prior_present"] else None,
                dry_run=bool(row["dry_run"]),
                started=bool(row["started"]),
            )
            assert returned == row["returned"], row["id"]

    @settings(
        max_examples=300,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(
        raw=st.one_of(
            st.none(),
            st.text(max_size=12),
            st.text(
                alphabet=st.sampled_from(
                    list("01falseno") + [ "\x1c", "\x1d", "\x1f", "\x85", "\xa0",
                     " ", "　", "​", " ", "F", "O", "ｎ", "ｏ", "İ", "ﬀ"]
                ),
                max_size=8,
            ),
        )
    )
    def test_hypothesis_env_differential_flag_on_vs_oracle(
        self, monkeypatch: pytest.MonkeyPatch, raw: str | None
    ) -> None:
        """Arbitrary raw env tokens: the Rust strip+lower+falsy law must be
        byte-equal to CPython's — the ``str.isspace`` superset included."""
        _require_wave2e_wheel()
        env = {} if raw is None else {_RESUME_ENV: raw}
        monkeypatch.setenv(_FLAG, "1")
        served = resolve_pull_intent_resume(env)
        monkeypatch.setenv(_FLAG, "0")
        oracle = resolve_pull_intent_resume(env)
        assert served == oracle

    @settings(
        max_examples=300,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(
        payload=st.one_of(
            st.none(),
            st.integers(),
            st.text(max_size=5),
            st.lists(st.integers(), max_size=3),
            st.fixed_dictionaries(
                {},
                optional={
                    "symbols": st.one_of(st.none(), st.text(max_size=5), st.integers()),
                    "symbol": st.one_of(st.none(), st.text(max_size=5), st.booleans()),
                    "from_date": st.one_of(st.none(), st.text(max_size=5), st.floats()),
                    "to_date": st.one_of(st.none(), st.text(max_size=5)),
                    "scope": st.one_of(st.none(), st.text(max_size=9), st.integers()),
                    "restarts": st.one_of(
                        st.none(),
                        st.booleans(),
                        st.integers(min_value=-(2**70), max_value=2**70),
                        st.floats(allow_nan=True),
                        st.text(max_size=3),
                    ),
                    "created_at": st.one_of(st.none(), st.text(max_size=5), st.integers()),
                    "extra": st.integers(),
                },
            ),
        )
    )
    def test_hypothesis_shape_differential_flag_on_vs_oracle(
        self, monkeypatch: pytest.MonkeyPatch, payload: object
    ) -> None:
        _require_wave2e_wheel()
        monkeypatch.setenv(_FLAG, "1")
        served = PullIntent.from_payload(payload)
        monkeypatch.setenv(_FLAG, "0")
        oracle = PullIntent.from_payload(payload)
        assert served == oracle
