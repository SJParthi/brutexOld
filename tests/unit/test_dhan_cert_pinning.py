"""Tests for `brutex.adapters.dhan.cert_pinning` — SPKI TLS pinning.

100% line+branch of the pure verification core (DEC-DHAN-TLS-SPKI-PIN-001):

* DER → SubjectPublicKeyInfo extraction + the SPKI pin, cross-checked against a
  REAL `cryptography`-generated certificate AND every malformed-DER branch via
  hand-built fixtures.
* `parse_pins` / `PinPolicy.from_env` config parsing (pins, opt-out, missing).
* `PinPolicy.verify` decision table: opt-out, no-config fallback, pin MATCH,
  ROTATION (multi-pin), MISMATCH (audit + alert + raise), absent cert.

The socket-level peer-cert extraction in `client.make_pinning_transport` is the
`# pragma: no cover` network shim; this exercises the policy it delegates to.

Whitebox: imports the module-private DER helpers for the malformed-input cases.
"""

# pyright: reportPrivateUsage=none
from __future__ import annotations

import base64
import hashlib

import pytest
from brutex.adapters.dhan.cert_pinning import (
    ENV_TLS_PIN_DISABLE,
    ENV_TLS_PINS,
    DhanCertPinError,
    PinPolicy,
    _extract_spki_der,
    _read_length,
    _read_tlv,
    parse_pins,
    pins_from_certs,
    spki_sha256_pin,
)
from brutex.adapters.dhan.errors import DhanError, is_retryable
from structlog.testing import capture_logs

# ── DER fixture helpers ──────────────────────────────────────────────────


def _der_len(length: int) -> bytes:
    """Encode *length* as a DER length octet sequence (short or long form)."""
    if length < 0x80:
        return bytes([length])
    body = length.to_bytes((length.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(body)]) + body


def _tlv(tag: int, value: bytes) -> bytes:
    """Encode one DER TLV: *tag* ‖ length(*value*) ‖ *value*."""
    return bytes([tag]) + _der_len(len(value)) + value


_SEQUENCE = 0x30
_INTEGER = 0x02
_VERSION_CTX = 0xA0

#: Five dummy TBSCertificate fields (serial, sigAlg, issuer, validity, subject).
_FIVE_FIELDS = _tlv(_INTEGER, b"\x01") * 5


def _spki(pubkey: bytes = b"\x00\x01\x02\x03") -> bytes:
    """A minimal SubjectPublicKeyInfo SEQUENCE wrapping *pubkey* bytes."""
    return _tlv(_SEQUENCE, pubkey)


def _certificate(*, with_version: bool, spki: bytes) -> bytes:
    """Build a minimal DER certificate exposing *spki* for extraction.

    Only the prefix the extractor walks is modelled (version? + 5 fields + SPKI);
    the trailing signatureAlgorithm/signature are unnecessary (never read).
    """
    version = _tlv(_VERSION_CTX, _tlv(_INTEGER, b"\x02")) if with_version else b""
    tbs = _tlv(_SEQUENCE, version + _FIVE_FIELDS + spki)
    return _tlv(_SEQUENCE, tbs)


# ── SPKI extraction + pin (real cert cross-check) ────────────────────────


def test_spki_pin_matches_cryptography_on_a_real_certificate() -> None:
    """The hand-rolled SPKI pin equals `cryptography`'s on a real X.509 cert."""
    crypto_x509 = pytest.importorskip("cryptography.x509")
    serialization = pytest.importorskip("cryptography.hazmat.primitives.serialization")
    rsa = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.rsa")
    hashes = pytest.importorskip("cryptography.hazmat.primitives.hashes")
    from datetime import UTC, datetime, timedelta

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = crypto_x509.Name(
        [crypto_x509.NameAttribute(crypto_x509.NameOID.COMMON_NAME, "api.dhan.co")]
    )
    now = datetime.now(UTC)
    cert = (
        crypto_x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(crypto_x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    cert_der = cert.public_bytes(serialization.Encoding.DER)
    spki_der = cert.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    expected = base64.b64encode(hashlib.sha256(spki_der).digest()).decode("ascii")

    assert spki_sha256_pin(cert_der) == expected
    assert _extract_spki_der(cert_der) == spki_der


def test_extract_spki_handles_version_present_and_absent() -> None:
    """SPKI is found whether or not the optional `[0] version` field is present."""
    spki = _spki(b"PUBKEYBYTES")
    assert _extract_spki_der(_certificate(with_version=True, spki=spki)) == spki
    assert _extract_spki_der(_certificate(with_version=False, spki=spki)) == spki


def test_extract_spki_handles_long_form_length() -> None:
    """A >127-byte public key (long-form DER length) is parsed correctly."""
    spki = _spki(b"\xab" * 200)
    cert = _certificate(with_version=True, spki=spki)
    assert _extract_spki_der(cert) == spki


def test_spki_pin_is_base64_sha256_of_spki() -> None:
    """`spki_sha256_pin` is exactly base64(SHA-256(SPKI DER))."""
    spki = _spki(b"key-material")
    cert = _certificate(with_version=False, spki=spki)
    expected = base64.b64encode(hashlib.sha256(spki).digest()).decode("ascii")
    assert spki_sha256_pin(cert) == expected


# ── malformed DER branches ───────────────────────────────────────────────


def test_read_length_short_and_long_forms() -> None:
    """Short-form and definite long-form lengths decode to the right value."""
    assert _read_length(b"\x05", 0) == (5, 1)
    assert _read_length(b"\x82\x01\x00", 0) == (256, 3)


@pytest.mark.parametrize(
    "data",
    [
        b"",  # truncated: no length octet
        b"\x80",  # indefinite form (octets == 0)
        b"\x85\x00\x00\x00\x00\x00",  # over-wide (octets > 4)
        b"\x82\x00",  # long form claims 2 octets, only 1 present
    ],
)
def test_read_length_rejects_malformed(data: bytes) -> None:
    """Every unsupported/truncated DER length raises a pin error."""
    with pytest.raises(DhanCertPinError):
        _read_length(data, 0)


def test_read_tlv_rejects_truncated_tag_and_overrun() -> None:
    """A TLV with a missing tag or a value that overruns the buffer is rejected."""
    with pytest.raises(DhanCertPinError, match="truncated tag"):
        _read_tlv(b"", 0)
    with pytest.raises(DhanCertPinError, match="overruns buffer"):
        _read_tlv(b"\x02\x05\x00", 0)  # INTEGER len 5, only 1 byte of value


def test_extract_spki_rejects_non_sequence_certificate() -> None:
    """A top-level that is not a SEQUENCE is rejected."""
    with pytest.raises(DhanCertPinError, match="top-level is not a SEQUENCE"):
        _extract_spki_der(_tlv(0x31, b"\x00"))


def test_extract_spki_rejects_non_sequence_tbs() -> None:
    """A tbsCertificate that is not a SEQUENCE is rejected."""
    cert = _tlv(_SEQUENCE, _tlv(_INTEGER, b"\x01"))
    with pytest.raises(DhanCertPinError, match="tbsCertificate is not a SEQUENCE"):
        _extract_spki_der(cert)


def test_extract_spki_rejects_missing_spki() -> None:
    """A tbsCertificate that ends right after the five pre-SPKI fields is rejected."""
    tbs = _tlv(_SEQUENCE, _FIVE_FIELDS)  # no SPKI element follows
    cert = _tlv(_SEQUENCE, tbs)
    with pytest.raises(DhanCertPinError, match="no subjectPublicKeyInfo"):
        _extract_spki_der(cert)


def test_extract_spki_rejects_non_sequence_spki() -> None:
    """A subjectPublicKeyInfo that is not a SEQUENCE is rejected."""
    not_a_seq_spki = _tlv(_INTEGER, b"\x09")
    cert = _certificate(with_version=False, spki=not_a_seq_spki)
    with pytest.raises(DhanCertPinError, match="subjectPublicKeyInfo is not a SEQUENCE"):
        _extract_spki_der(cert)


def test_spki_pin_propagates_malformed_error() -> None:
    """`spki_sha256_pin` surfaces the malformed-cert error from extraction."""
    with pytest.raises(DhanCertPinError):
        spki_sha256_pin(b"\x00\x00")


# ── pin parsing / config ─────────────────────────────────────────────────


def test_parse_pins_splits_comma_and_whitespace_and_strips_prefix() -> None:
    """Pins parse from comma/whitespace lists with an optional `sha256/` prefix."""
    parsed = parse_pins("  AAAA==, sha256/BBBB==   CCCC==  ")
    assert parsed == frozenset({"AAAA==", "BBBB==", "CCCC=="})


def test_parse_pins_empty_is_empty_set() -> None:
    """An empty / whitespace-only pin string yields no pins (safe fallback)."""
    assert parse_pins("") == frozenset()
    assert parse_pins("   ,  , ") == frozenset()


def test_from_env_reads_pins_and_default_not_disabled() -> None:
    """`from_env` loads the pin set; absence of the opt-out keeps it enforced."""
    policy = PinPolicy.from_env({ENV_TLS_PINS: "AAAA==,BBBB=="})
    assert policy.pins == frozenset({"AAAA==", "BBBB=="})
    assert policy.disabled is False
    assert policy.enforced is True


def test_from_env_missing_config_is_safe_fallback() -> None:
    """No env config ⇒ no pins, not enforced (system-CA validation only)."""
    policy = PinPolicy.from_env({})
    assert policy.pins == frozenset()
    assert policy.enforced is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", "On"])
def test_from_env_opt_out_truthy_variants(value: str) -> None:
    """Each truthy spelling of the opt-out disables enforcement even with pins."""
    policy = PinPolicy.from_env({ENV_TLS_PINS: "AAAA==", ENV_TLS_PIN_DISABLE: value})
    assert policy.disabled is True
    assert policy.enforced is False


@pytest.mark.parametrize("value", ["0", "false", "no", "", "  "])
def test_from_env_opt_out_falsy_variants_stay_enforced(value: str) -> None:
    """A non-truthy opt-out value leaves a configured pin set enforced."""
    policy = PinPolicy.from_env({ENV_TLS_PINS: "AAAA==", ENV_TLS_PIN_DISABLE: value})
    assert policy.disabled is False
    assert policy.enforced is True


def test_from_env_uses_default_noop_alert() -> None:
    """`from_env` without an alert hook wires the no-op (mismatch still raises)."""
    spki = _spki(b"real-key")
    cert = _certificate(with_version=False, spki=spki)
    policy = PinPolicy.from_env({ENV_TLS_PINS: "not-the-pin"})
    with pytest.raises(DhanCertPinError):
        policy.verify(cert, host="api.dhan.co")


# ── verify decision table ────────────────────────────────────────────────


def _cert_and_pin(pubkey: bytes = b"genuine-key") -> tuple[bytes, str]:
    """Return a (cert_der, expected_pin) pair built from the same key material."""
    cert = _certificate(with_version=True, spki=_spki(pubkey))
    return cert, spki_sha256_pin(cert)


def test_verify_opt_out_returns_and_warns() -> None:
    """An explicit opt-out skips the check and logs a visible WARNING."""
    cert, _pin = _cert_and_pin()
    policy = PinPolicy(pins=frozenset({"whatever"}), disabled=True)
    with capture_logs() as logs:
        policy.verify(cert, host="api.dhan.co")
    assert any(e["event"] == "dhan.tls.pin_disabled" for e in logs)


def test_verify_no_pins_is_safe_fallback() -> None:
    """No configured pins ⇒ return (safe fallback) with a DEBUG note."""
    cert, _pin = _cert_and_pin()
    policy = PinPolicy()
    with capture_logs() as logs:
        policy.verify(cert, host="api.dhan.co")
    assert any(e["event"] == "dhan.tls.pin_unconfigured" for e in logs)


def test_verify_match_passes() -> None:
    """A peer key whose pin is configured passes and logs `pin_ok`."""
    cert, pin = _cert_and_pin()
    policy = PinPolicy(pins=frozenset({pin}))
    with capture_logs() as logs:
        policy.verify(cert, host="api.dhan.co")
    ok = [e for e in logs if e["event"] == "dhan.tls.pin_ok"]
    assert ok and ok[0]["pin"] == pin


def test_verify_rotation_accepts_any_of_multiple_pins() -> None:
    """With current+next pins configured, the matching (2nd) pin is accepted."""
    cert, pin = _cert_and_pin()
    policy = PinPolicy(pins=frozenset({"old-rotated-out-pin", pin}))
    policy.verify(cert, host="api.dhan.co")  # no raise


def test_verify_mismatch_audits_alerts_and_raises() -> None:
    """A non-matching peer key raises, emits the CRITICAL audit, and alerts."""
    cert, _pin = _cert_and_pin()
    alerts: list[str] = []
    policy = PinPolicy(pins=frozenset({"some-other-pin"}), alert=alerts.append)
    with capture_logs() as logs, pytest.raises(DhanCertPinError, match="pin mismatch"):
        policy.verify(cert, host="api.dhan.co")
    critical = [e for e in logs if e["event"] == "dhan.tls.pin_mismatch"]
    assert critical and critical[0]["log_level"] == "critical"
    assert critical[0]["host"] == "api.dhan.co"
    assert critical[0]["configured_pins"] == 1
    assert len(alerts) == 1
    assert "possible MITM" in alerts[0]


@pytest.mark.parametrize("cert", [None, b""])
def test_verify_absent_cert_when_enforced_raises(cert: bytes | None) -> None:
    """An absent / empty peer cert under enforcement is refused (audit + alert)."""
    alerts: list[str] = []
    policy = PinPolicy(pins=frozenset({"a-pin"}), alert=alerts.append)
    with capture_logs() as logs, pytest.raises(DhanCertPinError, match="none-presented"):
        policy.verify(cert, host="api.dhan.co")
    assert any(e["event"] == "dhan.tls.pin_mismatch" for e in logs)
    assert len(alerts) == 1


def test_verify_absent_cert_not_enforced_is_noop() -> None:
    """An absent cert with NO pins configured is the safe fallback (no raise)."""
    PinPolicy().verify(None, host="api.dhan.co")


# ── misc ─────────────────────────────────────────────────────────────────


def test_pins_from_certs_bootstrap_helper() -> None:
    """`pins_from_certs` returns the SPKI pin set for the given certificates."""
    cert_a, pin_a = _cert_and_pin(b"key-a")
    cert_b, pin_b = _cert_and_pin(b"key-b")
    assert pins_from_certs([cert_a, cert_b]) == frozenset({pin_a, pin_b})


def test_cert_pin_error_is_terminal_not_retryable() -> None:
    """`DhanCertPinError` is a DhanError but NOT retryable (a MITM is never retried)."""
    err = DhanCertPinError("mismatch")
    assert isinstance(err, DhanError)
    assert is_retryable(err) is False
