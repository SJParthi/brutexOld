# pyright: reportPrivateUsage=none
"""Unit tests for the CLI boot-time auto-resume argv builder + validator.

DEC-AUTO-RESUME-ON-BOOT-001 (the decision layer) + DEC-AUTO-RESUME-ARGV-FIX-001
(the dead-resume fix): the spawned child used to run the RETIRED
``brutex fetch --universe --until-clean``, which :func:`brutex.cli.cmd_fetch`
refuses with ``EXIT_USER_ERROR`` before any pull work — so the crash/redeploy
recovery feature never resumed anything while the boot log read
``verdict=resumed``. These tests pin, through the REAL CLI parser + ``main``
dispatch, that the generated argv reaches the genuine pull entry, and that the
new pre-spawn validator (:func:`brutex.cli._validate_auto_resume_argv`) turns
any argv drift into the honest ``error`` verdict instead of a false
``resumed`` (the ``maybe_resume_on_boot`` never-raises contract).

The dashboard boot wiring (:func:`brutex.cli._start_auto_resume_on_boot` /
:func:`brutex.cli._spawn_auto_resume_pull`) is live daemon-thread + subprocess +
server glue, excluded at the SAME live-side-effect boundary the supervisor's
daemon loop is (``# pragma: no cover``); the resume DECISION is 100%-tested in
:mod:`brutex.orchestrator.auto_resume`. The PURE seams in the CLI — the argv
builder + the argv validator — are un-pragma'd and pinned here (mirrors
``test_cli_supervisor`` for ``_supervisor_child_argv``).
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

import pytest
from brutex import cli
from brutex.orchestrator.auto_resume import ResumeVerdict, maybe_resume_on_boot


def test_auto_resume_child_argv_is_until_clean_pull() -> None:
    """The resume child re-runs the CURRENT supported until-clean pull (no retired flag)."""
    argv = cli._auto_resume_child_argv(Path("/data/lake"))
    assert argv == [
        sys.executable,
        "-m",
        "brutex",
        "fetch",
        "--until-clean",
        "--lake-root",
        "/data/lake",
    ]


def test_auto_resume_child_argv_carries_resolved_lake_root() -> None:
    """The child argv carries the SAME lake root the ledger was read from (MAJOR fix).

    Without the explicit ``--lake-root`` a dashboard started with
    ``--lake-root /data/lake`` (``$BRUTEX_LAKE_ROOT`` unset) would spawn a child
    that falls back to ``~/.brutex/lake`` — re-pulling the WRONG lake AND missing
    the lake-root-keyed single-flight lock (the double-pull this feature prevents).
    """
    argv = cli._auto_resume_child_argv(Path("/mnt/ssd/brutex-lake"))
    assert "--lake-root" in argv
    # The resolved root is the LAST token, directly after the ``--lake-root`` flag.
    assert argv[argv.index("--lake-root") + 1] == "/mnt/ssd/brutex-lake"


class TestArgvReachesRealPullEntry:
    """DEC-AUTO-RESUME-ARGV-FIX-001 regression — the argv must reach cmd_fetch's pull path.

    FAILS-BEFORE / PASSES-AFTER: the pre-fix argv carried the RETIRED
    ``--universe`` flag, so the REAL ``main`` dispatch forwarded
    ``universe=True`` into ``cmd_fetch`` — which returns ``EXIT_USER_ERROR``
    (the retirement notice) before any pull work. The fixed argv dispatches
    with ``universe=False`` + ``until_clean=True`` — the genuine pull entry.
    """

    def test_generated_argv_dispatches_to_cmd_fetch_pull_entry(self) -> None:
        """Parse + dispatch the generated argv through the REAL CLI parser/main."""
        argv = cli._auto_resume_child_argv(Path("/data/lake"))
        tokens = argv[3:]  # strip [sys.executable, "-m", "brutex"]
        with mock.patch.object(cli, "cmd_fetch", return_value=cli.EXIT_OK) as fetch:
            rc = cli.main(tokens)

        assert rc == cli.EXIT_OK
        _, kwargs = fetch.call_args
        # The retired flag would make cmd_fetch return EXIT_USER_ERROR before pulling.
        assert kwargs["universe"] is False
        assert kwargs["until_clean"] is True
        assert kwargs["lake_root"] == Path("/data/lake")

    def test_retired_universe_flag_is_refused_by_cmd_fetch(self, tmp_path: Path) -> None:
        """Pin the pre-fix failure path: ``universe=True`` exits with EXIT_USER_ERROR.

        This is the exact dead-end the OLD child argv landed on — proving the
        bug class stays observable (the retirement refusal), not silently gone.
        """
        rc = cli.cmd_fetch(
            tmp_path,
            None,
            None,
            "all",
            window_days=None,
            dry_run=False,
            inspect=False,
            rows_per_chunk=5,
            universe=True,
        )
        assert rc == cli.EXIT_USER_ERROR


class TestValidateAutoResumeArgv:
    """The pre-spawn validator — every drift raises, the generated argv passes."""

    def test_generated_argv_is_accepted(self) -> None:
        """The builder's own argv validates cleanly (the wired production pair)."""
        cli._validate_auto_resume_argv(cli._auto_resume_child_argv(Path("/data/lake")))

    def test_rejects_non_module_prefix(self) -> None:
        """An argv not running ``python -m brutex`` is refused."""
        with pytest.raises(ValueError, match="python -m brutex"):
            cli._validate_auto_resume_argv(["brutex", "fetch", "--until-clean"])

    def test_rejects_empty_subcommand_tokens(self) -> None:
        """A bare ``python -m brutex`` (no subcommand) is refused."""
        with pytest.raises(ValueError, match="no CLI subcommand tokens"):
            cli._validate_auto_resume_argv([sys.executable, "-m", "brutex"])

    def test_rejects_parser_unknown_flag(self) -> None:
        """A flag the REAL parser refuses (SystemExit) surfaces as ValueError."""
        argv = [sys.executable, "-m", "brutex", "fetch", "--no-such-flag"]
        with pytest.raises(ValueError, match="rejected by the CLI parser"):
            cli._validate_auto_resume_argv(argv)

    def test_rejects_non_fetch_command(self) -> None:
        """A well-formed argv for a DIFFERENT subcommand is refused."""
        argv = [sys.executable, "-m", "brutex", "version"]
        with pytest.raises(ValueError, match="must dispatch to `fetch`"):
            cli._validate_auto_resume_argv(argv)

    def test_rejects_retired_universe_flag(self) -> None:
        """The exact pre-fix argv (RETIRED ``--universe``) is refused pre-spawn."""
        argv = [
            sys.executable,
            "-m",
            "brutex",
            "fetch",
            "--universe",
            "--until-clean",
            "--lake-root",
            "/data/lake",
        ]
        with pytest.raises(ValueError, match="RETIRED `--universe`"):
            cli._validate_auto_resume_argv(argv)

    def test_rejects_instruments_only_mode(self) -> None:
        """``--instruments`` loads the master and exits — no pull, so refused."""
        argv = [sys.executable, "-m", "brutex", "fetch", "--instruments", "--until-clean"]
        with pytest.raises(ValueError, match="--instruments"):
            cli._validate_auto_resume_argv(argv)

    def test_rejects_missing_until_clean(self) -> None:
        """A fetch without ``--until-clean`` breaks the bounded-resume contract."""
        argv = [sys.executable, "-m", "brutex", "fetch", "--lake-root", "/data/lake"]
        with pytest.raises(ValueError, match="--until-clean"):
            cli._validate_auto_resume_argv(argv)


class _OutstandingLedger:
    """A minimal LedgerLike double reporting one outstanding (FAILED) window."""

    class _Counts:
        total = 4
        planned = 0
        failed = 1
        settled = 3

    def progress_counts(self) -> _Counts:
        return self._Counts()


class TestBootNeverRaisesContract:
    """maybe_resume_on_boot stays never-raises AND never reports a false ``resumed``."""

    def test_invalid_argv_yields_error_verdict_not_resumed(self) -> None:
        """A resume action tripping the validator returns ERROR — never RESUMED."""
        bad_argv = [sys.executable, "-m", "brutex", "fetch", "--universe", "--until-clean"]
        verdict = maybe_resume_on_boot(
            load_ledger=_OutstandingLedger,
            resume=lambda incomplete: cli._validate_auto_resume_argv(bad_argv),
            enabled=True,
        )
        assert verdict is ResumeVerdict.ERROR
        assert verdict is not ResumeVerdict.RESUMED

    def test_valid_generated_argv_yields_resumed_verdict(self) -> None:
        """The production builder+validator pair keeps the RESUMED happy path."""
        verdict = maybe_resume_on_boot(
            load_ledger=_OutstandingLedger,
            resume=lambda incomplete: cli._validate_auto_resume_argv(
                cli._auto_resume_child_argv(Path("/data/lake"))
            ),
            enabled=True,
        )
        assert verdict is ResumeVerdict.RESUMED


class TestIntentDecisionPointAtBootSeam:
    """DEC-PULL-INTENT-RESUME-001 — the extended ``_start_auto_resume_on_boot``:
    ONE decision point, driven end-to-end through the REAL daemon-thread seam
    with a REAL ``PullIntentStore`` in a tmp lake root. Intent present ⇒ the
    pipeline resume fires and the legacy CLI child is NEVER spawned; no intent
    ⇒ the legacy spawn path is byte-identical."""

    @staticmethod
    def _wire_legacy_wants_to_fire(monkeypatch: pytest.MonkeyPatch) -> list[object]:
        """Make the LEGACY path eager: env ON + an outstanding ledger + a spawn spy."""
        import brutex.data.window_ledger_store as ledger_store

        spawned: list[object] = []
        monkeypatch.setenv("BRUTEX_AUTO_RESUME_ON_BOOT", "1")
        monkeypatch.setattr(ledger_store, "load_window_ledger", lambda _root: _OutstandingLedger())
        monkeypatch.setattr(
            cli,
            "_spawn_auto_resume_pull",
            lambda incomplete, lake_root, log: spawned.append(incomplete),
        )
        return spawned

    def test_intent_present_fires_pipeline_and_never_spawns_cli_child(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import threading

        from brutex.dashboard.pull_intent import PullIntentStore, persist_intent_for_launch

        spawned = self._wire_legacy_wants_to_fire(monkeypatch)
        store = PullIntentStore(tmp_path)
        persist_intent_for_launch(
            store, dry_run=False, symbols="NIFTY", symbol=None, from_date=None, to_date=None
        )
        fired = threading.Event()
        log = mock.Mock()
        cli._start_auto_resume_on_boot(tmp_path, log, intent_store=store, resume_pipeline=fired.set)
        assert fired.wait(timeout=10.0), "the pipeline resume must fire on an intent boot"
        # The daemon thread logs the verdict AFTER the resume — wait for it so the
        # legacy-spawn assertion cannot race the thread body.
        deadline = threading.Event()
        for _ in range(1000):
            if log.info.call_args_list:
                break
            deadline.wait(0.01)
        assert spawned == [], "the legacy CLI child must NOT spawn on an intent boot"

    def test_no_intent_keeps_legacy_spawn_byte_identical(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import threading

        from brutex.dashboard.pull_intent import PullIntentStore

        spawned = self._wire_legacy_wants_to_fire(monkeypatch)
        fired = threading.Event()
        cli._start_auto_resume_on_boot(
            tmp_path,
            mock.Mock(),
            intent_store=PullIntentStore(tmp_path),  # no intent file on disk
            resume_pipeline=fired.set,
        )
        for _ in range(1000):
            if spawned:
                break
            threading.Event().wait(0.01)
        assert len(spawned) == 1, "no intent ⇒ the legacy resume spawn runs as before"
        assert not fired.is_set(), "the pipeline resume must not fire without an intent"
