"""Byte-identical leaderboard parity: Rust enum kernel ON vs the Python loop OFF.

DEC-SWEEP-ENUM-RUST-001 (MIGRATION.md #1738) — THE merge gate. A real synthetic
multi-day lake is swept through :func:`brutex.engine.sweep_run.run_persisted_sweep`
twice for the SAME cell config — once with ``BRUTEX_SWEEP_RUST_ENUM`` unset (the
verbatim Python enumerate+prune loop) and once set (the fused Rust kernel) — and
the persisted leaderboard is asserted **byte-identical**: same
``store_content_hash`` AND same ``.parquet`` bytes AND (on the digest path) the
same trial-stats sidecar + completion-sentinel bytes. The proof rests on the
kernel's order law (survivors in the exact ``itertools.combinations``
lexicographic order) + the counter identity (``enumerated`` /
``conflict_pruned`` embed verbatim in the sidecar + audit payloads).

ANTI-VACUOUS: the ON+digest runs additionally assert the
``sweep_rust_enum.engaged`` INFO genuinely fired (the kernel actually served
the enumeration — never a trivially-passing both-ran-Python parity), the
conflict prune genuinely bit (``conflict_pruned > 0`` at k >= 2), and the
finalist set is non-empty at low k.

Also pinned here: the direct per-tuple survivor list equality (failure
localization below the sweep level), the FM-7 flag-flip CRASH-REDO golden
(complete OFF → sentinel deleted → redo ON with the same digest → every
artifact rewrites byte-identically), and the legacy no-digest ON run (the
kernel never engages — bytes match OFF with zero ``engaged`` events).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.data.models import CandleBar
from brutex.data.option_dedup import OptionDedupIndex
from brutex.engine.combo_mask import count_combos, iter_masks_k_of_bits
from brutex.engine.results_store import ResultsStore
from brutex.engine.rust_enum import (
    SWEEP_RUST_ENUM_ENV,
    enum_survivors_via_kernel,
    reset_observability_memo_for_tests,
    reset_self_check_for_tests,
)
from brutex.engine.sweep_run import (
    ComboDedup,
    SweepRunConfig,
    SweepRunSummary,
    run_persisted_sweep,
)
from brutex.engine.trial_stats import trial_stats_sidecar_path
from brutex.engine.tuple_complete import completion_sentinel_path
from brutex.orchestrator.audit import AuditWriter
from brutex.strategies.types import Side
from brutex.vocabulary import has_conflict
from structlog.testing import capture_logs

_IST = timezone(timedelta(hours=5, minutes=30))

#: A cell whose bits carry three REAL conflict pairs (0,1)/(2,3)/(4,5) plus
#: conflict-free bits, over a modest 8-bit palette so C(8, k) is small but
#: pruning still bites (the apriori-parity fixture shape).
_CONDITION_BITS = [0, 1, 2, 3, 4, 5, 6, 7]
_MIN_HITS = 5
_DIGEST = "abad1deaabad1dea"


@pytest.fixture(autouse=True)
def _fresh_rust_enum_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fresh warn-once memo + burn-in budget + an UNSET flag per test."""
    reset_observability_memo_for_tests()
    reset_self_check_for_tests()
    monkeypatch.delenv(SWEEP_RUST_ENUM_ENV, raising=False)
    monkeypatch.delenv("BRUTEX_SWEEP_APRIORI", raising=False)


def _bars() -> list[CandleBar]:
    """A 14-day synthetic 1m fixture with mixed intraday structure."""
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
        data_snapshot_hash="snap-rust-enum",
        indicator_version="ind-v1",
        code_commit_sha="deadbeef",
        tf_minutes=1,
        sl_paisa=80,
        tp_paisa=50,
        min_hits=_MIN_HITS,
    )


def _run(
    tmp_path: Path,
    tag: str,
    bars: Sequence[CandleBar],
    k: int,
    digest: str | None,
    *,
    audit_tag: str | None = None,
) -> SweepRunSummary:
    store = ResultsStore(path=tmp_path / f"{tag}.parquet")
    dedup = ComboDedup(OptionDedupIndex(capacity=10_000, fpr_target=1e-6))
    with AuditWriter.open(tmp_path / f"{audit_tag or tag}.jsonl") as audit:
        return run_persisted_sweep(
            bars,
            _config(k),
            store=store,
            dedup=dedup,
            audit=audit,
            trial_stats_digest=digest,
        )


@pytest.mark.parametrize("k", [1, 2, 3])
def test_leaderboard_byte_identical_on_vs_off_digest_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, k: int
) -> None:
    """THE gate: ON+digest == OFF+digest, byte-for-byte, kernel PROVEN engaged."""
    pytest.importorskip("polars")  # flush needs the [data] extra
    bars = _bars()

    monkeypatch.delenv(SWEEP_RUST_ENUM_ENV, raising=False)
    off_dir = tmp_path / "off"
    off_dir.mkdir()
    off = _run(off_dir, "cell", bars, k, _DIGEST)

    reset_self_check_for_tests()
    reset_observability_memo_for_tests()
    monkeypatch.setenv(SWEEP_RUST_ENUM_ENV, "1")
    on_dir = tmp_path / "on"
    on_dir.mkdir()
    with capture_logs() as logs:
        on = _run(on_dir, "cell", bars, k, _DIGEST)

    # ANTI-VACUOUS: the Rust kernel genuinely served the ON enumeration.
    assert [e for e in logs if e["event"] == "sweep_rust_enum.engaged"], (
        "the ON run never engaged the kernel — a vacuous parity"
    )

    # Byte-identical persisted leaderboard + sidecar.
    assert on.store_content_hash == off.store_content_hash
    assert (on_dir / "cell.parquet").read_bytes() == (off_dir / "cell.parquet").read_bytes()
    on_sidecar = trial_stats_sidecar_path(on_dir / "cell.parquet")
    off_sidecar = trial_stats_sidecar_path(off_dir / "cell.parquet")
    assert on_sidecar.read_bytes() == off_sidecar.read_bytes()
    on_sentinel = completion_sentinel_path(on_dir / "cell.parquet")
    off_sentinel = completion_sentinel_path(off_dir / "cell.parquet")
    assert on_sentinel.read_bytes() == off_sentinel.read_bytes()

    # Counter identity (the sidecar/audit-byte law).
    assert on.enumerated == off.enumerated == count_combos(k, len(set(_CONDITION_BITS)))
    assert on.conflict_pruned == off.conflict_pruned
    assert on.evaluated == off.evaluated
    assert on.persisted == off.persisted
    assert on.ranked == off.ranked
    assert on.backtested == off.backtested

    # The prune genuinely fires from k=2 (bits carry three real pairs) and
    # the low-k finalist pool is non-empty (meaningful parity).
    if k >= 2:
        assert on.conflict_pruned > 0
    if k == 1:
        assert on.persisted > 0


def test_legacy_no_digest_on_matches_off_and_never_engages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FM-14: on the legacy no-digest path the flag-ON run is byte-identical
    to OFF with ZERO ``engaged`` events — the kernel is structurally
    unreachable there (`bypass_dedup` guard)."""
    pytest.importorskip("polars")
    bars = _bars()
    k = 2

    monkeypatch.delenv(SWEEP_RUST_ENUM_ENV, raising=False)
    off_dir = tmp_path / "off"
    off_dir.mkdir()
    off = _run(off_dir, "cell", bars, k, None)

    monkeypatch.setenv(SWEEP_RUST_ENUM_ENV, "1")
    on_dir = tmp_path / "on"
    on_dir.mkdir()
    with capture_logs() as logs:
        on = _run(on_dir, "cell", bars, k, None)

    assert [e for e in logs if e["event"] == "sweep_rust_enum.engaged"] == []
    assert on.store_content_hash == off.store_content_hash
    assert (on_dir / "cell.parquet").read_bytes() == (off_dir / "cell.parquet").read_bytes()
    assert on.enumerated == off.enumerated
    assert on.conflict_pruned == off.conflict_pruned


def test_flag_flip_crash_redo_rewrites_byte_identically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FM-7: a tuple completed OFF whose completion sentinel is lost (the
    crash shape — the sentinel is written STRICTLY LAST) redoes under flag ON
    with the SAME digest and rewrites EVERY artifact byte-identically:
    parquet, trial-stats sidecar AND the re-paired sentinel."""
    pytest.importorskip("polars")
    bars = _bars()
    k = 2
    cell_dir = tmp_path / "cell"
    cell_dir.mkdir()

    monkeypatch.delenv(SWEEP_RUST_ENUM_ENV, raising=False)
    off = _run(cell_dir, "cell", bars, k, _DIGEST)
    store_path = cell_dir / "cell.parquet"
    sidecar_path = trial_stats_sidecar_path(store_path)
    sentinel_path = completion_sentinel_path(store_path)
    parquet_before = store_path.read_bytes()
    sidecar_before = sidecar_path.read_bytes()
    sentinel_before = sentinel_path.read_bytes()

    # The crash shape: the sentinel (written LAST) is missing — the redo is
    # authoritative and re-runs the FULL population.
    sentinel_path.unlink()

    reset_self_check_for_tests()
    reset_observability_memo_for_tests()
    monkeypatch.setenv(SWEEP_RUST_ENUM_ENV, "1")
    with capture_logs() as logs:
        # The redo appends its OWN audit chain (a real redo is a fresh run —
        # the byte-identical-rewrite contract covers the store/sidecar/
        # sentinel artifacts, never the append-only per-run audit file).
        redo = _run(cell_dir, "cell", bars, k, _DIGEST, audit_tag="cell-redo")

    assert [e for e in logs if e["event"] == "sweep_rust_enum.engaged"], (
        "the redo never engaged the kernel — a vacuous flag-flip parity"
    )
    assert redo.resumed_complete is False
    assert redo.enumerated == off.enumerated
    assert redo.store_content_hash == off.store_content_hash
    assert store_path.read_bytes() == parquet_before
    assert sidecar_path.read_bytes() == sidecar_before
    assert sentinel_path.read_bytes() == sentinel_before


@pytest.mark.parametrize("k", [1, 2, 3, 4])
def test_per_tuple_survivor_list_equality(monkeypatch: pytest.MonkeyPatch, k: int) -> None:
    """Failure localization below the sweep: the kernel's survivor list for
    this cell's bits equals the filtered Python generator — SET ∧ ORDER."""
    monkeypatch.setenv(SWEEP_RUST_ENUM_ENV, "1")
    result = enum_survivors_via_kernel(_CONDITION_BITS, k)
    oracle = [m for m in iter_masks_k_of_bits(_CONDITION_BITS, k) if not has_conflict(m)]
    assert result is not None
    assert result.survivors == oracle
    assert result.enumerated == count_combos(k, len(set(_CONDITION_BITS)))
    assert result.conflict_pruned == result.enumerated - len(oracle)
