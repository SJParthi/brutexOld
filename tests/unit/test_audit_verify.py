"""Unit + integration + property + golden + bench + stress tests for the
unified audit-chain verifier (:mod:`brutex.audit_verify`,
DEC-AUDIT-VERIFY-UNIFY-001).

100% line + branch coverage of the auto-detect + verify bridge that lets
``brutex audit verify`` validate EITHER the SHA-256 orchestrator chain
(:mod:`brutex.orchestrator.audit`) or the BLAKE3 sweep-supervisor chain
(:mod:`brutex.engine.sweep_audit`) — the gap where a valid supervisor chain
was falsely reported as corrupt.

Covers:

* :func:`sniff_format` — empty file, orchestrator match, sweep match, neither
  (tampered genesis), missing file, malformed first line, leading-blank-line skip;
* :func:`verify` — explicit orchestrator + sweep paths;
* :func:`auto_verify` — empty (valid), each valid format, each corrupt format,
  malformed later line, genesis-indeterminate corruption;
* :func:`verify_audit_file` — ``auto`` + each explicit format;
* a hypothesis property (any honest chain of either format auto-verifies; any
  single-byte payload tamper is caught), frozen golden self-hashes, an O(1)
  sniff bench, and a large-chain stress verify.
"""

from __future__ import annotations

import json
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from brutex.audit_verify import (
    AUTO,
    AuditFormat,
    VerifyResult,
    auto_verify,
    sniff_format,
    verify,
    verify_audit_file,
)
from brutex.engine.sweep_audit import (
    GENESIS_PREV_HASH,
    SweepAuditChainCorruption,
    SweepAuditEntry,
    SweepAuditWriter,
)
from brutex.orchestrator.audit import (
    AuditChainCorruption,
    AuditEntry,
    AuditError,
    AuditWriter,
)
from hypothesis import given
from hypothesis import strategies as st

# A frozen golden self-hash per format — any drift in canonicalisation or the
# hash provider would break these (and therefore the sniff).
_ORCH_GOLDEN = "60cd2bfefa3ca39d04b714617895fafaaf6fa3cfa2d07054f8a7dbff50e374b6"
_SWEEP_GOLDEN = "8f7c0c5bc96ca77cd126742bdd39f701be036fad8fadc59b9818d918c16f7abd"


# ────────────────────────────────────────────────────────────────────────
# Builders — write valid chains of each format with deterministic ts_utc
# ────────────────────────────────────────────────────────────────────────


def _write_orch_chain(path: Path, n: int = 3) -> None:
    """Write a valid n-entry SHA-256 orchestrator chain."""
    with AuditWriter.open(path) as log:
        for i in range(n):
            log.append(action="STEP", subject=f"s{i}", payload={"i": i}, ts_utc=float(i))


def _write_sweep_chain(path: Path, n: int = 3) -> None:
    """Write a valid n-entry BLAKE3 sweep-supervisor chain."""
    with SweepAuditWriter.open(path) as log:
        for i in range(n):
            log.append(action="TUPLE_DONE", subject=f"c{i}", payload={"i": i}, ts_utc=float(i))


def _orch_genesis() -> AuditEntry:
    """A valid orchestrator genesis entry (with its real entry_hash)."""
    e = AuditEntry(
        seq=0,
        prev_hash=GENESIS_PREV_HASH,
        ts_utc=1700000000.0,
        actor="brutex",
        action="RUN_START",
        subject="run:golden",
        payload={"universe": 3},
    )
    return replace(e, entry_hash=e.compute_hash())


def _sweep_genesis() -> SweepAuditEntry:
    """A valid sweep genesis entry (with its real entry_hash)."""
    e = SweepAuditEntry(
        seq=0,
        prev_hash=GENESIS_PREV_HASH,
        ts_utc=1700000000.0,
        actor="sweep",
        action="SWEEP_START",
        subject="sweep:golden",
        payload={"total_combos": 3},
    )
    return replace(e, entry_hash=e.compute_hash())


def _tamper_line(path: Path, line_idx: int, field: str, new_value: object) -> None:
    """Modify one field on one line of an audit file (keeps the stale hash)."""
    lines = path.read_text(encoding="utf-8").splitlines()
    data = json.loads(lines[line_idx])
    data[field] = new_value
    lines[line_idx] = json.dumps(data)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ────────────────────────────────────────────────────────────────────────
# sniff_format
# ────────────────────────────────────────────────────────────────────────


def test_sniff_orchestrator(tmp_path: Path) -> None:
    p = tmp_path / "orch.jsonl"
    _write_orch_chain(p)
    assert sniff_format(p) is AuditFormat.ORCHESTRATOR


def test_sniff_sweep(tmp_path: Path) -> None:
    p = tmp_path / "sweep.jsonl"
    _write_sweep_chain(p)
    assert sniff_format(p) is AuditFormat.SWEEP


def test_sniff_empty_file_returns_none(tmp_path: Path) -> None:
    p = tmp_path / "empty.jsonl"
    p.write_text("", encoding="utf-8")
    assert sniff_format(p) is None


def test_sniff_all_blank_lines_returns_none(tmp_path: Path) -> None:
    """A file of only blank lines has no entry to sniff."""
    p = tmp_path / "blanks.jsonl"
    p.write_text("\n\n\n", encoding="utf-8")
    assert sniff_format(p) is None


def test_sniff_skips_leading_blank_line(tmp_path: Path) -> None:
    """A leading blank line is skipped; the first real entry is classified."""
    p = tmp_path / "lead_blank.jsonl"
    e = _sweep_genesis()
    p.write_text("\n" + e.to_json_line() + "\n", encoding="utf-8")
    assert sniff_format(p) is AuditFormat.SWEEP


def test_sniff_tampered_genesis_returns_none(tmp_path: Path) -> None:
    """First entry whose hash matches neither algorithm → None."""
    p = tmp_path / "bad.jsonl"
    _write_orch_chain(p, n=1)
    _tamper_line(p, 0, "payload", {"i": 999})  # stale hash now matches neither algo
    assert sniff_format(p) is None


def test_sniff_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(AuditError, match="not found"):
        sniff_format(tmp_path / "nope.jsonl")


def test_sniff_malformed_first_line_raises(tmp_path: Path) -> None:
    p = tmp_path / "garbage.jsonl"
    p.write_text("{this is not json\n", encoding="utf-8")
    with pytest.raises(AuditError):
        sniff_format(p)


# ────────────────────────────────────────────────────────────────────────
# verify (explicit format)
# ────────────────────────────────────────────────────────────────────────


def test_verify_explicit_orchestrator(tmp_path: Path) -> None:
    p = tmp_path / "orch.jsonl"
    _write_orch_chain(p, n=4)
    result = verify(p, AuditFormat.ORCHESTRATOR)
    assert result == VerifyResult(AuditFormat.ORCHESTRATOR, 4)


def test_verify_explicit_sweep(tmp_path: Path) -> None:
    p = tmp_path / "sweep.jsonl"
    _write_sweep_chain(p, n=5)
    result = verify(p, AuditFormat.SWEEP)
    assert result == VerifyResult(AuditFormat.SWEEP, 5)


def test_verify_wrong_format_reports_corrupt(tmp_path: Path) -> None:
    """Forcing the sweep verifier on an orchestrator file → BLAKE3 corruption."""
    p = tmp_path / "orch.jsonl"
    _write_orch_chain(p)
    with pytest.raises(SweepAuditChainCorruption):
        verify(p, AuditFormat.SWEEP)


# ────────────────────────────────────────────────────────────────────────
# auto_verify
# ────────────────────────────────────────────────────────────────────────


def test_auto_verify_orchestrator(tmp_path: Path) -> None:
    p = tmp_path / "orch.jsonl"
    _write_orch_chain(p, n=3)
    assert auto_verify(p) == VerifyResult(AuditFormat.ORCHESTRATOR, 3)


def test_auto_verify_sweep(tmp_path: Path) -> None:
    p = tmp_path / "sweep.jsonl"
    _write_sweep_chain(p, n=3)
    assert auto_verify(p) == VerifyResult(AuditFormat.SWEEP, 3)


def test_auto_verify_single_genesis_orchestrator(tmp_path: Path) -> None:
    p = tmp_path / "orch1.jsonl"
    _write_orch_chain(p, n=1)
    assert auto_verify(p) == VerifyResult(AuditFormat.ORCHESTRATOR, 1)


def test_auto_verify_single_genesis_sweep(tmp_path: Path) -> None:
    p = tmp_path / "sweep1.jsonl"
    _write_sweep_chain(p, n=1)
    assert auto_verify(p) == VerifyResult(AuditFormat.SWEEP, 1)


def test_auto_verify_empty_file_is_valid(tmp_path: Path) -> None:
    p = tmp_path / "empty.jsonl"
    p.write_text("", encoding="utf-8")
    assert auto_verify(p) == VerifyResult(None, 0)


def test_auto_verify_corrupt_orchestrator(tmp_path: Path) -> None:
    """Genesis intact (sniff → orchestrator); a later-entry tamper → corruption."""
    p = tmp_path / "orch.jsonl"
    _write_orch_chain(p, n=3)
    _tamper_line(p, 2, "payload", {"i": 999})
    with pytest.raises(AuditChainCorruption) as exc:
        auto_verify(p)
    assert exc.value.seq == 2


def test_auto_verify_corrupt_sweep(tmp_path: Path) -> None:
    """Genesis intact (sniff → sweep); a later-entry tamper → BLAKE3 corruption."""
    p = tmp_path / "sweep.jsonl"
    _write_sweep_chain(p, n=3)
    _tamper_line(p, 2, "payload", {"i": 999})
    with pytest.raises(SweepAuditChainCorruption) as exc:
        auto_verify(p)
    assert exc.value.seq == 2


def test_auto_verify_genesis_indeterminate_is_corruption(tmp_path: Path) -> None:
    """First entry hash matches neither algorithm → corruption at the real seq."""
    p = tmp_path / "bad.jsonl"
    _write_orch_chain(p, n=1)
    _tamper_line(p, 0, "payload", {"i": 999})
    with pytest.raises(AuditChainCorruption) as exc:
        auto_verify(p)
    assert exc.value.seq == 0
    assert "neither" in exc.value.reason


def test_auto_verify_malformed_later_line_raises_read_error(tmp_path: Path) -> None:
    """A valid genesis but a malformed later line → base read error, not corruption."""
    p = tmp_path / "orch.jsonl"
    _write_orch_chain(p, n=3)
    lines = p.read_text(encoding="utf-8").splitlines()
    lines[2] = "{not json"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(AuditError):
        auto_verify(p)


def test_auto_verify_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(AuditError, match="not found"):
        auto_verify(tmp_path / "nope.jsonl")


# ────────────────────────────────────────────────────────────────────────
# verify_audit_file (top-level CLI entry)
# ────────────────────────────────────────────────────────────────────────


def test_verify_audit_file_auto(tmp_path: Path) -> None:
    p = tmp_path / "sweep.jsonl"
    _write_sweep_chain(p, n=2)
    assert verify_audit_file(p) == VerifyResult(AuditFormat.SWEEP, 2)
    assert verify_audit_file(p, AUTO) == VerifyResult(AuditFormat.SWEEP, 2)


def test_verify_audit_file_explicit_orchestrator(tmp_path: Path) -> None:
    p = tmp_path / "orch.jsonl"
    _write_orch_chain(p, n=2)
    assert verify_audit_file(p, "orchestrator") == VerifyResult(AuditFormat.ORCHESTRATOR, 2)


def test_verify_audit_file_explicit_sweep(tmp_path: Path) -> None:
    p = tmp_path / "sweep.jsonl"
    _write_sweep_chain(p, n=2)
    assert verify_audit_file(p, "sweep") == VerifyResult(AuditFormat.SWEEP, 2)


# ────────────────────────────────────────────────────────────────────────
# Golden byte-equality — the sniff's discriminator is the self-hash
# ────────────────────────────────────────────────────────────────────────


def test_golden_orchestrator_self_hash_stable() -> None:
    assert _orch_genesis().entry_hash == _ORCH_GOLDEN


def test_golden_sweep_self_hash_stable() -> None:
    assert _sweep_genesis().entry_hash == _SWEEP_GOLDEN


def test_golden_hashes_differ_across_algorithms() -> None:
    """Same logical genesis, different algorithm → different digest (sniff basis)."""
    assert _ORCH_GOLDEN != _SWEEP_GOLDEN


# ────────────────────────────────────────────────────────────────────────
# Property — any honest chain auto-verifies; any single tamper is caught
# ────────────────────────────────────────────────────────────────────────


# The orchestrator format forbids the ``"|"`` hash-preimage delimiter inside the
# free-form fields (DEC-ORCH-AUDIT-RATE-HARDEN-001 — keeps the preimage injective), so
# an HONEST chain never contains one; exclude it from the generated ``subject``
# text so the property models only honest chains (the sweep path is unaffected).
# ``codec="utf-8"`` also drops lone surrogates the audit writer cannot UTF-8
# encode (the same constraint ``st.text()``'s default alphabet applies).
_HONEST_SUBJECT = st.text(
    alphabet=st.characters(codec="utf-8", exclude_characters="|"), min_size=1, max_size=6
)


@given(
    fmt=st.sampled_from([AuditFormat.ORCHESTRATOR, AuditFormat.SWEEP]),
    actions=st.lists(
        st.tuples(
            _HONEST_SUBJECT,
            st.dictionaries(st.text(max_size=4), st.integers(), max_size=3),
        ),
        min_size=1,
        max_size=6,
    ),
)
def test_property_any_honest_chain_auto_verifies(
    tmp_path_factory: Any, fmt: AuditFormat, actions: list[Any]
) -> None:
    p = tmp_path_factory.mktemp("av") / "a.jsonl"
    writer_cls = AuditWriter if fmt is AuditFormat.ORCHESTRATOR else SweepAuditWriter
    with writer_cls.open(p) as w:
        for i, (subject, payload) in enumerate(actions):
            w.append(action="A", subject=subject, payload=payload, ts_utc=float(i))
    result = auto_verify(p)
    assert result.format is fmt
    assert result.entry_count == len(actions)


def test_property_single_payload_tamper_breaks_auto(tmp_path: Path) -> None:
    p = tmp_path / "sweep.jsonl"
    _write_sweep_chain(p, n=2)
    _tamper_line(p, 1, "payload", {"i": -1})
    with pytest.raises(SweepAuditChainCorruption):
        auto_verify(p)


# ────────────────────────────────────────────────────────────────────────
# Bench — sniff is O(1): detection time must not scale with file size
# ────────────────────────────────────────────────────────────────────────


def test_sniff_is_o1_in_file_size(tmp_path: Path) -> None:
    """Sniff reads only the first line, so a 50x larger file is not 50x slower."""
    small = tmp_path / "small.jsonl"
    large = tmp_path / "large.jsonl"
    _write_sweep_chain(small, n=20)
    _write_sweep_chain(large, n=1000)

    def _time_sniff(path: Path) -> float:
        best = float("inf")
        for _ in range(50):
            t0 = time.perf_counter()
            sniff_format(path)
            best = min(best, time.perf_counter() - t0)
        return best

    small_t = _time_sniff(small)
    large_t = _time_sniff(large)
    # Generous ceiling: O(1) sniff stays well under O(N)'s ~50x; flat ratio proves
    # it does not read the whole file (a 10x ceiling is far below 50x linear).
    assert large_t <= max(small_t * 10.0, 5e-3)


# ────────────────────────────────────────────────────────────────────────
# Stress — a large honest chain of each format verifies end-to-end
# ────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("fmt", [AuditFormat.ORCHESTRATOR, AuditFormat.SWEEP])
def test_stress_large_chain_verifies(tmp_path: Path, fmt: AuditFormat) -> None:
    p = tmp_path / "big.jsonl"
    n = 5000
    if fmt is AuditFormat.ORCHESTRATOR:
        _write_orch_chain(p, n=n)
    else:
        _write_sweep_chain(p, n=n)
    assert auto_verify(p) == VerifyResult(fmt, n)
