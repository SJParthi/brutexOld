"""Apriori parity RE-DERIVED at the rescue floor 8 — byte-identical ON vs OFF.

DEC-STAGE1B-LOW-SUPPORT-RESCUE-001 (LANE W3): when the low-support rescue
lane is armed, the Apriori survival floor TRACKS the effective lower floor
(``effective_apriori_min_hits`` → the rescue floor, default 8) so rescued
candidates are never pre-pruned — the conscious, operator-ruled (2026-07-18
approve-all) supersession of the apriori-freeze-contract's fixed
``MIN_TRADES`` survival-law clause. The frozen ``sweep_apriori`` module is
BYTE-UNTOUCHED (its ``min_hits`` PARAMETER carries the value), so the
pruned ≡ unpruned kept-set equality must be — and here is — consciously
RE-DERIVED at floor 8: the SAME anti-monotone argument
(``hits(M') <= hits(M)`` for ``M ⊇ M'``) holds for ANY floor, and this
golden pins it empirically with the byte-identical persisted leaderboard at
``min_hits=8``, exactly the DEC-SWEEP-APRIORI-TUPLE-001 merge-gate shape.

The fixture is IMPORTED from the frozen parity suite (CLAUDE.md §11 COMMON
— one fixture, never a copy), which itself stays UNMODIFIED.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from brutex.data.option_dedup import OptionDedupIndex
from brutex.engine.combo_mask import count_combos
from brutex.engine.rescue_lane import RESCUE_MIN_HITS_DEFAULT, effective_apriori_min_hits
from brutex.engine.results_store import ResultsStore
from brutex.engine.sweep_run import ComboDedup, SweepRunConfig, SweepRunSummary, run_persisted_sweep
from brutex.orchestrator.audit import AuditWriter
from brutex.ranking import MIN_TRADES
from brutex.strategies.types import Side

from tests.golden.test_engine_sweep_apriori_parity import _CONDITION_BITS, _bars


def _config_floor8(k: int) -> SweepRunConfig:
    """The frozen parity cell config at the ARMED rescue floor (min_hits=8)."""
    return SweepRunConfig(
        instrument="NIFTY",
        tf="1m",
        direction=Side.BUY,
        mode="A",
        condition_bits=_CONDITION_BITS,
        k=k,
        data_snapshot_hash="snap-apriori-floor8",
        indicator_version="ind-v1",
        code_commit_sha="deadbeef",
        tf_minutes=1,
        sl_paisa=80,
        tp_paisa=50,
        # The ARMED floor: effective_apriori_min_hits(MIN_TRADES, 8) == 8 —
        # derived through the ONE law, never a re-typed literal.
        min_hits=effective_apriori_min_hits(MIN_TRADES, RESCUE_MIN_HITS_DEFAULT),
    )


def _run(tmp_path: Path, bars: object, k: int) -> SweepRunSummary:
    store = ResultsStore(path=tmp_path / "cell.parquet")
    dedup = ComboDedup(OptionDedupIndex(capacity=10_000, fpr_target=1e-6))
    with AuditWriter.open(tmp_path / "cell.jsonl") as audit:
        return run_persisted_sweep(
            bars,  # type: ignore[arg-type]
            _config_floor8(k),
            store=store,
            dedup=dedup,
            audit=audit,
        )


@pytest.mark.parametrize("k", [1, 2, 3])
def test_floor8_leaderboard_byte_identical_on_vs_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, k: int
) -> None:
    """THE re-derived gate: byte-identical persisted leaderboard at floor 8."""
    pytest.importorskip("polars")
    bars = _bars()

    monkeypatch.delenv("BRUTEX_SWEEP_APRIORI", raising=False)
    off_dir = tmp_path / "off"
    off_dir.mkdir()
    off = _run(off_dir, bars, k)

    monkeypatch.setenv("BRUTEX_SWEEP_APRIORI", "1")
    on_dir = tmp_path / "on"
    on_dir.mkdir()
    on = _run(on_dir, bars, k)

    assert on.store_content_hash == off.store_content_hash
    assert (on_dir / "cell.parquet").read_bytes() == (off_dir / "cell.parquet").read_bytes()
    assert on.persisted == off.persisted
    assert on.ranked == off.ranked
    assert on.backtested == off.backtested
    # Default-OFF pin: brute-force enumeration is the full C(n, k).
    assert off.enumerated == count_combos(k, len(set(_CONDITION_BITS)))


def test_floor8_pruning_fires_and_kept_nonempty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ANTI-VACUOUS at floor 8: pruning genuinely bites AND survivors exist."""
    pytest.importorskip("polars")
    bars = _bars()
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    monkeypatch.delenv("BRUTEX_SWEEP_APRIORI", raising=False)
    off = _run(tmp_path / "a", bars, 3)
    monkeypatch.setenv("BRUTEX_SWEEP_APRIORI", "1")
    on = _run(tmp_path / "b", bars, 3)
    assert on.enumerated < off.enumerated
    assert on.persisted > 0


def test_effective_floor_is_the_armed_rescue_floor() -> None:
    """The ONE floor law: armed ⇒ 8; OFF ⇒ the caller's floor byte-identical."""
    assert effective_apriori_min_hits(MIN_TRADES, RESCUE_MIN_HITS_DEFAULT) == 8
    assert effective_apriori_min_hits(MIN_TRADES, None) == MIN_TRADES
