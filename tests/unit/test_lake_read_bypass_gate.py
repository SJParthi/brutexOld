"""CI bypass gate: no NEW direct lake Parquet read outside the dedup chokepoint.

DEC-LAKE-READ-UNIQUE-001 wires every lake reader through the ONE read-side
dedup gate (:mod:`brutex.data.lake_read`). This grep-based gate (the house
banned-pattern-scan style, ``scripts/ci/scan-banned-patterns.sh``) makes
bypassing it STRUCTURALLY impossible: a new ``pl.read_parquet(`` /
``pl.scan_parquet(`` / DuckDB-SQL ``read_parquet(`` call site on the bar /
options lake appearing in a file OUTSIDE the explicit allowlist fails CI, so
the author must either route through :mod:`brutex.data.lake_read` or
consciously extend the allowlist (with a dedup wiring + a reason) in the same
PR — never silently.

The allowlist is FILE-level with a mandatory reason per entry; a stale entry
(file no longer matches) ALSO fails, so the list can never rot.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

#: The banned raw-read pattern — any polars or DuckDB-SQL parquet scan call.
_RAW_READ_RE: Final[re.Pattern[str]] = re.compile(r"\b(?:read_parquet|scan_parquet)\s*\(")

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]

#: Every file allowed to contain a raw parquet-read call, with the reason it
#: is allowed (either "wired": it routes the read through the lake_read dedup
#: gate in the same function, or a documented non-bar-lake / write-side use).
ALLOWED_RAW_READ_FILES: Final[dict[str, str]] = {
    "brutex/data/lake_read.py": "the chokepoint itself (docstrings + SQL fragments)",
    "brutex/data/parquet_writer.py": "write-side merge read; keep-first collapse (#1067)",
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
    "brutex/dashboard/db_view.py": (
        "wired: pagers use DEDUP_PARQUET_SOURCE_SQL + QUALIFY; totals use distinct_count_sql"
    ),
    "brutex/dashboard/app.py": "comment reference only (no executable raw read)",
    "brutex/dashboard/movers_snapshot.py": "wired: _read_daily_partition dedups",
    "brutex/dashboard/movers_futures.py": "wired: _prev_day_oi dedups",
    "brutex/dashboard/movers_warm.py": (
        "candidate-discovery aggregate over symbols (ranking warm), not a bar-serving read"
    ),
    "brutex/options/lake_source.py": "wired: _contract_bars dedups before the bar map build",
    "brutex/doctor.py": "wired: check_derived_freshness _read dedups",
    "brutex/engine/results_store.py": "results-tree Parquet (run_id-keyed), not the bar lake",
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

    def test_pattern_catches_both_polars_and_sql_forms(self) -> None:
        assert _RAW_READ_RE.search("pl.read_parquet(path)")
        assert _RAW_READ_RE.search("pl.scan_parquet(glob)")
        assert _RAW_READ_RE.search('conn.execute("SELECT 1 FROM read_parquet(?)")')
        assert not _RAW_READ_RE.search("a_read_parquet(x)")  # word boundary
        assert not _RAW_READ_RE.search("read parquet")
