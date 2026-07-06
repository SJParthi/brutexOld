"""Unit tests — per-partition write-health breaker (DEC-WRITE-HEALTH-BREAKER-001).

Drives :mod:`brutex.data.write_breaker` directly: streak/open/trip semantics, the
success reset, the open-stays-open law, constructor clamps, the env kill switch +
limit overrides, and thread safety. 100% line + branch on the module.
"""

from __future__ import annotations

import threading

import pytest
from brutex.data.errors import LakeWriteHalt
from brutex.data.write_breaker import (
    WRITE_BREAKER_CONSECUTIVE_LIMIT,
    WRITE_BREAKER_DISTINCT_OPEN_LIMIT,
    WriteHealthBreaker,
    partition_health_key,
    resolve_write_breaker,
)


def _key(n: int) -> str:
    return partition_health_key(
        exchange="NSE", segment="INDEX", symbol=f"SYM{n}", interval="1minute", month="2026-07"
    )


class TestPartitionHealthKey:
    def test_key_is_series_plus_month(self) -> None:
        key = partition_health_key(
            exchange="NSE", segment="INDEX", symbol="NIFTY", interval="1minute", month="2026-07"
        )
        assert key == "NSE/INDEX/NIFTY/1minute/2026-07"


class TestWriteHealthBreaker:
    def test_single_defer_does_not_open(self) -> None:
        b = WriteHealthBreaker()
        b.observe_defer(_key(1))
        assert b.open_partitions == frozenset()

    def test_two_consecutive_defers_open_the_partition(self) -> None:
        b = WriteHealthBreaker()
        b.observe_defer(_key(1))
        b.observe_defer(_key(1))
        assert b.open_partitions == {_key(1)}

    def test_success_resets_the_streak(self) -> None:
        b = WriteHealthBreaker()
        b.observe_defer(_key(1))
        b.observe_success(_key(1))
        b.observe_defer(_key(1))
        # The streak restarted at 1 after the durable write — never opened.
        assert b.open_partitions == frozenset()

    def test_success_on_unknown_key_is_noop(self) -> None:
        b = WriteHealthBreaker()
        b.observe_success(_key(9))
        assert b.open_partitions == frozenset()

    def test_open_partition_stays_open_after_success(self) -> None:
        """No half-open re-probe in this step (named follow-up) — open is terminal per pull."""
        b = WriteHealthBreaker()
        b.observe_defer(_key(1))
        b.observe_defer(_key(1))
        b.observe_success(_key(1))
        assert b.open_partitions == {_key(1)}

    def test_two_distinct_open_partitions_do_not_trip(self) -> None:
        b = WriteHealthBreaker()
        for n in (1, 2):
            b.observe_defer(_key(n))
            b.observe_defer(_key(n))
        assert b.open_partitions == {_key(1), _key(2)}

    def test_three_distinct_open_partitions_raise_volume_wedged(self) -> None:
        b = WriteHealthBreaker()
        for n in (1, 2):
            b.observe_defer(_key(n))
            b.observe_defer(_key(n))
        b.observe_defer(_key(3))
        with pytest.raises(LakeWriteHalt) as excinfo:
            b.observe_defer(_key(3))
        assert excinfo.value.reason == "volume_wedged"

    def test_reopening_same_partition_does_not_double_count(self) -> None:
        """Extra defers on an ALREADY-open key never add a second distinct open."""
        b = WriteHealthBreaker(distinct_open_limit=2)
        for _ in range(5):
            b.observe_defer(_key(1))
        assert b.open_partitions == {_key(1)}

    def test_constructor_clamps_limits_up_to_one(self) -> None:
        b = WriteHealthBreaker(consecutive_limit=0, distinct_open_limit=0)
        with pytest.raises(LakeWriteHalt):
            b.observe_defer(_key(1))  # 1 defer opens (clamped 1) and trips (clamped 1)

    def test_concurrent_defers_trip_without_corruption(self) -> None:
        """8 threads defer 4 distinct keys; the trip fires in at least one thread."""
        b = WriteHealthBreaker(consecutive_limit=1, distinct_open_limit=4)
        barrier = threading.Barrier(8)
        halts: list[LakeWriteHalt] = []
        lock = threading.Lock()

        def _work(n: int) -> None:
            barrier.wait()
            try:
                for _ in range(3):
                    b.observe_defer(_key(n % 4))
            except LakeWriteHalt as exc:
                with lock:
                    halts.append(exc)

        threads = [threading.Thread(target=_work, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)
        assert not any(t.is_alive() for t in threads)
        assert halts  # the volume-wedged trip fired (possibly in several threads)
        assert all(h.reason == "volume_wedged" for h in halts)
        assert b.open_partitions == {_key(0), _key(1), _key(2), _key(3)}


class TestResolveWriteBreaker:
    def test_default_is_armed_with_default_limits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BRUTEX_WRITE_BREAKER", raising=False)
        monkeypatch.delenv("BRUTEX_WRITE_BREAKER_CONSECUTIVE", raising=False)
        monkeypatch.delenv("BRUTEX_WRITE_BREAKER_DISTINCT", raising=False)
        b = resolve_write_breaker()
        assert isinstance(b, WriteHealthBreaker)
        # The defaults drive the documented 2-consecutive / 3-distinct semantics.
        assert WRITE_BREAKER_CONSECUTIVE_LIMIT == 2
        assert WRITE_BREAKER_DISTINCT_OPEN_LIMIT == 3
        b.observe_defer(_key(1))
        assert b.open_partitions == frozenset()
        b.observe_defer(_key(1))
        assert b.open_partitions == {_key(1)}

    @pytest.mark.parametrize("raw", ["off", " OFF ", "Off"])
    def test_kill_switch_off_returns_none(self, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
        monkeypatch.setenv("BRUTEX_WRITE_BREAKER", raw)
        assert resolve_write_breaker() is None

    def test_unknown_mode_stays_armed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRUTEX_WRITE_BREAKER", "banana")
        assert isinstance(resolve_write_breaker(), WriteHealthBreaker)

    def test_env_limits_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BRUTEX_WRITE_BREAKER", raising=False)
        monkeypatch.setenv("BRUTEX_WRITE_BREAKER_CONSECUTIVE", "1")
        monkeypatch.setenv("BRUTEX_WRITE_BREAKER_DISTINCT", "1")
        b = resolve_write_breaker()
        assert b is not None
        with pytest.raises(LakeWriteHalt):
            b.observe_defer(_key(1))

    def test_env_garbage_falls_back_to_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BRUTEX_WRITE_BREAKER", raising=False)
        monkeypatch.setenv("BRUTEX_WRITE_BREAKER_CONSECUTIVE", "not-an-int")
        b = resolve_write_breaker()
        assert b is not None
        b.observe_defer(_key(1))
        assert b.open_partitions == frozenset()  # default 2 still applies
        b.observe_defer(_key(1))
        assert b.open_partitions == {_key(1)}

    def test_env_nonpositive_clamps_up_to_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BRUTEX_WRITE_BREAKER", raising=False)
        monkeypatch.setenv("BRUTEX_WRITE_BREAKER_CONSECUTIVE", "-5")
        b = resolve_write_breaker()
        assert b is not None
        b.observe_defer(_key(1))  # clamp -5 -> 1: the first defer opens
        assert b.open_partitions == {_key(1)}
