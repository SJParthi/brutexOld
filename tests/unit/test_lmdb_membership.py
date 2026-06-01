"""Tests for `brutex.data.lmdb_membership` — the persistent dedup tier.

100% line+branch over a REAL temp LMDB (no mocks): add/contains, idempotent
re-add, batch add_many, context-manager close, idempotent double-close,
the closed-env guard, and the drop-in into OptionDedupIndex as the exact tier.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from brutex.data.lmdb_membership import LmdbMembership, LmdbMembershipError
from brutex.data.option_dedup import OptionDedupIndex, option_bar_key

_K1 = bytes(range(32))
_K2 = bytes(range(32, 64))


def _open(tmp_path: Path) -> LmdbMembership:
    return LmdbMembership.open(tmp_path / "members.lmdb", map_size_mb=1)


def test_add_then_contains(tmp_path: Path) -> None:
    with _open(tmp_path) as m:
        assert _K1 not in m
        m.add(_K1)
        assert _K1 in m
        assert _K2 not in m


def test_readd_is_idempotent(tmp_path: Path) -> None:
    with _open(tmp_path) as m:
        m.add(_K1)
        m.add(_K1)  # no-op overwrite, no corruption
        assert _K1 in m


def test_add_many_batches(tmp_path: Path) -> None:
    with _open(tmp_path) as m:
        n = m.add_many([_K1, _K2])
        assert n == 2
        assert _K1 in m and _K2 in m


def test_persists_across_reopen(tmp_path: Path) -> None:
    path = tmp_path / "persist.lmdb"
    with LmdbMembership.open(path, map_size_mb=1) as m:
        m.add(_K1)
    # Re-open the same env — the key survives (off-heap, durable).
    with LmdbMembership.open(path, map_size_mb=1) as m2:
        assert _K1 in m2


def test_double_close_is_idempotent(tmp_path: Path) -> None:
    m = _open(tmp_path)
    m.close()
    m.close()  # second close is a no-op


class TestClosedGuard:
    def test_contains_after_close_raises(self, tmp_path: Path) -> None:
        m = _open(tmp_path)
        m.close()
        with pytest.raises(LmdbMembershipError, match="closed"):
            _K1 in m  # noqa: B015 — exercising __contains__ guard

    def test_add_after_close_raises(self, tmp_path: Path) -> None:
        m = _open(tmp_path)
        m.close()
        with pytest.raises(LmdbMembershipError, match="closed"):
            m.add(_K1)

    def test_add_many_after_close_raises(self, tmp_path: Path) -> None:
        m = _open(tmp_path)
        m.close()
        with pytest.raises(LmdbMembershipError, match="closed"):
            m.add_many([_K1])


def test_drop_in_as_dedup_exact_tier(tmp_path: Path) -> None:
    # The headline: LmdbMembership satisfies ExactMembership, so it is the
    # persistent exact-confirm tier of the O(1) dedup index.
    with _open(tmp_path) as m:
        index = OptionDedupIndex(capacity=1024, fpr_target=1e-3, exact=m)
        key = option_bar_key(
            underlying="NIFTY", expiry="2021-01-07", strike="17500", side="CE", epoch=1
        )
        assert index.add_if_new(key) is True
        assert index.add_if_new(key) is False  # duplicate → dropped via LMDB confirm
        assert key in m  # the key persisted to the off-heap tier
