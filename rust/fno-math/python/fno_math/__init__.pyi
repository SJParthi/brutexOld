"""Type stubs for the `fno_math` Rust extension (DEC-P0-055 / PR-N+2).

Every public symbol re-exported by `__init__.py` is declared here so
that `from fno_math import X` resolves under pyright-strict + mypy-strict
without `# pyright: ignore` shims.

The signatures mirror PyO3 0.28.3's runtime extract behavior (the version
pinned in `rust/fno-math/Cargo.lock:520-521`):
- Rust `u64` ⇆ Python `int`
- Rust `Vec<u64>` ⇆ Python `list[int]`
- Rust `&[u8]` / `PyBytes` ⇆ Python `bytes`

Length-mismatch and overflow conditions raise `ValueError` / `OverflowError`
at runtime; pyright does not encode raised exceptions in stubs, so callers
must consult the docstrings below.
"""

from __future__ import annotations

from collections.abc import Sequence

__all__ = [
    "ResidentBars",
    "ResidentBarsU128",
    "ResidentSweepSim",
    "ResidentSweepSimU128",
    "__version__",
    "add_u64",
    "bitand_count_u64s",
    "bitcount_u64s",
    "blake3_hash",
    "champion_dominates",
    "count_supermask_u64s",
    "count_supermask_u128s",
    "count_supermasks_u64s",
    "count_supermasks_u64s_nthreads",
    "count_supermasks_u128s",
    "decay_trailing_fold",
    "decay_verdict",
    "popcount_bytes",
    "stats_cpcv_pass_count",
]

__version__: str

__build_crate_version__: str
"""Crate version of the INSTALLED compiled kernel (build stamp, not API).

Derived at import from the compiled module's ``CARGO_PKG_VERSION``-baked
``__version__``; read by ``brutex doctor``'s fno_math wheel-freshness stage
(DEC-FNOMATH-WHEEL-FRESHNESS-001). Deliberately NOT in ``__all__``.
"""

_EXPECTED_COMPILED_SYMBOLS: tuple[str, ...]

def _assert_compiled_surface(
    compiled: object,
    expected: Sequence[str] = ...,
) -> None:
    """Raise an actionable ``ImportError`` if ``compiled`` misses an expected symbol."""
    ...

def add_u64(a: int, b: int) -> int:
    """Add two `u64` values.

    Raises `OverflowError` if `a + b >= 2**64`.
    """
    ...

def bitcount_u64s(values: list[int]) -> int:
    """Sum of `popcount(v)` over a list of `u64` values."""
    ...

def bitand_count_u64s(a: list[int], b: list[int]) -> int:
    """Sum of `popcount(a[i] & b[i])` over two equal-length `u64` lists.

    Raises `ValueError` if `len(a) != len(b)`.
    """
    ...

def blake3_hash(data: bytes) -> bytes:
    """Return the 32-byte BLAKE3 digest of `data`."""
    ...

def count_supermask_u64s(bar_bits: list[int], mask: int) -> int:
    """Count bars that are a SUPERSET of a single `mask`.

    Returns the number of `bar` in `bar_bits` with `(bar & mask) == mask`.
    `mask == 0` matches every bar (returns `len(bar_bits)`).
    """
    ...

def count_supermasks_u64s(bar_bits: list[int], masks: list[int]) -> list[int]:
    """Batched per-mask SUPERSET count — the Stage-2 hot kernel.

    Returns a list of length `len(masks)` where `out[j]` is the number of
    `bar` in `bar_bits` with `(bar & masks[j]) == masks[j]`. Positionally
    aligned with `masks`. Empty `masks` → `[]`; empty `bar_bits` → `0` per
    mask.
    """
    ...

def count_supermask_u128s(bar_bits: list[int], mask: int) -> int:
    """Single-mask SUPERSET count over a u128 domain (DEC-ENUM-KERNEL-U128-001).

    The wide twin of `count_supermask_u64s`: counts `bar` in `bar_bits` with
    `(bar & mask) == mask` over native u128 words. A value outside
    `[0, 2**128)` raises `OverflowError` at the PyO3 extraction edge.
    """
    ...

def count_supermasks_u128s(bar_bits: list[int], masks: list[int]) -> list[int]:
    """Batched per-mask SUPERSET count over a u128 domain (DEC-ENUM-KERNEL-U128-001).

    The wide twin of `count_supermasks_u64s` — same contract with `bar_bits`
    / `masks` widened to u128 (positional alignment; empty `masks` → `[]`;
    empty `bar_bits` → `0` per mask; `mask == 0` matches every bar). Routes
    through the SAME width-generic dispatch as the u64 kernel, so the counts
    cannot drift between widths. A value outside `[0, 2**128)` raises
    `OverflowError` at the PyO3 extraction edge.
    """
    ...

def count_supermasks_u64s_nthreads(
    bar_bits: list[int], masks: list[int], n_threads: int
) -> list[int]:
    """BENCH-ONLY: batched SUPERSET count on a SCOPED rayon pool of `n_threads`.

    DEC-RUST-RAYON-SUPERMASK-001 (MIGRATION.md #414). Production uses rayon's
    DEFAULT global pool via `count_supermasks_u64s`; this shim exists only so
    the scaling bench + thread-count-independence parity tests can pin a worker
    count without touching the global pool. The result is bit-identical to
    `count_supermasks_u64s` for any `n_threads >= 1` (order-preserving collect).
    `n_threads == 0` lets rayon choose its default count. Raises `ValueError` if
    the scoped pool fails to build.
    """
    ...

def popcount_bytes(data: bytes) -> int:
    """Return the total set-bit count across a byte buffer."""
    ...

def stats_cpcv_pass_count(
    counts: list[int],
    means: list[float],
    m2s: list[float],
    n_test_groups: int,
    min_sharpe: float,
) -> int:
    """CPCV fold pass-count over per-group moment triples (DEC-METRICS-RUST-STATS-001).

    `counts`/`means`/`m2s` are the per-group `(n, mean, M2)` triples from
    `brutex.metrics.cpcv._group_moments`. Returns how many of the
    `C(len(counts), n_test_groups)` folds have a defined Chan/Pébay-merged
    Sharpe STRICTLY greater than `min_sharpe` — the exact
    `brutex.metrics.cpcv._combine_moments` fold, operation-for-operation
    (claim class STRUCTURAL). Raises `ValueError` on a structurally invalid
    input (length mismatch, `n_groups < 2`, `n_test_groups` out of
    `[1, n_groups - 1]`, any `count < 1`, total count beyond i64); the
    `brutex.metrics.rust_stats` seam routes any error to the Python oracle.
    """
    ...

def decay_trailing_fold(returns: list[float]) -> tuple[float | None, float, float]:
    """Trailing-window fold `(sharpe, max_drawdown, net)` (DEC-NIGHTLY-DECAY-R48-001).

    One sequential pass over a per-trade return series: population-std
    Sharpe (zero risk-free, no annualisation; `None` for `n < 2`, zero or
    non-finite variance) + the largest peak-to-trough decline of the
    cumulative-return equity path (the lockbox `_max_drawdown` law) + the
    sequential net. Op-order transcription of the
    `brutex.nightly.decay_math` Python oracle (bit-identical by structure).
    """
    ...

def decay_verdict(
    trailing_sharpe: float | None,
    discovery_sharpe: float,
    trailing_max_dd: float,
    discovery_max_dd: float,
    n_trades: int,
    min_trades: int,
    sharpe_tolerance: float,
    mdd_tolerance: float,
) -> int:
    """R48 decay verdict code (DEC-NIGHTLY-DECAY-R48-001).

    `0` healthy / `1` decayed / `2` abstain-low-N / `3` abstain-undefined.
    Decayed iff `trailing_sharpe < (1 - sharpe_tolerance) * discovery_sharpe`
    OR `trailing_max_dd > (1 + mdd_tolerance) * discovery_max_dd` (the
    lockbox tolerance law); `n_trades < min_trades` abstains BEFORE any
    compare (never a decay claim at low N). Raises `ValueError` on a
    non-finite/negative tolerance, a non-finite `discovery_sharpe`, or a
    non-finite/negative `discovery_max_dd`.
    """
    ...

def champion_dominates(
    challenger_sharpe: float,
    challenger_max_dd: float,
    champion_sharpe: float,
    champion_max_dd: float,
) -> bool:
    """Champion/challenger domination compare (DEC-CHAMPION-CHALLENGER-001).

    True iff the challenger's lockbox Sharpe is STRICTLY higher AND its
    lockbox max-DD is no worse (`<=`). Raises `ValueError` on any
    non-finite input (a promotion never rides an undefined metric).
    """
    ...

def sweep_drawdown_stats(
    n_bars: int,
    start_equity_paisa: int,
    exits: list[tuple[int, int]],
) -> tuple[int, float, int, float]:
    """Fused per-mask equity fold + drawdown scan (DEC-SWEEP-RUST-METRICS-001).

    `exits` is `(exit_bar_idx, pnl_paisa)` per closed trade. Returns
    `(max_dd_paisa, max_dd_pct, max_dd_duration_bars, ulcer_index)` exactly as
    `brutex.metrics.compute._drawdown_stats` returns them (percentages ×100),
    bit-identical inside the `start + Σ|pnl| ≤ 2**52` exactness envelope the
    Python wrapper pre-guards. Raises `ValueError` outside the envelope.
    """
    ...

class ResidentBars:
    """Resident bar buffer — holds `bar_bits` Rust-side across batch calls.

    The `O(N_bars)` Python→Rust copy is paid ONCE in `__init__`; subsequent
    `count_supermasks` calls marshal only `masks`. Frozen + immutable: only
    `n_bars` is exposed (a `bars` getter would copy `O(N)` per access).
    """

    def __init__(self, bar_bits: list[int]) -> None: ...
    @property
    def n_bars(self) -> int: ...
    def count_supermasks(self, masks: list[int]) -> list[int]: ...
    def count_supermask(self, mask: int) -> int: ...

class ResidentBarsU128:
    """Resident bar buffer over a u128 domain (DEC-ENUM-KERNEL-U128-001).

    The wide twin of `ResidentBars`: holds u128 `bar_bits` Rust-side across
    batch calls (the `O(N_bars)` copy paid ONCE in `__init__`; subsequent
    `count_supermasks` calls marshal only `masks`). Frozen + immutable; only
    `n_bars` is exposed. A `bar_bits` / mask value outside `[0, 2**128)`
    raises `OverflowError` at the PyO3 extraction edge.
    """

    def __init__(self, bar_bits: list[int]) -> None: ...
    @property
    def n_bars(self) -> int: ...
    def count_supermasks(self, masks: list[int]) -> list[int]: ...
    def count_supermask(self, mask: int) -> int: ...

class ResidentSweepSim:
    """Resident Stage-2 trade-sim buffer — OHLC paisa + forced flags + sessions + bar_bits.

    DEC-SWEEP-RUST-TRADESIM-001 (MIGRATION.md #563). The per-bar SL/TP/forced-exit
    state machine (the Python `TradeSimWalker`) ported to Rust + fused with the
    per-mask entry projection. The `O(N_bars)` Python→Rust copy of the columns +
    flags + session ordinals + bar_bits is paid ONCE in `__init__`; subsequent
    `simulate_masks` / `count_trades` calls marshal only `masks`. Frozen +
    immutable (only `n_bars` is exposed). All money is int64 paisa; the
    price→paisa conversion, the forced-exit detection AND the per-bar session
    identity (`session_idx[i]` = bar `i`'s IST calendar-date ordinal) are done in
    Python (the single source of truth) so the trades are bit-identical to
    `brutex.engine.trade_sim.simulate_trades`. A pending entry only fills on a
    bar with the SAME `session_idx` it armed on — a cross-session fill (the
    overnight-gap leak, R2-OC-2) is dropped.

    Unequal column lengths raise `ValueError`.
    """

    def __init__(
        self,
        open: list[int],
        high: list[int],
        low: list[int],
        close: list[int],
        is_forced: list[bool],
        session_idx: list[int],
        bar_bits: list[int],
    ) -> None: ...
    @property
    def n_bars(self) -> int: ...
    def simulate_masks(
        self,
        masks: list[int],
        is_buy: bool,
        sl_paisa: int | None,
        tp_paisa: int | None,
    ) -> list[list[tuple[int, int, int, int, int, int]]]:
        """Simulate a batch of masks → per-mask round-trip trade tuples.

        `out[j]` is the trades for `masks[j]`, each a tuple
        `(pnl_paisa, mfe_paisa, mae_paisa, holding_bars, entry_idx, exit_idx)`.
        Empty `masks` → `[]`.
        """
        ...

    def count_trades(
        self,
        masks: list[int],
        is_buy: bool,
        sl_paisa: int | None,
        tp_paisa: int | None,
    ) -> list[int]:
        """Per-mask round-trip trade COUNT only (the throughput-bench fast path).

        Same state machine as `simulate_masks` without the per-trade allocation.
        `out[j]` aligns with `masks[j]`.
        """
        ...

class ResidentSweepSimU128:
    """Resident Stage-2 trade-sim buffer over a >64-bit vocabulary (u128 twin).

    DEC-TRADE-SIM-U128-KERNEL-001. Identical contract to `ResidentSweepSim`
    with `bar_bits` / `masks` widened to u128 — both widths share the ONE
    width-generic MaskWord core in `rust/fno-math/src/trade_sim.rs`, so the
    walk semantics cannot drift (an anti-drift test pins the two kernels
    field-by-field equal on u64-fit inputs). A `bar_bits` / mask value outside
    `[0, 2**128)` raises `OverflowError` at the PyO3 extraction edge; unequal
    column lengths raise `ValueError`. Today's production vocabulary is 60
    bits, so this kernel is future headroom — only tests (and a future >64-bit
    vocabulary) drive it.
    """

    def __init__(
        self,
        open: list[int],
        high: list[int],
        low: list[int],
        close: list[int],
        is_forced: list[bool],
        session_idx: list[int],
        bar_bits: list[int],
    ) -> None: ...
    @property
    def n_bars(self) -> int: ...
    def simulate_masks(
        self,
        masks: list[int],
        is_buy: bool,
        sl_paisa: int | None,
        tp_paisa: int | None,
    ) -> list[list[tuple[int, int, int, int, int, int]]]:
        """Simulate a batch of u128 masks → per-mask round-trip trade tuples.

        `out[j]` is the trades for `masks[j]`, each a tuple
        `(pnl_paisa, mfe_paisa, mae_paisa, holding_bars, entry_idx, exit_idx)`.
        Empty `masks` → `[]`.
        """
        ...

    def count_trades(
        self,
        masks: list[int],
        is_buy: bool,
        sl_paisa: int | None,
        tp_paisa: int | None,
    ) -> list[int]:
        """Per-mask round-trip trade COUNT only (no per-trade allocation).

        `out[j]` aligns with `masks[j]`.
        """
        ...
