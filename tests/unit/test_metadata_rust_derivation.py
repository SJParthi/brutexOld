"""Cross-language oracle for the shared Rust eval-path metadata derivation.

``DEC-NIFTY-ROLL-METADATA-001`` moved the per-bar metadata DERIVATION
(``tod_seconds`` / weekday / minute-of-session / is-expiry-day) and the
near-month roll DECISION into the shared Rust ``trading-core`` crate
(``rust/trading-core/src/indicators/metadata.rs``), exposed via PyO3 as
``trading_core.derive_bar_metadata`` / ``trading_core.weekday_from_ordinal`` /
``trading_core.minute_of_session`` / ``trading_core.resolve_near_month``.

The Rust core is the SINGLE source of truth shared with TickVault (native), so
the backtested signal equals the live signal by construction. This test pins the
contract by proving the Rust derivation is BIT-IDENTICAL to an INDEPENDENT pure
Python (stdlib) reference over a broad date/time domain — the "bit-identity vs
the Python twin" gate. The reference here is deliberately recomputed from
:mod:`datetime` (NOT a call back into the shim) so it is a true independent
oracle, not a tautology.
"""

from __future__ import annotations

from datetime import date, time

import trading_core
from hypothesis import given
from hypothesis import strategies as st

# 09:15 IST as seconds-since-midnight — the regular session open.
_OPEN_0915 = 9 * 3600 + 15 * 60


def _py_weekday(d: date) -> int:
    """Independent stdlib reference for the Rust ``weekday_from_ordinal``."""
    return d.weekday()


def _py_seconds_of_day(t: time) -> int:
    """Independent stdlib reference for a wall-clock seconds-of-day."""
    return t.hour * 3600 + t.minute * 60 + t.second


def _py_minute_of_session(tod_seconds: int, open_seconds: int) -> int:
    """Independent reference: Python floor-division (negative for pre-open)."""
    return (tod_seconds - open_seconds) // 60


def test_weekday_matches_python_for_known_dates() -> None:
    # A handful of anchored dates spanning the week (Mon..Sun) — no DST in IST.
    for d in (
        date(2026, 6, 29),  # Monday
        date(2026, 6, 30),  # Tuesday
        date(2023, 1, 25),  # Wednesday (the Jan-2023 holiday-shifted expiry)
        date(2025, 6, 26),  # Thursday (a NIFTY monthly expiry)
        date(2024, 11, 1),  # Friday (a Muhurat day)
        date(2025, 2, 1),  # Saturday (a special weekend session)
        date(2026, 2, 1),  # Sunday (a special weekend session)
    ):
        assert trading_core.weekday_from_ordinal(d.toordinal()) == _py_weekday(d)


@given(st.dates(min_value=date(2020, 1, 1), max_value=date(2030, 12, 31)))
def test_weekday_bit_identity_with_python(d: date) -> None:
    assert trading_core.weekday_from_ordinal(d.toordinal()) == _py_weekday(d)


@given(
    st.integers(min_value=0, max_value=86_399),
    st.integers(min_value=0, max_value=86_399),
)
def test_minute_of_session_bit_identity_with_python(tod: int, open_s: int) -> None:
    assert trading_core.minute_of_session(tod, open_s) == _py_minute_of_session(tod, open_s)


@given(
    st.dates(min_value=date(2020, 1, 1), max_value=date(2030, 12, 31)),
    st.times(),
    st.dates(min_value=date(2020, 1, 1), max_value=date(2030, 12, 31)),
)
def test_derive_bar_metadata_bit_identity_with_python(
    trade_date: date, t: time, expiry: date
) -> None:
    # The Rust derive_bar_metadata must equal the independent stdlib computation
    # for every field, over a broad date × time × expiry domain.
    tod = _py_seconds_of_day(t)
    rust_tod, rust_weekday, rust_minute, rust_is_expiry = trading_core.derive_bar_metadata(
        trade_date.toordinal(),
        t.hour,
        t.minute,
        t.second,
        _OPEN_0915,
        expiry.toordinal(),
    )
    assert rust_tod == tod
    assert rust_weekday == _py_weekday(trade_date)
    assert rust_minute == _py_minute_of_session(tod, _OPEN_0915)
    assert bool(rust_is_expiry) is (trade_date == expiry)


def test_resolve_near_month_decision_matches_reference() -> None:
    # The at-or-after near-month DECISION: candidate iff >= entry, else next.
    # Independent reference (not a call back into the shim).
    cases = [
        (100, 110, 140, 110),  # candidate after entry → candidate
        (110, 110, 140, 110),  # ON expiry day → expiring contract (>= holds)
        (112, 110, 140, 140),  # holiday-shift gap → roll to next month
    ]
    for entry, cand, nxt, want in cases:
        assert trading_core.resolve_near_month(entry, cand, nxt) == want


@given(
    st.integers(min_value=0, max_value=1_000_000),
    st.integers(min_value=0, max_value=1_000_000),
    st.integers(min_value=0, max_value=1_000_000),
)
def test_resolve_near_month_bit_identity_with_reference(entry: int, cand: int, nxt: int) -> None:
    want = cand if cand >= entry else nxt
    assert trading_core.resolve_near_month(entry, cand, nxt) == want
