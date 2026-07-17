"""Unit tests for the post-pipeline discovery-memo warm (DEC-DB-DISCOVERY-ADAPTIVE-FLOOR-001).

Proves :mod:`brutex.dashboard.discovery_warm`: ONE shared walk seeds all seven
discovery memos at the given generation (a subsequent read is a clean hit that
never re-walks), the seeded ``/db`` datasets payload is byte-identical to the
route-shaped builder, the warm FORCE-refreshes through an opted-in pull-active
floor (it runs while the job is still ``running``), a raising walk / store read
degrades to an honest ``0`` without ever raising (best-effort — a warm failure
must never fail the pull), and the shared candle walk runs exactly ONCE per
warm. Real tmp-dir lake trees (0-byte parquet files satisfy the existence-only
discovery walks); no FastAPI, no network.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from brutex.config.feed_scope import resolve_read_scope, scope_revision
from brutex.dashboard import discovery_warm as dw
from brutex.dashboard.discovery_warm import (
    DB_DATASETS_KEY,
    FNO_CASCADE_KEY,
    LATEST_MOVERS_DATE_KEY,
    LATEST_MOVERS_MINUTE_KEY_PREFIX,
    MOVERS_DATASETS_KEY,
    SPOT_CASCADE_KEY,
    build_db_datasets_payload,
    filter_spot_datasets,
    latest_movers_minute_key,
    preopen_datasets_key,
    warm_discovery_memos,
)
from brutex.dashboard.fno_cascade import FnoCascadeIndex
from brutex.dashboard.movers_cache import MoversSnapshotCache
from brutex.dashboard.preopen_report import REPORT_BASES, resolve_report_datasets
from brutex.dashboard.spot_cascade import SpotCascadeIndex
from structlog.testing import capture_logs


def _touch_partition(lake: Path, *parts: str) -> None:
    """Create ``<lake>/<parts...>`` with one 0-byte parquet (existence-only walks)."""
    d = lake.joinpath(*parts)
    d.mkdir(parents=True, exist_ok=True)
    (d / "01.parquet").touch()


@pytest.fixture
def lake(tmp_path: Path) -> Path:
    """A tiny lake: one spot INDEX set + one F&O contract set (1minute each)."""
    root = tmp_path / "lake"
    _touch_partition(root, "bars", "NSE", "INDEX", "NIFTY", "1minute", "2024")
    _touch_partition(root, "bars", "NSE", "FNO", "NSE-NIFTY-06Feb20-12250-PE", "1minute", "2020")
    return root


def _caches() -> dict[str, MoversSnapshotCache]:
    return {
        "db_datasets_cache": MoversSnapshotCache(),
        "spot_cascade_cache": MoversSnapshotCache(),
        "fno_cascade_cache": MoversSnapshotCache(),
        "latest_date_cache": MoversSnapshotCache(),
    }


def _warm(lake: Path, caches: dict[str, MoversSnapshotCache], *, generation: int = 7) -> int:
    return warm_discovery_memos(
        lake_root=lake,
        generation=generation,
        symbol_for_id_fn=lambda: {"13": "NIFTY"},
        **caches,
    )


class TestFilterSpotDatasets:
    def test_drops_fno_keeps_spot(self, lake: Path) -> None:
        from brutex.dashboard.db_view import list_candle_datasets

        candles = list_candle_datasets(lake)
        spot = filter_spot_datasets(candles)
        assert [d["segment"] for d in spot] == ["INDEX"]
        assert all(d["segment"] != "FNO" for d in spot)

    def test_empty_in_empty_out(self) -> None:
        assert filter_spot_datasets([]) == []


class TestBuildDbDatasetsPayload:
    def test_walks_when_no_prewalked_lists(self, lake: Path) -> None:
        payload = build_db_datasets_payload(lake, symbol_for_id={})
        assert [d["symbol"] for d in payload["datasets"]] == ["NIFTY"]
        assert [d["symbol"] for d in payload["fno"]] == ["NSE-NIFTY-06Feb20-12250-PE"]
        assert payload["options"] == []
        assert [c["name"] for c in payload["fno_schema"]][:2] == ["timestamp", "open"]

    def test_prewalked_lists_produce_identical_payload(self, lake: Path) -> None:
        from brutex.dashboard.db_view import list_candle_datasets, list_fno_candle_datasets

        walked = build_db_datasets_payload(
            lake,
            symbol_for_id={},
            candle_datasets=list_candle_datasets(lake),
            fno_datasets=list_fno_candle_datasets(lake),
        )
        fresh = build_db_datasets_payload(lake, symbol_for_id={})
        assert walked == fresh  # byte-identical shape either way


class TestWarmSeedsAllMemos:
    def test_memos_seeded_derived_from_report_bases(self) -> None:
        # Round-1 review drift pin (DEC-O1-CLAIMS-HONESTY-001): the warm's
        # log/return contract is DERIVED — the 5 legacy memos plus ONE preopen
        # memo per REPORT_BASES entry — never a hand-summed literal. A future
        # third basis then moves _MEMOS_SEEDED automatically instead of
        # `discovery.warm_done` silently under-reporting a stale hardcoded 7.
        assert dw._LEGACY_MEMOS == 5
        assert dw._LEGACY_MEMOS + len(REPORT_BASES) == dw._MEMOS_SEEDED
        assert dw._MEMOS_SEEDED == 7  # honest today: 5 legacy + 2 bases

    def test_all_seven_memos_seeded_at_generation(self, lake: Path) -> None:
        caches = _caches()
        with capture_logs() as logs:
            assert _warm(lake, caches, generation=7) == 7
        done = [e for e in logs if e["event"] == "discovery.warm_done"]
        assert len(done) == 1
        assert done[0]["generation"] == 7
        assert done[0]["memos"] == 7

        def _must_not_run() -> Any:
            raise AssertionError("a seeded memo must be a clean hit — no re-walk")

        db_payload = caches["db_datasets_cache"].get_or_compute(
            DB_DATASETS_KEY, generation=7, compute=_must_not_run
        )
        assert [d["symbol"] for d in db_payload["datasets"]] == ["NIFTY"]
        movers_list = caches["db_datasets_cache"].get_or_compute(
            MOVERS_DATASETS_KEY, generation=7, compute=_must_not_run
        )
        assert {d["segment"] for d in movers_list} == {"INDEX", "FNO"}
        spot_idx = caches["spot_cascade_cache"].get_or_compute(
            SPOT_CASCADE_KEY, generation=7, compute=_must_not_run
        )
        assert isinstance(spot_idx, SpotCascadeIndex)
        assert [s.symbol for s in spot_idx.symbols()] == ["NIFTY"]
        fno_idx = caches["fno_cascade_cache"].get_or_compute(
            FNO_CASCADE_KEY, generation=7, compute=_must_not_run
        )
        assert isinstance(fno_idx, FnoCascadeIndex)
        assert list(fno_idx.underlyings()) == ["NIFTY"]
        latest = caches["latest_date_cache"].get_or_compute(
            LATEST_MOVERS_DATE_KEY, generation=7, compute=_must_not_run
        )
        assert latest is None  # no 1day partitions in the fixture — honest None

    def test_seeded_db_payload_matches_route_builder(self, lake: Path) -> None:
        # The warm-seeded value must be byte-identical to what the route's
        # `_datasets()` (which delegates to build_db_datasets_payload) computes.
        caches = _caches()
        _warm(lake, caches, generation=3)
        seeded = caches["db_datasets_cache"].get_or_compute(
            DB_DATASETS_KEY, generation=3, compute=lambda: {"never": "runs"}
        )
        assert seeded == build_db_datasets_payload(lake, symbol_for_id={"13": "NIFTY"})

    def test_shared_candle_walk_runs_once(
        self, lake: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The whole point of the warm: ONE list_candle_datasets walk feeds the
        # db payload + the movers list + the spot cascade (the F&O decode walk
        # additionally reuses it internally via its own single call).
        from brutex.dashboard.db_view import list_candle_datasets as real_walk

        calls = {"n": 0}

        def _counting(root: Path) -> list[dict[str, str]]:
            calls["n"] += 1
            return real_walk(root)

        monkeypatch.setattr(dw, "list_candle_datasets", _counting)
        assert _warm(lake, _caches()) == 7
        assert calls["n"] == 1

    def test_force_refreshes_through_pull_active_floor(self, lake: Path) -> None:
        # The warm runs while the pull job is still `running` — an opted-in
        # floor would serve the stale pre-pull entry; force_fresh must win.
        floored = MoversSnapshotCache(pull_active_probe=lambda: True, adaptive_floor=True)
        # A stale pre-pull entry at an OLD generation, well within the base floor.
        floored.get_or_compute(MOVERS_DATASETS_KEY, generation=1, compute=lambda: ["stale"])
        floored.get_or_compute(DB_DATASETS_KEY, generation=1, compute=lambda: {"stale": True})
        caches = _caches()
        caches["db_datasets_cache"] = floored
        assert _warm(lake, caches, generation=2) == 7

        def _must_not_run() -> list[dict[str, str]]:
            raise AssertionError("the warm seed must be the clean hit at generation 2")

        fresh = floored.get_or_compute(MOVERS_DATASETS_KEY, generation=2, compute=_must_not_run)
        assert {d["segment"] for d in fresh} == {"INDEX", "FNO"}  # the fresh seed, not "stale"


class TestPreopenMemos:
    def test_preopen_datasets_key_format(self) -> None:
        # Key-constant drift pin: the |-joined per-basis memo keys.
        assert preopen_datasets_key("day") == "preopen_datasets|day"
        assert preopen_datasets_key("minute") == "preopen_datasets|minute"

    def test_latest_movers_minute_key_format(self) -> None:
        # Key-constant drift pin (DEC-MOVERS-ASOF-LATEST-DEFAULT-001): the
        # |-joined date-scoped latest-MINUTE memo key — distinct per date and
        # never colliding with the sibling latest-DATE key.
        assert latest_movers_minute_key("2024-06-13") == "latest_movers_minute|2024-06-13"
        assert latest_movers_minute_key("2024-06-13") != latest_movers_minute_key("2024-06-14")
        assert not LATEST_MOVERS_DATE_KEY.startswith(f"{LATEST_MOVERS_MINUTE_KEY_PREFIX}|")

    def test_preopen_memos_seeded_per_basis(self, lake: Path) -> None:
        # DEC-O1-CLAIMS-HONESTY-001: the warm seeds one preopen memo per basis
        # off its ONE shared unfiltered walk (datasets= seam), byte-identical
        # to a direct filtered resolve.
        caches = _caches()
        assert _warm(lake, caches, generation=7) == 7

        def _poison() -> Any:
            raise AssertionError("a seeded preopen memo must be a clean hit — no re-walk")

        # DEC-UNIVERSE-INDEX-ONLY-001 (design §3 R3): the warm seeds the SAME
        # scope-token-folded composite key the /reports route probes.
        seeded = {
            basis: caches["db_datasets_cache"].get_or_compute(
                preopen_datasets_key(basis, scope_token=scope_revision()),
                generation=7,
                compute=_poison,
            )
            for basis in REPORT_BASES
        }
        for basis in REPORT_BASES:
            assert seeded[basis] == resolve_report_datasets(
                lake, basis=basis, scope=resolve_read_scope()
            )
        assert seeded["day"] == []  # the fixture holds no 1day partition
        # The F&O contract partition is excluded — only the spot INDEX symbol.
        assert seeded["minute"] == [("NSE", "INDEX", "NIFTY")]


class TestWarmBestEffort:
    def test_raising_walk_returns_zero_never_raises(
        self, lake: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(root: Path) -> list[dict[str, str]]:
            raise OSError("stalled volume")

        monkeypatch.setattr(dw, "list_candle_datasets", _boom)
        with capture_logs() as logs:
            assert _warm(lake, _caches()) == 0
        failed = [e for e in logs if e["event"] == "discovery.warm_failed"]
        assert len(failed) == 1
        assert failed[0]["error_type"] == "OSError"

    def test_raising_symbol_map_returns_zero(self, lake: Path) -> None:
        def _store_broken() -> dict[str, str]:
            raise RuntimeError("store closed")

        caches = _caches()
        assert (
            warm_discovery_memos(
                lake_root=lake,
                generation=1,
                symbol_for_id_fn=_store_broken,
                **caches,
            )
            == 0
        )
        # Nothing was seeded — the next read computes for itself.
        assert caches["db_datasets_cache"].size() == 0
