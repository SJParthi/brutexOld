#!/usr/bin/env python3
# ruff: noqa: S101, S106, S108, T201 — dev-tooling generator: asserts + prints are
# the interface, "/tmp/x.json" is the pinned message literal (never opened), and
# the S106 hits are bandit mistaking `earliest_all_pass_ist=` kwargs for passwords.
"""Golden-fixture generator for the brutex-calendar Rust crate (DEC-RUST-CALENDAR-LEAVES-001).

Runs the REAL Python decision laws (`brutex.calendars.settle_evidence` +
`brutex.calendars.observed_sessions` + CPython 3.12's C-accelerated
``datetime.fromisoformat``) over a hostile battery and records the exact
outcomes — including every byte of every refusal message — so the Rust port's
``cargo test`` can assert BYTE-PARITY without any Python at test time.

Run from the repo root with the project venv's Python 3.12:

    .venv/bin/python rust/brutex-calendar/tests/goldens/generate_goldens.py

Regenerate (and review the diff) whenever the Python laws change; the Rust
crate must move in the SAME commit (the wheel-lockstep discipline).
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[3]
sys.path.insert(0, str(REPO))

from brutex.calendars.observed_sessions import (  # noqa: E402
    OVERLAY_LOAD_FAILED_EVENT,
    DayEvidence,
    classify_month_drift,
    load_observed_holidays,
    overlay_path,
)
from brutex.calendars.settle_evidence import validate_evidence_payload  # noqa: E402
from structlog.testing import capture_logs  # noqa: E402

assert sys.version_info[:2] == (3, 12), "goldens must come from the CPython 3.12 baseline"


# ── settle_evidence.validate_evidence_payload goldens ────────────────────────


def _base() -> dict:
    return {
        "schema": 1,
        "kind": "groww-postclose-settle-evidence",
        "earliest_all_pass_ist": "15:45",
        "adopted_settle_ist": "16:00",
        "margin_minutes": 15,
        "distinct_trading_days": 5,
        "per_day": {
            d: {"15:45": {"records": 14, "all_pass": True, "failed_checks": []}}
            for d in ["2026-07-06", "2026-07-07", "2026-07-08", "2026-07-09", "2026-07-10"]
        },
        "generated_at_utc": "2026-07-11T05:00:00+00:00",
        "probe_script_version": "postclose-settle-probe/1",
    }


def _mut(**kw) -> dict:
    p = _base()
    for k, v in kw.items():
        if v is ...:
            p.pop(k, None)
        else:
            p[k] = v
    return p


def _perday(extra_day: str, slots) -> dict:
    p = _base()
    p["per_day"][extra_day] = slots
    return p


CELL = {"records": 1, "all_pass": True, "failed_checks": []}

settle_cases: list[tuple[str, str]] = []  # (name, payload_json)


def s_add(name: str, payload) -> None:
    settle_cases.append((name, json.dumps(payload)))


def s_add_raw(name: str, payload_json: str) -> None:
    settle_cases.append((name, payload_json))


s_add("valid_base", _base())
s_add(
    "valid_2359",
    _mut(
        earliest_all_pass_ist="23:49",
        margin_minutes=10,
        adopted_settle_ist="23:59",
        per_day={
            d: {"23:49": dict(CELL)}
            for d in ["2026-07-06", "2026-07-07", "2026-07-08", "2026-07-09", "2026-07-10"]
        },
    ),
)
s_add("root_list", [])
s_add("root_str", "x")
s_add("root_int", 3)
s_add("root_float", 1.5)
s_add("root_none", None)
s_add("root_bool", True)
s_add("schema_missing", _mut(schema=...))
s_add("schema_true", _mut(schema=True))
s_add("schema_false", _mut(schema=False))
s_add("schema_float", _mut(schema=1.0))
s_add("schema_two", _mut(schema=2))
s_add("schema_str", _mut(schema="1"))
s_add("schema_huge", _mut(schema=10**30))
s_add("schema_neg", _mut(schema=-1))
s_add_raw("schema_neg_zero", json.dumps(_mut(schema=123)).replace('"schema": 123', '"schema": -0'))
s_add("kind_missing", _mut(kind=...))
s_add("kind_wrong", _mut(kind="groww-postclose"))
s_add("kind_int", _mut(kind=7))
s_add("kind_none", _mut(kind=None))
s_add("kind_list", _mut(kind=["a"]))
s_add("kind_ctrl", _mut(kind="a\x01b\tc\nd\\e"))
s_add("kind_quote_s", _mut(kind="it's"))
s_add("kind_quote_d", _mut(kind='say "hi"'))
s_add("kind_quote_both", _mut(kind="both ' and \""))
s_add("kind_nonascii", _mut(kind="café ☃"))
s_add("kind_nbsp", _mut(kind="a b"))
s_add("kind_zwsp", _mut(kind="a\u200bb"))
s_add("kind_del", _mut(kind="a\x7fb"))
s_add("kind_float_sci", _mut(kind=1.5e300))
s_add("kind_float_small", _mut(kind=1e-05))
s_add("kind_float_e16", _mut(kind=1e16))
s_add("kind_float_e15", _mut(kind=1e15))
s_add("kind_float_neg0", _mut(kind=-0.0))
s_add_raw("kind_float_1e400", json.dumps(_mut(kind=123)).replace('"kind": 123', '"kind": 1e400'))
s_add("kind_nested", _mut(kind={"k": [True, None, 1.0, "x"], "m": {}}))
s_add("earliest_missing", _mut(earliest_all_pass_ist=...))
s_add("earliest_int", _mut(earliest_all_pass_ist=1545))
s_add("earliest_newline", _mut(earliest_all_pass_ist="15:45\n"))
s_add("earliest_short", _mut(earliest_all_pass_ist="5:45"))
s_add("earliest_long", _mut(earliest_all_pass_ist="015:45"))
s_add("earliest_unicode_digit", _mut(earliest_all_pass_ist="١٥:45"))
s_add("earliest_unreal", _mut(earliest_all_pass_ist="25:00"))
s_add("earliest_min60", _mut(earliest_all_pass_ist="15:60"))
s_add("adopted_missing", _mut(adopted_settle_ist=...))
s_add("adopted_float", _mut(adopted_settle_ist=16.0))
s_add("adopted_unreal", _mut(adopted_settle_ist="99:99"))
s_add("margin_missing", _mut(margin_minutes=...))
s_add("margin_bool", _mut(margin_minutes=True))
s_add("margin_float", _mut(margin_minutes=15.0))
s_add("margin_str", _mut(margin_minutes="15"))
s_add("margin_9", _mut(margin_minutes=9, adopted_settle_ist="15:54"))
s_add("margin_10_valid", _mut(margin_minutes=10, adopted_settle_ist="15:55"))
s_add("margin_241", _mut(margin_minutes=241, adopted_settle_ist="19:46"))
s_add("margin_240_valid", _mut(margin_minutes=240, adopted_settle_ist="19:45"))
s_add("margin_huge", _mut(margin_minutes=10**30))
s_add("margin_neg_huge", _mut(margin_minutes=-(10**30)))
s_add(
    "wrap_1440", _mut(earliest_all_pass_ist="23:50", margin_minutes=10, adopted_settle_ist="00:00")
)
s_add(
    "wrap_past", _mut(earliest_all_pass_ist="23:45", margin_minutes=30, adopted_settle_ist="00:15")
)
s_add("arith", _mut(adopted_settle_ist="16:01"))
s_add("floor_1500", _mut(earliest_all_pass_ist="15:00", adopted_settle_ist="15:15"))
s_add(
    "floor_1534", _mut(earliest_all_pass_ist="15:20", margin_minutes=14, adopted_settle_ist="15:34")
)
s_add(
    "floor_1535_valid",
    _mut(
        earliest_all_pass_ist="15:20",
        margin_minutes=15,
        adopted_settle_ist="15:35",
        per_day={
            d: {"15:20": dict(CELL)}
            for d in ["2026-07-06", "2026-07-07", "2026-07-08", "2026-07-09", "2026-07-10"]
        },
    ),
)
s_add("days_bool", _mut(distinct_trading_days=False))
s_add("days_4", _mut(distinct_trading_days=4))
s_add("days_str", _mut(distinct_trading_days="5"))
s_add("days_huge", _mut(distinct_trading_days=10**30))
s_add("perday_missing", _mut(per_day=...))
s_add("perday_list", _mut(per_day=[1]))
s_add("perday_str", _mut(per_day="x"))
s_add("perday_empty", _mut(per_day={}))
s_add("perday_key_shape", _perday("20260711", {"15:45": dict(CELL)}))
s_add("perday_key_unreal", _perday("2026-02-30", {"15:45": dict(CELL)}))
s_add("perday_key_year0", _perday("0000-01-01", {"15:45": dict(CELL)}))
s_add("perday_key_month0", _perday("2026-00-11", {"15:45": dict(CELL)}))
s_add("perday_key_day0", _perday("2026-07-00", {"15:45": dict(CELL)}))
s_add("perday_key_leap_ok_support", _perday("2024-02-29", {"15:45": dict(CELL)}))
s_add("perday_day_empty", _perday("2026-07-11", {}))
s_add("perday_day_list", _perday("2026-07-11", ["x"]))
s_add("perday_slot_shape", _perday("2026-07-11", {"1545": dict(CELL)}))
s_add("perday_slot_9999_accepted", _perday("2026-07-11", {"99:99": dict(CELL)}))
s_add("perday_cell_str", _perday("2026-07-11", {"15:45": "x"}))
s_add(
    "perday_records_0",
    _perday("2026-07-11", {"15:45": {"records": 0, "all_pass": True, "failed_checks": []}}),
)
s_add(
    "perday_records_missing",
    _perday("2026-07-11", {"15:45": {"all_pass": True, "failed_checks": []}}),
)
s_add(
    "perday_records_bool",
    _perday("2026-07-11", {"15:45": {"records": True, "all_pass": True, "failed_checks": []}}),
)
s_add(
    "perday_allpass_int",
    _perday("2026-07-11", {"15:45": {"records": 3, "all_pass": 1, "failed_checks": []}}),
)
s_add(
    "perday_allpass_missing", _perday("2026-07-11", {"15:45": {"records": 3, "failed_checks": []}})
)
s_add(
    "perday_failedchecks_mixed",
    _perday("2026-07-11", {"15:45": {"records": 3, "all_pass": True, "failed_checks": ["a", 2]}}),
)
s_add(
    "perday_failedchecks_missing",
    _perday("2026-07-11", {"15:45": {"records": 3, "all_pass": True}}),
)
s_add("support_low", _mut(distinct_trading_days=6))
s_add(
    "support_extra_day_false",
    _perday("2026-07-11", {"15:45": {"records": 1, "all_pass": False, "failed_checks": ["x"]}}),
)
s_add("support_extra_day_true", _perday("2026-07-11", {"15:45": dict(CELL)}))
s_add("gen_missing", _mut(generated_at_utc=...))
s_add("gen_int", _mut(generated_at_utc=5))
s_add("gen_junk", _mut(generated_at_utc="yesterday"))
s_add("gen_naive", _mut(generated_at_utc="2026-07-11T05:00:00"))
s_add("gen_naive_dateonly", _mut(generated_at_utc="2026-07-11"))
s_add("gen_ist", _mut(generated_at_utc="2026-07-11T05:00:00+05:30"))
s_add("gen_neg_offset", _mut(generated_at_utc="2026-07-11T05:00:00-05:30"))
s_add("gen_z_valid", _mut(generated_at_utc="2026-07-11T05:00:00Z"))
s_add("gen_lower_z", _mut(generated_at_utc="2026-07-11T05:00:00z"))
s_add("gen_minus0000_valid", _mut(generated_at_utc="2026-07-11T05:00:00-00:00"))
s_add("gen_secoffset", _mut(generated_at_utc="2026-07-11T05:00:00+00:00:01"))
s_add("gen_usoffset_utc_quirk", _mut(generated_at_utc="2026-07-11T05:00:00+00:00:00.000001"))
s_add("gen_frac_offset", _mut(generated_at_utc="2026-07-11T05:00:00+05:30:15.123456"))
s_add("gen_newline_sep_valid", _mut(generated_at_utc="2026-07-11\n05:00:00+00:00"))
s_add("gen_compact_valid", _mut(generated_at_utc="20260711T050000Z"))
s_add("gen_week_valid", _mut(generated_at_utc="2026-W28-6T05:00:00Z"))
s_add("gen_offset_min75", _mut(generated_at_utc="2026-07-11T05:00:00+05:75"))
s_add("psv_missing", _mut(probe_script_version=...))
s_add("psv_empty", _mut(probe_script_version=""))
s_add("psv_int", _mut(probe_script_version=3))
s_add("checks_bad", _mut(checks=["a", 1]))
s_add("checks_str", _mut(checks="a"))
s_add("checks_none", _mut(checks=None))
s_add("checks_ok_valid", _mut(checks=["a", "b"]))
s_add("slots_observed_bad", _mut(slots_observed=[1.5]))
s_add("transcripts_bad", _mut(transcripts=[None]))
s_add("excluded_bad_val", _mut(excluded_days={"2026-01-01": 5}))
s_add("excluded_list", _mut(excluded_days=["x"]))
s_add("excluded_nested", _mut(excluded_days={"a": {"b": [1, "x", None, True]}}))
s_add("excluded_empty_valid", _mut(excluded_days={}))
s_add("excluded_ok_valid", _mut(excluded_days={"2026-01-01": "holiday"}))
s_add("ignored_neg", _mut(ignored_records=-1))
s_add("ignored_float", _mut(ignored_records=0.0))
s_add("ignored_ok_valid", _mut(ignored_records=0))

settle_out = []
for name, payload_json in settle_cases:
    payload = json.loads(payload_json)
    try:
        ev = validate_evidence_payload(payload, path="/tmp/x.json")
        settle_out.append(
            {
                "name": name,
                "payload_json": payload_json,
                "ok": True,
                "adopted": f"{ev.adopted:%H:%M}",
                "generated": ev.generated_at_utc,
            }
        )
    except ValueError as exc:
        settle_out.append(
            {"name": name, "payload_json": payload_json, "ok": False, "msg": str(exc)}
        )

# The validator's `_fail` uses `{path!r}` — every golden above ran with
# path="/tmp/x.json", so the messages open with "settle evidence '/tmp/x.json': ".
(HERE / "settle_goldens.json").write_text(
    json.dumps(
        {"path_repr": repr("/tmp/x.json"), "cases": settle_out}, indent=1, ensure_ascii=False
    )
    + "\n",
    encoding="utf-8",
)


# ── datetime.fromisoformat verdict goldens (the generated_at_utc law) ────────

iso_cases = [
    "2026-01-01T12:00:00+00:00",
    "2026-01-01 12:00:00+00:00",
    "2026-01-01x12:00:00+00:00",
    "2026-01-01T12:00:00Z",
    "2026-01-01T12:00:00z",
    "2026-01-01T12:00:00-00:00",
    "2026-01-01T12:00:00+05:30",
    "2026-01-01T12:00:00-05:30",
    "2026-01-01T12:00:00",
    "2026-01-01",
    "2026-01-01T12:00:00.5+00:00",
    "2026-01-01T12:00:00,5+00:00",
    "2026-01-01T12:00:00.1234567890+00:00",
    "2026-01-01T12:00:00.+00:00",
    "20260101T120000Z",
    "20260101",
    "2026-01-01T1200Z",
    "2026-01-01T12Z",
    "20260101T12:00:00Z",
    "2026-01-01T120000Z",
    "2026-W01-4T12:00:00Z",
    "2026-W01T12:00:00Z",
    "2026W014T12:00:00Z",
    "2026W01T12:00:00Z",
    "2026-W01-4",
    "2026-W01",
    "2026-W53-1T00:00Z",
    "2026-W54-1T00:00Z",
    "2020-W53-5T00:00Z",
    "2021-W53-1T00:00Z",
    "2026-W00-1T00:00Z",
    "2026-W01-0T00:00Z",
    "2026-W01-8T00:00Z",
    "2026-01-01T12:00:00+0530",
    "2026-01-01T12:00:00+05",
    "2026-01-01T12:00:00+05:30:15",
    "2026-01-01T12:00:00+05:30:15.123456",
    "2026-01-01T12:00:00+23:59",
    "2026-01-01T12:00:00+24:00",
    "2026-01-01T12:00:00-24:00",
    "2026-01-01T12:00:00+05:75",
    "2026-01-01T12:00:00+0000",
    "2026-01-01T12:00:00+00",
    "2026-01-01T12:00:00+00:00:00",
    "2026-01-01T12:00:00+00:00:00.000001",
    "2026-01-01T12:00:00-00:00:00.5",
    "2026-01-01T12:00:00+00:00:01",
    "2026-01-01T24:00:00Z",
    "2026-01-01T23:60:00Z",
    "2026-01-01T23:00:60Z",
    "2026-01-01T",
    "2026-01-01TZ",
    "T12:00:00Z",
    "2026-01-01T12:0:00Z",
    "2026-1-01T12:00:00Z",
    "2026-01-1T12:00:00Z",
    "0000-01-01T12:00:00Z",
    "0001-01-01T00:00:00Z",
    "9999-12-31T23:59:59Z",
    "2026-02-30T00:00:00Z",
    "2024-02-29T00:00:00Z",
    "2023-02-29T00:00:00Z",
    "2026-01-01T12:00:00.123456789012345678901234567890Z",
    "2026-01-01T12:00Z",
    "2026-01-01T12:00:00.5Z",
    "2026-0101T12:00Z",
    "202601-01T12:00Z",
    "2026-01-01T12:00:00 +00:00",
    "2026-01-01  12:00:00+00:00",
    "2026-01-01\n12:00:00+00:00",
    "2026-01-01T12:00:00+00:00\n",
    " 2026-01-01T12:00:00+00:00",
    "2026-01-01T12.5Z",
    "2026-01-01T12:00.5Z",
    "2026-01-01T1Z",
    "2026-01-01T123Z",
    "2026-01-01T12345Z",
    "2026-01-01T1234567Z",
    "2026-01-01T12345678Z",
    "2026-01-01T12:00:00.Z",
    "2026-01-01T12:00:00,Z",
    "2026-01-01T٠٢:00:00Z",
    "2026-01-01T12:00:00+٠٥:30",
    "2026-01-01T12:00:00+00:00x",
    "2026-01-01T12:00:00Zx",
    "2026-01-01T12:00:00ZZ",
    "20260101T",
    "2026-W1-4T12:00Z",
    "2026-W014T12:00Z",
    "2026W01-4T12:00Z",
    "2026-01T12:00Z",
    "202601T12:00Z",
    "2026-01-01T-12:00Z",
    "2026-01-01+00:00",
    "2026-01-0112:00Z",
    "12026-01-01T12:00Z",
    "999-01-01T12:00Z",
    "2026-01-01☃12:00:00+00:00",
    "2026-01-01T12:00:00:5Z",
    "2026-01-01T12:00:00:123456+00:00",
    "2026-01-01T12:00:00Z\x00x",
    "2026-01-01T12:00\x00:00+00:00",
    "2026-01-01T12:00:00+05:30\x00",
    "2026-W01-0000",
    "2026-W01-0000Z",
    "2026-01-01T00:00:00+23:59:59.999999",
    "2026-01-01T00:00:00-23:59:59.999999",
    "2026-01-01T12:00:00.5 +00:00",
    "2026-01-01T12:00:00.5x+00:00",
    "2026-01-01T1200 Z",
    "2026-01-01T12 Z",
    "2026-01-01T12:00:000Z",
    "2026-01-01T12:0000Z",
    "2026-01-01T1200:00Z",
    "2026-01-01T12::00Z",
    "2026-01-01t12:00:00Z",
    "2026-12-31T12:00:00Z",
    "2026-00-01T12:00:00Z",
    "2026-01-00T12:00:00Z",
    "2026-13-01T12:00:00Z",
    "2026-01-32T12:00:00Z",
    "9999-W52-5T00:00Z",
    "0001-W01-1T00:00Z",
]

iso_out = []
for s in iso_cases:
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        iso_out.append({"s": s, "verdict": "invalid"})
        continue
    off = dt.utcoffset()
    if off is None:
        iso_out.append({"s": s, "verdict": "naive"})
    elif off == timedelta(0):
        iso_out.append({"s": s, "verdict": "utc"})
    else:
        iso_out.append({"s": s, "verdict": "offset", "offset_str": str(off)})

(HERE / "iso_goldens.json").write_text(
    json.dumps({"cases": iso_out}, indent=1, ensure_ascii=False) + "\n", encoding="utf-8"
)


# ── observed_sessions loader goldens (whole-overlay accept/reject) ───────────


def _entry(d: str, label: str = "observed-not-official", **extra):
    e = {
        "date": d,
        "detected_at": "2026-07-04T10:00:00+00:00",
        "month": d[:7],
        "label": label,
        "evidence": {
            "zero_bar_indices": [],
            "clean_success_windows": 1,
            "failed_windows_touching": 0,
        },
    }
    e.update(extra)
    return e


observed_cases: list[tuple[str, str]] = []


def o_add(name: str, payload) -> None:
    observed_cases.append((name, json.dumps(payload)))


def o_add_raw(name: str, payload_json: str) -> None:
    observed_cases.append((name, payload_json))


o_add("valid_two", {"version": 1, "entries": [_entry("2026-01-13"), _entry("2026-05-13")]})
o_add("valid_empty", {"version": 1, "entries": []})
o_add("valid_dup_collapse", {"version": 1, "entries": [_entry("2026-01-13"), _entry("2026-01-13")]})
o_add(
    "valid_extra_keys",
    {
        "version": 1,
        "junk": {"nested": [1, 2]},
        "entries": [_entry("2026-01-13", extra_field="kept")],
    },
)
o_add_raw(
    "valid_version_true_punning",
    '{"version": true, "entries": [' + json.dumps(_entry("2026-01-13")) + "]}",
)
o_add_raw(
    "valid_version_float_punning",
    '{"version": 1.0, "entries": [' + json.dumps(_entry("2026-01-13")) + "]}",
)
o_add_raw(
    "reject_version_1e0_check",
    '{"version": 1e0, "entries": [' + json.dumps(_entry("2026-01-13")) + "]}",
)
o_add("reject_version_str", {"version": "1", "entries": [_entry("2026-01-13")]})
o_add("reject_version_2", {"version": 2, "entries": [_entry("2026-01-13")]})
o_add("reject_version_missing", {"entries": [_entry("2026-01-13")]})
o_add("reject_root_list", [1, 2])
o_add("reject_root_str", "x")
o_add("reject_entries_missing", {"version": 1})
o_add("reject_entries_dict", {"version": 1, "entries": {}})
o_add("reject_entry_nondict", {"version": 1, "entries": ["x"]})
o_add(
    "reject_label_missing",
    {"version": 1, "entries": [{k: v for k, v in _entry("2026-01-13").items() if k != "label"}]},
)
o_add("reject_label_wrong", {"version": 1, "entries": [_entry("2026-01-13", label="observed")]})
o_add(
    "reject_label_case",
    {"version": 1, "entries": [_entry("2026-01-13", label="Observed-Not-Official")]},
)
o_add("reject_label_int", {"version": 1, "entries": [_entry("2026-01-13", label=7)]})
o_add("reject_date_int", {"version": 1, "entries": [{**_entry("2026-01-13"), "date": 20260113}]})
o_add(
    "reject_date_missing",
    {"version": 1, "entries": [{k: v for k, v in _entry("2026-01-13").items() if k != "date"}]},
)
o_add(
    "reject_date_compact", {"version": 1, "entries": [{**_entry("2026-01-13"), "date": "20260513"}]}
)
o_add(
    "reject_date_week", {"version": 1, "entries": [{**_entry("2026-01-13"), "date": "2026-W20-3"}]}
)
o_add(
    "reject_date_unpadded",
    {"version": 1, "entries": [{**_entry("2026-01-13"), "date": "2026-5-13"}]},
)
o_add(
    "reject_date_unreal",
    {"version": 1, "entries": [{**_entry("2026-01-13"), "date": "2026-02-30"}]},
)
o_add(
    "reject_date_month13",
    {"version": 1, "entries": [{**_entry("2026-01-13"), "date": "2026-13-01"}]},
)
o_add(
    "reject_date_year0", {"version": 1, "entries": [{**_entry("2026-01-13"), "date": "0000-01-01"}]}
)
o_add("valid_leap", {"version": 1, "entries": [_entry("2024-02-29")]})
o_add("reject_nonleap", {"version": 1, "entries": [_entry("2023-02-29")]})
o_add(
    "reject_one_bad_rejects_all",
    {"version": 1, "entries": [_entry("2026-01-13"), _entry("2026-05-13", label="wrong")]},
)
o_add(
    "reject_date_trailing",
    {"version": 1, "entries": [{**_entry("2026-01-13"), "date": "2026-01-13 "}]},
)

observed_out = []
for name, payload_json in observed_cases:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        p = overlay_path(root)
        p.parent.mkdir(parents=True)
        p.write_text(payload_json, encoding="utf-8")
        with capture_logs() as logs:
            days = load_observed_holidays(root)
        rejected = any(e["event"] == OVERLAY_LOAD_FAILED_EVENT for e in logs)
    observed_out.append(
        {
            "name": name,
            "payload_json": payload_json,
            "days": None if rejected else sorted(d.isoformat() for d in days),
        }
    )

(HERE / "observed_goldens.json").write_text(
    json.dumps({"cases": observed_out}, indent=1, ensure_ascii=False) + "\n", encoding="utf-8"
)


# ── classify_month_drift goldens ─────────────────────────────────────────────


def ev(day: str, zero=True, idx=3, clean=1, failed=0) -> DayEvidence:
    return DayEvidence(
        day=date.fromisoformat(day),
        zero_bars_all_indices=zero,
        indices_with_month_data=idx,
        clean_success_windows=clean,
        failed_windows_touching=failed,
    )


classify_cases = [
    ("empty", [], 0, 3, 8),
    ("one_admitted", [ev("2026-05-13")], 0, 3, 8),
    ("skip_nonzero_bars", [ev("2026-05-12", zero=False), ev("2026-05-13")], 0, 3, 8),
    ("veto_failed", [ev("2026-05-13", failed=2)], 0, 3, 8),
    ("no_evidence_indices", [ev("2026-05-13", idx=2)], 0, 3, 8),
    ("no_evidence_indices_over", [ev("2026-05-13", idx=4)], 0, 3, 8),
    ("no_evidence_clean", [ev("2026-05-13", clean=0)], 0, 3, 8),
    ("veto_wins_over_no_evidence", [ev("2026-05-13", idx=2, failed=1)], 0, 3, 8),
    ("clean_2_admitted", [ev("2026-05-13", clean=2)], 0, 3, 8),
    ("three_at_cap", [ev("2026-05-11"), ev("2026-05-12"), ev("2026-05-13")], 0, 3, 8),
    (
        "four_over_month_cap",
        [ev("2026-05-11"), ev("2026-05-12"), ev("2026-05-13"), ev("2026-05-14")],
        0,
        3,
        8,
    ),
    (
        "month_cap_wins_over_year",
        [ev("2026-05-11"), ev("2026-05-12"), ev("2026-05-13"), ev("2026-05-14")],
        8,
        3,
        8,
    ),
    ("year_cap_exact_8", [ev("2026-05-11"), ev("2026-05-12"), ev("2026-05-13")], 5, 3, 8),
    ("year_cap_over", [ev("2026-05-11"), ev("2026-05-12"), ev("2026-05-13")], 6, 3, 8),
    ("year_cap_existing_only", [ev("2026-05-13")], 8, 3, 8),
    ("custom_month_cap_0", [ev("2026-05-13")], 0, 0, 8),
    ("custom_year_cap_0", [ev("2026-05-13")], 0, 3, 0),
    (
        "mixed_order",
        [
            ev("2026-05-11", failed=1),
            ev("2026-05-12", idx=1),
            ev("2026-05-13"),
            ev("2026-05-14", zero=False),
            ev("2026-05-15", clean=3),
        ],
        0,
        3,
        8,
    ),
]

classify_out = []
for name, evidence, existing, maxm, maxy in classify_cases:
    v = classify_month_drift(
        date(2026, 5, 1),
        evidence,
        existing_year_overlay_days=existing,
        detected_at="2026-07-04T10:00:00+00:00",
        max_flags_per_month=maxm,
        max_overlay_days_per_year=maxy,
        zero_bar_indices=("NSE/INDEX/NIFTY", "NSE/INDEX/BANKNIFTY", "BSE/INDEX/SENSEX"),
    )
    classify_out.append(
        {
            "name": name,
            "evidence": [
                [
                    r.day.isoformat(),
                    r.zero_bars_all_indices,
                    r.indices_with_month_data,
                    r.clean_success_windows,
                    r.failed_windows_touching,
                ]
                for r in evidence
            ],
            "existing_year_overlay_days": existing,
            "max_flags_per_month": maxm,
            "max_overlay_days_per_year": maxy,
            "candidates": [d.isoformat() for d in v.candidates],
            "flagged": [[f.day.isoformat(), f.clean_success_windows] for f in v.flagged],
            "vetoed_failed": [d.isoformat() for d in v.vetoed_failed],
            "no_evidence": [d.isoformat() for d in v.no_evidence],
            "over_month_cap": v.over_month_cap,
            "over_year_cap": v.over_year_cap,
        }
    )

(HERE / "classify_goldens.json").write_text(
    json.dumps({"cases": classify_out}, indent=1, ensure_ascii=False) + "\n", encoding="utf-8"
)

print(
    f"settle: {len(settle_out)} cases; iso: {len(iso_out)}; "
    f"observed: {len(observed_out)}; classify: {len(classify_out)}"
)
