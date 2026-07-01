"""Fail-fast regression for the Groww SDK socket timeout (DEC-GROWW-HTTP-SOCKET-TIMEOUT-001).

The whole point of the fix: a half-open / STALLED socket must fail in SECONDS (the read
timeout) instead of hanging until the 45 s wall-clock guard. This drives the patched
``_request_*`` through a REAL ``requests`` call against a REAL local socket that accepts a
connection then STALLS (never sends a byte), and asserts:

* ``requests.ReadTimeout`` is raised WELL WITHIN the read timeout (proving the socket
  timeout took effect on a real request — not the 45 s wall-clock), and
* that ``ReadTimeout`` classifies as a RETRYABLE
  :class:`~brutex.adapters.groww.errors.GrowwTimeoutError`, so it flows into the existing
  retry → defer → re-pull machinery.

This is the teeth of the fix — it FAILS (hangs for the full read window ≫ if unpatched, or
never times out at all with ``timeout=None``) without the injected socket timeout. Uses
only the stdlib + ``requests`` (already a hard dep); no ``growwapi``, no external network.
"""

# pyright: reportPrivateUsage=none
# Whitebox test (DEC-P0-017 convention): it calls the SDK's protected ``_request_get``
# (the patched seam) on a test-owned fake; `reportPrivateUsage` is a warning repo-wide.

from __future__ import annotations

import socket
import threading
import time
from typing import Any

import pytest
import requests  # a HARD BruteX dependency (pyproject `dependencies`), not an optional extra
from brutex.adapters.groww.errors import (
    GrowwTimeoutError,
    from_groww_exception,
    is_retryable,
)
from brutex.adapters.groww.http_timeout import apply_default_timeout


class _RealRequestsSDK:
    """A minimal SDK whose ``_request_get`` issues a REAL ``requests.get`` (like GrowwAPI)."""

    # S113: the timeout is INJECTED via the patched wrapper's kwargs (the whole point of
    # the test) — it is never a literal here, exactly like the real GrowwAPI helpers.
    def _request_get(self, **kwargs: Any) -> requests.Response:
        return requests.get(**kwargs)  # noqa: S113

    def _request_post(self, **kwargs: Any) -> requests.Response:  # pragma: no cover
        return requests.post(**kwargs)  # noqa: S113

    def _request_put(self, **kwargs: Any) -> requests.Response:  # pragma: no cover
        return requests.put(**kwargs)  # noqa: S113


def test_stalled_read_raises_within_read_timeout_not_wall_clock() -> None:
    """A socket that accepts then STALLS raises ``ReadTimeout`` within the read timeout.

    Proves the injected ``(connect, read)`` timeout actually bounds a real request: without
    it (``timeout=None``) this would hang far past the read window (up to the 45 s wall-clock
    guard, or forever). The read timeout here is 1 s and the stall is 10 s, so a raise at
    ~1 s (well under a 5 s safety ceiling ≪ 45 s) is the fail-fast proof.
    """
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    accepted: list[socket.socket] = []

    def _accept_then_stall() -> None:
        conn, _ = server.accept()
        accepted.append(conn)  # hold the connection open; NEVER respond
        time.sleep(10.0)

    thread = threading.Thread(target=_accept_then_stall, daemon=True)
    thread.start()
    try:
        # read timeout 1 s (connect 2 s); the server accepts then stalls → a READ timeout.
        # apply_default_timeout patches the SAME instance in place (returns `object`); keep the
        # concretely-typed `sdk` so the patched `_request_get` is type-visible.
        sdk = _RealRequestsSDK()
        apply_default_timeout(sdk, timeout=(2.0, 1.0))
        started = time.monotonic()
        with pytest.raises(requests.exceptions.ReadTimeout) as excinfo:
            sdk._request_get(url=f"http://127.0.0.1:{port}/", headers={}, params={})
        elapsed = time.monotonic() - started
        # fail-fast: raised near the 1 s read timeout, DEFINITELY not the 45 s wall-clock.
        assert elapsed < 5.0
        # and it classifies as a RETRYABLE timeout → the existing retry/defer/re-pull path.
        neutral = from_groww_exception(excinfo.value)
        assert isinstance(neutral, GrowwTimeoutError)
        assert is_retryable(neutral)
    finally:
        for conn in accepted:
            conn.close()
        server.close()
