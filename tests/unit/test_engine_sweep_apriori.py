"""Unit tests for the Apriori pruned sweep candidate generator.

DEC-SWEEP-APRIORI-TUPLE-001. The generator
(:func:`brutex.engine.sweep_apriori.generate_apriori_candidates`) is a
default-OFF, drop-in replacement for the brute-force
:func:`brutex.engine.combo_mask.iter_masks_k_of_bits` enumeration whose
Stage-1B-kept set is provably identical (byte-identical leaderboard). These
tests pin:

* the ORACLE parity — for every ``k`` and a range of ``min_hits``, the
  Apriori candidates re-filtered by ``count_supermasks >= min_hits`` equal the
  brute-force frequent set (conflict-free ∩ ``hits >= min_hits``);
* the EMISSION order — a subsequence of the brute-force enumeration
  (lex-by-position);
* determinism / purity, the ``root_mod`` shard union-and-disjointness, the
  time-based breaker, the env resolvers, the ``<64``-bit guard, and the
  ``k >= 3`` conflict-freeness that subset-prune guarantees for free.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from brutex.engine import sweep_apriori
from brutex.engine.combo_mask import count_supermasks, iter_masks_k_of_bits
from brutex.engine.sweep_apriori import (
    CELL_BUDGET_ENV,
    DEFAULT_CELL_BUDGET_S,
    DEFAULT_WALK_RATE,
    SWEEP_APRIORI_ENV,
    WALK_RATE_ENV,
    apriori_enabled,
    generate_apriori_candidates,
    resolve_cell_budget_s,
    resolve_walk_rate,
)
from brutex.engine.sweep_run import SweepAprioriBudgetError, SweepComboError
from brutex.vocabulary.conflicts import CONFLICT_PAIRS, has_conflict

# A bit set that INCLUDES three real conflict pairs — (0, 1), (2, 3), (4, 5) —
# plus two conflict-free bits, so conflict-pruning is genuinely exercised.
_BITS = [0, 1, 2, 3, 4, 5, 6, 7]


def _synthetic_bar_bits(seed: int, n: int = 500, width: int = 8) -> list[int]:
    """Deterministic per-bar packed bitsets over ``[0, 2**width)``."""
    rng = np.random.default_rng(seed)
    return [int(x) for x in rng.integers(0, 1 << width, size=n)]


def _brute_frequent(bar_bits: list[int], bits: list[int], k: int, min_hits: int) -> set[int]:
    """The brute-force frequent set: conflict-free ``k``-combos with hits >= min_hits."""
    out: set[int] = set()
    for mask in iter_masks_k_of_bits(bits, k):
        if has_conflict(mask):
            continue
        if count_supermasks(bar_bits, [mask])[0] >= min_hits:
            out.add(mask)
    return out


def _apriori_kept(bar_bits: list[int], bits: list[int], k: int, min_hits: int) -> set[int]:
    """The Apriori candidates re-filtered by Stage-1B (``hits >= min_hits``)."""
    cands = generate_apriori_candidates(
        bar_bits, condition_bits=bits, k=k, min_hits=min_hits, n_bars=len(bar_bits)
    )
    if not cands:
        return set()
    hits = count_supermasks(bar_bits, cands)
    return {mask for mask, hit in zip(cands, hits, strict=True) if hit >= min_hits}


# ────────────────────────── oracle parity ──────────────────────────


@pytest.mark.parametrize("seed", [1, 7, 99])
@pytest.mark.parametrize("min_hits", [0, 100, 250, 400])
def test_oracle_kept_set_matches_brute_force(seed: int, min_hits: int) -> None:
    bar_bits = _synthetic_bar_bits(seed)
    for k in range(len(_BITS) + 1):
        assert _apriori_kept(bar_bits, _BITS, k, min_hits) == _brute_frequent(
            bar_bits, _BITS, k, min_hits
        )


def test_k0_is_the_empty_mask() -> None:
    assert generate_apriori_candidates([1, 2, 3], condition_bits=_BITS, k=0, min_hits=0) == [0]


def test_k1_is_all_conflict_free_singletons_not_hit_filtered() -> None:
    # min_hits huge → no singleton is "frequent", but k==1 is NOT hit-filtered
    # (Stage-1B does that downstream), so every singleton is still emitted.
    bar_bits = _synthetic_bar_bits(1)
    got = generate_apriori_candidates(
        bar_bits, condition_bits=_BITS, k=1, min_hits=10_000, n_bars=len(bar_bits)
    )
    assert got == [1 << b for b in _BITS]


def test_high_min_hits_yields_empty_frontier() -> None:
    bar_bits = _synthetic_bar_bits(1)
    # No singleton fires on all 500 bars, so F_1 is empty and every k>=2 level
    # collapses to nothing.
    for k in (2, 3, 4):
        assert (
            generate_apriori_candidates(
                bar_bits, condition_bits=_BITS, k=k, min_hits=10_000, n_bars=len(bar_bits)
            )
            == []
        )


# ────────────────────────── emission order ──────────────────────────


@pytest.mark.parametrize("seed", [1, 7])
@pytest.mark.parametrize("min_hits", [0, 200])
def test_emission_is_iter_order_subsequence(seed: int, min_hits: int) -> None:
    bar_bits = _synthetic_bar_bits(seed)
    for k in range(len(_BITS) + 1):
        out = generate_apriori_candidates(
            bar_bits, condition_bits=_BITS, k=k, min_hits=min_hits, n_bars=len(bar_bits)
        )
        got_set = set(out)
        # The output must appear in the SAME order the brute-force enumeration
        # yields it (lex-by-position) — a literal subsequence.
        expected = [m for m in iter_masks_k_of_bits(_BITS, k) if m in got_set]
        assert out == expected


def test_lex_order_differs_from_int_order_but_matches_iter() -> None:
    # Interleaved bits where lex-by-position != ascending-by-int: (0,3)->9 sorts
    # before (1,2)->6 lexicographically but after it numerically. Bits 0-3 carry
    # the conflict pairs (0,1)/(2,3), so the expected value is the conflict-free
    # subsequence of the brute-force enumeration.
    bar_bits = [0b1111] * 40  # every mask fires on all bars → whole C_2 kept
    out = generate_apriori_candidates(bar_bits, condition_bits=[0, 1, 2, 3], k=2, min_hits=0)
    expected = [m for m in iter_masks_k_of_bits([0, 1, 2, 3], 2) if not has_conflict(m)]
    assert out == expected  # lex-by-position: [5, 9, 6, 10]
    assert out != sorted(out)  # NOT ascending-by-int (9 precedes 6)


# ────────────────────────── determinism / purity ──────────────────────────


def test_determinism_and_permutation_invariance() -> None:
    bar_bits = _synthetic_bar_bits(3)
    a = generate_apriori_candidates(bar_bits, condition_bits=_BITS, k=3, min_hits=150)
    b = generate_apriori_candidates(bar_bits, condition_bits=_BITS, k=3, min_hits=150)
    assert a == b
    # Permuting the input condition_bits (set semantics) yields the identical list.
    permuted = generate_apriori_candidates(
        bar_bits, condition_bits=list(reversed(_BITS)), k=3, min_hits=150
    )
    assert permuted == a


def test_dedups_condition_bits() -> None:
    bar_bits = _synthetic_bar_bits(3)
    assert generate_apriori_candidates(
        bar_bits, condition_bits=[6, 6, 7, 7], k=2, min_hits=0
    ) == generate_apriori_candidates(bar_bits, condition_bits=[6, 7], k=2, min_hits=0)


def test_default_counter_matches_injected() -> None:
    bar_bits = _synthetic_bar_bits(5)

    def injected(masks):  # type: ignore[no-untyped-def]
        return count_supermasks(bar_bits, masks)

    default = generate_apriori_candidates(
        bar_bits, condition_bits=_BITS, k=3, min_hits=180, n_bars=len(bar_bits)
    )
    with_injected = generate_apriori_candidates(
        bar_bits,
        condition_bits=_BITS,
        k=3,
        min_hits=180,
        count_hits=injected,
        n_bars=len(bar_bits),
    )
    assert default == with_injected


def test_n_bars_defaults_to_len_bar_bits() -> None:
    bar_bits = _synthetic_bar_bits(5, n=42)
    # A tiny budget with the default n_bars (=42) still trips → proves n_bars
    # defaulted to len(bar_bits) (not 0, which would never trip).
    with pytest.raises(SweepAprioriBudgetError):
        generate_apriori_candidates(
            bar_bits, condition_bits=_BITS, k=1, min_hits=0, cell_budget_s=1e-12, walk_rate=1.0
        )


# ────────────────────────── conflict-freeness ──────────────────────────


@pytest.mark.parametrize("seed", [1, 7, 99])
@pytest.mark.parametrize("k", [2, 3, 4, 5])
def test_candidates_are_conflict_free(seed: int, k: int) -> None:
    bar_bits = _synthetic_bar_bits(seed)
    cands = generate_apriori_candidates(
        bar_bits, condition_bits=_BITS, k=k, min_hits=0, n_bars=len(bar_bits)
    )
    assert not any(has_conflict(m) for m in cands)


def test_conflict_pair_dropped_at_level_two() -> None:
    # Bits [0, 1] are a conflict pair; both singletons frequent, so the ONLY
    # level-2 candidate (0, 1) exists but is conflict-dropped → empty.
    assert (0, 1) in CONFLICT_PAIRS
    bar_bits = [0b11] * 50  # both bits fire on every bar
    assert generate_apriori_candidates(bar_bits, condition_bits=[0, 1], k=2, min_hits=0) == []


def test_subset_prune_drops_triple_with_infrequent_pair() -> None:
    # bits 6, 7 fire on every bar; bit 2 fires on only a few — so the pairs
    # (2,6) and (2,7) are INfrequent and the triple (2,6,7) must be subset-pruned
    # even though it is conflict-free.
    bar_bits = [0b11000000] * 100 + [0b11000100] * 3  # 6,7 always; 2 rarely
    k, min_hits = 3, 10
    cands = generate_apriori_candidates(
        bar_bits, condition_bits=[2, 6, 7], k=k, min_hits=min_hits, n_bars=len(bar_bits)
    )
    triple = (1 << 2) | (1 << 6) | (1 << 7)
    assert triple not in cands
    # Every candidate is a real conflict-free k-combo of the chosen bits.
    assert set(cands) <= set(iter_masks_k_of_bits([2, 6, 7], k))


# ────────────────────────── root_mod sharding ──────────────────────────


@pytest.mark.parametrize("big_w", [1, 2, 3, 7])
@pytest.mark.parametrize("k", [0, 1, 2, 3])
def test_shards_union_to_whole_and_are_disjoint(big_w: int, k: int) -> None:
    bar_bits = _synthetic_bar_bits(11)
    whole = set(
        generate_apriori_candidates(
            bar_bits, condition_bits=_BITS, k=k, min_hits=0, n_bars=len(bar_bits)
        )
    )
    shards = [
        set(
            generate_apriori_candidates(
                bar_bits,
                condition_bits=_BITS,
                k=k,
                min_hits=0,
                n_bars=len(bar_bits),
                root_mod=(w, big_w),
            )
        )
        for w in range(big_w)
    ]
    union: set[int] = set()
    for shard in shards:
        union |= shard
    assert union == whole
    for i in range(big_w):
        for j in range(i + 1, big_w):
            assert not (shards[i] & shards[j])


def test_k0_owned_only_by_shard_zero() -> None:
    assert generate_apriori_candidates(
        [1], condition_bits=_BITS, k=0, min_hits=0, root_mod=(0, 3)
    ) == [0]
    assert (
        generate_apriori_candidates([1], condition_bits=_BITS, k=0, min_hits=0, root_mod=(1, 3))
        == []
    )


# ────────────────────────── the breaker ──────────────────────────


def test_breaker_raises_named_sweepcomboerror() -> None:
    bar_bits = _synthetic_bar_bits(1, n=1000)
    with pytest.raises(SweepAprioriBudgetError) as ei:
        generate_apriori_candidates(
            bar_bits,
            condition_bits=_BITS,
            k=2,
            min_hits=0,
            n_bars=len(bar_bits),
            cell_budget_s=1e-9,
            walk_rate=1.0,
            cell_label="NIFTY:1m:bull:k=2",
        )
    err = ei.value
    assert isinstance(err, SweepComboError)  # caught by the driver TUPLE_FAILED path
    msg = str(err)
    assert "NIFTY:1m:bull:k=2" in msg
    assert "k=2" in msg
    assert "1000 bars" in msg
    assert CELL_BUDGET_ENV in msg
    assert SWEEP_APRIORI_ENV in msg


def test_breaker_trips_before_materialising_giant_list(monkeypatch: pytest.MonkeyPatch) -> None:
    # A tiny stride makes the incremental in-join breaker fire early. With a
    # generous budget the incremental check simply EXECUTES (covering the
    # modulo branch); with a tiny budget it RAISES during accumulation.
    monkeypatch.setattr(sweep_apriori, "_BUDGET_CHECK_STRIDE", 4)
    bar_bits = [0b1111111111] * 20  # dense: many level-2 candidates
    bits = list(range(10))  # no conflicts among these (6..) but includes pairs; C(10,2)=45
    # generous budget → no raise, incremental branch executed
    out = generate_apriori_candidates(
        bar_bits, condition_bits=bits, k=2, min_hits=0, n_bars=len(bar_bits), walk_rate=1e18
    )
    assert len(out) > 4  # crossed the stride at least once
    # tiny budget → raises mid-accumulation
    with pytest.raises(SweepAprioriBudgetError):
        generate_apriori_candidates(
            bar_bits,
            condition_bits=bits,
            k=2,
            min_hits=0,
            n_bars=len(bar_bits),
            cell_budget_s=1e-12,
            walk_rate=1.0,
        )


# ────────────────────────── validation guards ──────────────────────────


def test_guard_rejects_bit_at_or_above_64() -> None:
    with pytest.raises(ValueError, match="u128"):
        generate_apriori_candidates([1], condition_bits=[0, 64], k=1, min_hits=0)


def test_guard_rejects_negative_bit() -> None:
    with pytest.raises(ValueError, match="out of range"):
        generate_apriori_candidates([1], condition_bits=[-1, 3], k=1, min_hits=0)


def test_rejects_k_out_of_range() -> None:
    with pytest.raises(ValueError, match="invalid k"):
        generate_apriori_candidates([1], condition_bits=[0, 1, 2], k=4, min_hits=0)


@pytest.mark.parametrize("root_mod", [(0, 0), (2, 2), (-1, 3), (3, 2)])
def test_rejects_bad_root_mod(root_mod: tuple[int, int]) -> None:
    with pytest.raises(ValueError, match="root_mod"):
        generate_apriori_candidates([1], condition_bits=_BITS, k=1, min_hits=0, root_mod=root_mod)


def test_rejects_negative_n_bars() -> None:
    with pytest.raises(ValueError, match="n_bars"):
        generate_apriori_candidates([1], condition_bits=_BITS, k=1, min_hits=0, n_bars=-1)


@pytest.mark.parametrize("bad", [0.0, -1.0, float("inf"), float("nan")])
def test_rejects_non_positive_budget(bad: float) -> None:
    with pytest.raises(ValueError, match="cell_budget_s"):
        generate_apriori_candidates([1], condition_bits=_BITS, k=1, min_hits=0, cell_budget_s=bad)


@pytest.mark.parametrize("bad", [0.0, -1.0, float("inf"), float("nan")])
def test_rejects_non_positive_walk_rate(bad: float) -> None:
    with pytest.raises(ValueError, match="walk_rate"):
        generate_apriori_candidates([1], condition_bits=_BITS, k=1, min_hits=0, walk_rate=bad)


# ────────────────────────── env flag + resolvers ──────────────────────────


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
        ("garbage", False),
    ],
)
def test_apriori_enabled(monkeypatch: pytest.MonkeyPatch, raw: str | None, expected: bool) -> None:
    if raw is None:
        monkeypatch.delenv(SWEEP_APRIORI_ENV, raising=False)
    else:
        monkeypatch.setenv(SWEEP_APRIORI_ENV, raw)
    assert apriori_enabled() is expected


def test_resolve_cell_budget_s_default_and_valid() -> None:
    assert resolve_cell_budget_s({}) == DEFAULT_CELL_BUDGET_S
    assert resolve_cell_budget_s({CELL_BUDGET_ENV: "  "}) == DEFAULT_CELL_BUDGET_S
    assert resolve_cell_budget_s({CELL_BUDGET_ENV: "3600"}) == 3600.0


def test_resolve_walk_rate_default_and_valid() -> None:
    assert resolve_walk_rate({}) == DEFAULT_WALK_RATE
    assert resolve_walk_rate({WALK_RATE_ENV: "2.5e8"}) == 2.5e8


@pytest.mark.parametrize("bad", ["garbage", "nan", "inf", "-1", "0"])
def test_resolve_invalid_falls_back_loudly(bad: str) -> None:
    # Invalid / non-positive → the documented default, never a raise.
    assert resolve_cell_budget_s({CELL_BUDGET_ENV: bad}) == DEFAULT_CELL_BUDGET_S
    assert resolve_walk_rate({WALK_RATE_ENV: bad}) == DEFAULT_WALK_RATE


def test_resolvers_read_os_environ_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CELL_BUDGET_ENV, "1234.5")
    monkeypatch.setenv(WALK_RATE_ENV, "9.9e7")
    assert resolve_cell_budget_s() == 1234.5
    assert resolve_walk_rate() == 9.9e7


# ────────────────────────── internal helpers ──────────────────────────
# The subset-membership (``_in_sorted``: present / absent-mid / absent-past-end /
# empty) and subset-prune (``_all_subsets_frequent``: all-present / one-missing)
# numpy branches are exercised behaviourally by the oracle-parity + subset-prune
# tests above (they reach 100% line+branch without touching the private helpers).


# ────────────────────────── CI / mutation wiring pins ──────────────────────────


class TestWorkflowWiring:
    """Pin this module into its dedicated per-module coverage + mutation gates.

    Deterministic pins (the ``TestMutationWorkflowWiring`` /
    ``TestBenchWorkflowWiring`` precedent) that fail the unit suite if the
    module:suite pairing is ever silently dropped from the workflows — an
    unwired gate must never masquerade as an active one (Golden Rule #10).
    """

    _CI = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"
    _MUTATION = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "mutation.yml"

    def test_dedicated_100pct_coverage_gate_wired(self) -> None:
        text = self._CI.read_text(encoding="utf-8")
        assert "--cov=brutex.engine.sweep_apriori" in text
        assert "tests/unit/test_engine_sweep_apriori.py" in text

    def test_paired_in_the_mutmut_loop(self) -> None:
        text = self._MUTATION.read_text(encoding="utf-8")
        assert (
            '"brutex/engine/sweep_apriori.py:tests/unit/test_engine_sweep_apriori.py:"' in text
        ), "sweep_apriori.py is not wired into mutation.yml's per-module mutmut loop"


# ─────────────── run_persisted_sweep ON-path integration ───────────────
# Exercises the ``apriori_enabled()`` branch inside
# ``brutex.engine.sweep_run.run_persisted_sweep`` (the flag-ON enumeration
# source), a mini version of tests/golden/test_engine_sweep_apriori_parity.py so
# the ``--cov=brutex.engine`` (test_engine_*.py) + whole-package coverage gates
# see the branch. The byte-identity gate itself lives in the golden.


def _mini_bars() -> list:  # type: ignore[type-arg]
    from datetime import UTC, datetime, timedelta, timezone
    from decimal import Decimal

    from brutex.data.models import CandleBar

    ist = timezone(timedelta(hours=5, minutes=30))
    out = []
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


def test_run_persisted_sweep_on_path_matches_off(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    pytest.importorskip("polars")  # flush needs the [data] extra
    from brutex.data.option_dedup import OptionDedupIndex
    from brutex.engine.results_store import ResultsStore
    from brutex.engine.sweep_run import ComboDedup, SweepRunConfig, run_persisted_sweep
    from brutex.orchestrator.audit import AuditWriter
    from brutex.strategies.types import Side

    bars = _mini_bars()

    def run(tag: str) -> tuple[object, bytes]:
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

    monkeypatch.delenv(SWEEP_APRIORI_ENV, raising=False)
    off, off_bytes = run("off")
    monkeypatch.setenv(SWEEP_APRIORI_ENV, "1")
    on, on_bytes = run("on")

    assert on.store_content_hash == off.store_content_hash  # type: ignore[attr-defined]
    assert on_bytes == off_bytes
    assert on.enumerated < off.enumerated  # type: ignore[attr-defined]  # pruning fired
