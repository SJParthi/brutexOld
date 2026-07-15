"""Sweep entrypoints engage the ``keep_awake`` guard (DEC-SWEEP-KEEPAWAKE-001).

The pull paths gained the macOS ``caffeinate`` keep-awake guard in
DEC-PULL-KEEPAWAKE-001 (its module docstring cites a real 6.5 h mid-pull host
sleep), but the SWEEP entrypoints — ``cmd_sweep_all`` (a §8 2-4 DAY grid run),
``cmd_sweep`` and ``cmd_sweep_daemon`` — had ZERO sleep protection. These
tests pin the fix:

* each sweep entrypoint enters ``keep_awake`` EXACTLY ONCE with its own
  reason (``brutex sweep-all`` / ``brutex sweep`` / ``brutex sweep-daemon``),
  spied at the :mod:`brutex.observability.power_keepawake` seam (the
  call-time ``from … import`` reads the CURRENT module attribute — the
  ``test_cli_enrich_greeks_cmd.py`` idiom);
* the daemon's guard wraps the WHOLE cycle loop (entered BEFORE the cycle's
  ``cmd_sweep_all`` fires, covering the between-cycle sleep);
* the dashboard sweep job (``dashboard/sweep_runner.run_sweep_from_request``)
  calls ``cmd_sweep_all`` IN-PROCESS and therefore INHERITS the guard —
  proven end-to-end, no second wrap needed;
* a keep-awake spawn failure NEVER fails the sweep (the guard's never-raise
  contract is pinned in ``test_power_keepawake.py`` —
  ``test_generic_oserror_on_spawn_is_a_logged_noop`` /
  ``test_missing_caffeinate_binary_is_a_logged_noop``; here the sweep-entry
  variant drives the REAL guard's darwin spawn-failure arm through the REAL
  call site);
* the non-darwin no-op arm logs ``power.keepawake.unavailable`` carrying the
  sweep reason (the REAL guard, platform injected for hermeticity — no test
  ever spawns a real ``caffeinate``).

Every scenario is offline: an EMPTY temp lake makes the wrapped body exit at
the honest "no bars" refusal — cheap, deterministic, and INSIDE the guard.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any

import pytest
from brutex import cli
from brutex.dashboard.sweep_runner import SweepRunRequest, run_sweep_from_request
from brutex.observability import power_keepawake
from structlog.testing import capture_logs

if TYPE_CHECKING:
    from collections.abc import Generator, MutableMapping
    from pathlib import Path

# ── Seam spies (the test_cli_enrich_greeks_cmd.py idiom) ─────────────────────


def _record_keep_awake(monkeypatch: pytest.MonkeyPatch, events: list[tuple[str, Any]]) -> None:
    """Replace ``keep_awake`` with a recording no-op context manager."""

    @contextlib.contextmanager
    def _fake_keep_awake(reason: str) -> Generator[None]:
        events.append(("keep_awake", reason))
        yield

    monkeypatch.setattr(power_keepawake, "keep_awake", _fake_keep_awake)


def _keepawake_events(logs: list[MutableMapping[str, Any]]) -> list[MutableMapping[str, Any]]:
    return [e for e in logs if e["event"] == "power.keepawake.unavailable"]


def _sweep_all_kwargs(lake_root: Path) -> dict[str, Any]:
    """A minimal in-scope ``cmd_sweep_all`` call over an EMPTY lake.

    The empty lake exits at the honest "no bars" ``EXIT_USER_ERROR`` refusal —
    which sits INSIDE the keep-awake wrap (the preload is covered), so the
    spy proves engagement without running a real grid.
    """
    return {
        "lake_root": lake_root,
        "catalog_path": None,
        "results_root": None,
        "exchange": "NSE",
        "segment": "INDEX",
        "symbols": "NIFTY",
        "intervals": "1minute",
        "start": "2024-01-01",
        "end": "2024-01-02",
        "directions": "BUY",
        "k": "3",
        "conditions": None,
        "sl": "none",
        "tp": "none",
        "top": 5,
        "mode": "TRAINING",
        "max_tuples": None,
        "max_combos": None,
        "data_snapshot_hash": None,
    }


def _sweep_kwargs(lake_root: Path, catalog_path: Path) -> dict[str, Any]:
    """A minimal in-scope ``cmd_sweep`` call over an EMPTY (real) catalog."""
    return {
        "lake_root": lake_root,
        "catalog_path": catalog_path,
        "exchange": "NSE",
        "segment": "INDEX",
        "symbol": "NIFTY",
        "interval": "1minute",
        "start": "2024-01-01",
        "end": "2024-01-02",
        "direction": "BUY",
        "k": 3,
        "conditions": None,
        "stop_loss_paisa": None,
        "take_profit_paisa": None,
        "top": 5,
        "min_hits": None,
        "sort_by": "sharpe",
        "validate_oos": False,
        "oos_only": False,
    }


def _make_empty_catalog(lake_root: Path) -> Path:
    """Create a real (empty) catalog file so ``cmd_sweep`` reaches its wrap."""
    from brutex.data.catalog import Catalog

    catalog_path = lake_root / "catalog.duckdb"
    with Catalog.open(catalog_path):
        pass
    return catalog_path


# ── (a) each entrypoint enters keep_awake exactly once ───────────────────────


class TestSweepEntrypointsEngageKeepAwake:
    def test_sweep_all_enters_keep_awake_exactly_once(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``cmd_sweep_all`` wraps the run (incl. the lake preload) in ONE
        ``keep_awake("brutex sweep-all")`` — the empty-lake refusal fires
        INSIDE the wrap, proving the preload is covered."""
        events: list[tuple[str, Any]] = []
        _record_keep_awake(monkeypatch, events)
        rc = cli.cmd_sweep_all(**_sweep_all_kwargs(tmp_path))
        assert rc == cli.EXIT_USER_ERROR
        assert events == [("keep_awake", "brutex sweep-all")]
        assert "no bars for any ['NIFTY']" in capsys.readouterr().err

    def test_sweep_enters_keep_awake_exactly_once(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``cmd_sweep`` wraps its bar-load + report body in ONE
        ``keep_awake("brutex sweep")``."""
        catalog_path = _make_empty_catalog(tmp_path)
        events: list[tuple[str, Any]] = []
        _record_keep_awake(monkeypatch, events)
        rc = cli.cmd_sweep(**_sweep_kwargs(tmp_path, catalog_path))
        assert rc == cli.EXIT_USER_ERROR
        assert events == [("keep_awake", "brutex sweep")]
        assert "no bars for NIFTY" in capsys.readouterr().err

    def test_sweep_daemon_enters_keep_awake_once_wrapping_the_cycle(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``cmd_sweep_daemon`` takes ONE guard for its WHOLE lifetime —
        entered BEFORE the cycle's ``cmd_sweep_all`` fires (so the
        between-cycle sleep is covered too; each cycle's ``cmd_sweep_all``
        then takes its OWN nested assertion — harmless, both watch this
        pid)."""
        events: list[tuple[str, Any]] = []
        _record_keep_awake(monkeypatch, events)

        def _stub_sweep_all(**kwargs: Any) -> int:
            events.append(("sweep_all", kwargs["lake_root"]))
            return cli.EXIT_OK

        monkeypatch.setattr(cli, "cmd_sweep_all", _stub_sweep_all)
        rc = cli.cmd_sweep_daemon(
            schedule="daily@02:30",
            once=True,
            max_cycles=None,
            tz_offset_minutes=None,
            lake_root=tmp_path,
        )
        assert rc == cli.EXIT_OK
        assert events == [
            ("keep_awake", "brutex sweep-daemon"),
            ("sweep_all", tmp_path),
        ]

    def test_dashboard_sweep_job_inherits_the_guard_in_process(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The dashboard sweep job calls the REAL ``cmd_sweep_all`` in-process
        (``run_sweep_from_request``), so it INHERITS the ``brutex sweep-all``
        guard.

        T2-fold note (integration train, #1285 × #1305 composition): #1285
        (DEC-SWEEP-SCALE-GUARDS-001 fix-3) ALSO wraps the dashboard job body
        itself in ``keep_awake("brutex sweep (dashboard)")`` — kept at the
        fold as a redundant-nested guard (nested best-effort assertions are
        harmless; the #1285 steward may drop it later). The composed
        expectation is therefore TWO engagements: the job-body wrap first,
        then ``cmd_sweep_all``'s own inherited wrap. #1305's original
        "exactly one entry, no second wrap" claim is superseded by the
        coordinator-mandated keep of the #1285 wrap.
        """
        events: list[tuple[str, Any]] = []
        _record_keep_awake(monkeypatch, events)
        request = SweepRunRequest(
            symbols="NIFTY",
            intervals="1minute",
            directions="BUY",
            k="3",
            sl="none",
            tp="none",
            exchange="NSE",
            segment="INDEX",
            start="2024-01-01",
            end="2024-01-02",
            mode="TRAINING",
            top=5,
        )
        rc = run_sweep_from_request(request, lake_root=tmp_path, results_root=tmp_path / "sweeps")
        assert rc == cli.EXIT_USER_ERROR  # the empty-lake refusal, inside the wrap
        assert events == [
            ("keep_awake", "brutex sweep (dashboard)"),
            ("keep_awake", "brutex sweep-all"),
        ]
        assert "no bars for any ['NIFTY']" in capsys.readouterr().err


# ── (b) a keep-awake failure NEVER fails the sweep ───────────────────────────


class TestKeepAwakeFailureNeverFailsTheSweep:
    def test_spawn_failure_at_the_sweep_entry_is_a_logged_noop(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The REAL guard's darwin spawn-failure arm, driven through the REAL
        ``cmd_sweep_all`` call site: the sweep still runs to its normal exit
        (the guard's never-raise contract — pinned module-side by
        ``test_power_keepawake.py``), with ONE unavailable note carrying the
        sweep reason."""
        real_keep_awake = power_keepawake.keep_awake

        def _spawn_fails(_argv: list[str]) -> power_keepawake.KeepAwakeProcess:
            raise PermissionError("caffeinate spawn denied")

        def _failing_keep_awake(reason: str) -> Any:
            return real_keep_awake(reason, spawn=_spawn_fails, platform="darwin")

        monkeypatch.setattr(power_keepawake, "keep_awake", _failing_keep_awake)
        with capture_logs() as logs:
            rc = cli.cmd_sweep_all(**_sweep_all_kwargs(tmp_path))
        # The NORMAL empty-lake refusal — never a crash from the guard.
        assert rc == cli.EXIT_USER_ERROR
        assert "no bars for any ['NIFTY']" in capsys.readouterr().err
        (event,) = _keepawake_events(logs)
        assert event["reason"] == "brutex sweep-all"
        assert event["error_type"] == "PermissionError"


# ── (c) the non-darwin no-op arm logs the sweep reason ───────────────────────


class TestLinuxNoopArmLogsTheSweepReason:
    def test_sweep_all_on_linux_logs_unavailable_with_the_sweep_reason(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Off darwin the guard no-ops with ONE ``power.keepawake.unavailable``
        note naming ``brutex sweep-all`` — the honest 'host sleep policy
        applies' signal (platform injected so no host ever spawns a real
        ``caffeinate``)."""
        real_keep_awake = power_keepawake.keep_awake

        def _linux_keep_awake(reason: str) -> Any:
            return real_keep_awake(reason, platform="linux")

        monkeypatch.setattr(power_keepawake, "keep_awake", _linux_keep_awake)
        with capture_logs() as logs:
            rc = cli.cmd_sweep_all(**_sweep_all_kwargs(tmp_path))
        assert rc == cli.EXIT_USER_ERROR
        assert "no bars for any ['NIFTY']" in capsys.readouterr().err
        (event,) = _keepawake_events(logs)
        assert event["reason"] == "brutex sweep-all"
        assert event["platform"] == "linux"
