"""Flag-ON Rust-arm replay of the audit-chain parity goldens.

DEC-AUDIT-CHAIN-RUST-PORT-001 (slice 9, ADR-0019 / MIGRATION.md #1727): the
SAME frozen fixtures as ``test_audit_chain_parity_golden.py`` (which keeps
pinning the flag-OFF Python arm), replayed with ``BRUTEX_RUST_AUDIT_CHAIN=1``
— the real ``AuditWriter`` drives the Rust serializer through the dispatch
seam and must land byte-for-byte on the frozen chain. Plus: the direct
kernel scalar-vector replay (``py_float_repr`` / ``canonical_json`` /
``audit_entry_hash`` / ``audit_entry_line``), the cross-arm verify proof (a
chain written flag-ON verifies flag-OFF and vice versa — the
historical-chain-compat law in miniature), the NaN refuse-before-write pin
on the Rust arm, and the ZERO-FALLBACK census (every fixture payload shape
is Rust-served, no envelope fallback fires).

Skips VISIBLY when the installed ``fno_math`` wheel predates the slice-9
audit symbols (a stale local wheel), never silently.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final

import pytest
from brutex.orchestrator import audit
from brutex.orchestrator.audit import AuditEntry, AuditPayloadError, AuditWriter, verify_chain
from structlog.testing import capture_logs

fno_math = pytest.importorskip("fno_math")
if not hasattr(fno_math, "audit_entry_hash"):  # pragma: no cover - stale-wheel guard
    pytest.skip(
        "installed fno_math wheel predates the slice-9 audit symbols — rebuild the wheel",
        allow_module_level=True,
    )

_FIXTURES: Final[Path] = Path(__file__).parent / "fixtures"
_CHAIN_FIXTURE: Final[Path] = _FIXTURES / "audit_chain_parity_golden.jsonl"
_SCALARS_FIXTURE: Final[Path] = _FIXTURES / "audit_chain_parity_scalars.json"


@pytest.fixture(autouse=True)
def _rust_audit_arm(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Arm the Rust audit kernel per test; restore the seam after."""
    monkeypatch.setenv(audit.RUST_AUDIT_CHAIN_ENV, "1")
    audit._reset_rust_audit_for_tests()  # pyright: ignore[reportPrivateUsage]
    assert audit._rust_audit_kernel() is not None  # pyright: ignore[reportPrivateUsage]
    yield
    audit._reset_rust_audit_for_tests()  # pyright: ignore[reportPrivateUsage]


def _chain_bytes() -> bytes:
    return _CHAIN_FIXTURE.read_bytes()


def _chain_entries() -> list[AuditEntry]:
    raw = _chain_bytes().decode("utf-8")
    return [AuditEntry.from_json_line(line) for line in raw.splitlines()]


def _scalars_doc() -> dict[str, Any]:
    doc: dict[str, Any] = json.loads(_SCALARS_FIXTURE.read_text(encoding="utf-8"))
    return doc


def test_writer_equivalence_rust_arm_reproduces_fixture_bytes(tmp_path: Path) -> None:
    """A fresh REAL ``AuditWriter`` under the Rust arm emits the frozen bytes.

    The strongest flag-ON tripwire: the production writer, dispatching its
    serialization to the Rust kernel, must land byte-for-byte on the frozen
    file — with ZERO envelope fallbacks (the census half: every fixture
    payload shape is genuinely Rust-served) and the burn-in never latching.
    """
    entries = _chain_entries()
    replay = tmp_path / "replay.jsonl"
    with capture_logs() as logs, AuditWriter.open(replay, resume=False) as writer:
        for entry in entries:
            writer.append(
                action=entry.action,
                subject=entry.subject,
                payload=entry.payload,
                ts_utc=entry.ts_utc,
            )
    assert replay.read_bytes() == _chain_bytes()
    events = [record.get("event") for record in logs]
    assert "audit.rust_envelope_fallback" not in events, "fixture payloads must be Rust-served"
    assert "audit.kernel_autoreverted" not in events
    assert audit._RUST_DISABLED_REASON[0] is None  # pyright: ignore[reportPrivateUsage]


def test_verify_chain_green_over_the_fixture_rust_arm() -> None:
    """The streaming verifier replays the frozen chain clean under the Rust arm."""
    assert verify_chain(_CHAIN_FIXTURE) == 10


def test_scalar_entry_vectors_replay_through_the_kernel() -> None:
    """``audit_entry_hash`` / ``audit_entry_line`` reproduce every vector directly."""
    doc = _scalars_doc()
    vectors: list[dict[str, Any]] = list(doc["entries"]) + list(doc["standalone_entries"])
    assert len(vectors) == 11
    for vector in vectors:
        args = (
            int(vector["seq"]),
            str(vector["prev_hash"]),
            float(vector["ts_utc"]),
            str(vector["actor"]),
            str(vector["action"]),
            str(vector["subject"]),
            dict(vector["payload"]),
        )
        assert fno_math.audit_entry_hash(*args) == vector["expected_entry_hash"]
        assert (
            fno_math.audit_entry_line(*args, str(vector["expected_entry_hash"]))
            == vector["expected_line"]
        )


def test_float_repr_vectors_replay_through_the_kernel() -> None:
    """``fno_math.py_float_repr`` reproduces every frozen repr string."""
    for vector in _scalars_doc()["float_repr_vectors"]:
        value = float(vector["value"])
        assert fno_math.py_float_repr(value) == vector["expected_str"]


def test_canonical_json_vectors_replay_through_the_kernel() -> None:
    """``fno_math.canonical_json`` reproduces every frozen canonical string."""
    for vector in _scalars_doc()["canonical_json_vectors"]:
        assert fno_math.canonical_json(vector["payload"]) == vector["expected_json"]


def test_cross_arm_verify_chain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A chain written flag-ON verifies flag-OFF, and vice versa.

    The historical-chain-compat proof in miniature: byte production is
    arm-independent, so operator chains on disk read identically whichever
    arm verifies them.
    """
    entries = _chain_entries()

    def _write(path: Path) -> None:
        with AuditWriter.open(path, resume=False) as writer:
            for entry in entries[:4]:
                writer.append(
                    action=entry.action,
                    subject=entry.subject,
                    payload=entry.payload,
                    ts_utc=entry.ts_utc,
                )

    written_on = tmp_path / "on.jsonl"
    _write(written_on)  # the autouse fixture armed the Rust kernel

    monkeypatch.delenv(audit.RUST_AUDIT_CHAIN_ENV, raising=False)
    audit._reset_rust_audit_for_tests()  # pyright: ignore[reportPrivateUsage]
    assert audit._rust_audit_kernel() is None  # pyright: ignore[reportPrivateUsage]
    assert verify_chain(written_on) == 4  # flag-OFF verifies the flag-ON bytes
    written_off = tmp_path / "off.jsonl"
    _write(written_off)
    assert written_off.read_bytes() == written_on.read_bytes()

    monkeypatch.setenv(audit.RUST_AUDIT_CHAIN_ENV, "1")
    audit._reset_rust_audit_for_tests()  # pyright: ignore[reportPrivateUsage]
    assert audit._rust_audit_kernel() is not None  # pyright: ignore[reportPrivateUsage]
    assert verify_chain(written_off) == 4  # flag-ON verifies the flag-OFF bytes


def test_nan_payload_refuses_before_write_rust_arm(tmp_path: Path) -> None:
    """NaN still refuses BEFORE any write on the Rust arm.

    The kernel refuses the non-finite float at its envelope; the seam falls
    back to the Python oracle, which raises today's exact
    ``AuditPayloadError`` — the chain position never advances.
    """
    path = tmp_path / "nan.jsonl"
    with AuditWriter.open(path, resume=False) as writer:
        first = writer.append(action="RUN_START", subject="run:nan", ts_utc=1700000000.0)
        before = path.read_bytes()
        with pytest.raises(AuditPayloadError):
            writer.append(
                action="BAD",
                subject="nan:payload",
                payload={"x": float("nan")},
                ts_utc=1700000001.0,
            )
        assert path.read_bytes() == before
        second = writer.append(action="RUN_COMPLETE", subject="run:nan", ts_utc=1700000002.0)
    assert (first.seq, second.seq) == (0, 1)
    assert verify_chain(path) == 2
