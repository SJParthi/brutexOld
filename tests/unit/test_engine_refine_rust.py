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
    reset_refine_rust_for_tests,
)
from brutex.models import BruteXError

if TYPE_CHECKING:
    from collections.abc import Iterator
    from types import ModuleType

pytest.importorskip("brutex_refine")  # the Stage-3 Rust wheel — built in the CI unit job


@pytest.fixture(autouse=True)
def _fresh_process_view() -> Iterator[None]:
    """Give every test a cold resolved-runtime memo.

    ``require_refine_rust`` memoizes the module on success
    (DEC-MONEY-RUST-WRAPPER-COST-001), so the wheel-absent tests below —
    which patch ``importlib.import_module`` WITHOUT swapping ``sys.modules``
    — must start from an unresolved state or they would be served a module
    resolved by an earlier test. Runs before AND after so this file also
    leaves a clean memo for the rest of the session.
    """
    reset_refine_rust_for_tests()
    yield
    reset_refine_rust_for_tests()


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


class TestRuntimeMemo:
    """The resolved-runtime memo: what it caches, what it must NOT, and the
    reset that keeps this file's ``import_module`` patches visible.

    Motivation (measured on the sibling ``money_rust``,
    DEC-MONEY-RUST-WRAPPER-COST-001): ``importlib.import_module`` costs
    ~574 ns even on a ``sys.modules`` HIT, and Stage-3 resolves the runtime
    per ask/tell hop.
    """

    def test_success_is_memoized_and_import_runs_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(REFINE_RUST_ENV, raising=False)
        wheel = importlib.import_module("brutex_refine")
        calls: list[str] = []

        def _counting(name: str, package: str | None = None) -> ModuleType:
            calls.append(name)
            return wheel

        reset_refine_rust_for_tests()
        monkeypatch.setattr(importlib, "import_module", _counting)
        for _ in range(5):
            assert require_refine_rust() is wheel
        assert calls == ["brutex_refine"], "the wheel must be imported exactly ONCE"
        assert refine_rust._CRATE_MEMO[0] is wheel

    def test_failed_import_is_never_memoized(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A FAILED import must NOT be cached. `require_refine_rust` RAISES on
        # failure, so a cached failure would turn "every call re-attempts and
        # re-refuses, loudly" into a silent latch — and a wheel installed
        # mid-process could never be picked up.
        monkeypatch.delenv(REFINE_RUST_ENV, raising=False)
        real_import = importlib.import_module

        def _no_wheel(name: str, package: str | None = None) -> ModuleType:
            if name == "brutex_refine":
                raise ImportError("No module named 'brutex_refine'")
            return real_import(name, package)

        reset_refine_rust_for_tests()
        monkeypatch.setattr(importlib, "import_module", _no_wheel)
        for _ in range(3):
            with pytest.raises(RefineRustUnavailableError):
                require_refine_rust()
        assert refine_rust._CRATE_MEMO[0] is None

    def test_reset_clears_the_memo_so_a_wheel_swap_is_seen(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # THE LOAD-BEARING PIN. The wheel-absent tests in this file patch
        # `importlib.import_module` WITHOUT resetting, so their isolation
        # rests entirely on the autouse `_fresh_process_view` fixture.
        monkeypatch.delenv(REFINE_RUST_ENV, raising=False)
        real = require_refine_rust()
        assert refine_rust._CRATE_MEMO[0] is not None
        real_import = importlib.import_module

        def _no_wheel(name: str, package: str | None = None) -> ModuleType:
            if name == "brutex_refine":
                raise ImportError("gone")
            return real_import(name, package)

        monkeypatch.setattr(importlib, "import_module", _no_wheel)
        assert require_refine_rust() is real, "warm memo still serves — no re-import"
        reset_refine_rust_for_tests()  # the autouse fixture's job
        with pytest.raises(RefineRustUnavailableError, match="not importable"):
            require_refine_rust()

    def test_memo_holds_the_module_not_a_bound_attribute(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # `RefineRuntime` is a Protocol read attribute-wise per call, so an
        # ATTRIBUTE monkeypatch on the memoized module must still be seen —
        # a memo holding a bound factory would serve the pre-patch object.
        monkeypatch.delenv(REFINE_RUST_ENV, raising=False)
        wheel = require_refine_rust()
        before = wheel.__version__
        monkeypatch.setattr(wheel, "__version__", "9.9.9-patched")
        assert require_refine_rust().__version__ == "9.9.9-patched"
        assert optimizer_versions()[0] == "brutex-refine-9.9.9-patched"
        monkeypatch.undo()
        assert require_refine_rust().__version__ == before

    def test_the_flag_verdict_is_never_memoized(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The memo read sits AFTER the flag verdict, so a falsy/junk value
        # set AFTER a successful resolve must still refuse loudly.
        monkeypatch.delenv(REFINE_RUST_ENV, raising=False)
        require_refine_rust()
        monkeypatch.setenv(REFINE_RUST_ENV, "0")
        with pytest.raises(RefineRustUnavailableError, match="NO fallback"):
            require_refine_rust()
        monkeypatch.setenv(REFINE_RUST_ENV, "junk")
        with pytest.raises(RefineRustUnavailableError, match="not a recognised"):
            require_refine_rust()
