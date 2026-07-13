"""Cost gate for the sweep-cache resume Bloom rebuild (H4g, OPTIONAL item).

DEC-O1-GUARANTEE-HARDEN-001: ``SweepCache.open`` on a RESUME repopulates the
fresh in-RAM L1 Bloom from every digest persisted in the L2 LMDB exact store
(``_rebuild_bloom_from_exact`` — the resume-dedup guarantee, CLAUDE.md §2
Rules 4 & 7). Its own docstring claims **O(persisted), paid exactly once at
startup** — but no bench gate pinned that law (the reader-B missing-gates
census), so a superlinear regression (a per-digest re-scan, an accidental
per-add Bloom rebuild) would ship silently and turn a large-grid resume's
startup into minutes.

The gate: rebuild wall time at 100k persisted digests must stay within a
LINEAR factor of the 10k rebuild — ``total(100k) < total(10k) × 20`` (the 10×
digest ratio with 2× noise headroom). Honest ceiling calibration (review
round 1, measured 3× 2026-07-13 sandbox): the ratio SPANS ~1.3× on a cold
process (one-time import/LMDB overhead inflates ``t_small`` — 57.0 ms →
76.4 ms) up to **9.17×** on a warm process (7.9 ms → 72.9 ms — approaching
the true 10× linear term), so a tightened 8× ceiling would FALSE-FAIL warm
runs; 20× stands as the deliberate LINEARITY-ONLY pin. What it catches: a
quadratic regression reads ≥ 50–100× here even with the fixed overhead
diluting it — far above the ceiling; what it deliberately does NOT catch: a
mild superlinear (O(n log n) ≈ 12×) can slip under 20× — disclosed, the
quadratic class is this gate's teeth. Plus the correctness tooth: a
persisted digest is SEEN after the reopen (the Bloom genuinely rebuilt —
a Bloom false-negative would re-run the recorded combo).

Seeding writes the persisted ground truth directly through
``LmdbMembership.add_many`` (ONE transaction — the durable state a crashed
run leaves behind; per-``record_digest`` seeding measured ~1.4 ms/put and
would blow the bench budget without changing what the gate times).

HONEST envelope (Golden Rule #10): the rebuild is O(persisted) BY DESIGN —
this is a LINEARITY pin, deliberately NOT an O(1) claim; wall times include
the constant LMDB-open + Bloom-init overhead (disclosed above).
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path

import pytest

pytest.importorskip("lmdb", reason="the L2 exact store needs the lmdb runtime dep")
pytest.importorskip("fastbloom_rs", reason="the L1 Bloom needs fastbloom-rs")

from brutex.data.lmdb_membership import LmdbMembership
from brutex.engine.sweep_cache import SweepCache

_SMALL = 10_000
_LARGE = 100_000
#: Linear law: 10× the persisted digests may cost at most 10× × 2 headroom.
_LINEAR_CEILING = 20.0
#: Bloom capacity comfortably above _LARGE so no auto-grow arm engages.
_CAPACITY = 200_000


def _digest(i: int) -> bytes:
    return hashlib.blake2b(i.to_bytes(8, "little"), digest_size=32).digest()


def _seed_exact(root: Path, n: int) -> None:
    """Persist *n* digests as the durable L2 ground truth a resume reloads."""
    root.mkdir(parents=True)
    with LmdbMembership.open(root / "exact") as exact:
        added = exact.add_many(_digest(i) for i in range(n))
        assert added == n


def _time_rebuild(root: Path, n: int) -> float:
    """Wall ns for ONE resume open (the rebuild) over *n* persisted digests."""
    _seed_exact(root, n)
    t0 = time.perf_counter_ns()
    with SweepCache.open(root, bloom_capacity=_CAPACITY) as cache:
        t1 = time.perf_counter_ns()
        # Correctness tooth: the rebuilt Bloom reflects the persisted ground
        # truth — a false-negative here means the resume would re-run a
        # recorded combo (the exact failure the rebuild exists to prevent).
        assert cache.seen_digest(_digest(n // 2))
        assert not cache.seen_digest(_digest(n + 1))
    return float(t1 - t0)


class TestBloomResumeRebuildIsLinear:
    def test_rebuild_scales_linearly_with_persisted_digests(self, tmp_path: Path) -> None:
        t_small = _time_rebuild(tmp_path / "small", _SMALL)
        t_large = _time_rebuild(tmp_path / "large", _LARGE)
        ratio = t_large / max(t_small, 1.0)
        assert ratio < _LINEAR_CEILING, (
            f"resume Bloom rebuild ratio {ratio:.2f}× ({_LARGE:_} vs {_SMALL:_} "
            f"persisted digests) exceeds the linear ceiling {_LINEAR_CEILING}× — "
            f"_rebuild_bloom_from_exact may have regressed past O(persisted). "
            f"t_small={t_small / 1e6:.1f}ms, t_large={t_large / 1e6:.1f}ms."
        )
