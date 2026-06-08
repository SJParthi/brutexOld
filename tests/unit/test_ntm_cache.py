"""Tests for ``brutex.dashboard.ntm_cache`` — the NTM constituent CSV cache.

100% line+branch on the pure disk-cache (DEC-NTM-CONSTITUENT-CACHE-001): save +
load round-trip, the empty-input no-op, the missing/empty-file None, the atomic
overwrite, and the best-effort never-raises contract (a save into an unwritable
location returns False rather than blowing up the instrument load it serves).
"""

from __future__ import annotations

from pathlib import Path

from brutex.dashboard.ntm_cache import (
    default_cache_dir,
    load_cached_constituent_csv,
    save_constituent_csv,
)

_CSV = (
    "Company Name,Industry,Symbol,Series,ISIN Code\nSmallCo One Ltd.,X,SMALLCO1,EQ,INE111A01011\n"
)


def test_save_then_load_round_trips(tmp_path: Path) -> None:
    assert save_constituent_csv(_CSV, cache_dir=tmp_path) is True
    assert load_cached_constituent_csv(cache_dir=tmp_path) == _CSV


def test_load_returns_none_when_absent(tmp_path: Path) -> None:
    assert load_cached_constituent_csv(cache_dir=tmp_path / "nope") is None


def test_save_empty_is_a_noop(tmp_path: Path) -> None:
    # An empty body must NOT overwrite a good cache with nothing.
    assert save_constituent_csv("", cache_dir=tmp_path) is False
    assert load_cached_constituent_csv(cache_dir=tmp_path) is None


def test_save_overwrites_with_latest(tmp_path: Path) -> None:
    save_constituent_csv("old\n", cache_dir=tmp_path)
    save_constituent_csv("new\n", cache_dir=tmp_path)
    assert load_cached_constituent_csv(cache_dir=tmp_path) == "new\n"


def test_load_empty_file_returns_none(tmp_path: Path) -> None:
    # A zero-byte cache file is treated as "no cache" (not an empty fallback).
    (tmp_path / "ntm_constituents.csv").write_text("", encoding="utf-8")
    assert load_cached_constituent_csv(cache_dir=tmp_path) is None


def test_save_into_unwritable_path_returns_false(tmp_path: Path) -> None:
    # Best-effort: a cache dir whose parent is a FILE (mkdir fails) must not raise.
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file, not a dir", encoding="utf-8")
    assert save_constituent_csv(_CSV, cache_dir=blocker / "sub") is False


def test_load_when_path_is_a_directory_returns_none(tmp_path: Path) -> None:
    # The cache "file" path is actually a directory → read raises OSError → None.
    (tmp_path / "ntm_constituents.csv").mkdir()
    assert load_cached_constituent_csv(cache_dir=tmp_path) is None


def test_default_cache_dir_is_under_home() -> None:
    d = default_cache_dir()
    assert d.name == "cache"
    assert d.parent.name == ".brutex"
    assert d == Path.home() / ".brutex" / "cache"
