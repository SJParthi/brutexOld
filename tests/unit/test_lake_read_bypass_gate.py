"""CI bypass gate: no NEW direct lake Parquet read outside the dedup chokepoint.

DEC-LAKE-READ-UNIQUE-001 wires every lake reader through the ONE read-side
dedup gate (:mod:`brutex.data.lake_read`). This grep-based gate (the house
banned-pattern-scan style, ``scripts/ci/scan-banned-patterns.sh``) makes
bypassing it STRUCTURALLY impossible for every parquet-read spelling the
repo's stack exposes: polars ``read_parquet(`` / ``scan_parquet(``, DuckDB-SQL
``read_parquet(`` / the legacy alias ``parquet_scan(``, and pyarrow
``read_table(`` / ``ParquetFile(``. A call site appearing in a file OUTSIDE
the explicit allowlist fails CI, so the author must either route through
:mod:`brutex.data.lake_read` or consciously extend the allowlist (with a dedup
wiring + a reason) in the same PR — never silently.

The allowlist is FILE-level with a mandatory reason per entry; a stale entry
(file no longer matches) ALSO fails, so the list can never rot.

Honest envelope (Golden Rule #10): the gate is TEXTUAL — a read spelled
through an exotic indirection (a bare-path DuckDB ``FROM 'x.parquet'`` string
literal, a pandas ``pd.read_parquet`` (pandas is banned from hot paths by
CLAUDE.md §4 anyway), or an aliased/getattr-built call) cannot be caught by
grep; those spellings are ALSO banned by this decision and are left to
review. The five function names above cover every reader the repo actually
uses (verified by the sweep that built this allowlist).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

#: The banned raw-read pattern — every polars / DuckDB-SQL / pyarrow parquet
#: read spelling the repo's stack exposes (see the module-docstring envelope).
_RAW_READ_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(?:read_parquet|scan_parquet|parquet_scan|read_table|ParquetFile)\s*\("
)

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]

#: Every file allowed to contain a raw parquet-read call, with the reason it
#: is allowed (either "wired": it routes the read through the lake_read dedup
#: gate in the same function, or a documented non-bar-lake / write-side use).
ALLOWED_RAW_READ_FILES: Final[dict[str, str]] = {
    "brutex/data/lake_read.py": "the chokepoint itself (docstrings + SQL fragments)",
    "brutex/data/parquet_writer.py": (
        "write-side merge read; keep-first collapse + the pq.ParquetFile footer "
        "row-count probe (metadata-only, no bar rows served) — DEC-LAKE-WRITE-UNIQUE-001"
    ),
    "brutex/data/option_parquet_writer.py": "write-side merge read; (side,ts) dedup (#1067)",
    "brutex/data/catalog.py": "wired: get_bars routes through dedup_bars_frame",
    "brutex/data/lake_day_glob.py": "wired: both day readers route through dedup_bars_frame",
    "brutex/data/spot_join.py": "wired: spot_close_by_ts_window dedups before the map build",
    "brutex/data/greeks_enrich.py": "wired: read_option_rows dedups the merged frame",
    "brutex/data/spot_daily_resample.py": (
        "1m->1d derive read — its own frame-level dedup self-heal (DEC-PULL-FORENSIC-WAVE3-001)"
    ),
    "brutex/data/timeframe_materialize.py": (
        "derive read over the SAME writer-deduped 1m base; derived writes re-render whole months"
    ),
    "brutex/data/catalog_reconcile.py": (
        "physical row-count audit — MUST see raw (possibly duplicated) rows to reconcile"
    ),
    "brutex/data/lake_invariants.py": (
        "invariant scanner (_read_key_columns) — MUST see raw rows to DETECT "
        "intra-file duplicates; a dedup-gated read would hide the very defect "
        "it exists to find (DEC-LAKE-HEAL-001)"
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
    # DEC-SUSPICION-SWEEP-FIXES-001 (G1): the doctor's freshness `_read` moved into
    # the COMMON tf_autoderive.read_lake_bars_frame (shared by the doctor probe AND
    # the sweep-preflight freshness gate) — the raw read feeds dedup_bars_frame
    # IMMEDIATELY, exactly as the superseded doctor.py entry did.
    "brutex/data/tf_autoderive.py": "wired: read_lake_bars_frame dedups",
    "brutex/engine/results_store.py": "results-tree Parquet (run_id-keyed), not the bar lake",
    "brutex/data/window_ledger_store.py": (
        "pq.read_table of the WINDOW-LEDGER sidecar (fetch-window rows), not the "
        "bar/options lake — dedup identity does not apply"
    ),
    "brutex/cli.py": (
        "wired: _LakeOptionBarSource._row_at keep-first + WARN via log_read_duplicates"
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
        # word boundaries — no false positives on lookalikes
        assert not _RAW_READ_RE.search("a_read_parquet(x)")
        assert not _RAW_READ_RE.search("my_read_table(x)")
        assert not _RAW_READ_RE.search("read parquet")
