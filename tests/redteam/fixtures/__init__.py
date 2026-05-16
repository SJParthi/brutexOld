"""Planted-bug fixtures for the red-team harness.

Each module here intentionally contains a known violation. The
verifier tests in `tests/redteam/test_redteam_*.py` run the
corresponding gate tool against these fixtures and assert the
tool catches the violation.

DO NOT lint/type-check this directory. It is intentionally broken.
See pyproject.toml for the exclusion (and `.pre-commit-config.yaml`).
"""
