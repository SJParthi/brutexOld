"""Property pins for the stale-while-revalidate arm (DEC-DASHBOARD-PULL-CONTENTION-RESILIENCE-001).

Hypothesis drives random interleavings of reads / generation moves /
force_fresh reseeds / pull-state flips / clock advances / deferred-revalidator
completions over a FLAGGED :class:`MoversSnapshotCache` (deterministic
injectable clock + a capturing runner — no real threads, no sleeps) and
asserts the serve-order invariants:

* every served value is a PREVIOUSLY-COMPLETED real compute result — never a
  fabrication, never a torn/partial value;
* the cache never deadlocks or raises on any legal interleaving;
* with an entry present and the SWR arm engaged (pull active OR a revalidator
  already in flight), a read NEVER invokes the inline compute — asserted via a
  per-read compute-counter delta (the non-blocking law; inline computes happen
  only on a miss, idle-no-inflight staleness, or force_fresh — round-5 review:
  this bullet previously claimed the assertion without the teeth);
* at most ONE revalidator is ever in flight per key.
"""

# Whitebox pins read module-private state by design (DEC-P0-017).
# pyright: reportPrivateUsage=none
from __future__ import annotations

from typing import Any

from brutex.dashboard.movers_cache import MoversSnapshotCache
from hypothesis import given, settings
from hypothesis import strategies as st

_OPS = st.lists(
    st.one_of(
        st.tuples(st.just("read"), st.integers(min_value=0, max_value=3)),
        st.tuples(st.just("warm"), st.integers(min_value=0, max_value=3)),
        st.tuples(st.just("advance"), st.sampled_from([0.5, 1.0, 3.0, 130.0, 700.0])),
        st.tuples(st.just("pull"), st.booleans()),
        st.tuples(st.just("complete"), st.just(0)),
    ),
    min_size=1,
    max_size=40,
)


@settings(max_examples=120, deadline=None)
@given(ops=_OPS)
def test_swr_interleavings_serve_only_completed_computes(
    ops: list[tuple[str, Any]],
) -> None:
    """Random op sequences: served values ⊆ completed computes; ≤1 revalidator
    in flight per key; no deadlock; the SWR-armed read never computes inline."""
    clock = {"t": 0.0}
    pull = {"active": True}
    pending: list[Any] = []  # captured revalidator jobs (deferred completions)

    def _runner(job: Any) -> None:
        pending.append(job)

    cache = MoversSnapshotCache(
        pull_active_probe=lambda: pull["active"],
        adaptive_floor=True,
        stale_while_revalidate=True,
        swr_runner=_runner,
        clock=lambda: clock["t"],
    )
    completed: set[str] = set()
    counter = {"n": 0}

    def _make_compute() -> Any:
        def _compute() -> str:
            counter["n"] += 1
            value = f"v{counter['n']}"
            completed.add(value)
            return value

        return _compute

    for op, arg in ops:
        if op == "read":
            # The SWR arm is engaged for this read iff an entry exists AND
            # (a pull is active OR a revalidator is already in flight) — the
            # exact serve-stale precondition. Captured BEFORE the call.
            swr_engaged = "k" in cache._entries and (pull["active"] or "k" in cache._revalidating)
            computes_before = counter["n"]
            served = cache.get_or_compute("k", generation=int(arg), compute=_make_compute())
            # THE core invariant: only a completed real compute is ever served.
            assert served in completed
            if swr_engaged:
                # The non-blocking law, ASSERTED (round-5 review nit — the
                # docstring claimed it without teeth): an SWR-armed read never
                # invokes the inline compute synchronously — a spawned
                # revalidator's compute runs only on a later "complete" op,
                # so ANY counter bump inside this call was an inline compute.
                assert counter["n"] == computes_before
        elif op == "warm":
            served = cache.get_or_compute(
                "k", generation=int(arg), compute=_make_compute(), force_fresh=True
            )
            assert served in completed
        elif op == "advance":
            clock["t"] += float(arg)
        elif op == "pull":
            pull["active"] = bool(arg)
        elif op == "complete" and pending:
            pending.pop(0)()  # a deferred revalidator finishes now
        # ≤ 1 revalidator in flight for the single key, always.
        assert len(cache._revalidating) <= 1
    # Drain every deferred revalidator — none may raise or corrupt the cache.
    while pending:
        pending.pop(0)()
    final = cache.get_or_compute("k", generation=999, compute=_make_compute())
    assert final in completed
