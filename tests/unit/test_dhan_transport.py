"""Unit tests for :mod:`brutex.adapters.dhan.transport` — 100% line+branch.

The real ``requests``-backed transport is ``# pragma: no cover`` (needs network); the
fake satisfies the runtime-checkable Protocol, proving the injected seam is honoured.
"""

from __future__ import annotations

from brutex.adapters.dhan import transport as t

from tests.fakes.fake_dhan_transport import FakeDhanTransport


def test_unavailable_is_import_error() -> None:
    assert issubclass(t.DhanTransportUnavailable, ImportError)


def test_fake_satisfies_runtime_checkable_protocol() -> None:
    fake = FakeDhanTransport()
    assert isinstance(fake, t.DhanTransport)


def test_protocol_declares_post_and_get() -> None:
    assert hasattr(t.DhanTransport, "post_json")
    assert hasattr(t.DhanTransport, "get_json")
