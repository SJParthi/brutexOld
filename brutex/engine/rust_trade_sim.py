"""Resident Rust trade-sim seam — the hot trade-walk reused across masks.

DEC-SWEEP-RUST-TRADESIM-001 (MIGRATION.md #563). The Python side of the Rust
Stage-2 trade-simulation kernel (``fno_math.ResidentSweepSim``). It is the
trade-sim analogue of :class:`brutex.engine.combo_mask.ResidentBarBuffer`: the
expensive per-(instrument, tf) inputs — the OHLC→paisa conversion
(:func:`brutex.engine.stage1a._price_to_paisa`), the SESSION-AWARE forced-exit
flags (:func:`brutex.calendars.nse.is_forced_exit_bar` against each bar session's
close − 10min; DEC-SESSION-FORCED-EXIT-001), the per-bar IST session identity
(each bar's IST calendar-date ordinal — the gate that DROPS a pending entry whose
fill bar falls on a later session, R2-OC-2), and the Stage-1A ``bar_bits``
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

## Kernel-envelope guards (DEC-SWEEP-RUST-TRADESIM-WIRE-001)

The compiled kernel is u64-native for ``bar_bits``/``masks`` and validates every
paisa input against the overflow-safe ceiling ``MAX_PRICE_PAISA = i64::MAX / 8``
(``rust/fno-math/src/trade_sim.rs``, #1090). An input outside that envelope must
NEVER surface as a PyO3 ``OverflowError`` / kernel ``ValueError`` where the
pure-Python oracle would simply simulate (a wheel-present vs wheel-absent
behaviour asymmetry). So — mirroring the count path's ``>= 64``-bit guard,
:func:`brutex.engine.combo_mask.count_supermasks` — every out-of-envelope input
routes to the bit-exact Python oracle instead, LOUDLY (one structlog WARNING,
never silent):

* at CONSTRUCTION (whole-object fallback, ``rust_active`` False): a ``bar_bits``
  value outside ``[0, U64_MASK]`` (:data:`~brutex.engine.combo_mask.U64_MASK`;
  above = the u128 headroom — ``MAX_VOCAB_BITS = 128``; below zero = API misuse
  only, the production emitter :func:`~brutex.engine.stage1a.build_bar_bits` is
  non-negative by construction, but a negative int would raise the SAME PyO3
  ``OverflowError`` at the ``Vec<u64>`` edge so both directions route to the
  oracle), or any price column value outside
  ``[-MAX_PRICE_PAISA, MAX_PRICE_PAISA]`` paisa;
* per :meth:`ResidentTradeSim.simulate_masks` CALL (that call only): a mask
  outside ``[0, U64_MASK]`` (same both-directions rationale), or an SL/TP
  distance above ``MAX_PRICE_PAISA``. The per-call WARNING is emitted ONCE per
  ``(instance, reason)`` — the production sweep calls ``simulate_masks`` once
  per bounded chunk (up to thousands of chunks per tuple), so a u128-era sweep
  warning on EVERY chunk would flood the logs; the first occurrence is loud,
  repeats of the same already-warned reason fall back silently (the path is
  still observable per tuple via ``sweep_run.trade_sim_path``).

A non-positive SL/TP distance is rejected with ``ValueError`` IDENTICALLY on both
arms (the kernel would silently accept ``0`` where the Python walker raises — the
wrapper normalises the domain so wheel state can never change the error
behaviour).

## Observability delta vs the Python walker (STATED per Golden Rule #10)

The Python walker (:func:`brutex.engine.trade_sim.simulate_trades`) emits
per-trade ``trade_sim.entry`` / ``trade_sim.exit`` DEBUG structlog events; the
compiled kernel emits NONE — deliberately (that per-trade logging is part of the
interpreter tax the wiring removes; filtering it alone moved a measured run
92.8 → 122.3 combos/s). So under DEBUG-level structlog, per-trade telemetry
appears ONLY on tuples served by the Python fallback arm. The per-TUPLE signals
(``sweep_run.trade_sim_path``, the fallback WARNINGs) are engine-independent.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import TYPE_CHECKING, Final

import structlog

from brutex.calendars.nse import (
    IST,
    forced_exit_seconds_for_session,
    get_session_checked,
    is_forced_exit_bar,
)
from brutex.engine.combo_mask import U64_MASK, mask_to_entry_signals
from brutex.engine.exit_index import bars_strictly_increasing

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
    TradeSimWalker,
)
from brutex.strategies.types import Side

if TYPE_CHECKING:  # pragma: no cover - import only for static typing
    from brutex.data.models import CandleBar
    from brutex.engine.reference_series import TradeReferenceStamper

    # ``ClosedTrade`` is imported lazily at its construction site (see the note in
    # :mod:`brutex.engine.trade_sim`) to keep numpy out of the engine package's
    # collection-time import chain (the per-module strict-coverage gate's
    # numpy-load-once guard).
    from brutex.metrics.types import ClosedTrade

__all__ = ["ResidentTradeSim"]

_LOG = structlog.get_logger("brutex.engine.rust_trade_sim")

#: Python mirror of the kernel's overflow-safe paisa ceiling
#: (``rust/fno-math/src/trade_sim.rs`` ``MAX_PRICE_PAISA = i64::MAX / 8``, #1090).
#: Any boundary input above it would raise a clean kernel ``ValueError`` — the
#: wrapper detects it FIRST and routes to the bit-exact Python oracle instead
#: (see the module docstring's kernel-envelope-guards section).
_MAX_KERNEL_PRICE_PAISA: Final[int] = (2**63 - 1) // 8


def _construction_fallback_reason(
    bar_bits: Sequence[int],
    open_p: Sequence[int],
    high_p: Sequence[int],
    low_p: Sequence[int],
    close_p: Sequence[int],
) -> str | None:
    """Return why the compiled kernel CANNOT hold this window resident, or ``None``.

    The construction-time half of the kernel-envelope guard (module docstring):
    a ``bar_bits`` value outside ``[0, U64_MASK]`` would fail the kernel's
    ``Vec<u64>`` edge (PyO3 raises ``OverflowError`` for BOTH a >64-bit value
    AND a negative one — mirroring
    :func:`~brutex.engine.combo_mask.count_supermasks`'s guard; the negative
    direction is API misuse only, kept so wheel state can never change
    behaviour), and a paisa price outside ``[-MAX_PRICE_PAISA,
    MAX_PRICE_PAISA]`` would raise the kernel's #1090 boundary ``ValueError``.
    Either routes the WHOLE object to the pure-Python oracle (bit-identical
    either way). One ``O(N_bars)`` scan, paid once at the already-``O(N_bars)``
    construction.
    """
    if any(not 0 <= b <= U64_MASK for b in bar_bits):
        return "bar_bits_u128"
    for column in (open_p, high_p, low_p, close_p):
        if any(not -_MAX_KERNEL_PRICE_PAISA <= p <= _MAX_KERNEL_PRICE_PAISA for p in column):
            return "price_out_of_envelope"
    return None


def _call_fallback_reason(
    masks: Sequence[int],
    stop_loss_paisa: int | None,
    take_profit_paisa: int | None,
) -> str | None:
    """Return why THIS batch cannot cross the kernel FFI boundary, or ``None``.

    The per-call half of the kernel-envelope guard: a mask outside
    ``[0, U64_MASK]`` would fail ``Vec<u64>`` (PyO3 ``OverflowError`` both for
    a u128 mask and a negative one — the latter API misuse only,
    :func:`~brutex.engine.combo_mask.iter_masks_k_of_bits` emits non-negative
    ints; guarded so wheel state can never change behaviour), an SL/TP distance
    above the #1090 ceiling would raise the kernel's batch-entry ``ValueError``
    where the Python walker simulates. ``O(N_masks)`` per call — negligible next
    to the ``O(N_masks · N_bars)`` sweep.
    """
    if any(not 0 <= m <= U64_MASK for m in masks):
        return "mask_u128"
    if stop_loss_paisa is not None and stop_loss_paisa > _MAX_KERNEL_PRICE_PAISA:
        return "stop_loss_out_of_envelope"
    if take_profit_paisa is not None and take_profit_paisa > _MAX_KERNEL_PRICE_PAISA:
        return "take_profit_out_of_envelope"
    return None


def _monotonic(exit_indices: Sequence[int]) -> bool:
    """Certify one mask's exit-index stream: non-negative + strictly increasing.

    DEC-SWEEP-EXIT-INDEX-FASTPATH-001, the kernel-record certification: the
    single-position walker (which the kernel mirrors bit-for-bit) closes at
    most one trade per bar and emits trades in close order, so a well-formed
    per-mask exit-index stream is strictly increasing and starts at ``>= 0``.
    A stream failing this certificate signals a kernel ordering/marshalling
    regression — the caller withholds the indices (the metrics tail then
    serves the mask through the bit-identical timestamp-dict path) rather
    than trusting them. ``O(trades)`` per mask, zero allocation.
    """
    prev = -1
    for idx in exit_indices:
        if idx <= prev:
            return False
        prev = idx
    return True


def _width_offender_fields(values: Sequence[int]) -> dict[str, str | int | None]:
    """Name the first value outside ``[0, U64_MASK]`` for a width-fallback WARNING.

    DEC-SWEEP-RUST-WIRE-001 part 1 (the engine-scale spec's PR-1 gate): the
    u64-width fallback WARNING must NAME the offending word — ``offending_value``
    (hex) + ``offending_bit`` — so the operator sees exactly which value could
    not cross the ``Vec<u64>`` FFI edge and at which bit (the first width that
    cannot fit u64 is bit 64; today's 60-bit vocabulary never triggers this).
    ``offending_bit`` is the word's highest set bit for the oversize direction;
    ``None`` for the negative API-misuse direction, where a "bit" has no
    meaning. One ``O(N)`` scan paid ONLY on the already-warned fallback path —
    the kernel fast path never runs this.
    """
    offender = next(v for v in values if not 0 <= v <= U64_MASK)
    return {
        "offending_value": hex(offender),
        "offending_bit": offender.bit_length() - 1 if offender > U64_MASK else None,
    }


class ResidentTradeSim:
    """Hold one ``(instrument, tf)`` bar window resident for repeated mask sims.

    Build it once from the bars + their Stage-1A ``bar_bits``; then call
    :meth:`simulate_masks` for each mask batch. The OHLC→paisa conversion, the
    forced-exit flags, and the resident bar copy are paid once at construction,
    not once per mask.

    When the compiled ``fno_math.ResidentSweepSim`` is unavailable the buffer
    transparently falls back to the pure-Python
    :func:`brutex.engine.trade_sim.simulate_trades` per mask — the SAME oracle
    the Rust kernel is verified against — so the trades are identical, and the
    fallback is LOGGED (``rust_trade_sim.fallback`` WARNING), never a silent
    divergence. Honest envelope: the production sweep hard-imports ``fno_math``
    at module scope (``sweep_driver.py`` / ``combo_mask.py``), so a truly
    wheel-ABSENT environment fails at import long before this seam — the real
    production fallback trigger is a STALE wheel that imports but lacks
    ``ResidentSweepSim`` (the ``AttributeError`` arm), plus the kernel-envelope
    guards documented on the module.

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
    reference_stamper
        Optional India-VIX
        :class:`~brutex.engine.reference_series.TradeReferenceStamper`
        (DEC-VIX-STAMP-WIRE-001). When supplied, EVERY trade this window emits
        — on the Rust arm AND the Python-oracle fallback arm alike — carries
        ``vix_at_entry`` / ``vix_at_exit`` (REFERENCE ONLY, never a signal
        input; a missing VIX bar stamps ``None``, never fabricated). The Rust
        kernel itself is UNTOUCHED: the stamp happens Python-side in the
        :meth:`_to_trade_and_exit` converter (an O(1) post-stamp per trade), so the
        kernel's trade tuples stay engine-blind and both arms stamp through
        the SAME :meth:`~brutex.engine.reference_series.TradeReferenceStamper.stamp`.
        ``None`` (the default) leaves both VIX fields ``None`` — byte-identical
        to the pre-wiring output.

    Raises
    ------
    ValueError
        If ``bars`` is empty, ``bar_bits`` length ≠ ``bars`` length, or
        ``tf_minutes < 1``.
    brutex.calendars.nse.HolidayDataUnavailableError
        If any bar is dated on a weekday OUTSIDE the published holiday span —
        the session (and thus the forced-exit boundary) cannot be verified, so
        construction refuses LOUDLY instead of fabricating a 15:20 boundary
        (mirrors :meth:`brutex.engine.trade_sim.TradeSimWalker` exactly).
    """

    __slots__ = (
        "_bar_bits",
        "_bars",
        "_call_fallback_warned",
        "_reference_stamper",
        "_rust",
        "_tf_minutes",
        "_ts_certified",
    )

    def __init__(
        self,
        bars: Sequence[CandleBar],
        bar_bits: Sequence[int],
        *,
        tf_minutes: int,
        reference_stamper: TradeReferenceStamper | None = None,
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
        self._reference_stamper = reference_stamper
        # Exit-index certification (DEC-SWEEP-EXIT-INDEX-FASTPATH-001): the
        # window's timestamps strictly increase ⇒ index↔timestamp is a
        # bijection ⇒ a trade's TRUE exit index provably equals the metrics
        # tail's last-wins dict lookup, so the indices may replace it. Paid
        # ONCE here (the construction is already O(N_bars)), never per mask.
        self._ts_certified: bool = bars_strictly_increasing(self._bars)
        # Per-(instance, reason) warn-once memo for the per-call fallback (module
        # docstring): the sweep chunks a tuple into up to thousands of
        # `simulate_masks` calls, so a per-CALL warning on a sticky reason (e.g.
        # a u128-era vocabulary) would flood production logs.
        self._call_fallback_warned: set[str] = set()

        # Derive the resident kernel inputs ONCE, via the canonical Python seams.
        # The forced-exit flags are SESSION-AWARE — byte-identical to
        # :func:`~brutex.engine.trade_sim.simulate_trades` — so the boundary is
        # the bar session's own close − 10min (15:20 IST on a regular day, the
        # early close − 10min on a half-day / Muhurat session), resolved per IST
        # date via the COMMON strict ``get_session_checked`` →
        # :func:`~brutex.calendars.nse.forced_exit_seconds_for_session`; an
        # in-coverage date with no session falls back to 15:20
        # (DEC-SESSION-FORCED-EXIT-001), while a bar dated on a weekday OUTSIDE
        # the published holiday span raises ``HolidayDataUnavailableError`` at
        # construction — LOUDLY, in lock-step with the streaming walker's
        # ``_forced_exit_seconds`` (the parity contract holds: both paths refuse
        # the same uncoverable date instead of silently fabricating a 15:20
        # boundary). The boundary is memoised by the LAST IST date only (a
        # 1-slot cache, O(1) per bar) — a chronological feed re-resolves once
        # per new day, exactly as the streaming walker does, so the precomputed
        # Rust-kernel flags and the Python-fallback exits never diverge.
        #
        # ``session_days`` is the per-bar SESSION identity — the bar's own IST
        # calendar-date ordinal, the SAME date key the forced-exit resolution
        # uses — threaded to the kernel so a pending entry armed on a session's
        # last stored bar is DROPPED at the next session's open instead of
        # filling ~18h stale across the overnight gap (R2-OC-2; mirrors the
        # walker's ``_pending_entry_day`` gate bit-for-bit). Bar ``i``'s session
        # derives from bar ``i``'s own timestamp — knowable at bar ``i`` (P-06:
        # no look-ahead).
        forced: list[bool] = []
        session_days: list[int] = []
        cache_date: date | None = None
        cache_seconds: int | None = None
        for bar in self._bars:
            ts_ist = bar.timestamp.astimezone(IST)
            bar_day = ts_ist.date()
            if bar_day != cache_date:
                session = get_session_checked(bar_day)
                cache_date = bar_day
                cache_seconds = (
                    None if session is None else forced_exit_seconds_for_session(session)
                )
            forced.append(is_forced_exit_bar(ts_ist.time(), tf_minutes, cache_seconds))
            session_days.append(bar_day.toordinal())
        open_p = [_price_to_paisa(bar.open) for bar in self._bars]
        high_p = [_price_to_paisa(bar.high) for bar in self._bars]
        low_p = [_price_to_paisa(bar.low) for bar in self._bars]
        close_p = [_price_to_paisa(bar.close) for bar in self._bars]
        # Kernel-envelope guard (module docstring): an out-of-envelope window —
        # a u128 bar_bits value or a price beyond the kernel's #1090 ceiling —
        # never crosses the FFI boundary; the whole object serves the bit-exact
        # Python oracle instead, and says so LOUDLY (never a silent divergence,
        # never a raise the pure-Python engine would not have raised).
        # ``fno_math`` is imported ONCE, lazily, at the construction just below
        # (the wheel may be absent — the SINGLE import site), so the resident
        # kernel handle is typed ``object`` with a targeted ignore at its one
        # call site — the same lazy-wheel convention
        # :class:`~brutex.engine.combo_mask.ResidentBarBuffer` uses.
        self._rust: object | None = None
        reason = _construction_fallback_reason(self._bar_bits, open_p, high_p, low_p, close_p)
        if reason is None:
            try:
                import fno_math

                self._rust = fno_math.ResidentSweepSim(
                    open_p, high_p, low_p, close_p, forced, session_days, self._bar_bits
                )
            except (ImportError, AttributeError):
                reason = "wheel_missing"
        if self._rust is None:
            # The width reason additionally NAMES the offending word (value +
            # bit) — spec PR-1, DEC-SWEEP-RUST-WIRE-001 part 1; other reasons
            # carry no width offender.
            _LOG.warning(
                "rust_trade_sim.fallback",
                reason=reason,
                n_bars=len(self._bars),
                tf_minutes=tf_minutes,
                **(_width_offender_fields(self._bar_bits) if reason == "bar_bits_u128" else {}),
            )

    @property
    def n_bars(self) -> int:
        """Number of bars in the resident window."""
        return len(self._bars)

    @property
    def reference_stamper(self) -> TradeReferenceStamper | None:
        """The India-VIX stamper bound to this window (``None`` = unstamped).

        PUBLIC so the sweep's chunk-failure re-attribution can hand the
        Python-oracle replay the SAME stamper this window simulates with
        (DEC-VIX-STAMP-WIRE-001, the STRUCTURAL oracle≡batch guarantee: the
        oracle and the batch can never diverge on the stamping input because
        the oracle reads it FROM the sim, never from a second wiring).
        """
        return self._reference_stamper

    @property
    def rust_active(self) -> bool:
        """``True`` iff the compiled kernel serves this window (``False`` = Python oracle).

        The observable path-selection signal (Golden Rule #10 — a silently
        Python-degraded sweep would be a fake-performance illusion): the
        production sweep logs it per tuple (``sweep_run.trade_sim_path``) and CI
        asserts the wired path actually engages the kernel.
        """
        return self._rust is not None

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

        Thin delegate over :meth:`simulate_masks_with_exit_indices` (ONE
        batch spine, DEC-SWEEP-EXIT-INDEX-FASTPATH-001) that drops the
        exit-index halves — byte-identical trades, same error contract,
        same fallback/warn behaviour. See that method for the full contract.
        """
        return [
            trades
            for trades, _ in self.simulate_masks_with_exit_indices(
                masks,
                direction=direction,
                stop_loss_paisa=stop_loss_paisa,
                take_profit_paisa=take_profit_paisa,
                policy=policy,
            )
        ]

    def simulate_masks_with_exit_indices(
        self,
        masks: Sequence[int],
        *,
        direction: Side,
        stop_loss_paisa: int | None = None,
        take_profit_paisa: int | None = None,
        policy: FillPolicy = DEFAULT_FILL_POLICY,
    ) -> list[tuple[list[ClosedTrade], list[int] | None]]:
        """Simulate a BATCH of masks → per-mask ``(trades, exit_indices)`` pairs.

        ``out[j]`` is ``(trades, exit_indices)`` for ``masks[j]`` (signal-only
        spot; next-bar-open entry, SL/TP, 15:20 forced exit). The trades are
        bit-identical to ``[simulate_trades(bars, mask_to_entry_signals(
        bar_bits, m), ...) for m in masks]`` — the Rust path and the fallback
        both honour the same fill policy and direction. Empty ``masks`` → ``[]``.

        ``exit_indices`` (DEC-SWEEP-EXIT-INDEX-FASTPATH-001) is each trade's
        exit BAR INDEX in the resident window, emitted only under a DOUBLE
        certificate — the window's timestamps strictly increase (checked once
        at construction) AND the mask's stream passes :func:`_monotonic` —
        else ``None``, and the metrics tail serves that mask through the
        bit-identical timestamp-dict path. A monotonicity failure on a
        CERTIFIED window is a kernel ordering regression and is WARNed once
        per instance (``rust_trade_sim.exit_indices_uncertified``); an
        uncertified window withholds indices silently (the dict path is the
        historical behaviour for such a malformed window, byte-unchanged).
        The Python-fallback arm's walker indices ride the same window
        certificate (the walker's stream is monotonic by construction).

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
        list[tuple[list[ClosedTrade], list[int] | None]]
            One ``(trades, exit_indices)`` pair per mask, positionally
            aligned with ``masks``.

        Raises
        ------
        ValueError
            If ``direction`` is ``HOLD``, or a non-``None`` stop/target distance
            is not strictly positive (the domain is normalised HERE so both arms
            raise identically — the kernel would silently accept ``0`` where the
            Python :class:`~brutex.engine.trade_sim.TradeSimWalker` raises).
        """
        if direction is Side.HOLD:
            raise ValueError("ResidentTradeSim.simulate_masks: direction must be BUY or SELL")
        for name, distance in (
            ("stop_loss_paisa", stop_loss_paisa),
            ("take_profit_paisa", take_profit_paisa),
        ):
            if distance is not None and distance <= 0:
                raise ValueError(
                    f"ResidentTradeSim.simulate_masks: {name} must be > 0 or None, got {distance}"
                )
        if not masks:
            return []

        rust = self._rust
        if rust is not None:
            # Per-call kernel-envelope guard (module docstring): a u128 mask or
            # an SL/TP distance beyond the kernel's #1090 ceiling routes THIS
            # call to the bit-exact Python oracle — loudly on the FIRST
            # occurrence per (instance, reason); repeats fall back without
            # re-warning (the chunked sweep would otherwise emit one WARNING
            # per chunk — a log flood on a u128-era vocabulary).
            call_reason = _call_fallback_reason(masks, stop_loss_paisa, take_profit_paisa)
            if call_reason is not None:
                if call_reason not in self._call_fallback_warned:
                    self._call_fallback_warned.add(call_reason)
                    # The width reason additionally NAMES the offending mask
                    # (value + bit) — spec PR-1, DEC-SWEEP-RUST-WIRE-001 part 1.
                    _LOG.warning(
                        "rust_trade_sim.call_fallback",
                        reason=call_reason,
                        n_masks=len(masks),
                        **(_width_offender_fields(masks) if call_reason == "mask_u128" else {}),
                    )
                rust = None
        if rust is None:
            # Pure-Python fallback: the exact oracle (the SAME walker
            # simulate_trades wraps), one mask at a time. The walker's exit
            # indices are TRUE indices and monotonic by construction; they
            # are emitted under the same window certificate as the kernel's.
            # The window's OWN stamper rides into every walker, so a
            # fallback-served window stamps identically to the kernel-served
            # one (never a wheel-state divergence — DEC-VIX-STAMP-WIRE-001).
            out: list[tuple[list[ClosedTrade], list[int] | None]] = []
            for mask in masks:
                walker = TradeSimWalker(
                    direction=direction,
                    tf_minutes=self._tf_minutes,
                    stop_loss_paisa=stop_loss_paisa,
                    take_profit_paisa=take_profit_paisa,
                    policy=policy,
                    reference_stamper=self._reference_stamper,
                )
                trades, exit_indices = walker.run_with_exit_indices(
                    self._bars, mask_to_entry_signals(self._bar_bits, mask)
                )
                out.append((trades, exit_indices if self._ts_certified else None))
            return out

        # Rust path — map the FillPolicy to the kernel's three boolean flags.
        sl_next_open = policy.stop_loss is LevelFill.NEXT_OPEN
        tp_next_open = policy.take_profit is LevelFill.NEXT_OPEN
        tp_first = policy.same_bar is SameBarTie.TP_FIRST
        raw = rust.simulate_masks(  # type: ignore[attr-defined]
            list(masks),
            direction is Side.BUY,
            stop_loss_paisa,
            take_profit_paisa,
            sl_next_open,
            tp_next_open,
            tp_first,
        )
        # HOISTED converter input (DEC-VIX-STAMP-WIRE-001): the stamper is read
        # ONCE per batch call — not once per trade — and threaded into the
        # per-record converter, which post-stamps each Rust trade Python-side.
        stamper = self._reference_stamper
        pairs: list[tuple[list[ClosedTrade], list[int] | None]] = []
        for mask_records in raw:
            mask_trades: list[ClosedTrade] = []
            mask_exit_indices: list[int] = []
            for record in mask_records:
                trade, exit_idx = self._to_trade_and_exit(record, direction, stamper)
                mask_trades.append(trade)
                mask_exit_indices.append(exit_idx)
            if not self._ts_certified:
                # Malformed (duplicate/reordered timestamp) window: withhold
                # the indices — the dict path is the historical behaviour.
                pairs.append((mask_trades, None))
            elif not _monotonic(mask_exit_indices):
                # A non-monotonic stream on a CERTIFIED window is a kernel
                # ordering regression — refuse the indices LOUDLY (once per
                # instance) and let the dict path serve the mask.
                if "exit_indices_not_monotonic" not in self._call_fallback_warned:
                    self._call_fallback_warned.add("exit_indices_not_monotonic")
                    _LOG.warning(
                        "rust_trade_sim.exit_indices_uncertified",
                        reason="exit_indices_not_monotonic",
                        n_trades=len(mask_trades),
                    )
                pairs.append((mask_trades, None))
            else:
                pairs.append((mask_trades, mask_exit_indices))
        return pairs

    def _to_trade_and_exit(
        self,
        record: tuple[int, int, int, int, int, int],
        direction: Side,
        stamper: TradeReferenceStamper | None,
    ) -> tuple[ClosedTrade, int]:
        """Map one Rust trade tuple → ``(ClosedTrade, exit_idx)`` (idx→bar timestamps).

        The exit bar index — which the kernel record already carries and the
        pre-#1186 mapping discarded — is returned alongside the trade so the
        metrics tail can skip its per-mask ``exit_ts``→index dict build
        (DEC-SWEEP-EXIT-INDEX-FASTPATH-001).

        ALSO the POST-STAMP seam (DEC-VIX-STAMP-WIRE-001): when a *stamper* is
        bound the trade's ``vix_at_entry`` / ``vix_at_exit`` are resolved HERE,
        at the Python-side conversion, through the SAME
        :meth:`~brutex.engine.reference_series.TradeReferenceStamper.stamp` the
        pure-Python walker uses — so Rust-batch trades and oracle trades stamp
        bit-identically, the kernel stays engine-blind, and a missing VIX bar
        stamps ``None`` (never fabricated). O(1) per trade: two dict probes.
        """
        # Lazy import (numpy-load-once guard; see the module/TYPE_CHECKING note).
        from brutex.metrics.types import ClosedTrade

        pnl, mfe, mae, holding, entry_idx, exit_idx = record
        entry_ts = self._bars[entry_idx].timestamp
        exit_ts = self._bars[exit_idx].timestamp
        if stamper is not None:
            vix_at_entry, vix_at_exit = stamper.stamp(entry_ts, exit_ts)
        else:
            vix_at_entry, vix_at_exit = None, None
        trade = ClosedTrade(
            pnl_paisa=pnl,
            mfe_paisa=mfe,
            mae_paisa=mae,
            holding_bars=holding,
            entry_ts=entry_ts,
            exit_ts=exit_ts,
            direction=direction,
            vix_at_entry=vix_at_entry,
            vix_at_exit=vix_at_exit,
        )
        return trade, exit_idx
