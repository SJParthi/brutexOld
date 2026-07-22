"""Unit gate for :mod:`brutex.engine.rust_parity_core` (slice-4 M8).

100% line + branch over the ONE COMMON parity wrapper core: the per-family
policy machinery (burn-in for (a)/(b), ALWAYS-ON for (b*)), the
ship-the-oracle-on-mismatch law, the autorevert latch, the warn-once
taxonomy (``wheel_missing`` / ``wheel_stale`` / ``kernel_error`` /
``kernel_result_refused`` / ``divergence`` / ``kernel_disabled`` /
``flag_unrecognised``), the bit-exact compare-key helpers (signed zeros
included) and the COMMON env-flag resolution.
"""

from __future__ import annotations

import math
import sys
import typing
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar

import pytest
from brutex.engine.rust_parity_core import (
    BURN_IN_SAMPLES,
    DIVERGENCE_COUNTER,
    POLICY_ALWAYS,
    POLICY_BURN_IN,
    ParityFamily,
    ParityPolicy,
    env_flag_enabled,
    pack_float,
    pack_float_rows,
    pack_floats,
    resolve_kernel_symbol,
    run_with_parity,
)
from brutex.observability import MetricsError, get_registry, render_prometheus
from structlog.testing import capture_logs

if TYPE_CHECKING:
    from collections.abc import Callable

_ENV = "BRUTEX_TEST_PARITY_FLAG"


def _family(policy: str = POLICY_BURN_IN, name: str = "test.family") -> ParityFamily:
    assert policy in (POLICY_BURN_IN, POLICY_ALWAYS)
    return ParityFamily(name, policy=policy)  # type: ignore[arg-type]


class _Counting:
    """A zero-arg closure that counts its calls and returns a fixed value."""

    def __init__(self, value: object) -> None:
        self.value = value
        self.calls = 0

    def __call__(self) -> object:
        self.calls += 1
        return self.value


# ── ParityFamily state machine ──────────────────────────────────────────


class TestParityFamily:
    def test_fresh_family_is_enabled_with_full_burn_in(self) -> None:
        fam = _family()
        assert not fam.disabled()
        assert fam.self_check_pending()

    def test_disable_latches_and_logs_error_once(self) -> None:
        fam = _family(name="test.latch")
        with capture_logs() as logs:
            fam.disable("divergence", detail="x")
            fam.disable("divergence", detail="y")  # repeat is a no-op
        assert fam.disabled()
        events = [e for e in logs if e["event"] == "rust_parity.autoreverted"]
        assert len(events) == 1
        assert events[0]["family"] == "test.latch"
        assert events[0]["reason"] == "divergence"

    def test_disable_increments_the_divergence_counter(self) -> None:
        fam = _family(name="test.counter")
        before = _counter_value("test.counter")
        fam.disable("divergence")
        assert _counter_value("test.counter") == before + 1.0
        fam.disable("divergence")  # latched — never double-counted
        assert _counter_value("test.counter") == before + 1.0

    def test_burn_in_budget_consumes_and_exhausts(self) -> None:
        fam = _family()
        for _ in range(BURN_IN_SAMPLES):
            assert fam.self_check_pending()
            fam.consume_sample()
        assert not fam.self_check_pending()
        fam.consume_sample()  # clamped at zero — never negative
        assert not fam.self_check_pending()

    def test_always_policy_always_pends_and_never_consumes(self) -> None:
        fam = _family(POLICY_ALWAYS)
        for _ in range(BURN_IN_SAMPLES + 5):
            assert fam.self_check_pending()
            fam.consume_sample()  # a no-op for the always policy
        assert fam.self_check_pending()

    def test_warn_fallback_once_per_reason(self) -> None:
        fam = _family()
        with capture_logs() as logs:
            fam.warn_fallback_once("wheel_missing")
            fam.warn_fallback_once("wheel_missing")
            fam.warn_fallback_once("wheel_stale")
        events = [e for e in logs if e["event"] == "rust_parity.fallback"]
        assert [e["reason"] for e in events] == ["wheel_missing", "wheel_stale"]

    def test_refuse_result_once(self) -> None:
        fam = _family()
        with capture_logs() as logs:
            fam.refuse_result_once(result="junk")
            fam.refuse_result_once(result="junk")
        events = [e for e in logs if e["event"] == "rust_parity.kernel_result_refused"]
        assert len(events) == 1

    def test_mark_engaged_once(self) -> None:
        fam = _family()
        with capture_logs() as logs:
            fam.mark_engaged()
            fam.mark_engaged()
        events = [e for e in logs if e["event"] == "rust_parity.engaged"]
        assert len(events) == 1

    def test_reset_for_tests_restores_fresh_view(self) -> None:
        fam = _family()
        fam.disable("divergence")
        fam.warn_fallback_once("kernel_disabled")
        fam.mark_engaged()
        fam.reset_for_tests()
        assert not fam.disabled()
        assert fam.self_check_pending()
        with capture_logs() as logs:
            fam.mark_engaged()  # memo cleared — fires again
        assert [e for e in logs if e["event"] == "rust_parity.engaged"]

    def test_reset_for_tests_remaining_zero_is_post_burn_in(self) -> None:
        fam = _family()
        fam.reset_for_tests(remaining=0)
        assert not fam.self_check_pending()


def _counter_value(family: str) -> float:
    registry = get_registry()
    try:
        return registry.counter_value(DIVERGENCE_COUNTER, labels=(family,))
    except MetricsError:  # unregistered counter reads as zero
        return 0.0


# ── env_flag_enabled ────────────────────────────────────────────────────


class TestEnvFlagEnabled:
    @pytest.mark.parametrize("value", ["1", "true", "YES", " on "])
    def test_truthy_forces_on(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        monkeypatch.setenv(_ENV, value)
        assert env_flag_enabled(_ENV, default=False, warn_family=_family()) is True

    @pytest.mark.parametrize("value", ["0", "false", "NO", " off "])
    def test_falsy_forces_off(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        monkeypatch.setenv(_ENV, value)
        assert env_flag_enabled(_ENV, default=True, warn_family=_family()) is False

    @pytest.mark.parametrize("default", [True, False])
    def test_unset_follows_the_default(
        self, monkeypatch: pytest.MonkeyPatch, default: bool
    ) -> None:
        monkeypatch.delenv(_ENV, raising=False)
        assert env_flag_enabled(_ENV, default=default, warn_family=_family()) is default

    def test_whitespace_empty_follows_the_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(_ENV, "   ")
        assert env_flag_enabled(_ENV, default=True, warn_family=_family()) is True

    def test_unrecognised_fails_safe_off_with_one_warning(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_ENV, "flase")
        fam = _family()
        with capture_logs() as logs:
            assert env_flag_enabled(_ENV, default=True, warn_family=fam) is False
            assert env_flag_enabled(_ENV, default=True, warn_family=fam) is False
        events = [e for e in logs if e["event"] == "rust_parity.fallback"]
        assert len(events) == 1
        assert events[0]["reason"] == "flag_unrecognised"


# ── resolve_kernel_symbol ───────────────────────────────────────────────


class TestResolveKernelSymbol:
    def test_present_symbol_resolves(self) -> None:
        kernel = resolve_kernel_symbol(_family(), "ranking_cr_div_i64")
        assert callable(kernel)

    def test_wheel_missing_warns_once_and_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(sys.modules, "fno_math", None)
        fam = _family()
        with capture_logs() as logs:
            assert resolve_kernel_symbol(fam, "ranking_cr_div_i64") is None
            assert resolve_kernel_symbol(fam, "ranking_cr_div_i64") is None
        events = [e for e in logs if e["event"] == "rust_parity.fallback"]
        assert [e["reason"] for e in events] == ["wheel_missing"]

    def test_wheel_stale_warns_once_and_returns_none(self) -> None:
        fam = _family()
        with capture_logs() as logs:
            assert resolve_kernel_symbol(fam, "no_such_symbol_ever") is None
            assert resolve_kernel_symbol(fam, "no_such_symbol_ever") is None
        events = [e for e in logs if e["event"] == "rust_parity.fallback"]
        assert [e["reason"] for e in events] == ["wheel_stale"]


# ── compare-key helpers ─────────────────────────────────────────────────


class TestPackHelpers:
    def test_pack_float_discriminates_signed_zero(self) -> None:
        assert pack_float(0.0) != pack_float(-0.0)
        assert pack_float(1.5) == pack_float(1.5)

    def test_pack_float_handles_nan_and_inf(self) -> None:
        assert pack_float(math.nan) == pack_float(math.nan)
        assert pack_float(math.inf) != pack_float(-math.inf)

    def test_pack_floats_is_element_wise(self) -> None:
        assert pack_floats([1.0, -0.0]) == (pack_float(1.0), pack_float(-0.0))
        assert pack_floats([1.0, 0.0]) != pack_floats([1.0, -0.0])

    def test_pack_float_rows_is_row_wise(self) -> None:
        rows = pack_float_rows([[1.0, 2.0], [3.0]])
        assert rows == (pack_floats([1.0, 2.0]), pack_floats([3.0]))


# ── run_with_parity — the policy engine ─────────────────────────────────


class TestRunWithParity:
    def _run(
        self,
        fam: ParityFamily,
        kernel: Callable[[], object],
        oracle: Callable[[], object],
        validate: Callable[[object], bool] | None = None,
    ) -> object:
        return run_with_parity(
            fam, kernel=kernel, oracle=oracle, key=lambda v: v, validate=validate
        )

    def test_disabled_family_serves_the_oracle_without_calling_the_kernel(self) -> None:
        fam = _family()
        fam.disable("divergence")
        kernel = _Counting("rust")
        with capture_logs() as logs:
            assert self._run(fam, kernel, lambda: "oracle") == "oracle"
        assert kernel.calls == 0
        events = [e for e in logs if e["event"] == "rust_parity.fallback"]
        assert [e["reason"] for e in events] == ["kernel_disabled"]

    @pytest.mark.parametrize("exc", [ValueError("v"), TypeError("t"), OverflowError("o")])
    def test_kernel_boundary_errors_degrade_to_the_oracle(self, exc: Exception) -> None:
        fam = _family()

        def _raises() -> object:
            raise exc

        with capture_logs() as logs:
            assert self._run(fam, _raises, lambda: "oracle") == "oracle"
        events = [e for e in logs if e["event"] == "rust_parity.fallback"]
        assert [e["reason"] for e in events] == ["kernel_error"]

    def test_memory_error_reraises_bare(self) -> None:
        fam = _family()

        def _oom() -> object:
            raise MemoryError

        with pytest.raises(MemoryError):
            self._run(fam, _oom, lambda: "oracle")

    def test_unexpected_exception_propagates(self) -> None:
        fam = _family()

        def _bug() -> object:
            raise RuntimeError("genuine code bug")

        with pytest.raises(RuntimeError, match="genuine code bug"):
            self._run(fam, _bug, lambda: "oracle")

    def test_invalid_result_is_refused_loudly_and_the_oracle_serves(self) -> None:
        fam = _family()
        with capture_logs() as logs:
            got = self._run(fam, lambda: "junk", lambda: "oracle", validate=lambda v: False)
        assert got == "oracle"
        assert [e for e in logs if e["event"] == "rust_parity.kernel_result_refused"]
        assert not fam.disabled()  # refused ≠ latched (a shape bug, not a divergence)

    def test_valid_result_passes_the_validate_gate(self) -> None:
        fam = _family(POLICY_ALWAYS)
        got = self._run(fam, lambda: "same", lambda: "same", validate=lambda v: v == "same")
        assert got == "same"

    def test_always_policy_match_ships_the_kernel_value_and_engages(self) -> None:
        fam = _family(POLICY_ALWAYS)
        oracle = _Counting((1, 2.5))
        with capture_logs() as logs:
            got = run_with_parity(fam, kernel=lambda: (1, 2.5), oracle=oracle, key=lambda v: v)
        assert got == (1, 2.5)
        assert oracle.calls == 1  # ALWAYS double-computes
        assert [e for e in logs if e["event"] == "rust_parity.engaged"]

    def test_always_policy_mismatch_ships_the_oracle_and_latches(self) -> None:
        fam = _family(POLICY_ALWAYS, name="test.always_div")
        with capture_logs() as logs:
            got = run_with_parity(fam, kernel=lambda: 1.0, oracle=lambda: 2.0, key=pack_float)
        assert got == 2.0  # the oracle value ships — never a divergent bit
        assert fam.disabled()
        events = [e for e in logs if e["event"] == "rust_parity.autoreverted"]
        assert len(events) == 1
        assert events[0]["reason"] == "divergence"

    def test_signed_zero_mismatch_is_a_divergence(self) -> None:
        # struct.pack keys discriminate -0.0 from +0.0 — float == would not.
        fam = _family(POLICY_ALWAYS)
        got = run_with_parity(fam, kernel=lambda: -0.0, oracle=lambda: 0.0, key=pack_float)
        assert pack_float(got) == pack_float(0.0)
        assert fam.disabled()

    def test_burn_in_match_consumes_one_sample_and_ships_the_kernel_value(self) -> None:
        fam = _family()
        oracle = _Counting("same")
        got = run_with_parity(fam, kernel=lambda: "same", oracle=oracle, key=lambda v: v)
        assert got == "same"
        assert oracle.calls == 1
        # One sample consumed; the budget still pends until exhausted.
        assert fam.self_check_pending()

    def test_burn_in_mismatch_latches_and_ships_the_oracle(self) -> None:
        fam = _family(name="test.burnin_div")
        got = run_with_parity(fam, kernel=lambda: "rust", oracle=lambda: "py", key=lambda v: v)
        assert got == "py"
        assert fam.disabled()

    def test_post_burn_in_serves_the_kernel_without_the_oracle(self) -> None:
        fam = _family()
        fam.reset_for_tests(remaining=0)
        oracle = _Counting("py")
        with capture_logs() as logs:
            got = run_with_parity(fam, kernel=lambda: "rust", oracle=oracle, key=lambda v: v)
        assert got == "rust"
        assert oracle.calls == 0  # steady state — no double compute
        assert [e for e in logs if e["event"] == "rust_parity.engaged"]

    def test_burn_in_budget_exhausts_after_the_configured_samples(self) -> None:
        fam = _family()
        oracle = _Counting("v")
        for _ in range(BURN_IN_SAMPLES + 3):
            run_with_parity(fam, kernel=lambda: "v", oracle=oracle, key=lambda v: v)
        # The oracle ran exactly BURN_IN_SAMPLES times, then stopped.
        assert oracle.calls == BURN_IN_SAMPLES


# ── mutation hardening (slice-4 PR-1 review round 7 — the R7.2/R7.3 fix) ──


class TestMutationHardening:
    """Deterministic pins killing the round-7 in-session mutmut survivors.

    The wired mutation gate (``TestMutationWorkflowWiring`` below) demands
    ZERO surviving mutants, so every behavioural literal the module exposes
    — the policy tokens (the ``ParityPolicy`` wire vocabulary a binding may
    also spell as raw strings), the burn-in budget, the Prometheus counter
    name / help / label, the engaged-memo namespace key — is pinned EXACTLY
    here (the ``test_lake_stats_manifest.py`` ``TestMutationHardening``
    precedent). Structural residue was RESTRUCTURED killable instead of
    pragma'd: the logger name rides ``__name__``, the ``str | None``
    instance annotation moved to class level (``get_type_hints``-evaluated,
    pinned below), and ``resolve_kernel_symbol``'s local union annotation
    became an ``object`` + narrowing non-string ``cast`` (no mutable
    literal/operator on the line) — the lake_stats endgame-round-2 law that
    a runtime-unevaluated-annotation pragma hides KILLABLE siblings.
    """

    def test_policy_and_knob_literals_pinned(self) -> None:
        assert POLICY_BURN_IN == "burn_in"
        assert POLICY_ALWAYS == "always"
        assert typing.get_args(ParityPolicy) == ("burn_in", "always")
        assert BURN_IN_SAMPLES == 16
        assert DIVERGENCE_COUNTER == "brutex_rust_parity_divergence_total"

    def test_engaged_memo_key_namespaced_apart_from_warn_reasons(self) -> None:
        from brutex.engine.rust_parity_core import (
            _ENGAGED_KEY,  # pyright: ignore[reportPrivateUsage]
        )

        # The exact namespace token — AND the structural law it exists for:
        # colliding with any warn-once reason would suppress the engaged
        # INFO after a prior fallback of that reason (or vice versa).
        assert _ENGAGED_KEY == "__engaged__"
        warn_reasons = {
            "wheel_missing",
            "wheel_stale",
            "kernel_error",
            "kernel_disabled",
            "flag_unrecognised",
            "kernel_result_refused",
        }
        assert _ENGAGED_KEY not in warn_reasons

    def test_logger_is_named_after_the_module(self) -> None:
        from brutex.engine import rust_parity_core

        # ``get_logger(__name__)`` (the zero-survivor siblings' idiom) —
        # structlog's lazy proxy records the name in ``_logger_factory_args``.
        assert rust_parity_core._LOG._logger_factory_args == (  # pyright: ignore[reportPrivateUsage]
            "brutex.engine.rust_parity_core",
        )

    def test_disabled_reason_annotation_evaluates_to_optional_str(self) -> None:
        # Class-level so ``get_type_hints`` EVALUATES it — a ``| → &``
        # annotation mutant raises TypeError here instead of surviving
        # runtime-unevaluated (PEP 563/526).
        hints = typing.get_type_hints(ParityFamily)
        assert hints["_disabled_reason"] == (str | None)

    def test_run_with_parity_typevar_pinned(self) -> None:
        from brutex.engine.rust_parity_core import _T  # pyright: ignore[reportPrivateUsage]

        assert isinstance(_T, TypeVar)
        assert _T.__name__ == "_T"

    def test_divergence_counter_help_and_label_on_the_metrics_wire(self) -> None:
        fam = ParityFamily("test.render_pin", policy=POLICY_BURN_IN)
        fam.disable("divergence")
        rendered = render_prometheus(get_registry())
        assert (
            "# HELP brutex_rust_parity_divergence_total "
            "Rust-vs-Python parity divergences (oracle shipped, family latched)" in rendered
        )
        assert 'brutex_rust_parity_divergence_total{family="test.render_pin"} 1' in rendered

    def test_consume_sample_clamps_at_zero(self) -> None:
        fam = ParityFamily("test.clamp", policy=POLICY_BURN_IN)
        fam.reset_for_tests(remaining=0)
        fam.consume_sample()  # docstring law: "Clamped at zero" — never -1
        assert fam._remaining == 0  # pyright: ignore[reportPrivateUsage]

    def test_consume_sample_always_family_budget_untouched(self) -> None:
        fam = ParityFamily("test.always_budget", policy=POLICY_ALWAYS)
        fam.consume_sample()  # docstring law: ALWAYS budget untouched
        assert fam._remaining == BURN_IN_SAMPLES  # pyright: ignore[reportPrivateUsage]


class TestMutationWorkflowWiring:
    """Pin this module INTO ``mutation.yml``'s per-module zero-survivor loop.

    Slice-4 PR-1 review round 7 (R7.2/R7.3): the refuter caught BOTH new
    parity wrapper modules shipping with NO ``--paths-to-mutate`` naming
    them — the exact unwired-gate class MIGRATION #1614's round 8 recorded
    (Golden Rule #10) — while the same PR added ``ranking.rs`` to
    cargo-mutants while the slice-4 design's CI-wiring clause
    (DEC-RANKING-RUST-001) requires "the weekly mutmut loop" for the
    wrapper Python modules. The
    module:testfile:timeout pair now sits in the workflow's per-module
    ``mutmut`` loop (empty third field = the historical wrap-less runner —
    no spin-capable loop constructs here, bounded ``for`` only; and
    ``run_with_parity`` was restructured off PEP-695 generics, which
    mutmut 2.5.1's parso hard-fails to parse). This deterministic pin (the
    ``test_lake_stats_manifest.py`` precedent) fails if the pair is ever
    dropped. HONEST: it pins the WIRING only — the weekly run executes on
    its own schedule/push triggers.
    """

    _WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "mutation.yml"

    def test_module_paired_with_this_suite_in_the_mutmut_loop(self) -> None:
        text = self._WORKFLOW.read_text(encoding="utf-8")
        assert (
            '"brutex/engine/rust_parity_core.py:tests/unit/test_engine_rust_parity_core.py:"'
            in text
        ), "rust_parity_core.py is not wired into mutation.yml's per-module mutmut loop"
