"""Absolute-baseline measurement registry (DEC-BENCH-ABS-BASELINE-001).

The ONE place mapping each :data:`REQUIRED_ENTRY_KEYS` hot-path entry to a
measurable operation built FROM its declared :data:`ENTRY_WORKLOADS`
parameters (the same mapping the workload hash covers), plus the house
best-of-N-MIN timing helper (``perf_counter_ns``, DEC-CANARY-MINTIME-001
idiom — deliberately NOT the pytest-benchmark fixture, whose JSON layout
would couple the gate to a third-party format none of the repo's gates
ever consumed).

Leading underscore → not pytest-collected. Adding hot path #7 is ONE
``AbsOp`` row here + one ``REQUIRED_ENTRY_KEYS`` member + one
``ENTRY_WORKLOADS`` row (CLAUDE.md §11 DYNAMIC); the registry↔keys pin in
``test_absolute_baseline.py`` fails loudly on any drift.

Each builder MIRRORS its verified sibling bench workload:
``bar_lookup`` ← ``test_throughput_canary._make_bar_dict``;
``mask_eval_scalar`` ← ``brutex.engine.combo_mask.evaluate_mask``;
``mask_and_vector_1m`` ← the canary's seeded uint64 mask-AND;
``dedup_bloom_probe`` ← ``test_bloom_o1``'s ``BloomFilter`` probe;
``dedup_lmdb_contains`` ← ``test_lmdb_membership_o1``'s ``__contains__``;
``result_append`` ← ``test_engine_results_store_o1``'s row factory.
"""

from __future__ import annotations

import atexit
import json
import os
import platform
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Final

import numpy as np
from brutex.observability.bench_baseline import (
    ENTRY_WORKLOADS,
    MEASURE_ROUNDS,
    MEASURE_WARMUP_ROUNDS,
    REQUIRED_ENTRY_KEYS,
    STAT,
    UNIT,
    workload_hash,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping


def measure_best_of_min(
    op: Callable[[], object],
    *,
    ops_per_round: int,
    rounds: int = MEASURE_ROUNDS,
    warmup: int = MEASURE_WARMUP_ROUNDS,
) -> tuple[float, list[float], float]:
    """Time *op* — ``(min_ns_per_op, samples_ns_per_op, spread_pct)``.

    The house best-of-N MIN idiom: *warmup* untimed rounds, then *rounds*
    timed rounds via ``time.perf_counter_ns``; every round sample is
    returned so any other statistic stays re-derivable forever. A round
    measuring ``<= 0`` ns raises (the clock-anomaly guard) — the fragment
    is never written from a broken clock.
    """
    if ops_per_round < 1 or rounds < 1:
        raise ValueError(f"ops_per_round={ops_per_round} and rounds={rounds} must be >= 1")
    for _ in range(warmup):
        op()
    samples: list[float] = []
    for _ in range(rounds):
        t0 = time.perf_counter_ns()
        op()
        elapsed = time.perf_counter_ns() - t0
        if elapsed <= 0:
            raise RuntimeError(
                f"clock anomaly: a timed round measured {elapsed} ns — refusing to emit"
            )
        samples.append(elapsed / ops_per_round)
    best = min(samples)
    spread_pct = (max(samples) - best) / best * 100.0
    return best, samples, spread_pct


@dataclass(frozen=True, slots=True)
class AbsOp:
    """One registry row: entry id + a builder from workload params to an op."""

    entry_id: str
    build: Callable[[Mapping[str, int | str]], Callable[[], object]]


def _int_param(params: Mapping[str, int | str], key: str) -> int:
    """A required integer workload parameter."""
    return int(params[key])


def _mask_from_bits(params: Mapping[str, int | str]) -> int:
    """Fold the declared ``mask_bits`` CSV into a required-mask integer."""
    return sum(1 << int(bit) for bit in str(params["mask_bits"]).split(","))


def _build_bar_lookup(params: Mapping[str, int | str]) -> Callable[[], object]:
    """Seeded dict probe — the canary ``_make_bar_dict`` idiom."""
    rng = np.random.default_rng(_int_param(params, "seed"))
    symbols = ("NIFTY", "BANKNIFTY", "SENSEX", "INDIAVIX")
    base_ts = 1_700_000_000
    bars: dict[tuple[str, int], tuple[int, int, int, int]] = {}
    for i in range(_int_param(params, "dict_size")):
        o = int(rng.integers(20_000, 30_000))
        bars[(symbols[i % 4], base_ts + i)] = (o, o + 10, o - 10, o)
    keys = list(bars)
    probes = [
        keys[int(i)] for i in rng.integers(0, len(keys), size=_int_param(params, "ops_per_round"))
    ]

    def op() -> object:
        value: tuple[int, int, int, int] | None = None
        for key in probes:
            value = bars[key]
        return value

    return op


def _build_mask_eval_scalar(params: Mapping[str, int | str]) -> Callable[[], object]:
    """Scalar u128 mask evaluation — ``brutex.engine.combo_mask.evaluate_mask``."""
    from brutex.engine.combo_mask import evaluate_mask

    mask = _mask_from_bits(params)
    bar_bits = mask | ((1 << 60) - 1)  # dense truth-vector: every condition fired
    loops = range(_int_param(params, "ops_per_round"))

    def op() -> object:
        hit = False
        for _ in loops:
            hit = evaluate_mask(bar_bits, mask)
        return hit

    return op


def _build_mask_and_vector(params: Mapping[str, int | str]) -> Callable[[], object]:
    """Vectorised mask AND — the throughput canary's seeded uint64 op."""
    rng = np.random.default_rng(_int_param(params, "seed"))
    arr = rng.integers(0, 2**63, size=_int_param(params, "array_len"), dtype=np.uint64)
    mask = np.uint64(_mask_from_bits(params))

    def op() -> object:
        return (arr & mask) == mask

    return op


def _build_bloom_probe(params: Mapping[str, int | str]) -> Callable[[], object]:
    """Bloom membership probe — the ``test_bloom_o1`` idiom, 50/50 present/absent."""
    from brutex.orchestrator.bloom import BloomFilter

    fill = _int_param(params, "fill")
    bloom = BloomFilter(capacity=_int_param(params, "capacity"))
    for i in range(fill):
        bloom.add(i.to_bytes(32, "big"))
    rng = np.random.default_rng(_int_param(params, "seed"))
    probes = [
        (int(i) if j % 2 == 0 else fill + int(i)).to_bytes(32, "big")
        for j, i in enumerate(rng.integers(0, fill, size=_int_param(params, "ops_per_round")))
    ]

    def op() -> object:
        hit = False
        for probe in probes:
            hit = bloom.might_contain(probe)
        return hit

    return op


def _build_lmdb_contains(params: Mapping[str, int | str]) -> Callable[[], object]:
    """LMDB exact-membership probe — the ``test_lmdb_membership_o1`` idiom."""
    from brutex.data.lmdb_membership import LmdbMembership

    workdir = Path(tempfile.mkdtemp(prefix="brutex-abs-bench-lmdb-"))
    # The bench runner is a PERSISTENT self-hosted machine — never leak the
    # multi-MB LMDB workdir into $TMPDIR (cleaned at process exit; the env
    # must outlive the returned op, so no context manager here).
    atexit.register(shutil.rmtree, workdir, ignore_errors=True)
    store = LmdbMembership.open(workdir, map_size_mb=_int_param(params, "map_size_mb"))
    fill = _int_param(params, "fill")
    key_bytes = _int_param(params, "key_bytes")
    store.add_many(i.to_bytes(key_bytes, "big") for i in range(fill))
    rng = np.random.default_rng(_int_param(params, "seed"))
    probes = [
        (int(i) if j % 2 == 0 else fill + int(i)).to_bytes(key_bytes, "big")
        for j, i in enumerate(rng.integers(0, fill, size=_int_param(params, "ops_per_round")))
    ]

    def op() -> object:
        hit = False
        for probe in probes:
            hit = probe in store
        return hit

    return op


def _build_result_append(params: Mapping[str, int | str]) -> Callable[[], object]:
    """Result-row append — a FRESH in-memory store per round (no accumulation)."""
    from brutex.engine.results_store import ResultRow, ResultsStore

    rows_per_round = _int_param(params, "rows_per_round")
    seed = _int_param(params, "seed")

    def _row(i: int) -> ResultRow:
        return ResultRow(
            run_id=f"{seed + i:064x}",
            instrument="NIFTY",
            tf="5m",
            direction="bull",
            mode="A",
            mask_bits=(seed + i) % (1 << 52),
            net_pnl_paisa=i,
            gross_profit_paisa=i,
            gross_loss_paisa=0,
            max_drawdown_paisa=0,
            expectancy_paisa=0,
            total_trades=1,
            winning_trades=1,
            losing_trades=0,
            sharpe_ratio=float(i % 7),
            sortino_ratio=0.0,
            calmar_ratio=0.0,
            profit_factor=1.0,
            win_rate=1.0,
            max_drawdown_pct=0.0,
            avg_entry_price=Decimal("1.00"),
            avg_exit_price=Decimal("2.00"),
        )

    rows = [_row(i) for i in range(rows_per_round)]

    def op() -> object:
        store = ResultsStore(path=Path("unused.parquet"))
        appended = False
        for row in rows:
            appended = store.append(row)
        return appended

    return op


#: The closed measurement registry — exactly one row per required entry key.
REGISTRY: Final[tuple[AbsOp, ...]] = (
    AbsOp("bar_lookup", _build_bar_lookup),
    AbsOp("mask_eval_scalar", _build_mask_eval_scalar),
    AbsOp("mask_and_vector_1m", _build_mask_and_vector),
    AbsOp("dedup_bloom_probe", _build_bloom_probe),
    AbsOp("dedup_lmdb_contains", _build_lmdb_contains),
    AbsOp("result_append", _build_result_append),
)


def registry_ids() -> frozenset[str]:
    """The registry's entry-id set (pinned == ``REQUIRED_ENTRY_KEYS``)."""
    return frozenset(op.entry_id for op in REGISTRY)


def _wheel_importable(name: str) -> bool:
    """Whether a required Rust wheel imports on this interpreter."""
    try:
        __import__(name)
    except ImportError:
        return False
    return True


def _runtime_context() -> dict[str, str]:
    """The soft-drift context stamped into every fragment."""
    context = {
        "python": platform.python_version(),
        "os_version": platform.mac_ver()[0] or platform.release(),
    }
    try:
        import numpy as _np

        context["numpy"] = _np.__version__
    except ImportError:  # pragma: no cover — numpy is a hard dep of this module
        pass
    return context


def write_fragment(
    out_dir: Path,
    entry_id: str,
    *,
    value: float,
    samples: list[float],
    spread_pct: float,
    ops_per_round: int,
    rounds: int,
) -> Path:
    """Write one measured fragment to ``<out_dir>/<entry_id>.json``."""
    doc = {
        "entry": entry_id,
        "unit": UNIT,
        "stat": STAT,
        "value_ns_per_op": value,
        "samples_ns_per_op": samples,
        "spread_pct": spread_pct,
        "ops_per_round": ops_per_round,
        "rounds": rounds,
        "workload_hash": workload_hash(entry_id),
        "wheels": {
            "fno_math": _wheel_importable("fno_math"),
            "trading_core": _wheel_importable("trading_core"),
        },
        "context": _runtime_context(),
    }
    out = out_dir / f"{entry_id}.json"
    out.write_text(json.dumps(doc, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return out


def write_fingerprint(out_dir: Path) -> Path:
    """Write ``<out_dir>/fingerprint.json`` (runtime key + raw fingerprint)."""
    from brutex.config.hardware_envelope import hardware_fingerprint, hardware_key

    fp = hardware_fingerprint()
    doc = {
        "hardware_key": hardware_key(),
        "fingerprint": {
            "model_slug": fp.model_slug,
            "arch": fp.arch,
            "os_family": fp.os_family,
            "physical_cores": fp.physical_cores,
            "performance_cores": fp.performance_cores,
            "efficiency_cores": fp.efficiency_cores,
            "total_ram_gib": fp.total_ram_gib,
        },
        "python": platform.python_version(),
        "commit": os.environ.get("GITHUB_SHA", "unknown"),
        "runner_name": os.environ.get("RUNNER_NAME", sys.platform),
    }
    out = out_dir / "fingerprint.json"
    out.write_text(json.dumps(doc, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return out


def _registry_sanity() -> None:
    """Import-time drift guard: one registry row per required key, no strays."""
    ids = registry_ids()
    if ids != REQUIRED_ENTRY_KEYS or len(REGISTRY) != len(REQUIRED_ENTRY_KEYS):
        raise RuntimeError(
            f"registry ids {sorted(ids)} != REQUIRED_ENTRY_KEYS {sorted(REQUIRED_ENTRY_KEYS)}"
        )
    declared = set(ENTRY_WORKLOADS)
    if declared != REQUIRED_ENTRY_KEYS:
        raise RuntimeError(f"ENTRY_WORKLOADS keys {sorted(declared)} != REQUIRED_ENTRY_KEYS")


_registry_sanity()
