"""Hypothesis differential: the Rust exec kernels == the Python oracles EXACTLY.

DEC-EXEC-RUST-CORE-001 (MIGRATION.md #1978). Every property drives the
flag-ON seam (:mod:`brutex.exec_rust`) and the verbatim Python body over the
SAME generated inputs and requires INTEGER equality — a single paisa of
drift fails (the coordinator's money-parity law; the #1510 IEEE-divergence
lesson is why the money primitives fuzz the FULL signed domain, boundaries
and extremes included, not just production-plausible values).

Domains (spec §5, re-expressed on the DEC-FILL-WORST-CASE-ONLY-001 law):
fill BARS as (high, low) extreme pairs over the full paisa envelope incl.
the tick-floor boundaries; lots/n_lots wide; rates incl. 0; both sides;
Groww-turnover / Zerodha-flat / cost-free arms; the UNCONDITIONAL stale
verdict edge; tick-floor OPEN gates; MAE/MFE paths. Primitive-level
round_to_paisa/rupee run over the full signed domain whose ADVERSE-CEILING
result fits i64 (one past the largest in-range rupee multiple overflows —
the kernel refuses; pinned in the seam unit suite).

Runs in the normal suite at the house ``ci`` profile example counts
(tests/conftest.py registers + loads it via ``HYPOTHESIS_PROFILE``).
The engagement is ASSERTED per example (``is not None``) — a silent
fallback fails, it never vacuously passes.
"""

from __future__ import annotations

from typing import Literal

import pytest
from brutex import exec_rust
from brutex.costs.calculator import (
    _apply_slippage,  # pyright: ignore[reportPrivateUsage] — the oracle arm
    _charge_stack_py,  # pyright: ignore[reportPrivateUsage] — the oracle arm
    _round_levy_paisa,  # pyright: ignore[reportPrivateUsage] — the oracle arm
    _round_levy_rupee,  # pyright: ignore[reportPrivateUsage] — the oracle arm
)
from brutex.costs.futures_costs import (
    _futures_charge_stack_py,  # pyright: ignore[reportPrivateUsage] — the oracle arm
    apply_futures_fill,
)
from hypothesis import given
from hypothesis import strategies as st

_I64_MAX = 2**63 - 1
_I64_MIN = -(2**63)
#: The largest multiple of 100 inside i64 — the ADVERSE-CEILING rupee law's
#: in-envelope result ceiling (any input above it CEILs past i64::MAX and
#: the kernel refuses; the refusal arm is pinned in the seam unit suite).
_I64_MAX_RUPEE_MULTIPLE = (_I64_MAX // 100) * 100

#: Bar-extreme anchors: the ≥-one-tick production envelope, biased onto the
#: tick-floor boundaries {5, 25, 26} by the shrinker; bounded so notional ×
#: rate stays inside i64 for the widest lot domain below (the seam
#: envelope-guards the rest to the Python arm — that arm is pinned in the
#: seam unit suite).
_ANCHORS = st.integers(min_value=5, max_value=10_000_000)
#: A fill bar = an unordered anchor pair → (high, low) = (max, min).
_BARS = st.tuples(_ANCHORS, _ANCHORS).map(lambda t: (max(t), min(t)))
_LOTS = st.integers(min_value=1, max_value=1_000)
#: The spec §5 domain (1..10_000; review round 1 H1-NIT-2 raised it from
#: 100): worst case n_shares = 10^7, notional = 10^14 paisa, notional×rate
#: = 2×10^20 (i128 in-kernel) → every result stays inside i64, so the
#: per-example engaged-asserts still hold at the upper envelope.
_N_LOTS = st.integers(min_value=1, max_value=10_000)
#: Rates INCLUDE 0 (the cost-free arm passes zeros) up to the GST magnitude.
_RATES = st.integers(min_value=0, max_value=2_000_000)
_VOLUMES = st.integers(min_value=0, max_value=3)  # 0 hits the stale edge often


@pytest.fixture(autouse=True)
def _rust_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the ON arm and REQUIRE the kernel — never a silent skip."""
    assert exec_rust._rust is not None, (  # pyright: ignore[reportPrivateUsage]
        "fno_math wheel with the exec_ kernels is required (engaged-assert)"
    )
    monkeypatch.setenv(exec_rust.ENV_FLAG, "1")


# ── money primitives: FULL signed domain ────────────────────────────────


@given(x=st.integers(min_value=_I64_MIN * 10_000_000, max_value=_I64_MAX * 10_000_000))
def test_round_to_paisa_differential_full_domain(x: int) -> None:
    """The kernel paisa CEILING == ``_round_levy_paisa`` over the whole
    i64-result i128 domain (boundaries, negatives, extremes included)."""
    rust = exec_rust._rust  # pyright: ignore[reportPrivateUsage]
    assert rust is not None
    got = rust.exec_round_to_paisa(x)
    assert got == _round_levy_paisa(x)


@given(p=st.integers(min_value=_I64_MIN, max_value=_I64_MAX_RUPEE_MULTIPLE))
def test_round_to_rupee_differential_full_domain(p: int) -> None:
    """The kernel rupee CEILING == ``_round_levy_rupee`` over every i64
    whose ceiled result still fits i64 (the last multiple of 100)."""
    rust = exec_rust._rust  # pyright: ignore[reportPrivateUsage]
    assert rust is not None
    got = rust.exec_round_to_rupee(p)
    assert got == _round_levy_rupee(p)


def test_round_to_rupee_past_the_ceiling_refuses() -> None:
    """One past the last in-range multiple: the Python oracle (unbounded
    ints) ceils PAST i64::MAX, so the kernel REFUSES with ``ValueError`` —
    the honest envelope edge, never a wrapped value."""
    rust = exec_rust._rust  # pyright: ignore[reportPrivateUsage]
    assert rust is not None
    assert _round_levy_rupee(_I64_MAX_RUPEE_MULTIPLE + 1) > _I64_MAX
    with pytest.raises(ValueError, match="round_to_rupee"):
        rust.exec_round_to_rupee(_I64_MAX_RUPEE_MULTIPLE + 1)


# ── fills (worst-case bar-extreme anchors) ──────────────────────────────


@given(entry_bar=_BARS, exit_bar=_BARS, is_long=st.booleans())
def test_fill_slippage_differential(
    entry_bar: tuple[int, int], exit_bar: tuple[int, int], is_long: bool
) -> None:
    side: Literal["long", "short"] = "long" if is_long else "short"
    (eh, el), (xh, xl) = entry_bar, exit_bar
    got = exec_rust.fill_slippage(eh, el, xh, xl, is_long=is_long)
    assert got is not None  # engaged
    assert got == _apply_slippage(
        side,
        entry_high_paisa=eh,
        entry_low_paisa=el,
        exit_high_paisa=xh,
        exit_low_paisa=xl,
    )


@given(entry_bar=_BARS, exit_bar=_BARS, is_long=st.booleans())
def test_fill_futures_differential(
    entry_bar: tuple[int, int], exit_bar: tuple[int, int], is_long: bool
) -> None:
    side: Literal["long", "short"] = "long" if is_long else "short"
    (eh, el), (xh, xl) = entry_bar, exit_bar
    got = exec_rust.fill_futures(eh, el, xh, xl, is_long=is_long)
    assert got is not None  # engaged
    assert got == apply_futures_fill(
        side=side,
        entry_high_paisa=eh,
        entry_low_paisa=el,
        exit_high_paisa=xh,
        exit_low_paisa=xl,
    )


# ── option charge stack ─────────────────────────────────────────────────


@given(
    entry_bar=_BARS,
    exit_bar=_BARS,
    is_long=st.booleans(),
    lot=_LOTS,
    n_lots=_N_LOTS,
    brokerage=st.integers(min_value=0, max_value=100_000),
    stt=_RATES,
    etc=_RATES,
    sebi=_RATES,
    ipft=_RATES,
    stamp=_RATES,
    gst=_RATES,
)
def test_option_charge_stack_differential(
    entry_bar: tuple[int, int],
    exit_bar: tuple[int, int],
    is_long: bool,
    lot: int,
    n_lots: int,
    brokerage: int,
    stt: int,
    etc: int,
    sebi: int,
    ipft: int,
    stamp: int,
    gst: int,
) -> None:
    (eh, el), (xh, xl) = entry_bar, exit_bar
    got = exec_rust.charge_stack_option(
        eh,
        el,
        xh,
        xl,
        is_long=is_long,
        lot_size=lot,
        n_lots=n_lots,
        brokerage_round_trip_paisa=brokerage,
        stt_rate=stt,
        etc_rate=etc,
        sebi_rate=sebi,
        ipft_rate=ipft,
        stamp_rate=stamp,
        gst_rate=gst,
    )
    assert got is not None  # engaged (inputs are in-envelope by construction)
    expected = _charge_stack_py(
        eh,
        el,
        xh,
        xl,
        is_long=is_long,
        lot_size=lot,
        n_lots=n_lots,
        brokerage_round_trip_paisa=brokerage,
        stt_rate=stt,
        etc_rate=etc,
        sebi_rate=sebi,
        ipft_rate=ipft,
        stamp_rate=stamp,
        gst_rate=gst,
    )
    assert tuple(got) == tuple(expected)
    # The algebraic law (also proptested cargo-side): net == gross − total.
    assert got.net_pnl_paisa == got.gross_pnl_paisa - got.total_charges_paisa


# ── futures charge stack (GST-split axis is display-only; not an input) ─


@given(
    entry_bar=_BARS,
    exit_bar=_BARS,
    is_long=st.booleans(),
    lot=_LOTS,
    n_lots=_N_LOTS,
    cost_free=st.booleans(),
    flat=st.integers(min_value=0, max_value=100_000),
    turnover=st.one_of(st.none(), st.integers(min_value=0, max_value=100_000)),
    stt=_RATES,
    etc=_RATES,
    sebi=_RATES,
    ipft=_RATES,
    stamp=_RATES,
    gst=_RATES,
)
def test_futures_charge_stack_differential(
    entry_bar: tuple[int, int],
    exit_bar: tuple[int, int],
    is_long: bool,
    lot: int,
    n_lots: int,
    cost_free: bool,
    flat: int,
    turnover: int | None,
    stt: int,
    etc: int,
    sebi: int,
    ipft: int,
    stamp: int,
    gst: int,
) -> None:
    (eh, el), (xh, xl) = entry_bar, exit_bar
    got = exec_rust.charge_stack_futures(
        eh,
        el,
        xh,
        xl,
        is_long=is_long,
        lot_size=lot,
        n_lots=n_lots,
        cost_free=cost_free,
        brokerage_flat_paisa=flat,
        brokerage_turnover_bps_x100=turnover,
        stt_rate=stt,
        etc_rate=etc,
        sebi_rate=sebi,
        ipft_rate=ipft,
        stamp_rate=stamp,
        gst_rate=gst,
    )
    assert got is not None  # engaged
    expected = _futures_charge_stack_py(
        eh,
        el,
        xh,
        xl,
        is_long=is_long,
        lot_size=lot,
        n_lots=n_lots,
        cost_free=cost_free,
        brokerage_flat_paisa=flat,
        brokerage_turnover_bps_x100=turnover,
        stt_rate=stt,
        etc_rate=etc,
        sebi_rate=sebi,
        ipft_rate=ipft,
        stamp_rate=stamp,
        gst_rate=gst,
    )
    assert tuple(got) == tuple(expected)
    if cost_free:
        assert got.total_charges_paisa == 0
        assert got.net_pnl_paisa == got.gross_pnl_paisa


# ── materialize tails (verdict edges + MAE/MFE paths) ───────────────────


@given(
    entry_open=_ANCHORS,
    exit_open=_ANCHORS,
    entry_bar=_BARS,
    exit_bar=_BARS,
    entry_volume=_VOLUMES,
    exit_volume=_VOLUMES,
    is_long=st.booleans(),
)
def test_options_materialize_differential(
    entry_open: int,
    exit_open: int,
    entry_bar: tuple[int, int],
    exit_bar: tuple[int, int],
    entry_volume: int,
    exit_volume: int,
    is_long: bool,
) -> None:
    """The kernel verdict tail == the pinned Python gate order (stale
    UNCONDITIONAL → tick floor on the OPENS → stack on the extremes),
    decided here with the SAME laws the executor Python tail applies
    (`executor.py` gates 4–5 under DEC-FILL-WORST-CASE-ONLY-001)."""
    (eh, el), (xh, xl) = entry_bar, exit_bar
    got = exec_rust.materialize_option_tail(
        entry_open,
        exit_open,
        eh,
        el,
        xh,
        xl,
        entry_volume=entry_volume,
        exit_volume=exit_volume,
        is_long=is_long,
        lot_size=65,
        n_lots=1,
        brokerage_round_trip_paisa=4_000,
        stt_rate=10_000,
        etc_rate=3_503,
        sebi_rate=10,
        ipft_rate=50,
        stamp_rate=300,
        gst_rate=1_800_000,
    )
    assert got is not None  # engaged
    if entry_volume == 0 or exit_volume == 0:
        assert got == ("stale_bar", None)
    elif entry_open <= 25 or exit_open < 5:
        assert got == ("tick_floor", None)
    else:
        kind, stack = got
        assert kind == "trade"
        expected = _charge_stack_py(
            eh,
            el,
            xh,
            xl,
            is_long=is_long,
            lot_size=65,
            n_lots=1,
            brokerage_round_trip_paisa=4_000,
            stt_rate=10_000,
            etc_rate=3_503,
            sebi_rate=10,
            ipft_rate=50,
            stamp_rate=300,
            gst_rate=1_800_000,
        )
        assert stack == expected


@given(
    entry_bar=_BARS,
    exit_bar=_BARS,
    entry_volume=_VOLUMES,
    exit_volume=_VOLUMES,
    is_long=st.booleans(),
    cost_free=st.booleans(),
    path=st.lists(
        st.tuples(
            st.integers(min_value=5, max_value=10_000_000),
            st.integers(min_value=5, max_value=10_000_000),
        ),
        max_size=8,
    ),
)
def test_futures_materialize_differential(
    entry_bar: tuple[int, int],
    exit_bar: tuple[int, int],
    entry_volume: int,
    exit_volume: int,
    is_long: bool,
    cost_free: bool,
    path: list[tuple[int, int]],
) -> None:
    """The kernel futures tail == the Python stack + the executor
    ``_excursions_paisa`` law (per-unit running max floored at 0 against
    the entry fill, then × n_shares; basis = buy fill long / sell fill
    short); the stale gate is UNCONDITIONAL."""
    (eh, el), (xh, xl) = entry_bar, exit_bar
    highs = [max(h, lo) for h, lo in path]
    lows = [min(h, lo) for h, lo in path]
    got = exec_rust.materialize_futures_tail(
        eh,
        el,
        xh,
        xl,
        entry_volume=entry_volume,
        exit_volume=exit_volume,
        path_highs_paisa=highs,
        path_lows_paisa=lows,
        is_long=is_long,
        lot_size=75,
        n_lots=2,
        cost_free=cost_free,
        brokerage_flat_paisa=2_000,
        brokerage_turnover_bps_x100=3_000,
        stt_rate=0 if cost_free else 2_000,
        etc_rate=0 if cost_free else 173,
        sebi_rate=0 if cost_free else 10,
        ipft_rate=0 if cost_free else 10,
        stamp_rate=0 if cost_free else 200,
        gst_rate=0 if cost_free else 1_800_000,
    )
    assert got is not None  # engaged
    if entry_volume == 0 or exit_volume == 0:
        assert got == ("stale_bar", None, 0, 0)
        return
    kind, stack, mae, mfe = got
    assert kind == "trade"
    expected = _futures_charge_stack_py(
        eh,
        el,
        xh,
        xl,
        is_long=is_long,
        lot_size=75,
        n_lots=2,
        cost_free=cost_free,
        brokerage_flat_paisa=2_000,
        brokerage_turnover_bps_x100=3_000,
        stt_rate=0 if cost_free else 2_000,
        etc_rate=0 if cost_free else 173,
        sebi_rate=0 if cost_free else 10,
        ipft_rate=0 if cost_free else 10,
        stamp_rate=0 if cost_free else 200,
        gst_rate=0 if cost_free else 1_800_000,
    )
    assert stack == expected
    basis = expected.buy_fill_paisa if is_long else expected.sell_fill_paisa
    mae_u = 0
    mfe_u = 0
    for high, low in zip(highs, lows, strict=True):
        fav = (high - basis) if is_long else (basis - low)
        adv = (basis - low) if is_long else (high - basis)
        mfe_u = max(mfe_u, fav)
        mae_u = max(mae_u, adv)
    assert (mae, mfe) == (mae_u * expected.n_shares, mfe_u * expected.n_shares)
    # MAE/MFE are ≥ 0 by the floored-at-0 law.
    assert mae >= 0
    assert mfe >= 0
