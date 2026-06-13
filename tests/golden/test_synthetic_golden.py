"""Golden byte-equality test for the synthetic generator (DEC-SYNTHETIC-ADAPTER-001).

Pins the sha256 of a canonical ``(DEFAULT_SEED, NIFTY, 1minute,
2021-01-04 → 2021-01-05)`` bar sequence. Any change to the deterministic
generator that alters even one digit of one price/volume/timestamp breaks this
digest — the reproducibility contract (Golden Rule #4) made executable. If this
test ever fails, the generator's output changed; update the digest ONLY with a
decision-ID'd, deliberate reason.
"""

from __future__ import annotations

import hashlib
from datetime import date

import pytest
from brutex.adapters.synthetic import SyntheticMarketAdapter
from brutex.data.models import CandleBar
from brutex.models import Exchange

pytestmark = pytest.mark.golden

#: Frozen canonical inputs.
_START = date(2021, 1, 4)
_END = date(2021, 1, 5)
#: A regular session at 1-minute = exactly 375 bars.
_GOLDEN_BAR_COUNT = 375
#: Pinned digest of the canonical INDEX sequence (NIFTY — zero-volume bars).
_GOLDEN_NIFTY_SHA256 = "f730389de703eb49f8ab638b4704ff79847ea7d366c7ec889a150920f2b92c37"
#: Pinned digest of the canonical EQUITY sequence (SYNTH — pins the traded-volume
#: path so a volume-formula mutation is caught, not just the price path).
_GOLDEN_SYNTH_SHA256 = "03535fcb481a37e23daf9d2be0f34aab906389961f1b6a4214a72224abb71bce"


def _serialise(bars: list[CandleBar]) -> str:
    return "\n".join(
        f"{b.timestamp.isoformat()}|{b.open}|{b.high}|{b.low}|{b.close}|{b.volume}|{b.open_interest}"
        for b in bars
    )


def _golden_digest(symbol: str, exchange: Exchange) -> tuple[int, str]:
    bars = SyntheticMarketAdapter().fetch_ohlcv(
        symbol=symbol, exchange=exchange, interval="1minute", start=_START, end=_END
    )
    return len(bars), hashlib.sha256(_serialise(bars).encode()).hexdigest()


def test_synthetic_nifty_index_day_golden_digest() -> None:
    count, digest = _golden_digest("NIFTY", Exchange.NSE)
    assert count == _GOLDEN_BAR_COUNT
    assert digest == _GOLDEN_NIFTY_SHA256, (
        f"Synthetic INDEX generator golden digest drift:\n"
        f"  expected: {_GOLDEN_NIFTY_SHA256}\n"
        f"  observed: {digest}\n"
        f"The deterministic generator's output changed — update only deliberately."
    )


def test_synthetic_synth_equity_day_golden_digest() -> None:
    count, digest = _golden_digest("SYNTH", Exchange.NSE)
    assert count == _GOLDEN_BAR_COUNT
    assert digest == _GOLDEN_SYNTH_SHA256, (
        f"Synthetic EQUITY generator golden digest drift (price OR volume path):\n"
        f"  expected: {_GOLDEN_SYNTH_SHA256}\n"
        f"  observed: {digest}\n"
        f"The deterministic generator's output changed — update only deliberately."
    )
