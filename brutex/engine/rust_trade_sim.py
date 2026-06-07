"""Resident Rust trade-sim seam — the hot trade-walk reused across masks.

DEC-SWEEP-RUST-TRADESIM-001 (MIGRATION.md #563). The Python side of the Rust
Stage-2 trade-simulation kernel (``fno_math.ResidentSweepSim``). It is the
trade-sim analogue of :class:`brutex.engine.combo_mask.ResidentBarBuffer`: the
expensive per-(instrument, tf) inputs — the OHLC→paisa conversion
(:func:`brutex.engine.stage1a._price_to_paisa`), the 15:20-IST forced-exit flags
(:func:`brutex.calendars.nse.is_forced_exit_bar`), and the Stage-1A ``bar_bits``
truth-vectors — are computed ONCE in Python (the single source of truth,
CLAUDE.md §11 COMMON) and handed to a resident Rust buffer. Thereafter a whole
batch of strategy masks is simulated into round-trip
:class:`~brutex.metrics.types.ClosedTrade` objects WITHOUT re-marshalling the bar
window per combo.

## Why this is bit-identical to the Python walker (Golden Rule #4)

Because the price→paisa conversion and the forced-exit detection — the two seams
a naive port would most easily get wrong — are done in Python by the SAME
functions :func:`brutex.engine.trade_sim.simulate_trades` uses, the Rust kernel
receives identical int64 inputs and only reproduces the per-bar SL/TP/forced
state-machine branch order. The result is byte-identical trades; a golden
cross-check (``tests/golden``) asserts it field-by-field, and the
:meth:`ResidentTradeSim.simulate_masks` fallback below IS
:func:`~brutex.engine.trade_sim.simulate_trades` (so an environment without the
compiled wheel produces the exact same trades, never a silent divergence).

## Honest envelope (Golden Rule #10) — NOT O(1) total

* Construction is ``O(N_bars)`` — the one-time paisa/forced/bar_bits derivation +
  the resident copy across the FFI boundary, paid ONCE.
* :meth:`ResidentTradeSim.simulate_masks` is ``O(N_masks · N_bars)`` compute (each
  mask walks the whole window once) + ``O(Σ trades)`` output. The per-bar work is
  O(1); the whole sweep is inherently O(N_masks · N_bars). Claiming O(1) total is
  a physical impossibility and is FORBIDDEN.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from brutex.calendars.nse import IST, is_forced_exit_bar
from brutex.engine.combo_mask import mask_to_entry_signals

# Intentional friend access (DEC-P0-017 per-site convention): ``_price_to_paisa``
# is the canonical Decimal-rupees → int64-paisa seam — the SAME one
# :mod:`brutex.engine.trade_sim` uses (CLAUDE.md §11 COMMON), so the resident Rust
# kernel's int64 inputs match the Python walker's bit-for-bit.
from brutex.engine.stage1a import _price_to_paisa  # pyright: ignore[reportPrivateUsage]
from brutex.engine.trade_sim import (
    DEFAULT_FILL_POLICY,
    FillPolicy,
    LevelFill,
    SameBarTie,
    simulate_trades,
)
from brutex.strategies.types import Side

if TYPE_CHECKING:  # pragma: no cover - import only for static typing
    from brutex.data.models import CandleBar

    # ``ClosedTrade`` is imported lazily at its construction site (see the note in
    # :mod:`brutex.engine.trade_sim`) to keep numpy out of the engine package's
    # collection-time import chain (the per-module strict-coverage gate's
    # numpy-load-once guard).
    from brutex.metrics.types import ClosedTrade

__all__ = ["ResidentTradeSim"]


class ResidentTradeSim:
    """Hold one ``(instrument, tf)`` bar window resident for repeated mask sims.

    Build it once from the bars + their Stage-1A ``bar_bits``; then call
    :meth:`simulate_masks` for each mask batch. The OHLC→paisa conversion, the
    forced-exit flags, and the resident bar copy are paid once at construction,
    not once per mask.

    When the compiled ``fno_math.ResidentSweepSim`` is unavailable (a plain
    ``pip install`` without the maturin-built wheel) the buffer transparently
    falls back to the pure-Python :func:`brutex.engine.trade_sim.simulate_trades`
    per mask — the SAME oracle the Rust kernel is verified against — so the trades
    are identical, never a silent divergence.

    Parameters
    ----------
    bars
        Chronologically ordered bars for one ``(instrument, interval)``
        (non-empty; the trade-sim feed).
    bar_bits
        The Stage-1A per-bar u64 truth-vectors for the same window (from
        :func:`brutex.engine.stage1a.build_bar_bits`). Must match ``bars`` length.
    tf_minutes
        The bar timeframe in whole minutes (``>= 1``), for the timeframe-aware
        forced-exit detection.

    Raises
    ------
    ValueError
        If ``bars`` is empty, ``bar_bits`` length ≠ ``bars`` length, or
        ``tf_minutes < 1``.
    """

    __slots__ = ("_bar_bits", "_bars", "_rust", "_tf_minutes")

    def __init__(
        self,
        bars: Sequence[CandleBar],
        bar_bits: Sequence[int],
        *,
        tf_minutes: int,
    ) -> None:
        if not bars:
            raise ValueError("ResidentTradeSim: bars must be non-empty")
        if len(bar_bits) != len(bars):
            raise ValueError(
                f"ResidentTradeSim: bar_bits length {len(bar_bits)} != bars length {len(bars)}"
            )
        if tf_minutes < 1:
            raise ValueError(f"ResidentTradeSim: tf_minutes must be >= 1, got {tf_minutes}")
        self._bars: tuple[CandleBar, ...] = tuple(bars)
        self._bar_bits: list[int] = list(bar_bits)
        self._tf_minutes = tf_minutes

        # Derive the resident kernel inputs ONCE, via the canonical Python seams.
        forced = [
            is_forced_exit_bar(bar.timestamp.astimezone(IST).time(), tf_minutes)
            for bar in self._bars
        ]
        open_p = [_price_to_paisa(bar.open) for bar in self._bars]
        high_p = [_price_to_paisa(bar.high) for bar in self._bars]
        low_p = [_price_to_paisa(bar.low) for bar in self._bars]
        close_p = [_price_to_paisa(bar.close) for bar in self._bars]
        try:
            import fno_math

            self._rust: fno_math.ResidentSweepSim | None = fno_math.ResidentSweepSim(
                open_p, high_p, low_p, close_p, forced, self._bar_bits
            )
        except (ImportError, AttributeError):
            self._rust = None

    @property
    def n_bars(self) -> int:
        """Number of bars in the resident window."""
        return len(self._bars)

    def simulate_masks(
        self,
        masks: Sequence[int],
        *,
        direction: Side,
        stop_loss_paisa: int | None = None,
        take_profit_paisa: int | None = None,
        policy: FillPolicy = DEFAULT_FILL_POLICY,
    ) -> list[list[ClosedTrade]]:
        """Simulate a BATCH of masks over the resident window → per-mask trades.

        ``out[j]`` is the round-trip trades for ``masks[j]`` (signal-only spot;
        next-bar-open entry, SL/TP, 15:20 forced exit). Bit-identical to
        ``[simulate_trades(bars, mask_to_entry_signals(bar_bits, m), ...) for m in
        masks]`` — the Rust path and the fallback both honour the same fill policy
        and direction. Empty ``masks`` → ``[]``.

        Parameters
        ----------
        masks
            The u64 strategy masks to simulate.
        direction
            :data:`~brutex.strategies.types.Side.BUY` (long) or ``Side.SELL``
            (short); ``HOLD`` is rejected.
        stop_loss_paisa, take_profit_paisa
            Stop / target distances from entry in paisa (``> 0``), or ``None`` to
            disable that leg.
        policy
            The :class:`~brutex.engine.trade_sim.FillPolicy` (defaults to the
            conservative :data:`~brutex.engine.trade_sim.DEFAULT_FILL_POLICY`).

        Returns
        -------
        list[list[ClosedTrade]]
            One trade list per mask, positionally aligned with ``masks``.

        Raises
        ------
        ValueError
            If ``direction`` is ``HOLD`` (mirrors
            :class:`~brutex.engine.trade_sim.TradeSimWalker`).
        """
        if direction is Side.HOLD:
            raise ValueError("ResidentTradeSim.simulate_masks: direction must be BUY or SELL")
        if not masks:
            return []

        rust = self._rust
        if rust is None:
            # Pure-Python fallback: the exact oracle, one mask at a time.
            return [
                simulate_trades(
                    self._bars,
                    mask_to_entry_signals(self._bar_bits, mask),
                    direction=direction,
                    tf_minutes=self._tf_minutes,
                    stop_loss_paisa=stop_loss_paisa,
                    take_profit_paisa=take_profit_paisa,
                    policy=policy,
                )
                for mask in masks
            ]

        # Rust path — map the FillPolicy to the kernel's three boolean flags.
        sl_next_open = policy.stop_loss is LevelFill.NEXT_OPEN
        tp_next_open = policy.take_profit is LevelFill.NEXT_OPEN
        tp_first = policy.same_bar is SameBarTie.TP_FIRST
        raw = rust.simulate_masks(
            list(masks),
            direction is Side.BUY,
            stop_loss_paisa,
            take_profit_paisa,
            sl_next_open,
            tp_next_open,
            tp_first,
        )
        return [
            [self._to_trade(record, direction) for record in mask_records] for mask_records in raw
        ]

    def _to_trade(
        self, record: tuple[int, int, int, int, int, int], direction: Side
    ) -> ClosedTrade:
        """Map one Rust trade tuple → a :class:`ClosedTrade` (idx→bar timestamps)."""
        # Lazy import (numpy-load-once guard; see the module/TYPE_CHECKING note).
        from brutex.metrics.types import ClosedTrade

        pnl, mfe, mae, holding, entry_idx, exit_idx = record
        return ClosedTrade(
            pnl_paisa=pnl,
            mfe_paisa=mfe,
            mae_paisa=mae,
            holding_bars=holding,
            entry_ts=self._bars[entry_idx].timestamp,
            exit_ts=self._bars[exit_idx].timestamp,
            direction=direction,
        )
