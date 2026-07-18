"""Tests for the fail-closed Rust pull-state facade (DEC-RUST-PULL-STATE-001,
flip phase — ``brutex/pull_state_rust.py``).

The single-arm world: the ``brutex_pull_state`` kernel is THE decision plane
— the Python oracles are deleted, so every fault (removed-knob-still-SET,
wheel missing/stale, kernel raise, poisoned result, out-of-domain input) is a
TYPED raise, never a silent degrade. Fault arms are driven via
``sys.modules`` stubs/bombs; happy paths run against the REAL installed
wheel with hand-derived expected values. The ci.yml way:

    pytest --cov=brutex.pull_state_rust --cov-branch --cov-report=term-missing \\
        --cov-fail-under=100 tests/unit/test_pull_state_rust.py \\
        tests/unit/test_pull_state_rust_stall.py
"""

from __future__ import annotations

import importlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import brutex_pull_state  # the flip demands the wheel — an ImportError here is a REAL failure
import pytest
from brutex import pull_state_rust
from brutex.pull_state_rust import PullStateFlagRemovedError, PullStateKernelError

_IST_LIKE = UTC  # tzinfo is irrelevant: only .time() is read.
_DT = datetime(2026, 5, 7, 9, 0, tzinfo=_IST_LIKE)

#: One microsecond-of-day helper for the auto-gate/Muhurat packing pins.
_US = 1_000_000


def _h(hh: int, mm: int = 0, ss: int = 0) -> int:
    """Microsecond-of-day for a wall-clock ``HH:MM:SS``."""
    return ((hh * 60 + mm) * 60 + ss) * _US


@pytest.fixture(autouse=True)
def _unset_removed_knob(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hermetic default: the removed knob is UNSET unless a test sets it."""
    monkeypatch.delenv(pull_state_rust.PULL_STATE_RUST_ENV, raising=False)


def _install(monkeypatch: pytest.MonkeyPatch, **symbols: Any) -> None:
    """Plant a stub wheel module exposing exactly *symbols*."""
    monkeypatch.setitem(sys.modules, "brutex_pull_state", SimpleNamespace(**symbols))


def _poison_import(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``import brutex_pull_state`` raise ImportError deterministically."""
    monkeypatch.setitem(sys.modules, "brutex_pull_state", None)


def test_none_in_sys_modules_makes_import_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    """The import-bomb MECHANISM itself: None in sys.modules ⇒ ImportError."""
    _poison_import(monkeypatch)
    with pytest.raises(ImportError):
        importlib.import_module("brutex_pull_state")


# ── require_pull_state_env_unset — the SET-knob refusal guard ────────────────


class TestRequirePullStateEnvUnset:
    """The DEC-FILL-WORST-CASE-ONLY-001 require_fill_mode_env_unset precedent."""

    def test_unset_passes(self) -> None:
        """An absent variable is the healthy state — no raise."""
        pull_state_rust.require_pull_state_env_unset(env={})

    @pytest.mark.parametrize("raw", ["", "   ", "\t\n"])
    def test_empty_and_whitespace_pass(self, raw: str) -> None:
        """The ``VAR= cmd`` unset idiom: empty/whitespace-only is not configured."""
        pull_state_rust.require_pull_state_env_unset(env={pull_state_rust.PULL_STATE_RUST_ENV: raw})

    @pytest.mark.parametrize("raw", ["1", "true", "0", "junk", " on ", "off"])
    def test_any_non_empty_value_refuses(self, raw: str) -> None:
        """Truthy AND falsy spellings alike refuse — the knob itself is removed."""
        with pytest.raises(PullStateFlagRemovedError) as excinfo:
            pull_state_rust.require_pull_state_env_unset(
                env={pull_state_rust.PULL_STATE_RUST_ENV: raw}
            )
        message = str(excinfo.value)
        assert "DEC-RUST-PULL-STATE-001" in message
        assert pull_state_rust.PULL_STATE_RUST_ENV in message
        assert "Unset" in message

    def test_default_reads_os_environ(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``env=None`` reads the live environment."""
        monkeypatch.setenv(pull_state_rust.PULL_STATE_RUST_ENV, "1")
        with pytest.raises(PullStateFlagRemovedError):
            pull_state_rust.require_pull_state_env_unset()
        monkeypatch.delenv(pull_state_rust.PULL_STATE_RUST_ENV)
        pull_state_rust.require_pull_state_env_unset()


class TestEnvRefusalAtAccessors:
    """The refusal is checked at EVERY kernel resolve (first real use)."""

    @pytest.mark.parametrize("raw", ["1", "true", "0", "junk"])
    def test_set_knob_refuses_from_the_guard_accessor(
        self, monkeypatch: pytest.MonkeyPatch, raw: str
    ) -> None:
        """Any non-empty value refuses ``rust_pull_allowed`` outright."""
        monkeypatch.setenv(pull_state_rust.PULL_STATE_RUST_ENV, raw)
        with pytest.raises(PullStateFlagRemovedError):
            pull_state_rust.rust_pull_allowed(_DT, True)

    def test_refusal_precedes_wheel_resolution(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """SET knob + missing wheel ⇒ the FLAG error, not the wheel error."""
        monkeypatch.setenv(pull_state_rust.PULL_STATE_RUST_ENV, "true")
        _poison_import(monkeypatch)
        with pytest.raises(PullStateFlagRemovedError):
            pull_state_rust.rust_pull_allowed(_DT, True)

    def test_refusal_precedes_the_kernel_call(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A healthy stub is never consulted while the removed knob is set."""
        calls: list[tuple[Any, ...]] = []

        def recording(*args: Any) -> bool:
            calls.append(args)
            return True

        _install(monkeypatch, pull_allowed_us=recording)
        monkeypatch.setenv(pull_state_rust.PULL_STATE_RUST_ENV, "1")
        with pytest.raises(PullStateFlagRemovedError):
            pull_state_rust.rust_pull_allowed(_DT, True)
        assert calls == []

    @pytest.mark.parametrize(
        "accessor",
        [
            lambda: pull_state_rust.rust_scope_token("spot-only"),
            lambda: pull_state_rust.rust_alive_log_due(0.0, 200.0),
            lambda: pull_state_rust.rust_token_revalidates("a", "a"),
        ],
    )
    def test_other_accessors_refuse_too(
        self, monkeypatch: pytest.MonkeyPatch, accessor: Any
    ) -> None:
        """The refusal rides the ONE shared resolver — every accessor inherits it."""
        monkeypatch.setenv(pull_state_rust.PULL_STATE_RUST_ENV, "junk")
        with pytest.raises(PullStateFlagRemovedError):
            accessor()


# ── wheel missing / stale — the resolver's typed raises ──────────────────────


class TestWheelMissing:
    def test_missing_wheel_raises_chained_import_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``reason=wheel_missing`` chained ``from`` the real ImportError."""
        _poison_import(monkeypatch)
        with pytest.raises(PullStateKernelError) as excinfo:
            pull_state_rust.rust_pull_allowed(_DT, True)
        assert "wheel_missing" in str(excinfo.value)
        assert isinstance(excinfo.value.__cause__, ImportError)

    def test_missing_wheel_names_the_needed_symbol(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The error names which kernel symbol the caller needed."""
        _poison_import(monkeypatch)
        with pytest.raises(PullStateKernelError, match="census_fold"):
            pull_state_rust.rust_census_fold([], [])


class TestWheelStale:
    def test_stale_wheel_names_the_missing_symbol(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An importable wheel lacking the symbol ⇒ ``reason=wheel_stale``."""
        _install(monkeypatch)  # importable, but exposes nothing
        with pytest.raises(PullStateKernelError) as excinfo:
            pull_state_rust.rust_pull_allowed(_DT, True)
        message = str(excinfo.value)
        assert "wheel_stale" in message
        assert "pull_allowed_us" in message

    def test_stale_wheel_other_symbol(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The stale raise rides the ONE resolver for every accessor."""
        _install(monkeypatch, pull_allowed_us=lambda *a: True)  # phase-1-only shape
        with pytest.raises(PullStateKernelError, match="wedge_decision"):
            pull_state_rust.rust_wedge_decision(
                hung=True,
                already_fired=False,
                hook_bound=True,
                baseline_captured=True,
                last_beat_age_s=4000.0,
                wedge_window_s=3600.0,
                restart_age_s=None,
                baseline="a",
                current="a",
            )


# ── kernel raise → typed kernel_error; MemoryError/KeyboardInterrupt raw ─────


class TestKernelError:
    def test_raising_kernel_becomes_typed_error_with_cause(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``raise ... from exc``: the original raise is the ``__cause__``."""
        boom = ValueError("poisoned wheel")

        def raising(*_a: Any) -> bool:
            raise boom

        _install(monkeypatch, pull_allowed_us=raising)
        with pytest.raises(PullStateKernelError) as excinfo:
            pull_state_rust.rust_pull_allowed(_DT, True)
        message = str(excinfo.value)
        assert "kernel_error" in message
        assert "pull_allowed_us" in message
        assert "ValueError" in message
        assert excinfo.value.__cause__ is boom

    def test_memory_error_propagates_raw(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """MemoryError is never wrapped — the process-level pressure signal."""

        def oom(*_a: Any) -> bool:
            raise MemoryError

        _install(monkeypatch, alive_log_due=oom)
        with pytest.raises(MemoryError):
            pull_state_rust.rust_alive_log_due(0.0, 200.0)

    def test_keyboard_interrupt_propagates_raw(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """KeyboardInterrupt (BaseException) escapes the ``except Exception`` belt."""

        def interrupted(*_a: Any) -> bool:
            raise KeyboardInterrupt

        _install(monkeypatch, alive_log_due=interrupted)
        with pytest.raises(KeyboardInterrupt):
            pull_state_rust.rust_alive_log_due(0.0, 200.0)


# ── refuse-poison gates — one representative per validation family + full
#    per-accessor coverage of the raise arm ──────────────────────────────────


class TestKernelInvalid:
    def _expect_invalid(self, fn_name: str, call: Any) -> None:
        """Assert the typed ``kernel_invalid`` raise naming *fn_name*."""
        with pytest.raises(PullStateKernelError) as excinfo:
            call()
        message = str(excinfo.value)
        assert "kernel_invalid" in message
        assert fn_name in message

    def test_pull_allowed_bool_exact(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An int(1) is truthy AND == True — refused on TYPE, never served."""
        _install(monkeypatch, pull_allowed_us=lambda *a: 1)
        self._expect_invalid(
            "pull_allowed_us", lambda: pull_state_rust.rust_pull_allowed(_DT, True)
        )

    @pytest.mark.parametrize("poison", [True, False, 6, -1, "5", 2.0, None])
    def test_wedge_token_vocabulary(self, monkeypatch: pytest.MonkeyPatch, poison: Any) -> None:
        """int-not-bool inside the six-token vocabulary — everything else refused."""
        _install(monkeypatch, wedge_decision=lambda *a, _p=poison: _p)
        self._expect_invalid(
            "wedge_decision",
            lambda: pull_state_rust.rust_wedge_decision(
                hung=True,
                already_fired=False,
                hook_bound=True,
                baseline_captured=True,
                last_beat_age_s=4000.0,
                wedge_window_s=3600.0,
                restart_age_s=None,
                baseline="a",
                current="a",
            ),
        )

    @pytest.mark.parametrize("poison", [1, "x", None])
    def test_alive_log_due_bool_exact(self, monkeypatch: pytest.MonkeyPatch, poison: Any) -> None:
        """Bool-exact: int/str/None puns refused."""
        _install(monkeypatch, alive_log_due=lambda *a, _p=poison: _p)
        self._expect_invalid(
            "alive_log_due", lambda: pull_state_rust.rust_alive_log_due(0.0, 200.0)
        )

    @pytest.mark.parametrize(
        "poison",
        [("a", "b", "c"), ("a", "b", "c", 4), ["a", "b", "c", "d"], None],
    )
    def test_outcome_codes_arity_and_types(
        self, monkeypatch: pytest.MonkeyPatch, poison: Any
    ) -> None:
        """EXACTLY a 4-tuple of str — wrong arity / non-str member / list pun refused."""
        _install(monkeypatch, classify_pull_outcome=lambda *a, _p=poison: _p)
        self._expect_invalid(
            "classify_pull_outcome",
            lambda: pull_state_rust.rust_classify_pull_outcome("idle", "", 0, False, False, False),
        )

    def test_verdict_correlates_bool_exact(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A truthy int pun is refused."""
        _install(monkeypatch, verdict_correlates=lambda *a: 1)
        self._expect_invalid(
            "verdict_correlates", lambda: pull_state_rust.rust_verdict_correlates("a", "a")
        )

    @pytest.mark.parametrize("poison", [True, 3, -1, "0", None])
    def test_scope_token_vocabulary(self, monkeypatch: pytest.MonkeyPatch, poison: Any) -> None:
        """int-not-bool inside {0, 1, 2, 255} — a True pun must never read spot-only."""
        _install(monkeypatch, scope_token=lambda *a, _p=poison: _p)
        self._expect_invalid("scope_token", lambda: pull_state_rust.rust_scope_token("spot-only"))

    def test_note_rewrite_str_exact(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A bytes result is refused (str-exact)."""
        _install(monkeypatch, market_hours_note_rewrite=lambda *a: b"x")
        self._expect_invalid(
            "market_hours_note_rewrite",
            lambda: pull_state_rust.rust_market_hours_note_rewrite("note", "repl"),
        )

    @pytest.mark.parametrize(
        "poison",
        [("a", True), ("a", 1, True), ("a", True, 0), (1, True, True), ["a", True, True]],
    )
    def test_surgery_triple_shape(self, monkeypatch: pytest.MonkeyPatch, poison: Any) -> None:
        """EXACTLY (str, bool, bool) — arity/int-pun/list-pun refused on both surgeries."""
        _install(
            monkeypatch,
            spot_only_note_surgery=lambda *a, _p=poison: _p,
            fno_only_note_surgery=lambda *a, _p=poison: _p,
        )
        self._expect_invalid(
            "spot_only_note_surgery",
            lambda: pull_state_rust.rust_spot_only_note_surgery("note"),
        )
        self._expect_invalid(
            "fno_only_note_surgery",
            lambda: pull_state_rust.rust_fno_only_note_surgery("note"),
        )

    @pytest.mark.parametrize(
        "poison",
        [(True, False), (9, False), (0, 1), "x", (0,), None],
    )
    def test_auto_gate_pair_shape(self, monkeypatch: pytest.MonkeyPatch, poison: Any) -> None:
        """EXACTLY (int-not-bool in vocab, exact bool)."""
        _install(monkeypatch, auto_gate_decision=lambda *a, _p=poison: _p)
        self._expect_invalid(
            "auto_gate_decision",
            lambda: pull_state_rust.rust_auto_gate_decision(
                trading_day=True,
                coverage_available=True,
                muhurat_listed=False,
                now_us_of_day=_h(18),
                settle_us_of_day=_h(17, 30),
            ),
        )

    def test_muhurat_window_bool_exact(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A 0 pun is refused."""
        _install(monkeypatch, muhurat_window_active=lambda *a: 0)
        self._expect_invalid(
            "muhurat_window_active",
            lambda: pull_state_rust.rust_muhurat_window_active(0, 0, 0, 0, 0),
        )

    @pytest.mark.parametrize(
        "poison",
        [
            (0, 1),  # OK arm with an off-minute-grid value — the strict HH:MM law
            (0, -1),  # OK arm with an out-of-day value
            (9, -1),  # out-of-vocabulary verdict
            (0, "x"),  # non-int value slot
            (True, -1),  # bool-pun verdict
            "x",
        ],
    )
    def test_settle_verdict_gate(self, monkeypatch: pytest.MonkeyPatch, poison: Any) -> None:
        """The OK-arm value gate + vocabulary/type gates."""
        _install(monkeypatch, settle_token_verdict=lambda *a, _p=poison: _p)
        self._expect_invalid(
            "settle_token_verdict",
            lambda: pull_state_rust.rust_settle_token_verdict("17:30", _h(15, 35)),
        )

    @pytest.mark.parametrize("poison", [255, 9, True, None])
    def test_intent_shape_vocabulary(self, monkeypatch: pytest.MonkeyPatch, poison: Any) -> None:
        """The kernel's own 255 bad-input answer is refused, never a verdict."""
        _install(monkeypatch, intent_shape_verdict=lambda *a, _p=poison: _p)
        self._expect_invalid(
            "intent_shape_verdict",
            lambda: pull_state_rust.rust_intent_shape_verdict(1, 2, 1, 2, 2, 2, 3, 0, 2),
        )

    @pytest.mark.parametrize("poison", [9, True, None])
    def test_resume_arm_vocabulary(self, monkeypatch: pytest.MonkeyPatch, poison: Any) -> None:
        """int-not-bool inside the four-arm vocabulary."""
        _install(monkeypatch, intent_resume_decision=lambda *a, _p=poison: _p)
        self._expect_invalid(
            "intent_resume_decision",
            lambda: pull_state_rust.rust_intent_resume_decision(
                has_intent=True, probe_acquirable=True, restarts=0, max_restarts=3
            ),
        )

    def test_resume_no_intent_consistency_gate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """NO_INTENT is total in has_intent — a lattice-breaking wheel is refused."""
        _install(monkeypatch, intent_resume_decision=lambda *a: 0)  # NO_INTENT
        self._expect_invalid(
            "intent_resume_decision",
            lambda: pull_state_rust.rust_intent_resume_decision(
                has_intent=True, probe_acquirable=True, restarts=0, max_restarts=3
            ),
        )
        _install(monkeypatch, intent_resume_decision=lambda *a: 3)  # PROCEED
        self._expect_invalid(
            "intent_resume_decision",
            lambda: pull_state_rust.rust_intent_resume_decision(
                has_intent=False, probe_acquirable=True, restarts=0, max_restarts=3
            ),
        )

    @pytest.mark.parametrize(
        ("restarts", "max_restarts"),
        [(2**64, 3), (-1, 3), (0, 2**64), (0, -1)],
    )
    def test_resume_counter_domain_gate_never_calls_the_kernel(
        self, monkeypatch: pytest.MonkeyPatch, restarts: int, max_restarts: int
    ) -> None:
        """An out-of-u64 counter is refused BEFORE any kernel consult."""
        calls: list[tuple[Any, ...]] = []

        def recording(*args: Any) -> int:
            calls.append(args)
            return 3

        _install(monkeypatch, intent_resume_decision=recording)
        with pytest.raises(PullStateKernelError, match="u64 domain"):
            pull_state_rust.rust_intent_resume_decision(
                has_intent=True,
                probe_acquirable=True,
                restarts=restarts,
                max_restarts=max_restarts,
            )
        assert calls == []

    @pytest.mark.parametrize("poison", [9, True, None])
    def test_restore_arm_vocabulary(self, monkeypatch: pytest.MonkeyPatch, poison: Any) -> None:
        """int-not-bool inside the four-arm vocabulary."""
        _install(monkeypatch, intent_refused_restore_decision=lambda *a, _p=poison: _p)
        self._expect_invalid(
            "intent_refused_restore_decision",
            lambda: pull_state_rust.rust_intent_refused_restore_decision(
                dry_run=False, started=False, current_present=True, prior_present=True
            ),
        )

    def test_restore_no_action_consistency_gate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """NO_ACTION is total in ``dry_run or started`` — both directions gated."""
        _install(monkeypatch, intent_refused_restore_decision=lambda *a: 3)  # RESTORE_PRIOR
        self._expect_invalid(
            "intent_refused_restore_decision",
            lambda: pull_state_rust.rust_intent_refused_restore_decision(
                dry_run=True, started=False, current_present=True, prior_present=True
            ),
        )
        _install(monkeypatch, intent_refused_restore_decision=lambda *a: 0)  # NO_ACTION
        self._expect_invalid(
            "intent_refused_restore_decision",
            lambda: pull_state_rust.rust_intent_refused_restore_decision(
                dry_run=False, started=False, current_present=True, prior_present=True
            ),
        )

    def test_resume_enabled_bool_exact(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An int pun is refused, never served as truthy."""
        _install(monkeypatch, intent_resume_enabled=lambda *a: 1)
        self._expect_invalid(
            "intent_resume_enabled", lambda: pull_state_rust.rust_intent_resume_enabled(None)
        )

    @pytest.mark.parametrize(
        "poison",
        [
            "x",
            (1.0, 1.0, 1.0, True),  # wrong arity
            (1.0, 1.0, 1.0, 1, 1.0),  # int pun in the bool slot
            (True, 1.0, 1.0, True, 1.0),  # bool pun in a float slot
            (1, 1.0, 1.0, True, 1.0),  # int pun in a float slot
        ],
    )
    def test_progress_tuple_shape(self, monkeypatch: pytest.MonkeyPatch, poison: Any) -> None:
        """EXACTLY (float, float, float, bool, float)."""
        _install(monkeypatch, progress_derived=lambda *a, _p=poison: _p)
        self._expect_invalid(
            "progress_derived",
            lambda: pull_state_rust.rust_progress_derived(
                state_running=True,
                open_ended=False,
                total=100,
                completed=25,
                phase_base_completed=0,
                updated_at=50.0,
                started_at=0.0,
                phase_started_at=0.0,
            ),
        )

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"total": -1},
            {"total": 2**53 + 1},
            {"completed": True},  # bool pun in a count slot
            {"updated_at": 1},  # int-typed instant
            {"open_ended": 1},  # truthy pun
        ],
    )
    def test_progress_domain_gate_never_calls_the_kernel(
        self, monkeypatch: pytest.MonkeyPatch, kwargs: dict[str, Any]
    ) -> None:
        """An out-of-domain input is refused BEFORE any kernel consult."""
        calls: list[tuple[Any, ...]] = []

        def recording(*args: Any) -> tuple[float, float, float, bool, float]:
            calls.append(args)
            return (0.0, 0.0, 0.0, False, 0.0)

        _install(monkeypatch, progress_derived=recording)
        base: dict[str, Any] = {
            "state_running": True,
            "open_ended": False,
            "total": 100,
            "completed": 25,
            "phase_base_completed": 0,
            "updated_at": 50.0,
            "started_at": 0.0,
            "phase_started_at": 0.0,
        }
        base.update(kwargs)
        with pytest.raises(PullStateKernelError, match="modeled domain"):
            pull_state_rust.rust_progress_derived(**base)
        assert calls == []

    @pytest.mark.parametrize("poison", [6, True, None])
    def test_tick_code_vocabulary(self, monkeypatch: pytest.MonkeyPatch, poison: Any) -> None:
        """int-not-bool inside the six-token ladder vocabulary."""
        _install(monkeypatch, auto_tick_decision=lambda *a, _p=poison: _p)
        self._expect_invalid(
            "auto_tick_decision",
            lambda: pull_state_rust.rust_auto_tick_decision(
                armed=True,
                succeeded_today=False,
                attempted_today=False,
                gate_allowed=True,
                launcher_bound=True,
            ),
        )

    @pytest.mark.parametrize("poison", [255, 7, True, None])
    def test_state_code_vocabulary(self, monkeypatch: pytest.MonkeyPatch, poison: Any) -> None:
        """The kernel's 255 input-mismatch answer is refused."""
        _install(monkeypatch, auto_state_verdict=lambda *a, _p=poison: _p)
        self._expect_invalid(
            "auto_state_verdict",
            lambda: pull_state_rust.rust_auto_state_verdict(
                root_mapping=True,
                schema_kind=2,
                schema_i64=1,
                schema_f64_bits=0,
                armed_is_bool=True,
                armed_at_kind=2,
                last_attempt_kind=1,
                last_success_kind=0,
            ),
        )

    @pytest.mark.parametrize(
        "poison",
        [
            "x",  # non-tuple
            (1, 1),  # wrong arity
            (True, 1, []),  # bool-pun count
            (-1, 1, []),  # negative count
            (1, 1, "x"),  # non-sequence samples
            (1, 1, [(0, "s", "i")] * 21),  # over the sample cap
            (1, 1, [("x",)]),  # malformed sample item
            (1, 1, [(2, "s", "i")]),  # out-of-vocabulary sample kind
            (1, 1, [(True, "s", "i")]),  # bool-pun sample kind
            (1, 1, [(0, 1, "i")]),  # non-str symbol
            (1, 1, [(0, "s", 1)]),  # non-str interval
        ],
    )
    def test_census_fold_shape(self, monkeypatch: pytest.MonkeyPatch, poison: Any) -> None:
        """The whole nested fold gate — counts, cap, per-item triple."""
        _install(monkeypatch, census_fold=lambda *a, _p=poison: _p)
        self._expect_invalid("census_fold", lambda: pull_state_rust.rust_census_fold([], []))

    def test_reload_master_bool_exact(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A 1 pun is refused."""
        _install(monkeypatch, should_reload_master=lambda *a: 1)
        self._expect_invalid(
            "should_reload_master",
            lambda: pull_state_rust.rust_should_reload_master(
                index_count=3,
                stock_count=200,
                legs_present=False,
                stale_economics=False,
                stale_eligibility=False,
            ),
        )

    @pytest.mark.parametrize(
        ("index_count", "stock_count"), [(2**63, 0), (-(2**63) - 1, 0), (0, 2**63)]
    )
    def test_reload_master_i64_domain_gate_never_calls_the_kernel(
        self, monkeypatch: pytest.MonkeyPatch, index_count: int, stock_count: int
    ) -> None:
        """An out-of-i64 count is refused BEFORE any kernel consult."""
        calls: list[tuple[Any, ...]] = []

        def recording(*args: Any) -> bool:
            calls.append(args)
            return False

        _install(monkeypatch, should_reload_master=recording)
        with pytest.raises(PullStateKernelError, match="i64 domain"):
            pull_state_rust.rust_should_reload_master(
                index_count=index_count,
                stock_count=stock_count,
                legs_present=False,
                stale_economics=False,
                stale_eligibility=False,
            )
        assert calls == []

    @pytest.mark.parametrize(
        "poison",
        [(0,), (13, 0), (0, 3), (True, 0), (0, True), "x", None],
    )
    def test_bridge_pair_shape(self, monkeypatch: pytest.MonkeyPatch, poison: Any) -> None:
        """EXACTLY a 2-tuple of exact-int codes inside the served vocabularies."""
        _install(monkeypatch, bridge_payload_verdict=lambda *a, _p=poison: _p)
        self._expect_invalid(
            "bridge_payload_verdict",
            lambda: pull_state_rust.rust_bridge_payload_verdict(1, 2, 3, 0.0, [1] * 10, 2),
        )

    @pytest.mark.parametrize("poison", [3, -1, True, None])
    def test_memo_arm_vocabulary(self, monkeypatch: pytest.MonkeyPatch, poison: Any) -> None:
        """int-not-bool inside the three-arm vocabulary."""
        _install(monkeypatch, memo_serve_arm=lambda *a, _p=poison: _p)
        self._expect_invalid(
            "memo_serve_arm", lambda: pull_state_rust.rust_memo_serve_arm(None, 2.0, False)
        )

    def test_token_revalidates_bool_exact(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An int pun is refused."""
        _install(monkeypatch, token_revalidates=lambda *a: 1)
        self._expect_invalid(
            "token_revalidates", lambda: pull_state_rust.rust_token_revalidates("a", "a")
        )


# ── real-wheel happy paths — every accessor, hand-derived expectations ───────


class TestRealWheelHappyPaths:
    """Every accessor served by the REAL installed wheel.

    Each expectation is hand-derived from the documented law (never a probe
    echo alone): the guard window ``[08:45, 15:35)``, the wedge lattice, the
    outcome code tables, the note-surgery anchor literals, the gate ladder,
    the intent lattices, the f64-exact progress arithmetic, the tick ladder,
    the census set fold, the reload predicate, the bridge probe order and
    the memo/token laws.
    """

    def test_pull_allowed_boundaries(self) -> None:
        """[08:45, 15:35) blocked on a trading day; non-trading always allowed."""
        day = datetime(2026, 5, 7, tzinfo=_IST_LIKE)
        assert pull_state_rust.rust_pull_allowed(day.replace(hour=8, minute=44, second=59), True)
        assert pull_state_rust.rust_pull_allowed(day.replace(hour=8, minute=45), True) is False
        assert (
            pull_state_rust.rust_pull_allowed(
                day.replace(hour=15, minute=34, second=59, microsecond=999999), True
            )
            is False
        )
        assert pull_state_rust.rust_pull_allowed(day.replace(hour=15, minute=35), True) is True
        assert pull_state_rust.rust_pull_allowed(day.replace(hour=12), False) is True

    def test_wedge_decision_all_six_arms(self) -> None:
        """The whole wedge lattice, one hand-derived input per arm."""

        def wedge(**overrides: Any) -> int:
            kwargs: dict[str, Any] = {
                "hung": True,
                "already_fired": False,
                "hook_bound": True,
                "baseline_captured": True,
                "last_beat_age_s": 4000.0,
                "wedge_window_s": 3600.0,
                "restart_age_s": None,
                "baseline": "revA",
                "current": "revA",
            }
            kwargs.update(overrides)
            return pull_state_rust.rust_wedge_decision(**kwargs)

        # Not hung / already fired / no hook / age < window → no probe wanted.
        assert wedge(hung=False) == pull_state_rust.WEDGE_TOKEN_NO_PROBE
        assert wedge(already_fired=True) == pull_state_rust.WEDGE_TOKEN_NO_PROBE
        assert wedge(hook_bound=False) == pull_state_rust.WEDGE_TOKEN_NO_PROBE
        assert wedge(last_beat_age_s=1800.0) == pull_state_rust.WEDGE_TOKEN_NO_PROBE
        # The probe abstained on this poll (current None) → abstain.
        assert wedge(current=None) == pull_state_rust.WEDGE_TOKEN_ABSTAIN_PROBE_NONE
        # A captured-as-None hung-edge baseline + a later real token → adopt it.
        assert wedge(baseline=None, current="revB") == pull_state_rust.WEDGE_TOKEN_ADOPT_BASELINE
        # The token moved → the worker IS writing → re-baseline.
        assert wedge(current="revB") == pull_state_rust.WEDGE_TOKEN_REBASELINE
        # A restarted window whose freeze has not yet spanned it → defer.
        assert wedge(restart_age_s=100.0) == pull_state_rust.WEDGE_TOKEN_DEFER
        # Full silence, both DUAL halves confirmed → escalate.
        assert wedge() == pull_state_rust.WEDGE_TOKEN_ESCALATE
        assert wedge(restart_age_s=3600.0) == pull_state_rust.WEDGE_TOKEN_ESCALATE

    def test_alive_log_due_threshold(self) -> None:
        """Due iff the silence spans the interval (>=)."""
        assert pull_state_rust.rust_alive_log_due(199.9, 200.0) is False
        assert pull_state_rust.rust_alive_log_due(200.0, 200.0) is True
        assert pull_state_rust.rust_alive_log_due(250.0, 200.0) is True

    def test_classify_pull_outcome_code_table(self) -> None:
        """Representative rows of the outcome code table."""
        assert pull_state_rust.rust_classify_pull_outcome("idle", "", 0, False, False, False) == (
            "never_run",
            "idle",
            "never_run",
            "empty",
        )
        assert pull_state_rust.rust_classify_pull_outcome(
            "running", "", 0, False, False, False
        ) == ("running", "ok", "running", "input")
        assert pull_state_rust.rust_classify_pull_outcome("done", "", 0, False, False, False) == (
            "completed",
            "ok",
            "completed",
            "completed",
        )
        assert pull_state_rust.rust_classify_pull_outcome("done", "", 3, False, False, False) == (
            "completed_with_failures",
            "warn",
            "completed_failures",
            "input",
        )
        assert pull_state_rust.rust_classify_pull_outcome(
            "error", "boom", 0, False, False, False
        ) == ("error", "bad", "failed", "input")

    def test_verdict_correlates_equality_law(self) -> None:
        """Correlates iff both ids are real and equal."""
        assert pull_state_rust.rust_verdict_correlates("abc", "abc") is True
        assert pull_state_rust.rust_verdict_correlates("abc", "def") is False
        assert pull_state_rust.rust_verdict_correlates("", "") is False

    def test_scope_token_vocabulary(self) -> None:
        """None/"" → full(0); the two scoped tokens; junk → 255 (caller refuses)."""
        assert pull_state_rust.rust_scope_token(None) == 0
        assert pull_state_rust.rust_scope_token("") == 0
        assert pull_state_rust.rust_scope_token("spot-only") == 1
        assert pull_state_rust.rust_scope_token("fno-only") == 2
        assert pull_state_rust.rust_scope_token("junk") == pull_state_rust.SCOPE_CODE_UNKNOWN
        # Deliberately case/whitespace-EXACT — normalisation stays the caller's.
        assert pull_state_rust.rust_scope_token(" SPOT-ONLY ") == pull_state_rust.SCOPE_CODE_UNKNOWN

    def test_market_hours_note_rewrite_anchors(self) -> None:
        """The three anchored clauses rewritten; a marker-less note passes through."""
        note = (
            "deferred — inside NSE market-hours window (08:45-15:35 IST) "
            "(spot / metadata / historical skipped to keep the shared Groww "
            "account free for the live feed); resume on the next out-of-hours run"
        )
        replacement = "(spot deferred; F&O not applicable — spot-only scope)"
        assert pull_state_rust.rust_market_hours_note_rewrite(note, replacement) == (
            "deferred — market-session protection window active "
            "(NSE hours 08:45-15:35 IST or a Muhurat session) "
            "(spot deferred; F&O not applicable — spot-only scope); "
            "resume once the protection window clears"
        )
        assert pull_state_rust.rust_market_hours_note_rewrite("plain note", "R") == "plain note"

    def test_spot_only_note_surgery_anchors(self) -> None:
        """Prefix + no-op F&O clause replaced; a drifted note reads (False, False)."""
        base = (
            "pipeline complete — Pulled 12 symbols, metadata 0 expiries + "
            "0 contracts, historical 0 bars over 0 contract(s)"
        )
        assert pull_state_rust.rust_spot_only_note_surgery(base) == (
            "spot-only pipeline complete — Pulled 12 symbols, "
            "F&O skipped by design (spot-only scope)",
            True,
            True,
        )
        assert pull_state_rust.rust_spot_only_note_surgery("some unrelated note") == (
            "some unrelated note",
            False,
            False,
        )

    def test_fno_only_note_surgery_anchors(self) -> None:
        """Prefix + no-op spot clause replaced on the fno axis."""
        base = "pipeline complete — spot clean (0 pair(s)), metadata 2 expiries + 3 contracts"
        assert pull_state_rust.rust_fno_only_note_surgery(base) == (
            "F&O-only pipeline complete — spot skipped by design (F&O-only scope), "
            "metadata 2 expiries + 3 contracts",
            True,
            True,
        )

    def test_auto_gate_ladder(self) -> None:
        """The ladder order: non-trading → muhurat → before-settle → window → ok."""
        assert pull_state_rust.rust_auto_gate_decision(
            trading_day=False,
            coverage_available=True,
            muhurat_listed=False,
            now_us_of_day=_h(18),
            settle_us_of_day=_h(17, 30),
        ) == (pull_state_rust.GATE_CODE_NON_TRADING_DAY, False)
        assert pull_state_rust.rust_auto_gate_decision(
            trading_day=True,
            coverage_available=True,
            muhurat_listed=True,
            now_us_of_day=_h(18),
            settle_us_of_day=_h(17, 30),
        ) == (pull_state_rust.GATE_CODE_MUHURAT_DEFERRAL, False)
        assert pull_state_rust.rust_auto_gate_decision(
            trading_day=True,
            coverage_available=True,
            muhurat_listed=False,
            now_us_of_day=_h(17, 29, 59),
            settle_us_of_day=_h(17, 30),
        ) == (pull_state_rust.GATE_CODE_BEFORE_SETTLE, False)
        # A (kernel-legal) pre-window settle exposes the market-hours arm.
        assert pull_state_rust.rust_auto_gate_decision(
            trading_day=True,
            coverage_available=True,
            muhurat_listed=False,
            now_us_of_day=_h(12),
            settle_us_of_day=_h(9),
        ) == (pull_state_rust.GATE_CODE_MARKET_HOURS_BLOCKED, False)
        assert pull_state_rust.rust_auto_gate_decision(
            trading_day=True,
            coverage_available=True,
            muhurat_listed=False,
            now_us_of_day=_h(18),
            settle_us_of_day=_h(17, 30),
        ) == (pull_state_rust.GATE_CODE_OK, False)
        # Coverage exhausted on a (fail-safe) trading weekday → warn + proceed.
        assert pull_state_rust.rust_auto_gate_decision(
            trading_day=True,
            coverage_available=False,
            muhurat_listed=False,
            now_us_of_day=_h(18),
            settle_us_of_day=_h(17, 30),
        ) == (pull_state_rust.GATE_CODE_OK, True)

    def test_muhurat_window_margined_boundaries(self) -> None:
        """[open − 30 min, close + 5 min) for an 18:15-19:15 session."""
        pre, post = 30 * 60 * _US, 5 * 60 * _US
        open_us, close_us = _h(18, 15), _h(19, 15)
        assert pull_state_rust.rust_muhurat_window_active(_h(17, 45), open_us, close_us, pre, post)
        assert (
            pull_state_rust.rust_muhurat_window_active(_h(17, 44, 59), open_us, close_us, pre, post)
            is False
        )
        assert pull_state_rust.rust_muhurat_window_active(
            _h(19, 20) - 1, open_us, close_us, pre, post
        )
        assert (
            pull_state_rust.rust_muhurat_window_active(_h(19, 20), open_us, close_us, pre, post)
            is False
        )

    def test_settle_token_verdicts(self) -> None:
        """The strict HH:MM law + the ≥ BLOCK_END floor (15:35 exact allowed)."""
        block_end = _h(15, 35)
        assert pull_state_rust.rust_settle_token_verdict("17:30", block_end) == (
            pull_state_rust.SETTLE_CODE_OK,
            _h(17, 30),
        )
        assert pull_state_rust.rust_settle_token_verdict("15:35", block_end) == (
            pull_state_rust.SETTLE_CODE_OK,
            _h(15, 35),
        )
        assert pull_state_rust.rust_settle_token_verdict("15:34", block_end) == (3, -1)
        assert pull_state_rust.rust_settle_token_verdict("9:05", block_end) == (1, -1)
        assert pull_state_rust.rust_settle_token_verdict("17:30:00", block_end) == (1, -1)
        assert pull_state_rust.rust_settle_token_verdict("banana", block_end) == (1, -1)
        assert pull_state_rust.rust_settle_token_verdict("24:00", block_end) == (2, -1)

    def test_intent_shape_verdicts(self) -> None:
        """Valid → 0; not-mapping → 1; the first violated field in probe order."""
        assert (
            pull_state_rust.rust_intent_shape_verdict(1, 2, 1, 2, 2, 2, 3, 0, 2)
            == pull_state_rust.INTENT_SHAPE_VALID
        )
        assert pull_state_rust.rust_intent_shape_verdict(0, 0, 0, 0, 0, 0, 0, 0, 0) == 1
        assert (
            pull_state_rust.rust_intent_shape_verdict(
                1, pull_state_rust.INTENT_KIND_OTHER, 1, 2, 2, 2, 3, 0, 2
            )
            == 2
        )

    def test_intent_resume_arms(self) -> None:
        """The four-arm resume lattice."""
        assert (
            pull_state_rust.rust_intent_resume_decision(
                has_intent=False, probe_acquirable=True, restarts=0, max_restarts=3
            )
            == pull_state_rust.RESUME_ARM_NO_INTENT
        )
        assert (
            pull_state_rust.rust_intent_resume_decision(
                has_intent=True, probe_acquirable=False, restarts=0, max_restarts=3
            )
            == pull_state_rust.RESUME_ARM_PEER_ACTIVE
        )
        assert (
            pull_state_rust.rust_intent_resume_decision(
                has_intent=True, probe_acquirable=True, restarts=3, max_restarts=3
            )
            == pull_state_rust.RESUME_ARM_OVER_CAP
        )
        assert (
            pull_state_rust.rust_intent_resume_decision(
                has_intent=True, probe_acquirable=True, restarts=2, max_restarts=3
            )
            == pull_state_rust.RESUME_ARM_PROCEED
        )

    def test_intent_restore_arms(self) -> None:
        """The four-arm refused-start restore lattice."""
        assert (
            pull_state_rust.rust_intent_refused_restore_decision(
                dry_run=True, started=False, current_present=True, prior_present=True
            )
            == pull_state_rust.RESTORE_ARM_NO_ACTION
        )
        assert (
            pull_state_rust.rust_intent_refused_restore_decision(
                dry_run=False, started=True, current_present=True, prior_present=True
            )
            == pull_state_rust.RESTORE_ARM_NO_ACTION
        )
        assert (
            pull_state_rust.rust_intent_refused_restore_decision(
                dry_run=False, started=False, current_present=False, prior_present=True
            )
            == pull_state_rust.RESTORE_ARM_LEAVE_ABSENT
        )
        assert (
            pull_state_rust.rust_intent_refused_restore_decision(
                dry_run=False, started=False, current_present=True, prior_present=False
            )
            == pull_state_rust.RESTORE_ARM_CLEAR_ORPHAN
        )
        assert (
            pull_state_rust.rust_intent_refused_restore_decision(
                dry_run=False, started=False, current_present=True, prior_present=True
            )
            == pull_state_rust.RESTORE_ARM_RESTORE_PRIOR
        )

    def test_intent_resume_enabled_token_law(self) -> None:
        """Default-ON; only the explicit falsy set disables (strip+lower)."""
        assert pull_state_rust.rust_intent_resume_enabled(None) is True
        assert pull_state_rust.rust_intent_resume_enabled("") is True
        assert pull_state_rust.rust_intent_resume_enabled("0") is False
        assert pull_state_rust.rust_intent_resume_enabled("off") is False
        assert pull_state_rust.rust_intent_resume_enabled(" FALSE ") is False
        assert pull_state_rust.rust_intent_resume_enabled("1") is True
        assert pull_state_rust.rust_intent_resume_enabled("junk") is True

    def test_progress_derived_arithmetic(self) -> None:
        """25/100 done over 50 s → 25%, 0.5 u/s, ETA (100−25)/0.5 = 150 s."""
        assert pull_state_rust.rust_progress_derived(
            state_running=True,
            open_ended=False,
            total=100,
            completed=25,
            phase_base_completed=0,
            updated_at=50.0,
            started_at=0.0,
            phase_started_at=0.0,
        ) == (25.0, 50.0, 0.5, 150.0)
        # Not running → no ETA (the eta_present fold → None).
        assert pull_state_rust.rust_progress_derived(
            state_running=False,
            open_ended=False,
            total=100,
            completed=25,
            phase_base_completed=0,
            updated_at=50.0,
            started_at=0.0,
            phase_started_at=0.0,
        ) == (25.0, 50.0, 0.5, None)
        # Open-ended → percent pinned 0, no ETA.
        assert pull_state_rust.rust_progress_derived(
            state_running=True,
            open_ended=True,
            total=0,
            completed=25,
            phase_base_completed=0,
            updated_at=50.0,
            started_at=0.0,
            phase_started_at=0.0,
        ) == (0.0, 50.0, 0.5, None)

    def test_auto_tick_ladder(self) -> None:
        """The six-token ladder, one arm per input."""
        assert (
            pull_state_rust.rust_auto_tick_decision(
                armed=False,
                succeeded_today=False,
                attempted_today=False,
                gate_allowed=True,
                launcher_bound=True,
            )
            == pull_state_rust.TICK_CODE_DISARMED
        )
        assert (
            pull_state_rust.rust_auto_tick_decision(
                armed=True,
                succeeded_today=True,
                attempted_today=False,
                gate_allowed=True,
                launcher_bound=True,
            )
            == pull_state_rust.TICK_CODE_ALREADY_SUCCEEDED
        )
        assert (
            pull_state_rust.rust_auto_tick_decision(
                armed=True,
                succeeded_today=False,
                attempted_today=True,
                gate_allowed=True,
                launcher_bound=True,
            )
            == pull_state_rust.TICK_CODE_ALREADY_ATTEMPTED
        )
        assert (
            pull_state_rust.rust_auto_tick_decision(
                armed=True,
                succeeded_today=False,
                attempted_today=False,
                gate_allowed=False,
                launcher_bound=True,
            )
            == pull_state_rust.TICK_CODE_DEFERRED
        )
        assert (
            pull_state_rust.rust_auto_tick_decision(
                armed=True,
                succeeded_today=False,
                attempted_today=False,
                gate_allowed=True,
                launcher_bound=False,
            )
            == pull_state_rust.TICK_CODE_LAUNCHER_UNBOUND
        )
        assert (
            pull_state_rust.rust_auto_tick_decision(
                armed=True,
                succeeded_today=False,
                attempted_today=False,
                gate_allowed=True,
                launcher_bound=True,
            )
            == pull_state_rust.TICK_CODE_FIRE
        )

    def test_auto_state_verdicts(self) -> None:
        """Valid → 0; not-object → 1; a non-1 schema → 2."""
        assert (
            pull_state_rust.rust_auto_state_verdict(
                root_mapping=True,
                schema_kind=pull_state_rust.SCHEMA_KIND_INT,
                schema_i64=1,
                schema_f64_bits=0,
                armed_is_bool=True,
                armed_at_kind=pull_state_rust.STATE_FIELD_STR,
                last_attempt_kind=pull_state_rust.STATE_FIELD_NULL,
                last_success_kind=pull_state_rust.STATE_FIELD_ABSENT,
            )
            == pull_state_rust.STATE_CODE_VALID
        )
        assert (
            pull_state_rust.rust_auto_state_verdict(
                root_mapping=False,
                schema_kind=0,
                schema_i64=0,
                schema_f64_bits=0,
                armed_is_bool=False,
                armed_at_kind=0,
                last_attempt_kind=0,
                last_success_kind=0,
            )
            == pull_state_rust.STATE_CODE_NOT_OBJECT
        )
        assert (
            pull_state_rust.rust_auto_state_verdict(
                root_mapping=True,
                schema_kind=pull_state_rust.SCHEMA_KIND_INT,
                schema_i64=2,
                schema_f64_bits=0,
                armed_is_bool=True,
                armed_at_kind=pull_state_rust.STATE_FIELD_STR,
                last_attempt_kind=pull_state_rust.STATE_FIELD_NULL,
                last_success_kind=pull_state_rust.STATE_FIELD_ABSENT,
            )
            == pull_state_rust.STATE_CODE_BAD_SCHEMA
        )

    def test_census_fold_set_difference(self) -> None:
        """settled {A, C} vs written {B, C}: A settled-unwritten, B written-unsettled."""
        rows = [("NSE-A", "1minute", True), ("NSE-B", "1minute", False), ("NSE-C", "1minute", True)]
        written = [("NSE-B", "1minute"), ("NSE-C", "1minute")]
        assert pull_state_rust.rust_census_fold(rows, written) == (
            1,
            1,
            (
                (pull_state_rust.SAMPLE_KIND_SETTLED_UNWRITTEN, "NSE-A", "1minute"),
                (pull_state_rust.SAMPLE_KIND_WRITTEN_UNSETTLED, "NSE-B", "1minute"),
            ),
        )

    @staticmethod
    def _reload(
        *,
        index_count: int = 3,
        stock_count: int = 200,
        legs_present: bool = False,
        stale_economics: bool = False,
        stale_eligibility: bool = False,
    ) -> bool:
        """Call the reload kernel with healthy defaults, overriding one axis."""
        return pull_state_rust.rust_should_reload_master(
            index_count=index_count,
            stock_count=stock_count,
            legs_present=legs_present,
            stale_economics=stale_economics,
            stale_eligibility=stale_eligibility,
        )

    def test_should_reload_master_predicate(self) -> None:
        """Reload iff empty/missing-index/non-lean/stale economics/eligibility."""
        assert self._reload() is False
        assert self._reload(index_count=0) is True
        assert self._reload(legs_present=True) is True
        assert self._reload(stale_economics=True) is True

    def test_bridge_payload_verdicts(self) -> None:
        """Valid → (0, pa_arm echo); not-mapping → 1; a bad 10th field → code 12."""
        assert pull_state_rust.rust_bridge_payload_verdict(1, 2, 3, 0.0, [1] * 10, 2) == (
            pull_state_rust.BRIDGE_VERDICT_VALID,
            pull_state_rust.BRIDGE_PA_TRUE,
        )
        assert pull_state_rust.rust_bridge_payload_verdict(0, 0, 0, 0.0, [0] * 10, 0) == (
            1,
            pull_state_rust.BRIDGE_PA_NONE,
        )
        assert pull_state_rust.rust_bridge_payload_verdict(1, 2, 3, 0.0, [1] * 9 + [2], 1) == (
            12,
            pull_state_rust.BRIDGE_PA_FALSE,
        )

    def test_memo_serve_arms(self) -> None:
        """Never-probed/expired → owner (follower while inflight); fresh → hit."""
        assert (
            pull_state_rust.rust_memo_serve_arm(None, 2.0, False) == pull_state_rust.MEMO_ARM_OWNER
        )
        assert (
            pull_state_rust.rust_memo_serve_arm(None, 2.0, True)
            == pull_state_rust.MEMO_ARM_FOLLOWER
        )
        assert (
            pull_state_rust.rust_memo_serve_arm(1.0, 2.0, False)
            == pull_state_rust.MEMO_ARM_CACHED_HIT
        )
        assert (
            pull_state_rust.rust_memo_serve_arm(2.0, 2.0, False) == pull_state_rust.MEMO_ARM_OWNER
        )
        assert (
            pull_state_rust.rust_memo_serve_arm(2.0, 2.0, True) == pull_state_rust.MEMO_ARM_FOLLOWER
        )

    def test_token_revalidates_law(self) -> None:
        """Keep the memo iff both tokens are real and equal."""
        assert pull_state_rust.rust_token_revalidates("t1", "t1") is True
        assert pull_state_rust.rust_token_revalidates("t1", "t2") is False
        assert pull_state_rust.rust_token_revalidates(None, "t1") is False
        assert pull_state_rust.rust_token_revalidates(None, None) is False


# ── module surface — the flip's public contract ──────────────────────────────


class TestModuleSurface:
    def test_deleted_flag_machinery_is_gone(self) -> None:
        """The flag/warn-once/duplicate-resolver machinery is DELETED, not shimmed."""
        for name in (
            "pull_state_rust_enabled",
            "warn_degrade_once",
            "reset_for_tests",
            "resolve_pull_state_kernel",
            "_resolve_wheel_symbol",
            "PULL_STATE_RUST_FALLBACK_EVENT",
            "PULL_STATE_RUST_FLAG_EVENT",
        ):
            assert not hasattr(pull_state_rust, name), name

    def test_all_exports_resolve_and_are_unique(self) -> None:
        """``__all__`` is duplicate-free and every name resolves."""
        assert len(pull_state_rust.__all__) == len(set(pull_state_rust.__all__))
        for name in pull_state_rust.__all__:
            assert hasattr(pull_state_rust, name), name
        for required in (
            "PullStateFlagRemovedError",
            "PullStateKernelError",
            "require_pull_state_env_unset",
        ):
            assert required in pull_state_rust.__all__

    def test_every_rust_accessor_is_exported(self) -> None:
        """Every public ``rust_*`` accessor appears in ``__all__``."""
        accessors = {
            name
            for name in dir(pull_state_rust)
            if name.startswith("rust_") and callable(getattr(pull_state_rust, name))
        }
        assert accessors <= set(pull_state_rust.__all__)
        assert len(accessors) == 24


# ── wheel-shim handshake pin (the wheel is REQUIRED — no importorskip) ───────


class TestWheelHandshake:
    def test_expected_build_json_mirrors_shim_symbols(self) -> None:
        """EXPECTED_BUILD.json == the shim's non-dunder handshake surface."""
        expected_path = (
            Path(__file__).resolve().parents[2]
            / "rust"
            / "brutex-pull-state"
            / "python"
            / "brutex_pull_state"
            / "EXPECTED_BUILD.json"
        )
        fingerprint = json.loads(expected_path.read_text(encoding="utf-8"))
        shim_symbols = sorted(
            brutex_pull_state._EXPECTED_COMPILED_SYMBOLS  # pyright: ignore[reportPrivateUsage]
        )
        assert fingerprint["symbols"] == shim_symbols
        assert fingerprint["crate_version"] == brutex_pull_state.__build_crate_version__

    def test_kernel_out_of_range_raise_becomes_typed_error(self) -> None:
        """The kernel's own ValueError surfaces as ``kernel_error`` at the facade.

        ``rust_pull_allowed`` can never pack an out-of-range instant from a
        ``datetime`` (max 86_399_999_999 µs), so the kernel refusal is pinned
        at the RAW symbol + through a direct ``_call_kernel``-backed accessor
        stub-free: the raw kernel raises ValueError; the facade would wrap it.
        """
        with pytest.raises(ValueError, match="out of range"):
            brutex_pull_state.pull_allowed_us(86_400_000_000, True)
        with pytest.raises(ValueError, match="out of range"):
            brutex_pull_state.pull_allowed_us(-1, True)
