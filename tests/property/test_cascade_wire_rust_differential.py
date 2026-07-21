"""Hypothesis differentials for the Rust-hosted cascade_wire PARSE + MATH laws.

DEC-RUST-CASCADE-RESULTS-001 phase B (slice 2a). The converted Python law
bodies are DELETED, so each property here drives the Rust-backed public API
against a VERBATIM in-test copy of the pre-conversion Python law (the
retained independent-oracle-in-test convention — the slice-1
``test_cascade_rust_properties.py`` precedent, R45): hypothesis mutates a
valid serializer-shaped payload with hostile edits, BOTH sides run to an
OUTCOME, and the outcomes must be EXACTLY equal — an equal dataclass on
success, the SAME exception TYPE and FULL ``str`` on failure (the raw
``IndexError``/``ValueError``/``AttributeError`` escapes ARE the frozen
law, so ``Exception`` is caught on both sides and compared).

Oracle copies are verbatim from commit f7ea834 (the pre-conversion module),
adapted ONLY to reference the unchanged type faces (``CascadeTupleSpec`` /
``CascadeWireError`` / the enums) and the still-Python §11-COMMON inputs
(``cascade_config_to_dict`` / ``price_source_params_fragment`` /
``FillMode`` / ``is_futures_series_symbol``) by import.

Disclosed divergence envelope (scoped OUT of the equality strategies and
pinned separately in ``TestBeyondWordEnvelope``): roll-schedule ordinals
beyond ±i128 and ``_per_bar_returns`` paisa beyond ±i64 refuse LOUDLY
(``OverflowError``) where the deleted Python law computed over unbounded
ints — the DEC's refuse-never-diverge posture for beyond-word integers.
"""

from __future__ import annotations

import copy
import json
import math
import struct
from collections.abc import Callable, Sequence
from typing import Any, cast

import pytest
from brutex.data.futures_series import is_futures_series_symbol
from brutex.engine.cascade_wire import (
    REPORT_VERSION,
    CascadeManifest,
    CascadeTupleSpec,
    CascadeWireError,
    CascadeWireReport,
    LeaderboardTrialsRecord,
    Stage3CellRecord,
    Stage3ManifestRecord,
    _finite,
    _lbt_from_dict,
    _lbt_to_dict,
    _max_drawdown_return_units,
    _per_bar_returns,
    _roll_schedules_from_payload,
    _spec_from_dict,
    _spec_to_dict,
    _stage3_from_dict,
    _stage3_to_dict,
    cascade_config_to_dict,
    manifest_from_json,
    manifest_to_json,
    serialize_wire_report,
    wire_report_to_dict,
)
from brutex.execution.fill_mode import FillMode
from brutex.metrics.cascade import CandidateVerdict, CascadeReport
from brutex.strategies.types import FibAnchorKind, PriceSource, price_source_params_fragment
from brutex.vocabulary.bits import TOTAL_CONDITIONS
from hypothesis import given
from hypothesis import strategies as st

from tests.golden._cascade_wire_parity_corpus import (
    _lbt,
    _manifest,
    _report,
    _rid,
    _spec,
    _stage3_cell,
    _verdict,
    wire_report_cases,
)

_MAX_MASK = (1 << TOTAL_CONDITIONS) - 1
_I64_MAX = 2**63 - 1
_I128_MAX = 2**127 - 1
_FUTURES_ALIAS = "NSE-NIFTY-FUT"


def _outcome(fn: Callable[[], object]) -> tuple[object, ...]:
    """Run *fn* to an outcome: ``("ok", value)`` or ``("exc", type, str)``.

    Raw-escape parity (the frozen law's
    ``IndexError``/``AttributeError`` passthroughs) IS the property;
    ``Exception`` (narrowed from ``BaseException`` at the 2026-07-19 CodeQL
    round) still captures every frozen raw escape — all are ordinary
    ``Exception`` subclasses — while ``KeyboardInterrupt``/``SystemExit``
    and a pyo3 ``PanicException`` now propagate LOUDLY instead of being
    laundered into a comparable outcome.
    """
    try:
        return ("ok", fn())
    except Exception as exc:
        return ("exc", type(exc), str(exc))


# ───────────────────── VERBATIM pre-conversion oracles (f7ea834) ─────────────


def _oracle_spec_from_dict(data: dict[str, object]) -> CascadeTupleSpec:
    """VERBATIM pre-conversion ``_spec_from_dict`` (fill gate + coercion walk)."""
    raw = cast(dict[str, Any], data)
    if raw.get("fill_mode") is None or str(raw["fill_mode"]) != FillMode.WORST_CASE.value:
        raise CascadeWireError(
            "precise-era cascade manifest refused (DEC-FILL-WORST-CASE-ONLY-001): "
            f"fill_mode={raw.get('fill_mode')!r} — worst_case is the only fill "
            "model; re-run the sweep to produce a worst-case manifest"
        )
    try:
        proximity = raw["proximity_bps"]
        proximity_bps = (
            None if proximity is None else (int(proximity[0]), int(proximity[1]), int(proximity[2]))
        )
        winners = tuple((str(run_id), int(mask)) for run_id, mask in raw["winners"])
        return CascadeTupleSpec(
            instrument=str(raw["instrument"]),
            tf_label=str(raw["tf_label"]),
            tf_minutes=int(raw["tf_minutes"]),
            direction_label=str(raw["direction_label"]),
            sl_paisa=None if raw["sl_paisa"] is None else int(raw["sl_paisa"]),
            tp_paisa=None if raw["tp_paisa"] is None else int(raw["tp_paisa"]),
            prior_n=int(raw["prior_n"]),
            proximity_bps=proximity_bps,
            start_equity_paisa=int(raw["start_equity_paisa"]),
            winners=winners,
            fib_anchor=(
                FibAnchorKind.PREV_DAY
                if raw.get("fib_anchor") is None
                else FibAnchorKind(str(raw["fib_anchor"]))
            ),
            vwap_source=(
                PriceSource.HLC3
                if raw.get("vwap_source") is None
                else PriceSource(str(raw["vwap_source"]))
            ),
            ema_source=(
                PriceSource.CLOSE
                if raw.get("ema_source") is None
                else PriceSource(str(raw["ema_source"]))
            ),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise CascadeWireError(f"invalid cascade tuple spec in manifest: {exc}") from exc


def _oracle_stage3_from_dict(data: dict[str, object]) -> Stage3ManifestRecord:
    """VERBATIM pre-conversion ``_stage3_from_dict`` (the per-cell construction law)."""
    raw = cast(dict[str, Any], data)
    try:
        cells: list[Stage3CellRecord] = []
        for cell in raw["refined_cells"]:
            proximity = cell["proximity_bps"]
            cells.append(
                Stage3CellRecord(
                    instrument=str(cell["instrument"]),
                    tf_label=str(cell["tf_label"]),
                    direction_label=str(cell["direction_label"]),
                    k=int(cell["k"]),
                    sl_paisa=int(cell["sl_paisa"]),
                    tp_paisa=int(cell["tp_paisa"]),
                    proximity_bps=(
                        None
                        if proximity is None
                        else (int(proximity[0]), int(proximity[1]), int(proximity[2]))
                    ),
                    rel_path=(None if cell.get("rel_path") is None else str(cell["rel_path"])),
                    fib_anchor=(
                        FibAnchorKind.PREV_DAY
                        if cell.get("fib_anchor") is None
                        else FibAnchorKind(str(cell["fib_anchor"]))
                    ),
                    vwap_source=(
                        PriceSource.HLC3
                        if cell.get("vwap_source") is None
                        else PriceSource(str(cell["vwap_source"]))
                    ),
                    ema_source=(
                        PriceSource.CLOSE
                        if cell.get("ema_source") is None
                        else PriceSource(str(cell["ema_source"]))
                    ),
                )
            )
        hash_raw = raw["train_snapshot_hash"]
        return Stage3ManifestRecord(
            enabled=bool(raw["enabled"]),
            train_snapshot_hash=None if hash_raw is None else str(hash_raw),
            refined_cells=tuple(cells),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise CascadeWireError(f"invalid stage3 record in manifest: {exc}") from exc


def _oracle_lbt_from_dict(data: dict[str, object]) -> LeaderboardTrialsRecord:
    """VERBATIM pre-conversion ``_lbt_from_dict`` (coercion + construction)."""
    raw = cast(dict[str, Any], data)
    try:
        return LeaderboardTrialsRecord(
            instrument=str(raw["instrument"]),
            tf_label=str(raw["tf_label"]),
            n_trials=int(raw["n_trials"]),
            n_finite=int(raw["n_finite"]),
            sum_sharpe=float(raw["sum_sharpe"]),
            sum_sharpe_sq=float(raw["sum_sharpe_sq"]),
            complete=bool(raw["complete"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise CascadeWireError(f"invalid leaderboard_trials record in manifest: {exc}") from exc


def _oracle_roll_schedules_from_payload(
    raw: object,
) -> tuple[tuple[str, tuple[int, ...]], ...] | None:
    """VERBATIM pre-conversion ``_roll_schedules_from_payload`` (full shape law)."""
    if raw is None:
        return None

    def _strict_ordinal(ordinal: object) -> int:
        if not isinstance(ordinal, int) or isinstance(ordinal, bool):
            raise ValueError(f"non-int roll_schedule ordinal {ordinal!r}")
        return ordinal

    try:
        parsed = tuple(
            (str(instrument), tuple(_strict_ordinal(ordinal) for ordinal in schedule))
            for instrument, schedule in cast("Sequence[Any]", raw)
        )
    except (TypeError, ValueError) as exc:
        raise CascadeWireError(f"invalid roll_schedules in manifest: {exc}") from exc
    seen: set[str] = set()
    for instrument, schedule in parsed:
        if instrument in seen:
            raise CascadeWireError(
                f"invalid roll_schedules in manifest: duplicate entry for instrument "
                f"{instrument!r} — an internally contradictory manifest cannot pick a "
                "schedule (dict() would silently adopt the last); the manifest is "
                "corrupt — re-run `brutex sweep-all` to mint a fresh one"
            )
        seen.add(instrument)
        if not is_futures_series_symbol(instrument):
            raise CascadeWireError(
                f"invalid roll_schedules in manifest: instrument {instrument!r} is not "
                "the NIFTY-futures series alias — a roll schedule may exist for (and "
                "only for) the futures alias (presence ⟺ alias); the manifest is "
                "corrupt — re-run `brutex sweep-all` to mint a fresh one"
            )
        if not schedule or list(schedule) != sorted(set(schedule)) or schedule[0] < 0:
            raise CascadeWireError(
                f"invalid roll_schedules in manifest: schedule for {instrument!r} must "
                "be a non-empty strictly increasing tuple of non-negative day "
                f"ordinals, got {list(schedule)}"
            )
    return parsed


def _oracle_spec_to_dict(spec: CascadeTupleSpec) -> dict[str, object]:
    """VERBATIM pre-conversion ``_spec_to_dict`` projection."""
    payload: dict[str, object] = {
        "instrument": spec.instrument,
        "tf_label": spec.tf_label,
        "tf_minutes": spec.tf_minutes,
        "direction_label": spec.direction_label,
        "sl_paisa": spec.sl_paisa,
        "tp_paisa": spec.tp_paisa,
        "prior_n": spec.prior_n,
        "proximity_bps": None if spec.proximity_bps is None else list(spec.proximity_bps),
        "start_equity_paisa": spec.start_equity_paisa,
        "winners": [[run_id, mask] for run_id, mask in spec.winners],
        "fib_anchor": spec.fib_anchor.value,
        "fill_mode": FillMode.WORST_CASE.value,
        **price_source_params_fragment(spec.vwap_source, spec.ema_source),
    }
    return payload


def _oracle_stage3_to_dict(record: Stage3ManifestRecord) -> dict[str, object]:
    """VERBATIM pre-conversion ``_stage3_to_dict`` projection."""
    return {
        "enabled": record.enabled,
        "train_snapshot_hash": record.train_snapshot_hash,
        "refined_cells": [
            {
                "instrument": cell.instrument,
                "tf_label": cell.tf_label,
                "direction_label": cell.direction_label,
                "k": cell.k,
                "sl_paisa": cell.sl_paisa,
                "tp_paisa": cell.tp_paisa,
                "proximity_bps": None if cell.proximity_bps is None else list(cell.proximity_bps),
                "rel_path": cell.rel_path,
                "fib_anchor": cell.fib_anchor.value,
                **price_source_params_fragment(cell.vwap_source, cell.ema_source),
            }
            for cell in record.refined_cells
        ],
    }


def _oracle_lbt_to_dict(record: LeaderboardTrialsRecord) -> dict[str, object]:
    """VERBATIM pre-conversion ``_lbt_to_dict`` projection."""
    return {
        "instrument": record.instrument,
        "tf_label": record.tf_label,
        "n_trials": record.n_trials,
        "n_finite": record.n_finite,
        "sum_sharpe": record.sum_sharpe,
        "sum_sharpe_sq": record.sum_sharpe_sq,
        "complete": record.complete,
    }


def _oracle_manifest_to_json(manifest: CascadeManifest) -> bytes:
    """VERBATIM pre-conversion ``manifest_to_json`` render (oracle projections)."""
    payload = {
        "version": manifest.version,
        "sweep_id": manifest.sweep_id,
        "cascade_config": cascade_config_to_dict(manifest.cascade_config),
        "bars_context": {
            "exchange": manifest.bars_context.exchange,
            "segment": manifest.bars_context.segment,
            "start": manifest.bars_context.start,
            "end": manifest.bars_context.end,
        },
        "specs": [_oracle_spec_to_dict(spec) for spec in manifest.specs],
        "stage3": None if manifest.stage3 is None else _oracle_stage3_to_dict(manifest.stage3),
        "leaderboard_trials": (
            None
            if manifest.leaderboard_trials is None
            else [_oracle_lbt_to_dict(record) for record in manifest.leaderboard_trials]
        ),
        "roll_schedules": (
            None
            if manifest.roll_schedules is None
            else [[instrument, list(schedule)] for instrument, schedule in manifest.roll_schedules]
        ),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(
        "utf-8"
    )


def _oracle_finite(value: float | None) -> float:
    """VERBATIM pre-conversion ``_finite`` (None / non-finite → 0.0)."""
    if value is None or not math.isfinite(value):
        return 0.0
    return value


def _oracle_per_bar_returns(
    equity_paisa: Sequence[int], start_equity_paisa: int
) -> tuple[float, ...]:
    """VERBATIM pre-conversion ``_per_bar_returns`` (int true-division fold)."""
    out: list[float] = []
    prev = start_equity_paisa
    for value in equity_paisa:
        out.append((value - prev) / start_equity_paisa)
        prev = value
    return tuple(out)


def _oracle_max_drawdown_return_units(returns: Sequence[float]) -> float:
    """VERBATIM pre-conversion ``_max_drawdown_return_units`` (Python max fold)."""
    peak = float("-inf")
    cum = 0.0
    max_dd = 0.0
    for r in returns:
        cum += r
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)
    return max_dd


# ───────────────────────────── shared strategies ─────────────────────────────

_price_sources = st.sampled_from(list(PriceSource))
_fib_anchors = st.sampled_from(list(FibAnchorKind))
_sl_tp = st.one_of(st.none(), st.integers(min_value=1, max_value=100_000))
_proximity = st.one_of(
    st.none(),
    st.tuples(
        st.integers(min_value=0, max_value=500),
        st.integers(min_value=0, max_value=500),
        st.integers(min_value=0, max_value=500),
    ),
)
_masks = st.one_of(st.just(0), st.just(_MAX_MASK), st.integers(min_value=0, max_value=_MAX_MASK))


@st.composite
def _valid_specs(draw: st.DrawFn) -> CascadeTupleSpec:
    """A structurally VALID CascadeTupleSpec (the mutation/serialisation base)."""
    winners = tuple(
        (_rid(draw(st.integers(min_value=0, max_value=2**64))), draw(_masks))
        for _ in range(draw(st.integers(min_value=0, max_value=3)))
    )
    return _spec(
        instrument=draw(st.sampled_from(["NIFTY", "BANKNIFTY", _FUTURES_ALIAS, "NSE-NIFTY-™"])),
        tf_label=draw(st.sampled_from(["1minute", "5minute", "tf-π"])),
        tf_minutes=draw(st.integers(min_value=1, max_value=375)),
        direction_label=draw(st.sampled_from(["bull", "bear"])),
        sl_paisa=draw(_sl_tp),
        tp_paisa=draw(_sl_tp),
        prior_n=draw(st.integers(min_value=1, max_value=12)),
        proximity_bps=draw(_proximity),
        start_equity_paisa=draw(st.integers(min_value=1, max_value=10**14)),
        winners=winners,
        fib_anchor=draw(_fib_anchors),
        vwap_source=draw(_price_sources),
        ema_source=draw(_price_sources),
    )


# ─────────────────────── spec payload mutation machinery ─────────────────────

#: Sentinel — the mutation DELETES the key instead of setting a value.
_DELETE = object()

_SPEC_KEYS: tuple[str, ...] = (
    "instrument",
    "tf_label",
    "tf_minutes",
    "direction_label",
    "sl_paisa",
    "tp_paisa",
    "prior_n",
    "proximity_bps",
    "start_equity_paisa",
    "winners",
    "fib_anchor",
    "fill_mode",
    "vwap_source",
    "ema_source",
)
_JUNK_SCALARS: tuple[object, ...] = (None, True, 2.75, "abc", "5", "9" * 40, 7, [1, 2])
_WINNER_JUNK: tuple[object, ...] = (
    [["a" * 64]],  # len-1 row → not enough values to unpack
    [["a" * 64, 1, 2]],  # len-3 row → too many values to unpack
    3,  # int → 'int' object is not iterable
    ["ab"],  # 2-char string row → int("b") ValueError
    [[str, "abc"]],  # a type object run_id + a junk mask token
    [["abc", 5]],  # short run_id → the __post_init__ 64-hex refusal
)
_PROX_JUNK: tuple[object, ...] = ([1, 2], [1, 2, 3, 4], "xy")
_FILL_JUNK: tuple[object, ...] = ("precise", _DELETE, "junk", 5, True)

_spec_mutations = st.one_of(
    st.tuples(st.sampled_from(_SPEC_KEYS), st.just(_DELETE)),
    st.tuples(st.sampled_from(_SPEC_KEYS), st.sampled_from(_JUNK_SCALARS)),
    st.tuples(st.just("winners"), st.sampled_from(_WINNER_JUNK)),
    st.tuples(st.just("proximity_bps"), st.sampled_from(_PROX_JUNK)),
    st.tuples(st.just("fill_mode"), st.sampled_from(_FILL_JUNK)),
)


def _mutated_payload(
    spec: CascadeTupleSpec, mutations: list[tuple[str, object]]
) -> dict[str, object]:
    """The CURRENT serializer's payload for *spec* with *mutations* applied.

    ``_spec_to_dict`` is a safe base: its bytes are independently pinned by
    the frozen parity replay, so the mutation base cannot drift.
    """
    payload = dict(_spec_to_dict(spec))
    for key, value in mutations:
        if value is _DELETE:
            payload.pop(key, None)
        else:
            payload[key] = copy.deepcopy(value)
    return payload


class TestSpecFromDictDifferential:
    """``_spec_from_dict`` ≡ the verbatim deleted law over hostile payloads."""

    @given(spec=_valid_specs(), mutations=st.lists(_spec_mutations, min_size=0, max_size=3))
    def test_matches_the_deleted_law(
        self, spec: CascadeTupleSpec, mutations: list[tuple[str, object]]
    ) -> None:
        payload = _mutated_payload(spec, mutations)
        got = _outcome(lambda: _spec_from_dict(dict(payload)))
        want = _outcome(lambda: _oracle_spec_from_dict(dict(payload)))
        assert got == want

    @given(spec=_valid_specs())
    def test_unmutated_payload_round_trips_equal(self, spec: CascadeTupleSpec) -> None:
        assert _spec_from_dict(_spec_to_dict(spec)) == spec

    def test_non_dict_payload_raises_the_raw_attribute_error(self) -> None:
        """A non-dict spec entry escapes as the REAL ``AttributeError`` (frozen)."""
        payload = cast("dict[str, object]", ["not", "a", "dict"])
        got = _outcome(lambda: _spec_from_dict(payload))
        want = _outcome(lambda: _oracle_spec_from_dict(payload))
        assert got == want
        assert got[0] == "exc"
        assert got[1] is AttributeError


# ───────────────────────────── roll schedules ────────────────────────────────

_roll_instruments = st.sampled_from(
    [_FUTURES_ALIAS, "NSE-NIFTY", "nse-nifty-fut", "instr-™", "", "a"]
)
_ordinals = st.one_of(
    st.integers(min_value=-(2**127), max_value=_I128_MAX),  # within ±i128 ONLY (envelope)
    st.booleans(),
    st.floats(allow_nan=False, allow_infinity=False),
    st.sampled_from(["5", "abc", "9" * 40]),
)
_schedules = st.lists(_ordinals, min_size=0, max_size=6)
_roll_rows = st.one_of(
    st.tuples(_roll_instruments, _schedules).map(lambda pair: [pair[0], list(pair[1])]),
    st.sampled_from([7, ["only"], ["a", [1], "x"], "ab", "abcd"]),
)
_roll_payloads = st.one_of(
    st.none(),
    st.lists(_roll_rows, min_size=0, max_size=3),
    st.sampled_from([7, "ab", {"a": 1}]),
)


class TestRollSchedulesDifferential:
    """``_roll_schedules_from_payload`` ≡ the verbatim deleted law."""

    @given(raw=_roll_payloads)
    def test_matches_the_deleted_law(self, raw: object) -> None:
        got = _outcome(lambda: _roll_schedules_from_payload(copy.deepcopy(raw)))
        want = _outcome(lambda: _oracle_roll_schedules_from_payload(copy.deepcopy(raw)))
        assert got == want

    @given(
        ordinals=st.sets(st.integers(min_value=0, max_value=800_000), min_size=1, max_size=5),
    )
    def test_valid_alias_schedule_parses_equal(self, ordinals: set[int]) -> None:
        raw = [[_FUTURES_ALIAS, sorted(ordinals)]]
        assert _roll_schedules_from_payload(raw) == _oracle_roll_schedules_from_payload(raw)


# ───────────────────────── manifest round-trip ───────────────────────────────

_finite_floats = st.floats(min_value=-1e6, max_value=1e6, allow_nan=False, allow_infinity=False)


@st.composite
def _stage3_records(draw: st.DrawFn) -> Stage3ManifestRecord:
    """A valid Stage-3 record — enabled with 0-2 varied cells, or disabled."""
    if not draw(st.booleans()):
        return Stage3ManifestRecord(enabled=False, train_snapshot_hash=None, refined_cells=())
    cells = tuple(
        _stage3_cell(
            direction_label=draw(st.sampled_from(["bull", "bear"])),
            k=draw(st.integers(min_value=0, max_value=5)),
            sl_paisa=draw(st.integers(min_value=1, max_value=10_000)),
            tp_paisa=draw(st.integers(min_value=1, max_value=10_000)),
            proximity_bps=draw(_proximity),
            rel_path=draw(st.one_of(st.none(), st.just("cells/nifty-π.parquet"))),
            fib_anchor=draw(_fib_anchors),
            vwap_source=draw(_price_sources),
            ema_source=draw(_price_sources),
        )
        for _ in range(draw(st.integers(min_value=0, max_value=2)))
    )
    return Stage3ManifestRecord(
        enabled=True,
        train_snapshot_hash=_rid(draw(st.integers(min_value=0, max_value=2**64))),
        refined_cells=cells,
    )


@st.composite
def _lbt_records(draw: st.DrawFn) -> LeaderboardTrialsRecord:
    """A valid leaderboard-trials record (the trial-stats invariants hold)."""
    n_trials = draw(st.integers(min_value=0, max_value=1000))
    n_finite = draw(st.integers(min_value=0, max_value=n_trials))
    if n_finite == 0:
        sum_sharpe, sum_sharpe_sq = 0.0, 0.0
    else:
        sum_sharpe = draw(_finite_floats)
        sum_sharpe_sq = draw(
            st.floats(min_value=0.0, max_value=1e12, allow_nan=False, allow_infinity=False)
        )
    return _lbt(
        instrument=draw(st.sampled_from(["NIFTY", "BANKNIFTY", "SENSEX-™"])),
        tf_label=draw(st.sampled_from(["1minute", "5minute"])),
        n_trials=n_trials,
        n_finite=n_finite,
        sum_sharpe=sum_sharpe,
        sum_sharpe_sq=sum_sharpe_sq,
        complete=draw(st.booleans()),
    )


# ────────────── stage3 / lbt hostile-parse machinery (fix round) ─────────────

_STAGE3_CELL_KEYS: tuple[str, ...] = (
    "instrument",
    "tf_label",
    "direction_label",
    "k",
    "sl_paisa",
    "tp_paisa",
    "proximity_bps",
    "rel_path",
    "fib_anchor",
    "vwap_source",
    "ema_source",
)
#: Post-coercion poison — tokens that PASS ``str()``/``int()`` but fail the
#: enum constructors or ``__post_init__``, so multi-defect CROSS-CELL payloads
#: exercise WHICH cell's error surfaces (the per-cell interleaving law).
_CELL_POISON: tuple[object, ...] = ("BOGUS", "sideways", 0, -1, "/abs/path", "")
_STAGE3_RECORD_JUNK: tuple[tuple[str, object], ...] = (
    ("refined_cells", "junk"),
    ("refined_cells", 7),
    ("refined_cells", [None]),
    ("refined_cells", [[]]),
    ("refined_cells", _DELETE),
    ("train_snapshot_hash", _DELETE),
    ("train_snapshot_hash", ["x"]),
    ("enabled", _DELETE),
)
_LBT_KEYS: tuple[str, ...] = (
    "instrument",
    "tf_label",
    "n_trials",
    "n_finite",
    "sum_sharpe",
    "sum_sharpe_sq",
    "complete",
)

_cell_mutations = st.tuples(
    st.integers(min_value=0, max_value=2),
    st.sampled_from(_STAGE3_CELL_KEYS),
    st.one_of(
        st.just(_DELETE),
        st.sampled_from(_JUNK_SCALARS),
        st.sampled_from(_CELL_POISON),
        st.sampled_from(_PROX_JUNK),
    ),
)
_lbt_mutations = st.tuples(
    st.sampled_from(_LBT_KEYS),
    st.one_of(st.just(_DELETE), st.sampled_from(_JUNK_SCALARS)),
)


@st.composite
def _stage3_parse_bases(draw: st.DrawFn) -> Stage3ManifestRecord:
    """An ENABLED 1-3-cell Stage-3 record — the hostile-parse mutation base."""
    cells = tuple(
        _stage3_cell(
            tf_label=draw(st.sampled_from(["1minute", "5minute"])),
            direction_label=draw(st.sampled_from(["bull", "bear"])),
            k=draw(st.integers(min_value=0, max_value=5)),
            proximity_bps=draw(_proximity),
            rel_path=draw(st.one_of(st.none(), st.just("cells/a.parquet"))),
            fib_anchor=draw(_fib_anchors),
            vwap_source=draw(_price_sources),
            ema_source=draw(_price_sources),
        )
        for _ in range(draw(st.integers(min_value=1, max_value=3)))
    )
    return Stage3ManifestRecord(
        enabled=True,
        train_snapshot_hash=_rid(draw(st.integers(min_value=0, max_value=2**64))),
        refined_cells=cells,
    )


def _two_cell_stage3_payload() -> dict[str, Any]:
    """A valid 2-cell serialized stage3 payload (the deterministic probe base)."""
    record = Stage3ManifestRecord(
        enabled=True,
        train_snapshot_hash="b" * 64,
        refined_cells=(_stage3_cell(), _stage3_cell(tf_label="5minute")),
    )
    return cast("dict[str, Any]", copy.deepcopy(_stage3_to_dict(record)))


def _mutated_stage3_payload(
    record: Stage3ManifestRecord,
    cell_mutations: list[tuple[int, str, object]],
    record_mutations: list[tuple[str, object]],
) -> dict[str, object]:
    """The CURRENT serializer's payload for *record* with mutations applied."""
    payload = cast("dict[str, Any]", copy.deepcopy(_stage3_to_dict(record)))
    for key, value in record_mutations:
        if value is _DELETE:
            payload.pop(key, None)
        else:
            payload[key] = copy.deepcopy(value)
    cells = payload.get("refined_cells")
    if isinstance(cells, list) and cells:
        for index, key, value in cell_mutations:
            cell = cells[index % len(cells)]
            if not isinstance(cell, dict):
                continue
            if value is _DELETE:
                cell.pop(key, None)
            else:
                cell[key] = copy.deepcopy(value)
    return cast("dict[str, object]", payload)


class TestStage3FromDictDifferential:
    """``_stage3_from_dict`` ≡ the verbatim deleted law over hostile payloads.

    Multi-defect CROSS-CELL shapes are generated deliberately: the deleted
    law constructed each cell FULLY (coercions, enum validation AND the
    ``Stage3CellRecord`` construction with its ``__post_init__`` raises)
    before touching the next cell, so WHICH cell's error surfaces is part of
    the frozen law — the fix-round MINOR-1 gap (the first-cut
    coerce-all-cells-first crate walk reported the LATER cell's error and
    was refuted by execution; the crate now constructs per-cell).
    """

    @given(
        record=_stage3_parse_bases(),
        cell_mutations=st.lists(_cell_mutations, min_size=0, max_size=4),
        record_mutations=st.lists(st.sampled_from(_STAGE3_RECORD_JUNK), min_size=0, max_size=1),
    )
    def test_matches_the_deleted_law(
        self,
        record: Stage3ManifestRecord,
        cell_mutations: list[tuple[int, str, object]],
        record_mutations: list[tuple[str, object]],
    ) -> None:
        payload = _mutated_stage3_payload(record, cell_mutations, record_mutations)
        got = _outcome(lambda: _stage3_from_dict(copy.deepcopy(payload)))
        want = _outcome(lambda: _oracle_stage3_from_dict(copy.deepcopy(payload)))
        assert got == want

    @given(record=_stage3_parse_bases())
    def test_unmutated_payload_round_trips_equal(self, record: Stage3ManifestRecord) -> None:
        assert _stage3_from_dict(_stage3_to_dict(record)) == record

    def test_first_offending_cell_enum_error_surfaces(self) -> None:
        """The fix-round MINOR-1 executed probe: cell[0] bad enum + cell[1] bad int.

        The FIRST offending cell's error is the law — pre-fix the crate
        coerced ALL cells before the enum validation and reported cell[1]'s
        ``int()`` failure instead.
        """
        payload = _two_cell_stage3_payload()
        payload["refined_cells"][0]["fib_anchor"] = "BOGUS"
        payload["refined_cells"][1]["k"] = "notanint"
        got = _outcome(lambda: _stage3_from_dict(copy.deepcopy(payload)))
        want = _outcome(lambda: _oracle_stage3_from_dict(copy.deepcopy(payload)))
        assert got == want
        assert got == (
            "exc",
            CascadeWireError,
            "invalid stage3 record in manifest: 'BOGUS' is not a valid FibAnchorKind",
        )

    def test_first_offending_cell_post_init_error_surfaces(self) -> None:
        """cell[0]'s ``__post_init__`` CascadeWireError beats cell[1]'s coercion.

        The construction itself is per-cell in the original law, so even the
        UNWRAPPED ``__post_init__`` raise of an earlier cell wins over a later
        cell's coercion failure.
        """
        payload = _two_cell_stage3_payload()
        payload["refined_cells"][0]["direction_label"] = "sideways"
        payload["refined_cells"][1]["sl_paisa"] = "junk"
        got = _outcome(lambda: _stage3_from_dict(copy.deepcopy(payload)))
        want = _outcome(lambda: _oracle_stage3_from_dict(copy.deepcopy(payload)))
        assert got == want
        assert got == (
            "exc",
            CascadeWireError,
            "Stage3CellRecord.direction_label must be 'bull' or 'bear', got 'sideways'",
        )

    def test_non_dict_payload_parity(self) -> None:
        """A non-dict stage3 entry raises the REAL TypeError, wrapped (frozen)."""
        payload = cast("dict[str, object]", ["not", "a", "dict"])
        got = _outcome(lambda: _stage3_from_dict(payload))
        want = _outcome(lambda: _oracle_stage3_from_dict(payload))
        assert got == want
        assert got[0] == "exc"
        assert got[1] is CascadeWireError


class TestLbtFromDictDifferential:
    """``_lbt_from_dict`` ≡ the verbatim deleted law over hostile payloads.

    Single-record per call at the manifest parse (``tuple(_lbt_from_dict(item)
    for item in ...)``), so no cross-record interleaving exists — the battery
    pins the within-record coercion order + the guard semantics.
    """

    @given(record=_lbt_records(), mutations=st.lists(_lbt_mutations, min_size=0, max_size=3))
    def test_matches_the_deleted_law(
        self, record: LeaderboardTrialsRecord, mutations: list[tuple[str, object]]
    ) -> None:
        payload = cast("dict[str, Any]", copy.deepcopy(_lbt_to_dict(record)))
        for key, value in mutations:
            if value is _DELETE:
                payload.pop(key, None)
            else:
                payload[key] = copy.deepcopy(value)
        got = _outcome(lambda: _lbt_from_dict(copy.deepcopy(payload)))
        want = _outcome(lambda: _oracle_lbt_from_dict(copy.deepcopy(payload)))
        assert got == want

    @given(record=_lbt_records())
    def test_unmutated_payload_round_trips_equal(self, record: LeaderboardTrialsRecord) -> None:
        assert _lbt_from_dict(_lbt_to_dict(record)) == record

    def test_post_init_invariant_violation_passes_the_guard_raw(self) -> None:
        """``n_finite > n_trials`` — the ``__post_init__`` raise is UNWRAPPED."""
        payload = cast("dict[str, Any]", copy.deepcopy(_lbt_to_dict(_lbt())))
        payload["n_trials"] = 5
        payload["n_finite"] = 6
        got = _outcome(lambda: _lbt_from_dict(copy.deepcopy(payload)))
        want = _outcome(lambda: _oracle_lbt_from_dict(copy.deepcopy(payload)))
        assert got == want
        assert got[0] == "exc"
        assert got[1] is CascadeWireError
        assert not str(got[2]).startswith("invalid leaderboard_trials")

    def test_non_dict_payload_parity(self) -> None:
        """A non-dict lbt entry raises the REAL TypeError, wrapped (frozen)."""
        payload = cast("dict[str, object]", ["not", "a", "dict"])
        got = _outcome(lambda: _lbt_from_dict(payload))
        want = _outcome(lambda: _oracle_lbt_from_dict(payload))
        assert got == want
        assert got[0] == "exc"
        assert got[1] is CascadeWireError


@st.composite
def _valid_manifests(draw: st.DrawFn) -> CascadeManifest:
    """A valid CascadeManifest varying every optional leg + config overrides."""
    overrides: dict[str, object] | None = None
    if draw(st.booleans()):
        overrides = {
            "min_profit_factor": draw(_finite_floats),
            "spa_alpha": draw(_finite_floats),
            "dsr_min": draw(_finite_floats),
        }
    roll: tuple[tuple[str, tuple[int, ...]], ...] | None = None
    if draw(st.booleans()):
        ordinals = draw(
            st.sets(st.integers(min_value=0, max_value=800_000), min_size=1, max_size=4)
        )
        roll = ((_FUTURES_ALIAS, tuple(sorted(ordinals))),)
    lbt = draw(st.one_of(st.none(), st.lists(_lbt_records(), min_size=1, max_size=2).map(tuple)))
    return _manifest(
        specs=tuple(draw(st.lists(_valid_specs(), min_size=0, max_size=2))),
        stage3=draw(st.one_of(st.none(), _stage3_records())),
        leaderboard_trials=lbt,
        roll_schedules=roll,
        config_overrides=overrides,
    )


class TestManifestRoundTripDifferential:
    """``manifest_to_json`` byte-parity vs the deleted law + object round-trip."""

    @given(manifest=_valid_manifests())
    def test_bytes_match_the_deleted_law_and_round_trip(self, manifest: CascadeManifest) -> None:
        rendered = manifest_to_json(manifest)
        # Deterministic render: two calls are byte-equal.
        assert manifest_to_json(manifest) == rendered
        # Byte parity vs the verbatim pre-conversion render.
        assert rendered == _oracle_manifest_to_json(manifest)
        # Object round-trip through the Rust-backed parse walk.
        assert manifest_from_json(rendered) == manifest


# ───────────────────────────── math trio ─────────────────────────────────────


def _packed(values: Sequence[float]) -> list[bytes]:
    """IEEE-754 bit patterns (signed zeros + NaN payloads distinguished)."""
    return [struct.pack("<d", v) for v in values]


class TestMathTrioDifferential:
    """The math trio ≡ the verbatim deleted laws, bit-exact."""

    @given(value=st.one_of(st.none(), st.floats(allow_nan=True, allow_infinity=True)))
    def test_finite_bit_exact(self, value: float | None) -> None:
        assert _packed([_finite(value)]) == _packed([_oracle_finite(value)])

    @given(
        equity=st.lists(st.integers(min_value=-(10**18), max_value=10**18), max_size=50),
        start=st.integers(min_value=-(10**18), max_value=10**18),
    )
    def test_per_bar_returns_bit_exact(self, equity: list[int], start: int) -> None:
        # Within ±i64 ONLY (see TestBeyondWordEnvelope). The Rust law delegates
        # each division to CPython int true-division, so >2^53 numerators round
        # exactly as the deleted law did; start == 0 raises ZeroDivisionError
        # identically on a non-empty series (outcome parity covers it).
        got = _outcome(lambda: _per_bar_returns(equity, start))
        want = _outcome(lambda: _oracle_per_bar_returns(equity, start))
        assert got[0] == want[0]
        if got[0] == "ok":
            assert _packed(cast("tuple[float, ...]", got[1])) == _packed(
                cast("tuple[float, ...]", want[1])
            )
        else:
            assert got == want

    @given(returns=st.lists(st.floats(allow_nan=True, allow_infinity=True), max_size=50))
    def test_max_drawdown_bit_exact(self, returns: list[float]) -> None:
        got = _max_drawdown_return_units(returns)
        want = _oracle_max_drawdown_return_units(returns)
        assert _packed([got]) == _packed([want])


class TestBeyondWordEnvelope:
    """The DISCLOSED beyond-word divergence envelope refuses LOUDLY.

    The deleted Python law computed over unbounded ints; the converted law
    refuses beyond-word integers with ``OverflowError`` (never a silently
    different value) — pinned here, scoped OUT of the equality strategies.
    """

    def test_roll_ordinal_beyond_i128_refuses_loudly(self) -> None:
        # The oracle ACCEPTED the same payload — the divergence is refuse-only.
        raw = [[_FUTURES_ALIAS, [_I128_MAX + 1]]]
        assert _oracle_roll_schedules_from_payload(raw) == ((_FUTURES_ALIAS, (_I128_MAX + 1,)),)
        with pytest.raises(OverflowError, match="i128"):
            _roll_schedules_from_payload(raw)
        with pytest.raises(OverflowError, match="i128"):
            _roll_schedules_from_payload([[_FUTURES_ALIAS, [-(2**127) - 1]]])

    def test_per_bar_returns_beyond_i64_refuses_loudly(self) -> None:
        # The FFI carries the equity/start paisa as i64 — a beyond-±i64 int
        # (10**19, 10**40, …) refuses where the oracle computed a float.
        assert _oracle_per_bar_returns([10**40], 10**39) == (9.0,)
        with pytest.raises(OverflowError):
            _per_bar_returns([10**40], 1)
        with pytest.raises(OverflowError):
            _per_bar_returns([_I64_MAX + 1], 1)
        with pytest.raises(OverflowError):
            _per_bar_returns([1], 10**40)
        # Exactly at the word boundary still computes — parity holds.
        assert _packed(_per_bar_returns([_I64_MAX], _I64_MAX)) == _packed(
            _oracle_per_bar_returns([_I64_MAX], _I64_MAX)
        )


class TestDumpsFloatReprLaw:
    """``dumps_sorted`` floats render via ``float.__repr__`` (fix-round MINOR-2).

    CPython ``json.dumps`` renders every float through ``float.__repr__(o)``
    — never the OBJECT's own ``repr`` — so a float SUBCLASS carrying a
    non-JSON ``repr`` (np.float64's exact shape) must serialise as its VALUE.
    Pre-fix the crate rendered the object's ``repr()`` and emitted INVALID
    JSON (``np.float64(1.5)`` in the byte stream).
    """

    @staticmethod
    def _report_with_score(score: float) -> CascadeWireReport:
        return CascadeWireReport(
            sweep_id="c" * 64,
            groups=(
                (
                    "NIFTY",
                    "1minute",
                    _report(
                        verdicts=(_verdict("cand-pass", composite_score=score),),
                        survivor_ids=("cand-pass",),
                        n_input=1,
                        n_survived=1,
                    ),
                ),
            ),
        )

    def test_float_subclass_renders_like_json_dumps(self) -> None:
        class NumpyLikeFloat(float):
            """The np.float64 hazard shape: a float subclass, non-JSON repr."""

            def __repr__(self) -> str:
                return f"np.float64({float.__repr__(self)})"

        report = self._report_with_score(NumpyLikeFloat(1.5))
        rendered = serialize_wire_report(report)
        reference = json.dumps(
            wire_report_to_dict(report), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        assert rendered == reference
        assert b'"composite_score":1.5' in rendered
        json.loads(rendered)  # valid JSON — the pre-fix output was not

    def test_numpy_float64_renders_like_json_dumps(self) -> None:
        np = pytest.importorskip("numpy")
        report = self._report_with_score(np.float64(1.5))
        rendered = serialize_wire_report(report)
        reference = json.dumps(
            wire_report_to_dict(report), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        assert rendered == reference
        assert b'"composite_score":1.5' in rendered
        json.loads(rendered)

    @given(value=st.floats(allow_nan=False, allow_infinity=False))
    def test_plain_floats_byte_unchanged(self, value: float) -> None:
        """Plain floats keep the exact CPython shortest-repr bytes."""
        rendered = serialize_wire_report(self._report_with_score(value))
        assert f'"composite_score":{float.__repr__(value)}'.encode() in rendered


# ───── report projection differential (the d5a297475 dsr_corrected law fold) ──


def _oracle_verdict_to_dict(verdict: CandidateVerdict) -> dict[str, object]:
    """VERBATIM ``_verdict_to_dict`` law at origin/main cbc2f581.

    TRAIN A's DEC-REGIME-MUST-SURVIVE-STRATUM-001 /
    DEC-REGIME-FRAGILITY-GATE-001 added the EMIT-ON-PRESENT filter-#10/#11
    keys (written ONLY when the default-OFF filter evaluated the candidate,
    so a flag-OFF report serialises BYTE-IDENTICALLY to the pre-filter
    shape). The projection law lives in the Rust crate, so this copy is the
    independent Python oracle the crate must match — verbatim from
    cbc2f581's ``cascade_wire.py`` (the 2026-07-20 mega-PR re-forward fold
    of DEC-RUST-CASCADE-RESULTS-001).
    """
    out: dict[str, object] = {
        "candidate_id": verdict.candidate_id,
        "passed": verdict.passed,
        "rejected_at": None if verdict.rejected_at is None else verdict.rejected_at.value,
        "detail": verdict.detail,
        "composite_score": verdict.composite_score,
        "provisional": verdict.provisional,
        "cpcv_pass_rate": verdict.cpcv_pass_rate,
        "dsr_corrected": verdict.dsr_corrected,
    }
    if verdict.extreme_vol_status is not None:
        out["extreme_vol_status"] = verdict.extreme_vol_status
        out["extreme_vol_stats"] = (
            None if verdict.extreme_vol_stats is None else dict(verdict.extreme_vol_stats)
        )
    # The filter-#11 keys ride the SAME EMIT-ON-PRESENT law
    # (DEC-REGIME-FRAGILITY-GATE-001).
    if verdict.regime_fragility_status is not None:
        out["regime_fragility_status"] = verdict.regime_fragility_status
        out["regime_fragility_stats"] = (
            None if verdict.regime_fragility_stats is None else dict(verdict.regime_fragility_stats)
        )
    return out


def _oracle_report_to_dict(report: CascadeReport) -> dict[str, object]:
    """VERBATIM ``_report_to_dict`` law at origin/main cbc2f581.

    Main's DEC-RESULTS-TRUST-HONESTY-001 added the per-verdict
    ``dsr_corrected`` key (ADDITIVE at ``REPORT_VERSION`` 1 — a pre-change
    report simply lacks the key); TRAIN A refactored the verdict projection
    into ``_verdict_to_dict`` (above). The projection law lives in the Rust
    crate, so this copy is the independent Python oracle the crate must
    match — verbatim from cbc2f581's ``cascade_wire.py``, adapted only to
    reference the unchanged type faces by import (the merge-forward fold of
    DEC-RUST-CASCADE-RESULTS-001).
    """
    return {
        "n_input": report.n_input,
        "n_survived": report.n_survived,
        "dsr_n_source": report.dsr_n_source,
        "dsr_n_trials": report.dsr_n_trials,
        "dsr_trial_variance": report.dsr_trial_variance,
        "pool_pbo": report.pool_pbo,
        "survivor_ids": list(report.survivor_ids),
        "stage_survivors": dict(report.stage_survivors),
        "verdicts": [_oracle_verdict_to_dict(verdict) for verdict in report.verdicts],
    }


def _oracle_wire_report_to_dict(report: CascadeWireReport) -> dict[str, object]:
    """VERBATIM ``wire_report_to_dict`` law at origin/main d5a297475."""
    return {
        "version": REPORT_VERSION,
        "sweep_id": report.sweep_id,
        "n_input": report.n_input,
        "n_survived": report.n_survived,
        "groups": [
            {
                "instrument": instrument,
                "tf": tf_label,
                "report": _oracle_report_to_dict(group_report),
            }
            for instrument, tf_label, group_report in report.groups
        ],
    }


class TestReportProjectionDifferential:
    """Crate ``wire_report_to_dict`` ≡ the d5a297475 Python law.

    The dsr_corrected merge-forward fold's genuine re-verification: the
    corpus fixture regeneration captures FROM the live (crate-delegating)
    functions, so THIS differential — crate vs the verbatim d5a297475
    oracle above — is what proves the regenerated expectations equal the
    folded Python law rather than merely echoing the crate.
    """

    def _one_verdict_report(
        self,
        *,
        dsr_corrected: float | None,
        cpcv_pass_rate: float | None,
        composite_score: float | None,
        provisional: bool,
    ) -> CascadeWireReport:
        return CascadeWireReport(
            sweep_id="d" * 64,
            groups=(
                (
                    "NIFTY",
                    "1minute",
                    _report(
                        verdicts=(
                            _verdict(
                                _rid(41),
                                composite_score=composite_score,
                                provisional=provisional,
                                cpcv_pass_rate=cpcv_pass_rate,
                                dsr_corrected=dsr_corrected,
                            ),
                        ),
                        survivor_ids=(_rid(41),),
                        n_input=1,
                        n_survived=1,
                    ),
                ),
            ),
        )

    @given(
        dsr_corrected=st.none() | st.floats(allow_nan=False, allow_infinity=True),
        cpcv_pass_rate=st.none() | st.floats(allow_nan=False, allow_infinity=True),
        composite_score=st.none() | st.floats(allow_nan=False, allow_infinity=True),
        provisional=st.booleans(),
    )
    def test_matches_the_folded_law(
        self,
        dsr_corrected: float | None,
        cpcv_pass_rate: float | None,
        composite_score: float | None,
        provisional: bool,
    ) -> None:
        report = self._one_verdict_report(
            dsr_corrected=dsr_corrected,
            cpcv_pass_rate=cpcv_pass_rate,
            composite_score=composite_score,
            provisional=provisional,
        )
        assert wire_report_to_dict(report) == _oracle_wire_report_to_dict(report)

    @given(dsr_corrected=st.none() | st.floats(allow_nan=False, allow_infinity=False))
    def test_serialized_bytes_match_the_oracle_dumps(self, dsr_corrected: float | None) -> None:
        """Byte parity: crate render ≡ ``json.dumps`` of the ORACLE dict."""
        report = self._one_verdict_report(
            dsr_corrected=dsr_corrected,
            cpcv_pass_rate=0.5,
            composite_score=1.25,
            provisional=False,
        )
        expected = json.dumps(
            _oracle_wire_report_to_dict(report),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        assert serialize_wire_report(report) == expected

    def test_corpus_reports_match_the_oracle(self) -> None:
        """Every frozen-corpus wire report projects law-identically."""
        for name, report in wire_report_cases():
            assert wire_report_to_dict(report) == _oracle_wire_report_to_dict(report), name

    _STATS = st.none() | st.dictionaries(
        st.text(min_size=1, max_size=8),
        st.none()
        | st.integers(min_value=-(2**53), max_value=2**53)
        # allow_infinity=False: the byte-parity leg renders through
        # `json.dumps(..., allow_nan=False)` — an inf stats value refuses
        # loudly on BOTH arms (the serialize law), so it is out of the
        # equality domain here.
        | st.floats(allow_nan=False, allow_infinity=False),
        max_size=4,
    )

    @given(
        extreme_vol_status=st.none() | st.sampled_from(["ok", "fail", "unproven_current_regime"]),
        extreme_vol_stats=_STATS,
        regime_fragility_status=st.none() | st.sampled_from(["ok", "fail", "abstain_no_evidence"]),
        regime_fragility_stats=_STATS,
    )
    def test_regime_evidence_keys_match_the_folded_law(
        self,
        extreme_vol_status: str | None,
        extreme_vol_stats: dict[str, object] | None,
        regime_fragility_status: str | None,
        regime_fragility_stats: dict[str, object] | None,
    ) -> None:
        """The cbc2f581 EMIT-ON-PRESENT filter-#10/#11 keys: crate ≡ oracle.

        The TRAIN A fold (DEC-REGIME-MUST-SURVIVE-STRATUM-001 /
        DEC-REGIME-FRAGILITY-GATE-001): a status-``None`` verdict emits
        NEITHER key (flag-OFF reports serialise byte-identically to the
        pre-filter shape — the both-None draw pins it), a status-carrying
        verdict emits status + stats (stats ``None`` passes through as JSON
        null; a mapping rides CPython's own ``dict()``).
        """
        verdict = _verdict(
            _rid(43),
            extreme_vol_status=extreme_vol_status,
            extreme_vol_stats=extreme_vol_stats,
            regime_fragility_status=regime_fragility_status,
            regime_fragility_stats=regime_fragility_stats,
        )
        report = CascadeWireReport(
            sweep_id="d" * 64,
            groups=(
                (
                    "NIFTY",
                    "1minute",
                    _report(
                        verdicts=(verdict,),
                        survivor_ids=(_rid(43),),
                        n_input=1,
                        n_survived=1,
                    ),
                ),
            ),
        )
        got = wire_report_to_dict(report)
        assert got == _oracle_wire_report_to_dict(report)
        vd = got["groups"][0]["report"]["verdicts"][0]  # type: ignore[index]
        if extreme_vol_status is None:
            assert "extreme_vol_status" not in vd and "extreme_vol_stats" not in vd
        if regime_fragility_status is None:
            assert "regime_fragility_status" not in vd and "regime_fragility_stats" not in vd
        expected = json.dumps(
            _oracle_wire_report_to_dict(report),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        assert serialize_wire_report(report) == expected
