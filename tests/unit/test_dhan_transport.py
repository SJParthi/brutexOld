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


def test_pyproject_declares_the_dhan_extra_the_remediation_names() -> None:
    """The ``[dhan]`` extra the fail-fast remediation names MUST exist + declare requests.

    Round-1 convention finding: ``build_real_dhan_transport`` tells the operator
    ``pip install -e ".[dhan]"`` — pre-fix pyproject declared NO such extra, so the
    guidance was factually wrong (Golden Rule #1). Mirrors
    ``test_pyproject_has_groww_extra``.
    """
    import tomllib
    from pathlib import Path

    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    extras = data["project"]["optional-dependencies"]
    assert "dhan" in extras, f"dhan extra missing from pyproject: {sorted(extras)}"
    assert any("requests" in dep for dep in extras["dhan"]), (
        f"dhan extra must declare requests: {extras['dhan']}"
    )
