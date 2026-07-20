"""Hypothesis differential: the Rust rate-math kernel vs the CPython oracle.

DEC-RATE-MATH-RUST-001 (slice 9, ADR-0019): unlike the audit-chain float
REPR (a statistical proof), the four token-math expressions are plain IEEE
``+ - * /`` plus ORDERED comparisons — bit-specified, so parity here is
EXACT. Every comparison is on the raw little-endian f64 BYTE pattern
(``struct.pack("<d", …)``), the honest "same f64" predicate (``==`` would
pass a ``-0.0``/``0.0`` swap and fail NaN against itself). The generated
domain is raw 64-bit patterns filtered finite (subnormals and ±0.0 KEPT,
overflow-adjacent magnitudes included) with rps drawn from the production
ladder values and random positive finite floats. If ANY byte diverges the
TRANSCRIPTION is wrong — there is no legitimate divergence class for these
ops (the slice-9 spec §3; the audit escalation clause does not apply here).
"""

from __future__ import annotations

import math
import struct
from typing import Final

import pytest
from hypothesis import given
from hypothesis import strategies as st

fno_math = pytest.importorskip("fno_math")
if not hasattr(fno_math, "bucket_refill"):  # pragma: no cover - stale-wheel guard
    pytest.skip(
        "installed fno_math wheel predates the slice-9 rate-math symbols — rebuild the wheel",
        allow_module_level=True,
    )


def _bits_to_float(bits: int) -> float:
    (value,) = struct.unpack("<d", struct.pack("<Q", bits))
    return value


def _b(*values: float) -> bytes:
    """Little-endian f64 byte pattern of each value — the parity predicate."""
    return struct.pack(f"<{len(values)}d", *values)


#: Any finite f64 via raw bit patterns — subnormals, ±0.0 and
#: overflow-adjacent magnitudes all reachable (unlike the value-biased
#: ``st.floats`` generator, which under-samples the exponent extremes).
_finite_bits = st.integers(0, 2**64 - 1).map(_bits_to_float).filter(math.isfinite)

#: Positive finite rates: the production ladder values (8/5/4/3/1.5/1 —
#: sampled, never asserted as caps: the VALUES stay owned by the Python
#: modules) plus random positive finite bit patterns down to subnormals.
_positive_rps = st.one_of(
    st.sampled_from([8.0, 5.0, 4.0, 3.0, 1.5, 1.0]),
    st.integers(1, 2**63 - 1).map(_bits_to_float).filter(lambda x: math.isfinite(x) and x > 0.0),
)

#: The retry-loop eps domain: the production 1e-9 plus arbitrary positive
#: finite values (the eps VALUE stays Python-owned; the math must be exact
#: for any float handed over).
_eps = st.one_of(st.just(1e-9), _positive_rps)


# ── the verbatim CPython transcriptions (the oracle) ─────────────────────


def _py_refill(tokens: float, burst: float, rps: float, elapsed: float) -> float:
    if elapsed <= 0.0:
        return tokens
    return min(burst, tokens + elapsed * rps)


def _py_consume(tokens: float, rps: float) -> tuple[float, float]:
    if tokens >= 1.0:
        return (tokens - 1.0, 0.0)
    return (tokens, (1.0 - tokens) / rps)


def _py_more_wait(tokens: float, rps: float, eps: float) -> float:
    return max((1.0 - tokens) / rps, eps)


def _py_set_rate_state(new_rps: float) -> tuple[float, float]:
    return (0.0, new_rps / 2.0)


# ── differentials ────────────────────────────────────────────────────────


@given(tokens=_finite_bits, burst=_finite_bits, rps=_positive_rps, elapsed=_finite_bits)
def test_bucket_refill_byte_equality(
    tokens: float, burst: float, rps: float, elapsed: float
) -> None:
    """Refill over arbitrary finite bit patterns — negative/zero elapsed
    (the backwards-clock arm), subnormal drips and overflow-adjacent
    products (the min clamp) all byte-equal."""
    assert _b(fno_math.bucket_refill(tokens, burst, rps, elapsed)) == _b(
        _py_refill(tokens, burst, rps, elapsed)
    )


@given(tokens=_finite_bits, rps=_positive_rps)
def test_bucket_consume_byte_equality(tokens: float, rps: float) -> None:
    assert _b(*fno_math.bucket_consume(tokens, rps)) == _b(*_py_consume(tokens, rps))


@given(tokens=_finite_bits, rps=_positive_rps, eps=_eps)
def test_bucket_more_wait_byte_equality(tokens: float, rps: float, eps: float) -> None:
    assert _b(fno_math.bucket_more_wait(tokens, rps, eps)) == _b(_py_more_wait(tokens, rps, eps))


@given(new_rps=_finite_bits)
def test_bucket_set_rate_state_byte_equality(new_rps: float) -> None:
    """The halving is exact for EVERY finite input (a power-of-two divide),
    including subnormal underflow-to-half shapes."""
    assert _b(*fno_math.bucket_set_rate_state(new_rps)) == _b(*_py_set_rate_state(new_rps))


# ── fixed adversarial corpus (deterministic, every boundary named) ───────

#: (tokens, burst, rps, elapsed) refill vectors: the elapsed<=0 inclusive
#: boundary, the exact-at-clamp tie, signed zeros, subnormal floor, the
#: acquire-loop float-drift shape, and overflow-adjacent products.
_REFILL_CORPUS: Final[tuple[tuple[float, float, float, float], ...]] = (
    (2.5, 5.0, 3.0, 0.0),
    (2.5, 5.0, 3.0, -0.0),
    (2.5, 5.0, 3.0, -1.0),
    (2.5, 5.0, 3.0, 5e-324),
    (2.0, 5.0, 3.0, 1.0),  # sum == burst exactly (the min tie)
    (4.0, 5.0, 3.0, 1.0),  # clamped
    (0.0, 8.0, 1.7976931348623157e308, 1.7976931348623157e308),  # inf product clamps
    (-0.0, 5.0, 3.0, 0.5),
    (0.9999999999999999, 5.0, 1.5, 1e-9),
)


@pytest.mark.parametrize("vector", _REFILL_CORPUS, ids=repr)
def test_refill_corpus(vector: tuple[float, float, float, float]) -> None:
    tokens, burst, rps, elapsed = vector
    assert _b(fno_math.bucket_refill(tokens, burst, rps, elapsed)) == _b(
        _py_refill(tokens, burst, rps, elapsed)
    )


@pytest.mark.parametrize(
    ("tokens", "rps"),
    [
        (1.0, 3.0),  # the >= boundary consumes
        (1.0 - 2.220446049250313e-16, 3.0),  # just below — waits
        (0.9999999999999999, 8.0),
        (0.0, 3.0),
        (-0.0, 3.0),
        (5.0, 1.0),
        (0.5, 5e-324),  # subnormal rate → huge wait, still exact
    ],
    ids=repr,
)
def test_consume_corpus(tokens: float, rps: float) -> None:
    assert _b(*fno_math.bucket_consume(tokens, rps)) == _b(*_py_consume(tokens, rps))


@pytest.mark.parametrize(
    ("tokens", "rps", "eps"),
    [
        (0.25, 3.0, 1e-9),
        (1.0 - 1e-12, 3.0, 1e-9),  # drift shape — eps floor serves
        (0.5, 2.0, 0.25),  # computed wait == eps exactly (the max tie)
        (1.0, 3.0, 1e-9),  # zero wait → eps
        (-0.0, 8.0, 1e-9),
    ],
    ids=repr,
)
def test_more_wait_corpus(tokens: float, rps: float, eps: float) -> None:
    assert _b(fno_math.bucket_more_wait(tokens, rps, eps)) == _b(_py_more_wait(tokens, rps, eps))


@pytest.mark.parametrize(
    "new_rps",
    [8.0, 5.0, 4.0, 3.0, 1.5, 1.0, 5e-324, 1.7976931348623157e308],
    ids=repr,
)
def test_set_rate_state_corpus(new_rps: float) -> None:
    served = fno_math.bucket_set_rate_state(new_rps)
    assert _b(*served) == _b(*_py_set_rate_state(new_rps))
    # The drained-token cell is POSITIVE zero, bit-exactly.
    assert _b(served[0]) == _b(0.0)


def test_python_min_max_order_semantics_survive_nan_inputs() -> None:
    """CPython ``min``/``max`` are POSITIONAL under NaN (first argument wins
    when the comparison is False) — the transcription must not use IEEE
    minNum/maxNum. NaN cannot reach a validated production bucket; this
    pins the transcription law itself via the refill clamp and the wait
    floor (byte-compare on the non-NaN results, identity on the NaN ones).
    """
    nan = float("nan")
    # burst=NaN: min(NaN, sum) → NaN in CPython (first arg — comparison False).
    rust = fno_math.bucket_refill(1.0, nan, 3.0, 1.0)
    oracle = _py_refill(1.0, nan, 3.0, 1.0)
    assert math.isnan(rust) and math.isnan(oracle)
    # tokens=NaN with positive elapsed: sum is NaN → min(burst, NaN) → burst.
    assert _b(fno_math.bucket_refill(nan, 5.0, 3.0, 1.0)) == _b(_py_refill(nan, 5.0, 3.0, 1.0))
    assert _b(_py_refill(nan, 5.0, 3.0, 1.0)) == _b(5.0)
    # eps=NaN: max(wait, NaN) → wait (first arg) in CPython.
    assert _b(fno_math.bucket_more_wait(0.25, 3.0, nan)) == _b(_py_more_wait(0.25, 3.0, nan))
    assert _b(_py_more_wait(0.25, 3.0, nan)) == _b((1.0 - 0.25) / 3.0)
