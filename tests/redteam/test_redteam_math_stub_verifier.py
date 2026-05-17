"""Red-team tests for scripts/ci/verify-math-stub-vs-wheel.py — the math stub verifier.

Per DEC-P0-019 (PR #41a): the verifier parses `rust/fno-math-py/src/lib.rs`
for `#[pyo3(signature = (...))]` declarations and compares against
`brutex/math/__init__.pyi`. It MUST exit 0 on match, exit 1 on any mismatch,
and handle missing-file edge cases gracefully.

These tests synthesize Rust + stub inputs at runtime via tempdir + monkeypatch
to verify the verifier's contract WITHOUT requiring a maturin install or PyO3
wheel build. The verifier is source-based parsing only.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
VERIFIER = REPO_ROOT / "scripts" / "ci" / "verify-math-stub-vs-wheel.py"


def _run() -> subprocess.CompletedProcess[str]:
    """Run the verifier against the real repo paths."""
    return subprocess.run(
        ["python3", str(VERIFIER)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )


class TestVerifierContract:
    """Behavior of `verify-math-stub-vs-wheel.py`."""

    def test_verifier_script_is_executable(self) -> None:
        assert VERIFIER.exists(), "verifier script must exist"
        assert os.access(VERIFIER, os.X_OK), "verifier must be +x"

    def test_verifier_passes_against_committed_main(self) -> None:
        """Against the currently-committed Rust + stub, the verifier MUST pass."""
        result = _run()
        assert result.returncode == 0, (
            f"verifier failed against committed sources:\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert "all 6 Greek signatures match" in result.stdout

    def test_verifier_lists_all_6_greeks(self) -> None:
        """Output table must include every Greek symbol."""
        result = _run()
        assert result.returncode == 0
        for symbol in (
            "delta",
            "gamma",
            "theta",
            "vega",
            "rho",
            "implied_volatility",
        ):
            assert symbol in result.stdout, f"missing {symbol} in output"

    def test_verifier_does_not_check_indicators(self) -> None:
        """Per DEC-P0-016: 6 indicators are deliberately stub-loose. The
        verifier MUST NOT include them in its table — listing them would
        force a mismatch on every run."""
        result = _run()
        assert result.returncode == 0
        for symbol in ("atr", "bollinger_bands", "ema", "macd", "rsi", "sma"):
            # Indicators may appear ONCE in the docstring or comment, but
            # NOT in a "OK" or "FAIL" status row. We check the status table.
            status_lines = [
                line for line in result.stdout.splitlines() if " OK " in line or "FAIL" in line
            ]
            for line in status_lines:
                assert not line.startswith(symbol), (
                    f"indicator {symbol} should not be in status table; found: {line}"
                )


class TestVerifierFailsClosedOnMissingFiles:
    """Fail-closed contract for missing inputs."""

    def test_rust_lib_missing_fails_closed(self, tmp_path: Path) -> None:
        """If `rust/fno-math-py/src/lib.rs` is missing, verifier must exit 1."""
        # Use a tempdir with no Rust source. The verifier resolves paths
        # relative to its own location (parents[2]) so we need to copy
        # the script to a temp location with a different repo layout.
        fake_repo = tmp_path / "fake_repo"
        fake_repo.mkdir()
        fake_scripts = fake_repo / "scripts" / "ci"
        fake_scripts.mkdir(parents=True)
        fake_verifier = fake_scripts / "verify-math-stub-vs-wheel.py"
        fake_verifier.write_text(VERIFIER.read_text())
        # NO rust/ or brutex/ in fake_repo → verifier must FAIL.
        result = subprocess.run(
            ["python3", str(fake_verifier)],
            cwd=str(fake_repo),
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 1
        assert "missing" in (result.stderr.lower() + result.stdout.lower())


class TestVerifierDetectsDrift:
    """Synthetic drift scenarios via tempdir fakery."""

    def _stage(self, tmp_path: Path, rust_src: str, pyi_src: str) -> Path:
        fake_repo = tmp_path / "drift_repo"
        (fake_repo / "rust" / "fno-math-py" / "src").mkdir(parents=True)
        (fake_repo / "brutex" / "math").mkdir(parents=True)
        (fake_repo / "scripts" / "ci").mkdir(parents=True)
        (fake_repo / "rust" / "fno-math-py" / "src" / "lib.rs").write_text(rust_src)
        (fake_repo / "brutex" / "math" / "__init__.pyi").write_text(pyi_src)
        target = fake_repo / "scripts" / "ci" / "verify-math-stub-vs-wheel.py"
        target.write_text(VERIFIER.read_text())
        return target

    def test_matching_signatures_pass(self, tmp_path: Path) -> None:
        verifier = self._stage(
            tmp_path,
            rust_src=(
                "#[pyfunction]\n"
                '#[pyo3(name = "delta")]\n'
                "#[pyo3(signature = (side, spot, strike, time, rate, div, sigma))]\n"
                "fn py_delta() {}\n"
            ),
            pyi_src=(
                "def delta(side: str, spot: float, strike: float, time: float,\n"
                "          rate: float, div: float, sigma: float) -> float: ...\n"
            ),
        )
        result = subprocess.run(
            ["python3", str(verifier)],
            cwd=str(verifier.parents[2]),
            capture_output=True,
            text=True,
            check=False,
        )
        # 5 other Greeks missing from the synthetic Rust source → FAIL.
        # delta itself should be OK in the table.
        assert "delta" in result.stdout
        assert "OK" in result.stdout

    def test_mismatched_param_names_fail(self, tmp_path: Path) -> None:
        verifier = self._stage(
            tmp_path,
            rust_src=(
                "#[pyfunction]\n"
                '#[pyo3(name = "delta")]\n'
                "#[pyo3(signature = (side, RENAMED_spot, strike, time, rate, div, sigma))]\n"
                "fn py_delta() {}\n"
            ),
            pyi_src=(
                "def delta(side: str, spot: float, strike: float, time: float,\n"
                "          rate: float, div: float, sigma: float) -> float: ...\n"
            ),
        )
        result = subprocess.run(
            ["python3", str(verifier)],
            cwd=str(verifier.parents[2]),
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 1
        assert "FAIL" in result.stdout
        # The mismatch list should mention the renamed param.
        assert "RENAMED_spot" in (result.stdout + result.stderr)


@pytest.mark.skipif(
    not (REPO_ROOT / "rust" / "fno-math-py" / "src" / "lib.rs").exists(),
    reason="real Rust source missing — sanity-only test",
)
def test_committed_rust_has_all_6_greeks() -> None:
    """Sanity test that the committed Rust source actually defines all 6 Greeks.

    If this fails, the .pyi tightening from PR #38 is out of sync with reality.
    """
    text = (REPO_ROOT / "rust" / "fno-math-py" / "src" / "lib.rs").read_text()
    for sym in ("delta", "gamma", "theta", "vega", "rho", "implied_volatility"):
        # Match `#[pyo3(name = "X")]`.
        assert f'name = "{sym}"' in text, f"missing pyo3 name = {sym!r} in Rust source"
