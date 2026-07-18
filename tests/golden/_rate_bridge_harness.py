"""Shared harness for the rate-bridge read-verdict goldens (DEC-RUST-PULL-STATE-001).

Wave 3H of the Rust pull-state lane freezes the DECISION plane of
``brutex/data/rate_bridge.py`` (DEC-CLI-RATE-BRIDGE-001 +
DEC-CLI-BRIDGE-PULL-ACTIVE-001) as the CONTRACT the Rust port must satisfy
bit-for-bit.  This module is imported by BOTH the generator
(``generate_rate_bridge_golden.py``) and the consuming test
(``test_rate_bridge_golden.py``) — the ``_pull_intent_harness`` shape (never
generator-imports-test, the CodeQL ``py/cyclic-import`` break).

The verdict table is computed by DRIVING the LIVE Python module (the
oracle): every enumerated raw-bytes shape is written to a real
``<lake>/.rate/pull_rate.json`` and read back through the REAL
:func:`brutex.data.rate_bridge.read_rate_snapshot`.  Outcomes:

* ``snapshot`` — the decoded :class:`RateBridgeSnapshot` (floats recorded as
  IEEE-754 bit patterns so NaN/inf survive the JSON fixture);
* ``none`` — the honest degrade (absent / corrupt / wrong shape);
* ``raises`` — the PRE-EXISTING latent envelope (Golden Rule #10, recorded
  honestly, never "fixed" by the port): a JSON payload whose ``float()`` /
  ``int()`` coercion raises ``OverflowError`` (a 400-digit int in a float
  field; ``Infinity`` in an int field) ESCAPES the oracle's
  ``except (KeyError, TypeError, ValueError)`` — the seam keeps such
  payloads on the DOCUMENTED Python arm (the classifier returns ``None``),
  so flag-ON behaviour is identical by construction.

Beside each decodable case the harness records the MECHANICAL kernel-input
classification (the ``_classify_payload_for_kernel`` mirror the Wave-3H seam
adds to ``brutex/data/rate_bridge.py`` — drift-pinned against the module
copy by the seam suite once wired): concrete-type / coercibility FACTS only
(the CPython ``float()``/``int()`` taxonomy is PROBED, never re-implemented
— the pull-intent decode-stays-Python precedent), while the verdict LATTICE
(root-mapping → schema-equality → all-ten-fields-coerce, plus the
``pull_active`` bool-or-None law) lives in the Rust kernel.

The TSV projection carries the classified inputs + the BINARY expected
verdict (``1`` = a decoded snapshot, ``0`` = the ``None`` answer — derived
from the OBSERVED oracle outcome, never re-derived) + the expected
``pull_active`` arm, for the serde-free cargo fixture test
(``rust/brutex-pull-state/tests/rate_bridge_golden.rs``).
"""

from __future__ import annotations

import json
import struct
import tempfile
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Any

from brutex.data.rate_bridge import rate_bridge_path, read_rate_snapshot

#: The bridge schema version the module accepts (mirrors the private
#: ``rate_bridge._SCHEMA_VERSION`` — pinned by the generator so a version
#: bump forces a conscious regeneration, never a silent drift).
SCHEMA_VERSION: int = 3

#: Schema-value kind codes (mirrors ``rust/brutex-pull-state/src/rate_bridge.rs``
#: + the facade constants — ``bool`` probed before ``int`` because it
#: subclasses ``int``; the ``== 3`` EQUALITY law itself lives in the kernel).
SCHEMA_KIND_MISSING: int = 0  # absent key OR JSON null (``.get`` default)
SCHEMA_KIND_BOOL: int = 1  # value carried as 0/1 (Python ``True == 3`` is False)
SCHEMA_KIND_INT: int = 2  # i64-representable; value carried
SCHEMA_KIND_INT_HUGE: int = 3  # outside i64 — can never equal 3
SCHEMA_KIND_FLOAT: int = 4  # value carried as f64 (``3.0 == 3`` is True)
SCHEMA_KIND_OTHER: int = 5  # str/list/dict — never numerically equal

#: Required-field coercibility codes (CPython-PROBED facts).
FIELD_ABSENT: int = 0  # ``KeyError`` — reject
FIELD_OK: int = 1  # ``float()``/``int()`` succeeds — accept
FIELD_BAD: int = 2  # ``TypeError``/``ValueError`` — reject

#: ``pull_active`` kind codes (== the kernel's served arm: the bool-or-None
#: normalisation law — a genuine ``bool`` is carried, everything else reads
#: ``None``; the kernel echoes the arm so the TSV pins it cross-language).
PA_NONE: int = 0
PA_FALSE: int = 1
PA_TRUE: int = 2

#: The ten REQUIRED fields in the oracle's construction (probe) order, with
#: their coercion kind (``float`` / ``int``) — ``read_rate_snapshot``'s
#: constructor-argument evaluation order, where the FIRST failure raises.
REQUIRED_FIELDS: tuple[tuple[str, str], ...] = (
    ("effective_rps", "float"),
    ("current_rps", "float"),
    ("recent_minute", "int"),
    ("recent_429", "int"),
    ("ceiling_per_min", "int"),
    ("recent_1s", "int"),
    ("recent_600s", "int"),
    ("recent_3600s", "int"),
    ("wall_time", "float"),
    ("monotonic", "float"),
)

_I64_MIN: int = -(2**63)
_I64_MAX: int = 2**63 - 1


def f64_bits(value: float) -> str:
    """The IEEE-754 bit pattern of *value* as 16 lowercase hex chars (``<f8``)."""
    return struct.pack("<d", value).hex()


def f64_from_bits(bits: str) -> float:
    """Invert :func:`f64_bits` (bit-exact)."""
    return float(struct.unpack("<d", bytes.fromhex(bits))[0])


def _field_kind(payload: Mapping[str, Any], key: str, kind: str) -> int | None:
    """One field's coercibility FACT — CPython's own taxonomy, probed.

    ``None`` = the coercion raises ``OverflowError`` (a class the oracle's
    ``except`` does NOT catch, so it ESCAPES ``read_rate_snapshot``): the
    whole payload stays on the documented Python arm — the classifier
    abstains rather than model a raise the kernel could never reproduce.
    """
    if key not in payload:
        return FIELD_ABSENT
    value = payload[key]
    try:
        if kind == "float":
            float(value)
        else:
            int(value)
    except (TypeError, ValueError):
        return FIELD_BAD
    except OverflowError:
        return None
    return FIELD_OK


def _schema_class(payload: Mapping[str, Any]) -> tuple[int, int, str]:
    """``(schema_kind, schema_int, schema_float_bits)`` — mechanical facts."""
    value = payload.get("schema")
    if value is None:
        return (SCHEMA_KIND_MISSING, 0, f64_bits(0.0))
    if isinstance(value, bool):
        return (SCHEMA_KIND_BOOL, int(value), f64_bits(0.0))
    if isinstance(value, int):
        if _I64_MIN <= value <= _I64_MAX:
            return (SCHEMA_KIND_INT, value, f64_bits(0.0))
        return (SCHEMA_KIND_INT_HUGE, 0, f64_bits(0.0))
    if isinstance(value, float):
        return (SCHEMA_KIND_FLOAT, 0, f64_bits(value))
    return (SCHEMA_KIND_OTHER, 0, f64_bits(0.0))


def classify_payload(data: object) -> dict[str, Any] | None:
    """The harness mirror of the seam's mechanical kernel-input classifier.

    ``None`` = the payload stays on the documented Python arm (some field's
    coercion probe raised ``OverflowError`` — see :func:`_field_kind`).  A
    non-mapping root zeroes every other slot (the kernel refuses on the root
    code before reading them — the pull-intent precedent).
    """
    if not isinstance(data, Mapping):
        return {
            "root_mapping": 0,
            "schema_kind": SCHEMA_KIND_MISSING,
            "schema_int": 0,
            "schema_float_bits": f64_bits(0.0),
            "field_kinds": [FIELD_ABSENT] * len(REQUIRED_FIELDS),
            "pa_kind": PA_NONE,
        }
    schema_kind, schema_int, schema_float_bits = _schema_class(data)
    field_kinds: list[int] = []
    for key, kind in REQUIRED_FIELDS:
        code = _field_kind(data, key, kind)
        if code is None:
            return None
        field_kinds.append(code)
    raw_pa = data.get("pull_active")
    if isinstance(raw_pa, bool):
        pa_kind = PA_TRUE if raw_pa else PA_FALSE
    else:
        pa_kind = PA_NONE
    return {
        "root_mapping": 1,
        "schema_kind": schema_kind,
        "schema_int": schema_int,
        "schema_float_bits": schema_float_bits,
        "field_kinds": field_kinds,
        "pa_kind": pa_kind,
    }


def _dumps(obj: Any) -> bytes:
    """JSON bytes with NaN/Infinity literals allowed (CPython's default)."""
    return json.dumps(obj).encode("utf-8")


def _valid_payload(**overrides: Any) -> dict[str, Any]:
    """One fully-valid v3 payload; ``overrides`` mutate/extend fields."""
    payload: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "effective_rps": 3.25,
        "current_rps": 5.0,
        "recent_minute": 180,
        "recent_429": 2,
        "ceiling_per_min": 500,
        "recent_1s": 4,
        "recent_600s": 1800,
        "recent_3600s": 9000,
        "wall_time": 1_700_000_000.5,
        "monotonic": 12345.25,
        "pull_active": True,
    }
    payload.update(overrides)
    return payload


_ABSENT = object()


def _without(payload: dict[str, Any], key: str) -> dict[str, Any]:
    out = dict(payload)
    out.pop(key, None)
    return out


def enumerate_cases() -> list[tuple[str, bytes]]:
    """Every frozen ``(case_id, raw_file_bytes)`` shape, deterministic order."""
    base = _valid_payload()
    cases: list[tuple[str, bytes]] = [
        # -- valid family -----------------------------------------------------
        ("valid_full_active_true", _dumps(base)),
        ("valid_active_false", _dumps(_valid_payload(pull_active=False))),
        ("valid_active_null", _dumps(_valid_payload(pull_active=None))),
        ("valid_no_active_key", _dumps(_without(base, "pull_active"))),
        ("valid_active_string", _dumps(_valid_payload(pull_active="yes"))),
        ("valid_active_int_one", _dumps(_valid_payload(pull_active=1))),
        # CPython float()/int() accept numeric STRINGS — a pinned quirk.
        ("valid_str_float_field", _dumps(_valid_payload(effective_rps="2.5"))),
        ("valid_str_int_field", _dumps(_valid_payload(recent_minute="7"))),
        ("valid_underscore_str_int", _dumps(_valid_payload(recent_minute="1_2"))),
        # bool is an int — float(True)=1.0 / int(True)=1 (accepted, pinned).
        ("valid_bool_fields", _dumps(_valid_payload(effective_rps=False, recent_429=True))),
        # int() truncates a float — 2.9 → 2 (accepted, pinned).
        ("valid_float_in_int_field", _dumps(_valid_payload(recent_429=2.9))),
        ("valid_inf_float_field", _dumps(_valid_payload(effective_rps=float("inf")))),
        ("valid_nan_float_field", _dumps(_valid_payload(wall_time=float("nan")))),
        ("valid_negative_values", _dumps(_valid_payload(current_rps=-1.5, recent_1s=-3))),
        ("valid_huge_int_int_field", _dumps(_valid_payload(recent_3600s=10**30))),
        ("valid_schema_float_three", _dumps(_valid_payload(schema=3.0))),
        ("valid_extra_keys", _dumps(_valid_payload(alien="ignored", zz=1))),
        # -- reject: non-mapping roots ---------------------------------------
        ("reject_root_list", _dumps([1, 2])),
        ("reject_root_str", _dumps("x")),
        ("reject_root_int", _dumps(7)),
        ("reject_root_null", _dumps(None)),
        ("reject_root_bool", _dumps(True)),
        # -- reject: schema law ----------------------------------------------
        ("reject_schema_absent", _dumps(_without(base, "schema"))),
        ("reject_schema_null", _dumps(_valid_payload(schema=None))),
        ("reject_schema_two", _dumps(_valid_payload(schema=2))),
        ("reject_schema_str_three", _dumps(_valid_payload(schema="3"))),
        ("reject_schema_three_point_five", _dumps(_valid_payload(schema=3.5))),
        ("reject_schema_true", _dumps(_valid_payload(schema=True))),
        ("reject_schema_huge_int", _dumps(_valid_payload(schema=10**30))),
        ("reject_schema_nan", _dumps(_valid_payload(schema=float("nan")))),
        ("reject_schema_list", _dumps(_valid_payload(schema=[3]))),
        # -- reject: field coercion law --------------------------------------
        ("reject_field_null", _dumps(_valid_payload(effective_rps=None))),
        ("reject_field_str_bad", _dumps(_valid_payload(effective_rps="not-a-number"))),
        ("reject_field_list", _dumps(_valid_payload(recent_minute=[1]))),
        ("reject_field_dict", _dumps(_valid_payload(monotonic={}))),
        ("reject_int_field_nan", _dumps(_valid_payload(recent_minute=float("nan")))),
        ("reject_int_field_float_str", _dumps(_valid_payload(recent_minute="2.5"))),
        ("reject_int_field_hex_str", _dumps(_valid_payload(recent_429="0x1"))),
        # -- corrupt bytes (decode fails BEFORE the seam — no classification) -
        ("corrupt_truncated_json", b'{"schema": 3'),
        ("corrupt_non_utf8", b"\xff\xfe{}"),
        ("corrupt_empty", b""),
        # -- the documented Python-arm raises (OverflowError escapes) ---------
        ("raises_huge_float_field", ("{" + '"schema": 3, "effective_rps": 1' + "0" * 400 + "}").encode()),
        ("raises_inf_in_int_field", _dumps(_valid_payload(recent_minute=float("inf")))),
        # first failure (ValueError field) precedes the huge field in probe
        # order → the ORACLE serves None; the classifier still abstains
        # (Python arm) because ITS probe sweep hits the OverflowError.
        (
            "pythonarm_bad_then_huge",
            _dumps(_valid_payload(effective_rps="abc", recent_minute=float("inf"))),
        ),
    ]
    # Per-field ABSENT sweep — every one of the ten required fields.
    for key, _ in REQUIRED_FIELDS:
        cases.append((f"reject_absent_{key}", _dumps(_without(base, key))))
    return cases


def _observe(raw: bytes) -> dict[str, Any]:
    """Drive the REAL ``read_rate_snapshot`` over *raw* in a scratch lake."""
    with tempfile.TemporaryDirectory() as tmp:
        lake = Path(tmp)
        dest = rate_bridge_path(lake)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(raw)
        try:
            snapshot = read_rate_snapshot(lake)
        except Exception as exc:  # noqa: BLE001 — the documented raises family is captured honestly
            return {"kind": "raises", "type": type(exc).__name__}
    if snapshot is None:
        return {"kind": "none"}
    fields = asdict(snapshot)
    out: dict[str, Any] = {"kind": "snapshot"}
    for name, value in fields.items():
        if isinstance(value, bool) or value is None:
            out[name] = value
        elif isinstance(value, float):
            out[name] = {"bits": f64_bits(value)}
        else:
            out[name] = value
    return out


def _decoded(raw: bytes) -> tuple[bool, object]:
    """``(decode_ok, payload)`` via the oracle's own decode taxonomy."""
    try:
        return (True, json.loads(raw))
    except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
        return (False, None)


def compute_rows() -> list[dict[str, Any]]:
    """One row per enumerated case: raw bytes + oracle outcome + classification."""
    rows: list[dict[str, Any]] = []
    for case_id, raw in enumerate_cases():
        decode_ok, payload = _decoded(raw)
        classified = classify_payload(payload) if decode_ok else None
        rows.append(
            {
                "id": case_id,
                "raw_hex": raw.hex(),
                "decode_ok": decode_ok,
                "outcome": _observe(raw),
                "classified": classified,
            }
        )
    return rows


def compute_payload() -> dict[str, Any]:
    """The whole JSON fixture payload."""
    return {
        "_comment": (
            "Frozen rate-bridge read-verdict golden (DEC-RUST-PULL-STATE-001 "
            "Wave 3H) — captured from the PRE-conversion "
            "brutex/data/rate_bridge.py; regenerate ONLY under "
            "BRUTEX_REGEN_RATE_BRIDGE_GOLDEN=1 with a ledgered reason."
        ),
        "schema_version": SCHEMA_VERSION,
        "rows": compute_rows(),
    }


def compute_tsv(payload: dict[str, Any]) -> str:
    """The serde-free cargo-fixture projection of *payload*.

    One line per DECODED + CLASSIFIED case (corrupt-bytes rows never reach
    the seam; Python-arm rows are excluded by contract):
    ``id root schema_kind schema_int schema_float_bits f0..f9 pa_kind
    expected_valid expected_pa`` — ``expected_valid`` from the OBSERVED
    oracle outcome (1 = snapshot decoded), ``expected_pa`` from the decoded
    snapshot's ``pull_active`` when valid, else the classified kind (the
    identity law the kernel echoes).
    """
    lines = [
        "# rate_bridge payload-verdict golden (DEC-RUST-PULL-STATE-001 Wave 3H)",
        "# id\troot\tschema_kind\tschema_int\tschema_float_bits\tf0..f9\tpa_kind\texpected_valid\texpected_pa",
    ]
    for row in payload["rows"]:
        classified = row["classified"]
        if classified is None:
            continue
        outcome = row["outcome"]
        assert outcome["kind"] in {"snapshot", "none"}, row["id"]
        expected_valid = 1 if outcome["kind"] == "snapshot" else 0
        if outcome["kind"] == "snapshot":
            pa = outcome["pull_active"]
            expected_pa = PA_NONE if pa is None else (PA_TRUE if pa else PA_FALSE)
        else:
            expected_pa = classified["pa_kind"]
        fields = "\t".join(str(code) for code in classified["field_kinds"])
        lines.append(
            f"{row['id']}\t{classified['root_mapping']}\t{classified['schema_kind']}\t"
            f"{classified['schema_int']}\t{classified['schema_float_bits']}\t{fields}\t"
            f"{classified['pa_kind']}\t{expected_valid}\t{expected_pa}"
        )
    return "\n".join(lines) + "\n"
