"""The `/movers` candidate cache's pull-time adaptive floor (DEC-LATENCY-HONESTY-BATCH-001).

``build_app``'s production ``_movers_cache`` was the lone BARE
:class:`~brutex.dashboard.movers_cache.MoversSnapshotCache` next to five
floored siblings — so during a pull, every window-tick write-generation bump
forced a full ``O(instruments × bars-read)`` candidate re-scan on EVERY
``/movers`` poll, back-to-back (the exact scan-storm class
DEC-SUMMARY-CACHE-ADAPTIVE-FLOOR-001 #920 fixed for the ``SummaryCache``).
The cache now opts into the SAME pull-time adaptive floor
(``pull_active_probe=_pull_live, adaptive_floor=True``) — a SANCTIONED change
to the DEC-DB-DISCOVERY-ADAPTIVE-FLOOR-001 movers-rows carve-out, recorded in
``movers_cache.py``'s module docstring and ledgered in MIGRATION.md.

Pins (each a mutant-killer for one arm of the wiring):

* the FIX — with a pull genuinely running, a generation bump within the floor
  window serves the cached candidate set (ONE compute, never back-to-back);
* the floor is PULL-SCOPED — no pull ⇒ a generation bump recomputes exactly
  as before (byte-identical no-pull behaviour);
* an INJECTED cache is used verbatim — no floor is smuggled onto it.

Skipped when ``fastapi`` / ``duckdb`` / ``polars`` / ``pyarrow`` are absent.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("duckdb")
pytest.importorskip("polars")
pytest.importorskip("pyarrow")

import brutex.dashboard.app as app_module
from brutex.dashboard.movers_cache import MoversSnapshotCache
from brutex.dashboard.summary_cache import SummaryCache
from brutex.data.instrument_store import InstrumentStore
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from fastapi.testclient import TestClient

_DAY = date(2024, 6, 13)


def _day_bar(*, close: str) -> CandleBar:
    """A flat 1-day bar on ``_DAY`` (09:15 IST == 03:45 UTC)."""
    c = Decimal(close)
    return CandleBar(
        timestamp=datetime(_DAY.year, _DAY.month, _DAY.day, 3, 45, tzinfo=UTC),
        open=c,
        high=c,
        low=c,
        close=c,
        volume=10,
        open_interest=None,
    )


def _idx(symbol: str) -> Instrument:
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=abs(hash(symbol)) % 100_000,
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
def store(tmp_path: Path) -> Iterator[InstrumentStore]:
    s = InstrumentStore.open(tmp_path / "instruments.duckdb")
    s.upsert_many(
        [_idx("NIFTY")], snapshot_date=date(2024, 1, 2), run_id="r1", csv_source_sha256="0" * 64
    )
    yield s
    s.close()


@pytest.fixture
def lake(tmp_path: Path) -> Path:
    ParquetWriter(lake_root=tmp_path).write(
        exchange="NSE",
        segment="INDEX",
        symbol="NIFTY",
        interval="1day",
        bars=[_day_bar(close="100")],
    )
    return tmp_path


_PARAMS = {"asset": "spot", "interval": "1day", "date": "2024-06-13", "top_n": 5}


def _counting_compute(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Spy on the route's REAL candidate scan — count compute invocations."""
    calls = {"n": 0}
    real = app_module.compute_spot_candidates

    def _counting(*args: object, **kwargs: object) -> object:
        calls["n"] += 1
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(app_module, "compute_spot_candidates", _counting)
    return calls


class TestMoversPullFloorWiring:
    def test_generation_bump_during_pull_serves_the_floored_candidate_set(
        self, store: InstrumentStore, lake: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """THE regression (fails before the fix, passes after).

        With a pull genuinely running (probe ``True``), a write-generation bump
        (the per-window-tick ``mark_dirty`` a real pull fires) must NOT force a
        back-to-back candidate re-scan on the very next poll: the entry is
        younger than the 2 s base floor, so it is served stale (logged) and the
        scan count stays at ONE. Before the fix the bare cache recomputed —
        the count read 2.
        """
        calls = _counting_compute(monkeypatch)
        summary_cache = SummaryCache(ttl_floor_s=0.0)
        client = TestClient(
            app_module.build_app(
                app_module.DashboardState(),
                instrument_store=store,
                summary_cache=summary_cache,
                pull_active_probe=lambda: True,
            )
        )
        assert client.get("/api/v1/movers", params=_PARAMS).status_code == 200
        assert calls["n"] == 1
        # A pull's window tick bumps the write generation …
        summary_cache.mark_dirty()
        # … and the immediate next poll (age << the 2 s base floor) is served
        # the floored candidate set — ONE scan total, never back-to-back.
        assert client.get("/api/v1/movers", params=_PARAMS).status_code == 200
        assert calls["n"] == 1

    def test_no_pull_generation_bump_recomputes_exactly_as_before(
        self, store: InstrumentStore, lake: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The floor is PULL-SCOPED: no pull running ⇒ a bump recomputes.

        Kills the "serve stale unconditionally" mutant — the no-pull path stays
        byte-identical to the pre-floor behaviour (a fresh pull's end always
        yields fresh movers rows on the next poll).
        """
        calls = _counting_compute(monkeypatch)
        summary_cache = SummaryCache(ttl_floor_s=0.0)
        client = TestClient(
            app_module.build_app(
                app_module.DashboardState(),
                instrument_store=store,
                summary_cache=summary_cache,
                pull_active_probe=lambda: False,
            )
        )
        assert client.get("/api/v1/movers", params=_PARAMS).status_code == 200
        assert calls["n"] == 1
        summary_cache.mark_dirty()
        assert client.get("/api/v1/movers", params=_PARAMS).status_code == 200
        assert calls["n"] == 2

    def test_injected_movers_cache_is_used_verbatim_no_floor_smuggled(
        self, store: InstrumentStore, lake: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An INJECTED cache keeps its own (bare) semantics even during a pull.

        The construction-site change floors only the DEFAULT instance; a test /
        legacy caller's injected cache must behave exactly as constructed —
        here a bare instance recomputes on the bump despite the active pull.
        """
        calls = _counting_compute(monkeypatch)
        summary_cache = SummaryCache(ttl_floor_s=0.0)
        client = TestClient(
            app_module.build_app(
                app_module.DashboardState(),
                instrument_store=store,
                summary_cache=summary_cache,
                movers_cache=MoversSnapshotCache(),
                pull_active_probe=lambda: True,
            )
        )
        assert client.get("/api/v1/movers", params=_PARAMS).status_code == 200
        assert calls["n"] == 1
        summary_cache.mark_dirty()
        assert client.get("/api/v1/movers", params=_PARAMS).status_code == 200
        assert calls["n"] == 2
