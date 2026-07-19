"""Unit tests for the post-pull movers warm (DEC-PULL-MOVERS-WARM-001).

`latest_movers_date` over a real seeded Parquet+DuckDB lake (latest day / empty → None),
plus the pure `warm_movers_snapshots` best-effort job runner (counts non-empty, a 0-count
job is not counted, a raising job is logged + counted 0 — never re-raised).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")

from brutex.dashboard.discovery_warm import latest_movers_minute_key
from brutex.dashboard.movers_warm import (
    WARM_EXCHANGES,
    WARM_OPTIONS_TIERS,
    WARM_OPTIONS_TRACKS,
    MoversWarmResult,
    futures_warm_grid,
    latest_movers_date,
    latest_movers_minute,
    options_warm_grid,
    spot_warm_grid,
    warm_coordinate_label,
    warm_movers_snapshots,
)
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter


def _day_bar(day: date, close: str) -> CandleBar:
    c = Decimal(close)
    return CandleBar(
        timestamp=datetime(day.year, day.month, day.day, 3, 45, tzinfo=UTC),  # 09:15 IST
        open=c,
        high=c,
        low=c,
        close=c,
        volume=100,
        open_interest=None,
    )


def test_latest_movers_date_returns_newest_ist_date(tmp_path: Path) -> None:
    ParquetWriter(lake_root=tmp_path).write(
        exchange="NSE",
        segment="INDEX",
        symbol="NIFTY",
        interval="1day",
        bars=[_day_bar(date(2024, 6, 12), "20000"), _day_bar(date(2024, 6, 13), "20100")],
    )
    assert latest_movers_date(tmp_path) == "2024-06-13"


def test_latest_movers_date_spans_index_and_cash(tmp_path: Path) -> None:
    # The newest day may live in the CASH segment (a stock), not INDEX — both are scanned.
    ParquetWriter(lake_root=tmp_path).write(
        exchange="NSE",
        segment="INDEX",
        symbol="NIFTY",
        interval="1day",
        bars=[_day_bar(date(2024, 6, 10), "20000")],
    )
    ParquetWriter(lake_root=tmp_path).write(
        exchange="NSE",
        segment="CASH",
        symbol="RELIANCE",
        interval="1day",
        bars=[_day_bar(date(2024, 6, 14), "1300")],
    )
    assert latest_movers_date(tmp_path) == "2024-06-14"


def test_latest_movers_date_none_on_empty_lake(tmp_path: Path) -> None:
    assert latest_movers_date(tmp_path) is None


def test_latest_movers_date_none_when_only_non_spot_segments(tmp_path: Path) -> None:
    # An FNO-only lake (no INDEX/CASH 1day) → no spot movers date.
    ParquetWriter(lake_root=tmp_path).write(
        exchange="NSE",
        segment="FNO",
        symbol="NIFTY24JUN20000CE",
        interval="1day",
        bars=[_day_bar(date(2024, 6, 13), "100")],
    )
    assert latest_movers_date(tmp_path) is None


def _minute_bar(day: date, hh: int, mm: int, close: str) -> CandleBar:
    """A 1-minute bar stamped at *hh:mm* IST (stored UTC = IST − 5:30) on *day*."""
    ist_min = hh * 60 + mm
    utc_min = ist_min - (5 * 60 + 30)  # IST is a fixed +05:30, no DST.
    c = Decimal(close)
    return CandleBar(
        timestamp=datetime(day.year, day.month, day.day, utc_min // 60, utc_min % 60, tzinfo=UTC),
        open=c,
        high=c,
        low=c,
        close=c,
        volume=100,
        open_interest=None,
    )


def test_latest_movers_minute_finalized_day_returns_session_close(tmp_path: Path) -> None:
    # A finalized day whose last 1-minute bar opens at 15:29 IST → "15:29".
    ParquetWriter(lake_root=tmp_path).write(
        exchange="NSE",
        segment="INDEX",
        symbol="NIFTY",
        interval="1minute",
        bars=[
            _minute_bar(date(2024, 6, 13), 9, 15, "20000"),
            _minute_bar(date(2024, 6, 13), 15, 28, "20050"),
            _minute_bar(date(2024, 6, 13), 15, 29, "20100"),
        ],
    )
    assert latest_movers_minute(tmp_path, date(2024, 6, 13)) == "15:29"


def test_latest_movers_minute_truncated_day_returns_last_traded_minute(tmp_path: Path) -> None:
    # A live/truncated day (last bar 14:32 IST) → the true last minute, not 15:29.
    ParquetWriter(lake_root=tmp_path).write(
        exchange="NSE",
        segment="CASH",
        symbol="RELIANCE",
        interval="1minute",
        bars=[
            _minute_bar(date(2024, 6, 14), 9, 15, "1300"),
            _minute_bar(date(2024, 6, 14), 14, 32, "1305"),
        ],
    )
    assert latest_movers_minute(tmp_path, date(2024, 6, 14)) == "14:32"


def test_latest_movers_minute_scopes_to_the_requested_day(tmp_path: Path) -> None:
    # Bars on two days; asking for the earlier day returns THAT day's last minute.
    ParquetWriter(lake_root=tmp_path).write(
        exchange="NSE",
        segment="INDEX",
        symbol="NIFTY",
        interval="1minute",
        bars=[
            _minute_bar(date(2024, 6, 13), 13, 5, "20000"),
            _minute_bar(date(2024, 6, 14), 15, 29, "20100"),
        ],
    )
    assert latest_movers_minute(tmp_path, date(2024, 6, 13)) == "13:05"


def test_latest_movers_minute_none_on_empty_lake(tmp_path: Path) -> None:
    assert latest_movers_minute(tmp_path, date(2024, 6, 13)) is None


def test_latest_movers_minute_none_when_only_non_spot_segments(tmp_path: Path) -> None:
    # An FNO-only lake (no INDEX/CASH 1minute) → no spot movers minute.
    ParquetWriter(lake_root=tmp_path).write(
        exchange="NSE",
        segment="FNO",
        symbol="NIFTY24JUN20000CE",
        interval="1minute",
        bars=[_minute_bar(date(2024, 6, 13), 15, 29, "100")],
    )
    assert latest_movers_minute(tmp_path, date(2024, 6, 13)) is None


def test_latest_movers_minute_none_when_no_bar_on_the_requested_day(tmp_path: Path) -> None:
    # Spot 1-minute data exists, but not for the asked day → None (MAX over empty rows).
    ParquetWriter(lake_root=tmp_path).write(
        exchange="NSE",
        segment="INDEX",
        symbol="NIFTY",
        interval="1minute",
        bars=[_minute_bar(date(2024, 6, 14), 15, 29, "20100")],
    )
    assert latest_movers_minute(tmp_path, date(2024, 6, 13)) is None


def test_latest_movers_minute_none_on_corrupt_lake(tmp_path: Path) -> None:
    """DEC-MOVERS-ASOF-LATEST-DEFAULT-001: a corrupt spot 1minute Parquet → None, never a raise.

    Mirrors `latest_movers_date`'s guard — it backs the DEFAULT `/movers` poll, so an
    unguarded DuckDB error would 500 the whole page; degrade to None instead.
    """
    corrupt = tmp_path / "bars" / "NSE" / "INDEX" / "NIFTY" / "1minute" / "2024" / "06.parquet"
    corrupt.parent.mkdir(parents=True)
    corrupt.write_bytes(b"this is not a parquet file at all")
    assert latest_movers_minute(tmp_path, date(2024, 6, 13)) is None


def test_latest_movers_minute_reads_only_the_requested_days_month(tmp_path: Path) -> None:
    """Month-prune regression: a sibling month's file is NEVER read.

    The day's `<yyyy>/<mm>` partition fully determines where its bars live, so
    the resolver globs ONLY that month's spot 1minute files. Teeth: the OTHER
    month's file is corrupt — the pre-prune `1minute/**/*.parquet` glob would
    hand it to DuckDB and the corrupt-lake guard would degrade the whole resolve
    to None (proven by `test_latest_movers_minute_none_on_corrupt_lake`); the
    month-bounded read never touches it and still resolves the real minute.
    """
    ParquetWriter(lake_root=tmp_path).write(
        exchange="NSE",
        segment="INDEX",
        symbol="NIFTY",
        interval="1minute",
        bars=[
            _minute_bar(date(2024, 6, 13), 9, 15, "20000"),
            _minute_bar(date(2024, 6, 13), 15, 29, "20100"),
        ],
    )
    poisoned = tmp_path / "bars" / "NSE" / "INDEX" / "NIFTY" / "1minute" / "2024" / "05.parquet"
    poisoned.write_bytes(b"this is not a parquet file at all")
    assert latest_movers_minute(tmp_path, date(2024, 6, 13)) == "15:29"


def test_latest_movers_minute_multi_month_lake_matches_single_month_result(
    tmp_path: Path,
) -> None:
    # Byte-identical pin: pruning the glob to the day's month resolves the SAME
    # minute a multi-month lake holds for that day (the WHERE date filter always
    # selected it; the prune only removes I/O, never changes the answer).
    writer = ParquetWriter(lake_root=tmp_path)
    for bar in (
        _minute_bar(date(2024, 5, 20), 15, 29, "19900"),
        _minute_bar(date(2024, 6, 13), 13, 5, "20000"),
        _minute_bar(date(2024, 7, 1), 15, 29, "20200"),
    ):
        writer.write(
            exchange="NSE",
            segment="INDEX",
            symbol="NIFTY",
            interval="1minute",
            bars=[bar],
        )
    assert latest_movers_minute(tmp_path, date(2024, 6, 13)) == "13:05"
    assert latest_movers_minute(tmp_path, date(2024, 5, 20)) == "15:29"


def test_latest_movers_minute_key_is_date_scoped_and_distinct() -> None:
    # The memo key carries the ISO date (O(1) repeat poll per day) and never collides
    # with the sibling latest-DATE key.
    assert latest_movers_minute_key("2024-06-13") == "latest_movers_minute|2024-06-13"
    assert latest_movers_minute_key("2024-06-13") != latest_movers_minute_key("2024-06-14")


def test_latest_movers_date_none_on_corrupt_lake(tmp_path: Path) -> None:
    """GAP #4: a corrupt / unreadable spot 1day Parquet → None (degrade), never a raise.

    `latest_movers_date` backs the DEFAULT `/movers` poll, so an unguarded DuckDB error on
    a corrupt file would 500 the whole page. Treat an unreadable lake the same as an empty
    one — return None so the route degrades to an honest empty/today payload.
    """
    # Lay down a file at the spot 1day glob path that is GARBAGE bytes (not Parquet), so
    # DuckDB's read_parquet raises an InvalidInputException ("No magic bytes …").
    corrupt = tmp_path / "bars" / "NSE" / "INDEX" / "NIFTY" / "1day" / "2024" / "06.parquet"
    corrupt.parent.mkdir(parents=True)
    corrupt.write_bytes(b"this is not a parquet file at all")
    assert latest_movers_date(tmp_path) is None


# ── latest_movers_date: the 1-MINUTE source of truth (DEC-MOVERS-DAILY-FRESHNESS-001) ──


def test_latest_movers_date_moves_with_1m_append_without_reprebuild(tmp_path: Path) -> None:
    """FAILS-BEFORE: the pre-fix read scanned ONLY the derived ``1day``
    partitions, so a post-prebuild 1-minute append (a CLI pull, a crashed
    prebuild) froze the default movers date at the last prebuilt day. The date
    now resolves from the 1-minute base too — the newer 1-minute day WINS."""
    ParquetWriter(lake_root=tmp_path).write(
        exchange="NSE",
        segment="INDEX",
        symbol="NIFTY",
        interval="1day",
        bars=[_day_bar(date(2024, 6, 12), "20000")],
    )
    ParquetWriter(lake_root=tmp_path).write(
        exchange="NSE",
        segment="INDEX",
        symbol="NIFTY",
        interval="1minute",
        bars=[_minute_bar(date(2024, 6, 13), 9, 15, "20100")],
    )
    assert latest_movers_date(tmp_path) == "2024-06-13"


def test_latest_movers_date_1minute_only_lake_resolves(tmp_path: Path) -> None:
    # FAILS-BEFORE: a never-prebuilt (CLI-only) lake has NO 1day partitions —
    # the pre-fix read returned None and the default poll fell to an empty
    # "today"; the 1-minute source now answers.
    ParquetWriter(lake_root=tmp_path).write(
        exchange="NSE",
        segment="CASH",
        symbol="RELIANCE",
        interval="1minute",
        bars=[_minute_bar(date(2024, 6, 14), 9, 15, "1300")],
    )
    assert latest_movers_date(tmp_path) == "2024-06-14"


def test_latest_movers_date_is_max_of_both_sources(tmp_path: Path) -> None:
    # The "latest AVAILABLE day" honesty: an (out-of-band) 1day bar LATER than
    # the 1-minute max still wins — MAX over both sources, never a narrowing.
    ParquetWriter(lake_root=tmp_path).write(
        exchange="NSE",
        segment="INDEX",
        symbol="NIFTY",
        interval="1day",
        bars=[_day_bar(date(2024, 6, 17), "20200")],
    )
    ParquetWriter(lake_root=tmp_path).write(
        exchange="NSE",
        segment="INDEX",
        symbol="NIFTY",
        interval="1minute",
        bars=[_minute_bar(date(2024, 6, 13), 9, 15, "20100")],
    )
    assert latest_movers_date(tmp_path) == "2024-06-17"


def test_latest_movers_date_1m_reads_only_the_latest_month(tmp_path: Path) -> None:
    # Month-prune law: the max (year, month) partition holds the max instant,
    # so only ITS files are bound — an older month never changes the answer.
    writer = ParquetWriter(lake_root=tmp_path)
    writer.write(
        exchange="NSE",
        segment="INDEX",
        symbol="NIFTY",
        interval="1minute",
        bars=[_minute_bar(date(2024, 5, 20), 9, 15, "19900")],
    )
    writer.write(
        exchange="NSE",
        segment="INDEX",
        symbol="NIFTY",
        interval="1minute",
        bars=[_minute_bar(date(2024, 6, 13), 9, 15, "20100")],
    )
    # An OLDER month in the second-enumerated segment (CASH) — the max-month
    # fold must keep the INDEX June key (the not-newer enumeration arm).
    writer.write(
        exchange="NSE",
        segment="CASH",
        symbol="RELIANCE",
        interval="1minute",
        bars=[_minute_bar(date(2024, 5, 21), 9, 15, "1290")],
    )
    assert latest_movers_date(tmp_path) == "2024-06-13"


def test_latest_movers_date_ist_date_crossing_utc_month_edge(tmp_path: Path) -> None:
    # A bar late on the UTC month's last day belongs to the NEXT IST month's
    # 1st (IST = UTC+05:30) — the max-UTC-month read still answers the true
    # max IST date (IST date is monotone in the instant).
    ParquetWriter(lake_root=tmp_path).write(
        exchange="NSE",
        segment="INDEX",
        symbol="NIFTY",
        interval="1minute",
        # 2024-06-30 19:00 UTC = 2024-07-01 00:30 IST — filed under 2024/06.
        bars=[
            CandleBar(
                timestamp=datetime(2024, 6, 30, 19, 0, tzinfo=UTC),
                open=Decimal("20100"),
                high=Decimal("20100"),
                low=Decimal("20100"),
                close=Decimal("20100"),
                volume=100,
                open_interest=None,
            )
        ],
    )
    assert latest_movers_date(tmp_path) == "2024-07-01"


def test_latest_movers_date_corrupt_1minute_falls_back_to_1day(tmp_path: Path) -> None:
    # Each source is guarded independently: a corrupt 1-minute month file
    # degrades THAT source to None (one WARN) — the healthy 1day still answers.
    ParquetWriter(lake_root=tmp_path).write(
        exchange="NSE",
        segment="INDEX",
        symbol="NIFTY",
        interval="1day",
        bars=[_day_bar(date(2024, 6, 12), "20000")],
    )
    corrupt = tmp_path / "bars" / "NSE" / "INDEX" / "NIFTY" / "1minute" / "2024" / "06.parquet"
    corrupt.parent.mkdir(parents=True)
    corrupt.write_bytes(b"garbage, not parquet")
    assert latest_movers_date(tmp_path) == "2024-06-12"


def test_latest_movers_date_non_canonical_1m_month_name_skipped(tmp_path: Path) -> None:
    # Only the writer's canonical zero-padded `<yyyy>/<mm>.parquet` stem parses
    # (the #1558 round-5/8 law) — a junk `2024/6.parquet` / `٢٠٢٤` name is
    # skipped, never crashed on (and never binds a glob that misses itself).
    ParquetWriter(lake_root=tmp_path).write(
        exchange="NSE",
        segment="INDEX",
        symbol="NIFTY",
        interval="1minute",
        bars=[_minute_bar(date(2024, 6, 13), 9, 15, "20100")],
    )
    junk = tmp_path / "bars" / "NSE" / "INDEX" / "NIFTY" / "1minute" / "2025" / "6.parquet"
    junk.parent.mkdir(parents=True)
    junk.write_bytes(b"non-canonical stem, ignored by the month enumeration")
    assert latest_movers_date(tmp_path) == "2024-06-13"


def test_warm_counts_only_non_empty_jobs() -> None:
    # 3 jobs: spot warmed 5, futures warmed 0 (empty), options warmed 2 → 2 non-empty.
    result = warm_movers_snapshots([lambda: 5, lambda: 0, lambda: 2])
    assert result == MoversWarmResult(warmed=2, failed=0, total=3)


def test_warm_empty_job_list_is_zero() -> None:
    assert warm_movers_snapshots([]) == MoversWarmResult(warmed=0, failed=0, total=0)


def test_warm_swallows_a_raising_job_counts_it_failed() -> None:
    # A transient lake-read race in one warmer is logged + counted FAILED (never
    # re-raised — the already-successful pull is never failed by a warm hiccup;
    # the failed count reaches the completion note, DEC-MOVERS-WARM-PROGRESS-001).
    def _boom() -> int:
        raise RuntimeError("transient lake read race")

    result = warm_movers_snapshots([lambda: 3, _boom, lambda: 1])
    assert result == MoversWarmResult(warmed=2, failed=1, total=3)


# ── DEC-MOVERS-WARM-PROGRESS-001: per-coordinate progress + observer seam ──


def test_warm_emits_progress_log_per_coordinate() -> None:
    # One `movers.warm.progress` line per completed coordinate — key i/N +
    # segment + per-job/cumulative elapsed — a BOUNDED cadence (grid ≤ 39).
    from structlog.testing import capture_logs

    with capture_logs() as logs:
        warm_movers_snapshots([lambda: 2, lambda: 0], segment="spot", labels=["all", "NSE"])
    progress = [e for e in logs if e["event"] == "movers.warm.progress"]
    assert [(e["key"], e["index"], e["total"]) for e in progress] == [
        ("all", 1, 2),
        ("NSE", 2, 2),
    ]
    assert all(e["segment"] == "spot" for e in progress)
    assert all(e["job_elapsed_s"] >= 0 and e["elapsed_s"] >= 0 for e in progress)
    done = [e for e in logs if e["event"] == "movers.warm.segment_done"]
    assert len(done) == 1
    assert done[0]["warmed"] == 1 and done[0]["failed"] == 0 and done[0]["total"] == 2


def test_warm_failed_job_logs_key_and_counts() -> None:
    from structlog.testing import capture_logs

    def _boom() -> int:
        raise RuntimeError("boom")

    with capture_logs() as logs:
        result = warm_movers_snapshots([_boom], segment="options", labels=["BSE|far|weekly"])
    assert result == MoversWarmResult(warmed=0, failed=1, total=1)
    failed = [e for e in logs if e["event"] == "movers.warm.job_failed"]
    assert len(failed) == 1
    assert failed[0]["key"] == "BSE|far|weekly"
    assert failed[0]["segment"] == "options"
    assert failed[0]["index"] == 1 and failed[0]["total"] == 1


def test_warm_on_progress_observer_sees_i_of_n() -> None:
    # The gate seam (DEC-PREBUILD-GATE-HONEST-NOTE-001): (0,N) before the first
    # job, advancing per coordinate, (N,N) after the last.
    seen: list[tuple[int, int]] = []
    warm_movers_snapshots(
        [lambda: 1, lambda: 0, lambda: 2],
        on_progress=lambda done, total: seen.append((done, total)),
    )
    assert seen == [(0, 3), (1, 3), (2, 3), (3, 3)]


def test_warm_on_progress_observer_error_never_breaks_the_warm() -> None:
    # A raising observer is a caller bug worth a log line — never an abort.
    def _bad(done: int, total: int) -> None:
        raise RuntimeError("observer bug")

    result = warm_movers_snapshots([lambda: 4], on_progress=_bad)
    assert result == MoversWarmResult(warmed=1, failed=0, total=1)


def test_warm_short_labels_fall_back_to_index() -> None:
    # A labels list shorter than jobs never crashes — the tail falls back to
    # the 1-based index (defensive; the callers zip from the same grid).
    from structlog.testing import capture_logs

    with capture_logs() as logs:
        warm_movers_snapshots([lambda: 1, lambda: 1], segment="spot", labels=["only-one"])
    progress = [e for e in logs if e["event"] == "movers.warm.progress"]
    assert [e["key"] for e in progress] == ["only-one", "2"]


def test_warm_coordinate_label_renders_grid_dicts() -> None:
    assert warm_coordinate_label({"exchange": None}) == "all"
    assert warm_coordinate_label({"exchange": "NSE", "expiry_tier": "near"}) == "NSE|near"
    assert (
        warm_coordinate_label({"exchange": "BSE", "expiry_tier": "far", "expiry_track": "monthly"})
        == "BSE|far|monthly"
    )
    assert (
        warm_coordinate_label({"exchange": None, "expiry_tier": "next", "expiry_track": "all"})
        == "all|next|all"
    )


# ── DEC-MOVERS-WARM-GRID-001: the post-pull warm grids (pure, exhaustive) ──


def test_spot_warm_grid_one_coord_per_exchange() -> None:
    grid = spot_warm_grid()
    assert [c["exchange"] for c in grid] == list(WARM_EXCHANGES)
    assert len(grid) == 3  # None + NSE + BSE


def test_futures_warm_grid_is_exchange_times_tier() -> None:
    grid = futures_warm_grid()
    assert len(grid) == len(WARM_EXCHANGES) * len(WARM_OPTIONS_TIERS)  # 3 × 3 = 9
    # every (exchange, tier) pair appears exactly once
    pairs = {(c["exchange"], c["expiry_tier"]) for c in grid}
    assert pairs == {(e, t) for e in WARM_EXCHANGES for t in WARM_OPTIONS_TIERS}


def test_options_warm_grid_is_exchange_times_tier_times_track() -> None:
    grid = options_warm_grid()
    assert len(grid) == len(WARM_EXCHANGES) * len(WARM_OPTIONS_TIERS) * len(WARM_OPTIONS_TRACKS)
    assert len(grid) == 27
    triples = {(c["exchange"], c["expiry_tier"], c["expiry_track"]) for c in grid}
    assert triples == {
        (e, t, k) for e in WARM_EXCHANGES for t in WARM_OPTIONS_TIERS for k in WARM_OPTIONS_TRACKS
    }


def test_grid_tokens_are_lowercase_canonical() -> None:
    # The cache key uses lower-cased tier/track tokens; the grid must match.
    assert WARM_OPTIONS_TIERS == ("near", "next", "far")
    assert WARM_OPTIONS_TRACKS == ("all", "weekly", "monthly")
    assert WARM_EXCHANGES == (None, "NSE", "BSE")


# ── DEC-MOVERS-WARM-UNBLOCK-001: per-job deadline + job-start observability ──


def test_resolve_warm_job_deadline_default_and_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from brutex.dashboard.movers_warm import (
        DEFAULT_WARM_JOB_DEADLINE_S,
        WARM_JOB_DEADLINE_ENV,
        resolve_warm_job_deadline_s,
    )

    # Unset env → the audited 300 s default.
    monkeypatch.delenv(WARM_JOB_DEADLINE_ENV, raising=False)
    assert resolve_warm_job_deadline_s() == DEFAULT_WARM_JOB_DEADLINE_S
    # Env override wins (read when raw is None).
    monkeypatch.setenv(WARM_JOB_DEADLINE_ENV, "120")
    assert resolve_warm_job_deadline_s() == 120.0
    # Explicit raw wins over env.
    assert resolve_warm_job_deadline_s("45.5") == 45.5
    # Fail-safe to the default (Golden Rule #1 — a bad value can NEVER disable
    # the bound): blank / non-numeric / non-positive / non-finite.
    assert resolve_warm_job_deadline_s("   ") == DEFAULT_WARM_JOB_DEADLINE_S
    assert resolve_warm_job_deadline_s("not-a-number") == DEFAULT_WARM_JOB_DEADLINE_S
    assert resolve_warm_job_deadline_s("0") == DEFAULT_WARM_JOB_DEADLINE_S
    assert resolve_warm_job_deadline_s("-5") == DEFAULT_WARM_JOB_DEADLINE_S
    assert resolve_warm_job_deadline_s("inf") == DEFAULT_WARM_JOB_DEADLINE_S
    assert resolve_warm_job_deadline_s("nan") == DEFAULT_WARM_JOB_DEADLINE_S


def test_warm_job_timeout_counts_failed_warns_and_continues() -> None:
    """A job that is merely SLOW (never raises) is bounded: counted failed +
    one `movers.warm.job_timeout` WARN, and the warm CONTINUES with the next
    coordinate — the 2026-07-04/05 unbounded-walk incident class (170-256 s
    per key over 39 keys) can never stall the warm without a trace."""
    import threading

    from structlog.testing import capture_logs

    release = threading.Event()

    def _wedged() -> int:
        release.wait(30.0)  # never set before the deadline fires
        return 0

    try:
        with capture_logs() as logs:
            result = warm_movers_snapshots(
                [_wedged, lambda: 3],
                segment="options",
                labels=["BSE|far|weekly", "NSE|near|all"],
                job_deadline_s=0.05,
            )
    finally:
        release.set()  # let the abandoned daemon worker exit promptly
    assert result == MoversWarmResult(warmed=1, failed=1, total=2)
    timeouts = [e for e in logs if e["event"] == "movers.warm.job_timeout"]
    assert len(timeouts) == 1
    assert timeouts[0]["key"] == "BSE|far|weekly"
    assert timeouts[0]["segment"] == "options"
    assert timeouts[0]["deadline_s"] == 0.05
    assert timeouts[0]["elapsed_s"] >= 0.05
    # The SECOND job still ran + warmed (the warm continued past the timeout).
    progress = [e for e in logs if e["event"] == "movers.warm.progress"]
    assert [e["key"] for e in progress] == ["NSE|near|all"]


def test_warm_job_own_timeout_error_is_generic_failure_not_timeout() -> None:
    """A job that RAISES its own TimeoutError is re-raised unchanged by the
    guard (never re-typed to DeadlineExceeded) and takes the generic
    `movers.warm.job_failed` path — the two failure modes stay distinguishable."""
    from structlog.testing import capture_logs

    def _raises_timeout() -> int:
        raise TimeoutError("the job's own timeout — not the guard's")

    with capture_logs() as logs:
        result = warm_movers_snapshots([_raises_timeout], job_deadline_s=5.0)
    assert result == MoversWarmResult(warmed=0, failed=1, total=1)
    assert [e["event"] for e in logs if "job_" in e["event"]] == [
        "movers.warm.job_start",
        "movers.warm.job_failed",
    ]


def test_warm_emits_job_start_with_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    """One `movers.warm.job_start` INFO per coordinate (bounded ≤ 39) carrying
    the resolved deadline — with the default arg the env-resolved bound is used
    (DEC-MOVERS-WARM-UNBLOCK-001 observability: start + done lines bound each
    job's elapsed from the log alone)."""
    from brutex.dashboard.movers_warm import WARM_JOB_DEADLINE_ENV
    from structlog.testing import capture_logs

    monkeypatch.setenv(WARM_JOB_DEADLINE_ENV, "60")
    with capture_logs() as logs:
        warm_movers_snapshots([lambda: 1, lambda: 2], segment="spot", labels=["all", "NSE"])
    starts = [e for e in logs if e["event"] == "movers.warm.job_start"]
    assert [(e["key"], e["index"], e["total"]) for e in starts] == [("all", 1, 2), ("NSE", 2, 2)]
    assert all(e["deadline_s"] == 60.0 and e["segment"] == "spot" for e in starts)
