"""Unit tests for :mod:`brutex.orchestrator.lake_replay` — the P-20 byte oracle.

DEC-AUDIT-LAKE-REPLAY-ORACLE-001. 100% line + branch of ``lake_replay.py``:

* the happy capture→replay roundtrip through the REAL sink/writer stack
  (single partition, month straddle, all-dedup re-pull, keep-first conflict,
  ``latest-wins`` forwarding, out-of-chain-order events sorted by
  ``capture_seq``);
* anchor-mode logs — :class:`ReplayCaptureMissing` from reconstruction,
  :func:`check_capture_anchors` clean / drifted / missing-file verdicts;
* chain integrity — a tampered chain dies as ``AuditChainCorruption`` BEFORE
  any reconstruction, the documented ``verify=False`` bypass, the torn-tail
  prefix replay surfacing as a loud byte mismatch;
* tampered payload shapes (junk ``capture_seq`` / ``partitions`` / ``bars``,
  an empty ``bars`` array the producer never emits) → loud refusals;
* per-event anchor mismatches (count / rel path / raw_hash) naming the exact
  ``capture_seq`` + partition;
* :func:`assert_lake_byte_equal` — missing files, the first-divergent-byte
  offset, all four three-way cross-check verdicts, strict un-audited
  partition detection + the ``strict=False`` scoping.

Every adversarial payload is committed through a GENUINE
:class:`~brutex.orchestrator.audit.AuditWriter`, so the chain itself is
valid and the refusal under test is the payload validation, never the chain.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from brutex.data.audit_capture import (
    AuditCaptureSink,
    CaptureMode,
    DedupJournal,
    encode_bars,
)
from brutex.data.models import CandleBar
from brutex.data.option_dedup import OptionDedupIndex
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.sink import ParquetBarSink
from brutex.orchestrator.audit import (
    AuditChainCorruption,
    AuditWriter,
    BufferedAuditRecord,
    verify_chain,
)
from brutex.orchestrator.lake_replay import (
    LakeReplayReport,
    ReplayCaptureMissing,
    ReplayMismatchError,
    assert_lake_byte_equal,
    check_capture_anchors,
    replay_lake_from_audit,
)

# ────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────

_JAN_TS = datetime(2021, 1, 29, 6, 0, tzinfo=UTC)  # mid-day UTC == same IST date
_FEB_TS = datetime(2021, 2, 2, 6, 0, tzinfo=UTC)

_COORDS: dict[str, str] = {
    "exchange": "NSE",
    "segment": "INDEX",
    "symbol": "NSE-NIFTY",
    "interval": "1minute",
}


def _bar(base: datetime, minute: int, *, close: str = "100.25", volume: int = 10) -> CandleBar:
    price = Decimal(close)
    return CandleBar(
        timestamp=base + timedelta(minutes=minute),
        open=price,
        high=price,
        low=price,
        close=price,
        volume=volume,
        open_interest=None,
    )


def _write_records(audit_path: Path, records: list[BufferedAuditRecord]) -> None:
    """Commit *records* through a genuine hash-chained :class:`AuditWriter`."""
    with AuditWriter.open(audit_path, resume=audit_path.exists()) as writer:
        for record in records:
            writer.append(action=record.action, subject=record.subject, payload=record.payload)


class _CaptureHarness:
    """A capture-mode sink over a REAL lake + a genuine audit chain."""

    def __init__(self, tmp_path: Path, *, journal: DedupJournal | None = None) -> None:
        self.lake_root = tmp_path / "lake"
        self.replay_root = tmp_path / "replay"
        self.audit_path = tmp_path / "audit" / "spot-ingest.jsonl"
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        inner = ParquetBarSink(writer=ParquetWriter(lake_root=self.lake_root))
        self.sink = AuditCaptureSink(
            inner, lake_root=self.lake_root, mode=CaptureMode.CAPTURE, journal=journal
        )

    def append(self, bars: list[CandleBar], **overrides: str) -> None:
        coords: dict[str, str] = {**_COORDS, **overrides}
        self.sink.append(
            exchange=coords["exchange"],
            segment=coords["segment"],
            symbol=coords["symbol"],
            interval=coords["interval"],
            bars=bars,
            merge_mode=coords.get("merge_mode", "keep-first"),
        )

    def drain_to_chain(self) -> list[BufferedAuditRecord]:
        records = list(self.sink.drain_remaining())
        _write_records(self.audit_path, records)
        return records

    def replay(self, *, verify: bool = True) -> LakeReplayReport:
        return replay_lake_from_audit(self.audit_path, replay_root=self.replay_root, verify=verify)


def _capture_payload(tmp_path: Path, bars: list[CandleBar], **overrides: str) -> dict[str, Any]:
    """One REAL captured payload dict (a template for adversarial mutation)."""
    harness = _CaptureHarness(tmp_path / "template")
    harness.append(bars, **overrides)
    (record,) = harness.sink.drain_remaining()
    return dict(record.payload)


def _write_event(audit_path: Path, payload: dict[str, Any], *, subject: str | None = None) -> None:
    """Commit ONE ``LAKE_APPEND_WRITTEN`` event with an arbitrary payload."""
    _write_records(
        audit_path,
        [
            BufferedAuditRecord(
                action="LAKE_APPEND_WRITTEN",
                subject=subject or "NSE:INDEX:NSE-NIFTY:1minute",
                payload=payload,
            )
        ],
    )


# ────────────────────────────────────────────────────────────────────────
# Happy path — capture → replay → byte equality
# ────────────────────────────────────────────────────────────────────────


def test_single_append_roundtrip_byte_equal(tmp_path: Path) -> None:
    """One append, one partition: replay reproduces the exact lake bytes."""
    harness = _CaptureHarness(tmp_path)
    bars = [_bar(_JAN_TS, i, close=f"100.{10 + i}") for i in range(5)]
    harness.append(bars)
    harness.drain_to_chain()
    report = harness.replay()
    assert report.appends_replayed == 1
    rel = "bars/NSE/INDEX/NSE-NIFTY/1minute/2021/01.parquet"
    assert report.partitions == frozenset({rel})
    assert report.anchor_matches == 1
    assert report.anchor_mismatches == ()
    expected_hash = hashlib.sha256((harness.lake_root / rel).read_bytes()).hexdigest()
    assert report.partition_anchors == {rel: expected_hash}
    assert_lake_byte_equal(
        original_root=harness.lake_root, replay_root=harness.replay_root, report=report
    )
    assert verify_chain(harness.audit_path) > 0


def test_month_straddle_repull_and_conflict_roundtrip(tmp_path: Path) -> None:
    """Multi-partition straddle + all-dedup re-pull + keep-first conflict."""
    harness = _CaptureHarness(tmp_path)
    batch = [_bar(_JAN_TS, 0), _bar(_JAN_TS, 1), _bar(_FEB_TS, 0)]
    harness.append(batch)
    # The all-dedup re-pull: identical batch, bars_net_new == 0.
    harness.append(batch)
    # A keep-first CONFLICT: same timestamp, different close — stored wins.
    harness.append([_bar(_JAN_TS, 0, close="999.99"), _bar(_JAN_TS, 2)])
    records = harness.drain_to_chain()
    anchors = [r for r in records if r.action == "LAKE_APPEND_WRITTEN"]
    assert anchors[1].payload["bars_net_new"] == 0
    assert [p["bars_added"] for p in anchors[1].payload["partitions"]] == [0, 0]
    # Unchanged raw_hash on the no-op re-pull.
    assert [p["raw_hash"] for p in anchors[1].payload["partitions"]] == [
        p["raw_hash"] for p in anchors[0].payload["partitions"]
    ]
    report = harness.replay()
    assert report.appends_replayed == 3
    assert report.partitions == frozenset(
        {
            "bars/NSE/INDEX/NSE-NIFTY/1minute/2021/01.parquet",
            "bars/NSE/INDEX/NSE-NIFTY/1minute/2021/02.parquet",
        }
    )
    assert_lake_byte_equal(
        original_root=harness.lake_root, replay_root=harness.replay_root, report=report
    )


def test_latest_wins_merge_mode_forwarded(tmp_path: Path) -> None:
    """``latest-wins`` (no production emitter this round) replays verbatim."""
    harness = _CaptureHarness(tmp_path)
    harness.append([_bar(_JAN_TS, 0, close="100.00")], interval="1day", merge_mode="latest-wins")
    harness.append([_bar(_JAN_TS, 0, close="200.00")], interval="1day", merge_mode="latest-wins")
    records = harness.drain_to_chain()
    assert all(r.payload["merge_mode"] == "latest-wins" for r in records)
    report = harness.replay()
    assert report.appends_replayed == 2
    assert_lake_byte_equal(
        original_root=harness.lake_root, replay_root=harness.replay_root, report=report
    )


def test_replay_sorts_by_capture_seq_not_chain_order(tmp_path: Path) -> None:
    """Chain-line order varies run-to-run; ``capture_seq`` is the apply order.

    The keep-first conflict makes the order LOAD-BEARING: applying the
    events in reversed chain order would store the conflicting close and
    diverge, so byte equality proves the sort.
    """
    harness = _CaptureHarness(tmp_path)
    harness.append([_bar(_JAN_TS, 0, close="100.00")])
    harness.append([_bar(_JAN_TS, 0, close="555.55"), _bar(_JAN_TS, 1)])
    records = list(harness.sink.drain_remaining())
    _write_records(harness.audit_path, list(reversed(records)))
    report = harness.replay()
    assert report.appends_replayed == 2
    assert_lake_byte_equal(
        original_root=harness.lake_root, replay_root=harness.replay_root, report=report
    )


def test_multi_run_resumed_chain_replays_in_run_order(tmp_path: Path) -> None:
    """Two pulls (fresh sink each — ``capture_seq`` restarts at 0) on ONE
    resume-appended chain replay in run order, never interleaved by seq.

    Round-1 review fix regression (the executed PoC shape): run 1 appends
    NIFTY then BANKNIFTY; run 2 (a new process — a fresh
    :class:`AuditCaptureSink`) appends BANKNIFTY then NIFTY, with a cross-run
    keep-first CONFLICT on the BANKNIFTY partition. A GLOBAL ``capture_seq``
    sort would apply run 2's BANKNIFTY (seq 0) BEFORE run 1's (seq 1) —
    storing the conflicting close and false-raising on run 2's anchor (which
    was captured over the merged live file). The ``(capture_run,
    capture_seq)`` order replays the honest multi-pull log clean.
    """
    lake_root = tmp_path / "lake"
    replay_root = tmp_path / "replay"
    audit_path = tmp_path / "audit" / "spot-ingest.jsonl"
    audit_path.parent.mkdir(parents=True, exist_ok=True)

    def _one_run(symbol_batches: list[tuple[str, list[CandleBar]]]) -> str:
        """One 'process': a FRESH capture sink drained onto the shared chain."""
        sink = AuditCaptureSink(
            ParquetBarSink(writer=ParquetWriter(lake_root=lake_root)),
            lake_root=lake_root,
            mode=CaptureMode.CAPTURE,
        )
        for symbol, bars in symbol_batches:
            sink.append(
                exchange="NSE", segment="INDEX", symbol=symbol, interval="1minute", bars=bars
            )
        _write_records(audit_path, list(sink.drain_remaining()))
        return sink.run_nonce

    # Run 1: NIFTY (seq 0) then BANKNIFTY (seq 1) — BANKNIFTY stores 100.00.
    _one_run(
        [
            ("NSE-NIFTY", [_bar(_JAN_TS, 0)]),
            ("NSE-BANKNIFTY", [_bar(_JAN_TS, 0, close="100.00")]),
        ]
    )
    # Run 2 (seq restarts at 0): BANKNIFTY FIRST — a keep-first conflict
    # (999.99 must LOSE to run 1's stored 100.00) + one net-new bar — then NIFTY.
    _one_run(
        [
            ("NSE-BANKNIFTY", [_bar(_JAN_TS, 0, close="999.99"), _bar(_JAN_TS, 1)]),
            ("NSE-NIFTY", [_bar(_JAN_TS, 1)]),
        ]
    )
    report = replay_lake_from_audit(audit_path, replay_root=replay_root)
    assert report.appends_replayed == 4
    assert report.anchor_matches == 4
    assert_lake_byte_equal(original_root=lake_root, replay_root=replay_root, report=report)
    # The anchor sweep picks each partition's truly-LAST anchor (run 2's) —
    # the untampered live lake verifies clean.
    anchors = check_capture_anchors(audit_path, lake_root=lake_root)
    assert anchors.anchor_mismatches == ()
    assert anchors.anchor_matches == 2


def test_duplicate_capture_run_seq_pair_refused(tmp_path: Path) -> None:
    """A duplicate ``(capture_run, capture_seq)`` pair is refused loudly —
    the producer stamps each run's seq exactly once, so a collision must
    never be silently mis-ordered."""
    payload = _valid_payload(tmp_path)
    audit_path = tmp_path / "audit.jsonl"
    _write_event(audit_path, payload)
    _write_event(audit_path, payload)  # same run nonce + same seq
    with pytest.raises(ReplayMismatchError, match=r"duplicate LAKE_APPEND_WRITTEN capture_seq=0"):
        replay_lake_from_audit(audit_path, replay_root=tmp_path / "replay")
    # check_capture_anchors shares the loader — the sweep refuses too.
    with pytest.raises(ReplayMismatchError, match=r"duplicate"):
        check_capture_anchors(audit_path, lake_root=tmp_path / "lake")


def test_foreign_actions_and_add_rows_ignored(tmp_path: Path) -> None:
    """Coarse ``INGEST_*`` lines and L3 ``add`` rows never reach replay."""
    journal = DedupJournal(OptionDedupIndex())
    harness = _CaptureHarness(tmp_path, journal=journal)
    with AuditWriter.open(harness.audit_path) as writer:
        writer.append(action="INGEST_RUN_START", subject="pull:x", payload={"pairs": 1})
    harness.append([_bar(_JAN_TS, 0)])
    records = harness.drain_to_chain()
    assert [r.action for r in records] == ["LAKE_APPEND_WRITTEN", "add"]
    with AuditWriter.open(harness.audit_path, resume=True) as writer:
        writer.append(action="INGEST_RUN_COMPLETE", subject="pull:x", payload={})
    report = harness.replay()
    assert report.appends_replayed == 1
    assert_lake_byte_equal(
        original_root=harness.lake_root, replay_root=harness.replay_root, report=report
    )


def test_empty_audit_log_is_an_empty_report(tmp_path: Path) -> None:
    """A chain with zero capture events replays to an empty report."""
    audit_path = tmp_path / "audit.jsonl"
    with AuditWriter.open(audit_path) as writer:
        writer.append(action="INGEST_RUN_START", subject="pull:x", payload={})
    report = replay_lake_from_audit(audit_path, replay_root=tmp_path / "replay")
    assert report.appends_replayed == 0
    assert report.partitions == frozenset()
    assert report.anchor_matches == 0
    assert report.partition_anchors == {}
    # Byte equality over zero audited partitions is vacuously green.
    original = tmp_path / "original"
    original.mkdir()
    assert_lake_byte_equal(original_root=original, replay_root=tmp_path / "replay", report=report)


# ────────────────────────────────────────────────────────────────────────
# Anchor-mode logs — reconstruction refused, verification works
# ────────────────────────────────────────────────────────────────────────


def _anchor_mode_harness(tmp_path: Path) -> tuple[Path, Path]:
    """An ANCHOR-mode capture (no ``bars`` payloads) over a real lake."""
    lake_root = tmp_path / "lake"
    audit_path = tmp_path / "audit.jsonl"
    inner = ParquetBarSink(writer=ParquetWriter(lake_root=lake_root))
    sink = AuditCaptureSink(inner, lake_root=lake_root, mode=CaptureMode.ANCHOR)
    sink.append(
        exchange="NSE",
        segment="INDEX",
        symbol="NSE-NIFTY",
        interval="1minute",
        bars=[_bar(_JAN_TS, 0), _bar(_FEB_TS, 0)],
    )
    _write_records(audit_path, list(sink.drain_remaining()))
    return audit_path, lake_root


def test_anchor_mode_log_refuses_reconstruction(tmp_path: Path) -> None:
    """A ``bars``-less event fed to replay raises ReplayCaptureMissing."""
    audit_path, _lake_root = _anchor_mode_harness(tmp_path)
    with pytest.raises(ReplayCaptureMissing, match=r"capture_seq=0 .*check_capture_anchors"):
        replay_lake_from_audit(audit_path, replay_root=tmp_path / "replay")


def test_check_capture_anchors_clean(tmp_path: Path) -> None:
    """Anchor verification over an untouched lake: every partition matches."""
    audit_path, lake_root = _anchor_mode_harness(tmp_path)
    report = check_capture_anchors(audit_path, lake_root=lake_root)
    assert report.appends_replayed == 1
    assert report.anchor_matches == 2
    assert report.anchor_mismatches == ()
    assert report.partitions == frozenset(
        {
            "bars/NSE/INDEX/NSE-NIFTY/1minute/2021/01.parquet",
            "bars/NSE/INDEX/NSE-NIFTY/1minute/2021/02.parquet",
        }
    )
    for rel, anchor in report.partition_anchors.items():
        assert hashlib.sha256((lake_root / rel).read_bytes()).hexdigest() == anchor


def test_check_capture_anchors_detects_post_write_mutation(tmp_path: Path) -> None:
    """A single-byte lake mutation flips exactly that partition to mismatch."""
    audit_path, lake_root = _anchor_mode_harness(tmp_path)
    jan = lake_root / "bars/NSE/INDEX/NSE-NIFTY/1minute/2021/01.parquet"
    raw = bytearray(jan.read_bytes())
    raw[13] ^= 0xFF
    jan.write_bytes(bytes(raw))
    report = check_capture_anchors(audit_path, lake_root=lake_root)
    assert report.anchor_matches == 1
    (mismatch,) = report.anchor_mismatches
    assert "2021/01.parquet" in mismatch
    assert "!= live sha256" in mismatch


def test_check_capture_anchors_missing_live_file(tmp_path: Path) -> None:
    """A vanished live partition is COLLECTED as a mismatch, never raised."""
    audit_path, lake_root = _anchor_mode_harness(tmp_path)
    (lake_root / "bars/NSE/INDEX/NSE-NIFTY/1minute/2021/02.parquet").unlink()
    report = check_capture_anchors(audit_path, lake_root=lake_root)
    assert report.anchor_matches == 1
    (mismatch,) = report.anchor_mismatches
    assert "2021/02.parquet" in mismatch
    assert "MISSING" in mismatch


def test_check_capture_anchors_last_anchor_wins(tmp_path: Path) -> None:
    """Multiple events on one partition: the LAST anchor (by seq) is checked."""
    harness = _CaptureHarness(tmp_path)
    harness.append([_bar(_JAN_TS, 0)])
    harness.append([_bar(_JAN_TS, 1)])  # rewrites the partition → new hash
    harness.drain_to_chain()
    report = check_capture_anchors(harness.audit_path, lake_root=harness.lake_root)
    assert report.appends_replayed == 2
    assert report.anchor_matches == 1
    assert report.anchor_mismatches == ()


# ────────────────────────────────────────────────────────────────────────
# Chain integrity — tampering, bypass, torn tail
# ────────────────────────────────────────────────────────────────────────


def _tamper_committed_line(audit_path: Path, *, line_index: int, needle: str, repl: str) -> None:
    """Flip bytes INSIDE one committed line (breaks the hash chain)."""
    lines = audit_path.read_text(encoding="utf-8").splitlines(keepends=True)
    assert needle in lines[line_index]
    lines[line_index] = lines[line_index].replace(needle, repl, 1)
    audit_path.write_text("".join(lines), encoding="utf-8")


def test_tampered_chain_dies_before_reconstruction(tmp_path: Path) -> None:
    """verify=True (default): a tampered chain raises AuditChainCorruption."""
    harness = _CaptureHarness(tmp_path)
    harness.append([_bar(_JAN_TS, 0)])
    harness.drain_to_chain()
    _tamper_committed_line(
        harness.audit_path, line_index=0, needle='"rows_in":1', repl='"rows_in":9'
    )
    with pytest.raises(AuditChainCorruption):
        harness.replay()
    with pytest.raises(AuditChainCorruption):
        check_capture_anchors(harness.audit_path, lake_root=harness.lake_root)
    # The replay root was never touched — the chain died first.
    assert not harness.replay_root.exists()


def test_verify_false_bypass_replays_a_tampered_chain(tmp_path: Path) -> None:
    """The documented diagnostic bypass: verify=False skips verify_chain.

    The tamper here is metadata-only (``rows_in``), so the reconstruction
    itself still succeeds — the point is that the bypass reaches it.
    """
    harness = _CaptureHarness(tmp_path)
    harness.append([_bar(_JAN_TS, 0)])
    harness.drain_to_chain()
    _tamper_committed_line(
        harness.audit_path, line_index=0, needle='"rows_in":1', repl='"rows_in":9'
    )
    report = harness.replay(verify=False)
    assert report.appends_replayed == 1
    assert_lake_byte_equal(
        original_root=harness.lake_root, replay_root=harness.replay_root, report=report
    )
    anchors_report = check_capture_anchors(
        harness.audit_path, lake_root=harness.lake_root, verify=False
    )
    assert anchors_report.anchor_mismatches == ()


def test_torn_tail_prefix_replays_then_byte_oracle_fails_loud(tmp_path: Path) -> None:
    """A torn final line is skipped; the missing append surfaces as a LOUD
    byte divergence naming the partition — never silently absorbed."""
    harness = _CaptureHarness(tmp_path)
    harness.append([_bar(_JAN_TS, 0)])
    harness.append([_bar(_JAN_TS, 1, close="333.33")])  # same partition
    harness.drain_to_chain()
    # Tear the FINAL committed line: drop its trailing newline + tail bytes.
    raw = harness.audit_path.read_bytes()
    assert raw.endswith(b"\n")
    harness.audit_path.write_bytes(raw[:-20])
    assert verify_chain(harness.audit_path) == 1  # torn tail tolerated
    report = harness.replay()
    assert report.appends_replayed == 1
    with pytest.raises(ReplayMismatchError, match=r"2021/01\.parquet"):
        assert_lake_byte_equal(
            original_root=harness.lake_root, replay_root=harness.replay_root, report=report
        )


# ────────────────────────────────────────────────────────────────────────
# Tampered payload shapes — loud refusals (genuine chain, junk payload)
# ────────────────────────────────────────────────────────────────────────


def _valid_payload(tmp_path: Path) -> dict[str, Any]:
    return _capture_payload(tmp_path, [_bar(_JAN_TS, 0)])


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        ({"capture_seq": "0"}, r"no valid capture_seq"),
        ({"capture_seq": True}, r"no valid capture_seq"),
        ({"capture_run": None}, r"no valid 'capture_run'"),
        ({"capture_run": 7}, r"no valid 'capture_run'"),
        ({"partitions": "junk"}, r"no valid partitions"),
        ({"partitions": ["junk"]}, r"malformed partition anchor"),
        ({"partitions": [{"rel": 7, "raw_hash": "ab"}]}, r"malformed partition anchor"),
        ({"partitions": [{"rel": "x.parquet", "raw_hash": 7}]}, r"malformed partition anchor"),
        ({"bars": "junk"}, r"malformed bars payload"),
        ({"bars": ["junk-row"]}, r"malformed bars row"),
        ({"exchange": None}, r"no valid 'exchange'"),
        ({"merge_mode": 3}, r"no valid 'merge_mode'"),
    ],
)
def test_tampered_payload_shape_refused(
    tmp_path: Path, mutation: dict[str, Any], match: str
) -> None:
    """A hand-crafted payload whose SHAPE cannot drive replay fails loud."""
    payload = _valid_payload(tmp_path)
    for key, value in mutation.items():
        if value is None:
            del payload[key]
        else:
            payload[key] = value
    audit_path = tmp_path / "audit.jsonl"
    _write_event(audit_path, payload)
    with pytest.raises(ReplayMismatchError, match=match):
        replay_lake_from_audit(audit_path, replay_root=tmp_path / "replay")


def test_missing_capture_seq_names_entry_seq(tmp_path: Path) -> None:
    """An absent ``capture_seq`` is named by the chain entry's own seq."""
    payload = _valid_payload(tmp_path)
    del payload["capture_seq"]
    audit_path = tmp_path / "audit.jsonl"
    _write_event(audit_path, payload)
    with pytest.raises(ReplayMismatchError, match=r"entry seq=0 .*capture_seq: None"):
        replay_lake_from_audit(audit_path, replay_root=tmp_path / "replay")


def test_empty_bars_array_is_tampering(tmp_path: Path) -> None:
    """The producer never emits an empty ``bars`` — its presence is tampering."""
    payload = _valid_payload(tmp_path)
    payload["bars"] = []
    audit_path = tmp_path / "audit.jsonl"
    _write_event(audit_path, payload)
    with pytest.raises(ReplayMismatchError, match=r"EMPTY 'bars' array"):
        replay_lake_from_audit(audit_path, replay_root=tmp_path / "replay")


# ────────────────────────────────────────────────────────────────────────
# Per-event anchor mismatches — divergence localized to the exact append
# ────────────────────────────────────────────────────────────────────────


def test_anchor_raw_hash_mismatch_names_seq_and_partition(tmp_path: Path) -> None:
    """A wrong captured raw_hash raises on ITS event, naming both hashes."""
    payload = _valid_payload(tmp_path)
    real_hash = payload["partitions"][0]["raw_hash"]
    payload["partitions"][0]["raw_hash"] = "0" * 64
    audit_path = tmp_path / "audit.jsonl"
    _write_event(audit_path, payload)
    with pytest.raises(
        ReplayMismatchError,
        match=rf"capture_seq=0 partition .*2021/01\.parquet: captured raw_hash 0{{64}} != "
        rf"replayed raw_hash {real_hash}",
    ):
        replay_lake_from_audit(audit_path, replay_root=tmp_path / "replay")


def test_anchor_partition_count_mismatch(tmp_path: Path) -> None:
    """More recorded anchors than replayed partitions is a loud mismatch."""
    payload = _valid_payload(tmp_path)
    payload["partitions"] = payload["partitions"] * 2
    audit_path = tmp_path / "audit.jsonl"
    _write_event(audit_path, payload)
    with pytest.raises(ReplayMismatchError, match=r"replayed into 1 partition.*recorded 2"):
        replay_lake_from_audit(audit_path, replay_root=tmp_path / "replay")


def test_anchor_rel_path_mismatch(tmp_path: Path) -> None:
    """A wrong recorded partition path is a loud mismatch naming both."""
    payload = _valid_payload(tmp_path)
    payload["partitions"][0]["rel"] = "bars/NSE/INDEX/NSE-NIFTY/1minute/2020/12.parquet"
    audit_path = tmp_path / "audit.jsonl"
    _write_event(audit_path, payload)
    with pytest.raises(
        ReplayMismatchError, match=r"replayed .*2021/01\.parquet.*recorded .*2020/12"
    ):
        replay_lake_from_audit(audit_path, replay_root=tmp_path / "replay")


# ────────────────────────────────────────────────────────────────────────
# assert_lake_byte_equal — the byte oracle + three-way cross-check
# ────────────────────────────────────────────────────────────────────────


def _report_for(rel: str, *, anchor: str | None) -> LakeReplayReport:
    return LakeReplayReport(
        appends_replayed=1,
        partitions=frozenset({rel}),
        anchor_matches=1,
        anchor_mismatches=(),
        partition_anchors={} if anchor is None else {rel: anchor},
    )


def _twin_files(tmp_path: Path, rel: str, *, original: bytes, replayed: bytes) -> tuple[Path, Path]:
    original_root = tmp_path / "original"
    replay_root = tmp_path / "replay"
    for root, data in ((original_root, original), (replay_root, replayed)):
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return original_root, replay_root


def test_byte_equal_missing_original_file(tmp_path: Path) -> None:
    """An audited partition absent from the ORIGINAL lake fails loud."""
    rel = "bars/NSE/INDEX/NSE-NIFTY/1minute/2021/01.parquet"
    _original_root, replay_root = _twin_files(tmp_path, rel, original=b"x", replayed=b"x")
    empty_root = tmp_path / "empty"
    empty_root.mkdir()
    with pytest.raises(ReplayMismatchError, match=r"MISSING from the original lake"):
        assert_lake_byte_equal(
            original_root=empty_root, replay_root=replay_root, report=_report_for(rel, anchor=None)
        )


def test_byte_equal_missing_replayed_file(tmp_path: Path) -> None:
    """An audited partition absent from the REPLAYED lake fails loud."""
    rel = "bars/NSE/INDEX/NSE-NIFTY/1minute/2021/01.parquet"
    original_root, _replay_root = _twin_files(tmp_path, rel, original=b"x", replayed=b"x")
    empty_root = tmp_path / "empty"
    empty_root.mkdir()
    with pytest.raises(ReplayMismatchError, match=r"MISSING from the replayed lake"):
        assert_lake_byte_equal(
            original_root=original_root,
            replay_root=empty_root,
            report=_report_for(rel, anchor=None),
        )


def test_divergence_verdict_live_lake_mutated_after_write(tmp_path: Path) -> None:
    """Real roundtrip + post-write mutation: verdict = live lake mutated,
    and the FIRST divergent byte offset is named."""
    harness = _CaptureHarness(tmp_path)
    harness.append([_bar(_JAN_TS, 0)])
    harness.drain_to_chain()
    report = harness.replay()
    rel = next(iter(report.partitions))
    live = harness.lake_root / rel
    raw = bytearray(live.read_bytes())
    raw[13] ^= 0xFF
    live.write_bytes(bytes(raw))
    with pytest.raises(
        ReplayMismatchError,
        match=r"first divergent byte offset 13.*mutated AFTER the audited write",
    ):
        assert_lake_byte_equal(
            original_root=harness.lake_root, replay_root=harness.replay_root, report=report
        )


def test_divergence_verdict_replay_diverged(tmp_path: Path) -> None:
    """original == anchor, replayed != anchor ⇒ the REPLAY diverged."""
    rel = "bars/x/01.parquet"
    original = b"canonical-bytes"
    original_root, replay_root = _twin_files(tmp_path, rel, original=original, replayed=b"drifted!")
    anchor = hashlib.sha256(original).hexdigest()
    with pytest.raises(
        ReplayMismatchError, match=r"REPLAY diverged .*capture/writer nondeterminism"
    ):
        assert_lake_byte_equal(
            original_root=original_root,
            replay_root=replay_root,
            report=_report_for(rel, anchor=anchor),
        )


def test_divergence_verdict_neither_side_matches(tmp_path: Path) -> None:
    """Neither side matches the audited raw_hash ⇒ both diverged."""
    rel = "bars/x/01.parquet"
    original_root, replay_root = _twin_files(tmp_path, rel, original=b"aaa", replayed=b"bbb")
    with pytest.raises(ReplayMismatchError, match=r"NEITHER side matches"):
        assert_lake_byte_equal(
            original_root=original_root,
            replay_root=replay_root,
            report=_report_for(rel, anchor="f" * 64),
        )


def test_divergence_verdict_no_anchor_available(tmp_path: Path) -> None:
    """No recorded anchor for the partition ⇒ the honest no-cross-check note."""
    rel = "bars/x/01.parquet"
    original_root, replay_root = _twin_files(tmp_path, rel, original=b"aaa", replayed=b"bbb")
    with pytest.raises(ReplayMismatchError, match=r"no audited raw_hash available"):
        assert_lake_byte_equal(
            original_root=original_root,
            replay_root=replay_root,
            report=_report_for(rel, anchor=None),
        )


def test_divergence_offset_for_strict_prefix(tmp_path: Path) -> None:
    """A strict-prefix divergence names the shorter length as the offset."""
    rel = "bars/x/01.parquet"
    original_root, replay_root = _twin_files(tmp_path, rel, original=b"abcd", replayed=b"abcdEXTRA")
    with pytest.raises(
        ReplayMismatchError,
        match=r"original 4 bytes .*replayed 9 bytes .*first divergent byte offset 4",
    ):
        assert_lake_byte_equal(
            original_root=original_root,
            replay_root=replay_root,
            report=_report_for(rel, anchor=None),
        )


def test_strict_mode_flags_unaudited_live_partition(tmp_path: Path) -> None:
    """strict=True names live parquet never captured; strict=False scopes."""
    harness = _CaptureHarness(tmp_path)
    harness.append([_bar(_JAN_TS, 0)])
    harness.drain_to_chain()
    report = harness.replay()
    stray = harness.lake_root / "bars/NSE/INDEX/NSE-NIFTY/1day/2021/01.parquet"
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_bytes(b"derived-output")
    with pytest.raises(ReplayMismatchError, match=r"1 live parquet partition.*1day/2021/01"):
        assert_lake_byte_equal(
            original_root=harness.lake_root, replay_root=harness.replay_root, report=report
        )
    # strict=False: the honest mode for a lake holding derived output.
    assert_lake_byte_equal(
        original_root=harness.lake_root,
        replay_root=harness.replay_root,
        report=report,
        strict=False,
    )


def test_strict_mode_ignores_non_bars_sidecar_parquet(tmp_path: Path) -> None:
    """strict=True stays GREEN on a real lake's non-bar parquet sidecars.

    Round-1 review fix regression: every real CLI pull writes
    ``window_ledger/ledger.parquet`` (+ delta parquets) and heal writes
    ``.quarantine/**`` copies — none routes through the capture-wrapped
    :class:`~brutex.data.sink.ParquetBarSink`, so the strict "write bypassed
    audit" walk must scope to ``bars/`` or it false-flags every genuine
    production capture (forcing strict=False and losing the bypass-detection
    class entirely).
    """
    harness = _CaptureHarness(tmp_path)
    harness.append([_bar(_JAN_TS, 0)])
    harness.drain_to_chain()
    report = harness.replay()
    ledger = harness.lake_root / "window_ledger" / "ledger.parquet"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_bytes(b"window-ledger-parquet")
    (harness.lake_root / "window_ledger" / "ledger-delta-000001.parquet").write_bytes(b"delta")
    quarantine = harness.lake_root / ".quarantine" / "20260707T000000Z" / "01.parquet"
    quarantine.parent.mkdir(parents=True, exist_ok=True)
    quarantine.write_bytes(b"heal-copy")
    # strict=True (the default): the sidecars are OUT of the bars/ scope.
    assert_lake_byte_equal(
        original_root=harness.lake_root, replay_root=harness.replay_root, report=report
    )
    # An un-audited stray UNDER bars/ is still flagged (the class is kept).
    stray = harness.lake_root / "bars/NSE/INDEX/NSE-NIFTY/1day/2021/01.parquet"
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_bytes(b"derived-output")
    with pytest.raises(ReplayMismatchError, match=r"under bars/ .*1day/2021/01"):
        assert_lake_byte_equal(
            original_root=harness.lake_root, replay_root=harness.replay_root, report=report
        )


def test_encoded_bars_in_payload_round_trip_through_chain(tmp_path: Path) -> None:
    """The chain's canonical JSON preserves the encoded tuples verbatim."""
    harness = _CaptureHarness(tmp_path)
    bars = [_bar(_JAN_TS, 0, close="14020.79"), _bar(_JAN_TS, 1, close="0.05")]
    harness.append(bars)
    harness.drain_to_chain()
    from brutex.orchestrator.audit import iter_entries

    (entry,) = [e for e in iter_entries(harness.audit_path) if e.action == "LAKE_APPEND_WRITTEN"]
    assert entry.payload["bars"] == encode_bars(bars)
