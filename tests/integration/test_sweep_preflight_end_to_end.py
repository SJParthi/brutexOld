# pyright: reportPrivateUsage=none
# Whitebox: drives the private pull-site seams (`cli._enter_pull_singleflight`,
# `BackgroundPullJob._acquire_singleflight`) directly — the house pattern of
# tests/unit/test_cli_retry_storm.py.
"""End-to-end integration of DEC-SWEEP-PREFLIGHT-ESTIMATOR-001 Phase 1 (WP7).

The BUILD-SPEC §10.1 integration twins — extremes 4 / 6 / 8 / 9 / 16 / 17 —
driven through the REAL ``cli.cmd_sweep_all`` over REAL tmp lakes written with
the SAME writers ingest uses (``ParquetWriter``/``ParquetBarSink``) and REAL
window ledgers (``save_window_ledger``):

* **4 (R4+O-a)** refuse-whole names the COMPLETE not-ready pair list + exact
  missing spans in ONE stderr text; READY pairs are NOT run (no store dirs).
* **6 (R4/G1)** a present-but-STALE derived TF passes the NEW pre-load gate
  and is refused by the SHIPPED #1492 freshness gate with its own remediation
  — the new pipeline did not perturb it.
* **8 (R4)** DISK verdict wins over ledger SUCCESS rows — presence is never
  read from the ledger.
* **9 (R4/G10)** both-way lock refusals: a held pull ``LOCK_EX`` refuses the
  sweep PRE-write; a held sweep ``LOCK_SH`` refuses the pull at BOTH
  production pull sites (``cli._enter_pull_singleflight`` +
  ``pull_jobs._acquire_singleflight``); the TOCTOU race ends with the sweep
  refusing at the guard's RE-probe (both sides refuse, fail-closed).
* **16 (R6/R44)** a re-click of the SAME selection passes every new gate and
  dedups fast (0 recomputed, ``resumed=2``); spies pin ZERO writes before the
  job start (first ``Catalog.open``) and the dedup-plan call byte-unchanged.
* **17 (R42)** EVERY refusal class leaves the tree UNTOUCHED — a recursive
  ``(path, mtime_ns, size)`` snapshot before/after is IDENTICAL: no
  ``catalog.duckdb``, no ``out_root``, no ledger delta, no calibration write,
  and NO ``.locks/sweep.lock`` (the lock exemption applies ONLY to the
  proceed path — asserted exactly).
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

from brutex import cli
from brutex.adapters.groww.pull_singleflight import pull_singleflight_lock
from brutex.data.catalog import Catalog
from brutex.data.interior_gap_detect import INTERIOR_GAP
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.sink import ParquetBarSink
from brutex.data.sweep_exclusion import sweep_lock_path, sweep_run_guard
from brutex.data.window_ledger import FAILED, SUCCESS, WindowLedger
from brutex.data.window_ledger_store import ledger_path, save_window_ledger
from brutex.engine.sweep_data_preflight import clamp_dates

_IST = timezone(timedelta(hours=5, minutes=30))
_EXCHANGE = "NSE"
_SEGMENT = "INDEX"
_START = date(2024, 1, 2)
_END = date(2024, 1, 3)
#: Canonical expected sessions of the base test range, from the REAL clamp —
#: the fixtures seed exactly these dates (never a hand-guessed calendar). A
#: fixed past `today` keeps the pin stable; the production shim's real-clock
#: clamp yields the SAME list for any today ≥ 2024 (end bound ≥ requested end).
_EXPECTED = clamp_dates(_START, _END, today=date(2026, 7, 7)).expected_sessions


@pytest.fixture(autouse=True)
def _sequential_sweep_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the sequential sweep path (ADR-0016 §6) — determinism over tiny grids."""
    monkeypatch.setenv("BRUTEX_SWEEP_WORKERS", "1")


def _minute_bars(day: date, n: int = 8) -> list[CandleBar]:
    """A short in-session minute window on *day* (stored UTC), valid OHLC."""
    out: list[CandleBar] = []
    for i in range(n):
        ts = datetime(day.year, day.month, day.day, 9, 16 + i, tzinfo=_IST).astimezone(UTC)
        close = Decimal("100.00") + Decimal(i) / 10
        out.append(
            CandleBar(
                timestamp=ts,
                open=close - Decimal("0.05"),
                high=close + Decimal("0.10"),
                low=close - Decimal("0.10"),
                close=close,
                volume=1000,
                open_interest=None,
            )
        )
    return out


def _daily_bar(day: date) -> list[CandleBar]:
    """One midnight-IST daily bar on *day* (the derived-1day lake shape)."""
    ts = datetime(day.year, day.month, day.day, 0, 0, tzinfo=_IST).astimezone(UTC)
    return [
        CandleBar(
            timestamp=ts,
            open=Decimal("100.00"),
            high=Decimal("101.00"),
            low=Decimal("99.00"),
            close=Decimal("100.50"),
            volume=10000,
            open_interest=None,
        )
    ]


def _seed(
    tmp_path: Path,
    coverage: dict[tuple[str, str], tuple[date, ...]],
) -> tuple[Path, Path]:
    """Write a real lake: per ``(symbol, interval)`` one bar-set per session date."""
    lake_root = tmp_path / "lake"
    catalog_path = tmp_path / "catalog.duckdb"
    writer = ParquetWriter(lake_root=lake_root)
    with Catalog.open(catalog_path) as cat:
        sink = ParquetBarSink(writer=writer, catalog=cat)
        for (symbol, interval), days in coverage.items():
            for day in days:
                bars = _daily_bar(day) if interval == "1day" else _minute_bars(day)
                sink.append(
                    exchange=_EXCHANGE,
                    segment=_SEGMENT,
                    symbol=symbol,
                    interval=interval,
                    bars=bars,
                )
    return lake_root, catalog_path


def _sweep_all(lake_root: Path, catalog_path: Path, results_root: Path, **overrides: Any) -> int:
    """Drive the REAL ``cli.cmd_sweep_all`` over the seeded lake."""
    kwargs: dict[str, Any] = {
        "lake_root": lake_root,
        "catalog_path": catalog_path,
        "results_root": results_root,
        "exchange": _EXCHANGE,
        "segment": _SEGMENT,
        "symbols": "NIFTY",
        "intervals": "1minute",
        "start": _START.isoformat(),
        "end": _END.isoformat(),
        "directions": "BUY",
        "k": "1,2",
        "conditions": "0,1,2,3",  # tiny 4-bit vocabulary → a fast real sweep
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


def _tree_snapshot(root: Path) -> list[tuple[str, int, int]]:
    """Recursive ``(relpath, size, mtime_ns)`` snapshot — the nothing-touched oracle."""
    out: list[tuple[str, int, int]] = []
    for path in sorted(root.rglob("*")):
        stat = path.stat()
        out.append((str(path.relative_to(root)), stat.st_size, stat.st_mtime_ns))
    return out


_NOW = "2026-07-01T00:00:00+00:00"


def _ledger_row(
    security_id: str,
    interval: str,
    window_start: str,
    window_end: str,
    status: str,
    last_error: str | None,
) -> tuple[
    str, str, str, str, str, str, int, int, str | None, str, str | None, int, int, int | None
]:
    """One raw window-ledger row tuple (the ``WindowLedger.from_rows`` shape)."""
    return (
        "groww",
        security_id,
        interval,
        window_start,
        window_end,
        status,
        0,
        1,
        last_error,
        _NOW,
        _NOW,
        0,
        1,
        None,
    )


# ────────────────────────────────────────────────────────────────────────
# Extreme 4 — refuse-whole names ALL pairs + spans; ready pairs NOT run
# ────────────────────────────────────────────────────────────────────────


def test_r4_oa_partial_availability_refuses_whole_naming_all_pairs_and_spans(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """R4+O-a extreme 4: 2 symbols × 2 TFs, one pair missing a 3-session run
    AND a second broken pair — ONE refusal names BOTH with exact spans; the
    READY pairs are named ready but NOT run (no store dirs created)."""
    start, end = date(2024, 1, 2), date(2024, 1, 8)
    expected = clamp_dates(start, end, today=date(2026, 7, 7)).expected_sessions
    assert len(expected) == 5  # Jan 2, 3, 4, 5, 8 — 2024 NSE sessions
    hole = expected[1:4]  # 3 contiguous sessions missing for BANKNIFTY/1minute
    lake_root, catalog_path = _seed(
        tmp_path,
        {
            ("NIFTY", "1minute"): expected,
            ("NIFTY", "1day"): expected,
            ("BANKNIFTY", "1minute"): (expected[0], expected[4]),
            # BANKNIFTY/1day: nothing on disk at all — a SECOND broken pair.
        },
    )
    results_root = tmp_path / "sweeps"

    rc = _sweep_all(
        lake_root,
        catalog_path,
        results_root,
        symbols="NIFTY,BANKNIFTY",
        intervals="1minute,1day",
        start=start.isoformat(),
        end=end.isoformat(),
    )

    assert rc == cli.EXIT_USER_ERROR
    captured = capsys.readouterr()
    # The estimate headline still printed (R20 — shown beside the refusal).
    assert "trials" in captured.out
    err = captured.err
    assert "sweep preflight REFUSED" in err
    # BOTH broken pairs named in the SAME text (gather-all, refuse-whole) …
    assert "BANKNIFTY × 1minute" in err
    assert "BANKNIFTY × 1day" in err
    # … with the EXACT missing span (contiguous expected-sequence run) …
    assert f"{hole[0].isoformat()}→{hole[-1].isoformat()} (3 sessions)" in err
    # … and the honest remediation (the sweep NEVER pulls — R3).
    assert "pull on /universe first" in err
    # The READY pairs (NIFTY) were NOT run: nothing was created anywhere.
    assert not results_root.exists()
    assert not (lake_root / ".locks" / "sweep.lock").exists()


# ────────────────────────────────────────────────────────────────────────
# Extreme 6 — the SHIPPED freshness gate is unperturbed (post-load belt)
# ────────────────────────────────────────────────────────────────────────


def test_r4_stale_derived_tf_still_refused_g1_gate_green(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """R4/G1 extreme 6: a PRESENT-but-STALE 5minute partition passes the NEW
    pre-load PRESENCE gate and is refused post-load by the SHIPPED #1492
    freshness gate with its own remediation — module + call site unperturbed."""
    day = _EXPECTED[0]
    lake_root, catalog_path = _seed(tmp_path, {("NIFTY", "1minute"): (day,)})
    # The drift shape: the 5minute partition holds RAW 1-minute bars (what a
    # stray write / pre-anchor-law derive leaves) — PRESENT on disk, STALE
    # versus a fresh 1m re-derivation.
    writer = ParquetWriter(lake_root=lake_root)
    with Catalog.open(catalog_path) as cat:
        sink = ParquetBarSink(writer=writer, catalog=cat)
        sink.append(
            exchange=_EXCHANGE,
            segment=_SEGMENT,
            symbol="NIFTY",
            interval="5minute",
            bars=_minute_bars(day),
        )

    rc = _sweep_all(
        lake_root,
        catalog_path,
        tmp_path / "sweeps",
        intervals="1minute,5minute",
        start=day.isoformat(),
        end=day.isoformat(),
    )

    assert rc == cli.EXIT_USER_ERROR
    captured = capsys.readouterr()
    # The NEW gate passed (presence-only pre-load; headline printed, no
    # preflight refusal) …
    assert "sweep preflight REFUSED" not in captured.err
    assert "trials" in captured.out
    # … and the SHIPPED freshness gate refused with its own text, verbatim
    # shape (DEC-SUSPICION-SWEEP-FIXES-001 G1 — byte-untouched post-load).
    assert "STALE" in captured.err
    assert "NIFTY/5minute" in captured.err
    assert "re-derivation" in captured.err


# ────────────────────────────────────────────────────────────────────────
# Extreme 8 — DISK verdict wins over ledger SUCCESS rows
# ────────────────────────────────────────────────────────────────────────


def test_r4_disk_verdict_wins_over_ledger_success(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """R4 extreme 8: ledger SUCCESS rows spanning the window + a DELETED month
    parquet ⇒ COVERAGE_MISSING naming the span — presence is read from DISK,
    never from the ledger."""
    lake_root, catalog_path = _seed(tmp_path, {("NIFTY", "1minute"): _EXPECTED})
    # Seed a ledger that CLAIMS the whole window pulled SUCCESSfully.
    ledger = WindowLedger.from_rows(
        [
            _ledger_row(
                "NSE-NIFTY",
                "1minute",
                _START.isoformat(),
                _END.isoformat(),
                SUCCESS,
                None,
            )
        ]
    )
    save_window_ledger(ledger, lake_root)
    # Delete the month parquet — the DISK truth now has ZERO sessions.
    month_file = (
        lake_root / "bars" / _EXCHANGE / _SEGMENT / "NIFTY" / "1minute" / "2024" / "01.parquet"
    )
    assert month_file.is_file()
    month_file.unlink()

    rc = _sweep_all(lake_root, catalog_path, tmp_path / "sweeps")

    assert rc == cli.EXIT_USER_ERROR
    err = capsys.readouterr().err
    assert "coverage_missing" in err
    assert "NIFTY × 1minute" in err
    # The whole expected range is the missing span — named exactly.
    assert f"{_EXPECTED[0].isoformat()}→{_EXPECTED[-1].isoformat()} (2 sessions)" in err
    assert "pull on /universe first" in err


# ────────────────────────────────────────────────────────────────────────
# Extreme 9 — both-way lock refusals + the TOCTOU both-refuse arm
# ────────────────────────────────────────────────────────────────────────


def test_r4_g10_live_pull_lock_refuses_sweep_pre_write(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """R4/G10 extreme 9 (sweep side): a REAL held pull LOCK_EX refuses the
    sweep at the G0 probe — PRE-write (nothing created, not even sweep.lock)."""
    lake_root, catalog_path = _seed(tmp_path, {("NIFTY", "1minute"): _EXPECTED})
    results_root = tmp_path / "sweeps"
    with pull_singleflight_lock(lake_root):  # a real pull is mid-flight
        before = _tree_snapshot(tmp_path)
        rc = _sweep_all(lake_root, catalog_path, results_root)
        after = _tree_snapshot(tmp_path)

    assert rc == cli.EXIT_USER_ERROR
    err = capsys.readouterr().err
    assert "pull_in_flight" in err
    assert "a pull is running for this lake" in err
    assert "lock wins" in err
    assert before == after  # PRE-write: byte-nothing touched
    assert not results_root.exists()
    assert not sweep_lock_path(lake_root).exists()


def test_r4_g10_held_sweep_lock_refuses_pull_at_both_sites(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """R4/G10 extreme 9 (pull side): a held sweep LOCK_SH refuses the pull at
    BOTH production pull sites — the CLI ``_enter_pull_singleflight`` and the
    dashboard ``pull_jobs._acquire_singleflight`` (P5's edit; the authoritative
    pass is the whole-package gate)."""
    import contextlib

    from brutex.dashboard.pull_jobs import BackgroundPullJob
    from brutex.dashboard.pull_progress import STATE_ERROR, PullProgress

    lake_root, _ = _seed(tmp_path, {("NIFTY", "1minute"): _EXPECTED})
    with sweep_run_guard(lake_root):
        # CLI pull site — the write-free PRE-probe refuses.
        with contextlib.ExitStack() as stack:
            ok = cli._enter_pull_singleflight(stack, lake_root, account_key_resolver=lambda: None)
        assert ok is False
        assert "sweep-vs-pull exclusion" in capsys.readouterr().err
        # Dashboard pull site — the job's acquire refuses and fails progress
        # with the honest sweep-exclusion message.
        progress = PullProgress()
        job = BackgroundPullJob(
            progress=progress,
            lake_root_resolver=lambda: lake_root,
            account_key_resolver=lambda: None,
        )
        assert job._acquire_singleflight() is False
        snap = progress.snapshot()
        assert snap.state == STATE_ERROR
        assert "sweep" in snap.detail.lower()


def test_r4_g10_toctou_pull_starting_mid_scan_refuses_at_guard_reprobe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """R4/G10 extreme 9 (TOCTOU): a pull that starts BETWEEN the probe-only
    scan and the guard acquire is caught at the guard's RE-probe — the sweep
    refuses (lock wins; the racing pull's own re-probe refuses too, so BOTH
    sides refuse — fail-closed, unit-pinned in test_data_sweep_exclusion.py)."""
    from brutex.data import sweep_exclusion

    lake_root, catalog_path = _seed(tmp_path, {("NIFTY", "1minute"): _EXPECTED})
    results_root = tmp_path / "sweeps"
    calls: list[int] = []
    real_probe = sweep_exclusion.pull_in_progress

    def _flip(root: Path) -> bool:
        # Call 1 = the guard's step-1 probe (no pull yet); call 2 = the
        # guard's step-3 RE-probe (a pull slipped in mid-acquire). The gate's
        # G0 probe rides sweep_gate's own import binding — unpatched, real.
        calls.append(1)
        if len(calls) == 1:
            return real_probe(root)
        return True

    monkeypatch.setattr(sweep_exclusion, "pull_in_progress", _flip)

    rc = _sweep_all(lake_root, catalog_path, results_root)

    assert rc == cli.EXIT_USER_ERROR
    err = capsys.readouterr().err
    assert "sweep-all REFUSED" in err
    assert "a pull/prebuild is running for this lake" in err
    assert len(calls) == 2  # probe + RE-probe, exactly
    # The guard released its fd; no run artifacts were created.
    assert not results_root.exists()


# ────────────────────────────────────────────────────────────────────────
# Extreme 16 — re-click same selection dedups fast; zero writes pre-job
# ────────────────────────────────────────────────────────────────────────


def test_r44_reclick_same_selection_dedup_skips_fast(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """R6/R44 extreme 16: the SAME 2-tuple micro-sweep twice through the gated
    ``cmd_sweep_all`` — the second launch passes every new gate, recomputes 0
    trials (``resumed=2``), and spies pin ZERO writes before the job start
    (the tree at the first ``Catalog.open`` == the pre-launch tree) plus the
    dedup-plan call byte-unchanged (``digest_path=True``, once per launch)."""
    from brutex.engine import sweep_launch

    lake_root, catalog_path = _seed(tmp_path, {("NIFTY", "1minute"): _EXPECTED})
    results_root = tmp_path / "sweeps"

    rc1 = _sweep_all(lake_root, catalog_path, results_root)
    assert rc1 == cli.EXIT_OK
    first_out = capsys.readouterr().out
    assert "tuples=2/2" in first_out

    # Spy 1 — zero writes before job start: snapshot the whole tree at the
    # FIRST Catalog.open of the second launch and compare to the pre-launch
    # tree (every gate before it is probe-only; the pre-existing sweep.lock
    # sidecar is re-opened, never rewritten).
    pre_launch = _tree_snapshot(tmp_path)
    at_open: dict[str, list[tuple[str, int, int]]] = {}
    real_open = Catalog.open.__func__  # type: ignore[attr-defined]

    def _spy_open(cls: Any, path: Path, *args: Any, **kwargs: Any) -> Any:
        if "snap" not in at_open:
            at_open["snap"] = _tree_snapshot(tmp_path)
        return real_open(cls, path, *args, **kwargs)

    monkeypatch.setattr(Catalog, "open", classmethod(_spy_open))

    # Spy 2 — the dedup-plan call order byte-unchanged by the new gates.
    plans: list[dict[str, Any]] = []
    real_plan = sweep_launch.plan_launch_dedup

    def _spy_plan(grid: Any, **kwargs: Any) -> Any:
        plans.append(dict(kwargs))
        return real_plan(grid, **kwargs)

    monkeypatch.setattr(sweep_launch, "plan_launch_dedup", _spy_plan)

    rc2 = _sweep_all(lake_root, catalog_path, results_root)

    assert rc2 == cli.EXIT_OK
    assert at_open["snap"] == pre_launch  # ZERO writes before the job start
    assert plans == [{"digest_path": True}]  # exactly one plan, digest path
    out = capsys.readouterr().out
    # The re-click dedups: every tuple resumes, nothing recomputed.
    assert "resumed=2" in out
    assert "evaluated=0" in out
    assert "tuples=0/2" in out


# ────────────────────────────────────────────────────────────────────────
# --estimate-only — the payload IS the deliverable; NOTHING created (§3.2 f)
# ────────────────────────────────────────────────────────────────────────


def test_estimate_only_prints_payload_and_creates_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """§3.2 step f: an ok --estimate-only run exits EXIT_OK with the JSON
    payload on stdout and creates NOTHING — no catalog write, no out_root,
    no sweep.lock (the tree snapshot is identical)."""
    import json

    lake_root, catalog_path = _seed(tmp_path, {("NIFTY", "1minute"): _EXPECTED})
    results_root = tmp_path / "sweeps"
    before = _tree_snapshot(tmp_path)

    rc = _sweep_all(lake_root, catalog_path, results_root, estimate_only=True)

    assert rc == cli.EXIT_OK
    assert _tree_snapshot(tmp_path) == before  # nothing created anywhere
    assert not results_root.exists()
    assert not sweep_lock_path(lake_root).exists()
    out = capsys.readouterr().out
    payload = json.loads(out[out.index("{") :])
    assert payload["schema_version"] == 1
    assert payload["ok"] is True
    assert payload["estimate"]["tuples"] == 2
    assert payload["confirm_token"] is None  # CLI supplies no lake_token


# ────────────────────────────────────────────────────────────────────────
# Extreme 17 — R42: EVERY refusal class touches NOTHING on disk
# ────────────────────────────────────────────────────────────────────────


def _assert_refusal_touches_nothing(
    tmp_path: Path,
    *,
    code: str,
    arrange: Any = None,
    overrides: dict[str, Any] | None = None,
    seed: dict[tuple[str, str], tuple[date, ...]] | None = None,
) -> str:
    """Drive one refusal class through the REAL shim and snapshot-pin R42."""
    lake_root, catalog_path = _seed(
        tmp_path, seed if seed is not None else {("NIFTY", "1minute"): _EXPECTED}
    )
    results_root = tmp_path / "sweeps"
    hold = arrange(lake_root) if arrange is not None else None
    try:
        before = _tree_snapshot(tmp_path)
        rc = _sweep_all(lake_root, catalog_path, results_root, **(overrides or {}))
        after = _tree_snapshot(tmp_path)
    finally:
        if hold is not None:
            hold.__exit__(None, None, None)
    assert rc == cli.EXIT_USER_ERROR
    assert after == before, f"{code}: the refusal touched the tree"
    assert not results_root.exists()  # no out_root
    assert not (tmp_path / "sweeps" / "calibration").exists()  # no calibration write
    assert not sweep_lock_path(lake_root).exists()  # NOT EVEN sweep.lock
    return code


def test_r42_preflight_fail_touches_nothing_on_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """R42 extreme 17: every refusal class — pull-lock, clamp, coverage,
    quarantine, ledger-unreadable, SLA, disk — leaves the recursive
    ``(path, mtime_ns, size)`` snapshot of lake + out_root IDENTICAL; no
    ``catalog.duckdb`` mutation, no out_root, no ledger delta, no calibration
    write, and NO ``.locks/sweep.lock`` (probe-only refusals; the sweep.lock
    exemption applies ONLY to the proceed path)."""

    # (a) pull-lock in flight.
    def _hold_pull(lake_root: Path) -> Any:
        cm = pull_singleflight_lock(lake_root)
        cm.__enter__()
        return cm

    _assert_refusal_touches_nothing(tmp_path / "a", code="pull_in_flight", arrange=_hold_pull)
    assert "pull_in_flight" in capsys.readouterr().err

    # (b) clamp: reversed range.
    _assert_refusal_touches_nothing(
        tmp_path / "b",
        code="reversed_range",
        overrides={"start": _END.isoformat(), "end": _START.isoformat()},
    )
    assert "reversed_range" in capsys.readouterr().err

    # (c) coverage: a session hole (only the first expected session seeded).
    _assert_refusal_touches_nothing(
        tmp_path / "c",
        code="coverage_missing",
        seed={("NIFTY", "1minute"): (_EXPECTED[0],)},
    )
    assert "coverage_missing" in capsys.readouterr().err

    # (d) quarantine: a demoted-OPEN ledger window overlapping the range.
    def _seed_quarantine(lake_root: Path) -> None:
        ledger = WindowLedger.from_rows(
            [
                _ledger_row(
                    "NSE-NIFTY",
                    "1minute",
                    _START.isoformat(),
                    _END.isoformat(),
                    FAILED,
                    INTERIOR_GAP,
                )
            ]
        )
        save_window_ledger(ledger, lake_root)

    class _NullCm:
        def __exit__(self, *args: object) -> None:
            """No-op release (the arrangement is a plain write, not a lock)."""

    def _arrange_quarantine(lake_root: Path) -> Any:
        _seed_quarantine(lake_root)
        return _NullCm()

    _assert_refusal_touches_nothing(
        tmp_path / "d", code="quarantined_range", arrange=_arrange_quarantine
    )
    assert "quarantined_range" in capsys.readouterr().err

    # (e) ledger unreadable: garbage bytes — fail-CLOSED.
    def _arrange_garbage_ledger(lake_root: Path) -> Any:
        path = ledger_path(lake_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"garbage, not a parquet ledger")
        return _NullCm()

    _assert_refusal_touches_nothing(
        tmp_path / "e", code="ledger_unreadable", arrange=_arrange_garbage_ledger
    )
    assert "ledger_unreadable" in capsys.readouterr().err

    # (f) SLA overrun (the relocated anchor-12 verdict).
    monkeypatch.setenv("BRUTEX_SWEEP_PER_TUPLE_SECONDS", "1e7")
    _assert_refusal_touches_nothing(tmp_path / "f", code="sla_overrun")
    assert "sla_overrun" in capsys.readouterr().err
    monkeypatch.delenv("BRUTEX_SWEEP_PER_TUPLE_SECONDS")

    # (g) disk: fail-CLOSED refusal on an insufficient results volume.
    from brutex.engine import sweep_gate

    monkeypatch.setattr(sweep_gate, "volume_free_bytes", lambda root: 0)
    _assert_refusal_touches_nothing(tmp_path / "g", code="disk_space")
    assert "disk_space" in capsys.readouterr().err
