"""Hypothesis differentials for the Rust-hosted baseline laws.

DEC-RUST-CASCADE-RESULTS-001 phase C (slice 2d). The converted Python law
bodies are DELETED, so each property here drives the Rust-backed shim
(``brutex.engine.baseline``) against a VERBATIM in-test copy of the
pre-conversion Python law frozen from commit 319ccdf87 (the retained
independent-oracle-in-test convention — the slice-2a/2b/2c
``test_*_rust_differential.py`` precedent, R45): hypothesis mutates valid
inputs with hostile edits, BOTH sides run to an OUTCOME, and the outcomes
must be EXACTLY equal — the value/bytes on success, the SAME exception
TYPE and FULL ``str`` on failure (the ``Decimal("NaN")`` compare's
``InvalidOperation``, the huge-int ``OverflowError`` from ``isfinite`` and
the ``json.loads`` ``RecursionError`` are RAW escapes — the frozen law —
so ``Exception`` is caught on both sides).

Priority targets (the S2D Phase-B divergence-risk roster):

* ``compute_baseline`` over hostile bar pools — permuted load orders (the
  oracle run on the SAME shuffled order), duplicate timestamps, IST
  midnight-boundary instants, non-positive anchors, mixed-tz bars — with
  the ``total_return_pct`` Decimal-division-then-f64 law asserted
  bit-exactly (``repr`` equality) on every success;
* the multi-defect error ORDER (empty → duplicate-ts → anchor) over
  shapes carrying SEVERAL defects at once;
* the ``BaselineSeries`` validation matrix (dates ascending, start/end
  consistency, the quiet/signalling-NaN + ``Infinity`` anchor arms with
  exact ``InvalidOperation`` parity, the bool total pun, the huge-int
  ``OverflowError`` and non-numeric ``TypeError`` from ``isfinite``);
* the write-render BYTE law over generated series (constructor-valid
  strategies — the accumulated in-strategy-never-raises law — with the
  quiet-NaN session-price passthrough and the ``sNaN`` / ``Infinity`` /
  beyond-28-digit ``InvalidOperation`` raise arms at the price snap);
* the load damage-verdict ladder over hypothesis-damaged payload
  matrices (version bool/float puns, the ``total_return_pct: true``
  exclusion asymmetry, sessions-shape damage, the construction funnel)
  plus raw-text damage and the ``RecursionError`` nesting-bomb escape.

The ``_price_str`` quantize differential itself SHIPPED in the Phase-B
seam file (``test_engine_baseline_rust_seam.py`` — the full Decimal domain
vs the LIVE ``round_money``); here the snap law is exercised through the
whole write render instead. The Phase-B handoff's untested quiet-NaN
PAYLOAD carry (``NaN123``) is closed below as an equality pin.
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from decimal import InvalidOperation as DecInvalidOperation
from math import isfinite
from pathlib import Path
from typing import Any, Final, cast

import pytest
from brutex.calendars.nse import IST
from brutex.data.models import CandleBar
from brutex.engine.baseline import (
    _PAYLOAD_KEYS,
    _SESSION_FIELDS,
    BASELINE_SCHEMA_VERSION,
    BaselineError,
    BaselineSeries,
    baseline_filename,
    compute_baseline,
    load_baseline,
    write_baseline,
)
from hypothesis import given
from hypothesis import strategies as st
from structlog.testing import capture_logs

# ───────────────────────────── outcome helper ────────────────────────────────


def _outcome(fn: Callable[[], object]) -> tuple[object, ...]:
    """Run *fn* to an outcome: ``("ok", value)`` or ``("exc", type, str)``.

    Raw-escape parity (the Decimal-NaN
    ``InvalidOperation``, the isfinite ``OverflowError``/``TypeError``,
    the json ``RecursionError``) IS the property;
    ``Exception`` (narrowed from ``BaseException`` at the 2026-07-19 CodeQL
    round) still captures every frozen raw escape — all are ordinary
    ``Exception`` subclasses — while ``KeyboardInterrupt``/``SystemExit``
    and a pyo3 ``PanicException`` now propagate LOUDLY instead of being
    laundered into a comparable outcome.
    """
    try:
        return ("ok", fn())
    except Exception as exc:  # outcome parity is the property
        return ("exc", type(exc), str(exc))


# ───────────────── VERBATIM pre-conversion oracles (319ccdf87) ───────────────

_MONEY_QUANTUM: Final[Decimal] = Decimal("0.01")


def _oracle_round_money(rupees: Decimal) -> Decimal:
    """VERBATIM 319ccdf87 ``brutex.money.round_money`` (the crate's twin)."""
    return rupees.quantize(_MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def _oracle_price_str(value: Decimal) -> str:
    """VERBATIM 319ccdf87 ``_price_str``."""
    return str(_oracle_round_money(value))


def _oracle_ist_trade_date(ts_utc: datetime) -> date:
    """The frozen ``session_lens.ist_trade_date`` conversion law.

    The naive-datetime guard is elided: every bar reaching the compute fold
    is a :class:`CandleBar`, whose constructor already refuses naive
    timestamps — the guard arm is unreachable through this suite's domain.
    """
    return ts_utc.astimezone(IST).date()


def _oracle_filename(instrument: str) -> str:
    """VERBATIM 319ccdf87 ``baseline_filename``."""
    if not instrument:
        raise BaselineError("baseline_filename: instrument must be non-empty")
    return f"baseline-{instrument}.json"


def _oracle_compute(bars: Sequence[CandleBar], *, instrument: str) -> dict[str, Any]:
    """VERBATIM 319ccdf87 ``compute_baseline`` body.

    The one elision: the tail returns the computed FIELDS as a dict instead
    of constructing a :class:`BaselineSeries` — the constructor's own
    validation matrix has its OWN differential below, so the oracle here
    never routes through the (Rust-backed) live constructor.
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
        closes[_oracle_ist_trade_date(bar.timestamp)] = bar.close
    sessions = tuple(sorted(closes.items()))
    last_close = sessions[-1][1]
    total_return_pct = float((last_close - anchor) / anchor * 100)
    return {
        "instrument": instrument,
        "start": sessions[0][0],
        "end": sessions[-1][0],
        "anchor_open": anchor,
        "sessions": sessions,
        "total_return_pct": total_return_pct,
        "source_note": (
            f"buy-&-hold {instrument}: anchor = first bar OPEN of the first session; "
            f"one point per IST session = that session's LAST bar CLOSE; computed from "
            f"the sweep's own {len(ordered)} bars over {len(sessions)} session(s) "
            "(R27 same-bars law)"
        ),
    }


def _oracle_validate_series(
    instrument: object,
    start: object,
    end: object,
    anchor_open: object,
    sessions: Sequence[tuple[object, object]],
    total_return_pct: object,
) -> None:
    """VERBATIM 319ccdf87 ``BaselineSeries.__post_init__`` (self.* → params)."""
    if not instrument:
        raise BaselineError("BaselineSeries.instrument must be non-empty")
    if not sessions:
        raise BaselineError("BaselineSeries.sessions must be non-empty")
    if any(sessions[i - 1][0] >= sessions[i][0] for i in range(1, len(sessions))):  # type: ignore[operator]
        raise BaselineError("BaselineSeries.sessions must be strictly ascending by date")
    if start != sessions[0][0] or end != sessions[-1][0]:
        raise BaselineError(
            "BaselineSeries start/end must equal the first/last session date, got "
            f"start={start} end={end} vs sessions "
            f"[{sessions[0][0]} … {sessions[-1][0]}]"
        )
    if anchor_open <= 0:  # type: ignore[operator]
        raise BaselineError(f"BaselineSeries.anchor_open must be > 0, got {anchor_open}")
    if not isfinite(total_return_pct):  # type: ignore[arg-type]
        raise BaselineError(
            f"BaselineSeries.total_return_pct must be finite, got {total_return_pct!r}"
        )


def _oracle_render(series: BaselineSeries, *, data_snapshot_hash: str) -> str:
    """VERBATIM 319ccdf87 ``write_baseline`` payload-build + dumps law
    (the atomic write + the ``baseline.written`` log elided — the shim owns
    the file I/O; the TEXT is the frozen byte contract)."""
    if not data_snapshot_hash:
        raise BaselineError("write_baseline: data_snapshot_hash must be non-empty")
    payload = {
        "schema_version": BASELINE_SCHEMA_VERSION,
        "instrument": series.instrument,
        "start": series.start.isoformat(),
        "end": series.end.isoformat(),
        "anchor_open": _oracle_price_str(series.anchor_open),
        "sessions": [[day.isoformat(), _oracle_price_str(close)] for day, close in series.sessions],
        "total_return_pct": series.total_return_pct,
        "data_snapshot_hash": data_snapshot_hash,
        "source_note": series.source_note,
    }
    return (
        json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
        )
        + "\n"
    )


def _oracle_load_text(raw: str) -> tuple[str, ...]:
    """VERBATIM 319ccdf87 ``load_baseline`` body over ALREADY-READ text.

    Two elisions: the file read (the shim's ``unreadable`` arm — the live
    side is driven through a real temp file below) and the construction
    funnel's :class:`BaselineSeries` call, replaced by marshalling the SAME
    fields then running the verbatim ``_oracle_validate_series`` — the
    funnel's catch set and verdict are identical, and the oracle never
    routes through the (Rust-backed) live constructor. Returns
    ``("invalid", reason)`` or ``("ok", <projected fields>)``.
    """
    try:
        payload = json.loads(raw)
    except ValueError:
        return ("invalid", "not_json")
    if not isinstance(payload, dict):
        return ("invalid", "not_object")
    if set(payload.keys()) != _PAYLOAD_KEYS:
        return ("invalid", "bad_key_set")
    if payload["schema_version"] != BASELINE_SCHEMA_VERSION:
        return ("invalid", "schema_version_mismatch")
    if not isinstance(payload["data_snapshot_hash"], str) or not payload["data_snapshot_hash"]:
        return ("invalid", "bad_data_snapshot_hash")
    sessions_raw = payload["sessions"]
    if not isinstance(sessions_raw, list) or not all(
        isinstance(entry, list)
        and len(entry) == _SESSION_FIELDS
        and isinstance(entry[0], str)
        and isinstance(entry[1], str)
        for entry in sessions_raw
    ):
        return ("invalid", "bad_sessions_shape")
    if not all(
        isinstance(payload[key], str) for key in ("instrument", "start", "end", "anchor_open")
    ) or not isinstance(payload["source_note"], str):
        return ("invalid", "bad_field_types")
    if not isinstance(payload["total_return_pct"], int | float) or isinstance(
        payload["total_return_pct"], bool
    ):
        return ("invalid", "bad_total_return_pct")
    try:
        instrument = payload["instrument"]
        start = date.fromisoformat(payload["start"])
        end = date.fromisoformat(payload["end"])
        anchor_open = Decimal(payload["anchor_open"])
        sessions = tuple(
            (date.fromisoformat(entry[0]), Decimal(entry[1])) for entry in sessions_raw
        )
        total_return_pct = float(payload["total_return_pct"])
        source_note = payload["source_note"]
        _oracle_validate_series(instrument, start, end, anchor_open, sessions, total_return_pct)
    except (BaselineError, DecInvalidOperation, ValueError):
        return ("invalid", "bad_field_values")
    return (
        "ok",
        _project_fields(
            instrument, start, end, anchor_open, sessions, total_return_pct, source_note
        ),
    )


def _project_fields(
    instrument: str,
    start: date,
    end: date,
    anchor_open: Decimal,
    sessions: tuple[tuple[date, Decimal], ...],
    total_return_pct: float,
    source_note: str,
) -> str:
    """A scale-exact, bit-exact comparable projection of series fields
    (``str(Decimal)`` keeps the stored scale; ``repr(float)`` keeps bits)."""
    return repr(
        (
            instrument,
            start.isoformat(),
            end.isoformat(),
            str(anchor_open),
            [(d.isoformat(), str(c)) for d, c in sessions],
            repr(total_return_pct),
            source_note,
        )
    )


def _project_series(series: BaselineSeries) -> str:
    return _project_fields(
        series.instrument,
        series.start,
        series.end,
        series.anchor_open,
        series.sessions,
        series.total_return_pct,
        series.source_note,
    )


# ───────────────────────────── shared helpers ────────────────────────────────

_TZS: Final[tuple[timezone, ...]] = (
    UTC,
    timezone(timedelta(hours=5, minutes=30)),
    timezone(timedelta(hours=-7)),
)

_BASE_TS: Final[datetime] = datetime(2024, 3, 4, 3, 45, tzinfo=UTC)
_BOUNDARY_TS: Final[datetime] = datetime(2024, 3, 4, 18, 25, tzinfo=UTC)  # 23:55 IST

#: Finite Decimal prices (CandleBar-constructible: ordering derived below).
_PRICES = st.decimals(
    allow_nan=False,
    allow_infinity=False,
    min_value=Decimal("-100"),
    max_value=Decimal("100000"),
    places=6,
)

#: Strictly positive anchors incl. high-precision multi-digit shapes.
_POS_PRICES = st.decimals(
    allow_nan=False,
    allow_infinity=False,
    min_value=Decimal("0.000001"),
    max_value=Decimal("100000"),
    places=6,
)


def _mk_bar(ts: datetime, open_: Decimal, close: Decimal) -> CandleBar:
    lo, hi = sorted([open_, close])
    return CandleBar(
        timestamp=ts, open=open_, high=hi, low=lo, close=close, volume=0, open_interest=None
    )


@st.composite
def _bar_pools(draw: st.DrawFn) -> list[CandleBar]:
    """Bars in ANY order — duplicate instants and IST-boundary instants
    arise naturally; mixed tz representations of the same instant too."""
    base = draw(st.sampled_from([_BASE_TS, _BOUNDARY_TS]))
    offsets = draw(st.lists(st.integers(min_value=0, max_value=30_000), min_size=1, max_size=7))
    bars: list[CandleBar] = []
    for off in offsets:
        ts = (base + timedelta(minutes=off)).astimezone(draw(st.sampled_from(_TZS)))
        bars.append(_mk_bar(ts, draw(_PRICES), draw(_PRICES)))
    return bars


_INSTRUMENTS = st.one_of(
    st.text(min_size=1, max_size=8),
    st.sampled_from(["π-index", "NSE-M&M", "BAD-\ud800", "a/b", ".."]),
)


def _assert_compute_parity(bars: Sequence[CandleBar], instrument: str) -> None:
    oracle = _outcome(lambda: _oracle_compute(bars, instrument=instrument))
    live = _outcome(lambda: compute_baseline(bars, instrument=instrument))
    if oracle[0] == "exc":
        assert live == oracle
        return
    assert live[0] == "ok", f"live diverged: {live!r} vs oracle ok"
    fields = cast("dict[str, Any]", oracle[1])
    series = cast("BaselineSeries", live[1])
    assert _project_series(series) == _project_fields(
        fields["instrument"],
        fields["start"],
        fields["end"],
        fields["anchor_open"],
        fields["sessions"],
        fields["total_return_pct"],
        fields["source_note"],
    )


def _write_live(series: BaselineSeries, snapshot: str) -> tuple[str, int]:
    """Write through the REAL shim writer; return (text, written-log count)."""
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "artifact.json"
        with capture_logs() as logs:
            write_baseline(target, series, data_snapshot_hash=snapshot)
        written = [e for e in logs if e.get("event") == "baseline.written"]
        return target.read_text(encoding="utf-8"), len(written)


def _load_live(raw: str) -> tuple[str, ...]:
    """Drive the REAL shim loader through a temp file; project the outcome."""
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "artifact.json"
        target.write_text(raw, encoding="utf-8")
        with capture_logs() as logs:
            series = load_baseline(target)
        reasons = [e["reason"] for e in logs if e.get("event") == "baseline.load_invalid"]
        assert len(reasons) <= 1, "load_baseline must warn at most once"
        if series is None:
            assert len(reasons) == 1, "a None load must name its reason"
            return ("invalid", cast("str", reasons[0]))
        assert not reasons, "a successful load must not warn"
        return ("ok", _project_series(series))


# ─────────────────────────────── compute family ──────────────────────────────


class TestComputeDifferential:
    """The anchor/session fold + the 28-digit total law vs the oracle."""

    @given(bars=_bar_pools(), instrument=_INSTRUMENTS)
    def test_compute_matches_the_deleted_law(self, bars: list[CandleBar], instrument: str) -> None:
        _assert_compute_parity(bars, instrument)

    @given(bars=_bar_pools(), instrument=_INSTRUMENTS, data=st.data())
    def test_permuted_load_order_parity_on_the_same_shuffle(
        self, bars: list[CandleBar], instrument: str, data: st.DataObject
    ) -> None:
        shuffled = data.draw(st.permutations(bars))
        _assert_compute_parity(shuffled, instrument)

    @given(instrument=_INSTRUMENTS)
    def test_empty_bars_refusal_parity(self, instrument: str) -> None:
        _assert_compute_parity([], instrument)

    @given(
        anchor=st.sampled_from([Decimal("0"), Decimal("0.00"), Decimal("-5.25"), Decimal("-0")]),
        close=_PRICES,
        dup=st.booleans(),
    )
    def test_multi_defect_duplicate_beats_bad_anchor(
        self, anchor: Decimal, close: Decimal, dup: bool
    ) -> None:
        """A pool carrying BOTH a duplicate instant and a non-positive
        anchor surfaces the DUPLICATE refusal on both sides; without the
        duplicate the anchor refusal surfaces — same shapes, same order."""
        bars = [
            _mk_bar(_BASE_TS, anchor, close),
            _mk_bar(_BASE_TS + timedelta(minutes=1), close, close),
        ]
        if dup:
            bars.append(_mk_bar(_BASE_TS + timedelta(minutes=1), anchor, anchor))
        _assert_compute_parity(bars, "NIFTY")
        with pytest.raises(
            BaselineError, match="duplicate bar timestamp" if dup else "first bar OPEN"
        ):
            compute_baseline(bars, instrument="NIFTY")

    @given(
        second_offset_s=st.integers(min_value=0, max_value=600),
        micro=st.integers(min_value=0, max_value=999_999),
    )
    def test_ist_midnight_boundary_session_split_parity(
        self, second_offset_s: int, micro: int
    ) -> None:
        """Instants straddling 18:30 UTC (IST midnight) bucket into the
        SAME sessions on both sides, to the microsecond."""
        bars = [
            _mk_bar(
                _BOUNDARY_TS + timedelta(seconds=second_offset_s, microseconds=micro),
                Decimal("100.00"),
                Decimal("101.00"),
            ),
            _mk_bar(_BOUNDARY_TS + timedelta(minutes=20), Decimal("101.00"), Decimal("102.00")),
        ]
        _assert_compute_parity(bars, "NIFTY")


# ───────────────────────── series validation family ──────────────────────────

_ANCHORS = st.one_of(
    _POS_PRICES,
    st.sampled_from(
        [
            Decimal("0"),
            Decimal("-0"),
            Decimal("-1.50"),
            Decimal("NaN"),
            Decimal("-NaN"),
            Decimal("sNaN"),
            Decimal("Infinity"),
            Decimal("-Infinity"),
        ]
    ),
)

_TOTALS = st.one_of(
    st.floats(allow_nan=True, allow_infinity=True),
    st.booleans(),
    st.integers(min_value=-(10**400), max_value=10**400),
    st.sampled_from([None, "5.5"]),
)


@st.composite
def _series_fields(draw: st.DrawFn) -> dict[str, Any]:
    days = draw(
        st.lists(
            st.dates(min_value=date(2020, 1, 1), max_value=date(2030, 12, 31)),
            max_size=4,
        )
    )
    if draw(st.booleans()):
        days = sorted(set(days))
    sessions = tuple((d, draw(_POS_PRICES)) for d in days)
    consistent_ends = draw(st.booleans())
    if days and consistent_ends:
        start, end = days[0], days[-1]
    else:
        start = draw(st.dates(min_value=date(2020, 1, 1), max_value=date(2030, 12, 31)))
        end = draw(st.dates(min_value=date(2020, 1, 1), max_value=date(2030, 12, 31)))
    return {
        "instrument": draw(st.one_of(st.just(""), _INSTRUMENTS)),
        "start": start,
        "end": end,
        "anchor_open": draw(_ANCHORS),
        "sessions": sessions,
        "total_return_pct": draw(_TOTALS),
        "source_note": draw(st.text(max_size=6)),
    }


class TestSeriesValidationDifferential:
    """The __post_init__ matrix (order + messages + raw escapes) vs oracle."""

    @given(fields=_series_fields())
    def test_construction_outcome_matches_the_deleted_law(self, fields: dict[str, Any]) -> None:
        oracle = _outcome(
            lambda: _oracle_validate_series(
                fields["instrument"],
                fields["start"],
                fields["end"],
                fields["anchor_open"],
                fields["sessions"],
                fields["total_return_pct"],
            )
        )

        def _construct() -> None:
            BaselineSeries(**fields)

        assert _outcome(_construct) == oracle

    @pytest.mark.parametrize(
        ("overrides", "expected_type", "fragment"),
        [
            ({"instrument": "", "sessions": ()}, BaselineError, "instrument must be non-empty"),
            ({"sessions": (), "anchor_open": Decimal("-1")}, BaselineError, "must be non-empty"),
            (
                {
                    "sessions": (
                        (date(2024, 3, 5), Decimal("1.00")),
                        (date(2024, 3, 4), Decimal("2.00")),
                    ),
                    "start": date(2020, 1, 1),
                },
                BaselineError,
                "strictly ascending",
            ),
            (
                {"start": date(2020, 1, 1), "anchor_open": Decimal("NaN")},
                BaselineError,
                "start/end must equal",
            ),
            ({"anchor_open": Decimal("NaN")}, DecInvalidOperation, ""),
            ({"anchor_open": Decimal("sNaN")}, DecInvalidOperation, ""),
            (
                {"anchor_open": Decimal("-Infinity")},
                BaselineError,
                "anchor_open must be > 0",
            ),
            ({"total_return_pct": float("nan")}, BaselineError, "must be finite"),
            ({"total_return_pct": 10**400}, OverflowError, "int too large"),
            ({"total_return_pct": "x"}, TypeError, "must be real number"),
        ],
        ids=[
            "empty_instrument_beats_empty_sessions",
            "empty_sessions_beats_bad_anchor",
            "descending_beats_start_mismatch",
            "start_mismatch_beats_nan_anchor",
            "nan_anchor_invalid_operation",
            "snan_anchor_invalid_operation",
            "negative_infinity_anchor_refused",
            "nan_total_refused",
            "huge_int_total_overflow_escape",
            "str_total_typeerror_escape",
        ],
    )
    def test_multi_defect_order_and_escapes(
        self, overrides: dict[str, Any], expected_type: type[BaseException], fragment: str
    ) -> None:
        fields: dict[str, Any] = {
            "instrument": "NIFTY",
            "start": date(2024, 3, 4),
            "end": date(2024, 3, 5),
            "anchor_open": Decimal("100.00"),
            "sessions": (
                (date(2024, 3, 4), Decimal("101.00")),
                (date(2024, 3, 5), Decimal("99.95")),
            ),
            "total_return_pct": -0.05,
            "source_note": "note",
        }
        fields.update(overrides)
        oracle = _outcome(
            lambda: _oracle_validate_series(
                fields["instrument"],
                fields["start"],
                fields["end"],
                fields["anchor_open"],
                fields["sessions"],
                fields["total_return_pct"],
            )
        )
        live = _outcome(lambda: BaselineSeries(**fields))
        assert oracle[0] == "exc" and oracle[1] is expected_type
        assert fragment in str(oracle[2])
        assert live == oracle


# ─────────────────────────────── filename family ─────────────────────────────


class TestFilenameDifferential:
    @given(instrument=st.one_of(st.just(""), st.text(max_size=10), _INSTRUMENTS))
    def test_filename_matches_the_deleted_law(self, instrument: str) -> None:
        assert _outcome(lambda: baseline_filename(instrument)) == _outcome(
            lambda: _oracle_filename(instrument)
        )


# ─────────────────────────────── write family ────────────────────────────────

_CLOSES = st.one_of(
    _POS_PRICES,
    st.sampled_from(
        [
            Decimal("NaN"),
            Decimal("-NaN"),
            Decimal("NaN123"),
            Decimal("sNaN"),
            Decimal("-0.004"),
            Decimal("2.675"),
            Decimal("1E+2"),
        ]
    ),
)


@st.composite
def _valid_series(draw: st.DrawFn) -> BaselineSeries:
    """Constructor-VALID series (the in-strategy-never-raises law): anchors
    > 0 (Infinity / beyond-28-digit shapes included — they construct and
    raise only at the WRITE snap), closes unvalidated (quiet NaN included)."""
    days = sorted(
        draw(
            st.sets(
                st.dates(min_value=date(2020, 1, 1), max_value=date(2030, 12, 31)),
                min_size=1,
                max_size=4,
            )
        )
    )
    anchor = draw(st.one_of(_POS_PRICES, st.sampled_from([Decimal("Infinity"), Decimal("1E+26")])))
    return BaselineSeries(
        instrument=draw(_INSTRUMENTS),
        start=days[0],
        end=days[-1],
        anchor_open=anchor,
        sessions=tuple((d, draw(_CLOSES)) for d in days),
        total_return_pct=draw(
            st.one_of(st.floats(allow_nan=False, allow_infinity=False), st.booleans())
        ),
        source_note=draw(st.text(max_size=8)),
    )


class TestWriteRenderDifferential:
    """The B8 byte law through the REAL writer vs the verbatim render."""

    @given(series=_valid_series(), snapshot=st.text(max_size=6))
    def test_written_bytes_match_the_deleted_law(
        self, series: BaselineSeries, snapshot: str
    ) -> None:
        oracle = _outcome(lambda: _oracle_render(series, data_snapshot_hash=snapshot))
        live = _outcome(lambda: _write_live(series, snapshot))
        if oracle[0] == "exc":
            assert live == oracle
            return
        assert live[0] == "ok", f"live diverged: {live!r} vs oracle ok"
        text, written_logs = cast("tuple[str, int]", live[1])
        assert text == oracle[1]
        assert written_logs == 1

    def test_quiet_nan_payload_carry_matches(self) -> None:
        """The Phase-B handoff's untested NaN-PAYLOAD carry closed: both
        sides propagate the diagnostic payload token verbatim."""
        series = BaselineSeries(
            instrument="NIFTY",
            start=date(2024, 3, 4),
            end=date(2024, 3, 4),
            sessions=((date(2024, 3, 4), Decimal("NaN123")),),
            anchor_open=Decimal("100.00"),
            total_return_pct=0.0,
            source_note="note",
        )
        oracle_text = _oracle_render(series, data_snapshot_hash="5a" * 32)
        live_text, _logs = _write_live(series, "5a" * 32)
        assert live_text == oracle_text
        assert '["2024-03-04","NaN123"]' in live_text


# ─────────────────────────────── load family ─────────────────────────────────


def _canonical_payload() -> dict[str, Any]:
    """The canonical VALID payload, minted through the ORACLE render (writer
    independence — the live writer is never the load fixtures' author)."""
    series = BaselineSeries(
        instrument="NIFTY",
        start=date(2024, 3, 4),
        end=date(2024, 3, 5),
        anchor_open=Decimal("200.00"),
        sessions=(
            (date(2024, 3, 4), Decimal("202.20")),
            (date(2024, 3, 5), Decimal("199.95")),
        ),
        total_return_pct=-0.025,
        source_note="note",
    )
    return cast("dict[str, Any]", json.loads(_oracle_render(series, data_snapshot_hash="5a" * 32)))


_VERSIONS = st.sampled_from([1, True, 1.0, "1", 2, 0, None, 99])
_ISO_TOKENS = st.one_of(
    st.sampled_from(["2024-03-04", "2024-03-05", "2024-03-06", "2024-13-01", "20240304", ""]),
    st.text(max_size=10),
)
_PRICE_TOKENS = st.one_of(
    st.sampled_from(
        ["100.00", "0.01", "NaN", "sNaN", "Infinity", "-5.00", "1.005", "junk", "", "1E+26"]
    ),
    st.text(max_size=6),
)
_SESSION_ENTRIES = st.one_of(
    st.tuples(_ISO_TOKENS, _PRICE_TOKENS).map(list),
    st.sampled_from(
        [
            ["2024-03-04", "1.00", "x"],
            ["2024-03-04", 1.5],
            ["2024-03-04", True],
            [20240304, "1.00"],
            "not-a-list",
            {},
            [],
        ]
    ),
)
_TOTAL_TOKENS = st.one_of(
    st.floats(allow_nan=True, allow_infinity=False),
    st.integers(min_value=-10, max_value=10),
    st.booleans(),
    st.sampled_from(["5.5", None]),
)


@st.composite
def _damaged_payload_text(draw: st.DrawFn) -> str:
    """The canonical payload with 0..3 hypothesis-chosen damage edits —
    valid loads, every ladder rung and the funnel all arise."""
    payload = _canonical_payload()
    edits = draw(st.integers(min_value=0, max_value=3))
    for _ in range(edits):
        op = draw(
            st.sampled_from(
                [
                    "version",
                    "instrument",
                    "start",
                    "end",
                    "anchor",
                    "sessions",
                    "total",
                    "hash",
                    "note",
                    "drop_key",
                    "extra_key",
                ]
            )
        )
        if op == "version":
            payload["schema_version"] = draw(_VERSIONS)
        elif op == "instrument":
            payload["instrument"] = draw(
                st.one_of(st.text(max_size=6), st.sampled_from([5, None, "", "BAD-\ud800"]))
            )
        elif op == "start":
            payload["start"] = draw(st.one_of(_ISO_TOKENS, st.sampled_from([20240304, None])))
        elif op == "end":
            payload["end"] = draw(_ISO_TOKENS)
        elif op == "anchor":
            payload["anchor_open"] = draw(
                st.one_of(_PRICE_TOKENS, st.sampled_from([100, None, True]))
            )
        elif op == "sessions":
            payload["sessions"] = draw(
                st.one_of(
                    st.lists(_SESSION_ENTRIES, max_size=3),
                    st.sampled_from([{}, "x", 5, None]),
                )
            )
        elif op == "total":
            payload["total_return_pct"] = draw(_TOTAL_TOKENS)
        elif op == "hash":
            payload["data_snapshot_hash"] = draw(
                st.one_of(st.text(max_size=6), st.sampled_from([7, "", None]))
            )
        elif op == "note":
            payload["source_note"] = draw(st.one_of(st.text(max_size=6), st.just(None)))
        elif op == "drop_key":
            payload.pop(draw(st.sampled_from(sorted(_PAYLOAD_KEYS))), None)
        else:
            payload[draw(st.sampled_from(["extra", "x", "schema_version2"]))] = 1
    return json.dumps(payload, ensure_ascii=True) + draw(st.sampled_from(["", "\n"]))


class TestLoadVerdictDifferential:
    """The damage-verdict ladder + construction funnel vs the oracle."""

    @given(raw=_damaged_payload_text())
    def test_damaged_payload_verdict_matches_the_deleted_law(self, raw: str) -> None:
        assert _load_live(raw) == _oracle_load_text(raw)

    @given(
        raw=st.one_of(
            st.text(max_size=20),
            st.sampled_from(
                [
                    "",
                    "42",
                    '"baseline"',
                    "null",
                    '["schema_version",1]',
                    "{",
                    '{"a":}',
                ]
            ),
        )
    )
    def test_raw_text_damage_verdict_matches_the_deleted_law(self, raw: str) -> None:
        assert _load_live(raw) == _oracle_load_text(raw)

    def test_duplicate_json_keys_last_wins_parity(self) -> None:
        canonical = json.dumps(_canonical_payload(), ensure_ascii=True)
        raw = '{"schema_version":99,' + canonical.removeprefix("{")
        live = _load_live(raw)
        assert live == _oracle_load_text(raw)
        assert live[0] == "ok"

    def test_recursion_bomb_escapes_raw_on_both_sides(self) -> None:
        raw = "[" * 200_000
        with pytest.raises(RecursionError):
            _oracle_load_text(raw)
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "artifact.json"
            target.write_text(raw, encoding="utf-8")
            with pytest.raises(RecursionError):
                load_baseline(target)


class TestLoadMultiDefectOrder:
    """WHICH damage rung fires first — the deleted ladder's order."""

    @pytest.mark.parametrize(
        ("overrides", "expected_reason"),
        [
            ({"drop": "source_note", "schema_version": 2}, "bad_key_set"),
            ({"schema_version": 2, "data_snapshot_hash": 7}, "schema_version_mismatch"),
            ({"data_snapshot_hash": 7, "sessions": {}}, "bad_data_snapshot_hash"),
            ({"sessions": {}, "instrument": 5}, "bad_sessions_shape"),
            (
                {
                    "sessions": [
                        ["2024-03-04", "1.00", "x"],
                        ["2024-03-05", "2.00"],
                    ],
                },
                "bad_sessions_shape",
            ),
            ({"instrument": 5, "total_return_pct": "x"}, "bad_field_types"),
            ({"total_return_pct": True, "anchor_open": "junk"}, "bad_total_return_pct"),
            ({"anchor_open": "junk", "start": "2024-13-01"}, "bad_field_values"),
            ({"anchor_open": "NaN"}, "bad_field_values"),
            ({"anchor_open": "Infinity"}, None),
            (
                {
                    "sessions": [["2024-03-04", "NaN"], ["2024-03-05", "1.00"]],
                },
                None,
            ),
        ],
        ids=[
            "key_set_beats_version",
            "version_beats_hash",
            "hash_beats_sessions",
            "sessions_beats_field_types",
            "sessions_entry_len_3_is_shape_damage",
            "field_types_beats_total",
            "bool_total_beats_funnel",
            "funnel_last",
            "nan_anchor_funnel_invalid_operation",
            "infinity_anchor_loads",
            "session_price_nan_loads",
        ],
    )
    def test_first_rung_wins_on_both_sides(
        self, overrides: dict[str, Any], expected_reason: str | None
    ) -> None:
        payload = _canonical_payload()
        dropped = overrides.pop("drop", None)
        if dropped is not None:
            payload.pop(dropped)
        payload.update(overrides)
        raw = json.dumps(payload, ensure_ascii=True)
        live = _load_live(raw)
        assert live == _oracle_load_text(raw)
        if expected_reason is None:
            assert live[0] == "ok"
        else:
            assert live == ("invalid", expected_reason)
