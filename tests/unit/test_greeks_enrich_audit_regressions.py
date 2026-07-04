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


class TestDegenerateInputHandling:
    """IVX-2 + IVX-3 (FIX-4): NaN/negative inputs degrade per-bar, never per-contract.

    A NaN close used to make the honest-NULL bar ITSELF raise
    (``decimal.InvalidOperation`` inside ``FnoGreeksBar`` validation),
    escaping into the per-TARGET quarantine — the whole contract errored
    every pass, good bars never enriched. A zero/negative close on an ITM
    strike mislabelled ``BELOW_INTRINSIC`` instead of ``BAD_INPUT``.
    """

    def test_nan_close_row_skipped_other_bars_enrich(self) -> None:
        good_ts = _BAR_TS + timedelta(minutes=1)
        rows = [_row(close=math.nan), _row(good_ts, close=200.0)]
        with capture_logs() as logs:
            bars, stats = _enrich_rows(
                rows, spot_by_ts={_BAR_TS: _SPOT, good_ts: _SPOT}
            )
        assert len(bars) == 1  # the NaN row builds NO bar (stored raw row untouched)
        assert stats.skipped == 1
        assert stats.solved == 1
        assert _status_of(bars[0]) == STATUS_OK
        assert any(e["event"] == "greeks_enrich.bar_skipped_nonfinite" for e in logs)

    def test_inf_high_row_skipped(self) -> None:
        row = _row()
        row["high"] = math.inf
        _bars, stats = _enrich_rows([row])
        assert stats.skipped == 1 and stats.solved == 0

    def test_nan_close_target_not_quarantined_in_driver(self, tmp_path: Path) -> None:
        good_ts = _BAR_TS + timedelta(minutes=1)
        rows = [_row(close=math.nan), _row(good_ts, close=200.0)]
        report = enrich_greeks_for_targets(
            targets=[
                GreeksEnrichTarget(
                    exchange="NSE", symbol=_CE_SYMBOL, start=_WINDOW[0], end=_WINDOW[1]
                )
            ],
            lake_root=tmp_path,
            rate_source=_RATE,
            read_bars=lambda _t: rows,
            read_spot=lambda _t, _p: SpotJoin({_BAR_TS: _SPOT, good_ts: _SPOT}, "INDEX"),
            write_bars=lambda _t, b: len(list(b)),  # type: ignore[arg-type]
        )
        assert report.errored == 0  # the whole contract is NOT quarantined
        assert report.enriched == 1
        assert report.bars_skipped == 1
        assert report.bars_solved == 1

    def test_nan_spot_treated_as_no_spot(self) -> None:
        with capture_logs() as logs:
            bars, stats = _enrich_rows([_row()], spot_by_ts={_BAR_TS: Decimal("NaN")})
        assert stats.nulls == 1
        assert _status_of(bars[0]) == STATUS_NO_SPOT
        assert bars[0].spot_at_bar is None  # never a stored NaN spot
        assert any(e["event"] == "greeks_enrich.spot_invalid" for e in logs)

    def test_negative_spot_treated_as_no_spot(self) -> None:
        bars, _stats = _enrich_rows([_row()], spot_by_ts={_BAR_TS: Decimal("-5")})
        assert _status_of(bars[0]) == STATUS_NO_SPOT

    def test_itm_zero_close_is_bad_input_not_below_intrinsic(self) -> None:
        # Deep ITM (floor ≈ 6560 > 0): a zero close is a BAD INPUT, not a
        # price below parity — the two stamps used to split by moneyness.
        row = _row(close=0.0)
        bars, _stats = _enrich_rows([row], symbol="NSE-NIFTY-25Jan24-15000-CE")
        assert _status_of(bars[0]) == STATUS_BAD_INPUT

    def test_negative_close_is_bad_input_everywhere(self) -> None:
        for symbol in ("NSE-NIFTY-25Jan24-15000-CE", "NSE-NIFTY-25Jan24-24000-CE"):
            bars, _stats = _enrich_rows([_row(close=-5.0)], symbol=symbol)
            assert _status_of(bars[0]) == STATUS_BAD_INPUT

    def test_subnormal_close_still_reaches_kernel_reject(self) -> None:
        # A positive subnormal passes the zero/negative pre-check and the
        # floor screen (OTM floor 0), so the kernel's caller-contract reject
        # still stamps BAD_INPUT (the except GreeksInputError arm stays live).
        bars, _stats = _enrich_rows([_row(close=1e-310)], symbol="NSE-NIFTY-25Jan24-24000-CE")
        assert _status_of(bars[0]) == STATUS_BAD_INPUT
