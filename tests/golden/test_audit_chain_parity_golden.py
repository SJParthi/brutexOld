"""Audit-chain byte-layout parity golden (DEC-AUDIT-CHAIN-PARITY-GOLDEN-001).

The frozen, language-neutral bit-identity oracle for the Rust port of the
orchestrator SHA-256 audit chain (`brutex/orchestrator/audit.py`) — the
DEC-BXSNAP2-PARITY-GOLDEN-001 (#1695) pattern applied to the audit surface.
The port flips ON only when it reproduces these fixtures BIT-FOR-BIT
(ADR-0019 goldens-first law); until then this suite pins the CURRENT Python
writer so the frozen bytes can never drift silently.

What is frozen
--------------
* ``fixtures/audit_chain_parity_golden.jsonl`` — a REAL 10-entry chain written
  ONCE by ``AuditWriter`` under injected deterministic ``ts_utc`` floats, one
  entry per hazard class: CPython shortest-roundtrip float repr (integer-valued
  ts ``.0``, fraction ts, ``-0.0``, the ``1e16``/``1e17`` sci-notation
  thresholds, the ``5e-324`` subnormal), ints above float53,
  ``ensure_ascii=False`` unicode (₹ / M&M / combining sequence),
  case-sensitive key sort, nested canonicalization with list-order
  preservation, and escape-bearing free-form fields (only ``"|"`` is rejected).
* ``fixtures/audit_chain_parity_scalars.json`` — the unit-vector table
  (per-entry hash/line vectors + a special-ACTOR standalone vector +
  ``str(float)`` repr vectors + ``_canonical_json`` vectors) a port unit-tests
  BEFORE attempting whole-file parity.

NEVER-REGENERATE LAW: existing operator audit chains on disk share these byte
semantics — the fixtures are never regenerated (the generator refuses without
``BRUTEX_REGEN_AUDIT_PARITY_GOLDEN=1``, and the ``_EXPECTED_SHA256_*`` manifest
constants below force any regen through a reviewed test edit). A future
non-Python writer must reproduce the fixture bytes; the blast radius of a
divergence is every historical operator chain reading corrupt plus the #1596
backup byte-prefix law.

Scope cuts (deliberate): the ENGINE BLAKE3 ``sweep_audit`` chain is OUT (a
different chain; named follow-up F1 in ADR-0019); torn-tail/resume POLICY is
not re-frozen here (behavioral, already chaos+unit gated) — this golden freezes
BYTES. Platform note: byte identity assumes a ``"\\n"``-newline platform (the
CI Linux baseline; the writer opens text-mode where os.linesep == "\\n").
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any, Final

import pytest
from brutex.orchestrator.audit import (
    GENESIS_PREV_HASH,
    AuditEntry,
    AuditPayloadError,
    AuditWriter,
    _canonical_json,  # pyright: ignore[reportPrivateUsage] — house private-import test pattern
    verify_chain,
)

_FIXTURES: Final[Path] = Path(__file__).parent / "fixtures"
_CHAIN_FIXTURE: Final[Path] = _FIXTURES / "audit_chain_parity_golden.jsonl"
_SCALARS_FIXTURE: Final[Path] = _FIXTURES / "audit_chain_parity_scalars.json"

#: SHA-256 manifests over the RAW fixture bytes (the fixtures/README.md
#: convention). Any fixture edit — including a git newline/text mangle — fails
#: here first. NEVER updated without its own ledgered DEC.
_EXPECTED_SHA256_CHAIN: Final[str] = (
    "254ccca491031166df8ff8f0daab3a09bdc6d1a996b6eab50eb3088d5d6a30e8"
)
_EXPECTED_SHA256_SCALARS: Final[str] = (
    "064d869e591d86d35890c44dc28bfb8ebd45e512542b248eb7747928ef55fae7"
)

_EXPECTED_ENTRY_COUNT: Final[int] = 10


def _chain_bytes() -> bytes:
    return _CHAIN_FIXTURE.read_bytes()


def _chain_lines() -> list[bytes]:
    """The fixture's newline-terminated lines, as raw bytes."""
    raw = _chain_bytes()
    assert raw.endswith(b"\n"), "chain fixture must end newline-terminated (committed tail)"
    return [line + b"\n" for line in raw[:-1].split(b"\n")]


def _scalars_doc() -> dict[str, Any]:
    doc: dict[str, Any] = json.loads(_SCALARS_FIXTURE.read_text(encoding="utf-8"))
    return doc


def _entry_from_vector(vector: dict[str, Any]) -> AuditEntry:
    """Rebuild an :class:`AuditEntry` from one scalar unit-vector record."""
    return AuditEntry(
        seq=int(vector["seq"]),
        prev_hash=str(vector["prev_hash"]),
        ts_utc=float(vector["ts_utc"]),
        actor=str(vector["actor"]),
        action=str(vector["action"]),
        subject=str(vector["subject"]),
        payload=dict(vector["payload"]),
        entry_hash=str(vector["expected_entry_hash"]),
    )


# ── SHA-256 manifests (the byte tripwire) ────────────────────────────────────


def test_chain_fixture_sha256_manifest() -> None:
    """The chain fixture's raw bytes match the frozen manifest exactly."""
    assert hashlib.sha256(_chain_bytes()).hexdigest() == _EXPECTED_SHA256_CHAIN


def test_scalars_fixture_sha256_manifest() -> None:
    """The scalars fixture's raw bytes match the frozen manifest exactly."""
    assert hashlib.sha256(_SCALARS_FIXTURE.read_bytes()).hexdigest() == _EXPECTED_SHA256_SCALARS


# ── Layer 1: whole-chain byte/hash laws ──────────────────────────────────────


def test_chain_has_the_frozen_entry_count() -> None:
    assert len(_chain_lines()) == _EXPECTED_ENTRY_COUNT


def test_verify_chain_green_over_the_committed_fixture() -> None:
    """The fixture IS a valid audit log — the real verifier replays it clean."""
    assert verify_chain(_CHAIN_FIXTURE) == _EXPECTED_ENTRY_COUNT


def test_per_line_round_trip_byte_identity() -> None:
    """``from_json_line(line).to_json_line() + "\\n"`` reproduces each line's bytes."""
    for raw in _chain_lines():
        entry = AuditEntry.from_json_line(raw.decode("utf-8").rstrip("\n"))
        assert (entry.to_json_line() + "\n").encode("utf-8") == raw


def test_per_line_hash_recompute_and_chain_linkage() -> None:
    """``compute_hash() == entry_hash`` per line + genesis + prev-hash linkage."""
    prev = GENESIS_PREV_HASH
    for expected_seq, raw in enumerate(_chain_lines()):
        entry = AuditEntry.from_json_line(raw.decode("utf-8").rstrip("\n"))
        assert entry.seq == expected_seq
        assert entry.prev_hash == prev
        assert entry.compute_hash() == entry.entry_hash
        prev = entry.entry_hash


def test_writer_equivalence_fresh_writer_reproduces_fixture_bytes(tmp_path: Path) -> None:
    """A fresh REAL ``AuditWriter`` driven with the same sequence emits the SAME bytes.

    The strongest tripwire: the CURRENT production writer, given the fixture's
    own (action, subject, payload, ts_utc) sequence, must land byte-for-byte on
    the frozen file — so ANY serialization/hashing change in audit.py (or a
    future port wired in its place) fails here mechanically.
    """
    entries = [
        AuditEntry.from_json_line(raw.decode("utf-8").rstrip("\n")) for raw in _chain_lines()
    ]
    actors = {e.actor for e in entries}
    assert actors == {"brutex"}, "chain fixture must be single-actor for one-writer replay"
    replay = tmp_path / "replay.jsonl"
    with AuditWriter.open(replay, resume=False) as writer:
        for entry in entries:
            writer.append(
                action=entry.action,
                subject=entry.subject,
                payload=entry.payload,
                ts_utc=entry.ts_utc,
            )
    assert replay.read_bytes() == _chain_bytes()


# ── Layer 2: scalar unit-vector contract ─────────────────────────────────────


def test_scalar_entry_vectors_reproduce_hash_and_line() -> None:
    """Every per-entry vector reproduces its ``entry_hash`` AND its exact line."""
    doc = _scalars_doc()
    vectors: list[dict[str, Any]] = list(doc["entries"]) + list(doc["standalone_entries"])
    assert len(vectors) == _EXPECTED_ENTRY_COUNT + 1
    for vector in vectors:
        entry = _entry_from_vector(vector)
        assert entry.compute_hash() == vector["expected_entry_hash"]
        assert entry.to_json_line() == vector["expected_line"]


def test_scalar_entries_mirror_the_chain_fixture() -> None:
    """The scalars ``entries`` section is exactly the chain fixture, line-for-line."""
    doc = _scalars_doc()
    lines = [raw.decode("utf-8").rstrip("\n") for raw in _chain_lines()]
    assert [v["expected_line"] for v in doc["entries"]] == lines


def test_standalone_vector_pins_the_special_actor_contract() -> None:
    """The actor-with-escapes vector carries newline/quote/tab — legal fields."""
    doc = _scalars_doc()
    (vector,) = doc["standalone_entries"]
    actor = str(vector["actor"])
    assert "\n" in actor and '"' in actor and "\t" in actor
    assert "|" not in actor  # the ONE rejected character


def test_float_repr_vectors_match_cpython_str() -> None:
    """``str(value)`` reproduces every frozen shortest-round-trip repr string."""
    doc = _scalars_doc()
    vectors: list[dict[str, Any]] = doc["float_repr_vectors"]
    assert len(vectors) >= 14
    for vector in vectors:
        value = float(vector["value"])
        assert str(value) == vector["expected_str"]
    # The signed-zero pair must be present AND distinguishable (a JSON reader
    # that collapses -0.0 to 0.0 would silently pass a weaker check).
    reprs = {v["expected_str"] for v in vectors}
    assert {"0.0", "-0.0"} <= reprs
    neg_zero = [v for v in vectors if v["expected_str"] == "-0.0"]
    assert all(math.copysign(1.0, float(v["value"])) == -1.0 for v in neg_zero)


def test_canonical_json_vectors_match_the_private_helper() -> None:
    """``_canonical_json(payload)`` reproduces every frozen canonical string."""
    doc = _scalars_doc()
    vectors: list[dict[str, Any]] = doc["canonical_json_vectors"]
    assert len(vectors) >= 6
    for vector in vectors:
        assert _canonical_json(vector["payload"]) == vector["expected_json"]


def test_canonical_json_case_sensitive_key_sort_is_frozen() -> None:
    """The ``{"b":1,"a":2,"A":3}`` vector freezes code-point key order A,a,b."""
    doc = _scalars_doc()
    expected = [
        v["expected_json"]
        for v in doc["canonical_json_vectors"]
        if v["payload"] == {"b": 1, "a": 2, "A": 3}
    ]
    assert expected == ['{"A":3,"a":2,"b":1}']


# ── Negative wire pin: NaN refuses BEFORE write ──────────────────────────────


def test_nan_payload_refuses_before_write_and_chain_continues(tmp_path: Path) -> None:
    """A NaN payload raises ``AuditPayloadError`` with NO bytes written.

    The frozen contract's refusal arm: ``allow_nan=False`` rejects the append
    BEFORE the file is touched, the chain position does not advance, and the
    next good append continues at the same seq — so a port cannot 'fix' NaN by
    emitting the non-RFC bare token.
    """
    path = tmp_path / "nan.jsonl"
    with AuditWriter.open(path, resume=False) as writer:
        first = writer.append(action="RUN_START", subject="run:nan", ts_utc=1700000000.0)
        after_first = path.read_bytes()
        with pytest.raises(AuditPayloadError):
            writer.append(
                action="BAD",
                subject="nan:payload",
                payload={"x": float("nan")},
                ts_utc=1700000001.0,
            )
        assert path.read_bytes() == after_first  # nothing written
        second = writer.append(action="RUN_COMPLETE", subject="run:nan", ts_utc=1700000002.0)
    assert (first.seq, second.seq) == (0, 1)  # the chain never advanced past the refusal
    assert second.prev_hash == first.entry_hash
    assert verify_chain(path) == 2
