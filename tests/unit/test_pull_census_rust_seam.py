"""Wave-3G Rust kernel path — ``brutex/dashboard/pull_census.py`` (DEC-RUST-PULL-STATE-001).

Flip phase: the fold/sort/cap IS the kernel — no flag, no oracle.

* the kernel triple is SERVED (deliberately-wrong fakes prove the serving;
  the ``repr()`` sample strings are rendered Python-side) over the
  pre-extracted, scope-filtered inputs;
* every wheel fault (missing / stale / raising / poison shapes / cap
  overflow) RAISES the typed :class:`PullStateKernelError` out of the
  builder — while the observe-only ``emit_written_settled_census`` wiring
  folds it into its guarded ``pull.written_settled_census_error`` WARN
  (the census can never break the pull);
* a still-SET removed ``BRUTEX_PULL_STATE_RUST`` knob raises
  :class:`PullStateFlagRemovedError`;
* the REAL wheel replays the frozen golden corpus bit-for-bit.
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
from brutex.dashboard.pull_census import (
    CENSUS_SAMPLE_CAP,
    build_written_vs_settled_census,
    emit_written_settled_census,
)
from structlog.testing import capture_logs

from tests.golden._pull_census_harness import _row, _span

_JSON_FIXTURE = (
    Path(__file__).resolve().parents[1] / "golden" / "fixtures" / "pull_census_verdicts.json"
)


def _fake_kernel(**functions: object) -> SimpleNamespace:
    """A ``sys.modules`` plant standing in for the ``brutex_pull_state`` wheel."""
    return SimpleNamespace(**functions)


def _build(**kwargs: Any) -> Any:
    """Drive the live builder with a one-row default corpus."""
    return build_written_vs_settled_census(
        kwargs.get("rows", [_row("NSE-NIFTY")]),
        kwargs.get("spans", []),
        intervals=("1minute",),
        symbols=kwargs.get("symbols", ["NSE-NIFTY"]),
    )


class TestKernelServes:
    """The kernel fold is SERVED — Python renders the strings only."""

    def test_kernel_triple_served_and_rendered(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A deliberately-WRONG kernel answer is SERVED — proving the fold
        decision comes from the kernel while Python renders the strings."""
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
        """The extraction/scope-filter law stays Python: the kernel sees the
        scoped ``(symbol, interval, is_success)`` rows + bars>0 pairs only."""
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
        """The other sample kind renders its own prefix."""
        monkeypatch.setitem(
            sys.modules,
            "brutex_pull_state",
            _fake_kernel(census_fold=lambda rows, written: (0, 1, [(1, "W", "1day")])),
        )
        census = _build()
        assert census.samples == ("written-unsettled: 'W' '1day'",)


class TestFailClosed:
    """Every wheel fault raises out of the builder — no oracle exists."""

    def test_missing_wheel_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An unimportable wheel refuses loudly (``wheel_missing``)."""
        monkeypatch.setitem(sys.modules, "brutex_pull_state", None)
        with pytest.raises(pull_state_rust.PullStateKernelError, match="wheel_missing"):
            _build()

    def test_stale_wheel_raises_naming_the_symbol(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A wheel predating the symbol refuses naming it (``wheel_stale``)."""
        monkeypatch.setitem(sys.modules, "brutex_pull_state", _fake_kernel())
        with pytest.raises(pull_state_rust.PullStateKernelError, match="census_fold"):
            _build()

    def test_raising_kernel_raises_kernel_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A raising kernel becomes the typed ``kernel_error`` raise."""

        def kernel(rows: Any, written: Any) -> tuple[int, int, list[Any]]:
            raise RuntimeError("kernel bomb")

        monkeypatch.setitem(sys.modules, "brutex_pull_state", _fake_kernel(census_fold=kernel))
        with pytest.raises(pull_state_rust.PullStateKernelError, match="kernel_error"):
            _build()

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
    def test_poison_shapes_raise_kernel_invalid(
        self, monkeypatch: pytest.MonkeyPatch, poison: Any
    ) -> None:
        """Every out-of-contract fold shape is refused (``kernel_invalid``)."""
        monkeypatch.setitem(sys.modules, "brutex_pull_state", _fake_kernel(census_fold=poison))
        with pytest.raises(pull_state_rust.PullStateKernelError, match="kernel_invalid"):
            _build()

    def test_set_removed_flag_refuses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A still-SET removed knob refuses loudly — truthy AND falsy alike."""
        for value in ("1", "off"):
            monkeypatch.setenv(pull_state_rust.PULL_STATE_RUST_ENV, value)
            with pytest.raises(pull_state_rust.PullStateFlagRemovedError):
                _build()

    def test_emit_wiring_folds_the_kernel_fault_into_its_guard(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The observe-only wiring contract survives the flip: a kernel
        fault logs ONE ``pull.written_settled_census_error`` WARN and
        returns — the census can never break the pull."""
        monkeypatch.setitem(sys.modules, "brutex_pull_state", None)
        with capture_logs() as logs:
            emit_written_settled_census(
                [_row("NSE-NIFTY")], [], intervals=("1minute",), symbols=["NSE-NIFTY"], source="t"
            )
        events = [e for e in logs if e["event"] == "pull.written_settled_census_error"]
        assert len(events) == 1
        assert "wheel_missing" in str(events[0]["error"])

    def test_cap_constant_sync_pin(self) -> None:
        """The facade's cap mirror stays sync-pinned to the census cap."""
        assert pull_state_rust._CENSUS_SAMPLE_CAP == CENSUS_SAMPLE_CAP  # pyright: ignore[reportPrivateUsage]


class TestRealWheelGoldenReplay:
    """The installed wheel replays the frozen corpus bit-for-bit."""

    def test_census_corpus_replays_bit_for_bit(self) -> None:
        """Every frozen case through the RAW wheel symbol."""
        payload = json.loads(_JSON_FIXTURE.read_text(encoding="utf-8"))
        for case in payload["cases"]:
            rows = [tuple(r) for r in case["kernel_rows"]]
            written = [tuple(p) for p in case["kernel_written"]]
            su, wu, samples = brutex_pull_state.census_fold(rows, written)
            out = case["out"]
            assert (su, wu) == (out["settled_unwritten"], out["written_unsettled"]), case["name"]
            assert [list(s) for s in samples] == case["kernel_samples"], case["name"]

    def test_kernel_law_matches_the_inline_reimplementation(self) -> None:
        """The kernel fold vs an INLINE test-code twin of the law (test code
        may reimplement the law; production may not) over a >cap corpus."""
        rows = [(f"NSE-S{n}", "1minute", True) for n in range(30)] + [("NSE-X", "1minute", False)]
        written = [(f"NSE-S{n}", "1minute") for n in range(3)] + [("NSE-X", "1minute")]
        settled = {(s, i) for s, i, ok in rows if ok}
        written_set = set(written)
        expect_su = sorted(settled - written_set)
        expect_wu = sorted(written_set - settled)
        expect_samples = [(0, s, i) for s, i in expect_su][:CENSUS_SAMPLE_CAP]
        expect_samples += [(1, s, i) for s, i in expect_wu][
            : CENSUS_SAMPLE_CAP - len(expect_samples)
        ]
        served = pull_state_rust.rust_census_fold(rows, written)
        assert served == (len(expect_su), len(expect_wu), tuple(expect_samples))

    def test_live_builder_end_to_end_over_the_real_wheel(self) -> None:
        """The LIVE builder (kernel-only) over a mixed corpus serves the
        law's answer with the rendered sample strings."""
        rows = [_row(f"NSE-S{n}") for n in range(3)] + [_row("NSE-X", status="FAILED")]
        spans = [_span("NSE-S0"), _span("NSE-X"), _span("NSE-Z", bars=0)]
        symbols = ["NSE-S0", "NSE-S1", "NSE-S2", "NSE-X", "NSE-Z"]
        census = build_written_vs_settled_census(
            rows, spans, intervals=("1minute",), symbols=symbols
        )
        assert (census.settled_unwritten, census.written_unsettled) == (2, 1)
        assert census.scanned == 4
        assert census.samples == (
            "settled-unwritten: 'NSE-S1' '1minute'",
            "settled-unwritten: 'NSE-S2' '1minute'",
            "written-unsettled: 'NSE-X' '1minute'",
        )
