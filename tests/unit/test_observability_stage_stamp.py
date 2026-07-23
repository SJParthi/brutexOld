"""Unit suite for :mod:`brutex.observability.stage_stamp` (R42, G7, D-4).

Pins the exact sidecar schema, the full verdict matrix (incl. torn artifact,
stale run-id stamp, swapped stamp, snapshot mismatch), the always-recompute
law (I-5), the stage-3 cell batch helpers, and the forensic
``discard_and_flag`` rename (never delete) with its QUARANTINE record.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final

import pytest
from brutex.observability.flight_recorder import FlightRecorder, iter_run_records
from brutex.observability.stage_stamp import (
    SNAPSHOT_EXTRA_KEY,
    STAGE3_CELL_STAGE,
    STAMP_SCHEMA_VERSION,
    STAMP_SUFFIX,
    StageStamp,
    StageStampError,
    StampVerdict,
    content_hash_of,
    discard_and_flag,
    read_stage_stamp,
    stamp_path,
    stamp_stage3_cells,
    validate_stage3_cells,
    validate_stage_stamp,
    write_stage_stamp,
)

#: D-6 mechanized traceability — test name → requirement/gap tokens.
TRACEABILITY: Final[dict[str, tuple[str, ...]]] = {
    "test_r42_stamp_sidecar_schema_exact": ("R42",),
    "test_r42_write_refuses_missing_artifact": ("R42",),
    "test_r42_write_refuses_non_string_extra": ("R42",),
    "test_r42_write_failure_maps_to_error": ("R42",),
    "test_r42_content_hash_streamed_sha256": ("R42",),
    "test_r42_verdict_ok": ("R42",),
    "test_r42_verdict_missing_artifact": ("R42",),
    "test_r42_verdict_missing_stamp": ("R42",),
    "test_r42_verdict_malformed_matrix": ("R42",),
    "test_r42_verdict_torn_artifact_hash_mismatch": ("R42",),
    "test_r42_verdict_stale_run_id_mismatch": ("R42",),
    "test_r42_verdict_swapped_stamp_mismatch": ("R42",),
    "test_r42_verdict_snapshot_extra_mismatch_g7": ("R42", "G7"),
    "test_r42_snapshot_absent_extra_not_failed": ("R42",),
    "test_r42_always_recomputes_hash": ("R42",),
    "test_r42_unreadable_artifact_raises_loud": ("R42",),
    "test_r42_read_stage_stamp_strict": ("R42",),
    "test_r42_stage3_cell_batch_helpers": ("R42",),
    "test_r42_stage3_cells_with_snapshot_extra": ("R42",),
    "test_r42_discard_and_flag_forensic_rename": ("R42",),
    "test_r42_discard_monotonic_ordinal_and_partials": ("R42",),
    "test_r42_discard_rename_failure_raises": ("R42",),
}

_RUN_ID: Final[str] = "ef" * 32
_SNAP: Final[str] = "12" * 32


def _artifact(tmp_path: Path, name: str = "report.json", data: bytes = b'{"x": 1}\n') -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


class TestWrite:
    def test_r42_stamp_sidecar_schema_exact(self, tmp_path: Path) -> None:
        """The sidecar carries EXACTLY the contract fields + additive
        schema_version/extra (contracts file #2)."""
        artifact = _artifact(tmp_path)
        sidecar = write_stage_stamp(
            artifact, stage="cascade_manifest", run_id=_RUN_ID, extra={SNAPSHOT_EXTRA_KEY: _SNAP}
        )
        assert sidecar == stamp_path(artifact) == tmp_path / ("report.json" + STAMP_SUFFIX)
        assert STAMP_SUFFIX == ".stamp.json"
        raw = json.loads(sidecar.read_text(encoding="utf-8"))
        assert set(raw.keys()) == {
            "stage",
            "run_id",
            "content_hash",
            "produced_at_utc",
            "schema_version",
            "extra",
        }
        assert raw["stage"] == "cascade_manifest"
        assert raw["run_id"] == _RUN_ID
        assert raw["content_hash"] == content_hash_of(artifact)
        assert len(raw["content_hash"]) == 64
        assert raw["schema_version"] == STAMP_SCHEMA_VERSION == 1
        assert raw["extra"] == {SNAPSHOT_EXTRA_KEY: _SNAP}
        assert isinstance(raw["produced_at_utc"], float)

    def test_r42_write_refuses_missing_artifact(self, tmp_path: Path) -> None:
        """I-5 artifact-first law: stamping a non-existent artifact refuses."""
        with pytest.raises(StageStampError, match="does not exist"):
            write_stage_stamp(tmp_path / "ghost.json", stage="s", run_id=_RUN_ID)

    def test_r42_write_refuses_non_string_extra(self, tmp_path: Path) -> None:
        """extra must be str→str — junk refuses loudly."""
        artifact = _artifact(tmp_path)
        with pytest.raises(StageStampError, match="str→str"):
            write_stage_stamp(artifact, stage="s", run_id=_RUN_ID, extra={"n": 3})  # type: ignore[dict-item]

    def test_r42_write_failure_maps_to_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An OSError in the atomic write maps to StageStampError."""
        artifact = _artifact(tmp_path)

        def _boom(dest: Path, write_to: object) -> None:
            raise OSError("disk full")

        # String-target monkeypatch (the #1810 CodeQL convention).
        monkeypatch.setattr("brutex.observability.stage_stamp.atomic_write_simple", _boom)
        with pytest.raises(StageStampError, match="failed to write stamp"):
            write_stage_stamp(artifact, stage="s", run_id=_RUN_ID)

    def test_r42_content_hash_streamed_sha256(self, tmp_path: Path) -> None:
        """content_hash_of == hashlib.sha256 over the exact bytes (full 64-hex)."""
        import hashlib

        data = b"\x00\x01" * (3 << 20)  # 6 MiB — spans multiple 1 MiB chunks
        artifact = _artifact(tmp_path, "big.bin", data)
        assert content_hash_of(artifact) == hashlib.sha256(data).hexdigest()
        empty = _artifact(tmp_path, "empty.bin", b"")
        assert content_hash_of(empty) == hashlib.sha256(b"").hexdigest()
        with pytest.raises(StageStampError, match="cannot hash artifact"):
            content_hash_of(tmp_path / "missing.bin")


class TestVerdictMatrix:
    def test_r42_verdict_ok(self, tmp_path: Path) -> None:
        artifact = _artifact(tmp_path)
        write_stage_stamp(artifact, stage="stage3_report", run_id=_RUN_ID)
        verdict, stamp = validate_stage_stamp(
            artifact, expected_stage="stage3_report", expected_run_id=_RUN_ID
        )
        assert verdict is StampVerdict.OK
        assert stamp is not None
        assert stamp.stage == "stage3_report"
        assert stamp.run_id == _RUN_ID
        assert stamp.content_hash == content_hash_of(artifact)

    def test_r42_verdict_missing_artifact(self, tmp_path: Path) -> None:
        """Artifact gone (even with a stamp present) → MISSING_ARTIFACT."""
        artifact = _artifact(tmp_path)
        write_stage_stamp(artifact, stage="s", run_id=_RUN_ID)
        artifact.unlink()
        assert validate_stage_stamp(artifact, expected_stage="s") == (
            StampVerdict.MISSING_ARTIFACT,
            None,
        )

    def test_r42_verdict_missing_stamp(self, tmp_path: Path) -> None:
        """A kill between artifact and stamp → MISSING_STAMP → rebuild (safe)."""
        artifact = _artifact(tmp_path)
        assert validate_stage_stamp(artifact, expected_stage="s") == (
            StampVerdict.MISSING_STAMP,
            None,
        )

    def test_r42_verdict_malformed_matrix(self, tmp_path: Path) -> None:
        """Bad JSON / non-object / missing keys / wrong types → MALFORMED."""
        artifact = _artifact(tmp_path)
        sidecar = stamp_path(artifact)
        for poison in (
            "{torn",
            "[1]",
            json.dumps({"stage": "s", "run_id": _RUN_ID}),  # missing content_hash
            json.dumps(
                {"stage": 1, "run_id": _RUN_ID, "content_hash": "a" * 64, "produced_at_utc": 1.0}
            ),
            json.dumps(
                {
                    "stage": "s",
                    "run_id": _RUN_ID,
                    "content_hash": "a" * 64,
                    "produced_at_utc": "now",
                }
            ),
            json.dumps(
                {"stage": "s", "run_id": _RUN_ID, "content_hash": "a" * 64, "produced_at_utc": True}
            ),
            json.dumps(
                {
                    "stage": "s",
                    "run_id": _RUN_ID,
                    "content_hash": "a" * 64,
                    "produced_at_utc": 1.0,
                    "extra": {"k": 5},
                }
            ),
        ):
            sidecar.write_text(poison, encoding="utf-8")
            assert validate_stage_stamp(artifact, expected_stage="s") == (
                StampVerdict.MALFORMED,
                None,
            )

    def test_r42_verdict_torn_artifact_hash_mismatch(self, tmp_path: Path) -> None:
        """TORN: truncating the artifact after stamping → HASH_MISMATCH."""
        artifact = _artifact(tmp_path, data=b"full artifact bytes\n")
        write_stage_stamp(artifact, stage="s", run_id=_RUN_ID)
        artifact.write_bytes(b"full art")  # torn tail
        verdict, stamp = validate_stage_stamp(artifact, expected_stage="s")
        assert verdict is StampVerdict.HASH_MISMATCH
        assert stamp is not None  # forensics: the parsed stamp comes back

    def test_r42_verdict_stale_run_id_mismatch(self, tmp_path: Path) -> None:
        """STALE: a stamp from a PREVIOUS run fails the expected_run_id check."""
        artifact = _artifact(tmp_path)
        write_stage_stamp(artifact, stage="s", run_id="00" * 32)
        verdict, stamp = validate_stage_stamp(artifact, expected_stage="s", expected_run_id=_RUN_ID)
        assert verdict is StampVerdict.HASH_MISMATCH
        assert stamp is not None
        assert stamp.run_id == "00" * 32
        # Without an expected_run_id the same stamp is OK (caller opts in).
        assert validate_stage_stamp(artifact, expected_stage="s")[0] is StampVerdict.OK

    def test_r42_verdict_swapped_stamp_mismatch(self, tmp_path: Path) -> None:
        """SWAPPED: a byte-identical artifact wearing ANOTHER stage's stamp fails
        on the stage field even though the content hash matches."""
        artifact = _artifact(tmp_path)
        write_stage_stamp(artifact, stage="cascade_report", run_id=_RUN_ID)
        verdict, stamp = validate_stage_stamp(artifact, expected_stage="stage3_report")
        assert verdict is StampVerdict.HASH_MISMATCH
        assert stamp is not None
        assert stamp.content_hash == content_hash_of(artifact)  # hash alone was fine

    def test_r42_verdict_snapshot_extra_mismatch_g7(self, tmp_path: Path) -> None:
        """G7: a stamped data_snapshot_hash differing from the live one refuses."""
        artifact = _artifact(tmp_path)
        write_stage_stamp(
            artifact, stage="cascade_manifest", run_id=_RUN_ID, extra={SNAPSHOT_EXTRA_KEY: _SNAP}
        )
        verdict, stamp = validate_stage_stamp(
            artifact,
            expected_stage="cascade_manifest",
            expected_snapshot_hash="34" * 32,
        )
        assert verdict is StampVerdict.HASH_MISMATCH
        assert stamp is not None
        assert stamp.extra[SNAPSHOT_EXTRA_KEY] == _SNAP
        ok, _ = validate_stage_stamp(
            artifact, expected_stage="cascade_manifest", expected_snapshot_hash=_SNAP
        )
        assert ok is StampVerdict.OK

    def test_r42_snapshot_absent_extra_not_failed(self, tmp_path: Path) -> None:
        """A stamp WITHOUT the snapshot extra is not failed on that axis (the
        run-id binding already folds the snapshot per D-1 — absent evidence is
        never fabricated)."""
        artifact = _artifact(tmp_path)
        write_stage_stamp(artifact, stage="s", run_id=_RUN_ID)
        verdict, _ = validate_stage_stamp(
            artifact, expected_stage="s", expected_run_id=_RUN_ID, expected_snapshot_hash=_SNAP
        )
        assert verdict is StampVerdict.OK

    def test_r42_always_recomputes_hash(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """I-5: EVERY validation recomputes the streamed hash (spy-pinned)."""
        artifact = _artifact(tmp_path)
        write_stage_stamp(artifact, stage="s", run_id=_RUN_ID)

        calls: list[Path] = []
        real = content_hash_of  # the top-level from-import — the real function

        def _spy(path: Path) -> str:
            calls.append(path)
            return real(path)

        # String-target monkeypatch (the #1810 CodeQL convention).
        monkeypatch.setattr("brutex.observability.stage_stamp.content_hash_of", _spy)
        # Even a stamp that will fail on STAGE (a swapped stamp) is re-hashed.
        assert (
            validate_stage_stamp(artifact, expected_stage="other")[0] is StampVerdict.HASH_MISMATCH
        )
        assert validate_stage_stamp(artifact, expected_stage="s")[0] is StampVerdict.OK
        assert calls == [artifact, artifact]

    def test_r42_unreadable_artifact_raises_loud(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An artifact that exists as a FILE but cannot be hashed raises loudly
        (an infrastructure defect, not a stamp verdict). The read failure is
        injected (chmod is invisible to a root test runner)."""
        # DEC-LAKE-READ-INODE-GUARD-001: the hash now opens the artifact via
        # the guarded `open_regular_binary` seam (os.open based, never
        # builtins.open) — fault it there so the loud-raise teeth stay real,
        # via the string-target monkeypatch (the #1810 CodeQL convention).
        # The delegate is guarded_read's identity-pinned object — the SAME
        # function stage_stamp binds (`from brutex.data.guarded_read import`).
        from brutex.data.guarded_read import open_regular_binary as real_open

        artifact = _artifact(tmp_path)
        write_stage_stamp(artifact, stage="s", run_id=_RUN_ID)

        def _boom(path: object, *args: object, **kwargs: object) -> object:
            if str(path) == str(artifact):
                raise OSError("I/O error")
            return real_open(path, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr("brutex.observability.stage_stamp.open_regular_binary", _boom)
        with pytest.raises(StageStampError, match="cannot hash artifact"):
            validate_stage_stamp(artifact, expected_stage="s")

    def test_r42_read_stage_stamp_strict(self, tmp_path: Path) -> None:
        """read_stage_stamp parses strictly; missing/garbage raise."""
        artifact = _artifact(tmp_path)
        with pytest.raises(StageStampError, match="cannot read stamp"):
            read_stage_stamp(artifact)
        write_stage_stamp(artifact, stage="s", run_id=_RUN_ID)
        stamp = read_stage_stamp(artifact)
        assert stamp == StageStamp(
            stage="s",
            run_id=_RUN_ID,
            content_hash=content_hash_of(artifact),
            produced_at_utc=stamp.produced_at_utc,
            extra={},
        )
        stamp_path(artifact).write_text("{garbage", encoding="utf-8")
        with pytest.raises(StageStampError, match="malformed stamp JSON"):
            read_stage_stamp(artifact)


class TestStage3CellHelpers:
    def test_r42_stage3_cell_batch_helpers(self, tmp_path: Path) -> None:
        """D-4: stamp_stage3_cells stamps every cell; validate_stage3_cells
        returns EXACTLY the invalid ones (empty tuple == all consumable)."""
        cells = [_artifact(tmp_path, f"cell-{i}.parquet", f"cell {i}".encode()) for i in range(3)]
        assert stamp_stage3_cells(cells, run_id=_RUN_ID) == 3
        for cell in cells:
            assert read_stage_stamp(cell).stage == STAGE3_CELL_STAGE == "stage3_cell"
        assert validate_stage3_cells(cells, run_id=_RUN_ID, snapshot_hash=_SNAP) == ()
        cells[1].write_bytes(b"poisoned")  # corrupt one cell
        stamp_path(cells[2]).unlink()  # unstamp another
        invalid = validate_stage3_cells(cells, run_id=_RUN_ID, snapshot_hash=_SNAP)
        assert invalid == (cells[1], cells[2])
        # A wrong run id invalidates EVERY cell (stale-run stamps).
        assert validate_stage3_cells([cells[0]], run_id="00" * 32, snapshot_hash=_SNAP) == (
            cells[0],
        )
        # A missing cell propagates the write refusal (fail-loud batch).
        with pytest.raises(StageStampError, match="does not exist"):
            stamp_stage3_cells([tmp_path / "ghost.parquet"], run_id=_RUN_ID)
        assert stamp_stage3_cells([], run_id=_RUN_ID) == 0

    def test_r42_stage3_cells_with_snapshot_extra(self, tmp_path: Path) -> None:
        """The optional snapshot_hash kw binds the extra; a lake change then
        fails validation on the snapshot axis (G7 teeth for cells)."""
        cell = _artifact(tmp_path, "cell.parquet", b"cell bytes")
        assert stamp_stage3_cells([cell], run_id=_RUN_ID, snapshot_hash=_SNAP) == 1
        assert read_stage_stamp(cell).extra == {SNAPSHOT_EXTRA_KEY: _SNAP}
        assert validate_stage3_cells([cell], run_id=_RUN_ID, snapshot_hash=_SNAP) == ()
        assert validate_stage3_cells([cell], run_id=_RUN_ID, snapshot_hash="34" * 32) == (cell,)


class TestDiscardAndFlag:
    def test_r42_discard_and_flag_forensic_rename(self, tmp_path: Path) -> None:
        """Both artifact and stamp are RENAMED (never deleted) and ONE
        QUARANTINE record lands on the flight recorder."""
        run_dir = tmp_path / "runs" / _RUN_ID
        run_dir.mkdir(parents=True)
        artifact = _artifact(run_dir, "cascade-report.json")
        write_stage_stamp(artifact, stage="cascade_report", run_id=_RUN_ID)
        recorder = FlightRecorder.open(tmp_path, run_id=_RUN_ID, attempt=1)
        discard_and_flag(artifact, reason="stamp hash_mismatch", recorder=recorder)
        recorder.close()
        assert not artifact.exists()
        assert not stamp_path(artifact).exists()
        discarded = artifact.with_name("cascade-report.json.discarded.0")
        assert discarded.exists()  # forensics kept, never deleted
        assert stamp_path(artifact).with_name("cascade-report.json.stamp.json.discarded.0").exists()
        records = [r for r in iter_run_records(tmp_path, _RUN_ID) if r["kind"] == "QUARANTINE"]
        assert len(records) == 1
        payload = records[0]["payload"]
        assert isinstance(payload, dict)
        assert payload["what"] == "stage_artifact"
        assert payload["path_rel"] == "cascade-report.json"
        assert payload["reason"] == "stamp hash_mismatch"
        assert payload["discard_ordinal"] == 0
        assert payload["moved"] == [
            "cascade-report.json.discarded.0",
            "cascade-report.json.stamp.json.discarded.0",
        ]

    def test_r42_discard_monotonic_ordinal_and_partials(self, tmp_path: Path) -> None:
        """Repeated discards pick the next free ordinal; a stamp-only or
        artifact-only pair discards the present side; recorder=None is fine."""
        artifact = _artifact(tmp_path, "cell.parquet", b"v1")
        write_stage_stamp(artifact, stage="s", run_id=_RUN_ID)
        discard_and_flag(artifact, reason="first", recorder=None)
        artifact.write_bytes(b"v2")  # rebuilt artifact, NO stamp this time
        discard_and_flag(artifact, reason="second", recorder=None)
        assert (tmp_path / "cell.parquet.discarded.0").read_bytes() == b"v1"
        assert (tmp_path / "cell.parquet.discarded.1").read_bytes() == b"v2"
        assert (tmp_path / "cell.parquet.stamp.json.discarded.0").exists()
        assert not (tmp_path / "cell.parquet.stamp.json.discarded.1").exists()
        # Stamp-only remnant (artifact already gone): the stamp still moves.
        ghost = tmp_path / "gone.parquet"
        _artifact(tmp_path, "gone.parquet", b"tmp")
        write_stage_stamp(ghost, stage="s", run_id=_RUN_ID)
        ghost.unlink()
        discard_and_flag(ghost, reason="stamp only", recorder=None)
        assert (tmp_path / "gone.parquet.stamp.json.discarded.0").exists()

    def test_r42_discard_rename_failure_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A rename failure is a loud infrastructure defect."""
        artifact = _artifact(tmp_path)

        def _boom(self: Path, target: Path) -> Path:
            raise OSError("read-only volume")

        monkeypatch.setattr(Path, "rename", _boom)
        with pytest.raises(StageStampError, match="failed to discard"):
            discard_and_flag(artifact, reason="r", recorder=None)


# ────────────────────────────────────────────────────────────────────────
# DEC-RUST-SWEEP-OPS-001 — flag-ON routing parity (wheel-free fake crate)
# ────────────────────────────────────────────────────────────────────────


class TestSweepOpsRouting:
    """Flag-ON routing parity for the ``validate_stage_stamp`` verdict law.

    Wheel-free: the shared behavior-correct FAKE crate is injected by
    patching the facade's ``importlib`` binding. Flag ON must be
    verdict-identical to the flag-OFF Python law (which every OTHER test in
    this file drives) — the I/O-derived MISSING/MALFORMED verdicts never
    reach the routed decision on either arm.
    """

    @staticmethod
    def _arm_fake_crate(monkeypatch: pytest.MonkeyPatch) -> None:
        from types import SimpleNamespace

        from brutex.engine import sweep_ops_rust

        from tests.fakes.sweep_ops_crate import build_fake_sweep_ops_crate

        crate = build_fake_sweep_ops_crate(version="fake")
        monkeypatch.setattr(
            sweep_ops_rust,
            "importlib",
            SimpleNamespace(import_module=lambda name: crate),
        )
        monkeypatch.setenv(sweep_ops_rust.SWEEP_OPS_RUST_ENV, "1")
        sweep_ops_rust.reset_sweep_ops_rust_for_tests()

    @staticmethod
    def _reset_facade() -> None:
        from brutex.engine.sweep_ops_rust import reset_sweep_ops_rust_for_tests

        reset_sweep_ops_rust_for_tests()

    def test_flag_on_matches_python_law_verdict_matrix(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ok = tmp_path / "ok.bin"
        ok.write_bytes(b"payload")
        write_stage_stamp(ok, stage="sweep", run_id="r1", extra={SNAPSHOT_EXTRA_KEY: "snap-a"})
        torn = tmp_path / "torn.bin"
        torn.write_bytes(b"payload")
        write_stage_stamp(torn, stage="sweep", run_id="r1")
        torn.write_bytes(b"tampered")
        missing_stamp = tmp_path / "nostamp.bin"
        missing_stamp.write_bytes(b"payload")

        def verdicts() -> list[StampVerdict]:
            return [
                validate_stage_stamp(
                    ok,
                    expected_stage="sweep",
                    expected_run_id="r1",
                    expected_snapshot_hash="snap-a",
                )[0],
                validate_stage_stamp(ok, expected_stage="stage3_cell")[0],
                validate_stage_stamp(ok, expected_stage="sweep", expected_run_id="r2")[0],
                validate_stage_stamp(ok, expected_stage="sweep", expected_snapshot_hash="snap-b")[
                    0
                ],
                validate_stage_stamp(torn, expected_stage="sweep")[0],
                validate_stage_stamp(missing_stamp, expected_stage="sweep")[0],
                validate_stage_stamp(tmp_path / "absent.bin", expected_stage="sweep")[0],
            ]

        off = verdicts()
        assert off == [
            StampVerdict.OK,
            StampVerdict.HASH_MISMATCH,
            StampVerdict.HASH_MISMATCH,
            StampVerdict.HASH_MISMATCH,
            StampVerdict.HASH_MISMATCH,
            StampVerdict.MISSING_STAMP,
            StampVerdict.MISSING_ARTIFACT,
        ]
        self._arm_fake_crate(monkeypatch)
        try:
            assert verdicts() == off
        finally:
            self._reset_facade()
