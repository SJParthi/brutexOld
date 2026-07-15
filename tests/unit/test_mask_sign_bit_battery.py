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
    assert TOTAL_CONDITIONS == 66


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
