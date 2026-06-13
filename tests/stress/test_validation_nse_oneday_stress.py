"""Sustained-load stress for the NSE/BSE 1-day cross-check (DEC-NSE-1DAY-VALIDATION-001).

Invariant under load: :func:`~brutex.data.validation.nse_oneday.validate_brute_force_indices`
NEVER crashes and ALWAYS returns a well-formed :class:`BatchReport`, even when
BOTH injected fetchers misbehave at random — timeouts, bot-blocks, garbage
bodies, vendor errors, empty payloads. This is the degrade-never-crash guarantee
(CLAUDE.md §13 priority law) exercised over thousands of randomized rounds.

Budget
------
* CI nightly: ``N_ROUNDS = 3_000`` (default, well under the GHA 60s flake line).
* Local soak: ``STRESS_ROUNDS=1_000_000`` via ``workflow_dispatch``.
"""

from __future__ import annotations

import os
import random
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from brutex.calendars import IST
from brutex.data.models import CandleBar
from brutex.data.validation.nse_oneday import (
    BatchReport,
    IndexSource,
    VerdictStatus,
    validate_brute_force_indices,
)
from brutex.models import BruteXError

pytestmark = pytest.mark.stress

_DATE = date(2025, 9, 24)
_VALID_STATUSES = set(VerdictStatus)

_GOOD_CSV = (
    "Index Name,Open Index Value,High Index Value,Low Index Value,Closing Index Value\n"
    "Nifty 50,25000.10,25100.20,24900.05,25050.75\n"
    "Nifty Bank,54000.50,54250.90,53800.10,54100.25\n"
)


def _bar() -> CandleBar:
    ts = datetime(2025, 9, 24, 9, 15, tzinfo=IST).astimezone(UTC)
    return CandleBar(
        timestamp=ts,
        open=Decimal("25000.10"),
        high=Decimal("25100.20"),
        low=Decimal("24900.05"),
        close=Decimal("25050.75"),
        volume=0,
        open_interest=None,
    )


def test_validate_never_crashes_under_random_failures() -> None:
    n_rounds = int(os.environ.get("STRESS_ROUNDS", "3000"))
    rng = random.Random(20250924)

    def chaos_groww(_source: IndexSource, _trade_date: date) -> list[CandleBar]:
        roll = rng.random()
        if roll < 0.25:
            raise TimeoutError("groww timed out")
        if roll < 0.40:
            raise BruteXError("groww vendor error")
        if roll < 0.50:
            return []  # no bar
        return [_bar(), _bar()] if roll < 0.60 else [_bar()]  # sometimes ambiguous

    def chaos_opener(_url: str) -> str:
        roll = rng.random()
        if roll < 0.30:
            raise OSError("blocked / timeout")
        if roll < 0.45:
            raise ValueError("decode error")
        if roll < 0.60:
            return "<html>bot challenge</html>"  # unparsable garbage
        return _GOOD_CSV

    for _ in range(n_rounds):
        report = validate_brute_force_indices(
            _DATE, groww_fetcher=chaos_groww, official_opener=chaos_opener
        )
        assert isinstance(report, BatchReport)
        assert len(report.verdicts) == 3
        for verdict in report.verdicts:
            assert verdict.status in _VALID_STATUSES
            # Counts are always consistent — no verdict is ever lost.
        assert report.matched + report.mismatched + report.unavailable == 3
