"""Tests for ``brutex.adapters.groww.http_timeout`` — the SDK socket-timeout seam.

DEC-GROWW-HTTP-SOCKET-TIMEOUT-001. 100% line+branch of
:func:`~brutex.adapters.groww.http_timeout.apply_default_timeout` +
:func:`~brutex.adapters.groww.http_timeout.resolve_http_timeout` +
``_resolve_timeout_seconds``:

* every env-resolution branch (unset / blank / good / non-numeric / non-finite /
  non-positive) — a bad env value can NEVER disable the bound (fail-safe to the audited
  default, Golden Rule #1);
* the ``(connect, read)`` tuple is INJECTED when a wrapped ``_request_*`` is called with
  ``timeout`` omitted OR ``timeout=None``, and an EXPLICIT non-``None`` timeout is
  preserved untouched (the belt-and-suspenders leave-it-alone branch);
* all three helpers are patched, and a MISSING / non-callable helper is SKIPPED with a
  warning (never crashes) so an SDK-shape change degrades gracefully;
* the REAL ``requests.ReadTimeout`` fail-fast is proven in
  ``test_groww_socket_timeout_failfast.py`` (a stalled socket raises within the read
  timeout, not the 45 s wall-clock) — this module pins the pure seam with a fake SDK, no
  ``growwapi`` / network.
"""
# pyright: reportPrivateUsage=none
# Whitebox test (DEC-P0-017 convention): it deliberately calls the SDK's protected
# ``_request_*`` helpers (the exact seam the fix patches) on test-owned fakes;
# `reportPrivateUsage` is a warning repo-wide.

from __future__ import annotations

from typing import Any

import pytest
from brutex.adapters.groww.constants import (
    GROWW_HTTP_CONNECT_TIMEOUT_S,
    GROWW_HTTP_READ_TIMEOUT_S,
)
from brutex.adapters.groww.http_timeout import (
    HTTP_CONNECT_TIMEOUT_ENV,
    HTTP_READ_TIMEOUT_ENV,
    apply_default_timeout,
    resolve_http_timeout,
)


class _FakeSDK:
    """A stand-in for ``growwapi.GrowwAPI`` with the three ``requests`` helpers.

    Each helper records the kwargs it was called with so a test can assert what ``timeout``
    reached ``requests`` after the patch — no ``growwapi`` / ``requests`` / network needed.
    """

    def __init__(self) -> None:
        self.calls: dict[str, list[dict[str, Any]]] = {
            "_request_get": [],
            "_request_post": [],
            "_request_put": [],
        }

    def _request_get(self, **kwargs: Any) -> str:
        self.calls["_request_get"].append(kwargs)
        return "get"

    def _request_post(self, **kwargs: Any) -> str:
        self.calls["_request_post"].append(kwargs)
        return "post"

    def _request_put(self, **kwargs: Any) -> str:
        self.calls["_request_put"].append(kwargs)
        return "put"


# ── resolve_http_timeout / _resolve_timeout_seconds env branches ──────────────


def test_resolve_http_timeout_defaults_when_env_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No env set → the audited constant defaults (connect 10 s, read 30 s)."""
    monkeypatch.delenv(HTTP_CONNECT_TIMEOUT_ENV, raising=False)
    monkeypatch.delenv(HTTP_READ_TIMEOUT_ENV, raising=False)
    assert resolve_http_timeout() == (
        GROWW_HTTP_CONNECT_TIMEOUT_S,
        GROWW_HTTP_READ_TIMEOUT_S,
    )


def test_resolve_http_timeout_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """A valid positive env value overrides both connect + read independently."""
    monkeypatch.setenv(HTTP_CONNECT_TIMEOUT_ENV, "3.5")
    monkeypatch.setenv(HTTP_READ_TIMEOUT_ENV, "12")
    assert resolve_http_timeout() == (3.5, 12.0)


@pytest.mark.parametrize(
    "bad",
    ["", "   ", "notanumber", "nan", "inf", "-inf", "0", "-1", "-0.5"],
)
def test_resolve_http_timeout_bad_env_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    """Blank / non-numeric / non-finite / non-positive env → the audited default (fail-safe).

    A bad value can NEVER disable the socket timeout — it falls back to the constant, so
    the worst case is "as bounded as the default", never an infinite / ill-defined bound
    (Golden Rule #1). Both env vars share the ONE ``_resolve_timeout_seconds`` shape.
    """
    monkeypatch.setenv(HTTP_CONNECT_TIMEOUT_ENV, bad)
    monkeypatch.setenv(HTTP_READ_TIMEOUT_ENV, bad)
    assert resolve_http_timeout() == (
        GROWW_HTTP_CONNECT_TIMEOUT_S,
        GROWW_HTTP_READ_TIMEOUT_S,
    )


def test_resolved_timeouts_below_wall_clock_cap() -> None:
    """Both defaults sit comfortably BELOW the 45 s wall-clock guard so the socket fires first.

    The whole point of the fix: the socket timeout must trip BEFORE
    :data:`~brutex.adapters.groww.constants.GROWW_PULL_CALL_TIMEOUT_S` (45 s), leaving the
    wall-clock guard only as belt-and-suspenders (DEC-GROWW-HTTP-SOCKET-TIMEOUT-001).
    """
    from brutex.adapters.groww.constants import GROWW_PULL_CALL_TIMEOUT_S

    connect, read = resolve_http_timeout()
    assert connect < GROWW_PULL_CALL_TIMEOUT_S
    assert read < GROWW_PULL_CALL_TIMEOUT_S
    # and the connect bound is tighter than the read bound (the read timeout, the longer of
    # the two, bounds a stalled transfer) — the ordering the fix relies on, not re-checking
    # the < cap already asserted above.
    assert connect < read


# ── apply_default_timeout injection / preservation branches ───────────────────


def test_apply_injects_timeout_on_omitted_and_none() -> None:
    """A wrapped helper called WITHOUT a timeout — or with ``timeout=None`` — gets the tuple."""
    sdk = _FakeSDK()
    apply_default_timeout(sdk, timeout=(10.0, 30.0))

    sdk._request_get(url="u", headers={}, params={})  # omitted
    sdk._request_get(url="u", headers={}, params={}, timeout=None)  # explicit None
    injected = [c["timeout"] for c in sdk.calls["_request_get"]]
    assert injected == [(10.0, 30.0), (10.0, 30.0)]


def test_apply_preserves_explicit_timeout() -> None:
    """An EXPLICIT non-``None`` timeout is left exactly as the caller passed it."""
    sdk = _FakeSDK()
    apply_default_timeout(sdk, timeout=(10.0, 30.0))

    assert sdk._request_get(url="u", headers={}, params={}, timeout=5) == "get"
    assert sdk.calls["_request_get"][0]["timeout"] == 5


def test_apply_patches_all_three_helpers() -> None:
    """GET, POST and PUT all inherit the injected timeout (the one COMMON list)."""
    sdk = _FakeSDK()
    apply_default_timeout(sdk, timeout=(1.0, 2.0))

    sdk._request_get(url="u")
    sdk._request_post(url="u")
    sdk._request_put(url="u")
    assert sdk.calls["_request_get"][0]["timeout"] == (1.0, 2.0)
    assert sdk.calls["_request_post"][0]["timeout"] == (1.0, 2.0)
    assert sdk.calls["_request_put"][0]["timeout"] == (1.0, 2.0)


def test_apply_returns_same_object() -> None:
    """The SDK is mutated in place + returned (fluent factory use)."""
    sdk = _FakeSDK()
    assert apply_default_timeout(sdk, timeout=(1.0, 2.0)) is sdk


def test_apply_uses_resolved_default_when_timeout_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``timeout=None`` (the production call) resolves the env-tunable audited tuple."""
    monkeypatch.setenv(HTTP_CONNECT_TIMEOUT_ENV, "7")
    monkeypatch.setenv(HTTP_READ_TIMEOUT_ENV, "8")
    sdk = _FakeSDK()
    apply_default_timeout(sdk)  # timeout defaults to resolve_http_timeout()

    sdk._request_get(url="u")
    assert sdk.calls["_request_get"][0]["timeout"] == (7.0, 8.0)


def test_apply_skips_missing_helper_with_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing / non-callable ``_request_*`` is SKIPPED + warned, never raises.

    Guards against an SDK-shape change: the pull must not crash on this best-effort
    hardening, and the 45 s wall-clock guard still bounds an un-patched call (Golden
    Rule #1). Both branches — a missing attribute AND a present-but-not-callable one.
    """
    warnings: list[tuple[str, Any]] = []
    monkeypatch.setattr(
        "brutex.adapters.groww.http_timeout._LOG.warning",
        lambda event, **kw: warnings.append((event, kw.get("method"))),
    )

    class _PartialSDK:
        # no _request_get at all (missing)
        _request_post = "not callable"  # present but not callable

        def _request_put(self, **kwargs: Any) -> str:
            return "put"

    sdk = _PartialSDK()
    result = apply_default_timeout(sdk, timeout=(1.0, 2.0))
    assert result is sdk
    # both the missing GET and the non-callable POST warned; PUT patched silently.
    assert ("groww.http_timeout.missing_method", "_request_get") in warnings
    assert ("groww.http_timeout.missing_method", "_request_post") in warnings
    assert callable(sdk._request_put)
    assert sdk._request_put(url="u") == "put"


def test_wrapped_helper_forwards_positional_and_returns_value() -> None:
    """The wrapper is transparent: positional args + the return value flow through."""

    class _PositionalSDK:
        def _request_get(self, url: str, **kwargs: Any) -> tuple[str, Any]:
            return (url, kwargs.get("timeout"))

        def _request_post(self, url: str, **kwargs: Any) -> tuple[str, Any]:
            return (url, kwargs.get("timeout"))

        def _request_put(self, url: str, **kwargs: Any) -> tuple[str, Any]:
            return (url, kwargs.get("timeout"))

    sdk = _PositionalSDK()
    apply_default_timeout(sdk, timeout=(1.0, 2.0))
    # All three wrapped helpers forward the positional URL AND the return value while
    # injecting the timeout — proven for GET, POST and PUT (not left as inert stubs).
    assert sdk._request_get("https://api.groww.in/v1/x") == (
        "https://api.groww.in/v1/x",
        (1.0, 2.0),
    )
    assert sdk._request_post("https://api.groww.in/v1/x") == (
        "https://api.groww.in/v1/x",
        (1.0, 2.0),
    )
    assert sdk._request_put("https://api.groww.in/v1/x") == (
        "https://api.groww.in/v1/x",
        (1.0, 2.0),
    )


# ── positional-timeout guard (hostile-review NIT-1: never double-inject) ──────


class _NamedTimeoutSDK:
    """An SDK whose ``_request_*`` declare ``timeout`` as a POSITIONAL-OR-KEYWORD param.

    Unlike ``_FakeSDK`` (``**kwargs``-only), this exposes a bindable ``timeout`` parameter,
    so a caller COULD pass it positionally — the exact shape the NIT-1 guard defends. Each
    helper records the ``timeout`` it actually received after the patch.
    """

    def __init__(self) -> None:
        self.seen: dict[str, Any] = {}

    def _request_get(self, url: str, timeout: Any = None, **kwargs: Any) -> str:
        self.seen["_request_get"] = timeout
        return "get"

    def _request_post(self, url: str, timeout: Any = None, **kwargs: Any) -> str:
        self.seen["_request_post"] = timeout
        return "post"

    def _request_put(self, url: str, timeout: Any = None, **kwargs: Any) -> str:
        self.seen["_request_put"] = timeout
        return "put"


def test_apply_injects_when_named_timeout_param_omitted() -> None:
    """A method with a bindable ``timeout`` param, called without it, gets the tuple.

    Exercises the ``bind_partial`` path: the signature binds, ``timeout`` resolves to its
    default ``None`` → NOT supplied → the tuple is injected.
    """
    sdk = _NamedTimeoutSDK()
    apply_default_timeout(sdk, timeout=(4.0, 8.0))

    assert sdk._request_get(url="u") == "get"
    assert sdk.seen["_request_get"] == (4.0, 8.0)


def test_apply_preserves_positional_timeout_without_double_injecting() -> None:
    """A ``timeout`` passed POSITIONALLY is honoured, never double-injected (NIT-1).

    Without the positional guard this call would raise
    ``TypeError: got multiple values for argument 'timeout'`` — the latent fragility the
    hostile review flagged. The guard binds the signature, sees ``timeout`` already supplied
    positionally, and leaves it exactly as passed.
    """
    sdk = _NamedTimeoutSDK()
    apply_default_timeout(sdk, timeout=(4.0, 8.0))

    # url positional, timeout positional (value 9) — must NOT raise, must NOT be overwritten.
    assert sdk._request_get("u", 9) == "get"
    assert sdk.seen["_request_get"] == 9


def test_apply_injects_when_named_timeout_param_explicit_none() -> None:
    """``timeout=None`` on a bindable-signature method still resolves to the injected tuple."""
    sdk = _NamedTimeoutSDK()
    apply_default_timeout(sdk, timeout=(4.0, 8.0))

    assert sdk._request_get(url="u", timeout=None) == "get"
    assert sdk.seen["_request_get"] == (4.0, 8.0)


# ── real-SDK contract pin (hostile-review MAJOR: detect a private-API rename) ──


def test_real_growwapi_exposes_the_three_request_helpers() -> None:
    """Pin the REAL ``growwapi.GrowwAPI`` private method names the patch hooks.

    ``apply_default_timeout`` wraps the SDK's ``_request_get`` / ``_request_post`` /
    ``_request_put`` — a PRIVATE (``_``-prefixed) surface that carries no stability
    guarantee. If a future ``growwapi`` renames or removes any of them, the patch would
    SILENTLY no-op (only a ``groww.http_timeout.missing_method`` warning) and every call
    would fall back to ``timeout=None`` (INFINITE), bounded only by the inferior 45 s
    wall-clock guard. This contract test makes that regression FAIL CI loudly instead
    (DEC-GROWW-HTTP-SOCKET-TIMEOUT-001). Skipped where the optional ``growwapi`` extra is
    not installed (e.g. the CI type-check job) — the pure seam is fully covered above.
    """
    growwapi = pytest.importorskip("growwapi")

    for method in ("_request_get", "_request_post", "_request_put"):
        helper = getattr(growwapi.GrowwAPI, method, None)
        assert helper is not None, f"growwapi.GrowwAPI lost {method!r} — the patch would no-op"
        assert callable(helper), f"growwapi.GrowwAPI.{method} is no longer callable"


def test_request_method_alias_matches_private_source() -> None:
    """The kit's public ``REQUEST_METHOD_NAMES`` alias never drifts from the source
    tuple the timeout patch walks (DEC-DOCTOR-LIVE-PROOF-KIT-001, G2 shape probe)."""
    from brutex.adapters.groww.http_timeout import _REQUEST_METHOD_NAMES, REQUEST_METHOD_NAMES

    assert REQUEST_METHOD_NAMES == _REQUEST_METHOD_NAMES
    assert REQUEST_METHOD_NAMES == ("_request_get", "_request_post", "_request_put")
