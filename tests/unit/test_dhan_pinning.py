"""Tests for `brutex.adapters.dhan.pinning` — SPKI certificate pinning.

100% line+branch of the pure pinning surface (DEC-DHAN-TLS-SPKI-PIN-001): SPKI
digest computation, pin parse/validate, policy resolution (enforce / opt-out /
unconfigured), the one-shot mode banner, and the match check across the four
worst cases the security gap-sweep names — **pin match**, **pin mismatch**
(raises + alerts, never silent fallback), **key rotation** (multi-pin set), and
**CA-only fallback** (disabled + unconfigured no-ops). The thin urllib/ssl glue
that pulls the live peer cert is the `# pragma: no cover` network shim in
`client.py`.

Whitebox: imports the module-private `_normalise_pin` to assert the validator's
branches directly.
"""

# pyright: reportPrivateUsage=none
from __future__ import annotations

import datetime as dt

import pytest
from brutex.adapters.dhan.errors import DhanError, is_retryable
from brutex.adapters.dhan.pinning import (
    ENV_TLS_PINNING,
    ENV_TLS_PINS,
    PIN_PREFIX,
    DhanPinMismatchError,
    PinningMode,
    PinningPolicy,
    _normalise_pin,
    load_pinning_policy,
    log_policy,
    parse_pins,
    spki_sha256_pin,
    verify_pin,
)
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from structlog.testing import capture_logs


def _self_signed_der(key: ec.EllipticCurvePrivateKey, *, cn: str = "api.dhan.co") -> bytes:
    """Return a DER-encoded self-signed leaf certificate for *key* (test fixture)."""
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    now = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + dt.timedelta(days=365))
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.DER)


def _fresh_cert_der(cn: str = "api.dhan.co") -> bytes:
    """A brand-new self-signed cert (fresh EC key) in DER."""
    return _self_signed_der(ec.generate_private_key(ec.SECP256R1()), cn=cn)


@pytest.fixture
def cert_a() -> bytes:
    """A stable self-signed cert (key A) reused across a test."""
    return _self_signed_der(ec.generate_private_key(ec.SECP256R1()))


@pytest.fixture
def cert_b() -> bytes:
    """A second self-signed cert with a DIFFERENT key (the rotation / MITM key)."""
    return _self_signed_der(ec.generate_private_key(ec.SECP256R1()))


# ── spki_sha256_pin ──────────────────────────────────────────────────────────


def test_spki_pin_is_stable_and_prefixed(cert_a: bytes) -> None:
    pin = spki_sha256_pin(cert_a)
    assert pin.startswith(PIN_PREFIX)
    # Deterministic: same cert → same pin.
    assert pin == spki_sha256_pin(cert_a)


def test_spki_pin_pins_the_key_not_the_cert() -> None:
    # Same key, two different certs (different serials/validity) → SAME pin,
    # so a routine certificate RENEWAL does not break a key pin.
    key = ec.generate_private_key(ec.SECP256R1())
    der1 = _self_signed_der(key)
    der2 = _self_signed_der(key, cn="api.dhan.co")
    assert der1 != der2
    assert spki_sha256_pin(der1) == spki_sha256_pin(der2)


def test_spki_pin_differs_per_key(cert_a: bytes, cert_b: bytes) -> None:
    assert spki_sha256_pin(cert_a) != spki_sha256_pin(cert_b)


def test_spki_pin_rejects_unparseable_der() -> None:
    with pytest.raises(ValueError, match="un-parseable peer certificate"):
        spki_sha256_pin(b"not-a-certificate")


# ── _normalise_pin / parse_pins ──────────────────────────────────────────────


def test_normalise_pin_accepts_prefixed_and_bare(cert_a: bytes) -> None:
    pin = spki_sha256_pin(cert_a)
    bare = pin[len(PIN_PREFIX) :]
    assert _normalise_pin(pin) == pin
    assert _normalise_pin(bare) == pin
    # Surrounding whitespace is stripped.
    assert _normalise_pin(f"  {pin}  ") == pin


def test_normalise_pin_rejects_bad_base64() -> None:
    with pytest.raises(ValueError, match="invalid base64"):
        _normalise_pin("sha256/!!!not-base64!!!")


def test_normalise_pin_rejects_wrong_digest_length() -> None:
    # Valid base64, but not 32 bytes → not a SHA-256 SPKI pin.
    with pytest.raises(ValueError, match="32-byte SHA-256"):
        _normalise_pin("sha256/YWJj")  # "abc"


def test_parse_pins_blank_is_empty() -> None:
    assert parse_pins("") == frozenset()
    assert parse_pins("   ") == frozenset()


def test_parse_pins_splits_on_comma_and_whitespace(cert_a: bytes, cert_b: bytes) -> None:
    pa = spki_sha256_pin(cert_a)
    pb = spki_sha256_pin(cert_b)
    assert parse_pins(f"{pa}, {pb}") == frozenset({pa, pb})
    assert parse_pins(f"{pa}\n{pb}") == frozenset({pa, pb})


# ── load_pinning_policy ──────────────────────────────────────────────────────


def test_load_policy_enforce_when_pins_set(cert_a: bytes) -> None:
    pa = spki_sha256_pin(cert_a)
    policy = load_pinning_policy({ENV_TLS_PINS: pa})
    assert policy.mode is PinningMode.ENFORCE
    assert policy.enforced is True
    assert policy.pins == frozenset({pa})


def test_load_policy_unconfigured_when_nothing_set() -> None:
    policy = load_pinning_policy({})
    assert policy.mode is PinningMode.UNCONFIGURED
    assert policy.enforced is False
    assert policy.pins == frozenset()


@pytest.mark.parametrize("value", ["0", "off", "false", "NO", "Disable", "disabled"])
def test_load_policy_disabled_on_opt_out(value: str) -> None:
    policy = load_pinning_policy({ENV_TLS_PINNING: value})
    assert policy.mode is PinningMode.DISABLED
    assert policy.enforced is False


def test_load_policy_opt_out_wins_over_pins(cert_a: bytes) -> None:
    # A configured pin set stays inert when the operator explicitly opts out.
    pa = spki_sha256_pin(cert_a)
    policy = load_pinning_policy({ENV_TLS_PINS: pa, ENV_TLS_PINNING: "off"})
    assert policy.mode is PinningMode.DISABLED
    assert policy.pins == frozenset()


def test_load_policy_unknown_toggle_is_ignored(cert_a: bytes) -> None:
    # A non-disable toggle value does NOT disable; pins still enforce.
    pa = spki_sha256_pin(cert_a)
    policy = load_pinning_policy({ENV_TLS_PINS: pa, ENV_TLS_PINNING: "yes-please"})
    assert policy.mode is PinningMode.ENFORCE


def test_load_policy_propagates_malformed_pin() -> None:
    with pytest.raises(ValueError, match="invalid base64"):
        load_pinning_policy({ENV_TLS_PINS: "sha256/###"})


def test_load_policy_defaults_to_os_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_TLS_PINS, raising=False)
    monkeypatch.delenv(ENV_TLS_PINNING, raising=False)
    assert load_pinning_policy().mode is PinningMode.UNCONFIGURED


# ── log_policy (one-shot mode banner) ────────────────────────────────────────


def test_log_policy_enforce_is_info(cert_a: bytes) -> None:
    pa = spki_sha256_pin(cert_a)
    with capture_logs() as logs:
        log_policy(PinningPolicy(PinningMode.ENFORCE, frozenset({pa})))
    (entry,) = logs
    assert entry["event"] == "tls.pinning"
    assert entry["log_level"] == "info"
    assert entry["mode"] == "enforce"
    assert entry["pins"] == 1


def test_log_policy_disabled_is_info() -> None:
    with capture_logs() as logs:
        log_policy(PinningPolicy(PinningMode.DISABLED, frozenset()))
    (entry,) = logs
    assert entry["log_level"] == "info"
    assert entry["mode"] == "disabled"


def test_log_policy_unconfigured_warns() -> None:
    with capture_logs() as logs:
        log_policy(PinningPolicy(PinningMode.UNCONFIGURED, frozenset()))
    (entry,) = logs
    assert entry["log_level"] == "warning"
    assert entry["mode"] == "unconfigured"
    assert ENV_TLS_PINS in entry["detail"]


# ── verify_pin ───────────────────────────────────────────────────────────────


def test_verify_pin_match_passes_and_logs_ok(cert_a: bytes) -> None:
    pa = spki_sha256_pin(cert_a)
    policy = PinningPolicy(PinningMode.ENFORCE, frozenset({pa}))
    with capture_logs() as logs:
        verify_pin(cert_a, policy)  # no raise
    assert any(e["event"] == "tls.pin_ok" for e in logs)


def test_verify_pin_mismatch_raises_and_alerts(cert_a: bytes, cert_b: bytes) -> None:
    # Pin key A, but the peer presents key B (the MITM / mis-issued case).
    pa = spki_sha256_pin(cert_a)
    policy = PinningPolicy(PinningMode.ENFORCE, frozenset({pa}))
    with capture_logs() as logs, pytest.raises(DhanPinMismatchError, match="SPKI pin mismatch"):
        verify_pin(cert_b, policy, host="api.dhan.co")
    alert = next(e for e in logs if e["event"] == "tls.pin_mismatch")
    assert alert["log_level"] == "error"
    assert alert["host"] == "api.dhan.co"
    assert alert["actual"] == spki_sha256_pin(cert_b)
    assert alert["expected"] == [pa]


def test_verify_pin_rotation_matches_any_configured_pin(cert_a: bytes, cert_b: bytes) -> None:
    # Rotation: both the current (A) and next (B) key pins are configured, so
    # the peer presenting EITHER key verifies with zero downtime.
    pa = spki_sha256_pin(cert_a)
    pb = spki_sha256_pin(cert_b)
    policy = PinningPolicy(PinningMode.ENFORCE, frozenset({pa, pb}))
    verify_pin(cert_a, policy)  # old key — ok
    verify_pin(cert_b, policy)  # new key — ok


def test_verify_pin_noop_when_disabled(cert_a: bytes) -> None:
    # CA-only fallback: an opt-out policy never inspects the cert.
    policy = PinningPolicy(PinningMode.DISABLED, frozenset())
    with capture_logs() as logs:
        verify_pin(b"ignored-not-even-parsed", policy)
    assert logs == []


def test_verify_pin_noop_when_unconfigured() -> None:
    policy = PinningPolicy(PinningMode.UNCONFIGURED, frozenset())
    verify_pin(b"ignored", policy)  # no raise, no parse


# ── error taxonomy ───────────────────────────────────────────────────────────


def test_pin_mismatch_is_terminal_dhan_error() -> None:
    exc = DhanPinMismatchError("boom")
    assert isinstance(exc, DhanError)
    # A pin mismatch is NEVER fast-retried — re-issuing would re-leak the
    # credentialed request to the interceptor.
    assert is_retryable(exc) is False
