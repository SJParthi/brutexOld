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
    "ResidentSweepSim",
    "ResidentSweepSimU128",
    "__version__",
    "add_u64",
    "bitand_count_u64s",
    "bitcount_u64s",
    "blake3_hash",
    "count_supermask_u64s",
    "count_supermasks_u64s",
    "count_supermasks_u64s_nthreads",
    "optcost_atm_offset",
    "optcost_grid_around_atm",
    "optcost_history_lookup",
    "optcost_moneyness_classify",
    "optcost_next_monthly_expiry",
    "optcost_next_weekly_expiry",
    "optcost_offset_strike",
    "optcost_regime_rate",
    "optcost_round_to_atm",
    "optcost_strike_offset_from_atm",
    "optcost_strike_step",
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

def optcost_regime_rate(starts: list[int], rates: list[int], on_ordinal: int) -> tuple[int, int]:
    """Date-regime rate lookup (DEC-OPTCOST-TAILS-RUST-001) — the
    `brutex.costs.constants.rate_for_date` decision kernel.

    `starts` are strictly-ascending `date.toordinal()` values; `rates` are
    parallel `rate_bps_x100` with `-1` marking a refusal row. Returns
    `(code, value)`: `(0, rate)` OK · `(1, row_index)` refusal (Python
    raises `UnverifiedCostRegimeError` from its own RegimeRow) ·
    `(2, 0)` empty table · `(3, 0)` pre-history (Python raises the exact
    existing ValueErrors). Raises `ValueError` on structurally invalid
    input — the seam degrades to the Python oracle.
    """
    ...

def optcost_history_lookup(
    starts: list[int], values: list[int], on_ordinal: int
) -> tuple[int, int]:
    """Dated history lookup — the shared `get_lot_size` /
    `_lookup_in_history` bisect law (DEC-OPTCOST-TAILS-RUST-001).

    Returns `(0, value)` OK (a `-1` value is legal data — the weekly
    NO_WEEKLY sentinel) or `(3, 0)` pre-history. Raises `ValueError` on
    poisoned input — the seam degrades to the Python oracle.
    """
    ...

def optcost_next_weekly_expiry(
    starts: list[int], dows: list[int], on_ordinal: int
) -> tuple[int, int]:
    """Next weekly expiry AT OR AFTER `on_ordinal` —
    `brutex.options.expiry_calendar.next_weekly_expiry` over a
    `(start_ordinal, dow | -1)` regime table.

    Returns `(0, expiry_ordinal)` · `(4, 0)` NO_WEEKLY (Python returns
    None) · `(3, 0)` pre-history. Raises `ValueError` on poisoned input.
    """
    ...

def optcost_next_monthly_expiry(
    starts: list[int], dows: list[int], year: int, month: int, on_ordinal: int
) -> tuple[int, int]:
    """Next monthly expiry AT OR AFTER `on_ordinal` — the "last <dow> of
    the month" law with the cross-month regime re-evaluation
    (`brutex.options.expiry_calendar.next_monthly_expiry`).

    `year`/`month` must be the ordinal's own civil year/month
    (consistency-checked). Returns `(0, expiry_ordinal)` or `(3, 0)`
    pre-history. Raises `ValueError` on poisoned/out-of-domain input
    (incl. the December-9999 rollover Python's `date` refuses).
    """
    ...

def optcost_moneyness_classify(
    is_put: bool, strike_paisa: int, spot_paisa: int, band_paisa: int
) -> int:
    """Moneyness bucket over i64 paisa —
    `brutex.data.moneyness.classify`'s graded decision (ITM=2 / ATM=3 /
    OTM=4; NA/UNKNOWN are decided Python-side pre-FFI). Raises
    `ValueError` on a violated precondition — the seam degrades to the
    Python oracle, preserving the module's never-raises contract.
    """
    ...

def optcost_atm_offset(strike_paisa: int, spot_paisa: int, step_paisa: int) -> int:
    """Signed ATM±N offset — `brutex.data.moneyness.atm_offset`'s
    ROUND_HALF_DOWN (half toward zero) law in exact integer arithmetic.
    Raises `ValueError` on non-positive inputs (Python returns None for
    those pre-FFI).
    """
    ...

def optcost_strike_step(strikes_paisa: list[int]) -> tuple[int, int]:
    """Strike-grid step — `brutex.data.moneyness.derive_strike_step`'s
    min-positive-consecutive-gap law over distinct positive i64 paisa
    strikes. Returns `(0, step_paisa)` or `(5, 0)` when no step is
    derivable (Python returns None). Raises `ValueError` on a
    non-positive strike.
    """
    ...

def optcost_offset_strike(atm_strike: int, step_count: int, strike_step: int, is_bull: bool) -> int:
    """OFFSET-rule strike resolution —
    `brutex.options.strike_rules.resolve_offset_strike`'s direction law
    (BULL adds `step_count * step`, BEAR inverts). The non-OFFSET-rule
    ValueError is decided Python-side pre-FFI. Raises `ValueError` on i64
    overflow (→ the Python oracle).
    """
    ...

def optcost_strike_offset_from_atm(atm_strike: int, offset_steps: int, step: int) -> int:
    """`brutex.options.strike_grid.strike_offset_from_atm` —
    `atm + offset_steps * step`, no bounds-check by contract. Raises
    `ValueError` on i64 overflow (→ the Python oracle).
    """
    ...

def optcost_grid_around_atm(atm_strike: int, step: int, half_width: int) -> list[int]:
    """`brutex.options.strike_grid.grid_around_atm` — the ATM-centered
    strike chain (negative `half_width` → empty, mirroring Python's
    range). Raises `ValueError` on i64 overflow or an absurd width (→ the
    Python oracle).
    """
    ...

def optcost_round_to_atm(spot_price: float, step: int) -> int:
    """`brutex.options.strike_grid.round_to_atm` — half-up grid rounding
    with CPython float semantics replicated operation-for-operation (the
    ONE sanctioned f64 FFI crossing: the Python function's own float-rupee
    input domain; no money AMOUNT is computed in floats). Raises
    `ValueError` outside the provable envelope (non-finite spot,
    out-of-i64 quotient, step past 2**53) — the seam degrades to the
    Python oracle.
    """
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
