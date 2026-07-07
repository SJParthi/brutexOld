"""P-20 property suite — audit→Parquet replay determinism (Hypothesis).

DEC-AUDIT-LAKE-REPLAY-ORACLE-001. The property-level half of the P-20
oracle (the fixture end-to-end lives in
``tests/integration/test_replay_audit_log.py``): for ANY bounded append
schedule — duplicates, keep-first conflicts, month-straddling batches, both
merge modes, arbitrary interleavings across appends — capturing the pull
through :class:`~brutex.data.audit_capture.AuditCaptureSink` and replaying
the hash-chained log through
:func:`~brutex.orchestrator.lake_replay.replay_lake_from_audit` reproduces
the original lake byte-for-byte (same-environment guarantee — P-19's own
envelope).

Also pinned here:

* ``decode_bars(encode_bars(x)) == x`` — the exact canonical-encoding
  round-trip for arbitrary valid bars;
* the L3 idempotence oracle — a re-append of any prefix admits nothing
  (zero new ``add`` rows) and the UNMODIFIED
  :func:`brutex.orchestrator.replay.assert_dedup_consistent` diffs clean
  against the live exact tier, while an injected extra key IS flagged;
* torn-capture self-healing — a write-durable/capture-lost record (the
  crash window between the durable append and the drain) is healed by the
  ordinary re-pull of the SAME batch through the SAME sink: replay byte
  equality is restored, so a PERMANENT divergence provably indicates a
  real bypass.

Bounded + deterministic + offline: ≤4 appends × ≤8 bars over a fixed
15-slot timestamp pool spanning one month boundary, real writers on a
``TemporaryDirectory``, no network, no clock.
"""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.data.audit_capture import (
    DEDUP_ADD_ACTION,
    LAKE_APPEND_ACTION,
    AuditCaptureSink,
    CaptureMode,
    DedupJournal,
    decode_bars,
    encode_bars,
)
from brutex.data.models import CandleBar
from brutex.data.option_dedup import OptionDedupIndex
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.sink import ParquetBarSink
from brutex.orchestrator.audit import AuditWriter, BufferedAuditRecord
from brutex.orchestrator.lake_replay import (
    assert_lake_byte_equal,
    replay_lake_from_audit,
)
from brutex.orchestrator.replay import AuditError, assert_dedup_consistent
from hypothesis import given, settings
from hypothesis import strategies as st

# ────────────────────────────────────────────────────────────────────────
# Bounded generation domain
# ────────────────────────────────────────────────────────────────────────

_JAN_BASE = datetime(2021, 1, 29, 6, 0, tzinfo=UTC)  # mid-day UTC
_FEB_BASE = datetime(2021, 2, 2, 6, 0, tzinfo=UTC)

#: 15 whole-minute slots spanning a (year, month) partition boundary, so a
#: single batch can straddle two Parquet partitions.
_SLOT_TIMESTAMPS: tuple[datetime, ...] = tuple(
    _JAN_BASE + timedelta(minutes=i) for i in range(10)
) + tuple(_FEB_BASE + timedelta(minutes=i) for i in range(5))

_COORDS = {
    "exchange": "NSE",
    "segment": "INDEX",
    "symbol": "NSE-NIFTY",
    "interval": "1minute",
}

#: One bar spec = (slot index, close paise, volume). Same slot + different
#: close across appends is a keep-first CONFLICT; within one batch it is a
#: within-batch vendor duplicate — both replay-order-sensitive on purpose.
_bar_spec = st.tuples(
    st.integers(min_value=0, max_value=len(_SLOT_TIMESTAMPS) - 1),
    st.integers(min_value=1, max_value=9_999_999),
    st.integers(min_value=0, max_value=10_000),
)

_batch = st.lists(_bar_spec, min_size=1, max_size=8)

_schedule = st.lists(_batch, min_size=1, max_size=4)

_merge_modes = st.lists(st.sampled_from(["keep-first", "latest-wins"]), min_size=4, max_size=4)


def _spec_to_bar(spec: tuple[int, int, int]) -> CandleBar:
    slot, close_paise, volume = spec
    price = Decimal(close_paise).scaleb(-2)
    return CandleBar(
        timestamp=_SLOT_TIMESTAMPS[slot],
        open=price,
        high=price,
        low=price,
        close=price,
        volume=volume,
        open_interest=None,
    )


def _make_capture_sink(root: Path, *, journal: DedupJournal | None = None) -> AuditCaptureSink:
    inner = ParquetBarSink(writer=ParquetWriter(lake_root=root / "lake"))
    return AuditCaptureSink(
        inner, lake_root=root / "lake", mode=CaptureMode.CAPTURE, journal=journal
    )


def _append(sink: AuditCaptureSink, specs: list[tuple[int, int, int]], merge_mode: str) -> None:
    sink.append(
        exchange=_COORDS["exchange"],
        segment=_COORDS["segment"],
        symbol=_COORDS["symbol"],
        interval=_COORDS["interval"],
        bars=[_spec_to_bar(spec) for spec in specs],
        merge_mode=merge_mode,
    )


def _write_chain(audit_path: Path, records: list[BufferedAuditRecord]) -> None:
    with AuditWriter.open(audit_path, resume=audit_path.exists()) as writer:
        for record in records:
            writer.append(action=record.action, subject=record.subject, payload=record.payload)


# ────────────────────────────────────────────────────────────────────────
# P-20 — capture → replay is byte-equal for EVERY generated schedule
# ────────────────────────────────────────────────────────────────────────


@given(schedule=_schedule, merge_modes=_merge_modes)
@settings(max_examples=25, deadline=None)
def test_p_20_capture_replay_byte_equal_for_any_schedule(
    schedule: list[list[tuple[int, int, int]]], merge_modes: list[str]
) -> None:
    """P-20: replaying the verified capture log through the REAL writers
    reproduces the original lake byte-for-byte — duplicates, keep-first
    conflicts, month straddles and both merge modes included."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        sink = _make_capture_sink(root)
        for batch, merge_mode in zip(schedule, merge_modes, strict=False):
            _append(sink, batch, merge_mode)
        audit_path = root / "audit.jsonl"
        _write_chain(audit_path, list(sink.drain_remaining()))
        report = replay_lake_from_audit(audit_path, replay_root=root / "replay")
        assert report.appends_replayed == len(schedule)
        # No raise from the per-event anchors, and the full byte oracle holds
        # under strict mode (every live partition was captured).
        assert_lake_byte_equal(
            original_root=root / "lake", replay_root=root / "replay", report=report
        )


# ────────────────────────────────────────────────────────────────────────
# encode/decode — the canonical tuples are an exact bijection
# ────────────────────────────────────────────────────────────────────────

_price_paise = st.integers(min_value=1, max_value=10**9)


@st.composite
def _valid_bars(draw: st.DrawFn) -> list[CandleBar]:
    """Arbitrary VALID whole-second bars (OHLC ordering respected)."""
    count = draw(st.integers(min_value=0, max_value=12))
    bars: list[CandleBar] = []
    for i in range(count):
        prices = sorted(Decimal(p).scaleb(-2) for p in draw(st.tuples(*[_price_paise] * 4)))
        low, open_, close, high = prices[0], prices[1], prices[2], prices[3]
        bars.append(
            CandleBar(
                timestamp=_JAN_BASE + timedelta(minutes=i),
                open=open_,
                high=high,
                low=low,
                close=close,
                volume=draw(st.integers(min_value=0, max_value=10**9)),
                open_interest=draw(st.one_of(st.none(), st.integers(min_value=0, max_value=10**9))),
            )
        )
    return bars


@given(bars=_valid_bars())
@settings(max_examples=200)
def test_decode_encode_is_identity(bars: list[CandleBar]) -> None:
    """``decode_bars(encode_bars(x)) == x`` — Decimal / None / UTC exact."""
    assert decode_bars(encode_bars(bars)) == bars


# ────────────────────────────────────────────────────────────────────────
# L3 — re-append of any prefix admits nothing; the set oracle diffs clean
# ────────────────────────────────────────────────────────────────────────


@given(schedule=_schedule, merge_modes=_merge_modes, data=st.data())
@settings(max_examples=20, deadline=None)
def test_repull_prefix_emits_zero_add_rows_and_oracle_is_clean(
    schedule: list[list[tuple[int, int, int]]],
    merge_modes: list[str],
    data: st.DataObject,
) -> None:
    """A re-append of any prefix of the schedule admits NOTHING (zero new
    ``add`` rows — Rule #7 idempotence), and the unmodified
    ``assert_dedup_consistent`` oracle diffs the audit's ``add`` record
    clean against the live exact tier; one injected extra key IS flagged."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        live_exact: set[bytes] = set()
        journal = DedupJournal(OptionDedupIndex(exact=live_exact))
        sink = _make_capture_sink(root, journal=journal)
        for batch, merge_mode in zip(schedule, merge_modes, strict=False):
            _append(sink, batch, merge_mode)
        first_pass = list(sink.drain_remaining())
        first_adds = [r for r in first_pass if r.action == DEDUP_ADD_ACTION]

        # Re-append an arbitrary prefix — the idempotent re-pull.
        prefix_len = data.draw(st.integers(min_value=1, max_value=len(schedule)))
        for batch, merge_mode in zip(schedule[:prefix_len], merge_modes[:prefix_len], strict=False):
            _append(sink, batch, merge_mode)
        second_pass = list(sink.drain_remaining())
        assert [r.action for r in second_pass].count(DEDUP_ADD_ACTION) == 0
        for record in second_pass:
            if record.action == LAKE_APPEND_ACTION:
                assert record.payload["added_count"] == 0

        # The set oracle: audit `add` subjects == the live exact tier.
        audit_path = root / "audit.jsonl"
        _write_chain(audit_path, first_pass + second_pass)
        live_subjects = frozenset(key.hex() for key in live_exact)
        assert len(live_subjects) == len(first_adds)
        assert_dedup_consistent(audit_path=audit_path, live_subjects=live_subjects)

        # Teeth: one extra live key (an `add` row lost) IS flagged.
        with pytest.raises(AuditError, match="extra_in_live"):
            assert_dedup_consistent(
                audit_path=audit_path, live_subjects=live_subjects | {"ff" * 32}
            )


# ────────────────────────────────────────────────────────────────────────
# Torn capture — write durable, record lost → the re-pull self-heals
# ────────────────────────────────────────────────────────────────────────


@given(schedule=_schedule, merge_modes=_merge_modes)
@settings(max_examples=20, deadline=None)
def test_torn_capture_self_heals_on_repull(
    schedule: list[list[tuple[int, int, int]]], merge_modes: list[str]
) -> None:
    """Drop the LAST append's buffered records before the drain (the durable
    write survived, its capture record did not — the crash window §6 risk 3),
    then re-pull the SAME batch through the SAME sink: the re-capture
    restores replay byte-equality, so a PERMANENT divergence provably
    indicates a real bypass."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        sink = _make_capture_sink(root)
        used_modes = merge_modes[: len(schedule)]
        for batch, merge_mode in zip(schedule, used_modes, strict=True):
            _append(sink, batch, merge_mode)
        records = list(sink.drain_remaining())
        # Simulate the crash: the LAST anchor record (and anything after it)
        # never reached the chain.
        last_anchor_index = max(i for i, r in enumerate(records) if r.action == LAKE_APPEND_ACTION)
        surviving = records[:last_anchor_index]

        # The ordinary re-pull of the lost window: a keep-first no-op on the
        # lake, but a FRESH capture record with the full bars payload.
        _append(sink, schedule[-1], used_modes[-1])
        healed = surviving + list(sink.drain_remaining())

        audit_path = root / "audit.jsonl"
        _write_chain(audit_path, healed)
        report = replay_lake_from_audit(audit_path, replay_root=root / "replay")
        assert report.appends_replayed == len(schedule)  # N-1 originals + 1 re-pull
        assert_lake_byte_equal(
            original_root=root / "lake", replay_root=root / "replay", report=report
        )
