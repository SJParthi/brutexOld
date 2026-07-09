"""Unit + property tests for the resumable sweep checkpoint (`brutex.engine.sweep_checkpoint`).

100% line + branch coverage of the crash-safe resume marker
(DEC-SWEEP-REALTIME-GUARANTEE-001, CLAUDE.md §2 Rule 7):

* :class:`SweepCheckpoint` every ``__post_init__`` validation branch + derived
  ``next_index`` / ``is_complete`` / ``completed_count`` + ``to_dict`` /
  ``from_dict`` (missing field, bad version, malformed field, valid);
* :class:`SweepCheckpointStore` constructor guards, ``load`` (no file, stat /
  read OSError, oversized file refused pre-read, malformed JSON incl. a
  RecursionError nesting bomb, non-object payload, sweep-id mismatch,
  same-id total_combos mismatch refused, matching resume), ``record``
  (negative, out-of-range, stale no-op, advance), ``save`` (success, OSError
  wrap incl. the parent-mkdir failure);
* the atomic-write OSError cleanup path;
* a hypothesis property that a monotone record sequence persists the running
  maximum and survives a reopen, and a real crash-resume round-trip.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from brutex.engine import sweep_checkpoint as ckpt
from brutex.engine.sweep_checkpoint import (
    CHECKPOINT_VERSION,
    SweepCheckpoint,
    SweepCheckpointError,
    SweepCheckpointStore,
)
from hypothesis import given, settings
from hypothesis import strategies as st


def _store(tmp_path: Path, **over: Any) -> SweepCheckpointStore:
    base: dict[str, Any] = {
        "path": tmp_path / "ckpt.json",
        "sweep_id": "sweep-1",
        "total_combos": 10,
        "clock": lambda: 1234.0,
    }
    base.update(over)
    return SweepCheckpointStore(**base)


# ── SweepCheckpoint dataclass ───────────────────────────────────────────────


def test_checkpoint_valid_and_derived() -> None:
    cp = SweepCheckpoint(sweep_id="s", total_combos=10, last_completed_index=3, updated_at=1.0)
    assert cp.next_index == 4
    assert cp.completed_count == 4
    assert not cp.is_complete


def test_checkpoint_is_complete() -> None:
    cp = SweepCheckpoint(sweep_id="s", total_combos=5, last_completed_index=4, updated_at=1.0)
    assert cp.is_complete
    assert cp.next_index == 5


def test_checkpoint_fresh_index_minus_one() -> None:
    cp = SweepCheckpoint(sweep_id="s", total_combos=5, last_completed_index=-1, updated_at=1.0)
    assert cp.next_index == 0
    assert cp.completed_count == 0
    assert not cp.is_complete


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"sweep_id": "", "total_combos": 5, "last_completed_index": 0}, "sweep_id"),
        ({"sweep_id": "s", "total_combos": -1, "last_completed_index": -1}, "total_combos"),
        ({"sweep_id": "s", "total_combos": 5, "last_completed_index": -2}, "last_completed_index"),
        ({"sweep_id": "s", "total_combos": 5, "last_completed_index": 5}, "must be"),
    ],
)
def test_checkpoint_validation_raises(kwargs: dict[str, Any], match: str) -> None:
    with pytest.raises(SweepCheckpointError, match=match):
        SweepCheckpoint(updated_at=1.0, **kwargs)


def test_to_from_dict_round_trip() -> None:
    cp = SweepCheckpoint(sweep_id="s", total_combos=10, last_completed_index=3, updated_at=2.5)
    d = cp.to_dict()
    assert d["version"] == CHECKPOINT_VERSION
    assert SweepCheckpoint.from_dict(d) == cp


def test_from_dict_missing_field_raises() -> None:
    with pytest.raises(SweepCheckpointError, match="missing required fields"):
        SweepCheckpoint.from_dict({"version": CHECKPOINT_VERSION})


def test_from_dict_unknown_version_raises() -> None:
    d = {
        "version": 999,
        "sweep_id": "s",
        "total_combos": 10,
        "last_completed_index": 0,
        "updated_at": 1.0,
    }
    with pytest.raises(SweepCheckpointError, match="Unknown checkpoint schema version"):
        SweepCheckpoint.from_dict(d)


def test_from_dict_malformed_field_raises() -> None:
    d = {
        "version": CHECKPOINT_VERSION,
        "sweep_id": "s",
        "total_combos": "not-an-int",
        "last_completed_index": 0,
        "updated_at": 1.0,
    }
    with pytest.raises(SweepCheckpointError, match="Malformed checkpoint field"):
        SweepCheckpoint.from_dict(d)


@pytest.mark.parametrize("field", ["total_combos", "last_completed_index"])
@pytest.mark.parametrize("value", [float("inf"), float("-inf")], ids=["inf", "-inf"])
def test_from_dict_infinite_int_field_raises_domain_error(field: str, value: float) -> None:
    """An infinite float in an int field maps to SweepCheckpointError.

    Refuter-confirmed (fix round 6): stdlib ``json.loads`` parses ``1e999``
    and the bare ``Infinity``/``-Infinity`` literals to ``float('inf')``, and
    ``int(float('inf'))`` raises ``OverflowError`` — which subclasses
    ``ArithmeticError``, NOT ``ValueError``/``TypeError`` — so it escaped the
    coercion catch RAW, past every consumer's narrow
    ``except SweepCheckpointError`` (the greeks-backfill runner's documented
    graceful fresh start, the sweep's own resume path), wedging every wet run
    behind one corrupt/planted checkpoint file until manual deletion —
    falsifying the load contract's "every corruption class maps to
    SweepCheckpointError". Inconsistently, ``NaN`` in the same field mapped
    gracefully (``int(nan)`` raises ``ValueError``).
    """
    d = {
        "version": CHECKPOINT_VERSION,
        "sweep_id": "s",
        "total_combos": 10,
        "last_completed_index": 0,
        "updated_at": 1.0,
        field: value,
    }
    with pytest.raises(SweepCheckpointError, match="Malformed checkpoint field"):
        SweepCheckpoint.from_dict(d)


@pytest.mark.parametrize("payload", [[], None, "hello", 42, True])
def test_from_dict_non_object_payload_raises(payload: object) -> None:
    """A valid-JSON NON-OBJECT payload is the caller-catchable domain error.

    Refuter-confirmed defect: ``required - set(data.keys())`` raised
    ``AttributeError`` on a list/None/str/int/bool payload, which escaped every
    consumer's ``except SweepCheckpointError`` (the greeks-backfill runner's
    documented graceful fresh start, the sweep-daemon's checkpoint view) and
    wedged the run behind one hand-edited/planted checkpoint file.
    """
    with pytest.raises(SweepCheckpointError, match="must be a JSON object"):
        SweepCheckpoint.from_dict(payload)


# ── SweepCheckpointStore construction ───────────────────────────────────────


def test_store_rejects_empty_sweep_id(tmp_path: Path) -> None:
    with pytest.raises(SweepCheckpointError, match="sweep_id"):
        _store(tmp_path, sweep_id="")


def test_store_rejects_negative_total(tmp_path: Path) -> None:
    with pytest.raises(SweepCheckpointError, match="total_combos"):
        _store(tmp_path, total_combos=-1)


def test_store_initial_state(tmp_path: Path) -> None:
    s = _store(tmp_path)
    assert s.path == tmp_path / "ckpt.json"
    assert s.last_completed_index == -1
    assert s.next_index == 0


# ── load ────────────────────────────────────────────────────────────────────


def test_load_no_file_returns_none(tmp_path: Path) -> None:
    assert _store(tmp_path).load() is None


def test_load_read_oserror_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s = _store(tmp_path)
    s.record(0)  # create the file

    def _boom(*_a: Any, **_k: Any) -> str:
        raise OSError("eio")

    monkeypatch.setattr(Path, "read_text", _boom)
    with pytest.raises(SweepCheckpointError, match="Failed to read checkpoint"):
        s.load()


def test_load_malformed_json_raises(tmp_path: Path) -> None:
    p = tmp_path / "ckpt.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(SweepCheckpointError, match="Malformed checkpoint JSON"):
        _store(tmp_path).load()


def test_load_invalid_utf8_raises_domain_error_not_unicodedecodeerror(tmp_path: Path) -> None:
    """RES-4: a torn checkpoint (kill -9 mid-write) or bitrot leaving invalid
    UTF-8 bytes makes ``read_text`` raise ``UnicodeDecodeError`` (a
    ``ValueError``). That must map to the typed ``SweepCheckpointError`` — like
    the malformed-JSON path — never escape RAW past a consumer's narrow
    ``except SweepCheckpointError`` and fire a false abort alert."""
    p = tmp_path / "ckpt.json"
    # A byte the utf-8 decoder rejects mid-stream (0xFF is never valid UTF-8).
    p.write_bytes(b'{"version": 1, "sweep_id": "sweep-1"\xff}')
    with pytest.raises(SweepCheckpointError, match="Undecodable checkpoint bytes"):
        _store(tmp_path).load()


def test_load_torn_leading_invalid_byte_raises_domain_error(tmp_path: Path) -> None:
    """RES-4 (first-byte torn variant): a checkpoint truncated to raw invalid
    bytes (a UTF-16 BOM prefix / arbitrary binary) is also the typed error, not
    a raw ``UnicodeDecodeError``."""
    p = tmp_path / "ckpt.json"
    p.write_bytes(b"\xff\xfe\x00 not utf8 {")
    with pytest.raises(SweepCheckpointError, match="Undecodable checkpoint bytes"):
        _store(tmp_path).load()


@pytest.mark.parametrize("payload", ["[]", "null", '"hello"', "42", "true"])
def test_load_non_object_json_raises_domain_error(tmp_path: Path, payload: str) -> None:
    """load() on valid-JSON-non-object corruption raises SweepCheckpointError, never
    the AttributeError that escaped every caller's narrow catch (refuter repro)."""
    p = tmp_path / "ckpt.json"
    p.write_text(payload, encoding="utf-8")
    with pytest.raises(SweepCheckpointError, match="must be a JSON object"):
        _store(tmp_path).load()


@pytest.mark.parametrize("literal", ["1e999", "Infinity", "-Infinity"])
def test_load_infinite_total_raises_domain_error_not_overflow(tmp_path: Path, literal: str) -> None:
    """load() on an Infinity/1e999 int field is the caller-catchable domain error.

    Refuter repro (fix round 6): ``json.loads`` accepts all three literals as
    ``float('inf')`` and ``int(inf)`` raised a RAW ``OverflowError`` out of
    ``from_dict``'s ``(TypeError, ValueError)`` catch — escaping
    ``run_greeks_backfill``'s ``except SweepCheckpointError`` (the documented
    graceful fresh start) and crashing every wet run until the operator
    deleted ``<lake>/.greeks_backfill/checkpoint.json`` (the file is unlinked
    only on clean completion, so the wedge was persistent).
    """
    p = tmp_path / "ckpt.json"
    p.write_text(
        '{"version": 1, "sweep_id": "sweep-1", "total_combos": ' + literal + ", "
        '"last_completed_index": 0, "updated_at": 1.0}',
        encoding="utf-8",
    )
    with pytest.raises(SweepCheckpointError, match="Malformed checkpoint field"):
        _store(tmp_path).load()


def test_load_different_sweep_id_ignored(tmp_path: Path) -> None:
    _store(tmp_path, sweep_id="other").record(4)
    # A store for a different sweep must not adopt the foreign checkpoint.
    s = _store(tmp_path, sweep_id="sweep-1")
    assert s.load() is None
    assert s.last_completed_index == -1


def test_load_matching_resumes(tmp_path: Path) -> None:
    _store(tmp_path).record(6)
    s2 = _store(tmp_path)
    cp = s2.load()
    assert cp is not None
    assert cp.last_completed_index == 6
    assert s2.next_index == 7


def test_load_deeply_nested_json_raises_domain_error(tmp_path: Path) -> None:
    """A recursion-bomb payload (200k nested arrays) maps to SweepCheckpointError.

    Refuter repro (fix round 2): ``json.loads`` raises ``RecursionError`` —
    neither ``JSONDecodeError`` nor ``SweepCheckpointError`` — so it escaped
    every consumer's narrow catch and crashed EVERY wet run until the operator
    deleted the file.
    """
    depth = 200_000
    p = tmp_path / "ckpt.json"
    p.write_text("[" * depth + "]" * depth, encoding="utf-8")
    with pytest.raises(SweepCheckpointError, match="Malformed checkpoint JSON"):
        _store(tmp_path).load()


def test_load_oversized_file_refused_before_read(tmp_path: Path) -> None:
    """A file beyond MAX_CHECKPOINT_BYTES is refused BEFORE the read (no MemoryError)."""
    p = tmp_path / "ckpt.json"
    p.write_text(" " * (ckpt.MAX_CHECKPOINT_BYTES + 1), encoding="utf-8")
    with pytest.raises(SweepCheckpointError, match="sanity cap"):
        _store(tmp_path).load()


def test_load_stat_oserror_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A stat failure on an existing file maps to the domain error (never raw OSError)."""
    s = _store(tmp_path)
    s.record(0)  # create the file
    real_stat = Path.stat

    def _boom(self: Path, *args: Any, **kwargs: Any) -> Any:
        if kwargs:
            # exists()'s internal ``stat(follow_symlinks=True)`` probe passes
            # through — only the size-cap stat (positional, no kwargs) booms.
            return real_stat(self, *args, **kwargs)
        raise OSError("eio")

    monkeypatch.setattr(Path, "stat", _boom)
    with pytest.raises(SweepCheckpointError, match="Failed to stat checkpoint"):
        s.load()


def test_load_same_id_total_combos_mismatch_raises(tmp_path: Path) -> None:
    """A SAME-sweep_id checkpoint with a foreign total is refused, never adopted.

    Refuter repro (fix round 2): a tampered same-id checkpoint with an
    inflated ``total_combos``/``last_completed_index`` (internally consistent,
    so ``from_dict`` passes) used to adopt ``next_index`` past the store's
    real total — the run silently zero-batched "complete" and DELETED the
    checkpoint.
    """
    _store(tmp_path, total_combos=10_000).record(9_999)  # the tampered/inflated file
    s = _store(tmp_path, total_combos=10)
    with pytest.raises(SweepCheckpointError, match="corrupt/tampered"):
        s.load()
    assert s.next_index == 0  # nothing adopted — a fresh start remains correct


# ── record ──────────────────────────────────────────────────────────────────


def test_record_negative_raises(tmp_path: Path) -> None:
    with pytest.raises(SweepCheckpointError, match="must be >= 0"):
        _store(tmp_path).record(-1)


def test_record_out_of_range_raises(tmp_path: Path) -> None:
    with pytest.raises(SweepCheckpointError, match="< total_combos"):
        _store(tmp_path, total_combos=3).record(3)


def test_record_advances_and_is_monotonic(tmp_path: Path) -> None:
    s = _store(tmp_path)
    assert s.record(0) is True
    assert s.record(2) is True
    assert s.record(1) is False  # stale — ignored, no rewind
    assert s.record(2) is False  # duplicate — no-op
    assert s.last_completed_index == 2


def test_discard_adoption_unadopts_in_memory_only(tmp_path: Path) -> None:
    """``discard_adoption`` resets to "nothing completed" WITHOUT touching disk.

    DEC-GREEKS-BULK-REENRICH-001 fix round 10: the backfill runner un-adopts a
    loaded checkpoint when an out-of-band overlay-write freshness signal says
    the on-disk resume point is stale; the caller owns the file's fate, and
    the monotonic advance restarts from ``-1`` so the first advancing record
    atomically overwrites whatever is on disk.
    """
    _store(tmp_path).record(4)
    s = _store(tmp_path)
    assert s.load() is not None
    assert s.next_index == 5
    on_disk = (tmp_path / "ckpt.json").read_bytes()
    s.discard_adoption()
    assert s.next_index == 0
    assert s.last_completed_index == -1
    assert (tmp_path / "ckpt.json").read_bytes() == on_disk  # in-memory only
    # The monotonic advance restarts: index 0 records again (it would have
    # been a stale no-op against the adopted mark) and overwrites the file.
    assert s.record(0) is True
    reread = _store(tmp_path)
    loaded = reread.load()
    assert loaded is not None
    assert loaded.last_completed_index == 0


# ── save ────────────────────────────────────────────────────────────────────


def test_save_oserror_wrapped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s = _store(tmp_path)

    def _boom(*_a: Any, **_k: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(ckpt, "_atomic_write", _boom)
    with pytest.raises(SweepCheckpointError, match="Failed to write checkpoint"):
        s.record(0)


def test_save_explicit_checkpoint(tmp_path: Path) -> None:
    s = _store(tmp_path)
    cp = SweepCheckpoint(
        sweep_id="sweep-1", total_combos=10, last_completed_index=5, updated_at=9.0
    )
    s.save(cp)
    assert SweepCheckpoint.from_dict(__import__("json").loads(s.path.read_text())) == cp


def test_save_mkdir_failure_wrapped_as_domain_error(tmp_path: Path) -> None:
    """A stray non-directory file at the checkpoint dir maps to SweepCheckpointError.

    Refuter repro (fix round 2): ``save()`` ran the parent ``mkdir`` OUTSIDE
    its OSError → SweepCheckpointError mapping, so a stray FILE at the sidecar
    dir raised a raw ``FileExistsError`` past every consumer's documented
    advisory ``except SweepCheckpointError`` and aborted the whole run.
    """
    stray = tmp_path / "sidecar"
    stray.write_text("not a directory", encoding="utf-8")
    s = _store(tmp_path, path=stray / "ckpt.json")
    with pytest.raises(SweepCheckpointError, match="Failed to write checkpoint"):
        s.record(0)


# ── atomic-write cleanup ────────────────────────────────────────────────────


def test_atomic_write_cleanup_on_replace_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dest = tmp_path / "x.json"

    def _boom(*_a: Any, **_k: Any) -> None:
        raise OSError("replace failed")

    # The atomic write is now the shared atomic_io core (DEC-ATOMIC-WRITE-CONSOLIDATE-001),
    # so os.replace + the temp prefix live there.
    monkeypatch.setattr("brutex.data.atomic_io.os.replace", _boom)
    with pytest.raises(OSError, match="replace failed"):
        ckpt._atomic_write(dest, b"data")
    # No temp files left behind in the directory.
    assert list(tmp_path.glob(".brutex-write-*")) == []
    assert not dest.exists()


# ── property + real resume round-trip ───────────────────────────────────────


# ``deadline=None``: each example writes the checkpoint JSON to disk up to 40
# times, so the per-example wall-clock cost is dominated by real file I/O and
# varies with machine load — the default 200 ms Hypothesis deadline made this
# test intermittently flake with ``DeadlineExceeded`` (a timing artefact, NOT a
# logic failure). Disabling the deadline removes the flake without weakening what
# the property asserts.
@settings(deadline=None)
@given(indices=st.lists(st.integers(min_value=0, max_value=49), max_size=40))
def test_property_record_tracks_running_max(tmp_path_factory: Any, indices: list[int]) -> None:
    d = tmp_path_factory.mktemp("ck")
    s = SweepCheckpointStore(path=d / "c.json", sweep_id="p", total_combos=50, clock=lambda: 0.0)
    running = -1
    for i in indices:
        advanced = s.record(i)
        if i > running:
            assert advanced is True
            running = i
        else:
            assert advanced is False
    assert s.last_completed_index == running
    if running >= 0:
        reopened = SweepCheckpointStore(path=d / "c.json", sweep_id="p", total_combos=50)
        loaded = reopened.load()
        assert loaded is not None
        assert loaded.last_completed_index == running


def test_crash_resume_skips_completed(tmp_path: Path) -> None:
    # Simulate a sweep that completes combos 0..4 then is killed.
    s = SweepCheckpointStore(path=tmp_path / "c.json", sweep_id="run", total_combos=10)
    for i in range(5):
        s.record(i)
    # A fresh process reopens and resumes from where it left off.
    resumed = SweepCheckpointStore(path=tmp_path / "c.json", sweep_id="run", total_combos=10)
    cp = resumed.load()
    assert cp is not None
    assert resumed.next_index == 5
    assert not cp.is_complete
