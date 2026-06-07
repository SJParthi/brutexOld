"""Golden byte-equality oracle for the persisted sweep runner.

Two pinned, hand-fixed oracles (DEC-SWEEP-RUN-001 / Golden Rule #4):

* **Run-identity byte-for-byte.** :func:`mask_run_id` is a pure BLAKE3 digest of
  its 9 canonical inputs — version-stable across polars / zstd (it touches
  neither). One mask + one fixed context + fixed params is pinned to its exact
  64-char hex.
* **End-to-end store content hash.** A fixed synthetic bar fixture run through
  :func:`run_persisted_sweep` (with a fixed ``ts_utc`` on every audit line, so
  the audit chain is deterministic) is pinned to:
  (a) a stable logical summary (enumerated / ranked / persisted), and
  (b) a byte-identical store across two independent runs (determinism), plus
  (c) the exact ``store_content_hash`` literal.

Mirrors the LOGICAL-first philosophy of
``tests/golden/test_engine_results_store_golden.py``: the determinism +
summary assertions are the version-robust contract; the literal content-hash
pin is the strongest form but is sensitive to the installed polars / zstd
build (same caveat the results-store golden documents) — a mismatch there with
the determinism assertions still green means a dependency bump, not a logic
regression.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.data.models import CandleBar
from brutex.data.option_dedup import OptionDedupIndex
from brutex.engine.results_store import ResultsStore
from brutex.engine.sweep_run import (
    ComboDedup,
    SweepRunConfig,
    SweepRunSummary,
    mask_run_id,
    run_persisted_sweep,
)
from brutex.orchestrator.audit import AuditWriter
from brutex.strategies.types import Side

_IST = timezone(timedelta(hours=5, minutes=30))

#: A fixed audit timestamp so the audit chain hashes deterministically.
_FIXED_TS_UTC = 1_700_000_000.0

#: The pinned 9-field context for the run-identity oracle.
_CTX: dict[str, str] = {
    "direction": "bull",
    "instrument": "NIFTY",
    "tf": "1m",
    "mode": "A",
    "data_snapshot_hash": "snap0001",
    "indicator_version": "ind-v1",
    "code_commit_sha": "deadbeef",
}
_PARAMS: dict[str, object] = {
    "sl_paisa": 80,
    "tp_paisa": 50,
    "prior_n": 3,
    "proximity_bps": None,
    "start_equity_paisa": 10_000_000,
}


def _bars() -> list[CandleBar]:
    """The fixed synthetic 12-day upward-drift fixture (deterministic OHLC)."""
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
        condition_bits=[0, 1, 2, 3, 4],
        k=1,
        data_snapshot_hash="snap0001",
        indicator_version="ind-v1",
        code_commit_sha="deadbeef",
        tf_minutes=1,
        sl_paisa=80,
        tp_paisa=50,
    )


def _run(tmp_path: Path, tag: str) -> SweepRunSummary:
    store = ResultsStore(path=tmp_path / f"{tag}.parquet")
    dedup = ComboDedup(OptionDedupIndex(capacity=10_000, fpr_target=1e-6))
    audit = AuditWriter(path=tmp_path / f"{tag}.jsonl")
    with audit:
        # The audit timestamps are not part of the store content hash; the fixed
        # ts only makes the audit JSONL chain reproducible (used by the integration
        # test). run_persisted_sweep stamps time.time() internally, so we patch the
        # writer to a fixed ts via a thin subclass-free monkeypatch is unnecessary
        # here — the store hash is independent of audit timing.
        return run_persisted_sweep(_bars(), _config(), store=store, dedup=dedup, audit=audit)


# ────── Pinned run-identity ──────

#: The hand-pinned BLAKE3 run-identity of mask 0b101 under the fixed context.
_GOLDEN_RUN_ID = "1d4c8830c1d3c9f12e7fc407a6bec3058da214b9096bcf71c5cc27f187bd5942"


def test_golden_run_id_is_byte_stable() -> None:
    assert mask_run_id(mask=0b101, params=_PARAMS, **_CTX) == _GOLDEN_RUN_ID


# ────── Pinned end-to-end summary + determinism + content hash ──────

#: The hand-pinned store content hash for the fixed fixture (polars/zstd-build
#: sensitive — see the module docstring; the determinism + summary assertions
#: are the version-robust contract).
# Re-locked for DEC-SWEEP-WINNERS-BY-NAME-001: the readable-mask surface added the
# mask_bits + condition_names + (nullable) deflated_sharpe columns to the persisted
# Parquet schema, so the deterministic content hash moved by design.
_GOLDEN_CONTENT_HASH = "d3070e20afc27fcc060df3862b1b5053cea838e48748e46933fe1c8b26486d74"


def test_golden_summary_is_stable(tmp_path: Path) -> None:
    pytest.importorskip("polars")  # flush needs the [data] extra; skips the .[dev]-only job
    summary = _run(tmp_path, "a")
    assert summary.enumerated == 5
    assert summary.conflict_pruned == 0
    assert summary.skipped_dedup == 0
    assert summary.evaluated == 5
    assert summary.ranked == 4
    assert summary.persisted == 4


def test_golden_store_is_deterministic_across_runs(tmp_path: Path) -> None:
    pytest.importorskip("polars")  # flush needs the [data] extra; skips the .[dev]-only job
    a = _run(tmp_path, "a")
    b = _run(tmp_path, "b")
    assert a.store_content_hash == b.store_content_hash
    assert (tmp_path / "a.parquet").read_bytes() == (tmp_path / "b.parquet").read_bytes()


def test_golden_content_hash_literal(tmp_path: Path) -> None:
    pytest.importorskip("polars")  # flush needs the [data] extra; skips the .[dev]-only job
    summary = _run(tmp_path, "a")
    assert summary.store_content_hash == _GOLDEN_CONTENT_HASH
