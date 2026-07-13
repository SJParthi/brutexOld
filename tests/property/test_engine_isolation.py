"""Hypothesis property test — the BRUTE-FORCE ENGINE isolation invariant.

DEC-ENGINE-ISOLATION-INVARIANT-001 — the P0 mitigation gate for Stage 2b
(DEC-NIFTY-TOTAL-MARKET-001). The Stage 2b hostile review (CLAUDE.md Golden
Rule #9) flagged that admitting ~750 non-F&O equities as REFERENCE_ONLY risks
microcaps leaking into the brute-force combinations + the 52-condition
vocabulary if any future code trusts the wrong scope filter — a Golden Rule #3
scope-lock violation. Today that protection (``is_in_brute_force_scope``,
``brutex/universe.py``) is real but only **unit**-tested; this turns it into a
**property-enforced invariant** so no Stage-2b code (or any future change) can
silently widen the swept set.

The contract (proven against adversarial inputs — an equity *named* like an
index, ``SENSEX`` on the wrong exchange, a reference equity, empty symbols):

* ``is_in_brute_force_scope(i)`` is True **iff** ``i.instrument == IDX`` AND
  ``(i.exch_id, i.symbol_name)`` is one of the exactly-3 brute-force index
  underlyings (sourced from :data:`BRUTE_FORCE_INDEX_UNDERLYINGS`, never
  hardcoded here — Golden Rule #1).
* **No equity is EVER in brute-force scope** — not even one named ``NIFTY``.
* For **any** universe (any list of instruments), the swept symbol set is a
  subset of the 3 named indexes, and every swept row is an ``IDX``.
"""

from __future__ import annotations

from decimal import Decimal

from brutex.models import Exchange, Instrument, InstrumentType, Segment
from brutex.universe import BRUTE_FORCE_INDEX_UNDERLYINGS, is_in_brute_force_scope
from hypothesis import given
from hypothesis import strategies as st

# The exactly-3 brute-force index symbols, taken from the source of truth.
_INDEX_SYMBOLS: list[str] = sorted({sym for _exch, sym in BRUTE_FORCE_INDEX_UNDERLYINGS})

# Symbols chosen to try to fool the filter: the real index names (with possibly
# the WRONG exchange via the strategy), the reference-only VIX, and equities
# (incl. one deliberately named like an index).
_ADVERSARIAL_SYMBOLS: list[str] = [
    *_INDEX_SYMBOLS,
    "INDIAVIX",
    "NIFTY",  # an equity could be (mischievously) named this
    "RELIANCE",
    "RELIANCE INDUSTRIES LTD",  # the non-F&O long-name shape
    "NIFTY IT",
    "",
]


def _make(instrument_type: InstrumentType, exch: Exchange, symbol: str) -> Instrument:
    """Build a valid IDX/EQ Instrument (no derivative → no __post_init__ guard)."""
    is_idx = instrument_type is InstrumentType.IDX
    return Instrument(
        exch_id=exch,
        security_id=1,
        symbol_name=symbol,
        display_name=None,
        instrument=instrument_type,
        segment=Segment.CASH,
        series=None,
        isin=None,
        underlying_symbol=None,
        underlying_security_id=None,
        lot_size=0 if is_idx else 1,
        tick_size=Decimal(0) if is_idx else Decimal("0.05"),
        sm_freeze_qty=None,
        sm_expiry_date=None,
        strike_price=None,
    )


_symbols = st.sampled_from(_ADVERSARIAL_SYMBOLS) | st.text(max_size=12)
_instruments = st.builds(
    _make,
    st.sampled_from([InstrumentType.IDX, InstrumentType.EQ]),
    st.sampled_from([Exchange.NSE, Exchange.BSE]),
    _symbols,
)


@given(_instruments)
def test_brute_force_scope_exact_contract(instrument: Instrument) -> None:
    expected = (
        instrument.instrument is InstrumentType.IDX
        and (instrument.exch_id, instrument.symbol_name) in BRUTE_FORCE_INDEX_UNDERLYINGS
    )
    assert is_in_brute_force_scope(instrument) is expected


@given(_instruments)
def test_no_equity_ever_enters_the_engine(instrument: Instrument) -> None:
    # The Stage-2b landmine: a non-F&O reference equity (even one named like an
    # index) must NEVER be swept.
    if instrument.instrument is InstrumentType.EQ:
        assert is_in_brute_force_scope(instrument) is False


@given(st.lists(_instruments, max_size=40))
def test_swept_set_is_subset_of_the_three_indexes(instruments: list[Instrument]) -> None:
    swept = [i for i in instruments if is_in_brute_force_scope(i)]
    # Every swept row is an IDX (no equity / derivative leaked in)...
    assert all(i.instrument is InstrumentType.IDX for i in swept)
    # ...and the swept symbol set never exceeds the 3 named indexes.
    assert {i.symbol_name for i in swept} <= set(_INDEX_SYMBOLS)


# ────────────────────────────────────────────────────────────────────────
# Engine sweep-entry scope law (DEC-ENGINE-SCOPE-GUARD-001)
# ────────────────────────────────────────────────────────────────────────
#
# The launch-time twin of the instrument-tier invariant above: for ANY
# (exchange, segment, symbol) coordinate the classifier may allow ONLY the 3
# spot triples (DEC-ENGINE-SPOT-ONLY-001 — the engine is spot-only; the
# pre-flip NIFTY-futures NSE/FNO arm is RETIRED) — and a REFERENCE_ONLY
# symbol (India VIX) is NEVER allowed.

_SCOPE_EXCHANGES = st.sampled_from(["NSE", "BSE", "MCX", "nse", ""])
_SCOPE_SEGMENTS = st.sampled_from(["INDEX", "FNO", "CASH", "index", ""])
_SCOPE_SYMBOLS = st.sampled_from(
    [
        *_ADVERSARIAL_SYMBOLS,
        "NSE-NIFTY-FUT",  # the RETIRED series alias (DEC-ENGINE-SPOT-ONLY-001)
        "NIFTY-FUT-2026-07-30",
        "BANKNIFTY-FUT-2026-07-30",
        "NSE-NIFTY-30Jul26-FUT",
        "NSE-NIFTY-30Jul26-24000-CE",
        "NSE-RELIANCE-30Jul26-FUT",
    ]
) | st.text(max_size=16)


@given(_SCOPE_EXCHANGES, _SCOPE_SEGMENTS, _SCOPE_SYMBOLS)
def test_engine_sweep_allowed_is_subset_of_the_law(
    exchange: str, segment: str, symbol: str
) -> None:
    from brutex.universe import ENGINE_SWEEP_SPOT_TRIPLES, classify_engine_sweep_target

    verdict = classify_engine_sweep_target(exchange=exchange, segment=segment, symbol=symbol)
    if verdict.allowed:
        # DEC-ENGINE-SPOT-ONLY-001: the ONLY allowed coordinates are the 3
        # spot index triples — the pre-flip nifty_future NSE/FNO arm is
        # RETIRED, so an allowed verdict outside the triples is a scope leak.
        assert (exchange, segment, symbol) in ENGINE_SWEEP_SPOT_TRIPLES
        assert verdict.tier == "spot_index"
    else:
        assert verdict.tier == "refused"
        assert verdict.reason


@given(_SCOPE_EXCHANGES, _SCOPE_SEGMENTS)
def test_reference_only_vix_never_allowed(exchange: str, segment: str) -> None:
    from brutex.universe import REFERENCE_ONLY_INDEX_UNDERLYINGS, classify_engine_sweep_target

    for _exch, symbol in REFERENCE_ONLY_INDEX_UNDERLYINGS:
        verdict = classify_engine_sweep_target(exchange=exchange, segment=segment, symbol=symbol)
        assert verdict.allowed is False
