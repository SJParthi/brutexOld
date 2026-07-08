"""Tests for `brutex.data.session_coverage` (DEC-SWEEP-PREFLIGHT-ESTIMATOR-001).

100% line+branch over the READ-ONLY sweep-preflight scanners: session-DATE
coverage from real tiny month parquets (duplicate-timestamp collapse, the
``1day`` UTC-straddle fixture, out-of-span months never opened — spy),
span-bounded PRESENCE for derived TFs, the garbage-bytes corrupt-month arm
(R38 — named refusal, never a traceback), and the ledger-quarantine scan
(ONE read-only parse; both-forms ``security_id``; ISO-string window overlap;
the bimodal-SKIPPED split; ``load_failed`` surfaced verbatim). Probes NEVER
write — pinned by recursive tree snapshots.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import polars as pl
import pytest
from brutex.data.interior_gap_detect import INTERIOR_GAP
from brutex.data.session import ALL_FILTERED
from brutex.data.session_coverage import (
    DATE_CHECKED_INTERVALS,
    LedgerRowFlag,
    PairScan,
    QuarantineScan,
    ScanKind,
    scan_ledger_quarantine,
    scan_selection_coverage,
)
from brutex.data.window_ledger import (
    FAILED,
    SKIPPED,
    SUCCESS,
    LedgerRowTuple,
    WindowLedger,
    expired_window_settle_reason,
)
from brutex.data.window_ledger_store import ledger_path, save_window_ledger
from structlog.testing import capture_logs

_EXCHANGE = "NSE"
_SEGMENT = "INDEX"
_SYMBOL = "NSE-NIFTY"


def _write_month(
    lake: Path, interval: str, year: int, month: int, timestamps: list[datetime]
) -> Path:
    """Write a minimal real month parquet holding only the ``timestamp`` column."""
    path = lake / "bars" / _EXCHANGE / _SEGMENT / _SYMBOL / interval / f"{year:04d}"
    path.mkdir(parents=True, exist_ok=True)
    file = path / f"{month:02d}.parquet"
    frame = pl.DataFrame({"timestamp": timestamps}, schema={"timestamp": pl.Datetime("us", "UTC")})
    frame.write_parquet(file)
    return file


def _ist_session_ts(day: date, hour: int = 4, minute: int = 0) -> datetime:
    """A UTC bar timestamp whose IST session date equals *day* (09:30 IST default)."""
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC)


def _tree_snapshot(root: Path) -> list[tuple[str, int, int]]:
    """Recursive ``(relpath, size, mtime_ns)`` snapshot — the nothing-written oracle."""
    out: list[tuple[str, int, int]] = []
    for path in sorted(root.rglob("*")):
        stat = path.stat()
        out.append((str(path.relative_to(root)), stat.st_size, stat.st_mtime_ns))
    return out


def _scan_one(lake: Path, interval: str, start: date, end: date) -> PairScan:
    scans = scan_selection_coverage(
        lake_root=lake,
        exchange=_EXCHANGE,
        segment=_SEGMENT,
        symbols=[_SYMBOL],
        intervals=[interval],
        start=start,
        end=end,
    )
    return scans[(_SYMBOL, interval)]


class TestScanKindClassification:
    def test_date_checked_intervals_are_exactly_1minute_and_1day(self) -> None:
        assert frozenset({"1minute", "1day"}) == DATE_CHECKED_INTERVALS

    def test_1minute_and_1day_scan_dates_derived_tf_scans_presence(self, tmp_path: Path) -> None:
        scans = scan_selection_coverage(
            lake_root=tmp_path,
            exchange=_EXCHANGE,
            segment=_SEGMENT,
            symbols=[_SYMBOL],
            intervals=["1minute", "1day", "5minute"],
            start=date(2025, 3, 1),
            end=date(2025, 3, 31),
        )
        assert scans[(_SYMBOL, "1minute")].kind is ScanKind.DATES
        assert scans[(_SYMBOL, "1day")].kind is ScanKind.DATES
        assert scans[(_SYMBOL, "5minute")].kind is ScanKind.PRESENCE


class TestDatesScan:
    def test_covered_session_dates_golden(self, tmp_path: Path) -> None:
        """Distinct IST session dates across two month files, span-intersected."""
        _write_month(
            tmp_path,
            "1minute",
            2025,
            3,
            [
                _ist_session_ts(date(2025, 3, 3)),
                _ist_session_ts(date(2025, 3, 3), minute=1),
                _ist_session_ts(date(2025, 3, 4)),
            ],
        )
        _write_month(tmp_path, "1minute", 2025, 4, [_ist_session_ts(date(2025, 4, 1))])
        scan = _scan_one(tmp_path, "1minute", date(2025, 3, 1), date(2025, 4, 30))
        assert scan.covered == frozenset({date(2025, 3, 3), date(2025, 3, 4), date(2025, 4, 1)})
        assert scan.present is True
        assert scan.bars_found == 4
        assert scan.corrupt_files == ()
        # Padded month set for [Mar 1, Apr 30]: Feb..May = 4 coordinates probed.
        assert scan.months_probed == 4

    def test_missing_partition_reads_empty_not_present(self, tmp_path: Path) -> None:
        scan = _scan_one(tmp_path, "1minute", date(2025, 3, 1), date(2025, 3, 31))
        assert scan.covered == frozenset()
        assert scan.present is False
        assert scan.bars_found == 0
        assert scan.months_probed == 3  # Feb, Mar, Apr — probed, none existed

    def test_duplicate_timestamps_collapse_in_bars_found(self, tmp_path: Path) -> None:
        """Physical rows ≠ bars — a duplicated row never inflates the tally."""
        ts = _ist_session_ts(date(2025, 3, 3))
        _write_month(tmp_path, "1minute", 2025, 3, [ts, ts, ts])
        scan = _scan_one(tmp_path, "1minute", date(2025, 3, 1), date(2025, 3, 31))
        assert scan.bars_found == 1
        assert scan.covered == frozenset({date(2025, 3, 3)})

    def test_out_of_span_ist_dates_excluded_from_covered(self, tmp_path: Path) -> None:
        """A padded month file's rows outside [start, end] never count covered."""
        _write_month(
            tmp_path,
            "1minute",
            2025,
            2,
            [_ist_session_ts(date(2025, 2, 27))],  # before the requested start
        )
        scan = _scan_one(tmp_path, "1minute", date(2025, 3, 1), date(2025, 3, 31))
        assert scan.covered == frozenset()
        assert scan.bars_found == 0
        assert scan.present is True  # the padded month file physically exists

    def test_1day_utc_straddle_midnight_ist_bar_found_in_prior_utc_month(
        self, tmp_path: Path
    ) -> None:
        """A midnight-IST Jan-1 ``1day`` bar lives in the DECEMBER UTC file — found."""
        # 2025-12-31T18:30:00Z == 2026-01-01T00:00 IST — partitioned under 2025/12.
        _write_month(
            tmp_path,
            "1day",
            2025,
            12,
            [datetime(2025, 12, 31, 18, 30, tzinfo=UTC)],
        )
        scan = _scan_one(tmp_path, "1day", date(2026, 1, 1), date(2026, 1, 1))
        assert scan.kind is ScanKind.DATES
        assert scan.covered == frozenset({date(2026, 1, 1)})
        assert scan.months_probed == 2  # (2025, 12) + (2026, 1) — the ±1-day pad

    def test_r38_corrupt_partition_during_scan_named_refusal_not_traceback(
        self, tmp_path: Path
    ) -> None:
        """R38-class: a garbage-bytes month file is NAMED, never a traceback."""
        good = _write_month(tmp_path, "1minute", 2025, 3, [_ist_session_ts(date(2025, 3, 3))])
        corrupt = good.parent / "04.parquet"
        corrupt.write_bytes(b"this is not a parquet file")
        with capture_logs() as logs:
            scan = _scan_one(tmp_path, "1minute", date(2025, 3, 1), date(2025, 4, 30))
        assert scan.corrupt_files == (str(corrupt),)
        # The readable month is STILL scanned — gather-all, never fail-fast.
        assert scan.covered == frozenset({date(2025, 3, 3)})
        events = [e for e in logs if e["event"] == "session_coverage.corrupt_file"]
        assert len(events) == 1
        assert events[0]["path"] == str(corrupt)

    def test_out_of_span_months_never_opened_spy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only padded-span month files are read — unrelated months untouched."""
        out_of_span = _write_month(
            tmp_path, "1minute", 2024, 1, [_ist_session_ts(date(2024, 1, 2))]
        )
        in_span = _write_month(tmp_path, "1minute", 2025, 3, [_ist_session_ts(date(2025, 3, 3))])
        opened: list[str] = []
        real_read = pl.read_parquet

        def spy(source: Any, *args: Any, **kwargs: Any) -> Any:
            opened.append(str(source))
            return real_read(source, *args, **kwargs)

        monkeypatch.setattr(pl, "read_parquet", spy)
        scan = _scan_one(tmp_path, "1minute", date(2025, 3, 1), date(2025, 3, 31))
        assert scan.covered == frozenset({date(2025, 3, 3)})
        assert opened == [str(in_span)]
        assert str(out_of_span) not in opened

    def test_reversed_span_probes_nothing(self, tmp_path: Path) -> None:
        """A reversed range yields an empty month set — zero probes, zero reads."""
        _write_month(tmp_path, "1minute", 2025, 3, [_ist_session_ts(date(2025, 3, 3))])
        scan = _scan_one(tmp_path, "1minute", date(2025, 3, 10), date(2025, 3, 5))
        assert scan.months_probed == 0
        assert scan.covered == frozenset()
        assert scan.present is False


class TestPresenceScan:
    def test_in_span_month_file_present_short_circuits(self, tmp_path: Path) -> None:
        _write_month(tmp_path, "5minute", 2025, 2, [_ist_session_ts(date(2025, 2, 27))])
        scan = _scan_one(tmp_path, "5minute", date(2025, 3, 1), date(2025, 3, 31))
        assert scan.kind is ScanKind.PRESENCE
        assert scan.present is True
        assert scan.months_probed == 1  # first padded month (Feb) hit — short-circuit
        assert scan.covered == frozenset()
        assert scan.bars_found == 0

    def test_only_out_of_span_months_read_absent(self, tmp_path: Path) -> None:
        """Span-bounded — an out-of-range-only partition does NOT read present."""
        _write_month(tmp_path, "5minute", 2023, 5, [_ist_session_ts(date(2023, 5, 2))])
        scan = _scan_one(tmp_path, "5minute", date(2025, 3, 1), date(2025, 3, 31))
        assert scan.present is False
        assert scan.months_probed == 3  # Feb, Mar, Apr — exhausted, no hit

    def test_missing_interval_dir_absent_zero_probes(self, tmp_path: Path) -> None:
        scan = _scan_one(tmp_path, "15minute", date(2025, 3, 1), date(2025, 3, 31))
        assert scan.present is False
        assert scan.months_probed == 0

    def test_presence_never_reads_file_contents(self, tmp_path: Path) -> None:
        """A garbage-bytes derived month still reads PRESENT — existence only."""
        garbage = tmp_path / "bars" / _EXCHANGE / _SEGMENT / _SYMBOL / "5minute" / "2025"
        garbage.mkdir(parents=True)
        (garbage / "03.parquet").write_bytes(b"not parquet at all")
        scan = _scan_one(tmp_path, "5minute", date(2025, 3, 1), date(2025, 3, 31))
        assert scan.present is True
        assert scan.corrupt_files == ()

    def test_dir_present_but_reversed_span_absent(self, tmp_path: Path) -> None:
        """Empty month set with an existing dir — zero probes, honest absent."""
        _write_month(tmp_path, "5minute", 2025, 3, [_ist_session_ts(date(2025, 3, 3))])
        scan = _scan_one(tmp_path, "5minute", date(2025, 3, 10), date(2025, 3, 5))
        assert scan.present is False
        assert scan.months_probed == 0


class TestScanSelectionCoverage:
    def test_every_pair_scanned_in_request_order_symbols_major(self, tmp_path: Path) -> None:
        scans = scan_selection_coverage(
            lake_root=tmp_path,
            exchange=_EXCHANGE,
            segment=_SEGMENT,
            symbols=["NSE-NIFTY", "NSE-BANKNIFTY"],
            intervals=["1minute", "5minute"],
            start=date(2025, 3, 1),
            end=date(2025, 3, 31),
        )
        assert list(scans) == [
            ("NSE-NIFTY", "1minute"),
            ("NSE-NIFTY", "5minute"),
            ("NSE-BANKNIFTY", "1minute"),
            ("NSE-BANKNIFTY", "5minute"),
        ]

    def test_scan_never_writes(self, tmp_path: Path) -> None:
        """Preflight probes NEVER write — recursive tree snapshot identical."""
        _write_month(tmp_path, "1minute", 2025, 3, [_ist_session_ts(date(2025, 3, 3))])
        before = _tree_snapshot(tmp_path)
        scan_selection_coverage(
            lake_root=tmp_path,
            exchange=_EXCHANGE,
            segment=_SEGMENT,
            symbols=[_SYMBOL],
            intervals=["1minute", "1day", "5minute"],
            start=date(2025, 3, 1),
            end=date(2025, 3, 31),
        )
        assert _tree_snapshot(tmp_path) == before


_NOW = "2026-07-01T00:00:00+00:00"


def _row(
    security_id: str,
    interval: str,
    window_start: str,
    window_end: str,
    status: str,
    last_error: str | None,
) -> LedgerRowTuple:
    return (
        "groww",
        security_id,
        interval,
        window_start,
        window_end,
        status,
        0,
        1,
        last_error,
        _NOW,
        _NOW,
        0,
        1,
        None,
        None,
        None,
    )


def _seed_ledger(lake: Path) -> None:
    """Persist a real ledger via the real store writer (read back by the scan)."""
    ledger = WindowLedger.from_rows(
        [
            # Demoted-OPEN gap, prefixed form — flagged.
            _row("NSE-NIFTY", "1minute", "2025-03-01", "2025-03-31", FAILED, INTERIOR_GAP),
            # Accepted gap, BARE form — flagged (both-forms security_id).
            _row("NIFTY", "1minute", "2025-03-01", "2025-03-31", SKIPPED, ALL_FILTERED),
            # Boundary overlap: window_end == requested start — flagged.
            _row("NSE-NIFTY", "1minute", "2025-02-01", "2025-03-01", FAILED, "DEGRADED"),
            # Expired-window settle — NOT a demotion tag, never flagged.
            _row(
                "NSE-NIFTY",
                "1minute",
                "2025-03-15",
                "2025-03-20",
                SKIPPED,
                expired_window_settle_reason(1),
            ),
            # Transient error class — not a demotion tag.
            _row("NSE-NIFTY", "1minute", "2025-03-05", "2025-03-10", FAILED, "GrowwTimeoutError"),
            # FAILED with no error at all.
            _row("NSE-NIFTY", "1minute", "2025-03-11", "2025-03-12", FAILED, None),
            # SUCCESS rows never flag, whatever the tag says (status filter).
            _row("NSE-NIFTY", "1minute", "2025-03-13", "2025-03-14", SUCCESS, INTERIOR_GAP),
            # Unrequested symbol.
            _row("NSE-BANKNIFTY", "1minute", "2025-03-01", "2025-03-31", FAILED, INTERIOR_GAP),
            # Unrequested interval.
            _row("NSE-NIFTY", "5minute", "2025-03-01", "2025-03-31", FAILED, INTERIOR_GAP),
            # Window entirely BEFORE the range (window_end < start).
            _row("NSE-NIFTY", "1minute", "2025-01-01", "2025-01-31", FAILED, INTERIOR_GAP),
            # Window entirely AFTER the range (window_start > end).
            _row("NSE-NIFTY", "1minute", "2025-05-01", "2025-05-31", FAILED, INTERIOR_GAP),
        ]
    )
    save_window_ledger(ledger, lake)


class TestScanLedgerQuarantine:
    def test_r4_flags_demoted_rows_both_forms_and_overlap_only(self, tmp_path: Path) -> None:
        """FAILED+tag and SKIPPED+tag overlap rows flag; everything else stays out."""
        _seed_ledger(tmp_path)
        scan = scan_ledger_quarantine(
            tmp_path,
            exchange=_EXCHANGE,
            symbols=["NIFTY"],
            intervals=["1minute"],
            start=date(2025, 3, 1),
            end=date(2025, 3, 31),
        )
        assert scan.load_failed is False
        assert scan.flags == (
            LedgerRowFlag(
                security_id="NIFTY",
                interval="1minute",
                window_start="2025-03-01",
                window_end="2025-03-31",
                status=SKIPPED,
                last_error=ALL_FILTERED,
            ),
            LedgerRowFlag(
                security_id="NSE-NIFTY",
                interval="1minute",
                window_start="2025-02-01",
                window_end="2025-03-01",
                status=FAILED,
                last_error="DEGRADED",
            ),
            LedgerRowFlag(
                security_id="NSE-NIFTY",
                interval="1minute",
                window_start="2025-03-01",
                window_end="2025-03-31",
                status=FAILED,
                last_error=INTERIOR_GAP,
            ),
        )

    def test_r4_bimodal_skipped_split_expired_settle_never_flags(self, tmp_path: Path) -> None:
        """SKIPPED+ALL_FILTERED (accept-gap) flags; SKIPPED+expired-empty does NOT."""
        _seed_ledger(tmp_path)
        scan = scan_ledger_quarantine(
            tmp_path,
            exchange=_EXCHANGE,
            symbols=["NIFTY"],
            intervals=["1minute"],
            start=date(2025, 3, 1),
            end=date(2025, 3, 31),
        )
        skipped = [f for f in scan.flags if f.status == SKIPPED]
        assert [f.last_error for f in skipped] == [ALL_FILTERED]

    def test_no_ledger_reads_clean_empty(self, tmp_path: Path) -> None:
        scan = scan_ledger_quarantine(
            tmp_path,
            exchange=_EXCHANGE,
            symbols=["NIFTY"],
            intervals=["1minute"],
            start=date(2025, 3, 1),
            end=date(2025, 3, 31),
        )
        assert scan == QuarantineScan(flags=(), load_failed=False)

    def test_r4_ledger_unreadable_surfaces_load_failed_verbatim(self, tmp_path: Path) -> None:
        """A garbage persisted ledger reads ``load_failed=True`` — fail-CLOSED upstream."""
        path = ledger_path(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_bytes(b"garbage, not a parquet ledger")
        with capture_logs() as logs:
            scan = scan_ledger_quarantine(
                tmp_path,
                exchange=_EXCHANGE,
                symbols=["NIFTY"],
                intervals=["1minute"],
                start=date(2025, 3, 1),
                end=date(2025, 3, 31),
            )
        assert scan.load_failed is True
        assert scan.flags == ()
        assert any(e["event"] == "window_ledger.load_failed" for e in logs)

    def test_scan_never_writes(self, tmp_path: Path) -> None:
        """The ledger scan is read-only — no delta, no plan, no lock file."""
        _seed_ledger(tmp_path)
        before = _tree_snapshot(tmp_path)
        scan_ledger_quarantine(
            tmp_path,
            exchange=_EXCHANGE,
            symbols=["NIFTY", "BANKNIFTY"],
            intervals=["1minute", "1day"],
            start=date(2025, 1, 1),
            end=date(2025, 12, 31),
        )
        assert _tree_snapshot(tmp_path) == before
