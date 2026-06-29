"""Session-anchored intraday futures VWAP — the indicator Group 10 consumes.

Per ADR-0012 D12 (DEC-NIFTY-INDICATORS-001): the brute-force NIFTY-futures track
needs a session-anchored intraday VWAP for the ``close_above_vwap`` /
``close_below_vwap`` conditions (bits 52/53). A spot index carries no traded
volume (``drop_volume=is_index``), so VWAP is computed on the FUTURES series.

## ONE indicator, ONE source of truth (shared with TickVault)

This is the **bit-for-bit Python twin** of the Rust ``trading-core``
``indicators::vwap::VwapState`` (the native TickVault path). Both compute
``cum(typical_price · volume) / cum(volume)`` anchored per trading session:

* **reset at 09:15 IST** each trading day (a new IST date, or a backwards
  time-of-day step, starts a fresh session);
* **excludes the 09:00-09:15 pre-open auction volume** (the caller passes
  ``is_pre_open`` for those bars — via ``brutex.calendars.nse.is_pre_open`` — and
  they are skipped entirely, CLAUDE.md §7);
* **resets on contract roll** (a change of the engine-injected ``session_key`` —
  per ADR-0012 D11; no VWAP ever spans a roll boundary);
* is **NA / ``None`` on the 1day timeframe** — the caller never feeds a 1day bar
  to this state (the evaluator's ``vwap_paisa`` is ``None`` there), so a session
  VWAP is never degenerate-with-one-bar.

## Overflow / precision

``cum(price · volume)`` over a full session can exceed int64 (price paisa ×
cumulative volume). Python ints are unbounded, and the read uses Python floor
division ``//`` on non-negative operands — exactly matching the Rust i128
accumulator + truncating ``cum_pv / cum_vol`` narrowing, so the two are
bit-identical for any real (non-negative price + volume) futures bar.

## No look-ahead (Property P-06)

``vwap(t)`` depends only on bars ``[0..t]`` fed in arrival order — a streaming
state object, identical to ``DayExtremesState``. Each ``update()`` is O(1): one
reset check + (for a counted bar) one multiply-add + one divide. No growing
history.

## The typical price

``typical = (high + low + close) / 3`` (HLC3, ``round_money``-snapped to the
2-decimal tick grid) — the same proxy ``brutex.dashboard.movers_snapshot`` uses
for turnover. The caller computes it and passes ``typical_paisa``; this keeps the
state agnostic to the price representation (the evaluator + the Rust twin both
take the resolved ``vwap_paisa``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any


@dataclass
class VwapState:
    """Streaming session-anchored VWAP accumulator (int64 paisa in / out).

    Hold once, call ``.update()`` per new bar. Bit-identical to the Rust
    ``trading-core`` ``VwapState`` (the TickVault-native path), so the backtest
    VWAP equals the live VWAP by construction.

    Attributes:
        session_key: The current session's contract key (engine-injected — a
            change is a contract roll and resets the session).
        session_day: IST date of the current session (a change resets).
        cum_pv: Running Σ(typical_price · volume) for the live session.
        cum_vol: Running Σ(volume) for the live session.
        warm: True once the running sums describe the current session only.
        last_tod_seconds: The last bar's IST time-of-day, so a backwards step
            (a re-opened session sharing the same day ordinal in a test) resets.
    """

    session_key: int = 0
    session_day: date | None = None
    cum_pv: int = 0
    cum_vol: int = 0
    warm: bool = False
    last_tod_seconds: int = 0

    def _start_session(self, session_key: int, session_day: date) -> None:
        """Reset the running sums to start a fresh session."""
        self.session_key = session_key
        self.session_day = session_day
        self.cum_pv = 0
        self.cum_vol = 0
        self.warm = False

    def update(
        self,
        typical_paisa: int,
        volume: int,
        *,
        is_pre_open: bool,
        session_key: int,
        session_day: date,
        tod_seconds: int,
    ) -> int | None:
        """Feed one bar; return the session VWAP in int64 paisa, or ``None``.

        ``None`` means the session has no usable (post-09:15, non-zero) volume
        yet — the warm-up bar — so the Group-10 bits do not fire.

        Args:
            typical_paisa: The bar's typical price (HLC3, ``round_money``-snapped).
            volume: The bar's traded volume.
            is_pre_open: True for a 09:00-09:15 pre-open auction bar — its volume
                is EXCLUDED (ADR-0012 D12 / CLAUDE.md §7).
            session_key: Engine-injected contract key (a change is a roll → reset).
            session_day: The bar's IST trading date (a change → reset).
            tod_seconds: The bar's IST time-of-day seconds (a backwards step → reset).

        Returns:
            The session VWAP in int64 paisa, or ``None`` while warming up.

        O(1) per bar.
        """
        new_session = (
            session_key != self.session_key
            or session_day != self.session_day
            or (self.warm and tod_seconds < self.last_tod_seconds)
        )
        if new_session:
            self._start_session(session_key, session_day)
        self.last_tod_seconds = tod_seconds

        # The 09:00-09:15 pre-open auction is EXCLUDED — its volume never enters
        # the VWAP. A zero-volume bar contributes nothing (and never warms).
        if is_pre_open or volume <= 0:
            return self.current()

        self.cum_pv += typical_paisa * volume
        self.cum_vol += volume
        self.warm = True
        return self.current()

    def current(self) -> int | None:
        """The current session VWAP (paisa), or ``None`` if no volume counted."""
        if self.cum_vol <= 0:
            return None
        # Truncating floor division on non-negative operands — matches the Rust
        # i128 ``cum_pv / cum_vol`` narrowing bit-for-bit.
        return self.cum_pv // self.cum_vol

    def reset(self) -> None:
        """Forget all history. Used at start-of-session-replay or in tests."""
        self.session_key = 0
        self.session_day = None
        self.cum_pv = 0
        self.cum_vol = 0
        self.warm = False
        self.last_tod_seconds = 0

    def to_dict(self) -> dict[str, Any]:
        """Serialize for checkpoint persistence."""
        return {
            "session_key": self.session_key,
            "session_day": self.session_day.isoformat() if self.session_day else None,
            "cum_pv": self.cum_pv,
            "cum_vol": self.cum_vol,
            "warm": self.warm,
            "last_tod_seconds": self.last_tod_seconds,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> VwapState:
        """Restore from :meth:`to_dict` output (checkpoint resume)."""
        raw_day = data["session_day"]
        return cls(
            session_key=int(data["session_key"]),
            session_day=date.fromisoformat(raw_day) if raw_day else None,
            cum_pv=int(data["cum_pv"]),
            cum_vol=int(data["cum_vol"]),
            warm=bool(data["warm"]),
            last_tod_seconds=int(data["last_tod_seconds"]),
        )
