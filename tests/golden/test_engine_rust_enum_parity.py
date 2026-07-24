"""Byte-identical parity + the design-mandated poison/order goldens — enum kernel.

DEC-SWEEP-ENUM-RUST-001 (MIGRATION.md #1738) — THE merge gate, reworked to the
design-of-record inventory (`DESIGN_FINAL_RUST_SWEEP_CORE.md` §5/§6 Slice 1):

* **Twin-run byte identity (the CI-lane arm of the §7.2 venue law):** a real
  synthetic multi-day lake swept twice — flag OFF (the verbatim Python loop +
  batch Stage-1B) and flag ON (the ranged fused kernel) — persisted
  leaderboard, trial-stats sidecar and completion sentinel all byte-identical,
  with the ``sweep_rust_enum.engaged`` anti-vacuous pin. The REAL-DATA twin is
  the on-branch operator-box gate (`scripts/ci/rust_enum_twin_run.sh`).
* **Emission-order golden (design Slice-1 item 1):** first + last 10k kept
  masks + the blake3 of the FULL emission stream vs the Python oracle over the
  production 60-bit vocabulary, plus the frozen stream literal — the
  LEX-RANK-vs-colex trap's teeth.
* **Inverted-teeth poison PAIR (§5 round-6 symmetrised):** a domain-VALID
  poisoned kernel past the enum burn-in that silently DROPS one persisted-set
  mask — or spuriously KEEPS one below-min-hits mask — with self-consistent
  counts MOVES the tallies AND the store content hash (the finalist belt
  structurally cannot catch a wrong kept SET; the twin-run gate can, and these
  prove it has teeth in BOTH directions).
* **COUNT-ONLY poison (residual (ix) teeth — §7.1):** a tallies-only
  mis-bucketing poison (a min-hits drop re-bucketed as conflict-pruned, kept
  set CORRECT) moves the trial-stats SIDECAR bytes while the store hash stays
  pinned UNCHANGED — proving the ship-time twin-run SIDECAR compare is the
  net for that class.
* **F1 INDEX→TUPLE-DIGEST golden (§3):** the positional checkpoint ↔ tuple
  digest map over a fixture grid is IDENTICAL on the OFF and ON arms (the
  tuple-ORDER authority stays the untouched Python grid iteration — this pin
  makes that invariant tested, not assumed).
* The FM-7 flag-flip CRASH-REDO golden and the per-tuple kept-list equality
  (failure localization below the sweep level).

The DIGEST-PATH GOLDEN VARIANT + engaged-event positive/negative pins + the
LEGACY-REFUSAL + disabled-arm poison goldens live beside the frozen literals
in ``tests/golden/test_engine_sweep_run_golden.py`` (the design mandates the
variant lands in the SAME module that owns ``_GOLDEN_CONTENT_HASH``).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import fno_math
import pytest
from brutex.data.models import CandleBar
from brutex.data.option_dedup import OptionDedupIndex
from brutex.engine.combo_mask import count_combos, count_supermasks, iter_masks_k_of_bits
from brutex.engine.results_store import ResultsStore
from brutex.engine.rust_enum import (
    SWEEP_RUST_ENUM_ENV,
    enum_prefilter_via_kernel,
    reset_enum_self_check_for_tests,
    reset_observability_memo_for_tests,
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

#: The REAL compiled class, captured before any poison monkeypatching.
_REAL_U64 = fno_math.ResidentSweepEnum

#: Fixture facts RE-MEASURED on the OFF arm (2026-07-19, the club-R1
#: #1825 worst-case-fills reconcile — the original 2026-07-11 facts were
#: minted under precise-era fills) and load-bearing for the poison goldens:
#: at k=2 / min_hits=5 the fixture backtests ELEVEN kept masks but only
#: SEVEN persist store rows under worst-case fills — {9, 10, 17, 20, 33,
#: 34, 40} (masks 5/18/24/36 backtest to no persisted row, so a poison
#: targeting them can no longer move store bytes). The highest-hits
#: PERSISTED finalist is mask 17 (bits {0, 4}, 716 hits — mask 5 has 835
#: hits but persists nothing) and the LOWEST-hits persisted finalist is
#: mask 34 (bits {1, 5}, 250 hits); ``min_hits = 251`` drops mask 34 on a
#: healthy run (kept order there: [5, 9, 17, 33, 10, 20, 40]).
_DROP_TARGET_MASK = 17
_SPURIOUS_MASK = 34
_SPURIOUS_MIN_HITS = 251


@pytest.fixture(autouse=True)
def _fresh_rust_enum_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fresh warn-once memo + shadow windows + the OFF/oracle arm per test.

    Since DEC-RUST-DEFAULT-ON-WAVE-001 (#2215) the seam default is ON, so the
    OFF/oracle baseline every ON-vs-OFF parity test compares against must be
    EXPLICITLY armed ``"0"`` (not an unset flag, which would reveal the ON
    default and collapse both arms onto the Rust path)."""
    reset_observability_memo_for_tests()
    reset_enum_self_check_for_tests()
    monkeypatch.setenv(SWEEP_RUST_ENUM_ENV, "0")
    monkeypatch.delenv("BRUTEX_SWEEP_APRIORI", raising=False)


def _bars() -> list[CandleBar]:
    """A 14-day synthetic 1m fixture with mixed intraday structure.

    RE-SHAPED at the 2026-07-19 club-R1 merge-reconcile onto main's landed
    DEC-FILL-WORST-CASE-ONLY-001 (#1825) fixture law (the
    `_worst_case_ranking_bars` note in test_engine_sweep_run.py): the
    original 0.6-per-3-bars drift under a ±0.5 pad lost every trade once
    entries fill at the bar HIGH, so the worst-case arm never persisted a
    finalist and this golden's anti-vacuity teeth (persisted > 0; the
    inverted drop/keep hash moves) went dead. This variant rises 1.0 with a
    0.5 dip every 4th bar under a tight ±0.05 pad — worst-case fills still
    clear the rank gates, and the ON-vs-OFF byte-parity law is
    bars-shape-independent either way.
    """
    out: list[CandleBar] = []
    for d in range(14):
        price = 100.0
        for m in range(120):
            close = price + 1.0 if m % 4 else price - 0.5
            ts = (
                datetime(2026, 1, 5, 9, 16, tzinfo=_IST) + timedelta(days=d, minutes=m)
            ).astimezone(UTC)
            hi = max(price, close) + 0.05
            lo = min(price, close) - 0.05
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


def _config(k: int, min_hits: int = _MIN_HITS) -> SweepRunConfig:
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
        # sl/tp widened 80/50 → 200/100 at the club-R1 #1825 reconcile (the
        # main fixture-law pair the worst-case fills still rank under).
        sl_paisa=200,
        tp_paisa=100,
        min_hits=min_hits,
    )


def _run(
    tmp_path: Path,
    tag: str,
    bars: Sequence[CandleBar],
    k: int,
    digest: str | None,
    *,
    min_hits: int = _MIN_HITS,
    audit_tag: str | None = None,
) -> SweepRunSummary:
    store = ResultsStore(path=tmp_path / f"{tag}.parquet")
    dedup = ComboDedup(OptionDedupIndex(capacity=10_000, fpr_target=1e-6))
    with AuditWriter.open(tmp_path / f"{audit_tag or tag}.jsonl") as audit:
        return run_persisted_sweep(
            bars,
            _config(k, min_hits),
            store=store,
            dedup=dedup,
            audit=audit,
            # DEC-FILL-EXEC-GRID-1M-001: the 1-minute execution stream is a
            # REQUIRED arg since the exec-grid landed. These decision bars ARE
            # 1-minute bars, so the two streams coincide (the tf==1 fixed point
            # — the same-seq law), which keeps this parity fixture's ON/OFF
            # comparison isolated to the enum arm.
            exec_bars_1m=bars,
            trial_stats_digest=digest,
        )


@pytest.mark.parametrize("k", [1, 2, 3])
def test_leaderboard_byte_identical_on_vs_off_digest_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, k: int
) -> None:
    """THE gate: ON+digest == OFF+digest, byte-for-byte, kernel PROVEN engaged
    — with the Stage-1B min-hits FUSED in-kernel on the ON arm."""
    pytest.importorskip("polars")  # flush needs the [data] extra
    bars = _bars()

    monkeypatch.setenv(SWEEP_RUST_ENUM_ENV, "0")  # the OFF/oracle arm (#2215: default is ON)
    off_dir = tmp_path / "off"
    off_dir.mkdir()
    off = _run(off_dir, "cell", bars, k, _DIGEST)

    reset_enum_self_check_for_tests()
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

    # Byte-identical persisted leaderboard + sidecar + sentinel.
    assert on.store_content_hash == off.store_content_hash
    assert (on_dir / "cell.parquet").read_bytes() == (off_dir / "cell.parquet").read_bytes()
    on_sidecar = trial_stats_sidecar_path(on_dir / "cell.parquet")
    off_sidecar = trial_stats_sidecar_path(off_dir / "cell.parquet")
    assert on_sidecar.read_bytes() == off_sidecar.read_bytes()
    on_sentinel = completion_sentinel_path(on_dir / "cell.parquet")
    off_sentinel = completion_sentinel_path(off_dir / "cell.parquet")
    assert on_sentinel.read_bytes() == off_sentinel.read_bytes()

    # Counter identity (the sidecar/audit-byte law) — incl. the fused
    # min-hits bucketing (evaluated / minhits_dropped).
    assert on.enumerated == off.enumerated == count_combos(k, len(set(_CONDITION_BITS)))
    assert on.conflict_pruned == off.conflict_pruned
    assert on.evaluated == off.evaluated
    assert on.minhits_dropped == off.minhits_dropped
    assert on.persisted == off.persisted
    assert on.ranked == off.ranked
    assert on.backtested == off.backtested

    # The prune genuinely fires from k=2 (bits carry three real pairs), the
    # min-hits filter genuinely drops at k=2, and the low-k finalist pool is
    # non-empty (meaningful parity).
    if k >= 2:
        assert on.conflict_pruned > 0
        assert on.minhits_dropped > 0
    if k == 1:
        assert on.persisted > 0


def test_legacy_no_digest_on_matches_off_and_never_engages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On the legacy no-digest path the flag-ON run is byte-identical to OFF
    with ZERO ``engaged`` events and one ``legacy_dedup_path`` warn — the
    kernel refuses the legacy dedup arm by contract."""
    pytest.importorskip("polars")
    bars = _bars()
    k = 2

    monkeypatch.setenv(SWEEP_RUST_ENUM_ENV, "0")  # the OFF/oracle arm (#2215: default is ON)
    off_dir = tmp_path / "off"
    off_dir.mkdir()
    off = _run(off_dir, "cell", bars, k, None)

    monkeypatch.setenv(SWEEP_RUST_ENUM_ENV, "1")
    on_dir = tmp_path / "on"
    on_dir.mkdir()
    with capture_logs() as logs:
        on = _run(on_dir, "cell", bars, k, None)

    assert [e for e in logs if e["event"] == "sweep_rust_enum.engaged"] == []
    warns = [e for e in logs if e["event"] == "sweep_rust_enum.fallback"]
    assert [w["reason"] for w in warns] == ["legacy_dedup_path"]
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

    monkeypatch.setenv(SWEEP_RUST_ENUM_ENV, "0")  # the OFF/oracle arm (#2215: default is ON)
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

    reset_enum_self_check_for_tests()
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


# ────── inverted-teeth poison pair + count-only poison (design §5/§7.1) ──────


class _PoisonBase:
    """Wraps the REAL kernel and post-processes exactly one range."""

    def __init__(self, bar_bits: list[int]) -> None:
        self._real = _REAL_U64(bar_bits)
        self.fired = False

    def n_bars(self) -> int:
        return self._real.n_bars()

    def enumerate_prefilter(self, *args: Any) -> tuple[int, int, int, bool, int, list[int]]:
        raw = self._real.enumerate_prefilter(*args)
        if not self.fired:
            poisoned = self._poison(raw)
            if poisoned is not None:
                self.fired = True
                return poisoned
        return raw

    def _poison(
        self, raw: tuple[int, int, int, bool, int, list[int]]
    ) -> tuple[int, int, int, bool, int, list[int]] | None:
        raise NotImplementedError


class _DropFinalist(_PoisonBase):
    """Silently DROP the highest-hits PERSISTED finalist mask with
    self-consistent counts (the drop re-bucketed as a conflict prune; the
    in-band hits count adjusted per the short-circuit law) — the design's
    dropped-mask poison. The target must be a mask that persists a store
    row under worst-case fills, or the byte-compare teeth go dead."""

    def _poison(
        self, raw: tuple[int, int, int, bool, int, list[int]]
    ) -> tuple[int, int, int, bool, int, list[int]] | None:
        enumerated, pruned, next_rank, exhausted, hits, kept = raw
        if _DROP_TARGET_MASK not in kept:
            return None
        kept = [m for m in kept if m != _DROP_TARGET_MASK]
        return (enumerated, pruned + 1, next_rank, exhausted, hits - 1, kept)


class _SpuriousKeep(_PoisonBase):
    """Wrongly ADMIT the below-min-hits mask 34 (a genuine gate-passing,
    row-persisting strategy the healthy min_hits=251 arm drops) with
    self-consistent counts — the round-6 spurious-keep mirror. The mask is
    conflict-free, so the tallies need NO adjustment: only the seam-derived
    minhits_dropped moves."""

    def _poison(
        self, raw: tuple[int, int, int, bool, int, list[int]]
    ) -> tuple[int, int, int, bool, int, list[int]] | None:
        enumerated, pruned, next_rank, exhausted, hits, kept = raw
        if _SPURIOUS_MASK in kept or 20 not in kept:
            return None
        kept = list(kept)
        kept.insert(kept.index(20), _SPURIOUS_MASK)  # lex position: {1,5} before {2,4}
        return (enumerated, pruned, next_rank, exhausted, hits, kept)


class _CountOnlyMisbucket(_PoisonBase):
    """Re-bucket ONE min-hits drop as a conflict prune — kept set CORRECT,
    Σ enumerated CORRECT, hits law self-consistent: the residual-(ix)
    tallies-only divergence."""

    def _poison(
        self, raw: tuple[int, int, int, bool, int, list[int]]
    ) -> tuple[int, int, int, bool, int, list[int]] | None:
        enumerated, pruned, next_rank, exhausted, hits, kept = raw
        if len(kept) >= enumerated - pruned:  # need a genuine min-hits drop
            return None
        return (enumerated, pruned + 1, next_rank, exhausted, hits - 1, kept)


def _poisoned_run(
    tmp_path: Path,
    tag: str,
    poison_cls: type[_PoisonBase],
    monkeypatch: pytest.MonkeyPatch,
    *,
    min_hits: int = _MIN_HITS,
) -> tuple[SweepRunSummary, _PoisonBase]:
    """One flag-ON run past the enum burn-in with *poison_cls* installed."""
    instances: list[_PoisonBase] = []

    def _factory(bar_bits: list[int]) -> _PoisonBase:
        inst = poison_cls(bar_bits)
        instances.append(inst)
        return inst

    monkeypatch.setattr(fno_math, "ResidentSweepEnum", _factory)
    monkeypatch.setenv(SWEEP_RUST_ENUM_ENV, "1")
    reset_observability_memo_for_tests()
    # PAST the burn-in: the shadow window is closed (masks floor consumed) —
    # the poison must escape the runtime nets and be caught only by the
    # golden-level byte compare (that is exactly what these teeth prove).
    reset_enum_self_check_for_tests(masks_remaining=0)
    summary = _run(tmp_path, tag, _bars(), 2, _DIGEST, min_hits=min_hits)
    assert instances, "the poisoned kernel class was never constructed"
    return summary, instances[0]


def test_inverted_teeth_dropped_mask_moves_tallies_and_content_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A domain-VALID silent DROP of a finalist mask past the enum burn-in
    MOVES the tallies AND the store content hash — necessary because the
    finalist belt structurally cannot catch a wrong kept SET; the ship-time
    twin-run byte compare (this golden's shape) is the net with teeth."""
    pytest.importorskip("polars")
    off_dir = tmp_path / "off"
    off_dir.mkdir()
    off = _run(off_dir, "cell", _bars(), 2, _DIGEST)
    on_dir = tmp_path / "on"
    on_dir.mkdir()
    poisoned, inst = _poisoned_run(on_dir, "cell", _DropFinalist, monkeypatch)
    assert inst.fired
    assert poisoned.store_content_hash != off.store_content_hash
    assert poisoned.conflict_pruned == off.conflict_pruned + 1  # the tallies move
    assert poisoned.evaluated == off.evaluated - 1
    assert poisoned.persisted == off.persisted - 1


def test_inverted_teeth_spurious_keep_moves_tallies_and_content_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The round-6 SYMMETRIC twin: a spuriously-KEPT below-min-hits mask (a
    gate-passing strategy today's spine would never emit at this floor)
    walks, persists, and MOVES the tallies AND the content hash — the
    twin-run gate has teeth in BOTH directions of the kept-set class."""
    pytest.importorskip("polars")
    off_dir = tmp_path / "off"
    off_dir.mkdir()
    off = _run(off_dir, "cell", _bars(), 2, _DIGEST, min_hits=_SPURIOUS_MIN_HITS)
    on_dir = tmp_path / "on"
    on_dir.mkdir()
    poisoned, inst = _poisoned_run(
        on_dir, "cell", _SpuriousKeep, monkeypatch, min_hits=_SPURIOUS_MIN_HITS
    )
    assert inst.fired
    assert poisoned.store_content_hash != off.store_content_hash
    assert poisoned.minhits_dropped == off.minhits_dropped - 1  # the tallies move
    assert poisoned.backtested == off.backtested + 1
    assert poisoned.persisted == off.persisted + 1


def test_count_only_poison_moves_the_sidecar_but_not_the_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Residual-(ix) teeth: a COUNT-ONLY mis-bucketing (one min-hits drop
    incremented into conflict_pruned; kept set + Σ enumerated CORRECT) leaves
    the store hash pinned UNCHANGED while the trial-stats SIDECAR bytes MOVE
    — proving the ship-time twin-run SIDECAR compare catches exactly the
    class the store-hash compare is blind to."""
    pytest.importorskip("polars")
    off_dir = tmp_path / "off"
    off_dir.mkdir()
    off = _run(off_dir, "cell", _bars(), 2, _DIGEST)
    on_dir = tmp_path / "on"
    on_dir.mkdir()
    poisoned, inst = _poisoned_run(on_dir, "cell", _CountOnlyMisbucket, monkeypatch)
    assert inst.fired
    # Store bytes PINNED unchanged (the kept set is correct) …
    assert poisoned.store_content_hash == off.store_content_hash
    assert (on_dir / "cell.parquet").read_bytes() == (off_dir / "cell.parquet").read_bytes()
    # … while the sidecar bytes MOVE (the mis-bucketed tallies persist there).
    on_sidecar = trial_stats_sidecar_path(on_dir / "cell.parquet").read_bytes()
    off_sidecar = trial_stats_sidecar_path(off_dir / "cell.parquet").read_bytes()
    assert on_sidecar != off_sidecar
    assert poisoned.conflict_pruned == off.conflict_pruned + 1
    assert poisoned.minhits_dropped == off.minhits_dropped - 1


# ────── emission-order golden (design Slice-1 item 1) ──────

#: Frozen blake3 (hex) of the FULL kept-mask emission stream — production
#: 60-bit vocabulary, k=3, min_hits=0, REAL conflict pairs, each mask encoded
#: as 16 little-endian bytes. Minted from the PYTHON oracle 2026-07-11;
#: re-lock ONLY on a deliberate vocabulary/conflict-table change.
_EMISSION_STREAM_BLAKE3 = "54c107154ec79cd311c439611d031d717a32f0d96a273e31745a12201797c384"
_EMISSION_KEPT_COUNT = 32_733


def test_emission_order_golden_first_last_10k_and_full_stream_blake3(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The design-mandated emission-order golden: first + last 10k kept masks
    + the blake3 of the FULL emission stream vs the Python oracle, plus the
    frozen stream literal — a colex/standard-CNS unranking (or ANY order or
    set drift) breaks this golden instead of silently moving durable bytes."""
    bits = list(range(60))
    k = 3
    oracle = [m for m in iter_masks_k_of_bits(bits, k) if not has_conflict(m)]
    assert len(oracle) == _EMISSION_KEPT_COUNT
    monkeypatch.setenv(SWEEP_RUST_ENUM_ENV, "1")
    result = enum_prefilter_via_kernel(
        bits,
        k,
        min_hits=0,
        bar_bits=[],
        count_hits=lambda masks: [0] * len(masks),
        digest_path=True,
    )
    assert result is not None
    kept = result.kept
    assert kept[:10_000] == oracle[:10_000]
    assert kept[-10_000:] == oracle[-10_000:]
    stream = b"".join(m.to_bytes(16, "little") for m in kept)
    oracle_stream = b"".join(m.to_bytes(16, "little") for m in oracle)
    assert fno_math.blake3_hash(stream).hex() == _EMISSION_STREAM_BLAKE3
    assert fno_math.blake3_hash(oracle_stream).hex() == _EMISSION_STREAM_BLAKE3
    # The set+order survivor golden vs the flat path (one plain ==).
    assert kept == oracle


# ────── F1 index→tuple-digest golden (design §3) ──────


def test_f1_positional_checkpoint_tuple_digest_map_is_arm_identical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verifier B F1: the durable checkpoint is POSITIONAL (a tuple index),
    so the positional-index → tuple-digest map over a fixture grid must be
    IDENTICAL on the OFF and ON arms — any enumeration/ordering drift breaks
    this golden instead of silently prefix-skipping never-run tuples on
    resume. (The tuple-ORDER authority stays the untouched Python grid
    iteration; the enum kernel serves MASKS inside one tuple — this pin makes
    the invariant tested, not assumed.)"""
    from brutex.engine.sweep_orchestrator import (
        SweepGridConfig,
        Timeframe,
        _tuple_digest,
        iter_sweep_tuples,
    )

    def _digests() -> list[str]:
        grid = SweepGridConfig(
            instruments=["NIFTY", "BANKNIFTY"],
            timeframes=[Timeframe("1minute", 1), Timeframe("5minute", 5)],
            directions=[Side.BUY, Side.SELL],
            k_values=[1, 2],
            condition_bits=[0, 1, 2, 3, 4],
            stop_losses=[80],
            take_profits=[50],
            proximities=[None],
            mode="A",
            data_snapshot_hash="snap0001",
            indicator_version="ind-v1",
            code_commit_sha="deadbeef",
        )
        return [_tuple_digest(t, grid) for t in iter_sweep_tuples(grid)]

    monkeypatch.setenv(SWEEP_RUST_ENUM_ENV, "0")  # the OFF/oracle arm (#2215: default is ON)
    off_map = _digests()
    monkeypatch.setenv(SWEEP_RUST_ENUM_ENV, "1")
    on_map = _digests()
    assert off_map == on_map
    assert len(off_map) == 16  # 2 instruments × 2 TFs × 2 directions × 2 k
    assert len(set(off_map)) == 16  # digests are distinct (no collisions)


# ────── per-tuple kept-list equality (failure localization) ──────


@pytest.mark.parametrize("k", [1, 2, 3, 4])
@pytest.mark.parametrize("min_hits", [0, 5])
def test_per_tuple_kept_list_equality(
    monkeypatch: pytest.MonkeyPatch, k: int, min_hits: int
) -> None:
    """Failure localization below the sweep: the kernel's kept list for this
    cell's bits equals today's spine (conflict prune + batch Stage-1B) —
    SET ∧ ORDER — on both min-hits arms."""
    bar_bits = [0b10101010, 0b01010101, 0b11111111, 0b00000001] * 8
    monkeypatch.setenv(SWEEP_RUST_ENUM_ENV, "1")
    result = enum_prefilter_via_kernel(
        _CONDITION_BITS,
        k,
        min_hits=min_hits,
        bar_bits=bar_bits,
        count_hits=lambda masks: count_supermasks(bar_bits, masks),
        digest_path=True,
    )
    assert result is not None
    survivors = [m for m in iter_masks_k_of_bits(_CONDITION_BITS, k) if not has_conflict(m)]
    if min_hits > 0 and survivors:
        hits = count_supermasks(bar_bits, survivors)
        oracle = [m for m, h in zip(survivors, hits, strict=True) if h >= min_hits]
    else:
        oracle = survivors
    assert result.kept == oracle
    assert result.enumerated == count_combos(k, len(set(_CONDITION_BITS)))
    assert result.conflict_pruned == result.enumerated - len(survivors)
    assert result.evaluated == len(survivors)
