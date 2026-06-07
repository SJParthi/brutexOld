"""Golden byte-equality test for the Stage-4 cascade wiring (DEC-CASCADE-WIRE-001).

The headline contract of this PR: the AUTOMATIC cascade (run inside
``brutex sweep-all`` right after the grid) and the MANUAL replay
(``brutex cascade <run-id>`` / the dashboard "Run cascade" button) produce a
**byte-identical** ``cascade-report.json``. Both paths funnel through the SAME
pure core (:func:`brutex.engine.cascade_wire.run_cascade_over_specs`); the only
difference is auto holds the specs in memory while manual replays them from the
manifest it wrote. This test pins that equality on disk.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from brutex.data.models import CandleBar
from brutex.engine import cascade_wire as cw

_IST = timezone(timedelta(hours=5, minutes=30))


def _bar(minute: int, o: str, h: str, lo: str, c: str) -> CandleBar:
    ts = datetime(2026, 5, 13, 9, minute, tzinfo=_IST).astimezone(UTC)
    return CandleBar(
        timestamp=ts,
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(lo),
        close=Decimal(c),
        volume=100,
        open_interest=None,
    )


def _bars() -> list[CandleBar]:
    return [
        _bar(16, "100.00", "101.00", "99.50", "100.50"),
        _bar(17, "100.50", "102.00", "100.00", "101.50"),
        _bar(18, "101.50", "101.80", "100.20", "100.40"),
        _bar(19, "100.40", "100.90", "99.80", "100.10"),
        _bar(20, "100.10", "100.30", "99.00", "99.20"),
        _bar(21, "99.20", "99.80", "98.50", "98.70"),
    ]


def _bars_for(bars: list[CandleBar]):
    def loader(instrument: str, tf_label: str) -> list[CandleBar]:
        return bars if (instrument, tf_label) == ("NIFTY", "1minute") else []

    return loader


def _specs() -> list[cw.CascadeTupleSpec]:
    return [
        cw.CascadeTupleSpec(
            instrument="NIFTY",
            tf_label="1minute",
            tf_minutes=1,
            direction_label="bull",
            sl_paisa=50,
            tp_paisa=80,
            prior_n=3,
            proximity_bps=None,
            start_equity_paisa=10_000_000,
            winners=((f"{1:064x}", 0), (f"{2:064x}", 1)),
        ),
        cw.CascadeTupleSpec(
            instrument="NIFTY",
            tf_label="1minute",
            tf_minutes=1,
            direction_label="bear",
            sl_paisa=None,
            tp_paisa=None,
            prior_n=2,
            proximity_bps=(10, 20, 30),
            start_equity_paisa=10_000_000,
            winners=((f"{3:064x}", 0),),
        ),
    ]


def test_auto_path_equals_manual_path_byte_for_byte(tmp_path: Path) -> None:
    sweep_id = "deadbeef" * 8
    bars_for = _bars_for(_bars())
    config = cw.cascade_config_for_sweep(sweep_id)
    ctx = cw.BarsContext(exchange="NSE", segment="INDEX", start="2021-01-01", end="2021-06-01")

    cw.run_and_write_from_specs(
        tmp_path,
        sweep_id=sweep_id,
        specs=_specs(),
        bars_for=bars_for,
        bars_context=ctx,
        cascade_config=config,
    )
    auto_bytes = cw.report_path(tmp_path).read_bytes()

    # MANUAL replay reloads ONLY the manifest the auto path wrote.
    cw.run_and_write_from_manifest(tmp_path, bars_for=bars_for)
    manual_bytes = cw.report_path(tmp_path).read_bytes()

    assert manual_bytes == auto_bytes


def test_manifest_round_trip_is_byte_stable(tmp_path: Path) -> None:
    manifest = cw.CascadeManifest(
        version=cw.MANIFEST_VERSION,
        sweep_id="deadbeef" * 8,
        cascade_config=cw.cascade_config_for_sweep("deadbeef" * 8),
        bars_context=cw.BarsContext(
            exchange="NSE", segment="INDEX", start="2021-01-01", end="2021-06-01"
        ),
        specs=tuple(_specs()),
    )
    first = cw.manifest_to_json(manifest)
    second = cw.manifest_to_json(cw.manifest_from_json(first))
    assert first == second
