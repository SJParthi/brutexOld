"""R4/R14 futures-partition preflight pins (DEC-FUTURES-EXEC-TRANSLATION-001).

Disk-verified, refuse-whole, names the exact missing months, never auto-pulls.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

from brutex.futures.preflight import (
    FuturesPreflightReport,
    futures_partition_preflight,
    preflight_futures_months,
    probe_futures_partitions,
)


@dataclass
class _MapResolver:
    """Resolves from a fixed (underlying, on_date-month) → symbol map."""

    by_month: dict[tuple[str, int, int], str | None]

    def resolve(self, *, instrument: str, on_date: date) -> str | None:
        return self.by_month.get((instrument, on_date.year, on_date.month))


def _touch_partition(lake: Path, symbol: str, year: int, month: int) -> None:
    dest = (
        lake
        / "bars"
        / symbol.split("-", maxsplit=1)[0]
        / "FNO"
        / symbol
        / "1minute"
        / f"{year:04d}"
        / f"{month:02d}.parquet"
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"")


class TestPureFold:
    def test_all_present_ok(self) -> None:
        months = [("NIFTY", 2025, 1), ("NIFTY", 2025, 2)]
        report = futures_partition_preflight(dict.fromkeys(months, True), requested=months)
        assert report.ok
        assert report.missing_months == ()
        assert report.requested == tuple(sorted(months))

    def test_r4_preflight_refuses_missing_futures_month_named(self) -> None:
        # [R4/R14] the refusal NAMES the exact missing month.
        months = [("NIFTY", 2025, 1), ("NIFTY", 2025, 2)]
        present = {("NIFTY", 2025, 1): True, ("NIFTY", 2025, 2): False}
        report = futures_partition_preflight(present, requested=months)
        assert not report.ok
        assert report.missing_months == (("NIFTY", 2025, 2),)
        assert "NIFTY 2025-02" in report.remediation()

    def test_unprobed_month_fails_closed(self) -> None:
        # A month ABSENT from the probe map is missing — never assumed present.
        report = futures_partition_preflight({}, requested=[("NIFTY", 2025, 1)])
        assert not report.ok

    def test_r4_preflight_remediation_names_months_never_auto_pulls(self) -> None:
        # [R4] the remediation names every month + the /universe pull remedy;
        # the fold performs NO side effect (it is a pure function over maps).
        report = futures_partition_preflight(
            {("BANKNIFTY", 2024, 12): False, ("NIFTY", 2025, 1): False},
            requested=[("NIFTY", 2025, 1), ("BANKNIFTY", 2024, 12)],
        )
        text = report.remediation()
        assert "BANKNIFTY 2024-12" in text
        assert "NIFTY 2025-01" in text
        assert "never auto-pulls" in text
        assert "/universe" in text

    def test_deterministic_order_and_dedup(self) -> None:
        report = futures_partition_preflight(
            {},
            requested=[("NIFTY", 2025, 2), ("BANKNIFTY", 2025, 1), ("NIFTY", 2025, 2)],
        )
        assert report.requested == (("BANKNIFTY", 2025, 1), ("NIFTY", 2025, 2))

    def test_report_is_frozen(self) -> None:
        report = FuturesPreflightReport(missing_months=(), requested=())
        assert report.ok


class TestDiskProbe:
    def test_present_month_passes(self, tmp_path: Path) -> None:
        symbol = "NSE-NIFTY-30Jan25-FUT"
        _touch_partition(tmp_path, symbol, 2025, 1)
        resolver = _MapResolver({("NIFTY", 2025, 1): symbol})
        present = probe_futures_partitions(
            tmp_path, resolver=resolver, trade_months=[("NIFTY", 2025, 1)]
        )
        assert present == {("NIFTY", 2025, 1): True}

    def test_r4_ledger_present_but_disk_absent_still_refuses(self, tmp_path: Path) -> None:
        # [R4/R14] the resolver (registry = the 'ledger side') RESOLVES a
        # contract, but its month file is NOT on disk — DISK beats ledger.
        resolver = _MapResolver({("NIFTY", 2025, 1): "NSE-NIFTY-30Jan25-FUT"})
        report = preflight_futures_months(
            tmp_path, resolver=resolver, trade_months=[("NIFTY", 2025, 1)]
        )
        assert not report.ok
        assert report.missing_months == (("NIFTY", 2025, 1),)

    def test_unresolvable_month_refuses(self, tmp_path: Path) -> None:
        # [R14] an empty registry month (no contract at all) refuses, naming it.
        report = preflight_futures_months(
            tmp_path, resolver=_MapResolver({}), trade_months=[("NIFTY", 2025, 1)]
        )
        assert not report.ok
        assert "NIFTY 2025-01" in report.remediation()

    def test_roll_seam_month_requires_both_contracts(self, tmp_path: Path) -> None:
        # A month whose first/last days resolve DIFFERENT contracts (the
        # post-expiry tail) requires BOTH partitions on disk.
        jan = "NSE-NIFTY-30Jan25-FUT"
        feb = "NSE-NIFTY-27Feb25-FUT"

        @dataclass
        class _SeamResolver:
            def resolve(self, *, instrument: str, on_date: date) -> str | None:
                return jan if on_date <= date(2025, 1, 30) else feb

        _touch_partition(tmp_path, jan, 2025, 1)
        report = preflight_futures_months(
            tmp_path, resolver=_SeamResolver(), trade_months=[("NIFTY", 2025, 1)]
        )
        assert not report.ok  # the Feb (post-expiry tail) partition is absent
        _touch_partition(tmp_path, feb, 2025, 1)
        report = preflight_futures_months(
            tmp_path, resolver=_SeamResolver(), trade_months=[("NIFTY", 2025, 1)]
        )
        assert report.ok

    def test_end_to_end_mixed_verdict(self, tmp_path: Path) -> None:
        symbol = "NSE-NIFTY-30Jan25-FUT"
        _touch_partition(tmp_path, symbol, 2025, 1)
        resolver = _MapResolver(
            {("NIFTY", 2025, 1): symbol, ("NIFTY", 2025, 2): "NSE-NIFTY-27Feb25-FUT"}
        )
        report = preflight_futures_months(
            tmp_path,
            resolver=resolver,
            trade_months=[("NIFTY", 2025, 1), ("NIFTY", 2025, 2)],
        )
        assert not report.ok
        assert report.missing_months == (("NIFTY", 2025, 2),)
