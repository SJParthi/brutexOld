"""O-cost bench — the ADR-0012 futures-series resolver + stitched-slice access.

DEC-NIFTY-FUTURES-SWEEP-LAUNCH-001. **Honest, narrow claims (Golden Rule #10)
— the resolver is O(D) in the FNO dir count, NOT O(1):**

1. **Resolution is LINEAR in the dir count** — the PER-CONTRACT cost is flat
   between a 12-contract lake and a 72-contract lake carrying 10× foreign
   option dirs (the ``-CE``/``-PE`` names cost one suffix compare each at the
   scan prefilter). A super-linear regression (an accidental per-dir re-scan /
   re-glob) blows the flat-per-contract ratio.
2. **Per-bar access on the stitched slice is O(1)** — ``load_sweep_slice``
   returns ONE materialised ``list[CandleBar]``, so indexing it is
   size-independent (the engine's per-bar path envelope).

Ceilings route through :mod:`tests.bench._o1_tiers`
(DEC-O1-STRICT-TIER-001 — CLAUDE.md §11 COMMON). Resolution runs ONCE per
launch, never per bar; correctness is unit-owned by
``tests/unit/test_futures_series.py``.
"""

from __future__ import annotations

import calendar
import time
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.data.futures_series import resolve_futures_series
from brutex.data.models import CandleBar

from tests.bench._o1_tiers import ratio_ceiling

pytestmark = pytest.mark.bench

_SMALL_CONTRACTS = 12
_BIG_CONTRACTS = 72
_OPTION_DIRS_PER_CONTRACT = 10  # the 10× foreign-dir load on the big lake
_REPEATS = 5


def _last_thursday(year: int, month: int) -> date:
    """The month's last Thursday — the synthetic monthly-expiry grid."""
    last_day = calendar.monthrange(year, month)[1]
    d = date(year, month, last_day)
    while d.weekday() != 3:  # Thursday
        d -= timedelta(days=1)
    return d


def _monthly_expiries(n: int, *, start_year: int = 2020) -> list[date]:
    out: list[date] = []
    year, month = start_year, 1
    for _ in range(n):
        out.append(_last_thursday(year, month))
        month += 1
        if month > 12:
            month, year = 1, year + 1
    return out


def _contract_dir_name(expiry: date) -> str:
    return f"NSE-NIFTY-{expiry.strftime('%d%b%y')}-FUT"


def _build_dir_lake(root: Path, expiries: list[date], *, option_dirs: int) -> None:
    """Create the contract dirs (+ *option_dirs* foreign option dirs per contract).

    The resolver's scan reads DIR NAMES only (parse + window math — no file
    I/O), so empty dirs are the honest workload for the scan-cost pin.
    """
    fno = root / "bars" / "NSE" / "FNO"
    fno.mkdir(parents=True)
    for expiry in expiries:
        (fno / _contract_dir_name(expiry)).mkdir()
        token = expiry.strftime("%d%b%y")
        for k in range(option_dirs):
            (fno / f"NSE-NIFTY-{token}-{24000 + 100 * k}-CE").mkdir()


def _per_contract_resolve_s(root: Path, expiries: list[date]) -> float:
    """Best-of-N amortised per-contract resolution time (seconds)."""
    start, end = expiries[0] - timedelta(days=20), expiries[-1]
    best = float("inf")
    for _ in range(_REPEATS):
        t0 = time.perf_counter()
        res = resolve_futures_series(root, start=start, end=end)
        best = min(best, (time.perf_counter() - t0) / len(expiries))
        assert len(res.segments) == len(expiries)
    return best


@pytest.mark.slow
def test_resolver_per_contract_cost_is_flat_in_dir_count(tmp_path: Path) -> None:
    """O(D) pinned LINEAR: per-contract cost flat from 12 → 72 contracts + 10× options."""
    small_root = tmp_path / "small"
    big_root = tmp_path / "big"
    small = _monthly_expiries(_SMALL_CONTRACTS, start_year=2024)
    big = _monthly_expiries(_BIG_CONTRACTS, start_year=2020)
    _build_dir_lake(small_root, small, option_dirs=0)
    _build_dir_lake(big_root, big, option_dirs=_OPTION_DIRS_PER_CONTRACT)

    per_small = _per_contract_resolve_s(small_root, small)
    per_big = _per_contract_resolve_s(big_root, big)
    ratio = per_big / max(per_small, 1e-12)

    ceiling = ratio_ceiling(5.0)
    print(
        f"\nfutures-series resolver linearity bench — best of {_REPEATS}:\n"
        f"  {_SMALL_CONTRACTS} contracts | {per_small * 1e6:.1f} µs/contract\n"
        f"  {_BIG_CONTRACTS} contracts + {_OPTION_DIRS_PER_CONTRACT}× option dirs "
        f"| {per_big * 1e6:.1f} µs/contract\n"
        f"  ratio = {ratio:.2f}× (ceiling {ceiling}×)\n"
    )
    assert ratio < ceiling, (
        f"resolver per-contract cost ratio {ratio:.2f}× exceeds the {ceiling}× ceiling — "
        f"resolution may have regressed to super-linear in the FNO dir count "
        f"(per_small={per_small * 1e6:.1f}µs, per_big={per_big * 1e6:.1f}µs)"
    )


def _stitched_list(n: int) -> list[CandleBar]:
    """A synthetic stitched slice of *n* bars (the loader's return shape)."""
    base = datetime(2025, 6, 2, 3, 46, tzinfo=UTC)
    price = Decimal("100.00")
    return [
        CandleBar(
            timestamp=base + timedelta(minutes=i),
            open=price,
            high=price,
            low=price,
            close=price,
            volume=100,
            open_interest=None,
        )
        for i in range(n)
    ]


def _per_access_s(bars: list[CandleBar], probes: int = 50_000) -> float:
    """Best-of-N per-index-access time over a fixed probe count."""
    n = len(bars)
    indices = [(i * 7919) % n for i in range(probes)]  # deterministic scatter
    best = float("inf")
    for _ in range(_REPEATS):
        t0 = time.perf_counter()
        acc = 0
        for i in indices:
            acc += bars[i].volume
        best = min(best, (time.perf_counter() - t0) / probes)
        assert acc == 100 * probes
    return best


@pytest.mark.slow
def test_stitched_slice_per_bar_access_is_flat() -> None:
    """Per-bar access on the stitched list is O(1) — independent of slice length."""
    small = _stitched_list(1_000)
    big = _stitched_list(100_000)
    per_small = _per_access_s(small)
    per_big = _per_access_s(big)
    ratio = per_big / max(per_small, 1e-12)
    ceiling = ratio_ceiling(5.0)
    print(
        f"\nstitched-slice per-bar access bench — best of {_REPEATS}:\n"
        f"  1,000 bars | {per_small * 1e9:.1f} ns/access\n"
        f"  100,000 bars | {per_big * 1e9:.1f} ns/access\n"
        f"  ratio = {ratio:.2f}× (ceiling {ceiling}×)\n"
    )
    assert ratio < ceiling
