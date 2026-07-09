"""End-to-end proof of ``brutex sweep-all``'s cooperative-cancel wiring.

DEC-JOB-COOPERATIVE-CANCEL-001 review fold (the #1524 pragma-lift precedent):
``cmd_sweep_all`` is ``# pragma: no cover`` (an orchestration shim over the
100%-tested engine), and its NEW cancel wiring — the launch-time stale-sidecar
clear, the ``EXIT_SWEEP_CANCELLED=7`` verdict with precedence over
``tuples_failed``, the ``clear_cancel`` consumption of the request, and the
skip of every post-grid pass — previously rode that pragma with ZERO executing
tests (a false coverage claim in the ledger, since corrected). This is the
HONEST functional validation: a tiny lake persisted with the same writers
ingest uses, ``cli.cmd_sweep_all`` driven for real.

* **Stale-sidecar clear at LAUNCH.** A pre-planted ``.sweep_cancel.json`` is
  cleared before the grid starts — a leftover request from a prior cancelled
  run never kills a fresh/resumed run (exit != 7, the run completes).
* **Mid-run cancel.** A sidecar planted at the tuple boundary (via the real
  ``on_tuple`` hook) stops the grid cooperatively: exit ``7``
  (``EXIT_SWEEP_CANCELLED``), the consumed sidecar is CLEARED (a resume is
  never re-cancelled by the stale request), and EVERY post-grid pass is
  skipped (the stage3 pass is poisoned to prove it never runs).
"""

from __future__ import annotations

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
from brutex.engine import sweep_supervisor
from brutex.engine.sweep_cancel import cancel_sidecar_path, request_cancel

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


def _poison_stage3(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail LOUDLY if the post-grid stage3 pass runs (the cancel must skip it)."""
    from brutex.engine import stage3_wire

    def _never(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a cancelled sweep must SKIP the post-grid stage3 pass")

    monkeypatch.setattr(stage3_wire, "run_stage3_pass", _never)


def test_stale_cancel_sidecar_is_cleared_and_the_run_completes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A pre-planted (stale) sidecar never kills a fresh run — cleared at launch."""
    lake_root, catalog_path = _persist(tmp_path)
    results_root = tmp_path / "sweeps"
    monkeypatch.setenv("BRUTEX_SWEEP_WORKERS", "1")
    # Plant the STALE request from a "prior cancelled run".
    request_cancel(results_root, source="prior-run")
    assert cancel_sidecar_path(results_root).exists()

    rc = _sweep_all(lake_root, catalog_path, results_root)

    out = capsys.readouterr().out
    assert rc == 0, "a stale sidecar must never cancel a fresh run"
    assert rc != cli.EXIT_SWEEP_CANCELLED
    assert "cleared a STALE cancel request" in out
    assert "sweep-all done" in out
    assert not cancel_sidecar_path(results_root).exists()


def test_mid_run_cancel_exits_7_consumes_sidecar_and_skips_post_grid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A cancel planted at the tuple boundary → exit 7, sidecar consumed, post-grid skipped.

    ``k="2,3"`` yields TWO tuples; the REAL ``on_tuple`` hook (wrapped through a
    spy on the sequential driver) plants the sidecar after tuple 1 completes, so
    the supervisor's ``should_stop`` probe fires at tuple 2's boundary — the
    production cancel path end-to-end, no engine internals faked.
    """
    lake_root, catalog_path = _persist(tmp_path)
    results_root = tmp_path / "sweeps"
    monkeypatch.setenv("BRUTEX_SWEEP_WORKERS", "1")
    _poison_stage3(monkeypatch)

    real = sweep_supervisor.run_supervised_grid

    def _spy(*args: Any, **kwargs: Any) -> Any:
        inner = kwargs.get("on_tuple")

        def _on_tuple(*a: Any, **k: Any) -> Any:
            # The operator's Stop lands while tuple 1 is finishing: the durable
            # sidecar appears BEFORE the next tuple-boundary probe.
            request_cancel(results_root, source="test-operator")
            return inner(*a, **k) if inner is not None else None

        kwargs["on_tuple"] = _on_tuple
        return real(*args, **kwargs)

    # cmd_sweep_all imports run_supervised_grid locally from sweep_supervisor,
    # so patching the source module attribute is picked up at call time.
    monkeypatch.setattr(sweep_supervisor, "run_supervised_grid", _spy)

    rc = _sweep_all(lake_root, catalog_path, results_root, k="2,3")

    out = capsys.readouterr().out
    assert rc == cli.EXIT_SWEEP_CANCELLED == 7
    assert "CANCELLED at operator request" in out
    assert "post-grid" in out and "skipped" in out
    # The consumed request is cleared — a resume is never re-cancelled by it.
    assert not cancel_sidecar_path(results_root).exists()
    # The clean summary line never printed (the cancel returned before it).
    assert "sweep-all done" not in out


def test_parallel_run_publishes_the_progress_bridge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The PARALLEL arm feeds the /api/v1/sweep-progress file bridge (review fold).

    Fails-before: ``cmd_sweep_all`` wired ``on_progress=_on_progress`` (→
    ``publish_progress``) on the SEQUENTIAL arm only; ``run_parallel_grid`` took
    no hook, so the flagship multi-day parallel sweep never wrote
    ``<results_root>/progress/sweep-progress.json`` — the dashboard's Stop
    button (keyed on the bridge's ``running``) never showed. A real 2-worker
    spawn run must now land the bridge file with the honest ``done`` terminal
    AND the ``wall_time`` staleness stamp (the SIGKILL phantom decay's key).
    """
    import json

    lake_root, catalog_path = _persist(tmp_path)
    results_root = tmp_path / "sweeps"
    monkeypatch.setenv("BRUTEX_SWEEP_WORKERS", "2")  # explicit env → parallel, verbatim

    rc = _sweep_all(lake_root, catalog_path, results_root)

    out = capsys.readouterr().out
    assert rc == 0
    assert "parallel mode" in out  # the PARALLEL arm actually ran
    bridge = results_root / "progress" / "sweep-progress.json"
    assert bridge.exists(), "the parallel arm must publish the progress bridge"
    parsed = json.loads(bridge.read_text(encoding="utf-8"))
    assert parsed["state"] == "done"
    assert parsed["running"] is False
    assert isinstance(parsed["wall_time"], float)


def test_post_grid_cancel_skips_the_remaining_tail_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A Stop landing AFTER the grid is observed at the next post-grid boundary.

    Fix round 4 (fails-before): the post-grid tail (options-sim → cascade →
    results-depth) previously ran to completion regardless of the sidecar —
    the route kept acking ``{"cancelling": true}`` while the run was blind for
    the whole span. The sidecar is planted DURING stage3 (the real pass runs
    first), so the boundary probe BEFORE options-sim fires: exit 7, the
    consumed sidecar cleared, and the cascade pass (poisoned) never runs.
    """
    lake_root, catalog_path = _persist(tmp_path)
    results_root = tmp_path / "sweeps"
    monkeypatch.setenv("BRUTEX_SWEEP_WORKERS", "1")

    from brutex.engine import cascade_wire, stage3_wire

    real_stage3 = stage3_wire.run_stage3_pass

    def _stage3_then_cancel(*args: Any, **kwargs: Any) -> Any:
        report = real_stage3(*args, **kwargs)
        # The operator's Stop lands while the grid tail settles — AFTER the
        # last tuple-boundary probe, BEFORE the options-sim boundary probe.
        request_cancel(results_root, source="test-operator-post-grid")
        return report

    monkeypatch.setattr(stage3_wire, "run_stage3_pass", _stage3_then_cancel)

    def _never_cascade(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a tail-cancelled sweep must SKIP the cascade pass")

    monkeypatch.setattr(cascade_wire, "build_stage2_cascade_specs", _never_cascade)

    rc = _sweep_all(lake_root, catalog_path, results_root)

    out = capsys.readouterr().out
    assert rc == cli.EXIT_SWEEP_CANCELLED == 7
    assert "CANCELLED at operator request" in out
    assert "grid results" in out and "already durable" in out
    # The consumed request is cleared — a resume is never re-cancelled by it.
    assert not cancel_sidecar_path(results_root).exists()
    # The grid itself completed normally (the cancel hit only the tail).
    assert "sweep-all done" in out


def test_cancel_after_last_tail_probe_clears_orphan_sidecar_at_run_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A cancel arriving AFTER the last probe is unobservable — cleared at run end.

    Fix round 4 (fails-before): the orphan sidecar previously survived until
    the NEXT launch's stale-sidecar guard, where a fresh run reported clearing
    "a STALE cancel request" that was really a late one. The sidecar is planted
    inside the results-depth pass (the last post-grid pass, after the final
    between-pass probe); the run completes normally (exit 0), clears the orphan
    loudly, and the sidecar cannot cancel a future run.
    """
    lake_root, catalog_path = _persist(tmp_path)
    results_root = tmp_path / "sweeps"
    monkeypatch.setenv("BRUTEX_SWEEP_WORKERS", "1")

    def _depth_plants_cancel(**kwargs: Any) -> None:
        # The Stop lands mid-results-depth — after the LAST between-pass probe.
        request_cancel(results_root, source="test-operator-too-late")

    monkeypatch.setattr(cli, "_run_results_depth_adapter", _depth_plants_cancel)

    rc = _sweep_all(lake_root, catalog_path, results_root)

    out = capsys.readouterr().out
    assert rc == 0
    assert "sweep-all done" in out
    assert "arrived after the LAST" in out and "orphan sidecar was cleared" in out
    assert not cancel_sidecar_path(results_root).exists()
    assert "CANCELLED at operator request" not in out
