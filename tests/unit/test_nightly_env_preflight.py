"""Unit matrix for the §12 removed-env-flag pre-flight (DEC-BOX-AUTODEPLOY-LANE-001)."""

from __future__ import annotations

import os
import signal
import threading
from pathlib import Path

import pytest
from brutex.config.dotenv import load_dotenv_file
from brutex.nightly.env_preflight import (
    REMOVED_ENV_FLAGS,
    EnvPreflightOutcome,
    run_env_preflight,
)
from structlog.testing import capture_logs


def _flag_names() -> set[str]:
    return {row.name for row in REMOVED_ENV_FLAGS}


class TestRegistry:
    def test_registry_carries_the_two_designed_rows(self) -> None:
        by_name = {row.name: row for row in REMOVED_ENV_FLAGS}
        assert by_name["BRUTEX_FILL_MODE"].tier == "removed"
        assert "DEC-FILL-WORST-CASE-ONLY-001" in by_name["BRUTEX_FILL_MODE"].removed_by
        assert by_name["BRUTEX_SKIP_WHEEL_FRESHNESS"].tier == "active_escape"

    def test_every_row_has_remediation_text(self) -> None:
        # §10 test 14(f) — a future retired flag cannot ship without remediation.
        for row in REMOVED_ENV_FLAGS:
            assert row.remediation.strip(), row.name
            assert row.removed_by.strip(), row.name
            assert row.tier in ("removed", "active_escape"), row.name

    def test_fill_mode_registry_row_matches_the_live_refuser(self) -> None:
        # The motivating case: the flag the registry names IS the launch-refuser.
        from brutex.execution.fill_mode import FILL_MODE_ENV

        assert FILL_MODE_ENV in _flag_names()

    def test_reader_default_is_the_dotenv_module_reader(self) -> None:
        # §10 test 14(i) identity pin — the is_file()-guarded reader is REUSED,
        # never copied (a raw read_text on a FIFO would hang the nightly).
        import inspect

        sig = inspect.signature(run_env_preflight)
        assert sig.parameters["load_file"].default is load_dotenv_file


class TestScan:
    def test_process_env_hit(self) -> None:
        with capture_logs() as logs:
            out = run_env_preflight(env={"BRUTEX_FILL_MODE": "sekrit-value-123"}, dotenv_paths=[])
        assert len(out.flagged) == 1
        f = out.flagged[0]
        assert f.name == "BRUTEX_FILL_MODE"
        assert f.source == "process_env"
        assert "DEC-FILL-WORST-CASE-ONLY-001" in f.remediation
        warns = [e for e in logs if e["event"] == "env_preflight.removed_flag_set"]
        assert len(warns) == 1
        # Values are NEVER logged (law 3) — the distinctive value string
        # must appear nowhere in the emitted event.
        assert "sekrit-value-123" not in str(warns[0])
        assert "value" not in warns[0]

    def test_dotenv_hit_names_the_exact_file(self, tmp_path: Path) -> None:
        dotenv = tmp_path / ".env"
        dotenv.write_text("BRUTEX_FILL_MODE=worst_case\n", encoding="utf-8")
        out = run_env_preflight(env={}, dotenv_paths=[dotenv])
        assert [f.source for f in out.flagged] == [str(dotenv)]

    def test_both_sources_flag_both(self, tmp_path: Path) -> None:
        # §10 test 14(c) — process env does not MASK the lingering file (the
        # file is what survives the next reboot).
        dotenv = tmp_path / ".env"
        dotenv.write_text("BRUTEX_FILL_MODE=x\n", encoding="utf-8")
        out = run_env_preflight(env={"BRUTEX_FILL_MODE": "x"}, dotenv_paths=[dotenv])
        assert [f.source for f in out.flagged] == ["process_env", str(dotenv)]

    def test_clean_scan_is_empty_and_silent(self, tmp_path: Path) -> None:
        dotenv = tmp_path / ".env"
        dotenv.write_text("OTHER=1\n", encoding="utf-8")
        with capture_logs() as logs:
            out = run_env_preflight(env={"HOME": "/x"}, dotenv_paths=[dotenv])
        assert out.flagged == ()
        assert out.dotenv_errors == ()
        assert logs == []

    def test_active_escape_tier_renders(self) -> None:
        # §10 test 14(j).
        out = run_env_preflight(env={"BRUTEX_SKIP_WHEEL_FRESHNESS": "1"}, dotenv_paths=[])
        assert out.flagged[0].tier == "active_escape"
        assert "gate" in out.flagged[0].remediation
        block = out.to_dict()
        flagged = block["flagged"]
        assert isinstance(flagged, list)
        assert flagged[0]["tier"] == "active_escape"

    def test_unreadable_dotenv_degrades_named(self, tmp_path: Path) -> None:
        # §10 test 14(e) — a raising loader folds into dotenv_errors; the
        # OTHER candidates still scan; never a raise.
        bad = tmp_path / "bad.env"
        good = tmp_path / "good.env"
        good.write_text("BRUTEX_FILL_MODE=1\n", encoding="utf-8")

        def _loader(path: Path) -> dict[str, str]:
            if path == bad:
                raise OSError("planted read failure")
            return load_dotenv_file(path)

        with capture_logs() as logs:
            out = run_env_preflight(env={}, dotenv_paths=[bad, good], load_file=_loader)
        assert out.dotenv_errors == (f"{bad}: OSError",)
        assert [f.source for f in out.flagged] == [str(good)]
        assert any(e["event"] == "env_preflight.dotenv_unreadable" for e in logs)

    def test_directory_at_candidate_path_is_benign(self, tmp_path: Path) -> None:
        # is_file() guard: a directory reads {} — no error, no finding.
        out = run_env_preflight(env={}, dotenv_paths=[tmp_path])
        assert out.flagged == () and out.dotenv_errors == ()

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX only")
    def test_fifo_at_candidate_path_never_hangs(self, tmp_path: Path) -> None:
        # §10 test 14(i) — a planted FIFO neither hangs nor raises (the
        # is_file() guard fires BEFORE any read); SIGALRM watchdog.
        fifo = tmp_path / ".env"
        os.mkfifo(fifo)
        fired = threading.Event()

        def _abort(_sig: int, _frame: object) -> None:  # pragma: no cover - watchdog
            fired.set()
            raise TimeoutError("env preflight hung on a FIFO")

        old = signal.signal(signal.SIGALRM, _abort)
        signal.alarm(5)
        try:
            out = run_env_preflight(env={}, dotenv_paths=[fifo])
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old)
        assert not fired.is_set()
        assert out.flagged == () and out.dotenv_errors == ()

    def test_block_shape_stable_keys(self) -> None:
        out = EnvPreflightOutcome(flagged=(), dotenv_errors=())
        assert out.to_dict() == {"flagged": [], "dotenv_errors": []}
        assert list(out.to_dict()) == ["flagged", "dotenv_errors"]
