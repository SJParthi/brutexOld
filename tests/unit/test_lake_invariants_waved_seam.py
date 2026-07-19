"""The Wave-D seam gate around ``scan_lake_invariants`` (DEC-RUST-LAKE-WAVED-001).

W-4's seam-unit twins of ``test_catalog_reconcile.TestWavedSeam`` — a NEW
stem-matched file so the three pre-existing ``lake_invariants`` suites stay
byte-UNMODIFIED (the W-4 brief's flag-OFF-green-unmodified law) while the
discovery coverage gate still maps this file onto ``lake_invariants.py``.
Wheel-free by design: the flag-ON arm poisons ``sys.modules`` so the
facade's import guard latches and the ORACLE (the verbatim
``_scan_lake_invariants_python``) serves; the real-wheel parity gate is
``tests/integration/test_waved_scan_differential.py``.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import dataclasses
import sys
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

import polars as pl
import pytest
from brutex.data.lake_invariants import _scan_lake_invariants_python, scan_lake_invariants
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter

if TYPE_CHECKING:
    from pathlib import Path

pytest.importorskip("polars")
pytest.importorskip("pyarrow")

_ = pl  # imported for the importorskip guard chain


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


def _comparable(report: object) -> dict[str, object]:
    """The report as a dict with the wall clock popped (byte-identity oracle)."""
    raw = dataclasses.asdict(report)  # type: ignore[call-overload]
    raw.pop("duration_s")
    return raw


class TestWavedSeam:
    def test_flag_off_never_calls_the_facade(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The never-routes bomb spy: flag OFF (the default) must not touch
        # the routing function at all.
        from brutex.data import lake_rust_waved as lrw

        monkeypatch.delenv("BRUTEX_LAKE_WAVED_RUST", raising=False)

        def _bomb(*_args: object, **_kwargs: object) -> object:
            raise AssertionError("scan_lake_via_rust must not run flag-OFF")

        monkeypatch.setattr(lrw, "scan_lake_via_rust", _bomb)
        _write_partition(tmp_path)
        report = scan_lake_invariants(tmp_path)
        assert report.files_scanned == 1 and report.findings == ()

    def test_flag_off_is_byte_identical_to_the_python_arm(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("BRUTEX_LAKE_WAVED_RUST", raising=False)
        _write_partition(tmp_path)
        assert _comparable(scan_lake_invariants(tmp_path)) == _comparable(
            _scan_lake_invariants_python(tmp_path)
        )

    def test_flag_on_without_crate_serves_the_oracle_identically(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Flag ON + an absent wheel: the facade latches crate_unavailable
        # and the whole scan serves the verbatim Python body.
        from brutex.data import lake_rust_waved as lrw

        _write_partition(tmp_path, n=4)
        baseline = _comparable(scan_lake_invariants(tmp_path))
        monkeypatch.setenv(lrw.LAKE_WAVED_RUST_ENV, "1")
        monkeypatch.setitem(sys.modules, "brutex_lake", None)  # ImportError → latch
        lrw.reset_lake_rust_waved_for_tests()
        try:
            routed = scan_lake_invariants(tmp_path)
        finally:
            lrw.reset_lake_rust_waved_for_tests()
        assert _comparable(routed) == baseline
        assert routed.files_scanned == 1 and routed.findings == ()

    def test_flag_on_routes_through_the_facade(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The positive twin of the bomb spy: flag ON dispatches to the
        # routing function with the verbatim-Python oracle threaded.
        from brutex.data import lake_rust_waved as lrw

        monkeypatch.setenv(lrw.LAKE_WAVED_RUST_ENV, "1")
        seen: list[dict[str, object]] = []

        def _capture(lake_root: object, **kwargs: object) -> object:
            seen.append({"lake_root": lake_root, **kwargs})
            oracle = kwargs["oracle"]
            return oracle()  # type: ignore[operator]

        monkeypatch.setattr(lrw, "scan_lake_via_rust", _capture)
        _write_partition(tmp_path)
        report = scan_lake_invariants(tmp_path, months=[(2024, 6)], include_options=False)
        assert report.files_scanned == 1
        (call,) = seen
        assert call["lake_root"] == tmp_path
        assert call["months"] == [(2024, 6)] and call["include_options"] is False
