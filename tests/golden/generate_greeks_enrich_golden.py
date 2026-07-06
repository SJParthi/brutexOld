"""Generate the frozen Greeks-enrich golden fixture (DEC-GREEKS-ENGINE-PR2B-001).

Runs the REAL end-to-end enrichment (real temp lake → real window-bounded
readers → the shared Rust kernel → the real ``latest-wins`` write spine) over
a small deterministic scenario and freezes the STORED rows to
``tests/golden/fixtures/greeks_enrich_golden.json``. The paired test
(``test_greeks_enrich_golden.py``) replays the SAME scenario and asserts the
stored bytes match the fixture EXACTLY — the end-to-end twin of the kernel's
``greeks_golden.json`` (per-artifact bit-identity on the Linux x86_64 CI
reference platform, the DEC-GREEKS-ENGINE-PR2-001 envelope).

Deterministic: a re-run over an unchanged kernel + enrichment emits a
byte-identical fixture. Regenerate ONLY on a deliberate
``GREEKS_FORMULA_VERSION`` / convention bump::

    python tests/golden/generate_greeks_enrich_golden.py
"""

from __future__ import annotations

import json
import tempfile
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

_FIXTURE = Path(__file__).parent / "fixtures" / "greeks_enrich_golden.json"

#: The frozen scenario — an ATM CE + an ITM PE over three 1-minute bars
#: (2024-01-15 09:15 IST, ten days to the 25Jan24 expiry), spot 21500, plus a
#: no-spot minute and an at-expiry bar. Small enough to eyeball, wide enough
#: to pin a solved tuple, a stamped NULL, and the boundary stamps.
SPOT = "21500"
CE_SYMBOL = "NSE-NIFTY-25Jan24-21500-CE"
PE_SYMBOL = "NSE-NIFTY-25Jan24-21000-PE"
BASE_TS = datetime(2024, 1, 15, 3, 45, tzinfo=UTC)
NO_SPOT_TS = BASE_TS + timedelta(minutes=10)
AT_EXPIRY_TS = datetime(2024, 1, 25, 9, 59, tzinfo=UTC)  # 15:29 IST on expiry day
WINDOW = (date(2024, 1, 1), date(2024, 1, 26))
CE_CLOSES = ("200", "205.5", "198.25")
PE_CLOSES = ("35.6", "36.15", "34.9")


def build_scenario_lake(lake: Path) -> None:
    """Seed the deterministic spot + option lake the golden freezes."""
    from brutex.data.models import CandleBar
    from brutex.data.parquet_writer import ParquetWriter
    from brutex.data.sink import ParquetBarSink

    def bar(ts: datetime, close: str, *, oi: int | None) -> CandleBar:
        # The synthetic low is floored at the real 0.05 tick: the original
        # `close - 2` fabricated a NEGATIVE low (-0.95) for the 1.05 at-expiry
        # premium bar — corrupt fixture data the read-boundary price-positivity
        # gate now rightly refuses (DEC-STORED-DATA-VALIDATION-001; the frozen
        # fixture was regenerated — ONLY that bar's low moved, -0.95 → 0.05;
        # low is not a Greeks input, so every enriched value is unchanged).
        return CandleBar(
            timestamp=ts,
            open=Decimal(close),
            high=Decimal(close) + 2,
            low=max(Decimal(close) - 2, Decimal("0.05")),
            close=Decimal(close),
            volume=7,
            open_interest=oi,
        )

    sink = ParquetBarSink(writer=ParquetWriter(lake_root=lake))
    spot_bars = [bar(BASE_TS + timedelta(minutes=i), SPOT, oi=None) for i in range(3)]
    spot_bars.append(bar(AT_EXPIRY_TS, SPOT, oi=None))
    sink.append(exchange="NSE", segment="INDEX", symbol="NIFTY", interval="1minute", bars=spot_bars)
    ce_bars = [bar(BASE_TS + timedelta(minutes=i), c, oi=50) for i, c in enumerate(CE_CLOSES)]
    ce_bars.append(bar(NO_SPOT_TS, "199", oi=50))  # a minute with NO spot → stamped NULL
    ce_bars.append(bar(AT_EXPIRY_TS, "1.05", oi=50))  # inside the T-floor → at_expiry
    sink.append(exchange="NSE", segment="FNO", symbol=CE_SYMBOL, interval="1minute", bars=ce_bars)
    pe_bars = [bar(BASE_TS + timedelta(minutes=i), c, oi=25) for i, c in enumerate(PE_CLOSES)]
    sink.append(exchange="NSE", segment="FNO", symbol=PE_SYMBOL, interval="1minute", bars=pe_bars)


def run_scenario(lake: Path) -> dict[str, list[dict[str, object]]]:
    """Enrich the scenario lake and project the STORED rows per contract."""
    import polars as pl
    from brutex.data.greeks_enrich import GreeksEnrichTarget, enrich_greeks_for_targets
    from brutex.options.risk_free import FixedRateSource

    build_scenario_lake(lake)
    targets = [
        GreeksEnrichTarget(exchange="NSE", symbol=s, start=WINDOW[0], end=WINDOW[1])
        for s in (CE_SYMBOL, PE_SYMBOL)
    ]
    report = enrich_greeks_for_targets(
        targets=targets, lake_root=lake, rate_source=FixedRateSource()
    )
    if report.enriched != 2:  # pragma: no cover — generator sanity guard
        msg = f"golden scenario expected 2 enriched targets, got {report!r}"
        raise RuntimeError(msg)
    out: dict[str, list[dict[str, object]]] = {}
    for symbol in (CE_SYMBOL, PE_SYMBOL):
        base = lake / "bars" / "NSE" / "FNO" / symbol / "1minute"
        frame = pl.read_parquet(sorted(str(p) for p in base.rglob("*.parquet"))).sort("timestamp")
        rows: list[dict[str, object]] = []
        for row in frame.iter_rows(named=True):
            row["timestamp"] = row["timestamp"].isoformat()
            rows.append(row)
        out[symbol] = rows
    return out


def main() -> None:
    """Regenerate the frozen fixture (deterministic; byte-identical re-runs)."""
    with tempfile.TemporaryDirectory() as tmp:
        stored = run_scenario(Path(tmp) / "lake")
    _FIXTURE.write_text(json.dumps(stored, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {_FIXTURE}")


if __name__ == "__main__":
    main()
