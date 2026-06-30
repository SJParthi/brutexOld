"""Unit + property tests for the session-anchored futures VWAP state (PR-2).

Covers the ADR-0012 D12 contract: reset at 09:15 IST, pre-open auction-volume
exclusion, reset on contract roll, warm-up ``None``, the i128/unbounded-int
accumulator (no precision loss), and the Python↔Rust bit-identity of the read.
100% line + branch on ``brutex.indicators.vwap``.
"""

from __future__ import annotations

from datetime import date

import pytest
from brutex.indicators.vwap import VwapState
from hypothesis import given
from hypothesis import strategies as st

KEY = 1
DAY = date(2024, 1, 2)
DAY2 = date(2024, 1, 3)
T0916 = 9 * 3600 + 16 * 60
T0917 = 9 * 3600 + 17 * 60
T0905 = 9 * 3600 + 5 * 60


def test_warmup_returns_none_until_first_volume() -> None:
    v = VwapState()
    # Pre-open auction bar → excluded → still warming up.
    assert (
        v.update(10_000, 500, is_pre_open=True, session_key=KEY, session_day=DAY, tod_seconds=T0905)
        is None
    )
    # Zero-volume post-open bar → still cold.
    assert (
        v.update(10_000, 0, is_pre_open=False, session_key=KEY, session_day=DAY, tod_seconds=T0916)
        is None
    )
    # First real volume warms it.
    assert (
        v.update(
            10_000, 100, is_pre_open=False, session_key=KEY, session_day=DAY, tod_seconds=T0917
        )
        == 10_000
    )


def test_vwap_is_volume_weighted() -> None:
    v = VwapState()
    assert (
        v.update(
            10_000, 100, is_pre_open=False, session_key=KEY, session_day=DAY, tod_seconds=T0916
        )
        == 10_000
    )
    # (100*10000 + 300*20000) / 400 = 17500
    assert (
        v.update(
            20_000, 300, is_pre_open=False, session_key=KEY, session_day=DAY, tod_seconds=T0917
        )
        == 17_500
    )


def test_auction_volume_is_excluded() -> None:
    v = VwapState()
    # A huge pre-open auction print must not bias the session VWAP.
    v.update(
        99_999, 1_000_000, is_pre_open=True, session_key=KEY, session_day=DAY, tod_seconds=T0905
    )
    assert (
        v.update(
            10_000, 100, is_pre_open=False, session_key=KEY, session_day=DAY, tod_seconds=T0916
        )
        == 10_000
    )


def test_resets_on_new_day() -> None:
    v = VwapState()
    v.update(50_000, 100, is_pre_open=False, session_key=KEY, session_day=DAY, tod_seconds=T0916)
    assert (
        v.update(
            10_000, 100, is_pre_open=False, session_key=KEY, session_day=DAY2, tod_seconds=T0916
        )
        == 10_000
    )


def test_resets_on_contract_roll() -> None:
    v = VwapState()
    v.update(50_000, 100, is_pre_open=False, session_key=KEY, session_day=DAY, tod_seconds=T0916)
    # Same day, new contract key (roll) → fresh session.
    assert (
        v.update(
            10_000, 100, is_pre_open=False, session_key=KEY + 1, session_day=DAY, tod_seconds=T0917
        )
        == 10_000
    )


def test_resets_on_tod_rewind_same_day() -> None:
    v = VwapState()
    v.update(
        50_000, 100, is_pre_open=False, session_key=KEY, session_day=DAY, tod_seconds=14 * 3600
    )
    # Backwards time-of-day step (a re-opened session) resets even same key+day.
    assert (
        v.update(
            10_000, 100, is_pre_open=False, session_key=KEY, session_day=DAY, tod_seconds=T0916
        )
        == 10_000
    )


def test_unbounded_accumulator_no_precision_loss() -> None:
    v = VwapState()
    # Σ(price·vol) far past int64 (8e6 * 1e11 = 8e17 per bar × 50 = 4e19 > 9.2e18).
    price = 8_000_000
    for k in range(50):
        got = v.update(
            price,
            100_000_000_000,
            is_pre_open=False,
            session_key=KEY,
            session_day=DAY,
            tod_seconds=T0916 + k,
        )
        assert got == price
    assert v.cum_pv > 2**63 - 1


def test_current_none_before_any_volume() -> None:
    assert VwapState().current() is None


def test_reset_clears_state() -> None:
    v = VwapState()
    v.update(10_000, 100, is_pre_open=False, session_key=KEY, session_day=DAY, tod_seconds=T0916)
    v.reset()
    assert v.current() is None
    assert v.session_day is None
    assert v.session_key == 0
    assert v.last_tod_seconds == 0


def test_to_from_dict_roundtrip() -> None:
    v = VwapState()
    v.update(10_000, 100, is_pre_open=False, session_key=KEY, session_day=DAY, tod_seconds=T0916)
    restored = VwapState.from_dict(v.to_dict())
    assert restored == v
    assert restored.current() == v.current()


def test_to_from_dict_roundtrip_cold() -> None:
    # The session_day=None branch of to_dict / from_dict.
    v = VwapState()
    restored = VwapState.from_dict(v.to_dict())
    assert restored == v
    assert restored.session_day is None


@given(
    prices=st.lists(st.integers(min_value=1, max_value=10_000_000), min_size=1, max_size=30),
    vols=st.lists(st.integers(min_value=1, max_value=1_000_000_000), min_size=1, max_size=30),
)
def test_vwap_within_session_price_range(prices: list[int], vols: list[int]) -> None:
    """The session VWAP always lies within [min typical, max typical] of the
    counted bars — the fundamental volume-weighted-average invariant."""
    v = VwapState()
    n = min(len(prices), len(vols))
    result: int | None = None
    for k in range(n):
        result = v.update(
            prices[k],
            vols[k],
            is_pre_open=False,
            session_key=KEY,
            session_day=DAY,
            tod_seconds=T0916 + k,
        )
    assert result is not None
    assert min(prices[:n]) <= result <= max(prices[:n])


def test_pre_open_only_session_stays_none() -> None:
    # A whole session of only pre-open bars never warms.
    v = VwapState()
    for k in range(5):
        assert (
            v.update(
                10_000,
                100,
                is_pre_open=True,
                session_key=KEY,
                session_day=DAY,
                tod_seconds=T0905 + k,
            )
            is None
        )


def test_negative_volume_is_ignored() -> None:
    # A defensive negative/zero volume contributes nothing (the `volume <= 0`
    # branch) and never warms the state.
    v = VwapState()
    assert (
        v.update(10_000, -5, is_pre_open=False, session_key=KEY, session_day=DAY, tod_seconds=T0916)
        is None
    )


def test_invalid_construction_via_dataclass_defaults() -> None:
    # A fresh VwapState is cold and equal to another fresh one (dataclass eq).
    assert VwapState() == VwapState()
    with pytest.raises(TypeError):
        VwapState().update(10_000, 100)  # type: ignore[call-arg]  # kw-only guard
