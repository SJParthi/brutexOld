"""Tests for `brutex.adapters.dhan.token_holder` — the thread-safe token cell.

100% line+branch: the seed value, the atomic get, the atomic set propagation,
and concurrent reads/writes under the lock (DEC-DHAN-TOKEN-KEEPALIVE-001).
"""

from __future__ import annotations

import threading

from brutex.adapters.dhan.token_holder import TokenHolder


class TestTokenHolder:
    def test_get_returns_seed_token(self) -> None:
        holder = TokenHolder("seed-jwt")
        assert holder.get() == "seed-jwt"

    def test_set_replaces_token_and_get_sees_it(self) -> None:
        holder = TokenHolder("old-jwt")
        holder.set("new-jwt")
        assert holder.get() == "new-jwt"

    def test_repeated_set_keeps_latest(self) -> None:
        holder = TokenHolder("v0")
        holder.set("v1")
        holder.set("v2")
        assert holder.get() == "v2"

    def test_on_set_write_through_persists_each_set(self) -> None:
        # DEC-DHAN-TOKEN-SINGLE-SOURCE-001: with an on_set hook the holder is a
        # write-through cache — every set (renewal OR mid-session save) persists,
        # but construction does NOT (only an explicit set propagates).
        persisted: list[str] = []
        holder = TokenHolder("seed", on_set=persisted.append)
        assert persisted == []  # construction does not write through
        holder.set("renewed-1")
        holder.set("renewed-2")
        assert holder.get() == "renewed-2"
        assert persisted == ["renewed-1", "renewed-2"]  # both sets wrote through, in order

    def test_concurrent_get_set_never_tears(self) -> None:
        # The lock guarantees a reader always sees a whole token string. Hammer
        # get/set from many threads and assert every read is one of the written
        # whole values (never a torn intermediate) — the renew-propagation seam.
        valid = {f"jwt-{i}" for i in range(50)}
        holder = TokenHolder("jwt-0")
        seen: list[str] = []
        seen_lock = threading.Lock()

        def writer() -> None:
            for i in range(50):
                holder.set(f"jwt-{i}")

        def reader() -> None:
            for _ in range(200):
                value = holder.get()
                with seen_lock:
                    seen.append(value)

        threads = [threading.Thread(target=writer) for _ in range(3)]
        threads += [threading.Thread(target=reader) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert all(value in valid for value in seen)
        assert holder.get() == "jwt-49"
