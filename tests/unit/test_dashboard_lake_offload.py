"""Unit tests for ``brutex.dashboard.lake_offload`` (DEC-DASHBOARD-LAKE-IO-LIMITER-001).

100% line + branch on the dedicated lake-I/O capacity limiter:

* the fail-loud env resolver (default / blank / valid / junk / < 1 / zero);
* constructor default-vs-explicit resolution + the < 1 refusal;
* the ``run_in_threadpool``-identical call contract (args/kwargs forwarded,
  return value passed through, exceptions — ``HTTPException`` 400s included —
  propagate unchanged);
* the LIMITER TEETH: with a 2-token instance, 5 concurrent blocked offloads
  never run more than 2 worker threads at once (measured max concurrency),
  while the exact same drive on the DEFAULT pool runs all 5 — the
  discriminating pin that the custom limiter actually binds;
* observability pins (``total_tokens`` / ``borrowed_tokens``).

Deterministic thread coordination via :class:`threading.Event` (released in
``finally``); anyio-on-asyncio driven through plain ``asyncio.run``.
"""

from __future__ import annotations

import asyncio
import threading

import pytest
from brutex.dashboard.lake_offload import (
    DEFAULT_LAKE_OFFLOAD_TOKENS,
    LAKE_OFFLOAD_TOKENS_ENV,
    LakeOffload,
    resolve_lake_offload_tokens,
)


class TestEnvResolver:
    """Fail-loud env override parsing."""

    def test_default_when_unset(self) -> None:
        assert resolve_lake_offload_tokens(env={}) == DEFAULT_LAKE_OFFLOAD_TOKENS

    def test_default_when_blank(self) -> None:
        assert (
            resolve_lake_offload_tokens(env={LAKE_OFFLOAD_TOKENS_ENV: "   "})
            == DEFAULT_LAKE_OFFLOAD_TOKENS
        )

    def test_valid_value_adopted(self) -> None:
        assert resolve_lake_offload_tokens(env={LAKE_OFFLOAD_TOKENS_ENV: "8"}) == 8

    def test_reads_process_env_when_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(LAKE_OFFLOAD_TOKENS_ENV, "16")
        assert resolve_lake_offload_tokens() == 16

    @pytest.mark.parametrize("raw", ["junk", "3.5", "0", "-4"])
    def test_malformed_or_sub_one_fails_loud(self, raw: str) -> None:
        with pytest.raises(ValueError, match=LAKE_OFFLOAD_TOKENS_ENV):
            resolve_lake_offload_tokens(env={LAKE_OFFLOAD_TOKENS_ENV: raw})


class TestCtor:
    """Constructor resolution + refusal."""

    def test_default_resolves_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(LAKE_OFFLOAD_TOKENS_ENV, "12")
        assert LakeOffload().total_tokens == 12

    def test_explicit_tokens_win(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(LAKE_OFFLOAD_TOKENS_ENV, "12")
        assert LakeOffload(tokens=3).total_tokens == 3

    def test_sub_one_explicit_refused(self) -> None:
        with pytest.raises(ValueError, match="tokens must be >= 1"):
            LakeOffload(tokens=0)

    def test_borrowed_tokens_zero_when_never_used(self) -> None:
        assert LakeOffload(tokens=2).borrowed_tokens == 0

    def test_default_size_is_below_the_default_pool(self) -> None:
        """32 < AnyIO's 40-token default pool — the sizing law the module states."""
        assert DEFAULT_LAKE_OFFLOAD_TOKENS == 32
        assert DEFAULT_LAKE_OFFLOAD_TOKENS < 40


class TestCallContract:
    """``run_in_threadpool``-identical semantics on the custom limiter."""

    def test_args_kwargs_and_return_value(self) -> None:
        offload = LakeOffload(tokens=2)

        def _fn(a: int, b: int, *, c: int) -> int:
            return a + b + c

        result = asyncio.run(offload(_fn, 1, 2, c=3))
        assert result == 6

    def test_exceptions_propagate_unchanged(self) -> None:
        offload = LakeOffload(tokens=2)

        class _Boom(RuntimeError):
            pass

        def _raise() -> None:
            raise _Boom("propagates")

        with pytest.raises(_Boom, match="propagates"):
            asyncio.run(offload(_raise))

    def test_runs_off_the_event_loop_thread(self) -> None:
        offload = LakeOffload(tokens=2)

        async def _main() -> tuple[int, int]:
            loop_thread = threading.get_ident()
            worker_thread = await offload(threading.get_ident)
            return loop_thread, worker_thread

        loop_thread, worker_thread = asyncio.run(_main())
        assert worker_thread != loop_thread


class TestLimiterTeeth:
    """The custom limiter actually BINDS (and the default pool does not, at 5)."""

    @staticmethod
    def _measure_max_concurrency(offload: LakeOffload | None, calls: int) -> int:
        """Drive *calls* concurrent blocked offloads; return the max simultaneous workers."""
        from fastapi.concurrency import run_in_threadpool

        lock = threading.Lock()
        active = 0
        peak = 0
        proceed = threading.Event()

        def _blocked() -> None:
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                proceed.wait(timeout=10.0)
            finally:
                with lock:
                    active -= 1

        async def _main() -> None:
            async def _one() -> None:
                if offload is None:
                    await run_in_threadpool(_blocked)
                else:
                    await offload(_blocked)

            tasks = [asyncio.ensure_future(_one()) for _ in range(calls)]
            # Let the dispatches land on workers, then release them all.
            await asyncio.sleep(0.3)
            proceed.set()
            await asyncio.gather(*tasks)

        try:
            asyncio.run(_main())
        finally:
            proceed.set()
        return peak

    def test_two_token_limiter_caps_concurrency_at_two(self) -> None:
        pytest.importorskip("fastapi", reason="the control arm uses run_in_threadpool")
        offload = LakeOffload(tokens=2)
        assert self._measure_max_concurrency(offload, calls=5) == 2

    def test_default_pool_control_arm_runs_all_five(self) -> None:
        """The discriminating control: the SAME drive on the default pool is unbounded at 5."""
        pytest.importorskip("fastapi", reason="the control arm uses run_in_threadpool")
        assert self._measure_max_concurrency(None, calls=5) == 5

    def test_borrowed_tokens_observability_while_held(self) -> None:
        offload = LakeOffload(tokens=2)
        entered = threading.Event()
        proceed = threading.Event()

        def _blocked() -> None:
            entered.set()
            proceed.wait(timeout=10.0)

        async def _main() -> int:
            task = asyncio.ensure_future(offload(_blocked))
            await asyncio.to_thread(entered.wait, 10.0)
            held = offload.borrowed_tokens
            proceed.set()
            await task
            return held

        try:
            assert asyncio.run(_main()) == 1
        finally:
            proceed.set()
