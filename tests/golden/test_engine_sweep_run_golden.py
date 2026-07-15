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
}


def _bars() -> list[CandleBar]:
    """The fixed synthetic 12-day upward-drift fixture (deterministic OHLC)."""
    out: list[CandleBar] = []
    for d in range(12):
        price = 100.0
        for m in range(120):
            close = price + 0.6 if m % 3 else price - 0.3
            ts = (
                datetime(2026, 1, 5, 9, 16, tzinfo=_IST) + timedelta(days=d, minutes=m)
            ).astimezone(UTC)
            hi = max(price, close) + 0.5
            lo = min(price, close) - 0.5
            out.append(
                CandleBar(
                    timestamp=ts,
                    open=Decimal(str(round(price, 2))),
                    high=Decimal(str(round(hi, 2))),
                    low=Decimal(str(round(lo, 2))),
                    close=Decimal(str(round(close, 2))),
                    volume=100,
                    open_interest=None,
                )
            )
            price = close
    return out


def _config() -> SweepRunConfig:
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
#: Re-locked for DEC-VOCAB-U128-CODEGEN-001: the run-id encodes the mask as
#: ``mask:%032x`` (u128, 32 hex) instead of ``%016x`` (u64, 16 hex), so the
#: BLAKE3 input — and thus the digest — moved by design. Re-locked AGAIN for
#: DEC-NIFTY-FIB-AXIS-001: the params layout gained the ``fib_anchor`` key (its
#: string enum value), so the digest moved by design.
_GOLDEN_RUN_ID = "f588194f3f107bec68ee48975171041dd77c8f5ceec7e0625534b352d2d5921a"


def test_golden_run_id_is_byte_stable() -> None:
    assert mask_run_id(mask=0b101, params=_PARAMS, **_CTX) == _GOLDEN_RUN_ID


# ────── Pinned end-to-end summary + determinism + content hash ──────

#: The hand-pinned store content hash for the fixed fixture (polars/zstd-build
#: sensitive — see the module docstring; the determinism + summary assertions
#: are the version-robust contract).
# Re-locked for DEC-SWEEP-WINNERS-BY-NAME-001: the readable-mask surface added the
# mask_bits + condition_names + (nullable) deflated_sharpe columns to the persisted
# Parquet schema, so the deterministic content hash moved by design. Re-locked AGAIN
# for DEC-VOCAB-U128-CODEGEN-001: the u128 mask is persisted as two Int64 limb
# columns (mask_bits_lo/mask_bits_hi) replacing the single mask_bits Int64 column,
# and the run-id encoding widened to %032x — both move the deterministic hash.
# Re-locked AGAIN for DEC-NIFTY-FIB-AXIS-001: the ``fib_anchor`` key entered the
# params layout, so each persisted row's run_id (and thus the store bytes) moved.
# Re-locked AGAIN for the R2-OC-2 overnight-entry-leak fix: this fixture's 12
# synthetic days each end at 11:15 IST (never reaching a forced-exit bar), so a
# mask firing on a day's LAST bar previously filled at the NEXT day's 09:16 open
# — a cross-session overnight entry (audited pre-fix: 11+9+2 such fills on masks
# 1<<0 / 1<<2 / 1<<4, every one an instant −80 stop-loss on the daily gap; masks
# 1<<1 / 1<<3 had none and are byte-identical). Dropping those fills changes the
# trade streams → per-mask metrics → one more mask passes the rank gates
# (ranked/persisted 4 → 5) and the store bytes move BY DESIGN.
# Re-locked AGAIN for DEC-VIX-STAMP-WIRE-001 (Stage C): the persisted schema
# gained the three appended nullable REFERENCE-ONLY vix_mean/vix_min/vix_max
# columns (all null on this UNSTAMPED fixture — the honest unknown), so the
# deterministic byte stream moved by design. Every row VALUE, every run_id and
# the summary tally are unchanged (pinned by test_golden_summary_is_stable +
# test_golden_run_id_is_byte_stable; the pre-column layout itself stays pinned
# by tests/golden/test_results_store_backcompat_golden.py's frozen fixture).
_GOLDEN_CONTENT_HASH = "18129707c1bf493120668dd9d3645e9e24d8dfcac2e7e90d9a744db3c986ca8b"


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


def test_golden_ranking_rust_on_arm_is_byte_identical_and_engages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Slice-4 PR-1: ``BRUTEX_RANKING_RUST=1`` reproduces the frozen
    pre-conversion store hash byte-for-byte AND genuinely runs the ranking
    kernels (exactly one ``engaged`` per pow-free family — the
    vacuous-gate trap, Golden Rule #10). The hash equality is
    UNCONDITIONAL on every host: the (b*) families' always-on differential
    ships the Python oracle's bits on any libm divergence (such a
    divergence is a LOUD ``ranking.composite``/``ranking.dsr_z``
    autorevert, never a byte delta — asserted below)."""
    pytest.importorskip("polars")  # flush needs the [data] extra; skips the .[dev]-only job
    from brutex.ranking.rust_ranking import reset_ranking_rust_for_tests

    monkeypatch.setenv("BRUTEX_RANKING_RUST", "0")
    wired = _run(tmp_path, "a")  # explicit OFF — the reference bytes
    monkeypatch.setenv("BRUTEX_RANKING_RUST", "1")
    reset_ranking_rust_for_tests()
    with capture_logs() as logs:
        rust_arm = _run(tmp_path, "b")
    engaged = [e["family"] for e in logs if e["event"] == "rust_parity.engaged"]
    assert engaged.count("ranking.gates") == 1  # the ranking kernels actually served
    assert engaged.count("ranking.order") == 1
    for event in (e for e in logs if e["event"] == "rust_parity.autoreverted"):
        # Legal ONLY as the loud (b*) host-libm arm — never a wire delta.
        assert event["family"] in {"ranking.composite", "ranking.dsr_z"}
        assert event["reason"] == "divergence"
    assert wired.store_content_hash == _GOLDEN_CONTENT_HASH
    assert rust_arm.store_content_hash == _GOLDEN_CONTENT_HASH
    assert (rust_arm.enumerated, rust_arm.evaluated, rust_arm.ranked, rust_arm.persisted) == (
        wired.enumerated,
        wired.evaluated,
        wired.ranked,
        wired.persisted,
    )
    assert (tmp_path / "a.parquet").read_bytes() == (tmp_path / "b.parquet").read_bytes()
    reset_ranking_rust_for_tests()  # never leak burn-in/latch state to siblings


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
