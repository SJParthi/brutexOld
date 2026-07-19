"""Brute-force sweep orchestrator — FR-BF-001 (grid) + FR-BF-004 (dedup).

Ties together the three primitives already in place:

1. :class:`~brutex.strategies.protocol.Strategy` — the unit of work.
2. :class:`~brutex.strategies.runner.StrategyRunner` — one replay per combo.
3. :func:`~brutex.strategies.combo.combo_hash` — the FR-BF-003 combo
   identity every result is keyed by.

and adds:

* :class:`ParamGrid` — enumerates a Cartesian grid from a dict of
  ``param_name -> iterable`` into the per-combo param dicts the
  runner expects.
* :class:`ComboCache` — LMDB-backed persistent dedup cache, keyed by
  the 32-byte raw sha256 of the combo identity. Two sweep runs over
  the same ``(strategy_id, params, data_snapshot_hash, commit_sha)``
  produce identical cache hits via one LMDB point-get — an O(log n)
  B+tree descent that is effectively flat for realistic n (high fanout
  + warm page cache), not a literal O(1). FR-BF-004 demands
  deterministic dedup, not constant-time lookup.
* :class:`ComboSweep` — the driver. Enumerates the grid, computes
  the :func:`combo_hash` for each combo, short-circuits on cache
  hits, otherwise constructs a fresh strategy + runner pair and
  persists the :class:`ComboResult`.

### Scope — P3 kickoff

This is a **grid sweep only** — FR-BF-001 (a). Bayesian / genetic
sweep variants land in follow-ups, but the orchestrator shape stays
the same: a grid is one concrete :class:`Iterable[Params]`, and any
future sampler just needs to produce a different iterable.

The "metric" recorded per combo is currently
:attr:`ComboResult.signal_count` — a toy placeholder that lets
reruns verify the dedup cache works end-to-end. Real P&L / Sharpe /
PBO metrics (FR-BF-006) land alongside the execution simulator and
do not affect the orchestration contract here.
"""

from __future__ import annotations

import itertools
import json
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Any, Self

from brutex.feed.replay import ReplayFeed
from brutex.strategies.combo import combo_hash
from brutex.strategies.errors import StrategyError
from brutex.strategies.runner import StrategyRunner

if TYPE_CHECKING:  # pragma: no cover
    from brutex.strategies.protocol import Strategy


#: Factory returning a freshly-constructed :class:`Strategy` for the
#: given params. Called once per combo by :class:`ComboSweep`, so
#: strategies can safely hold mutable scratch between ``on_event``
#: calls inside a single combo.
StrategyFactory = Callable[[Mapping[str, Any]], "Strategy"]

#: Length of a BLAKE3-256 hex digest in ASCII characters. Constant value
#: 64 was previously named ``SHA256_HEX_LEN`` (DEC-P0-060 / PR-N+7 renamed
#: alongside the sha256 → BLAKE3 migration in ``combo.py``; DEC-P0-061 /
#: PR-N+8 dropped the deprecated alias after confirming zero external
#: imports). BLAKE3's default 32-byte digest matches SHA256's wire format,
#: so existing LMDB keys remain 32-byte raw / 64-char hex unchanged.
COMBO_HASH_HEX_LEN: int = 64

#: Factory returning a freshly-prepared :class:`ReplayFeed` for the
#: given params. Receives the full combo params dict so strategies
#: whose symbol universe depends on a param can build the right
#: feed, but most callers will ignore the argument.
FeedFactory = Callable[[Mapping[str, Any]], ReplayFeed]


# ────────────────────────────────────────────────────────────────────────
# ParamGrid — Cartesian product over a dict of iterables
# ────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ParamGrid:
    """A Cartesian product over a dict of ``name -> iterable``.

    Example::

        grid = ParamGrid({"fast": [5, 10], "slow": [20, 50]})
        list(grid)
        # [
        #     {"fast": 5, "slow": 20},
        #     {"fast": 5, "slow": 50},
        #     {"fast": 10, "slow": 20},
        #     {"fast": 10, "slow": 50},
        # ]

    The iteration order is deterministic — keys sorted
    lexicographically, values taken in the user-supplied order. This
    matches the canonicalisation contract in
    :func:`~brutex.strategies.combo.canonicalise_params` so grid
    position ↔ combo identity stays stable across reruns.

    An empty grid (``ParamGrid({})``) yields exactly one empty dict
    — the "no parameters" single combo.

    Attributes
    ----------
    axes
        Mapping from param name to a sequence of candidate values.
        The sequence may be any iterable, but is materialised to a
        tuple on construction so re-iteration is safe.
    """

    axes: Mapping[str, tuple[Any, ...]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Reject any empty axis (the single source of truth for the guard).

        ``ParamGrid`` is a public, directly-constructible frozen dataclass, so
        the empty-axis guard must live here — not only in :meth:`from_dict`. A
        frozen ``__post_init__`` may validate (it reads, never writes, fields).
        An empty axis would otherwise yield a silent zero-combo sweep
        (``len == 0``, ``iter`` empty) with no error.
        """
        for name, values in self.axes.items():
            if not values:
                raise StrategyError(
                    f"ParamGrid axis {name!r} must have at least one candidate value"
                )

    @classmethod
    def from_dict(cls, axes: Mapping[str, Iterable[Any]]) -> Self:
        """Build a :class:`ParamGrid` from any iterable-valued dict."""
        materialised = {k: tuple(v) for k, v in axes.items()}
        return cls(axes=materialised)

    def __iter__(self) -> Iterator[dict[str, Any]]:
        """Yield one param dict per point in the Cartesian product."""
        if not self.axes:
            yield {}
            return
        sorted_keys = sorted(self.axes.keys())
        for combo_values in itertools.product(*(self.axes[k] for k in sorted_keys)):
            yield dict(zip(sorted_keys, combo_values, strict=True))

    def __len__(self) -> int:
        """Return the total number of combos (``∏ len(axis)``)."""
        if not self.axes:
            return 1
        total = 1
        for values in self.axes.values():
            total *= len(values)
        return total


# ────────────────────────────────────────────────────────────────────────
# ComboResult
# ────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ComboResult:
    """Outcome of running one combo through a :class:`StrategyRunner`.

    Attributes
    ----------
    combo_hash
        FR-BF-003 identity: ``sha256(strategy_id ‖ canonical_params
        ‖ data_snapshot_hash ‖ code_commit_sha)``.
    strategy_id
        ``strategy.META.strategy_id`` at run time.
    params
        The concrete parameter dict fed to the strategy factory.
    signal_count
        Number of signals the strategy emitted during the run.
    bars_observed
        In-scope bar count the runner reported.
    elapsed_seconds
        Wall-clock seconds the run took, monotonic-clock based.
    run_at
        Timezone-aware UTC timestamp of when the combo was persisted.
    cache_hit
        ``True`` if this result was loaded from the cache, ``False``
        if it came from a fresh run. Always ``False`` in the
        serialised-to-disk form — set on the way out when
        :meth:`ComboCache.get` succeeds.
    """

    combo_hash: str
    strategy_id: str
    params: Mapping[str, Any]
    signal_count: int
    bars_observed: int
    elapsed_seconds: float
    run_at: datetime
    cache_hit: bool = False

    def to_json(self) -> dict[str, Any]:
        """Return a JSON-serialisable projection of the result."""
        return {
            "combo_hash": self.combo_hash,
            "strategy_id": self.strategy_id,
            "params": dict(self.params),
            "signal_count": self.signal_count,
            "bars_observed": self.bars_observed,
            "elapsed_seconds": self.elapsed_seconds,
            "run_at": self.run_at.isoformat(),
        }

    @classmethod
    def from_json(cls, payload: Mapping[str, Any]) -> Self:
        """Inverse of :meth:`to_json`. Sets ``cache_hit=True``."""
        return cls(
            combo_hash=payload["combo_hash"],
            strategy_id=payload["strategy_id"],
            params=dict(payload["params"]),
            signal_count=payload["signal_count"],
            bars_observed=payload["bars_observed"],
            elapsed_seconds=payload["elapsed_seconds"],
            run_at=datetime.fromisoformat(payload["run_at"]),
            cache_hit=True,
        )


# ────────────────────────────────────────────────────────────────────────
# ComboCache — LMDB-backed persistent dedup
# ────────────────────────────────────────────────────────────────────────


class ComboCache:
    """Persistent ``combo_hash → ComboResult`` cache on LMDB.

    A persistent LMDB result cache. Keys are 32-byte raw sha256 digests so
    the B+tree fanout stays tight; values are JSON bytes.

    A single instance is a context manager — ``.close()`` must be
    called (explicitly or via ``with``) to release the LMDB
    environment cleanly. The cache is single-writer / multi-reader.

    Parameters
    ----------
    path
        Directory (LMDB uses a *directory*-shaped environment, not a
        single file). Created on first open.
    map_size_bytes
        Maximum on-disk size of the environment. Default 1 GiB which
        comfortably holds millions of combo results.
    """

    DB_NAME: bytes = b"combos"

    def __init__(self, *, env: object, path: Path) -> None:
        self._env = env
        self._path = path
        self._closed = False

    @classmethod
    def open(cls, path: Path, *, map_size_bytes: int = 1 << 30) -> Self:
        """Open or create the LMDB environment at *path*.

        Raises
        ------
        StrategyError
            If the ``lmdb`` dependency is not installed or the
            environment cannot be created.
        """
        try:
            import lmdb
        except ImportError as exc:
            raise StrategyError(
                "ComboCache requires 'lmdb' — install with "
                "'pip install -e .[data]' or the top-level 'lmdb' extra"
            ) from exc

        path.mkdir(parents=True, exist_ok=True)
        try:
            env = lmdb.open(
                str(path),
                map_size=map_size_bytes,
                subdir=True,
                max_dbs=4,
                readahead=False,
                sync=True,
                metasync=True,
            )
        except Exception as exc:  # pragma: no cover — defensive
            raise StrategyError(f"failed to open ComboCache at {path}: {exc}") from exc
        return cls(env=env, path=path)

    @property
    def path(self) -> Path:
        """Return the on-disk environment directory."""
        return self._path

    def get(self, combo_hash_hex: str) -> ComboResult | None:
        """Return the cached :class:`ComboResult` for *combo_hash_hex*, or None."""
        self._ensure_open()
        key = _hex_to_key(combo_hash_hex)
        with self._env.begin(db=self._db(), buffers=False) as txn:  # type: ignore[attr-defined]
            raw = txn.get(key)
        if raw is None:
            return None
        payload = json.loads(raw.decode("utf-8"))
        return ComboResult.from_json(payload)

    def put(self, result: ComboResult) -> None:
        """Insert or overwrite *result* by its :attr:`combo_hash`."""
        self._ensure_open()
        key = _hex_to_key(result.combo_hash)
        value = json.dumps(result.to_json(), sort_keys=True).encode("utf-8")
        with self._env.begin(db=self._db(), write=True) as txn:  # type: ignore[attr-defined]
            txn.put(key, value, overwrite=True)

    def exists(self, combo_hash_hex: str) -> bool:
        """Return ``True`` iff *combo_hash_hex* has a cached result.

        Faster than :meth:`get` because it does not deserialise the
        payload — one B+tree point-lookup with no payload
        deserialisation (an O(log n) descent, vs :meth:`get`'s
        O(log n) descent + O(value_size) JSON decode).
        """
        self._ensure_open()
        key = _hex_to_key(combo_hash_hex)
        with self._env.begin(db=self._db(), buffers=False) as txn:  # type: ignore[attr-defined]
            return txn.get(key) is not None

    def count(self) -> int:
        """Return the total number of cached combo results."""
        self._ensure_open()
        with self._env.begin(db=self._db()) as txn:  # type: ignore[attr-defined]
            stat = txn.stat()
        return int(stat["entries"])

    def close(self) -> None:
        """Close the underlying LMDB environment. Idempotent."""
        if self._closed:
            return
        try:
            self._env.close()  # type: ignore[attr-defined]
        finally:
            self._closed = True

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.close()

    # ── Private ────────────────────────────────────────────────────

    def _ensure_open(self) -> None:
        """Raise :class:`StrategyError` if the cache has been closed."""
        if self._closed:
            raise StrategyError("ComboCache is closed")

    def _db(self) -> object:
        """Return the single named sub-db handle, opened lazily."""
        return self._env.open_db(self.DB_NAME)  # type: ignore[attr-defined]


# ────────────────────────────────────────────────────────────────────────
# ComboSweep — the driver
# ────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SweepSummary:
    """High-level summary of one :meth:`ComboSweep.run` invocation.

    Attributes
    ----------
    total_combos
        Total number of combos enumerated from the :class:`ParamGrid`.
    cache_hits
        How many combos were short-circuited via the
        :class:`ComboCache`.
    fresh_runs
        How many combos were actually driven through the
        :class:`StrategyRunner`. ``cache_hits + fresh_runs ==
        total_combos``.
    elapsed_seconds
        Wall-clock time for the whole sweep.
    results
        One :class:`ComboResult` per combo, in grid-iteration order.
    """

    total_combos: int
    cache_hits: int
    fresh_runs: int
    elapsed_seconds: float
    results: tuple[ComboResult, ...]


class ComboSweep:
    """Drive a :class:`Strategy` over every point in a :class:`ParamGrid`.

    Parameters
    ----------
    strategy_factory
        Callable producing a fresh strategy instance for a given
        params dict. See :data:`StrategyFactory`.
    feed_factory
        Callable producing a fresh :class:`ReplayFeed` for a given
        params dict. See :data:`FeedFactory`.
    grid
        :class:`ParamGrid` to sweep over.
    data_snapshot_hash
        sha256 of the data slice the combos will run against.
        Typically produced by
        :meth:`~brutex.data.catalog.Catalog.data_snapshot_hash`.
    code_commit_sha
        Git SHA of the BruteX checkout. Recorded in every combo's
        identity so a bug-fix release invalidates stale cache
        entries automatically.
    cache
        Optional :class:`ComboCache`. When supplied, cache hits
        short-circuit the run; when ``None``, every combo runs
        fresh. Callers own the cache lifetime.
    """

    def __init__(
        self,
        *,
        strategy_factory: StrategyFactory,
        feed_factory: FeedFactory,
        grid: ParamGrid,
        data_snapshot_hash: str,
        code_commit_sha: str,
        cache: ComboCache | None = None,
    ) -> None:
        self._strategy_factory = strategy_factory
        self._feed_factory = feed_factory
        self._grid = grid
        self._data_snapshot_hash = data_snapshot_hash
        self._code_commit_sha = code_commit_sha
        self._cache = cache

    def run(self) -> SweepSummary:
        """Execute every combo in the grid and return a :class:`SweepSummary`."""
        start = time.monotonic()
        results: list[ComboResult] = []
        cache_hits = 0
        fresh_runs = 0

        for params in self._grid:
            probe = self._strategy_factory(params)
            strategy_id = probe.META.strategy_id
            ch = combo_hash(
                strategy_id=strategy_id,
                # TODO(engine PR): pass the real direction / instrument / tf /
                # mode / indicator_version from the Stage 1A/1B/2 funnel. Until
                # the engine modules exist (G-03 GAP), pre-engine sweep callers
                # supply explicit "N/A" placeholders so the 9-field BLAKE3
                # contract per DEC-P0-060 holds.
                direction="N/A",
                instrument="N/A",
                tf="N/A",
                mode="N/A",
                params=params,
                data_snapshot_hash=self._data_snapshot_hash,
                indicator_version="N/A",
                code_commit_sha=self._code_commit_sha,
            )

            if self._cache is not None:
                cached = self._cache.get(ch)
                if cached is not None:
                    results.append(cached)
                    cache_hits += 1
                    continue

            result = self._execute(
                strategy=probe,
                params=params,
                combo_hash_hex=ch,
            )
            if self._cache is not None:
                self._cache.put(result)
            results.append(result)
            fresh_runs += 1

        elapsed = time.monotonic() - start
        return SweepSummary(
            total_combos=len(self._grid),
            cache_hits=cache_hits,
            fresh_runs=fresh_runs,
            elapsed_seconds=elapsed,
            results=tuple(results),
        )

    # ── Private ────────────────────────────────────────────────────

    def _execute(
        self,
        *,
        strategy: Strategy,
        params: Mapping[str, Any],
        combo_hash_hex: str,
    ) -> ComboResult:
        """Run one combo through a freshly-built feed + runner pair."""
        feed = self._feed_factory(params)
        runner = StrategyRunner(strategy=strategy, feed=feed)
        run_start = time.monotonic()
        run_result = runner.run()
        elapsed = time.monotonic() - run_start

        return ComboResult(
            combo_hash=combo_hash_hex,
            strategy_id=strategy.META.strategy_id,
            params=dict(params),
            signal_count=len(run_result.signals),
            bars_observed=run_result.bars_observed,
            elapsed_seconds=elapsed,
            run_at=datetime.now(UTC),
            cache_hit=False,
        )


# ────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────


def _hex_to_key(combo_hash_hex: str) -> bytes:
    """Convert a 64-char BLAKE3 hex string to its 32-byte raw form.

    Raises :class:`StrategyError` on a wrong-length input. Centralised
    so :class:`ComboCache` always speaks the same key format and
    malformed inputs fail at the call site, not inside LMDB.
    """
    if len(combo_hash_hex) != COMBO_HASH_HEX_LEN:
        raise StrategyError(
            f"combo_hash must be {COMBO_HASH_HEX_LEN}-char BLAKE3 hex, "
            f"got {len(combo_hash_hex)} chars"
        )
    try:
        return bytes.fromhex(combo_hash_hex)
    except ValueError as exc:
        raise StrategyError(f"combo_hash {combo_hash_hex!r} is not valid hex") from exc


__all__ = [
    "ComboCache",
    "ComboResult",
    "ComboSweep",
    "FeedFactory",
    "ParamGrid",
    "StrategyFactory",
    "SweepSummary",
]
