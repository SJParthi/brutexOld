"""``FakeGrowwSDK`` — a synthetic, deterministic, network-free Groww SDK.

The at-scale pull harness (``tests/bench/pull_harness/``) + the slack/idle CI gate
(``tests/bench/test_pull_at_scale_gate.py``) need a Groww SDK that:

* satisfies the real :class:`~brutex.adapters.groww.sdk_protocol.GrowwSDK` Protocol so
  it flows through the REAL :class:`~brutex.adapters.groww.client.GrowwClient` /
  :class:`~brutex.adapters.groww.pool.ClientPool` / parser / assemble path — NOT a
  shortcut that bypasses the production code under test;
* returns synthetic **row-of-arrays** candle payloads
  (``{"candles": [[ts, o, h, l, c, v, oi], ...]}``) the real
  :func:`~brutex.adapters.groww.parser.parse_candle_rows` accepts byte-for-byte;
* sleeps a small, configurable, seeded-jitter REAL latency per call (5-20 ms by
  default) so the harness measures genuine in-flight concurrency + write/fetch
  overlap, exactly like a live socket round-trip (but ~100× faster + offline);
* can inject **429 / timeout bursts by call index** so the gate can prove the AIMD
  ladder + bounded retry behave under pressure;
* has a configurable ``candles_per_window`` + ``empty_rate`` so a deep multi-year
  partition produces a realistic bar volume + the occasional empty window.

It is thread-safe (the :class:`ClientPool` fans K worker threads across K leaves, all
sharing nothing but their own SDK instance — the fake's only shared state is an atomic
call counter guarded by a lock) and fully deterministic for a fixed ``seed`` +
single-threaded driver; under the real thread-pool the per-call RNG is seeded by the
monotonically-incrementing call index, so latency/empty/burst decisions are reproducible
regardless of which worker thread services a given call index.

This is a TEST DOUBLE — no ``growwapi`` install, no network, no real time beyond the tiny
injected latency. CLAUDE.md §11 COMMON: the harness builds exactly ONE of these per leaf
(via the real :func:`~brutex.adapters.groww.live.build_live_pool` ``sdk_factory`` seam),
never re-implementing the client/parser.
"""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta


def _timed_out() -> Exception:
    """Build a transient TIMEOUT the real ``from_groww_exception`` classifies retryable.

    The message carries a ``_TIMEOUT_HINTS`` phrase ("timed out") so the production
    error policy maps it to a retryable
    :class:`~brutex.adapters.groww.errors.GrowwTimeoutError` (drives the gentle timeout
    ladder), NOT a terminal quarantine.
    """
    return RuntimeError("Request timed out waiting for the vendor")


def _rate_limited() -> Exception:
    """Build a transient 429 the real ``from_groww_exception`` classifies a rate-limit.

    The message carries a ``_RATE_LIMIT_HINTS`` phrase ("Rate limit") so the production
    error policy maps it to a retryable
    :class:`~brutex.adapters.groww.errors.GrowwRateLimitError` — the AIMD ladder steps
    the shared bucket DOWN, exactly as a live 429 would.
    """
    return RuntimeError("Rate limit has breached for your request. Please retry.")


@dataclass(slots=True)
class FakeGrowwSDK:
    """A deterministic, offline Groww :class:`GrowwSDK` for the at-scale pull harness.

    Parameters
    ----------
    seed
        Base RNG seed. Each physical call seeds a fresh :class:`random.Random` from
        ``(seed, call_index)`` so latency/empty decisions are reproducible + independent
        of worker-thread scheduling (the call index is the only cross-thread state).
    min_latency_s, max_latency_s
        The per-call REAL ``time.sleep`` bounds (seconds). The call sleeps a seeded
        uniform draw in ``[min, max]`` so the harness measures genuine concurrency
        (in-flight > 1 across the pool) + fetch/write overlap. Defaults 5-20 ms.
    candles_per_window
        Synthetic 1-minute bars returned per non-empty ``get_historical_candles`` window
        (the rows the REAL parser turns into :class:`CandleBar`s + the lake writes).
    empty_rate
        Probability ``[0, 1]`` a given window returns an EMPTY ``{"candles": []}`` payload
        (a pre-listing / holiday / no-data window). ``0.0`` = every window has bars.
    rate_limit_call_indexes, timeout_call_indexes
        1-based physical-call indexes at which to inject a 429 / timeout burst. A call
        whose index is in the set raises the transient error INSTEAD of returning a
        payload (after the latency sleep), so the gate can drive the real retry ladder.
        A call index may appear in at most one set (429 wins if both, defensively).
    sleep
        Injected sleeper (default :func:`time.sleep`); a no-op in pure unit tests.
    """

    seed: int = 0
    min_latency_s: float = 0.005
    max_latency_s: float = 0.020
    candles_per_window: int = 120
    empty_rate: float = 0.0
    rate_limit_call_indexes: frozenset[int] = field(default_factory=frozenset)
    timeout_call_indexes: frozenset[int] = field(default_factory=frozenset)
    sleep: object = field(default=time.sleep)

    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _calls: int = field(default=0, init=False, repr=False)
    _empty_windows: int = field(default=0, init=False, repr=False)

    @property
    def calls(self) -> int:
        """Total physical ``get_historical_candles`` calls served (thread-safe read)."""
        with self._lock:
            return self._calls

    @property
    def empty_windows(self) -> int:
        """Count of windows that returned an EMPTY payload (thread-safe read)."""
        with self._lock:
            return self._empty_windows

    def _next_call_index(self) -> int:
        """Atomically claim + return this call's 1-based index (thread-safe)."""
        with self._lock:
            self._calls += 1
            return self._calls

    def _record_empty(self) -> None:
        with self._lock:
            self._empty_windows += 1

    def get_historical_candles(self, **kwargs: object) -> Mapping[str, object]:
        """Return one synthetic candle window (or inject a transient burst).

        The window's row count + empty-decision are seeded by ``(seed, call_index)`` so
        the result is reproducible. A real (short) ``sleep`` runs first so the harness
        sees genuine in-flight concurrency; a configured burst index then raises a
        retryable error, otherwise a row-of-arrays payload is built around the
        request's ``start_time``.
        """
        index = self._next_call_index()
        # Seed a fresh RNG from (seed, call_index) so latency/empty decisions are
        # reproducible + independent of which worker thread services this index.
        # ``random.Random`` accepts int/str/bytes (NOT a tuple in 3.12), so derive a
        # stable int from the pair.
        rng = random.Random(self.seed * 2_147_483_647 + index)
        latency = rng.uniform(self.min_latency_s, self.max_latency_s)
        if latency > 0.0:
            self.sleep(latency)  # type: ignore[operator]
        if index in self.rate_limit_call_indexes:
            raise _rate_limited()
        if index in self.timeout_call_indexes:
            raise _timed_out()
        if self.empty_rate > 0.0 and rng.random() < self.empty_rate:
            self._record_empty()
            return {"candles": []}
        start = _coerce_start(kwargs.get("start_time"))
        rows = _synthetic_rows(start, self.candles_per_window, rng)
        return {"candles": rows}

    def get_expiries(self, **_kwargs: object) -> Mapping[str, object]:
        """Return no FNO expiries — the harness drives SPOT only."""
        return {"expiries": []}

    def get_contracts(self, **_kwargs: object) -> Mapping[str, object]:
        """Return no FNO contracts — the harness drives SPOT only."""
        return {"contracts": []}


def _coerce_start(raw: object) -> datetime:
    """Parse the SDK ``start_time`` kwarg to a base IST datetime for the synthetic bars.

    The real client sends Groww's ``yyyy-MM-dd HH:mm:ss`` (space-separated) string. The
    bar timestamps are emitted as native-IST strings (the shape the real parser
    localizes), so the anchor is the window's DATE at **09:15 IST** — the post-auction
    session open (CLAUDE.md §7) — so the minute-stepped bars land INSIDE the 09:15-15:30
    trading session and survive the production session filter (a midnight anchor would
    put every bar pre-open and the filter would drop them all). A non-string /
    unparseable value falls back to a fixed 2020 trading-day anchor (it is a test double,
    not a validator).
    """
    if isinstance(raw, str):
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                parsed = datetime.strptime(raw.strip(), fmt)
            except ValueError:
                continue
            return parsed.replace(hour=9, minute=15, second=0, microsecond=0)
    return datetime(2020, 1, 1, 9, 15)


def _synthetic_rows(start: datetime, count: int, rng: random.Random) -> list[list[object]]:
    """Build *count* 7-element ``[ts, o, h, l, c, v, oi]`` rows from *start* (1-min step).

    Prices are a small deterministic random walk around 20000 (NIFTY-scale) so the
    rows assemble into valid :class:`CandleBar`s (open/high/low/close consistent —
    ``low <= open,close <= high``). Timestamps are native-IST strings (the shape the
    real parser localizes), one minute apart.
    """
    rows: list[list[object]] = []
    price = 20000.0 + rng.uniform(-50.0, 50.0)
    for i in range(count):
        ts = start + timedelta(minutes=i)
        delta = rng.uniform(-5.0, 5.0)
        open_ = round(price, 2)
        close = round(price + delta, 2)
        high = round(max(open_, close) + rng.uniform(0.0, 3.0), 2)
        low = round(min(open_, close) - rng.uniform(0.0, 3.0), 2)
        volume = rng.randint(100, 10_000)
        rows.append(
            [
                ts.strftime("%Y-%m-%dT%H:%M:%S"),
                open_,
                high,
                low,
                close,
                volume,
                0,  # open_interest — null-equivalent for spot
            ]
        )
        price = close
    return rows


def make_fake_sdk_factory(
    template: FakeGrowwSDK,
) -> object:
    """Return an ``sdk_factory(token) -> FakeGrowwSDK`` for :func:`build_live_pool`.

    Each leaf gets its OWN :class:`FakeGrowwSDK` (independent call counters + RNG seeds
    spread by leaf) so the K pool connections are genuinely independent — mirroring K
    real sockets — while the harness still controls latency/empty/burst via *template*.
    The factory ignores the (irrelevant, fake) access token.
    """
    leaf_counter = {"n": 0}
    lock = threading.Lock()

    def factory(_token: str) -> FakeGrowwSDK:
        with lock:
            leaf_index = leaf_counter["n"]
            leaf_counter["n"] += 1
        return FakeGrowwSDK(
            seed=template.seed + leaf_index * 1_000_003,
            min_latency_s=template.min_latency_s,
            max_latency_s=template.max_latency_s,
            candles_per_window=template.candles_per_window,
            empty_rate=template.empty_rate,
            rate_limit_call_indexes=template.rate_limit_call_indexes,
            timeout_call_indexes=template.timeout_call_indexes,
            sleep=template.sleep,
        )

    return factory


def shift_burst_indexes(indexes: Sequence[int]) -> frozenset[int]:
    """Normalize a sequence of 1-based burst call-indexes into a frozenset.

    A tiny convenience so a scenario can pass a list literal; deduped + frozen so the
    fake's membership test is O(1).
    """
    return frozenset(int(i) for i in indexes)


__all__ = [
    "FakeGrowwSDK",
    "make_fake_sdk_factory",
    "shift_burst_indexes",
]
