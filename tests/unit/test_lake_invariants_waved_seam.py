"""The Wave-D seam gate around ``scan_lake_invariants`` (DEC-RUST-LAKE-WAVED-001).

W-6a ENDGAME: the Python scan core is DELETED and the retired
``$BRUTEX_LAKE_WAVED_RUST`` flag refuses for EVERY value (the
``$BRUTEX_FILL_MODE`` precedent), so this stem-matched seam suite pins the
dispatcher's endgame contract — refuse-on-set-flag, the unconditional
facade dispatch, and the loud typed refusal on an absent wheel. The
real-wheel behavior gate is ``tests/integration/test_waved_scan_differential.py``
(frozen expectations) + the #1916 poison-corpus harness.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import sys
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest

pytest.importorskip("polars")
pytest.importorskip("pyarrow")

from brutex.data.errors import (
    LakeRustWavedFlagRemovedError,
    LakeRustWavedUnavailableError,
)
from brutex.data.lake_invariants import scan_lake_invariants
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter

if TYPE_CHECKING:
    from pathlib import Path


def _write_partition(lake: Path, *, n: int = 3) -> None:
    """One clean NIFTY June-2024 partition through the production writer."""
    writer = ParquetWriter(lake_root=lake)
    bars = [
        CandleBar(
            timestamp=datetime(2024, 6, 3, 4, 1 + i, tzinfo=UTC),
            open=Decimal("100.00"),
            high=Decimal("101.00"),
            low=Decimal("99.00"),
            close=Decimal("100.50"),
            volume=0,
            open_interest=None,
        )
        for i in range(n)
    ]
    writer.write(exchange="NSE", segment="INDEX", symbol="NIFTY", interval="1minute", bars=bars)


class TestWavedSeam:
    @pytest.mark.parametrize("raw", ["0", "1", "junk"])
    def test_set_retired_flag_refuses_loudly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw: str
    ) -> None:
        # The retired-flag law: ANY set value — '0' and '1' included —
        # refuses before any lake work (a stale kill-switch config must
        # never silently bless or skip the ONE Rust path).
        monkeypatch.setenv("BRUTEX_LAKE_WAVED_RUST", raw)
        _write_partition(tmp_path)
        with pytest.raises(LakeRustWavedFlagRemovedError, match="no longer flag-gated"):
            scan_lake_invariants(tmp_path)

    def test_unset_flag_scans_through_the_crate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("BRUTEX_LAKE_WAVED_RUST", raising=False)
        _write_partition(tmp_path)
        report = scan_lake_invariants(tmp_path)
        assert report.files_scanned == 1 and report.findings == ()

    def test_absent_wheel_raises_the_typed_unavailable_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # W-6a: with the Python core deleted an absent/stale wheel is a LOUD
        # typed refusal — never a silent no-scan, never a fabricated report.
        from brutex.data import lake_rust_waved as lrw

        monkeypatch.delenv("BRUTEX_LAKE_WAVED_RUST", raising=False)
        _write_partition(tmp_path)
        monkeypatch.setitem(sys.modules, "brutex_lake", None)  # ImportError shape
        lrw.reset_lake_rust_waved_for_tests()
        try:
            with pytest.raises(LakeRustWavedUnavailableError, match="pip install"):
                scan_lake_invariants(tmp_path)
        finally:
            lrw.reset_lake_rust_waved_for_tests()

    def test_dispatch_crosses_every_argument(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The positive dispatch pin: scan_lake_invariants delegates to the
        # ONE facade routing function with every argument verbatim (and NO
        # oracle parameter — there is no Python arm left to thread).
        from brutex.data import lake_rust_waved as lrw

        monkeypatch.delenv("BRUTEX_LAKE_WAVED_RUST", raising=False)
        seen: list[dict[str, object]] = []

        def _capture(lake_root: object, **kwargs: object) -> object:
            seen.append({"lake_root": lake_root, **kwargs})
            from brutex.data.lake_invariants import LakeScanReport

            return LakeScanReport(
                findings=(),
                files_scanned=1,
                files_skipped=0,
                series_scanned=1,
                option_files_scanned=0,
                option_files_skipped=0,
                duration_s=0.0,
            )

        monkeypatch.setattr(lrw, "scan_lake_via_rust", _capture)
        _write_partition(tmp_path)
        report = scan_lake_invariants(tmp_path, months=[(2024, 6)], include_options=False)
        assert report.files_scanned == 1
        (call,) = seen
        assert call["lake_root"] == tmp_path
        assert call["months"] == [(2024, 6)] and call["include_options"] is False
        assert "oracle" not in call
