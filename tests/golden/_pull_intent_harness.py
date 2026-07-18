"""Shared case-enumeration harness for the pull-intent goldens (DEC-RUST-PULL-STATE-001, Wave 2E).

Imported by BOTH ``generate_pull_intent_golden.py`` (the one-shot freeze) and
``test_pull_intent_golden.py`` (the recompute-and-compare gate) — the
``_pull_outcome_harness`` shape, never generator-imports-test.

Four tables are computed from the LIVE ``brutex.dashboard.pull_intent``:

* the PARSE table — every enumerated corruption / valid shape of the intent
  sidecar BYTES, driven through the REAL :class:`PullIntentStore` pipeline
  (``read_sidecar_bytes`` → the corruption-tolerant decode →
  ``PullIntent.from_payload`` → the corrupt-delete law).  The recorded verdict
  (the loaded intent payload or ``None``) + the corrupt-DELETE effect are the
  byte-level ORACLE the Rust seam must preserve.  Each row also carries the
  MECHANICAL kernel-input classification of the decoded payload (the 9-slot
  code tuple the seam feeds ``intent_shape_verdict``) — the taxonomy-parity
  law by CONSTRUCTION: the JSON decode (and therefore CPython's entire
  failure taxonomy — ``UnicodeDecodeError`` ⊂ ``ValueError``,
  ``json.JSONDecodeError``, ``RecursionError`` nesting bombs,
  ``OverflowError``) STAYS Python, and every decode failure folds into the
  ONE ``payload is None`` → not-a-Mapping classification, so serde/hand-rolled
  parser drift is structurally impossible (design risk #3 closed at the
  boundary, never approximated).
* the RESUME-DECISION table — the ``run_auto_resume`` arm lattice
  (``has_intent`` × probe × restarts × cap), derived by driving the REAL
  function against a REAL store + injected probe/launch/alert fakes; the arm
  token is the mechanical projection of the observed :class:`ResumeAction`
  (+ the bump-before-launch counter law and the probe-never-called-without-
  intent ORDER law).
* the REFUSED-START RESTORE table — the
  ``restore_intent_after_refused_start`` lattice (dry_run × started ×
  current-present × prior-present), arm tokens projected from the OBSERVED
  on-disk effect (untouched / left absent / orphan cleared / prior restored).
* the ENV-TOKEN table — ``resolve_pull_intent_resume`` over hostile raw
  values (unicode whitespace incl. the CPython-``isspace``-only ``\\x1c``
  class, fullwidth case, junk) — the strip+lower+falsy-set law.

The kernel TSV projects the SAME rows onto the dependency-free line format
the crate's own ``cargo test`` replays (the cross-language pin).  Rows whose
integers do not fit the FFI (a ``restarts`` beyond i64/u64) are marked
``kernel_row=False`` — the seam keeps those on the documented Python arm.
"""

from __future__ import annotations

import base64
import json
import os
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from brutex.dashboard.pull_intent import (
    MAX_INTENT_BYTES,
    PULL_INTENT_RESUME_ENV,
    PullIntent,
    PullIntentStore,
    ResumeAction,
    resolve_pull_intent_resume,
    restore_intent_after_refused_start,
    run_auto_resume,
)

if TYPE_CHECKING:
    from pathlib import Path

# ── kernel-input classification codes (the FFI contract; mirrored by the
#    facade constants in brutex/pull_state_rust.py + rust src/intent.rs) ─────

#: Field-kind codes for the five optional request-string fields AND
#: ``created_at``: absent key · present-``null`` · string · anything else.
KIND_ABSENT = 0
KIND_NULL = 1
KIND_STR = 2
KIND_OTHER = 3

#: ``restarts``-kind codes.  ``BOOL`` is distinct from ``INT`` (the concrete
#: CPython type tag — ``bool`` subclasses ``int``, so accurate type reporting
#: must probe it first; the "a JSON ``true`` is not a count" LAW itself lives
#: in the kernel).  ``INT_HUGE_*`` are pure REPRESENTABILITY facts (outside
#: i64) — the sign law for them also lives in the kernel.
RESTARTS_ABSENT = 0
RESTARTS_NULL = 1
RESTARTS_BOOL = 2
RESTARTS_INT = 3
RESTARTS_INT_HUGE_POS = 4
RESTARTS_INT_HUGE_NEG = 5
RESTARTS_OTHER = 6

_I64_MIN = -(2**63)
_I64_MAX = 2**63 - 1
_U64_MAX = 2**64 - 1

#: Resume-decision arm tokens (mirrors ``rust/brutex-pull-state/src/intent.rs``).
ARM_NO_INTENT = 0
ARM_PEER_ACTIVE = 1
ARM_OVER_CAP = 2
ARM_PROCEED = 3

#: Refused-start restore arm tokens.
RESTORE_NO_ACTION = 0
RESTORE_LEAVE_ABSENT = 1
RESTORE_CLEAR_ORPHAN = 2
RESTORE_RESTORE_PRIOR = 3

_ACTION_TO_ARM: dict[ResumeAction, int] = {
    ResumeAction.NO_INTENT: ARM_NO_INTENT,
    ResumeAction.PEER_ACTIVE: ARM_PEER_ACTIVE,
    ResumeAction.OVER_CAP: ARM_OVER_CAP,
    ResumeAction.FIRED: ARM_PROCEED,
}


def classify_parsed_payload(payload: object) -> tuple[int, int, int, int, int, int, int, int, int]:
    """MECHANICAL kernel-input classification of a decoded payload — no law.

    Returns the 9-slot code tuple ``(root_mapping, symbols, symbol,
    from_date, to_date, scope, restarts_kind, restarts_value,
    created_at_kind)`` the seam feeds ``intent_shape_verdict``.  Every slot is
    a concrete-type/representability FACT (``isinstance`` tags + the i64
    range); every VERDICT (absent ≡ null for request fields, bool-is-not-a-
    count, the sign law, created_at's absent-defaults-empty) lives in the
    kernel.  A non-Mapping root (including the ``None`` a failed JSON decode
    folds to) zeroes the field slots — the kernel refuses on the root code
    before reading them.
    """
    if not isinstance(payload, Mapping):
        return (0, 0, 0, 0, 0, 0, 0, 0, 0)
    codes: list[int] = [1]
    for key in ("symbols", "symbol", "from_date", "to_date", "scope"):
        if key not in payload:
            codes.append(KIND_ABSENT)
        else:
            value = payload[key]
            if value is None:
                codes.append(KIND_NULL)
            elif isinstance(value, str):
                codes.append(KIND_STR)
            else:
                codes.append(KIND_OTHER)
    if "restarts" not in payload:
        codes.extend((RESTARTS_ABSENT, 0))
    else:
        restarts = payload["restarts"]
        if restarts is None:
            codes.extend((RESTARTS_NULL, 0))
        elif isinstance(restarts, bool):
            codes.extend((RESTARTS_BOOL, 0))
        elif isinstance(restarts, int):
            if _I64_MIN <= restarts <= _I64_MAX:
                codes.extend((RESTARTS_INT, restarts))
            elif restarts > 0:
                codes.extend((RESTARTS_INT_HUGE_POS, 0))
            else:
                codes.extend((RESTARTS_INT_HUGE_NEG, 0))
        else:
            codes.extend((RESTARTS_OTHER, 0))
    if "created_at" not in payload:
        codes.append(KIND_ABSENT)
    else:
        created = payload["created_at"]
        if created is None:
            codes.append(KIND_NULL)
        elif isinstance(created, str):
            codes.append(KIND_STR)
        else:
            codes.append(KIND_OTHER)
    return (
        codes[0],
        codes[1],
        codes[2],
        codes[3],
        codes[4],
        codes[5],
        codes[6],
        codes[7],
        codes[8],
    )


# ── the parse-case corpus ────────────────────────────────────────────────────


def _payload_bytes(**fields: object) -> bytes:
    """A well-formed intent JSON body with the given field overrides."""
    payload: dict[str, Any] = {
        "version": 1,
        "symbols": "NSE-NIFTY,NSE-BANKNIFTY",
        "symbol": None,
        "from_date": "2024-01-01",
        "to_date": "2024-02-01",
        "restarts": 1,
        "created_at": "2026-07-18T00:00:00+00:00",
        "scope": "spot-only",
    }
    for key, value in fields.items():
        if value is _ABSENT:
            payload.pop(key, None)
        else:
            payload[key] = value
    return json.dumps(payload, sort_keys=True).encode("utf-8")


class _AbsentType:
    """Sentinel: remove the key from the well-formed payload."""


_ABSENT = _AbsentType()

#: The nesting-bomb depth — far past every plausible recursion limit, so the
#: CPython scanner's ``RecursionError`` fires deterministically.
NESTING_BOMB_DEPTH = 100_000

#: ``(case_id, recipe)`` — recipes are deterministic harness constants; the
#: fixture stores the materialised base64 for ``bytes`` recipes (pinning the
#: corpus itself) and the recipe parameters for the two generated shapes.
PARSE_CASES: tuple[tuple[str, dict[str, Any]], ...] = (
    # valid shapes -----------------------------------------------------------
    ("valid_full", {"kind": "bytes", "raw": _payload_bytes()}),
    ("valid_scope_fno_only", {"kind": "bytes", "raw": _payload_bytes(scope="fno-only")}),
    # a junk scope STRING is shape-valid (token validation happens at launch,
    # where the resume path's ValueError arm clears the intent)
    ("valid_scope_junk_token", {"kind": "bytes", "raw": _payload_bytes(scope="not-a-scope")}),
    ("valid_scope_null", {"kind": "bytes", "raw": _payload_bytes(scope=None)}),
    # the pre-scope file (rollback residual): absent key ⇒ None ⇒ full chain
    ("valid_pre_scope_absent", {"kind": "bytes", "raw": _payload_bytes(scope=_ABSENT)}),
    ("valid_minimal", {"kind": "bytes", "raw": b'{"restarts": 0}'}),
    ("valid_created_at_absent", {"kind": "bytes", "raw": _payload_bytes(created_at=_ABSENT)}),
    ("valid_restarts_zero", {"kind": "bytes", "raw": _payload_bytes(restarts=0)}),
    ("valid_restarts_i64_max", {"kind": "bytes", "raw": _payload_bytes(restarts=_I64_MAX)}),
    # arbitrary-precision ints: CPython json yields exact big ints; ≥ 0 is a
    # VALID count (outside i64 ⇒ the huge-pos representability code)
    ("valid_restarts_beyond_i64", {"kind": "bytes", "raw": _payload_bytes(restarts=2**63)}),
    ("valid_restarts_huge", {"kind": "bytes", "raw": _payload_bytes(restarts=2**100)}),
    # unknown extra keys are ignored (forward-compat) — version included
    (
        "valid_extra_keys_ignored",
        {"kind": "bytes", "raw": _payload_bytes(future_key={"nested": [1, 2]}, version=99)},
    ),
    # duplicate keys: CPython json.loads is last-wins (decode-taxonomy pin —
    # the decode stays Python, so this can never drift)
    (
        "valid_duplicate_key_last_wins",
        {"kind": "bytes", "raw": b'{"restarts": 7, "restarts": 2}'},
    ),
    # shape violations on a decodable payload -------------------------------
    ("bad_restarts_true", {"kind": "bytes", "raw": _payload_bytes(restarts=True)}),
    ("bad_restarts_false", {"kind": "bytes", "raw": _payload_bytes(restarts=False)}),
    ("bad_restarts_negative", {"kind": "bytes", "raw": _payload_bytes(restarts=-1)}),
    ("bad_restarts_i64_min", {"kind": "bytes", "raw": _payload_bytes(restarts=_I64_MIN)}),
    ("bad_restarts_huge_negative", {"kind": "bytes", "raw": _payload_bytes(restarts=-(2**100))}),
    ("bad_restarts_absent", {"kind": "bytes", "raw": _payload_bytes(restarts=_ABSENT)}),
    ("bad_restarts_null", {"kind": "bytes", "raw": _payload_bytes(restarts=None)}),
    ("bad_restarts_float", {"kind": "bytes", "raw": _payload_bytes(restarts=1.5)}),
    ("bad_restarts_float_integral", {"kind": "bytes", "raw": _payload_bytes(restarts=2.0)}),
    ("bad_restarts_str", {"kind": "bytes", "raw": _payload_bytes(restarts="1")}),
    # NaN: CPython json accepts the literal (a float) — not an int ⇒ corrupt
    ("bad_restarts_nan_literal", {"kind": "bytes", "raw": b'{"restarts": NaN}'}),
    ("bad_symbols_int", {"kind": "bytes", "raw": _payload_bytes(symbols=5)}),
    ("bad_symbols_list", {"kind": "bytes", "raw": _payload_bytes(symbols=["NSE-NIFTY"])}),
    ("bad_symbol_bool", {"kind": "bytes", "raw": _payload_bytes(symbol=True)}),
    ("bad_from_date_float", {"kind": "bytes", "raw": _payload_bytes(from_date=20240101.0)}),
    ("bad_to_date_object", {"kind": "bytes", "raw": _payload_bytes(to_date={"y": 2024})}),
    ("bad_scope_int", {"kind": "bytes", "raw": _payload_bytes(scope=1)}),
    ("bad_created_at_null", {"kind": "bytes", "raw": _payload_bytes(created_at=None)}),
    ("bad_created_at_int", {"kind": "bytes", "raw": _payload_bytes(created_at=1234)}),
    # valid JSON, non-object roots ------------------------------------------
    ("bad_root_array", {"kind": "bytes", "raw": b'[{"restarts": 0}]'}),
    ("bad_root_string", {"kind": "bytes", "raw": b'"an intent"'}),
    ("bad_root_int", {"kind": "bytes", "raw": b"7"}),
    ("bad_root_null", {"kind": "bytes", "raw": b"null"}),
    ("bad_root_bool", {"kind": "bytes", "raw": b"true"}),
    # decode failures (the taxonomy that STAYS Python) ----------------------
    ("corrupt_not_json", {"kind": "bytes", "raw": b"not json {{{"}),
    ("corrupt_empty", {"kind": "bytes", "raw": b""}),
    ("corrupt_truncated_json", {"kind": "bytes", "raw": b'{"restarts": 0'}),
    ("corrupt_invalid_utf8", {"kind": "bytes", "raw": b'\xff\xfe{"restarts": 0}'}),
    # a UTF-8 BOM survives the manual .decode("utf-8") but fails json.loads
    ("corrupt_utf8_bom", {"kind": "bytes", "raw": b'\xef\xbb\xbf{"restarts": 0}'}),
    ("corrupt_nesting_bomb", {"kind": "nested_array", "depth": NESTING_BOMB_DEPTH}),
    # the 16 MiB fstat-on-fd cap (checked in read_sidecar_bytes — Python);
    # the VERDICT law (oversized ⇒ corrupt ⇒ deleted + absent) is pinned here
    ("corrupt_oversized", {"kind": "oversized"}),
)


def materialize_parse_bytes(recipe: Mapping[str, Any]) -> bytes:
    """Materialise one parse-case recipe into the raw sidecar bytes."""
    kind = recipe["kind"]
    if kind == "bytes":
        raw = recipe["raw"]
        if isinstance(raw, bytes):
            return raw
        return base64.b64decode(str(raw))
    if kind == "nested_array":
        depth = int(recipe["depth"])
        return b"[" * depth + b"]" * depth
    if kind == "oversized":
        return b'{"restarts": 0}' + b" " * MAX_INTENT_BYTES
    raise ValueError(f"unknown parse-case recipe kind {kind!r}")


def _fixture_recipe(recipe: Mapping[str, Any]) -> dict[str, Any]:
    """The JSON-serialisable recipe (bytes → base64) frozen in the fixture."""
    if recipe["kind"] == "bytes":
        raw = recipe["raw"]
        b64 = (
            base64.b64encode(raw).decode("ascii") if isinstance(raw, bytes) else str(recipe["raw"])
        )
        return {"kind": "bytes", "raw_b64": b64}
    return {key: (int(value) if key == "depth" else value) for key, value in recipe.items()}


def _decoded_payload(raw: bytes) -> object:
    """The store's decode law, replicated ONLY to derive the kernel codes.

    (The store-level VERDICT in the table below is computed through the REAL
    ``PullIntentStore.load`` — this projection exists purely so the fixture
    carries the classified kernel inputs each case folds to.)
    """
    if len(raw) > MAX_INTENT_BYTES:
        return None  # read_sidecar_bytes answers ``oversized`` with raw=None
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError, OverflowError):
        return None


def compute_parse_table(base_dir: Path) -> list[dict[str, Any]]:
    """Drive every parse case through the REAL store pipeline; return the rows."""
    rows: list[dict[str, Any]] = []
    for case_id, recipe in PARSE_CASES:
        lake_root = base_dir / f"parse_{case_id}"
        lake_root.mkdir(parents=True, exist_ok=True)
        store = PullIntentStore(lake_root)
        raw = materialize_parse_bytes(recipe)
        store.path.write_bytes(raw)
        intent = store.load()
        codes = classify_parsed_payload(_decoded_payload(raw))
        rows.append(
            {
                "id": case_id,
                "recipe": _fixture_recipe(recipe),
                "intent": None if intent is None else intent.to_payload(),
                "file_deleted": not store.path.exists(),
                "codes": list(codes),
            }
        )
    return rows


# ── the resume-decision matrix ───────────────────────────────────────────────

#: ``restarts`` values driven through the REAL ``run_auto_resume``; the huge
#: value exercises the beyond-u64 Python-arm row (``kernel_row=False``).
RESUME_RESTARTS: tuple[int, ...] = (0, 1, 2, 3, 5, _I64_MAX, 2**100)
RESUME_CAPS: tuple[int, ...] = (0, 1, 2, 3)


def compute_resume_table(base_dir: Path) -> list[dict[str, Any]]:
    """Drive the resume lattice through the REAL ``run_auto_resume``."""
    rows: list[dict[str, Any]] = []
    case_n = 0
    for has_intent in (False, True):
        for probe_ok in (False, True):
            for restarts in RESUME_RESTARTS if has_intent else (0,):
                for cap in RESUME_CAPS:
                    case_n += 1
                    lake_root = base_dir / f"resume_{case_n}"
                    lake_root.mkdir(parents=True, exist_ok=True)
                    store = PullIntentStore(lake_root)
                    if has_intent:
                        store.write(
                            PullIntent(
                                symbols="NSE-NIFTY",
                                symbol=None,
                                from_date=None,
                                to_date=None,
                                restarts=restarts,
                                created_at="2026-07-18T00:00:00+00:00",
                                scope=None,
                            )
                        )
                    probe_calls: list[bool] = []
                    launched: list[PullIntent] = []

                    def _probe(probe_ok: bool = probe_ok, calls: list[bool] = probe_calls) -> bool:
                        calls.append(probe_ok)
                        return probe_ok

                    action = run_auto_resume(
                        store,
                        launch=launched.append,
                        probe_singleflight=_probe,
                        alert=lambda intent: None,
                        max_restarts=cap,
                    )
                    rows.append(
                        {
                            "id": f"resume_{case_n}",
                            "has_intent": has_intent,
                            "probe": probe_ok,
                            "restarts": restarts,
                            "max_restarts": cap,
                            "action": action.value,
                            "arm": _ACTION_TO_ARM[action],
                            # the ORDER law: no probe without an intent
                            "probe_called": bool(probe_calls),
                            # the bump-BEFORE-launch law
                            "launch_restarts": launched[0].restarts if launched else None,
                            "kernel_row": 0 <= restarts <= _U64_MAX and 0 <= cap <= _U64_MAX,
                        }
                    )
    return rows


# ── the refused-start restore matrix ─────────────────────────────────────────


def compute_restore_table(base_dir: Path) -> list[dict[str, Any]]:
    """Drive the 16-combo restore lattice through the REAL function."""
    current = PullIntent(
        symbols="CURRENT",
        symbol=None,
        from_date=None,
        to_date=None,
        restarts=3,
        created_at="2026-07-18T01:00:00+00:00",
        scope=None,
    )
    prior = PullIntent(
        symbols="PRIOR",
        symbol="NSE-NIFTY",
        from_date="2024-01-01",
        to_date=None,
        restarts=1,
        created_at="2026-07-17T00:00:00+00:00",
        scope="spot-only",
    )
    rows: list[dict[str, Any]] = []
    case_n = 0
    for dry_run in (False, True):
        for started in (False, True):
            for current_present in (False, True):
                for prior_present in (False, True):
                    case_n += 1
                    lake_root = base_dir / f"restore_{case_n}"
                    lake_root.mkdir(parents=True, exist_ok=True)
                    store = PullIntentStore(lake_root)
                    if current_present:
                        store.write(current)
                    returned = restore_intent_after_refused_start(
                        store,
                        prior=prior if prior_present else None,
                        dry_run=dry_run,
                        started=started,
                    )
                    after = store.load()
                    if after is None:
                        final = "absent"
                    elif after == prior:
                        final = "prior"
                    elif after == current:
                        final = "current"
                    else:  # pragma: no cover — no fourth state is reachable
                        final = "other"
                    # the arm token, projected MECHANICALLY from the effects
                    if not returned:
                        arm = RESTORE_NO_ACTION if (dry_run or started) else RESTORE_LEAVE_ABSENT
                    elif final == "absent":
                        arm = RESTORE_CLEAR_ORPHAN
                    else:
                        arm = RESTORE_RESTORE_PRIOR
                    rows.append(
                        {
                            "id": f"restore_{case_n}",
                            "dry_run": dry_run,
                            "started": started,
                            "current_present": current_present,
                            "prior_present": prior_present,
                            "returned": returned,
                            "final_file": final,
                            "arm": arm,
                        }
                    )
    return rows


# ── the env-token matrix ─────────────────────────────────────────────────────

#: Hostile raw values for the strip+lower+falsy-set law.  ``\x1c``/``\x1d``…
#: are CPython-``str.isspace``-ONLY whitespace (bidi class B/S — NOT Unicode
#: ``White_Space``), the exact class a naive ``str::trim`` port would miss.
ENV_CASES: tuple[str | None, ...] = (
    None,
    "",
    " ",
    "0",
    "1",
    "false",
    "FALSE",
    "False",
    "no",
    " no ",
    "NO",
    "off",
    "OFF",
    "on",
    "yes",
    "true",
    "junk",
    "0x0",
    "00",
    "falsey",
    "nope",
    "\x1c0",
    "0\x1d",
    "\x1e\x1foff\x1c",
    "\x850",
    "\xa0no\xa0",
    " false　",
    "   no ",
    "  0",
    "ｎｏ",  # fullwidth — lowercases to fullwidth, NOT ascii "no"
    "ＯＦＦ",
    "İ",  # dotted capital I — lowercases to "i" + combining dot
    "ﬀ",  # latin small ligature ff — .lower() keeps the ligature
    "oﬀ",
    "​0",  # ZERO WIDTH SPACE is NOT whitespace — must stay truthy
    "no​",
    " 0 ",
    "\t\nfalse\r\v\f",
)


def compute_env_table() -> list[dict[str, Any]]:
    """Every env case through the LIVE ``resolve_pull_intent_resume``."""
    rows: list[dict[str, Any]] = []
    for index, raw in enumerate(ENV_CASES):
        env: dict[str, str] = {} if raw is None else {PULL_INTENT_RESUME_ENV: raw}
        rows.append(
            {
                "id": f"env_{index}",
                "raw": raw,
                "enabled": resolve_pull_intent_resume(env),
            }
        )
    return rows


# ── the cross-language kernel TSV ────────────────────────────────────────────


def _hex_or_none(raw: str | None) -> str:
    return "@none" if raw is None else raw.encode("utf-8").hex() or "@empty"


def compute_kernel_tsv(
    parse_rows: list[dict[str, Any]],
    resume_rows: list[dict[str, Any]],
    restore_rows: list[dict[str, Any]],
    env_rows: list[dict[str, Any]],
) -> str:
    """The dependency-free TSV the crate's ``cargo test`` replays.

    * ``shape`` rows: the 9 classified kernel inputs + the BINARY expected
      verdict (``1`` = a valid intent, ``0`` = the corrupt/absent answer) —
      the fine violation codes are pinned by the crate's own unit tests.
    * ``resume`` rows: ``has_intent · probe · restarts · max_restarts → arm``.
    * ``restore`` rows: the four bools → arm.
    * ``env`` rows: UTF-8-hex raw (``@none`` = unset) → enabled.
    """
    lines = ["# generated by tests/golden/generate_pull_intent_golden.py — do not hand-edit"]
    for row in parse_rows:
        codes = "\t".join(str(code) for code in row["codes"])
        expected = 0 if row["intent"] is None else 1
        lines.append(f"shape\t{row['id']}\t{codes}\t{expected}")
    for row in resume_rows:
        if not row["kernel_row"]:
            continue
        lines.append(
            "resume\t{id}\t{hi}\t{probe}\t{restarts}\t{cap}\t{arm}".format(
                id=row["id"],
                hi=int(row["has_intent"]),
                probe=int(row["probe"]),
                restarts=row["restarts"],
                cap=row["max_restarts"],
                arm=row["arm"],
            )
        )
    for row in restore_rows:
        lines.append(
            "restore\t{id}\t{dry}\t{started}\t{cur}\t{prior}\t{arm}".format(
                id=row["id"],
                dry=int(row["dry_run"]),
                started=int(row["started"]),
                cur=int(row["current_present"]),
                prior=int(row["prior_present"]),
                arm=row["arm"],
            )
        )
    for row in env_rows:
        lines.append(f"env\t{row['id']}\t{_hex_or_none(row['raw'])}\t{int(row['enabled'])}")
    return "\n".join(lines) + "\n"


def compute_all_tables(base_dir: Path) -> dict[str, list[dict[str, Any]]]:
    """Every table in one call (the generator + test entry point)."""
    return {
        "parse": compute_parse_table(base_dir),
        "resume": compute_resume_table(base_dir),
        "restore": compute_restore_table(base_dir),
        "env": compute_env_table(),
    }


def assert_no_env_override() -> None:
    """Fail loud when the ambient env would poison the resume-env table."""
    if os.environ.get(PULL_INTENT_RESUME_ENV) is not None:  # pragma: no cover — env guard
        raise SystemExit(f"unset {PULL_INTENT_RESUME_ENV} before computing the goldens")
