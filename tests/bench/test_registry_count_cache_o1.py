"""O(1)-on-repeat gate for the /db registry pager COUNT cache (H1).

DEC-O1-GUARANTEE-HARDEN-001: the registry pagers were the ONE wholly-uncached
``/db`` pager — every click re-opened ``registry.duckdb`` and re-ran a
FILTERED ``COUNT(*)`` over the monotonically-growing contracts table
(O(filtered rows) per click). The totals are paging-invariant, so they are now
memoised per registry CONTENT (the #867 ``candle_count_key`` idiom over the
``registry_revision`` stat token). Two gates:

* DETERMINISTIC (primary teeth): a repeat/paging click within one generation
  triggers EXACTLY ONE store ``COUNT(*)`` — a per-click recount regression
  fails on the spy count, no wall clock involved;
* WALL-CLOCK: the cached repeat-click latency is flat vs the contracts-table
  depth (10× more legs < 8× shared-CI ceiling via :func:`ratio_ceiling`;
  honest tier note — this file is NOT in bench-m4pro.yml's curated strict
  list, so the 1.1× strict ceiling is dormant here unless the file joins that
  list — the shared 8× ceiling is the operative gate. DuckDB I/O noise
  matches the sibling ``test_registry_pagination_o1.py`` gate).

HONEST envelope (Golden Rule #10): O(1)-on-repeat — the FIRST count per
registry revision still runs the real filtered ``COUNT(*)``, and the page
DATA query stays a per-click ``LIMIT``/``OFFSET`` SELECT — O(page) in RESULT
size only: under the covering ``ORDER BY`` DuckDB still SCANS the filtered
rows into a top-(offset+limit) heap, so scan TIME is O(filtered rows),
bounded per underlying (the same caveat the candle pager's D1 row spells
out; gated by the sibling bench). Never a bare-O(1)/bare-O(page) claim.
"""

from __future__ import annotations

import time
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from tests.bench._o1_tiers import ratio_ceiling

pytest.importorskip("duckdb")

from brutex.dashboard.db_view import list_registry_contracts
from brutex.dashboard.movers_cache import MoversSnapshotCache
from brutex.data.registry_store import RegistryStore

#: Leg counts the CACHED repeat click must be independent of (10× difference).
_SMALL = 200
_LARGE = 2000
#: Page the viewer requests (first page, default limit).
_LIMIT = 100
#: How many repeat clicks to time at each size (averaged).
_QUERIES = 25

_NOW = datetime(2026, 6, 14, 9, 15, tzinfo=UTC)


def _build(path: Path, legs: int) -> None:
    """Seed a registry with *legs* NIFTY contract legs across 1 expiry."""
    with RegistryStore.open(path) as store:
        exp = date(2020, 1, 30)
        store.upsert_expiry("NSE", "NIFTY", exp, run_id="r1", fetched_at=_NOW)
        for i in range(legs):
            store.upsert_contract(
                "NSE",
                "NIFTY",
                exp,
                f"NSE-NIFTY-30Jan20-{i:05d}-CE",
                side="CE",
                strike=10000.0 + i,
                run_id="r1",
                fetched_at=_NOW,
            )


def _click(path: Path, cache: MoversSnapshotCache, *, offset: int = 0) -> dict[str, object]:
    """One viewer click — the cached-count pager read at a fixed generation."""
    return list_registry_contracts(
        path,
        underlying="NIFTY",
        limit=_LIMIT,
        offset=offset,
        count_cache=cache,
        generation=1,
    )


class TestRegistryCountCachedOncePerToken:
    """DETERMINISTIC: one filtered COUNT per (key, generation) — never per click."""

    def test_repeat_and_paging_clicks_count_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "registry.duckdb"
        _build(path, _SMALL)
        counts = [0]
        real = RegistryStore.count_contracts

        def _spy(self: RegistryStore, **kwargs: object) -> int:
            counts[0] += 1
            return real(self, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(RegistryStore, "count_contracts", _spy)
        cache = MoversSnapshotCache()
        totals = {_click(path, cache, offset=n)["total"] for n in (0, 0, 100, 0)}
        assert totals == {_SMALL}
        assert counts[0] == 1, (
            f"expected ONE filtered COUNT for four clicks within one generation, "
            f"measured {counts[0]} — the pager count cache has regressed to per-click"
        )

    def test_generation_move_recounts_exactly_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "registry.duckdb"
        _build(path, _SMALL)
        counts = [0]
        real = RegistryStore.count_contracts

        def _spy(self: RegistryStore, **kwargs: object) -> int:
            counts[0] += 1
            return real(self, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(RegistryStore, "count_contracts", _spy)
        cache = MoversSnapshotCache()
        _click(path, cache)
        assert counts[0] == 1
        # A registry-content move (new generation) recounts ONCE, then memoises.
        for _ in range(3):
            list_registry_contracts(
                path,
                underlying="NIFTY",
                limit=_LIMIT,
                offset=0,
                count_cache=cache,
                generation=2,
            )
        assert counts[0] == 2


class TestRegistryCachedClickLatencyIsFlat:
    """WALL-CLOCK: the cached repeat click is flat vs contracts-table depth."""

    def test_cached_click_ratio_below_8x(self, tmp_path: Path) -> None:
        """Repeat-click latency at 2000 vs 200 legs stays flat (< 8×).

        The cached click is the DATA query (O(page) result; the ORDER BY
        scan is O(filtered rows) — bounded per underlying, the D1-class
        caveat) + an O(1) memo hit — the pre-H1 per-click filtered
        ``COUNT(*)`` growth term is gone. DuckDB I/O noise matches the
        sibling pagination gate's 8× shared ceiling.
        """

        def _time(path: Path, legs: int) -> float:
            _build(path, legs)
            cache = MoversSnapshotCache()
            for _ in range(5):  # warm — the FIRST click pays the real COUNT
                _click(path, cache)
            t0 = time.perf_counter_ns()
            for _ in range(_QUERIES):
                _click(path, cache)
            return (time.perf_counter_ns() - t0) / _QUERIES

        t_small = _time(tmp_path / "rs.duckdb", _SMALL)
        t_large = _time(tmp_path / "rl.duckdb", _LARGE)
        ratio = t_large / max(t_small, 1.0)
        assert ratio < ratio_ceiling(8.0), (
            f"cached registry click latency ratio {ratio:.2f}× ({_LARGE} vs "
            f"{_SMALL} legs) exceeds 8× — the memoised COUNT may have regressed "
            f"to a per-click recount. t_small={t_small:.0f}ns, t_large={t_large:.0f}ns."
        )
