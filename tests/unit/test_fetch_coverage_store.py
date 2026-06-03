"""Tests for `brutex.data.fetch_coverage_store` — the durable skip-ledger sidecar.

100% line+branch: the path helper, the missing-file empty load, the
save→load round-trip, the crash-safe atomic write (no stray ``*.tmp``), every
degrade-to-empty failure mode (corrupt JSON, wrong-shaped JSON, bad-arity row,
bad ISO date) with its WARNING, and the dry-run-gated ``begin``/``end`` pull
helpers (load-for-live / honour-explicit / skip-for-dry-run).
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from brutex.data.fetch_coverage import FetchCoverage, WindowKey
from brutex.data.fetch_coverage_store import (
    COVERAGE_FILENAME,
    begin_pull_coverage,
    coverage_path,
    end_pull_coverage,
    load_fetch_coverage,
    save_fetch_coverage,
)
from structlog.testing import capture_logs


def _key(security_id: str = "13", interval: str = "1minute") -> WindowKey:
    return WindowKey(
        market="dhan",
        security_id=security_id,
        interval=interval,
        start=date(2021, 1, 1),
        end=date(2021, 4, 1),
    )


def _seeded() -> FetchCoverage:
    cov = FetchCoverage()
    cov.mark_complete(_key("13", "1minute"))
    cov.mark_complete(_key("25", "1day"))
    return cov


# ── path ────────────────────────────────────────────────────────────────


def test_coverage_path_is_sidecar_under_lake(tmp_path: Path) -> None:
    assert coverage_path(tmp_path) == tmp_path / COVERAGE_FILENAME


# ── load / save ───────────────────────────────────────────────────────────


def test_load_missing_file_is_empty(tmp_path: Path) -> None:
    cov = load_fetch_coverage(tmp_path)
    assert cov.completed_count == 0


def test_save_then_load_round_trips(tmp_path: Path) -> None:
    save_fetch_coverage(_seeded(), tmp_path)
    restored = load_fetch_coverage(tmp_path)
    assert restored.completed_count == 2
    assert restored.is_complete(_key("13", "1minute")) is True
    assert restored.is_complete(_key("25", "1day")) is True


def test_save_creates_lake_root_and_is_deterministic(tmp_path: Path) -> None:
    lake = tmp_path / "missing" / "lake"  # parent does not exist yet
    save_fetch_coverage(_seeded(), lake)
    path = coverage_path(lake)
    assert path.is_file()
    # The on-disk JSON is exactly the sorted to_records() output (reproducible).
    assert json.loads(path.read_text(encoding="utf-8")) == [list(r) for r in _seeded().to_records()]


def test_save_is_atomic_no_tmp_left_behind(tmp_path: Path) -> None:
    save_fetch_coverage(_seeded(), tmp_path)
    # Only the final sidecar — the *.tmp staging file was os.replace'd away.
    assert (tmp_path / COVERAGE_FILENAME).is_file()
    assert not (tmp_path / f"{COVERAGE_FILENAME}.tmp").exists()


def test_save_empty_then_load_empty(tmp_path: Path) -> None:
    save_fetch_coverage(FetchCoverage(), tmp_path)
    assert load_fetch_coverage(tmp_path).completed_count == 0


# ── degrade-to-empty failure modes (never crash a pull) ───────────────────


def test_load_corrupt_json_degrades_to_empty_with_warning(tmp_path: Path) -> None:
    coverage_path(tmp_path).write_text("{not valid json", encoding="utf-8")
    with capture_logs() as logs:
        cov = load_fetch_coverage(tmp_path)
    assert cov.completed_count == 0
    assert any(e["event"] == "fetch_coverage.load_failed" for e in logs)


def test_load_non_record_json_degrades_to_empty(tmp_path: Path) -> None:
    # A JSON object (not a list of rows) → the row-unpack raises TypeError.
    coverage_path(tmp_path).write_text('{"a": 1}', encoding="utf-8")
    with capture_logs() as logs:
        cov = load_fetch_coverage(tmp_path)
    assert cov.completed_count == 0
    assert any(e["event"] == "fetch_coverage.load_failed" for e in logs)


def test_load_bad_arity_row_degrades_to_empty(tmp_path: Path) -> None:
    # A row with the wrong number of fields → unpack ValueError.
    coverage_path(tmp_path).write_text(json.dumps([["dhan", "13", "1minute"]]), encoding="utf-8")
    cov = load_fetch_coverage(tmp_path)
    assert cov.completed_count == 0


def test_load_bad_iso_date_degrades_to_empty(tmp_path: Path) -> None:
    # Well-shaped row but an unparseable date → from_records raises ValueError.
    bad = [["dhan", "13", "1minute", "not-a-date", "2021-04-01"]]
    coverage_path(tmp_path).write_text(json.dumps(bad), encoding="utf-8")
    cov = load_fetch_coverage(tmp_path)
    assert cov.completed_count == 0


# ── begin_pull_coverage ───────────────────────────────────────────────────


def test_begin_dry_run_returns_caller_coverage_unchanged(tmp_path: Path) -> None:
    # Dry-run never loads from disk; threads the caller's value (here None) as-is.
    assert begin_pull_coverage(tmp_path, dry_run=True, coverage=None) is None
    seeded = _seeded()
    assert begin_pull_coverage(tmp_path, dry_run=True, coverage=seeded) is seeded


def test_begin_live_honours_explicit_coverage(tmp_path: Path) -> None:
    seeded = _seeded()
    assert begin_pull_coverage(tmp_path, dry_run=False, coverage=seeded) is seeded


def test_begin_live_loads_persisted_when_none(tmp_path: Path) -> None:
    save_fetch_coverage(_seeded(), tmp_path)
    loaded = begin_pull_coverage(tmp_path, dry_run=False, coverage=None)
    assert loaded is not None
    assert loaded.completed_count == 2


# ── end_pull_coverage ─────────────────────────────────────────────────────


def test_end_live_persists_coverage(tmp_path: Path) -> None:
    end_pull_coverage(_seeded(), tmp_path, dry_run=False)
    assert load_fetch_coverage(tmp_path).completed_count == 2


def test_end_dry_run_does_not_persist(tmp_path: Path) -> None:
    end_pull_coverage(_seeded(), tmp_path, dry_run=True)
    assert not coverage_path(tmp_path).exists()


def test_end_live_none_coverage_is_noop(tmp_path: Path) -> None:
    end_pull_coverage(None, tmp_path, dry_run=False)
    assert not coverage_path(tmp_path).exists()
