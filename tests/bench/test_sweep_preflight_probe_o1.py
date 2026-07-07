"""O(1)/flatness bench pins for the sweep-preflight probes + coverage scan.

DEC-SWEEP-PREFLIGHT-ESTIMATOR-001 (BUILD-SPEC §10.2(b) / R29). Three pins:

(a) the WRITE-FREE lock probes (:func:`~brutex.data.sweep_exclusion.
    pull_in_progress` / :func:`~brutex.data.sweep_exclusion.sweep_in_progress`)
    are FLAT vs lake size — each is one path open + one non-blocking flock,
    O(1) in lake depth by construction; the gate exists so an accidental
    scan/glob regression fails CI mechanically.
(b) :func:`~brutex.data.session_coverage.scan_selection_coverage` is FLAT vs
    UNRELATED lake depth (+200 junk symbol trees ⇒ same wall). HONEST claim
    (Golden Rule #10): the scan's cost is **O(requested symbols ×
    window-months) file probes + timestamp-column reads — NOT O(1)**; the
    pinned property is depth-INDEPENDENCE (never a tree walk — the
    2.35M-file trap), not constant total time.
(c) :func:`~brutex.data.session_coverage.scan_ledger_quarantine` performs
    exactly ONE read-only ``load_window_ledger`` parse per launch, however
    many (symbol, interval) pairs the selection carries (spy pin).

Ceilings route through :func:`tests.bench._o1_tiers.ratio_ceiling` — never a
bare constant (DEC-O1-STRICT-TIER-001). Measured 2026-07-07 on the shared
sandbox: probe ratio ~1.0×, scan ratio ~1.0× (both far inside the robust
5.0× shared-CI ceiling).
"""

from __future__ import annotations

import time
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import brutex.data.session_coverage as session_coverage_module
import polars as pl
import pytest
from brutex.data.session_coverage import scan_ledger_quarantine, scan_selection_coverage
from brutex.data.sweep_exclusion import (
    pull_in_progress,
    pull_lock_path,
    sweep_in_progress,
    sweep_lock_path,
)

from tests.bench._o1_tiers import ratio_ceiling

if TYPE_CHECKING:
    from brutex.data.window_ledger import WindowLedger

#: Junk-file counts two orders apart — a probe that scanned the lake tree
#: would blow the ratio far past the ceiling.
_JUNK_SMALL = 20
_JUNK_BIG = 2_000
#: Unrelated junk symbol trees added to the DEEP lake for the scan pin.
_JUNK_SYMBOLS = 200
_PROBE_ROUNDS = 300
_SCAN_ROUNDS = 5


def _seed_junk_files(lake: Path, count: int) -> None:
    """Populate *count* junk parquet-named files across many symbol dirs."""
    for i in range(count):
        d = lake / "bars" / "NSE" / "CASH" / f"NSE-JUNK{i % 100:03d}" / "1minute" / "2025"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{(i % 12) + 1:02d}.parquet").write_bytes(b"j")


def _touch_locks(lake: Path) -> None:
    for path in (pull_lock_path(lake), sweep_lock_path(lake)):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()


def _probe_mean_ns(lake: Path) -> float:
    """Mean nanoseconds for ONE pull-probe + ONE sweep-probe round."""
    start = time.perf_counter_ns()
    for _ in range(_PROBE_ROUNDS):
        pull_in_progress(lake)
        sweep_in_progress(lake)
    return (time.perf_counter_ns() - start) / _PROBE_ROUNDS


def test_lock_probes_flat_vs_lake_size(tmp_path: Path) -> None:
    """(a) each probe is one path open + one flock — flat however deep the lake."""
    small = tmp_path / "small"
    big = tmp_path / "big"
    _seed_junk_files(small, _JUNK_SMALL)
    _seed_junk_files(big, _JUNK_BIG)
    _touch_locks(small)
    _touch_locks(big)
    # Warm, then measure (mirrors the sibling O(1) benches).
    _probe_mean_ns(small)
    _probe_mean_ns(big)
    small_ns = _probe_mean_ns(small)
    big_ns = _probe_mean_ns(big)
    ratio = big_ns / small_ns if small_ns > 0 else 1.0
    ceiling = ratio_ceiling(5.0)
    assert ratio <= ceiling, (
        f"lock-probe ratio {ratio:.2f} (big={big_ns:.0f}ns small={small_ns:.0f}ns) "
        f"exceeds the O(1) ceiling {ceiling:.2f} — a probe is scanning the lake"
    )


def _seed_requested_symbol(lake: Path) -> None:
    """One REAL requested symbol: two 1-minute months of a few bars each."""
    base = lake / "bars" / "NSE" / "INDEX" / "NSE-NIFTY" / "1minute"
    for month, day in ((3, 3), (4, 1)):
        d = base / "2025"
        d.mkdir(parents=True, exist_ok=True)
        frame = pl.DataFrame(
            {"timestamp": [datetime(2025, month, day, 4, m, tzinfo=UTC) for m in range(30)]},
            schema={"timestamp": pl.Datetime("us", "UTC")},
        )
        frame.write_parquet(d / f"{month:02d}.parquet")


def _scan_min_seconds(lake: Path) -> float:
    best = float("inf")
    for _ in range(_SCAN_ROUNDS):
        start = time.perf_counter()
        scan_selection_coverage(
            lake_root=lake,
            exchange="NSE",
            segment="INDEX",
            symbols=["NSE-NIFTY"],
            intervals=["1minute", "5minute"],
            start=date(2025, 3, 1),
            end=date(2025, 4, 30),
        )
        best = min(best, time.perf_counter() - start)
    return best


def test_scan_selection_coverage_flat_vs_unrelated_lake_depth(tmp_path: Path) -> None:
    """(b) +200 junk symbol trees change NOTHING — the scan never walks the tree.

    HONEST envelope: the scan is O(requested symbols × window-months), NOT
    O(1) — this pin holds only its independence from UNRELATED lake depth.
    """
    shallow = tmp_path / "shallow"
    deep = tmp_path / "deep"
    _seed_requested_symbol(shallow)
    _seed_requested_symbol(deep)
    for i in range(_JUNK_SYMBOLS):
        d = deep / "bars" / "NSE" / "CASH" / f"NSE-JUNK{i:03d}" / "1minute" / "2025"
        d.mkdir(parents=True)
        (d / "03.parquet").write_bytes(b"junk, never opened by the bounded scan")
    # Warm both, then measure best-of-N.
    _scan_min_seconds(shallow)
    _scan_min_seconds(deep)
    shallow_s = _scan_min_seconds(shallow)
    deep_s = _scan_min_seconds(deep)
    ratio = deep_s / shallow_s if shallow_s > 0 else 1.0
    ceiling = ratio_ceiling(5.0)
    assert ratio <= ceiling, (
        f"coverage-scan depth ratio {ratio:.2f} (deep={deep_s * 1e3:.2f}ms "
        f"shallow={shallow_s * 1e3:.2f}ms) exceeds {ceiling:.2f} — the scan is "
        "walking unrelated lake depth"
    )


def test_ledger_scan_is_one_parse_per_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(c) ONE ``load_window_ledger`` parse per launch, whatever the pair count."""
    calls = {"n": 0}
    real_load = session_coverage_module.load_window_ledger

    def counting_load(lake_root: Path, **kwargs: Any) -> WindowLedger:
        calls["n"] += 1
        return real_load(lake_root, **kwargs)

    monkeypatch.setattr(session_coverage_module, "load_window_ledger", counting_load)
    scan_ledger_quarantine(
        tmp_path,
        exchange="NSE",
        symbols=[f"SYM{i}" for i in range(50)],
        intervals=["1minute", "1day", "5minute", "15minute"],
        start=date(2025, 1, 1),
        end=date(2025, 12, 31),
    )
    assert calls["n"] == 1
