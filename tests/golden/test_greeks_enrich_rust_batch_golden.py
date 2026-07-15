"""ON-arm frozen-fixture + spawn-parallel goldens (`DEC-GREEKS-RUST-BATCH-001`).

Two anti-vacuous proofs that the Rust columnar batch arm is BYTE-IDENTICAL to
the frozen Python path:

1. **Frozen fixture, flag ON** — the EXISTING deterministic golden scenario
   (``generate_greeks_enrich_golden.py``) replayed with
   ``BRUTEX_GREEKS_RUST_BATCH=1`` must store bytes equal to the frozen
   ``greeks_enrich_golden.json`` EXACTLY — the fixture is read-only here,
   NEVER regenerated (the OFF-arm proof is the unmodified
   ``test_greeks_enrich_golden.py`` running beside this file). The engaged
   INFO is pinned so a stale wheel silently serving the Python arm cannot
   vacuously pass (the ``rust_metrics`` engaged-golden precedent).
2. **Spawn-parallel, flag ON** — the SAME multi-underlying scenario as the
   OFF-arm parallel golden, run sequential(workers=1) vs a REAL 2-worker
   spawn pool with the flag exported: per-file sha256 equality proves the
   env flag reaches spawn workers and both execution shapes store identical
   bytes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from structlog.testing import capture_logs

pytest.importorskip("trading_core", reason="the enrich golden needs the trading_core wheel")
pytest.importorskip("polars", reason="the enrich golden reads the temp lake via polars")

from brutex.data.greeks_enrich import (
    GREEKS_RUST_BATCH_ENV,
    enrich_greeks_for_targets,
    reset_rust_batch_for_tests,
)
from brutex.options.risk_free import FixedRateSource

from tests.golden.generate_greeks_enrich_golden import run_scenario
from tests.golden.test_greeks_enrich_parallel_golden import _build_lake, _partition_hashes

_FIXTURE = Path(__file__).parent / "fixtures" / "greeks_enrich_golden.json"


@pytest.fixture(autouse=True)
def _batch_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test in this file runs with the batch arm ENABLED, fresh state."""
    monkeypatch.setenv(GREEKS_RUST_BATCH_ENV, "1")
    reset_rust_batch_for_tests()


class TestFrozenFixtureWithBatchOn:
    def test_stored_rows_match_the_frozen_fixture_exactly(self, tmp_path: Path) -> None:
        """Flag ON ⇒ the SAME frozen bytes — and the batch genuinely engaged."""
        lake = tmp_path / "lake"
        lake.mkdir()
        with capture_logs() as logs:
            stored = run_scenario(lake)
        frozen = json.loads(_FIXTURE.read_text(encoding="utf-8"))
        assert stored == frozen
        # Anti-vacuous teeth: the batch arm actually served (one engaged INFO
        # per process) and never autoreverted.
        engaged = [e for e in logs if e["event"] == "greeks_enrich.rust_batch_engaged"]
        assert len(engaged) == 1
        assert [e for e in logs if e["event"] == "greeks_enrich.rust_batch_autoreverted"] == []


class TestSpawnParallelWithBatchOn:
    def test_parallel_pass_is_byte_identical_to_sequential(self, tmp_path: Path) -> None:
        """The env flag reaches REAL spawn workers: workers=1 vs workers=2
        store per-file sha256-identical partitions with the batch arm ON."""
        rate = FixedRateSource()
        lake_seq = tmp_path / "lake_seq"
        lake_par = tmp_path / "lake_par"
        targets_seq = _build_lake(lake_seq)
        targets_par = _build_lake(lake_par)
        assert _partition_hashes(lake_seq) == _partition_hashes(lake_par)

        report_seq = enrich_greeks_for_targets(
            targets=targets_seq, lake_root=lake_seq, rate_source=rate, workers=1
        )
        reset_rust_batch_for_tests()  # fresh parent state for the pool pass
        report_par = enrich_greeks_for_targets(
            targets=targets_par, lake_root=lake_par, rate_source=rate, workers=2
        )

        assert report_par == report_seq
        assert report_seq.enriched == 5
        assert report_seq.errored == 0
        hashes_seq = _partition_hashes(lake_seq)
        hashes_par = _partition_hashes(lake_par)
        assert set(hashes_seq) == set(hashes_par)
        assert hashes_seq == hashes_par  # per-file sha256 byte-equality
