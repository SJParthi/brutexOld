# pyright: reportPrivateUsage=none
"""End-to-end CLI Greeks enrichment over the REAL driver (DEC-CLI-GREEKS-ENRICH-001).

The design §3.4 suite: unlike the hermetic unit seams
(``tests/unit/test_cli_greeks_enrich.py`` stubs ``brutex.data.greeks_enrich``
in ``sys.modules``), every test here drives the REAL
``enrich_greeks_for_targets`` driver over REAL 1-minute bars written with the
production ``ParquetBarSink``/``ParquetWriter`` (the
``test_pull_historical.py`` live-store recipe), so the CLI seam is proven to
actually SOLVE + STORE Greeks:

* ``cmd_fetch`` (live-wiring fakes for creds/instruments/adapter/ingest — no
  network) enriches a just-covered index option end-to-end: the CE partition
  gains non-null ``iv``/``spot_at_bar``/``greeks_provenance_id`` while the
  sibling FUT partition stays raw 7-column (never a bogus Greek for a future —
  filtered at DISCOVERY on the CLI path, so ``futures_skipped`` honestly
  reads 0: the documented dashboard asymmetry);
* a second identical fetch is ``already_current`` with ``bars_written == 0``
  and the CE partition BYTE-identical (Golden Rule #7 idempotence);
* one corrupt contract partition is quarantined INSIDE the driver
  (``errored == 1``) while the good contract still enriches and the pull still
  exits ``EXIT_OK`` (a fenced/quarantined enrichment never fails the fetch);
* ``brutex enrich-greeks`` bulk-enriches a contract whose window NO fetch ever
  covered — the #1188 expired-contract payoff — and exits ``EXIT_OK`` when
  fully clean.

``BRUTEX_GREEKS_WORKERS=1`` pins the serial driver path (the documented
kill-switch) so the suite is deterministic under CI's shared runners.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from structlog.testing import capture_logs

pytest.importorskip("polars")
pytest.importorskip("pyarrow")
pytest.importorskip("duckdb")
pytest.importorskip("trading_core", reason="the REAL Greeks driver needs the wheel")

import polars as pl
from brutex import cli
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.sink import ParquetBarSink

# ── Seeded lake coordinates ──────────────────────────────────────────────

#: The enriched workhorse: one NIFTY Jan-2024 CE leg (expiry 2024-01-25).
_CE_JAN = "NSE-NIFTY-25Jan24-21000-CE"
#: Its sibling FUT — discovered NEVER (CE/PE-only CLI discovery), stays raw.
_FUT_JAN = "NSE-NIFTY-25Jan24-FUT"
#: A second CE whose month file is garbage bytes — the quarantine fixture.
_CE_BAD = "NSE-NIFTY-25Jan24-21100-CE"
#: A CE in a window no fetch ever covered — the bulk-subcommand fixture.
_CE_FEB = "NSE-NIFTY-29Feb24-21000-CE"

#: 2024-01-15 09:15 IST — the shared spot/option bar instant (Jan fixtures).
_TS_JAN = datetime(2024, 1, 15, 3, 45, tzinfo=UTC)
#: 2024-02-15 09:15 IST — the out-of-fetch-window instant (Feb fixture).
_TS_FEB = datetime(2024, 2, 15, 3, 45, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the enrichment env: default-ON gate, fixed 6.5% rate, serial driver."""
    monkeypatch.delenv("BRUTEX_CLI_GREEKS_ENRICH", raising=False)
    monkeypatch.delenv("BRUTEX_RISK_FREE_CSV", raising=False)
    monkeypatch.setenv("BRUTEX_GREEKS_WORKERS", "1")


@dataclass(frozen=True)
class _FakeIngestReport:
    """Duck-typed ``GrowwIngestReport`` surface the fetch summary reads."""

    windows: int = 1
    bars_appended: int = 375
    failed_pairs: int = 0
    transient_failures: int = 0
    still_failed_windows: int = 0
    auth_aborted: bool = False
    pairs: dict[tuple[str, str], Any] = field(default_factory=dict)


@dataclass(frozen=True)
class _FakeIngestResult:
    """Duck-typed ``SpotIngestWithRetry`` (clean report, no retry residual)."""

    report: _FakeIngestReport = field(default_factory=_FakeIngestReport)
    retry: Any = None

    @property
    def still_failed_windows(self) -> int:
        """Mirror the real wrapper: no retry ran, the report residual wins."""
        return self.report.still_failed_windows


def _wire_live_fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fake the live wiring (creds / instruments / adapter / ingest) — no network.

    The ``test_cli_retry_storm.py`` idiom: the vendor engine is a clean no-op
    (the bars under test are pre-seeded in the lake), so ``cmd_fetch`` reaches
    the post-drain Greeks seam with the REAL driver as the only heavy work.
    """
    import importlib

    from brutex.universe import BRUTE_FORCE_INDICES

    nifty = cli._synthetic_index_instrument(BRUTE_FORCE_INDICES[0])
    monkeypatch.setattr(cli, "_resolve_brute_force_instruments", lambda lake: [nifty])
    credentials = importlib.import_module("brutex.config.credentials")
    monkeypatch.setattr(credentials, "groww_credentials_error", lambda: None)
    monkeypatch.setattr(
        cli, "_build_live_groww_spot", lambda lake, catalog=None: (object(), object())
    )
    run_ingest = importlib.import_module("brutex.adapters.groww.run_ingest")
    monkeypatch.setattr(
        run_ingest,
        "run_groww_spot_ingest_with_retry",
        lambda adapter, sink, **kwargs: _FakeIngestResult(),
    )


def _bar(ts: datetime, close: str, *, open_interest: int | None = None) -> CandleBar:
    """One real 1-minute bar around *close* (2-dp tick-grid prices)."""
    price = Decimal(close)
    return CandleBar(
        timestamp=ts,
        open=price,
        high=price + Decimal("1"),
        low=price - Decimal("1"),
        close=price,
        volume=10,
        open_interest=open_interest,
    )


def _seed_lake(
    lake: Path,
    *,
    ts: datetime,
    contracts: dict[str, str],
    spot_close: str = "21000",
) -> None:
    """Seed NIFTY spot + the given ``{symbol: close}`` F&O legs via the REAL sink."""
    sink = ParquetBarSink(writer=ParquetWriter(lake_root=lake))
    sink.append(
        exchange="NSE",
        segment="INDEX",
        symbol="NIFTY",
        interval="1minute",
        bars=[_bar(ts, spot_close)],
    )
    for symbol, close in contracts.items():
        sink.append(
            exchange="NSE",
            segment="FNO",
            symbol=symbol,
            interval="1minute",
            bars=[_bar(ts, close, open_interest=100)],
        )


def _month_file(lake: Path, symbol: str, year: int, month: int) -> Path:
    """The contract's month partition path under the FNO tree."""
    return (
        lake / "bars" / "NSE" / "FNO" / symbol / "1minute" / f"{year:04d}" / f"{month:02d}.parquet"
    )


def _fetch_jan(lake: Path) -> int:
    """Run ``cmd_fetch`` over the seeded Jan-2024 bar's window."""
    return cli.cmd_fetch(
        lake,
        "2024-01-15",
        "2024-01-16",
        "1m",
        window_days=None,
        dry_run=False,
        inspect=False,
        rows_per_chunk=5,
    )


def _done_events(logs: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """The pass-level ``fno.greeks_enrich.done`` events."""
    return [r for r in logs if r["event"] == "fno.greeks_enrich.done"]


class TestCliFetchGreeksE2E:
    """``cmd_fetch`` → the REAL driver → Greeks stored on disk."""

    def test_cli_fetch_enriches_index_options_end_to_end(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A live fetch solves + stores the CE overlay; the FUT stays raw."""
        lake = tmp_path / "lake"
        _seed_lake(lake, ts=_TS_JAN, contracts={_CE_JAN: "105", _FUT_JAN: "21005"})
        _wire_live_fakes(monkeypatch)
        with capture_logs() as logs:
            rc = _fetch_jan(lake)
        assert rc == cli.EXIT_OK
        done = _done_events(logs)
        assert len(done) == 1
        assert done[0]["enriched"] == 1
        assert done[0]["errored"] == 0
        # The CLI discovery is CE/PE-only, so the driver never even SEES the
        # FUT — the documented `futures_skipped == 0` dashboard asymmetry.
        assert done[0]["futures_skipped"] == 0
        assert [r for r in logs if r["event"] == "fno.greeks_enrich.error"] == []
        ce = pl.read_parquet(_month_file(lake, _CE_JAN, 2024, 1))
        assert "iv" in ce.columns
        assert ce["iv"].null_count() == 0
        assert ce["spot_at_bar"].to_list() == [21000.0]
        assert ce["greeks_provenance_id"].null_count() == 0
        fut = pl.read_parquet(_month_file(lake, _FUT_JAN, 2024, 1))
        assert fut.width == 7  # raw OHLCV+OI — never a bogus Greek for a future

    def test_second_fetch_is_already_current_no_write(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Golden Rule #7: the re-run is stamp-gated — zero writes, same bytes."""
        lake = tmp_path / "lake"
        _seed_lake(lake, ts=_TS_JAN, contracts={_CE_JAN: "105"})
        _wire_live_fakes(monkeypatch)
        with capture_logs() as first_logs:
            assert _fetch_jan(lake) == cli.EXIT_OK
        assert _done_events(first_logs)[0]["enriched"] == 1
        enriched_bytes = _month_file(lake, _CE_JAN, 2024, 1).read_bytes()
        with capture_logs() as second_logs:
            assert _fetch_jan(lake) == cli.EXIT_OK
        done = _done_events(second_logs)
        assert len(done) == 1
        assert done[0]["already_current"] == 1
        assert done[0]["enriched"] == 0
        assert done[0]["bars_written"] == 0
        assert _month_file(lake, _CE_JAN, 2024, 1).read_bytes() == enriched_bytes

    def test_corrupt_partition_quarantined_end_to_end(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """One garbage contract is driver-quarantined; the good one enriches."""
        lake = tmp_path / "lake"
        _seed_lake(lake, ts=_TS_JAN, contracts={_CE_JAN: "105"})
        bad = _month_file(lake, _CE_BAD, 2024, 1)
        bad.parent.mkdir(parents=True)
        bad.write_bytes(b"this is not a parquet file")
        _wire_live_fakes(monkeypatch)
        with capture_logs() as logs:
            rc = _fetch_jan(lake)
        assert rc == cli.EXIT_OK  # a quarantined target NEVER fails the fetch
        done = _done_events(logs)
        assert len(done) == 1
        assert done[0]["errored"] == 1
        assert done[0]["enriched"] == 1
        assert [r for r in logs if r["event"] == "fno.greeks_enrich.error"] == []
        ce = pl.read_parquet(_month_file(lake, _CE_JAN, 2024, 1))
        assert ce["iv"].null_count() == 0


class TestEnrichGreeksSubcommandE2E:
    """``brutex enrich-greeks`` — the #1188 bulk re-enrich, real driver."""

    def test_enrich_greeks_subcommand_bulk_reenriches_out_of_window_contract(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A contract NO fetch ever covered gains stamps via the subcommand."""
        lake = tmp_path / "lake"
        _seed_lake(lake, ts=_TS_FEB, contracts={_CE_FEB: "105"})
        raw = pl.read_parquet(_month_file(lake, _CE_FEB, 2024, 2))
        assert "iv" not in raw.columns  # nothing enriched it — no fetch ran
        with capture_logs() as logs:
            rc = cli.cmd_enrich_greeks(lake, underlying=None, start=None, end=None, dry_run=False)
        assert rc == cli.EXIT_OK
        done = _done_events(logs)
        assert len(done) == 1
        assert done[0]["enriched"] == 1
        assert done[0]["errored"] == 0
        # The clean summary carries the REAL target count (review round-4).
        assert "enrich-greeks done — contracts=1 errored_targets=0" in capsys.readouterr().out
        ce = pl.read_parquet(_month_file(lake, _CE_FEB, 2024, 2))
        assert ce["iv"].null_count() == 0
        assert ce["greeks_provenance_id"].null_count() == 0
        assert ce["spot_at_bar"].to_list() == [21000.0]

    def test_enrich_greeks_lowercase_underlying_is_loud_zero_match(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Round-4 regression, full real stack: ``--underlying nifty`` is LOUD.

        The case-mismatched token matches zero contracts through the REAL
        discovery (exact frozenset membership against the canonical-uppercase
        ``parse_fno_symbol`` underlying), nothing is enriched, and the run
        prints one stderr WARNING + ``contracts=0`` on the summary instead of
        the old silent ``errored_targets=0`` green (exit 0 — the pass
        genuinely completed; warn, not fail).
        """
        lake = tmp_path / "lake"
        _seed_lake(lake, ts=_TS_FEB, contracts={_CE_FEB: "105"})
        with capture_logs() as logs:
            rc = cli.cmd_enrich_greeks(
                lake, underlying=["nifty"], start=None, end=None, dry_run=False
            )
        assert rc == cli.EXIT_OK
        captured = capsys.readouterr()
        assert "WARNING: --underlying matched ZERO stored option contracts" in captured.err
        assert "(tokens: nifty)" in captured.err
        assert "exact and case-sensitive" in captured.err
        assert "enrich-greeks done — contracts=0 errored_targets=0" in captured.out
        done = _done_events(logs)
        assert len(done) == 1
        assert done[0]["enriched"] == 0
        raw = pl.read_parquet(_month_file(lake, _CE_FEB, 2024, 2))
        assert "iv" not in raw.columns  # untouched — the filter matched nothing
