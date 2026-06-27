"""Tests for `brutex.dashboard.lake_revision` (DEC-CROSS-PROCESS-CACHE-FRESH-001).

The cheap cross-process lake fingerprints that close the in-process-generation
blind spot: a CLI ``brutex fetch`` writing into ``BRUTEX_LAKE_ROOT`` alongside the
dashboard must move these tokens so the read caches recompute instead of serving
stale counts forever.

100% line + branch via a real tmp lake (no DuckDB, no FastAPI, no network):

* :func:`lake_revision_token` — empty/absent roots key at ``"0:0"``; a new
  partition leaf directory moves the COUNT; an atomic append into an existing
  partition (``os.replace`` bumping the containing directory's mtime) moves the MAX;
  multiple roots are summed; repeat calls on an unchanged lake are stable.
* :func:`partition_parquet_mtime_token` — an absent/empty base yields ``0``; the
  newest Parquet ``st_mtime_ns`` is returned; an in-place rewrite (``os.replace``)
  moves it; only ``*.parquet`` files count.
"""

from __future__ import annotations

import os
from pathlib import Path

from brutex.dashboard.lake_revision import (
    lake_revision_token,
    partition_parquet_mtime_token,
)


def _touch(path: Path, data: bytes = b"x") -> None:
    """Create *path* (and parents) with *data*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _atomic_rewrite(path: Path, data: bytes) -> None:
    """Rewrite *path* in place via a tmp + ``os.replace`` (mirrors the lake writer)."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


# ── lake_revision_token ───────────────────────────────────────────────────────────


def test_revision_token_absent_roots_key_at_zero(tmp_path: Path) -> None:
    # No roots exist yet → the stable empty token (well-defined before the first pull).
    assert lake_revision_token([tmp_path / "bars", tmp_path / "options"]) == "0:0"


def test_revision_token_counts_directories(tmp_path: Path) -> None:
    bars = tmp_path / "bars"
    _touch(bars / "NSE" / "INDEX" / "NIFTY" / "1minute" / "2020" / "01.parquet")
    tok = lake_revision_token([bars])
    # The token is "<dir-count>:<max-mtime>"; the count is the directories UNDER bars
    # plus bars itself (bars, NSE, INDEX, NIFTY, 1minute, 2020 = 6).
    count, _, mtime = tok.partition(":")
    assert int(count) == 6
    assert int(mtime) > 0


def test_revision_token_new_partition_moves_count(tmp_path: Path) -> None:
    bars = tmp_path / "bars"
    _touch(bars / "NSE" / "INDEX" / "NIFTY" / "1minute" / "2020" / "01.parquet")
    before = lake_revision_token([bars])
    # A cross-process write creates a NEW symbol partition → the dir count moves.
    _touch(bars / "NSE" / "INDEX" / "BANKNIFTY" / "1minute" / "2020" / "01.parquet")
    after = lake_revision_token([bars])
    assert after != before


def test_revision_token_append_into_existing_partition_moves_mtime(tmp_path: Path) -> None:
    bars = tmp_path / "bars"
    month = bars / "NSE" / "INDEX" / "NIFTY" / "1minute" / "2020" / "01.parquet"
    _touch(month)
    before = lake_revision_token([bars])
    # An atomic append into the EXISTING month file bumps the containing dir's mtime.
    _atomic_rewrite(month, b"xy")
    after = lake_revision_token([bars])
    assert after != before


def test_revision_token_is_stable_on_unchanged_lake(tmp_path: Path) -> None:
    bars = tmp_path / "bars"
    _touch(bars / "NSE" / "INDEX" / "NIFTY" / "1minute" / "2020" / "01.parquet")
    assert lake_revision_token([bars]) == lake_revision_token([bars])


def test_revision_token_sums_multiple_roots(tmp_path: Path) -> None:
    bars = tmp_path / "bars"
    options = tmp_path / "options"
    _touch(bars / "NSE" / "INDEX" / "NIFTY" / "1minute" / "2020" / "01.parquet")
    bars_only = lake_revision_token([bars])
    # Adding the options root with its own partitions raises the dir count.
    _touch(options / "NIFTY-CE" / "2024-01.parquet")
    both = lake_revision_token([bars, options])
    assert both != bars_only
    assert int(both.partition(":")[0]) > int(bars_only.partition(":")[0])


# ── partition_parquet_mtime_token ───────────────────────────────────────────────


def test_partition_mtime_absent_base_is_zero(tmp_path: Path) -> None:
    assert partition_parquet_mtime_token(tmp_path / "bars" / "NSE" / "X" / "Y") == 0


def test_partition_mtime_empty_base_is_zero(tmp_path: Path) -> None:
    base = tmp_path / "bars" / "NSE" / "INDEX" / "NIFTY" / "1minute"
    base.mkdir(parents=True)  # exists but holds no Parquet
    assert partition_parquet_mtime_token(base) == 0


def test_partition_mtime_returns_newest_parquet(tmp_path: Path) -> None:
    base = tmp_path / "bars" / "NSE" / "INDEX" / "NIFTY" / "1minute"
    f = base / "2020" / "01.parquet"
    _touch(f)
    tok = partition_parquet_mtime_token(base)
    assert tok == f.stat().st_mtime_ns


def test_partition_mtime_moves_on_in_place_rewrite(tmp_path: Path) -> None:
    base = tmp_path / "bars" / "NSE" / "INDEX" / "NIFTY" / "1minute"
    f = base / "2020" / "01.parquet"
    _touch(f)
    before = partition_parquet_mtime_token(base)
    _atomic_rewrite(f, b"xy")
    after = partition_parquet_mtime_token(base)
    assert after != before
    assert after == f.stat().st_mtime_ns


def test_partition_mtime_ignores_non_parquet(tmp_path: Path) -> None:
    base = tmp_path / "bars" / "NSE" / "INDEX" / "NIFTY" / "1minute"
    _touch(base / "2020" / "notes.txt")  # not a *.parquet → contributes nothing
    assert partition_parquet_mtime_token(base) == 0


def test_partition_mtime_takes_max_over_multiple_files(tmp_path: Path) -> None:
    base = tmp_path / "bars" / "NSE" / "INDEX" / "NIFTY" / "1minute"
    f1 = base / "2020" / "01.parquet"
    f2 = base / "2020" / "02.parquet"
    _touch(f1)
    _touch(f2)
    # Pin DETERMINISTIC mtimes (independent of the filesystem's walk order): the token
    # is the MAX over both files, regardless of which is visited first.
    os.utime(f1, ns=(2_000_000_000, 2_000_000_000))
    os.utime(f2, ns=(1_000_000_000, 1_000_000_000))
    tok = partition_parquet_mtime_token(base)
    assert tok == f1.stat().st_mtime_ns == 2_000_000_000
