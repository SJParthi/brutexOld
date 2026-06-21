"""Golden byte-equality oracle for the economics-integrity CSV (DEC-INSTRUMENT-ECONOMICS-INTEGRITY-001).

Locks the EXACT bytes of `economics_integrity_to_csv` for a hand-fixed master (one within-expiry
lot mismatch = RED + one cross-expiry revision = informational). Any serializer drift flips the
SHA-256 and fails loudly (Golden Rule #4).
"""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from decimal import Decimal

from brutex.dashboard.economics_integrity_csv import economics_integrity_to_csv
from brutex.data.economics_integrity import audit_economics_integrity
from brutex.models import Exchange, Instrument, InstrumentType, Segment

_GOLDEN_SHA256 = "6385d4f183107247d4b7d96bbb79b1b09c1c48c7385cd63d40579ebcf920947a"


def _leg(s: str, i: int, u: str, e: date, lot: int) -> Instrument:
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=i,
        symbol_name=s,
        display_name=s,
        instrument=InstrumentType.CE,
        segment=Segment.FNO,
        series=None,
        isin=None,
        underlying_symbol=u,
        underlying_security_id=100,
        lot_size=lot,
        tick_size=Decimal("0.05"),
        sm_freeze_qty=lot * 10,
        sm_expiry_date=e,
        strike_price=Decimal("100"),
        asm_gsm_flag=False,
    )


def test_economics_integrity_csv_sha256_pinned() -> None:
    e1, e2 = date(2026, 6, 25), date(2026, 7, 30)
    rep = audit_economics_integrity(
        [
            _leg("RCE1", 1, "RELIANCE", e1, 500),
            _leg("RCE2", 2, "RELIANCE", e1, 750),
            _leg("ACE1", 3, "AMBUJACEM", e1, 1050),
            _leg("ACE2", 4, "AMBUJACEM", e2, 1200),
        ],
        now=datetime(2026, 6, 21, tzinfo=UTC),
        run_id="run-golden",
        csv_source_sha256="deadbeef",
    )
    rendered = economics_integrity_to_csv(rep)
    assert hashlib.sha256(rendered.encode("utf-8")).hexdigest() == _GOLDEN_SHA256
