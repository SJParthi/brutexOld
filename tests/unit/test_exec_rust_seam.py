"""Unit tests for the flag-gated Rust exec-cores seam (``brutex/exec_rust.py``).

DEC-EXEC-RUST-CORE-001 (MIGRATION.md #2212). Covers the ``BRUTEX_EXEC_RUST``
tristate resolution, the structural OFF-never-touches-the-kernel proof, the
wheel-missing / envelope / narrow-catch fallbacks (with ``MemoryError``
re-raising bare), the wire-arity refusal, the kernel-vs-Python-oracle
equality of every wrapper, and the ENGAGEMENT of the real dispatch sites
(``compute_trade_cost`` / ``compute_futures_trade_cost``) through a counting
kernel proxy — every line + branch, for the strict standalone 100% gate.

Since DEC-RUST-DEFAULT-ON-WAVE-001 (MIGRATION.md #2215) the seam default is
ON — an unset flag ENGAGES the kernel; OFF is the explicit kill switch
(``"0"``/``"off"``). The OFF-arm == TODAY byte-identity pin lives here as
:func:`test_off_arm_is_todays_python_bytes`: the OFF arm (both explicit kill
-switch spellings) is proven to run the verbatim Python oracle with the
kernel structurally unreachable (a BOMB kernel object) — combined with the
UNMODIFIED frozen golden suites passing on a ``"0"``-armed run, that is the
"OFF arm == today's bytes" license the default flip requires.
"""

from __future__ import annotations

from collections.abc import Callable, Generator, Iterator
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import fno_math
import pytest
from brutex import exec_rust
from brutex.costs import constants as C
from brutex.costs.calculator import (
    ChargeStack,
    _apply_slippage,  # pyright: ignore[reportPrivateUsage] — the oracle arm
    _charge_stack_py,  # pyright: ignore[reportPrivateUsage] — the oracle arm
    compute_trade_cost,
)
from brutex.costs.futures_costs import (
    FuturesCostBreakdown,
    FuturesTradeInput,
    _futures_charge_stack_py,  # pyright: ignore[reportPrivateUsage] — the oracle arm
    apply_futures_fill,
    compute_futures_trade_cost,
)
from brutex.costs.types import TradeCostBreakdown, TradeInput
from structlog.testing import capture_logs

# ── canonical sample arguments (parity is vs the Python oracle, so the
#    values only need to be diverse, not market-realistic) ────────────────

_OPTION_ARGS: dict[str, Any] = {
    "is_long": True,
    "lot_size": 65,
    "n_lots": 2,
    "brokerage_round_trip_paisa": 4_000,
    "stt_rate": 15_000,
    "etc_rate": 3_503,
    "sebi_rate": 10,
    "ipft_rate": 50,
    "stamp_rate": 300,
    "gst_rate": 1_800_000,
}

_FUTURES_ARGS: dict[str, Any] = {
    "is_long": True,
    "lot_size": 500,
    "n_lots": 1,
    "cost_free": False,
    "brokerage_flat_paisa": 2_000,
    "brokerage_turnover_bps_x100": 3_000,
    "stt_rate": 2_000,
    "etc_rate": 173,
    "sebi_rate": 10,
    "ipft_rate": 10,
    "stamp_rate": 200,
    "gst_rate": 1_800_000,
}


@pytest.fixture(autouse=True)
def _strict_off_degrade_lattice() -> Iterator[None]:
    """This module is the *degrade-lattice* suite — pin ``BRUTEX_RUST_STRICT=0``.

    DEC-RUST-STRICT-NO-FALLBACK-001 ships strict mode ON by default, so every
    CLASS-A (``wheel_missing``, ``flag_unrecognised``) and CLASS-B
    (``kernel:<site>`` — the kernel raised, or returned a malformed wire)
    reason now RAISES ``RustFallbackRefused`` instead of quietly serving the
    verbatim Python oracle. This module exists to exercise that oracle arm —
    the still-supported ``BRUTEX_RUST_STRICT=0`` mode — so the mode is
    DECLARED once, here, rather than by weakening any assertion. Strict-ON
    refusal is pinned separately in ``TestStrictRefusal`` at the end of this
    module.

    The patch runs in its OWN :class:`pytest.MonkeyPatch` context, not the
    shared ``monkeypatch`` fixture, so ``TestStrictRefusal``'s nested
    ``delenv`` can never record ``"0"`` as a value to restore across fixture
    teardown (the shipped ``test_rust_stats`` shape).
    """
    from brutex.config import rust_strict

    with pytest.MonkeyPatch.context() as mp:
        mp.setenv(rust_strict.RUST_STRICT_ENV, "0")
        yield


@pytest.fixture(autouse=True)
def _fresh_seam(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Each test sees a fresh warn-once memo and an UNSET flag (which, since
    DEC-RUST-DEFAULT-ON-WAVE-001 (#2215), reveals the true production default
    — ON). Every OFF-arm/bomb-kernel test explicitly arms ``"0"``/``"off"``;
    every ON-arm test explicitly arms ``"1"``."""
    exec_rust.reset_warn_memo_for_tests()
    monkeypatch.delenv(exec_rust.ENV_FLAG, raising=False)
    yield
    exec_rust.reset_warn_memo_for_tests()


class _BombKernel:
    """A kernel stand-in that RECORDS + refuses ANY attribute access —
    the structural proof that a code path never touches the kernel.

    ``__getattr__`` raises :class:`AttributeError` (the special method's
    standard contract — a non-AttributeError there breaks ``getattr``/
    ``hasattr`` semantics and trips CodeQL's non-standard-exception check),
    but the access is recorded in :attr:`touched` FIRST, and every
    must-not-touch test asserts ``touched == []`` — so even an
    AttributeError swallowed by a defaulted ``getattr(rust, name, None)``
    probe can never let a kernel touch pass silently.
    """

    def __init__(self) -> None:
        """Start with an empty access record."""
        self.touched: list[str] = []

    def __getattr__(self, name: str) -> Any:
        """Record the access, then refuse it with the standard exception."""
        self.touched.append(name)
        raise AttributeError(f"kernel touched via {name!r} on a must-not-touch path")


def _raising_kernel(fn_name: str, exc: BaseException) -> SimpleNamespace:
    """A kernel stand-in whose *fn_name* raises *exc* when called."""

    def _boom(*_a: object, **_k: object) -> object:
        raise exc

    return SimpleNamespace(**{fn_name: _boom})


# ── flag resolution ─────────────────────────────────────────────────────


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", "  On  "])
def test_flag_truthy_values_enable(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(exec_rust.ENV_FLAG, value)
    assert exec_rust.exec_rust_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "FALSE", "no", "off", "  Off  "])
def test_flag_falsy_values_disable(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(exec_rust.ENV_FLAG, value)
    assert exec_rust.exec_rust_enabled() is False


def test_flag_unset_is_on_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default is ON since DEC-RUST-DEFAULT-ON-WAVE-001 (#2215): the
    exec-cost kernel is ENGAGED by default (the one-constant lever is pinned
    here). delenv removes the conftest belt so the true default shows."""
    monkeypatch.delenv(exec_rust.ENV_FLAG, raising=False)
    assert exec_rust.EXEC_RUST_DEFAULT is True
    assert exec_rust.exec_rust_enabled() is True


def test_env_flag_literal_is_pinned(monkeypatch: pytest.MonkeyPatch) -> None:
    """The operator-facing env var NAME is the pinned literal — the tests
    above address it via ``exec_rust.ENV_FLAG``, which a renamed-flag mutant
    would follow self-consistently; this literal pin kills that class."""
    assert exec_rust.ENV_FLAG == "BRUTEX_EXEC_RUST"
    monkeypatch.setenv("BRUTEX_EXEC_RUST", "1")
    assert exec_rust.exec_rust_enabled() is True


@pytest.mark.parametrize("value", ["", "   "])
def test_flag_whitespace_follows_the_default(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(exec_rust.ENV_FLAG, value)
    assert exec_rust.exec_rust_enabled() is exec_rust.EXEC_RUST_DEFAULT


def test_flag_unrecognised_fails_safe_off_with_one_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unrecognised value resolves OFF with exactly ONE
    ``exec_rust.flag_unrecognised`` warning (warn-once memo)."""
    monkeypatch.setenv(exec_rust.ENV_FLAG, "garbage")
    with capture_logs() as logs:
        assert exec_rust.exec_rust_enabled() is False
        assert exec_rust.exec_rust_enabled() is False  # warn-once, still OFF
    events = [e for e in logs if e["event"] == "exec_rust.flag_unrecognised"]
    assert len(events) == 1
    assert events[0]["reason"] == "flag_unrecognised"
    assert events[0]["value"] == "garbage"
    assert events[0]["resolved"] == "off"


# ── OFF arm: structurally kernel-free ───────────────────────────────────


def _call_every_wrapper() -> list[object]:
    """Invoke all six seam wrappers once with in-envelope arguments."""
    return [
        exec_rust.fill_slippage(9_995, 9_995, 12_005, 12_005, is_long=True),
        exec_rust.fill_futures(9_995, 9_995, 12_005, 12_005, is_long=False),
        exec_rust.charge_stack_option(9_995, 9_995, 12_005, 12_005, **_OPTION_ARGS),
        exec_rust.charge_stack_futures(2_201_000, 2_199_000, 2_211_000, 2_209_000, **_FUTURES_ARGS),
        exec_rust.materialize_option_tail(
            12_000,
            15_000,
            12_000,
            12_000,
            15_000,
            15_000,
            entry_volume=10,
            exit_volume=10,
            **_OPTION_ARGS,
        ),
        exec_rust.materialize_futures_tail(
            2_201_000,
            2_199_000,
            2_211_000,
            2_209_000,
            entry_volume=10,
            exit_volume=10,
            path_highs_paisa=[2_201_000],
            path_lows_paisa=[2_199_000],
            **_FUTURES_ARGS,
        ),
    ]


@pytest.mark.parametrize("flag", ["0", "off"])
def test_off_arm_never_touches_the_kernel(monkeypatch: pytest.MonkeyPatch, flag: str) -> None:
    """OFF (the explicit kill switch, both spellings ``"0"``/``"off"``):
    every wrapper returns ``None`` BEFORE any kernel attribute access
    (bomb-kernel structural proof) and emits ZERO warnings. Since
    DEC-RUST-DEFAULT-ON-WAVE-001 (#2215) an UNSET flag engages the crate, so
    OFF is the explicit kill switch — never the unset default."""
    monkeypatch.setenv(exec_rust.ENV_FLAG, flag)
    bomb = _BombKernel()
    monkeypatch.setattr(exec_rust, "_rust", bomb)
    with capture_logs() as logs:
        results = _call_every_wrapper()
    assert results == [None] * 6
    assert logs == []
    assert bomb.touched == []


# ── wheel-missing fallback ──────────────────────────────────────────────


def test_wheel_missing_falls_back_with_one_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(exec_rust.ENV_FLAG, "1")
    monkeypatch.setattr(exec_rust, "_rust", None)
    with capture_logs() as logs:
        results = _call_every_wrapper()
    assert results == [None] * 6
    events = [e for e in logs if e["event"] == "exec_rust.fallback"]
    assert len(events) == 1  # warn-once across all six wrappers
    assert events[0]["reason"] == "wheel_missing"


# ── stale-wheel skew: LOUD named error, never a silent fallback ─────────


def test_stale_wheel_missing_exec_symbol_raises_named_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wheel that IMPORTS fine but lacks the ``exec_*`` symbols (a wheel
    built before ``src/exec/`` landed) raises the NAMED
    ``StaleExecWheelError`` carrying the rebuild remediation — LOUD, never
    a bare ``AttributeError`` and NEVER a silent Python-arm fallback
    (review round 1, H3-NIT-2)."""
    monkeypatch.setenv(exec_rust.ENV_FLAG, "1")
    monkeypatch.setattr(exec_rust, "_rust", SimpleNamespace())  # no exec_* symbols
    with pytest.raises(exec_rust.StaleExecWheelError) as excinfo:
        exec_rust.fill_slippage(9_995, 9_995, 12_005, 12_005, is_long=True)
    # EXACT message pin — kills any string-literal mutant of the message
    # or of the resolved symbol name.
    assert str(excinfo.value) == (
        "fno_math wheel predates the exec module (exec_apply_slippage missing) "
        "— rebuild the wheel: pip install ./rust/fno-math"
    )
    # RuntimeError subclass: OUTSIDE every narrow catch on the dispatch
    # path, so it always propagates (loud by construction).
    assert isinstance(excinfo.value, RuntimeError)


def test_stale_wheel_raises_at_every_wrapper(monkeypatch: pytest.MonkeyPatch) -> None:
    """All six wrappers resolve through the SAME ``_kernel_fn`` guard —
    none can silently fall back (or bare-AttributeError) on a stale wheel."""
    monkeypatch.setenv(exec_rust.ENV_FLAG, "1")
    monkeypatch.setattr(exec_rust, "_rust", SimpleNamespace())
    for _site, call in sorted(_in_envelope_calls().items()):
        with pytest.raises(exec_rust.StaleExecWheelError, match="pip install"):
            call()


# ── envelope fallback ───────────────────────────────────────────────────

_TOO_BIG = 2**63  # one past i64::MAX
_TOO_SMALL = -(2**63) - 1  # one past i64::MIN

_OPTION_ARGS_BIG: dict[str, Any] = {**_OPTION_ARGS, "n_lots": _TOO_BIG}
_FUTURES_ARGS_BIG: dict[str, Any] = {**_FUTURES_ARGS, "brokerage_turnover_bps_x100": _TOO_BIG}


def _envelope_calls() -> dict[str, Callable[[], object]]:
    return {
        "fill_slippage": lambda: exec_rust.fill_slippage(
            _TOO_BIG, 9_995, 12_005, 12_005, is_long=True
        ),
        "fill_futures": lambda: exec_rust.fill_futures(
            9_995, 9_995, _TOO_SMALL, 12_005, is_long=True
        ),
        "charge_stack_option": lambda: exec_rust.charge_stack_option(
            9_995, 9_995, 12_005, 12_005, **_OPTION_ARGS_BIG
        ),
        "charge_stack_futures": lambda: exec_rust.charge_stack_futures(
            2_201_000, 2_199_000, 2_211_000, 2_209_000, **_FUTURES_ARGS_BIG
        ),
        "materialize_option_tail": lambda: exec_rust.materialize_option_tail(
            _TOO_BIG,
            15_000,
            _TOO_BIG,
            _TOO_BIG,
            15_000,
            15_000,
            entry_volume=10,
            exit_volume=10,
            **_OPTION_ARGS,
        ),
        # The path LISTS are part of the futures envelope — a single
        # out-of-envelope path element must route to the Python arm.
        "materialize_futures_tail": lambda: exec_rust.materialize_futures_tail(
            2_201_000,
            2_199_000,
            2_211_000,
            2_209_000,
            entry_volume=10,
            exit_volume=10,
            path_highs_paisa=[2_201_000, _TOO_BIG],
            path_lows_paisa=[2_199_000, 2_199_000],
            **_FUTURES_ARGS,
        ),
    }


@pytest.mark.parametrize("site", sorted(_envelope_calls()))
def test_envelope_fallback_per_site(monkeypatch: pytest.MonkeyPatch, site: str) -> None:
    """An argument outside the i64 envelope → ``None`` + one warn-once
    ``exec_rust.envelope_fallback`` naming the site — the kernel is never
    called (bomb kernel)."""
    monkeypatch.setenv(exec_rust.ENV_FLAG, "1")
    bomb = _BombKernel()
    monkeypatch.setattr(exec_rust, "_rust", bomb)
    call = _envelope_calls()[site]
    with capture_logs() as logs:
        assert call() is None
        assert call() is None  # warn-once per site
    events = [e for e in logs if e["event"] == "exec_rust.envelope_fallback"]
    assert len(events) == 1
    assert events[0]["site"] == site
    assert events[0]["reason"] == f"envelope:{site}"
    assert bomb.touched == []


def test_exact_i64_boundaries_are_in_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    """Values EXACTLY at i64::MIN / i64::MAX are IN-envelope — they must
    reach the kernel (a raising stand-in proves the call happened: the
    fallback is ``kernel_fallback``, never ``envelope_fallback``) — the
    exact-boundary teeth for the ``<=`` comparisons in ``_fits_i64``."""
    monkeypatch.setenv(exec_rust.ENV_FLAG, "1")
    monkeypatch.setattr(
        exec_rust, "_rust", _raising_kernel("exec_apply_slippage", ValueError("reached"))
    )
    for boundary in (2**63 - 1, -(2**63)):
        exec_rust.reset_warn_memo_for_tests()
        with capture_logs() as logs:
            assert exec_rust.fill_slippage(boundary, boundary, 12_005, 12_005, is_long=True) is None
        events = {e["event"] for e in logs}
        assert events == {"exec_rust.kernel_fallback"}, boundary


def test_futures_tail_turnover_none_served_and_present_enveloped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The futures-tail turnover splice both ways: ``None`` is a LEGAL
    kernel argument (the Zerodha flat-only arm — served, oracle-equal),
    while a PRESENT out-of-envelope turnover routes to the Python arm via
    the envelope guard (never a kernel call)."""
    monkeypatch.setenv(exec_rust.ENV_FLAG, "1")

    def tail(turnover: int | None) -> tuple[str, ChargeStack | None, int, int] | None:
        args: dict[str, Any] = {**_FUTURES_ARGS, "brokerage_turnover_bps_x100": turnover}
        return exec_rust.materialize_futures_tail(
            2_201_000,
            2_199_000,
            2_211_000,
            2_209_000,
            entry_volume=10,
            exit_volume=10,
            path_highs_paisa=[2_201_000],
            path_lows_paisa=[2_199_000],
            **args,
        )

    served = tail(None)
    assert served is not None
    kind, stack, _mae, _mfe = served
    assert kind == "trade"
    none_args: dict[str, Any] = {**_FUTURES_ARGS, "brokerage_turnover_bps_x100": None}
    assert stack == _futures_charge_stack_py(
        2_201_000, 2_199_000, 2_211_000, 2_209_000, **none_args
    )
    bomb = _BombKernel()
    monkeypatch.setattr(exec_rust, "_rust", bomb)
    with capture_logs() as logs:
        assert tail(_TOO_BIG) is None
    assert [e["event"] for e in logs] == ["exec_rust.envelope_fallback"]
    assert bomb.touched == []


def test_warn_once_dedupes_per_event_reason_pair(monkeypatch: pytest.MonkeyPatch) -> None:
    """TWO distinct (event, reason) keys in ONE memo window each warn once —
    the memo never collapses distinct keys onto one entry."""
    monkeypatch.setenv(exec_rust.ENV_FLAG, "1")
    bomb = _BombKernel()
    monkeypatch.setattr(exec_rust, "_rust", bomb)
    calls = _envelope_calls()
    with capture_logs() as logs:
        assert calls["fill_slippage"]() is None
        assert calls["fill_futures"]() is None
    events = [e for e in logs if e["event"] == "exec_rust.envelope_fallback"]
    assert [e["reason"] for e in events] == ["envelope:fill_slippage", "envelope:fill_futures"]
    assert bomb.touched == []


# ── narrow kernel-call catch + MemoryError bare re-raise ────────────────

_WRAPPER_TO_KERNEL_FN: dict[str, str] = {
    "fill_slippage": "exec_apply_slippage",
    "fill_futures": "exec_apply_futures_fill",
    "charge_stack_option": "exec_option_charge_stack",
    "charge_stack_futures": "exec_futures_charge_stack",
    "materialize_option_tail": "exec_options_materialize",
    "materialize_futures_tail": "exec_futures_materialize",
}


def _in_envelope_calls() -> dict[str, Callable[[], object]]:
    return {
        "fill_slippage": lambda: exec_rust.fill_slippage(
            9_995, 9_995, 12_005, 12_005, is_long=True
        ),
        "fill_futures": lambda: exec_rust.fill_futures(9_995, 9_995, 12_005, 12_005, is_long=True),
        "charge_stack_option": lambda: exec_rust.charge_stack_option(
            9_995, 9_995, 12_005, 12_005, **_OPTION_ARGS
        ),
        "charge_stack_futures": lambda: exec_rust.charge_stack_futures(
            2_201_000, 2_199_000, 2_211_000, 2_209_000, **_FUTURES_ARGS
        ),
        "materialize_option_tail": lambda: exec_rust.materialize_option_tail(
            12_000,
            15_000,
            12_000,
            12_000,
            15_000,
            15_000,
            entry_volume=10,
            exit_volume=10,
            **_OPTION_ARGS,
        ),
        "materialize_futures_tail": lambda: exec_rust.materialize_futures_tail(
            2_201_000,
            2_199_000,
            2_211_000,
            2_209_000,
            entry_volume=10,
            exit_volume=10,
            path_highs_paisa=[2_201_000],
            path_lows_paisa=[2_199_000],
            **_FUTURES_ARGS,
        ),
    }


@pytest.mark.parametrize("site", sorted(_WRAPPER_TO_KERNEL_FN))
def test_kernel_error_falls_back_narrowly(monkeypatch: pytest.MonkeyPatch, site: str) -> None:
    """A kernel ``ValueError`` → ``None`` + one ``exec_rust.kernel_fallback``
    naming the site + error type (the narrow catch)."""
    monkeypatch.setenv(exec_rust.ENV_FLAG, "1")
    monkeypatch.setattr(
        exec_rust, "_rust", _raising_kernel(_WRAPPER_TO_KERNEL_FN[site], ValueError("kernel says"))
    )
    with capture_logs() as logs:
        assert _in_envelope_calls()[site]() is None
    events = [e for e in logs if e["event"] == "exec_rust.kernel_fallback"]
    assert len(events) == 1
    assert events[0]["site"] == site
    assert events[0]["reason"] == f"kernel:{site}"
    assert events[0]["error_type"] == "ValueError"


@pytest.mark.parametrize("site", sorted(_WRAPPER_TO_KERNEL_FN))
def test_memory_error_reraises_bare(monkeypatch: pytest.MonkeyPatch, site: str) -> None:
    """``MemoryError`` is a global stop — re-raised BARE, never swallowed
    into the fallback arm."""
    monkeypatch.setenv(exec_rust.ENV_FLAG, "1")
    monkeypatch.setattr(
        exec_rust, "_rust", _raising_kernel(_WRAPPER_TO_KERNEL_FN[site], MemoryError())
    )
    with pytest.raises(MemoryError):
        _in_envelope_calls()[site]()


# ── kernel wire / verdict SHAPE guard (review round 2 —
#    R2-MINOR/R3-F2/R3-F3: every kernel-returned wire is validated for
#    length AND element type BEFORE any indexing; ANY violation → the same
#    warn-once loud fallback → the Python arm — never an IndexError crash,
#    never a silent adoption, never a zero-log executor swallow) ──────────


def _futures_tail_call() -> object:
    return exec_rust.materialize_futures_tail(
        2_201_000,
        2_199_000,
        2_211_000,
        2_209_000,
        entry_volume=10,
        exit_volume=10,
        path_highs_paisa=[2_201_000],
        path_lows_paisa=[2_199_000],
        **_FUTURES_ARGS,
    )


def _option_tail_call() -> object:
    return exec_rust.materialize_option_tail(
        12_000,
        15_000,
        12_000,
        12_000,
        15_000,
        15_000,
        entry_volume=10,
        exit_volume=10,
        **_OPTION_ARGS,
    )


class TestKernelWireShapeGuard:
    """A malformed kernel wire — wrong length, wrong element TYPE, junk
    verdict kind, broken kind↔wire pairing — routes through the seam's
    :func:`_wire_fault`/:func:`_verdict_fault` validation to ONE warn-once
    ``exec_rust.kernel_fallback`` (``error_type="KernelWireFault"``) and the
    ``None`` Python-arm sentinel, on EVERY wire the seam consumes.

    Review round 2 regressions: a 17/18-slot futures wire raised an
    UNCAUGHT ``IndexError`` past the seam catch AND the executor catch
    (R2-MINOR/R3-F2); a ≥20-slot wire was silently truncated-adopted; a
    right-length wire of junk elements was silently adopted whole (R2
    probe C); and the options wrong-arity ``TypeError`` escaped the seam
    un-warned into the options executor's broad catch — a ZERO-log silent
    fallback (R3-F3).
    """

    @staticmethod
    def _assert_one_wire_fallback(logs: list[Any], site: str) -> None:
        events = [e for e in logs if e["event"] == "exec_rust.kernel_fallback"]
        assert len(events) == 1
        assert events[0]["site"] == site
        assert events[0]["reason"] == f"kernel:{site}"
        assert events[0]["error_type"] == "KernelWireFault"

    @pytest.mark.parametrize("n", [16, 18, 20])
    @pytest.mark.parametrize(
        ("site", "kernel_fn", "call"),
        [
            (
                "charge_stack_option",
                "exec_option_charge_stack",
                lambda: exec_rust.charge_stack_option(9_995, 9_995, 12_005, 12_005, **_OPTION_ARGS),
            ),
            (
                "charge_stack_futures",
                "exec_futures_charge_stack",
                lambda: exec_rust.charge_stack_futures(
                    2_201_000, 2_199_000, 2_211_000, 2_209_000, **_FUTURES_ARGS
                ),
            ),
        ],
        ids=["option-stack", "futures-stack"],
    )
    def test_stack_wire_wrong_length_falls_back(
        self,
        monkeypatch: pytest.MonkeyPatch,
        site: str,
        kernel_fn: str,
        call: Callable[[], object],
        n: int,
    ) -> None:
        monkeypatch.setenv(exec_rust.ENV_FLAG, "1")
        fake = SimpleNamespace(**{kernel_fn: lambda *_a: list(range(n))})
        monkeypatch.setattr(exec_rust, "_rust", fake)
        with capture_logs() as logs:
            assert call() is None
        self._assert_one_wire_fallback(logs, site)
        assert logs[0]["error"] == f"wire length {n} != the pinned 17"

    @pytest.mark.parametrize(
        ("site", "kernel_fn", "call"),
        [
            (
                "charge_stack_option",
                "exec_option_charge_stack",
                lambda: exec_rust.charge_stack_option(9_995, 9_995, 12_005, 12_005, **_OPTION_ARGS),
            ),
            (
                "charge_stack_futures",
                "exec_futures_charge_stack",
                lambda: exec_rust.charge_stack_futures(
                    2_201_000, 2_199_000, 2_211_000, 2_209_000, **_FUTURES_ARGS
                ),
            ),
        ],
        ids=["option-stack", "futures-stack"],
    )
    def test_stack_wire_right_length_wrong_type_falls_back(
        self,
        monkeypatch: pytest.MonkeyPatch,
        site: str,
        kernel_fn: str,
        call: Callable[[], object],
    ) -> None:
        """R2 probe C: a right-LENGTH wire of chars was silently adopted
        whole (17 junk elements through ``ChargeStack(*wire)``) — now a
        counted type fault → the Python arm."""
        monkeypatch.setenv(exec_rust.ENV_FLAG, "1")
        fake = SimpleNamespace(**{kernel_fn: lambda *_a: list("abcdefghijklmnopq")})
        monkeypatch.setattr(exec_rust, "_rust", fake)
        with capture_logs() as logs:
            assert call() is None
        self._assert_one_wire_fallback(logs, site)
        assert logs[0]["error"] == "wire[0] is str, expected int"

    @pytest.mark.parametrize("n", [16, 18, 20])
    def test_option_tail_wire_wrong_length_falls_back(
        self, monkeypatch: pytest.MonkeyPatch, n: int
    ) -> None:
        """The R3-F3 seam-side shape: pre-fix, ``_stack_from_wire(wire)``
        sat OUTSIDE the try, so the ``TypeError`` escaped the seam with no
        warn and the options executor's broad catch swallowed it silently."""
        monkeypatch.setenv(exec_rust.ENV_FLAG, "1")
        fake = SimpleNamespace(exec_options_materialize=lambda *_a: ("trade", list(range(n))))
        monkeypatch.setattr(exec_rust, "_rust", fake)
        with capture_logs() as logs:
            assert _option_tail_call() is None
        self._assert_one_wire_fallback(logs, "materialize_option_tail")

    def test_option_tail_right_length_wrong_type_falls_back(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(exec_rust.ENV_FLAG, "1")
        fake = SimpleNamespace(
            exec_options_materialize=lambda *_a: ("trade", list("abcdefghijklmnopq"))
        )
        monkeypatch.setattr(exec_rust, "_rust", fake)
        with capture_logs() as logs:
            assert _option_tail_call() is None
        self._assert_one_wire_fallback(logs, "materialize_option_tail")

    @pytest.mark.parametrize("n", [16, 17, 18, 20])
    def test_futures_tail_wire_wrong_length_falls_back_never_crashes(
        self, monkeypatch: pytest.MonkeyPatch, n: int
    ) -> None:
        """The R2-MINOR/R3-F2 regression matrix: 17/18 slots previously
        raised an UNCAUGHT ``IndexError`` (a raw crash out of
        ``materialize_futures_trade``); 20 slots was silently
        truncated-adopted; 16 was the one shape round 1 actually fixed."""
        monkeypatch.setenv(exec_rust.ENV_FLAG, "1")
        fake = SimpleNamespace(exec_futures_materialize=lambda *_a: ("trade", list(range(n))))
        monkeypatch.setattr(exec_rust, "_rust", fake)
        with capture_logs() as logs:
            assert _futures_tail_call() is None
        self._assert_one_wire_fallback(logs, "materialize_futures_tail")
        assert logs[0]["error"] == f"wire length {n} != the pinned 19"

    def test_futures_tail_right_length_wrong_type_falls_back(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(exec_rust.ENV_FLAG, "1")
        fake = SimpleNamespace(
            exec_futures_materialize=lambda *_a: ("trade", list("abcdefghijklmnopqrs"))
        )
        monkeypatch.setattr(exec_rust, "_rust", fake)
        with capture_logs() as logs:
            assert _futures_tail_call() is None
        self._assert_one_wire_fallback(logs, "materialize_futures_tail")

    @pytest.mark.parametrize(
        ("site", "kernel_fn", "call", "bad"),
        [
            (
                "fill_slippage",
                "exec_apply_slippage",
                lambda: exec_rust.fill_slippage(9_995, 9_995, 12_005, 12_005, is_long=True),
                (1, 2),
            ),
            (
                "fill_futures",
                "exec_apply_futures_fill",
                lambda: exec_rust.fill_futures(9_995, 9_995, 12_005, 12_005, is_long=True),
                (1, 2, "x"),
            ),
        ],
        ids=["fill-slippage-short", "fill-futures-wrong-type"],
    )
    def test_fill_wire_shape_faults_fall_back(
        self,
        monkeypatch: pytest.MonkeyPatch,
        site: str,
        kernel_fn: str,
        call: Callable[[], object],
        bad: object,
    ) -> None:
        monkeypatch.setenv(exec_rust.ENV_FLAG, "1")
        fake = SimpleNamespace(**{kernel_fn: lambda *_a: bad})
        monkeypatch.setattr(exec_rust, "_rust", fake)
        with capture_logs() as logs:
            assert call() is None
        self._assert_one_wire_fallback(logs, site)

    @pytest.mark.parametrize(
        ("verdict", "error"),
        [
            (("bogus", None), "unknown verdict kind 'bogus'"),
            (("trade", None), "verdict ('trade', None) is missing its stack wire"),
            (("stale_bar", list(range(19))), "verdict kind 'stale_bar' carries an unexpected wire"),
            ((["unhashable"], None), "unknown verdict kind ['unhashable']"),
            (("trade", "not-a-list"), "wire is str, expected a list or tuple"),
        ],
        ids=[
            "unknown-kind",
            "trade-without-wire",
            "reject-with-wire",
            "unhashable-kind",
            "str-wire",
        ],
    )
    def test_futures_verdict_contract_faults_fall_back(
        self, monkeypatch: pytest.MonkeyPatch, verdict: object, error: str
    ) -> None:
        monkeypatch.setenv(exec_rust.ENV_FLAG, "1")
        fake = SimpleNamespace(exec_futures_materialize=lambda *_a: verdict)
        monkeypatch.setattr(exec_rust, "_rust", fake)
        with capture_logs() as logs:
            assert _futures_tail_call() is None
        self._assert_one_wire_fallback(logs, "materialize_futures_tail")
        assert logs[0]["error"] == error

    @pytest.mark.parametrize(
        "verdict",
        [("bogus", None), ("trade", None), ("tick_floor", list(range(17)))],
        ids=["unknown-kind", "trade-without-wire", "reject-with-wire"],
    )
    def test_option_verdict_contract_faults_fall_back(
        self, monkeypatch: pytest.MonkeyPatch, verdict: object
    ) -> None:
        monkeypatch.setenv(exec_rust.ENV_FLAG, "1")
        fake = SimpleNamespace(exec_options_materialize=lambda *_a: verdict)
        monkeypatch.setattr(exec_rust, "_rust", fake)
        with capture_logs() as logs:
            assert _option_tail_call() is None
        self._assert_one_wire_fallback(logs, "materialize_option_tail")

    def test_bool_wire_element_is_a_type_fault(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``bool`` is an ``int`` subclass but NOT a legal ``Vec<i64>``
        element — refused as a type fault, never adopted."""
        monkeypatch.setenv(exec_rust.ENV_FLAG, "1")
        wire = [*range(3), True, *range(4, 17)]
        fake = SimpleNamespace(exec_option_charge_stack=lambda *_a: wire)
        monkeypatch.setattr(exec_rust, "_rust", fake)
        with capture_logs() as logs:
            assert (
                exec_rust.charge_stack_option(9_995, 9_995, 12_005, 12_005, **_OPTION_ARGS) is None
            )
        self._assert_one_wire_fallback(logs, "charge_stack_option")
        assert logs[0]["error"] == "wire[3] is bool, expected int"

    @pytest.mark.parametrize("junk", [2**64, -(2**63) - 1], ids=["past-i64-max", "below-i64-min"])
    def test_out_of_i64_range_wire_element_falls_back_options(
        self, monkeypatch: pytest.MonkeyPatch, junk: int
    ) -> None:
        """rA round-2 NIT-1: an int-TYPED element outside the i64 range in a
        right-length wire was silently ADOPTED whole (a junk net=2**64
        breakdown served with zero warns — the last int-shaped adoption
        channel). The real kernel wire is ``Vec<i64>``, so an out-of-range
        int is the same build/contract skew as a wrong element type: now a
        counted range fault → warn-once → the byte-identical OFF-arm value
        at the REAL ``compute_trade_cost`` dispatch."""
        trade = _option_trade()
        monkeypatch.setenv(exec_rust.ENV_FLAG, "0")
        off = compute_trade_cost(trade)
        exec_rust.reset_warn_memo_for_tests()
        monkeypatch.setenv(exec_rust.ENV_FLAG, "1")
        fake = SimpleNamespace(exec_option_charge_stack=lambda *_a: [junk] * 17)
        monkeypatch.setattr(exec_rust, "_rust", fake)
        with capture_logs() as logs:
            assert (
                exec_rust.charge_stack_option(9_995, 9_995, 12_005, 12_005, **_OPTION_ARGS) is None
            )
            assert compute_trade_cost(trade) == off
        self._assert_one_wire_fallback(logs, "charge_stack_option")
        assert logs[0]["error"] == "wire[0] is outside the i64 range"

    @pytest.mark.parametrize("junk", [2**64, -(2**63) - 1], ids=["past-i64-max", "below-i64-min"])
    def test_out_of_i64_range_wire_element_falls_back_futures(
        self, monkeypatch: pytest.MonkeyPatch, junk: int
    ) -> None:
        """The futures twin — an out-of-i64-range element in a right-length
        19-slot trade wire is a warn-once range fault → the Python arm."""
        monkeypatch.setenv(exec_rust.ENV_FLAG, "1")
        fake = SimpleNamespace(exec_futures_materialize=lambda *_a: ("trade", [junk] * 19))
        monkeypatch.setattr(exec_rust, "_rust", fake)
        with capture_logs() as logs:
            assert _futures_tail_call() is None
        self._assert_one_wire_fallback(logs, "materialize_futures_tail")
        assert logs[0]["error"] == "wire[0] is outside the i64 range"

    def test_malformed_wire_at_the_real_dispatch_equals_the_python_arm(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Dispatch-level value law: a malformed-wire kernel under the REAL
        ``compute_trade_cost`` serves the byte-identical OFF-arm breakdown
        (pre-fix this path CRASHED raw — the calculator dispatch has no
        outer catch)."""
        trade = _option_trade()
        monkeypatch.setenv(exec_rust.ENV_FLAG, "0")
        off = compute_trade_cost(trade)
        exec_rust.reset_warn_memo_for_tests()
        monkeypatch.setenv(exec_rust.ENV_FLAG, "1")
        fake = SimpleNamespace(exec_option_charge_stack=lambda *_a: list(range(20)))
        monkeypatch.setattr(exec_rust, "_rust", fake)
        with capture_logs() as logs:
            on = compute_trade_cost(trade)
        assert on == off
        self._assert_one_wire_fallback(logs, "charge_stack_option")


# ── ON arm: kernel == Python oracle, wrapper by wrapper ─────────────────


@pytest.mark.parametrize("is_long", [True, False])
@pytest.mark.parametrize(
    ("entry", "exit_"), [(9_995, 12_005), (5, 5), (7, 6), (25, 5), (12_000, 15_000)]
)
def test_fill_slippage_matches_python_oracle(
    monkeypatch: pytest.MonkeyPatch, entry: int, exit_: int, is_long: bool
) -> None:
    monkeypatch.setenv(exec_rust.ENV_FLAG, "1")
    got = exec_rust.fill_slippage(entry, entry, exit_, exit_, is_long=is_long)
    assert got == _apply_slippage(
        "long" if is_long else "short",
        entry_high_paisa=entry,
        entry_low_paisa=entry,
        exit_high_paisa=exit_,
        exit_low_paisa=exit_,
    )


@pytest.mark.parametrize("is_long", [True, False])
@pytest.mark.parametrize(
    ("entry", "exit_"), [(9_995, 12_005), (5, 5), (7, 6), (25, 5), (2_200_000, 2_210_000)]
)
def test_fill_futures_matches_python_oracle(
    monkeypatch: pytest.MonkeyPatch, entry: int, exit_: int, is_long: bool
) -> None:
    monkeypatch.setenv(exec_rust.ENV_FLAG, "1")
    got = exec_rust.fill_futures(entry, entry, exit_, exit_, is_long=is_long)
    assert got == apply_futures_fill(
        side="long" if is_long else "short",
        entry_high_paisa=entry,
        entry_low_paisa=entry,
        exit_high_paisa=exit_,
        exit_low_paisa=exit_,
    )


@pytest.mark.parametrize("is_long", [True, False])
def test_charge_stack_option_matches_python_oracle(
    monkeypatch: pytest.MonkeyPatch, is_long: bool
) -> None:
    monkeypatch.setenv(exec_rust.ENV_FLAG, "1")
    args = {**_OPTION_ARGS, "is_long": is_long}
    got = exec_rust.charge_stack_option(9_995, 9_995, 12_005, 12_005, **args)
    assert isinstance(got, ChargeStack)
    assert got == _charge_stack_py(9_995, 9_995, 12_005, 12_005, **args)


@pytest.mark.parametrize(
    "overrides",
    [
        {},  # groww min(flat, turnover)
        {"brokerage_turnover_bps_x100": None},  # zerodha flat-only arm
        {"cost_free": True, "stt_rate": 0, "etc_rate": 0, "ipft_rate": 0, "stamp_rate": 0},
        {"is_long": False},
    ],
)
def test_charge_stack_futures_matches_python_oracle(
    monkeypatch: pytest.MonkeyPatch, overrides: dict[str, Any]
) -> None:
    monkeypatch.setenv(exec_rust.ENV_FLAG, "1")
    args = {**_FUTURES_ARGS, **overrides}
    got = exec_rust.charge_stack_futures(2_201_000, 2_199_000, 2_211_000, 2_209_000, **args)
    assert isinstance(got, ChargeStack)
    assert got == _futures_charge_stack_py(2_201_000, 2_199_000, 2_211_000, 2_209_000, **args)


def test_materialize_option_tail_verdicts(monkeypatch: pytest.MonkeyPatch) -> None:
    """stale_bar / tick_floor / trade verdicts, matching the Python laws:
    stale = zero volume on either bar, UNCONDITIONAL
    (DEC-FILL-WORST-CASE-ONLY-001 — the ``reject_stale`` knob is deleted);
    tick floor = entry OPEN ≤ 5×tick (25 paisa) OR exit OPEN < 1 tick;
    trade = the oracle stack over the fill bars' extremes (flat bars here,
    extremes == opens)."""
    monkeypatch.setenv(exec_rust.ENV_FLAG, "1")

    def tail(entry: int, exit_: int, ev: int, xv: int) -> object:
        return exec_rust.materialize_option_tail(
            entry,
            exit_,
            entry,
            entry,
            exit_,
            exit_,
            entry_volume=ev,
            exit_volume=xv,
            **_OPTION_ARGS,
        )

    assert tail(12_000, 15_000, 0, 10) == ("stale_bar", None)
    assert tail(12_000, 15_000, 10, 0) == ("stale_bar", None)
    assert tail(12_000, 15_000, 0, 0) == ("stale_bar", None)  # unconditional
    assert tail(25, 15_000, 10, 10) == ("tick_floor", None)  # entry ≤ 25 paisa
    assert tail(26, 4, 10, 10) == ("tick_floor", None)  # exit < one tick
    # Stale precedes the tick floor (the pinned gate order).
    assert tail(25, 15_000, 0, 10) == ("stale_bar", None)
    expected = _charge_stack_py(12_000, 12_000, 15_000, 15_000, **_OPTION_ARGS)
    assert tail(12_000, 15_000, 10, 10) == ("trade", expected)


def test_materialize_futures_tail_verdicts(monkeypatch: pytest.MonkeyPatch) -> None:
    """stale_bar / trade verdicts; MAE/MFE = per-unit running max floored at
    0 over (high, low), then × n_shares — the executor ``_excursions_paisa``
    twin (basis fill = buy_fill for a long)."""
    monkeypatch.setenv(exec_rust.ENV_FLAG, "1")

    def tail(ev: int, xv: int) -> object:
        return exec_rust.materialize_futures_tail(
            2_201_000,
            2_199_000,
            2_211_000,
            2_209_000,
            entry_volume=ev,
            exit_volume=xv,
            path_highs_paisa=[2_201_000, 2_208_000],
            path_lows_paisa=[2_199_000, 2_204_000],
            **_FUTURES_ARGS,
        )

    assert tail(0, 10) == ("stale_bar", None, 0, 0)
    assert tail(10, 0) == ("stale_bar", None, 0, 0)
    expected = _futures_charge_stack_py(2_201_000, 2_199_000, 2_211_000, 2_209_000, **_FUTURES_ARGS)
    n_shares = _FUTURES_ARGS["lot_size"] * _FUTURES_ARGS["n_lots"]
    entry_fill = expected.buy_fill_paisa  # long basis
    mae_unit = max(0, entry_fill - 2_199_000, entry_fill - 2_204_000)
    mfe_unit = max(0, 2_201_000 - entry_fill, 2_208_000 - entry_fill)
    assert tail(10, 10) == ("trade", expected, mae_unit * n_shares, mfe_unit * n_shares)
    # UNCONDITIONAL stale (DEC-FILL-WORST-CASE-ONLY-001): both bars stale
    # rejects — there is no opt-out arm any more.
    assert tail(0, 0) == ("stale_bar", None, 0, 0)


# ── engagement through the REAL dispatch sites ──────────────────────────


class _CountingKernel:
    """Delegates to the real ``fno_math`` while counting per-entry calls —
    the anti-vacuous engagement spy."""

    def __init__(self) -> None:
        self.calls: dict[str, int] = {}

    def __getattr__(self, name: str) -> Any:
        real = getattr(fno_math, name)

        def _wrapped(*a: object, **k: object) -> object:
            self.calls[name] = self.calls.get(name, 0) + 1
            return real(*a, **k)

        return _wrapped


def _option_trade() -> TradeInput:
    return TradeInput(
        entry_dt_ist=datetime(2026, 5, 15, 10, 0),
        exit_dt_ist=datetime(2026, 5, 15, 11, 0),
        instrument="NIFTY",
        option_type="CE",
        strike=2_500_000,
        expiry_date=date(2026, 5, 21),
        entry_bar_open_paisa=9_995,
        exit_bar_open_paisa=12_005,
        entry_bar_high_paisa=9_995,
        entry_bar_low_paisa=9_995,
        exit_bar_high_paisa=12_005,
        exit_bar_low_paisa=12_005,
        side="long_call",
        n_lots=1,
        broker="groww",
        gst_split="intra_state",
        trade_outcome="NORMAL_CLOSE",
    )


def _futures_trade() -> FuturesTradeInput:
    return FuturesTradeInput(
        entry_dt_ist=datetime(2025, 1, 2, 9, 16),
        exit_dt_ist=datetime(2025, 1, 2, 15, 20),
        instrument="NIFTY",
        contract_symbol="NSE-NIFTY-30Jan25-FUT",
        expiry_date=date(2025, 1, 30),
        entry_bar_open_paisa=2_200_000,
        exit_bar_open_paisa=2_210_000,
        entry_bar_high_paisa=2_200_000,
        entry_bar_low_paisa=2_200_000,
        exit_bar_high_paisa=2_210_000,
        exit_bar_low_paisa=2_210_000,
        side="long",
        lot_size=75,
        n_lots=1,
        broker="groww",
        gst_split="intra_state",
    )


def test_off_arm_is_todays_python_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    """The OFF-arm (kill switch) == TODAY byte-identity pin.

    Both explicit OFF spellings (``"0"`` and ``"off"``) produce IDENTICAL
    breakdowns through the REAL dispatch sites with the kernel structurally
    unreachable (bomb) — i.e. the OFF arm IS the verbatim Python oracle.
    Since DEC-RUST-DEFAULT-ON-WAVE-001 (#2215) the seam default is ON, so an
    UNSET flag now engages the crate and is no longer an OFF spelling — OFF is
    the explicit kill switch. The "== today" half is carried by the UNMODIFIED
    frozen golden suites (test_costs_verified_examples / the executor goldens)
    passing on the ``"0"``-armed run; this pin proves the OFF arm cannot
    involve the kernel at all, so those suites certify the OFF arm bytes.
    """
    bomb = _BombKernel()
    monkeypatch.setattr(exec_rust, "_rust", bomb)
    results: list[tuple[TradeCostBreakdown, FuturesCostBreakdown]] = []
    for flag in ("0", "off"):
        monkeypatch.setenv(exec_rust.ENV_FLAG, flag)
        results.append(
            (compute_trade_cost(_option_trade()), compute_futures_trade_cost(_futures_trade()))
        )
    assert results[0] == results[1]
    # The direct-oracle projection: the OFF-arm breakdown carries exactly the
    # _charge_stack_py numbers for the same resolved arguments.
    breakdown = results[0][0]
    stack = _charge_stack_py(
        9_995,
        9_995,
        12_005,
        12_005,
        is_long=True,
        lot_size=breakdown.n_shares_per_lot,
        n_lots=1,
        brokerage_round_trip_paisa=C.brokerage_paisa("groww") * 2,
        stt_rate=C.stt_options_rate_bps_x100(date(2026, 5, 15)),
        etc_rate=C.exchange_charge_bps_x100("NIFTY", date(2026, 5, 15)),
        sebi_rate=C.SEBI_FEE_BPS_X100,
        ipft_rate=C.ipft_bps_x100("NIFTY"),
        stamp_rate=C.STAMP_DUTY_OPTIONS_BPS_X100,
        gst_rate=C.GST_TOTAL_BPS_X100,
    )
    assert breakdown.net_pnl_paisa == stack.net_pnl_paisa
    assert bomb.touched == []


def test_on_arm_engages_the_kernel_at_compute_trade_cost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ON: ``compute_trade_cost`` is SERVED by exactly one kernel call and
    its breakdown equals the OFF arm whole-object (dual-arm equality +
    engagement — never vacuous)."""
    trade = _option_trade()
    monkeypatch.setenv(exec_rust.ENV_FLAG, "0")
    off = compute_trade_cost(trade)
    spy = _CountingKernel()
    monkeypatch.setattr(exec_rust, "_rust", spy)
    monkeypatch.setenv(exec_rust.ENV_FLAG, "1")
    on = compute_trade_cost(trade)
    assert spy.calls == {"exec_option_charge_stack": 1}
    assert on == off


def test_on_arm_engages_the_kernel_at_compute_futures_trade_cost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ON: ``compute_futures_trade_cost`` (cost-free index arm) is served by
    exactly one kernel call and equals the OFF arm whole-object."""
    trade = _futures_trade()
    monkeypatch.setenv(exec_rust.ENV_FLAG, "0")
    off = compute_futures_trade_cost(trade)
    spy = _CountingKernel()
    monkeypatch.setattr(exec_rust, "_rust", spy)
    monkeypatch.setenv(exec_rust.ENV_FLAG, "1")
    on = compute_futures_trade_cost(trade)
    assert spy.calls == {"exec_futures_charge_stack": 1}
    assert on == off
    assert on.cost_free is True
    assert on.net_pnl_paisa == on.gross_pnl_paisa


def test_import_census_only_the_four_dispatch_sites_import_the_seam() -> None:
    """No module outside the FOUR dispatch sites (+ this suite / the seam
    itself) imports ``brutex.exec_rust`` — the raw kernel entries stay
    private plumbing behind the seam (the import-census pin)."""
    import subprocess

    root = Path(__file__).resolve().parents[2]
    out = subprocess.run(
        [
            "grep",
            "-rl",
            "--include=*.py",
            r"from brutex import exec_rust",
            str(root / "brutex"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    got = {str(Path(p).relative_to(root)) for p in out.stdout.split() if p}
    assert got == {
        "brutex/costs/calculator.py",
        "brutex/costs/futures_costs.py",
        "brutex/options/executor.py",
        "brutex/futures/executor.py",
    }
    # And nothing in brutex/ CALLS the exec_ kernel entries directly (the
    # `fno_math.exec_*(` call shape — docstring mentions are prose) —
    # every consumer goes through the seam module's typed wrappers.
    out2 = subprocess.run(
        [
            "grep",
            "-rlE",
            "--include=*.py",
            r"fno_math\.exec_\w+\(",
            str(root / "brutex"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert out2.stdout.strip() == ""


def test_seam_is_a_costs_free_leaf_ast_pin() -> None:
    """The seam's ``brutex``-internal imports are EXACTLY the pinned leaf set.

    The CodeQL ``py/cyclic-import`` fix (the #1533 leaf-homing precedent):
    ``brutex/exec_rust.py`` must import the shared :class:`ChargeStack`
    shape from the stdlib-only leaf ``brutex.costs.types`` — NEVER from
    ``brutex.costs.calculator``, whose dispatch site imports the seam back
    (the mutual pair CodeQL flagged). Walks the WHOLE AST (module-level
    AND function-local ``import``/``from`` statements), so a lazily
    re-introduced calculator import fails too; the exact-set law also
    catches a silently GROWN import surface.

    GROWTH RECORD (DEC-RUST-STRICT-NO-FALLBACK-001): ``brutex.config.rust_strict``
    joins the set as the strict-mode gate the ONE ``_warn_once`` choke point
    consults. It is a CONSCIOUS, reviewed addition — the exact-set law did its
    job by forcing this review rather than letting the surface drift. It is
    cycle-free (``rust_strict`` → ``brutex.config.env_flags`` + ``brutex.models``
    → ``brutex.models_rust``, none of which imports the seam back) and is
    imported FUNCTION-LOCALLY inside ``exec_rust._refuse_if_strict`` so this
    leaf's import-time weight is unchanged. The calculator ban — the defect
    this pin exists for — is untouched.
    """
    import ast

    source = (Path(__file__).resolve().parents[2] / "brutex" / "exec_rust.py").read_text(
        encoding="utf-8"
    )
    modules: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.add(node.module)
        elif isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
    brutex_imports = {m for m in modules if m == "brutex" or m.startswith("brutex.")}
    assert brutex_imports == {
        "brutex.config.rust_strict",
        "brutex.costs.types",
        "brutex.engine.sweep_lookahead",
    }
    # The defect this pin was minted for: the calculator half of the mutual
    # pair must NEVER re-appear, at any scope.
    assert "brutex.costs.calculator" not in brutex_imports


class TestMutationWorkflowWiring:
    """Pin this module INTO ``mutation.yml``'s per-module zero-survivor loop
    (DEC-EXEC-RUST-CORE-001 — the lake_stats_manifest precedent; the pair
    rides the historical runner, hence the trailing ``:``). Local run
    2026-07-11: 90 mutants, 0 survived. HONEST: pins the WIRING only — the
    weekly run executes on its own schedule."""

    _WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "mutation.yml"

    def test_module_paired_with_this_suite_in_the_mutmut_loop(self) -> None:
        text = self._WORKFLOW.read_text(encoding="utf-8")
        assert '"brutex/exec_rust.py:tests/unit/test_exec_rust_seam.py:"' in text, (
            "exec_rust.py is not wired into mutation.yml's per-module mutmut loop"
        )


# ────── strict mode (DEC-RUST-STRICT-NO-FALLBACK-001) ──────


class TestStrictRefusal:
    """Strict-ON pins — the companion to ``_strict_off_degrade_lattice``.

    Everything else in this module runs with ``BRUTEX_RUST_STRICT=0`` (the
    degrade lattice). These tests DELETE the kill switch so strict mode takes
    its shipped default (ON) and pin that the single ``_warn_once`` choke
    point REFUSES instead of quietly handing the call to the Python arm.
    """

    @staticmethod
    @contextmanager
    def _strict_on() -> Generator[None]:
        """Delete the kill switch for the DURATION OF THE TEST BODY ONLY.

        Owns its OWN :class:`pytest.MonkeyPatch` context rather than the
        shared ``monkeypatch`` fixture: ``_strict_off_degrade_lattice`` sets
        ``BRUTEX_RUST_STRICT=0`` in a *separate* context, and pytest tears
        fixtures down LIFO across fixture objects — a shared-``monkeypatch``
        ``delenv`` here would record ``"0"`` as the value to restore and leak
        the kill switch into every later test file in the process.
        """
        from brutex.config import rust_strict

        with pytest.MonkeyPatch.context() as mp:
            mp.delenv(rust_strict.RUST_STRICT_ENV, raising=False)
            assert rust_strict.strict_mode() is True
            yield

    def test_class_a_wheel_missing_refuses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """CLASS A — **an ABSENT wheel is not a legitimate input shape**.

        The pre-strict seam refused only a STALE wheel (``StaleExecWheelError``)
        while an ABSENT one degraded silently; strict mode closes that half.
        """
        from brutex.config.rust_strict import RustFallbackRefused

        monkeypatch.setenv(exec_rust.ENV_FLAG, "1")
        monkeypatch.setattr(exec_rust, "_rust", None)
        with self._strict_on(), pytest.raises(RustFallbackRefused, match="wheel_missing"):
            exec_rust.fill_slippage(9_995, 9_995, 12_005, 12_005, is_long=True)

    def test_class_a_flag_unrecognised_refuses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """CLASS A — a typo'd kill switch must be fatal, never a quiet OFF."""
        from brutex.config.rust_strict import RustFallbackRefused

        monkeypatch.setenv(exec_rust.ENV_FLAG, "yes-please")
        with self._strict_on(), pytest.raises(RustFallbackRefused, match="flag_unrecognised"):
            exec_rust.exec_rust_enabled()

    def test_class_b_kernel_fault_refuses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """CLASS B — the kernel RAN and raised; a quiet Python re-run hides it."""
        from brutex.config.rust_strict import RustFallbackRefused

        def _boom(*_args: object, **_kwargs: object) -> object:
            raise ValueError("kernel says no")

        monkeypatch.setenv(exec_rust.ENV_FLAG, "1")
        monkeypatch.setattr(exec_rust, "_rust", SimpleNamespace(exec_apply_slippage=_boom))
        with self._strict_on(), pytest.raises(RustFallbackRefused, match="kernel:fill_slippage"):
            exec_rust.fill_slippage(9_995, 9_995, 12_005, 12_005, is_long=True)

    @pytest.mark.parametrize("site", sorted(_envelope_calls()))
    def test_class_c_envelope_prefix_does_not_refuse(
        self, monkeypatch: pytest.MonkeyPatch, site: str
    ) -> None:
        """CLASS C — the ``envelope:`` prefix family.

        The ``_fits_i64`` PRE-gate refused a value the kernel's i64 wire
        cannot carry, so the kernel is NEVER called (bomb kernel proves it)
        and the exact Python arm serves a legal-but-wide input. Refusing would
        crash a legal charge; the real fix is a wider kernel wire.
        """
        monkeypatch.setenv(exec_rust.ENV_FLAG, "1")
        bomb = _BombKernel()
        monkeypatch.setattr(exec_rust, "_rust", bomb)
        with self._strict_on():
            assert _envelope_calls()[site]() is None
        assert bomb.touched == []

    def test_strict_off_still_serves_the_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Kill switch ON (``0``): the SAME CLASS-A path degrades, no raise."""
        from brutex.config import rust_strict

        monkeypatch.setenv(rust_strict.RUST_STRICT_ENV, "0")
        monkeypatch.setenv(exec_rust.ENV_FLAG, "1")
        monkeypatch.setattr(exec_rust, "_rust", None)
        assert exec_rust.fill_slippage(9_995, 9_995, 12_005, 12_005, is_long=True) is None
