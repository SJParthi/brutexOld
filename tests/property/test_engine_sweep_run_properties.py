"""Property + rerun-safe oracle tests for `brutex.engine.sweep_run`.

Two invariant families (hypothesis + a hand-built fixed-bar oracle):

* **Run-identity full-field invariant.** :func:`mask_run_id` is a deterministic
  function of its 9 inputs: identical inputs ⇒ identical id; any single field
  changing ⇒ a different id (no aliasing). This is the Golden Rule #4
  reproducibility contract for the mask identity.
* **Rerun-safe oracle (Golden Rule #7).** Running :func:`run_persisted_sweep`
  twice into the SAME store + dedup + audit dirs ⇒ the second run evaluates
  nothing (everything is deduped) and the store stays byte-identical. Running it
  twice into FRESH dirs ⇒ identical content hash (determinism across runs).
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
    mask_run_id,
    run_persisted_sweep,
)
from brutex.orchestrator.audit import AuditWriter
from brutex.strategies.types import Side
from hypothesis import given, settings
from hypothesis import strategies as st

_IST = timezone(timedelta(hours=5, minutes=30))

_FIELDS = (
    "direction",
    "instrument",
    "tf",
    "mode",
    "data_snapshot_hash",
    "indicator_version",
    "code_commit_sha",
)


def _ctx() -> dict[str, str]:
    return {
        "direction": "bull",
        "instrument": "NIFTY",
        "tf": "1m",
        "mode": "A",
        "data_snapshot_hash": "snap0001",
        "indicator_version": "ind-v1",
        "code_commit_sha": "deadbeef",
    }


# ────── mask_run_id full-field invariant ──────

# The hashed run-identity fields are joined with the ``\x1f`` unit-separator
# byte inside ``combo_hash``, which (DEC-STRATEGIES-CANONICAL-HARDEN-001)
# rejects any field containing that delimiter to close a real join-ambiguity
# collision. The fields' real domain is therefore delimiter-free text, so the
# generators below draw from a representative ``\x1f``-free alphabet (mirroring
# ``_FIELD_TEXT`` in ``test_combo_hash_uniqueness.py``). This keeps every
# invariant asserted over the function's TRUE input domain — it is a domain
# constraint, not a weakening of the property.
_FIELD_ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
_nonempty = st.text(alphabet=_FIELD_ALPHABET, min_size=1, max_size=12)


class TestRunIdInvariant:
    @settings(max_examples=200)
    @given(
        mask=st.integers(min_value=0, max_value=(1 << 52) - 1),
        ctx=st.fixed_dictionaries(dict.fromkeys(_FIELDS, _nonempty)),
        prior_n=st.integers(min_value=1, max_value=4),
    )
    def test_deterministic(self, mask: int, ctx: dict[str, str], prior_n: int) -> None:
        params = {"prior_n": prior_n}
        a = mask_run_id(mask=mask, params=params, **ctx)
        b = mask_run_id(mask=mask, params=params, **ctx)
        assert a == b
        assert len(a) == 64

    @settings(max_examples=200)
    @given(
        mask_a=st.integers(min_value=0, max_value=(1 << 52) - 1),
        mask_b=st.integers(min_value=0, max_value=(1 << 52) - 1),
    )
    def test_distinct_masks_distinct_ids(self, mask_a: int, mask_b: int) -> None:
        ctx = _ctx()
        id_a = mask_run_id(mask=mask_a, params={}, **ctx)
        id_b = mask_run_id(mask=mask_b, params={}, **ctx)
        assert (mask_a == mask_b) == (id_a == id_b)

    @settings(max_examples=150)
    @given(
        field=st.sampled_from(_FIELDS),
        suffix=st.text(alphabet=_FIELD_ALPHABET, min_size=1, max_size=4),
    )
    def test_any_field_change_changes_id(self, field: str, suffix: str) -> None:
        ctx = _ctx()
        base = mask_run_id(mask=42, params={}, **ctx)
        mutated = {**ctx, field: ctx[field] + suffix}
        assert mask_run_id(mask=42, params={}, **mutated) != base


# ────── Rerun-safe oracle (Golden Rule #7) ──────


def _bars() -> list[CandleBar]:
    bars: list[CandleBar] = []
    for d in range(12):
        price = 100.0
        for m in range(120):
            close = price + 0.6 if m % 3 else price - 0.3
            ts = (
                datetime(2026, 1, 5, 9, 16, tzinfo=_IST) + timedelta(days=d, minutes=m)
            ).astimezone(UTC)
            hi = max(price, close) + 0.5
            lo = min(price, close) - 0.5
            bars.append(
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
    return bars


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


def test_rerun_same_dirs_dedups_everything(tmp_path: Path) -> None:
    pytest.importorskip("polars")  # flush/load need the [data] extra; skips the .[dev]-only job
    bars = _bars()
    cfg = _config()
    store_path = tmp_path / "r.parquet"
    audit_path = tmp_path / "a.jsonl"
    dedup = ComboDedup(OptionDedupIndex(capacity=10_000, fpr_target=1e-6))

    store1 = ResultsStore(path=store_path)
    with AuditWriter.open(audit_path) as audit:
        first = run_persisted_sweep(
            bars, cfg, store=store1, dedup=dedup, audit=audit, exec_bars_1m=bars
        )
    first_bytes = store_path.read_bytes()

    # Second run into the SAME store + dedup: resume the persisted store (the
    # realistic crash-recovery pattern), so every enumerated non-conflict mask is
    # already seen, nothing is re-evaluated, and the re-flush is byte-identical.
    store2 = ResultsStore.load(store_path)
    with AuditWriter.open(audit_path, resume=True) as audit:
        second = run_persisted_sweep(
            bars, cfg, store=store2, dedup=dedup, audit=audit, exec_bars_1m=bars
        )

    assert second.evaluated == 0
    assert second.skipped_dedup == first.enumerated - first.conflict_pruned
    assert second.persisted == 0
    assert store_path.read_bytes() == first_bytes
    assert second.store_content_hash == first.store_content_hash


def test_rerun_fresh_dirs_identical_content_hash(tmp_path: Path) -> None:
    pytest.importorskip("polars")  # flush needs the [data] extra; skips the .[dev]-only job
    bars = _bars()
    cfg = _config()

    def _run(tag: str) -> str:
        store = ResultsStore(path=tmp_path / f"{tag}.parquet")
        dedup = ComboDedup(OptionDedupIndex(capacity=10_000, fpr_target=1e-6))
        with AuditWriter.open(tmp_path / f"{tag}.jsonl") as audit:
            return run_persisted_sweep(
                bars, cfg, store=store, dedup=dedup, audit=audit, exec_bars_1m=bars
            ).store_content_hash

    assert _run("a") == _run("b")
