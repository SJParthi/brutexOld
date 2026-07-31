"""Tests for :mod:`brutex.config.seam_core` — the shared config-seam core.

DEC-CONFIG-RUST-LEAVES-001 (MIGRATION.md #1943, slice T35 milestone 3).
Every fail-closed layer of :class:`ConfigRustSeam` is exercised
WHEEL-FREE (fake ``brutex_core`` modules injected via ``sys.modules``) on
throwaway seam instances, so the 100% line+branch gate never depends on a
built wheel; the per-facade suites drive the SAME machinery through the
real routed laws (and, in the parity lane, the real crate).
"""

from __future__ import annotations

import sys
import types

import pytest
from brutex.config import seam_core

_ENV = "BRUTEX_CORE_RUST_DEMO"


def _make_seam(**overrides: object) -> seam_core.ConfigRustSeam:
    """A throwaway two-law seam instance (no module-global state)."""
    kwargs: dict[str, object] = {
        "seam": "demo",
        "env_var": _ENV,
        "crate_api": ("demo_fn",),
        "laws": ("law_a", "law_b"),
    }
    kwargs.update(overrides)
    return seam_core.ConfigRustSeam(**kwargs)  # type: ignore[arg-type]


def _fake_crate(fn: object) -> types.ModuleType:
    """A fake ``brutex_core`` whose ``demo_fn`` is *fn*."""
    crate = types.ModuleType("brutex_core")
    crate.__version__ = "0.0-fake"  # type: ignore[attr-defined]
    crate.demo_fn = fn  # type: ignore[attr-defined]
    return crate


@pytest.fixture(autouse=True)
def _no_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_ENV, raising=False)


def _capture_logs(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[tuple[str, str]], list[tuple[str, str]], list[str]]:
    """Route seam_core WARN/ERROR/INFO into lists as (event, reason)."""
    warns: list[tuple[str, str]] = []
    errors: list[tuple[str, str]] = []
    infos: list[str] = []
    monkeypatch.setattr(
        seam_core._LOG,  # pyright: ignore[reportPrivateUsage]
        "warning",
        lambda event, **kw: warns.append((event, str(kw.get("reason")))),
    )
    monkeypatch.setattr(
        seam_core._LOG,  # pyright: ignore[reportPrivateUsage]
        "error",
        lambda event, **kw: errors.append((event, str(kw.get("reason")))),
    )
    monkeypatch.setattr(
        seam_core._LOG,  # pyright: ignore[reportPrivateUsage]
        "info",
        lambda event, **kw: infos.append(event),
    )
    return warns, errors, infos


class TestFlagTristate:
    @pytest.mark.parametrize("raw", ["1", "true", "YES", " on ", "On"])
    def test_truthy_literals_enable(self, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
        monkeypatch.setenv(_ENV, raw)
        assert _make_seam().enabled() is True

    @pytest.mark.parametrize("raw", ["0", "false", "NO", " off "])
    def test_falsy_literals_disable_silently(
        self, monkeypatch: pytest.MonkeyPatch, raw: str
    ) -> None:
        monkeypatch.setenv(_ENV, raw)
        seam = _make_seam()
        assert seam.enabled() is False
        assert not seam._memo  # pyright: ignore[reportPrivateUsage]

    def test_unset_and_whitespace_default_off_silently(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seam = _make_seam()
        assert seam.enabled() is False
        monkeypatch.setenv(_ENV, "   ")
        assert seam.enabled() is False
        assert not seam._memo  # pyright: ignore[reportPrivateUsage]

    def test_junk_fails_loud_off_and_warns_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        warns, _errors, _infos = _capture_logs(monkeypatch)
        monkeypatch.setenv(_ENV, "onn")
        seam = _make_seam()
        assert seam.enabled() is False
        assert seam.enabled() is False  # second call — memoised
        assert warns == [("demo_rust.fallback", "flag_unrecognised")]

    def test_burn_in_literal_pinned(self) -> None:
        assert seam_core.BURN_IN_SAMPLES == 16

    def test_truthy_falsy_mirror_money_rust(self) -> None:
        # The transitive mirror chain: seam_core ↔ money_rust ↔
        # sweep_lookahead (money_rust's own pin carries the second hop).
        from brutex import money_rust

        assert seam_core._TRUTHY == money_rust._TRUTHY  # pyright: ignore[reportPrivateUsage]
        assert seam_core._FALSY == money_rust._FALSY  # pyright: ignore[reportPrivateUsage]

    def test_leaf_import_discipline_ast_pin(self) -> None:
        # seam_core must stay a CONFIG leaf: stdlib + structlog ONLY at
        # MODULE scope (it sits under brutex.config.paths/dotenv, which
        # bootstrap the CLI before any wheel-bearing package may load).
        # Deferred (function-local) imports are exempt by construction —
        # they cannot affect what loading this module pulls in — and the
        # separate pin below enumerates the only one that exists.
        import ast
        from pathlib import Path

        source = Path(seam_core.__file__).read_text(encoding="utf-8")
        allowed = {"importlib", "os", "typing", "structlog", "__future__"}
        allowed_from = allowed | {"collections.abc", "types"}
        tree = ast.parse(source)
        module_scope: list[ast.stmt] = list(tree.body)
        for node in tree.body:  # e.g. `if TYPE_CHECKING:` blocks
            if isinstance(node, ast.If):
                module_scope.extend(node.body)
        for node in module_scope:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name.split(".")[0] in allowed, alias.name
            elif isinstance(node, ast.ImportFrom):
                assert node.module is not None
                assert node.module in allowed_from, node.module

    def test_deferred_imports_are_the_enumerated_set(self) -> None:
        # The ONE deferred brutex import (DEC-RUST-STRICT-NO-FALLBACK-001):
        # `brutex.config.rust_strict`, imported inside `_refuse_if_strict` so
        # the strict gate never becomes a module-scope edge. Enumerated here
        # so a future deferred import cannot slip in unreviewed.
        import ast
        from pathlib import Path

        source = Path(seam_core.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        module_scope_ids = {id(n) for n in tree.body}
        for node in tree.body:
            if isinstance(node, ast.If):
                module_scope_ids.update(id(n) for n in node.body)
        deferred: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and id(node) not in module_scope_ids:
                deferred.append(str(node.module))
            elif isinstance(node, ast.Import) and id(node) not in module_scope_ids:
                deferred.extend(alias.name for alias in node.names)
        assert deferred == ["brutex.config.rust_strict"]


class TestLatchAndReset:
    def test_disable_latches_once_and_repeat_is_noop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        warns, _errors, _infos = _capture_logs(monkeypatch)
        seam = _make_seam()
        seam._disable("first")  # pyright: ignore[reportPrivateUsage]
        seam._disable("second")  # pyright: ignore[reportPrivateUsage]
        assert seam.disabled() is True
        assert seam.disabled_reason == "first"
        assert warns == [("demo_rust.rust_path_disabled", "first")]

    def test_disable_error_logs_autoreverted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _warns, errors, _infos = _capture_logs(monkeypatch)
        seam = _make_seam()
        seam._disable("mismatch", error=True)  # pyright: ignore[reportPrivateUsage]
        assert errors == [("demo_rust.autoreverted", "mismatch")]

    def test_reset_restores_fresh_view_and_steady_state(self) -> None:
        seam = _make_seam()
        seam._disable("gone")  # pyright: ignore[reportPrivateUsage]
        seam.reset_for_tests()
        assert seam.disabled() is False
        assert seam.disabled_reason is None
        assert seam.burn_in_remaining("law_a") == seam_core.BURN_IN_SAMPLES
        assert seam.burn_in_remaining("law_b") == seam_core.BURN_IN_SAMPLES
        seam.reset_for_tests(remaining=0)
        assert seam.burn_in_remaining("law_a") == 0

    def test_warn_once_suppresses_repeats_per_reason(self, monkeypatch: pytest.MonkeyPatch) -> None:
        warns, _errors, _infos = _capture_logs(monkeypatch)
        seam = _make_seam()
        seam._warn_once("r1", extra=1)  # pyright: ignore[reportPrivateUsage]
        seam._warn_once("r1")  # pyright: ignore[reportPrivateUsage]
        seam._warn_once("r2")  # pyright: ignore[reportPrivateUsage]
        assert [reason for _event, reason in warns] == ["r1", "r2"]

    def test_seam_instances_are_independent(self) -> None:
        # Two facades' latches/budgets must never couple.
        a = _make_seam()
        b = _make_seam(seam="other")
        a._disable("a_only")  # pyright: ignore[reportPrivateUsage]
        assert b.disabled() is False


class TestResolveCrate:
    def test_absent_crate_latches_crate_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "brutex_core", None)  # → ImportError
        seam = _make_seam()
        assert seam._resolve_crate() is None  # pyright: ignore[reportPrivateUsage]
        assert seam.disabled_reason == "crate_unavailable"

    def test_module_missing_api_latches_crate_stale(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "brutex_core", types.ModuleType("brutex_core"))
        seam = _make_seam()
        assert seam._resolve_crate() is None  # pyright: ignore[reportPrivateUsage]
        assert seam.disabled_reason == "crate_stale"

    def test_non_callable_api_attr_latches_crate_stale(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(sys.modules, "brutex_core", _fake_crate("not-callable"))
        seam = _make_seam()
        assert seam._resolve_crate() is None  # pyright: ignore[reportPrivateUsage]
        assert seam.disabled_reason == "crate_stale"

    def test_full_api_resolves(self, monkeypatch: pytest.MonkeyPatch) -> None:
        crate = _fake_crate(lambda: "ok")
        monkeypatch.setitem(sys.modules, "brutex_core", crate)
        seam = _make_seam()
        assert seam._resolve_crate() is crate  # pyright: ignore[reportPrivateUsage]
        assert seam.disabled() is False


class TestPanicBelt:
    def test_belt_always_carries_exception_first(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "pyo3_runtime", None)  # → ImportError arm
        assert seam_core.panic_belt_errors() == (Exception,)

    def test_belt_folds_a_real_panic_class(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = types.ModuleType("pyo3_runtime")

        class PanicException(BaseException):
            pass

        fake.PanicException = PanicException  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "pyo3_runtime", fake)
        assert seam_core.panic_belt_errors() == (Exception, PanicException)

    def test_non_class_and_non_baseexception_attrs_are_skipped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = types.ModuleType("pyo3_runtime")
        fake.PanicException = "not-a-class"  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "pyo3_runtime", fake)
        assert seam_core.panic_belt_errors() == (Exception,)
        fake2 = types.ModuleType("pyo3_runtime")
        fake2.PanicException = dict  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "pyo3_runtime", fake2)
        assert seam_core.panic_belt_errors() == (Exception,)


def _adopt(
    seam: seam_core.ConfigRustSeam,
    *,
    law: str = "law_a",
    arguments: tuple[object, ...] = ("in",),
    validate: object = None,
    oracle: object = None,
) -> object:
    """Drive ``adopt`` with permissive defaults (str-shape validate)."""

    def _default_validate(value: object) -> bool:
        return isinstance(value, str)

    def _default_oracle() -> str:
        return "oracle"

    return seam.adopt(
        law,
        "demo_fn",
        arguments,
        validate if callable(validate) else _default_validate,  # type: ignore[arg-type]
        oracle if callable(oracle) else _default_oracle,  # type: ignore[arg-type]
    )


class TestAdopt:
    def test_absent_crate_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "brutex_core", None)
        assert _adopt(_make_seam()) is None

    @pytest.mark.parametrize(
        "exc",
        [
            ValueError("v"),
            RuntimeError("r"),
            TypeError("t"),
            OverflowError("o"),
            AttributeError("a"),
        ],
    )
    def test_degrade_errors_warn_once_and_serve_none(
        self, monkeypatch: pytest.MonkeyPatch, exc: Exception
    ) -> None:
        warns, _errors, _infos = _capture_logs(monkeypatch)

        def _boom(_s: str) -> str:
            raise exc

        monkeypatch.setitem(sys.modules, "brutex_core", _fake_crate(_boom))
        seam = _make_seam()
        assert _adopt(seam) is None
        assert _adopt(seam) is None
        assert [reason for _event, reason in warns] == ["law_a_error"]
        assert seam.disabled() is False  # degrade ≠ latch

    def test_degrade_error_detail_repr_on_plain_seam(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A non-redacting seam keeps the full repr detail (r2 m4 twin).
        warns: list[dict[str, object]] = []
        monkeypatch.setattr(
            seam_core._LOG,  # pyright: ignore[reportPrivateUsage]
            "warning",
            lambda event, **kw: warns.append({"event": event, **kw}),
        )

        def _boom(_s: str) -> str:
            raise ValueError("detail SECRET-ARG-abc")

        monkeypatch.setitem(sys.modules, "brutex_core", _fake_crate(_boom))
        assert _adopt(_make_seam()) is None
        assert warns[0]["error_detail"] == repr(ValueError("detail SECRET-ARG-abc"))
        assert "error_type" not in warns[0]

    def test_degrade_error_redacted_on_secret_seam(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # r2 m4: a redacting seam logs the exception TYPE only — an
        # exception repr can embed a crossed VALUE (e.g. a PyO3
        # conversion TypeError echoing its argument).
        warns: list[dict[str, object]] = []
        monkeypatch.setattr(
            seam_core._LOG,  # pyright: ignore[reportPrivateUsage]
            "warning",
            lambda event, **kw: warns.append({"event": event, **kw}),
        )

        def _boom(_s: str) -> str:
            raise TypeError("argument 'SECRET-VALUE-xyz': incompatible")

        monkeypatch.setitem(sys.modules, "brutex_core", _fake_crate(_boom))
        seam = _make_seam(redact_values=True)
        assert _adopt(seam) is None
        assert warns[0]["reason"] == "law_a_error"
        assert warns[0]["error_type"] == "TypeError"
        assert "error_detail" not in warns[0]
        assert all("SECRET-VALUE-xyz" not in str(v) for v in warns[0].values())
        assert seam.disabled() is False  # degrade ≠ latch

    @pytest.mark.parametrize("exc_type", [KeyboardInterrupt, SystemExit, MemoryError])
    def test_escape_set_propagates_bare(
        self, monkeypatch: pytest.MonkeyPatch, exc_type: type[BaseException]
    ) -> None:
        def _boom(_s: str) -> str:
            raise exc_type()

        monkeypatch.setitem(sys.modules, "brutex_core", _fake_crate(_boom))
        with pytest.raises(exc_type):
            _adopt(_make_seam())

    def test_out_of_degrade_set_exception_latches_crate_panic(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(_s: str) -> str:
            raise ZeroDivisionError("broken boundary")

        monkeypatch.setitem(sys.modules, "brutex_core", _fake_crate(_boom))
        seam = _make_seam()
        assert _adopt(seam) is None
        assert seam.disabled_reason == "crate_panic"

    def test_crate_panic_detail_repr_on_plain_seam(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A non-redacting seam's panic latch keeps the repr detail.
        warns: list[dict[str, object]] = []
        monkeypatch.setattr(
            seam_core._LOG,  # pyright: ignore[reportPrivateUsage]
            "warning",
            lambda event, **kw: warns.append({"event": event, **kw}),
        )

        def _boom(_s: str) -> str:
            raise ZeroDivisionError("broken boundary SECRET-ARG-abc")

        monkeypatch.setitem(sys.modules, "brutex_core", _fake_crate(_boom))
        seam = _make_seam()
        assert _adopt(seam) is None
        assert seam.disabled_reason == "crate_panic"
        assert warns[0]["error_detail"] == repr(ZeroDivisionError("broken boundary SECRET-ARG-abc"))
        assert "error_type" not in warns[0]

    def test_crate_panic_redacted_on_secret_seam(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # r2 m4: the panic latch on a redacting seam logs the exception
        # TYPE only — no crossed VALUE can reach a log record.
        warns: list[dict[str, object]] = []
        monkeypatch.setattr(
            seam_core._LOG,  # pyright: ignore[reportPrivateUsage]
            "warning",
            lambda event, **kw: warns.append({"event": event, **kw}),
        )

        def _boom(_s: str) -> str:
            raise ZeroDivisionError("broken boundary SECRET-VALUE-xyz")

        monkeypatch.setitem(sys.modules, "brutex_core", _fake_crate(_boom))
        seam = _make_seam(redact_values=True)
        assert _adopt(seam) is None
        assert seam.disabled_reason == "crate_panic"
        assert warns[0]["error_type"] == "ZeroDivisionError"
        assert "error_detail" not in warns[0]
        assert all("SECRET-VALUE-xyz" not in str(v) for v in warns[0].values())

    def test_panic_exception_class_latches_via_the_belt(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_pyo3 = types.ModuleType("pyo3_runtime")

        class PanicException(BaseException):
            pass

        fake_pyo3.PanicException = PanicException  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "pyo3_runtime", fake_pyo3)

        def _boom(_s: str) -> str:
            raise PanicException("panicked past catch_unwind")

        monkeypatch.setitem(sys.modules, "brutex_core", _fake_crate(_boom))
        seam = _make_seam()
        assert _adopt(seam) is None
        assert seam.disabled_reason == "crate_panic"

    def test_poison_refused_with_value_head(self, monkeypatch: pytest.MonkeyPatch) -> None:
        warns: list[dict[str, object]] = []
        monkeypatch.setattr(
            seam_core._LOG,  # pyright: ignore[reportPrivateUsage]
            "warning",
            lambda event, **kw: warns.append({"event": event, **kw}),
        )
        monkeypatch.setitem(sys.modules, "brutex_core", _fake_crate(lambda _s: 42))
        seam = _make_seam()
        assert _adopt(seam) is None
        assert warns[0]["reason"] == "law_a_result_refused"
        assert warns[0]["result_type"] == "int"
        assert "value_head" in warns[0]

    def test_poison_refused_redacted_on_secret_seam(self, monkeypatch: pytest.MonkeyPatch) -> None:
        warns: list[dict[str, object]] = []
        monkeypatch.setattr(
            seam_core._LOG,  # pyright: ignore[reportPrivateUsage]
            "warning",
            lambda event, **kw: warns.append({"event": event, **kw}),
        )
        monkeypatch.setitem(sys.modules, "brutex_core", _fake_crate(lambda _s: "SECRET-SHAPED"))
        seam = _make_seam(redact_values=True)
        assert _adopt(seam, validate=lambda _v: False) is None
        assert warns[0]["reason"] == "law_a_result_refused"
        # The redact law: NO value content in the log record.
        assert "value_head" not in warns[0]
        assert all("SECRET-SHAPED" not in str(v) for v in warns[0].values())

    def test_clean_burn_in_ships_value_decrements_and_completes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _warns, _errors, infos = _capture_logs(monkeypatch)
        monkeypatch.setitem(sys.modules, "brutex_core", _fake_crate(lambda _s: "oracle"))
        seam = _make_seam()
        seam.reset_for_tests(remaining=2)
        assert _adopt(seam) == "oracle"
        assert seam.burn_in_remaining("law_a") == 1
        assert seam.burn_in_remaining("law_b") == 2  # per-law — sibling untouched
        assert _adopt(seam) == "oracle"
        assert seam.burn_in_remaining("law_a") == 0
        assert "demo_rust.engaged" in infos
        assert "demo_rust.burn_in_complete" in infos
        # Steady state: the oracle is no longer consulted.
        assert (
            _adopt(seam, oracle=lambda: pytest.fail("oracle must not run post burn-in")) == "oracle"
        )

    def test_burn_in_mismatch_autoreverts_with_values(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        errors: list[dict[str, object]] = []
        monkeypatch.setattr(
            seam_core._LOG,  # pyright: ignore[reportPrivateUsage]
            "error",
            lambda event, **kw: errors.append({"event": event, **kw}),
        )
        monkeypatch.setitem(sys.modules, "brutex_core", _fake_crate(lambda _s: "wrong"))
        seam = _make_seam()
        assert _adopt(seam) is None
        assert seam.disabled_reason == "burn_in_mismatch_law_a"
        # The #907 repr convention (r2 n8): log fields carry repr()s.
        assert errors[0]["rust_value"] == repr("wrong")
        assert errors[0]["python_value"] == repr("oracle")

    def test_burn_in_mismatch_redacted_on_secret_seam(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        errors: list[dict[str, object]] = []
        monkeypatch.setattr(
            seam_core._LOG,  # pyright: ignore[reportPrivateUsage]
            "error",
            lambda event, **kw: errors.append({"event": event, **kw}),
        )
        monkeypatch.setitem(sys.modules, "brutex_core", _fake_crate(lambda _s: "wrong"))
        seam = _make_seam(redact_values=True)
        assert _adopt(seam) is None
        assert seam.disabled_reason == "burn_in_mismatch_law_a"
        record = errors[0]
        assert "rust_value" not in record
        assert "python_value" not in record
        assert all("wrong" not in str(v) and "oracle" not in str(v) for v in record.values())

    def test_engaged_info_logged_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _warns, _errors, infos = _capture_logs(monkeypatch)
        monkeypatch.setitem(sys.modules, "brutex_core", _fake_crate(lambda _s: "oracle"))
        seam = _make_seam()
        seam.reset_for_tests(remaining=0)
        assert _adopt(seam) == "oracle"
        assert _adopt(seam) == "oracle"
        assert infos.count("demo_rust.engaged") == 1
