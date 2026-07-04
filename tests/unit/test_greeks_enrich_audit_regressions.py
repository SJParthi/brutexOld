"""Regressions for the DEC-GREEKS-CORRECTNESS-AUDIT-001 fix round.

Each class pins one confirmed finding family of the 7-lens adversarial audit
of the Greeks enrichment value chain (``brutex/data/greeks_enrich.py`` +
``brutex/options/risk_free.py``): every test FAILED against the pre-fix code
(captured fails-before) and passes after the fix — the permanent net against
the audited defect re-appearing.
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from structlog.testing import capture_logs

pytest.importorskip("trading_core", reason="greeks enrichment needs the trading_core wheel")

from brutex.data.fno_symbol import parse_fno_symbol
from brutex.data.greeks_enrich import (
    RATE_SOURCE_ID_CSV,
    RATE_SOURCE_ID_FIXED,
    STATUS_BAD_INPUT,
    STATUS_BELOW_INTRINSIC,
    STATUS_NO_RATE,
    STATUS_NO_SPOT,
    STATUS_OK,
    STYLE_AMERICAN_APPROX,
    STYLE_EUROPEAN_EXACT,
    GreeksEnrichTarget,
    enrich_greeks_for_targets,
    enrich_option_bars,
    pack_greeks_provenance,
    unpack_greeks_provenance,
)
from brutex.data.models import GREEKS_OVERLAY_FIELDS, FnoGreeksBar
from brutex.data.spot_join import SpotJoin
from brutex.options.risk_free import CsvRateSource, FixedRateSource, RiskFreeRate

if TYPE_CHECKING:
    from brutex.data.fno_symbol import FnoSymbolParts

_CE_SYMBOL = "NSE-NIFTY-25Jan24-21500-CE"
_EXPIRY = date(2024, 1, 25)
_BAR_TS = datetime(2024, 1, 15, 3, 45, tzinfo=UTC)  # 09:15 IST, ten days out
_WINDOW = (date(2024, 1, 1), date(2024, 1, 20))
_SPOT = Decimal("21500")
_RATE = FixedRateSource()


def _parts(symbol: str) -> FnoSymbolParts:
    parts = parse_fno_symbol(symbol)
    assert parts is not None
    return parts


def _row(
    ts: datetime = _BAR_TS,
    *,
    close: float = 200.0,
    overlay: dict[str, object] | None = None,
) -> dict[str, object]:
    """One lake row shaped like the schema-tolerant option reader's output."""
    row: dict[str, object] = {
        "timestamp": ts,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": 10,
        "open_interest": 100,
    }
    for name in GREEKS_OVERLAY_FIELDS:
        row[name] = None
    if overlay:
        row.update(overlay)
    return row


def _enrich_rows(
    rows: list[dict[str, object]],
    *,
    symbol: str = _CE_SYMBOL,
    spot_by_ts: dict[datetime, Decimal] | None = None,
    rate_source: object = _RATE,
    style_flag: int = STYLE_EUROPEAN_EXACT,
) -> tuple[list[FnoGreeksBar], object]:
    return enrich_option_bars(
        rows,
        parts=_parts(symbol),
        spot_by_ts={_BAR_TS: _SPOT} if spot_by_ts is None else spot_by_ts,
        rate_source=rate_source,  # type: ignore[arg-type]
        style_flag=style_flag,
    )


def _status_of(bar: FnoGreeksBar) -> int:
    pid = bar.greeks_provenance_id
    assert pid is not None
    return unpack_greeks_provenance(pid).status


class TestPackBandValidation:
    """PROV-2 (FIX-9): out-of-band provenance fields raise instead of colliding.

    ``pack_greeks_provenance`` silently ``&``-masked every field, so a future
    ``RISK_FREE_SOURCE_VERSION = 17`` stamp was bit-identical to today's v1 —
    the staleness detector would reuse stale Greeks against it forever. Every
    field is now band-validated with a loud ``ValueError`` naming the field.
    """

    def test_rate_version_17_raises_instead_of_colliding_with_1(self) -> None:
        with pytest.raises(ValueError, match=r"rate_version=17 outside its 4-bit band"):
            pack_greeks_provenance(
                rate_source_id=RATE_SOURCE_ID_FIXED,
                rate_version=17,
                style_flag=STYLE_EUROPEAN_EXACT,
                status=STATUS_OK,
            )

    def test_negative_field_raises(self) -> None:
        with pytest.raises(ValueError, match=r"status=-1 outside its 4-bit band"):
            pack_greeks_provenance(
                rate_source_id=RATE_SOURCE_ID_FIXED,
                style_flag=STYLE_EUROPEAN_EXACT,
                status=-1,
            )

    def test_out_of_band_formula_and_style_raise(self) -> None:
        with pytest.raises(ValueError, match=r"formula_version=256 outside its 8-bit band"):
            pack_greeks_provenance(
                rate_source_id=RATE_SOURCE_ID_FIXED,
                style_flag=STYLE_EUROPEAN_EXACT,
                status=STATUS_OK,
                formula_version=256,
            )
        with pytest.raises(ValueError, match=r"style_flag=8 outside its 3-bit band"):
            pack_greeks_provenance(
                rate_source_id=RATE_SOURCE_ID_FIXED, style_flag=8, status=STATUS_OK
            )

    def test_in_band_golden_pid_byte_identical(self) -> None:
        # The audited v1-era golden stamp: explicit in-band fields still pack
        # to the exact historical integer (no packing-layout drift).
        pid = pack_greeks_provenance(
            rate_source_id=RATE_SOURCE_ID_FIXED,
            rate_version=1,
            style_flag=STYLE_EUROPEAN_EXACT,
            status=STATUS_OK,
            formula_version=1,
            q_bias_id=1,
            tte_convention_id=1,
        )
        assert pid == 1380609
