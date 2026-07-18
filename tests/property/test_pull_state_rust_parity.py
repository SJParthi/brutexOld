"""Rust-arm ≡ Python-oracle differential for the market-hours guard
(DEC-RUST-PULL-STATE-001, lane Phase 1 — the ``test_feed_rust_parity`` shape).

Skips cleanly when the ``brutex_pull_state`` wheel is not built (the sibling
kernels' convention — the wheel is OPTIONAL and the flag default-OFF, so a
wheel-less box never fails here). With the wheel present it proves:

* the hypothesis differential — the facade's ``rust_pull_allowed`` equals the
  retained Python oracle expression over ARBITRARY wall-clock instants
  (microsecond granularity) × dates spanning pre-coverage, the full covered
  NSE calendar, and post-coverage (the fail-safe arm), with the trading-day
  flag resolved by the ONE Python calendar (never re-minted in Rust);
* the frozen-golden sweep — the Rust arm reproduces EVERY verdict bit of the
  committed ``market_hours_guard_verdicts.json`` fixture (the whole covered
  calendar × the boundary/representative probe times + the out-of-coverage
  fail-safe rows) — the lane's byte-identity contract, served by the KERNEL.
"""

from __future__ import annotations

import json
from datetime import date, datetime, time
from pathlib import Path

import pytest
from brutex.calendars.market_hours import BLOCK_END, BLOCK_START
from brutex.calendars.nse import IST, is_trading_day
from brutex.pull_state_rust import rust_pull_allowed
from hypothesis import given
from hypothesis import strategies as st

pytest.importorskip("brutex_pull_state", reason="brutex_pull_state wheel not installed")

_FIXTURE = (
    Path(__file__).resolve().parents[1] / "golden" / "fixtures" / "market_hours_guard_verdicts.json"
)

#: The window boundaries + one-microsecond neighbours — always worth mixing
#: into the arbitrary-time draw (the exactness of the boundary law is the
#: whole point of the microsecond-of-day packing).
_BOUNDARY_TIMES = (
    time(8, 44, 59, 999999),
    time(8, 45, 0),
    time(8, 45, 0, 1),
    time(15, 34, 59, 999999),
    time(15, 35, 0),
    time(15, 35, 0, 1),
)


def _oracle(ist_dt: datetime, trading: bool) -> bool:
    """The VERBATIM retained Python arm of ``pull_allowed_now`` (the oracle)."""
    if not trading:
        return True
    return not (BLOCK_START <= ist_dt.time() < BLOCK_END)


@given(
    d=st.dates(min_value=date(2019, 1, 1), max_value=date(2030, 12, 31)),
    t=st.one_of(st.times(), st.sampled_from(_BOUNDARY_TIMES)),
)
def test_rust_arm_equals_python_oracle(d: date, t: time) -> None:
    """kernel(us_of_day, flag) ≡ the oracle, calendar-resolved flag included."""
    ist_dt = datetime.combine(d, t, tzinfo=IST)
    trading = is_trading_day(d)
    verdict = rust_pull_allowed(ist_dt, trading)
    # The wheel is installed (importorskip above): a None here would mean a
    # degrade fired — that is a FAILURE of the differential, never a skip.
    assert verdict is not None
    assert verdict is _oracle(ist_dt, trading)


@given(
    d=st.dates(min_value=date(2019, 1, 1), max_value=date(2030, 12, 31)),
    t=st.one_of(st.times(), st.sampled_from(_BOUNDARY_TIMES)),
    trading=st.booleans(),
)
def test_rust_arm_equals_python_oracle_forced_flag(d: date, t: time, trading: bool) -> None:
    """Both flag polarities, decoupled from the calendar (full input plane)."""
    ist_dt = datetime.combine(d, t, tzinfo=IST)
    verdict = rust_pull_allowed(ist_dt, trading)
    assert verdict is not None
    assert verdict is _oracle(ist_dt, trading)


def test_rust_arm_reproduces_the_frozen_golden() -> None:
    """The kernel serves EVERY verdict bit of the committed golden fixture.

    The lane's byte-identity contract: the frozen capture (pre-conversion
    Python over the whole covered NSE calendar + the out-of-coverage
    fail-safe rows) is reproduced by the RUST arm — 2557 days × 10 probes +
    the out-of-coverage rows, every bit compared.
    """
    frozen = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    probe_times = [time.fromisoformat(t) for t in frozen["probe_times"]]
    checked = 0
    for section in ("days", "out_of_coverage"):
        for day_iso, (trading_int, bits) in frozen[section].items():
            d = date.fromisoformat(day_iso)
            trading = bool(trading_int)
            for probe, expected_bit in zip(probe_times, bits, strict=True):
                ist_dt = datetime.combine(d, probe, tzinfo=IST)
                verdict = rust_pull_allowed(ist_dt, trading)
                assert verdict is not None
                assert verdict is (expected_bit == "1"), (day_iso, probe)
                checked += 1
    # Anti-vacuous: the sweep genuinely covered the whole fixture.
    assert checked == (2557 + 4) * len(probe_times)
