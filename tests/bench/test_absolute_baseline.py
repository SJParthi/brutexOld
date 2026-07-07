"""Absolute hot-path baseline measurement + always-on machinery pins.

DEC-BENCH-ABS-BASELINE-001. The gated measurement test mirrors
``test_throughput_absolute.py`` (``skip_unless_m4pro`` — active ONLY under
``BRUTEX_M4PRO_ABS=1`` on the self-hosted M4 Pro runner); everywhere else
it skips while the ALWAYS-RUN machinery tests below keep the registry,
fragment writer, env gate, and workload hashes pinned on shared CI.

Skip-honesty: a skipped measurement run can never masquerade as a pass —
the comparator's fragment-set equality law STRUCTURAL-fails a compare fed
fewer than the registry's entries, and the registry↔REQUIRED_ENTRY_KEYS
pin here fails BEFORE a renamed hot path could vanish from the gate.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest
from brutex.observability.bench_baseline import (
    ENTRY_WORKLOADS,
    MEASURE_ROUNDS,
    REQUIRED_ENTRY_KEYS,
    parse_fragment,
    workload_hash,
)

from tests.bench._abs_baseline_registry import (
    REGISTRY,
    measure_best_of_min,
    registry_ids,
    write_fingerprint,
    write_fragment,
)
from tests.bench.test_throughput_absolute import _m4pro_gate_active, skip_unless_m4pro

if TYPE_CHECKING:
    from collections.abc import Mapping

#: Where the gated measurement run writes its fragments (set by the
#: bench-m4pro workflow; missing while the gate is ACTIVE → loud fail).
_ENV_OUT_DIR: Final[str] = "BRUTEX_ABS_BENCH_OUT"

#: Tiny injected workloads so every builder executes on shared CI in
#: milliseconds (measurement-free — no perf number is asserted).
_TINY_WORKLOADS: Final[Mapping[str, Mapping[str, int | str]]] = {
    "bar_lookup": {"dict_size": 64, "ops_per_round": 32, "seed": 42},
    "mask_eval_scalar": {"ops_per_round": 32, "mask_bits": "3,17,33,47"},
    "mask_and_vector_1m": {
        "array_len": 64,
        "ops_per_round": 64,
        "seed": 42,
        "mask_bits": "3,17,33,47",
    },
    "dedup_bloom_probe": {"capacity": 1_000, "fill": 64, "ops_per_round": 32, "seed": 42},
    "dedup_lmdb_contains": {
        "fill": 64,
        "map_size_mb": 4,
        "key_bytes": 16,
        "ops_per_round": 32,
        "seed": 42,
    },
    "result_append": {"rows_per_round": 8, "ops_per_round": 8, "seed": 42},
}

#: Golden pin of the CURRENT declared-workload hashes. Changing
#: ENTRY_WORKLOADS forces a conscious edit HERE too — doubling the S11
#: forcing function (the same PR must also remove the affected committed
#: baseline entries with a ledgered note).
_WORKLOAD_HASH_GOLDEN: Final[Mapping[str, str]] = {
    "bar_lookup": "sha256:1b40547a757f0c6fd6204d49e15ffbb75f0fcaec73ee9fc62840297ada762192",
    "mask_eval_scalar": "sha256:e38dab542fb922779ab4c82b2722a78f3809c79b7f1c981acfb9772f4239d45b",
    "mask_and_vector_1m": "sha256:477af8c19474701a396ef1e32ce6bea4f5f6073de6073e5fcb646c8c927e4c69",
    "dedup_bloom_probe": "sha256:b9f99dc1bc3a88ae1a3f70d8717853db2449d2f0aa103eb1762b0aea708b9b04",
    "dedup_lmdb_contains": "sha256:e56d757b448160628cf49ce43b5add918573c25fde03e0be016a7fa2e16be9c0",
    "result_append": "sha256:2a8749548c21e9b07699c48eb66accd2872651d6efc2abd8c6cd6a948be261c7",
}


class TestMeasureAndEmit:
    """The gated measurement run (M4 Pro runner only)."""

    @skip_unless_m4pro
    @pytest.mark.bench
    def test_measure_and_emit_absolute_hot_paths(self) -> None:
        """Measure every registry op at FULL workload size and emit fragments.

        Requires ``BRUTEX_ABS_BENCH_OUT`` while the gate is active — a
        missing output dir is a LOUD ``pytest.fail``, never a silent no-op.
        Asserts positive/finite sanity ONLY (the compare gate owns the
        verdict); prints each measured ns/op + derived ops/s so the number
        surfaces in the CI log (DEC-M4PRO-BENCH-SURFACE-001, ``-s``).
        """
        raw_out = os.environ.get(_ENV_OUT_DIR, "").strip()
        if not raw_out:
            pytest.fail(
                f"{_ENV_OUT_DIR} is not set while the absolute gate is active — "
                "the measurement run has nowhere to write fragments (workflow bug)"
            )
        out_dir = Path(raw_out)
        out_dir.mkdir(parents=True, exist_ok=True)
        for abs_op in REGISTRY:
            params = ENTRY_WORKLOADS[abs_op.entry_id]
            op = abs_op.build(params)
            ops_per_round = int(params["ops_per_round"])
            value, samples, spread_pct = measure_best_of_min(op, ops_per_round=ops_per_round)
            assert value > 0.0
            assert all(s > 0.0 for s in samples)
            print(
                f"\nabs-bench {abs_op.entry_id}: {value:.3f} ns/op "
                f"({1e9 / value:,.0f} ops/s, spread {spread_pct:.2f}%)"
            )
            write_fragment(
                out_dir,
                abs_op.entry_id,
                value=value,
                samples=samples,
                spread_pct=spread_pct,
                ops_per_round=ops_per_round,
                rounds=len(samples),
            )
        write_fingerprint(out_dir)


class TestAbsBaselineMachinery:
    """Always-run pins — execute on shared CI's ``bench`` job (no skip)."""

    def test_registry_ids_match_required_entry_keys(self) -> None:
        """The drift pin: a renamed/removed hot path fails HERE first."""
        assert registry_ids() == REQUIRED_ENTRY_KEYS
        assert len(REGISTRY) == len(REQUIRED_ENTRY_KEYS)

    def test_entry_workloads_cover_every_required_key(self) -> None:
        """Every registry row has a declared (hashed) workload."""
        assert set(ENTRY_WORKLOADS) == REQUIRED_ENTRY_KEYS

    @pytest.mark.parametrize("abs_op", REGISTRY, ids=lambda op: str(op.entry_id))
    def test_each_builder_runs_at_tiny_size(self, abs_op: object) -> None:
        """Every builder builds + executes once at tiny injected size."""
        from tests.bench._abs_baseline_registry import AbsOp

        assert isinstance(abs_op, AbsOp)
        if abs_op.entry_id == "dedup_lmdb_contains":
            pytest.importorskip("lmdb")
        op = abs_op.build(_TINY_WORKLOADS[abs_op.entry_id])
        result = op()
        assert result is not None

    def test_measure_best_of_min_shape_and_positivity(self) -> None:
        """The timing helper returns min/samples/spread with sane values."""
        value, samples, spread = measure_best_of_min(
            lambda: sum(range(100)), ops_per_round=100, rounds=3, warmup=1
        )
        assert value == min(samples)
        assert len(samples) == 3
        assert value > 0.0
        assert spread >= 0.0

    def test_measure_best_of_min_rejects_bad_params(self) -> None:
        """ops_per_round/rounds < 1 refuse loudly."""
        with pytest.raises(ValueError, match="must be >= 1"):
            measure_best_of_min(lambda: None, ops_per_round=0)
        with pytest.raises(ValueError, match="must be >= 1"):
            measure_best_of_min(lambda: None, ops_per_round=1, rounds=0)

    def test_fragment_writer_roundtrips(self, tmp_path: Path) -> None:
        """write_fragment output parses back through the ONE schema law."""
        path = write_fragment(
            tmp_path,
            "bar_lookup",
            value=55.0,
            samples=[55.0, 56.1, 57.0],
            spread_pct=3.6,
            ops_per_round=10_000,
            rounds=3,
        )
        frag = parse_fragment(path.read_text(encoding="utf-8"), entry_hint="bar_lookup")
        assert frag.value_ns_per_op == 55.0
        assert frag.workload_hash == workload_hash("bar_lookup")
        assert set(frag.wheels) == {"fno_math", "trading_core"}
        assert "python" in frag.context

    def test_fingerprint_writer_emits_runtime_key(self, tmp_path: Path) -> None:
        """write_fingerprint emits the runtime hardware key + raw fields."""
        import json

        path = write_fingerprint(tmp_path)
        doc = json.loads(path.read_text(encoding="utf-8"))
        assert set(doc) == {"hardware_key", "fingerprint", "python", "commit", "runner_name"}
        assert doc["hardware_key"]
        assert doc["fingerprint"]["physical_cores"] >= 1

    @pytest.mark.parametrize("val", ["", "0", "false", "yes", "true", " 1 "])
    def test_env_gate_decodes_literal_one_only(
        self, monkeypatch: pytest.MonkeyPatch, val: str
    ) -> None:
        """Mirrors TestGateMachinery: only literal ``"1"`` (stripped) arms."""
        monkeypatch.setenv("BRUTEX_M4PRO_ABS", val)
        assert _m4pro_gate_active() is (val.strip() == "1")

    def test_workload_hash_stable_golden(self) -> None:
        """Pins the 6 current workload hashes as literals (S11 double lock)."""
        assert {entry: workload_hash(entry) for entry in sorted(REQUIRED_ENTRY_KEYS)} == dict(
            _WORKLOAD_HASH_GOLDEN
        )

    def test_measure_rounds_at_least_three(self) -> None:
        """A capture (3 runs x MEASURE_ROUNDS) always clears the schema's
        sample floor."""
        assert MEASURE_ROUNDS >= 3
