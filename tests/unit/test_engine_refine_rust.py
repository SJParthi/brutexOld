"""Unit tests for the Stage-3 Rust runtime resolver (`brutex.engine.refine_rust`).

DEC-RUST-STAGE3-REFINE-001 — 100% line+branch of the ONE wheel seam: the
DEFAULT-ON flag law (unset/blank/truthy engage; falsy REFUSES loudly — the
pycma oracle is deleted, there is no fallback; junk REFUSES loudly — the
fail-loud env law), the wheel-absent typed error carrying the build hint at
CALL time (never at import), and the ``optimizer_versions`` identity tokens
(crate version tag + baked source digest).
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

import pytest
from brutex.engine import refine_rust
from brutex.engine.refine_rust import (
    BUILD_HINT,
    REFINE_RUST_ENV,
    RefineRustUnavailableError,
    optimizer_versions,
    require_refine_rust,
)
from brutex.models import BruteXError

if TYPE_CHECKING:
    from types import ModuleType

pytest.importorskip("brutex_refine")  # the Stage-3 Rust wheel — built in the CI unit job


class TestFlagLaw:
    """Default-ON; falsy/junk refuse loudly (no fallback exists)."""

    def test_unset_engages(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(REFINE_RUST_ENV, raising=False)
        runtime = require_refine_rust()
        assert hasattr(runtime, "RefineSession")

    @pytest.mark.parametrize("raw", ["", "   "])
    def test_blank_engages(self, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
        monkeypatch.setenv(REFINE_RUST_ENV, raw)
        assert hasattr(require_refine_rust(), "SelectSession")

    @pytest.mark.parametrize("raw", ["1", "true", "YES", " on "])
    def test_truthy_engages(self, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
        monkeypatch.setenv(REFINE_RUST_ENV, raw)
        assert hasattr(require_refine_rust(), "grid_denormalize")

    @pytest.mark.parametrize("raw", ["0", "false", "NO", " off "])
    def test_falsy_refuses_loudly_no_fallback(
        self, monkeypatch: pytest.MonkeyPatch, raw: str
    ) -> None:
        monkeypatch.setenv(REFINE_RUST_ENV, raw)
        with pytest.raises(RefineRustUnavailableError, match="NO fallback"):
            require_refine_rust()

    @pytest.mark.parametrize("raw", ["2", "enabled", "junk"])
    def test_junk_refuses_loudly(self, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
        monkeypatch.setenv(REFINE_RUST_ENV, raw)
        with pytest.raises(RefineRustUnavailableError, match="not a recognised"):
            require_refine_rust()

    def test_error_is_a_brutex_error(self) -> None:
        # The typed refusal rides the house exception root, so callers'
        # BruteXError handling (CLI exit mapping) sees it.
        assert issubclass(RefineRustUnavailableError, BruteXError)


class TestWheelAbsent:
    """A missing wheel is a loud typed error AT CALL TIME with the build hint."""

    def test_import_failure_maps_to_typed_error_with_hint(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(REFINE_RUST_ENV, raising=False)
        real_import = importlib.import_module

        def _no_wheel(name: str, package: str | None = None) -> ModuleType:
            if name == "brutex_refine":
                raise ImportError("No module named 'brutex_refine'")
            return real_import(name, package)

        monkeypatch.setattr(importlib, "import_module", _no_wheel)
        with pytest.raises(RefineRustUnavailableError, match="maturin develop") as exc:
            require_refine_rust()
        assert "DEC-RUST-STAGE3-REFINE-001" in str(exc.value)
        assert BUILD_HINT in str(exc.value)

    def test_stage3_modules_import_wheel_free(self) -> None:
        # The dashboard-boots-wheel-free law: importing the resolver AND all
        # three stage3 thin wrappers (incl. constructing the module-level
        # Mode-A axes — validation is pure Python) must never touch the
        # wheel. Proven in a fresh subprocess with `brutex_refine` POISONED
        # (sys.modules[name] = None makes any import attempt raise), so a
        # regression that moves a wheel call to import time fails loudly.
        import subprocess
        import sys

        code = (
            "import sys\n"
            "sys.modules['brutex_refine'] = None\n"
            "import brutex.engine.refine_rust\n"
            "from brutex.engine.stage3_grid import MODE_A_SL_AXIS, MODE_A_TP_AXIS\n"
            "import brutex.engine.stage3_select\n"
            "import brutex.engine.stage3_refine\n"
            "assert MODE_A_SL_AXIS.steps == 40 and MODE_A_TP_AXIS.steps == 50\n"
            "print('wheel-free-import-ok')\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        assert proc.returncode == 0, proc.stderr
        assert "wheel-free-import-ok" in proc.stdout


class TestOptimizerVersions:
    """The identity tokens: crate version tag + baked source digest."""

    def test_tokens_shape(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(REFINE_RUST_ENV, raising=False)
        import brutex_refine

        version, build = optimizer_versions()
        assert version == f"brutex-refine-{brutex_refine.__version__}"
        assert build == brutex_refine.SOURCE_DIGEST
        # The digest is the 64-hex BLAKE3 of the crate source (FRAMING v1).
        assert len(build) == 64
        assert set(build) <= set("0123456789abcdef")

    def test_refusal_propagates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(REFINE_RUST_ENV, "0")
        with pytest.raises(RefineRustUnavailableError):
            optimizer_versions()


class TestFlagVocabularyDriftPin:
    """The inline truthy/falsy sets mirror the house env-flag vocabulary.

    Inlined per the feed_rust precedent (this leaf imports nothing heavy);
    the pin keeps the spellings equal to `sweep_lookahead`'s so the two flag
    families can never drift apart silently.
    """

    def test_sets_match_sweep_lookahead(self) -> None:
        from brutex.engine import sweep_lookahead

        assert refine_rust._TRUTHY == sweep_lookahead._TRUTHY
        assert refine_rust._FALSY == sweep_lookahead._FALSY
