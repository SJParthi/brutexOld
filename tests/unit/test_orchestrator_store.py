"""Unit tests for brutex.orchestrator.store — the LMDB WorkItemStore.

Every test uses tmp_path so the underlying LMDB environment is created
in an isolated directory and cleaned up automatically.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path

import pytest
from brutex.orchestrator.state import (
    IngestPhase,
    Outcome,
    QuarantineReason,
    WorkItem,
    WorkItemState,
    dispatch,
    transition,
)
from brutex.orchestrator.store import (
    CONTENT_HASH_BYTES,
    DEFAULT_MAP_SIZE_MB,
    MAX_DBS,
    StoreCorruptionError,
    StoreError,
    StoreKeyError,
    WorkItemStore,
    _deserialize_item,
    _encode_key,
    _serialize_item,
)

# ────────────────────────────────────────────────────────────────────────
# Fixtures
# ────────────────────────────────────────────────────────────────────────


def _make_hash(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def _make_item(
    *,
    phase: IngestPhase = IngestPhase.A_INSTRUMENTS,
    hash_seed: str = "default",
    state: WorkItemState = WorkItemState.PENDING,
) -> WorkItem:
    item = WorkItem.new(
        phase=phase,
        content_hash=_make_hash(hash_seed),
        params={"exchange": "NSE", "underlying": "NIFTY"},
    )
    if state != WorkItemState.PENDING:
        # Advance it to the requested state via legal paths
        item = dispatch(item)
        if state == WorkItemState.SUCCESS:
            item = transition(item, Outcome.OK_WITH_DATA)
        elif state == WorkItemState.NO_DATA:
            item = transition(item, Outcome.OK_EMPTY)
        elif state == WorkItemState.QUARANTINED:
            item = transition(item, Outcome.BAD_REQUEST)
        elif state == WorkItemState.RETRYING:
            item = transition(item, Outcome.RATE_LIMIT)
        elif state == WorkItemState.IN_FLIGHT:
            pass  # dispatch already moved it there
    return item


@pytest.fixture
def store_path(tmp_path: Path) -> Path:
    return tmp_path / "store.lmdb"


@pytest.fixture
def store(store_path: Path):
    with WorkItemStore.open(store_path) as s:
        yield s


# ────────────────────────────────────────────────────────────────────────
# Open / close
# ────────────────────────────────────────────────────────────────────────


class TestOpen:
    def test_creates_parent_dir(self, tmp_path: Path) -> None:
        nested = tmp_path / "nested" / "path" / "store.lmdb"
        with WorkItemStore.open(nested):
            pass
        assert nested.exists()

    def test_reopen_resumes(self, store_path: Path) -> None:
        hash_a = _make_hash("a")
        with WorkItemStore.open(store_path) as s:
            s.put(_make_item(hash_seed="a"))

        # Re-open
        with WorkItemStore.open(store_path) as s:
            fetched = s.get(IngestPhase.A_INSTRUMENTS, hash_a)
            assert fetched is not None
            assert fetched.content_hash == hash_a

    def test_read_only_mode(self, store_path: Path) -> None:
        with WorkItemStore.open(store_path) as s:
            s.put(_make_item(hash_seed="a"))

        # Re-open read-only
        with WorkItemStore.open(store_path, read_only=True) as s:
            item = s.get(IngestPhase.A_INSTRUMENTS, _make_hash("a"))
            assert item is not None

    def test_close_idempotent(self, store_path: Path) -> None:
        s = WorkItemStore.open(store_path)
        s.close()
        s.close()  # Should not raise

    def test_operations_after_close_raise(self, store_path: Path) -> None:
        s = WorkItemStore.open(store_path)
        s.close()
        with pytest.raises(StoreError, match="closed"):
            s.put(_make_item())
        with pytest.raises(StoreError, match="closed"):
            s.get(IngestPhase.A_INSTRUMENTS, _make_hash("x"))

    def test_every_remaining_operation_after_close_raises(self, store_path: Path) -> None:
        """EVERY public read/write path guards the closed store.

        Complements ``test_operations_after_close_raise`` (put/get) with
        the five remaining guard sites — put_many, maybe_seen_terminal,
        iter_phase, count_phase, delete_phase
        (DEC-ORCH-STRICT-GATES-001 standalone-gap closure).
        """
        s = WorkItemStore.open(store_path)
        s.close()
        with pytest.raises(StoreError, match="closed"):
            s.put_many([_make_item()])
        with pytest.raises(StoreError, match="closed"):
            s.maybe_seen_terminal(IngestPhase.A_INSTRUMENTS, _make_hash("x"))
        with pytest.raises(StoreError, match="closed"):
            # iter_phase is a generator — the guard fires on first pull.
            list(s.iter_phase(IngestPhase.A_INSTRUMENTS))
        with pytest.raises(StoreError, match="closed"):
            s.count_phase(IngestPhase.A_INSTRUMENTS)
        with pytest.raises(StoreError, match="closed"):
            s.delete_phase(IngestPhase.A_INSTRUMENTS)

    def test_flush_syncs_open_store_and_noops_when_closed(self, store_path: Path) -> None:
        """``flush()`` syncs an open env and silently no-ops post-close.

        The post-close call must NOT raise — flush is the one maintenance
        path that degrades gracefully (belt-and-braces durability before a
        pause must never crash a shutdown sequence).
        """
        s = WorkItemStore.open(store_path)
        s.put(_make_item(hash_seed="flushed"))
        s.flush()
        s.flush(force=True)
        s.close()
        s.flush()  # no raise — closed flush is a no-op


# ────────────────────────────────────────────────────────────────────────
# Put / Get round-trip
# ────────────────────────────────────────────────────────────────────────


class TestPutGet:
    def test_put_then_get(self, store) -> None:
        item = _make_item(hash_seed="reliance")
        store.put(item)
        fetched = store.get(IngestPhase.A_INSTRUMENTS, _make_hash("reliance"))
        assert fetched is not None
        assert fetched.content_hash == item.content_hash
        assert fetched.state == WorkItemState.PENDING
        assert fetched.phase == IngestPhase.A_INSTRUMENTS

    def test_get_missing_returns_none(self, store) -> None:
        result = store.get(IngestPhase.A_INSTRUMENTS, _make_hash("nothing"))
        assert result is None

    def test_put_overwrites(self, store) -> None:
        item = _make_item(hash_seed="nifty")
        store.put(item)
        # Advance state
        dispatched = dispatch(item)
        succeeded = transition(dispatched, Outcome.OK_WITH_DATA)
        store.put(succeeded)

        fetched = store.get(IngestPhase.A_INSTRUMENTS, _make_hash("nifty"))
        assert fetched is not None
        assert fetched.state == WorkItemState.SUCCESS

    def test_cross_phase_isolation(self, store) -> None:
        """Same content_hash in different phases must not collide."""
        hash_shared = _make_hash("shared")
        item_a = WorkItem.new(
            phase=IngestPhase.A_INSTRUMENTS,
            content_hash=hash_shared,
            params={"exchange": "NSE"},
        )
        item_b = WorkItem.new(
            phase=IngestPhase.B_EXPIRIES,
            content_hash=hash_shared,
            params={"exchange": "NSE"},
        )
        store.put(item_a)
        store.put(item_b)

        assert store.get(IngestPhase.A_INSTRUMENTS, hash_shared) is not None
        assert store.get(IngestPhase.B_EXPIRIES, hash_shared) is not None
        assert store.get(IngestPhase.C_CONTRACTS, hash_shared) is None

    def test_deserialize_preserves_retry_count(self, store) -> None:
        item = _make_item(hash_seed="retry", state=WorkItemState.RETRYING)
        store.put(item)
        fetched = store.get(IngestPhase.A_INSTRUMENTS, item.content_hash)
        assert fetched is not None
        assert fetched.retry_count == 1
        assert fetched.state == WorkItemState.RETRYING

    def test_deserialize_preserves_quarantine_reason(self, store) -> None:
        item = _make_item(hash_seed="quar", state=WorkItemState.QUARANTINED)
        store.put(item)
        fetched = store.get(IngestPhase.A_INSTRUMENTS, item.content_hash)
        assert fetched is not None
        assert fetched.quarantine_reason == QuarantineReason.BAD_REQUEST

    def test_deserialize_preserves_timestamps(self, store) -> None:
        item = _make_item(hash_seed="ts")
        store.put(item)
        fetched = store.get(IngestPhase.A_INSTRUMENTS, item.content_hash)
        assert fetched is not None
        assert fetched.created_at.tzinfo is not None
        assert fetched.last_transition_at.tzinfo is not None


# ────────────────────────────────────────────────────────────────────────
# Batch put
# ────────────────────────────────────────────────────────────────────────


class TestPutMany:
    def test_batch_insert(self, store) -> None:
        items = [_make_item(hash_seed=f"item{i}") for i in range(100)]
        store.put_many(items)
        for item in items:
            assert store.get(IngestPhase.A_INSTRUMENTS, item.content_hash) is not None

    def test_empty_batch_ok(self, store) -> None:
        store.put_many([])  # Should not raise
        assert store.count_phase(IngestPhase.A_INSTRUMENTS) == 0


# ────────────────────────────────────────────────────────────────────────
# exists_terminal (dedup check)
# ────────────────────────────────────────────────────────────────────────


class TestExistsTerminal:
    def test_pending_is_not_terminal(self, store) -> None:
        item = _make_item(hash_seed="p", state=WorkItemState.PENDING)
        store.put(item)
        assert store.exists_terminal(IngestPhase.A_INSTRUMENTS, item.content_hash) is False

    def test_in_flight_is_not_terminal(self, store) -> None:
        item = _make_item(hash_seed="inf", state=WorkItemState.IN_FLIGHT)
        store.put(item)
        assert store.exists_terminal(IngestPhase.A_INSTRUMENTS, item.content_hash) is False

    def test_retrying_is_not_terminal(self, store) -> None:
        item = _make_item(hash_seed="ret", state=WorkItemState.RETRYING)
        store.put(item)
        assert store.exists_terminal(IngestPhase.A_INSTRUMENTS, item.content_hash) is False

    def test_success_is_terminal(self, store) -> None:
        item = _make_item(hash_seed="suc", state=WorkItemState.SUCCESS)
        store.put(item)
        assert store.exists_terminal(IngestPhase.A_INSTRUMENTS, item.content_hash) is True

    def test_no_data_is_terminal(self, store) -> None:
        item = _make_item(hash_seed="nod", state=WorkItemState.NO_DATA)
        store.put(item)
        assert store.exists_terminal(IngestPhase.A_INSTRUMENTS, item.content_hash) is True

    def test_quarantined_is_terminal(self, store) -> None:
        item = _make_item(hash_seed="qua", state=WorkItemState.QUARANTINED)
        store.put(item)
        assert store.exists_terminal(IngestPhase.A_INSTRUMENTS, item.content_hash) is True

    def test_missing_returns_false(self, store) -> None:
        assert store.exists_terminal(IngestPhase.A_INSTRUMENTS, _make_hash("nothing")) is False


# ────────────────────────────────────────────────────────────────────────
# Iterate / count
# ────────────────────────────────────────────────────────────────────────


class TestIterAndCount:
    def test_iter_phase_empty(self, store) -> None:
        assert list(store.iter_phase(IngestPhase.A_INSTRUMENTS)) == []

    def test_iter_phase_all_items(self, store) -> None:
        hashes = [_make_hash(f"item{i}") for i in range(5)]
        for h in hashes:
            store.put(
                WorkItem.new(
                    phase=IngestPhase.A_INSTRUMENTS,
                    content_hash=h,
                    params={"k": "v"},
                )
            )
        items = list(store.iter_phase(IngestPhase.A_INSTRUMENTS))
        assert len(items) == 5
        assert {item.content_hash for item in items} == set(hashes)

    def test_count_phase(self, store) -> None:
        store.put_many([_make_item(hash_seed=f"i{i}") for i in range(42)])
        assert store.count_phase(IngestPhase.A_INSTRUMENTS) == 42
        assert store.count_phase(IngestPhase.B_EXPIRIES) == 0

    def test_count_by_state_histogram(self, store) -> None:
        store.put(_make_item(hash_seed="s1", state=WorkItemState.SUCCESS))
        store.put(_make_item(hash_seed="s2", state=WorkItemState.SUCCESS))
        store.put(_make_item(hash_seed="n1", state=WorkItemState.NO_DATA))
        store.put(_make_item(hash_seed="q1", state=WorkItemState.QUARANTINED))
        store.put(_make_item(hash_seed="p1", state=WorkItemState.PENDING))

        counts = store.count_by_state(IngestPhase.A_INSTRUMENTS)
        assert counts.get(WorkItemState.SUCCESS, 0) == 2
        assert counts.get(WorkItemState.NO_DATA, 0) == 1
        assert counts.get(WorkItemState.QUARANTINED, 0) == 1
        assert counts.get(WorkItemState.PENDING, 0) == 1


# ────────────────────────────────────────────────────────────────────────
# delete_phase
# ────────────────────────────────────────────────────────────────────────


class TestDeletePhase:
    def test_drops_all_items(self, store) -> None:
        store.put_many([_make_item(hash_seed=f"i{i}") for i in range(10)])
        deleted = store.delete_phase(IngestPhase.A_INSTRUMENTS)
        assert deleted == 10
        assert store.count_phase(IngestPhase.A_INSTRUMENTS) == 0

    def test_other_phases_unaffected(self, store) -> None:
        item_a = _make_item(hash_seed="a", phase=IngestPhase.A_INSTRUMENTS)
        item_b = WorkItem.new(
            phase=IngestPhase.B_EXPIRIES,
            content_hash=_make_hash("b"),
            params={"k": "v"},
        )
        store.put(item_a)
        store.put(item_b)

        store.delete_phase(IngestPhase.A_INSTRUMENTS)
        assert store.count_phase(IngestPhase.A_INSTRUMENTS) == 0
        assert store.count_phase(IngestPhase.B_EXPIRIES) == 1


# ────────────────────────────────────────────────────────────────────────
# Key encoding errors
# ────────────────────────────────────────────────────────────────────────


class TestKeyEncoding:
    def test_short_hash_raises(self, store) -> None:
        bad_item = WorkItem(
            item_id="test",
            phase=IngestPhase.A_INSTRUMENTS,
            content_hash="abc123",  # too short
            params=(),
        )
        with pytest.raises(StoreKeyError, match="64 hex"):
            store.put(bad_item)

    def test_non_hex_hash_raises(self, store) -> None:
        bad_item = WorkItem(
            item_id="test",
            phase=IngestPhase.A_INSTRUMENTS,
            content_hash="z" * 64,  # not hex
            params=(),
        )
        with pytest.raises(StoreKeyError, match="not valid hex"):
            store.put(bad_item)


# ────────────────────────────────────────────────────────────────────────
# Corruption handling
# ────────────────────────────────────────────────────────────────────────


class TestCorruption:
    def test_malformed_json_raises(self, store, store_path: Path) -> None:
        """Write garbage directly into LMDB and confirm deserialize raises."""
        import lmdb

        # Close store first to release the env
        store.close()

        # Re-open the env manually and write garbage
        env = lmdb.open(
            str(store_path),
            max_dbs=8,
            subdir=True,
        )
        with env.begin(write=True) as txn:
            db = env.open_db(b"A_INSTRUMENTS", txn=txn)
            txn.put(b"\x00" * CONTENT_HASH_BYTES, b"not json at all", db=db)
        env.close()

        with WorkItemStore.open(store_path) as s, pytest.raises(StoreCorruptionError):
            s.get(IngestPhase.A_INSTRUMENTS, "00" * CONTENT_HASH_BYTES)

    def test_missing_required_fields_raises(self) -> None:
        """Valid JSON lacking required WorkItem fields names the gaps.

        Covers the schema-drift guard (store.py:174) — a value written by
        an OLDER WorkItem schema deserialises into a clear
        StoreCorruptionError, not a KeyError
        (DEC-ORCH-STRICT-GATES-001 standalone-gap closure).
        """
        with pytest.raises(StoreCorruptionError, match="missing required fields"):
            _deserialize_item(b'{"item_id": "only-one-field"}')

    def test_malformed_field_value_raises(self) -> None:
        """A field that fails enum/timestamp coercion raises corruption.

        Covers the malformed-field guard (store.py:191-192): all required
        keys present but ``phase`` is not a valid IngestPhase value
        (DEC-ORCH-STRICT-GATES-001 standalone-gap closure).
        """
        import json

        good = json.loads(_serialize_item(_make_item()))
        good["phase"] = "NOT_A_PHASE"
        with pytest.raises(StoreCorruptionError, match="malformed field"):
            _deserialize_item(json.dumps(good).encode("utf-8"))


# ────────────────────────────────────────────────────────────────────────
# BloomFilter integration (DEC-P0-048 / PR-I)
# ────────────────────────────────────────────────────────────────────────


class TestBloomNoneDelegates:
    """When bloom=None the store behaves exactly as before PR-I."""

    def test_open_without_bloom_works(self, store_path: Path) -> None:
        with WorkItemStore.open(store_path) as s:
            assert s._bloom is None  # type: ignore[reportPrivateUsage]

    def test_maybe_seen_terminal_delegates_to_exists_terminal(self, store) -> None:
        from brutex.orchestrator.state import WorkItemState

        item = _make_item(hash_seed="x", state=WorkItemState.SUCCESS)
        store.put(item)
        # SUCCESS is terminal
        assert store.maybe_seen_terminal(item.phase, item.content_hash) is True
        # Non-terminal
        pending = _make_item(hash_seed="pending", state=WorkItemState.PENDING)
        store.put(pending)
        assert store.maybe_seen_terminal(pending.phase, pending.content_hash) is False


class TestBloomIntegration:
    """Bloom-supplied store: L1 fast-fail + cold-start rebuild."""

    def test_open_with_bloom_runs_cold_start_rebuild(self, store_path: Path) -> None:
        from brutex.orchestrator.bloom import BloomFilter

        # Seed LMDB without Bloom
        with WorkItemStore.open(store_path) as s:
            s.put(_make_item(hash_seed="a"))
            s.put(_make_item(hash_seed="b"))
            s.put(_make_item(hash_seed="c"))
        # Re-open with Bloom; rebuild seeds all 3.
        bloom = BloomFilter(capacity=1000, fpr_target=1e-6)
        with WorkItemStore.open(store_path, bloom=bloom) as s:
            assert bloom.insertions_attempted == 3

    def test_put_adds_to_bloom_after_commit(self, store_path: Path) -> None:
        from brutex.orchestrator.bloom import BloomFilter

        bloom = BloomFilter(capacity=1000, fpr_target=1e-6)
        with WorkItemStore.open(store_path, bloom=bloom) as s:
            s.put(_make_item(hash_seed="x"))
            assert bloom.insertions_attempted == 1

    def test_maybe_seen_terminal_short_circuits_on_bloom_miss(self, store_path: Path) -> None:
        from brutex.orchestrator.bloom import BloomFilter

        bloom = BloomFilter(capacity=1000, fpr_target=1e-6)
        with WorkItemStore.open(store_path, bloom=bloom) as s:
            # Never added — Bloom-miss → returns False without LMDB.
            assert (
                s.maybe_seen_terminal(
                    IngestPhase.A_INSTRUMENTS,
                    _make_hash("never-put"),
                )
                is False
            )

    def test_maybe_seen_terminal_bloom_hit_then_lmdb_filters_state(self, store_path: Path) -> None:
        """Bloom-hit on a non-terminal item: falls through to LMDB and
        the terminal-state filter returns False."""
        from brutex.orchestrator.bloom import BloomFilter
        from brutex.orchestrator.state import WorkItemState

        bloom = BloomFilter(capacity=1000, fpr_target=1e-6)
        with WorkItemStore.open(store_path, bloom=bloom) as s:
            pending = _make_item(hash_seed="p", state=WorkItemState.PENDING)
            s.put(pending)
            # Bloom-hit (we just added it) but state is PENDING.
            assert s.maybe_seen_terminal(pending.phase, pending.content_hash) is False
            # Now advance to terminal.
            success = _make_item(hash_seed="p", state=WorkItemState.SUCCESS)
            s.put(success)
            assert s.maybe_seen_terminal(success.phase, success.content_hash) is True

    def test_put_many_empty_short_circuits(self, store_path: Path) -> None:
        from brutex.orchestrator.bloom import BloomFilter

        bloom = BloomFilter(capacity=1000, fpr_target=1e-6)
        with WorkItemStore.open(store_path, bloom=bloom) as s:
            s.put_many([])
            assert bloom.insertions_attempted == 0

    def test_put_many_pre_flight_rejects_oversize_batch(self, store_path: Path) -> None:
        """Pre-flight capacity check raises BEFORE opening LMDB txn."""
        from brutex.orchestrator.bloom import BloomCapacityExceeded, BloomFilter

        bloom = BloomFilter(capacity=3, fpr_target=1e-3)
        with WorkItemStore.open(store_path, bloom=bloom) as s:
            items = [_make_item(hash_seed=str(i)) for i in range(5)]
            with pytest.raises(BloomCapacityExceeded, match="put_many"):
                s.put_many(items)
            # Neither LMDB nor Bloom is mutated.
            assert bloom.insertions_attempted == 0
            assert s.get(IngestPhase.A_INSTRUMENTS, _make_hash("0")) is None

    def test_put_many_batch_adds_all_after_commit(self, store_path: Path) -> None:
        from brutex.orchestrator.bloom import BloomFilter

        bloom = BloomFilter(capacity=100, fpr_target=1e-3)
        with WorkItemStore.open(store_path, bloom=bloom) as s:
            items = [_make_item(hash_seed=str(i)) for i in range(10)]
            s.put_many(items)
            assert bloom.insertions_attempted == 10
            # Spot-check Bloom membership.
            assert bloom.might_contain(bytes.fromhex(_make_hash("5"))) is True

    def test_put_raises_when_bloom_full(self, store_path: Path) -> None:
        from brutex.orchestrator.bloom import BloomCapacityExceeded, BloomFilter

        bloom = BloomFilter(capacity=2, fpr_target=1e-3)
        with WorkItemStore.open(store_path, bloom=bloom) as s:
            s.put(_make_item(hash_seed="a"))
            s.put(_make_item(hash_seed="b"))
            # 3rd insertion past capacity. LMDB has already committed but
            # Bloom rejects.
            with pytest.raises(BloomCapacityExceeded):
                s.put(_make_item(hash_seed="c"))

    def test_cold_start_rebuild_failure_closes_store(self, store_path: Path) -> None:
        """If cold-start rebuild raises (oversize capacity), the store
        is closed and the exception propagates."""
        from brutex.orchestrator.bloom import BloomCapacityExceeded, BloomFilter

        # Seed LMDB with 10 items without Bloom.
        with WorkItemStore.open(store_path) as s:
            for i in range(10):
                s.put(_make_item(hash_seed=f"seed-{i}"))
        # Re-open with Bloom too small to hold them all.
        small_bloom = BloomFilter(capacity=3, fpr_target=1e-3)
        with pytest.raises(BloomCapacityExceeded):
            WorkItemStore.open(store_path, bloom=small_bloom)

    def test_exists_terminal_unchanged_when_bloom_supplied(self, store_path: Path) -> None:
        """exists_terminal must NOT consult Bloom — only ground-truth."""
        from brutex.orchestrator.bloom import BloomFilter
        from brutex.orchestrator.state import WorkItemState

        bloom = BloomFilter(capacity=1000, fpr_target=1e-6)
        with WorkItemStore.open(store_path, bloom=bloom) as s:
            item = _make_item(hash_seed="x", state=WorkItemState.SUCCESS)
            s.put(item)
            # exists_terminal always touches LMDB.
            assert s.exists_terminal(item.phase, item.content_hash) is True
            # exists_terminal returns False on absent hashes.
            assert s.exists_terminal(IngestPhase.A_INSTRUMENTS, _make_hash("z")) is False


# ────────────────────────────────────────────────────────────────────────
# Mutation-kill pins (DEC-MUTATION-RUNTIME-001)
# ────────────────────────────────────────────────────────────────────────


class TestConstantPins:
    """Module sizing constants pinned ``==`` (DEC-MUTATION-RUNTIME-001).

    Both constants are public API (``__all__``) and shape the LMDB
    environment geometry; a mutated value silently changes every store
    opened with defaults, so each is pinned byte-exact (house style per
    tests/unit/test_ranking.py TestCompositeWeightPins).
    """

    def test_default_map_size_mb_exactly_10240(self) -> None:
        assert DEFAULT_MAP_SIZE_MB == 10_240

    def test_max_dbs_exactly_8(self) -> None:
        assert MAX_DBS == 8


class TestExactErrorMessagePins:
    """Every store error message pinned via byte-exact ``str(exc.value)``.

    ``pytest.raises(..., match=...)`` is a regex *search* (substring), so
    an XX-wrapped string mutant still matches — only full-string ``==``
    kills it (DEC-MUTATION-RUNTIME-001; house style per
    tests/unit/test_ranking.py). Messages embedding a stdlib exception's
    text are built from the SAME triggering call so the pins stay
    version-proof.
    """

    def test_short_hash_message_byte_exact(self) -> None:
        with pytest.raises(StoreKeyError) as exc_info:
            _encode_key("abc123")
        assert str(exc_info.value) == "content_hash must be 64 hex chars (got 6)"

    def test_non_hex_hash_message_byte_exact(self) -> None:
        bad = "z" * 64
        with pytest.raises(ValueError, match="non-hexadecimal") as raw_exc:
            bytes.fromhex(bad)
        with pytest.raises(StoreKeyError) as exc_info:
            _encode_key(bad)
        assert str(exc_info.value) == f"content_hash is not valid hex: {raw_exc.value}"

    def test_invalid_json_message_byte_exact(self) -> None:
        import json

        raw = b"definitely not json"
        with pytest.raises(json.JSONDecodeError) as raw_exc:
            json.loads(raw)
        with pytest.raises(StoreCorruptionError) as exc_info:
            _deserialize_item(raw)
        assert str(exc_info.value) == f"Store entry is not valid JSON: {raw_exc.value}"

    def test_missing_required_fields_message_byte_exact(self) -> None:
        """A payload missing EXACTLY ``content_hash`` names exactly it.

        Pins both the required-set member spelling (the
        ``"XXcontent_hashXX"`` set mutant reports the wrong field name)
        and the message text around ``sorted(missing)``.
        """
        import json

        data = json.loads(_serialize_item(_make_item()))
        del data["content_hash"]
        with pytest.raises(StoreCorruptionError) as exc_info:
            _deserialize_item(json.dumps(data).encode("utf-8"))
        assert str(exc_info.value) == "Store entry missing required fields: ['content_hash']"

    def test_malformed_field_message_byte_exact(self) -> None:
        import json

        data = json.loads(_serialize_item(_make_item()))
        data["phase"] = "NOT_A_PHASE"
        with pytest.raises(ValueError, match="NOT_A_PHASE") as raw_exc:
            IngestPhase("NOT_A_PHASE")
        with pytest.raises(StoreCorruptionError) as exc_info:
            _deserialize_item(json.dumps(data).encode("utf-8"))
        assert str(exc_info.value) == f"Store entry has malformed field: {raw_exc.value}"

    def test_closed_message_byte_exact_at_every_guard_site(self, store_path: Path) -> None:
        """All 7 ``_closed`` guard sites render the same byte-exact message.

        Exact equality also pins ``self._path`` — the ``_path = None``
        constructor mutant renders ``Store None is closed`` at every site.
        """
        s = WorkItemStore.open(store_path)
        s.close()
        guarded_ops: list[Callable[[], object]] = [
            lambda: s.put(_make_item()),
            lambda: s.put_many([_make_item()]),
            lambda: s.get(IngestPhase.A_INSTRUMENTS, _make_hash("x")),
            lambda: s.maybe_seen_terminal(IngestPhase.A_INSTRUMENTS, _make_hash("x")),
            lambda: list(s.iter_phase(IngestPhase.A_INSTRUMENTS)),
            lambda: s.count_phase(IngestPhase.A_INSTRUMENTS),
            lambda: s.delete_phase(IngestPhase.A_INSTRUMENTS),
        ]
        for op in guarded_ops:
            with pytest.raises(StoreError) as exc_info:
                op()
            assert str(exc_info.value) == f"Store {store_path} is closed"


class TestSerializationEncodingPin:
    """``ensure_ascii=False`` is load-bearing (DEC-MUTATION-RUNTIME-001)."""

    def test_serialize_emits_raw_utf8_not_ascii_escapes(self) -> None:
        """Non-ASCII params serialise as raw UTF-8 bytes, never ``\\uXXXX``.

        The ``ensure_ascii=True`` mutant escapes ``₹`` to ``\\u20b9`` —
        same JSON value but different stored bytes, which breaks the
        byte-for-byte idempotence oracle (Golden Rule #7).
        """
        item = WorkItem.new(
            phase=IngestPhase.A_INSTRUMENTS,
            content_hash=_make_hash("inr"),
            params={"symbol": "₹NIFTY"},
        )
        raw = _serialize_item(item)
        assert "₹NIFTY".encode() in raw
        assert b"\\u20b9" not in raw


class TestOpenGeometryPins:
    """LMDB environment geometry pinned exact (DEC-MUTATION-RUNTIME-001)."""

    def test_map_size_mb_is_exact_mib_multiplier(self, store_path: Path) -> None:
        # 64 MiB exactly — either ``× 1025`` mutant lands at 67_174_400.
        with WorkItemStore.open(store_path, map_size_mb=64) as s:
            assert s._env.info()["map_size"] == 67_108_864  # type: ignore[reportPrivateUsage]

    def test_open_creates_reader_lock_table(self, store_path: Path) -> None:
        # ``lock=True`` materialises lock.mdb on disk; the ``lock=False``
        # mutant omits it and read-only processes lose writer coordination.
        with WorkItemStore.open(store_path):
            assert (store_path / "lock.mdb").exists()

    def test_read_only_never_creates_missing_subdbs(self, store_path: Path) -> None:
        """Read-only open of an env without the phase sub-dbs is NotFound.

        ``create=False`` surfaces ``lmdb.NotFoundError`` (operator opened
        a never-written store); the ``create=True`` mutant instead trips
        LMDB's EACCES → ``lmdb.ReadonlyError``, masking the real cause.
        """
        import lmdb

        env = lmdb.open(str(store_path), max_dbs=MAX_DBS, subdir=True)
        env.close()
        with pytest.raises(lmdb.NotFoundError):
            WorkItemStore.open(store_path, read_only=True)

    def test_closed_flag_is_exactly_false_then_true(self, store_path: Path) -> None:
        # Pins ``self._closed = False`` (the ``None`` mutant is truthiness-
        # equivalent everywhere, so only an identity pin can kill it).
        s = WorkItemStore.open(store_path)
        try:
            assert s._closed is False  # type: ignore[reportPrivateUsage]
        finally:
            s.close()
        assert s._closed is True  # type: ignore[reportPrivateUsage]


class TestFlushForcePin:
    """``flush`` forwards ``force`` with default exactly ``False``."""

    def test_flush_force_default_exactly_false_and_forwards(
        self, store_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Record the value ``env.sync`` receives per flush call.

        The ``force: bool = True`` default mutant records ``[True, True]``
        — silently upgrading every routine flush to a blocking full
        fsync (DEC-MUTATION-RUNTIME-001).
        """
        calls: list[bool] = []
        with WorkItemStore.open(store_path) as s:
            real_env = s._env  # type: ignore[reportPrivateUsage]

            class _RecordingEnv:
                def sync(self, force: bool) -> None:
                    calls.append(force)
                    real_env.sync(force)

            monkeypatch.setattr(s, "_env", _RecordingEnv())
            s.flush()
            s.flush(force=True)
            monkeypatch.setattr(s, "_env", real_env)
        assert calls == [False, True]


class TestBloomPreflightPins:
    """Byte-exact pins on the put_many pre-flight + cold-start rebuild."""

    def test_put_many_exactly_at_capacity_succeeds(self, store_path: Path) -> None:
        """A batch landing EXACTLY at capacity must commit.

        Pins the strict ``projected > capacity`` boundary — the ``>=``
        mutant rejects a legal full-to-the-brim batch
        (DEC-MUTATION-RUNTIME-001).
        """
        from brutex.orchestrator.bloom import BloomFilter

        bloom = BloomFilter(capacity=3, fpr_target=1e-3)
        with WorkItemStore.open(store_path, bloom=bloom) as s:
            s.put_many([_make_item(hash_seed=f"edge-{i}") for i in range(3)])
            assert bloom.insertions_attempted == 3
            assert s.count_phase(IngestPhase.A_INSTRUMENTS) == 3

    def test_put_many_capacity_message_byte_exact(self, store_path: Path) -> None:
        from brutex.orchestrator.bloom import BloomCapacityExceeded, BloomFilter

        bloom = BloomFilter(capacity=3, fpr_target=1e-3)
        with WorkItemStore.open(store_path, bloom=bloom) as s:
            with pytest.raises(BloomCapacityExceeded) as exc_info:
                s.put_many([_make_item(hash_seed=f"cap-{i}") for i in range(5)])
            assert str(exc_info.value) == (
                "put_many would push Bloom past capacity: current=0 + batch=5 > capacity=3"
            )

    def test_cold_start_rebuild_logs_exact_event_and_count(self, store_path: Path) -> None:
        """The rebuild emits ONE ``bloom_rebuild_complete`` with the true count.

        Pins the event name + the ``items_rebuilt`` accumulator (init 0,
        step +1) — the observability contract the audit oracle replays
        (DEC-MUTATION-RUNTIME-001).
        """
        from brutex.orchestrator.bloom import BloomFilter
        from structlog.testing import capture_logs

        with WorkItemStore.open(store_path) as s:
            for i in range(3):
                s.put(_make_item(hash_seed=f"log-{i}"))
        bloom = BloomFilter(capacity=1000, fpr_target=1e-6)
        with capture_logs() as logs, WorkItemStore.open(store_path, bloom=bloom):
            pass
        events = [e for e in logs if e["event"] == "bloom_rebuild_complete"]
        assert len(events) == 1
        assert events[0]["items_rebuilt"] == 3
        assert events[0]["store_path"] == str(store_path)
        assert events[0]["bloom_capacity"] == 1000
        assert events[0]["bloom_fpr_target"] == 1e-6
