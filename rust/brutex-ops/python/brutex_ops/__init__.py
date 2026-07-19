"""brutex_ops — BruteX ops Rust kernels (PyO3 bridge).

DEC-NIGHTLY-RUST-CORE-001. The compiled cdylib ships as the private
submodule ``brutex_ops._brutex_ops``; this ``__init__.py`` re-exports its
symbols (the fno-math DEC-P0-055 mixed layout) so callers write
``import brutex_ops; brutex_ops.nightly_exit_code(...)``.

The ONE consumer is the default-OFF facade
``brutex/nightly/nightly_rust.py`` (``BRUTEX_NIGHTLY_RUST``) — the nightly
chain's pure decision kernels (break/exit-code law · audit-reconcile
tripwire · verdict/Telegram summary · launchd plist render + schedule
arithmetic), each token-for-token mirrored from ``brutex/nightly/`` with
the verbatim Python bodies retained as the live parity oracle. Nothing
else in ``brutex/`` imports this package, and the facade treats an
absent/stale install as "stay on the Python law" (one loud WARN), so a
missing wheel can never break a nightly run, an install, or a status
probe.
"""

from __future__ import annotations

from brutex_ops._brutex_ops import (
    __version__,
    nightly_audit_reconcile_detail,
    nightly_count_text,
    nightly_exit_code,
    nightly_first_breaker_index,
    nightly_parse_at,
    nightly_pmset_wake_command,
    nightly_render_launchd_plist,
    nightly_seconds_since_fire,
    nightly_summary_lines,
    nightly_verdict,
    nightly_xml_escape,
)

__all__ = [
    "__version__",
    "nightly_audit_reconcile_detail",
    "nightly_count_text",
    "nightly_exit_code",
    "nightly_first_breaker_index",
    "nightly_parse_at",
    "nightly_pmset_wake_command",
    "nightly_render_launchd_plist",
    "nightly_seconds_since_fire",
    "nightly_summary_lines",
    "nightly_verdict",
    "nightly_xml_escape",
]
