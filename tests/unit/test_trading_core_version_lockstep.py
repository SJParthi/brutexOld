"""Version-lockstep pin for the shared trading-core crate (G-COMBIN-05).

``rust/trading-core/Cargo.toml`` (the crate the Rust tests build) and
``rust/trading-core/pyproject.toml`` (the maturin wheel BruteX installs) carry
INDEPENDENT ``version`` fields with no cross-file sync — a one-sided bump
would ship a wheel whose ``__version__`` (and therefore the run-identity's
``indicator_version`` string, CLAUDE.md §2 Rule 4) disagrees with the crate
the goldens were proven against. This test is the mechanical lockstep pin;
the installed-wheel ↔ source agreement is separately watched by the doctor's
wheel-freshness stage.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

_CRATE = Path(__file__).resolve().parents[2] / "rust" / "trading-core"


def _version_of(name: str) -> str:
    data = tomllib.loads((_CRATE / name).read_text(encoding="utf-8"))
    table = "package" if name == "Cargo.toml" else "project"
    version = data[table]["version"]
    assert isinstance(version, str) and version, f"{name} carries no version string"
    return version


def test_cargo_and_pyproject_versions_are_lockstep() -> None:
    cargo = _version_of("Cargo.toml")
    pyproject = _version_of("pyproject.toml")
    assert cargo == pyproject, (
        f"rust/trading-core version drift: Cargo.toml={cargo!r} vs "
        f"pyproject.toml={pyproject!r} — bump BOTH in the same commit (the crate "
        "version feeds the run-identity indicator_version string)"
    )


def test_version_is_a_plain_semver_triple() -> None:
    # The indicator_version string embeds this verbatim — keep it boring.
    major, minor, patch = _version_of("Cargo.toml").split(".")
    assert all(part.isdigit() for part in (major, minor, patch))
