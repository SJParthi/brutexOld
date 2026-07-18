"""The pull-outcome Rust seam (DEC-RUST-PULL-STATE-001, Wave 1A).

Covers all four seam arcs of the ``BRUTEX_PULL_STATE_RUST`` flag on
``brutex.dashboard.pull_outcome`` + the Wave-1A facade accessors appended to
``brutex.pull_state_rust``:

* flag OFF — byte-identical oracle, NEVER imports the wheel (the bomb-module
  spy: it raises ``AttributeError`` — the shape ``getattr``/``hasattr`` would
  SWALLOW — so the teeth are the recorded-touch assertion, not the raise);
* flag ON + a fake kernel — the served-codes tooth (a deliberately-WRONG
  kernel answer is composed and served, proving the kernel is consulted) +
  every composition arm (verdict markers, halt-table headline keys, the
  failed-count compose, every detail code);
* flag ON + degrade — wheel missing/stale, a raising kernel, and every
  refuse-poison gate falls through to the byte-identical oracle;
* flag ON + the REAL wheel — the frozen-golden parity replay + a hypothesis
  differential (``importorskip`` + a symbol capability probe: SKIPPED until
  the integrator's merged wheel carries the Wave-1A symbols; the crate's own
  fixture-driven ``cargo test`` carries kernel parity meanwhile).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from brutex import pull_state_rust
from brutex.dashboard.pull_jobs import RetainedPullState
from brutex.dashboard.pull_outcome import (
    _HALT_NAME_OUTCOMES,
    LastPullOutcome,
    classify_last_pull_outcome,
    classify_retained_pull_outcome,
)
from brutex.dashboard.pull_progress import ProgressSnapshot

FLAG_ENV = "BRUTEX_PULL_STATE_RUST"
FIXTURES = Path(__file__).resolve().parents[1] / "golden" / "fixtures"
MODULE_FIXTURE = FIXTURES / "pull_outcome_classification.json"


class _BombKernelModule:
    """A ``sys.modules`` plant that RECORDS every attribute access, then
    raises ``AttributeError``.

    CodeQL posture: the bomb raises ``AttributeError`` (never
    ``AssertionError``) — exactly the shape ``getattr(mod, name, default)``
    and ``hasattr`` SWALLOW, and the shape the facade's own
    ``except (ImportError, AttributeError)`` degrade arm folds.  The teeth
    therefore live in ``touches``: the flag-OFF pins assert it stays EMPTY,
    so a swallowed raise can never turn the pin vacuous.
    """

    def __init__(self) -> None:
        self.touches: list[str] = []

    def __getattr__(self, name: str) -> object:
        object.__getattribute__(self, "touches").append(name)
        raise AttributeError(name)


def _classify(
    *,
    state: str = "idle",
    detail: str = "",
    failed: int = 0,
    stalled: bool = False,
    wedged: bool = False,
    verdict: dict[str, Any] | None = None,
) -> LastPullOutcome:
    return classify_last_pull_outcome(
        state=state,
        detail=detail,
        failed=failed,
        stalled=stalled,
        wedged=wedged,
        verdict=verdict,
    )


def _verdict(kind: str, *, action: str = "do the thing") -> dict[str, Any]:
    return {
        "kind": kind,
        "headline": f"headline for {kind}",
        "action": action,
        "pending_windows": 3,
        "needs_operator": kind != "clean",
    }


def _fake_wheel(
    monkeypatch: pytest.MonkeyPatch,
    *,
    classify: object = None,
    correlates: object = None,
) -> None:
    """Install a fake ``brutex_pull_state`` wheel + turn the flag ON."""
    monkeypatch.setenv(FLAG_ENV, "1")
    ns = SimpleNamespace()
    if classify is not None:
        ns.classify_pull_outcome = classify
    if correlates is not None:
        ns.verdict_correlates = correlates
    monkeypatch.setitem(sys.modules, "brutex_pull_state", ns)


@pytest.fixture(autouse=True)
def _clean_warn_registry() -> Any:
    pull_state_rust.reset_for_tests()
    yield
    pull_state_rust.reset_for_tests()


class TestFlagOffNeverImports:
    """Flag OFF: the oracle serves and the wheel is NEVER touched."""

    def test_flag_off_classify_is_oracle_and_never_touches_the_wheel(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(FLAG_ENV, raising=False)
        bomb = _BombKernelModule()
        monkeypatch.setitem(sys.modules, "brutex_pull_state", bomb)
        out = _classify(state="error", detail="DailyQuotaExceeded: boom")
        assert (out.outcome, out.tone) == ("halted_quota", "bad")
        out = _classify(state="done", detail="", verdict=_verdict("clean"))
        assert (out.outcome, out.tone) == ("clean", "ok")
        # The teeth: AttributeError would be swallowed by getattr/hasattr —
        # the recorded-touch list is the assertion that cannot be swallowed.
        assert bomb.touches == []

    def test_flag_off_retained_correlation_never_touches_the_wheel(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(FLAG_ENV, "0")
        bomb = _BombKernelModule()
        monkeypatch.setitem(sys.modules, "brutex_pull_state", bomb)
        retained = RetainedPullState(snapshot=_snap(), pull_id="p1")
        out = classify_retained_pull_outcome(
            retained, verdict=_verdict("clean"), verdict_pull_id="p1"
        )
        assert out.outcome == "clean"
        assert bomb.touches == []

    def test_bomb_raises_attribute_error_not_assertion_error(self) -> None:
        """The bomb's own contract (CodeQL: never an ``assert``-shaped raise)."""
        bomb = _BombKernelModule()
        with pytest.raises(AttributeError):
            _ = bomb.classify_pull_outcome
        # …and the swallowing shapes genuinely swallow it, leaving the trace:
        assert getattr(bomb, "verdict_correlates", None) is None
        assert not hasattr(bomb, "pull_allowed_us")
        assert bomb.touches == ["classify_pull_outcome", "verdict_correlates", "pull_allowed_us"]


class TestFlagOnServedCodes:
    """Flag ON: the kernel's codes are composed and SERVED (the seam tooth)."""

    def test_wrong_kernel_answer_is_served_not_the_oracle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The oracle says "running"; the fake kernel says wedged — and the
        # seam serves the kernel's decision, proving it is consulted.
        _fake_wheel(monkeypatch, classify=lambda *a: ("wedged", "bad", "wedged", "input"))
        out = _classify(state="running", detail="beat")
        assert (out.outcome, out.tone) == ("wedged", "bad")
        assert out.headline == "pull WEDGED — restart required"
        assert out.detail == "beat"

    def test_halt_table_headline_code_composes_from_the_python_table(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_wheel(
            monkeypatch,
            classify=lambda *a: (
                "halted_quota",
                "bad",
                "halt:QuotaInvariantError",
                "input",
            ),
        )
        out = _classify(state="error", detail="QuotaInvariantError: boom")
        assert out.headline == _HALT_NAME_OUTCOMES["QuotaInvariantError"][2]
        assert out.detail == "QuotaInvariantError: boom"

    def test_completed_failures_headline_composes_the_count(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_wheel(
            monkeypatch,
            classify=lambda *a: (
                "completed_with_failures",
                "warn",
                "completed_failures",
                "input",
            ),
        )
        out = _classify(state="done", detail="note", failed=7)
        assert out.headline == "pull finished — 7 unit(s) failed/quarantined (will re-pull)"

    def test_verdict_marker_folds_via_the_retained_python_banner_authority(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_wheel(monkeypatch, classify=lambda *a: ("@verdict", "", "", ""))
        out = _classify(state="done", verdict=_verdict("quota_exhausted"))
        assert (out.outcome, out.tone) == ("verdict_quota_exhausted", "warn")

    def test_prepull_marker_carries_the_provenance_detail(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_wheel(monkeypatch, classify=lambda *a: ("@verdict_prepull", "", "", ""))
        out = _classify(state="idle", verdict=_verdict("credentials_missing"))
        assert out.outcome == "verdict_credentials_missing"
        assert out.detail == "do the thing [stamped before any pull started]"

    @pytest.mark.parametrize(
        ("detail_code", "detail", "expected"),
        [
            ("empty", "ignored", ""),
            ("completed", "kept note", "kept note"),
            ("completed", "", "completed — no classified verdict recorded for this run"),
        ],
    )
    def test_detail_codes_compose(
        self,
        monkeypatch: pytest.MonkeyPatch,
        detail_code: str,
        detail: str,
        expected: str,
    ) -> None:
        _fake_wheel(monkeypatch, classify=lambda *a: ("completed", "ok", "completed", detail_code))
        out = _classify(state="done", detail=detail)
        assert out.detail == expected

    def test_kernel_sees_the_packed_inputs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: list[tuple[Any, ...]] = []

        def spy(*args: Any) -> tuple[str, str, str, str]:
            seen.append(args)
            return ("running", "ok", "running", "input")

        _fake_wheel(monkeypatch, classify=spy)
        _classify(state="running", detail="d", failed=2, stalled=True, verdict=_verdict("clean"))
        assert seen == [("running", "d", 2, True, False, True)]


class TestFlagOnDegrades:
    """Every degrade arm falls through to the byte-identical oracle."""

    def test_missing_wheel_degrades_to_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(FLAG_ENV, "1")
        monkeypatch.setitem(sys.modules, "brutex_pull_state", None)  # ImportError
        out = _classify(state="error", detail="LakeWriteHalt: lake write halted: disk_full")
        assert out.outcome == "halted_disk_full"

    def test_stale_wheel_missing_symbol_degrades(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A Phase-1-only wheel: pull_allowed_us present, Wave-1A symbols absent.
        monkeypatch.setenv(FLAG_ENV, "1")
        monkeypatch.setitem(
            sys.modules, "brutex_pull_state", SimpleNamespace(pull_allowed_us=lambda *a: True)
        )
        out = _classify(state="running")
        assert out.outcome == "running"

    def test_raising_kernel_degrades(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(*a: Any) -> tuple[str, str, str, str]:
            raise RuntimeError("kernel exploded")

        _fake_wheel(monkeypatch, classify=boom)
        out = _classify(state="done", detail="note")
        assert (out.outcome, out.headline) == ("completed", "pull finished")

    @pytest.mark.parametrize(
        "poison",
        [
            "not a tuple",
            ("too", "short"),
            ("a", "b", "c", 4),
            ["completed", "ok", "completed", "completed"],
        ],
    )
    def test_poisoned_result_shape_degrades(
        self, monkeypatch: pytest.MonkeyPatch, poison: object
    ) -> None:
        _fake_wheel(monkeypatch, classify=lambda *a: poison)
        out = _classify(state="running")
        assert out.outcome == "running"

    @pytest.mark.parametrize(
        "codes",
        [
            ("running", "ok", "no_such_headline_code", "input"),
            ("halted_quota", "bad", "halt:NoSuchException", "input"),
            ("not_an_outcome", "ok", "running", "input"),
            ("running", "purple", "running", "input"),
            ("running", "ok", "running", "no_such_detail_code"),
        ],
    )
    def test_unrecognised_codes_degrade_to_oracle(
        self, monkeypatch: pytest.MonkeyPatch, codes: tuple[str, str, str, str]
    ) -> None:
        _fake_wheel(monkeypatch, classify=lambda *a: codes)
        out = _classify(state="running", detail="beat")
        assert (out.outcome, out.tone, out.headline) == ("running", "ok", "pull running")

    def test_verdict_marker_without_a_verdict_degrades(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_wheel(monkeypatch, classify=lambda *a: ("@verdict", "", "", ""))
        out = _classify(state="done", detail="note", verdict=None)
        assert out.outcome == "completed"


class TestRetainedCorrelationSeam:
    """The correlation-law seam on ``classify_retained_pull_outcome``."""

    def test_kernel_true_consults_the_verdict_even_on_oracle_mismatch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_wheel(monkeypatch, correlates=lambda *a: True)
        retained = RetainedPullState(snapshot=_snap(), pull_id="p1")
        out = classify_retained_pull_outcome(
            retained, verdict=_verdict("clean"), verdict_pull_id="OTHER"
        )
        assert out.outcome == "clean"  # the kernel's (wrong) law is served

    def test_kernel_false_drops_the_verdict_even_on_oracle_match(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_wheel(monkeypatch, correlates=lambda *a: False)
        retained = RetainedPullState(snapshot=_snap(), pull_id="p1")
        out = classify_retained_pull_outcome(
            retained, verdict=_verdict("clean"), verdict_pull_id="p1"
        )
        assert out.outcome == "completed"

    def test_kernel_degrade_keeps_the_oracle_correlation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_wheel(monkeypatch, correlates=lambda *a: 1)  # non-bool → poison
        retained = RetainedPullState(snapshot=_snap(), pull_id="p1")
        out = classify_retained_pull_outcome(
            retained, verdict=_verdict("clean"), verdict_pull_id="p1"
        )
        assert out.outcome == "clean"

    def test_never_ran_slot_takes_no_correlation_kernel(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(*a: Any) -> bool:
            raise AssertionError("the never-ran arm must not consult the correlation kernel")

        _fake_wheel(monkeypatch, classify=lambda *a: ("never_run", "idle", "never_run", "empty"))
        sys.modules["brutex_pull_state"].verdict_correlates = boom  # type: ignore[attr-defined]
        out = classify_retained_pull_outcome(None, verdict=None, verdict_pull_id="")
        assert out.outcome == "never_run"


class TestFacadeAccessors:
    """The Wave-1A accessors on ``brutex.pull_state_rust`` directly."""

    def test_classify_missing_wheel_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "brutex_pull_state", None)
        assert (
            pull_state_rust.rust_classify_pull_outcome("idle", "", 0, False, False, False) is None
        )

    def test_classify_stale_wheel_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "brutex_pull_state", SimpleNamespace())
        assert (
            pull_state_rust.rust_classify_pull_outcome("idle", "", 0, False, False, False) is None
        )
        assert pull_state_rust.rust_verdict_correlates("a", "b") is None

    def test_classify_happy_path_returns_the_tuple(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(
            sys.modules,
            "brutex_pull_state",
            SimpleNamespace(classify_pull_outcome=lambda *a: ("x", "y", "z", "w")),
        )
        assert pull_state_rust.rust_classify_pull_outcome("idle", "", 0, True, True, True) == (
            "x",
            "y",
            "z",
            "w",
        )

    def test_correlates_happy_and_poison_paths(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(
            sys.modules,
            "brutex_pull_state",
            SimpleNamespace(verdict_correlates=lambda *a: True),
        )
        assert pull_state_rust.rust_verdict_correlates("p", "p") is True
        monkeypatch.setitem(
            sys.modules,
            "brutex_pull_state",
            SimpleNamespace(verdict_correlates=lambda *a: "yes"),
        )
        assert pull_state_rust.rust_verdict_correlates("p", "p") is None

    def test_correlates_raising_kernel_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(*a: Any) -> bool:
            raise ValueError("nope")

        monkeypatch.setitem(
            sys.modules, "brutex_pull_state", SimpleNamespace(verdict_correlates=boom)
        )
        assert pull_state_rust.rust_verdict_correlates("p", "p") is None


def _snap(**overrides: Any) -> ProgressSnapshot:
    kwargs: dict[str, Any] = {
        "state": "done",
        "total": 1,
        "completed": 1,
        "failed": 0,
        "bars": 0,
        "current": "",
        "detail": "",
        "updated_at": 0.0,
        "stalled": False,
        "wedged": False,
    }
    kwargs.update(overrides)
    return ProgressSnapshot(**kwargs)


# ---------------------------------------------------------------------------
# Flag ON + the REAL wheel — skipped until the integrator's merged wheel
# carries the Wave-1A symbols (the crate's fixture-driven `cargo test`
# carries kernel parity meanwhile).
# ---------------------------------------------------------------------------


def _require_wave1a_wheel() -> None:
    wheel = pytest.importorskip("brutex_pull_state")
    if not hasattr(wheel, "classify_pull_outcome") or not hasattr(wheel, "verdict_correlates"):
        pytest.skip("brutex_pull_state wheel predates the Wave-1A pull_outcome symbols")


def test_real_wheel_replays_the_frozen_golden_flag_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Flag-ON output ≡ the frozen pre-conversion oracle, case for case."""
    _require_wave1a_wheel()
    from tests.golden._pull_outcome_harness import verdict_from_token

    monkeypatch.setenv(FLAG_ENV, "1")
    frozen = json.loads(MODULE_FIXTURE.read_text(encoding="utf-8"))
    for row in frozen["classify"]:
        case = row["case"]
        out = classify_last_pull_outcome(
            state=case["state"],
            detail=case["detail"],
            failed=case["failed"],
            stalled=case["stalled"],
            wedged=case["wedged"],
            verdict=verdict_from_token(case["verdict"]),
        )
        assert out.to_dict() == row["out"], f"flag-ON drift on {case}"
    for row in frozen["retained"]:
        case = row["case"]
        retained = (
            None
            if case["retained_pull_id"] is None
            else RetainedPullState(
                snapshot=_snap(**case["snapshot"]), pull_id=case["retained_pull_id"]
            )
        )
        out = classify_retained_pull_outcome(
            retained,
            verdict=verdict_from_token(case["verdict"]),
            verdict_pull_id=case["verdict_pull_id"],
        )
        assert out.to_dict() == row["out"], f"flag-ON drift on {case}"


def test_real_wheel_hypothesis_differential(monkeypatch: pytest.MonkeyPatch) -> None:
    """Flag-ON ≡ flag-OFF over arbitrary inputs (the 4-string bit-equality)."""
    _require_wave1a_wheel()
    from brutex.dashboard.pull_outcome import MARKET_HOURS_DEFERRAL_PREFIX
    from hypothesis import given, settings
    from hypothesis import strategies as st

    details = st.one_of(
        st.text(max_size=80),
        st.text(max_size=40).map(lambda s: MARKET_HOURS_DEFERRAL_PREFIX + s),
        st.text(max_size=40).map(lambda s: f"LakeWriteHalt: {s}"),
        st.sampled_from(sorted(_HALT_NAME_OUTCOMES)).map(lambda n: f"{n}: boom"),
    )

    @settings(max_examples=200, deadline=None)
    @given(
        state=st.sampled_from(["idle", "running", "done", "error", "limbo"]),
        detail=details,
        failed=st.integers(min_value=0, max_value=10),
        stalled=st.booleans(),
        wedged=st.booleans(),
        has_verdict=st.booleans(),
    )
    def check(
        state: str, detail: str, failed: int, stalled: bool, wedged: bool, has_verdict: bool
    ) -> None:
        verdict = _verdict("clean") if has_verdict else None
        monkeypatch.setenv(FLAG_ENV, "1")
        on = _classify(
            state=state,
            detail=detail,
            failed=failed,
            stalled=stalled,
            wedged=wedged,
            verdict=verdict,
        )
        monkeypatch.setenv(FLAG_ENV, "0")
        off = _classify(
            state=state,
            detail=detail,
            failed=failed,
            stalled=stalled,
            wedged=wedged,
            verdict=verdict,
        )
        assert on == off

    check()  # pyright: ignore[reportCallIssue] — hypothesis @given rebinds the signature
