"""O(1) benchmark for the cache-first NTM constituent load (DEC-O1-BENCH-GAP-CLOSURE-001).

Gates the load-bearing claim of ``brutex/dashboard/ntm_cache.py`` +
``instrument_loader._resolve_constituent_csv`` (DEC-NTM-CACHE-FIRST-001): once a
fresh last-good constituent cache exists on disk, a ``/universe`` load resolves the
~750-stock NTM universe from it **with ZERO network calls** — the operator's
30–61 s niftyindices block (which cratered the universe to 211 stocks on a timeout)
is paid at most once per TTL, never per load.

Two things are pinned here (the module has correctness tests, but no perf gate):

1. **Zero network on the warm path (the load-bearing guarantee).** The ``opener``
   seam is a function that RAISES if called. A ``cache_fresh`` resolve that returns
   without raising PROVES the request thread issued no live fetch. This is binary
   and the primary gate — it is the exact regression that caused the incident.
2. **Bounded read vs constituent count.** The warm resolve's only cost is a small
   ``stat`` + ``read_text`` of the cached CSV, so its latency is bounded (and far
   below any network round-trip) as the constituent count grows. Asserted flat
   under ``ratio_ceiling(5.0)`` (``tests/bench/_o1_tiers.py``).

HONEST envelope (Golden Rule #10): the warm resolve is NOT literally O(1) over the
CSV — it reads the whole file each call (the module docstring says so:
"constant w.r.t. the slow niftyindices host … NOT a literal O(1) over the CSV").
So this gate pins the two guarantees that matter — **no network** and a read cost
that stays a small multiple as the list grows — not a false flat-over-bytes claim.
Best-of-N-MIN timing (DEC-CANARY-MINTIME-001) keeps the shared-runner ratio robust.
"""

# Whitebox: drives the private ``_resolve_constituent_csv`` cache-first seam, so per
# the DEC-P0-017 convention the file silences ``reportPrivateUsage``.
# pyright: reportPrivateUsage=none

from __future__ import annotations

import time
from pathlib import Path

import pytest
from brutex.dashboard.instrument_loader import _resolve_constituent_csv
from brutex.dashboard.ntm_cache import save_constituent_csv

from tests.bench._o1_tiers import ratio_ceiling


def _raise_if_called(_url: str) -> str:
    """An ``opener`` that fails the test if the warm path ever hits the network."""
    raise AssertionError("live niftyindices fetch issued on the warm-cache path")


def _seed_cache(cache_dir: Path, n_rows: int) -> None:
    """Write a fresh last-good constituent CSV of *n_rows* rows (mtime = now → fresh)."""
    header = "Company Name,Industry,Symbol,Series,ISIN Code\n"
    body = "\n".join(f"Co{i},Financial Services,SYM{i},EQ,INE{i:09d}01" for i in range(n_rows))
    assert save_constituent_csv(header + body, cache_dir=cache_dir) is True


def _warm_resolve(cache_dir: Path) -> tuple[str, str]:
    """Resolve with the raising opener — a fresh cache must serve without any fetch."""
    return _resolve_constituent_csv(_raise_if_called, cache_dir=cache_dir)


class TestWarmCacheZeroNetwork:
    """A fresh-cache resolve does zero network and stays bounded vs constituent count."""

    def test_warm_resolve_issues_no_network_call(self, tmp_path: Path) -> None:
        _seed_cache(tmp_path, 750)
        csv, source = _warm_resolve(tmp_path)  # would AssertionError if the opener ran
        assert source == "cache_fresh"
        assert "SYM749" in csv

    def test_warm_resolve_latency_flat_vs_constituent_count(
        self, tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        """Warm-resolve latency at many vs few constituents stays under the ceiling."""
        small_dir = tmp_path
        big_dir = tmp_path_factory.mktemp("ntm_big")
        _seed_cache(small_dir, 50)
        _seed_cache(big_dir, 5_000)
        # Both must be zero-network fresh serves.
        assert _warm_resolve(small_dir)[1] == "cache_fresh"
        assert _warm_resolve(big_dir)[1] == "cache_fresh"

        def _per_call(cache_dir: Path, iterations: int) -> float:
            best = float("inf")
            for _ in range(5):  # best-of-N-MIN (DEC-CANARY-MINTIME-001)
                t0 = time.perf_counter_ns()
                for _ in range(iterations):
                    _resolve_constituent_csv(_raise_if_called, cache_dir=cache_dir)
                best = min(best, (time.perf_counter_ns() - t0) / iterations)
            return best

        _per_call(small_dir, 5_000)  # warm
        small = _per_call(small_dir, 20_000)
        big = _per_call(big_dir, 20_000)
        ratio = big / max(small, 1.0)
        assert ratio < ratio_ceiling(5.0), (
            f"NTM warm-resolve latency ratio {ratio:.2f}× (5000 vs 50 constituents) exceeds "
            f"the 5× CI ceiling — the cache-first read may have regressed to a live fetch or "
            f"a super-linear parse. small={small:.0f}ns big={big:.0f}ns."
        )
