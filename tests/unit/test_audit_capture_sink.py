"""Unit tests for :mod:`brutex.data.audit_capture` — the P-20/L3 capture producer.

DEC-AUDIT-LAKE-REPLAY-ORACLE-001. 100% line + branch of ``audit_capture.py``:

* env resolver (unset / blank / off / anchor / capture / junk → fail-loud,
  ``os.environ`` default source);
* canonical bar encoding — exact Decimal / ``None`` / UTC round-trip, the
  :class:`FnoGreeksBar` loud refusal, the sub-second-timestamp refusal, and
  every ``decode_bars`` junk-row branch (tampered payloads never decode
  silently);
* :class:`AuditCaptureSink` — transparent delegation (result ``is`` the
  inner's), the lock-spans-delegate white-box pin (stamp order IS lake-apply
  order), monotonic ``capture_seq``, empty-append short-circuit,
  inner-exception → nothing buffered, pair-scoped vs remaining drains,
  anchor vs capture payload shape, the ``"|"``-subject AND the
  ``"\\x1f"``-coordinate (dedup-key) refusals BEFORE the durable write, the
  journal-fault-after-durable-write event/add-row buffering (round-5 review
  fixes);
* :class:`DedupJournal` — admitted-once semantics + the ``out`` accumulator
  exception seam, the ``added_keys_digest`` determinism AND
  order-insensitivity (sorted-set digest — mutant-killing fixtures), zero
  ``add`` rows on a re-pull;
* :func:`resolve_audit_capture` — the OFF identity pin (the IDENTICAL
  un-wrapped sink object), anchor/capture wiring, the LMDB-backed exact
  tier, the once-per-process ``audit_capture.enabled`` WARN.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import fno_math
import pytest
from brutex.data.audit_capture import (
    CAPTURE_MODE_ENV,
    CAPTURE_PAYLOAD_VERSION,
    DEDUP_ADD_ACTION,
    LAKE_APPEND_ACTION,
    AuditCaptureError,
    AuditCaptureSink,
    CaptureMode,
    DedupJournal,
    _added_keys_digest,  # pyright: ignore[reportPrivateUsage]
    decode_bars,
    encode_bars,
    input_blake3,
    reset_capture_enable_warnings,
    resolve_audit_capture,
    resolve_capture_mode,
)
from brutex.data.candle_fields import CandleParseError
from brutex.data.models import CandleBar, FnoGreeksBar
from brutex.data.option_dedup import OptionDedupIndex, candle_dedup_key
from brutex.data.parquet_writer import ParquetWriter, WriteResult
from brutex.data.sink import ParquetBarSink, SinkAppendResult
from brutex.orchestrator.audit import (
    _canonical_json,  # pyright: ignore[reportPrivateUsage]
)
from structlog.testing import capture_logs

# ────────────────────────────────────────────────────────────────────────
# Fixtures + helpers
# ────────────────────────────────────────────────────────────────────────

_BASE_TS = datetime(2021, 1, 4, 4, 0, tzinfo=UTC)  # 09:30 IST, a Monday


@pytest.fixture(autouse=True)
def _fresh_warn_guard() -> Iterator[None]:
    """Isolate the once-per-process enable-WARN guard per test."""
    reset_capture_enable_warnings()
    yield
    reset_capture_enable_warnings()


def _bar(
    minute: int,
    *,
    close: str = "100.25",
    volume: int = 10,
    open_interest: int | None = None,
) -> CandleBar:
    """One valid whole-second candle at ``_BASE_TS + minute``."""
    price = Decimal(close)
    return CandleBar(
        timestamp=_BASE_TS + timedelta(minutes=minute),
        open=price,
        high=price,
        low=price,
        close=price,
        volume=volume,
        open_interest=open_interest,
    )


def _greeks_bar(minute: int) -> FnoGreeksBar:
    """One enriched F&O bar — the overlay-loss refusal input."""
    price = Decimal("55.10")
    return FnoGreeksBar(
        timestamp=_BASE_TS + timedelta(minutes=minute),
        open=price,
        high=price,
        low=price,
        close=price,
        volume=5,
        open_interest=750,
        iv=0.18,
        delta=0.5,
    )


class RecordingSink:
    """A :class:`BarSink` fake — records calls, returns a canned result."""

    def __init__(self, *, lake_root: Path, results: list[SinkAppendResult] | None = None) -> None:
        self.lake_root = lake_root
        self.calls: list[dict[str, Any]] = []
        self._results = results

    def append(
        self,
        *,
        exchange: str,
        segment: str,
        symbol: str,
        interval: str,
        bars: Sequence[CandleBar],
        merge_mode: str = "keep-first",
    ) -> SinkAppendResult:
        self.calls.append(
            {
                "exchange": exchange,
                "segment": segment,
                "symbol": symbol,
                "interval": interval,
                "bars": list(bars),
                "merge_mode": merge_mode,
            }
        )
        if self._results is not None:
            return self._results.pop(0)
        if not bars:
            return SinkAppendResult(bars_appended=0, partitions_written=0, write_results=())
        write_result = WriteResult(
            path=self.lake_root / "bars/NSE/INDEX/NSE-NIFTY/1minute/2021/01.parquet",
            row_count=len(bars),
            raw_hash="ab" * 32,
            first_ts=bars[0].timestamp,
            last_ts=bars[-1].timestamp,
            bytes_written=1024,
            bars_added=len(bars),
        )
        return SinkAppendResult(
            bars_appended=len(bars),
            partitions_written=1,
            write_results=(write_result,),
            bars_net_new=len(bars),
        )


class RaisingSink:
    """A :class:`BarSink` fake whose ``append`` always raises."""

    def append(self, **_kwargs: Any) -> SinkAppendResult:
        raise RuntimeError("inner sink exploded")


class FaultyExact:
    """An exact-membership tier raising on its Nth ``add`` — the L3
    fault-injection seam (LMDB write error / Bloom FAIL_CLOSED stand-in)."""

    def __init__(self, fail_on_add: int) -> None:
        self._seen: set[bytes] = set()
        self._adds = 0
        self._fail_on_add = fail_on_add

    def __contains__(self, key: bytes, /) -> bool:
        return key in self._seen

    def add(self, key: bytes, /) -> None:
        self._adds += 1
        if self._adds == self._fail_on_add:
            raise OSError("exact tier write failed")
        self._seen.add(key)


class LockProbeSink:
    """White-box probe: asserts the capture lock is HELD during ``append``.

    The deterministic proof that ONE critical section spans the durable
    delegate AND the ``capture_seq`` stamp — stamp order IS lake-apply order.
    """

    def __init__(self) -> None:
        self.capture: AuditCaptureSink | None = None
        self.lock_was_held: bool | None = None

    def append(self, *, bars: Sequence[CandleBar], **_kwargs: Any) -> SinkAppendResult:
        assert self.capture is not None
        acquired = self.capture._lock.acquire(blocking=False)
        if acquired:  # pragma: no cover — only on a broken lock contract
            self.capture._lock.release()
        self.lock_was_held = not acquired
        return SinkAppendResult(
            bars_appended=len(bars),
            partitions_written=0,
            write_results=(),
            bars_net_new=len(bars),
        )


def _capture_sink(
    tmp_path: Path,
    *,
    mode: CaptureMode = CaptureMode.ANCHOR,
    journal: DedupJournal | None = None,
) -> tuple[AuditCaptureSink, RecordingSink]:
    inner = RecordingSink(lake_root=tmp_path)
    return AuditCaptureSink(inner, lake_root=tmp_path, mode=mode, journal=journal), inner


# ────────────────────────────────────────────────────────────────────────
# resolve_capture_mode — the ONE env gate
# ────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("off", CaptureMode.OFF),
        ("", CaptureMode.OFF),
        ("   ", CaptureMode.OFF),
        ("anchor", CaptureMode.ANCHOR),
        ("  ANCHOR ", CaptureMode.ANCHOR),
        ("Capture", CaptureMode.CAPTURE),
        ("capture", CaptureMode.CAPTURE),
    ],
)
def test_resolve_capture_mode_values(raw: str, expected: CaptureMode) -> None:
    """Every documented value resolves case/whitespace-insensitively."""
    assert resolve_capture_mode({CAPTURE_MODE_ENV: raw}) is expected


def test_resolve_capture_mode_unset_is_off() -> None:
    """An absent env key is the production default: OFF."""
    assert resolve_capture_mode({}) is CaptureMode.OFF


@pytest.mark.parametrize("junk", ["yes", "1", "on", "anchor,capture", "offf"])
def test_resolve_capture_mode_junk_fails_loud(junk: str) -> None:
    """Junk config must never silently enable OR disable a forensic capture."""
    with pytest.raises(AuditCaptureError, match=CAPTURE_MODE_ENV):
        resolve_capture_mode({CAPTURE_MODE_ENV: junk})


def test_resolve_capture_mode_reads_os_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    """``env=None`` (the default) reads the real process environment."""
    monkeypatch.setenv(CAPTURE_MODE_ENV, "anchor")
    assert resolve_capture_mode() is CaptureMode.ANCHOR
    monkeypatch.delenv(CAPTURE_MODE_ENV)
    assert resolve_capture_mode() is CaptureMode.OFF


# ────────────────────────────────────────────────────────────────────────
# encode_bars / decode_bars / input_blake3 — the canonical encoding
# ────────────────────────────────────────────────────────────────────────


def test_encode_bars_shape_and_order() -> None:
    """One 7-element row per bar, input order, prices as Decimal strings."""
    bars = [
        _bar(0, close="14020.79", volume=1200, open_interest=None),
        _bar(1, close="14031.1", volume=0, open_interest=42),
    ]
    encoded = encode_bars(bars)
    assert encoded == [
        [
            int(bars[0].timestamp.timestamp()),
            "14020.79",
            "14020.79",
            "14020.79",
            "14020.79",
            1200,
            None,
        ],
        [int(bars[1].timestamp.timestamp()), "14031.1", "14031.1", "14031.1", "14031.1", 0, 42],
    ]


def test_encode_bars_empty_batch() -> None:
    """An empty batch encodes to an empty list (the loop-not-entered branch)."""
    assert encode_bars([]) == []


def test_decode_encode_exact_roundtrip() -> None:
    """``decode_bars(encode_bars(x)) == x`` — Decimal / None / UTC exact.

    Includes >2dp vendor-noise prices (``14020.7999`` + the exact ``".xx5"``
    ROUND_HALF_UP boundary ``14031.005``): the capture contract is PRE-snap
    fidelity, so a codec that quantizes to the 2dp tick grid (a
    precision-corrupting decode mutant — numerically EQUAL on 2dp-only
    fixtures) is killed HERE, deterministically (round-4 review fix).
    """
    bars = [
        _bar(0, close="14020.79", volume=7, open_interest=None),
        _bar(3, close="0.05", volume=0, open_interest=123456),
        _bar(5, close="14031.005", volume=3, open_interest=None),
        _bar(6, close="14020.7999", volume=1, open_interest=9),
    ]
    decoded = decode_bars(encode_bars(bars))
    assert decoded == bars
    assert decoded[0].timestamp.tzinfo is UTC
    assert isinstance(decoded[0].close, Decimal)
    # The noise prices survive EXACTLY — no tick-grid snap in the codec
    # (ROUND_HALF_UP would give 14031.01, ROUND_HALF_EVEN 14031.00; both
    # compare UNEQUAL to the raw Decimal, unlike a 2dp fixture).
    assert decoded[2].close == Decimal("14031.005")
    assert decoded[2].close != Decimal("14031.01")
    assert decoded[2].close != Decimal("14031.00")
    assert decoded[3].close == Decimal("14020.7999")


def test_encode_bars_refuses_fno_greeks_bar() -> None:
    """The overlay-loss refusal: an FnoGreeksBar batch fails loud, naming index."""
    bars: list[CandleBar] = [_bar(0), _greeks_bar(1)]
    with pytest.raises(AuditCaptureError, match=r"FnoGreeksBar at index 1"):
        encode_bars(bars)


def test_encode_bars_refuses_sub_second_timestamp() -> None:
    """A sub-second timestamp would silently truncate — refused loudly."""
    price = Decimal("10")
    bad = CandleBar(
        timestamp=_BASE_TS + timedelta(microseconds=250),
        open=price,
        high=price,
        low=price,
        close=price,
        volume=1,
        open_interest=None,
    )
    with pytest.raises(AuditCaptureError, match=r"sub-second bar timestamp at index 0"):
        encode_bars([bad])


def test_decode_bars_rejects_wrong_arity() -> None:
    """A row with the wrong field count is a tampered payload."""
    with pytest.raises(AuditCaptureError, match=r"row 0 has 2 fields, expected 7"):
        decode_bars([[1, "2"]])


@pytest.mark.parametrize(
    ("epoch", "volume"),
    [
        ("1609731900", 10),  # epoch as string
        (True, 10),  # bool masquerading as int
        (1609731900, "10"),  # volume as string
        (1609731900, False),  # volume as bool
    ],
)
def test_decode_bars_rejects_non_int_epoch_or_volume(epoch: object, volume: object) -> None:
    """Epoch / volume must be genuine ints (bool is JSON junk here)."""
    row = [epoch, "10", "10", "10", "10", volume, None]
    with pytest.raises(AuditCaptureError, match=r"epoch/volume must be int"):
        decode_bars([row])


@pytest.mark.parametrize("bad_index", [1, 2, 3, 4])
def test_decode_bars_rejects_non_string_price(bad_index: int) -> None:
    """Each of the four price slots must be a Decimal string."""
    row: list[object] = [1609731900, "10", "10", "10", "10", 1, None]
    row[bad_index] = 10.0
    with pytest.raises(AuditCaptureError, match=r"prices must be Decimal strings"):
        decode_bars([row])


def test_decode_bars_rejects_bad_open_interest() -> None:
    """``open_interest`` must be int or null — a float/bool is junk."""
    row: list[object] = [1609731900, "10", "10", "10", "10", 1, 1.5]
    with pytest.raises(AuditCaptureError, match=r"open_interest must be int or null"):
        decode_bars([row])
    row[6] = True
    with pytest.raises(AuditCaptureError, match=r"open_interest must be int or null"):
        decode_bars([row])


def test_decode_bars_accepts_int_open_interest() -> None:
    """A genuine int OI decodes (the non-None happy branch)."""
    row: list[object] = [1609731900, "10", "10", "10", "10", 1, 99]
    assert decode_bars([row])[0].open_interest == 99


def test_decode_bars_rejects_junk_decimal() -> None:
    """A non-numeric price string dies in Decimal (ArithmeticError branch)."""
    row: list[object] = [1609731900, "not-a-price", "10", "10", "10", 1, None]
    with pytest.raises(AuditCaptureError, match=r"row 0 is not decodable"):
        decode_bars([row])


def test_decode_bars_rejects_ohlc_ordering_violation() -> None:
    """A decoded row violating ``low <= open,close <= high`` fails loud."""
    row: list[object] = [1609731900, "10", "5", "8", "10", 1, None]  # high < low
    with pytest.raises(AuditCaptureError, match=r"row 0 is not decodable"):
        decode_bars([row])


def test_decode_bars_rejects_out_of_range_epoch() -> None:
    """An absurd epoch dies in ``datetime.fromtimestamp`` — still loud."""
    row: list[object] = [2**62, "10", "10", "10", "10", 1, None]
    with pytest.raises(AuditCaptureError, match=r"row 0 is not decodable"):
        decode_bars([row])


def test_input_blake3_matches_canonical_json_digest() -> None:
    """The digest is blake3 over the SAME canonical JSON the chain hashes."""
    encoded = encode_bars([_bar(0), _bar(1, close="101.00")])
    expected = fno_math.blake3_hash(_canonical_json(encoded).encode("utf-8")).hex()
    assert input_blake3(encoded) == expected
    # Any single-bar difference re-keys the digest.
    other = encode_bars([_bar(0), _bar(1, close="101.01")])
    assert input_blake3(other) != expected


# ────────────────────────────────────────────────────────────────────────
# DedupJournal — the L3 seam
# ────────────────────────────────────────────────────────────────────────


def test_dedup_journal_admits_once_in_input_order() -> None:
    """Only newly-admitted keys come back, in input order; re-admit is empty."""
    index = OptionDedupIndex()
    journal = DedupJournal(index)
    assert journal.index is index
    key_a = candle_dedup_key(
        exchange="NSE", segment="INDEX", symbol="NSE-NIFTY", interval="1minute", epoch=1
    )
    key_b = candle_dedup_key(
        exchange="NSE", segment="INDEX", symbol="NSE-NIFTY", interval="1minute", epoch=2
    )
    assert journal.admit([key_a, key_b, key_a]) == [key_a, key_b]
    assert journal.admit([key_b, key_a]) == []


def test_dedup_journal_admit_out_accumulator_is_returned_and_filled() -> None:
    """``out`` is filled in input order and IS the returned list (identity)."""
    journal = DedupJournal(OptionDedupIndex())
    key_a = candle_dedup_key(
        exchange="NSE", segment="INDEX", symbol="NSE-NIFTY", interval="1minute", epoch=1
    )
    key_b = candle_dedup_key(
        exchange="NSE", segment="INDEX", symbol="NSE-NIFTY", interval="1minute", epoch=2
    )
    acc: list[bytes] = []
    returned = journal.admit([key_a, key_b], out=acc)
    assert returned is acc
    assert acc == [key_a, key_b]


def test_dedup_journal_admit_out_holds_prefix_on_mid_iteration_fault() -> None:
    """Round-5 review fix: a mid-loop exact-tier fault re-raises, but ``out``
    already holds every key DURABLY admitted before the fault — the caller's
    audit seam can never under-report a durable admission."""
    journal = DedupJournal(OptionDedupIndex(exact=FaultyExact(2)))
    key_a = candle_dedup_key(
        exchange="NSE", segment="INDEX", symbol="NSE-NIFTY", interval="1minute", epoch=1
    )
    key_b = candle_dedup_key(
        exchange="NSE", segment="INDEX", symbol="NSE-NIFTY", interval="1minute", epoch=2
    )
    acc: list[bytes] = []
    with pytest.raises(OSError, match="exact tier write failed"):
        journal.admit([key_a, key_b], out=acc)
    assert acc == [key_a]


def test_added_keys_digest_is_an_order_insensitive_set_digest() -> None:
    """The digest is over the SORTED hex keys — admission order must not
    matter (round-5 review fix: the sorted() deletion mutant previously
    survived because the only non-trivial fixture was coincidentally already
    in sorted order)."""
    # hex(b"\x02") = "02" > hex(b"\x01") = "01": input order is OUT of
    # sorted order, so an unsorted join ("02\n01") diverges from the
    # sorted recompute ("01\n02") — the mutant-killing shape.
    digest = _added_keys_digest([b"\x02", b"\x01"])
    assert digest == _added_keys_digest([b"\x01", b"\x02"])
    assert digest == fno_math.blake3_hash(b"01\n02").hex()


# ────────────────────────────────────────────────────────────────────────
# AuditCaptureSink — construction + delegation
# ────────────────────────────────────────────────────────────────────────


def test_constructor_refuses_off_mode(tmp_path: Path) -> None:
    """OFF must be the un-wrapped identity path, never a wrapper."""
    inner = RecordingSink(lake_root=tmp_path)
    with pytest.raises(AuditCaptureError, match=r"OFF mode"):
        AuditCaptureSink(inner, lake_root=tmp_path, mode=CaptureMode.OFF)


def test_mode_and_journal_properties(tmp_path: Path) -> None:
    """The mode / journal accessors reflect construction."""
    journal = DedupJournal(OptionDedupIndex())
    sink, _inner = _capture_sink(tmp_path, mode=CaptureMode.CAPTURE, journal=journal)
    assert sink.mode is CaptureMode.CAPTURE
    assert sink.journal is journal
    anchor_sink, _ = _capture_sink(tmp_path, mode=CaptureMode.ANCHOR)
    assert anchor_sink.journal is None


def test_append_delegates_transparently(tmp_path: Path) -> None:
    """Inner sees identical args; caller receives the inner's EXACT result."""
    sink, inner = _capture_sink(tmp_path)
    bars = [_bar(0), _bar(1)]
    result = sink.append(
        exchange="NSE",
        segment="INDEX",
        symbol="NSE-NIFTY",
        interval="1minute",
        bars=bars,
        merge_mode="keep-first",
    )
    assert len(inner.calls) == 1
    call = inner.calls[0]
    assert call["exchange"] == "NSE"
    assert call["segment"] == "INDEX"
    assert call["symbol"] == "NSE-NIFTY"
    assert call["interval"] == "1minute"
    assert call["bars"] == bars
    assert call["merge_mode"] == "keep-first"
    # The identity pin: the very object the inner returned.
    assert isinstance(result, SinkAppendResult)
    assert result.bars_appended == 2


def test_append_result_is_inner_result_object(tmp_path: Path) -> None:
    """The returned result object IS the inner's (never a copy)."""
    canned = SinkAppendResult(bars_appended=1, partitions_written=0, write_results=())
    inner = RecordingSink(lake_root=tmp_path, results=[canned])
    sink = AuditCaptureSink(inner, lake_root=tmp_path, mode=CaptureMode.ANCHOR)
    result = sink.append(
        exchange="NSE", segment="INDEX", symbol="NSE-NIFTY", interval="1minute", bars=[_bar(0)]
    )
    assert result is canned


def test_empty_append_short_circuits_without_event(tmp_path: Path) -> None:
    """An empty batch is the inner's own no-op — no lock, no event, no seq."""
    sink, inner = _capture_sink(tmp_path)
    result = sink.append(
        exchange="NSE", segment="INDEX", symbol="NSE-NIFTY", interval="1minute", bars=[]
    )
    assert result.bars_appended == 0
    assert len(inner.calls) == 1
    assert sink.drain_remaining() == ()
    # The next real append still stamps capture_seq 0 — no seq was consumed.
    sink.append(
        exchange="NSE", segment="INDEX", symbol="NSE-NIFTY", interval="1minute", bars=[_bar(0)]
    )
    (record,) = sink.drain_remaining()
    assert record.payload["capture_seq"] == 0


def test_inner_exception_buffers_nothing(tmp_path: Path) -> None:
    """Capture-after-durable: a failed inner append leaves no record, no seq."""
    sink = AuditCaptureSink(RaisingSink(), lake_root=tmp_path, mode=CaptureMode.ANCHOR)
    with pytest.raises(RuntimeError, match="inner sink exploded"):
        sink.append(
            exchange="NSE", segment="INDEX", symbol="NSE-NIFTY", interval="1minute", bars=[_bar(0)]
        )
    assert sink.drain_remaining() == ()


def test_lock_spans_the_inner_append(tmp_path: Path) -> None:
    """White-box pin: the capture lock is HELD during the durable delegate."""
    probe = LockProbeSink()
    sink = AuditCaptureSink(probe, lake_root=tmp_path, mode=CaptureMode.ANCHOR)
    probe.capture = sink
    sink.append(
        exchange="NSE", segment="INDEX", symbol="NSE-NIFTY", interval="1minute", bars=[_bar(0)]
    )
    assert probe.lock_was_held is True


def test_capture_seq_monotonic_across_pairs(tmp_path: Path) -> None:
    """``capture_seq`` is a single monotonic stamp across all pairs."""
    sink, _inner = _capture_sink(tmp_path)
    for i, symbol in enumerate(["NSE-NIFTY", "NSE-BANKNIFTY", "NSE-NIFTY"]):
        sink.append(
            exchange="NSE", segment="INDEX", symbol=symbol, interval="1minute", bars=[_bar(i)]
        )
    records = sink.drain_remaining()
    assert [r.payload["capture_seq"] for r in records] == [0, 1, 2]


def test_pipe_bearing_symbol_refused_before_durable_write(tmp_path: Path) -> None:
    """A ``"|"`` subject is refused BEFORE the inner append runs."""
    sink, inner = _capture_sink(tmp_path)
    with pytest.raises(AuditCaptureError, match=r"hash-preimage delimiter"):
        sink.append(
            exchange="NSE", segment="INDEX", symbol="BAD|SYM", interval="1minute", bars=[_bar(0)]
        )
    assert inner.calls == []
    assert sink.drain_remaining() == ()


def test_unit_separator_symbol_refused_before_durable_write(tmp_path: Path) -> None:
    """Round-5 review fix: a ``"\\x1f"``-bearing vendor symbol (the dedup-key
    field separator) is refused BEFORE the inner append runs.

    Pre-fix the L3 key derivation ran AFTER the durable write, so the raise
    stranded exactly the un-audited durable append the pre-write validation
    block exists to prevent (and strict replay would later misattribute the
    partition divergence to post-write tampering).
    """
    journal = DedupJournal(OptionDedupIndex())
    sink, inner = _capture_sink(tmp_path, mode=CaptureMode.CAPTURE, journal=journal)
    with pytest.raises(CandleParseError, match="unit separator"):
        sink.append(
            exchange="NSE", segment="CASH", symbol="NIF\x1fTY", interval="1minute", bars=[_bar(0)]
        )
    assert inner.calls == []  # the write never became durable
    assert sink.drain_remaining() == ()


def test_journal_fault_after_durable_write_still_buffers_event_and_add_rows(
    tmp_path: Path,
) -> None:
    """Round-5 review fix: a mid-admit journal fault AFTER the durable inner
    append still buffers the ``LAKE_APPEND_WRITTEN`` event + one ``add`` row
    per key durably admitted before the fault, then re-raises.

    Pre-fix nothing was buffered: the durable write went un-audited and the
    one durably-admitted key became a permanent false L3 ``extra_in_live``
    against the persistent LMDB (the re-pull admits nothing new).
    """
    journal = DedupJournal(OptionDedupIndex(exact=FaultyExact(2)))
    sink, inner = _capture_sink(tmp_path, mode=CaptureMode.CAPTURE, journal=journal)
    bars = [_bar(0), _bar(1)]
    with pytest.raises(OSError, match="exact tier write failed"):
        sink.append(
            exchange="NSE", segment="INDEX", symbol="NSE-NIFTY", interval="1minute", bars=bars
        )
    # The inner append DID run (capture-after-durable for the write itself).
    assert len(inner.calls) == 1
    anchor, add = sink.drain_remaining()
    key0 = candle_dedup_key(
        exchange="NSE",
        segment="INDEX",
        symbol="NSE-NIFTY",
        interval="1minute",
        epoch=int(bars[0].timestamp.timestamp()),
    )
    assert anchor.action == LAKE_APPEND_ACTION
    assert anchor.payload["capture_seq"] == 0
    assert anchor.payload["added_count"] == 1  # ONLY the durably-admitted key
    assert anchor.payload["added_keys_digest"] == fno_math.blake3_hash(key0.hex().encode()).hex()
    assert anchor.payload["bars"] == encode_bars(bars)
    assert add.action == DEDUP_ADD_ACTION
    assert add.subject == key0.hex()


def test_greeks_batch_refused_before_durable_write(tmp_path: Path) -> None:
    """An FnoGreeksBar batch is refused BEFORE the inner append runs."""
    sink, inner = _capture_sink(tmp_path)
    with pytest.raises(AuditCaptureError, match=r"FnoGreeksBar"):
        sink.append(
            exchange="NSE",
            segment="FNO",
            symbol="NIFTY24JAN22000CE",
            interval="1minute",
            bars=[_greeks_bar(0)],
        )
    assert inner.calls == []


@pytest.mark.parametrize(
    ("exchange", "segment", "symbol"),
    [
        ("NSE", "INDEX", "NSE-NIFTY"),
        ("NSE", "CASH", "BAJAJ-AUTO"),
        ("NSE", "FNO", "NIFTY24JAN22000CE"),
        ("BSE", "INDEX", "BSE-SENSEX"),
    ],
)
def test_subject_is_colon_joined_and_pipe_free(
    tmp_path: Path, exchange: str, segment: str, symbol: str
) -> None:
    """The subject is the colon-joined coordinates — pipe-free for real names."""
    sink, _inner = _capture_sink(tmp_path)
    sink.append(
        exchange=exchange, segment=segment, symbol=symbol, interval="1minute", bars=[_bar(0)]
    )
    (record,) = sink.drain_remaining()
    assert record.subject == f"{exchange}:{segment}:{symbol}:1minute"
    assert "|" not in record.subject


# ────────────────────────────────────────────────────────────────────────
# Payload shape — anchor vs capture, journal on/off
# ────────────────────────────────────────────────────────────────────────


def test_anchor_mode_payload_shape(tmp_path: Path) -> None:
    """Anchor mode: full anchors, NO bars, NO journal fields."""
    sink, _inner = _capture_sink(tmp_path)
    bars = [_bar(0, volume=3), _bar(1, volume=4)]
    sink.append(exchange="NSE", segment="INDEX", symbol="NSE-NIFTY", interval="1minute", bars=bars)
    (record,) = sink.drain_remaining()
    assert record.action == LAKE_APPEND_ACTION
    payload = record.payload
    assert payload["v"] == CAPTURE_PAYLOAD_VERSION
    assert payload["capture_run"] == sink.run_nonce
    assert payload["capture_seq"] == 0
    assert payload["exchange"] == "NSE"
    assert payload["segment"] == "INDEX"
    assert payload["symbol"] == "NSE-NIFTY"
    assert payload["interval"] == "1minute"
    assert payload["merge_mode"] == "keep-first"
    assert payload["rows_in"] == 2
    assert payload["bars_net_new"] == 2
    assert payload["input_blake3"] == input_blake3(encode_bars(bars))
    assert payload["partitions"] == [
        {
            "rel": "bars/NSE/INDEX/NSE-NIFTY/1minute/2021/01.parquet",
            "raw_hash": "ab" * 32,
            "row_count": 2,
            "bars_added": 2,
            "first_ts": int(bars[0].timestamp.timestamp()),
            "last_ts": int(bars[1].timestamp.timestamp()),
        }
    ]
    assert "bars" not in payload
    assert "added_keys_digest" not in payload
    assert "added_count" not in payload


def test_capture_run_nonce_is_per_sink_and_stamped_into_every_event(tmp_path: Path) -> None:
    """Two sinks (two 'processes') carry DISTINCT ``capture_run`` nonces.

    Round-1 review fix (capture_seq collision): ``capture_seq`` restarts at 0
    per process while the production chain is resume-appended across pulls,
    so every event must carry its sink's run nonce — the replay-side run
    discriminator.
    """
    sink_a, _inner_a = _capture_sink(tmp_path / "a")
    sink_b, _inner_b = _capture_sink(tmp_path / "b")
    assert sink_a.run_nonce != sink_b.run_nonce
    assert len(sink_a.run_nonce) == 32  # uuid4 hex
    for sink in (sink_a, sink_b):
        sink.append(
            exchange="NSE", segment="INDEX", symbol="NSE-NIFTY", interval="1minute", bars=[_bar(0)]
        )
        sink.append(
            exchange="NSE", segment="INDEX", symbol="NSE-NIFTY", interval="1minute", bars=[_bar(1)]
        )
        records = sink.drain_remaining()
        # Both events of one sink share ITS nonce (one nonce per run).
        assert [r.payload["capture_run"] for r in records] == [sink.run_nonce] * 2


def test_capture_mode_payload_carries_bars_and_journal_fields(tmp_path: Path) -> None:
    """Capture mode + journal: bars tuples, digest, count, and add rows."""
    journal = DedupJournal(OptionDedupIndex())
    sink, _inner = _capture_sink(tmp_path, mode=CaptureMode.CAPTURE, journal=journal)
    bars = [_bar(0), _bar(1)]
    sink.append(exchange="NSE", segment="INDEX", symbol="NSE-NIFTY", interval="1minute", bars=bars)
    records = sink.drain_remaining()
    anchor, *adds = records
    assert anchor.action == LAKE_APPEND_ACTION
    assert anchor.payload["bars"] == encode_bars(bars)
    assert anchor.payload["added_count"] == 2
    expected_keys = [
        candle_dedup_key(
            exchange="NSE",
            segment="INDEX",
            symbol="NSE-NIFTY",
            interval="1minute",
            epoch=int(bar.timestamp.timestamp()),
        )
        for bar in bars
    ]
    joined = "\n".join(sorted(key.hex() for key in expected_keys))
    assert anchor.payload["added_keys_digest"] == fno_math.blake3_hash(joined.encode()).hex()
    assert [a.action for a in adds] == [DEDUP_ADD_ACTION, DEDUP_ADD_ACTION]
    assert [a.subject for a in adds] == [key.hex() for key in expected_keys]
    assert all(a.payload == {} for a in adds)


def test_repull_admits_nothing_and_emits_zero_add_rows(tmp_path: Path) -> None:
    """Idempotence (Rule #7): a re-append of the same bars adds zero rows."""
    journal = DedupJournal(OptionDedupIndex())
    sink, _inner = _capture_sink(tmp_path, mode=CaptureMode.CAPTURE, journal=journal)
    bars = [_bar(0), _bar(1)]
    for _ in range(2):
        sink.append(
            exchange="NSE", segment="INDEX", symbol="NSE-NIFTY", interval="1minute", bars=bars
        )
    records = sink.drain_remaining()
    anchors = [r for r in records if r.action == LAKE_APPEND_ACTION]
    adds = [r for r in records if r.action == DEDUP_ADD_ACTION]
    assert len(anchors) == 2
    assert len(adds) == 2  # first append only
    assert anchors[1].payload["added_count"] == 0
    # The empty admission set still stamps a deterministic digest.
    assert anchors[1].payload["added_keys_digest"] == fno_math.blake3_hash(b"").hex()


def test_added_keys_digest_equal_across_admission_orders_through_the_sink(
    tmp_path: Path,
) -> None:
    """Two sinks admitting the SAME key set in opposite orders stamp the SAME
    ``added_keys_digest`` (the order-insensitivity pin, at event level).

    Minutes 0 and 3 are chosen so admission order is OUT of sorted-hex order
    (pinned by direct computation: key(minute 0) = ``603e83d2…`` >
    key(minute 3) = ``5952a172…``), so an unsorted-join mutant diverges here
    — unlike the coincidentally-ordered 2-key fixture the round-5 review
    caught (round-4 TEETH VERIFIED discipline).
    """
    bars_fwd = [_bar(0), _bar(3)]
    digests: list[str] = []
    for batch in (bars_fwd, list(reversed(bars_fwd))):
        sink, _inner = _capture_sink(
            tmp_path, mode=CaptureMode.CAPTURE, journal=DedupJournal(OptionDedupIndex())
        )
        sink.append(
            exchange="NSE", segment="INDEX", symbol="NSE-NIFTY", interval="1minute", bars=batch
        )
        anchor = sink.drain_remaining()[0]
        digests.append(str(anchor.payload["added_keys_digest"]))
    assert digests[0] == digests[1]
    # ...and both equal the independently-recomputed sorted-join digest.
    hexes = sorted(
        candle_dedup_key(
            exchange="NSE",
            segment="INDEX",
            symbol="NSE-NIFTY",
            interval="1minute",
            epoch=int(bar.timestamp.timestamp()),
        ).hex()
        for bar in bars_fwd
    )
    assert hexes[0] < hexes[1]  # the fixture genuinely exercises the sort
    expected = fno_math.blake3_hash("\n".join(hexes).encode()).hex()
    assert digests[0] == expected


def test_capture_mode_without_journal_has_no_journal_fields(tmp_path: Path) -> None:
    """Capture mode with journal=None: bars present, journal fields absent."""
    sink, _inner = _capture_sink(tmp_path, mode=CaptureMode.CAPTURE, journal=None)
    sink.append(
        exchange="NSE", segment="INDEX", symbol="NSE-NIFTY", interval="1minute", bars=[_bar(0)]
    )
    (record,) = sink.drain_remaining()
    assert "bars" in record.payload
    assert "added_keys_digest" not in record.payload
    assert "added_count" not in record.payload


def test_real_sink_month_straddle_partitions_in_sorted_order(tmp_path: Path) -> None:
    """Against the REAL sink: anchors mirror the sorted per-month WriteResults."""
    lake_root = tmp_path / "lake"
    inner = ParquetBarSink(writer=ParquetWriter(lake_root=lake_root))
    sink = AuditCaptureSink(inner, lake_root=lake_root, mode=CaptureMode.CAPTURE)
    jan = datetime(2021, 1, 29, 6, 0, tzinfo=UTC)
    feb = datetime(2021, 2, 2, 6, 0, tzinfo=UTC)
    price = Decimal("101.25")
    bars = [
        CandleBar(
            timestamp=ts,
            open=price,
            high=price,
            low=price,
            close=price,
            volume=1,
            open_interest=None,
        )
        for ts in (feb, jan)  # deliberately unsorted input
    ]
    result = sink.append(
        exchange="NSE", segment="INDEX", symbol="NSE-NIFTY", interval="1minute", bars=bars
    )
    (record,) = sink.drain_remaining()
    partitions = record.payload["partitions"]
    assert [p["rel"] for p in partitions] == [
        "bars/NSE/INDEX/NSE-NIFTY/1minute/2021/01.parquet",
        "bars/NSE/INDEX/NSE-NIFTY/1minute/2021/02.parquet",
    ]
    for anchor, write_result in zip(partitions, result.write_results, strict=True):
        assert anchor["raw_hash"] == write_result.raw_hash
        assert anchor["row_count"] == write_result.row_count
        assert anchor["bars_added"] == write_result.bars_added
        assert anchor["first_ts"] == int(write_result.first_ts.timestamp())
        assert anchor["last_ts"] == int(write_result.last_ts.timestamp())
        assert (lake_root / anchor["rel"]).is_file()


# ────────────────────────────────────────────────────────────────────────
# Drains — pair-scoped vs remaining
# ────────────────────────────────────────────────────────────────────────


def test_drain_pair_removes_only_that_pair(tmp_path: Path) -> None:
    """A pair drain returns + removes ITS records; other pairs remain."""
    journal = DedupJournal(OptionDedupIndex())
    sink, _inner = _capture_sink(tmp_path, mode=CaptureMode.CAPTURE, journal=journal)
    sink.append(
        exchange="NSE", segment="INDEX", symbol="NSE-NIFTY", interval="1minute", bars=[_bar(0)]
    )
    sink.append(
        exchange="NSE", segment="INDEX", symbol="NSE-BANKNIFTY", interval="1minute", bars=[_bar(0)]
    )
    nifty = sink.drain_pair("NSE-NIFTY", "1minute")
    # Anchor + its add row, both tagged to the pair, in buffer order.
    assert [r.action for r in nifty] == [LAKE_APPEND_ACTION, DEDUP_ADD_ACTION]
    assert nifty[0].payload["symbol"] == "NSE-NIFTY"
    # A second drain of the same pair is empty (records were removed).
    assert sink.drain_pair("NSE-NIFTY", "1minute") == ()
    remaining = sink.drain_remaining()
    assert [r.action for r in remaining] == [LAKE_APPEND_ACTION, DEDUP_ADD_ACTION]
    assert remaining[0].payload["symbol"] == "NSE-BANKNIFTY"
    assert sink.drain_remaining() == ()


def test_drain_pair_distinguishes_interval(tmp_path: Path) -> None:
    """The pair tag is ``(symbol, interval)`` — a different interval stays."""
    sink, _inner = _capture_sink(tmp_path)
    sink.append(
        exchange="NSE", segment="INDEX", symbol="NSE-NIFTY", interval="1minute", bars=[_bar(0)]
    )
    sink.append(
        exchange="NSE", segment="INDEX", symbol="NSE-NIFTY", interval="1day", bars=[_bar(1)]
    )
    drained = sink.drain_pair("NSE-NIFTY", "1minute")
    assert len(drained) == 1
    assert drained[0].payload["interval"] == "1minute"
    leftover = sink.drain_remaining()
    assert len(leftover) == 1
    assert leftover[0].payload["interval"] == "1day"


# ────────────────────────────────────────────────────────────────────────
# resolve_audit_capture — the ONE production seam
# ────────────────────────────────────────────────────────────────────────


def test_resolver_off_returns_identical_unwrapped_sink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The identity pin: OFF returns the VERY sink object, no wrapper."""
    monkeypatch.delenv(CAPTURE_MODE_ENV, raising=False)
    inner = RecordingSink(lake_root=tmp_path)
    resolved, capture = resolve_audit_capture(inner, lake_root=tmp_path)
    assert resolved is inner
    assert capture is None


def test_resolver_anchor_wraps_without_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Anchor mode wraps the sink; both tuple slots are the SAME wrapper."""
    monkeypatch.setenv(CAPTURE_MODE_ENV, "anchor")
    inner = RecordingSink(lake_root=tmp_path)
    resolved, capture = resolve_audit_capture(inner, lake_root=tmp_path)
    assert isinstance(resolved, AuditCaptureSink)
    assert resolved is capture
    assert resolved.mode is CaptureMode.ANCHOR
    assert resolved.journal is None


def test_resolver_capture_in_memory_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Capture mode with ``lmdb_dir=None`` attaches an in-memory journal."""
    monkeypatch.setenv(CAPTURE_MODE_ENV, "capture")
    inner = RecordingSink(lake_root=tmp_path)
    _resolved, capture = resolve_audit_capture(inner, lake_root=tmp_path, lmdb_dir=None)
    assert capture is not None
    assert capture.mode is CaptureMode.CAPTURE
    assert capture.journal is not None
    key = candle_dedup_key(
        exchange="NSE", segment="INDEX", symbol="NSE-NIFTY", interval="1minute", epoch=1
    )
    assert capture.journal.admit([key]) == [key]
    assert capture.journal.admit([key]) == []


def test_resolver_capture_lmdb_journal_is_persistent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Capture mode with a real ``lmdb_dir``: the exact tier persists on disk."""
    monkeypatch.setenv(CAPTURE_MODE_ENV, "capture")
    lmdb_dir = tmp_path / "audit" / "dedup-live.lmdb"
    key = candle_dedup_key(
        exchange="NSE", segment="INDEX", symbol="NSE-NIFTY", interval="1minute", epoch=7
    )
    _resolved, capture = resolve_audit_capture(
        RecordingSink(lake_root=tmp_path), lake_root=tmp_path, lmdb_dir=lmdb_dir
    )
    assert capture is not None
    assert capture.journal is not None
    assert capture.journal.admit([key]) == [key]
    assert lmdb_dir.is_dir()

    from brutex.data.lmdb_membership import LmdbMembership

    exact = capture.journal.index._exact
    assert isinstance(exact, LmdbMembership)
    assert key in exact
    assert frozenset(exact.iter_keys()) == frozenset({key})
    # Close the live handle and re-open fresh: the admitted key PERSISTED.
    exact.close()
    with LmdbMembership.open(lmdb_dir) as membership:
        assert key in membership


def _spy_dedup_index_ctor(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Spy the resolver's ``OptionDedupIndex(**kwargs)`` construction kwargs.

    The resolver imports ``OptionDedupIndex`` function-locally, so patching the
    source-module attribute is exactly what the resolver sees — no white-box
    private-attribute probe needed (round-2 review fix regression seam).
    """
    seen: dict[str, Any] = {}

    class _SpyIndex(OptionDedupIndex):
        def __init__(self, **kwargs: Any) -> None:
            seen.update(kwargs)
            super().__init__(**kwargs)

    monkeypatch.setattr("brutex.data.option_dedup.OptionDedupIndex", _SpyIndex)
    return seen


def test_resolver_capture_journal_bloom_is_right_sized_in_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The in-memory-journal branch sizes the Bloom to the forensic envelope.

    Round-2 review fix (Golden Rule #10): the sweep-scale 100M-key/1e-7
    default would silently allocate a FIXED ~419 MB bit array per capture-mode
    pull regardless of run size; the journal is right-sized to
    ``JOURNAL_BLOOM_CAPACITY``/``JOURNAL_BLOOM_FPR`` (~36 MB) instead, and the
    loud enable WARN discloses the fixed allocation.
    """
    from brutex.data.audit_capture import JOURNAL_BLOOM_CAPACITY, JOURNAL_BLOOM_FPR

    monkeypatch.setenv(CAPTURE_MODE_ENV, "capture")
    seen = _spy_dedup_index_ctor(monkeypatch)
    with capture_logs() as logs:
        _resolved, capture = resolve_audit_capture(
            RecordingSink(lake_root=tmp_path), lake_root=tmp_path, lmdb_dir=None
        )
    assert capture is not None and capture.journal is not None
    assert seen["capacity"] == JOURNAL_BLOOM_CAPACITY == 10_000_000
    assert seen["fpr_target"] == JOURNAL_BLOOM_FPR == 1e-6
    assert "exact" not in seen  # the in-memory default exact tier
    # The enable WARN discloses the fixed journal-Bloom allocation honestly.
    (enabled,) = [e for e in logs if e["event"] == "audit_capture.enabled"]
    assert "~36 MB L3-journal Bloom" in str(enabled["cost"])


def test_resolver_capture_journal_bloom_is_right_sized_lmdb(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The LMDB-journal branch sizes the Bloom to the SAME forensic envelope."""
    from brutex.data.audit_capture import JOURNAL_BLOOM_CAPACITY, JOURNAL_BLOOM_FPR
    from brutex.data.lmdb_membership import LmdbMembership

    monkeypatch.setenv(CAPTURE_MODE_ENV, "capture")
    seen = _spy_dedup_index_ctor(monkeypatch)
    _resolved, capture = resolve_audit_capture(
        RecordingSink(lake_root=tmp_path),
        lake_root=tmp_path,
        lmdb_dir=tmp_path / "audit" / "dedup-live.lmdb",
    )
    assert capture is not None and capture.journal is not None
    assert seen["capacity"] == JOURNAL_BLOOM_CAPACITY
    assert seen["fpr_target"] == JOURNAL_BLOOM_FPR
    assert isinstance(seen["exact"], LmdbMembership)


def test_resolver_junk_env_fails_loud(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A junk env value propagates as AuditCaptureError (never a fallback)."""
    monkeypatch.setenv(CAPTURE_MODE_ENV, "forensics")
    with pytest.raises(AuditCaptureError, match=CAPTURE_MODE_ENV):
        resolve_audit_capture(RecordingSink(lake_root=tmp_path), lake_root=tmp_path)


def test_enable_warn_fires_once_per_process_per_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The loud ``audit_capture.enabled`` WARN is once per process per mode."""
    inner = RecordingSink(lake_root=tmp_path)
    monkeypatch.setenv(CAPTURE_MODE_ENV, "anchor")
    with capture_logs() as logs:
        resolve_audit_capture(inner, lake_root=tmp_path)
        resolve_audit_capture(inner, lake_root=tmp_path)
    enabled = [entry for entry in logs if entry["event"] == "audit_capture.enabled"]
    wired = [entry for entry in logs if entry["event"] == "audit_capture.wired"]
    assert len(enabled) == 1
    assert enabled[0]["mode"] == "anchor"
    assert len(wired) == 2
    # A DIFFERENT mode gets its own single WARN.
    monkeypatch.setenv(CAPTURE_MODE_ENV, "capture")
    with capture_logs() as logs2:
        resolve_audit_capture(inner, lake_root=tmp_path, lmdb_dir=None)
        resolve_audit_capture(inner, lake_root=tmp_path, lmdb_dir=None)
    enabled2 = [entry for entry in logs2 if entry["event"] == "audit_capture.enabled"]
    assert len(enabled2) == 1
    assert enabled2[0]["mode"] == "capture"
    # The reset seam re-arms the guard.
    reset_capture_enable_warnings()
    monkeypatch.setenv(CAPTURE_MODE_ENV, "anchor")
    with capture_logs() as logs3:
        resolve_audit_capture(inner, lake_root=tmp_path)
    assert [e["event"] for e in logs3 if e["event"] == "audit_capture.enabled"] == [
        "audit_capture.enabled"
    ]
