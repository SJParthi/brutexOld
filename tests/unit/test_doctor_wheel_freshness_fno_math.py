"""fno_math wheel-source freshness handshake tests (DEC-FNOMATH-WHEEL-FRESHNESS-001).

The fno_math twin of ``test_doctor_wheel_freshness.py`` (trading_core,
R2-CC-G1 / MIGRATION.md #1166 Task 4) — made urgent by
DEC-SWEEP-RUST-TRADESIM-WIRE-001 (#1182), which wired ``ResidentSweepSim``
UNFLAGGED into the sweep hot path: a stale installed ``_fno_math`` wheel is
an always-on production risk. Covers BOTH halves of the handshake:

* the ``fno_math`` shim's import-time compiled-surface assertion
  (``_assert_compiled_surface``) — the actionable ImportError on a stale
  ``.so`` missing a re-exported symbol (e.g. a wheel that predates
  ``ResidentSweepSim``);
* ``brutex.doctor.check_fno_math_wheel_freshness`` — the doctor stage
  (bound to the SAME common ``_check_one_wheel_freshness`` helper the
  trading_core stage uses) comparing the INSTALLED wheel (symbol surface +
  ``__build_crate_version__``) against the in-repo ``EXPECTED_BUILD.json``
  fingerprint: WARN (non-fatal) on mismatch, PASS on match, SKIP via
  ``BRUTEX_SKIP_WHEEL_FRESHNESS=1`` (one escape hatch shared by both
  wheels, deliberately).

HONEST residual gap (Golden Rule #10, pinned in the source too): version +
symbol-set checks catch a stale wheel that dropped/added a symbol or changed
crate version; they do NOT catch same-version symbol-compatible BEHAVIORAL
drift — that class is covered by the SHIPPED source-digest handshake
(DEC-RUST-SOURCE-DIGEST-TWIN-001, MIGRATION.md #1258 — the follow-up
previously named here) plus the LAYERED CI correctness nets: the in-crate
cargo bit-level tests at the Rust-source level and the Python-side
parity/golden suites, whose ULP-boundary discriminators reach WHEEL-level
rounding-order drift too (refuter round 5, MIGRATION.md #1728 — value-level
parity cases alone were proven blind to a 1-ULP fold-op-order mutant wheel).
"""

# Whitebox test of the shim's private handshake internals — same convention as
# the repo's other whitebox files (pyproject [tool.pyright] reportPrivateUsage).
# pyright: reportPrivateUsage=none

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from typing import Any

import fno_math
import pytest
from brutex.doctor import (
    DoctorReport,
    check_fno_math_wheel_freshness,
    main,
    run_doctor,
)

# ONE import style only for fno_math (CodeQL py/import-and-import-from): the
# private handshake internals are read off the module object; the compiled
# submodule is loaded dynamically (its attribute is not declared in the
# package's ``__init__.pyi`` stub). Local aliases keep the test bodies
# parallel to the trading_core suite.
_EXPECTED_COMPILED_SYMBOLS = fno_math._EXPECTED_COMPILED_SYMBOLS
_assert_compiled_surface = fno_math._assert_compiled_surface
_fno_math = importlib.import_module("fno_math._fno_math")

REPO_FINGERPRINT = (
    Path(__file__).resolve().parents[2]
    / "rust"
    / "fno-math"
    / "python"
    / "fno_math"
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
        """Stand-in for a stale ``_fno_math`` .so."""

    fake = FakeCompiled()
    for name in _EXPECTED_COMPILED_SYMBOLS:
        if name != missing:
            setattr(fake, name, getattr(_fno_math, name))
    return fake


# ────────────────────────────────────────────────────────────────────────
# Shim import-time assertion
# ────────────────────────────────────────────────────────────────────────


class TestShimCompiledSurfaceAssertion:
    def test_real_compiled_module_passes(self) -> None:
        # The freshly-built wheel exposes every re-exported symbol.
        _assert_compiled_surface(_fno_math)

    def test_missing_symbol_raises_actionable_import_error(self) -> None:
        # The exact stale-wheel failure this container hit: an installed .so
        # predating the trade-sim kernel — missing ResidentSweepSim.
        fake = _fake_stale_module("ResidentSweepSim")
        with pytest.raises(ImportError, match="ResidentSweepSim"):
            _assert_compiled_surface(fake)

    def test_missing_u128_symbol_raises_actionable_import_error(self) -> None:
        # DEC-TRADE-SIM-U128-KERNEL-001 handshake pin (the LOUD stale-wheel
        # mode): a new-shim + old-.so posture — an installed wheel predating
        # the u128 kernel — fails AT IMPORT naming ResidentSweepSimU128, so
        # the kernel commit is not cherry-pickable without a rebuild.
        fake = _fake_stale_module("ResidentSweepSimU128")
        with pytest.raises(ImportError, match="ResidentSweepSimU128"):
            _assert_compiled_surface(fake)

    def test_error_message_names_maturin_rebuild(self) -> None:
        fake = _fake_stale_module("blake3_hash")
        with pytest.raises(ImportError) as excinfo:
            _assert_compiled_surface(fake)
        message = str(excinfo.value)
        assert "blake3_hash" in message
        assert "STALE" in message
        assert "maturin develop --release" in message
        assert "rust/fno-math" in message

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
        # Stamped on the package, derived from the compiled module's
        # CARGO_PKG_VERSION-baked __version__ — never hardcoded.
        assert fno_math.__build_crate_version__ == _fno_math.__version__

    def test_build_stamp_not_in_all_so_facade_surface_stays_frozen(self) -> None:
        # Deliberately NOT in __all__: brutex.math rebuilds the merged facade's
        # frozen public surface from each kernel's __all__, so listing the
        # kernel-specific build stamp there would leak it into (and break)
        # tests/golden/test_math_facade_golden.py. Attribute access is how the
        # doctor consumer reads it.
        assert "__build_crate_version__" not in fno_math.__all__
        assert isinstance(fno_math.__build_crate_version__, str)

    def test_matches_fingerprint_file_for_fresh_wheel(self) -> None:
        payload = _fingerprint_payload()
        assert fno_math.__build_crate_version__ == payload["crate_version"]

    def test_fingerprint_symbols_pin_the_shim_list(self) -> None:
        # The JSON fingerprint and the in-code expected-symbol list are the
        # SAME contract — pinned equal so they cannot silently drift.
        payload = _fingerprint_payload()
        assert payload["symbols"] == sorted(_EXPECTED_COMPILED_SYMBOLS)

    def test_fingerprint_symbols_match_installed_wheel(self) -> None:
        payload = _fingerprint_payload()
        installed = {a for a in dir(_fno_math) if not a.startswith("_") or a == "__version__"}
        assert set(payload["symbols"]) == installed


# ────────────────────────────────────────────────────────────────────────
# Doctor stage — check_fno_math_wheel_freshness
# ────────────────────────────────────────────────────────────────────────


class TestCheckFnoMathWheelFreshness:
    def test_pass_on_match_via_default_fingerprint(self) -> None:
        # Default resolution: EXPECTED_BUILD.json next to the installed
        # fno_math package (the repo copy under maturin develop).
        result = check_fno_math_wheel_freshness(env={})
        assert result.name == "wheel_freshness_fno_math"
        assert result.status == "PASS"
        assert "fno_math" in result.detail
        assert "matches" in result.detail
        # Honest-envelope wording: PASS never overclaims behavioral freshness.
        assert "behavioral drift not detectable" in result.detail

    def test_pass_on_match_via_explicit_fingerprint(self) -> None:
        result = check_fno_math_wheel_freshness(fingerprint_file=REPO_FINGERPRINT, env={})
        assert result.status == "PASS"

    def test_env_override_skips(self) -> None:
        result = check_fno_math_wheel_freshness(env={"BRUTEX_SKIP_WHEEL_FRESHNESS": "1"})
        assert result.status == "SKIP"
        assert "BRUTEX_SKIP_WHEEL_FRESHNESS=1" in result.detail

    def test_env_override_other_value_does_not_skip(self) -> None:
        result = check_fno_math_wheel_freshness(env={"BRUTEX_SKIP_WHEEL_FRESHNESS": "0"})
        assert result.status == "PASS"

    def test_default_env_is_os_environ(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRUTEX_SKIP_WHEEL_FRESHNESS", "1")
        result = check_fno_math_wheel_freshness()
        assert result.status == "SKIP"

    def test_warn_when_fno_math_not_importable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # None in sys.modules makes importlib.import_module raise ImportError.
        # HONEST reachability (pinned in the docstring too): this WARN arm is
        # reachable only from a programmatic run_doctor/check call like this
        # one — through the `brutex` CLI a broken fno_math import kills the
        # process at `import brutex.cli` (brutex.math imports fno_math at
        # module import) before any doctor stage runs.
        monkeypatch.setitem(sys.modules, "fno_math", None)
        result = check_fno_math_wheel_freshness(env={})
        assert result.status == "WARN"
        assert "fno_math wheel not importable" in result.detail
        assert "maturin develop --release" in result.detail
        assert "rust/fno-math" in result.detail

    def test_skip_when_fingerprint_file_absent(self, tmp_path: Path) -> None:
        result = check_fno_math_wheel_freshness(fingerprint_file=tmp_path / "nope.json", env={})
        assert result.status == "SKIP"
        assert "not checkable" in result.detail

    def test_warn_on_malformed_json(self, tmp_path: Path) -> None:
        target = tmp_path / "EXPECTED_BUILD.json"
        target.write_text("{not valid json", encoding="utf-8")
        result = check_fno_math_wheel_freshness(fingerprint_file=target, env={})
        assert result.status == "WARN"
        assert "malformed" in result.detail

    def test_warn_on_missing_key(self, tmp_path: Path) -> None:
        target = _write_fingerprint(tmp_path, {"symbols": []})
        result = check_fno_math_wheel_freshness(fingerprint_file=target, env={})
        assert result.status == "WARN"
        assert "malformed" in result.detail

    def test_warn_on_wrong_value_types(self, tmp_path: Path) -> None:
        target = _write_fingerprint(tmp_path, {"crate_version": "0.2.0", "symbols": "not-a-list"})
        result = check_fno_math_wheel_freshness(fingerprint_file=target, env={})
        assert result.status == "WARN"
        assert "malformed" in result.detail

    def test_warn_on_crate_version_mismatch(self, tmp_path: Path) -> None:
        payload = _fingerprint_payload()
        payload["crate_version"] = "0.0.0-stale"
        target = _write_fingerprint(tmp_path, payload)
        result = check_fno_math_wheel_freshness(fingerprint_file=target, env={})
        assert result.status == "WARN"
        assert "crate version" in result.detail
        assert "0.0.0-stale" in result.detail
        assert "maturin develop --release" in result.detail
        assert "rust/fno-math" in result.detail

    def test_warn_when_installed_wheel_missing_expected_symbol(self, tmp_path: Path) -> None:
        # The fingerprint expects a symbol the installed .so does not have —
        # exactly what a fresh source tree sees over a stale wheel.
        payload = _fingerprint_payload()
        payload["symbols"] = sorted([*payload["symbols"], "brand_new_kernel_fn"])
        target = _write_fingerprint(tmp_path, payload)
        result = check_fno_math_wheel_freshness(fingerprint_file=target, env={})
        assert result.status == "WARN"
        assert "missing symbol(s)" in result.detail
        assert "brand_new_kernel_fn" in result.detail

    def test_warn_when_installed_wheel_has_unexpected_symbol(self, tmp_path: Path) -> None:
        # The installed .so exposes MORE than the fingerprint — a stale
        # fingerprint (or a wheel built from a newer tree) is also drift.
        payload = _fingerprint_payload()
        payload["symbols"] = [s for s in payload["symbols"] if s != "ResidentSweepSim"]
        target = _write_fingerprint(tmp_path, payload)
        result = check_fno_math_wheel_freshness(fingerprint_file=target, env={})
        assert result.status == "WARN"
        assert "unexpected symbol(s)" in result.detail
        assert "ResidentSweepSim" in result.detail


# ────────────────────────────────────────────────────────────────────────
# Aggregation — run_doctor / main / report rendering
# ────────────────────────────────────────────────────────────────────────


class TestFnoMathWheelFreshnessAggregation:
    def test_run_doctor_includes_both_wheel_stages(self, tmp_path: Path) -> None:
        report = run_doctor(
            audit_dir=tmp_path / "no_audit",
            runtime_file=tmp_path / "no_runtime.json",
            lake_root=tmp_path / "no_lake",
            fno_math_wheel_fingerprint_file=REPO_FINGERPRINT,
        )
        statuses = {c.name: c.status for c in report.checks}
        # Both wheels are watched — the trading_core stage is unchanged and
        # the fno_math stage rides the same common helper.
        assert statuses["wheel_freshness"] == "PASS"
        assert statuses["wheel_freshness_fno_math"] == "PASS"

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
            fno_math_wheel_fingerprint_file=target,
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "⚠️" in out
        assert "wheel_freshness_fno_math" in out
        assert "OVERALL: ✅ PASS" in out

    def test_report_ok_true_with_warn(self) -> None:
        report = DoctorReport()
        report.add(check_fno_math_wheel_freshness(env={"BRUTEX_SKIP_WHEEL_FRESHNESS": "1"}))
        assert report.ok is True
