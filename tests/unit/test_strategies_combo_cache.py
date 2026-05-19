"""Unit tests for :class:`brutex.strategies.sweep.ComboCache` (LMDB-backed)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from brutex.strategies.combo import combo_hash
from brutex.strategies.errors import StrategyError
from brutex.strategies.sweep import ComboCache, ComboResult

pytest.importorskip("lmdb")


def _make_result(ch: str, signal_count: int = 5) -> ComboResult:
    return ComboResult(
        combo_hash=ch,
        strategy_id="test",
        params={"fast": 5, "slow": 20},
        signal_count=signal_count,
        bars_observed=100,
        elapsed_seconds=0.123,
        run_at=datetime(2024, 1, 1, 9, 15, tzinfo=UTC),
    )


def _hash(suffix: str = "a") -> str:
    # DEC-P0-060 (PR-N+7): pass the 5 new combo_hash fields with placeholder
    # strings until the engine PR (G-03 GAP) wires real values.
    return combo_hash(
        strategy_id=f"strat_{suffix}",
        direction="N/A",
        instrument="N/A",
        tf="N/A",
        mode="N/A",
        params={"fast": 5},
        data_snapshot_hash="a" * 64,
        indicator_version="N/A",
        code_commit_sha="deadbeef",
    )


class TestComboCacheBasic:
    def test_open_creates_directory(self, tmp_path: Path) -> None:
        path = tmp_path / "combo-cache"
        with ComboCache.open(path) as cache:
            assert cache.path == path
        assert path.exists()
        assert path.is_dir()

    def test_put_and_get_roundtrip(self, tmp_path: Path) -> None:
        with ComboCache.open(tmp_path / "c") as cache:
            ch = _hash()
            result = _make_result(ch, signal_count=42)
            cache.put(result)

            fetched = cache.get(ch)
            assert fetched is not None
            assert fetched.combo_hash == ch
            assert fetched.signal_count == 42
            assert fetched.strategy_id == "test"
            # cache_hit flag flips on read.
            assert fetched.cache_hit is True

    def test_get_missing_returns_none(self, tmp_path: Path) -> None:
        with ComboCache.open(tmp_path / "c") as cache:
            assert cache.get(_hash()) is None

    def test_exists_matches_get(self, tmp_path: Path) -> None:
        with ComboCache.open(tmp_path / "c") as cache:
            ch = _hash()
            assert not cache.exists(ch)
            cache.put(_make_result(ch))
            assert cache.exists(ch)

    def test_put_overwrites_existing(self, tmp_path: Path) -> None:
        with ComboCache.open(tmp_path / "c") as cache:
            ch = _hash()
            cache.put(_make_result(ch, signal_count=1))
            cache.put(_make_result(ch, signal_count=999))
            fetched = cache.get(ch)
            assert fetched is not None
            assert fetched.signal_count == 999
            assert cache.count() == 1

    def test_count_tracks_unique_hashes(self, tmp_path: Path) -> None:
        with ComboCache.open(tmp_path / "c") as cache:
            assert cache.count() == 0
            cache.put(_make_result(_hash("a")))
            cache.put(_make_result(_hash("b")))
            cache.put(_make_result(_hash("c")))
            assert cache.count() == 3

    def test_persistence_across_reopens(self, tmp_path: Path) -> None:
        """Data must survive close + reopen."""
        path = tmp_path / "c"
        ch = _hash()
        with ComboCache.open(path) as cache:
            cache.put(_make_result(ch, signal_count=7))

        with ComboCache.open(path) as cache2:
            fetched = cache2.get(ch)
        assert fetched is not None
        assert fetched.signal_count == 7

    def test_operations_on_closed_cache_raise(self, tmp_path: Path) -> None:
        cache = ComboCache.open(tmp_path / "c")
        cache.close()
        with pytest.raises(StrategyError, match="closed"):
            cache.put(_make_result(_hash()))
        with pytest.raises(StrategyError, match="closed"):
            cache.get(_hash())
        with pytest.raises(StrategyError, match="closed"):
            cache.exists(_hash())
        with pytest.raises(StrategyError, match="closed"):
            cache.count()

    def test_close_is_idempotent(self, tmp_path: Path) -> None:
        cache = ComboCache.open(tmp_path / "c")
        cache.close()
        cache.close()  # must not raise


class TestComboCacheValidation:
    def test_invalid_hash_length_raises(self, tmp_path: Path) -> None:
        with ComboCache.open(tmp_path / "c") as cache:
            with pytest.raises(StrategyError, match="64-char"):
                cache.get("tooshort")
            with pytest.raises(StrategyError, match="64-char"):
                cache.exists("tooshort")

    def test_invalid_hash_nonhex_raises(self, tmp_path: Path) -> None:
        with (
            ComboCache.open(tmp_path / "c") as cache,
            pytest.raises(StrategyError, match="not valid hex"),
        ):
            cache.get("z" * 64)


class TestComboResultSerialisation:
    def test_to_json_roundtrip(self) -> None:
        ch = _hash()
        original = _make_result(ch, signal_count=17)
        payload = original.to_json()
        restored = ComboResult.from_json(payload)
        assert restored.combo_hash == original.combo_hash
        assert restored.strategy_id == original.strategy_id
        assert dict(restored.params) == dict(original.params)
        assert restored.signal_count == original.signal_count
        assert restored.bars_observed == original.bars_observed
        assert restored.elapsed_seconds == original.elapsed_seconds
        assert restored.run_at == original.run_at
        # from_json flips cache_hit.
        assert restored.cache_hit is True
