"""Unit tests for the dashboard live-capture subscription-manifest writer.

The store + the F&O registry are faked / seeded (no real DuckDB needed for the
pure builders; a real seeded registry exercises the leg path). The manifest is
asserted to round-trip through :func:`brutex.live.multiconn.read_subscription_manifest`
— the ONLY ``brutex.live`` import is HERE in the test (shape-compatibility
assertion), never in the dashboard source (``DEC-LIVE-DECOUPLE-001``).
Covers :mod:`brutex.dashboard.live_manifest`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from brutex.dashboard.live_manifest import (
    build_leg_rows,
    build_live_manifest,
    build_manifest,
    build_spot_rows,
    make_store_manifest_writer,
    write_manifest,
)


@dataclass
class _Instr:
    """A minimal stand-in for the store's ``Instrument`` (only the read fields)."""

    exch_id: str
    segment: str
    security_id: int
    instrument: str
    underlying_symbol: str | None
    feed_token: str | None = None


class _FakeStore:
    """Returns a fixed IDX+EQ universe via ``list_index_equity_instruments``."""

    def __init__(self, rows: list[_Instr]) -> None:
        self._rows = rows

    def list_index_equity_instruments(self) -> list[_Instr]:
        return list(self._rows)


def _universe() -> list[_Instr]:
    return [
        # NSE index: string CSV token "NIFTY" → security_id is a hash surrogate,
        # so the feed token MUST come from feed_token, never security_id.
        _Instr("NSE", "INDEX", 935205938, "IDX", None, feed_token="NIFTY"),
        # BSE index: numeric CSV token "1" survives in security_id AND feed_token.
        _Instr("BSE", "INDEX", 1, "IDX", None, feed_token="1"),
        _Instr("NSE", "CASH", 2885, "EQ", "RELIANCE", feed_token="2885"),
        # A FUT row that somehow survived in the master must NOT be a spot row.
        _Instr("NSE", "FNO", 53001, "FUT", "NIFTY", feed_token="53001"),
    ]


# ───────────────────────────── spot rows ─────────────────────────


def test_build_spot_rows_keeps_only_idx_and_eq() -> None:
    rows = build_spot_rows(_FakeStore(_universe()))
    # 2 indices + 1 equity = 3; the FUT row is excluded.
    assert len(rows) == 3
    assert {r["instrument_type"] for r in rows} == {"IDX", "EQ"}
    # NSE index: the Groww feed row uses segment="CASH" (NOT the internal "INDEX")
    # and the RAW feed token "NIFTY" (NOT the security_id surrogate 935205938) —
    # docs/groww/api-reference/07-feed.md:132.
    nifty = rows[0]
    assert nifty == {
        "exchange": "NSE",
        "segment": "CASH",
        "exchange_token": "NIFTY",
        "instrument_type": "IDX",
        "underlying_symbol": None,
    }


def test_build_spot_rows_index_token_is_feed_token_not_security_id() -> None:
    """The index feed token is the RAW CSV token, never the hashed security_id."""
    rows = build_spot_rows(_FakeStore(_universe()))
    sensex = next(r for r in rows if r["exchange"] == "BSE")
    # BSE SENSEX feeds on segment CASH + token "1" (07-feed.md:132).
    assert sensex == {
        "exchange": "BSE",
        "segment": "CASH",
        "exchange_token": "1",
        "instrument_type": "IDX",
        "underlying_symbol": None,
    }
    # Equity: segment CASH + the numeric token (subscribe_ltp, 07-feed.md:54).
    reliance = next(r for r in rows if r["instrument_type"] == "EQ")
    assert reliance["segment"] == "CASH" and reliance["exchange_token"] == "2885"


def test_build_spot_rows_falls_back_to_security_id_without_feed_token() -> None:
    """A legacy row (no feed_token) falls back to security_id (pre-v7 store)."""
    legacy = _Instr("NSE", "CASH", 2885, "EQ", "RELIANCE", feed_token=None)
    rows = build_spot_rows(_FakeStore([legacy]))
    assert rows[0]["exchange_token"] == "2885" and rows[0]["segment"] == "CASH"


def test_build_spot_rows_empty_universe() -> None:
    assert build_spot_rows(_FakeStore([])) == []


# ───────────────────────────── leg rows ──────────────────────────


def test_build_leg_rows_missing_registry_is_empty(tmp_path) -> None:
    """No registry file → legs = [] (degrade honestly, never invent — Golden Rule #1)."""
    assert build_leg_rows(tmp_path / "registry.duckdb") == []


def test_build_leg_rows_reads_contracts(tmp_path) -> None:
    """A seeded registry yields one FNO leg feed-row per contract."""
    pytest.importorskip("duckdb", reason="registry leg test needs the [data] extra")
    from brutex.data.registry_store import RegistryStore

    path = tmp_path / "registry.duckdb"
    store = RegistryStore.open(path)
    now = datetime(2026, 6, 14, tzinfo=UTC)
    store.upsert_contract(
        "NSE",
        "NIFTY",
        datetime(2026, 6, 26).date(),
        "NSE-NIFTY-26Jun26-23000-CE",
        side="CE",
        strike=23000.0,
        run_id="r1",
        fetched_at=now,
    )
    store.upsert_contract(
        "NSE",
        "NIFTY",
        datetime(2026, 6, 26).date(),
        "NSE-NIFTY-26Jun26-FUT",
        side="FUT",
        strike=None,
        run_id="r1",
        fetched_at=now,
    )
    store.close()

    legs = build_leg_rows(path)
    assert len(legs) == 2
    ce = next(leg for leg in legs if leg["instrument_type"] == "CE")
    assert ce == {
        "exchange": "NSE",
        "segment": "FNO",
        "exchange_token": "NSE-NIFTY-26Jun26-23000-CE",
        "instrument_type": "CE",
        "underlying_symbol": "NIFTY",
    }
    assert {leg["instrument_type"] for leg in legs} == {"CE", "FUT"}


# ───────────────────────────── assembly ──────────────────────────


def test_build_manifest_copies_rows() -> None:
    spot = [
        {"exchange": "NSE", "segment": "INDEX", "exchange_token": "1", "instrument_type": "IDX"}
    ]
    legs: list[dict] = []
    manifest = build_manifest(spot, legs)
    assert manifest == {"spot": spot, "legs": []}
    # The manifest copies rows — mutating the source does not leak into it.
    spot[0]["exchange_token"] = "CHANGED"
    assert manifest["spot"][0]["exchange_token"] == "1"


def test_build_live_manifest_composes_spot_and_legs(tmp_path) -> None:
    """The full composition: spot from the store + legs [] when no registry."""
    manifest = build_live_manifest(_FakeStore(_universe()), tmp_path / "registry.duckdb")
    assert len(manifest["spot"]) == 3 and manifest["legs"] == []


# ───────────────────────────── write + round-trip ─────────────────


def test_write_manifest_round_trips_through_capture_reader(tmp_path) -> None:
    """The written manifest parses cleanly through the capture's reader (shape contract).

    The ONLY ``brutex.live`` import is in this TEST (shape assertion), never in the
    dashboard source (DEC-LIVE-DECOUPLE-001).
    """
    from brutex.live.multiconn import read_subscription_manifest

    manifest = build_live_manifest(_FakeStore(_universe()), tmp_path / "registry.duckdb")
    out = write_manifest(tmp_path / "m.json", manifest)
    assert out.is_file()
    # The on-disk JSON is exactly the manifest dict.
    assert json.loads(out.read_text(encoding="utf-8")) == manifest
    # And the capture's reader accepts every row (the 4 feed keys are present).
    spot, legs = read_subscription_manifest(out)
    assert len(spot) == 3 and legs == []


def test_write_manifest_creates_parent_dirs(tmp_path) -> None:
    out = write_manifest(tmp_path / "nested" / "deeper" / "m.json", {"spot": [], "legs": []})
    assert out.is_file()
    assert json.loads(out.read_text(encoding="utf-8")) == {"spot": [], "legs": []}


# ─────────────────────── store-bound writer builder ──────────────


class _StoreWithPath(_FakeStore):
    """A store whose ``.path`` resolves the sibling registry (the production shape)."""

    def __init__(self, rows: list[_Instr], path) -> None:
        super().__init__(rows)
        self.path = path


def test_make_writer_none_store_writes_empty_manifest(tmp_path) -> None:
    """No store wired → the writer emits an EMPTY manifest (capture self-resolves)."""
    writer = make_store_manifest_writer(None)
    out = tmp_path / "m.json"
    writer(str(out))
    assert json.loads(out.read_text(encoding="utf-8")) == {"spot": [], "legs": []}


def test_make_writer_store_with_path_writes_spot(tmp_path) -> None:
    """A store with a path writes the spot rows + resolves its sibling registry."""
    store = _StoreWithPath(_universe(), tmp_path / "instruments.duckdb")
    writer = make_store_manifest_writer(store)
    out = tmp_path / "m.json"
    writer(str(out))
    manifest = json.loads(out.read_text(encoding="utf-8"))
    assert len(manifest["spot"]) == 3 and manifest["legs"] == []  # no registry file → []


def test_make_writer_store_without_path_falls_back(tmp_path, monkeypatch) -> None:
    """A store with no on-disk path falls back to the relative registry name (absent → [])."""
    monkeypatch.chdir(tmp_path)  # the fallback 'registry.duckdb' resolves under tmp (absent)
    store = _FakeStore(_universe())  # no .path attribute
    writer = make_store_manifest_writer(store)
    out = tmp_path / "m.json"
    writer(str(out))
    manifest = json.loads(out.read_text(encoding="utf-8"))
    assert len(manifest["spot"]) == 3 and manifest["legs"] == []
