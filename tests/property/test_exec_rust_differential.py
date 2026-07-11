"""Hypothesis differential: the Rust exec kernels == the Python oracles EXACTLY.

DEC-EXEC-RUST-CORE-001 (MIGRATION.md #1724). Every property drives the
flag-ON seam (:mod:`brutex.exec_rust`) and the verbatim Python body over the
SAME generated inputs and requires INTEGER equality — a single paisa of
drift fails (the coordinator's money-parity law; the #1510 IEEE-divergence
lesson is why the money primitives fuzz the FULL signed domain, ties and
extremes included, not just production-plausible values).

Domains (spec §5): opens over the full paisa envelope incl. the tick-floor
boundaries; lots/n_lots wide; rates incl. 0; both sides; Groww-turnover /
Zerodha-flat / cost-free arms; stale/tick verdict edges; MAE/MFE paths.
Primitive-level round_to_paisa/rupee run over the full signed i64 (and the
in-envelope i128 product domain for the paisa law).

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
    _round_to_paisa,  # pyright: ignore[reportPrivateUsage] — the oracle arm
    _round_to_rupee,  # pyright: ignore[reportPrivateUsage] — the oracle arm
)
from brutex.costs.futures_costs import (
    _futures_charge_stack_py,  # pyright: ignore[reportPrivateUsage] — the oracle arm
    apply_futures_fill,
)
from hypothesis import given
from hypothesis import strategies as st

_I64_MAX = 2**63 - 1
_I64_MIN = -(2**63)

#: Opens: the ≥-one-tick production envelope, biased onto the tick-floor
#: boundaries {5, 25, 26} by the shrinker; bounded so notional × rate stays
#: inside i64 for the widest lot domain below (the seam envelope-guards the
#: rest to the Python arm — that arm is pinned in the seam unit suite).
_OPENS = st.integers(min_value=5, max_value=10_000_000)
_LOTS = st.integers(min_value=1, max_value=1_000)
_N_LOTS = st.integers(min_value=1, max_value=100)
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
    """The kernel paisa rounding == ``_round_to_paisa`` over the whole
    i64-result i128 domain (ties, negatives, extremes included)."""
    rust = exec_rust._rust  # pyright: ignore[reportPrivateUsage]
    assert rust is not None
    got = rust.exec_round_to_paisa(x)
    assert got == _round_to_paisa(x)


@given(p=st.integers(min_value=_I64_MIN, max_value=_I64_MAX))
def test_round_to_rupee_differential_full_domain(p: int) -> None:
    """The kernel rupee rounding == ``_round_to_rupee`` over ALL i64."""
    rust = exec_rust._rust  # pyright: ignore[reportPrivateUsage]
    assert rust is not None
    got = rust.exec_round_to_rupee(p)
    assert got == _round_to_rupee(p)


# ── fills ───────────────────────────────────────────────────────────────


@given(entry=_OPENS, exit_=_OPENS, is_long=st.booleans())
def test_fill_slippage_differential(entry: int, exit_: int, is_long: bool) -> None:
    side: Literal["long", "short"] = "long" if is_long else "short"
    got = exec_rust.fill_slippage(entry, exit_, is_long=is_long)
    assert got is not None  # engaged
    assert got == _apply_slippage(entry, exit_, side)


@given(entry=_OPENS, exit_=_OPENS, is_long=st.booleans())
def test_fill_futures_differential(entry: int, exit_: int, is_long: bool) -> None:
    side: Literal["long", "short"] = "long" if is_long else "short"
    got = exec_rust.fill_futures(entry, exit_, is_long=is_long)
    assert got is not None  # engaged
    assert got == apply_futures_fill(entry, exit_, side=side)


# ── option charge stack ─────────────────────────────────────────────────


@given(
    entry=_OPENS,
    exit_=_OPENS,
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
    entry: int,
    exit_: int,
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
    got = exec_rust.charge_stack_option(
        entry,
        exit_,
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
        entry,
        exit_,
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
    entry=_OPENS,
    exit_=_OPENS,
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
    entry: int,
    exit_: int,
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
    got = exec_rust.charge_stack_futures(
        entry,
        exit_,
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
        entry,
        exit_,
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
    entry=_OPENS,
    exit_=_OPENS,
    entry_volume=_VOLUMES,
    exit_volume=_VOLUMES,
    is_long=st.booleans(),
    reject_stale=st.booleans(),
)
def test_options_materialize_differential(
    entry: int,
    exit_: int,
    entry_volume: int,
    exit_volume: int,
    is_long: bool,
    reject_stale: bool,
) -> None:
    """The kernel verdict tail == the pinned Python gate order (stale →
    tick floor → stack), decided here with the SAME laws the executor
    Python tail applies (`executor.py` gates 4–5)."""
    got = exec_rust.materialize_option_tail(
        entry,
        exit_,
        entry_volume=entry_volume,
        exit_volume=exit_volume,
        is_long=is_long,
        reject_stale=reject_stale,
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
    if reject_stale and (entry_volume == 0 or exit_volume == 0):
        assert got == ("stale_bar", None)
    elif entry <= 25 or exit_ < 5:
        assert got == ("tick_floor", None)
    else:
        kind, stack = got
        assert kind == "trade"
        expected = _charge_stack_py(
            entry,
            exit_,
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
    entry=_OPENS,
    exit_=_OPENS,
    entry_volume=_VOLUMES,
    exit_volume=_VOLUMES,
    is_long=st.booleans(),
    reject_stale=st.booleans(),
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
    entry: int,
    exit_: int,
    entry_volume: int,
    exit_volume: int,
    is_long: bool,
    reject_stale: bool,
    cost_free: bool,
    path: list[tuple[int, int]],
) -> None:
    """The kernel futures tail == the Python stack + the executor
    ``_excursions_paisa`` law (per-unit running max floored at 0 against
    the entry fill, then × n_shares; basis = buy fill long / sell fill
    short — executor.py:590-613/731)."""
    highs = [max(h, lo) for h, lo in path]
    lows = [min(h, lo) for h, lo in path]
    got = exec_rust.materialize_futures_tail(
        entry,
        exit_,
        entry_volume=entry_volume,
        exit_volume=exit_volume,
        path_highs_paisa=highs,
        path_lows_paisa=lows,
        is_long=is_long,
        reject_stale=reject_stale,
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
    if reject_stale and (entry_volume == 0 or exit_volume == 0):
        assert got == ("stale_bar", None, 0, 0)
        return
    kind, stack, mae, mfe = got
    assert kind == "trade"
    expected = _futures_charge_stack_py(
        entry,
        exit_,
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
