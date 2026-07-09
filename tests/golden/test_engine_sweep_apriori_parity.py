"""Byte-identical leaderboard parity: Apriori ON vs the brute-force path OFF.

DEC-SWEEP-APRIORI-TUPLE-001 — THE merge gate. A real synthetic multi-day lake is
swept through :func:`brutex.engine.sweep_run.run_persisted_sweep` twice for the
SAME cell config — once with ``BRUTEX_SWEEP_APRIORI`` unset (the brute-force
enumeration) and once set (the Apriori pruned generator) — and the persisted
leaderboard is asserted **byte-identical**: same ``store_content_hash`` AND same
``.parquet`` bytes. The proof rests on the anti-monotone kept-set equality
(``sweep_apriori`` module docstring) + the order-preserving emission +
:func:`brutex.ranking.rank_combos` deriving ``N`` and its z-score pool from the
kept set alone (DSR ``N = len(kept)`` prune-invariance).

ANTI-VACUOUS: the test also asserts that pruning genuinely FIRES
(``enumerated`` ON ``<`` OFF at a ``k >= 3`` level) while the kept/finalist set
is non-empty — so the parity is meaningful, not a trivial "both did the same
work". Both the streaming DIGEST path (sidecar + sentinel) and the legacy
no-digest path are covered, plus the digest sidecar's exact-zero tally invariant
(the one :func:`brutex.engine.sweep_verify.verify_structural` enforces) and a
default-OFF tally pin.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.data.models import CandleBar
from brutex.data.option_dedup import OptionDedupIndex
from brutex.engine.combo_mask import count_combos
from brutex.engine.results_store import ResultsStore
from brutex.engine.sweep_run import (
    ComboDedup,
    SweepRunConfig,
    SweepRunSummary,
    run_persisted_sweep,
)
from brutex.engine.trial_stats import read_tuple_trial_stats, trial_stats_sidecar_path
from brutex.orchestrator.audit import AuditWriter
from brutex.strategies.types import Side

_IST = timezone(timedelta(hours=5, minutes=30))

#: A cell whose bits carry three conflict pairs (0,1)/(2,3)/(4,5) + conflict-free
#: bits, over a modest 8-bit palette so C(8, k) is small but pruning still bites.
_CONDITION_BITS = [0, 1, 2, 3, 4, 5, 6, 7]
_MIN_HITS = 5


def _bars() -> list[CandleBar]:
    """A 14-day synthetic 1m fixture with mixed intraday structure.

    The upward-drift-with-dips shape makes the vocabulary bits fire on DIFFERENT
    bar subsets (some dense, some sparse), so the frequent frontier genuinely
    prunes sparse triples/quads at ``k >= 3`` while keeping the dense ones.
    """
    out: list[CandleBar] = []
    for d in range(14):
        price = 100.0
        for m in range(120):
            close = price + 0.6 if m % 3 else price - 0.3
            ts = (
                datetime(2026, 1, 5, 9, 16, tzinfo=_IST) + timedelta(days=d, minutes=m)
            ).astimezone(UTC)
            hi = max(price, close) + 0.5
            lo = min(price, close) - 0.5
            out.append(
                CandleBar(
                    timestamp=ts,
                    open=Decimal(str(round(price, 2))),
                    high=Decimal(str(round(hi, 2))),
                    low=Decimal(str(round(lo, 2))),
                    close=Decimal(str(round(close, 2))),
                    volume=100,
                    open_interest=None,
                )
            )
            price = close
    return out


def _config(k: int) -> SweepRunConfig:
    return SweepRunConfig(
        instrument="NIFTY",
        tf="1m",
        direction=Side.BUY,
        mode="A",
        condition_bits=_CONDITION_BITS,
        k=k,
        data_snapshot_hash="snap-apriori",
        indicator_version="ind-v1",
        code_commit_sha="deadbeef",
        tf_minutes=1,
        sl_paisa=80,
        tp_paisa=50,
        min_hits=_MIN_HITS,
    )


def _run(
    tmp_path: Path, tag: str, bars: Sequence[CandleBar], k: int, digest: str | None
) -> SweepRunSummary:
    store = ResultsStore(path=tmp_path / f"{tag}.parquet")
    dedup = ComboDedup(OptionDedupIndex(capacity=10_000, fpr_target=1e-6))
    with AuditWriter.open(tmp_path / f"{tag}.jsonl") as audit:
        return run_persisted_sweep(
            bars, _config(k), store=store, dedup=dedup, audit=audit, trial_stats_digest=digest
        )


@pytest.mark.parametrize("k", [1, 2, 3, 4])
@pytest.mark.parametrize("digest", [None, "abad1deaabad1dea"])
def test_leaderboard_byte_identical_on_vs_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, k: int, digest: str | None
) -> None:
    pytest.importorskip("polars")  # flush needs the [data] extra
    bars = _bars()

    monkeypatch.delenv("BRUTEX_SWEEP_APRIORI", raising=False)
    off_dir = tmp_path / "off"
    off_dir.mkdir()
    off = _run(off_dir, "cell", bars, k, digest)

    monkeypatch.setenv("BRUTEX_SWEEP_APRIORI", "1")
    on_dir = tmp_path / "on"
    on_dir.mkdir()
    on = _run(on_dir, "cell", bars, k, digest)

    # THE gate: byte-identical persisted leaderboard.
    assert on.store_content_hash == off.store_content_hash
    assert (on_dir / "cell.parquet").read_bytes() == (off_dir / "cell.parquet").read_bytes()
    # The kept/finalist counts must also agree (a further necessary condition).
    assert on.persisted == off.persisted
    assert on.ranked == off.ranked
    assert on.backtested == off.backtested

    # Default-OFF pin: with the flag unset the enumeration is the full brute-force
    # C(n, k) — unchanged from the pre-change behaviour.
    assert off.enumerated == count_combos(k, len(set(_CONDITION_BITS)))

    # The digest path's exact-zero tally invariant (verify_structural enforces it).
    if digest is not None:
        stats = read_tuple_trial_stats(trial_stats_sidecar_path(on_dir / "cell.parquet"))
        remainder = (
            stats.n_enumerated
            - stats.n_conflict_pruned
            - stats.n_minhits_dropped
            - stats.n_backtested
        )
        assert remainder == 0


def test_pruning_actually_fires_and_kept_is_nonempty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ANTI-VACUOUS: at k=3 the Apriori ``enumerated`` is strictly smaller than the
    brute-force enumeration AND the kept/finalist set is non-empty."""
    pytest.importorskip("polars")
    bars = _bars()
    k = 3

    monkeypatch.delenv("BRUTEX_SWEEP_APRIORI", raising=False)
    off = _run(tmp_path / "a", "cell", bars, k, None)
    monkeypatch.setenv("BRUTEX_SWEEP_APRIORI", "1")
    on = _run(tmp_path / "b", "cell", bars, k, None)

    assert on.enumerated < off.enumerated  # pruning bit
    assert on.conflict_pruned == 0  # Apriori candidates are conflict-free by construction
    assert on.persisted > 0  # F_k is non-empty (meaningful parity)
    # The brute-force path DID prune conflicts (so the two paths reach the same
    # kept set by different routes).
    assert off.conflict_pruned > 0
