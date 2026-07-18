"""The pull-outcome Rust decision kernel (DEC-RUST-PULL-STATE-001, flip phase).

The ``brutex_pull_state`` kernel is THE ONLY decision path for
``brutex.dashboard.pull_outcome`` — the Python classify-ladder oracle was
deleted with the flip.  This suite pins:

* the removed ``BRUTEX_PULL_STATE_RUST`` knob refusing LOUDLY when still set
  (ANY spelling — the ``PullStateFlagRemovedError`` law);
* kernel AUTHORITY — a deliberately-WRONG fake kernel's codes are composed
  and SERVED (proving the consult) + every composition arm (verdict markers,
  halt-table headline keys, the failed-count compose, every detail code);
* FAIL-CLOSED faults — wheel missing/stale, a raising kernel, every facade
  refuse-poison shape and every module-side out-of-vocabulary code is the
  typed ``PullStateKernelError`` raise, never a fabricated wire token;
* the REAL wheel — the frozen-golden replay + a hypothesis differential
  against an IN-TEST reimplementation of the frozen classification law
  (test code may reimplement the law; production may not).
"""

# Whitebox imports of module-private composition tables (sanctioned — the
# unit-suite precedent).
# pyright: reportPrivateUsage=none

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
    MARKET_HOURS_DEFERRAL_PREFIX,
    LastPullOutcome,
    _from_verdict,
    classify_last_pull_outcome,
    classify_retained_pull_outcome,
)
from brutex.dashboard.pull_progress import ProgressSnapshot
from brutex.pull_state_rust import PullStateFlagRemovedError, PullStateKernelError

FLAG_ENV = "BRUTEX_PULL_STATE_RUST"
FIXTURES = Path(__file__).resolve().parents[1] / "golden" / "fixtures"
MODULE_FIXTURE = FIXTURES / "pull_outcome_classification.json"


class _BombKernelModule:
    """A ``sys.modules`` plant that RECORDS every attribute access, then
    raises ``AttributeError``.

    CodeQL posture: the bomb raises ``AttributeError`` (never
    ``AssertionError``) — the shape ``getattr(mod, name, default)`` swallows,
    so the facade's resolver reads the plant as a wheel LACKING the symbol
    and refuses ``reason=wheel_stale``.  The teeth are BOTH the typed raise
    and the recorded ``touches`` (proof the kernel module was consulted).
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
    """Call the classifier with defaults (each test overrides its own axis)."""
    return classify_last_pull_outcome(
        state=state,
        detail=detail,
        failed=failed,
        stalled=stalled,
        wedged=wedged,
        verdict=verdict,
    )


def _verdict(kind: str, *, action: str = "do the thing") -> dict[str, Any]:
    """A minimal :meth:`PullVerdict.to_dict`-shaped mapping."""
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
    """Install a fake ``brutex_pull_state`` wheel carrying the given kernels."""
    ns = SimpleNamespace()
    if classify is not None:
        ns.classify_pull_outcome = classify
    if correlates is not None:
        ns.verdict_correlates = correlates
    monkeypatch.setitem(sys.modules, "brutex_pull_state", ns)


class TestRemovedFlagRefusal:
    """The retired opt-in knob refuses LOUDLY when still set (ANY spelling)."""

    @pytest.mark.parametrize("value", ["1", "0", "true", "off"])
    def test_classify_refuses_a_set_flag(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        """Truthy AND falsy spellings alike — the knob itself is removed."""
        monkeypatch.setenv(FLAG_ENV, value)
        with pytest.raises(PullStateFlagRemovedError, match=FLAG_ENV):
            _classify(state="running")

    def test_retained_correlation_refuses_a_set_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The correlation consult checks the refusal guard too."""
        monkeypatch.setenv(FLAG_ENV, "1")
        retained = RetainedPullState(snapshot=_snap(), pull_id="p1")
        with pytest.raises(PullStateFlagRemovedError, match=FLAG_ENV):
            classify_retained_pull_outcome(
                retained, verdict=_verdict("clean"), verdict_pull_id="p1"
            )


class TestKernelCodesCompose:
    """The kernel's codes are composed and SERVED (the authority tooth)."""

    def test_wrong_kernel_answer_is_served(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The frozen law says "running"; the fake kernel says wedged — and
        # the module serves the kernel's decision, proving it is consulted.
        _fake_wheel(monkeypatch, classify=lambda *a: ("wedged", "bad", "wedged", "input"))
        out = _classify(state="running", detail="beat")
        assert (out.outcome, out.tone) == ("wedged", "bad")
        assert out.headline == "pull WEDGED — restart required"
        assert out.detail == "beat"

    def test_halt_table_headline_code_composes_from_the_python_table(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``halt:<Name>`` headline codes key the retained Python table."""
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
        """The failed-count wording is composed Python-side off the input."""
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
        """``@verdict`` hands the fold to ``_from_verdict`` → ``banner_for``."""
        _fake_wheel(monkeypatch, classify=lambda *a: ("@verdict", "", "", ""))
        out = _classify(state="done", verdict=_verdict("quota_exhausted"))
        assert (out.outcome, out.tone) == ("verdict_quota_exhausted", "warn")

    def test_prepull_marker_carries_the_provenance_detail(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``@verdict_prepull`` folds with the stamped-before-start note."""
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
        """Each detail code renders its pinned Python-side composition."""
        _fake_wheel(monkeypatch, classify=lambda *a: ("completed", "ok", "completed", detail_code))
        out = _classify(state="done", detail=detail)
        assert out.detail == expected

    def test_kernel_sees_the_packed_inputs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The consult packs (state, detail, failed, stalled, wedged, has_verdict)."""
        seen: list[tuple[Any, ...]] = []

        def spy(*args: Any) -> tuple[str, str, str, str]:
            seen.append(args)
            return ("running", "ok", "running", "input")

        _fake_wheel(monkeypatch, classify=spy)
        _classify(state="running", detail="d", failed=2, stalled=True, verdict=_verdict("clean"))
        assert seen == [("running", "d", 2, True, False, True)]


class TestKernelFaultsFailClosed:
    """Every wheel fault is the typed raise — no oracle exists to degrade to."""

    def test_missing_wheel_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "brutex_pull_state", None)  # ImportError
        with pytest.raises(PullStateKernelError, match="wheel_missing"):
            _classify(state="error", detail="LakeWriteHalt: lake write halted: disk_full")

    def test_stale_wheel_missing_symbol_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A Phase-1-only wheel: pull_allowed_us present, the outcome symbols absent.
        monkeypatch.setitem(
            sys.modules, "brutex_pull_state", SimpleNamespace(pull_allowed_us=lambda *a: True)
        )
        with pytest.raises(PullStateKernelError, match="wheel_stale"):
            _classify(state="running")

    def test_bomb_module_is_touched_and_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The recording bomb proves the consult AND the typed refusal."""
        bomb = _BombKernelModule()
        monkeypatch.setitem(sys.modules, "brutex_pull_state", bomb)
        with pytest.raises(PullStateKernelError, match="wheel_stale"):
            _classify(state="running")
        assert "classify_pull_outcome" in bomb.touches

    def test_raising_kernel_raises_kernel_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(*a: Any) -> tuple[str, str, str, str]:
            raise RuntimeError("kernel exploded")

        _fake_wheel(monkeypatch, classify=boom)
        with pytest.raises(PullStateKernelError, match="kernel_error"):
            _classify(state="done", detail="note")

    @pytest.mark.parametrize(
        "poison",
        [
            "not a tuple",
            ("too", "short"),
            ("a", "b", "c", 4),
            ["completed", "ok", "completed", "completed"],
        ],
    )
    def test_poisoned_result_shape_raises(
        self, monkeypatch: pytest.MonkeyPatch, poison: object
    ) -> None:
        """The facade's shape gate (a non-4-tuple-of-str) refuses loudly."""
        _fake_wheel(monkeypatch, classify=lambda *a: poison)
        with pytest.raises(PullStateKernelError, match="kernel_invalid"):
            _classify(state="running")

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
    def test_unrecognised_codes_raise(
        self, monkeypatch: pytest.MonkeyPatch, codes: tuple[str, str, str, str]
    ) -> None:
        """The module-side vocabulary gate refuses out-of-contract codes —
        never an unvalidated token on the wire."""
        _fake_wheel(monkeypatch, classify=lambda *a: codes)
        with pytest.raises(PullStateKernelError, match="kernel_invalid"):
            _classify(state="running", detail="beat")

    def test_verdict_marker_without_a_verdict_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A verdict marker with no verdict to fold is structurally
        impossible from the real kernel — refused, never guessed."""
        _fake_wheel(monkeypatch, classify=lambda *a: ("@verdict", "", "", ""))
        with pytest.raises(PullStateKernelError, match="verdict marker without a verdict"):
            _classify(state="done", detail="note", verdict=None)


class TestRetainedCorrelationKernel:
    """The correlation law on ``classify_retained_pull_outcome`` is kernel-fed."""

    def test_kernel_true_consults_the_verdict(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A (deliberately-wrong) True correlation is SERVED — authority."""
        _fake_wheel(
            monkeypatch,
            classify=lambda *a: ("@verdict", "", "", ""),
            correlates=lambda *a: True,
        )
        retained = RetainedPullState(snapshot=_snap(), pull_id="p1")
        out = classify_retained_pull_outcome(
            retained, verdict=_verdict("clean"), verdict_pull_id="OTHER"
        )
        assert out.outcome == "clean"  # the kernel's (wrong) law is served

    def test_kernel_false_drops_the_verdict(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A False correlation drops the verdict even on an id match."""
        _fake_wheel(
            monkeypatch,
            classify=lambda *a: ("completed", "ok", "completed", "completed"),
            correlates=lambda *a: False,
        )
        retained = RetainedPullState(snapshot=_snap(), pull_id="p1")
        out = classify_retained_pull_outcome(
            retained, verdict=_verdict("clean"), verdict_pull_id="p1"
        )
        assert out.outcome == "completed"

    def test_non_bool_correlation_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A truthy int pun from a stale wheel is refused, never served."""
        _fake_wheel(monkeypatch, correlates=lambda *a: 1)  # non-bool → poison
        retained = RetainedPullState(snapshot=_snap(), pull_id="p1")
        with pytest.raises(PullStateKernelError, match="kernel_invalid"):
            classify_retained_pull_outcome(
                retained, verdict=_verdict("clean"), verdict_pull_id="p1"
            )

    def test_never_ran_slot_takes_no_correlation_kernel(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``retained=None`` never consults the correlation kernel."""

        def boom(*a: Any) -> bool:
            raise AssertionError("the never-ran arm must not consult the correlation kernel")

        _fake_wheel(monkeypatch, classify=lambda *a: ("never_run", "idle", "never_run", "empty"))
        sys.modules["brutex_pull_state"].verdict_correlates = boom  # type: ignore[attr-defined]
        out = classify_retained_pull_outcome(None, verdict=None, verdict_pull_id="")
        assert out.outcome == "never_run"


class TestFacadeAccessors:
    """The pull-outcome accessors on ``brutex.pull_state_rust`` directly."""

    def test_classify_missing_wheel_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "brutex_pull_state", None)
        with pytest.raises(PullStateKernelError, match="wheel_missing"):
            pull_state_rust.rust_classify_pull_outcome("idle", "", 0, False, False, False)

    def test_classify_stale_wheel_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "brutex_pull_state", SimpleNamespace())
        with pytest.raises(PullStateKernelError, match="wheel_stale"):
            pull_state_rust.rust_classify_pull_outcome("idle", "", 0, False, False, False)
        with pytest.raises(PullStateKernelError, match="wheel_stale"):
            pull_state_rust.rust_verdict_correlates("a", "b")

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
        with pytest.raises(PullStateKernelError, match="kernel_invalid"):
            pull_state_rust.rust_verdict_correlates("p", "p")

    def test_correlates_raising_kernel_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(*a: Any) -> bool:
            raise ValueError("nope")

        monkeypatch.setitem(
            sys.modules, "brutex_pull_state", SimpleNamespace(verdict_correlates=boom)
        )
        with pytest.raises(PullStateKernelError, match="kernel_error"):
            pull_state_rust.rust_verdict_correlates("p", "p")


def _snap(**overrides: Any) -> ProgressSnapshot:
    """A minimal :class:`ProgressSnapshot` for retained-state drives."""
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
# The REAL wheel — the frozen golden replay + the kernel-vs-inline-law
# differential.  The wheel is REQUIRED (fail-closed facade posture — a
# missing wheel fails these loudly, never skips them green).
# ---------------------------------------------------------------------------


def test_real_wheel_replays_the_frozen_golden() -> None:
    """Kernel-backed output ≡ the frozen pre-conversion capture, case for case."""
    from tests.golden._pull_outcome_harness import verdict_from_token

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
        assert out.to_dict() == row["out"], f"kernel drift on {case}"
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
        assert out.to_dict() == row["out"], f"kernel drift on {case}"


def _inline_classify(
    state: str,
    detail: str,
    failed: int,
    stalled: bool,
    wedged: bool,
    verdict: dict[str, Any] | None,
) -> LastPullOutcome:
    """The frozen classification LAW, reimplemented in test code.

    A faithful transcription of the pre-flip Python oracle ladder (test-only
    — the production copy lives in the Rust kernel): the running arms, the
    error-detail classification (the market-hours prefix, the
    ``LakeWriteHalt`` reason split, the halt-name table, the generic-error
    arm), the done-arm precedence (deferral BEFORE the verdict; ``failed >
    0`` outranks a clean verdict) and the idle fall-through.  The verdict
    fold rides the module's RETAINED ``_from_verdict`` composition (the
    kernel only ever emits the ``@verdict`` markers for it).
    """
    if state == "running":
        if wedged:
            return LastPullOutcome("wedged", "bad", "pull WEDGED — restart required", detail)
        if stalled:
            return LastPullOutcome(
                "running_stalled_suspect",
                "warn",
                "pull running — STALLED-SUSPECT (no progress beats)",
                detail,
            )
        return LastPullOutcome("running", "ok", "pull running", detail)
    if state == "error":
        if detail.startswith(MARKET_HOURS_DEFERRAL_PREFIX):
            return LastPullOutcome(
                "deferred_market_hours",
                "warn",
                "pull deferred — NSE market hours (08:45-15:35 IST)",
                detail,
            )
        name = detail.split(":", 1)[0]
        if name == "LakeWriteHalt":
            if "disk_full" in detail:
                return LastPullOutcome("halted_disk_full", "bad", "pull HALTED — disk full", detail)
            if "out_of_memory" in detail:
                return LastPullOutcome("halted_oom", "bad", "pull HALTED — out of memory", detail)
            return LastPullOutcome(
                "halted_lake_write", "bad", "pull HALTED — lake write halted", detail
            )
        mapped = _HALT_NAME_OUTCOMES.get(name)
        if mapped is not None:
            return LastPullOutcome(mapped[0], mapped[1], mapped[2], detail)
        return LastPullOutcome("error", "bad", "pull FAILED", detail)
    if state == "done":
        if detail.startswith(MARKET_HOURS_DEFERRAL_PREFIX):
            return LastPullOutcome(
                "deferred_market_hours",
                "warn",
                "pull deferred — NSE market hours (08:45-15:35 IST)",
                detail,
            )
        if failed > 0:
            return LastPullOutcome(
                "completed_with_failures",
                "warn",
                f"pull finished — {failed} unit(s) failed/quarantined (will re-pull)",
                detail,
            )
        if verdict is not None:
            return _from_verdict(verdict)
        return LastPullOutcome(
            "completed",
            "ok",
            "pull finished",
            detail if detail else "completed — no classified verdict recorded for this run",
        )
    if verdict is not None:
        return _from_verdict(verdict, provenance="stamped before any pull started")
    return LastPullOutcome("never_run", "idle", "no pull has run in this dashboard process", "")


def test_real_wheel_hypothesis_vs_inline_law() -> None:
    """Kernel-backed classification ≡ the inline law over arbitrary inputs."""
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
        got = _classify(
            state=state,
            detail=detail,
            failed=failed,
            stalled=stalled,
            wedged=wedged,
            verdict=verdict,
        )
        assert got == _inline_classify(state, detail, failed, stalled, wedged, verdict)

    check()  # pyright: ignore[reportCallIssue] — hypothesis @given rebinds the signature
