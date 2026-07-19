"""Tests for :mod:`brutex.config.backup_env_rust` — the default-OFF seam.

DEC-CONFIG-RUST-LEAVES-001 (MIGRATION.md #1943, slice T35 milestone 3).
Wheel-free fake-crate coverage of every facade layer + the hypothesis
subset-shape differential vs the live oracle in
:class:`TestRealCrateParity` (skips cleanly when the wheel is absent; the
``core-rust-parity.yml`` lane runs it for real).
"""

from __future__ import annotations

import sys
import types
from collections.abc import Iterator
from pathlib import Path

import pytest
from brutex.config import backup_env, backup_env_rust, seam_core

# ── Harness ─────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _fresh_seam(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv(backup_env_rust.BACKUPENV_RUST_ENV, raising=False)
    backup_env_rust.reset_backupenv_rust_for_tests()
    yield
    backup_env_rust.reset_backupenv_rust_for_tests()


def _oracle(value: str) -> bool:
    return backup_env._valid_verify_subset_python(value)  # pyright: ignore[reportPrivateUsage]


def _fake_crate(**overrides: object) -> types.ModuleType:
    crate = types.ModuleType("brutex_core")
    crate.__version__ = "0.0-fake"  # type: ignore[attr-defined]
    defaults: dict[str, object] = {"backupenv_verify_subset_valid": _oracle}
    defaults.update(overrides)
    for name, fn in defaults.items():
        setattr(crate, name, fn)
    return crate


def _bomb_crate() -> types.ModuleType:
    def _boom(*_args: object) -> bool:
        raise AssertionError("the crate must not be called on this path")

    return _fake_crate(backupenv_verify_subset_valid=_boom)


def _arm(monkeypatch: pytest.MonkeyPatch, crate: types.ModuleType | None) -> None:
    monkeypatch.setenv(backup_env_rust.BACKUPENV_RUST_ENV, "1")
    monkeypatch.setitem(sys.modules, "brutex_core", crate)


# ── Flag + constants ────────────────────────────────────────────────────────


class TestFlagAndConstants:
    def test_env_name_pinned_and_default_off(self) -> None:
        assert backup_env_rust.BACKUPENV_RUST_ENV == "BRUTEX_CORE_RUST_BACKUPENV"
        assert backup_env_rust.backupenv_rust_enabled() is False
        assert backup_env_rust.backupenv_rust_disabled() is False

    def test_flag_delegates_to_the_shared_tristate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(backup_env_rust.BACKUPENV_RUST_ENV, "on")
        assert backup_env_rust.backupenv_rust_enabled() is True
        monkeypatch.setenv(backup_env_rust.BACKUPENV_RUST_ENV, "0")
        assert backup_env_rust.backupenv_rust_enabled() is False

    def test_reset_seam_restores_budget(self) -> None:
        backup_env_rust.reset_backupenv_rust_for_tests(remaining=0)
        assert (
            backup_env_rust._SEAM.burn_in_remaining("verify_subset") == 0  # pyright: ignore[reportPrivateUsage]
        )
        backup_env_rust.reset_backupenv_rust_for_tests()
        assert (
            backup_env_rust._SEAM.burn_in_remaining("verify_subset")  # pyright: ignore[reportPrivateUsage]
            == seam_core.BURN_IN_SAMPLES
        )


# ── OFF arm ─────────────────────────────────────────────────────────────────


class TestFlagOffNeverTouchesCrate:
    def test_off_arm_serves_oracle_and_never_resolves(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(sys.modules, "brutex_core", _bomb_crate())

        def _resolver_bomb() -> types.ModuleType | None:
            raise AssertionError("_resolve_crate must not run flag-OFF")

        monkeypatch.setattr(
            backup_env_rust._SEAM,  # pyright: ignore[reportPrivateUsage]
            "_resolve_crate",
            _resolver_bomb,
        )
        assert backup_env._valid_verify_subset("2%") is True  # pyright: ignore[reportPrivateUsage]
        assert backup_env._valid_verify_subset("1/300") is False  # pyright: ignore[reportPrivateUsage]
        assert backup_env_rust.backupenv_rust_disabled() is False

    def test_resolver_off_arm_uses_the_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The resolver-level consumer: an invalid shape falls back to
        # "auto" with the WARN — Python-side law, flag-OFF.
        monkeypatch.setitem(sys.modules, "brutex_core", _bomb_crate())
        config = backup_env.resolve_backup_env({"BRUTEX_BACKUP_VERIFY_SUBSET": "1/999"})
        assert config.verify_subset == "auto"
        good = backup_env.resolve_backup_env({"BRUTEX_BACKUP_VERIFY_SUBSET": "1/56"})
        assert good.verify_subset == "1/56"


# ── Eligibility ─────────────────────────────────────────────────────────────


class TestEligibility:
    def test_non_ascii_value_routes_to_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _arm(monkeypatch, _bomb_crate())
        assert backup_env._valid_verify_subset("٢%") is False  # pyright: ignore[reportPrivateUsage]
        assert backup_env_rust.backupenv_rust_disabled() is False

    def test_oversize_value_routes_to_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _arm(monkeypatch, _bomb_crate())
        long = "1" * 5000
        assert backup_env._valid_verify_subset(long) is False  # pyright: ignore[reportPrivateUsage]
        assert backup_env_rust.backupenv_rust_disabled() is False

    def test_eligibility_predicate_direct(self) -> None:
        eligible = backup_env_rust._eligible_value  # pyright: ignore[reportPrivateUsage]
        assert eligible("1/2") is True
        assert eligible(b"1/2") is False
        assert eligible("٢") is False
        assert eligible("x" * 4097) is False


# ── Refuse-poison ───────────────────────────────────────────────────────────


class TestRefusePoison:
    @pytest.mark.parametrize("poison", [1, 0, "true", None])
    def test_non_bool_result_refused_serves_oracle(
        self, monkeypatch: pytest.MonkeyPatch, poison: object
    ) -> None:
        # NOTE 1/0: the exact-type law refuses the int puns a truthiness
        # check would silently accept.
        _arm(monkeypatch, _fake_crate(backupenv_verify_subset_valid=lambda _v: poison))
        assert backup_env._valid_verify_subset("2%") is True  # pyright: ignore[reportPrivateUsage]
        assert backup_env_rust.backupenv_rust_disabled() is False

    def test_valid_bool_shapes(self) -> None:
        valid = backup_env_rust._valid_bool  # pyright: ignore[reportPrivateUsage]
        assert valid(True) is True
        assert valid(False) is True
        assert valid(1) is False
        assert valid("True") is False


# ── Burn-in + degrade + wiring ──────────────────────────────────────────────


class TestBurnInAndDegrade:
    def test_burn_in_mismatch_autoreverts_and_ships_oracle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _arm(monkeypatch, _fake_crate(backupenv_verify_subset_valid=lambda _v: False))
        assert backup_env._valid_verify_subset("2%") is True  # pyright: ignore[reportPrivateUsage]
        assert backup_env_rust.backupenv_rust_disabled() is True
        monkeypatch.setitem(sys.modules, "brutex_core", _bomb_crate())
        assert backup_env._valid_verify_subset("2%") is True  # pyright: ignore[reportPrivateUsage]

    def test_crate_error_degrades_to_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(_v: str) -> bool:
            raise ValueError("crate refusal")

        _arm(monkeypatch, _fake_crate(backupenv_verify_subset_valid=_boom))
        assert backup_env._valid_verify_subset("1/56") is True  # pyright: ignore[reportPrivateUsage]
        assert backup_env_rust.backupenv_rust_disabled() is False

    def test_routed_wiring_crosses_the_raw_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: list[str] = []

        def _spy(value: str) -> bool:
            seen.append(value)
            return _oracle(value)

        _arm(monkeypatch, _fake_crate(backupenv_verify_subset_valid=_spy))
        backup_env_rust.reset_backupenv_rust_for_tests(remaining=0)
        assert backup_env._valid_verify_subset("500m") is True  # pyright: ignore[reportPrivateUsage]
        assert seen == ["500m"]

    def test_resolver_flag_on_end_to_end(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _arm(monkeypatch, _fake_crate())
        backup_env_rust.reset_backupenv_rust_for_tests(remaining=0)
        config = backup_env.resolve_backup_env({"BRUTEX_BACKUP_VERIFY_SUBSET": "1/56"})
        assert config.verify_subset == "1/56"
        bad = backup_env.resolve_backup_env({"BRUTEX_BACKUP_VERIFY_SUBSET": "1/999"})
        assert bad.verify_subset == "auto"


# ── ON-arm parity with the REAL wheel (skips cleanly when absent) ───────────


class TestRealCrateParity:
    @pytest.fixture(autouse=True)
    def _require_wheel(self) -> None:
        pytest.importorskip(
            "brutex_core", reason="brutex-core wheel not built in this env (parity lane covers it)"
        )

    def test_corpus_identical_both_arms_with_engagement_spy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import brutex_core

        corpus = [
            "auto",
            "none",
            "",
            "1/56",
            "0001/0004",
            "1/256",
            "1/257",
            "257/257",
            "2%",
            ".5%",
            "1.%",
            "007.5%",
            "100%",
            "100.0000001%",
            "0%",
            "2e1%",
            "500m",
            "8388607t",
            "8388608t",
            "9223372036854775807b",
            "9223372036854775808b",
            "12",
            "1/2\n",
            "1/2\n\n",
            "1b\n",
            "0" * 30 + "1/2",
            "9" * 400 + "%",
        ]
        off = [backup_env._valid_verify_subset(v) for v in corpus]  # pyright: ignore[reportPrivateUsage]

        calls: list[str] = []
        real = brutex_core.backupenv_verify_subset_valid

        def _spy(value: str) -> bool:
            calls.append(value)
            return real(value)

        monkeypatch.setattr(brutex_core, "backupenv_verify_subset_valid", _spy)
        monkeypatch.setenv(backup_env_rust.BACKUPENV_RUST_ENV, "1")
        backup_env_rust.reset_backupenv_rust_for_tests(remaining=0)  # steady state
        on = [backup_env._valid_verify_subset(v) for v in corpus]  # pyright: ignore[reportPrivateUsage]
        assert on == off
        assert len(calls) == len(corpus)  # genuinely ENGAGED
        assert backup_env_rust.backupenv_rust_disabled() is False

    def test_real_crate_burn_in_completes_clean(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(backup_env_rust.BACKUPENV_RUST_ENV, "1")
        backup_env_rust.reset_backupenv_rust_for_tests()  # full budget live
        for _ in range(seam_core.BURN_IN_SAMPLES + 1):
            assert backup_env._valid_verify_subset("1/56") is True  # pyright: ignore[reportPrivateUsage]
        assert backup_env_rust.backupenv_rust_disabled() is False
        assert (
            backup_env_rust._SEAM.burn_in_remaining("verify_subset") == 0  # pyright: ignore[reportPrivateUsage]
        )

    def test_hypothesis_differential_subset_shapes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from hypothesis import HealthCheck, given, settings
        from hypothesis import strategies as st

        monkeypatch.setenv(backup_env_rust.BACKUPENV_RUST_ENV, "1")

        tokens = st.sampled_from(list("0123456789./%bkmgtBKMGT \n-e_"))
        values = st.builds("".join, st.lists(tokens, min_size=0, max_size=14))

        @settings(max_examples=400, deadline=None, suppress_health_check=[HealthCheck.too_slow])
        @given(value=values)
        def _differential(value: str) -> None:
            backup_env_rust.reset_backupenv_rust_for_tests(remaining=0)
            off = backup_env._valid_verify_subset_python(value)  # pyright: ignore[reportPrivateUsage]
            assert backup_env._valid_verify_subset(value) is off  # pyright: ignore[reportPrivateUsage]
            assert backup_env_rust.backupenv_rust_disabled() is False, (
                "a burn-in/latch trip inside the differential marks a real divergence"
            )

        _differential()


# ── Workflow wiring pins ────────────────────────────────────────────────────


class TestWorkflowWiring:
    _WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"

    def test_mutation_pair_wired(self) -> None:
        text = (self._WORKFLOWS / "mutation.yml").read_text(encoding="utf-8")
        pair = '"brutex/config/backup_env_rust.py:tests/unit/test_backup_env_rust.py:"'
        assert pair in text, (
            "backup_env_rust.py is not wired into mutation.yml's per-module mutmut loop"
        )

    def test_parity_lane_carries_the_backupenv_flag_on_leg(self) -> None:
        text = (self._WORKFLOWS / "core-rust-parity.yml").read_text(encoding="utf-8")
        assert "BRUTEX_CORE_RUST_BACKUPENV" in text, (
            "core-rust-parity.yml lost the backupenv flag-ON leg"
        )
        assert "tests/unit/test_backup_env_rust.py" in text, (
            "core-rust-parity.yml no longer runs the backupenv facade suite"
        )
