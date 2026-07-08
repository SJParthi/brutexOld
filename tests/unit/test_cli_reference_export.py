# pyright: reportPrivateUsage=none
"""CLI wiring tests for the post-pull S3 reference export (DEC-REFERENCE-EXPORT-S3-001).

Pins the cli.py surface that the pragma'd composition shims
(``_run_cli_reference_export`` / ``cmd_export_reference`` / the real boto3 build)
sit above:

- ``cmd_fetch`` Seam B fires the export EXACTLY when enabled AND persisting, and
  is inert on ``--dry-run`` (``_no_persist``) and when the gate is off (default);
- a junk ``BRUTEX_REFERENCE_EXPORT`` fails ``EXIT_USER_ERROR`` BEFORE any vendor
  work (the fail-loud env convention);
- the ``export-reference`` subcommand dispatches to ``cmd_export_reference`` with
  the parsed ``--lake-root`` / ``--date``.

Reuses the proven ``test_cli_greeks_enrich`` live-wiring fakes (creds / instrument
resolve / adapter build / ingest engine all faked — no network, no Groww creds).
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, Any

import pytest
from brutex import cli

from tests.unit.test_cli_greeks_enrich import _fetch, _wire_live_fakes

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def _lake_deps() -> None:
    """``cmd_fetch``'s function-body imports need the P2 lake extras."""
    pytest.importorskip("polars")
    pytest.importorskip("duckdb")
    pytest.importorskip("pyarrow")


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear both fetch-seam env gates so the ambient shell can never flip a test."""
    monkeypatch.delenv("BRUTEX_REFERENCE_EXPORT", raising=False)
    monkeypatch.delenv("BRUTEX_REFERENCE_EXPORT_BUCKET", raising=False)
    monkeypatch.delenv("BRUTEX_REFERENCE_EXPORT_REGION", raising=False)
    monkeypatch.delenv("BRUTEX_CLI_GREEKS_ENRICH", raising=False)
    monkeypatch.delenv("BRUTEX_RISK_FREE_CSV", raising=False)


def _spy_export(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Replace ``cli._run_cli_reference_export`` with a kwargs recorder."""
    calls: list[dict[str, Any]] = []

    def _recorder(**kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(cli, "_run_cli_reference_export", _recorder)
    return calls


def test_seam_b_fires_on_enabled_persisting_pull(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Enabled + persisting → the export seam runs once for the pull's end day."""
    monkeypatch.setenv("BRUTEX_REFERENCE_EXPORT", "1")
    _wire_live_fakes(monkeypatch)
    calls = _spy_export(monkeypatch)

    rc = _fetch(tmp_path)

    assert rc == cli.EXIT_OK
    assert len(calls) == 1
    assert calls[0]["lake_root"] == tmp_path
    assert calls[0]["export_date"] == date(2024, 1, 3)  # the pull's end day


def test_seam_b_inert_on_dry_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Enabled but ``--dry-run`` (``_no_persist``) → the export seam never fires."""
    monkeypatch.setenv("BRUTEX_REFERENCE_EXPORT", "1")
    _wire_live_fakes(monkeypatch)
    calls = _spy_export(monkeypatch)

    rc = _fetch(tmp_path, dry_run=True)

    assert rc == cli.EXIT_OK
    assert calls == []


def test_seam_b_inert_when_disabled_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default (gate unset) persisting pull → the export seam never fires."""
    _wire_live_fakes(monkeypatch)
    calls = _spy_export(monkeypatch)

    rc = _fetch(tmp_path)

    assert rc == cli.EXIT_OK
    assert calls == []


def test_junk_gate_fails_user_error_before_vendor_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A junk ``BRUTEX_REFERENCE_EXPORT`` exits ``EXIT_USER_ERROR`` pre-pull."""
    monkeypatch.setenv("BRUTEX_REFERENCE_EXPORT", "maybe")
    # No live wiring: the junk gate raises inside cmd_fetch's early try BEFORE any
    # instrument resolve / adapter build, so the pull is never reached.
    calls = _spy_export(monkeypatch)

    rc = _fetch(tmp_path)

    assert rc == cli.EXIT_USER_ERROR
    assert calls == []


def test_dispatch_export_reference_threads_lake_and_date(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``export-reference`` dispatches to ``cmd_export_reference`` with parsed args."""
    seen: dict[str, Any] = {}

    def _fake_cmd(lake_root: Any, *, date_str: str | None) -> int:
        seen["lake_root"] = lake_root
        seen["date_str"] = date_str
        return cli.EXIT_OK

    monkeypatch.setattr(cli, "cmd_export_reference", _fake_cmd)

    rc = cli.main(["export-reference", "--lake-root", str(tmp_path), "--date", "2026-07-07"])

    assert rc == cli.EXIT_OK
    assert seen["lake_root"] == tmp_path
    assert seen["date_str"] == "2026-07-07"


def test_seam_b_boto3_absent_is_fenced_pull_stays_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing boto3 (``build_real_s3_uploader`` raises) NEVER fails the pull.

    Regression for the fence gap (hostile-review MAJOR): the real S3 client build
    sits OUTSIDE ``run_reference_export_seam``'s fence, so a boto3-absent box would
    have raised ``ReferenceExportConfigError`` straight out of ``cmd_fetch`` Seam B
    AFTER a fully-persisted pull — a red exit + traceback on a green pull. Drives
    the REAL ``_run_cli_reference_export`` guarded path (deliberately NOT spy-mocked
    so the boto3-absent branch is genuinely covered) and asserts the completed pull
    still returns ``EXIT_OK`` with ONE ``reference_export.failed`` WARN + ONE
    best-effort alert.
    """
    import structlog
    from brutex.export.reference_export import ReferenceExportConfigError

    monkeypatch.setenv("BRUTEX_REFERENCE_EXPORT", "1")
    monkeypatch.setenv("BRUTEX_REFERENCE_EXPORT_BUCKET", "tickvault-reference")
    _wire_live_fakes(monkeypatch)

    def _raise_boto3_missing(*, bucket: str, region: str | None = None) -> Any:
        raise ReferenceExportConfigError("boto3 is required to export the reference lake to S3")

    monkeypatch.setattr(
        "brutex.export.reference_export.build_real_s3_uploader", _raise_boto3_missing
    )
    alerts: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "brutex.notifications.sweep_alerts.emit_reference_export_failed_alert",
        lambda **kwargs: alerts.append(kwargs),
    )

    with structlog.testing.capture_logs() as logs:
        rc = _fetch(tmp_path)

    assert rc == cli.EXIT_OK  # the fence holds — the completed pull never fails
    assert len(alerts) == 1
    assert alerts[0]["export_date"] == date(2024, 1, 3)
    assert "ReferenceExportConfigError" in alerts[0]["reason"]
    assert any(entry.get("event") == "reference_export.failed" for entry in logs)


def test_seam_b_unset_bucket_skips_without_failing_pull(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Enabled but no bucket → the shim WARN-skips (no alert) and the pull is OK.

    Covers the ``resolve_export_bucket`` config-error branch of the real
    ``_run_cli_reference_export`` shim (an unset bucket is a benign
    not-configured skip — distinct from the boto3-absent failure, which alerts).
    """
    monkeypatch.setenv("BRUTEX_REFERENCE_EXPORT", "1")
    monkeypatch.delenv("BRUTEX_REFERENCE_EXPORT_BUCKET", raising=False)
    _wire_live_fakes(monkeypatch)
    alerts: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "brutex.notifications.sweep_alerts.emit_reference_export_failed_alert",
        lambda **kwargs: alerts.append(kwargs),
    )

    rc = _fetch(tmp_path)

    assert rc == cli.EXIT_OK
    assert alerts == []  # an unset bucket is a benign not-configured skip — no alert


def test_dispatch_export_reference_default_date_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Omitting ``--date`` threads ``date_str=None`` (the latest-day default)."""
    seen: dict[str, Any] = {}

    def _fake_cmd(lake_root: Any, *, date_str: str | None) -> int:
        seen["date_str"] = date_str
        return cli.EXIT_OK

    monkeypatch.setattr(cli, "cmd_export_reference", _fake_cmd)

    rc = cli.main(["export-reference", "--lake-root", str(tmp_path)])

    assert rc == cli.EXIT_OK
    assert seen["date_str"] is None


# ── Round-4 refutation regressions: the CLIENT-BUILD failure class ─────────────
#
# `boto3.client(...)` raises NON-ReferenceExportConfigError types on config gaps
# (botocore InvalidRegionError IS a ValueError; ProfileNotFound is its own tree).
# Pre-fix these escaped both shims' `except ReferenceExportConfigError` guards —
# traceback + red exit AFTER a persisted green pull. The fix converts EVERY
# client-build failure to ReferenceExportConfigError INSIDE build_real_s3_uploader,
# so these tests drive the REAL build (a fake `boto3` module in sys.modules whose
# `client` raises) — build_real_s3_uploader itself is deliberately NOT mocked.


def _install_raising_boto3(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
    """Install a stand-in ``boto3`` whose ``client`` raises *exc*."""
    import sys
    import types

    def _client(service: str, **kwargs: Any) -> Any:
        raise exc

    fake = types.ModuleType("boto3")
    fake.client = _client  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "boto3", fake)


class _InvalidRegionError(ValueError):
    """Shape of botocore.exceptions.InvalidRegionError (a ValueError subclass)."""


class _ProfileNotFound(Exception):
    """Shape of botocore.exceptions.ProfileNotFound (generic BotoCoreError tree)."""


@pytest.mark.parametrize(
    "client_exc",
    [
        _InvalidRegionError(
            "Provided region_name 'not a region!' doesn't match a supported format."
        ),
        _ProfileNotFound("The config profile (nope) could not be found"),
    ],
    ids=["invalid_region_valueerror", "profile_not_found_generic"],
)
def test_seam_b_client_build_failure_is_fenced_pull_stays_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, client_exc: Exception
) -> None:
    """A botocore-shaped client-build failure NEVER fails the persisted pull.

    Drives the REAL ``build_real_s3_uploader`` through the REAL
    ``_run_cli_reference_export`` (only the ``boto3`` module is faked): the build
    converts the raw error to ``ReferenceExportConfigError``, the shim's guard
    catches it → the pull returns ``EXIT_OK`` with ONE WARN + ONE alert naming
    the underlying error type.
    """
    import structlog

    monkeypatch.setenv("BRUTEX_REFERENCE_EXPORT", "1")
    monkeypatch.setenv("BRUTEX_REFERENCE_EXPORT_BUCKET", "tickvault-reference")
    monkeypatch.setenv("BRUTEX_REFERENCE_EXPORT_REGION", " not a region! ")
    _wire_live_fakes(monkeypatch)
    _install_raising_boto3(monkeypatch, client_exc)
    alerts: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "brutex.notifications.sweep_alerts.emit_reference_export_failed_alert",
        lambda **kwargs: alerts.append(kwargs),
    )

    with structlog.testing.capture_logs() as logs:
        rc = _fetch(tmp_path)

    assert rc == cli.EXIT_OK  # the fence holds — the persisted pull never fails
    assert len(alerts) == 1
    assert type(client_exc).__name__ in alerts[0]["reason"]  # names the underlying error
    warn_events = [e for e in logs if e.get("event") == "reference_export.failed"]
    assert len(warn_events) == 1
    assert warn_events[0]["error_type"] == "ReferenceExportConfigError"


def test_cmd_export_reference_client_build_failure_exits_user_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``export-reference`` maps a client-build failure to ``EXIT_USER_ERROR``.

    The subcommand twin of the Seam B fence: the converted
    ``ReferenceExportConfigError`` is caught by the guard that already wraps
    ``resolve_export_bucket`` + ``build_real_s3_uploader`` — a config gap, never
    a traceback / ``EXIT_INTERNAL_ERROR``.
    """
    monkeypatch.setenv("BRUTEX_REFERENCE_EXPORT_BUCKET", "tickvault-reference")
    _install_raising_boto3(
        monkeypatch, _InvalidRegionError("Provided region_name 'junk' doesn't match")
    )

    rc = cli.cmd_export_reference(tmp_path, date_str="2026-07-07")

    assert rc == cli.EXIT_USER_ERROR
    err = capsys.readouterr().err
    assert "failed to build the S3 client" in err
    assert "_InvalidRegionError" in err  # the underlying error type is named
