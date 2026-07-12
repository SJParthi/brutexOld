"""Source-frozen hand-rolled fold reference (DEC-SWEEP-CORE-RUST-001).

MIGRATION.md #1739 (reserved) — the design §1(b) INDEPENDENT ORACLE for the
Rust ``fold_block`` differential: a stdlib-only reimplementation of the fold
algorithms, importing NEITHER ``brutex.metrics`` NOR ``fno_math`` (the AST
pin in ``test_reference_fold_frozen.py`` enforces the closed import set, and
a header sha256 pin freezes this source text). The recorded counterexample
this mandate exists for: a Pébay-m3 delta-sign mutant passed a differential
whose oracle SHARED ``_accumulate_moments`` — an oracle importing live
``compute.py`` leaf helpers proves nothing about them.

## Algorithm provenance (Golden Rule #1)

* :func:`_ref_moments` — Welford/Pébay (SAND2008-6212), token-for-token the
  ``compute.py:136-152`` update expressions (integer ``(n*n - 3*n + 3)``
  then converted; m4 before m3 before m2; strictly sequential).
* :func:`_ref_neumaier_sum` — CPython 3.12 ``builtins.sum()`` float fast
  path (Python/bltinmodule.c, gh-100425): running ``f_result``,
  compensation ``c``, the ``|f_result| >= |x|`` branch, and the GUARDED
  final add ``if c != 0.0 and isfinite(c): f_result += c`` — the guarded
  form, never the unconditional paraphrase. INT-START TRANSITION: CPython
  seeds from int 0 and consumes the first float OUTSIDE the loop
  (``0 + x₀``); this reference seeds ``f_result = x₀, c = 0.0`` — the
  natural Rust-port form, bit-identical for every finite ``x₀`` except
  ``-0.0`` (``0.0 + -0.0 == +0.0``), which is outside the fold domain
  (summands are non-negative squares) and excluded from the Neumaier
  corpus vectors. The frozen corpus cross-checks this loop against the
  REAL ``builtins.sum`` on every vector.
* :func:`_ref_percentile` — the ``compute.py:272-280`` two-branch linear
  lerp over a pre-sorted series.
* :func:`_ref_drawdown` — the ``compute.py:313-337`` per-bar scan over the
  materialised equity curve.
* :func:`reference_fold_row` — the design §2.3 per-mask fold pipeline
  (order is LAW): zero-trade short-circuit → integer folds with i64-narrow
  checks → the 2^52 envelope gate → returns → moments → Neumaier downside
  Σr² → finite pre-pass → stable sort + lerp → finite guard → SCAN LAST.
  A ``python_required`` row is FULLY ZEROED (Python never reads it).

FROZEN: edits to this file move the pinned header hash and require a
conscious re-pin (the point — the oracle can never drift silently with the
code under test).
"""

from __future__ import annotations

import math

#: The f64-exactness envelope (2^52) — the fold's bit1 gate.
MAX_EXACT_EQUITY_PAISA = 1 << 52

#: i64 bounds for the narrow checks.
I64_MIN = -(1 << 63)
I64_MAX = (1 << 63) - 1

#: Flag bits (design §1.3 column law): bit0 zero_trade, bit1 python_required.
FLAG_ZERO_TRADE = 1
FLAG_PYTHON_REQUIRED = 2


def _ref_moments(values: list[float]) -> tuple[float, float, float, float]:
    """Streaming Welford/Pébay first-four central moments (raw tuple)."""
    n = 0
    mean = 0.0
    m2 = 0.0
    m3 = 0.0
    m4 = 0.0
    for x in values:
        n1 = n
        n += 1
        delta = x - mean
        delta_n = delta / n
        delta_n2 = delta_n * delta_n
        term1 = delta * delta_n * n1
        mean += delta_n
        m4 += term1 * delta_n2 * (n * n - 3 * n + 3) + 6 * delta_n2 * m2 - 4 * delta_n * m3
        m3 += term1 * delta_n * (n - 2) - 3 * delta_n * m2
        m2 += term1
    return mean, m2, m3, m4


def _ref_neumaier_sum(values: list[float]) -> float:
    """CPython 3.12 ``builtins.sum()`` float fast path — Neumaier, guarded.

    Seed ``f_result = x₀, c = 0.0``; per item ``t = f_result + x``, branch
    on ``|f_result| >= |x|``; GUARDED final add (module docstring). Empty
    input returns ``0.0`` (``builtins.sum([]) == 0`` packs identically).
    """
    if not values:
        return 0.0
    f_result = values[0]
    c = 0.0
    for x in values[1:]:
        t = f_result + x
        if abs(f_result) >= abs(x):
            c += (f_result - t) + x
        else:
            c += (x - t) + f_result
        f_result = t
    if c != 0.0 and math.isfinite(c):
        f_result += c
    return f_result


def _ref_percentile(sorted_values: list[float], pct: float) -> float:
    """Two-branch linear-interpolation percentile over a pre-sorted series."""
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = (pct / 100.0) * (len(sorted_values) - 1)
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return sorted_values[lower]
    frac = rank - lower
    return sorted_values[lower] * (1.0 - frac) + sorted_values[upper] * frac


def _ref_drawdown(
    n_bars: int, start: int, exits: list[tuple[int, int]]
) -> tuple[int, float, int, float]:
    """Per-bar drawdown scan over the exit scatter's equity curve.

    Returns ``(max_dd_paisa, max_dd_pct*100, max_dd_bars, ulcer*100)`` —
    the ``_drawdown_stats`` contract over
    ``equity[i] = start + Σ_{(idx,pnl): idx <= i} pnl``.
    """
    realized = [0] * n_bars
    for idx, pnl in exits:
        realized[idx] += pnl
    equity: list[int] = []
    running = start
    for delta in realized:
        running += delta
        equity.append(running)
    peak = equity[0]
    max_dd_paisa = 0
    max_dd_pct = 0.0
    sum_sq_pct = 0.0
    current_dd_bars = 0
    max_dd_bars = 0
    for value in equity:
        peak = max(peak, value)
        drop = peak - value
        if drop > 0:
            current_dd_bars += 1
        else:
            current_dd_bars = 0
        max_dd_bars = max(max_dd_bars, current_dd_bars)
        max_dd_paisa = max(max_dd_paisa, drop)
        if peak > 0:
            dd_pct = drop / peak
            max_dd_pct = max(max_dd_pct, dd_pct)
            sum_sq_pct += dd_pct * dd_pct
    ulcer = math.sqrt(sum_sq_pct / len(equity))
    return max_dd_paisa, max_dd_pct * 100.0, max_dd_bars, ulcer * 100.0


def _zero_row(flags: int) -> tuple[tuple[int, ...], tuple[float, ...], int]:
    """A fully-zeroed 15-int/9-float row with *flags*."""
    return (0,) * 15, (0.0,) * 9, flags


def reference_fold_row(
    trades: list[tuple[int, int, int, int, int]], n_bars: int, start: int
) -> tuple[tuple[int, ...], tuple[float, ...], int]:
    """The full §2.3 fold pipeline over one trade stream.

    ``trades`` entries are ``(pnl, mfe, mae, holding, exit_idx)``. Returns
    ``(ints ×15, floats ×9, flags)`` in the design §1.3 column-law order:
    ints ``n_trades, net_pnl, gross_profit, gross_loss_abs, winners,
    losers, sum_mfe, sum_mae, sum_holding, max_holding, largest_loss,
    max_consecutive_losses, max_consecutive_wins, max_dd_paisa,
    max_dd_bars``; floats ``mean, m2, m3, m4, sum_sq_downside, p95, p5,
    max_dd_pct, ulcer``.
    """
    if not trades:
        return _zero_row(FLAG_ZERO_TRADE)
    n_trades = len(trades)
    net_pnl = 0
    gross_profit = 0
    gross_loss_abs = 0
    winners = 0
    losers = 0
    sum_mfe = 0
    sum_mae = 0
    sum_holding = 0
    max_holding = 0
    largest_loss = 0
    max_losses = 0
    max_wins = 0
    cur_losses = 0
    cur_wins = 0
    for pnl, mfe, mae, holding, _exit_idx in trades:
        net_pnl += pnl
        sum_mfe += mfe
        sum_mae += mae
        sum_holding += holding
        max_holding = max(max_holding, holding)
        if pnl > 0:
            winners += 1
            gross_profit += pnl
            cur_wins += 1
            cur_losses = 0
        elif pnl < 0:
            losers += 1
            gross_loss_abs += -pnl
            largest_loss = min(largest_loss, pnl)
            cur_losses += 1
            cur_wins = 0
        else:
            cur_wins = 0
            cur_losses = 0
        max_wins = max(max_wins, cur_wins)
        max_losses = max(max_losses, cur_losses)
    ints_head = (
        n_trades,
        net_pnl,
        gross_profit,
        gross_loss_abs,
        winners,
        losers,
        sum_mfe,
        sum_mae,
        sum_holding,
        max_holding,
        largest_loss,
        max_losses,
        max_wins,
    )
    if any(v < I64_MIN or v > I64_MAX for v in ints_head):
        return _zero_row(FLAG_PYTHON_REQUIRED)
    magnitude = start
    for pnl, *_rest in trades:
        magnitude += abs(pnl)
    if magnitude > MAX_EXACT_EQUITY_PAISA:
        return _zero_row(FLAG_PYTHON_REQUIRED)
    returns = [pnl / start for pnl, *_rest in trades]
    mean, m2, m3, m4 = _ref_moments(returns)
    downside_squares = [r * r for r in returns if r < 0.0]
    downside_sum_sq = _ref_neumaier_sum(downside_squares)
    if any(not math.isfinite(r) for r in returns):
        return _zero_row(FLAG_PYTHON_REQUIRED)
    sorted_returns = sorted(returns)
    p95 = _ref_percentile(sorted_returns, 95.0)
    p5 = _ref_percentile(sorted_returns, 5.0)
    pre_scan = (mean, m2, m3, m4, downside_sum_sq, p95, p5)
    if any(not math.isfinite(v) for v in pre_scan):
        return _zero_row(FLAG_PYTHON_REQUIRED)
    exits = [(exit_idx, pnl) for pnl, _mfe, _mae, _holding, exit_idx in trades]
    max_dd_paisa, max_dd_pct, max_dd_bars, ulcer = _ref_drawdown(n_bars, start, exits)
    floats = (*pre_scan, max_dd_pct, ulcer)
    if any(not math.isfinite(v) for v in floats):
        return _zero_row(FLAG_PYTHON_REQUIRED)
    ints = (*ints_head, max_dd_paisa, max_dd_bars)
    return ints, floats, 0
