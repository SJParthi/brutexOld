"""Golden byte-equality for the sweep-scheduler audit payload (DEC-SWEEP-SCHEDULER-001).

The live ``brutex sweep-daemon`` appends :func:`cycle_audit_payload` to its
hash-chained BLAKE3 JSONL audit once per cycle (CLAUDE.md Golden Rule #8). Any
drift in that payload's keys, ordering, or scalar encoding would silently break
the audit replay oracle, so this golden **freezes the exact JSON bytes** for a
hand-picked corpus of cycles — interval + daily, fresh + resume + complete.

Where ``tests/unit/test_engine_sweep_scheduler.py`` asserts the payload *shape*
generatively, this pins the canonical ``json.dumps(..., sort_keys=True)`` bytes
so a change to a key name or a value type is caught byte-for-byte.
"""

from __future__ import annotations

import json

from brutex.engine.sweep_scheduler import (
    IST_OFFSET_SECONDS,
    CheckpointView,
    Schedule,
    cycle_audit_payload,
    decide_resume,
)

# ── The frozen corpus: (cycle_index, fire_at, schedule, view, once) → JSON bytes ──

_INTERVAL = Schedule.parse("24h")
_DAILY = Schedule.parse("daily@02:30", tz_offset_seconds=IST_OFFSET_SECONDS)

_FRESH = CheckpointView(exists=False, sweep_id_matches=False, next_index=0, total_combos=0)
_RESUME = CheckpointView(exists=True, sweep_id_matches=True, next_index=3, total_combos=10)
_COMPLETE = CheckpointView(exists=True, sweep_id_matches=True, next_index=10, total_combos=10)


def _payload_json(
    *,
    cycle_index: int,
    fire_at: float,
    schedule: Schedule,
    view: CheckpointView,
    once: bool,
) -> str:
    return json.dumps(
        cycle_audit_payload(
            cycle_index=cycle_index,
            fire_at=fire_at,
            schedule=schedule,
            resume=decide_resume(view),
            once=once,
        ),
        sort_keys=True,
    )


def test_golden_interval_fresh_cycle() -> None:
    frozen = (
        '{"cycle_index": 0, "fire_at": 1700000000.0, "interval_seconds": 86400, '
        '"once": true, "resume_from_index": 0, '
        '"resume_kind": "fresh", '
        '"resume_reason": "no checkpoint for this grid identity \\u2014 fresh sweep from tuple 0", '
        '"schedule_kind": "interval", "schedule_spec": "24h", "second_of_day": 0, '
        '"should_run": true, "tz_offset_seconds": 0}'
    )
    assert (
        _payload_json(
            cycle_index=0, fire_at=1_700_000_000.0, schedule=_INTERVAL, view=_FRESH, once=True
        )
        == frozen
    )


def test_golden_daily_resume_cycle() -> None:
    frozen = (
        '{"cycle_index": 5, "fire_at": 1700000000.0, "interval_seconds": 0, '
        '"once": false, "resume_from_index": 3, '
        '"resume_kind": "resume", '
        '"resume_reason": "resuming from tuple 3 of 10 (prior cycle was interrupted)", '
        '"schedule_kind": "daily", "schedule_spec": "daily@02:30", "second_of_day": 9000, '
        '"should_run": true, "tz_offset_seconds": 19800}'
    )
    assert (
        _payload_json(
            cycle_index=5, fire_at=1_700_000_000.0, schedule=_DAILY, view=_RESUME, once=False
        )
        == frozen
    )


def test_golden_daily_already_complete_cycle() -> None:
    frozen = (
        '{"cycle_index": 9, "fire_at": 1700000000.0, "interval_seconds": 0, '
        '"once": false, "resume_from_index": 10, '
        '"resume_kind": "already_complete", '
        '"resume_reason": "checkpoint already covers all 10 tuples \\u2014 '
        'nothing to run this cycle", '
        '"schedule_kind": "daily", "schedule_spec": "daily@02:30", "second_of_day": 9000, '
        '"should_run": false, "tz_offset_seconds": 19800}'
    )
    assert (
        _payload_json(
            cycle_index=9, fire_at=1_700_000_000.0, schedule=_DAILY, view=_COMPLETE, once=False
        )
        == frozen
    )
