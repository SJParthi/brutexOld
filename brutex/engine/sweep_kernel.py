"""Resident Stage-2 sweep kernel — bar window reused across masks (DEC-SWEEP-RESIDENT-KERNEL-001).

The grid orchestrator (:func:`brutex.engine.sweep_orchestrator.run_sweep_grid`)
walks the outer product ``instrument × timeframe × direction × K × stop-loss ×
take-profit × proximity``. The Stage-1A ``bar_bits`` truth-matrix
(:func:`brutex.engine.stage1a.build_bar_bits`) — the expensive per-bar full-vocabulary
evaluation — depends ONLY on ``(bars, proximity, prior_n)``, so it is **identical**
for every ``(direction, K, sl, tp)`` tuple that shares one ``(instrument, tf,
proximity)``. The single-slice runner rebuilds it from scratch on every tuple and
re-marshals it Python→Rust for each Stage-1B count.

This module removes both redundancies:

* :class:`ResidentSweepKernel` builds ``bar_bits`` ONCE for a single
  ``(instrument, tf, proximity, prior_n)`` and holds a resident Rust
  ``Vec<u64>`` buffer (:class:`~brutex.engine.combo_mask.ResidentBarBuffer`), so a
  Stage-1B count over many successive mask batches marshals only the (small)
  masks — never the (large) bar window — across the FFI boundary.
* :class:`ResidentKernelCache` is the bounded, LRU cache the grid threads through
  its tuples so that ``bar_bits`` is built (and the bar window marshalled) ONCE
  per distinct ``(instrument, tf, proximity, prior_n)`` rather than once per tuple.

## Bit-exactness (Golden Rule #4)

:meth:`ResidentSweepKernel.count_hits` returns the same per-mask SUPERSET counts
as the free :func:`brutex.engine.combo_mask.count_supermasks` for the same
``(bar_bits, masks)`` — both delegate to the single ``count_one`` Rust kernel (or
the byte-identical numpy fallback). Reusing a kernel therefore produces a
byte-identical sweep result + audit chain; it changes only *when* work is done,
never *what* the answer is.

## Honest envelope (Golden Rule #10) — NOT O(1)-total

* :class:`ResidentSweepKernel` construction is ``O(N_bars)`` — the one-time
  Stage-1A build + resident copy, paid ONCE here.
* :meth:`ResidentSweepKernel.count_hits` is ``O(N_masks · N_bars)`` compute +
  ``O(N_masks)`` marshalling per call. The ``O(N_bars)`` marshalling the free
  function repeats every call is paid once at construction — that elimination,
  repeated across many batches and many sharing tuples, is the entire win, which
  grows with the reuse count.
* :class:`ResidentKernelCache` holds at most ``capacity`` kernels →
  ``O(capacity · N_bars)`` bounded memory, NOT O(1) total. Claiming O(1) total
  time/space for a whole grid sweep (inherently O(Σ Nᵢ) work + O(Σ finalists)
  rows) is a physical impossibility and is FORBIDDEN.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Final

from brutex.engine.combo_mask import ResidentBarBuffer
from brutex.engine.rust_trade_sim import PriceColumns, derive_price_columns
from brutex.engine.stage1a import build_bar_bits
from brutex.strategies.types import FibAnchorKind, PriceSource

if TYPE_CHECKING:  # pragma: no cover - import only for static typing
    # Annotation-only — keep CandleBar / ProximityParams out of this module's
    # runtime import graph so the strict per-module `--cov=brutex.engine` gate is
    # unaffected (mirrors the convention in sweep_run.py / sweep_orchestrator.py).
    from brutex.data.models import CandleBar
    from brutex.vocabulary import ProximityParams

__all__ = [
    "DEFAULT_KERNEL_CACHE_CAPACITY",
    "ResidentKernelCache",
    "ResidentSweepKernel",
]

#: Default bound on the number of distinct bar windows a :class:`ResidentKernelCache`
#: holds resident at once. One per distinct ``(instrument, tf, proximity, prior_n)``;
#: the grid's proximity fan-out for a single ``(instrument, tf)`` is the natural
#: working set, so this default keeps a full fan-out resident while bounding memory.
DEFAULT_KERNEL_CACHE_CAPACITY: Final[int] = 8


class ResidentSweepKernel:
    """Stage-1A ``bar_bits`` + resident bar buffer for ONE ``(instrument, tf, proximity, prior_n)``.

    Build it once, then reuse it across every ``(direction, K, sl, tp)`` sub-sweep
    and every mask batch that shares this bar window: the Stage-1A full-vocabulary
    evaluation and the Python→Rust marshal of the bar window are each paid once,
    not once per tuple / once per batch.

    Parameters
    ----------
    bars
        Chronologically ordered bars for one ``(instrument, interval)``
        (non-empty).
    proximity
        Proximity tolerances for the ``near_*`` conditions, or ``None`` for the
        Stage-1A default (10 bps).
    prior_n
        Prior-bar pattern look-back ∈ {1, 2, 3, 4}.
    fib_anchor
        The swept Fibonacci-anchor family
        (:class:`~brutex.strategies.types.FibAnchorKind`) the ``bar_bits`` were
        built against (DEC-NIFTY-FIB-AXIS-001). Recorded so the sweep can fail-fast
        when a kernel is reused under a DIFFERENT anchor (the ``bar_bits`` matrix is
        anchor-specific — R-1). Defaults to :attr:`FibAnchorKind.PREV_DAY`.
    vwap_source, ema_source
        The swept VWAP / EMA price-source axis
        (:class:`~brutex.strategies.types.PriceSource`) the ``bar_bits`` were built
        against (DEC-NIFTY-PRICE-SOURCE-AXIS-001). Recorded so the sweep can
        fail-fast when a kernel is reused under a DIFFERENT source (bits 52-53 /
        0-5 are source-specific). Default ``(HLC3, CLOSE)``.
    roll_schedule
        ADR-0012 futures roll schedule (DEC-STAGE1A-ROLL-RESET-001) the
        ``bar_bits`` were built against — recorded for the schedule-mismatch
        fail-fast (the roll twin of the anchor guard). ``None`` (the default)
        is the byte-identical spot path.
    apply_1day_mask
        D12 honesty flag (see :func:`brutex.engine.stage1a.build_bar_bits`).
        Default False (byte-identical). Recorded so the sweep can fail-fast
        when a kernel is reused under a DIFFERENT flag (the D12 twin of the
        anchor/schedule guards — review round-1 finding 5).

    Raises
    ------
    ValueError
        If ``bars`` is empty.
    """

    __slots__ = (
        "_apply_1day_mask",
        "_bar_bits",
        "_bars",
        "_ema_source",
        "_exec_bars_1m",
        "_exec_columns",
        "_fib_anchor",
        "_resident",
        "_roll_schedule",
        "_vwap_source",
    )

    def __init__(
        self,
        bars: Sequence[CandleBar],
        *,
        proximity: ProximityParams | None = None,
        prior_n: int = 3,
        fib_anchor: FibAnchorKind = FibAnchorKind.PREV_DAY,
        vwap_source: PriceSource = PriceSource.HLC3,
        ema_source: PriceSource = PriceSource.CLOSE,
        roll_schedule: tuple[int, ...] | None = None,
        apply_1day_mask: bool = False,
        exec_bars_1m: Sequence[CandleBar] | None = None,
    ) -> None:
        if not bars:
            raise ValueError("ResidentSweepKernel: bars must be non-empty")
        self._bars: tuple[CandleBar, ...] = tuple(bars)
        self._exec_bars_1m: tuple[CandleBar, ...] | None = (
            None if exec_bars_1m is None else tuple(exec_bars_1m)
        )
        self._fib_anchor: FibAnchorKind = fib_anchor
        self._vwap_source: PriceSource = vwap_source
        self._ema_source: PriceSource = ema_source
        self._roll_schedule: tuple[int, ...] | None = roll_schedule
        self._apply_1day_mask: bool = apply_1day_mask
        self._bar_bits: list[int] = build_bar_bits(
            self._bars,
            proximity,
            prior_n,
            fib_anchor,
            vwap_source=vwap_source,
            ema_source=ema_source,
            roll_schedule=roll_schedule,
            apply_1day_mask=apply_1day_mask,
        )
        self._resident: ResidentBarBuffer = ResidentBarBuffer(self._bar_bits)
        # Lazily-derived trade-sim EXECUTION columns (the W2 rider of
        # DEC-STAGE1A-RUST-BATCH-001, rebased onto the 1m execution stream by
        # DEC-FILL-EXEC-GRID-1M-001) — see :attr:`exec_columns`.
        self._exec_columns: PriceColumns | None = None

    @property
    def bars(self) -> tuple[CandleBar, ...]:
        """The bars this kernel was built from (the trade-sim feed)."""
        return self._bars

    @property
    def fib_anchor(self) -> FibAnchorKind:
        """The Fib anchor this kernel's ``bar_bits`` were built against (R-1 guard)."""
        return self._fib_anchor

    @property
    def vwap_source(self) -> PriceSource:
        """The VWAP price-source this kernel's ``bar_bits`` (bits 52-53) were built for."""
        return self._vwap_source

    @property
    def ema_source(self) -> PriceSource:
        """The EMA price-source this kernel's ``bar_bits`` (bits 0-5) were built for."""
        return self._ema_source

    @property
    def roll_schedule(self) -> tuple[int, ...] | None:
        """The roll schedule this kernel's ``bar_bits`` were built against (roll guard)."""
        return self._roll_schedule

    @property
    def apply_1day_mask(self) -> bool:
        """The D12 flag this kernel's ``bar_bits`` were built against (D12 guard)."""
        return self._apply_1day_mask

    @property
    def bar_bits(self) -> list[int]:
        """The pre-built Stage-1A per-bar u128 truth-vectors (reused, never rebuilt)."""
        return self._bar_bits

    @property
    def exec_bars_1m(self) -> tuple[CandleBar, ...] | None:
        """The 1-minute EXECUTION stream this kernel was built with (or ``None``).

        DEC-FILL-EXEC-GRID-1M-001: carried so the sweep spine reuses ONE
        stream + column set per ``(instrument, tf, proximity, prior_n)``
        coordinate; ``None`` (a legacy caller that supplied no exec stream)
        simply means the trade sim derives its own columns locally.
        """
        return self._exec_bars_1m

    @property
    def exec_columns(self) -> PriceColumns | None:
        """The trade-sim EXECUTION-stream input columns for this window.

        Derived LAZILY via ONE
        :func:`~brutex.engine.rust_trade_sim.derive_price_columns` call per
        kernel lifetime over ``exec_bars_1m`` (the W2 rider of
        DEC-STAGE1A-RUST-BATCH-001, rebased onto the EXECUTION stream by
        DEC-FILL-EXEC-GRID-1M-001) and memoised, so every ``(direction, K,
        sl, tp)`` tuple reusing this kernel hands its
        :class:`~brutex.engine.rust_trade_sim.ResidentTradeSim` pre-derived
        1m columns instead of re-paying the 4×-Decimal-snap pass —
        byte-identical by construction (the SAME seam derives both).
        ``None`` iff the kernel was built without an execution stream.
        """
        if self._exec_bars_1m is None:
            return None
        if self._exec_columns is None:
            self._exec_columns = derive_price_columns(self._exec_bars_1m)
        return self._exec_columns

    @property
    def n_bars(self) -> int:
        """Number of bars in the resident window (``== len(bar_bits) == len(bars)``)."""
        return len(self._bar_bits)

    def count_hits(self, masks: Sequence[int]) -> list[int]:
        """Per-mask SUPERSET hit count over the resident bars — no bar re-marshal.

        For each ``mask`` in ``masks`` return how many resident bars satisfy
        ``(bar & mask) == mask``; ``out[j]`` aligns with ``masks[j]``. Bit-identical
        to :func:`brutex.engine.combo_mask.count_supermasks` for the same
        ``(bar_bits, masks)`` (both delegate to the one shared Rust ``count_one``
        kernel / numpy fallback) — only the bar-window marshalling differs, so a
        sweep that reuses this kernel is byte-identical (Golden Rule #4). Empty
        ``masks`` → ``[]``; ``mask == 0`` matches every bar.

        Parameters
        ----------
        masks
            The strategy masks to count (e.g. the Stage-1B survivor masks).

        Returns
        -------
        list[int]
            One hit count per mask, positionally aligned with ``masks``.
        """
        return self._resident.count_supermasks(masks)

    def prefilter(self, masks: Sequence[int], min_hits: int) -> list[int]:
        """Stage-1B coarse pre-filter via the resident buffer → surviving masks.

        Keeps only the masks that fire (are a superset) on at least ``min_hits``
        bars — counted by :meth:`count_hits` (resident, no bar re-marshal). A mask
        firing on fewer bars than the trade-count gate can never rank, so dropping
        it changes no ranked leaderboard (DEC-STAGE1B-WIRE-001). ``min_hits <= 0``
        keeps every mask (the pre-filter disabled). Input order is preserved.

        Parameters
        ----------
        masks
            The candidate masks.
        min_hits
            The minimum superset-hit count to survive (``<= 0`` disables).

        Returns
        -------
        list[int]
            The surviving masks, in input order.
        """
        mask_list = list(masks)
        if min_hits <= 0 or not mask_list:
            return mask_list
        hits = self.count_hits(mask_list)
        return [mask for mask, hit in zip(mask_list, hits, strict=True) if hit >= min_hits]


class ResidentKernelCache:
    """Bounded LRU cache of :class:`ResidentSweepKernel` keyed by bar-window identity.

    Threaded through :func:`brutex.engine.sweep_orchestrator.run_sweep_grid` so the
    Stage-1A build + bar-window marshal are paid ONCE per distinct ``(instrument,
    tf, proximity, prior_n)`` instead of once per grid tuple. The grid iterates
    ``instrument → timeframe → direction → K → sl → tp → proximity`` with a
    grid-global ``prior_n``, so for a fixed ``(instrument, tf)`` the same set of
    proximities recurs across every ``(direction, K, sl, tp)`` — a cache sized to
    the proximity fan-out gets a hit on every recurrence.

    Correctness contract: the caller MUST supply the same ``bars`` for a given
    ``(instrument, tf)`` on every call (the grid's ``bars_for`` is deterministic),
    so a cache hit's resident ``bar_bits`` always matches the freshly-loaded bars.
    Reusing a kernel is bit-identical to rebuilding one (Golden Rule #4).

    Parameters
    ----------
    capacity
        Maximum number of kernels held resident at once (``>= 1``). LRU eviction
        once exceeded — bounded ``O(capacity · N_bars)`` memory (Golden Rule #10:
        NOT O(1) total).
    builder
        Injectable kernel factory (defaults to :class:`ResidentSweepKernel`) — the
        seam tests use to assert build-once / reuse without a real Stage-1A build.

    Raises
    ------
    ValueError
        If ``capacity < 1``.
    """

    __slots__ = ("_builder", "_builds", "_cache", "_capacity", "_hits")

    def __init__(
        self,
        *,
        capacity: int = DEFAULT_KERNEL_CACHE_CAPACITY,
        builder: Callable[..., ResidentSweepKernel] = ResidentSweepKernel,
    ) -> None:
        if capacity < 1:
            raise ValueError(f"ResidentKernelCache: capacity must be >= 1, got {capacity}")
        self._capacity: int = capacity
        self._builder: Callable[..., ResidentSweepKernel] = builder
        self._cache: OrderedDict[
            tuple[
                str,
                str,
                ProximityParams | None,
                int,
                FibAnchorKind,
                PriceSource,
                PriceSource,
                tuple[int, ...] | None,
            ],
            ResidentSweepKernel,
        ] = OrderedDict()
        self._builds: int = 0
        self._hits: int = 0

    @property
    def builds(self) -> int:
        """How many kernels this cache has built (cache misses) — the work saved metric."""
        return self._builds

    @property
    def hits(self) -> int:
        """How many lookups were served from a resident kernel (cache hits)."""
        return self._hits

    def __len__(self) -> int:
        """Number of kernels currently resident (``<= capacity``)."""
        return len(self._cache)

    def get(
        self,
        bars: Sequence[CandleBar],
        *,
        instrument: str,
        tf_label: str,
        proximity: ProximityParams | None,
        prior_n: int,
        fib_anchor: FibAnchorKind = FibAnchorKind.PREV_DAY,
        vwap_source: PriceSource = PriceSource.HLC3,
        ema_source: PriceSource = PriceSource.CLOSE,
        roll_schedule: tuple[int, ...] | None = None,
        apply_1day_mask: bool = False,
        exec_bars_1m: Sequence[CandleBar] | None = None,
    ) -> ResidentSweepKernel:
        """Return the kernel for ``(instrument, tf_label, proximity, prior_n, fib_anchor, sources, roll_schedule)``, building on miss.

        On a hit the resident kernel is returned (and marked most-recently-used)
        WITHOUT rebuilding ``bar_bits`` or re-marshalling the bar window. On a miss
        a kernel is built from ``bars`` and inserted, evicting the
        least-recently-used kernel if the cache is over ``capacity``.

        Parameters
        ----------
        bars
            The bar feed for ``(instrument, tf_label)`` (used only on a miss).
        instrument, tf_label
            The instrument symbol + timeframe label (the bar-source identity).
        proximity
            The proximity tolerances (or ``None`` for default) — part of the key,
            since ``bar_bits`` depends on it.
        prior_n
            The prior-bar look-back — part of the key, since ``bar_bits`` depends
            on it.
        fib_anchor
            The swept Fib anchor — part of the key, since the Group-4 Fib bits in
            ``bar_bits`` depend on it (DEC-NIFTY-FIB-AXIS-001 R-1): a Near/Next/…
            anchor sharing one ``(instrument, tf, proximity, prior_n)`` must get its
            OWN kernel, never a stale prev-day one. Defaults to
            :attr:`FibAnchorKind.PREV_DAY`.
        vwap_source, ema_source
            The swept VWAP / EMA price-source
            (:class:`~brutex.strategies.types.PriceSource`) — part of the key,
            since ``bar_bits`` bits 52-53 / 0-5 depend on it
            (DEC-NIFTY-PRICE-SOURCE-AXIS-001): a non-default source sharing one
            ``(instrument, tf, proximity, prior_n, fib_anchor)`` must get its OWN
            kernel, never a stale ``(HLC3, CLOSE)`` one. Default ``(HLC3, CLOSE)``.
        roll_schedule
            ADR-0012 futures roll schedule — part of the key
            (belt-and-braces: within one launch the schedule is constant per
            instrument, but a long-lived daemon process re-launching over a
            changed lake must never serve a stale-schedule kernel —
            DEC-STAGE1A-ROLL-RESET-001). ``None`` is the spot path.
        apply_1day_mask
            D12 honesty flag — a pure function of ``(instrument, tf_label)``,
            both already in the key, so it needs no key slot of its own.
        exec_bars_1m
            The instrument's 1-minute EXECUTION stream
            (DEC-FILL-EXEC-GRID-1M-001; used only on a miss) — like ``bars``
            it is a pure function of the key's ``instrument`` under the
            deterministic ``bars_for`` contract, so it needs no key slot.

        Returns
        -------
        ResidentSweepKernel
            The (cached or freshly built) kernel for this bar window.
        """
        key = (
            instrument,
            tf_label,
            proximity,
            prior_n,
            fib_anchor,
            vwap_source,
            ema_source,
            roll_schedule,
        )
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            self._hits += 1
            return cached
        kernel = self._builder(
            bars,
            proximity=proximity,
            prior_n=prior_n,
            fib_anchor=fib_anchor,
            vwap_source=vwap_source,
            ema_source=ema_source,
            roll_schedule=roll_schedule,
            apply_1day_mask=apply_1day_mask,
            exec_bars_1m=exec_bars_1m,
        )
        self._builds += 1
        self._cache[key] = kernel
        if len(self._cache) > self._capacity:
            self._cache.popitem(last=False)
        return kernel
