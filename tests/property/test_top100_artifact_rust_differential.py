"""Hypothesis differentials for the Rust-hosted top100_artifact laws.

DEC-RUST-CASCADE-RESULTS-001 phase B (slice 2b). The converted Python law
bodies are DELETED, so each property here drives the Rust-backed public API
against a VERBATIM in-test copy of the pre-conversion Python law (the
retained independent-oracle-in-test convention — the slice-1/2a
``test_cascade_wire_rust_differential.py`` precedent, R45): hypothesis
mutates valid inputs with hostile edits, BOTH sides run to an OUTCOME, and
the outcomes must be EXACTLY equal — a bit-exact canonicalised payload on
success (bool-before-int tags + IEEE-754 bit patterns, so ``True`` never
puns ``1`` and ``-0.0`` never puns ``0.0`` in the equality), the SAME
exception TYPE and FULL ``str`` on failure (the raw ``JSONDecodeError``
escapes ARE the frozen law, so ``BaseException`` is caught on both sides).

Oracle copies are verbatim from commit 68bc463a6 (the pre-conversion
module), adapted ONLY to reference the unchanged type faces
(``Top100Candidate`` / ``Top100Artifact`` / ``Top100Error`` / the
constants) and the still-Python §11-COMMON delegations (``resolve_preset``
/ ``rank_candidates`` / ``matched_condition_names``) by import — plus the
oracle ``load_top100``'s ``_invalid`` recording reasons into a list instead
of logging (the shim side's reasons are captured off the REAL
``top100.load_invalid`` events).

Multi-defect construction-order shapes are generated + pinned deliberately
(the slice-2a MINOR-1 lesson): payloads carrying several defects at once
must surface the SAME defect first on both sides — the provider-argument
evaluation order, the metrics-before-regime JSON parse order, and the
dup-refusal-before-preset-WARN order are each asserted explicitly.

Disclosed divergence envelope (scoped OUT of the equality strategies and
pinned separately in ``TestBeyondWordEnvelope``): gate-field INT extraction
(``total_trades`` / ``max_consecutive_losses``) beyond ±i64 refuses LOUDLY
(``OverflowError``) where the deleted Python law compared unbounded ints —
the DEC's refuse-never-diverge posture. ``_dumps`` itself has NO int-width
envelope (ints render via CPython, probed beyond 2**200 in-suite).
"""

from __future__ import annotations

import copy
import functools
import hashlib
import json
import math
import struct
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest
from brutex.engine.top100_artifact import (
    CASCADE_STATE_JOINED,
    CASCADE_STATE_NO_REPORT,
    CASCADE_STATE_SWEEP_ID_MISMATCH,
    ENTRY_REQUIRED_KEYS,
    GATE_MAX_CONSECUTIVE_LOSSES,
    GATE_MAX_DRAWDOWN_PCT,
    GATE_MIN_PROFIT_FACTOR,
    GATE_MIN_TRADES,
    GATE_MIN_WIN_LOSS_RATIO,
    GATE_MIN_WIN_RATE,
    TOP100_FILENAME,
    TOP100_LIMIT,
    TOP100_PRESETS,
    TOP100_SCHEMA_VERSION,
    TOP100_STAGE,
    TOP100_STAMP_FILENAME,
    PoolCounts,
    Top100Artifact,
    Top100Candidate,
    Top100Error,
    _cascade_payload,
    _dumps,
    _loads_object,
    build_top100,
    load_top100,
    passes_hard_gates,
    rank_candidates,
    resolve_preset,
)
from brutex.engine.twin_detect import TwinFamilies
from brutex.vocabulary.describe import matched_condition_names
from hypothesis import given
from hypothesis import strategies as st
from structlog.testing import capture_logs

from tests.fakes.wp4_twin_fixtures import fingerprint_seed
from tests.fakes.wp5_depth_fixtures import make_candidate, run_id_of
from tests.golden._top100_artifact_parity_corpus import (
    _counts,
    _empty_families,
    _join,
    _view,
)

if TYPE_CHECKING:
    from brutex.engine.cascade_join import CascadeJoin, CascadeVerdictView

_RUN = "f0" * 32
_I64_MAX = 2**63 - 1
_ORACLE_MASK_HEX_WIDTH = 32
_ORACLE_SHA256_HEX_LEN = 64
_STAMP_KEYS = frozenset({"stage", "run_id", "content_hash", "produced_at_utc"})
_FIXED_PRODUCED_AT = "2026-07-19T00:00:00+00:00"


# ───────────────────────── outcome + canonicalisation ────────────────────────


def _canon(obj: object) -> object:
    """Bit-exact canonical form: bool≠int, float by IEEE bits, order-free dicts."""
    if isinstance(obj, bool):
        return ("bool", obj)
    if isinstance(obj, int):
        return ("int", obj)
    if isinstance(obj, float):
        return ("float", struct.pack("<d", obj))
    if obj is None or isinstance(obj, str):
        return obj
    if isinstance(obj, Mapping):
        return ("dict", tuple(sorted((str(k), _canon(v)) for k, v in obj.items())))
    if isinstance(obj, (list, tuple)):
        return ("list", tuple(_canon(v) for v in obj))
    return ("obj", type(obj).__name__, repr(obj))


def _outcome(
    fn: Callable[[], object], canon: Callable[[object], object] = lambda v: v
) -> tuple[object, ...]:
    """Run *fn* to an outcome: ``("ok", canon(value))`` or ``("exc", type, str)``.

    ``BaseException`` is deliberate — raw-escape parity (the frozen law's
    ``JSONDecodeError`` passthroughs) IS the property.
    """
    try:
        return ("ok", canon(fn()))
    except BaseException as exc:  # outcome parity is the property
        return ("exc", type(exc), str(exc))


def _canon_artifact(value: object) -> object:
    art = cast(Top100Artifact, value)
    return _canon(art.to_payload())


# ───────────────────── VERBATIM pre-conversion oracles (68bc463a6) ───────────


def _oracle_dumps(payload: object) -> str:
    """VERBATIM pre-conversion ``_dumps`` (the ONE deterministic render law)."""
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


def _oracle_passes_hard_gates(row: Any, agg: Any) -> bool:
    """VERBATIM pre-conversion ``passes_hard_gates`` (the 5-gate read order)."""
    if row.total_trades < GATE_MIN_TRADES:
        return False
    if row.profit_factor < GATE_MIN_PROFIT_FACTOR:
        return False
    if row.max_drawdown_pct > GATE_MAX_DRAWDOWN_PCT:
        return False
    wlr = agg.win_loss_ratio if agg.win_loss_ratio is not None else 0.0
    if row.win_rate < GATE_MIN_WIN_RATE and wlr < GATE_MIN_WIN_LOSS_RATIO:
        return False
    return bool(agg.max_consecutive_losses <= GATE_MAX_CONSECUTIVE_LOSSES)


def _oracle_loads_object(rendered: str) -> dict[str, object]:
    """VERBATIM pre-conversion ``_loads_object`` (raw JSONDecodeError escape)."""
    payload = json.loads(rendered)
    if not isinstance(payload, dict):  # defensive: the agg writer only emits objects
        raise Top100Error(f"agg JSON column did not decode to an object: {rendered!r}")
    return payload


def _oracle_metrics_payload(
    row: Any, agg: Any, *, cpcv_pass_rate: float | None = None
) -> dict[str, object]:
    """VERBATIM pre-conversion ``_metrics_payload`` (dict-literal walk order)."""
    payload: dict[str, object] = {
        "net_pnl_paisa": row.net_pnl_paisa,
        "gross_profit_paisa": row.gross_profit_paisa,
        "gross_loss_paisa": row.gross_loss_paisa,
        "max_drawdown_paisa": row.max_drawdown_paisa,
        "expectancy_paisa": row.expectancy_paisa,
        "total_trades": row.total_trades,
        "winning_trades": row.winning_trades,
        "losing_trades": row.losing_trades,
        "sharpe_ratio": row.sharpe_ratio,
        "sortino_ratio": row.sortino_ratio,
        "calmar_ratio": row.calmar_ratio,
        "profit_factor": row.profit_factor,
        "win_rate": row.win_rate,
        "max_drawdown_pct": row.max_drawdown_pct,
        "deflated_sharpe": row.deflated_sharpe,
        "avg_entry_price": str(row.avg_entry_price),
        "avg_exit_price": str(row.avg_exit_price),
        "composite_score": agg.composite_score,
        "win_loss_ratio": agg.win_loss_ratio,
        "recovery_factor": agg.recovery_factor,
        "max_consecutive_losses": agg.max_consecutive_losses,
        "max_consecutive_wins": agg.max_consecutive_wins,
        "mae_avg_paisa": agg.mae_avg_paisa,
        "mae_worst_paisa": agg.mae_worst_paisa,
        "mae_p95_paisa": agg.mae_p95_paisa,
        "mfe_avg_paisa": agg.mfe_avg_paisa,
        "mfe_best_paisa": agg.mfe_best_paisa,
        "mfe_p95_paisa": agg.mfe_p95_paisa,
        "mae_1m_avg_paisa": agg.mae_1m_avg_paisa,
        "mae_1m_worst_paisa": agg.mae_1m_worst_paisa,
        "mae_1m_p95_paisa": agg.mae_1m_p95_paisa,
        "mfe_1m_avg_paisa": agg.mfe_1m_avg_paisa,
        "mfe_1m_best_paisa": agg.mfe_1m_best_paisa,
        "mfe_1m_p95_paisa": agg.mfe_1m_p95_paisa,
        "trades_with_1m": agg.trades_with_1m,
        "max_dd_minute_path_paisa": agg.max_dd_minute_path_paisa,
        "max_dd_minute_path_pct": agg.max_dd_minute_path_pct,
        "equity_status": agg.equity_status,
        "worst5_days": json.loads(agg.worst5_days_json),
        "exits": {
            "stop_loss": agg.exits_stop_loss,
            "take_profit": agg.exits_take_profit,
            "signal": agg.exits_signal,
            "forced_1520": agg.exits_forced_1520,
            "end_of_data": agg.exits_end_of_data,
            "session_gap": agg.exits_session_gap,
        },
        "fingerprint": agg.fingerprint,
    }
    if cpcv_pass_rate is not None:
        payload["cpcv_pass_pct"] = cpcv_pass_rate * 100.0
    return payload


def _oracle_regime_payload(agg: Any) -> dict[str, object]:
    """VERBATIM pre-conversion ``_regime_payload`` (JSON column parse order)."""
    return {
        "by_year": _oracle_loads_object(agg.pnl_by_year_json),
        "by_trend": _oracle_loads_object(agg.pnl_by_trend_json),
        "one_regime_wonder": agg.one_regime_wonder,
        "regime_wonder_axis": agg.regime_wonder_axis,
        "entry_hour_hist": _oracle_loads_object(agg.entry_hour_hist_json),
        "event_days": {
            "counts": _oracle_loads_object(agg.event_day_counts_json),
            "pnl": _oracle_loads_object(agg.event_day_pnl_json),
        },
    }


def _oracle_vix_band_payload(agg: Any) -> dict[str, object]:
    """VERBATIM pre-conversion ``_vix_band_payload`` (all-three-present law)."""
    win: dict[str, float] | None = None
    if agg.vix_win_min is not None and agg.vix_win_avg is not None and agg.vix_win_max is not None:
        win = {"min": agg.vix_win_min, "avg": agg.vix_win_avg, "max": agg.vix_win_max}
    loss: dict[str, float] | None = None
    if (
        agg.vix_loss_min is not None
        and agg.vix_loss_avg is not None
        and agg.vix_loss_max is not None
    ):
        loss = {"min": agg.vix_loss_min, "avg": agg.vix_loss_avg, "max": agg.vix_loss_max}
    return {
        "lo": agg.vix_band_lo,
        "hi": agg.vix_band_hi,
        "stamped": agg.vix_stamped_trades,
        "missing": agg.vix_missing_trades,
        "win": win,
        "loss": loss,
    }


def _oracle_cascade_payload(
    run_id: str, *, join: CascadeJoin | None, state: str
) -> tuple[dict[str, object], float | None]:
    """VERBATIM pre-conversion ``_cascade_payload`` (the verdict law table)."""
    if state != CASCADE_STATE_JOINED or join is None:
        return {"status": "not_validated", "reason": state}, None
    if run_id in join.ambiguous_ids:
        return {"status": "not_validated", "reason": "duplicate_candidate_id"}, None
    verdict: CascadeVerdictView | None = join.by_candidate.get(run_id)
    if verdict is None:
        return {"status": "not_validated", "reason": "not_in_cascade_pool"}, None
    if verdict.passed:
        status = "provisional_pass" if verdict.provisional else "pass"
        return {
            "status": status,
            "rejected_at": None,
            "detail": verdict.detail,
            "cascade_composite": verdict.cascade_composite,
            "provisional": verdict.provisional,
            "cpcv_pass_rate": verdict.cpcv_pass_rate,
            "survivor_rank": verdict.survivor_rank,
        }, verdict.cpcv_pass_rate
    return {
        "status": "fail",
        "rejected_at": verdict.rejected_at,
        "detail": verdict.detail,
        "cascade_composite": None,
        "provisional": False,
        "cpcv_pass_rate": verdict.cpcv_pass_rate,
        "survivor_rank": None,
    }, verdict.cpcv_pass_rate


def _oracle_entry_payload(
    rank: int,
    candidate: Top100Candidate,
    *,
    family_id: str,
    family_members: Sequence[str],
    reasons: Mapping[str, str] | None,
    margin_roi: Mapping[str, object] | None,
    join: CascadeJoin | None = None,
    join_state: str = CASCADE_STATE_NO_REPORT,
) -> dict[str, object]:
    """VERBATIM pre-conversion ``_entry_payload`` (dict-literal walk order)."""
    row = candidate.row
    cascade, cpcv_rate = _oracle_cascade_payload(row.run_id, join=join, state=join_state)
    return {
        "rank": rank,
        "run_id": row.run_id,
        "mask_hex": f"{row.mask_bits:0{_ORACLE_MASK_HEX_WIDTH}x}",
        "conditions": list(matched_condition_names(row.mask_bits)),
        "instrument": row.instrument,
        "tf": row.tf,
        "direction": row.direction,
        "arm": {"sl_paisa": candidate.sl_paisa, "tp_paisa": candidate.tp_paisa},
        "metrics": _oracle_metrics_payload(row, candidate.agg, cpcv_pass_rate=cpcv_rate),
        "regime": _oracle_regime_payload(candidate.agg),
        "vix_band": _oracle_vix_band_payload(candidate.agg),
        "family_id": family_id,
        "family_members": list(family_members),
        "reasons": None if reasons is None else dict(reasons),
        "margin_roi": None if margin_roi is None else dict(margin_roi),
        "equity_ref": {"tuple": candidate.tuple_ref, "sidecar": "equity"},
        "cascade": cascade,
    }


def _oracle_build_top100(
    candidates: Sequence[Top100Candidate],
    *,
    run_id: str,
    preset: str,
    families: TwinFamilies,
    counts: PoolCounts,
    data_snapshot_hashes: Sequence[str],
    baselines: Mapping[str, str],
    reason_provider: Callable[[int], Mapping[str, str] | None] | None = None,
    margin_roi_provider: Callable[[Top100Candidate], Mapping[str, object] | None] | None = None,
    cascade_join: CascadeJoin | None = None,
    rank_fn: Callable[..., tuple[Top100Candidate, ...]] = rank_candidates,
) -> Top100Artifact:
    """VERBATIM pre-conversion ``build_top100`` over the oracle sub-laws.

    ``rank_fn`` defaults to the shim's still-Python ``rank_candidates`` (the
    presets carve-out — a consumed law, not a conversion target); it is
    injectable so the raising-rank propagation test can drive BOTH sides
    with the same hostile callable. No log seam is adapted: the oracle emits
    the same ``_LOG`` events the deleted body did (log parity is corpus-
    frozen; outcome parity is this file's property).
    """
    import structlog

    log = structlog.get_logger("brutex.engine.top100_artifact")
    seen: set[str] = set()
    for candidate in candidates:
        if candidate.row.run_id in seen:
            raise Top100Error(f"build_top100: duplicate candidate run_id {candidate.row.run_id!r}")
        seen.add(candidate.row.run_id)
    resolved = resolve_preset(preset)

    if cascade_join is None:
        join_state = CASCADE_STATE_NO_REPORT
        join_for_entries: CascadeJoin | None = None
    elif cascade_join.sweep_id != run_id:
        log.warning(
            "top100.cascade_sweep_id_mismatch",
            run_id=run_id,
            report_sweep_id=cascade_join.sweep_id,
            note="stale cascade report — entries stay not_validated",
        )
        join_state = CASCADE_STATE_SWEEP_ID_MISMATCH
        join_for_entries = None
    else:
        join_state = CASCADE_STATE_JOINED
        join_for_entries = cascade_join

    passers = [c for c in candidates if _oracle_passes_hard_gates(c.row, c.agg)]

    by_family: dict[str, list[Top100Candidate]] = {}
    for candidate in passers:
        family_id = families.family_by_run_id.get(candidate.row.run_id, candidate.row.run_id)
        by_family.setdefault(family_id, []).append(candidate)
    representatives: list[Top100Candidate] = []
    members_of: dict[str, tuple[str, ...]] = {}
    for family_id, members in by_family.items():
        chosen = min(
            members,
            key=lambda c: (
                0 if c.row.run_id == family_id else 1,
                c.row.mask_bits.bit_count(),
                c.row.run_id,
            ),
        )
        listed = families.members_by_family.get(
            family_id, tuple(sorted(c.row.run_id for c in members))
        )
        representatives.append(chosen)
        members_of[chosen.row.run_id] = listed
    families_collapsed = len(passers) - len(representatives)

    ranked = rank_fn(representatives, preset=resolved)[:TOP100_LIMIT]
    entries: list[Mapping[str, object]] = []
    for position, candidate in enumerate(ranked, start=1):
        run = candidate.row.run_id
        entries.append(
            _oracle_entry_payload(
                position,
                candidate,
                family_id=families.family_by_run_id.get(run, run),
                family_members=members_of[run],
                reasons=(
                    None if reason_provider is None else reason_provider(candidate.row.mask_bits)
                ),
                margin_roi=(
                    None if margin_roi_provider is None else margin_roi_provider(candidate)
                ),
                join=join_for_entries,
                join_state=join_state,
            )
        )

    source: dict[str, object] = {
        "data_snapshot_hashes": sorted(set(data_snapshot_hashes)),
        "stores": counts.stores,
        "pool": len(candidates),
        "gate_passers": len(passers),
        "excluded_no_aggs": counts.excluded_no_aggs,
        "zero_trade_excluded": counts.zero_trade_excluded,
        "parity_excluded": counts.parity_excluded,
        "families_collapsed": families_collapsed,
        "reasons_wired": reason_provider is not None,
        "margin_roi_wired": margin_roi_provider is not None,
    }
    if join_state == CASCADE_STATE_JOINED and join_for_entries is not None:
        cascade_meta: dict[str, object] = {
            "state": join_state,
            "sweep_id": join_for_entries.sweep_id,
            "n_input": join_for_entries.n_input,
            "n_survived": join_for_entries.n_survived,
        }
    else:
        cascade_meta = {
            "state": join_state,
            "sweep_id": None if cascade_join is None else cascade_join.sweep_id,
            "n_input": None,
            "n_survived": None,
        }
    return Top100Artifact(
        schema_version=TOP100_SCHEMA_VERSION,
        run_id=run_id,
        preset=resolved,
        source=source,
        baselines=dict(baselines),
        entries=tuple(entries),
        cascade_join=cascade_meta,
    )


def _oracle_load_top100(runs_dir: Path, run_id: str) -> tuple[Top100Artifact | None, list[str]]:
    """VERBATIM pre-conversion ``load_top100`` — ``_invalid`` records reasons."""
    reasons: list[str] = []

    def _invalid(reason: str) -> None:
        reasons.append(reason)

    def _run() -> Top100Artifact | None:
        target_dir = runs_dir / run_id
        try:
            stamp_raw = (target_dir / TOP100_STAMP_FILENAME).read_bytes()
        except OSError:
            _invalid("stamp_unreadable")
            return None
        try:
            stamp = json.loads(stamp_raw)
        except ValueError:
            _invalid("stamp_not_json")
            return None
        if not isinstance(stamp, dict) or set(stamp.keys()) != _STAMP_KEYS:
            _invalid("stamp_bad_shape")
            return None
        if stamp["stage"] != TOP100_STAGE:
            _invalid("stamp_wrong_stage")
            return None
        if stamp["run_id"] != run_id:
            _invalid("stamp_wrong_run_id")
            return None
        content_hash = stamp["content_hash"]
        if not isinstance(content_hash, str) or len(content_hash) != _ORACLE_SHA256_HEX_LEN:
            _invalid("stamp_bad_content_hash")
            return None
        if not isinstance(stamp["produced_at_utc"], str):
            _invalid("stamp_bad_produced_at")
            return None

        try:
            raw = (target_dir / TOP100_FILENAME).read_bytes()
        except OSError:
            _invalid("artifact_unreadable")
            return None
        if hashlib.sha256(raw).hexdigest() != content_hash:
            _invalid("artifact_hash_mismatch")
            return None
        try:
            payload = json.loads(raw)
        except ValueError:
            _invalid("artifact_not_json")
            return None
        if not isinstance(payload, dict):
            _invalid("artifact_not_object")
            return None
        if payload.get("schema_version") != TOP100_SCHEMA_VERSION:
            _invalid("schema_version_mismatch")
            return None
        if payload.get("run_id") != run_id:
            _invalid("artifact_wrong_run_id")
            return None
        preset = payload.get("preset")
        if not isinstance(preset, str) or not preset:
            _invalid("bad_preset")
            return None
        source = payload.get("source")
        if not isinstance(source, dict):
            _invalid("bad_source")
            return None
        baselines = payload.get("baselines")
        if not isinstance(baselines, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in baselines.items()
        ):
            _invalid("bad_baselines")
            return None
        entries_raw = payload.get("entries")
        if not isinstance(entries_raw, list):
            _invalid("bad_entries")
            return None
        entries: list[Mapping[str, object]] = []
        for position, item in enumerate(entries_raw, start=1):
            if not isinstance(item, dict) or not ENTRY_REQUIRED_KEYS.issubset(item.keys()):
                _invalid("bad_entry_shape")
                return None
            if item["rank"] != position:
                _invalid("bad_entry_rank_order")
                return None
            entries.append(item)
        cascade_join_raw = payload.get("cascade_join")
        return Top100Artifact(
            schema_version=TOP100_SCHEMA_VERSION,
            run_id=run_id,
            preset=preset,
            source=source,
            baselines={str(key): str(value) for key, value in baselines.items()},
            entries=tuple(entries),
            cascade_join=cascade_join_raw if isinstance(cascade_join_raw, dict) else None,
        )

    return _run(), reasons


# ───────────────────────────── shared strategies ─────────────────────────────

_json_floats = st.one_of(
    st.floats(allow_nan=True, allow_infinity=True),
    st.sampled_from(
        [
            -0.0,
            0.0,
            5e-324,  # smallest subnormal
            2.2250738585072014e-308,  # smallest normal
            1e16,
            9007199254740993.0,  # beyond-2^53 rounding shape
            math.pi,
            -1e308,
        ]
    ),
)
#: Ints deliberately UNBOUNDED — `_dumps` renders via CPython, no word envelope.
_json_ints = st.one_of(
    st.integers(),
    st.sampled_from([2**62, 2**127, -(2**127) - 1, 2**200, -(2**200)]),
)
_bmp_astral_text = st.text(
    alphabet=st.characters(codec="utf-8", exclude_characters="\x00"), max_size=12
)
_surrogate_text = st.sampled_from(
    ["\ud800", "BAD-\ud800-SURROGATE", "tail-\udfff", "😀-unpaired-\ud83d"]
)
_json_text = st.one_of(_bmp_astral_text, _surrogate_text, st.sampled_from(["", "π", "😀", "a\tb"]))
#: Keys stay lone-surrogate-free str (the disclosed key envelope — every
#: production payload key is writer-minted ASCII or a json.loads product).
_json_keys = st.one_of(
    st.text(alphabet=st.characters(codec="utf-8", exclude_characters="\x00"), max_size=6),
    st.sampled_from(["", "a", "Z", "π", "é", "😀", "0", " "]),
)
_json_scalars = st.one_of(st.none(), st.booleans(), _json_ints, _json_floats, _json_text)
_json_trees = st.recursive(
    _json_scalars,
    lambda children: st.one_of(
        st.lists(children, max_size=4),
        st.dictionaries(_json_keys, children, max_size=4),
    ),
    max_leaves=20,
)


class TestDumpsDifferential:
    """``_dumps`` ≡ the verbatim deleted render law over hostile JSON trees."""

    @given(tree=_json_trees)
    def test_matches_the_deleted_law(self, tree: object) -> None:
        got = _outcome(lambda: _dumps(tree))
        want = _outcome(lambda: _oracle_dumps(tree))
        assert got == want

    @given(tree=_json_trees)
    def test_deterministic_render(self, tree: object) -> None:
        assert _outcome(lambda: _dumps(tree)) == _outcome(lambda: _dumps(tree))

    def test_lone_surrogate_value_escapes_like_cpython(self) -> None:
        payload = {"instrument": "BAD-\ud800-SURROGATE", "tail": "\udfff"}
        rendered = _dumps(payload)
        assert rendered == _oracle_dumps(payload)
        assert "\\ud800" in rendered
        assert rendered.isascii()

    def test_huge_ints_render_exactly_beyond_i128(self) -> None:
        """No int-width envelope for the render law (CPython-delegated)."""
        for value in (2**127, -(2**127) - 1, 2**200):
            assert _dumps({"v": value}) == _oracle_dumps({"v": value})

    def test_nan_and_inf_refusal_messages_verbatim(self) -> None:
        for value in (float("nan"), float("inf"), float("-inf")):
            payload = {"a": value}
            got = _outcome(functools.partial(_dumps, payload))
            want = _outcome(functools.partial(_oracle_dumps, payload))
            assert got == want
            assert got[0] == "exc"
            assert got[1] is ValueError


class TestLoadsObjectDifferential:
    """``_loads_object`` ≡ the verbatim deleted law over hostile documents."""

    @given(doc=st.one_of(_json_trees.map(_oracle_dumps), st.text(max_size=20)))
    def test_matches_the_deleted_law(self, doc: str) -> None:
        got = _outcome(lambda: _loads_object(doc), canon=_canon)
        want = _outcome(lambda: _oracle_loads_object(doc), canon=_canon)
        assert got == want

    def test_raw_json_decode_error_escapes_unwrapped(self) -> None:
        got = _outcome(lambda: _loads_object("{not json"))
        want = _outcome(lambda: _oracle_loads_object("{not json"))
        assert got == want
        assert got[0] == "exc"
        assert got[1] is json.JSONDecodeError

    def test_non_object_repr_message_verbatim(self) -> None:
        got = _outcome(lambda: _loads_object("[1,2]"))
        assert got == _outcome(lambda: _oracle_loads_object("[1,2]"))
        assert got == (
            "exc",
            Top100Error,
            "agg JSON column did not decode to an object: '[1,2]'",
        )


# ───────────────────────────── hard gates ────────────────────────────────────


def _bnd(center: float) -> list[float]:
    return [center, math.nextafter(center, math.inf), math.nextafter(center, -math.inf)]


_gate_floats = st.one_of(
    st.floats(allow_nan=True, allow_infinity=True),
    st.sampled_from(
        _bnd(GATE_MIN_PROFIT_FACTOR)
        + _bnd(GATE_MAX_DRAWDOWN_PCT)
        + _bnd(GATE_MIN_WIN_RATE)
        + _bnd(GATE_MIN_WIN_LOSS_RATIO)
        + [-0.0, 0.0]
    ),
)
_gate_ints = st.one_of(
    st.integers(min_value=-_I64_MAX - 1, max_value=_I64_MAX),
    st.sampled_from(
        [
            GATE_MIN_TRADES - 1,
            GATE_MIN_TRADES,
            GATE_MAX_CONSECUTIVE_LOSSES,
            GATE_MAX_CONSECUTIVE_LOSSES + 1,
        ]
    ),
)


class TestPassesHardGatesDifferential:
    """``passes_hard_gates`` ≡ the verbatim deleted law over the float plane.

    Duck-typed row/agg attrs on BOTH sides — the crate's getattr walk and
    the deleted body read the same six attributes, so the FULL IEEE plane
    (NaN / ±inf / signed zeros / boundary ULPs) is in scope, wider than any
    constructible ``ResultRow`` (which pins finiteness).
    """

    @given(
        total_trades=_gate_ints,
        profit_factor=_gate_floats,
        max_drawdown_pct=_gate_floats,
        win_rate=_gate_floats,
        wlr=st.one_of(st.none(), _gate_floats),
        max_losses=_gate_ints,
    )
    def test_matches_the_deleted_law(
        self,
        total_trades: int,
        profit_factor: float,
        max_drawdown_pct: float,
        win_rate: float,
        wlr: float | None,
        max_losses: int,
    ) -> None:
        row = SimpleNamespace(
            total_trades=total_trades,
            profit_factor=profit_factor,
            max_drawdown_pct=max_drawdown_pct,
            win_rate=win_rate,
        )
        agg = SimpleNamespace(win_loss_ratio=wlr, max_consecutive_losses=max_losses)
        got = passes_hard_gates(cast(Any, row), cast(Any, agg))
        want = _oracle_passes_hard_gates(row, agg)
        assert got is want

    @given(
        profit_factor=st.sampled_from([*_bnd(GATE_MIN_PROFIT_FACTOR), 1.5]),
        max_drawdown_pct=st.sampled_from([*_bnd(GATE_MAX_DRAWDOWN_PCT), 10.0]),
        win_rate=st.sampled_from([*_bnd(GATE_MIN_WIN_RATE), 0.625]),
        wlr=st.sampled_from([None, *_bnd(GATE_MIN_WIN_LOSS_RATIO)]),
        max_losses=st.sampled_from(
            [3, GATE_MAX_CONSECUTIVE_LOSSES, GATE_MAX_CONSECUTIVE_LOSSES + 1]
        ),
        total_trades=st.sampled_from([GATE_MIN_TRADES - 1, GATE_MIN_TRADES, 40]),
    )
    def test_real_records_at_the_boundaries(
        self,
        profit_factor: float,
        max_drawdown_pct: float,
        win_rate: float,
        wlr: float | None,
        max_losses: int,
        total_trades: int,
    ) -> None:
        """The same law over REAL slotted dataclass records (extraction path)."""
        candidate = make_candidate(
            1,
            fingerprint=fingerprint_seed(1),
            row_overrides={
                "profit_factor": profit_factor,
                "max_drawdown_pct": max_drawdown_pct,
                "win_rate": win_rate,
                "total_trades": total_trades,
                "winning_trades": 10,
                "losing_trades": 10,
            },
            agg_overrides={"win_loss_ratio": wlr, "max_consecutive_losses": max_losses},
        )
        got = passes_hard_gates(candidate.row, candidate.agg)
        want = _oracle_passes_hard_gates(candidate.row, candidate.agg)
        assert got is want


class TestBeyondWordEnvelope:
    """The DISCLOSED beyond-word divergence envelope refuses LOUDLY.

    Gate INT extraction is i64-bounded on the crate — a beyond-±i64
    ``total_trades`` / ``max_consecutive_losses`` refuses with
    ``OverflowError`` where the deleted Python law compared unbounded ints
    (never a silently different verdict). Unreachable from ``ResultRow`` /
    ``ComboAggregate`` (both int64-validate); duck-typed here.
    """

    def test_gate_int_beyond_i64_refuses_loudly(self) -> None:
        agg = SimpleNamespace(win_loss_ratio=None, max_consecutive_losses=3)
        row_big = SimpleNamespace(
            total_trades=2**63, profit_factor=1.5, max_drawdown_pct=10.0, win_rate=0.625
        )
        assert _oracle_passes_hard_gates(row_big, agg) is True
        with pytest.raises(OverflowError):
            passes_hard_gates(cast(Any, row_big), cast(Any, agg))
        row_ok = SimpleNamespace(
            total_trades=40, profit_factor=1.5, max_drawdown_pct=10.0, win_rate=0.625
        )
        agg_big = SimpleNamespace(win_loss_ratio=None, max_consecutive_losses=2**63)
        assert _oracle_passes_hard_gates(row_ok, agg_big) is False
        with pytest.raises(OverflowError):
            passes_hard_gates(cast(Any, row_ok), cast(Any, agg_big))
        # Exactly at the word boundary still computes — parity holds.
        agg_edge = SimpleNamespace(win_loss_ratio=None, max_consecutive_losses=_I64_MAX)
        assert passes_hard_gates(
            cast(Any, row_ok), cast(Any, agg_edge)
        ) is _oracle_passes_hard_gates(row_ok, agg_edge)


# ───────────────────────────── cascade payload ───────────────────────────────

_states = st.sampled_from(
    [
        CASCADE_STATE_JOINED,
        CASCADE_STATE_NO_REPORT,
        CASCADE_STATE_SWEEP_ID_MISMATCH,
        "some-future-token",
        "",
    ]
)
_rates = st.one_of(st.none(), st.sampled_from([0.0, -0.0, 1 / 3, 0.25, 1.0]))


@st.composite
def _cascade_calls(draw: st.DrawFn) -> tuple[str, CascadeJoin | None, str]:
    """A ``(run_id, join, state)`` triple covering every law-table arm."""
    ids = [run_id_of(n) for n in (1, 2, 3)]
    state = draw(_states)
    if draw(st.booleans()):
        return draw(st.sampled_from(ids)), None, state
    views: dict[str, CascadeVerdictView] = {}
    for cid in ids[:2]:
        kind = draw(st.sampled_from(["pass", "provisional", "fail", "absent"]))
        if kind == "absent":
            continue
        views[cid] = _view(
            cid,
            passed=kind != "fail",
            provisional=kind == "provisional",
            rejected_at=None if kind != "fail" else "spa",
            cascade_composite=None if kind == "fail" else draw(st.sampled_from([1.5, -0.0, 0.0])),
            cpcv_pass_rate=draw(_rates),
            survivor_rank=None if kind == "fail" else 1,
        )
    ambiguous = frozenset([ids[2]] if draw(st.booleans()) else [])
    join = _join(views, ambiguous=ambiguous)
    return draw(st.sampled_from([*ids, "9" * 64])), join, state


class TestCascadePayloadDifferential:
    """``_cascade_payload`` ≡ the verbatim deleted law table (direct-call arms)."""

    @given(call=_cascade_calls())
    def test_matches_the_deleted_law(self, call: tuple[str, CascadeJoin | None, str]) -> None:
        run_id, join, state = call
        got = _outcome(lambda: _cascade_payload(run_id, join=join, state=state), canon=_canon)
        want = _outcome(
            lambda: _oracle_cascade_payload(run_id, join=join, state=state), canon=_canon
        )
        assert got == want

    def test_direct_call_arms(self) -> None:
        """Every table arm once, deterministically (the direct-call surface)."""
        cid = run_id_of(7)
        join = _join({cid: _view(cid)}, ambiguous=frozenset({run_id_of(8)}))
        for run_id, j, state in [
            (cid, None, CASCADE_STATE_NO_REPORT),
            (cid, None, CASCADE_STATE_JOINED),
            (cid, join, CASCADE_STATE_SWEEP_ID_MISMATCH),
            (cid, join, CASCADE_STATE_JOINED),
            (run_id_of(8), join, CASCADE_STATE_JOINED),
            (run_id_of(9), join, CASCADE_STATE_JOINED),
        ]:
            got = _cascade_payload(run_id, join=j, state=state)
            want = _oracle_cascade_payload(run_id, join=j, state=state)
            assert _canon(got) == _canon(want)


# ───────────────────────────── build differential ────────────────────────────

_PF_VALUES = [*_bnd(GATE_MIN_PROFIT_FACTOR), 1.5]
_DD_VALUES = [*_bnd(GATE_MAX_DRAWDOWN_PCT), 10.0]
_WR_VALUES = [*_bnd(GATE_MIN_WIN_RATE), 0.625]
_MASKS = [0, 1, 0b111, 1 << 59, (1 << 60) - 1]
_COMPOSITES = [1.25, float("nan"), -0.0, 0.0, 2.0, None]
_YEAR_JSON = ['{"2024":10000}', '{"2024":10000,"2025":-1}']
_WORST5 = ['[["2024-01-02",-500]]', '{"not":"a list"}', "[]"]


def _reasons_fn(mask_bits: int) -> Mapping[str, str] | None:
    return None if mask_bits % 2 else {"technical": f"t-{mask_bits}", "plain": "p"}


def _margin_fn(candidate: Top100Candidate) -> Mapping[str, object] | None:
    if candidate.row.mask_bits == 0:
        return None
    return {"status": "unverified", "roi_pct": None, "detail": candidate.row.run_id[:8]}


@st.composite
def _build_kwargs(draw: st.DrawFn) -> dict[str, Any]:
    """A full ``build_top100`` kwargs dict — dup ids + junk presets included."""
    ns = draw(st.lists(st.integers(min_value=1, max_value=6), min_size=0, max_size=4))
    candidates: list[Top100Candidate] = []
    for n in ns:
        candidates.append(
            make_candidate(
                n,
                fingerprint=fingerprint_seed(n),
                row_overrides={
                    "profit_factor": draw(st.sampled_from(_PF_VALUES)),
                    "max_drawdown_pct": draw(st.sampled_from(_DD_VALUES)),
                    "win_rate": draw(st.sampled_from(_WR_VALUES)),
                    "total_trades": draw(
                        st.sampled_from([GATE_MIN_TRADES - 1, GATE_MIN_TRADES, 40])
                    ),
                    "winning_trades": 10,
                    "losing_trades": 10,
                    "mask_bits": draw(st.sampled_from(_MASKS)),
                },
                agg_overrides={
                    "composite_score": draw(st.sampled_from(_COMPOSITES)),
                    "win_loss_ratio": draw(st.sampled_from([None, *_bnd(GATE_MIN_WIN_LOSS_RATIO)])),
                    "max_consecutive_losses": draw(
                        st.sampled_from(
                            [3, GATE_MAX_CONSECUTIVE_LOSSES, GATE_MAX_CONSECUTIVE_LOSSES + 1]
                        )
                    ),
                    "pnl_by_year_json": draw(st.sampled_from(_YEAR_JSON)),
                    "worst5_days_json": draw(st.sampled_from(_WORST5)),
                    "vix_win_min": draw(st.sampled_from([12.0, None])),
                },
            )
        )
    run_ids = [c.row.run_id for c in candidates]
    family_by: dict[str, str] = {}
    for rid in dict.fromkeys(run_ids):
        # "anchor" keys the family by the FIRST pool candidate's OWN run_id —
        # the one shape where the collapse min-key's nominal-representative
        # PREFER component can disagree with plain (bit_count, run_id)
        # (a hand-built partition; real group_twins reps are already the
        # (bits, run_id)-min, making prefer redundant there).
        choice = draw(st.sampled_from(["absent", "self", "famA", "famB", "anchor"]))
        if choice == "absent":
            continue
        if choice == "anchor":
            family_by[rid] = run_ids[0]
        else:
            family_by[rid] = rid if choice == "self" else choice
    members_by: dict[str, tuple[str, ...]] = {}
    for token in dict.fromkeys(family_by.values()):
        if draw(st.booleans()):
            members_by[token] = tuple(sorted([*run_ids[:2], "listed-extra"]))
    families = TwinFamilies(family_by_run_id=family_by, members_by_family=members_by)

    cascade_join: CascadeJoin | None = None
    with_join: bool = bool(run_ids) and draw(st.booleans())
    if with_join:
        views: dict[str, CascadeVerdictView] = {}
        for rid in dict.fromkeys(run_ids):
            if draw(st.booleans()):
                views[rid] = _view(rid, cpcv_pass_rate=draw(_rates))
        ambiguous = frozenset({run_ids[0]} if draw(st.booleans()) else set())
        sweep_id = draw(st.sampled_from([_RUN, "e1" * 32]))
        cascade_join = _join(views, sweep_id=sweep_id, ambiguous=ambiguous)

    return {
        "candidates": candidates,
        "run_id": _RUN,
        "preset": draw(st.sampled_from([*TOP100_PRESETS, "bogus", ""])),
        "families": families,
        "counts": _counts(),
        "data_snapshot_hashes": draw(
            st.sampled_from([("snap-1",), ("snap-2", "snap-1", "snap-1"), ()])
        ),
        "baselines": draw(st.sampled_from([{}, {"NIFTY": "baseline-NIFTY.json"}])),
        "reason_provider": _reasons_fn if draw(st.booleans()) else None,
        "margin_roi_provider": _margin_fn if draw(st.booleans()) else None,
        "cascade_join": cascade_join,
    }


class TestBuildDifferential:
    """``build_top100`` ≡ the verbatim deleted pass over hostile pools."""

    @given(kwargs=_build_kwargs())
    def test_matches_the_deleted_law(self, kwargs: dict[str, Any]) -> None:
        with capture_logs():
            got = _outcome(lambda: build_top100(**kwargs), canon=_canon_artifact)
            want = _outcome(lambda: _oracle_build_top100(**kwargs), canon=_canon_artifact)
        assert got == want

    def test_nominal_representative_preferred_over_fewer_bits(self) -> None:
        """The collapse min-key's PREFER component is load-bearing.

        A hand-built partition keys the family by the ANCHOR member's own
        run_id while a sibling passer carries strictly FEWER mask bits: the
        deleted law's ``(0 if run==family_id else 1, bit_count, run_id)``
        key still chooses the ANCHOR (prefer wins before bit count). The
        first-cut mutation campaign proved no prior test reached this arm
        (the dropped-prefer crate mutant survived the whole battery).
        """
        anchor = make_candidate(
            1, fingerprint=fingerprint_seed(1), row_overrides={"mask_bits": 0b111}
        )
        sibling = make_candidate(
            2, fingerprint=fingerprint_seed(2), row_overrides={"mask_bits": 0b1}
        )
        families = TwinFamilies(
            family_by_run_id={
                anchor.row.run_id: anchor.row.run_id,
                sibling.row.run_id: anchor.row.run_id,
            },
            members_by_family={anchor.row.run_id: (anchor.row.run_id, sibling.row.run_id)},
        )
        kwargs: dict[str, Any] = {
            "candidates": [sibling, anchor],  # sibling FIRST — order must not matter
            "run_id": _RUN,
            "preset": "min_dd_first",
            "families": families,
            "counts": _counts(),
            "data_snapshot_hashes": ("snap-1",),
            "baselines": {},
        }
        with capture_logs():
            got = build_top100(**kwargs)
            want = _oracle_build_top100(**kwargs)
        assert _canon_artifact(got) == _canon_artifact(want)
        assert len(got.entries) == 1
        assert got.entries[0]["run_id"] == anchor.row.run_id

    @given(kwargs=_build_kwargs())
    def test_rendered_bytes_parity_or_same_refusal(self, kwargs: dict[str, Any]) -> None:
        """When both build, the RENDERED artifact bytes are equal too."""
        with capture_logs():
            got = _outcome(lambda: build_top100(**kwargs))
            want = _outcome(lambda: _oracle_build_top100(**kwargs))
        if got[0] == "ok" and want[0] == "ok":
            got_art = cast(Top100Artifact, got[1])
            want_art = cast(Top100Artifact, want[1])
            got_bytes = _outcome(got_art.rendered_bytes)

            def _want_render() -> bytes:
                return (_oracle_dumps(want_art.to_payload()) + "\n").encode()

            want_bytes = _outcome(_want_render)
            assert got_bytes == want_bytes
        else:
            assert got[:1] == want[:1]


class TestMultiDefectConstructionOrder:
    """WHICH defect surfaces first on a multi-defect input matches Python.

    The slice-2a MINOR-1 lesson applied up front: the crate hand-keeps the
    deleted CALL-SITE order — dup refusal before the unknown-preset WARN,
    provider argument evaluation before the entry dict-literal walk, metrics
    (``worst5_days``) JSON before regime JSON, ``by_year`` before
    ``by_trend`` before ``entry_hour_hist``.
    """

    @staticmethod
    def _base_kwargs(candidate: Top100Candidate, **overrides: Any) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "candidates": [candidate],
            "run_id": _RUN,
            "preset": "min_dd_first",
            "families": _empty_families(),
            "counts": _counts(),
            "data_snapshot_hashes": ("snap-1",),
            "baselines": {},
        }
        kwargs.update(overrides)
        return kwargs

    def _assert_same_first_defect(
        self, kwargs: dict[str, Any], exc_type: type[BaseException], fragment: str
    ) -> None:
        with capture_logs():
            got = _outcome(lambda: build_top100(**kwargs))
            want = _outcome(lambda: _oracle_build_top100(**kwargs))
        assert got == want
        assert got[0] == "exc"
        assert got[1] is exc_type
        assert fragment in str(got[2])

    def test_raising_reason_provider_beats_regime_json_defect(self) -> None:
        """The provider argument evaluates BEFORE the regime JSON parse."""

        def boom(_mask_bits: int) -> Mapping[str, str] | None:
            raise RuntimeError("reason provider boom")

        candidate = make_candidate(
            1, fingerprint=fingerprint_seed(1), agg_overrides={"pnl_by_year_json": "{not json"}
        )
        kwargs = self._base_kwargs(candidate, reason_provider=boom)
        self._assert_same_first_defect(kwargs, RuntimeError, "reason provider boom")

    def test_raising_margin_provider_beats_regime_json_defect(self) -> None:
        """``margin_roi_provider`` evaluates after reasons, before the walk."""

        def boom(_candidate: Top100Candidate) -> Mapping[str, object] | None:
            raise RuntimeError("margin provider boom")

        candidate = make_candidate(
            1, fingerprint=fingerprint_seed(1), agg_overrides={"pnl_by_trend_json": "[broken"}
        )
        kwargs = self._base_kwargs(candidate, reason_provider=_reasons_fn, margin_roi_provider=boom)
        self._assert_same_first_defect(kwargs, RuntimeError, "margin provider boom")

    def test_worst5_metrics_defect_beats_regime_defect(self) -> None:
        """metrics (``worst5_days``) parses BEFORE the regime block."""
        candidate = make_candidate(
            1,
            fingerprint=fingerprint_seed(1),
            agg_overrides={
                # Distinct malformed docs → distinct JSONDecodeError messages,
                # so WHICH parse raised is visible in the message equality.
                "worst5_days_json": "[worst5 broken",
                "pnl_by_year_json": "{year broken",
            },
        )
        kwargs = self._base_kwargs(candidate)
        with capture_logs():
            got = _outcome(lambda: build_top100(**kwargs))
            want = _outcome(lambda: _oracle_build_top100(**kwargs))
        assert got == want
        assert got[0] == "exc"
        assert got[1] is json.JSONDecodeError

    def test_by_trend_defect_beats_entry_hour_defect(self) -> None:
        """Within regime: ``by_trend`` parses before ``entry_hour_hist``."""
        candidate = make_candidate(
            1,
            fingerprint=fingerprint_seed(1),
            agg_overrides={
                "pnl_by_trend_json": "[1,2]",  # Top100Error (non-object)
                "entry_hour_hist_json": "{hour broken",  # JSONDecodeError
            },
        )
        kwargs = self._base_kwargs(candidate)
        self._assert_same_first_defect(
            kwargs, Top100Error, "agg JSON column did not decode to an object: '[1,2]'"
        )

    def test_dup_refusal_beats_unknown_preset_warn(self) -> None:
        """A dup pool raises BEFORE the unknown-preset WARN can fire."""
        candidate = make_candidate(1, fingerprint=fingerprint_seed(1))
        kwargs = self._base_kwargs(candidate, preset="bogus")
        kwargs["candidates"] = [candidate, candidate]
        with capture_logs() as got_logs:
            got = _outcome(lambda: build_top100(**kwargs))
        with capture_logs() as want_logs:
            want = _outcome(lambda: _oracle_build_top100(**kwargs))
        assert got == want
        assert got == (
            "exc",
            Top100Error,
            f"build_top100: duplicate candidate run_id {candidate.row.run_id!r}",
        )
        assert [e for e in got_logs if e["event"] == "top100.unknown_preset"] == []
        assert [e for e in want_logs if e["event"] == "top100.unknown_preset"] == []

    def test_raising_rank_fn_propagates_raw(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A raising rank callable escapes the crate pass unwrapped."""
        import brutex.engine.top100_artifact as mod

        def boom(
            candidates: Sequence[Top100Candidate], *, preset: str
        ) -> tuple[Top100Candidate, ...]:
            raise RuntimeError(f"rank boom under {preset}")

        candidate = make_candidate(1, fingerprint=fingerprint_seed(1))
        kwargs = self._base_kwargs(candidate)
        monkeypatch.setattr(mod, "rank_candidates", boom)
        with capture_logs():
            got = _outcome(lambda: build_top100(**kwargs))
            want = _outcome(lambda: _oracle_build_top100(**kwargs, rank_fn=boom))
        assert got == want
        assert got == ("exc", RuntimeError, "rank boom under min_dd_first")


# ───────────────────────────── load-ladder differential ──────────────────────


def _base_artifact() -> Top100Artifact:
    with capture_logs():
        return build_top100(
            [make_candidate(1, fingerprint=fingerprint_seed(1))],
            run_id=_RUN,
            preset="min_dd_first",
            families=_empty_families(),
            counts=_counts(),
            data_snapshot_hashes=("snap-1",),
            baselines={"NIFTY": "baseline-NIFTY.json"},
        )


#: Built once — every example deep-copies before mutating.
_BASE_PAYLOAD: dict[str, Any] = cast("dict[str, Any]", _base_artifact().to_payload())

#: Sentinel — the mutation DELETES the key instead of setting a value.
_DELETE = object()

_STAMP_JUNK: tuple[tuple[str, object], ...] = (
    ("stage", "other"),
    ("stage", 1),
    ("stage", None),
    ("stage", True),
    ("run_id", "deadbeef"),
    ("run_id", 1),
    ("run_id", None),
    ("content_hash", "short"),
    ("content_hash", "x" * 64),  # right length, wrong digest → hash mismatch
    ("content_hash", 5),
    ("content_hash", None),
    ("produced_at_utc", 1),
    ("produced_at_utc", None),
    ("produced_at_utc", True),
    ("produced_at_utc", 1.5),
    ("stage", _DELETE),
    ("run_id", _DELETE),
    ("content_hash", _DELETE),
    ("produced_at_utc", _DELETE),
    ("extra", 1),  # extra key → stamp_bad_shape
)
_ROOT_JUNK: tuple[tuple[str, object], ...] = (
    ("schema_version", 2),
    ("schema_version", "1"),
    ("schema_version", None),
    ("schema_version", True),  # bool pun — LOADS (True == 1)
    ("schema_version", 1.0),  # float pun — LOADS (1.0 == 1)
    ("schema_version", _DELETE),
    ("run_id", "e1" * 32),
    ("run_id", 5),
    ("run_id", _DELETE),
    ("preset", ""),
    ("preset", 5),
    ("preset", None),
    ("preset", True),
    ("preset", _DELETE),
    ("source", []),
    ("source", "x"),
    ("source", 5),
    ("source", None),
    ("source", _DELETE),
    ("baselines", []),
    ("baselines", "x"),
    ("baselines", {"k": 5}),
    ("baselines", {"k": None}),
    ("baselines", {"k": ["x"]}),
    ("baselines", {"k": True}),
    ("baselines", _DELETE),
    ("entries", "x"),
    ("entries", 5),
    ("entries", {}),
    ("entries", None),
    ("entries", _DELETE),
    ("cascade_join", []),
    ("cascade_join", "x"),
    ("cascade_join", 5),
    ("cascade_join", True),
    ("cascade_join", None),
    ("cascade_join", {"state": "joined"}),
    ("cascade_join", _DELETE),
)
_ENTRY_KEYS: tuple[str, ...] = tuple(sorted(ENTRY_REQUIRED_KEYS))
_RANK_JUNK: tuple[object, ...] = (0, 2, True, 1.0, "1", None, -1)

_stamp_mutations = st.lists(st.sampled_from(_STAMP_JUNK), min_size=0, max_size=2)
_root_mutations = st.lists(st.sampled_from(_ROOT_JUNK), min_size=0, max_size=2)
_entry_mutations = st.lists(
    st.one_of(
        st.tuples(st.just("drop"), st.sampled_from(_ENTRY_KEYS)),
        st.tuples(st.just("rank"), st.sampled_from(_RANK_JUNK)),
        st.tuples(st.just("extra"), st.just("additive-ok")),
        st.tuples(st.just("nondict"), st.just(7)),
    ),
    min_size=0,
    max_size=2,
)


def _write_mutated_pair(
    runs_dir: Path,
    stamp_mutations: list[tuple[str, object]],
    root_mutations: list[tuple[str, object]],
    entry_mutations: list[tuple[str, object]],
) -> None:
    """Write a (possibly corrupted) artifact + stamp pair via the ORACLE render."""
    payload: dict[str, Any] = copy.deepcopy(_BASE_PAYLOAD)
    for op, value in entry_mutations:
        entries = payload.get("entries")
        if not isinstance(entries, list) or not entries:
            continue
        if op == "nondict":
            entries[0] = value
            continue
        if not isinstance(entries[0], dict):
            continue  # a prior "nondict" op replaced the entry — nothing to edit
        if op == "drop":
            cast("dict[str, Any]", entries[0]).pop(cast(str, value), None)
        elif op == "rank":
            cast("dict[str, Any]", entries[0])["rank"] = value
        else:  # extra
            cast("dict[str, Any]", entries[0])["additive_key"] = value
    for key, value in root_mutations:
        if value is _DELETE:
            payload.pop(key, None)
        else:
            payload[key] = copy.deepcopy(value)
    raw = (_oracle_dumps(payload) + "\n").encode("utf-8")
    stamp: dict[str, Any] = {
        "stage": TOP100_STAGE,
        "run_id": _RUN,
        "content_hash": hashlib.sha256(raw).hexdigest(),
        "produced_at_utc": _FIXED_PRODUCED_AT,
    }
    for key, value in stamp_mutations:
        if value is _DELETE:
            stamp.pop(key, None)
        else:
            stamp[key] = copy.deepcopy(value)
    target = runs_dir / _RUN
    target.mkdir(parents=True, exist_ok=True)
    (target / TOP100_FILENAME).write_bytes(raw)
    (target / TOP100_STAMP_FILENAME).write_text(_oracle_dumps(stamp) + "\n", encoding="utf-8")


def _shim_load(runs_dir: Path) -> tuple[Top100Artifact | None, list[str]]:
    """The Rust-backed ``load_top100`` + its captured ``load_invalid`` reasons."""
    with capture_logs() as logs:
        loaded = load_top100(runs_dir, _RUN)
    reasons = [
        cast(str, event["reason"]) for event in logs if event["event"] == "top100.load_invalid"
    ]
    return loaded, reasons


class TestLoadLadderDifferential:
    """``load_top100`` ≡ the verbatim deleted ladder over mutated pairs.

    The pair is written through the ORACLE render (writer independence), the
    stamp's ``content_hash`` recomputed over the MUTATED bytes so the ladder
    reaches the shape checks; stamp mutations then divert it earlier. The
    loaded-artifact projection AND the ``top100.load_invalid`` reason
    sequence must both match (the bool/float rank + schema_version PUNS —
    ``True == 1`` / ``1.0 == 1`` — legally LOAD on both sides).
    """

    @given(
        stamp_mutations=_stamp_mutations,
        root_mutations=_root_mutations,
        entry_mutations=_entry_mutations,
    )
    def test_matches_the_deleted_law(
        self,
        stamp_mutations: list[tuple[str, object]],
        root_mutations: list[tuple[str, object]],
        entry_mutations: list[tuple[str, object]],
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runs_dir = Path(tmp)
            _write_mutated_pair(runs_dir, stamp_mutations, root_mutations, entry_mutations)
            got, got_reasons = _shim_load(runs_dir)
            want, want_reasons = _oracle_load_top100(runs_dir, _RUN)
        assert got_reasons == want_reasons
        if want is None:
            assert got is None
        else:
            assert got is not None
            assert _canon_artifact(got) == _canon_artifact(want)

    def test_unmutated_pair_round_trips_equal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runs_dir = Path(tmp)
            _write_mutated_pair(runs_dir, [], [], [])
            got, got_reasons = _shim_load(runs_dir)
            want, want_reasons = _oracle_load_top100(runs_dir, _RUN)
        assert got_reasons == want_reasons == []
        assert got is not None and want is not None
        assert _canon_artifact(got) == _canon_artifact(want)
        assert _canon_artifact(got) == _canon(_BASE_PAYLOAD)

    @pytest.mark.parametrize(
        ("artifact_bytes", "stamp_bytes"),
        [
            (b"{not json\n", None),  # artifact_not_json (stamp valid over these bytes)
            (None, b"{not json\n"),  # stamp_not_json
            (b"[1,2]\n", None),  # artifact_not_object
            (None, b"[1,2]\n"),  # stamp_bad_shape (non-dict stamp)
            (b"", None),  # empty artifact → not json
            (None, b""),  # empty stamp → not json
        ],
    )
    def test_raw_byte_damage_parity(
        self, artifact_bytes: bytes | None, stamp_bytes: bytes | None
    ) -> None:
        """Non-JSON / non-object raw bytes divert both ladders identically."""
        with tempfile.TemporaryDirectory() as tmp:
            runs_dir = Path(tmp)
            _write_mutated_pair(runs_dir, [], [], [])
            target = runs_dir / _RUN
            if artifact_bytes is not None:
                (target / TOP100_FILENAME).write_bytes(artifact_bytes)
                stamp = {
                    "stage": TOP100_STAGE,
                    "run_id": _RUN,
                    "content_hash": hashlib.sha256(artifact_bytes).hexdigest(),
                    "produced_at_utc": _FIXED_PRODUCED_AT,
                }
                (target / TOP100_STAMP_FILENAME).write_text(
                    _oracle_dumps(stamp) + "\n", encoding="utf-8"
                )
            if stamp_bytes is not None:
                (target / TOP100_STAMP_FILENAME).write_bytes(stamp_bytes)
            got, got_reasons = _shim_load(runs_dir)
            want, want_reasons = _oracle_load_top100(runs_dir, _RUN)
        assert got is None and want is None
        assert got_reasons == want_reasons
        assert len(got_reasons) == 1
