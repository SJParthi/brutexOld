"""The nightly sweep leg — ``cmd_sweep_all`` kwargs law + counts + contract probes
(DEC-SWEEP-NIGHTLY-AUTOMATION-001).

The nightly drives the sweep through the EXISTING ``cmd_sweep_all(**kwargs)``
entry point on the ``cmd_sweep_daemon`` dispatch precedent (cli.py:7306-7340):
:func:`nightly_sweep_kwargs` forwards EXACTLY the daemon key list and NOTHING
else, so ``allow_out_of_scope`` / ``acknowledge_sla_overrun`` /
``allow_missing_slices`` / ``sweep_cache_*`` take their safe defaults. The
three unattended escape flags are STRUCTURALLY absent (a signature-drift test
pins every produced key) AND dispatch-hard-refused at the CLI — deliberately
STRICTER than today's daemon, which dispatch-refuses ONLY
``--allow-out-of-scope`` (cli.py:7293-7305); this is stated, never claimed as
existing behavior.

Consequence (runbook): an SLA-overrun-sized grid makes the SWEEP link REFUSED
(``EXIT_USER_ERROR``) every night until the operator shrinks the grid or runs
a supervised manual ``sweep-all --acknowledge-sla-overrun`` — never a silent
overrun.

Counts are best-effort from the SUPERVISOR audit tail's completion payload
(the one VERIFIED field source, sweep_supervisor.py:822-836). Production
writes that tally via ``SweepGuards.audit`` (``sup_audit``) to
``<results_root>/audit/sweep-all-supervisor.jsonl`` (cli.py) with the REAL
engine action VALUE ``SUPERVISOR_ACTION_COMPLETE == "SUPERVISOR_COMPLETE"`` —
:func:`sweep_supervisor_audit_path` is the ONE path law and the reader keys on
the imported constant, never a re-minted literal (review round 2: the round-1
reader scanned the ingest ``sweep-all.jsonl`` for the alias NAME, so counts
were ALWAYS ``None`` and the T5 tripwire was dead). An absent/unreadable
payload is an HONEST ``None`` — rendered ``"?"``, NEVER a fabricated 0 or a
``-1`` sentinel (the ``bars_net_new`` precedent).
"""

from __future__ import annotations

import argparse
import contextlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import structlog

from brutex.nightly.chain import (
    EXIT_OK,
    EXIT_SWEEP_TUPLE_FAILED,
    EXIT_USER_ERROR,
    LinkResult,
    LinkStatus,
)

logger = structlog.get_logger(__name__)

__all__ = [
    "SWEEP_FORWARDED_KEYS",
    "SWEEP_SUPERVISOR_AUDIT_RELPATH",
    "UNATTENDED_ESCAPE_FLAGS",
    "ContractProbe",
    "nightly_sweep_kwargs",
    "probe_contract_artifacts",
    "read_sweep_tail_counts",
    "sweep_link_result",
    "sweep_supervisor_audit_path",
]

#: The safety escape hatches a nightly may NEVER carry — structurally absent
#: from the produced kwargs AND hard-refused at CLI dispatch.
UNATTENDED_ESCAPE_FLAGS: Final[tuple[str, ...]] = (
    "allow_out_of_scope",
    "acknowledge_sla_overrun",
    "allow_missing_slices",
)

#: EXACTLY the ``cmd_sweep_daemon`` → ``cmd_sweep_all`` dispatch key list
#: (cli.py:7306-7340) — the ONE forwarded-kwargs law. ``lake_root``/``start``/
#: ``end`` are filled by :func:`nightly_sweep_kwargs`; everything else comes
#: verbatim off the parsed args.
SWEEP_FORWARDED_KEYS: Final[tuple[str, ...]] = (
    "lake_root",
    "catalog_path",
    "results_root",
    "exchange",
    "segment",
    "symbols",
    "intervals",
    "start",
    "end",
    "directions",
    "k",
    "fib_anchors",
    "conditions",
    "sl",
    "tp",
    "top",
    "mode",
    "max_tuples",
    "max_combos",
    "data_snapshot_hash",
    "overfit_gate",
    "dsr_min",
    "options_sim",
    "options_expiry",
    "options_broker",
    "stage3_enable",
    "stage3_top_k",
    "stage3_fevals",
    "stage3_restarts",
)

#: Where PRODUCTION writes the supervisor completion tally, relative to the
#: sweep results root: ``cmd_sweep_all`` opens ``SweepGuards.audit``
#: (``sup_audit``) on ``out_root / "audit" / "sweep-all-supervisor.jsonl"``
#: (cli.py) — NOT the ingest ``sweep-all.jsonl``, which never carries the
#: completion payload. Pinned against cli.py's literal wiring in the stem test.
SWEEP_SUPERVISOR_AUDIT_RELPATH: Final[tuple[str, str]] = (
    "audit",
    "sweep-all-supervisor.jsonl",
)


def sweep_supervisor_audit_path(results_root: Path) -> Path:
    """The SUPERVISOR audit chain path under *results_root* — the ONE path law.

    This is the file :func:`read_sweep_tail_counts` must read; the ingest
    ``sweep-all.jsonl`` sibling never carries the ``SUPERVISOR_COMPLETE``
    tally (review round 2, finding 1: reading the ingest file kept counts
    ``None`` forever and left the T5 tripwire dead in production).
    """
    return results_root.joinpath(*SWEEP_SUPERVISOR_AUDIT_RELPATH)


def _supervisor_complete_action() -> str:
    """The REAL completion action VALUE (``"SUPERVISOR_COMPLETE"``).

    Imported from the engine constant, never a re-minted literal (Golden Rule
    #11 COMMON — the round-1 reader hardcoded the alias NAME
    ``"SUPERVISOR_ACTION_COMPLETE"``, which never matches a real audit line).
    Call-time import keeps this module's import wheel-free (the engine package
    pulls the Rust wheels; the reader only ever runs after a real sweep).
    """
    from brutex.engine.sweep_supervisor import SUPERVISOR_ACTION_COMPLETE

    return SUPERVISOR_ACTION_COMPLETE


def nightly_sweep_kwargs(
    args: argparse.Namespace,
    *,
    lake_root: Path | None,
    start_default: str,
    end_default: str,
) -> dict[str, Any]:
    """Pure translation: parsed nightly args → the ``cmd_sweep_all`` kwargs.

    Forwards EXACTLY :data:`SWEEP_FORWARDED_KEYS`; blank/absent ``--start`` /
    ``--end`` fill from the pull's own ``default_date_range()`` law
    (``[2020-01-01, previous IST day]`` — passed in as *start_default* /
    *end_default* so this stays pure). The pinned escape-flag trio is
    STRUCTURALLY absent — even a Namespace carrying them contributes nothing.
    """
    kwargs: dict[str, Any] = {}
    for key in SWEEP_FORWARDED_KEYS:
        if key == "lake_root":
            kwargs[key] = lake_root
        elif key == "start":
            value = getattr(args, "start", None)
            kwargs[key] = value if value else start_default
        elif key == "end":
            value = getattr(args, "end", None)
            kwargs[key] = value if value else end_default
        else:
            kwargs[key] = getattr(args, key)
    return kwargs


def read_sweep_tail_counts(audit_path: Path) -> Mapping[str, int] | None:
    """The last supervisor-completion payload's integer counters, or ``None``.

    *audit_path* must be the SUPERVISOR chain
    (:func:`sweep_supervisor_audit_path`); the match is the imported engine
    action VALUE (``"SUPERVISOR_COMPLETE"``). Best-effort by contract: ANY
    read/parse problem (absent file, torn line, no completion entry yet)
    returns ``None`` — the honest absence the report renders as ``"?"``. Only
    genuinely-integer payload values are kept (a bool is not a counter).
    """
    try:
        lines = audit_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    complete_action = _supervisor_complete_action()
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue  # a torn tail line — keep scanning upward
        if not isinstance(entry, dict) or entry.get("action") != complete_action:
            continue
        payload = entry.get("payload")
        if not isinstance(payload, dict):
            return None
        return {
            key: value
            for key, value in payload.items()
            if isinstance(value, int) and not isinstance(value, bool)
        }
    return None


def sweep_link_result(
    exit_code: int,
    *,
    audit_path: Path,
    counts: Mapping[str, int] | None,
) -> LinkResult:
    """Map a ``cmd_sweep_all`` exit code onto the SWEEP link (spec §4.3).

    ``EXIT_OK`` → OK · ``EXIT_USER_ERROR`` → REFUSED (its own preflight
    refusal; the stderr text is not capturable in-process, so the detail
    points at the sweep audit) · ``EXIT_SWEEP_TUPLE_FAILED`` → FAILED with
    ``exit_hint=6`` (the pass-through) · anything else → FAILED. *counts*
    (``None`` = honestly unknown) fold into the outcome verbatim.
    """
    folded = dict(counts) if counts is not None else {}
    if exit_code == EXIT_OK:
        return LinkResult(status=LinkStatus.OK, detail="sweep complete", counts=folded)
    if exit_code == EXIT_USER_ERROR:
        return LinkResult(
            status=LinkStatus.REFUSED,
            detail=f"sweep preflight refused (exit 1) — see sweep audit {audit_path}",
            counts=folded,
        )
    if exit_code == EXIT_SWEEP_TUPLE_FAILED:
        return LinkResult(
            status=LinkStatus.FAILED,
            detail=f"sweep finished with failed tuples (exit 6) — see sweep audit {audit_path}",
            counts=folded,
            exit_hint=EXIT_SWEEP_TUPLE_FAILED,
        )
    return LinkResult(
        status=LinkStatus.FAILED,
        detail=f"sweep exited {exit_code} — see sweep audit {audit_path}",
        counts=folded,
    )


@dataclass(frozen=True, slots=True)
class ContractProbe:
    """The honest P2/P3 contract-artifact presence probe (never a fabrication)."""

    found: tuple[str, ...]
    absent: tuple[str, ...]
    paths: Mapping[str, str]


#: The documented P2/P3 contract artifacts probed per run
#: (sweep-parallel-integration-contracts: ``<results_root>/runs/<run_id>/``).
_CONTRACT_ARTIFACTS: Final[tuple[tuple[str, str], ...]] = (
    ("run_manifest", "manifest.json"),
    ("top100", "top100.json"),
)


def probe_contract_artifacts(results_root: Path, *, since_utc: float) -> ContractProbe:
    """Probe the newest ``<results_root>/runs/<run_id>/`` dir for P2/P3 artifacts.

    Presence-only (the ADAPTER rule): the newest run dir with mtime >=
    *since_utc* is checked for ``manifest.json`` (P2's run manifest) and
    ``top100.json`` (P3's artifact — the documented v1.1 seam for the deferred
    R48 decay diff). No ``runs/`` dir / no fresh run ⇒ both HONESTLY absent.
    NEVER a hard failure, NEVER fabrication.
    """
    runs_dir = results_root / "runs"
    newest: Path | None = None
    newest_mtime = float("-inf")
    with contextlib.suppress(OSError):
        for child in runs_dir.iterdir():
            if not child.is_dir():
                continue
            with contextlib.suppress(OSError):
                mtime = child.stat().st_mtime
                if mtime >= since_utc and mtime > newest_mtime:
                    newest = child
                    newest_mtime = mtime
    found: list[str] = []
    absent: list[str] = []
    paths: dict[str, str] = {}
    for name, filename in _CONTRACT_ARTIFACTS:
        candidate = None if newest is None else newest / filename
        if candidate is not None and candidate.is_file():
            found.append(name)
            paths[name] = str(candidate)
        else:
            absent.append(name)
    if newest is not None:
        logger.info(
            "nightly.contract_probe",
            run_dir=str(newest),
            found=found,
            absent=absent,
        )
    return ContractProbe(found=tuple(found), absent=tuple(absent), paths=paths)
