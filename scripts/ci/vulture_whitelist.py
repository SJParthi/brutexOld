# ruff: noqa: B018, F821
"""Vulture whitelist — suppress only verified false positives.

Per DEC-P0-001 (2026-05-17): the CI vulture job is no longer advisory.
This file lists members that vulture cannot prove are used because the
usage pattern is framework-driven or test-fixture-driven. Vulture reads
this file like normal Python and treats every attribute access as a
"used" mark. See https://github.com/jendrikseipp/vulture#whitelists

**Honesty discipline (per Golden Rule #1):** every entry below MUST
reference a symbol that ACTUALLY EXISTS in the codebase today. Do not
add entries for "planned" or "roadmap" symbols — those are dead code
until they exist. The 7-agent review of DEC-P0-001 caught 11 invented
whitelist entries in the first draft; this version contains only
verified-extant symbols.

Add an entry ONLY when:
- The symbol IS used but vulture can't see it (context manager protocol,
  dataclass __post_init__, pytest fixtures via dependency injection)
- AND the symbol exists in the codebase today

NEVER add an entry to silence genuinely dead code. NEVER add entries
for roadmapped APIs that don't exist yet.
"""

# Context-manager protocol parameters (vulture flags unused dunder-method args).
_.__enter__
_.__exit__
_.exc_type
_.exc_val
_.exc_tb

# Dataclass __post_init__ is called by the dataclass machinery, not by
# direct reference. Verified used in several brutex dataclasses and
# test fixtures.
_.__post_init__

# Audit chain integrity verifier — currently invoked by tests/property/
# test_audit_chain_properties.py (not yet by a CLI). Function exists at
# brutex/orchestrator/audit.py:466.
_.verify_chain
