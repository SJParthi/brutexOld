"""Startup env pre-flight — removed-env-flag detection (DEC-BOX-AUTODEPLOY-LANE-001 §12).

A REGISTRY-driven scan of the process env + every dotenv candidate file for
env flags that are either RETIRED launch-refusers (``BRUTEX_FILL_MODE`` — a
lingering export makes every sweep launch refuse with an error the operator
won't connect to an old env var, ``execution/fill_mode.py``) or live escapes
that silently defeat a safety layer on the unattended box
(``BRUTEX_SKIP_WHEEL_FRESHNESS`` — blinds the box-update deploy gate's belt +
the doctor staleness stages).

Laws (§12):

1. REGISTRY-driven — scanning = registry × sources; the dotenv reader is
   IMPORTED from :mod:`brutex.config.dotenv` (never copied), specifically
   :func:`~brutex.config.dotenv.load_dotenv_file` whose ``is_file()`` guard
   returns ``{}`` for ANY non-regular inode BEFORE any read (a raw
   ``read_text`` on a planted FIFO would hang the nightly forever — the
   #1614/#1832 regular-file-only discipline).
2. BOTH the process env AND every dotenv candidate are checked — a flag in
   several sources yields several findings (the file is what survives the
   next reboot; no masking).
3. DETECT + WARN only — never auto-unset, never edits any file; values are
   NEVER logged (the dotenv module's own secrets-never-logged law, kept for
   uniformity even though these values are not secrets).
4. The outcome is an additive ``env_preflight`` report block beside
   ``box_update``.
5. Degrade law: an unreadable/undecodable candidate folds into
   ``dotenv_errors`` (path + error type); scanning continues; the pre-flight
   NEVER raises (the chain's never-raise posture).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import structlog

from brutex.config.dotenv import load_dotenv_file

logger = structlog.get_logger(__name__)

__all__ = [
    "REMOVED_ENV_FLAGS",
    "EnvPreflightFinding",
    "EnvPreflightOutcome",
    "RemovedEnvFlag",
    "run_env_preflight",
]

#: The process-env source token (dotenv findings carry the exact file path).
PROCESS_ENV_SOURCE: Final[str] = "process_env"


@dataclass(frozen=True, slots=True)
class RemovedEnvFlag:
    """One registry row (the doctor ``_WheelSpec`` descriptor idiom).

    ``tier``: ``"removed"`` = a retired flag whose SET value makes launches
    REFUSE (the FILL_MODE class); ``"active_escape"`` = a live documented
    escape that silently defeats a safety layer on the unattended box.  Both
    tiers are DETECT+WARN only.
    """

    name: str
    removed_by: str
    remediation: str
    tier: str


#: The registry — growing it is a one-line append + one test row.
REMOVED_ENV_FLAGS: Final[tuple[RemovedEnvFlag, ...]] = (
    RemovedEnvFlag(
        name="BRUTEX_FILL_MODE",
        removed_by="DEC-FILL-WORST-CASE-ONLY-001 (MIGRATION.md #1825)",
        remediation=(
            "unset BRUTEX_FILL_MODE / remove the line from ~/.brutex/.env — removed by "
            "DEC-FILL-WORST-CASE-ONLY-001; launches refuse while it is set (any value, "
            "even the literal worst_case)"
        ),
        tier="removed",
    ),
    RemovedEnvFlag(
        name="BRUTEX_LAKE_RUST_LEDGER_RENDER",
        removed_by=(
            "DEC-RUST-RUNID-STORES-001 (MIGRATION.md #1962 — supersedes-in-part "
            "DEC-LAKE-RUST-LEDGERSTORE-CONVERT-001 #1918 on the render plane)"
        ),
        remediation=(
            "unset BRUTEX_LAKE_RUST_LEDGER_RENDER / remove the line from ~/.brutex/.env "
            "— the window-ledger render is no longer flag-selectable (the "
            "brutex_identity kernel render is canonical); every ledger load/save/append "
            "refuses while it is set (any value, its old truthy and falsy forms alike)"
        ),
        tier="removed",
    ),
    RemovedEnvFlag(
        name="BRUTEX_SKIP_WHEEL_FRESHNESS",
        removed_by="DEC-BOX-AUTODEPLOY-LANE-001 (active doctor escape, registered §4.5)",
        remediation=(
            "unset BRUTEX_SKIP_WHEEL_FRESHNESS on the unattended box — it blinds the "
            "box-update deploy gate's belt layer and the doctor wheel-staleness stages "
            "(the gate scrubs it from its own child env, but the doctor lanes stay blind "
            "while it is set)"
        ),
        tier="active_escape",
    ),
)


@dataclass(frozen=True, slots=True)
class EnvPreflightFinding:
    """One (registry row × source) hit — row fields copied VERBATIM, ``source`` minted."""

    name: str
    source: str
    tier: str
    removed_by: str
    remediation: str

    def to_dict(self) -> dict[str, object]:
        """Stable-key-order report projection."""
        return {
            "name": self.name,
            "source": self.source,
            "tier": self.tier,
            "removed_by": self.removed_by,
            "remediation": self.remediation,
        }


@dataclass(frozen=True, slots=True)
class EnvPreflightOutcome:
    """The pre-flight scan result (report-block source)."""

    flagged: tuple[EnvPreflightFinding, ...]
    dotenv_errors: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        """The additive ``env_preflight`` report block (stable key set)."""
        return {
            "flagged": [f.to_dict() for f in self.flagged],
            "dotenv_errors": list(self.dotenv_errors),
        }


def run_env_preflight(
    *,
    env: Mapping[str, str],
    dotenv_paths: Sequence[Path],
    load_file: Callable[[Path], dict[str, str]] = load_dotenv_file,
) -> EnvPreflightOutcome:
    """Scan *env* + every dotenv candidate for the registry flags; NEVER raises.

    One ``env_preflight.removed_flag_set`` WARN per finding (name + source +
    removed_by + remediation; values never logged).  An unreadable candidate
    file folds into ``dotenv_errors`` and scanning continues.
    """
    flagged: list[EnvPreflightFinding] = []
    errors: list[str] = []

    def _hit(row: RemovedEnvFlag, source: str) -> None:
        finding = EnvPreflightFinding(
            name=row.name,
            source=source,
            tier=row.tier,
            removed_by=row.removed_by,
            remediation=row.remediation,
        )
        flagged.append(finding)
        logger.warning(
            "env_preflight.removed_flag_set",
            name=row.name,
            source=repr(source),
            tier=row.tier,
            removed_by=row.removed_by,
            remediation=row.remediation,
        )

    for row in REMOVED_ENV_FLAGS:
        if row.name in env:
            _hit(row, PROCESS_ENV_SOURCE)
    for path in dotenv_paths:
        try:
            values = load_file(path)
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            errors.append(f"{path}: {type(exc).__name__}")
            logger.warning(
                "env_preflight.dotenv_unreadable",
                path=repr(str(path)),
                error=type(exc).__name__,
            )
            continue
        for row in REMOVED_ENV_FLAGS:
            if row.name in values:
                _hit(row, str(path))
    return EnvPreflightOutcome(flagged=tuple(flagged), dotenv_errors=tuple(errors))
