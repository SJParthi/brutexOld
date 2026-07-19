"""Deterministic input corpus for the cascade_wire serialisation/parse parity goldens.

DEC-RUST-CASCADE-RESULTS-001 phase B (slice 2a): the T14 lane converts the
``brutex.engine.cascade_wire`` serialisation + parse LAW family to the
``rust/brutex-cascade`` crate under the compressed greenfield cycle
(capture-first — this corpus is frozen against the UNTOUCHED Python module
BEFORE any conversion). This module is the ONE builder of the parity corpus
INPUTS — shared verbatim by

* the capture generator (``gen_cascade_rust_parity.py`` — run ONCE against the
  pre-conversion pure-Python implementations; fixture frozen), and
* the replay golden (drives the post-conversion Rust-backed API against the
  frozen fixture),

so the two sides can never drift on what was fed in. Inputs are built in CODE
(never parsed from the fixture) because several hostile cases carry values JSON
cannot round-trip faithfully (``float('nan')``, bool-vs-int puns, huge-int
strings, non-JSON raw bytes).

Every case is fully deterministic: fixed labels, fixed numbers, no RNG, no
wall-clock, no I/O. The parse-family payloads are minted by MUTATING the
current serializer's own output (``manifest_to_json`` → ``json.loads`` → edit →
re-dump with sorted keys) — safe because the manifest family independently pins
the serializer's bytes, so the mutation base is itself frozen.
"""

from __future__ import annotations

import json
from typing import Any

from brutex.engine.cascade_wire import (
    MANIFEST_VERSION,
    BarsContext,
    CascadeManifest,
    CascadeTupleSpec,
    CascadeWireReport,
    LeaderboardTrialsRecord,
    Stage3CellRecord,
    Stage3ManifestRecord,
    cascade_config_for_sweep,
    manifest_to_json,
)
from brutex.metrics.cascade import CandidateVerdict, CascadeReport, FilterStage
from brutex.strategies.types import FibAnchorKind, PriceSource
from brutex.vocabulary.bits import TOTAL_CONDITIONS

__all__ = [
    "finite_cases",
    "manifest_cases",
    "max_drawdown_cases",
    "parse_payload_cases",
    "per_bar_returns_cases",
    "wire_report_cases",
]

_SWEEP_ID = "a" * 64
#: Largest valid strategy mask (mirrors ``cascade_wire._MASK_BITS_MAX`` == (1<<60)-1).
_MAX_MASK = (1 << TOTAL_CONDITIONS) - 1
#: The ONE futures series alias ``roll_schedules`` may name (import-identity law).
_FUTURES_ALIAS = "NSE-NIFTY-FUT"


def _rid(seed: int) -> str:
    """Return a valid 64-char lowercase-hex run-identity from *seed*."""
    return f"{seed:064x}"


def _spec(**overrides: object) -> CascadeTupleSpec:
    """Build a valid one-winner :class:`CascadeTupleSpec`, overridable by keyword."""
    base: dict[str, object] = {
        "instrument": "NIFTY",
        "tf_label": "1minute",
        "tf_minutes": 1,
        "direction_label": "bull",
        "sl_paisa": None,
        "tp_paisa": None,
        "prior_n": 3,
        "proximity_bps": None,
        "start_equity_paisa": 10_000_000,
        "winners": ((_rid(1), 0),),
    }
    base.update(overrides)
    return CascadeTupleSpec(**base)  # type: ignore[arg-type]


def _stage3_cell(**overrides: object) -> Stage3CellRecord:
    """Build a valid :class:`Stage3CellRecord`, overridable by keyword."""
    base: dict[str, object] = {
        "instrument": "NIFTY",
        "tf_label": "1minute",
        "direction_label": "bull",
        "k": 1,
        "sl_paisa": 80,
        "tp_paisa": 50,
        "proximity_bps": None,
    }
    base.update(overrides)
    return Stage3CellRecord(**base)  # type: ignore[arg-type]


def _lbt(**overrides: object) -> LeaderboardTrialsRecord:
    """Build a valid :class:`LeaderboardTrialsRecord`, overridable by keyword."""
    base: dict[str, object] = {
        "instrument": "NIFTY",
        "tf_label": "1minute",
        "n_trials": 10,
        "n_finite": 8,
        "sum_sharpe": 1.5,
        "sum_sharpe_sq": 2.25,
        "complete": True,
    }
    base.update(overrides)
    return LeaderboardTrialsRecord(**base)  # type: ignore[arg-type]


def _bars_context() -> BarsContext:
    return BarsContext(exchange="NSE", segment="INDEX", start="2021-01-01", end="2021-02-01")


def _manifest(
    *,
    specs: tuple[CascadeTupleSpec, ...] | None = None,
    stage3: Stage3ManifestRecord | None = None,
    leaderboard_trials: tuple[LeaderboardTrialsRecord, ...] | None = None,
    roll_schedules: tuple[tuple[str, tuple[int, ...]], ...] | None = None,
    config_overrides: dict[str, object] | None = None,
) -> CascadeManifest:
    """Build a valid :class:`CascadeManifest` with optional additive blocks."""
    return CascadeManifest(
        version=MANIFEST_VERSION,
        sweep_id=_SWEEP_ID,
        cascade_config=cascade_config_for_sweep(_SWEEP_ID, overrides=config_overrides),
        bars_context=_bars_context(),
        specs=(_spec(),) if specs is None else specs,
        stage3=stage3,
        leaderboard_trials=leaderboard_trials,
        roll_schedules=roll_schedules,
    )


# ──────────────────────────── manifest round-trip ────────────────────────────


def _full_manifest() -> CascadeManifest:
    """The everything-populated manifest (stage3 + lbt + roll_schedules)."""
    return _manifest(
        specs=(
            _spec(),
            _spec(
                instrument=_FUTURES_ALIAS,
                tf_label="5minute",
                tf_minutes=5,
                direction_label="bear",
                sl_paisa=50,
                tp_paisa=100,
                proximity_bps=(10, 20, 30),
                winners=((_rid(2), 5), (_rid(3), 7)),
            ),
        ),
        stage3=Stage3ManifestRecord(
            enabled=True,
            train_snapshot_hash="b" * 64,
            refined_cells=(
                _stage3_cell(),  # rel_path None (the pre-provenance parse leg)
                _stage3_cell(
                    direction_label="bear",
                    k=3,
                    sl_paisa=85,
                    tp_paisa=60,
                    proximity_bps=(5, 10, 15),
                    rel_path="cells/nifty-1m-bear.parquet",
                ),
            ),
        ),
        leaderboard_trials=(
            _lbt(),
            _lbt(
                instrument="BANKNIFTY",
                n_trials=4,
                n_finite=0,
                sum_sharpe=0.0,
                sum_sharpe_sq=0.0,
                complete=False,
            ),
        ),
        roll_schedules=((_FUTURES_ALIAS, (739000, 739030, 739061)),),
    )


def manifest_cases() -> list[tuple[str, CascadeManifest]]:
    """``(name, manifest)`` pairs for the manifest_to_json byte + round-trip family."""
    return [
        ("minimal", _manifest()),
        ("full_stage3_lbt_roll", _full_manifest()),
        (
            "non_default_sources",
            _manifest(
                specs=(
                    _spec(
                        fib_anchor=FibAnchorKind.ROLLING_WEEKLY,
                        vwap_source=PriceSource.OPEN,
                        ema_source=PriceSource.HLC3,
                    ),
                ),
                stage3=Stage3ManifestRecord(
                    enabled=True,
                    train_snapshot_hash="c" * 64,
                    refined_cells=(
                        _stage3_cell(
                            fib_anchor=FibAnchorKind.WEEKLY_STATIC,
                            vwap_source=PriceSource.CLOSE,
                            ema_source=PriceSource.OPEN,
                        ),
                    ),
                ),
            ),
        ),
        (
            "unicode_labels",
            _manifest(specs=(_spec(instrument="NSE-NIFTY-™", tf_label="5min-π", tf_minutes=5),)),
        ),
        (
            "sl_tp_one_sided",
            _manifest(specs=(_spec(sl_paisa=45, tp_paisa=None), _spec(sl_paisa=None, tp_paisa=90))),
        ),
        ("empty_specs", _manifest(specs=())),
        (
            "winners_mask_extremes",
            _manifest(specs=(_spec(winners=((_rid(1), 0), (_rid(2), _MAX_MASK))),)),
        ),
        (
            "config_float_overrides",
            _manifest(
                config_overrides={
                    "min_profit_factor": 0.1,
                    "spa_alpha": -0.25,
                    "dsr_min": 1e-9,
                }
            ),
        ),
    ]


# ──────────────────────────── wire report ────────────────────────────


def _verdict(candidate_id: str, **overrides: object) -> CandidateVerdict:
    """Build a passing :class:`CandidateVerdict`, overridable by keyword."""
    base: dict[str, object] = {
        "candidate_id": candidate_id,
        "passed": True,
        "rejected_at": None,
        "detail": "ok",
        "composite_score": None,
        "provisional": False,
        "cpcv_pass_rate": None,
    }
    base.update(overrides)
    return CandidateVerdict(**base)  # type: ignore[arg-type]


def _report(**overrides: object) -> CascadeReport:
    """Build an empty :class:`CascadeReport`, overridable by keyword."""
    base: dict[str, object] = {
        "verdicts": (),
        "survivor_ids": (),
        "n_input": 0,
        "n_survived": 0,
        "stage_survivors": {},
    }
    base.update(overrides)
    return CascadeReport(**base)  # type: ignore[arg-type]


def wire_report_cases() -> list[tuple[str, CascadeWireReport]]:
    """``(name, report)`` pairs for the serialize_wire_report byte + dict family."""
    stage_variants = _report(
        verdicts=(
            _verdict("cand-pass", composite_score=1.25, cpcv_pass_rate=0.5),
            _verdict(
                "cand-hard",
                passed=False,
                rejected_at=FilterStage.HARD_GATES,
                detail="total_trades 3 < 30",
            ),
            _verdict(
                "cand-cpcv",
                passed=False,
                rejected_at=FilterStage.CPCV,
                detail="cpcv 0.2500 < 0.5",
                cpcv_pass_rate=0.25,
            ),
            _verdict("cand-prov", provisional=True, composite_score=0.75, cpcv_pass_rate=1.0),
        ),
        survivor_ids=("cand-pass", "cand-prov"),
        n_input=4,
        n_survived=2,
        stage_survivors={"hard_gates": 3, "deflated_sharpe": 3, "cpcv": 2},
        pool_pbo=0.25,
        dsr_n_source="leaderboard",
        dsr_n_trials=42,
        dsr_trial_variance=2.5,
    )
    composite_specials = _report(
        verdicts=(
            _verdict("cs-none"),
            _verdict("cs-plain", composite_score=1.5),
            _verdict("cs-negzero", composite_score=-0.0),
            _verdict("cs-big", composite_score=1e16),
        ),
        survivor_ids=("cs-none", "cs-plain", "cs-negzero", "cs-big"),
        n_input=4,
        n_survived=4,
        stage_survivors={"hard_gates": 4},
    )
    unicode_report = _report(
        verdicts=(
            _verdict(
                "uni-™",
                passed=False,
                rejected_at=FilterStage.MONTE_CARLO,
                detail="π-детали ™",
            ),
        ),
        n_input=1,
        stage_survivors={"monte_carlo": 0},
        pool_pbo=None,
        dsr_trial_variance=None,
    )
    return [
        ("empty_groups", CascadeWireReport(sweep_id=_SWEEP_ID, groups=())),
        (
            "single_empty_report",
            CascadeWireReport(sweep_id=_SWEEP_ID, groups=(("NIFTY", "1minute", _report()),)),
        ),
        (
            "multi_group",
            CascadeWireReport(
                sweep_id=_SWEEP_ID,
                groups=(
                    ("BANKNIFTY", "5minute", stage_variants),
                    ("NIFTY", "1minute", _report(pool_pbo=0.5)),
                ),
            ),
        ),
        (
            "stage_variants",
            CascadeWireReport(sweep_id=_SWEEP_ID, groups=(("NIFTY", "1minute", stage_variants),)),
        ),
        (
            "composite_specials",
            CascadeWireReport(
                sweep_id=_SWEEP_ID, groups=(("NIFTY", "1minute", composite_specials),)
            ),
        ),
        (
            "unicode_detail",
            CascadeWireReport(sweep_id=_SWEEP_ID, groups=(("NIFTY", "3minute", unicode_report),)),
        ),
    ]


# ──────────────────────────── parse payloads ────────────────────────────


def _dump(payload: object) -> bytes:
    """Deterministic re-dump of a mutated payload dict (sorted keys, tight)."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _payload(manifest: CascadeManifest) -> dict[str, Any]:
    """The serializer's own payload dict for *manifest* (the mutation base)."""
    loaded = json.loads(manifest_to_json(manifest))
    assert isinstance(loaded, dict)
    return loaded


def parse_payload_cases() -> list[tuple[str, bytes]]:
    """``(name, payload_bytes)`` pairs fed to ``manifest_from_json`` verbatim.

    The capture records whatever the UNTOUCHED Python actually does — success
    (re-serialised bytes) or the raised exception's type + full ``str``. NB the
    expected outcomes include NON-CascadeWireError escapes (raw ``IndexError``
    for a 2-element proximity list, raw ``AttributeError`` for a non-dict spec
    entry) and the ``version: true`` bool==int PUN (parses OK) — those ARE the
    law being frozen.
    """
    cases: list[tuple[str, bytes]] = [
        ("raw_not_json", b"{not json"),
        ("raw_json_array_root", b"[1,2,3]"),
        ("valid_minimal", manifest_to_json(_manifest())),
        ("valid_full", manifest_to_json(_full_manifest())),
    ]

    minimal = _manifest()
    full = _full_manifest()

    def _mut(name: str, base: CascadeManifest, edit: Any) -> None:
        payload = _payload(base)
        edit(payload)
        cases.append((name, _dump(payload)))

    def _del_key(key: str) -> Any:
        def _edit(p: dict[str, Any]) -> None:
            del p[key]

        return _edit

    # Missing top-level keys.
    _mut("missing_bars_context", minimal, _del_key("bars_context"))
    _mut("missing_specs", minimal, _del_key("specs"))
    _mut("missing_sweep_id", minimal, _del_key("sweep_id"))
    _mut("missing_cascade_config", minimal, _del_key("cascade_config"))

    # Missing nested keys, one per family.
    def _spec_del_tf(p: dict[str, Any]) -> None:
        del p["specs"][0]["tf_minutes"]

    _mut("spec_missing_tf_minutes", minimal, _spec_del_tf)

    def _cell_del_sl(p: dict[str, Any]) -> None:
        del p["stage3"]["refined_cells"][0]["sl_paisa"]

    _mut("stage3_cell_missing_sl_paisa", full, _cell_del_sl)

    def _lbt_del_n(p: dict[str, Any]) -> None:
        del p["leaderboard_trials"][0]["n_trials"]

    _mut("lbt_missing_n_trials", full, _lbt_del_n)

    # DEC-FILL-WORST-CASE-ONLY-001 refusal family.
    def _fill_del(p: dict[str, Any]) -> None:
        del p["specs"][0]["fill_mode"]

    def _fill_set(token: object) -> Any:
        def _edit(p: dict[str, Any]) -> None:
            p["specs"][0]["fill_mode"] = token

        return _edit

    _mut("fill_mode_absent", minimal, _fill_del)
    _mut("fill_mode_precise", minimal, _fill_set("precise"))
    _mut("fill_mode_junk", minimal, _fill_set("junk"))

    # Version gate (NB: True == 1 in Python — the bool pun PARSES).
    def _version(value: object) -> Any:
        def _edit(p: dict[str, Any]) -> None:
            p["version"] = value

        return _edit

    _mut("version_99", minimal, _version(99))
    _mut("version_none", minimal, _version(None))
    _mut("version_str_1", minimal, _version("1"))
    _mut("version_true_bool_pun", minimal, _version(True))

    # Non-dict spec entries (raw AttributeError escapes — the frozen law).
    def _specs_set(value: object) -> Any:
        def _edit(p: dict[str, Any]) -> None:
            p["specs"] = value

        return _edit

    _mut("spec_entry_string", minimal, _specs_set(["junk"]))
    _mut("spec_entry_int", minimal, _specs_set([7]))

    # Winners junk.
    def _winners(value: object) -> Any:
        def _edit(p: dict[str, Any]) -> None:
            p["specs"][0]["winners"] = value

        return _edit

    _mut("winners_row_len1", minimal, _winners([[_rid(1)]]))
    _mut("winners_row_len3", minimal, _winners([[_rid(1), 3, 3]]))
    _mut("winners_row_int", minimal, _winners([7]))
    _mut("winners_row_str2", minimal, _winners(["ab"]))
    _mut("winners_run_id_int_abc", minimal, _winners([["run", "abc"]]))
    _mut("winners_huge_mask_string", minimal, _winners([[_rid(1), "9" * 40]]))

    # Proximity triple too short (raw IndexError escape — the frozen law).
    def _proximity_len2(p: dict[str, Any]) -> None:
        p["specs"][0]["proximity_bps"] = [1, 2]

    _mut("proximity_len2", minimal, _proximity_len2)

    # Stage-3 junk.
    def _stage3_cells_string(p: dict[str, Any]) -> None:
        p["stage3"] = {
            "enabled": True,
            "train_snapshot_hash": "b" * 64,
            "refined_cells": "junk",
        }

    _mut("stage3_cells_string", minimal, _stage3_cells_string)

    def _stage3_cell_sideways(p: dict[str, Any]) -> None:
        p["stage3"]["refined_cells"][0]["direction_label"] = "sideways"

    _mut("stage3_cell_direction_sideways", full, _stage3_cell_sideways)

    # Leaderboard-trials junk.
    def _lbt_invariant(p: dict[str, Any]) -> None:
        p["leaderboard_trials"][0]["n_trials"] = 5
        p["leaderboard_trials"][0]["n_finite"] = 6

    _mut("lbt_n_finite_exceeds_n_trials", full, _lbt_invariant)

    def _lbt_sum_str(p: dict[str, Any]) -> None:
        p["leaderboard_trials"][0]["sum_sharpe"] = "x"

    _mut("lbt_sum_sharpe_string", full, _lbt_sum_str)

    # roll_schedules junk.
    def _roll(value: object) -> Any:
        def _edit(p: dict[str, Any]) -> None:
            p["roll_schedules"] = value

        return _edit

    _mut("roll_ordinal_bool", minimal, _roll([[_FUTURES_ALIAS, [True]]]))
    _mut("roll_ordinal_float", minimal, _roll([[_FUTURES_ALIAS, [739000.9]]]))
    _mut("roll_ordinal_int_string", minimal, _roll([[_FUTURES_ALIAS, ["739000"]]]))
    _mut(
        "roll_duplicate_instrument",
        minimal,
        _roll([[_FUTURES_ALIAS, [1, 2]], [_FUTURES_ALIAS, [3, 4]]]),
    )
    _mut("roll_non_alias_instrument", minimal, _roll([["NSE-NIFTY", [1, 2]]]))
    _mut("roll_non_increasing", minimal, _roll([[_FUTURES_ALIAS, [5, 5]]]))
    _mut("roll_negative_ordinal", minimal, _roll([[_FUTURES_ALIAS, [-1, 3]]]))
    _mut("roll_empty_schedule", minimal, _roll([[_FUTURES_ALIAS, []]]))
    _mut("roll_row_int", minimal, _roll([7]))
    _mut("roll_row_len1", minimal, _roll([[_FUTURES_ALIAS]]))
    _mut("roll_row_len3", minimal, _roll([[_FUTURES_ALIAS, [1, 2], "extra"]]))

    # bars_context junk.
    def _bars_string(p: dict[str, Any]) -> None:
        p["bars_context"] = "junk"

    _mut("bars_context_string", minimal, _bars_string)

    def _bars_del_end(p: dict[str, Any]) -> None:
        del p["bars_context"]["end"]

    _mut("bars_context_missing_end", minimal, _bars_del_end)

    return cases


# ──────────────────────────── math trio ────────────────────────────


def finite_cases() -> list[tuple[str, float | None]]:
    """``(name, value)`` pairs for the ``_finite`` None/non-finite → 0.0 law."""
    return [
        ("none", None),
        ("nan", float("nan")),
        ("inf", float("inf")),
        ("neg_inf", float("-inf")),
        ("neg_zero", -0.0),
        ("plain", 1.5),
        ("big_finite", 1e308),
    ]


def per_bar_returns_cases() -> list[tuple[str, list[int], int]]:
    """``(name, equity_paisa, start_equity_paisa)`` for ``_per_bar_returns``."""
    return [
        ("empty", [], 1_000),
        ("single", [100], 1_000),
        ("mixed_signs", [100, -50, 200, 0], 1_000),
        ("big_paisa", [10**16, 10**16 + 1, 10**15], 10**12),
        ("flat", [500, 500, 500], 500),
    ]


def max_drawdown_cases() -> list[tuple[str, list[float]]]:
    """``(name, returns)`` for ``_max_drawdown_return_units``.

    ``nan_in_series`` freezes Python's ``max(a, b)`` first-arg-on-unordered
    semantics (a NaN never displaces the running peak — captured ``0.0``).
    """
    return [
        ("empty", []),
        ("monotonic_up", [0.1, 0.2, 0.3]),
        ("mixed", [0.1, -0.3, 0.2, -0.5]),
        ("neg_zero_sum", [0.0, -0.0, 0.0]),
        ("nan_in_series", [0.1, float("nan"), 0.2]),
        ("all_negative", [-0.1, -0.2]),
    ]
