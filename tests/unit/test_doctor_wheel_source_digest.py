"""Wheel source-digest doctor-stage tests (DEC-RUST-SOURCE-DIGEST-TWIN-001).

The sibling suite of ``test_doctor_wheel_freshness*.py`` for the NEW
``wheel_source_digest`` / ``wheel_source_digest_fno_math`` stages
(``docs/MIGRATION.md`` #1258): the installed wheel's build.rs-baked
``__source_digest__`` vs the Python twin's (:mod:`brutex.wheel_digest`)
fresh recompute over the repo crate tree — the stage that closes the
"same-version, symbol-compatible behavioural drift" residual gap the
freshness stage names (``brutex/doctor.py``, formerly its lines 616-618
follow-up note).

Fail-before evidence: on ``origin/main`` these stages do not exist —
``from brutex.doctor import check_wheel_source_digest`` fails collection
outright — and the BUG CLASS (a local wheel rebuilt from older source at
the same crate version passing the doctor silently) is demonstrated caught
by the mismatch tests below, which feed a stale-digest wheel stand-in and
assert the WARN names BOTH hex digests.

HONEST scope (pinned in the source too): LOCAL machines only — CI builds
every wheel from source per job (no committed wheel exists anywhere), so CI
can never see a stale wheel; mismatch is WARN, non-fatal, consistent with
the freshness stage + the shared ``BRUTEX_SKIP_WHEEL_FRESHNESS`` escape.
"""

from __future__ import annotations

import importlib
import os
import sys
import types
from pathlib import Path

import pytest
from brutex.doctor import (
    check_brutex_identity_wheel_source_digest,
    check_fno_math_wheel_source_digest,
    check_wheel_source_digest,
    main,
    run_doctor,
)
from brutex.wheel_digest import SourceDigestError, crate_source_digest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _skip_if_local_wheels_stale() -> None:
    """Skip a live-PASS assertion on a LOCAL machine with a stale wheel.

    The live-PASS tests require the installed wheels to be freshly built
    from THIS tree. In CI that holds by construction (ci.yml installs
    `./rust/*` from the job checkout) — so under CI (the `CI` env GitHub
    Actions always sets) this NEVER skips and a mismatch fails loudly, a
    real regression. Locally, a developer with a stale wheel is EXACTLY the
    class the doctor deliberately downgrades to a non-fatal WARN — a red
    unit suite there would be self-inflicted noise, so the tests skip with
    the exact rebuild command instead (hostile-review fold,
    DEC-RUST-SOURCE-DIGEST-TWIN-001). A wheel that fails to IMPORT does NOT
    skip — the tests then fail loudly, because that is breakage, not
    staleness.
    """
    if os.environ.get("CI"):
        return
    stale: list[str] = []
    for module_name, crate in (
        ("trading_core._trading_core", "rust/trading-core"),
        ("fno_math._fno_math", "rust/fno-math"),
        ("brutex_identity._brutex_identity", "rust/brutex-identity"),
    ):
        try:
            compiled = importlib.import_module(module_name)
        except ImportError:
            return  # broken, not stale — let the tests fail loudly
        if getattr(compiled, "__source_digest__", None) != crate_source_digest(REPO_ROOT / crate):
            stale.append(crate)
    if stale:
        pytest.skip(
            "installed wheel(s) stale vs this checkout (the exact class the "
            "doctor downgrades to a non-fatal WARN) — rebuild via "
            "`maturin develop --release` in " + " and ".join(stale)
        )


def _mini_crate(tmp_path: Path) -> Path:
    """A tiny but real crate tree the twin can digest."""
    root = tmp_path / "mini-crate"
    (root / "src").mkdir(parents=True)
    (root / "Cargo.toml").write_text('[package]\nname = "mini"\n', encoding="utf-8")
    (root / "src" / "lib.rs").write_text("pub fn mini() {}\n", encoding="utf-8")
    return root


def _fake_compiled(
    monkeypatch: pytest.MonkeyPatch, package: str, submodule: str, **attrs: object
) -> None:
    """Install a fake compiled module under ``package.submodule``.

    ``importlib.import_module`` returns the ``sys.modules`` entry directly,
    so a stand-in module object lets the tests drive every digest-attr arm
    without uninstalling the real wheel.
    """
    fake = types.ModuleType(f"{package}.{submodule}")
    for name, value in attrs.items():
        setattr(fake, name, value)
    monkeypatch.setitem(sys.modules, f"{package}.{submodule}", fake)


# ────────────────────────────────────────────────────────────────────────
# Live end-to-end (the real installed wheels vs the real repo trees)
# ────────────────────────────────────────────────────────────────────────


class TestLiveWheels:
    """The real wheels, freshly maturin-built from THIS tree, must PASS.

    This is the live proof the PR promises: build.rs baked the digest, the
    doctor recomputes it with the Python twin over the checkout, and the two
    agree byte-for-byte. In CI the wheels are always built from the job's
    own checkout, so this holds there by construction — and these tests
    HARD-assert there. On a LOCAL machine with a genuinely stale wheel they
    skip with the rebuild command instead (see
    :func:`_skip_if_local_wheels_stale`).
    """

    def test_trading_core_live_pass_via_default_crate_root(self) -> None:
        _skip_if_local_wheels_stale()
        result = check_wheel_source_digest(env={})
        assert result.name == "wheel_source_digest"
        assert result.status == "PASS"
        assert "trading_core" in result.detail
        assert "matches the repo crate tree" in result.detail
        # The PASS names the digest it verified.
        assert crate_source_digest(REPO_ROOT / "rust" / "trading-core") in result.detail

    def test_fno_math_live_pass_via_default_crate_root(self) -> None:
        _skip_if_local_wheels_stale()
        result = check_fno_math_wheel_source_digest(env={})
        assert result.name == "wheel_source_digest_fno_math"
        assert result.status == "PASS"
        assert "fno_math" in result.detail
        assert crate_source_digest(REPO_ROOT / "rust" / "fno-math") in result.detail

    def test_brutex_identity_live_pass_via_default_crate_root(self) -> None:
        # DEC-RUST-RUNID-STORES-001 (independent refuter round 2): the third
        # hard-imported wheel joins BOTH doctor drift nets.
        _skip_if_local_wheels_stale()
        result = check_brutex_identity_wheel_source_digest(env={})
        assert result.name == "wheel_source_digest_brutex_identity"
        assert result.status == "PASS"
        assert "brutex_identity" in result.detail
        assert crate_source_digest(REPO_ROOT / "rust" / "brutex-identity") in result.detail

    def test_installed_stamp_matches_twin_recompute_directly(self) -> None:
        _skip_if_local_wheels_stale()
        # Belt-and-suspenders: the raw baked attribute equals the twin's
        # recompute for BOTH wheels (no doctor plumbing in between). The
        # compiled submodules load dynamically — repo convention, they carry
        # no stub (mirrors test_doctor_wheel_freshness_fno_math.py).
        core_compiled = importlib.import_module("trading_core._trading_core")
        fno_compiled = importlib.import_module("fno_math._fno_math")
        assert core_compiled.__source_digest__ == crate_source_digest(
            REPO_ROOT / "rust" / "trading-core"
        )
        assert fno_compiled.__source_digest__ == crate_source_digest(
            REPO_ROOT / "rust" / "fno-math"
        )


class TestLocalStaleSkipGuard:
    """The live-PASS skip guard itself: local-stale skips, CI never does."""

    def test_local_stale_wheel_skips_naming_rebuild_command(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # No CI env + a stale trading_core stamp → the guard skips and the
        # message names the exact rebuild command + the stale crate dir.
        monkeypatch.delenv("CI", raising=False)
        _fake_compiled(monkeypatch, "trading_core", "_trading_core", __source_digest__="0" * 64)
        with pytest.raises(pytest.skip.Exception) as excinfo:
            _skip_if_local_wheels_stale()
        assert "maturin develop --release" in str(excinfo.value)
        assert "rust/trading-core" in str(excinfo.value)
        assert "stale" in str(excinfo.value)

    def test_ci_env_never_skips_even_when_stale(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Under CI the wheels are built from the job checkout by
        # construction, so a mismatch is a REAL regression — the guard must
        # return silently and let the live assertions fail loudly.
        monkeypatch.setenv("CI", "true")
        _fake_compiled(monkeypatch, "trading_core", "_trading_core", __source_digest__="0" * 64)
        _skip_if_local_wheels_stale()

    def test_broken_import_does_not_skip(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A wheel that fails to IMPORT is breakage, not staleness — the
        # guard returns silently so the live tests fail loudly.
        monkeypatch.delenv("CI", raising=False)
        monkeypatch.setitem(sys.modules, "trading_core._trading_core", None)
        _skip_if_local_wheels_stale()


# ────────────────────────────────────────────────────────────────────────
# Branch coverage — every arm of the common helper
# ────────────────────────────────────────────────────────────────────────


class TestCheckWheelSourceDigest:
    def test_env_override_skips(self) -> None:
        result = check_wheel_source_digest(env={"BRUTEX_SKIP_WHEEL_FRESHNESS": "1"})
        assert result.status == "SKIP"
        assert "BRUTEX_SKIP_WHEEL_FRESHNESS=1" in result.detail

    def test_env_override_other_value_does_not_skip(self) -> None:
        _skip_if_local_wheels_stale()  # asserts a live PASS, same envelope
        result = check_wheel_source_digest(env={"BRUTEX_SKIP_WHEEL_FRESHNESS": "0"})
        assert result.status == "PASS"

    def test_default_env_is_os_environ(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRUTEX_SKIP_WHEEL_FRESHNESS", "1")
        result = check_wheel_source_digest()
        assert result.status == "SKIP"

    def test_warn_when_compiled_module_not_importable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # None in sys.modules makes importlib raise ImportError. Same honest
        # reachability envelope as the freshness stage: through the `brutex`
        # CLI a broken wheel import kills the process at `import brutex.cli`
        # first; this arm serves programmatic run_doctor embeddings.
        monkeypatch.setitem(sys.modules, "trading_core._trading_core", None)
        result = check_wheel_source_digest(env={})
        assert result.status == "WARN"
        assert "trading_core wheel not importable" in result.detail
        assert "maturin develop --release" in result.detail
        assert "rust/trading-core" in result.detail

    def test_warn_when_stamp_absent_older_wheel(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A wheel built BEFORE this decision has no __source_digest__ at all
        # → WARN with the rebuild hint (never a crash, never a fake PASS).
        _fake_compiled(monkeypatch, "trading_core", "_trading_core", __version__="0.3.0")
        result = check_wheel_source_digest(env={})
        assert result.status == "WARN"
        assert "no usable __source_digest__" in result.detail
        assert "before the source-digest handshake" in result.detail
        assert "maturin develop --release" in result.detail

    def test_warn_when_stamp_is_not_a_string(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Same arm as absent: a non-str stamp is unusable, not comparable.
        _fake_compiled(monkeypatch, "trading_core", "_trading_core", __source_digest__=1234)
        result = check_wheel_source_digest(env={})
        assert result.status == "WARN"
        assert "no usable __source_digest__" in result.detail

    def test_skip_when_no_crate_tree(self, tmp_path: Path) -> None:
        # A wheel-only install with no repo checkout: honestly not checkable.
        result = check_wheel_source_digest(crate_root=tmp_path / "nope", env={})
        assert result.status == "SKIP"
        assert "not checkable" in result.detail

    def test_warn_when_twin_recompute_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A framing-violating tree (the twin raises SourceDigestError) must
        # WARN, never crash the doctor. Driven through the real exception
        # type via a monkeypatched recompute for determinism.
        root = _mini_crate(tmp_path)

        def _boom(_: Path) -> str:
            raise SourceDigestError("non-UTF-8 file name synthetic")

        monkeypatch.setattr("brutex.doctor.crate_source_digest", _boom)
        result = check_wheel_source_digest(crate_root=root, env={})
        assert result.status == "WARN"
        assert "source-digest recompute failed" in result.detail
        assert "non-UTF-8 file name synthetic" in result.detail

    def test_warn_when_twin_recompute_hits_recursion_limit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # An adversarial symlink CYCLE under src/ makes the twin's recursive
        # walk raise RecursionError — which is NOT an OSError, so before the
        # hostile-review fold it escaped the recompute catch and CRASHED
        # run_doctor instead of WARNing. Driven via a monkeypatched recompute
        # (a real ~1000-deep cycle walk is wasteful and depth-limit
        # dependent); the exception TYPE is the thing under test.
        root = _mini_crate(tmp_path)

        def _cycle(_: Path) -> str:
            raise RecursionError("maximum recursion depth exceeded")

        monkeypatch.setattr("brutex.doctor.crate_source_digest", _cycle)
        result = check_wheel_source_digest(crate_root=root, env={})
        assert result.status == "WARN"
        assert "source-digest recompute failed" in result.detail
        assert "maximum recursion depth exceeded" in result.detail

    def test_warn_on_mismatch_names_both_digests(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # THE bug class this stage exists for: a wheel whose baked digest is
        # not this checkout's digest (built from older source, same version,
        # same symbols) — undetectable by every pre-existing doctor stage
        # (fail-before), now a WARN naming BOTH hex digests (pass-after).
        root = _mini_crate(tmp_path)
        stale = "0" * 64
        _fake_compiled(monkeypatch, "trading_core", "_trading_core", __source_digest__=stale)
        result = check_wheel_source_digest(crate_root=root, env={})
        assert result.status == "WARN"
        assert stale in result.detail
        assert crate_source_digest(root) in result.detail
        assert "DIFFERENT source" in result.detail
        assert "maturin develop --release" in result.detail

    def test_pass_on_match_with_explicit_crate_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _mini_crate(tmp_path)
        expected = crate_source_digest(root)
        _fake_compiled(monkeypatch, "trading_core", "_trading_core", __source_digest__=expected)
        result = check_wheel_source_digest(crate_root=root, env={})
        assert result.status == "PASS"
        assert expected in result.detail


class TestFnoMathTwinBinding:
    """The fno_math wrapper rides the SAME common helper (COMMON rule)."""

    def test_env_override_skips(self) -> None:
        result = check_fno_math_wheel_source_digest(env={"BRUTEX_SKIP_WHEEL_FRESHNESS": "1"})
        assert result.name == "wheel_source_digest_fno_math"
        assert result.status == "SKIP"

    def test_warn_when_not_importable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "fno_math._fno_math", None)
        result = check_fno_math_wheel_source_digest(env={})
        assert result.status == "WARN"
        assert "fno_math wheel not importable" in result.detail
        assert "rust/fno-math" in result.detail

    def test_warn_on_mismatch(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        root = _mini_crate(tmp_path)
        stale = "f" * 64
        _fake_compiled(monkeypatch, "fno_math", "_fno_math", __source_digest__=stale)
        result = check_fno_math_wheel_source_digest(crate_root=root, env={})
        assert result.status == "WARN"
        assert stale in result.detail
        assert crate_source_digest(root) in result.detail

    def test_default_env_is_os_environ(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRUTEX_SKIP_WHEEL_FRESHNESS", "1")
        result = check_fno_math_wheel_source_digest()
        assert result.status == "SKIP"


class TestBrutexIdentityTwinBinding:
    """The brutex_identity wrapper rides the SAME common helper (COMMON rule;
    DEC-RUST-RUNID-STORES-001 independent refuter round 2 — the third
    hard-imported wheel gains BOTH doctor drift nets)."""

    def test_env_override_skips(self) -> None:
        result = check_brutex_identity_wheel_source_digest(env={"BRUTEX_SKIP_WHEEL_FRESHNESS": "1"})
        assert result.name == "wheel_source_digest_brutex_identity"
        assert result.status == "SKIP"

    def test_warn_when_not_importable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "brutex_identity._brutex_identity", None)
        result = check_brutex_identity_wheel_source_digest(env={})
        assert result.status == "WARN"
        assert "brutex_identity wheel not importable" in result.detail
        assert "rust/brutex-identity" in result.detail

    def test_warn_on_mismatch(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        root = _mini_crate(tmp_path)
        stale = "f" * 64
        _fake_compiled(monkeypatch, "brutex_identity", "_brutex_identity", __source_digest__=stale)
        result = check_brutex_identity_wheel_source_digest(crate_root=root, env={})
        assert result.status == "WARN"
        assert stale in result.detail
        assert crate_source_digest(root) in result.detail

    def test_default_env_is_os_environ(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRUTEX_SKIP_WHEEL_FRESHNESS", "1")
        result = check_brutex_identity_wheel_source_digest()
        assert result.status == "SKIP"


# ────────────────────────────────────────────────────────────────────────
# Aggregation — run_doctor / main
# ────────────────────────────────────────────────────────────────────────


class TestSourceDigestAggregation:
    def test_run_doctor_includes_both_digest_stages_live(self, tmp_path: Path) -> None:
        _skip_if_local_wheels_stale()  # live PASS through the aggregate
        report = run_doctor(
            audit_dir=tmp_path / "no_audit",
            runtime_file=tmp_path / "no_runtime.json",
            lake_root=tmp_path / "no_lake",
        )
        statuses = {c.name: c.status for c in report.checks}
        # Default crate roots resolve to THIS repo checkout; the freshly
        # built wheels match it → live PASS through the aggregate too.
        assert statuses["wheel_source_digest"] == "PASS"
        assert statuses["wheel_source_digest_fno_math"] == "PASS"

    def test_mismatch_warn_is_not_fatal_for_main(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # A stale-wheel WARN must never flip the doctor red (non-fatal by
        # design, mirroring the freshness stage): the real wheel's digest
        # cannot match a synthetic mini-crate, so both stages WARN — and the
        # exit code stays 0 with the ⚠️ glyph rendered.
        root = _mini_crate(tmp_path)
        rc = main(
            audit_dir=tmp_path / "no_audit",
            runtime_file=tmp_path / "no_runtime.json",
            lake_root=tmp_path / "no_lake",
            wheel_source_crate_root=root,
            fno_math_wheel_source_crate_root=root,
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "⚠️" in out
        assert "wheel_source_digest" in out
        assert "wheel_source_digest_fno_math" in out
        assert "OVERALL: ✅ PASS" in out

    def test_run_doctor_threads_explicit_crate_roots(self, tmp_path: Path) -> None:
        report = run_doctor(
            audit_dir=tmp_path / "no_audit",
            runtime_file=tmp_path / "no_runtime.json",
            lake_root=tmp_path / "no_lake",
            wheel_source_crate_root=tmp_path / "absent-a",
            fno_math_wheel_source_crate_root=tmp_path / "absent-b",
            brutex_identity_wheel_source_crate_root=tmp_path / "absent-c",
        )
        statuses = {c.name: c.status for c in report.checks}
        assert statuses["wheel_source_digest"] == "SKIP"
        assert statuses["wheel_source_digest_fno_math"] == "SKIP"
        assert statuses["wheel_source_digest_brutex_identity"] == "SKIP"
