"""Integration — the sweep-all DERIVED-1DAY freshness gate (R2N-03, DEPEND-on-#1342).

The derived ``1day`` partition is the PDH/PDL/pivot/Fib day anchor (bits
6-29+48-51). A stored ``1day`` that drifted from a fresh re-derivation of its
stored 1-minute base (a 1m backfill after the last prebuild) would feed the
engine STALE daily anchors AND hash them into the run identity as canonical
(Golden Rule #4). At HEAD the sweep-launch freshness gate keys ONLY the intraday
derived timeframes (``tf_autoderive.lake_derived_freshness_verdicts`` iterates
``INTRADAY_TF_INTERVALS`` and ``sweep_preflight._FETCHED_INTERVALS`` still lists
``"1day"`` as fetched), so a stale derived ``1day`` sails through.

The 1day arm is #1342's deliverable (``claude/movers-stored-freshness-guard``,
DEC-MOVERS-STORED-FRESHNESS-001): a ``compare_daily_freshness`` midnight-IST twin
+ ``lake_derived_freshness_verdicts(daily_before=...)`` keying ``(symbol,
"1day")`` + the ``sweep_preflight`` refusal. Because ``cmd_sweep_all`` already
passes the FULL requested interval list into the verdicts call, the 1day arm
activates at the sweep gate with ZERO cli change the moment #1342 merges.

PR C ships ZERO production code for this item — this is the stale-1day
sweep-launch REFUSAL pin, SKIPPED until #1342's seam lands (detected by the
``daily_before`` parameter on ``lake_derived_freshness_verdicts``). When #1342
merges this file activates and pins the refusal; it is greenfield vs #1342's own
hunks (conflict-free by construction).
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("polars")
pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")
pytest.importorskip("lmdb")

from brutex.cli import EXIT_OK, EXIT_USER_ERROR, cmd_sweep_all
from brutex.data.catalog import Catalog
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.sink import ParquetBarSink
from brutex.data.tf_autoderive import lake_derived_freshness_verdicts

_IST = timezone(timedelta(hours=5, minutes=30))
_EXCHANGE = "NSE"
_SEGMENT = "INDEX"

# #1342 seam detection: its `lake_derived_freshness_verdicts` grows a
# `daily_before` parameter (the midnight-IST daily-comparator anchor). Absent at
# HEAD ⇒ the 1day arm does not exist yet ⇒ skip (test-only DEPEND-on-#1342).
# NOT a module-wide pytestmark: `test_1342_seam_tripwire` below must ALWAYS run
# so a 1day arm landing under a DIFFERENT parameter name fails LOUDLY instead of
# skipping this suite forever (review round 1 item 6).
_HAS_1DAY_ARM = "daily_before" in inspect.signature(lake_derived_freshness_verdicts).parameters

_skip_until_1342 = pytest.mark.skipif(
    not _HAS_1DAY_ARM,
    reason="the derived-1day freshness arm lands with #1342 (DEC-MOVERS-STORED-FRESHNESS-001)",
)


@pytest.fixture(autouse=True)
def _sequential_sweep_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRUTEX_SWEEP_WORKERS", "1")


def _minute_bars() -> list[CandleBar]:
    """Six sessions of 1-minute NIFTY bars (Jan 2-9 2024, weekdays)."""
    out: list[CandleBar] = []
    for day in (2, 3, 4, 5, 8, 9):
        price = 100.0
        for m in range(40):
            close = price + 0.6 if m % 3 else price - 0.3
            ts = datetime(2024, 1, day, 9, 16, tzinfo=_IST) + timedelta(minutes=m)
            hi = max(price, close) + 0.5
            lo = min(price, close) - 0.5
            out.append(
                CandleBar(
                    timestamp=ts.astimezone(UTC),
                    open=Decimal(str(round(price, 2))),
                    high=Decimal(str(round(hi, 2))),
                    low=Decimal(str(round(lo, 2))),
                    close=Decimal(str(round(close, 2))),
                    volume=1000,
                    open_interest=None,
                )
            )
            price = close
    return out


def _seed(tmp_path: Path, *, daily: str) -> tuple[Path, Path]:
    """Seed 1minute NIFTY bars + a derived 1day partition (fresh|stale)."""
    from brutex.data.spot_daily_resample import resample_1m_to_daily
    from brutex.data.tf_autoderive import read_lake_bars_frame

    lake_root = tmp_path / "lake"
    catalog_path = tmp_path / "catalog.duckdb"
    writer = ParquetWriter(lake_root=lake_root)
    with Catalog.open(catalog_path) as cat:
        sink = ParquetBarSink(writer=writer, catalog=cat)
        sink.append(
            exchange=_EXCHANGE,
            segment=_SEGMENT,
            symbol="NIFTY",
            interval="1minute",
            bars=_minute_bars(),
        )
        symbol_dir = lake_root / "bars" / _EXCHANGE / _SEGMENT / "NIFTY"
        df_1m = read_lake_bars_frame(symbol_dir, "1minute")
        # resample_1m_to_daily already yields list[CandleBar] (a latent bug in
        # this previously-always-skipped helper wrapped it in
        # frame_to_candle_bars — caught the moment the round-1 tripwire made
        # this module actually EXECUTE; review round 1 item 6).
        derived = resample_1m_to_daily(df_1m, before=datetime(2024, 1, 20).date())
        if daily == "stale":
            # The drift shape: bump one stored daily close (high raised with it —
            # CandleBar enforces low <= close <= high) so it no longer matches a
            # fresh re-derivation from the stored 1-minute base. The ordering fix
            # is another latent defect in this previously-always-skipped helper,
            # caught when the round-1 tripwire made it execute (item 6).
            from dataclasses import replace

            derived = list(derived)
            stale_close = derived[0].close + Decimal("5.00")
            derived[0] = replace(
                derived[0], close=stale_close, high=max(derived[0].high, stale_close)
            )
        sink.append(
            exchange=_EXCHANGE,
            segment=_SEGMENT,
            symbol="NIFTY",
            interval="1day",
            bars=derived,
        )
    return lake_root, catalog_path


def _kwargs(lake_root: Path, catalog_path: Path, tmp_path: Path) -> dict[str, Any]:
    return {
        "lake_root": lake_root,
        "catalog_path": catalog_path,
        "results_root": tmp_path / "sweeps",
        "exchange": _EXCHANGE,
        "segment": _SEGMENT,
        "symbols": "NIFTY",
        "intervals": "1day",
        "start": "2024-01-02",
        "end": "2024-01-10",
        "directions": "BUY",
        "k": "1",
        "conditions": "0,1",
        "sl": "none",
        "tp": "none",
        "top": 5,
        "mode": "A",
        "max_tuples": None,
        "max_combos": None,
        "data_snapshot_hash": None,
        "options_sim": False,
    }


@_skip_until_1342
def test_stale_derived_1day_refuses_launch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A stale stored derived 1day REFUSES the sweep, naming (NIFTY, 1day)."""
    lake_root, catalog_path = _seed(tmp_path, daily="stale")
    rc = cmd_sweep_all(**_kwargs(lake_root, catalog_path, tmp_path))
    err = capsys.readouterr().err
    assert rc == EXIT_USER_ERROR
    assert "REFUSED" in err
    assert "NIFTY/1day" in err


@_skip_until_1342
def test_fresh_derived_1day_launches(tmp_path: Path) -> None:
    """A byte-fresh derived 1day passes the gate — the sweep runs (EXIT_OK)."""
    lake_root, catalog_path = _seed(tmp_path, daily="fresh")
    rc = cmd_sweep_all(**_kwargs(lake_root, catalog_path, tmp_path))
    assert rc == EXIT_OK


def test_1342_seam_tripwire(tmp_path: Path) -> None:
    """Fail-loud tripwire (review round 1 item 6) — NEVER skipped.

    The skipif above keys on the #1342 ``daily_before`` parameter NAME. If the
    1day freshness arm lands under a DIFFERENT name, the gate tests above would
    silently skip FOREVER while the arm ships untested. So while the skip
    condition holds, functionally PROVE the arm is still absent: a genuinely
    STALE stored derived 1day must yield ZERO verdicts keyed ("NIFTY", "1day").
    An arm landing under ANY name/mechanism produces a verdict here and fails
    this test — forcing the detection constant + the gate tests above to be
    reactivated together.
    """
    if _HAS_1DAY_ARM:
        pytest.skip("the #1342 `daily_before` seam landed — the gate tests above are active")
    lake_root, _catalog_path = _seed(tmp_path, daily="stale")
    verdicts = lake_derived_freshness_verdicts(
        lake_root,
        exchange=_EXCHANGE,
        segment=_SEGMENT,
        symbols=["NIFTY"],
        intervals=["1day"],
    )
    assert ("NIFTY", "1day") not in verdicts, (
        "a derived-1day freshness arm has landed (under a different name than "
        "#1342's `daily_before`) — update _HAS_1DAY_ARM's detection and activate "
        "the stale-1day refusal tests in this file"
    )
