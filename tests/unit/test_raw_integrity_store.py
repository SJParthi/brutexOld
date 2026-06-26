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


def test_load_unreadable_file_degrades_to_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = _record()
    append_record(record, tmp_path)

    def _boom(*_a: object, **_k: object) -> str:
        raise OSError("disk gone")

    monkeypatch.setattr(Path, "read_text", _boom)
    assert load_records(tmp_path) == {}


# --------------------------------------------------------------------------- #
# make_capture_hook — best-effort, never raises
# --------------------------------------------------------------------------- #
def test_capture_hook_persists_record(tmp_path: Path) -> None:
    hook = make_capture_hook(tmp_path, exchange="NSE", segment="CASH", groww_symbol="NSE-NIFTY")
    hook(_PAYLOAD, "1minute", "2024-01-01 00:00:00", "2024-01-31 00:00:00")
    loaded = load_records(tmp_path)
    (record,) = loaded.values()
    assert record.payload_hash == hash_payload(_PAYLOAD)
    assert record.groww_symbol == "NSE-NIFTY"
    assert record.interval == "1minute"


def test_capture_hook_swallows_malformed_payload(tmp_path: Path) -> None:
    hook = make_capture_hook(tmp_path, exchange="NSE", segment="CASH", groww_symbol="X")
    # An un-hashable payload must NEVER raise out of the hook (best-effort).
    hook({"candles": object()}, "1minute", "a", "b")
    assert load_records(tmp_path) == {}  # nothing persisted, no crash


def test_capture_hook_swallows_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hook = make_capture_hook(tmp_path, exchange="NSE", segment="CASH", groww_symbol="X")

    def _boom(*_a: object, **_k: object) -> None:
        raise OSError("no space left")

    monkeypatch.setattr("brutex.data.raw_integrity_store.append_record", _boom)
    # A sidecar write failure must NEVER raise out of the hook (best-effort).
    hook(_PAYLOAD, "1minute", "a", "b")


# --------------------------------------------------------------------------- #
# make_capture_hook_factory — per-symbol attribution (the live-pull seam)
# --------------------------------------------------------------------------- #
def test_capture_hook_factory_attributes_each_symbol(tmp_path: Path) -> None:
    # A single factory (bound to one lake root) builds a CORRECTLY per-symbol hook —
    # the property the live build sites rely on (one adapter, many symbols).
    factory = make_capture_hook_factory(tmp_path)
    factory("NSE", "CASH", "NSE-NIFTY")(_PAYLOAD, "1minute", "2024-01-01 00:00:00", "e1")
    factory("BSE", "CASH", "BSE-SENSEX")(_PAYLOAD, "1minute", "2024-01-02 00:00:00", "e2")
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
        assert operation == fcntl.LOCK_EX  # exclusive, held for the write
        locked.append(operation)

    monkeypatch.setattr("brutex.data.raw_integrity_store.fcntl.flock", _spy_flock)
    append_record(_record(), tmp_path)
    assert locked == [fcntl.LOCK_EX]  # the advisory lock was taken once
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
