"""Per-mask losing-streak resilience analytics — a sibling results-depth sidecar.

DEC-<KEBAB>-NNN. A psychological-endurance spec sheet per surviving strategy,
folded at run-END inside the fenced results-depth pass BESIDE
:func:`brutex.engine.combo_aggregates.aggregate_combo` — typical / severe / max
losing streak, worst consecutive-loss run in ₹ + %, longest time underwater in
TRADES and CALENDAR DAYS, loss clustering by month/week, and win-rate stability
across years.

ANNOTATION / DISPLAY ONLY. Never re-ranks / filters / rejects / mutates a
winner. ZERO run-identity impact — a NEW ``<stem>.resilience.parquet`` +
its OWN self-hash stamp under ``sweep_runs/<run_id>/results-depth/…``;
``ResultRow`` / ``combo_aggregates`` / ``AGG_COLUMNS`` /
``write_tuple_sidecars`` / the trio stamp are byte-untouched and
``TRADES_SCHEMA_VERSION`` is unbumped (this sidecar carries its own version —
the structurally collision-free dividend).

## Honesty envelope (Golden Rule #1 / #10)

* ``parity_ok=False`` OR zero trades ⇒ a FULL NULL record (every nullable
  ``None``, both jsons ``"{}"``, ``still_underwater_at_end is None``,
  ``stats_live=False``) — never a fabricated streak.
* A 0-loss LIVE mask reports ``None`` percentiles + a real ``0``
  ``losing_streak_count`` (the max lives on the agg's
  ``max_consecutive_losses`` — this module never persists a rival column).
* Every abstain is ``None``, never a fabricated ``0``.
* All ₹ are GROSS (costs ``None`` until P5, gap-register Ruling 18) and
  per-unit (no sizing, ``lots`` ``None`` until P5).

## Spec-locked honest limits (documented at each fold site below)

* ``longest_underwater_days`` is the peak → LAST-UNDERWATER-TRADE span, so when
  ``still_underwater_at_end`` is ``True`` it is a strict LOWER BOUND on the true
  peak → recovery duration (recovery, if it ever comes, lands one trade later —
  outside the backtest). Never read it as a promise of recovery.
* A scratch (``pnl == 0``) trade while below the peak INCREMENTS the underwater
  trade/day counters (it does not deepen or recover the curve): those counters
  measure how LONG you were underwater — a DURATION, not a count of losses.
* The win-rate ``total`` denominator counts EVERY trade that year (scratches
  included as non-wins), so ``wins / total`` is the literal win rate on a
  CONSISTENT denominator — a scratch is neither a win here nor a loss in the
  streak arm (they are treated distinctly, by design).
* ``losing_streak_count`` counts losing RUNS, never losing TRADES — the name is
  intentional; a display consumer must not read it as a trade count.

## O() envelope

O(trades) single pass at run-END over ≤ top-k finalists (≤ ~10³ trades each —
``ranking.MIN_TRADES=30`` floor, ``read_trade_page`` clamps 500); O(1) running
state + O(√n) streak histogram (a ``dict[int, int]`` of run lengths — positive
lengths summing to ≤ n ⇒ ≤ √(2n) distinct keys, EXACT, no cap) + calendar-
bounded month / week / year dicts — NOT literal O(1) memory (this MATCHES the
shipped ``mae_p95`` / ``mfe_p95`` envelope, which sorts an O(n) list; the dict
histogram is strictly better at O(√n)). ZERO diffs to the ~33B-trial hot path.
"""

from __future__ import annotations

import hashlib
import io
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final, Protocol, TypeGuard

import structlog

from brutex.data.atomic_io import atomic_write_simple
from brutex.engine.results_store import ROW_GROUP_SIZE, ZSTD_LEVEL
from brutex.engine.session_lens import ist_trade_date
from brutex.models import BruteXError

if TYPE_CHECKING:  # pragma: no cover - import only for static typing
    from collections.abc import Sequence
    from datetime import date
    from pathlib import Path

    from brutex.engine.trade_store import SidecarPaths

_LOG = structlog.get_logger("brutex.engine.resilience_streaks")

__all__ = [
    "RESILIENCE_ANNOTATION_KEY",
    "RESILIENCE_COLUMNS",
    "RESILIENCE_SCHEMA_VERSION",
    "YEAR_MIN_TRADES",
    "ResilienceError",
    "ResilienceStamp",
    "ResilienceStreaks",
    "fold_resilience",
    "load_resilience_stamp",
    "losing_run_length_hist",
    "project_resilience_annotation",
    "read_captured_trades",
    "read_resilience_record",
    "read_resilience_records",
    "render_resilience_line",
    "resilience_null",
    "resilience_payload",
    "write_resilience_sidecar",
]

#: The ONE top-level display key the resilience annotation lands under.
RESILIENCE_ANNOTATION_KEY: Final[str] = "resilience"

#: Own sidecar schema version — bump on ANY column/dtype/stamp-shape change (a
#: reader seeing an unknown version treats the sidecar as absent → rebuild).
RESILIENCE_SCHEMA_VERSION: Final[int] = 1

#: A year needs at least this many trades to enter the win-rate spread — fewer
#: is statistical noise, never a stability signal.
YEAR_MIN_TRADES: Final[int] = 20

#: Integer percent ranks for the streak-length percentiles.
_PCT_P50: Final[int] = 50
_PCT_P95: Final[int] = 95
_PCT_MAX: Final[int] = 100

#: The minimum qualifying years for a win-rate spread (stability needs ≥ 2).
_MIN_QUALIFYING_YEARS: Final[int] = 2

#: Percent scale for the worst-streak ₹ → % conversion.
_PCT_SCALE: Final[float] = 100.0

#: Dtype tags — resolved to polars dtypes lazily (polars never imports at
#: module import; the ``trade_store`` / ``results_store`` convention).
_UTF8: Final[str] = "utf8"
_INT64: Final[str] = "int64"
_FLOAT64: Final[str] = "float64"
_BOOL: Final[str] = "bool"

#: sha256 hex digest length.
_SHA256_HEX_LEN: Final[int] = 64

#: Unix epoch (UTC) — the exact-integer microsecond timestamp anchor.
_EPOCH_UTC: Final[datetime] = datetime(1970, 1, 1, tzinfo=UTC)

#: Reused install hint for the optional ``polars`` dependency.
_POLARS_HINT: Final[str] = (
    "resilience_streaks requires 'polars' — install with 'pip install -e .[data]' "
    "to enable the P2 data lake"
)

#: ``<stem>.resilience.parquet`` schema — pinned column order + dtypes
#: (byte-determinism law). Own schema authority; ``to_record()`` keys are
#: drift-pinned against these names by an in-module test.
RESILIENCE_COLUMNS: Final[tuple[tuple[str, str], ...]] = (
    ("run_id", _UTF8),
    ("stats_live", _BOOL),
    ("n_trades", _INT64),
    ("losing_streak_p50", _INT64),
    ("losing_streak_p95", _INT64),
    ("losing_streak_count", _INT64),
    ("worst_streak_pnl_paisa", _INT64),
    ("worst_streak_pnl_pct", _FLOAT64),
    ("worst_streak_len", _INT64),
    ("worst_streak_start_ts_us", _INT64),
    ("worst_streak_end_ts_us", _INT64),
    ("longest_underwater_trades", _INT64),
    ("longest_underwater_days", _INT64),
    ("still_underwater_at_end", _BOOL),
    ("losses_by_month_json", _UTF8),
    ("worst_loss_month", _UTF8),
    ("worst_loss_month_count", _INT64),
    ("worst_loss_week", _UTF8),
    ("worst_loss_week_count", _INT64),
    ("win_rate_by_year_json", _UTF8),
    ("win_rate_year_spread", _FLOAT64),
    ("fingerprint", _UTF8),
)

#: The column-name set (validation key for incoming records).
_COLUMN_NAMES: Final[frozenset[str]] = frozenset(name for name, _ in RESILIENCE_COLUMNS)


class ResilienceError(BruteXError):
    """Raised on a broken resilience sidecar invariant (paths, schema, damage)."""


def _dumps(payload: object) -> str:
    """The ONE deterministic JSON law (sorted keys, compact, ASCII, NaN refused)."""
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


# ── The record ────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ResilienceStreaks:
    """One finalist's losing-streak resilience record — one ``.resilience.parquet`` row.

    Field-for-field the :data:`RESILIENCE_COLUMNS` schema (test-pinned). Every
    abstain is ``None``; a ``not stats_live`` record is the FULL NULL record.
    """

    run_id: str
    stats_live: bool
    n_trades: int
    losing_streak_p50: int | None
    losing_streak_p95: int | None
    losing_streak_count: int | None
    worst_streak_pnl_paisa: int | None
    worst_streak_pnl_pct: float | None
    worst_streak_len: int | None
    worst_streak_start_ts_us: int | None
    worst_streak_end_ts_us: int | None
    longest_underwater_trades: int | None
    longest_underwater_days: int | None
    still_underwater_at_end: bool | None
    losses_by_month_json: str
    worst_loss_month: str | None
    worst_loss_month_count: int | None
    worst_loss_week: str | None
    worst_loss_week_count: int | None
    win_rate_by_year_json: str
    win_rate_year_spread: float | None
    fingerprint: str

    def __post_init__(self) -> None:
        if not self.run_id:
            raise ResilienceError("ResilienceStreaks.run_id must be non-empty")
        if self.n_trades < 0:
            raise ResilienceError(f"n_trades must be >= 0, got {self.n_trades}")
        if self.losing_streak_count is not None and self.losing_streak_count < 0:
            raise ResilienceError(
                f"losing_streak_count must be >= 0, got {self.losing_streak_count}"
            )
        if self.worst_streak_pnl_paisa is not None and self.worst_streak_pnl_paisa > 0:
            raise ResilienceError(
                f"worst_streak_pnl_paisa must be <= 0, got {self.worst_streak_pnl_paisa}"
            )
        if self.worst_streak_len is not None and self.worst_streak_len <= 0:
            raise ResilienceError(f"worst_streak_len must be > 0, got {self.worst_streak_len}")
        if self.longest_underwater_trades is not None and self.longest_underwater_trades < 0:
            raise ResilienceError(
                f"longest_underwater_trades must be >= 0, got {self.longest_underwater_trades}"
            )
        if self.longest_underwater_days is not None and self.longest_underwater_days < 0:
            raise ResilienceError(
                f"longest_underwater_days must be >= 0, got {self.longest_underwater_days}"
            )
        if self.worst_loss_month_count is not None and self.worst_loss_month_count <= 0:
            raise ResilienceError(
                f"worst_loss_month_count must be > 0, got {self.worst_loss_month_count}"
            )
        if self.worst_loss_week_count is not None and self.worst_loss_week_count <= 0:
            raise ResilienceError(
                f"worst_loss_week_count must be > 0, got {self.worst_loss_week_count}"
            )
        if not self.stats_live:
            self._assert_null_consistency()

    def _assert_null_consistency(self) -> None:
        """A ``not stats_live`` record must be the FULL NULL record (pinned)."""
        nullables = (
            self.losing_streak_p50,
            self.losing_streak_p95,
            self.losing_streak_count,
            self.worst_streak_pnl_paisa,
            self.worst_streak_pnl_pct,
            self.worst_streak_len,
            self.worst_streak_start_ts_us,
            self.worst_streak_end_ts_us,
            self.longest_underwater_trades,
            self.longest_underwater_days,
            self.still_underwater_at_end,
            self.worst_loss_month,
            self.worst_loss_month_count,
            self.worst_loss_week,
            self.worst_loss_week_count,
            self.win_rate_year_spread,
        )
        if any(value is not None for value in nullables):
            raise ResilienceError("a not-stats_live record must have every nullable field None")
        if self.losses_by_month_json != "{}" or self.win_rate_by_year_json != "{}":
            raise ResilienceError('a not-stats_live record must carry both jsons == "{}"')

    def to_record(self) -> dict[str, object]:
        """Render the record keyed EXACTLY by :data:`RESILIENCE_COLUMNS` names."""
        return {
            "run_id": self.run_id,
            "stats_live": self.stats_live,
            "n_trades": self.n_trades,
            "losing_streak_p50": self.losing_streak_p50,
            "losing_streak_p95": self.losing_streak_p95,
            "losing_streak_count": self.losing_streak_count,
            "worst_streak_pnl_paisa": self.worst_streak_pnl_paisa,
            "worst_streak_pnl_pct": self.worst_streak_pnl_pct,
            "worst_streak_len": self.worst_streak_len,
            "worst_streak_start_ts_us": self.worst_streak_start_ts_us,
            "worst_streak_end_ts_us": self.worst_streak_end_ts_us,
            "longest_underwater_trades": self.longest_underwater_trades,
            "longest_underwater_days": self.longest_underwater_days,
            "still_underwater_at_end": self.still_underwater_at_end,
            "losses_by_month_json": self.losses_by_month_json,
            "worst_loss_month": self.worst_loss_month,
            "worst_loss_month_count": self.worst_loss_month_count,
            "worst_loss_week": self.worst_loss_week,
            "worst_loss_week_count": self.worst_loss_week_count,
            "win_rate_by_year_json": self.win_rate_by_year_json,
            "win_rate_year_spread": self.win_rate_year_spread,
            "fingerprint": self.fingerprint,
        }


def resilience_null(run_id: str, n_trades: int, fingerprint: str) -> ResilienceStreaks:
    """The FULL NULL record (parity-failed / zero-trade / absent-data honesty)."""
    return ResilienceStreaks(
        run_id=run_id,
        stats_live=False,
        n_trades=n_trades,
        losing_streak_p50=None,
        losing_streak_p95=None,
        losing_streak_count=None,
        worst_streak_pnl_paisa=None,
        worst_streak_pnl_pct=None,
        worst_streak_len=None,
        worst_streak_start_ts_us=None,
        worst_streak_end_ts_us=None,
        longest_underwater_trades=None,
        longest_underwater_days=None,
        still_underwater_at_end=None,
        losses_by_month_json="{}",
        worst_loss_month=None,
        worst_loss_month_count=None,
        worst_loss_week=None,
        worst_loss_week_count=None,
        win_rate_by_year_json="{}",
        win_rate_year_spread=None,
        fingerprint=fingerprint,
    )


# ── Pure fold helpers ─────────────────────────────────────────────────────────


def losing_run_length_hist(pnls: Sequence[int]) -> dict[int, int]:
    """Return ``{run_length: frequency}`` of maximal consecutive losing runs.

    A loss is ``pnl < 0``; a win OR scratch (``pnl >= 0``) breaks a run (the
    ``combo_aggregates._consecutive_runs`` break law). The tooth reads
    ``max(losing_run_length_hist(pnls))`` — always equal to the longest losing
    streak (``_consecutive_runs(pnls)[0]``) when the histogram is non-empty.
    """
    hist: dict[int, int] = {}
    cur = 0
    for pnl in pnls:
        if pnl < 0:
            cur += 1
        elif cur > 0:
            hist[cur] = hist.get(cur, 0) + 1
            cur = 0
    if cur > 0:
        hist[cur] = hist.get(cur, 0) + 1
    return hist


def _nearest_rank_from_hist(hist: Mapping[int, int], pct: int) -> int:
    """The nearest-rank *pct*-th percentile over a run-length frequency histogram.

    The EXACT ``combo_aggregates.nearest_rank`` law (``rank = ceil(pct·n/100)``
    via ``-(-pct·n // 100)`` — no float) walked over the sorted distinct
    lengths, accumulating frequency until the rank is reached. Equal to
    ``nearest_rank(sorted(expanded), pct)`` (property-pinned).
    """
    count = sum(hist.values())
    if count == 0:
        raise ResilienceError("_nearest_rank_from_hist: histogram must be non-empty")
    if not 1 <= pct <= _PCT_MAX:
        raise ResilienceError(f"_nearest_rank_from_hist: pct must be in [1, 100], got {pct}")
    rank = -(-pct * count // _PCT_MAX)
    cumulative = 0
    for length in sorted(hist):
        cumulative += hist[length]
        if cumulative >= rank:
            return length
    raise ResilienceError(  # pragma: no cover - cumulative reaches count >= rank
        "_nearest_rank_from_hist: rank exceeded the cumulative frequency (unreachable)"
    )


class _TradeLike(Protocol):
    """The minimal per-trade view the fold reads (live capture OR read-back row).

    Read-only properties (not bare attributes) so a FROZEN ``CapturedTrade`` /
    ``_ReadTrade`` satisfies the structural protocol under ``mypy --strict``.
    """

    @property
    def pnl_paisa(self) -> int:
        raise NotImplementedError  # pragma: no cover - Protocol stub, never executed

    @property
    def entry_ts(self) -> datetime:  # tz-aware UTC
        raise NotImplementedError  # pragma: no cover - Protocol stub, never executed

    @property
    def exit_ts(self) -> datetime:
        raise NotImplementedError  # pragma: no cover - Protocol stub, never executed


@dataclass(slots=True)
class _RunState:
    """The active losing-run + worst-run accumulator (O(1) scalars)."""

    cur_len: int = 0
    cur_sum: int = 0
    cur_start_ts: datetime | None = None
    cur_end_ts: datetime | None = None
    worst_sum: int | None = None
    worst_len: int = 0
    worst_start_ts: datetime | None = None
    worst_end_ts: datetime | None = None


def _flush_run(state: _RunState, hist: dict[int, int]) -> None:
    """Close the active losing run into the histogram + worst-run trackers."""
    if state.cur_len == 0:
        return
    hist[state.cur_len] = hist.get(state.cur_len, 0) + 1
    # Strict ``<`` ⇒ a tie keeps the EARLIER run (the first-seen worst).
    if state.worst_sum is None or state.cur_sum < state.worst_sum:
        state.worst_sum = state.cur_sum
        state.worst_len = state.cur_len
        state.worst_start_ts = state.cur_start_ts
        state.worst_end_ts = state.cur_end_ts
    state.cur_len = 0
    state.cur_sum = 0
    state.cur_start_ts = None
    state.cur_end_ts = None


def _worst_bucket(counts: Mapping[str, int]) -> tuple[str, int]:
    """The bucket with the most losers, ties broken by EARLIEST key."""
    key, count = min(counts.items(), key=lambda item: (-item[1], item[0]))
    return key, count


def _ts_to_us(ts: datetime) -> int:
    """Convert a tz-aware datetime to exact UTC epoch microseconds (no floats)."""
    return (ts - _EPOCH_UTC) // timedelta(microseconds=1)


def fold_resilience(
    run_id: str,
    trades: Sequence[_TradeLike],
    *,
    parity_ok: bool,
    start_equity_paisa: int,
    fingerprint: str,
) -> ResilienceStreaks:
    """Fold one finalist's chronological trades into its resilience record.

    ONE pass over *trades* (``trade_seq`` order). ``parity_ok=False`` OR zero
    trades ⇒ the FULL NULL record. Percentiles come from the exact run-length
    histogram (``None`` when there are 0 losses); the worst-streak ``%`` uses
    *start_equity_paisa* as the denominator (guarded ``<= 0`` → ``None``); the
    underwater curve is the inline cumulative ``pnl_paisa`` anchored at
    *start_equity_paisa* (NOT ``capture.equity``), with trades AND calendar
    days as INDEPENDENT maxima.
    """
    n_trades = len(trades)
    if not (parity_ok and n_trades > 0):
        return resilience_null(run_id, n_trades=n_trades, fingerprint=fingerprint)

    run = _RunState()
    hist: dict[int, int] = {}
    month_losses: dict[str, int] = {}
    week_losses: dict[str, int] = {}
    year_wins: dict[str, int] = {}
    year_total: dict[str, int] = {}

    equity = start_equity_paisa
    peak = start_equity_paisa
    peak_exit_date: date = ist_trade_date(trades[0].exit_ts)
    cur_uw_trades = max_uw_trades = 0
    cur_uw_days = max_uw_days = 0

    for trade in trades:
        pnl = trade.pnl_paisa
        exit_date = ist_trade_date(trade.exit_ts)

        # 1.1 / 1.2 — streak length distribution + worst consecutive run.
        if pnl < 0:
            if run.cur_len == 0:
                run.cur_start_ts = trade.entry_ts
            run.cur_len += 1
            run.cur_sum += pnl
            run.cur_end_ts = trade.exit_ts
        else:  # win OR scratch (pnl >= 0) — the break law
            _flush_run(run, hist)

        # 1.4 — intermittent-loss clustering (by IST exit date).
        if pnl < 0:
            iso_year, iso_week, _ = exit_date.isocalendar()
            month_key = f"{exit_date.year:04d}-{exit_date.month:02d}"
            week_key = f"{iso_year:04d}-W{iso_week:02d}"
            month_losses[month_key] = month_losses.get(month_key, 0) + 1
            week_losses[week_key] = week_losses.get(week_key, 0) + 1

        # 1.5 — win-rate stability across years. HONEST LIMIT (iii): ``total``
        # counts EVERY trade this year — a scratch (``pnl == 0``) is a non-win
        # here (only ``pnl > 0`` wins), so ``wins / total`` is the literal win
        # rate on a CONSISTENT denominator (the streak arm treats a scratch
        # distinctly — neither win nor loss; that is deliberate, not a bug).
        year_key = f"{exit_date.year:04d}"
        year_total[year_key] = year_total.get(year_key, 0) + 1
        if pnl > 0:
            year_wins[year_key] = year_wins.get(year_key, 0) + 1

        # 1.3 — underwater (inline cumulative curve, independent maxima). The
        # trade-episode and day-episode worst runs are tracked INDEPENDENTLY (a
        # short-deep drawdown and a long-shallow one can be different episodes —
        # both are the honest "worst you endured").
        equity += pnl
        if equity >= peak:
            peak = equity
            peak_exit_date = exit_date
            cur_uw_trades = 0
            cur_uw_days = 0
        else:
            # HONEST LIMIT (ii): a scratch (``pnl == 0``) adds nothing to the
            # curve yet STILL increments these counters while below the peak —
            # they measure the DURATION you spent underwater, never a count of
            # losing trades. HONEST LIMIT (i): ``cur_uw_days`` is the span from
            # the peak's exit date to THIS trade's exit date, so if the curve
            # never recovers, ``longest_underwater_days`` (below) is a strict
            # LOWER BOUND on the true peak → recovery duration.
            cur_uw_trades += 1
            cur_uw_days = (exit_date - peak_exit_date).days
            max_uw_trades = max(max_uw_trades, cur_uw_trades)
            max_uw_days = max(max_uw_days, cur_uw_days)

    _flush_run(run, hist)

    # HONEST LIMIT (iv): the count of losing RUNS (histogram frequencies),
    # NEVER the count of losing trades — the field name is intentional.
    losing_streak_count = sum(hist.values())
    if losing_streak_count > 0:
        p50: int | None = _nearest_rank_from_hist(hist, _PCT_P50)
        p95: int | None = _nearest_rank_from_hist(hist, _PCT_P95)
    else:
        p50 = None
        p95 = None

    if run.worst_len > 0:
        assert run.worst_sum is not None  # noqa: S101 - worst_len>0 ⇒ a run was flushed
        assert run.worst_start_ts is not None  # noqa: S101
        assert run.worst_end_ts is not None  # noqa: S101
        worst_pnl: int | None = run.worst_sum
        worst_len: int | None = run.worst_len
        worst_start_us: int | None = _ts_to_us(run.worst_start_ts)
        worst_end_us: int | None = _ts_to_us(run.worst_end_ts)
        worst_pct: float | None = (
            run.worst_sum / start_equity_paisa * _PCT_SCALE if start_equity_paisa > 0 else None
        )
    else:
        worst_pnl = worst_len = worst_start_us = worst_end_us = None
        worst_pct = None

    worst_month: str | None
    worst_week: str | None
    worst_month_count: int | None
    worst_week_count: int | None
    if month_losses:
        losses_by_month_json = _dumps(month_losses)
        worst_month, worst_month_count = _worst_bucket(month_losses)
        worst_week, worst_week_count = _worst_bucket(week_losses)
    else:
        losses_by_month_json = "{}"
        worst_month = worst_week = None
        worst_month_count = worst_week_count = None

    by_year = {y: [year_wins.get(y, 0), year_total[y]] for y in year_total}
    qualifying = [y for y in year_total if year_total[y] >= YEAR_MIN_TRADES]
    if len(qualifying) >= _MIN_QUALIFYING_YEARS:
        rates = [year_wins.get(y, 0) / year_total[y] for y in qualifying]
        spread: float | None = max(rates) - min(rates)
    else:
        spread = None

    return ResilienceStreaks(
        run_id=run_id,
        stats_live=True,
        n_trades=n_trades,
        losing_streak_p50=p50,
        losing_streak_p95=p95,
        losing_streak_count=losing_streak_count,
        worst_streak_pnl_paisa=worst_pnl,
        worst_streak_pnl_pct=worst_pct,
        worst_streak_len=worst_len,
        worst_streak_start_ts_us=worst_start_us,
        worst_streak_end_ts_us=worst_end_us,
        longest_underwater_trades=max_uw_trades,
        longest_underwater_days=max_uw_days,
        still_underwater_at_end=equity < peak,
        losses_by_month_json=losses_by_month_json,
        worst_loss_month=worst_month,
        worst_loss_month_count=worst_month_count,
        worst_loss_week=worst_week,
        worst_loss_week_count=worst_week_count,
        win_rate_by_year_json=_dumps(by_year),
        win_rate_year_spread=spread,
        fingerprint=fingerprint,
    )


# ── Sidecar I/O — OWN parquet + OWN self-hash stamp ───────────────────────────


@dataclass(frozen=True, slots=True)
class ResilienceStamp:
    """The resilience sidecar's validation stamp (own version + self-hash)."""

    schema_version: int
    store_content_hash: str
    rows: int
    content_hash: str
    content_size: int
    produced_at_utc: str


def write_resilience_sidecar(
    paths: SidecarPaths,
    records: Sequence[ResilienceStreaks],
    *,
    store_content_hash: str,
) -> ResilienceStamp:
    """Write the deterministic ``<stem>.resilience.parquet`` + its stamp (R42).

    Records are sorted by ``run_id`` (input order never matters — Golden Rule
    #4); a duplicate ``run_id`` is rejected. The parquet is byte-deterministic
    (pinned dtypes, zstd, ``statistics=False``); the stamp is written LAST so a
    crash between the two leaves a stamp-less parquet every reader treats as
    absent → rebuild.

    Raises
    ------
    ResilienceError
        On a malformed *store_content_hash*, a duplicate ``run_id``, or a
        record that does not fit the pinned schema. Raw ``OSError`` from the
        atomic writes propagates unchanged (the engine sidecar contract).
    """
    if not _is_sha256(store_content_hash):
        raise ResilienceError(
            f"store_content_hash must be 64-char lowercase sha256 hex, got {store_content_hash!r}"
        )
    ordered = sorted(records, key=lambda record: record.run_id)
    seen: set[str] = set()
    for record in ordered:
        if record.run_id in seen:
            raise ResilienceError(f"duplicate resilience run_id {record.run_id!r}")
        seen.add(record.run_id)

    columns: dict[str, list[object]] = {name: [] for name, _ in RESILIENCE_COLUMNS}
    for record in ordered:
        wire = record.to_record()
        for name, column in columns.items():
            column.append(wire[name])

    parquet_bytes = _render_resilience_bytes(columns)
    paths.resilience.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_simple(paths.resilience, lambda tmp: tmp.write_bytes(parquet_bytes))

    stamp = ResilienceStamp(
        schema_version=RESILIENCE_SCHEMA_VERSION,
        store_content_hash=store_content_hash,
        rows=len(ordered),
        content_hash=hashlib.sha256(parquet_bytes).hexdigest(),
        content_size=len(parquet_bytes),
        produced_at_utc=datetime.now(tz=UTC).isoformat(),
    )
    payload = {
        "schema_version": stamp.schema_version,
        "store_content_hash": stamp.store_content_hash,
        "rows": stamp.rows,
        "content_hash": stamp.content_hash,
        "content_size": stamp.content_size,
        "produced_at_utc": stamp.produced_at_utc,
    }
    rendered = _dumps(payload) + "\n"
    atomic_write_simple(
        paths.resilience_stamp, lambda tmp: tmp.write_text(rendered, encoding="utf-8")
    )
    _LOG.info(
        "resilience.sidecar_written",
        stamp=str(paths.resilience_stamp),
        rows=stamp.rows,
        bytes=stamp.content_size,
    )
    return stamp


def load_resilience_stamp(
    paths: SidecarPaths, *, verify_hashes: bool = True
) -> ResilienceStamp | None:
    """Read + validate the resilience stamp; ANY damage returns ``None`` (rebuild).

    Validation (the ``trade_store.load_stamp`` law): the stamp reads + parses
    as a JSON object; ``schema_version`` matches; ``rows`` / ``content_size``
    are counts; ``store_content_hash`` / ``content_hash`` are sha256;
    ``produced_at_utc`` is a string; the parquet's CURRENT byte size matches;
    and — by default — its CURRENT sha256 equals the stamped one. Each
    rejection logs ONE ``resilience.stamp_invalid`` warning naming the reason.
    ``verify_hashes=False`` is the page-read tier (skips the O(file) re-hash).
    """

    def _invalid(reason: str) -> None:
        _LOG.warning("resilience.stamp_invalid", stamp=str(paths.resilience_stamp), reason=reason)

    try:
        raw = paths.resilience_stamp.read_bytes()
    except OSError:
        _invalid("stamp_unreadable")
        return None
    try:
        payload = json.loads(raw)
    except ValueError:
        _invalid("stamp_not_json")
        return None
    if not isinstance(payload, dict):
        _invalid("stamp_not_object")
        return None
    if payload.get("schema_version") != RESILIENCE_SCHEMA_VERSION:
        _invalid("schema_version_mismatch")
        return None
    rows = payload.get("rows")
    if not _is_count(rows):
        _invalid("bad_rows")
        return None
    size = payload.get("content_size")
    if not _is_count(size):
        _invalid("bad_content_size")
        return None
    store_hash = payload.get("store_content_hash")
    if not _is_sha256(store_hash):
        _invalid("bad_store_content_hash")
        return None
    content_hash = payload.get("content_hash")
    if not _is_sha256(content_hash):
        _invalid("bad_content_hash")
        return None
    produced = payload.get("produced_at_utc")
    if not isinstance(produced, str):
        _invalid("bad_produced_at_utc")
        return None
    try:
        actual_size = paths.resilience.stat().st_size
    except OSError:
        _invalid("parquet_unreadable")
        return None
    if actual_size != size:
        _invalid("parquet_size_mismatch")
        return None
    if verify_hashes:
        try:
            content = paths.resilience.read_bytes()
        except OSError:
            _invalid("parquet_unreadable")
            return None
        if hashlib.sha256(content).hexdigest() != content_hash:
            _invalid("parquet_hash_mismatch")
            return None
    return ResilienceStamp(
        schema_version=RESILIENCE_SCHEMA_VERSION,
        store_content_hash=store_hash,
        rows=rows,
        content_hash=content_hash,
        content_size=size,
        produced_at_utc=produced,
    )


def read_resilience_record(paths: SidecarPaths, *, run_id: str) -> ResilienceStreaks | None:
    """Return one finalist's resilience record, or ``None`` (unknown / absent).

    Page-read tier (``verify_hashes=False``): a missing / damaged stamp, or an
    unknown ``run_id``, returns ``None`` (the surface renders "—"). Reads the
    small parquet (≤ ~100 rows) and reconstructs the row as
    :class:`ResilienceStreaks`.
    """
    stamp = load_resilience_stamp(paths, verify_hashes=False)
    if stamp is None:
        return None
    for record in _read_records(paths.resilience):
        if record.get("run_id") == run_id:
            return _record_from_row(record)
    return None


def read_resilience_records(paths: SidecarPaths) -> dict[str, ResilienceStreaks]:
    """Return the tuple's resilience records keyed by ``run_id`` (batch reader).

    Page-read tier (``verify_hashes=False``): a missing / damaged stamp returns
    ``{}`` (every candidate then annotates an honest all-``None`` block). The
    pool-attach path in the run-end pass reads the small parquet ONCE per tuple
    (the ``_load_agg_records`` precedent) instead of once per candidate.
    """
    stamp = load_resilience_stamp(paths, verify_hashes=False)
    if stamp is None:
        return {}
    records: dict[str, ResilienceStreaks] = {}
    for row in _read_records(paths.resilience):
        record = _record_from_row(row)
        records[record.run_id] = record
    return records


def read_captured_trades(trades_path: Path) -> dict[str, list[_TradeLike]]:
    """Reconstruct per-``run_id`` chronological trades from the trades sidecar.

    Reads the CURRENT ``<stem>.trades.parquet`` (which the trio stamp vouched
    for) and rebuilds the minimal :class:`_TradeLike` view (pnl + tz-aware UTC
    entry/exit) per finalist, ordered by ``trade_seq`` — the adoption-backfill
    input (no re-simulation). A parity-failed / zero-trade finalist has 0 rows
    → absent from the mapping.
    """
    by_run: dict[str, list[tuple[int, _TradeLike]]] = {}
    for row in _read_records(trades_path):
        run_id = str(row["run_id"])
        trade: _TradeLike = _ReadTrade(
            pnl_paisa=_req_int(row["pnl_paisa"]),
            entry_ts=_EPOCH_UTC + timedelta(microseconds=_req_int(row["entry_ts_us"])),
            exit_ts=_EPOCH_UTC + timedelta(microseconds=_req_int(row["exit_ts_us"])),
        )
        by_run.setdefault(run_id, []).append((_req_int(row["trade_seq"]), trade))
    return {
        run_id: [trade for _seq, trade in sorted(rows, key=lambda item: item[0])]
        for run_id, rows in by_run.items()
    }


@dataclass(frozen=True, slots=True)
class _ReadTrade:
    """A read-back trade — the minimal :class:`_TradeLike` for the backfill fold."""

    pnl_paisa: int
    entry_ts: datetime
    exit_ts: datetime


def _record_from_row(row: Mapping[str, object]) -> ResilienceStreaks:
    """Reconstruct a :class:`ResilienceStreaks` from a stored parquet row."""
    if set(row.keys()) != _COLUMN_NAMES:
        raise ResilienceError(
            f"resilience row keys do not match RESILIENCE_COLUMNS: {sorted(row.keys())}"
        )
    return ResilienceStreaks(
        run_id=str(row["run_id"]),
        stats_live=bool(row["stats_live"]),
        n_trades=int(_req_int(row["n_trades"])),
        losing_streak_p50=_opt_int(row["losing_streak_p50"]),
        losing_streak_p95=_opt_int(row["losing_streak_p95"]),
        losing_streak_count=_opt_int(row["losing_streak_count"]),
        worst_streak_pnl_paisa=_opt_int(row["worst_streak_pnl_paisa"]),
        worst_streak_pnl_pct=_opt_float(row["worst_streak_pnl_pct"]),
        worst_streak_len=_opt_int(row["worst_streak_len"]),
        worst_streak_start_ts_us=_opt_int(row["worst_streak_start_ts_us"]),
        worst_streak_end_ts_us=_opt_int(row["worst_streak_end_ts_us"]),
        longest_underwater_trades=_opt_int(row["longest_underwater_trades"]),
        longest_underwater_days=_opt_int(row["longest_underwater_days"]),
        still_underwater_at_end=_opt_bool(row["still_underwater_at_end"]),
        losses_by_month_json=str(row["losses_by_month_json"]),
        worst_loss_month=_opt_str(row["worst_loss_month"]),
        worst_loss_month_count=_opt_int(row["worst_loss_month_count"]),
        worst_loss_week=_opt_str(row["worst_loss_week"]),
        worst_loss_week_count=_opt_int(row["worst_loss_week_count"]),
        win_rate_by_year_json=str(row["win_rate_by_year_json"]),
        win_rate_year_spread=_opt_float(row["win_rate_year_spread"]),
        fingerprint=str(row["fingerprint"]),
    )


def _read_records(parquet_path: Path) -> list[dict[str, object]]:
    """Read a small resilience/trades parquet as named records (lazy polars import)."""
    try:
        import polars as pl
    except ImportError as exc:
        raise ResilienceError(_POLARS_HINT) from exc
    try:
        df = pl.read_parquet(parquet_path)
    except (pl.exceptions.PolarsError, OSError) as exc:
        raise ResilienceError(f"unreadable parquet at {parquet_path}: {exc}") from exc
    return list(df.iter_rows(named=True))


def _render_resilience_bytes(columns: dict[str, list[object]]) -> bytes:
    """Serialise the pinned-schema columns to deterministic Parquet bytes.

    The exact ``trade_store._render_frame_bytes`` discipline: pinned dtypes,
    zstd :data:`~brutex.engine.results_store.ZSTD_LEVEL`, row groups of
    :data:`~brutex.engine.results_store.ROW_GROUP_SIZE`, ``statistics=False``
    (non-deterministic), ``use_pyarrow=False``.
    """
    try:
        import polars as pl
    except ImportError as exc:
        raise ResilienceError(_POLARS_HINT) from exc
    dtypes = {_UTF8: pl.Utf8, _INT64: pl.Int64, _FLOAT64: pl.Float64, _BOOL: pl.Boolean}
    schema = {name: dtypes[tag] for name, tag in RESILIENCE_COLUMNS}
    try:
        df = pl.DataFrame(columns, schema=schema)
    except (pl.exceptions.PolarsError, TypeError, OverflowError) as exc:
        raise ResilienceError(f"resilience rows do not fit the pinned schema: {exc}") from exc
    bio = io.BytesIO()
    df.write_parquet(
        bio,
        compression="zstd",
        compression_level=ZSTD_LEVEL,
        row_group_size=ROW_GROUP_SIZE,
        use_pyarrow=False,
        statistics=False,
    )
    return bio.getvalue()


# ── B-shape annotation + payload + CLI line ───────────────────────────────────


def project_resilience_annotation(
    entry: Mapping[str, object], record: ResilienceStreaks | None
) -> dict[str, object]:
    """Attach the resilience block to a COPY of *entry* under ONE top-level key.

    ADDITIVE-ONLY: every pre-existing key/value passes through byte-identical,
    the ranking fields are never rewritten, and re-annotation is refused
    (fail-loud) — the futures ``project_futures_annotation`` verbatim guard.

    Raises
    ------
    ValueError
        If *entry* already carries a ``"resilience"`` key.
    """
    if RESILIENCE_ANNOTATION_KEY in entry:
        msg = (
            f"entry already carries a {RESILIENCE_ANNOTATION_KEY!r} annotation — "
            "refusing to overwrite"
        )
        raise ValueError(msg)
    return {**entry, RESILIENCE_ANNOTATION_KEY: resilience_payload(record)}


def resilience_payload(record: ResilienceStreaks | None) -> dict[str, object]:
    """The full-precision display block (ints exact, pct/spread full float).

    A ``None`` (absent) record OR a NULL record yields a payload whose fields
    are all ``None`` / empty (NEVER omitted, so the panel always finds the
    keys) with ``stats_live: False`` — never a fabricated streak. ``worst_streak
    _start`` / ``worst_streak_end`` are ISO-8601 UTC (``None`` when no run);
    ``win_rate_by_year`` / ``losses_by_month`` are parsed from the stored JSON.
    """
    if record is None:
        return _absent_payload()
    return {
        "stats_live": record.stats_live,
        "n_trades": record.n_trades,
        "losing_streak_p50": record.losing_streak_p50,
        "losing_streak_p95": record.losing_streak_p95,
        "losing_streak_count": record.losing_streak_count,
        "worst_streak_pnl_paisa": record.worst_streak_pnl_paisa,
        "worst_streak_pnl_pct": record.worst_streak_pnl_pct,
        "worst_streak_len": record.worst_streak_len,
        "worst_streak_start": _us_to_iso_or_none(record.worst_streak_start_ts_us),
        "worst_streak_end": _us_to_iso_or_none(record.worst_streak_end_ts_us),
        "longest_underwater_trades": record.longest_underwater_trades,
        "longest_underwater_days": record.longest_underwater_days,
        "still_underwater_at_end": record.still_underwater_at_end,
        "worst_loss_month": record.worst_loss_month,
        "worst_loss_month_count": record.worst_loss_month_count,
        "worst_loss_week": record.worst_loss_week,
        "worst_loss_week_count": record.worst_loss_week_count,
        "win_rate_by_year": json.loads(record.win_rate_by_year_json),
        "win_rate_year_spread": record.win_rate_year_spread,
        "losses_by_month": json.loads(record.losses_by_month_json),
        "fingerprint": record.fingerprint,
    }


def _absent_payload() -> dict[str, object]:
    """The all-``None`` payload for an absent record (keys always present)."""
    return {
        "stats_live": False,
        "n_trades": None,
        "losing_streak_p50": None,
        "losing_streak_p95": None,
        "losing_streak_count": None,
        "worst_streak_pnl_paisa": None,
        "worst_streak_pnl_pct": None,
        "worst_streak_len": None,
        "worst_streak_start": None,
        "worst_streak_end": None,
        "longest_underwater_trades": None,
        "longest_underwater_days": None,
        "still_underwater_at_end": None,
        "worst_loss_month": None,
        "worst_loss_month_count": None,
        "worst_loss_week": None,
        "worst_loss_week_count": None,
        "win_rate_by_year": {},
        "win_rate_year_spread": None,
        "losses_by_month": {},
        "fingerprint": None,
    }


def render_resilience_line(entry: Mapping[str, object]) -> str:
    """Render one top100 entry's resilience block as a one-line CLI summary.

    DIRECT key access (fail-loud, no ``getattr``/``get`` default — a renamed
    key must fail here, never silently render nothing; the futures leaderboard
    precedent). A NULL / unavailable record renders the honest
    ``"resilience: — (parity-failed or no trades)"``.
    """
    from brutex.money import format_inr

    run_id = str(entry["run_id"])
    metrics = entry["metrics"]
    resilience = entry[RESILIENCE_ANNOTATION_KEY]
    if not isinstance(metrics, Mapping) or not isinstance(resilience, Mapping):
        raise ResilienceError(
            "render_resilience_line: entry 'metrics'/'resilience' must be objects"
        )
    if not resilience["stats_live"]:
        return f"  resilience {run_id[:12]}: — (parity-failed or no trades)"

    max_losses = metrics["max_consecutive_losses"]
    p50 = _dash(resilience["losing_streak_p50"])
    p95 = _dash(resilience["losing_streak_p95"])
    worst_pnl = resilience["worst_streak_pnl_paisa"]
    worst_str = "—" if worst_pnl is None else format_inr(int(worst_pnl))
    pct = resilience["worst_streak_pnl_pct"]
    pct_str = "—" if pct is None else f"{float(pct):.2f}%"
    uw_trades = _dash(resilience["longest_underwater_trades"])
    uw_days = _dash(resilience["longest_underwater_days"])
    underwater_badge = "STILL UNDERWATER" if resilience["still_underwater_at_end"] else "recovered"
    month = _dash(resilience["worst_loss_month"])
    return (
        f"  resilience {run_id[:12]}: worst-streak {max_losses} (typical {p50} · P95 {p95}) · "
        f"cost {worst_str} ({pct_str}) · underwater {uw_trades} trades / {uw_days} days "
        f"[{underwater_badge}] · worst month {month} · signal-only gross per-unit"
    )


def _dash(value: object) -> str:
    """Render a value or ``"—"`` when ``None`` (display honesty)."""
    return "—" if value is None else str(value)


def _us_to_iso_or_none(us: int | None) -> str | None:
    """ISO-8601 UTC string from epoch microseconds, or ``None``."""
    if us is None:
        return None
    return (_EPOCH_UTC + timedelta(microseconds=us)).isoformat()


# ── Small typed coercion + validation helpers ─────────────────────────────────


def _req_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ResilienceError(f"expected a non-null int, got {value!r}")
    return value


def _opt_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ResilienceError(f"expected an int or None, got {value!r}")
    return value


def _opt_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ResilienceError(f"expected a float or None, got {value!r}")
    return float(value)


def _opt_bool(value: object) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ResilienceError(f"expected a bool or None, got {value!r}")
    return value


def _opt_str(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ResilienceError(f"expected a str or None, got {value!r}")
    return value


def _is_count(value: object) -> TypeGuard[int]:
    """``True`` iff *value* is a genuine non-negative int (bools rejected)."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_sha256(value: object) -> TypeGuard[str]:
    """``True`` iff *value* is a 64-char lowercase sha256 hex string."""
    if not isinstance(value, str) or len(value) != _SHA256_HEX_LEN or value != value.lower():
        return False
    try:
        bytes.fromhex(value)
    except ValueError:
        return False
    return True
