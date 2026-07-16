"""O(1)/zero-cost pins for the feed-root resolver on the default (Groww) path.

DEC-FEED-LAKE-ROOT-001 (MIGRATION.md #1832), Golden Rule #5 / #10. The feed-root
nesting law must add ZERO cost to the default write/read path. Two deterministic,
noise-free teeth (a wall-clock ratio would be secondary — the real claim is a
STRUCTURAL zero-syscall / zero-extra-touch invariant, per CLAUDE.md §11 SCALABLE):

* :func:`~brutex.data.feed_root.feed_lake_root` /
  :func:`~brutex.data.feed_root.feed_of_partition_path` are pure + lexical — they
  touch the filesystem ZERO times, at any path depth (spy-pinned). Root
  resolution happens once per sink construction, never per bar (Golden Rule #10:
  no new hot path is claimed).
* An existing lake reader's ``bars/`` walk touches ZERO ``feeds/`` paths and
  performs the EXACT same filesystem operations whether the sibling
  ``feeds/<f>/`` subtree is absent or heavily populated — the feed tree adds no
  cost to a default-path scan (structural non-vacuity: the populated tree really
  is on disk).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from brutex.dashboard.db_view import _walk_candle_datasets
from brutex.data.feed_root import feed_lake_root, feed_of_partition_path


class TestResolverZeroSyscalls:
    """The pure resolvers touch the filesystem zero times, at any depth."""

    def test_feed_lake_root_zero_fs_syscalls(self, tmp_path: Path) -> None:
        counts = {"stat": 0, "lstat": 0, "scandir": 0, "listdir": 0, "open": 0}
        with pytest.MonkeyPatch.context() as patch:
            real = {n: getattr(os, n) for n in counts}

            def make(n: str) -> Any:
                def inner(*a: Any, **k: Any) -> Any:
                    counts[n] += 1
                    return real[n](*a, **k)

                return inner

            for n in counts:
                patch.setattr(os, n, make(n))
            for _ in range(1000):
                assert feed_lake_root(tmp_path, feed="groww") is tmp_path
                feed_lake_root(tmp_path, feed="feedx")
        assert sum(counts.values()) == 0, f"feed_lake_root did filesystem I/O: {counts}"

    def test_feed_of_partition_path_zero_fs_syscalls_at_any_depth(self, tmp_path: Path) -> None:
        # A DEEP path (a fully-materialised options partition) and a shallow one:
        # attribution is lexical, so neither touches the filesystem.
        deep = (
            tmp_path
            / "feeds"
            / "feedx"
            / "options"
            / "NSE_FNO__25__MONTH__1__ATM-10__CALL"
            / "2024"
            / "01.parquet"
        )
        shallow = tmp_path / "bars" / "NSE" / "INDEX" / "NIFTY" / "1minute" / "2024" / "01.parquet"
        counts = {"stat": 0, "lstat": 0, "scandir": 0, "listdir": 0, "open": 0}
        with pytest.MonkeyPatch.context() as patch:
            real = {n: getattr(os, n) for n in counts}

            def make(n: str) -> Any:
                def inner(*a: Any, **k: Any) -> Any:
                    counts[n] += 1
                    return real[n](*a, **k)

                return inner

            for n in counts:
                patch.setattr(os, n, make(n))
            for _ in range(1000):
                assert feed_of_partition_path(deep, lake_root=tmp_path) == "feedx"
                assert feed_of_partition_path(shallow, lake_root=tmp_path) == "groww"
        assert sum(counts.values()) == 0, f"feed_of_partition_path did filesystem I/O: {counts}"


class TestDefaultPathScanUnaffectedByFeedTree:
    """A ``bars/`` walk touches zero ``feeds/`` paths and equal fs ops ± the tree."""

    @staticmethod
    def _make_top_lake(root: Path) -> None:
        """A tiny valid spot dataset dir with one (fake) partition file."""
        part = root / "bars" / "NSE" / "INDEX" / "NIFTY" / "1minute" / "2024"
        part.mkdir(parents=True, exist_ok=True)
        (part / "01.parquet").write_bytes(b"x")  # the walk only tests existence

    @staticmethod
    def _populate_feed_tree(root: Path, *, partitions: int) -> int:
        """Create a heavily-populated sibling ``feeds/feedx/bars/...`` tree.

        Uses raw mkdir/touch (the walk never reads bytes) so it is fast. Returns
        the number of parquet files planted.
        """
        made = 0
        base = root / "feeds" / "feedx" / "bars" / "NSE" / "FNO"
        for i in range(partitions):
            part = base / f"CONTRACT{i:04d}" / "1minute" / "2024"
            part.mkdir(parents=True, exist_ok=True)
            (part / "01.parquet").write_bytes(b"x")
            made += 1
        return made

    def _measured_walk(self, root: Path) -> tuple[dict[str, int], list[str]]:
        """Run the bars walk under an fs spy; return (op counts, touched paths)."""
        counts = {"stat": 0, "lstat": 0, "scandir": 0, "listdir": 0}
        touched: list[str] = []
        with pytest.MonkeyPatch.context() as patch:
            real = {n: getattr(os, n) for n in counts}

            def make(n: str) -> Any:
                def inner(path: Any = ".", *a: Any, **k: Any) -> Any:
                    counts[n] += 1
                    touched.append(str(path))
                    return real[n](path, *a, **k)

                return inner

            for n in counts:
                patch.setattr(os, n, make(n))
            result = _walk_candle_datasets(root, exclude_segments=frozenset(), intervals=None)
        assert result == [
            {
                "kind": "spot",
                "exchange": "NSE",
                "segment": "INDEX",
                "symbol": "NIFTY",
                "interval": "1minute",
            }
        ]
        return counts, touched

    def test_walk_touches_zero_feeds_paths_and_equal_ops(self, tmp_path: Path) -> None:
        bare = tmp_path / "bare"
        self._make_top_lake(bare)
        bare_counts, _bare_touched = self._measured_walk(bare)

        populated = tmp_path / "pop"
        self._make_top_lake(populated)
        planted = self._populate_feed_tree(populated, partitions=200)
        pop_counts, pop_touched = self._measured_walk(populated)

        # Structural non-vacuity: the feed tree really carries the extra files.
        assert planted == 200
        assert len(list((populated / "feeds").rglob("*.parquet"))) == 200

        # No walked path is under feeds/ — the walk never descends the subtree.
        assert not any((os.sep + "feeds" + os.sep) in p for p in pop_touched), (
            "the bars/ walk descended into feeds/ — the feed tree is not invisible"
        )
        # The default-path scan's filesystem cost is IDENTICAL ± the feed tree.
        assert pop_counts == bare_counts, (
            f"a feed subtree changed the default walk's fs ops "
            f"(bare={bare_counts}, populated={pop_counts})"
        )
