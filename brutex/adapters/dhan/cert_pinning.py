"""Dhan TLS SPKI certificate pinning (DEC-DHAN-TLS-SPKI-PIN-001).

The Dhan adapter's production transport (:func:`brutex.adapters.dhan.client.
urllib_transport`) used the default :func:`urllib.request.urlopen`, which
validates the server certificate against the system CA store **only**. A CA
compromise / mis-issuance / corporate-proxy MITM that presents a *validly
chained* certificate for ``api.dhan.co`` would therefore be accepted — the
access token (a 24 h bearer JWT) and every fetched bar would flow to the
attacker. This module adds **SPKI (SubjectPublicKeyInfo SHA-256) public-key
pinning**: a second, independent check that the peer's public key hashes to one
of an operator-configured set, on TOP of normal CA validation.

## Why SPKI (not full-cert) pinning

The pin is ``base64(SHA-256(DER SubjectPublicKeyInfo))`` — the SAME value HPKP /
the major mobile-pinning libraries use. Pinning the *public key* (not the whole
certificate) means a routine certificate renewal that **keeps the same key pair**
does not break the pin, so rotation is survivable by configuring the *current*
and *next* key's pin together (both accepted — see :meth:`PinPolicy.verify`).

## Safety model (Golden Rule #1 / #10 — honest, never silent)

* **Opt-in, safe fallback.** Pinning is enforced ONLY when the operator
  configures a pin set (``BRUTEX_DHAN_TLS_PINS``). With NO pins configured the
  transport behaves EXACTLY as before (system-CA validation) — a missing config
  never breaks a live pull. This is the SAFE fallback.
* **Explicit operator opt-out.** ``BRUTEX_DHAN_TLS_PIN_DISABLE=1`` disables the
  check even when pins are configured, for a deliberate break-glass (e.g. a
  debugging proxy). Logged at WARNING so the bypass is never invisible.
* **NEVER a silent fall-back on MISMATCH.** When pins ARE enforced and the peer
  key does not match, the connection is REFUSED with a security-specific
  :class:`DhanCertPinError`, a CRITICAL ``dhan.tls.pin_mismatch`` audit line is
  emitted, and the injected ``alert`` hook fires. A mismatch is treated as an
  active attack, not a soft warning — it can never degrade to "connect anyway".
* **No hardcoded pins / secrets.** There is NO built-in pin for ``api.dhan.co``
  (a hardcoded pin would brick every operator on Dhan's next key rotation, and
  Golden Rule #1 forbids unverifiable embedded facts). Pins are supplied by the
  operator via env. A pin is a PUBLIC key hash — not a secret — so it is safe to
  log/echo (unlike the access token).

The pure verification core (DER → SPKI → pin, policy decision) is fully
unit-tested with synthetic + real certificates; only the socket-level peer-cert
extraction in :mod:`brutex.adapters.dhan.client` is the ``# pragma: no cover``
network shim.
"""

from __future__ import annotations

import base64
import hashlib
import os
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import NoReturn

import structlog

from brutex.adapters.dhan.errors import DhanError

#: Audit/alert logger — a pin mismatch emits a CRITICAL ``dhan.tls.pin_mismatch``
#: line into the SAME structlog stream the operator reads on /logs (the audit
#: surface), mirroring the ``rate.breach`` alarm in :mod:`~brutex.adapters.dhan.client`.
_LOG = structlog.get_logger("brutex.adapters.dhan.cert_pinning")

#: Env var holding the operator's SPKI pin set — a comma/whitespace separated
#: list of ``base64(SHA-256(SPKI))`` values (an optional ``sha256/`` prefix per
#: the common HPKP notation is accepted and stripped). The env var NAME, not a
#: secret; a pin is a PUBLIC key hash.
ENV_TLS_PINS: str = "BRUTEX_DHAN_TLS_PINS"

#: Env var for the explicit break-glass opt-out. Truthy (``1``/``true``/``yes``/
#: ``on``, case-insensitive) disables pinning even when pins are configured.
ENV_TLS_PIN_DISABLE: str = "BRUTEX_DHAN_TLS_PIN_DISABLE"

#: Accepted truthy spellings for :data:`ENV_TLS_PIN_DISABLE` (lower-cased compare).
_TRUTHY: frozenset[str] = frozenset({"1", "true", "yes", "on"})

#: Optional HPKP-style prefix on a configured pin (``sha256/<base64>``); stripped.
_PIN_PREFIX: str = "sha256/"

#: DER tag for a constructed SEQUENCE — the encoding of ``Certificate``,
#: ``TBSCertificate`` and ``SubjectPublicKeyInfo``.
_DER_SEQUENCE: int = 0x30

#: DER tag for the OPTIONAL ``version [0] EXPLICIT`` field that, when present, is
#: the FIRST element of ``TBSCertificate`` (context-specific constructed, [0]).
_DER_VERSION_CTX: int = 0xA0

#: Count of ``TBSCertificate`` fields between (exclusive) the optional version and
#: the ``subjectPublicKeyInfo``: serialNumber, signature, issuer, validity,
#: subject — five elements to skip before the SPKI (RFC 5280 §4.1).
_TBS_FIELDS_BEFORE_SPKI: int = 5

#: Long-form DER length cap (bytes of length): a 4-byte length covers any real
#: certificate (≤ 4 GiB); a larger declared width is treated as malformed.
_MAX_LEN_OCTETS: int = 4

#: DER length: a leading octet with bit 8 set (``>= 0x80``) signals the long form
#: (the low 7 bits, ``& _DER_LEN_MASK``, give the count of following octets); a
#: value below it IS the short-form length.
_DER_LONG_FORM: int = 0x80
_DER_LEN_MASK: int = 0x7F


class DhanCertPinError(DhanError):
    """The peer's TLS public key did not match any configured SPKI pin.

    A **security-specific, terminal** Dhan failure: it is deliberately NOT in the
    :func:`brutex.adapters.dhan.errors.is_retryable` set, so a pin mismatch is
    never fast-retried or re-queued — retrying an attacker-presented certificate
    would only re-expose the credentials. Raised by :meth:`PinPolicy.verify` on a
    mismatch (or an absent/malformed peer certificate when pinning is enforced).
    """


def spki_sha256_pin(cert_der: bytes) -> str:
    """Return the SPKI pin ``base64(SHA-256(SubjectPublicKeyInfo))`` for *cert_der*.

    Extracts the ``SubjectPublicKeyInfo`` (the DER-encoded public-key SEQUENCE)
    from the X.509 certificate and returns the standard HPKP pin: the base64 of
    its SHA-256 digest. This is the value an operator configures in
    :data:`ENV_TLS_PINS`.

    Parameters
    ----------
    cert_der
        The peer certificate in DER (binary) form, as returned by
        :meth:`ssl.SSLSocket.getpeercert(binary_form=True)`.

    Returns
    -------
    str
        The base64 SPKI pin (no ``sha256/`` prefix).

    Raises
    ------
    DhanCertPinError
        If *cert_der* is not a parseable DER X.509 certificate.
    """
    spki = _extract_spki_der(cert_der)
    digest = hashlib.sha256(spki).digest()
    return base64.b64encode(digest).decode("ascii")


def _read_length(data: bytes, index: int) -> tuple[int, int]:
    """Read a DER length starting at *index*; return ``(length, next_index)``.

    Handles the short form (one octet ``< 0x80``) and the definite long form
    (``0x80 | n`` then *n* big-endian octets). The indefinite form (``0x80``) and
    an over-wide length are rejected — neither occurs in a valid DER certificate.
    """
    if index >= len(data):
        raise DhanCertPinError("malformed certificate: truncated length")
    first = data[index]
    if first < _DER_LONG_FORM:
        return first, index + 1
    octets = first & _DER_LEN_MASK
    if octets == 0 or octets > _MAX_LEN_OCTETS:
        raise DhanCertPinError("malformed certificate: unsupported DER length encoding")
    end = index + 1 + octets
    if end > len(data):
        raise DhanCertPinError("malformed certificate: truncated long-form length")
    return int.from_bytes(data[index + 1 : end], "big"), end


def _read_tlv(data: bytes, index: int) -> tuple[int, int, int]:
    """Read one DER TLV at *index*; return ``(tag, value_start, value_end)``.

    Tags are assumed single-octet (true for every structural certificate tag —
    SEQUENCE, the ``[0]`` version, INTEGER, etc.); high-tag-number form does not
    appear in the fields this parser traverses.
    """
    if index >= len(data):
        raise DhanCertPinError("malformed certificate: truncated tag")
    tag = data[index]
    length, value_start = _read_length(data, index + 1)
    value_end = value_start + length
    if value_end > len(data):
        raise DhanCertPinError("malformed certificate: element overruns buffer")
    return tag, value_start, value_end


def _extract_spki_der(cert_der: bytes) -> bytes:
    """Return the raw DER ``SubjectPublicKeyInfo`` TLV from an X.509 certificate.

    Walks the minimal structure required (RFC 5280 §4.1)::

        Certificate     ::= SEQUENCE { tbsCertificate, sigAlg, sig }
        TBSCertificate  ::= SEQUENCE { [0] version OPTIONAL, serialNumber,
                                       signature, issuer, validity, subject,
                                       subjectPublicKeyInfo, ... }

    so the SPKI is the element after an optional ``[0]`` version plus the five
    fields before it. The returned slice is the COMPLETE TLV (tag + length +
    value), which is what gets hashed for the pin.
    """
    cert_tag, tbs_start, _cert_end = _read_tlv(cert_der, 0)
    if cert_tag != _DER_SEQUENCE:
        raise DhanCertPinError("malformed certificate: top-level is not a SEQUENCE")
    tbs_tag, field_pos, tbs_end = _read_tlv(cert_der, tbs_start)
    if tbs_tag != _DER_SEQUENCE:
        raise DhanCertPinError("malformed certificate: tbsCertificate is not a SEQUENCE")
    # Skip the optional version [0], then the five fields before the SPKI.
    first_tag, _vs, version_end = _read_tlv(cert_der, field_pos)
    if first_tag == _DER_VERSION_CTX:
        field_pos = version_end
    for _ in range(_TBS_FIELDS_BEFORE_SPKI):
        _tag, _vstart, field_pos = _read_tlv(cert_der, field_pos)
    if field_pos >= tbs_end:
        raise DhanCertPinError("malformed certificate: no subjectPublicKeyInfo")
    spki_tag, _spki_value_start, spki_end = _read_tlv(cert_der, field_pos)
    if spki_tag != _DER_SEQUENCE:
        raise DhanCertPinError("malformed certificate: subjectPublicKeyInfo is not a SEQUENCE")
    return cert_der[field_pos:spki_end]


def _normalise_pin(raw: str) -> str:
    """Return *raw* stripped of whitespace and an optional ``sha256/`` prefix."""
    pin = raw.strip()
    if pin.lower().startswith(_PIN_PREFIX):
        pin = pin[len(_PIN_PREFIX) :].strip()
    return pin


def parse_pins(value: str) -> frozenset[str]:
    """Parse a comma/whitespace-separated pin string into a normalised pin set.

    Empty tokens are dropped, an optional ``sha256/`` prefix is stripped, and the
    order is irrelevant (a set). Used by :meth:`PinPolicy.from_env`.
    """
    tokens = value.replace(",", " ").split()
    return frozenset(_normalise_pin(token) for token in tokens if _normalise_pin(token))


def _noop_alert(message: str) -> None:
    """Default :attr:`PinPolicy.alert` hook — no external alert (audit log still fires)."""


@dataclass(frozen=True, slots=True)
class PinPolicy:
    """An operator-configured SPKI pin set + its enforce/opt-out decision.

    Immutable and injectable: the live transport builds one from the environment
    (:meth:`from_env`); tests construct one directly. :meth:`verify` is the pure,
    fully-tested decision — given a peer certificate it returns silently (match,
    or a SAFE fallback when not enforced) or raises :class:`DhanCertPinError`
    (mismatch / absent cert when enforced), auditing + alerting on mismatch.

    Attributes
    ----------
    pins
        The accepted ``base64(SHA-256(SPKI))`` pins. Empty ⇒ pinning not enforced
        (safe fallback to system-CA validation). Multiple pins ⇒ rotation-ready
        (current + next key both accepted).
    disabled
        Explicit break-glass opt-out (``BRUTEX_DHAN_TLS_PIN_DISABLE``); when
        ``True`` the check is skipped even if :attr:`pins` is non-empty.
    alert
        Best-effort security-alert hook invoked (in addition to the CRITICAL
        audit log) on a mismatch. Defaults to a no-op; production wiring injects
        a Telegram bridge.
    """

    pins: frozenset[str] = frozenset()
    disabled: bool = False
    alert: Callable[[str], None] = field(default=_noop_alert)

    @property
    def enforced(self) -> bool:
        """``True`` iff a non-empty pin set is configured AND not opted-out."""
        return bool(self.pins) and not self.disabled

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        alert: Callable[[str], None] | None = None,
    ) -> PinPolicy:
        """Build a policy from :data:`ENV_TLS_PINS` / :data:`ENV_TLS_PIN_DISABLE`.

        Parameters
        ----------
        env
            Environment mapping. Defaults to :data:`os.environ`.
        alert
            Optional security-alert hook (see :attr:`alert`). Defaults to the
            no-op (the CRITICAL audit log always fires regardless).

        Returns
        -------
        PinPolicy
            The resolved policy. With no pins configured the result is the SAFE
            fallback (``enforced`` is ``False``).
        """
        source = os.environ if env is None else env
        pins = parse_pins(source.get(ENV_TLS_PINS, ""))
        disabled = source.get(ENV_TLS_PIN_DISABLE, "").strip().lower() in _TRUTHY
        return cls(pins=pins, disabled=disabled, alert=alert or _noop_alert)

    def verify(self, cert_der: bytes | None, *, host: str) -> None:
        """Enforce the pin against the peer certificate *cert_der* for *host*.

        Decision table (Golden Rule #10 — explicit, never silent):

        * **Opt-out** (:data:`ENV_TLS_PIN_DISABLE`): a one-line WARNING, return
          (the bypass is visible, never invisible).
        * **No pins configured**: a DEBUG note, return — the SAFE fallback to
          system-CA validation; a missing config never breaks a pull.
        * **Enforced + matching key**: a DEBUG confirmation, return.
        * **Enforced + absent/empty cert**: raise (cannot verify ⇒ refuse).
        * **Enforced + MISMATCH**: emit the CRITICAL ``dhan.tls.pin_mismatch``
          audit line, fire :attr:`alert`, and raise — NEVER fall back.

        Parameters
        ----------
        cert_der
            Peer certificate (DER), or ``None``/empty when the transport could
            not obtain one.
        host
            The connected host (for the audit/alert context, e.g. ``api.dhan.co``).

        Raises
        ------
        DhanCertPinError
            When pinning is enforced and the peer key does not match (or no peer
            certificate was presented).
        """
        if self.disabled:
            _LOG.warning("dhan.tls.pin_disabled", host=host)
            return
        if not self.pins:
            _LOG.debug("dhan.tls.pin_unconfigured", host=host)
            return
        if not cert_der:
            self._fail(host, actual=None)
        actual = spki_sha256_pin(cert_der)
        if actual in self.pins:
            _LOG.debug("dhan.tls.pin_ok", host=host, pin=actual)
            return
        self._fail(host, actual=actual)

    def _fail(self, host: str, *, actual: str | None) -> NoReturn:
        """Audit + alert + raise on a pin failure (mismatch or absent cert)."""
        # A pin is a PUBLIC key hash, never a secret — safe to log/alert in full.
        message = (
            f"TLS SPKI pin mismatch for {host}: peer key pin "
            f"{actual or '<none-presented>'} is not in the configured pin set "
            f"({len(self.pins)} pin(s)) — refusing connection (possible MITM)"
        )
        _LOG.critical(
            "dhan.tls.pin_mismatch",
            host=host,
            actual_pin=actual,
            configured_pins=len(self.pins),
        )
        self.alert(message)
        raise DhanCertPinError(message)


def pins_from_certs(certs_der: Iterable[bytes]) -> frozenset[str]:
    """Compute the SPKI pin set for *certs_der* — an operator pin-bootstrap helper.

    Convenience for an operator (or a one-shot ``brutex`` helper) to derive the
    ``BRUTEX_DHAN_TLS_PINS`` value from the certificate(s) they currently trust,
    so the pin set is generated from real material, never hand-typed.
    """
    return frozenset(spki_sha256_pin(cert) for cert in certs_der)


__all__ = [
    "ENV_TLS_PINS",
    "ENV_TLS_PIN_DISABLE",
    "DhanCertPinError",
    "PinPolicy",
    "parse_pins",
    "pins_from_certs",
    "spki_sha256_pin",
]
