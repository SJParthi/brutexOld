"""Unit tests for :mod:`brutex.orchestrator.audit_seam_log`.

The structlog sink the audit module's Rust-kernel seam late-binds to
(DEC-AUDIT-CHAIN-RUST-PORT-001 / DEC-SWEEP-LOG-VOLUME-GUARD-001 #1630):
``audit.py`` itself must stay structlog-free (the
``TestAuditChainIsNotStructlog`` pin), so its seam-state WARN/ERROR
events route through this sibling. These tests pin the sink's two
surfaces plus the deliberate logger-name choice, and prove ``audit.py``
genuinely routes through it (the late-binding wiring).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from brutex.orchestrator import audit as audit_module
from brutex.orchestrator import audit_seam_log
from structlog.testing import capture_logs


class TestSinkSurfaces:
    """The two sink functions emit at the right level with the context kept."""

    def test_warn_emits_warning_with_context(self) -> None:
        with capture_logs() as logs:
            audit_seam_log.warn("audit.rust_kernel_unavailable", reason="wheel_missing")
        assert logs == [
            {
                "event": "audit.rust_kernel_unavailable",
                "log_level": "warning",
                "reason": "wheel_missing",
            }
        ]

    def test_error_emits_error_with_context(self) -> None:
        with capture_logs() as logs:
            audit_seam_log.error("audit.kernel_autoreverted", reason="burn_in_mismatch", n=1)
        assert logs == [
            {
                "event": "audit.kernel_autoreverted",
                "log_level": "error",
                "reason": "burn_in_mismatch",
                "n": 1,
            }
        ]

    def test_logger_is_named_for_the_audit_module(self) -> None:
        # Deliberate: the events belong to the audit seam, so operators
        # filtering by logger name find them under the OWNING module.
        assert audit_seam_log._LOG._logger_factory_args == ("brutex.orchestrator.audit",)  # type: ignore[attr-defined]


class TestAuditModuleRoutesThroughTheSink:
    """The late-bound wiring: audit.py's seam events reach THIS sink."""

    def test_warn_once_routes_through_the_sink(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[tuple[str, dict[str, object]]] = []

        def _spy(event: str, **context: object) -> None:
            calls.append((event, context))

        monkeypatch.setattr(audit_seam_log, "warn", _spy)
        audit_module._reset_rust_audit_for_tests()
        try:
            audit_module._warn_rust_once("audit.rust_kernel_unavailable", reason="wheel_missing")
            # Memoized: a second identical event never reaches the sink.
            audit_module._warn_rust_once("audit.rust_kernel_unavailable", reason="wheel_missing")
        finally:
            audit_module._reset_rust_audit_for_tests()
        assert calls == [("audit.rust_kernel_unavailable", {"reason": "wheel_missing"})]

    def test_autorevert_routes_through_the_sink(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[tuple[str, dict[str, object]]] = []

        def _spy(event: str, **context: object) -> None:
            calls.append((event, context))

        monkeypatch.setattr(audit_seam_log, "error", _spy)
        audit_module._reset_rust_audit_for_tests()
        try:
            audit_module._disable_rust_kernel("burn_in_mismatch", surface="compute_hash")
            audit_module._disable_rust_kernel("again")  # latched — no second emit
        finally:
            audit_module._reset_rust_audit_for_tests()
        assert calls == [
            ("audit.kernel_autoreverted", {"reason": "burn_in_mismatch", "surface": "compute_hash"})
        ]

    def test_audit_module_source_stays_free_of_the_logging_stack(self) -> None:
        # The #1630 pin's exact predicate, asserted here too so THIS suite
        # fails locally the moment anyone re-adds the import to audit.py.
        source = Path(audit_module.__file__).read_text(encoding="utf-8")
        assert "structlog" not in source
