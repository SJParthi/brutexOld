"""The strict-Rust gate's own tests — DEC-RUST-STRICT-NO-FALLBACK-001.

Two jobs, deliberately in one file because they are two halves of one claim:

1. **The gate behaves** — :func:`strict_mode` resolves the inverted junk
   polarity correctly, :func:`refuse` renders a message that names every fact
   an operator needs, and :func:`refuse_if_strict` is a no-op only when the
   kill switch is explicitly thrown.

2. **The gate reaches everywhere** — the mechanized posture pin at the bottom.
   The gate is worthless if a facade can degrade to Python without passing
   through it, and prose in an ADR cannot notice a new facade that forgets to
   wire itself. :class:`TestEveryFacadeIsStrictCovered` walks the live census
   and fails the build if any ENGAGED facade has an unrefused degrade path.
"""

from __future__ import annotations

import ast
import importlib
from typing import TYPE_CHECKING

import pytest
from brutex.config.rust_strict import (
    RUST_STRICT_DEFAULT,
    RUST_STRICT_ENV,
    RustFallbackRefused,
    refuse,
    refuse_if_strict,
    strict_mode,
)
from brutex.models import BruteXError
from brutex.observability.rust_census import (
    REQUIRED,
    _calls_strict_refusal,  # pyright: ignore[reportPrivateUsage]
    _source_of,  # pyright: ignore[reportPrivateUsage]
    census,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from types import ModuleType


class TestStrictModeResolution:
    """:func:`strict_mode` — the inverted junk polarity, case by case."""

    def test_unset_is_the_shipped_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(RUST_STRICT_ENV, raising=False)
        assert strict_mode() is RUST_STRICT_DEFAULT
        assert RUST_STRICT_DEFAULT is True, "strict mode must ship ON"

    @pytest.mark.parametrize("raw", ["0", "false", "FALSE", "no", "off"])
    def test_explicit_falsy_is_the_only_kill_switch(
        self, monkeypatch: pytest.MonkeyPatch, raw: str
    ) -> None:
        monkeypatch.setenv(RUST_STRICT_ENV, raw)
        assert strict_mode() is False

    @pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "on"])
    def test_explicit_truthy_is_on(self, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
        monkeypatch.setenv(RUST_STRICT_ENV, raw)
        assert strict_mode() is True

    @pytest.mark.parametrize("raw", ["", "   ", "\t\n"])
    def test_blank_is_treated_as_unset(self, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
        monkeypatch.setenv(RUST_STRICT_ENV, raw)
        assert strict_mode() is RUST_STRICT_DEFAULT

    @pytest.mark.parametrize("raw", ["garbage", "2", "ON!", "-1", "None"])
    def test_junk_keeps_strict_on(self, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
        """The deliberate inversion of the house fail-safe-OFF junk law.

        Elsewhere a typo must never arm an experimental kernel. Here the
        dangerous outcome points the other way — a typo'd kill switch must not
        silently hand the work back to Python — so junk stays ON.
        """
        monkeypatch.setenv(RUST_STRICT_ENV, raw)
        assert strict_mode() is True

    def test_the_verdict_is_not_memoized_across_calls(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A cached verdict would leak the first test's env into every later one."""
        monkeypatch.setenv(RUST_STRICT_ENV, "0")
        assert strict_mode() is False
        monkeypatch.setenv(RUST_STRICT_ENV, "1")
        assert strict_mode() is True


class TestRefuseMessage:
    """:func:`refuse` — every fact the operator needs, none invented."""

    def test_names_facade_reason_and_the_kill_switch(self) -> None:
        with pytest.raises(RustFallbackRefused) as excinfo:
            refuse("brutex.some_rust", "wheel_missing")
        text = str(excinfo.value)
        assert "brutex.some_rust" in text
        assert "reason: wheel_missing" in text
        assert RUST_STRICT_ENV in text
        assert "DEC-RUST-STRICT-NO-FALLBACK-001" in text
        assert f"{RUST_STRICT_ENV}=0" in text, "the message must name the escape"

    def test_context_free_refusal_carries_no_empty_parens(self) -> None:
        with pytest.raises(RustFallbackRefused) as excinfo:
            refuse("f", "r")
        assert "()" not in str(excinfo.value)

    def test_context_is_sorted_and_repred(self) -> None:
        """Sorted so the message is stable; ``repr`` so a newline cannot forge a line."""
        with pytest.raises(RustFallbackRefused) as excinfo:
            refuse("f", "r", zeta=1, alpha="a\nb")
        text = str(excinfo.value)
        assert "(alpha='a\\nb', zeta=1)" in text
        assert text.index("alpha=") < text.index("zeta="), "context must be sorted"

    def test_the_refusal_is_a_brutex_error(self) -> None:
        """So every existing ``except BruteXError`` boundary sees it as ours."""
        assert issubclass(RustFallbackRefused, BruteXError)

    def test_a_huge_context_value_cannot_mint_an_unbounded_message(self) -> None:
        """The docstring says "bounded context" — this is what makes that true.

        ``context`` is caller-supplied and a facade may hand over a whole frame
        or a kernel result. Without the cap a single refusal could mint a
        megabyte-long exception string into a log line. Bounding lives at this
        ONE choke point, so the pin here covers all 38 wired facades.
        """
        from brutex.config.rust_strict import _CONTEXT_REPR_CAP

        with pytest.raises(RustFallbackRefused) as excinfo:
            refuse("f", "r", blob="x" * 100_000)
        text = str(excinfo.value)
        # The value is capped; the surrounding message is not truncated, so the
        # operator still gets the facade, the reason and the kill switch.
        assert "x" * _CONTEXT_REPR_CAP not in text, "the cap must actually bite"
        assert "x" * (_CONTEXT_REPR_CAP - 1) in text, "…but must not over-trim"
        assert f"{RUST_STRICT_ENV}=0" in text

    def test_a_short_context_value_is_byte_identical_to_the_pre_cap_form(self) -> None:
        """The cap is a ceiling, not a reformat: normal refusals are unchanged."""
        with pytest.raises(RustFallbackRefused) as excinfo:
            refuse("f", "r", error=ValueError("boom"))
        assert "(error=ValueError('boom'))" in str(excinfo.value)


class TestRefuseIfStrict:
    """:func:`refuse_if_strict` — the one-line insert at each choke point."""

    def test_raises_when_strict(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(RUST_STRICT_ENV, raising=False)
        with pytest.raises(RustFallbackRefused, match="burn_in_mismatch"):
            refuse_if_strict("brutex.some_rust", "burn_in_mismatch", n=3)

    def test_returns_none_when_the_kill_switch_is_thrown(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(RUST_STRICT_ENV, "0")
        assert refuse_if_strict("brutex.some_rust", "wheel_missing") is None


class TestPublicSurface:
    def test_all_is_exactly_the_documented_surface(self) -> None:
        from brutex.config import rust_strict

        assert sorted(rust_strict.__all__) == [
            "RUST_STRICT_DEFAULT",
            "RUST_STRICT_ENV",
            "RustFallbackRefused",
            "refuse",
            "refuse_if_strict",
            "strict_mode",
        ]
        for name in rust_strict.__all__:
            assert hasattr(rust_strict, name), f"__all__ names a missing symbol: {name}"


# ──────────────────────────────────────────────────────────────────────
# The posture pin — the gate is only a guarantee if it reaches every facade
# ──────────────────────────────────────────────────────────────────────
#
# A facade that degrades to Python without passing through ``refuse_if_strict``
# is a silent hole in DEC-RUST-STRICT-NO-FALLBACK-001, and nothing else in the
# tree would notice it: the census would still report the facade ENGAGED
# (Rust is configured and its crate imports), and the operator reading that row
# would reasonably conclude Python cannot serve. So the coverage is mechanized
# here rather than asserted in an ADR.
#
# A facade is strict-covered when ANY of three things is true:
#
#   1. it calls the gate itself;
#   2. it is wheel-REQUIRED — no Python oracle exists to fall back TO, so the
#      absent crate is already a typed loud raise and there is nothing for the
#      gate to intercept;
#   3. it imports one of the shared degrade cores below, which owns its
#      fall-back choke point and calls the gate there.
#
# Arm 3 is an allowlist, and an allowlist can rot into a bypass — so the test
# re-derives each entry's own enrolment from source rather than trusting the
# name. An allowlisted core that stops calling the gate fails this file.

#: Shared modules that own a facade's degrade choke point. Each is itself a
#: census facade and is verified strict-enrolled below, so delegating to one is
#: genuine coverage rather than an exemption.
_SHARED_DEGRADE_CORES: frozenset[str] = frozenset(
    {
        # The T35 config seams (paths / dotenv / backup-env) are thin wrappers;
        # ``ConfigRustSeam`` runs the engaged call and owns the degrade.
        "brutex.config.seam_core",
        # ``rust_ranking`` and the metrics families route every kernel call
        # through the shared burn-in / autorevert / degrade core.
        "brutex.engine.rust_parity_core",
    }
)


def _imported_module_names(module: ModuleType) -> frozenset[str]:
    """Every module name *module* imports, absolute names only.

    Reads the parse tree rather than ``__dict__`` so a lazily-imported core
    (the house pattern for breaking import cycles — the cores here are imported
    *inside* the degrade function) is still seen.
    """
    try:
        tree = ast.parse(_source_of(module))
    except SyntaxError:  # pragma: no cover - defensive; every facade parses
        return frozenset()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None and node.level == 0:
            names.add(node.module)
    return frozenset(names)


class TestEveryFacadeIsStrictCovered:
    """The live posture pin over the real census — no fixtures, no fakes."""

    def test_every_allowlisted_shared_core_is_itself_enrolled(self) -> None:
        """The allowlist cannot become a bypass: each core must call the gate."""
        for name in sorted(_SHARED_DEGRADE_CORES):
            module = importlib.import_module(name)
            assert _calls_strict_refusal(_source_of(module)), (
                f"{name} is allowlisted as a shared degrade core but does not "
                "call refuse/refuse_if_strict — delegating to it would be an "
                "exemption, not coverage"
            )

    def test_no_facade_can_degrade_to_python_unrefused(self) -> None:
        rows = census()
        assert rows, "precondition: the census found facades"

        uncovered: list[str] = []
        for row in rows:
            if row.verdict == REQUIRED:
                continue  # no oracle exists to fall back to
            module = importlib.import_module(row.module)
            source = _source_of(module)
            if _calls_strict_refusal(source):
                continue
            if _imported_module_names(module) & _SHARED_DEGRADE_CORES:
                continue
            uncovered.append(f"{row.module} ({row.verdict}: {row.detail})")

        assert not uncovered, (
            "these facades can fall back to Python without passing the strict "
            "gate — wire refuse_if_strict at their degrade choke point (or "
            "route them through a shared core):\n  " + "\n  ".join(sorted(uncovered))
        )

    def test_the_pin_would_notice_a_new_unwired_facade(self) -> None:
        """Anti-vacuity: the check above must actually reject something.

        Without this, a bug that made every module look enrolled would leave the
        pin permanently green and permanently useless.
        """
        from brutex.observability import rust_census

        unwired = ast.parse("def work():\n    return 1\n")
        assert not _calls_strict_refusal(ast.unparse(unwired))
        assert not (_imported_module_names(rust_census) & _SHARED_DEGRADE_CORES), (
            "precondition: the census module itself is not a shared degrade core"
        )
