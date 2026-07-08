"""Cross-process lake cache invalidation, end-to-end (DEC-HARDENING-LEFTOVERS-001).

The defect: the dashboard's freshness signal was the in-process
``SummaryCache._generation`` int, bumped only by ``mark_dirty()`` at dashboard
write sites — a CLI ``brutex fetch`` into the SAME lake (a separate process)
never bumped it, so every generation-keyed cache (``/db`` dataset discovery,
candle totals, cascades, movers, coverage) served pre-CLI-write data until a
dashboard restart.

The fix under test: the production ``SummaryCache`` carries a
``revision_probe`` — an O(1) stat token over the window-ledger Parquet that
every production pull (dashboard AND CLI, DEC-CLI-LEDGER-CHECKPOINT-001)
checkpoints — so an out-of-band write is picked up on the very next poll.

This test simulates the CLI: it writes lake bars via the PRODUCTION
``ParquetWriter`` + checkpoints the ledger via the PRODUCTION
``save_window_ledger`` with NO dashboard-side ``mark_dirty``, then asserts the
generation-keyed ``/api/v1/db/candles/datasets`` discovery serves the FRESH
dataset list. (Before the fix, the second poll served the cached pre-write
list — verified by the probe-less control test.)

Skipped when ``fastapi`` / ``duckdb`` / ``polars`` are absent — matches the
``test_dashboard_coverage.py`` convention.
"""

# The probe-builder tests exercise the private `_lake_revision_probe_or_none`.
# pyright: reportPrivateUsage=none
from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("duckdb")
pytest.importorskip("polars")

from brutex.dashboard.app import DashboardState, _lake_revision_probe_or_none, build_app
from brutex.dashboard.lake_revision import ABSENT_TOKEN
from brutex.dashboard.summary_cache import SummaryCache
from brutex.data.fetch_coverage import WindowKey
from brutex.data.instrument_store import InstrumentStore
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.window_ledger import WindowLedger
from brutex.data.window_ledger_store import save_window_ledger
from fastapi.testclient import TestClient

_DAY = date(2024, 6, 13)


@pytest.fixture
def store(tmp_path: Path):
    """An InstrumentStore at ``<tmp>/instruments.duckdb`` → lake root = tmp."""
    s = InstrumentStore.open(tmp_path / "instruments.duckdb")
    yield s
    s.close()


def _bar(minute: int) -> CandleBar:
    """One flat 1-minute bar at 09:{minute} IST (= 03:{minute+45-60}Z-ish) on _DAY."""
    total = 9 * 60 + minute - (5 * 60 + 30)
    return CandleBar(
        timestamp=datetime(_DAY.year, _DAY.month, _DAY.day, total // 60, total % 60, tzinfo=UTC),
        open=Decimal("100.00"),
        high=Decimal("100.00"),
        low=Decimal("100.00"),
        close=Decimal("100.00"),
        volume=10,
        open_interest=None,
    )


def _cli_style_write(lake_root: Path, symbol: str, ledger: WindowLedger) -> None:
    """Simulate a CLI ``brutex fetch``: PRODUCTION bar write + ledger checkpoint.

    No dashboard object is touched — in particular NO ``mark_dirty`` — exactly
    like a pull running in another process against the same ``BRUTEX_LAKE_ROOT``.
    """
    ParquetWriter(lake_root=lake_root).write(
        exchange="NSE", segment="INDEX", symbol=symbol, interval="1minute", bars=[_bar(15)]
    )
    ledger.record(
        WindowKey(market="groww", security_id=symbol, interval="1minute", start=_DAY, end=_DAY),
        raw_bars=1,
        error_type=None,
        bars_appended=1,
        now=datetime.now(UTC),
    )
    save_window_ledger(ledger, lake_root)


def _dataset_symbols(client: TestClient) -> set[str]:
    """The spot dataset symbols the /db discovery endpoint currently serves."""
    body = client.get("/api/v1/db/candles/datasets").json()
    return {d["symbol"] for d in body["datasets"]}


class TestCliWriteFreshness:
    """An out-of-band (CLI-process) lake write is served fresh on the next poll."""

    def test_out_of_band_ledger_checkpoint_refreshes_datasets(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        ledger = WindowLedger()
        _cli_style_write(tmp_path, "NIFTY", ledger)
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        assert _dataset_symbols(client) == {"NIFTY"}

        # The "CLI" writes a SECOND symbol + checkpoints the ledger — no
        # dashboard mark_dirty, no restart.
        _cli_style_write(tmp_path, "BANKNIFTY", ledger)
        assert _dataset_symbols(client) == {"NIFTY", "BANKNIFTY"}

    def test_without_probe_the_stale_cache_is_served_the_control(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        """The pre-fix behaviour, pinned: a probe-less cache stays stale."""
        ledger = WindowLedger()
        _cli_style_write(tmp_path, "NIFTY", ledger)
        # An INJECTED probe-less cache — the legacy/pre-fix configuration.
        client = TestClient(
            build_app(DashboardState(), instrument_store=store, summary_cache=SummaryCache())
        )
        assert _dataset_symbols(client) == {"NIFTY"}
        _cli_style_write(tmp_path, "BANKNIFTY", ledger)
        # STALE: the generation never moved, so the discovery cache still
        # serves the pre-write list (this is the defect the probe fixes).
        assert _dataset_symbols(client) == {"NIFTY"}


class TestProbeBuilder:
    """`_lake_revision_probe_or_none` — the build_app wiring seam."""

    def test_no_store_disables_the_probe(self) -> None:
        assert _lake_revision_probe_or_none(None) is None

    def test_pathless_store_disables_the_probe(self) -> None:
        class _PathlessStore:
            path = "not-a-Path"

        assert _lake_revision_probe_or_none(_PathlessStore()) is None

    def test_wired_store_probe_stats_the_ledger(self, store: InstrumentStore) -> None:
        probe = _lake_revision_probe_or_none(store)
        assert probe is not None
        # No ledger + no deltas + no sweeps tree + no backfill marker yet → the
        # stable all-absent composite (the ledger component folds the
        # deltas-dir stat since DEC-LEDGER-DELTA-FLUSH-001; the Greeks-backfill
        # marker component landed in DEC-GREEKS-BULK-REENRICH-001 fix round 2).
        absent = f"{ABSENT_TOKEN}+{ABSENT_TOKEN}|{ABSENT_TOKEN}|{ABSENT_TOKEN}"
        assert probe() == absent
        # A checkpoint moves the token.
        lake_root = Path(store.path).parent
        save_window_ledger(WindowLedger(), lake_root)
        token = probe()
        assert token is not None and token != absent


class TestCliSweepFreshness:
    """DEC-RESULTS-SURFACE-HONESTY-001: an out-of-band ``ResultsStore`` write
    (a CLI ``sweep-all`` in another process) is served fresh on the next poll.

    The #1069 probe statted ONLY the window ledger, so a CLI sweep never bumped
    the generation — the ``/results`` leaderboard stayed stale on its cached
    keys (and INCONSISTENT: a never-polled filter key computed fresh rows while
    the polled key served pre-write data) until a dashboard restart.
    """

    @staticmethod
    def _write_sweep_store(lake_root: Path, *, seed: int, sharpe: float, name: str) -> None:
        """A CLI-style per-tuple store write: production ``ResultsStore`` flush
        (tempfile + ``os.replace``), NO dashboard object touched."""
        from decimal import Decimal

        from brutex.engine.results_store import ResultRow, ResultsStore

        dest = lake_root / "sweeps" / "NIFTY" / "5m" / f"bull_k1_{name}.parquet"
        dest.parent.mkdir(parents=True, exist_ok=True)
        results = ResultsStore(path=dest)
        results.append(
            ResultRow(
                run_id=f"{seed:064x}",
                instrument="NIFTY",
                tf="5m",
                direction="bull",
                mode="A",
                mask_bits=0,
                net_pnl_paisa=100_00,
                gross_profit_paisa=200_00,
                gross_loss_paisa=100_00,
                max_drawdown_paisa=50_00,
                expectancy_paisa=10_00,
                total_trades=10,
                winning_trades=6,
                losing_trades=4,
                sharpe_ratio=sharpe,
                sortino_ratio=1.2,
                calmar_ratio=0.9,
                profit_factor=2.0,
                win_rate=0.6,
                max_drawdown_pct=0.05,
                avg_entry_price=Decimal("100.00"),
                avg_exit_price=Decimal("101.00"),
            )
        )
        results.flush()

    def test_out_of_band_sweep_store_refreshes_leaderboard_same_cache_key(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        """Write store → the NEXT poll of the SAME leaderboard cache key shows it."""
        self._write_sweep_store(tmp_path, seed=1, sharpe=1.0, name="a" * 16)
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        first = client.get("/api/v1/sweep/leaderboard").json()
        assert first["total_rows"] == 1

        # The "CLI sweep-all" lands a SECOND store into the SAME
        # <instrument>/<tf>/ directory — no ledger touch, no mark_dirty, no
        # restart. The sweeps-tree revision component must bump the generation.
        self._write_sweep_store(tmp_path, seed=2, sharpe=2.0, name="b" * 16)
        second = client.get("/api/v1/sweep/leaderboard").json()
        assert second["total_rows"] == 2
        assert [r["run_id"] for r in second["rows"]] == [f"{2:064x}", f"{1:064x}"]
