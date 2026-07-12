"""Red-team plants for the unkillable run core (DEC-SWEEP-UNKILLABLE-CORE-001 WP-6).

"Test the tester": each test PLANTS a defect the Phase-2 gates exist to catch,
proves the gate goes RED, then restores the plant and proves the gate goes
green again — so the rehearsal/diffcheck teeth are themselves regression-pinned.

1. **Chain-break plant (R28 teeth):** ONE ``entry_hash`` hex character of a
   mid-chain flight-recorder / CLI-chain line is flipped DETERMINISTICALLY —
   always structurally-valid JSON with a wrong stored hash
   (DEC-REDTEAM-TAMPER-DETERMINISTIC-001; the retired first-``b"1"`` byte flip
   was run-content-dependent and occasionally landed in a bare JSON number →
   malformed JSON → the base ``AuditError`` instead of the asserted
   ``AuditChainCorruption`` — a flaky test by construction) — so
   ``brutex.audit_verify.auto_verify`` — the EXACT verifier the R38 rehearsal
   runs (D-2) — raises ``AuditChainCorruption``. Restore the bytes → the
   verifier is green again.
2. **Broken-reference plant (R45 teeth):** the independent reference simulator
   is monkeypatch-replaced by a broken clone; the R45 diffcheck then latches
   SUSPECT everywhere (flight record + manifest + exit-status + chain-map)
   while the PRODUCTION store bytes stay byte-identical (I-4 never-adopt).
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

import pytest

# The nightly red-team harness (redteam-nightly.yml) runs a PYTHON-ONLY editable
# install and never builds the Rust kernels, so the ``brutex`` imports below —
# which transitively import ``brutex.engine``/``brutex.orchestrator`` → ``fno_math``
# + ``trading_core`` — would abort collection of the WHOLE tests/redteam/ dir with
# ``ModuleNotFoundError``. Guard the kernel FIRST (before any ``brutex`` import) so
# a kernel-less collector SKIPS this one module cleanly; the sweep-rehearsal /
# local / full-CI jobs that DO build the wheels still run every test here.
pytest.importorskip("fno_math")
pytest.importorskip("trading_core")
pytest.importorskip("polars")
pytest.importorskip("lmdb")

from brutex.audit_verify import auto_verify
from brutex.data.models import CandleBar
from brutex.orchestrator.audit import AuditChainCorruption, AuditError
from brutex.strategies.types import Side

#: D-6 traceability — test name → requirement/gap tokens.
TRACEABILITY: Final[dict[str, tuple[str, ...]]] = {
    "test_r28_flight_chain_break_plant_turns_the_rehearsal_verifier_red": ("R28", "R41"),
    "test_r28_cli_chain_break_plant_turns_the_rehearsal_verifier_red": ("R28",),
    "test_r28_retired_first_one_flip_was_run_dependent_flaky": ("R28",),
    "test_r28_deterministic_tamper_always_valid_json_wrong_hash": ("R28",),
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


#: The stored ``entry_hash`` hex value inside one raw chain line. Both audit
#: formats share the identical eight-key line shape (``brutex.audit_verify``
#: module docstring), so this matches a flight-recorder / CLI-chain /
#: supervisor line alike.
_ENTRY_HASH_RE: Final = re.compile(rb'"entry_hash"\s*:\s*"([0-9a-f]+)"')


def _tamper_entry_hash(line: bytes) -> bytes:
    """Flip ONE hex char of *line*'s stored ``entry_hash`` — DETERMINISTICALLY.

    The flip stays INSIDE the quoted hex value (hex chars never need JSON
    escaping), so the tampered line is ALWAYS byte-structurally valid JSON
    whose stored hash provably no longer matches the verifier's recompute
    (``verify_chain`` recomputes from the OTHER seven fields, never from
    ``entry_hash`` itself) — ``auto_verify`` must raise
    ``AuditChainCorruption``, never the parse-time base ``AuditError``.

    DEC-REDTEAM-TAMPER-DETERMINISTIC-001: the retired construction
    ``line.replace(b"1", b"@", 1)`` flipped the first ``b"1"`` ANYWHERE in the
    line; the line's bytes are run-specific (hash hex + wall-clock ``ts_utc``),
    so on some runs that byte sat in a bare JSON number (``seq`` / ``ts_utc``)
    and yielded MALFORMED JSON → a different exception → a flaky test.
    """
    match = _ENTRY_HASH_RE.search(line)
    assert match is not None, "chain line carries no entry_hash hex value"
    stored = match.group(1)
    flipped = (b"0" if stored[:1] != b"0" else b"1") + stored[1:]
    assert flipped != stored
    start, end = match.span(1)
    return line[:start] + flipped + line[end:]


def _flip_middle_line(path: Path) -> tuple[bytes, bytes]:
    """Corrupt a MIDDLE chain line's ``entry_hash`` in place; return (before, after)."""
    original = path.read_bytes()
    lines = original.splitlines(keepends=True)
    assert len(lines) >= 3, "the plant needs a middle line"
    middle = lines[len(lines) // 2]
    # Corrupt the recorded content (not the framing): one entry_hash hex char.
    swapped = _tamper_entry_hash(middle)
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
    _run_one(tmp_path, diffcheck_k=0)
    chain = tmp_path / "out" / "audit" / "sweep-all.jsonl"
    clean_count = auto_verify(chain).entry_count
    assert clean_count > 2

    original, _tampered = _flip_middle_line(chain)
    with pytest.raises(AuditChainCorruption):
        auto_verify(chain)

    chain.write_bytes(original)
    assert auto_verify(chain).entry_count == clean_count


def _write_mined_chain(path: Path, *, middle_hash_one_free: bool) -> None:
    """Write a REAL 3-entry orchestrator chain via the REAL ``AuditWriter``,
    with the MIDDLE line's pre-``"seq"`` bytes deterministically containing
    (or not containing) the byte ``b"1"``.

    Canonical key order is ``action < actor < entry_hash < payload <
    prev_hash < seq < subject < ts_utc``; the chosen ``action``/``actor``/
    ``subject``/``payload`` values are digit-free, so the only ``b"1"``
    candidates BEFORE the bare number ``"seq":1`` are the two hex hashes
    (``entry_hash`` = this entry's, ``prev_hash`` = the genesis entry's).
    Both are mined over ``ts_utc`` (P(a SHA-256 hex is '1'-free) =
    ``(15/16)**64`` ≈ 1.6%, so both mining targets land within the bound) —
    making the retired first-``b"1"`` flip's landing spot CONTROLLED:

    * ``middle_hash_one_free=True`` → first ``b"1"`` is the ``"seq":1``
      NUMBER token (the retired flip yields MALFORMED JSON — the flake arm);
    * ``middle_hash_one_free=False`` → first ``b"1"`` is inside the
      ``entry_hash`` hex (the retired flip yields valid JSON — the arm that
      usually passed).
    """
    from brutex.orchestrator.audit import GENESIS_PREV_HASH, AuditEntry, AuditWriter

    def mine(seq: int, prev_hash: str, action: str, *, one_free: bool) -> tuple[float, str]:
        # P(miss all 50_000 tries) ≈ (1 - (15/16)**64)**50_000 — negligible.
        for k in range(50_000):
            ts = 1_700_000_000.0 + k
            entry = AuditEntry(
                seq=seq,
                prev_hash=prev_hash,
                ts_utc=ts,
                actor="brutex",
                action=action,
                subject="run:redteam",
                payload={},
            )
            digest = entry.compute_hash()
            if ("1" not in digest) is one_free:
                return ts, digest
        raise AssertionError("hash mining bound exhausted — statistically impossible")

    ts0, h0 = mine(0, GENESIS_PREV_HASH, "RUN_START", one_free=middle_hash_one_free)
    ts1, h1 = mine(1, h0, "PHASE", one_free=middle_hash_one_free)
    with AuditWriter.open(path, resume=False) as writer:
        e0 = writer.append(action="RUN_START", subject="run:redteam", payload={}, ts_utc=ts0)
        e1 = writer.append(action="PHASE", subject="run:redteam", payload={}, ts_utc=ts1)
        writer.append(action="RUN_END", subject="run:redteam", payload={}, ts_utc=1_700_100_000.0)
    assert (e0.entry_hash, e1.entry_hash) == (h0, h1)  # writer == mined preimage
    middle_prefix = path.read_bytes().splitlines()[1].split(b'"seq"')[0]
    assert (b"1" not in middle_prefix) is middle_hash_one_free


def test_r28_retired_first_one_flip_was_run_dependent_flaky(tmp_path: Path) -> None:
    """The RETIRED tamper construction was run-dependent — the flake was real:
    two REAL writer-built chains, the SAME construction, TWO different
    verdicts. The fixed helper is deterministic on BOTH
    (DEC-REDTEAM-TAMPER-DETERMINISTIC-001)."""
    # Chain A — the HOSTILE shape: middle entry_hash + prev_hash mined
    # '1'-free, so the line's FIRST b"1" is the bare-number ``"seq":1`` token.
    hostile = tmp_path / "hostile.jsonl"
    _write_mined_chain(hostile, middle_hash_one_free=True)
    assert auto_verify(hostile).entry_count == 3
    hostile_lines = hostile.read_bytes().splitlines(keepends=True)
    # The RETIRED flaky construction, kept inline as the self-contained proof:
    retired = hostile_lines[1].replace(b"1", b"@", 1)
    with pytest.raises(json.JSONDecodeError):
        json.loads(retired)  # structurally broken — NOT a hash mismatch
    hostile.write_bytes(b"".join([hostile_lines[0], retired, hostile_lines[2]]))
    with pytest.raises(AuditError) as excinfo:
        auto_verify(hostile)
    # The WRONG verdict class — the base parse error, not chain corruption.
    assert not isinstance(excinfo.value, AuditChainCorruption)

    # Chain B — the USUAL shape: middle entry_hash carries a '1', so the SAME
    # retired construction flips inside the hash hex → valid JSON → the
    # asserted corruption. Same code, two verdicts ⇒ run-dependent flakiness.
    usual = tmp_path / "usual.jsonl"
    _write_mined_chain(usual, middle_hash_one_free=False)
    usual_lines = usual.read_bytes().splitlines(keepends=True)
    retired_b = usual_lines[1].replace(b"1", b"@", 1)
    json.loads(retired_b)  # valid JSON this time
    usual.write_bytes(b"".join([usual_lines[0], retired_b, usual_lines[2]]))
    with pytest.raises(AuditChainCorruption):
        auto_verify(usual)

    # The FIXED helper is deterministic on BOTH chains — always the asserted
    # AuditChainCorruption, INCLUDING the hostile shape that broke the
    # retired construction (this arm FAILS with the fix reverted).
    for path, clean_lines in ((hostile, hostile_lines), (usual, usual_lines)):
        path.write_bytes(b"".join(clean_lines))
        original, _tampered = _flip_middle_line(path)
        with pytest.raises(AuditChainCorruption):
            auto_verify(path)
        path.write_bytes(original)
        assert auto_verify(path).entry_count == 3


def test_r28_deterministic_tamper_always_valid_json_wrong_hash(tmp_path: Path) -> None:
    """EVERY line of a REAL writer-built chain (varied timestamps +
    digit-heavy payloads), tampered by the fixed helper, stays valid JSON
    with ONLY ``entry_hash`` changed — and always turns the exact rehearsal
    verifier red with ``AuditChainCorruption``, never a parse error."""
    from brutex.orchestrator.audit import AuditWriter

    chain = tmp_path / "chain.jsonl"
    with AuditWriter.open(chain, resume=False) as writer:
        for i in range(10):
            writer.append(
                action=f"KIND_{i}",
                subject=f"run:redteam-{i}",
                payload={"n": i, "note": f"w1ndow-2024-0{i}", "count": 1123 + i},
                ts_utc=1_700_000_000.0 + i * 97.25,
            )
    original = chain.read_bytes()
    lines = original.splitlines(keepends=True)
    assert auto_verify(chain).entry_count == len(lines) == 10

    for index, line in enumerate(lines):
        tampered_line = _tamper_entry_hash(line)
        record = json.loads(tampered_line)  # (a) ALWAYS valid JSON
        clean_record = json.loads(line)
        assert record["entry_hash"] != clean_record["entry_hash"]  # (b) hash changed …
        assert {k: v for k, v in record.items() if k != "entry_hash"} == {
            k: v for k, v in clean_record.items() if k != "entry_hash"
        }  # … and NOTHING else on the line
        mutated = list(lines)
        mutated[index] = tampered_line
        chain.write_bytes(b"".join(mutated))
        with pytest.raises(AuditChainCorruption):  # (c) the real verifier goes red
            auto_verify(chain)
        chain.write_bytes(original)
        assert auto_verify(chain).entry_count == 10  # green again


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
