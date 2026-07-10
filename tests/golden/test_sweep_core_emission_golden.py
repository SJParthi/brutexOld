"""Goldens for the Rust sweep-core Slice-1 enumeration arm.

DESIGN_FINAL_RUST_SWEEP_CORE.md §6 (Slice 1) + the "Slice-1 substrate +
ordering pins" fold, item 1:

1. **EMISSION-ORDER GOLDEN** — first + last 10k masks + blake3 of the FULL
   emission stream vs the Python `iter_masks_k_of_bits` oracle across
   k = 1..6 and multiple bit sets (scattered, contiguous, u128-crossing),
   plus the memorialized n=4, k=2 POSITION-LEX-not-colex counterexample.
2. **SET+ORDER SURVIVOR GOLDEN** — the kept list vs the flat Python path on
   a real-shaped fixture (the REAL `CONFLICT_PAIRS`, `min_hits > 0`).
3. **COUNTER-EXACTNESS GOLDEN (end-to-end twin)** — the DIGEST-PATH GOLDEN
   VARIANT: `run_persisted_sweep` OFF-arm vs enum-arm on the same fixture is
   byte-identical (summary tallies, store `content_hash`, trial-stats
   sidecar BYTES, completion sentinel) with EXACTLY ONE
   `sweep_rust_enum.engaged` event on the ON arm and ZERO on the OFF arm;
   the frozen `_GOLDEN_CONTENT_HASH_DIGEST` literal is MINTED ON THE OFF ARM
   (shipping-spine-authored, new-core-free by construction — §6 Slice 1).
4. **LEGACY-REFUSAL GOLDEN** — the legacy (no-digest) fixture with the flag
   ON refuses with exactly ONE `legacy_dedup_path` warn, ZERO engaged
   events, and bytes identical to the OFF arm (the §5 precedence-law pin).
5. **F27 seam parity** — an empty Python-side window reproduces
   skip-not-error (the same ValueError) at the SAME tuple with the kernel
   module never even loaded (spy), flag ON or OFF.
6. **Work-bound boundary vectors** — exactly-at-cap `next_rank` pins +
   the dual-knob cap-respected inequalities on the real kernel.
7. **OFF-arm spy** — with the flag unset the kernel loader is never called.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import blake3
import fno_math
import pytest
from brutex.data.models import CandleBar
from brutex.data.option_dedup import OptionDedupIndex
from brutex.engine import sweep_core
from brutex.engine.combo_mask import count_combos, count_supermasks, iter_masks_k_of_bits
from brutex.engine.results_store import ResultsStore
from brutex.engine.sweep_run import ComboDedup, SweepRunConfig, SweepRunSummary, run_persisted_sweep
from brutex.engine.trial_stats import trial_stats_sidecar_path
from brutex.orchestrator.audit import AuditWriter
from brutex.strategies.types import Side
from brutex.vocabulary import has_conflict
from structlog.testing import capture_logs

_IST = timezone(timedelta(hours=5, minutes=30))


@pytest.fixture(autouse=True)
def _fresh_seam_state() -> Iterator[None]:
    """Process-global shadow/latch/memo isolation (the seam test convention)."""
    sweep_core.reset_enum_self_check_for_tests()
    sweep_core.reset_observability_memo_for_tests()
    yield
    sweep_core.reset_enum_self_check_for_tests()
    sweep_core.reset_observability_memo_for_tests()


# ────── 1. Emission-order goldens ──────


def _emission_stream(bits: Sequence[int], k: int) -> list[int]:
    """The core's FULL emission stream (no conflicts, min_hits = 0 → kept ≡
    enumerated), driven through ranged calls like the production driver."""
    core: Any = fno_math.ResidentSweepCore([100], [101], [99], [100], [False], [1], [0])
    if max(bits, default=0) >= 64:
        core = fno_math.ResidentSweepCoreU128([100], [101], [99], [100], [False], [1], [0])
    rank = 0
    out: list[int] = []
    while True:
        _, _, next_rank, exhausted, _, masks = core.enumerate_prefilter(
            sorted(set(bits)), k, [], 0, rank, 1 << 14, 1 << 28
        )
        out.extend(masks)
        rank = next_rank
        if exhausted:
            return out


def _stream_blake3(masks: Sequence[int]) -> str:
    """blake3 of the emission stream — each mask as 16 bytes LE (u128)."""
    hasher = blake3.blake3()
    for mask in masks:
        hasher.update(mask.to_bytes(16, "little"))
    return hasher.hexdigest()


class TestEmissionOrderGolden:
    """Item 1 of the lever-discovery fold — the LEX-RANK mandate's teeth."""

    def test_memorialized_counterexample_n4_k2(self) -> None:
        stream = _emission_stream([0, 1, 2, 3], 2)
        assert stream == [3, 5, 9, 6, 10, 12]  # itertools / POSITION-LEX
        assert stream != [3, 5, 6, 9, 10, 12]  # ascending-numeric == COLEX

    @pytest.mark.parametrize(
        "bits",
        [
            list(range(12)),  # contiguous
            [0, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41, 43],  # scattered
            [2, 59, 60, 63],  # the u64 top edge
        ],
    )
    @pytest.mark.parametrize("k", [1, 2, 3, 4, 5, 6])
    def test_full_stream_matches_python_oracle(self, bits: list[int], k: int) -> None:
        if k > len(bits):
            pytest.skip("k exceeds the bit set")
        expected = list(iter_masks_k_of_bits(bits, k))
        stream = _emission_stream(bits, k)
        assert stream == expected
        assert _stream_blake3(stream) == _stream_blake3(expected)

    @pytest.mark.parametrize("k", [1, 2, 3, 4, 5, 6])
    def test_u128_stream_matches_python_oracle(self, k: int) -> None:
        bits = [1, 33, 62, 63, 64, 65, 90, 100, 127]
        expected = list(iter_masks_k_of_bits(bits, k))
        stream = _emission_stream(bits, k)
        assert stream == expected

    def test_first_and_last_10k_plus_full_stream_blake3(self) -> None:
        # C(25, 6) = 177,100 — a genuinely >10k stream, per the mandate.
        bits = list(range(25))
        k = 6
        expected = list(iter_masks_k_of_bits(bits, k))
        assert len(expected) == count_combos(6, 25) == 177_100
        stream = _emission_stream(bits, k)
        assert stream[:10_000] == expected[:10_000]
        assert stream[-10_000:] == expected[-10_000:]
        digest = _stream_blake3(stream)
        assert digest == _stream_blake3(expected)
        # Frozen literal — the full-stream blake3 of C(25,6) POSITION-LEX
        # masks encoded 16-byte LE (re-lock consciously on any order change).
        assert digest == "c321db78eae147734ae6f3f416049cfb3c69a4fb4f78a659f66df92b50c9f6c6"


# ────── 2. Set+order survivor golden (real-shaped) ──────


class TestSurvivorGolden:
    """The kept list vs the flat Python path — REAL conflict pairs + min_hits."""

    def test_set_and_order_vs_flat_python_path(self) -> None:
        bits = list(range(10))
        k = 3
        min_hits = 2
        # Real-shaped bar_bits: a dense head, sparse tail (some masks fall
        # below min_hits; some conflict via the REAL CONFLICT_PAIRS).
        bar_bits = [(1 << 10) - 1, (1 << 10) - 1, 0b1010101011, 0b1, 0b0] * 4
        # Flat Python path (today's spine, verbatim semantics):
        survivors = [m for m in iter_masks_k_of_bits(bits, k) if not has_conflict(m)]
        hits = count_supermasks(bar_bits, survivors)
        expected = [m for m, h in zip(survivors, hits, strict=True) if h >= min_hits]
        # Core path (real pairs marshalled exactly as the seam does):
        from brutex.vocabulary import CONFLICT_PAIRS

        n = len(bar_bits)
        core = fno_math.ResidentSweepCore(
            [100] * n, [101] * n, [99] * n, [100] * n, [False] * n, [1] * n, bar_bits
        )
        rank = 0
        kept: list[int] = []
        enumerated = 0
        pruned = 0
        while True:
            enum_r, pruned_r, next_rank, exhausted, _, masks = core.enumerate_prefilter(
                bits, k, sorted(CONFLICT_PAIRS), min_hits, rank, 37, 1 << 28
            )
            enumerated += enum_r
            pruned += pruned_r
            kept.extend(masks)
            rank = next_rank
            if exhausted:
                break
        assert kept == expected  # set AND order
        assert enumerated == count_combos(k, len(bits))
        assert pruned == count_combos(k, len(bits)) - len(survivors)


# ────── 3+4. The digest-path golden variant (end-to-end twins) ──────

#: Fixed context mirroring the legacy sweep-run golden's identity discipline.
_DIGEST = "a" * 16


def _bars() -> list[CandleBar]:
    """The 12-day upward-drift fixture (the legacy sweep-run golden's shape)
    — deep enough that the digest variant persists REAL finalist rows."""
    out: list[CandleBar] = []
    for d in range(12):
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


def _config() -> SweepRunConfig:
    return SweepRunConfig(
        instrument="NIFTY",
        tf="1m",
        direction=Side.BUY,
        mode="A",
        condition_bits=[0, 1, 2, 3, 4, 5],
        k=2,
        min_hits=3,
        top_k=5,
        data_snapshot_hash="snap0001",
        indicator_version="ind-v1",
        code_commit_sha="deadbeef",
        tf_minutes=1,
        sl_paisa=80,
        tp_paisa=50,
    )


def _run_digest(tmp_path: Path, tag: str) -> tuple[SweepRunSummary, bytes, bytes]:
    """One digest-path run → (summary, store bytes, sidecar bytes)."""
    store_path = tmp_path / f"{tag}.parquet"
    store = ResultsStore(path=store_path)
    dedup = ComboDedup(OptionDedupIndex(capacity=10_000, fpr_target=1e-6))
    audit = AuditWriter(path=tmp_path / f"{tag}.jsonl")
    with audit:
        summary = run_persisted_sweep(
            _bars(),
            _config(),
            store=store,
            dedup=dedup,
            audit=audit,
            trial_stats_digest=_DIGEST,
        )
    sidecar = trial_stats_sidecar_path(store_path)
    return summary, store_path.read_bytes(), sidecar.read_bytes()


#: The OFF-arm-minted store content hash of the digest fixture (the §6
#: shipping-spine-authored literal — new-core-free by construction; the
#: determinism assertions beside it are the version-robust contract, the
#: literal the strongest form — same caveat as the legacy sweep-run golden).
_GOLDEN_CONTENT_HASH_DIGEST = "9be499497db187a339d9adfb23e518a0bd84ea35711ab69d5e09ba090c9319ab"

#: The digest fixture's logical tallies (identical on BOTH arms).
_GOLDEN_TALLIES_DIGEST = {"enumerated": 15, "conflict_pruned": 3, "evaluated": 12}


class TestDigestPathTwin:
    """The end-to-end counter-exactness golden — OFF arm ≡ enum arm, bytes."""

    def test_off_arm_reproduces_the_minted_literals(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(sweep_core.SWEEP_RUST_ENUM_ENV, raising=False)
        with capture_logs() as logs:
            summary, _store_bytes, _sidecar = _run_digest(tmp_path, "off")
        assert summary.store_content_hash == _GOLDEN_CONTENT_HASH_DIGEST
        assert summary.enumerated == _GOLDEN_TALLIES_DIGEST["enumerated"]
        assert summary.conflict_pruned == _GOLDEN_TALLIES_DIGEST["conflict_pruned"]
        assert summary.evaluated == _GOLDEN_TALLIES_DIGEST["evaluated"]
        assert summary.skipped_dedup == 0
        engaged = [e for e in logs if e["event"] == "sweep_rust_enum.engaged"]
        assert engaged == []  # the negative pin — OFF arm never engages

    def test_enum_arm_is_byte_identical_with_exactly_one_engaged_event(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(sweep_core.SWEEP_RUST_ENUM_ENV, raising=False)
        off_summary, off_store, off_sidecar = _run_digest(tmp_path, "off")
        monkeypatch.setenv(sweep_core.SWEEP_RUST_ENUM_ENV, "1")
        with capture_logs() as logs:
            on_summary, on_store, on_sidecar = _run_digest(tmp_path, "on")
        # Counter exactness incl. the trial-stats sidecar BYTES (§6 golden 3).
        assert on_summary == off_summary
        assert on_summary.store_content_hash == _GOLDEN_CONTENT_HASH_DIGEST
        assert on_store == off_store
        assert on_sidecar == off_sidecar
        engaged = [e for e in logs if e["event"] == "sweep_rust_enum.engaged"]
        assert len(engaged) == 1  # the anti-vacuous positive pin
        fallbacks = [e for e in logs if e["event"] == "sweep_rust_enum.fallback"]
        assert fallbacks == []

    def test_legacy_fixture_refuses_with_one_precedence_law_warn(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The §5 FALLBACK-REASON PRECEDENCE golden: the legacy (no-digest)
        fixture under the enum flag warns EXACTLY `legacy_dedup_path` and
        stays byte-identical to the OFF arm."""

        def _run_legacy(tag: str) -> tuple[SweepRunSummary, bytes]:
            store_path = tmp_path / f"{tag}.parquet"
            store = ResultsStore(path=store_path)
            dedup = ComboDedup(OptionDedupIndex(capacity=10_000, fpr_target=1e-6))
            audit = AuditWriter(path=tmp_path / f"{tag}.jsonl")
            with audit:
                summary = run_persisted_sweep(
                    _bars(), _config(), store=store, dedup=dedup, audit=audit
                )
            return summary, store_path.read_bytes()

        monkeypatch.delenv(sweep_core.SWEEP_RUST_ENUM_ENV, raising=False)
        off_summary, off_store = _run_legacy("legacy-off")
        monkeypatch.setenv(sweep_core.SWEEP_RUST_ENUM_ENV, "1")
        with capture_logs() as logs:
            on_summary, on_store = _run_legacy("legacy-on")
        assert on_summary == off_summary
        assert on_store == off_store
        reasons = [e["reason"] for e in logs if e["event"] == "sweep_rust_enum.fallback"]
        assert reasons == ["legacy_dedup_path"]
        assert [e for e in logs if e["event"] == "sweep_rust_enum.engaged"] == []

    def test_enum_arm_determinism(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(sweep_core.SWEEP_RUST_ENUM_ENV, "1")
        one = _run_digest(tmp_path, "a")
        sweep_core.reset_enum_self_check_for_tests()
        two = _run_digest(tmp_path, "b")
        assert one[0] == two[0]
        assert one[1] == two[1]
        assert one[2] == two[2]


# ────── 5. F27 seam parity + 7. OFF-arm spy ──────


class TestF27AndOffArmSpy:
    """Empty-window skip-not-error parity + the flag-OFF never-loads pin."""

    def _run_empty(self, tmp_path: Path, tag: str) -> Exception:
        store = ResultsStore(path=tmp_path / f"{tag}.parquet")
        dedup = ComboDedup(OptionDedupIndex(capacity=1_000, fpr_target=1e-6))
        audit = AuditWriter(path=tmp_path / f"{tag}.jsonl")
        with audit, pytest.raises(ValueError, match="bars must be non-empty") as excinfo:
            run_persisted_sweep(
                [], _config(), store=store, dedup=dedup, audit=audit, trial_stats_digest=_DIGEST
            )
        return excinfo.value

    def test_empty_window_skip_not_error_parity_core_never_constructed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The empty-window refusal authority stays PYTHON-side PRE-FFI (§1.3
        # F27 (a)): the same ValueError on both arms, the kernel never loaded.
        monkeypatch.delenv(sweep_core.SWEEP_RUST_ENUM_ENV, raising=False)
        off_exc = self._run_empty(tmp_path, "off")
        calls: list[None] = []
        real_loader = sweep_core._load_kernel_module

        def _spy() -> object | None:
            calls.append(None)
            return real_loader()

        monkeypatch.setattr(sweep_core, "_load_kernel_module", _spy)
        monkeypatch.setenv(sweep_core.SWEEP_RUST_ENUM_ENV, "1")
        on_exc = self._run_empty(tmp_path, "on")
        assert str(on_exc) == str(off_exc)
        assert calls == []  # refused BEFORE any core construction

    def test_flag_off_never_loads_the_kernel(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[None] = []

        def _spy() -> object | None:  # pragma: no cover - must never run
            calls.append(None)
            return None

        monkeypatch.setattr(sweep_core, "_load_kernel_module", _spy)
        monkeypatch.delenv(sweep_core.SWEEP_RUST_ENUM_ENV, raising=False)
        summary, _, _ = _run_digest(tmp_path, "spy-off")
        assert summary.enumerated == _GOLDEN_TALLIES_DIGEST["enumerated"]
        assert calls == []


# ────── 6. Work-bound boundary vectors (exactly-at-cap) ──────


class TestWorkBoundBoundaries:
    """§3 cap-respected pins on the REAL kernel, from the return's own fields."""

    def _core(self, n_bars: int) -> Any:
        return fno_math.ResidentSweepCore(
            [100] * n_bars,
            [101] * n_bars,
            [99] * n_bars,
            [100] * n_bars,
            [False] * n_bars,
            [1] * n_bars,
            [0b1111] * n_bars,
        )

    def test_one_mask_cap_advances_exactly_one(self) -> None:
        core = self._core(4)
        enum_r, _, next_rank, exhausted, _, kept = core.enumerate_prefilter(
            [0, 1, 2, 3], 2, [], 0, 0, 1, 1 << 28
        )
        assert (enum_r, next_rank, exhausted) == (1, 1, False)
        assert kept == [3]

    def test_exactly_at_enumerated_cap(self) -> None:
        core = self._core(4)
        _, _, next_rank, exhausted, _, _ = core.enumerate_prefilter(
            [0, 1, 2, 3], 2, [], 0, 0, 6, 1 << 28
        )
        assert (next_rank, exhausted) == (6, True)  # cap == total → exhausted
        _, _, next_rank, exhausted, _, _ = core.enumerate_prefilter(
            [0, 1, 2, 3], 2, [], 0, 0, 5, 1 << 28
        )
        assert (next_rank, exhausted) == (5, False)  # exactly at cap, one left

    def test_work_cells_exact_boundary_min_hits_blind(self) -> None:
        # 100 bars, cells cap 200: mask 1 → 100 cells (< 200, continue),
        # mask 2 → 200 cells (>= 200, stop) — EXACTLY at the cap, and the
        # charge is min-hits-BLIND (identical at min_hits 0 and 1).
        core = self._core(100)
        for min_hits in (0, 1):
            _, _, next_rank, exhausted, _, _ = core.enumerate_prefilter(
                [0, 1, 2, 3], 2, [], min_hits, 0, 1 << 20, 200
            )
            assert (next_rank, exhausted) == (2, False), f"min_hits={min_hits}"
            # (next_rank − start_rank) × n_bars ≤ max_work_cells + n_bars
            assert next_rank * 100 <= 200 + 100

    def test_zero_mask_cap_still_progresses(self) -> None:
        # The guaranteed-progress law: max_enumerated == 0 advances ONE mask.
        core = self._core(4)
        enum_r, _, next_rank, exhausted, _, _ = core.enumerate_prefilter(
            [0, 1, 2, 3], 2, [], 0, 0, 0, 1 << 28
        )
        assert (enum_r, next_rank, exhausted) == (1, 1, False)

    def test_dual_knob_invariance_matrix(self) -> None:
        # ≥5 (max_enumerated, max_work_cells) shapes → identical kept sets +
        # tallies (§3 dual-knob invariance, with the cap-respected asserts).
        n_bars = 16
        core = self._core(n_bars)
        bits = list(range(8))
        shapes = [(1, 1 << 28), (3, 1 << 28), (1 << 20, 64), (7, 100), (1 << 20, 1 << 28), (0, 1)]
        reference: tuple[int, int, list[int]] | None = None
        for max_enum, max_cells in shapes:
            rank = 0
            enumerated = 0
            pruned = 0
            kept: list[int] = []
            while True:
                enum_r, pruned_r, next_rank, exhausted, _, masks = core.enumerate_prefilter(
                    bits, 3, [(0, 1), (2, 3)], 2, rank, max_enum, max_cells
                )
                assert next_rank - rank <= max(1, max_enum)
                assert (next_rank - rank) * n_bars <= max_cells + n_bars
                enumerated += enum_r
                pruned += pruned_r
                kept.extend(masks)
                if exhausted:
                    break
                assert next_rank > rank
                rank = next_rank
            outcome = (enumerated, pruned, kept)
            if reference is None:
                reference = outcome
            else:
                assert outcome == reference, f"shape=({max_enum}, {max_cells})"
        assert reference is not None
        assert reference[0] == count_combos(3, 8)
