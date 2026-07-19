"""Canonical catalog of the 24 Track 1 property invariants.

This file is the **single source of truth** for which of the P-01 ..
P-24 invariants (per ``docs/TRACK1_VERIFICATION_PYRAMID.md`` §"Property
tests — 24 invariants × 500 examples each") are implemented vs awaiting
Phase 2 implementation. CI lists this file and reports skip-counts per
invariant, so the verification pyramid's coverage is mechanically
visible without re-reading the spec.

## How this catalog works

Each ``test_p_NN_*`` function corresponds to exactly one invariant ID
from the spec. The function body is either:

* ``pytest.skip(...)`` with a reference to the file where the invariant
  is actually implemented (e.g., ``test_audit_chain_properties.py``);
  the catalog test passes via skip while CI knows the invariant is
  covered by a sibling file.
* ``pytest.skip("Phase 2 implementation pending")`` for invariants
  that have NOT yet been implemented anywhere; CI shows these as
  TODO without failing.

Per ``CLAUDE.md`` Golden Rule #10 (honest envelope), the catalog must
match reality: an invariant pointing to a non-existent test function
in a sibling file is a Golden Rule #1 hallucination and CI will catch
the lie via the catalog's own implementation-reference cross-check.

## Implementation status (verified 2026-06-07)

* ✅ IMPLEMENTED — invariant is covered by tests in another file in
  this directory. The catalog test ``skip``s with a pointer.
* 📋 ROADMAP — invariant awaits Phase 2 / Phase 3 implementation when
  the underlying subject (options-cost calculator, Rust parity
  harness, deterministic Parquet writer, etc.) lands. The catalog test
  ``skip``s with the phase tag.

DEC-PROPERTY-INVARIANTS-LIVE-001 (``docs/MIGRATION.md``) promoted five
ROADMAP stubs to live hypothesis tests against the real sandbox-safe
components (no :mod:`brutex.orchestrator` import): **P-03 / P-04 / P-09**
in ``test_engine_trade_sim_properties.py`` (driving
:func:`brutex.engine.trade_sim.simulate_trades` +
:func:`brutex.metrics.compute.compute_metrics`) and **P-13 / P-14** in
``test_indicator_properties.py`` (the EMA / pivot / Fib indicators).

DEC-LOOKAHEAD-BARRIER-001 (``docs/MIGRATION.md``) then promoted **P-06** to a
live hypothesis test in ``test_lookahead_properties.py``, driving the runtime
look-ahead enforcer :class:`brutex.feed.lookahead.LookAheadBarrier` (the
instrumented bar accessor): for any current bar index ``N``, every access index
``> N`` raises and every access ``≤ N`` is allowed.

DEC-PHASE2-INVARIANTS-PROMOTE-001 (``docs/MIGRATION.md``) promoted the
remaining sandbox-safe ROADMAP stubs to live Hypothesis properties in
``test_phase2_invariants_promoted.py``: **P-11 / P-24** (u64 mask AND algebra
+ worker-order/partition invariance), **P-19** (deterministic Parquet writer),
**P-21** (2-decimal tick snap — the stale "5 paise" claim corrected),
**P-22** (no NSE holiday is a trading day), **P-23** (IST session window), and
the two spec-named-but-absent invariants **P-25** (no VIX leak in the
vocabulary) and **P-26** (audit→dedup replay oracle). **P-08** points at the
existing golden associativity test. **P-20** (audit→Parquet byte-equality)
is LIVE per DEC-AUDIT-LAKE-REPLAY-ORACLE-001: the Hypothesis suite in
``test_replay_lake_properties.py`` captures bounded append schedules through
the real ``AuditCaptureSink`` → hash-chained audit log and replays them
through the REAL sink/writer stack into a fresh lake, asserting per-event
``raw_hash`` anchors + full byte equality (the end-to-end fixture pull lives
in ``tests/integration/test_replay_audit_log.py``).

DEC-TEST-APPARATUS-DEBTS-001(h) (``docs/MIGRATION.md``) then promoted the two
cost-model stubs to live Hypothesis properties over the REAL Track 2
calculator :func:`brutex.costs.calculator.compute_trade_cost` in
``test_cost_calculator_properties.py``: **P-10** (STT sell-leg-only, three
ways — buy-leg-price invariance + independent sell-notional recomputation
with a test-side half-up rounder + the stamp-duty buy-leg mirror) and
**P-12** (brokerage = the VERIFIED flat ₹20/order, ₹40 round-trip law +
the pairwise monotone-in-notional shape check; the old ``min(₹20, 0.03%
notional)`` docstring here was the stale equity-delivery formula —
CALCULATOR_SPEC §4.4 corrects it, and the calculator was left untouched).

Decision: DEC-PHASE1-INVARIANT-CATALOG-001 (MIGRATION entry #250).
"""

from __future__ import annotations

import pytest

# ────────────────────────────────────────────────────────────────────
# Group 6 — Dedup uniqueness (P-01, P-02, P-05, P-11, P-24)
# ────────────────────────────────────────────────────────────────────


def test_p_01_combo_hash_permutation_invariance() -> None:
    """P-01: ``combo_hash(C) == combo_hash(canonical(C))`` for any param permutation."""
    pytest.skip("implemented in tests/property/test_combo_hash_uniqueness.py")


def test_p_02_combo_hash_one_byte_change_propagates() -> None:
    """P-02: ``combo_hash`` changes if any single byte of the canonical input changes."""
    pytest.skip("implemented in tests/property/test_combo_hash_uniqueness.py")


def test_p_05_bloom_zero_false_negatives() -> None:
    """P-05: Bloom filter has zero false-negatives vs the LMDB backing store."""
    pytest.skip("implemented in tests/property/test_bloom_invariants.py")


def test_p_11_mask_and_commutativity() -> None:
    """P-11: Bitwise u64 mask AND on vocabulary is commutative & associative."""
    pytest.skip("implemented in tests/property/test_phase2_invariants_promoted.py")


def test_p_24_mask_and_independent_of_worker_order() -> None:
    """P-24: Bitmask AND result is independent of evaluation order across workers."""
    pytest.skip("implemented in tests/property/test_phase2_invariants_promoted.py")


# ────────────────────────────────────────────────────────────────────
# Group 4 — Correct PnL (P-03, P-04, P-08, P-09, P-13, P-14, P-21)
# ────────────────────────────────────────────────────────────────────


def test_p_03_per_trade_pnl_sums_to_balance_delta() -> None:
    """P-03: ``Σ per-trade paper_pnl == final_balance − initial_balance`` (exact paise)."""
    pytest.skip("implemented in tests/property/test_engine_trade_sim_properties.py")


def test_p_04_gross_minus_costs_equals_net() -> None:
    """P-04: ``gross_pnl − Σ costs == net_pnl`` (Track 1: costs=0, so gross == net)."""
    pytest.skip("implemented in tests/property/test_engine_trade_sim_properties.py")


def test_p_08_resample_associative() -> None:
    """P-08: ``resample(resample(x,2),3) ≡ resample(x,6)`` when 09:15 anchor aligns."""
    pytest.skip("implemented in tests/golden/test_resample_cross_instrument_golden.py")


def test_p_09_forced_exit_no_trade_past_1520() -> None:
    """P-09: Forced-exit invariant — no trade survives 15:20 IST."""
    pytest.skip("implemented in tests/property/test_engine_trade_sim_properties.py")


def test_p_13_ema_is_contraction() -> None:
    """P-13: ``EMA(α, x)`` is a contraction in the ``ℓ∞`` norm."""
    pytest.skip("implemented in tests/property/test_indicator_properties.py")


def test_p_14_pivot_fib_pure_functions() -> None:
    """P-14: Pivot/Fib levels are pure functions of ``(PDH, PDL, PDC)`` — no intraday state leak."""
    pytest.skip("implemented in tests/property/test_indicator_properties.py")


def test_p_21_tick_rounding() -> None:
    """P-21: Every persisted price snaps to the 2-decimal (1-paisa) tick grid
    via ``round_money`` (ROUND_HALF_UP) — DEC-SPOT-PRICE-2DP-001. (The earlier
    "multiple of 5 paise" wording was stale vs the live writer.)"""
    pytest.skip("implemented in tests/property/test_phase2_invariants_promoted.py")


# ────────────────────────────────────────────────────────────────────
# Group 3 — No look-ahead (P-06)
# ────────────────────────────────────────────────────────────────────


def test_p_06_no_lookahead_in_evaluate() -> None:
    """P-06: For any bar index N, ``evaluate(N)`` reads only indices ≤ N."""
    pytest.skip("implemented in tests/property/test_lookahead_properties.py")


# ────────────────────────────────────────────────────────────────────
# Phase 2 reserved — Track 2 + costs (P-07, P-15 pending; P-10, P-12
# promoted live by DEC-TEST-APPARATUS-DEBTS-001(h))
# ────────────────────────────────────────────────────────────────────


def test_p_07_pessimistic_fill_envelope() -> None:
    """P-07: Entry fill price ≥ bar.high (BUY) or ≤ bar.low (SELL) — pessimistic mode."""
    pytest.skip("Phase 2 implementation pending (pessimistic-fill simulator)")


def test_p_10_stt_sell_leg_only() -> None:
    """P-10: STT applied only on SELL leg (invariant in the buy-leg price +
    independent sell-notional recomputation + the stamp-duty buy-leg mirror)."""
    pytest.skip("implemented in tests/property/test_cost_calculator_properties.py")


def test_p_12_brokerage_monotone() -> None:
    """P-12: brokerage is flat ₹20 per executed order (₹40 round-trip),
    invariant in notional/lots — the degenerate flat case of the spec's
    monotone-until-floor claim; the ``min(₹20, 0.03% notional)`` wording was
    the stale equity formula (CALCULATOR_SPEC §4.4 corrects it)."""
    pytest.skip("implemented in tests/property/test_cost_calculator_properties.py")


def test_p_15_back_adjustment_multiplicative_invertible() -> None:
    """P-15: Continuous-futures back-adjustment is multiplicative & invertible."""
    pytest.skip("Phase 2 implementation pending (continuous-futures harness)")


# ────────────────────────────────────────────────────────────────────
# Group 7 — Audit chain (P-16, P-17)
# ────────────────────────────────────────────────────────────────────


def test_p_16_audit_chain_count_matches_appends() -> None:
    """P-16: ``verify_chain(file).count == #append() calls``."""
    pytest.skip("implemented in tests/property/test_audit_chain_properties.py")


def test_p_17_audit_single_byte_tamper_detected() -> None:
    """P-17: Any single-byte tamper on a JSONL audit line raises ``AuditChainCorruption``."""
    pytest.skip("implemented in tests/property/test_audit_chain_properties.py")


# ────────────────────────────────────────────────────────────────────
# Group 8 — Adapter + Microstructure (P-18, P-22, P-23)
# ────────────────────────────────────────────────────────────────────


def test_p_18_rate_limiter_holds_to_spec_rate() -> None:
    """P-18: the token-bucket rate limiter never exceeds the vendor cap
    (Dhan Data API: 5 r/s — DEC-GROWW-DHAN-REVERT-001, CLAUDE.md §6;
    the earlier "10 r/s Groww-tuned" wording was stale)."""
    pytest.skip("implemented in tests/property/test_rate_limiter_properties.py")


def test_p_22_no_bars_on_nse_holidays() -> None:
    """P-22: No bar timestamp on any NSE holiday 2021-2026."""
    pytest.skip("implemented in tests/property/test_phase2_invariants_promoted.py")


def test_p_23_session_window_respected() -> None:
    """P-23: Every persisted intraday bar has ``03:45 ≤ utc_minute_of_day ≤ 10:00`` UTC."""
    pytest.skip("implemented in tests/property/test_phase2_invariants_promoted.py")


# ────────────────────────────────────────────────────────────────────
# Group 2 — Reproducibility (P-19, P-20)
# ────────────────────────────────────────────────────────────────────


def test_p_19_parquet_writer_deterministic() -> None:
    """P-19: ``bytes(write(df)) == bytes(write(df))`` across two processes."""
    pytest.skip("implemented in tests/property/test_phase2_invariants_promoted.py")


def test_p_20_replay_byte_equals_original() -> None:
    """P-20: ``replay(audit_log) → Parquet`` byte-equals the original Parquet."""
    # DEC-AUDIT-LAKE-REPLAY-ORACLE-001: the capture→replay oracle is LIVE —
    # `AuditCaptureSink` records every non-empty sink append onto the
    # hash-chained audit log and `replay_lake_from_audit` re-drives the REAL
    # ParquetBarSink→ParquetWriter stack from the verified log alone into a
    # fresh lake, per-event raw_hash anchors + full byte equality asserted
    # (property suite below; end-to-end fixture pull in the integration file).
    pytest.skip("implemented in tests/property/test_replay_lake_properties.py")


# ────────────────────────────────────────────────────────────────────
# Group 9 — Vocabulary scope + replay oracle (P-25, P-26)
# Promoted from spec-named-but-absent to live (DEC-PHASE2-INVARIANTS-PROMOTE-001).
# ────────────────────────────────────────────────────────────────────


def test_p_25_no_vix_leak_in_vocabulary() -> None:
    """P-25: India-VIX (REFERENCE-ONLY, CLAUDE.md §2 Rule 3) never appears
    in the shared condition vocabulary, so it cannot leak into a pattern mask."""
    pytest.skip("implemented in tests/property/test_phase2_invariants_promoted.py")


def test_p_26_audit_replay_oracle_consistent() -> None:
    """P-26: replaying the ``add``-action audit log against the live dedup
    set diffs clean (idempotence/replay oracle — CLAUDE.md Rules #7/#8)."""
    pytest.skip("implemented in tests/property/test_phase2_invariants_promoted.py")
