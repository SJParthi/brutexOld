"""Mint the frozen-bytes sweep-core fold corpus (DEC-SWEEP-CORE-RUST-001).

MIGRATION.md #1739 (reserved) — Slice 2 of the Rust sweep-core track, hard
gate C0 of the implementation spec: the INDEPENDENT-ORACLE MANDATE
(design-of-record ``docs/design/sweep_core/DESIGN_FINAL_RUST_SWEEP_CORE.md``
§6 Slice-2 binding block) requires the Rust ``fold_block`` differential to
verify against a genuinely independent oracle — a frozen-bytes golden corpus
minted ONCE, then committed as BYTES. This script is the ONE minter.

## What it writes

* ``tests/golden/fixtures/sweep_core_fold_corpus.json`` — ~200 trade-stream
  records + helper-level float vectors, each carrying its expected fold
  outputs as ``struct.pack('<q'/'<d')`` hex (LE bytes — the exact
  ``fold_block`` column encoding). After commit the corpus is BYTES: the
  verifying tests import nothing from ``brutex.metrics`` or ``fno_math``.
* ``tests/golden/fixtures/sweep_core_fold_corpus.json.sha256`` — the
  corpus digest (the tamper pin).
* ``rust/fno-math/src/sweep_core_vectors.rs`` — the SAME numbers as
  ``#[cfg(test)]`` Rust consts, consumed by the in-crate ``sweep_core.rs``
  vector tests (the design §1(a) "same numbers" clause; this kills the
  Pébay-m3 mutant class — the expected m3 bytes are frozen literals, never
  a shared helper's output).

## Authority (Golden Rule #1 — no hallucination)

Expected values are computed by RUNNING the literal CPython 3.12
expressions at mint time — the token-for-token bodies of
``brutex/metrics/compute.py``'s ``_accumulate_moments`` (Welford/Pébay),
``percentile`` (the two-branch lerp), ``_drawdown_stats`` (the per-bar
scan), and the REAL ``builtins.sum`` over the downside squares (CPython
3.12's Neumaier-compensated float fast path, gh-100425 — running the actual
builtin IS the authority, so the corpus freezes CPython's guarded-final-add
semantics without paraphrase). The bodies are REPRODUCED here (stdlib-only,
zero ``brutex`` imports) so the corpus is independent of the live package:
a defect that edits ``compute.py`` and its tests together can never move
these bytes.

## Record-shape delta from the design §1(a) sketch (recorded, deliberate)

The design sketch ``{trades: [[pnl,mfe,mae,holding],…], start_equity_paisa,
expected_ints_hex, expected_floats_hex}`` omits the inputs the DRAWDOWN
columns (ints[13..14] + floats[7..8]) are functions of: the exit scatter.
Each trade therefore carries a 5th element ``exit_idx`` and each record
carries ``n_bars`` — additive fields, the sketch's fields all kept.

## The fold pipeline mirrored (design §2.3 — order is LAW)

zero-trade short-circuit (flags bit0, zeroed row, NO scan) → integer folds
with i64-narrow checks (fail ⇒ bit1) → the 2^52 envelope gate
(``start + Σ|pnl| > 2^52`` ⇒ bit1) → returns ``pnl / start`` → Pébay
moments → Neumaier downside Σr² → finite pre-pass → stable sort +
percentile lerp → finite guard → SCAN LAST (drawdown; only on plain rows).
A bit1 (``python_required``) row is FULLY ZEROED — Python never reads it.

## Neumaier scope note (the −0.0-first seed/transition divergence)

``builtins.sum()`` starts at int 0 and consumes the first float OUTSIDE the
compensation loop (``0 + x₀``); the Rust port seeds ``f_result = x₀``. The
two are bit-identical for every finite ``x₀`` EXCEPT ``x₀ == -0.0``
(``0.0 + -0.0 == +0.0``). The fold's summands are non-negative squares, so
the divergence is outside the fold domain; helper Neumaier vectors here
therefore never START with ``-0.0`` (−0.0 ties are still exercised by the
percentile vectors, where the sort/lerp helpers legitimately see them).

## Regen law

Re-minting over an existing corpus is REFUSED without
``BRUTEX_REGEN_SWEEP_CORE_CORPUS=1`` (the preopen-golden precedent): the
committed capture is the oracle; a silent regen would let a defect re-mint
its own expectations.

Run from the repo root with the 3.12 venv::

    BRUTEX_REGEN_SWEEP_CORE_CORPUS=1 .venv/bin/python \
        scripts/dev/mint_sweep_core_fold_corpus.py
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import random
import struct
import sys
from pathlib import Path

#: The f64-exactness envelope (2^52) — mirrors
#: ``rust/fno-math/src/metrics.rs::MAX_EXACT_EQUITY_PAISA`` and the fold's
#: bit1 gate (design §2.3 step 4).
MAX_EXACT_EQUITY_PAISA = 1 << 52

#: i64 bounds for the narrow checks (design §2.3 step 3).
I64_MIN = -(1 << 63)
I64_MAX = (1 << 63) - 1

#: Flag bits (design §1.3 column law).
FLAG_ZERO_TRADE = 1
FLAG_PYTHON_REQUIRED = 2

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CORPUS_PATH = REPO_ROOT / "tests" / "golden" / "fixtures" / "sweep_core_fold_corpus.json"
SHA_PATH = CORPUS_PATH.with_suffix(".json.sha256")
RUST_VECTORS_PATH = REPO_ROOT / "rust" / "fno-math" / "src" / "sweep_core_vectors.rs"


def _moments(values: list[float]) -> tuple[float, float, float, float]:
    """Literal ``_accumulate_moments`` body (compute.py:136-152) — Pébay 2008.

    Token-for-token: the exact update expressions in the exact operand
    order, ``(n*n - 3*n + 3)`` in INTEGER then converted, m4 before m3
    before m2. Returns the raw ``(mean, m2, m3, m4)``.
    """
    n = 0
    mean = 0.0
    m2 = 0.0
    m3 = 0.0
    m4 = 0.0
    for x in values:
        n1 = n
        n += 1  # noqa: SIM113 — token-for-token port target; never restructured
        delta = x - mean
        delta_n = delta / n
        delta_n2 = delta_n * delta_n
        term1 = delta * delta_n * n1
        mean += delta_n
        m4 += term1 * delta_n2 * (n * n - 3 * n + 3) + 6 * delta_n2 * m2 - 4 * delta_n * m3
        m3 += term1 * delta_n * (n - 2) - 3 * delta_n * m2
        m2 += term1
    return mean, m2, m3, m4


def _percentile(sorted_values: list[float], pct: float) -> float:
    """Literal ``percentile`` body (compute.py:272-280) — both branches."""
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = (pct / 100.0) * (len(sorted_values) - 1)
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return sorted_values[lower]
    frac = rank - lower
    return sorted_values[lower] * (1.0 - frac) + sorted_values[upper] * frac


def _drawdown(
    n_bars: int, start: int, exits: list[tuple[int, int]]
) -> tuple[int, float, int, float]:
    """Literal ``_drawdown_stats`` scan (compute.py:313-337) over the scatter.

    The equity curve is ``equity[i] = start + Σ_{(idx,pnl): idx <= i} pnl``
    — the exact curve ``build_equity_curve`` materialises. Returns
    ``(max_dd_paisa, max_dd_pct*100, max_dd_bars, ulcer*100)``.
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


def _zero_row(flags: int) -> tuple[list[int], list[float], int]:
    """A fully-zeroed 15-int/9-float row with *flags* (bit0/bit1 rows)."""
    return [0] * 15, [0.0] * 9, flags


def fold_expected(
    trades: list[tuple[int, int, int, int, int]], n_bars: int, start: int
) -> tuple[list[int], list[float], int]:
    """The authoritative fold outputs for one stream — the §2.3 pipeline.

    ``trades`` entries are ``(pnl, mfe, mae, holding, exit_idx)``. Returns
    ``(ints ×15, floats ×9, flags)`` where ints/floats are the design
    §1.3 column law order.
    """
    if not trades:
        return _zero_row(FLAG_ZERO_TRADE)
    # ── integer folds (exact bigint; narrow-checked) — step 3 ──────────
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
    ints_head = [
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
    ]
    if any(v < I64_MIN or v > I64_MAX for v in ints_head):
        return _zero_row(FLAG_PYTHON_REQUIRED)
    # ── the 2^52 envelope gate — step 4 ────────────────────────────────
    magnitude = start
    for pnl, *_rest in trades:
        magnitude += abs(pnl)
    if magnitude > MAX_EXACT_EQUITY_PAISA:
        return _zero_row(FLAG_PYTHON_REQUIRED)
    # ── returns + float folds — steps 5-9 ──────────────────────────────
    returns = [pnl / start for pnl, *_rest in trades]
    mean, m2, m3, m4 = _moments(returns)
    downside = [r for r in returns if r < 0.0]
    downside_sum_sq = sum(r * r for r in downside)  # the REAL builtins.sum
    if any(not math.isfinite(r) for r in returns):
        return _zero_row(FLAG_PYTHON_REQUIRED)
    sorted_returns = sorted(returns)
    p95 = _percentile(sorted_returns, 95.0)
    p5 = _percentile(sorted_returns, 5.0)
    pre_scan = [mean, m2, m3, m4, float(downside_sum_sq), p95, p5]
    if any(not math.isfinite(v) for v in pre_scan):
        return _zero_row(FLAG_PYTHON_REQUIRED)
    # ── SCAN LAST — step 11 ────────────────────────────────────────────
    exits = [(exit_idx, pnl) for pnl, _mfe, _mae, _holding, exit_idx in trades]
    max_dd_paisa, max_dd_pct, max_dd_bars, ulcer = _drawdown(n_bars, start, exits)
    floats = [*pre_scan, max_dd_pct, ulcer]
    if any(not math.isfinite(v) for v in floats):
        return _zero_row(FLAG_PYTHON_REQUIRED)
    ints = [*ints_head, max_dd_paisa, max_dd_bars]
    return ints, floats, 0


def _ints_hex(ints: list[int]) -> str:
    """The 15 i64s as one ``'<q'``-packed LE hex string."""
    return struct.pack("<15q", *ints).hex()


def _floats_hex(floats: list[float]) -> str:
    """The 9 f64s as one ``'<d'``-packed LE hex string."""
    return struct.pack("<9d", *floats).hex()


def _f64_hex(value: float) -> str:
    """One f64 as ``'<d'`` LE hex."""
    return struct.pack("<d", value).hex()


def _hand_picked() -> list[tuple[str, int, int, list[tuple[int, int, int, int, int]]]]:
    """The hand-picked edge streams: ``(name, n_bars, start, trades)``."""
    e = MAX_EXACT_EQUITY_PAISA
    cases: list[tuple[str, int, int, list[tuple[int, int, int, int, int]]]] = [
        ("zero_trade", 5, 10_000, []),
        ("zero_trade_one_bar", 1, 1, []),
        ("single_win", 4, 100_000, [(500, 700, 100, 2, 3)]),
        ("single_loss", 4, 100_000, [(-500, 100, 700, 2, 3)]),
        ("single_scratch_zero_pnl", 4, 100_000, [(0, 50, 50, 1, 0)]),
        (
            "scratch_breaks_both_streaks",
            8,
            100_000,
            [
                (100, 100, 0, 1, 1),
                (100, 100, 0, 1, 2),
                (0, 0, 0, 1, 3),
                (-100, 0, 100, 1, 4),
                (-100, 0, 100, 1, 5),
                (0, 0, 0, 1, 6),
                (100, 100, 0, 1, 7),
            ],
        ),
        ("all_win_streak", 6, 50_000, [(10, 10, 0, 1, i) for i in range(6)]),
        ("all_loss_streak", 6, 50_000, [(-10, 0, 10, 1, i) for i in range(6)]),
        ("ruin_below_zero", 3, 1_000, [(-1_500, 0, 1_500, 2, 1)]),
        (
            "negative_net_pnl_f3_cell",
            5,
            100_000,
            [(-301, 0, 400, 2, 1), (100, 150, 0, 1, 3), (-100, 0, 150, 1, 4)],
        ),
        ("max_holding_tie", 6, 100_000, [(50, 60, 0, 3, 2), (-50, 0, 60, 3, 5)]),
        ("envelope_exact_2_52", 1, 1, [(e - 1, 0, 0, 1, 0)]),
        ("envelope_over_2_52_plus_1", 1, 2, [(e - 1, 0, 0, 1, 0)]),
        ("envelope_over_large_negative", 1, 1, [(-e, 0, e, 1, 0)]),
        (
            "sum_mfe_i64_narrow_overflow",
            4,
            1_000,
            [(1, 1 << 62, 0, 1, 0), (1, 1 << 62, 0, 1, 1), (1, 1 << 62, 0, 1, 2)],
        ),
        (
            "sum_mae_i64_narrow_overflow",
            4,
            1_000,
            [(-1, 0, 1 << 62, 1, 0), (-1, 0, 1 << 62, 1, 1), (-1, 0, 1 << 62, 1, 2)],
        ),
        (
            "multi_exit_same_bar",
            4,
            10_000,
            [(-30, 0, 30, 1, 1), (-20, 0, 20, 1, 1), (60, 60, 0, 1, 3)],
        ),
        ("exit_first_and_last_bar", 3, 10_000, [(-10, 0, 10, 1, 0), (10, 10, 0, 1, 2)]),
        ("drawdown_recovery_shape", 4, 1_000, [(-100, 0, 100, 1, 1), (200, 200, 0, 1, 3)]),
        ("nonpositive_peak_skips_pct", 3, 1_000, [(-1_100, 0, 1_100, 1, 0), (400, 400, 0, 1, 2)]),
        ("tiny_start_one_paisa", 3, 1, [(1, 1, 0, 1, 0), (-1, 0, 1, 1, 2)]),
        (
            "win_rate_two_thirds",
            6,
            100_000,
            [(300, 300, 0, 2, 1), (300, 350, 10, 2, 3), (-200, 0, 250, 2, 5)],
        ),
        (
            "identical_returns_zero_variance",
            5,
            100_000,
            [(250, 250, 0, 1, 1), (250, 250, 0, 1, 2), (250, 250, 0, 1, 3)],
        ),
        ("single_trade_percentile_single_element", 2, 77_000, [(123, 200, 10, 2, 1)]),
        ("two_trade_percentile_lerp", 3, 77_000, [(123, 200, 10, 1, 1), (-321, 0, 400, 1, 2)]),
        (
            "neumaier_mixed_magnitudes",
            3,
            1,
            [((1 << 51), 1 << 51, 0, 1, 0), (-1, 0, 1, 1, 1), (1, 1, 0, 1, 2)],
        ),
        (
            "deep_window_sparse_exits",
            480,
            1_000_000,
            [
                (5_000, 6_000, 500, 30, 100),
                (-7_500, 0, 8_000, 60, 300),
                (2_500, 3_000, 100, 15, 460),
            ],
        ),
    ]
    return cases


def _random_records(
    count: int,
) -> list[tuple[str, int, int, list[tuple[int, int, int, int, int]]]]:
    """Seeded random streams — varied depth/magnitude/sign mixes."""
    out: list[tuple[str, int, int, list[tuple[int, int, int, int, int]]]] = []
    for i in range(count):
        rng = random.Random(f"sweep-core-fold-corpus-{i}")  # noqa: S311 — deterministic corpus seeding, not crypto
        n_bars = rng.choice([1, 2, 3, 5, 8, 21, 55, 144, 377])
        start = rng.choice([1, 7, 100, 9_973, 1_000_000, 10**10, 1 << 40])
        magnitude = rng.choice([3, 97, 10_007, 10**7, 1 << 44])
        n_trades = rng.randint(1, 40)
        trades: list[tuple[int, int, int, int, int]] = []
        for _ in range(n_trades):
            pnl = rng.randint(-magnitude, magnitude)
            mfe = rng.randint(0, magnitude)
            mae = rng.randint(0, magnitude)
            holding = rng.randint(1, n_bars)
            exit_idx = rng.randint(0, n_bars - 1)
            trades.append((pnl, mfe, mae, holding, exit_idx))
        out.append((f"random_{i:03d}", n_bars, start, trades))
    return out


def _helper_vectors() -> list[dict[str, object]]:
    """Helper-level float vectors — the streams trades cannot reach.

    Each vector freezes the CPython-authoritative ``builtins.sum`` (the
    Neumaier fast path), the literal Pébay moments, and the two-branch
    percentile lerp over the SORTED values (Python's stable ``sorted``).
    Never −0.0-FIRST for the Neumaier column (module docstring).
    """
    denorm = 5e-324
    streams: list[tuple[str, list[float]]] = [
        ("empty", []),
        ("single", [0.25]),
        ("neumaier_classic_1e16", [1e16, 1.0, -1e16]),
        ("neumaier_alternating_huge_tiny", [1e16, 1e-16, -1e16, 1e-16, 1e16, 1e-16, -1e16]),
        ("neumaier_all_tiny_squares", [1e-160, 1e-160, 1e-160, 1e-160]),
        ("denormal_first", [denorm, denorm * 3, denorm * 7]),
        ("denormal_mixed_with_ones", [denorm, 1.0, denorm, -1.0, denorm]),
        ("negzero_ties_interior", [1.0, -0.0, 0.0, -0.0, 0.0, -1.0]),
        ("negzero_pair_tail", [2.0, 0.0, -0.0]),
        ("squares_domain_ascending", [1e-8, 2e-8, 4e-8, 1e-4, 0.25]),
        ("squares_domain_descending", [0.25, 1e-4, 4e-8, 2e-8, 1e-8]),
        ("cancellation_ladder", [1e15, -1e15, 1e14, -1e14, 1e13, -1e13, 1.5]),
        ("compensation_carry", [1.0, 1e-16, 1e-16, 1e-16, 1e-16]),
        ("large_uniform", [1e300, 1e300]),
        ("two_element_lerp", [3.0, 5.0]),
        ("twenty_ramp", [float(i) / 7.0 for i in range(20)]),
    ]
    for i in range(16):
        rng = random.Random(f"sweep-core-helper-{i}")  # noqa: S311 — deterministic corpus seeding, not crypto
        n = rng.randint(2, 64)
        scale = rng.choice([1e-12, 1e-3, 1.0, 1e6, 1e14])
        values = [rng.uniform(-1.0, 1.0) * scale for _ in range(n)]
        streams.append((f"helper_random_{i:02d}", values))
    vectors: list[dict[str, object]] = []
    for name, values in streams:
        mean, m2, m3, m4 = _moments(values)
        total = sum(values)  # the REAL builtins.sum — the Neumaier authority
        ordered = sorted(values)
        p95 = _percentile(ordered, 95.0) if values else 0.0
        p5 = _percentile(ordered, 5.0) if values else 0.0
        vectors.append(
            {
                "name": name,
                "values_hex": [_f64_hex(v) for v in values],
                "neumaier_hex": _f64_hex(float(total)),
                "moments_hex": struct.pack("<4d", mean, m2, m3, m4).hex(),
                "p95_hex": _f64_hex(p95),
                "p5_hex": _f64_hex(p5),
            }
        )
    return vectors


def _build_corpus() -> dict[str, object]:
    """Assemble the whole corpus document."""
    records: list[dict[str, object]] = []
    for name, n_bars, start, trades in [*_hand_picked(), *_random_records(160)]:
        ints, floats, flags = fold_expected(trades, n_bars, start)
        records.append(
            {
                "name": name,
                "n_bars": n_bars,
                "start_equity_paisa": start,
                "trades": [list(t) for t in trades],
                "ints_hex": _ints_hex(ints),
                "floats_hex": _floats_hex(floats),
                "flags": flags,
            }
        )
    return {
        "schema": "sweep-core-fold-corpus-v1",
        "minted_with": f"CPython {sys.version_info[0]}.{sys.version_info[1]}.{sys.version_info[2]}",
        "decision": "DEC-SWEEP-CORE-RUST-001",
        "records": records,
        "helper_vectors": _helper_vectors(),
    }


def _rust_literal_f64_bits(hex_le: str) -> str:
    """LE ``'<d'`` hex → a Rust ``u64`` bit literal."""
    return f"0x{int.from_bytes(bytes.fromhex(hex_le), 'little'):016x}"


def _emit_rust_vectors(corpus: dict[str, object]) -> str:
    """Render the in-crate ``#[cfg(test)]`` vector consts — same numbers."""
    lines: list[str] = [
        "// GENERATED by scripts/dev/mint_sweep_core_fold_corpus.py — DO NOT EDIT.",
        "// DEC-SWEEP-CORE-RUST-001 (MIGRATION.md #1739): the frozen-bytes fold",
        '// corpus as in-crate consts (design §1(a) "same numbers") — the',
        "// independent-oracle mandate's Rust arm. Expected floats are u64 BIT",
        "// patterns (f64::to_bits), compared exactly; a Pébay/Neumaier operand",
        "// mutant dies against these literals, never against a shared helper.",
        "#![allow(clippy::unreadable_literal)]",
        "",
        "/// One frozen fold stream: trades are `(pnl, mfe, mae, holding, exit_idx)`.",
        "pub(crate) struct FoldVector {",
        "    pub(crate) name: &'static str,",
        "    pub(crate) n_bars: usize,",
        "    pub(crate) start_equity_paisa: i64,",
        "    pub(crate) trades: &'static [(i64, i64, i64, u64, u64)],",
        "    pub(crate) expected_ints: [i64; 15],",
        "    pub(crate) expected_float_bits: [u64; 9],",
        "    pub(crate) expected_flags: u8,",
        "}",
        "",
        "/// One frozen helper-level float stream (Neumaier / moments / lerp).",
        "pub(crate) struct HelperVector {",
        "    pub(crate) name: &'static str,",
        "    pub(crate) value_bits: &'static [u64],",
        "    pub(crate) neumaier_bits: u64,",
        "    pub(crate) moments_bits: [u64; 4],",
        "    pub(crate) p95_bits: u64,",
        "    pub(crate) p5_bits: u64,",
        "}",
        "",
        "#[rustfmt::skip] // generated one-record-per-line layout — kept stable across mints",
        "pub(crate) static FOLD_VECTORS: &[FoldVector] = &[",
    ]
    records = corpus["records"]
    assert isinstance(records, list)
    for rec in records:
        ints = struct.unpack("<15q", bytes.fromhex(rec["ints_hex"]))
        float_bits = [f"0x{b:016x}" for b in struct.unpack("<9Q", bytes.fromhex(rec["floats_hex"]))]
        trades = ", ".join(f"({p}, {mfe}, {mae}, {h}, {x})" for p, mfe, mae, h, x in rec["trades"])
        ints_txt = ", ".join(str(v) for v in ints)
        floats_txt = ", ".join(float_bits)
        lines.append(
            f'    FoldVector {{ name: "{rec["name"]}", n_bars: {rec["n_bars"]}, '
            f"start_equity_paisa: {rec['start_equity_paisa']}, "
            f"trades: &[{trades}], expected_ints: [{ints_txt}], "
            f"expected_float_bits: [{floats_txt}], "
            f"expected_flags: {rec['flags']} }},"
        )
    lines.append("];")
    lines.append("")
    lines.append(
        "#[rustfmt::skip] // generated one-record-per-line layout — kept stable across mints"
    )
    lines.append("pub(crate) static HELPER_VECTORS: &[HelperVector] = &[")
    helpers = corpus["helper_vectors"]
    assert isinstance(helpers, list)
    for vec in helpers:
        value_bits = ", ".join(_rust_literal_f64_bits(h) for h in vec["values_hex"])
        moments_bits = ", ".join(
            f"0x{b:016x}" for b in struct.unpack("<4Q", bytes.fromhex(vec["moments_hex"]))
        )
        neumaier = _rust_literal_f64_bits(vec["neumaier_hex"])
        p95 = _rust_literal_f64_bits(vec["p95_hex"])
        p5 = _rust_literal_f64_bits(vec["p5_hex"])
        lines.append(
            f'    HelperVector {{ name: "{vec["name"]}", value_bits: &[{value_bits}], '
            f"neumaier_bits: {neumaier}, moments_bits: [{moments_bits}], "
            f"p95_bits: {p95}, p5_bits: {p5} }},"
        )
    lines.append("];")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    """Mint the corpus + the Rust vectors (regen-guarded)."""
    if sys.version_info[:2] != (3, 12):
        print(
            f"REFUSED: the minting authority is CPython 3.12 (builtins.sum "
            f"Neumaier, gh-100425); this is {sys.version}",
            file=sys.stderr,
        )
        return 1
    if CORPUS_PATH.exists() and os.environ.get("BRUTEX_REGEN_SWEEP_CORE_CORPUS") != "1":
        print(
            f"REFUSED: {CORPUS_PATH} exists — the committed capture is the "
            f"oracle. Set BRUTEX_REGEN_SWEEP_CORE_CORPUS=1 to consciously "
            f"re-mint.",
            file=sys.stderr,
        )
        return 1
    corpus = _build_corpus()
    payload = json.dumps(corpus, indent=1, sort_keys=True).encode() + b"\n"
    CORPUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    CORPUS_PATH.write_bytes(payload)
    SHA_PATH.write_text(hashlib.sha256(payload).hexdigest() + "\n", encoding="utf-8")
    RUST_VECTORS_PATH.write_text(_emit_rust_vectors(corpus), encoding="utf-8")
    records = corpus["records"]
    helpers = corpus["helper_vectors"]
    assert isinstance(records, list)
    assert isinstance(helpers, list)
    print(
        f"minted {len(records)} fold records + {len(helpers)} helper vectors\n"
        f"  {CORPUS_PATH}\n  {SHA_PATH}\n  {RUST_VECTORS_PATH}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
