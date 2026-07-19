"""Deterministic input corpus for the top100_artifact parity goldens.

DEC-RUST-CASCADE-RESULTS-001 phase B (slice 2b): the T14 lane converts the
``brutex.engine.top100_artifact`` builder/writer/reader LAW family to the
``rust/brutex-cascade`` crate under the compressed greenfield cycle
(capture-first — this corpus is frozen against the UNTOUCHED Python module
BEFORE any conversion). This module is the ONE builder of the parity corpus
INPUTS *and* the shared scenario RUNNERS — shared verbatim by

* the capture generator (``gen_cascade_rust_parity.py`` — run ONCE against the
  pre-conversion pure-Python implementations; fixture frozen), and
* the replay golden (``test_top100_artifact_rust_parity_golden.py`` — drives
  the post-conversion Rust-backed API against the frozen fixture),

so the two sides can never drift on what was fed in. Inputs are built in CODE
(never parsed from the fixture) because several hostile cases carry values
JSON cannot round-trip faithfully (``float('nan')`` aggregate scores, lone
surrogates, bool-vs-int puns).

Every case is fully deterministic: fixed labels, fixed numbers, no RNG, no
wall-clock in any CAPTURED value (``write_top100``'s stamp ``produced_at_utc``
is recorded only as ``is-a-string`` — the module's own reader contract), no
ambient I/O (the write/load runners use throwaway temp dirs whose paths never
reach a captured value; the ``top100.load_invalid`` WARN's ``runs_dir`` field
is deliberately NOT captured for exactly that reason).

The consumed laws stay Python and are treated as INPUT builders, not targets:
``brutex.ranking.presets`` (the preset ordering law the module delegates to),
``twin_detect.group_twins`` / hand-built :class:`TwinFamilies` (the family
partition), ``results_store.ResultRow`` / ``combo_aggregates.ComboAggregate``
construction validation (their refusals are THEIR laws — the corpus feeds only
constructible instances plus the in-module refusal shapes).
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from brutex.engine.cascade_join import CascadeJoin, CascadeVerdictView
from brutex.engine.top100_artifact import (
    TOP100_FILENAME,
    TOP100_SCHEMA_VERSION,
    TOP100_STAMP_FILENAME,
    PoolCounts,
    Top100Artifact,
    Top100Candidate,
    _dumps,
    build_top100,
    load_top100,
    write_top100,
)
from brutex.engine.twin_detect import TwinCandidate, TwinFamilies, group_twins
from structlog.testing import capture_logs

from tests.fakes.wp4_twin_fixtures import fingerprint_seed
from tests.fakes.wp5_depth_fixtures import make_candidate, run_id_of

__all__ = [
    "build_cases",
    "constructor_cases",
    "gate_cases",
    "load_scenarios",
    "preset_resolution_cases",
    "rank_cases",
    "run_build_case",
    "run_load_scenario",
    "run_write_roundtrip",
    "write_roundtrip_cases",
]

#: The grid identity every corpus build uses (a valid 64-hex token).
_RUN = "f0" * 32

#: The largest legal mask (mirrors ``results_store.MASK_BITS_MAX`` == (1<<60)-1).
_MAX_MASK = (1 << 60) - 1

#: A fixed, VALID produced_at string for hand-built stamps (the reader only
#: checks ``isinstance(str)`` — never parsed, so a constant is honest).
_FIXED_PRODUCED_AT = "2026-07-19T00:00:00+00:00"


def _empty_families() -> TwinFamilies:
    """No partition at all — every candidate is its own family (the lenient law)."""
    return TwinFamilies(family_by_run_id={}, members_by_family={})


def _counts(**overrides: int) -> PoolCounts:
    base = {"stores": 1, "excluded_no_aggs": 0, "zero_trade_excluded": 0, "parity_excluded": 0}
    base.update(overrides)
    return PoolCounts(**base)


def _view(cid: str, **overrides: object) -> CascadeVerdictView:
    """A passing cascade verdict view, overridable by keyword."""
    base: dict[str, object] = {
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
    return CascadeVerdictView(**base)  # type: ignore[arg-type]


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


# ──────────────────────────── hard gates ────────────────────────────


def gate_cases() -> list[tuple[str, Top100Candidate]]:
    """``(name, candidate)`` pairs for the ``passes_hard_gates`` boolean law.

    Every boundary of the 5-gate law is exercised on BOTH sides: strict-``<``
    on trades/PF, strict-``>`` on drawdown (30.0 exactly passes, the next
    representable double fails), the combined win-rate ∧ win/loss gate (with
    the ``None → 0.0`` substitution), and ``<=`` on the losing streak.
    """

    def cand(
        n: int, row: dict[str, Any] | None = None, agg: dict[str, Any] | None = None
    ) -> Top100Candidate:
        return make_candidate(
            n, fingerprint=fingerprint_seed(n), row_overrides=row, agg_overrides=agg
        )

    return [
        ("defaults_pass", cand(1)),
        (
            "trades_at_min",
            cand(2, row={"total_trades": 30, "winning_trades": 20, "losing_trades": 10}),
        ),
        (
            "trades_below_min",
            cand(3, row={"total_trades": 29, "winning_trades": 20, "losing_trades": 9}),
        ),
        ("pf_at_gate", cand(4, row={"profit_factor": 1.2})),
        ("pf_below_gate", cand(5, row={"profit_factor": 1.19})),
        ("dd_at_gate", cand(6, row={"max_drawdown_pct": 30.0})),
        ("dd_next_double_above_gate", cand(7, row={"max_drawdown_pct": 30.000000000000004})),
        ("low_wr_low_wlr_fails", cand(8, row={"win_rate": 0.24}, agg={"win_loss_ratio": 1.9})),
        ("low_wr_wlr_at_gate_passes", cand(9, row={"win_rate": 0.24}, agg={"win_loss_ratio": 2.0})),
        (
            "low_wr_none_wlr_substitutes_zero",
            cand(10, row={"win_rate": 0.2}, agg={"win_loss_ratio": None}),
        ),
        ("wr_at_gate_passes", cand(11, row={"win_rate": 0.25}, agg={"win_loss_ratio": 0.1})),
        ("streak_at_gate", cand(12, agg={"max_consecutive_losses": 10})),
        ("streak_above_gate", cand(13, agg={"max_consecutive_losses": 11})),
    ]


# ──────────────────────────── preset resolution ────────────────────────────


def preset_resolution_cases() -> list[tuple[str, str]]:
    """``(name, token)`` pairs for ``resolve_preset`` (unknown degrades, logged)."""
    return [
        ("known_min_dd_first", "min_dd_first"),
        ("known_composite", "composite"),
        ("known_profit_factor", "profit_factor"),
        ("known_win_rate", "win_rate"),
        ("known_calmar", "calmar"),
        ("unknown_junk", "junk"),
        ("unknown_blank", ""),
        ("unknown_case_sensitive", "MIN_DD_FIRST"),
    ]


# ──────────────────────────── ranking (preset order + ties) ─────────────────


def _preset_pool() -> list[Top100Candidate]:
    """FOUR gate-passing candidates whose raw values discriminate EVERY preset.

    * min_dd_first: c2 (dd 5.0, composite 1.0) → c3 (dd 5.0, composite None —
      unscored ranks AFTER scored) → c1 (10.0) → c4 (20.0);
    * composite: c4 (3.0) → c1 (2.0) → c2 (1.0) → c3 (None LAST);
    * profit_factor: c1 == c3 at 2.0 → run_id-ASC tie → c1, c3, c2, c4;
    * win_rate: c4 → c2 → c1 → c3;  * calmar: c2 → c3 → c1 → c4.
    """
    return [
        make_candidate(
            1,
            fingerprint=fingerprint_seed(101),
            row_overrides={
                "max_drawdown_pct": 10.0,
                "profit_factor": 2.0,
                "win_rate": 0.5,
                "calmar_ratio": 1.0,
            },
            agg_overrides={"composite_score": 2.0, "max_consecutive_losses": 3},
        ),
        make_candidate(
            2,
            fingerprint=fingerprint_seed(102),
            row_overrides={
                "max_drawdown_pct": 5.0,
                "profit_factor": 1.5,
                "win_rate": 0.7,
                "calmar_ratio": 3.0,
            },
            agg_overrides={"composite_score": 1.0, "max_consecutive_losses": 2},
        ),
        make_candidate(
            3,
            fingerprint=fingerprint_seed(103),
            row_overrides={
                "max_drawdown_pct": 5.0,
                "profit_factor": 2.0,
                "win_rate": 0.3,
                "calmar_ratio": 2.0,
            },
            agg_overrides={"composite_score": None, "max_consecutive_losses": 1},
        ),
        make_candidate(
            4,
            fingerprint=fingerprint_seed(104),
            row_overrides={
                "max_drawdown_pct": 20.0,
                "profit_factor": 1.21,
                "win_rate": 0.9,
                "calmar_ratio": 0.5,
            },
            agg_overrides={"composite_score": 3.0, "max_consecutive_losses": 9},
        ),
    ]


def _tie_ladder_pool() -> list[Top100Candidate]:
    """The full min_dd_first tie LADDER: dd tie → composite desc → losses asc
    → run_id ASC, plus the ``-0.0`` vs ``0.0`` composite pun (equal under
    ``<`` — the tie falls through to run_id)."""
    shared_row = {"max_drawdown_pct": 7.5}
    return [
        # a/b: dd tie, composite 2.0 beats 1.0.
        make_candidate(
            21,
            fingerprint=fingerprint_seed(21),
            row_overrides=shared_row,
            agg_overrides={"composite_score": 1.0},
        ),
        make_candidate(
            22,
            fingerprint=fingerprint_seed(22),
            row_overrides=shared_row,
            agg_overrides={"composite_score": 2.0},
        ),
        # c/d: dd + composite tie, losses 1 beats 4.
        make_candidate(
            23,
            fingerprint=fingerprint_seed(23),
            row_overrides=shared_row,
            agg_overrides={"composite_score": 3.0, "max_consecutive_losses": 4},
        ),
        make_candidate(
            24,
            fingerprint=fingerprint_seed(24),
            row_overrides=shared_row,
            agg_overrides={"composite_score": 3.0, "max_consecutive_losses": 1},
        ),
        # e/f: FULL tie (dd, composite, losses) → run_id ASC (25 before 26).
        make_candidate(
            26,
            fingerprint=fingerprint_seed(26),
            row_overrides=shared_row,
            agg_overrides={"composite_score": 4.0, "max_consecutive_losses": 2},
        ),
        make_candidate(
            25,
            fingerprint=fingerprint_seed(25),
            row_overrides=shared_row,
            agg_overrides={"composite_score": 4.0, "max_consecutive_losses": 2},
        ),
        # g/h: -0.0 vs 0.0 composite compare EQUAL → run_id ASC (27 before 28).
        make_candidate(
            28,
            fingerprint=fingerprint_seed(28),
            row_overrides=shared_row,
            agg_overrides={"composite_score": 0.0, "max_consecutive_losses": 2},
        ),
        make_candidate(
            27,
            fingerprint=fingerprint_seed(27),
            row_overrides=shared_row,
            agg_overrides={"composite_score": -0.0, "max_consecutive_losses": 2},
        ),
    ]


def rank_cases() -> list[tuple[str, str, list[Top100Candidate]]]:
    """``(name, preset, pool)`` triples for ``rank_candidates`` order capture."""
    pool = _preset_pool()
    return [
        ("min_dd_first", "min_dd_first", pool),
        ("composite", "composite", pool),
        ("profit_factor_tie_run_id", "profit_factor", pool),
        ("win_rate", "win_rate", pool),
        ("calmar", "calmar", pool),
        ("unknown_degrades_to_default", "junk", pool),
        ("min_dd_tie_ladder", "min_dd_first", _tie_ladder_pool()),
    ]


# ──────────────────────────── build cases ────────────────────────────


def _twin_pool() -> tuple[list[Top100Candidate], TwinFamilies]:
    """Five pooled winners: one cross-bucket twin pair, two singletons, one
    gate-failer — the family collapse + honesty-header shape."""
    twin_simple = make_candidate(
        31,
        fingerprint=fingerprint_seed(707),
        row_overrides={"mask_bits": 0b1, "max_drawdown_pct": 9.0},
        tuple_ref="NIFTY/1minute/bull_k1_cccccccccccccccc",
    )
    twin_complex = make_candidate(
        32,
        fingerprint=fingerprint_seed(707),
        row_overrides={"mask_bits": 0b1011, "tf": "5minute", "max_drawdown_pct": 9.0},
        tuple_ref="NIFTY/5minute/bull_k3_dddddddddddddddd",
        sl_paisa=None,
        tp_paisa=None,
    )
    calm = make_candidate(
        33,
        fingerprint=fingerprint_seed(303),
        row_overrides={"max_drawdown_pct": 3.5, "mask_bits": 0b110000},
        agg_overrides={"composite_score": None},
        tuple_ref="NIFTY/1minute/bull_k2_cccccccccccccccc",
    )
    deep = make_candidate(
        34,
        fingerprint=fingerprint_seed(304),
        row_overrides={"max_drawdown_pct": 21.0, "mask_bits": 0b101},
        tuple_ref="NIFTY/5minute/bull_k2_dddddddddddddddd",
    )
    failer = make_candidate(
        35,
        fingerprint=fingerprint_seed(305),
        row_overrides={"profit_factor": 1.05},
        tuple_ref="NIFTY/1minute/bull_k1_cccccccccccccccc",
    )
    pool = [twin_simple, twin_complex, calm, deep, failer]
    entry_sets = {
        twin_simple.row.run_id: frozenset({100, 200}),
        twin_complex.row.run_id: frozenset({100, 200}),
        calm.row.run_id: frozenset({300, 400}),
        deep.row.run_id: frozenset({500, 600}),
    }
    families = group_twins(
        [
            TwinCandidate(
                run_id=c.row.run_id,
                mask_bits=c.row.mask_bits,
                fingerprint=c.agg.fingerprint,
                entry_ts_us=entry_sets[c.row.run_id],
            )
            for c in pool
            if c is not failer
        ]
    )
    return pool, families


def _limit_pool() -> list[Top100Candidate]:
    """105 gate-passing singletons — 100 survive the cap, ties at the seam.

    ``dd = 1 + ((i * 37) % 100) / 4`` collides for i ∈ {101..105} vs {1..5}
    (the composite tiebreak decides), so the cap boundary itself sits on a
    tie ladder — the exact shape a Rust re-sort could silently flip.
    """
    return [
        make_candidate(
            1000 + i,
            fingerprint=fingerprint_seed(1000 + i),
            row_overrides={"max_drawdown_pct": 1.0 + ((i * 37) % 100) / 4.0},
            agg_overrides={"composite_score": float(i) / 8.0},
        )
        for i in range(1, 106)
    ]


def _reason_provider(mask_bits: int) -> Mapping[str, str] | None:
    """Deterministic WP3-shaped reasons; masks with bit 4 set get honest None."""
    if mask_bits & 0b10000:
        return None
    return {"technical": f"mask {mask_bits:#x} technical", "plain": f"mask {mask_bits:#x} plain"}


def _margin_roi_provider(candidate: Top100Candidate) -> Mapping[str, object] | None:
    """Deterministic R47 refusal-shaped payload keyed by the candidate row."""
    return {
        "status": "unverified",
        "roi_pct": None,
        "detail": f"margin history unverified for {candidate.row.instrument}",
    }


def build_cases() -> list[tuple[str, dict[str, Any]]]:
    """``(name, case)`` pairs for the build family.

    ``case`` carries ``kwargs`` (fed to ``build_top100`` verbatim on top of
    the candidates) plus ``candidates`` and ``full_json`` (False = the
    rendered bytes are frozen by sha256 + entry run_id order only — the
    105-candidate cap case would bloat the fixture).
    """

    def case(
        name: str,
        candidates: list[Top100Candidate],
        *,
        full_json: bool = True,
        **kwargs: Any,
    ) -> tuple[str, dict[str, Any]]:
        base: dict[str, Any] = {
            "run_id": _RUN,
            "preset": "min_dd_first",
            "families": _empty_families(),
            "counts": _counts(),
            "data_snapshot_hashes": ("snap-1",),
            "baselines": {"NIFTY": "baseline-NIFTY.json"},
        }
        base.update(kwargs)
        return name, {"candidates": candidates, "kwargs": base, "full_json": full_json}

    twin_pool, twin_families = _twin_pool()
    gated_rep_pool = [
        # The nominal representative (fewest mask bits) FAILS the gates; the
        # family still ships its simplest PASSING member.
        make_candidate(
            41,
            fingerprint=fingerprint_seed(41),
            row_overrides={"mask_bits": 0b1, "profit_factor": 1.0},
        ),
        make_candidate(42, fingerprint=fingerprint_seed(42), row_overrides={"mask_bits": 0b111111}),
        make_candidate(43, fingerprint=fingerprint_seed(43), row_overrides={"mask_bits": 0b11}),
    ]
    gated_rep_families = TwinFamilies(
        family_by_run_id={
            run_id_of(41): run_id_of(41),
            run_id_of(42): run_id_of(41),
            run_id_of(43): run_id_of(41),
        },
        members_by_family={run_id_of(41): (run_id_of(41), run_id_of(42), run_id_of(43))},
    )
    custom_family_pool = [
        make_candidate(51, fingerprint=fingerprint_seed(51), row_overrides={"mask_bits": 0b111}),
        make_candidate(52, fingerprint=fingerprint_seed(52), row_overrides={"mask_bits": 0b1}),
    ]
    # A family_id that is NO member's run_id and is ABSENT from
    # members_by_family — the sorted-members fallback + the all-members-rank-1
    # chooser arm (bit_count then run_id decides).
    custom_families = TwinFamilies(
        family_by_run_id={run_id_of(51): "zz-custom", run_id_of(52): "zz-custom"},
        members_by_family={},
    )

    joined_pool = [
        make_candidate(
            61, fingerprint=fingerprint_seed(61), row_overrides={"max_drawdown_pct": 4.0}
        ),
        make_candidate(
            62, fingerprint=fingerprint_seed(62), row_overrides={"max_drawdown_pct": 5.0}
        ),
        make_candidate(
            63, fingerprint=fingerprint_seed(63), row_overrides={"max_drawdown_pct": 6.0}
        ),
        make_candidate(
            64, fingerprint=fingerprint_seed(64), row_overrides={"max_drawdown_pct": 7.0}
        ),
        make_candidate(
            65, fingerprint=fingerprint_seed(65), row_overrides={"max_drawdown_pct": 8.0}
        ),
    ]
    joined_views = {
        # A pass with a NON-representable-in-decimal rate: the ×100.0 unit
        # projection must stay the EXACT float product (1/3 → 33.333333333333336).
        run_id_of(61): _view(run_id_of(61), cascade_composite=2.5, cpcv_pass_rate=1.0 / 3.0),
        # A provisional pass (pool-N DSR) with rate 1.0.
        run_id_of(62): _view(run_id_of(62), provisional=True, cpcv_pass_rate=1.0, survivor_rank=2),
        # A hard FAIL carrying the verbatim stage token + its measured rate.
        run_id_of(63): _view(
            run_id_of(63),
            passed=False,
            rejected_at="cpcv",
            detail="cpcv 0.2500 < 0.5",
            cascade_composite=None,
            cpcv_pass_rate=0.25,
            survivor_rank=None,
        ),
        # A pass with NO measured rate — cpcv_pass_pct key must be ABSENT.
        run_id_of(64): _view(run_id_of(64), cpcv_pass_rate=None, survivor_rank=3),
        # run_id_of(65) is deliberately NOT in the pool → not_in_cascade_pool.
    }

    ambiguous_pool = [
        make_candidate(71, fingerprint=fingerprint_seed(71)),
        make_candidate(
            72, fingerprint=fingerprint_seed(72), row_overrides={"max_drawdown_pct": 12.0}
        ),
    ]
    ambiguous_join = _join(
        {run_id_of(72): _view(run_id_of(72))}, ambiguous=frozenset({run_id_of(71)})
    )

    unicode_pool = [
        make_candidate(
            81,
            fingerprint=fingerprint_seed(81),
            row_overrides={"instrument": "NSE-NIFTY-™π", "direction": "bull-β"},
            tuple_ref="NSE-NIFTY-™π/1minute/bull_k1_π",
        )
    ]
    surrogate_pool = [
        make_candidate(
            82,
            fingerprint=fingerprint_seed(82),
            row_overrides={"instrument": "BAD-\ud800-SURROGATE"},
        )
    ]
    huge_int_pool = [
        make_candidate(
            83,
            fingerprint=fingerprint_seed(83),
            row_overrides={
                "mask_bits": _MAX_MASK,
                "net_pnl_paisa": 2**62,
                "gross_profit_paisa": 2**62 + 1,
                "expectancy_paisa": -(2**61),
            },
            agg_overrides={"mae_worst_paisa": 2**53 + 1},
        )
    ]
    vix_partial_pool = [
        make_candidate(
            84,
            fingerprint=fingerprint_seed(84),
            agg_overrides={"vix_win_min": None},  # partial trio → win block None
        )
    ]
    worst5_string_pool = [
        make_candidate(
            85,
            fingerprint=fingerprint_seed(85),
            agg_overrides={"worst5_days_json": '"free-form"'},  # any JSON — no object law
        )
    ]

    return [
        case("empty_pool", []),
        case(
            "basic_two",
            [
                make_candidate(36, fingerprint=fingerprint_seed(36)),
                make_candidate(
                    37,
                    fingerprint=fingerprint_seed(37),
                    row_overrides={"max_drawdown_pct": 12.0, "sharpe_ratio": 0.9},
                ),
            ],
            data_snapshot_hashes=("snap-b", "snap-a", "snap-b"),
            counts=_counts(stores=2, excluded_no_aggs=1, zero_trade_excluded=2, parity_excluded=3),
        ),
        case("twin_collapse", twin_pool, families=twin_families, counts=_counts(stores=2)),
        case("gated_representative_fallback", gated_rep_pool, families=gated_rep_families),
        case("custom_family_id_fallback", custom_family_pool, families=custom_families),
        case("unknown_preset_degrades", _preset_pool(), preset="bogus-token"),
        case("preset_composite_build", _preset_pool(), preset="composite"),
        case(
            "providers_wired",
            [
                make_candidate(
                    38, fingerprint=fingerprint_seed(38), row_overrides={"mask_bits": 0b111}
                ),
                make_candidate(
                    39,
                    fingerprint=fingerprint_seed(39),
                    row_overrides={"mask_bits": 0b10000, "max_drawdown_pct": 11.0},
                ),
            ],
            reason_provider=_reason_provider,
            margin_roi_provider=_margin_roi_provider,
        ),
        case("cascade_joined_states", joined_pool, cascade_join=_join(joined_views)),
        case("cascade_ambiguous_id", ambiguous_pool, cascade_join=ambiguous_join),
        case(
            "cascade_sweep_id_mismatch",
            [make_candidate(73, fingerprint=fingerprint_seed(73))],
            cascade_join=_join({run_id_of(73): _view(run_id_of(73))}, sweep_id="0d" * 32),
        ),
        case("unicode_fields", unicode_pool, baselines={"NSE-NIFTY-™π": "baseline-™.json"}),
        case("lone_surrogate_instrument", surrogate_pool),
        case("huge_ints_max_mask", huge_int_pool),
        case("vix_partial_trio_none", vix_partial_pool),
        case("worst5_nonlist_json_passthrough", worst5_string_pool),
        case("limit_cap_105_to_100", _limit_pool(), full_json=False),
        # ── refusal / render-quirk arms (the frozen exception laws) ──
        case(
            "duplicate_run_id_refused",
            [
                make_candidate(91, fingerprint=fingerprint_seed(91)),
                make_candidate(91, fingerprint=fingerprint_seed(92)),
            ],
        ),
        case(
            "nan_composite_render_refused",
            [
                make_candidate(
                    93,
                    fingerprint=fingerprint_seed(93),
                    agg_overrides={"composite_score": float("nan")},
                )
            ],
        ),
        case(
            "inf_recovery_render_refused",
            [
                make_candidate(
                    94,
                    fingerprint=fingerprint_seed(94),
                    agg_overrides={"recovery_factor": float("inf")},
                )
            ],
        ),
        case(
            "agg_regime_json_array_refused",
            [
                make_candidate(
                    95,
                    fingerprint=fingerprint_seed(95),
                    agg_overrides={"pnl_by_year_json": "[1,2]"},
                )
            ],
        ),
        case(
            "agg_regime_json_invalid_refused",
            [
                make_candidate(
                    96,
                    fingerprint=fingerprint_seed(96),
                    agg_overrides={"pnl_by_trend_json": "{not json"},
                )
            ],
        ),
    ]


#: The build-family WARN events worth freezing (field subset, order preserved).
_BUILD_WARN_EVENTS = ("top100.unknown_preset", "top100.cascade_sweep_id_mismatch")
_BUILD_WARN_FIELDS = ("requested", "resolved", "run_id", "report_sweep_id")


def run_build_case(case: dict[str, Any]) -> dict[str, Any]:
    """Execute ONE build case and project its outcome to a JSON-able dict.

    Shared verbatim by the generator and the replay golden — the outcome
    projection IS the parity contract: build error (type + str) → render
    error (type + str) → rendered bytes (full json or sha256 + order).
    """
    with capture_logs() as logs:
        try:
            artifact = build_top100(case["candidates"], **case["kwargs"])
        except Exception as exc:  # freeze the VERBATIM refusal law
            # Every frozen refusal is an ordinary Exception (Top100Error /
            # ValueError); a BaseException at replay (pyo3 panic,
            # KeyboardInterrupt) must propagate and fail the test loudly.
            return {"error_type": type(exc).__name__, "error_str": str(exc)}
        try:
            rendered = artifact.rendered_bytes()
        except Exception as exc:  # the allow_nan=False render refusal law
            # The frozen render refusal is ValueError (allow_nan=False); a
            # BaseException at replay must propagate, never be captured.
            return {
                "built": True,
                "n_entries": len(artifact.entries),
                "render_error_type": type(exc).__name__,
                "render_error_str": str(exc),
            }
    warns = [
        {"event": e["event"]} | {k: e[k] for k in _BUILD_WARN_FIELDS if k in e}
        for e in logs
        if e.get("event") in _BUILD_WARN_EVENTS
    ]
    out: dict[str, Any] = {
        "sha256": hashlib.sha256(rendered).hexdigest(),
        "n_entries": len(artifact.entries),
        "warns": warns,
    }
    if case["full_json"]:
        out["json"] = rendered.decode("utf-8")
    else:
        out["entry_run_ids"] = [entry["run_id"] for entry in artifact.entries]
    return out


# ──────────────────────────── constructor refusals ───────────────────────────


def constructor_cases() -> list[tuple[str, Callable[[], object]]]:
    """``(name, thunk)`` pairs whose construction refusals are frozen laws."""

    def mismatched_candidate() -> object:
        good = make_candidate(1, fingerprint=fingerprint_seed(1))
        other = make_candidate(2, fingerprint=fingerprint_seed(2))
        return Top100Candidate(
            row=good.row, agg=other.agg, sl_paisa=80, tp_paisa=50, tuple_ref="t/x/y"
        )

    def empty_tuple_ref() -> object:
        good = make_candidate(1, fingerprint=fingerprint_seed(1))
        return Top100Candidate(row=good.row, agg=good.agg, sl_paisa=80, tp_paisa=50, tuple_ref="")

    return [
        ("candidate_run_id_mismatch", mismatched_candidate),
        ("candidate_empty_tuple_ref", empty_tuple_ref),
        ("counts_negative_stores", lambda: _counts(stores=-1)),
        ("counts_negative_no_aggs", lambda: _counts(excluded_no_aggs=-1)),
        ("counts_negative_zero_trade", lambda: _counts(zero_trade_excluded=-2)),
        ("counts_negative_parity", lambda: _counts(parity_excluded=-3)),
    ]


# ──────────────────────────── write round-trip ───────────────────────────────


def _small_artifact() -> Top100Artifact:
    return build_top100(
        [make_candidate(1, fingerprint=fingerprint_seed(1))],
        run_id=_RUN,
        preset="min_dd_first",
        families=_empty_families(),
        counts=_counts(),
        data_snapshot_hashes=("snap-1",),
        baselines={"NIFTY": "baseline-NIFTY.json"},
    )


def _cascaded_artifact() -> Top100Artifact:
    return build_top100(
        [make_candidate(2, fingerprint=fingerprint_seed(2))],
        run_id=_RUN,
        preset="composite",
        families=_empty_families(),
        counts=_counts(),
        data_snapshot_hashes=("snap-2",),
        baselines={},
        cascade_join=_join({run_id_of(2): _view(run_id_of(2))}),
    )


def write_roundtrip_cases() -> list[tuple[str, Callable[[], Top100Artifact]]]:
    """``(name, artifact_builder)`` pairs for the write→load behavioral pins."""
    return [
        ("small_no_cascade", _small_artifact),
        ("small_with_cascade_join", _cascaded_artifact),
    ]


def run_write_roundtrip(build: Callable[[], Top100Artifact]) -> dict[str, Any]:
    """Write the artifact to a throwaway dir + project the behavioral pins.

    Pins: the file bytes ARE ``rendered_bytes()``; the stamp carries exactly
    the 4 stage-stamp keys with stage/run_id/content-hash correct and a
    STRING produced_at (never frozen — wall clock); the dir holds exactly the
    two files with zero ``.brutex-write-*`` temp residue (the atomic-write
    law); ``load_top100`` round-trips to the identical rendered bytes with
    the cascade_join meta preserved.
    """
    artifact = build()
    rendered = artifact.rendered_bytes()
    with tempfile.TemporaryDirectory() as tmp:
        runs_dir = Path(tmp)
        path = write_top100(runs_dir, artifact)
        target_dir = runs_dir / artifact.run_id
        stamp = json.loads((target_dir / TOP100_STAMP_FILENAME).read_text(encoding="utf-8"))
        reloaded = load_top100(runs_dir, artifact.run_id)
        assert reloaded is not None  # a failed round-trip is a broken corpus
        return {
            "artifact_filename": path.name,
            "dir_files": sorted(p.name for p in target_dir.iterdir()),
            "file_equals_rendered": path.read_bytes() == rendered,
            "rendered_sha256": hashlib.sha256(rendered).hexdigest(),
            "stamp_keys": sorted(stamp),
            "stamp_stage": stamp["stage"],
            "stamp_run_id_matches": stamp["run_id"] == artifact.run_id,
            "content_hash_matches": stamp["content_hash"] == hashlib.sha256(rendered).hexdigest(),
            "produced_at_is_str": isinstance(stamp["produced_at_utc"], str),
            "reload_equals_original": reloaded.rendered_bytes() == rendered,
            "reload_cascade_join_preserved": reloaded.cascade_join == artifact.cascade_join,
        }


# ──────────────────────────── load scenarios ─────────────────────────────────


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _write_pair(
    runs_dir: Path,
    run_id: str,
    raw: bytes,
    *,
    stamp_overrides: dict[str, Any] | None = None,
    drop_stamp_key: str | None = None,
    extra_stamp_key: bool = False,
) -> None:
    """Write an artifact + a (possibly corrupted) matching stamp by hand."""
    target = runs_dir / run_id
    target.mkdir(parents=True, exist_ok=True)
    (target / TOP100_FILENAME).write_bytes(raw)
    stamp: dict[str, Any] = {
        "stage": "top100",
        "run_id": run_id,
        "content_hash": _sha(raw),
        "produced_at_utc": _FIXED_PRODUCED_AT,
    }
    if stamp_overrides:
        stamp.update(stamp_overrides)
    if drop_stamp_key is not None:
        del stamp[drop_stamp_key]
    if extra_stamp_key:
        stamp["extra"] = 1
    (target / TOP100_STAMP_FILENAME).write_text(_dumps(stamp) + "\n", encoding="utf-8")


def _mutated_payload_bytes(edit: Callable[[dict[str, Any]], None]) -> bytes:
    """The small artifact's payload, mutated + re-rendered via the module law."""
    payload = _small_artifact().to_payload()
    edit(payload)
    return (_dumps(payload) + "\n").encode("utf-8")


def load_scenarios() -> list[tuple[str, Callable[[Path], str]]]:
    """``(name, setup)`` pairs; ``setup(runs_dir) -> run_id`` plants the files.

    Every damage class of the validating reader, each expected to log ONE
    named ``top100.load_invalid`` reason and return ``None`` — plus the two
    bool==int PUNS (an entry ``rank: true`` at position 1 legally LOADS —
    ``True == 1``, the frozen law; ``schema_version: true`` legally LOADED
    under the schema-1 law and is the version-gate MISMATCH pin since the
    DEC-RESILIENCE-STREAKS-001 1→2 bump) and the additive cascade_join
    tolerance arms.
    """
    ok_raw = _small_artifact().rendered_bytes()

    def ok(runs_dir: Path) -> str:
        _write_pair(runs_dir, _RUN, ok_raw)
        return _RUN

    def stamp_missing(runs_dir: Path) -> str:
        _write_pair(runs_dir, _RUN, ok_raw)
        (runs_dir / _RUN / TOP100_STAMP_FILENAME).unlink()
        return _RUN

    def stamp_not_json(runs_dir: Path) -> str:
        _write_pair(runs_dir, _RUN, ok_raw)
        (runs_dir / _RUN / TOP100_STAMP_FILENAME).write_bytes(b"{not json")
        return _RUN

    def stamp_extra_key(runs_dir: Path) -> str:
        _write_pair(runs_dir, _RUN, ok_raw, extra_stamp_key=True)
        return _RUN

    def stamp_missing_key(runs_dir: Path) -> str:
        _write_pair(runs_dir, _RUN, ok_raw, drop_stamp_key="content_hash")
        return _RUN

    def stamp_wrong_stage(runs_dir: Path) -> str:
        _write_pair(runs_dir, _RUN, ok_raw, stamp_overrides={"stage": "stage3"})
        return _RUN

    def stamp_wrong_run_id(runs_dir: Path) -> str:
        _write_pair(runs_dir, _RUN, ok_raw, stamp_overrides={"run_id": "0e" * 32})
        return _RUN

    def stamp_short_hash(runs_dir: Path) -> str:
        _write_pair(runs_dir, _RUN, ok_raw, stamp_overrides={"content_hash": "abc"})
        return _RUN

    def stamp_nonstr_hash(runs_dir: Path) -> str:
        _write_pair(runs_dir, _RUN, ok_raw, stamp_overrides={"content_hash": 7})
        return _RUN

    def stamp_produced_at_int(runs_dir: Path) -> str:
        _write_pair(runs_dir, _RUN, ok_raw, stamp_overrides={"produced_at_utc": 0})
        return _RUN

    def artifact_missing(runs_dir: Path) -> str:
        _write_pair(runs_dir, _RUN, ok_raw)
        (runs_dir / _RUN / TOP100_FILENAME).unlink()
        return _RUN

    def artifact_tampered(runs_dir: Path) -> str:
        _write_pair(runs_dir, _RUN, ok_raw)
        (runs_dir / _RUN / TOP100_FILENAME).write_bytes(ok_raw[: len(ok_raw) // 2])
        return _RUN

    def artifact_not_json(runs_dir: Path) -> str:
        _write_pair(runs_dir, _RUN, b"{not json")  # hash MATCHES the junk
        return _RUN

    def artifact_json_array(runs_dir: Path) -> str:
        _write_pair(runs_dir, _RUN, b"[1,2,3]")
        return _RUN

    def schema_version_future(runs_dir: Path) -> str:
        # Was the literal 2 (a FUTURE version under the schema-1 law this
        # corpus first froze); DEC-RESILIENCE-STREAKS-001's schema bump (1→2,
        # merged from main at the 2026-07-19 merge-forward) made 2 CURRENT, so
        # the future-version mismatch arm now derives off the live constant.
        def edit(p: dict[str, Any]) -> None:
            p["schema_version"] = TOP100_SCHEMA_VERSION + 1

        _write_pair(runs_dir, _RUN, _mutated_payload_bytes(edit))
        return _RUN

    def schema_version_true_bool_pun(runs_dir: Path) -> str:
        def edit(p: dict[str, Any]) -> None:
            # True == 1 PASSED the version gate under the schema-1 law this
            # corpus first froze; since the DEC-RESILIENCE-STREAKS-001 1→2 bump
            # (merged from main) True != 2, so the pun is now the version-gate
            # MISMATCH pin (the rank: true entry pun still legally loads).
            p["schema_version"] = True

        _write_pair(runs_dir, _RUN, _mutated_payload_bytes(edit))
        return _RUN

    def artifact_wrong_run_id(runs_dir: Path) -> str:
        def edit(p: dict[str, Any]) -> None:
            p["run_id"] = "0e" * 32

        _write_pair(runs_dir, _RUN, _mutated_payload_bytes(edit))
        return _RUN

    def preset_empty(runs_dir: Path) -> str:
        def edit(p: dict[str, Any]) -> None:
            p["preset"] = ""

        _write_pair(runs_dir, _RUN, _mutated_payload_bytes(edit))
        return _RUN

    def preset_int(runs_dir: Path) -> str:
        def edit(p: dict[str, Any]) -> None:
            p["preset"] = 7

        _write_pair(runs_dir, _RUN, _mutated_payload_bytes(edit))
        return _RUN

    def source_list(runs_dir: Path) -> str:
        def edit(p: dict[str, Any]) -> None:
            p["source"] = [1]

        _write_pair(runs_dir, _RUN, _mutated_payload_bytes(edit))
        return _RUN

    def baselines_int_value(runs_dir: Path) -> str:
        def edit(p: dict[str, Any]) -> None:
            p["baselines"] = {"NIFTY": 7}

        _write_pair(runs_dir, _RUN, _mutated_payload_bytes(edit))
        return _RUN

    def entries_dict(runs_dir: Path) -> str:
        def edit(p: dict[str, Any]) -> None:
            p["entries"] = {}

        _write_pair(runs_dir, _RUN, _mutated_payload_bytes(edit))
        return _RUN

    def entry_missing_key(runs_dir: Path) -> str:
        def edit(p: dict[str, Any]) -> None:
            del p["entries"][0]["family_id"]

        _write_pair(runs_dir, _RUN, _mutated_payload_bytes(edit))
        return _RUN

    def entry_not_dict(runs_dir: Path) -> str:
        def edit(p: dict[str, Any]) -> None:
            p["entries"] = ["junk"]

        _write_pair(runs_dir, _RUN, _mutated_payload_bytes(edit))
        return _RUN

    def entry_rank_zero_based(runs_dir: Path) -> str:
        def edit(p: dict[str, Any]) -> None:
            p["entries"][0]["rank"] = 0

        _write_pair(runs_dir, _RUN, _mutated_payload_bytes(edit))
        return _RUN

    def entry_rank_true_bool_pun(runs_dir: Path) -> str:
        def edit(p: dict[str, Any]) -> None:
            p["entries"][0]["rank"] = True  # True == 1 → PASSES the rank law

        _write_pair(runs_dir, _RUN, _mutated_payload_bytes(edit))
        return _RUN

    def cascade_join_string_dropped(runs_dir: Path) -> str:
        def edit(p: dict[str, Any]) -> None:
            p["cascade_join"] = "junk"  # non-object → dropped, never a reject

        _write_pair(runs_dir, _RUN, _mutated_payload_bytes(edit))
        return _RUN

    def cascade_join_absent_pre_change(runs_dir: Path) -> str:
        def edit(p: dict[str, Any]) -> None:
            del p["cascade_join"]

        _write_pair(runs_dir, _RUN, _mutated_payload_bytes(edit))
        return _RUN

    return [
        ("ok_roundtrip", ok),
        ("stamp_missing", stamp_missing),
        ("stamp_not_json", stamp_not_json),
        ("stamp_extra_key", stamp_extra_key),
        ("stamp_missing_key", stamp_missing_key),
        ("stamp_wrong_stage", stamp_wrong_stage),
        ("stamp_wrong_run_id", stamp_wrong_run_id),
        ("stamp_short_hash", stamp_short_hash),
        ("stamp_nonstr_hash", stamp_nonstr_hash),
        ("stamp_produced_at_int", stamp_produced_at_int),
        ("artifact_missing", artifact_missing),
        ("artifact_tampered", artifact_tampered),
        ("artifact_not_json", artifact_not_json),
        ("artifact_json_array", artifact_json_array),
        ("schema_version_future", schema_version_future),
        ("schema_version_true_bool_pun", schema_version_true_bool_pun),
        ("artifact_wrong_run_id", artifact_wrong_run_id),
        ("preset_empty", preset_empty),
        ("preset_int", preset_int),
        ("source_list", source_list),
        ("baselines_int_value", baselines_int_value),
        ("entries_dict", entries_dict),
        ("entry_missing_key", entry_missing_key),
        ("entry_not_dict", entry_not_dict),
        ("entry_rank_zero_based", entry_rank_zero_based),
        ("entry_rank_true_bool_pun", entry_rank_true_bool_pun),
        ("cascade_join_string_dropped", cascade_join_string_dropped),
        ("cascade_join_absent_pre_change", cascade_join_absent_pre_change),
    ]


def run_load_scenario(setup: Callable[[Path], str]) -> dict[str, Any]:
    """Plant one scenario in a throwaway dir + project the load outcome.

    Outcome: ``{"loaded": False, "reason": <the ONE top100.load_invalid
    reason>}`` or ``{"loaded": True, "reloaded_sha256": …, "cascade_join":
    …}`` — the reloaded artifact re-rendered through the module's OWN
    ``rendered_bytes`` (so a load that normalizes — e.g. drops a junk
    cascade_join — freezes its normalized bytes).
    """
    with tempfile.TemporaryDirectory() as tmp:
        runs_dir = Path(tmp)
        run_id = setup(runs_dir)
        with capture_logs() as logs:
            loaded = load_top100(runs_dir, run_id)
        reasons = [e["reason"] for e in logs if e.get("event") == "top100.load_invalid"]
        if loaded is None:
            assert len(reasons) == 1, reasons  # exactly ONE named rejection
            return {"loaded": False, "reason": reasons[0]}
        assert reasons == []
        return {
            "loaded": True,
            "reloaded_sha256": hashlib.sha256(loaded.rendered_bytes()).hexdigest(),
            "cascade_join": None if loaded.cascade_join is None else dict(loaded.cascade_join),
        }
