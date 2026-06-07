"""Unified hash-chain verifier — auto-detect + verify EITHER audit format.

BruteX writes two hash-chained, append-only ``.jsonl`` audit logs with an
**identical line shape** (the same eight keys: ``seq``, ``prev_hash``,
``ts_utc``, ``actor``, ``action``, ``subject``, ``payload``, ``entry_hash``)
but **different chain hash algorithms**:

* :mod:`brutex.orchestrator.audit` — the ingest pipeline's **SHA-256** chain
  (e.g. the per-combo ``sweep-all.jsonl``).
* :mod:`brutex.engine.sweep_audit` — the sweep SUPERVISOR's **BLAKE3** chain
  (``sweep-all-supervisor.jsonl``, CLAUDE.md Golden Rule #4 — BLAKE3 is the
  BruteX hash).

Because the line shape is identical, ``brutex audit verify`` historically only
knew the SHA-256 verifier and therefore FALSELY reported a perfectly valid
BLAKE3 supervisor chain as ``AUDIT CHAIN CORRUPT`` (wrong verifier — not real
corruption). This module closes that operability gap: it auto-detects which
algorithm a file uses, then delegates to that format's own ``verify_chain`` —
**reusing** the two existing verifiers, never re-implementing the hash logic
(CLAUDE.md §11 COMMON).

## Auto-detection (the sniff)

The two ``compute_hash`` preimages are byte-for-byte identical (same field
order, same ``|`` separator, same canonical JSON) and differ ONLY in the hash
function, so the **first** entry's stored ``entry_hash`` is reproducible by
**exactly one** algorithm. :func:`sniff_format` parses the first non-empty
JSONL line and recomputes its self-hash under each algorithm; whichever
reproduces the stored hash names the format. This is O(1) — only the first
line is read (the cross-algorithm preimage-collision probability is ~2⁻²⁵⁶,
treated as impossible). Detection NEVER claims a valid chain is corrupt: a
valid chain's genesis entry always reproduces under its own algorithm.

## Honest envelope (CLAUDE.md Golden Rule #10)

* Detection is O(1) (first line only); **verification is inherently O(N)** over
  the N entries — each entry is one O(1) hash-recompute + compare. This is NOT
  an O(1)-total claim; that is physically impossible.
* Both underlying verifiers require a **seq-0 origin** — a legitimately
  rotated/truncated segment that starts at seq ≥ 1 is reported corrupt by
  either format. That is pre-existing behaviour this module inherits, stated
  plainly rather than masked.

## Exit-code contract (consumed by ``brutex audit verify``)

* valid chain (or empty file) → OK
* hash-chain integrity broken (incl. a genesis line whose hash matches neither
  algorithm) → corruption (``AuditChainCorruption`` / ``SweepAuditChainCorruption``)
* missing file / malformed JSON / wrong-shape line → the base
  ``AuditError`` / ``SweepAuditError`` (a user-input error, not corruption)

The CLI maps the corruption subclasses to the internal-error exit code and the
base errors to the user-error exit code — and MUST catch the corruption
subclasses FIRST, since each subclasses its base error class.

## Usage

::

    from brutex.audit_verify import verify_audit_file

    result = verify_audit_file(Path("sweep-all-supervisor.jsonl"))  # fmt="auto"
    print(result.format, result.entry_count)  # AuditFormat.SWEEP 3
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final

from brutex.engine import sweep_audit as _sweep
from brutex.orchestrator import audit as _orch

# ────────────────────────────────────────────────────────────────────────
# Format taxonomy
# ────────────────────────────────────────────────────────────────────────


class AuditFormat(StrEnum):
    """Which hash-chain algorithm an audit JSONL file uses."""

    #: SHA-256 chain — :mod:`brutex.orchestrator.audit` (ingest pipeline).
    ORCHESTRATOR = "orchestrator"
    #: BLAKE3 chain — :mod:`brutex.engine.sweep_audit` (sweep supervisor).
    SWEEP = "sweep"


#: The ``--format`` ``auto`` selector (detect the format from the file).
AUTO: Final[str] = "auto"


@dataclass(frozen=True, slots=True)
class VerifyResult:
    """Outcome of a successful chain verification.

    Attributes
    ----------
    format
        The detected / requested :class:`AuditFormat`, or ``None`` for an
        empty file (no entries → format is indeterminate but trivially valid).
    entry_count
        Number of entries verified (``0`` for an empty file).
    """

    format: AuditFormat | None
    entry_count: int


# ────────────────────────────────────────────────────────────────────────
# Format detection (the O(1) sniff)
# ────────────────────────────────────────────────────────────────────────


def _first_nonempty_line(path: Path) -> str | None:
    """Return the first non-blank line of *path*, or ``None`` if there is none.

    Reads at most up to the first non-blank line (O(1) for a normal log whose
    first line is the genesis entry), so detection never scans the whole file.

    Raises
    ------
    AuditError
        If *path* does not exist (mirrors the underlying verifiers' contract).
    """
    if not path.exists():
        raise _orch.AuditError(f"Audit log not found: {path}")
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.rstrip("\n")
            if line:
                return line
    return None


def _classify_line(line: str) -> AuditFormat | None:
    """Classify one JSONL *line* by which algorithm reproduces its self-hash.

    The two entry shapes are identical, so the orchestrator parser handles the
    line for both candidates; a malformed/wrong-shape line therefore raises
    :exc:`AuditError` here. Returns the matching :class:`AuditFormat`, or
    ``None`` when neither algorithm reproduces the stored ``entry_hash``
    (content or genesis tampered → the caller treats it as corruption).
    """
    entry = _orch.AuditEntry.from_json_line(line)
    if entry.entry_hash == entry.compute_hash():
        return AuditFormat.ORCHESTRATOR
    sweep_entry = _sweep.SweepAuditEntry.from_json_line(line)
    if sweep_entry.entry_hash == sweep_entry.compute_hash():
        return AuditFormat.SWEEP
    return None


def sniff_format(path: Path) -> AuditFormat | None:
    """Detect the chain format of *path* from its first non-empty line.

    Returns the matching :class:`AuditFormat`, or ``None`` when the file is
    empty OR the first entry's hash matches neither algorithm. O(1) — only the
    first line is read.

    Raises
    ------
    AuditError
        If *path* does not exist or its first line is malformed JSON / missing
        a required field.
    """
    first = _first_nonempty_line(path)
    if first is None:
        return None
    return _classify_line(first)


# ────────────────────────────────────────────────────────────────────────
# Verification
# ────────────────────────────────────────────────────────────────────────


def verify(path: Path, fmt: AuditFormat) -> VerifyResult:
    """Verify *path* under an explicitly chosen *fmt*.

    Delegates to the selected module's ``verify_chain`` (no hash logic here),
    propagating that module's exceptions unchanged — ``AuditError`` /
    ``AuditChainCorruption`` for ORCHESTRATOR, ``SweepAuditError`` /
    ``SweepAuditChainCorruption`` for SWEEP.
    """
    if fmt is AuditFormat.ORCHESTRATOR:
        return VerifyResult(AuditFormat.ORCHESTRATOR, _orch.verify_chain(path))
    return VerifyResult(AuditFormat.SWEEP, _sweep.verify_chain(path))


def auto_verify(path: Path) -> VerifyResult:
    """Auto-detect the format of *path* and verify it.

    An empty file is trivially valid (``VerifyResult(None, 0)``). A file whose
    first entry's hash matches neither algorithm is reported as corruption
    (carrying the entry's real ``seq``), since its content was altered.

    Raises
    ------
    AuditError
        Missing file or a malformed / wrong-shape line.
    AuditChainCorruption
        First entry undetectable (content/genesis tampered) OR — once a format
        is detected — a SHA-256 chain integrity break.
    SweepAuditChainCorruption
        A detected BLAKE3 chain's integrity break.
    """
    first = _first_nonempty_line(path)
    if first is None:
        return VerifyResult(None, 0)
    fmt = _classify_line(first)
    if fmt is None:
        entry = _orch.AuditEntry.from_json_line(first)
        raise _orch.AuditChainCorruption(
            seq=entry.seq,
            expected=entry.compute_hash(),
            actual=entry.entry_hash,
            reason=(
                "first entry hash matches neither the SHA-256 (orchestrator) "
                "nor the BLAKE3 (sweep) chain algorithm — content or genesis "
                "tampered, format indeterminate"
            ),
        )
    return verify(path, fmt)


def verify_audit_file(path: Path, fmt: str = AUTO) -> VerifyResult:
    """Top-level entry the CLI calls — verify *path* under *fmt*.

    Parameters
    ----------
    path
        The audit JSONL file.
    fmt
        ``"auto"`` (default) auto-detects the format; ``"orchestrator"`` or
        ``"sweep"`` force that algorithm (an :class:`AuditFormat` member value).

    Returns
    -------
    VerifyResult
        The detected/forced format + the number of entries verified.
    """
    if fmt == AUTO:
        return auto_verify(path)
    return verify(path, AuditFormat(fmt))


__all__ = [
    "AUTO",
    "AuditFormat",
    "VerifyResult",
    "auto_verify",
    "sniff_format",
    "verify",
    "verify_audit_file",
]
