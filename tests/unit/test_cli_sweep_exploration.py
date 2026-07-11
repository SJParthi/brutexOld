"""CLI exploration-mode resolution tests (DEC-SWEEP-APRIORI-DEFAULT-001, #1738).

The ONE pure resolver ``brutex.cli.resolve_sweep_exploration`` behind every grid
surface's ``--k`` (sweep-all / sweep-daemon / sweep-verify / nightly share
``_add_sweep_grid_args``), plus the ``cmd_sweep_all`` env-setdefault law — the
spawn-worker inheritance seam (workers re-read ``BRUTEX_SWEEP_APRIORI`` from the
inherited environment, so the CLI must write it BEFORE any pool exists) — pinned
through the REAL ``main()`` dispatch against a nonexistent lake root: the
resolver + env write run EAGERLY before any lake I/O, so the loud lake refusal
doubles as the "no heavier work ran" call-order spy.

The resolver matrix mirrors the judge spec §(e) rows 1/6/9/10; the ``cmd_*``
bodies are ``# pragma: no cover`` orchestration shims, so the dispatch tests pin
BEHAVIOR (env state + stderr copy + exit codes), never coverage.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from brutex.cli import (
    EXIT_USER_ERROR,
    EXTINCTION_K_TOKEN,
    CliUserError,
    SweepExplorationPlan,
    main,
    resolve_sweep_exploration,
)
from brutex.engine.sweep_apriori import SWEEP_APRIORI_ENV

pytestmark = pytest.mark.usefixtures("_inert_sweep_logging_guard")  # see tests/conftest.py

_BITS_6 = [0, 1, 2, 3, 4, 5]  # n_unique = 6


# ─────────────────────────── the pure resolver ───────────────────────────


class TestResolverLegacyArm:
    """An explicit numeric ``--k`` is the byte-identical legacy arm."""

    def test_numeric_csv_is_legacy_and_env_unread(self) -> None:
        # A JUNK env value proves the arm never reads the environment — the
        # byte-identity mandate covers explicit-k paths (ruling 7).
        plan = resolve_sweep_exploration(
            "1,2", condition_bits=_BITS_6, max_combos=None, env={SWEEP_APRIORI_ENV: "ture"}
        )
        assert plan == SweepExplorationPlan(apriori=False, k_values=[1, 2], notes=())

    def test_single_numeric_k(self) -> None:
        plan = resolve_sweep_exploration("3", condition_bits=_BITS_6, max_combos=None, env={})
        assert plan.apriori is False
        assert plan.k_values == [3]

    def test_legacy_arm_ignores_an_explicit_max_combos(self) -> None:
        # The ladder-trim branch is extinction-mode ONLY (ruling 9).
        plan = resolve_sweep_exploration("1,2", condition_bits=_BITS_6, max_combos=3, env={})
        assert plan == SweepExplorationPlan(apriori=False, k_values=[1, 2], notes=())

    def test_non_integer_csv_raises(self) -> None:
        with pytest.raises(CliUserError):
            resolve_sweep_exploration("1,x", condition_bits=_BITS_6, max_combos=None, env={})

    def test_token_mixed_with_numbers_is_refused(self) -> None:
        # "extinction,2" is not the token ⇒ the legacy CSV parser refuses it —
        # the two grammars don't compose.
        with pytest.raises(CliUserError):
            resolve_sweep_exploration(
                "extinction,2", condition_bits=_BITS_6, max_combos=None, env={}
            )


class TestResolverExtinctionArm:
    """The token default: k = [1..n_unique] under the tri-state env law."""

    def test_token_env_unset_resolves_extinction(self) -> None:
        plan = resolve_sweep_exploration(
            EXTINCTION_K_TOKEN, condition_bits=_BITS_6, max_combos=None, env={}
        )
        assert plan.apriori is True
        assert plan.k_values == [1, 2, 3, 4, 5, 6]
        assert plan.notes == ()

    def test_token_is_case_and_whitespace_insensitive(self) -> None:
        plan = resolve_sweep_exploration(
            "  EXTINCTION  ", condition_bits=_BITS_6, max_combos=None, env={}
        )
        assert plan.apriori is True

    def test_duplicate_bits_dedup_into_n_unique(self) -> None:
        plan = resolve_sweep_exploration(
            EXTINCTION_K_TOKEN, condition_bits=[0, 0, 1, 1], max_combos=None, env={}
        )
        assert plan.k_values == [1, 2]

    @pytest.mark.parametrize("raw", ["1", "true", "yes", "on", "TRUE"])
    def test_token_truthy_env_resolves_extinction(self, raw: str) -> None:
        plan = resolve_sweep_exploration(
            EXTINCTION_K_TOKEN,
            condition_bits=_BITS_6,
            max_combos=None,
            env={SWEEP_APRIORI_ENV: raw},
        )
        assert plan.apriori is True
        assert plan.notes == ()

    @pytest.mark.parametrize("raw", ["", "   "])
    def test_token_blank_env_is_classified_like_unset(self, raw: str) -> None:
        plan = resolve_sweep_exploration(
            EXTINCTION_K_TOKEN,
            condition_bits=_BITS_6,
            max_combos=None,
            env={SWEEP_APRIORI_ENV: raw},
        )
        assert plan.apriori is True

    @pytest.mark.parametrize("raw", ["0", "false", "no", "off"])
    def test_token_falsy_env_is_the_loud_kill_switch(self, raw: str) -> None:
        plan = resolve_sweep_exploration(
            EXTINCTION_K_TOKEN,
            condition_bits=_BITS_6,
            max_combos=None,
            env={SWEEP_APRIORI_ENV: raw},
        )
        assert plan.apriori is False
        assert plan.k_values == [1, 2]  # the legacy default, byte-identical
        assert len(plan.notes) == 1
        assert "kill switch" in plan.notes[0]

    def test_token_junk_env_fails_loud(self) -> None:
        # A typo must never silently flip the DEFAULT mode (Golden Rule #1).
        with pytest.raises(CliUserError, match="not a recognised value"):
            resolve_sweep_exploration(
                EXTINCTION_K_TOKEN,
                condition_bits=_BITS_6,
                max_combos=None,
                env={SWEEP_APRIORI_ENV: "ture"},
            )

    def test_resolver_never_mutates_the_env_mapping(self) -> None:
        env = {SWEEP_APRIORI_ENV: "0"}
        resolve_sweep_exploration(
            EXTINCTION_K_TOKEN, condition_bits=_BITS_6, max_combos=None, env=env
        )
        assert env == {SWEEP_APRIORI_ENV: "0"}

    def test_default_env_is_os_environ(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(SWEEP_APRIORI_ENV, "0")
        plan = resolve_sweep_exploration(
            EXTINCTION_K_TOKEN, condition_bits=_BITS_6, max_combos=None
        )
        assert plan.apriori is False  # the kill switch was read from os.environ

    def test_u128_bit_falls_back_to_legacy_with_a_warn(self) -> None:
        # Ruling 10 — dormant at today's 60-bit vocabulary, armed for u128.
        plan = resolve_sweep_exploration(
            EXTINCTION_K_TOKEN, condition_bits=[0, 1, 64], max_combos=None, env={}
        )
        assert plan.apriori is False
        assert plan.k_values == [1, 2]
        assert len(plan.notes) == 1
        assert "64" in plan.notes[0]
        assert "u64-only" in plan.notes[0]


class TestResolverLadderTrim:
    """Explicit ``--max-combos`` × extinction (ruling 9): the contiguous-prefix trim."""

    def test_explicit_cap_trims_the_ladder_with_a_loud_note(self) -> None:
        # n_unique=6: C(6,1)=6 <= 10, C(6,2)=15 > 10 ⇒ ladder [1] + the note.
        plan = resolve_sweep_exploration(
            EXTINCTION_K_TOKEN, condition_bits=_BITS_6, max_combos=10, env={}
        )
        assert plan.apriori is True
        assert plan.k_values == [1]
        assert len(plan.notes) == 1
        assert "TRIMMED" in plan.notes[0]
        assert "k=2" in plan.notes[0]  # the first refused level is named

    def test_cap_admitting_the_whole_ladder_needs_no_note(self) -> None:
        # C(6,3)=20 is the ladder maximum ⇒ a cap of 20 admits every level.
        plan = resolve_sweep_exploration(
            EXTINCTION_K_TOKEN, condition_bits=_BITS_6, max_combos=20, env={}
        )
        assert plan.k_values == [1, 2, 3, 4, 5, 6]
        assert plan.notes == ()

    def test_cap_refusing_even_k1_is_a_clean_user_error(self) -> None:
        # C(6,1)=6 > 5 ⇒ an empty ladder is a refusal, never a silent no-op.
        with pytest.raises(CliUserError, match="refuses even k=1"):
            resolve_sweep_exploration(
                EXTINCTION_K_TOKEN, condition_bits=_BITS_6, max_combos=5, env={}
            )


# ──────────────────── the cmd_sweep_all env-setdefault law ────────────────────


def _sweep_all_argv(lake: Path) -> list[str]:
    return [
        "sweep-all",
        "--lake-root",
        str(lake),
        "--start",
        "2024-01-01",
        "--end",
        "2024-01-02",
    ]


class TestSweepAllEnvSetdefault:
    """The spawn-inheritance seam: the env write happens EAGERLY, before any
    lake I/O — proven by driving the REAL dispatch against a NONEXISTENT lake
    root (the loud refusal fires AFTER the resolver + env write)."""

    def test_default_k_sets_the_env_before_lake_io(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(SWEEP_APRIORI_ENV, raising=False)
        rc = main(_sweep_all_argv(tmp_path / "no-such-lake"))
        assert rc == EXIT_USER_ERROR  # the missing-lake refusal, not a sweep
        assert os.environ[SWEEP_APRIORI_ENV] == "1"

    def test_blank_env_is_overwritten_so_plan_and_engine_agree(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A SET-but-BLANK value resolves like unset but apriori_enabled() would
        # read it OFF — the overwrite closes the disagreement window.
        monkeypatch.setenv(SWEEP_APRIORI_ENV, "")
        main(_sweep_all_argv(tmp_path / "no-such-lake"))
        assert os.environ[SWEEP_APRIORI_ENV] == "1"

    def test_truthy_operator_value_is_left_verbatim(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(SWEEP_APRIORI_ENV, "true")
        main(_sweep_all_argv(tmp_path / "no-such-lake"))
        assert os.environ[SWEEP_APRIORI_ENV] == "true"  # setdefault semantics

    def test_explicit_numeric_k_never_touches_the_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(SWEEP_APRIORI_ENV, raising=False)
        rc = main([*_sweep_all_argv(tmp_path / "no-such-lake"), "--k", "1,2"])
        assert rc == EXIT_USER_ERROR
        assert SWEEP_APRIORI_ENV not in os.environ

    def test_kill_switch_prints_the_note_and_keeps_the_env(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv(SWEEP_APRIORI_ENV, "0")
        rc = main(_sweep_all_argv(tmp_path / "no-such-lake"))
        assert rc == EXIT_USER_ERROR
        assert os.environ[SWEEP_APRIORI_ENV] == "0"  # never overwritten
        assert "kill switch" in capsys.readouterr().err

    def test_junk_env_on_the_default_path_fails_loud_before_lake_io(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv(SWEEP_APRIORI_ENV, "ture")
        rc = main(_sweep_all_argv(tmp_path / "no-such-lake"))
        assert rc == EXIT_USER_ERROR
        err = capsys.readouterr().err
        # The env refusal fired — NOT the missing-lake message (call order).
        assert "not a recognised value" in err
        assert "no-such-lake" not in err
        assert os.environ[SWEEP_APRIORI_ENV] == "ture"  # never overwritten

    def test_junk_env_with_explicit_k_stays_silent_legacy(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # The shipped tri-state opt-in semantics: junk is silent-OFF on the
        # explicit-k path (the byte-identity mandate covers it).
        monkeypatch.setenv(SWEEP_APRIORI_ENV, "ture")
        rc = main([*_sweep_all_argv(tmp_path / "no-such-lake"), "--k", "1,2"])
        assert rc == EXIT_USER_ERROR
        assert "not a recognised value" not in capsys.readouterr().err


class TestSweepVerifyExplorationDispatch:
    """sweep-verify rides the SAME shared parser default + resolver + env law.

    An EMPTY results tree with NO recorded context takes the structural-only
    early return BEFORE the resolver, so these tests pass the three context
    overrides to reach the exploration resolution (a tiny fixture — the
    identity tier walks an all-absent expected-store set, INFO-only).
    """

    @staticmethod
    def _verify_argv(tmp_path: Path) -> list[str]:
        (tmp_path / "results").mkdir(exist_ok=True)  # the results-root gate runs FIRST
        return [
            "sweep-verify",
            "--results-root",
            str(tmp_path / "results"),
            "--lake-root",
            str(tmp_path / "no-such-lake"),
            "--start",
            "2024-01-01",
            "--end",
            "2024-01-02",
            # A context-less tree exits structural-only before the resolver —
            # the overrides carry the flow into the exploration resolution.
            "--data-snapshot-hash",
            "ab" * 32,
            "--indicator-version",
            "ind-v1",
            "--code-commit-sha",
            "deadbeef",
            # Keep the tuple enumeration tiny (n_unique=4 ⇒ a 4-rung ladder).
            "--conditions",
            "0,1,2,3",
        ]

    def test_parser_default_k_is_the_token_on_all_grid_surfaces(self) -> None:
        from brutex.cli import build_parser

        parser = build_parser()
        for surface in (
            ["sweep-all", "--start", "2024-01-01", "--end", "2024-01-02"],
            ["sweep-daemon", "--once", "--start", "2024-01-01", "--end", "2024-01-02"],
            ["sweep-verify", "--start", "2024-01-01", "--end", "2024-01-02"],
        ):
            args = parser.parse_args(surface)
            assert args.k == EXTINCTION_K_TOKEN, surface[0]

    def test_junk_env_on_the_default_path_fails_loud(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv(SWEEP_APRIORI_ENV, "ture")
        rc = main(self._verify_argv(tmp_path))
        assert rc == EXIT_USER_ERROR
        assert "not a recognised value" in capsys.readouterr().err
        assert os.environ[SWEEP_APRIORI_ENV] == "ture"  # never overwritten

    def test_default_k_sets_the_env_for_the_shadow_recount(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(SWEEP_APRIORI_ENV, raising=False)
        rc = main(self._verify_argv(tmp_path))
        # An empty tree's absent expected stores are INFO findings — the
        # verify itself is CLEAN; the point is the env write for the shadow
        # recount + effective_max_combos (extinction verify parity).
        assert rc == 0
        assert os.environ[SWEEP_APRIORI_ENV] == "1"

    @staticmethod
    def _write_mode_record(tmp_path: Path, payload: dict[str, object]) -> None:
        from brutex.orchestrator.audit import AuditWriter

        (tmp_path / "results").mkdir(exist_ok=True)
        audit_path = tmp_path / "results" / "audit" / "sweep-all.jsonl"
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        with AuditWriter.open(audit_path) as audit:
            audit.append(action="GRID_SWEEP_START", subject="sweep-all", payload=payload)

    def test_recorded_legacy_vs_resolved_extinction_prints_the_context_mismatch(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The recorded-mode gate (DEC-SWEEP-APRIORI-DEFAULT-001 ruling 8): a
        definite recorded-mode ≠ resolved-mode is a CONTEXT mismatch — named
        with the honest "without corruption" copy (n_enumerated is
        mode-dependent, so a wrong-mode recount byte-diverges on a HEALTHY
        tree; the recount ladder folds it into the existing refuse /
        --allow-context-mismatch → INCONCLUSIVE mechanism, never
        verdict-grade recount_divergence)."""
        monkeypatch.delenv(SWEEP_APRIORI_ENV, raising=False)
        self._write_mode_record(tmp_path, {"sweep_id": "x"})  # legacy: NO apriori key
        rc = main(self._verify_argv(tmp_path))
        assert rc == 0  # INFO findings only — the gate never verdict-grades alone
        out = capsys.readouterr().out
        assert "exploration_mode: recorded legacy vs resolved apriori-extinction" in out
        assert "without corruption" in out

    def test_recorded_apriori_tree_matches_the_resolved_default_silently(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.delenv(SWEEP_APRIORI_ENV, raising=False)
        self._write_mode_record(tmp_path, {"apriori": {"enabled": True, "max_mask_bars": 42}})
        rc = main(self._verify_argv(tmp_path))
        assert rc == 0
        assert "exploration_mode:" not in capsys.readouterr().out
