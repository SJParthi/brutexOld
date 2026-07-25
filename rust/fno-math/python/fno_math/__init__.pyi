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
    "ResidentSweepEnum",
    "ResidentSweepEnumU128",
    "ResidentSweepSim",
    "ResidentSweepSimU128",
    "__version__",
    "add_u64",
    "alpha_vs_buyhold_pct",
    "bitand_count_u64s",
    "bitcount_u64s",
    "blake3_hash",
    "count_supermask_u64s",
    "count_supermask_u128s",
    "count_supermasks_u64s",
    "count_supermasks_u64s_nthreads",
    "count_supermasks_u128s",
    "exec_apply_futures_fill",
    "exec_apply_slippage",
    "exec_futures_charge_stack",
    "exec_futures_materialize",
    "exec_option_charge_stack",
    "exec_options_materialize",
    "exec_round_to_paisa",
    "exec_round_to_rupee",
    "metrics_fold_recency",
    "metrics_fold_trades",
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
    "rank_axis_topm",
    "rank_dd_band_indices",
    "rank_pareto_frontier",
    "ranking_composite_scores",
    "ranking_composite_term_inputs",
    "ranking_cr_div_i64",
    "ranking_dsr_z_contributions",
    "ranking_gate_reasons",
    "ranking_overfit_verdicts",
    "ranking_passes_gates",
    "ranking_rank_order",
    "ranking_zscores",
    "stats_cpcv_pass_count",
    "stats_cvar_tail_fold",
    "stats_expected_max_sharpe",
    "stats_pbo_cscv",
    "stats_psr",
    "stats_wfo_pass_count",
    "sweep_drawdown_stats",
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

def alpha_vs_buyhold_pct(
    net_pnl_paisa: int,
    start_equity_paisa: int,
    anchor_open_paisa: int,
    last_close_paisa: int,
) -> float:
    """ALPHA vs buy-&-hold, percent (DEC-ALPHA-BUYHOLD-METRIC-001).

    `((net_pnl_paisa as f64) / (start_equity_paisa as f64) * 100.0) -
    (((last_close_paisa - anchor_open_paisa) as f64) /
    (anchor_open_paisa as f64) * 100.0)` under the fixed convert-first
    IEEE-754 op order — bit-identical to the Python oracle
    (`brutex.engine.alpha_metric.alpha_vs_buyhold_pct_python`). Raises
    `ValueError` on a domain violation (`start_equity_paisa < 1`,
    `anchor_open_paisa < 1`, `last_close_paisa < 0`).
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

def rank_pareto_frontier(
    net_pnl_paisa: list[int],
    win_rate: list[float],
    max_dd_pct: list[float],
    losing_trades: list[int],
    eps_net_pnl_paisa: int,
    eps_win_rate: float,
    eps_max_dd_pct: float,
    eps_losing_trades: int,
) -> list[int]:
    """Additive-ε Pareto frontier over the four W1 axes (LANE W1).

    Returns the ASCENDING indices of every row NOT ε-dominated by any other
    row under `{net_pnl MAX, win_rate MAX, max_dd_pct MIN, losing_trades MIN}`.
    O(n²) worst case (disclosed — never O(1)). Raises `ValueError` on a
    length mismatch, a non-finite float, or a negative/non-finite ε; the
    `brutex.engine.rust_rank_select` seam routes any error to the oracle.
    """
    ...

def rank_axis_topm(
    m: int,
    composite: list[float],
    mask_lo: list[int],
    mask_hi: list[int],
    max_dd_pct: list[float],
    losing_trades: list[int],
    win_rate: list[float],
    expectancy_paisa: list[int],
) -> tuple[list[int], list[int], list[int], list[int], list[int]]:
    """Per-axis bounded top-M select over the five quota axes (LANE W1).

    Returns five best-first index lists in `(composite DESC, max_dd_pct ASC,
    losing_trades ASC, win_rate DESC, expectancy_paisa DESC)` order, each
    tie-broken `(axis, −composite, mask)`. O(A·n·log M) time, O(A·M) space.
    Raises `ValueError` on a length mismatch, a non-finite float, or an int
    axis value outside the f64-exact `|v| <= 2**53` envelope.
    """
    ...

def rank_dd_band_indices(max_dd_pct: list[float], band_pct: float) -> list[int]:
    """Batch DD ε-band quantizer: `floor(dd / band_pct)` per element (LANE W1).

    Bit-identical to Python's `math.floor(dd / band)` on finite inputs (same
    IEEE division + floor). Raises `ValueError` on a non-positive/non-finite
    band, a non-finite dd, or a quotient outside the f64-exact int envelope.
    """
    ...

def ranking_passes_gates(
    total_trades: list[int],
    profit_factor: list[float | None],
    max_drawdown_pct: list[float],
    win_rate: list[float],
    win_loss_ratio: list[float | None],
    max_consecutive_losses: list[int],
    streak_threshold: list[int],
    loss_rate: Sequence[float | None],
    sl_hit_rate: Sequence[float | None],
    max_sl_hit_rate: float | None,
) -> list[bool]:
    """Batched `brutex.ranking.passes_gates` twin (slice-4 PR-1).

    One verdict per candidate, positionally aligned. `None` metric values
    cross as `None` (never NaN-encoded). Raises `ValueError` on unequal
    column lengths.

    Door-5/6 fold-in-Rust/combine-in-Python columns (DEC-METRICS-RUST-STATS-002,
    the #1544 ranking seam): the transcendental ``streak_threshold`` is the
    shared Python seam's per-row output (kernel does only host-invariant
    integer/finite compares), ``loss_rate``/``sl_hit_rate`` feed Door 5/6, and
    the scalar ``max_sl_hit_rate`` is the opt-in Door-6 knob (``None`` = inert).
    """
    ...

def ranking_gate_reasons(
    total_trades: list[int],
    profit_factor: list[float | None],
    max_drawdown_pct: list[float],
    win_rate: list[float],
    win_loss_ratio: list[float | None],
    max_consecutive_losses: list[int],
    streak_threshold: list[int],
    loss_rate: Sequence[float | None],
    sl_hit_rate: Sequence[float | None],
    max_sl_hit_rate: float | None,
) -> list[list[str]]:
    """Batched `brutex.ranking.gate_reasons` twin — the exact wire tokens.

    Door-5/6 columns mirror :func:`ranking_passes_gates` (the #1544 ranking
    seam). Raises `ValueError` on unequal column lengths.
    """
    ...

def ranking_zscores(values: list[float]) -> list[float]:
    """`brutex.ranking._zscores` twin — population z-scores clamped [-3, +3].

    Class (b*): always run under the Python-side per-call differential.
    Raises `ValueError` on an empty input (Python raises ZeroDivisionError).
    """
    ...

def ranking_composite_term_inputs(
    sharpe_ratio: list[float | None],
    sortino_ratio: list[float | None],
    calmar_ratio: list[float | None],
    profit_factor: list[float | None],
    recovery_factor: list[float | None],
    win_rate: list[float],
    win_loss_ratio: list[float | None],
    avg_mfe_paisa: list[int],
    avg_mae_paisa: list[int],
) -> list[list[float]]:
    """Batched `brutex.ranking.composite_term_inputs` twin (7 columns each).

    Returns one 7-element ROW per survivor as a `list[list[float]]` —
    deliberately NOT list-of-tuples (the Rust wrapper is `Vec<Vec<f64>>`;
    a 7-tuple return type was measured to explode the cargo-mutants
    value-enumeration ~45× and was avoided — see `ranking.rs`). Raises
    `ValueError` on unequal column lengths or a paisa magnitude beyond
    the 2**53 correctly-rounded-division fast-path envelope.
    """
    ...

def ranking_composite_scores(
    term_columns: list[list[float]],
    dsr_contrib: list[float],
) -> list[float]:
    """`brutex.ranking.composite_scores` twin — z-normalise + weighted fold.

    Raises `ValueError` unless exactly 7 term columns, each at least as
    long as `dsr_contrib` and non-empty.
    """
    ...

def ranking_dsr_z_contributions(
    dsr_by_survivor: list[float],
    weight: float,
) -> list[float]:
    """ACTIVE-arm `brutex.ranking.dsr_z_contributions` twin (z-scores × weight).

    Raises `ValueError` on an empty input.
    """
    ...

def ranking_rank_order(composites: list[float], masks: list[int]) -> list[int]:
    """`rank_combos` total-order sort twin — the INDEX PERMUTATION over
    `(-composite_score, mask)` (score DESC, unique mask ASC tie-break;
    `-0.0 == +0.0`).

    Raises `ValueError` on a length mismatch or a non-finite composite.
    """
    ...

def ranking_overfit_verdicts(dsrs: list[float | None], dsr_min: float) -> list[bool]:
    """Batched `brutex.ranking.overfit.passes_overfit_gate` twin
    (`dsr is not None and dsr >= dsr_min`, boundary inclusive)."""
    ...

def ranking_cr_div_i64(numerator: int, denominator: int) -> float:
    """Correctly-rounded i64/i64 → f64 division — the M1 FAST path.

    Raises `ValueError` on a zero denominator or an operand magnitude
    beyond 2**53 (the exact-cast envelope; the slow path is PR-2).
    """
    ...

# --- exec (slice 7 / P8, DEC-EXEC-RUST-CORE-001, MIGRATION.md #1960) ---
# Options/futures execution integer cores (rust/fno-math/src/exec/). All
# money values are int64 paisa; rates use the `rate_decimal × 10^7` encoding;
# a charge stack crosses as the PINNED 17-slot list mirroring
# `brutex.costs.types.ChargeStack`; out-of-envelope arithmetic raises
# `ValueError` (the Python seam falls back to the Python oracle arm).

def exec_round_to_paisa(amount_x_bps_x100: int) -> int:
    """ADVERSE CEILING at scale 10^7 (`_round_levy_paisa` twin,
    DEC-FILL-WORST-CASE-ONLY-001)."""
    ...

def exec_round_to_rupee(amount_paisa: int) -> int:
    """ADVERSE CEILING to a multiple of 100 paise (`_round_levy_rupee` twin)."""
    ...

def exec_apply_slippage(
    entry_high_paisa: int,
    entry_low_paisa: int,
    exit_high_paisa: int,
    exit_low_paisa: int,
    is_long: bool,
) -> tuple[int, int, int]:
    """Worst-case bar-extreme fill law →
    `(buy_fill, sell_fill, realized_slip_per_share)`."""
    ...

def exec_apply_futures_fill(
    entry_high_paisa: int,
    entry_low_paisa: int,
    exit_high_paisa: int,
    exit_low_paisa: int,
    is_long: bool,
) -> tuple[int, int, int]:
    """Futures worst-case fill law (numerically the reconciled
    `exec_apply_slippage`)."""
    ...

def exec_option_charge_stack(
    entry_high_paisa: int,
    entry_low_paisa: int,
    exit_high_paisa: int,
    exit_low_paisa: int,
    is_long: bool,
    lot_size: int,
    n_lots: int,
    brokerage_round_trip_paisa: int,
    stt_rate: int,
    etc_rate: int,
    sebi_rate: int,
    ipft_rate: int,
    stamp_rate: int,
    gst_rate: int,
) -> list[int]:
    """CALCULATOR_SPEC §4 steps 1,3–13 → the pinned 17-slot wire list."""
    ...

def exec_futures_charge_stack(
    entry_high_paisa: int,
    entry_low_paisa: int,
    exit_high_paisa: int,
    exit_low_paisa: int,
    is_long: bool,
    lot_size: int,
    n_lots: int,
    cost_free: bool,
    brokerage_flat_paisa: int,
    brokerage_turnover_bps_x100: int | None,
    stt_rate: int,
    etc_rate: int,
    sebi_rate: int,
    ipft_rate: int,
    stamp_rate: int,
    gst_rate: int,
) -> list[int]:
    """Futures charge stack (cost-free short-circuit) → 17-slot wire list."""
    ...

def exec_options_materialize(
    entry_open_paisa: int,
    exit_open_paisa: int,
    entry_high_paisa: int,
    entry_low_paisa: int,
    exit_high_paisa: int,
    exit_low_paisa: int,
    entry_volume: int,
    exit_volume: int,
    is_long: bool,
    lot_size: int,
    n_lots: int,
    brokerage_round_trip_paisa: int,
    stt_rate: int,
    etc_rate: int,
    sebi_rate: int,
    ipft_rate: int,
    stamp_rate: int,
    gst_rate: int,
) -> tuple[str, list[int] | None]:
    """Options post-I/O tail (UNCONDITIONAL stale gate) →
    `("stale_bar"|"tick_floor", None)` or `("trade", wire)`."""
    ...

def exec_futures_materialize(
    entry_high_paisa: int,
    entry_low_paisa: int,
    exit_high_paisa: int,
    exit_low_paisa: int,
    entry_volume: int,
    exit_volume: int,
    path_highs_paisa: list[int],
    path_lows_paisa: list[int],
    is_long: bool,
    lot_size: int,
    n_lots: int,
    cost_free: bool,
    brokerage_flat_paisa: int,
    brokerage_turnover_bps_x100: int | None,
    stt_rate: int,
    etc_rate: int,
    sebi_rate: int,
    ipft_rate: int,
    stamp_rate: int,
    gst_rate: int,
) -> tuple[str, list[int] | None]:
    """Futures post-I/O tail (UNCONDITIONAL stale gate) →
    `("stale_bar", None)` or `("trade", wire+[mae, mfe])`."""
    ...

# --- end exec (slice 7 / P8) ---

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

def stats_expected_max_sharpe(n_trials: int, trial_sharpe_variance: float) -> float:
    """Expected maximum Sharpe `SR_0` of `n_trials` zero-edge trials
    (DEC-METRICS-RUST-STATS-002).

    The operation-for-operation transcription of
    `brutex.metrics.deflated_sharpe.expected_max_sharpe_ratio`'s tail (two
    AS241 standard-normal quantiles + the Gumbel combination). Raises
    `ValueError` on a domain-invalid input or on the `inv_cdf` raise domain
    (`1 - 1/N` rounding to 1.0 at astronomical `N`); the
    `brutex.metrics.rust_stats` seam routes any error to the Python oracle.
    """
    ...

def stats_pbo_cscv(
    block_sum: list[float],
    block_sumsq: list[float],
    n_splits: int,
    n_strat: int,
    is_count: int,
) -> tuple[int, list[float]]:
    """CSCV split-enumeration logit loop (DEC-METRICS-RUST-STATS-002).

    `block_sum`/`block_sumsq` are `brutex.metrics.pbo._block_moments`'s
    per-block per-strategy moment matrices flattened row-major. Returns
    `(overfit_count, logits)` in `itertools.combinations` split order —
    the exact `pbo_probability` loop with the default `sharpe_from_moments`
    evaluator, operation-for-operation. Raises `ValueError` on a
    structurally invalid input or past the C(n_splits, S/2) combination cap;
    the `brutex.metrics.rust_stats` seam routes any error to the oracle.
    """
    ...

def stats_psr(
    observed_sharpe: float,
    benchmark_sharpe: float,
    sample_length: int,
    skew: float,
    kurtosis: float,
) -> float:
    """Probabilistic Sharpe Ratio `PSR(SR*)` (DEC-METRICS-RUST-STATS-002).

    The transcription of
    `brutex.metrics.deflated_sharpe.probabilistic_sharpe_ratio`'s tail,
    with `erf` bound to the SAME system-libm symbol CPython's `math.erf`
    calls. Raises `ValueError` on a domain-invalid input (structurally
    unreachable behind the Python validation); the seam routes any error to
    the Python oracle.
    """
    ...

def stats_wfo_pass_count(
    returns: list[float],
    test_starts: list[int],
    test_stops: list[int],
    min_sharpe: float,
) -> int:
    """Walk-forward test-window pass-count (DEC-METRICS-RUST-STATS-002).

    Counts walks whose test-window Sharpe (the numpy-pairwise-REPLICATED
    `sharpe_of_returns` mirror) is defined and STRICTLY greater than
    `min_sharpe`. Raises `ValueError` on a structurally invalid window; the
    `brutex.metrics.rust_stats` seam routes any error to the Python oracle.
    """
    ...

def stats_cvar_tail_fold(
    pnls: list[int],
    alpha_bps: int,
) -> tuple[int, int, int]:
    """CVaR worst-k tail fold over one candidate's per-trade P&L list (DEC-RANK-CVAR-TAIL-GATE-001).

    `pnls` is int paisa per closed trade; `alpha_bps` the tail fraction in
    basis points (500 = the worst 5%). Returns `(k, var_paisa,
    tail_sum_paisa)` — `k = max(1, ceil(n * alpha_bps / 10_000))`, the k-th
    smallest P&L (the VaR bound) and the exact integer sum of the worst k
    (an i128 crossing as an exact Python int). All-integer, bit-identical
    to the `brutex.ranking.cvar_gate` Python oracle by construction. Raises
    `ValueError` on an empty list or `alpha_bps` outside `[1, 10_000]`; the
    seam routes any error to the Python oracle.
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

def metrics_fold_recency(
    pnl_paisa: list[int],
    mfe_paisa: list[int],
    mae_paisa: list[int],
    holding_bars: list[int],
    returns: list[float],
    exit_us: list[int],
    anchor_us: int,
    cutoff_us: int,
) -> tuple[
    tuple[
        tuple[int, int, int, int, int, int, int],
        tuple[int, int, int, int, int, int],
        tuple[int, float, float, float, float],
        tuple[float, float, float],
    ]
    | None,
    list[tuple[int, int]],
]:
    """Recency FOLD twin (DEC-METRICS-TRAILING-WINDOW-001 Stage-C2).

    The Rust mirror of `brutex.metrics.compute.fold_recency`: the trailing
    window's `metrics_fold_trades` wire over the `exit_us >= cutoff_us`
    subsequence (`None` when the window holds zero trades) plus the exact
    integer `(age_month_bucket, net_pnl_paisa)` pairs sorted ascending
    (bucket = `max(0, div_euclid(anchor_us - exit_us, 86_400_000_000)) //
    30` — the `timedelta.days` floor law). `returns` and the epoch-µs
    instants are Python-computed (bit-identity by construction). Raises
    `ValueError` outside the validated envelope; the seam degrades to the
    Python oracle.
    """
    ...

def metrics_fold_trades(
    pnl_paisa: list[int],
    mfe_paisa: list[int],
    mae_paisa: list[int],
    holding_bars: list[int],
    returns: list[float],
) -> tuple[
    tuple[int, int, int, int, int, int, int],
    tuple[int, int, int, int, int, int],
    tuple[int, float, float, float, float],
    tuple[float, float, float],
]:
    """Per-trade metrics FOLD (DEC-METRICS-RUST-COMPUTE-FOLD-001).

    The Rust mirror of `brutex.metrics.compute._fold_trades` over five
    parallel per-trade columns; `returns` is the Python-computed
    `pnl_paisa / start_equity_paisa` column (bit-identity by construction).
    Returns four nested tuples the seam reassembles into `TradeFolds`:
    `(n_trades, winners, losers, max_holding_bars, largest_loss_paisa,
    max_consecutive_losses, max_consecutive_wins)` · `(net_pnl_paisa,
    gross_profit_paisa, gross_loss_abs_paisa, sum_mfe_paisa, sum_mae_paisa,
    sum_holding_bars)` · the `(n, mean, m2, m3, m4)` moments · the
    `(downside_sum_sq, p95, p5)` floats. Raises `ValueError` on any input
    outside the validated envelope; the seam degrades to the Python oracle.
    """
    ...
class ResidentSweepEnum:
    """Resident u64-width sweep-enumeration substrate (DEC-SWEEP-ENUM-RUST-001).

    Holds the tuple's `bar_bits` window as condition-major transposed
    bitplanes (built once in `__init__` — the `O(n_bars)` Python→Rust copy +
    transpose is paid per tuple). `enumerate_prefilter` serves RANGES of the
    fused k-of-bits enumeration + conflict prune + Stage-1B min-hits count in
    the exact `iter_masks_k_of_bits` lexicographic order, returning the
    design sextuple `(enumerated_in_range, conflict_pruned_in_range,
    next_rank, exhausted, hits_counted_in_range, kept_masks)`. Raises
    `ValueError` outside the validated envelope (a condition-bit or pair
    index >= 64, k > n_unique, malformed pairs, start_rank > C(n, k), a
    total count past the u64 rank domain) and `MemoryError` when a buffer
    cannot be reserved (the global-stop class — re-raised bare by the
    wrapper).
    """

    def __init__(self, bar_bits: list[int]) -> None: ...
    def n_bars(self) -> int: ...
    def enumerate_prefilter(
        self,
        condition_bits: list[int],
        k: int,
        conflict_pairs: list[tuple[int, int]],
        min_hits: int,
        start_rank: int,
        max_enumerated: int,
        max_work_cells: int,
    ) -> tuple[int, int, int, bool, int, list[int]]: ...

class ResidentSweepEnumU128:
    """Resident u128-width sweep-enumeration substrate — the wide twin of
    `ResidentSweepEnum` for condition bits in `[64, 128)` (legal per
    `MAX_VOCAB_BITS = 128`; production-unreachable today,
    `IMPLEMENTED_MASK < 2**60`)."""

    def __init__(self, bar_bits: list[int]) -> None: ...
    def n_bars(self) -> int: ...
    def enumerate_prefilter(
        self,
        condition_bits: list[int],
        k: int,
        conflict_pairs: list[tuple[int, int]],
        min_hits: int,
        start_rank: int,
        max_enumerated: int,
        max_work_cells: int,
    ) -> tuple[int, int, int, bool, int, list[int]]: ...

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
    """Resident Stage-2 dual-feed trade-sim buffer — 1m EXECUTION columns + arm_source + bar_bits.

    DEC-SWEEP-RUST-TRADESIM-001 (MIGRATION.md #563), rebuilt on the 1-minute
    execution grid by DEC-FILL-EXEC-GRID-1M-001. The per-1m-bar SL/TP/forced
    state machine (the Python `TradeSimWalker`) ported to Rust + fused with the
    per-mask entry projection: the four `exec_*` OHLC paisa columns, the
    `exec_is_forced` flags and the `exec_session_idx` ordinals cover the
    1-MINUTE EXECUTION stream (the walk domain); `bar_bits` stays
    DECISION-length (the signal domain) and `arm_source[j]` names the decision
    bar whose next-1m fill lands on 1m bar `j` (`-1` = unarmed; from
    `brutex.engine.exec_grid.ExecGridMap` — the mapping gates are applied at
    build time in Python). The `O(N_1m + N_tf)` Python→Rust copy is paid ONCE
    in `__init__`; subsequent `simulate_masks` / `count_trades` calls marshal
    only `masks`. Frozen + immutable (only `n_bars` — the 1m walk length — is
    exposed). All money is int64 paisa; the price→paisa conversion, the
    forced-exit detection, the session identity AND the arm_source mapping are
    done in Python (the single source of truth) so the trades are bit-identical
    to `brutex.engine.trade_sim.simulate_trades`. Trade-record indices/holding
    are 1-MINUTE quantities (design §2.6).

    Unequal execution-column lengths, an out-of-range or non-increasing
    `arm_source`, or an out-of-envelope price raise `ValueError`.
    """

    def __init__(
        self,
        exec_open: list[int],
        exec_high: list[int],
        exec_low: list[int],
        exec_close: list[int],
        exec_is_forced: list[bool],
        exec_session_idx: list[int],
        arm_source: list[int],
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

    def simulate_masks_with_exit_counts(
        self,
        masks: list[int],
        is_buy: bool,
        sl_paisa: int | None,
        tp_paisa: int | None,
    ) -> list[
        tuple[list[tuple[int, int, int, int, int, int]], tuple[int, int, int, int, int, int]]
    ]:
        """`simulate_masks` PLUS per-mask exit-reason counters (the additive twin).

        DEC-METRICS-LOSSRATE-FIRSTCLASS-001 Stage B: `out[j]` is
        `(trades, exit_reason_counts)` for `masks[j]` — the trades byte-equal
        to `simulate_masks`'s (one shared walk core), the counts one `u64` per
        `EXIT_REASONS` member in vocabulary order `(stop_loss, take_profit,
        forced_1520, signal, end_of_data, session_gap)`, with
        `sum(counts) == len(trades)`.
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
    vocabulary) drive it. The constructor takes the SAME dual-feed
    execution-grid faces as `ResidentSweepSim` (DEC-FILL-EXEC-GRID-1M-001).
    """

    def __init__(
        self,
        exec_open: list[int],
        exec_high: list[int],
        exec_low: list[int],
        exec_close: list[int],
        exec_is_forced: list[bool],
        exec_session_idx: list[int],
        arm_source: list[int],
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

    def simulate_masks_with_exit_counts(
        self,
        masks: list[int],
        is_buy: bool,
        sl_paisa: int | None,
        tp_paisa: int | None,
    ) -> list[
        tuple[list[tuple[int, int, int, int, int, int]], tuple[int, int, int, int, int, int]]
    ]:
        """Batch trades PLUS per-mask exit-reason counters — the wide twin of
        `ResidentSweepSim.simulate_masks_with_exit_counts` (same vocabulary
        order, same `sum(counts) == len(trades)` law)."""
        ...
