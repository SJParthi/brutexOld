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
    PullSpotReport,
    TimeframeResult,
    pull_spot,
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


# ────────────────────────────────────────────────────────────────────────
# Dataclass surface
# ────────────────────────────────────────────────────────────────────────


class TestPullSpotReport:
    def test_totals_and_to_dict(self) -> None:
        report = PullSpotReport(
            symbol="NSE-NIFTY",
            year=2026,
            dry_run=True,
            timeframes=[
                TimeframeResult(interval="1m", completed=3, failed=0),
                TimeframeResult(interval="5m", completed=2, failed=1),
            ],
        )
        assert report.total_completed == 5
        assert report.total_failed == 1
        d = report.to_dict()
        assert d["symbol"] == "NSE-NIFTY"
        assert d["year"] == 2026
        assert d["dry_run"] is True
        assert d["total_completed"] == 5
        assert d["total_failed"] == 1
        assert d["timeframes"][1] == {"interval": "5m", "completed": 2, "failed": 1}

    def test_empty_timeframes_default(self) -> None:
        report = PullSpotReport(symbol="X", year=2026, dry_run=True)
        assert report.timeframes == []
        assert report.total_completed == 0
        assert report.total_failed == 0


# ────────────────────────────────────────────────────────────────────────
# pull_spot — dry-run (default factory exercises ``_build_dry_run_fetcher``)
# ────────────────────────────────────────────────────────────────────────


class TestPullSpotDryRun:
    def test_default_year_is_current_utc_year(self, tmp_path: Path) -> None:
        report = pull_spot("NSE-NIFTY", lake_root=tmp_path, dry_run=True)
        assert report.year == datetime.now(UTC).year
        assert report.dry_run is True

    def test_covers_all_intervals(self, tmp_path: Path) -> None:
        report = pull_spot("NSE-NIFTY", lake_root=tmp_path, year=2026, dry_run=True)
        assert [tf.interval for tf in report.timeframes] == list(ALL_GROWW_INTERVALS)
        # Synthetic fetcher always succeeds → some completed, zero failed.
        assert report.total_failed == 0
        assert report.total_completed > 0

    def test_explicit_year(self, tmp_path: Path) -> None:
        report = pull_spot("NSE-BANKNIFTY", lake_root=tmp_path, year=2024, dry_run=True)
        assert report.year == 2024

    def test_blank_symbol_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            pull_spot("   ", lake_root=tmp_path, dry_run=True)


# ────────────────────────────────────────────────────────────────────────
# pull_spot — injected factory (covers the non-default-factory branch)
# ────────────────────────────────────────────────────────────────────────


class TestPullSpotInjectedFactory:
    def test_factory_overrides_dry_run_false(self, tmp_path: Path) -> None:
        seen: dict[str, Path] = {}

        def factory(lake_root: Path) -> tuple[CandleFetcher, Callable[[], None] | None]:
            seen["lake_root"] = lake_root
            return SyntheticCandleFetcher(rows_per_chunk=2), None

        report = pull_spot(
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
        report = pull_spot(
            "NSE-NIFTY",
            lake_root=tmp_path,
            year=2026,
            dry_run=True,
            fetcher_factory=lambda _root: (_FailingFetcher(), None),
        )
        assert report.total_completed == 0
        assert report.total_failed > 0
