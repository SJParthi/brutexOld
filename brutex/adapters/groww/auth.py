"""Groww TOTP authentication — Surface A (DEC-GROWW-ADAPTER-DESIGN-001 §13.2).

Extracts + hardens the TOTP auth that was inline in
:meth:`~brutex.adapters.groww.client.GrowwCandleFetcher.from_env`. Per
``CLAUDE.md §6`` + ``docs/TRACK1_GROWW_ADAPTER.md §2``, only the TOTP flow is
accepted (the API-key flow's daily approval violates §11 AUTOMATED).

:class:`GrowwAuth` owns the access-token lifecycle:

* **Mint** — ``pyotp.TOTP(secret).now()`` → ``GrowwAPI.get_access_token`` →
  cache the token to a mode-0600 file so a process restart reuses it (the
  TOTP token has "No Expiry" per the Groww docs).
* **Cache read** — loads the token file IF it is mode 0600; a world-readable
  token file is REFUSED (possible secret leak — surface it, never silently
  reuse). A corrupt/unreadable file is treated as "no cache" → re-mint.
* **Refresh** — the loop's ``reauth`` hook (called on a 401 / token
  rollover); re-mints with a single clock-skew retry (a TOTP code minted at
  the edge of its 30 s window can land expired — wait one window, re-mint
  with a fresh code) before raising :class:`GrowwAuthError` (→ HALT).

Testability: every SDK-facing operation is an injected callable, so this
module imports — and unit-tests run — with no ``growwapi`` / ``pyotp``
installed. :meth:`GrowwAuth.from_env` wires the real SDK (``no cover`` —
live network).
"""

from __future__ import annotations

import json
import os
import stat
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from brutex.models import BruteXError

#: Default cached-token path (``docs/TRACK1_GROWW_ADAPTER.md §2.1``).
_DEFAULT_TOKEN_PATH: Final[Path] = Path.home() / ".brutex" / "groww" / "access_token.json"

#: One TOTP step — the clock-skew retry waits this long for a fresh code.
_TOTP_STEP_SECONDS: Final[float] = 30.0

#: Permission bits that, if set, mean the token file is group/world accessible.
_INSECURE_MODE_BITS: Final[int] = 0o077


class GrowwAuthError(BruteXError):
    """Authentication could not be established (bad creds, or insecure token file).

    A terminal condition — the operator must intervene (fix the TOTP secret or
    the token-file permissions). ``str(exc)`` returns the redacted
    ``safe_message`` so no token/secret reaches the audit log.
    """


def _utc_now_iso() -> str:
    """Return the current UTC instant as an ISO-8601 string (audit timestamp)."""
    return datetime.now(UTC).isoformat()


@dataclass(slots=True)
class GrowwAuth:
    """TOTP access-token lifecycle for the Groww adapter.

    Parameters
    ----------
    api_key
        The Groww TOTP token (``BRUTEX_GROWW_API_KEY``).
    totp_secret
        The base32 TOTP secret (``BRUTEX_GROWW_TOTP_SECRET``).
    token_path
        Where the minted access token is cached (mode 0600).
    mint_token
        ``(api_key, totp_code) -> access_token``. The real impl calls
        ``GrowwAPI.get_access_token``; tests inject a fake.
    build_session
        ``access_token -> GrowwAPI``. Builds the authenticated SDK client.
    totp_now
        ``secret -> current 6-digit code``. Real impl is ``pyotp.TOTP(s).now()``.
    auth_error
        The SDK exception type raised on an auth failure (caught for the
        clock-skew retry). Injected to avoid importing the SDK.
    sleep
        Injected for the clock-skew wait (tests pass a no-op).
    skew_retry_seconds
        How long to wait before the single clock-skew re-mint.
    """

    api_key: str
    totp_secret: str
    token_path: Path
    mint_token: Callable[[str, str], str]
    build_session: Callable[[str], Any]
    totp_now: Callable[[str], str]
    auth_error: type[BaseException]
    sleep: Callable[[float], None] = time.sleep
    skew_retry_seconds: float = _TOTP_STEP_SECONDS
    _lock: threading.Lock = field(init=False, default_factory=threading.Lock, repr=False)
    _session: Any = field(init=False, default=None, repr=False)

    def session(self) -> Any:
        """Return an authenticated SDK session.

        Reuses the in-memory session, then the on-disk token cache, then mints
        a fresh token as a last resort.
        """
        with self._lock:
            if self._session is not None:
                return self._session
            cached = self._load_cached_token()
            if cached is not None:
                self._session = self.build_session(cached)
                return self._session
            return self._mint_locked()

    def refresh(self) -> Any:
        """Force a fresh mint (the loop's ``reauth`` hook on token rollover)."""
        with self._lock:
            return self._mint_locked()

    # ── internals (caller holds the lock) ──────────────────────────────

    def _load_cached_token(self) -> str | None:
        """Return the cached access token, or ``None`` to force a mint.

        Raises :class:`GrowwAuthError` if the file exists but is group/world
        accessible (a possible secret leak — never silently reuse it).
        """
        if not self.token_path.exists():
            return None
        mode = stat.S_IMODE(self.token_path.stat().st_mode)
        if mode & _INSECURE_MODE_BITS:
            raise GrowwAuthError(
                f"token file {self.token_path} mode {oct(mode)} is not 0600",
                safe_message="Groww token file has insecure permissions; refusing to use it",
            )
        try:
            payload = json.loads(self.token_path.read_text())
            return str(payload["access_token"])
        except (json.JSONDecodeError, KeyError, OSError):
            return None  # corrupt / unreadable cache → re-mint

    def _mint_locked(self) -> Any:
        """Mint a fresh token (one clock-skew retry), cache it, build the session."""
        token = self._mint_with_skew_retry()
        self._write_token(token)
        self._session = self.build_session(token)
        return self._session

    def _mint_with_skew_retry(self) -> str:
        """Mint a token; on an auth failure, wait one TOTP window and try once more."""
        try:
            return self.mint_token(self.api_key, self.totp_now(self.totp_secret))
        except self.auth_error:
            self.sleep(self.skew_retry_seconds)
        try:
            return self.mint_token(self.api_key, self.totp_now(self.totp_secret))
        except self.auth_error as exc:
            raise GrowwAuthError(
                "Groww auth failed after TOTP refresh + clock-skew retry",
                safe_message="Groww authentication failed (check API key / TOTP secret)",
            ) from exc

    def _write_token(self, token: str) -> None:
        """Write *token* to ``token_path`` at mode 0600 (dir created if absent)."""
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"access_token": token, "minted_at": _utc_now_iso()})
        fd = os.open(str(self.token_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(payload)
        os.chmod(str(self.token_path), 0o600)  # enforce 0600 even if the file pre-existed

    @classmethod
    def from_env(cls) -> GrowwAuth:  # pragma: no cover — wires live growwapi + pyotp
        """Build from ``BRUTEX_GROWW_*`` env vars + the real ``growwapi``/``pyotp``."""
        import pyotp
        from growwapi import GrowwAPI
        from growwapi.groww.exceptions import GrowwAPIAuthenticationException

        token_path = Path(os.environ.get("BRUTEX_GROWW_TOKEN_PATH", str(_DEFAULT_TOKEN_PATH)))

        def _mint(api_key: str, code: str) -> str:
            return str(GrowwAPI.get_access_token(api_key=api_key, totp=code))

        return cls(
            api_key=os.environ["BRUTEX_GROWW_API_KEY"],
            totp_secret=os.environ["BRUTEX_GROWW_TOTP_SECRET"],
            token_path=token_path,
            mint_token=_mint,
            build_session=GrowwAPI,
            totp_now=lambda secret: str(pyotp.TOTP(secret).now()),
            auth_error=GrowwAPIAuthenticationException,
        )


__all__ = [
    "GrowwAuth",
    "GrowwAuthError",
]
