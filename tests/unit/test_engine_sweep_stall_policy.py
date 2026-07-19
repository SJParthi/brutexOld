"""Unit suite for :mod:`brutex.engine.sweep_stall_policy` (G2 — DEC-SWEEP-UNKILLABLE-CORE-001).

Pins the I-1 identity (unscaled default == EXACTLY 300.0, byte-equal to BOTH
existing guard constants), the fail-loud env parses, and the raise-only
scaling law (scaling can NEVER lower the timeout below the default; only the
explicit operator env override can).
"""

from __future__ import annotations

import math
from typing import Final

import pytest
from brutex.engine.sweep_parallel import DEFAULT_STALL_SECONDS
from brutex.engine.sweep_stall_policy import (
    DEFAULT_STALL_BASE_SECONDS,
    DEFAULT_STALL_FACTOR,
    ENV_STALL_FACTOR,
    ENV_STALL_SECONDS,
    StallPolicy,
    resolve_stall_policy,
)
from brutex.engine.sweep_watchdog import DEFAULT_TIMEOUT_SECONDS

#: D-6 mechanized traceability — test name → requirement/gap tokens.
TRACEABILITY: Final[dict[str, tuple[str, ...]]] = {
    "test_g2_default_is_exactly_300": ("G2",),
    "test_g2_default_identity_with_both_existing_guard_constants": ("G2",),
    "test_g2_env_override_wins": ("G2",),
    "test_g2_env_override_may_go_below_default": ("G2",),
    "test_g2_env_junk_raises": ("G2",),
    "test_g2_factor_env_junk_raises": ("G2",),
    "test_g2_scaling_raises_only_never_lowers": ("G2",),
    "test_g2_scaled_boundary_exactly_base_is_default": ("G2",),
    "test_g2_scaled_strictly_above_base": ("G2",),
    "test_g2_factor_env_scales": ("G2",),
    "test_g2_sla_env_seam_feeds_estimate": ("G2",),
    "test_g2_sla_env_junk_raises": ("G2",),
    "test_g2_explicit_estimate_beats_sla_env": ("G2",),
    "test_g2_estimate_junk_raises": ("G2",),
    "test_g2_workers_validated_and_never_lowers": ("G2",),
    "test_g2_policy_construction_validation": ("G2",),
    "test_g2_env_mapping_injected_not_process_env": ("G2",),
}


def test_g2_default_is_exactly_300() -> None:
    """No envs + no estimate resolves to EXACTLY 300.0 with source 'default' (I-1)."""
    policy = resolve_stall_policy(per_tuple_estimate_s=None, workers=1, env={})
    assert policy == StallPolicy(timeout_seconds=300.0, source="default")
    assert policy.timeout_seconds == 300.0


def test_g2_default_identity_with_both_existing_guard_constants() -> None:
    """The base is byte-equal to BOTH existing 300 s guard constants (ONE source)."""
    assert DEFAULT_STALL_BASE_SECONDS == 300.0
    assert DEFAULT_STALL_BASE_SECONDS == DEFAULT_TIMEOUT_SECONDS
    assert DEFAULT_STALL_BASE_SECONDS == DEFAULT_STALL_SECONDS
    assert DEFAULT_STALL_FACTOR == 10.0


def test_g2_env_override_wins() -> None:
    """A set BRUTEX_SWEEP_STALL_SECONDS wins outright over any scaling input."""
    policy = resolve_stall_policy(
        per_tuple_estimate_s=1000.0,
        workers=4,
        env={ENV_STALL_SECONDS: "1234.5", ENV_STALL_FACTOR: "99"},
    )
    assert policy == StallPolicy(timeout_seconds=1234.5, source="env")


def test_g2_env_override_may_go_below_default() -> None:
    """The EXPLICIT operator override is the sole way below 300 (an operator decision)."""
    policy = resolve_stall_policy(
        per_tuple_estimate_s=None, workers=1, env={ENV_STALL_SECONDS: "120"}
    )
    assert policy == StallPolicy(timeout_seconds=120.0, source="env")


@pytest.mark.parametrize("raw", ["abc", "", "0", "-5", "nan", "inf", "-inf", "1e999"])
def test_g2_env_junk_raises(raw: str) -> None:
    """A set-but-junk stall-seconds env fails loudly, naming the variable."""
    with pytest.raises(ValueError, match=ENV_STALL_SECONDS):
        resolve_stall_policy(per_tuple_estimate_s=None, workers=1, env={ENV_STALL_SECONDS: raw})


@pytest.mark.parametrize("raw", ["junk", "", "0", "-2", "nan", "inf"])
def test_g2_factor_env_junk_raises(raw: str) -> None:
    """A set-but-junk stall-factor env fails loudly, naming the variable."""
    with pytest.raises(ValueError, match=ENV_STALL_FACTOR):
        resolve_stall_policy(per_tuple_estimate_s=None, workers=1, env={ENV_STALL_FACTOR: raw})


def test_g2_scaling_raises_only_never_lowers() -> None:
    """A tiny estimate can NEVER pull the timeout below the 300 s base (G2 law)."""
    policy = resolve_stall_policy(per_tuple_estimate_s=0.001, workers=1, env={})
    assert policy == StallPolicy(timeout_seconds=300.0, source="default")


def test_g2_scaled_boundary_exactly_base_is_default() -> None:
    """factor × estimate == 300 exactly stays 'default' (strictly-greater law)."""
    policy = resolve_stall_policy(per_tuple_estimate_s=30.0, workers=1, env={})
    assert policy == StallPolicy(timeout_seconds=300.0, source="default")


def test_g2_scaled_strictly_above_base() -> None:
    """factor × estimate > 300 scales UP with source 'scaled'."""
    policy = resolve_stall_policy(per_tuple_estimate_s=60.0, workers=1, env={})
    assert policy == StallPolicy(timeout_seconds=600.0, source="scaled")


def test_g2_factor_env_scales() -> None:
    """An explicit factor env multiplies the estimate (20 × 60 = 1200)."""
    policy = resolve_stall_policy(
        per_tuple_estimate_s=60.0, workers=1, env={ENV_STALL_FACTOR: "20"}
    )
    assert policy == StallPolicy(timeout_seconds=1200.0, source="scaled")


def test_g2_sla_env_seam_feeds_estimate() -> None:
    """With no caller estimate, BRUTEX_SWEEP_PER_TUPLE_SECONDS feeds the scale."""
    policy = resolve_stall_policy(
        per_tuple_estimate_s=None, workers=1, env={"BRUTEX_SWEEP_PER_TUPLE_SECONDS": "45"}
    )
    assert policy == StallPolicy(timeout_seconds=450.0, source="scaled")


def test_g2_sla_env_junk_raises() -> None:
    """A junk SLA-seam env fails loudly through the SLA guard's own parse."""
    with pytest.raises(ValueError, match="BRUTEX_SWEEP_PER_TUPLE_SECONDS"):
        resolve_stall_policy(
            per_tuple_estimate_s=None,
            workers=1,
            env={"BRUTEX_SWEEP_PER_TUPLE_SECONDS": "bogus"},
        )


def test_g2_explicit_estimate_beats_sla_env() -> None:
    """A caller-provided estimate wins over the SLA env seam."""
    policy = resolve_stall_policy(
        per_tuple_estimate_s=100.0,
        workers=1,
        env={"BRUTEX_SWEEP_PER_TUPLE_SECONDS": "45"},
    )
    assert policy == StallPolicy(timeout_seconds=1000.0, source="scaled")


@pytest.mark.parametrize("estimate", [0.0, -1.0, math.nan, math.inf])
def test_g2_estimate_junk_raises(estimate: float) -> None:
    """A non-positive/non-finite caller estimate fails loudly."""
    with pytest.raises(ValueError, match="per_tuple_estimate_s"):
        resolve_stall_policy(per_tuple_estimate_s=estimate, workers=1, env={})


def test_g2_workers_validated_and_never_lowers() -> None:
    """workers < 1 raises; a bigger pool NEVER shrinks the bound (exact equality)."""
    with pytest.raises(ValueError, match="workers"):
        resolve_stall_policy(per_tuple_estimate_s=None, workers=0, env={})
    one = resolve_stall_policy(per_tuple_estimate_s=60.0, workers=1, env={})
    many = resolve_stall_policy(per_tuple_estimate_s=60.0, workers=64, env={})
    assert one == many == StallPolicy(timeout_seconds=600.0, source="scaled")


def test_g2_policy_construction_validation() -> None:
    """StallPolicy fails loudly on a bad timeout or an unknown source label."""
    with pytest.raises(ValueError, match="timeout_seconds"):
        StallPolicy(timeout_seconds=0.0, source="default")
    with pytest.raises(ValueError, match="timeout_seconds"):
        StallPolicy(timeout_seconds=math.nan, source="default")
    with pytest.raises(ValueError, match="source"):
        StallPolicy(timeout_seconds=300.0, source="magic")


def test_g2_env_mapping_injected_not_process_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """An injected env mapping shields the resolution from the process environment."""
    monkeypatch.setenv(ENV_STALL_SECONDS, "7")
    policy = resolve_stall_policy(per_tuple_estimate_s=None, workers=1, env={})
    assert policy == StallPolicy(timeout_seconds=300.0, source="default")
    from_process = resolve_stall_policy(per_tuple_estimate_s=None, workers=1)
    assert from_process == StallPolicy(timeout_seconds=7.0, source="env")


# ────────── DEC-RUST-SWEEP-OPS-001 — slice-1 routing (wheel-free) ──────────


class TestSweepOpsRouting:
    """Flag-ON routing parity for the scaled stall-timeout tail.

    Wheel-free: a behavior-correct FAKE crate is injected by patching the
    facade's ``importlib`` binding. Flag ON must be value-identical to the
    flag-OFF Python law (which every OTHER test in this file drives).
    """

    @staticmethod
    def _arm_fake_crate(monkeypatch: pytest.MonkeyPatch) -> None:
        from types import SimpleNamespace

        from brutex.engine import sweep_ops_rust

        def stall_scaled_timeout(factor: float, estimate: float, base: float) -> tuple[float, bool]:
            scaled = factor * estimate
            if scaled > base:
                return (scaled, True)
            return (base, False)

        crate = SimpleNamespace(
            __version__="fake",
            join_path_segments=lambda base, segments: base,
            gate_config_fragment=lambda *args: (None, None),
            stall_scaled_timeout=stall_scaled_timeout,
        )
        monkeypatch.setattr(
            sweep_ops_rust,
            "importlib",
            SimpleNamespace(import_module=lambda name: crate),
        )
        monkeypatch.setenv(sweep_ops_rust.SWEEP_OPS_RUST_ENV, "1")
        sweep_ops_rust.reset_sweep_ops_rust_for_tests()

    @staticmethod
    def _reset_facade() -> None:
        from brutex.engine.sweep_ops_rust import reset_sweep_ops_rust_for_tests

        reset_sweep_ops_rust_for_tests()

    def test_flag_on_matches_python_law_both_arms(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._arm_fake_crate(monkeypatch)
        try:
            scaled = resolve_stall_policy(per_tuple_estimate_s=60.0, workers=1, env={})
            assert scaled == StallPolicy(timeout_seconds=600.0, source="scaled")
            unscaled = resolve_stall_policy(per_tuple_estimate_s=1.0, workers=1, env={})
            assert unscaled == StallPolicy(timeout_seconds=300.0, source="default")
        finally:
            self._reset_facade()
