"""O(1) benchmark for the BLAKE3 combo-identity hash.

DEC-O1-BENCH-GAPS-001 (G1 of the 4-gap deep audit). CLAUDE.md Golden Rule #4
(reproducibility) identifies every combo by a BLAKE3 digest of its 9-field
identity (``brutex/strategies/combo.py:combo_hash`` →
``fno_math.blake3_hash``), and Rule #5 requires combo dedup to be O(1). The
deep audit flagged the hash primitive as a YELLOW gap: architecturally O(1) per
fixed-size combo, but never directly benched.

This closes it. :func:`combo_hash` is a **stateless pure function** over a
fixed-width canonical payload, so its per-call latency must be:

* independent of how many combos were hashed before it (no accumulating state /
  accidental memoization that would make sweep-wide dedup hashing super-linear),
  and
* constant for the canonical 9-field identity (the brute-force sweep hashes one
  such identity per combo).

A regression that introduced per-call growth (e.g. an unbounded cache keyed by
payload) would blow the ratio and fail the gate.

Mirrors the ``_o1_tiers`` two-tier pattern via :func:`ratio_ceiling` —
robust ``< 5×`` on shared CI. Honest tier note: this file is NOT in
bench-m4pro.yml's curated strict list, so the decreed 1.1× M4-Pro strict
ceiling is DORMANT here unless the file joins that list; the shared robust
ceiling is the operative gate.
"""

from __future__ import annotations

import time
from typing import Final

from brutex.strategies.combo import canonicalise_params, combo_hash

from tests.bench._o1_tiers import ratio_ceiling

#: Per-call latency must not grow between these two call-batch sizes.
_SMALL = 1_000
_LARGE = 100_000

#: A canonical 9-field combo identity (the real dedup primitive's input).
_PARAMS: Final[dict[str, object]] = {"k": 2, "sl": 1500, "tp": 3000, "min_hits": 30}


def _hash_once(i: int) -> str:
    """Hash one canonical combo identity (vary only the strategy id)."""
    return combo_hash(
        strategy_id=f"mask-{i & 0xFFFF}",
        direction="BUY",
        instrument="NSE:NIFTY",
        tf="5minute",
        mode="signal",
        params=_PARAMS,
        data_snapshot_hash="a" * 64,
        indicator_version="v1",
        code_commit_sha="b" * 40,
    )


class TestComboHashIsO1:
    """``combo_hash`` is O(1) per call — stateless, no growth over a sweep."""

    def test_hash_is_64_hex(self) -> None:
        digest = _hash_once(0)
        assert len(digest) == 64 and int(digest, 16) >= 0

    def test_hash_at_small_scale(self, benchmark) -> None:
        assert len(benchmark(lambda: _hash_once(7))) == 64

    def test_hash_at_large_scale(self, benchmark) -> None:
        # Same per-call work as small scale; benchmarked separately so the
        # harness records both points (the ratio test below is the gate).
        assert len(benchmark(lambda: _hash_once(123_457))) == 64

    def test_hash_ratio_below_5x(self) -> None:
        """Per-call latency over a 100k batch must match a 1k batch (< 5×)."""

        def _time(n: int) -> float:
            for i in range(50):  # warm
                _ = _hash_once(i)
            t0 = time.perf_counter_ns()
            for i in range(n):
                _ = _hash_once(i)
            t1 = time.perf_counter_ns()
            return (t1 - t0) / n

        t_small = _time(_SMALL)
        t_large = _time(_LARGE)
        ratio = t_large / max(t_small, 1.0)
        assert ratio < ratio_ceiling(5.0), (
            f"combo_hash per-call latency ratio {ratio:.2f}× (100k vs 1k calls) "
            f"exceeds 5× — the hash may have acquired growing per-call state "
            f"(O(1) dedup-hashing regression). "
            f"t_1k={t_small:.1f}ns, t_100k={t_large:.1f}ns."
        )


def _params_with_extras(n_keys: int) -> dict[str, object]:
    """The canonical 4-key params padded with *n_keys* synthetic int knobs."""
    padded: dict[str, object] = dict(_PARAMS)
    for i in range(n_keys):
        padded[f"p{i:05d}"] = i * 7
    return padded


def _hash_params(params: dict[str, object]) -> str:
    """Hash one combo identity carrying *params* (all other fields fixed)."""
    return combo_hash(
        strategy_id="mask-7",
        direction="BUY",
        instrument="NSE:NIFTY",
        tf="5minute",
        mode="signal",
        params=params,
        data_snapshot_hash="a" * 64,
        indicator_version="v1",
        code_commit_sha="b" * 40,
    )


def _time_params(params: dict[str, object], *, iterations: int) -> float:
    """Per-call ns for hashing ONE identity shape, warmed then averaged."""
    for _ in range(50):
        _ = _hash_params(params)
    t0 = time.perf_counter_ns()
    for _ in range(iterations):
        _ = _hash_params(params)
    t1 = time.perf_counter_ns()
    return (t1 - t0) / iterations


class TestComboHashPayloadCost:
    """The payload-size cost law (DEC-O1-GUARANTEE-HARDEN-001 H4e).

    HONEST envelope (Golden Rule #10): ``combo_hash`` canonicalises + hashes
    its params payload, so its per-call cost is inherently O(payload bytes) —
    a FLAT-vs-payload pin is not physical and is deliberately NOT claimed
    (measured 2026-07-13 sandbox: 41 B baseline → 6.3 µs; a 58 KB synthetic
    payload → 1.52 ms, tracking the byte ratio almost exactly). What IS
    pinned: (a) the largest REAL production params shape stays a small
    bounded factor of the baseline, and (b) the growth is at-most-LINEAR in
    payload bytes — a superlinear canonicalisation regression (an O(n²)
    re-sort / re-encode per key) blows the ratio and fails loudly.
    """

    def test_realistic_roll_schedule_shape_is_bounded(self) -> None:
        """The futures ``roll_schedule`` identity costs < 8× the baseline.

        The ADR-0012 ``NSE-NIFTY-FUT`` track is the ONE production combo whose
        ``canonical_trade_params`` carries a large member — the roll schedule
        of ~80 owning-expiry ordinals (2020 → today, one per monthly contract;
        DEC-STAGE1A-ROLL-RESET-001). Measured 2026-07-13 sandbox: 405 B
        payload → 22.3 µs/call vs the 41 B baseline's 6.3 µs = 3.54× (payload
        physics — bytes grew ~9.9×, the fixed identity fields amortise the
        rest). The 8× ceiling is a PLAIN constant, deliberately NOT routed
        through ``ratio_ceiling`` — the strict 1.1× tier is an O(1) law and
        this ratio is a genuine, disclosed payload cost, not noise.
        """
        roll_params = dict(_PARAMS, roll_schedule=tuple(range(0, 80 * 21, 21)))
        t_base = _time_params(_PARAMS, iterations=5_000)
        t_roll = _time_params(roll_params, iterations=5_000)
        ratio = t_roll / max(t_base, 1.0)
        assert ratio < 8.0, (
            f"combo_hash on the realistic roll_schedule identity is {ratio:.2f}× "
            f"the 9-field baseline (>= 8×) — the futures-track hash cost has "
            f"regressed beyond payload physics. "
            f"t_base={t_base:.0f}ns, t_roll={t_roll:.0f}ns."
        )

    def test_payload_growth_is_at_most_linear(self) -> None:
        """10× the payload bytes must cost at most ~10× the time (linear law).

        Measured 2026-07-13 sandbox: 400 extra keys (5,481 B) → 143.7 µs;
        4,000 extra keys (58,452 B) → 1,521 µs — a 10.66× byte ratio costing
        10.59× the time (linear to within noise). A superlinear regression
        reads ~byte_ratio² ≈ 114× here, far above the 2× headroom.
        """
        mid = _params_with_extras(400)
        big = _params_with_extras(4_000)
        bytes_mid = len(canonicalise_params(mid).encode())
        bytes_big = len(canonicalise_params(big).encode())
        byte_ratio = bytes_big / bytes_mid
        assert byte_ratio > 5.0  # the workload spread is real, not degenerate
        t_mid = _time_params(mid, iterations=1_000)
        t_big = _time_params(big, iterations=200)
        time_ratio = t_big / max(t_mid, 1.0)
        assert time_ratio < byte_ratio * 2.0, (
            f"combo_hash payload scaling is SUPERLINEAR: {byte_ratio:.2f}× the "
            f"bytes cost {time_ratio:.2f}× the time (allowed < {byte_ratio * 2.0:.1f}×) "
            f"— the canonicalisation may have regressed past O(payload bytes). "
            f"t_mid={t_mid:.0f}ns ({bytes_mid}B), t_big={t_big:.0f}ns ({bytes_big}B)."
        )
