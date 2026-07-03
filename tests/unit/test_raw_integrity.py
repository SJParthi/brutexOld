"""Unit + property + golden tests for ``brutex.data.raw_integrity``.

DEC-RAW-DATA-INTEGRITY-001. 100% line+branch of the PURE integrity core:
canonicalisation determinism, blake2b hashing, the record dataclass + key,
build_record, and verify_record (match + mismatch). The two operator-critical
properties — same payload → same hash (idempotence), a 1-byte change → a
different hash (tamper detection) — are proven by hypothesis + a frozen golden.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from typing import TypedDict

import pytest
from brutex.data.raw_integrity import (
    HASH_ALGORITHM,
    RawIntegrityError,
    VerifyResult,
    build_record,
    canonicalize_payload,
    hash_payload,
    verify_record,
)
from hypothesis import given
from hypothesis import strategies as st

_PAYLOAD = {
    "candles": [
        ["2024-01-01T09:15:00", 100.0, 110.0, 90.0, 105.0, 777, 42],
        ["2024-01-01T09:16:00", 105.0, 112.0, 104.0, 111.0, 800, 43],
    ]
}


class _Coords(TypedDict):
    """Exactly the 6 fetch-coordinate kwargs of build_record (no ``now``)."""

    exchange: str
    segment: str
    groww_symbol: str
    interval: str
    window_start: str
    window_end: str


def _coords() -> _Coords:
    return _Coords(
        exchange="NSE",
        segment="CASH",
        groww_symbol="NSE-NIFTY",
        interval="1minute",
        window_start="2024-01-01 00:00:00",
        window_end="2024-01-31 00:00:00",
    )


# --------------------------------------------------------------------------- #
# canonicalize_payload
# --------------------------------------------------------------------------- #
def test_canonicalize_candles_only() -> None:
    """The hash input is the candle rows, serialised compact + sorted."""
    assert canonicalize_payload(_PAYLOAD) == (
        b'[["2024-01-01T09:15:00",100.0,110.0,90.0,105.0,777,42],'
        b'["2024-01-01T09:16:00",105.0,112.0,104.0,111.0,800,43]]'
    )


def test_canonicalize_ignores_envelope_noise() -> None:
    """A non-candle envelope field never changes the canonical bytes."""
    base = canonicalize_payload(_PAYLOAD)
    noisy = {**_PAYLOAD, "status": "ok", "server_ts": 12345}
    assert canonicalize_payload(noisy) == base


def test_canonicalize_missing_candles_is_empty_list() -> None:
    """A payload with no 'candles' key canonicalises to an empty list (real state)."""
    assert canonicalize_payload({}) == b"[]"
    assert canonicalize_payload({"other": 1}) == b"[]"


def test_canonicalize_rejects_non_mapping() -> None:
    with pytest.raises(RawIntegrityError, match="must be a mapping"):
        canonicalize_payload([1, 2, 3])  # type: ignore[arg-type]


def test_canonicalize_rejects_non_serialisable_candles() -> None:
    with pytest.raises(RawIntegrityError, match="not JSON-serialisable"):
        canonicalize_payload({"candles": object()})


# --------------------------------------------------------------------------- #
# hash_payload — determinism + tamper detection (golden + unit)
# --------------------------------------------------------------------------- #
def test_hash_is_64_hex_chars() -> None:
    digest = hash_payload(_PAYLOAD)
    assert len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)


def test_hash_golden_frozen_value() -> None:
    """GOLDEN: the exact blake2b digest of the canonical fixture — pins the algo.

    A change here means the hash algorithm / canonicalisation changed, which would
    break every recorded proof — so this frozen value is the contract.
    """
    assert hash_payload(_PAYLOAD) == (
        "6efde60fa62320bf1b15eda6b132ac27b7f687f3ff731b17b2e204426b8e38c5"
    )


def test_hash_deterministic_across_dict_copies() -> None:
    """Same candle series (independent copy) → identical hash (idempotence)."""
    assert hash_payload(_PAYLOAD) == hash_payload(copy.deepcopy(_PAYLOAD))


def test_hash_changes_on_one_value_tamper() -> None:
    """A single changed cell yields a different hash (tamper detection)."""
    tampered = copy.deepcopy(_PAYLOAD)
    tampered["candles"][0][4] = 105.01  # close 105.0 → 105.01
    assert hash_payload(tampered) != hash_payload(_PAYLOAD)


@given(
    st.lists(
        st.lists(
            st.one_of(
                st.text(max_size=12),
                st.floats(allow_nan=False, allow_infinity=False, width=32),
                st.integers(min_value=-(10**9), max_value=10**9),
            ),
            min_size=1,
            max_size=7,
        ),
        max_size=8,
    )
)
def test_property_hash_is_deterministic(rows: list[list[object]]) -> None:
    """PROPERTY: an identical payload always hashes to the same value."""
    payload = {"candles": rows}
    assert hash_payload(payload) == hash_payload({"candles": copy.deepcopy(rows)})


@given(
    st.lists(
        st.lists(st.integers(min_value=0, max_value=1000), min_size=2, max_size=7),
        min_size=1,
        max_size=6,
    ),
    st.integers(min_value=0, max_value=5),
)
def test_property_one_byte_change_changes_hash(rows: list[list[int]], bump: int) -> None:
    """PROPERTY: bumping one cell by a non-zero delta always changes the hash."""
    payload = {"candles": copy.deepcopy(rows)}
    base = hash_payload(payload)
    tampered = copy.deepcopy(rows)
    idx = bump % len(tampered[0])
    tampered[0][idx] = tampered[0][idx] + 1  # guaranteed-different value
    assert hash_payload({"candles": tampered}) != base


# --------------------------------------------------------------------------- #
# RawIntegrityRecord + build_record
# --------------------------------------------------------------------------- #
def test_build_record_captures_hash_and_coords() -> None:
    now = datetime(2024, 6, 1, 12, 0, tzinfo=UTC)
    record = build_record(_PAYLOAD, now=now, **_coords())
    assert record.payload_hash == hash_payload(_PAYLOAD)
    assert record.row_count == 2
    assert record.algorithm == HASH_ALGORITHM
    assert record.captured_at == now
    assert record.key == (
        "NSE",
        "CASH",
        "NSE-NIFTY",
        "1minute",
        "2024-01-01 00:00:00",
        "2024-01-31 00:00:00",
    )


def test_build_record_defaults_captured_at_to_now() -> None:
    before = datetime.now(UTC)
    record = build_record(_PAYLOAD, **_coords())
    assert record.captured_at is not None
    assert record.captured_at >= before


def test_build_record_row_count_zero_for_empty() -> None:
    assert build_record({}, **_coords()).row_count == 0
    assert build_record({"candles": "nope"}, **_coords()).row_count == 0


def test_build_record_propagates_malformed_payload() -> None:
    with pytest.raises(RawIntegrityError):
        build_record({"candles": object()}, **_coords())


def test_build_record_issued_symbol_default_is_none() -> None:
    # LEGACY / plain path: no issued form recorded → verify re-issues the key symbol.
    record = build_record(_PAYLOAD, **_coords())
    assert record.issued_symbol is None
    assert record.fetch_symbol == "NSE-NIFTY"


def test_build_record_normalises_identical_issued_symbol_to_none() -> None:
    # The common plain-form fetch (issued == key) normalises to None so the record
    # stays byte-identical to the pre-field shape (back-compat by construction).
    record = build_record(_PAYLOAD, issued_symbol="NSE-NIFTY", **_coords())
    assert record.issued_symbol is None
    assert record.fetch_symbol == "NSE-NIFTY"


def test_build_record_keeps_divergent_issued_symbol_and_key_unchanged() -> None:
    # DEC-GROWW-AMPERSAND-CANDLE-FALLBACK-001 Fix-1: the `%26` fallback recovery
    # records the ENCODED issued form while the 6-tuple key stays the plain symbol
    # (latest-wins semantics preserved; fetch_symbol drives the verify re-issue).
    coords = _coords()
    coords["groww_symbol"] = "NSE-M&M"
    record = build_record(_PAYLOAD, issued_symbol="NSE-M%26M", **coords)
    assert record.issued_symbol == "NSE-M%26M"
    assert record.fetch_symbol == "NSE-M%26M"
    assert record.key[2] == "NSE-M&M"  # issued form is NEVER part of the key


# --------------------------------------------------------------------------- #
# verify_record
# --------------------------------------------------------------------------- #
def test_verify_record_match() -> None:
    record = build_record(_PAYLOAD, **_coords())
    result = verify_record(record, copy.deepcopy(_PAYLOAD))
    assert isinstance(result, VerifyResult)
    assert result.matches is True
    assert result.recorded_hash == result.actual_hash == record.payload_hash
    assert result.key == record.key


def test_verify_record_mismatch() -> None:
    record = build_record(_PAYLOAD, **_coords())
    tampered = copy.deepcopy(_PAYLOAD)
    tampered["candles"][1][3] = 0.5  # low changed
    result = verify_record(record, tampered)
    assert result.matches is False
    assert result.recorded_hash != result.actual_hash


def test_verify_record_propagates_malformed_payload() -> None:
    record = build_record(_PAYLOAD, **_coords())
    with pytest.raises(RawIntegrityError):
        verify_record(record, {"candles": object()})
