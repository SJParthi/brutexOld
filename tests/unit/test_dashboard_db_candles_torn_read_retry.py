"""Torn-read retry for the ``/db`` candle pagers (DEC-DB-CANDLES-TORN-READ-RETRY-001).

The ``/db`` candle-pager twin of DEC-WINDOW-TORN-READ-RETRY-001: the candle lake
writers land atomically (``os.replace``), but one :func:`query_candles` /
:func:`query_option_candles` request performs MULTIPLE independent
``read_parquet`` opens (the COUNT + the page SELECT + the cursor ``fetchall``
streaming read) — a live pull's replace landing between/inside them surfaces as
a transient ``duckdb.Error`` that previously 500'd the page (the
``fetchall`` at the tail of ``_execute_candle_page_resilient`` and the
corrupt-file degrade path's re-run both sat OUTSIDE the corrupt-skip ``try``).
These tests pin the fix:

* the COUNT and the page execute+fetch are each RETRIED ONCE after ``0.1 s``
  on ``duckdb.Error`` (via the COMMON :func:`_retry_candle_read_once`), each
  attempt re-globbing fresh from the partition directory;
* a PERSISTENT error still raises after exactly one retry (honest envelope —
  retry-once narrows the race, it does not eliminate it; a genuine SQL bug is
  never silently swallowed);
* the sleeper is INJECTABLE (``retry_sleep``) so every test is deterministic —
  and the default is the ALIASED ``from time import sleep`` (the bare name
  ``time`` in ``db_view`` is ``datetime.time``);
* the no-error path pays ZERO sleeps and is behaviour-identical;
* a retried success is cached normally by the injected count cache (never a
  poisoned entry);
* the returned ``total`` is clamped to the honest observed lower bound
  ``offset + len(rows)`` — the CACHED-total vs live-page skew is inherent to
  the DEC-DB-CANDLE-COUNT-CACHE-001 generation-keyed design (documented, not
  eliminated), but the pager never reports a total the served page disproves.

Target: 100% line + branch coverage of the new ``db_view`` block alongside the
sibling suites in the per-module gate.
"""

from __future__ import annotations

# Whitebox viewer tests read module-private helpers by design (DEC-P0-017 convention).
# pyright: reportPrivateUsage=false
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("duckdb", reason="the candle pagers read a real Parquet lake")

import duckdb
from brutex.dashboard import db_view
from brutex.dashboard.db_view import (
    _DUCKDB_RETRY_DELAY_S,
    _retry_candle_read_once,
    query_candles,
    query_option_candles,
)
from brutex.dashboard.movers_cache import MoversSnapshotCache
from brutex.data.models import CandleBar, OptionBar
from brutex.data.option_parquet_writer import OptionParquetWriter, OptionSeriesCoordinate
from brutex.data.parquet_writer import ParquetWriter
from hypothesis import given
from hypothesis import strategies as st
from structlog.testing import capture_logs

#: The filesystem-safe series segment the option writer derives from this PK.
_OPT_PK = "NSE_FNO|13|WEEK|0|ATM|CALL"
_OPT_SERIES = "NSE_FNO__13__WEEK__0__ATM__CALL"


def _bar(ts: datetime) -> CandleBar:
    """One spot bar (2-dp money in [low, high], OI null for CASH)."""
    return CandleBar(
        timestamp=ts,
        open=Decimal("100.00"),
        high=Decimal("101.00"),
        low=Decimal("99.00"),
        close=Decimal("100.00"),
        volume=10,
        open_interest=None,
    )


def _write(lake: Path, *, n_days: int = 3) -> None:
    """Write *n_days* single-bar daily partitions (2024-03-01, 02, …) for NSE-NIFTY."""
    bars = [_bar(datetime(2024, 3, d, 9, 15, tzinfo=UTC)) for d in range(1, n_days + 1)]
    ParquetWriter(lake_root=lake).write(
        exchange="NSE", segment="CASH", symbol="NSE-NIFTY", interval="1minute", bars=bars
    )


def _obar(ts: datetime) -> OptionBar:
    """One expired-option CE bar (full-precision iv, 2dp money like the writer)."""
    return OptionBar(
        timestamp=ts,
        open=Decimal("100.00"),
        high=Decimal("110.10"),
        low=Decimal("90.00"),
        close=Decimal("105.00"),
        volume=250,
        open_interest=1200,
        side="CE",
        strike=Decimal("17500.00"),
        spot=Decimal("17480.55"),
        iv=Decimal("0.1523"),
    )


def _write_options(lake: Path, *, n_bars: int = 3) -> None:
    """Write *n_bars* CE bars (one per minute) for the fixed series via the real writer."""
    seg, sec, flag, code, offset, _typ = _OPT_PK.split("|")
    coord = OptionSeriesCoordinate(
        exch_id="NSE",
        exchange_segment=seg,
        security_id=int(sec),
        underlying_symbol="NIFTY",
        expiry_flag=flag,
        expiry_code=int(code),
        strike_offset=offset,
    )
    bars = [_obar(datetime(2024, 3, 1, 4, m, tzinfo=UTC)) for m in range(n_bars)]
    OptionParquetWriter(lake_root=lake).write(partition_key=_OPT_PK, coordinate=coord, bars=bars)


def _candles(
    conn: Any,
    lake: Path,
    *,
    limit: int = 10,
    offset: int = 0,
    count_cache: MoversSnapshotCache | None = None,
    generation: int = 0,
    retry_sleep: Callable[[float], None] | None = None,
) -> dict[str, Any]:
    """Call ``query_candles`` over the fixed NSE/CASH/1minute partition with typed kwargs."""
    return query_candles(
        conn,
        lake,
        exchange="NSE",
        segment="CASH",
        symbol="NSE-NIFTY",
        interval="1minute",
        limit=limit,
        offset=offset,
        count_cache=count_cache,
        generation=generation,
        retry_sleep=retry_sleep,
    )


def _opt(
    conn: Any,
    lake: Path,
    *,
    limit: int = 10,
    offset: int = 0,
    count_cache: MoversSnapshotCache | None = None,
    generation: int = 0,
    retry_sleep: Callable[[float], None] | None = None,
) -> dict[str, Any]:
    """Call ``query_option_candles`` over the fixed series with typed kwargs."""
    return query_option_candles(
        conn,
        lake,
        series=_OPT_SERIES,
        limit=limit,
        offset=offset,
        count_cache=count_cache,
        generation=generation,
        retry_sleep=retry_sleep,
    )


class _FlakyDelegate:
    """Stateful stand-in for a db_view read helper — raise N times, then delegate.

    Mirrors the ``_FlakyFastPath`` idiom of the window torn-read suite: the
    first ``len(failures)`` calls raise (simulating the mid-``os.replace``
    race), every later call delegates to the REAL helper so the retry serves
    the REAL page/count.
    """

    def __init__(self, real: Callable[..., Any], failures: list[BaseException]) -> None:
        self.real = real
        self.failures = failures
        self.calls = 0

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return self.real(*args, **kwargs)


def _events(logs: Sequence[Mapping[str, Any]], event: str) -> list[Mapping[str, Any]]:
    """Filter captured structlog entries by event name."""
    return [entry for entry in logs if entry["event"] == event]


@pytest.fixture
def conn() -> Any:
    """A real in-process DuckDB connection (read_parquet over the tmp lake)."""
    return duckdb.connect()


# ── _retry_candle_read_once — the COMMON guard primitive ─────────────────────


class TestRetryCandleReadOnce:
    """The guard itself: retry-once on duckdb.Error, both sleeper arms, honest re-raise."""

    def test_no_error_returns_first_answer_zero_sleeps(self) -> None:
        sleeps: list[float] = []
        out = _retry_candle_read_once(lambda: 41 + 1, source="t", retry_sleep=sleeps.append)
        assert out == 42
        assert sleeps == []

    def test_transient_error_retried_once_after_delay(self) -> None:
        flaky = _FlakyDelegate(lambda: "answer", [duckdb.InvalidInputException("torn footer")])
        sleeps: list[float] = []
        with capture_logs() as logs:
            out = _retry_candle_read_once(flaky, source="t", retry_sleep=sleeps.append)
        assert out == "answer"
        assert flaky.calls == 2
        assert sleeps == [_DUCKDB_RETRY_DELAY_S]
        (retry,) = _events(logs, "db.candle_read_retry")
        assert retry["source"] == "t"
        assert retry["error_type"] == "InvalidInputException"
        assert retry["error"] == "torn footer"

    def test_persistent_error_raises_after_exactly_one_retry(self) -> None:
        # Honest envelope: a SECOND replace inside the retry window (or a genuine
        # SQL bug) still raises — never silently swallowed, never a retry storm.
        flaky = _FlakyDelegate(
            lambda: "never", [duckdb.IOException("torn"), duckdb.IOException("still torn")]
        )
        sleeps: list[float] = []
        with pytest.raises(duckdb.Error, match="still torn"):
            _retry_candle_read_once(flaky, source="t", retry_sleep=sleeps.append)
        assert flaky.calls == 2
        assert sleeps == [_DUCKDB_RETRY_DELAY_S]

    def test_non_duckdb_error_propagates_without_retry(self) -> None:
        # A deterministic shape/code bug is NOT retried — retrying would fail
        # identically and just add latency (the window-guard contract).
        flaky = _FlakyDelegate(lambda: "never", [ValueError("shape")])
        sleeps: list[float] = []
        with pytest.raises(ValueError, match="shape"):
            _retry_candle_read_once(flaky, source="t", retry_sleep=sleeps.append)
        assert flaky.calls == 1
        assert sleeps == []

    def test_default_sleeper_is_the_aliased_time_sleep(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # retry_sleep=None → the module's ALIASED `from time import sleep` binding
        # runs (patched to a recorder so the test never pays a real 100 ms wait).
        flaky = _FlakyDelegate(lambda: "answer", [duckdb.InvalidInputException("torn")])
        slept: list[float] = []
        monkeypatch.setattr("brutex.dashboard.db_view._default_sleep", slept.append)
        assert _retry_candle_read_once(flaky, source="t", retry_sleep=None) == "answer"
        assert slept == [_DUCKDB_RETRY_DELAY_S]

    @given(
        exc_cls=st.sampled_from(
            [
                duckdb.Error,
                duckdb.InvalidInputException,
                duckdb.IOException,
                duckdb.SerializationException,
                duckdb.OperationalError,
                duckdb.InternalException,
            ]
        )
    )
    def test_property_any_duckdb_error_subclass_is_retried(
        self, exc_cls: type[duckdb.Error]
    ) -> None:
        # DuckDB exposes no version-stable transient subclass (repo precedent), so
        # the retry keys on the BROAD duckdb.Error — ANY subclass gets the one retry.
        flaky = _FlakyDelegate(lambda: "answer", [exc_cls("torn")])
        sleeps: list[float] = []
        out = _retry_candle_read_once(flaky, source="t", retry_sleep=sleeps.append)
        assert out == "answer"
        assert sleeps == [_DUCKDB_RETRY_DELAY_S]


# ── query_candles — the spot/F&O pager rides the guard ───────────────────────


class TestQueryCandlesTornReadRetry:
    """The COUNT + page executes each retry once on a transient duckdb.Error."""

    def test_count_torn_read_retries_once_and_recovers(
        self, conn: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The COUNT is the FIRST resilient read of the request; failing it once
        # (the mid-replace race) must recover on the retry with the REAL total.
        _write(tmp_path, n_days=3)
        flaky = _FlakyDelegate(
            db_view._execute_parquet_resilient, [duckdb.InvalidInputException("torn footer")]
        )
        monkeypatch.setattr(db_view, "_execute_parquet_resilient", flaky)
        sleeps: list[float] = []
        with capture_logs() as logs:
            page = _candles(conn, tmp_path, retry_sleep=sleeps.append)
        assert page["total"] == 3
        assert len(page["rows"]) == 3
        assert sleeps == [_DUCKDB_RETRY_DELAY_S]  # exactly ONE 0.1 s pause
        (retry,) = _events(logs, "db.candle_read_retry")
        assert retry["source"] == "candle:NSE/CASH/NSE-NIFTY/1minute"
        assert retry["error_type"] == "InvalidInputException"

    def test_page_torn_read_retries_once_and_recovers(
        self, conn: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The page execute+fetch (the previously-unguarded fetchall window) —
        # one failure, then the REAL page on the retry.
        _write(tmp_path, n_days=3)
        flaky = _FlakyDelegate(
            db_view._execute_candle_page_resilient, [duckdb.SerializationException("torn")]
        )
        monkeypatch.setattr(db_view, "_execute_candle_page_resilient", flaky)
        sleeps: list[float] = []
        with capture_logs() as logs:
            page = _candles(conn, tmp_path, retry_sleep=sleeps.append)
        assert flaky.calls == 2
        assert len(page["rows"]) == 3
        assert page["total"] == 3
        assert sleeps == [_DUCKDB_RETRY_DELAY_S]
        (retry,) = _events(logs, "db.candle_read_retry")
        assert retry["error_type"] == "SerializationException"

    def test_persistent_page_error_still_raises_after_one_retry(
        self, conn: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Honest envelope: retry-once NARROWS the race — a second replace inside
        # the retry window still raises (the route 500s), never a retry storm.
        _write(tmp_path, n_days=2)
        flaky = _FlakyDelegate(
            db_view._execute_candle_page_resilient,
            [duckdb.IOException("torn"), duckdb.IOException("still torn")],
        )
        monkeypatch.setattr(db_view, "_execute_candle_page_resilient", flaky)
        sleeps: list[float] = []
        with pytest.raises(duckdb.Error, match="still torn"):
            _candles(conn, tmp_path, retry_sleep=sleeps.append)
        assert flaky.calls == 2  # ONE retry, never more
        assert sleeps == [_DUCKDB_RETRY_DELAY_S]

    def test_no_error_path_zero_sleeps_and_unchanged_payload(
        self, conn: Any, tmp_path: Path
    ) -> None:
        # The steady state pays no sleep and serves the byte-identical payload
        # the parameterless call serves (the guard is pure pass-through).
        _write(tmp_path, n_days=3)
        sleeps: list[float] = []
        guarded = _candles(conn, tmp_path, retry_sleep=sleeps.append)
        plain = _candles(conn, tmp_path)
        assert sleeps == []
        assert guarded == plain
        assert guarded["total"] == 3

    def test_retried_count_success_is_cached_not_poisoned(
        self, conn: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The retry runs INSIDE the count compute, so the retried success is
        # cached normally — the next paging click is an O(1) hit (no re-count,
        # no second sleep). The COUNT tally is proven at the CONNECTION level:
        # the flaky wrapper's first failure raises BEFORE any conn.execute, so
        # exactly ONE COUNT reaches DuckDB across both clicks.
        _write(tmp_path, n_days=2)
        flaky = _FlakyDelegate(
            db_view._execute_parquet_resilient, [duckdb.InvalidInputException("torn")]
        )
        monkeypatch.setattr(db_view, "_execute_parquet_resilient", flaky)

        class _CountingConn:
            def __init__(self, real: Any) -> None:
                self._real = real
                self.count_execs = 0

            def execute(self, sql: str, parameters: Any = None) -> Any:
                if "COUNT(" in sql:
                    self.count_execs += 1
                if parameters is None:
                    return self._real.execute(sql)
                return self._real.execute(sql, parameters)

        counting = _CountingConn(conn)
        cache = MoversSnapshotCache()
        sleeps: list[float] = []
        first = _candles(
            counting, tmp_path, count_cache=cache, generation=1, retry_sleep=sleeps.append
        )
        second = _candles(
            counting, tmp_path, count_cache=cache, generation=1, retry_sleep=sleeps.append
        )
        assert first["total"] == second["total"] == 2
        assert sleeps == [_DUCKDB_RETRY_DELAY_S]  # only the FIRST click paid the retry
        assert counting.count_execs == 1, "the retried success was cached — no recount"

    def test_total_clamped_to_observed_lower_bound(self, conn: Any, tmp_path: Path) -> None:
        # The cached-total vs live-page skew is INHERENT to the generation-keyed
        # count cache (DEC-DB-CANDLE-COUNT-CACHE-001) — a concurrent writer inside
        # one generation leaves the cached total stale. The pager now clamps the
        # returned total to offset + len(rows): never a total the page disproves.
        _write(tmp_path, n_days=2)
        cache = MoversSnapshotCache()
        first = _candles(conn, tmp_path, count_cache=cache, generation=5)
        assert first["total"] == 2
        # A concurrent writer lands a third day WITHOUT a generation bump.
        _write(tmp_path, n_days=3)
        second = _candles(conn, tmp_path, count_cache=cache, generation=5)
        assert len(second["rows"]) == 3
        assert second["total"] == 3, "the served page disproves the stale cached 2"


# ── query_option_candles — the expired-options twin ──────────────────────────


class TestQueryOptionCandlesTornReadRetry:
    """The options pager runs the SAME two-query shape — identical guard wiring."""

    def test_count_torn_read_retries_once_and_recovers(
        self, conn: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_options(tmp_path, n_bars=3)
        flaky = _FlakyDelegate(
            db_view._execute_parquet_resilient, [duckdb.InvalidInputException("torn footer")]
        )
        monkeypatch.setattr(db_view, "_execute_parquet_resilient", flaky)
        sleeps: list[float] = []
        with capture_logs() as logs:
            page = _opt(conn, tmp_path, retry_sleep=sleeps.append)
        assert page["total"] == 3
        assert len(page["rows"]) == 3
        assert sleeps == [_DUCKDB_RETRY_DELAY_S]
        (retry,) = _events(logs, "db.candle_read_retry")
        assert retry["source"] == f"option-candle:{_OPT_SERIES}"
        assert retry["error_type"] == "InvalidInputException"

    def test_page_torn_read_retries_once_and_recovers(
        self, conn: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_options(tmp_path, n_bars=3)
        flaky = _FlakyDelegate(
            db_view._execute_candle_page_resilient, [duckdb.SerializationException("torn")]
        )
        monkeypatch.setattr(db_view, "_execute_candle_page_resilient", flaky)
        sleeps: list[float] = []
        page = _opt(conn, tmp_path, retry_sleep=sleeps.append)
        assert flaky.calls == 2
        assert len(page["rows"]) == 3
        assert page["total"] == 3
        assert sleeps == [_DUCKDB_RETRY_DELAY_S]

    def test_persistent_count_error_still_raises_after_one_retry(
        self, conn: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_options(tmp_path, n_bars=2)
        flaky = _FlakyDelegate(
            db_view._execute_parquet_resilient,
            [duckdb.IOException("torn"), duckdb.IOException("still torn")],
        )
        monkeypatch.setattr(db_view, "_execute_parquet_resilient", flaky)
        sleeps: list[float] = []
        with pytest.raises(duckdb.Error, match="still torn"):
            _opt(conn, tmp_path, retry_sleep=sleeps.append)
        assert flaky.calls == 2
        assert sleeps == [_DUCKDB_RETRY_DELAY_S]

    def test_no_error_path_zero_sleeps_and_unchanged_payload(
        self, conn: Any, tmp_path: Path
    ) -> None:
        _write_options(tmp_path, n_bars=3)
        sleeps: list[float] = []
        guarded = _opt(conn, tmp_path, retry_sleep=sleeps.append)
        plain = _opt(conn, tmp_path)
        assert sleeps == []
        assert guarded == plain
        assert guarded["total"] == 3

    def test_total_clamped_to_observed_lower_bound(self, conn: Any, tmp_path: Path) -> None:
        # The expired-options twin of the spot clamp: a stale cached total is
        # raised to the observed offset + len(rows) lower bound.
        _write_options(tmp_path, n_bars=2)
        cache = MoversSnapshotCache()
        first = _opt(conn, tmp_path, count_cache=cache, generation=5)
        assert first["total"] == 2
        _write_options(tmp_path, n_bars=3)
        second = _opt(conn, tmp_path, count_cache=cache, generation=5)
        assert len(second["rows"]) == 3
        assert second["total"] == 3, "the served page disproves the stale cached 2"
