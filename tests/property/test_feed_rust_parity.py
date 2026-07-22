"""THE master parity gate for the feed-materialization conversion
(DEC-FEED-RUST-MATERIALIZE-002 over DEC-FEED-RUST-CORE-001).

The production Python oracle loop was DELETED — the strict Rust kernel is
the only decision path — so this module retains the deleted loop VERBATIM
as a **frozen in-test reference** (the ``tf_resampler`` frozen-oracle
idiom: the pre-conversion math preserved inside the test as the permanent
differential partner). Hypothesis-generated canonical bar tables — hostile
floats (exact ``prev * 4.0`` boundary products, ±1ulp neighbours,
negatives, ``0.0``, ``-0.0``, subnormals, NaN, ±inf, ``>= 1e16``,
``<= 1e-7``, the shortest-repr hard-case corpus), timestamps straddling
IST midnight, multi-session, backwards dates, µs-carrying instants — are
driven through the REAL :func:`brutex.feed.replay.arrow_table_to_bars`
AND the frozen oracle, asserting:

* bar-count + per-bar 7-field **``str()``-level** equality
  (``str(Decimal)`` per price — plain ``==`` is numeric-insensitive and
  would hide a trailing-zero re-key of the ``bxsnap2`` digest);
* exception parity (type AND ``str(exc)`` AND ``__cause__`` type) via
  dual capture;
* ``capture_logs`` event-list equality (order + full dicts);
* the ``BRUTEX_BAD_TICK_SCREEN=off`` arm (spikes kept, ts never
  consulted) under the same laws — INCLUDING the out-of-envelope
  timestamps (pre-1970 / ``>= 9999-12-31``) that the screen-ON unified
  path now refuses loudly (that refusal is pinned by the seam
  integration suite, not here — this gate's domain is the SERVED plane).

Skips loudly when pyarrow or the wheel is absent — without the kernel the
unified path refuses everything and the gate would be vacuous. The CI
property job now installs the brutex_feed wheel (the T17 wheel-required
posture); the gate ALSO runs in the unit-test job's whole-package
coverage pass.
"""

from __future__ import annotations

import math
import os
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from brutex.calendars.nse import IST
from brutex.data.bad_tick import BAD_TICK_ENABLED_ENV, BadTickScreen, bad_tick_screen_enabled
from brutex.data.models import CandleBar
from brutex.feed.replay import arrow_table_to_bars
from brutex.feed_rust import FEED_RUST_ENV, MATERIALIZE_RUST_ENV
from hypothesis import given, settings
from hypothesis import strategies as st
from structlog.testing import capture_logs

# Module-level guards: the parity tables need pyarrow ([data] extra) and the
# unified path needs the REQUIRED wheel.
pa = pytest.importorskip(
    "pyarrow", reason="pyarrow not installed — the parity tables need the [data] extra"
)
pytest.importorskip(
    "brutex_feed",
    reason="brutex_feed wheel not installed — the wheel-required path needs the kernel",
)

_SOURCE = "NSE/INDEX/NSE-NIFTY/1minute"

#: 09:15 IST on 2024-01-02.
_BASE = datetime(2024, 1, 2, 3, 45, tzinfo=UTC)

#: 23:59:59.999999 IST (same session) and 00:00:00 IST (next session) — the
#: div_euclid midnight boundary.
_IST_MIDNIGHT_MINUS = datetime(2024, 1, 2, 18, 29, 59, 999999, tzinfo=UTC)
_IST_MIDNIGHT = datetime(2024, 1, 2, 18, 30, 0, tzinfo=UTC)

#: In-ENVELOPE instants (every ts inside ``[epoch 0, 9999-12-31T00:00:00Z)``
#: where the kernel's fixed +05:30 civil-date law is provable): in-session
#: minutes, midnight straddles, other sessions (multi-session + backwards
#: orderings arise from list sampling), µs-carrying, epoch-adjacent and
#: far-future boundary neighbours.
_TS_ENVELOPE_POOL: list[datetime] = [
    _BASE,
    _BASE + timedelta(minutes=1),
    _BASE + timedelta(minutes=2, microseconds=123456),
    _IST_MIDNIGHT_MINUS,
    _IST_MIDNIGHT,
    _BASE + timedelta(days=1),
    _BASE - timedelta(days=3),
    datetime(1970, 1, 1, 0, 0, 0, tzinfo=UTC),  # epoch — the inclusive floor
    datetime(9999, 12, 30, 23, 59, 59, tzinfo=UTC),  # just inside the ceiling
]

#: OUT-of-envelope instants: with the screen ON the unified path REFUSES
#: these loudly (seam-suite pinned); with the screen OFF the ts is never
#: consulted and parity must hold — so they join ONLY the screen-killed
#: strategy here.
_TS_OUT_OF_ENVELOPE: list[datetime] = [
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
_volume = st.one_of(st.sampled_from([10, 0, -5]), st.integers(min_value=0, max_value=10**9))
_oi = st.one_of(st.none(), st.integers(min_value=-2, max_value=100))
_ts_envelope = st.sampled_from(_TS_ENVELOPE_POOL)
_ts_any = st.sampled_from(_TS_ENVELOPE_POOL + _TS_OUT_OF_ENVELOPE)

_row_envelope = st.tuples(_ts_envelope, _price, _price, _price, _price, _volume, _oi)
_rows_envelope = st.lists(_row_envelope, min_size=0, max_size=10)
_row_any = st.tuples(_ts_any, _price, _price, _price, _price, _volume, _oi)
_rows_any = st.lists(_row_any, min_size=0, max_size=10)


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


# ────────────────────────────────────────────────────────────────────────
# The FROZEN Python oracle — the deleted production loop, VERBATIM.
# ────────────────────────────────────────────────────────────────────────
# Transcribed byte-for-byte (modulo the local helper names) from the
# pre-T17 `brutex/feed/replay.py` retained-oracle path: the per-field
# finiteness→positivity refusal order, the BadTickScreen consult, and the
# `Decimal(str(price))` / wrapped-constructor materialization expressions.
# DO NOT "improve" this code — it is the frozen differential partner; any
# behavioral edit here voids the gate.


def _frozen_nonfinite_error(origin: str, ts: Any, price_name: str, price: float) -> ValueError:
    return ValueError(
        f"corrupt stored bar{origin} at timestamp {ts!r}: "
        f"{price_name}={price} is a non-finite price (the vendor parse "
        f"rejects inf/NaN — a stored non-finite price is planted/corrupt "
        f"data) — refusing to serve corrupt data to the engine "
        f"(DEC-LAKE-READ-NONFINITE-OHLC-GUARD-001)"
    )


def _frozen_positivity_error(origin: str, ts: Any, price_name: str, price: float) -> ValueError:
    return ValueError(
        f"corrupt stored bar{origin} at timestamp {ts!r}: "
        f"{price_name}={price} is a non-positive price (a traded Indian "
        f"price is never <= 0) — refusing to serve corrupt data to the "
        f"engine (DEC-STORED-DATA-VALIDATION-001)"
    )


def _frozen_build_bar(
    origin: str,
    ts: Any,
    o: float,
    h: float,
    lo: float,
    c: float,
    v: int,
    oi: int | None,
) -> CandleBar:
    try:
        return CandleBar(
            timestamp=ts,
            open=Decimal(str(o)),
            high=Decimal(str(h)),
            low=Decimal(str(lo)),
            close=Decimal(str(c)),
            volume=int(v),
            open_interest=int(oi) if oi is not None else None,
        )
    except (ValueError, ArithmeticError) as exc:
        raise ValueError(
            f"corrupt stored bar{origin} at timestamp {ts!r}: {exc} — "
            f"refusing to serve corrupt data to the engine "
            f"(DEC-STORED-DATA-VALIDATION-001)"
        ) from exc


def frozen_oracle_bars(table: Any, *, source: str | None = None) -> list[CandleBar]:
    """The deleted production oracle loop, preserved verbatim in-test."""
    if table.num_rows == 0:
        return []

    timestamps: list[Any] = table.column("timestamp").to_pylist()
    opens: list[float] = table.column("open").to_pylist()
    highs: list[float] = table.column("high").to_pylist()
    lows: list[float] = table.column("low").to_pylist()
    closes: list[float] = table.column("close").to_pylist()
    volumes: list[int] = table.column("volume").to_pylist()
    open_interests: list[int | None] = table.column("open_interest").to_pylist()

    screen_on = bad_tick_screen_enabled()
    screen = BadTickScreen(source=source) if screen_on else None

    origin = f" in {source}" if source is not None else ""
    bars: list[CandleBar] = []
    for ts, o, h, lo, c, v, oi in zip(
        timestamps, opens, highs, lows, closes, volumes, open_interests, strict=True
    ):
        for price_name, price in (("open", o), ("high", h), ("low", lo), ("close", c)):
            if not math.isfinite(price):
                raise _frozen_nonfinite_error(origin, ts, price_name, price)
            if price <= 0:
                raise _frozen_positivity_error(origin, ts, price_name, price)
        if screen is not None and not screen.keep(ist_date=ts.astimezone(IST).date(), close=c):
            continue
        bars.append(_frozen_build_bar(origin, ts, o, h, lo, c, v, oi))
    if screen is not None:
        screen.finish()
    return bars


# ────────────────────────────────────────────────────────────────────────
# Dual-capture harness
# ────────────────────────────────────────────────────────────────────────


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
    fn: Any, table: Any, screen: str | None
) -> tuple[list[Any] | None, Exception | None, list[Any]]:
    """One arm: bars-or-exception + the full log record sequence."""
    with (
        _env(MATERIALIZE_RUST_ENV, None),
        _env(FEED_RUST_ENV, None),
        _env(BAD_TICK_ENABLED_ENV, screen),
        capture_logs() as logs,
    ):
        bars: list[Any] | None = None
        exc: Exception | None = None
        try:
            bars = fn(table, source=_SOURCE)
        except Exception as caught:  # dual-capture: parity is asserted on the pair
            exc = caught
    return bars, exc, list(logs)


def _assert_unified_matches_frozen(table: Any, *, screen: str | None) -> None:
    ref_bars, ref_exc, ref_logs = _observe(frozen_oracle_bars, table, screen)
    got_bars, got_exc, got_logs = _observe(arrow_table_to_bars, table, screen)
    if ref_exc is None:
        assert got_exc is None, (
            f"the unified path raised where the frozen oracle served: {got_exc!r}"
        )
        assert ref_bars is not None and got_bars is not None
        assert len(ref_bars) == len(got_bars)
        for a, b in zip(ref_bars, got_bars, strict=True):
            assert a.timestamp.isoformat() == b.timestamp.isoformat()
            assert str(a.open) == str(b.open)
            assert str(a.high) == str(b.high)
            assert str(a.low) == str(b.low)
            assert str(a.close) == str(b.close)
            assert repr(a.volume) == repr(b.volume)
            assert repr(a.open_interest) == repr(b.open_interest)
    else:
        assert got_exc is not None, "the frozen oracle raised where the unified path served"
        assert type(got_exc) is type(ref_exc)
        assert str(got_exc) == str(ref_exc)
        assert type(got_exc.__cause__) is type(ref_exc.__cause__)
    assert ref_logs == got_logs


@settings(deadline=None)
@given(rows=_rows_envelope)
def test_unified_equals_frozen_oracle_screen_default(rows: list[tuple[Any, ...]]) -> None:
    """Unified ≡ frozen oracle over hostile in-envelope tables (screen ON)."""
    _assert_unified_matches_frozen(_table(rows), screen=None)


@settings(deadline=None)
@given(rows=_rows_any)
def test_unified_equals_frozen_oracle_screen_killed(rows: list[tuple[Any, ...]]) -> None:
    """Unified ≡ frozen oracle with ``BRUTEX_BAD_TICK_SCREEN=off`` — spikes
    kept, ts NEVER consulted, so even out-of-envelope timestamps must serve
    identically (the screen-ON refusal on those is the seam suite's pin)."""
    _assert_unified_matches_frozen(_table(rows), screen="off")
