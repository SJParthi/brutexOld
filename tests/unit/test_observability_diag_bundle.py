"""Unit suite for :mod:`brutex.observability.diag_bundle` (R40, D-16).

Pins bundle content (run-dir pass + extras + log tail + env fingerprint +
meta), the D-16 TOKEN/SECRET/KEY value redaction, the fail-soft law (WARN +
``None``, per-member skip), atomicity, and self/temp exclusion.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Final

import pytest
from brutex.observability.diag_bundle import (
    BUNDLE_FILENAME,
    REDACTED_VALUE,
    build_diagnostic_bundle,
    bundle_path,
    redact_env,
)
from structlog.testing import capture_logs

#: D-6 mechanized traceability — test name → requirement/gap tokens.
TRACEABILITY: Final[dict[str, tuple[str, ...]]] = {
    "test_r40_bundle_path_law": ("R40",),
    "test_r40_bundle_contains_run_dir_extras_logtail_fingerprint_meta": ("R40",),
    "test_r40_redaction_token_secret_key_values": ("R40",),
    "test_r40_fingerprint_hardware_key_best_effort": ("R40",),
    "test_r40_member_read_failure_skipped_and_listed": ("R40",),
    "test_r40_missing_extra_file_skipped": ("R40",),
    "test_r40_excludes_prior_bundle_and_temp_files": ("R40",),
    "test_r40_missing_run_dir_still_bundles_fingerprint": ("R40",),
    "test_r40_whole_build_failure_warns_none": ("R40",),
    "test_r40_log_tail_bounded": ("R40",),
    "test_r40_rewrite_is_atomic_no_temp_remnant": ("R40",),
}

_RUN_ID: Final[str] = "aa" * 32


def _seed_run_dir(tmp_path: Path) -> Path:
    run_dir = tmp_path / "runs" / _RUN_ID
    (run_dir / "sub").mkdir(parents=True)
    (run_dir / "manifest.json").write_text('{"run_id": "x"}\n', encoding="utf-8")
    (run_dir / "flight-recorder.jsonl").write_text('{"seq": 0}\n', encoding="utf-8")
    (run_dir / "sub" / "cell.stamp.json").write_text("{}\n", encoding="utf-8")
    return run_dir


def _names(bundle: Path) -> list[str]:
    with zipfile.ZipFile(bundle) as archive:
        return sorted(archive.namelist())


def _member(bundle: Path, name: str) -> bytes:
    with zipfile.ZipFile(bundle) as archive:
        return archive.read(name)


class TestRedaction:
    def test_r40_redaction_token_secret_key_values(self) -> None:
        """D-16: names containing TOKEN/SECRET/KEY (any case) redact their
        VALUES; other BRUTEX_* values pass; non-BRUTEX names are excluded."""
        env = {
            "BRUTEX_TELEGRAM_BOT_TOKEN": "123:abc",
            "BRUTEX_TOTP_secret": "s3cr3t",
            "BRUTEX_API_KEY": "k",
            "BRUTEX_BENCH_BASELINE_KEY": "mac16-8-14c10p4e-48g-arm64",
            "BRUTEX_SWEEP_WORKERS": "4",
            "BRUTEX_LAKE_ROOT": "/lake",
            "PATH": "/usr/bin",
            "AWS_SECRET_ACCESS_KEY": "never-even-listed",
        }
        redacted = redact_env(env)
        assert redacted == {
            "BRUTEX_API_KEY": REDACTED_VALUE,
            "BRUTEX_BENCH_BASELINE_KEY": REDACTED_VALUE,
            "BRUTEX_LAKE_ROOT": "/lake",
            "BRUTEX_SWEEP_WORKERS": "4",
            "BRUTEX_TELEGRAM_BOT_TOKEN": REDACTED_VALUE,
            "BRUTEX_TOTP_secret": REDACTED_VALUE,
        }
        assert list(redacted.keys()) == sorted(redacted.keys())  # deterministic order
        assert REDACTED_VALUE == "[REDACTED]"
        assert "123:abc" not in json.dumps(redacted)
        assert "s3cr3t" not in json.dumps(redacted)


class TestBuild:
    def test_r40_bundle_path_law(self, tmp_path: Path) -> None:
        assert bundle_path(tmp_path, _RUN_ID) == tmp_path / "runs" / _RUN_ID / BUNDLE_FILENAME
        assert BUNDLE_FILENAME == "diagnostic-bundle.zip"

    def test_r40_bundle_contains_run_dir_extras_logtail_fingerprint_meta(
        self, tmp_path: Path
    ) -> None:
        """The one-pass bundle carries every run-dir file (recursive), the
        extras, the bounded log tail, the fingerprint, and the meta."""
        _seed_run_dir(tmp_path)
        log_file = tmp_path / "brutex.log"
        log_file.write_text("line-1\nline-2\nline-3\n", encoding="utf-8")
        checkpoint = tmp_path / "checkpoint.json"
        checkpoint.write_text('{"resume_from": 7}\n', encoding="utf-8")
        bundle = build_diagnostic_bundle(
            tmp_path,
            run_id=_RUN_ID,
            log_file=log_file,
            extra_files=[checkpoint],
            env={"BRUTEX_SWEEP_WORKERS": "8", "BRUTEX_TELEGRAM_BOT_TOKEN": "tok"},
        )
        assert bundle == bundle_path(tmp_path, _RUN_ID)
        assert bundle is not None and bundle.exists()
        assert _names(bundle) == [
            "bundle-meta.json",
            "env-fingerprint.json",
            "extra/checkpoint.json",
            "flight-recorder.jsonl",
            "log-tail.txt",
            "manifest.json",
            "sub/cell.stamp.json",
        ]
        assert _member(bundle, "extra/checkpoint.json") == b'{"resume_from": 7}\n'
        assert _member(bundle, "log-tail.txt") == b"line-1\nline-2\nline-3\n"
        fingerprint = json.loads(_member(bundle, "env-fingerprint.json"))
        assert set(fingerprint.keys()) == {"hardware_key", "python", "platform", "brutex_env"}
        assert fingerprint["brutex_env"] == {
            "BRUTEX_SWEEP_WORKERS": "8",
            "BRUTEX_TELEGRAM_BOT_TOKEN": REDACTED_VALUE,
        }
        meta = json.loads(_member(bundle, "bundle-meta.json"))
        assert meta == {"run_id": _RUN_ID, "member_count": 7, "skipped": []}

    def test_r40_fingerprint_hardware_key_best_effort(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A hardware_key probe failure degrades to 'unknown' + WARN, never fails
        the bundle (fail-soft, never a fabricated key)."""
        _seed_run_dir(tmp_path)
        import brutex.observability.diag_bundle as mod

        def _boom() -> str:
            raise ValueError("bad override")

        monkeypatch.setattr(mod, "hardware_key", _boom)
        with capture_logs() as logs:
            bundle = build_diagnostic_bundle(tmp_path, run_id=_RUN_ID, env={})
        assert bundle is not None
        assert json.loads(_member(bundle, "env-fingerprint.json"))["hardware_key"] == "unknown"
        assert [e for e in logs if e["event"] == "diag_bundle.hardware_key_failed"]

    def test_r40_member_read_failure_skipped_and_listed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unreadable member is skipped + WARNed + honestly listed in meta."""
        run_dir = _seed_run_dir(tmp_path)
        poison = run_dir / "manifest.json"
        real_read_bytes = Path.read_bytes

        def _boom(self: Path) -> bytes:
            if self == poison:
                raise OSError("I/O error")
            return real_read_bytes(self)

        monkeypatch.setattr(Path, "read_bytes", _boom)
        with capture_logs() as logs:
            bundle = build_diagnostic_bundle(tmp_path, run_id=_RUN_ID, env={})
        assert bundle is not None
        assert "manifest.json" not in _names(bundle)
        assert json.loads(_member(bundle, "bundle-meta.json"))["skipped"] == ["manifest.json"]
        warns = [e for e in logs if e["event"] == "diag_bundle.member_skipped"]
        assert len(warns) == 1
        assert warns[0]["member"] == "manifest.json"

    def test_r40_missing_extra_file_skipped(self, tmp_path: Path) -> None:
        """A missing extra file is skipped + listed — never fatal."""
        _seed_run_dir(tmp_path)
        with capture_logs() as logs:
            bundle = build_diagnostic_bundle(
                tmp_path, run_id=_RUN_ID, extra_files=[tmp_path / "ghost.json"], env={}
            )
        assert bundle is not None
        meta = json.loads(_member(bundle, "bundle-meta.json"))
        assert meta["skipped"] == ["extra/ghost.json"]
        assert [e for e in logs if e["event"] == "diag_bundle.member_skipped"]

    def test_r40_excludes_prior_bundle_and_temp_files(self, tmp_path: Path) -> None:
        """A rebuild never bundles the previous bundle nor an in-flight temp."""
        run_dir = _seed_run_dir(tmp_path)
        (run_dir / ".brutex-write-abc123.parquet.tmp").write_bytes(b"half")
        first = build_diagnostic_bundle(tmp_path, run_id=_RUN_ID, env={})
        assert first is not None
        second = build_diagnostic_bundle(tmp_path, run_id=_RUN_ID, env={})
        assert second is not None
        names = _names(second)
        assert BUNDLE_FILENAME not in names
        assert all(not n.startswith(".brutex-write-") for n in names)

    def test_r40_missing_run_dir_still_bundles_fingerprint(self, tmp_path: Path) -> None:
        """No run dir yet (a refusal at launch) → the bundle still lands with
        the fingerprint + meta (never a crash, never None for this case)."""
        bundle = build_diagnostic_bundle(tmp_path, run_id=_RUN_ID, env={})
        assert bundle is not None
        assert _names(bundle) == ["bundle-meta.json", "env-fingerprint.json"]

    def test_r40_whole_build_failure_warns_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A zip-write failure → ONE diag_bundle.build_failed WARN + None (I-3)."""
        _seed_run_dir(tmp_path)
        import brutex.observability.diag_bundle as mod

        def _boom(dest: Path, write_to: object) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(mod, "atomic_write_simple", _boom)
        with capture_logs() as logs:
            assert build_diagnostic_bundle(tmp_path, run_id=_RUN_ID, env={}) is None
        warns = [e for e in logs if e["event"] == "diag_bundle.build_failed"]
        assert len(warns) == 1
        assert warns[0]["error_type"] == "OSError"

    def test_r40_log_tail_bounded(self, tmp_path: Path) -> None:
        """max_log_lines bounds the tail (clamped by the COMMON law); an empty /
        missing log adds no member."""
        _seed_run_dir(tmp_path)
        log_file = tmp_path / "big.log"
        log_file.write_text("".join(f"line-{i}\n" for i in range(50)), encoding="utf-8")
        bundle = build_diagnostic_bundle(
            tmp_path, run_id=_RUN_ID, log_file=log_file, max_log_lines=3, env={}
        )
        assert bundle is not None
        assert _member(bundle, "log-tail.txt") == b"line-47\nline-48\nline-49\n"
        missing = build_diagnostic_bundle(
            tmp_path, run_id=_RUN_ID, log_file=tmp_path / "no.log", env={}
        )
        assert missing is not None
        assert "log-tail.txt" not in _names(missing)

    def test_r40_rewrite_is_atomic_no_temp_remnant(self, tmp_path: Path) -> None:
        """Rebuilding over an existing bundle leaves exactly one zip, no temps."""
        run_dir = _seed_run_dir(tmp_path)
        assert build_diagnostic_bundle(tmp_path, run_id=_RUN_ID, env={}) is not None
        assert build_diagnostic_bundle(tmp_path, run_id=_RUN_ID, env={}) is not None
        leftovers = [p.name for p in run_dir.iterdir() if p.name.startswith(".brutex-write-")]
        assert leftovers == []
        assert zipfile.is_zipfile(bundle_path(tmp_path, _RUN_ID))
