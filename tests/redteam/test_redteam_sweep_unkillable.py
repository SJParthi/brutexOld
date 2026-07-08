"""Red-team plants for the unkillable run core (DEC-SWEEP-UNKILLABLE-CORE-001 WP-6).

"Test the tester": each test PLANTS a defect the Phase-2 gates exist to catch,
proves the gate goes RED, then restores the plant and proves the gate goes
green again — so the rehearsal/diffcheck teeth are themselves regression-pinned.

1. **Chain-break plant (R28 teeth):** a byte flipped in a mid-chain
   flight-recorder / CLI-chain line makes ``brutex.audit_verify.auto_verify`` —
   the EXACT verifier the R38 rehearsal runs (D-2) — raise
   ``AuditChainCorruption``. Restore the bytes → the verifier is green again.
2. **Broken-reference plant (R45 teeth):** the independent reference simulator
   is monkeypatch-replaced by a broken clone; the R45 diffcheck then latches
   SUSPECT everywhere (flight record + manifest + exit-status + chain-map)
   while the PRODUCTION store bytes stay byte-identical (I-4 never-adopt).
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

import pytest
from brutex.audit_verify import auto_verify
from brutex.data.models import CandleBar
from brutex.orchestrator.audit import AuditChainCorruption
from brutex.strategies.types import Side

pytest.importorskip("polars")
pytest.importorskip("lmdb")

#: D-6 traceability — test name → requirement/gap tokens.
TRACEABILITY: Final[dict[str, tuple[str, ...]]] = {
    "test_r28_flight_chain_break_plant_turns_the_rehearsal_verifier_red": ("R28", "R41"),
    "test_r28_cli_chain_break_plant_turns_the_rehearsal_verifier_red": ("R28",),
    "test_r45_broken_reference_plant_marks_the_run_suspect_everywhere": ("R45", "R39"),
}

_IST = timezone(timedelta(hours=5, minutes=30))


def _bars() -> list[CandleBar]:
    out: list[CandleBar] = []
    for d in range(3):
        price = 100.0
        for m in range(40):
            close = price + 0.6 if m % 3 else price - 0.3
            ts = (
                datetime(2026, 1, 5, 9, 16, tzinfo=_IST) + timedelta(days=d, minutes=m)
            ).astimezone(UTC)
            out.append(
                CandleBar(
                    timestamp=ts,
                    open=Decimal(str(round(price, 2))),
                    high=Decimal(str(round(max(price, close) + 0.5, 2))),
                    low=Decimal(str(round(min(price, close) - 0.5, 2))),
                    close=Decimal(str(round(close, 2))),
                    volume=100,
                    open_interest=None,
                )
            )
            price = close
    return out


def _grid() -> Any:
    from brutex.engine.sweep_orchestrator import SweepGridConfig, Timeframe

    return SweepGridConfig(
        instruments=["NIFTY"],
        timeframes=[Timeframe("1minute", 1)],
        directions=[Side.BUY],
        k_values=[1],
        condition_bits=[0, 1, 2, 3, 4],
        stop_losses=[80],
        take_profits=[50],
        proximities=[None],
        mode="A",
        data_snapshot_hash="snap-redteam",
        indicator_version="ind-v1",
        code_commit_sha="deadbeef",
    )


def _run_one(root: Path, *, diffcheck_k: int) -> tuple[str, Path]:
    """One run-core-wrapped single-tuple run; returns (run_id, store_path)."""
    from brutex.data.option_dedup import OptionDedupIndex
    from brutex.engine.sweep_kernel import ResidentKernelCache
    from brutex.engine.sweep_orchestrator import (
        iter_sweep_tuples,
        make_store_for,
        run_one_tuple,
        tuple_store_path,
    )
    from brutex.engine.sweep_run import ComboDedup
    from brutex.engine.sweep_run_core import sweep_run_core
    from brutex.notifications.sweep_alerts import SweepAlerter
    from brutex.observability.run_tripwires import TripwireConfig
    from brutex.orchestrator.audit import AuditWriter

    grid = _grid()
    bars = _bars()
    out = root / "out"
    chain_path = out / "audit" / "sweep-all.jsonl"
    chain_path.parent.mkdir(parents=True, exist_ok=True)
    with sweep_run_core(
        results_root=out,
        grid=grid,
        alerter=SweepAlerter(),
        entry_label="redteam",
        cli_chain_path=chain_path,
        tripwire_config=TripwireConfig(),
        diffcheck_k=diffcheck_k,
    ) as core:
        core.stage("sweep")
        with AuditWriter.open(chain_path, resume=chain_path.exists()) as audit:
            tuple_ = next(iter_sweep_tuples(grid))
            run_one_tuple(
                grid,
                tuple_,
                bars_for=lambda _i, _t: bars,
                store_for=make_store_for(out, grid),
                dedup=ComboDedup(OptionDedupIndex(capacity=10_000, fpr_target=1e-6)),
                audit=audit,
                kernel_cache=ResidentKernelCache(capacity=1),
                diffcheck=core.diffcheck,
                store_quarantine=core.store_quarantine,
            )
        core.set_exit_code(0)
    return core.run_id, tuple_store_path(out, tuple_, grid)


def _flip_middle_line(path: Path) -> tuple[bytes, bytes]:
    """Flip one payload byte in a MIDDLE chain line; return (before, after)."""
    original = path.read_bytes()
    lines = original.splitlines(keepends=True)
    assert len(lines) >= 3, "the plant needs a middle line"
    middle = lines[len(lines) // 2]
    # Corrupt the recorded content (not the framing): 1 <-> 2 in the JSON body.
    swapped = middle.replace(b"1", b"@", 1)
    assert swapped != middle
    lines[len(lines) // 2] = swapped
    tampered = b"".join(lines)
    path.write_bytes(tampered)
    return original, tampered


def test_r28_flight_chain_break_plant_turns_the_rehearsal_verifier_red(
    tmp_path: Path,
) -> None:
    from brutex.observability.flight_recorder import recorder_path

    run_id, _store = _run_one(tmp_path, diffcheck_k=0)
    flight = recorder_path(tmp_path / "out", run_id)
    clean_count = auto_verify(flight).entry_count
    assert clean_count > 2

    original, tampered = _flip_middle_line(flight)
    with pytest.raises(AuditChainCorruption):
        auto_verify(flight)  # the EXACT rehearsal gate call goes RED
    assert flight.read_bytes() == tampered

    flight.write_bytes(original)  # restore the plant
    assert auto_verify(flight).entry_count == clean_count  # green again


def test_r28_cli_chain_break_plant_turns_the_rehearsal_verifier_red(
    tmp_path: Path,
) -> None:
    _run_id, _store = _run_one(tmp_path, diffcheck_k=0)
    chain = tmp_path / "out" / "audit" / "sweep-all.jsonl"
    clean_count = auto_verify(chain).entry_count
    assert clean_count > 2

    original, _tampered = _flip_middle_line(chain)
    with pytest.raises(AuditChainCorruption):
        auto_verify(chain)

    chain.write_bytes(original)
    assert auto_verify(chain).entry_count == clean_count


def test_r45_broken_reference_plant_marks_the_run_suspect_everywhere(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A silently-broken reference simulator can never pass: the diffcheck
    mismatch latches SUSPECT on EVERY surface and never adopts (I-4)."""
    import brutex.engine.sweep_diffcheck as diffcheck_mod
    from brutex.observability.flight_recorder import iter_run_records

    # The clean control arm first — the oracle store bytes for never-adopt.
    clean_run_id, clean_store = _run_one(tmp_path / "clean", diffcheck_k=8)
    clean_bytes = clean_store.read_bytes()
    clean_records = list(iter_run_records(tmp_path / "clean" / "out", clean_run_id))
    assert [r for r in clean_records if r["kind"] == "DIFFCHECK_OK"]

    # PLANT: the reference clone silently returns NO trades for every mask.
    real_reference = diffcheck_mod.reference_simulate

    def _broken_clone(*args: object, **kwargs: object) -> list[object]:
        return []

    monkeypatch.setattr(diffcheck_mod, "reference_simulate", _broken_clone)
    run_id, store = _run_one(tmp_path / "planted", diffcheck_k=8)
    monkeypatch.setattr(diffcheck_mod, "reference_simulate", real_reference)

    out = tmp_path / "planted" / "out"
    records = list(iter_run_records(out, run_id))
    mismatches = [r for r in records if r["kind"] == "DIFFCHECK_MISMATCH"]
    assert len(mismatches) >= 1
    payload = mismatches[0]["payload"]
    assert isinstance(payload, dict)
    assert set(payload) >= {"tuple_digest", "mask_hex", "field", "prod", "ref"}
    suspects = [r for r in records if r["kind"] == "SUSPECT"]
    assert len(suspects) == 1  # the D-12 latch fans out ONCE

    manifest = json.loads((out / "runs" / run_id / "manifest.json").read_text())
    assert manifest["suspect"] is True
    assert manifest["suspect_reasons"] != []
    status = json.loads((out / "runs" / run_id / "exit-status.json").read_text())
    assert status["state"] == "suspect_complete"
    assert status["suspect"] is True
    chain_map = json.loads((out / "runs" / run_id / "chain-map.json").read_text())
    assert chain_map["suspect"] is True

    # I-4 never-adopt: the production store bytes are EXACTLY the clean arm's.
    assert run_id == clean_run_id  # same grid+data ⇒ same identity (D-1)
    assert hashlib.sha256(store.read_bytes()).hexdigest() == hashlib.sha256(clean_bytes).hexdigest()
