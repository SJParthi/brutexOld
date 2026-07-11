"""OFF-mode cost gate + measured A/B for the Rust enum+prune kernel seam.

DEC-SWEEP-ENUM-RUST-001's hard constraint: with ``BRUTEX_SWEEP_RUST_ENUM``
OFF (the shipped default) the sweep's enumeration path must be byte-identical
AND cost-unchanged. Three complementary proofs + one honest measurement:

1. **Structural (the load-bearing one):** flag OFF, the seam probe
   (:func:`enum_prefilter_via_kernel`) returns ``None`` BEFORE any kernel
   resolution, and the sweep iterates the identical lazy
   ``iter_masks_k_of_bits`` generator (iteration-spy proven) — the OFF hot
   path is the pre-change hot path by construction.
2. **O(1)-in-workload seam cost:** the OFF probe (one env read + return) costs
   the same for a 4-bit and a 60-bit palette — it never touches the
   combination space. Ratio routed through the COMMON
   :func:`tests.bench._o1_tiers.ratio_ceiling`.
3. **ON-arm laziness:** flag ON + kernel served, the seam's fallback
   generator is CREATED but NEVER iterated (iteration-spy proven) — the
   deterministic twin of the wall-clock A/B, immune to CI timer noise.
4. **A/B segment measurement (REPORTED, not ratio-gated):** the enum+prune
   segment ON vs OFF at k=3 over the 60-bit vocabulary — printed as MEASURED
   context per Golden Rule #10 (the default-OFF slice ships inert, so a
   speedup GATE would gate nothing; the honest numbers live in the PR body /
   MIGRATION.md #1738 tagged with the machine they were measured on).
"""

from __future__ import annotations

import time
from collections.abc import Iterator, Sequence

import pytest
from brutex.engine import rust_enum
from brutex.engine.combo_mask import count_supermasks, iter_masks_k_of_bits
from brutex.engine.rust_enum import SWEEP_RUST_ENUM_ENV, enum_prefilter_via_kernel
from brutex.vocabulary import has_conflict

from tests.bench._o1_tiers import ratio_ceiling

_SMALL_BITS = [0, 2, 5, 7]
_LARGE_BITS = list(range(60))

#: A tiny shared bar window for the probe/A-B calls (the OFF probe never
#: reads it; the ON segment A/B uses min_hits=0 so the planes go unread).
_PROBE_BARS: list[int] = [0b1011, 0b0001, 0b1111] * 4


def _probe(bits: list[int], k: int, *, min_hits: int = 0) -> object:
    return enum_prefilter_via_kernel(
        bits,
        k,
        min_hits=min_hits,
        bar_bits=_PROBE_BARS,
        count_hits=lambda masks: count_supermasks(_PROBE_BARS, masks),
        digest_path=True,
    )


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch: pytest.MonkeyPatch) -> None:
    rust_enum.reset_observability_memo_for_tests()
    rust_enum.reset_enum_self_check_for_tests()
    monkeypatch.delenv(SWEEP_RUST_ENUM_ENV, raising=False)


def _median_probe_seconds(bits: list[int], k: int, *, rounds: int = 200) -> float:
    """Median wall-clock of ONE OFF-mode seam probe."""
    samples: list[float] = []
    for _ in range(rounds):
        t0 = time.perf_counter()
        result = _probe(bits, k)
        samples.append(time.perf_counter() - t0)
        assert result is None  # OFF on every sample — never a kernel engage
    samples.sort()
    return samples[len(samples) // 2]


class TestOffModeIsFree:
    def test_off_probe_never_resolves_the_kernel(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The structural zero-cost proof: OFF returns before ANY kernel work."""

        def _must_not_resolve(width: str) -> None:
            raise AssertionError("flag-OFF probe must never resolve the kernel")

        monkeypatch.setattr(rust_enum, "_resolve_kernel_class", _must_not_resolve)
        assert _probe(_LARGE_BITS, 3) is None

    def test_off_probe_cost_is_independent_of_the_combination_space(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The OFF probe never touches C(n, k): cost(60 bits, k=5) ≈ cost(4, 2).

        C(60, 5) is 455,126× C(4, 2) — any per-combination work in the OFF
        arm would blow this ratio; the probe is one env read + one return.
        """
        _median_probe_seconds(_SMALL_BITS, 2, rounds=50)  # warm-up
        t_small = _median_probe_seconds(_SMALL_BITS, 2)
        t_large = _median_probe_seconds(_LARGE_BITS, 5)
        # Sub-microsecond medians divide noisily; floor both at 100 ns so the
        # ratio compares real work, not timer granularity (house convention).
        floor = 1e-7
        ratio = max(t_large, floor) / max(t_small, floor)
        assert ratio <= ratio_ceiling(5.0), (
            f"OFF-mode probe cost scaled with the combination space: "
            f"{t_small * 1e9:.0f} ns @ C(4,2) vs {t_large * 1e9:.0f} ns @ C(60,5) "
            f"(ratio {ratio:.2f})"
        )


class TestOnArmStructure:
    def _iteration_spy(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, bool]:
        """Spy on the SEAM's generator: created / iterated flags."""
        state = {"created": False, "iterated": False}
        real = iter_masks_k_of_bits

        def _spying(bits: Sequence[int], k: int) -> Iterator[int]:
            state["created"] = True

            def _gen() -> Iterator[int]:
                for mask in real(bits, k):
                    state["iterated"] = True
                    yield mask

            return _gen()

        monkeypatch.setattr("brutex.engine.sweep_run.iter_masks_k_of_bits", _spying)
        return state

    def _run_cell(self, tmp_path: object, digest: str | None) -> None:
        """One tiny persisted sweep over a 5-bit cell (real sweep spine)."""
        from pathlib import Path

        from brutex.data.option_dedup import OptionDedupIndex
        from brutex.engine.results_store import ResultsStore
        from brutex.engine.sweep_run import ComboDedup, SweepRunConfig, run_persisted_sweep
        from brutex.orchestrator.audit import AuditWriter
        from brutex.strategies.types import Side

        from tests.unit.test_engine_sweep_run import _trending_bars

        assert isinstance(tmp_path, Path)
        cfg = SweepRunConfig(
            instrument="NIFTY",
            tf="1m",
            direction=Side.BUY,
            mode="A",
            condition_bits=[0, 1, 2, 3, 4],
            k=2,
            data_snapshot_hash="snap0001",
            indicator_version="ind-v1",
            code_commit_sha="deadbeef",
            tf_minutes=1,
            sl_paisa=80,
            tp_paisa=50,
            min_hits=0,
        )
        store = ResultsStore(path=tmp_path / "cell.parquet")
        dedup = ComboDedup(OptionDedupIndex(capacity=10_000, fpr_target=1e-6))
        with AuditWriter.open(tmp_path / "cell.jsonl") as audit:
            run_persisted_sweep(
                _trending_bars(),
                cfg,
                store=store,
                dedup=dedup,
                audit=audit,
                trial_stats_digest=digest,
            )

    def test_off_arm_iterates_the_python_generator(
        self, tmp_path: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Flag OFF: the sweep genuinely consumes the lazy Python generator."""
        state = self._iteration_spy(monkeypatch)
        self._run_cell(tmp_path, "abad1deaabad1dea")
        assert state["created"] is True
        assert state["iterated"] is True

    def test_on_arm_never_iterates_the_python_generator(
        self, tmp_path: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Flag ON + kernel served: the fallback generator is created lazily
        but NEVER iterated — the deterministic no-double-work proof."""
        monkeypatch.setenv(SWEEP_RUST_ENUM_ENV, "1")
        rust_enum.reset_enum_self_check_for_tests(masks_remaining=0)  # steady state
        state = self._iteration_spy(monkeypatch)
        self._run_cell(tmp_path, "abad1deaabad1dea")
        assert state["created"] is True
        assert state["iterated"] is False


class TestSegmentAbMeasured:
    def test_segment_ab_reported_not_gated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """MEASURED A/B of the enum+prune segment at k=3 (34,220 masks).

        REPORTED per Golden Rule #10, deliberately NOT ratio-gated on shared
        CI (default-OFF slice; the deterministic ON-arm proof is the
        iteration spy above). The only assertions are sanity: both arms
        produce the identical survivor list.
        """
        bits = _LARGE_BITS
        k = 3

        t0 = time.perf_counter()
        survivors_py = [m for m in iter_masks_k_of_bits(bits, k) if not has_conflict(m)]
        t_py = time.perf_counter() - t0

        monkeypatch.setenv(SWEEP_RUST_ENUM_ENV, "1")
        rust_enum.reset_enum_self_check_for_tests(masks_remaining=0)  # steady state
        t0 = time.perf_counter()
        result = _probe(bits, k)
        t_rs = time.perf_counter() - t0

        assert result is not None
        assert result.kept == survivors_py
        ratio = t_py / t_rs if t_rs > 0 else float("inf")
        print(
            f"\nenum+prune segment A/B (k={k}, C(60,3)={result.enumerated}): "
            f"python={t_py * 1e3:.1f} ms rust={t_rs * 1e3:.1f} ms "
            f"ratio={ratio:.1f}x (MEASURED on this runner — context only)"
        )
