"""Wave-3G Rust kernel path — ``brutex/dashboard/auto_pull.py`` (DEC-RUST-PULL-STATE-001).

Flip phase: the reload predicate IS the kernel — no flag, no oracle.

* the kernel bool is SERVED (a deliberately-wrong fake proves the serving)
  and the five facts cross the FFI in order;
* every wheel fault (missing / stale / raising / poison / out-of-i64 counts)
  RAISES the typed :class:`PullStateKernelError` — fail-closed, no fallback;
* a still-SET removed ``BRUTEX_PULL_STATE_RUST`` knob raises
  :class:`PullStateFlagRemovedError`;
* the REAL wheel replays the frozen golden lattice bit-for-bit.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import brutex_pull_state
import pytest
from brutex import pull_state_rust
from brutex.dashboard.auto_pull import should_reload_instrument_master

_JSON_FIXTURE = (
    Path(__file__).resolve().parents[1] / "golden" / "fixtures" / "auto_pull_reload_verdicts.json"
)


def _fake_kernel(**functions: object) -> SimpleNamespace:
    """A ``sys.modules`` plant standing in for the ``brutex_pull_state`` wheel."""
    return SimpleNamespace(**functions)


def _call(index_count: int = 3, stock_count: int = 5, **flags: bool) -> bool:
    """Drive the live predicate with defaulted healthy counts."""
    return should_reload_instrument_master(
        index_count=index_count,
        stock_count=stock_count,
        legs_present=flags.get("legs_present", False),
        stale_economics=flags.get("stale_economics", False),
        stale_eligibility=flags.get("stale_eligibility", False),
    )


class TestKernelServes:
    """The kernel decision is SERVED — never re-derived in Python."""

    def test_kernel_bool_served(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A deliberately-WRONG kernel answer is SERVED — the decision comes
        from the kernel, not a Python re-derivation."""
        monkeypatch.setitem(
            sys.modules,
            "brutex_pull_state",
            _fake_kernel(should_reload_master=lambda *a: True),
        )
        assert _call() is True

    def test_kernel_receives_the_five_facts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The five facts cross the FFI positionally, in the declared order."""
        seen: list[tuple[object, ...]] = []

        def kernel(*args: object) -> bool:
            seen.append(args)
            return False

        monkeypatch.setitem(
            sys.modules, "brutex_pull_state", _fake_kernel(should_reload_master=kernel)
        )
        assert _call(index_count=0, stock_count=7, stale_economics=True) is False
        assert seen == [(0, 7, False, True, False)]


class TestFailClosed:
    """Every wheel fault raises the typed error — no oracle exists."""

    def test_missing_wheel_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An unimportable wheel refuses loudly (``wheel_missing``)."""
        monkeypatch.setitem(sys.modules, "brutex_pull_state", None)
        with pytest.raises(pull_state_rust.PullStateKernelError, match="wheel_missing"):
            _call(index_count=0)

    def test_stale_wheel_raises_naming_the_symbol(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A wheel predating the symbol refuses naming it (``wheel_stale``)."""
        monkeypatch.setitem(sys.modules, "brutex_pull_state", _fake_kernel())
        with pytest.raises(pull_state_rust.PullStateKernelError, match="should_reload_master"):
            _call()

    def test_raising_kernel_raises_kernel_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A raising kernel becomes the typed ``kernel_error`` raise."""

        def kernel(*_a: object) -> bool:
            raise RuntimeError("kernel bomb")

        monkeypatch.setitem(
            sys.modules, "brutex_pull_state", _fake_kernel(should_reload_master=kernel)
        )
        with pytest.raises(pull_state_rust.PullStateKernelError, match="kernel_error"):
            _call(legs_present=True)

    @pytest.mark.parametrize("poison", [1, None, "yes"])
    def test_poison_results_raise_kernel_invalid(
        self, monkeypatch: pytest.MonkeyPatch, poison: Any
    ) -> None:
        """A non-bool result is refused (``kernel_invalid``), never punned."""
        monkeypatch.setitem(
            sys.modules, "brutex_pull_state", _fake_kernel(should_reload_master=lambda *a: poison)
        )
        with pytest.raises(pull_state_rust.PullStateKernelError, match="kernel_invalid"):
            _call(index_count=0)

    def test_out_of_i64_counts_refuse_loudly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The input-domain gate: an out-of-i64 count raises NAMING the
        domain — the pre-flip silent Python arm was deleted with the oracle."""
        monkeypatch.setitem(
            sys.modules, "brutex_pull_state", _fake_kernel(should_reload_master=lambda *a: True)
        )
        with pytest.raises(
            pull_state_rust.PullStateKernelError, match="outside the kernel i64 domain"
        ):
            _call(index_count=2**70, stock_count=0)
        with pytest.raises(
            pull_state_rust.PullStateKernelError, match="outside the kernel i64 domain"
        ):
            _call(index_count=0, stock_count=-(2**70))

    def test_set_removed_flag_refuses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A still-SET removed knob refuses loudly — truthy AND falsy alike."""
        for value in ("1", "0"):
            monkeypatch.setenv(pull_state_rust.PULL_STATE_RUST_ENV, value)
            with pytest.raises(pull_state_rust.PullStateFlagRemovedError):
                _call()


class TestRealWheelGoldenReplay:
    """The installed wheel replays the frozen lattice bit-for-bit."""

    def test_reload_lattice_replays_bit_for_bit(self) -> None:
        """Every frozen kernel row through the RAW wheel symbol."""
        payload = json.loads(_JSON_FIXTURE.read_text(encoding="utf-8"))
        replayed = 0
        for row in payload["rows"]:
            if not row["kernel_row"]:
                continue
            got = brutex_pull_state.should_reload_master(
                row["index_count"],
                row["stock_count"],
                row["legs_present"],
                row["stale_economics"],
                row["stale_eligibility"],
            )
            assert got is row["verdict"], row["name"]
            replayed += 1
        assert replayed == 40

    def test_live_module_serves_the_frozen_kernel_rows(self) -> None:
        """The LIVE predicate (kernel-only) equals every frozen kernel row."""
        payload = json.loads(_JSON_FIXTURE.read_text(encoding="utf-8"))
        for row in payload["rows"]:
            if not row["kernel_row"]:
                continue
            got = should_reload_instrument_master(
                index_count=row["index_count"],
                stock_count=row["stock_count"],
                legs_present=row["legs_present"],
                stale_economics=row["stale_economics"],
                stale_eligibility=row["stale_eligibility"],
            )
            assert got is row["verdict"], row["name"]
