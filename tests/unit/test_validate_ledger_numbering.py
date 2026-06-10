"""Unit tests for the ledger-numbering gate (DEC-LEDGER-RENUMBER-001).

The gate makes MIGRATION.md entry NUMBERS trustworthy again after the
parallel-session collision era (29 duplicated numbers measured on main,
2026-06-10): (number, suffix) pairs unique, no bare/suffixed mixing per
number, DEC-ID heads at most one entry. Fail-closed with an explicit
operator override env (ADR required).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "ci" / "validate_ledger_numbering.py"


def _load() -> ModuleType:
    """Import the gate script as a module (scripts/ are not a package)."""
    spec = importlib.util.spec_from_file_location("validate_ledger_numbering", _SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["validate_ledger_numbering"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(name="gate")
def _gate() -> ModuleType:
    """The loaded gate module."""
    return _load()


def _ledger(*headers: str) -> str:
    """Build a minimal ledger text from entry-header lines."""
    body = "\n\nfiller prose that is not a header\n\n".join(headers)
    return f"# ledger\n\n{body}\n"


class TestScan:
    """The pure scanner: violations for exactly the outlawed shapes."""

    def test_clean_ledger_passes(self, gate: ModuleType) -> None:
        violations, entries = gate.scan(
            _ledger(
                "611. **DEC-A-001 — first**",
                "612. **DEC-B-001 — second**",
                "613a. **DEC-C-001 — disambiguated**",
                "613b. **DEC-D-001 — disambiguated sibling**",
                "614. **DEC-E-001 — next**",
            )
        )
        assert violations == []
        assert len(entries) == 5

    def test_duplicate_bare_number_flagged(self, gate: ModuleType) -> None:
        violations, _ = gate.scan(_ledger("613. **DEC-A-001 — one**", "613. **DEC-B-001 — two**"))
        assert len(violations) == 1
        assert "613 is claimed 2x" in violations[0]
        assert "DEC-A-001" in violations[0] and "DEC-B-001" in violations[0]

    def test_duplicate_suffixed_pair_flagged(self, gate: ModuleType) -> None:
        violations, _ = gate.scan(_ledger("613a. **DEC-A-001 — one**", "613a. **DEC-B-001 — two**"))
        assert len(violations) == 1
        assert "613a is claimed 2x" in violations[0]

    def test_bare_and_suffixed_mix_flagged(self, gate: ModuleType) -> None:
        violations, _ = gate.scan(
            _ledger("613. **DEC-A-001 — bare**", "613a. **DEC-B-001 — suffixed**")
        )
        assert any("BOTH bare and suffixed" in v for v in violations)

    def test_repeated_dec_id_is_the_legitimate_slice_pattern(self, gate: ModuleType) -> None:
        # The house multi-slice/amend pattern: one DEC-ID heads several
        # entries (foundation/wiring/UI). NUMBERS must be unique; the
        # DEC-ID reuse is deliberate and must NOT be flagged.
        violations, _ = gate.scan(
            _ledger("214. **DEC-SAME-001 — foundation**", "215. **DEC-SAME-001 — wiring slice**")
        )
        assert violations == []

    def test_non_header_lines_ignored(self, gate: ModuleType) -> None:
        text = _ledger("611. **DEC-A-001 — entry**") + (
            "\nprose mentioning 611. **DEC-B-001 mid-line is not a header\n"
            "  612. **DEC-C-001 indented is not a header\n"
        )
        violations, entries = gate.scan(text)
        assert violations == []
        assert len(entries) == 1


class TestCli:
    """The CLI contract: exit codes + override env."""

    def test_clean_file_exits_zero(self, gate: ModuleType, tmp_path: Path) -> None:
        p = tmp_path / "MIGRATION.md"
        p.write_text(_ledger("611. **DEC-A-001 — ok**"), encoding="utf-8")
        assert gate.main(["prog", str(p)]) == 0

    def test_duplicate_exits_one(self, gate: ModuleType, tmp_path: Path) -> None:
        p = tmp_path / "MIGRATION.md"
        p.write_text(
            _ledger("613. **DEC-A-001 — one**", "613. **DEC-B-001 — two**"),
            encoding="utf-8",
        )
        assert gate.main(["prog", str(p)]) == 1

    def test_override_env_downgrades_to_warn(
        self, gate: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        p = tmp_path / "MIGRATION.md"
        p.write_text(
            _ledger("613. **DEC-A-001 — one**", "613. **DEC-B-001 — two**"),
            encoding="utf-8",
        )
        monkeypatch.setenv("LEDGER_NUMBERING_ALLOW_DUPES", "1")
        assert gate.main(["prog", str(p)]) == 0

    def test_missing_file_exits_two(self, gate: ModuleType, tmp_path: Path) -> None:
        assert gate.main(["prog", str(tmp_path / "absent.md")]) == 2


class TestSiblingValidatorSuffixContract:
    """The suffix scheme must stay legible to the SIBLING DEC-ID validator.

    Regression: the first reconcile run suffixed entry #214 to 214a/214b and
    the PR-body DEC-ID gate (scripts/ci/validate_dec_ids.py) stopped
    resolving DEC-DEC-ID-VALIDATOR-001 — its header regex predated the
    suffix form. Caught live by #580's own CI (DEC-LEDGER-RENUMBER-001).
    """

    def test_dec_id_validator_resolves_suffixed_headers(self) -> None:
        spec = importlib.util.spec_from_file_location(
            "validate_dec_ids", _SCRIPT.parent / "validate_dec_ids.py"
        )
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        sys.modules["validate_dec_ids"] = mod
        spec.loader.exec_module(mod)

        ledger = "214a. **DEC-SUFFIXED-001 — first**\n615. **DEC-BARE-001 — second**\n"
        known = mod.load_known_dec_ids(ledger)
        assert known == {"DEC-SUFFIXED-001", "DEC-BARE-001"}
