"""Deprecation contract of the dormant ``brutex.execution.costs`` model (R2-OC-8).

The stale ``IndianCostModel`` (STT 0.05% vs the shipped 0.0625/0.10/0.15%
regimes; zero stamp duty; no IPFT) is dead production code kept only because
the equally-dormant execution simulator/driver stack self-tests it. It must:

- emit a ``DeprecationWarning`` when CONSTRUCTED (pointing at the
  authoritative ``brutex.costs.calculator``), and
- emit NOTHING on a plain ``import brutex.execution`` (which eagerly
  re-exports the class — an import-time warning would spam every brutex
  import).

Its numeric behaviour stays FROZEN (dormant = frozen) — pinned here.
"""

from __future__ import annotations

import subprocess
import sys
from decimal import Decimal

import pytest
from brutex.execution.costs import IndianCostModel
from brutex.models import InstrumentType, Segment
from brutex.strategies.types import Side


def test_deprecation_warning_fires_on_construction() -> None:
    with pytest.warns(DeprecationWarning, match="IndianCostModel .* is DEPRECATED"):
        IndianCostModel()


def test_deprecation_warning_names_the_authoritative_model() -> None:
    with pytest.warns(DeprecationWarning, match="brutex.costs.calculator.compute_trade_cost"):
        IndianCostModel(brokerage_per_order=Decimal("20"))


def test_import_brutex_execution_does_not_warn() -> None:
    """A plain ``import brutex.execution`` (which re-exports IndianCostModel)
    must stay silent — the warning is at CONSTRUCTION, not import. Proven in
    a fresh interpreter with DeprecationWarning escalated to an error."""
    proc = subprocess.run(
        [sys.executable, "-W", "error::DeprecationWarning", "-c", "import brutex.execution"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr


def test_numeric_behaviour_frozen_after_deprecation() -> None:
    """Dormant = frozen: the (stale, documented) numbers are unchanged by the
    deprecation — one options sell fill pinned byte-exact."""
    with pytest.warns(DeprecationWarning, match="IndianCostModel"):
        model = IndianCostModel()
    costs = model.compute(
        side=Side.SELL,
        qty=75,
        fill_price=Decimal("100"),
        segment=Segment.FNO,
        instrument_type=InstrumentType.CE,
    )
    # notional 7500; stale STT 0.05% -> 3.75; exch 0.03503% -> 2.63 (half-even);
    # SEBI 0.0001% -> 0.01 (0.0075 rounds half-even to 0.01); GST 18% of
    # (20 + 2.63 + 0.01) = 4.0752 -> 4.08; stamp 0 (never modelled here).
    assert costs.brokerage == Decimal("20.00")
    assert costs.stt == Decimal("3.75")
    assert costs.exchange_fee == Decimal("2.63")
    assert costs.sebi_fee == Decimal("0.01")
    assert costs.stamp_duty == Decimal("0")
    assert costs.gst == Decimal("4.08")
