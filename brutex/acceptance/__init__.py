"""Operator acceptance verifier — ``brutex verify-acceptance``.

DEC-OPERATOR-ACCEPTANCE-VERIFY-001 (MIGRATION.md #1797). One operator-runnable
command that verifies the 7 acceptance classes on the REAL lake with
PASS/FAIL/ABSTAIN(named) verdicts, mandatory evidence blocks, a gating teeth
smoke, and a frozen machine+human report. Compose-never-reimplement, with the
single sanctioned exception of the fenced movers independent leg.
"""

from __future__ import annotations

from brutex.acceptance.model import SCHEMA_VERSION
from brutex.acceptance.run import RunConfig, run_acceptance

__all__ = ["SCHEMA_VERSION", "RunConfig", "run_acceptance"]
