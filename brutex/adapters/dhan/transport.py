"""Dhan transport Protocol — the dependency-free HTTP seam the client calls through.

DEC-DHAN-ADAPTER-001. Unlike Groww (a Python SDK), Dhan is a plain REST API, so the
network seam is a narrow :class:`DhanTransport` Protocol (a ``post_json`` /
``get_json`` pair) rather than an SDK Protocol. The real ``requests``-backed
transport lives behind a ``# pragma: no cover`` factory (the exact shape of Groww's
``aws_client.build_real_aws_client``), reached ONLY when a live pull is wired AND
``requests`` is installed; a deterministic in-memory fake
(``tests/fakes/fake_dhan_transport.py``) drives every client branch to 100%
line+branch with NO ``requests`` install and NO network.

This module imports NOTHING from the package (stdlib only) so it can never join an
import cycle — the exact cycle-break discipline of
:mod:`brutex.adapters.groww.sdk_protocol`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol, runtime_checkable


class DhanTransportUnavailable(ImportError):
    """``requests`` is not installed, so the real Dhan HTTP transport cannot be built.

    An :class:`ImportError` subclass — a purely-LOCAL / inert run never installs the
    ``requests`` extra. The (future) live build site maps this to a clear fail-fast
    error naming the missing extra; there is no fallback (Golden Rule #1).
    """


@runtime_checkable
class DhanTransport(Protocol):
    """The narrow HTTP surface the Dhan client calls — a ``post_json`` / ``get_json`` pair.

    Each method returns ``(status_code, json_body)`` verbatim; the client owns retry,
    error classification, pacing, and the meter. Neither the client nor this Protocol
    imports ``requests``.
    """

    def post_json(
        self, *, path: str, body: Mapping[str, object], headers: Mapping[str, str]
    ) -> tuple[int, Mapping[str, object]]:
        """POST *body* as JSON to *path* under the base URL; return ``(status, json)``.

        Raises a transport exception (``requests``/``json`` class) on a
        connection/decode failure — the client maps it via
        :func:`~brutex.adapters.dhan.errors.from_transport_exception`.
        """
        raise NotImplementedError  # structural Protocol stub — never executed

    def get_json(
        self, *, path: str, headers: Mapping[str, str]
    ) -> tuple[int, Mapping[str, object]]:
        """GET *path* under the base URL; return ``(status, json)`` (e.g. ``/v2/profile``)."""
        raise NotImplementedError  # structural Protocol stub — never executed


def build_real_dhan_transport(  # pragma: no cover — needs requests + network
    *, base_url: str
) -> DhanTransport:
    """Build the production :class:`DhanTransport` backed by ``requests`` (lazy import).

    A missing ``requests`` raises :class:`DhanTransportUnavailable`. Reached ONLY by a
    (future) live build site — the inert reference-only adapter never calls it; the
    fake drives every branch of the code that USES this surface.
    """
    try:
        import requests  # type: ignore[import-not-found, unused-ignore]
    except ImportError as exc:
        raise DhanTransportUnavailable(
            "requests is required for the live Dhan HTTP transport; "
            'install the extra: pip install -e ".[dhan]"'
        ) from exc

    return _RequestsDhanTransport(base_url=base_url, session=requests.Session())


class _RequestsDhanTransport:  # pragma: no cover — needs requests + network
    """Adapts a ``requests.Session`` to the :class:`DhanTransport` Protocol.

    A thin translation layer only (no caching, no retry — the client + shared rate
    controller own those). Kept ``# pragma: no cover`` because it can only run against
    the real Dhan API; the fake drives every branch of the client logic that uses it.
    """

    def __init__(self, *, base_url: str, session: object) -> None:
        self._base_url = base_url.rstrip("/")
        self._session = session

    def post_json(
        self, *, path: str, body: Mapping[str, object], headers: Mapping[str, str]
    ) -> tuple[int, Mapping[str, object]]:
        resp = self._session.post(  # type: ignore[attr-defined]
            f"{self._base_url}{path}", json=dict(body), headers=dict(headers), timeout=(5, 30)
        )
        return int(resp.status_code), dict(resp.json())

    def get_json(
        self, *, path: str, headers: Mapping[str, str]
    ) -> tuple[int, Mapping[str, object]]:
        resp = self._session.get(  # type: ignore[attr-defined]
            f"{self._base_url}{path}", headers=dict(headers), timeout=(5, 30)
        )
        return int(resp.status_code), dict(resp.json())


__all__ = [
    "DhanTransport",
    "DhanTransportUnavailable",
    "build_real_dhan_transport",
]
