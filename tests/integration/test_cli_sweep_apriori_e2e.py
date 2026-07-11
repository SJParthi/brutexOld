"""End-to-end apriori-extinction CLI proofs (DEC-SWEEP-APRIORI-DEFAULT-001, review fix round 1).

``cmd_sweep_all`` / ``cmd_sweep_verify`` are ``# pragma: no cover``
orchestration shims, so this suite is the honest FUNCTIONAL validation of the
round-1 review fixes over a REAL tiny Parquet lake (the
``test_cli_sweep_all_dedup.py`` harness pattern):

* **M4 (spec row 23)** — the operator-facing extinction copy EXECUTED for
  real: the ``APRIORI-TO-EXTINCTION`` banner, the ``hard guard: per-level
  mask·bar breaker`` preflight, the exact worst-case level-2 arithmetic line,
  the ``MODELED`` disclaimer, the deeper-levels ``DATA-DEPENDENT`` honesty
  line, and the done-line budget-trips remediation sentence.
* **n5 (spec test-16 second leg)** — a tiny-budget run TRIPS a tuple; the
  raised-budget re-run completes EXACTLY the refused tuple (checkpoint /
  no-sentinel law) while the already-completed tuples resume-skip.
* **M3 / m5 (row 21)** — the recorded-mode gate compares against the recount's
  ENGINE-EFFECTIVE mode: (a) a legacy tree recounted under an exported truthy
  ``BRUTEX_SWEEP_APRIORI`` + explicit ``--k`` downgrades to INCONCLUSIVE
  (never verdict-grade ``recount_divergence`` on a healthy tree); (b) an
  explicit-ladder-under-truthy-env tree re-verified the same way is a MATCHED
  mode — verdict-grade allowed, recount green.
* **m2** — an explicit-k NO-recount verify never hard-fails on a corrupt
  supervisor chain the mode gate would not consume (main's rc-0 behaviour).
* **m7** — the Telegram ``SweepWatchdog`` timeout is ladder-scaled on the
  extinction arm and stays the flat default on the legacy arm.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from brutex.cli import EXIT_OK, EXIT_VERIFY_INCONCLUSIVE, cmd_sweep_all, main
from brutex.data.catalog import Catalog
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.sink import ParquetBarSink
from brutex.engine.sweep_apriori import MAX_MASK_BARS_ENV, SWEEP_APRIORI_ENV

pytest.importorskip("polars")
pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")
pytest.importorskip("lmdb")

_IST = timezone(timedelta(hours=5, minutes=30))
_EXCHANGE = "NSE"
_SEGMENT = "INDEX"
_SYMBOL = "NIFTY"
_INTERVAL = "1minute"
_START = "2024-01-02"
_END = "2024-01-03"
_N_BARS = 8


@pytest.fixture(autouse=True)
def _sequential_sweep_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the sequential path explicitly (ADR-0016 §6) — 1-instrument grids."""
    monkeypatch.setenv("BRUTEX_SWEEP_WORKERS", "1")


@pytest.fixture(autouse=True)
def _restore_apriori_env() -> Iterator[None]:
    """Snapshot + restore ``BRUTEX_SWEEP_APRIORI`` around EVERY test here.

    The B1 leak class: ``cmd_sweep_all``'s extinction default setdefault-writes
    the var into ``os.environ`` — this fixture guarantees the pre-test state is
    restored no matter which production path wrote it.
    """
    before = os.environ.get(SWEEP_APRIORI_ENV)
    try:
        yield
    finally:
        if before is None:
            os.environ.pop(SWEEP_APRIORI_ENV, None)
        else:
            os.environ[SWEEP_APRIORI_ENV] = before


def _bars() -> list[CandleBar]:
    out: list[CandleBar] = []
    for i in range(_N_BARS):
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
    kwargs: dict[str, Any] = {
        "lake_root": lake_root,
        "catalog_path": catalog_path,
        "results_root": results_root,
        "exchange": _EXCHANGE,
        "segment": _SEGMENT,
        "symbols": _SYMBOL,
        "intervals": _INTERVAL,
        "start": _START,
        "end": _END,
        "directions": "BUY",
        "k": "extinction",
        "conditions": "0,1,2,3",  # n_unique = 4 → the extinction ladder k=1..4
        "sl": "none",
        "tp": "none",
        "top": 5,
        "mode": "A",
        "max_tuples": None,
        "max_combos": None,
        "data_snapshot_hash": None,
        "options_sim": False,
    }
    kwargs.update(overrides)
    return cmd_sweep_all(**kwargs)


def _verify_argv(lake_root: Path, catalog_path: Path, results_root: Path, *extra: str) -> list[str]:
    return [
        "sweep-verify",
        "--symbols",
        _SYMBOL,
        "--intervals",
        _INTERVAL,
        "--start",
        _START,
        "--end",
        _END,
        "--directions",
        "BUY",
        "--conditions",
        "0,1,2,3",
        "--top",
        "5",
        "--lake-root",
        str(lake_root),
        "--catalog",
        str(catalog_path),
        "--results-root",
        str(results_root),
        *extra,
    ]


def _tuple_stores(results_root: Path) -> list[Path]:
    return sorted((results_root / _SYMBOL).rglob("*.parquet"))


# ───────────────────────── M4: the executed copy pins ─────────────────────────


def test_extinction_launch_prints_the_operator_copy_for_real(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """M4 (spec row 23): the banner / hard-guard / worst-case arithmetic /
    MODELED / data-dependent lines all EXECUTE on a real extinction launch."""
    monkeypatch.setenv(SWEEP_APRIORI_ENV, "1")
    lake_root, catalog_path = _persist(tmp_path)
    rc = _sweep_all(lake_root, catalog_path, tmp_path / "sweeps")
    assert rc == EXIT_OK
    captured = capsys.readouterr()
    err = captured.err
    assert "sweep exploration: APRIORI-TO-EXTINCTION (default)" in err
    assert "hard guard: per-level mask·bar breaker" in err
    # The exact worst-case level-2 arithmetic line shape (C(4,2)=6 × 8 bars).
    assert (
        f"worst-case level-2 work, exact: {_SYMBOL}/{_INTERVAL} C(4,2)=6 x "
        f"{_N_BARS} bars = 48 mask·bar" in err
    )
    assert "MODELED default, not an" in err  # the honesty disclaimer token
    assert "deeper levels are DATA-DEPENDENT" in err
    # The k-axis banner names the ladder + the operative breaker budget.
    assert "k=1..4 (Apriori extinction mode; breaker budget" in captured.out


# ─────────── n5 + the done-line remediation: budget trip → raised re-run ───────────


def test_budget_trip_then_raised_budget_rerun_completes_the_refused_tuples(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """n5 (spec test-16 second leg) + the M4 done-line pin: a tiny-budget run
    trips the k=1 tuple (C(4,1)=4 × 8 bars = 32 > 20) and prints the
    remediation sentence; the raised-budget re-run completes EXACTLY the
    refused tuple (checkpoint / no-sentinel law) while the extinct k=2..4
    tuples — which enumerated 0 under the frozen frontier and completed —
    resume-skip untouched."""
    monkeypatch.setenv(SWEEP_APRIORI_ENV, "1")
    monkeypatch.setenv(MAX_MASK_BARS_ENV, "20")
    lake_root, catalog_path = _persist(tmp_path)
    results_root = tmp_path / "sweeps"

    rc_first = _sweep_all(lake_root, catalog_path, results_root)
    assert rc_first != EXIT_OK  # the budget trip is a loud non-zero verdict
    captured = capsys.readouterr()
    assert "FAILED mid-sweep" in captured.err
    # The mode-aware done-line remediation sentence (M4).
    assert "were mask·bar budget trips" in captured.err
    assert "raise BRUTEX_SWEEP_APRIORI_MAX_MASK_BARS" in captured.err
    assert "the frozen checkpoint re-runs exactly the refused tuples" in captured.err
    # The refused k=1 tuple left NO store; the extinct deeper tuples completed.
    first_stores = _tuple_stores(results_root)
    assert len(first_stores) == 3  # k=2, k=3, k=4 (k=1 refused, un-sentineled)

    # The raised-budget re-run (the ONE reversible lever): unset the tiny cap.
    monkeypatch.delenv(MAX_MASK_BARS_ENV)
    rc_second = _sweep_all(lake_root, catalog_path, results_root)
    assert rc_second == EXIT_OK
    out = capsys.readouterr().out
    # Exactly the refused tuple ran; nothing redid, nothing failed.
    assert "tuples=1/4" in out
    assert "failed=0)" in out
    second_stores = _tuple_stores(results_root)
    assert len(second_stores) == 4
    assert set(first_stores) <= set(second_stores)


# ─────────────── M3 + m5 (row 21): the engine-effective mode gate ───────────────


def test_recount_under_exported_truthy_env_downgrades_never_false_corrupts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """M3 arm (a) / m5: a LEGACY tree + ``BRUTEX_SWEEP_APRIORI=1`` exported +
    ``sweep-verify --k 1,2 --recount`` under an otherwise MATCHED context —
    the shadow re-sweep would run in apriori mode (n_enumerated diverges on a
    HEALTHY tree), so the gate must read the ENGINE-EFFECTIVE mode and
    downgrade to INCONCLUSIVE, never verdict-grade recount_divergence."""
    monkeypatch.delenv(SWEEP_APRIORI_ENV, raising=False)
    lake_root, catalog_path = _persist(tmp_path)
    results_root = tmp_path / "sweeps"
    assert _sweep_all(lake_root, catalog_path, results_root, k="1,2") == EXIT_OK
    capsys.readouterr()

    monkeypatch.setenv(SWEEP_APRIORI_ENV, "1")  # the operator's exported env
    rc = main(
        [
            *_verify_argv(lake_root, catalog_path, results_root, "--recount"),
            "--k",
            "1,2",
        ]
    )
    out = capsys.readouterr().out
    assert "exploration_mode: recorded legacy vs effective apriori-extinction" in out
    assert "recount REFUSED" in out  # the context-mismatch ladder, not a verdict
    assert "recount_divergence" not in out  # never false-corrupt a healthy tree
    assert rc == EXIT_VERIFY_INCONCLUSIVE


def test_explicit_ladder_under_truthy_env_tree_reverifies_verdict_grade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """M3 arm (b): a tree SWEPT with an explicit ladder under a truthy env
    (recorded apriori) and re-verified explicit + the same env is a MATCHED
    mode — the recount proceeds verdict-grade and reads GREEN (the earlier
    resolver-verdict comparand over-conservatively forced INCONCLUSIVE)."""
    monkeypatch.setenv(SWEEP_APRIORI_ENV, "1")
    lake_root, catalog_path = _persist(tmp_path)
    results_root = tmp_path / "sweeps"
    assert _sweep_all(lake_root, catalog_path, results_root, k="1,2") == EXIT_OK
    capsys.readouterr()

    rc = main(
        [
            *_verify_argv(lake_root, catalog_path, results_root, "--recount"),
            "--k",
            "1,2",
        ]
    )
    out = capsys.readouterr().out
    assert "exploration_mode:" not in out  # matched mode — no mismatch line
    assert "recount REFUSED" not in out
    assert "recount_divergence" not in out
    assert rc == EXIT_OK


# ──────────── m2: explicit-k no-recount verify skips the mode read ────────────


def test_explicit_k_no_recount_verify_survives_a_corrupt_supervisor_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """m2: the mode read runs ONLY when it can matter (recount requested OR the
    default/extinction resolution) — an explicit-k, no-recount verify over a
    tree with a CORRUPT supervisor chain returns rc 0 with the report, exactly
    like main did before the gate existed."""
    monkeypatch.delenv(SWEEP_APRIORI_ENV, raising=False)
    lake_root, catalog_path = _persist(tmp_path)
    results_root = tmp_path / "sweeps"
    assert _sweep_all(lake_root, catalog_path, results_root, k="1,2") == EXIT_OK
    # Corrupt the SUPERVISOR chain: a malformed but newline-TERMINATED line is
    # real (committed) corruption — read_entries/iter_entries raise on it.
    sup_path = results_root / "audit" / "sweep-all-supervisor.jsonl"
    with sup_path.open("a", encoding="utf-8") as fh:
        fh.write("{not jsonl\n")
    capsys.readouterr()

    rc = main([*_verify_argv(lake_root, catalog_path, results_root), "--k", "1,2"])
    captured = capsys.readouterr()
    assert rc == EXIT_OK  # the gate was skipped — no hard fail on the chain
    assert "unreadable audit log" not in captured.err
    assert "sweep-verify report" in captured.out or "structural" in captured.out.lower()


# ──────────────── m7: the ladder-scaled Telegram watchdog timeout ────────────────


def _capture_watchdog_timeouts(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    import brutex.engine.sweep_watchdog as watchdog_mod

    captured: list[float] = []
    real = watchdog_mod.SweepWatchdog

    class _Spy(real):  # type: ignore[misc, valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            captured.append(kwargs.get("timeout_seconds", watchdog_mod.DEFAULT_TIMEOUT_SECONDS))
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(watchdog_mod, "SweepWatchdog", _Spy)
    return captured


def test_extinction_launch_scales_the_telegram_watchdog_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """m7: the extinction launch's SweepWatchdog timeout rides the SAME
    ladder-aware capped bound as the parallel stall guard (floored at the
    default) — a healthy mid-ladder level can never false-CRITICAL."""
    from brutex.engine.sweep_parallel import apriori_ladder_stall_bound_seconds
    from brutex.engine.sweep_watchdog import DEFAULT_TIMEOUT_SECONDS

    monkeypatch.setenv(SWEEP_APRIORI_ENV, "1")
    # A tiny throughput floor makes the scaled bound dominate the 300 s default
    # even on this 8-bar lake, so the wiring is observable.
    monkeypatch.setenv("BRUTEX_SWEEP_STALL_MASK_BARS_PER_SEC", "0.001")
    lake_root, catalog_path = _persist(tmp_path)
    captured = _capture_watchdog_timeouts(monkeypatch)

    assert _sweep_all(lake_root, catalog_path, tmp_path / "sweeps") == EXIT_OK
    expected = max(
        DEFAULT_TIMEOUT_SECONDS,
        apriori_ladder_stall_bound_seconds(k=4, n_unique=4, n_bars=_N_BARS),
    )
    assert captured == [expected]
    assert expected > DEFAULT_TIMEOUT_SECONDS  # the scaled bound genuinely engaged


def test_legacy_launch_keeps_the_flat_default_watchdog_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """m7: the legacy arm is untouched — the flat default timeout, even under
    the same tiny throughput-floor env."""
    from brutex.engine.sweep_watchdog import DEFAULT_TIMEOUT_SECONDS

    monkeypatch.delenv(SWEEP_APRIORI_ENV, raising=False)
    monkeypatch.setenv("BRUTEX_SWEEP_STALL_MASK_BARS_PER_SEC", "0.001")
    lake_root, catalog_path = _persist(tmp_path)
    captured = _capture_watchdog_timeouts(monkeypatch)

    assert _sweep_all(lake_root, catalog_path, tmp_path / "sweeps", k="1,2") == EXIT_OK
    assert captured == [DEFAULT_TIMEOUT_SECONDS]
