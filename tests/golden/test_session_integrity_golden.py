"""Frozen session-integrity goldens (DEC-EXTENDED-SESSION-OUTAGE-001 +
DEC-SESSION-DENSITY-CENSUS-001).

* the SYNTHETIC 2021-02-24 extended-day trade list — byte-frozen: the trade
  rides through the OLD 15:20 boundary and force-exits at 16:50 (fails on the
  pre-fix resolver, which forced out at 15:20 — the reversion-gate teeth);
* the census verdict over the REAL 2024-01-02..04 anchor with the FABRICATED
  45-minute hole — byte-frozen flag shape/gap/abstain payload.

Regeneration is env-gated (``BRUTEX_REGEN_SESSION_INTEGRITY_GOLDEN=1`` in the
generator) — the frozen capture is the oracle (Golden Rule #1).
"""

from __future__ import annotations

from tests.golden._session_integrity_harness import (
    GOLDEN_CENSUS,
    GOLDEN_TRADES,
    census_payload,
    extended_day_trades_payload,
    render,
)


def test_extended_day_trade_list_matches_frozen_bytes() -> None:
    assert GOLDEN_TRADES.exists(), "run tests/golden/generate_session_integrity_golden.py once"
    assert render(extended_day_trades_payload()) == GOLDEN_TRADES.read_bytes()


def test_extended_day_trade_exits_at_1650_not_1520() -> None:
    # The semantic teeth on top of the byte pin: exactly one trade, exiting at
    # the 16:50 IST bar (11:20 UTC), pnl == the 16:50 open − the entry open.
    payload = extended_day_trades_payload()
    assert len(payload["trades"]) == 1
    trade = payload["trades"][0]
    assert trade["exit_ts"] == "2021-02-24T11:20:00+00:00"  # 16:50 IST
    assert trade["pnl_paisa"] == 60  # entry 100.10 @15:19-next-open ... forced @100.70


def test_census_verdict_matches_frozen_bytes() -> None:
    assert GOLDEN_CENSUS.exists(), "run tests/golden/generate_session_integrity_golden.py once"
    assert render(census_payload()) == GOLDEN_CENSUS.read_bytes()
