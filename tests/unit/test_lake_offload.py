"""Unit tests for the dedicated lake-offload pool (DEC-DASHBOARD-READPATH-WEDGE-001).

Cover :mod:`brutex.dashboard.lake_offload` to 100% line+branch: the env-resolved
token budget (fail-loud), the ``LakeOffloadPool`` construction arms (default /
injected limit / injected limiter), and the ``run`` thread-hop that accounts an
offload against THIS pool's disjoint :class:`anyio.CapacityLimiter` (arg/kwarg
binding + result pass-through + genuine bounding).
"""

from __future__ import annotations

import threading

import anyio
import anyio.to_thread
import pytest
from anyio import CapacityLimiter
from brutex.dashboard.lake_offload import (
    DEFAULT_LAKE_OFFLOAD_LIMIT,
    LAKE_OFFLOAD_LIMIT_ENV,
    LakeOffloadPool,
    resolve_lake_offload_limit,
)


class TestResolveLakeOffloadLimit:
    """The env-overridable, fail-loud token-budget resolver."""

    def test_blank_env_uses_default(self) -> None:
        assert resolve_lake_offload_limit(env={}) == DEFAULT_LAKE_OFFLOAD_LIMIT

    def test_whitespace_only_uses_default(self) -> None:
        assert resolve_lake_offload_limit(env={LAKE_OFFLOAD_LIMIT_ENV: "  "}) == (
            DEFAULT_LAKE_OFFLOAD_LIMIT
        )

    def test_valid_override(self) -> None:
        assert resolve_lake_offload_limit(env={LAKE_OFFLOAD_LIMIT_ENV: "3"}) == 3

    def test_non_integer_raises(self) -> None:
        with pytest.raises(ValueError, match="not an integer"):
            resolve_lake_offload_limit(env={LAKE_OFFLOAD_LIMIT_ENV: "eight"})

    def test_below_one_raises(self) -> None:
        with pytest.raises(ValueError, match=">= 1"):
            resolve_lake_offload_limit(env={LAKE_OFFLOAD_LIMIT_ENV: "0"})

    def test_default_reads_os_environ(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``env=None`` reads ``os.environ`` (the production path)."""
        monkeypatch.setenv(LAKE_OFFLOAD_LIMIT_ENV, "5")
        assert resolve_lake_offload_limit() == 5


class TestLakeOffloadPoolConstruction:
    """The three construction arms + the read-only properties."""

    def test_default_limit(self) -> None:
        pool = LakeOffloadPool()
        assert pool.limit == DEFAULT_LAKE_OFFLOAD_LIMIT
        assert isinstance(pool.limiter, CapacityLimiter)
        assert pool.limiter.total_tokens == DEFAULT_LAKE_OFFLOAD_LIMIT

    def test_injected_limit(self) -> None:
        pool = LakeOffloadPool(limit=2)
        assert pool.limit == 2
        assert pool.limiter.total_tokens == 2

    def test_injected_limiter_wins_over_limit(self) -> None:
        limiter = CapacityLimiter(4)
        pool = LakeOffloadPool(limit=99, limiter=limiter)
        # The injected limiter is used verbatim; ``limit`` records the passed size.
        assert pool.limiter is limiter
        assert pool.limit == 99
        assert pool.limiter.total_tokens == 4


class TestLakeOffloadPoolRun:
    """``run`` hops to a worker thread under this pool's limiter and returns the result."""

    def test_runs_off_the_caller_thread_and_returns_result(self) -> None:
        pool = LakeOffloadPool(limit=2)

        async def scenario() -> tuple[tuple[int, int], float]:
            def work(a: int, *, b: int) -> tuple[int, int]:
                return (a, b)

            result = await pool.run(work, 7, b=11)
            return result, pool.limiter.total_tokens

        result, tokens = anyio.run(scenario)
        assert result == (7, 11)
        assert tokens == 2.0

    def test_bounds_concurrency_to_the_limiter(self) -> None:
        """A 1-token pool serialises two offloads: the second cannot enter until the first exits."""
        pool = LakeOffloadPool(limit=1)
        first_entered = threading.Event()
        release_first = threading.Event()
        order: list[str] = []

        def first() -> None:
            order.append("first-enter")
            first_entered.set()
            assert release_first.wait(timeout=5.0)
            order.append("first-exit")

        def second() -> None:
            order.append("second-enter")

        async def scenario() -> None:
            async with anyio.create_task_group() as tg:
                tg.start_soon(pool.run, first)
                # Wait until `first` holds the pool's only token, THEN launch
                # `second`; it must block on the limiter until `first` exits.
                await anyio.to_thread.run_sync(first_entered.wait, 5.0)
                tg.start_soon(pool.run, second)
                await anyio.sleep(0.05)
                assert "second-enter" not in order, "the 1-token pool did not bound concurrency"
                release_first.set()

        anyio.run(scenario)
        assert order == ["first-enter", "first-exit", "second-enter"]
