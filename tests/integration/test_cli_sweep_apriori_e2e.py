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
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from brutex.cli import EXIT_OK, EXIT_USER_ERROR, EXIT_VERIFY_INCONCLUSIVE, cmd_sweep_all, main
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
    """m2 (the SURVIVAL law): an explicit-k, no-recount verify over a tree with
    a CORRUPT supervisor chain never HARD-fails on the chain read — the
    apriori mode read runs only when it can matter, and the overfit-gate
    recovery (DEC-SWEEP-OVERFIT-RECOUNT-RECOVERY-001) is per-chain TOLERANT
    (one loud ``sweep_verify.overfit_recovery_chain_unreadable`` WARNING,
    never a raise / rc 1).

    HONEST expectation shift (both directions, Golden Rule #10): this test
    originally pinned rc 0 — true while the tree was UNGATED (pre
    default-on-overfit). With the default-ON overfit DSR gate the sweep's
    stores live at GATED addresses, and a ``cmd_sweep_all`` tree carries the
    recorded gate config on the SUPERVISOR chain ONLY (its ingest chain holds
    per-tuple SWEEP_START lines — not the START family), so corrupting that
    one chain makes the config UNRECOVERABLE: the honest verdict is the named
    ``grid_mismatch`` finding (rc 3 — correct-or-absent, never a fabricated
    default config), exactly what this shape read at the fold base too
    (pre-channel the test was ALREADY red rc 3; the channel's interim
    non-tolerant read briefly made it rc 1 — the regression this pin kills)."""
    from brutex.cli import EXIT_VERIFY_FINDINGS
    from structlog.testing import capture_logs

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

    with capture_logs() as logs:
        rc = main([*_verify_argv(lake_root, catalog_path, results_root), "--k", "1,2"])
    captured = capsys.readouterr()
    # SURVIVAL: never a hard fail (rc 1) on the corrupt chain, never the
    # "unreadable audit log" refusal — the verify runs to a real report.
    assert rc == EXIT_VERIFY_FINDINGS
    assert "unreadable audit log" not in captured.err
    assert "sweep-verify report" in captured.out or "structural" in captured.out.lower()
    # The tolerant recovery warned EXACTLY once for the one corrupt chain.
    warned = [e for e in logs if e["event"] == "sweep_verify.overfit_recovery_chain_unreadable"]
    assert len(warned) == 1
    # The config-unrecoverable residual is the HONEST named grid_mismatch.
    assert "grid_mismatch" in captured.out


# ──────────────── m7: the ladder-scaled Telegram watchdog timeout ────────────────


def _capture_watchdog_timeouts(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    from brutex.engine import sweep_watchdog as watchdog_mod

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


# ──── round 2: the mixed-mode census residue (recount_mode_divergence) ────


def _mixed_mode_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path]:
    """Cycle 1 LEGACY (``--k 1,2``) then cycle 2 EXTINCTION over the SAME
    root: the mode-blind tuple digest resume-skips the two legacy-minted
    tuples (``cached=2``) and mints k=3,4 under apriori mode → a MIXED tree
    (k=1,2 sidecars carry legacy census tallies; k=3,4 extinction ones)."""
    monkeypatch.delenv(SWEEP_APRIORI_ENV, raising=False)
    lake_root, catalog_path = _persist(tmp_path)
    results_root = tmp_path / "sweeps"
    assert _sweep_all(lake_root, catalog_path, results_root, k="1,2") == EXIT_OK
    monkeypatch.setenv(SWEEP_APRIORI_ENV, "1")
    assert _sweep_all(lake_root, catalog_path, results_root) == EXIT_OK
    return lake_root, catalog_path, results_root


def test_mixed_mode_tree_recount_reads_mode_divergence_never_corrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Round-2 R2-1 (the executed BEFORE/AFTER oracle): a matched-mode default
    recount of the MIXED tree previously read verdict-grade
    ``recount_divergence`` (rc 3) on a provably HEALTHY tree; it now reads the
    confined ``recount_mode_divergence`` WARN and exits 0."""
    lake_root, catalog_path, results_root = _mixed_mode_tree(tmp_path, monkeypatch)
    out = capsys.readouterr().out
    assert "cached=2" in out  # the mode-blind digest resume-skipped both
    assert "tuples=2/4" in out  # only k=3,4 genuinely ran in cycle 2

    rc = main(_verify_argv(lake_root, catalog_path, results_root, "--recount"))
    out = capsys.readouterr().out
    assert rc == EXIT_OK  # the BEFORE oracle read rc 3 here
    assert "[CORRUPT]" not in out
    assert "verdict: CLEAN" in out
    assert (
        "recount_mode_divergence: ONLY the exploration-MODE-dependent census "
        "triple (n_enumerated/n_conflict_pruned/n_minhits_dropped) diverge" in out
    )
    assert "[WARN]" in out
    assert "MIXED-mode" in out


def test_apriori_tree_legacy_recount_under_allow_mismatch_keeps_the_attribution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Round-2 R2-1, the cross-mode direction: an apriori-minted tree
    recounted LEGACY (env removed, explicit ``--k 1,2``) under
    ``--allow-context-mismatch`` reads the SAME precise attribution — the
    confined sidecar diff never degrades to the generic inconclusive code and
    never reads corrupt."""
    monkeypatch.setenv(SWEEP_APRIORI_ENV, "1")
    lake_root, catalog_path = _persist(tmp_path)
    results_root = tmp_path / "sweeps"
    assert _sweep_all(lake_root, catalog_path, results_root) == EXIT_OK
    capsys.readouterr()

    monkeypatch.delenv(SWEEP_APRIORI_ENV, raising=False)
    rc = main(
        [
            *_verify_argv(lake_root, catalog_path, results_root, "--recount"),
            "--k",
            "1,2",
            "--allow-context-mismatch",
        ]
    )
    out = capsys.readouterr().out
    assert "exploration_mode: recorded apriori-extinction vs effective legacy" in out
    assert "recount_mode_divergence:" in out  # the precise code, context gate or not
    assert "[CORRUPT]" not in out
    assert " recount_divergence:" not in out  # never the verdict-grade code
    assert "recount_divergence_inconclusive" not in out  # never the generic downgrade
    assert rc == EXIT_OK  # warn-attributed findings are clean


def test_tampered_store_byte_on_the_mixed_tree_stays_rc3_corrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Round-2 R2-1 confinement condition (a): the store-byte equality is the
    proof obligation — a tampered store byte on the SAME mixed tree keeps the
    verify verdict-grade (rc 3), never laundered through the mode family."""
    from brutex.cli import EXIT_VERIFY_FINDINGS

    lake_root, catalog_path, results_root = _mixed_mode_tree(tmp_path, monkeypatch)
    k2_store = next(p for p in _tuple_stores(results_root) if "_k2_" in p.name)
    k2_store.write_bytes(k2_store.read_bytes() + b"x")
    capsys.readouterr()

    rc = main(_verify_argv(lake_root, catalog_path, results_root, "--recount"))
    out = capsys.readouterr().out
    assert rc == EXIT_VERIFY_FINDINGS
    assert "[CORRUPT]" in out


def test_triple_plus_mode_invariant_divergence_stays_rc3_corrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Round-2 R2-1 confinement condition (c): a divergence spilling past the
    census triple into a mode-INVARIANT field (``n_backtested``) stays
    verdict-grade ``recount_divergence`` (rc 3) — the fields()-driven
    must-equal side is the unsafe-drift firewall."""
    import dataclasses

    from brutex.cli import EXIT_VERIFY_FINDINGS
    from brutex.engine.trial_stats import (
        read_tuple_trial_stats,
        trial_stats_sidecar_path,
        write_tuple_trial_stats,
    )

    lake_root, catalog_path, results_root = _mixed_mode_tree(tmp_path, monkeypatch)
    k2_store = next(p for p in _tuple_stores(results_root) if "_k2_" in p.name)
    sidecar = trial_stats_sidecar_path(k2_store)
    stats = read_tuple_trial_stats(sidecar)
    # +1 on BOTH keeps the Tier-1 sidecar_tally_remainder law (== 0) intact,
    # so the ONLY red is the recount's confinement refusal.
    write_tuple_trial_stats(
        sidecar,
        dataclasses.replace(
            stats,
            n_backtested=stats.n_backtested + 1,
            n_enumerated=stats.n_enumerated + 1,
        ),
    )
    capsys.readouterr()

    rc = main(_verify_argv(lake_root, catalog_path, results_root, "--recount"))
    out = capsys.readouterr().out
    assert rc == EXIT_VERIFY_FINDINGS
    assert " recount_divergence:" in out  # the verdict-grade code, colon-anchored
    assert "recount_mode_divergence:" not in out


# ── round 3: the R45 diffcheck samples the EXECUTED population ──


def test_extinct_tuple_diffcheck_completes_in_bounded_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-3 BLOCKER regression (MIGRATION.md #1752): an explicit ``--k 6``
    over a WIDE pool on this 8-bar lake (extinct at level 1 — 8 bars can
    never reach MIN_TRADES=30 hits) with the R45 diffcheck DEFAULT-ON
    completes in seconds. Pre-fix ``run_for_tuple`` materialised the raw
    conflict-pruned combination walk — 126.4 s measured in this container for
    the era's C(60,6)=50,063,860 masks (k=7 ≈ 45 min, k=8 ≈ 5 h + a ~20 GB
    list → OOM) — which the extinction-mode ``effective_max_combos``
    auto-raise unbounds and the per-level breaker cannot see (0 candidates ⇒
    0 breaker work). Post-fix the diffcheck samples the EXECUTED (empty)
    candidate population and skips: seconds, == the diffcheck-OFF baseline.

    RE-DERIVED for the 74-bit head (DEC-SWEEP-APRIORI-DEFAULT-001 #1850 ×
    DEC-SUPERTREND-CONDITIONS-001 #2203): the DEFAULT vocabulary now carries
    bits >= 64, which Apriori-ON refuses per tuple (rc 6 — the interim-law
    e2e twin below pins that shape), so this bounded-time pin restricts the
    run to the FULL sub-64 prefix (bits 0..63) — the Apriori frontier
    genuinely executes, and the pre-fix raw walk would materialise
    C(64,6)=74,974,368 masks, an even STRONGER regression detector than the
    original C(60,6)."""
    monkeypatch.setenv(SWEEP_APRIORI_ENV, "1")
    monkeypatch.delenv("BRUTEX_SWEEP_DIFFCHECK", raising=False)  # default ON
    lake_root, catalog_path = _persist(tmp_path)
    sub64 = ",".join(str(b) for b in range(64))  # the full u64-frontier pool
    t0 = time.monotonic()
    rc = _sweep_all(lake_root, catalog_path, tmp_path / "sweeps", k="6", conditions=sub64)
    wall = time.monotonic() - t0
    assert rc == EXIT_OK
    assert wall < 60.0, (
        f"extinct k=6 tuple took {wall:.1f}s with the diffcheck ON — "
        "the raw C(64,6) walk is back (round-3 BLOCKER regression)"
    )


def test_apriori_on_over_the_default_74bit_pool_refuses_gracefully_rc6(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The #2203 interim law, END-TO-END: Apriori-ON (explicit truthy env +
    explicit ``--k``) over the DEFAULT 74-bit vocabulary refuses PER TUPLE
    via the typed ``SweepAprioriUnsupportedError`` — the CLI exits the loud
    ``EXIT_SWEEP_TUPLE_FAILED`` (rc 6), never a crash, never a corrupt store,
    never a silent legacy downgrade (the operator explicitly asked for
    Apriori; the frozen generator honestly cannot serve a >= 64-bit pool —
    the u128 frontier is the named follow-up). The driver-level mechanism
    twin is ``test_driver_reports_supertrend_bit_tuple_as_tuple_failed``
    (tests/unit/test_engine_sweep_apriori.py); this pins the CLI exit-code
    shape CI observed on the 74-bit head.

    The typed message itself does NOT reach stderr (the CLI prints only the
    generic ``TUPLE_FAILED`` WARNING block naming the supervisor audit path —
    measured), so the typed-error TEETH are pinned where the mechanism records
    them: the ``TUPLE_FAILED`` audit line's ``error`` payload."""
    import json

    from brutex.cli import EXIT_SWEEP_TUPLE_FAILED

    monkeypatch.setenv(SWEEP_APRIORI_ENV, "1")
    lake_root, catalog_path = _persist(tmp_path)
    results_root = tmp_path / "sweeps"
    rc = _sweep_all(lake_root, catalog_path, results_root, k="6", conditions=None)
    assert rc == EXIT_SWEEP_TUPLE_FAILED
    err = capsys.readouterr().err
    # The stderr surface: the loud generic failed-tuple WARNING naming the
    # TUPLE_FAILED audit lane (never the raw traceback, never silence).
    assert "FAILED mid-sweep" in err
    assert "TUPLE_FAILED" in err
    # The typed-error pin, read from the audit lane stderr names: the tuple
    # failed for EXACTLY the #2203 u64-only-frontier reason — not some
    # unrelated defect that happens to share the exit code.
    audit_path = results_root / "audit" / "sweep-all-supervisor.jsonl"
    failed_records = [
        rec
        for line in audit_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
        for rec in [json.loads(line)]
        if rec.get("action") == "TUPLE_FAILED"
    ]
    assert failed_records, "no TUPLE_FAILED audit line recorded"
    assert any(
        "SweepAprioriUnsupportedError" in str(rec["payload"]["error"])
        and "Apriori frontier requires <64-bit masks" in str(rec["payload"]["error"])
        for rec in failed_records
    )
    assert _tuple_stores(results_root) == []  # nothing persisted


# ── DEC-SWEEP-OVERFIT-RECOUNT-RECOVERY-001: the overfit gate-config channel ──
#
# The S2 fold flipped the overfit DSR gate default-ON (fb06ca8a), which folds
# an ``overfit`` fragment into every tuple's `_tuple_digest` store address —
# but ``cmd_verify`` never set ``overfit`` on the verify grid, so a plain
# ``sweep-verify --recount`` over a HEALTHY default-swept tree planned UNGATED
# addresses against GATED files and read a ``grid_mismatch`` FALSE-POSITIVE on
# every such tree (an ALARM-FATIGUE regression — recount unusable on overfit
# trees; NOT a false-clean). The fix records the gate config at MINT
# (SUPERVISOR_START / GRID_SWEEP_START ``gate_config``) and recovers it at
# VERIFY (``recorded_overfit_from_audit`` → ``dataclasses.replace(grid,
# overfit=…)``), plus the (a) honesty floor for MIXED-gate-config trees.


def test_uniform_overfit_default_tree_recount_stays_rc0_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The healthy UNIFORM-gate-config pin: a default (overfit-gated) legacy
    tree recounted with the recovered gate config reads CLEAN rc 0 — never the
    pre-fix ``grid_mismatch`` false-positive, and never a false
    ``recount_mixed_mode_unverifiable`` rc 4 (the (a) floor must not fire on a
    single-family tree)."""
    monkeypatch.delenv(SWEEP_APRIORI_ENV, raising=False)
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
    assert rc == EXIT_OK
    assert "verdict: CLEAN" in out
    assert "grid_mismatch" not in out
    assert "recount_mixed_mode_unverifiable" not in out
    assert "[CORRUPT]" not in out


def test_mixed_gate_config_tree_recount_reads_unverifiable_rc4(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The (a) honesty floor: a tree built under MIXED gate configs — some
    tuples minted with ``--no-overfit-gate`` (ungated digests), others under
    the overfit default (gated digests) over ONE root — recounts to the
    honest ``recount_mixed_mode_unverifiable`` INCONCLUSIVE rc 4: the single
    recovered gate config (the LATEST START record's) cannot verify the other
    digest family, so the verdict is never CLEAN-by-silence (rc 0 with the
    ungated family invisibly skipped) and never verdict-grade corruption
    (rc 3) or the pre-fix ``grid_mismatch``."""
    monkeypatch.delenv(SWEEP_APRIORI_ENV, raising=False)
    lake_root, catalog_path = _persist(tmp_path)
    results_root = tmp_path / "sweeps"
    # Cycle 1: k=1,2 UNGATED (`--no-overfit-gate`) → ungated tuple digests.
    assert _sweep_all(lake_root, catalog_path, results_root, k="1,2", overfit_gate=False) == (
        EXIT_OK
    )
    # Cycle 2: k=3,4 under the overfit DEFAULT → gated digests; its START
    # record is the LATEST, so the recovery adopts the GATED config.
    assert _sweep_all(lake_root, catalog_path, results_root, k="3,4") == EXIT_OK
    capsys.readouterr()

    rc = main(
        [
            *_verify_argv(lake_root, catalog_path, results_root, "--recount"),
            "--k",
            "1,2,3,4",
        ]
    )
    out = capsys.readouterr().out
    # PRESENCE, not an exact count: the floor emits per orphaned ARTIFACT
    # (store + sidecar + sentinel per mixed tuple, as the recount ladder sees
    # them) — the law under pin is the code + the exit grade, not the tally.
    assert "recount_mixed_mode_unverifiable" in out
    assert "MIXED gate configs" in out
    assert "grid_mismatch" not in out  # the pre-fix false-positive class
    assert " recount_divergence:" not in out  # never verdict-grade corruption
    assert "[CORRUPT]" not in out
    assert rc == EXIT_VERIFY_INCONCLUSIVE  # rc 4 — honestly unverifiable, not rc 0/3


def test_ungated_tree_with_no_recorded_gate_config_verifies_rc0_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The no-record backward-compat arm: a ``--no-overfit-gate`` tree's audit
    carries NO ``gate_config`` key anywhere (the EMIT-ON-active law), the
    recovery returns ``None``, and the verify grid stays byte-identical to the
    pre-channel shape — recount CLEAN rc 0.

    HONEST envelope: a genuinely PRE-channel audit from an overfit-GATED
    sweep (gated digests, no record) stays the disclosed ``grid_mismatch``
    residual — the audit chain is hash-sealed, so that shape cannot be
    simulated here without breaking the chain the recount verifies; the
    no-record law is therefore pinned on the ungated arm, whose audit
    genuinely lacks the key."""
    monkeypatch.delenv(SWEEP_APRIORI_ENV, raising=False)
    lake_root, catalog_path = _persist(tmp_path)
    results_root = tmp_path / "sweeps"
    assert _sweep_all(lake_root, catalog_path, results_root, k="1,2", overfit_gate=False) == (
        EXIT_OK
    )
    # The EMIT-ON-active mint law: no overfit gate ⇒ no gate_config recorded.
    for chain in ("sweep-all-supervisor.jsonl", "sweep-all.jsonl"):
        chain_path = results_root / "audit" / chain
        if chain_path.exists():
            assert "gate_config" not in chain_path.read_text(encoding="utf-8")
    capsys.readouterr()

    rc = main(
        [
            *_verify_argv(lake_root, catalog_path, results_root, "--recount"),
            "--k",
            "1,2",
        ]
    )
    out = capsys.readouterr().out
    assert rc == EXIT_OK
    assert "verdict: CLEAN" in out
    assert "grid_mismatch" not in out
    assert "recount_mixed_mode_unverifiable" not in out


def test_malformed_recorded_gate_config_on_a_valid_chain_exits_user_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """MINOR-2 (review fix round): the ``cmd_sweep_verify``
    ``except SweepVerifyError → EXIT_USER_ERROR`` recovery branch, EXECUTED
    end-to-end. A MALFORMED ``gate_config.overfit`` leg — here a NON-FINITE
    ``weight`` member, which the supervisor chain's default ``json.dumps``
    happily persists as a ``NaN`` token (MINOR-1's boundary) — is appended
    through the REAL :class:`SweepAuditWriter` (``resume=True``, so the hash
    chain stays VALID and the tolerant per-chain read cannot skip it); the
    verify must refuse with the loud typed one-liner + rc 1, never a raw
    traceback and never a later ``json.dumps(allow_nan=False)`` crash."""
    from brutex.engine.sweep_audit import SweepAuditWriter

    monkeypatch.delenv(SWEEP_APRIORI_ENV, raising=False)
    lake_root, catalog_path = _persist(tmp_path)
    results_root = tmp_path / "sweeps"
    assert _sweep_all(lake_root, catalog_path, results_root, k="1,2", overfit_gate=False) == (
        EXIT_OK
    )
    sup_path = results_root / "audit" / "sweep-all-supervisor.jsonl"
    assert sup_path.exists()
    with SweepAuditWriter.open(sup_path, resume=True) as sup:
        sup.append(
            action="SUPERVISOR_START",
            subject="sweep-all",
            payload={"gate_config": {"overfit": [0.9, float("nan"), 0.0, 0.0]}},
        )
    capsys.readouterr()

    rc = main(
        [
            *_verify_argv(lake_root, catalog_path, results_root),
            "--k",
            "1,2",
        ]
    )
    captured = capsys.readouterr()
    assert rc == EXIT_USER_ERROR
    assert "sweep-verify error: malformed gate_config.overfit leg" in captured.err
