"""brutex_cascade — BruteX cascade / results-depth compute laws (PyO3 bridge).

DEC-RUST-CASCADE-RESULTS-001 (the T14 lane of the pure-Rust O(1) conversion
program). The compiled cdylib ships as the private submodule
``brutex_cascade._brutex_cascade``; this ``__init__.py`` re-exports its
symbols (the fno-math DEC-P0-055 mixed layout) so callers write
``import brutex_cascade; brutex_cascade.build_cascade_join(...)``.

The consumers are the engine shims ``brutex/engine/{cascade_join,
minute_path, combo_aggregates}.py`` — resolved through the ONE facade
``brutex/engine/cascade_rust.py`` (``BRUTEX_CASCADE_RUST`` default ON; the
converted Python laws are DELETED, so an absent wheel is a loud typed
refusal naming the bootstrap remediation, never a silent degrade).
"""

from __future__ import annotations

from brutex_cascade._brutex_cascade import (
    EXIT_REASONS,
    MinuteIndexRs,
    aggregate_fold,
    build_cascade_join,
    float_stats,
    ist_session_dates,
    minute_path_max_drawdown,
    nearest_rank_index,
    session_equity_series,
    version,
)

__all__ = [
    "EXIT_REASONS",
    "MinuteIndexRs",
    "aggregate_fold",
    "build_cascade_join",
    "float_stats",
    "ist_session_dates",
    "minute_path_max_drawdown",
    "nearest_rank_index",
    "session_equity_series",
    "version",
]
