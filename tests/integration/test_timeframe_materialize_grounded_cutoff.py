"""Data-grounded daily-cutoff integration oracle (DEC-DAILY-CUTOFF-GROUNDED-001).

End-to-end over a REAL tmp lake through the production
``materialize_lake_timeframes`` glue: the DEFER-only grounded arm — a
POST-CLOSE run finalizes the run day's own ``1day`` bar ONLY when the frame's
last bar proves the session complete (last bar 15:29 + 1 min == the calendar
close); a provably-INCOMPLETE run-day session (a pull died mid-day) is
deferred instead of finalized as a partial daily bar, then self-heals on the
next run once the data completes. Byte-identity pins: a complete dense
run-day session derives byte-identically to the pure clock verdict (grounded
== clock when the data agrees); the env kill switch, a non-dense symbol, and
an explicit ``daily_before`` all revert to / stay on the pre-grounding
behavior byte-for-byte.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("polars")
pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")

from brutex.calendars.nse import IST
from brutex.data.derive_stamp import derive_stamp_path, load_derive_stamp
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.sink import ParquetBarSink
from brutex.data.timeframe_materialize import (
    DAILY_TF_LABEL,
    MaterializeResult,
    PrebuildKind,
    materialize_lake_timeframes,
)
from structlog.testing import capture_logs

_EXCHANGE = "NSE"
_SEGMENT = "INDEX"
_SYMBOL = "NIFTY"

#: The run day — Mon 2021-01-04, a regular NSE session (09:15–15:30 IST).
_RUN_DAY = date(2021, 1, 4)
#: 09:15 IST on the run day == 03:45 UTC.
_SESSION_OPEN_UTC = datetime(2021, 1, 4, 3, 45, tzinfo=UTC)
#: A COMPLETE session holds 375 one-minute bars: 09:15 … 15:29 (the ingest
#: filter is half-open [open, close) — no 15:30 bar ever exists).
_FULL_SESSION_BARS = 375
#: The post-close run instant on the run day.
_POST_CLOSE = datetime(2021, 1, 4, 16, 0, tzinfo=IST)


def _bars(n: int, *, start: datetime = _SESSION_OPEN_UTC) -> list[CandleBar]:
    return [
        CandleBar(
            timestamp=start + timedelta(minutes=i),
            open=Decimal(str(100.0 + i)),
            high=Decimal(str(110.0 + i)),
            low=Decimal(str(90.0 + i)),
            close=Decimal(str(105.0 + i)),
            volume=10 + i,
            open_interest=0,
        )
        for i in range(n)
    ]


def _write_1m(lake_root: Path, bars: list[CandleBar]) -> None:
    sink = ParquetBarSink(writer=ParquetWriter(lake_root=lake_root))
    sink.append(exchange=_EXCHANGE, segment=_SEGMENT, symbol=_SYMBOL, interval="1minute", bars=bars)


def _derive(
    lake_root: Path,
    *,
    daily_before: date | None = None,
    daily_cutoff_now: datetime | None = _POST_CLOSE,
    dense_symbol: bool = True,
    incremental: bool | None = None,
) -> MaterializeResult:
    return materialize_lake_timeframes(
        lake_root=lake_root,
        exchange=_EXCHANGE,
        segment=_SEGMENT,
        symbol=_SYMBOL,
        kind=PrebuildKind.SPOT,
        tfs=("5m",),
        include_daily=True,
        daily_before=daily_before,
        daily_cutoff_now=daily_cutoff_now,
        dense_symbol=dense_symbol,
        incremental=incremental,
    )


def _tree_bytes(lake_root: Path) -> dict[str, bytes]:
    root = lake_root / "bars"
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*.parquet"))}


# ── the headline byte-identity pin: complete session ⇒ grounded == clock ────


def test_post_close_complete_dense_session_derives_same_run_byte_equal_to_clock(
    tmp_path: Path,
) -> None:
    """A COMPLETE run-day session (last bar 15:29) derives its 1day bar in the
    SAME post-close run, and the grounded lake is BYTE-IDENTICAL to an
    explicit clock-cutoff run — the debt item's core no-byte-change claim."""
    grounded_root = tmp_path / "grounded"
    clock_root = tmp_path / "clock"
    full = _bars(_FULL_SESSION_BARS)
    _write_1m(grounded_root, full)
    _write_1m(clock_root, full)

    grounded = _derive(grounded_root)  # default cutoff path + dense + post-close
    clock = _derive(clock_root, daily_before=_RUN_DAY + timedelta(days=1), daily_cutoff_now=None)

    assert grounded.written[DAILY_TF_LABEL] == 1  # the run day's own bar, same run
    assert clock.written[DAILY_TF_LABEL] == 1
    assert _tree_bytes(grounded_root) == _tree_bytes(clock_root)


def test_default_clock_seam_still_drives_grounding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With daily_cutoff_now=None the injectable ``_now_ist`` seam feeds the
    grounded arm (production fallback) — frozen post-close, a truncated dense
    frame defers exactly as with an explicit pinned instant."""
    from brutex.data import timeframe_materialize as tfm

    _write_1m(tmp_path, _bars(180))  # 09:15 … 12:14 — truncated
    monkeypatch.setattr(tfm, "_now_ist", lambda: _POST_CLOSE)
    result = _derive(tmp_path, daily_cutoff_now=None)
    assert result.written[DAILY_TF_LABEL] == 0


# ── the sanctioned defer + self-heal ─────────────────────────────────────────


def test_post_close_truncated_dense_session_defers_then_next_run_writes(tmp_path: Path) -> None:
    """A pull died mid-day: the post-close run DEFERS the partial run-day
    session (never a partial daily bar) and logs ONE grounded_defer; once the
    tail lands, the next run finalizes it (latest-wins self-heal)."""
    _write_1m(tmp_path, _bars(180))  # 09:15 … 12:14

    with capture_logs() as logs:
        first = _derive(tmp_path)
    assert first.written[DAILY_TF_LABEL] == 0  # deferred, not a partial bar
    defers = [e for e in logs if e["event"] == "prebuild.daily_cutoff.grounded_defer"]
    assert len(defers) == 1
    assert defers[0]["symbol"] == _SYMBOL
    assert defers[0]["clock_before"] == "2021-01-05"
    assert defers[0]["grounded_before"] == "2021-01-04"

    # The re-pull lands the rest of the session (the token moves) → finalized.
    _write_1m(tmp_path, _bars(_FULL_SESSION_BARS))  # keep-first: appends the tail
    second = _derive(tmp_path)
    assert second.written[DAILY_TF_LABEL] == 1


def test_grounded_deferral_stamps_effective_cutoff_and_never_fast_path_skips(
    tmp_path: Path,
) -> None:
    """The stamp records the EFFECTIVE (deferred) cutoff; an unchanged-source
    re-run therefore fails BOTH skip clauses and honestly re-derives (an
    idempotent rewrite), never a wrong skip that strands the run day's bar."""
    _write_1m(tmp_path, _bars(180))

    first = _derive(tmp_path, incremental=True)
    assert first.written[DAILY_TF_LABEL] == 0
    stamp = load_derive_stamp(derive_stamp_path(tmp_path, _EXCHANGE, _SEGMENT, _SYMBOL))
    assert stamp is not None
    assert stamp.daily_before == "2021-01-04"  # the DEFERRED value, not the clock's 01-05
    assert stamp.last_1m_ist_date == "2021-01-04"

    before = _tree_bytes(tmp_path)
    second = _derive(tmp_path, incremental=True)
    assert second.unchanged is False  # re-derives — the deferred day is never skip-stranded
    assert second.written[DAILY_TF_LABEL] == 0  # grounded defers again (data unchanged)
    assert _tree_bytes(tmp_path) == before  # idempotent rewrite (Golden Rule #7)


# ── the gates: env kill switch · dense · explicit daily_before ───────────────


def test_kill_switch_env_off_reverts_to_pure_clock_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BRUTEX_DAILY_CUTOFF_GROUNDED=0 ⇒ the pure clock arm of
    DEC-PREBUILD-SESSION-CLOSE-CUTOFF-001 — the truncated post-close day is
    finalized as a (partial) daily bar, byte-identical to pre-grounding."""
    monkeypatch.setenv("BRUTEX_DAILY_CUTOFF_GROUNDED", "0")
    _write_1m(tmp_path, _bars(180))
    result = _derive(tmp_path)
    assert result.written[DAILY_TF_LABEL] == 1


def test_non_dense_symbol_never_grounds(tmp_path: Path) -> None:
    """dense_symbol=False (every non-index spot + all F&O contracts): an
    illiquid name's genuine last trade legitimately precedes 15:29 — grounding
    would wrongly defer its market-complete sessions, so it never applies."""
    _write_1m(tmp_path, _bars(180))
    result = _derive(tmp_path, dense_symbol=False)
    assert result.written[DAILY_TF_LABEL] == 1


def test_explicit_daily_before_is_law_no_grounding(tmp_path: Path) -> None:
    """An explicit cutoff date (the lake-heal months rebuild / the doctor's
    probe_day+1 call) is absolute LAW — the truncated day derives under it."""
    _write_1m(tmp_path, _bars(180))
    result = _derive(
        tmp_path,
        daily_before=_RUN_DAY + timedelta(days=1),
        daily_cutoff_now=_POST_CLOSE,  # provided but MUST be ignored
    )
    assert result.written[DAILY_TF_LABEL] == 1


# ── the observe-only anomaly ─────────────────────────────────────────────────


def test_future_dated_last_bar_warns_and_abstains_to_clock(tmp_path: Path) -> None:
    """A FUTURE-dated bar (clock skew / corruption escaping drop_bars_after):
    ONE future_bar_anomaly WARN, the grounded arm abstains to the clock
    verdict, the prebuild never crashes on a diagnostic."""
    future_open = datetime(2021, 1, 5, 3, 45, tzinfo=UTC)  # the day AFTER the run day
    _write_1m(tmp_path, _bars(60, start=future_open))

    with capture_logs() as logs:
        result = _derive(tmp_path)  # run instant still post-close on 2021-01-04
    warns = [e for e in logs if e["event"] == "prebuild.daily_cutoff.future_bar_anomaly"]
    assert len(warns) == 1
    assert warns[0]["run_day"] == "2021-01-04"
    # Clock verdict stands: before = 2021-01-05, so the future day's session
    # (ist_date >= before) is excluded — nothing fabricated, nothing crashed.
    assert result.written[DAILY_TF_LABEL] == 0
