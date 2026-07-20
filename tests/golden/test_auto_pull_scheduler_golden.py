"""Frozen auto-pull-scheduler goldens (`DEC-RUST-PULL-STATE-001`, Wave 3G).

The committed fixtures (``auto_pull_scheduler_verdicts.json`` + the
serde-free ``auto_pull_scheduler_verdicts.tsv`` projection) were captured by
``generate_auto_pull_scheduler_golden.py`` on the PRE-conversion tree — they
are the oracle the Rust port of ``brutex/dashboard/auto_pull_scheduler.py``'s
tick-decision + durable-state-parse plane must satisfy bit-for-bit (Golden
Rule #1).  This test recomputes the SAME tables from the LIVE module via the
shared ``_auto_pull_scheduler_harness`` and asserts structural equality, plus
pins:

* the TSV is EXACTLY the projection of the JSON (one truth, two encodings —
  the cargo fixture test consumes the TSV, so the two can never drift);
* the load-bearing lattice rows are genuinely IN the fixture (anti-vacuous:
  the ladder ORDER — a both-marks state reads ``already_succeeded``; the
  laziness law — a disarmed tick consults NO gate, a deferred tick calls NO
  launcher; the started-marks / refused-marks-nothing split);
* the schema value-law puns (``true`` / ``1.0`` legitimately PASS the
  ``schema == 1`` law) and the corrupt⇒DISARMED posture are in the corpus.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.golden._auto_pull_scheduler_harness import (
    STATE_VALID,
    TICK_ALREADY_SUCCEEDED,
    TICK_DEFERRED,
    TICK_DISARMED,
    TICK_FIRE,
    TICK_LAUNCHER_UNBOUND,
    compute_payload,
    compute_tsv,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
JSON_FIXTURE = FIXTURES / "auto_pull_scheduler_verdicts.json"
TSV_FIXTURE = FIXTURES / "auto_pull_scheduler_verdicts.tsv"


@pytest.fixture(scope="module")
def frozen() -> dict[str, Any]:
    payload = json.loads(JSON_FIXTURE.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


@pytest.fixture(scope="module")
def live() -> dict[str, Any]:
    payload = json.loads(json.dumps(compute_payload(), allow_nan=False))
    assert isinstance(payload, dict)
    return payload


def _tick(payload: dict[str, Any], name: str) -> dict[str, Any]:
    (row,) = [row for row in payload["tick"] if row["name"] == name]
    assert isinstance(row, dict)
    return row


class TestFrozenGolden:
    def test_tick_rows_match_frozen(self, frozen: dict[str, Any], live: dict[str, Any]) -> None:
        assert live["tick"] == frozen["tick"]

    def test_sequence_rows_match_frozen(self, frozen: dict[str, Any], live: dict[str, Any]) -> None:
        assert live["sequence"] == frozen["sequence"]

    def test_state_rows_match_frozen(self, frozen: dict[str, Any], live: dict[str, Any]) -> None:
        assert live["state"] == frozen["state"]

    def test_tsv_is_the_json_projection(self, frozen: dict[str, Any]) -> None:
        assert TSV_FIXTURE.read_text(encoding="utf-8") == compute_tsv(frozen)


class TestAntiVacuous:
    """The load-bearing rows are genuinely in the frozen lattice."""

    def test_lattice_is_exhaustive(self, frozen: dict[str, Any]) -> None:
        lattice = [row for row in frozen["tick"] if row["name"].startswith("lattice_")]
        assert len(lattice) == 32
        combos = {
            (
                row["armed"],
                row["succeeded_today"],
                row["attempted_today"],
                row["gate_allowed"],
                row["launcher_bound"],
            )
            for row in lattice
        }
        assert len(combos) == 32

    def test_ladder_order_succeeded_beats_attempted(self, frozen: dict[str, Any]) -> None:
        row = _tick(frozen, "lattice_a1s1t1g1b1")
        assert row["token"] == "already_succeeded"
        assert row["code"] == TICK_ALREADY_SUCCEEDED

    def test_disarmed_never_consults_the_gate(self, frozen: dict[str, Any]) -> None:
        for row in frozen["tick"]:
            if not row["armed"]:
                assert row["code"] == TICK_DISARMED
                assert (row["gate_calls"], row["launcher_calls"]) == (0, 0)

    def test_deferred_never_calls_the_launcher(self, frozen: dict[str, Any]) -> None:
        row = _tick(frozen, "lattice_a1s0t0g0b1")
        assert row["code"] == TICK_DEFERRED
        assert (row["gate_calls"], row["launcher_calls"]) == (1, 0)
        assert not row["attempt_marked"]

    def test_unbound_launcher_is_its_own_arm(self, frozen: dict[str, Any]) -> None:
        row = _tick(frozen, "lattice_a1s0t0g1b0")
        assert row["token"] == "refused_retry"
        assert row["code"] == TICK_LAUNCHER_UNBOUND
        assert not row["attempt_marked"]

    def test_started_marks_the_attempt(self, frozen: dict[str, Any]) -> None:
        row = _tick(frozen, "lattice_a1s0t0g1b1")
        assert row["token"] == "fired"
        assert row["code"] == TICK_FIRE
        assert row["attempt_marked"]
        assert row["launcher_calls"] == 1

    def test_refused_marks_nothing_and_stays_fire_code(self, frozen: dict[str, Any]) -> None:
        row = _tick(frozen, "outcome_split_bound_launcher_refuses")
        assert row["token"] == "refused_retry"
        assert row["code"] == TICK_FIRE
        assert not row["attempt_marked"]
        assert row["launcher_calls"] == 1

    def test_schema_value_law_puns_pass(self, frozen: dict[str, Any]) -> None:
        by_name = {row["name"]: row for row in frozen["state"]}
        for name in ("valid_schema_bool_true_pun_passes", "valid_schema_float_one_passes"):
            assert by_name[name]["verdict"] == STATE_VALID

    def test_every_corrupt_state_loads_disarmed(self, frozen: dict[str, Any]) -> None:
        disarmed = {
            "armed": False,
            "armed_at": None,
            "last_attempt_date": None,
            "last_success_date": None,
        }
        for row in frozen["state"]:
            if row.get("verdict", 1) != STATE_VALID or not row.get("kernel_row"):
                assert row["loaded"] == disarmed, row["name"]

    def test_decode_failures_are_python_rows(self, frozen: dict[str, Any]) -> None:
        names = {row["name"] for row in frozen["state"] if not row.get("kernel_row")}
        assert {"decode_junk_json", "decode_invalid_utf8", "decode_nesting_bomb"} <= names

    def test_rollover_sequences_present(self, frozen: dict[str, Any]) -> None:
        by_name = {row["name"]: row for row in frozen["sequence"]}
        assert by_name["once_per_day_then_rollover_fires_again"]["tokens"] == [
            "fired",
            "already_attempted",
            "fired",
        ]
        assert by_name["success_marks_day_next_day_fires"]["tokens"] == [
            "fired",
            "already_succeeded",
            "fired",
        ]
        assert by_name["failed_run_burns_the_day_retries_next_day"]["tokens"] == [
            "fired",
            "already_attempted",
            "fired",
        ]
        assert by_name["refused_launch_retries_same_day"]["tokens"] == [
            "refused_retry",
            "refused_retry",
            "refused_retry",
        ]
