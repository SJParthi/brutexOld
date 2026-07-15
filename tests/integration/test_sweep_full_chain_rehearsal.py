"""R38 full-chain CI rehearsal (DEC-SWEEP-UNKILLABLE-CORE-001 WP-6, design §7/D-10).

The ENTIRE sweep chain — preflight → sweep → stage-3 refinement → Stage-4
cascade → artifacts — runs against a small REAL lake (seeded with the SAME
production writers ingest uses) by driving the REAL ``cli.cmd_sweep_all`` shim,
"the 2-day run in miniature, minutes, automatic" (R38). Three legs:

1. **Happy leg** (module-scoped, run ONCE): a 190-trading-day winning NIFTY
   slice (the stage-3 lockbox window is 182 trading days, so the train slice is
   non-empty and stage-3 CELLS are genuinely written + stamped). Diffcheck runs
   at the rehearsal budget ``BRUTEX_SWEEP_DIFFCHECK_K=8`` (design CI_K_SAMPLES).
   Every Phase-2 artifact is then asserted: manifest contract, ALL stamps VALID
   (incl. stage-3 cells), verdicts, chain-map, diagnostic bundle, exit-status,
   and ``brutex.audit_verify.auto_verify`` over the flight recorder AND both
   CLI-level chains (D-2 — the matching verifier).
2. **Resume leg**: the SAME selection re-clicked — 0 tuples recomputed (the
   per-tuple store bytes are untouched; the manifest reports the resume skip).
3. **Adversarial leg** (separate results root, same lake): a corrupt
   checkpoint + a corrupt tuple store + an OLD-SCHEMA store are planted, then
   the chain re-runs — every plant is quarantined HONESTLY (forensic copies
   kept, QUARANTINE records on the flight chain) and the regenerated results
   are byte-identical to the clean pass (the sentinel-redo law).

Runtime is measured and printed per leg (``rehearsal.<leg>.seconds`` lines in
the pytest output) so the CI job's budget claim stays evidence-backed.

DETERMINISM (DEC-R38-REHEARSAL-DETERMINISM-001, MIGRATION.md #1773): the
measured wall time is made COMMIT-INVARIANT. The Golden-Rule-#4 run identity
deliberately folds ``code_commit_sha``, and the Stage-3 optimizer seeds derive
from that identity (``stage3_refine.seed_from_mask_run_id``; the cascade seed
is ``int(sweep_id[:16], 16)``) — so before the pin, EVERY commit reshuffled the
Stage-3 refined rows, flipping whether any candidate survived the DSR gate into
the Stage-4 1000-permutation Monte-Carlo filter: a per-commit seed LOTTERY that
made this file's wall time bimodal (~5 s vs ~200 s+ locally; ~33 s vs
310–321 s under coverage on CI) with ZERO code change. The fix: (a)
``_sweep_all`` pins a constant ``code_commit_sha`` (the identity input, not the
seeds, so the WHOLE seeded chain stays production-shaped), and (b) the
``_log_volume_guard`` fixture extends the DEC-SWEEP-LOG-VOLUME-GUARD-001
(#1630) INFO bound to this in-process chain (the CLI dispatch guard never
engages under pytest). This is a test-DETERMINISM/CORRECTNESS fix, NOT a gate
weakening: the runtime assertion previously measured a commit-hash seed lottery
plus DEBUG-render overhead, not chain feasibility (its stated purpose); pinned,
it measures the actual chain on the SAME workload every commit, and a REAL
chain regression on that workload still fails at 300 s (teeth proven by
``test_r38_runtime_gate_teeth_slowed_chain_inflates_measured_seconds``).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import zipfile
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

import pytest
from brutex.audit_verify import auto_verify
from brutex.calendars.nse import get_session
from brutex.data.models import CandleBar
from brutex.engine.sweep_audit import verify_chain
from brutex.observability.flight_recorder import iter_run_records
from brutex.observability.progress_beat import (
    register_progress_beat,
    unregister_progress_beat,
)
from brutex.observability.stage_stamp import StampVerdict, read_stage_stamp, validate_stage_stamp

pytest.importorskip("polars")
pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")
pytest.importorskip("lmdb")
pytest.importorskip("cma")

#: D-6 traceability — test name → requirement/gap tokens (R38 rehearsal file).
TRACEABILITY: Final[dict[str, tuple[str, ...]]] = {
    "test_r38_full_chain_completes_clean": ("R38", "R7"),
    "test_r38_rehearsal_runtime_is_ci_feasible": ("R38",),
    "test_r38_runtime_gate_teeth_slowed_chain_inflates_measured_seconds": ("R38",),
    "test_r38_stage3_cells_genuinely_written": ("R38",),
    "test_r38_cascade_genuinely_ran": ("R38",),
    "test_r28_manifest_contract_shape_and_funnel": ("R28",),
    "test_r28_run_manifest_action_rides_the_cli_chain": ("R28",),
    "test_r41_flight_and_cli_chains_verify": ("R41", "R28"),
    "test_r41_chain_map_links_all_green": ("R41",),
    "test_r42_every_stamp_validates_including_stage3_cells": ("R42",),
    "test_r42_golden_tuple_hash_matches_sentinel_and_store_bytes": ("R42",),
    "test_r43_one_run_id_threads_every_artifact": ("R43",),
    "test_r45_diffcheck_ran_at_the_rehearsal_budget": ("R45",),
    "test_r40_diagnostic_bundle_packages_the_run": ("R40",),
    "test_g15_progress_beats_reach_the_pull_jobs_channel": ("G15", "G3"),
    "test_r38_second_pass_recomputes_zero_tuples": ("R38", "R7"),
    "test_r38_adversarial_plants_quarantined_honestly": ("R38", "G1", "G12"),
    "test_r38_adversarial_recovery_is_byte_identical": ("R38", "R42"),
    "test_r38_adversarial_poisoned_lake_refuses_with_nothing_touched": ("R38", "R42"),
    "test_g14_old_schema_store_degrades_typed_never_raw": ("G14",),
}

_IST = timezone(timedelta(hours=5, minutes=30))
_SYMBOL = "NIFTY"
#: > LOCKBOX_DEFAULT_WINDOW_DAYS (182) so the stage-3 train slice is non-empty.
_N_DAYS = 190
_PER_DAY = 12
#: The design's rehearsal-sized diffcheck budget (CI_K_SAMPLES, D-10/graft C).
_CI_K_SAMPLES = 8
#: The PINNED run-identity commit sha (DEC-R38-REHEARSAL-DETERMINISM-001
#: #1773) — a deliberately synthetic 40-hex constant, chosen by MEASURING the
#: rehearsal under three candidate constants (full matrix in the #1773 ledger
#: entry; same sandbox, INFO log guard active): ``"0"*40`` → 3.41 s and
#: ``"5eedc0de"*5`` → 3.28 s both draw a DSR-KILLED candidate pool (the
#: statistical cascade legs never run — less chain coverage), while THIS
#: constant (``"deadbeef"*5``) → 14.87 s draws ONE candidate through the DSR
#: gate and exercises the ENTIRE statistical cascade — walk-forward, CPCV,
#: PBO, SPA/reality-check, Romano-Wolf, lockbox AND the 1000-permutation
#: Monte-Carlo filter (which kills it: ``stage_survivors.deflated_sharpe ==
#: 1``, ``monte_carlo == 0``) — the most faithful "full chain in miniature"
#: (R38) of the three. Margin to the 300 s budget, both measured on the same
#: 4-vCPU sandbox (Golden Rule #10): ~20× WITHOUT coverage (14.87 s — the
#: figure quoted in #1773's pin matrix) and ~6× UNDER ``--cov=brutex
#: --cov-branch`` (50.53 s — ≈ the WITH-COVERAGE unit job's context, the arm
#: whose pre-fix failures the ledger records). The MC leg is
#: BOUNDED here because the pinned seed's candidate is signal-LIGHT and the
#: ``_log_volume_guard`` keeps its per-trade DEBUG events unrendered — the
#: unlucky pre-pin seeds drew signal-DENSE candidates (~663 entries per
#: permutation) whose MC re-simulation plus 2.78 M DEBUG renders measured
#: 310–321 s under coverage on CI.
_PINNED_CODE_COMMIT_SHA: Final[str] = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"


def _trading_days(start: date, n: int) -> list[date]:
    """The first *n* REAL NSE trading days on/after *start* (calendar-true)."""
    out: list[date] = []
    d = start
    while len(out) < n:
        if get_session(d) is not None:
            out.append(d)
        d += timedelta(days=1)
    return out


def _bars() -> list[CandleBar]:
    """A winning upward-zigzag: BUY masks pass the 5 hard gates via the TP leg."""
    out: list[CandleBar] = []
    for day in _trading_days(date(2024, 1, 1), _N_DAYS):
        price = 100.0
        for m in range(_PER_DAY):
            close = price + 0.6 if m % 3 else price - 0.3
            ts = datetime(day.year, day.month, day.day, 9, 16, tzinfo=_IST) + timedelta(minutes=m)
            out.append(
                CandleBar(
                    timestamp=ts.astimezone(UTC),
                    open=Decimal(str(round(price, 2))),
                    high=Decimal(str(round(max(price, close) + 0.5, 2))),
                    low=Decimal(str(round(min(price, close) - 0.5, 2))),
                    close=Decimal(str(round(close, 2))),
                    volume=1000,
                    open_interest=None,
                )
            )
            price = close
    return out


def _seed_lake(root: Path) -> tuple[Path, Path]:
    """Persist the slice with the REAL production writers (never a mock lake)."""
    from brutex.data.catalog import Catalog
    from brutex.data.parquet_writer import ParquetWriter
    from brutex.data.sink import ParquetBarSink

    lake_root = root / "lake"
    catalog_path = root / "catalog.duckdb"
    writer = ParquetWriter(lake_root=lake_root)
    with Catalog.open(catalog_path) as cat:
        sink = ParquetBarSink(writer=writer, catalog=cat)
        sink.append(
            exchange="NSE",
            segment="INDEX",
            symbol=_SYMBOL,
            interval="1minute",
            bars=_bars(),
        )
    return lake_root, catalog_path


def _sweep_all(lake_root: Path, catalog_path: Path, results_root: Path) -> int:
    """One full-chain ``cmd_sweep_all`` pass — stage-3 + cascade + diffcheck ON.

    ``code_commit_sha`` is PINNED to :data:`_PINNED_CODE_COMMIT_SHA`
    (DEC-R38-REHEARSAL-DETERMINISM-001 #1773) by patching
    ``cli._resolve_code_commit_sha`` — the ONE site feeding the grid's
    ``code_commit_sha`` (``cmd_sweep_all`` exposes no kwarg for it; the
    ``--code-commit-sha`` CLI seam belongs to ``sweep-verify``, and adding a
    kwarg would be an engine-side change outside this fix's ruled test-only
    scope). One frozen sha ⇒ one frozen run identity ⇒ frozen Stage-3/cascade
    seeds ⇒ the wall-time gate measures the SAME work on every commit.
    """
    from brutex import cli

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(cli, "_resolve_code_commit_sha", lambda: _PINNED_CODE_COMMIT_SHA)
        return cli.cmd_sweep_all(
            lake_root=lake_root,
            catalog_path=catalog_path,
            results_root=results_root,
            exchange="NSE",
            segment="INDEX",
            symbols=_SYMBOL,
            intervals="1minute",
            start="2024-01-01",
            end="2024-10-15",
            directions="BUY",
            k="1",
            conditions="0,1,2,3,4",
            sl="80",
            tp="50",
            top=10,
            mode="A",
            max_tuples=None,
            max_combos=None,
            data_snapshot_hash=None,
            options_sim=False,
            stage3_enable=True,
            stage3_top_k=2,
            stage3_fevals=8,
            stage3_restarts=1,
        )


def _result_store_hashes(results_root: Path) -> dict[str, str]:
    """SHA-256 of every per-tuple RESULT artifact (store + trials + sentinel)."""
    out: dict[str, str] = {}
    tuple_dir = results_root / _SYMBOL
    for p in sorted(tuple_dir.rglob("*")):
        if p.is_file() and not p.name.endswith(".stamp.json"):
            out[str(p.relative_to(results_root))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def _run_dir(results_root: Path) -> Path:
    runs = sorted((results_root / "runs").iterdir())
    assert len(runs) == 1, f"expected exactly one runs/<run_id>/ dir, got {runs}"
    return runs[0]


class _Rehearsal:
    """The module-scoped happy+resume rehearsal state (one chain run each)."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.lake_root, self.catalog_path = _seed_lake(root)
        self.results_root = root / "sweeps"
        self.beats = 0

        def _beat() -> None:
            self.beats += 1

        register_progress_beat(_beat)
        try:
            t0 = time.monotonic()
            self.exit_first = _sweep_all(self.lake_root, self.catalog_path, self.results_root)
            self.seconds_first = time.monotonic() - t0
        finally:
            unregister_progress_beat(beat=_beat)
        self.hashes_first = _result_store_hashes(self.results_root)
        self.run_dir = _run_dir(self.results_root)
        self.run_id = self.run_dir.name
        self.manifest_first = json.loads((self.run_dir / "manifest.json").read_text())

        t0 = time.monotonic()
        self.exit_second = _sweep_all(self.lake_root, self.catalog_path, self.results_root)
        self.seconds_second = time.monotonic() - t0
        self.hashes_second = _result_store_hashes(self.results_root)
        self.manifest_second = json.loads((self.run_dir / "manifest.json").read_text())


@pytest.fixture(scope="module", autouse=True)
def _log_volume_guard() -> Iterator[None]:
    """Bound the in-process chain's structlog volume at INFO — the #1630 law.

    DEC-SWEEP-LOG-VOLUME-GUARD-001 (#1630) bounds a REAL sweep process at INFO
    via the ``main()`` dispatch guard (``cli._configure_sweep_logging``) — but
    this rehearsal drives ``cmd_sweep_all`` IN-PROCESS under pytest, where that
    guard never engages and structlog's default wrapper filters at NOTSET, so
    the engine's per-trade DEBUG events (``trade_sim.entry``/``trade_sim.exit``)
    rendered unfiltered INSIDE the measured wall time
    (DEC-R38-REHEARSAL-DETERMINISM-001 #1773: 2.78 M such renders cost ~256 s
    on an MC-surviving seed). This fixture extends the #1630 law to the
    rehearsal by installing the CLI guard's EXACT sink-level threshold —
    ``structlog.make_filtering_bound_logger(INFO)`` as the wrapper class — and
    NOTHING else: the full ``configure_logging`` seam is deliberately NOT
    called in-process, because it closes + replaces the root logger's handlers
    (pytest's own logging-plugin capture handlers included) — a suite-wide
    hazard the real CLI process never has. Snapshot/restore plus first-use
    cache clears (both directions — an EARLIER in-process configure may have
    frozen module loggers, and ours must not freeze later ``capture_logs``)
    keep the rest of the suite's logging state as found. Per the #1630 pin,
    the audit / flight-recorder hash chains are direct file appends, NOT
    structlog events — byte-untouched at any level.
    """
    import structlog
    from brutex.observability.logging import _clear_structlog_first_use_cache

    saved_config = structlog.get_config()
    saved_is_configured = structlog.is_configured()
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.INFO))
    _clear_structlog_first_use_cache()
    try:
        yield
    finally:
        if saved_is_configured:  # a prior test left structlog configured — restore it
            structlog.configure(**saved_config)
        else:
            structlog.reset_defaults()
        _clear_structlog_first_use_cache()


@pytest.fixture(scope="module")
def rehearsal(tmp_path_factory: pytest.TempPathFactory) -> _Rehearsal:
    """Run the full chain (first pass + resume pass) exactly ONCE per module."""
    env_backup = os.environ.get("BRUTEX_SWEEP_DIFFCHECK_K")
    os.environ["BRUTEX_SWEEP_DIFFCHECK_K"] = str(_CI_K_SAMPLES)
    try:
        state = _Rehearsal(tmp_path_factory.mktemp("rehearsal"))
    finally:
        if env_backup is None:
            del os.environ["BRUTEX_SWEEP_DIFFCHECK_K"]
        else:  # pragma: no cover — env pre-set only outside the suite
            os.environ["BRUTEX_SWEEP_DIFFCHECK_K"] = env_backup
    print(f"\nrehearsal.first_pass.seconds={state.seconds_first:.2f}")
    print(f"rehearsal.resume_pass.seconds={state.seconds_second:.2f}")
    return state


# ────── happy leg ──────


def test_r38_full_chain_completes_clean(rehearsal: _Rehearsal) -> None:
    assert rehearsal.exit_first == 0
    status = json.loads((rehearsal.run_dir / "exit-status.json").read_text())
    assert status["state"] == "complete"
    assert status["suspect"] is False
    assert status["exit_code"] == 0
    assert status["error_type"] is None


def test_r38_rehearsal_runtime_is_ci_feasible(rehearsal: _Rehearsal) -> None:
    """The chain-in-miniature stays MINUTES not hours (measured, not assumed).

    DETERMINISM + HONESTY (DEC-R38-REHEARSAL-DETERMINISM-001 #1773): before
    the ``_sweep_all`` sha pin + the ``_log_volume_guard`` INFO bound, this
    assertion measured a commit-hash SEED LOTTERY plus DEBUG-render overhead,
    NOT chain feasibility (its stated purpose) — the Stage-3 seeds derive from
    the run identity, which folds ``code_commit_sha``, so per commit the DSR
    gate either killed the candidate pool (~5 s locally / ~33 s CI) or let one
    candidate into the 1000-permutation Monte-Carlo (~200 s+ locally /
    310–321 s under coverage — measured FAILURES of this very assert with zero
    code change, and a zero-diff empty commit removed them). Pinned, the
    assertion measures the ACTUAL chain on the SAME workload every commit
    (pinned-path measurements in the #1773 ledger entry); a REAL chain
    regression on that workload still fails here at 300 s — teeth proven by
    ``test_r38_runtime_gate_teeth_slowed_chain_inflates_measured_seconds``.
    The 300.0 budget itself is UNCHANGED (never weakened).
    """
    assert rehearsal.seconds_first < 300.0, (
        f"first rehearsal pass took {rehearsal.seconds_first:.1f}s — the CI budget claim broke"
    )


def test_r38_stage3_cells_genuinely_written(rehearsal: _Rehearsal) -> None:
    """The lockbox-window-sized slice makes stage-3 refine REAL cells (not a no-op)."""
    raw = (rehearsal.results_root / "audit" / "sweep-all.jsonl").read_text()
    lines = [json.loads(line) for line in raw.splitlines() if line]
    complete = [e for e in lines if e["action"] == "STAGE_3_PASS_COMPLETE"]
    assert len(complete) >= 1
    assert complete[0]["payload"]["masks_refined"] == 2
    assert complete[0]["payload"]["cells_written"] > 0
    assert complete[0]["payload"]["skipped_empty_train"] == 0


def test_r38_cascade_genuinely_ran(rehearsal: _Rehearsal) -> None:
    """The cascade report exists AND carries the pinned seed's FULL-cascade shape.

    CASCADE-SHAPE DRIFT PIN (DEC-R38-REHEARSAL-DETERMINISM-001 #1773): the
    "full chain in miniature" claim rests on the PINNED seed drawing ONE
    candidate through the DSR gate so every statistical cascade leg —
    walk-forward, CPCV, PBO, SPA/reality-check, Romano-Wolf, lockbox and the
    1000-permutation Monte-Carlo filter (which kills it) — genuinely EXECUTES.
    That coverage is a property of the seed choice, not of the code, so a
    future ``indicator_version`` / wheel / engine change that re-keys the
    pinned seed could silently revert the rehearsal to a DSR-KILLED fast path
    (legs never run) while the runtime gate stays green. This assertion makes
    that rot fail LOUD and forces a conscious re-pin (re-measure candidate
    constants per the #1773 matrix; never a silent coverage drop).
    """
    report = json.loads((rehearsal.results_root / "cascade" / "cascade-report.json").read_text())
    assert report["sweep_id"] == rehearsal.run_id
    # ONE (instrument, tf) group in the rehearsal grid — its per-group report
    # carries the stage_survivors funnel (cascade_wire.wire_report_to_dict).
    assert len(report["groups"]) == 1
    assert report["groups"][0]["report"]["stage_survivors"] == {
        "hard_gates": 5,
        "deflated_sharpe": 1,
        "cpcv": 1,
        "reality_check_spa": 1,
        "pbo": 1,
        "romano_wolf": 1,
        "walk_forward": 1,
        "lockbox": 1,
        "monte_carlo": 0,
    }, "the pinned seed's full-cascade shape drifted — re-pin consciously (#1773)"
    manifest = json.loads((rehearsal.results_root / "cascade" / "manifest.json").read_text())
    assert manifest["sweep_id"] == rehearsal.run_id


def test_r28_manifest_contract_shape_and_funnel(rehearsal: _Rehearsal) -> None:
    doc = rehearsal.manifest_first
    contract_keys = {
        "run_id",
        "selection",
        "estimate",
        "actual",
        "per_stage_timings",
        "per_door_kills",
        "dedup_skips",
        "tripwires",
        "suspect",
    }
    assert contract_keys <= set(doc)
    assert doc["run_id"] == rehearsal.run_id
    assert doc["suspect"] is False
    assert doc["actual"]["tuples_done"] == 1
    assert doc["actual"]["tuples_failed"] == 0
    assert set(doc["per_stage_timings"]) == {"preflight", "sweep", "stage3", "cascade", "artifacts"}
    doors = {row["door"]: row["killed"] for row in doc["per_door_kills"]}
    # Sourceable doors populated (D-8); the cascade's dsr door genuinely judged.
    assert doors["min_hits"] == 0
    assert doors["dsr"] > 0
    assert doc["dedup_skips"] == 0
    assert doc["selection"]["symbols"] == [_SYMBOL]
    assert doc["selection"]["conditions_hex"] == "1f"


def test_r28_run_manifest_action_rides_the_cli_chain(rehearsal: _Rehearsal) -> None:
    from brutex.orchestrator.audit import iter_entries

    entries = list(iter_entries(rehearsal.results_root / "audit" / "sweep-all.jsonl"))
    manifests = [e for e in entries if e.action == "RUN_MANIFEST"]
    # One per pass (first + resume) — the D-3 additive chain-safe action.
    assert len(manifests) == 2
    assert manifests[0].subject == rehearsal.run_id
    assert set(manifests[0].payload) == {"manifest", "manifest_content_hash"}


def test_r41_flight_and_cli_chains_verify(rehearsal: _Rehearsal) -> None:
    """D-2: the SAME auto verifier covers flight + CLI chains; BLAKE3 via its own."""
    flight = rehearsal.run_dir / "flight-recorder.jsonl"
    assert auto_verify(flight).entry_count > 0
    cli_chain = rehearsal.results_root / "audit" / "sweep-all.jsonl"
    assert auto_verify(cli_chain).entry_count > 0
    assert verify_chain(rehearsal.results_root / "audit" / "sweep-all-supervisor.jsonl") > 0


def test_r41_chain_map_links_all_green(rehearsal: _Rehearsal) -> None:
    chain = json.loads((rehearsal.run_dir / "chain-map.json").read_text())
    assert chain["run_id"] == rehearsal.run_id
    assert chain["suspect"] is False
    states = {link: doc["state"] for link, doc in chain["links"].items()}
    assert states == {
        "preflight": "green",
        "sweep": "green",
        "stage3": "green",
        "cascade": "green",
        "artifacts": "green",
    }


def test_r42_every_stamp_validates_including_stage3_cells(rehearsal: _Rehearsal) -> None:
    """Every ``.stamp.json`` in the tree recomputes to OK — none torn, none stale."""
    stamps = sorted(rehearsal.results_root.rglob("*.stamp.json"))
    by_stage: dict[str, int] = {}
    for sidecar in stamps:
        artifact = sidecar.with_name(sidecar.name[: -len(".stamp.json")])
        stamp = read_stage_stamp(artifact)
        verdict, validated = validate_stage_stamp(artifact, expected_stage=stamp.stage)
        assert verdict is StampVerdict.OK, f"{artifact}: {verdict}"
        assert validated is not None
        assert validated.run_id == rehearsal.run_id
        by_stage[stamp.stage] = by_stage.get(stamp.stage, 0) + 1
    # The COMPLETE stamped set (design §3): stage-3 cells + cascade pair +
    # stage2 aggregate + manifest + verdicts.
    assert by_stage["stage3_cell"] > 0
    assert by_stage["cascade_manifest"] == 1
    assert by_stage["cascade_report"] == 1
    assert by_stage["stage2"] == 1
    assert by_stage["manifest"] == 1
    assert by_stage["verdicts"] == 1


def test_r42_golden_tuple_hash_matches_sentinel_and_store_bytes(rehearsal: _Rehearsal) -> None:
    """The sentinel's recorded store hash IS the store's real content hash."""
    sentinels = sorted((rehearsal.results_root / _SYMBOL).rglob("*.complete.json"))
    assert len(sentinels) == 1
    doc = json.loads(sentinels[0].read_text())
    store = sentinels[0].with_name(sentinels[0].name[: -len(".complete.json")])
    assert doc["store_content_hash"] == hashlib.sha256(store.read_bytes()).hexdigest()


def test_r43_one_run_id_threads_every_artifact(rehearsal: _Rehearsal) -> None:
    rid = rehearsal.run_id
    assert json.loads((rehearsal.run_dir / "exit-status.json").read_text())["run_id"] == rid
    assert json.loads((rehearsal.run_dir / "manifest.json").read_text())["run_id"] == rid
    assert json.loads((rehearsal.run_dir / "chain-map.json").read_text())["run_id"] == rid
    assert json.loads((rehearsal.run_dir / "verdicts.json").read_text())["run_id"] == rid
    records = list(iter_run_records(rehearsal.results_root, rid))
    assert len(records) > 0
    assert all(r["run_id"] == rid for r in records)
    progress = json.loads((rehearsal.results_root / "progress" / "sweep-progress.json").read_text())
    assert progress["run_id"] == rid


def test_r45_diffcheck_ran_at_the_rehearsal_budget(rehearsal: _Rehearsal) -> None:
    """R45 genuinely engaged: 5 enumerable masks < K=8 budget ⇒ all 5 checked."""
    records = list(iter_run_records(rehearsal.results_root, rehearsal.run_id))
    ok = [r for r in records if r["kind"] == "DIFFCHECK_OK"]
    assert len(ok) == 1
    payload = ok[0]["payload"]
    assert isinstance(payload, dict)
    assert payload["checked"] == 5  # min(CI_K_SAMPLES=8, C(5,1)=5 masks)
    assert payload["mismatches"] == 0
    assert not [r for r in records if r["kind"] in ("DIFFCHECK_MISMATCH", "SUSPECT")]


def test_r40_diagnostic_bundle_packages_the_run(rehearsal: _Rehearsal) -> None:
    bundle = rehearsal.run_dir / "diagnostic-bundle.zip"
    with zipfile.ZipFile(bundle) as zf:
        names = set(zf.namelist())
    packaged = {n.rsplit("/", 1)[-1] for n in names}
    for required in ("manifest.json", "exit-status.json", "flight-recorder.jsonl"):
        assert required in packaged, f"{required} missing from {sorted(names)}"


def test_g15_progress_beats_reach_the_pull_jobs_channel(rehearsal: _Rehearsal) -> None:
    """The composed on_progress emits on the EXACT channel pull_jobs consumes."""
    assert rehearsal.beats > 0


def test_r38_runtime_gate_teeth_slowed_chain_inflates_measured_seconds(
    rehearsal: _Rehearsal, tmp_path: Path
) -> None:
    """The 300 s gate keeps its TEETH under the sha pin (noise out, teeth intact).

    DEC-R38-REHEARSAL-DETERMINISM-001 (#1773) teeth proof: pinning
    ``code_commit_sha`` removed the per-commit SEED lottery from the measured
    quantity — this test proves it removed ONLY the noise, never the gate's
    bite. The SAME pinned chain re-runs against a FRESH results root (the
    rehearsal's own tree is untouched — its ``attempt`` counter and store
    bytes stay exactly what the resume-leg tests assert) with a controlled
    per-mask delay injected into ``TradeSimWalker.run`` — the per-mask
    trade-sim step every sweep/cascade/results-depth evaluation walks
    (class-method patch, so every import style of ``simulate_trades`` is
    covered). HONEST framing of what each half proves (Golden Rule #10): the
    PERMANENT load-bearing teeth here is ``calls > 0`` — SEAM LIVENESS, so a
    refactor that moves per-mask trade-sim off ``TradeSimWalker.run`` (e.g.
    onto a Rust kernel) fails LOUD instead of leaving this proof vacuous —
    plus the time-RESPONSE check that the measured wall time contains the
    injected delay ≥ 1:1 (near-mechanical for serial in-process sleeps inside
    the timed window; it is a structural proxy, not a magnitude proof). The
    actual BUDGET-MAGNITUDE bite — that a budget-scale chain regression blows
    the unchanged ``< 300.0`` assert — is proven by the ONE-TIME executed
    wedge recorded in the #1773 ledger entry (one wedged-chain-step 301 s
    sleep → ``test_r38_rehearsal_runtime_is_ci_feasible`` FAILED at 315.8 s
    on the pinned sha); this companion alone does not prove it — it keeps that
    recorded proof's premises (seam fires; wall time responds) permanently
    pinned at CI speed.
    """
    from brutex.engine.trade_sim import TradeSimWalker

    delay_s = 0.01  # ~1,044 walker calls on the pinned workload ⇒ ~10 s injected
    calls = {"n": 0}
    real_run = TradeSimWalker.run

    def slowed_run(self: TradeSimWalker, *args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        time.sleep(delay_s)
        return real_run(self, *args, **kwargs)

    results_root = tmp_path / "sweeps-slowed"
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("BRUTEX_SWEEP_DIFFCHECK_K", str(_CI_K_SAMPLES))
        mp.setattr(TradeSimWalker, "run", slowed_run)
        t0 = time.monotonic()
        assert _sweep_all(rehearsal.lake_root, rehearsal.catalog_path, results_root) == 0
        slowed_seconds = time.monotonic() - t0

    total_injected = delay_s * calls["n"]
    assert calls["n"] > 0, "the per-mask trade-sim seam never fired — no teeth proven"
    # The timed window contains the chain's per-mask work >= 1:1 — a genuinely
    # slowed chain step inflates EXACTLY the number the 300 s assert gates.
    assert slowed_seconds >= total_injected
    print(
        f"\nrehearsal.teeth.slowed_seconds={slowed_seconds:.2f} "
        f"injected={total_injected:.2f} calls={calls['n']}"
    )


# ────── resume leg ──────


def test_r38_second_pass_recomputes_zero_tuples(rehearsal: _Rehearsal) -> None:
    assert rehearsal.exit_second == 0
    # 0 recomputed: every per-tuple result byte is UNTOUCHED by the re-click.
    assert rehearsal.hashes_second == rehearsal.hashes_first
    assert rehearsal.manifest_second["actual"]["tuples_done"] == 0
    assert rehearsal.manifest_second["actual"]["tuples_skipped_resume"] == 1
    status = json.loads((rehearsal.run_dir / "exit-status.json").read_text())
    assert status["attempt"] == 2
    assert status["state"] == "complete"


# ────── adversarial leg ──────


class _Adversarial:
    """Corrupt checkpoint + corrupt tuple store planted, then recover."""

    def __init__(self, root: Path) -> None:
        import shutil

        self.lake_root, self.catalog_path = _seed_lake(root)
        self.results_root = root / "sweeps"
        assert _sweep_all(self.lake_root, self.catalog_path, self.results_root) == 0
        self.clean_hashes = _result_store_hashes(self.results_root)

        # Plant 1 (G12): garbage checkpoint — historically a crash-loop.
        checkpoint = self.results_root / "checkpoint" / "sweep-all.json"
        checkpoint.write_bytes(b"\x00garbage{{{not-json")
        # Plant 2 (G1): the STAGE-2 tuple store (the one with a completion
        # sentinel — stage-3 cell stores carry stamps, not sentinels)
        # overwritten with garbage bytes — historically a whole-grid abort.
        sentinels = sorted((self.results_root / _SYMBOL).rglob("*.complete.json"))
        assert len(sentinels) == 1
        self.store = sentinels[0].with_name(sentinels[0].name[: -len(".complete.json")])
        self.store.write_bytes(b"\x00not-a-parquet-store")
        # The sentinel + the LMDB tuple-cache tier both vouch for the tuple —
        # remove them so the redo genuinely re-enters the store-open path (the
        # G1 quarantine seam). HONEST NOTE (filed for the coordinator): with
        # the cache tier intact the tuple is cache-skipped WITHOUT a sentinel
        # cross-check and the poisoned store first surfaces at the cascade's
        # spec loader — a discovered follow-up, not silently papered over here.
        self.store.with_name(self.store.name + ".complete.json").unlink()
        shutil.rmtree(self.results_root / "dedup-cache")

        t0 = time.monotonic()
        self.exit_recovery = _sweep_all(self.lake_root, self.catalog_path, self.results_root)
        self.seconds = time.monotonic() - t0
        self.recovered_hashes = _result_store_hashes(self.results_root)
        self.run_id = _run_dir(self.results_root).name
        self.records = list(iter_run_records(self.results_root, self.run_id))


@pytest.fixture(scope="module")
def adversarial(tmp_path_factory: pytest.TempPathFactory) -> _Adversarial:
    env_backup = os.environ.get("BRUTEX_SWEEP_DIFFCHECK_K")
    os.environ["BRUTEX_SWEEP_DIFFCHECK_K"] = str(_CI_K_SAMPLES)
    try:
        state = _Adversarial(tmp_path_factory.mktemp("adversarial"))
    finally:
        if env_backup is None:
            del os.environ["BRUTEX_SWEEP_DIFFCHECK_K"]
        else:  # pragma: no cover — env pre-set only outside the suite
            os.environ["BRUTEX_SWEEP_DIFFCHECK_K"] = env_backup
    print(f"\nrehearsal.adversarial.seconds={state.seconds:.2f}")
    return state


def test_r38_adversarial_plants_quarantined_honestly(adversarial: _Adversarial) -> None:
    assert adversarial.exit_recovery == 0
    quarantine = adversarial.results_root / ".quarantine"
    checkpoints = sorted((quarantine / "checkpoints").glob("*"))
    assert len(checkpoints) == 1  # G12 forensic copy — kept, never deleted
    assert checkpoints[0].read_bytes().startswith(b"\x00garbage")
    runtime = sorted((quarantine / "runtime").iterdir())
    assert len(runtime) == 1  # G1 — the garbage store trio moved for forensics
    kinds = [r["kind"] for r in adversarial.records]
    assert "QUARANTINE" in kinds
    status = json.loads(
        (adversarial.results_root / "runs" / adversarial.run_id / "exit-status.json").read_text()
    )
    assert status["state"] == "complete"
    assert status["suspect"] is False


def test_r38_adversarial_recovery_is_byte_identical(adversarial: _Adversarial) -> None:
    """R42 one-line law: the redo regenerates the EXACT clean-pass bytes."""
    assert adversarial.recovered_hashes == adversarial.clean_hashes


def test_r38_adversarial_poisoned_lake_refuses_with_nothing_touched(
    tmp_path: Path,
) -> None:
    """A garbage lake partition fails the chain LOUDLY at preload — with ZERO
    results-root artifacts created (R42 row 1: preflight fail = nothing touched)."""
    import polars as pl

    lake_root, catalog_path = _seed_lake(tmp_path)
    month = next(lake_root.rglob("*.parquet"))
    month.write_bytes(b"\x00poisoned-not-parquet")
    results_root = tmp_path / "sweeps"
    with pytest.raises(pl.exceptions.ComputeError):
        _sweep_all(lake_root, catalog_path, results_root)
    assert not results_root.exists()


def test_g14_old_schema_store_degrades_typed_never_raw(tmp_path: Path) -> None:
    """The committed OLD-SCHEMA results Parquet degrades TYPED, never raw.

    ``ResultsStore.load`` now types a missing required column itself as a
    :class:`ResultsStoreSchemaError` (a :class:`ResultsStoreError`) naming the
    store path (DEC-SWEEP-RESULTS-DEPTH-001 ruling 5, the main fold), preserving
    the raw ``KeyError`` as ``__cause__`` — so the /results skip set (which
    catches the ``ResultsStoreError`` family) degrades honestly instead of a raw
    ``KeyError`` escaping into a 500. ``guarded_load`` passes that already-typed
    store error through UNWRAPPED (never double-wrapping into an
    ``ArtifactLoadError``), so the end-to-end degrade stays a single typed
    ``ResultsStoreError``."""
    import shutil

    from brutex.engine.results_store import (
        ResultsStore,
        ResultsStoreError,
        ResultsStoreSchemaError,
    )
    from brutex.engine.sweep_artifact_guard import guarded_load

    fixture = Path("tests/fixtures/results_store_old_schema.parquet")
    assert fixture.exists(), "the committed old-schema fixture is required (WP-4 note #1)"
    legacy = tmp_path / "legacy.parquet"
    shutil.copy(fixture, legacy)
    with pytest.raises(ResultsStoreSchemaError) as excinfo:
        guarded_load(ResultsStore.load, legacy)
    assert isinstance(excinfo.value, ResultsStoreError)  # the resilient-reader skip family
    assert isinstance(excinfo.value.__cause__, KeyError)  # typed wrap of the raw escape
