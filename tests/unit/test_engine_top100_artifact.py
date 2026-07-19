"""Unit suite for :mod:`brutex.engine.top100_artifact` (DEC-SWEEP-RESULTS-DEPTH-001, WP5).

Covers: the 5-gate recompute law (cross-pinned against ``brutex.ranking``'s
own constants so the two homes can never drift), preset resolution + the
strict total orders, family collapse, the honesty header, the provider seams,
the deterministic writer, and the FULL corrupt-artifact validation matrix
(every rejection → ``None`` → rebuild).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest
from brutex import ranking
from brutex.engine.top100_artifact import (
    DEFAULT_TOP100_PRESET,
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
    TOP100_STAMP_FILENAME,
    PoolCounts,
    Top100Artifact,
    Top100Candidate,
    Top100Error,
    build_top100,
    load_top100,
    passes_hard_gates,
    rank_candidates,
    resolve_preset,
    write_top100,
)
from brutex.engine.twin_detect import TwinCandidate, TwinFamilies, group_twins
from structlog.testing import capture_logs

from tests.fakes.wp4_twin_fixtures import fingerprint_seed
from tests.fakes.wp5_depth_fixtures import (
    make_agg,
    make_candidate,
    make_result_row,
    run_id_of,
    with_agg,
)

_RUN = "grid" + "0" * 60  # a stable 64-char-ish grid id (format not validated)
_NO_FAMILIES = TwinFamilies(family_by_run_id={}, members_by_family={})


def _counts(**overrides: int) -> PoolCounts:
    base = {"stores": 1, "excluded_no_aggs": 0, "zero_trade_excluded": 0, "parity_excluded": 0}
    base.update(overrides)
    return PoolCounts(**base)


def _build(candidates: list[Top100Candidate], **kwargs: Any) -> Top100Artifact:
    base: dict[str, Any] = {
        "run_id": _RUN,
        "preset": DEFAULT_TOP100_PRESET,
        "families": _NO_FAMILIES,
        "counts": _counts(),
        "data_snapshot_hashes": ("snap0001",),
        "baselines": {"NIFTY": "baseline-NIFTY.json"},
    }
    base.update(kwargs)
    return build_top100(candidates, **base)


class TestGateConstantsCrossPin:
    """The re-pinned gate constants must equal ``brutex.ranking``'s own."""

    def test_min_trades_is_the_public_upstream_constant(self) -> None:
        assert GATE_MIN_TRADES == ranking.MIN_TRADES

    def test_private_twins_match(self) -> None:
        assert GATE_MIN_PROFIT_FACTOR == ranking._MIN_PROFIT_FACTOR
        assert GATE_MAX_DRAWDOWN_PCT == ranking._MAX_DRAWDOWN_PCT
        assert GATE_MIN_WIN_RATE == ranking._MIN_WIN_RATE
        assert GATE_MIN_WIN_LOSS_RATIO == ranking._MIN_WIN_LOSS_RATIO
        assert GATE_MAX_CONSECUTIVE_LOSSES == ranking._MAX_CONSECUTIVE_LOSSES


class TestPassesHardGates:
    def test_defaults_pass(self) -> None:
        cand = make_candidate(1, fingerprint=fingerprint_seed(1))
        assert passes_hard_gates(cand.row, cand.agg) is True

    def test_too_few_trades_rejected(self) -> None:
        cand = make_candidate(
            1,
            fingerprint=fingerprint_seed(1),
            row_overrides={"total_trades": 29, "winning_trades": 20, "losing_trades": 9},
        )
        assert passes_hard_gates(cand.row, cand.agg) is False

    def test_low_profit_factor_rejected(self) -> None:
        cand = make_candidate(
            1, fingerprint=fingerprint_seed(1), row_overrides={"profit_factor": 1.19}
        )
        assert passes_hard_gates(cand.row, cand.agg) is False

    def test_deep_drawdown_rejected(self) -> None:
        cand = make_candidate(
            1, fingerprint=fingerprint_seed(1), row_overrides={"max_drawdown_pct": 30.01}
        )
        assert passes_hard_gates(cand.row, cand.agg) is False

    def test_low_win_rate_with_low_wlr_rejected(self) -> None:
        cand = make_candidate(
            1,
            fingerprint=fingerprint_seed(1),
            row_overrides={"win_rate": 0.20},
            agg_overrides={"win_loss_ratio": 1.9},
        )
        assert passes_hard_gates(cand.row, cand.agg) is False

    def test_low_win_rate_with_none_wlr_rejected(self) -> None:
        cand = make_candidate(
            1,
            fingerprint=fingerprint_seed(1),
            row_overrides={"win_rate": 0.20},
            agg_overrides={"win_loss_ratio": None},
        )
        assert passes_hard_gates(cand.row, cand.agg) is False

    def test_low_win_rate_with_big_wlr_passes(self) -> None:
        cand = make_candidate(
            1,
            fingerprint=fingerprint_seed(1),
            row_overrides={"win_rate": 0.20},
            agg_overrides={"win_loss_ratio": 2.0},
        )
        assert passes_hard_gates(cand.row, cand.agg) is True

    def test_long_losing_streak_rejected(self) -> None:
        cand = make_candidate(
            1, fingerprint=fingerprint_seed(1), agg_overrides={"max_consecutive_losses": 11}
        )
        assert passes_hard_gates(cand.row, cand.agg) is False


class TestResolvePreset:
    @pytest.mark.parametrize("name", TOP100_PRESETS)
    def test_known_presets_resolve_to_themselves(self, name: str) -> None:
        assert resolve_preset(name) == name

    def test_unknown_degrades_to_default_and_logs(self) -> None:
        with capture_logs() as logs:
            assert resolve_preset("bogus") == DEFAULT_TOP100_PRESET
        assert any(log["event"] == "top100.unknown_preset" for log in logs)


class TestRankCandidates:
    def test_min_dd_first_orders_by_drawdown_ascending(self) -> None:
        low = make_candidate(
            2, fingerprint=fingerprint_seed(2), row_overrides={"max_drawdown_pct": 5.0}
        )
        high = make_candidate(
            1, fingerprint=fingerprint_seed(1), row_overrides={"max_drawdown_pct": 20.0}
        )
        ranked = rank_candidates([high, low], preset="min_dd_first")
        assert [c.row.run_id for c in ranked] == [low.row.run_id, high.row.run_id]

    def test_min_dd_composite_breaks_drawdown_ties_descending(self) -> None:
        weak = make_candidate(
            1, fingerprint=fingerprint_seed(1), agg_overrides={"composite_score": 0.5}
        )
        strong = make_candidate(
            2, fingerprint=fingerprint_seed(2), agg_overrides={"composite_score": 2.5}
        )
        ranked = rank_candidates([weak, strong], preset="min_dd_first")
        assert [c.row.run_id for c in ranked] == [strong.row.run_id, weak.row.run_id]

    def test_min_dd_none_composite_ranks_after_scored(self) -> None:
        unscored = make_candidate(
            1, fingerprint=fingerprint_seed(1), agg_overrides={"composite_score": None}
        )
        scored = make_candidate(
            2, fingerprint=fingerprint_seed(2), agg_overrides={"composite_score": -3.0}
        )
        ranked = rank_candidates([unscored, scored], preset="min_dd_first")
        assert [c.row.run_id for c in ranked] == [scored.row.run_id, unscored.row.run_id]

    def test_min_dd_fewest_consecutive_losses_breaks_composite_ties(self) -> None:
        calm = make_candidate(
            2, fingerprint=fingerprint_seed(2), agg_overrides={"max_consecutive_losses": 1}
        )
        streaky = make_candidate(
            1, fingerprint=fingerprint_seed(1), agg_overrides={"max_consecutive_losses": 9}
        )
        ranked = rank_candidates([streaky, calm], preset="min_dd_first")
        assert [c.row.run_id for c in ranked] == [calm.row.run_id, streaky.row.run_id]

    def test_min_dd_full_tie_falls_to_run_id_ascending(self) -> None:
        a = make_candidate(1, fingerprint=fingerprint_seed(1))
        b = make_candidate(2, fingerprint=fingerprint_seed(2))
        ranked = rank_candidates([b, a], preset="min_dd_first")
        assert [c.row.run_id for c in ranked] == [a.row.run_id, b.row.run_id]

    @pytest.mark.parametrize(
        ("preset", "field"),
        [("profit_factor", "profit_factor"), ("win_rate", "win_rate"), ("calmar", "calmar_ratio")],
    )
    def test_alternate_presets_rank_descending_on_the_raw_row_value(
        self, preset: str, field: str
    ) -> None:
        low = make_candidate(1, fingerprint=fingerprint_seed(1), row_overrides={field: 1.3})
        high = make_candidate(2, fingerprint=fingerprint_seed(2), row_overrides={field: 1.9})
        ranked = rank_candidates([low, high], preset=preset)
        assert [c.row.run_id for c in ranked] == [high.row.run_id, low.row.run_id]

    def test_composite_preset_ranks_descending_none_last(self) -> None:
        none_score = make_candidate(
            1, fingerprint=fingerprint_seed(1), agg_overrides={"composite_score": None}
        )
        low = make_candidate(
            2, fingerprint=fingerprint_seed(2), agg_overrides={"composite_score": -1.0}
        )
        high = make_candidate(
            3, fingerprint=fingerprint_seed(3), agg_overrides={"composite_score": 2.0}
        )
        ranked = rank_candidates([none_score, low, high], preset="composite")
        assert [c.row.run_id for c in ranked] == [
            high.row.run_id,
            low.row.run_id,
            none_score.row.run_id,
        ]

    def test_order_is_input_permutation_invariant(self) -> None:
        candidates = [
            make_candidate(
                n,
                fingerprint=fingerprint_seed(n),
                row_overrides={"max_drawdown_pct": float((n * 7) % 13)},
            )
            for n in range(1, 9)
        ]
        forward = rank_candidates(candidates, preset="min_dd_first")
        backward = rank_candidates(list(reversed(candidates)), preset="min_dd_first")
        assert [c.row.run_id for c in forward] == [c.row.run_id for c in backward]


class TestCandidateAndCountsValidation:
    def test_candidate_run_id_mismatch_raises(self) -> None:
        row = make_result_row(run_id_of(1))
        agg = make_agg(run_id_of(2), fingerprint=fingerprint_seed(2))
        with pytest.raises(Top100Error, match="mislabelled join"):
            Top100Candidate(row=row, agg=agg, sl_paisa=None, tp_paisa=None, tuple_ref="a/b/c")

    def test_candidate_empty_tuple_ref_raises(self) -> None:
        row = make_result_row(run_id_of(1))
        agg = make_agg(run_id_of(1), fingerprint=fingerprint_seed(1))
        with pytest.raises(Top100Error, match="tuple_ref"):
            Top100Candidate(row=row, agg=agg, sl_paisa=None, tp_paisa=None, tuple_ref="")

    def test_negative_pool_count_raises(self) -> None:
        with pytest.raises(Top100Error, match="stores"):
            PoolCounts(stores=-1, excluded_no_aggs=0, zero_trade_excluded=0, parity_excluded=0)


class TestBuildTop100:
    def test_duplicate_run_id_raises(self) -> None:
        a = make_candidate(1, fingerprint=fingerprint_seed(1))
        with pytest.raises(Top100Error, match="duplicate candidate run_id"):
            _build([a, a])

    def test_empty_pool_builds_an_honest_empty_artifact(self) -> None:
        artifact = _build([])
        assert artifact.entries == ()
        assert artifact.source["pool"] == 0
        assert artifact.source["gate_passers"] == 0

    def test_gate_failers_are_excluded_and_counted(self) -> None:
        passer = make_candidate(1, fingerprint=fingerprint_seed(1))
        failer = make_candidate(
            2, fingerprint=fingerprint_seed(2), row_overrides={"profit_factor": 1.0}
        )
        artifact = _build([passer, failer])
        assert [e["run_id"] for e in artifact.entries] == [passer.row.run_id]
        assert artifact.source == {
            "data_snapshot_hashes": ["snap0001"],
            "stores": 1,
            "pool": 2,
            "gate_passers": 1,
            "excluded_no_aggs": 0,
            "zero_trade_excluded": 0,
            "parity_excluded": 0,
            "families_collapsed": 0,
            "reasons_wired": False,
            "margin_roi_wired": False,
        }

    def test_family_collapse_ships_the_simplest_member(self) -> None:
        simple = make_candidate(
            2, fingerprint=fingerprint_seed(9), row_overrides={"mask_bits": 0b1}
        )
        complex_twin = make_candidate(
            1, fingerprint=fingerprint_seed(9), row_overrides={"mask_bits": 0b111}
        )
        families = group_twins(
            [
                TwinCandidate(
                    run_id=c.row.run_id,
                    mask_bits=c.row.mask_bits,
                    fingerprint=c.agg.fingerprint,
                    entry_ts_us=frozenset({1, 2, 3}),
                )
                for c in (simple, complex_twin)
            ]
        )
        artifact = _build([simple, complex_twin], families=families)
        assert len(artifact.entries) == 1
        entry = artifact.entries[0]
        assert entry["run_id"] == simple.row.run_id
        assert entry["family_id"] == simple.row.run_id
        assert entry["family_members"] == sorted([simple.row.run_id, complex_twin.row.run_id])
        assert artifact.source["families_collapsed"] == 1

    def test_gated_out_representative_falls_back_to_simplest_passing_member(self) -> None:
        # The family's nominal representative (fewest bits) FAILS the gates;
        # the family still ships its simplest PASSING member under the same
        # family_id — never an empty family, never a gated-out entry.
        rep_gated_out = make_candidate(
            1,
            fingerprint=fingerprint_seed(9),
            row_overrides={
                "mask_bits": 0b1,
                "total_trades": 5,
                "winning_trades": 3,
                "losing_trades": 2,
            },
        )
        passing_twin = make_candidate(
            2, fingerprint=fingerprint_seed(9), row_overrides={"mask_bits": 0b111}
        )
        families = group_twins(
            [
                TwinCandidate(
                    run_id=c.row.run_id,
                    mask_bits=c.row.mask_bits,
                    fingerprint=c.agg.fingerprint,
                    entry_ts_us=frozenset({1, 2}),
                )
                for c in (rep_gated_out, passing_twin)
            ]
        )
        artifact = _build([rep_gated_out, passing_twin], families=families)
        assert len(artifact.entries) == 1
        entry = artifact.entries[0]
        assert entry["run_id"] == passing_twin.row.run_id
        assert entry["family_id"] == rep_gated_out.row.run_id

    def test_entries_cap_at_the_top100_limit_never_padding(self) -> None:
        many = [
            make_candidate(
                n,
                fingerprint=fingerprint_seed(n),
                row_overrides={"max_drawdown_pct": float(n % 29) + 0.5},
            )
            for n in range(1, TOP100_LIMIT + 6)
        ]
        artifact = _build(many)
        assert len(artifact.entries) == TOP100_LIMIT
        assert [e["rank"] for e in artifact.entries] == list(range(1, TOP100_LIMIT + 1))
        few = _build(many[:3])
        assert len(few.entries) == 3

    def test_entry_carries_the_binding_contract_shape(self) -> None:
        cand = make_candidate(1, fingerprint=fingerprint_seed(1))
        artifact = _build([cand])
        entry = artifact.entries[0]
        assert ENTRY_REQUIRED_KEYS.issubset(entry.keys())
        assert entry["mask_hex"] == f"{cand.row.mask_bits:032x}"
        assert isinstance(entry["conditions"], list)
        assert len(entry["conditions"]) == cand.row.mask_bits.bit_count()
        assert entry["arm"] == {"sl_paisa": 80, "tp_paisa": 50}
        assert entry["instrument"] == "NIFTY"
        assert entry["tf"] == "1minute"
        assert entry["direction"] == "bull"
        metrics = entry["metrics"]
        assert isinstance(metrics, dict)
        assert metrics["net_pnl_paisa"] == cand.row.net_pnl_paisa
        assert metrics["sharpe_ratio"] == cand.row.sharpe_ratio
        assert metrics["composite_score"] == cand.agg.composite_score
        assert metrics["worst5_days"] == [["2024-01-02", -500]]
        assert metrics["exits"] == {
            "stop_loss": 5,
            "take_profit": 10,
            "signal": 20,
            "forced_1520": 5,
            "end_of_data": 0,
            "session_gap": 0,
        }
        regime = entry["regime"]
        assert isinstance(regime, dict)
        assert regime["by_year"] == {"2024": 10000}
        assert regime["event_days"] == {"counts": {}, "pnl": {}}
        vix = entry["vix_band"]
        assert isinstance(vix, dict)
        assert vix["lo"] == 12.5
        assert vix["win"] == {"min": 12.0, "avg": 14.5, "max": 18.0}
        assert vix["loss"] == {"min": 11.0, "avg": 13.0, "max": 16.0}
        assert entry["reasons"] is None
        assert entry["margin_roi"] is None
        assert entry["equity_ref"] == {
            "tuple": "NIFTY/1minute/bull_k1_deadbeefdeadbeef",
            "sidecar": "equity",
        }

    def test_exit_tallies_map_positionally_never_swapped_or_zeroed(self) -> None:
        """Each exits key reads its OWN agg tally (refuter round 4, CM3 pattern).

        The fixture DEFAULTS zero both tail tallies, so the shape pin above let
        an ``end_of_data`` <-> ``session_gap`` SWAP and a literal-``0``
        ``session_gap`` both survive. Six pairwise-DISTINCT NONZERO tallies
        (rebalanced so the exits still sum to ``n_trades`` = 40 — the
        ``ComboAggregate.__post_init__`` invariant) kill the swap mutant in
        BOTH directions plus the literal-0. Deliberately per-test
        ``agg_overrides``, never a fixture-default change — the defaults feed
        the sha256-locked ``test_top100_artifact_golden.py`` bytes.
        """
        cand = make_candidate(
            1,
            fingerprint=fingerprint_seed(1),
            agg_overrides={
                "exits_stop_loss": 7,
                "exits_take_profit": 11,
                "exits_signal": 13,
                "exits_forced_1520": 3,
                "exits_end_of_data": 2,
                "exits_session_gap": 4,
            },
        )
        artifact = _build([cand])
        metrics = artifact.entries[0]["metrics"]
        assert isinstance(metrics, dict)
        assert metrics["exits"] == {
            "stop_loss": 7,
            "take_profit": 11,
            "signal": 13,
            "forced_1520": 3,
            "end_of_data": 2,
            "session_gap": 4,
        }

    def test_unstamped_vix_band_is_honest_none(self) -> None:
        cand = make_candidate(
            1,
            fingerprint=fingerprint_seed(1),
            agg_overrides={
                "vix_win_min": None,
                "vix_win_avg": None,
                "vix_win_max": None,
                "vix_loss_min": None,
                "vix_loss_avg": None,
                "vix_loss_max": None,
                "vix_band_lo": None,
                "vix_band_hi": None,
                "vix_stamped_trades": 0,
                "vix_missing_trades": 40,
            },
        )
        artifact = _build([cand])
        vix = artifact.entries[0]["vix_band"]
        assert isinstance(vix, dict)
        assert vix == {
            "lo": None,
            "hi": None,
            "stamped": 0,
            "missing": 40,
            "win": None,
            "loss": None,
        }

    def test_providers_are_threaded_and_flagged(self) -> None:
        cand = make_candidate(1, fingerprint=fingerprint_seed(1))
        artifact = _build(
            [cand],
            reason_provider=lambda mask: {"technical": f"mask={mask:x}", "plain": "plain words"},
            margin_roi_provider=lambda c: {
                "status": "unverified",
                "roi_pct": None,
                "detail": c.row.instrument,
            },
        )
        entry = artifact.entries[0]
        assert entry["reasons"] == {"technical": "mask=7", "plain": "plain words"}
        assert entry["margin_roi"] == {
            "status": "unverified",
            "roi_pct": None,
            "detail": "NIFTY",
        }
        assert artifact.source["reasons_wired"] is True
        assert artifact.source["margin_roi_wired"] is True

    def test_provider_returning_none_stays_none(self) -> None:
        cand = make_candidate(1, fingerprint=fingerprint_seed(1))
        artifact = _build([cand], reason_provider=lambda _mask: None)
        assert artifact.entries[0]["reasons"] is None
        assert artifact.source["reasons_wired"] is True

    def test_zero_trade_agg_json_defaults_render(self) -> None:
        # A candidate with a parity-failed-style NULL agg never reaches the
        # pool in production; the builder still renders any VALID agg. This
        # pins the None-stat rendering (nullable metrics stay None).
        cand = make_candidate(
            1,
            fingerprint=fingerprint_seed(1),
            agg_overrides={"composite_score": None, "win_loss_ratio": None},
        )
        artifact = _build([cand])
        metrics = artifact.entries[0]["metrics"]
        assert isinstance(metrics, dict)
        assert metrics["composite_score"] is None
        assert metrics["win_loss_ratio"] is None


class TestWriteAndLoad:
    def _artifact(self) -> Top100Artifact:
        return _build([make_candidate(1, fingerprint=fingerprint_seed(1))])

    def test_round_trip(self, tmp_path: Path) -> None:
        artifact = self._artifact()
        path = write_top100(tmp_path, artifact)
        assert path == tmp_path / _RUN / TOP100_FILENAME
        loaded = load_top100(tmp_path, _RUN)
        assert loaded is not None
        assert loaded.to_payload() == artifact.to_payload()

    def test_artifact_bytes_are_deterministic_across_writes(self, tmp_path: Path) -> None:
        artifact = self._artifact()
        path = write_top100(tmp_path, artifact)
        first = path.read_bytes()
        write_top100(tmp_path, self._artifact())
        assert path.read_bytes() == first

    def test_missing_stamp_returns_none(self, tmp_path: Path) -> None:
        write_top100(tmp_path, self._artifact())
        (tmp_path / _RUN / TOP100_STAMP_FILENAME).unlink()
        with capture_logs() as logs:
            assert load_top100(tmp_path, _RUN) is None
        assert any(log.get("reason") == "stamp_unreadable" for log in logs)

    def _corrupt_stamp(self, tmp_path: Path, payload: object) -> None:
        rendered = payload if isinstance(payload, str) else json.dumps(payload)
        (tmp_path / _RUN / TOP100_STAMP_FILENAME).write_text(rendered, encoding="utf-8")

    def _stamp_payload(self, tmp_path: Path) -> dict[str, Any]:
        raw = (tmp_path / _RUN / TOP100_STAMP_FILENAME).read_text(encoding="utf-8")
        payload = json.loads(raw)
        assert isinstance(payload, dict)
        return payload

    @pytest.mark.parametrize(
        ("mutation", "reason"),
        [
            ("torn{", "stamp_not_json"),
            (["a"], "stamp_bad_shape"),
            ({"stage": "top100"}, "stamp_bad_shape"),
        ],
    )
    def test_damaged_stamp_shapes_return_none(
        self, tmp_path: Path, mutation: object, reason: str
    ) -> None:
        write_top100(tmp_path, self._artifact())
        self._corrupt_stamp(tmp_path, mutation)
        with capture_logs() as logs:
            assert load_top100(tmp_path, _RUN) is None
        assert any(log.get("reason") == reason for log in logs)

    @pytest.mark.parametrize(
        ("key", "value", "reason"),
        [
            ("stage", "cascade", "stamp_wrong_stage"),
            ("run_id", "other", "stamp_wrong_run_id"),
            ("content_hash", "ab" * 3, "stamp_bad_content_hash"),
            ("content_hash", 7, "stamp_bad_content_hash"),
            ("produced_at_utc", 12, "stamp_bad_produced_at"),
        ],
    )
    def test_bad_stamp_fields_return_none(
        self, tmp_path: Path, key: str, value: object, reason: str
    ) -> None:
        write_top100(tmp_path, self._artifact())
        payload = self._stamp_payload(tmp_path)
        payload[key] = value
        self._corrupt_stamp(tmp_path, payload)
        with capture_logs() as logs:
            assert load_top100(tmp_path, _RUN) is None
        assert any(log.get("reason") == reason for log in logs)

    def test_missing_artifact_returns_none(self, tmp_path: Path) -> None:
        write_top100(tmp_path, self._artifact())
        (tmp_path / _RUN / TOP100_FILENAME).unlink()
        with capture_logs() as logs:
            assert load_top100(tmp_path, _RUN) is None
        assert any(log.get("reason") == "artifact_unreadable" for log in logs)

    def test_tampered_artifact_fails_the_stamp_hash(self, tmp_path: Path) -> None:
        path = write_top100(tmp_path, self._artifact())
        tampered = path.read_bytes().replace(b'"rank":1', b'"rank":2')
        assert tampered != path.read_bytes()
        path.write_bytes(tampered)
        with capture_logs() as logs:
            assert load_top100(tmp_path, _RUN) is None
        assert any(log.get("reason") == "artifact_hash_mismatch" for log in logs)

    def _write_raw(self, tmp_path: Path, payload_bytes: bytes) -> None:
        """Write raw artifact bytes with a MATCHING stamp (payload-shape probes)."""
        target = tmp_path / _RUN
        target.mkdir(parents=True, exist_ok=True)
        (target / TOP100_FILENAME).write_bytes(payload_bytes)
        stamp = {
            "stage": "top100",
            "run_id": _RUN,
            "content_hash": hashlib.sha256(payload_bytes).hexdigest(),
            "produced_at_utc": "2026-07-07T00:00:00+00:00",
        }
        (target / TOP100_STAMP_FILENAME).write_text(json.dumps(stamp), encoding="utf-8")

    def _valid_payload(self) -> dict[str, Any]:
        payload = self._artifact().to_payload()
        assert isinstance(payload, dict)
        return payload

    @pytest.mark.parametrize(
        ("mutate", "reason"),
        [
            (lambda p: b"torn{", "artifact_not_json"),
            (lambda p: b'["not an object"]', "artifact_not_object"),
            (lambda p: _mutated(p, "schema_version", 99), "schema_version_mismatch"),
            (lambda p: _mutated(p, "run_id", "other"), "artifact_wrong_run_id"),
            (lambda p: _mutated(p, "preset", ""), "bad_preset"),
            (lambda p: _mutated(p, "source", "not a dict"), "bad_source"),
            (lambda p: _mutated(p, "baselines", {"NIFTY": 7}), "bad_baselines"),
            (lambda p: _mutated(p, "entries", {"not": "a list"}), "bad_entries"),
            (lambda p: _mutated(p, "entries", ["not a dict"]), "bad_entry_shape"),
            (
                lambda p: _mutated(p, "entries", [{"rank": 1}]),
                "bad_entry_shape",
            ),
            (
                lambda p: _mutated(p, "entries", [dict(p["entries"][0], rank=2)]),
                "bad_entry_rank_order",
            ),
        ],
    )
    def test_damaged_payload_shapes_return_none(
        self, tmp_path: Path, mutate: Any, reason: str
    ) -> None:
        payload_bytes = mutate(self._valid_payload())
        self._write_raw(tmp_path, payload_bytes)
        with capture_logs() as logs:
            assert load_top100(tmp_path, _RUN) is None
        assert any(log.get("reason") == reason for log in logs)

    def test_rebuild_after_corruption_converges_to_identical_bytes(self, tmp_path: Path) -> None:
        artifact = self._artifact()
        path = write_top100(tmp_path, artifact)
        pristine = path.read_bytes()
        path.write_bytes(b"corrupted")
        assert load_top100(tmp_path, _RUN) is None
        write_top100(tmp_path, self._artifact())  # the deterministic rebuild
        assert path.read_bytes() == pristine
        assert load_top100(tmp_path, _RUN) is not None


def _mutated(payload: dict[str, Any], key: str, value: object) -> bytes:
    mutated = dict(payload)
    mutated[key] = value
    return (json.dumps(mutated, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


class TestWithAggHelper:
    def test_with_agg_replaces_only_the_agg(self) -> None:
        cand = make_candidate(1, fingerprint=fingerprint_seed(1))
        bumped = with_agg(cand, composite_score=9.0)
        assert bumped.agg.composite_score == 9.0
        assert bumped.row is cand.row


class TestSchemaVersionPin:
    def test_schema_version_is_two(self) -> None:
        # Bumped 1→2 by the resilience annotation (each entry now carries a
        # top-level ``resilience`` block); an old top100.json reads absent.
        assert TOP100_SCHEMA_VERSION == 2


class TestLoadsObjectBelt:
    def test_non_object_agg_json_column_raises_loudly(self) -> None:
        from brutex.engine.top100_artifact import _loads_object

        with pytest.raises(Top100Error, match="did not decode to an object"):
            _loads_object("[1,2,3]")


# ── DEC-RESULTS-CASCADE-VERDICT-JOIN-001: the cascade join at the writer ──

from pathlib import Path as _Path  # noqa: E402

from brutex.engine.cascade_join import CascadeJoin, CascadeVerdictView  # noqa: E402
from brutex.engine.top100_artifact import (  # noqa: E402
    CASCADE_STATE_JOINED,
    CASCADE_STATE_NO_REPORT,
    CASCADE_STATE_SWEEP_ID_MISMATCH,
)

_FIXTURES = _Path(__file__).resolve().parents[1] / "golden" / "fixtures"
_PRE_CASCADE_TOP100 = _FIXTURES / "top100_pre_cascade_v1" / ("grid" + "0" * 60) / "top100.json"


def _view(cid: str, **overrides: Any) -> CascadeVerdictView:
    base: dict[str, Any] = {
        "candidate_id": cid,
        "instrument": "NIFTY",
        "tf": "1minute",
        "passed": True,
        "rejected_at": None,
        "detail": "survived all Stage-4 filters",
        "cascade_composite": 1.5,
        "provisional": False,
        "cpcv_pass_rate": 0.75,
        "survivor_rank": 1,
    }
    base.update(overrides)
    return CascadeVerdictView(**base)


def _join(
    views: dict[str, CascadeVerdictView],
    *,
    sweep_id: str = _RUN,
    ambiguous: frozenset[str] = frozenset(),
) -> CascadeJoin:
    return CascadeJoin(
        sweep_id=sweep_id,
        version=1,
        n_input=len(views) + len(ambiguous),
        n_survived=sum(1 for v in views.values() if v.passed),
        by_candidate=views,
        ambiguous_ids=ambiguous,
        groups=(),
        skipped_groups=0,
        skipped_verdicts=0,
        nulled_values=0,
        rank_inconsistencies=0,
    )


def _fixture_pair() -> list[Top100Candidate]:
    """The EXACT candidate pair the frozen pre-change fixture was built from."""
    from tests.fakes.wp4_twin_fixtures import fingerprint_seed as _fp

    return [
        make_candidate(1, fingerprint=_fp("fp-a")),  # type: ignore[arg-type]
        make_candidate(
            2,
            fingerprint=_fp("fp-b"),  # type: ignore[arg-type]
            row_overrides={"sharpe_ratio": 0.9, "max_drawdown_pct": 12.0},
        ),
    ]


class TestCascadeJoinAtWriter:
    def test_no_report_state(self) -> None:
        artifact = _build([make_candidate(1, fingerprint=fingerprint_seed(1))])
        assert artifact.cascade_join == {
            "state": CASCADE_STATE_NO_REPORT,
            "sweep_id": None,
            "n_input": None,
            "n_survived": None,
        }
        (entry,) = artifact.entries
        assert entry["cascade"] == {"status": "not_validated", "reason": "no_report"}
        assert "cpcv_pass_pct" not in entry["metrics"]  # type: ignore[operator]

    def test_sweep_id_gate_mismatch_drops_join_loudly(self) -> None:
        join = _join({run_id_of(1): _view(run_id_of(1))}, sweep_id="other" + "0" * 59)
        with capture_logs() as logs:
            artifact = _build(
                [make_candidate(1, fingerprint=fingerprint_seed(1))], cascade_join=join
            )
        assert artifact.cascade_join == {
            "state": CASCADE_STATE_SWEEP_ID_MISMATCH,
            "sweep_id": "other" + "0" * 59,
            "n_input": None,
            "n_survived": None,
        }
        (entry,) = artifact.entries
        assert entry["cascade"] == {
            "status": "not_validated",
            "reason": "sweep_id_mismatch",
        }
        assert "cpcv_pass_pct" not in entry["metrics"]  # type: ignore[operator]
        (warn,) = [e for e in logs if e["event"] == "top100.cascade_sweep_id_mismatch"]
        assert warn["run_id"] == _RUN
        assert warn["report_sweep_id"] == "other" + "0" * 59

    def test_joined_pass_provisional_fail_and_absent(self) -> None:
        views = {
            run_id_of(1): _view(run_id_of(1), cascade_composite=2.5, cpcv_pass_rate=0.9),
            run_id_of(2): _view(
                run_id_of(2), provisional=True, cascade_composite=1.0, survivor_rank=2
            ),
            run_id_of(3): _view(
                run_id_of(3),
                passed=False,
                rejected_at="walk_forward",
                detail="anchored pass-rate 0.5000 < 0.75",
                cascade_composite=None,
                cpcv_pass_rate=0.625,
                survivor_rank=None,
            ),
        }
        candidates = [
            make_candidate(1, fingerprint=fingerprint_seed(1)),
            make_candidate(2, fingerprint=fingerprint_seed(2)),
            make_candidate(3, fingerprint=fingerprint_seed(3)),
            make_candidate(4, fingerprint=fingerprint_seed(4)),
        ]
        artifact = _build(candidates, cascade_join=_join(views))
        assert artifact.cascade_join is not None
        assert artifact.cascade_join["state"] == CASCADE_STATE_JOINED
        assert artifact.cascade_join["sweep_id"] == _RUN
        by_run = {entry["run_id"]: entry for entry in artifact.entries}
        one = by_run[run_id_of(1)]
        assert one["cascade"] == {
            "status": "pass",
            "rejected_at": None,
            "detail": "survived all Stage-4 filters",
            "cascade_composite": 2.5,
            "provisional": False,
            "cpcv_pass_rate": 0.9,
            "survivor_rank": 1,
        }
        # The display alias is the EXACT float unit projection.
        assert one["metrics"]["cpcv_pass_pct"] == 0.9 * 100.0  # type: ignore[index]
        two = by_run[run_id_of(2)]
        assert two["cascade"]["status"] == "provisional_pass"  # type: ignore[index]
        assert two["cascade"]["provisional"] is True  # type: ignore[index]
        three = by_run[run_id_of(3)]
        assert three["cascade"] == {
            "status": "fail",
            "rejected_at": "walk_forward",
            "detail": "anchored pass-rate 0.5000 < 0.75",
            "cascade_composite": None,
            "provisional": False,
            "cpcv_pass_rate": 0.625,
            "survivor_rank": None,
        }
        assert three["metrics"]["cpcv_pass_pct"] == 0.625 * 100.0  # type: ignore[index]
        four = by_run[run_id_of(4)]
        assert four["cascade"] == {
            "status": "not_validated",
            "reason": "not_in_cascade_pool",
        }
        assert "cpcv_pass_pct" not in four["metrics"]  # type: ignore[operator]

    def test_fail_rank_one_stays_rank_one(self) -> None:
        # R13: annotation NEVER reranks — the min_dd_first winner keeps rank 1
        # even when the cascade FAILED it (the display flags it instead).
        views = {
            run_id_of(1): _view(
                run_id_of(1),
                passed=False,
                rejected_at="pbo",
                cascade_composite=None,
                cpcv_pass_rate=None,
                survivor_rank=None,
            )
        }
        candidates = [
            make_candidate(1, fingerprint=fingerprint_seed(1)),
            make_candidate(
                2, fingerprint=fingerprint_seed(2), row_overrides={"max_drawdown_pct": 20.0}
            ),
        ]
        artifact = _build(candidates, cascade_join=_join(views))
        assert artifact.entries[0]["run_id"] == run_id_of(1)
        assert artifact.entries[0]["cascade"]["status"] == "fail"  # type: ignore[index]
        # A fail with no measured rate emits NO pct key — never a 0.
        assert "cpcv_pass_pct" not in artifact.entries[0]["metrics"]  # type: ignore[operator]

    def test_duplicate_candidate_id_reads_ambiguous(self) -> None:
        join = _join({}, ambiguous=frozenset({run_id_of(1)}))
        artifact = _build([make_candidate(1, fingerprint=fingerprint_seed(1))], cascade_join=join)
        (entry,) = artifact.entries
        assert entry["cascade"] == {
            "status": "not_validated",
            "reason": "duplicate_candidate_id",
        }

    def test_join_bytes_deterministic(self) -> None:
        join = _join({run_id_of(1): _view(run_id_of(1))})
        one = _build(
            [make_candidate(1, fingerprint=fingerprint_seed(1))], cascade_join=join
        ).rendered_bytes()
        two = _build(
            [make_candidate(1, fingerprint=fingerprint_seed(1))], cascade_join=join
        ).rendered_bytes()
        assert one == two

    def test_entry_required_keys_not_extended(self) -> None:
        # Extending ENTRY_REQUIRED_KEYS would flip every pre-change artifact
        # + fixture to ABSENT on load — pinned NOT to grow.
        assert (
            frozenset(
                {
                    "rank",
                    "run_id",
                    "mask_hex",
                    "conditions",
                    "instrument",
                    "tf",
                    "direction",
                    "arm",
                    "metrics",
                    "regime",
                    "vix_band",
                    "family_id",
                    "family_members",
                    "reasons",
                    "margin_roi",
                    "equity_ref",
                }
            )
            == ENTRY_REQUIRED_KEYS
        )
        assert "cascade" not in ENTRY_REQUIRED_KEYS

    def test_no_kwarg_build_is_additive_only_vs_frozen_pre_change_fixture(self) -> None:
        """Stripping ONLY the new keys reproduces the frozen pre-change bytes.

        The frozen fixture predates THREE additive deltas now in the tree: the
        #1749 ``cascade_join``/``cascade`` keys (this DEC's), main's
        DEC-TRADESIM-OPEN-SESSION-GUARD-001 (MIGRATION.md #1743)
        ``metrics.exits["session_gap"]`` key, AND — since the club-O
        integration merge — DEC-RESILIENCE-STREAKS-001 (MIGRATION.md #1959)'s
        per-entry ``resilience`` block, whose payload-shape change carries the
        DELIBERATE ``schema_version`` bump 1→2 (bump-on-any-shape-change law,
        documented on ``TOP100_SCHEMA_VERSION``). The strip normalizes all
        three (the version compared explicitly: live == 2, frozen == 1), so
        the pin still proves the entry/meta deltas are additive-only.
        """
        frozen = json.loads(_PRE_CASCADE_TOP100.read_text(encoding="utf-8"))
        artifact = build_top100(
            _fixture_pair(),
            run_id="grid" + "0" * 60,
            preset="min_dd_first",
            families=_NO_FAMILIES,
            counts=_counts(),
            data_snapshot_hashes=("snap0001",),
            baselines={"NIFTY": "baseline-NIFTY.json"},
        )
        payload = artifact.to_payload()
        stripped = dict(payload)
        stripped.pop("cascade_join")
        # The #1959 deliberate version bump — asserted, then normalized so the
        # remaining compare proves every OTHER delta is additive-only.
        assert stripped["schema_version"] == TOP100_SCHEMA_VERSION == 2
        assert frozen["schema_version"] == 1
        stripped["schema_version"] = frozen["schema_version"]

        def _strip_entry(entry: dict[str, object]) -> dict[str, object]:
            out = {k: v for k, v in entry.items() if k not in {"cascade", "resilience"}}
            metrics = dict(cast("dict[str, object]", out["metrics"]))
            exits = dict(cast("dict[str, object]", metrics["exits"]))
            session_gap_count = exits.pop("session_gap")
            assert session_gap_count == 0  # the #1743 zero-count delta
            metrics["exits"] = exits
            out["metrics"] = metrics
            return out

        stripped["entries"] = [
            _strip_entry(entry)
            for entry in payload["entries"]  # type: ignore[union-attr]
        ]
        assert stripped == frozen

    def test_frozen_pre_change_artifact_reads_absent_never_crashes(self, tmp_path: Path) -> None:
        """The back-compat oracle UNDER the merged #1959 schema-v2 law.

        DEC-RESILIENCE-STREAKS-001 (MIGRATION.md #1959) bumped
        ``TOP100_SCHEMA_VERSION`` 1→2 with the documented reader law: an
        artifact at an UNKNOWN version reads ABSENT (→ deterministic rebuild),
        never a crash and never a silently mis-shaped load — so the frozen v1
        pre-change fixture now loads ``None`` via the NAMED
        ``schema_version_mismatch`` degrade (the pre-club version of this test
        asserted a successful v1 load; superseded by the #1959 bump).
        """
        import shutil

        from structlog.testing import capture_logs

        run_id = "grid" + "0" * 60
        shutil.copytree(_PRE_CASCADE_TOP100.parent.parent, tmp_path / "runs")
        with capture_logs() as logs:
            loaded = load_top100(tmp_path / "runs", run_id)
        assert loaded is None  # absent-by-version, the #1959 rebuild law
        (event,) = [e for e in logs if e["event"] == "top100.load_invalid"]
        assert event["reason"] == "schema_version_mismatch"

    def test_load_round_trips_cascade_join_meta(self, tmp_path: Path) -> None:
        join = _join({run_id_of(1): _view(run_id_of(1))})
        artifact = _build([make_candidate(1, fingerprint=fingerprint_seed(1))], cascade_join=join)
        write_top100(tmp_path, artifact)
        loaded = load_top100(tmp_path, _RUN)
        assert loaded is not None
        assert loaded.cascade_join == artifact.cascade_join
        assert loaded.entries[0]["cascade"] == artifact.entries[0]["cascade"]


# ── `DEC-OPTIONS-EXEC-FEEDBACK-001` / DEC-FILL-EXEC-GRID-1M-001: the exec-sim
#    provider seams + the net_after_cost preset at the writer ─────────────────


def _priced_verdict(net: int) -> dict[str, object]:
    """A minimal PRICED options-exec verdict (headline net, int64 paisa)."""
    return {"status": "priced", "net_after_cost_paisa": net, "window": "2024-10-01.."}


class TestExecProviderNoneByteIdentity:
    """The LOAD-BEARING pin: ALL providers ``None`` ⇒ pre-change bytes.

    `DEC-OPTIONS-EXEC-FEEDBACK-001` / DEC-FILL-EXEC-GRID-1M-001's emit-on-wired
    law — a build with every provider kwarg ``None`` (explicit OR defaulted)
    must be BYTE-IDENTICAL to the pre-change output: no ``options_exec`` /
    ``futures_exec`` / ``fill_sensitivity`` entry key, no ``*_wired`` source
    flag, anywhere.
    """

    def _pair(self) -> list[Top100Candidate]:
        return [
            make_candidate(1, fingerprint=fingerprint_seed(1)),
            make_candidate(
                2, fingerprint=fingerprint_seed(2), row_overrides={"max_drawdown_pct": 20.0}
            ),
        ]

    def test_default_build_equals_explicit_all_none_providers_byte_for_byte(self) -> None:
        default = _build(self._pair())
        explicit = _build(
            self._pair(),
            options_exec_provider=None,
            futures_exec_provider=None,
            fill_sensitivity_provider=None,
        )
        assert default.to_payload() == explicit.to_payload()
        # The json BYTE law itself, not just dict equality.
        assert json.dumps(default.to_payload(), sort_keys=True) == json.dumps(
            explicit.to_payload(), sort_keys=True
        )
        assert default.rendered_bytes() == explicit.rendered_bytes()

    def test_provider_none_artifact_carries_no_new_keys_anywhere(self) -> None:
        artifact = _build(
            self._pair(),
            options_exec_provider=None,
            futures_exec_provider=None,
            fill_sensitivity_provider=None,
        )
        assert artifact.entries  # anti-vacuous: there ARE entries to inspect
        for entry in artifact.entries:
            assert "options_exec" not in entry
            assert "futures_exec" not in entry
            assert "fill_sensitivity" not in entry
        for flag in ("options_exec_wired", "futures_exec_wired", "fill_sensitivity_wired"):
            assert flag not in artifact.source
        # Belt: the tokens appear NOWHERE in the rendered bytes.
        rendered = artifact.rendered_bytes()
        for token in (b"options_exec", b"futures_exec", b"fill_sensitivity"):
            assert token not in rendered


class TestExecProvidersWired:
    """Wired providers: exact objects on matching entries + emit-on-wired flags."""

    def test_wired_providers_stamp_the_exact_objects_and_flags(self) -> None:
        a = make_candidate(1, fingerprint=fingerprint_seed(1))
        b = make_candidate(2, fingerprint=fingerprint_seed(2))
        options_by_id: dict[str, dict[str, object]] = {
            a.row.run_id: _priced_verdict(12_345),
            b.row.run_id: {"status": "not_priced", "reason": "no_option_chain"},
        }
        futures_by_id: dict[str, dict[str, object]] = {
            a.row.run_id: {"status": "priced", "net_pnl_paisa": 999},
            b.row.run_id: {"status": "not_priced", "reason": "lot_size_unknown"},
        }
        fill_by_id: dict[str, dict[str, object]] = {
            a.row.run_id: {"status": "priced", "net_delta_paisa": -40},
            b.row.run_id: {"status": "not_priced", "reason": "no_1m_bars"},
        }
        artifact = _build(
            [a, b],
            options_exec_provider=lambda c: options_by_id[c.row.run_id],
            futures_exec_provider=lambda c: futures_by_id[c.row.run_id],
            fill_sensitivity_provider=lambda c: fill_by_id[c.row.run_id],
        )
        by_run = {entry["run_id"]: entry for entry in artifact.entries}
        for cand in (a, b):
            rid = cand.row.run_id
            # The objects are dict(...)-copied onto the entry — EQUALITY is
            # the contract (identity cannot survive the copy, by design).
            assert by_run[rid]["options_exec"] == options_by_id[rid]
            assert by_run[rid]["futures_exec"] == futures_by_id[rid]
            assert by_run[rid]["fill_sensitivity"] == fill_by_id[rid]
        assert artifact.source["options_exec_wired"] is True
        assert artifact.source["futures_exec_wired"] is True
        assert artifact.source["fill_sensitivity_wired"] is True
        # The pre-existing UNCONDITIONAL flags are untouched beside the new ones.
        assert artifact.source["reasons_wired"] is False
        assert artifact.source["margin_roi_wired"] is False

    def test_futures_and_fill_per_candidate_none_leaves_that_entry_keyless(self) -> None:
        """The ``_entry_payload`` ``is not None`` guard, pinned AS-IS.

        The provider aliases document TOTAL-when-wired; the runtime tolerance
        beyond that contract is the entry-level guard — a wired futures/fill
        provider whose return for ONE candidate is ``None`` leaves THAT entry
        key-less while the source flag stays True (wiredness is the
        PROVIDER's presence, never per-entry). Deliberately NOT exercised for
        the OPTIONS provider: its returns feed the ``net_after_cost_of`` fold
        (``verdict.get``), so TOTAL is load-bearing there and a ``None``
        return is a contract violation, not a behavior to pin.
        """
        a = make_candidate(1, fingerprint=fingerprint_seed(1))
        b = make_candidate(2, fingerprint=fingerprint_seed(2))
        futures_map: dict[str, dict[str, object]] = {
            a.row.run_id: {"status": "priced", "net_pnl_paisa": 1}
        }
        fill_map: dict[str, dict[str, object]] = {a.row.run_id: {"status": "not_priced"}}
        artifact = _build(
            [a, b],
            futures_exec_provider=lambda c: futures_map.get(c.row.run_id),
            fill_sensitivity_provider=lambda c: fill_map.get(c.row.run_id),
        )
        by_run = {entry["run_id"]: entry for entry in artifact.entries}
        assert by_run[a.row.run_id]["futures_exec"] == {"status": "priced", "net_pnl_paisa": 1}
        assert by_run[a.row.run_id]["fill_sensitivity"] == {"status": "not_priced"}
        assert "futures_exec" not in by_run[b.row.run_id]
        assert "fill_sensitivity" not in by_run[b.row.run_id]
        assert artifact.source["futures_exec_wired"] is True
        assert artifact.source["fill_sensitivity_wired"] is True
        # options stayed unwired here — no key, no flag.
        assert "options_exec" not in by_run[a.row.run_id]
        assert "options_exec_wired" not in artifact.source

    def test_providers_resolve_once_per_representative_only(self) -> None:
        # "resolved ONCE per gate-passing representative BEFORE ranking":
        # a gate-FAILER and a family-COLLAPSED twin are never offered to the
        # providers — only the shipped representative, exactly once each seam.
        simple = make_candidate(
            2, fingerprint=fingerprint_seed(9), row_overrides={"mask_bits": 0b1}
        )
        complex_twin = make_candidate(
            1, fingerprint=fingerprint_seed(9), row_overrides={"mask_bits": 0b111}
        )
        failer = make_candidate(
            3, fingerprint=fingerprint_seed(3), row_overrides={"profit_factor": 1.0}
        )
        families = group_twins(
            [
                TwinCandidate(
                    run_id=c.row.run_id,
                    mask_bits=c.row.mask_bits,
                    fingerprint=c.agg.fingerprint,
                    entry_ts_us=frozenset({1, 2, 3}),
                )
                for c in (simple, complex_twin)
            ]
        )
        calls: list[str] = []

        def provider(c: Top100Candidate) -> dict[str, object]:
            calls.append(c.row.run_id)
            return {"status": "not_priced", "reason": "n/a"}

        artifact = _build(
            [simple, complex_twin, failer],
            families=families,
            options_exec_provider=provider,
            futures_exec_provider=provider,
            fill_sensitivity_provider=provider,
        )
        assert [e["run_id"] for e in artifact.entries] == [simple.row.run_id]
        # Three seams × the ONE representative — never the failer/twin.
        assert calls == [simple.row.run_id] * 3

    def test_fill_sensitivity_resolved_only_for_ranked_entries(self) -> None:
        # DEC-FILL-EXEC-GRID-1M-001: each fill call is one O(bars) re-sim, so
        # it resolves ONLY for the RANKED entries — while the options seam
        # resolves for EVERY representative (its nets feed ranking).
        many = [
            make_candidate(
                n,
                fingerprint=fingerprint_seed(n),
                row_overrides={"max_drawdown_pct": float(n % 29) + 0.5},
            )
            for n in range(1, TOP100_LIMIT + 6)
        ]
        options_calls: list[str] = []
        fill_calls: list[str] = []

        def options_provider(c: Top100Candidate) -> dict[str, object]:
            options_calls.append(c.row.run_id)
            return {"status": "not_priced", "reason": "n/a"}

        def fill_provider(c: Top100Candidate) -> dict[str, object]:
            fill_calls.append(c.row.run_id)
            return {"status": "not_priced", "reason": "n/a"}

        artifact = _build(
            many, options_exec_provider=options_provider, fill_sensitivity_provider=fill_provider
        )
        assert len(artifact.entries) == TOP100_LIMIT
        assert sorted(options_calls) == sorted(c.row.run_id for c in many)  # every rep
        ranked_ids = [str(e["run_id"]) for e in artifact.entries]
        assert sorted(fill_calls) == sorted(ranked_ids)  # ranked only
        assert len(fill_calls) == TOP100_LIMIT

    def test_wired_artifact_round_trips_through_write_and_load(self, tmp_path: Path) -> None:
        # ENTRY_REQUIRED_KEYS is untouched, so the presence-tolerant reader
        # round-trips the wired keys verbatim.
        cand = make_candidate(1, fingerprint=fingerprint_seed(1))
        artifact = _build(
            [cand],
            options_exec_provider=lambda c: _priced_verdict(777),
            futures_exec_provider=lambda c: {"status": "not_priced", "reason": "n/a"},
            fill_sensitivity_provider=lambda c: {"status": "not_priced", "reason": "n/a"},
        )
        write_top100(tmp_path, artifact)
        loaded = load_top100(tmp_path, _RUN)
        assert loaded is not None
        assert loaded.to_payload() == artifact.to_payload()
        assert loaded.entries[0]["options_exec"] == _priced_verdict(777)
        assert loaded.source["options_exec_wired"] is True


class TestNetAfterCostPresetAtWriter:
    """build_top100(preset="net_after_cost") over a wired options-exec map."""

    def test_ranks_priced_desc_then_unpriced_by_run_id_asc(self) -> None:
        # Hand-derived expected order (net_after_cost: reverse=True, int-exact,
        # unpriced entries LAST):
        #   run 3 — priced net +2_000_000            → rank 1 (largest net)
        #   run 1 — priced net    -50_000            → rank 2 (negative but
        #                                              PRICED beats unpriced)
        #   run 2 — status "not_priced" (its embedded 9_999_999 int MUST be
        #           ignored — the status gate)       → unpriced pool
        #   run 4 — status "priced" but a FLOAT net (9.9e15): the isinstance
        #           int gate refuses it              → unpriced pool
        #   the unpriced pool orders by run_id ASC → run 2 (…02) before run 4.
        rich = make_candidate(3, fingerprint=fingerprint_seed(3))
        modest = make_candidate(1, fingerprint=fingerprint_seed(1))
        hostile_not_priced = make_candidate(2, fingerprint=fingerprint_seed(2))
        float_net = make_candidate(4, fingerprint=fingerprint_seed(4))
        verdicts: dict[str, dict[str, object]] = {
            rich.row.run_id: _priced_verdict(2_000_000),
            modest.row.run_id: _priced_verdict(-50_000),
            hostile_not_priced.row.run_id: {
                "status": "not_priced",
                "reason": "no_option_chain",
                "net_after_cost_paisa": 9_999_999,
            },
            float_net.row.run_id: {"status": "priced", "net_after_cost_paisa": 9.9e15},
        }
        artifact = _build(
            [modest, hostile_not_priced, rich, float_net],
            preset="net_after_cost",
            options_exec_provider=lambda c: verdicts[c.row.run_id],
        )
        assert artifact.preset == "net_after_cost"
        assert [e["rank"] for e in artifact.entries] == [1, 2, 3, 4]
        assert [e["run_id"] for e in artifact.entries] == [
            rich.row.run_id,
            modest.row.run_id,
            hostile_not_priced.row.run_id,
            float_net.row.run_id,
        ]
        # The SAME resolved verdicts land on the entries (never a double resolve).
        by_run = {e["run_id"]: e for e in artifact.entries}
        assert by_run[rich.row.run_id]["options_exec"] == _priced_verdict(2_000_000)
        assert by_run[float_net.row.run_id]["options_exec"] == {
            "status": "priced",
            "net_after_cost_paisa": 9.9e15,
        }

    def test_wired_options_nets_are_inert_under_min_dd_first(self) -> None:
        # The nets feed ONLY the net_after_cost preset's key — a huge net on
        # the WORSE-drawdown candidate must not rerank min_dd_first, and the
        # wired order equals the unwired build's (R13: annotation never
        # reranks).
        calm = make_candidate(
            1, fingerprint=fingerprint_seed(1), row_overrides={"max_drawdown_pct": 5.0}
        )
        deep = make_candidate(
            2, fingerprint=fingerprint_seed(2), row_overrides={"max_drawdown_pct": 25.0}
        )
        verdicts: dict[str, dict[str, object]] = {
            calm.row.run_id: _priced_verdict(-1),
            deep.row.run_id: _priced_verdict(10**15),
        }
        unwired = _build([calm, deep])
        wired = _build([calm, deep], options_exec_provider=lambda c: verdicts[c.row.run_id])
        assert (
            [e["run_id"] for e in wired.entries]
            == [e["run_id"] for e in unwired.entries]
            == [calm.row.run_id, deep.row.run_id]
        )

    def test_to_payload_elides_cascade_join_when_none(self) -> None:
        """A ``cascade_join=None`` artifact ELIDES the root key (EMIT-ON-PRESENT).

        ``build_top100`` has stamped a cascade_join meta unconditionally since
        #1749, so the ``None`` arm is reachable only on a directly-constructed
        (legacy-shaped) artifact object — e.g. one whose stored payload lacked
        the key at load. Regression pin: the club-O #1959 schema-v2 reader law
        turned the frozen-v1 fixture load into an ABSENT read, which silently
        dropped the suite's only exercise of this arm (the CI 100%-branch
        coverage gate caught the hole — ``to_payload`` 368->370).
        """
        base = _build([make_candidate(1, fingerprint=fingerprint_seed(1))])
        assert base.cascade_join is not None  # the writer-stamped default
        legacy = replace(base, cascade_join=None)
        payload = legacy.to_payload()
        assert "cascade_join" not in payload
        with_key = dict(base.to_payload())
        with_key.pop("cascade_join")
        assert payload == with_key  # the ONLY delta is the elided root key
        assert legacy.rendered_bytes().endswith(b"\n")
