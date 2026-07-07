"""Integration — the ADR-0012 NIFTY-futures sweep LAUNCH, end to end.

DEC-NIFTY-FUTURES-SWEEP-LAUNCH-001 / DEC-FUTURES-ADDRESSING-PIN-001: the REAL
``cmd_sweep_all`` is driven over REAL Parquet mini-lakes (written with the
same ``ParquetBarSink`` + ``materialize_lake_timeframes`` the pull pipeline
uses) under ``--symbols NSE-NIFTY-FUT --exchange NSE --segment FNO``,
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
    store = InstrumentStore.open(lake_root / "instruments.duckdb")
    try:
        with TestClient(build_app(DashboardState(), instrument_store=store)) as client:
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
    finally:
        store.close()


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

#: Good-Friday adjacency (real 2026 NSE calendar): expiry Thu 2026-04-02, then
#: Fri 2026-04-03 (Good Friday) + the weekend — a SESSIONLESS overhang.
_APR26 = ("NSE-NIFTY-02Apr26-FUT", date(2026, 4, 2))
_MAY26 = "NSE-NIFTY-30Apr26-FUT"


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
    bars = [b for day in (date(2026, 4, 1), _APR26[1]) for b in _day_bars(day, 100.0)]
    sink.append(exchange="NSE", segment="FNO", symbol=_APR26[0], interval="1minute", bars=bars)
    results_root = tmp_path / "sweeps"
    kwargs = _kwargs(
        lake_root,
        catalog_path,
        results_root,
        intervals="1minute",
        start="2026-04-01",
        end="2026-04-05",  # 3-day tail overhang past the Apr-2 expiry
    )
    rc = cmd_sweep_all(**kwargs)
    assert rc == EXIT_OK

    # The successor contract is pulled between launches. Its in-window owned
    # span (Apr 3 → Apr 5) holds ZERO sessions, so it joins the roll schedule
    # while contributing ZERO bars — the snapshot hash stays byte-identical.
    (lake_root / "bars" / "NSE" / "FNO" / _MAY26).mkdir()
    capsys.readouterr()
    rc = cmd_sweep_all(**kwargs)
    err = capsys.readouterr().err
    assert rc == EXIT_USER_ERROR
    assert "sweep-all REFUSED" in err
    assert str([_APR26[1].toordinal()]) in err  # the RECORDED schedule
    assert str([_APR26[1].toordinal(), date(2026, 4, 30).toordinal()]) in err  # the FRESH one
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
    """F8: the alias needs NSE/FNO — the Layer-2 scope gate refuses INDEX."""
    lake_root, catalog_path = _seed_lake(tmp_path, derive=False)
    rc = cmd_sweep_all(**_kwargs(lake_root, catalog_path, tmp_path / "sweeps", segment="INDEX"))
    err = capsys.readouterr().err
    assert rc == EXIT_USER_ERROR
    assert "NSE/FNO" in err


def test_mixed_spot_and_futures_symbols_under_one_segment_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """F8: a combined spot+futures study is TWO launches by design — one
    (exchange, segment) coordinate cannot satisfy both symbol families."""
    lake_root, catalog_path = _seed_lake(tmp_path, derive=False)
    rc = cmd_sweep_all(
        **_kwargs(lake_root, catalog_path, tmp_path / "sweeps", symbols=f"NIFTY,{_ALIAS}")
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
    store = InstrumentStore.open(tmp_path / "instruments.duckdb")
    try:
        with TestClient(build_app(DashboardState(), instrument_store=store)) as client:
            resp = client.post("/api/v1/sweep/run", params={"symbols": _ALIAS})
            assert resp.status_code == 400
            assert "NSE/FNO" in resp.json()["detail"]
    finally:
        store.close()
