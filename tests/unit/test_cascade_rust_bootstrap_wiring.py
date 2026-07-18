"""Mechanical wiring pins: the brutex-cascade wheel is built by the documented
setup paths (DEC-RUST-CASCADE-RESULTS-001, hostile-review round-2 MAJOR).

The crate is DEFAULT-ON hard-require with the converted Python law DELETED, so
an environment set up via the documented ``scripts/setup/bootstrap.sh`` /
``make run`` path MUST end up with the wheel installed — otherwise /results and
the results-depth pass raise ``CascadeRustError`` on a fresh clone while CI
stays green (the exact "green in CI, broken on the operator's box" gap the
round-2 review confirmed). These grep-style pins follow the house
workflow-wiring idiom (the ``TestMutationWorkflowWiring`` pattern): each
assertion FAILS with the bootstrap/Makefile fix reverted, so the wiring can
never silently regress.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BOOTSTRAP = REPO / "scripts" / "setup" / "bootstrap.sh"
MAKEFILE = REPO / "Makefile"
FACADE = REPO / "brutex" / "engine" / "cascade_rust.py"


class TestBootstrapWiring:
    def test_bootstrap_builds_the_cascade_wheel(self) -> None:
        text = BOOTSTRAP.read_text(encoding="utf-8")
        assert "rust/brutex-cascade" in text, (
            "bootstrap.sh no longer builds rust/brutex-cascade — the crate is "
            "default-ON hard-require (DEC-RUST-CASCADE-RESULTS-001); a fresh "
            "clone via the documented path would break /results."
        )
        assert 'python -c "import brutex_cascade"' in text, (
            "bootstrap.sh must verify the wheel imports after building it "
            "(the fno_math/trading_core precedent shape)."
        )

    def test_bootstrap_cascade_block_is_required_not_optional(self) -> None:
        """The block must ABORT on failure (exit 1) like fno_math/trading_core —
        never the optional brutex_feed warn shape (the law is deleted; there is
        no Python degrade to fall back to)."""
        text = BOOTSTRAP.read_text(encoding="utf-8")
        match = re.search(r"brutex_cascade Rust extension.*?(?=# ─+ 6c\.)", text, flags=re.DOTALL)
        assert match is not None, "the brutex_cascade bootstrap block is missing"
        block = match.group(0)
        assert "exit 1" in block, (
            "the brutex_cascade bootstrap block must exit 1 on a failed build "
            "(REQUIRED shape) — a warn-and-continue would ship a broken /results."
        )
        assert "_warn" not in block, (
            "the brutex_cascade bootstrap block must not use the optional "
            "_warn shape (that is the default-OFF brutex_feed convention)."
        )


class TestMakefileWiring:
    def test_make_run_builds_the_cascade_wheel(self) -> None:
        text = MAKEFILE.read_text(encoding="utf-8")
        assert "rust/brutex-cascade" in text, (
            "Makefile run-raw no longer builds rust/brutex-cascade — the crate "
            "is default-ON hard-require (DEC-RUST-CASCADE-RESULTS-001)."
        )
        assert 'import brutex_cascade" 2>/dev/null ||' in text, (
            "Makefile must gate the build on the import probe (the "
            "fno_math/trading_core precedent shape)."
        )

    def test_makefile_cascade_block_has_no_optional_fallback(self) -> None:
        text = MAKEFILE.read_text(encoding="utf-8")
        start = text.index("import brutex_cascade")
        block = text[start : text.index("$(VPY) -m brutex run", start)]
        assert "|| echo" not in block, (
            "the brutex_cascade Makefile block must not carry the optional "
            "'|| echo' fallback (that is the default-OFF brutex_feed shape) — "
            "a failed build must fail `make run` loudly."
        )


class TestRemediationTruth:
    def test_facade_remediation_names_a_path_that_actually_builds_it(self) -> None:
        """The CascadeRustError remediation string names bootstrap.sh — that
        claim must stay TRUE (round-2 MAJOR: the string was false before the
        bootstrap wiring landed)."""
        facade = FACADE.read_text(encoding="utf-8")
        assert "scripts/setup/bootstrap.sh" in facade
        bootstrap = BOOTSTRAP.read_text(encoding="utf-8")
        assert "rust/brutex-cascade" in bootstrap, (
            "cascade_rust.py tells the operator to run bootstrap.sh, but "
            "bootstrap.sh does not build the wheel — the remediation is a lie."
        )
