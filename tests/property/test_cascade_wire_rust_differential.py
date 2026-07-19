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
law, so ``BaseException`` is caught on both sides and compared).

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
    CascadeManifest,
    CascadeTupleSpec,
    CascadeWireError,
    LeaderboardTrialsRecord,
    Stage3ManifestRecord,
    _finite,
    _max_drawdown_return_units,
    _per_bar_returns,
    _roll_schedules_from_payload,
    _spec_from_dict,
    _spec_to_dict,
    cascade_config_to_dict,
    manifest_from_json,
    manifest_to_json,
)
from brutex.execution.fill_mode import FillMode
from brutex.strategies.types import FibAnchorKind, PriceSource, price_source_params_fragment
from brutex.vocabulary.bits import TOTAL_CONDITIONS
from hypothesis import given
from hypothesis import strategies as st

from tests.golden._cascade_wire_parity_corpus import _lbt, _manifest, _rid, _spec, _stage3_cell

_MAX_MASK = (1 << TOTAL_CONDITIONS) - 1
_I64_MAX = 2**63 - 1
_I128_MAX = 2**127 - 1
_FUTURES_ALIAS = "NSE-NIFTY-FUT"


def _outcome(fn: Callable[[], object]) -> tuple[object, ...]:
    """Run *fn* to an outcome: ``("ok", value)`` or ``("exc", type, str)``.

    ``BaseException`` is deliberate — raw-escape parity (the frozen law's
    ``IndexError``/``AttributeError`` passthroughs) IS the property.
    """
    try:
        return ("ok", fn())
    except BaseException as exc:
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
