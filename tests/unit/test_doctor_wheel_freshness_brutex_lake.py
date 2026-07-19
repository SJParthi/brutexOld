"""brutex_lake wheel doctor-stage tests (DEC-BRUTEX-LAKE-BOX-WHEEL-001).

The brutex_lake twin of ``test_doctor_wheel_freshness_fno_math.py`` +
``test_doctor_wheel_source_digest.py``, covering BOTH new stages —
``check_brutex_lake_wheel_freshness`` (``wheel_freshness_brutex_lake``) and
``check_brutex_lake_wheel_source_digest``
(``wheel_source_digest_brutex_lake``) — bound to the SAME common helpers
the trading_core/fno_math stages use (zero new branches in the common
bodies).

The lake wheel is OPTIONAL (every ``brutex/data/*_rust*`` facade degrades
to the Python law when it is absent; nothing imports ``brutex_lake`` at
module top), so — unlike the fno_math twin — this suite must run WITHOUT
the wheel installed too (main CI's required jobs deliberately never build
it; ``.github/workflows/lake-rust-parity.yml`` is the lane that does):

* every arm that needs an importable package is driven through FAKE
  ``sys.modules`` stand-ins (the ``test_doctor_wheel_source_digest.py``
  ``_fake_compiled`` pattern — ``importlib.import_module`` returns the
  ``sys.modules`` entry directly), so the WARN/SKIP/mismatch teeth execute
  identically with or without a real wheel;
* the live-PASS arms against the REAL installed wheel are ``skipif``-gated
  on wheel presence — they execute locally after a box build and in the
  lake-rust-parity lane (which additionally asserts both stages PASS by
  name in its own step), never a false red on a wheel-less required job.

Symbol-pin note (round-2 review fold F2-2): the lake shim deliberately
carries NO ``_EXPECTED_COMPILED_SYMBOLS`` constant (it stays minimal) — the
fingerprint symbols are pinned against ``sorted(brutex_lake.__all__)``,
which equals the 19-symbol ``EXPECTED_BUILD.json`` set ({17 non-dunder
kernel fns} + ``SOURCE_DIGEST`` + ``__version__``).

HONEST envelope (pinned in the source too): the freshness (version+symbol)
check alone is nearly toothless for this crate — the crate version is
frozen at 0.1.0 — so the source-digest stage is THE stale-``.so`` detector;
and a PASS on either stage does not prove polars lockstep (the facades' own
version guard owns that condition).
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest
from brutex.doctor import (
    check_brutex_lake_wheel_freshness,
    check_brutex_lake_wheel_source_digest,
    main,
    run_doctor,
)
from brutex.wheel_digest import crate_source_digest

REPO_ROOT = Path(__file__).resolve().parents[2]
LAKE_CRATE_ROOT = REPO_ROOT / "rust" / "brutex-lake"
SHIM_DIR = LAKE_CRATE_ROOT / "python" / "brutex_lake"
REPO_FINGERPRINT = SHIM_DIR / "EXPECTED_BUILD.json"


def _wheel_installed() -> bool:
    """True when a brutex_lake wheel is importable in this environment."""
    try:
        return importlib.util.find_spec("brutex_lake") is not None
    except (ImportError, ValueError):
        return False


requires_lake_wheel = pytest.mark.skipif(
    not _wheel_installed(),
    reason=(
        "brutex_lake wheel not installed (OPTIONAL kernel — build via "
        "`maturin develop --release` in rust/brutex-lake; the wheel-gated "
        "live arms run in the lake-rust-parity lane and on a built box)"
    ),
)


def _fingerprint_payload() -> dict[str, Any]:
    payload = json.loads(REPO_FINGERPRINT.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _write_fingerprint(tmp_path: Path, payload: object) -> Path:
    target = tmp_path / "EXPECTED_BUILD.json"
    target.write_text(json.dumps(payload), encoding="utf-8")
    return target


def _fake_package(
    monkeypatch: pytest.MonkeyPatch,
    *,
    symbols: list[str] | None = None,
    crate_version: str = "0.1.0",
    source_digest: str | None = None,
) -> None:
    """Install FAKE ``brutex_lake`` + ``brutex_lake._brutex_lake`` modules.

    The stand-in exposes exactly *symbols* (default: the repo fingerprint's
    list) on the compiled module, ``__build_crate_version__`` on the
    package, and points ``__file__`` at the REAL repo shim dir so the
    default fingerprint resolution finds the committed
    ``EXPECTED_BUILD.json`` — every doctor arm is drivable with no wheel
    installed (``importlib.import_module`` returns ``sys.modules`` entries
    directly).
    """
    wanted = list(_fingerprint_payload()["symbols"]) if symbols is None else symbols
    package = types.ModuleType("brutex_lake")
    package.__file__ = str(SHIM_DIR / "__init__.py")
    compiled = types.ModuleType("brutex_lake._brutex_lake")
    for name in wanted:
        if not name.startswith("_"):
            setattr(compiled, name, f"fake {name}")
    # setattr (not direct assignment) for the dunder stamps: ModuleType's
    # class surface does not declare them, and pyright flags direct dunder
    # assignment on it (the test_doctor_wheel_source_digest.py convention).
    setattr(compiled, "__version__", crate_version)  # noqa: B010
    setattr(package, "__build_crate_version__", crate_version)  # noqa: B010
    if source_digest is not None:
        setattr(compiled, "__source_digest__", source_digest)  # noqa: B010
    monkeypatch.setitem(sys.modules, "brutex_lake", package)
    monkeypatch.setitem(sys.modules, "brutex_lake._brutex_lake", compiled)


def _mini_crate(tmp_path: Path) -> Path:
    """A tiny but real crate tree the digest twin can digest."""
    root = tmp_path / "mini-crate"
    (root / "src").mkdir(parents=True)
    (root / "Cargo.toml").write_text('[package]\nname = "mini"\n', encoding="utf-8")
    (root / "src" / "lib.rs").write_text("pub fn mini() {}\n", encoding="utf-8")
    return root


# ────────────────────────────────────────────────────────────────────────
# Fingerprint file contract (no wheel needed)
# ────────────────────────────────────────────────────────────────────────


class TestRepoFingerprint:
    def test_fingerprint_exists_and_is_wellformed(self) -> None:
        payload = _fingerprint_payload()
        assert payload["crate_version"] == "0.1.0"
        symbols = payload["symbols"]
        assert isinstance(symbols, list)
        assert symbols == sorted(symbols), "fingerprint symbols must be sorted"

    def test_fingerprint_carries_the_19_symbol_surface(self) -> None:
        # 17 non-dunder kernel fns + SOURCE_DIGEST + __version__
        # (DEC-BRUTEX-LAKE-BOX-WHEEL-001; __source_digest__ is deliberately
        # NOT listed — dunder-invisible to the doctor's symbol check).
        symbols = _fingerprint_payload()["symbols"]
        assert len(symbols) == 19
        assert "SOURCE_DIGEST" in symbols
        assert "__version__" in symbols
        assert "__source_digest__" not in symbols

    def test_fingerprint_symbols_match_the_shim_all_list_source(self) -> None:
        # F2-2 (no wheel needed): the shim source's __all__ literal is the
        # one in-code surface list (the lake shim deliberately has no
        # _EXPECTED_COMPILED_SYMBOLS constant) — parse it from the committed
        # source so the pin holds even on wheel-less CI jobs; the wheel-gated
        # twin below re-pins against the IMPORTED module.
        shim_source = (SHIM_DIR / "__init__.py").read_text(encoding="utf-8")
        all_block = shim_source.split("__all__ = [", 1)[1].split("]", 1)[0]
        declared = [
            line.strip().strip(",").strip('"')
            for line in all_block.splitlines()
            if line.strip().startswith('"')
        ]
        assert sorted(declared) == _fingerprint_payload()["symbols"]


# ────────────────────────────────────────────────────────────────────────
# Doctor stage — check_brutex_lake_wheel_freshness (fake-module arms)
# ────────────────────────────────────────────────────────────────────────


class TestCheckBrutexLakeWheelFreshness:
    def test_env_override_skips(self) -> None:
        result = check_brutex_lake_wheel_freshness(env={"BRUTEX_SKIP_WHEEL_FRESHNESS": "1"})
        assert result.name == "wheel_freshness_brutex_lake"
        assert result.status == "SKIP"
        assert "BRUTEX_SKIP_WHEEL_FRESHNESS=1" in result.detail

    def test_default_env_is_os_environ(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRUTEX_SKIP_WHEEL_FRESHNESS", "1")
        result = check_brutex_lake_wheel_freshness()
        assert result.status == "SKIP"

    def test_warn_when_wheel_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # THE doctor-daily hermetic-env arm (asserted deterministic WARN in
        # the cron): the OPTIONAL wheel is not installed — advisory WARN
        # naming the rebuild, never a FAIL. None in sys.modules forces the
        # ImportError deterministically whether or not a real wheel exists.
        monkeypatch.setitem(sys.modules, "brutex_lake", None)
        result = check_brutex_lake_wheel_freshness(env={})
        assert result.status == "WARN"
        assert "brutex_lake wheel not importable" in result.detail
        assert "maturin develop --release" in result.detail
        assert "rust/brutex-lake" in result.detail

    def test_pass_on_match_via_default_fingerprint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Default resolution: EXPECTED_BUILD.json next to the (faked)
        # installed package — which IS the repo copy under maturin develop.
        _fake_package(monkeypatch)
        result = check_brutex_lake_wheel_freshness(env={})
        assert result.status == "PASS"
        assert "brutex_lake" in result.detail
        assert "matches" in result.detail
        # Honest-envelope wording: PASS never overclaims behavioral freshness.
        assert "behavioral drift not detectable" in result.detail

    def test_env_override_other_value_does_not_skip(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _fake_package(monkeypatch)
        result = check_brutex_lake_wheel_freshness(env={"BRUTEX_SKIP_WHEEL_FRESHNESS": "0"})
        assert result.status == "PASS"

    def test_skip_when_fingerprint_file_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_package(monkeypatch)
        result = check_brutex_lake_wheel_freshness(fingerprint_file=tmp_path / "nope.json", env={})
        assert result.status == "SKIP"
        assert "not checkable" in result.detail

    def test_warn_on_malformed_json(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _fake_package(monkeypatch)
        target = tmp_path / "EXPECTED_BUILD.json"
        target.write_text("{not valid json", encoding="utf-8")
        result = check_brutex_lake_wheel_freshness(fingerprint_file=target, env={})
        assert result.status == "WARN"
        assert "malformed" in result.detail

    def test_warn_on_crate_version_mismatch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_package(monkeypatch)
        payload = _fingerprint_payload()
        payload["crate_version"] = "0.0.0-stale"
        target = _write_fingerprint(tmp_path, payload)
        result = check_brutex_lake_wheel_freshness(fingerprint_file=target, env={})
        assert result.status == "WARN"
        assert "crate version" in result.detail
        assert "0.0.0-stale" in result.detail
        assert "maturin develop --release" in result.detail
        assert "rust/brutex-lake" in result.detail

    def test_warn_when_installed_wheel_missing_expected_symbol(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The fingerprint expects a symbol the installed .so does not have —
        # exactly what a fresh source tree sees over a stale wheel (e.g. a
        # pre-SOURCE_DIGEST wheel under this very PR's fingerprint).
        _fake_package(monkeypatch)
        payload = _fingerprint_payload()
        payload["symbols"] = sorted([*payload["symbols"], "brand_new_lake_fn"])
        target = _write_fingerprint(tmp_path, payload)
        result = check_brutex_lake_wheel_freshness(fingerprint_file=target, env={})
        assert result.status == "WARN"
        assert "missing symbol(s)" in result.detail
        assert "brand_new_lake_fn" in result.detail

    def test_warn_when_installed_wheel_has_unexpected_symbol(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_package(monkeypatch)
        payload = _fingerprint_payload()
        payload["symbols"] = [s for s in payload["symbols"] if s != "render_parquet_from_ipc"]
        target = _write_fingerprint(tmp_path, payload)
        result = check_brutex_lake_wheel_freshness(fingerprint_file=target, env={})
        assert result.status == "WARN"
        assert "unexpected symbol(s)" in result.detail
        assert "render_parquet_from_ipc" in result.detail


# ────────────────────────────────────────────────────────────────────────
# Doctor stage — check_brutex_lake_wheel_source_digest (fake-module arms)
# ────────────────────────────────────────────────────────────────────────


class TestCheckBrutexLakeWheelSourceDigest:
    def test_env_override_skips(self) -> None:
        result = check_brutex_lake_wheel_source_digest(env={"BRUTEX_SKIP_WHEEL_FRESHNESS": "1"})
        assert result.name == "wheel_source_digest_brutex_lake"
        assert result.status == "SKIP"
        assert "BRUTEX_SKIP_WHEEL_FRESHNESS=1" in result.detail

    def test_default_env_is_os_environ(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRUTEX_SKIP_WHEEL_FRESHNESS", "1")
        result = check_brutex_lake_wheel_source_digest()
        assert result.status == "SKIP"

    def test_warn_when_wheel_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The doctor-daily hermetic-env arm's digest twin. BOTH keys are
        # None-ed: the digest stage imports the compiled submodule directly,
        # and an earlier test in the session may have cached the REAL
        # `brutex_lake._brutex_lake` in sys.modules (importlib returns the
        # cached entry without touching the None-ed parent — a test-order
        # trap caught by the discovery-gate combined run).
        monkeypatch.setitem(sys.modules, "brutex_lake", None)
        monkeypatch.setitem(sys.modules, "brutex_lake._brutex_lake", None)
        result = check_brutex_lake_wheel_source_digest(env={})
        assert result.status == "WARN"
        assert "brutex_lake wheel not importable" in result.detail
        assert "maturin develop --release" in result.detail
        assert "rust/brutex-lake" in result.detail

    def test_warn_when_stamp_absent_older_wheel(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A wheel built BEFORE this decision (the pre-DEC brutex_lake — it
        # stamped NOTHING) has no __source_digest__ → WARN with the rebuild
        # hint, never a crash, never a fake PASS.
        _fake_package(monkeypatch)  # no source_digest kwarg → stamp absent
        result = check_brutex_lake_wheel_source_digest(env={})
        assert result.status == "WARN"
        assert "no usable __source_digest__" in result.detail
        assert "before the source-digest handshake" in result.detail
        assert "maturin develop --release" in result.detail

    def test_skip_when_no_crate_tree(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # A wheel-only install with no repo checkout: honestly not checkable.
        _fake_package(monkeypatch, source_digest="0" * 64)
        result = check_brutex_lake_wheel_source_digest(crate_root=tmp_path / "nope", env={})
        assert result.status == "SKIP"
        assert "not checkable" in result.detail

    def test_warn_on_mismatch_names_both_digests(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # THE bug class this stage exists for (and the crate's ONLY real
        # stale signal at its frozen 0.1.0 version): a wheel whose baked
        # digest is not this checkout's — WARN naming BOTH hex digests.
        root = _mini_crate(tmp_path)
        stale = "0" * 64
        _fake_package(monkeypatch, source_digest=stale)
        result = check_brutex_lake_wheel_source_digest(crate_root=root, env={})
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
        _fake_package(monkeypatch, source_digest=expected)
        result = check_brutex_lake_wheel_source_digest(crate_root=root, env={})
        assert result.status == "PASS"
        assert expected in result.detail

    def test_default_crate_root_is_the_repo_lake_crate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # crate_root=None resolves rust/brutex-lake next to the installed
        # brutex package — under this repo checkout the REAL crate tree, so
        # a fake wheel stamped with the real tree's digest reads PASS.
        expected = crate_source_digest(LAKE_CRATE_ROOT)
        _fake_package(monkeypatch, source_digest=expected)
        result = check_brutex_lake_wheel_source_digest(env={})
        assert result.status == "PASS"
        assert expected in result.detail


# ────────────────────────────────────────────────────────────────────────
# Aggregation — run_doctor / main
# ────────────────────────────────────────────────────────────────────────


class TestBrutexLakeAggregation:
    def test_run_doctor_includes_both_lake_stages(self, tmp_path: Path) -> None:
        # Wheel-agnostic: present → PASS/WARN via the real wheel; absent →
        # WARN. Either way BOTH stages are IN the report and never FAIL —
        # the OPTIONAL-wheel law (an absent lake wheel must not red the
        # doctor; DoctorReport.ok is FAIL-only).
        report = run_doctor(
            audit_dir=tmp_path / "no_audit",
            runtime_file=tmp_path / "no_runtime.json",
            lake_root=tmp_path / "no_lake",
        )
        statuses = {c.name: c.status for c in report.checks}
        assert statuses["wheel_freshness_brutex_lake"] in {"PASS", "WARN"}
        assert statuses["wheel_source_digest_brutex_lake"] in {"PASS", "WARN"}
        assert report.ok is True

    def test_run_doctor_threads_explicit_lake_params(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The two new run_doctor passthrough params reach the lake stages.
        _fake_package(monkeypatch, source_digest="0" * 64)
        report = run_doctor(
            audit_dir=tmp_path / "no_audit",
            runtime_file=tmp_path / "no_runtime.json",
            lake_root=tmp_path / "no_lake",
            brutex_lake_wheel_fingerprint_file=tmp_path / "nope.json",
            brutex_lake_wheel_source_crate_root=tmp_path / "no-crate",
        )
        statuses = {c.name: c.status for c in report.checks}
        assert statuses["wheel_freshness_brutex_lake"] == "SKIP"  # absent file
        assert statuses["wheel_source_digest_brutex_lake"] == "SKIP"  # absent tree

    def test_absent_wheel_warn_is_not_fatal_for_main(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The doctor-daily law end-to-end: an absent OPTIONAL wheel WARNs
        # both lake stages and the doctor still exits 0 (advisory, never
        # fatal) with the ⚠️ rows rendered. Both keys None-ed (the
        # cached-submodule test-order trap — see the digest WARN test).
        monkeypatch.setitem(sys.modules, "brutex_lake", None)
        monkeypatch.setitem(sys.modules, "brutex_lake._brutex_lake", None)
        rc = main(
            audit_dir=tmp_path / "no_audit",
            runtime_file=tmp_path / "no_runtime.json",
            lake_root=tmp_path / "no_lake",
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "wheel_freshness_brutex_lake" in out
        assert "wheel_source_digest_brutex_lake" in out
        assert "OVERALL: ✅ PASS" in out


# ────────────────────────────────────────────────────────────────────────
# Live arms — the REAL installed wheel (wheel-gated; run on a built box +
# in the lake-rust-parity lane, skipped on wheel-less required CI jobs)
# ────────────────────────────────────────────────────────────────────────


@requires_lake_wheel
class TestLiveLakeWheel:
    def test_fingerprint_symbols_pin_the_shim_all_list(self) -> None:
        # F2-2: sorted(brutex_lake.__all__) IS the fingerprint contract (the
        # lake shim deliberately carries no _EXPECTED_COMPILED_SYMBOLS).
        import brutex_lake

        assert _fingerprint_payload()["symbols"] == sorted(brutex_lake.__all__)

    def test_fingerprint_symbols_match_installed_wheel(self) -> None:
        compiled = importlib.import_module("brutex_lake._brutex_lake")
        installed = {a for a in dir(compiled) if not a.startswith("_") or a == "__version__"}
        assert set(_fingerprint_payload()["symbols"]) == installed

    def test_build_crate_version_stamp(self) -> None:
        import brutex_lake

        compiled = importlib.import_module("brutex_lake._brutex_lake")
        assert brutex_lake.__build_crate_version__ == compiled.__version__
        assert "__build_crate_version__" not in brutex_lake.__all__
        assert "__source_digest__" not in brutex_lake.__all__
        assert brutex_lake.__source_digest__ == brutex_lake.SOURCE_DIGEST

    def test_live_freshness_pass_via_default_fingerprint(self) -> None:
        result = check_brutex_lake_wheel_freshness(env={})
        assert result.status == "PASS", result.detail

    def test_live_source_digest_pass_via_default_crate_root(self) -> None:
        # A freshly-built wheel's baked digest equals the twin recompute
        # over this checkout. If the local wheel is stale this is EXACTLY
        # the class the doctor downgrades to WARN — skip with the rebuild
        # command instead of a self-inflicted red (the
        # test_doctor_wheel_source_digest.py local-stale convention).
        compiled = importlib.import_module("brutex_lake._brutex_lake")
        baked = getattr(compiled, "__source_digest__", None)
        if baked != crate_source_digest(LAKE_CRATE_ROOT):
            pytest.skip(
                "installed brutex_lake wheel stale vs this checkout — rebuild "
                "via `maturin develop --release` in rust/brutex-lake"
            )
        result = check_brutex_lake_wheel_source_digest(env={})
        assert result.status == "PASS", result.detail
