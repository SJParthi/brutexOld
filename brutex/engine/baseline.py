"""Buy-&-hold baseline series for the results-depth pass (R27).

DEC-SWEEP-RESULTS-DEPTH-001 (WP2). R27's plain-English card compares every
combo's net P&L against a buy-&-hold baseline computed **from the same bars
the sweep loaded** — never a vendor quote, never an invented figure. This
module owns that series:

* :func:`compute_baseline` — anchor = the FIRST session's FIRST bar OPEN;
  one point per IST session = that session's LAST bar CLOSE;
  ``total_return_pct`` = ``(last_close − anchor) / anchor × 100`` at FULL
  float precision (a statistical figure, never rounded — CLAUDE.md §4);
  prices stay exact 2-dp Decimal rupees (the lake's own tick grid).
* :func:`write_baseline` / :func:`load_baseline` — the deterministic JSON
  artifact at ``<sweep_runs root>/<run_id>/baseline-<instrument>.json``
  (design B8 as amended by gap-register Ruling 4 — the per-run directory
  comes from the ONE :func:`brutex.engine.trade_store.sweep_run_dir` seam,
  a SIBLING root beside the sweeps tree): sorted keys, compact separators,
  ASCII, ``allow_nan=False`` (a non-finite figure is refused loudly, never
  serialized as the non-JSON ``NaN`` token), trailing newline — the bytes
  are a pure function of the series + the stamped ``data_snapshot_hash``
  (traceability: the artifact names the exact data identity it was computed
  from). ANY damage on load — missing file, torn JSON, wrong schema version,
  a malformed field — returns ``None`` (one ``baseline.load_invalid``
  warning; the caller rebuilds deterministically — R42's mid-chain break
  law, the ``load_stamp`` idiom).

Wheel-free stdlib leaf (design F.10): consumes already-loaded
:class:`~brutex.data.models.CandleBar` sequences; the only sibling imports are
the COMMON money snap, the IST session key and the atomic writer.

## Determinism / honesty

* Input bars may arrive in ANY load order — the series is computed over the
  timestamp-sorted view, so permuted loads reproduce identical output
  (property-pinned). DUPLICATE timestamps are refused (two different bars on
  one instant cannot be ordered deterministically; the lake read gate dedups
  upstream — silent keep-one here would hide poison).
* A non-positive anchor price is refused (corrupt stored bar — the baseline
  never divides by junk).
* One O(N_bars log N_bars) pass per (instrument, window), run once at
  run-end; reads are O(1) small-file loads.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from math import isfinite
from typing import TYPE_CHECKING, Final

import structlog

from brutex.data.atomic_io import atomic_write_simple
from brutex.engine.session_lens import ist_trade_date
from brutex.models import BruteXError
from brutex.money import round_money

if TYPE_CHECKING:  # pragma: no cover - import only for static typing
    from collections.abc import Sequence
    from pathlib import Path

    from brutex.data.models import CandleBar

_LOG = structlog.get_logger("brutex.engine.baseline")

__all__ = [
    "BASELINE_SCHEMA_VERSION",
    "BaselineError",
    "BaselineSeries",
    "baseline_filename",
    "compute_baseline",
    "load_baseline",
    "write_baseline",
]

#: Baseline artifact schema version — bump on ANY shape change (a reader
#: seeing an unknown version treats the artifact as absent → rebuild).
BASELINE_SCHEMA_VERSION: Final[int] = 1

#: The B8 payload keys, pinned (load validates the EXACT set — an extra or
#: missing key is damage, never silently tolerated).
_PAYLOAD_KEYS: Final[frozenset[str]] = frozenset(
    {
        "schema_version",
        "instrument",
        "start",
        "end",
        "anchor_open",
        "sessions",
        "total_return_pct",
        "data_snapshot_hash",
        "source_note",
    }
)

#: A serialized session entry is exactly ``[iso_date, price_str]``.
_SESSION_FIELDS: Final[int] = 2


class BaselineError(BruteXError):
    """Raised on malformed baseline input (empty/duplicate bars, bad prices)."""


@dataclass(frozen=True, slots=True)
class BaselineSeries:
    """One instrument's buy-&-hold baseline over a sweep window (R27).

    ``anchor_open`` is the first session's first bar OPEN (the buy instant);
    ``sessions`` is one ``(session_date, last_bar_close)`` pair per IST
    session, strictly ascending; ``total_return_pct`` is the full-precision
    percentage return of holding from the anchor to the LAST session's close;
    ``source_note`` states the computed source in operator English (R27
    traceability — every displayed number names where it came from).
    """

    instrument: str
    start: date
    end: date
    anchor_open: Decimal
    sessions: tuple[tuple[date, Decimal], ...]
    total_return_pct: float
    source_note: str

    def __post_init__(self) -> None:
        if not self.instrument:
            raise BaselineError("BaselineSeries.instrument must be non-empty")
        if not self.sessions:
            raise BaselineError("BaselineSeries.sessions must be non-empty")
        if any(
            self.sessions[i - 1][0] >= self.sessions[i][0] for i in range(1, len(self.sessions))
        ):
            raise BaselineError("BaselineSeries.sessions must be strictly ascending by date")
        if self.start != self.sessions[0][0] or self.end != self.sessions[-1][0]:
            raise BaselineError(
                "BaselineSeries start/end must equal the first/last session date, got "
                f"start={self.start} end={self.end} vs sessions "
                f"[{self.sessions[0][0]} … {self.sessions[-1][0]}]"
            )
        if self.anchor_open <= 0:
            raise BaselineError(f"BaselineSeries.anchor_open must be > 0, got {self.anchor_open}")
        if not isfinite(self.total_return_pct):
            raise BaselineError(
                f"BaselineSeries.total_return_pct must be finite, got {self.total_return_pct!r}"
            )


def baseline_filename(instrument: str) -> str:
    """The pinned artifact filename — ``baseline-<instrument>.json`` (design D0b)."""
    if not instrument:
        raise BaselineError("baseline_filename: instrument must be non-empty")
    return f"baseline-{instrument}.json"


def compute_baseline(bars: Sequence[CandleBar], *, instrument: str) -> BaselineSeries:
    """Compute the buy-&-hold baseline from the sweep's OWN bars (R27).

    Parameters
    ----------
    bars
        The (instrument, window) bars the sweep itself loaded — ANY load
        order (sorted internally by timestamp; duplicate timestamps refused).
    instrument
        The instrument token, recorded on the series.

    Returns
    -------
    BaselineSeries
        Anchor + per-session closes + the full-precision total return.

    Raises
    ------
    BaselineError
        On empty *bars*, a duplicate timestamp, or a non-positive anchor OPEN.
    """
    if not bars:
        raise BaselineError(f"compute_baseline: no bars for {instrument!r} — nothing to anchor")
    ordered = sorted(bars, key=lambda bar: bar.timestamp)
    for i in range(1, len(ordered)):
        if ordered[i - 1].timestamp == ordered[i].timestamp:
            raise BaselineError(
                f"compute_baseline: duplicate bar timestamp {ordered[i].timestamp!r} — "
                "dedup at the lake read boundary before baselining"
            )
    anchor = ordered[0].open
    if anchor <= 0:
        raise BaselineError(
            f"compute_baseline: first bar OPEN must be > 0, got {anchor} (corrupt stored bar)"
        )
    closes: dict[date, Decimal] = {}
    for bar in ordered:
        closes[ist_trade_date(bar.timestamp)] = bar.close
    sessions = tuple(sorted(closes.items()))
    last_close = sessions[-1][1]
    total_return_pct = float((last_close - anchor) / anchor * 100)
    return BaselineSeries(
        instrument=instrument,
        start=sessions[0][0],
        end=sessions[-1][0],
        anchor_open=anchor,
        sessions=sessions,
        total_return_pct=total_return_pct,
        source_note=(
            f"buy-&-hold {instrument}: anchor = first bar OPEN of the first session; "
            f"one point per IST session = that session's LAST bar CLOSE; computed from "
            f"the sweep's own {len(ordered)} bars over {len(sessions)} session(s) "
            "(R27 same-bars law)"
        ),
    )


def _price_str(value: Decimal) -> str:
    """Serialize a rupee price as its 2-dp tick-grid string (B8 law)."""
    return str(round_money(value))


def write_baseline(path: Path, series: BaselineSeries, *, data_snapshot_hash: str) -> None:
    """Write the deterministic B8 JSON artifact (atomic, trailing newline).

    ``data_snapshot_hash`` stamps the EXACT data identity the series was
    computed from (the sweep's own ``bxsnap2`` digest) — the artifact is
    traceable to its inputs byte-for-byte.

    Raises
    ------
    BaselineError
        On an empty *data_snapshot_hash* (an unstamped artifact would be
        untraceable — refused).
    OSError
        Raw from the atomic write (the engine sidecar error contract —
        never mis-escalated to a lake halt).
    """
    if not data_snapshot_hash:
        raise BaselineError("write_baseline: data_snapshot_hash must be non-empty")
    payload = {
        "schema_version": BASELINE_SCHEMA_VERSION,
        "instrument": series.instrument,
        "start": series.start.isoformat(),
        "end": series.end.isoformat(),
        "anchor_open": _price_str(series.anchor_open),
        "sessions": [[day.isoformat(), _price_str(close)] for day, close in series.sessions],
        "total_return_pct": series.total_return_pct,
        "data_snapshot_hash": data_snapshot_hash,
        "source_note": series.source_note,
    }
    rendered = (
        json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
        )
        + "\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_simple(path, lambda tmp: tmp.write_text(rendered, encoding="utf-8"))
    _LOG.info(
        "baseline.written",
        path=str(path),
        instrument=series.instrument,
        sessions=len(series.sessions),
    )


def load_baseline(path: Path) -> BaselineSeries | None:
    """Read + validate a baseline artifact; ANY damage returns ``None`` (rebuild).

    The ``load_stamp`` law: the file must read, parse as a JSON object, carry
    the exact B8 key set with ``schema_version`` matching, and every field
    must reconstruct into a valid :class:`BaselineSeries` (dates ISO, prices
    Decimal, sessions strictly ascending, start/end consistent). Each
    rejection logs ONE ``baseline.load_invalid`` warning naming the reason —
    never silent, never a half-trusted artifact.
    """

    def _invalid(reason: str) -> None:
        _LOG.warning("baseline.load_invalid", path=str(path), reason=reason)

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        _invalid("unreadable")
        return None
    try:
        payload = json.loads(raw)
    except ValueError:
        _invalid("not_json")
        return None
    if not isinstance(payload, dict):
        _invalid("not_object")
        return None
    if set(payload.keys()) != _PAYLOAD_KEYS:
        _invalid("bad_key_set")
        return None
    if payload["schema_version"] != BASELINE_SCHEMA_VERSION:
        _invalid("schema_version_mismatch")
        return None
    if not isinstance(payload["data_snapshot_hash"], str) or not payload["data_snapshot_hash"]:
        _invalid("bad_data_snapshot_hash")
        return None
    sessions_raw = payload["sessions"]
    if not isinstance(sessions_raw, list) or not all(
        isinstance(entry, list)
        and len(entry) == _SESSION_FIELDS
        and isinstance(entry[0], str)
        and isinstance(entry[1], str)
        for entry in sessions_raw
    ):
        _invalid("bad_sessions_shape")
        return None
    if not all(
        isinstance(payload[key], str) for key in ("instrument", "start", "end", "anchor_open")
    ) or not isinstance(payload["source_note"], str):
        _invalid("bad_field_types")
        return None
    if not isinstance(payload["total_return_pct"], int | float) or isinstance(
        payload["total_return_pct"], bool
    ):
        _invalid("bad_total_return_pct")
        return None
    try:
        series = BaselineSeries(
            instrument=payload["instrument"],
            start=date.fromisoformat(payload["start"]),
            end=date.fromisoformat(payload["end"]),
            anchor_open=Decimal(payload["anchor_open"]),
            sessions=tuple(
                (date.fromisoformat(entry[0]), Decimal(entry[1])) for entry in sessions_raw
            ),
            total_return_pct=float(payload["total_return_pct"]),
            source_note=payload["source_note"],
        )
    except (BaselineError, InvalidOperation, ValueError):
        _invalid("bad_field_values")
        return None
    return series
