"""``/api/v1/sweep/leaderboard`` preset param + store-fault degrade (package C).

DEC-SWEEP-RESULTS-DEPTH-001, gap-register ruling 10: the OPTIONAL ``?preset=``
query param — absent/blank byte-identical to the legacy payload, a registry
token ranks via :mod:`brutex.ranking.presets`, an unknown token is a clean 400
naming the valid values, and the preset rides the cache KEY so presets can
never cross-contaminate the default ordering within one revision. Plus C3: a
typed :class:`ResultsStoreError` escaping the per-store skip net degrades to
the honest JSON error payload (200 + explicit markers), never a 500 and never
the bare "no sweep run yet" blank.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="dashboard routes need the [ui] extra")
pytest.importorskip("duckdb", reason="the wired-store routes need duckdb")
pytest.importorskip("polars", reason="the leaderboard reads per-tuple Parquet stores")

from brutex.dashboard import app as app_module
from brutex.dashboard.app import DashboardState, build_app
from brutex.dashboard.summary_cache import SummaryCache
from brutex.dashboard.sweep_results_view import STORE_ERROR_PUBLIC_DETAIL
from brutex.data.instrument_store import InstrumentStore
from brutex.engine.results_store import ResultsStoreError
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from brutex.ranking.presets import PRESET_NAMES
from fastapi.testclient import TestClient

from tests.fakes.leaderboard_preset_fixtures import make_leaderboard_row, write_tuple_store


def _nifty() -> Instrument:
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=13,
        symbol_name="NIFTY",
        display_name="NIFTY",
        instrument=InstrumentType.IDX,
        segment=Segment.INDEX,
        series=None,
        isin=None,
        underlying_symbol=None,
        underlying_security_id=None,
        lot_size=0,
        tick_size=Decimal("0"),
        sm_freeze_qty=None,
        sm_expiry_date=None,
        strike_price=None,
        asm_gsm_flag=False,
    )


@pytest.fixture
def store(tmp_path: Path) -> Iterator[InstrumentStore]:
    instrument_store = InstrumentStore.open(tmp_path / "instruments.duckdb")
    instrument_store.upsert_many(
        [_nifty()], snapshot_date=date(2024, 1, 2), run_id="r1", csv_source_sha256="0" * 64
    )
    yield instrument_store
    instrument_store.close()


@pytest.fixture
def summary_cache() -> SummaryCache:
    return SummaryCache()


@pytest.fixture
def client(store: InstrumentStore, summary_cache: SummaryCache) -> Iterator[TestClient]:
    app = build_app(DashboardState(), instrument_store=store, summary_cache=summary_cache)
    with TestClient(app) as c:
        yield c


def _seed_dd_rows(lake_root: Path) -> None:
    """Rows whose Sharpe (default) and drawdown (preset) orders DIFFER."""
    write_tuple_store(
        lake_root,
        [
            make_leaderboard_row(1, sharpe_ratio=3.0, max_drawdown_pct=25.0),
            make_leaderboard_row(2, sharpe_ratio=1.0, max_drawdown_pct=2.0),
        ],
    )


class TestPresetParam:
    def test_preset_orders_and_echoes(self, client: TestClient, store: InstrumentStore) -> None:
        _seed_dd_rows(store.path.parent)
        resp = client.get("/api/v1/sweep/leaderboard?preset=min_dd_first")
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["preset"] == "min_dd_first"
        assert payload["preset_inert_axes"] == ["composite_score", "max_consecutive_losses"]
        assert [r["max_drawdown_pct"] for r in payload["rows"]] == [2.0, 25.0]

    def test_invalid_preset_is_a_clean_400_naming_valid_values(self, client: TestClient) -> None:
        resp = client.get("/api/v1/sweep/leaderboard?preset=bogus")
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert "unknown ranking preset" in detail
        for token in PRESET_NAMES:
            assert token in detail

    def test_blank_preset_byte_identical_to_absent(
        self, client: TestClient, store: InstrumentStore
    ) -> None:
        """The absent/blank pin: ?preset= produces the EXACT legacy payload."""
        _seed_dd_rows(store.path.parent)
        absent = client.get("/api/v1/sweep/leaderboard").json()
        blank = client.get("/api/v1/sweep/leaderboard?preset=").json()
        assert blank == absent
        assert "preset" not in absent
        assert "preset_inert_axes" not in absent
        # The legacy default ordering is untouched (Sharpe descending).
        assert [r["sharpe_ratio"] for r in absent["rows"]] == [3.0, 1.0]

    def test_preset_rides_the_cache_key(self, client: TestClient, store: InstrumentStore) -> None:
        """Default → preset → default within ONE revision: the preset payload
        gets its own cache slot, so the default ordering is never served a
        preset-ranked board (the key-regression pin)."""
        _seed_dd_rows(store.path.parent)
        default_first = client.get("/api/v1/sweep/leaderboard").json()
        preset = client.get("/api/v1/sweep/leaderboard?preset=min_dd_first").json()
        default_again = client.get("/api/v1/sweep/leaderboard").json()
        assert default_again == default_first
        assert [r["max_drawdown_pct"] for r in preset["rows"]] == [2.0, 25.0]
        assert [r["sharpe_ratio"] for r in default_again["rows"]] == [3.0, 1.0]

    def test_preset_on_empty_tree_is_honest_empty_board(self, client: TestClient) -> None:
        """A preset over an empty sweeps tree is the honest empty board, preset echoed."""
        payload = client.get("/api/v1/sweep/leaderboard?preset=calmar").json()
        assert payload["available"] is False
        assert payload["rows"] == []
        assert payload["preset"] == "calmar"


def test_preset_validated_even_without_a_lake_root() -> None:
    """The 400 fires BEFORE the no-lake-root degrade (eager validation)."""

    class _NoPathStore:
        path = None

        def list_index_equity_instruments(self) -> list[object]:
            return []

    with TestClient(build_app(DashboardState(), instrument_store=_NoPathStore())) as c:  # type: ignore[arg-type]
        assert c.get("/api/v1/sweep/leaderboard?preset=bogus").status_code == 400
        ok = c.get("/api/v1/sweep/leaderboard?preset=win_rate")
        assert ok.status_code == 200
        assert ok.json()["preset"] == "win_rate"


class TestStoreErrorDegrade:
    """C3: a typed ResultsStoreError from the scan → honest JSON, never a 500."""

    def test_store_error_degrades_to_honest_json(
        self, client: TestClient, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _raise(_lake_root: Path) -> object:
            raise ResultsStoreError("synthetic store fault outside the skip net")

        monkeypatch.setattr(app_module, "load_sweep_scan", _raise)
        resp = client.get("/api/v1/sweep/leaderboard")
        assert resp.status_code == 200  # never a 500
        payload = resp.json()
        assert payload["available"] is False
        # SANITIZED wire marker (CodeQL py/stack-trace-exposure): the raised
        # message's internals never reach the externally-served response.
        assert payload["error"] == STORE_ERROR_PUBLIC_DETAIL
        assert "synthetic store fault outside the skip net" not in resp.text
        assert payload["error_type"] == "results_store"
        assert payload["rows"] == []

    def test_recovers_after_the_fault_clears(
        self, client: TestClient, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nothing is cached on the error path — the next healthy poll serves
        the real board (the movers gated-payload self-recovery idiom)."""
        _seed_dd_rows(store.path.parent)
        with pytest.MonkeyPatch.context() as outage:

            def _raise(_lake_root: Path) -> object:
                raise ResultsStoreError("transient fault")

            outage.setattr(app_module, "load_sweep_scan", _raise)
            errored = client.get("/api/v1/sweep/leaderboard").json()
            assert errored["error"] == STORE_ERROR_PUBLIC_DETAIL
        healthy = client.get("/api/v1/sweep/leaderboard").json()
        assert "error" not in healthy
        assert healthy["available"] is True
        assert healthy["total_rows"] == 2
