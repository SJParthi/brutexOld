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

## Kernel-envelope guards (DEC-SWEEP-RUST-TRADESIM-WIRE-001 + DEC-TRADE-SIM-U128-KERNEL-001)

TWO compiled kernels share one width-generic Rust core
(``rust/fno-math/src/trade_sim.rs`` ``MaskWord``): the u64-native
``ResidentSweepSim`` (today's 60-bit production vocabulary) and the u128
``ResidentSweepSimU128`` (the ``MAX_VOCAB_BITS = 128`` headroom — >64-bit
masks/bar_bits are now Rust-served instead of oracle-served). Both validate
every paisa input against the overflow-safe ceiling ``MAX_PRICE_PAISA =
i64::MAX / 8`` (#1090). An input outside the WIDEST kernel's envelope must
still NEVER surface as a PyO3 ``OverflowError`` / kernel ``ValueError`` where
the pure-Python oracle would simply simulate (a wheel-present vs wheel-absent
behaviour asymmetry), so every such input routes to the bit-exact Python
oracle instead, LOUDLY (one structlog WARNING, never silent):

* at CONSTRUCTION: ``bar_bits`` all inside ``[0, U64_MASK]``
  (:data:`~brutex.engine.combo_mask.U64_MASK`) build the u64 kernel —
  byte-identical to the pre-u128 seam; any value in ``(U64_MASK,
  _U128_MASK]`` builds the u128 kernel instead (``rust_active`` True either
  way, one ``except (ImportError, AttributeError)`` → ``wheel_missing``
  covers both); a value outside ``[0, _U128_MASK]`` (above = beyond even the
  u128 headroom; below zero = API misuse only, the production emitter
  :func:`~brutex.engine.stage1a.build_bar_bits` is non-negative by
  construction, but a negative int would raise the SAME PyO3
  ``OverflowError`` at either ``Vec`` edge) routes the WHOLE object to the
  oracle with reason ``bar_bits_beyond_u128``, as does any price column
  value outside ``[-MAX_PRICE_PAISA, MAX_PRICE_PAISA]`` paisa
  (``price_out_of_envelope``);
* per :meth:`ResidentTradeSim.simulate_masks` CALL (that call only): a mask
  outside ``[0, _U128_MASK]`` → ``mask_beyond_u128`` → oracle (same
  both-directions rationale), an SL/TP distance above ``MAX_PRICE_PAISA`` →
  oracle. A mask in ``(U64_MASK, _U128_MASK]`` over a u64-resident window is
  NOT a fallback: the u128 sibling kernel is built LAZILY (at most once per
  instance, one ``rust_trade_sim.wide_kernel_promoted`` INFO) and the WHOLE
  batch is routed wide — u64-fit batches keep the u64 kernel byte-identical.
  Only when the installed wheel PREDATES the u128 class does the wide batch
  fall back to the oracle, warn-once ``wheel_missing_u128`` (reachable only
  from a monkeypatched/hand-imported stale module: the shim's import-time
  handshake makes a genuinely stale ``.so`` fail at ``import fno_math``).
  Every per-call WARNING is emitted ONCE per ``(instance, reason)`` — the
  production sweep calls ``simulate_masks`` once per bounded chunk (up to
  thousands of chunks per tuple), so a sticky-reason warning on EVERY chunk
  would flood the logs; the first occurrence is loud, repeats of the same
  already-warned reason fall back silently (the path is still observable per
  tuple via ``sweep_run.trade_sim_path``).

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
from brutex.engine.combo_mask import MAX_VOCAB_BITS, U64_MASK, mask_to_entry_signals
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

#: The WIDEST value a mask / bar_bits word may carry and still cross the u128
#: kernel's ``Vec<u128>`` FFI edge — derived from the ONE 128 source,
#: :data:`~brutex.engine.combo_mask.MAX_VOCAB_BITS` (DEC-TRADE-SIM-U128-KERNEL-001).
#: Beyond it (or below zero) the value cannot reach EITHER compiled kernel and
#: the seam routes to the pure-Python oracle.
_U128_MASK: Final[int] = (1 << MAX_VOCAB_BITS) - 1


def _construction_fallback_reason(
    bar_bits: Sequence[int],
    open_p: Sequence[int],
    high_p: Sequence[int],
    low_p: Sequence[int],
    close_p: Sequence[int],
) -> str | None:
    """Return why NO compiled kernel can hold this window resident, or ``None``.

    The construction-time half of the kernel-envelope guard (module docstring):
    a ``bar_bits`` value outside ``[0, _U128_MASK]`` would fail even the u128
    kernel's ``Vec<u128>`` edge (PyO3 raises ``OverflowError`` for BOTH a
    >128-bit value AND a negative one — mirroring
    :func:`~brutex.engine.combo_mask.count_supermasks`'s guard stance; the
    negative direction is API misuse only, kept so wheel state can never
    change behaviour), and a paisa price outside ``[-MAX_PRICE_PAISA,
    MAX_PRICE_PAISA]`` would raise the #1090 boundary ``ValueError`` in either
    kernel. Either routes the WHOLE object to the pure-Python oracle
    (bit-identical either way). A value in ``(U64_MASK, _U128_MASK]`` is NOT a
    fallback — the constructor builds the u128 kernel for it. One
    ``O(N_bars)`` scan, paid once at the already-``O(N_bars)`` construction.
    """
    if any(not 0 <= b <= _U128_MASK for b in bar_bits):
        return "bar_bits_beyond_u128"
    for column in (open_p, high_p, low_p, close_p):
        if any(not -_MAX_KERNEL_PRICE_PAISA <= p <= _MAX_KERNEL_PRICE_PAISA for p in column):
            return "price_out_of_envelope"
    return None


def _call_fallback_reason(
    masks: Sequence[int],
    stop_loss_paisa: int | None,
    take_profit_paisa: int | None,
) -> str | None:
    """Return why THIS batch cannot cross EITHER kernel's FFI boundary, or ``None``.

    The per-call half of the kernel-envelope guard: a mask outside
    ``[0, _U128_MASK]`` would fail even ``Vec<u128>`` (PyO3 ``OverflowError``
    both for a >128-bit mask and a negative one — the latter API misuse only,
    :func:`~brutex.engine.combo_mask.iter_masks_k_of_bits` emits non-negative
    ints; guarded so wheel state can never change behaviour), an SL/TP distance
    above the #1090 ceiling would raise the batch-entry ``ValueError`` in
    either kernel where the Python walker simulates. A mask in ``(U64_MASK,
    _U128_MASK]`` is NOT a fallback — the caller promotes the batch to the
    u128 kernel. ``O(N_masks)`` per call — negligible next to the
    ``O(N_masks · N_bars)`` sweep.
    """
    if any(not 0 <= m <= _U128_MASK for m in masks):
        return "mask_beyond_u128"
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
    """Name the first value outside ``[0, _U128_MASK]`` for a width-fallback WARNING.

    DEC-SWEEP-RUST-WIRE-001 part 1 (the engine-scale spec's PR-1 gate): the
    width fallback WARNING must NAME the offending word — ``offending_value``
    (hex) + ``offending_bit`` — so the operator sees exactly which value could
    not cross even the ``Vec<u128>`` FFI edge and at which bit (the first
    width that cannot fit u128 is bit 128; today's 60-bit vocabulary never
    triggers this). ``offending_bit`` is the word's highest set bit for the
    oversize direction; ``None`` for the negative API-misuse direction, where
    a "bit" has no meaning. One ``O(N)`` scan paid ONLY on the already-warned
    fallback path — the kernel fast path never runs this.
    """
    offender = next(v for v in values if not 0 <= v <= _U128_MASK)
    return {
        "offending_value": hex(offender),
        "offending_bit": offender.bit_length() - 1 if offender > _U128_MASK else None,
    }


class ResidentTradeSim:
    """Hold one ``(instrument, tf)`` bar window resident for repeated mask sims.

    Build it once from the bars + their Stage-1A ``bar_bits``; then call
    :meth:`simulate_masks` for each mask batch. The OHLC→paisa conversion, the
    forced-exit flags, and the resident bar copy are paid once at construction,
    not once per mask.

    The compiled kernel is width-dispatched (DEC-TRADE-SIM-U128-KERNEL-001):
    u64-fit ``bar_bits`` build ``fno_math.ResidentSweepSim`` (byte-identical
    to the pre-u128 seam), wider (≤ u128) ``bar_bits`` build
    ``fno_math.ResidentSweepSimU128``, and a >64-bit MASK over a u64-resident
    window lazily promotes the batch to the u128 sibling (see
    :meth:`_promote_wide_kernel`). When NO compiled kernel is available the
    buffer transparently falls back to the pure-Python
    :func:`brutex.engine.trade_sim.simulate_trades` per mask — the SAME oracle
    both Rust kernels are verified against — so the trades are identical, and
    the fallback is LOGGED (``rust_trade_sim.fallback`` WARNING), never a
    silent divergence. Honest envelope: the production sweep hard-imports
    ``fno_math`` at module scope (``sweep_driver.py`` / ``combo_mask.py``), so
    a truly wheel-ABSENT environment fails at import long before this seam —
    the real production fallback trigger is a STALE wheel that imports but
    lacks a kernel class (the ``AttributeError`` arms), plus the
    kernel-envelope guards documented on the module.

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
        "_rust",
        "_rust_is_wide",
        "_rust_wide",
        "_tf_minutes",
        "_ts_certified",
        "_wide_unavailable",
    )

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

        # Lazily-promoted u128 sibling kernel state (DEC-TRADE-SIM-U128-KERNEL-001):
        # built at most ONCE, on the first >64-bit-mask batch over a
        # u64-resident window; ``_wide_unavailable`` memoises a stale-wheel
        # verdict so a sticky wide-mask sweep never re-attempts the import per
        # chunk.
        self._rust_wide: object | None = None
        self._wide_unavailable = False
        self._rust_is_wide = False

        open_p, high_p, low_p, close_p, forced, session_days = self._derive_kernel_inputs()
        # Kernel-envelope guard (module docstring): an out-of-envelope window —
        # a beyond-u128 bar_bits value or a price beyond the kernel's #1090
        # ceiling — never crosses the FFI boundary; the whole object serves the
        # bit-exact Python oracle instead, and says so LOUDLY (never a silent
        # divergence, never a raise the pure-Python engine would not have
        # raised). ``fno_math`` is imported ONCE, lazily, at the construction
        # just below (the wheel may be absent — the SINGLE import site apart
        # from the wide promotion's), so the resident kernel handle is typed
        # ``object`` with a targeted ignore at its one call site — the same
        # lazy-wheel convention :class:`~brutex.engine.combo_mask.ResidentBarBuffer`
        # uses. A window whose bar_bits fit u64 builds the u64 kernel —
        # byte-identical to the pre-u128 seam; a wider (≤ u128) window builds
        # the u128 kernel; EITHER class missing on a stale wheel lands in the
        # ONE ``wheel_missing`` arm.
        self._rust: object | None = None
        reason = _construction_fallback_reason(self._bar_bits, open_p, high_p, low_p, close_p)
        if reason is None:
            wide = any(b > U64_MASK for b in self._bar_bits)
            try:
                import fno_math

                if wide:
                    self._rust = fno_math.ResidentSweepSimU128(
                        open_p, high_p, low_p, close_p, forced, session_days, self._bar_bits
                    )
                    self._rust_is_wide = True
                else:
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
                **(
                    _width_offender_fields(self._bar_bits)
                    if reason == "bar_bits_beyond_u128"
                    else {}
                ),
            )

    def _derive_kernel_inputs(
        self,
    ) -> tuple[list[int], list[int], list[int], list[int], list[bool], list[int]]:
        """Derive the resident kernel's per-bar input columns from the bars.

        Returns ``(open_p, high_p, low_p, close_p, forced, session_days)`` —
        the SAME canonical Python seams both kernel widths are fed from
        (called once at construction, and once more only if a >64-bit mask
        batch later promotes the window to the u128 sibling kernel; a pure
        function of the immutable ``_bars`` + ``_tf_minutes``, so both
        derivations are identical by construction).

        The forced-exit flags are SESSION-AWARE — byte-identical to
        :func:`~brutex.engine.trade_sim.simulate_trades` — so the boundary is
        the bar session's own close − 10min (15:20 IST on a regular day, the
        early close − 10min on a half-day / Muhurat session), resolved per IST
        date via the COMMON strict ``get_session_checked`` →
        :func:`~brutex.calendars.nse.forced_exit_seconds_for_session`; an
        in-coverage date with no session falls back to 15:20
        (DEC-SESSION-FORCED-EXIT-001), while a bar dated on a weekday OUTSIDE
        the published holiday span raises ``HolidayDataUnavailableError`` —
        LOUDLY, in lock-step with the streaming walker's
        ``_forced_exit_seconds`` (the parity contract holds: both paths refuse
        the same uncoverable date instead of silently fabricating a 15:20
        boundary). The boundary is memoised by the LAST IST date only (a
        1-slot cache, O(1) per bar) — a chronological feed re-resolves once
        per new day, exactly as the streaming walker does, so the precomputed
        Rust-kernel flags and the Python-fallback exits never diverge.

        ``session_days`` is the per-bar SESSION identity — the bar's own IST
        calendar-date ordinal, the SAME date key the forced-exit resolution
        uses — threaded to the kernel so a pending entry armed on a session's
        last stored bar is DROPPED at the next session's open instead of
        filling ~18h stale across the overnight gap (R2-OC-2; mirrors the
        walker's ``_pending_entry_day`` gate bit-for-bit). Bar ``i``'s session
        derives from bar ``i``'s own timestamp — knowable at bar ``i`` (P-06:
        no look-ahead).
        """
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
            forced.append(is_forced_exit_bar(ts_ist.time(), self._tf_minutes, cache_seconds))
            session_days.append(bar_day.toordinal())
        open_p = [_price_to_paisa(bar.open) for bar in self._bars]
        high_p = [_price_to_paisa(bar.high) for bar in self._bars]
        low_p = [_price_to_paisa(bar.low) for bar in self._bars]
        close_p = [_price_to_paisa(bar.close) for bar in self._bars]
        return open_p, high_p, low_p, close_p, forced, session_days

    def _promote_wide_kernel(self) -> object | None:
        """Build (at most once) the u128 sibling kernel for >64-bit mask batches.

        DEC-TRADE-SIM-U128-KERNEL-001: a mask in ``(U64_MASK, _U128_MASK]``
        over a u64-resident window promotes the WHOLE batch to
        ``fno_math.ResidentSweepSimU128`` — built lazily HERE from the same
        :meth:`_derive_kernel_inputs` columns (one more ``O(N_bars)``
        derivation + FFI copy, paid once per instance, announced with ONE
        ``rust_trade_sim.wide_kernel_promoted`` INFO) and memoised, so the
        chunked sweep's subsequent wide batches are pure kernel calls. Returns
        ``None`` when the installed wheel predates the class (memoised via
        ``_wide_unavailable`` — no per-chunk re-import attempts); the caller
        then falls back to the oracle with the warn-once
        ``wheel_missing_u128`` reason. Reachable only from a monkeypatched /
        hand-imported stale module: a genuinely stale installed ``.so`` fails
        at ``import fno_math`` (the shim's import-time symbol handshake).
        """
        if self._rust_wide is not None:
            return self._rust_wide
        if self._wide_unavailable:
            return None
        try:
            import fno_math

            kernel_cls = fno_math.ResidentSweepSimU128
        except (ImportError, AttributeError):
            self._wide_unavailable = True
            return None
        open_p, high_p, low_p, close_p, forced, session_days = self._derive_kernel_inputs()
        self._rust_wide = kernel_cls(
            open_p, high_p, low_p, close_p, forced, session_days, self._bar_bits
        )
        _LOG.info(
            "rust_trade_sim.wide_kernel_promoted",
            n_bars=len(self._bars),
            tf_minutes=self._tf_minutes,
        )
        return self._rust_wide

    @property
    def n_bars(self) -> int:
        """Number of bars in the resident window."""
        return len(self._bars)

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
            The strategy masks to simulate — u64-fit masks ride the u64
            kernel; masks up to ``_U128_MASK`` ride the u128 kernel
            (DEC-TRADE-SIM-U128-KERNEL-001); wider values route to the
            oracle.
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
            # Per-call kernel-envelope guard (module docstring): a beyond-u128
            # mask or an SL/TP distance beyond the kernel's #1090 ceiling
            # routes THIS call to the bit-exact Python oracle — loudly on the
            # FIRST occurrence per (instance, reason); repeats fall back
            # without re-warning (the chunked sweep would otherwise emit one
            # WARNING per chunk — a log flood on a sticky reason).
            call_reason = _call_fallback_reason(masks, stop_loss_paisa, take_profit_paisa)
            if call_reason is None and not self._rust_is_wide and any(m > U64_MASK for m in masks):
                # A >64-bit mask over a u64-resident window is NOT a fallback
                # (DEC-TRADE-SIM-U128-KERNEL-001): the WHOLE batch is promoted
                # to the lazily-built u128 sibling kernel — u64-fit batches
                # keep taking the u64 kernel byte-identically — unless the
                # installed wheel predates the class (→ warn-once oracle).
                rust = self._promote_wide_kernel()
                if rust is None:
                    call_reason = "wheel_missing_u128"
            if call_reason is not None:
                if call_reason not in self._call_fallback_warned:
                    self._call_fallback_warned.add(call_reason)
                    # The width reason additionally NAMES the offending mask
                    # (value + bit) — spec PR-1, DEC-SWEEP-RUST-WIRE-001 part 1.
                    _LOG.warning(
                        "rust_trade_sim.call_fallback",
                        reason=call_reason,
                        n_masks=len(masks),
                        **(
                            _width_offender_fields(masks)
                            if call_reason == "mask_beyond_u128"
                            else {}
                        ),
                    )
                rust = None
        if rust is None:
            # Pure-Python fallback: the exact oracle (the SAME walker
            # simulate_trades wraps), one mask at a time. The walker's exit
            # indices are TRUE indices and monotonic by construction; they
            # are emitted under the same window certificate as the kernel's.
            out: list[tuple[list[ClosedTrade], list[int] | None]] = []
            for mask in masks:
                walker = TradeSimWalker(
                    direction=direction,
                    tf_minutes=self._tf_minutes,
                    stop_loss_paisa=stop_loss_paisa,
                    take_profit_paisa=take_profit_paisa,
                    policy=policy,
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
        pairs: list[tuple[list[ClosedTrade], list[int] | None]] = []
        for mask_records in raw:
            mask_trades: list[ClosedTrade] = []
            mask_exit_indices: list[int] = []
            for record in mask_records:
                trade, exit_idx = self._to_trade_and_exit(record, direction)
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
        self, record: tuple[int, int, int, int, int, int], direction: Side
    ) -> tuple[ClosedTrade, int]:
        """Map one Rust trade tuple → ``(ClosedTrade, exit_idx)`` (idx→bar timestamps).

        The exit bar index — which the kernel record already carries and the
        pre-#1186 mapping discarded — is returned alongside the trade so the
        metrics tail can skip its per-mask ``exit_ts``→index dict build
        (DEC-SWEEP-EXIT-INDEX-FASTPATH-001).
        """
        # Lazy import (numpy-load-once guard; see the module/TYPE_CHECKING note).
        from brutex.metrics.types import ClosedTrade

        pnl, mfe, mae, holding, entry_idx, exit_idx = record
        trade = ClosedTrade(
            pnl_paisa=pnl,
            mfe_paisa=mfe,
            mae_paisa=mae,
            holding_bars=holding,
            entry_ts=self._bars[entry_idx].timestamp,
            exit_ts=self._bars[exit_idx].timestamp,
            direction=direction,
        )
        return trade, exit_idx
