"""Unit tests — the ``run_id`` correlation twin in :mod:`brutex.observability.correlation`.

DEC-SWEEP-UNKILLABLE-CORE-001 (R43): ``run_id_scope`` is the sweep twin of
``pull_id_scope`` — same bind/yield/restore contract, pinned side-by-side
against the pull twin's observable behaviour — with ONE deliberate divergence:
no mint-on-``None`` path (a sweep's run_id IS the deterministic grid identity,
D-1; a minted id would fabricate an identity — Golden Rule #1).

Sibling of ``tests/unit/test_observability_correlation.py`` (which owns the
pre-existing surface); WP-6 adds this file to the correlation ci.yml gate line
(see wp-integration-notes.md).
"""

from __future__ import annotations

import structlog
from brutex.observability.correlation import (
    RUN_ID_KEY,
    current_pull_id,
    current_run_id,
    pull_id_scope,
    run_id_scope,
)

#: D-6 traceability — test name → requirement/gap tokens.
TRACEABILITY: dict[str, tuple[str, ...]] = {
    "test_r43_run_id_key_value": ("R43",),
    "test_r43_scope_binds_yields_and_unbinds": ("R43",),
    "test_r43_scope_restores_prior_value_nesting_safe": ("R43",),
    "test_r43_outside_any_scope_reads_empty": ("R43",),
    "test_r43_twin_behaviour_matches_pull_id_scope": ("R43",),
    "test_r43_run_and_pull_ids_coexist": ("R43",),
}

#: A deterministic 64-hex grid-identity digest (the D-1 shape).
RUN_ID = "c3" * 32


def test_r43_run_id_key_value() -> None:
    """The contextvar key is exactly ``run_id`` (the grep key every event carries)."""
    assert RUN_ID_KEY == "run_id"


def test_r43_scope_binds_yields_and_unbinds() -> None:
    assert current_run_id() == ""
    with run_id_scope(RUN_ID) as bound:
        assert bound == RUN_ID  # yielded VERBATIM — never re-minted (D-1)
        assert current_run_id() == RUN_ID
        # The bound id rides the structlog contextvars, so merge_contextvars
        # stamps it on every downstream event.
        assert structlog.contextvars.get_contextvars()[RUN_ID_KEY] == RUN_ID
    assert current_run_id() == ""


def test_r43_scope_restores_prior_value_nesting_safe() -> None:
    with run_id_scope("a" * 64):
        with run_id_scope("b" * 64):
            assert current_run_id() == "b" * 64
        assert current_run_id() == "a" * 64
    assert current_run_id() == ""


def test_r43_outside_any_scope_reads_empty() -> None:
    """Outside any scope the read is a benign '' — never a raise (the pull twin's law)."""
    assert current_run_id() == ""


def test_r43_twin_behaviour_matches_pull_id_scope() -> None:
    """Side-by-side twin pin: explicit-id bind/yield/restore matches the pull scope."""
    token = "d4" * 6
    with pull_id_scope(token) as pull_bound, run_id_scope(token) as run_bound:
        assert pull_bound == run_bound == token
        assert current_pull_id() == current_run_id() == token
    assert current_pull_id() == ""
    assert current_run_id() == ""


def test_r43_run_and_pull_ids_coexist() -> None:
    """Distinct keys — one thread can carry a pull id AND a run id at once."""
    with pull_id_scope("e5" * 6), run_id_scope(RUN_ID):
        assert current_pull_id() == "e5" * 6
        assert current_run_id() == RUN_ID
        bound = structlog.contextvars.get_contextvars()
        assert bound["pull_id"] == "e5" * 6
        assert bound[RUN_ID_KEY] == RUN_ID
