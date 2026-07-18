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
