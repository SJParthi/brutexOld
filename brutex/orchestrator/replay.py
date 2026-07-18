"""L3 audit-log replay oracle — closes 2026-05-25 honest-audit row 14
(dedup L3 replay was UNIMPLEMENTED).

Per the 5-agent audit (`docs/STANDARDS_100PCT_HONEST_AUDIT.md` row 14):

> L3 — Audit JSONL replay  | 🟡 SCAFFOLDED | 🟡 PARTIAL |
> Declared at bloom.py:11 as "bit-equality test oracle."
> **UNIMPLEMENTED in code** — no replay logic found in
> brutex/orchestrator/. Tests exist (test_audit_chain_properties.py)
> but verify the audit data structure, NOT the replay oracle that
> would validate dedup against L1/L2.

This module implements the missing replay oracle:

1. Walk the append-only audit JSONL (verified hash-chained by the
   existing `verify_chain` in `brutex.orchestrator.audit`).
2. Collect every entry whose ``action`` matches an operator-supplied
   filter (default ``"add"``) — these represent dedup-cache
   insertions.
3. Project the entries to a set of ``subject`` strings (dedup keys).
4. Compare against a caller-supplied snapshot of the LIVE dedup state
   — return a :class:`ReplayDiff` carrying ``missing_from_live``,
   ``extra_in_live``, and ``intersection``.

The oracle is **caller-driven**: WHERE the live snapshot comes from
(LMDB scan, Bloom dump, in-memory dict) is the caller's problem. The
module is purely about audit-log → set projection + diff calculation.
This keeps it agnostic of the underlying dedup tier — same code works
for any (Bloom, LMDB) or future (Redis, …) backend.

## Hash-chain verification

Replay implicitly relies on the audit log being un-tampered. Callers
SHOULD run :func:`brutex.orchestrator.audit.verify_chain` first; if
they don't, the replay still proceeds (so a partially-corrupted log
can still produce diagnostic output), but the resulting diff cannot be
trusted as a correctness proof.

## Why this is the right design

* **One-way dependency.** `replay` imports `audit` but NOT
  `bloom`/`store`. Caller passes the live snapshot as a `set[str]` —
  no circular module dependency, no coupling to a specific dedup
  backend.
* **Pure-function oracle.** `compute_replay_diff(...)` is deterministic
  + side-effect-free. Easy to unit-test exhaustively.
* **Action filter.** Auditors can replay "add" alone, or "add" +
  "remove" + "resolve" depending on the dedup semantics they're
  verifying.

## Decision: DEC-L3-REPLAY-ORACLE-001 (MIGRATION entry #241).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from brutex.orchestrator.audit import AuditEntry, AuditError, iter_entries

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────────
# Diff dataclass
# ────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ReplayDiff:
    """Result of comparing replayed audit-subjects against the live snapshot.

    Attributes
    ----------
    intersection
        Subjects present in BOTH replay and live. The "happy path" set.
    missing_from_live
        Subjects the audit log says were added but the live state
        does NOT contain. Indicates dedup-cache loss, eviction, or
        manual deletion that bypassed audit.
    extra_in_live
        Subjects the live state contains but the audit log does NOT.
        Indicates a write that bypassed the audit log (BUG — every
        material action must be audited per CLAUDE.md §2 rule 8).
    replayed_count
        Total number of subjects observed in the audit log
        (post-action-filter). Useful for reconciling against the
        audit's `seq` counter.

    Notes
    -----
    A clean dedup state has ``missing_from_live`` and ``extra_in_live``
    both empty. The :prop:`is_consistent` shortcut encodes this.
    """

    intersection: frozenset[str]
    missing_from_live: frozenset[str]
    extra_in_live: frozenset[str]
    replayed_count: int

    @property
    def is_consistent(self) -> bool:
        """Return True iff both missing + extra sets are empty."""
        return not self.missing_from_live and not self.extra_in_live


# ────────────────────────────────────────────────────────────────────────
# Replay logic
# ────────────────────────────────────────────────────────────────────────


def project_subjects(
    entries: Iterable[AuditEntry],
    *,
    action_filter: str = "add",
) -> frozenset[str]:
    """Project an iterable of :class:`AuditEntry` to the set of subjects
    whose action matches *action_filter*.

    Parameters
    ----------
    entries
        Any iterable of entries — the streaming
        :func:`brutex.orchestrator.audit.iter_entries` (one entry resident
        at a time; DEC-AUDIT-READERS-STREAM-001) or the materialised
        :func:`brutex.orchestrator.audit.read_entries` output (a list IS an
        Iterable — every pre-widening caller is unchanged). The replay
        oracle does NOT re-read the file — callers can chain
        verify_chain → iter_entries/read_entries → project_subjects in any
        combination they need.
    action_filter
        Action name to retain. Defaults to ``"add"`` since that's the
        canonical dedup-insertion action. Pass an empty string to
        retain all entries.

    Returns
    -------
    frozenset[str]
        Set of distinct ``subject`` strings whose ``action`` matched.
        Duplicates within the audit log collapse into a single set
        member (dedup is idempotent — adding the same subject twice
        is not an error in the live tier, so the set semantics is
        the right comparison shape).
    """
    if action_filter == "":
        return frozenset(entry.subject for entry in entries)
    return frozenset(entry.subject for entry in entries if entry.action == action_filter)


def compute_replay_diff(
    *,
    audit_subjects: frozenset[str],
    live_subjects: frozenset[str],
    replayed_count: int,
) -> ReplayDiff:
    """Compute the :class:`ReplayDiff` from a pair of pre-projected sets.

    Parameters
    ----------
    audit_subjects
        Subjects derived from the audit log via
        :func:`project_subjects`.
    live_subjects
        Subjects scanned from the live dedup tier (caller-supplied).
    replayed_count
        Number of subjects observed in the audit log post-filter
        (typically ``len(audit_subjects)`` unless duplicates).

    Returns
    -------
    ReplayDiff
        The diff carrying intersection / missing / extra sets.
    """
    return ReplayDiff(
        intersection=audit_subjects & live_subjects,
        missing_from_live=audit_subjects - live_subjects,
        extra_in_live=live_subjects - audit_subjects,
        replayed_count=replayed_count,
    )


def replay_audit_to_diff(
    *,
    audit_path: Path,
    live_subjects: frozenset[str],
    action_filter: str = "add",
) -> ReplayDiff:
    """End-to-end replay: stream audit JSONL → project subjects → diff
    against live snapshot.

    Convenience wrapper for the common case. Tests + advanced callers
    may prefer to chain the individual helpers
    (:func:`brutex.orchestrator.audit.iter_entries` /
    :func:`brutex.orchestrator.audit.read_entries`,
    :func:`project_subjects`, :func:`compute_replay_diff`).

    Peak memory is O(distinct matching subjects) — the frozenset IS the
    output — never O(chain): entries stream one at a time through
    :func:`brutex.orchestrator.audit.iter_entries` instead of the old
    whole-chain ``read_entries`` list (DEC-AUDIT-READERS-STREAM-001).

    Parameters
    ----------
    audit_path
        Path to the audit JSONL file. Passed straight through to
        :func:`brutex.orchestrator.audit.iter_entries`.
    live_subjects
        Set of subjects currently present in the live dedup tier.
    action_filter
        See :func:`project_subjects`.

    Raises
    ------
    AuditError
        If the audit file is missing (raised at the ``iter_entries`` call)
        or malformed at the JSONL line layer (raised lazily mid-projection,
        at the malformed committed line). Propagates verbatim — the same
        error contract the materialised reader had.
    """
    audit_subjects = project_subjects(iter_entries(audit_path), action_filter=action_filter)
    return compute_replay_diff(
        audit_subjects=audit_subjects,
        live_subjects=live_subjects,
        replayed_count=len(audit_subjects),
    )


def assert_dedup_consistent(
    *,
    audit_path: Path,
    live_subjects: frozenset[str],
    action_filter: str = "add",
) -> None:
    """Strict variant: raise :class:`AuditError` if replay vs live differ.

    Useful in the CI replay oracle test: the test calls this and
    expects either silence (consistent) or an explicit failure.

    Raises
    ------
    AuditError
        If ``missing_from_live`` or ``extra_in_live`` is non-empty.
        The exception message lists the first ~5 discrepancies of
        each kind so the operator can investigate without manually
        diffing the audit log.
    """
    diff = replay_audit_to_diff(
        audit_path=audit_path,
        live_subjects=live_subjects,
        action_filter=action_filter,
    )
    if diff.is_consistent:
        return

    missing_sample = sorted(diff.missing_from_live)[:5]
    extra_sample = sorted(diff.extra_in_live)[:5]
    raise AuditError(
        "L3 dedup-replay oracle: live state inconsistent with audit log. "
        f"missing_from_live={len(diff.missing_from_live)} "
        f"(first {missing_sample}); "
        f"extra_in_live={len(diff.extra_in_live)} "
        f"(first {extra_sample})."
    )


__all__ = (
    "ReplayDiff",
    "assert_dedup_consistent",
    "compute_replay_diff",
    "project_subjects",
    "replay_audit_to_diff",
)
