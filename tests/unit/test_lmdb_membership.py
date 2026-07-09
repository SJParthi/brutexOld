"""Tests for `brutex.data.lmdb_membership` — the persistent dedup tier.

100% line+branch over a REAL temp LMDB (no mocks): add/contains, idempotent
re-add, batch add_many, context-manager close, idempotent double-close,
the closed-env guard, and the drop-in into OptionDedupIndex as the exact tier.

R2-SL-F4-F5 additions: the ``resolve_positive_int_env`` operator-knob
resolver (explicit > ``BRUTEX_SWEEP_CACHE_MAP_MB`` > module default; loud
``ValueError`` on a bad value), the env-tunable ``open`` map ceiling, and the
graceful map-full path — a genuine ``lmdb.MapFullError`` on BOTH ``add`` and
``add_many`` surfaces as the actionable :class:`LmdbMapFullError` domain error
while a non-map-full ``lmdb.Error`` still propagates raw.

Whitebox: the knob tests read the effective map size off the private ``_env``
(DEC-P0-017), so the file silences ``reportPrivateUsage``.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

from pathlib import Path

import lmdb
import pytest
from brutex.data.lmdb_membership import (
    DEFAULT_MAP_SIZE_MB,
    SWEEP_CACHE_MAP_MB_ENV,
    LmdbCorruptEnvError,
    LmdbMapFullError,
    LmdbMembership,
    LmdbMembershipError,
    resolve_positive_int_env,
)
from brutex.data.option_dedup import OptionDedupIndex, option_bar_key

_K1 = bytes(range(32))
_K2 = bytes(range(32, 64))

_MIB = 1024 * 1024


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


def test_len_counts_stored_keys(tmp_path: Path) -> None:
    with _open(tmp_path) as m:
        assert len(m) == 0
        m.add(_K1)
        assert len(m) == 1
        m.add(_K1)  # idempotent — count stays 1
        assert len(m) == 1
        m.add(_K2)
        assert len(m) == 2


def test_iter_keys_yields_all_stored(tmp_path: Path) -> None:
    with _open(tmp_path) as m:
        assert list(m.iter_keys()) == []  # empty
        m.add_many([_K1, _K2])
        assert set(m.iter_keys()) == {_K1, _K2}
        # Every yielded key is exact bytes (not an lmdb buffer view).
        assert all(isinstance(k, bytes) for k in m.iter_keys())


class TestLenIterClosedGuard:
    def test_len_after_close_raises(self, tmp_path: Path) -> None:
        m = _open(tmp_path)
        m.close()
        with pytest.raises(LmdbMembershipError, match="closed"):
            len(m)

    def test_iter_keys_after_close_raises(self, tmp_path: Path) -> None:
        m = _open(tmp_path)
        m.close()
        with pytest.raises(LmdbMembershipError, match="closed"):
            list(m.iter_keys())


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


# ── resolve_positive_int_env — the COMMON operator-knob resolver ─────────────


class TestResolvePositiveIntEnv:
    """explicit arg > env var > ``None``; bad values raise LOUDLY (R2-SL-F4-F5)."""

    def test_explicit_wins_over_env(self) -> None:
        got = resolve_positive_int_env("KNOB", 7, env={"KNOB": "99"})
        assert got == 7

    def test_env_used_when_no_explicit(self) -> None:
        assert resolve_positive_int_env("KNOB", None, env={"KNOB": "42"}) == 42

    def test_unset_env_returns_none(self) -> None:
        assert resolve_positive_int_env("KNOB", None, env={}) is None

    def test_blank_env_treated_as_unset(self) -> None:
        assert resolve_positive_int_env("KNOB", None, env={"KNOB": "   "}) is None

    def test_default_env_source_is_os_environ(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KNOB", "13")
        assert resolve_positive_int_env("KNOB") == 13

    @pytest.mark.parametrize("bad", ["abc", "1.5", "0", "-3"])
    def test_bad_env_value_raises_naming_the_var(self, bad: str) -> None:
        with pytest.raises(ValueError, match="KNOB"):
            resolve_positive_int_env("KNOB", None, env={"KNOB": bad})

    @pytest.mark.parametrize("bad", [0, -1])
    def test_non_positive_explicit_raises_naming_the_var(self, bad: int) -> None:
        with pytest.raises(ValueError, match="KNOB"):
            resolve_positive_int_env("KNOB", bad, env={})

    def test_positive_explicit_needs_no_env(self) -> None:
        assert resolve_positive_int_env("KNOB", 1, env={}) == 1


# ── open() map-size knob threading (env > default) ──────────────────────────


class TestOpenMapSizeKnob:
    def test_env_var_sets_map_size(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(SWEEP_CACHE_MAP_MB_ENV, "1")
        with LmdbMembership.open(tmp_path / "env.lmdb") as m:
            assert m._env.info()["map_size"] == 1 * _MIB

    def test_explicit_wins_over_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(SWEEP_CACHE_MAP_MB_ENV, "1")
        with LmdbMembership.open(tmp_path / "exp.lmdb", map_size_mb=2) as m:
            assert m._env.info()["map_size"] == 2 * _MIB

    def test_unset_keeps_module_default_10_gib(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # An unset knob must NEVER shrink this store's own 10 GiB default
        # (the sweep-cache tuple store keeps its separate 2 GiB default).
        monkeypatch.delenv(SWEEP_CACHE_MAP_MB_ENV, raising=False)
        with LmdbMembership.open(tmp_path / "def.lmdb") as m:
            assert m._env.info()["map_size"] == DEFAULT_MAP_SIZE_MB * _MIB

    def test_bad_env_value_raises_loudly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(SWEEP_CACHE_MAP_MB_ENV, "not-a-size")
        with pytest.raises(ValueError, match=SWEEP_CACHE_MAP_MB_ENV):
            LmdbMembership.open(tmp_path / "bad.lmdb")


# ── graceful map-full (LmdbMapFullError, R2-SL-F4-F5) ────────────────────────


def _fill_to_map_full(m: LmdbMembership, batch_size: int = 512) -> int:
    """Commit *batch_size* batches until the map refuses one; return keys stored.

    The refused batch aborts atomically, so afterwards the map is within one
    batch of full and every earlier batch is durably committed.
    """
    i = 0
    while True:
        batch = [n.to_bytes(32, "big") for n in range(i, i + batch_size)]
        try:
            m.add_many(batch)
        except LmdbMapFullError:
            return i
        i += batch_size
        assert i < 200_000, "1 MiB map never filled — fixture broken"


def _add_until_map_full(m: LmdbMembership, start: int, bound: int = 4096) -> None:
    """Single-key adds until the map refuses one (raises LmdbMapFullError)."""
    for n in range(start, start + bound):
        m.add(n.to_bytes(32, "big"))


class TestMapFullGraceful:
    """A full memory-map surfaces as the actionable LmdbMapFullError."""

    def test_add_many_map_full_raises_domain_error(self, tmp_path: Path) -> None:
        with _open(tmp_path) as m:
            m.add(_K1)  # a committed key that must survive the overflow
            with pytest.raises(LmdbMapFullError) as excinfo:
                m.add_many(n.to_bytes(32, "big") for n in range(60_000))
            # Domain error, riding the module's existing hierarchy.
            assert isinstance(excinfo.value, LmdbMembershipError)
            # Actionable message: names the CURRENT map size + BOTH remedies.
            msg = str(excinfo.value)
            assert "1 MiB" in msg
            assert "--sweep-cache-map-mb" in msg
            assert SWEEP_CACHE_MAP_MB_ENV in msg
            # The failing batch aborted atomically; committed data is intact
            # and the store is still readable after the fault.
            assert _K1 in m
            assert len(m) == 1

    def test_add_map_full_raises_domain_error(self, tmp_path: Path) -> None:
        with _open(tmp_path) as m:
            stored = _fill_to_map_full(m)
            with pytest.raises(LmdbMapFullError, match="sweep-cache-map-mb"):
                _add_until_map_full(m, stored)
            # Reads keep working on a full map.
            assert (0).to_bytes(32, "big") in m

    def test_regrown_map_resumes_idempotently(self, tmp_path: Path) -> None:
        """The advertised remedy WORKS: re-open larger, re-add, no corruption."""
        path = tmp_path / "grow.lmdb"
        with LmdbMembership.open(path, map_size_mb=1) as m:
            stored = _fill_to_map_full(m)
        with LmdbMembership.open(path, map_size_mb=4) as grown:
            assert len(grown) == stored  # everything committed survived
            grown.add(_K2)  # the previously-refused write now lands
            assert _K2 in grown
            n = grown.add_many([_K1, _K2])  # idempotent re-add still a no-op
            assert n == 2

    def test_non_map_full_lmdb_error_propagates_raw(self, tmp_path: Path) -> None:
        """A non-map-full ``lmdb.Error`` is NOT wrapped (infra faults stay raw).

        A >511-byte key genuinely raises ``lmdb.BadValsizeError`` — an
        ``lmdb.Error`` that is not ``MapFullError`` — through both write paths.
        """
        with _open(tmp_path) as m:
            oversized = b"x" * 600
            with pytest.raises(lmdb.BadValsizeError):
                m.add(oversized)
            with pytest.raises(lmdb.BadValsizeError):
                m.add_many([oversized])


# ── R2-D2 (DEC-SWEEP-LMDB-QUARANTINE-REBUILD-001, MIGRATION.md #1632) ──────────


def _corrupt_data_mdb(env_dir: Path) -> None:
    """XOR the first min(4096, len) bytes of a real ``data.mdb`` with 0xFF."""
    data_mdb = env_dir / "data.mdb"
    raw = bytearray(data_mdb.read_bytes())
    n = min(4096, len(raw))
    for i in range(n):
        raw[i] ^= 0xFF
    data_mdb.write_bytes(bytes(raw))


def test_corrupt_env_raises_typed_not_raw_lmdb_error(tmp_path: Path) -> None:
    """A corrupt ``data.mdb`` raises the typed ``LmdbCorruptEnvError`` at open.

    Fix-reversion (R2-D2): pre-fix ``lmdb.open`` raises a raw ``lmdb.Error``
    (e.g. ``lmdb.InvalidError``) out of :meth:`LmdbMembership.open`; post-fix the
    fault is mapped to :class:`LmdbCorruptEnvError` (a ``LmdbMembershipError``), so
    the SweepCache consumer can quarantine + rebuild.
    """
    env_dir = tmp_path / "members.lmdb"
    with LmdbMembership.open(env_dir, map_size_mb=1) as m:
        m.add(_K1)
    _corrupt_data_mdb(env_dir)
    with pytest.raises(LmdbCorruptEnvError) as excinfo:
        LmdbMembership.open(env_dir, map_size_mb=1)
    assert str(env_dir) in str(excinfo.value)
    # It IS a LmdbMembershipError (the domain hierarchy), never a raw lmdb.Error.
    assert isinstance(excinfo.value, LmdbMembershipError)
    assert not isinstance(excinfo.value, lmdb.Error)


def test_clean_env_opens_for_non_sweepcache_consumer(tmp_path: Path) -> None:
    """A clean env opens + reads normally — the corruption guard never false-trips."""
    env_dir = tmp_path / "members.lmdb"
    with LmdbMembership.open(env_dir, map_size_mb=1) as m:
        m.add(_K1)
    # Re-open the SAME clean env (a non-SweepCache consumer, e.g. option-dedup).
    with LmdbMembership.open(env_dir, map_size_mb=1) as m2:
        assert _K1 in m2
