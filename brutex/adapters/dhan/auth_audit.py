"""DhanHQ v2 auth audit — the Golden Rule #8 hash-chained ledger of every token action.

DEC-DHAN-AUTH-001. Every material auth action (mint, renew, adopt, store, lockdown,
exhausted) appends ONE line to a SHA-256 hash-chained JSONL audit log via the COMMON
orchestrator :class:`~brutex.orchestrator.audit.AuditWriter` (actor ``"dhan-auth"``, file
``~/.brutex/audit/dhan-auth.jsonl``). Two invariants keep it secret-safe + non-blocking:

* **Payload key ALLOWLIST** — only a fixed set of secret-free keys (outcome, attempts,
  reason class, param path, 8-hex fingerprint, source) may be recorded. A payload carrying a
  ``token`` / ``pin`` / ``totp`` (or any other) key raises AT CONSTRUCTION (a loud
  programming error in tests, never a silent leak in production).
* **Fail-SOFT append** — a disk/write failure NEVER blocks auth: it latches the audit OFF
  with one WARN (the flight-recorder law — observability failure must not change behaviour).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import structlog

from brutex.orchestrator.audit import AuditWriter

_LOG = structlog.get_logger("brutex.adapters.dhan.auth_audit")

#: The audit actor stamped on every Dhan auth entry.
DHAN_AUTH_ACTOR: str = "dhan-auth"

#: Audit actions (the ONLY actions this ledger emits).
ACTION_MINT: str = "DHAN_TOKEN_MINT"
ACTION_RENEW: str = "DHAN_TOKEN_RENEW"
ACTION_ADOPT: str = "DHAN_TOKEN_ADOPT"
ACTION_STORE: str = "DHAN_TOKEN_STORE"
ACTION_LOCKDOWN: str = "DHAN_AUTH_LOCKDOWN"
ACTION_EXHAUSTED: str = "DHAN_AUTH_EXHAUSTED"

#: The ONLY payload keys allowed — all secret-free. Any other key raises at record time.
_ALLOWED_PAYLOAD_KEYS: frozenset[str] = frozenset(
    {"outcome", "attempts", "reason_class", "param", "fingerprint", "source"}
)


def _default_audit_path() -> Path:
    """The default Dhan auth-audit path (``~/.brutex/audit/dhan-auth.jsonl``)."""
    return Path.home() / ".brutex" / "audit" / "dhan-auth.jsonl"


def _default_writer_factory(path: Path) -> AuditWriter:  # pragma: no cover — real fs open
    """Open (resume) the real chained :class:`AuditWriter` at *path* (actor ``dhan-auth``)."""
    return AuditWriter.open(path, actor=DHAN_AUTH_ACTOR, resume=True)


class DhanAuthAudit:
    """Fail-soft, allowlist-guarded writer for the Dhan auth hash chain.

    Parameters
    ----------
    path
        The audit-log path (``None`` uses ``~/.brutex/audit/dhan-auth.jsonl``).
    writer_factory
        Injected :class:`AuditWriter` opener (``None`` uses the real resume-open, which is
        ``# pragma: no cover``). Opened + closed per record (records are rare — a handful per
        day — so per-record open avoids any fd lifecycle across the reauth lock).
    """

    __slots__ = ("_degraded", "_path", "_writer_factory")

    def __init__(
        self,
        path: Path | None = None,
        *,
        writer_factory: Callable[[Path], AuditWriter] | None = None,
    ) -> None:
        self._path = path if path is not None else _default_audit_path()
        self._writer_factory = (
            writer_factory if writer_factory is not None else _default_writer_factory
        )
        #: Latched OFF after the first append failure (fail-soft — auth proceeds).
        self._degraded = False

    @property
    def degraded(self) -> bool:
        """``True`` once an append failure latched the audit off (observability only)."""
        return self._degraded

    def record(self, action: str, *, subject: str, **payload: object) -> None:
        """Append one hash-chained entry (allowlist-guarded, fail-soft).

        The payload keys are validated against :data:`_ALLOWED_PAYLOAD_KEYS` FIRST — a
        disallowed key (a ``token`` / ``pin`` / ``totp``, or any typo) raises
        :class:`ValueError` immediately (a loud programming error, never a silent leak). Only
        then is the write attempted; a write failure latches the audit off with ONE WARN and
        returns (auth is never blocked by an observability failure).
        """
        disallowed = set(payload) - _ALLOWED_PAYLOAD_KEYS
        if disallowed:
            raise ValueError(
                f"dhan auth audit payload has disallowed key(s) {sorted(disallowed)}; "
                f"only {sorted(_ALLOWED_PAYLOAD_KEYS)} are permitted (secret-free)"
            )
        if self._degraded:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._writer_factory(self._path) as writer:
                writer.append(action=action, subject=subject, payload=dict(payload))
        except Exception as exc:  # pylint: disable=broad-exception-caught — fail-soft, auth must proceed
            self._degraded = True
            _LOG.warning("dhan.audit.degraded", error_type=type(exc).__name__)


__all__ = [
    "ACTION_ADOPT",
    "ACTION_EXHAUSTED",
    "ACTION_LOCKDOWN",
    "ACTION_MINT",
    "ACTION_RENEW",
    "ACTION_STORE",
    "DHAN_AUTH_ACTOR",
    "DhanAuthAudit",
]
