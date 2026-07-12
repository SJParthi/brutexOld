"""DhanHQ v2 adapter — AUTH ONLY (PR-1, DEC-DHAN-AUTH-001).

The re-admitted second data source (ADR-0020, superseding ADR-0009 §5; operator decision
2026-07-12). THIS package currently holds ONLY the auth control plane — headless PIN+TOTP
mint + RenewToken, an AWS-SSM SecureString token store BruteX self-mints into, and the
thread-safe / single-flight / lockdown-guarded reauth spine. NO fetch / candle / data-plane
logic lives here yet (that is PR-3). The seam PR-3 consumes is
:meth:`~brutex.adapters.dhan.reauth.DhanTokenManager.current_token` — a plain header string.
"""

from __future__ import annotations

__all__: list[str] = []
