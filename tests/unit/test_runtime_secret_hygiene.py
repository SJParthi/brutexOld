"""runtime.json secret hygiene tests (GROUP 4 · Task 6a, R2-CS-1).

* ``BRUTEX_*CLIENT_ID*`` env vars are redacted in the written snapshot
  (the pattern previously missed CLIENT_ID).
* ``runtime.json`` is written owner-only (0600), matching the credential
  store — previously it landed with the umask default (0644).
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
from brutex.config.runtime import (
    RuntimeInfo,
    _redact_env_snapshot,  # pyright: ignore[reportPrivateUsage]
    build_runtime_info,
    write_runtime_file,
)


@pytest.fixture
def sample_info() -> RuntimeInfo:
    return RuntimeInfo(
        pid=4321,
        run_id="run-abc",
        started_at="2026-07-03T00:00:00Z",
        host_bind="127.0.0.1",
        dashboard_urls=("http://127.0.0.1:9080",),
        metrics_url="http://127.0.0.1:9080/metrics",
        phase="idle",
        env_snapshot=(),
        environment="terminal",
    )


class TestClientIdRedaction:
    def test_client_id_redacted(self) -> None:
        out = _redact_env_snapshot({"BRUTEX_CLIENT_ID": "cid-123456"})
        assert out == {"BRUTEX_CLIENT_ID": "<redacted>"}

    def test_client_id_redacted_case_insensitive(self) -> None:
        out = _redact_env_snapshot({"BRUTEX_GROWW_client_id": "cid-xyz"})
        assert out == {"BRUTEX_GROWW_client_id": "<redacted>"}

    def test_non_secret_still_kept(self) -> None:
        out = _redact_env_snapshot({"BRUTEX_LAKE_ROOT": "/data/lake"})
        assert out == {"BRUTEX_LAKE_ROOT": "/data/lake"}

    def test_client_id_never_lands_in_written_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BRUTEX_CLIENT_ID", "cid-should-not-leak")
        info = build_runtime_info(
            pid=1,
            run_id="r",
            host_bind="127.0.0.1",
            dashboard_urls=["http://127.0.0.1:9080"],
            metrics_url="http://127.0.0.1:9080/metrics",
            phase="idle",
            environment="terminal",
        )
        target = tmp_path / "runtime.json"
        write_runtime_file(info, path=target)
        raw = target.read_text(encoding="utf-8")
        assert "cid-should-not-leak" not in raw
        assert json.loads(raw)["env_snapshot"]["BRUTEX_CLIENT_ID"] == "<redacted>"


class TestRuntimeFileMode:
    def test_written_file_is_owner_only_0600(
        self, tmp_path: Path, sample_info: RuntimeInfo
    ) -> None:
        target = tmp_path / "runtime.json"
        write_runtime_file(sample_info, path=target)
        mode = stat.S_IMODE(target.stat().st_mode)
        assert mode == 0o600

    def test_rewrite_keeps_owner_only_mode(self, tmp_path: Path, sample_info: RuntimeInfo) -> None:
        """A periodic phase-update rewrite must not widen the mode."""
        target = tmp_path / "runtime.json"
        write_runtime_file(sample_info, path=target)
        write_runtime_file(sample_info, path=target)
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
