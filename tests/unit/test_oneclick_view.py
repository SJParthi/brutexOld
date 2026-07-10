"""Unit suite for ``brutex/dashboard/oneclick_view.py`` (DEC-OPERATOR-PAGE-REAL-001).

Drives the pure payload builders to 100% line+branch STANDALONE (the ci.yml
per-module gate). Fails-before framing: the module is NEW; the pins here are
the R27/R44 honesty laws — every pill/link/check "state" word is DERIVED
mechanically (payload names the derivation), counts are never fabricated
(``row_count == len(rows)``, missing funnel keys ⇒ ``ok=None``), the R44
truth table caps operator-judged checks at ``ready`` and un-wired checks at
``not_wired`` (never a fake green), and EVERY builder stamps
``fixture_root_active`` (C-MINOR-1).
"""

from __future__ import annotations

from typing import Any

import pytest
from brutex.dashboard import oneclick_view as ov
from brutex.dashboard.oneclick_artifacts import (
    EventsPage,
    ManifestResult,
    RunInfo,
    Top100Result,
    TradesResult,
)

# ── helpers ─────────────────────────────────────────────────────────────


def _info(**overrides: Any) -> RunInfo:
    base: dict[str, Any] = {
        "run_id": "r-x",
        "dir_mtime_ns": 1_751_800_000_000_000_000,
        "has_manifest": True,
        "has_top100": True,
        "has_events": True,
        "suspect": False,
    }
    base.update(overrides)
    return RunInfo(**base)


def _manifest_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "run_id": "r-x",
        "selection": {},
        "estimate": {},
        "actual": {
            "enumerated": 10,
            "tested": 8,
            "skipped": 2,
            "funnel": [
                {"stage": "tested", "count": 8},
                {"stage": "gates", "count": 5},
                {"stage": "rank", "count": 2},
            ],
        },
        "per_stage_timings": [{"stage": "sweep", "seconds": 1.5}],
        "per_door_kills": [{"door": "gates", "killed": 3}, {"door": "rank", "killed": 3}],
        "dedup_skips": 2,
        "tripwires": [],
        "suspect": False,
    }
    body.update(overrides)
    return body


def _ok_manifest(**overrides: Any) -> ManifestResult:
    return ManifestResult("ok", _manifest_body(**overrides), "", "1:1")


_AWAITING = ManifestResult("awaiting", None, "awaiting real run artifact", None)
_CORRUPT = ManifestResult("corrupt", None, "manifest.json is not valid JSON", "1:1")


# ── runs payload ────────────────────────────────────────────────────────


class TestBuildRunsPayload:
    def test_empty_is_loud_awaiting(self) -> None:
        payload = ov.build_runs_payload((), fixture_root_active=False)
        assert payload["status"] == "awaiting"
        assert payload["runs"] == []
        assert payload["latest_run_id"] is None
        assert "awaiting real run artifact" in payload["detail"]

    def test_rows_project_the_probe_facts(self) -> None:
        infos = (_info(run_id="r-new", suspect=None), _info(run_id="r-old", suspect=True))
        payload = ov.build_runs_payload(infos, fixture_root_active=True)
        assert payload["status"] == "ok"
        assert payload["latest_run_id"] == "r-new"
        assert payload["fixture_root_active"] is True
        first = payload["runs"][0]
        assert first["run_id"] == "r-new"
        assert first["suspect"] is None  # never a fabricated False
        assert payload["runs"][1]["suspect"] is True

    def test_mtime_renders_as_utc_iso(self) -> None:
        payload = ov.build_runs_payload(
            (_info(dir_mtime_ns=1_751_800_000_000_000_000),), fixture_root_active=False
        )
        assert payload["runs"][0]["mtime_utc"] == "2025-07-06T11:06:40Z"


# ── run pill ────────────────────────────────────────────────────────────


class TestRunPillState:
    def test_suspect_wins(self) -> None:
        pill = ov.run_pill_state(_info(), _ok_manifest(suspect=True))
        assert pill == {"state": "suspect", "derived_from": "manifest.suspect is true"}

    def test_complete_needs_readable_funnel(self) -> None:
        pill = ov.run_pill_state(_info(), _ok_manifest())
        assert pill["state"] == "complete"
        assert "actual.funnel" in pill["derived_from"]

    def test_readable_manifest_without_funnel_is_pending(self) -> None:
        manifest = _ok_manifest(actual={"enumerated": 1})
        pill = ov.run_pill_state(_info(), manifest)
        assert pill["state"] == "pending"

    def test_corrupt_manifest_is_artifacts_partial(self) -> None:
        pill = ov.run_pill_state(_info(), _CORRUPT)
        assert pill == {"state": "artifacts partial", "derived_from": "manifest.json corrupt"}

    def test_absent_run_dir_is_awaiting(self) -> None:
        pill = ov.run_pill_state(None, _AWAITING)
        assert pill["state"] == "awaiting"

    def test_missing_files_named(self) -> None:
        info = _info(has_manifest=False, has_events=False)
        pill = ov.run_pill_state(info, _AWAITING)
        assert pill["state"] == "artifacts partial"
        assert "manifest.json" in pill["derived_from"]
        assert "events.jsonl" in pill["derived_from"]
        assert "top100.json" not in pill["derived_from"]

    def test_no_manifest_verdict_with_all_files_is_pending(self) -> None:
        pill = ov.run_pill_state(_info(), None)
        assert pill["state"] == "pending"

    def test_actual_non_dict_falls_through(self) -> None:
        pill = ov.run_pill_state(_info(), _ok_manifest(actual="junk"))
        assert pill["state"] == "pending"


# ── manifest / top100 / events / trades payloads ────────────────────────


class TestBuildManifestPayload:
    def test_ok_passthrough_with_pill(self) -> None:
        manifest = _ok_manifest()
        payload = ov.build_manifest_payload(
            "r-x", manifest, fixture_root_active=False, info=_info()
        )
        assert payload["status"] == "ok"
        assert payload["run_id"] == "r-x"
        assert payload["manifest"] is manifest.manifest  # contract keys verbatim
        assert payload["pill"]["state"] == "complete"
        assert payload["revision"] == "1:1"
        assert payload["fixture_root_active"] is False

    def test_awaiting_without_info(self) -> None:
        payload = ov.build_manifest_payload("r-x", _AWAITING, fixture_root_active=True)
        assert payload["status"] == "awaiting"
        assert payload["manifest"] is None
        assert payload["pill"]["state"] == "awaiting"
        assert payload["fixture_root_active"] is True


class TestBuildTop100Payload:
    def test_ok_rows_row_count_is_len(self) -> None:
        rows = ({"rank": 1, "metrics": {"sharpe": 1.0000000000000002}},)
        payload = ov.build_top100_payload(
            "r-x", Top100Result("ok", rows, "", "1:1"), fixture_root_active=False
        )
        assert payload["row_count"] == 1  # never assumed 100 (T-M2)
        assert payload["empty_note"] is None
        assert payload["rows"][0]["metrics"]["sharpe"] == 1.0000000000000002  # untouched

    def test_resilience_block_passes_through_untouched(self) -> None:
        """The sibling resilience block is a top-level entry key the reader
        boundary never maps — it reaches the wire byte-for-byte (full
        precision), so the /strategy panel + /screener read row.resilience.*."""
        resilience = {
            "stats_live": True,
            "losing_streak_p95": 4,
            "worst_streak_pnl_pct": -12.5,
            "longest_underwater_days": 30,
            "win_rate_by_year": {"2024": [30, 50]},
        }
        rows = ({"rank": 1, "metrics": {"max_consecutive_losses": 5}, "resilience": resilience},)
        payload = ov.build_top100_payload(
            "r-x", Top100Result("ok", rows, "", "1:1"), fixture_root_active=False
        )
        assert payload["rows"][0]["resilience"] == resilience  # untouched, full precision

    def test_valid_empty_gets_the_honest_note(self) -> None:
        payload = ov.build_top100_payload(
            "r-x", Top100Result("ok", (), "", "1:1"), fixture_root_active=False
        )
        assert payload["status"] == "ok"
        assert payload["row_count"] == 0
        assert payload["empty_note"] == "0 winners stored for this run"

    def test_corrupt_has_no_empty_note(self) -> None:
        payload = ov.build_top100_payload(
            "r-x", Top100Result("corrupt", (), "bad", "1:1"), fixture_root_active=False
        )
        assert payload["status"] == "corrupt"
        assert payload["empty_note"] is None
        assert payload["detail"] == "bad"

    def test_preset_passes_through_for_the_sort_basis_label(self) -> None:
        """DEC-ONECLICK-TOP100-OBJECT-ROOT-001: the artifact's R30 preset token
        reaches the payload so the pages can LABEL the sort basis honestly."""
        payload = ov.build_top100_payload(
            "r-x",
            Top100Result("ok", (), "", "1:1", "min_dd_first"),
            fixture_root_active=False,
        )
        assert payload["preset"] == "min_dd_first"

    def test_preset_defaults_to_none_when_the_root_never_parsed(self) -> None:
        payload = ov.build_top100_payload(
            "r-x", Top100Result("awaiting", (), "", None), fixture_root_active=False
        )
        assert payload["preset"] is None  # honest — the label falls back client-side


class TestBuildEventsPayload:
    def test_passthrough_and_window_filter_flag(self) -> None:
        page = EventsPage("ok", ({"kind": "a"},), 120, 240, True, "note")
        payload = ov.build_events_payload(
            "r-x", page, fixture_root_active=True, window_filtered=True
        )
        assert payload["records"] == [{"kind": "a"}]
        assert payload["next_offset"] == 120
        assert payload["total_bytes"] == 240
        assert payload["truncated_tail"] is True
        assert payload["window_filtered"] is True
        assert payload["fixture_root_active"] is True

    def test_window_filter_defaults_false(self) -> None:
        page = EventsPage("awaiting", (), 0, 0, False, "awaiting real run artifact")
        payload = ov.build_events_payload("r-x", page, fixture_root_active=False)
        assert payload["window_filtered"] is False


class TestBuildTradesPayload:
    def test_passthrough_incl_honest_null_total(self) -> None:
        page = TradesResult("awaiting", (), None, "none", "trades land at merge-integration")
        payload = ov.build_trades_payload(
            "r-x", 3, page, offset=10, limit=50, fixture_root_active=False
        )
        assert payload["rank"] == 3
        assert payload["total"] is None  # never a fabricated 0
        assert payload["offset"] == 10
        assert payload["limit"] == 50
        assert payload["source"] == "none"

    def test_fixture_rows_pass_through_with_null_vix(self) -> None:
        rows = ({"pnl_paisa": 100, "vix_entry": None, "vix_exit": None},)
        page = TradesResult("ok", rows, 412, "fixture", "")
        payload = ov.build_trades_payload(
            "r-x", 1, page, offset=0, limit=100, fixture_root_active=True
        )
        assert payload["rows"][0]["vix_entry"] is None  # R31 honest NULL
        assert payload["total"] == 412
        assert payload["source"] == "fixture"
        assert payload["fixture_root_active"] is True


class TestBuildEquityPayload:
    """DEC-ONECLICK-WINNER-DETAIL-TRUTH-001 item 4C — the equity route projector."""

    def test_delegates_to_the_one_page_projector_byte_identically(self) -> None:
        """§11 COMMON: the equity payload IS the trades page shape over the
        stored series points — the delegation can never fork the wire."""
        rows = (
            {
                "session_date": "2026-07-01",
                "equity_close_paisa": 1_000,
                "equity_intraday_low_paisa": None,  # honest None low
                "trades_closed": 1,
            },
        )
        page = TradesResult("ok", rows, 3, "results_store", "")
        equity = ov.build_equity_payload(
            "r-x", 2, page, offset=0, limit=5000, fixture_root_active=False
        )
        trades = ov.build_trades_payload(
            "r-x", 2, page, offset=0, limit=5000, fixture_root_active=False
        )
        assert equity == trades
        assert equity["rows"][0]["equity_intraday_low_paisa"] is None
        assert equity["total"] == 3

    def test_awaiting_series_keeps_total_null(self) -> None:
        page = TradesResult("awaiting", (), None, "none", "awaiting real run artifact — x")
        payload = ov.build_equity_payload(
            "r-x", 1, page, offset=0, limit=100, fixture_root_active=False
        )
        assert payload["status"] == "awaiting"
        assert payload["total"] is None  # never a fabricated 0


# ── chain map ───────────────────────────────────────────────────────────


class TestChainLinks:
    def test_fixed_stage_order(self) -> None:
        links = ov.chain_links(_AWAITING, ())
        assert [link["stage"] for link in links] == list(ov.CHAIN_STAGES)
        assert all(link["state"] == "pending" for link in links)  # unknown → NEVER green

    def test_green_from_list_shaped_timings(self) -> None:
        links = {link["stage"]: link for link in ov.chain_links(_ok_manifest(), ())}
        assert links["sweep"]["state"] == "green"
        assert links["gates"]["state"] == "pending"

    def test_green_from_dict_shaped_timings(self) -> None:
        manifest = _ok_manifest(per_stage_timings={"rank": 2.0})
        links = {link["stage"]: link for link in ov.chain_links(manifest, ())}
        assert links["rank"]["state"] == "green"

    def test_malformed_timing_entries_ignored(self) -> None:
        manifest = _ok_manifest(per_stage_timings=["junk", {"seconds": 1}, {"stage": "rank"}])
        links = {link["stage"]: link for link in ov.chain_links(manifest, ())}
        assert links["rank"]["state"] == "green"
        assert links["sweep"]["state"] == "pending"

    def test_non_list_timings_ignored(self) -> None:
        manifest = _ok_manifest(per_stage_timings="junk")
        assert all(link["state"] == "pending" for link in ov.chain_links(manifest, ()))

    def test_amber_from_event_kinds_without_timing(self) -> None:
        manifest = _ok_manifest(per_stage_timings=[])
        links = {link["stage"]: link for link in ov.chain_links(manifest, ("SWEEP.tuple_done",))}
        assert links["sweep"]["state"] == "amber"
        assert links["gates"]["state"] == "pending"

    def test_red_from_dict_tripwire_beats_green(self) -> None:
        manifest = _ok_manifest(tripwires=[{"stage": "sweep", "note": "count drift"}])
        links = {link["stage"]: link for link in ov.chain_links(manifest, ())}
        assert links["sweep"]["state"] == "red"  # tripwire wins over the timing

    def test_red_from_string_tripwire_substring(self) -> None:
        manifest = _ok_manifest(tripwires=["NaN detector fired in cascade metrics"])
        links = {link["stage"]: link for link in ov.chain_links(manifest, ())}
        assert links["cascade"]["state"] == "red"

    def test_malformed_tripwire_entries_ignored(self) -> None:
        manifest = _ok_manifest(tripwires=[{"stage": 42}, 3.14], per_stage_timings=[])
        assert all(link["state"] == "pending" for link in ov.chain_links(manifest, ()))

    def test_non_list_tripwires_ignored(self) -> None:
        manifest = _ok_manifest(tripwires="junk", per_stage_timings=[])
        assert all(link["state"] == "pending" for link in ov.chain_links(manifest, ()))


# ── funnel reconcile ────────────────────────────────────────────────────


class TestReconcileFunnel:
    def test_missing_manifest_is_awaiting_ok_none(self) -> None:
        verdict = ov.reconcile_funnel(_AWAITING)
        assert verdict["ok"] is None
        assert verdict["status"] == "awaiting"

    def test_actual_missing_or_not_dict_is_awaiting(self) -> None:
        for actual in (None, "junk"):
            body = _manifest_body()
            if actual is None:
                del body["actual"]
            else:
                body["actual"] = actual
            verdict = ov.reconcile_funnel(ManifestResult("partial", body, "", None))
            assert verdict["ok"] is None
            assert verdict["status"] == "awaiting"

    def test_missing_pieces_named(self) -> None:
        body = _manifest_body()
        del body["per_door_kills"]
        body["actual"] = {"enumerated": 1, "tested": True, "funnel": []}
        verdict = ov.reconcile_funnel(ManifestResult("partial", body, "", None))
        assert verdict["ok"] is None
        assert verdict["status"] == "partial"
        line = verdict["lines"][0]
        assert "per_door_kills" in line
        assert "actual.tested" in line  # a bool is NOT an honest count
        assert "actual.skipped" in line

    def test_malformed_funnel_entry_named(self) -> None:
        body = _manifest_body()
        body["actual"]["funnel"] = [{"stage": "tested", "count": 8}, "junk"]
        verdict = ov.reconcile_funnel(ManifestResult("ok", body, "", None))
        assert verdict["ok"] is None
        assert "actual.funnel[1]" in verdict["lines"][0]

    def test_funnel_entry_bad_types_named(self) -> None:
        body = _manifest_body()
        body["actual"]["funnel"] = [{"stage": "tested", "count": True}]
        verdict = ov.reconcile_funnel(ManifestResult("ok", body, "", None))
        assert verdict["ok"] is None
        assert "actual.funnel[0]" in verdict["lines"][0]

    def test_malformed_kill_entry_named(self) -> None:
        body = _manifest_body()
        body["per_door_kills"] = [{"door": "gates", "killed": "3"}]
        verdict = ov.reconcile_funnel(ManifestResult("ok", body, "", None))
        assert verdict["ok"] is None
        assert "per_door_kills[0]" in verdict["lines"][0]

    def test_non_dict_kill_entry_named(self) -> None:
        body = _manifest_body()
        body["per_door_kills"] = [7]
        verdict = ov.reconcile_funnel(ManifestResult("ok", body, "", None))
        assert verdict["ok"] is None
        assert "per_door_kills[0]" in verdict["lines"][0]

    def test_door_count_mismatch_abstains_never_a_false_fail(self) -> None:
        """Hostile-round MINOR-1: the ABSENT-key law makes a sparse kills list
        a LEGITIMATE real-P2 shape — the reconcile abstains (ok=None, amber),
        it must NEVER paint a scary red ok=False on a count/name mismatch."""
        body = _manifest_body()
        body["per_door_kills"] = [{"door": "gates", "killed": 3}]  # 3 stages, 1 door
        verdict = ov.reconcile_funnel(ManifestResult("ok", body, "", None))
        assert verdict["ok"] is None
        assert verdict["status"] == "partial"
        line = verdict["lines"][0]
        assert "1 door(s)" in line
        assert "3 stage(s)" in line
        assert "merge-integration" in line
        assert "✗" not in line  # abstention, not a verdict

    def test_door_surplus_also_abstains(self) -> None:
        """The mismatch abstention is symmetric: MORE doors than transitions
        (a run core observing extra doors) abstains too — never a false red."""
        body = _manifest_body()
        body["per_door_kills"].append({"door": "extra", "killed": 0})
        verdict = ov.reconcile_funnel(ManifestResult("ok", body, "", None))
        assert verdict["ok"] is None
        assert verdict["status"] == "partial"
        assert "3 door(s)" in verdict["lines"][0]

    def test_happy_arithmetic_reconciles_to_the_digit(self) -> None:
        verdict = ov.reconcile_funnel(_ok_manifest())
        assert verdict["ok"] is True
        assert verdict["status"] == "ok"
        assert verdict["lines"][0] == "tested 8 − gates kills 3 = 5 → gates ✓"
        assert verdict["lines"][-1] == "enumerated 10 == tested 8 + skipped 2 ✓"

    def test_stage_arithmetic_break_is_a_fail_naming_the_stage(self) -> None:
        body = _manifest_body()
        body["actual"]["funnel"][1]["count"] = 4  # manifest says 4, arithmetic says 5
        verdict = ov.reconcile_funnel(ManifestResult("ok", body, "", None))
        assert verdict["ok"] is False
        assert "✗ (manifest says 4)" in verdict["lines"][0]

    def test_enumerated_sum_break_is_a_fail(self) -> None:
        body = _manifest_body()
        body["actual"]["enumerated"] = 11
        verdict = ov.reconcile_funnel(ManifestResult("ok", body, "", None))
        assert verdict["ok"] is False
        assert verdict["lines"][-1].endswith("✗")

    def test_empty_funnel_abstains_never_a_vacuous_pass(self) -> None:
        """Regression: [] funnel + [] doors used to read ok=True having
        reconciled NOTHING — zero evidence must abstain, never pass."""
        body = _manifest_body()
        body["actual"]["funnel"] = []
        body["per_door_kills"] = []
        verdict = ov.reconcile_funnel(ManifestResult("ok", body, "", None))
        assert verdict["ok"] is None
        assert verdict["status"] == "awaiting"
        assert "actual.funnel is empty" in verdict["lines"][0]
        assert "✓" not in verdict["lines"][0]  # abstention, not a verdict


# ── R44 truth table ─────────────────────────────────────────────────────


class TestVerifyChecks:
    def test_seven_rows_with_stable_ids(self) -> None:
        checks = ov.verify_checks(_ok_manifest(), None, False)
        assert [check["id"] for check in checks] == [1, 2, 3, 4, 5, 6, 7]

    def test_unwired_checks_can_never_pass(self) -> None:
        checks = {check["id"]: check for check in ov.verify_checks(_ok_manifest(), None, True)}
        for check_id in (1, 5, 6):
            assert checks[check_id]["state"] == "not_wired"
            assert "merge-integration" in checks[check_id]["detail"]

    def test_chain_none_is_awaiting_with_the_caveat(self) -> None:
        row = ov.verify_checks(_ok_manifest(), None, False)[1]
        assert row["state"] == "awaiting"
        assert "(algorithm assumed until P2 merges)" in row["detail"]
        assert row["records"] is None
        assert row["first_break_line"] is None

    def test_chain_pass_and_fail(self) -> None:
        ok_detail = "chain verified: 118 records (algorithm assumed until P2 merges)"
        row = ov.verify_checks(_ok_manifest(), (True, 118, None, ok_detail), False)[1]
        assert row["state"] == "pass"
        assert row["records"] == 118
        assert row["detail"] == ok_detail
        bad = ov.verify_checks(
            _ok_manifest(),
            (False, 3, 4, "chain break at line 4 (algorithm assumed until P2 merges)"),
            False,
        )[1]
        assert bad["state"] == "fail"
        assert bad["first_break_line"] == 4

    def test_chain_clean_over_zero_records_is_awaiting_never_pass(self) -> None:
        """Regression: (ok=True, records=0) — the empty events.jsonl of a run
        killed before the recorder's first write — used to read a green PASS
        having verified nothing. It must read awaiting, with the caveat."""
        row = ov.verify_checks(
            _ok_manifest(),
            (True, 0, None, "chain verified: 0 records (algorithm assumed until P2 merges)"),
            False,
        )[1]
        assert row["state"] == "awaiting"
        assert row["records"] == 0
        assert row["first_break_line"] is None
        assert "verifies nothing" in row["detail"]
        assert "(algorithm assumed until P2 merges)" in row["detail"]

    def test_operator_judged_checks_cap_at_ready(self) -> None:
        """C-M4: #3/#4 can NEVER exceed 'ready' server-side."""
        checks = {check["id"]: check for check in ov.verify_checks(_ok_manifest(), None, True)}
        assert checks[3]["state"] == "ready"
        assert checks[4]["state"] == "ready"
        assert "operator" in checks[3]["detail"]
        assert "never auto-passes server-side" in checks[3]["detail"]

    def test_no_trades_makes_operator_checks_awaiting(self) -> None:
        checks = {check["id"]: check for check in ov.verify_checks(_ok_manifest(), None, False)}
        assert checks[3]["state"] == "awaiting"
        assert checks[4]["state"] == "awaiting"

    def test_funnel_row_tracks_the_reconcile_verdict(self) -> None:
        assert ov.verify_checks(_ok_manifest(), None, False)[6]["state"] == "pass"
        body = _manifest_body()
        body["actual"]["enumerated"] = 999
        failing = ManifestResult("ok", body, "", None)
        assert ov.verify_checks(failing, None, False)[6]["state"] == "fail"
        assert ov.verify_checks(_AWAITING, None, False)[6]["state"] == "awaiting"

    def test_states_stay_within_the_five_state_vocabulary(self) -> None:
        allowed = {"pass", "fail", "ready", "awaiting", "not_wired"}
        for chain in (None, (True, 1, None, "x"), (False, 0, 1, "y"), (True, 0, None, "z")):
            for trades in (True, False):
                for check in ov.verify_checks(_ok_manifest(), chain, trades):
                    assert check["state"] in allowed


class TestBuildVerifyPayload:
    def test_assembles_checks_and_reconcile(self) -> None:
        payload = ov.build_verify_payload(
            "r-x", _ok_manifest(), None, True, fixture_root_active=True
        )
        assert payload["run_id"] == "r-x"
        assert len(payload["checks"]) == 7
        assert payload["funnel_reconcile"]["ok"] is True
        assert isinstance(payload["funnel_reconcile"]["lines"], list)
        assert payload["fixture_root_active"] is True

    def test_reconcile_computed_exactly_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Hostile-round NIT-3: build_verify_payload reconciles ONCE and threads
        the verdict into verify_checks — never a second reconcile_funnel call."""
        calls: list[ManifestResult] = []
        real = ov.reconcile_funnel

        def _counting(manifest: ManifestResult) -> dict[str, object]:
            calls.append(manifest)
            return real(manifest)

        monkeypatch.setattr(ov, "reconcile_funnel", _counting)
        payload = ov.build_verify_payload(
            "r-once", _ok_manifest(), None, False, fixture_root_active=False
        )
        assert len(calls) == 1
        assert payload["funnel_reconcile"]["ok"] is True
        assert payload["checks"][6]["state"] == "pass"

    def test_mismatched_funnel_yields_awaiting_check_row(self) -> None:
        """End-to-end MINOR-1: a sparse per_door_kills flows through the payload
        as check #7 'awaiting' (amber) with ok=None — never a red 'fail'."""
        body = _manifest_body()
        body["per_door_kills"] = [{"door": "gates", "killed": 3}]
        payload = ov.build_verify_payload(
            "r-sparse",
            ManifestResult("ok", body, "", None),
            None,
            False,
            fixture_root_active=False,
        )
        assert payload["funnel_reconcile"]["ok"] is None
        assert payload["checks"][6]["state"] == "awaiting"


class TestFixtureFlagStampedEverywhere:
    """C-MINOR-1: ALL builders stamp fixture_root_active — parametrized both ways."""

    @pytest.mark.parametrize("active", [True, False])
    def test_all_builders_stamp_the_flag(self, active: bool) -> None:
        payloads = [
            ov.build_runs_payload((), fixture_root_active=active),
            ov.build_manifest_payload("r", _AWAITING, fixture_root_active=active),
            ov.build_top100_payload(
                "r", Top100Result("awaiting", (), "", None), fixture_root_active=active
            ),
            ov.build_events_payload(
                "r", EventsPage("awaiting", (), 0, 0, False, ""), fixture_root_active=active
            ),
            ov.build_trades_payload(
                "r",
                1,
                TradesResult("awaiting", (), None, "none", ""),
                offset=0,
                limit=1,
                fixture_root_active=active,
            ),
            ov.build_verify_payload("r", _AWAITING, None, False, fixture_root_active=active),
        ]
        assert all(payload["fixture_root_active"] is active for payload in payloads)
