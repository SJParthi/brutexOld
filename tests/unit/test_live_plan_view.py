"""Unit tests for the dashboard live N-connection plan / probe manifest reader.

The reader parses the PLAIN ``{spot, legs, meta}`` JSON manifest the dashboard wrote
and re-derives the connection plan locally — it NEVER imports :mod:`brutex.live`
(``DEC-LIVE-DECOUPLE-001``); the test seeds a manifest file by hand. Covers
:mod:`brutex.dashboard.live_plan_view`.
"""

from __future__ import annotations

import json

from brutex.dashboard.live_plan_view import (
    DEFAULT_PER_CONNECTION_CAP,
    EMPTY_NOTE,
    NO_PROBE_NOTE,
    read_connection_plan,
)


def _write(tmp_path, payload) -> str:
    """Write *payload* (a dict or raw value) as a JSON manifest, return its path."""
    path = tmp_path / "live-manifest.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def _rows(n: int) -> list[dict[str, str]]:
    """Return *n* placeholder feed-rows (only the count matters to the plan)."""
    return [{"exchange": "NSE", "exchange_token": str(i)} for i in range(n)]


def test_missing_manifest_is_empty_plan(tmp_path) -> None:
    payload = read_connection_plan(tmp_path / "nope.json")
    assert payload["note"] == EMPTY_NOTE
    assert payload["plan"] == {
        "connections": 0,
        "per_connection_cap": DEFAULT_PER_CONNECTION_CAP,
        "spot": 0,
        "legs": 0,
        "total": 0,
    }
    assert payload["probe"] is None and payload["probe_note"] == NO_PROBE_NOTE


def test_unreadable_manifest_is_empty_plan(tmp_path, monkeypatch) -> None:
    """A file whose read raises OSError → the honest empty plan (never raises)."""
    path = tmp_path / "m.json"
    path.write_text('{"spot": []}', encoding="utf-8")

    def _boom(self, *args, **kwargs):
        raise OSError("permission denied")

    monkeypatch.setattr("pathlib.Path.read_text", _boom)
    assert read_connection_plan(path)["note"] == EMPTY_NOTE


def test_invalid_json_is_empty_plan(tmp_path) -> None:
    path = tmp_path / "m.json"
    path.write_text("{not json", encoding="utf-8")
    assert read_connection_plan(path)["note"] == EMPTY_NOTE


def test_non_object_manifest_is_empty_plan(tmp_path) -> None:
    """A JSON list (not an object) degrades to the empty plan, never raises."""
    assert read_connection_plan(_write(tmp_path, [1, 2, 3]))["note"] == EMPTY_NOTE


def test_single_connection_under_cap(tmp_path) -> None:
    """775 spot + 0 legs at the default 1000 cap → 1 connection."""
    payload = read_connection_plan(_write(tmp_path, {"spot": _rows(775), "legs": []}))
    assert payload["note"] is None
    assert payload["plan"] == {
        "connections": 1,
        "per_connection_cap": DEFAULT_PER_CONNECTION_CAP,
        "spot": 775,
        "legs": 0,
        "total": 775,
    }
    assert payload["probe"] is None and payload["probe_note"] == NO_PROBE_NOTE


def test_many_legs_shard_into_multiple_connections(tmp_path) -> None:
    """775 spot + 1000 legs at cap 1000 → ceil(1775/1000) = 2 connections."""
    payload = read_connection_plan(_write(tmp_path, {"spot": _rows(775), "legs": _rows(1000)}))
    assert payload["plan"]["connections"] == 2
    assert payload["plan"]["spot"] == 775 and payload["plan"]["legs"] == 1000
    assert payload["plan"]["total"] == 1775


def test_zero_instruments_zero_connections(tmp_path) -> None:
    payload = read_connection_plan(_write(tmp_path, {"spot": [], "legs": []}))
    assert payload["plan"]["connections"] == 0 and payload["plan"]["total"] == 0
    assert payload["note"] is None  # the manifest exists (just empty) → not the EMPTY note


def test_stamped_cap_wins_and_recomputes_connections(tmp_path) -> None:
    """A stamped meta.plan.per_connection_cap drives the connection arithmetic."""
    payload = read_connection_plan(
        _write(
            tmp_path,
            {"spot": _rows(3), "legs": _rows(2), "meta": {"plan": {"per_connection_cap": 2}}},
        )
    )
    # 5 total / cap 2 → ceil(2.5) = 3 connections; the stamped cap is surfaced.
    assert payload["plan"]["per_connection_cap"] == 2
    assert payload["plan"]["connections"] == 3


def test_malformed_stamped_cap_falls_back_to_default(tmp_path) -> None:
    """A non-int / bool / <1 stamped cap falls back to the default (never divides by 0)."""
    for bad in [0, -5, True, "1000", None]:
        payload = read_connection_plan(
            _write(tmp_path, {"spot": _rows(2000), "meta": {"plan": {"per_connection_cap": bad}}})
        )
        assert payload["plan"]["per_connection_cap"] == DEFAULT_PER_CONNECTION_CAP
        assert payload["plan"]["connections"] == 2  # 2000 / 1000


def test_malformed_group_counts_zero(tmp_path) -> None:
    """A non-list spot/legs group counts as 0 rather than raising."""
    payload = read_connection_plan(_write(tmp_path, {"spot": "not-a-list", "legs": 5}))
    assert payload["plan"]["spot"] == 0 and payload["plan"]["legs"] == 0


def test_non_dict_meta_is_ignored(tmp_path) -> None:
    """A meta that is not an object degrades to the default cap (no probe)."""
    payload = read_connection_plan(_write(tmp_path, {"spot": _rows(1), "meta": "oops"}))
    assert payload["plan"]["per_connection_cap"] == DEFAULT_PER_CONNECTION_CAP
    assert payload["probe"] is None


def test_probe_block_surfaced_when_present(tmp_path) -> None:
    """A stamped meta.probe with an integer max_held surfaces the ceiling."""
    payload = read_connection_plan(
        _write(
            tmp_path,
            {
                "spot": _rows(775),
                "legs": [],
                "meta": {"probe": {"max_held": 8, "failed_at": 9, "reached_ceiling": True}},
            },
        )
    )
    assert payload["probe"] == {"max_held": 8, "failed_at": 9, "reached_ceiling": True}
    assert payload["probe_note"] is None


def test_probe_without_failed_at_normalises_none(tmp_path) -> None:
    """A probe that reached max_conns (failed_at None / bool) → failed_at None."""
    payload = read_connection_plan(
        _write(tmp_path, {"spot": [], "meta": {"probe": {"max_held": 50, "failed_at": True}}})
    )
    assert payload["probe"] == {"max_held": 50, "failed_at": None, "reached_ceiling": False}


def test_probe_rejected_when_no_usable_ceiling(tmp_path) -> None:
    """A probe block without an integer max_held (absent / bool / str) → None."""
    for bad in [{"max_held": True}, {"max_held": "8"}, {"failed_at": 3}, "not-a-dict"]:
        payload = read_connection_plan(_write(tmp_path, {"spot": [], "meta": {"probe": bad}}))
        assert payload["probe"] is None and payload["probe_note"] == NO_PROBE_NOTE
