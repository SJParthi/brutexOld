"""The brute-force SWEEP timeframe scope — DEC-SWEEP-TF-SCOPE-5-001.

THE ONE place the scope VALUE is pinned. Every other test that mentions the
default (`test_cli_backfill`, `test_dashboard_sweep_run_route`) REFERENCES
:data:`~brutex.data.timeframes.DEFAULT_SWEEP_INTERVALS` instead of re-typing it,
so those tests pin THREADING and this module pins the DECISION. A scope change
is then exactly one edit plus one deliberate golden update here — never a
scavenger hunt through six files that each quietly disagree.

Why this module exists at all: before this DEC the default lived as TWO
hardcoded copies (`brutex.cli`'s ``--intervals`` argparse default and
``brutex.dashboard.sweep_runner.DEFAULT_INTERVALS``) of a 3-TF string
(``"1minute,5minute,15minute"``) that no ledger decision had ever ruled on.
Nothing stopped the CLI and the one-click button from sweeping DIFFERENT
timeframe sets while both called themselves "the default" (§11 COMMON).
"""

from __future__ import annotations

import argparse
from typing import Final

from brutex.data.timeframes import (
    ALL_LAKE_INTERVAL_LABELS,
    ALL_TFS,
    DEFAULT_SWEEP_INTERVALS,
    SWEEP_TIMEFRAMES,
)

#: The operator's ruled scope, re-typed ONCE (2026-07-31): 1/2/3/5/15-minute.
#: A change here is a scope change and needs its own DEC entry.
_RULED_SCOPE: Final[tuple[str, ...]] = (
    "1minute",
    "2minute",
    "3minute",
    "5minute",
    "15minute",
)


class TestScopeValue:
    """The decision itself."""

    def test_scope_is_the_ruled_five(self) -> None:
        assert SWEEP_TIMEFRAMES == _RULED_SCOPE

    def test_derived_csv_matches_the_tuple(self) -> None:
        """The CSV is DERIVED, so a tuple edit cannot leave a stale help string."""
        assert ",".join(SWEEP_TIMEFRAMES) == DEFAULT_SWEEP_INTERVALS
        assert DEFAULT_SWEEP_INTERVALS == "1minute,2minute,3minute,5minute,15minute"

    def test_every_scope_label_is_a_registered_lake_label(self) -> None:
        """A scope label the lake never materialises would sweep an empty series."""
        assert set(SWEEP_TIMEFRAMES) <= set(ALL_LAKE_INTERVAL_LABELS)

    def test_scope_has_no_duplicates(self) -> None:
        """A duplicated label would double that TF's share of the grid fan-out."""
        assert len(set(SWEEP_TIMEFRAMES)) == len(SWEEP_TIMEFRAMES)


class TestOneDayIsAnchorSourceNotSweptSurface:
    """``1day`` must EXIST in the lake but must NOT be swept.

    The operator's own framing (2026-07-31): *"1day timeframe is to calculate the
    prev day OHLC"*. That is exactly the Single-Timeframe Invariant
    (MIGRATION.md #97) — PDH/PDL, the pivot ladder and the Fibonacci anchors are
    SCALAR DAILY values broadcast across intraday bars, which is why no condition
    in the 60-bit vocabulary reads a second intraday series. So ``1day`` is an
    anchor SOURCE, never a strategy timeframe.
    """

    def test_one_day_is_absent_from_the_sweep_scope(self) -> None:
        assert "1day" not in SWEEP_TIMEFRAMES

    def test_one_day_remains_a_registered_lake_label(self) -> None:
        """The absence above is a SCOPE choice, not a de-registration.

        If this ever fails, the daily partition stopped being materialised and
        Stage-1A loses its prev-day anchors — a data bug, not a scope change.
        """
        assert "1day" in ALL_LAKE_INTERVAL_LABELS


class TestScopeIsNarrowerThanCapability:
    """Scope ≠ capability. The lake still materialises all 21 TFs."""

    def test_registry_still_ships_twenty_one(self) -> None:
        assert len(ALL_TFS) == 21

    def test_scope_is_a_strict_subset(self) -> None:
        assert set(SWEEP_TIMEFRAMES) < set(ALL_LAKE_INTERVAL_LABELS)


class TestBothSurfacesShareTheOneHome:
    """The §11 COMMON pin — the CLI and the dashboard cannot disagree."""

    def test_dashboard_default_is_the_shared_object(self) -> None:
        from brutex.dashboard import sweep_runner

        assert sweep_runner.DEFAULT_INTERVALS is DEFAULT_SWEEP_INTERVALS

    def test_cli_argparse_default_is_the_shared_object(self) -> None:
        """Read the LIVE parser, so deleting the import cannot pass this test."""
        from brutex import cli

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command")
        grid = sub.add_parser("sweep-all")
        cli._add_sweep_grid_args(grid)

        actions = [a for a in grid._actions if "--intervals" in a.option_strings]
        assert len(actions) == 1, "expected exactly one --intervals argparse action"
        assert actions[0].default is DEFAULT_SWEEP_INTERVALS

    def test_blank_input_resolves_to_the_scope(self) -> None:
        """Blank ⇒ the scope. (The v29 placeholder used to claim "all 21" — false.)"""
        from brutex.dashboard.sweep_runner import _normalise_intervals

        assert _normalise_intervals(None) == DEFAULT_SWEEP_INTERVALS
        assert _normalise_intervals("") == DEFAULT_SWEEP_INTERVALS
        assert _normalise_intervals("   ") == DEFAULT_SWEEP_INTERVALS


class TestMeasuredCostArithmetic:
    """Pin the cost model the scope decision was actually made on.

    Sweep work scales as ``masks x bars``. A coarser timeframe holds
    proportionally FEWER bars, so a TF SET costs ``sum(1/minutes)`` relative to a
    single 1-minute sweep — NOT ``len(set)``. Counting tuples overstates the
    saving of narrowing 21 -> 5 by **2.6x** (it suggests 4.2x; the real figure is
    1.61x). That error was made and caught during this DEC; this test is the
    forcing function that stops it being made again.
    """

    @staticmethod
    def _relative_bar_work(labels: tuple[str, ...]) -> float:
        from brutex.data.timeframes import trading_minutes_for_lake_interval

        return sum(1.0 / trading_minutes_for_lake_interval(label) for label in labels)

    def test_five_tf_scope_costs_2_1x_a_single_1minute_sweep(self) -> None:
        assert self._relative_bar_work(SWEEP_TIMEFRAMES) == 1 + 1 / 2 + 1 / 3 + 1 / 5 + 1 / 15
        assert round(self._relative_bar_work(SWEEP_TIMEFRAMES), 3) == 2.100

    def test_narrowing_from_all_21_saves_1_61x_not_4_2x(self) -> None:
        all_21 = self._relative_bar_work(tuple(ALL_LAKE_INTERVAL_LABELS))
        five = self._relative_bar_work(SWEEP_TIMEFRAMES)
        saving = all_21 / five
        assert round(all_21, 3) == 3.389
        assert 1.60 < saving < 1.62, f"expected ~1.61x, got {saving:.3f}x"
        # The naive tuple-count reading, pinned so the gap stays visible.
        naive = len(ALL_LAKE_INTERVAL_LABELS) / len(SWEEP_TIMEFRAMES)
        assert round(naive, 1) == 4.2
        assert naive / saving > 2.5, "the tuple-count reading overstates by >2.5x"
