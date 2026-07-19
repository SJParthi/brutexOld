"""End-to-end proof of the sweep-all results-depth run-end wiring (WP6).

DEC-SWEEP-RESULTS-DEPTH-001. ``cmd_sweep_all`` is ``# pragma: no cover`` (an
orchestration shim over the 100%-tested engine), so — exactly like
``test_cli_sweep_all_dedup.py``, the precedent suite — this is the HONEST
functional validation of the P3 integration adapter's production wiring: a
tiny REAL lake is persisted with the same writers ingest uses and
``cli.cmd_sweep_all`` is driven for real, proving

* the run-end pass FIRES post-cascade (output-order pinned) and writes the
  contract artifacts (``sweep_runs/<grid_identity>/top100.json`` + stamp and
  the per-tuple sidecar trio, ALL under the Ruling-4 ``sweep_runs/`` SIBLING
  root — never inside the ``**/*.parquet``-globbed sweeps tree) with the WP3
  providers wired;
* the NSE-INDIAVIX lake coordinate is REAL (Ruling 7): bars written through
  the PRODUCTION writer at the CLI's own VIX coordinates are found by the
  depth loader and stamped onto every captured trade;
* ``BRUTEX_RESULTS_DEPTH=0`` skips the pass loudly (no depth artifacts,
  sweep exit unchanged);
* a JUNK switch value is a clean PRE-sweep ``EXIT_USER_ERROR`` (nothing ran,
  no false abort alert);
* a pass failure is FENCED — loud stderr WARNING, sweep still exits 0, no
  halt alert (the failure is rebuildable, never masked, never fatal).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from brutex import cli
from brutex.data.catalog import Catalog
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.sink import ParquetBarSink
from brutex.engine import results_depth_pass as results_depth_pass_mod

pytest.importorskip("polars")
pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")
pytest.importorskip("lmdb")

_IST = timezone(timedelta(hours=5, minutes=30))
_EXCHANGE = "NSE"
_SEGMENT = "INDEX"
_SYMBOL = "NIFTY"
_INTERVAL = "1minute"


def _bars() -> list[CandleBar]:
    """A short in-session NIFTY minute window (stored UTC), valid OHLC."""
    out: list[CandleBar] = []
    for i in range(8):
        ts = datetime(2024, 1, 2, 9, 16 + i, tzinfo=_IST).astimezone(UTC)
        out.append(
            CandleBar(
                timestamp=ts,
                open=Decimal("100.00"),
                high=Decimal("102.00"),
                low=Decimal("99.00"),
                close=Decimal("100.00") + Decimal(i) / 10,
                volume=1000,
                open_interest=None,
            )
        )
    return out


def _persist(tmp_path: Path) -> tuple[Path, Path]:
    """Write the NIFTY bars to a temp lake; return (lake_root, catalog_path)."""
    lake_root = tmp_path / "lake"
    catalog_path = tmp_path / "catalog.duckdb"
    writer = ParquetWriter(lake_root=lake_root)
    with Catalog.open(catalog_path) as cat:
        sink = ParquetBarSink(writer=writer, catalog=cat)
        sink.append(
            exchange=_EXCHANGE,
            segment=_SEGMENT,
            symbol=_SYMBOL,
            interval=_INTERVAL,
            bars=_bars(),
        )
    return lake_root, catalog_path


def _trading_bars() -> list[CandleBar]:
    """The proven trade-PRODUCING fixture (the depth unit suite's 12-day
    GAP-UP drift, verbatim shape — DEC-FILL-WORST-CASE-ONLY-001: a worst-case
    entry fills at the bar HIGH, so the old ±0.5-pad drift SL'd every entry
    on its own bar; narrow gap-up bars rank at sl=80/tp=50) — the tiny 8-bar
    lake above persists zero trades, which cannot exercise VIX stamping."""
    out: list[CandleBar] = []
    cycle = 0
    for d in range(12):
        price = 100.0
        for m in range(120):
            k = m % 7
            if k == 0:
                cycle += 1
            if k == 3:  # shallow bearish bar (below-EMA bits fire)
                o = price
                close, hi, lo = o - 0.3, o + 0.1, o - 0.4
            elif k == 4 and cycle % 4 == 0:  # deep bearish loss carrier
                o = price
                close, hi, lo = o - 0.65, o + 0.1, o - 0.75
            else:  # narrow gap-up win bar
                o = price + 3.0
                close, hi, lo = o + 0.3, o + 0.4, o - 0.1
            ts = (
                datetime(2026, 1, 5, 9, 16, tzinfo=_IST) + timedelta(days=d, minutes=m)
            ).astimezone(UTC)
            out.append(
                CandleBar(
                    timestamp=ts,
                    open=Decimal(str(round(o, 2))),
                    high=Decimal(str(round(hi, 2))),
                    low=Decimal(str(round(lo, 2))),
                    close=Decimal(str(round(close, 2))),
                    volume=100,
                    open_interest=None,
                )
            )
            price = close
    return out


def _vix_bars_like(bars: list[CandleBar]) -> list[CandleBar]:
    """A synthetic India-VIX series on the SAME minute grid as *bars*."""
    out: list[CandleBar] = []
    for i, bar in enumerate(bars):
        level = Decimal(str(round(14.0 + (i % 40) / 10.0, 2)))
        out.append(
            CandleBar(
                timestamp=bar.timestamp,
                open=level,
                high=level,
                low=level,
                close=level,
                volume=0,
                open_interest=None,
            )
        )
    return out


def _persist_trading_lake(tmp_path: Path, *, with_vix: bool) -> tuple[Path, Path]:
    """Persist the trade-producing NIFTY bars (+ optionally INDIAVIX bars at
    the CLI's OWN VIX coordinates) through the PRODUCTION writer — never a
    hand-built path string (Ruling 7 proof)."""
    lake_root = tmp_path / "lake"
    catalog_path = tmp_path / "catalog.duckdb"
    writer = ParquetWriter(lake_root=lake_root)
    bars = _trading_bars()
    with Catalog.open(catalog_path) as cat:
        sink = ParquetBarSink(writer=writer, catalog=cat)
        sink.append(
            exchange=_EXCHANGE,
            segment=_SEGMENT,
            symbol=_SYMBOL,
            interval=_INTERVAL,
            bars=bars,
        )
        if with_vix:
            sink.append(
                exchange=cli._RESULTS_DEPTH_VIX_EXCHANGE,
                segment=cli._RESULTS_DEPTH_VIX_SEGMENT,
                symbol=cli._RESULTS_DEPTH_VIX_SYMBOL,
                interval=_INTERVAL,
                bars=_vix_bars_like(bars),
            )
    return lake_root, catalog_path


#: The sweep args matching the trade-producing fixture (the unit grid shape).
_TRADING_SWEEP_ARGS: dict[str, Any] = {
    "start": "2026-01-05",
    "end": "2026-01-17",
    "k": "1",
    "conditions": "0,1,2,3,4",
    "sl": "80",
    "tp": "50",
}


def _sweep_all(lake_root: Path, catalog_path: Path, results_root: Path, **overrides: Any) -> int:
    """Drive ``cli.cmd_sweep_all`` over the tiny persisted lake."""
    kwargs: dict[str, Any] = {
        "lake_root": lake_root,
        "catalog_path": catalog_path,
        "results_root": results_root,
        "exchange": _EXCHANGE,
        "segment": _SEGMENT,
        "symbols": _SYMBOL,
        "intervals": _INTERVAL,
        "start": "2024-01-02",
        "end": "2024-01-03",
        "directions": "BUY",
        "k": "2",
        "conditions": "0,1,2,3",  # C(4,2) = 6 combos → a real multi-combo sweep
        "sl": "none",
        "tp": "none",
        "top": 10,
        "mode": "A",
        "max_tuples": None,
        "max_combos": None,
        "data_snapshot_hash": None,
        "options_sim": False,
    }
    kwargs.update(overrides)
    return cli.cmd_sweep_all(**kwargs)


@pytest.fixture(autouse=True)
def _sequential_no_env_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRUTEX_SWEEP_WORKERS", "1")
    monkeypatch.delenv("BRUTEX_RESULTS_DEPTH", raising=False)


def test_results_depth_fires_post_cascade_and_writes_the_contract_artifacts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Default ON: the pass runs AFTER the sweep+cascade output and lands
    top100.json + stamp at the binding contract path plus the sidecar trio."""
    lake_root, catalog_path = _persist(tmp_path)
    results_root = tmp_path / "sweeps"

    rc = _sweep_all(lake_root, catalog_path, results_root)

    assert rc == 0
    out = capsys.readouterr().out
    assert "results-depth — " in out
    # POST-cascade law: the depth report prints strictly after the sweep
    # completion line (and after the cascade line whenever a cascade ran).
    assert out.index("results-depth — ") > out.index("sweep-all done")
    if "cascade — " in out:
        assert out.index("results-depth — ") > out.index("cascade — ")

    # Ruling 4: run artifacts live in the sweep_runs/ SIBLING root beside
    # the sweeps tree — never inside the **/*.parquet-globbed tree.
    runs_dir = results_root.parent / "sweep_runs"
    run_dirs = [p for p in runs_dir.iterdir() if p.is_dir()]
    assert len(run_dirs) == 1  # ONE grid identity for this run
    artifact = run_dirs[0] / "top100.json"
    stamp = run_dirs[0] / "top100.stamp.json"
    assert artifact.exists() and stamp.exists()
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    # The WP3 providers were wired by the adapter — honesty header proves it.
    assert payload["source"]["reasons_wired"] is True
    assert payload["source"]["margin_roi_wired"] is True
    assert payload["run_id"] == run_dirs[0].name
    # The per-tuple sidecar trio landed under the run dir…
    depth_dir = run_dirs[0] / "results-depth"
    assert list(depth_dir.glob("*/*/*.trades.parquet"))
    assert list(depth_dir.glob("*/*/*.aggs.parquet"))
    assert list(depth_dir.glob("*/*/*.stamp.json"))
    # …and NOTHING results-depth sits inside the sweeps tree (results_report
    # + sweep_verify recursively glob every *.parquet under it).
    assert not [
        p
        for p in results_root.rglob("*.parquet")
        if p.name.endswith((".trades.parquet", ".aggs.parquet", ".equity.parquet"))
    ]


def test_vix_lake_coordinate_is_real_and_stamps_every_captured_trade(
    tmp_path: Path,
) -> None:
    """Ruling 7 coordinate proof: the NSE-INDIAVIX lake coordinate is REAL.

    India-VIX bars are written through the PRODUCTION writer/path-builder
    (``ParquetBarSink`` — never a hand-built string) at the CLI's OWN
    ``_RESULTS_DEPTH_VIX_*`` coordinates; the depth loader must FIND them and
    the pass must STAMP them onto every captured trade — the dormant-feature
    trap (a coordinate typo silently NULL-ing every VIX field) is closed by
    this end-to-end pin.
    """
    lake_root, catalog_path = _persist_trading_lake(tmp_path, with_vix=True)
    results_root = tmp_path / "sweeps"

    rc = _sweep_all(lake_root, catalog_path, results_root, **_TRADING_SWEEP_ARGS)
    assert rc == 0

    # (1) The production loader resolves the CLI's own VIX coordinates.
    loader = cli._depth_reference_loader(
        slices={},
        lake_root=lake_root,
        catalog_path=catalog_path,
        exchange=cli._RESULTS_DEPTH_VIX_EXCHANGE,
        segment=cli._RESULTS_DEPTH_VIX_SEGMENT,
        start=_TRADING_SWEEP_ARGS["start"],
        end=_TRADING_SWEEP_ARGS["end"],
        interval="1minute",
    )
    vix_bars = loader(cli._RESULTS_DEPTH_VIX_SYMBOL)
    assert vix_bars, "the INDIAVIX lake coordinate must resolve to real stored bars"
    assert [bar.close for bar in vix_bars] == [bar.close for bar in _vix_bars_like(_trading_bars())]

    # (2) The sweep's own depth artifacts carry the stamps — never NULL.
    from brutex.engine.trade_store import SidecarPaths, read_trade_page

    (run_dir,) = [p for p in (results_root.parent / "sweep_runs").iterdir() if p.is_dir()]
    stamp_paths = sorted(
        p
        for p in run_dir.glob("results-depth/*/*/*.stamp.json")
        if not p.name.endswith(".resilience.stamp.json")  # exclude the sibling resilience stamp
    )
    assert stamp_paths
    stamped_rows = 0
    for stamp_path in stamp_paths:
        stem = stamp_path.name[: -len(".stamp.json")]
        base = stamp_path.parent
        paths = SidecarPaths(
            trades=base / f"{stem}.trades.parquet",
            aggs=base / f"{stem}.aggs.parquet",
            equity=base / f"{stem}.equity.parquet",
            stamp=stamp_path,
            resilience=base / f"{stem}.resilience.parquet",
            resilience_stamp=base / f"{stem}.resilience.stamp.json",
        )
        spans = json.loads(stamp_path.read_text(encoding="utf-8"))["trade_spans"]
        for run_id, (_start_row, count) in spans.items():
            if count == 0:
                continue
            page = read_trade_page(paths, run_id=run_id, offset=0, limit=500)
            assert page.rows
            for row in page.rows:
                assert row["vix_entry"] is not None
                assert row["vix_exit"] is not None
                stamped_rows += 1
    assert stamped_rows > 0  # real trades really carried the stamp


def test_absent_vix_partition_warns_capped_vix_miss_and_nulls_honestly(
    tmp_path: Path,
) -> None:
    """No INDIAVIX partition in the lake → the pass degrades to honest NULLs
    with the LOUD (capped) ``results_depth.vix_miss`` per-combo WARN + the
    one-shot ``results_depth.vix_absent`` — never a silent zero."""
    import structlog.testing

    # NIFTY only — no INDIAVIX partition in the lake.
    lake_root, catalog_path = _persist_trading_lake(tmp_path, with_vix=False)
    results_root = tmp_path / "sweeps"

    with structlog.testing.capture_logs() as logs:
        rc = _sweep_all(lake_root, catalog_path, results_root, **_TRADING_SWEEP_ARGS)
    assert rc == 0
    events = [log["event"] for log in logs]
    assert "results_depth.vix_absent" in events
    assert "results_depth.vix_miss" in events  # the Ruling-7 capped WARN fired


def test_kill_switch_skips_the_pass_loudly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """BRUTEX_RESULTS_DEPTH=0: sweep completes, pass skipped, ZERO depth artifacts."""
    lake_root, catalog_path = _persist(tmp_path)
    results_root = tmp_path / "sweeps"
    monkeypatch.setenv("BRUTEX_RESULTS_DEPTH", "0")

    rc = _sweep_all(lake_root, catalog_path, results_root)

    assert rc == 0
    out = capsys.readouterr().out
    assert "sweep-all done" in out
    assert "results-depth: disabled via BRUTEX_RESULTS_DEPTH" in out
    assert "results-depth — " not in out  # the pass never ran
    assert not (results_root.parent / "sweep_runs").exists()


def test_junk_kill_switch_is_a_clean_pre_sweep_user_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A junk BRUTEX_RESULTS_DEPTH refuses BEFORE any compute — fail-loud,
    never a surprise after a multi-day sweep; never a false abort alert."""
    lake_root, catalog_path = _persist(tmp_path)
    results_root = tmp_path / "sweeps"
    fired: list[Exception] = []
    monkeypatch.setattr(cli, "_emit_pull_halt_alert", fired.append)
    monkeypatch.setenv("BRUTEX_RESULTS_DEPTH", "maybe")

    rc = _sweep_all(lake_root, catalog_path, results_root)

    assert rc == cli.EXIT_USER_ERROR
    captured = capsys.readouterr()
    assert "BRUTEX_RESULTS_DEPTH" in captured.err
    assert "sweep-all done" not in captured.out  # the sweep never launched …
    assert not (results_root / "audit").exists()  # … and wrote nothing
    assert fired == []  # a config error is not an abort — never a false alert


def test_pass_failure_is_fenced_and_never_fails_the_sweep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A defect inside the pass prints LOUD + logs and leaves the sweep exit
    path byte-unchanged (exit 0, completion lines intact, no halt alert)."""
    lake_root, catalog_path = _persist(tmp_path)
    results_root = tmp_path / "sweeps"
    fired: list[Exception] = []
    monkeypatch.setattr(cli, "_emit_pull_halt_alert", fired.append)

    def _boom(**_kwargs: Any) -> Any:
        raise RuntimeError("depth pass wedged")

    # The adapter imports the pass at call time from its source module.
    monkeypatch.setattr(results_depth_pass_mod, "run_results_depth_pass", _boom)

    rc = _sweep_all(lake_root, catalog_path, results_root)

    assert rc == 0
    captured = capsys.readouterr()
    assert "sweep-all done" in captured.out
    assert "results-depth pass FAILED (RuntimeError: depth pass wedged)" in captured.err
    assert "sweep itself SUCCEEDED" in captured.err
    assert fired == []  # fenced — never escalated to a sweep abort alert
