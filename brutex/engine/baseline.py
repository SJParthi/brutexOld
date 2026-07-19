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

**RUST-HOSTED LAWS (DEC-RUST-CASCADE-RESULTS-001, slice 2d):** the
compute fold (sorting, the frozen refusal ORDER, the IST session key, the
28-significant-digit ``total_return_pct`` precision law run on the ORIGINAL
``Decimal`` objects through CPython's own operators), the
:class:`BaselineSeries` validation matrix, :func:`baseline_filename`, the
``_price_str`` 2-dp ROUND_HALF_UP snap, the B8 artifact BYTE render and the
load damage-VERDICT ladder live in ``rust/brutex-cascade/src/baseline.rs``,
verified against the FROZEN pre-conversion parity corpus
(``tests/golden/fixtures/cascade_rust_parity/baseline.json``). This module
keeps the type face (constants, the frozen dataclass, the error class) plus
the sanctioned I/O half — the atomic write, the ``path.read_text`` probe,
the structlog observability and the load CONSTRUCTION funnel
(``date.fromisoformat`` / ``Decimal`` / ``float`` marshalling under the
``(BaselineError, InvalidOperation, ValueError)`` catch). The
pre-conversion wheel-free posture is therefore CHANGED by design: the
module hard-requires the brutex-cascade wheel through the
``BRUTEX_CASCADE_RUST`` facade (absent wheel = a loud typed refusal naming
the bootstrap remediation, never a silent degrade).

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

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, Final, cast

import structlog

from brutex.data.atomic_io import atomic_write_simple
from brutex.data.guarded_read import read_regular_text
from brutex.engine.cascade_rust import EPOCH_DATE, epoch_us, require_cascade_rust
from brutex.models import BruteXError

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
#: Threaded into the Rust verdict/render laws as data (single source HERE).
BASELINE_SCHEMA_VERSION: Final[int] = 1

#: The B8 payload keys, pinned (load validates the EXACT set — an extra or
#: missing key is damage, never silently tolerated). Threaded into the Rust
#: verdict law as data.
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
    Validation is RUST-HOSTED (DEC-RUST-CASCADE-RESULTS-001): the raised
    refusals are the REAL :class:`BaselineError` with byte-identical
    messages, and a ``Decimal("NaN")`` anchor's ``<= 0`` comparison raises
    ``InvalidOperation`` exactly as the deleted Python compare did.
    """

    instrument: str
    start: date
    end: date
    anchor_open: Decimal
    sessions: tuple[tuple[date, Decimal], ...]
    total_return_pct: float
    source_note: str

    def __post_init__(self) -> None:
        require_cascade_rust().baseline_validate_series(
            self.instrument,
            self.start,
            self.end,
            self.anchor_open,
            self.sessions,
            self.total_return_pct,
            BaselineError,
        )


def baseline_filename(instrument: str) -> str:
    """The pinned artifact filename — ``baseline-<instrument>.json`` (design D0b).

    RUST-HOSTED: NO sanitization — a hostile instrument passes VERBATIM
    (that IS the law); only an empty instrument refuses.
    """
    return cast("str", require_cascade_rust().baseline_filename(instrument, BaselineError))


def compute_baseline(bars: Sequence[CandleBar], *, instrument: str) -> BaselineSeries:
    """Compute the buy-&-hold baseline from the sweep's OWN bars (R27).

    RUST-HOSTED fold (DEC-RUST-CASCADE-RESULTS-001): timestamps cross as
    exact epoch microseconds (plus the original datetime objects, so the
    duplicate refusal interpolates the FULL CPython repr); prices cross as
    the ORIGINAL ``Decimal`` objects — the series carries them VERBATIM
    (the 2-dp snap happens only at write) and the 28-significant-digit
    ``total_return_pct`` division runs through CPython's own Decimal
    operators at the ambient context, never plain f64 division.

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
    pool = list(bars)
    out = cast(
        "dict[str, Any]",
        require_cascade_rust().baseline_compute(
            instrument,
            [epoch_us(bar.timestamp) for bar in pool],
            [bar.timestamp for bar in pool],
            [bar.open for bar in pool],
            [bar.close for bar in pool],
            BaselineError,
        ),
    )
    sessions: tuple[tuple[date, Decimal], ...] = tuple(
        (EPOCH_DATE + timedelta(days=day), close)
        for day, close in zip(out["session_days"], out["session_closes"], strict=True)
    )
    return BaselineSeries(
        instrument=instrument,
        start=sessions[0][0],
        end=sessions[-1][0],
        anchor_open=out["anchor"],
        sessions=sessions,
        total_return_pct=out["total_return_pct"],
        source_note=out["source_note"],
    )


def _price_str(value: Decimal) -> str:
    """Serialize a rupee price as its 2-dp tick-grid string (B8 law).

    RUST-HOSTED: ``str(round_money(value))`` via the crate's half-up 2-dp
    quantize twin (a quiet NaN passes through; ``sNaN`` / ``Infinity`` and
    a beyond-28-digit quantize raise the exact CPython
    ``decimal.InvalidOperation``).
    """
    return cast("str", require_cascade_rust().baseline_price_str(value))


def write_baseline(path: Path, series: BaselineSeries, *, data_snapshot_hash: str) -> None:
    """Write the deterministic B8 JSON artifact (atomic, trailing newline).

    ``data_snapshot_hash`` stamps the EXACT data identity the series was
    computed from (the sweep's own ``bxsnap2`` digest) — the artifact is
    traceable to its inputs byte-for-byte. RUST-HOSTED render
    (DEC-RUST-CASCADE-RESULTS-001): the payload build + the
    ``json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    allow_nan=False)`` byte law live crate-side; the atomic write + the
    ``baseline.written`` log stay here.

    Raises
    ------
    BaselineError
        On an empty *data_snapshot_hash* (an unstamped artifact would be
        untraceable — refused, BEFORE any payload build or write).
    OSError
        Raw from the atomic write (the engine sidecar error contract —
        never mis-escalated to a lake halt).
    """
    rendered = cast(
        "str",
        require_cascade_rust().baseline_render(
            series.instrument,
            series.start,
            series.end,
            series.anchor_open,
            series.sessions,
            series.total_return_pct,
            series.source_note,
            data_snapshot_hash,
            BASELINE_SCHEMA_VERSION,
            BaselineError,
        ),
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
    never silent, never a half-trusted artifact. RUST-HOSTED verdict ladder
    (DEC-RUST-CASCADE-RESULTS-001): the parse + shape/type/version gates run
    crate-side over the already-read content; the file read (``unreadable``)
    and the construction funnel (``bad_field_values``) stay here.
    """

    def _invalid(reason: str) -> None:
        _LOG.warning("baseline.load_invalid", path=str(path), reason=reason)

    try:
        raw = read_regular_text(path)
    except OSError:
        _invalid("unreadable")
        return None
    reason, payload = cast(
        "tuple[str | None, dict[str, Any] | None]",
        require_cascade_rust().baseline_load_verdict(
            raw, BASELINE_SCHEMA_VERSION, _PAYLOAD_KEYS, _SESSION_FIELDS
        ),
    )
    if payload is None:
        # The crate contract: exactly one of (reason, payload) is None.
        _invalid(cast("str", reason))
        return None
    try:
        series = BaselineSeries(
            instrument=payload["instrument"],
            start=date.fromisoformat(payload["start"]),
            end=date.fromisoformat(payload["end"]),
            anchor_open=Decimal(payload["anchor_open"]),
            sessions=tuple(
                (date.fromisoformat(entry[0]), Decimal(entry[1])) for entry in payload["sessions"]
            ),
            total_return_pct=float(payload["total_return_pct"]),
            source_note=payload["source_note"],
        )
    except (BaselineError, InvalidOperation, ValueError):
        _invalid("bad_field_values")
        return None
    return series
