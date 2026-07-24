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
    # DEC-FILL-EXEC-GRID-1M-001: the SECOND permanent identity token — every
    # fill resolves on the 1-minute execution grid, emitted UNCONDITIONALLY
    # directly below fill_mode (same anti-aliasing rationale: a tf=1minute
    # run would otherwise alias its PR-A identity while trades differ via
    # the same-bar deferral).
    "execution_grid": "1m_next_bar",
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
        bars = _bars()
        return run_persisted_sweep(
            bars,
            _config(),
            store=store,
            dedup=dedup,
            audit=audit,
            # tf=1minute — the decision stream IS the execution stream
            # (DEC-FILL-EXEC-GRID-1M-001 §6 fixed point: one list, one key).
            exec_bars_1m=bars,
        )


# ────── Pinned run-identity ──────

#: The hand-pinned BLAKE3 run-identity of mask 0b101 under the fixed context.
#: Re-locked for DEC-VOCAB-U128-CODEGEN-001 (mask ``%032x``), AGAIN for
#: DEC-NIFTY-FIB-AXIS-001 (the ``fib_anchor`` params key), AGAIN for
#: DEC-FILL-WORST-CASE-ONLY-001 (the ``fill_mode: worst_case`` token), and
#: AGAIN for **DEC-FILL-EXEC-GRID-1M-001 (THIS re-lock)**: the params
#: preimage gained EXACTLY the one key ``execution_grid: 1m_next_bar``
#: (nothing else moved — pinned by ``test_worst_case_params_add_exactly_the
#: _two_permanent_keys``), so the digest moved off the PR-A grid identity
#: below by construction.
_GOLDEN_RUN_ID = "caf0cb630c4a1c74d71605f23140a9f35d3b3da2e41543b918723e4a9c9390ca"

#: FROZEN HISTORICAL (anti-alias pin only — the retired PR-A decision-TF-era
#: identity, minted when the params carried ``fill_mode`` but NO
#: ``execution_grid`` key; the #1825 ``_GOLDEN_RUN_ID``). No production run
#: may ever reproduce it again (DEC-FILL-EXEC-GRID-1M-001 §4.2).
_PRE_GRID_RUN_ID = "715212420d2cca2dd998aa0f716ef5b658d551a212a61af135fe0cf6fae62fc9"

#: FROZEN HISTORICAL (anti-alias pin only — the retired precise-era identity;
#: minted pre-DEC when the params carried NO fill_mode key). No production
#: run may ever reproduce it again.
_PRECISE_ERA_RUN_ID = "f588194f3f107bec68ee48975171041dd77c8f5ceec7e0625534b352d2d5921a"


def test_golden_run_id_never_aliases_the_retired_eras() -> None:
    # Anti-aliasing pins: dropping EITHER permanent key would collapse the
    # one-law identity back onto a retired era's digest — this makes that
    # regression loud (DEC-FILL-WORST-CASE-ONLY-001 for fill_mode,
    # DEC-FILL-EXEC-GRID-1M-001 for execution_grid).
    assert len({_GOLDEN_RUN_ID, _PRE_GRID_RUN_ID, _PRECISE_ERA_RUN_ID}) == 3
    current = mask_run_id(mask=0b101, params=_PARAMS, **_CTX)
    assert current != _PRECISE_ERA_RUN_ID
    assert current != _PRE_GRID_RUN_ID


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
# nullable vix columns) → DEC-FILL-WORST-CASE-ONLY-001 (the one-law worst-case
# re-lock over the RE-CRAFTED rankable gap-up fixture; its hash `ee874356…0789`
# is the retired pre-grid literal) → the W2 metrics-depth fold (MIGRATION.md
# #1974–#1976 — the EIGHT appended-LAST nullable columns: the growth trio
# loss_rate / kelly_fraction / log_growth_at_half_kelly + sl_hit_rate
# (EXIT_QUALITY_FIELDS) + the recency four (RECENCY_FIELDS)) →
# DEC-RANK-PARETO-PERSIST-001 / DEC-RANK-QUOTA-PERSIST-1000-001 (#2059/#2060 —
# the TWO appended-LAST selection-provenance columns `pareto_member` Boolean +
# `selection_basis` Utf8; TAGS only at `top_k=None`, so the row SET is provably
# unchanged — `ranked == persisted == 5`, `_GOLDEN_RUN_ID` byte-identical) →
# **DEC-FILL-EXEC-GRID-1M-001 × TRAIN-B FOLD (THIS re-lock)**: every fill
# resolves on the 1-minute execution grid — the run_id preimage gained
# `execution_grid: 1m_next_bar` (byte-stable at `_GOLDEN_RUN_ID = caf0cb63…`
# above) AND the persisted metrics moved (the §2.4 same-bar deferral + the 1m
# re-arm cadence re-shape the stream: 304 trades = 271 × +50 TP + 22 × −115 SL
# + 11 × −10 B3 terminal closes → net +10_910, hand-derived per class in
# `test_golden_trade_stream_exact`). The merged store persists the exec-grid
# stream (304 trades) on the SAME ResultRow columns as the woven W2 + LANE-W1
# additive families, so NEITHER parent literal reproduces (train-b's
# `af3bf465…bed9` had the worst-case 203-trade stream; the member's
# `23ebaf1c…933f` had the exec-grid stream WITHOUT the additive columns).
# RE-DERIVED FRESH on the FOLDED tree in the Train-B integrity pass (the house
# law: goldens are re-derived, never merged textually; determinism pinned by
# the two-run byte-equality test below).
_GOLDEN_CONTENT_HASH = "6e15a6540c0ef8997d71daa3342ebeefb60006e56fb005b16c02744b2e22f7f9"


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


def test_worst_case_params_add_exactly_the_two_permanent_keys() -> None:
    # REWRITTEN for DEC-FILL-EXEC-GRID-1M-001 (was the one-key fill_mode
    # law): canonical_trade_params emits TWO permanent tokens
    # UNCONDITIONALLY — ``fill_mode: worst_case``
    # (DEC-FILL-WORST-CASE-ONLY-001) and ``execution_grid: 1m_next_bar``
    # (this DEC) — no caller can vary or elide either, and popping both
    # yields exactly the legacy params layout (nothing else moved; the
    # _GOLDEN_RUN_ID derivation above rests on this exactly-one-new-key
    # property).
    params = _canonical_params()
    assert params == _PARAMS  # the oracle dict INCLUDES both permanent keys
    legacy = dict(params)
    # Pop OUTSIDE the assert (py/side-effect-in-assert): `python -O` strips
    # asserts, which would elide the mutation the following two lines rely on.
    fill_mode_value = legacy.pop("fill_mode")
    execution_grid_value = legacy.pop("execution_grid")
    assert fill_mode_value == "worst_case"
    assert execution_grid_value == "1m_next_bar"
    assert "fill_mode" not in legacy
    assert "execution_grid" not in legacy


def test_fill_mode_emitted_for_every_leg_shape() -> None:
    # The unconditional law is leg-shape-independent: SL-only / TP-only /
    # EOD-only params all carry BOTH permanent tokens.
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
        assert params["execution_grid"] == "1m_next_bar"


def test_golden_trade_stream_exact() -> None:
    # The numeric TEETH behind the store hash: mask 0b101's worst-case
    # per-trade stream over the fixture on the 1m EXECUTION grid, pinned
    # exactly at head/tail/aggregate + the full exit-reason census
    # (DEC-FILL-EXEC-GRID-1M-001 re-lock — the §"Census obligation": the
    # per-class fill ARITHMETIC below is hand-derived; the class COUNTS are
    # execution-pinned exactly like the #1825 predecessor's 181/22 split,
    # with the two-run byte-determinism tests as the oracle).
    #
    # HAND-DERIVATION per exit class (tf=1minute fixed point — the decision
    # stream IS the exec stream; a signal on bar N fills on bar N+1, and the
    # NEW same-bar deferral starts SL/TP detection on bar N+2):
    # * take_profit (+50): entry at a narrow gap-up bar's HIGH (open+0.40);
    #   the NEXT gap-up opens at prior close + 3.00 with low = open − 0.10 =
    #   entry + 2.90 − 0.20... i.e. far ABOVE the TP level (entry + 0.50) —
    #   the B5 gap-open law fills a BUY exit at the adverse-max vs the
    #   level: min(level, bar low) = the LEVEL → exactly +50 paisa.
    # * stop_loss (−115): entry at the shallow bearish bar's HIGH (o + 0.10,
    #   stop floor entry − 0.80 = o − 0.70); the DEEP bar's low (its open
    #   (o − 0.30) − 0.75 = o − 1.05) breaches it → adverse fill
    #   min(level, low) = o − 1.05 → (o − 1.05) − (o + 0.10) = −115 paisa.
    # * session_gap / end_of_data (−10 each): a position entered on a day's
    #   LAST stored 1m bar (a gap-up bar: entry = high = o + 0.40); the next
    #   1m bar is the NEXT session (or the stream ends), so the retroactive
    #   close lands on the entry bar itself — the B3 terminal carve-out
    #   prices it at that bar's CLOSE (o + 0.30) → −10 paisa; mae =
    #   entry − low = (o+0.40) − (o−0.10) = 50.
    # Census (execution-pinned counts × hand-derived per-class pnl):
    #   271 × (+50) + 22 × (−115) + 10 × (−10) + 1 × (−10)
    #   = 13_550 − 2_530 − 100 − 10 = +10_910 over 304 trades.
    # (The #1825 decision-TF stream was 203 = 181×50 + 22×(−115) = +6_520 —
    # the deferral + 1m re-arm cadence legitimately re-shapes the stream.)
    import collections

    from brutex.engine.combo_mask import mask_to_entry_signals
    from brutex.engine.stage1a import build_bar_bits
    from brutex.engine.trade_sim import TradeDetail, simulate_trades

    bars = _bars()
    signals = mask_to_entry_signals(build_bar_bits(bars), 0b101)
    details: list[TradeDetail] = []
    stream = [
        (t.pnl_paisa, t.mfe_paisa, t.mae_paisa, t.holding_bars)
        for t in simulate_trades(
            bars,
            signals,
            direction=Side.BUY,
            tf_minutes=1,
            exec_bars_1m=bars,  # the §6 tf=1minute fixed point
            stop_loss_paisa=80,
            take_profit_paisa=50,
            detail_sink=details,
        )
    ]
    assert len(stream) == 304
    assert sum(t[0] for t in stream) == 10_910
    assert sum(1 for t in stream if t[0] == 50) == 271
    assert sum(1 for t in stream if t[0] == -115) == 22
    assert collections.Counter(d.exit_reason for d in details) == {
        "take_profit": 271,
        "stop_loss": 22,
        "session_gap": 10,
        "end_of_data": 1,
    }
    # First trade: entry o+0.40, TP at level (+50); mfe/mae fold the entry
    # bar + the exit bar up to the fill (unchanged from the #1825 pin);
    # holding is a 1m-bar count (entry bar + exit bar = 2).
    assert stream[0] == (50, 300, 50, 2)
    # Last trade: the final stored bar's B3 terminal end_of_data close —
    # pnl = close − high = (o+0.30) − (o+0.40) = −10; mfe 0 (the entry bar's
    # high IS the entry); mae 50 (entry − low); holding 1 (one bar).
    assert stream[-1] == (-10, 0, 50, 1)


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
    exec_bars_1m: Sequence[CandleBar],
    exec_map: object = None,
    reference_stamper: object = None,
    precomputed_columns: object = None,
) -> ResidentTradeSim:
    """ResidentTradeSim with the compiled kernel DISABLED — the pure-Python arm.

    The same instance-level ``_rust = None`` forcing seam the rust_trade_sim
    fallback tests use, applied over ``sweep_run.ResidentTradeSim`` (as a
    ctor-signature FACTORY, so no subclass ``__init__`` overwrites the
    superclass attribute) so the WHOLE persisted sweep runs the oracle.
    ``precomputed_columns`` tracks the additive W2-rider kwarg
    (DEC-STAGE1A-RUST-BATCH-001) and ``exec_bars_1m``/``exec_map`` the
    dual-feed kwargs (DEC-FILL-EXEC-GRID-1M-001) — signature glue only.
    """
    sim = ResidentTradeSim(
        bars,
        bar_bits,
        tf_minutes=tf_minutes,
        exec_bars_1m=exec_bars_1m,
        exec_map=exec_map,  # type: ignore[arg-type]
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
            bars = _bars()
            return run_persisted_sweep(
                bars,
                cfg,
                store=store,
                dedup=dedup,
                audit=audit,
                exec_bars_1m=bars,  # the §6 tf=1minute fixed point
            )

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


# ────── DEC-SWEEP-ENUM-RUST-001 — the DIGEST-PATH GOLDEN VARIANT ──────
#
# The design-of-record's §5 anti-vacuous bullet (round-3 MAJOR fix): the
# ORIGINAL fixture above passes NO trial_stats_digest (the LEGACY dedup path)
# and leaves top_k=None — the Rust enum/core arms can never engage on it, so
# engagement-dependent goldens against it would be either unimplementable or
# vacuous Python==Python compares. This VARIANT is the SAME synthetic grid
# with a synthetic trial_stats_digest and a finite top_k >= the ranked count
# (finalists == ranked, so the finalist SET matches the legacy fixture's).
# Its literals were MINTED ON THE OFF ARM FIRST (BRUTEX_SWEEP_RUST_ENUM
# unset, 2026-07-11 — SHIPPING-SPINE-authored, new-enum-kernel-free by
# construction) and are now FROZEN; every engagement-dependent golden below
# runs against the VARIANT, and the ORIGINAL fixture gains the LEGACY-REFUSAL
# role (flag ON reproduces the original literals with zero engaged events +
# one `legacy_dedup_path` warn).

#: The variant's synthetic tuple digest (16 hex chars — the trial-stats law).
_DIGEST_VARIANT = "d16e57e0d16e57e0"

#: OFF-arm-minted store content hash for the digest variant. EQUAL to
#: `_GOLDEN_CONTENT_HASH` by construction — top_k=8 >= the 5 ranked combos, so
#: the persisted finalist rows are identical to the legacy fixture's; the
#: digest path adds only the sidecar + sentinel artifacts, which do not enter
#: the store bytes. Frozen as its OWN literal so a future divergence between
#: the paths breaks THIS golden by name.
# RE-PINNED at the 2026-07-24 club fold: the base's landed
#: DEC-FILL-EXEC-GRID-1M-001 (#1989) re-locked the legacy fixture's
#: `_GOLDEN_CONTENT_HASH` (the 1-minute execution grid moved the store bytes)
#: but MISSED this digest-variant literal, which is EQUAL to it by the
#: documented construction — so it moves in lockstep to the base's re-locked
#: value (never a fresh mint; captured equal to `_GOLDEN_CONTENT_HASH` on the
#: OFF/ENUM-ON/poisoned-OFF arms alike, all three agree by construction).
_GOLDEN_CONTENT_HASH_DIGEST = "6e15a6540c0ef8997d71daa3342ebeefb60006e56fb005b16c02744b2e22f7f9"

#: The variant's run-identity literal — identical to `_GOLDEN_RUN_ID` (the
#: run identity is a pure BLAKE3 of its 9 canonical inputs; neither the
#: digest nor top_k enters it — pinned so a future accidental coupling of
#: run identity to either would break THIS golden by name).
# RE-PINNED at the same fold: #1989 emits `execution_grid: "1m_next_bar"`
#: unconditionally in `canonical_trade_params`, so the run identity moved to
#: the base's re-locked `_GOLDEN_RUN_ID` (the retired value is the base's
#: negative-pinned `_PRE_GRID_RUN_ID`). Verified via
#: `mask_run_id(mask=0b101, params=_PARAMS, **_CTX)` in the OFF-arm test below.
_GOLDEN_RUN_ID_DIGEST = "caf0cb630c4a1c74d71605f23140a9f35d3b3da2e41543b918723e4a9c9390ca"


def _run_digest_variant(tmp_path: Path, tag: str) -> SweepRunSummary:
    """The digest-path variant run: same grid, synthetic digest, top_k=8."""
    bars = _bars()
    store = ResultsStore(path=tmp_path / f"{tag}.parquet")
    dedup = ComboDedup(OptionDedupIndex(capacity=10_000, fpr_target=1e-6))
    with AuditWriter(path=tmp_path / f"{tag}.jsonl") as audit:
        return run_persisted_sweep(
            bars,
            replace(_config(), top_k=8),
            store=store,
            dedup=dedup,
            audit=audit,
            trial_stats_digest=_DIGEST_VARIANT,
            exec_bars_1m=bars,  # the §6 tf=1minute fixed point
        )


def _reset_enum_state() -> None:
    from brutex.engine.rust_enum import (
        reset_enum_self_check_for_tests,
        reset_observability_memo_for_tests,
    )

    reset_enum_self_check_for_tests()
    reset_observability_memo_for_tests()


def test_digest_variant_off_arm_reproduces_the_frozen_literals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OFF arm (the explicit ``"0"`` kill switch): the frozen OFF-minted
    literals reproduce, with ZERO `sweep_rust_enum.engaged` AND ZERO
    `sweep_rust_core.engaged` events (the negative arm-discriminator pins —
    bytes are arm-identical by construction, so the events are the ONLY
    discriminator). Since DEC-RUST-DEFAULT-ON-WAVE-001 (#2215) the default is
    ON, so OFF is armed ``"0"`` — an unset flag would engage the kernel."""
    pytest.importorskip("polars")
    monkeypatch.setenv("BRUTEX_SWEEP_RUST_ENUM", "0")
    _reset_enum_state()
    with capture_logs() as logs:
        summary = _run_digest_variant(tmp_path, "off")
    assert summary.store_content_hash == _GOLDEN_CONTENT_HASH_DIGEST
    assert mask_run_id(mask=0b101, params=_PARAMS, **_CTX) == _GOLDEN_RUN_ID_DIGEST
    assert not [e for e in logs if e["event"] == "sweep_rust_enum.engaged"]
    assert not [e for e in logs if e["event"] == "sweep_rust_core.engaged"]
    _reset_enum_state()


def test_digest_variant_enum_on_reproduces_the_literals_and_engages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ENUM-only arm: byte-for-byte the OFF-minted literals WITH exactly one
    `sweep_rust_enum.engaged` and ZERO `sweep_rust_core.engaged` (the
    positive + negative engaged pins; the fold core does not exist in
    Slice 1 — the zero-count assertion is structured so Slice 2 extends this
    to the full 4-combo arm matrix), and zero autorevert events."""
    pytest.importorskip("polars")
    monkeypatch.setenv("BRUTEX_SWEEP_RUST_ENUM", "1")
    _reset_enum_state()
    with capture_logs() as logs:
        summary = _run_digest_variant(tmp_path, "on")
    engaged = [e for e in logs if e["event"] == "sweep_rust_enum.engaged"]
    assert len(engaged) == 1
    assert not [e for e in logs if e["event"] == "sweep_rust_core.engaged"]
    assert not [e for e in logs if e["event"] == "sweep_rust_enum.kernel_autoreverted"]
    assert summary.store_content_hash == _GOLDEN_CONTENT_HASH_DIGEST
    _reset_enum_state()


def test_legacy_refusal_golden_flag_on_reproduces_the_original_literals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The LEGACY-REFUSAL golden: the ORIGINAL fixture (no digest, top_k=None)
    with the enum flag ON reproduces the ORIGINAL frozen literals with ZERO
    engaged events and exactly one `legacy_dedup_path` fallback warn — the
    engagement-condition teeth (the digest-path condition is named FIRST in
    the design's listing order)."""
    pytest.importorskip("polars")
    monkeypatch.setenv("BRUTEX_SWEEP_RUST_ENUM", "1")
    _reset_enum_state()
    with capture_logs() as logs:
        summary = _run(tmp_path, "legacy")
    assert summary.store_content_hash == _GOLDEN_CONTENT_HASH
    assert not [e for e in logs if e["event"] == "sweep_rust_enum.engaged"]
    warns = [e for e in logs if e["event"] == "sweep_rust_enum.fallback"]
    assert [w["reason"] for w in warns] == ["legacy_dedup_path"]
    _reset_enum_state()


def test_digest_variant_disabled_arm_poison_reproduces_the_literals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Disabled-arm POISON golden (the runbook's teeth): a poisoned
    `fno_math.ResidentSweepEnum` with the enum flag OFF reproduces the
    variant literals byte-for-byte with zero enum events — the flag ALONE
    prevents kernel consumption (exactly the mid-incident configuration the
    kill-switch runbook puts an operator in)."""
    pytest.importorskip("polars")
    import fno_math

    class _Poisoned:
        def __init__(self, bar_bits: list[int]) -> None:
            raise AssertionError("the disabled arm must never construct the kernel")

    monkeypatch.setenv("BRUTEX_SWEEP_RUST_ENUM", "0")
    monkeypatch.setattr(fno_math, "ResidentSweepEnum", _Poisoned)
    _reset_enum_state()
    with capture_logs() as logs:
        summary = _run_digest_variant(tmp_path, "poisoned_off")
    assert summary.store_content_hash == _GOLDEN_CONTENT_HASH_DIGEST
    assert not [e for e in logs if e["event"] == "sweep_rust_enum.engaged"]
    assert not [e for e in logs if e["event"] == "sweep_rust_enum.kernel_autoreverted"]
    _reset_enum_state()
