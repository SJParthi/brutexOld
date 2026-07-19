"""Deterministic input corpus for the baseline parity goldens.

DEC-RUST-CASCADE-RESULTS-001 phase A (slice 2d): the T14 lane will convert the
``brutex.engine.baseline`` law families to the ``rust/brutex-cascade`` crate
under the compressed greenfield cycle (capture-first — this corpus is frozen
against the UNTOUCHED Python module BEFORE any conversion). This module is the
ONE builder of the parity corpus INPUTS *and* the shared scenario RUNNERS —
shared verbatim by

* the capture generator (``gen_cascade_rust_parity.py`` — run ONCE against the
  pre-conversion pure-Python implementations; fixture frozen), and
* the replay golden (``test_baseline_rust_parity_golden.py`` — drives the
  post-conversion Rust-backed API against the frozen fixture),

so the two sides can never drift on what was fed in.

FROZEN FAMILIES: ``compute_baseline`` (the anchor/per-session-close law, the
Decimal-division-then-float ``total_return_pct`` precision law, the IST
session-key edge, the exact refusal messages incl. their ORDER), the
:class:`BaselineSeries` validation matrix (bool==int puns included),
``baseline_filename`` (NO sanitization — hostile instruments pass through
verbatim, that IS the law), ``_price_str`` (ROUND_HALF_UP 2dp snaps), the
``write_baseline`` artifact BYTES (sorted keys · compact separators · ASCII ·
trailing newline; the write-time price snap; the ``baseline.written`` log
shape) and the ``load_baseline`` damage-verdict matrix (every ``None`` arm's
``baseline.load_invalid`` reason, the version bool/float==int puns that LOAD,
the Decimal ``NaN``-comparison ``InvalidOperation`` funnel, and the quirky
``Infinity``-anchor / ``NaN``-session-price payloads that legitimately LOAD).

Every case is fully deterministic: fixed dates/prices, no RNG, no wall clock,
and NO temp path ever reaches a captured value — the write/load runners work
under a throwaway temp dir whose path appears only in the (uncaptured) log
``path`` field; the artifact TEXT itself is path-free by design. Inputs are
built in CODE (never parsed from the fixture) because several hostile cases
carry values JSON cannot round-trip faithfully as INPUTS (bool-vs-int puns,
Decimal precision, lone surrogates).

The consumed laws stay Python and are treated as INPUT builders, not targets:
:class:`~brutex.data.models.CandleBar` construction (tz-aware + OHLC-ordering
validation is ITS law — the corpus feeds only constructible bars, including
the deliberately-permissive non-positive-price shapes),
``session_lens.ist_trade_date`` (the ONE IST session key — exercised, not
re-derived) and ``money.round_money`` (the 2dp HALF_UP snap ``_price_str``
delegates to).
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

from brutex.data.models import CandleBar
from brutex.engine.baseline import (
    BaselineError,
    BaselineSeries,
    _price_str,
    baseline_filename,
    compute_baseline,
    load_baseline,
    write_baseline,
)
from structlog.testing import capture_logs

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = [
    "MISSING_FILE",
    "ComputeCase",
    "compute_cases",
    "filename_cases",
    "load_cases",
    "price_str_cases",
    "run_compute_case",
    "run_filename_case",
    "run_load_case",
    "run_price_str_case",
    "run_series_ctor_case",
    "run_write_case",
    "series_ctor_cases",
    "write_cases",
]

#: The canonical valid data_snapshot_hash stamp (the unit-suite convention).
_SNAPSHOT = "5a" * 32

#: Sentinel: the load scenario's artifact file deliberately does NOT exist.
MISSING_FILE = "<missing-file>"


def _bar(d: date, hh: int, mm: int, o: str, c: str, *, us: int = 0) -> CandleBar:
    """A constructible bar at ``hh:mm`` UTC with open *o* and close *c*.

    High/low are derived so the CandleBar ordering invariant always holds —
    prices themselves are NOT snapped (the container is permissive; the
    3dp-anchor case exploits exactly that).
    """
    ts = datetime(d.year, d.month, d.day, hh, mm, us // 1_000_000, us % 1_000_000, tzinfo=UTC)
    prices = sorted([Decimal(o), Decimal(c)])
    return CandleBar(
        timestamp=ts,
        open=Decimal(o),
        high=prices[1],
        low=prices[0],
        close=Decimal(c),
        volume=0,
        open_interest=None,
    )


_D1 = date(2024, 3, 4)  # a regular NSE Monday
_D2 = date(2024, 3, 5)
_D3 = date(2024, 3, 7)


def _canonical_bars() -> list[CandleBar]:
    """Three sessions, two bars each — the canonical multi-session fixture."""
    return [
        _bar(_D1, 3, 45, "200.00", "201.40"),
        _bar(_D1, 9, 59, "201.40", "202.20"),
        _bar(_D2, 3, 45, "202.10", "201.80"),
        _bar(_D2, 9, 59, "201.80", "199.95"),
        _bar(_D3, 3, 45, "200.10", "208.60"),
        _bar(_D3, 9, 59, "208.60", "211.55"),
    ]


def _series_to_jsonable(series: BaselineSeries) -> dict[str, Any]:
    """JSON-stable series projection (Decimals as str, float as repr)."""
    return {
        "instrument": series.instrument,
        "start": series.start.isoformat(),
        "end": series.end.isoformat(),
        "anchor_open": str(series.anchor_open),
        "sessions": [[day.isoformat(), str(close)] for day, close in series.sessions],
        "total_return_pct_repr": repr(series.total_return_pct),
        "source_note": series.source_note,
    }


# ──────────────────────────────────────────────────────────────────────────
# compute_baseline — anchor / session closes / division precision / refusals
# ──────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ComputeCase:
    """One compute_baseline invocation.

    ``orderings`` are index permutations of ``bars`` — the runner replays
    every ordering and asserts identical output (the sorted-view law), then
    captures the canonical result.
    """

    bars: tuple[CandleBar, ...]
    instrument: str = "NIFTY"
    orderings: tuple[tuple[int, ...], ...] = ()


def compute_cases() -> list[tuple[str, ComputeCase]]:
    canonical = tuple(_canonical_bars())
    return [
        ("three_sessions_two_bars", ComputeCase(bars=canonical)),
        (
            "permuted_load_orders_reproduce",
            ComputeCase(
                bars=canonical,
                orderings=((5, 4, 3, 2, 1, 0), (2, 0, 4, 1, 5, 3)),
            ),
        ),
        (
            "single_session_single_bar",
            ComputeCase(bars=(_bar(_D1, 5, 0, "123.45", "124.56"),)),
        ),
        (
            "flat_close_equals_anchor_zero_return",
            ComputeCase(bars=(_bar(_D1, 5, 0, "50.00", "50.00"),)),
        ),
        (
            "negative_return",
            ComputeCase(
                bars=(
                    _bar(_D1, 5, 0, "100.00", "97.35"),
                    _bar(_D2, 5, 0, "97.00", "88.20"),
                )
            ),
        ),
        (
            "nonterminating_division_anchor_3",
            ComputeCase(bars=(_bar(_D1, 5, 0, "3", "10"),)),
        ),
        (
            "nonterminating_division_anchor_7_77",
            ComputeCase(bars=(_bar(_D1, 5, 0, "7.77", "11.13"),)),
        ),
        (
            "anchor_3dp_kept_verbatim_in_series",
            ComputeCase(bars=(_bar(_D1, 5, 0, "100.005", "101.115"),)),
        ),
        (
            "huge_prices",
            ComputeCase(
                bars=(
                    _bar(_D1, 5, 0, "99999999.98", "99999999.99"),
                    _bar(_D2, 5, 0, "99999999.99", "99999999.97"),
                )
            ),
        ),
        (
            # 18:35 UTC on _D1 is 00:05 IST on _D1+1 — the bar belongs to the
            # NEXT IST session date (the ist_trade_date session-key law).
            "ist_midnight_edge_next_session",
            ComputeCase(
                bars=(
                    _bar(_D1, 5, 0, "100.00", "101.00"),
                    _bar(_D1, 18, 35, "101.00", "102.50"),
                )
            ),
        ),
        (
            # Later bar (by sorted ts) wins the session close, whatever the
            # given order.
            "last_bar_per_session_wins",
            ComputeCase(
                bars=(
                    _bar(_D1, 9, 59, "101.00", "103.33"),
                    _bar(_D1, 3, 45, "100.00", "101.00"),
                    _bar(_D1, 7, 30, "101.50", "102.22"),
                ),
                orderings=((2, 1, 0), (1, 0, 2)),
            ),
        ),
        ("empty_bars_refused", ComputeCase(bars=(), instrument="π-index")),
        (
            "duplicate_timestamp_refused",
            ComputeCase(
                bars=(
                    _bar(_D1, 3, 46, "100.00", "101.00"),
                    _bar(_D1, 3, 46, "100.00", "102.00"),
                )
            ),
        ),
        (
            "zero_anchor_refused",
            ComputeCase(bars=(_bar(_D1, 5, 0, "0.00", "1.00"),)),
        ),
        (
            "negative_anchor_refused",
            ComputeCase(bars=(_bar(_D1, 5, 0, "-5.00", "1.00"),)),
        ),
        (
            # The duplicate check runs BEFORE the anchor check — a list with
            # BOTH defects reads the duplicate message (frozen error order).
            "duplicate_beats_anchor_check",
            ComputeCase(
                bars=(
                    _bar(_D1, 3, 46, "0.00", "1.00"),
                    _bar(_D1, 3, 46, "0.00", "2.00"),
                )
            ),
        ),
    ]


def run_compute_case(case: ComputeCase) -> dict[str, Any]:
    """The serialized series (order-independence verified in-runner) or error."""
    try:
        canonical = _series_to_jsonable(compute_baseline(case.bars, instrument=case.instrument))
    except BaselineError as exc:
        return {"error_type": type(exc).__name__, "error_str": str(exc)}
    for ordering in case.orderings:
        permuted = tuple(case.bars[i] for i in ordering)
        result = _series_to_jsonable(compute_baseline(permuted, instrument=case.instrument))
        assert result == canonical, f"load-order independence violated for ordering {ordering}"
    return canonical


# ──────────────────────────────────────────────────────────────────────────
# BaselineSeries — the constructor validation matrix
# ──────────────────────────────────────────────────────────────────────────


def _series(**overrides: Any) -> BaselineSeries:
    base: dict[str, Any] = {
        "instrument": "NIFTY",
        "start": _D1,
        "end": _D2,
        "anchor_open": Decimal("100.00"),
        "sessions": ((_D1, Decimal("101.20")), (_D2, Decimal("99.95"))),
        "total_return_pct": -0.05,
        "source_note": "note",
    }
    base.update(overrides)
    return BaselineSeries(**base)


def series_ctor_cases() -> list[tuple[str, Callable[[], BaselineSeries]]]:
    return [
        ("ok_two_sessions", _series),
        (
            "ok_bool_total_return_pun",
            lambda: _series(total_return_pct=True),
        ),
        ("empty_instrument_refused", lambda: _series(instrument="")),
        ("empty_sessions_refused", lambda: _series(sessions=())),
        (
            "equal_dates_not_ascending_refused",
            lambda: _series(sessions=((_D1, Decimal("1")), (_D1, Decimal("2")))),
        ),
        (
            "descending_dates_refused",
            lambda: _series(sessions=((_D2, Decimal("1")), (_D1, Decimal("2")))),
        ),
        ("start_mismatch_refused", lambda: _series(start=_D3)),
        ("end_mismatch_refused", lambda: _series(end=_D3)),
        (
            "zero_anchor_refused",
            lambda: _series(anchor_open=Decimal("0.00")),
        ),
        (
            "negative_anchor_refused",
            lambda: _series(anchor_open=Decimal("-1.23")),
        ),
        ("nan_total_refused", lambda: _series(total_return_pct=float("nan"))),
        ("inf_total_refused", lambda: _series(total_return_pct=float("inf"))),
        (
            "negative_inf_total_refused",
            lambda: _series(total_return_pct=float("-inf")),
        ),
    ]


def run_series_ctor_case(thunk: Callable[[], BaselineSeries]) -> dict[str, Any]:
    try:
        series = thunk()
    except BaselineError as exc:
        return {"error_type": type(exc).__name__, "error_str": str(exc)}
    return {"series": _series_to_jsonable(series)}


# ──────────────────────────────────────────────────────────────────────────
# baseline_filename — NO sanitization; hostile instruments pass VERBATIM
# ──────────────────────────────────────────────────────────────────────────


def filename_cases() -> list[tuple[str, str]]:
    return [
        ("plain", "NIFTY"),
        ("ampersand_verbatim", "NSE-M&M"),
        ("unicode_verbatim", "π-index"),
        ("slash_verbatim", "a/b"),
        ("dotdot_verbatim", ".."),
        ("whitespace_is_truthy", " "),
        ("empty_refused", ""),
    ]


def run_filename_case(instrument: str) -> dict[str, Any]:
    try:
        return {"filename": baseline_filename(instrument)}
    except BaselineError as exc:
        return {"error_type": type(exc).__name__, "error_str": str(exc)}


# ──────────────────────────────────────────────────────────────────────────
# _price_str — the ROUND_HALF_UP 2dp serialization snap
# ──────────────────────────────────────────────────────────────────────────


def price_str_cases() -> list[tuple[str, str]]:
    """(name, Decimal literal) — half-up snaps with NO float in the loop."""
    return [
        ("half_up_boundary", "1.005"),
        ("negative_half_away_from_zero", "-1.005"),
        ("below_boundary_truncates", "1.0049"),
        ("zero_pads", "0"),
        ("exponent_form_expands", "1E+2"),
        ("one_dp_pads", "123.4"),
        ("tiny_half_up", "0.005"),
        ("two_675_exact_decimal", "2.675"),
        ("already_2dp_identity", "99999999.99"),
    ]


def run_price_str_case(literal: str) -> str:
    return _price_str(Decimal(literal))


# ──────────────────────────────────────────────────────────────────────────
# write_baseline — the deterministic B8 artifact BYTES + the written log
# ──────────────────────────────────────────────────────────────────────────


def write_cases() -> list[tuple[str, Callable[[], tuple[BaselineSeries, str]]]]:
    """Thunks yielding ``(series, data_snapshot_hash)`` for the write runner."""

    def canonical() -> tuple[BaselineSeries, str]:
        return compute_baseline(_canonical_bars(), instrument="NIFTY"), _SNAPSHOT

    def unicode_instrument() -> tuple[BaselineSeries, str]:
        return (
            compute_baseline([_bar(_D1, 5, 0, "10.00", "11.00")], instrument="π-index"),
            _SNAPSHOT,
        )

    def lone_surrogate_instrument() -> tuple[BaselineSeries, str]:
        return (
            compute_baseline([_bar(_D1, 5, 0, "10.00", "11.00")], instrument="BAD-\ud800"),
            _SNAPSHOT,
        )

    def anchor_3dp_snaps_at_write() -> tuple[BaselineSeries, str]:
        return (
            compute_baseline([_bar(_D1, 5, 0, "100.005", "101.115")], instrument="SNAP"),
            _SNAPSHOT,
        )

    def bool_total_serializes_true() -> tuple[BaselineSeries, str]:
        return _series(total_return_pct=True), _SNAPSHOT

    def empty_hash_refused() -> tuple[BaselineSeries, str]:
        return _series(), ""

    return [
        ("canonical_three_sessions", canonical),
        ("unicode_instrument_ascii_escaped", unicode_instrument),
        ("lone_surrogate_instrument_escaped", lone_surrogate_instrument),
        ("anchor_3dp_snaps_at_write", anchor_3dp_snaps_at_write),
        ("bool_total_serializes_as_true", bool_total_serializes_true),
        ("empty_snapshot_hash_refused", empty_hash_refused),
    ]


def run_write_case(thunk: Callable[[], tuple[BaselineSeries, str]]) -> dict[str, Any]:
    """The artifact TEXT (path-free by design) + the written-log shape, or error."""
    series, snapshot = thunk()
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "artifact.json"
        with capture_logs() as logs:
            try:
                write_baseline(path, series, data_snapshot_hash=snapshot)
            except BaselineError as exc:
                return {"error_type": type(exc).__name__, "error_str": str(exc)}
        written = [e for e in logs if e.get("event") == "baseline.written"]
        assert len(written) == 1, "write_baseline must log exactly one baseline.written"
        return {
            "text": path.read_text(encoding="utf-8"),
            "log_instrument": written[0]["instrument"],
            "log_sessions": written[0]["sessions"],
        }


# ──────────────────────────────────────────────────────────────────────────
# load_baseline — the damage-verdict matrix (reason names frozen)
# ──────────────────────────────────────────────────────────────────────────


def _valid_payload_text() -> str:
    """The canonical VALID artifact text, produced by the real writer."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "artifact.json"
        write_baseline(
            path,
            compute_baseline(_canonical_bars(), instrument="NIFTY"),
            data_snapshot_hash=_SNAPSHOT,
        )
        return path.read_text(encoding="utf-8")


def _payload_variant(**overrides: Any) -> str:
    """The valid payload with fields overridden (a key set to ... is DROPPED)."""
    payload = json.loads(_valid_payload_text())
    for key, value in overrides.items():
        if value is ...:
            payload.pop(key, None)
        else:
            payload[key] = value
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"


def load_cases() -> list[tuple[str, str]]:
    """(name, raw artifact text | MISSING_FILE)."""
    return [
        ("valid_roundtrip", _valid_payload_text()),
        ("missing_file_unreadable", MISSING_FILE),
        ("torn_json", _valid_payload_text()[:40]),
        ("json_list_not_object", '["schema_version",1]\n'),
        ("json_string_not_object", '"baseline"\n'),
        ("json_int_not_object", "42\n"),
        ("json_null_not_object", "null\n"),
        ("missing_key", _payload_variant(source_note=...)),
        ("extra_key", _payload_variant(extra="x")),
        ("schema_version_2", _payload_variant(schema_version=2)),
        ("schema_version_str_1", _payload_variant(schema_version="1")),
        ("schema_version_bool_true_pun_loads", _payload_variant(schema_version=True)),
        ("schema_version_float_1_0_pun_loads", _payload_variant(schema_version=1.0)),
        ("snapshot_hash_int", _payload_variant(data_snapshot_hash=7)),
        ("snapshot_hash_empty", _payload_variant(data_snapshot_hash="")),
        ("sessions_dict_shape", _payload_variant(sessions={"2024-03-04": "1.00"})),
        (
            "sessions_entry_len_3",
            _payload_variant(sessions=[["2024-03-04", "1.00", "x"]]),
        ),
        (
            "sessions_entry_float_price",
            _payload_variant(sessions=[["2024-03-04", 1.5]]),
        ),
        (
            "sessions_entry_bool_price",
            _payload_variant(sessions=[["2024-03-04", True]]),
        ),
        ("sessions_entry_int_date", _payload_variant(sessions=[[20240304, "1.00"]])),
        ("instrument_int", _payload_variant(instrument=5)),
        ("start_int", _payload_variant(start=20240304)),
        ("source_note_null", _payload_variant(source_note=None)),
        ("total_return_str", _payload_variant(total_return_pct="5.5")),
        ("total_return_bool_true_excluded", _payload_variant(total_return_pct=True)),
        ("total_return_null", _payload_variant(total_return_pct=None)),
        (
            "total_return_int_loads",
            _payload_variant(
                total_return_pct=5,
                start="2024-03-04",
                end="2024-03-04",
                sessions=[["2024-03-04", "105.00"]],
            ),
        ),
        ("bad_iso_date", _payload_variant(start="2024-13-01")),
        ("bad_decimal_junk", _payload_variant(anchor_open="junk")),
        (
            "anchor_nan_comparison_invalid_operation",
            _payload_variant(anchor_open="NaN"),
        ),
        ("anchor_infinity_loads", _payload_variant(anchor_open="Infinity")),
        (
            "session_price_nan_loads",
            _payload_variant(
                start="2024-03-04",
                end="2024-03-04",
                sessions=[["2024-03-04", "NaN"]],
            ),
        ),
        (
            "descending_sessions_rejected_by_series",
            _payload_variant(
                sessions=[["2024-03-07", "1.00"], ["2024-03-04", "2.00"]],
                start="2024-03-07",
                end="2024-03-04",
            ),
        ),
        (
            "start_mismatch_rejected_by_series",
            _payload_variant(start="2020-01-01"),
        ),
        ("empty_instrument_rejected_by_series", _payload_variant(instrument="")),
        ("empty_sessions_rejected_by_series", _payload_variant(sessions=[])),
        (
            # A leading duplicate schema_version:99 — Python json's
            # LAST-duplicate-wins law means the trailing real key wins → loads.
            "duplicate_json_keys_last_wins",
            '{"schema_version":99,' + _valid_payload_text().rstrip("\n").removeprefix("{") + "\n",
        ),
        (
            "surrogate_instrument_loads",
            _payload_variant(instrument="BAD-\ud800"),
        ),
    ]


def run_load_case(raw: str) -> dict[str, Any]:
    """The load verdict: loaded series (serialized) or the warn reason."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "artifact.json"
        if raw != MISSING_FILE:
            path.write_text(raw, encoding="utf-8")
        with capture_logs() as logs:
            series = load_baseline(path)
        reasons = [e["reason"] for e in logs if e.get("event") == "baseline.load_invalid"]
        assert len(reasons) <= 1, "load_baseline must warn at most once"
        if series is None:
            assert len(reasons) == 1, "a None load must name its reason"
            return {"loaded": False, "reason": reasons[0]}
        assert not reasons, "a successful load must not warn"
        return {"loaded": True, "series": _series_to_jsonable(series)}
