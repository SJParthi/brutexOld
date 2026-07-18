"""Unit tests for :mod:`brutex.adapters.dhan.reauth` — 100% line+branch."""

from __future__ import annotations

import threading

import pytest
from brutex.adapters.dhan.errors import DhanTokenStillStaleError
from brutex.adapters.dhan.reauth import DhanTokenRefresher


def test_access_token_and_generation_initial() -> None:
    r = DhanTokenRefresher(initial_token="tok0", read_token=lambda: "tok0")
    assert r.access_token == "tok0"
    assert r.generation == 0


def test_reauth_adopts_fresh_token() -> None:
    r = DhanTokenRefresher(initial_token="tok0", read_token=lambda: "tok1")
    r.reauth()
    assert r.access_token == "tok1"
    assert r.generation == 1


def test_reauth_same_token_raises_stale() -> None:
    r = DhanTokenRefresher(initial_token="tok0", read_token=lambda: "tok0")
    with pytest.raises(DhanTokenStillStaleError):
        r.reauth()
    assert r.generation == 0


def test_reauth_blank_token_raises_stale() -> None:
    r = DhanTokenRefresher(initial_token="tok0", read_token=lambda: "  ")
    with pytest.raises(DhanTokenStillStaleError):
        r.reauth()


def test_reauth_none_token_raises_stale() -> None:
    r = DhanTokenRefresher(initial_token="tok0", read_token=lambda: None)
    with pytest.raises(DhanTokenStillStaleError):
        r.reauth()


def test_reauth_single_flight_adopts_concurrent_refresh() -> None:
    """The single-flight branch: another thread advanced the generation first."""
    read_calls: list[int] = []

    def read_token() -> str | None:
        read_calls.append(1)
        return "tok1"

    r = DhanTokenRefresher(initial_token="tok0", read_token=read_token)

    class _AdvancingLock:
        def __init__(self) -> None:
            self._real = threading.Lock()
            self._advanced = False

        def __enter__(self) -> object:
            self._real.acquire()
            if not self._advanced:
                self._advanced = True
                # simulate a concurrent thread having already adopted a fresh token
                r._generation += 1
            return self

        def __exit__(self, *_a: object) -> None:
            self._real.release()

    r._lock = _AdvancingLock()  # type: ignore[assignment]
    r.reauth()  # seen=0, but generation advanced to 1 under the lock → adopt, no re-read
    assert read_calls == []  # read_token NOT called on the single-flight branch
    assert r.generation == 1
