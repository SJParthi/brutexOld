"""Unit tests for ``brutex.data.raw_integrity_store``.

DEC-RAW-DATA-INTEGRITY-001. 100% line+branch of the durable sidecar:
append → load round-trip (latest-wins per key), the missing / unreadable /
malformed-line degrade-to-empty paths, find_record, append_records, and the
best-effort capture hook (success + malformed-payload swallow + OSError swallow).
The candle lake is never touched (the sidecar lives in its own directory).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from brutex.data.raw_integrity import RawIntegrityRecord, build_record, hash_payload
from brutex.data.raw_integrity_store import (
    INTEGRITY_DIRNAME,
    append_record,
    append_records,
    find_record,
    integrity_dir,
    integrity_path,
    load_records,
    make_capture_hook,
    make_capture_hook_factory,
    shard_path,
)

_PAYLOAD = {"candles": [["2024-01-01T09:15:00", 100.0, 110.0, 90.0, 105.0, 777, 42]]}


def _record(
    symbol: str = "NSE-NIFTY", window_start: str = "2024-01-01 00:00:00"
) -> RawIntegrityRecord:
    return build_record(
        _PAYLOAD,
        exchange="NSE",
        segment="CASH",
        groww_symbol=symbol,
        interval="1minute",
        window_start=window_start,
        window_end="2024-01-31 00:00:00",
        now=datetime(2024, 6, 1, tzinfo=UTC),
    )


def test_paths_under_lake_root_not_in_bars(tmp_path: Path) -> None:
    assert integrity_dir(tmp_path) == tmp_path / INTEGRITY_DIRNAME
    assert integrity_path(tmp_path) == tmp_path / INTEGRITY_DIRNAME / "records.jsonl"
    # NEVER under bars/ or options/ (the candle lake is untouched).
    assert "bars" not in integrity_path(tmp_path).parts
    assert "options" not in integrity_path(tmp_path).parts


def test_append_then_load_round_trip(tmp_path: Path) -> None:
    record = _record()
    append_record(record, tmp_path)
    loaded = load_records(tmp_path)
    assert loaded == {record.key: record}


def test_load_missing_file_is_empty(tmp_path: Path) -> None:
    assert load_records(tmp_path) == {}


def test_latest_wins_for_same_key(tmp_path: Path) -> None:
    """A re-captured window's LAST record wins (latest-wins per key)."""
    first = _record()
    # same key, different captured_at → must overwrite
    second = RawIntegrityRecord(
        exchange=first.exchange,
        segment=first.segment,
        groww_symbol=first.groww_symbol,
        interval=first.interval,
        window_start=first.window_start,
        window_end=first.window_end,
        payload_hash="deadbeef",
        row_count=99,
        captured_at=datetime(2024, 7, 1, tzinfo=UTC),
    )
    append_record(first, tmp_path)
    append_record(second, tmp_path)
    loaded = load_records(tmp_path)
    assert loaded[first.key] == second


def test_append_records_batch_returns_count(tmp_path: Path) -> None:
    records = [_record("NSE-A"), _record("NSE-B")]
    assert append_records(records, tmp_path) == 2
    assert set(load_records(tmp_path).keys()) == {records[0].key, records[1].key}


def test_find_record_present_and_absent(tmp_path: Path) -> None:
    record = _record()
    append_record(record, tmp_path)
    found = find_record(
        tmp_path,
        exchange="NSE",
        segment="CASH",
        groww_symbol="NSE-NIFTY",
        interval="1minute",
        window_start="2024-01-01 00:00:00",
        window_end="2024-01-31 00:00:00",
    )
    assert found == record
    assert (
        find_record(
            tmp_path,
            exchange="NSE",
            segment="CASH",
            groww_symbol="MISSING",
            interval="1minute",
            window_start="2024-01-01 00:00:00",
            window_end="2024-01-31 00:00:00",
        )
        is None
    )


def test_load_skips_blank_and_malformed_lines(tmp_path: Path) -> None:
    record = _record()
    append_record(record, tmp_path)
    path = integrity_path(tmp_path)
    # Append a blank line, a non-JSON trailing line (crash sim), and a JSON object
    # missing a required key — all must be skipped, the good record survives.
    with path.open("a", encoding="utf-8") as handle:
        handle.write("\n")
        handle.write("{partial-truncated\n")
        handle.write('{"exchange":"NSE"}\n')  # missing keys → _record_from_obj None
        handle.write("[1,2,3]\n")  # valid JSON but not a dict
    loaded = load_records(tmp_path)
    assert loaded == {record.key: record}


def test_load_unparseable_captured_at_is_none(tmp_path: Path) -> None:
    path = integrity_dir(tmp_path)
    path.mkdir(parents=True, exist_ok=True)
    integrity_path(tmp_path).write_text(
        '{"exchange":"NSE","segment":"CASH","groww_symbol":"NSE-NIFTY",'
        '"interval":"1minute","window_start":"a","window_end":"b",'
        '"payload_hash":"h","row_count":1,"algorithm":"blake2b",'
        '"captured_at":"not-a-timestamp"}\n',
        encoding="utf-8",
    )
    loaded = load_records(tmp_path)
    (record,) = loaded.values()
    assert record.captured_at is None
    assert record.payload_hash == "h"


def test_load_non_string_captured_at_is_none(tmp_path: Path) -> None:
    path = integrity_dir(tmp_path)
    path.mkdir(parents=True, exist_ok=True)
    integrity_path(tmp_path).write_text(
        '{"exchange":"NSE","segment":"CASH","groww_symbol":"NSE-NIFTY",'
        '"interval":"1minute","window_start":"a","window_end":"b",'
        '"payload_hash":"h","row_count":1,"algorithm":"blake2b",'
        '"captured_at":12345}\n',
        encoding="utf-8",
    )
    (record,) = load_records(tmp_path).values()
    assert record.captured_at is None


# --------------------------------------------------------------------------- #
# issued_symbol — the `%26` fallback verify correction (back-compat both ways)
# --------------------------------------------------------------------------- #
def test_issued_symbol_round_trips_through_sidecar(tmp_path: Path) -> None:
    # DEC-GROWW-AMPERSAND-CANDLE-FALLBACK-001 Fix-1: an encoded-form capture
    # survives the append → load round trip with its issued form intact.
    record = build_record(
        _PAYLOAD,
        exchange="NSE",
        segment="CASH",
        groww_symbol="NSE-M&M",
        interval="1minute",
        window_start="2024-01-01 00:00:00",
        window_end="2024-01-31 00:00:00",
        now=datetime(2024, 6, 1, tzinfo=UTC),
        issued_symbol="NSE-M%26M",
    )
    append_record(record, tmp_path)
    (loaded,) = load_records(tmp_path).values()
    assert loaded == record
    assert loaded.issued_symbol == "NSE-M%26M"
    assert loaded.fetch_symbol == "NSE-M%26M"


def test_plain_record_json_line_has_no_issued_symbol_key(tmp_path: Path) -> None:
    # BACK-COMPAT (byte-identical plain path): a plain-form record's sidecar line
    # carries NO ``issued_symbol`` key — the exact pre-field serialisation format.
    import json

    append_record(_record(), tmp_path)
    line = shard_path(tmp_path, "NSE-NIFTY").read_text(encoding="utf-8").strip()
    assert "issued_symbol" not in line
    assert set(json.loads(line)) == {
        "exchange",
        "segment",
        "groww_symbol",
        "interval",
        "window_start",
        "window_end",
        "payload_hash",
        "row_count",
        "algorithm",
        "captured_at",
    }


def test_legacy_line_without_issued_symbol_loads_as_none(tmp_path: Path) -> None:
    # BACK-COMPAT (mandatory): a LEGACY sidecar line written before the field
    # existed loads with ``issued_symbol=None`` → ``fetch_symbol`` is the key
    # symbol, so verify re-issues exactly the call it always did.
    path = integrity_dir(tmp_path)
    path.mkdir(parents=True, exist_ok=True)
    integrity_path(tmp_path).write_text(
        '{"exchange":"NSE","segment":"CASH","groww_symbol":"NSE-NIFTY",'
        '"interval":"1minute","window_start":"a","window_end":"b",'
        '"payload_hash":"h","row_count":1,"algorithm":"blake2b",'
        '"captured_at":null}\n',
        encoding="utf-8",
    )
    (record,) = load_records(tmp_path).values()
    assert record.issued_symbol is None
    assert record.fetch_symbol == "NSE-NIFTY"


def test_non_string_issued_symbol_loads_as_none(tmp_path: Path) -> None:
    # A malformed (non-string) issued_symbol is provenance-adjacent — never fatal.
    path = integrity_dir(tmp_path)
    path.mkdir(parents=True, exist_ok=True)
    integrity_path(tmp_path).write_text(
        '{"exchange":"NSE","segment":"CASH","groww_symbol":"NSE-NIFTY",'
        '"interval":"1minute","window_start":"a","window_end":"b",'
        '"payload_hash":"h","row_count":1,"algorithm":"blake2b",'
        '"captured_at":null,"issued_symbol":123}\n',
        encoding="utf-8",
    )
    (record,) = load_records(tmp_path).values()
    assert record.issued_symbol is None
    assert record.fetch_symbol == "NSE-NIFTY"


def test_load_unreadable_file_degrades_to_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = _record()
    append_record(record, tmp_path)

    def _boom(*_a: object, **_k: object) -> str:
        raise OSError("disk gone")

    # DEC-LAKE-READ-INODE-GUARD-001: the shard read rides the module-level
    # guarded seam, not Path.read_text — fault it there via the string-target
    # monkeypatch (the #1810 CodeQL convention).
    monkeypatch.setattr("brutex.data.raw_integrity_store.read_regular_text", _boom)
    assert load_records(tmp_path) == {}


# --------------------------------------------------------------------------- #
# make_capture_hook — best-effort, never raises
# --------------------------------------------------------------------------- #
def test_capture_hook_persists_record(tmp_path: Path) -> None:
    hook = make_capture_hook(tmp_path, exchange="NSE", segment="CASH", groww_symbol="NSE-NIFTY")
    hook(_PAYLOAD, "1minute", "2024-01-01 00:00:00", "2024-01-31 00:00:00", "NSE-NIFTY")
    loaded = load_records(tmp_path)
    (record,) = loaded.values()
    assert record.payload_hash == hash_payload(_PAYLOAD)
    assert record.groww_symbol == "NSE-NIFTY"
    assert record.interval == "1minute"
    # A plain-form fetch (issued == key symbol) is NORMALISED to None — the record
    # (and its sidecar line) stays byte-identical to the pre-issued_symbol format.
    assert record.issued_symbol is None
    assert record.fetch_symbol == "NSE-NIFTY"


def test_capture_hook_records_divergent_issued_symbol(tmp_path: Path) -> None:
    # DEC-GROWW-AMPERSAND-CANDLE-FALLBACK-001 Fix-1: a `%26` fallback recovery is
    # captured under the PLAIN key symbol but was PHYSICALLY fetched with the
    # encoded form — the surviving record must carry that issued form so the
    # verify path can re-issue the byte-identical call.
    hook = make_capture_hook(tmp_path, exchange="NSE", segment="CASH", groww_symbol="NSE-M&M")
    hook(_PAYLOAD, "1minute", "2024-01-01 00:00:00", "2024-01-31 00:00:00", "NSE-M%26M")
    (record,) = load_records(tmp_path).values()
    assert record.groww_symbol == "NSE-M&M"  # keyed under the PLAIN symbol
    assert record.issued_symbol == "NSE-M%26M"  # the form actually sent
    assert record.fetch_symbol == "NSE-M%26M"  # verify re-issues THIS form


def test_capture_hook_swallows_malformed_payload(tmp_path: Path) -> None:
    hook = make_capture_hook(tmp_path, exchange="NSE", segment="CASH", groww_symbol="X")
    # An un-hashable payload must NEVER raise out of the hook (best-effort).
    hook({"candles": object()}, "1minute", "a", "b", "X")
    assert load_records(tmp_path) == {}  # nothing persisted, no crash


def test_capture_hook_swallows_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hook = make_capture_hook(tmp_path, exchange="NSE", segment="CASH", groww_symbol="X")

    def _boom(*_a: object, **_k: object) -> None:
        raise OSError("no space left")

    monkeypatch.setattr("brutex.data.raw_integrity_store.append_record", _boom)
    # A sidecar write failure must NEVER raise out of the hook (best-effort).
    hook(_PAYLOAD, "1minute", "a", "b", "X")


# --------------------------------------------------------------------------- #
# make_capture_hook_factory — per-symbol attribution (the live-pull seam)
# --------------------------------------------------------------------------- #
def test_capture_hook_factory_attributes_each_symbol(tmp_path: Path) -> None:
    # A single factory (bound to one lake root) builds a CORRECTLY per-symbol hook —
    # the property the live build sites rely on (one adapter, many symbols).
    factory = make_capture_hook_factory(tmp_path)
    factory("NSE", "CASH", "NSE-NIFTY")(
        _PAYLOAD, "1minute", "2024-01-01 00:00:00", "e1", "NSE-NIFTY"
    )
    factory("BSE", "CASH", "BSE-SENSEX")(
        _PAYLOAD, "1minute", "2024-01-02 00:00:00", "e2", "BSE-SENSEX"
    )
    loaded = load_records(tmp_path)
    symbols = {record.groww_symbol for record in loaded.values()}
    assert symbols == {"NSE-NIFTY", "BSE-SENSEX"}  # each window kept its own symbol
    nifty = find_record(
        tmp_path,
        exchange="NSE",
        segment="CASH",
        groww_symbol="NSE-NIFTY",
        interval="1minute",
        window_start="2024-01-01 00:00:00",
        window_end="e1",
    )
    assert nifty is not None
    assert nifty.payload_hash == hash_payload(_PAYLOAD)


# --------------------------------------------------------------------------- #
# append_record — cross-process flock (DEC-LAKE-PARTITION-FLOCK-001 mirror)
# --------------------------------------------------------------------------- #
def test_append_takes_cross_process_flock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import fcntl

    locked: list[int] = []

    def _spy_flock(fd: int, operation: int) -> None:
        # The bounded acquire takes a NON-BLOCKING exclusive lock (LOCK_EX | LOCK_NB)
        # so a wedged peer can never pin the worker (DEC-PULL-CONTENTION-ANTISTALL-001).
        assert operation == fcntl.LOCK_EX | fcntl.LOCK_NB
        locked.append(operation)

    monkeypatch.setattr("brutex.data.raw_integrity_store.fcntl.flock", _spy_flock)
    append_record(_record(), tmp_path)
    assert locked == [fcntl.LOCK_EX | fcntl.LOCK_NB]  # acquired on the first try
    # The record still landed despite the spied lock — append is unaffected.
    (record,) = load_records(tmp_path).values()
    assert record.groww_symbol == "NSE-NIFTY"


def test_append_concurrent_threads_no_interleave(tmp_path: Path) -> None:
    # The real flock + the in-process lock together keep every appended line intact
    # (each JSON line independently parseable) under concurrent appends.
    import threading

    barrier = threading.Barrier(8)

    def _worker(n: int) -> None:
        barrier.wait()
        append_record(_record(symbol=f"NSE-SYM{n}", window_start=f"w{n}"), tmp_path)

    threads = [threading.Thread(target=_worker, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    loaded = load_records(tmp_path)
    assert len(loaded) == 8  # every line landed, none corrupted/interleaved


# --------------------------------------------------------------------------- #
# Per-symbol sharding (DEC-PULL-CONTENTION-ANTISTALL-001 GAP #14)
# --------------------------------------------------------------------------- #
def test_shard_path_is_per_symbol_and_deterministic(tmp_path: Path) -> None:
    a = shard_path(tmp_path, "NSE-NIFTY")
    b = shard_path(tmp_path, "BSE-SENSEX")
    assert a.parent == integrity_dir(tmp_path)
    assert a.suffix == ".jsonl"
    assert a != b  # different symbols → different shards (never contend)
    assert shard_path(tmp_path, "NSE-NIFTY") == a  # same symbol → same shard (stable)


def test_two_symbols_write_distinct_shards(tmp_path: Path) -> None:
    """Two symbols' captures land in DIFFERENT files, so they never contend on one lock."""
    append_record(_record("NSE-NIFTY"), tmp_path)
    append_record(_record("BSE-SENSEX"), tmp_path)
    nifty_shard = shard_path(tmp_path, "NSE-NIFTY")
    sensex_shard = shard_path(tmp_path, "BSE-SENSEX")
    assert nifty_shard.is_file()
    assert sensex_shard.is_file()
    assert nifty_shard != sensex_shard
    # Both still load through the shard glob.
    symbols = {r.groww_symbol for r in load_records(tmp_path).values()}
    assert symbols == {"NSE-NIFTY", "BSE-SENSEX"}


def test_load_reads_legacy_unsharded_file(tmp_path: Path) -> None:
    """A pre-sharding ``records.jsonl`` is still loaded (back-compat glob)."""
    import json

    legacy = _record("NSE-LEGACY")
    integrity_dir(tmp_path).mkdir(parents=True, exist_ok=True)
    legacy_line = json.dumps(
        {
            "exchange": legacy.exchange,
            "segment": legacy.segment,
            "groww_symbol": legacy.groww_symbol,
            "interval": legacy.interval,
            "window_start": legacy.window_start,
            "window_end": legacy.window_end,
            "payload_hash": legacy.payload_hash,
            "row_count": legacy.row_count,
            "algorithm": legacy.algorithm,
            "captured_at": None,
        }
    )
    with integrity_path(tmp_path).open("a", encoding="utf-8") as handle:
        handle.write(legacy_line + "\n")
    # A sharded record alongside the legacy file: BOTH load.
    append_record(_record("NSE-NEW"), tmp_path)
    symbols = {r.groww_symbol for r in load_records(tmp_path).values()}
    assert symbols == {"NSE-LEGACY", "NSE-NEW"}


def test_load_skips_one_unreadable_shard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """One unreadable shard logs a WARNING + is skipped; the others still load."""
    append_record(_record("NSE-GOOD"), tmp_path)
    append_record(_record("NSE-BAD"), tmp_path)
    bad_shard = shard_path(tmp_path, "NSE-BAD")
    # DEC-LAKE-READ-INODE-GUARD-001: fault the guarded seam the loader uses via
    # the string-target monkeypatch (the #1810 CodeQL convention). The delegate
    # is guarded_read's identity-pinned object — the SAME function
    # raw_integrity_store binds (`from brutex.data.guarded_read import ...`).
    from brutex.data.guarded_read import read_regular_text as real_read_text

    def _selective_boom(path: Path, *a: object, **k: object) -> str:
        if Path(path) == bad_shard:
            raise OSError("disk gone")
        return real_read_text(path, *a, **k)  # type: ignore[arg-type]

    monkeypatch.setattr("brutex.data.raw_integrity_store.read_regular_text", _selective_boom)
    symbols = {r.groww_symbol for r in load_records(tmp_path).values()}
    assert symbols == {"NSE-GOOD"}  # the good shard survives the bad one


# --------------------------------------------------------------------------- #
# Bounded cross-process flock (DEC-PULL-CONTENTION-ANTISTALL-001 GAP #14)
# --------------------------------------------------------------------------- #
def test_append_uses_nonblocking_flock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import fcntl

    ops: list[int] = []

    def _spy_flock(fd: int, operation: int) -> None:
        ops.append(operation)

    monkeypatch.setattr("brutex.data.raw_integrity_store.fcntl.flock", _spy_flock)
    append_record(_record(), tmp_path)
    assert ops == [fcntl.LOCK_EX | fcntl.LOCK_NB]  # bounded, non-blocking acquire


def test_append_lock_timeout_skips_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A wedged peer holding the shard flock → the record is SKIPPED, never a block."""
    import fcntl

    def _always_blocked(fd: int, operation: int) -> None:
        raise BlockingIOError("peer holds the shard lock")

    # No real sleeping; a clock that jumps past the deadline immediately.
    monkeypatch.setattr("brutex.data.raw_integrity_store.fcntl.flock", _always_blocked)
    monkeypatch.setattr("brutex.data.raw_integrity_store.time.sleep", lambda _s: None)
    ticks = iter([0.0, 100.0])
    monkeypatch.setattr("brutex.data.raw_integrity_store.time.monotonic", lambda: next(ticks))
    append_record(_record("NSE-WEDGED"), tmp_path)  # must NOT raise / block
    # The additive record was dropped (best-effort skip — the hash is not worth blocking).
    assert load_records(tmp_path) == {}
    assert fcntl.LOCK_NB  # sanity: the constant exists on this platform


def test_append_lock_timeout_then_retry_acquires(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A peer that releases AFTER one poll → the bounded acquire wins on the retry."""
    import fcntl

    real_flock = fcntl.flock
    calls = {"n": 0}

    def _blocked_once(fd: int, operation: int) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise BlockingIOError("peer holds it briefly")
        real_flock(fd, operation)  # second attempt wins

    monkeypatch.setattr("brutex.data.raw_integrity_store.fcntl.flock", _blocked_once)
    monkeypatch.setattr("brutex.data.raw_integrity_store.time.sleep", lambda _s: None)
    append_record(_record("NSE-RETRY"), tmp_path)
    (record,) = load_records(tmp_path).values()
    assert record.groww_symbol == "NSE-RETRY"
    assert calls["n"] == 2  # one block, then a winning retry
