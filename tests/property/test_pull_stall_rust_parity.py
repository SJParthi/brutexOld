"""Rust-arm ≡ Python-oracle differential for the pull-stall decision plane
(DEC-RUST-PULL-STATE-001, Wave 1C — the ``test_pull_state_rust_parity`` shape).

Skips cleanly when the ``brutex_pull_state`` wheel is not built, AND when an
installed wheel predates Wave 1C (the capability probe — a stale phase-1
wheel lacks ``wedge_decision``; the facade would degrade it as
``wheel_stale``, so parity is meaningless until the integrator's merged
wheel is rebuilt). With a current wheel present it proves:

* the frozen-golden sweep — the KERNEL reproduces EVERY arm token / due bit
  of the committed ``pull_stall_verdicts.json`` fixture (floats decoded from
  their IEEE-754 bit patterns — no parse rounding anywhere);
* the whole-module differential — the SAME whitebox scenario driven through
  the REAL ``PullWatchdog`` twice (flag OFF = the Python oracle, flag ON =
  the kernel seam) classifies the SAME arm via the shared golden harness,
  over hypothesis-drawn guard flags × ages (NaN/inf/negatives included) ×
  token shapes × window geometries — no re-derived oracle anywhere;
* the alive-throttle differential — payloads (not just due-ness) equal on
  both arms over hostile since/interval draws.
"""

# Whitebox drives of module-private state (sanctioned — the unit suite precedent).
# pyright: reportPrivateUsage=none

from __future__ import annotations

import json
import os
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

_wheel = pytest.importorskip("brutex_pull_state", reason="brutex_pull_state wheel not installed")
if not hasattr(_wheel, "wedge_decision"):  # the Wave-1C capability probe
    pytest.skip(
        "brutex_pull_state wheel predates Wave 1C (no pull_stall symbols) — "
        "rebuild via `maturin develop --release` in rust/brutex-pull-state",
        allow_module_level=True,
    )

from brutex.dashboard.pull_stall import PullWatchdog  # noqa: E402
from brutex.pull_state_rust import PULL_STATE_RUST_ENV, reset_for_tests  # noqa: E402

from tests.golden._pull_stall_harness import (  # noqa: E402
    WEDGE_ARM_NAMES,
    WedgeCase,
    _drive_alive_case,
    _drive_wedge_case,
    f64_from_bits,
)

_FIXTURE = Path(__file__).resolve().parents[1] / "golden" / "fixtures" / "pull_stall_verdicts.json"


@contextmanager
def _flag(value: str | None) -> Generator[None]:
    """Set/unset the seam flag around a drive (restored exactly)."""
    saved = os.environ.get(PULL_STATE_RUST_ENV)
    try:
        if value is None:
            os.environ.pop(PULL_STATE_RUST_ENV, None)
        else:
            os.environ[PULL_STATE_RUST_ENV] = value
        yield
    finally:
        if saved is None:
            os.environ.pop(PULL_STATE_RUST_ENV, None)
        else:
            os.environ[PULL_STATE_RUST_ENV] = saved
        reset_for_tests()


@pytest.fixture(scope="module")
def frozen() -> dict[str, Any]:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


class TestKernelReproducesTheFrozenGolden:
    def test_every_wedge_row(self, frozen: dict[str, Any]) -> None:
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
        for row in frozen["alive"]:
            got = _wheel.alive_log_due(
                f64_from_bits(row["since_last_bits"]),
                f64_from_bits(row["interval_bits"]),
            )
            assert got is row["expected_due"], row["label"]


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
    """Flag OFF vs flag ON through the REAL module — the harness classifies both."""

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
    def test_arm_classification_is_identical(
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
        with _flag(None):
            oracle_token = _drive_wedge_case(case)
        with _flag("1"):
            kernel_token = _drive_wedge_case(case)
        assert kernel_token == oracle_token, WEDGE_ARM_NAMES[oracle_token]

    @given(
        since=st.one_of(
            st.floats(min_value=-1e6, max_value=1e6, allow_nan=False),
            st.sampled_from([200.0, 200.0 - 2.0**-10, float("inf"), float("nan"), -5.0]),
        ),
        interval=st.sampled_from([200.0, 333.25, 600.0]),
    )
    def test_alive_due_is_identical(self, since: float, interval: float) -> None:
        with _flag(None):
            oracle_due = _drive_alive_case(since, interval)
        with _flag("1"):
            kernel_due = _drive_alive_case(since, interval)
        assert kernel_due is oracle_due


class TestEndToEndSeamParity:
    """One scripted lifecycle, both arms: identical events, latch and payloads."""

    def _lifecycle(self) -> tuple[list[str], bool, str | None]:
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

    def test_lifecycle_traces_match(self) -> None:
        with _flag(None):
            off = self._lifecycle()
        with _flag("1"):
            on = self._lifecycle()
        assert on == off
        assert off[0] == ["hung=1", "wedges=1", "fired=True"]
