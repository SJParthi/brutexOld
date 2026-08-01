"""Tests for :mod:`brutex.money_rust` — the default-OFF money Rust seam.

DEC-CONFIG-RUST-LEAVES-001 (MIGRATION.md #1943, slice T35 milestone 1).
Every fail-closed layer is exercised WHEEL-FREE (fake crate modules
injected via ``sys.modules``), so the 100% line+branch gate on the facade
never depends on a built ``brutex_core``; the ON-arm parity legs
(:class:`TestRealCrateParity`) skip cleanly when the wheel is absent and
run for real in the ``core-rust-parity.yml`` lane.
"""

from __future__ import annotations

import sys
import types
from collections.abc import Iterator
from decimal import Context, Decimal, Inexact, Rounded, localcontext
from typing import TYPE_CHECKING

import pytest
from brutex import money, money_rust
from brutex.config.rust_strict import RUST_STRICT_ENV, RustFallbackRefused

if TYPE_CHECKING:
    from collections.abc import Callable

# ── Harness ─────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _fresh_seam(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Every test starts flag-unset with a fresh latch/burn-in/memo."""
    monkeypatch.delenv(money_rust.MONEY_RUST_ENV, raising=False)
    money_rust.reset_money_rust_for_tests()
    yield
    money_rust.reset_money_rust_for_tests()


@pytest.fixture
def _strict_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable strict mode for tests that exercise the DEGRADE contract.

    DEC-RUST-STRICT-NO-FALLBACK-001 ships ``BRUTEX_RUST_STRICT`` ON by
    default, so every fall-back this facade can announce (junk flag, absent
    / stale / panicking crate, a degrade-set raise, a poisoned crate return,
    a burn-in mismatch) now RAISES instead of degrading. Warn-and-degrade
    remains a fully supported, kill-switched mode — these tests pin THAT
    mode, so they opt into it explicitly rather than having their assertions
    weakened. Tests of the CLASS-C eligibility gate need no such opt-in: an
    ineligible input never reaches a choke point, so it degrades under
    strict mode too (:class:`TestStrictMode` pins exactly that).
    """
    monkeypatch.setenv(RUST_STRICT_ENV, "0")


def _fake_crate(**overrides: object) -> types.ModuleType:
    """A fake ``brutex_core`` carrying the full API (identity-echo defaults)."""
    crate = types.ModuleType("brutex_core")
    crate.__version__ = "0.0-fake"  # type: ignore[attr-defined]
    defaults: dict[str, Callable[[str], str]] = {
        "round_money_str": lambda s: str(money._round_money_python(Decimal(s))),  # pyright: ignore[reportPrivateUsage]
        "rupees_to_paisa_str": lambda s: str(money._rupees_to_paisa_python(Decimal(s))),  # pyright: ignore[reportPrivateUsage]
        "paisa_to_rupees_str": lambda s: str(money._paisa_to_rupees_python(int(s))),  # pyright: ignore[reportPrivateUsage]
        "format_inr_str": lambda s: money._format_inr_python(int(s)),  # pyright: ignore[reportPrivateUsage]
    }
    for name, fn in defaults.items():
        setattr(crate, name, overrides.get(name, fn))
    for name, value in overrides.items():
        setattr(crate, name, value)
    return crate


def _context_free_crate() -> types.ModuleType:
    """A law-faithful fake modeling the REAL crate's context-INDEPENDENT
    string arithmetic.

    The identity-echo defaults of :func:`_fake_crate` compute through the
    Python oracles, which SHARE the ambient decimal context — useless for
    the r2 narrowed-context repros, where the whole point is that the
    Rust crate does NOT (its digit arithmetic ignores ``getcontext()``
    entirely). Each law here runs its oracle under a fresh DEFAULT
    ``Context()``, so it returns a VALUE exactly where the real crate
    would, even while the caller's ambient context would make the oracle
    raise.
    """

    def _free(fn: Callable[[str], str]) -> Callable[[str], str]:
        def inner(s: str) -> str:
            with localcontext(Context()):
                return fn(s)

        return inner

    base = _fake_crate()
    return _fake_crate(
        **{name: _free(getattr(base, name)) for name in money_rust._CRATE_API}  # pyright: ignore[reportPrivateUsage]
    )


def _bomb_crate() -> types.ModuleType:
    """A crate whose every call is a test failure (the OFF-arm spy)."""

    def _boom(_s: str) -> str:
        raise AssertionError("the crate must not be called on this path")

    return _fake_crate(
        round_money_str=_boom,
        rupees_to_paisa_str=_boom,
        paisa_to_rupees_str=_boom,
        format_inr_str=_boom,
    )


def _arm(monkeypatch: pytest.MonkeyPatch, crate: types.ModuleType | None) -> None:
    """Set the flag ON and install *crate* as ``brutex_core``."""
    monkeypatch.setenv(money_rust.MONEY_RUST_ENV, "1")
    monkeypatch.setitem(sys.modules, "brutex_core", crate)


# ── Flag matrix ─────────────────────────────────────────────────────────────


class TestFlagParsing:
    @pytest.mark.parametrize("raw", ["1", "true", "YES", " on ", "On"])
    def test_truthy_literals_enable(self, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
        monkeypatch.setenv(money_rust.MONEY_RUST_ENV, raw)
        assert money_rust.money_rust_enabled() is True

    @pytest.mark.parametrize("raw", ["0", "false", "NO", " off "])
    def test_falsy_literals_disable_silently(
        self, monkeypatch: pytest.MonkeyPatch, raw: str
    ) -> None:
        monkeypatch.setenv(money_rust.MONEY_RUST_ENV, raw)
        assert money_rust.money_rust_enabled() is False

    def test_unset_and_whitespace_default_on_silently(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The DEC-RUST-DEFAULT-ON-WAVE-001 default pin: unset/blank ⇒ ON.
        assert money_rust.money_rust_enabled() is True
        monkeypatch.setenv(money_rust.MONEY_RUST_ENV, "   ")
        assert money_rust.money_rust_enabled() is True

    @pytest.mark.usefixtures("_strict_off")
    def test_junk_fails_loud_off_and_warns_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(money_rust.MONEY_RUST_ENV, "onn")
        events: list[str] = []
        monkeypatch.setattr(
            money_rust._LOG,  # pyright: ignore[reportPrivateUsage]
            "warning",
            lambda event, **kw: events.append(f"{event}:{kw.get('reason')}"),
        )
        assert money_rust.money_rust_enabled() is False
        assert money_rust.money_rust_enabled() is False  # second call — memoised
        assert events == ["money_rust.fallback:flag_unrecognised"]

    def test_env_name_and_burn_in_literals_pinned(self) -> None:
        # The canonical BRUTEX_<CRATE>_RUST_<SEAM> form + the #1531 budget.
        assert money_rust.MONEY_RUST_ENV == "BRUTEX_CORE_RUST_MONEY"
        assert money_rust.BURN_IN_SAMPLES == 16

    def test_leaf_import_discipline_ast_pin(self) -> None:
        # money_rust must stay a ROOT-leaf module: stdlib + structlog ONLY
        # at MODULE scope (brutex.data / brutex.engine would cycle or drag
        # the fno_math wheel under every brutex.money importer). AST pin —
        # the feed_rust precedent.
        #
        # The pin now distinguishes module-scope from function-scope imports
        # instead of walking the whole tree: the DEC-RUST-STRICT-NO-FALLBACK-001
        # gate MUST be imported inside `_refuse_if_strict`, because
        # `brutex.config.rust_strict` -> `brutex.config` -> `brutex.data` ->
        # `brutex.money` is an executed-and-confirmed cycle (see the
        # `_refuse_if_strict` docstring for the reproduced ImportError). The
        # module-scope surface is asserted UNCHANGED (still stdlib + structlog),
        # and the deferred surface is pinned to EXACTLY that one gate import so
        # this cannot become a general escape hatch.
        import ast
        from pathlib import Path

        source = Path(money_rust.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        allowed = {"importlib", "os", "decimal", "typing", "structlog", "__future__"}
        allowed_from = allowed | {"collections.abc", "types"}

        def module_scope_nodes(body: list[ast.stmt]) -> list[ast.stmt]:
            # Everything except function bodies (a nested `if TYPE_CHECKING:`
            # still executes at import, so it stays module scope).
            out: list[ast.stmt] = []
            for stmt in body:
                if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef):
                    continue
                out.append(stmt)
                for field in ("body", "orelse", "finalbody"):
                    nested = getattr(stmt, field, None)
                    if isinstance(nested, list):
                        out.extend(module_scope_nodes(nested))
            return out

        module_level = module_scope_nodes(tree.body)
        for node in module_level:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name.split(".")[0] in allowed, alias.name
            elif isinstance(node, ast.ImportFrom):
                assert node.module is not None
                assert node.module in allowed_from, node.module

        # Deferred (function-scope) imports: exactly the one strict gate.
        module_level_ids = {id(node) for node in module_level}
        deferred: list[str] = []
        for node in ast.walk(tree):
            if id(node) in module_level_ids:
                continue
            if isinstance(node, ast.Import):
                deferred.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                assert node.module is not None
                deferred.append(node.module)
        assert deferred == ["brutex.config.rust_strict"], deferred

    def test_truthy_falsy_mirror_sweep_lookahead(self) -> None:
        # The leaf may not IMPORT env_flag_tristate (money is a ROOT leaf —
        # brutex.data.parquet_writer imports brutex.money at module scope,
        # and brutex.engine hard-imports the fno_math wheel) — so its
        # inline vocabulary is mirror-PINNED instead (the feed_rust
        # precedent; skips only where the engine wheel is absent).
        sweep_lookahead = pytest.importorskip("brutex.engine.sweep_lookahead")
        assert money_rust._TRUTHY == sweep_lookahead._TRUTHY  # pyright: ignore[reportPrivateUsage]
        assert money_rust._FALSY == sweep_lookahead._FALSY  # pyright: ignore[reportPrivateUsage]


# ── Reset seam + latch + warn-once ──────────────────────────────────────────


class TestResetSeamAndLatch:
    @pytest.mark.usefixtures("_strict_off")
    def test_reset_restores_fresh_process_view(self) -> None:
        money_rust._disable("some_reason")  # pyright: ignore[reportPrivateUsage]
        assert money_rust.money_rust_disabled() is True
        money_rust.reset_money_rust_for_tests()
        assert money_rust.money_rust_disabled() is False
        for law in money_rust._BURN_IN_REMAINING:  # pyright: ignore[reportPrivateUsage]
            budget = money_rust._BURN_IN_REMAINING[law]  # pyright: ignore[reportPrivateUsage]
            assert budget == money_rust.BURN_IN_SAMPLES

    def test_reset_remaining_zero_is_steady_state(self) -> None:
        money_rust.reset_money_rust_for_tests(remaining=0)
        budgets = set(money_rust._BURN_IN_REMAINING.values())  # pyright: ignore[reportPrivateUsage]
        assert budgets == {0}

    @pytest.mark.usefixtures("_strict_off")
    def test_warn_once_suppresses_repeats(self, monkeypatch: pytest.MonkeyPatch) -> None:
        events: list[str] = []
        monkeypatch.setattr(
            money_rust._LOG,  # pyright: ignore[reportPrivateUsage]
            "warning",
            lambda event, **kw: events.append(str(kw.get("reason"))),
        )
        money_rust._warn_once("r1", extra=1)  # pyright: ignore[reportPrivateUsage]
        money_rust._warn_once("r1")  # pyright: ignore[reportPrivateUsage]
        money_rust._warn_once("r2")  # pyright: ignore[reportPrivateUsage]
        assert events == ["r1", "r2"]

    @pytest.mark.usefixtures("_strict_off")
    def test_disable_latches_once_and_repeat_is_noop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        warned: list[str] = []
        monkeypatch.setattr(
            money_rust._LOG,  # pyright: ignore[reportPrivateUsage]
            "warning",
            lambda event, **kw: warned.append(str(kw.get("reason"))),
        )
        money_rust._disable("first")  # pyright: ignore[reportPrivateUsage]
        money_rust._disable("second")  # pyright: ignore[reportPrivateUsage]
        assert money_rust.money_rust_disabled() is True
        assert warned == ["first"]

    @pytest.mark.usefixtures("_strict_off")
    def test_disable_error_logs_autoreverted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        errors: list[str] = []
        monkeypatch.setattr(
            money_rust._LOG,  # pyright: ignore[reportPrivateUsage]
            "error",
            lambda event, **kw: errors.append(event),
        )
        money_rust._disable("mismatch", error=True)  # pyright: ignore[reportPrivateUsage]
        assert errors == ["money_rust.autoreverted"]


# ── OFF-arm bomb-crate spy ──────────────────────────────────────────────────


class TestFlagOffNeverTouchesCrate:
    def test_off_arm_is_byte_identical_and_crate_untouched(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The explicit "0" kill switch (retargeted from the pre-flip unset
        # arm — DEC-RUST-DEFAULT-ON-WAVE-001) + a bomb crate installed + the
        # resolver itself booby-trapped: the OFF arm must never even resolve.
        monkeypatch.setenv(money_rust.MONEY_RUST_ENV, "0")
        monkeypatch.setitem(sys.modules, "brutex_core", _bomb_crate())

        def _resolver_bomb() -> types.ModuleType | None:
            raise AssertionError("_resolve_crate must not run flag-OFF")

        monkeypatch.setattr(money_rust, "_resolve_crate", _resolver_bomb)
        assert str(money.round_money(Decimal("1.005"))) == "1.01"
        assert money.rupees_to_paisa(Decimal("14020.7999")) == 1402080
        assert str(money.paisa_to_rupees(-50)) == "-0.50"
        assert money.format_inr(123456) == "₹1,234.56"
        # TEETH vs a flag-routing mutant: had any call entered the seam, the
        # resolver bomb would have latched via the panic belt — the pristine
        # latch is the proof the OFF arm never touched the apparatus.
        assert money_rust.money_rust_disabled() is False
        assert not money_rust._OBSERVABILITY_MEMO  # pyright: ignore[reportPrivateUsage]

    def test_explicit_falsy_arm_is_off_too(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(money_rust.MONEY_RUST_ENV, "0")
        monkeypatch.setitem(sys.modules, "brutex_core", _bomb_crate())
        assert str(money.round_money(Decimal("-0.001"))) == "-0.00"
        assert money_rust.money_rust_disabled() is False
        assert not money_rust._OBSERVABILITY_MEMO  # pyright: ignore[reportPrivateUsage]


# ── Import guard ────────────────────────────────────────────────────────────


class TestImportGuard:
    @pytest.mark.usefixtures("_strict_off")
    def test_absent_crate_latches_and_serves_python(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _arm(monkeypatch, None)  # sys.modules[name] = None → ImportError
        assert str(money.round_money(Decimal("1.005"))) == "1.01"
        assert money_rust.money_rust_disabled() is True
        assert money_rust._DISABLED_REASON[0] == "crate_unavailable"  # pyright: ignore[reportPrivateUsage]

    @pytest.mark.usefixtures("_strict_off")
    def test_module_missing_api_latches_crate_stale(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(money_rust.MONEY_RUST_ENV, "1")
        monkeypatch.setitem(sys.modules, "brutex_core", types.ModuleType("brutex_core"))
        assert money.rupees_to_paisa(Decimal("1.00")) == 100
        assert money_rust._DISABLED_REASON[0] == "crate_stale"  # pyright: ignore[reportPrivateUsage]

    @pytest.mark.usefixtures("_strict_off")
    def test_non_callable_api_attr_latches_crate_stale(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _arm(monkeypatch, _fake_crate(paisa_to_rupees_str="not-callable"))
        assert str(money.paisa_to_rupees(5)) == "0.05"
        assert money_rust._DISABLED_REASON[0] == "crate_stale"  # pyright: ignore[reportPrivateUsage]

    @pytest.mark.usefixtures("_strict_off")
    def test_latched_process_serves_python_without_resolving(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _arm(monkeypatch, _bomb_crate())
        money_rust._disable("prior_reason")  # pyright: ignore[reportPrivateUsage]
        assert money.format_inr(5) == "₹0.05"  # bomb untouched — latch short-circuits


# ── Eligibility gate (layer 2 — crate never touched, spy-pinned) ────────────


class TestEligibilityGate:
    @pytest.mark.parametrize(
        "rupees",
        [
            Decimal("NaN"),
            Decimal("sNaN"),
            Decimal("Infinity"),
            Decimal("-Infinity"),
            Decimal("1E+21"),  # adjusted > 20
            Decimal("1E-61"),  # exponent < -60
            Decimal("1e26"),  # oracle quantize InvalidOperation class
        ],
    )
    def test_ineligible_decimals_route_to_oracle_without_crate(
        self, monkeypatch: pytest.MonkeyPatch, rupees: Decimal
    ) -> None:
        _arm(monkeypatch, _bomb_crate())
        oracle_outcome: object
        try:
            oracle_outcome = money._round_money_python(rupees)  # pyright: ignore[reportPrivateUsage]
        except ArithmeticError as exc:
            oracle_outcome = type(exc)
        if isinstance(oracle_outcome, type):
            with pytest.raises(oracle_outcome):
                money.round_money(rupees)
        else:
            # str-compare — a quiet-NaN oracle result is NaN != NaN by value
            # (probed: quantize of a quiet NaN RETURNS NaN, only sNaN/Inf
            # raise InvalidOperation at default traps).
            assert str(money.round_money(rupees)) == str(oracle_outcome)

    def test_non_decimal_inputs_reproduce_todays_exact_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _arm(monkeypatch, _bomb_crate())
        with pytest.raises(AttributeError, match="quantize"):
            money.round_money(3.5)  # type: ignore[arg-type]
        with pytest.raises(AttributeError, match="quantize"):
            money.rupees_to_paisa(7)  # type: ignore[arg-type]

    def test_decimal_subclass_routes_to_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class MyDecimal(Decimal):
            pass

        _arm(monkeypatch, _bomb_crate())
        assert str(money.round_money(MyDecimal("1.005"))) == "1.01"

    def test_bool_and_huge_paisa_route_to_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _arm(monkeypatch, _bomb_crate())
        assert str(money.paisa_to_rupees(True)) == "0.01"  # bool → oracle (Decimal(True) == 1)
        big = 2**63
        assert str(money.paisa_to_rupees(big)) == str(
            money._paisa_to_rupees_python(big)  # pyright: ignore[reportPrivateUsage]
        )
        assert money.format_inr(-big) == money._format_inr_python(-big)  # pyright: ignore[reportPrivateUsage]

    def test_shrunken_context_precision_routes_to_oracle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _arm(monkeypatch, _bomb_crate())
        with localcontext() as ctx:
            ctx.prec = 6
            # adjusted(1234.5) = 3; 3 + 5 > 6 → oracle (which still succeeds).
            assert str(money.round_money(Decimal("1234.5"))) == "1234.50"
            # paisa digit law: 5 digits + 2 > 6 → oracle.
            assert str(money.paisa_to_rupees(12345)) == "123.45"

    @pytest.mark.parametrize("trap", [Inexact, Rounded])
    def test_armed_rounding_traps_route_to_oracle(
        self, monkeypatch: pytest.MonkeyPatch, trap: object
    ) -> None:
        _arm(monkeypatch, _bomb_crate())
        with localcontext() as ctx:
            ctx.traps[trap] = True  # type: ignore[index]
            # An exact op still succeeds through the oracle; the crate is
            # never consulted while the ambient context can raise.
            assert str(money.round_money(Decimal("1.50"))) == "1.50"
            assert money.format_inr(150) == "₹1.50"

    def test_eligible_paisa_and_rupees_true_paths(self) -> None:
        assert money_rust._eligible_rupees(Decimal("1.005")) is True  # pyright: ignore[reportPrivateUsage]
        assert money_rust._eligible_paisa(123456) is True  # pyright: ignore[reportPrivateUsage]


# ── Strict mode (DEC-RUST-STRICT-NO-FALLBACK-001) ───────────────────────────


class TestStrictMode:
    """Class A/B fall-backs REFUSE; the Class-C eligibility gate does not.

    The split is structural, not a per-reason allowlist: the eligibility gate
    returns the oracle *before* either choke point (``_warn_once`` /
    ``_disable``), so an ineligible input has no reason token to refuse.
    """

    def test_absent_crate_refuses_under_strict_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Strict is ON by default — the fixture never sets the env at all.
        _arm(monkeypatch, None)  # sys.modules[name] = None → ImportError
        with pytest.raises(RustFallbackRefused) as excinfo:
            money.round_money(Decimal("1.005"))
        message = str(excinfo.value)
        assert "brutex.money_rust" in message
        assert "crate_unavailable" in message
        assert RUST_STRICT_ENV in message
        # Refused BEFORE the latch — no silently-degraded steady state.
        assert money_rust.money_rust_disabled() is False

    def test_junk_flag_refuses_under_strict_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(money_rust.MONEY_RUST_ENV, "onn")
        with pytest.raises(RustFallbackRefused, match="flag_unrecognised"):
            money_rust.money_rust_enabled()

    def test_degrade_set_raise_refuses_under_strict_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(_s: str) -> str:
            raise ValueError("boom")

        _arm(monkeypatch, _fake_crate(round_money_str=_boom))
        with pytest.raises(RustFallbackRefused, match="round_money_error"):
            money.round_money(Decimal("1.005"))

    def test_poisoned_result_refuses_under_strict_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _arm(monkeypatch, _fake_crate(round_money_str=lambda _s: "not-a-number"))
        with pytest.raises(RustFallbackRefused, match="round_money_result_refused"):
            money.round_money(Decimal("1.005"))

    @pytest.mark.parametrize(
        "rupees",
        [
            Decimal("1E+21"),  # adjusted > 20 — outside the crate's envelope
            Decimal("1E-61"),  # exponent < -60
        ],
    )
    def test_ineligible_input_still_serves_the_oracle_under_strict(
        self, monkeypatch: pytest.MonkeyPatch, rupees: Decimal
    ) -> None:
        # CLASS C: the crate deliberately cannot represent these, Python was
        # always the intended handler. Refusing would crash a legitimate
        # input — the bomb crate proves the crate is never even consulted.
        _arm(monkeypatch, _bomb_crate())
        assert str(money.round_money(rupees)) == str(
            money._round_money_python(rupees)  # pyright: ignore[reportPrivateUsage]
        )
        assert money_rust.money_rust_disabled() is False

    def test_decimal_subclass_still_serves_the_oracle_under_strict(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class MyDecimal(Decimal):
            pass

        _arm(monkeypatch, _bomb_crate())
        assert str(money.round_money(MyDecimal("1.005"))) == "1.01"

    def test_armed_trap_still_serves_the_oracle_under_strict(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _arm(monkeypatch, _bomb_crate())
        with localcontext() as ctx:
            ctx.traps[Inexact] = True
            assert str(money.round_money(Decimal("1.50"))) == "1.50"

    def test_kill_switch_restores_the_degrade_arm(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(RUST_STRICT_ENV, "0")
        _arm(monkeypatch, None)
        assert str(money.round_money(Decimal("1.005"))) == "1.01"
        assert money_rust.money_rust_disabled() is True

    def test_engaged_success_arm_is_untouched_by_strict(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _arm(monkeypatch, _fake_crate())
        assert str(money.round_money(Decimal("1.005"))) == "1.01"
        assert money_rust.money_rust_disabled() is False


class TestNarrowedContextGate:
    """The r2 MINOR fold: narrowed ambient ``Emax``/``Emin`` route to the oracle.

    Executed repro (pre-fix): under ``localcontext(Emax=1)``, steady state
    (``remaining=0``), flag ON with a context-INDEPENDENT crate, all four
    laws returned a VALUE (e.g. ``round_money(Decimal("100")) → 100.00``)
    where flag-OFF RAISES (``InvalidOperation``/``Overflow``) — a
    raise-vs-value divergence the burn-in structurally cannot catch once
    exhausted. Post-fix the exponent-window conjunct in
    ``_context_hazard`` routes such contexts to the oracle wholesale.
    """

    def test_narrowed_emax_all_four_laws_raise_identically_to_off(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The law-faithful context-free fake is the fails-before tooth:
        # pre-fix it was CONSULTED and its value ADOPTED (no raise); the
        # identity-echo fake would itself raise under the narrowed
        # context and mask the divergence behind a crate_panic latch.
        _arm(monkeypatch, _context_free_crate())
        money_rust.reset_money_rust_for_tests(remaining=0)  # steady state — no oracle cushion
        laws: list[tuple[str, Callable[[], object], Callable[[], object]]] = [
            (
                "round_money",
                lambda: money.round_money(Decimal("100")),
                lambda: money._round_money_python(Decimal("100")),  # pyright: ignore[reportPrivateUsage]
            ),
            (
                "rupees_to_paisa",
                lambda: money.rupees_to_paisa(Decimal("100")),
                lambda: money._rupees_to_paisa_python(Decimal("100")),  # pyright: ignore[reportPrivateUsage]
            ),
            (
                "paisa_to_rupees",
                lambda: money.paisa_to_rupees(12345),
                lambda: money._paisa_to_rupees_python(12345),  # pyright: ignore[reportPrivateUsage]
            ),
            (
                "format_inr",
                lambda: money.format_inr(12345),
                lambda: money._format_inr_python(12345),  # pyright: ignore[reportPrivateUsage]
            ),
        ]
        with localcontext() as ctx:
            ctx.Emax = 1
            for law, routed, oracle in laws:
                with pytest.raises(ArithmeticError) as off_exc:
                    oracle()  # flag-OFF behavior: the oracle RAISES
                with pytest.raises(type(off_exc.value)):
                    routed()  # flag-ON must raise IDENTICALLY, not serve
                assert off_exc.type.__name__ in {"InvalidOperation", "Overflow"}, law
        # The gate (not a degrade/latch) did the routing — crate untouched.
        assert money_rust.money_rust_disabled() is False
        assert not money_rust._OBSERVABILITY_MEMO  # pyright: ignore[reportPrivateUsage]

    def test_narrowed_emin_with_shrunken_prec_routes_to_oracle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The Emin arm needs a JOINTLY-narrowed prec: Etiny = Emin − prec
        # + 1, so (prec=2, Emin=0) puts Etiny = −1 ABOVE the 2dp quantize
        # target (exponent −2) and the oracle raises InvalidOperation on
        # an otherwise-eligible tiny input — Decimal("0.001") has
        # adjusted −3, and the prec-headroom conjunct −3 + 5 = 2 <= 2
        # PASSES, so pre-fix the input crossed and the context-free
        # crate's "0.00" was adopted.
        _arm(monkeypatch, _context_free_crate())
        money_rust.reset_money_rust_for_tests(remaining=0)
        with localcontext() as ctx:
            ctx.prec = 2
            ctx.Emin = 0
            with pytest.raises(ArithmeticError) as off_exc:
                money._round_money_python(Decimal("0.001"))  # pyright: ignore[reportPrivateUsage]
            with pytest.raises(type(off_exc.value)):
                money.round_money(Decimal("0.001"))
        assert money_rust.money_rust_disabled() is False
        assert not money_rust._OBSERVABILITY_MEMO  # pyright: ignore[reportPrivateUsage]

    def test_safe_boundary_window_stays_eligible(self) -> None:
        # Emax=25 / Emin=-65 are the SAFE bounds — exactly at them the
        # gate must NOT over-route (the envelope ceiling stays eligible;
        # probed: max result adjusted is 23, min operand exponent −62).
        with localcontext() as ctx:
            ctx.Emax = money_rust._MIN_SAFE_EMAX  # pyright: ignore[reportPrivateUsage]
            ctx.Emin = money_rust._MAX_SAFE_EMIN  # pyright: ignore[reportPrivateUsage]
            assert money_rust._eligible_rupees(Decimal("1.005")) is True  # pyright: ignore[reportPrivateUsage]
            assert money_rust._eligible_paisa(123456) is True  # pyright: ignore[reportPrivateUsage]
            # And the whole-envelope ceiling input survives the ORACLE at
            # these bounds (the derivation's safety claim, executed).
            ceiling = Decimal("9" * 21 + ".995")  # adjusted == 20
            # rounds to 10**21 rupees; × 100 → 10**23 paisa (adjusted 23).
            assert money._rupees_to_paisa_python(ceiling) == 10**23  # pyright: ignore[reportPrivateUsage]
            assert str(money._round_money_python(Decimal("1E-60"))) == "0.00"  # pyright: ignore[reportPrivateUsage]

    @pytest.mark.parametrize(
        ("emax", "emin"),
        [(24, -999999), (1, -999999), (999999, -64), (999999, 0), (24, -64)],
    )
    def test_past_boundary_windows_are_ineligible(self, emax: int, emin: int) -> None:
        with localcontext() as ctx:
            ctx.Emax = emax
            ctx.Emin = emin
            assert money_rust._eligible_rupees(Decimal("1.005")) is False  # pyright: ignore[reportPrivateUsage]
            assert money_rust._eligible_paisa(123456) is False  # pyright: ignore[reportPrivateUsage]


# ── Degrade paths + escape set + panic belt ─────────────────────────────────


class TestDegradePaths:
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
    @pytest.mark.usefixtures("_strict_off")
    def test_crate_errors_degrade_with_one_warn(
        self, monkeypatch: pytest.MonkeyPatch, exc: Exception
    ) -> None:
        def _boom(_s: str) -> str:
            raise exc

        _arm(monkeypatch, _fake_crate(round_money_str=_boom))
        warned: list[str] = []
        monkeypatch.setattr(
            money_rust._LOG,  # pyright: ignore[reportPrivateUsage]
            "warning",
            lambda event, **kw: warned.append(str(kw.get("reason"))),
        )
        assert str(money.round_money(Decimal("1.005"))) == "1.01"
        assert str(money.round_money(Decimal("2.675"))) == "2.68"
        assert warned == ["round_money_error"]
        assert money_rust.money_rust_disabled() is False  # degrade ≠ latch

    @pytest.mark.parametrize("exc_type", [KeyboardInterrupt, SystemExit, MemoryError])
    def test_escape_set_propagates_bare(
        self, monkeypatch: pytest.MonkeyPatch, exc_type: type[BaseException]
    ) -> None:
        def _boom(_s: str) -> str:
            raise exc_type()

        _arm(monkeypatch, _fake_crate(format_inr_str=_boom))
        with pytest.raises(exc_type):
            money.format_inr(5)

    @pytest.mark.usefixtures("_strict_off")
    def test_out_of_degrade_set_exception_latches_crate_panic(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(_s: str) -> str:
            raise ZeroDivisionError("broken boundary")

        _arm(monkeypatch, _fake_crate(rupees_to_paisa_str=_boom))
        assert money.rupees_to_paisa(Decimal("1.005")) == 101
        assert money_rust._DISABLED_REASON[0] == "crate_panic"  # pyright: ignore[reportPrivateUsage]

    @pytest.mark.usefixtures("_strict_off")
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

        _arm(monkeypatch, _fake_crate(paisa_to_rupees_str=_boom))
        assert str(money.paisa_to_rupees(5)) == "0.05"
        assert money_rust._DISABLED_REASON[0] == "crate_panic"  # pyright: ignore[reportPrivateUsage]


class TestPanicBelt:
    def test_belt_always_carries_exception_first(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "pyo3_runtime", None)  # → ImportError arm
        belt = money_rust._panic_belt_errors()  # pyright: ignore[reportPrivateUsage]
        assert belt == (Exception,)

    def test_belt_folds_a_real_panic_class(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = types.ModuleType("pyo3_runtime")

        class PanicException(BaseException):
            pass

        fake.PanicException = PanicException  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "pyo3_runtime", fake)
        belt = money_rust._panic_belt_errors()  # pyright: ignore[reportPrivateUsage]
        assert belt == (Exception, PanicException)

    def test_non_class_and_non_baseexception_attrs_are_skipped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = types.ModuleType("pyo3_runtime")
        fake.PanicException = "not-a-class"  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "pyo3_runtime", fake)
        assert money_rust._panic_belt_errors() == (Exception,)  # pyright: ignore[reportPrivateUsage]
        fake2 = types.ModuleType("pyo3_runtime")
        fake2.PanicException = dict  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "pyo3_runtime", fake2)
        assert money_rust._panic_belt_errors() == (Exception,)  # pyright: ignore[reportPrivateUsage]


# ── Refuse-poison gate ──────────────────────────────────────────────────────


class TestRefusePoison:
    @pytest.mark.usefixtures("_strict_off")
    def test_non_str_result_refused_loudly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _arm(monkeypatch, _fake_crate(round_money_str=lambda _s: 1.01))
        warned: list[str] = []
        monkeypatch.setattr(
            money_rust._LOG,  # pyright: ignore[reportPrivateUsage]
            "warning",
            lambda event, **kw: warned.append(str(kw.get("reason"))),
        )
        assert str(money.round_money(Decimal("1.005"))) == "1.01"
        assert warned == ["round_money_result_refused"]

    @pytest.mark.parametrize(
        "poison",
        ["1.0", "1", "1.005", "", "-", "a.bc", "1.٢٣", "1_0.00", "+1.00", " 1.00"],
    )
    @pytest.mark.usefixtures("_strict_off")
    def test_malformed_fixed2_refused(self, monkeypatch: pytest.MonkeyPatch, poison: str) -> None:
        _arm(monkeypatch, _fake_crate(round_money_str=lambda _s: poison))
        assert str(money.round_money(Decimal("1.005"))) == "1.01"

    @pytest.mark.parametrize("poison", ["", "-", "1.0", "1_0", "١٢", "+1", "0x10"])
    @pytest.mark.usefixtures("_strict_off")
    def test_malformed_int_refused(self, monkeypatch: pytest.MonkeyPatch, poison: str) -> None:
        _arm(monkeypatch, _fake_crate(rupees_to_paisa_str=lambda _s: poison))
        assert money.rupees_to_paisa(Decimal("1.00")) == 100

    @pytest.mark.parametrize(
        "poison",
        [
            "1,234.56",  # missing ₹
            "₹1234.56",  # 4-digit group
            "₹12,34.56",  # wrong grouping (2-digit tail group)
            "₹1,23.56",
            "₹,123.56",
            "₹1,234",  # no decimals
            "₹1,234.5",
            "₹1,234.5x",  # non-digit fraction at a valid "." position
            "₹-1,2b4.56",
            "₹--1.00",
        ],
    )
    @pytest.mark.usefixtures("_strict_off")
    def test_malformed_inr_refused(self, monkeypatch: pytest.MonkeyPatch, poison: str) -> None:
        _arm(monkeypatch, _fake_crate(format_inr_str=lambda _s: poison))
        assert money.format_inr(123456) == "₹1,234.56"

    def test_valid_inr_shapes_accepted_by_validator(self) -> None:
        valid = money_rust._valid_inr_str  # pyright: ignore[reportPrivateUsage]
        for good in ["₹0.05", "₹999.99", "₹1,234.56", "₹-1,234.56", "₹1,000,000.00"]:
            assert valid(good), good


# ── Burn-in + autorevert ────────────────────────────────────────────────────


class TestBurnIn:
    def test_clean_burn_in_ships_rust_value_and_completes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        money_rust.reset_money_rust_for_tests(remaining=2)
        _arm(monkeypatch, _fake_crate())
        infos: list[str] = []
        monkeypatch.setattr(
            money_rust._LOG,  # pyright: ignore[reportPrivateUsage]
            "info",
            lambda event, **kw: infos.append(event),
        )
        assert str(money.round_money(Decimal("1.005"))) == "1.01"
        assert str(money.round_money(Decimal("2.675"))) == "2.68"
        remaining = money_rust._BURN_IN_REMAINING["round_money"]  # pyright: ignore[reportPrivateUsage]
        assert remaining == 0
        assert "money_rust.engaged" in infos
        assert "money_rust.burn_in_complete" in infos
        # Third call — steady state, no further oracle compare needed.
        assert str(money.round_money(Decimal("9.995"))) == "10.00"

    def test_burn_in_budgets_are_per_law(self, monkeypatch: pytest.MonkeyPatch) -> None:
        money_rust.reset_money_rust_for_tests(remaining=1)
        _arm(monkeypatch, _fake_crate())
        assert str(money.round_money(Decimal("1.005"))) == "1.01"
        budgets = money_rust._BURN_IN_REMAINING  # pyright: ignore[reportPrivateUsage]
        assert budgets["round_money"] == 0
        assert budgets["format_inr"] == 1  # untouched sibling budget

    @pytest.mark.usefixtures("_strict_off")
    def test_burn_in_mismatch_autoreverts_and_ships_oracle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A wrong-but-well-shaped crate value: the burn-in must catch it,
        # ship the ORACLE value and latch the process OFF.
        _arm(monkeypatch, _fake_crate(round_money_str=lambda _s: "9.99"))
        errors: list[str] = []
        monkeypatch.setattr(
            money_rust._LOG,  # pyright: ignore[reportPrivateUsage]
            "error",
            lambda event, **kw: errors.append(str(kw.get("reason"))),
        )
        assert str(money.round_money(Decimal("1.005"))) == "1.01"
        assert errors == ["burn_in_mismatch_round_money"]
        assert money_rust.money_rust_disabled() is True
        # And the NEXT call short-circuits at the latch (bomb-safe).
        monkeypatch.setitem(sys.modules, "brutex_core", _bomb_crate())
        assert str(money.round_money(Decimal("1.005"))) == "1.01"

    @pytest.mark.usefixtures("_strict_off")
    def test_post_burn_in_poison_is_caught_by_shape_not_burn_in(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Steady state (remaining=0): a WELL-SHAPED wrong value would ship
        # (the documented burn-in envelope); the SHAPE gate still refuses
        # malformed ones. Pin the envelope honestly.
        money_rust.reset_money_rust_for_tests(remaining=0)
        _arm(monkeypatch, _fake_crate(round_money_str=lambda _s: "banana"))
        assert str(money.round_money(Decimal("1.005"))) == "1.01"  # refused → oracle
        money_rust.reset_money_rust_for_tests(remaining=0)
        _arm(monkeypatch, _fake_crate(round_money_str=lambda _s: "9.99"))
        assert str(money.round_money(Decimal("1.005"))) == "9.99"  # the honest envelope


# ── Routed wiring (each law's success arm through brutex.money) ─────────────


class TestRoutedWiring:
    def test_each_public_law_routes_through_its_crate_callable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[tuple[str, str]] = []

        def _spy(name: str, fn: Callable[[str], str]) -> Callable[[str], str]:
            def inner(s: str) -> str:
                calls.append((name, s))
                return fn(s)

            return inner

        crate = _fake_crate()
        for name in money_rust._CRATE_API:  # pyright: ignore[reportPrivateUsage]
            setattr(crate, name, _spy(name, getattr(crate, name)))
        monkeypatch.setenv(money_rust.MONEY_RUST_ENV, "1")
        monkeypatch.setitem(sys.modules, "brutex_core", crate)
        money_rust.reset_money_rust_for_tests(remaining=0)

        assert money.round_money(Decimal("1.005")) == Decimal("1.01")
        assert money.rupees_to_paisa(Decimal("1.005")) == 101
        assert money.paisa_to_rupees(-50) == Decimal("-0.50")
        assert money.format_inr(123456) == "₹1,234.56"
        assert calls == [
            ("round_money_str", "1.005"),
            ("rupees_to_paisa_str", "1.005"),
            ("paisa_to_rupees_str", "-50"),
            ("format_inr_str", "123456"),
        ]

    def test_scientific_decimal_crosses_as_fixed_point(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[str] = []
        crate = _fake_crate()
        real = crate.round_money_str

        def _capture(s: str) -> str:
            seen.append(s)
            return real(s)  # type: ignore[no-any-return]

        crate.round_money_str = _capture  # type: ignore[attr-defined]
        _arm(monkeypatch, crate)
        assert str(money.round_money(Decimal("1E+2"))) == "100.00"
        assert seen == ["100"]  # format(d, "f") — never "1E+2"

    def test_result_objects_reconstruct_identically(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _arm(monkeypatch, _fake_crate())
        money_rust.reset_money_rust_for_tests(remaining=0)
        routed = money.round_money(Decimal("-0.001"))
        oracle = money._round_money_python(Decimal("-0.001"))  # pyright: ignore[reportPrivateUsage]
        assert routed.as_tuple() == oracle.as_tuple()  # sign + digits + exponent
        assert str(routed) == "-0.00"


# ── ON-arm parity with the REAL wheel (skips cleanly when absent) ───────────


class TestRealCrateParity:
    """Flag-ON legs against the real ``brutex_core`` wheel."""

    @pytest.fixture(autouse=True)
    def _require_wheel(self) -> None:
        pytest.importorskip(
            "brutex_core", reason="brutex-core wheel not built in this env (parity lane covers it)"
        )

    def test_half_boundary_cells_byte_identical_both_arms(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cells = [
            "1.005",
            "-1.005",
            "0.005",
            "-0.005",
            "1.004",
            "9.995",
            "-9.995",
            "0.995",
            "99.995",
            "2.675",
            "14020.7999",
            "0.00499999999",
            "-0.001",
            "-0",
            "0",
            "1.0100",
            "123456789.995",
        ]
        off = [str(money._round_money_python(Decimal(c))) for c in cells]  # pyright: ignore[reportPrivateUsage]
        monkeypatch.setenv(money_rust.MONEY_RUST_ENV, "1")
        money_rust.reset_money_rust_for_tests(remaining=0)  # steady state — no oracle cushion
        on = [str(money.round_money(Decimal(c))) for c in cells]
        assert on == off

    def test_frozen_test_money_pins_hold_flag_on_with_engagement_spy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import brutex_core

        calls: list[str] = []
        real = brutex_core.round_money_str

        def _spy(s: str) -> str:
            calls.append(s)
            return real(s)

        monkeypatch.setattr(brutex_core, "round_money_str", _spy)
        monkeypatch.setenv(money_rust.MONEY_RUST_ENV, "1")
        money_rust.reset_money_rust_for_tests(remaining=0)
        # The frozen test_money.py pin table, re-run flag-ON:
        for raw, expected in [
            ("1.005", "1.01"),
            ("1.004", "1.00"),
            ("1234.5", "1234.50"),
            ("-0.005", "-0.01"),
            ("0", "0.00"),
        ]:
            assert money.round_money(Decimal(raw)) == Decimal(expected)
        assert len(calls) == 5  # genuinely ENGAGED — not a silent degrade
        for paisa, expected_str in [(1, "0.01"), (123456, "1234.56"), (-50, "-0.50")]:
            assert money.paisa_to_rupees(paisa) == Decimal(expected_str)
        for rupees_str, expected_paisa in [
            ("0.01", 1),
            ("14020.7999", 1402080),
            ("1.005", 101),
            ("-0.50", -50),
        ]:
            assert money.rupees_to_paisa(Decimal(rupees_str)) == expected_paisa
        for paisa, rendered in [
            (5, "₹0.05"),
            (123456, "₹1,234.56"),
            (100_000_000, "₹1,000,000.00"),
            (0, "₹0.00"),
            (-123456, "₹-1,234.56"),
        ]:
            assert money.format_inr(paisa) == rendered

    def test_real_crate_burn_in_completes_clean(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(money_rust.MONEY_RUST_ENV, "1")
        money_rust.reset_money_rust_for_tests()  # full 16-sample budgets live
        for _ in range(money_rust.BURN_IN_SAMPLES + 1):
            assert money.rupees_to_paisa(Decimal("14020.7999")) == 1402080
        assert money_rust.money_rust_disabled() is False
        assert money_rust._BURN_IN_REMAINING["rupees_to_paisa"] == 0  # pyright: ignore[reportPrivateUsage]

    def test_hypothesis_differential_all_four_laws(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from hypothesis import HealthCheck, given, settings
        from hypothesis import strategies as st

        monkeypatch.setenv(money_rust.MONEY_RUST_ENV, "1")

        decimals = st.builds(
            lambda sign, digits, exp: Decimal((sign, tuple(digits), exp)),
            st.integers(min_value=0, max_value=1),
            st.lists(st.integers(min_value=0, max_value=9), min_size=1, max_size=24),
            st.integers(min_value=-40, max_value=8),
        ).filter(
            # Keep the ORACLE itself raise-free (quantize + ×100 headroom at
            # the default prec 28 needs adjusted <= 22) while still crossing
            # the ELIGIBILITY bound (20 < adjusted <= 22 exercises the
            # ineligible-but-oracle-safe routing arm).
            lambda d: d.adjusted() <= 22
        )
        paisas = st.integers(min_value=-(2**64), max_value=2**64)

        @settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
        @given(rupees=decimals, paisa=paisas)
        def _differential(rupees: Decimal, paisa: int) -> None:
            money_rust.reset_money_rust_for_tests(remaining=0)
            off_round = money._round_money_python(rupees)  # pyright: ignore[reportPrivateUsage]
            off_paisa = money._rupees_to_paisa_python(rupees)  # pyright: ignore[reportPrivateUsage]
            off_rupees = money._paisa_to_rupees_python(paisa)  # pyright: ignore[reportPrivateUsage]
            off_inr = money._format_inr_python(paisa)  # pyright: ignore[reportPrivateUsage]
            assert str(money.round_money(rupees)) == str(off_round)
            assert money.rupees_to_paisa(rupees) == off_paisa
            assert str(money.paisa_to_rupees(paisa)) == str(off_rupees)
            assert money.format_inr(paisa) == off_inr
            assert money_rust.money_rust_disabled() is False, (
                "a burn-in/latch trip inside the differential marks a real divergence"
            )

        _differential()

    def test_hypothesis_differential_narrowed_contexts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # r2 MINOR fold: OUTCOME parity (value OR exception type) between
        # the flag-ON public laws and the flag-OFF oracles under sampled
        # narrowed exponent windows — straddling the safe bounds
        # (Emax 25 / Emin −65) both sides, so the gate's routing and the
        # wide-window crate path are both exercised.
        from hypothesis import HealthCheck, given, settings
        from hypothesis import strategies as st

        monkeypatch.setenv(money_rust.MONEY_RUST_ENV, "1")

        def _outcome(fn: Callable[[], object]) -> tuple[str, str]:
            try:
                return ("value", str(fn()))
            except ArithmeticError as exc:
                return ("raise", type(exc).__name__)

        decimals = st.builds(
            lambda sign, digits, exp: Decimal((sign, tuple(digits), exp)),
            st.integers(min_value=0, max_value=1),
            st.lists(st.integers(min_value=0, max_value=9), min_size=1, max_size=24),
            st.integers(min_value=-40, max_value=8),
        )
        paisas = st.integers(min_value=-(2**64), max_value=2**64)
        emaxes = st.sampled_from([1, 5, 24, 25, 100, 999999])
        emins = st.sampled_from([0, -5, -64, -65, -100, -999999])

        @settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
        @given(rupees=decimals, paisa=paisas, emax=emaxes, emin=emins)
        def _differential(rupees: Decimal, paisa: int, emax: int, emin: int) -> None:
            money_rust.reset_money_rust_for_tests(remaining=0)
            with localcontext() as ctx:
                ctx.Emax = emax
                ctx.Emin = emin
                off = [
                    _outcome(lambda: money._round_money_python(rupees)),  # pyright: ignore[reportPrivateUsage]
                    _outcome(lambda: money._rupees_to_paisa_python(rupees)),  # pyright: ignore[reportPrivateUsage]
                    _outcome(lambda: money._paisa_to_rupees_python(paisa)),  # pyright: ignore[reportPrivateUsage]
                    _outcome(lambda: money._format_inr_python(paisa)),  # pyright: ignore[reportPrivateUsage]
                ]
                on = [
                    _outcome(lambda: money.round_money(rupees)),
                    _outcome(lambda: money.rupees_to_paisa(rupees)),
                    _outcome(lambda: money.paisa_to_rupees(paisa)),
                    _outcome(lambda: money.format_inr(paisa)),
                ]
            assert on == off
            assert money_rust.money_rust_disabled() is False, (
                "a latch trip under a narrowed context marks a gate hole — the crate "
                "must never be consulted where the oracle could raise"
            )

        _differential()
