"""THE master dual-arm parity gate for DEC-FEED-RUST-CORE-001.

Hypothesis-generated canonical bar tables — hostile floats (exact
``prev * 4.0`` boundary products, ±1ulp neighbours, negatives, ``0.0``,
``-0.0``, subnormals, NaN, ±inf, ``>= 1e16``, ``<= 1e-7``, the shortest-repr
hard-case corpus), timestamps straddling IST midnight, multi-session,
backwards dates, µs-carrying instants, pre-1970 + ``>= 9999-12-31`` degrade
lanes, OI-null mixes, and null-price/volume/timestamp degrade variants —
driven through the REAL :func:`brutex.feed.replay.arrow_table_to_bars` with
``BRUTEX_FEED_RUST`` OFF then ON, asserting:

* bar-count + per-bar 7-field **``str()``-level** equality (``str(Decimal)``
  per price — plain ``==`` is numeric-insensitive and would hide a
  trailing-zero re-key of the ``bxsnap2`` digest);
* exception parity (type AND ``str(exc)`` AND ``__cause__`` type) via dual
  capture;
* ``capture_logs`` event-list equality (order + full dicts) over every
  non-fallback event (the ON arm's own ``feed_rust.fallback`` degrade
  markers are the ONE sanctioned difference);
* the ``BRUTEX_BAD_TICK_SCREEN=off`` × flag-ON arm (spikes kept, no ts ever
  crossing the FFI) under the same laws.

Skips loudly when pyarrow or the wheel is absent — without the kernel every
ON arm would degrade to the oracle and the gate would be vacuous. The CI
property job is `.[dev]`-only (no pyarrow), so this module's real CI
execution home is the unit-test job, whose [dev,data,ui] env + wheel install
runs tests/property inside the whole-package coverage pass.
"""

from __future__ import annotations

import math
import os
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from brutex.data.bad_tick import BAD_TICK_ENABLED_ENV
from brutex.feed.replay import arrow_table_to_bars
from brutex.feed_rust import (
    FEED_RUST_ENV,
    FEED_RUST_FALLBACK_EVENT,
    FEED_RUST_FLAG_EVENT,
    reset_for_tests,
)
from hypothesis import given, settings
from hypothesis import strategies as st
from structlog.testing import capture_logs

# Module-level guards (the property job installs `.[dev]` ONLY — no [data]
# extra, so pyarrow must importorskip exactly like the sibling polars guards;
# the dual-arm gate's REAL CI execution home is the unit-test job, whose
# [dev,data,ui] env + wheel install runs tests/property in the coverage pass).
pa = pytest.importorskip(
    "pyarrow", reason="pyarrow not installed — the parity tables need the [data] extra"
)
pytest.importorskip(
    "brutex_feed",
    reason="brutex_feed wheel not installed — the dual-arm parity gate needs the kernel",
)

_SOURCE = "NSE/INDEX/NSE-NIFTY/1minute"

#: 09:15 IST on 2024-01-02.
_BASE = datetime(2024, 1, 2, 3, 45, tzinfo=UTC)

#: 23:59:59.999999 IST (same session) and 00:00:00 IST (next session) — the
#: div_euclid midnight boundary.
_IST_MIDNIGHT_MINUS = datetime(2024, 1, 2, 18, 29, 59, 999999, tzinfo=UTC)
_IST_MIDNIGHT = datetime(2024, 1, 2, 18, 30, 0, tzinfo=UTC)

#: Interesting instants: in-session minutes, midnight straddles, other
#: sessions (multi-session + backwards orderings arise from list sampling),
#: µs-carrying, and the two DEGRADE lanes (pre-1970; >= 9999-12-31).
_TS_POOL: list[datetime] = [
    _BASE,
    _BASE + timedelta(minutes=1),
    _BASE + timedelta(minutes=2, microseconds=123456),
    _IST_MIDNIGHT_MINUS,
    _IST_MIDNIGHT,
    _BASE + timedelta(days=1),
    _BASE - timedelta(days=3),
    datetime(1969, 12, 31, 23, 59, 59, tzinfo=UTC),
    datetime(9999, 12, 31, 0, 0, tzinfo=UTC),
]

#: Hostile float pool: shortest-repr hard cases, exact 4.0x boundary
#: products + their ±1ulp neighbours, magnitude extremes, specials.
_PRICE_POOL: list[float] = [
    20000.0,
    20000.5,
    100.5,
    99.95,
    27940.0,
    2794.5,
    6000.0,
    0.1 + 0.2,
    1e15,
    1e16,
    1e-4,
    1e-5,
    1e-7,
    100.0,
    400.0,  # exactly 100.0 * 4.0 — the inclusive boundary
    math.nextafter(400.0, 0.0),
    math.nextafter(400.0, math.inf),
    25.0,  # exactly 100.0 / 4.0
    5e-324,  # subnormal
    0.0,
    -0.0,
    -42.5,
    float("nan"),
    float("inf"),
    float("-inf"),
]

_price = st.one_of(
    st.sampled_from(_PRICE_POOL),
    st.floats(min_value=0.01, max_value=1_000_000.0, allow_nan=False, allow_infinity=False),
)
_maybe_null_price = st.one_of(_price, st.just(None))
_volume = st.one_of(st.sampled_from([10, 0, -5]), st.integers(min_value=0, max_value=10**9))
_maybe_null_volume = st.one_of(_volume, st.just(None))
_oi = st.one_of(st.none(), st.integers(min_value=-2, max_value=100))
_ts = st.sampled_from(_TS_POOL)
_maybe_null_ts = st.one_of(_ts, st.just(None))

_row = st.tuples(_ts, _price, _price, _price, _price, _volume, _oi)
_rows = st.lists(_row, min_size=0, max_size=10)

#: Degrade-lane rows: nullable timestamp / prices / volume.
_hostile_row = st.tuples(
    _maybe_null_ts, _maybe_null_price, _price, _price, _maybe_null_price, _maybe_null_volume, _oi
)
_hostile_rows = st.lists(_hostile_row, min_size=1, max_size=6)


def _table(rows: list[tuple[Any, ...]]) -> Any:
    return pa.table(
        {
            "timestamp": pa.array([r[0] for r in rows], type=pa.timestamp("us", tz="UTC")),
            "open": pa.array([r[1] for r in rows], type=pa.float64()),
            "high": pa.array([r[2] for r in rows], type=pa.float64()),
            "low": pa.array([r[3] for r in rows], type=pa.float64()),
            "close": pa.array([r[4] for r in rows], type=pa.float64()),
            "volume": pa.array([r[5] for r in rows], type=pa.int64()),
            "open_interest": pa.array([r[6] for r in rows], type=pa.int64()),
        }
    )


@contextmanager
def _env(name: str, value: str | None) -> Generator[None]:
    """Set/unset one env var, restoring the prior state (hypothesis-safe —
    function-scoped monkeypatch fixtures do not mix with ``@given``)."""
    old = os.environ.get(name)
    try:
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
        yield
    finally:
        if old is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = old


def _observe(
    table: Any, flag: str | None, screen: str | None
) -> tuple[list[Any] | None, Exception | None, list[Any]]:
    """One arm: bars-or-exception + the non-fallback log record sequence."""
    reset_for_tests()
    with _env(FEED_RUST_ENV, flag), _env(BAD_TICK_ENABLED_ENV, screen), capture_logs() as logs:
        bars: list[Any] | None = None
        exc: Exception | None = None
        try:
            bars = arrow_table_to_bars(table, source=_SOURCE)
        except Exception as caught:  # dual-capture: parity is asserted on the pair
            exc = caught
    filtered = [
        rec for rec in logs if rec["event"] not in {FEED_RUST_FALLBACK_EVENT, FEED_RUST_FLAG_EVENT}
    ]
    return bars, exc, filtered


def _assert_arms_identical(table: Any, *, screen: str | None) -> None:
    off_bars, off_exc, off_logs = _observe(table, None, screen)
    on_bars, on_exc, on_logs = _observe(table, "1", screen)
    if off_exc is None:
        assert on_exc is None, f"ON arm raised where OFF served: {on_exc!r}"
        assert off_bars is not None and on_bars is not None
        assert len(off_bars) == len(on_bars)
        for a, b in zip(off_bars, on_bars, strict=True):
            assert a.timestamp.isoformat() == b.timestamp.isoformat()
            assert str(a.open) == str(b.open)
            assert str(a.high) == str(b.high)
            assert str(a.low) == str(b.low)
            assert str(a.close) == str(b.close)
            assert repr(a.volume) == repr(b.volume)
            assert repr(a.open_interest) == repr(b.open_interest)
    else:
        assert on_exc is not None, "OFF arm raised where ON served"
        assert type(on_exc) is type(off_exc)
        assert str(on_exc) == str(off_exc)
        assert type(on_exc.__cause__) is type(off_exc.__cause__)
    assert off_logs == on_logs


@settings(deadline=None)
@given(rows=_rows)
def test_dual_arm_parity_screen_default(rows: list[tuple[Any, ...]]) -> None:
    """OFF ≡ ON over hostile-but-non-null tables (screen at its default ON)."""
    _assert_arms_identical(_table(rows), screen=None)


@settings(deadline=None)
@given(rows=_rows)
def test_dual_arm_parity_screen_killed(rows: list[tuple[Any, ...]]) -> None:
    """OFF ≡ ON with ``BRUTEX_BAD_TICK_SCREEN=off`` (spikes kept, ts_us=[])."""
    _assert_arms_identical(_table(rows), screen="off")


@settings(deadline=None)
@given(rows=_hostile_rows)
def test_dual_arm_parity_null_degrade_lanes(rows: list[tuple[Any, ...]]) -> None:
    """Null ts/price/volume variants: the ON arm degrades and stays identical
    (including the oracle's own TypeError/AttributeError escapes)."""
    _assert_arms_identical(_table(rows), screen=None)
