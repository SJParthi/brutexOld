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
    def test_schema_version_is_one(self) -> None:
        assert TOP100_SCHEMA_VERSION == 1


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

        The frozen fixture predates BOTH additive deltas now in the tree: the
        #1749 ``cascade_join``/``cascade`` keys (this DEC's) AND main's
        DEC-TRADESIM-OPEN-SESSION-GUARD-001 (MIGRATION.md #1743)
        ``metrics.exits["session_gap"]`` key — the strip normalizes both, so
        the pin still proves the deltas are additive-only.
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

        def _strip_entry(entry: dict[str, object]) -> dict[str, object]:
            out = {k: v for k, v in entry.items() if k != "cascade"}
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

    def test_frozen_pre_change_artifact_still_loads(self, tmp_path: Path) -> None:
        """The back-compat oracle: a pre-change artifact loads fine, meta None."""
        import shutil

        run_id = "grid" + "0" * 60
        shutil.copytree(_PRE_CASCADE_TOP100.parent.parent, tmp_path / "runs")
        loaded = load_top100(tmp_path / "runs", run_id)
        assert loaded is not None
        assert loaded.cascade_join is None
        assert all("cascade" not in entry for entry in loaded.entries)
        # A None meta is NOT re-emitted — the round-trip payload byte-law.
        assert "cascade_join" not in loaded.to_payload()

    def test_load_round_trips_cascade_join_meta(self, tmp_path: Path) -> None:
        join = _join({run_id_of(1): _view(run_id_of(1))})
        artifact = _build([make_candidate(1, fingerprint=fingerprint_seed(1))], cascade_join=join)
        write_top100(tmp_path, artifact)
        loaded = load_top100(tmp_path, _RUN)
        assert loaded is not None
        assert loaded.cascade_join == artifact.cascade_join
        assert loaded.entries[0]["cascade"] == artifact.entries[0]["cascade"]


# ---------------------------------------------------------------------------
# DEC-ALPHA-BUYHOLD-METRIC-001 — the alpha-vs-buy-&-hold display metric + the
# DEFAULT-OFF rejection gate at the writer.
# ---------------------------------------------------------------------------

from brutex.engine.alpha_metric import AlphaBaseline  # noqa: E402

#: A hand-derivable baseline: anchor ₹20,000.00 → close ₹20,400.00 = +2%.
_ALPHA_BASELINE = AlphaBaseline(anchor_open_paisa=2_000_000, last_close_paisa=2_040_000)

#: Start equity ₹10,000.00 = 1_000_000 paisa (the fixture rows' default
#: net_pnl_paisa of 10_000 then reads +1%: alpha = 1 − 2 = −1.0 exactly).
_ALPHA_EQUITY = 1_000_000


def _alpha_kwargs(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "alpha_baselines": {"NIFTY": _ALPHA_BASELINE},
        "start_equity_paisa": _ALPHA_EQUITY,
    }
    base.update(overrides)
    return base


class TestAlphaMetricsAtWriter:
    def test_default_build_bytes_are_identical_without_alpha_inputs(self) -> None:
        """Defaults (no alpha_baselines / start_equity / gate) == pre-alpha bytes."""
        candidates = [make_candidate(1, fingerprint=fingerprint_seed(1))]
        assert (
            _build(candidates).rendered_bytes()
            == _build(
                candidates, alpha_baselines=None, start_equity_paisa=None, alpha_gate=False
            ).rendered_bytes()
        )
        # An EMPTY baselines map is also a no-op (no instrument covered).
        assert (
            _build(
                candidates, alpha_baselines={}, start_equity_paisa=_ALPHA_EQUITY
            ).rendered_bytes()
            == _build(candidates).rendered_bytes()
        )

    def test_alpha_build_is_additive_only_vs_the_default_build(self) -> None:
        """Stripping the three alpha metric keys recovers the default payload."""
        candidates = [make_candidate(1, fingerprint=fingerprint_seed(1))]
        default_payload = _build(candidates).to_payload()
        alpha_payload = _build(candidates, **_alpha_kwargs()).to_payload()
        for entry in alpha_payload["entries"]:
            metrics = entry["metrics"]
            for key in ("alpha_pct", "strategy_return_pct", "bh_return_pct"):
                assert key in metrics
                metrics.pop(key)
        assert alpha_payload == default_payload

    def test_alpha_values_hand_derived(self) -> None:
        # net +₹100 on ₹10,000 = +1%; B&H ₹20,000 → ₹20,400 = +2%; alpha −1.0.
        artifact = _build([make_candidate(1, fingerprint=fingerprint_seed(1))], **_alpha_kwargs())
        metrics = artifact.entries[0]["metrics"]
        assert metrics["strategy_return_pct"] == 1.0
        assert metrics["bh_return_pct"] == 2.0
        assert metrics["alpha_pct"] == -1.0

    def test_uncovered_instrument_emits_no_alpha_keys(self) -> None:
        artifact = _build(
            [make_candidate(1, fingerprint=fingerprint_seed(1))],
            **_alpha_kwargs(alpha_baselines={"SENSEX": _ALPHA_BASELINE}),
        )
        metrics = artifact.entries[0]["metrics"]
        for key in ("alpha_pct", "strategy_return_pct", "bh_return_pct"):
            assert key not in metrics

    def test_missing_start_equity_emits_no_alpha_keys(self) -> None:
        artifact = _build(
            [make_candidate(1, fingerprint=fingerprint_seed(1))],
            alpha_baselines={"NIFTY": _ALPHA_BASELINE},
            start_equity_paisa=None,
        )
        metrics = artifact.entries[0]["metrics"]
        assert "alpha_pct" not in metrics


class TestAlphaRejectionGate:
    def test_gate_off_by_default_source_carries_no_gate_keys(self) -> None:
        artifact = _build([make_candidate(1, fingerprint=fingerprint_seed(1))], **_alpha_kwargs())
        assert "alpha_gate" not in artifact.source
        assert "alpha_gate_excluded" not in artifact.source

    def test_gate_excludes_nonpositive_computable_alpha_counted_and_named(self) -> None:
        # positive alpha: net +₹500 on ₹10,000 = +5% vs B&H +2% → alpha +3.
        winner = make_candidate(
            1, fingerprint=fingerprint_seed(1), row_overrides={"net_pnl_paisa": 50_000}
        )
        # negative alpha: the default net +1% vs B&H +2% → alpha −1 (excluded).
        loser = make_candidate(2, fingerprint=fingerprint_seed(2))
        # ZERO alpha: net +₹200 on ₹10,000 = +2% == B&H +2% → alpha 0 (<= 0,
        # excluded — the boundary law).
        boundary = make_candidate(
            3, fingerprint=fingerprint_seed(3), row_overrides={"net_pnl_paisa": 20_000}
        )
        # UNCOMPUTABLE alpha (no baseline for SENSEX): NEVER excluded.
        uncovered = make_candidate(
            4,
            fingerprint=fingerprint_seed(4),
            row_overrides={"instrument": "SENSEX", "net_pnl_paisa": -10_000},
        )
        with capture_logs() as logs:
            artifact = _build(
                [winner, loser, boundary, uncovered],
                **_alpha_kwargs(alpha_gate=True),
            )
        kept = [entry["run_id"] for entry in artifact.entries]
        assert winner.row.run_id in kept
        assert uncovered.row.run_id in kept
        assert loser.row.run_id not in kept
        assert boundary.row.run_id not in kept
        assert artifact.source["alpha_gate"] is True
        assert artifact.source["alpha_gate_excluded"] == 2
        events = [log for log in logs if log["event"] == "top100.alpha_gate_excluded"]
        assert len(events) == 1
        assert events[0]["excluded"] == 2
        assert set(events[0]["sample"]) == {loser.row.run_id, boundary.row.run_id}

    def test_gate_on_with_zero_exclusions_is_counted_zero_no_warn(self) -> None:
        winner = make_candidate(
            1, fingerprint=fingerprint_seed(1), row_overrides={"net_pnl_paisa": 50_000}
        )
        with capture_logs() as logs:
            artifact = _build([winner], **_alpha_kwargs(alpha_gate=True))
        assert artifact.source["alpha_gate"] is True
        assert artifact.source["alpha_gate_excluded"] == 0
        assert [log for log in logs if log["event"] == "top100.alpha_gate_excluded"] == []

    def test_gate_without_alpha_inputs_excludes_nothing(self) -> None:
        """Gate ON but nothing computable — every passer kept (never judged
        on a number that does not exist)."""
        candidates = [make_candidate(1, fingerprint=fingerprint_seed(1))]
        artifact = _build(candidates, alpha_gate=True)
        assert len(artifact.entries) == 1
        assert artifact.source["alpha_gate_excluded"] == 0


class TestAlphaPreset:
    def test_alpha_preset_orders_by_alpha_desc_none_after_scored(self) -> None:
        low = make_candidate(
            1, fingerprint=fingerprint_seed(1), row_overrides={"net_pnl_paisa": 30_000}
        )  # +3% − 2% = +1
        high = make_candidate(
            2, fingerprint=fingerprint_seed(2), row_overrides={"net_pnl_paisa": 90_000}
        )  # +9% − 2% = +7
        uncovered = make_candidate(
            3, fingerprint=fingerprint_seed(3), row_overrides={"instrument": "SENSEX"}
        )  # None alpha — ranks LAST
        artifact = _build([low, high, uncovered], preset="alpha", **_alpha_kwargs())
        assert [entry["run_id"] for entry in artifact.entries] == [
            high.row.run_id,
            low.row.run_id,
            uncovered.row.run_id,
        ]
        assert artifact.preset == "alpha"

    def test_rank_candidates_default_map_matches_pre_alpha_order(self) -> None:
        candidates = [make_candidate(n, fingerprint=fingerprint_seed(n)) for n in (3, 1, 2)]
        no_map = rank_candidates(candidates, preset=DEFAULT_TOP100_PRESET)
        empty_map = rank_candidates(candidates, preset=DEFAULT_TOP100_PRESET, alpha_by_run_id={})
        assert [c.row.run_id for c in no_map] == [c.row.run_id for c in empty_map]
