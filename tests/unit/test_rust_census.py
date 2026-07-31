"""Tests for the runtime Rust-engagement census.

DEC-RUST-ENGAGEMENT-CENSUS-001.

The census exists to answer a question logs cannot: *is Rust actually
running right now?* Its own tests therefore have to be careful about a
specific failure mode — a census that reports a clean bill of health
because it discovered nothing, or because every verdict defaults to the
happy one. Two disciplines guard that here:

* **Anti-vacuity.** :class:`TestDiscovery` pins a floor on the number of
  facades found and asserts named ones are present. A discovery filter
  that silently stops matching would otherwise turn the whole suite
  green while the census reported an empty, perfect table.
* **Classification is driven, not asserted.** :class:`TestClassify`
  exercises the verdict matrix through synthetic inputs rather than
  reading the live table, so a change in the machine's wheel state
  cannot flip a test that is supposed to be about the *rule*.
"""

from __future__ import annotations

import importlib
import textwrap
from pathlib import Path
from types import ModuleType
from typing import Final

import pytest
from brutex.observability.rust_census import (
    CONFIGURED,
    DEGRADED,
    ENGAGED,
    OFF,
    PROVEN,
    REQUIRED,
    UNKNOWN,
    FacadeStatus,
    _accepted_events,
    _classify,
    _crate_for_source,
    _crate_names,
    _delegated_crate,
    _is_required_seam,
    _source_of,
    census,
    discover_facades,
    engaged_events,
    main,
    prove,
    render_table,
    strict_failures,
    summarize,
)

#: A floor, not an exact count — the repo adds facades routinely and an
#: equality pin would fail on unrelated work. The point is to catch a
#: COLLAPSE (a broken filter finding nothing), not to freeze the number.
_MIN_FACADES: Final[int] = 25

#: Facades that must always be discovered. Each is load-bearing for a
#: different reason: one hard-imported by the engine chain, one
#: wheel-required with its oracle deleted, one config seam that resolves
#: its crate through a shared helper rather than its own source.
_MUST_FIND: Final[tuple[str, ...]] = (
    "brutex.engine.rust_stage1a",
    "brutex.feed_rust",
    "brutex.config.paths_rust",
)


class TestDiscovery:
    """Discovery must be structural, complete, and never self-referential."""

    def test_finds_a_plausible_number_of_facades(self) -> None:
        found = discover_facades()
        assert len(found) >= _MIN_FACADES, (
            f"discovery collapsed to {len(found)} facades (floor {_MIN_FACADES}). "
            "A census that finds nothing reports a perfect table -- which is "
            "exactly the failure this floor exists to catch."
        )

    @pytest.mark.parametrize("module", _MUST_FIND)
    def test_finds_named_facades(self, module: str) -> None:
        assert module in discover_facades()

    def test_does_not_audit_itself(self) -> None:
        """The census matches its own name filter and carries the marker
        strings as data; without the self-exclusion it reports a phantom row.
        """
        assert "brutex.observability.rust_census" not in discover_facades()

    def test_discovery_is_deterministic(self) -> None:
        assert discover_facades() == discover_facades()


class TestCrateResolution:
    """The crate a facade routes to, read from source rather than imported."""

    def test_finds_import_module_literal(self) -> None:
        """The DOMINANT shape: the resolve is lazy, so the crate is a string."""
        source = 'mod = importlib.import_module("brutex_lake")'
        assert _crate_for_source(source, frozenset({"brutex_lake"})) == "brutex_lake"

    def test_finds_single_quoted_literal(self) -> None:
        source = "mod = importlib.import_module('fno_math')"
        assert _crate_for_source(source, frozenset({"fno_math"})) == "fno_math"

    def test_finds_plain_import_statement(self) -> None:
        source = "import fno_math\n"
        assert _crate_for_source(source, frozenset({"fno_math"})) == "fno_math"

    def test_finds_from_import_statement(self) -> None:
        source = "from trading_core import evaluate\n"
        assert _crate_for_source(source, frozenset({"trading_core"})) == "trading_core"

    def test_returns_none_when_no_crate_named(self) -> None:
        assert _crate_for_source("x = 1\n", frozenset({"fno_math"})) is None

    def test_prefers_the_longest_matching_crate_name(self) -> None:
        """``brutex_lake`` must not be shadowed by a shorter prefix crate."""
        source = 'importlib.import_module("brutex_lake")'
        crates = frozenset({"brutex", "brutex_lake"})
        assert _crate_for_source(source, crates) == "brutex_lake"

    def test_crate_names_are_derived_from_the_rust_tree(self) -> None:
        names = _crate_names()
        assert "fno_math" in names
        assert "trading_core" in names

    def test_delegated_crate_follows_one_hop(self) -> None:
        """A facade whose resolve lives in a shared seam helper."""
        source = "from brutex.config.seam_core import ConfigRustSeam\n"
        assert _delegated_crate(source, _crate_names()) is not None

    def test_delegated_crate_is_none_without_a_brutex_import(self) -> None:
        assert _delegated_crate("import os\n", _crate_names()) is None

    def test_delegated_crate_skips_unimportable_modules(self) -> None:
        source = "from brutex.does_not_exist_anywhere import Thing\n"
        assert _delegated_crate(source, _crate_names()) is None


class TestRequiredSeam:
    """REQUIRED means *unconditionally* required -- the honesty boundary."""

    def test_explicit_helper_marker(self) -> None:
        assert _is_required_seam("require_feed_kernel()\n") is True

    def test_unavailable_error_class(self) -> None:
        assert _is_required_seam("raise LakeRustWavedUnavailableError(...)\n") is True

    def test_flag_removed_error_class(self) -> None:
        assert _is_required_seam("raise PullStateFlagRemovedError(...)\n") is True

    def test_plain_degrading_facade_is_not_required(self) -> None:
        assert _is_required_seam("log.warning('crate_unavailable')\n") is False

    @pytest.mark.parametrize(
        "conditional",
        ["raise StaleExecWheelError(...)", "raise PullStateKernelError(...)"],
    )
    def test_conditional_refusals_are_not_counted_required(self, conditional: str) -> None:
        """A seam that refuses only on SOME inputs is not proof Rust ran.

        ``exec_rust`` refuses on a stale wheel but degrades on an absent
        one; ``rust_trade_sim``'s wide-kernel refusal covers only the
        >=64-bit path. Counting either as REQUIRED would overstate how
        many seams prove engagement by completing.
        """
        assert _is_required_seam(f"{conditional}\n") is False


class TestSourceOf:
    """Source reading must degrade, never abort the census."""

    def test_reads_a_real_module(self) -> None:
        from brutex.observability import rust_census

        assert "DEC-RUST-ENGAGEMENT-CENSUS-001" in _source_of(rust_census)

    def test_returns_empty_for_a_sourceless_module(self) -> None:
        synthetic = ModuleType("synthetic_no_source")
        assert _source_of(synthetic) == ""


class TestClassify:
    """The verdict matrix, driven through synthetic inputs."""

    def test_required_seam_with_crate_present(self) -> None:
        verdict, _ = _classify(
            env_var=None, flag_on=None, crate="brutex_feed", crate_present=True, required_seam=True
        )
        assert verdict == REQUIRED

    def test_flag_off_is_off_regardless_of_crate(self) -> None:
        verdict, _ = _classify(
            env_var="BRUTEX_X",
            flag_on=False,
            crate="fno_math",
            crate_present=True,
            required_seam=False,
        )
        assert verdict == OFF

    def test_flag_on_with_crate_present_is_engaged(self) -> None:
        verdict, _ = _classify(
            env_var="BRUTEX_X",
            flag_on=True,
            crate="fno_math",
            crate_present=True,
            required_seam=False,
        )
        assert verdict == ENGAGED

    def test_flag_on_with_crate_missing_is_degraded(self) -> None:
        """The dangerous cell: configured for Rust, actually running Python."""
        verdict, _ = _classify(
            env_var="BRUTEX_X",
            flag_on=True,
            crate="ghost",
            crate_present=False,
            required_seam=False,
        )
        assert verdict == DEGRADED

    def test_unresolvable_flag_is_unknown_not_assumed_on(self) -> None:
        verdict, _ = _classify(
            env_var="BRUTEX_X",
            flag_on=None,
            crate="fno_math",
            crate_present=True,
            required_seam=False,
        )
        assert verdict == UNKNOWN

    def test_every_verdict_carries_a_nonempty_detail(self) -> None:
        for flag_on in (True, False, None):
            for present in (True, False, None):
                _, detail = _classify(
                    env_var="BRUTEX_X",
                    flag_on=flag_on,
                    crate="fno_math",
                    crate_present=present,
                    required_seam=False,
                )
                assert detail.strip(), "a verdict without a reason is not evidence"


class TestCensusRun:
    """The live census over this process."""

    def test_returns_a_row_per_discovered_facade(self) -> None:
        rows = census()
        assert len(rows) == len(discover_facades())

    def test_no_facade_is_degraded_in_this_environment(self) -> None:
        """All crates are installed here, so nothing may report DEGRADED.

        If this fails, a wheel is missing or stale and some seam is
        silently running Python -- which is precisely the condition the
        census was built to surface.
        """
        degraded = [r.module for r in census() if r.verdict == DEGRADED]
        assert not degraded, f"degraded facades running Python: {degraded}"

    def test_is_rust_running_agrees_with_the_verdict(self) -> None:
        for row in census():
            assert row.is_rust_running == (row.verdict in {ENGAGED, REQUIRED})

    def test_rows_are_immutable(self) -> None:
        row = census()[0]
        with pytest.raises((AttributeError, TypeError)):
            row.verdict = "TAMPERED"  # type: ignore[misc]


class TestReporting:
    """Table + summary rendering."""

    def test_table_names_every_facade(self) -> None:
        rows = census()
        table = render_table(rows)
        for row in rows:
            assert row.module in table

    def test_summary_counts_sum_to_the_row_count(self) -> None:
        rows = census()
        assert sum(summarize(rows).values()) == len(rows)

    def test_table_of_no_rows_does_not_crash(self) -> None:
        assert isinstance(render_table([]), str)


class TestStrictMode:
    """The preflight gate -- the half that turns config into a guarantee."""

    @staticmethod
    def _row(verdict: str) -> FacadeStatus:
        return FacadeStatus(
            module="synthetic.facade",
            env_var="BRUTEX_SYNTHETIC",
            flag_on=True,
            crate="fno_math",
            crate_present=True,
            verdict=verdict,
            detail="synthetic",
        )

    @pytest.mark.parametrize("verdict", [DEGRADED, OFF, UNKNOWN])
    def test_non_rust_verdicts_fail_strict(self, verdict: str) -> None:
        assert strict_failures([self._row(verdict)])

    @pytest.mark.parametrize("verdict", [ENGAGED, REQUIRED])
    def test_rust_verdicts_pass_strict(self, verdict: str) -> None:
        assert not strict_failures([self._row(verdict)])

    def test_strict_detects_the_suite_wide_hermeticity_belt(self) -> None:
        """Under pytest the census must report OFF, and strict must FAIL.

        This asserts something initially surprising, so it is worth being
        explicit: ``tests/conftest.py`` sets ``os.environ.setdefault(FLAG,
        "0")`` for every Rust flag (the DEC-RUST-DEFAULT-ON-WAVE-001
        hermeticity belts), so **the test suite deliberately runs the
        Python oracles**, not Rust. That is correct — a suite whose
        results depended on which wheels happened to be built would not be
        hermetic.

        The consequence is that a strict preflight is a PRODUCTION check,
        never a test-time one. Asserting "strict passes here" would pin
        the wrong environment and fail for a reason that is by design.

        What is genuinely worth pinning is the converse, and it is a
        strong statement about the census itself: the belt is a real
        divergence between configured intent (default ON) and actual
        behaviour (Python runs), and the census SEES it. A census that
        reported ENGAGED here would be reading source or config rather
        than asking the process — exactly the failure it exists to avoid.
        """
        rows = census()
        off_rows = [r for r in rows if r.verdict == OFF]
        assert off_rows, (
            "expected the conftest hermeticity belt to force facades OFF. "
            "If this fails, either the belt was removed or the census is "
            "reporting intent instead of observed process state."
        )
        assert strict_failures(rows), "strict must refuse a Python-running process"
        for row in off_rows:
            assert row.is_rust_running is False


class TestMain:
    """CLI behaviour -- the exit code is what a preflight actually consumes."""

    def test_exits_zero_when_healthy(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main([]) == 0
        assert "ENGAGED" in capsys.readouterr().out

    def test_strict_exits_one_under_the_test_belt(self, capsys: pytest.CaptureFixture[str]) -> None:
        """See ``test_strict_detects_the_suite_wide_hermeticity_belt``: the
        suite forces every flag OFF, so a strict run here must refuse.
        """
        assert main(["--strict"]) == 1
        assert "STRICT FAILURES" in capsys.readouterr().out

    def test_strict_exits_zero_when_every_facade_is_rust(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The healthy production shape, driven synthetically."""
        healthy = [
            FacadeStatus(
                module=f"synthetic.{verdict.lower()}",
                env_var="BRUTEX_SYNTHETIC",
                flag_on=True,
                crate="fno_math",
                crate_present=True,
                verdict=verdict,
                detail="synthetic",
            )
            for verdict in (ENGAGED, REQUIRED)
        ]
        monkeypatch.setattr("brutex.observability.rust_census.census", lambda: healthy)
        assert main(["--strict"]) == 0
        assert "STRICT FAILURES" not in capsys.readouterr().out

    def test_reports_failures_and_exits_one(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A DEGRADED facade must fail the default (non-strict) run too."""
        bad = FacadeStatus(
            module="synthetic.broken",
            env_var="BRUTEX_SYNTHETIC",
            flag_on=True,
            crate="ghost_crate",
            crate_present=False,
            verdict=DEGRADED,
            detail="crate missing; Python oracle runs",
        )
        monkeypatch.setattr("brutex.observability.rust_census.census", lambda: [bad])
        assert main([]) == 1
        out = capsys.readouterr().out
        assert "synthetic.broken" in out
        assert "DEGRADED" in out

    def test_strict_fails_on_off_where_default_would_pass(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The whole point of ``--strict``: an explicitly OFF facade is a
        legitimate state, but not one a Rust-guaranteed run may contain.
        """
        off_row = FacadeStatus(
            module="synthetic.off",
            env_var="BRUTEX_SYNTHETIC",
            flag_on=False,
            crate="fno_math",
            crate_present=True,
            verdict=OFF,
            detail="explicitly disabled",
        )
        monkeypatch.setattr("brutex.observability.rust_census.census", lambda: [off_row])
        assert main([]) == 0
        capsys.readouterr()
        assert main(["--strict"]) == 1
        assert "STRICT FAILURES" in capsys.readouterr().out

    def test_argv_defaults_to_sys_argv(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr("sys.argv", ["rust_census", "--strict"])
        # Value, not exit code: the point is that ``--strict`` was READ from
        # sys.argv. Under the test belt that means a refusal, and a plain
        # ``main([])`` returns 0 -- so the two exit codes differing is
        # itself the evidence the argument was picked up.
        assert main() == 1
        assert "STRICT FAILURES" in capsys.readouterr().out
        assert main([]) == 0


class TestDegradedPaths:
    """The unhappy branches -- each must produce a row, never an exception.

    A census that raises on one odd module reports nothing about the other
    thirty-six, so every failure mode here has to degrade to an honest row.
    """

    @staticmethod
    def _install(monkeypatch: pytest.MonkeyPatch, name: str, module: ModuleType) -> str:
        monkeypatch.setitem(__import__("sys").modules, name, module)
        return name

    def test_unimportable_module_yields_an_unknown_row(self) -> None:
        rows = census(modules=["brutex.definitely_not_a_real_module"])
        assert len(rows) == 1
        assert rows[0].verdict == UNKNOWN
        assert "import failed" in rows[0].detail

    def test_non_brutex_env_constant_is_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Only ``BRUTEX_*`` strings count as the facade's flag."""
        mod = ModuleType("synthetic.foreign_env")
        mod.SOME_ENV = "OTHER_VENDOR_FLAG"  # type: ignore[attr-defined]
        name = self._install(monkeypatch, "synthetic.foreign_env", mod)
        assert census(modules=[name])[0].env_var is None

    def test_arg_taking_enabled_predicate_is_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``pull_state_rust`` exposes ``…_enabled(raw)``; it must not abort
        resolution, it must simply not contribute an answer.
        """
        mod = ModuleType("synthetic.arg_enabled")
        mod.X_ENV = "BRUTEX_SYNTHETIC"  # type: ignore[attr-defined]

        def _enabled(raw: str) -> bool:
            return bool(raw)

        mod.thing_enabled = _enabled  # type: ignore[attr-defined]
        name = self._install(monkeypatch, "synthetic.arg_enabled", mod)
        assert census(modules=[name])[0].flag_on is None

    def test_non_callable_enabled_attribute_is_skipped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mod = ModuleType("synthetic.str_enabled")
        mod.X_ENV = "BRUTEX_SYNTHETIC"  # type: ignore[attr-defined]
        mod.thing_enabled = "not callable"  # type: ignore[attr-defined]
        name = self._install(monkeypatch, "synthetic.str_enabled", mod)
        assert census(modules=[name])[0].flag_on is None

    def test_sourceless_module_reports_unknown_crate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mod = ModuleType("synthetic.sourceless")
        mod.X_ENV = "BRUTEX_SYNTHETIC"  # type: ignore[attr-defined]
        mod.thing_enabled = lambda: True  # type: ignore[attr-defined]
        name = self._install(monkeypatch, "synthetic.sourceless", mod)
        row = census(modules=[name])[0]
        assert row.crate is None
        assert row.verdict == UNKNOWN

    def test_unimportable_crate_spec_counts_as_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``find_spec`` raises for some malformed names; that is 'absent'."""

        def boom(_name: str) -> object:
            raise ValueError("malformed module name")

        monkeypatch.setattr("brutex.observability.rust_census.importlib.util.find_spec", boom)
        rows = census(modules=["brutex.engine.rust_stage1a"])
        assert rows[0].crate_present is False

    def test_required_seam_with_missing_crate_is_degraded(self) -> None:
        verdict, detail = _classify(
            env_var=None,
            flag_on=None,
            crate="brutex_feed",
            crate_present=False,
            required_seam=True,
        )
        assert verdict == DEGRADED
        assert "raises at call time" in detail

    def test_no_crate_found_is_unknown(self) -> None:
        verdict, detail = _classify(
            env_var="BRUTEX_X",
            flag_on=True,
            crate=None,
            crate_present=None,
            required_seam=False,
        )
        assert verdict == UNKNOWN
        assert "no crate import found" in detail

    def test_discovery_survives_an_unimportable_facade(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One broken optional module must not blind the whole census."""
        real = importlib.import_module

        def selective(name: str, package: str | None = None) -> ModuleType:
            if name.endswith("rust_stage1a"):
                raise ImportError("synthetic breakage")
            return real(name, package)

        monkeypatch.setattr("brutex.observability.rust_census.importlib.import_module", selective)
        found = discover_facades()
        assert "brutex.engine.rust_stage1a" not in found
        assert len(found) >= _MIN_FACADES - 1

    def test_delegated_crate_skips_a_helper_without_a_crate(self) -> None:
        """A helper import that names no crate must not stop the search."""
        source = "from brutex.money import round_money\nfrom brutex.config.seam_core import X\n"
        assert _delegated_crate(source, _crate_names()) is not None


class TestCrateNamesFallback:
    """A wheel-only deployment has no ``rust/`` tree."""

    @pytest.fixture
    def relocated(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        module_path = tmp_path / "a" / "b" / "rust_census.py"
        module_path.parent.mkdir(parents=True)
        module_path.write_text("")
        monkeypatch.setattr("brutex.observability.rust_census.__file__", str(module_path))
        return tmp_path

    def test_absent_rust_tree_yields_empty_set(self, relocated: Path) -> None:
        assert _crate_names() == frozenset()

    def test_unreadable_manifest_is_skipped(self, relocated: Path) -> None:
        crate = relocated / "rust" / "some_crate"
        crate.mkdir(parents=True)
        (crate / "Cargo.toml").mkdir()  # a DIRECTORY where a file is expected
        assert _crate_names() == frozenset()

    def test_package_name_is_normalised_to_the_module_name(self, relocated: Path) -> None:
        """``brutex-lake`` the package builds ``brutex_lake`` the module."""
        crate = relocated / "rust" / "brutex-lake"
        crate.mkdir(parents=True)
        (crate / "Cargo.toml").write_text(
            textwrap.dedent(
                """
                [package]
                name = "brutex-lake"
                version = "0.1.0"
                """
            ).strip()
        )
        assert _crate_names() == frozenset({"brutex_lake"})

    def test_manifest_naming_nothing_contributes_no_crate(self, relocated: Path) -> None:
        """A manifest with neither a ``[lib]`` nor a ``[package]`` name.

        Malformed rather than hypothetical: a workspace-root or
        `[workspace]`-only ``Cargo.toml`` has no crate name at all. It must
        be skipped silently, not guessed at from the directory name -- a
        guessed crate would produce a confident, wrong verdict.
        """
        crate = relocated / "rust" / "nameless"
        crate.mkdir(parents=True)
        (crate / "Cargo.toml").write_text("[workspace]\nmembers = []\n")
        assert _crate_names() == frozenset()

    def test_lib_name_wins_over_package_name(self, relocated: Path) -> None:
        crate = relocated / "rust" / "some-crate"
        crate.mkdir(parents=True)
        (crate / "Cargo.toml").write_text(
            textwrap.dedent(
                """
                [package]
                name = "some-crate"

                [lib]
                name = "renamed_lib"
                """
            ).strip()
        )
        assert _crate_names() == frozenset({"renamed_lib"})


# ──────────────────────────────────────────────────────────────────────
# Call-level proof — DEC-RUST-ENGAGEMENT-CENSUS-002
# ──────────────────────────────────────────────────────────────────────


def _row(module: str, verdict: str) -> FacadeStatus:
    return FacadeStatus(
        module=module,
        env_var="BRUTEX_X",
        flag_on=True,
        crate="some_crate",
        crate_present=True,
        verdict=verdict,
    )


class TestEngagedEvents:
    """``engaged_events`` reads who answered, from the log stream itself."""

    def test_collects_only_engaged_suffixed_events(self) -> None:
        import structlog

        def workload() -> None:
            log = structlog.get_logger("probe")
            log.info("money_rust.engaged", crate_version="0.4.0")
            log.info("lake_rust_render.engaged")
            log.warning("lake_rust_merge.degraded", reason="wheel_missing")
            log.info("some.unrelated.event")

        assert engaged_events(workload) == {"money_rust", "lake_rust_render"}

    def test_empty_when_nothing_answered(self) -> None:
        assert engaged_events(lambda: None) == frozenset()

    def test_non_string_event_is_ignored_not_crashed(self) -> None:
        """A non-str ``event`` is malformed input, not a reason to explode.

        The capture list is other people's data; a defensive read keeps a
        stray structlog processor from taking the whole preflight down.
        """
        import structlog

        def workload() -> None:
            structlog.get_logger("probe").info(12345)

        assert engaged_events(workload) == frozenset()

    def test_capture_is_restored_even_when_the_workload_raises(self) -> None:
        """The exception propagates AND the global config is put back.

        Swallowing it would report a half-populated set as if it were a
        complete measurement -- "these facades are not engaged" when the
        truth is "we never got that far".
        """
        import structlog

        before = structlog.get_config()
        with pytest.raises(RuntimeError, match="boom"):
            engaged_events(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        assert structlog.get_config() == before

    def test_capture_is_restored_on_the_happy_path_too(self) -> None:
        import structlog

        before = structlog.get_config()
        engaged_events(lambda: None)
        assert structlog.get_config() == before


class TestProve:
    """``prove`` re-verdicts ENGAGED rows from measured behaviour."""

    def test_engaged_that_answered_becomes_proven(self) -> None:
        import structlog

        rows = [_row("brutex.money_rust", ENGAGED)]
        out = prove(lambda: structlog.get_logger("p").info("money_rust.engaged"), rows)
        assert [r.verdict for r in out] == [PROVEN]
        assert "money_rust.engaged observed" in out[0].detail

    def test_engaged_that_stayed_silent_becomes_configured(self) -> None:
        rows = [_row("brutex.money_rust", ENGAGED)]
        out = prove(lambda: None, rows)
        assert [r.verdict for r in out] == [CONFIGURED]
        assert "uncalled" in out[0].detail

    @pytest.mark.parametrize("verdict", [REQUIRED, DEGRADED, OFF, UNKNOWN])
    def test_non_engaged_rows_pass_through_untouched(self, verdict: str) -> None:
        """Already decided by stronger evidence than a workload trace.

        A silent REQUIRED facade in particular is not a finding -- it has
        no Python arm to fall back to, so silence proves nothing about it.
        """
        rows = [_row("brutex.feed_rust", verdict)]
        out = prove(lambda: None, rows)
        assert out == rows

    def test_rows_default_to_a_live_census(self) -> None:
        """Omitting *rows* censuses this process rather than inventing one."""
        out = prove(lambda: None)
        assert out, "a live census must find facades"
        assert not any(r.verdict == ENGAGED for r in out), (
            "every ENGAGED row must be re-verdicted PROVEN or CONFIGURED"
        )

    def test_the_dotted_path_is_never_the_join_key(self) -> None:
        """The event name is a short token, never the module's dotted path."""
        rows = [_row("brutex.data.lake_rust_render", ENGAGED)]
        import structlog

        out = prove(lambda: structlog.get_logger("p").info("lake_rust_render.engaged"), rows)
        assert out[0].verdict == PROVEN

    def test_a_different_facade_answering_does_not_prove_this_one(self) -> None:
        import structlog

        rows = [_row("brutex.money_rust", ENGAGED)]
        out = prove(lambda: structlog.get_logger("p").info("models_rust.engaged"), rows)
        assert out[0].verdict == CONFIGURED


class TestAcceptedEvents:
    """The join key is DERIVED from source, because the leaf name is wrong.

    Measured on the live census: 10 of 32 ENGAGED facades emit under a name
    that is not their module leaf. A leaf join reported CONFIGURED for
    facades that had genuinely answered, so the derivation below is the
    load-bearing part of :func:`prove`, not a refinement.
    """

    def test_source_literals_win_over_the_leaf_name(self) -> None:
        got = _accepted_events(
            "brutex.config.hardware_envelope_rust",
            'x = 1\n_LOG.info("hwenv_rust.engaged", crate_version=v)\n',
        )
        assert got == frozenset({"hwenv_rust"})
        assert "hardware_envelope_rust" not in got, "the leaf must not leak in as well"

    def test_multiple_literals_are_all_accepted(self) -> None:
        got = _accepted_events("brutex.x.multi", '"a_rust.engaged"\n"b_rust.engaged"\n')
        assert got == frozenset({"a_rust", "b_rust"})

    def test_leaf_is_the_fallback_when_source_holds_no_literal(self) -> None:
        """``ConfigRustSeam`` renders ``f"{seam}_rust.engaged"`` == the leaf."""
        got = _accepted_events("brutex.config.paths_rust", "no literal here\n")
        assert got == frozenset({"paths_rust"})

    def test_unreadable_source_still_yields_the_leaf(self) -> None:
        assert _accepted_events("brutex.a.b_rust", "") == frozenset({"b_rust"})

    @pytest.mark.parametrize(
        ("module", "event"),
        [
            ("brutex.config.hardware_envelope_rust", "hwenv_rust"),
            ("brutex.crossverify.rust_seam", "crossverify_rust"),
            ("brutex.engine.rust_enum", "sweep_rust_enum"),
            ("brutex.engine.rust_metrics", "sweep_rust_metrics"),
            ("brutex.metrics.compute_rust", "metrics_rust_fold"),
        ],
    )
    def test_real_name_mismatched_facades_are_provable(self, module: str, event: str) -> None:
        """The five facades a leaf join would have silently missed.

        Each is a REAL module read from its REAL source, so a rename that
        breaks the derivation fails here rather than degrading ``prove``
        into a permanent CONFIGURED for that seam.
        """
        import structlog

        pytest.importorskip("brutex.observability.rust_census")
        importlib.import_module(module)
        out = prove(
            lambda: structlog.get_logger("p").info(f"{event}.engaged"),
            [_row(module, ENGAGED)],
        )
        assert out[0].verdict == PROVEN, f"{module} must be provable via {event}.engaged"
        assert out[0].detail == f"{event}.engaged observed"

    def test_named_unprovable_residuals_read_configured_not_proven(self) -> None:
        """The two conservative false-negatives, pinned so they stay honest.

        ``rust_ranking`` delegates to the SHARED ``rust_parity.engaged`` and
        ``meta_rust`` emits no first-answer event at all. Both must read
        CONFIGURED — never PROVEN — even when a shared event fires, because
        a false PROVEN is the one direction this gate must never fail in.
        """
        import structlog

        for module in ("brutex.ranking.rust_ranking", "brutex.meta.meta_rust"):
            importlib.import_module(module)
            out = prove(
                lambda: structlog.get_logger("p").info("rust_parity.engaged", family="other"),
                [_row(module, ENGAGED)],
            )
            assert out[0].verdict == CONFIGURED, f"{module} must not be falsely PROVEN"
