# pyright: reportPrivateUsage=none
# Whitebox: drives the private CLI pull-site seam `_enter_pull_singleflight`
# directly (the house pattern of tests/unit/test_cli_retry_storm.py).
"""CLI wiring tests for DEC-SWEEP-PREFLIGHT-ESTIMATOR-001 (BUILD-SPEC §9.1, WP7).

``cmd_sweep_all`` / ``cmd_sweep_daemon`` are ``# pragma: no cover`` shims, so
these tests pin the MEASURED wiring around them plus the non-pragma'd
``_enter_pull_singleflight`` pull-site sweep-vs-pull exclusion arms:

* ``--estimate-only`` parses on the shared grid parser and ``main()`` threads
  it into ``cmd_sweep_all`` (default ``False``);
* the ``--k`` default flipped ``"1,2" → "1,2,3"`` (decision O-f) while the
  ``--sl``/``--tp`` defaults stay ``"none"`` (the unattended daemon's workload
  is not silently 16×'d — BUILD-SPEC §0 O-g);
* the unattended daemon REFUSES ``--estimate-only`` as a config error (the
  ``--allow-out-of-scope`` refusal shape) and pins ``estimate_only=False``
  into every cycle's ``cmd_sweep_all`` call;
* ``cli.default_condition_bits`` is a delegating wrapper over the MOVED
  engine source (drift pin, BUILD-SPEC §10.3);
* ``_enter_pull_singleflight`` — the CLI half of extreme test 9 (R4/G10):
  a held sweep ``LOCK_SH`` refuses the pull at the write-free PRE-probe
  (neither pull lock created), the post-acquire RE-probe rolls both pull
  locks back LIFO on the TOCTOU race (both sides refuse, fail-closed), and
  the happy path transfers the held locks to the caller's ExitStack.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from unittest import mock

import pytest

pytest.importorskip("polars")
pytest.importorskip("duckdb")

from brutex import cli
from brutex.adapters.groww.pull_singleflight import (
    PullAlreadyRunningError,
    pull_singleflight_lock,
)
from brutex.data.sweep_exclusion import (
    pull_lock_path,
    sweep_run_guard,
)
from brutex.engine.sweep_estimate import default_condition_bits as engine_default_bits

_SWEEP_ALL_MIN = ["sweep-all", "--start", "2024-01-01", "--end", "2024-01-02"]
_DAEMON_MIN = ["sweep-daemon", "--once", "--start", "2024-01-01", "--end", "2024-01-02"]


# ────────────────────────────────────────────────────────────────────────
# --estimate-only parse + dispatch threading (BUILD-SPEC §9.1 item 1)
# ────────────────────────────────────────────────────────────────────────


class TestEstimateOnlyFlag:
    def test_estimate_only_defaults_false_on_parse(self) -> None:
        """The shared grid parser carries --estimate-only, default False."""
        parser = cli.build_parser()
        args = parser.parse_args(_SWEEP_ALL_MIN)
        assert args.estimate_only is False

    def test_estimate_only_parses_true(self) -> None:
        parser = cli.build_parser()
        args = parser.parse_args([*_SWEEP_ALL_MIN, "--estimate-only"])
        assert args.estimate_only is True

    def test_main_dispatch_threads_estimate_only_false_by_default(self) -> None:
        """Unset flag threads as False into cmd_sweep_all."""
        with mock.patch.object(cli, "cmd_sweep_all", return_value=0) as sweep_all:
            rc = cli.main(_SWEEP_ALL_MIN)
        assert rc == 0
        _, kwargs = sweep_all.call_args
        assert kwargs["estimate_only"] is False

    def test_main_dispatch_threads_estimate_only_true_when_passed(self) -> None:
        with mock.patch.object(cli, "cmd_sweep_all", return_value=0) as sweep_all:
            rc = cli.main([*_SWEEP_ALL_MIN, "--estimate-only"])
        assert rc == 0
        _, kwargs = sweep_all.call_args
        assert kwargs["estimate_only"] is True


# ────────────────────────────────────────────────────────────────────────
# defaults: --k flip (O-f) + --sl/--tp UNCHANGED (O-g CLI side)
# ────────────────────────────────────────────────────────────────────────


class TestGridDefaults:
    def test_k_default_flipped_to_1_2_3(self) -> None:
        """O-f: the CLI --k default is now '1,2,3' (sanctioned + ledgered)."""
        parser = cli.build_parser()
        args = parser.parse_args(_SWEEP_ALL_MIN)
        assert args.k == "1,2,3"

    def test_daemon_parser_shares_the_k_default(self) -> None:
        """The daemon rides the SAME shared declaration (one source of truth)."""
        parser = cli.build_parser()
        args = parser.parse_args(_DAEMON_MIN)
        assert args.k == "1,2,3"

    def test_sl_tp_defaults_stay_none(self) -> None:
        """O-g CLI side: --sl/--tp defaults deliberately UNCHANGED at 'none'
        (the unattended daemon's workload is not silently 16×'d)."""
        parser = cli.build_parser()
        args = parser.parse_args(_SWEEP_ALL_MIN)
        assert args.sl == "none"
        assert args.tp == "none"


# ────────────────────────────────────────────────────────────────────────
# daemon refusals + estimate_only=False pin (BUILD-SPEC §9.1 item 7)
# ────────────────────────────────────────────────────────────────────────


class TestDaemonEstimateOnlyRefusal:
    def test_daemon_refuses_estimate_only_as_config_error(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """--estimate-only on sweep-daemon is a loud EXIT_USER_ERROR — the
        daemon never runs (a scheduled cycle must run, never no-op)."""
        with mock.patch.object(cli, "cmd_sweep_daemon") as daemon:
            rc = cli.main([*_DAEMON_MIN, "--estimate-only"])
        assert rc == cli.EXIT_USER_ERROR
        daemon.assert_not_called()
        err = capsys.readouterr().err
        assert "sweep-daemon REFUSED" in err
        assert "--estimate-only" in err
        assert "DEC-SWEEP-PREFLIGHT-ESTIMATOR-001" in err

    def test_daemon_without_flag_still_dispatches(self) -> None:
        """The refusal block never blocks a normal daemon dispatch."""
        with mock.patch.object(cli, "cmd_sweep_daemon", return_value=0) as daemon:
            rc = cli.main(_DAEMON_MIN)
        assert rc == 0
        daemon.assert_called_once()

    def test_daemon_cycle_pins_estimate_only_false(self, tmp_path: Path) -> None:
        """Each daemon cycle calls cmd_sweep_all(estimate_only=False, ...)."""
        with mock.patch.object(cli, "cmd_sweep_all", return_value=0) as sweep_all:
            rc = cli.cmd_sweep_daemon(
                schedule="daily@02:30",
                once=True,
                max_cycles=None,
                tz_offset_minutes=None,
                lake_root=tmp_path / "lake",
                results_root=tmp_path / "sweeps",
            )
        assert rc == cli.EXIT_OK
        _, kwargs = sweep_all.call_args
        assert kwargs["estimate_only"] is False


# ────────────────────────────────────────────────────────────────────────
# default_condition_bits delegating wrapper (drift pin, BUILD-SPEC §10.3)
# ────────────────────────────────────────────────────────────────────────


class TestDefaultConditionBitsWrapper:
    def test_cli_wrapper_equals_engine_source(self) -> None:
        """cli.default_condition_bits() == the MOVED engine source, element-wise
        (§11 COMMON — one derivation, three consumers; drift-pinned)."""
        assert cli.default_condition_bits() == list(engine_default_bits())

    def test_cli_wrapper_keeps_the_list_shape(self) -> None:
        """Import-compat: the pre-move CLI callers keep their list shape."""
        assert isinstance(cli.default_condition_bits(), list)
        assert isinstance(engine_default_bits(), tuple)


# ────────────────────────────────────────────────────────────────────────
# _enter_pull_singleflight — sweep-vs-pull exclusion (extreme 9, CLI half)
# ────────────────────────────────────────────────────────────────────────


class TestEnterPullSingleflightSweepExclusion:
    def test_r4_g10_held_sweep_lock_refuses_pull_at_pre_probe(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A sweep holding LOCK_SH refuses the CLI pull at the write-free
        PRE-probe — neither pull lock file is even created (extreme 9)."""
        lake = tmp_path / "lake"
        lake.mkdir()
        with sweep_run_guard(lake), contextlib.ExitStack() as stack:
            ok = cli._enter_pull_singleflight(stack, lake, account_key_resolver=lambda: None)
        assert ok is False
        err = capsys.readouterr().err
        assert "a sweep is running for this lake" in err
        assert "sweep-vs-pull exclusion" in err
        # PRE-probe refusal ⇒ the pull single-flight lock was never created.
        assert not pull_lock_path(lake).exists()

    def test_r4_g10_toctou_reprobe_rolls_both_locks_back(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A sweep slipping in BETWEEN the pre-probe and the acquires is caught
        by the post-acquire RE-probe: both pull locks roll back LIFO and the
        pull refuses (the both-refuse TOCTOU arm, fail-closed)."""
        from brutex.data import sweep_exclusion

        lake = tmp_path / "lake"
        lake.mkdir()
        calls: list[int] = []
        real_probe = sweep_exclusion.sweep_in_progress

        def _flip(root: Path) -> bool:
            calls.append(1)
            if len(calls) == 1:
                return real_probe(root)  # pre-probe: no sweep yet
            return True  # re-probe: a sweep slipped in mid-acquire

        monkeypatch.setattr(sweep_exclusion, "sweep_in_progress", _flip)
        with contextlib.ExitStack() as stack:
            ok = cli._enter_pull_singleflight(stack, lake, account_key_resolver=lambda: None)
        assert ok is False
        assert len(calls) == 2  # pre-probe + re-probe, exactly
        err = capsys.readouterr().err
        assert "sweep-vs-pull exclusion" in err
        # The pull lock was ROLLED BACK — a fresh acquire succeeds immediately.
        with pull_singleflight_lock(lake):
            pass

    def test_happy_path_transfers_locks_to_caller_stack(self, tmp_path: Path) -> None:
        """No sweep running ⇒ True; the locks live in the CALLER's stack (held
        until it closes) — the local-stack staging never changes the contract."""
        lake = tmp_path / "lake"
        lake.mkdir()
        with contextlib.ExitStack() as stack:
            ok = cli._enter_pull_singleflight(stack, lake, account_key_resolver=lambda: None)
            assert ok is True
            # Held: a second acquire on the SAME lake refuses while the caller's
            # stack keeps the transferred locks open.
            with pytest.raises(PullAlreadyRunningError), pull_singleflight_lock(lake):
                pass  # pragma: no cover — __enter__ raises first
        # Caller's stack exited → released → a fresh acquire succeeds.
        with pull_singleflight_lock(lake):
            pass

    def test_lake_contention_rolls_back_before_sweep_reprobe(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A peer pull holding the lake lock still refuses with the historical
        message — the sweep probes never mask the pull-vs-pull refusal."""
        lake = tmp_path / "lake"
        lake.mkdir()
        with pull_singleflight_lock(lake), contextlib.ExitStack() as stack:
            ok = cli._enter_pull_singleflight(stack, lake, account_key_resolver=lambda: None)
        assert ok is False
        err = capsys.readouterr().err
        assert "a pull is already running for this lake" in err
        assert "refusing to start a second concurrent pull" in err
