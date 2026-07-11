"""Hypothesis differential: the Rust audit-chain kernel vs the CPython oracle.

DEC-AUDIT-CHAIN-RUST-PORT-001 (slice 9, ADR-0019): the STATISTICAL float-repr
proof (the 2^64 domain is provable only statistically — the pre-committed
escalation clause lives in ``rust/fno-math/src/audit_chain.rs``) plus the
payload-tree differential over the bounded value model and the envelope
refusal laws. Every comparison is byte-exact string equality against the
live CPython behaviour (``repr`` / ``audit._canonical_json`` /
``AuditEntry._compute_hash_python``), so a divergence on ANY generated case
fails loudly with the offending value in the falsifying example.
"""

from __future__ import annotations

import enum
import json
import math
import struct
from typing import Any, Final

import pytest
from brutex.orchestrator import audit
from brutex.orchestrator.audit import AuditEntry
from hypothesis import given
from hypothesis import strategies as st

fno_math = pytest.importorskip("fno_math")
if not hasattr(fno_math, "audit_entry_hash"):  # pragma: no cover - stale-wheel guard
    pytest.skip(
        "installed fno_math wheel predates the slice-9 audit symbols — rebuild the wheel",
        allow_module_level=True,
    )

# ── float repr differential ──────────────────────────────────────────────

#: The fixed adversarial corpus: every decpt notation boundary −5..18, the
#: signed zeros, subnormal floor, f64 extremes, 2^k ladder samples, the
#: float53 tie region, and the empirically-pinned spec vectors.
_FLOAT_CORPUS: Final[tuple[float, ...]] = (
    0.0,
    -0.0,
    5e-324,
    -5e-324,
    2.2250738585072014e-308,  # smallest normal
    1.7976931348623157e308,
    -1.7976931348623157e308,
    # decpt boundary ladder: 10^k for k in -6..18 (decpt = k+1).
    *(10.0**k for k in range(-6, 19)),
    *(-(10.0**k) for k in range(-6, 19)),
    # 2^k ladder samples across the exponent range.
    *(2.0**k for k in (-1074, -1022, -520, -53, -1, 0, 1, 52, 53, 60, 519, 1023)),
    9007199254740992.0,  # 2^53
    9007199254740993.0,  # rounds to 2^53 (tie)
    9999999999999998.0,  # decpt 16 — the last fixed-notation integer repr
    0.1,
    1.5,
    1 / 3,
    2 / 3,
    1.152921504606847e18,
    1700000000.0,
    1718000000.123456,
)


@pytest.mark.parametrize("value", _FLOAT_CORPUS, ids=repr)
def test_float_corpus_matches_cpython_repr(value: float) -> None:
    """The fixed adversarial corpus — byte-equal to ``repr``."""
    assert fno_math.py_float_repr(value) == repr(value)


@given(st.floats(allow_nan=False, allow_infinity=False))
def test_float_repr_differential_value_domain(value: float) -> None:
    """Random finite floats (value-biased generator) — byte-equal to ``repr``."""
    assert fno_math.py_float_repr(value) == repr(value)


@given(st.integers(0, 2**64 - 1))
def test_float_repr_differential_bit_domain(bits: int) -> None:
    """Random f64 BIT PATTERNS (subnormals/extremes included) — byte-equal."""
    (value,) = struct.unpack("<d", struct.pack("<Q", bits))
    if math.isnan(value) or math.isinf(value):
        with pytest.raises(fno_math.AuditEnvelopeError):
            fno_math.py_float_repr(value)
        return
    assert fno_math.py_float_repr(value) == repr(value)


# ── payload-tree differential ────────────────────────────────────────────

#: In-model scalars (Golden Rule: exactly the bounded model the kernel
#: serves — bigger ints and non-finite floats are the REFUSAL suite below).
_SCALARS = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(2**63), max_value=2**63 - 1),
    st.floats(allow_nan=False, allow_infinity=False),
    st.text(),  # default alphabet excludes surrogates — the crossable set
)

_PAYLOAD_VALUES = st.recursive(
    _SCALARS,
    lambda children: st.one_of(
        st.lists(children, max_size=4),
        st.dictionaries(st.text(), children, max_size=4),
    ),
    max_leaves=12,
)

_PAYLOADS = st.dictionaries(st.text(), _PAYLOAD_VALUES, max_size=5)


@given(_PAYLOADS)
def test_canonical_json_differential(payload: dict[str, Any]) -> None:
    """``fno_math.canonical_json`` ≡ ``audit._canonical_json`` byte-for-byte."""
    expected = audit._canonical_json(payload)  # pyright: ignore[reportPrivateUsage]
    assert fno_math.canonical_json(payload) == expected


@given(
    seq=st.integers(min_value=0, max_value=2**62),
    ts_utc=st.floats(allow_nan=False, allow_infinity=False),
    payload=_PAYLOADS,
)
def test_entry_hash_differential(seq: int, ts_utc: float, payload: dict[str, Any]) -> None:
    """``fno_math.audit_entry_hash`` ≡ the retained Python oracle body."""
    entry = AuditEntry(
        seq=seq,
        prev_hash="ab" * 32,
        ts_utc=ts_utc,
        actor="brutex",
        action="DIFFERENTIAL",
        subject="prop:hash",
        payload=payload,
    )
    expected = entry._compute_hash_python()  # pyright: ignore[reportPrivateUsage]
    assert (
        fno_math.audit_entry_hash(
            seq, "ab" * 32, ts_utc, "brutex", "DIFFERENTIAL", "prop:hash", payload
        )
        == expected
    )


class _Small(enum.IntEnum):
    """An int-subclass payload — json renders the BASE int repr."""

    A = 7


class _F(float):
    """A float-subclass payload — json renders the BASE float repr."""


def test_subclass_scalars_render_via_the_base_type() -> None:
    """IntEnum / float-subclass VALUES serve in-model (the encoder law)."""
    payload = {"e": _Small.A, "f": _F(1.5), "b": True}
    expected = audit._canonical_json(payload)  # pyright: ignore[reportPrivateUsage]
    assert expected == '{"b":true,"e":7,"f":1.5}'
    assert fno_math.canonical_json(payload) == expected


def test_bool_renders_before_int() -> None:
    """``True`` is an int subclass — it must render ``true``, never ``1``."""
    assert fno_math.canonical_json({"b": True, "i": 1}) == '{"b":true,"i":1}'


# ── envelope refusals + the seam's fallback identity ─────────────────────


@pytest.mark.parametrize(
    "payload",
    [
        {"big": 1 << 63},
        {"big": -(1 << 63) - 1},
        {"huge": 1 << 127},
        {"inf": float("inf")},
        {"nan": float("nan")},
        {"set": {1, 2}},
        {"bytes": b"raw"},
        {1: "non-str-key"},
        {True: "bool-key"},
        {"nested": {"deep": {2: "non-str"}}},
    ],
    ids=str,
)
def test_out_of_model_payloads_refuse_with_the_envelope_error(payload: dict[Any, Any]) -> None:
    """Every out-of-model payload raises ``AuditEnvelopeError`` — never junk."""
    with pytest.raises(fno_math.AuditEnvelopeError):
        fno_math.canonical_json(payload)
    with pytest.raises(fno_math.AuditEnvelopeError):
        fno_math.audit_entry_hash(0, "p", 1.0, "a", "b", "c", payload)


@pytest.mark.parametrize("payload", [{"big": 1 << 100}, {"mixed": [1, {"k": 1 << 90}]}], ids=str)
def test_seam_falls_back_to_the_oracle_on_big_ints(
    payload: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Flag-ON, an out-of-model payload serves TODAY's Python bytes exactly."""
    monkeypatch.setenv(audit.RUST_AUDIT_CHAIN_ENV, "1")
    audit._reset_rust_audit_for_tests()  # pyright: ignore[reportPrivateUsage]
    try:
        entry = AuditEntry(
            seq=0,
            prev_hash="0" * 64,
            ts_utc=1700000000.0,
            actor="brutex",
            action="BIG",
            subject="prop:big",
            payload=payload,
            entry_hash="h",
        )
        assert entry.compute_hash() == entry._compute_hash_python()  # pyright: ignore[reportPrivateUsage]
        assert entry.to_json_line() == entry._to_json_line_python()  # pyright: ignore[reportPrivateUsage]
        assert json.loads(entry.to_json_line())["payload"] == payload
    finally:
        audit._reset_rust_audit_for_tests()  # pyright: ignore[reportPrivateUsage]


def test_delimiter_in_field_refuses_defensively() -> None:
    """A ``|`` in a free-form field refuses at the kernel (defensive twin)."""
    with pytest.raises(fno_math.AuditEnvelopeError):
        fno_math.audit_entry_hash(0, "p", 1.0, "a|b", "ACT", "subj", {})
