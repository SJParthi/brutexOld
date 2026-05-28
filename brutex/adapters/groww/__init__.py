"""Groww adapter — the Track 1 historical-candle data source.

**Status:** under construction (Phase 2). The live client (TOTP auth +
``get_historical_candles`` wrapper) lands gated behind the Phase 0b
empirical probe + credentials. The resilient ``run_fetch`` orchestration
(``brutex.adapters.groww.fetch_loop``) and the ``plan_chunks`` planner
(``brutex.adapters.groww.chunks``) are pure + fully tested today.

This package ``__init__`` intentionally does NOT eagerly import the
submodules: ``fetch_loop`` pulls in the orchestrator (and transitively
``fastbloom_rs``), whose PyO3 ABI3 single-init limitation would taint
standalone coverage of the pure submodules. Import the submodules
directly, e.g. ``from brutex.adapters.groww.chunks import plan_chunks``.

See ``docs/TRACK1_GROWW_ADAPTER.md`` for the full adapter spec.
"""

from __future__ import annotations

__all__: list[str] = []
