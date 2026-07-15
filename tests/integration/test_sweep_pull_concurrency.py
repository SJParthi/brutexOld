"""Integration — a pull runs CONCURRENTLY with the sweep COMPUTE phase
(DEC-SWEEP-LOCK-PULL-CONCURRENCY-001).

The operator requirement: the daily spot pull + crossverify→S3 must run
concurrently with a multi-day sweep — never deferred behind it. The CLI law
(G10, DEC-SWEEP-UNKILLABLE-CORE-001) already delivers it: ``cmd_sweep_all``
holds the SHARED lake read-guard (``LOCK_SH`` on the pull single-flight file)
across the bar PRELOAD ONLY; the multi-day compute runs with NO lake lock held,
and run identity (``data_snapshot_hash``) is FROZEN at preload from the bars
memo — the engine never re-reads the lake mid-compute.

THE EXECUTED PROOF (the corruption oracle, mirroring the
``test_sweep_runcore_byte_identity.py`` A/B tree-hash pattern of #1712):

* CONTROL — the REAL ``cmd_sweep_all`` runs solo over a real Parquet lake.
* TREATMENT — the SAME grid over an identically-seeded lake; at a
  DETERMINISTIC mid-compute point (the second ``run_one_tuple`` dispatch —
  after tuple 1 persisted, before tuple 2; an injected seam, never a sleep) a
  simulated peer pull (1) takes the REAL ``pull_singleflight_lock`` EX on a
  separate open file description — proving a pull would NOT defer — and
  (2) while holding it, REWRITES the very partition the sweep preloaded
  through the production ``ParquetBarSink`` (extra later-day bars; keep-first
  atomic ``os.replace``).
* The treatment sweep completes cleanly, its result artifacts are
  BYTE-IDENTICAL to the control run (per-tuple stores + sentinels + trials +
  the ``sweep_runs/`` sibling incl. ``top100.json`` + baselines +
  results-depth parquet — state/timestamp surfaces excluded, see
  ``_EXCLUDE_PREFIXES``), the supervisor ``TUPLE_DONE`` content hashes match,
  and the concurrently-written bars LANDED intact in the lake.

Plus the unchanged other side of the law: a pull holding the lake flock EX
during the sweep's PRELOAD still refuses the launch loudly (``LakeBusyError``
— bounded to the preload instant, never the sweep's wall time).
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("polars")
pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")
pytest.importorskip("lmdb")

from brutex.adapters.groww.pull_singleflight import pull_singleflight_lock
from brutex.cli import EXIT_OK, cmd_sweep_all
from brutex.data.catalog import Catalog
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.sink import ParquetBarSink
from brutex.engine import sweep_supervisor
from brutex.engine.sweep_audit import read_entries
from brutex.engine.sweep_singleflight import LakeBusyError

_IST = timezone(timedelta(hours=5, minutes=30))
_EXCHANGE = "NSE"
_SEGMENT = "INDEX"

#: State/timestamp surfaces excluded from the byte-identity tree compare —
#: the #1712 ``_PHASE2_PREFIXES`` set plus cmd_sweep_all's own state dirs
#: (checkpoint + dedup-cache carry wall-clock/LMDB internals, `runs/` +
#: `audit/` + `progress/` carry timestamps by design). Everything else —
#: the per-tuple stores, completion sentinels, trial-stats sidecars — is
#: compared strictly; `.stamp.json` sidecars are content-hash stamps with a
#: wall-clock field (additive by design, excluded exactly as #1712 does).
_EXCLUDE_PREFIXES = (
    "runs/",
    ".locks/",
    ".quarantine/",
    "progress/",
    "audit/",
    "checkpoint/",
    "dedup-cache/",
)


@pytest.fixture(autouse=True)
def _sequential_sweep_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the sequential sweep path (ADR-0016 §6) — determinism over the tiny
    grid AND the `run_one_tuple` injection seam is only visible in-process."""
    monkeypatch.setenv("BRUTEX_SWEEP_WORKERS", "1")


def _trending_bars(n_days: int = 3, per_day: int = 50) -> list[CandleBar]:
    """Trending intraday bars (the #1712 byte-identity shape) — enough closed
    trades that combos genuinely pass the MIN_TRADES gate and PERSIST rows,
    so the byte compare has real result content, not empty stores."""
    bars: list[CandleBar] = []
    for d in range(n_days):
        price = 100.0
        for m in range(per_day):
            close = price + 0.6 if m % 3 else price - 0.3
            ts = (
                datetime(2024, 1, 2, 9, 16, tzinfo=_IST) + timedelta(days=d, minutes=m)
            ).astimezone(UTC)
            bars.append(
                CandleBar(
                    timestamp=ts,
                    open=Decimal(str(round(price, 2))),
                    high=Decimal(str(round(max(price, close) + 0.5, 2))),
                    low=Decimal(str(round(min(price, close) - 0.5, 2))),
                    close=Decimal(str(round(close, 2))),
                    volume=100,
                    open_interest=None,
                )
            )
            price = close
    return bars


def _later_day_bars(n: int = 10) -> list[CandleBar]:
    """The concurrent pull's payload: NEW bars for a later day that is INSIDE
    the sweep's requested window (2024-01-05 < the kwargs ``end``) in the SAME
    month partition the sweep preloaded — the strongest rewrite shape: the
    exact file the sweep read is replaced mid-compute, and any engine surface
    that RE-READ the lake instead of serving the preload memo would consume
    these bars and change its artifacts (the anti-vacuous arm below MEASURES
    that sensitivity)."""
    out: list[CandleBar] = []
    for i in range(n):
        ts = datetime(2024, 1, 5, 9, 16 + i, tzinfo=_IST).astimezone(UTC)
        close = Decimal("222.00") + Decimal(i) / 10
        out.append(
            CandleBar(
                timestamp=ts,
                open=close - Decimal("0.05"),
                high=close + Decimal("0.05"),
                low=close - Decimal("0.10"),
                close=close,
                volume=999,
                open_interest=None,
            )
        )
    return out


def _seed(root: Path) -> tuple[Path, Path]:
    """Seed a real lake (production writer + catalog) with 1minute NIFTY bars."""
    lake_root = root / "lake"
    catalog_path = root / "catalog.duckdb"
    writer = ParquetWriter(lake_root=lake_root)
    with Catalog.open(catalog_path) as cat:
        sink = ParquetBarSink(writer=writer, catalog=cat)
        sink.append(
            exchange=_EXCHANGE,
            segment=_SEGMENT,
            symbol="NIFTY",
            interval="1minute",
            bars=_trending_bars(),
        )
    return lake_root, catalog_path


def _kwargs(lake_root: Path, catalog_path: Path, results_root: Path) -> dict[str, Any]:
    return {
        "lake_root": lake_root,
        "catalog_path": catalog_path,
        "results_root": results_root,
        "exchange": _EXCHANGE,
        "segment": _SEGMENT,
        "intervals": "1minute",
        "start": "2024-01-02",
        "end": "2024-01-06",
        "directions": "BUY,SELL",  # TWO tuples — the injection fires between them
        "k": "1",
        "conditions": "0,1,2,3,4",
        "sl": "80",
        "tp": "50",
        "top": 5,
        "mode": "A",
        "max_tuples": None,
        "max_combos": None,
        "data_snapshot_hash": None,
        "options_sim": False,
    }


def _result_tree(root: Path) -> dict[str, str]:
    """SHA-256 of every RESULT file under *root* (state/timestamp surfaces
    excluded) — the #1712 ``_result_tree`` idiom."""
    out: dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = str(p.relative_to(root))
        if any(rel.startswith(prefix) for prefix in _EXCLUDE_PREFIXES):
            continue
        if rel.endswith(".stamp.json") or rel.endswith("/stage2") or rel == "stage2":
            continue
        out[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def _sup_tuple_lines(path: Path) -> list[tuple[str, object, object]]:
    """The supervisor chain reduced to TUPLE_DONE (action, tuple, store hash)."""
    return [
        (e.action, e.payload.get("tuple"), e.payload.get("store_content_hash"))
        for e in read_entries(path)
        if e.action == "TUPLE_DONE"
    ]


def test_pull_mid_compute_leaves_sweep_results_byte_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE executed concurrency regression — see the module docstring."""
    # ── CONTROL: solo sweep over the pre-write lake ──────────────────────────
    control_lake, control_catalog = _seed(tmp_path / "control")
    control_results = tmp_path / "control" / "sweeps"
    rc = cmd_sweep_all(symbols="NIFTY", **_kwargs(control_lake, control_catalog, control_results))
    assert rc == EXIT_OK
    control_tree = _result_tree(control_results)
    control_stores = [rel for rel in control_tree if rel.endswith(".parquet")]
    assert len(control_stores) == 2  # two tuples, both with a persisted store

    # ── TREATMENT: same grid; a REAL pull-shaped write lands mid-compute ────
    lake_root, catalog_path = _seed(tmp_path / "treat")
    results_root = tmp_path / "treat" / "sweeps"
    partition = lake_root / "bars" / _EXCHANGE / _SEGMENT / "NIFTY" / "1minute"
    month_file = partition / "2024" / "01.parquet"
    assert month_file.exists()
    pre_write_bytes = month_file.read_bytes()

    injected: dict[str, Any] = {"calls": 0}
    real_run_one_tuple = sweep_supervisor.run_one_tuple

    def run_one_tuple_with_concurrent_pull(*args: Any, **kwargs: Any) -> Any:
        injected["calls"] += 1
        if injected["calls"] == 2:
            # Deterministic MID-COMPUTE instant: tuple 1 is persisted, tuple 2
            # has not run. (1) A peer pull's EX acquire on a separate open
            # file description SUCCEEDS — the sweep holds no lake lock here,
            # so a real pull/crossverify would NOT defer.
            with pull_singleflight_lock(lake_root):
                # (2) While holding it, rewrite the EXACT partition the sweep
                # preloaded through the production writer (keep-first merge,
                # atomic os.replace) — the pull-shaped concurrent write.
                writer = ParquetWriter(lake_root=lake_root)
                with Catalog.open(catalog_path) as cat:
                    ParquetBarSink(writer=writer, catalog=cat).append(
                        exchange=_EXCHANGE,
                        segment=_SEGMENT,
                        symbol="NIFTY",
                        interval="1minute",
                        bars=_later_day_bars(),
                    )
            injected["partition_rewritten"] = month_file.read_bytes() != pre_write_bytes
        return real_run_one_tuple(*args, **kwargs)

    monkeypatch.setattr(sweep_supervisor, "run_one_tuple", run_one_tuple_with_concurrent_pull)
    rc = cmd_sweep_all(symbols="NIFTY", **_kwargs(lake_root, catalog_path, results_root))
    assert rc == EXIT_OK

    # The injection genuinely fired mid-compute and the partition file the
    # sweep preloaded was genuinely REPLACED under its feet.
    assert injected["calls"] == 2
    assert injected["partition_rewritten"] is True

    # ── the corruption oracle: BYTE-IDENTICAL results ────────────────────────
    treatment_tree = _result_tree(results_root)
    assert treatment_tree == control_tree
    # The sweep_runs SIBLING root (top100.json + baselines + results-depth
    # sidecars — R27 baselines derive from the sweep's OWN preloaded bars, so
    # identity here ALSO proves the run-end display pass served the memo, not
    # the rewritten lake).
    control_runs = _result_tree(tmp_path / "control" / "sweep_runs")
    treatment_runs = _result_tree(tmp_path / "treat" / "sweep_runs")
    assert len(control_runs) > 0
    assert treatment_runs == control_runs
    # Supervisor TUPLE_DONE content hashes — run identity + per-tuple store
    # content equal (the #1712 chain comparison).
    control_sup = _sup_tuple_lines(control_results / "audit" / "sweep-all-supervisor.jsonl")
    treatment_sup = _sup_tuple_lines(results_root / "audit" / "sweep-all-supervisor.jsonl")
    assert len(control_sup) == 2
    assert treatment_sup == control_sup

    # ── the concurrent write LANDED intact (keep-first union) ───────────────
    import polars as pl

    frame = pl.read_parquet(month_file)
    later_day = frame.filter(pl.col("timestamp") >= datetime(2024, 1, 4, 18, tzinfo=UTC))
    assert later_day.height == 10  # every concurrently-written bar stored
    assert later_day["volume"].to_list() == [999] * 10

    # ── ANTI-VACUOUS teeth: the oracle is genuinely sensitive ───────────────
    # A solo run over a lake that held the extra in-window bars AT PRELOAD
    # produces a DIFFERENT result tree — so the treatment==control equality
    # above cannot pass vacuously: had the sweep consumed the concurrently
    # written bars anywhere, the compare would have caught it.
    poisoned_lake, poisoned_catalog = _seed(tmp_path / "poisoned")
    writer = ParquetWriter(lake_root=poisoned_lake)
    with Catalog.open(poisoned_catalog) as cat:
        ParquetBarSink(writer=writer, catalog=cat).append(
            exchange=_EXCHANGE,
            segment=_SEGMENT,
            symbol="NIFTY",
            interval="1minute",
            bars=_later_day_bars(),
        )
    poisoned_results = tmp_path / "poisoned" / "sweeps"
    rc = cmd_sweep_all(
        symbols="NIFTY", **_kwargs(poisoned_lake, poisoned_catalog, poisoned_results)
    )
    assert rc == EXIT_OK
    assert _result_tree(poisoned_results) != control_tree
    # ...and the sweep_runs SIBLING differs too (the extra preloaded bars move
    # data_snapshot_hash → a different run_id keys the run-dir + sidecar
    # paths), so the treatment_runs == control_runs equality above is
    # INDEPENDENTLY proven non-vacuous — not just the sweeps tree.
    assert _result_tree(tmp_path / "poisoned" / "sweep_runs") != control_runs


def test_sweep_preload_still_refused_while_pull_holds_the_lake_lock(
    tmp_path: Path,
) -> None:
    """The UNCHANGED other side of the G10 law: a pull mid-WRITE when a sweep
    STARTS still refuses the sweep at preload — loudly, bounded to the preload
    (the sweep can simply be relaunched after the pull), never a torn read."""
    lake_root, catalog_path = _seed(tmp_path)
    with (
        pull_singleflight_lock(lake_root),  # the peer pull, EX on its own OFD
        pytest.raises(LakeBusyError, match="refusing to preload sweep bars"),
    ):
        cmd_sweep_all(symbols="NIFTY", **_kwargs(lake_root, catalog_path, tmp_path / "sweeps"))
