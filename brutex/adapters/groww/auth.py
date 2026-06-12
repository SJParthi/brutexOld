"""Groww TOTP authentication — token generation, caching, and refresh.

**TOTP flow only.** The API-key+secret flow requires daily approval on the
Groww Cloud API Keys page, violating CLAUDE.md §11 AUTOMATED rule
("ZERO clicks per day"). TOTP tokens have no expiry and support automated
unattended runs (same rationale as DEC-DHAN-FINALIZE-001 for Dhan TOTP).

Auth lifecycle
--------------
1. On first call: read env vars → generate TOTP code → POST to auth URL
   → cache token to TOKEN_PATH (mode 0600).
2. On subsequent calls: load token from TOKEN_PATH.
3. On ``GrowwAuthError`` (HTTP 401/403): regenerate TOTP → refresh token
   → retry up to ``MAX_REFRESH_ATTEMPTS``; then raise.

Required env vars (CLAUDE.md §4 "inject dependencies"):
    ``BRUTEX_GROWW_API_KEY`` — TOTP API key (the base32 token from Groww's
        Cloud API Keys page, generated with "Generate TOTP token").
    ``BRUTEX_GROWW_TOTP_SECRET`` — TOTP secret (32-char base32 string used
        to generate the 6-digit one-time code, e.g. via pyotp.TOTP).
    ``BRUTEX_GROWW_TOKEN_PATH`` — optional; defaults to
        ``~/.brutex/groww/access_token.json``.

This module performs real I/O (file + network). The transport is injected
for testability. The TOTP generator is also injected so tests can supply a
fixed code without needing the real secret.
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

from brutex.adapters.groww.constants import (
    AUTH_KEY_TYPE_TOTP,
    DEFAULT_TOKEN_PATH,
    ENV_API_KEY,
    ENV_TOKEN_PATH,
    ENV_TOTP_SECRET,
    GROWW_AUTH_URL,
)
from brutex.adapters.groww.errors import GrowwAuthError

_LOG = structlog.get_logger("brutex.adapters.groww.auth")

#: Maximum token refresh attempts before giving up and raising.
MAX_REFRESH_ATTEMPTS: int = 3

#: Required file permission mode for the cached token (owner read/write only).
REQUIRED_FILE_MODE: int = 0o600

#: Type alias for the injected POST transport.
#: Signature: ``(url, headers, json_body) → (status_code, response_bytes)``
PostTransport = Callable[[str, dict[str, str], dict[str, Any]], tuple[int, bytes]]


def _default_post_transport(
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
) -> tuple[int, bytes]:  # pragma: no cover — real network, tested via smoke only
    """Real urllib-based POST transport."""
    import urllib.request

    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310
        return resp.status, resp.read()


def _build_auth_headers(api_key: str) -> dict[str, str]:
    """Build the POST headers for the Groww auth endpoint."""
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def _fetch_token(
    api_key: str,
    totp_code: str,
    transport: PostTransport,
) -> str:
    """Exchange TOTP code for an access token via the Groww auth endpoint.

    Parameters
    ----------
    api_key:
        The TOTP API key from ``BRUTEX_GROWW_API_KEY``.
    totp_code:
        The current 6-digit TOTP code (30-second window).
    transport:
        Injected HTTP POST callable (real urllib or fake for tests).

    Returns
    -------
    str
        The raw access token string from the Groww response.

    Raises
    ------
    GrowwAuthError
        If the HTTP response indicates an authentication failure.
    """
    headers = _build_auth_headers(api_key)
    body: dict[str, Any] = {"key_type": AUTH_KEY_TYPE_TOTP, "totp": totp_code}
    status, raw = transport(GROWW_AUTH_URL, headers, body)
    if status == 200:
        try:
            payload = json.loads(raw)
            token = payload.get("token") or payload
            if not isinstance(token, str):
                raise GrowwAuthError(
                    f"auth response 'token' field is not a string: {token!r}"
                )
            return token
        except (json.JSONDecodeError, KeyError) as exc:
            raise GrowwAuthError(
                f"cannot parse auth response (status {status}): {raw!r}"
            ) from exc
    raise GrowwAuthError(
        f"auth failed with HTTP {status}: {raw.decode(errors='replace')[:200]}"
    )


def _read_cached_token(path: Path) -> str | None:
    """Load the cached access token from *path*, or ``None`` if absent/unreadable."""
    if not path.exists():
        return None
    try:
        mode = path.stat().st_mode & 0o777
        if mode != REQUIRED_FILE_MODE:
            _LOG.warning(
                "groww.auth.token_perms_wrong",
                path=str(path),
                mode=oct(mode),
                expected=oct(REQUIRED_FILE_MODE),
            )
        data = json.loads(path.read_text())
        token = data.get("access_token")
        return str(token) if token else None
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def _write_cached_token(path: Path, token: str) -> None:
    """Persist *token* to *path* with mode 0600."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"access_token": token}))
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


@dataclass
class GrowwAuth:
    """Manages Groww access-token lifecycle (TOTP flow).

    Parameters
    ----------
    api_key:
        TOTP API key (from ``BRUTEX_GROWW_API_KEY``).
    totp_generator:
        Zero-argument callable that returns the current 6-digit TOTP code.
        Inject ``pyotp.TOTP(secret).now`` for production; inject a lambda
        returning a fixed string for tests.
    token_path:
        Path to cache the access token on disk.
    transport:
        Injected HTTP POST callable. Defaults to real urllib.
    """

    api_key: str
    totp_generator: Callable[[], str]
    token_path: Path
    transport: PostTransport = _default_post_transport

    def get_token(self) -> str:
        """Return a valid access token, refreshing if necessary.

        Loads from the on-disk cache first; generates a new one via TOTP
        if the cache is absent.
        """
        cached = _read_cached_token(self.token_path)
        if cached:
            return cached
        return self._refresh()

    def refresh(self) -> str:
        """Force a TOTP token refresh, bypassing the cache.

        Called by the client on ``GrowwAuthError`` (HTTP 401/403).
        """
        return self._refresh()

    def _refresh(self) -> str:
        totp_code = self.totp_generator()
        _LOG.info("groww.auth.refresh", token_path=str(self.token_path))
        token = _fetch_token(self.api_key, totp_code, self.transport)
        _write_cached_token(self.token_path, token)
        return token


def build_from_env(
    transport: PostTransport = _default_post_transport,
) -> GrowwAuth:
    """Construct a :class:`GrowwAuth` from environment variables.

    Reads ``BRUTEX_GROWW_API_KEY``, ``BRUTEX_GROWW_TOTP_SECRET``, and
    optionally ``BRUTEX_GROWW_TOKEN_PATH``.

    Raises
    ------
    EnvironmentError
        If either required env var is absent.
    """
    api_key = os.environ.get(ENV_API_KEY, "")
    totp_secret = os.environ.get(ENV_TOTP_SECRET, "")
    if not api_key:
        raise OSError(f"Required env var {ENV_API_KEY!r} is not set")
    if not totp_secret:
        raise OSError(f"Required env var {ENV_TOTP_SECRET!r} is not set")
    token_path_str = os.environ.get(ENV_TOKEN_PATH, DEFAULT_TOKEN_PATH)
    token_path = Path(token_path_str).expanduser()
    try:
        import pyotp  # noqa: PLC0415  # optional dep — imported lazily
    except ImportError as exc:
        raise ImportError(
            "pyotp is required for Groww TOTP auth. "
            "Install with: pip install 'brutex[groww]'"
        ) from exc
    totp_gen = pyotp.TOTP(totp_secret)
    return GrowwAuth(
        api_key=api_key,
        totp_generator=totp_gen.now,
        token_path=token_path,
        transport=transport,
    )


__all__ = [
    "GrowwAuth",
    "MAX_REFRESH_ATTEMPTS",
    "PostTransport",
    "REQUIRED_FILE_MODE",
    "build_from_env",
]
