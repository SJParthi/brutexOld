"""Type stubs for the brutex_cascade PyO3 bridge (DEC-RUST-CASCADE-RESULTS-001)."""

from collections.abc import Mapping, Sequence
from typing import Any

class MinuteIndexRs:
    def __init__(
        self,
        ts_us: Sequence[int],
        opens: Sequence[str],
        highs: Sequence[str],
        lows: Sequence[str],
    ) -> None: ...
    def __len__(self) -> int: ...
    def span_slice(self, start_us: int, end_us: int) -> tuple[int, int]: ...
    def excursions(
        self,
        entry_us: int,
        exit_us: int,
        entry_fill_paisa: int,
        is_buy: bool,
        exit_bar_full_range: bool,
    ) -> tuple[int | None, int | None, str]: ...

EXIT_REASONS: list[str]

def version() -> str: ...
def session_equity_series(
    index: MinuteIndexRs | None,
    entry_us: Sequence[int],
    exit_us: Sequence[int],
    is_buy: Sequence[bool],
    pnl_paisa: Sequence[int],
    entry_px: Sequence[str],
    sessions: Sequence[int],
    start_equity_paisa: int,
) -> tuple[list[tuple[int, int | None, int]], str]: ...
def minute_path_max_drawdown(
    closes: Sequence[int],
    lows: Sequence[int | None],
    start_equity_paisa: int | None,
) -> tuple[int, float]: ...
def ist_session_dates(ts_us: Sequence[int]) -> list[int]: ...
def nearest_rank_index(n: int, pct: int) -> int: ...
def float_stats(values: Sequence[float]) -> tuple[float, float, float]: ...
def aggregate_fold(
    entry_us: Sequence[int],
    exit_us: Sequence[int],
    pnl_paisa: Sequence[int],
    mae_paisa: Sequence[int],
    mfe_paisa: Sequence[int],
    mae_1m_paisa: Sequence[int | None],
    mfe_1m_paisa: Sequence[int | None],
    path_1m_ok: Sequence[bool],
    exit_reasons: Sequence[str],
    vix_entry: Sequence[float | None],
    trend_days: Sequence[int],
    trend_labels: Sequence[str],
    event_days: Sequence[int],
    event_flags: Sequence[Sequence[str]],
    eq_closes: Sequence[int],
    eq_lows: Sequence[int | None],
    row_total_trades: int,
    row_net_pnl_paisa: int,
    row_max_drawdown_paisa: int,
    parity_ok: bool,
    start_equity_paisa: int,
) -> dict[str, Any]: ...
def build_cascade_join(
    payload: Mapping[str, Any] | object, report_version: int
) -> dict[str, Any]: ...

# ── cascade_wire (slice 2a) ──────────────────────────────────────────────

def wire_report_to_dict(report: Any, report_version: int) -> dict[str, Any]: ...
def serialize_wire_report(report: Any, report_version: int) -> bytes: ...
def spec_to_dict(
    spec: Any, fill_mode_token: str, fragment: Mapping[str, str]
) -> dict[str, Any]: ...
def stage3_to_dict(record: Any, cell_fragments: Sequence[Mapping[str, str]]) -> dict[str, Any]: ...
def lbt_to_dict(record: Any) -> dict[str, Any]: ...
def manifest_to_json(
    manifest: Any,
    config_dict: Mapping[str, Any],
    fill_mode_token: str,
    spec_fragments: Sequence[Mapping[str, str]],
    cell_fragments: Sequence[Mapping[str, str]] | None,
) -> bytes: ...
def spec_fill_mode_gate(data: Any, worst_case_token: str) -> None: ...
def spec_coerce(data: Any) -> dict[str, Any]: ...
def stage3_coerce(data: Any) -> dict[str, Any]: ...
def lbt_coerce(data: Any) -> dict[str, Any]: ...
def roll_schedules_parse(
    raw: Any, futures_alias: str
) -> tuple[tuple[str, tuple[int, ...]], ...] | None: ...
def manifest_version_gate(payload: Any, manifest_version: int) -> None: ...
def bars_context_coerce(payload: Any) -> dict[str, str]: ...
def finite_or_zero(value: float | None) -> float: ...
def per_bar_returns(equity_paisa: Sequence[int], start_equity_paisa: int) -> list[float]: ...
def max_drawdown_return_units(returns: Sequence[float]) -> float: ...
