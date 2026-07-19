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
    bars_context_coerce,
    build_cascade_join,
    finite_or_zero,
    float_stats,
    ist_session_dates,
    lbt_coerce,
    lbt_to_dict,
    manifest_to_json,
    manifest_version_gate,
    max_drawdown_return_units,
    minute_path_max_drawdown,
    nearest_rank_index,
    per_bar_returns,
    roll_schedules_parse,
    serialize_wire_report,
    session_equity_series,
    spec_coerce,
    spec_fill_mode_gate,
    spec_to_dict,
    stage3_coerce,
    stage3_to_dict,
    version,
    wire_report_to_dict,
)

__all__ = [
    "EXIT_REASONS",
    "MinuteIndexRs",
    "aggregate_fold",
    "bars_context_coerce",
    "build_cascade_join",
    "finite_or_zero",
    "float_stats",
    "ist_session_dates",
    "lbt_coerce",
    "lbt_to_dict",
    "manifest_to_json",
    "manifest_version_gate",
    "max_drawdown_return_units",
    "minute_path_max_drawdown",
    "nearest_rank_index",
    "per_bar_returns",
    "roll_schedules_parse",
    "serialize_wire_report",
    "session_equity_series",
    "spec_coerce",
    "spec_fill_mode_gate",
    "spec_to_dict",
    "stage3_coerce",
    "stage3_to_dict",
    "version",
    "wire_report_to_dict",
]
