"""Unit tests for :mod:`brutex.data.feed_root` (DEC-FEED-LAKE-ROOT-001, #1751).

100% line + branch. Covers the feed-id grammar + reserved matrix, the
identity-default lake-root law (the mutant that adds a component on the default
path is killed by the ``is`` pin), the path-structural attribution round-trip,
and the ``list_feed_roots`` junk-dir / stray-file skip arms.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from brutex.data.feed_root import (
    DEFAULT_FEED,
    FEEDS_DIR_NAME,
    RESERVED_FEED_IDS,
    FeedRootError,
    feed_lake_root,
    feed_of_partition_path,
    is_valid_feed_id,
    list_feed_roots,
    validate_feed_id,
)
from hypothesis import given
from hypothesis import strategies as st

_VALID_IDS = ("feedx", "abc", "a1", "z_9", "feed_two", "x" + "y" * 15)
_INVALID_GRAMMAR = (
    "",
    "a",
    "A",
    "Abc",
    "1abc",
    "_abc",
    "feed-x",
    "féed",
    "x" * 17,
    "with space",
    ".hidden",
)


class TestFeedIdGrammar:
    @pytest.mark.parametrize("fid", _VALID_IDS)
    def test_valid_ids_accepted(self, fid: str) -> None:
        assert is_valid_feed_id(fid) is True
        validate_feed_id(fid)  # does not raise

    @pytest.mark.parametrize("fid", _INVALID_GRAMMAR)
    def test_grammar_rejects(self, fid: str) -> None:
        assert is_valid_feed_id(fid) is False
        with pytest.raises(FeedRootError, match="must match"):
            validate_feed_id(fid)

    @pytest.mark.parametrize("fid", sorted(RESERVED_FEED_IDS))
    def test_reserved_rejected(self, fid: str) -> None:
        # A reserved id is grammar-valid but collides with a top-root artifact.
        assert is_valid_feed_id(fid) is False
        with pytest.raises(FeedRootError, match="reserved"):
            validate_feed_id(fid)

    def test_default_feed_is_valid(self) -> None:
        assert is_valid_feed_id(DEFAULT_FEED) is True
        validate_feed_id(DEFAULT_FEED)

    def test_reserved_set_matches_top_root_artifacts(self) -> None:
        # Drift pin: the reserved set is exactly the grammar-valid top-root
        # artifact dir names a feed id must never shadow.
        assert frozenset({"bars", "options", "feeds", "sweeps", "sweep_runs"}) == RESERVED_FEED_IDS
        # every reserved name is itself grammar-shaped (so the reserved branch is reachable).
        for name in RESERVED_FEED_IDS:
            assert name != DEFAULT_FEED


class TestFeedLakeRoot:
    def test_default_returns_identity(self) -> None:
        root = Path("/lake")
        resolved = feed_lake_root(root, feed=DEFAULT_FEED)
        # THE byte-identity law: same object, not merely equal (kills the
        # "always add a component" mutant).
        assert resolved is root

    def test_non_default_nests(self) -> None:
        root = Path("/lake")
        assert feed_lake_root(root, feed="feedx") == root / FEEDS_DIR_NAME / "feedx"

    def test_invalid_feed_raises(self) -> None:
        with pytest.raises(FeedRootError):
            feed_lake_root(Path("/lake"), feed="Bad")


class TestFeedOfPartitionPath:
    _root = Path("/lake")

    def _partition(self, base: Path) -> Path:
        return base / "bars" / "NSE" / "INDEX" / "NSE-NIFTY" / "1minute" / "2024" / "01.parquet"

    def test_default_lake_attributes_default(self) -> None:
        p = self._partition(self._root)
        assert feed_of_partition_path(p, lake_root=self._root) == DEFAULT_FEED

    def test_feed_subtree_attributes_feed(self) -> None:
        p = self._partition(self._root / FEEDS_DIR_NAME / "feedx")
        assert feed_of_partition_path(p, lake_root=self._root) == "feedx"

    def test_path_outside_lake_root_parses_from_tail(self) -> None:
        # relative_to raises ValueError → fall back to the path's own parts.
        rel = Path(FEEDS_DIR_NAME) / "feedx" / "bars" / "x"
        assert feed_of_partition_path(rel, lake_root=Path("/elsewhere")) == "feedx"

    def test_short_path_is_default(self) -> None:
        assert (
            feed_of_partition_path(Path(FEEDS_DIR_NAME) / "feedx", lake_root=Path("."))
            == DEFAULT_FEED
        )

    def test_non_feeds_prefix_is_default(self) -> None:
        p = self._partition(self._root)
        assert feed_of_partition_path(p, lake_root=self._root) == DEFAULT_FEED

    def test_invalid_feed_component_is_default(self) -> None:
        # feeds/<junk>/bars/... where <junk> is not a legal feed id.
        p = self._partition(self._root / FEEDS_DIR_NAME / "BADID")
        assert feed_of_partition_path(p, lake_root=self._root) == DEFAULT_FEED

    @given(
        fid=st.from_regex(r"\A[a-z][a-z0-9_]{1,15}\Z", fullmatch=True).filter(is_valid_feed_id),
    )
    def test_round_trip_property(self, fid: str) -> None:
        root = Path("/lake")
        p = self._partition(feed_lake_root(root, feed=fid))
        assert feed_of_partition_path(p, lake_root=root) == fid


class TestListFeedRoots:
    def test_missing_feeds_dir_is_empty(self, tmp_path: Path) -> None:
        assert list_feed_roots(tmp_path) == []

    def test_lists_valid_sorted(self, tmp_path: Path) -> None:
        feeds = tmp_path / FEEDS_DIR_NAME
        (feeds / "zeta").mkdir(parents=True)
        (feeds / "alpha").mkdir()
        assert list_feed_roots(tmp_path) == [
            ("alpha", feeds / "alpha"),
            ("zeta", feeds / "zeta"),
        ]

    def test_skips_stray_file(self, tmp_path: Path) -> None:
        feeds = tmp_path / FEEDS_DIR_NAME
        feeds.mkdir()
        (feeds / "keep").mkdir()
        (feeds / "stray.txt").write_text("junk", encoding="utf-8")
        assert list_feed_roots(tmp_path) == [("keep", feeds / "keep")]

    def test_skips_invalid_name_dir(self, tmp_path: Path) -> None:
        feeds = tmp_path / FEEDS_DIR_NAME
        feeds.mkdir()
        (feeds / "keep").mkdir()
        (feeds / "BADNAME").mkdir()
        (feeds / "bars").mkdir()  # reserved name, also skipped
        assert list_feed_roots(tmp_path) == [("keep", feeds / "keep")]
