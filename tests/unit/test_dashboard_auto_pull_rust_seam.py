"""Wave-3G Rust seam — ``brutex/dashboard/auto_pull.py`` (DEC-RUST-PULL-STATE-001).

The reload-predicate seam behind the default-OFF ``BRUTEX_PULL_STATE_RUST``:
flag OFF byte-identical + bomb-spied; flag ON serves the kernel bool
(deliberately-wrong fakes prove the serving); degrades (raise / poison /
out-of-i64 counts / wheel missing) fall back to the byte-identical oracle;
the REAL wheel (capability-probed) replays the frozen golden lattice.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from brutex import pull_state_rust
from brutex.dashboard.auto_pull import should_reload_instrument_master

_FLAG = pull_state_rust.PULL_STATE_RUST_ENV
_JSON_FIXTURE = (
    Path(__file__).resolve().parents[1] / "golden" / "fixtures" / "auto_pull_reload_verdicts.json"
)


@pytest.fixture(autouse=True)
def _clean_warn_registry() -> Iterator[None]:
    pull_state_rust.reset_for_tests()
    yield
    pull_state_rust.reset_for_tests()


class _RecordingBomb:
    def __init__(self) -> None:
        self.touched: list[str] = []

    def __getattr__(self, name: str) -> object:
        object.__getattribute__(self, "touched").append(name)
        raise AttributeError(name)


def _fake_kernel(**functions: object) -> SimpleNamespace:
    return SimpleNamespace(**functions)


def _call(index_count: int = 3, stock_count: int = 5, **flags: bool) -> bool:
    return should_reload_instrument_master(
        index_count=index_count,
        stock_count=stock_count,
        legs_present=flags.get("legs_present", False),
        stale_economics=flags.get("stale_economics", False),
        stale_eligibility=flags.get("stale_eligibility", False),
    )


class TestFlagOffNeverTouchesKernel:
    def test_flag_unset_and_falsy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for flag in (None, "0"):
            if flag is None:
                monkeypatch.delenv(_FLAG, raising=False)
            else:
                monkeypatch.setenv(_FLAG, flag)
            bomb = _RecordingBomb()
            monkeypatch.setitem(sys.modules, "brutex_pull_state", bomb)
            assert _call() is False
            assert _call(index_count=0) is True
            assert bomb.touched == []


class TestBombTeeth:
    def test_flag_on_records_the_touch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(_FLAG, "1")
        bomb = _RecordingBomb()
        monkeypatch.setitem(sys.modules, "brutex_pull_state", bomb)
        assert _call() is False  # the oracle still serves
        assert "should_reload_master" in bomb.touched


class TestFlagOnServes:
    def test_kernel_bool_served(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A deliberately-WRONG kernel answer is SERVED — the decision comes
        from the kernel, not the oracle."""
        monkeypatch.setenv(_FLAG, "1")
        monkeypatch.setitem(
            sys.modules,
            "brutex_pull_state",
            _fake_kernel(should_reload_master=lambda *a: True),
        )
        assert _call() is True

    def test_kernel_receives_the_five_facts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(_FLAG, "1")
        seen: list[tuple[object, ...]] = []

        def kernel(*args: object) -> bool:
            seen.append(args)
            return False

        monkeypatch.setitem(
            sys.modules, "brutex_pull_state", _fake_kernel(should_reload_master=kernel)
        )
        assert _call(index_count=0, stock_count=7, stale_economics=True) is False
        assert seen == [(0, 7, False, True, False)]


class TestFlagOnDegrades:
    @pytest.mark.parametrize("poison", [lambda *a: 1, lambda *a: None, lambda *a: "yes"])
    def test_poison_degrades_to_oracle(self, monkeypatch: pytest.MonkeyPatch, poison: Any) -> None:
        monkeypatch.setenv(_FLAG, "1")
        monkeypatch.setitem(
            sys.modules, "brutex_pull_state", _fake_kernel(should_reload_master=poison)
        )
        assert _call(index_count=0) is True

    def test_raising_kernel_degrades_to_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(_FLAG, "1")

        def kernel(*_a: object) -> bool:
            raise RuntimeError("kernel bomb")

        monkeypatch.setitem(
            sys.modules, "brutex_pull_state", _fake_kernel(should_reload_master=kernel)
        )
        assert _call(legs_present=True) is True

    def test_out_of_i64_counts_stay_python_silently(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The representability gate: no kernel call, no warn — the
        documented Python arm."""
        monkeypatch.setenv(_FLAG, "1")
        bomb = _RecordingBomb()
        monkeypatch.setitem(sys.modules, "brutex_pull_state", bomb)
        assert _call(index_count=2**70, stock_count=0) is False
        assert _call(index_count=0, stock_count=-(2**70)) is True
        assert bomb.touched == []


class TestRealWheelGoldenReplay:
    def test_reload_lattice_replays_bit_for_bit(self) -> None:
        wheel = pytest.importorskip("brutex_pull_state")
        if not hasattr(wheel, "should_reload_master"):
            pytest.skip("installed brutex_pull_state wheel predates the Wave-3G symbols")
        payload = json.loads(_JSON_FIXTURE.read_text(encoding="utf-8"))
        replayed = 0
        for row in payload["rows"]:
            if not row["kernel_row"]:
                continue
            got = wheel.should_reload_master(
                row["index_count"],
                row["stock_count"],
                row["legs_present"],
                row["stale_economics"],
                row["stale_eligibility"],
            )
            assert got is row["verdict"], row["name"]
            replayed += 1
        assert replayed == 40
