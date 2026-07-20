"""Generate the frozen audit-chain byte-layout parity goldens (DEC-AUDIT-CHAIN-PARITY-GOLDEN-001).

Run ONCE from the repo root:

    python tests/golden/generate_audit_chain_parity_golden.py

It drives the REAL :class:`brutex.orchestrator.audit.AuditWriter` (``resume=False``,
injected deterministic ``ts_utc`` floats) over one hazard-class entry per
CPython-serialization trap — float repr zoo, unicode ``ensure_ascii=False``,
case-sensitive key sort, nested canonicalization, escape-bearing free-form
fields — and freezes:

* ``tests/golden/fixtures/audit_chain_parity_golden.jsonl`` — the EXACT on-disk
  chain file (10 newline-terminated lines, seq 0..9). Because it IS a valid
  audit log, ``verify_chain`` runs on it directly.
* ``tests/golden/fixtures/audit_chain_parity_scalars.json`` — the unit-vector
  table (per-entry hash/line vectors + ``str(float)`` repr vectors +
  ``_canonical_json`` vectors) a non-Python port unit-tests against BEFORE
  attempting whole-file parity (the bxsnap2 Layer-2 shape, #1695).

NEVER regenerate these fixtures (Golden Rule #1): existing operator audit
chains on disk share these byte semantics, so the first capture IS the oracle —
a future port must reproduce it, never the other way around. The refusal is
MECHANICAL (the ``BRUTEX_REGEN_PREOPEN_GOLDEN`` precedent): when any fixture
already exists, :func:`main` raises ``SystemExit`` instead of silently
overwriting, unless the explicit escape hatch
``BRUTEX_REGEN_AUDIT_PARITY_GOLDEN=1`` is set — and even then the consuming
test's SHA-256 manifest constants force the regen through a reviewed test edit.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Final

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from brutex.orchestrator.audit import (
    GENESIS_PREV_HASH,
    AuditEntry,
    AuditWriter,
    read_entries,
)

FIXTURES: Final[Path] = Path(__file__).resolve().parent / "fixtures"
CHAIN_FIXTURE: Final[Path] = FIXTURES / "audit_chain_parity_golden.jsonl"
SCALARS_FIXTURE: Final[Path] = FIXTURES / "audit_chain_parity_scalars.json"

#: The explicit overwrite escape hatch — the ONLY way to regenerate an existing
#: fixture. The audit fixture is a NEVER-regenerate byte law (operator chains on
#: disk share these byte semantics); the hatch exists solely so a deliberate,
#: ledger-worthy act is mechanically distinguishable from an accident.
REGEN_ENV: Final[str] = "BRUTEX_REGEN_AUDIT_PARITY_GOLDEN"

#: The deterministic (ts_utc, action, subject, payload) drive sequence — one
#: entry per hazard class. ``actor`` stays the writer default ``"brutex"`` so
#: the writer-equivalence test can re-drive ONE fresh writer.
CHAIN_SEQUENCE: Final[tuple[tuple[float, str, str, dict[str, Any]], ...]] = (
    # seq 0 — genesis: GENESIS_PREV_HASH + empty payload "{}"; integer-valued
    # ts float renders the trailing ".0" through str(float).
    (1700000000.0, "RUN_START", "run:parity-golden", {}),
    # seq 1 — shortest-roundtrip fraction ts (repr rounding).
    (1718000000.123456, "PHASE_START", "phase:A_FRACTION_TS", {"note": "fraction ts"}),
    # seq 2 — payload float zoo: 0.1 repr, negative zero, the 1e16/1e17
    # sci-notation thresholds, the 5e-324 subnormal, an int above float53.
    (
        1700000000.1,
        "FLOAT_ZOO",
        "zoo:floats",
        {
            "a": 0.1,
            "neg_zero": -0.0,
            "sci16": 1e16,
            "sci17": 1e17,
            "subnormal": 5e-324,
            "big_int": 2**60,
            "plain": 1.5,
        },
    ),
    # seq 3 — unicode under ensure_ascii=False: rupee sign, ampersand,
    # a combining sequence (e + COMBINING ACUTE ACCENT — NOT precomposed).
    (
        1700000001.5,
        "UNICODE_PAYLOAD",
        "NSE-M&M",
        {"currency": "₹1,00,000.50", "name": "M&M", "combining": "é"},
    ),
    # seq 4 — case-sensitive key sort: "A" < "a" < "b" in code-point order.
    (1700000002.25, "KEY_SORT", "sort:case", {"b": 1, "a": 2, "A": 3}),
    # seq 5 — nested payload: keys sorted at EVERY depth, list ORDER preserved,
    # null/true/false literals.
    (
        1700000003.0,
        "NESTED",
        "nest:deep",
        {"outer": {"z": 1, "a": [3, 1, 2], "flags": [True, False, None]}, "b": None},
    ),
    # seq 6 — escape-bearing free-form fields: action carries a double quote,
    # subject a newline + tab (legal — only "|" is rejected), payload the
    # escape zoo. Exercises the JSON escape path in the line bytes AND the
    # raw-bytes preimage.
    (
        1700000004.75,
        'EDGE"QUOTE',
        "sub\nject\twith tab",
        {"esc": 'line\nbreak\ttab"quote'},
    ),
    # seq 7 — small-magnitude floats: the 1e-5 exponent flip, the 1e-4/1e15
    # fixed-notation neighbours, a negative.
    (
        1700000005.5,
        "SMALL_FLOATS",
        "zoo:small",
        {"tiny": 1e-05, "neg": -1.5, "fixed15": 1e15, "fixed4": 0.0001},
    ),
    # seq 8 — integer boundaries: 2**53 (float-exactness edge) and its
    # successor, int64 max. JSON ints are arbitrary-precision in Python; a port
    # must NOT round-trip them through f64.
    (
        1700000006.125,
        "INT_BOUNDS",
        "zoo:ints",
        {"i53": 2**53, "i53p1": 2**53 + 1, "imax64": 2**63 - 1},
    ),
    # seq 9 — tail: pins seq stringification + chain linking over 10 entries.
    (1700000007.0, "RUN_COMPLETE", "run:parity-golden", {"total": 9}),
)

#: str(float) repr vectors — the exact CPython shortest-round-trip strings a
#: port's f64 formatter must match (the ``str(self.ts_utc)`` preimage step and
#: the json.dumps float rendering share this formatter).
FLOAT_REPR_VALUES: Final[tuple[float, ...]] = (
    0.0,
    -0.0,
    0.1,
    1.5,
    1e15,
    1e16,
    1e17,
    1e-4,
    1e-5,
    5e-324,
    1700000000.0,
    1718000000.123456,
    1700000006.125,
    2 / 3,
)

#: _canonical_json payload vectors — the exact canonical-JSON strings
#: (sort_keys, separators=(",", ":"), ensure_ascii=False, allow_nan=False).
CANONICAL_JSON_PAYLOADS: Final[tuple[dict[str, Any], ...]] = (
    {},
    {"b": 1, "a": 2, "A": 3},
    {"outer": {"z": 1, "a": [3, 1, 2]}, "flags": [True, False, None]},
    {"currency": "₹", "name": "M&M", "combining": "é"},
    {"neg_zero": -0.0, "sci": 1e16, "sub": 5e-324, "big": 2**60},
    {"esc": 'line\nbreak\ttab"quote'},
)


def _entry_vector(entry: AuditEntry) -> dict[str, Any]:
    """Project one committed entry into the scalar unit-vector shape."""
    return {
        "seq": entry.seq,
        "prev_hash": entry.prev_hash,
        "ts_utc": entry.ts_utc,
        "actor": entry.actor,
        "action": entry.action,
        "subject": entry.subject,
        "payload": entry.payload,
        "expected_entry_hash": entry.entry_hash,
        "expected_line": entry.to_json_line(),
    }


def _standalone_special_actor_vector() -> dict[str, Any]:
    """A direct-construction vector whose ACTOR carries escapes.

    ``AuditWriter`` stamps ONE actor per writer, so the chain fixture keeps the
    default ``"brutex"`` (writer-equivalence needs one writer). The
    actor-with-specials contract is pinned here instead, via direct
    :class:`AuditEntry` construction — newline / quote / tab are legal in the
    free-form fields (only ``"|"`` is rejected).
    """
    base = AuditEntry(
        seq=0,
        prev_hash=GENESIS_PREV_HASH,
        ts_utc=1700000042.5,
        actor='ac\ntor"\twith specials',
        action="STANDALONE",
        subject="actor:specials",
        payload={"k": "v"},
    )
    entry = AuditEntry(
        seq=base.seq,
        prev_hash=base.prev_hash,
        ts_utc=base.ts_utc,
        actor=base.actor,
        action=base.action,
        subject=base.subject,
        payload=base.payload,
        entry_hash=base.compute_hash(),
    )
    return _entry_vector(entry)


def build_chain_bytes() -> bytes:
    """Drive the REAL AuditWriter over :data:`CHAIN_SEQUENCE`; return file bytes."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "audit_chain_parity_golden.jsonl"
        with AuditWriter.open(path, resume=False) as writer:
            for ts_utc, action, subject, payload in CHAIN_SEQUENCE:
                writer.append(action=action, subject=subject, payload=payload, ts_utc=ts_utc)
        return path.read_bytes()


def build_scalars_doc(chain_bytes: bytes) -> dict[str, Any]:
    """Build the unit-vector table from the committed chain + standalone vectors."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "chain.jsonl"
        path.write_bytes(chain_bytes)
        entries = read_entries(path)
    return {
        "note": (
            "Unit-vector contract for a future non-Python audit-chain port "
            "(DEC-AUDIT-CHAIN-PARITY-GOLDEN-001). NEVER regenerated — the byte "
            "semantics are shared by existing on-disk operator chains."
        ),
        "hash_preimage": (
            "sha256(str(seq) | prev_hash | str(ts_utc) | actor | action | subject "
            "| canonical_json(payload)) — '|' is a literal delimiter byte; "
            "str(float) is CPython shortest-round-trip; canonical_json is "
            "json.dumps(sort_keys=True, separators=(',', ':'), "
            "ensure_ascii=False, allow_nan=False)"
        ),
        "entries": [_entry_vector(e) for e in entries],
        "standalone_entries": [_standalone_special_actor_vector()],
        "float_repr_vectors": [{"value": v, "expected_str": str(v)} for v in FLOAT_REPR_VALUES],
        "canonical_json_vectors": [
            {
                "payload": p,
                "expected_json": json.dumps(
                    p, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
                ),
            }
            for p in CANONICAL_JSON_PAYLOADS
        ],
    }


def main() -> None:
    existing = [p for p in (CHAIN_FIXTURE, SCALARS_FIXTURE) if p.exists()]
    if existing and os.environ.get(REGEN_ENV) != "1":
        names = ", ".join(str(p) for p in existing)
        raise SystemExit(
            "REFUSED: audit-chain parity fixture(s) already exist and are the "
            f"frozen byte oracle ({names}); they are NEVER regenerated — existing "
            "operator audit chains on disk share these byte semantics (Golden "
            f"Rule #1). Set {REGEN_ENV}=1 ONLY for a deliberate, ledgered act."
        )
    chain_bytes = build_chain_bytes()
    scalars = build_scalars_doc(chain_bytes)
    FIXTURES.mkdir(parents=True, exist_ok=True)
    CHAIN_FIXTURE.write_bytes(chain_bytes)
    print(f"wrote {CHAIN_FIXTURE} ({len(chain_bytes)} bytes)")
    scalars_bytes = (json.dumps(scalars, indent=1, ensure_ascii=False) + "\n").encode("utf-8")
    SCALARS_FIXTURE.write_bytes(scalars_bytes)
    print(f"wrote {SCALARS_FIXTURE} ({len(scalars_bytes)} bytes)")


if __name__ == "__main__":
    main()
