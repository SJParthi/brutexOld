"""Dedicated suffix-independence property for the near-swing bits 72/73.

DEC-FIB-TEMPLATE-EXPRESS-001 §4.4 obligation 3: bits 72 and 73 of
``bar_bits[N]`` are INVARIANT to arbitrary mutation / truncation / extreme
perturbation of bars ``N+1..`` — the mechanical proof that the
k-bar-confirmed swing anchors (read from ``MarketStructureState`` strictly
POST-update) never consult a bar past the consumption front. Run at BOTH the
Python builder (``build_bar_bits``) and the Rust batch kernel
(``trading_core.stage1a_bar_bits_batch`` over ``collect_batch_inputs``
columns), with an ANTI-VACUOUS tooth: the deterministic feeds below provably
FIRE each bit in the prefix, and the hypothesis corpus asserts a non-zero
fire count.

The general whole-word suites (``test_sweep_suffix_independence.py``,
``test_stage1a_rust_batch_parity.py``) implicitly cover the new bits because
``bar_bits`` carry them; this file pins bits 72/73 EXPLICITLY so a
swing-specific look-ahead regression names itself.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import trading_core
from brutex.data.models import CandleBar
from brutex.engine.stage1a import Stage1AMatrixBuilder, build_bar_bits
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

_BIT_72 = 1 << 72
_BIT_73 = 1 << 73
_SWING_BITS = _BIT_72 | _BIT_73

#: Anti-vacuous corpus tooth — bumped by the hypothesis property whenever a
#: swing bit fires in a kept prefix; asserted non-zero by the final test.
_corpus_fires: list[int] = []


def _minute_bar(minute: int, o: str, h: str, low: str, c: str) -> CandleBar:
    ts = datetime(2024, 1, 3, 3, 45, tzinfo=UTC) + timedelta(minutes=minute)
    return CandleBar(
        timestamp=ts,
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(low),
        close=Decimal(c),
        volume=10,
        open_interest=None,
    )


def _swing_high_feed() -> list[CandleBar]:
    """Peak at bar 3 (high 104) confirms as a swing HIGH at bar 5 (k=2);
    bars 6-7 close back AT the level → bit 72 fires in the prefix."""
    seq = [
        ("100", "100", "99", "100"),
        ("101", "101", "100", "101"),
        ("102", "102", "101", "102"),
        ("103", "104", "102", "103"),
        ("102", "103", "101", "102"),
        ("101", "102", "100", "101"),
        ("103", "104", "103", "104"),
        ("104", "104", "103", "104"),
    ]
    return [_minute_bar(i, *p) for i, p in enumerate(seq)]


def _swing_low_feed() -> list[CandleBar]:
    """The mirrored trough: bar-3 low 96 confirms as a swing LOW at bar 5;
    bars 6-7 close back AT the level → bit 73 fires in the prefix."""
    seq = [
        ("100", "101", "100", "100"),
        ("99", "100", "99", "99"),
        ("98", "99", "98", "98"),
        ("97", "98", "96", "97"),
        ("98", "99", "97", "98"),
        ("99", "100", "98", "99"),
        ("97", "97", "96", "96"),
        ("96", "97", "96", "96"),
    ]
    return [_minute_bar(i, *p) for i, p in enumerate(seq)]


def _rust_batch_bits(bars: list[CandleBar]) -> list[int]:
    """Drive the REAL Rust batch kernel over the prepass columns."""
    inputs = Stage1AMatrixBuilder().collect_batch_inputs(bars)
    result: list[int] = trading_core.stage1a_bar_bits_batch(
        inputs.open_paisa,
        inputs.high_paisa,
        inputs.low_paisa,
        inputs.close_paisa,
        inputs.tod_seconds,
        inputs.contract_reset,
        inputs.day_index,
        inputs.ema20_paisa,
        inputs.ema200_paisa,
        inputs.day_pivot_levels,
        inputs.day_fib_levels,
        inputs.day_has_pivot,
        inputs.day_has_fib,
        inputs.pivot_bps,
        inputs.pdh_pdl_bps,
        inputs.fib_bps,
        inputs.swing_bps,
        inputs.prior_n,
        inputs.anchor_dependent_mask,
        inputs.fib_dependent_mask,
        inputs.applies_to_1day_mask,
        inputs.apply_1day_mask,
    )
    return result


def _extreme_suffix(bars: list[CandleBar], start: int, scale: int) -> list[CandleBar]:
    """Replace bars[start:] with extreme prices (timestamps preserved)."""
    mutated = list(bars)
    for j in range(start, len(bars)):
        price = Decimal(scale)
        mutated[j] = CandleBar(
            timestamp=bars[j].timestamp,
            open=price,
            high=price + 1,
            low=price - 1,
            close=price,
            volume=bars[j].volume,
            open_interest=None,
        )
    return mutated


class TestDeterministicAntiVacuous:
    """The teeth: each bit provably FIRES, and the fired prefix is invariant."""

    def test_bit_72_fires_and_is_suffix_invariant_both_kernels(self) -> None:
        bars = _swing_high_feed()
        py_bits = build_bar_bits(bars)
        assert any(b & _BIT_72 for b in py_bits), "anti-vacuous: bit 72 must fire"
        rs_bits = _rust_batch_bits(bars)
        assert py_bits == rs_bits  # Python builder ≡ Rust batch kernel
        fired_at = max(i for i, b in enumerate(py_bits) if b & _BIT_72)
        # Truncation: the stream ending right after the fire changes nothing.
        assert build_bar_bits(bars[: fired_at + 1]) == py_bits[: fired_at + 1]
        assert _rust_batch_bits(bars[: fired_at + 1]) == rs_bits[: fired_at + 1]

    def test_bit_73_fires_and_is_suffix_invariant_both_kernels(self) -> None:
        bars = _swing_low_feed()
        py_bits = build_bar_bits(bars)
        assert any(b & _BIT_73 for b in py_bits), "anti-vacuous: bit 73 must fire"
        rs_bits = _rust_batch_bits(bars)
        assert py_bits == rs_bits
        fired_at = max(i for i, b in enumerate(py_bits) if b & _BIT_73)
        assert build_bar_bits(bars[: fired_at + 1]) == py_bits[: fired_at + 1]
        assert _rust_batch_bits(bars[: fired_at + 1]) == rs_bits[: fired_at + 1]

    def test_extreme_suffix_mutation_never_moves_a_fired_prefix_bit(self) -> None:
        for bars, bit in ((_swing_high_feed(), _BIT_72), (_swing_low_feed(), _BIT_73)):
            py_bits = build_bar_bits(bars)
            fired_at = max(i for i, b in enumerate(py_bits) if b & bit)
            for scale in (1, 10_000_000):  # crash-to-1 and moonshot suffixes
                mutated = _extreme_suffix(bars, fired_at + 1, scale)
                assert build_bar_bits(mutated)[: fired_at + 1] == py_bits[: fired_at + 1]
                assert _rust_batch_bits(mutated)[: fired_at + 1] == py_bits[: fired_at + 1]


@st.composite
def _swingy_series(draw: st.DrawFn) -> list[CandleBar]:
    """A random walk seeded with the confirmed-peak shape so swings confirm
    (and often fire) inside the prefix, plus free-drawn continuation bars."""
    base = draw(st.integers(min_value=95, max_value=110))
    peak = base + draw(st.integers(min_value=2, max_value=6))
    ramp = [base, base + 1, peak, base + 1, base]  # centre-bar peak, k=2 clean
    tail_len = draw(st.integers(min_value=3, max_value=8))
    # The tail revisits the peak's neighbourhood (±1) so bit 72 fires often.
    tail = [peak + draw(st.integers(min_value=-1, max_value=1)) for _ in range(tail_len)]
    prices = ramp + tail
    bars: list[CandleBar] = []
    for i, p in enumerate(prices):
        s = str(p)
        bars.append(_minute_bar(i, s, str(p + 1), str(p - 1), s))
    return bars


@given(bars=_swingy_series(), data=st.data())
@settings(suppress_health_check=[HealthCheck.too_slow], deadline=None, max_examples=150)
def test_swing_bits_prefix_invariant_under_truncation_and_mutation(
    bars: list[CandleBar], data: st.DataObject
) -> None:
    full = build_bar_bits(bars)
    k = data.draw(st.integers(min_value=0, max_value=len(bars) - 1))
    prefix = full[: k + 1]
    _corpus_fires.extend(1 for b in prefix if b & _SWING_BITS)
    # Truncation form.
    assert [b & _SWING_BITS for b in build_bar_bits(bars[: k + 1])] == [
        b & _SWING_BITS for b in prefix
    ]
    # Extreme-mutation form (both kernels).
    if k + 1 < len(bars):
        scale = data.draw(st.sampled_from([1, 3, 10_000_000]))
        mutated = _extreme_suffix(bars, k + 1, scale)
        assert [b & _SWING_BITS for b in build_bar_bits(mutated)[: k + 1]] == [
            b & _SWING_BITS for b in prefix
        ]
        assert [b & _SWING_BITS for b in _rust_batch_bits(mutated)[: k + 1]] == [
            b & _SWING_BITS for b in prefix
        ]


def test_corpus_actually_fired_the_swing_bits() -> None:
    """Anti-vacuous tooth over the WHOLE hypothesis corpus above (§4.4)."""
    assert sum(_corpus_fires) > 0, "the generated corpus never fired bits 72/73"
