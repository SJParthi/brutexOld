"""Golden byte-equality oracle for the persisted sweep runner.

Two pinned, hand-fixed oracles (DEC-SWEEP-RUN-001 / Golden Rule #4):

* **Run-identity byte-for-byte.** :func:`mask_run_id` is a pure BLAKE3 digest of
  its 9 canonical inputs — version-stable across polars / zstd (it touches
  neither). One mask + one fixed context + fixed params is pinned to its exact
  64-char hex.
* **End-to-end store content hash.** A fixed synthetic bar fixture run through
  :func:`run_persisted_sweep` (with a fixed ``ts_utc`` on every audit line, so
  the audit chain is deterministic) is pinned to:
  (a) a stable logical summary (enumerated / ranked / persisted), and
  (b) a byte-identical store across two independent runs (determinism), plus
  (c) the exact ``store_content_hash`` literal.

Mirrors the LOGICAL-first philosophy of
``tests/golden/test_engine_results_store_golden.py``: the determinism +
summary assertions are the version-robust contract; the literal content-hash
pin is the strongest form but is sensitive to the installed polars / zstd
build (same caveat the results-store golden documents) — a mismatch there with
the determinism assertions still green means a dependency bump, not a logic
regression.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.data.models import CandleBar
from brutex.data.option_dedup import OptionDedupIndex
from brutex.engine.results_store import ResultsStore
from brutex.engine.rust_trade_sim import ResidentTradeSim
from brutex.engine.sweep_run import (
    ComboDedup,
    SweepRunConfig,
    SweepRunSummary,
    mask_run_id,
    run_persisted_sweep,
)
from brutex.orchestrator.audit import AuditWriter
from brutex.strategies.types import Side
from structlog.testing import capture_logs

_IST = timezone(timedelta(hours=5, minutes=30))

#: A fixed audit timestamp so the audit chain hashes deterministically.
_FIXED_TS_UTC = 1_700_000_000.0

#: The pinned 9-field context for the run-identity oracle.
_CTX: dict[str, str] = {
    "direction": "bull",
    "instrument": "NIFTY",
    "tf": "1m",
    "mode": "A",
    "data_snapshot_hash": "snap0001",
    "indicator_version": "ind-v1",
    "code_commit_sha": "deadbeef",
}
_PARAMS: dict[str, object] = {
    "sl_paisa": 80,
    "tp_paisa": 50,
    "prior_n": 3,
    "proximity_bps": None,
    # DEC-NIFTY-FIB-AXIS-001: the swept Fib anchor is now folded into the params
    # layout (its string enum value); the default prev-day sweep carries this key
    # UNCONDITIONALLY, so the persisted run-identity matches this oracle dict.
    "fib_anchor": "prev_day",
    "start_equity_paisa": 10_000_000,
    # DEC-FILL-WORST-CASE-ONLY-001: canonical_trade_params emits the
    # fill_mode key UNCONDITIONALLY — worst-case is the ONLY fill model, and
    # the permanent token keeps every run identity off the precise-era bytes.
    "fill_mode": "worst_case",
}


def _bars() -> list[CandleBar]:
    """The fixed synthetic 12-day GAP-UP fixture (deterministic OHLC).

    RE-CRAFTED for DEC-FILL-WORST-CASE-ONLY-001: the pre-DEC upward-drift
    fixture's wide-range bars stop out every worst-case entry on its own bar
    (its worst-case store was EMPTY — a toothless golden). This shape RANKS:
    7-bar cycles of NARROW gap-up bars (open = prior close + 3.00, range 0.50
    < the SL 0.80; the next gap-up's low clears the TP level so a win banks
    exactly +50), a shallow bearish bar at cycle position 3, and a DEEP
    bearish bar (range 0.85 > the SL) every 4th cycle whose stop-out keeps
    profit_factor finite — all 5 masks rank + persist (5 finalists, a
    NON-EMPTY store hash with teeth).
    """
    out: list[CandleBar] = []
    cycle = 0
    for d in range(12):
        price = 100.0
        for m in range(120):
            k = m % 7
            if k == 0:
                cycle += 1
            if k == 3:  # shallow bearish bar
                o = price
                c, h, lo = o - 0.3, o + 0.1, o - 0.4
            elif k == 4 and cycle % 4 == 0:  # deep bearish loss carrier
                o = price
                c, h, lo = o - 0.65, o + 0.1, o - 0.75
            else:  # narrow gap-up win bar
                o = price + 3.0
                c, h, lo = o + 0.3, o + 0.4, o - 0.1
            ts = (
                datetime(2026, 1, 5, 9, 16, tzinfo=_IST) + timedelta(days=d, minutes=m)
            ).astimezone(UTC)
            out.append(
                CandleBar(
                    timestamp=ts,
                    open=Decimal(str(round(o, 2))),
                    high=Decimal(str(round(h, 2))),
                    low=Decimal(str(round(lo, 2))),
                    close=Decimal(str(round(c, 2))),
                    volume=100,
                    open_interest=None,
                )
            )
            price = c
    return out


def _config() -> SweepRunConfig:
    # THE goldens pin the ONE worst-case fill law (DEC-FILL-WORST-CASE-ONLY-001
    # re-lock): the precise-era `_GOLDEN_RUN_ID f588…921a` /
    # `_GOLDEN_CONTENT_HASH 18129707…ca8b` constants are RETIRED (kept below
    # as the frozen anti-alias literals) — worst-case is the only fill model,
    # `SweepRunConfig` carries no mode field, and the constants below were
    # regenerated on THIS tree over the rankable gap-up fixture.
    return SweepRunConfig(
        instrument="NIFTY",
        tf="1m",
        direction=Side.BUY,
        mode="A",
        condition_bits=[0, 1, 2, 3, 4],
        k=1,
        data_snapshot_hash="snap0001",
        indicator_version="ind-v1",
        code_commit_sha="deadbeef",
        tf_minutes=1,
        sl_paisa=80,
        tp_paisa=50,
    )


def _run(tmp_path: Path, tag: str) -> SweepRunSummary:
    store = ResultsStore(path=tmp_path / f"{tag}.parquet")
    dedup = ComboDedup(OptionDedupIndex(capacity=10_000, fpr_target=1e-6))
    audit = AuditWriter(path=tmp_path / f"{tag}.jsonl")
    with audit:
        # The audit timestamps are not part of the store content hash; the fixed
        # ts only makes the audit JSONL chain reproducible (used by the integration
        # test). run_persisted_sweep stamps time.time() internally, so we patch the
        # writer to a fixed ts via a thin subclass-free monkeypatch is unnecessary
        # here — the store hash is independent of audit timing.
        return run_persisted_sweep(_bars(), _config(), store=store, dedup=dedup, audit=audit)


# ────── Pinned run-identity ──────

#: The hand-pinned BLAKE3 run-identity of mask 0b101 under the fixed context.
#: Re-locked for DEC-VOCAB-U128-CODEGEN-001 (mask ``%032x``), AGAIN for
#: DEC-NIFTY-FIB-AXIS-001 (the ``fib_anchor`` params key), and AGAIN for
#: DEC-FILL-WORST-CASE-ONLY-001: the params layout carries the UNCONDITIONAL
#: ``fill_mode: worst_case`` token, so THE run identity IS #1522's worst-case
#: identity (`715212…62fc9` — byte-identical to the retired
#: `_WORST_CASE_GOLDEN_RUN_ID`, the anti-aliasing property: keeping the key
#: emitted keeps the pin byte-valid) and the precise-era identity below is
#: permanently unreachable.
_GOLDEN_RUN_ID = "715212420d2cca2dd998aa0f716ef5b658d551a212a61af135fe0cf6fae62fc9"

#: FROZEN HISTORICAL (anti-alias pin only — the retired precise-era identity;
#: minted pre-DEC when the params carried NO fill_mode key). No production
#: run may ever reproduce it again.
_PRECISE_ERA_RUN_ID = "f588194f3f107bec68ee48975171041dd77c8f5ceec7e0625534b352d2d5921a"


def test_golden_run_id_never_aliases_the_precise_era() -> None:
    # DEC-FILL-WORST-CASE-ONLY-001 anti-aliasing pin: dropping the permanent
    # key would collapse the one-law identity back onto the precise-era
    # digest — this pin makes that regression loud.
    assert _GOLDEN_RUN_ID != _PRECISE_ERA_RUN_ID
    assert mask_run_id(mask=0b101, params=_PARAMS, **_CTX) != _PRECISE_ERA_RUN_ID


def test_golden_run_id_is_byte_stable() -> None:
    assert mask_run_id(mask=0b101, params=_PARAMS, **_CTX) == _GOLDEN_RUN_ID


# ────── Pinned end-to-end summary + determinism + content hash ──────

#: The hand-pinned store content hash for the fixed fixture (polars/zstd-build
#: sensitive — see the module docstring; the determinism + summary assertions
#: are the version-robust contract).
# Re-lock history: DEC-SWEEP-WINNERS-BY-NAME-001 (readable-mask columns) →
# DEC-VOCAB-U128-CODEGEN-001 (two Int64 mask limbs + %032x run ids) →
# DEC-NIFTY-FIB-AXIS-001 (the fib_anchor params key) → the R2-OC-2
# overnight-entry-leak fix → DEC-VIX-STAMP-WIRE-001 (the three appended
# nullable vix columns) → **DEC-FILL-WORST-CASE-ONLY-001 (THIS re-lock)**:
# worst-case is the ONLY fill law and the retired precise-era hash
# (`18129707…ca8b`) is unreproducible by construction; the fixture was
# RE-CRAFTED to the rankable gap-up shape (see `_bars`) so the one-law store
# is NON-EMPTY (5 finalists — the pre-DEC worst-case golden's empty-store
# hash `cadb06a3…3a9f` was toothless), and this literal was regenerated on
# THIS tree (2026-07-15; determinism pinned by the two-run byte-equality
# test below; mask 0b101's stream teeth: 203 trades = 181 × +50 TP wins +
# 22 × −115 deep-bar stop-outs → net +6_520 — hand-derived in
# `test_golden_trade_stream_exact`).
# ── W2 FOLD — R2 RE-DERIVE TODO (DO NOT hand-edit; census obligation (b),
# MIGRATION.md #1976) ─────────────────────────────────────────────────────
# This literal is MAIN's worst-case hash WITHOUT the W2 lane's appended
# columns. On this branch the persisted schema additionally carries the
# growth trio (loss_rate/kelly_fraction/log_growth_at_half_kelly) +
# sl_hit_rate (EXIT_QUALITY_FIELDS) + the FOUR recency columns
# (RECENCY_FIELDS) — so this hash MUST FAIL here until R2 regenerates it on
# the folded tree WITH a fresh in-file hand-audit under worst-case
# semantics (row-set / run_id stability re-verified, per-column values
# re-audited; the pre-fold audit prose was derived under PRE-worst-case
# fills and is HISTORY, not the folded truth).
_GOLDEN_CONTENT_HASH = "ee874356dcf6d81cf290eb7d8d7e0d516b668cfcf665c45fca94127ecd4c0789"


def test_golden_summary_is_stable(tmp_path: Path) -> None:
    pytest.importorskip("polars")  # flush needs the [data] extra; skips the .[dev]-only job
    summary = _run(tmp_path, "a")
    assert summary.enumerated == 5
    assert summary.conflict_pruned == 0
    assert summary.skipped_dedup == 0
    assert summary.evaluated == 5
    assert summary.ranked == 5
    assert summary.persisted == 5


def test_golden_store_is_deterministic_across_runs(tmp_path: Path) -> None:
    pytest.importorskip("polars")  # flush needs the [data] extra; skips the .[dev]-only job
    a = _run(tmp_path, "a")
    b = _run(tmp_path, "b")
    assert a.store_content_hash == b.store_content_hash
    assert (tmp_path / "a.parquet").read_bytes() == (tmp_path / "b.parquet").read_bytes()


def test_golden_content_hash_literal(tmp_path: Path) -> None:
    pytest.importorskip("polars")  # flush needs the [data] extra; skips the .[dev]-only job
    summary = _run(tmp_path, "a")
    assert summary.store_content_hash == _GOLDEN_CONTENT_HASH


# ────── DEC-FILL-WORST-CASE-ONLY-001 — the one-law identity + stream teeth ──────


def _canonical_params() -> dict[str, object]:
    from brutex.engine.sweep_run import canonical_trade_params

    return canonical_trade_params(
        sl_paisa=80,
        tp_paisa=50,
        prior_n=3,
        proximity=None,
        start_equity_paisa=10_000_000,
    )


def test_worst_case_params_add_exactly_the_one_key() -> None:
    # REWRITTEN for DEC-FILL-WORST-CASE-ONLY-001 (was the emit-on-non-PRECISE
    # law): canonical_trade_params emits the permanent ``fill_mode:
    # worst_case`` key UNCONDITIONALLY — no caller can vary or elide it, and
    # popping it yields exactly the legacy params layout (nothing else moved).
    params = _canonical_params()
    assert params == _PARAMS  # the oracle dict INCLUDES the permanent key
    legacy = dict(params)
    popped = legacy.pop("fill_mode")
    assert popped == "worst_case"
    assert "fill_mode" not in legacy


def test_fill_mode_emitted_for_every_leg_shape() -> None:
    # The unconditional law is leg-shape-independent: SL-only / TP-only /
    # EOD-only params all carry the same permanent token.
    from brutex.engine.sweep_run import canonical_trade_params

    for sl, tp in ((80, None), (None, 50), (None, None)):
        params = canonical_trade_params(
            sl_paisa=sl,
            tp_paisa=tp,
            prior_n=3,
            proximity=None,
            start_equity_paisa=10_000_000,
        )
        assert params["fill_mode"] == "worst_case"


def test_golden_trade_stream_exact() -> None:
    # The numeric TEETH behind the store hash: mask 0b101's worst-case
    # per-trade stream over the fixture, pinned exactly at
    # head/tail/aggregate. HAND-DERIVATION: a win enters at a narrow gap-up
    # bar's HIGH (open + 0.40) and the NEXT gap-up's low (prior close +
    # 2.90) clears the TP level (entry + 0.50) → the fill is AT level = +50
    # paisa; a loss enters at the shallow bearish bar's HIGH (its open o +
    # 0.10, stop floor o − 0.70) and the DEEP bar's low (o − 0.30 − 0.75 =
    # o − 1.05) breaches it → the adverse fill min(level, low) = o − 1.05 →
    # −115 paisa. 181 wins × 50 − 22 losses × 115 = 9_050 − 2_530 = +6_520.
    from brutex.engine.combo_mask import mask_to_entry_signals
    from brutex.engine.stage1a import build_bar_bits
    from brutex.engine.trade_sim import simulate_trades

    bars = _bars()
    signals = mask_to_entry_signals(build_bar_bits(bars), 0b101)
    stream = [
        (t.pnl_paisa, t.mfe_paisa, t.mae_paisa, t.holding_bars)
        for t in simulate_trades(
            bars,
            signals,
            direction=Side.BUY,
            tf_minutes=1,
            stop_loss_paisa=80,
            take_profit_paisa=50,
        )
    ]
    assert len(stream) == 203
    assert sum(t[0] for t in stream) == 6_520
    assert sum(1 for t in stream if t[0] == 50) == 181
    assert sum(1 for t in stream if t[0] == -115) == 22
    assert stream[0] == (50, 300, 50, 2)
    assert stream[-1] == (50, 330, 50, 2)


# ────── DEC-SWEEP-RUST-TRADESIM-WIRE-001 — wired-vs-unwired bit-identity ──────
#
# `_GOLDEN_CONTENT_HASH` above was generated on the PURE-PYTHON per-mask loop
# (pre-wiring). The wiring PR deliberately leaves BOTH literals UNCHANGED — the
# wired (Rust-kernel) run reproducing the Python-era hash IS the
# wired-vs-unwired proof at the strongest (byte) level. These tests make the
# two arms explicit: the wired arm ASSERTS the kernel actually engaged (so the
# gate can never silently degenerate to Python==Python), and the forced-Python
# arm asserts the same bytes.


def _forced_fallback_sim(
    bars: Sequence[CandleBar],
    bar_bits: Sequence[int],
    *,
    tf_minutes: int,
    reference_stamper: object = None,
    precomputed_columns: object = None,
) -> ResidentTradeSim:
    """ResidentTradeSim with the compiled kernel DISABLED — the pure-Python arm.

    The same instance-level ``_rust = None`` forcing seam the rust_trade_sim
    fallback tests use, applied over ``sweep_run.ResidentTradeSim`` (as a
    ctor-signature FACTORY, so no subclass ``__init__`` overwrites the
    superclass attribute) so the WHOLE persisted sweep runs the oracle.
    ``precomputed_columns`` tracks the additive W2-rider kwarg
    (DEC-STAGE1A-RUST-BATCH-001) — signature glue only; every pinned literal
    in this module is byte-untouched.
    """
    sim = ResidentTradeSim(
        bars,
        bar_bits,
        tf_minutes=tf_minutes,
        reference_stamper=reference_stamper,  # type: ignore[arg-type]
        precomputed_columns=precomputed_columns,  # type: ignore[arg-type]
    )
    sim._rust = None  # pyright: ignore[reportPrivateUsage] — the DEC-P0-017 friend seam
    return sim


def test_golden_wired_arm_engages_rust_and_reproduces_the_python_era_hash(
    tmp_path: Path,
) -> None:
    """The WIRED sweep takes the Rust path AND lands on the pre-wiring hash.

    Without the rust_active assertion a stale/broken wheel would silently
    revert the sweep to the Python walker — bit-identical results, so every
    golden stays green while proving nothing about the kernel (the vacuous-gate
    trap; Golden Rule #10).
    """
    pytest.importorskip("polars")  # flush needs the [data] extra; skips the .[dev]-only job
    with capture_logs() as logs:
        summary = _run(tmp_path, "a")
    events = [e for e in logs if e["event"] == "sweep_run.trade_sim_path"]
    assert len(events) == 1
    assert events[0]["rust_active"] is True  # the wheel is built in CI — Rust MUST engage
    assert summary.store_content_hash == _GOLDEN_CONTENT_HASH


def test_golden_forced_python_fallback_is_byte_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The forced-Python arm reproduces the SAME pinned hash + store bytes."""
    pytest.importorskip("polars")  # flush needs the [data] extra; skips the .[dev]-only job
    wired = _run(tmp_path, "a")
    monkeypatch.setattr("brutex.engine.sweep_run.ResidentTradeSim", _forced_fallback_sim)
    fallback = _run(tmp_path, "b")
    assert wired.store_content_hash == _GOLDEN_CONTENT_HASH
    assert fallback.store_content_hash == _GOLDEN_CONTENT_HASH
    assert (tmp_path / "a.parquet").read_bytes() == (tmp_path / "b.parquet").read_bytes()


def test_golden_sell_direction_wired_equals_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SELL-direction arm: wired vs forced-Python byte equality.

    The pinned fixture above is BUY-only while production sweeps bull AND bear
    — direction feeds the kernel's ``is_buy`` flag, so the bear arm gets its
    own wired-vs-unwired equality gate (no pinned literal needed: equality
    between the arms IS the contract).
    """
    pytest.importorskip("polars")  # flush needs the [data] extra; skips the .[dev]-only job
    cfg = replace(_config(), direction=Side.SELL)

    def _run_sell(tag: str) -> SweepRunSummary:
        store = ResultsStore(path=tmp_path / f"{tag}.parquet")
        dedup = ComboDedup(OptionDedupIndex(capacity=10_000, fpr_target=1e-6))
        with AuditWriter(path=tmp_path / f"{tag}.jsonl") as audit:
            return run_persisted_sweep(_bars(), cfg, store=store, dedup=dedup, audit=audit)

    with capture_logs() as logs:
        wired = _run_sell("sell_wired")
    events = [e for e in logs if e["event"] == "sweep_run.trade_sim_path"]
    assert events[0]["rust_active"] is True
    monkeypatch.setattr("brutex.engine.sweep_run.ResidentTradeSim", _forced_fallback_sim)
    fallback = _run_sell("sell_fallback")
    assert wired.store_content_hash == fallback.store_content_hash
    assert (tmp_path / "sell_wired.parquet").read_bytes() == (
        tmp_path / "sell_fallback.parquet"
    ).read_bytes()


# ────── DEC-SWEEP-RUST-METRICS-001 — Rust-metrics-tail dual-arm gates ──────
#
# The Phase-B flag (`BRUTEX_SWEEP_RUST_METRICS`) swaps the per-mask drawdown
# scan onto the fused Rust kernel — and since ADR-0017
# (DEC-SWEEP-RUST-METRICS-DEFAULT-ON-001) the DEFAULT arm IS the kernel, so
# every no-env test in this file now runs it. The pinned literals above were
# generated on the pure-Python tail and stay UNCHANGED — the default/ON arm
# reproducing them IS the dual-arm proof at the strongest (byte) level; the
# Python arm is pinned via the explicit "0" kill switch where a reference is
# needed.


def test_golden_default_arm_engages_the_kernel_and_reproduces_the_hash(
    tmp_path: Path,
) -> None:
    """THE ADR-0017 flip gate: with NO env set, the sweep runs the Rust
    metrics kernel (exactly one engaged event — the anti-vacuous-gate
    signal) AND reproduces the frozen pre-kernel store hash byte-for-byte.
    If this ever fails on the hash side, the flip must be reverted (the one
    SWEEP_RUST_METRICS_DEFAULT constant) — parity is the flip's licence."""
    pytest.importorskip("polars")  # flush needs the [data] extra; skips the .[dev]-only job
    import os

    from brutex.engine.rust_metrics import (
        reset_observability_memo_for_tests,
        reset_self_check_for_tests,
    )

    assert os.environ.get("BRUTEX_SWEEP_RUST_METRICS") is None
    reset_observability_memo_for_tests()
    reset_self_check_for_tests()
    with capture_logs() as logs:
        summary = _run(tmp_path, "a")
    engaged = [e for e in logs if e["event"] == "sweep_rust_metrics.engaged"]
    assert len(engaged) == 1  # the DEFAULT arm genuinely served masks from Rust
    assert not [e for e in logs if e["event"] == "sweep_rust_metrics.kernel_autoreverted"]
    assert summary.store_content_hash == _GOLDEN_CONTENT_HASH


def test_golden_rust_metrics_on_arm_is_byte_identical_and_engages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Flag ON reproduces the Python-era hash AND genuinely runs the kernel.

    Without the engaged assertion a stale wheel would silently serve Python
    on both arms — bit-identical results proving nothing about the kernel
    (the vacuous-gate trap, Golden Rule #10)."""
    pytest.importorskip("polars")  # flush needs the [data] extra; skips the .[dev]-only job
    from brutex.engine.rust_metrics import (
        reset_observability_memo_for_tests,
        reset_self_check_for_tests,
    )

    monkeypatch.setenv("BRUTEX_SWEEP_RUST_METRICS", "0")
    wired = _run(tmp_path, "a")  # explicit OFF — the reference bytes
    monkeypatch.setenv("BRUTEX_SWEEP_RUST_METRICS", "1")
    reset_observability_memo_for_tests()
    reset_self_check_for_tests()
    with capture_logs() as logs:
        rust_arm = _run(tmp_path, "b")
    engaged = [e for e in logs if e["event"] == "sweep_rust_metrics.engaged"]
    assert len(engaged) == 1  # the metrics kernel actually served masks
    assert wired.store_content_hash == _GOLDEN_CONTENT_HASH
    assert rust_arm.store_content_hash == _GOLDEN_CONTENT_HASH
    # The whole summary tally set matches across arms, not just the hash.
    assert (rust_arm.enumerated, rust_arm.evaluated, rust_arm.ranked, rust_arm.persisted) == (
        wired.enumerated,
        wired.evaluated,
        wired.ranked,
        wired.persisted,
    )
    assert rust_arm.skipped_dedup == wired.skipped_dedup
    assert rust_arm.conflict_pruned == wired.conflict_pruned
    assert (tmp_path / "a.parquet").read_bytes() == (tmp_path / "b.parquet").read_bytes()


def test_golden_rust_metrics_domain_invalid_poison_is_refused_loudly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A domain-invalid kernel result is REFUSED at runtime — the sweep lands
    on the pinned Python-era bytes (never adopts the poison) and says so."""
    pytest.importorskip("polars")  # flush needs the [data] extra; skips the .[dev]-only job
    import math

    import fno_math
    from brutex.engine.rust_metrics import (
        reset_observability_memo_for_tests,
        reset_self_check_for_tests,
    )

    monkeypatch.setenv("BRUTEX_SWEEP_RUST_METRICS", "1")
    monkeypatch.setattr(
        fno_math,
        "sweep_drawdown_stats",
        lambda n_bars, start, exits: (0, math.nan, 0, 0.0),
    )
    reset_observability_memo_for_tests()
    reset_self_check_for_tests()
    with capture_logs() as logs:
        summary = _run(tmp_path, "a")
    refused = [e for e in logs if e["event"] == "sweep_rust_metrics.kernel_result_refused"]
    assert len(refused) == 1
    assert summary.store_content_hash == _GOLDEN_CONTENT_HASH


def test_golden_rust_metrics_domain_valid_poison_moves_the_hash_past_burn_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The inverted engagement teeth: PAST the burn-in self-check, a
    domain-VALID poisoned kernel result flows into the persisted rows, so
    the store hash MOVES off the pinned literal — proving the ON arm
    consumes the kernel output end-to-end (were the seam secretly
    recomputing in Python, this test would fail). The burn-in budget is
    explicitly exhausted first — WITH samples pending the same poison is
    caught + autoreverted (the sibling test below)."""
    pytest.importorskip("polars")  # flush needs the [data] extra; skips the .[dev]-only job
    import fno_math
    from brutex.engine.rust_metrics import (
        reset_observability_memo_for_tests,
        reset_self_check_for_tests,
    )

    monkeypatch.setenv("BRUTEX_SWEEP_RUST_METRICS", "1")
    monkeypatch.setattr(
        fno_math,
        "sweep_drawdown_stats",
        lambda n_bars, start, exits: (77_777, 61.803, 3, 27.182),
    )
    reset_observability_memo_for_tests()
    reset_self_check_for_tests(remaining=0)
    summary = _run(tmp_path, "a")
    assert summary.store_content_hash != _GOLDEN_CONTENT_HASH
    reset_self_check_for_tests()  # never leak the exhausted budget to siblings


def test_golden_rust_metrics_domain_valid_poison_in_burn_in_is_autoreverted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DURING the burn-in, the SAME domain-valid poison is caught by the
    kernel-vs-oracle bit-compare: the sweep lands on the pinned Python-era
    bytes (the oracle value ships — the poison never persists), one
    kernel_autoreverted ERROR fires, and the kernel stays latched OFF
    (DEC-SWEEP-EXIT-INDEX-FASTPATH-001 — the default-ON safety net)."""
    pytest.importorskip("polars")  # flush needs the [data] extra; skips the .[dev]-only job
    import fno_math
    from brutex.engine.rust_metrics import (
        reset_observability_memo_for_tests,
        reset_self_check_for_tests,
    )

    monkeypatch.setenv("BRUTEX_SWEEP_RUST_METRICS", "1")
    monkeypatch.setattr(
        fno_math,
        "sweep_drawdown_stats",
        lambda n_bars, start, exits: (77_777, 61.803, 3, 27.182),
    )
    reset_observability_memo_for_tests()
    reset_self_check_for_tests()
    with capture_logs() as logs:
        summary = _run(tmp_path, "a")
    reverted = [e for e in logs if e["event"] == "sweep_rust_metrics.kernel_autoreverted"]
    assert len(reverted) == 1
    assert reverted[0]["reason"] == "self_check_mismatch"
    assert summary.store_content_hash == _GOLDEN_CONTENT_HASH
    reset_self_check_for_tests()  # never leak the latch to sibling tests
