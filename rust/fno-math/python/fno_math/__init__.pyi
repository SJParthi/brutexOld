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

__version__: str

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

class ResidentSweepSim:
    """Resident Stage-2 trade-sim buffer — OHLC paisa + forced flags + bar_bits.

    DEC-SWEEP-RUST-TRADESIM-001 (MIGRATION.md #562). The per-bar SL/TP/forced-exit
    state machine (the Python `TradeSimWalker`) ported to Rust + fused with the
    per-mask entry projection. The `O(N_bars)` Python→Rust copy of the 5 columns +
    flags + bar_bits is paid ONCE in `__init__`; subsequent `simulate_masks` /
    `count_trades` calls marshal only `masks`. Frozen + immutable (only `n_bars`
    is exposed). All money is int64 paisa; the price→paisa conversion + forced-exit
    detection are done in Python (the single source of truth) so the trades are
    bit-identical to `brutex.engine.trade_sim.simulate_trades`.

    Unequal column lengths raise `ValueError`.
    """

    def __init__(
        self,
        open: list[int],
        high: list[int],
        low: list[int],
        close: list[int],
        is_forced: list[bool],
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
        sl_next_open: bool = ...,
        tp_next_open: bool = ...,
        tp_first: bool = ...,
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
        sl_next_open: bool = ...,
        tp_next_open: bool = ...,
        tp_first: bool = ...,
    ) -> list[int]:
        """Per-mask round-trip trade COUNT only (the throughput-bench fast path).

        Same state machine as `simulate_masks` without the per-trade allocation.
        `out[j]` aligns with `masks[j]`.
        """
        ...
