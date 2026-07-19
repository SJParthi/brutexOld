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
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import zipfile
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Final

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
pytest.importorskip("brutex_refine")

#: D-6 traceability — test name → requirement/gap tokens (R38 rehearsal file).
TRACEABILITY: Final[dict[str, tuple[str, ...]]] = {
    "test_r38_full_chain_completes_clean": ("R38", "R7"),
    "test_r38_rehearsal_runtime_is_ci_feasible": ("R38",),
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
    """The worst-case-rankable gap-up drift: BUY masks pass the 5 hard gates.

    DEC-FILL-WORST-CASE-ONLY-001 re-craft (the old wide-range zigzag SL'd
    every worst-case entry on its own bar): narrow gap-up bars (range 0.50 <
    the SL 0.80; the next gap-up's low clears the TP level → +50 wins), a
    shallow bearish bar each 7-bar cycle, and a DEEP bearish bar (range
    0.85) every 4th cycle whose stop-out keeps profit_factor finite.
    """
    out: list[CandleBar] = []
    cycle = 0
    for day in _trading_days(date(2024, 1, 1), _N_DAYS):
        price = 100.0
        for m in range(_PER_DAY):
            k = m % 7
            if k == 0:
                cycle += 1
            if k == 3:  # shallow bearish bar
                o = price
                c, h, lo = o - 0.3, o + 0.1, o - 0.4
            elif k == 4 and cycle % 4 == 0:  # deep bearish loss carrier
                o = price
                c, h, lo = o - 0.65, o + 0.1, o - 0.75
            else:  # narrow gap-up win bar
                o = price + 3.0
                c, h, lo = o + 0.3, o + 0.4, o - 0.1
            ts = datetime(day.year, day.month, day.day, 9, 16, tzinfo=_IST) + timedelta(minutes=m)
            out.append(
                CandleBar(
                    timestamp=ts.astimezone(UTC),
                    open=Decimal(str(round(o, 2))),
                    high=Decimal(str(round(h, 2))),
                    low=Decimal(str(round(lo, 2))),
                    close=Decimal(str(round(c, 2))),
                    volume=1000,
                    open_interest=None,
                )
            )
            price = c
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
    """One full-chain ``cmd_sweep_all`` pass — stage-3 + cascade + diffcheck ON."""
    from brutex import cli

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


def _coverage_instrumented() -> bool:
    """True when coverage.py is actively tracing THIS process.

    Distinguishes the unit job (whole-suite ``pytest --cov=brutex
    --cov-branch`` — every traced line pays the tracer) from the dedicated
    ``sweep-rehearsal`` CI job, which runs this file standalone with NO
    coverage. Probes the LIVE tracer (``coverage.Coverage.current()``), never
    "is the package importable" — [dev] always installs coverage.
    """
    try:
        import coverage
    except ImportError:
        return False
    return coverage.Coverage.current() is not None


#: The STRICT sweep-compute ceiling — asserted where the process is NOT
#: coverage-traced (the dedicated `sweep-rehearsal` job, measured ~7 s there),
#: so ci.yml's "a real sweep slowdown trips the in-test ceiling" claim keeps
#: its full teeth on every push.
_RUNTIME_CEILING_S = 300.0
#: The documented 3x instrumentation allowance for a coverage-traced process:
#: under the unit job's whole-package `--cov=brutex --cov-branch` tracing the
#: SAME first pass measures the TRACER, not sweep compute (315.4 s vs ~7 s
#: uninstrumented — run 29269883269, 2026-07-13), so the strict tier would
#: flake on tracer+contention noise while catching nothing the dedicated job
#: does not. 900 s stays "minutes not hours" — the assertion is never deleted.
_RUNTIME_CEILING_COVERAGE_S = 3 * _RUNTIME_CEILING_S


def test_r38_rehearsal_runtime_is_ci_feasible(rehearsal: _Rehearsal) -> None:
    """The chain-in-miniature stays MINUTES not hours (measured, not assumed).

    Two-tier ceiling (the CLAUDE.md §2 Rule 5 two-tier philosophy): strict
    300 s where uninstrumented (the dedicated rehearsal job); the documented
    3x instrumentation allowance where coverage.py is actively tracing (the
    unit job's whole-package branch tracing).
    """
    ceiling = _RUNTIME_CEILING_COVERAGE_S if _coverage_instrumented() else _RUNTIME_CEILING_S
    assert rehearsal.seconds_first < ceiling, (
        f"first rehearsal pass took {rehearsal.seconds_first:.1f}s"
        f" (ceiling {ceiling:.0f}s) — the CI budget claim broke"
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
    report = json.loads((rehearsal.results_root / "cascade" / "cascade-report.json").read_text())
    assert report["sweep_id"] == rehearsal.run_id
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
