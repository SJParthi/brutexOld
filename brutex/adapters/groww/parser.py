"""Groww candle parser — row-of-arrays payload → :class:`CandleBar` rows.

Groww's ``get_historical_candles`` returns OHLCV as a **list of 7-element rows**
(``docs/groww/api-reference/03-backtesting.md``)::

    {"candles": [[timestamp, open, high, low, close, volume, open_interest], ...]}

The payload is row-oriented (not columnar), so this module iterates rows and
hands each to the shared
:func:`~brutex.data.candle_fields.assemble_candle` (CLAUDE.md §11 COMMON — the
field-level rules live once). The only Groww-specific glue is the **IST timestamp**
rule: Groww returns native-IST strings like ``"2025-09-24T10:30:00"`` (or epoch
seconds defensively), which are localized to IST (fixed +05:30, no DST) then
converted to UTC for storage.

No-trade bars (all OHLC absent/zero) are skipped gracefully, so the result may be
shorter than ``len(candles)`` (DEC-CANDLE-EMPTY-BAR-001).
"""

from __future__ import annotations

import math
import threading
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Final

import structlog

from brutex.adapters.groww.epoch_verdict import classify_epoch_timestamps
from brutex.calendars.nse import IST_FIXED_OFFSET
from brutex.data.candle_fields import (
    CandleParseError,
    EmptyCandleRow,
    assemble_candle,
)
from brutex.data.models import CandleBar
from brutex.observability.correlation import current_pull_id

#: Structured logger — the once-per-pull epoch-timestamp anomaly WARNING only
#: (the parser is otherwise pure vendor decode; per-row events would flood).
_LOG = structlog.get_logger("brutex.adapters.groww.parser")

#: India Standard Time — a FIXED +05:30 offset (no DST), so no tzdata needed.
#: The ONE canonical :data:`brutex.calendars.nse.IST_FIXED_OFFSET` root
#: (DEC-TIMESTAMP-IST-SINGLESOURCE-001).
_IST = IST_FIXED_OFFSET

#: Guards the once-per-pull epoch-timestamp WARNING dedup set below. The parser
#: is called from PARALLEL window workers (both ingest spines fan out), so the
#: check-then-add must be atomic or a burst of epoch rows could warn twice.
_EPOCH_WARN_LOCK: Final[threading.Lock] = threading.Lock()

#: Pull ids that already emitted the epoch-timestamp WARNING — the tiny
#: module-level warn-once guard (DEC-INGEST-BOUNDARY-INTEGRITY-001; repo
#: precedent: the process-global MetricRegistry). Keyed on
#: :func:`~brutex.observability.correlation.current_pull_id` — ``""`` when no
#: pull scope is bound (a direct/offline parse), which still warns (once per
#: process until :func:`reset_epoch_timestamp_warnings`). Grows one 12-char id
#: per pull per process lifetime — O(pulls) memory, honestly bounded by the few
#: pulls a process ever runs.
_EPOCH_WARNED_PULL_IDS: set[str] = set()


def reset_epoch_timestamp_warnings() -> None:
    """Clear the once-per-pull epoch-timestamp WARNING dedup — the explicit reset seam.

    Test isolation (and an operator escape hatch): the module-level guard set
    otherwise persists for the process lifetime, so a test asserting "exactly one
    WARNING per pull" must reset it between cases. Thread-safe, O(1).
    """
    with _EPOCH_WARN_LOCK:
        _EPOCH_WARNED_PULL_IDS.clear()


def _warn_epoch_timestamp_once(raw: object) -> None:
    """Emit ONE ``groww.parse.epoch_timestamp`` WARNING per pull — thread-safe.

    Fired when the DEFENSIVE epoch branch of :func:`parse_timestamp_ist` is taken:
    the vendor sent a NUMERIC timestamp where the documented native-IST string is
    expected. The epoch path assumes **Unix-UTC seconds**; if Groww ever sent
    IST-naive epoch seconds instead, every bar would silently shift by 5h30m — the
    vendor's epoch semantics are UNVERIFIED (no probe transcript), so the branch
    firing at all must be LOUD, but once per pull (a whole epoch payload would
    otherwise spam one WARNING per row). ``pull_id`` is stamped as an EXPLICIT
    field (``structlog.testing.capture_logs`` strips contextvars); an unbound
    scope dedups under the ``""`` key. Never raises; O(1).
    """
    pull_id = current_pull_id()
    with _EPOCH_WARN_LOCK:
        if pull_id in _EPOCH_WARNED_PULL_IDS:
            return
        _EPOCH_WARNED_PULL_IDS.add(pull_id)
    _LOG.warning(
        "groww.parse.epoch_timestamp",
        pull_id=pull_id,
        sample=repr(raw),
        assumed="unix-utc-seconds",
        risk="if the vendor sent IST-naive epochs every bar shifts +05:30 silently",
    )


#: Field count of a FULL Groww candle row
#: ``[ts, open, high, low, close, volume, open_interest]`` (settled historical bars).
_CANDLE_ROW_LEN = 7
#: Field count of the SHORTER row Groww returns for the **current, not-yet-finalised
#: trading day's intraday bars**: ``[ts, open, high, low, close, volume]`` — the
#: trailing ``open_interest`` slot is DROPPED (and ``volume`` is typically ``None``)
#: until the vendor's end-of-day settlement fills them in. Index/spot rows carry null
#: volume/OI regardless, so accepting this shorter shape never loses data — it only
#: stops the still-settling live edge from quarantining the WHOLE symbol's series
#: (DEC-GROWW-FORMING-BAR-PARSE-001).
_CANDLE_ROW_LEN_NO_OI = 6
#: The row lengths the parser accepts: full settled (7) OR current-session-without-OI (6).
_ACCEPTED_ROW_LENS = frozenset({_CANDLE_ROW_LEN_NO_OI, _CANDLE_ROW_LEN})


def parse_timestamp_ist(raw: object) -> datetime:
    """Parse a Groww candle timestamp to a tz-aware UTC datetime.

    Accepts a native-IST string (``"yyyy-MM-ddTHH:mm:ss"`` or with a space
    separator) — localized to IST then converted to UTC — or epoch seconds
    (int/float, **assumed Unix-UTC**). Rejects bools, non-finite, and out-of-range
    values (raises :class:`CandleParseError`), applying the same
    timestamp-validation guards as the shared candle-field rules.

    **Epoch honesty (DEC-INGEST-BOUNDARY-INTEGRITY-001):** the numeric branch is
    DEFENSIVE — Groww documents the native-IST string form; the vendor's epoch
    semantics are UNVERIFIED (no live probe transcript). The branch assumes
    Unix-UTC seconds; if the vendor ever sent IST-naive epoch seconds, every bar
    would silently shift by +05:30. Parsing behaviour is unchanged, but taking
    the branch now emits ONE loud ``groww.parse.epoch_timestamp`` WARNING per
    pull (:func:`_warn_epoch_timestamp_once`) so the assumption can never fire
    silently. On top, :func:`parse_candle_rows` cross-checks a whole epoch
    payload against the real NSE session calendar and emits a per-payload
    ``groww.parse.epoch_timestamp_verdict``
    (:mod:`brutex.adapters.groww.epoch_verdict`,
    DEC-GROWW-EPOCH-VERDICT-001); VERIFYING the semantics needs the live
    probe of ``docs/runbooks/groww-epoch-timestamp-probe.md``.
    """
    if isinstance(raw, bool):  # bool is an int subclass — reject explicitly
        raise CandleParseError(f"candle timestamp must not be bool: {raw!r}")
    if isinstance(raw, str):
        text = raw.strip().replace("T", " ")
        try:
            naive = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
        except ValueError as exc:
            raise CandleParseError(f"unparseable Groww timestamp: {raw!r}") from exc
        try:
            # A pathological out-of-range year (e.g. ``"0001-01-01T00:00:00"``)
            # parses cleanly but raises ``OverflowError("date value out of
            # range")`` when shifted to UTC — the IST→UTC conversion can push a
            # year-0001 datetime below ``datetime.min``. Mirror the epoch branch:
            # map it to a quarantinable ``CandleParseError`` (a ``BruteXError``) so
            # ONE bad vendor row isolates that symbol instead of crashing the whole
            # multi-symbol pull with a raw ``OverflowError`` (run_ingest re-raises
            # any non-``BruteXError``).
            return naive.replace(tzinfo=_IST).astimezone(UTC)
        except (OverflowError, OSError, ValueError) as exc:
            raise CandleParseError(f"out-of-range candle timestamp: {raw!r}") from exc
    if isinstance(raw, (int, float)):
        # LOUD once-per-pull anomaly flag — the vendor sent a numeric timestamp
        # (fires at branch entry: even a non-finite/out-of-range epoch is the
        # same "vendor sent epochs" anomaly worth flagging).
        _warn_epoch_timestamp_once(raw)
        epoch = float(raw)
        if not math.isfinite(epoch):
            raise CandleParseError(f"non-finite candle timestamp: {raw!r}")
        try:
            return datetime.fromtimestamp(epoch, tz=UTC)
        except (OverflowError, OSError, ValueError) as exc:
            raise CandleParseError(f"out-of-range candle timestamp: {raw!r}") from exc
    raise CandleParseError(
        f"candle timestamp must be a string or epoch seconds, got {type(raw).__name__}: {raw!r}"
    )


def parse_candle_rows(
    payload: Mapping[str, object], *, drop_volume: bool = False
) -> list[CandleBar]:
    """Parse a Groww row-of-arrays candle payload into validated rows.

    Parameters
    ----------
    payload
        A mapping carrying a ``"candles"`` key whose value is a list of
        7-element ``[ts, open, high, low, close, volume, open_interest]`` rows
        (settled bars). The current, not-yet-finalised trading day's intraday rows
        may arrive as a 6-element ``[ts, open, high, low, close, volume]`` (no
        ``open_interest`` slot yet) — these are accepted with ``open_interest=None``
        (DEC-GROWW-FORMING-BAR-PARSE-001), never quarantined.
    drop_volume
        When ``True`` the per-row volume is ignored outright (every bar's volume
        → 0). An **index** (NIFTY / BANKNIFTY / SENSEX / INDIA VIX) carries no
        traded volume; the adapter passes ``drop_volume=instrument.is_index``
        (DEC-VENDOR-VOLUME-NORMALIZE-001).

    Returns
    -------
    list[CandleBar]
        One row per non-empty candle, in input order. No-trade bars are skipped.

    Notes
    -----
    A payload carrying NUMERIC timestamps (the defensive epoch branch,
    documented native-IST strings expected) additionally emits ONE
    ``groww.parse.epoch_timestamp_verdict`` WARNING per payload — the
    deterministic session-plausibility cross-check of the UNVERIFIED Unix-UTC
    assumption (DEC-GROWW-EPOCH-VERDICT-001,
    :func:`brutex.adapters.groww.epoch_verdict.classify_epoch_timestamps`).
    OBSERVE-ONLY: the returned bars are byte-identical; a string-timestamp
    payload emits nothing.

    Raises
    ------
    CandleParseError
        If ``"candles"`` is missing/non-array, a row is neither a 7-element nor a
        6-element array, or any non-empty field is unparseable.
    """
    if "candles" not in payload:
        raise CandleParseError("missing required 'candles' key in Groww payload")
    candles = payload["candles"]
    if not isinstance(candles, (list, tuple)):
        raise CandleParseError(f"'candles' must be an array, got {type(candles).__name__}")

    bars: list[CandleBar] = []
    # OHLC-clamp counter (DEC-STORED-DATA-VALIDATION-001): the vendor-OHLC
    # normalization (``assemble_candle``'s high=max/low=min clamp,
    # DEC-OHLC-VENDOR-NORMALIZE-001) used to fire with NO counter and NO log —
    # a vendor systematically under-reporting highs would never surface.
    # OBSERVE-ONLY: parsed bars are byte-identical; ONE
    # ``groww.parse.ohlc_clamped`` WARNING per parsed window payload (this
    # function is called once per fetched window) carries the count.
    clamped_bars = 0

    def _count_clamp() -> None:
        nonlocal clamped_bars
        clamped_bars += 1

    # Negative-count-clamp counter (DEC-AUDIT-ALERT-CLAMP-OBS-001, sibling of
    # the OHLC-clamp counter above): a vendor-emitted NEGATIVE volume /
    # open_interest is clamped to 0 (DEC-VENDOR-VOLUME-NORMALIZE-001) — which
    # used to fire with NO counter and NO log, unlike the OHLC clamp.
    # OBSERVE-ONLY: parsed bars are byte-identical (the clamped value stays 0);
    # ONE ``groww.parse.negative_volume_clamped`` WARNING per parsed window
    # payload carries the count.
    negative_count_clamped_bars = 0

    def _count_negative_count_clamp() -> None:
        nonlocal negative_count_clamped_bars
        negative_count_clamped_bars += 1

    # Epoch-verdict samples (DEC-GROWW-EPOCH-VERDICT-001): raw NUMERIC row
    # timestamps, collected at ROW level (before ``assemble_candle``, so even
    # an all-no-trade epoch payload still yields evidence). Production string
    # payloads pay only the two isinstance checks per row — no allocation.
    epoch_ts_samples: list[object] = []

    for index, row in enumerate(candles):
        if not isinstance(row, (list, tuple)) or len(row) not in _ACCEPTED_ROW_LENS:
            raise CandleParseError(
                f"candle row {index} must be a 7-element array "
                f"[ts, open, high, low, close, volume, open_interest] (settled bars) "
                f"or a 6-element array [ts, open, high, low, close, volume] (the current "
                f"not-yet-finalised session, no open_interest), got {row!r}"
            )
        # The current trading day's intraday bars come back WITHOUT the trailing
        # open_interest slot until the vendor's EOD settlement (DEC-GROWW-FORMING-BAR-
        # PARSE-001) — pad it to None so the live edge parses instead of quarantining
        # the whole series. OI is null for index/spot rows anyway, so no data is lost.
        if len(row) == _CANDLE_ROW_LEN_NO_OI:
            ts_raw, open_, high, low, close, volume = row
            open_interest = None
        else:
            ts_raw, open_, high, low, close, volume, open_interest = row
        if isinstance(ts_raw, (int, float)) and not isinstance(ts_raw, bool):
            epoch_ts_samples.append(ts_raw)
        try:
            bar = assemble_candle(
                ts_raw=ts_raw,
                parse_timestamp=parse_timestamp_ist,
                open_=open_,
                high=high,
                low=low,
                close=close,
                volume=None if drop_volume else volume,
                open_interest=open_interest,
                on_clamp=_count_clamp,
                on_negative_count_clamp=_count_negative_count_clamp,
            )
        except EmptyCandleRow:
            continue  # graceful skip — no-trade minute
        bars.append(bar)
    if clamped_bars:
        _LOG.warning(
            "groww.parse.ohlc_clamped",
            clamped_bars=clamped_bars,
            total_rows=len(candles),
            pull_id=current_pull_id(),
        )
    if negative_count_clamped_bars:
        # ONE WARNING per window payload (mirrors ``ohlc_clamped``): the vendor
        # emitted a NEGATIVE volume / open_interest on this many bars, each
        # clamped to 0. OBSERVE-ONLY (DEC-AUDIT-ALERT-CLAMP-OBS-001) — the
        # stored counts are byte-identical; the WARNING just makes the
        # previously-silent clamp visible.
        _LOG.warning(
            "groww.parse.negative_volume_clamped",
            negative_count_clamped_bars=negative_count_clamped_bars,
            total_rows=len(candles),
            pull_id=current_pull_id(),
        )
    if epoch_ts_samples:
        # Deterministic session-plausibility cross-check of the UNVERIFIED
        # Unix-UTC epoch assumption (DEC-GROWW-EPOCH-VERDICT-001): ONE
        # ``groww.parse.epoch_timestamp_verdict`` WARNING per parsed window
        # payload carrying numeric timestamps (mirrors the ohlc_clamped
        # per-payload precedent). OBSERVE-ONLY — the parsed bars are
        # byte-identical; the verdict is EVIDENCE, not vendor verification
        # (only the runbook's live probe can close the UNVERIFIED gap).
        verdict = classify_epoch_timestamps(epoch_ts_samples)
        _LOG.warning(
            "groww.parse.epoch_timestamp_verdict",
            pull_id=current_pull_id(),
            verdict=verdict.verdict.value,
            supports_unix_utc=verdict.supports_unix_utc,
            supports_ist_naive=verdict.supports_ist_naive,
            ambiguous=verdict.ambiguous,
            out_of_session=verdict.out_of_session,
            abstained=verdict.abstained,
            invalid=verdict.invalid,
            epoch_rows=verdict.total,
            sampled=verdict.sampled,
            assumed="unix-utc-seconds",
            runbook="docs/runbooks/groww-epoch-timestamp-probe.md",
        )
    return bars


__all__ = [
    "CandleParseError",
    "EmptyCandleRow",
    "parse_candle_rows",
    "parse_timestamp_ist",
    "reset_epoch_timestamp_warnings",
]
