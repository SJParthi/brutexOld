"""Golden cross-language oracle for the exec integer cores (dual-arm).

DEC-EXEC-RUST-CORE-001 (MIGRATION.md #1960), the ``vocab_golden_vectors``
discipline: a frozen ``input → value`` answer-key
(``fixtures/exec_costs_golden_vectors.json``, produced by
``scripts/gen_exec_costs_golden.py``, regeneration refused without
``BRUTEX_REGEN_EXEC_GOLDEN=1``) that:

* **locks** today's Python charge-stack / fill / money bodies against
  accidental drift (the Python-oracle arm below), and
* is the **contract** the Rust ``fno-math`` exec cores satisfy bit-for-bit —
  natively (``rust/fno-math/tests/exec_golden.rs``) AND through the
  flag-ON seam (the Rust arm below, ENGAGED-asserted so it can never go
  vacuous: a missing wheel or a silent fallback FAILS, it never skips).

Schema v2 = the DEC-FILL-WORST-CASE-ONLY-001 (#1825) reconcile: fills
anchor on the fill bars' FOUR adverse extremes, levy rounding is the
permanent ADVERSE CEILING (``_round_levy_paisa`` / ``_round_levy_rupee``),
and the stale gate is UNCONDITIONAL (no ``reject_stale`` column).

Money values are STRINGS in the fixture (a JSON double loses > 2^53 — the
vocab mask-hex precedent). The materialize sections' verdict laws are
consumed by the seam arm + the cargo test; their ``trade`` stacks are
additionally Python-checked against the stack oracles here (the inline
executor tails carry no standalone pure-Python function — their ON/OFF
whole-object parity is pinned in the executor unit suites instead).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

import pytest
from brutex import exec_rust
from brutex.costs.calculator import (
    _apply_slippage,  # pyright: ignore[reportPrivateUsage] — the oracle arm
    _charge_stack_py,  # pyright: ignore[reportPrivateUsage] — the oracle arm
    _round_levy_paisa,  # pyright: ignore[reportPrivateUsage] — the oracle arm
    _round_levy_rupee,  # pyright: ignore[reportPrivateUsage] — the oracle arm
)
from brutex.costs.futures_costs import (
    _futures_charge_stack_py,  # pyright: ignore[reportPrivateUsage] — the oracle arm
    apply_futures_fill,
)
from brutex.costs.types import ChargeStack

_FIXTURE = Path(__file__).parent / "fixtures" / "exec_costs_golden_vectors.json"
_DATA: dict[str, Any] = json.loads(_FIXTURE.read_text(encoding="utf-8"))

_STACK_ARG_KEYS = (
    "is_long",
    "lot_size",
    "n_lots",
    "stt_rate",
    "etc_rate",
    "sebi_rate",
    "ipft_rate",
    "stamp_rate",
    "gst_rate",
)


def _bars(case: dict[str, Any]) -> tuple[int, int, int, int]:
    """The four fill-bar extremes in the oracle argument order."""
    return (case["entry_high"], case["entry_low"], case["exit_high"], case["exit_low"])


def _option_args(case: dict[str, Any]) -> dict[str, Any]:
    args = {k: case[k] for k in _STACK_ARG_KEYS}
    args["brokerage_round_trip_paisa"] = case["brokerage_round_trip_paisa"]
    return args


def _futures_args(case: dict[str, Any]) -> dict[str, Any]:
    args = {k: case[k] for k in _STACK_ARG_KEYS}
    args["cost_free"] = case["cost_free"]
    args["brokerage_flat_paisa"] = case["brokerage_flat_paisa"]
    args["brokerage_turnover_bps_x100"] = case["brokerage_turnover_bps_x100"]
    return args


def test_fixture_shape_and_volume() -> None:
    """The frozen corpus is present, versioned, and non-trivially sized."""
    assert _DATA["schema_version"] == 2
    for section, floor in (
        ("money", 30),
        ("fills", 300),
        ("option_stacks", 70),
        ("futures_stacks", 50),
        ("options_materialize", 9),
        ("futures_materialize", 7),
    ):
        assert len(_DATA[section]) >= floor, section


# ── the Python-oracle arm (drift lock) ──────────────────────────────────


def test_python_oracle_reproduces_money_vectors() -> None:
    for case in _DATA["money"]:
        x = int(case["x"])
        expected = int(case["expected"])
        got = _round_levy_paisa(x) if case["op"] == "round_to_paisa" else _round_levy_rupee(x)
        assert got == expected, case["id"]


def test_python_oracle_reproduces_fill_vectors() -> None:
    for case in _DATA["fills"]:
        side: Literal["long", "short"] = "long" if case["is_long"] else "short"
        eh, el, xh, xl = _bars(case)
        bar_args = {
            "entry_high_paisa": eh,
            "entry_low_paisa": el,
            "exit_high_paisa": xh,
            "exit_low_paisa": xl,
        }
        if case["op"] == "slippage":
            got = _apply_slippage(side, **bar_args)
        else:
            got = apply_futures_fill(side=side, **bar_args)
        assert list(got) == case["expected"], case["id"]


def test_python_oracle_reproduces_option_stacks() -> None:
    for case in _DATA["option_stacks"]:
        stack = _charge_stack_py(*_bars(case), **_option_args(case))
        assert list(stack) == case["expected"], case["id"]


def test_python_oracle_reproduces_futures_stacks() -> None:
    for case in _DATA["futures_stacks"]:
        stack = _futures_charge_stack_py(*_bars(case), **_futures_args(case))
        assert list(stack) == case["expected"], case["id"]


def test_python_oracle_reproduces_materialize_trade_stacks() -> None:
    """The ``trade`` verdicts' stack halves against the Python stack oracles
    (the verdict GATING itself is pinned by the executor ON/OFF suites +
    the cargo twin — see the module docstring)."""
    checked = 0
    for case in _DATA["options_materialize"]:
        if case["expected_kind"] != "trade":
            continue
        stack = _charge_stack_py(*_bars(case), **_option_args(case))
        assert list(stack) == case["expected"], case["id"]
        checked += 1
    for case in _DATA["futures_materialize"]:
        if case["expected_kind"] != "trade":
            continue
        stack = _futures_charge_stack_py(*_bars(case), **_futures_args(case))
        assert list(stack) == case["expected"][:17], case["id"]
        checked += 1
    assert checked >= 5  # the corpus must genuinely carry priced verdicts


# ── the Rust seam arm (flag ON, ENGAGED-asserted) ───────────────────────


@pytest.fixture
def _rust_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the ON arm and REQUIRE the kernel — never a silent skip."""
    assert exec_rust._rust is not None, (  # pyright: ignore[reportPrivateUsage]
        "fno_math wheel with the exec_ kernels is required (engaged-assert; "
        "rebuild via `pip install ./rust/fno-math`)"
    )
    monkeypatch.setenv(exec_rust.ENV_FLAG, "1")


@pytest.mark.usefixtures("_rust_on")
def test_rust_seam_reproduces_fill_vectors() -> None:
    for case in _DATA["fills"]:
        if case["op"] == "slippage":
            got = exec_rust.fill_slippage(*_bars(case), is_long=case["is_long"])
        else:
            got = exec_rust.fill_futures(*_bars(case), is_long=case["is_long"])
        assert got is not None, f"{case['id']}: seam fell back (not engaged)"
        assert list(got) == case["expected"], case["id"]


@pytest.mark.usefixtures("_rust_on")
def test_rust_seam_reproduces_option_stacks() -> None:
    for case in _DATA["option_stacks"]:
        got = exec_rust.charge_stack_option(*_bars(case), **_option_args(case))
        assert got is not None, f"{case['id']}: seam fell back (not engaged)"
        assert isinstance(got, ChargeStack)
        assert list(got) == case["expected"], case["id"]


@pytest.mark.usefixtures("_rust_on")
def test_rust_seam_reproduces_futures_stacks() -> None:
    for case in _DATA["futures_stacks"]:
        got = exec_rust.charge_stack_futures(*_bars(case), **_futures_args(case))
        assert got is not None, f"{case['id']}: seam fell back (not engaged)"
        assert list(got) == case["expected"], case["id"]


@pytest.mark.usefixtures("_rust_on")
def test_rust_seam_reproduces_options_materialize_verdicts() -> None:
    for case in _DATA["options_materialize"]:
        got = exec_rust.materialize_option_tail(
            case["entry_open"],
            case["exit_open"],
            *_bars(case),
            entry_volume=case["entry_volume"],
            exit_volume=case["exit_volume"],
            **_option_args(case),
        )
        assert got is not None, f"{case['id']}: seam fell back (not engaged)"
        kind, stack = got
        assert kind == case["expected_kind"], case["id"]
        if case["expected"] is None:
            assert stack is None, case["id"]
        else:
            assert stack is not None, case["id"]
            assert list(stack) == case["expected"], case["id"]


@pytest.mark.usefixtures("_rust_on")
def test_rust_seam_reproduces_futures_materialize_verdicts() -> None:
    for case in _DATA["futures_materialize"]:
        got = exec_rust.materialize_futures_tail(
            *_bars(case),
            entry_volume=case["entry_volume"],
            exit_volume=case["exit_volume"],
            path_highs_paisa=case["path_highs"],
            path_lows_paisa=case["path_lows"],
            **_futures_args(case),
        )
        assert got is not None, f"{case['id']}: seam fell back (not engaged)"
        kind, stack, mae, mfe = got
        assert kind == case["expected_kind"], case["id"]
        if case["expected"] is None:
            assert stack is None, case["id"]
            assert (mae, mfe) == (0, 0), case["id"]
        else:
            assert stack is not None, case["id"]
            assert [*stack, mae, mfe] == case["expected"], case["id"]


def test_generator_refuses_regeneration_without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The frozen oracle cannot be silently rewritten — the generator refuses
    while the fixture exists unless ``BRUTEX_REGEN_EXEC_GOLDEN=1`` is set."""
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location(
        "gen_exec_costs_golden",
        Path(__file__).resolve().parents[2] / "scripts" / "gen_exec_costs_golden.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["gen_exec_costs_golden"] = module
    try:
        spec.loader.exec_module(module)
        monkeypatch.delenv(module.REGEN_ENV, raising=False)
        before = _FIXTURE.read_bytes()
        assert module.main() == 1  # refused
        assert _FIXTURE.read_bytes() == before  # byte-untouched
    finally:
        del sys.modules["gen_exec_costs_golden"]
