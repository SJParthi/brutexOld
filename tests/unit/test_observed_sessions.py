"""Tests for `brutex.calendars.observed_sessions` — the observed-sessions drift reconciler.

DEC-CALENDAR-OBSERVED-SESSIONS-001 (Session D PR-1). Pins every hard bound of the
remove-only overlay: the evidence-gated admission rule, the FAILED-ledger veto,
the <=3/month + <=8/year caps (over-cap -> LOUD refusal, no write), the labeled
sidecar (loader rejects unlabeled entries), the diff-based self-healing write
(refuter fold R2), the ``detected_at`` idempotence (R6), the mandatory
``market == GROWW_COVERAGE_MARKET`` SUCCESS filter (R9), and the loud abstain on
an unreadable window ledger (R12 fold).

The synthetic drift month used throughout is 2026-05 with a FICTIONAL observed
holiday 2026-05-12 — a test fixture, NOT a claim about any real NSE date
(Golden Rule #1: the real Jan-2026 vendor-observed holiday date stays UNVERIFIED
and is deliberately never used here).

The suite deliberately reaches module-private helpers (``_window_covers``,
``_read_sidecar_entries``, ``_ENGINE_INDEX_COUNT``) cross-module, so per repo
convention the file silences ``reportPrivateUsage`` at the top.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from pathlib import Path

import polars as pl
import pytest
from brutex.calendars import observed_sessions as obs
from brutex.calendars.nse import HolidayDataUnavailableError, is_trading_day
from brutex.calendars.observed_sessions import (
    MAX_FLAGS_PER_MONTH,
    MAX_OVERLAY_BYTES,
    MAX_OVERLAY_DAYS_PER_YEAR,
    OBSERVED_LABEL,
    OBSERVED_OVERLAY_VERSION,
    DayEvidence,
    ObservedHoliday,
    ObservedSessionsError,
    classify_month_drift,
    detect_session_drift,
    is_trading_day_checked_observed,
    is_trading_day_observed,
    load_observed_holidays,
    overlay_path,
)
from brutex.data.fetch_coverage import WindowKey
from brutex.data.lake_invariants import ENGINE_INDEX_COORDS
from brutex.data.window_ledger import WindowLedger
from brutex.data.window_ledger_store import ledger_path, save_window_ledger
from structlog.testing import capture_logs

# ── synthetic-month fixture constants (fictional; Golden Rule #1) ────────────
_MONTH = date(2026, 5, 1)
_DRIFT_DAY = date(2026, 5, 12)  # a Tuesday — a derived trading day
_NOW = datetime(2026, 7, 4, 10, 0, tzinfo=UTC)
_MAY_START = date(2026, 5, 1)
_JUNE_START = date(2026, 6, 1)
_INDEX_SYMBOLS = ("NIFTY", "BANKNIFTY", "SENSEX")


def _evidence(
    day: date,
    *,
    zero: bool = True,
    month_data: int = 3,
    success: int = 1,
    failed: int = 0,
) -> DayEvidence:
    return DayEvidence(
        day=day,
        zero_bars_all_indices=zero,
        indices_with_month_data=month_data,
        clean_success_windows=success,
        failed_windows_touching=failed,
    )


def _classify(evidence: list[DayEvidence], *, existing_year: int = 0) -> obs.DriftVerdict:
    return classify_month_drift(
        _MONTH,
        evidence,
        existing_year_overlay_days=existing_year,
        detected_at=_NOW.isoformat(),
        zero_bar_indices=("NSE/INDEX/NIFTY", "NSE/INDEX/BANKNIFTY", "BSE/INDEX/SENSEX"),
    )


def _write_month_bars(
    lake_root: Path, coord: tuple[str, str, str], days: list[date], *, empty: bool = False
) -> None:
    """Write one 1-minute month Parquet for *coord* carrying one bar per day."""
    exchange, segment, symbol = coord
    base = lake_root / "bars" / exchange / segment / symbol / "1minute"
    path = base / f"{_MONTH.year:04d}" / f"{_MONTH.month:02d}.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    if empty:
        frame = pl.DataFrame(schema={"timestamp": pl.Datetime(time_zone="UTC")})
    else:
        # 03:45 UTC == 09:15 IST — one session-open bar per covered day.
        stamps = [datetime(d.year, d.month, d.day, 3, 45, tzinfo=UTC) for d in days]
        frame = pl.DataFrame({"timestamp": stamps, "close": [100.0] * len(stamps)})
    frame.write_parquet(path)


def _seed_lake(
    lake_root: Path,
    *,
    missing_day: date | None = _DRIFT_DAY,
    skip_coords: tuple[tuple[str, str, str], ...] = (),
    success_windows: bool = True,
    failed_window: tuple[str, str, str, date, date] | None = None,
    success_market: str = "groww",
) -> None:
    """Build the synthetic May-2026 lake: 3 index month files + a window ledger."""
    from brutex.calendars.nse import trading_days_between

    days = trading_days_between(_MAY_START, date(2026, 5, 31))
    for coord in ENGINE_INDEX_COORDS:
        if coord in skip_coords:
            continue
        covered = [d for d in days if d != missing_day]
        _write_month_bars(lake_root, coord, covered)
    ledger = WindowLedger()
    now = _NOW
    if success_windows:
        for symbol in _INDEX_SYMBOLS:
            key = WindowKey(
                market=success_market,
                security_id=symbol,
                interval="1minute",
                start=_MAY_START,
                end=_JUNE_START,
            )
            ledger.record(key, raw_bars=100, error_type=None, bars_appended=100, now=now)
    if failed_window is not None:
        market, symbol, interval, start, end = failed_window
        key = WindowKey(market=market, security_id=symbol, interval=interval, start=start, end=end)
        ledger.record(key, raw_bars=0, error_type="GrowwTimeoutError", bars_appended=0, now=now)
    save_window_ledger(ledger, lake_root)


def _events(logs: Sequence[Mapping[str, object]], name: str) -> list[Mapping[str, object]]:
    return [e for e in logs if e["event"] == name]


# ─────────────────────────────────────────────────────────────────────────────
class TestClassifyMonthDrift:
    def test_flags_zero_bar_day_with_clean_success(self) -> None:
        verdict = _classify([_evidence(_DRIFT_DAY)])
        assert verdict.candidates == (_DRIFT_DAY,)
        assert len(verdict.flagged) == 1
        flag = verdict.flagged[0]
        assert isinstance(flag, ObservedHoliday)
        assert flag.day == _DRIFT_DAY
        assert flag.label == OBSERVED_LABEL
        assert not verdict.over_month_cap
        assert not verdict.over_year_cap

    def test_min_clean_success_is_one(self) -> None:
        # >=1 admits; 0 lands in no_evidence — pins the >=1 recalibration (the
        # sketched >=2 is UNSATISFIABLE: a settled SUCCESS window never re-fetches).
        admitted = _classify([_evidence(_DRIFT_DAY, success=1)])
        assert admitted.flagged[0].clean_success_windows == 1
        rejected = _classify([_evidence(_DRIFT_DAY, success=0)])
        assert rejected.flagged == ()
        assert rejected.no_evidence == (_DRIFT_DAY,)

    def test_failed_ledger_row_vetoes_flag(self) -> None:
        verdict = _classify([_evidence(_DRIFT_DAY, failed=1)])
        assert verdict.flagged == ()
        assert verdict.vetoed_failed == (_DRIFT_DAY,)
        assert verdict.no_evidence == ()

    def test_month_corroboration_required(self) -> None:
        verdict = _classify([_evidence(_DRIFT_DAY, month_data=2)])
        assert verdict.flagged == ()
        assert verdict.no_evidence == (_DRIFT_DAY,)

    def test_month_cap_refuses_whole_month(self) -> None:
        four = [_evidence(date(2026, 5, d)) for d in (4, 5, 6, 7)]
        verdict = _classify(four)
        assert verdict.over_month_cap is True
        assert verdict.flagged == ()  # the WHOLE month refused — never "first 3 kept"
        assert len(verdict.candidates) == 4

    def test_month_cap_boundary_exactly_three_allowed(self) -> None:
        three = [_evidence(date(2026, 5, d)) for d in (4, 5, 6)]
        verdict = _classify(three)
        assert verdict.over_month_cap is False
        assert len(verdict.flagged) == MAX_FLAGS_PER_MONTH

    def test_year_cap_refuses_loudly(self) -> None:
        verdict = _classify([_evidence(date(2026, 5, d)) for d in (4, 5)], existing_year=7)
        assert verdict.over_year_cap is True
        assert verdict.flagged == ()

    def test_year_cap_boundary_exactly_eight_allowed(self) -> None:
        verdict = _classify([_evidence(_DRIFT_DAY)], existing_year=MAX_OVERLAY_DAYS_PER_YEAR - 1)
        assert verdict.over_year_cap is False
        assert len(verdict.flagged) == 1

    def test_non_candidate_days_ignored(self) -> None:
        verdict = _classify([_evidence(date(2026, 5, 4), zero=False), _evidence(_DRIFT_DAY)])
        assert verdict.candidates == (_DRIFT_DAY,)
        assert date(2026, 5, 4) not in verdict.no_evidence
        assert date(2026, 5, 4) not in verdict.vetoed_failed
        assert all(f.day != date(2026, 5, 4) for f in verdict.flagged)


class TestOverlayPredicates:
    def test_is_trading_day_observed_remove_only(self) -> None:
        # A trading day in the overlay flips True -> False.
        assert is_trading_day(_DRIFT_DAY) is True
        assert is_trading_day_observed(_DRIFT_DAY, observed=frozenset({_DRIFT_DAY})) is False
        # A weekend / listed holiday in the overlay changes NOTHING (no ADD channel).
        sunday = date(2026, 5, 10)
        holiday = date(2026, 5, 1)  # Maharashtra Day
        for non_session in (sunday, holiday):
            assert is_trading_day_observed(non_session, observed=frozenset()) is False
            assert is_trading_day_observed(non_session, observed=frozenset({non_session})) is False
        # No overlay input can flip False -> True.
        assert is_trading_day_observed(_DRIFT_DAY, observed=frozenset()) is True

    def test_checked_variant_raises_out_of_coverage(self) -> None:
        with pytest.raises(HolidayDataUnavailableError):
            is_trading_day_checked_observed(date(2019, 5, 6), observed=frozenset())

    def test_checked_variant_subtracts(self) -> None:
        assert is_trading_day_checked_observed(_DRIFT_DAY, observed=frozenset()) is True
        assert (
            is_trading_day_checked_observed(_DRIFT_DAY, observed=frozenset({_DRIFT_DAY})) is False
        )


class TestLoadObservedHolidays:
    def _write(self, lake_root: Path, payload: object) -> Path:
        path = overlay_path(lake_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def _entry(self, day: str, label: str = OBSERVED_LABEL) -> dict[str, object]:
        return {"date": day, "detected_at": _NOW.isoformat(), "month": day[:7], "label": label}

    def test_load_missing_file_empty_no_warn(self, tmp_path: Path) -> None:
        with capture_logs() as logs:
            assert load_observed_holidays(tmp_path) == frozenset()
        assert _events(logs, obs.OVERLAY_LOAD_FAILED_EVENT) == []

    def test_load_valid_sidecar(self, tmp_path: Path) -> None:
        self._write(
            tmp_path,
            {"version": OBSERVED_OVERLAY_VERSION, "entries": [self._entry("2026-05-12")]},
        )
        assert load_observed_holidays(tmp_path) == frozenset({_DRIFT_DAY})

    def test_load_malformed_json_warns_empty(self, tmp_path: Path) -> None:
        path = overlay_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        with capture_logs() as logs:
            assert load_observed_holidays(tmp_path) == frozenset()
        assert len(_events(logs, obs.OVERLAY_LOAD_FAILED_EVENT)) == 1

    def test_load_wrong_version_warns_empty(self, tmp_path: Path) -> None:
        self._write(tmp_path, {"version": 99, "entries": [self._entry("2026-05-12")]})
        with capture_logs() as logs:
            assert load_observed_holidays(tmp_path) == frozenset()
        assert len(_events(logs, obs.OVERLAY_LOAD_FAILED_EVENT)) == 1

    def test_load_non_dict_payload_warns_empty(self, tmp_path: Path) -> None:
        self._write(tmp_path, ["not", "a", "dict"])
        with capture_logs() as logs:
            assert load_observed_holidays(tmp_path) == frozenset()
        assert len(_events(logs, obs.OVERLAY_LOAD_FAILED_EVENT)) == 1

    def test_load_entries_not_list_warns_empty(self, tmp_path: Path) -> None:
        self._write(tmp_path, {"version": OBSERVED_OVERLAY_VERSION, "entries": {"a": 1}})
        with capture_logs() as logs:
            assert load_observed_holidays(tmp_path) == frozenset()
        assert len(_events(logs, obs.OVERLAY_LOAD_FAILED_EVENT)) == 1

    def test_unlabeled_entry_rejected_whole_file(self, tmp_path: Path) -> None:
        # A hand-edited ADD-style entry (missing the observed-not-official label)
        # rejects the WHOLE sidecar — warn + empty (the labeled hard bound).
        self._write(
            tmp_path,
            {
                "version": OBSERVED_OVERLAY_VERSION,
                "entries": [self._entry("2026-05-12"), self._entry("2026-05-13", label="added")],
            },
        )
        with capture_logs() as logs:
            assert load_observed_holidays(tmp_path) == frozenset()
        assert len(_events(logs, obs.OVERLAY_LOAD_FAILED_EVENT)) == 1

    def test_invalid_date_entry_rejected(self, tmp_path: Path) -> None:
        self._write(
            tmp_path, {"version": OBSERVED_OVERLAY_VERSION, "entries": [self._entry("not-a-date")]}
        )
        with capture_logs() as logs:
            assert load_observed_holidays(tmp_path) == frozenset()
        assert len(_events(logs, obs.OVERLAY_LOAD_FAILED_EVENT)) == 1

    def test_compact_iso_date_entry_rejected(self, tmp_path: Path) -> None:
        # Canonical-form-only (hostile-review fold): Python 3.11+
        # ``date.fromisoformat`` also parses the compact "20260513", but the
        # reconciler's string-prefix month/year grouping cannot see that
        # spelling — a hand-edited compact twin would evade the 8/year cap and
        # never self-heal — so the loader now rejects any non-canonical form.
        self._write(
            tmp_path, {"version": OBSERVED_OVERLAY_VERSION, "entries": [self._entry("20260513")]}
        )
        with capture_logs() as logs:
            assert load_observed_holidays(tmp_path) == frozenset()
        assert len(_events(logs, obs.OVERLAY_LOAD_FAILED_EVENT)) == 1

    def test_non_dict_entry_rejected(self, tmp_path: Path) -> None:
        self._write(tmp_path, {"version": OBSERVED_OVERLAY_VERSION, "entries": ["2026-05-12"]})
        with capture_logs() as logs:
            assert load_observed_holidays(tmp_path) == frozenset()
        assert len(_events(logs, obs.OVERLAY_LOAD_FAILED_EVENT)) == 1

    def test_non_string_date_entry_rejected(self, tmp_path: Path) -> None:
        entry = self._entry("2026-05-12")
        entry["date"] = 20260512
        self._write(tmp_path, {"version": OBSERVED_OVERLAY_VERSION, "entries": [entry]})
        with capture_logs() as logs:
            assert load_observed_holidays(tmp_path) == frozenset()
        assert len(_events(logs, obs.OVERLAY_LOAD_FAILED_EVENT)) == 1

    def test_load_oserror_warns_empty(self, tmp_path: Path) -> None:
        # The sidecar path being a DIRECTORY raises IsADirectoryError (an OSError).
        overlay_path(tmp_path).mkdir(parents=True)
        with capture_logs() as logs:
            assert load_observed_holidays(tmp_path) == frozenset()
        assert len(_events(logs, obs.OVERLAY_LOAD_FAILED_EVENT)) == 1

    def test_load_nesting_bomb_warns_empty_never_raises(self, tmp_path: Path) -> None:
        # Review round 4 (fails-before: RecursionError ESCAPED all three catch
        # clauses — the #1558-r2 SweepCheckpointStore nesting-bomb class): a
        # '['*100k sidecar makes json.loads raise RecursionError; the loader
        # must degrade to the empty overlay with ONE WARN, never a raw crash of
        # the sweep launch / pull / dashboard page consuming it.
        path = overlay_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("[" * 100_000 + "]" * 100_000, encoding="utf-8")
        with capture_logs() as logs:
            assert load_observed_holidays(tmp_path) == frozenset()
        assert len(_events(logs, obs.OVERLAY_LOAD_FAILED_EVENT)) == 1

    def test_load_invalid_utf8_warns_empty_never_raises(self, tmp_path: Path) -> None:
        # Review round 4 (fails-before: UnicodeDecodeError — a ValueError, not
        # an OSError — ESCAPED the read arm's catch on the old read_text path).
        path = overlay_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\xff\xfe{")
        with capture_logs() as logs:
            assert load_observed_holidays(tmp_path) == frozenset()
        assert len(_events(logs, obs.OVERLAY_LOAD_FAILED_EVENT)) == 1

    def test_load_oversized_sidecar_warns_empty(self, tmp_path: Path) -> None:
        # Review round 4 (the #1614 bounded-read precedent): one byte over the
        # 1 MiB cap is rejected fail-closed — WARN + empty overlay — without
        # ever reading the plant whole (the read stops at cap + 1 bytes).
        path = overlay_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b" " * (MAX_OVERLAY_BYTES + 1))
        with capture_logs() as logs:
            assert load_observed_holidays(tmp_path) == frozenset()
        events = _events(logs, obs.OVERLAY_LOAD_FAILED_EVENT)
        assert len(events) == 1
        assert "oversized" in str(events[0].get("error"))

    def test_load_exactly_at_cap_parses(self, tmp_path: Path) -> None:
        # The boundary law: EXACTLY MAX_OVERLAY_BYTES parses normally (kills a
        # `>` -> `>=` cap mutant) — JSON tolerates trailing whitespace padding.
        path = overlay_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        body = json.dumps(
            {"version": OBSERVED_OVERLAY_VERSION, "entries": [self._entry("2026-05-12")]}
        ).encode("utf-8")
        path.write_bytes(body + b" " * (MAX_OVERLAY_BYTES - len(body)))
        assert path.stat().st_size == MAX_OVERLAY_BYTES
        with capture_logs() as logs:
            assert load_observed_holidays(tmp_path) == frozenset({_DRIFT_DAY})
        assert _events(logs, obs.OVERLAY_LOAD_FAILED_EVENT) == []


class TestWindowCovers:
    def test_end_exclusive(self) -> None:
        assert obs._window_covers("2026-05-12", "2026-05-13", _DRIFT_DAY) is True
        assert obs._window_covers("2026-05-01", "2026-05-12", _DRIFT_DAY) is False

    def test_malformed_dates_never_cover(self) -> None:
        assert obs._window_covers("garbage", "2026-05-13", _DRIFT_DAY) is False


class TestDetectSessionDrift:
    def test_end_to_end_flags_and_writes_overlay(self, tmp_path: Path) -> None:
        _seed_lake(tmp_path)
        with capture_logs() as logs:
            report = detect_session_drift(tmp_path, _MONTH, now=_NOW)
        assert report.wrote_overlay is True
        assert [f.day for f in report.verdict.flagged] == [_DRIFT_DAY]
        assert len(_events(logs, obs.DRIFT_DETECTED_EVENT)) == 1
        assert len(_events(logs, obs.OVERLAY_WRITTEN_EVENT)) == 1
        observed = load_observed_holidays(tmp_path)
        assert observed == frozenset({_DRIFT_DAY})
        assert is_trading_day_observed(_DRIFT_DAY, observed=observed) is False
        # Every OTHER derived trading day of the month is unchanged.
        from brutex.calendars.nse import trading_days_between

        for day in trading_days_between(_MAY_START, date(2026, 5, 31)):
            if day != _DRIFT_DAY:
                assert is_trading_day_observed(day, observed=observed) is True

    def test_clean_month_no_flags_no_sidecar(self, tmp_path: Path) -> None:
        _seed_lake(tmp_path, missing_day=None)
        report = detect_session_drift(tmp_path, _MONTH, now=_NOW)
        assert report.wrote_overlay is False
        assert report.verdict.flagged == ()
        assert not overlay_path(tmp_path).exists()

    def test_current_month_raises_valueerror(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="not fully past"):
            detect_session_drift(tmp_path, date(2026, 7, 1), now=_NOW)

    def test_future_month_raises_valueerror(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="not fully past"):
            detect_session_drift(tmp_path, date(2026, 9, 1), now=_NOW)

    def test_out_of_coverage_year_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="outside the published"):
            detect_session_drift(tmp_path, date(2019, 5, 1), now=_NOW)

    def test_month_normalized_to_day_one(self, tmp_path: Path) -> None:
        _seed_lake(tmp_path)
        report = detect_session_drift(tmp_path, date(2026, 5, 27), now=_NOW)
        assert report.verdict.month == _MONTH

    def test_ledger_success_cover_end_exclusive(self, tmp_path: Path) -> None:
        # A SUCCESS window ending exactly ON the drift day ([May-01, May-12)) does
        # NOT cover it — the day lands in no_evidence, never flagged.
        from brutex.calendars.nse import trading_days_between

        days = trading_days_between(_MAY_START, date(2026, 5, 31))
        for coord in ENGINE_INDEX_COORDS:
            _write_month_bars(tmp_path, coord, [d for d in days if d != _DRIFT_DAY])
        ledger = WindowLedger()
        for symbol in _INDEX_SYMBOLS:
            key = WindowKey(
                market="groww",
                security_id=symbol,
                interval="1minute",
                start=_MAY_START,
                end=_DRIFT_DAY,
            )
            ledger.record(key, raw_bars=50, error_type=None, bars_appended=50, now=_NOW)
        save_window_ledger(ledger, tmp_path)
        with capture_logs() as logs:
            report = detect_session_drift(tmp_path, _MONTH, now=_NOW)
        assert report.verdict.flagged == ()
        assert _DRIFT_DAY in report.verdict.no_evidence
        assert report.wrote_overlay is False
        assert len(_events(logs, obs.NO_EVIDENCE_EVENT)) >= 1

    def test_failed_row_any_symbol_vetoes_and_warns(self, tmp_path: Path) -> None:
        # An unrelated F&O contract's FAILED window touching the day vetoes the
        # flag (operator-verbatim breadth) — and the veto logs at WARNING (R7).
        _seed_lake(
            tmp_path,
            failed_window=(
                "groww-option",
                "NSE-NIFTY-26May2026-24000-CE",
                "1minute",
                date(2026, 5, 11),
                date(2026, 5, 14),
            ),
        )
        with capture_logs() as logs:
            report = detect_session_drift(tmp_path, _MONTH, now=_NOW)
        assert report.verdict.flagged == ()
        assert report.verdict.vetoed_failed == (_DRIFT_DAY,)
        assert report.wrote_overlay is False
        vetoes = _events(logs, obs.FLAG_VETOED_EVENT)
        assert len(vetoes) == 1
        assert vetoes[0]["log_level"] == "warning"

    def test_success_cover_requires_spot_market(self, tmp_path: Path) -> None:
        # R9: a SUCCESS row under a NON-spot market (e.g. a legacy/foreign
        # namespace) must NOT count as clean evidence — the day stays no_evidence.
        _seed_lake(tmp_path, success_market="groww-option")
        report = detect_session_drift(tmp_path, _MONTH, now=_NOW)
        assert report.verdict.flagged == ()
        assert _DRIFT_DAY in report.verdict.no_evidence

    def test_absent_index_month_file_blocks_flag(self, tmp_path: Path) -> None:
        # Corroboration: SENSEX has no month file at all -> data ABSENCE, not
        # holiday evidence -> no flag.
        _seed_lake(tmp_path, skip_coords=(("BSE", "INDEX", "SENSEX"),))
        report = detect_session_drift(tmp_path, _MONTH, now=_NOW)
        assert report.verdict.flagged == ()
        assert _DRIFT_DAY in report.verdict.no_evidence

    def test_empty_index_month_file_blocks_flag(self, tmp_path: Path) -> None:
        _seed_lake(tmp_path, skip_coords=(("BSE", "INDEX", "SENSEX"),))
        _write_month_bars(tmp_path, ("BSE", "INDEX", "SENSEX"), [], empty=True)
        report = detect_session_drift(tmp_path, _MONTH, now=_NOW)
        assert report.verdict.flagged == ()

    def test_redetect_same_month_idempotent_bytes(self, tmp_path: Path) -> None:
        _seed_lake(tmp_path)
        detect_session_drift(tmp_path, _MONTH, now=_NOW)
        first = overlay_path(tmp_path).read_bytes()
        # Re-run with a LATER clock: the (day, evidence) pair is unchanged, so the
        # stored detected_at is preserved and the sidecar is BYTE-identical — and
        # the diff-based write is skipped entirely (R2 + R6).
        later = datetime(2026, 7, 5, 9, 0, tzinfo=UTC)
        report = detect_session_drift(tmp_path, _MONTH, now=later)
        assert report.wrote_overlay is False
        assert overlay_path(tmp_path).read_bytes() == first

    def test_redetect_preserves_other_months(self, tmp_path: Path) -> None:
        _seed_lake(tmp_path)
        path = overlay_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        other = {
            "date": "2026-02-10",
            "detected_at": _NOW.isoformat(),
            "month": "2026-02",
            "label": OBSERVED_LABEL,
            "evidence": {
                "zero_bar_indices": [],
                "clean_success_windows": 1,
                "failed_windows_touching": 0,
            },
        }
        path.write_text(
            json.dumps({"version": OBSERVED_OVERLAY_VERSION, "entries": [other]}),
            encoding="utf-8",
        )
        detect_session_drift(tmp_path, _MONTH, now=_NOW)
        observed = load_observed_holidays(tmp_path)
        assert observed == frozenset({date(2026, 2, 10), _DRIFT_DAY})

    def test_redetect_after_bars_appear_removes_entry(self, tmp_path: Path) -> None:
        # R2 regression: the drift day is flagged, then a later re-pull lands REAL
        # bars for it -> re-detection REMOVES the stale entry (delete-the-month).
        _seed_lake(tmp_path)
        detect_session_drift(tmp_path, _MONTH, now=_NOW)
        assert load_observed_holidays(tmp_path) == frozenset({_DRIFT_DAY})
        _seed_lake(tmp_path, missing_day=None)  # bars now exist on the drift day
        report = detect_session_drift(tmp_path, _MONTH, now=_NOW)
        assert report.wrote_overlay is True
        assert report.verdict.flagged == ()
        assert load_observed_holidays(tmp_path) == frozenset()

    def test_month_cap_no_write(self, tmp_path: Path) -> None:
        from brutex.calendars.nse import trading_days_between

        days = trading_days_between(_MAY_START, date(2026, 5, 31))
        missing = set(days[:4])  # 4 zero-bar trading days -> over the 3/month cap
        for coord in ENGINE_INDEX_COORDS:
            _write_month_bars(tmp_path, coord, [d for d in days if d not in missing])
        ledger = WindowLedger()
        for symbol in _INDEX_SYMBOLS:
            key = WindowKey(
                market="groww",
                security_id=symbol,
                interval="1minute",
                start=_MAY_START,
                end=_JUNE_START,
            )
            ledger.record(key, raw_bars=100, error_type=None, bars_appended=100, now=_NOW)
        save_window_ledger(ledger, tmp_path)
        with capture_logs() as logs:
            report = detect_session_drift(tmp_path, _MONTH, now=_NOW)
        assert report.verdict.over_month_cap is True
        assert report.wrote_overlay is False
        assert not overlay_path(tmp_path).exists()
        errs = _events(logs, obs.MONTH_CAP_EVENT)
        assert len(errs) == 1
        assert errs[0]["log_level"] == "error"

    def test_year_cap_no_write(self, tmp_path: Path) -> None:
        _seed_lake(tmp_path)
        path = overlay_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        others = [
            {
                "date": f"2026-03-{10 + i:02d}",
                "detected_at": _NOW.isoformat(),
                "month": "2026-03",
                "label": OBSERVED_LABEL,
            }
            for i in range(MAX_OVERLAY_DAYS_PER_YEAR)
        ]
        path.write_text(
            json.dumps({"version": OBSERVED_OVERLAY_VERSION, "entries": others}),
            encoding="utf-8",
        )
        before = path.read_bytes()
        with capture_logs() as logs:
            report = detect_session_drift(tmp_path, _MONTH, now=_NOW)
        assert report.verdict.over_year_cap is True
        assert report.wrote_overlay is False
        assert path.read_bytes() == before  # LOUD refusal, sidecar untouched
        errs = _events(logs, obs.YEAR_CAP_EVENT)
        assert len(errs) == 1
        assert errs[0]["log_level"] == "error"

    def test_other_year_entries_do_not_count_toward_year_cap(self, tmp_path: Path) -> None:
        _seed_lake(tmp_path)
        path = overlay_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        others = [
            {
                "date": f"2025-03-{10 + i:02d}",
                "detected_at": _NOW.isoformat(),
                "month": "2025-03",
                "label": OBSERVED_LABEL,
            }
            for i in range(MAX_OVERLAY_DAYS_PER_YEAR)
        ]
        path.write_text(
            json.dumps({"version": OBSERVED_OVERLAY_VERSION, "entries": others}),
            encoding="utf-8",
        )
        report = detect_session_drift(tmp_path, _MONTH, now=_NOW)
        assert report.verdict.over_year_cap is False
        assert report.wrote_overlay is True

    def test_overlay_write_failure_raises_domain_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_lake(tmp_path)
        from brutex.data import atomic_io

        def _boom(*_args: object, **_kwargs: object) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(atomic_io, "atomic_write_simple", _boom)
        from brutex.data.errors import LakeWriteHalt

        with capture_logs() as logs, pytest.raises(ObservedSessionsError) as excinfo:
            detect_session_drift(tmp_path, _MONTH, now=_NOW)
        assert not isinstance(excinfo.value, LakeWriteHalt)
        assert len(_events(logs, obs.OVERLAY_WRITE_FAILED_EVENT)) == 1

    def test_overlay_dir_collision_raises_domain_error(self, tmp_path: Path) -> None:
        # ``<lake>/calendar_overlays`` existing as a FILE makes mkdir raise an
        # OSError -> wrapped into ObservedSessionsError (never a LakeWriteHalt).
        _seed_lake(tmp_path)
        (tmp_path / obs.OBSERVED_OVERLAY_DIRNAME).write_text("in the way", encoding="utf-8")
        with pytest.raises(ObservedSessionsError):
            detect_session_drift(tmp_path, _MONTH, now=_NOW)

    def test_unreadable_ledger_aborts_loudly(self, tmp_path: Path) -> None:
        _seed_lake(tmp_path)
        ledger_path(tmp_path).write_bytes(b"not a parquet file")
        with capture_logs() as logs, pytest.raises(ObservedSessionsError, match="unreadable"):
            detect_session_drift(tmp_path, _MONTH, now=_NOW)
        assert len(_events(logs, obs.LEDGER_UNREADABLE_EVENT)) == 1
        assert not overlay_path(tmp_path).exists()

    def test_missing_ledger_is_no_evidence_not_error(self, tmp_path: Path) -> None:
        # A genuinely-fresh lake (no ledger file) is NOT an error — it is simply
        # zero evidence, so nothing can be flagged.
        from brutex.calendars.nse import trading_days_between

        days = trading_days_between(_MAY_START, date(2026, 5, 31))
        for coord in ENGINE_INDEX_COORDS:
            _write_month_bars(tmp_path, coord, [d for d in days if d != _DRIFT_DAY])
        report = detect_session_drift(tmp_path, _MONTH, now=_NOW)
        assert report.verdict.flagged == ()
        assert _DRIFT_DAY in report.verdict.no_evidence


class TestAnchorsRecoverEndToEnd:
    """The drifted-month e2e regression: overlay flags the day -> anchors recover."""

    def test_prev_day_anchor_recovers_silently_with_overlay(self, tmp_path: Path) -> None:
        from brutex.indicators.prev_day_ohlc import DailyCandle, lookup_prev_day_ohlc

        _seed_lake(tmp_path)
        detect_session_drift(tmp_path, _MONTH, now=_NOW)
        observed = load_observed_holidays(tmp_path)
        assert observed == frozenset({_DRIFT_DAY})
        daily = {
            date(2026, 5, 11): DailyCandle(
                day=date(2026, 5, 11),
                open_paisa=100_00,
                high_paisa=110_00,
                low_paisa=90_00,
                close_paisa=105_00,
            )
        }
        # WITHOUT the overlay: the calendar prior session (the drift day) has no
        # candle -> the walk-back returns a STALE anchor with a WARNING.
        with capture_logs() as logs:
            bare = lookup_prev_day_ohlc(date(2026, 5, 13), daily)
        assert bare is not None
        assert bare.prev_day == date(2026, 5, 11)
        assert len(_events(logs, "prev_day_ohlc.stale_anchor")) == 1
        # WITH the overlay: the drift day is not a session -> the SAME anchor is
        # returned SILENTLY (the drift is reconciled, not noisy).
        with capture_logs() as logs:
            reconciled = lookup_prev_day_ohlc(date(2026, 5, 13), daily, observed=observed)
        assert reconciled is not None
        assert reconciled.prev_day == date(2026, 5, 11)
        assert _events(logs, "prev_day_ohlc.stale_anchor") == []


class TestReadSidecarEntries:
    """The tolerant merge-side reader (private; drives the diff-based write)."""

    def test_wrong_version_reads_empty(self, tmp_path: Path) -> None:
        path = overlay_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"version": 99, "entries": []}), encoding="utf-8")
        assert obs._read_sidecar_entries(path) == []

    def test_entries_not_list_reads_empty(self, tmp_path: Path) -> None:
        path = overlay_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"version": OBSERVED_OVERLAY_VERSION, "entries": {"a": 1}}),
            encoding="utf-8",
        )
        assert obs._read_sidecar_entries(path) == []

    def test_missing_file_reads_empty(self, tmp_path: Path) -> None:
        assert obs._read_sidecar_entries(overlay_path(tmp_path)) == []

    def test_invalid_entries_filtered(self, tmp_path: Path) -> None:
        path = overlay_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        good = {
            "date": "2026-05-12",
            "detected_at": _NOW.isoformat(),
            "month": "2026-05",
            "label": OBSERVED_LABEL,
        }
        path.write_text(
            json.dumps({"version": OBSERVED_OVERLAY_VERSION, "entries": [good, {"bad": 1}]}),
            encoding="utf-8",
        )
        assert obs._read_sidecar_entries(path) == [good]

    def test_nesting_bomb_reads_empty_never_raises(self, tmp_path: Path) -> None:
        # Review round 4: the merge-side reader rides the SAME widened belt —
        # a nesting-bomb sidecar reads as the tolerant empty entry list instead
        # of crashing the manual detect run with a raw RecursionError.
        path = overlay_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("[" * 100_000 + "]" * 100_000, encoding="utf-8")
        assert obs._read_sidecar_entries(path) == []

    def test_oversized_sidecar_reads_empty(self, tmp_path: Path) -> None:
        # Review round 4: the merge-side reader shares the bounded-read law.
        path = overlay_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b" " * (MAX_OVERLAY_BYTES + 1))
        assert obs._read_sidecar_entries(path) == []

    def test_compact_iso_entry_filtered(self, tmp_path: Path) -> None:
        # The merge-side reader applies the SAME canonical-form-only rule
        # (hostile-review fold): a compact-date twin like "20260513" can never
        # survive a reconcile as an invisible stored_other duplicate.
        path = overlay_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        good = {
            "date": "2026-05-13",
            "detected_at": _NOW.isoformat(),
            "month": "2026-05",
            "label": OBSERVED_LABEL,
        }
        compact_twin = dict(good, date="20260513")
        path.write_text(
            json.dumps({"version": OBSERVED_OVERLAY_VERSION, "entries": [good, compact_twin]}),
            encoding="utf-8",
        )
        assert obs._read_sidecar_entries(path) == [good]


class TestEngineIndexCountPin:
    def test_engine_index_count_matches_coords(self) -> None:
        # Rule 11 COMMON pin (hostile-review fold): the pure classifier's
        # module-level admission bar (`_ENGINE_INDEX_COUNT`, kept a literal
        # because `brutex.data.*` must stay function-local in the module) must
        # track the imported coord set. If ENGINE_INDEX_COORDS ever grows the
        # classifier fail-CLOSES (every candidate lands in no_evidence — the
        # safe direction); this pin turns that silent never-flag into a LOUD
        # failure forcing a conscious update.
        assert len(ENGINE_INDEX_COORDS) == obs._ENGINE_INDEX_COUNT
