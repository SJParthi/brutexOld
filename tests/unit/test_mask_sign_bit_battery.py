"""The u64 SIGN-BIT audit battery (vocab-66 reshape PR evidence).

Bit 63 (``narrow_cpr_day``) is now a VALID, IMPLEMENTED mask bit — the first
time a production mask can set the u64 sign bit / exceed ``2**53``. This
battery proves a bit-63-set mask round-trips EXACTLY through every persistence
+ identity surface:

* :class:`~brutex.engine.results_store.ResultsStore` write → load (the
  two-limb ``mask_bits_lo``/``mask_bits_hi`` offset-limb Parquet path);
* :func:`~brutex.engine.sweep_cache.combo_digest` determinism (same digest
  in-process twice AND across a fresh interpreter — the dedup key must never
  fork on sign-bit handling);
* the top100 WRITER's ``mask_hex`` → the oneclick READER;
* the cascade manifest's winner ints (+ the additive ``winners_hex`` twin);
* :class:`~brutex.strategies.spec.StrategySpec` construction +
  ``canonical_bytes`` stability/sensitivity;
* the ``%032x`` run-identity formatting width;
* the sweep HOT-PATH kernels — the Stage-2 superset count
  (:func:`~brutex.engine.combo_mask.count_supermasks` → the u64 Rust kernel
  ``fno_math.count_supermasks_u64s`` + the resident
  :class:`~brutex.engine.combo_mask.ResidentBarBuffer`) and the Stage-2
  trade-sim kernel (``fno_math.ResidentSweepSim``) — each verified against a
  pure-Python reference over bit-63-set bars, so a future accidental i64 FFI
  signature can never truncate/overflow the sign bit silently;
* the L1 Bloom + L2 LMDB dedup membership — a bit-63 mask's combo digest
  driven through the REAL :class:`~brutex.engine.sweep_cache.SweepCache`
  (record → probe true; the sign-bit-cleared mask probes false; a re-open
  reloads the persisted LMDB exact membership);
* the trivial import law: ``IMPLEMENTED_MASK > 2**63`` breaks nothing at
  import anywhere (it now contains bit 63).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from brutex.engine import cascade_wire as cw
from brutex.engine.sweep_cache import combo_digest
from brutex.strategies.fib_definitions import Direction
from brutex.strategies.spec import StrategySpec
from brutex.vocabulary.bits import IMPLEMENTED_MASK, TOTAL_CONDITIONS

#: The battery mask: the u64 sign bit + a CPR bit + two low bits.
BIT63_MASK = (1 << 63) | (1 << 60) | 0b101

_RID = f"{1:064x}"


def test_battery_mask_is_valid_and_past_the_double_ceiling() -> None:
    assert BIT63_MASK & ~IMPLEMENTED_MASK == 0  # every bit implemented
    assert BIT63_MASK > 2**63  # the u64 sign bit is set
    assert BIT63_MASK > 2**53  # an IEEE double cannot hold it exactly


def test_implemented_mask_exceeds_the_sign_bit_at_import() -> None:
    # Trivial import assertion: the module graph already imported cleanly to
    # get here; the mask itself now spans past bit 63's value.
    assert IMPLEMENTED_MASK > 2**63
    assert (IMPLEMENTED_MASK >> 63) & 1 == 1
    # 69 -> 74 at DEC-FIB-TEMPLATE-EXPRESS-001 (MIGRATION.md #1935): the
    # IMPLEMENTED mask occupies bits 64-73 of the second u64 limb (SuperTrend
    # 64/65 live + the gap trio 66-68 + the express bits 69-73).
    assert TOTAL_CONDITIONS == 74
    assert IMPLEMENTED_MASK >> 64 == 0b1111111111


def test_results_store_two_limb_round_trip(tmp_path: Path) -> None:
    polars = pytest.importorskip("polars")
    from brutex.engine.results_store import ResultsStore

    from tests.fakes.wp5_depth_fixtures import make_result_row

    row = make_result_row(_RID, mask_bits=BIT63_MASK)
    store = ResultsStore(path=tmp_path / "battery.parquet")
    store.append(row)
    store.flush()
    loaded = ResultsStore.load(tmp_path / "battery.parquet")
    (loaded_row,) = loaded.rows()
    assert loaded_row.mask_bits == BIT63_MASK
    # The physical limbs: lo carries the sign-bit word as an OFFSET int64
    # (never a float/py-int coercion), hi carries the empty upper word.
    frame = polars.read_parquet(tmp_path / "battery.parquet")
    lo = int(frame["mask_bits_lo"][0])
    hi = int(frame["mask_bits_hi"][0])
    assert frame["mask_bits_lo"].dtype == polars.Int64
    assert frame["mask_bits_hi"].dtype == polars.Int64
    # The store's offset-limb law (`_encode_limb`/`_decode` — each u64 word is
    # persisted as signed Int64 via a 2**63 offset): reassemble EXACTLY.
    word_lo = lo + (1 << 63)
    word_hi = hi + (1 << 63)
    assert word_lo | (word_hi << 64) == BIT63_MASK


def test_combo_digest_is_deterministic_in_process_and_across_interpreters() -> None:
    kwargs = {
        "data_snapshot_hash": "s" * 64,
        "indicator_version": "iv-battery",
        "code_commit_sha": "c" * 40,
    }
    first = combo_digest(BIT63_MASK, "NIFTY", "5m", "bull", {"sl": 80}, **kwargs)
    second = combo_digest(BIT63_MASK, "NIFTY", "5m", "bull", {"sl": 80}, **kwargs)
    assert first == second
    assert len(first) == 32
    # A fresh interpreter (a second "run"/process) must mint the SAME digest —
    # the dedup key can never fork on sign-bit serialization.
    script = (
        "from brutex.engine.sweep_cache import combo_digest\n"
        f"d = combo_digest({BIT63_MASK}, 'NIFTY', '5m', 'bull', {{'sl': 80}}, "
        f"data_snapshot_hash={'s' * 64!r}, indicator_version='iv-battery', "
        f"code_commit_sha={'c' * 40!r})\n"
        "print(d.hex())\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=True, timeout=120
    )
    assert out.stdout.strip() == first.hex()
    # Sensitivity: clearing the sign bit MUST move the digest (anti-vacuity).
    assert combo_digest(BIT63_MASK ^ (1 << 63), "NIFTY", "5m", "bull", {"sl": 80}, **kwargs) != (
        first
    )


def test_top100_writer_mask_hex_reaches_the_reader(tmp_path: Path) -> None:
    pytest.importorskip("polars")
    from brutex.dashboard import oneclick_artifacts as oc
    from brutex.engine.top100_artifact import PoolCounts, build_top100, write_top100
    from brutex.engine.twin_detect import TwinFamilies

    from tests.fakes.wp4_twin_fixtures import fingerprint_seed
    from tests.fakes.wp5_depth_fixtures import make_candidate

    candidate = make_candidate(
        1, fingerprint=fingerprint_seed(1), row_overrides={"mask_bits": BIT63_MASK}
    )
    artifact = build_top100(
        [candidate],
        run_id="grid" + "0" * 60,
        preset="min_dd_first",
        families=TwinFamilies(family_by_run_id={}, members_by_family={}),
        counts=PoolCounts(stores=1, excluded_no_aggs=0, zero_trade_excluded=0, parity_excluded=0),
        data_snapshot_hashes=("snap0001",),
        baselines={"NIFTY": "baseline-NIFTY.json"},
    )
    runs_dir = tmp_path / "sweep_runs"
    path = write_top100(runs_dir, artifact)  # writes <runs_dir>/<run_id>/top100.json
    run_dir = runs_dir / artifact.run_id
    written = json.loads(path.read_text(encoding="utf-8"))
    entry = written["entries"][0]
    assert entry["mask_hex"] == f"{BIT63_MASK:032x}"
    # …and the display reader recovers the EXACT mask from the hex.
    result = oc.load_top100(run_dir)
    assert result.status == "ok"
    assert int(result.rows[0]["mask_hex"], 16) == BIT63_MASK


def test_cascade_manifest_winner_round_trip() -> None:
    spec = cw.CascadeTupleSpec(
        instrument="NIFTY",
        tf_label="1minute",
        tf_minutes=1,
        direction_label="bull",
        sl_paisa=None,
        tp_paisa=None,
        prior_n=3,
        proximity_bps=None,
        start_equity_paisa=10_000_000,
        winners=((_RID, BIT63_MASK),),
    )
    manifest = cw.CascadeManifest(
        version=cw.MANIFEST_VERSION,
        sweep_id="a" * 64,
        cascade_config=cw.cascade_config_for_sweep("a" * 64),
        bars_context=cw.BarsContext(
            exchange="NSE", segment="INDEX", start="2021-01-01", end="2021-02-01"
        ),
        specs=(spec,),
    )
    data = cw.manifest_to_json(manifest)
    payload = json.loads(data)
    assert payload["specs"][0]["winners"] == [[_RID, BIT63_MASK]]  # exact JSON int
    assert payload["specs"][0]["winners_hex"] == [[_RID, f"{BIT63_MASK:032x}"]]
    parsed = cw.manifest_from_json(data)
    assert parsed.specs[0].winners == ((_RID, BIT63_MASK),)


def test_strategy_spec_round_trip_and_canonical_bytes_stability() -> None:
    pytest.importorskip("trading_core")

    def _spec() -> StrategySpec:
        return StrategySpec(
            condition_mask=BIT63_MASK,
            direction=Direction.LONG,
            pivot_proximity_bps=50,
            pdh_pdl_proximity_bps=10,
            fib_proximity_bps=25,
            prior_n=3,
            symbol_id=1,
        )

    spec = _spec()
    assert spec.condition_mask == BIT63_MASK
    # Stability: two identical constructions → identical canonical bytes.
    assert spec.canonical_bytes() == _spec().canonical_bytes()
    # Sensitivity: the sign bit is ENCODED (clearing it moves the bytes).
    from dataclasses import replace

    cleared = replace(spec, condition_mask=BIT63_MASK ^ (1 << 63))
    assert cleared.canonical_bytes() != spec.canonical_bytes()


def test_mask_hex_formatting_width_is_32() -> None:
    rendered = f"{BIT63_MASK:032x}"
    assert len(rendered) == 32  # u128 width — no sign, no truncation
    assert rendered == "00000000000000009000000000000005"
    assert int(rendered, 16) == BIT63_MASK


# ─────────────────────────────────────────────────────────────────
# The sweep HOT-PATH kernels (Stage-2 count + trade-sim)
# ─────────────────────────────────────────────────────────────────

#: Bit-63-set bar_bits window for the kernel legs: the battery mask itself,
#: two partial sign-bit supersets, a sign-bit-free superset of the low bits,
#: and the empty bar. Every value fits u64 (bit 63 IS the top u64 bit), so
#: the production router takes the Rust hot kernel — the surface under test.
_KERNEL_BARS = [BIT63_MASK, (1 << 63) | 0b101, 1 << 63, 0b101, 0]
_KERNEL_MASKS = [BIT63_MASK, 1 << 63, 0b101, 0]


def _scalar_superset_counts(bar_bits: list[int], masks: list[int]) -> list[int]:
    """Pure-Python (big-int) superset-count reference — no numpy, no Rust."""
    return [sum(1 for b in bar_bits if (b & m) == m) for m in masks]


def test_sweep_hot_kernel_count_supermasks_sign_bit() -> None:
    """The Stage-2 count kernel serves a bit-63-set mask/bars EXACTLY.

    All inputs fit u64 (bit 63 is the TOP u64 bit — no bit >= 64), so
    ``count_supermasks`` routes to the Rust hot kernel
    ``fno_math.count_supermasks_u64s``, NOT the two-limb numpy reference:
    this is the sweep hot path itself. A future accidental i64 FFI signature
    would overflow/misparse ``1 << 63`` here instead of shipping silently.
    """
    fno_math = pytest.importorskip("fno_math")
    from brutex.engine.combo_mask import U64_MASK, count_supermasks

    assert all(0 <= v <= U64_MASK for v in [*_KERNEL_BARS, *_KERNEL_MASKS])
    expected = _scalar_superset_counts(_KERNEL_BARS, _KERNEL_MASKS)
    # Hand-derived: only the battery bar ⊇ the battery mask; three bars carry
    # bit 63; three bars ⊇ 0b101; every bar ⊇ 0.
    assert expected == [1, 3, 3, 5]
    # The production router (takes the u64 Rust kernel for these inputs).
    assert count_supermasks(_KERNEL_BARS, _KERNEL_MASKS) == expected
    # The raw kernel itself, no router in between.
    assert list(fno_math.count_supermasks_u64s(_KERNEL_BARS, _KERNEL_MASKS)) == expected


def test_sweep_hot_kernel_resident_bar_buffer_sign_bit() -> None:
    """The resident Rust buffer (``fno_math.ResidentBars``) agrees on bit-63 bars."""
    pytest.importorskip("fno_math")
    from brutex.engine.combo_mask import ResidentBarBuffer

    rb = ResidentBarBuffer(_KERNEL_BARS)
    # All bars fit u64 → the RUST resident kernel backs the buffer (not the
    # numpy fallback); this pins the sign-bit word crossed the FFI boundary.
    assert rb._rust is not None
    assert rb.count_supermasks(_KERNEL_MASKS) == _scalar_superset_counts(
        _KERNEL_BARS, _KERNEL_MASKS
    )
    assert rb.count_supermask(BIT63_MASK) == 1


def test_sweep_hot_kernel_resident_sweep_sim_sign_bit_mask() -> None:
    """The u64 trade-sim kernel fires a bit-63-set mask on a bit-63-set bar.

    The flat-window construction mirrors
    ``tests/unit/test_fno_math_resident_sweep_sim_u128.py``: open/close
    10_000, high +150 / low −150, one session, no forced bars. The battery
    mask arms on bar 0 (the only superset); under main's worst-case-only fill
    law (DEC-FILL-WORST-CASE-ONLY-001, adopted at the 2026-07-19
    merge-reconcile — the expectation here flipped from the retired
    optimistic open-fill (0, 150, 150, 2, 1, 2)) the entry fills at the
    adverse high[1] = 10_150 and the end-of-data exit lands at the last
    bar's adverse low[2] = 9_850 → pnl −300, mfe 0, mae 300, holding 2 —
    byte-matching the u128 sibling's pinned tuple.
    """
    fno_math = pytest.importorskip("fno_math")
    bar_bits = [BIT63_MASK, 0, 0]
    n = len(bar_bits)
    sim = fno_math.ResidentSweepSim(
        [10_000] * n,
        [10_150] * n,
        [9_850] * n,
        [10_000] * n,
        [False] * n,
        [0] * n,
        bar_bits,
    )
    # Pure-Python reference: exactly one bar is a superset of the mask.
    assert _scalar_superset_counts(bar_bits, [BIT63_MASK]) == [1]
    assert sim.count_trades([BIT63_MASK], True, None, None) == [1]
    assert sim.simulate_masks([BIT63_MASK], True, None, None) == [[(-300, 0, 300, 2, 1, 2)]]
    # Anti-vacuity: a high mask bit NO bar carries (bit 62) fires nothing —
    # the kernel genuinely evaluated the top word, it did not match-all.
    assert sim.count_trades([1 << 62], True, None, None) == [0]


# ─────────────────────────────────────────────────────────────────
# The Bloom + LMDB dedup membership (SweepCache → LmdbMembership)
# ─────────────────────────────────────────────────────────────────


def test_sweep_cache_lmdb_membership_sign_bit_round_trip(tmp_path: Path) -> None:
    """A bit-63 mask's combo digest round-trips the REAL Bloom + LMDB dedup.

    Drives :class:`~brutex.engine.sweep_cache.SweepCache` (the production
    dedup tier backed by :class:`~brutex.data.lmdb_membership.LmdbMembership`)
    with the battery mask: record → probe true; a DIFFERENT mask (the sign
    bit cleared — same low word) probes false, so the persisted key genuinely
    encodes bit 63; and a fresh re-open reloads the durable LMDB exact
    membership (the resume-dedup guarantee) with the same true/false split.
    """
    pytest.importorskip("lmdb")
    from brutex.engine.sweep_cache import SweepCache

    def seen(cache: SweepCache, mask: int) -> bool:
        return cache.seen(
            mask,
            "NIFTY",
            "5m",
            "bull",
            {"sl": 80},
            data_snapshot_hash="s" * 64,
            indicator_version="iv-battery",
            code_commit_sha="c" * 40,
        )

    def record(cache: SweepCache, mask: int) -> bool:
        return cache.record(
            mask,
            "NIFTY",
            "5m",
            "bull",
            {"sl": 80},
            data_snapshot_hash="s" * 64,
            indicator_version="iv-battery",
            code_commit_sha="c" * 40,
        )

    cleared = BIT63_MASK ^ (1 << 63)
    with SweepCache.open(tmp_path / "cache", bloom_capacity=1_000) as cache:
        assert seen(cache, BIT63_MASK) is False
        assert record(cache, BIT63_MASK) is True
        assert seen(cache, BIT63_MASK) is True
        # Idempotent re-record: already a member, never double-admitted.
        assert record(cache, BIT63_MASK) is False
        # The sign-bit-cleared twin is a DIFFERENT combo — probes false.
        assert seen(cache, cleared) is False
    # Durability: a fresh open reloads the LMDB exact store (and rebuilds the
    # in-RAM Bloom from it), preserving the exact true/false split.
    with SweepCache.open(tmp_path / "cache", bloom_capacity=1_000) as reopened:
        assert seen(reopened, BIT63_MASK) is True
        assert seen(reopened, cleared) is False
