"""Deterministic, seeded, O(1)-per-bar synthetic candle generator.

This is the pure maths core of the SYNTHETIC adapter (DEC-SYNTHETIC-ADAPTER-001).
It has NO network, NO global RNG, NO wall-clock reads — every value comes from a
stable :func:`hashlib.blake2b` digest of ``(seed, symbol, …)``, so the SAME
``(seed, symbol, interval, start, end)`` always yields **bit-identical** bars,
across processes and Python runs (Golden Rule #4 reproducibility / #7
idempotence). We deliberately hash ``blake2b`` bytes rather than Python's
salted built-in ``hash()`` so the determinism survives a fresh interpreter
regardless of ``PYTHONHASHSEED``.

## Why it is O(1) per bar

A bar at timestamp ``ts`` is derived **only** from ``ts`` (via its absolute
minute ordinal) and the seed — never from any prior bar. The price path looks
continuous because the noise function is smooth across adjacent ordinals, not
because we accumulate a running sum. Each bar costs a FIXED number of hash
evaluations (``NOISE_OCTAVES`` per price sample, plus two single draws), so the
per-bar cost is constant — a million-bar range is a million × constant, with no
scan of earlier bars anywhere.

## OHLC + tick-grid discipline

Prices are exact :class:`~decimal.Decimal` rupees, snapped to the 2-decimal
tick grid via :func:`brutex.money.round_money` (``ROUND_HALF_UP``). After
snapping, ``high``/``low`` are clamped to bound ``open``/``close`` (the same
mathematically-correct bound :func:`brutex.data.candle_fields.assemble_candle`
uses), so every emitted :class:`~brutex.data.models.CandleBar` satisfies its
``low <= open, close <= high`` invariant by construction.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Final

import structlog

from brutex.adapters.synthetic.constants import (
    ANCHOR_MINUTES,
    EPOCH,
    INTERVAL_TO_MINUTES,
    NOISE_OCTAVES,
    SyntheticSymbolSpec,
)
from brutex.calendars import IST, get_session, is_holiday_data_available
from brutex.data.models import CandleBar
from brutex.money import round_money

_LOG = structlog.get_logger(__name__)

#: Field separator hashed between parts so ``("a", "bc")`` and ``("ab", "c")``
#: never collide to the same digest.
_FIELD_SEP: Final[bytes] = b"\x1f"
#: Denominator that maps a 64-bit digest into a float in ``[0, 1)``.
_U64_DENOM: Final[float] = float(1 << 64)
#: Digest width (bytes) — 8 bytes = 64 bits.
_DIGEST_BYTES: Final[int] = 8
#: Centring offset so a unit-noise sample is symmetric about zero.
_HALF: Final[float] = 0.5
#: Smoothstep cubic coefficients (``3t² − 2t³``) — C¹-continuous interpolation.
_SMOOTH_A: Final[float] = 3.0
_SMOOTH_B: Final[float] = 2.0
#: Volume shaping: a per-bar multiplier in ``[_VOL_FLOOR, _VOL_FLOOR + 1)``.
_VOL_FLOOR: Final[float] = 0.5


def _u64(*parts: object) -> int:
    """Return a stable 64-bit unsigned int from the string forms of *parts*.

    Uses :func:`hashlib.blake2b` (unsalted) so the value is identical across
    processes and interpreter restarts — the bedrock of the generator's
    reproducibility.
    """
    digest = hashlib.blake2b(digest_size=_DIGEST_BYTES)
    for part in parts:
        digest.update(str(part).encode())
        digest.update(_FIELD_SEP)
    return int.from_bytes(digest.digest(), "big")


def _unit(*parts: object) -> float:
    """Return a deterministic float in ``[0, 1)`` from *parts*."""
    return _u64(*parts) / _U64_DENOM


def _value_noise(seed: int, symbol: str, x: float) -> float:
    """Smooth fractal value-noise at position *x*, in roughly ``[-0.5, 0.5]``.

    Sums :data:`~brutex.adapters.synthetic.constants.NOISE_OCTAVES` octaves of
    hash-sampled, smoothstep-interpolated noise. The result is continuous in
    *x* (so adjacent bars relate to one another) yet every sample is an O(1)
    pure function of ``(seed, symbol, x)`` — no accumulation over prior bars.
    """
    total = 0.0
    amplitude = 1.0
    frequency = 1.0
    norm = 0.0
    for octave in range(NOISE_OCTAVES):
        xi = x * frequency
        lattice = int(xi // 1)  # floor toward -inf for the lower anchor
        frac = xi - lattice
        low = _unit(seed, symbol, octave, lattice)
        high = _unit(seed, symbol, octave, lattice + 1)
        smooth = frac * frac * (_SMOOTH_A - _SMOOTH_B * frac)
        total += amplitude * (low + (high - low) * smooth - _HALF)
        norm += amplitude
        amplitude *= _HALF
        frequency *= _SMOOTH_B
    return total / norm


def minute_ordinal(timestamp: datetime) -> int:
    """Return the absolute minute ordinal of *timestamp* since the epoch.

    An O(1) function of the timestamp alone — the index that makes per-bar
    generation independent of every other bar.
    """
    return int((timestamp - EPOCH).total_seconds() // 60)


def bar_timestamps(interval: str, start: date, end: date) -> Iterator[datetime]:
    """Yield the UTC bar-open timestamps for *interval* over ``[start, end)``.

    Iterates the real NSE/BSE calendar: a bar exists only inside an actual
    trading session (regular, Muhurat, or special-weekend), anchored at the
    session open in IST and stepping by the interval's minutes until the
    session close. ``1day`` (375-minute step) therefore yields exactly one bar
    per session. A range with no trading sessions yields nothing.

    Out-of-coverage honesty (calendar-correctness pack): the generator
    DELIBERATELY stays on the unguarded :func:`~brutex.calendars.get_session`
    — it fabricates data by design, and any-date deterministic generation is
    its purpose — but when the requested span reaches OUTSIDE the published
    holiday coverage it emits ONE ``synthetic.calendar_coverage_exceeded``
    structlog warning per call: out-of-coverage weekdays are all treated as
    regular sessions (an unlisted real-world holiday would still get bars),
    stated loudly instead of silently.

    Raises
    ------
    ValueError
        If *interval* is not a supported synthetic interval (fail-loud — never
        silently default a bar spacing, Golden Rule #1).
    """
    if interval not in INTERVAL_TO_MINUTES:
        raise ValueError(f"unsupported synthetic interval: {interval!r}")
    last_day = end - timedelta(days=1)
    if start <= last_day and not (
        is_holiday_data_available(start) and is_holiday_data_available(last_day)
    ):
        # Coverage is one contiguous year span, so the two endpoints decide
        # whether ANY requested day falls outside it. One warning per call.
        _LOG.warning(
            "synthetic.calendar_coverage_exceeded",
            start=start.isoformat(),
            end=end.isoformat(),
            detail=(
                "requested synthetic range reaches outside the published holiday "
                "coverage; out-of-coverage weekdays are generated as regular "
                "sessions (synthetic-by-design, never real data)"
            ),
        )
    step = timedelta(minutes=INTERVAL_TO_MINUTES[interval])
    day = start
    while day < end:
        session = get_session(day)
        if session is not None:
            cursor = datetime.combine(day, session.open_time_ist, tzinfo=IST)
            close = datetime.combine(day, session.close_time_ist, tzinfo=IST)
            while cursor < close:
                yield cursor.astimezone(UTC)
                cursor += step
        day += timedelta(days=1)


def generate_bar(
    *, seed: int, spec: SyntheticSymbolSpec, interval: str, timestamp: datetime
) -> CandleBar:
    """Generate one deterministic :class:`CandleBar` for *spec* at *timestamp*.

    The open level is the smooth noise at this bar's ordinal; the close level
    is the noise one interval ahead (so consecutive intraday closes meet the
    next open — an overnight gap appears naturally across sessions). An
    independent per-bar draw sets the wick amplitude. All four prices are
    snapped to the 2dp tick grid, then ``high``/``low`` are clamped to bound
    ``open``/``close`` so the bar is always valid.
    """
    tf_minutes = INTERVAL_TO_MINUTES[interval]
    ordinal = minute_ordinal(timestamp)
    x_open = ordinal / ANCHOR_MINUTES
    x_close = (ordinal + tf_minutes) / ANCHOR_MINUTES

    open_factor = 1.0 + spec.vol_frac * _value_noise(seed, spec.symbol, x_open)
    close_factor = 1.0 + spec.vol_frac * _value_noise(seed, spec.symbol, x_close)
    open_raw = spec.base_price * Decimal(str(open_factor))
    close_raw = spec.base_price * Decimal(str(close_factor))

    wick = spec.wick_frac * _unit(seed, spec.symbol, interval, ordinal, "wick")
    body_high = open_raw if open_raw >= close_raw else close_raw
    body_low = close_raw if open_raw >= close_raw else open_raw
    high_raw = body_high * Decimal(str(1.0 + wick))
    low_raw = body_low * Decimal(str(1.0 - wick))

    open_px = round_money(open_raw)
    close_px = round_money(close_raw)
    high_px = max(round_money(high_raw), open_px, close_px)
    low_px = min(round_money(low_raw), open_px, close_px)

    if spec.is_index:
        volume = 0
    else:
        scale = _VOL_FLOOR + _unit(seed, spec.symbol, interval, ordinal, "vol")
        volume = int(spec.base_volume * tf_minutes * scale)

    return CandleBar(
        timestamp=timestamp,
        open=open_px,
        high=high_px,
        low=low_px,
        close=close_px,
        volume=volume,
        open_interest=None,
    )


def generate_candles(
    *, seed: int, spec: SyntheticSymbolSpec, interval: str, start: date, end: date
) -> list[CandleBar]:
    """Generate the full ascending-timestamp bar list for ``[start, end)``.

    Pure and idempotent: the same arguments always return a bit-identical list.
    Raises :class:`ValueError` (via :func:`bar_timestamps`) for an unsupported
    interval.
    """
    return [
        generate_bar(seed=seed, spec=spec, interval=interval, timestamp=ts)
        for ts in bar_timestamps(interval, start, end)
    ]


__all__ = [
    "bar_timestamps",
    "generate_bar",
    "generate_candles",
    "minute_ordinal",
]
