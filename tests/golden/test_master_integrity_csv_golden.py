"""Golden byte-equality oracle for the master-integrity CSV (DEC-INSTRUMENT-MASTER-INTEGRITY-001).

Locks the EXACT bytes of :func:`brutex.dashboard.master_integrity_csv.master_integrity_to_csv`
for a hand-fixed master (RELIANCE resolved-both, M&M token-only, a NIFTY index with no legs,
and one orphan leg). Any serializer drift flips the SHA-256 and fails loudly (Golden Rule #4).
The report is built by the REAL audit over hand-built instruments (no store/lake/network).
"""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from decimal import Decimal

from brutex.dashboard.master_integrity_csv import master_integrity_to_csv
from brutex.data.master_integrity import audit_master_integrity
from brutex.models import Exchange, Instrument, InstrumentType, Segment

_GOLDEN_SHA256 = "117691185751632b20b026d15d5e43e3ee3ab68850d57d0a499501b3e1b5d8ed"


def _spot(sym: str, sid: int, kind: InstrumentType, ft: str | None) -> Instrument:
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=sid,
        symbol_name=sym,
        display_name=sym,
        instrument=kind,
        segment=Segment.INDEX if kind is InstrumentType.IDX else Segment.CASH,
        series=None,
        isin=None,
        underlying_symbol=None,
        underlying_security_id=None,
        lot_size=0 if kind is InstrumentType.IDX else 1,
        tick_size=Decimal("0") if kind is InstrumentType.IDX else Decimal("0.05"),
        sm_freeze_qty=None,
        sm_expiry_date=None,
        strike_price=None,
        asm_gsm_flag=False,
        feed_token=ft,
    )


def _leg(sym: str, sid: int, kind: InstrumentType, usym: str, usid: int | None) -> Instrument:
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=sid,
        symbol_name=sym,
        display_name=sym,
        instrument=kind,
        segment=Segment.FNO,
        series=None,
        isin=None,
        underlying_symbol=usym,
        underlying_security_id=usid,
        lot_size=50,
        tick_size=Decimal("0.05"),
        sm_freeze_qty=None,
        sm_expiry_date=date(2024, 1, 25),
        strike_price=None if kind is InstrumentType.FUT else Decimal("100"),
        asm_gsm_flag=False,
    )


def _golden_report() -> object:
    insts = [
        _spot("RELIANCE", 100, InstrumentType.EQ, "100"),
        _spot("M&M", 2031, InstrumentType.EQ, "2031"),
        _spot("NIFTY", 99999, InstrumentType.IDX, "NIFTY"),
        _leg("R24JANFUT", 920, InstrumentType.FUT, "RELIANCE", 100),  # resolved_both
        _leg("MM24JANCE", 903, InstrumentType.CE, "MAHINDRA", 2031),  # token_only
        _leg("NOPECE", 930, InstrumentType.CE, "NOPE", None),  # orphan
    ]
    return audit_master_integrity(
        insts,
        now=datetime(2026, 6, 21, tzinfo=UTC),
        run_id="run-golden",
        csv_source_sha256="deadbeef",
    )


def test_master_integrity_csv_sha256_pinned() -> None:
    rendered = master_integrity_to_csv(_golden_report())  # type: ignore[arg-type]
    assert hashlib.sha256(rendered.encode("utf-8")).hexdigest() == _GOLDEN_SHA256
