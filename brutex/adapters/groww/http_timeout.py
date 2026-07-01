"""Impose a connect + read socket timeout on every Groww SDK HTTP request.

DEC-GROWW-HTTP-SOCKET-TIMEOUT-001. The ``growwapi`` SDK issues every REST call
through module-level ``requests`` — ``GrowwAPI._request_get`` / ``_request_post`` /
``_request_put`` each call ``requests.get(url, ..., timeout=timeout, **kwargs)`` where
the per-call ``timeout`` **defaults to ``None`` (INFINITE)**, and BruteX's client
(:class:`~brutex.adapters.groww.client.GrowwClient`) never forwards one. So before this
the ONLY bound on a half-open / dead socket was the 45 s wall-clock FREEZE GUARD
(:func:`~brutex.adapters.groww.deadline.run_with_deadline`, injected from
:data:`~brutex.adapters.groww.constants.GROWW_PULL_CALL_TIMEOUT_S`), which on a flaky
network burns 45 s × the bounded retry ladder (~6 attempts) ≈ 4.5 min PER window before
the transient is even classified — the operator's slow-network stall.

**The seam.** The installed SDK exposes NO ``requests.Session`` object to mount an
``HTTPAdapter`` on (it uses module-level ``requests.get``/``post``/``put``). But those
three ``_request_*`` bound methods DO honour an explicit ``timeout`` kwarg — they simply
default it to ``None``. So the cleanest, most-robust reach to the SDK's real socket layer
is to WRAP each of the three bound methods at construction time so a call that passes
``timeout=None`` (every call BruteX makes) is transparently rewritten to
``timeout=(connect, read)`` — a ``requests`` connect-and-read timeout TUPLE — while a
call that ALREADY carries an explicit timeout is left untouched (belt-and-suspenders: the
SDK's own timed calls, e.g. the ``get_access_token`` static path's hardcoded 15 s, keep
their value). This mutates the constructed instance's ``__dict__`` (an instance attribute
SHADOWING the class method), NOT the ``GrowwAPI`` class, so it is per-connection and can
never leak into another SDK object.

:func:`apply_default_timeout` is DEPENDENCY-FREE — it takes ANY object with the three
``_request_*`` callables and patches them, so it is unit-tested against a fake with no
``growwapi`` / ``requests`` / network (the real ``GrowwAPI(token)`` construction stays
behind the one ``# pragma: no cover`` factory in :mod:`~brutex.adapters.groww.reauth`).
A missing ``_request_*`` attribute (an SDK-shape change) DEGRADES that method to no-patch
+ a structlog warning rather than crashing the pull (Golden Rule #1 — the 45 s wall-clock
guard still bounds an un-patched call). This is applied at the SINGLE common factory seam
``reauth._real_sdk_factory`` so BOTH live build sites
(:func:`~brutex.adapters.groww.live.build_live_client` /
:func:`~brutex.adapters.groww.live.build_live_pool`) inherit it with no per-site change.

**Why a socket timeout, not just the wall-clock guard.** The wall-clock guard runs the
SDK call in a daemon thread and abandons it on timeout — it FREES the fetch lane but the
zombie ``requests`` call keeps its socket open until the OS finally gives up (which on an
infinite ``timeout`` may be minutes). A ``requests`` connect/read timeout makes the SDK
itself raise a ``requests.ConnectTimeout`` / ``requests.ReadTimeout`` in SECONDS, which
:func:`~brutex.adapters.groww.errors.from_groww_exception` classifies RETRYABLE, so the
window flows straight into the existing retry → defer → re-pull machinery. Both bounds
coexist: the socket timeout (10 s / 30 s) fires FIRST for a network stall; the 45 s
wall-clock cap remains as belt-and-suspenders for a hang OUTSIDE the socket (a wedged
non-network syscall the socket timeout cannot see). No pacing change — the AIMD ladder,
per-second ceiling, 500/min governor, and ledger logic are all untouched.
"""

from __future__ import annotations

import inspect
import math
import os
from collections.abc import Callable
from typing import Any

import structlog

from brutex.adapters.groww.constants import (
    GROWW_HTTP_CONNECT_TIMEOUT_S,
    GROWW_HTTP_READ_TIMEOUT_S,
)

_LOG = structlog.get_logger("brutex.adapters.groww.http_timeout")

#: Environment override for the SDK's per-request TCP CONNECT timeout (operator escape
#: hatch, no code change) — DEC-GROWW-HTTP-SOCKET-TIMEOUT-001.
HTTP_CONNECT_TIMEOUT_ENV: str = "BRUTEX_GROWW_HTTP_CONNECT_TIMEOUT_S"
#: Environment override for the SDK's per-request SOCKET-READ timeout (operator escape
#: hatch, no code change) — DEC-GROWW-HTTP-SOCKET-TIMEOUT-001.
HTTP_READ_TIMEOUT_ENV: str = "BRUTEX_GROWW_HTTP_READ_TIMEOUT_S"

#: The three ``growwapi.GrowwAPI`` bound request helpers that reach ``requests`` — the
#: single COMMON list every ``get_*`` public method funnels through. Patched together so a
#: future GET/POST/PUT call site inherits the timeout with no further change.
_REQUEST_METHOD_NAMES: tuple[str, ...] = (
    "_request_get",
    "_request_post",
    "_request_put",
)


def _resolve_timeout_seconds(env_var: str, default: float) -> float:
    """Resolve one HTTP timeout: the env override if valid, else the audited *default*.

    Reads *env_var*; falls back to *default* when unset, blank, non-numeric, non-positive,
    OR non-finite (``nan`` / ``inf`` parse as floats but would DEFEAT the bound — an
    ``inf`` timeout never expires, re-introducing the infinite block this fix removes; a
    ``nan`` timeout is ill-defined for ``requests``). A bad value can therefore NEVER
    disable the socket timeout (fail-safe to the audited default, Golden Rule #1). Mirrors
    :func:`~brutex.adapters.groww.write_deadline.resolve_write_deadline_seconds` so the
    pull deadlines share ONE validation shape (CLAUDE.md §11 COMMON). O(1).

    Parameters
    ----------
    env_var
        The environment variable name to read (:data:`HTTP_CONNECT_TIMEOUT_ENV` /
        :data:`HTTP_READ_TIMEOUT_ENV`).
    default
        The audited fall-back value used whenever the env value is absent or invalid.
    """
    value = os.environ.get(env_var)
    if value is None or not value.strip():
        return default
    try:
        parsed = float(value)
    except ValueError:
        return default
    if not math.isfinite(parsed) or parsed <= 0.0:
        return default
    return parsed


def resolve_http_timeout() -> tuple[float, float]:
    """Resolve the ``(connect, read)`` socket-timeout tuple the SDK's requests carry.

    Returns the env-tunable connect + read timeouts
    (:data:`HTTP_CONNECT_TIMEOUT_ENV` / :data:`HTTP_READ_TIMEOUT_ENV`, defaulting to
    :data:`~brutex.adapters.groww.constants.GROWW_HTTP_CONNECT_TIMEOUT_S` /
    :data:`~brutex.adapters.groww.constants.GROWW_HTTP_READ_TIMEOUT_S`), the exact shape
    ``requests`` accepts as its ``timeout=(connect, read)`` argument. O(1).
    """
    return (
        _resolve_timeout_seconds(HTTP_CONNECT_TIMEOUT_ENV, GROWW_HTTP_CONNECT_TIMEOUT_S),
        _resolve_timeout_seconds(HTTP_READ_TIMEOUT_ENV, GROWW_HTTP_READ_TIMEOUT_S),
    )


def _timeout_already_supplied(
    signature: inspect.Signature | None, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> bool:
    """Return True if a non-``None`` ``timeout`` was passed by keyword OR positionally.

    The keyword case is the common one (``kwargs["timeout"]``). The positional case can
    only arise if a caller passed ``timeout`` as a positional arg — which the real SDK +
    BruteX never do (both pass it by keyword). We still guard it: binding the wrapped
    method's *signature* to the actual call resolves a positional ``timeout`` to its name,
    so we never inject a second ``timeout`` and raise ``TypeError: multiple values`` (NIT-1
    from the hostile review). A ``None`` value (explicit or defaulted) counts as NOT
    supplied so it is injected. O(1) — one dict lookup, plus a single ``bind_partial`` only
    when the method exposes a bindable signature.
    """
    if kwargs.get("timeout") is not None:
        return True
    if signature is None:
        # A ``*args, **kwargs``-only callable (e.g. a test fake) has no named ``timeout``
        # parameter to bind positionally, so the keyword check above is authoritative.
        return False
    try:
        bound = signature.bind_partial(*args, **kwargs)
    except TypeError:  # pragma: no cover - a genuinely malformed call raises in ``original``
        return False
    return bound.arguments.get("timeout") is not None


def _bindable_signature(original: Callable[..., Any]) -> inspect.Signature | None:
    """Return *original*'s signature IFF it has a positional-bindable ``timeout`` parameter.

    Returns ``None`` for a ``**kwargs``-only callable (no named ``timeout`` a caller could
    pass positionally) or an un-introspectable one, so the call-time guard falls back to the
    cheap keyword-only check. Resolved ONCE per wrap (not per request). O(1) per wrap.
    """
    try:
        signature = inspect.signature(original)
    except (TypeError, ValueError):  # pragma: no cover - builtins without a signature
        return None
    for parameter in signature.parameters.values():
        if parameter.name == "timeout" and parameter.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        ):
            return signature
    return None


def _wrap_request_method(
    original: Callable[..., Any], timeout: tuple[float, float]
) -> Callable[..., Any]:
    """Wrap one bound ``_request_*`` so a ``timeout=None`` call becomes ``timeout=*timeout*``.

    The SDK's ``_request_get`` / ``_request_post`` / ``_request_put`` accept a ``timeout``
    keyword defaulting to ``None`` (INFINITE). This wrapper INJECTS the resolved
    ``(connect, read)`` tuple whenever the caller omits ``timeout`` or passes it as
    ``None`` — every call BruteX makes — while leaving an EXPLICIT non-``None`` ``timeout``
    exactly as given (so a call site that already bounds its own request keeps its value).
    A ``timeout`` passed POSITIONALLY is also honoured (never double-injected — NIT-1).
    O(1); a thin keyword-normalising delegate, no per-bar cost.
    """
    signature = _bindable_signature(original)

    def _bounded(*args: Any, **kwargs: Any) -> Any:
        if not _timeout_already_supplied(signature, args, kwargs):
            kwargs["timeout"] = timeout
        return original(*args, **kwargs)

    return _bounded


def apply_default_timeout(sdk: object, *, timeout: tuple[float, float] | None = None) -> object:
    """Patch *sdk*'s ``_request_*`` helpers to carry a default ``(connect, read)`` timeout.

    Wraps each of the SDK's three ``requests``-issuing bound methods
    (:data:`_REQUEST_METHOD_NAMES`) with :func:`_wrap_request_method`, installing the
    wrapper as an INSTANCE attribute (shadowing the class method) so EVERY HTTP request the
    SDK makes on our behalf — ``get_historical_candles`` / ``get_expiries`` /
    ``get_contracts`` all funnel through ``_request_get`` — carries a socket timeout unless
    the caller passed an explicit one. Returns the SAME *sdk* object (mutated in place) for
    fluent use at the factory seam.

    A missing helper (an SDK-shape change) is SKIPPED with a ``groww.http_timeout.\
    missing_method`` structlog warning rather than raising — the pull is never crashed by
    this best-effort hardening, and the 45 s wall-clock FREEZE GUARD still bounds an
    un-patched call (Golden Rule #1). O(1) — three attribute wraps at construction, NOT a
    per-request cost.

    Parameters
    ----------
    sdk
        The constructed SDK instance (the real ``growwapi.GrowwAPI`` at the factory seam,
        or a fake with the three ``_request_*`` callables in tests).
    timeout
        The ``(connect, read)`` tuple to inject; defaults to :func:`resolve_http_timeout`
        (the env-tunable audited values). Tests pass an explicit tuple.
    """
    resolved = resolve_http_timeout() if timeout is None else timeout
    for name in _REQUEST_METHOD_NAMES:
        original = getattr(sdk, name, None)
        if not callable(original):
            _LOG.warning("groww.http_timeout.missing_method", method=name)
            continue
        setattr(sdk, name, _wrap_request_method(original, resolved))
    return sdk


__all__ = [
    "HTTP_CONNECT_TIMEOUT_ENV",
    "HTTP_READ_TIMEOUT_ENV",
    "apply_default_timeout",
    "resolve_http_timeout",
]
