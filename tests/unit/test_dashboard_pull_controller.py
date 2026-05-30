"""Unit tests for :mod:`brutex.dashboard.pull_controller`.

Offline only — exercises the dry-run synthetic path (no creds / network)
and the injected-factory path. The live Groww builder
(:func:`_build_live_pull_fetcher`) is ``# pragma: no cover`` because it
needs real credentials + the ``growwapi`` SDK, both absent in CI.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest
from brutex.adapters.groww.chunks import ALL_GROWW_INTERVALS
from brutex.adapters.groww.fetch_loop import CandleFetcher
from brutex.adapters.groww.ingest import SyntheticCandleFetcher
from brutex.dashboard.pull_controller import (
    GROWW_HISTORY_FLOOR,
    PullCandlesReport,
    TimeframeResult,
    groww_creds_present,
    pull_candles,
    resolve_candle_range,
)
from brutex.data.watermark import ChunkKey


class VendorAPIBadRequestException(Exception):
    """Stub whose name matches the retry classifier's BAD_REQUEST entry.

    The fetch loop classifies caught exceptions by class ``__name__``
    (see ``brutex.orchestrator.retry.classify_exception``); this name maps
    to a terminal BAD_REQUEST → the chunk is quarantined as FAILED.
    """


class _FailingFetcher:
    """A :class:`CandleFetcher` that raises a terminal Groww-style error."""

    def fetch_chunk(self, key: ChunkKey) -> Sequence[object]:
        """Always raise — drives the fetch loop's terminal-failure branch."""
        raise VendorAPIBadRequestException(f"boom for {key.interval}")


class _EmptyFetcher:
    """A :class:`CandleFetcher` that returns no rows for every chunk.

    Drives the fetch loop's NO_DATA branch — the vendor returned an empty
    candle set, so nothing is persisted (completed stays 0) while no_data
    increments. Exercises the N1 ``total_no_data`` aggregation + the
    UI's "no data" (not green ✓) display path.
    """

    def fetch_chunk(self, key: ChunkKey) -> Sequence[object]:
        """Return an empty sequence — the NO_DATA outcome for that chunk."""
        return []


# ────────────────────────────────────────────────────────────────────────
# Dataclass surface
# ────────────────────────────────────────────────────────────────────────


class TestPullCandlesReport:
    def test_totals_and_to_dict(self) -> None:
        report = PullCandlesReport(
            symbol="NSE-NIFTY",
            year=2026,
            dry_run=True,
            timeframes=[
                TimeframeResult(interval="1m", completed=3, failed=0, no_data=1, skipped=2),
                TimeframeResult(interval="5m", completed=2, failed=1, no_data=4, skipped=5),
            ],
        )
        assert report.total_completed == 5
        assert report.total_failed == 1
        assert report.total_no_data == 5
        assert report.total_skipped == 7
        d = report.to_dict()
        assert d["symbol"] == "NSE-NIFTY"
        assert d["year"] == 2026
        assert d["dry_run"] is True
        assert d["total_completed"] == 5
        assert d["total_failed"] == 1
        assert d["total_no_data"] == 5
        assert d["total_skipped"] == 7
        assert d["timeframes"][1] == {
            "interval": "5m",
            "completed": 2,
            "failed": 1,
            "no_data": 4,
            "skipped": 5,
        }

    def test_empty_timeframes_default(self) -> None:
        report = PullCandlesReport(symbol="X", year=2026, dry_run=True)
        assert report.timeframes == []
        assert report.total_completed == 0
        assert report.total_failed == 0
        assert report.total_no_data == 0
        assert report.total_skipped == 0


# ────────────────────────────────────────────────────────────────────────
# groww_creds_present — testable creds/dry-run safety gate (H1)
# ────────────────────────────────────────────────────────────────────────


class TestGrowwCredsPresent:
    """``groww_creds_present`` now delegates to the credentials store.

    Each test that expects ``False`` with the env vars absent points
    ``BRUTEX_CREDENTIALS_FILE`` at a nonexistent path so a developer's real
    ``~/.brutex/credentials.json`` can never contaminate the result
    (DEC-CREDENTIALS-STORE-001 hermeticity).
    """

    def test_both_present_is_true(self) -> None:
        env = {
            "BRUTEX_GROWW_API_KEY": "k",
            "BRUTEX_GROWW_TOTP_SECRET": "s",
        }
        assert groww_creds_present(env) is True

    def test_both_absent_is_false(self, tmp_path: Path) -> None:
        env = {"BRUTEX_CREDENTIALS_FILE": str(tmp_path / "nonexistent.json")}
        assert groww_creds_present(env) is False

    def test_api_key_missing_is_false(self, tmp_path: Path) -> None:
        env = {
            "BRUTEX_GROWW_TOTP_SECRET": "s",
            "BRUTEX_CREDENTIALS_FILE": str(tmp_path / "nonexistent.json"),
        }
        assert groww_creds_present(env) is False

    def test_totp_missing_is_false(self, tmp_path: Path) -> None:
        env = {
            "BRUTEX_GROWW_API_KEY": "k",
            "BRUTEX_CREDENTIALS_FILE": str(tmp_path / "nonexistent.json"),
        }
        assert groww_creds_present(env) is False

    def test_blank_value_is_false(self, tmp_path: Path) -> None:
        env = {
            "BRUTEX_GROWW_API_KEY": "k",
            "BRUTEX_GROWW_TOTP_SECRET": "",
            "BRUTEX_CREDENTIALS_FILE": str(tmp_path / "nonexistent.json"),
        }
        assert groww_creds_present(env) is False

    def test_defaults_to_os_environ(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        # Default env=None reads os.environ. Clear both vars + point the
        # credentials file at a nonexistent path → hermetic False branch.
        monkeypatch.setenv("BRUTEX_CREDENTIALS_FILE", str(tmp_path / "nonexistent.json"))
        monkeypatch.delenv("BRUTEX_GROWW_API_KEY", raising=False)
        monkeypatch.delenv("BRUTEX_GROWW_TOTP_SECRET", raising=False)
        assert groww_creds_present() is False
        monkeypatch.setenv("BRUTEX_GROWW_API_KEY", "k")
        monkeypatch.setenv("BRUTEX_GROWW_TOTP_SECRET", "s")
        assert groww_creds_present() is True


# ────────────────────────────────────────────────────────────────────────
# pull_candles — dry-run (default factory exercises ``_build_dry_run_fetcher``)
# ────────────────────────────────────────────────────────────────────────


class TestPullCandlesDryRun:
    def test_default_year_is_current_utc_year(self, tmp_path: Path) -> None:
        report = pull_candles("NSE-NIFTY", lake_root=tmp_path, dry_run=True)
        assert report.year == datetime.now(UTC).year
        assert report.dry_run is True

    def test_covers_all_intervals(self, tmp_path: Path) -> None:
        report = pull_candles("NSE-NIFTY", lake_root=tmp_path, year=2026, dry_run=True)
        assert [tf.interval for tf in report.timeframes] == list(ALL_GROWW_INTERVALS)
        # Synthetic fetcher always succeeds → some completed, zero failed.
        assert report.total_failed == 0
        assert report.total_completed > 0

    def test_explicit_year(self, tmp_path: Path) -> None:
        report = pull_candles("NSE-BANKNIFTY", lake_root=tmp_path, year=2024, dry_run=True)
        assert report.year == 2024

    def test_blank_symbol_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            pull_candles("   ", lake_root=tmp_path, dry_run=True)

    def test_unprefixed_symbol_raises(self, tmp_path: Path) -> None:
        # L1: a bare trading_symbol (no NSE-/BSE- prefix) is rejected — it
        # would break symbol_to_exchange_segment. Guards B1 regressions.
        with pytest.raises(ValueError, match="exchange-prefixed"):
            pull_candles("NIFTY", lake_root=tmp_path, dry_run=True)

    def test_bse_prefixed_symbol_passes(self, tmp_path: Path) -> None:
        # SENSEX is BSE-; the BSE- prefix branch must pass the L1 guard.
        report = pull_candles("BSE-SENSEX", lake_root=tmp_path, year=2026, dry_run=True)
        assert report.total_completed > 0

    def test_no_data_and_skipped_propagate(self, tmp_path: Path) -> None:
        # N1: a fetcher that returns no rows surfaces no_data, and zero
        # completed → the UI shows "no data" rather than a green ✓.
        report = pull_candles(
            "NSE-NIFTY",
            lake_root=tmp_path,
            year=2026,
            dry_run=True,
            fetcher_factory=lambda _root: (_EmptyFetcher(), None),
        )
        assert report.total_completed == 0
        assert report.total_failed == 0
        assert report.total_no_data > 0
        d = report.to_dict()
        assert d["total_no_data"] == report.total_no_data
        assert d["total_skipped"] == report.total_skipped


# ────────────────────────────────────────────────────────────────────────
# pull_candles — injected factory (covers the non-default-factory branch)
# ────────────────────────────────────────────────────────────────────────


class TestPullCandlesInjectedFactory:
    def test_factory_overrides_dry_run_false(self, tmp_path: Path) -> None:
        seen: dict[str, Path] = {}

        def factory(lake_root: Path) -> tuple[CandleFetcher, Callable[[], None] | None]:
            seen["lake_root"] = lake_root
            return SyntheticCandleFetcher(rows_per_chunk=2), None

        report = pull_candles(
            "NSE-NIFTY",
            lake_root=tmp_path,
            year=2026,
            dry_run=False,
            fetcher_factory=factory,
        )
        # Factory got the lake root, dry_run flag is recorded as passed (False).
        assert seen["lake_root"] == tmp_path
        assert report.dry_run is False
        assert report.total_completed > 0

    def test_failing_fetcher_reports_failures(self, tmp_path: Path) -> None:
        report = pull_candles(
            "NSE-NIFTY",
            lake_root=tmp_path,
            year=2026,
            dry_run=True,
            fetcher_factory=lambda _root: (_FailingFetcher(), None),
        )
        assert report.total_completed == 0
        assert report.total_failed > 0


# ────────────────────────────────────────────────────────────────────────
# resolve_candle_range — pure [start, end) resolver (the from/to picker core)
# ────────────────────────────────────────────────────────────────────────

_NOW = datetime(2026, 5, 30, 12, 0, tzinfo=UTC)  # the context "today"


class TestResolveCandleRange:
    """The pure, O(1) window resolver behind the from/to-date candle picker."""

    def test_no_dates_no_year_uses_current_utc_year(self) -> None:
        # Default mode 3 — neither date nor year → current UTC calendar year.
        start, end = resolve_candle_range(from_date=None, to_date=None, year=None, now=_NOW)
        assert start == datetime(2026, 1, 1, tzinfo=UTC)
        assert end == datetime(2027, 1, 1, tzinfo=UTC)

    def test_no_dates_explicit_year(self) -> None:
        # Mode 2 — year given, no dates → that full calendar year.
        start, end = resolve_candle_range(from_date=None, to_date=None, year=2023, now=_NOW)
        assert start == datetime(2023, 1, 1, tzinfo=UTC)
        assert end == datetime(2024, 1, 1, tzinfo=UTC)

    def test_explicit_from_and_to_are_day_inclusive(self) -> None:
        # Mode 1 — both ends inclusive; end is the midnight after to_date.
        start, end = resolve_candle_range(
            from_date="2024-02-01", to_date="2024-02-15", year=None, now=_NOW
        )
        assert start == datetime(2024, 2, 1, tzinfo=UTC)
        assert end == datetime(2024, 2, 16, tzinfo=UTC)

    def test_from_only_runs_to_today(self) -> None:
        start, end = resolve_candle_range(from_date="2026-05-01", to_date=None, year=None, now=_NOW)
        assert start == datetime(2026, 5, 1, tzinfo=UTC)
        assert end == datetime(2026, 5, 31, tzinfo=UTC)  # today (05-30) + 1 day

    def test_to_only_starts_at_history_floor(self) -> None:
        start, end = resolve_candle_range(from_date=None, to_date="2020-03-10", year=None, now=_NOW)
        assert start == datetime(GROWW_HISTORY_FLOOR.year, 1, 1, tzinfo=UTC)
        assert end == datetime(2020, 3, 11, tzinfo=UTC)

    def test_from_before_floor_is_clamped_up(self) -> None:
        start, _ = resolve_candle_range(
            from_date="2018-06-01", to_date="2021-01-01", year=None, now=_NOW
        )
        assert start == datetime(2020, 1, 1, tzinfo=UTC)

    def test_to_after_today_is_clamped_down(self) -> None:
        _, end = resolve_candle_range(
            from_date="2026-01-01", to_date="2099-12-31", year=None, now=_NOW
        )
        assert end == datetime(2026, 5, 31, tzinfo=UTC)  # today + 1 day

    def test_year_is_ignored_when_a_date_is_given(self) -> None:
        # A date present → mode 1; the year argument must not leak in.
        start, end = resolve_candle_range(from_date="2024-07-01", to_date=None, year=2099, now=_NOW)
        assert start == datetime(2024, 7, 1, tzinfo=UTC)
        assert end == datetime(2026, 5, 31, tzinfo=UTC)

    def test_from_after_to_raises(self) -> None:
        with pytest.raises(ValueError, match="after to_date"):
            resolve_candle_range(from_date="2025-06-01", to_date="2025-05-01", year=None, now=_NOW)

    def test_future_from_date_raises(self) -> None:
        # from_date in the future + to=None (today) → from > to → refused.
        with pytest.raises(ValueError, match="after to_date"):
            resolve_candle_range(from_date="2027-01-01", to_date=None, year=None, now=_NOW)

    def test_malformed_from_date_raises(self) -> None:
        with pytest.raises(ValueError, match="from_date must be ISO"):
            resolve_candle_range(from_date="2024/02/01", to_date=None, year=None, now=_NOW)

    def test_malformed_to_date_raises(self) -> None:
        with pytest.raises(ValueError, match="to_date must be ISO"):
            resolve_candle_range(from_date=None, to_date="not-a-date", year=None, now=_NOW)

    def test_empty_string_dates_behave_as_omitted(self) -> None:
        # A bare ?from_date=&to_date= (or blank pickers) must behave exactly
        # like omitted bounds → fall through to year/default mode, NOT raise
        # a malformed-date error. Worst-case input hardening.
        start, end = resolve_candle_range(from_date="", to_date="", year=2023, now=_NOW)
        assert start == datetime(2023, 1, 1, tzinfo=UTC)
        assert end == datetime(2024, 1, 1, tzinfo=UTC)

    def test_whitespace_from_date_is_treated_as_omitted(self) -> None:
        # Whitespace-only from_date → floor; explicit mode still entered
        # because to_date is a real bound.
        start, end = resolve_candle_range(
            from_date="   ", to_date="2024-02-15", year=None, now=_NOW
        )
        assert start == datetime(GROWW_HISTORY_FLOOR.year, 1, 1, tzinfo=UTC)
        assert end == datetime(2024, 2, 16, tzinfo=UTC)


# ────────────────────────────────────────────────────────────────────────
# pull_candles — from/to date range threading (the picker, end to end)
# ────────────────────────────────────────────────────────────────────────


class TestPullCandlesDateRange:
    def test_year_mode_stamps_start_and_end_dates(self, tmp_path: Path) -> None:
        # Even in legacy year mode the report now carries the resolved window.
        report = pull_candles("NSE-NIFTY", lake_root=tmp_path, year=2024, dry_run=True)
        assert report.start_date == "2024-01-01"
        assert report.end_date == "2024-12-31"
        d = report.to_dict()
        assert d["start_date"] == "2024-01-01"
        assert d["end_date"] == "2024-12-31"

    def test_explicit_range_is_pulled_and_reported(self, tmp_path: Path) -> None:
        report = pull_candles(
            "NSE-NIFTY",
            lake_root=tmp_path,
            from_date="2024-03-01",
            to_date="2024-03-31",
            dry_run=True,
        )
        assert report.start_date == "2024-03-01"
        assert report.end_date == "2024-03-31"
        assert report.year == 2024
        assert report.total_completed > 0

    def test_inverted_range_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="after to_date"):
            pull_candles(
                "NSE-NIFTY",
                lake_root=tmp_path,
                from_date="2024-05-01",
                to_date="2024-04-01",
                dry_run=True,
            )
