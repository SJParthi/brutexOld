"""M4 Pro absolute-throughput gates — STANDARDS row 14 closure path.

Per CLAUDE.md Golden Rule #12 / `STANDARDS_100PCT.md` §1 row 14 the
operator's "100% standards" decree mandates a throughput canary of
**≥ 50 M ops/s** for the brute-force mask-AND hot path. The
existing ``test_throughput_canary.py`` enforces only the **ratio**
(p99(N=1M) / p99(N=100K) ≤ 8×) because shared GitHub Actions
runners have noisy cache-cliff behaviour that chronic-flakes any
absolute ops/s assertion.

Per CLAUDE.md §8 the production envelope is an **Apple M4 Pro,
14-core CPU (10P + 4E), 48 GB RAM**. Local M4 Pro benchmarks
(operator log 2026-05-22) hit ~512 M ops/s on the numpy fast-path
and ~2.3 M ops/s on the scalar-Python slow-path — well above the
50 M floor for the numpy gate.

This file gates the **absolute** floor and **skips by default**.
The gate activates only when:

1. The env var ``BRUTEX_M4PRO_ABS=1`` is set, AND
2. The workflow runs on a self-hosted runner labelled ``m4pro``
   (gated externally via ``runs-on: [self-hosted, macos, m4pro]``
   in ``.github/workflows/bench-m4pro.yml``).

On shared GHA runners (no env var, no label) every test in this
module skips with a clear reason — no flakes, no false fails.

## Why a separate file (not adding to test_throughput_canary.py)

Keeping the absolute gate in its own file lets the ratio gate keep
running on every push (cheap, stable) while the absolute gate runs
only on the M4 Pro runner. The two files share the same input
strategy (``_make_bar_bits_array``) so future refactors stay
single-source.

## Activation checklist

After registering the M4 Pro self-hosted runner:

1. GitHub → ``sjparthi/brutex`` → Settings → Actions → Runners →
   "New self-hosted runner" → macOS. Register with labels
   ``self-hosted, macos, m4pro``.
2. The workflow ``.github/workflows/bench-m4pro.yml`` is already
   wired with ``BRUTEX_M4PRO_ABS=1`` and ``runs-on:
   [self-hosted, macos, m4pro]`` — no further change.
3. On the next push, the absolute gate runs on the M4 Pro and
   fails the build if numpy ops/s drops below 50 M.

## Decision

DEC-M4PRO-ABSOLUTE-001 (this file, 2026-05-25, MIGRATION entry
#236). Closes STANDARDS row 14 partial → 🟢 once the runner is
live.
"""

from __future__ import annotations

import os
import time
from typing import Final

import numpy as np
import pytest

# ────────────────────────────────────────────────────────────────────────
# Constants
# ────────────────────────────────────────────────────────────────────────

#: Operator-decreed absolute floor per STANDARDS_100PCT.md §1 row 14.
#: On M4 Pro the local benchmark hits ~512 M ops/s; 50 M is the
#: floor below which a real regression has occurred.
M4PRO_ABSOLUTE_FLOOR_OPS_PER_SEC: Final[int] = 50_000_000

#: Sample size for the absolute benchmark. Large enough to amortise
#: numpy setup cost; small enough to keep the test < 1s on M4 Pro.
_SAMPLE_N: Final[int] = 1_000_000

#: How many iterations to average per measurement.
_ITER_COUNT: Final[int] = 5

#: Reuse the same 4-bit mask the ratio canary uses for parity.
_REQUIRED_MASK: Final[int] = 0b1011_0010_1100_0000_0000_0000_0000_0000

#: Env-var gate. Set to "1" on the M4 Pro runner; left unset on
#: shared GHA so the tests skip without flaking.
_ENV_M4PRO_ABS: Final[str] = "BRUTEX_M4PRO_ABS"


# ────────────────────────────────────────────────────────────────────────
# Test gating
# ────────────────────────────────────────────────────────────────────────


def _m4pro_gate_active() -> bool:
    """Return True iff ``BRUTEX_M4PRO_ABS=1`` is set."""
    return os.environ.get(_ENV_M4PRO_ABS, "").strip() == "1"


skip_unless_m4pro = pytest.mark.skipif(
    not _m4pro_gate_active(),
    reason=(
        f"{_ENV_M4PRO_ABS}=1 not set — absolute throughput gate runs "
        "only on the M4 Pro self-hosted runner (skipping on shared GHA "
        "to avoid chronic flake per DEC-P0-020 honest-envelope)"
    ),
)


# ────────────────────────────────────────────────────────────────────────
# Fixture builder (deterministic random uint64 bar bits)
# ────────────────────────────────────────────────────────────────────────


def _make_bar_bits_array(n: int) -> np.ndarray:
    """Deterministic seed → same array each run. Bit pattern uniformly random."""
    rng = np.random.default_rng(seed=42)
    return rng.integers(0, 2**63, size=n, dtype=np.uint64)


# ────────────────────────────────────────────────────────────────────────
# Absolute-floor gate
# ────────────────────────────────────────────────────────────────────────


class TestM4ProAbsoluteThroughput:
    """Gates ONLY active under ``BRUTEX_M4PRO_ABS=1``.

    Otherwise every test in this class skips, which keeps the file safe
    on shared CI (the env var is set only by ``bench-m4pro.yml``). The
    M4 Pro self-hosted runner is REGISTERED and LIVE since 2026-07-07 (the
    workflow header's "RUNNER IS LIVE" note), so these gates genuinely
    execute there on that workflow's triggers.
    """

    @skip_unless_m4pro
    @pytest.mark.bench
    def test_numpy_mask_and_meets_50m_ops_per_sec(self) -> None:
        """Vectorised mask AND must clear the operator-decreed 50 M ops/s floor.

        On M4 Pro the typical headroom is ~10× (512 M ops/s observed
        local). If this gate ever fails it points at a real regression
        (CPU-pinning change, OS energy mode shift, numpy ABI break).

        Per STANDARDS row 14: this is the **absolute** gate; the **ratio**
        gate in ``test_throughput_canary.py`` runs everywhere.
        """
        # Warm-up numpy + cache.
        warmup = _make_bar_bits_array(100_000)
        for _ in range(3):
            _ = (warmup & _REQUIRED_MASK) == _REQUIRED_MASK

        arr = _make_bar_bits_array(_SAMPLE_N)
        t0 = time.perf_counter_ns()
        for _ in range(_ITER_COUNT):
            _ = (arr & _REQUIRED_MASK) == _REQUIRED_MASK
        t1 = time.perf_counter_ns()

        elapsed_seconds = (t1 - t0) / 1e9
        total_ops = _SAMPLE_N * _ITER_COUNT
        ops_per_sec = total_ops / elapsed_seconds

        print(
            f"\nM4 Pro numpy mask AND: {ops_per_sec:,.0f} ops/s "
            f"(floor {M4PRO_ABSOLUTE_FLOOR_OPS_PER_SEC:,.0f})"
        )

        assert ops_per_sec >= M4PRO_ABSOLUTE_FLOOR_OPS_PER_SEC, (
            f"M4 Pro absolute throughput {ops_per_sec:,.0f} ops/s is "
            f"BELOW the operator-decreed floor of "
            f"{M4PRO_ABSOLUTE_FLOOR_OPS_PER_SEC:,.0f} ops/s. "
            "This indicates a real regression — investigate CPU pinning, "
            "OS energy-mode, numpy ABI, or thermal throttling."
        )


# ────────────────────────────────────────────────────────────────────────
# Smoke: the gate machinery itself is testable WITHOUT the runner
# ────────────────────────────────────────────────────────────────────────


class TestGateMachinery:
    """These tests run on EVERY runner (no skip). They verify the gate
    decoder itself, independent of whether the gate fires."""

    def test_gate_inactive_without_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(_ENV_M4PRO_ABS, raising=False)
        assert not _m4pro_gate_active()

    def test_gate_active_with_env_set_to_1(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(_ENV_M4PRO_ABS, "1")
        assert _m4pro_gate_active()

    @pytest.mark.parametrize("val", ["", "0", "false", "yes", "true", " 1 "])
    def test_gate_inactive_for_non_one_values(
        self, monkeypatch: pytest.MonkeyPatch, val: str
    ) -> None:
        """Only the literal string ``"1"`` (after strip) enables the gate.

        Avoids accidental activation by ``BRUTEX_M4PRO_ABS=true`` etc.
        — the env var has exactly one canonical truthy value to keep
        CI predictability.
        """
        monkeypatch.setenv(_ENV_M4PRO_ABS, val)
        # " 1 " strips to "1" so that one DOES activate; the others don't.
        if val.strip() == "1":
            assert _m4pro_gate_active()
        else:
            assert not _m4pro_gate_active()

    def test_floor_constant_matches_operator_decree(self) -> None:
        """STANDARDS_100PCT.md §1 row 14 specifies 50 M ops/s."""
        assert M4PRO_ABSOLUTE_FLOOR_OPS_PER_SEC == 50_000_000

    def test_sample_n_is_one_million(self) -> None:
        """1M sample = same scale as the ratio canary's `large` bucket."""
        assert _SAMPLE_N == 1_000_000
