"""Unit tests for the native-Rust Apriori seam (DEC-SWEEP-APRIORI-RUST-001).

Covers the seam module :mod:`brutex.engine.sweep_apriori_rust` at 100%
line+branch: the composed doubly-default-OFF gate, the fail-safe-OFF + warn-once
arm flag, the u64-envelope delegation, the census emit branches, the breaker
propagation, and the ``run_persisted_sweep`` composed-gate wiring (default-OFF
spy + leaderboard byte-identity). Bit-for-bit parity vs the FROZEN generator
lives in ``tests/golden/test_engine_sweep_apriori_rust_parity.py``.
"""

# Whitebox test: it drives the seam's private warn-state + conflict-pair helper
# directly (DEC-P0-017 whitebox file-top convention, the frozen sweep_apriori
# whitebox precedent).
# pyright: reportPrivateUsage=none

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from brutex.data.models import CandleBar
from brutex.engine import sweep_apriori as _apriori
from brutex.engine import sweep_apriori_rust as seam
from brutex.engine.sweep_apriori_rust import (
    SWEEP_RUST_APRIORI_ENV,
    apriori_rust_enabled,
    apriori_use_rust,
    generate_apriori_candidates_rust,
)
from brutex.engine.sweep_run import (
    SweepAprioriBudgetError,
    SweepAprioriUnsupportedError,
    SweepRunSummary,
)

pytest.importorskip("fno_math")

_BITS = [0, 1, 2, 3, 4, 5, 6, 7]


def _bar_bits(seed: int, n: int = 300, width: int = 8) -> list[int]:
    rng = np.random.default_rng(seed)
    return [int(x) for x in rng.integers(0, 1 << width, size=n)]


# ───────────────────── the arm env flag (fail-safe OFF + warn-once) ─────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, False),
        ("", False),
        ("1", True),
        ("true", True),
        ("on", True),
        ("YES", True),
        ("0", False),
        ("off", False),
        ("no", False),
    ],
)
def test_apriori_rust_enabled_known_literals(
    monkeypatch: pytest.MonkeyPatch, raw: str | None, expected: bool
) -> None:
    monkeypatch.setitem(seam._ARM_FLAG_WARN_STATE, "warned", False)
    if raw is None:
        monkeypatch.delenv(SWEEP_RUST_APRIORI_ENV, raising=False)
    else:
        monkeypatch.setenv(SWEEP_RUST_APRIORI_ENV, raw)
    assert apriori_rust_enabled() is expected


def test_unrecognised_value_is_off_and_warns_once(monkeypatch: pytest.MonkeyPatch) -> None:
    from structlog.testing import capture_logs

    monkeypatch.setitem(seam._ARM_FLAG_WARN_STATE, "warned", False)
    monkeypatch.setenv(SWEEP_RUST_APRIORI_ENV, "garbage")
    with capture_logs() as logs:
        assert apriori_rust_enabled() is False  # first call warns
        assert apriori_rust_enabled() is False  # second call: already warned, silent
    warns = [e for e in logs if e["event"] == "sweep.apriori.rust.flag_unrecognised"]
    assert len(warns) == 1  # warn-once
    assert warns[0]["env"] == SWEEP_RUST_APRIORI_ENV
    assert warns[0]["value"] == "'garbage'"
    assert warns[0]["resolved"] == "off"


# ───────────────────── the composed gate ─────────────────────


@pytest.mark.parametrize(
    ("apriori", "rust", "expected"),
    [
        (None, None, False),  # both off → False
        ("1", None, False),  # apriori on, rust off → False (frozen Python path)
        (None, "1", False),  # apriori off, rust on → False (brute-force path)
        ("1", "1", True),  # both on → True (native arm)
    ],
)
def test_apriori_use_rust_composed(
    monkeypatch: pytest.MonkeyPatch, apriori: str | None, rust: str | None, expected: bool
) -> None:
    monkeypatch.setitem(seam._ARM_FLAG_WARN_STATE, "warned", False)
    for env, val in ((_apriori.SWEEP_APRIORI_ENV, apriori), (SWEEP_RUST_APRIORI_ENV, rust)):
        if val is None:
            monkeypatch.delenv(env, raising=False)
        else:
            monkeypatch.setenv(env, val)
    assert apriori_use_rust() is expected


# ───────────────────── conflict-pair masks ─────────────────────


def test_conflict_pair_masks_are_two_bit_supersets() -> None:
    from brutex.vocabulary.conflicts import CONFLICT_PAIRS, has_conflict

    masks = seam._conflict_pair_masks()
    assert len(masks) == len(CONFLICT_PAIRS)
    for m in masks:
        assert bin(m).count("1") == 2  # exactly two bits per pair
        assert has_conflict(m)  # each is a real conflict mask


# ───────────────────── validation branches (each raises the frozen message) ─────────────────────


def test_negative_bit_valueerror() -> None:
    with pytest.raises(ValueError, match="negative"):
        generate_apriori_candidates_rust([1], condition_bits=[-1, 3], k=1, min_hits=0)


def test_unsupported_bit_raises_typed() -> None:
    with pytest.raises(SweepAprioriUnsupportedError, match="u128"):
        generate_apriori_candidates_rust([1], condition_bits=[0, 64], k=1, min_hits=0)


def test_k_out_of_range_valueerror() -> None:
    with pytest.raises(ValueError, match="invalid k"):
        generate_apriori_candidates_rust([1], condition_bits=[0, 1, 2], k=4, min_hits=0)


@pytest.mark.parametrize("root_mod", [(0, 0), (2, 2), (-1, 3), (3, 2)])
def test_bad_root_mod_valueerror(root_mod: tuple[int, int]) -> None:
    with pytest.raises(ValueError, match="root_mod"):
        generate_apriori_candidates_rust(
            [1], condition_bits=_BITS, k=1, min_hits=0, root_mod=root_mod
        )


def test_negative_n_bars_valueerror() -> None:
    with pytest.raises(ValueError, match="n_bars"):
        generate_apriori_candidates_rust([1], condition_bits=_BITS, k=1, min_hits=0, n_bars=-1)


@pytest.mark.parametrize("bad", [0, -1, -1_000_000])
def test_non_positive_max_mask_bars_valueerror(bad: int) -> None:
    with pytest.raises(ValueError, match="max_mask_bars"):
        generate_apriori_candidates_rust(
            [1], condition_bits=_BITS, k=1, min_hits=0, max_mask_bars=bad
        )


# ───────────────────── success + census + breaker ─────────────────────


def test_k0_and_k1_native() -> None:
    assert generate_apriori_candidates_rust([1, 2, 3], condition_bits=_BITS, k=0, min_hits=0) == [0]
    bar_bits = _bar_bits(1)
    out = generate_apriori_candidates_rust(
        bar_bits, condition_bits=_BITS, k=1, min_hits=10_000, n_bars=len(bar_bits)
    )
    assert out == [1 << b for b in _BITS]  # k==1 not hit-filtered


def test_max_mask_bars_none_resolves_default_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # max_mask_bars=None → resolve_max_mask_bars() (frozen default). A tiny env
    # override makes even a k=1 cell trip, proving the None branch resolves it.
    monkeypatch.setenv(_apriori.MAX_MASK_BARS_ENV, "1")
    bar_bits = _bar_bits(1, n=100)
    with pytest.raises(SweepAprioriBudgetError):
        generate_apriori_candidates_rust(bar_bits, condition_bits=_BITS, k=1, min_hits=0)


def test_census_frequent_int_and_none_branches() -> None:
    # A clean k=3 build emits both a frequent-int census (levels 1,2) and a
    # frequent-None census (target level 3) — exercising both emit branches.
    from structlog.testing import capture_logs

    bar_bits = [(1 << 6) | (1 << 7) | (1 << 8)] * 20
    with capture_logs() as logs:
        out = generate_apriori_candidates_rust(
            bar_bits, condition_bits=[6, 7, 8], k=3, min_hits=0, n_bars=20, cell_label="c"
        )
    assert out == [(1 << 6) | (1 << 7) | (1 << 8)]
    census = [e for e in logs if e["event"] == "sweep.apriori.census"]
    assert [e.get("frequent") for e in census] == [3, 3, None]  # int, int, omitted


def test_budget_trip_propagates_named_error() -> None:
    bar_bits = _bar_bits(1, n=1000)
    with pytest.raises(SweepAprioriBudgetError) as ei:
        generate_apriori_candidates_rust(
            bar_bits,
            condition_bits=_BITS,
            k=2,
            min_hits=0,
            n_bars=len(bar_bits),
            max_mask_bars=1,
            cell_label="NIFTY:1m:bull:k=2",
        )
    msg = str(ei.value)
    assert "NIFTY:1m:bull:k=2" in msg
    assert "1000 bars" in msg
    assert "mask·bar" in msg


def test_envelope_delegation_both_operands(monkeypatch: pytest.MonkeyPatch) -> None:
    # Cover BOTH operands of the `limit > U64 or n_bars > U64` envelope guard.
    import fno_math

    called = {"n": 0}
    real = fno_math.apriori_frontier_u64

    def spy(*a, **k):  # type: ignore[no-untyped-def]
        called["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(fno_math, "apriori_frontier_u64", spy)
    bar_bits = _bar_bits(1, n=40)
    huge = (1 << 64) + 3
    # limit > U64 (n_bars in range)
    out1 = generate_apriori_candidates_rust(
        bar_bits, condition_bits=_BITS, k=2, min_hits=0, max_mask_bars=huge
    )
    # n_bars > U64 (limit in range) — n_bars huge means the mask·bar product is
    # astronomically large but delegated to the big-int Python generator, which
    # will trip on its own big-int product; both raise the SAME error.
    with pytest.raises(SweepAprioriBudgetError) as rs_ei:
        generate_apriori_candidates_rust(
            bar_bits, condition_bits=_BITS, k=1, min_hits=0, n_bars=huge, max_mask_bars=100
        )
    from brutex.engine.sweep_apriori import generate_apriori_candidates

    with pytest.raises(SweepAprioriBudgetError) as py_ei:
        generate_apriori_candidates(
            bar_bits, condition_bits=_BITS, k=1, min_hits=0, n_bars=huge, max_mask_bars=100
        )
    assert str(rs_ei.value) == str(py_ei.value)
    assert out1  # non-empty
    assert called["n"] == 0  # kernel never invoked beyond the envelope


# ───────────────────── run_persisted_sweep composed-gate wiring ─────────────────────


def _mini_bars() -> list[CandleBar]:
    from datetime import UTC, datetime, timedelta, timezone
    from decimal import Decimal

    ist = timezone(timedelta(hours=5, minutes=30))
    out: list[CandleBar] = []
    for d in range(8):
        price = 100.0
        for m in range(90):
            close = price + 0.6 if m % 3 else price - 0.3
            ts = (
                datetime(2026, 1, 5, 9, 16, tzinfo=ist) + timedelta(days=d, minutes=m)
            ).astimezone(UTC)
            out.append(
                CandleBar(
                    timestamp=ts,
                    open=Decimal(str(round(price, 2))),
                    high=Decimal(str(round(max(price, close) + 0.5, 2))),
                    low=Decimal(str(round(min(price, close) - 0.5, 2))),
                    close=Decimal(str(round(close, 2))),
                    volume=100,
                    open_interest=None,
                )
            )
            price = close
    return out


def _run_sweep(
    tmp_path: Path,
    tag: str,
    bars: list[CandleBar],
    monkeypatch: pytest.MonkeyPatch,
    apriori: str | None,
    rust: str | None,
) -> tuple[SweepRunSummary, bytes]:
    pytest.importorskip("polars")
    from brutex.data.option_dedup import OptionDedupIndex
    from brutex.engine.results_store import ResultsStore
    from brutex.engine.sweep_run import ComboDedup, SweepRunConfig, run_persisted_sweep
    from brutex.orchestrator.audit import AuditWriter
    from brutex.strategies.types import Side

    for env, val in ((_apriori.SWEEP_APRIORI_ENV, apriori), (SWEEP_RUST_APRIORI_ENV, rust)):
        if val is None:
            monkeypatch.delenv(env, raising=False)
        else:
            monkeypatch.setenv(env, val)
    d = tmp_path / tag
    d.mkdir()
    store = ResultsStore(path=d / "cell.parquet")
    dedup = ComboDedup(OptionDedupIndex(capacity=10_000, fpr_target=1e-6))
    cfg = SweepRunConfig(
        instrument="NIFTY",
        tf="1m",
        direction=Side.BUY,
        mode="A",
        condition_bits=_BITS,
        k=3,
        data_snapshot_hash="snap",
        indicator_version="iv",
        code_commit_sha="cc",
        tf_minutes=1,
        sl_paisa=80,
        tp_paisa=50,
        min_hits=5,
    )
    with AuditWriter.open(d / "cell.jsonl") as audit:
        summary = run_persisted_sweep(bars, cfg, store=store, dedup=dedup, audit=audit)
    return summary, (d / "cell.parquet").read_bytes()


def test_composed_gate_leaderboard_byte_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BRUTEX_SWEEP_APRIORI=1 + BRUTEX_SWEEP_RUST_APRIORI=1 (native arm) produces a
    byte-identical leaderboard vs the flag-OFF brute-force path."""
    bars = _mini_bars()
    off, off_bytes = _run_sweep(tmp_path, "off", bars, monkeypatch, apriori=None, rust=None)
    on, on_bytes = _run_sweep(tmp_path, "on", bars, monkeypatch, apriori="1", rust="1")
    assert on.store_content_hash == off.store_content_hash
    assert on_bytes == off_bytes
    assert on.enumerated < off.enumerated  # pruning fired (native arm)


def test_default_off_arm_uses_python_never_kernel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the rust arm OFF (BRUTEX_SWEEP_APRIORI=1 only) the native kernel is
    NEVER invoked — the frozen Python generator runs (spy-pinned)."""
    import fno_math

    called = {"n": 0}
    real = fno_math.apriori_frontier_u64

    def spy(*a, **k):  # type: ignore[no-untyped-def]
        called["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(fno_math, "apriori_frontier_u64", spy)
    bars = _mini_bars()
    on_py, _ = _run_sweep(tmp_path, "pyonly", bars, monkeypatch, apriori="1", rust=None)
    assert on_py.enumerated < 300  # pruning fired via the Python generator
    assert called["n"] == 0  # the kernel was NEVER touched with the arm off


def test_rust_arm_invokes_kernel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With BOTH flags on the native kernel IS invoked (spy-pinned) — proving the
    composed arm actually routes through Rust."""
    import fno_math

    called = {"n": 0}
    real = fno_math.apriori_frontier_u64

    def spy(*a, **k):  # type: ignore[no-untyped-def]
        called["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(fno_math, "apriori_frontier_u64", spy)
    bars = _mini_bars()
    _run_sweep(tmp_path, "rustarm", bars, monkeypatch, apriori="1", rust="1")
    assert called["n"] >= 1  # the native kernel was invoked


# ───────────────────── CI wiring pin ─────────────────────


class TestWorkflowWiring:
    """Pin the seam module into its dedicated per-module 100% coverage gate.

    Deterministic pin (the frozen ``sweep_apriori`` ``TestWorkflowWiring``
    precedent) that fails the unit suite if the module:suite pairing is ever
    silently dropped from ci.yml — an unwired gate must never masquerade as an
    active one (Golden Rule #10).
    """

    _CI = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"

    def test_dedicated_100pct_coverage_gate_wired(self) -> None:
        text = self._CI.read_text(encoding="utf-8")
        assert "--cov=brutex.engine.sweep_apriori_rust" in text
        assert "tests/unit/test_engine_sweep_apriori_rust.py" in text
