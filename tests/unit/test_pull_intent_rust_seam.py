"""The pull_intent ↔ ``brutex_pull_state`` kernel seam (DEC-RUST-PULL-STATE-001, flip phase).

Post-flip pins (the Python oracle arms are DELETED — the kernel is THE
decision plane, fail-closed):

* the kernel verdict/arm is SERVED at every consult site
  (deliberately-wrong fakes — the only honest proof the kernel decided),
  incl. the refused-restore structural belt (a drifted RESTORE_PRIOR vote
  with no snapshot clears the orphan, never writes ``None``);
* every wheel fault (missing / stale / raising) is a typed
  :class:`PullStateKernelError` RAISE at every site — including through the
  ``PullIntentStore.load`` pipeline (no oracle fallback exists);
* a loaded ``restarts`` counter beyond the kernel's u64 domain is REFUSED
  loudly (the pre-flip silent Python arm went with the oracle);
* a SET ``BRUTEX_PULL_STATE_RUST`` knob refuses loudly
  (:class:`PullStateFlagRemovedError`);
* the facade accessors' refuse-poison + site-consistency gates;
* the CLASSIFIER DRIFT PIN: the production ``_classify_payload_for_kernel``
  must reproduce the frozen golden fixture's kernel-input codes for EVERY
  parse case (the taxonomy-parity chokepoint — the JSON decode stays
  Python, every decode failure folds into the ONE not-a-Mapping
  classification; design risk #3);
* real-wheel golden replay + hypothesis differentials against IN-TEST
  reimplementations of the frozen laws (test code may re-state the law;
  production may not).
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace

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
from brutex.pull_state_rust import PullStateFlagRemovedError, PullStateKernelError
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tests.golden._pull_intent_harness import (
    PARSE_CASES,
    _decoded_payload,  # pyright: ignore[reportPrivateUsage] — the harness projection
    classify_parsed_payload,
    materialize_parse_bytes,
)

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
def _clean_seam_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ambient hygiene: the resume hatch unset, the removed knob unset (a
    SET knob refuses loudly by design — pinned in its own class below)."""
    monkeypatch.delenv(_RESUME_ENV, raising=False)
    monkeypatch.delenv(_FLAG, raising=False)


def _fake_kernel(**functions: object) -> SimpleNamespace:
    """A fake wheel exposing exactly the given kernel symbols."""
    return SimpleNamespace(**functions)


def _store_with_intent(tmp_path: Path, intent: PullIntent | None = None) -> PullIntentStore:
    """A store under *tmp_path*, optionally pre-seeded with *intent*."""
    store = PullIntentStore(tmp_path)
    if intent is not None:
        store.write(intent)
    return store


# ── the kernel verdict/arm is SERVED (deliberately-wrong fakes) ──────────────


class TestKernelServes:
    def test_shape_invalid_verdict_served(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(
            sys.modules,
            "brutex_pull_state",
            _fake_kernel(intent_shape_verdict=lambda *args: 7),
        )
        # A deliberately-WRONG invalid verdict on a VALID payload is served —
        # proving the kernel arm is genuinely routed.
        assert PullIntent.from_payload(_VALID_PAYLOAD) is None

    def test_shape_valid_verdict_constructs_in_python(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A VALID verdict certifies the shape; the dataclass CONSTRUCTION
        (the mechanical field extraction) stays Python."""
        monkeypatch.setitem(
            sys.modules,
            "brutex_pull_state",
            _fake_kernel(intent_shape_verdict=lambda *args: 0),
        )
        intent = PullIntent.from_payload(_VALID_PAYLOAD)
        assert intent is not None
        assert intent.symbols == "NSE-NIFTY"
        assert intent.restarts == 1
        assert intent.created_at == "2026-07-18T00:00:00+00:00"
        assert intent.scope == "spot-only"

    def test_resume_enabled_verdict_served(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(
            sys.modules,
            "brutex_pull_state",
            _fake_kernel(intent_resume_enabled=lambda raw: False),
        )
        # An unset env is True on the kernel law — the wrong fake is served.
        assert resolve_pull_intent_resume({}) is False

    def test_resume_arm_peer_active_served(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setitem(
            sys.modules,
            "brutex_pull_state",
            _fake_kernel(
                intent_shape_verdict=lambda *args: 0,
                intent_resume_decision=lambda *args: 1,
            ),
        )
        store = _store_with_intent(tmp_path, _PRIOR)
        launched: list[PullIntent] = []
        # probe True + under cap would be FIRED on the lattice — the wrong
        # PEER_ACTIVE fake is served: no bump, no launch, intent untouched.
        action = run_auto_resume(store, launch=launched.append, probe_singleflight=lambda: True)
        assert action is ResumeAction.PEER_ACTIVE
        assert launched == []
        loaded = store.load()
        assert loaded is not None
        assert loaded.restarts == _PRIOR.restarts

    def test_resume_arm_over_cap_served(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setitem(
            sys.modules,
            "brutex_pull_state",
            _fake_kernel(
                intent_shape_verdict=lambda *args: 0,
                intent_resume_decision=lambda *args: 2,
            ),
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
        monkeypatch.setitem(
            sys.modules,
            "brutex_pull_state",
            _fake_kernel(
                intent_shape_verdict=lambda *args: 0,
                intent_resume_decision=lambda *args: 3,
            ),
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
        action = run_auto_resume(store, launch=launched.append, probe_singleflight=lambda: True)
        # The wrong PROCEED fake bumps + fires despite the cap — proving the
        # kernel arm owns the decision.
        assert action is ResumeAction.FIRED
        assert launched[0].restarts == 100

    def test_restore_clear_orphan_served_over_prior(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setitem(
            sys.modules,
            "brutex_pull_state",
            _fake_kernel(
                intent_shape_verdict=lambda *args: 0,
                intent_refused_restore_decision=lambda *args: 2,
            ),
        )
        store = _store_with_intent(tmp_path, _PRIOR)
        # prior present would be RESTORE_PRIOR on the lattice — the wrong
        # CLEAR_ORPHAN fake is served (file gone, never re-written).
        assert (
            restore_intent_after_refused_start(store, prior=_PRIOR, dry_run=False, started=False)
            is True
        )
        assert store.load() is None

    def test_restore_leave_absent_served(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setitem(
            sys.modules,
            "brutex_pull_state",
            _fake_kernel(
                intent_shape_verdict=lambda *args: 0,
                intent_refused_restore_decision=lambda *args: 1,
            ),
        )
        store = _store_with_intent(tmp_path, _PRIOR)
        assert (
            restore_intent_after_refused_start(store, prior=_PRIOR, dry_run=False, started=False)
            is False
        )
        # served LEAVE_ABSENT touched nothing — the current file survives
        assert store.load() is not None

    def test_restore_prior_vote_with_no_prior_takes_the_structural_belt(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A drifted wheel voting RESTORE_PRIOR with ``prior=None`` (a shape
        the facade's vocabulary gate cannot see) must CLEAR the orphan — the
        lattice's own arm for that input — never write ``None``."""
        monkeypatch.setitem(
            sys.modules,
            "brutex_pull_state",
            _fake_kernel(
                intent_shape_verdict=lambda *args: 0,
                intent_refused_restore_decision=lambda *args: 3,
            ),
        )
        store = _store_with_intent(tmp_path, _PRIOR)
        assert (
            restore_intent_after_refused_start(store, prior=None, dry_run=False, started=False)
            is True
        )
        assert store.load() is None


# ── kernel faults are typed raises (fail-closed — no oracle exists) ──────────


class TestKernelFaultsFailClosed:
    def test_raising_kernel_raises_at_every_site(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        def _boom(*args: object) -> object:
            raise RuntimeError("poisoned wheel")

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
        with pytest.raises(PullStateKernelError, match="kernel_error"):
            PullIntent.from_payload(_VALID_PAYLOAD)
        with pytest.raises(PullStateKernelError, match="kernel_error"):
            resolve_pull_intent_resume({_RESUME_ENV: "no"})
        store = _store_with_intent(tmp_path)
        with pytest.raises(PullStateKernelError, match="kernel_error"):
            restore_intent_after_refused_start(store, prior=None, dry_run=False, started=False)

    def test_wheel_missing_raises_through_the_store_pipeline(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """`PullIntentStore.load` folds every I/O + corruption shape — but a
        pull-state KERNEL fault propagates typed (fail-closed, the deleted
        oracle is not there to decide the shape lattice blind)."""
        store = _store_with_intent(tmp_path, _PRIOR)
        monkeypatch.setitem(sys.modules, "brutex_pull_state", None)  # ImportError shape
        with pytest.raises(PullStateKernelError, match="wheel_missing"):
            store.load()

    def test_stale_wheel_raises_naming_the_symbol(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "brutex_pull_state", _fake_kernel())
        with pytest.raises(PullStateKernelError, match="intent_resume_enabled"):
            resolve_pull_intent_resume({_RESUME_ENV: "x"})

    def test_beyond_u64_restarts_refused_loudly(self, tmp_path: Path) -> None:
        """REAL wheel: a forged intent whose ``restarts`` exceeds u64 loads
        (the shape law admits huge positives) but the resume decision
        REFUSES it loudly — the pre-flip silent over-cap arm is gone."""
        forged = PullIntent(
            symbols=None,
            symbol=None,
            from_date=None,
            to_date=None,
            restarts=2**100,
            created_at="",
            scope=None,
        )
        store = _store_with_intent(tmp_path, forged)
        loaded = store.load()
        assert loaded is not None
        assert loaded.restarts == 2**100
        with pytest.raises(PullStateKernelError, match="u64 domain"):
            run_auto_resume(store, launch=lambda intent: None, probe_singleflight=lambda: True)


# ── the removed-knob refusal law ─────────────────────────────────────────────


class TestFlagRemovedRefusal:
    @pytest.mark.parametrize("value", ["1", "0", "junk"])
    def test_every_site_refuses_when_the_removed_knob_is_set(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        """ANY non-empty ``BRUTEX_PULL_STATE_RUST`` refuses loudly at the
        first kernel resolve (DEC-RUST-PULL-STATE-001: the knob is removed)."""
        monkeypatch.setenv(_FLAG, value)
        with pytest.raises(PullStateFlagRemovedError, match=_FLAG):
            resolve_pull_intent_resume({})
        with pytest.raises(PullStateFlagRemovedError, match=_FLAG):
            PullIntent.from_payload(_VALID_PAYLOAD)


# ── the facade accessors' refuse-poison + consistency gates ──────────────────


class TestAccessorGates:
    def test_shape_verdict_gates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for poison in ("7", 7.0, True, None, 255, 9, -1):
            monkeypatch.setitem(
                sys.modules,
                "brutex_pull_state",
                _fake_kernel(intent_shape_verdict=lambda *args, p=poison: p),
            )
            with pytest.raises(PullStateKernelError, match="kernel_invalid"):
                pull_state_rust.rust_intent_shape_verdict(1, 2, 1, 2, 2, 2, 3, 1, 2)
        # every served code passes the gate
        for code in range(9):
            monkeypatch.setitem(
                sys.modules,
                "brutex_pull_state",
                _fake_kernel(intent_shape_verdict=lambda *args, c=code: c),
            )
            assert pull_state_rust.rust_intent_shape_verdict(1, 2, 1, 2, 2, 2, 3, 1, 2) == code

    def test_resume_decision_gates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for poison in ("1", 1.0, False, None, 4, -1):
            monkeypatch.setitem(
                sys.modules,
                "brutex_pull_state",
                _fake_kernel(intent_resume_decision=lambda *args, p=poison: p),
            )
            with pytest.raises(PullStateKernelError, match="kernel_invalid"):
                pull_state_rust.rust_intent_resume_decision(
                    has_intent=True, probe_acquirable=True, restarts=0, max_restarts=2
                )

    def test_resume_decision_site_consistency_gate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # NO_INTENT with an intent present = a stale wheel — refused.
        monkeypatch.setitem(
            sys.modules,
            "brutex_pull_state",
            _fake_kernel(intent_resume_decision=lambda *args: 0),
        )
        with pytest.raises(PullStateKernelError, match="kernel_invalid"):
            pull_state_rust.rust_intent_resume_decision(
                has_intent=True, probe_acquirable=True, restarts=0, max_restarts=2
            )
        # …and a NON-zero arm without an intent is refused symmetrically.
        monkeypatch.setitem(
            sys.modules,
            "brutex_pull_state",
            _fake_kernel(intent_resume_decision=lambda *args: 3),
        )
        with pytest.raises(PullStateKernelError, match="kernel_invalid"):
            pull_state_rust.rust_intent_resume_decision(
                has_intent=False, probe_acquirable=True, restarts=0, max_restarts=2
            )
        # the consistent answer serves
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

    def test_resume_decision_beyond_u64_refuses_without_a_kernel_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The u64 domain gate refuses BEFORE any wheel resolve (the bomb
        stays untouched) — reason ``kernel_invalid`` naming the domain."""

        class _Bomb:
            def __init__(self) -> None:
                """One recorded-touch list per plant."""
                self.touched: list[str] = []

            def __getattr__(self, name: str) -> object:
                """Record the touch (the gate must never reach here)."""
                object.__getattribute__(self, "touched").append(name)
                raise AttributeError(name)

        bomb = _Bomb()
        monkeypatch.setitem(sys.modules, "brutex_pull_state", bomb)
        with pytest.raises(PullStateKernelError, match="u64 domain"):
            pull_state_rust.rust_intent_resume_decision(
                has_intent=True, probe_acquirable=True, restarts=2**100, max_restarts=2
            )
        with pytest.raises(PullStateKernelError, match="u64 domain"):
            pull_state_rust.rust_intent_resume_decision(
                has_intent=True, probe_acquirable=True, restarts=0, max_restarts=-1
            )
        assert bomb.touched == []

    def test_restore_decision_gates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for poison in ("2", 2.0, True, None, 4):
            monkeypatch.setitem(
                sys.modules,
                "brutex_pull_state",
                _fake_kernel(intent_refused_restore_decision=lambda *args, p=poison: p),
            )
            with pytest.raises(PullStateKernelError, match="kernel_invalid"):
                pull_state_rust.rust_intent_refused_restore_decision(
                    dry_run=False, started=False, current_present=True, prior_present=True
                )

    def test_restore_decision_site_consistency_gate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(
            sys.modules,
            "brutex_pull_state",
            _fake_kernel(intent_refused_restore_decision=lambda *args: 0),
        )
        # NO_ACTION on a live (non-dry, non-started) input = stale wheel.
        with pytest.raises(PullStateKernelError, match="kernel_invalid"):
            pull_state_rust.rust_intent_refused_restore_decision(
                dry_run=False, started=False, current_present=True, prior_present=True
            )
        # …but NO_ACTION on a dry-run input is the consistent answer.
        assert (
            pull_state_rust.rust_intent_refused_restore_decision(
                dry_run=True, started=False, current_present=True, prior_present=True
            )
            == 0
        )
        # a NON-zero arm on a short-circuit input is refused symmetrically
        monkeypatch.setitem(
            sys.modules,
            "brutex_pull_state",
            _fake_kernel(intent_refused_restore_decision=lambda *args: 3),
        )
        with pytest.raises(PullStateKernelError, match="kernel_invalid"):
            pull_state_rust.rust_intent_refused_restore_decision(
                dry_run=False, started=True, current_present=True, prior_present=True
            )

    def test_resume_enabled_gates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for poison in (1, 0, "true", None):
            monkeypatch.setitem(
                sys.modules,
                "brutex_pull_state",
                _fake_kernel(intent_resume_enabled=lambda raw, p=poison: p),
            )
            with pytest.raises(PullStateKernelError, match="kernel_invalid"):
                pull_state_rust.rust_intent_resume_enabled("0")
        monkeypatch.setitem(
            sys.modules,
            "brutex_pull_state",
            _fake_kernel(intent_resume_enabled=lambda raw: True),
        )
        assert pull_state_rust.rust_intent_resume_enabled(None) is True

    def test_wheel_missing_and_stale_raise(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "brutex_pull_state", _fake_kernel())
        with pytest.raises(PullStateKernelError, match="wheel_stale"):
            pull_state_rust.rust_intent_resume_enabled("x")
        monkeypatch.setitem(sys.modules, "brutex_pull_state", None)
        with pytest.raises(PullStateKernelError, match="wheel_missing"):
            pull_state_rust.rust_intent_resume_enabled("x")


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
        """The classifier reads via ``in`` + ``[]`` while the constructor
        reads via ``.get`` — coherent for every json.loads product (dict)."""
        payload = {"symbols": "X", "restarts": 0}
        codes = pi._classify_payload_for_kernel(payload)  # pyright: ignore[reportPrivateUsage]
        assert codes[0] == 1
        assert codes[1] == pull_state_rust.INTENT_KIND_STR
        assert codes[2] == pull_state_rust.INTENT_KIND_ABSENT


# ── real-wheel parity (golden replay + kernel-vs-inline-law differentials) ───


def _env_law(raw: str | None) -> bool:
    """The frozen resume-env token law, reimplemented IN TEST CODE (the
    deleted production oracle: unset → True; strip+lower ∈ the falsy set →
    False; anything else → True)."""
    if raw is None:
        return True
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _shape_law(payload: object) -> PullIntent | None:
    """The frozen intent shape law, reimplemented IN TEST CODE (the deleted
    production oracle, token-for-token — test code may re-state the law)."""
    if not isinstance(payload, Mapping):
        return None
    fields: dict[str, str | None] = {}
    for key in ("symbols", "symbol", "from_date", "to_date", "scope"):
        raw = payload.get(key)
        if raw is not None and not isinstance(raw, str):
            return None
        fields[key] = raw
    restarts = payload.get("restarts")
    if isinstance(restarts, bool) or not isinstance(restarts, int) or restarts < 0:
        return None
    created_at = payload.get("created_at", "")
    if not isinstance(created_at, str):
        return None
    return PullIntent(
        symbols=fields["symbols"],
        symbol=fields["symbol"],
        from_date=fields["from_date"],
        to_date=fields["to_date"],
        restarts=restarts,
        created_at=created_at,
        scope=fields["scope"],
    )


class TestRealWheelParity:
    """The frozen golden corpus + hypothesis differentials against the REAL
    kernel — the public functions are kernel-backed, so the replay IS the
    kernel pin."""

    def test_golden_corpus_parity(self, tmp_path: Path) -> None:
        frozen = json.loads(_JSON_FIXTURE.read_text(encoding="utf-8"))
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

    def test_golden_resume_and_restore_parity(self, tmp_path: Path) -> None:
        """Kernel-domain resume rows replay to the frozen verdicts; the
        beyond-u64 rows (``kernel_row=False`` — the deleted silent Python
        arm's history) now RAISE the typed fail-closed refusal."""
        frozen = json.loads(_JSON_FIXTURE.read_text(encoding="utf-8"))
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
            if not row["kernel_row"]:
                with pytest.raises(PullStateKernelError, match="u64 domain"):
                    run_auto_resume(
                        store,
                        launch=launched.append,
                        probe_singleflight=lambda ok=bool(row["probe"]): ok,
                        alert=lambda intent: None,
                        max_restarts=int(row["max_restarts"]),
                    )
                assert launched == [], row["id"]
                continue
            action = run_auto_resume(
                store,
                launch=launched.append,
                probe_singleflight=lambda ok=bool(row["probe"]): ok,
                alert=lambda intent: None,
                max_restarts=int(row["max_restarts"]),
            )
            assert action.value == row["action"], row["id"]
            assert (launched[0].restarts if launched else None) == row["launch_restarts"], row["id"]
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
                    [
                        *"01falseno",
                        "\x1c",
                        "\x1d",
                        "\x1f",
                        "\x85",
                        "\xa0",
                        " ",
                        "　",
                        "\u200b",
                        " ",
                        "F",
                        "O",
                        "ｎ",
                        "ｏ",
                        "İ",
                        "ﬀ",
                    ]
                ),
                max_size=8,
            ),
        )
    )
    def test_hypothesis_env_kernel_vs_inline_law(self, raw: str | None) -> None:
        """Arbitrary raw env tokens: the Rust strip+lower+falsy law must be
        byte-equal to CPython's — the ``str.isspace`` superset included
        (the inline :func:`_env_law` re-states the deleted oracle)."""
        env = {} if raw is None else {_RESUME_ENV: raw}
        assert resolve_pull_intent_resume(env) == _env_law(raw)

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
    def test_hypothesis_shape_kernel_vs_inline_law(self, payload: object) -> None:
        """The kernel-backed ``from_payload`` ≡ the inline re-statement of
        the deleted oracle law, over hostile payload shapes."""
        assert PullIntent.from_payload(payload) == _shape_law(payload)
