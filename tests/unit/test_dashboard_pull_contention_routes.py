"""Route-level pins for DEC-DASHBOARD-PULL-CONTENTION-RESILIENCE-001 (G2 + G4).

G2 — the /db SPOT-cascade build prunes the F&O contract tree AT WALK DESCENT
(``exclude_segments={FNO}``): the walk-descent spy (the per-symbol-dir
liveness beat) proves the FNO contract dirs are never even iterated; the
served payload is BYTE-identical to the unpruned+filtered result; and the
manifest-rebuild division of duty holds (the FILTERED spot walk never rebuilds
the dataset manifest — the unfiltered datasets route keeps that duty).

G4 (widened) — the /universe pull-progress ``phases`` fold and the
/coverage/windows counts+page serve LAST-KNOWN instantly past the adaptive
floor mid-stamp-churn with a PARKED fold stub (the operator's live repro: the
windows page stuck at "loading…" + the /universe feed dropping ~154 s), on the
PRODUCTION cache shape (real daemon revalidator). FAILS-BEFORE: both routes
blocked for the whole fold.

Skipped cleanly without the ``[ui]`` extra (the sibling dashboard suites).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Generator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi", reason="dashboard routes need the [ui] extra")
pytest.importorskip("httpx", reason="TestClient needs httpx")

from brutex.dashboard import db_view
from brutex.dashboard.app import DashboardState, build_app
from brutex.data import window_ledger_store as wls
from brutex.data.fetch_coverage import WindowKey
from brutex.data.instrument_store import InstrumentStore
from brutex.data.window_ledger import WindowLedger
from fastapi.testclient import TestClient

_NOW = datetime(2026, 7, 11, 12, 0, tzinfo=UTC)

#: The parked-fold hold (upper bound; released early on pass).
_FOLD_HOLD_S: float = 6.0

#: Wall-clock budget for a serve-last-known response while the fold is parked.
#: A non-blocking serve answers in single-digit ms; a blocking one waits the
#: whole hold (4x over budget) — unambiguous separation.
_SERVE_BUDGET_S: float = 1.5

#: The #920 base floor (2 s) plus slack — real-time aging for the route-level
#: tests (the app-internal caches use the production monotonic clock).
_FLOOR_AGE_S: float = 2.2


def _touch_parquet(root: Path, *parts: str) -> None:
    """Create ``<root>/<parts...>/2024/01.parquet`` (the walk checks names only)."""
    d = root.joinpath(*parts, "2024")
    d.mkdir(parents=True, exist_ok=True)
    (d / "01.parquet").touch()


def _seed_lake(lake: Path, *, fno_contracts: int = 5) -> None:
    """Two spot symbol dirs + *fno_contracts* F&O contract dirs."""
    bars = lake / "bars"
    _touch_parquet(bars, "NSE", "INDEX", "NIFTY", "1minute")
    _touch_parquet(bars, "NSE", "CASH", "RELIANCE", "1minute")
    for i in range(fno_contracts):
        _touch_parquet(bars, "NSE", "FNO", f"NSE-NIFTY-30Jan25-2{i}000-CE", "1minute")


def _window_key(security_id: str) -> WindowKey:
    return WindowKey(
        market="groww",
        security_id=security_id,
        interval="1minute",
        start=date(2026, 1, 1),
        end=date(2026, 4, 1),
    )


def _seed_ledger(lake: Path, symbol: str) -> None:
    ledger = WindowLedger()
    ledger.record(_window_key(symbol), raw_bars=5, error_type=None, bars_appended=5, now=_NOW)
    wls.save_window_ledger(ledger, lake)


def _append_delta(lake: Path, symbol: str) -> None:
    live = wls.load_window_ledger(lake)
    live.record(_window_key(symbol), raw_bars=7, error_type=None, bars_appended=7, now=_NOW)
    assert wls.append_ledger_delta(live, lake) == 1


@pytest.fixture
def lake(tmp_path: Path) -> Path:
    root = tmp_path / "lake"
    root.mkdir()
    return root


@pytest.fixture
def store(lake: Path) -> Generator[InstrumentStore]:
    s = InstrumentStore.open(lake / "instruments.duckdb")
    yield s
    s.close()


@pytest.fixture
def client(store: InstrumentStore) -> Generator[TestClient]:
    app = build_app(DashboardState(), instrument_store=store)
    with TestClient(app) as c:
        yield c


class TestSpotCascadeFnoPrune:
    """G2 — the walk never descends the F&O contract tree (teeth + parity)."""

    def test_spot_cascade_walk_never_descends_fno(
        self, lake: Path, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """THE walk-descent spy: `_walk_candle_datasets` fires ONE liveness
        beat per walked SYMBOL dir, so the beat count IS the descent census.
        Pruned: 2 (the spot symbols). FAILS-BEFORE (unpruned): 7 — the walk
        iterated every F&O contract dir too (the 29.7-min class at the
        production 2.36M-file scale)."""
        _seed_lake(lake, fno_contracts=5)
        beats = {"n": 0}

        def _count_beat() -> None:
            beats["n"] += 1

        monkeypatch.setattr(db_view, "emit_progress_beat", _count_beat)
        resp = client.get("/api/v1/db/spot/symbols")
        assert resp.status_code == 200
        symbols = [s["symbol"] for s in resp.json()["symbols"]]
        assert symbols == ["NIFTY", "RELIANCE"]  # indices-first, then CASH alpha
        assert beats["n"] == 2, (
            f"the spot-cascade walk descended {beats['n']} symbol dirs — "
            "the F&O contract tree must be pruned at descent (exclude_segments)"
        )

    def test_pruned_walk_equals_unpruned_filtered(self, lake: Path) -> None:
        """Byte-equality: the pruned walk's spot rows == the unpruned walk's
        rows through `filter_spot_datasets` (the belt) — walk path (no
        manifest exists on this fresh lake)."""
        from brutex.dashboard.discovery_warm import filter_spot_datasets
        from brutex.data.fno_candle_lake import FNO_CANDLE_LAKE_SEGMENT

        _seed_lake(lake)
        unpruned = filter_spot_datasets(db_view.list_candle_datasets(lake))
        pruned = filter_spot_datasets(
            db_view.list_candle_datasets(
                lake, exclude_segments=frozenset({FNO_CANDLE_LAKE_SEGMENT})
            )
        )
        assert pruned == unpruned
        assert pruned  # non-vacuous: real spot rows exist

    def test_pruned_read_equals_filtered_on_the_manifest_path(self, lake: Path) -> None:
        """The same equality on the MANIFEST-served path (an unfiltered walk
        seeds the manifest; both reads then serve from it)."""
        from brutex.dashboard.discovery_warm import filter_spot_datasets
        from brutex.data.fno_candle_lake import FNO_CANDLE_LAKE_SEGMENT

        _seed_lake(lake)
        _seed_ledger(lake, "NIFTY")  # the manifest needs a ledger token to stamp
        db_view.list_candle_datasets(lake)  # unfiltered walk → rebuilds the manifest
        unfiltered = filter_spot_datasets(db_view.list_candle_datasets(lake))
        pruned = filter_spot_datasets(
            db_view.list_candle_datasets(
                lake, exclude_segments=frozenset({FNO_CANDLE_LAKE_SEGMENT})
            )
        )
        assert pruned == unfiltered

    def test_spot_route_never_rebuilds_manifest_datasets_route_does(
        self, lake: Path, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The manifest division-of-duty spy, BOTH directions: the FILTERED
        spot-cascade walk never rebuilds (a filtered walk would persist a
        silently-partial row set — the db_view guard law), while the
        UNFILTERED datasets route keeps the rebuild duty."""
        _seed_lake(lake)
        _seed_ledger(lake, "NIFTY")
        calls: list[str] = []
        real_rebuild = db_view.rebuild_manifest

        def _spy(lake_root: Path, *args: Any, **kwargs: Any) -> Any:
            calls.append("rebuild")
            return real_rebuild(lake_root, *args, **kwargs)

        monkeypatch.setattr(db_view, "rebuild_manifest", _spy)
        assert client.get("/api/v1/db/spot/symbols").status_code == 200
        assert calls == []  # the filtered spot walk NEVER rebuilds
        assert client.get("/api/v1/db/candles/datasets").status_code == 200
        assert calls == ["rebuild"]  # the unfiltered datasets route still does


class _ParkedFold:
    """A parked `_window_counts_total` stand-in: first NEW call blocks on the
    release event (the slow O(rows + delta-footers) fold under churn); every
    call is counted. Pass-through to the real fold on release."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0
        self._real = db_view._window_counts_total
        self._park = False

    def arm(self) -> None:
        self._park = True

    def __call__(self, conn: Any, source: Any) -> Any:
        self.calls += 1
        if self._park:
            self.entered.set()
            assert self.release.wait(timeout=_FOLD_HOLD_S)
        return self._real(conn, source)


class TestPullProgressServesLastKnown:
    """G4 — the pull-progress `phases` fold never blocks a poll past-floor."""

    def test_phases_serve_last_known_past_floor_with_parked_fold(
        self, lake: Path, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """FAILS-BEFORE: a poll landing past the 120 s-capped floor after a
        stamp move recomputed the fold INLINE — with the fold parked, the
        route blocked for the whole hold (the operator's ~154 s feed drops).
        Now: the PRIOR stamp's counts serve within the budget and the fold
        runs in ONE background revalidator (the production daemon runner)."""
        _seed_ledger(lake, "NIFTY")
        first = client.get("/api/v1/universe/pull-progress")
        assert first.status_code == 200
        prior_phases = first.json()["phases"]
        assert any(p["done"] == 1 for p in prior_phases)  # the seeded row landed
        time.sleep(_FLOOR_AGE_S)  # age the entry past the 2 s base floor
        _append_delta(lake, "BANKNIFTY")  # the stamp moves (the mid-pull shape)
        fold = _ParkedFold()
        fold.arm()
        monkeypatch.setattr(db_view, "_window_counts_total", fold)
        try:
            started = time.monotonic()
            resp = client.get("/api/v1/universe/pull-progress")
            elapsed = time.monotonic() - started
            assert resp.status_code == 200
            assert elapsed < _SERVE_BUDGET_S, (
                f"pull-progress blocked {elapsed:.2f}s behind the parked fold — "
                "the counts cache must serve last-known past the floor"
            )
            assert resp.json()["phases"] == prior_phases  # the last-known counts
            assert fold.entered.wait(timeout=5.0)  # the revalidator IS folding
        finally:
            fold.release.set()
        # Bounded settle: the background revalidator lands the fresh counts.
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            phases = client.get("/api/v1/universe/pull-progress").json()["phases"]
            if any(p["done"] == 2 for p in phases):
                break
            time.sleep(0.05)
        else:  # pragma: no cover — a genuine hang fails loudly here
            pytest.fail("the background revalidate never landed the fresh counts")


class TestCoverageWindowsServesLastKnown:
    """G4 widened — /coverage/windows rows+counts never block past-floor."""

    def test_windows_page_serves_last_known_past_floor_with_parked_fold(
        self, lake: Path, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The operator's live repro (the windows page stuck at 'loading…'
        mid-pull): past the floor, with the counts fold PARKED, the route
        serves the PRIOR stamp's counts AND rows within the budget."""
        _seed_ledger(lake, "NIFTY")
        first = client.get("/api/v1/coverage/windows")
        assert first.status_code == 200
        prior = first.json()
        assert prior["count"] == 1
        time.sleep(_FLOOR_AGE_S)
        _append_delta(lake, "BANKNIFTY")
        fold = _ParkedFold()
        fold.arm()
        monkeypatch.setattr(db_view, "_window_counts_total", fold)
        try:
            started = time.monotonic()
            resp = client.get("/api/v1/coverage/windows")
            elapsed = time.monotonic() - started
            assert resp.status_code == 200
            assert elapsed < _SERVE_BUDGET_S, (
                f"/coverage/windows blocked {elapsed:.2f}s behind the parked fold"
            )
            body = resp.json()
            assert body["counts"] == prior["counts"]  # last-known counts
            assert [r["symbol"] for r in body["windows"]] == [
                r["symbol"] for r in prior["windows"]
            ]  # last-known rows — the page family serves too
        finally:
            fold.release.set()
