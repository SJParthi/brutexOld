"""Unit suite for :mod:`brutex.engine.run_layout` (Ruling 4 / Row 15 leaf).

The single-source run-directory layout + the internal-disk forensics-mirror
resolver (DEC-SWEEP-UNKILLABLE-CORE-001). 100% line+branch standalone.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from brutex.engine.run_layout import (
    MIRROR_ROOT_ENV,
    RUN_DIR_SUBDIR,
    mirror_run_dir,
    run_dir,
)

_RUN_ID: Final[str] = "deadbeef" * 8


def test_run_dir_is_subdir_of_results_root() -> None:
    """The run dir is ``<results_root>/<RUN_DIR_SUBDIR>/<run_id>`` — the ONE source."""
    assert run_dir(Path("/lake/sweeps"), _RUN_ID) == Path("/lake/sweeps") / RUN_DIR_SUBDIR / _RUN_ID


def test_mirror_default_is_home_brutex_runs() -> None:
    """With no override the mirror resolves under ``~/.brutex/runs/<run_id>``."""
    dest = mirror_run_dir(_RUN_ID, env={})
    assert dest == Path.home() / ".brutex" / "runs" / _RUN_ID


def test_mirror_env_name_literal_pinned() -> None:
    """Drift pin: the suite-wide conftest isolation fixture sets the LITERAL
    ``"BRUTEX_RUN_MIRROR_ROOT"`` (importing brutex.engine there would abort the
    wheel-less redteam-nightly collector — run 29145422566). If this constant
    ever renames, the conftest literal must move with it."""
    assert MIRROR_ROOT_ENV == "BRUTEX_RUN_MIRROR_ROOT"


def test_mirror_override_root_used() -> None:
    """A non-empty ``BRUTEX_RUN_MIRROR_ROOT`` names the mirror root directly."""
    dest = mirror_run_dir(_RUN_ID, env={MIRROR_ROOT_ENV: "/mnt/internal"})
    assert dest == Path("/mnt/internal") / _RUN_ID


def test_mirror_blank_override_disables() -> None:
    """An empty/whitespace override DISABLES mirroring (returns ``None``)."""
    assert mirror_run_dir(_RUN_ID, env={MIRROR_ROOT_ENV: "   "}) is None


def test_mirror_none_when_home_unresolvable(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A home-less box (``Path.home`` raises) yields ``None`` — never a crash."""

    def _boom() -> Path:
        raise RuntimeError("no home dir")

    monkeypatch.setattr(Path, "home", staticmethod(_boom))
    assert mirror_run_dir(_RUN_ID, env={}) is None


def test_mirror_reads_os_environ_by_default(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """With ``env=None`` the resolver reads ``os.environ`` (the production path)."""
    monkeypatch.setenv(MIRROR_ROOT_ENV, "/mnt/env")
    assert mirror_run_dir(_RUN_ID) == Path("/mnt/env") / _RUN_ID


# ────────── DEC-RUST-SWEEP-OPS-001 — slice-1 routing (wheel-free) ──────────


class TestSweepOpsRouting:
    """Flag-ON routing parity for the three run-layout join sites.

    Wheel-free: a behavior-correct FAKE crate is injected by patching the
    facade's ``importlib`` binding (deterministic whether or not the real
    ``brutex_sweep_ops`` wheel is installed). Flag ON must be value-identical
    to the flag-OFF Python law (the routed law's burn-in enforces it too).
    """

    @staticmethod
    def _arm_fake_crate(monkeypatch) -> None:  # type: ignore[no-untyped-def]
        from types import SimpleNamespace

        from brutex.engine import sweep_ops_rust

        def join_path_segments(base: str, segments: list[str]) -> str:
            acc = base
            for segment in segments:
                if acc and not acc.endswith("/"):
                    acc += "/"
                acc += segment
            return acc

        crate = SimpleNamespace(
            __version__="fake",
            join_path_segments=join_path_segments,
            gate_config_fragment=lambda *args: (None, None),
            stall_scaled_timeout=lambda *args: (300.0, False),
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

    def test_run_dir_flag_on_matches_python_law(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        self._arm_fake_crate(monkeypatch)
        try:
            assert run_dir(Path("/lake/sweeps"), _RUN_ID) == (
                Path("/lake/sweeps") / RUN_DIR_SUBDIR / _RUN_ID
            )
        finally:
            self._reset_facade()

    def test_mirror_override_flag_on_matches_python_law(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        self._arm_fake_crate(monkeypatch)
        try:
            assert mirror_run_dir(_RUN_ID, env={MIRROR_ROOT_ENV: "/mnt/mirror"}) == (
                Path("/mnt/mirror") / _RUN_ID
            )
        finally:
            self._reset_facade()

    def test_mirror_home_flag_on_matches_python_law(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        self._arm_fake_crate(monkeypatch)
        try:
            assert mirror_run_dir(_RUN_ID, env={}) == (Path.home() / ".brutex" / "runs" / _RUN_ID)
        finally:
            self._reset_facade()
