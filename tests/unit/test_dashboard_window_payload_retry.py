"""Torn-read retry for the /coverage/windows fast path + the pull-progress phases guard.

DEC-WINDOW-TORN-READ-RETRY-001. The window-ledger WRITER is atomic (``os.replace``,
DEC-WINDOW-LEDGER-WRITE-HALT-001 #901), but the ``/coverage/windows`` DuckDB fast path
and the ``/universe/pull-progress`` phase counts each perform MULTIPLE independent
``read_parquet`` opens per request — a concurrent pull's ``os.replace`` landing between
opens yields a footer/data mismatch surfacing as a transient ``duckdb.Error``
(log-observed as a thrift ``TProtocolException``; reader-side only, so it does NOT
contradict #901). These tests pin the fix:

* :func:`build_window_payload` RETRIES the fast path ONCE after ``0.1 s`` on
  ``duckdb.Error`` ONLY (deterministic ``KeyError``/``ValueError`` shape errors fall
  back immediately, sleeper untouched), then falls back to the legacy pyarrow path
  exactly as before — with the ``window_payload.duckdb_fallback`` log ENRICHED
  (``error_type`` + best-effort ledger ``stat`` forensics);
* :func:`phase_progress_payload` gains the SAME retry-once guard and on a second
  ``duckdb.Error`` degrades to the honest ``[]`` no-ledger shape — the
  ``/universe/pull-progress`` poll can never 500 on a torn read;
* the sleeper is INJECTABLE (``retry_sleep``) so every test is deterministic — and the
  default is the ALIASED ``from time import sleep`` (the bare name ``time`` in
  ``db_view`` is ``datetime.time``, so a naive ``time.sleep`` would be an
  ``AttributeError``).

Target: 100% line + branch coverage of the new ``db_view`` block (with the sibling
``test_dashboard_db_view.py`` suite covering the rest of the module).
"""

from __future__ import annotations

# Whitebox viewer tests read module-private helpers by design (DEC-P0-017 convention).
# pyright: reportPrivateUsage=false
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import duckdb
import pytest
from brutex.dashboard.db_view import (
    _DUCKDB_RETRY_DELAY_S,
    _build_window_payload_duckdb,
    _ledger_stat_fields,
    _window_counts_total,
    build_window_payload,
    phase_progress_payload,
)
from brutex.dashboard.movers_cache import MoversSnapshotCache
from brutex.data.fetch_coverage import WindowKey
from brutex.data.window_ledger import WindowLedger
from brutex.data.window_ledger_store import ledger_dir, ledger_path, save_window_ledger
from hypothesis import given
from hypothesis import strategies as st
from structlog.testing import capture_logs

_NOW = datetime(2026, 6, 8, 12, 0, tzinfo=UTC)


def _save_ledger(lake_root: Path) -> None:
    """Save a small REAL ledger (one SUCCESS + one FAILED spot window)."""
    ledger = WindowLedger()
    key = WindowKey(
        market="groww",
        security_id="NIFTY",
        interval="1minute",
        start=date(2021, 1, 1),
        end=date(2021, 4, 1),
    )
    ledger.record(key, raw_bars=5, error_type=None, bars_appended=5, now=_NOW)
    bad = WindowKey(
        market="groww",
        security_id="BANKNIFTY",
        interval="1minute",
        start=date(2021, 1, 1),
        end=date(2021, 4, 1),
    )
    ledger.record(bad, raw_bars=0, error_type="GrowwTimeoutError", bars_appended=0, now=_NOW)
    save_window_ledger(ledger, lake_root)


class _FlakyFastPath:
    """Stateful stand-in for ``_build_window_payload_duckdb`` — raise N times, then answer."""

    def __init__(self, failures: list[BaseException], payload: dict[str, Any]) -> None:
        self.failures = failures
        self.payload = payload
        self.calls = 0

    def __call__(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return self.payload


def _events(logs: Sequence[Mapping[str, Any]], event: str) -> list[Mapping[str, Any]]:
    """Filter captured structlog entries by event name."""
    return [entry for entry in logs if entry["event"] == event]


# ── _ledger_stat_fields — best-effort forensics helper ───────────────────────


def test_ledger_stat_fields_real_file(tmp_path: Path) -> None:
    """A real file yields its true size + mtime_ns (the forensic log payload)."""
    target = tmp_path / "ledger.parquet"
    target.write_bytes(b"0123456789")
    st_ = target.stat()
    assert _ledger_stat_fields(target) == {
        "ledger_size": 10,
        "ledger_mtime_ns": st_.st_mtime_ns,
    }


def test_ledger_stat_fields_missing_file_never_raises(tmp_path: Path) -> None:
    """A stat failure (file replaced/removed) yields honest Nones — never raises."""
    assert _ledger_stat_fields(tmp_path / "gone.parquet") == {
        "ledger_size": None,
        "ledger_mtime_ns": None,
    }


# ── build_window_payload — retry-once on duckdb.Error ────────────────────────


class TestWindowPayloadTornReadRetry:
    """The /coverage/windows fast path retries ONCE on a transient duckdb.Error."""

    def test_transient_torn_read_retries_once_and_succeeds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _save_ledger(tmp_path)
        sentinel: dict[str, Any] = {"source": "ledger", "sentinel": True}
        fake = _FlakyFastPath([duckdb.InvalidInputException("torn footer")], sentinel)
        monkeypatch.setattr("brutex.dashboard.db_view._build_window_payload_duckdb", fake)
        sleeps: list[float] = []
        with capture_logs() as logs:
            payload = build_window_payload(
                tmp_path, conn=duckdb.connect(), retry_sleep=sleeps.append
            )
        assert payload is sentinel  # the RETRY answered — no legacy fallback scan paid
        assert fake.calls == 2
        assert sleeps == [_DUCKDB_RETRY_DELAY_S]  # exactly ONE 0.1 s pause
        (retry,) = _events(logs, "window_payload.duckdb_retry")
        assert retry["error_type"] == "InvalidInputException"
        assert retry["error"] == "torn footer"
        # Best-effort ledger forensics ride the event (size + mtime of the real file).
        real = ledger_path(tmp_path).stat()
        assert retry["ledger_size"] == real.st_size
        assert retry["ledger_mtime_ns"] == real.st_mtime_ns
        assert _events(logs, "window_payload.duckdb_fallback") == []

    def test_persistent_duckdb_error_falls_back_to_legacy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _save_ledger(tmp_path)
        fake = _FlakyFastPath(
            [duckdb.IOException("torn"), duckdb.IOException("still torn")],
            {"never": "reached"},
        )
        monkeypatch.setattr("brutex.dashboard.db_view._build_window_payload_duckdb", fake)
        sleeps: list[float] = []
        with capture_logs() as logs:
            payload = build_window_payload(
                tmp_path, conn=duckdb.connect(), retry_sleep=sleeps.append
            )
        assert fake.calls == 2  # ONE retry, never more
        assert sleeps == [_DUCKDB_RETRY_DELAY_S]
        # The legacy pyarrow path answered with the REAL ledger projection.
        assert payload == build_window_payload(tmp_path)
        assert payload["source"] == "ledger"
        assert payload["count"] == 2
        (retry,) = _events(logs, "window_payload.duckdb_retry")
        assert retry["error_type"] == "IOException"
        (fallback,) = _events(logs, "window_payload.duckdb_fallback")
        assert fallback["error_type"] == "IOException"
        assert fallback["error"] == "still torn"
        assert fallback["ledger_size"] == ledger_path(tmp_path).stat().st_size
        assert isinstance(fallback["ledger_mtime_ns"], int)

    def test_key_error_falls_back_immediately_without_retry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A deterministic row-projection KeyError is NOT retried — retrying a shape
        # error would fail identically and just add latency.
        _save_ledger(tmp_path)
        fake = _FlakyFastPath([KeyError("empty_streak")], {"never": "reached"})
        monkeypatch.setattr("brutex.dashboard.db_view._build_window_payload_duckdb", fake)
        sleeps: list[float] = []
        with capture_logs() as logs:
            payload = build_window_payload(
                tmp_path, conn=duckdb.connect(), retry_sleep=sleeps.append
            )
        assert fake.calls == 1
        assert sleeps == []  # sleeper NEVER called for a non-duckdb.Error
        assert payload == build_window_payload(tmp_path)
        (fallback,) = _events(logs, "window_payload.duckdb_fallback")
        assert fallback["error_type"] == "KeyError"
        assert _events(logs, "window_payload.duckdb_retry") == []

    def test_retry_then_value_error_still_falls_back(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The INNER except keeps the full (duckdb.Error, KeyError, ValueError) net: a
        # torn read whose retry lands on a wrong-shape ledger still degrades gracefully.
        _save_ledger(tmp_path)
        fake = _FlakyFastPath(
            [duckdb.SerializationException("torn"), ValueError("legacy-shape")],
            {"never": "reached"},
        )
        monkeypatch.setattr("brutex.dashboard.db_view._build_window_payload_duckdb", fake)
        sleeps: list[float] = []
        with capture_logs() as logs:
            payload = build_window_payload(
                tmp_path, conn=duckdb.connect(), retry_sleep=sleeps.append
            )
        assert fake.calls == 2
        assert sleeps == [_DUCKDB_RETRY_DELAY_S]
        assert payload == build_window_payload(tmp_path)
        (fallback,) = _events(logs, "window_payload.duckdb_fallback")
        assert fallback["error_type"] == "ValueError"

    def test_default_sleeper_is_the_aliased_time_sleep(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # retry_sleep=None → the module's ALIASED `from time import sleep` binding runs
        # (patched to a recorder so the test never pays a real 100 ms wait). Guards the
        # datetime.time name-collision trap: `time.sleep` in db_view would AttributeError.
        _save_ledger(tmp_path)
        sentinel: dict[str, Any] = {"source": "ledger", "sentinel": True}
        fake = _FlakyFastPath([duckdb.InvalidInputException("torn")], sentinel)
        monkeypatch.setattr("brutex.dashboard.db_view._build_window_payload_duckdb", fake)
        slept: list[float] = []
        monkeypatch.setattr("brutex.dashboard.db_view._default_sleep", slept.append)
        assert build_window_payload(tmp_path, conn=duckdb.connect()) is sentinel
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
        self, tmp_path_factory: pytest.TempPathFactory, exc_cls: type[duckdb.Error]
    ) -> None:
        # DuckDB exposes no version-stable transient subclass (repo precedent), so the
        # retry keys on the BROAD duckdb.Error — ANY subclass gets the one retry.
        lake = tmp_path_factory.mktemp("torn")
        _save_ledger(lake)
        sentinel: dict[str, Any] = {"source": "ledger", "sentinel": True}
        fake = _FlakyFastPath([exc_cls("torn")], sentinel)
        sleeps: list[float] = []
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("brutex.dashboard.db_view._build_window_payload_duckdb", fake)
            payload = build_window_payload(lake, conn=duckdb.connect(), retry_sleep=sleeps.append)
        assert payload is sentinel
        assert sleeps == [_DUCKDB_RETRY_DELAY_S]

    @given(exc_cls=st.sampled_from([KeyError, ValueError]))
    def test_property_shape_errors_are_never_retried(
        self, tmp_path_factory: pytest.TempPathFactory, exc_cls: type[Exception]
    ) -> None:
        lake = tmp_path_factory.mktemp("shape")
        _save_ledger(lake)
        fake = _FlakyFastPath([exc_cls("shape")], {"never": "reached"})
        sleeps: list[float] = []
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("brutex.dashboard.db_view._build_window_payload_duckdb", fake)
            payload = build_window_payload(lake, conn=duckdb.connect(), retry_sleep=sleeps.append)
        assert fake.calls == 1
        assert sleeps == []
        assert payload["source"] == "ledger"  # legacy path answered


# ── phase_progress_payload — retry-once, then honest [] (never a 500) ────────


class _FlakyCounts:
    """Stand-in for ``_window_counts_total`` — raise N times, then delegate to the real one."""

    def __init__(self, failures: list[BaseException]) -> None:
        self.failures = failures
        self.calls = 0

    def __call__(self, conn: Any, glob: str) -> tuple[dict[str, dict[str, int]], int]:
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return _window_counts_total(conn, glob)


class TestPhaseProgressTornReadGuard:
    """phase_progress_payload retries once on duckdb.Error then degrades to []."""

    @pytest.mark.parametrize("use_cache", [False, True], ids=["direct", "cached"])
    def test_transient_torn_read_recovers(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, use_cache: bool
    ) -> None:
        _save_ledger(tmp_path)
        fake = _FlakyCounts([duckdb.InvalidInputException("torn")])
        monkeypatch.setattr("brutex.dashboard.db_view._window_counts_total", fake)
        sleeps: list[float] = []
        cache = MoversSnapshotCache() if use_cache else None
        with capture_logs() as logs:
            rows = phase_progress_payload(
                duckdb.connect(),
                ledger_path(tmp_path),
                current_phase="spot",
                count_cache=cache,
                generation=1,
                retry_sleep=sleeps.append,
            )
        assert [r["phase"] for r in rows] == ["spot", "futures", "options"]
        by_phase = {r["phase"]: r for r in rows}
        assert by_phase["spot"]["done"] == 1  # the REAL counts answered on the retry
        assert by_phase["spot"]["failed"] == 1
        assert fake.calls == 2
        assert sleeps == [_DUCKDB_RETRY_DELAY_S]
        (retry,) = _events(logs, "phase_progress.duckdb_retry")
        assert retry["error_type"] == "InvalidInputException"
        assert retry["ledger_size"] == ledger_path(tmp_path).stat().st_size
        assert _events(logs, "phase_progress.duckdb_fallback") == []

    @pytest.mark.parametrize("use_cache", [False, True], ids=["direct", "cached"])
    def test_persistent_torn_read_degrades_to_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, use_cache: bool
    ) -> None:
        _save_ledger(tmp_path)
        fake = _FlakyCounts([duckdb.IOException("torn"), duckdb.IOException("still torn")])
        monkeypatch.setattr("brutex.dashboard.db_view._window_counts_total", fake)
        sleeps: list[float] = []
        cache = MoversSnapshotCache() if use_cache else None
        with capture_logs() as logs:
            rows = phase_progress_payload(
                duckdb.connect(),
                ledger_path(tmp_path),
                current_phase="spot",
                count_cache=cache,
                generation=1,
                retry_sleep=sleeps.append,
            )
        # The SAME honest empty shape the no-ledger contract serves — never a raise/500.
        assert rows == []
        assert fake.calls == 2
        assert sleeps == [_DUCKDB_RETRY_DELAY_S]
        (fallback,) = _events(logs, "phase_progress.duckdb_fallback")
        assert fallback["error_type"] == "IOException"
        assert fallback["error"] == "still torn"
        assert isinstance(fallback["ledger_size"], int)
        assert isinstance(fallback["ledger_mtime_ns"], int)

    def test_retried_success_is_cached_not_poisoned(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A failed compute caches NOTHING (MoversSnapshotCache contract) and the
        # retried SUCCESS is cached normally — the next poll is an O(1) hit.
        _save_ledger(tmp_path)
        fake = _FlakyCounts([duckdb.InvalidInputException("torn")])
        monkeypatch.setattr("brutex.dashboard.db_view._window_counts_total", fake)
        cache = MoversSnapshotCache()
        sleeps: list[float] = []
        first = phase_progress_payload(
            duckdb.connect(),
            ledger_path(tmp_path),
            current_phase="spot",
            count_cache=cache,
            generation=1,
            retry_sleep=sleeps.append,
        )
        assert fake.calls == 2  # fail once + retried success (which was cached)
        second = phase_progress_payload(
            duckdb.connect(),
            ledger_path(tmp_path),
            current_phase="spot",
            count_cache=cache,
            generation=1,
            retry_sleep=sleeps.append,
        )
        assert fake.calls == 2  # cache HIT — no recompute, no sleep
        assert sleeps == [_DUCKDB_RETRY_DELAY_S]
        assert second == first

    def test_default_sleeper_branch(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # retry_sleep=None → the aliased module default runs (patched to a recorder).
        _save_ledger(tmp_path)
        fake = _FlakyCounts([duckdb.InvalidInputException("torn")])
        monkeypatch.setattr("brutex.dashboard.db_view._window_counts_total", fake)
        slept: list[float] = []
        monkeypatch.setattr("brutex.dashboard.db_view._default_sleep", slept.append)
        rows = phase_progress_payload(duckdb.connect(), ledger_path(tmp_path), current_phase="spot")
        assert [r["phase"] for r in rows] == ["spot", "futures", "options"]
        assert slept == [_DUCKDB_RETRY_DELAY_S]

    def test_real_garbage_parquet_raises_duckdb_error_end_to_end(self, tmp_path: Path) -> None:
        # NO monkeypatch — a REAL garbage-bytes ledger drives the REAL duckdb exception
        # class through the guard (the exception-class assumption is not a test fiction).
        ledger_dir(tmp_path).mkdir(parents=True, exist_ok=True)
        ledger_path(tmp_path).write_bytes(b"definitely not a parquet file")
        sleeps: list[float] = []
        with capture_logs() as logs:
            rows = phase_progress_payload(
                duckdb.connect(),
                ledger_path(tmp_path),
                current_phase="spot",
                retry_sleep=sleeps.append,
            )
        assert rows == []
        assert sleeps == [_DUCKDB_RETRY_DELAY_S]
        (fallback,) = _events(logs, "phase_progress.duckdb_fallback")
        assert fallback["ledger_size"] == len(b"definitely not a parquet file")


# ── direct builder unchanged — the retry lives in the CALLERS only ────────────


def test_direct_duckdb_builder_still_raises_on_torn_read(tmp_path: Path) -> None:
    # _build_window_payload_duckdb itself keeps raising (the retry policy lives in
    # build_window_payload) — a garbage Parquet raises a real duckdb.Error.
    ledger_dir(tmp_path).mkdir(parents=True, exist_ok=True)
    ledger_path(tmp_path).write_bytes(b"garbage")
    with pytest.raises(duckdb.Error):
        _build_window_payload_duckdb(
            duckdb.connect(),
            ledger_path(tmp_path),
            symbol_for_id=None,
            market=None,
            status=None,
            limit=10,
            offset=0,
            count_cache=None,
            generation=0,
        )
