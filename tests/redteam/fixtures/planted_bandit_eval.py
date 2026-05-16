"""Planted-bug fixture: eval(user_input) for bandit B307.

This file intentionally contains a violation. Excluded from the
global bandit scan via pyproject.toml [tool.bandit].

The verifier `test_redteam_bandit.py::test_bandit_catches_eval_user_input`
runs `bandit` directly against this file and asserts non-zero exit
and B307 in the output.

If bandit says "No issues identified" here, the security gate is broken.
"""


def dangerous_eval(user_input: str) -> object:
    # nosec intentionally absent — bandit must flag this
    return eval(user_input)
