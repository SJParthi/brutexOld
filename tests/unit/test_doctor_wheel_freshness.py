"""Wheel-source freshness handshake tests (R2-CC-G1).

Covers BOTH halves of the handshake:

* the ``trading_core`` shim's import-time compiled-surface assertion
  (``_assert_compiled_surface``) — the actionable ImportError on a stale
  ``.so`` missing a re-exported symbol (the exact failure three audit
  agents hit: a wheel missing ``seconds_of_day``);
* ``brutex.doctor.check_wheel_freshness`` — the doctor stage comparing the
  INSTALLED wheel (symbol surface + ``__build_crate_version__``) against
  the in-repo ``EXPECTED_BUILD.json`` fingerprint: WARN (non-fatal) on
  mismatch, PASS on match, SKIP via ``BRUTEX_SKIP_WHEEL_FRESHNESS=1``.

HONEST residual gap (Golden Rule #10, pinned in the source too): version +
symbol-set checks catch a stale wheel that dropped/added a symbol or changed
crate version; they do NOT catch same-version symbol-compatible BEHAVIORAL
drift — that needs a build.rs source-content digest (named follow-up).
"""

# Whitebox test of the shim's private handshake internals — same convention as
# the repo's other whitebox files (pyproject [tool.pyright] reportPrivateUsage).
# pyright: reportPrivateUsage=none

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
import trading_core
from brutex.doctor import DoctorReport, check_wheel_freshness, main, run_doctor

# ONE import style only for trading_core (CodeQL py/import-and-import-from):
# the private handshake internals are read off the module object — the SAME
# installed-package surface the prior from-import bound, so nothing the tests
# probe is weakened. Local aliases keep every test body unchanged.
_EXPECTED_COMPILED_SYMBOLS = trading_core._EXPECTED_COMPILED_SYMBOLS
_assert_compiled_surface = trading_core._assert_compiled_surface
_trading_core = trading_core._trading_core

REPO_FINGERPRINT = (
    Path(__file__).resolve().parents[2]
    / "rust"
    / "trading-core"
    / "python"
    / "trading_core"
    / "EXPECTED_BUILD.json"
)


def _fingerprint_payload() -> dict[str, Any]:
    payload = json.loads(REPO_FINGERPRINT.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _write_fingerprint(tmp_path: Path, payload: object) -> Path:
    target = tmp_path / "EXPECTED_BUILD.json"
    target.write_text(json.dumps(payload), encoding="utf-8")
    return target


def _fake_stale_module(missing: str) -> object:
    """A fake compiled module carrying every expected symbol EXCEPT ``missing``."""

    class FakeCompiled:
        """Stand-in for a stale ``_trading_core`` .so."""

    fake = FakeCompiled()
    for name in _EXPECTED_COMPILED_SYMBOLS:
        if name != missing:
            setattr(fake, name, getattr(_trading_core, name))
    return fake


# ────────────────────────────────────────────────────────────────────────
# Shim import-time assertion
# ────────────────────────────────────────────────────────────────────────


class TestShimCompiledSurfaceAssertion:
    def test_real_compiled_module_passes(self) -> None:
        # The freshly-built wheel exposes every re-exported symbol.
        _assert_compiled_surface(_trading_core)

    def test_missing_symbol_raises_actionable_import_error(self) -> None:
        # The exact stale-wheel failure this environment hit: an installed
        # .so missing seconds_of_day.
        fake = _fake_stale_module("seconds_of_day")
        with pytest.raises(ImportError, match="seconds_of_day"):
            _assert_compiled_surface(fake)

    def test_error_message_names_maturin_rebuild(self) -> None:
        fake = _fake_stale_module("greeks")
        with pytest.raises(ImportError) as excinfo:
            _assert_compiled_surface(fake)
        message = str(excinfo.value)
        assert "greeks" in message
        assert "STALE" in message
        assert "maturin develop --release" in message

    def test_multiple_missing_symbols_all_named(self) -> None:
        class Empty:
            """A wheel exposing nothing at all."""

        with pytest.raises(ImportError) as excinfo:
            _assert_compiled_surface(Empty(), expected=("alpha_sym", "beta_sym"))
        message = str(excinfo.value)
        assert "alpha_sym" in message
        assert "beta_sym" in message


class TestBuildCrateVersion:
    def test_exposed_and_derived_from_compiled_version(self) -> None:
        # (b) stamped on the package, derived from the compiled module's
        # CARGO_PKG_VERSION-baked __version__ — never hardcoded.
        assert trading_core.__build_crate_version__ == _trading_core.__version__

    def test_build_stamp_not_in_all_so_facade_surface_stays_frozen(self) -> None:
        # Deliberately NOT in __all__: brutex.math rebuilds the merged facade's
        # frozen public surface from each kernel's __all__, so listing the
        # trading-core-specific build stamp there would leak it into (and
        # break) tests/golden/test_math_facade_golden.py. Attribute access is
        # how both consumers (doctor + the golden generator) read it.
        assert "__build_crate_version__" not in trading_core.__all__
        assert isinstance(trading_core.__build_crate_version__, str)

    def test_matches_fingerprint_file_for_fresh_wheel(self) -> None:
        payload = _fingerprint_payload()
        assert trading_core.__build_crate_version__ == payload["crate_version"]

    def test_fingerprint_symbols_pin_the_shim_list(self) -> None:
        # The JSON fingerprint and the in-code expected-symbol list are the
        # SAME contract — pinned equal so they cannot silently drift.
        payload = _fingerprint_payload()
        assert payload["symbols"] == sorted(_EXPECTED_COMPILED_SYMBOLS)

    def test_fingerprint_symbols_match_installed_wheel(self) -> None:
        payload = _fingerprint_payload()
        installed = {a for a in dir(_trading_core) if not a.startswith("_") or a == "__version__"}
        assert set(payload["symbols"]) == installed


# ────────────────────────────────────────────────────────────────────────
# Doctor stage — check_wheel_freshness
# ────────────────────────────────────────────────────────────────────────


class TestCheckWheelFreshness:
    def test_pass_on_match_via_default_fingerprint(self) -> None:
        # Default resolution: EXPECTED_BUILD.json next to the installed
        # trading_core package (the repo copy under maturin develop).
        result = check_wheel_freshness(env={})
        assert result.name == "wheel_freshness"
        assert result.status == "PASS"
        assert "matches" in result.detail
        # Honest-envelope wording: PASS never overclaims behavioral freshness.
        assert "behavioral drift not detectable" in result.detail

    def test_pass_on_match_via_explicit_fingerprint(self) -> None:
        result = check_wheel_freshness(fingerprint_file=REPO_FINGERPRINT, env={})
        assert result.status == "PASS"

    def test_env_override_skips(self) -> None:
        result = check_wheel_freshness(env={"BRUTEX_SKIP_WHEEL_FRESHNESS": "1"})
        assert result.status == "SKIP"
        assert "BRUTEX_SKIP_WHEEL_FRESHNESS=1" in result.detail

    def test_env_override_other_value_does_not_skip(self) -> None:
        result = check_wheel_freshness(env={"BRUTEX_SKIP_WHEEL_FRESHNESS": "0"})
        assert result.status == "PASS"

    def test_default_env_is_os_environ(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRUTEX_SKIP_WHEEL_FRESHNESS", "1")
        result = check_wheel_freshness()
        assert result.status == "SKIP"

    def test_warn_when_trading_core_not_importable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # None in sys.modules makes `import trading_core` raise ImportError.
        # HONEST reachability (pinned in the docstring too): this WARN arm is
        # reachable only from a programmatic run_doctor/check_wheel_freshness
        # call like this one — through the `brutex` CLI a broken trading_core
        # import kills the process at `import brutex.cli` (brutex.vocabulary
        # imports trading_core at module import) before any doctor stage runs.
        monkeypatch.setitem(sys.modules, "trading_core", None)
        result = check_wheel_freshness(env={})
        assert result.status == "WARN"
        assert "not importable" in result.detail
        assert "maturin develop --release" in result.detail

    def test_skip_when_fingerprint_file_absent(self, tmp_path: Path) -> None:
        result = check_wheel_freshness(fingerprint_file=tmp_path / "nope.json", env={})
        assert result.status == "SKIP"
        assert "not checkable" in result.detail

    def test_warn_on_malformed_json(self, tmp_path: Path) -> None:
        target = tmp_path / "EXPECTED_BUILD.json"
        target.write_text("{not valid json", encoding="utf-8")
        result = check_wheel_freshness(fingerprint_file=target, env={})
        assert result.status == "WARN"
        assert "malformed" in result.detail

    def test_warn_on_missing_key(self, tmp_path: Path) -> None:
        target = _write_fingerprint(tmp_path, {"symbols": []})
        result = check_wheel_freshness(fingerprint_file=target, env={})
        assert result.status == "WARN"
        assert "malformed" in result.detail

    def test_warn_on_wrong_value_types(self, tmp_path: Path) -> None:
        target = _write_fingerprint(tmp_path, {"crate_version": "0.3.0", "symbols": "not-a-list"})
        result = check_wheel_freshness(fingerprint_file=target, env={})
        assert result.status == "WARN"
        assert "malformed" in result.detail

    def test_warn_on_crate_version_mismatch(self, tmp_path: Path) -> None:
        payload = _fingerprint_payload()
        payload["crate_version"] = "0.0.0-stale"
        target = _write_fingerprint(tmp_path, payload)
        result = check_wheel_freshness(fingerprint_file=target, env={})
        assert result.status == "WARN"
        assert "crate version" in result.detail
        assert "0.0.0-stale" in result.detail
        assert "maturin develop --release" in result.detail

    def test_warn_when_installed_wheel_missing_expected_symbol(self, tmp_path: Path) -> None:
        # The fingerprint expects a symbol the installed .so does not have —
        # exactly what a fresh source tree sees over a stale wheel.
        payload = _fingerprint_payload()
        payload["symbols"] = sorted([*payload["symbols"], "brand_new_kernel_fn"])
        target = _write_fingerprint(tmp_path, payload)
        result = check_wheel_freshness(fingerprint_file=target, env={})
        assert result.status == "WARN"
        assert "missing symbol(s)" in result.detail
        assert "brand_new_kernel_fn" in result.detail

    def test_warn_when_installed_wheel_has_unexpected_symbol(self, tmp_path: Path) -> None:
        # The installed .so exposes MORE than the fingerprint — a stale
        # fingerprint (or a wheel built from a newer tree) is also drift.
        payload = _fingerprint_payload()
        payload["symbols"] = [s for s in payload["symbols"] if s != "seconds_of_day"]
        target = _write_fingerprint(tmp_path, payload)
        result = check_wheel_freshness(fingerprint_file=target, env={})
        assert result.status == "WARN"
        assert "unexpected symbol(s)" in result.detail
        assert "seconds_of_day" in result.detail


# ────────────────────────────────────────────────────────────────────────
# Aggregation — run_doctor / main / report rendering
# ────────────────────────────────────────────────────────────────────────


class TestWheelFreshnessAggregation:
    def test_run_doctor_includes_wheel_stage(self, tmp_path: Path) -> None:
        report = run_doctor(
            audit_dir=tmp_path / "no_audit",
            runtime_file=tmp_path / "no_runtime.json",
            lake_root=tmp_path / "no_lake",
            wheel_fingerprint_file=REPO_FINGERPRINT,
        )
        statuses = {c.name: c.status for c in report.checks}
        assert statuses["wheel_freshness"] == "PASS"

    def test_warn_is_not_fatal_for_report_and_main(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # A stale-wheel WARN must never flip the doctor red (non-fatal by
        # design) — exit code stays 0 and the report renders the ⚠️ glyph.
        payload = _fingerprint_payload()
        payload["crate_version"] = "0.0.0-stale"
        target = _write_fingerprint(tmp_path, payload)
        rc = main(
            audit_dir=tmp_path / "no_audit",
            runtime_file=tmp_path / "no_runtime.json",
            lake_root=tmp_path / "no_lake",
            wheel_fingerprint_file=target,
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "⚠️" in out
        assert "wheel_freshness" in out
        assert "OVERALL: ✅ PASS" in out

    def test_report_ok_true_with_warn(self) -> None:
        report = DoctorReport()
        report.add(check_wheel_freshness(env={"BRUTEX_SKIP_WHEEL_FRESHNESS": "1"}))
        assert report.ok is True


# ────────────────────────────────────────────────────────────────────────
# brutex_identity twin binding (DEC-RUST-RUNID-STORES-001)
# ────────────────────────────────────────────────────────────────────────


class TestBrutexIdentityTwinBinding:
    """The brutex_identity wrapper rides the SAME common helper (COMMON rule).

    Added at the lane's independent refuter round 2: the third hard-imported
    wheel (``brutex.strategies.combo`` / ``brutex.engine`` /
    ``brutex.data.window_ledger_store`` → ``brutex_identity``) had no doctor
    stage while the ``_WheelSpec`` docstring claimed "every PyO3 wheel".
    """

    def test_env_override_skips(self) -> None:
        from brutex.doctor import check_brutex_identity_wheel_freshness

        result = check_brutex_identity_wheel_freshness(env={"BRUTEX_SKIP_WHEEL_FRESHNESS": "1"})
        assert result.name == "wheel_freshness_brutex_identity"
        assert result.status == "SKIP"

    def test_live_pass_via_default_fingerprint(self) -> None:
        # The installed wheel vs the repo EXPECTED_BUILD.json (which IS the
        # shipped fingerprint under `maturin develop`): PASS on a fresh
        # build; a WARN here means a stale local wheel — rebuild via
        # `maturin develop --release` in rust/brutex-identity. Version or
        # symbol drift in a fresh build is a REAL regression (the doctor
        # stage's whole point), so this asserts PASS outright.
        from brutex.doctor import check_brutex_identity_wheel_freshness

        result = check_brutex_identity_wheel_freshness(env={})
        assert result.name == "wheel_freshness_brutex_identity"
        assert result.status == "PASS", result.detail
        assert "brutex_identity" in result.detail

    def test_warn_when_not_importable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from brutex.doctor import check_brutex_identity_wheel_freshness

        monkeypatch.setitem(sys.modules, "brutex_identity._brutex_identity", None)
        result = check_brutex_identity_wheel_freshness(env={})
        assert result.status == "WARN"
        assert "brutex_identity wheel not importable" in result.detail
        assert "rust/brutex-identity" in result.detail

    def test_run_doctor_includes_both_identity_stages(self, tmp_path: Path) -> None:
        report = run_doctor(
            audit_file=None,
            audit_dir=tmp_path / "no_audit",
            runtime_file=tmp_path / "no_runtime.json",
            lake_root=tmp_path / "no_lake",
            wheel_fingerprint_file=None,
            fno_math_wheel_fingerprint_file=None,
            brutex_identity_wheel_fingerprint_file=None,
        )
        names = [c.name for c in report.checks]
        assert "wheel_freshness_brutex_identity" in names
        assert "wheel_source_digest_brutex_identity" in names
