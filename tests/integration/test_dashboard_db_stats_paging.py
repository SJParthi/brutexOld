"""Route-level stats-manifest paging integration (DEC-LAKE-STATS-MANIFEST-001).

End-to-end through the REAL app (``build_app`` + ``TestClient`` — the
DEC-SURFACE-TRIAL-001 pattern): the production ``/api/v1/db/candles`` +
``/api/v1/db/options`` routes opt into the per-file stats manifest, so

* the FIRST view of a dataset runs the legacy scan and write-behind SEEDS the
  shard under ``<lake>/.manifest/stats/``;
* every LATER paging click is manifest-served and cell-identical to a
  kill-switched control request (``BRUTEX_LAKE_STATS_MANIFEST=off`` — the
  byte-identical legacy path);
* a heal-shaped out-of-band file change falls back to the legacy scan and
  RESEEDS — the wire payload never diverges.

Skipped when ``fastapi`` / ``duckdb`` are absent (the existing
``test_dashboard_coverage.py`` convention).

Whitebox spy on ``db_view._stats_read_context`` per the DEC-P0-017
convention (review round 4 — it pins that the fresh manifest arm's totals
traverse the manifest ON THE WIRE), so ``reportPrivateUsage`` is silenced.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("duckdb")
pytest.importorskip("polars")

from brutex.dashboard import db_view
from brutex.dashboard.app import DashboardState, build_app
from brutex.data import lake_stats_manifest as lsm
from brutex.data.instrument_store import InstrumentStore
from brutex.data.models import CandleBar, OptionBar
from brutex.data.option_parquet_writer import OptionParquetWriter, OptionSeriesCoordinate
from brutex.data.parquet_writer import ParquetWriter
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from fastapi.testclient import TestClient

_SPOT_KEY: dict[str, str] = {
    "exchange": "NSE",
    "segment": "CASH",
    "symbol": "NSE-NIFTY",
    "interval": "1minute",
}
_OPT_PK = "NSE_FNO|25|MONTH|1|ATM|CALL"
_OPT_SERIES = "NSE_FNO__25__MONTH__1__ATM__CALL"


def _idx(symbol: str, security_id: int) -> Instrument:
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=security_id,
        symbol_name=symbol,
        display_name=symbol,
        instrument=InstrumentType.IDX,
        segment=Segment.CASH,
        series=None,
        isin=None,
        underlying_symbol=None,
        underlying_security_id=None,
        lot_size=1,
        tick_size=Decimal("0.05"),
        sm_freeze_qty=None,
        sm_expiry_date=None,
        strike_price=None,
        asm_gsm_flag=False,
    )


@pytest.fixture
def store(tmp_path: Path) -> Any:
    s = InstrumentStore.open(tmp_path / "instruments.duckdb")
    s.upsert_many(
        [_idx("BANKNIFTY", 25)],
        snapshot_date=date(2026, 6, 1),
        run_id="r1",
        csv_source_sha256="0" * 64,
    )
    yield s
    s.close()


def _bar(ts: datetime, close: str) -> CandleBar:
    price = Decimal(close)
    return CandleBar(
        timestamp=ts,
        open=price,
        high=price,
        low=price,
        close=price,
        volume=7,
        open_interest=None,
    )


def _write_spot(lake_root: Path) -> None:
    """Two month partitions of distinct-minute bars via the production writer."""
    writer = ParquetWriter(lake_root=lake_root)
    for month in (1, 2):
        writer.write(
            bars=[
                _bar(datetime(2024, month, 5 + m % 3, 4, m, tzinfo=UTC), f"{100 + m}.00")
                for m in range(12)
            ],
            **_SPOT_KEY,
        )


def _obar(ts: datetime, side: str) -> OptionBar:
    return OptionBar(
        timestamp=ts,
        open=Decimal("100.00"),
        high=Decimal("110.10"),
        low=Decimal("90.00"),
        close=Decimal("105.00"),
        volume=250,
        open_interest=1200,
        side=side,
        strike=Decimal("48000.00"),
        spot=Decimal("47980.55"),
        iv=Decimal("0.1523"),
    )


def _write_options(lake_root: Path) -> None:
    seg, sec, flag, code, offset, _typ = _OPT_PK.split("|")
    coord = OptionSeriesCoordinate(
        exch_id="NSE",
        exchange_segment=seg,
        security_id=int(sec),
        underlying_symbol="BANKNIFTY",
        expiry_flag=flag,
        expiry_code=int(code),
        strike_offset=offset,
    )
    writer = OptionParquetWriter(lake_root=lake_root)
    for month in (3, 4):
        bars: list[OptionBar] = []
        for minute in range(5):
            ts = datetime(2024, month, 1, 4, minute, tzinfo=UTC)
            bars.extend((_obar(ts, "CE"), _obar(ts, "PE")))
        writer.write(partition_key=_OPT_PK, coordinate=coord, bars=bars)


_SPOT_URL = "/api/v1/db/candles"
_SPOT_PARAMS: dict[str, Any] = {
    "exchange": "NSE",
    "segment": "CASH",
    "symbol": "NSE-NIFTY",
    "interval": "1minute",
}


class TestRouteManifestPaging:
    def test_routes_seed_then_serve_cell_identical_pages(
        self, store: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Both /db pagers: manifest-served == kill-switched legacy control —
        with the manifest arm on a FRESH app so every wire TOTAL is COMPUTED
        through the manifest path, never an O(1) cache hit of a control-run
        legacy total.

        Review round 4: the prior single-app shape ran the kill-switched
        control clicks FIRST, populating the app's generation-stamped count
        cache under the SAME generation the manifest clicks then used — so
        every manifest-run TOTAL was a cache hit of the control run's LEGACY
        number and a WRONG manifest total could never have failed the
        route-level equality. Three apps now separate the arms:

        1. a CONTROL app (kill switch) computes the pure-legacy payloads;
        2. a SEED app (manifest on) pays each dataset's first-view fallback +
           write-behind seed;
        3. a FRESH manifest app (empty caches, shards already present) whose
           every total/page compute traverses the manifest — pinned by a spy
           proving every request built a SERVED stats context.
        """
        _write_spot(tmp_path)
        _write_options(tmp_path)
        clicks: list[dict[str, Any]] = [
            {"limit": 5, "offset": 0, "desc": True},
            {"limit": 5, "offset": 5, "desc": True},
            {"limit": 4, "offset": 10, "desc": False},
            {"limit": 4, "offset": 0, "desc": False, "start": "2024-02-01", "end": "2024-02-06"},
        ]

        def _run_clicks(client: TestClient) -> tuple[list[Any], list[Any]]:
            spot = [client.get(_SPOT_URL, params={**_SPOT_PARAMS, **c}).json() for c in clicks]
            opt = [
                client.get("/api/v1/db/options", params={"series": _OPT_SERIES, **c}).json()
                for c in clicks
            ]
            return spot, opt

        # (1) CONTROL app under the kill switch — pure legacy, no shard.
        monkeypatch.setenv(lsm.STATS_MANIFEST_ENV, "off")
        control_client = TestClient(build_app(DashboardState(), instrument_store=store))
        spot_control, opt_control = _run_clicks(control_client)
        assert not lsm.stats_shard_path(tmp_path, lsm.KIND_BARS, _SPOT_KEY).exists()

        # (2) SEED app: each pager's first view falls back + write-behind seeds.
        monkeypatch.delenv(lsm.STATS_MANIFEST_ENV)
        seed_client = TestClient(build_app(DashboardState(), instrument_store=store))
        seed_client.get(_SPOT_URL, params={**_SPOT_PARAMS, "limit": 5})
        seed_client.get("/api/v1/db/options", params={"series": _OPT_SERIES, "limit": 5})
        assert lsm.stats_shard_path(tmp_path, lsm.KIND_BARS, _SPOT_KEY).exists(), (
            "the route's first view must write-behind seed the spot shard"
        )
        assert lsm.load_valid_dataset_stats(tmp_path, kind=lsm.KIND_BARS, key=_SPOT_KEY)
        assert lsm.load_valid_dataset_stats(
            tmp_path, kind=lsm.KIND_OPTIONS, key={"series": _OPT_SERIES}
        )

        # (3) FRESH manifest app: empty count caches + valid shards ⇒ every
        # total is computed THIS run, through the manifest. The spy pins that
        # every request consulted AND served the validated shard (a wrong
        # manifest total would now fail the cell-identical asserts below).
        contexts: list[Any] = []
        real_ctx = db_view._stats_read_context

        def _spy_ctx(*args: Any, **kwargs: Any) -> Any:
            ctx = real_ctx(*args, **kwargs)
            contexts.append(ctx)
            return ctx

        monkeypatch.setattr(db_view, "_stats_read_context", _spy_ctx)
        manifest_client = TestClient(build_app(DashboardState(), instrument_store=store))
        spot_live, opt_live = _run_clicks(manifest_client)
        assert len(contexts) == 2 * len(clicks), "every route request must consult the manifest"
        assert all(ctx is not None and ctx.served for ctx in contexts), (
            "every manifest-arm request must SERVE the validated shard "
            "(totals + pages traversed the manifest on the wire)"
        )
        assert spot_live == spot_control, "manifest-served spot pages must be cell-identical"
        assert opt_live == opt_control, "manifest-served option pages must be cell-identical"
        assert spot_live[0]["total"] == 24
        assert opt_live[0]["total"] == 20

    def test_out_of_band_change_falls_back_then_reseeds_on_the_wire(
        self, store: Any, tmp_path: Path
    ) -> None:
        """A hand-deleted month between clicks: the next click serves the
        legacy truth (fallback) and reseeds — the wire payload tracks disk."""
        _write_spot(tmp_path)
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        first = client.get(_SPOT_URL, params={**_SPOT_PARAMS, "limit": 5}).json()
        assert first["total"] == 24
        dataset = lsm.dataset_dir(tmp_path, lsm.KIND_BARS, _SPOT_KEY)
        (dataset / "2024" / "02.parquet").unlink()
        # NOTE: the route memoises the total per write-generation; an
        # out-of-band delete moves no generation, so the SAME (bounds) key
        # would serve the cached total — the documented #867 envelope. A
        # DIFFERENT bounds key forces a fresh compute, which must fall back
        # to the live scan (the manifest fails file-set equality) and reseed.
        after = client.get(
            _SPOT_URL,
            params={**_SPOT_PARAMS, "limit": 5, "start": "2024-01-01", "end": "2024-12-31"},
        ).json()
        assert after["total"] == 12, "the fallback must serve the live on-disk truth"
        stats = lsm.load_valid_dataset_stats(tmp_path, kind=lsm.KIND_BARS, key=_SPOT_KEY)
        assert stats is not None and stats.row_count == 12, "the shard must be reseeded"
