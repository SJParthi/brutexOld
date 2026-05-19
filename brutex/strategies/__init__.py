"""BruteX strategy engine — scaffolding for P3.

Implements the core protocol + runner pieces of ``FR-STR-001`` through
``FR-STR-003`` and ``FR-BF-003``:

* :class:`Strategy` — event-driven protocol every concrete strategy
  must satisfy. Stateless except for the engine-managed
  :class:`StrategyState`.
* :class:`StrategyMeta` — static declaration of a strategy's required
  ``symbols``, ``interval``, ``params``, and ``lookback_bars`` so the
  engine can pre-slice data and refuse to register a strategy whose
  declarations don't match its callable surface (``FR-STR-002``).
* :class:`StrategyRunner` — drives a
  :class:`~brutex.feed.ReplayFeed` through a :class:`Strategy`,
  collecting every emitted :class:`Signal` into a
  :class:`StrategyRunResult`. Enforces FR-INT-004 look-ahead via the
  feed's ``ensure_no_lookahead`` on every event.
* :func:`combo_hash` — deterministic 9-field BLAKE3 over
  ``(strategy_id, direction, instrument, tf, mode, canonical_params,
  data_snapshot_hash, indicator_version, code_commit_sha)`` per
  ``FR-BF-003`` + ``docs/TRACK1_BACKTEST_SPEC.md`` §"Identity"
  (DEC-P0-060 / PR-N+7 migration; was 4-field SHA256 pre-PR-N+7).
  Canonicalisation matches ``NFR-UNIQ-002`` (sorted keys, stable
  float repr, no NaN keys).

## Scope — P3 kickoff

Enough plumbing to register a strategy, drive it through a feed, and
compute the combo identity that every subsequent result will be keyed
by. The reference library of concrete strategies (MA crossover,
IronCondor, …) from ``FR-STR-004`` lands in follow-up commits — this
kickoff ships only the framework and one trivial *PassThrough*
strategy used by the test suite.

No math formulas live here. Track 1 strategies operate on bar-level
:class:`~brutex.data.models.CandleBar` values; any future Rust-accelerated
indicator pipeline will live under ``rust/fno-math/`` and be wired in
by Phase 2 (MG-043). Until then strategies that need indicators do the
math in pure Python on the bar feed.
"""

from __future__ import annotations

# Import the concrete strategy library so every @register_strategy
# decorator fires and the default_registry is populated. Users then
# discover strategies via ``default_registry.ids()`` or the CLI.
from brutex.strategies import (
    library,  # noqa: F401  (side-effect import)  # pyright: ignore[reportUnusedImport]
)
from brutex.strategies.combo import canonicalise_params, combo_hash
from brutex.strategies.errors import (
    StrategyContractError,
    StrategyError,
    StrategyRegistrationError,
)
from brutex.strategies.protocol import Strategy, StrategyMeta
from brutex.strategies.registry import (
    StrategyRegistry,
    default_registry,
    register_strategy,
)
from brutex.strategies.runner import StrategyRunner, StrategyRunResult
from brutex.strategies.sweep import (
    ComboCache,
    ComboResult,
    ComboSweep,
    FeedFactory,
    ParamGrid,
    StrategyFactory,
    SweepSummary,
)
from brutex.strategies.types import (
    Side,
    Signal,
    StrategyState,
)

__all__ = [
    "ComboCache",
    "ComboResult",
    "ComboSweep",
    "FeedFactory",
    "ParamGrid",
    "Side",
    "Signal",
    "Strategy",
    "StrategyContractError",
    "StrategyError",
    "StrategyFactory",
    "StrategyMeta",
    "StrategyRegistrationError",
    "StrategyRegistry",
    "StrategyRunResult",
    "StrategyRunner",
    "StrategyState",
    "SweepSummary",
    "canonicalise_params",
    "combo_hash",
    "default_registry",
    "register_strategy",
]
