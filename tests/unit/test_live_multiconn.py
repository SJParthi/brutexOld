"""Unit tests for the N-connection live subscription orchestration.

Every collaborator (feed open, subscribe) is injected with a fake — no
``growwapi``, no network. Covers :mod:`brutex.live.multiconn`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from brutex.live import multiconn as mc


def _idx(token: str) -> dict[str, Any]:
    return {"exchange": "NSE", "segment": "IDX", "exchange_token": token, "instrument_type": "IDX"}


def _eq(token: str) -> dict[str, Any]:
    return {"exchange": "NSE", "segment": "CASH", "exchange_token": token, "instrument_type": "EQ"}


def _leg(token: str, *, underlying: str) -> dict[str, Any]:
    return {
        "exchange": "NSE",
        "segment": "FNO",
        "exchange_token": token,
        "instrument_type": "CE",
        "underlying_symbol": underlying,
    }


# ───────────────────────── _instrument_of ─────────────────────────


def test_instrument_of_reads_underlying_variants() -> None:
    assert mc._instrument_of(_leg("1", underlying="NIFTY")).underlying == "NIFTY"
    # the bare ``underlying`` key is honoured when ``underlying_symbol`` is absent
    assert mc._instrument_of({**_eq("2"), "underlying": "RELIANCE"}).underlying == "RELIANCE"
    # neither key → no underlying
    assert mc._instrument_of(_eq("3")).underlying is None


# ───────────────────────── plan_connections ───────────────────────


def test_plan_connections_spot_first_then_legs() -> None:
    spot = [_idx("26000"), _eq("100")]
    legs = [_leg("900", underlying="NIFTY"), _leg("901", underlying="NIFTY")]
    bindings, skipped, plan = mc.plan_connections(spot, legs, per_connection_cap=1000)
    assert skipped == 0
    # spot fills connection #0 ("both"), legs connection #1 ("ltp"), never mixed
    assert [b.connection_index for b in bindings] == [0, 1]
    assert bindings[0].feed_kind == "both" and bindings[0].is_spot
    assert {r["exchange_token"] for r in bindings[0].instruments} == {"26000", "100"}
    assert bindings[1].feed_kind == "ltp" and not bindings[1].is_spot
    assert {r["exchange_token"] for r in bindings[1].instruments} == {"900", "901"}
    assert plan.num_connections == 2 and bindings[0].size == 2


def test_plan_connections_shards_each_group_at_the_cap() -> None:
    spot = [_idx("26000"), _eq("100")]  # 2 → 2 spot shards at cap=1
    legs = [_leg("900", underlying="NIFTY"), _leg("901", underlying="BANKNIFTY")]
    bindings, skipped, _plan = mc.plan_connections(spot, legs, per_connection_cap=1)
    assert skipped == 0
    assert [b.feed_kind for b in bindings] == ["both", "both", "ltp", "ltp"]
    assert all(b.size == 1 for b in bindings)


def test_plan_connections_honours_the_ceiling_and_reports_skipped() -> None:
    spot = [_idx("26000"), _eq("100")]
    legs = [_leg("900", underlying="NIFTY"), _leg("901", underlying="NIFTY")]
    # cap=1 → 4 shards; ceiling=2 binds #0,#1 and skips the 2 leg shards
    bindings, skipped, plan = mc.plan_connections(spot, legs, per_connection_cap=1, ceiling=2)
    assert plan.num_connections == 4
    assert [b.connection_index for b in bindings] == [0, 1]
    assert skipped == 2


def test_plan_connections_dedups_cross_group_spot_wins() -> None:
    shared = _idx("26000")
    bindings, _skipped, _plan = mc.plan_connections([shared], [shared], per_connection_cap=1000)
    # the instrument present in BOTH groups is kept ONCE, on its spot connection
    assert len(bindings) == 1
    assert bindings[0].is_spot and bindings[0].size == 1


def test_plan_connections_empty_is_zero_connections() -> None:
    bindings, skipped, plan = mc.plan_connections([], [], per_connection_cap=1000)
    assert bindings == [] and skipped == 0 and plan.num_connections == 0


def test_plan_connections_zero_ceiling_skips_everything() -> None:
    bindings, skipped, plan = mc.plan_connections([_idx("26000")], [], ceiling=0)
    assert bindings == [] and skipped == plan.num_connections == 1


def test_plan_connections_rejects_negative_ceiling() -> None:
    with pytest.raises(ValueError, match="ceiling must be >= 0"):
        mc.plan_connections([_idx("26000")], [], ceiling=-1)


# ─────────────────────── open_and_subscribe ───────────────────────


class _FakeFeed:
    def __init__(self, ordinal: int) -> None:
        self.ordinal = ordinal


def test_open_and_subscribe_opens_one_feed_per_binding() -> None:
    spot = [_idx("26000")]
    legs = [_leg("900", underlying="NIFTY")]
    bindings, _skipped, _plan = mc.plan_connections(spot, legs, per_connection_cap=1000)

    opened_ordinals: list[int] = []
    calls: list[tuple[int, str, int]] = []
    cb = lambda: None  # noqa: E731 - sentinel callback

    def open_feed(ordinal: int) -> _FakeFeed:
        opened_ordinals.append(ordinal)
        return _FakeFeed(ordinal)

    def make_subscribe(feed: Any, instruments: Any, feed_kind: str):
        def subscribe(callback: Any) -> None:
            assert callback is cb  # the SAME callback feeds every connection
            calls.append((feed.ordinal, feed_kind, len(list(instruments))))

        return subscribe

    opened = mc.open_and_subscribe(
        bindings, open_feed=open_feed, make_subscribe=make_subscribe, callback=cb
    )
    assert opened_ordinals == [0, 1]
    assert calls == [(0, "both", 1), (1, "ltp", 1)]
    assert [o.binding.connection_index for o in opened] == [0, 1]
    assert opened[0].feed.ordinal == 0


# ──────────────────────────── report ──────────────────────────────


def test_build_report_summarises_per_connection() -> None:
    spot = [_idx("26000"), _eq("100")]
    legs = [_leg("900", underlying="NIFTY")]
    bindings, skipped, _plan = mc.plan_connections(spot, legs, per_connection_cap=1000)
    report = mc.build_report(bindings, skipped)
    assert report.num_connections == 2
    assert report.total_instruments == 3
    assert report.connections[0].feed_kind == "both" and report.connections[0].subscribed == 2
    assert report.summary() == "2 connections (1 spot + 1 leg), 3 instruments"
    md = report.render_markdown()
    assert "| #0 | both | 2 |" in md and "| #1 | ltp | 1 |" in md


def test_build_report_surfaces_skipped_shards() -> None:
    spot = [_idx("26000"), _eq("100")]
    legs = [_leg("900", underlying="NIFTY"), _leg("901", underlying="NIFTY")]
    bindings, skipped, _plan = mc.plan_connections(spot, legs, per_connection_cap=1, ceiling=2)
    report = mc.build_report(bindings, skipped)
    assert report.skipped_shards == 2
    assert "2 shard(s) over the ceiling" in report.summary()


# ─────────────────── make_multiconn_capture seam ──────────────────


def test_make_multiconn_capture_subscribes_all_and_merges_snapshots() -> None:
    spot = [_idx("26000")]
    legs = [_leg("900", underlying="NIFTY")]
    bindings, _skipped, _plan = mc.plan_connections(spot, legs, per_connection_cap=1000)

    def open_feed(ordinal: int) -> _FakeFeed:
        return _FakeFeed(ordinal)

    def make_subscribe(feed: Any, _instruments: Any, _feed_kind: str):
        return lambda _cb: None  # the real SDK subscribe is exercised elsewhere

    def snapshot_of(feed: _FakeFeed, feed_kind: str) -> dict[str, Any]:
        # each connection contributes a distinct instrument to the merged record
        return {f"conn{feed.ordinal}:{feed_kind}": float(feed.ordinal)}

    subscribe, read_snapshot, opened = mc.make_multiconn_capture(
        bindings, open_feed=open_feed, make_subscribe=make_subscribe, snapshot_of=snapshot_of
    )
    # nothing opened until subscribe() is called (the capture's start() does it)
    assert opened == []
    assert read_snapshot() == {}
    subscribe(lambda: None)
    assert [o.binding.connection_index for o in opened] == [0, 1]
    # the merged snapshot unions every connection's snapshot
    assert read_snapshot() == {"conn0:both": 0.0, "conn1:ltp": 1.0}


# ─────────────────── subscription manifest (775 + legs) ───────────


def test_read_subscription_manifest_splits_spot_and_legs(tmp_path) -> None:
    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps(
            {"spot": [_idx("NIFTY"), _eq("2885")], "legs": [_leg("35241", underlying="NIFTY")]}
        ),
        encoding="utf-8",
    )
    spot, legs = mc.read_subscription_manifest(path)
    assert [r["exchange_token"] for r in spot] == ["NIFTY", "2885"]
    assert [r["exchange_token"] for r in legs] == ["35241"]
    # the rows feed straight into plan_connections (spot #0, legs #1..N)
    bindings, _skipped, _plan = mc.plan_connections(spot, legs, per_connection_cap=1000)
    assert [b.feed_kind for b in bindings] == ["both", "ltp"]


def test_read_subscription_manifest_defaults_missing_groups_to_empty(tmp_path) -> None:
    path = tmp_path / "m.json"
    path.write_text(json.dumps({"spot": [_idx("NIFTY")]}), encoding="utf-8")  # no "legs"
    spot, legs = mc.read_subscription_manifest(path)
    assert len(spot) == 1 and legs == []


def test_read_subscription_manifest_rejects_bad_shapes(tmp_path) -> None:
    bad_top = tmp_path / "top.json"
    bad_top.write_text(json.dumps([1, 2, 3]), encoding="utf-8")  # not an object
    with pytest.raises(ValueError, match="manifest must be a JSON object"):
        mc.read_subscription_manifest(bad_top)

    bad_group = tmp_path / "group.json"
    bad_group.write_text(json.dumps({"spot": "NIFTY"}), encoding="utf-8")  # group not a list
    with pytest.raises(ValueError, match="'spot' must be a list"):
        mc.read_subscription_manifest(bad_group)

    bad_row = tmp_path / "row.json"
    bad_row.write_text(json.dumps({"legs": ["NIFTY"]}), encoding="utf-8")  # row not an object
    with pytest.raises(ValueError, match="'legs' row must be an object"):
        mc.read_subscription_manifest(bad_row)

    missing_key = tmp_path / "miss.json"
    missing_key.write_text(
        json.dumps({"spot": [{"exchange": "NSE", "segment": "CASH"}]}), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="missing keys"):
        mc.read_subscription_manifest(missing_key)


# ─────────────────── probe→manifest stamping (DEC-LIVE-PROBE-MANIFEST-001) ───────────────────


def _probe(max_held: int, *, failed_at: int | None, failure: str | None) -> Any:
    """Build a :class:`~brutex.live.probe.ConnectionProbeResult` for the stamp tests."""
    from brutex.live.probe import ConnectionProbeResult

    return ConnectionProbeResult(max_held=max_held, failed_at=failed_at, failure=failure)


def test_probe_meta_block_matches_dashboard_reader_shape() -> None:
    # A refused ramp: 7 held, #8 refused → reached_ceiling True.
    block = mc.probe_meta_block(_probe(7, failed_at=8, failure="RuntimeError: refused"))
    assert block == {"max_held": 7, "failed_at": 8, "reached_ceiling": True}
    # A ramp that hit --max with no refusal → failed_at None, reached_ceiling False.
    clean = mc.probe_meta_block(_probe(100, failed_at=None, failure=None))
    assert clean == {"max_held": 100, "failed_at": None, "reached_ceiling": False}
    # The failure REASON string is deliberately not carried (reader keys on the int).
    assert "failure" not in block


def test_merge_probe_preserves_spot_legs_and_meta_plan() -> None:
    manifest = {
        "spot": [_idx("NIFTY")],
        "legs": [_leg("900", underlying="NIFTY")],
        "meta": {"plan": {"per_connection_cap": 1000, "spot": 1, "legs": 1}},
    }
    merged = mc.merge_probe_into_manifest(manifest, _probe(5, failed_at=6, failure="x"))
    # spot/legs/meta.plan untouched, meta.probe added.
    assert merged["spot"] == manifest["spot"]
    assert merged["legs"] == manifest["legs"]
    assert merged["meta"]["plan"] == {"per_connection_cap": 1000, "spot": 1, "legs": 1}
    assert merged["meta"]["probe"] == {"max_held": 5, "failed_at": 6, "reached_ceiling": True}
    # Pure: the input manifest is not mutated.
    assert "probe" not in manifest["meta"]


def test_merge_probe_replaces_malformed_meta() -> None:
    # A non-mapping meta is replaced by a fresh meta carrying only probe.
    merged = mc.merge_probe_into_manifest(
        {"spot": [], "legs": [], "meta": "broken"}, _probe(3, failed_at=None, failure=None)
    )
    assert merged["meta"] == {"probe": {"max_held": 3, "failed_at": None, "reached_ceiling": False}}


def test_merge_probe_replaces_existing_probe() -> None:
    manifest = {
        "spot": [],
        "legs": [],
        "meta": {"probe": {"max_held": 1, "failed_at": 2, "reached_ceiling": True}},
    }
    merged = mc.merge_probe_into_manifest(manifest, _probe(9, failed_at=None, failure=None))
    assert merged["meta"]["probe"] == {"max_held": 9, "failed_at": None, "reached_ceiling": False}


def test_merge_probe_when_no_meta_present() -> None:
    merged = mc.merge_probe_into_manifest(
        {"spot": [], "legs": []}, _probe(4, failed_at=5, failure="y")
    )
    assert merged["meta"] == {"probe": {"max_held": 4, "failed_at": 5, "reached_ceiling": True}}


def test_stamp_probe_manifest_file_present(tmp_path) -> None:
    path = tmp_path / "live-manifest.json"
    path.write_text(
        json.dumps(
            {
                "spot": [_idx("NIFTY")],
                "legs": [],
                "meta": {"plan": {"per_connection_cap": 1000, "spot": 1, "legs": 0}},
            }
        ),
        encoding="utf-8",
    )
    stamped = mc.stamp_probe_manifest_file(path, _probe(6, failed_at=7, failure="refused"))
    assert stamped is True
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["spot"] == [_idx("NIFTY")]
    assert on_disk["meta"]["plan"] == {"per_connection_cap": 1000, "spot": 1, "legs": 0}
    assert on_disk["meta"]["probe"] == {"max_held": 6, "failed_at": 7, "reached_ceiling": True}


def test_stamp_probe_manifest_file_missing_is_untouched(tmp_path) -> None:
    """An absent manifest → False, nothing written (dashboard keeps 'probe not run')."""
    path = tmp_path / "nope.json"
    stamped = mc.stamp_probe_manifest_file(path, _probe(6, failed_at=7, failure="refused"))
    assert stamped is False
    assert not path.exists()


def test_stamp_probe_manifest_file_unreadable_json_is_untouched(tmp_path) -> None:
    path = tmp_path / "broken.json"
    path.write_text("{not valid json", encoding="utf-8")
    stamped = mc.stamp_probe_manifest_file(path, _probe(6, failed_at=None, failure=None))
    assert stamped is False
    # The corrupt file is left exactly as-is, never overwritten with a fabricated probe.
    assert path.read_text(encoding="utf-8") == "{not valid json"


def test_stamp_probe_manifest_file_non_object_is_untouched(tmp_path) -> None:
    path = tmp_path / "list.json"
    path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    stamped = mc.stamp_probe_manifest_file(path, _probe(6, failed_at=None, failure=None))
    assert stamped is False
    assert json.loads(path.read_text(encoding="utf-8")) == [1, 2, 3]


def test_stamp_probe_manifest_file_oserror_is_untouched(tmp_path, monkeypatch) -> None:
    """A read that raises OSError (e.g. a directory path) → False, never raises."""
    path = tmp_path / "amanifest.json"
    path.write_text(json.dumps({"spot": [], "legs": []}), encoding="utf-8")

    def _boom(*_args: Any, **_kwargs: Any) -> str:
        raise OSError("disk gone")

    monkeypatch.setattr(Path, "read_text", _boom)
    stamped = mc.stamp_probe_manifest_file(path, _probe(6, failed_at=None, failure=None))
    assert stamped is False


def test_stamp_probe_manifest_file_is_byte_idempotent(tmp_path) -> None:
    """Stamping the SAME probe twice yields byte-identical manifest content (Golden Rule #7)."""
    path = tmp_path / "live-manifest.json"
    path.write_text(
        json.dumps({"spot": [_idx("NIFTY")], "legs": [], "meta": {"plan": {"spot": 1}}}),
        encoding="utf-8",
    )
    probe = _probe(8, failed_at=9, failure="ceiling")
    first_ok = mc.stamp_probe_manifest_file(path, probe)
    first_bytes = path.read_bytes()
    second_ok = mc.stamp_probe_manifest_file(path, probe)
    second_bytes = path.read_bytes()
    assert first_ok is True and second_ok is True
    assert first_bytes == second_bytes


def test_stamp_then_read_round_trip_surfaces_ceiling(tmp_path) -> None:
    """A stamped manifest, read by the REAL dashboard reader, surfaces the ceiling."""
    from brutex.dashboard.live_plan_view import read_connection_plan

    path = tmp_path / "live-manifest.json"
    path.write_text(
        json.dumps(
            {
                "spot": [_idx("NIFTY"), _eq("100")],
                "legs": [_leg("900", underlying="NIFTY")],
                "meta": {"plan": {"per_connection_cap": 1000, "spot": 2, "legs": 1}},
            }
        ),
        encoding="utf-8",
    )
    ok = mc.stamp_probe_manifest_file(path, _probe(42, failed_at=43, failure="ceiling"))
    assert ok is True
    payload = read_connection_plan(Path(path))
    # The dashboard panel now shows the REAL ceiling, not "probe not run".
    assert payload["probe"] == {"max_held": 42, "failed_at": 43, "reached_ceiling": True}
    assert payload["probe_note"] is None
    # The plan band (spot/legs/total/connections) is preserved + re-derived.
    assert payload["plan"]["spot"] == 2 and payload["plan"]["legs"] == 1
    assert payload["plan"]["total"] == 3


def test_merge_probe_is_pure_no_shared_mutable_subobjects() -> None:
    """The merge deep-copies — mutating the result never corrupts the input (purity)."""
    manifest: dict[str, Any] = {
        "spot": [_idx("NIFTY")],
        "legs": [],
        "meta": {"plan": {"per_connection_cap": 1000, "spot": 1, "legs": 0}},
    }
    merged = mc.merge_probe_into_manifest(manifest, _probe(5, failed_at=6, failure="x"))
    # No mutable sub-object is shared between input and result.
    assert merged["spot"] is not manifest["spot"]
    assert merged["meta"]["plan"] is not manifest["meta"]["plan"]
    # Mutating the result leaves the input untouched.
    merged["spot"].append(_eq("999"))
    merged["meta"]["plan"]["per_connection_cap"] = 4242
    assert manifest["spot"] == [_idx("NIFTY")]
    assert manifest["meta"]["plan"]["per_connection_cap"] == 1000


def test_stamp_probe_manifest_canonicalises_key_order(tmp_path) -> None:
    """An OUT-OF-ORDER manifest stamps to canonical sort_keys bytes (the idempotence guard).

    Proves ``sort_keys=True`` is load-bearing: a manifest whose keys were emitted in
    a non-sorted order yields the SAME canonical bytes as a sorted one, so re-stamping
    is byte-stable regardless of how the dashboard writer ordered the original keys.
    """
    import json as _json

    out_of_order = tmp_path / "scrambled.json"
    # Deliberately reversed top-level + nested key order; no sort on write.
    out_of_order.write_text(
        _json.dumps(
            {"meta": {"plan": {"spot": 1, "legs": 0}}, "legs": [], "spot": [_idx("NIFTY")]}
        ),
        encoding="utf-8",
    )
    probe = _probe(8, failed_at=9, failure="ceiling")
    stamped = mc.stamp_probe_manifest_file(out_of_order, probe)
    assert stamped is True
    scrambled_bytes = out_of_order.read_bytes()

    # A second manifest with the SAME content but already-sorted insertion order.
    sorted_src = tmp_path / "sorted.json"
    sorted_src.write_text(
        _json.dumps(
            {"legs": [], "meta": {"plan": {"legs": 0, "spot": 1}}, "spot": [_idx("NIFTY")]}
        ),
        encoding="utf-8",
    )
    stamped2 = mc.stamp_probe_manifest_file(sorted_src, probe)
    assert stamped2 is True
    # Identical canonical bytes despite different source key orderings.
    assert scrambled_bytes == sorted_src.read_bytes()


def test_stamp_zero_max_held_round_trips_as_real_ceiling(tmp_path) -> None:
    """A probe that held ZERO connections before the first refusal surfaces 0, not 'probe not run'.

    ``max_held=0`` is a VALID ceiling (the account refused even the first connection);
    the reader's ``isinstance(int) and not bool`` guard accepts 0, so it must NOT be
    dropped as a falsy/absent ceiling (Golden Rule #1 — an honest zero, not a guess).
    """
    from brutex.dashboard.live_plan_view import read_connection_plan

    path = tmp_path / "live-manifest.json"
    path.write_text(json.dumps({"spot": [], "legs": []}), encoding="utf-8")
    stamped = mc.stamp_probe_manifest_file(path, _probe(0, failed_at=1, failure="refused"))
    assert stamped is True
    payload = read_connection_plan(Path(path))
    assert payload["probe"] == {"max_held": 0, "failed_at": 1, "reached_ceiling": True}
    assert payload["probe_note"] is None
