"""The sweep CLI log-volume guard — DEC-SWEEP-LOG-VOLUME-GUARD-001.

Regression net for the composite-run finding: a bare ``brutex sweep-all``
process left structlog UNCONFIGURED (default wrapper filters at NOTSET), so
the engine's per-trade DEBUG events (``trade_sim.entry``/``trade_sim.exit``)
streamed unfiltered to stderr — a measured 870 MB on a ~200-session fixture,
a disk-fill run-killer on the 2-4 day production grid.

Pins (each captures ACTUAL emitted output via capsys, never a mock of the
sink — the guard's claim is about real stderr bytes):

* Default: after the ``main()`` sweep dispatch, a per-trade-shaped DEBUG
  event does NOT emit; WARNING and ERROR still DO (sink-level threshold
  only — no event is re-levelled, nothing loud is dropped).
* ``BRUTEX_LOG_LEVEL=DEBUG`` restores DEBUG emission (the operator lever).
* Junk ``BRUTEX_LOG_LEVEL`` fails LOUD: exit 1 + one ``[brutex] …`` line,
  and the sweep handler is NEVER invoked (never a silent fallback).
* All three sweep entries (``sweep`` / ``sweep-all`` / ``sweep-daemon``)
  pass through the ONE dispatch guard.
* An already-configured process (the dashboard's in-process sweep job) is
  DEFERRED to — the guard never clobbers an existing configuration.
* The audit JSONL writers are structlog-free (the honesty note: the guard
  bounds LOG volume only; the Golden-Rule-#8 hash chain is untouched).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from unittest import mock

import pytest
import structlog
from brutex import cli
from brutex.observability.logging import (
    LOG_LEVEL_ENV,
    configure_logging,
    reset_configuration,
)

_SWEEP_ALL_ARGV = ["sweep-all", "--start", "2024-01-01", "--end", "2024-01-02"]


@pytest.fixture(autouse=True)
def _clean_logging_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Isolate global logging config + the env override per test."""
    monkeypatch.delenv(LOG_LEVEL_ENV, raising=False)
    reset_configuration()
    yield
    reset_configuration()


def _dispatch_sweep_all() -> int:
    """Run the real ``main()`` dispatch with the sweep handler mocked out.

    The guard fires at dispatch (before the handler), so mocking
    ``cmd_sweep_all`` exercises the production seam without a lake.
    """
    with mock.patch.object(cli, "cmd_sweep_all", return_value=0):
        rc = cli.main(_SWEEP_ALL_ARGV)
    assert isinstance(rc, int)
    return rc


class TestDefaultSuppressesDebug:
    """Default (no env): DEBUG suppressed, WARNING/ERROR still emitted."""

    def test_per_trade_debug_suppressed_by_default(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert _dispatch_sweep_all() == 0
        # A fresh logger binds the JUST-configured filtering wrapper — the
        # same first-use binding the engine's module loggers get, because the
        # guard runs before cmd_sweep_all imports any engine module.
        log = structlog.get_logger("brutex.engine.trade_sim")
        log.debug("trade_sim.entry", idx=7, direction="BUY", entry_price_paisa=2_425_800)
        assert "trade_sim.entry" not in capsys.readouterr().err

    def test_warning_and_error_pass_at_default(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert _dispatch_sweep_all() == 0
        log = structlog.get_logger("brutex.engine.sweep")
        log.warning("sweep.scope_override", symbols=["INDIAVIX"])
        log.error("sweep.tuple_failed", combo="deadbeef")
        err = capsys.readouterr().err
        assert "sweep.scope_override" in err
        assert "sweep.tuple_failed" in err

    def test_info_passes_at_default(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert _dispatch_sweep_all() == 0
        structlog.get_logger("brutex.engine.sweep").info("sweep.progress", done=1)
        assert "sweep.progress" in capsys.readouterr().err


class TestEnvOverride:
    """$BRUTEX_LOG_LEVEL=DEBUG restores per-trade events; junk fails loud."""

    def test_debug_env_restores_per_trade_events(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv(LOG_LEVEL_ENV, "DEBUG")
        assert _dispatch_sweep_all() == 0
        log = structlog.get_logger("brutex.engine.trade_sim")
        log.debug("trade_sim.exit", exit_idx=9, reason="tp", pnl_paisa=1500)
        assert "trade_sim.exit" in capsys.readouterr().err

    def test_junk_env_fails_loud_before_the_handler(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv(LOG_LEVEL_ENV, "VERBOSE")
        with mock.patch.object(cli, "cmd_sweep_all", return_value=0) as handler:
            rc = cli.main(_SWEEP_ALL_ARGV)
        assert rc == cli.EXIT_USER_ERROR
        handler.assert_not_called()
        err = capsys.readouterr().err
        assert "[brutex] invalid BRUTEX_LOG_LEVEL='VERBOSE'" in err
        # Junk must also mean "nothing was configured behind the error".
        assert not structlog.is_configured()

    def test_junk_env_fails_loud_even_when_already_configured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Fail-loud beats defer-to-existing: a typo'd env var never rides
        # silently on the host process's configuration.
        configure_logging(level="INFO", json_renderer=False)
        monkeypatch.setenv(LOG_LEVEL_ENV, "DEBG")
        with mock.patch.object(cli, "cmd_sweep_all", return_value=0) as handler:
            rc = cli.main(_SWEEP_ALL_ARGV)
        assert rc == cli.EXIT_USER_ERROR
        handler.assert_not_called()


class TestAllSweepEntriesGuarded:
    """sweep / sweep-all / sweep-daemon all pass the ONE dispatch guard."""

    @pytest.mark.parametrize(
        ("argv", "handler"),
        [
            (
                ["sweep", "NIFTY", "--start", "2024-01-01", "--end", "2024-01-02"],
                "cmd_sweep",
            ),
            (_SWEEP_ALL_ARGV, "cmd_sweep_all"),
            (
                ["sweep-daemon", "--once", "--start", "2024-01-01", "--end", "2024-01-02"],
                "cmd_sweep_daemon",
            ),
        ],
    )
    def test_guard_configures_before_each_sweep_handler(
        self, argv: list[str], handler: str
    ) -> None:
        assert not structlog.is_configured()
        with mock.patch.object(cli, handler, return_value=0) as mocked:
            rc = cli.main(argv)
        assert rc == 0
        mocked.assert_called_once()
        assert structlog.is_configured()

    def test_non_sweep_command_does_not_configure(self) -> None:
        # The guard is scoped to the sweep family — `brutex version` (the
        # polars-free fast path) must stay logging-untouched.
        rc = cli.main(["version"])
        assert rc == 0
        assert not structlog.is_configured()


class TestDefersToExistingConfiguration:
    """A host-process configuration (the dashboard) is never clobbered."""

    def test_existing_config_is_respected(self, capsys: pytest.CaptureFixture[str]) -> None:
        # Simulate the dashboard process: cmd_dashboard configured DEBUG
        # explicitly; the in-process sweep job must NOT re-threshold it.
        configure_logging(level="DEBUG", json_renderer=False)
        assert _dispatch_sweep_all() == 0
        structlog.get_logger("brutex.engine.trade_sim").debug("trade_sim.entry", idx=1)
        assert "trade_sim.entry" in capsys.readouterr().err


class TestAuditChainIsNotStructlog:
    """Honesty pin: the hash-chained audit JSONL writers are structlog-free.

    The guard's docstring claims the audit trail is untouched at any level
    BECAUSE those records are direct file appends, not structlog events —
    pin that fact so a future structlog-routed audit write re-opens the
    decision loudly.
    """

    @pytest.mark.parametrize(
        "module_path",
        ["brutex/engine/sweep_audit.py", "brutex/orchestrator/audit.py"],
    )
    def test_audit_writers_do_not_import_structlog(self, module_path: str) -> None:
        root = Path(cli.__file__).resolve().parent.parent
        source = (root / module_path).read_text(encoding="utf-8")
        assert "structlog" not in source
