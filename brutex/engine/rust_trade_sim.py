"""Resident Rust trade-sim seam — the hot dual-feed trade-walk reused across masks.

DEC-SWEEP-RUST-TRADESIM-001 (MIGRATION.md #563), rebuilt on the 1-minute
EXECUTION grid by DEC-FILL-EXEC-GRID-1M-001 (the PR-B design,
``docs/design/fills/PRB_1M_EXECUTION_GRID_DESIGN.md``). The Python side of the
Rust Stage-2 trade-simulation kernel (``fno_math.ResidentSweepSim``). It is
the trade-sim analogue of :class:`brutex.engine.combo_mask.ResidentBarBuffer`:
the expensive per-(instrument, tf) inputs — the 1-minute EXECUTION stream's
OHLC→paisa conversion (:func:`brutex.engine.stage1a._price_to_paisa`), its
SESSION-AWARE forced-exit flags (:func:`brutex.calendars.nse.is_forced_exit_bar`
at 1-minute granularity against each bar session's close − 10min;
DEC-SESSION-FORCED-EXIT-001), its per-bar IST session identity (each 1m bar's
IST calendar-date ordinal — the open-position ``session_gap`` guard's key), the
:class:`~brutex.engine.exec_grid.ExecGridMap`'s ``arm_source`` projection
column (which 1m bar fills which DECISION bar's signal — the mapping law
``exec_idx(N) = first ts_1m >= ts_tf[N] + tf_minutes`` with the
same-session/B6/B4 gates already applied at build time), and the Stage-1A
DECISION-length ``bar_bits`` truth-vectors — are computed ONCE in Python (the
single source of truth, CLAUDE.md §11 COMMON) and handed to a resident Rust
buffer. Thereafter a whole batch of strategy masks is simulated into
round-trip :class:`~brutex.metrics.types.ClosedTrade` objects WITHOUT
re-marshalling the windows per combo.

## The dual-feed shape (design §3.4)

Signals live on the DECISION timeframe (``bar_bits`` — Stage-1A untouched);
fills + management live on the 1-minute EXECUTION stream. The kernel walks the
1m stream; on a 1m bar ``j`` with ``arm_source[j] = N >= 0`` it evaluates
``bar_bits[N] & mask == mask`` and fills a due entry at THAT bar's worst-case
adverse extreme. Every ``TradeRecord`` index/holding is therefore a 1-MINUTE
quantity (design §2.6); NO timestamp ever crosses the FFI.

## Why this is bit-identical to the Python walker (Golden Rule #4)

Because the price→paisa conversion, the forced-exit detection and the
``arm_source`` mapping — the seams a naive port would most easily get wrong —
are done in Python by the SAME functions
:func:`brutex.engine.trade_sim.simulate_trades` uses, the Rust kernel receives
identical int64 inputs and only reproduces the per-bar SL/TP/forced
state-machine branch order. The result is byte-identical trades; the
:meth:`ResidentTradeSim.simulate_masks` fallback below IS
:func:`~brutex.engine.trade_sim.simulate_trades` (so an environment without
the compiled wheel produces the exact same trades, never a silent divergence).

## Honest envelope (Golden Rule #10) — NOT O(1) total

* Construction is ``O(N_1m + N_tf)`` — the one-time paisa/forced/exec-map
  derivation + the resident copy across the FFI boundary, paid ONCE.
* :meth:`ResidentTradeSim.simulate_masks` is ``O(N_masks · N_1m)`` compute in
  the worst case (each mask walks the whole 1m window; the kernel's flat-skip
  ``next_arm`` jump table makes a FLAT stretch O(armed bars), design §1.5) +
  ``O(Σ trades)`` output. Claiming O(1) total is a physical impossibility and
  is FORBIDDEN.

## Kernel-envelope guards (DEC-SWEEP-RUST-TRADESIM-WIRE-001 + DEC-TRADE-SIM-U128-KERNEL-001)

TWO compiled kernels share one width-generic Rust core
(``rust/fno-math/src/trade_sim.rs`` over ``rust/fno-math/src/mask_word.rs``
``MaskWord``): the u64-native ``ResidentSweepSim`` (serves today's production
masks — every IMPLEMENTED bit is <= 63, so a u64 word still holds the full
active vocabulary; flipping bit 64+ live — the declared SuperTrend pair —
moves production onto the u128 kernel) and the u128 ``ResidentSweepSimU128``
(the ``MAX_VOCAB_BITS = 128`` headroom — >64-bit masks/bar_bits are now
Rust-served instead of oracle-served). Both validate every paisa input against
the overflow-safe ceiling ``MAX_PRICE_PAISA = i64::MAX / 8`` (#1090). An input
outside the WIDEST kernel's envelope must still NEVER surface as a PyO3
``OverflowError`` / kernel ``ValueError`` where the pure-Python oracle would
simply simulate (a wheel-present vs wheel-absent behaviour asymmetry), so every
such input routes to the bit-exact Python oracle instead, LOUDLY (one structlog
WARNING, never silent):

* at CONSTRUCTION: ``bar_bits`` all inside ``[0, U64_MASK]``
  (:data:`~brutex.engine.combo_mask.U64_MASK`) build the u64 kernel; any value
  in ``(U64_MASK, _U128_MASK]`` builds the u128 kernel instead (``rust_active``
  True either way, one ``except (ImportError, AttributeError)`` →
  ``wheel_missing`` covers both); a value outside ``[0, _U128_MASK]`` routes
  the WHOLE object to the oracle with reason ``bar_bits_beyond_u128``, as does
  any EXECUTION price column value outside ``[-MAX_PRICE_PAISA,
  MAX_PRICE_PAISA]`` paisa (``price_out_of_envelope``). A wheel whose kernel
  class PREDATES the exec-grid constructor (DEC-FILL-EXEC-GRID-1M-001 — no
  ``arm_source`` parameter; its single-feed walk prices the RETIRED
  decision-TF law) demotes the WHOLE object to the dual-feed oracle with
  warn-once ``exec_grid_kernel_stale`` (signature-inspected BEFORE
  construction — never a constructor ``TypeError``, never a wrong-law kernel
  answer; see :func:`_kernel_exec_grid_current`);
* per :meth:`ResidentTradeSim.simulate_masks` CALL (that call only): a mask
  outside ``[0, _U128_MASK]`` → ``mask_beyond_u128`` → oracle, an SL/TP
  distance above ``MAX_PRICE_PAISA`` → oracle. A mask in ``(U64_MASK,
  _U128_MASK]`` over a u64-resident window is NOT a fallback: the u128
  sibling kernel is built LAZILY (at most once per instance, one
  ``rust_trade_sim.wide_kernel_promoted`` INFO) and the WHOLE batch is routed
  wide — u64-fit batches keep the u64 kernel byte-identical. Only when the
  installed wheel PREDATES the u128 class (or its u128 class predates the
  exec-grid constructor) does the wide batch fall back to the oracle,
  warn-once ``wheel_missing_u128`` (reachable only from a monkeypatched /
  hand-imported stale module: the shim's import-time handshake makes a
  genuinely stale ``.so`` fail at ``import fno_math``). Likewise a batch over
  a wheel PREDATING the worst-case-only kernel (DEC-FILL-WORST-CASE-ONLY-001
  — the retired ``worst_case`` / ``sl_next_open`` / ``tp_next_open`` /
  ``tp_first`` mode flags on ``simulate_masks``) demotes to the bit-exact
  worst-case oracle with warn-once ``fill_law_kernel_stale``
  (signature-inspected — see :func:`_kernel_fill_law_current`); wheel state
  can therefore never change results, only the serving engine.
  Every per-call WARNING is emitted ONCE per ``(instance, reason)`` — the
  production sweep calls ``simulate_masks`` once per bounded chunk (up to
  thousands of chunks per tuple), so a sticky-reason warning on EVERY chunk
  would flood the logs; the first occurrence is loud, repeats of the same
  already-warned reason fall back silently (the path is still observable per
  tuple via ``sweep_run.trade_sim_path``).

A non-positive SL/TP distance is rejected with ``ValueError`` IDENTICALLY on
both arms (the kernel would silently accept ``0`` where the Python walker
raises — the wrapper normalises the domain so wheel state can never change the
error behaviour).

## Observability delta vs the Python walker (STATED per Golden Rule #10)

The Python walker (:func:`brutex.engine.trade_sim.simulate_trades`) emits
per-trade ``trade_sim.entry`` / ``trade_sim.exit`` DEBUG structlog events plus
the walker-level deferral/collision/slide counters
(DEC-FILL-EXEC-GRID-1M-001 §L); the compiled kernel emits NONE — deliberately
(that per-trade logging is part of the interpreter tax the wiring removes).
So under DEBUG-level structlog, per-trade telemetry appears ONLY on tuples
served by the Python fallback arm. The per-TUPLE signals
(``sweep_run.trade_sim_path``, the fallback WARNINGs, the
:class:`~brutex.engine.exec_grid.ExecGridMap` build events) are
engine-independent.
"""

from __future__ import annotations

import inspect
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Final

import structlog

from brutex.calendars.nse import (
    IST,
    forced_exit_seconds_for_session,
    get_session_checked,
    is_forced_exit_bar,
)
from brutex.config.rust_strict import refuse_if_strict
from brutex.engine.combo_mask import MAX_VOCAB_BITS, U64_MASK, mask_to_entry_signals
from brutex.engine.exec_grid import ExecGridMap, build_exec_grid_map
from brutex.engine.exit_index import bars_strictly_increasing

# Intentional friend access (DEC-P0-017 per-site convention): ``_price_to_paisa``
# is the canonical Decimal-rupees → int64-paisa seam — the SAME one
# :mod:`brutex.engine.trade_sim` uses (CLAUDE.md §11 COMMON), so the resident Rust
# kernel's int64 inputs match the Python walker's bit-for-bit.
from brutex.engine.stage1a import _price_to_paisa  # pyright: ignore[reportPrivateUsage]
from brutex.engine.trade_sim import TradeSimWalker
from brutex.strategies.types import Side

if TYPE_CHECKING:  # pragma: no cover - import only for static typing
    from brutex.data.models import CandleBar
    from brutex.engine.reference_series import TradeReferenceStamper

    # ``ClosedTrade`` is imported lazily at its construction site (see the note in
    # :mod:`brutex.engine.trade_sim`) to keep numpy out of the engine package's
    # collection-time import chain (the per-module strict-coverage gate's
    # numpy-load-once guard).
    from brutex.metrics.types import ClosedTrade

__all__ = ["PriceColumns", "ResidentTradeSim", "derive_price_columns"]

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

#: Cap on ``trade_sim.session_tail_hole`` WARNs per kernel-input derivation
#: (DEC-TRADESIM-OPEN-SESSION-GUARD-001) — everything past the cap rolls into
#: ONE ``trade_sim.session_tail_hole_suppressed`` summary (the house
#: preopen.prev_close_noncontiguous capped-warn precedent). Mask-independent
#: by construction: the detection runs ONCE per tuple at derivation over the
#: 1-minute EXECUTION stream, never per mask and never inside the walker's
#: ``_close`` (the DEC-SWEEP-LOG-VOLUME-GUARD-001 #1630 posture).
_TAIL_HOLE_WARN_CAP: Final[int] = 20

#: The facade name every strict refusal from this module carries.
_STRICT_FACADE: Final[str] = "brutex.engine.rust_trade_sim"

#: CLASS-C fall-back reasons — the INPUT sits outside BOTH compiled kernels'
#: FFI envelope, so the pure-Python oracle was ALWAYS the intended handler
#: (DEC-RUST-STRICT-NO-FALLBACK-001 §"Scope note"). Refusing here would crash a
#: legitimate call; widening the crate's envelope — never refusing — is the
#: real fix for each. Every one is produced by
#: :func:`_construction_fallback_reason` / :func:`_call_fallback_reason`, i.e.
#: decided BEFORE any kernel is reached — the crate never ran:
#:
#: * ``bar_bits_beyond_u128`` — a ``bar_bits`` word outside ``[0, 2**128-1]``;
#:   beyond even ``Vec<u128>`` (the real fix is a wider kernel word).
#: * ``mask_beyond_u128`` — the per-call twin for a batch mask.
#: * ``price_out_of_envelope`` / ``stop_loss_out_of_envelope`` /
#:   ``take_profit_out_of_envelope`` — a paisa magnitude past the kernel's
#:   overflow-safe ``MAX_PRICE_PAISA = i64::MAX / 8`` ceiling (#1090); the real
#:   fix is a wider kernel accumulator, not a crash on a legal price.
#:
#: NOT exempt (CLASS A — the crate is absent or stale, which is never a
#: legitimate input shape): ``wheel_missing`` (**the u64 production degrade** —
#: today's 69-bit vocabulary keeps every real batch on the u64 kernel, so this
#: is the arm that actually fires in production), ``wheel_missing_u128``,
#: ``wheel_missing_exit_counts``, ``exec_grid_kernel_stale``,
#: ``fill_law_kernel_stale``.
_STRICT_EXEMPT_REASONS: Final[frozenset[str]] = frozenset(
    {
        "bar_bits_beyond_u128",
        "mask_beyond_u128",
        "price_out_of_envelope",
        "stop_loss_out_of_envelope",
        "take_profit_out_of_envelope",
    }
)


def _refuse_if_strict(reason: str, **context: object) -> None:
    """Refuse a Python fall-back under strict mode unless *reason* is CLASS C.

    The module's ONE strict-mode choke point (DEC-RUST-STRICT-NO-FALLBACK-001).
    This facade has no named ``warn_fallback_once`` — it warns inline at two
    sites (construction demotion + per-call demotion), so both call this helper
    BEFORE their warn-once memo write, making an already-warned repeat
    structurally unable to slip past.
    """
    if reason in _STRICT_EXEMPT_REASONS:
        return
    refuse_if_strict(_STRICT_FACADE, reason, **context)


def _warn_session_tail_holes(forced: Sequence[bool], session_days: Sequence[int]) -> None:
    """WARN once per session-tail data hole in the window (capped, mask-independent).

    A *session-tail hole* is a maximal run of consecutive 1-minute EXECUTION
    bars sharing one session ordinal that (i) is NOT the window's final
    session and (ii) contains NO bar with ``forced=True`` — exactly the shape
    on which the walker's (A2) open-position session guard fires a
    ``session_gap`` close (DEC-TRADESIM-OPEN-SESSION-GUARD-001 on the 1m
    stream): the session's stored bars ended before any forced-exit-window
    bar, so a position opened in it can only close retroactively at its last
    stored 1m bar. The FINAL session is exempt — an incomplete final session
    is the ordinary end-of-window shape the ``end_of_data`` close already
    names honestly.

    Emits at most :data:`_TAIL_HOLE_WARN_CAP` ``trade_sim.session_tail_hole``
    WARNs (fields: ``session`` — the ISO date from the ordinal — and ``bars``,
    the session's stored-bar count) plus ONE
    ``trade_sim.session_tail_hole_suppressed`` summary carrying the total when
    the cap is exceeded. One O(N_1m) pass, paid at the already-O(N_1m)
    derivation. The pure-Python re-sim paths (capture / cascade / robustness /
    CLI) deliberately get no WARN — the ``exits_session_gap`` aggregate
    counter is their persistent signal; data-hole ALERTING stays owned by the
    #1031 detectors.
    """
    holes: list[tuple[int, int]] = []
    run_start = 0
    n = len(session_days)
    for i in range(1, n + 1):
        if i < n and session_days[i] == session_days[run_start]:
            continue
        # A maximal run [run_start, i) just ended. The FINAL run (i == n) is
        # exempt; a prior run with no forced bar is a session-tail hole.
        if i < n and not any(forced[run_start:i]):
            holes.append((session_days[run_start], i - run_start))
        run_start = i
    for ordinal, bar_count in holes[:_TAIL_HOLE_WARN_CAP]:
        _LOG.warning(
            "trade_sim.session_tail_hole",
            session=date.fromordinal(ordinal).isoformat(),
            bars=bar_count,
        )
    if len(holes) > _TAIL_HOLE_WARN_CAP:
        _LOG.warning(
            "trade_sim.session_tail_hole_suppressed",
            total=len(holes),
            shown=_TAIL_HOLE_WARN_CAP,
        )


@dataclass(frozen=True, slots=True)
class PriceColumns:
    """The tf-independent per-bar kernel input columns for ONE bar stream.

    The four OHLC paisa columns plus the per-bar IST-date ``session_days``
    ordinals — everything in the resident kernel's input set that is a pure
    function of ONE bar sequence alone (the ``forced`` flags and the
    ``arm_source`` projection stay per-instance). Under the 1-minute
    execution grid (DEC-FILL-EXEC-GRID-1M-001) the kernel consumes columns
    derived from the EXECUTION 1m stream — a kernel holder passing
    ``precomputed_columns`` must derive them from the SAME ``exec_bars_1m``
    it hands the sim (mismatched lengths refuse loudly at construction).
    Derived once per kernel lifetime by :func:`derive_price_columns` and
    shareable across every :class:`ResidentTradeSim` built over the same
    stream (DEC-STAGE1A-RUST-BATCH-001, the W2 rider). The columns are
    immutable ``tuple``\\s — the SAME object is shared by the kernel and every
    sim built from it, so a mutable list here would let one consumer corrupt
    every sibling in place (review LOW-2 hardening; PyO3 ``Vec`` extraction
    accepts any sequence, so the FFI edge is unchanged).

    Raises
    ------
    ValueError
        If the five columns do not share one length (a malformed hand-built
        instance — :func:`derive_price_columns` always produces consistent
        columns).
    """

    open_paisa: tuple[int, ...]
    high_paisa: tuple[int, ...]
    low_paisa: tuple[int, ...]
    close_paisa: tuple[int, ...]
    session_days: tuple[int, ...]

    def __post_init__(self) -> None:
        n = len(self.open_paisa)
        for name, column in (
            ("high_paisa", self.high_paisa),
            ("low_paisa", self.low_paisa),
            ("close_paisa", self.close_paisa),
            ("session_days", self.session_days),
        ):
            if len(column) != n:
                raise ValueError(
                    f"PriceColumns: {name} length {len(column)} != open_paisa length {n}"
                )

    @property
    def n_bars(self) -> int:
        """Number of bars the columns cover."""
        return len(self.open_paisa)


def derive_price_columns(bars: Sequence[CandleBar]) -> PriceColumns:
    """Derive the per-bar kernel input columns from *bars* (pure).

    The four ``_price_to_paisa`` comprehensions + the per-bar IST-date
    ordinals (the SAME canonical seams ``ResidentTradeSim`` uses internally,
    byte-identical output — golden-pinned) so a caller holding a
    :class:`~brutex.engine.sweep_kernel.ResidentSweepKernel` can pay them
    ONCE per stream instead of once per tuple. Under the 1-minute execution
    grid the kernel-facing columns are the EXECUTION stream's — pass
    ``exec_bars_1m`` here when precomputing for :class:`ResidentTradeSim`.
    Bar ``i``'s values derive from bar ``i``'s own fields (P-06: no
    look-ahead).

    Parameters
    ----------
    bars
        Chronologically ordered bars of ONE stream.

    Returns
    -------
    PriceColumns
        The five per-bar columns, positionally aligned with *bars*.
    """
    return PriceColumns(
        open_paisa=tuple(_price_to_paisa(bar.open) for bar in bars),
        high_paisa=tuple(_price_to_paisa(bar.high) for bar in bars),
        low_paisa=tuple(_price_to_paisa(bar.low) for bar in bars),
        close_paisa=tuple(_price_to_paisa(bar.close) for bar in bars),
        session_days=tuple(bar.timestamp.astimezone(IST).date().toordinal() for bar in bars),
    )


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
    change behaviour), and an EXECUTION-stream paisa price outside
    ``[-MAX_PRICE_PAISA, MAX_PRICE_PAISA]`` would raise the #1090 boundary
    ``ValueError`` in either kernel. Either routes the WHOLE object to the
    pure-Python oracle (bit-identical either way). A value in ``(U64_MASK,
    _U128_MASK]`` is NOT a fallback — the constructor builds the u128 kernel
    for it. One ``O(N)`` scan, paid once at the already-``O(N)`` construction.
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
    ``O(N_masks · N_1m)`` sweep.
    """
    if any(not 0 <= m <= _U128_MASK for m in masks):
        return "mask_beyond_u128"
    if stop_loss_paisa is not None and stop_loss_paisa > _MAX_KERNEL_PRICE_PAISA:
        return "stop_loss_out_of_envelope"
    if take_profit_paisa is not None and take_profit_paisa > _MAX_KERNEL_PRICE_PAISA:
        return "take_profit_out_of_envelope"
    return None


#: The retired kernel mode flags (DEC-FILL-WORST-CASE-ONLY-001): a kernel
#: whose ``simulate_masks`` signature still carries ANY of them predates the
#: worst-case-only law — its flagless call would price the retired PRECISE
#: defaults, so the seam demotes such a wheel to the worst-case oracle.
_STALE_KERNEL_FLAGS: Final[tuple[str, ...]] = (
    "worst_case",
    "sl_next_open",
    "tp_next_open",
    "tp_first",
)


def _kernel_fill_law_current(kernel: object) -> bool:
    """``True`` iff *kernel*'s batch entry implements the worst-case-only law.

    The FEATURE-DETECTION half of the DEC-FILL-WORST-CASE-ONLY-001 kernel
    seam (the lazy-promotion / ``wheel_missing_u128`` style): an installed
    wheel that PREDATES the worst-case-only kernel still exposes the retired
    mode flags (``worst_case`` / ``sl_next_open`` / ``tp_next_open`` /
    ``tp_first``) whose DEFAULTS price the retired PRECISE law — handing it
    the new flagless call would SILENTLY produce precise fills, the exact
    illusion this DEC deletes. So the seam inspects the kernel's
    ``simulate_masks`` signature ONCE (PyO3 emits ``__text_signature__``, so
    :func:`inspect.signature` resolves it) and routes every batch on a stale
    wheel to the bit-exact worst-case Python oracle instead (reason
    ``fill_law_kernel_stale`` — never a wrong kernel answer, never a crash).
    A signature that cannot be inspected at all (``ValueError`` on a C
    callable without ``__text_signature__``) reads as STALE — the
    conservative arm: the oracle serves the batch correctly either way.
    """
    try:
        signature = inspect.signature(type(kernel).simulate_masks)  # type: ignore[attr-defined]
    except (AttributeError, TypeError, ValueError):
        return False
    return not any(flag in signature.parameters for flag in _STALE_KERNEL_FLAGS)


def _kernel_exec_grid_current(kernel_cls: object) -> bool:
    """``True`` iff *kernel_cls*'s constructor implements the exec-grid law.

    The DEC-FILL-EXEC-GRID-1M-001 twin of :func:`_kernel_fill_law_current`,
    probed BEFORE construction: an installed wheel whose kernel class
    PREDATES the 1-minute execution grid takes the retired single-feed
    7-column constructor — handing it the new 8-argument call would raise
    ``TypeError`` (or worse, a monkeypatched near-miss could silently walk
    the RETIRED decision-TF law). So the seam inspects the class constructor
    signature ONCE (PyO3 emits the class ``__text_signature__``, so
    :func:`inspect.signature` resolves it) and demotes a stale wheel's WHOLE
    window to the bit-exact dual-feed Python oracle instead (reason
    ``exec_grid_kernel_stale`` — never a wrong kernel answer, never a
    crash). The current constructor is recognised by its ``arm_source``
    parameter (the exec-grid projection column — no pre-PR-B constructor
    ever carried it). A signature that cannot be inspected at all reads as
    STALE — the conservative arm: the oracle serves the window correctly
    either way.
    """
    try:
        signature = inspect.signature(kernel_cls)  # type: ignore[arg-type]
    except (AttributeError, TypeError, ValueError):
        return False
    return "arm_source" in signature.parameters


def _monotonic(exit_indices: Sequence[int]) -> bool:
    """Certify one mask's exit-index stream: non-negative + strictly increasing.

    DEC-SWEEP-EXIT-INDEX-FASTPATH-001, the kernel-record certification: the
    single-position walker (which the kernel mirrors bit-for-bit) closes at
    most one trade per 1m bar and emits trades in close order, so a
    well-formed per-mask exit-index stream is strictly increasing and starts
    at ``>= 0``. A stream failing this certificate signals a kernel
    ordering/marshalling regression — the caller withholds the indices (the
    metrics tail then serves the mask through the bit-identical
    timestamp-dict path) rather than trusting them. ``O(trades)`` per mask,
    zero allocation.
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
    width that cannot fit u128 is bit 128; today's vocabulary — implemented
    bits <= 63, declared layout <= 65 — never triggers this). ``offending_bit`` is the word's highest set bit for the
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
    """Hold one dual-feed ``(instrument, tf)`` window resident for repeated mask sims.

    Build it once from the DECISION bars + their Stage-1A ``bar_bits`` + the
    1-minute EXECUTION stream; then call :meth:`simulate_masks` for each mask
    batch. The OHLC→paisa conversion (EXECUTION stream), the 1m forced-exit
    flags, the :class:`~brutex.engine.exec_grid.ExecGridMap` and the resident
    bar copy are paid once at construction, not once per mask.

    The compiled kernel is width-dispatched (DEC-TRADE-SIM-U128-KERNEL-001):
    u64-fit ``bar_bits`` build ``fno_math.ResidentSweepSim`` , wider (≤ u128)
    ``bar_bits`` build ``fno_math.ResidentSweepSimU128``, and a >64-bit MASK
    over a u64-resident window lazily promotes the batch to the u128 sibling
    (see :meth:`_promote_wide_kernel`). When NO compiled kernel is available
    (or the installed one predates the exec-grid constructor —
    ``exec_grid_kernel_stale``) the buffer transparently falls back to the
    pure-Python :func:`brutex.engine.trade_sim.simulate_trades` per mask — the
    SAME dual-feed oracle both Rust kernels are verified against — so the
    trades are identical, and the fallback is LOGGED
    (``rust_trade_sim.fallback`` WARNING), never a silent divergence. Honest
    envelope: the production sweep hard-imports ``fno_math`` at module scope
    (``sweep_driver.py`` / ``combo_mask.py``), so a truly wheel-ABSENT
    environment fails at import long before this seam — the real production
    fallback trigger is a STALE wheel, plus the kernel-envelope guards
    documented on the module.

    Parameters
    ----------
    decision_bars
        Chronologically ordered DECISION-TF bars for one ``(instrument,
        interval)`` (non-empty; the signal domain).
    bar_bits
        The Stage-1A per-DECISION-bar truth-vectors for the same window (from
        :func:`brutex.engine.stage1a.build_bar_bits`). Must match
        ``decision_bars`` length.
    tf_minutes
        The DECISION timeframe in whole minutes (``>= 1``), for the
        close-instant mapping law. At ``tf_minutes == 1`` pass the same
        sequence for both streams.
    exec_bars_1m
        The 1-minute EXECUTION stream covering the window (non-empty; the
        fill/walk domain). REQUIRED — no silent single-feed call shape
        survives DEC-FILL-EXEC-GRID-1M-001 (design §3.3).
    exec_map
        Optional pre-built :class:`~brutex.engine.exec_grid.ExecGridMap` for
        this exact stream pair; ``None`` derives it once at construction. A
        supplied map must cover both streams (lengths refuse loudly).
    reference_stamper
        Optional India-VIX
        :class:`~brutex.engine.reference_series.TradeReferenceStamper`
        (DEC-VIX-STAMP-WIRE-001). When supplied, EVERY trade this window emits
        — on the Rust arm AND the Python-oracle fallback arm alike — carries
        ``vix_at_entry`` / ``vix_at_exit`` (REFERENCE ONLY, never a signal
        input; a missing VIX bar stamps ``None``, never fabricated). The Rust
        kernel itself is UNTOUCHED: the stamp happens Python-side in the
        :meth:`_to_trade_and_exit` converter (an O(1) post-stamp per trade),
        so the kernel's trade tuples stay engine-blind and both arms stamp
        through the SAME
        :meth:`~brutex.engine.reference_series.TradeReferenceStamper.stamp`.
        ``None`` (the default) leaves both VIX fields ``None``.
    precomputed_columns
        Optional :class:`PriceColumns` derived from the SAME ``exec_bars_1m``
        (via :func:`derive_price_columns`) — the W2-rider sharing seam. A
        mismatched column set refuses loudly.

    Raises
    ------
    ValueError
        If either stream is empty, ``bar_bits`` length ≠ ``decision_bars``
        length, ``tf_minutes < 1``, or an injected ``exec_map`` /
        ``precomputed_columns`` does not cover the streams.
    brutex.calendars.nse.HolidayDataUnavailableError
        If any bar is dated on a weekday OUTSIDE the published holiday span —
        the session (and thus the forced-exit boundary / session-close
        instant) cannot be verified, so construction refuses LOUDLY instead
        of fabricating a boundary (mirrors
        :class:`brutex.engine.trade_sim.TradeSimWalker` +
        :func:`~brutex.engine.exec_grid.build_exec_grid_map` exactly).
    """

    __slots__ = (
        "_bar_bits",
        "_call_fallback_warned",
        "_decision_bars",
        "_exec_bars",
        "_exec_map",
        "_kernel_inputs",
        "_precomputed_columns",
        "_reference_stamper",
        "_rust",
        "_rust_is_wide",
        "_rust_wide",
        "_tf_minutes",
        "_ts_certified",
        "_wide_unavailable",
    )

    def __init__(
        self,
        decision_bars: Sequence[CandleBar],
        bar_bits: Sequence[int],
        *,
        tf_minutes: int,
        exec_bars_1m: Sequence[CandleBar],
        exec_map: ExecGridMap | None = None,
        reference_stamper: TradeReferenceStamper | None = None,
        precomputed_columns: PriceColumns | None = None,
    ) -> None:
        if not decision_bars:
            raise ValueError("ResidentTradeSim: decision_bars must be non-empty")
        if not exec_bars_1m:
            raise ValueError("ResidentTradeSim: exec_bars_1m must be non-empty")
        if len(bar_bits) != len(decision_bars):
            raise ValueError(
                f"ResidentTradeSim: bar_bits length {len(bar_bits)} != decision_bars "
                f"length {len(decision_bars)}"
            )
        if tf_minutes < 1:
            raise ValueError(f"ResidentTradeSim: tf_minutes must be >= 1, got {tf_minutes}")
        if precomputed_columns is not None and precomputed_columns.n_bars != len(exec_bars_1m):
            # The W2 rider's injection seam (DEC-STAGE1A-RUST-BATCH-001),
            # rebased onto the EXECUTION stream: a mismatched column set is a
            # caller bug — refuse it naming both lengths, never a silently
            # misaligned kernel window.
            raise ValueError(
                f"ResidentTradeSim: precomputed_columns cover {precomputed_columns.n_bars} "
                f"bars != exec_bars_1m length {len(exec_bars_1m)}"
            )
        if exec_map is not None and (
            exec_map.n_decision != len(decision_bars) or exec_map.n_exec != len(exec_bars_1m)
        ):
            raise ValueError(
                f"ResidentTradeSim: exec_map covers ({exec_map.n_decision}, "
                f"{exec_map.n_exec}) bars != streams "
                f"({len(decision_bars)}, {len(exec_bars_1m)})"
            )
        self._decision_bars: tuple[CandleBar, ...] = tuple(decision_bars)
        self._exec_bars: tuple[CandleBar, ...] = tuple(exec_bars_1m)
        self._bar_bits: list[int] = list(bar_bits)
        self._tf_minutes = tf_minutes
        self._reference_stamper = reference_stamper
        # The G-4 unconditional memo: `_derive_kernel_inputs` is a pure
        # function of the immutable `_exec_bars` (its docstring law), so its
        # 6-tuple is derived at most ONCE per instance.
        self._kernel_inputs: (
            tuple[
                tuple[int, ...],
                tuple[int, ...],
                tuple[int, ...],
                tuple[int, ...],
                tuple[bool, ...],
                tuple[int, ...],
            ]
            | None
        ) = None
        self._precomputed_columns = precomputed_columns
        # The ONE exec-grid mapping (design §1.2): built here when not
        # injected — the same map the Python-oracle fallback threads into
        # every walker run, so both arms consume the identical arm_source
        # projection (never a per-arm re-derivation drift).
        self._exec_map: ExecGridMap = (
            exec_map
            if exec_map is not None
            else build_exec_grid_map(self._decision_bars, self._exec_bars, tf_minutes=tf_minutes)
        )
        # Exit-index certification (DEC-SWEEP-EXIT-INDEX-FASTPATH-001 on the
        # 1m grid): the EXECUTION stream's timestamps strictly increase ⇒
        # index↔timestamp is a bijection over the walk domain ⇒ a trade's
        # TRUE 1m exit index provably equals the metrics tail's last-wins
        # dict lookup, so the indices may replace it. Paid ONCE here.
        self._ts_certified: bool = bars_strictly_increasing(self._exec_bars)
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
        # Kernel-envelope guard (module docstring): an out-of-envelope window
        # never crosses the FFI boundary; the whole object serves the
        # bit-exact Python oracle instead, and says so LOUDLY (never a silent
        # divergence, never a raise the pure-Python engine would not have
        # raised). ``fno_math`` is imported ONCE, lazily, at the construction
        # just below (the wheel may be absent — the SINGLE import site apart
        # from the wide promotion's), so the resident kernel handle is typed
        # ``object`` with a targeted ignore at its one call site. A window
        # whose bar_bits fit u64 builds the u64 kernel; a wider (≤ u128)
        # window builds the u128 kernel; EITHER class missing on a stale
        # wheel lands in the ONE ``wheel_missing`` arm, and a class carried
        # by a wheel that PREDATES the exec-grid constructor is demoted
        # BEFORE construction (``exec_grid_kernel_stale`` — never a
        # constructor TypeError; :func:`_kernel_exec_grid_current`).
        self._rust: object | None = None
        reason = _construction_fallback_reason(self._bar_bits, open_p, high_p, low_p, close_p)
        if reason is None:
            wide = any(b > U64_MASK for b in self._bar_bits)
            try:
                import fno_math

                kernel_cls: object = (
                    fno_math.ResidentSweepSimU128 if wide else fno_math.ResidentSweepSim
                )
            except (ImportError, AttributeError):
                reason = "wheel_missing"
            else:
                if not _kernel_exec_grid_current(kernel_cls):
                    reason = "exec_grid_kernel_stale"
                else:
                    # Fresh list copies at the FFI edge only: the memoised
                    # columns are SHARED immutable tuples (review LOW-2),
                    # while the fno_math stub's typed contract declares list
                    # columns (PyO3 itself accepts any sequence). One O(N)
                    # copy per construction — the constructor's own Vec
                    # marshal is already O(N).
                    self._rust = kernel_cls(  # type: ignore[operator]
                        list(open_p),
                        list(high_p),
                        list(low_p),
                        list(close_p),
                        list(forced),
                        list(session_days),
                        list(self._exec_map.arm_source),
                        self._bar_bits,
                    )
                    self._rust_is_wide = wide
        if self._rust is not None and not _kernel_fill_law_current(self._rust):
            # Stale-wheel demotion (DEC-FILL-WORST-CASE-ONLY-001): the built
            # kernel still exposes the retired mode flags, so its flagless
            # call would price the retired PRECISE defaults — the WHOLE
            # object serves the bit-exact worst-case Python oracle instead
            # (wheel state can never change results). Probed ONCE here; the
            # u128 promotion shares the wheel, so one verdict covers it too
            # (the promotion is unreachable off the oracle arm).
            self._rust = None
            reason = "fill_law_kernel_stale"
        if self._rust is None:
            # Structurally: ``self._rust is None`` here IFF a reason was set
            # above (every branch that leaves it None assigns one) — the
            # narrowing guard keeps that visible to mypy --strict without an
            # ignore. Strict mode refuses BEFORE the WARNING is emitted.
            if reason is not None:
                _refuse_if_strict(
                    reason,
                    n_bars=len(self._decision_bars),
                    n_exec_bars=len(self._exec_bars),
                    tf_minutes=tf_minutes,
                )
            # The width reason additionally NAMES the offending word (value +
            # bit) — spec PR-1, DEC-SWEEP-RUST-WIRE-001 part 1; other reasons
            # carry no width offender.
            _LOG.warning(
                "rust_trade_sim.fallback",
                reason=reason,
                n_bars=len(self._decision_bars),
                n_exec_bars=len(self._exec_bars),
                tf_minutes=tf_minutes,
                **(
                    _width_offender_fields(self._bar_bits)
                    if reason == "bar_bits_beyond_u128"
                    else {}
                ),
            )

    def _derive_kernel_inputs(
        self,
    ) -> tuple[
        tuple[int, ...],
        tuple[int, ...],
        tuple[int, ...],
        tuple[int, ...],
        tuple[bool, ...],
        tuple[int, ...],
    ]:
        """Derive the resident kernel's per-1m-bar input columns from the EXECUTION stream.

        Returns ``(open_p, high_p, low_p, close_p, forced, session_days)`` —
        the SAME canonical Python seams both kernel widths are fed from
        (called once at construction, and once more only if a >64-bit mask
        batch later promotes the window to the u128 sibling kernel; a pure
        function of the immutable ``_exec_bars``, so both derivations are
        identical by construction). The seventh kernel input —
        ``arm_source`` — lives on the instance's
        :class:`~brutex.engine.exec_grid.ExecGridMap` (built once at
        construction; design §1.2), so the memo stays a pure function of ONE
        stream.

        The forced-exit flags are SESSION-AWARE at 1-MINUTE granularity —
        byte-identical to :meth:`brutex.engine.trade_sim.TradeSimWalker.push_1m`
        (``is_forced_exit_bar(t, 1, boundary)``) — so the boundary is the 1m
        bar session's own close − 10min (15:20 IST on a regular day, the
        early close − 10min on a half-day / Muhurat session), resolved per
        IST date via the COMMON strict ``get_session_checked`` →
        :func:`~brutex.calendars.nse.forced_exit_seconds_for_session`; an
        in-coverage date with no session falls back to 15:20
        (DEC-SESSION-FORCED-EXIT-001), while a bar dated on a weekday OUTSIDE
        the published holiday span raises ``HolidayDataUnavailableError`` —
        LOUDLY, in lock-step with the streaming walker (the parity contract
        holds: both paths refuse the same uncoverable date instead of
        silently fabricating a boundary). The boundary is memoised by the
        LAST IST date only (a 1-slot cache, O(1) per bar) — a chronological
        feed re-resolves once per new day, exactly as the streaming walker
        does, so the precomputed Rust-kernel flags and the Python-fallback
        exits never diverge.

        ``session_days`` is the per-1m-bar SESSION identity — the bar's own
        IST calendar-date ordinal, the SAME date key the forced-exit
        resolution uses — threaded to the kernel so an open position never
        survives a session-date change (the ``session_gap`` close,
        DEC-TRADESIM-OPEN-SESSION-GUARD-001 on the 1m stream — the same
        ordinals feed the kernel's open-position guard). Bar ``i``'s session
        derives from bar ``i``'s own timestamp — knowable at bar ``i`` (P-06:
        no look-ahead).

        Observability (DEC-TRADESIM-OPEN-SESSION-GUARD-001): the forced/
        session loop ends with ONE mask-independent capped sweep over its
        derived columns — :func:`_warn_session_tail_holes` — naming every
        non-final session whose stored 1m bars never reach a forced-exit bar
        (the exact shape the guard will fire on), so a session-tail data
        hole is loud once per derivation, never once per mask. Under the
        memo a memo HIT RE-EMITS the sweep from the memoised
        ``forced``/``session_days`` columns — a pure function of them, so
        the warn frequency and payloads match the non-memo derivation's
        per-call emission 1:1 (main-parity: the u128 promotion's second
        call warns exactly as its second full derivation used to).

        Since DEC-STAGE1A-RUST-BATCH-001 (the W2 rider): the tf-independent
        columns come from the injected ``precomputed_columns`` when a kernel
        holder supplied them (byte-identical by construction — the SAME
        :func:`derive_price_columns` over the SAME ``exec_bars_1m`` produced
        them, golden-pinned) else from one local
        :func:`derive_price_columns` call, and the assembled 6-tuple is
        MEMOISED on the instance (pure — the G-4 law), so the u128
        promotion's second derivation is a dict-free O(1) handoff. Every
        column in the memo is an immutable ``tuple`` — the price columns are
        SHARED across every sim built from one kernel, so no consumer can
        corrupt a sibling in place (review LOW-2 hardening). The forced/
        session loop (with the tail-hole sweep at its end, per the
        session-guard order above) runs BEFORE the local price-column
        derivation — the pre-rider statement order — so a multi-pathology
        window (an out-of-coverage weekday PLUS an over-precision price, both
        constructible from validated ``CandleBar``\\s) still raises the class
        contract's ``HolidayDataUnavailableError``, never
        ``decimal.InvalidOperation`` (refuter RF2 C-1; regression-pinned) —
        and the tail-hole WARNs land before a price-derivation raise, exactly
        as the non-memo derivation ordered them.
        """
        if self._kernel_inputs is not None:
            # Memo HIT: re-emit the session-tail-hole sweep from the memoised
            # forced/session_days columns (docstring — main-parity warn
            # frequency: every call warns, exactly as every non-memo
            # derivation did).
            _warn_session_tail_holes(self._kernel_inputs[4], self._kernel_inputs[5])
            return self._kernel_inputs
        forced: list[bool] = []
        session_days: list[int] = []
        cache_date: date | None = None
        cache_seconds: int | None = None
        for bar in self._exec_bars:
            ts_ist = bar.timestamp.astimezone(IST)
            bar_day = ts_ist.date()
            if bar_day != cache_date:
                session = get_session_checked(bar_day)
                cache_date = bar_day
                cache_seconds = (
                    None if session is None else forced_exit_seconds_for_session(session)
                )
            forced.append(is_forced_exit_bar(ts_ist.time(), 1, cache_seconds))
            session_days.append(bar_day.toordinal())
        _warn_session_tail_holes(forced, session_days)
        columns = self._precomputed_columns
        if columns is None:
            columns = derive_price_columns(self._exec_bars)
        self._kernel_inputs = (
            columns.open_paisa,
            columns.high_paisa,
            columns.low_paisa,
            columns.close_paisa,
            tuple(forced),
            columns.session_days,
        )
        return self._kernel_inputs

    def _promote_wide_kernel(self) -> object | None:
        """Build (at most once) the u128 sibling kernel for >64-bit mask batches.

        DEC-TRADE-SIM-U128-KERNEL-001: a mask in ``(U64_MASK, _U128_MASK]``
        over a u64-resident window promotes the WHOLE batch to
        ``fno_math.ResidentSweepSimU128`` — built lazily HERE from the same
        :meth:`_derive_kernel_inputs` columns + the instance's
        ``ExecGridMap.arm_source`` (a memo handoff since the
        DEC-STAGE1A-RUST-BATCH-001 G-4 memo; the hit still pays the
        ``O(N_1m)`` session-tail-hole RE-EMIT sweep for main-parity warn
        frequency, DEC-TRADESIM-OPEN-SESSION-GUARD-001 — plus the FFI copy,
        paid once per instance, announced with ONE
        ``rust_trade_sim.wide_kernel_promoted`` INFO) and memoised, so the
        chunked sweep's subsequent wide batches are pure kernel calls.
        Returns ``None`` when the installed wheel predates the class OR its
        class predates the exec-grid constructor
        (:func:`_kernel_exec_grid_current` — an old-constructor sibling
        cannot take ``arm_source``); either verdict is memoised via
        ``_wide_unavailable`` (no per-chunk re-import attempts) and the
        caller falls back to the oracle with the warn-once
        ``wheel_missing_u128`` reason. Reachable only from a monkeypatched /
        hand-imported stale module: a genuinely stale installed ``.so``
        fails at ``import fno_math`` (the shim's import-time symbol
        handshake).
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
        if not _kernel_exec_grid_current(kernel_cls):
            self._wide_unavailable = True
            return None
        open_p, high_p, low_p, close_p, forced, session_days = self._derive_kernel_inputs()
        # Fresh list copies at the FFI edge — the memo stays immutable tuples
        # (review LOW-2; the construction-site comment has the full rationale).
        self._rust_wide = kernel_cls(
            list(open_p),
            list(high_p),
            list(low_p),
            list(close_p),
            list(forced),
            list(session_days),
            list(self._exec_map.arm_source),
            self._bar_bits,
        )
        _LOG.info(
            "rust_trade_sim.wide_kernel_promoted",
            n_bars=len(self._decision_bars),
            n_exec_bars=len(self._exec_bars),
            tf_minutes=self._tf_minutes,
        )
        return self._rust_wide

    @property
    def n_bars(self) -> int:
        """Number of DECISION bars in the resident window (the signal domain)."""
        return len(self._decision_bars)

    @property
    def n_exec_bars(self) -> int:
        """Number of 1-minute EXECUTION bars in the resident window (the walk domain)."""
        return len(self._exec_bars)

    @property
    def exec_bars(self) -> tuple[CandleBar, ...]:
        """The resident 1-minute EXECUTION stream (the fill/walk domain).

        PUBLIC so the sweep spine's metrics tail validates each mask's exit
        1m indices against the SAME stream the kernel walked
        (DEC-FILL-EXEC-GRID-1M-001 §3.8) — never a second copy that could
        drift from the resident window.
        """
        return self._exec_bars

    @property
    def exec_map(self) -> ExecGridMap:
        """The window's :class:`~brutex.engine.exec_grid.ExecGridMap`.

        PUBLIC so downstream consumers (the metrics tail's 1m→decision index
        translation via ``decision_of``, design §3.8, and the chunk-failure
        re-attribution's oracle replay) read the SAME mapping this window
        simulates with — never a second derivation that could drift.
        """
        return self._exec_map

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
    ) -> list[list[ClosedTrade]]:
        """Simulate a BATCH of masks over the resident window → per-mask trades.

        Thin delegate over :meth:`simulate_masks_with_exit_indices` (itself
        a thin delegate over :meth:`simulate_masks_with_exit_counts` — the
        ONE batch spine, DEC-METRICS-LOSSRATE-FIRSTCLASS-001 Stage B over
        the DEC-SWEEP-EXIT-INDEX-FASTPATH-001 shape) that drops the
        exit-index halves — byte-identical trades, same error contract,
        same fallback/warn behaviour. See the spine for the full contract.
        """
        return [
            trades
            for trades, _ in self.simulate_masks_with_exit_indices(
                masks,
                direction=direction,
                stop_loss_paisa=stop_loss_paisa,
                take_profit_paisa=take_profit_paisa,
            )
        ]

    def simulate_masks_with_exit_indices(
        self,
        masks: Sequence[int],
        *,
        direction: Side,
        stop_loss_paisa: int | None = None,
        take_profit_paisa: int | None = None,
    ) -> list[tuple[list[ClosedTrade], list[int] | None]]:
        """Simulate a BATCH of masks → per-mask ``(trades, exit_indices)`` pairs.

        Thin delegate over :meth:`simulate_masks_with_exit_counts` (ONE batch
        spine — DEC-METRICS-LOSSRATE-FIRSTCLASS-001 Stage B, mirroring how
        :meth:`simulate_masks` already delegated here) that drops the
        exit-reason counters — byte-identical trades + indices, same error
        contract, same fallback/warn behaviour. See that method for the full
        contract.
        """
        return [
            (trades, exit_indices)
            for trades, exit_indices, _counts in self.simulate_masks_with_exit_counts(
                masks,
                direction=direction,
                stop_loss_paisa=stop_loss_paisa,
                take_profit_paisa=take_profit_paisa,
            )
        ]

    def simulate_masks_with_exit_counts(
        self,
        masks: Sequence[int],
        *,
        direction: Side,
        stop_loss_paisa: int | None = None,
        take_profit_paisa: int | None = None,
    ) -> list[tuple[list[ClosedTrade], list[int] | None, tuple[int, ...]]]:
        """Simulate a BATCH of masks → ``(trades, exit_indices, exit_counts)`` triples.

        ``out[j]`` is the triple for ``masks[j]`` (signal-only spot; the
        worst-case adverse-max fill law on the 1-MINUTE execution grid — the
        ONLY law, DEC-FILL-WORST-CASE-ONLY-001 + DEC-FILL-EXEC-GRID-1M-001 —
        next-1m-bar entry at the adverse extreme, level-clamped SL/TP per 1m
        bar, forced session-close exit at the adverse extreme). The trades are
        bit-identical to ``[simulate_trades(decision_bars,
        mask_to_entry_signals(bar_bits, m), exec_bars_1m=..., ...) for m in
        masks]`` — the Rust path and the fallback walk the same hardcoded law
        and direction. Empty ``masks`` → ``[]``.

        ``exit_counts`` (DEC-METRICS-LOSSRATE-FIRSTCLASS-001 Stage B) is the
        mask's per-reason closed-trade counters in
        :data:`~brutex.engine.trade_sim.EXIT_REASONS` order — on the Rust arm
        the kernel's own accumulation, on the fallback arm the walker's
        :attr:`~brutex.engine.trade_sim.TradeSimWalker.exit_reason_counts`;
        both are parity-pinned bit-equal, and ``sum(exit_counts) ==
        len(trades)`` is VALIDATED per mask on the kernel arm (a mismatch is
        a kernel/marshalling bug and raises ``ValueError`` — inside the
        sweep's chunk try, so it rides the existing re-attribution →
        tuple-skip ladder, never a silently wrong ``sl_hit_rate``). A STALE
        wheel predating the kernel's counted twin serves the WHOLE call
        through the bit-exact Python oracle instead (warn-once reason
        ``wheel_missing_exit_counts`` — trades/counts identical, only the
        engine path changes; reachable only from a monkeypatched /
        hand-imported stale module, the ``wheel_missing_u128`` envelope).

        ``exit_indices`` (DEC-SWEEP-EXIT-INDEX-FASTPATH-001) is each trade's
        exit bar index in the resident 1-MINUTE EXECUTION window (design
        §2.6 — a 1m index, translatable to its owning decision bar via
        ``exec_map.decision_of``), emitted only under a DOUBLE certificate —
        the EXECUTION stream's timestamps strictly increase (checked once at
        construction) AND the mask's stream passes :func:`_monotonic` — else
        ``None``, and the metrics tail serves that mask through the
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

        Returns
        -------
        list[tuple[list[ClosedTrade], list[int] | None, tuple[int, ...]]]
            One ``(trades, exit_indices, exit_counts)`` triple per mask,
            positionally aligned with ``masks``.

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
            # Stale-wheel probe (DEC-METRICS-LOSSRATE-FIRSTCLASS-001 Stage B):
            # a hand-imported wheel predating the counted twin lacks the
            # method — serve the WHOLE call through the bit-exact Python
            # oracle (trades + counts identical, only the engine path
            # changes), warn-once like every other sticky per-call reason.
            if (
                call_reason is None
                and rust is not None
                and getattr(rust, "simulate_masks_with_exit_counts", None) is None
            ):
                call_reason = "wheel_missing_exit_counts"
            if call_reason is not None:
                # Strict mode refuses BEFORE the warn-once memo write, so a
                # repeat of an already-warned sticky reason cannot degrade
                # silently past the early return below.
                _refuse_if_strict(call_reason, n_masks=len(masks))
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
            # Pure-Python fallback: the exact dual-feed oracle (the SAME
            # walker simulate_trades wraps), one mask at a time, threading
            # the instance's OWN ExecGridMap (never a per-mask re-derivation).
            # The walker's exit indices are TRUE 1m indices and monotonic by
            # construction; they are emitted under the same window
            # certificate as the kernel's. The window's OWN stamper rides
            # into every walker, so a fallback-served window stamps
            # identically to the kernel-served one (never a wheel-state
            # divergence — DEC-VIX-STAMP-WIRE-001). The exit-reason counters
            # come from the walker's OWN accumulation
            # (:attr:`~brutex.engine.trade_sim.TradeSimWalker.exit_reason_counts`
            # — the same source the parity gates pin the kernel against).
            out: list[tuple[list[ClosedTrade], list[int] | None, tuple[int, ...]]] = []
            for mask in masks:
                walker = TradeSimWalker(
                    direction=direction,
                    tf_minutes=self._tf_minutes,
                    stop_loss_paisa=stop_loss_paisa,
                    take_profit_paisa=take_profit_paisa,
                    reference_stamper=self._reference_stamper,
                )
                trades, exit_indices = walker.run_with_exit_indices(
                    self._decision_bars,
                    self._exec_bars,
                    mask_to_entry_signals(self._bar_bits, mask),
                    exec_map=self._exec_map,
                )
                out.append(
                    (
                        trades,
                        exit_indices if self._ts_certified else None,
                        walker.exit_reason_counts,
                    )
                )
            return out

        # Rust path — the flagless worst-case-only COUNTED kernel call
        # (DEC-FILL-WORST-CASE-ONLY-001: the retired mode flags are DELETED
        # from the FFI; a stale flag-carrying wheel was demoted to the oracle
        # at construction, so this call is provably the current kernel).
        raw = rust.simulate_masks_with_exit_counts(  # type: ignore[attr-defined]
            list(masks),
            direction is Side.BUY,
            stop_loss_paisa,
            take_profit_paisa,
        )
        # HOISTED converter input (DEC-VIX-STAMP-WIRE-001): the stamper is read
        # ONCE per batch call — not once per trade — and threaded into the
        # per-record converter, which post-stamps each Rust trade Python-side.
        stamper = self._reference_stamper
        triples: list[tuple[list[ClosedTrade], list[int] | None, tuple[int, ...]]] = []
        for mask_records, exit_counts in raw:
            mask_trades: list[ClosedTrade] = []
            mask_exit_indices: list[int] = []
            for record in mask_records:
                trade, exit_idx = self._to_trade_and_exit(record, direction, stamper)
                mask_trades.append(trade)
                mask_exit_indices.append(exit_idx)
            counts = tuple(exit_counts)
            if sum(counts) != len(mask_trades):
                # A counter/trade mismatch is a kernel or marshalling bug —
                # refuse it (docstring: inside the sweep's chunk try, so it
                # rides the re-attribution → tuple-skip ladder), never a
                # silently wrong sl_hit_rate downstream.
                raise ValueError(
                    "ResidentTradeSim.simulate_masks_with_exit_counts: kernel "
                    f"exit_counts sum {sum(counts)} != trade count {len(mask_trades)}"
                )
            if not self._ts_certified:
                # Malformed (duplicate/reordered timestamp) window: withhold
                # the indices — the dict path is the historical behaviour.
                triples.append((mask_trades, None, counts))
            elif not _monotonic(mask_exit_indices):
                # A non-monotonic stream on a CERTIFIED window is a kernel
                # ordering regression — refuse the indices LOUDLY (once per
                # instance) and let the dict path serve the mask.
                #
                # Deliberately NOT wired to strict mode
                # (DEC-RUST-STRICT-NO-FALLBACK-001): this is NOT an arm
                # switch. RUST still serves the trades — only the O(trades)
                # index FAST PATH is withheld, and the bit-identical
                # timestamp-dict path in the metrics tail produces the SAME
                # values. There is no Python fall-back here to refuse. Its
                # distinct event name (``exit_indices_uncertified``, not
                # ``call_fallback``) records exactly that.
                if "exit_indices_not_monotonic" not in self._call_fallback_warned:
                    self._call_fallback_warned.add("exit_indices_not_monotonic")
                    _LOG.warning(
                        "rust_trade_sim.exit_indices_uncertified",
                        reason="exit_indices_not_monotonic",
                        n_trades=len(mask_trades),
                    )
                triples.append((mask_trades, None, counts))
            else:
                triples.append((mask_trades, mask_exit_indices, counts))
        return triples

    def _to_trade_and_exit(
        self,
        record: tuple[int, int, int, int, int, int],
        direction: Side,
        stamper: TradeReferenceStamper | None,
    ) -> tuple[ClosedTrade, int]:
        """Map one Rust trade tuple → ``(ClosedTrade, exit_idx)`` (1m idx→bar timestamps).

        The kernel record's ``entry_idx`` / ``exit_idx`` / ``holding_bars``
        are 1-MINUTE EXECUTION-stream quantities (design §2.6) — the
        timestamps resolve against ``exec_bars_1m``, and the exit index is
        returned alongside the trade so the metrics tail can skip its
        per-mask ``exit_ts``→index dict build
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
        entry_ts = self._exec_bars[entry_idx].timestamp
        exit_ts = self._exec_bars[exit_idx].timestamp
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
