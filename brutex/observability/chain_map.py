"""Live chain map — pure per-link state derivation + atomic snapshot (R41, D-18).

The dashboard's chain-map widget (preflight → sweep → stage3 → cascade →
artifacts, each link green/amber/red — R41) polls ONE tiny JSON at
``<results_root>/runs/<run_id>/chain-map.json``::

    {"run_id": "...",
     "links": {"preflight" | "sweep" | "stage3" | "cascade" | "artifacts":
               {"state": "pending"|"running"|"green"|"amber"|"red",
                "detail": "..."}},
     "suspect": false, "updated_utc": 1789..., "schema_version": 1}

:func:`build_chain_map` is PURE (mappings in, dict out — fully
matrix-testable); :func:`write_chain_map` is the fail-soft atomic writer the
run core calls at each stage boundary (P4's O(1) poll target — the poll reads
one small file, honestly O(1) in run size because derivation cost was paid at
write time).

Derivation rules (priority order per link)
------------------------------------------
1. A RED stamp verdict (``HASH_MISMATCH`` / ``MALFORMED``) → ``red`` —
   evidence of a broken artifact always wins.
2. The currently-running stage (progress ``running=True`` + ``stage``) →
   ``running``.
3. A stamp-``OK`` verdict → ``green`` (validated artifact).
4. While running: links BEFORE the current stage → ``green`` (inferred
   complete); AFTER → ``pending``. Running with an unknown stage → every
   undecided link ``amber`` (honest uncertainty, never invented progress).
5. Not running, by exit-status ``state``: ``complete``/``suspect_complete``
   → ``green``; ``refused`` → preflight ``red`` (+ the refusal text), rest
   ``pending``; ``failed``/``halted`` → links before the last known stage
   ``green``, the last known stage ``red`` (+ ``error_type``), later links
   ``pending`` — or every undecided link ``amber`` when the stage is
   unknown; anything else → ``pending`` (no evidence).
6. SUSPECT floor (D-12): a run-level suspect latch downgrades every
   ``green`` link to ``amber`` — a suspect run never shows an all-green map.

All timestamps are observability-only (I-2); a chain-map write failure is ONE
``chain_map.write_failed`` WARN, never a raise (I-3, failure matrix A17).
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from enum import StrEnum
from pathlib import Path
from typing import Final

import structlog

from brutex.data.atomic_io import atomic_write_simple
from brutex.engine.run_layout import run_dir
from brutex.engine.sweep_ops_rust import routed_chain_link_state, sweep_ops_rust_enabled
from brutex.observability.stage_stamp import StampVerdict

_LOG = structlog.get_logger(__name__)

__all__ = [
    "CHAIN_MAP_FILENAME",
    "CHAIN_MAP_SCHEMA_VERSION",
    "LINKS",
    "LinkState",
    "build_chain_map",
    "chain_map_path",
    "write_chain_map",
]

#: The five chain links, in pipeline order (design §2).
LINKS: Final[tuple[str, ...]] = ("preflight", "sweep", "stage3", "cascade", "artifacts")

#: Chain-map schema version (design §3).
CHAIN_MAP_SCHEMA_VERSION: Final[int] = 1

#: The chain-map file name inside ``runs/<run_id>/`` (D-18).
CHAIN_MAP_FILENAME: Final[str] = "chain-map.json"

#: Exit-status states that mean the whole chain completed.
_COMPLETE_STATES: Final[frozenset[str]] = frozenset({"complete", "suspect_complete"})

#: Exit-status states that mean the run died at its last known stage.
_DEAD_STATES: Final[frozenset[str]] = frozenset({"failed", "halted"})


class LinkState(StrEnum):
    """One chain link's rendered state (R41 green/amber/red + lifecycle)."""

    PENDING = "pending"
    RUNNING = "running"
    GREEN = "green"
    AMBER = "amber"
    RED = "red"


def chain_map_path(results_root: Path, run_id: str) -> Path:
    """Return the chain-map path — ``<results_root>/runs/<run_id>/chain-map.json``.

    Parameters
    ----------
    results_root
        The sweep results root.
    run_id
        The run identity (D-1).

    Returns
    -------
    Path
        The snapshot location (D-18).
    """
    return run_dir(results_root, run_id) / CHAIN_MAP_FILENAME


def _str_or_none(source: Mapping[str, object] | None, key: str) -> str | None:
    """Read a string field from an optional mapping — anything else is ``None``."""
    if source is None:
        return None
    value = source.get(key)
    return value if isinstance(value, str) else None


def _link_state_python(
    *,
    link: str,
    idx: int,
    verdict_token: str | None,
    running: bool,
    last_stage: str | None,
    terminal: str | None,
    refusal_text: str | None,
    error_type: str | None,
) -> tuple[str, str]:
    """The verbatim per-link priority ladder over PRIMITIVES — the LIVE oracle.

    The DEC-RUST-SWEEP-OPS-001 oracle for ``routed_chain_link_state``:
    :func:`_link_state`'s rules 1-5 with the mapping reads hoisted to the
    wrapper (the T10-PR-2 sanctioned signature surgery — ``stamps.get`` +
    the two ``_str_or_none`` reads are pure lookups, so hoisting them is
    behavior-neutral and lets the burn-in compare LAWS, never mapping
    access). Returns ``(state_token, detail)`` in the ``LinkState`` value
    vocabulary; the ``or ""`` / ``or "unknown error"`` falsy laws are
    verbatim (an EMPTY string falls back exactly like ``None``).
    """
    if verdict_token in ("hash_mismatch", "malformed"):
        return (LinkState.RED.value, f"stamp {verdict_token}")
    if running and link == last_stage:
        return (LinkState.RUNNING.value, "in progress")
    if verdict_token == "ok":  # noqa: S105 # nosec B105 — stamp verdict, not a secret
        return (LinkState.GREEN.value, "stamp valid")
    if running:
        if last_stage is None:
            return (LinkState.AMBER.value, "running; stage unknown")
        if idx < LINKS.index(last_stage):
            return (LinkState.GREEN.value, "completed (inferred)")
        return (LinkState.PENDING.value, "not started")
    if terminal in _COMPLETE_STATES:
        return (LinkState.GREEN.value, "run complete")
    if terminal == "refused":
        if link == "preflight":
            refusal = refusal_text or ""
            return (LinkState.RED.value, f"refused: {refusal}")
        return (LinkState.PENDING.value, "not started")
    if terminal in _DEAD_STATES:
        if last_stage is None:
            return (LinkState.AMBER.value, f"run {terminal}; stage unknown")
        if idx < LINKS.index(last_stage):
            return (LinkState.GREEN.value, "completed (inferred)")
        if link == last_stage:
            error = error_type or "unknown error"
            return (LinkState.RED.value, f"run {terminal} at this stage ({error})")
        return (LinkState.PENDING.value, "not started")
    return (LinkState.PENDING.value, "no evidence")


def _link_state(
    *,
    link: str,
    idx: int,
    stamps: Mapping[str, StampVerdict],
    running: bool,
    last_stage: str | None,
    terminal: str | None,
    exit_status: Mapping[str, object] | None,
) -> tuple[LinkState, str]:
    """Derive ONE link's state per the module's priority rules.

    Extracts the primitives ONCE (verdict token — a non-``StampVerdict``
    mapping value reads as absent, exactly like the pre-extraction identity
    compares — plus the refusal/error strings), then runs the ladder LAW:
    the default-OFF DEC-RUST-SWEEP-OPS-001 routed seam with
    :func:`_link_state_python` as the LIVE oracle (flag OFF = the verbatim
    Python ladder).
    """
    verdict = stamps.get(link)
    verdict_token = verdict.value if isinstance(verdict, StampVerdict) else None
    refusal_text = _str_or_none(exit_status, "refusal_text")
    error_type = _str_or_none(exit_status, "error_type")

    def _oracle() -> tuple[str, str]:
        return _link_state_python(
            link=link,
            idx=idx,
            verdict_token=verdict_token,
            running=running,
            last_stage=last_stage,
            terminal=terminal,
            refusal_text=refusal_text,
            error_type=error_type,
        )

    if sweep_ops_rust_enabled():
        state_token, detail = routed_chain_link_state(
            link,
            idx,
            verdict_token,
            running,
            last_stage,
            terminal,
            refusal_text,
            error_type,
            python_law=_oracle,
        )
    else:
        state_token, detail = _oracle()
    return (LinkState(state_token), detail)


def build_chain_map(
    *,
    progress: Mapping[str, object] | None,
    stamps: Mapping[str, StampVerdict],
    suspect: bool,
    exit_status: Mapping[str, object] | None,
) -> dict[str, object]:
    """Derive the full chain-map document — PURE, no I/O (D-18).

    Parameters
    ----------
    progress
        The progress-file payload (its additive ``running`` / ``stage`` /
        ``run_id`` keys are read), or ``None`` when no progress exists.
    stamps
        Per-link :class:`StampVerdict` evidence, keyed by link name; a link
        with no stamped artifact is simply absent.
    suspect
        The run-level SUSPECT latch (D-12) — applies the amber floor.
    exit_status
        The exit-status payload (``state`` / ``refusal_text`` /
        ``error_type`` / ``run_id``), or ``None``.

    Returns
    -------
    dict[str, object]
        ``{"run_id", "links", "suspect", "schema_version"}`` —
        :func:`write_chain_map` adds ``updated_utc``.
    """
    raw_terminal = _str_or_none(exit_status, "state")
    terminal = raw_terminal.lower() if raw_terminal is not None else None
    running = progress is not None and progress.get("running") is True
    raw_stage = _str_or_none(progress, "stage")
    last_stage = raw_stage if raw_stage in LINKS else None
    links: dict[str, dict[str, str]] = {}
    for idx, link in enumerate(LINKS):
        state, detail = _link_state(
            link=link,
            idx=idx,
            stamps=stamps,
            running=running,
            last_stage=last_stage,
            terminal=terminal,
            exit_status=exit_status,
        )
        if suspect and state is LinkState.GREEN:
            state, detail = (LinkState.AMBER, f"{detail}; run marked SUSPECT")
        links[link] = {"state": state.value, "detail": detail}
    run_id = _str_or_none(exit_status, "run_id") or _str_or_none(progress, "run_id") or ""
    return {
        "run_id": run_id,
        "links": links,
        "suspect": bool(suspect),
        "schema_version": CHAIN_MAP_SCHEMA_VERSION,
    }


def write_chain_map(
    results_root: Path,
    run_id: str,
    chain: Mapping[str, object],
    *,
    wall: Callable[[], float] = time.time,
) -> None:
    """Atomically snapshot *chain* at ``runs/<run_id>/chain-map.json`` — fail-soft.

    Stamps ``run_id`` (the caller's authority — R43 one-id law) and
    ``updated_utc`` (observability-only wall clock) onto a copy, then writes
    via the COMMON atomic tmp→fsync→``os.replace``. Any failure is ONE
    ``chain_map.write_failed`` WARN, never a raise (I-3, A17).

    Parameters
    ----------
    results_root
        The sweep results root.
    run_id
        The run identity (overrides any ``run_id`` inside *chain*).
    chain
        The :func:`build_chain_map` document.
    wall
        Wall clock (injected for tests).
    """
    try:
        doc = dict(chain)
        doc["run_id"] = run_id
        doc["updated_utc"] = wall()
        doc.setdefault("schema_version", CHAIN_MAP_SCHEMA_VERSION)
        rendered = json.dumps(doc, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
        dest = chain_map_path(results_root, run_id)
        dest.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_simple(dest, lambda tmp: tmp.write_text(rendered, encoding="utf-8"))
    except Exception as exc:
        _LOG.warning(
            "chain_map.write_failed",
            run_id=run_id,
            error=str(exc),
            error_type=type(exc).__name__,
        )
