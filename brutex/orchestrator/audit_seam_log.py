"""Observability sink for the audit module's Rust-kernel seam.

The hash-chained audit writers in :mod:`brutex.orchestrator.audit` are
DIRECT file appends — deliberately structlog-free, so the tamper-evident
JSONL records can never be filtered, re-levelled, or reformatted by any
process logging configuration (DEC-SWEEP-LOG-VOLUME-GUARD-001,
``docs/MIGRATION.md`` #1630; pinned by
``tests/unit/test_cli_sweep_log_level.py::TestAuditChainIsNotStructlog``,
which asserts the audit-writer SOURCE contains no structlog reference at
all).

The slice-9 Rust audit-chain seam (DEC-AUDIT-CHAIN-RUST-PORT-001,
ADR-0019) still needs loud WARNING/ERROR observability for its
seam-STATE events (``audit.rust_kernel_unavailable`` /
``audit.rust_envelope_fallback`` / ``audit.kernel_autoreverted`` — none
of which is an audit RECORD), so that structlog surface lives HERE, in a
sibling module the pin does not cover, and ``audit.py`` reaches it only
through a late-bound function-local import at the moment an event
actually fires. The audit RECORD path (hash + line bytes + the direct
``write()``) never touches this module.
"""

from __future__ import annotations

import structlog

#: Deliberately named for the AUDIT module (not ``__name__``): the events
#: routed here are the audit seam's own (``audit.rust_*`` /
#: ``audit.kernel_autoreverted``), so operators filtering by logger keep
#: seeing them under the module that owns the seam.
_LOG = structlog.get_logger("brutex.orchestrator.audit")


def warn(event: str, **context: object) -> None:
    """Emit one seam-state WARNING event on behalf of ``audit.py``."""
    _LOG.warning(event, **context)


def error(event: str, **context: object) -> None:
    """Emit one seam-state ERROR event on behalf of ``audit.py``."""
    _LOG.error(event, **context)


__all__ = ["error", "warn"]
