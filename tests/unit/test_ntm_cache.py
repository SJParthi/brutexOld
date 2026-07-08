"""Tests for ``brutex.dashboard.ntm_cache`` — the NTM constituent CSV cache.

100% line+branch on the pure disk-cache (DEC-NTM-CONSTITUENT-CACHE-001): save +
load round-trip, the empty-input no-op, the missing/empty-file None, the atomic
overwrite, and the best-effort never-raises contract (a save into an unwritable
location returns False rather than blowing up the instrument load it serves).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from brutex.dashboard.ntm_cache import (
    REVALIDATE_LOCK_FILENAME,
    CachedConstituents,
    default_cache_dir,
    load_cached_constituent_csv,
    load_cached_constituent_with_age,
    revalidate_lock,
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


# ── load_cached_constituent_with_age (DEC-NTM-CACHE-FIRST-001) ────────────────
def test_with_age_returns_text_and_age(tmp_path: Path) -> None:
    """A present non-empty cache returns its text + a non-negative age (now - mtime)."""
    save_constituent_csv(_CSV, cache_dir=tmp_path)
    mtime = (tmp_path / "ntm_constituents.csv").stat().st_mtime
    loaded = load_cached_constituent_with_age(cache_dir=tmp_path, now=mtime + 120.0)
    assert isinstance(loaded, CachedConstituents)
    assert loaded.csv_text == _CSV
    assert loaded.age_seconds == 120.0


def test_with_age_clamps_future_mtime_to_zero(tmp_path: Path) -> None:
    """A future mtime (clock skew / copied file) clamps the age at 0.0, never negative."""
    save_constituent_csv(_CSV, cache_dir=tmp_path)
    mtime = (tmp_path / "ntm_constituents.csv").stat().st_mtime
    loaded = load_cached_constituent_with_age(cache_dir=tmp_path, now=mtime - 500.0)
    assert loaded is not None
    assert loaded.age_seconds == 0.0


def test_with_age_default_now_uses_wall_clock(tmp_path: Path) -> None:
    """With ``now`` omitted the age is measured against the wall clock (≈0 for a fresh write)."""
    save_constituent_csv(_CSV, cache_dir=tmp_path)
    loaded = load_cached_constituent_with_age(cache_dir=tmp_path)
    assert loaded is not None
    assert loaded.csv_text == _CSV
    assert loaded.age_seconds >= 0.0  # just-written → tiny non-negative age


def test_with_age_returns_none_when_absent(tmp_path: Path) -> None:
    assert load_cached_constituent_with_age(cache_dir=tmp_path / "nope") is None


def test_with_age_returns_none_for_empty_file(tmp_path: Path) -> None:
    (tmp_path / "ntm_constituents.csv").write_text("", encoding="utf-8")
    assert load_cached_constituent_with_age(cache_dir=tmp_path) is None


def test_with_age_returns_none_when_path_is_a_directory(tmp_path: Path) -> None:
    # The cache "file" path is a directory → read_text raises OSError → None.
    (tmp_path / "ntm_constituents.csv").mkdir()
    assert load_cached_constituent_with_age(cache_dir=tmp_path) is None


def test_with_age_returns_none_when_stat_fails(tmp_path: Path) -> None:
    """A read that succeeds but a stat that fails (file vanished mid-read) → None.

    Exercises the ``stat`` arm of the combined ``read_text``+``stat`` try-block by
    deleting the file the instant ``read_text`` returns, so the subsequent ``stat``
    raises ``FileNotFoundError`` (an ``OSError``) and the helper degrades to ``None``.
    """
    target = tmp_path / "ntm_constituents.csv"
    save_constituent_csv(_CSV, cache_dir=tmp_path)
    real_read_text = Path.read_text

    def _read_then_delete(self: Path, *args: object, **kwargs: object) -> str:
        text = real_read_text(self, *args, **kwargs)  # type: ignore[arg-type]
        os.unlink(target)  # remove before the stat() call inside the helper
        return text

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(Path, "read_text", _read_then_delete)
        assert load_cached_constituent_with_age(cache_dir=tmp_path) is None


# ── Cross-process revalidation lock (DEC-NTM-REVALIDATE-CROSSPROC-001) ────────


def test_revalidate_lock_acquired_is_exclusive_and_released_on_exit(tmp_path: Path) -> None:
    """Acquired = a REAL exclusive flock; exit releases it for the next taker."""
    import fcntl

    with revalidate_lock(cache_dir=tmp_path) as verdict:
        assert verdict == "acquired"
        # Probe from a second fd (flock is per open-file-description, so a
        # same-process second open genuinely contends): must be held.
        probe = os.open(tmp_path / REVALIDATE_LOCK_FILENAME, os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(probe)
    # After exit the lock is free again.
    with revalidate_lock(cache_dir=tmp_path) as verdict:
        assert verdict == "acquired"


def test_revalidate_lock_held_elsewhere_when_peer_holds(tmp_path: Path) -> None:
    """A peer holding the sidecar → 'held_elsewhere'; the peer's lock survives us."""
    import fcntl

    with revalidate_lock(cache_dir=tmp_path) as outer:
        assert outer == "acquired"
        with revalidate_lock(cache_dir=tmp_path) as inner:
            assert inner == "held_elsewhere"
        # Closing the inner fd must NOT have released the outer holder's lock.
        probe = os.open(tmp_path / REVALIDATE_LOCK_FILENAME, os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(probe)


def test_revalidate_lock_unavailable_when_cache_dir_is_a_file(tmp_path: Path) -> None:
    """A cache_dir path occupied by a FILE → 'unavailable' (fail-open), never a raise."""
    blocker = tmp_path / "cache"
    blocker.write_text("not a directory", encoding="utf-8")
    with revalidate_lock(cache_dir=blocker) as verdict:
        assert verdict == "unavailable"


def test_revalidate_lock_creates_dir_and_private_sidecar(tmp_path: Path) -> None:
    """A missing cache dir is created; the sidecar is owner-only (0o600)."""
    import stat

    cache_dir = tmp_path / "fresh" / "cache"
    with revalidate_lock(cache_dir=cache_dir) as verdict:
        assert verdict == "acquired"
    sidecar = cache_dir / REVALIDATE_LOCK_FILENAME
    assert sidecar.is_file()
    assert stat.S_IMODE(sidecar.stat().st_mode) == 0o600
