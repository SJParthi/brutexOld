"""Wave-3G Rust seam — ``brutex/dashboard/pull_census.py`` (DEC-RUST-PULL-STATE-001).

The census-fold seam behind the default-OFF ``BRUTEX_PULL_STATE_RUST``:
flag OFF byte-identical + bomb-spied; flag ON serves the kernel triple
(deliberately-wrong fakes prove the serving; the ``repr()`` sample strings
are rendered Python-side); every degrade (raise / poison shapes / cap
overflow / wheel missing) falls back to the byte-identical oracle; the REAL
wheel (capability-probed) replays the frozen golden corpus bit-for-bit.
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
from brutex.dashboard.pull_census import (
    CENSUS_SAMPLE_CAP,
    build_written_vs_settled_census,
    emit_written_settled_census,
)

from tests.golden._pull_census_harness import _row, _span

_FLAG = pull_state_rust.PULL_STATE_RUST_ENV
_JSON_FIXTURE = (
    Path(__file__).resolve().parents[1] / "golden" / "fixtures" / "pull_census_verdicts.json"
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


def _build(**kwargs: Any) -> Any:
    return build_written_vs_settled_census(
        kwargs.get("rows", [_row("NSE-NIFTY")]),
        kwargs.get("spans", []),
        intervals=("1minute",),
        symbols=kwargs.get("symbols", ["NSE-NIFTY"]),
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
            census = _build()
            assert (census.settled_unwritten, census.scanned) == (1, 1)
            emit_written_settled_census(
                [_row("NSE-NIFTY")], [], intervals=("1minute",), symbols=["NSE-NIFTY"], source="t"
            )
            assert bomb.touched == []


class TestBombTeeth:
    def test_flag_on_records_the_touch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(_FLAG, "1")
        bomb = _RecordingBomb()
        monkeypatch.setitem(sys.modules, "brutex_pull_state", bomb)
        census = _build()
        assert census.settled_unwritten == 1  # the oracle still serves
        assert "census_fold" in bomb.touched


class TestFlagOnServes:
    def test_kernel_triple_served_and_rendered(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A deliberately-WRONG kernel answer is SERVED — proving the fold
        decision comes from the kernel while Python renders the strings."""
        monkeypatch.setenv(_FLAG, "1")
        monkeypatch.setitem(
            sys.modules,
            "brutex_pull_state",
            _fake_kernel(census_fold=lambda rows, written: (7, 2, [(0, "X", "1minute")])),
        )
        census = _build()
        assert (census.settled_unwritten, census.written_unsettled) == (7, 2)
        assert census.samples == ("settled-unwritten: 'X' '1minute'",)
        assert census.scanned == 1  # scanned stays the Python-side count
        assert census.mismatched is True

    def test_kernel_receives_scoped_prefiltered_inputs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_FLAG, "1")
        seen: list[tuple[Any, Any]] = []

        def kernel(rows: Any, written: Any) -> tuple[int, int, list[Any]]:
            seen.append((rows, written))
            return (0, 0, [])

        monkeypatch.setitem(sys.modules, "brutex_pull_state", _fake_kernel(census_fold=kernel))
        build_written_vs_settled_census(
            [_row("NSE-NIFTY"), _row("NSE-FOREIGN"), _row("NSE-NIFTY", status="FAILED")],
            [_span("NSE-NIFTY"), _span("NSE-NIFTY", interval="1day"), _span("NSE-NIFTY", bars=0)],
            intervals=("1minute",),
            symbols=["NSE-NIFTY"],
        )
        assert seen == [
            (
                [("NSE-NIFTY", "1minute", True), ("NSE-NIFTY", "1minute", False)],
                [("NSE-NIFTY", "1minute")],
            )
        ]

    def test_written_unsettled_kind_rendered(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(_FLAG, "1")
        monkeypatch.setitem(
            sys.modules,
            "brutex_pull_state",
            _fake_kernel(census_fold=lambda rows, written: (0, 1, [(1, "W", "1day")])),
        )
        census = _build()
        assert census.samples == ("written-unsettled: 'W' '1day'",)


class TestFlagOnDegrades:
    @pytest.mark.parametrize(
        "poison",
        [
            lambda r, w: None,
            lambda r, w: (1, 2),
            lambda r, w: [1, 2, []],
            lambda r, w: (True, 0, []),
            lambda r, w: (-1, 0, []),
            lambda r, w: (0, 0, [(2, "X", "i")]),
            lambda r, w: (0, 0, [(0, b"X", "i")]),
            lambda r, w: (0, 0, [(0, "X")]),
            lambda r, w: (0, 0, [(0, "X", "i")] * 21),
        ],
    )
    def test_poison_shapes_degrade_to_oracle(
        self, monkeypatch: pytest.MonkeyPatch, poison: Any
    ) -> None:
        monkeypatch.setenv(_FLAG, "1")
        monkeypatch.setitem(sys.modules, "brutex_pull_state", _fake_kernel(census_fold=poison))
        census = _build()
        assert (census.settled_unwritten, census.written_unsettled) == (1, 0)
        assert census.samples == ("settled-unwritten: 'NSE-NIFTY' '1minute'",)

    def test_raising_kernel_degrades_to_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(_FLAG, "1")

        def kernel(rows: Any, written: Any) -> tuple[int, int, list[Any]]:
            raise RuntimeError("kernel bomb")

        monkeypatch.setitem(sys.modules, "brutex_pull_state", _fake_kernel(census_fold=kernel))
        census = _build()
        assert census.settled_unwritten == 1

    def test_cap_constant_sync_pin(self) -> None:
        assert pull_state_rust._CENSUS_SAMPLE_CAP == CENSUS_SAMPLE_CAP  # pyright: ignore[reportPrivateUsage]


class TestRealWheelGoldenReplay:
    def test_census_corpus_replays_bit_for_bit(self) -> None:
        wheel = pytest.importorskip("brutex_pull_state")
        if not hasattr(wheel, "census_fold"):
            pytest.skip("installed brutex_pull_state wheel predates the Wave-3G symbols")
        payload = json.loads(_JSON_FIXTURE.read_text(encoding="utf-8"))
        for case in payload["cases"]:
            rows = [tuple(r) for r in case["kernel_rows"]]
            written = [tuple(p) for p in case["kernel_written"]]
            su, wu, samples = wheel.census_fold(rows, written)
            out = case["out"]
            assert (su, wu) == (out["settled_unwritten"], out["written_unsettled"]), case["name"]
            assert [list(s) for s in samples] == case["kernel_samples"], case["name"]

    def test_flag_on_build_matches_oracle_end_to_end(self, monkeypatch: pytest.MonkeyPatch) -> None:
        wheel = pytest.importorskip("brutex_pull_state")
        if not hasattr(wheel, "census_fold"):
            pytest.skip("installed brutex_pull_state wheel predates the Wave-3G symbols")
        rows = [_row(f"NSE-S{n}") for n in range(30)] + [_row("NSE-X", status="FAILED")]
        spans = [_span(f"NSE-S{n}") for n in range(3)] + [_span("NSE-X"), _span("NSE-Z", bars=0)]
        symbols = [f"NSE-S{n}" for n in range(30)] + ["NSE-X", "NSE-Z"]
        monkeypatch.delenv(_FLAG, raising=False)
        oracle = build_written_vs_settled_census(
            rows, spans, intervals=("1minute",), symbols=symbols
        )
        monkeypatch.setenv(_FLAG, "1")
        served = build_written_vs_settled_census(
            rows, spans, intervals=("1minute",), symbols=symbols
        )
        assert served == oracle
