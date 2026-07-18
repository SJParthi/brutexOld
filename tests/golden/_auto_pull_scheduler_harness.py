"""Shared case-enumeration harness for the auto-pull-scheduler goldens (DEC-RUST-PULL-STATE-001, Wave 3G).

Imported by BOTH ``generate_auto_pull_scheduler_golden.py`` (the one-shot
freeze) and ``test_auto_pull_scheduler_golden.py`` (the recompute-and-compare
gate) — the ``_pull_intent_harness`` shape, never generator-imports-test.

Three tables are computed from the LIVE
``brutex.dashboard.auto_pull_scheduler``:

* the TICK table — the ``tick_once`` decision ladder over the EXHAUSTIVE
  ``2**5`` lattice (armed × succeeded-today × attempted-today × gate-allowed
  × launcher-bound), each row DRIVEN through the real
  :class:`AutoPullScheduler` with a frozen clock + counting gate/launcher
  fakes against a real on-disk :class:`AutoPullStateStore`.  Every row
  records the OBSERVED action token, the gate/launcher CALL COUNTS (the
  laziness law — a disarmed tick must never consult the gate; a deferred
  tick must never call the launcher) and whether the day's attempt was
  marked.  The kernel code is a MECHANICAL projection of those observations
  (token + call counts), never a re-derivation of the ladder.  Outcome-split
  rows (a bound launcher that REFUSES) pin the fired-vs-refused resolution
  that stays Python-side after the kernel's FIRE decision.
* the SEQUENCE table — multi-tick scripts pinning the once-per-day
  STARTED-attempt law, the refused-launch retry law, the failed-run
  retries-next-day law and the IST day-rollover semantics (the kernel is
  clockless — these pin the SEAM's date bookkeeping, so they are
  Python-parity rows only, never TSV/kernel rows).
* the STATE table — the durable-state decode lattice
  (``_state_from_payload`` over enumerated JSON bodies, plus the full
  :meth:`AutoPullStateStore.load` corrupt⇒DISARMED posture).  Each
  decodable row also carries the MECHANICAL kernel-input classification of
  the decoded payload (root/schema/armed/field kind codes — the FFI
  contract) with the verdict code projected from the OBSERVED oracle
  outcome (the raised message, never a hand-written twin of the law).  The
  JSON decode and the read-layer shapes (non-regular inode, oversized,
  invalid UTF-8, nesting bomb) STAY Python — every decode failure is a
  ``kernel_row=False`` row pinning the load outcome only (the intent-harness
  taxonomy-parity posture).

The kernel TSV projects the SAME tick + state rows onto the dependency-free
line format the crate's own ``cargo test`` replays (the cross-language pin).
"""

from __future__ import annotations

import base64
import json
import struct
import tempfile
from datetime import date, datetime
from itertools import product
from pathlib import Path
from typing import Any

from brutex.calendars.auto_pull_gate import AutoGateVerdict
from brutex.calendars.settle_evidence import SETTLE_EVIDENCE_PATH_ENV
from brutex.dashboard.auto_pull_scheduler import (
    AutoLaunchResult,
    AutoPullScheduler,
    AutoPullState,
    AutoPullStateStore,
    _state_from_payload,
)

# ── kernel-input classification codes (the FFI contract; mirrored by the
#    facade constants in brutex/pull_state_rust.py + rust src/auto_pull_scheduler.rs) ──

#: ``schema``-kind codes.  ``MISSING`` covers absent AND JSON ``null`` — the
#: oracle reads both through ``payload.get("schema")`` as ``None``, so the
#: two are indistinguishable BY CONSTRUCTION.  ``BOOL`` / ``INT`` / ``FLOAT``
#: carry the value (CPython's ``==`` is NUMERIC across bool/int/float, so
#: ``True`` and ``1.0`` legitimately pass the ``schema == 1`` law — the
#: value law itself lives in the kernel).  ``INT_HUGE`` is the pure
#: outside-i64 representability fact (can never equal 1).
SCHEMA_MISSING = 0
SCHEMA_BOOL = 1
SCHEMA_INT = 2
SCHEMA_INT_HUGE = 3
SCHEMA_FLOAT = 4
SCHEMA_OTHER = 5

#: Optional-string field-kind codes — the intent-harness vocabulary verbatim
#: (absent ≡ null both load ``None``; a string loads itself; anything else
#: is the corruption the kernel refuses).
KIND_ABSENT = 0
KIND_NULL = 1
KIND_STR = 2
KIND_OTHER = 3

#: State-verdict codes (the kernel output vocabulary).  4/5/6 follow the
#: oracle's FIRST-failing-field order (``armed_at`` → ``last_attempt_date``
#: → ``last_success_date`` — CPython evaluates the constructor args in
#: order, so the first ``_optional_str`` refusal wins).
STATE_VALID = 0
STATE_NOT_OBJECT = 1
STATE_BAD_SCHEMA = 2
STATE_ARMED_NOT_BOOL = 3
STATE_BAD_ARMED_AT = 4
STATE_BAD_LAST_ATTEMPT = 5
STATE_BAD_LAST_SUCCESS = 6

#: Tick-decision codes (the kernel output vocabulary; ladder order).
TICK_DISARMED = 0
TICK_ALREADY_SUCCEEDED = 1
TICK_ALREADY_ATTEMPTED = 2
TICK_DEFERRED = 3
TICK_LAUNCHER_UNBOUND = 4
TICK_FIRE = 5

_I64_MIN = -(2**63)
_I64_MAX = 2**63 - 1

# Monday 2026-07-13 is a real NSE trading day (the parity suite's anchor);
# 18:00 IST is past the conservative 17:30 settle default.
_TODAY = date(2026, 7, 13)
_PRIOR_ISO = "2026-07-12"
_INSTANT = datetime(2026, 7, 13, 18, 0)

#: DEC-SETTLE-EVIDENCE-ADOPTION-001 hermeticity for injected env mappings
#: (the parity suite's ``_NO_EVID`` posture — mappings bypass ``os.environ``,
#: so the conftest belt cannot protect them).
_ENV = {SETTLE_EVIDENCE_PATH_ENV: "off"}


class _CountingGate:
    """A counting gate fake: fixed verdict, records every consult."""

    def __init__(self, *, allowed: bool) -> None:
        self.calls = 0
        self._verdict = (
            AutoGateVerdict(allowed=True, reason="ok", detail="ok")
            if allowed
            else AutoGateVerdict(
                allowed=False,
                reason="before_settle",
                detail="deferred until 17:30 IST (vendor settle)",
            )
        )

    def __call__(self, _instant: datetime) -> AutoGateVerdict:
        self.calls += 1
        return self._verdict


class _CountingLauncher:
    """A counting launcher fake: fixed outcome, records every call."""

    def __init__(self, *, started: bool) -> None:
        self.calls = 0
        self._result = (
            AutoLaunchResult(started=True, detail="spot-only pipeline job started")
            if started
            else AutoLaunchResult(
                started=False, detail="launch refused — the pull single-flight is held"
            )
        )

    def __call__(self) -> AutoLaunchResult:
        self.calls += 1
        return self._result


def _drive_tick(
    tmp: Path,
    *,
    armed: bool,
    succeeded_today: bool,
    attempted_today: bool,
    gate_allowed: bool,
    launcher_bound: bool,
    launcher_started: bool = True,
    none_dates: bool = False,
) -> dict[str, Any]:
    """Drive ONE real tick over a fresh store; return the observation row."""
    store = AutoPullStateStore(tmp)
    iso = _TODAY.isoformat()
    prior: str | None = None if none_dates else _PRIOR_ISO
    store.save(
        AutoPullState(
            armed=armed,
            armed_at="2026-07-01T10:00:00" if armed else None,
            last_attempt_date=iso if attempted_today else prior,
            last_success_date=iso if succeeded_today else prior,
        )
    )
    gate = _CountingGate(allowed=gate_allowed)
    launcher = _CountingLauncher(started=launcher_started) if launcher_bound else None
    scheduler = AutoPullScheduler(
        store, launcher=launcher, gate=gate, tick_seconds=3600.0, env=dict(_ENV)
    )
    token = scheduler.tick_once(now=_INSTANT)
    after = AutoPullStateStore(tmp).load()
    launcher_calls = launcher.calls if launcher is not None else 0
    return {
        "token": token,
        "gate_calls": gate.calls,
        "launcher_calls": launcher_calls,
        "attempt_marked": (not attempted_today) and after.last_attempt_date == iso,
        "code": _project_tick_code(token, launcher_bound=launcher_bound),
    }


def _project_tick_code(token: str, *, launcher_bound: bool) -> int:
    """MECHANICAL projection of the observed token onto the kernel vocabulary.

    ``refused_retry`` splits on the OBSERVED wiring fact: an unbound launcher
    is the kernel's ``LAUNCHER_UNBOUND`` arm; a bound-but-refusing launcher is
    the kernel's ``FIRE`` arm whose refusal resolves Python-side AFTER the
    launcher call (the design's fired-vs-refused split).
    """
    if token == "disarmed":
        return TICK_DISARMED
    if token == "already_succeeded":
        return TICK_ALREADY_SUCCEEDED
    if token == "already_attempted":
        return TICK_ALREADY_ATTEMPTED
    if token.startswith("deferred:"):
        return TICK_DEFERRED
    if token == "refused_retry" and not launcher_bound:
        return TICK_LAUNCHER_UNBOUND
    if token in ("fired", "refused_retry"):
        return TICK_FIRE
    raise AssertionError(f"unknown tick token {token!r}")


def _tick_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory() as tmpdir:
        base = Path(tmpdir)
        n = 0
        for armed, succ, att, gate_allowed, bound in product((False, True), repeat=5):
            tmp = base / f"lattice-{n}"
            tmp.mkdir()
            n += 1
            row = _drive_tick(
                tmp,
                armed=armed,
                succeeded_today=succ,
                attempted_today=att,
                gate_allowed=gate_allowed,
                launcher_bound=bound,
            )
            rows.append(
                {
                    "name": f"lattice_a{int(armed)}s{int(succ)}t{int(att)}g{int(gate_allowed)}b{int(bound)}",
                    "armed": armed,
                    "succeeded_today": succ,
                    "attempted_today": att,
                    "gate_allowed": gate_allowed,
                    "launcher_bound": bound,
                    **row,
                }
            )
        # Outcome-split extra: a BOUND launcher that REFUSES on the reachable
        # fire row — the kernel decision stays FIRE; Python resolves the
        # refusal (no attempt marked, retried by later ticks).
        tmp = base / "refused"
        tmp.mkdir()
        row = _drive_tick(
            tmp,
            armed=True,
            succeeded_today=False,
            attempted_today=False,
            gate_allowed=True,
            launcher_bound=True,
            launcher_started=False,
        )
        rows.append(
            {
                "name": "outcome_split_bound_launcher_refuses",
                "armed": True,
                "succeeded_today": False,
                "attempted_today": False,
                "gate_allowed": True,
                "launcher_bound": True,
                **row,
            }
        )
        # Never-armed store shape: the date slots are ``None`` (not merely a
        # prior day) — the ``succeeded_today``/``attempted_today`` bools the
        # kernel sees are the same ``False``.
        tmp = base / "none-dates"
        tmp.mkdir()
        row = _drive_tick(
            tmp,
            armed=True,
            succeeded_today=False,
            attempted_today=False,
            gate_allowed=True,
            launcher_bound=True,
            none_dates=True,
        )
        rows.append(
            {
                "name": "none_dates_fresh_arm_fires",
                "armed": True,
                "succeeded_today": False,
                "attempted_today": False,
                "gate_allowed": True,
                "launcher_bound": True,
                **row,
            }
        )
    return rows


def _sequence_rows() -> list[dict[str, Any]]:
    """Multi-tick scripts — the once-per-day / rollover / retry laws.

    Python-parity rows only (the kernel is clockless): each script drives the
    REAL scheduler through dated ticks + completion hooks and records the
    token stream + the durable date marks.
    """
    day2 = datetime(2026, 7, 14, 18, 0)  # Tuesday — the next trading day
    rows: list[dict[str, Any]] = []

    def run(name: str, script: list[tuple[str, Any]], *, started: bool = True) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            store = AutoPullStateStore(tmp)
            gate = _CountingGate(allowed=True)
            launcher = _CountingLauncher(started=started)
            scheduler = AutoPullScheduler(
                store, launcher=launcher, gate=gate, tick_seconds=3600.0, env=dict(_ENV)
            )
            scheduler.arm(now=_INSTANT)
            scheduler.stop()  # the ticker thread is irrelevant to the script
            tokens: list[str] = []
            for op, arg in script:
                if op == "tick":
                    tokens.append(scheduler.tick_once(now=arg))
                elif op == "complete_ok":
                    scheduler.note_run_complete(ok=True, run_date=arg, source="script")
                elif op == "complete_failed":
                    scheduler.note_run_complete(ok=False, run_date=arg, source="script")
                else:  # pragma: no cover - harness misuse guard
                    raise AssertionError(f"unknown op {op!r}")
            after = AutoPullStateStore(tmp).load()
            rows.append(
                {
                    "name": name,
                    "tokens": tokens,
                    "last_attempt_date": after.last_attempt_date,
                    "last_success_date": after.last_success_date,
                    "launcher_calls": launcher.calls,
                }
            )

    run(
        "once_per_day_then_rollover_fires_again",
        [("tick", _INSTANT), ("tick", datetime(2026, 7, 13, 19, 0)), ("tick", day2)],
    )
    run(
        "success_marks_day_next_day_fires",
        [
            ("tick", _INSTANT),
            ("complete_ok", _TODAY),
            ("tick", datetime(2026, 7, 13, 19, 0)),
            ("tick", day2),
        ],
    )
    run(
        "failed_run_burns_the_day_retries_next_day",
        [
            ("tick", _INSTANT),
            ("complete_failed", _TODAY),
            ("tick", datetime(2026, 7, 13, 19, 0)),
            ("tick", day2),
        ],
    )
    run(
        "refused_launch_retries_same_day",
        [
            ("tick", _INSTANT),
            ("tick", datetime(2026, 7, 13, 18, 1)),
            ("tick", datetime(2026, 7, 13, 18, 2)),
        ],
        started=False,
    )
    return rows


# ── the STATE decode corpus ──────────────────────────────────────────────────

#: name → raw body bytes.  Decodable rows exercise the shape lattice;
#: non-decodable rows pin the decode-stays-Python fold (kernel_row=False).
_STATE_BODIES: dict[str, bytes] = {
    "valid_minimal_disarmed": b'{"schema": 1, "armed": false}',
    "valid_full_armed": (
        b'{"schema": 1, "armed": true, "armed_at": "2026-07-13T18:00:00",'
        b' "last_attempt_date": "2026-07-13", "last_success_date": "2026-07-12"}'
    ),
    "valid_null_optionals": (
        b'{"schema": 1, "armed": true, "armed_at": null,'
        b' "last_attempt_date": null, "last_success_date": null}'
    ),
    # CPython's ``==`` is numeric across bool/int/float — both PASS the
    # ``schema == 1`` law (the oracle's semantics, frozen here).
    "valid_schema_bool_true_pun_passes": b'{"schema": true, "armed": false}',
    "valid_schema_float_one_passes": b'{"schema": 1.0, "armed": false}',
    "bad_schema_absent": b'{"armed": true}',
    "bad_schema_null": b'{"schema": null, "armed": true}',
    "bad_schema_two": b'{"schema": 2, "armed": true}',
    "bad_schema_bool_false": b'{"schema": false, "armed": true}',
    "bad_schema_str_one": b'{"schema": "1", "armed": true}',
    "bad_schema_float_half": b'{"schema": 0.5, "armed": true}',
    "bad_schema_nan": b'{"schema": NaN, "armed": true}',
    "bad_schema_huge_pos": b'{"schema": 1180591620717411303424, "armed": true}',
    "bad_schema_huge_neg": b'{"schema": -1180591620717411303424, "armed": true}',
    "bad_schema_list": b'{"schema": [1], "armed": true}',
    "bad_armed_absent": b'{"schema": 1}',
    "bad_armed_null": b'{"schema": 1, "armed": null}',
    "bad_armed_int_one": b'{"schema": 1, "armed": 1}',
    "bad_armed_str": b'{"schema": 1, "armed": "true"}',
    "bad_armed_at_int": b'{"schema": 1, "armed": true, "armed_at": 7}',
    "bad_last_attempt_list": b'{"schema": 1, "armed": true, "last_attempt_date": []}',
    "bad_last_success_obj": b'{"schema": 1, "armed": false, "last_success_date": {}}',
    # ORDER law: multiple violations — the FIRST failing check names itself.
    "order_schema_beats_armed": b'{"schema": 2, "armed": 7}',
    "order_armed_beats_fields": b'{"schema": 1, "armed": 7, "armed_at": 9}',
    "order_armed_at_beats_later_fields": (
        b'{"schema": 1, "armed": true, "armed_at": 9, "last_success_date": 9}'
    ),
    "not_object_list": b"[1, 2]",
    "not_object_str": b'"armed"',
    "not_object_int": b"3",
    "not_object_null": b"null",
    # Decode failures — the kernel is NEVER consulted (kernel_row=False).
    "decode_junk_json": b"{not json",
    "decode_invalid_utf8": b'{"schema": 1, "armed": \xff}',
    "decode_nesting_bomb": b"[" * 200_000 + b"]" * 200_000,
}


def _classify_state_payload(payload: object) -> dict[str, Any]:
    """The MECHANICAL kernel-input classification (the future seam's twin)."""
    if not isinstance(payload, dict):
        return {
            "root_mapping": False,
            "schema_kind": SCHEMA_MISSING,
            "schema_i64": 0,
            "schema_f64_bits": 0,
            "armed_is_bool": False,
            "armed_at_kind": KIND_ABSENT,
            "last_attempt_kind": KIND_ABSENT,
            "last_success_kind": KIND_ABSENT,
        }
    schema = payload.get("schema")
    schema_kind = SCHEMA_OTHER
    schema_i64 = 0
    schema_f64_bits = 0
    if schema is None:
        schema_kind = SCHEMA_MISSING
    elif isinstance(schema, bool):
        schema_kind = SCHEMA_BOOL
        schema_i64 = int(schema)
    elif isinstance(schema, int):
        if _I64_MIN <= schema <= _I64_MAX:
            schema_kind = SCHEMA_INT
            schema_i64 = schema
        else:
            schema_kind = SCHEMA_INT_HUGE
    elif isinstance(schema, float):
        schema_kind = SCHEMA_FLOAT
        schema_f64_bits = struct.unpack("<Q", struct.pack("<d", schema))[0]

    def field_kind(key: str) -> int:
        value = payload.get(key)
        if key not in payload:
            return KIND_ABSENT
        if value is None:
            return KIND_NULL
        if isinstance(value, str):
            return KIND_STR
        return KIND_OTHER

    return {
        "root_mapping": True,
        "schema_kind": schema_kind,
        "schema_i64": schema_i64,
        "schema_f64_bits": schema_f64_bits,
        "armed_is_bool": isinstance(payload.get("armed"), bool),
        "armed_at_kind": field_kind("armed_at"),
        "last_attempt_kind": field_kind("last_attempt_date"),
        "last_success_kind": field_kind("last_success_date"),
    }


def _project_state_verdict(error: str | None) -> int:
    """MECHANICAL projection of the oracle's raised message onto the codes."""
    if error is None:
        return STATE_VALID
    if error == "not a JSON object":
        return STATE_NOT_OBJECT
    if error.startswith("unknown schema "):
        return STATE_BAD_SCHEMA
    if error == "armed is not a bool":
        return STATE_ARMED_NOT_BOOL
    if error == "armed_at is not a string":
        return STATE_BAD_ARMED_AT
    if error == "last_attempt_date is not a string":
        return STATE_BAD_LAST_ATTEMPT
    if error == "last_success_date is not a string":
        return STATE_BAD_LAST_SUCCESS
    raise AssertionError(f"unmapped oracle refusal {error!r}")


def _state_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory() as tmpdir:
        base = Path(tmpdir)
        for n, (name, body) in enumerate(sorted(_STATE_BODIES.items())):
            tmp = base / f"state-{n}"
            tmp.mkdir()
            store = AutoPullStateStore(tmp)
            store.path.write_bytes(body)
            loaded = store.load()
            row: dict[str, Any] = {
                "name": name,
                "body_b64": base64.b64encode(body).decode("ascii"),
                "loaded": {
                    "armed": loaded.armed,
                    "armed_at": loaded.armed_at,
                    "last_attempt_date": loaded.last_attempt_date,
                    "last_success_date": loaded.last_success_date,
                },
            }
            try:
                payload: object = json.loads(body.decode("utf-8"))
            except (ValueError, RecursionError, OverflowError):
                row["kernel_row"] = False
                rows.append(row)
                continue
            error: str | None
            try:
                _state_from_payload(payload)
                error = None
            except ValueError as exc:
                error = str(exc)
            row["kernel_row"] = True
            row["error"] = error
            row["verdict"] = _project_state_verdict(error)
            row["classify"] = _classify_state_payload(payload)
            rows.append(row)
    return rows


def compute_payload() -> dict[str, Any]:
    """The canonical golden payload — recomputed from the LIVE module."""
    return {
        "tick": _tick_rows(),
        "sequence": _sequence_rows(),
        "state": _state_rows(),
    }


def compute_tsv(payload: dict[str, Any]) -> str:
    """The serde-free kernel-case projection for the cargo fixture test."""
    lines = [
        "# generated by tests/golden/generate_auto_pull_scheduler_golden.py — do not hand-edit"
    ]
    for row in payload["tick"]:
        lines.append(
            "\t".join(
                (
                    "tick",
                    row["name"],
                    str(int(row["armed"])),
                    str(int(row["succeeded_today"])),
                    str(int(row["attempted_today"])),
                    str(int(row["gate_allowed"])),
                    str(int(row["launcher_bound"])),
                    str(row["code"]),
                )
            )
        )
    for row in payload["state"]:
        if not row.get("kernel_row"):
            continue
        c = row["classify"]
        lines.append(
            "\t".join(
                (
                    "state",
                    row["name"],
                    str(int(c["root_mapping"])),
                    str(c["schema_kind"]),
                    str(c["schema_i64"]),
                    f"{c['schema_f64_bits']:016x}",
                    str(int(c["armed_is_bool"])),
                    str(c["armed_at_kind"]),
                    str(c["last_attempt_kind"]),
                    str(c["last_success_kind"]),
                    str(row["verdict"]),
                )
            )
        )
    return "\n".join(lines) + "\n"
