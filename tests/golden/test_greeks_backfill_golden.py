"""Frozen golden pins for the bulk Greeks backfill (DEC-GREEKS-BULK-REENRICH-001).

Byte-exact frozen expectations: the v2/fixed-rate INPUT bits, the full
STATUS_OK European stamp, and one fixed plan's content-addressed
``backfill_id``. A drift in ANY of these silently re-keys staleness /
checkpoint identity for every stored lake, so it must be a DELIBERATE,
ledger-recorded decision — never an accident (the greeks-enrich golden
discipline). The backfill deliberately does NOT bump
``GREEKS_FORMULA_VERSION``, so the existing frozen enrich goldens
(``fixtures/greeks_enrich_golden.json`` + the parallel golden) stand
unmodified — asserted structurally here via the version pin.
"""

from __future__ import annotations

from datetime import date

import pytest

pytest.importorskip("trading_core", reason="greeks enrichment needs the trading_core wheel")

from brutex.data.greeks_backfill import BackfillScan, plan_greeks_backfill
from brutex.data.greeks_enrich import (
    GREEKS_FORMULA_VERSION,
    RATE_SOURCE_ID_FIXED,
    STATUS_OK,
    STYLE_EUROPEAN_EXACT,
    GreeksEnrichTarget,
    current_inputs_pack,
    pack_greeks_provenance,
)
from brutex.options.risk_free import FixedRateSource

_RATE = FixedRateSource()

#: The frozen v2/fixed-rate INPUT bits (formula=2 | rate_source=1<<8 |
#: rate_version=1<<12 | q_bias=1<<16 | tte=1<<18).
_GOLDEN_INPUTS_PACK = 332034

#: The frozen full STATUS_OK European v2 stamp (inputs | style=1<<20).
_GOLDEN_OK_STAMP = 1380610

#: The frozen backfill_id of the fixed two-target plan below. RE-KEYED
#: DELIBERATELY in DEC-GREEKS-BULK-REENRICH-001 fix round 4 (ledger #1531,
#: refuter-confirmed MAJOR): the id head now binds the rate-source VALUE
#: fingerprint (here ``fixed=0.065``), so a rate VALUE change under an
#: unchanged source kind re-keys the plan and a crashed force run's surviving
#: checkpoint can never exempt its recorded prefix from the driver's
#: value-checked gate. (Previous frozen id, superseded by that decision:
#: ``5d3638eacbee2094c377d549e0544968546a36e8c86ea19de0945cf1ae867e43``.)
_GOLDEN_BACKFILL_ID = "dae921b2e8c5dcc57eee3984cdb374669a5c8900b33e854298cab5a10d3886bf"


def test_formula_version_unbumped() -> None:
    """The backfill never bumps the formula — the enrich goldens stay frozen."""
    assert GREEKS_FORMULA_VERSION == 2


def test_current_inputs_pack_golden() -> None:
    assert current_inputs_pack(_RATE) == _GOLDEN_INPUTS_PACK


def test_status_ok_european_stamp_golden() -> None:
    stamp = pack_greeks_provenance(
        rate_source_id=RATE_SOURCE_ID_FIXED,
        style_flag=STYLE_EUROPEAN_EXACT,
        status=STATUS_OK,
    )
    assert stamp == _GOLDEN_OK_STAMP
    assert stamp & ((1 << 20) - 1) == _GOLDEN_INPUTS_PACK  # inputs = low 20 bits


def test_backfill_id_golden() -> None:
    """One fixed plan's sha256 id — the checkpoint's cross-run identity anchor."""
    targets = (
        GreeksEnrichTarget(
            exchange="NSE",
            symbol="NSE-NIFTY-25Jan24-21500-CE",
            start=date(2024, 1, 1),
            end=date(2024, 2, 2),
        ),
        GreeksEnrichTarget(
            exchange="NSE",
            symbol="NSE-NIFTY-25Jan24-21500-PE",
            start=date(2024, 1, 1),
            end=date(2024, 2, 2),
        ),
    )
    plan = plan_greeks_backfill(
        BackfillScan(targets=targets), rate_source=_RATE, force=False, expired_only=False
    )
    assert plan.backfill_id == _GOLDEN_BACKFILL_ID
