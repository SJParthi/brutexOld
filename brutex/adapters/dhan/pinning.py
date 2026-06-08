"""SPKI certificate pinning for the Dhan TLS egress (DEC-DHAN-TLS-SPKI-PIN-001).

The Dhan client's only network surface is HTTPS to ``api.dhan.co`` (token +
data path). The transport already enforces an SSRF allow-list + ``https``
scheme (:func:`brutex.adapters.dhan.client._guard_url`), and the default
:mod:`ssl` context validates the server chain against the system CA bundle.
But CA validation alone trusts EVERY CA in that bundle: a mis-issued or
compromised CA (or a corporate MITM proxy quietly added to the trust store)
can present a *chain-valid* certificate for ``api.dhan.co`` and silently
intercept the credentialed token/data traffic.

**SPKI pinning** closes that gap. We pin the SHA-256 of the server's
**SubjectPublicKeyInfo** (the HPKP ``sha256/<base64>`` form) — the public key,
NOT the leaf certificate — so the pin survives certificate *renewal* (same key,
new cert) and only rotates when the operator deliberately rolls the key. On
every handshake the peer's presented public-key SPKI is hashed and matched
against the configured pin set; a miss raises :class:`DhanPinMismatchError`
(a terminal, NON-retryable security failure) and emits a ``tls.pin_mismatch``
ERROR alert — it NEVER silently falls back to CA-only trust.

## Configuration (no hardcoded secret — Golden Rule #1)

Pins are NOT baked into the source. They are operator-config, resolved from
the environment (mirrors the :mod:`brutex.config.credentials` env-first model):

* ``BRUTEX_DHAN_TLS_PINS`` — one or more ``sha256/<base64>`` SPKI pins,
  separated by commas or whitespace. List BOTH the current key's pin AND the
  next (backup) key's pin to pre-stage a **rotation** with zero downtime.
* ``BRUTEX_DHAN_TLS_PINNING`` — set to ``0`` / ``off`` / ``false`` / ``no`` /
  ``disable`` to explicitly OPT OUT (CA-only fallback) for an environment where
  pinning cannot be maintained. This is the SAFE, deliberate fallback.

Resulting policy modes (:class:`PinningMode`):

* **ENFORCE** — pins configured (and not opted out) → every handshake verified.
* **DISABLED** — explicit opt-out → CA-only validation, logged ``tls.pinning``
  once at INFO (deliberate operator choice).
* **UNCONFIGURED** — no pins and no opt-out → CA-only validation, logged
  ``tls.pinning`` once at WARNING (pinning is available but not yet armed).

The opt-out is honoured even when pins are present, so an operator can leave a
pin set in place but temporarily disable enforcement without deleting it.

## What is and isn't here

This module is **pure** (no network, no global mutable state): SPKI digest
computation, pin parse/validate, policy resolution, the match check, and the
one-shot mode log are all unit-testable with an in-memory DER certificate. The
thin :mod:`ssl`/:mod:`urllib` glue that grabs the live peer certificate and
calls :func:`verify_pin` lives in
:func:`brutex.adapters.dhan.client.urllib_transport` (the ``# pragma: no cover``
network shim), exactly like the rest of the real transport.

The mismatch ERROR log is the real-time **alert**; the orchestrator's
hash-chained JSONL audit (CLAUDE.md §2 Rule 8) records the resulting failed
chunk at its own layer, so a pin breach is both alerted AND audited without
this pure module reaching for an audit handle.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Final

import structlog
from cryptography import x509
from cryptography.hazmat.primitives import serialization

from brutex.adapters.dhan.errors import DhanError

#: Structured logger — emits ``tls.pin_mismatch`` (ERROR alert), ``tls.pin_ok``
#: (DEBUG), and ``tls.pinning`` (mode banner) into the operator's /logs stream.
_LOG = structlog.get_logger("brutex.adapters.dhan.pinning")

#: HPKP-style SPKI pin label prefix: ``sha256/<base64-of-32-byte-digest>``.
PIN_PREFIX: Final[str] = "sha256/"

#: Raw SHA-256 digest length in bytes (a valid SPKI pin decodes to exactly this).
_SHA256_LEN: Final[int] = 32

#: Env var carrying the operator's SPKI pin set (comma/whitespace separated).
ENV_TLS_PINS: Final[str] = "BRUTEX_DHAN_TLS_PINS"

#: Env var to explicitly disable pinning (CA-only fallback). Name, not a secret.
ENV_TLS_PINNING: Final[str] = "BRUTEX_DHAN_TLS_PINNING"

#: Values of :data:`ENV_TLS_PINNING` that mean "pinning OFF" (case-insensitive).
_DISABLE_VALUES: Final[frozenset[str]] = frozenset(
    {"0", "off", "false", "no", "disable", "disabled"}
)

#: Splitter for the pin list — any run of commas / whitespace.
_PIN_SPLIT: Final[re.Pattern[str]] = re.compile(r"[,\s]+")


class DhanPinMismatchError(DhanError):
    """The peer certificate's SPKI matched none of the configured pins.

    A **terminal security failure**: it is NOT in :func:`is_retryable`'s set
    (it is a bare :class:`DhanError` subclass), so the client's fast-retry loop
    treats it as fatal and never re-issues — re-trying a MITM would only re-leak
    the credentialed request. The operator must rotate the pin (a legitimate key
    roll) or investigate the interception.
    """


class PinningMode(Enum):
    """Resolved pinning posture for the Dhan TLS egress."""

    #: Pins configured and enforced — every handshake's SPKI is verified.
    ENFORCE = "enforce"
    #: Operator explicitly opted out — CA-only validation (deliberate fallback).
    DISABLED = "disabled"
    #: No pins and no opt-out — CA-only validation (pinning available, not armed).
    UNCONFIGURED = "unconfigured"


@dataclass(frozen=True, slots=True)
class PinningPolicy:
    """An immutable resolved pinning policy (mode + the configured pin set)."""

    mode: PinningMode
    pins: frozenset[str]

    @property
    def enforced(self) -> bool:
        """``True`` iff this policy verifies the peer SPKI on every handshake."""
        return self.mode is PinningMode.ENFORCE


def spki_sha256_pin(cert_der: bytes) -> str:
    """Return the HPKP-style ``sha256/<base64>`` SPKI pin for a DER certificate.

    The pin is the base64 of ``SHA-256(SubjectPublicKeyInfo)`` — i.e. it pins
    the public KEY, so a routine certificate renewal that re-uses the same key
    keeps the same pin (only a deliberate key rotation changes it).

    Parameters
    ----------
    cert_der
        The peer leaf certificate in DER (``getpeercert(binary_form=True)``).

    Returns
    -------
    str
        The canonical ``sha256/<base64>`` pin string.

    Raises
    ------
    ValueError
        If *cert_der* is not a parseable X.509 certificate.
    """
    try:
        cert = x509.load_der_x509_certificate(cert_der)
    except ValueError as exc:
        raise ValueError(f"un-parseable peer certificate (DER): {exc}") from exc
    spki_der = cert.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    digest = hashlib.sha256(spki_der).digest()
    return PIN_PREFIX + base64.b64encode(digest).decode("ascii")


def _normalise_pin(raw: str) -> str:
    """Validate and canonicalise one operator-supplied SPKI pin.

    Accepts a pin with or without the ``sha256/`` prefix; returns the canonical
    prefixed form. Rejects anything that is not base64 of exactly a 32-byte
    SHA-256 digest, so a typo'd / truncated pin fails loudly at config time
    rather than silently never matching at handshake time.
    """
    pin = raw.strip()
    body = pin[len(PIN_PREFIX) :] if pin.startswith(PIN_PREFIX) else pin
    try:
        decoded = base64.b64decode(body, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"invalid base64 in SPKI pin {raw!r}") from exc
    if len(decoded) != _SHA256_LEN:
        raise ValueError(
            f"SPKI pin {raw!r} must be base64 of a 32-byte SHA-256 digest, got {len(decoded)} bytes"
        )
    return PIN_PREFIX + body


def parse_pins(raw: str) -> frozenset[str]:
    """Parse a comma/whitespace-separated pin string into a canonical pin set.

    Parameters
    ----------
    raw
        The raw ``BRUTEX_DHAN_TLS_PINS`` value (may be blank).

    Returns
    -------
    frozenset[str]
        The canonical (prefixed, validated) pins; empty when *raw* is blank.

    Raises
    ------
    ValueError
        If any token is not a valid base64 SHA-256 SPKI pin.
    """
    tokens = [t for t in _PIN_SPLIT.split(raw.strip()) if t]
    return frozenset(_normalise_pin(t) for t in tokens)


def load_pinning_policy(env: Mapping[str, str] | None = None) -> PinningPolicy:
    """Resolve the TLS pinning policy from the environment (no hardcoded pins).

    Resolution order:

    1. An explicit opt-out (:data:`ENV_TLS_PINNING` in :data:`_DISABLE_VALUES`)
       always wins → :attr:`PinningMode.DISABLED` (CA-only). Honoured even when
       pins are also present, so a pin set can stay configured but inert.
    2. Otherwise, a non-empty :data:`ENV_TLS_PINS` → :attr:`PinningMode.ENFORCE`.
    3. Otherwise → :attr:`PinningMode.UNCONFIGURED` (CA-only, pinning un-armed).

    Parameters
    ----------
    env
        Environment mapping to read. Defaults to :data:`os.environ`.

    Returns
    -------
    PinningPolicy
        The resolved policy.

    Raises
    ------
    ValueError
        Propagated from :func:`parse_pins` for a malformed pin (fail fast).
    """
    source = os.environ if env is None else env
    pins = parse_pins(source.get(ENV_TLS_PINS, ""))
    toggle = source.get(ENV_TLS_PINNING, "").strip().lower()
    if toggle in _DISABLE_VALUES:
        return PinningPolicy(PinningMode.DISABLED, frozenset())
    if pins:
        return PinningPolicy(PinningMode.ENFORCE, pins)
    return PinningPolicy(PinningMode.UNCONFIGURED, frozenset())


def log_policy(policy: PinningPolicy) -> None:
    """Emit the one-shot ``tls.pinning`` mode banner for *policy*.

    Called once when the pinned opener is built (NOT per request, so it never
    floods /logs). An UNCONFIGURED policy is logged at WARNING — pinning is
    available but not armed, the operator should set ``BRUTEX_DHAN_TLS_PINS`` —
    while a deliberate DISABLED opt-out and an active ENFORCE are INFO.
    """
    if policy.mode is PinningMode.ENFORCE:
        _LOG.info("tls.pinning", mode=policy.mode.value, pins=len(policy.pins))
    elif policy.mode is PinningMode.DISABLED:
        _LOG.info("tls.pinning", mode=policy.mode.value, detail="operator opt-out (CA-only)")
    else:
        _LOG.warning(
            "tls.pinning",
            mode=policy.mode.value,
            detail=f"no pins configured; set {ENV_TLS_PINS} to arm SPKI pinning (CA-only fallback)",
        )


def verify_pin(cert_der: bytes, policy: PinningPolicy, *, host: str = "api.dhan.co") -> None:
    """Verify the peer's SPKI against *policy*; raise on a mismatch.

    A no-op when *policy* is not :attr:`PinningMode.ENFORCE` (CA-only). When
    enforced, the peer's SPKI pin is computed and matched against the configured
    set: a hit logs ``tls.pin_ok`` (DEBUG) and returns; a miss logs a
    ``tls.pin_mismatch`` ERROR alert and raises :class:`DhanPinMismatchError`.

    Parameters
    ----------
    cert_der
        The peer leaf certificate DER (``getpeercert(binary_form=True)``).
    policy
        The resolved :class:`PinningPolicy`.
    host
        The host being verified (for the log/alert context only).

    Raises
    ------
    DhanPinMismatchError
        When enforcing and the peer SPKI matches none of the configured pins.
    ValueError
        If *cert_der* is un-parseable (propagated from :func:`spki_sha256_pin`).
    """
    if not policy.enforced:
        return
    actual = spki_sha256_pin(cert_der)
    if actual not in policy.pins:
        _LOG.error(
            "tls.pin_mismatch",
            host=host,
            actual=actual,
            expected=sorted(policy.pins),
        )
        raise DhanPinMismatchError(
            f"SPKI pin mismatch for {host}: peer public key {actual} is not in the "
            f"configured pin set ({len(policy.pins)} pin(s)) — refusing the connection"
        )
    _LOG.debug("tls.pin_ok", host=host, pin=actual)


__all__ = [
    "ENV_TLS_PINNING",
    "ENV_TLS_PINS",
    "PIN_PREFIX",
    "DhanPinMismatchError",
    "PinningMode",
    "PinningPolicy",
    "load_pinning_policy",
    "log_policy",
    "parse_pins",
    "spki_sha256_pin",
    "verify_pin",
]
