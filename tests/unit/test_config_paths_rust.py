"""Tests for :mod:`brutex.config.paths_rust` — the default-OFF paths seam.

DEC-CONFIG-RUST-LEAVES-001 (MIGRATION.md #1943, slice T35 milestone 3).
Every facade layer is exercised WHEEL-FREE (fake crates via
``sys.modules``); the ON-arm parity legs (:class:`TestRealCrateParity`)
skip cleanly when the wheel is absent and run for real in the
``core-rust-parity.yml`` lane.
"""

from __future__ import annotations

import sys
import types
from collections.abc import Iterator
from pathlib import Path, PurePosixPath

import pytest
from brutex.config import paths, paths_rust, rust_strict, seam_core

# ── Harness ─────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _fresh_seam(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv(paths_rust.PATHS_RUST_ENV, raising=False)
    paths_rust.reset_paths_rust_for_tests()
    yield
    paths_rust.reset_paths_rust_for_tests()


def _oracle_lake_root(override: str | None, home: str) -> str:
    if override:
        return str(PurePosixPath(override))
    return str(Path(home) / ".brutex" / "lake")


def _oracle_volume_parent(path: str) -> str:
    parts = PurePosixPath(path).parts
    if len(parts) >= 3 and parts[0] == "/" and parts[1] == "Volumes":
        return f"/Volumes/{parts[2]}"
    return ""


def _fake_crate(**overrides: object) -> types.ModuleType:
    crate = types.ModuleType("brutex_core")
    crate.__version__ = "0.0-fake"  # type: ignore[attr-defined]
    defaults: dict[str, object] = {
        "paths_lake_root_str": _oracle_lake_root,
        "paths_volume_parent_str": _oracle_volume_parent,
    }
    defaults.update(overrides)
    for name, fn in defaults.items():
        setattr(crate, name, fn)
    return crate


def _bomb_crate() -> types.ModuleType:
    def _boom(*_args: object) -> str:
        raise AssertionError("the crate must not be called on this path")

    return _fake_crate(paths_lake_root_str=_boom, paths_volume_parent_str=_boom)


def _arm(monkeypatch: pytest.MonkeyPatch, crate: types.ModuleType | None) -> None:
    monkeypatch.setenv(paths_rust.PATHS_RUST_ENV, "1")
    monkeypatch.setitem(sys.modules, "brutex_core", crate)


def _strict_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Re-enable the Python oracle fall-back for a *degrade-behaviour* test.

    DEC-RUST-STRICT-NO-FALLBACK-001 ships ``BRUTEX_RUST_STRICT`` ON by
    default, so a class-A (crate absent/stale/errored) or class-B
    (structurally refused crate result) reason now RAISES
    :class:`~brutex.config.rust_strict.RustFallbackRefused` instead of
    silently serving the oracle.  The tests below exercise the still-supported
    ``BRUTEX_RUST_STRICT=0`` mode — the degrade path itself is unchanged, only
    its default reachability moved.  The assertions are untouched.
    """
    monkeypatch.setenv(rust_strict.RUST_STRICT_ENV, "0")


def _identity(lake_root: Path) -> paths.LakeRootIdentity:
    return paths.lake_root_identity(lake_root, ismount=lambda _s: True, stat_dev=lambda _s: 7)


# ── Flag + constants ────────────────────────────────────────────────────────


class TestFlagAndConstants:
    def test_env_name_pinned_and_default_on(self) -> None:
        # The DEC-RUST-DEFAULT-ON-WAVE-001 default pin: unset ⇒ ON.
        assert paths_rust.PATHS_RUST_ENV == "BRUTEX_CORE_RUST_PATHS"
        assert paths_rust.paths_rust_enabled() is True
        assert paths_rust.paths_rust_disabled() is False

    def test_flag_delegates_to_the_shared_tristate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(paths_rust.PATHS_RUST_ENV, "yes")
        assert paths_rust.paths_rust_enabled() is True
        monkeypatch.setenv(paths_rust.PATHS_RUST_ENV, "off")
        assert paths_rust.paths_rust_enabled() is False

    def test_reset_seam_restores_budgets(self) -> None:
        paths_rust.reset_paths_rust_for_tests(remaining=0)
        assert paths_rust._SEAM.burn_in_remaining("lake_root") == 0  # pyright: ignore[reportPrivateUsage]
        paths_rust.reset_paths_rust_for_tests()
        assert (
            paths_rust._SEAM.burn_in_remaining("lake_root")  # pyright: ignore[reportPrivateUsage]
            == seam_core.BURN_IN_SAMPLES
        )


# ── OFF arm — behavior-identical, crate untouched ───────────────────────────


class TestFlagOffNeverTouchesCrate:
    def test_off_arm_serves_oracle_and_never_resolves(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The explicit "0" kill switch (retargeted from the pre-flip unset
        # arm — DEC-RUST-DEFAULT-ON-WAVE-001); assertions unchanged.
        monkeypatch.setenv(paths_rust.PATHS_RUST_ENV, "0")
        monkeypatch.setitem(sys.modules, "brutex_core", _bomb_crate())

        def _resolver_bomb() -> types.ModuleType | None:
            raise AssertionError("_resolve_crate must not run flag-OFF")

        monkeypatch.setattr(
            paths_rust._SEAM,  # pyright: ignore[reportPrivateUsage]
            "_resolve_crate",
            _resolver_bomb,
        )
        assert paths.resolve_lake_root({"BRUTEX_LAKE_ROOT": "/Volumes/T7/lake"}) == Path(
            "/Volumes/T7/lake"
        )
        assert paths.resolve_lake_root({}, home=Path("/home/u")) == Path("/home/u/.brutex/lake")
        identity = _identity(Path("/Volumes/T7/lake"))
        assert identity.volume_parent == Path("/Volumes/T7")
        assert paths_rust.paths_rust_disabled() is False

    def test_explicit_falsy_arm_is_off_too(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(paths_rust.PATHS_RUST_ENV, "0")
        monkeypatch.setitem(sys.modules, "brutex_core", _bomb_crate())
        assert paths.resolve_lake_root({}) == Path.home() / ".brutex" / "lake"
        assert paths_rust.paths_rust_disabled() is False


# ── Eligibility (layer 2 — crate never touched, bomb-pinned) ────────────────


class TestEligibility:
    def test_tilde_override_routes_to_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _arm(monkeypatch, _bomb_crate())
        # The oracle path expands ~ for real; compare against it.
        expected = Path("~/lake").expanduser()
        assert paths.resolve_lake_root({"BRUTEX_LAKE_ROOT": "~/lake"}) == expected
        assert paths_rust.paths_rust_disabled() is False

    def test_non_ascii_and_oversize_override_route_to_oracle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _arm(monkeypatch, _bomb_crate())
        assert paths.resolve_lake_root({"BRUTEX_LAKE_ROOT": "/données/lake"}) == Path(
            "/données/lake"
        )
        long = "/" + "a" * 4096
        assert paths.resolve_lake_root({"BRUTEX_LAKE_ROOT": long}) == Path(long)
        assert paths_rust.paths_rust_disabled() is False

    def test_non_ascii_home_routes_to_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _arm(monkeypatch, _bomb_crate())
        assert paths.resolve_lake_root({}, home=Path("/casa/née")) == Path("/casa/née/.brutex/lake")
        assert paths_rust.paths_rust_disabled() is False

    def test_path_subclass_routes_to_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class MyPath(type(Path())):  # type: ignore[misc]
            pass

        _arm(monkeypatch, _bomb_crate())
        assert paths.resolve_lake_root({}, home=MyPath("/home/u")) == Path("/home/u/.brutex/lake")
        result = paths_rust.route_volume_parent(
            MyPath("/Volumes/T7/lake"), lambda: Path("/Volumes/T7")
        )
        assert result == Path("/Volumes/T7")
        assert paths_rust.paths_rust_disabled() is False

    def test_non_ascii_resolved_path_routes_to_oracle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _arm(monkeypatch, _bomb_crate())
        identity = _identity(Path("/Volumes/Té7/lake"))
        assert identity.volume_parent == Path("/Volumes/Té7")
        assert paths_rust.paths_rust_disabled() is False

    def test_eligibility_predicates_direct(self) -> None:
        eligible_override = paths_rust._eligible_override  # pyright: ignore[reportPrivateUsage]
        assert eligible_override(None) is True
        assert eligible_override("/ok") is True
        assert eligible_override("~nope") is False
        # r2 M1: "~" ANYWHERE routes to the oracle, not just a prefix.
        assert eligible_override("./~nope") is False
        assert eligible_override("a/~b") is False
        assert eligible_override("/a/b~c") is False
        assert eligible_override(123) is False
        eligible_path = paths_rust._eligible_path_obj  # pyright: ignore[reportPrivateUsage]
        assert eligible_path(Path("/ok")) is True
        assert eligible_path("/ok") is False  # a str is not a Path

    def test_normalized_tilde_override_raise_parity(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The r2 M1 fails-before regression: a ``./~user`` override.

        ``PurePosixPath`` normalization drops the ``"."`` component, so
        ``Path("./~zzz").expanduser()`` acts on the ``~zzz`` FIRST
        component and RAISES ``RuntimeError`` for a nonexistent user —
        flag-ON steady state must route to the oracle and raise
        IDENTICALLY. Pre-fix the prefix-only gate crossed the value and
        the crate served the normalized ``~zzz…`` string as a VALUE
        (burn-in structurally cannot catch raise-vs-value).
        """
        env = {"BRUTEX_LAKE_ROOT": "./~brutex_no_such_user_zzz"}
        # The flag-OFF oracle law: expanduser raises for the ghost user.
        with pytest.raises(RuntimeError):
            paths.resolve_lake_root(dict(env))
        # (a) A crate mimicking the REAL normalization law: pre-fix this
        # ADOPTED "~brutex_no_such_user_zzz" as a value — no raise (the
        # fails-before shape, verified via stash on the pre-fix tree).
        _arm(monkeypatch, _fake_crate())
        paths_rust.reset_paths_rust_for_tests(remaining=0)  # steady state
        with pytest.raises(RuntimeError):
            paths.resolve_lake_root(dict(env))
        # (b) Post-fix the gate routes BEFORE the crate is ever touched.
        _arm(monkeypatch, _bomb_crate())
        paths_rust.reset_paths_rust_for_tests(remaining=0)
        with pytest.raises(RuntimeError):
            paths.resolve_lake_root(dict(env))
        assert paths_rust.paths_rust_disabled() is False

    def test_override_arm_never_resolves_home(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The oracle's laziness law: with an override set, Path.home()
        # must never run — flag-ON included.
        _arm(monkeypatch, _fake_crate())
        paths_rust.reset_paths_rust_for_tests(remaining=0)

        def _home_bomb() -> Path:
            raise AssertionError("home must not resolve on the override arm")

        result = paths_rust.route_lake_root(
            "/Volumes/T7/lake", _home_bomb, lambda: Path("/Volumes/T7/lake")
        )
        assert result == Path("/Volumes/T7/lake")


# ── Refuse-poison shape laws (layer 4) ──────────────────────────────────────


class TestRefusePoison:
    @pytest.mark.parametrize("poison", [42, "", "café", "x" * 5000])
    def test_poison_lake_root_refused_serves_oracle(
        self, monkeypatch: pytest.MonkeyPatch, poison: object
    ) -> None:
        _strict_off(monkeypatch)
        _arm(monkeypatch, _fake_crate(paths_lake_root_str=lambda *_a: poison))
        assert paths.resolve_lake_root({"BRUTEX_LAKE_ROOT": "/lake"}) == Path("/lake")

    @pytest.mark.parametrize(
        "poison",
        [42, "café", "/Volumes/", "/Volumes/a/b", "/vol/x", "x" * 5000],
    )
    def test_poison_volume_parent_refused_serves_oracle(
        self, monkeypatch: pytest.MonkeyPatch, poison: object
    ) -> None:
        _strict_off(monkeypatch)
        _arm(monkeypatch, _fake_crate(paths_volume_parent_str=lambda *_a: poison))
        identity = _identity(Path("/Volumes/T7/lake"))
        assert identity.volume_parent == Path("/Volumes/T7")

    def test_valid_volume_parent_shapes(self) -> None:
        valid = paths_rust._valid_volume_parent_str  # pyright: ignore[reportPrivateUsage]
        assert valid("") is True
        assert valid("/Volumes/T7") is True
        assert valid("/Volumes/") is False
        assert valid("/Volumes/a/b") is False
        assert valid("/elsewhere") is False


# ── Burn-in + degrade + wiring ──────────────────────────────────────────────


class TestBurnInAndDegrade:
    def test_burn_in_mismatch_autoreverts_and_ships_oracle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _strict_off(monkeypatch)
        _arm(monkeypatch, _fake_crate(paths_lake_root_str=lambda *_a: "/wrong/place"))
        assert paths.resolve_lake_root({"BRUTEX_LAKE_ROOT": "/lake"}) == Path("/lake")
        assert paths_rust.paths_rust_disabled() is True
        # Latched: the next call short-circuits (bomb-safe).
        monkeypatch.setitem(sys.modules, "brutex_core", _bomb_crate())
        assert paths.resolve_lake_root({"BRUTEX_LAKE_ROOT": "/lake"}) == Path("/lake")

    def test_crate_error_degrades_to_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(*_a: object) -> str:
            raise ValueError("crate refusal")

        _strict_off(monkeypatch)
        _arm(monkeypatch, _fake_crate(paths_volume_parent_str=_boom))
        identity = _identity(Path("/Volumes/T7/lake"))
        assert identity.volume_parent == Path("/Volumes/T7")
        assert paths_rust.paths_rust_disabled() is False

    def test_routed_wiring_crosses_expected_strings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[tuple[object, ...]] = []

        def _spy_lake(override: object, home: object) -> str:
            calls.append(("lake", override, home))
            return _oracle_lake_root(override, home)  # type: ignore[arg-type]

        def _spy_vol(path: object) -> str:
            calls.append(("vol", path))
            return _oracle_volume_parent(path)  # type: ignore[arg-type]

        _arm(
            monkeypatch,
            _fake_crate(paths_lake_root_str=_spy_lake, paths_volume_parent_str=_spy_vol),
        )
        paths_rust.reset_paths_rust_for_tests(remaining=0)
        assert paths.resolve_lake_root({"BRUTEX_LAKE_ROOT": "/Volumes/T7//lake/"}) == Path(
            "/Volumes/T7/lake"
        )
        assert paths.resolve_lake_root({}, home=Path("/home/u")) == Path("/home/u/.brutex/lake")
        identity = _identity(Path("/Volumes/T7/lake"))
        assert identity.volume_parent == Path("/Volumes/T7")
        assert identity.volume_mounted is True
        assert calls == [
            ("lake", "/Volumes/T7//lake/", ""),
            ("lake", None, "/home/u"),
            ("vol", "/Volumes/T7/lake"),
        ]

    def test_volume_parent_none_sentinel_round_trip(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _arm(monkeypatch, _fake_crate())
        paths_rust.reset_paths_rust_for_tests(remaining=0)
        identity = _identity(Path("/home/u/lake"))
        assert identity.volume_parent is None
        assert identity.volume_mounted is None

    def test_volume_parent_burn_in_compares_both_oracle_shapes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A LIVE burn-in budget: the oracle-canonical closure renders the
        # str arm for a /Volumes parent and the "" sentinel for None.
        _arm(monkeypatch, _fake_crate())
        paths_rust.reset_paths_rust_for_tests(remaining=4)
        assert _identity(Path("/Volumes/T7/lake")).volume_parent == Path("/Volumes/T7")
        assert _identity(Path("/home/u/lake")).volume_parent is None
        assert (
            paths_rust._SEAM.burn_in_remaining("volume_parent") == 2  # pyright: ignore[reportPrivateUsage]
        )
        assert paths_rust.paths_rust_disabled() is False


# ── ON-arm parity with the REAL wheel (skips cleanly when absent) ───────────


class TestRealCrateParity:
    @pytest.fixture(autouse=True)
    def _require_wheel(self) -> None:
        pytest.importorskip(
            "brutex_core", reason="brutex-core wheel not built in this env (parity lane covers it)"
        )

    def test_corpus_identical_both_arms_with_engagement_spy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import brutex_core

        overrides = [
            None,
            "/Volumes/T7/lake",
            "/Volumes/T7//lake/",
            "rel/./lake",
            "//net/share",
            "a/../b",
            " /odd",
        ]
        homes = [Path("/home/u"), Path("/"), Path("."), Path("relative/home")]
        off: list[str] = []
        for override in overrides:
            for home in homes:
                env = {} if override is None else {"BRUTEX_LAKE_ROOT": override}
                off.append(str(paths.resolve_lake_root(env, home=home)))

        calls: list[object] = []
        real = brutex_core.paths_lake_root_str

        def _spy(override: object, home: object) -> str:
            calls.append(override)
            return real(override, home)  # type: ignore[arg-type]

        monkeypatch.setattr(brutex_core, "paths_lake_root_str", _spy)
        monkeypatch.setenv(paths_rust.PATHS_RUST_ENV, "1")
        paths_rust.reset_paths_rust_for_tests(remaining=0)  # steady state
        on: list[str] = []
        for override in overrides:
            for home in homes:
                env = {} if override is None else {"BRUTEX_LAKE_ROOT": override}
                on.append(str(paths.resolve_lake_root(env, home=home)))
        assert on == off
        assert len(calls) == len(overrides) * len(homes)  # genuinely ENGAGED
        assert paths_rust.paths_rust_disabled() is False

    def test_volume_parent_identical_both_arms(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cases = [
            Path("/Volumes/T7/lake"),
            Path("/Volumes/T7"),
            Path("/Volumes"),
            Path("//Volumes/T7"),
            Path("/home/u/lake"),
            Path("relative"),
        ]
        off = [_identity(p).volume_parent for p in cases]
        monkeypatch.setenv(paths_rust.PATHS_RUST_ENV, "1")
        paths_rust.reset_paths_rust_for_tests(remaining=0)
        on = [_identity(p).volume_parent for p in cases]
        assert on == off
        assert paths_rust.paths_rust_disabled() is False

    def test_real_crate_burn_in_completes_clean(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(paths_rust.PATHS_RUST_ENV, "1")
        paths_rust.reset_paths_rust_for_tests()  # full budgets live
        for _ in range(seam_core.BURN_IN_SAMPLES + 1):
            assert paths.resolve_lake_root({"BRUTEX_LAKE_ROOT": "/Volumes/T7/lake"}) == Path(
                "/Volumes/T7/lake"
            )
        assert paths_rust.paths_rust_disabled() is False
        assert paths_rust._SEAM.burn_in_remaining("lake_root") == 0  # pyright: ignore[reportPrivateUsage]

    def test_hypothesis_differential_both_laws(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from hypothesis import HealthCheck, given, settings
        from hypothesis import strategies as st

        monkeypatch.setenv(paths_rust.PATHS_RUST_ENV, "1")

        token = st.sampled_from(
            # The `~`-bearing tokens (r2 M1) exercise the tilde gate:
            # tilde-bearing OVERRIDES route to the oracle (skipped below
            # — the oracle may legitimately RAISE for a nonexistent
            # user); tilde-bearing HOMES run the differential for real.
            [
                "a",
                "b",
                "Volumes",
                "lake",
                ".",
                "..",
                "...",
                "",
                " ",
                "T7",
                "~",
                "~x",
                "./~x",
                "a/~b",
            ]
        )
        raw_path = st.builds(
            lambda lead, parts: "/" * lead + "/".join(parts),
            st.integers(min_value=0, max_value=3),
            st.lists(token, min_size=0, max_size=6),
        )

        @settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
        @given(override=st.one_of(st.none(), raw_path), home=raw_path)
        def _differential(override: str | None, home: str) -> None:
            if override is not None and "~" in override:
                # The r2 M1 gate rule verbatim ("~" ANYWHERE — not a
                # prefix check): these route to the oracle, whose
                # expanduser may raise for a nonexistent ~user; the
                # dedicated raise-parity regression covers that arm.
                return
            paths_rust.reset_paths_rust_for_tests(remaining=0)
            env = {} if override is None else {"BRUTEX_LAKE_ROOT": override}
            home_path = Path(home) if home else Path(".")
            off = paths._resolve_lake_root_python(  # pyright: ignore[reportPrivateUsage]
                env.get("BRUTEX_LAKE_ROOT"), home_path
            )
            assert paths.resolve_lake_root(env, home=home_path) == off
            resolved = off
            off_parent = paths._volume_parent_python(resolved)  # pyright: ignore[reportPrivateUsage]
            assert (
                paths_rust.route_volume_parent(
                    resolved,
                    lambda: paths._volume_parent_python(resolved),  # pyright: ignore[reportPrivateUsage]
                )
                == off_parent
            )
            assert paths_rust.paths_rust_disabled() is False, (
                "a burn-in/latch trip inside the differential marks a real divergence"
            )

        _differential()


# ── Workflow wiring pins ────────────────────────────────────────────────────


class TestWorkflowWiring:
    _WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"

    def test_mutation_pair_wired(self) -> None:
        text = (self._WORKFLOWS / "mutation.yml").read_text(encoding="utf-8")
        pair = '"brutex/config/paths_rust.py:tests/unit/test_config_paths_rust.py:"'
        assert pair in text, "paths_rust.py is not wired into mutation.yml's per-module mutmut loop"

    def test_parity_lane_carries_the_paths_flag_on_leg(self) -> None:
        text = (self._WORKFLOWS / "core-rust-parity.yml").read_text(encoding="utf-8")
        assert "BRUTEX_CORE_RUST_PATHS" in text, "core-rust-parity.yml lost the paths flag-ON leg"
        assert "tests/unit/test_config_paths_rust.py" in text, (
            "core-rust-parity.yml no longer runs the paths facade suite"
        )


# ── Strict mode (DEC-RUST-STRICT-NO-FALLBACK-001) ───────────────────────────


class TestStrictRefusal:
    """Strict-ON pins for the shared :class:`ConfigRustSeam` choke points.

    The degrade tests above declare ``BRUTEX_RUST_STRICT=0`` via
    :func:`_strict_off`; these delete the kill switch so strict mode takes
    its shipped default (ON) and pin the refusal. This facade carries no class-C reason at the announced choke points: the ``_eligible_lake_root`` / ``_eligible_volume_parent`` gates return the Python law SILENTLY, never through ``_warn_once``.
    """

    @staticmethod
    def _strict_on(monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(rust_strict.RUST_STRICT_ENV, raising=False)
        assert rust_strict.strict_mode() is True

    def test_seam_warn_once_refuses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._strict_on(monkeypatch)
        with pytest.raises(rust_strict.RustFallbackRefused):
            paths_rust._SEAM._warn_once("crate_stale")  # pyright: ignore[reportPrivateUsage]

    def test_seam_disable_refuses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._strict_on(monkeypatch)
        with pytest.raises(rust_strict.RustFallbackRefused):
            paths_rust._SEAM._disable("crate_unavailable")  # pyright: ignore[reportPrivateUsage]

    def test_unclassified_seam_is_untouched(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # ``strict_facade`` is OPT-IN on the shared core: a seam built without
        # it (e.g. backup_env_rust, whose reasons are not yet classified)
        # stays byte-identical and still degrades silently under strict mode.
        self._strict_on(monkeypatch)
        seam = seam_core.ConfigRustSeam(
            seam="test.unclassified",
            env_var="BRUTEX_TEST_UNCLASSIFIED",
            crate_api=(),
            laws=("x",),
        )
        seam._disable("crate_unavailable")  # pyright: ignore[reportPrivateUsage]
        assert seam.disabled() is True
