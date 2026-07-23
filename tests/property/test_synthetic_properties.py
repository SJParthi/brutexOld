"""Hypothesis property tests for the synthetic offline adapter.

Proves the load-bearing invariants over randomised ``(seed, symbol, interval,
range)`` inputs (DEC-SYNTHETIC-ADAPTER-001):

1. **Idempotence / determinism.** The same inputs always yield a bit-identical
   bar list (Golden Rule #4 / #7) — including across a fresh subprocess
   (``blake2b``, not Python's salted ``hash()``).
2. **OHLC + grid validity.** Every bar has ``low <= open, close <= high``,
   2-decimal prices, a tz-aware UTC timestamp, and sits inside its real
   trading-session window.
3. **Volume semantics.** Index bars carry zero volume; the equity carries
   non-negative volume.
4. **Monotonic timestamps.** Bars are strictly ascending.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from datetime import UTC, date, timedelta
from decimal import Decimal

from brutex.adapters.synthetic import SUPPORTED_INTERVALS, SyntheticMarketAdapter
from brutex.calendars import IST, get_session
from brutex.data.models import CandleBar
from brutex.models import Exchange
from hypothesis import given, settings
from hypothesis import strategies as st

_SYMBOLS = ("NIFTY", "BANKNIFTY", "SENSEX", "SYNTH")
_INTERVALS = sorted(SUPPORTED_INTERVALS)
#: Well inside the verified NSE/BSE calendar coverage (2021-2026).
_MIN_DATE = date(2021, 1, 1)
_MAX_DATE = date(2023, 12, 1)


def _digest(bars: list[CandleBar]) -> str:
    serialised = "\n".join(
        f"{b.timestamp.isoformat()}|{b.open}|{b.high}|{b.low}|{b.close}|{b.volume}|{b.open_interest}"
        for b in bars
    )
    return hashlib.sha256(serialised.encode()).hexdigest()


@given(
    seed=st.integers(min_value=0, max_value=2**32 - 1),
    symbol=st.sampled_from(_SYMBOLS),
    interval=st.sampled_from(_INTERVALS),
    start=st.dates(min_value=_MIN_DATE, max_value=_MAX_DATE),
    span=st.integers(min_value=1, max_value=4),
)
@settings(max_examples=60, deadline=None)
def test_fetch_is_deterministic_and_valid(
    seed: int, symbol: str, interval: str, start: date, span: int
) -> None:
    adapter = SyntheticMarketAdapter(seed=seed)
    end = start + timedelta(days=span)
    exchange = Exchange.BSE if symbol == "SENSEX" else Exchange.NSE
    bars = adapter.fetch_ohlcv(
        symbol=symbol, exchange=exchange, interval=interval, start=start, end=end
    )

    # 1. Idempotence — re-fetch is bit-identical.
    assert (
        adapter.fetch_ohlcv(
            symbol=symbol, exchange=exchange, interval=interval, start=start, end=end
        )
        == bars
    )

    is_index = symbol != "SYNTH"
    prev_ts = None
    for b in bars:
        # 2. OHLC + grid validity.
        assert b.low <= b.open <= b.high
        assert b.low <= b.close <= b.high
        assert b.high >= b.low
        for px in (b.open, b.high, b.low, b.close):
            assert px == px.quantize(Decimal("0.01"))
        assert b.timestamp.tzinfo is not None
        assert b.timestamp.utcoffset() == UTC.utcoffset(None)
        # Inside the real session window for its IST date.
        ist = b.timestamp.astimezone(IST)
        session = get_session(ist.date())
        assert session is not None
        assert session.open_time_ist <= ist.time() < session.close_time_ist
        # 3. Volume semantics.
        assert b.volume == 0 if is_index else b.volume >= 0
        assert b.open_interest is None
        # 4. Strictly ascending timestamps.
        if prev_ts is not None:
            assert b.timestamp > prev_ts
        prev_ts = b.timestamp


@given(
    seed=st.integers(min_value=0, max_value=2**16),
    symbol=st.sampled_from(_SYMBOLS),
)
@settings(max_examples=20, deadline=None)
def test_distinct_seeds_diverge(seed: int, symbol: str) -> None:
    exchange = Exchange.BSE if symbol == "SENSEX" else Exchange.NSE
    start, end = date(2021, 3, 1), date(2021, 3, 2)
    one = SyntheticMarketAdapter(seed=seed).fetch_ohlcv(
        symbol=symbol, exchange=exchange, interval="1minute", start=start, end=end
    )
    two = SyntheticMarketAdapter(seed=seed + 1).fetch_ohlcv(
        symbol=symbol, exchange=exchange, interval="1minute", start=start, end=end
    )
    # Same length (same session), but the seed perturbs every price path.
    assert len(one) == len(two)
    assert [b.open for b in one] != [b.open for b in two]


_SUBPROCESS_SRC = """
import hashlib
from datetime import date
from brutex.adapters.synthetic import SyntheticMarketAdapter
from brutex.models import Exchange

bars = SyntheticMarketAdapter().fetch_ohlcv(
    symbol="NIFTY", exchange=Exchange.NSE, interval="1minute",
    start=date(2021, 1, 4), end=date(2021, 1, 5),
)
serialised = "\\n".join(
    f"{b.timestamp.isoformat()}|{b.open}|{b.high}|{b.low}|{b.close}|{b.volume}|{b.open_interest}"
    for b in bars
)
print(hashlib.sha256(serialised.encode()).hexdigest())
"""


def test_determinism_holds_across_a_fresh_process() -> None:
    """A separate interpreter reproduces the exact same bars (no salted hashing)."""
    in_process = _digest(
        SyntheticMarketAdapter().fetch_ohlcv(
            symbol="NIFTY",
            exchange=Exchange.NSE,
            interval="1minute",
            start=date(2021, 1, 4),
            end=date(2021, 1, 5),
        )
    )
    # The fresh `python -c` subprocess runs with UNCONFIGURED structlog, so any
    # warn-once event lands on STDOUT (the default PrintLogger sink) and would
    # pollute the captured hash. The config-core money/models seams default ON
    # (DEC-RUST-DEFAULT-ON-WAVE-001) and, on a wheel-less lane, emit a one-shot
    # `*.rust_path_disabled` degrade-to-oracle warning. Pin them to the explicit
    # silent kill switch: ON degrades BIT-IDENTICAL to the Python oracle, so the
    # determinism guarantee is unchanged — the subprocess just stays warn-free.
    subprocess_env = {
        **os.environ,
        "BRUTEX_CORE_RUST_MONEY": "0",
        "BRUTEX_CORE_RUST_MODELS": "0",
    }
    completed = subprocess.run(
        [sys.executable, "-c", _SUBPROCESS_SRC],
        capture_output=True,
        text=True,
        check=True,
        env=subprocess_env,
    )
    assert completed.stdout.strip() == in_process
