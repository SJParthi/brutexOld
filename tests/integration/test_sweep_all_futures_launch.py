"""Integration — the ADR-0012 NIFTY-futures sweep LAUNCH, end to end.

DEC-NIFTY-FUTURES-SWEEP-LAUNCH-001 / DEC-FUTURES-ADDRESSING-PIN-001: the REAL
``cmd_sweep_all`` is driven over REAL Parquet mini-lakes (written with the
same ``ParquetBarSink`` + ``materialize_lake_timeframes`` the pull pipeline
uses) under ``--symbols NSE-NIFTY-FUT --exchange NSE --segment FNO``
(+ ``--allow-out-of-scope`` since DEC-ENGINE-SPOT-ONLY-001 retired the alias
from default engine scope — the retained machinery's ONLY admit path),
composing the whole chain the unit suites own piecewise:

    scope gate → resolver → stitched loader slices → zero-bar preflight →
    G1 freshness gate over the CONTRACT symbols → grid → persisted stores
    under the SERIES instrument label.

Failure-mode matrix rows exercised end-to-end (design F1/F6/F8/F12):

* F1 — a missing MIDDLE contract refuses loudly naming the missing expiry
  month, and is NOT maskable by ``--allow-missing-slices``;
* F6 — a hand-staled derived 5minute partition on ONE contract is refused by
  the freshness gate (the alias expanded to contract dirs — an alias probe
  would silently abstain);
* F8 — the alias under the default INDEX segment is refused BEFORE any lake
  I/O (CLI stderr names the expected NSE/FNO coordinate; the dashboard route
  400s with the same reason);
* F12 — two lakes differing by ONE bar mint DISJOINT run identities (the
  full-content snapshot digest re-keys).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("polars")
pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")
pytest.importorskip("lmdb")

import polars as pl
from brutex.cli import EXIT_OK, EXIT_USER_ERROR, cmd_sweep_all
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.sink import ParquetBarSink
from brutex.data.timeframe_materialize import PrebuildKind, materialize_lake_timeframes

_IST = timezone(timedelta(hours=5, minutes=30))
_ALIAS = "NSE-NIFTY-FUT"

#: The 3-contract lake: real vendor-grammar dirs, real 2025 monthly expiries.
_JUN = ("NSE-NIFTY-26Jun25-FUT", date(2025, 6, 26))
_JUL = ("NSE-NIFTY-31Jul25-FUT", date(2025, 7, 31))
_AUG = ("NSE-NIFTY-28Aug25-FUT", date(2025, 8, 28))

#: Owned trading days each contract carries bars for (inside the window).
_DAYS: dict[str, list[date]] = {
    _JUN[0]: [date(2025, 6, 24), date(2025, 6, 25), date(2025, 6, 26)],
    _JUL[0]: [date(2025, 6, 27), date(2025, 6, 30)],
    _AUG[0]: [date(2025, 8, 1), date(2025, 8, 4)],
}
_BASE: dict[str, float] = {_JUN[0]: 100.0, _JUL[0]: 140.0, _AUG[0]: 180.0}

_WINDOW_START = "2025-06-24"
_WINDOW_END = "2025-08-05"


@pytest.fixture(autouse=True)
def _sequential_sweep_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the sequential sweep path — determinism over the tiny grids."""
    monkeypatch.setenv("BRUTEX_SWEEP_WORKERS", "1")


def _day_bars(day: date, base: float, n: int = 120) -> list[CandleBar]:
    """The sweep-run golden's drifting intraday shape, anchored on *day*."""
    out: list[CandleBar] = []
    price = base
    for m in range(n):
        close = price + 0.6 if m % 3 else price - 0.3
        ts = datetime(day.year, day.month, day.day, 9, 16, tzinfo=_IST) + timedelta(minutes=m)
        out.append(
            CandleBar(
                timestamp=ts.astimezone(UTC),
                open=Decimal(str(round(price, 2))),
                high=Decimal(str(round(max(price, close) + 0.5, 2))),
                low=Decimal(str(round(min(price, close) - 0.5, 2))),
                close=Decimal(str(round(close, 2))),
                volume=100,
                open_interest=None,
            )
        )
        price = close
    return out


def _seed_lake(
    tmp_path: Path,
    *,
    contracts: tuple[str, ...] = (_JUN[0], _JUL[0], _AUG[0]),
    derive: bool = True,
    extra_bar_on: str | None = None,
) -> tuple[Path, Path]:
    """Write the futures mini-lake (+ derived 5minute via the REAL prebuild)."""
    lake_root = tmp_path / "lake"
    catalog_path = tmp_path / "catalog.duckdb"
    writer = ParquetWriter(lake_root=lake_root)
    sink = ParquetBarSink(writer=writer)
    for symbol in contracts:
        bars = [b for day in _DAYS[symbol] for b in _day_bars(day, _BASE[symbol])]
        if extra_bar_on == symbol:
            extra_day = _DAYS[symbol][-1]
            bars.extend(_day_bars(extra_day, _BASE[symbol], n=121)[-1:])
        sink.append(exchange="NSE", segment="FNO", symbol=symbol, interval="1minute", bars=bars)
        if derive:
            # tfs takes TF LABELS ("5m"), written to the lake INTERVAL dir
            # ("5minute") — the same prebuild call the pipeline runs.
            materialize_lake_timeframes(
                lake_root=lake_root,
                exchange="NSE",
                segment="FNO",
                symbol=symbol,
                kind=PrebuildKind.FUTURES,
                tfs=("5m",),
            )
    return lake_root, catalog_path


def _kwargs(
    lake_root: Path, catalog_path: Path, results_root: Path, **overrides: Any
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "lake_root": lake_root,
        "catalog_path": catalog_path,
        "results_root": results_root,
        "exchange": "NSE",
        "segment": "FNO",
        "symbols": _ALIAS,
        "intervals": "1minute,5minute",
        "start": _WINDOW_START,
        "end": _WINDOW_END,
        "directions": "BUY",
        "k": "1",
        "conditions": "0,1,2,3,4",
        "sl": "80",
        "tp": "50",
        "top": 10,
        "mode": "A",
        "max_tuples": None,
        "max_combos": None,
        "data_snapshot_hash": None,
        "options_sim": False,
        # LEGACY-MODE PIN (DEC-FILL-WORST-CASE-001 fix round 2): this suite's
        # fixtures + expectations were minted under PRECISE — under the
        # WORST_CASE production default the tiny fixture's combos fail the
        # rank gates and persist NOTHING (the Stage-1 pin sweep missed this
        # file; the explicit pin is byte-identical to the pre-mode launch).
        "fill_mode": "precise",
        # DEC-ENGINE-SPOT-ONLY-001: NSE-NIFTY-FUT is RETIRED from default
        # engine sweep scope — these tests exercise the RETAINED
        # below-the-gate futures machinery via the loud, audit-stamped
        # research override, the ONLY remaining admit path (the default
        # refusal itself is pinned by TestSpotOnlyScopeRefusal below).
        "allow_out_of_scope": True,
    }
    kwargs.update(overrides)
    return kwargs


def _persisted_frames(results_root: Path) -> list[pl.DataFrame]:
    """Every persisted results-store frame under *results_root* (non-empty)."""
    frames: list[pl.DataFrame] = []
    for path in sorted(results_root.rglob("*.parquet")):
        try:
            df = pl.read_parquet(path)
        except pl.exceptions.PolarsError:  # sidecars etc. — not a results store
            continue
        if {"run_id", "instrument"} <= set(df.columns) and df.height > 0:
            frames.append(df)
    return frames


def _run_ids(results_root: Path) -> set[str]:
    return {rid for df in _persisted_frames(results_root) for rid in df["run_id"].to_list()}


# ────── the composed happy chain ──────


def test_futures_launch_end_to_end_persists_under_the_series_label(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """scope → resolver → stitched slices → gates → grid → stores, one command."""
    lake_root, catalog_path = _seed_lake(tmp_path)
    results_root = tmp_path / "sweeps"
    rc = cmd_sweep_all(**_kwargs(lake_root, catalog_path, results_root))
    out = capsys.readouterr().out
    assert rc == EXIT_OK
    assert "sweep-all done" in out
    frames = _persisted_frames(results_root)
    assert frames, "the futures launch persisted no ranked finalists"
    # Every persisted row carries the SERIES alias as its instrument — the
    # ONE identity that pools trades across contracts (never a per-contract
    # label; the D14/D6 statistical deliverables depend on this pooling).
    for df in frames:
        assert set(df["instrument"].to_list()) == {_ALIAS}


def test_futures_launch_run_ids_reproduce_across_invocations(tmp_path: Path) -> None:
    """Golden Rule #4/#7 at the launch grain: same lake ⇒ same run identities."""
    lake_root, catalog_path = _seed_lake(tmp_path)
    rc_a = cmd_sweep_all(**_kwargs(lake_root, catalog_path, tmp_path / "sweeps_a"))
    rc_b = cmd_sweep_all(**_kwargs(lake_root, catalog_path, tmp_path / "sweeps_b"))
    assert rc_a == EXIT_OK and rc_b == EXIT_OK
    ids_a = _run_ids(tmp_path / "sweeps_a")
    ids_b = _run_ids(tmp_path / "sweeps_b")
    assert ids_a
    assert ids_a == ids_b


def test_one_extra_bar_rekeys_every_run_identity(tmp_path: Path) -> None:
    """F12: two lakes differing by ONE stored bar mint DISJOINT run identities.

    The full-content bxsnap2 snapshot digest covers every stitched bar, so a
    lake change between two launches of the same window is an HONEST RE-KEY,
    never a silent collision.
    """
    lake_a, catalog_a = _seed_lake(tmp_path / "a")
    lake_b, catalog_b = _seed_lake(tmp_path / "b", extra_bar_on=_JUL[0])
    rc_a = cmd_sweep_all(**_kwargs(lake_a, catalog_a, tmp_path / "a" / "sweeps"))
    rc_b = cmd_sweep_all(**_kwargs(lake_b, catalog_b, tmp_path / "b" / "sweeps"))
    assert rc_a == EXIT_OK and rc_b == EXIT_OK
    ids_a = _run_ids(tmp_path / "a" / "sweeps")
    ids_b = _run_ids(tmp_path / "b" / "sweeps")
    assert ids_a and ids_b
    assert ids_a.isdisjoint(ids_b)


# ────── F16: record → sweep-verify --recount round trip ──────


def _verify_kwargs(
    lake_root: Path, catalog_path: Path, results_root: Path, scratch: Path
) -> dict[str, Any]:
    return {
        "lake_root": lake_root,
        "catalog_path": catalog_path,
        "results_root": results_root,
        "exchange": "NSE",
        "segment": "FNO",
        "symbols": _ALIAS,
        "intervals": "1minute",
        "start": _WINDOW_START,
        "end": _WINDOW_END,
        "directions": "BUY",
        "k": "1",
        "conditions": "0,1,2,3,4",
        "sl": "80",
        "tp": "50",
        "top": 10,
        "mode": "A",
        "max_tuples": None,
        "max_combos": None,
        "data_snapshot_hash": None,
        "indicator_version": None,
        "code_commit_sha": None,
        "recount": True,
        "scratch_root": scratch,
        "allow_context_mismatch": False,
        # DEC-ENGINE-SPOT-ONLY-001: the recount Tier-3 shadow re-sweep rides
        # the SAME launch gate — a recorded futures tree recounts only under
        # the research override.
        "allow_out_of_scope": True,
    }


# ────── the launch cascade + the manual replay (review round-2) ──────


def test_launch_cascade_carries_schedules_and_scores_futures_pools(tmp_path: Path) -> None:
    """Round-2 finding 6(a): the LAUNCH cascade genuinely cascades futures.

    Pre-fix teeth: dropping the `roll_schedule_by_instrument=` argument at the
    cmd_sweep_all call site (round-1 commit da461a99) degraded every futures
    launch cascade to the loud-WARN pool skip while the whole suite stayed
    green — nothing asserted the manifest carriage OR that the report scored
    any futures pool. This pins both at the REAL launch mouth.
    """
    import json

    lake_root, catalog_path = _seed_lake(tmp_path, derive=False)
    results_root = tmp_path / "sweeps"
    rc = cmd_sweep_all(**_kwargs(lake_root, catalog_path, results_root, intervals="1minute"))
    assert rc == EXIT_OK
    manifest = json.loads((results_root / "cascade" / "manifest.json").read_text(encoding="utf-8"))
    expected_schedule = [_JUN[1].toordinal(), _JUL[1].toordinal(), _AUG[1].toordinal()]
    assert manifest["roll_schedules"] == [[_ALIAS, expected_schedule]]
    report = json.loads(
        (results_root / "cascade" / "cascade-report.json").read_text(encoding="utf-8")
    )
    # >0 futures pools actually SCORED — the launch never silently degrades to
    # the futures_pool_skipped_no_roll_schedule path.
    assert report["groups"], "the launch cascade scored ZERO pools — schedule carriage broken"
    assert {group["instrument"] for group in report["groups"]} == {_ALIAS}


def test_manual_cascade_replays_then_refuses_a_drifted_lake(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Round-2 findings 1/4/9 (MAJOR, fails-before/passes-after): the manual
    `brutex cascade` replay cross-checks the manifest's FROZEN roll_schedules
    against the CURRENT lake's re-derivation. Unchanged lake → the golden
    byte-identical report; a lake whose contract set CHANGED since the sweep →
    a clean refusal naming BOTH schedules (pre-fix: EXIT_OK with a silently
    diverged report rewritten under mismatched Stage-1A resets)."""
    import shutil

    from brutex.cli import cmd_cascade

    lake_root, catalog_path = _seed_lake(tmp_path, derive=False)
    results_root = tmp_path / "sweeps"
    rc = cmd_sweep_all(**_kwargs(lake_root, catalog_path, results_root, intervals="1minute"))
    assert rc == EXIT_OK
    report_path = results_root / "cascade" / "cascade-report.json"
    launch_report = report_path.read_bytes()
    capsys.readouterr()

    # Unchanged lake: the replay round-trips to the SAME report bytes.
    rc = cmd_cascade(
        lake_root=lake_root, results_root=results_root, catalog_path=catalog_path, run_id=None
    )
    assert rc == EXIT_OK
    assert report_path.read_bytes() == launch_report

    # The lake DRIFTS (loses its newest contract) → the replay REFUSES with
    # both schedules named instead of silently rewriting the report.
    shutil.rmtree(lake_root / "bars" / "NSE" / "FNO" / _AUG[0])
    capsys.readouterr()
    rc = cmd_cascade(
        lake_root=lake_root, results_root=results_root, catalog_path=catalog_path, run_id=None
    )
    err = capsys.readouterr().err
    assert rc == EXIT_USER_ERROR
    assert "cascade error" in err
    assert "FROZEN" in err
    assert str([_JUN[1].toordinal(), _JUL[1].toordinal(), _AUG[1].toordinal()]) in err
    assert str([_JUN[1].toordinal(), _JUL[1].toordinal()]) in err
    assert "contract set changed" in err
    assert "sweep-all" in err  # the remediation
    assert report_path.read_bytes() == launch_report  # the report is NOT rewritten


def test_dashboard_cascade_route_400s_a_drifted_lake(tmp_path: Path) -> None:
    """Round-2 findings 10/12 at the dashboard mouth: the "Run cascade" route
    turns the replay's schedule cross-check refusal AND a futures-series
    resolution refusal into clear 400s — never a 500 traceback."""
    pytest.importorskip("fastapi", reason="dashboard routes need the [ui] extra")
    import shutil

    from brutex.dashboard.app import DashboardState, build_app
    from brutex.data.instrument_store import InstrumentStore
    from fastapi.testclient import TestClient

    lake_root, catalog_path = _seed_lake(tmp_path, derive=False)
    # The route derives the lake from the injected store's path parent and
    # replays <lake>/sweeps — sweep into exactly that root.
    rc = cmd_sweep_all(
        **_kwargs(lake_root, catalog_path, lake_root / "sweeps", intervals="1minute")
    )
    assert rc == EXIT_OK
    with (
        InstrumentStore.open(lake_root / "instruments.duckdb") as store,
        TestClient(build_app(DashboardState(), instrument_store=store)) as client,
    ):
        # (a) schedule drift: the lake loses its newest contract.
        shutil.rmtree(lake_root / "bars" / "NSE" / "FNO" / _AUG[0])
        resp = client.post("/api/v1/cascade/run")
        assert resp.status_code == 400
        payload = resp.json()
        assert payload["ok"] is False
        assert "contract set changed" in payload["error"]
        # (b) resolution refusal (FuturesSeriesError): the whole futures
        # tree vanished since launch — re-resolution refuses loudly.
        shutil.rmtree(lake_root / "bars" / "NSE" / "FNO" / _JUL[0])
        shutil.rmtree(lake_root / "bars" / "NSE" / "FNO" / _JUN[0])
        resp = client.post("/api/v1/cascade/run")
        assert resp.status_code == 400
        assert "zero NIFTY futures contract dirs" in resp.json()["error"]


def test_dashboard_cascade_route_200s_a_clean_replay(tmp_path: Path) -> None:
    """The "Run cascade" route's SUCCESS path: an unchanged lake replays the
    launch manifest and returns 200 with the pool / candidate / survivor tally
    (covers the ``run_and_write_from_manifest`` success arm + the 200 return —
    the drifted-lake test only exercises the 400 refusal arms)."""
    pytest.importorskip("fastapi", reason="dashboard routes need the [ui] extra")
    from brutex.dashboard.app import DashboardState, build_app
    from brutex.data.instrument_store import InstrumentStore
    from fastapi.testclient import TestClient

    lake_root, catalog_path = _seed_lake(tmp_path, derive=False)
    rc = cmd_sweep_all(
        **_kwargs(lake_root, catalog_path, lake_root / "sweeps", intervals="1minute")
    )
    assert rc == EXIT_OK
    with (
        InstrumentStore.open(lake_root / "instruments.duckdb") as store,
        TestClient(build_app(DashboardState(), instrument_store=store)) as client,
    ):
        resp = client.post("/api/v1/cascade/run")
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["ok"] is True
        assert payload["sweep_id"]
        assert payload["pools"] >= 1
        assert "candidates" in payload
        assert "survivors" in payload


def test_dashboard_cascade_route_400s_a_missing_manifest(tmp_path: Path) -> None:
    """The route turns a missing launch manifest (no ``sweep-all`` has run into
    ``<lake>/sweeps`` yet) into a clean 400 naming the remediation, never a 500
    (covers the ``load_manifest`` ``CascadeWireError`` arm)."""
    pytest.importorskip("fastapi", reason="dashboard routes need the [ui] extra")
    from brutex.dashboard.app import DashboardState, build_app
    from brutex.data.instrument_store import InstrumentStore
    from fastapi.testclient import TestClient

    lake_root, _catalog_path = _seed_lake(tmp_path, derive=False)
    # No cmd_sweep_all — <lake>/sweeps carries no manifest to replay.
    with (
        InstrumentStore.open(lake_root / "instruments.duckdb") as store,
        TestClient(build_app(DashboardState(), instrument_store=store)) as client,
    ):
        resp = client.post("/api/v1/cascade/run")
        assert resp.status_code == 400
        payload = resp.json()
        assert payload["ok"] is False
        assert "no cascade manifest" in payload["error"]


def test_dashboard_cascade_route_400s_when_no_lake_is_configured(tmp_path: Path) -> None:
    """The route refuses with a clear 400 when the wired store exposes no
    on-disk lake path (``store.path`` is not a ``Path``) — never a 500 (covers
    the ``isinstance(lake_root, Path)`` guard's refusal arm)."""
    pytest.importorskip("fastapi", reason="dashboard routes need the [ui] extra")
    from brutex.dashboard.app import DashboardState, build_app
    from brutex.data.instrument_store import InstrumentStore
    from fastapi.testclient import TestClient

    lake_root, _catalog_path = _seed_lake(tmp_path, derive=False)
    with (
        InstrumentStore.open(lake_root / "instruments.duckdb") as store,
        TestClient(build_app(DashboardState(), instrument_store=store)) as client,
    ):
        # Drop the store's on-disk path so the route's `isinstance(..., Path)`
        # guard trips (a store wired without a resolvable lake root).
        store._path = None  # type: ignore[assignment]
        resp = client.post("/api/v1/cascade/run")
        assert resp.status_code == 400
        assert "no lake configured for cascade" in resp.json()["detail"]


def test_manual_cascade_interior_hole_is_a_clean_user_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Round-2 findings 10/12: a FuturesSeriesError out of the replay's
    re-resolution (an interior hole that appeared since the launch) is a
    one-line user error — never a raw traceback (the cmd_cascade net now
    covers load_bars_for_manifest + run_and_write_from_manifest)."""
    import shutil

    from brutex.cli import cmd_cascade

    lake_root, catalog_path = _seed_lake(tmp_path, derive=False)
    results_root = tmp_path / "sweeps"
    rc = cmd_sweep_all(**_kwargs(lake_root, catalog_path, results_root, intervals="1minute"))
    assert rc == EXIT_OK
    shutil.rmtree(lake_root / "bars" / "NSE" / "FNO" / _JUL[0])  # Jun + Aug stay: interior hole
    capsys.readouterr()
    rc = cmd_cascade(
        lake_root=lake_root, results_root=results_root, catalog_path=catalog_path, run_id=None
    )
    err = capsys.readouterr().err
    assert rc == EXIT_USER_ERROR
    assert "cascade error" in err
    assert "INTERIOR hole" in err


# ────── the resume-honesty gate (review round-2 finding 11) ──────

#: Holiday adjacency (real 2026 NSE calendar, calendar-real expiries — the
#: prior 02Apr26/30Apr26 pair was a vendor-IMPOSSIBLE same-month duplicate the
#: round-5 finding-10 gate now refuses): the March contract expires Mon
#: 2026-03-30 and Tue 2026-03-31 is a trading holiday — a SESSIONLESS overhang.
_MAR26 = ("NSE-NIFTY-30Mar26-FUT", date(2026, 3, 30))
_APR26_SUCC = ("NSE-NIFTY-28Apr26-FUT", date(2026, 4, 28))


def test_schedule_only_lake_drift_refuses_the_resume_up_front(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Round-2 finding 11: a SCHEDULE-ONLY drift (snapshot byte-identical, the
    successor contract's in-window owned span sessionless) is refused UP FRONT
    with both schedules named — instead of resuming rows minted under the
    recorded schedule and crashing Stage-3/cascade recovery with a
    misattributed grid-mismatch error."""
    lake_root = tmp_path / "lake"
    catalog_path = tmp_path / "catalog.duckdb"
    writer = ParquetWriter(lake_root=lake_root)
    sink = ParquetBarSink(writer=writer)
    bars = [b for day in (date(2026, 3, 27), _MAR26[1]) for b in _day_bars(day, 100.0)]
    sink.append(exchange="NSE", segment="FNO", symbol=_MAR26[0], interval="1minute", bars=bars)
    results_root = tmp_path / "sweeps"
    kwargs = _kwargs(
        lake_root,
        catalog_path,
        results_root,
        intervals="1minute",
        start="2026-03-27",
        end="2026-03-31",  # 1-day tail overhang past the Mar-30 expiry (a holiday)
    )
    rc = cmd_sweep_all(**kwargs)
    assert rc == EXIT_OK

    # The successor contract is pulled between launches. Its in-window owned
    # span (exactly 2026-03-31) holds ZERO sessions, so it joins the roll
    # schedule while contributing ZERO bars — the snapshot stays byte-identical.
    (lake_root / "bars" / "NSE" / "FNO" / _APR26_SUCC[0]).mkdir()
    capsys.readouterr()
    rc = cmd_sweep_all(**kwargs)
    err = capsys.readouterr().err
    assert rc == EXIT_USER_ERROR
    assert "sweep-all REFUSED" in err
    assert str([_MAR26[1].toordinal()]) in err  # the RECORDED schedule
    assert str([_MAR26[1].toordinal(), _APR26_SUCC[1].toordinal()]) in err  # the FRESH one
    assert "UNCHANGED" in err
    assert "contract set changed" in err


def test_sweep_verify_recount_round_trips_a_futures_tree(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """F16: the recount loads through the SAME resolver + loader the launch
    used, re-derives the SAME schedule, and byte-verifies the futures tree —
    a clean round trip, never a false divergence (Golden Rule #4/#7)."""
    from brutex.cli import cmd_sweep_verify

    lake_root, catalog_path = _seed_lake(tmp_path, derive=False)
    results_root = tmp_path / "sweeps"
    rc = cmd_sweep_all(**_kwargs(lake_root, catalog_path, results_root, intervals="1minute"))
    assert rc == EXIT_OK
    capsys.readouterr()
    rc = cmd_sweep_verify(
        **_verify_kwargs(lake_root, catalog_path, results_root, tmp_path / "scratch")
    )
    out = capsys.readouterr().out
    assert rc == EXIT_OK
    assert "recount REFUSED" not in out
    assert "roll_schedule[" not in out  # recorded == re-derived — no mismatch line


def test_recount_names_a_recorded_vs_rederived_schedule_mismatch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """F16 (the evidence arm): a lake whose contract set CHANGED since the
    recorded run is surfaced as a named recorded-vs-re-derived schedule
    mismatch — with evidence, never a bare hash diff — and the recount
    REFUSES (verdict-grade only under a fully matched context)."""
    import shutil

    from brutex.cli import cmd_sweep_verify

    lake_root, catalog_path = _seed_lake(tmp_path, derive=False)
    results_root = tmp_path / "sweeps"
    rc = cmd_sweep_all(**_kwargs(lake_root, catalog_path, results_root, intervals="1minute"))
    assert rc == EXIT_OK
    # The lake LOSES its newest contract after the record: the re-derived
    # schedule drops the Aug expiry while the recorded one carries it.
    shutil.rmtree(lake_root / "bars" / "NSE" / "FNO" / _AUG[0])
    capsys.readouterr()
    rc = cmd_sweep_verify(
        **_verify_kwargs(lake_root, catalog_path, results_root, tmp_path / "scratch")
    )
    out = capsys.readouterr().out
    assert rc != EXIT_OK
    assert f"roll_schedule[{_ALIAS}]: recorded" in out
    assert "re-derived" in out
    assert "recount REFUSED" in out


def test_recount_without_audit_refuses_verdict_grade_and_names_the_absent_schedule(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Round-4 finding 9 (upgrading round-1 finding 23 / round-2 finding 6b's
    informational note): a futures tree whose audit is absent/rotated has NO
    recorded roll_schedule to match — and the schedule IS recorded context
    (it enters canonical_trade_params, hence every futures run_id) and can
    drift while the snapshot stays byte-identical (a SESSIONLESS successor
    contract). Pre-fix the missing schedule was only PRINTED as informational,
    `context_match` stayed True, and a shadow re-sweep under a drifted fresh
    schedule condemned a healthy tree as verdict-grade CORRUPT (heal plan
    included). Now it is a context MISMATCH: the recount REFUSES (or degrades
    to INCONCLUSIVE under --allow-context-mismatch), still naming the absent
    recorded schedule + the derived one OUT LOUD."""
    from brutex.cli import EXIT_VERIFY_INCONCLUSIVE, cmd_sweep_verify
    from brutex.engine.sweep_verify import recorded_context_from_audit

    lake_root, catalog_path = _seed_lake(tmp_path, derive=False)
    results_root = tmp_path / "sweeps"
    rc = cmd_sweep_all(**_kwargs(lake_root, catalog_path, results_root, intervals="1minute"))
    assert rc == EXIT_OK
    audit_path = results_root / "audit" / "sweep-all.jsonl"
    recorded = recorded_context_from_audit(audit_path)
    assert recorded is not None
    audit_path.unlink()  # the rotated/absent-audit tree
    capsys.readouterr()
    kwargs = _verify_kwargs(lake_root, catalog_path, results_root, tmp_path / "scratch")
    kwargs.update(
        data_snapshot_hash=recorded.data_snapshot_hash,
        indicator_version=recorded.indicator_version,
        code_commit_sha=recorded.code_commit_sha,
    )
    rc = cmd_sweep_verify(**kwargs)
    out = capsys.readouterr().out
    # The evidence trail stays loud (the round-1 finding 23 contract) ...
    assert f"no RECORDED roll_schedule for {_ALIAS}" in out
    assert "CURRENT lake's schedule" in out
    expected = [_JUN[1].toordinal(), _JUL[1].toordinal(), _AUG[1].toordinal()]
    assert str(expected) in out  # the derived schedule is named in the evidence
    # ... but it is now a context MISMATCH, never verdict-grade (finding 9).
    assert "recorded context does NOT match" in out
    assert "recount REFUSED" in out
    assert rc == EXIT_VERIFY_INCONCLUSIVE


# ────── round-5 finding 6: the doctor on the sanctioned multi-generation daemon root ──────


def test_sweep_verify_scopes_facts_to_one_generation_on_a_daemon_root(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Round-5 finding 6: the sweep daemon reuses ONE results root across
    cycles, so its audit accretes a NEW context + a GROWN futures schedule
    every moved-snapshot cycle. Pre-fix `cmd_sweep_verify` derived facts from
    the WHOLE audit and exited EXIT_USER_ERROR ('inconsistent recorded
    context') from the second cycle onward — every tier (incl. the Tier-3
    recount oracle) permanently locked out of the production tree. Now the
    facts scope to the LATEST generation with an honest note naming the
    others."""
    from brutex.cli import cmd_sweep_verify

    lake_root, catalog_path = _seed_lake(tmp_path, derive=False)
    results_root = tmp_path / "sweeps"
    rc = cmd_sweep_all(**_kwargs(lake_root, catalog_path, results_root, intervals="1minute"))
    assert rc == EXIT_OK

    # Cycle 2 of the daemon cadence: the lake GROWS a front-month contract
    # (schedule grows too) and the window advances — snapshot + schedule both
    # move, appended to the SAME resume=True audit in the SAME root.
    sep = "NSE-NIFTY-25Sep25-FUT"
    writer = ParquetWriter(lake_root=lake_root)
    sink = ParquetBarSink(writer=writer)
    sep_bars = [b for day in (date(2025, 9, 1), date(2025, 9, 2)) for b in _day_bars(day, 220.0)]
    sink.append(exchange="NSE", segment="FNO", symbol=sep, interval="1minute", bars=sep_bars)
    rc = cmd_sweep_all(
        **_kwargs(lake_root, catalog_path, results_root, intervals="1minute", end="2025-09-03")
    )
    assert rc == EXIT_OK
    capsys.readouterr()

    verify_kwargs = _verify_kwargs(lake_root, catalog_path, results_root, tmp_path / "scratch")
    verify_kwargs["recount"] = False  # Tier 1/2 — the recount context gate has its own pins
    rc = cmd_sweep_verify(**verify_kwargs)
    captured = capsys.readouterr()
    # Pre-fix: EXIT_USER_ERROR + 'inconsistent recorded context' on stderr.
    assert rc != EXIT_USER_ERROR
    assert "inconsistent recorded context" not in captured.err
    assert "1 other generation(s)" in captured.out  # the honest note
    assert "LATEST recorded generation" in captured.out
    assert "--data-snapshot-hash" in captured.out  # how to scope another


# ────── round-5 finding 11: the digest-aware fresh-sentinel resume probe ──────


def _replace_audit_with_one_spot_start(
    audit_path: Path, *, snapshot: str, indicator: str, code_sha: str
) -> None:
    """Simulate the MID-FILE-lossy audit: only a schedule-less SPOT
    ``SWEEP_START`` of the named generation survives (the futures ones lost)."""
    from brutex.orchestrator.audit import AuditWriter

    audit_path.unlink()
    with AuditWriter.open(audit_path) as audit:
        audit.append(
            action="SWEEP_START",
            subject="s",
            payload={
                "data_snapshot_hash": snapshot,
                "indicator_version": indicator,
                "code_commit_sha": code_sha,
                "instrument": "NIFTY",
            },
        )


def test_lossy_audit_over_fresh_generation_sentinels_refuses_the_resume(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Round-5 finding 11: a mid-file-lossy audit that kept a matching-
    generation SPOT start while losing the futures SWEEP_STARTs abstained
    (`recorded.get(instrument)` is None → fall-through), even though the
    tree's sentinels were minted under THIS generation — rows whose schedule
    is exactly as unverifiable as with the audit absent. The digest-aware
    probe now refuses: the sentinel sits at one of THIS launch's expected
    paths, so it provably pairs with this grid."""
    from brutex.engine.sweep_verify import recorded_context_from_audit

    lake_root, catalog_path = _seed_lake(tmp_path, derive=False)
    results_root = tmp_path / "sweeps"
    rc = cmd_sweep_all(**_kwargs(lake_root, catalog_path, results_root, intervals="1minute"))
    assert rc == EXIT_OK
    audit_path = results_root / "audit" / "sweep-all.jsonl"
    recorded = recorded_context_from_audit(audit_path)
    assert recorded is not None
    _replace_audit_with_one_spot_start(
        audit_path,
        snapshot=recorded.data_snapshot_hash,
        indicator=recorded.indicator_version,
        code_sha=recorded.code_commit_sha,
    )
    capsys.readouterr()
    rc = cmd_sweep_all(**_kwargs(lake_root, catalog_path, results_root, intervals="1minute"))
    err = capsys.readouterr().err
    # Pre-fix: EXIT_OK — the resume proceeded, pairing sentinel rows minted
    # under an UNVERIFIABLE schedule with the freshly resolved one.
    assert rc == EXIT_USER_ERROR
    assert "sweep-all REFUSED" in err
    assert "record NO roll_schedule" in err
    assert ".complete.json" in err  # the fresh-digest sentinel named
    assert "mid-file-lossy" in err
    assert "--results-root" in err  # the remediation


def test_crashed_parallel_launch_prefix_audit_still_resumes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Round-7 finding 1 (MAJOR), end to end: the durable state a PARENT crash
    leaves on the parallel driver — workers wrote stores + completion
    sentinels directly, while every buffered per-tuple SWEEP_START was lost
    with the parent — must re-launch cleanly, not be condemned as a
    tail-lossy audit. ``cmd_sweep_all`` now appends one fsynced SWEEP_LAUNCH
    context line per futures instrument BEFORE grid dispatch, so the audit's
    pre-dispatch PREFIX (exactly what such a crash leaves durable) records
    the generation's roll_schedule and the resume gate compares
    recorded == fresh instead of refusing."""
    import json

    lake_root, catalog_path = _seed_lake(tmp_path, derive=False)
    results_root = tmp_path / "sweeps"
    rc = cmd_sweep_all(**_kwargs(lake_root, catalog_path, results_root, intervals="1minute"))
    assert rc == EXIT_OK
    audit_path = results_root / "audit" / "sweep-all.jsonl"
    lines = audit_path.read_text(encoding="utf-8").splitlines(keepends=True)
    actions = [json.loads(line)["action"] for line in lines]
    # The ordering guarantee the fix rests on: the launch context line is
    # durably appended BEFORE any per-tuple SWEEP_START can run (fails
    # pre-fix: no SWEEP_LAUNCH line exists at all).
    assert "SWEEP_LAUNCH" in actions
    assert actions.index("SWEEP_LAUNCH") < actions.index("SWEEP_START")
    # Truncate the audit to the strict pre-dispatch prefix — a valid
    # hash-chain prefix (AuditWriter fsyncs per append), byte-identical to
    # what a parent crash right after dispatch leaves; sentinels + stores
    # stay (the workers wrote them durably).
    keep = lines[: actions.index("SWEEP_LAUNCH") + 1]
    assert len(keep) < len(lines)
    audit_path.write_text("".join(keep), encoding="utf-8")
    capsys.readouterr()
    rc = cmd_sweep_all(**_kwargs(lake_root, catalog_path, results_root, intervals="1minute"))
    err = capsys.readouterr().err
    # Pre-fix: EXIT_USER_ERROR — 'the audit records NO SWEEP_START under this
    # snapshot generation … a tail-lossy/restored audit' (a permanent
    # wrong-diagnosis refusal of the sanctioned crash-resume).
    assert "sweep-all REFUSED" not in err
    assert rc == EXIT_OK


def test_worst_case_launch_line_records_the_fill_mode(tmp_path: Path) -> None:
    """The SWEEP_LAUNCH fill-mode recording law (DEC-FILL-WORST-CASE-001 —
    round-2 NIT-3 superseded by FIX ROUND 3): the pre-dispatch launch context
    line records the fill mode ALWAYS AND EXPLICITLY — including the literal
    "precise" — matching the SWEEP_START recording law, so a same-generation
    mixed-mode root is detectable and a parallel-crash-window prefix audit
    (the launch line only — every buffered per-tuple SWEEP_START lost with
    the parent) reads the TRUE mode. The round-2 emit-on-non-PRECISE /
    key-ABSENCE pin legitimately FLIPPED here: PRECISE is now an explicit
    token on the AUDIT wire (canonical_trade_params / run identity / tuple
    digests keep the elision law byte-untouched)."""
    import json

    from brutex.engine.sweep_verify import recorded_audit_facts
    from brutex.execution.fill_mode import FillMode

    lake_root, catalog_path = _seed_lake(tmp_path, derive=False)
    results_root = tmp_path / "sweeps"
    rc = cmd_sweep_all(
        **_kwargs(
            lake_root, catalog_path, results_root, intervals="1minute", fill_mode="worst_case"
        )
    )
    assert rc == EXIT_OK
    audit_path = results_root / "audit" / "sweep-all.jsonl"
    lines = audit_path.read_text(encoding="utf-8").splitlines(keepends=True)
    actions = [json.loads(line)["action"] for line in lines]
    launch_idx = actions.index("SWEEP_LAUNCH")
    launch = json.loads(lines[launch_idx])
    assert launch["payload"]["params"]["fill_mode"] == "worst_case"
    # Truncate to the strict pre-dispatch prefix — exactly what a parent
    # crash right after dispatch leaves durable — and read the scoped facts:
    # the generation's fill mode survives the crash window.
    audit_path.write_text("".join(lines[: launch_idx + 1]), encoding="utf-8")
    assert recorded_audit_facts(audit_path)[3] == (FillMode.WORST_CASE,)

    # The explicit-PRECISE half (fix round 3): a precise launch RECORDS the
    # literal token on both the launch line and every per-tuple SWEEP_START.
    precise_root = tmp_path / "sweeps_precise"
    rc = cmd_sweep_all(**_kwargs(lake_root, catalog_path, precise_root, intervals="1minute"))
    assert rc == EXIT_OK
    precise_audit = precise_root / "audit" / "sweep-all.jsonl"
    recording_actions = set()
    for line in precise_audit.read_text(encoding="utf-8").splitlines():
        entry = json.loads(line)
        if entry["action"] in ("SWEEP_LAUNCH", "SWEEP_START"):
            recording_actions.add(entry["action"])
            assert entry["payload"]["params"]["fill_mode"] == "precise"
    assert recording_actions == {"SWEEP_LAUNCH", "SWEEP_START"}
    assert recorded_audit_facts(precise_audit)[3] == (FillMode.PRECISE,)


def test_lossy_audit_over_foreign_generation_sentinels_still_resumes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Round-5 finding 11 (the zero-false-positive law): generation-A futures
    sentinels under a generation-B lossy audit can NEVER pair with the fresh
    launch (their digests folded generation A's snapshot), so the probe
    abstains and the legitimate resume proceeds — the naive generation-blind
    glob would have false-refused exactly this."""
    from brutex.engine.sweep_verify import recorded_context_from_audit

    lake_root, catalog_path = _seed_lake(tmp_path, derive=False)
    results_root = tmp_path / "sweeps"
    rc = cmd_sweep_all(**_kwargs(lake_root, catalog_path, results_root, intervals="1minute"))
    assert rc == EXIT_OK  # generation A: futures sentinels now on disk

    # The lake moves (one extra in-window bar) → generation B's snapshot differs.
    writer = ParquetWriter(lake_root=lake_root)
    sink = ParquetBarSink(writer=writer)
    extra = _day_bars(_DAYS[_AUG[0]][-1], _BASE[_AUG[0]], n=121)[-1:]
    sink.append(exchange="NSE", segment="FNO", symbol=_AUG[0], interval="1minute", bars=extra)
    # Learn generation B's recorded context from a throwaway launch.
    rc = cmd_sweep_all(
        **_kwargs(lake_root, catalog_path, tmp_path / "sweeps_b", intervals="1minute")
    )
    assert rc == EXIT_OK
    gen_b = recorded_context_from_audit(tmp_path / "sweeps_b" / "audit" / "sweep-all.jsonl")
    assert gen_b is not None

    _replace_audit_with_one_spot_start(
        results_root / "audit" / "sweep-all.jsonl",
        snapshot=gen_b.data_snapshot_hash,
        indicator=gen_b.indicator_version,
        code_sha=gen_b.code_commit_sha,
    )
    capsys.readouterr()
    rc = cmd_sweep_all(**_kwargs(lake_root, catalog_path, results_root, intervals="1minute"))
    err = capsys.readouterr().err
    assert "sweep-all REFUSED" not in err
    assert rc == EXIT_OK  # the crash-resume guarantee stands


# ────── failure-mode matrix rows ──────


def test_missing_middle_contract_refuses_naming_the_month(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """F1: an INTERIOR hole (Jun + Aug present, Jul absent) refuses loudly."""
    lake_root, catalog_path = _seed_lake(tmp_path, contracts=(_JUN[0], _AUG[0]))
    rc = cmd_sweep_all(**_kwargs(lake_root, catalog_path, tmp_path / "sweeps"))
    err = capsys.readouterr().err
    assert rc == EXIT_USER_ERROR
    assert "REFUSED" in err
    assert "INTERIOR hole" in err
    assert "Jul 2025" in err  # the calendar-named missing monthly expiry
    assert "Pull · full pipeline" in err  # the remediation


def test_interior_hole_is_not_maskable_by_allow_missing_slices(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """F1 (the escape-hatch teeth): --allow-missing-slices governs empty
    (symbol, interval) pairs, NEVER interior continuity — the refusal stands."""
    lake_root, catalog_path = _seed_lake(tmp_path, contracts=(_JUN[0], _AUG[0]))
    rc = cmd_sweep_all(
        **_kwargs(lake_root, catalog_path, tmp_path / "sweeps", allow_missing_slices=True)
    )
    err = capsys.readouterr().err
    assert rc == EXIT_USER_ERROR
    assert "INTERIOR hole" in err


def test_staled_derived_tf_on_one_contract_is_refused_by_g1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """F6: the G1 gate probes the resolved CONTRACT dirs (the alias names no
    lake dir — probing it would silently abstain, a gate bypass); ONE contract
    whose stored 5minute drifted from a fresh 1m re-derivation refuses the
    whole launch, naming that contract pair."""
    lake_root, catalog_path = _seed_lake(tmp_path, derive=True)
    # Hand-stale the Jun contract's derived 5minute: overwrite the partition
    # with RAW 1-minute bars (the wrong-grid drift shape) — heavier than the
    # real derivation, so the byte-compare genuinely differs.
    import shutil

    five_min_dir = lake_root / "bars" / "NSE" / "FNO" / _JUN[0] / "5minute"
    shutil.rmtree(five_min_dir)
    writer = ParquetWriter(lake_root=lake_root)
    sink = ParquetBarSink(writer=writer)
    stale_bars = [b for day in _DAYS[_JUN[0]] for b in _day_bars(day, _BASE[_JUN[0]])]
    sink.append(exchange="NSE", segment="FNO", symbol=_JUN[0], interval="5minute", bars=stale_bars)
    rc = cmd_sweep_all(**_kwargs(lake_root, catalog_path, tmp_path / "sweeps"))
    err = capsys.readouterr().err
    assert rc == EXIT_USER_ERROR
    assert "STALE" in err
    assert f"{_JUN[0]}/5minute" in err
    assert "prebuild" in err.lower()


def test_alias_under_index_segment_refused_before_any_lake_io(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """F8, reworded by DEC-ENGINE-SPOT-ONLY-001: the alias is refused on
    EVERY coordinate (the pre-flip wrong-coordinate arm folded into the ONE
    spot-only refusal) — no override, the DEFAULT gate."""
    lake_root, catalog_path = _seed_lake(tmp_path, derive=False)
    rc = cmd_sweep_all(
        **_kwargs(
            lake_root,
            catalog_path,
            tmp_path / "sweeps",
            segment="INDEX",
            allow_out_of_scope=False,
        )
    )
    err = capsys.readouterr().err
    assert rc == EXIT_USER_ERROR
    assert "DEC-ENGINE-SPOT-ONLY-001" in err


def test_mixed_spot_and_futures_symbols_under_one_segment_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """F8: a combined spot+futures study is TWO launches by design — one
    (exchange, segment) coordinate cannot satisfy both symbol families."""
    lake_root, catalog_path = _seed_lake(tmp_path, derive=False)
    rc = cmd_sweep_all(
        **_kwargs(
            lake_root,
            catalog_path,
            tmp_path / "sweeps",
            symbols=f"NIFTY,{_ALIAS}",
            allow_out_of_scope=False,
        )
    )
    err = capsys.readouterr().err
    assert rc == EXIT_USER_ERROR
    # Under NSE/FNO the SPOT symbol is the refused one (its coordinate is
    # NSE/INDEX); under INDEX the alias would be — either way the launch
    # refuses BEFORE any lake read.
    assert "NIFTY" in err


def test_dashboard_route_400s_the_alias_under_the_default_index_segment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F8 at the dashboard mouth: the alias under the route's default INDEX
    segment is a 400 whose detail names the expected NSE/FNO coordinate —
    and NO sweep job starts."""
    pytest.importorskip("fastapi", reason="dashboard routes need the [ui] extra")
    import importlib

    from brutex.dashboard.app import DashboardState, build_app
    from brutex.data.instrument_store import InstrumentStore
    from fastapi.testclient import TestClient

    app_module = importlib.import_module("brutex.dashboard.app")

    def _never_runs(*args: object, **kwargs: object) -> int:
        raise AssertionError("a refused launch must never start a sweep job")

    monkeypatch.setattr(app_module, "run_sweep_from_request", _never_runs)
    with (
        InstrumentStore.open(tmp_path / "instruments.duckdb") as store,
        TestClient(build_app(DashboardState(), instrument_store=store)) as client,
    ):
        resp = client.post("/api/v1/sweep/run", params={"symbols": _ALIAS})
        assert resp.status_code == 400
        # DEC-ENGINE-SPOT-ONLY-001: the alias is refused on EVERY coordinate
        # (the pre-flip "expected NSE/FNO" arm is gone) — the detail cites
        # the spot-only law; there is NO dashboard override by design.
        assert "DEC-ENGINE-SPOT-ONLY-001" in resp.json()["detail"]


# ────── DEC-ENGINE-SPOT-ONLY-001 — the spot-only DEFAULT refusal ──────


class TestSpotOnlyScopeRefusal:
    """The engine is SPOT-ONLY (DEC-ENGINE-SPOT-ONLY-001, operator directive
    2026-07-13 — consciously supersedes the ADR-0012 /
    DEC-NIFTY-FUTURES-SWEEP-LAUNCH-001 engine-scope authorization): a
    ``NSE-NIFTY-FUT`` sweep through the REAL production entry is REFUSED by
    default — loud, typed, citing the DEC + the remediation — and the
    refusal fires BEFORE any futures-series resolution or lake I/O."""

    def test_futures_alias_sweep_all_refused_by_default(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The fails-before regression: pre-flip this launch RAN a sweep."""
        lake_root, catalog_path = _seed_lake(tmp_path)
        results_root = tmp_path / "sweeps"
        rc = cmd_sweep_all(
            **_kwargs(lake_root, catalog_path, results_root, allow_out_of_scope=False)
        )
        err = capsys.readouterr().err
        assert rc == EXIT_USER_ERROR
        assert "sweep REFUSED" in err
        assert "DEC-ENGINE-SPOT-ONLY-001" in err
        assert "spot-only" in err
        assert "lake/REFERENCE_ONLY" in err
        # Nothing persisted — the refusal beats every store write.
        assert _persisted_frames(results_root) == []

    def test_refusal_fires_before_any_futures_series_resolution(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The refusal-before-I/O pin: a refused request never touches the
        futures resolver or the alias-aware slice loader (zero lake I/O)."""
        import brutex.data.futures_series as futures_series_module
        import brutex.feed.series_load as series_load_module

        def _never_resolves(*args: object, **kwargs: object) -> object:
            raise AssertionError("resolve_futures_series must never run on a scope-refused launch")

        def _never_loads(*args: object, **kwargs: object) -> object:
            raise AssertionError("load_sweep_slice must never run on a scope-refused launch")

        monkeypatch.setattr(futures_series_module, "resolve_futures_series", _never_resolves)
        monkeypatch.setattr(series_load_module, "load_sweep_slice", _never_loads)
        lake_root, catalog_path = _seed_lake(tmp_path, derive=False)
        rc = cmd_sweep_all(
            **_kwargs(lake_root, catalog_path, tmp_path / "sweeps", allow_out_of_scope=False)
        )
        err = capsys.readouterr().err
        assert rc == EXIT_USER_ERROR
        assert "DEC-ENGINE-SPOT-ONLY-001" in err
