"""Audit→lake replay capture — the P-20 / L3 oracle producer (observe-only).

DEC-AUDIT-LAKE-REPLAY-ORACLE-001. This module is the PRODUCER half of the two
sibling replay oracles:

* **P-20 (audit→Parquet byte replay):** :class:`AuditCaptureSink` wraps any
  :class:`~brutex.data.sink.BarSink` and buffers ONE ``LAKE_APPEND_WRITTEN``
  :class:`~brutex.orchestrator.audit.BufferedAuditRecord` per non-empty
  ``append`` — coordinates + ``merge_mode`` + a monotonic ``capture_seq`` +
  per-partition :class:`~brutex.data.parquet_writer.WriteResult` anchors
  (``raw_hash`` verbatim) + an :func:`input_blake3` digest of the canonically
  encoded input batch, plus (capture mode only) the full 7-field bar tuples
  (:func:`encode_bars`). The replay engine
  (:mod:`brutex.orchestrator.lake_replay`) re-drives the REAL sink→writer
  stack from those records and asserts byte equality.
* **L3 (live-LMDB dedup oracle):** an optional :class:`DedupJournal` (capture
  mode only) admits each input bar's :func:`~brutex.data.option_dedup.
  candle_dedup_key` into a reused
  :class:`~brutex.data.option_dedup.OptionDedupIndex` and buffers one
  ``action="add"`` row per key FIRST admitted — the first real producer for
  the unmodified :func:`brutex.orchestrator.replay.assert_dedup_consistent`
  oracle. Observe-only: the journal never filters what reaches the inner
  sink; the writer's merge remains the sole authority over stored bytes.

**Default-un-wired (clean lakes byte-unaffected).** The ONE env gate is
:data:`CAPTURE_MODE_ENV` (``BRUTEX_AUDIT_REPLAY_CAPTURE``): unset / blank /
``off`` makes :func:`resolve_audit_capture` return the ORIGINAL sink object
un-wrapped (identity) — a production pull today is byte- and event-identical.
Enabling ``anchor`` / ``capture`` logs one loud ``audit_capture.enabled``
structlog WARN per process per mode (silent enablement is impossible).

**Threading (Golden Rule #8 — the chain stays literally single-writer).**
Records are buffered here under the sink's own lock (worker / shard threads)
and appended to the real hash-chained
:class:`~brutex.orchestrator.audit.AuditWriter` ONLY on the controller thread
at the :func:`~brutex.adapters.groww.run_ingest.run_groww_spot_ingest` drain
seams. ONE critical section spans the durable inner append AND the
``capture_seq`` stamp, so stamp order IS the lake-apply order — the
parallel-window same-partition keep-first replay hazard is closed by
construction. Honest cost: while wired (both modes are opt-in), ALL lake
appends serialize through that one lock; OFF mode has no wrapper at all.

**Honest envelope (Golden Rule #10).** Full byte reconstruction is a
CAPTURE-mode guarantee (CI / bounded forensic runs — a full production
backfill would be tens of GB of audit + O(bars) fsyncs, structurally NOT a
production mode). Anchor mode gives per-append tamper-evident verification
(``raw_hash`` + ``input_blake3``), not reconstruction. Greeks-overlay bars
(:class:`~brutex.data.models.FnoGreeksBar`) are REFUSED loudly by
:func:`encode_bars` rather than silently dropping the overlay fields
(lossless overlay encoding is follow-up ``-002``). Capture is O(1)/bar —
one blake3 update + one encoded tuple per bar, same order as the write it
observes; no engine hot path is touched.
"""

from __future__ import annotations

import enum
import os
import threading
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final, TypeGuard

import structlog

from brutex.data.errors import DataLakeError
from brutex.data.models import CandleBar, FnoGreeksBar
from brutex.orchestrator.audit import (
    BufferedAuditRecord,
    _canonical_json,  # pyright: ignore[reportPrivateUsage]
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence
    from pathlib import Path

    from brutex.data.option_dedup import OptionDedupIndex
    from brutex.data.sink import BarSink, SinkAppendResult

_LOG = structlog.get_logger("brutex.data.audit_capture")

#: The ONE env gate: ``"" | "off" | "anchor" | "capture"`` (junk fails loud).
CAPTURE_MODE_ENV: Final[str] = "BRUTEX_AUDIT_REPLAY_CAPTURE"

#: Audit action of the per-append replay event (§1.1 of the design).
LAKE_APPEND_ACTION: Final[str] = "LAKE_APPEND_WRITTEN"

#: Audit action of the L3 dedup-subject event — literally ``"add"``, the
#: :func:`brutex.orchestrator.replay.project_subjects` default, so the
#: existing set oracle is consumed UNMODIFIED.
DEDUP_ADD_ACTION: Final[str] = "add"

#: Schema version stamped into every ``LAKE_APPEND_WRITTEN`` payload.
CAPTURE_PAYLOAD_VERSION: Final[int] = 1

#: Field count of one encoded bar tuple (the canonical 7-field candle).
_ENCODED_FIELDS: Final[int] = 7


class AuditCaptureError(DataLakeError):
    """A loud capture refusal — junk env value, an :class:`FnoGreeksBar`
    batch (silent overlay loss), an un-encodable / un-decodable bar row, or a
    ``"|"``-bearing subject (the audit hash-preimage delimiter)."""


class CaptureMode(enum.Enum):
    """The three :data:`CAPTURE_MODE_ENV` states (§1.4 volume envelope)."""

    OFF = "off"
    ANCHOR = "anchor"
    CAPTURE = "capture"


# ────────────────────────────────────────────────────────────────────────
# Env resolution + the warn-once enable guard
# ────────────────────────────────────────────────────────────────────────

_ENABLE_WARN_LOCK: Final[threading.Lock] = threading.Lock()
_ENABLE_WARNED: set[CaptureMode] = set()


def reset_capture_enable_warnings() -> None:
    """Reset the once-per-process ``audit_capture.enabled`` WARN guard (test seam)."""
    with _ENABLE_WARN_LOCK:
        _ENABLE_WARNED.clear()


def _warn_enabled_once(mode: CaptureMode) -> None:
    """Emit the loud enable WARN once per process per *mode* (never silent)."""
    with _ENABLE_WARN_LOCK:
        if mode in _ENABLE_WARNED:
            return
        _ENABLE_WARNED.add(mode)
    _LOG.warning(
        "audit_capture.enabled",
        mode=mode.value,
        env=CAPTURE_MODE_ENV,
        cost=(
            "audit log grows O(appends) with +1 fsync per drained event on the "
            "controller thread, and ALL lake appends serialize through the "
            "capture lock while wired; 'capture' additionally records full bar "
            "tuples + one 'add' row per net-new bar — a CI/forensic mode, "
            "structurally NOT for a full production backfill"
        ),
    )


def resolve_capture_mode(env: Mapping[str, str] | None = None) -> CaptureMode:
    """Resolve the capture mode from :data:`CAPTURE_MODE_ENV` — fail-loud.

    Parameters
    ----------
    env
        Environment mapping to read (``None`` → ``os.environ``; test seam).

    Returns
    -------
    CaptureMode
        Unset / blank / ``"off"`` → :attr:`CaptureMode.OFF`; ``"anchor"`` /
        ``"capture"`` (case- and whitespace-insensitive) → the matching mode.

    Raises
    ------
    AuditCaptureError
        On any other value — junk config must never silently disable (or
        silently enable) a forensic capture (Golden Rule #1).
    """
    source: Mapping[str, str] = os.environ if env is None else env
    raw = source.get(CAPTURE_MODE_ENV)
    if raw is None or not raw.strip():
        return CaptureMode.OFF
    normalized = raw.strip().lower()
    for mode in CaptureMode:
        if normalized == mode.value:
            return mode
    raise AuditCaptureError(
        f"{CAPTURE_MODE_ENV} must be one of 'off' / 'anchor' / 'capture' (or unset), got {raw!r}"
    )


# ────────────────────────────────────────────────────────────────────────
# Canonical bar encoding — the exact-round-trip capture payload
# ────────────────────────────────────────────────────────────────────────


def _strict_int(value: object) -> TypeGuard[int]:
    """True iff *value* is a genuine ``int`` (``bool`` rejected — JSON junk)."""
    return isinstance(value, int) and not isinstance(value, bool)


def encode_bars(bars: Sequence[CandleBar]) -> list[list[object]]:
    """Encode *bars* as canonical 7-element JSON-safe tuples (input order).

    One ``[epoch_s:int, open:str, high:str, low:str, close:str, volume:int,
    oi:int|None]`` array per bar. Prices are ``str(Decimal)`` — exact
    round-trip, never JSON floats, so the audit writer's ``allow_nan=False``
    canonical JSON can never trip. The bars are the PRE-write input (post
    session-filter, pre ``_tick_round``); replay re-applies the writer's own
    rounding, so this is a faithful input record, not a re-derivation.

    Raises
    ------
    AuditCaptureError
        On an :class:`~brutex.data.models.FnoGreeksBar` (its ten overlay
        fields would be silently LOST by the 7-field encoding — fail loud;
        lossless overlay encoding is follow-up ``-002``), or on a bar whose
        timestamp is not whole-second UTC-representable (a sub-second
        timestamp would silently truncate and break the byte round-trip).
    """
    encoded: list[list[object]] = []
    for index, bar in enumerate(bars):
        if isinstance(bar, FnoGreeksBar):
            raise AuditCaptureError(
                f"encode_bars refuses FnoGreeksBar at index {index}: the 7-field "
                "capture encoding would silently drop the Greeks overlay fields "
                "(lossless overlay encoding is DEC-AUDIT-LAKE-REPLAY-ORACLE-002)"
            )
        if bar.timestamp.microsecond != 0:
            raise AuditCaptureError(
                f"encode_bars refuses a sub-second bar timestamp at index {index} "
                f"({bar.timestamp.isoformat()}): the epoch-second encoding would "
                "silently truncate it and break the replay byte round-trip"
            )
        encoded.append(
            [
                int(bar.timestamp.timestamp()),
                str(bar.open),
                str(bar.high),
                str(bar.low),
                str(bar.close),
                bar.volume,
                bar.open_interest,
            ]
        )
    return encoded


def decode_bars(rows: Sequence[Sequence[object]]) -> list[CandleBar]:
    """Decode :func:`encode_bars` output back to bars — the exact inverse.

    ``decode_bars(encode_bars(x)) == x`` for every valid spot batch (Decimal /
    ``None`` / UTC round-trip; timestamps come back as UTC-aware datetimes at
    the same instant).

    Raises
    ------
    AuditCaptureError
        Loudly on any junk row — wrong arity, a non-int epoch/volume, a
        non-string price, a ``bool`` masquerading as an int, or values a
        :class:`~brutex.data.models.CandleBar` refuses (a tampered payload
        must never decode silently).
    """
    bars: list[CandleBar] = []
    for index, row in enumerate(rows):
        if len(row) != _ENCODED_FIELDS:
            raise AuditCaptureError(
                f"encoded bar row {index} has {len(row)} fields, expected {_ENCODED_FIELDS}"
            )
        epoch, open_, high, low, close, volume, open_interest = row
        if not _strict_int(epoch) or not _strict_int(volume):
            raise AuditCaptureError(
                f"encoded bar row {index}: epoch/volume must be int, got {epoch!r} / {volume!r}"
            )
        if (
            not isinstance(open_, str)
            or not isinstance(high, str)
            or not isinstance(low, str)
            or not isinstance(close, str)
        ):
            raise AuditCaptureError(
                f"encoded bar row {index}: prices must be Decimal strings, got "
                f"{open_!r} / {high!r} / {low!r} / {close!r}"
            )
        oi: int | None
        if open_interest is None:
            oi = None
        elif _strict_int(open_interest):
            oi = open_interest
        else:
            raise AuditCaptureError(
                f"encoded bar row {index}: open_interest must be int or null, got {open_interest!r}"
            )
        try:
            bar = CandleBar(
                timestamp=datetime.fromtimestamp(epoch, tz=UTC),
                open=Decimal(open_),
                high=Decimal(high),
                low=Decimal(low),
                close=Decimal(close),
                volume=volume,
                open_interest=oi,
            )
        except (ArithmeticError, OSError, OverflowError, ValueError) as exc:
            raise AuditCaptureError(f"encoded bar row {index} is not decodable: {exc}") from exc
        bars.append(bar)
    return bars


def input_blake3(encoded: list[list[object]]) -> str:
    """Return the blake3 hex digest of the canonical JSON of *encoded*.

    The per-event input anchor (``input_blake3`` in the
    ``LAKE_APPEND_WRITTEN`` payload): any single-bar difference in the input
    batch re-keys it. Uses the SAME ``_canonical_json`` the audit chain hashes
    with, so the digest and the stored payload can never disagree on encoding.
    ``fno_math`` is lazy-imported (the wheel-free import discipline).
    """
    import fno_math

    payload = _canonical_json(encoded)
    digest: bytes = fno_math.blake3_hash(payload.encode("utf-8"))
    return digest.hex()


def _added_keys_digest(admitted: Sequence[bytes]) -> str:
    """blake3 hex over the ``\\n``-joined SORTED hex keys this append admitted.

    The zero-extra-fsync batch-granularity set-divergence breadcrumb riding
    the ``LAKE_APPEND_WRITTEN`` event (present iff a journal is attached).
    """
    import fno_math

    joined = "\n".join(sorted(key.hex() for key in admitted))
    digest: bytes = fno_math.blake3_hash(joined.encode("utf-8"))
    return digest.hex()


def _capture_subject(exchange: str, segment: str, symbol: str, interval: str) -> str:
    """Build the colon-joined event subject — ``"|"``-free or fail loud.

    Real coordinates (NSE/BSE · INDEX/CASH/FNO · ``-``-separated symbols)
    never carry ``"|"``; the guard turns an impossible-but-catastrophic
    :class:`~brutex.orchestrator.audit.AuditFieldError` at drain time into a
    loud refusal BEFORE any durable write escapes capture.
    """
    subject = f"{exchange}:{segment}:{symbol}:{interval}"
    if "|" in subject:
        raise AuditCaptureError(
            f"audit capture subject must not contain the '|' hash-preimage delimiter: {subject!r}"
        )
    return subject


# ────────────────────────────────────────────────────────────────────────
# DedupJournal — the L3 producer seam (observe-only)
# ────────────────────────────────────────────────────────────────────────


class DedupJournal:
    """Observe-only admitted-key journal over a reused
    :class:`~brutex.data.option_dedup.OptionDedupIndex`.

    The L3 seam (§3 of the design): :meth:`admit` records which keys were
    FIRST admitted in this journal's lifetime — a re-pull admits nothing →
    zero ``add`` rows (idempotence, CLAUDE.md §2 Rule 7, is exactly what the
    :func:`brutex.orchestrator.replay.assert_dedup_consistent` oracle then
    proves). It never filters what reaches the wrapped sink.
    """

    def __init__(self, index: OptionDedupIndex) -> None:
        self._index = index

    @property
    def index(self) -> OptionDedupIndex:
        """The wrapped dedup index (Bloom L1 + injected exact tier)."""
        return self._index

    def admit(self, keys: Iterable[bytes]) -> list[bytes]:
        """Admit *keys*; return ONLY the newly-admitted ones, in input order.

        Delegates to the index's atomic
        :meth:`~brutex.data.option_dedup.OptionDedupIndex.add_if_new`, so a
        key is admitted exactly once even under concurrent shard workers.
        """
        return [key for key in keys if self._index.add_if_new(key)]


# ────────────────────────────────────────────────────────────────────────
# AuditCaptureSink — the observe-only BarSink wrapper
# ────────────────────────────────────────────────────────────────────────


class AuditCaptureSink:
    """A :class:`~brutex.data.sink.BarSink` wrapper buffering replay records.

    Implements ``BarSink`` structurally and delegates transparently: the
    inner sink sees identical arguments and the caller receives the inner's
    EXACT :class:`~brutex.data.sink.SinkAppendResult` object. ONE lock spans
    the durable inner append AND the ``capture_seq`` stamp + buffer, so the
    stamp order IS the lake-apply order (the parallel-window same-partition
    keep-first hazard is closed structurally). An inner-append exception
    buffers nothing (capture-after-durable); an empty append short-circuits
    to the inner's own no-op with no lock and no event.

    Records are pair-tagged ``(symbol, interval)`` so the controller-thread
    drain seams (:meth:`drain_pair` / :meth:`drain_remaining`) can flush a
    finished pair's records onto the single-writer audit chain; a drained
    record is REMOVED from the buffer (capture memory is bounded to
    undrained pairs).

    Parameters
    ----------
    inner
        The wrapped sink (any :class:`~brutex.data.sink.BarSink`).
    lake_root
        The lake root the inner sink writes under — used to relativize each
        :class:`~brutex.data.parquet_writer.WriteResult` path into the
        portable ``rel`` anchor field.
    mode
        :attr:`CaptureMode.ANCHOR` or :attr:`CaptureMode.CAPTURE`;
        constructing in :attr:`CaptureMode.OFF` is refused loudly —
        :func:`resolve_audit_capture` returns the sink UN-wrapped instead.
    journal
        Optional :class:`DedupJournal` (capture mode); when attached, every
        append additionally buffers one ``action="add"`` row per newly
        admitted :func:`~brutex.data.option_dedup.candle_dedup_key` and
        stamps ``added_keys_digest`` / ``added_count`` into the event.
    """

    def __init__(
        self,
        inner: BarSink,
        *,
        lake_root: Path,
        mode: CaptureMode,
        journal: DedupJournal | None = None,
    ) -> None:
        if mode is CaptureMode.OFF:
            raise AuditCaptureError(
                "AuditCaptureSink must not be constructed in OFF mode — "
                "resolve_audit_capture returns the sink un-wrapped instead"
            )
        self._inner = inner
        self._lake_root = lake_root
        self._mode = mode
        self._journal = journal
        #: ONE critical section spans the durable delegate AND the stamp —
        #: white-box-probeable by tests (a probe inner sink asserts the lock
        #: is held during its own ``append``).
        self._lock = threading.Lock()
        self._next_seq = 0
        self._buffer: list[tuple[tuple[str, str], BufferedAuditRecord]] = []

    @property
    def mode(self) -> CaptureMode:
        """The active capture mode (never :attr:`CaptureMode.OFF`)."""
        return self._mode

    @property
    def journal(self) -> DedupJournal | None:
        """The attached L3 journal, or ``None`` in anchor mode."""
        return self._journal

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
        """Delegate to the inner sink, then buffer ONE replay event (locked).

        The empty batch short-circuits to the inner's own empty no-op —
        no lock, no event (the producer never emits an empty ``bars`` array,
        which is what lets replay treat one as tampering).
        """
        if not bars:
            return self._inner.append(
                exchange=exchange,
                segment=segment,
                symbol=symbol,
                interval=interval,
                bars=bars,
                merge_mode=merge_mode,
            )
        # Fail-loud input validation BEFORE the durable write: a batch the
        # capture cannot faithfully record (Greeks overlay / '|' subject) must
        # never become an un-audited durable append — that would be structural
        # replay incompleteness, worse than a refused write.
        subject = _capture_subject(exchange, segment, symbol, interval)
        encoded = encode_bars(bars)
        batch_digest = input_blake3(encoded)
        with self._lock:
            result = self._inner.append(
                exchange=exchange,
                segment=segment,
                symbol=symbol,
                interval=interval,
                bars=bars,
                merge_mode=merge_mode,
            )
            seq = self._next_seq
            self._next_seq += 1
            payload: dict[str, Any] = {
                "v": CAPTURE_PAYLOAD_VERSION,
                "capture_seq": seq,
                "exchange": exchange,
                "segment": segment,
                "symbol": symbol,
                "interval": interval,
                "merge_mode": merge_mode,
                "rows_in": len(bars),
                "bars_net_new": result.bars_net_new,
                "input_blake3": batch_digest,
                "partitions": [
                    {
                        "rel": wr.path.relative_to(self._lake_root).as_posix(),
                        "raw_hash": wr.raw_hash,
                        "row_count": wr.row_count,
                        "bars_added": wr.bars_added,
                        "first_ts": int(wr.first_ts.timestamp()),
                        "last_ts": int(wr.last_ts.timestamp()),
                    }
                    for wr in result.write_results
                ],
            }
            add_records: list[BufferedAuditRecord] = []
            if self._journal is not None:
                admitted = self._journal.admit(
                    self._dedup_keys(
                        exchange=exchange,
                        segment=segment,
                        symbol=symbol,
                        interval=interval,
                        bars=bars,
                    )
                )
                payload["added_keys_digest"] = _added_keys_digest(admitted)
                payload["added_count"] = len(admitted)
                add_records = [
                    BufferedAuditRecord(action=DEDUP_ADD_ACTION, subject=key.hex(), payload={})
                    for key in admitted
                ]
            if self._mode is CaptureMode.CAPTURE:
                payload["bars"] = encoded
            tag = (symbol, interval)
            self._buffer.append(
                (
                    tag,
                    BufferedAuditRecord(
                        action=LAKE_APPEND_ACTION, subject=subject, payload=payload
                    ),
                )
            )
            self._buffer.extend((tag, record) for record in add_records)
        return result

    @staticmethod
    def _dedup_keys(
        *,
        exchange: str,
        segment: str,
        symbol: str,
        interval: str,
        bars: Sequence[CandleBar],
    ) -> list[bytes]:
        """Derive one L3 dedup key per input bar (lazy ``option_dedup`` import)."""
        from brutex.data.option_dedup import candle_dedup_key

        return [
            candle_dedup_key(
                exchange=exchange,
                segment=segment,
                symbol=symbol,
                interval=interval,
                epoch=int(bar.timestamp.timestamp()),
            )
            for bar in bars
        ]

    def drain_pair(self, symbol: str, interval: str) -> tuple[BufferedAuditRecord, ...]:
        """Remove + return this pair's buffered records, in buffer order.

        Called on the controller thread after the pair's ``INGEST_PAIR_DONE``
        / ``INGEST_PAIR_FAILED`` audit line (a quarantined pair's
        already-durable windows MUST still be audited or replay is
        structurally incomplete).
        """
        tag = (symbol, interval)
        with self._lock:
            drained = tuple(record for record_tag, record in self._buffer if record_tag == tag)
            self._buffer = [
                (record_tag, record) for record_tag, record in self._buffer if record_tag != tag
            ]
        return drained

    def drain_remaining(self) -> tuple[BufferedAuditRecord, ...]:
        """Remove + return ALL still-buffered records (the final run drain)."""
        with self._lock:
            drained = tuple(record for _tag, record in self._buffer)
            self._buffer = []
        return drained


# ────────────────────────────────────────────────────────────────────────
# resolve_audit_capture — the ONE production wiring seam
# ────────────────────────────────────────────────────────────────────────


def resolve_audit_capture(
    sink: BarSink,
    *,
    lake_root: Path,
    lmdb_dir: Path | None = None,
) -> tuple[BarSink, AuditCaptureSink | None]:
    """Resolve the env gate and (maybe) wrap *sink* for replay capture.

    The ONE seam a production build site calls immediately BEFORE handing the
    sink to the write pool (so pool-routed shard appends route THROUGH the
    wrapper).

    Parameters
    ----------
    sink
        The base sink to (maybe) wrap.
    lake_root
        The lake root *sink* writes under (anchors are relativized to it).
    lmdb_dir
        Capture mode's persistent exact-tier directory for the L3 journal's
        LMDB set (``None`` → an in-memory exact tier; tests / ephemeral runs).

    Returns
    -------
    tuple[BarSink, AuditCaptureSink | None]
        OFF → ``(sink, None)`` — the IDENTICAL un-wrapped object (the
        identity pin: the default pull path has no wrapper at all).
        ANCHOR → the wrapped sink, no journal. CAPTURE → the wrapped sink
        with a :class:`DedupJournal` over
        ``OptionDedupIndex(exact=LmdbMembership.open(lmdb_dir))``.

    Raises
    ------
    AuditCaptureError
        On a junk :data:`CAPTURE_MODE_ENV` value (fail-loud, never a silent
        fallback).
    """
    mode = resolve_capture_mode()
    if mode is CaptureMode.OFF:
        return (sink, None)
    _warn_enabled_once(mode)
    journal: DedupJournal | None = None
    if mode is CaptureMode.CAPTURE:
        from brutex.data.option_dedup import OptionDedupIndex

        if lmdb_dir is None:
            index = OptionDedupIndex()
        else:
            from brutex.data.lmdb_membership import LmdbMembership

            index = OptionDedupIndex(exact=LmdbMembership.open(lmdb_dir))
        journal = DedupJournal(index)
    capture = AuditCaptureSink(sink, lake_root=lake_root, mode=mode, journal=journal)
    _LOG.info(
        "audit_capture.wired",
        mode=mode.value,
        lake_root=str(lake_root),
        journal=journal is not None,
    )
    return (capture, capture)


__all__ = [
    "CAPTURE_MODE_ENV",
    "CAPTURE_PAYLOAD_VERSION",
    "DEDUP_ADD_ACTION",
    "LAKE_APPEND_ACTION",
    "AuditCaptureError",
    "AuditCaptureSink",
    "CaptureMode",
    "DedupJournal",
    "decode_bars",
    "encode_bars",
    "input_blake3",
    "reset_capture_enable_warnings",
    "resolve_audit_capture",
    "resolve_capture_mode",
]
