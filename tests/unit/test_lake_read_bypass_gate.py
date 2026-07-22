"""CI bypass gate: no NEW direct lake Parquet read outside the dedup chokepoint.

DEC-LAKE-READ-UNIQUE-001 wires every lake reader through the ONE read-side
dedup gate (:mod:`brutex.data.lake_read`). This grep-based gate (the house
banned-pattern-scan style, ``scripts/ci/scan-banned-patterns.sh``) makes
bypassing it STRUCTURALLY impossible for every parquet-read spelling the
repo's stack exposes: polars ``read_parquet(`` / ``scan_parquet(``, DuckDB-SQL
``read_parquet(`` / the legacy alias ``parquet_scan(``, pyarrow
``read_table(`` / ``ParquetFile(``, and the fd-guarded seam spellings
``read_parquet_regular(`` / ``read_parquet_glob_regular(`` (the
DEC-LAKE-READ-INODE-GUARD-001 helpers in :mod:`brutex.data.guarded_read`,
which perform the raw polars read on behalf of their callers — the caller
file must therefore still carry its own allowlist entry, preserving the
per-file accountability this gate exists for). A call site appearing in a
file OUTSIDE the explicit allowlist fails CI, so the author must either
route through :mod:`brutex.data.lake_read` or consciously extend the
allowlist (with a dedup wiring + a reason) in the same PR — never silently.

The allowlist is FILE-level with a mandatory reason per entry; a stale entry
(file no longer matches) ALSO fails, so the list can never rot.

Honest envelope (Golden Rule #10): the gate is TEXTUAL — a read spelled
through an exotic indirection (a bare-path DuckDB ``FROM 'x.parquet'`` string
literal, a pandas ``pd.read_parquet`` (pandas is banned brutex/-wide by
scan-banned-patterns.sh check #7), or an aliased/getattr-built call) cannot be caught by
grep; those spellings are ALSO banned by this decision and are left to
review. The function names above cover every reader the repo actually
uses (verified by the sweep that built this allowlist + the
DEC-LAKE-READ-INODE-GUARD-001 re-sweep that added the guarded spellings).
The SCHEMA-ONLY probes — ``read_parquet_schema(`` and its guarded twin
``read_parquet_schema_regular(`` — are deliberately OUTSIDE the gate
(the pre-existing envelope, now stated): they read Parquet METADATA only
and can never serve a bar row, so dedup identity does not apply.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

#: The banned raw-read pattern — every polars / DuckDB-SQL / pyarrow parquet
#: read spelling the repo's stack exposes (see the module-docstring envelope).
_RAW_READ_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(?:read_parquet_glob_regular|read_parquet_regular|read_parquet"
    r"|scan_parquet_regular|scan_parquet|parquet_scan|read_table|ParquetFile)\s*\("
)

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]

#: Every file allowed to contain a raw parquet-read call, with the reason it
#: is allowed (either "wired": it routes the read through the lake_read dedup
#: gate in the same function, or a documented non-bar-lake / write-side use).
ALLOWED_RAW_READ_FILES: Final[dict[str, str]] = {
    "brutex/data/lake_read.py": "the chokepoint itself (docstrings + SQL fragments)",
    "brutex/data/guarded_read.py": (
        "the COMMON fd-validated read SEAM (DEC-LAKE-READ-INODE-GUARD-001) — "
        "read_parquet_regular/read_parquet_glob_regular perform the ONE raw "
        "pl.read_parquet on behalf of their callers (polars is handed the "
        "validated OPEN handle); the seam serves whatever its caller's dedup "
        "wiring dictates, and every CALLER file still needs its own entry "
        "here because the guarded spellings are themselves in _RAW_READ_RE"
    ),
    "brutex/data/parquet_writer.py": (
        "write-side merge read; keep-first collapse + the pq.ParquetFile footer "
        "row-count probe (metadata-only, no bar rows served) — DEC-LAKE-WRITE-UNIQUE-001"
    ),
    "brutex/data/option_parquet_writer.py": "write-side merge read; (side,ts) dedup (#1067)",
    "brutex/data/catalog.py": "wired: get_bars routes through dedup_bars_frame",
    "brutex/data/lake_day_glob.py": "wired: both day readers route through dedup_bars_frame",
    "brutex/data/spot_join.py": "wired: spot_close_by_ts_window dedups before the map build",
    "brutex/data/greeks_enrich.py": "wired: read_option_rows dedups the merged frame",
    "brutex/data/greeks_backfill.py": (
        "stamp-staleness probe — a bounded greeks_provenance_id COLUMN read per "
        "month file (no OHLCV bar rows served); the RAW read DETECTS a stale "
        "stamp on ANY physical row, but every TARGETING judgment runs over the "
        "driver's row view (_driver_row_view: own-UTC-month bound + the COMMON "
        "dedup_bars_frame keep-first — read_option_rows' exact per-file "
        "discipline), because a stamp only a dropped dup/foreign row carries is "
        "driver-unactionable and would loop false-green forever; such staleness "
        "folds into stale_unfixable + one loud WARN naming lake_heal "
        "(DEC-GREEKS-BULK-REENRICH-001 fix round 7)"
    ),
    "brutex/data/spot_daily_resample.py": (
        "1m->1d derive read — its own frame-level dedup self-heal (DEC-PULL-FORENSIC-WAVE3-001)"
    ),
    # W-6a (DEC-RUST-LAKE-WAVED-001): timeframe_materialize.py,
    # catalog_reconcile.py and lake_invariants.py DROPPED from this
    # allowlist — their Python raw-read cores are DELETED; the raw reads
    # now live in the brutex_lake crate (the same raw-see-everything laws,
    # crate-side), so the Python modules no longer contain the pattern.
    "brutex/data/lake_heal.py": (
        "the Wave-D W-2 option-stats replay re-read (DEC-RUST-LAKE-WAVED-001, "
        "deviation D-12): after the Rust heal REWRITES an options file "
        "keep-first, the shell re-reads THAT freshly-deduped file once to "
        "drive the verbatim _record_option_stats — a stats-manifest hook "
        "over just-written bytes (no dups by construction; never a "
        "bar-serving read), so the dedup gate has nothing to collapse"
    ),
    "brutex/adapters/dhan/rolling_option_writer.py": (
        "write-side merge read under the partition locks; timestamp keep-first "
        "dedup before the atomic rewrite — the dhan feed-lake twin of "
        "option_parquet_writer (DEC-DHAN-ROLLING-OPTIONS-001)"
    ),
    "brutex/adapters/dhan/rolling_option_greeks.py": (
        "dhan rolling-option Greeks overlay — reads the keep-first writer's OWN "
        "partitions + the locked write-side overlay merge read "
        "(DEC-DHAN-ROLLING-OPTIONS-001)"
    ),
    "brutex/adapters/dhan/daily_consistency.py": (
        "observe-only daily-vs-minute close probe over the dhan feed lake "
        "(timestamp+close columns; never serves bar rows) — "
        "DEC-DHAN-NATIVE-DAILY-001"
    ),
    "brutex/dashboard/db_view.py": (
        "wired: pagers use DEDUP_PARQUET_SOURCE_SQL + QUALIFY; totals use distinct_count_sql"
    ),
    "brutex/dashboard/app.py": "comment reference only (no executable raw read)",
    "brutex/dashboard/movers_snapshot.py": "wired: _read_daily_partition dedups",
    "brutex/dashboard/movers_futures.py": (
        "wired: resolve_prev_oi dedups (the _prev_day_oi value seam rides it — "
        "DEC-MOVERS-PREVCLOSE-CONTIGUITY-001 kept the OI scan physically here)"
    ),
    "brutex/dashboard/movers_warm.py": (
        "candidate-discovery aggregate over symbols (ranking warm), not a bar-serving read"
    ),
    "brutex/options/lake_source.py": "wired: _contract_bars dedups before the bar map build",
    "brutex/futures/lake_source.py": (
        "wired: _contract_bars dedups (keep-FIRST) before the bar map build — "
        "the futures twin of options/lake_source (DEC-FUTURES-EXEC-TRANSLATION-001)"
    ),
    # DEC-SUSPICION-SWEEP-FIXES-001 (G1): the doctor's freshness `_read` moved into
    # the COMMON tf_autoderive.read_lake_bars_frame (shared by the doctor probe AND
    # the sweep-preflight freshness gate) — the raw read feeds dedup_bars_frame
    # IMMEDIATELY, exactly as the superseded doctor.py entry did.
    "brutex/data/tf_autoderive.py": "wired: read_lake_bars_frame dedups",
    "brutex/engine/results_store.py": "results-tree Parquet (run_id-keyed), not the bar lake",
    "brutex/engine/trade_store.py": (
        "results-depth engine sidecars (trades/aggs/equity, run_id-keyed, "
        "byte-locked writer/reader pair under sweep_runs/) — not lake bars; "
        "dedup identity does not apply (DEC-SWEEP-RESULTS-DEPTH-001)"
    ),
    "brutex/engine/results_depth_pass.py": (
        "reads back the SAME stamp-validated engine sidecars trade_store "
        "writes (aggs join + twin entry-ts sets) — engine artifacts, not "
        "lake bars (DEC-SWEEP-RESULTS-DEPTH-001)"
    ),
    "brutex/engine/resilience_streaks.py": (
        "reads its OWN stamp-validated <stem>.resilience.parquet sidecar (+ the "
        "run_id-keyed trades sidecar for the adoption backfill) — run_id-keyed "
        "engine artifacts under sweep_runs/, not lake bars; dedup identity does "
        "not apply (DEC-SWEEP-RESULTS-DEPTH-001, losing-streak resilience)"
    ),
    "brutex/dashboard/oneclick_artifacts.py": (
        "display-zone read of the results-depth EQUITY sidecar "
        "(<stem>.equity.parquet under sweep_runs/, run_id-keyed — the "
        "trade_store writer/reader pair's stored series) for "
        "/api/v1/oneclick/equity — engine artifacts, not lake bars; dedup "
        "identity does not apply (DEC-ONECLICK-WINNER-DETAIL-TRUTH-001)"
    ),
    "brutex/dashboard/composite_join.py": (
        "display-zone read of the results-depth AGGS sidecars under "
        "sweep_runs/ ((run_id, composite_score) columns only — the "
        "trade_store writer/reader pair's artifacts) for the /results "
        "?preset=composite join — engine artifacts, not lake bars; the "
        "lake_read dedup chokepoint governs lake bar reads only "
        "(DEC-RESULTS-CASCADE-VERDICT-JOIN-001)"
    ),
    "brutex/meta/annotate.py": (
        "annotation-pass read of the results-depth AGGS sidecar "
        "(<stem>.aggs.parquet under sweep_runs/, run_id-keyed engine "
        "artifacts — the trade_store writer/reader pair's outputs) for the "
        "informational vix_band_winners_p10_p90 field — not a "
        "bar/options-lake read, so the lake_read dedup identity does not "
        "apply (DEC-META-LABEL-ANNOTATE-001)"
    ),
    "brutex/cli.py": (
        "wired: _LakeOptionBarSource._row_at keep-first + WARN via log_read_duplicates"
    ),
    "brutex/acceptance/independent.py": (
        "the ONE AST-FENCED independent movers-truth oracle "
        "(DEC-OPERATOR-ACCEPTANCE-VERIFY-001, the DEC-SURFACE-TRIAL-001 "
        "precedent): every compared /movers cell is re-derived from the RAW "
        "month parquet with glob=False + hand-rolled math (keep-first dedup "
        "re-stated as a LAW, hand IST/bucket-anchor/%CHNG). Routing it through "
        "lake_read would import a production seam and DEFEAT the independence "
        "guarantee the fence (test_acceptance_independent_fence.py — no "
        "brutex.* callable import) exists to enforce"
    ),
    "brutex/acceptance/sampling.py": (
        "wired: month_bounded_read feeds the raw glob=False read (metachar-named "
        "dir literal, #1594-R15) into dedup_bars_frame IMMEDIATELY, exactly as "
        "the tf_autoderive.read_lake_bars_frame all-months arm does"
    ),
    "brutex/acceptance/selftest.py": (
        "poison-twin builder — surgical read-modify-write on the SYNTHETIC "
        "self-test twin lake (written with the real production writers) to "
        "inject duplicate / foreign-month / x10 / pre-open / value poisons the "
        "checks must DETECT; a dedup-gated read would strip the very duplicate "
        "row it plants (the teeth-before-trust smoke), never a real-lake read "
        "(DEC-OPERATOR-ACCEPTANCE-VERIFY-001)"
    ),
}


def _matching_files() -> set[str]:
    """Return every brutex/ Python file containing the raw-read pattern."""
    hits: set[str] = set()
    for path in sorted((_REPO_ROOT / "brutex").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if _RAW_READ_RE.search(text):
            hits.add(path.relative_to(_REPO_ROOT).as_posix())
    return hits


class TestLakeReadBypassGate:
    def test_no_new_raw_parquet_read_outside_allowlist(self) -> None:
        """A NEW raw parquet-read call site outside the allowlist fails CI."""
        offenders = _matching_files() - set(ALLOWED_RAW_READ_FILES)
        assert not offenders, (
            f"NEW raw parquet read(s) found outside the lake_read dedup gate: "
            f"{sorted(offenders)}. Route the read through brutex.data.lake_read "
            f"(dedup_bars / dedup_bars_frame / DEDUP_PARQUET_SOURCE_SQL + "
            f"dedup_qualify_sql / distinct_count_sql) or — if the read is "
            f"genuinely not a bar/options-lake read — add the file to "
            f"ALLOWED_RAW_READ_FILES with the reason (DEC-LAKE-READ-UNIQUE-001)."
        )

    def test_allowlist_entries_are_not_stale(self) -> None:
        """Every allowlisted file still contains the pattern — the list cannot rot."""
        stale = set(ALLOWED_RAW_READ_FILES) - _matching_files()
        assert not stale, (
            f"stale ALLOWED_RAW_READ_FILES entries (file no longer contains a raw "
            f"parquet read — remove them): {sorted(stale)}"
        )

    def test_pattern_catches_every_stack_spelling(self) -> None:
        # polars
        assert _RAW_READ_RE.search("pl.read_parquet(path)")
        assert _RAW_READ_RE.search("pl.scan_parquet(glob)")
        # DuckDB SQL — modern name + the legacy alias
        assert _RAW_READ_RE.search('conn.execute("SELECT 1 FROM read_parquet(?)")')
        assert _RAW_READ_RE.search('conn.execute("SELECT 1 FROM parquet_scan(?)")')
        # pyarrow — the reviewer-proved bypass spellings (hostile-review F1)
        assert _RAW_READ_RE.search("pq.read_table(bars_path)")
        assert _RAW_READ_RE.search("pq.ParquetFile(dest)")
        # the fd-guarded seam spellings (DEC-LAKE-READ-INODE-GUARD-001 /
        # DEC-LAKE-READ-INODE-GUARD-002) — a caller routing its raw read
        # through guarded_read stays in-gate
        assert _RAW_READ_RE.search("read_parquet_regular(path, columns=cols)")
        assert _RAW_READ_RE.search('read_parquet_glob_regular(base + "/**/*.parquet")')
        assert _RAW_READ_RE.search('scan_parquet_regular(files, missing_columns="insert")')
        # the SCHEMA-ONLY probes stay OUTSIDE the gate (metadata, no bar rows
        # served — the pre-existing pl.read_parquet_schema envelope)
        assert not _RAW_READ_RE.search("pl.read_parquet_schema(path)")
        assert not _RAW_READ_RE.search("read_parquet_schema_regular(path)")
        # word boundaries — no false positives on lookalikes
        assert not _RAW_READ_RE.search("a_read_parquet(x)")
        assert not _RAW_READ_RE.search("my_read_table(x)")
        assert not _RAW_READ_RE.search("read parquet")
