"""Kernel ≡ the frozen pull-stall LAW — the flip-phase differential
(DEC-RUST-PULL-STATE-001; the Wave-1C parity suite restructured).

The ``brutex_pull_state`` wheel is REQUIRED (the fail-closed facade posture —
there is no oracle to skip back to; a missing wheel must fail this suite
loudly, never skip it green).  With the Python oracle deleted, the
differential's second arm is an IN-TEST reimplementation of the frozen
decision law (test code may reimplement the law; production may not —
CLAUDE.md §11 keeps the production copy singular in Rust):

* the frozen-golden sweep — the KERNEL reproduces EVERY arm token / due bit
  of the committed ``pull_stall_verdicts.json`` fixture (floats decoded from
  their IEEE-754 bit patterns — no parse rounding anywhere);
* the whole-module differential — the REAL ``PullWatchdog`` (kernel-backed)
  classifies the SAME arm as the inline law over hypothesis-drawn guard
  flags × ages (NaN/inf/negatives included) × token shapes × window
  geometries;
* the alive-throttle differential — the REAL module's due-ness equals the
  inline ``not (since < interval)`` law over hostile since/interval draws;
* the end-to-end lifecycle — one scripted hung→wedged run through the real
  module lands the pinned trace.
"""

# Whitebox drives of module-private state (sanctioned — the unit suite precedent).
# pyright: reportPrivateUsage=none

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import brutex_pull_state as _wheel
import pytest
from brutex.dashboard.pull_stall import PullWatchdog
from hypothesis import given
from hypothesis import strategies as st

from tests.golden._pull_stall_harness import (
    WEDGE_ARM_NAMES,
    WedgeCase,
    _drive_alive_case,
    _drive_wedge_case,
    f64_from_bits,
)

_FIXTURE = Path(__file__).resolve().parents[1] / "golden" / "fixtures" / "pull_stall_verdicts.json"


@pytest.fixture(scope="module")
def frozen() -> dict[str, Any]:
    """The committed pull-stall verdict fixture (byte-frozen)."""
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


class TestKernelReproducesTheFrozenGolden:
    """The wheel reproduces every committed verdict row bit-for-bit."""

    def test_every_wedge_row(self, frozen: dict[str, Any]) -> None:
        """Every frozen wedge lattice point yields its recorded arm token."""
        for row in frozen["wedge"]:
            restart_bits = row["restart_age_bits"]
            got = _wheel.wedge_decision(
                row["hung"],
                row["already_fired"],
                row["hook_bound"],
                row["baseline_captured"],
                f64_from_bits(row["last_beat_age_bits"]),
                f64_from_bits(row["wedge_window_bits"]),
                None if restart_bits is None else f64_from_bits(restart_bits),
                row["baseline"],
                row["current"],
            )
            assert got == row["expected_token"], row["label"]

    def test_every_alive_row(self, frozen: dict[str, Any]) -> None:
        """Every frozen alive-throttle row yields its recorded due bit."""
        for row in frozen["alive"]:
            got = _wheel.alive_log_due(
                f64_from_bits(row["since_last_bits"]),
                f64_from_bits(row["interval_bits"]),
            )
            assert got is row["expected_due"], row["label"]


def _inline_wedge_arm(case: WedgeCase) -> int:
    """The frozen wedge-escalation LAW, reimplemented in test code.

    A faithful transcription of the pre-flip Python oracle's guard +
    escalation ladder (test-only — the production copy lives in the Rust
    kernel): the wants-probe guards (flags → HUNG → the strict ``age <
    window`` refusal → the captured baseline), then abstain-on-None →
    adopt-blind-baseline → rebaseline-on-movement → defer-unspanned-window →
    escalate.  NaN/inf ages ride the same IEEE comparisons.
    """
    window = case.multiplier * case.timeout
    wants = (
        not case.already_fired
        and case.hook_bound
        and case.hung
        and not (case.last_beat_age < window)
        and case.baseline_captured
    )
    if not wants:
        return WEDGE_ARM_NAMES.index("no_probe")
    if case.current is None:
        return WEDGE_ARM_NAMES.index("abstain_probe_none")
    if case.baseline is None:
        return WEDGE_ARM_NAMES.index("adopt_baseline")
    if case.current != case.baseline:
        return WEDGE_ARM_NAMES.index("rebaseline")
    if case.restart_age is not None and case.restart_age < window:
        return WEDGE_ARM_NAMES.index("defer_window")
    return WEDGE_ARM_NAMES.index("escalate")


#: Hostile float ages: boundaries, dyadic epsilons, skew negatives, NaN/inf.
_AGES = st.one_of(
    st.floats(min_value=-1e7, max_value=1e7, allow_nan=False),
    st.sampled_from(
        [
            3600.0,
            3600.0 - 2.0**-10,
            3600.0 + 2.0**-10,
            0.0,
            -0.0,
            -10.0,
            float("nan"),
            float("inf"),
        ]
    ),
)
_TOKENS = st.one_of(st.none(), st.sampled_from(["", "revA", "revB", "a:b.c-1"]))


class TestWholeModuleDifferential:
    """The REAL module (kernel-backed) vs the inline law — same arm, always."""

    @given(
        hung=st.booleans(),
        fired=st.booleans(),
        hook=st.booleans(),
        captured=st.booleans(),
        age=_AGES,
        timeout=st.sampled_from([0.5, 30.0, 600.0, 100.0]),
        multiplier=st.sampled_from([0.25, 1.5, 6.0]),
        restart=st.one_of(st.none(), _AGES),
        baseline=_TOKENS,
        current=_TOKENS,
    )
    def test_arm_classification_matches_the_inline_law(
        self,
        hung: bool,
        fired: bool,
        hook: bool,
        captured: bool,
        age: float,
        timeout: float,
        multiplier: float,
        restart: float | None,
        baseline: str | None,
        current: str | None,
    ) -> None:
        """The observed module arm equals the reimplemented frozen law."""
        case = WedgeCase(
            label="differential",
            hung=hung,
            already_fired=fired,
            hook_bound=hook,
            baseline_captured=captured,
            last_beat_age=age,
            timeout=timeout,
            multiplier=multiplier,
            restart_age=restart,
            baseline=baseline,
            current=current,
        )
        module_token = _drive_wedge_case(case)
        assert module_token == _inline_wedge_arm(case), WEDGE_ARM_NAMES[module_token]

    @given(
        since=st.one_of(
            st.floats(min_value=-1e6, max_value=1e6, allow_nan=False),
            st.sampled_from([200.0, 200.0 - 2.0**-10, float("inf"), float("nan"), -5.0]),
        ),
        interval=st.sampled_from([200.0, 333.25, 600.0]),
    )
    def test_alive_due_matches_the_inline_law(self, since: float, interval: float) -> None:
        """Module due-ness ≡ ``not (since < interval)`` (NaN ⇒ due — the
        strict-``<`` refusal is False on a NaN age, so the line emits)."""
        expected = not (since < interval)
        assert _drive_alive_case(since, interval) is expected


class TestEndToEndLifecycle:
    """One scripted hung→wedged lifecycle through the real (kernel-backed) module."""

    def _lifecycle(self) -> tuple[list[str], bool, str | None]:
        """Drive start → HUNG edge → wedge-window silence and trace the arms."""

        class _Clock:
            t = 0.0

            def __call__(self) -> float:
                return self.t

        clock = _Clock()
        hungs: list[object] = []
        wedges: list[object] = []
        wd = PullWatchdog(
            on_hung=hungs.append,
            timeout_seconds=30.0,
            spawn=lambda _fn: None,
            clock=clock,
            wedge_multiplier=6.0,
        )
        wd.on_wedged = wedges.append
        tokens = iter(["revA", "revA", "revB", "revB", "revB", "revB"])
        wd.revision_probe = lambda: next(tokens, "revB")
        wd.start()
        trace: list[str] = []
        # silence past the timeout → HUNG edge + baseline capture (revA)
        clock.t = 31.0
        wd.check_once()
        trace.append(f"hung={len(hungs)}")
        # past the wedge window: token still revA on this poll → ESCALATE?
        # no — the second draw is revA, equal baseline, no restart → fires.
        clock.t = 200.0
        wd.check_once()
        trace.append(f"wedges={len(wedges)}")
        trace.append(f"fired={wd._wedged_fired}")
        return trace, wd._wedged_fired, wd._hung_baseline_token

    def test_lifecycle_lands_the_pinned_trace(self) -> None:
        """The kernel-backed lifecycle fires exactly the pre-flip arms."""
        trace, fired, baseline = self._lifecycle()
        assert trace == ["hung=1", "wedges=1", "fired=True"]
        assert fired is True
        assert baseline == "revA"
