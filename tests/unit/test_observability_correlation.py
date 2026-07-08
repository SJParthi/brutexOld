"""Tests for `brutex.observability.correlation` — the pull_id correlation id.

100% line+branch: id minting (shape + uniqueness), the bound / unbound reads,
the explicit-vs-generated scope branches, nesting restore, and the
capture_logs contract (a scoped ``current_pull_id`` is assertable as an
explicit field even though capture_logs strips the contextvars processor).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
import structlog
import structlog.contextvars as cv
from brutex.observability.correlation import (
    PULL_ID_KEY,
    REQUEST_ID_KEY,
    current_pull_id,
    current_request_id,
    inherited_pull_id_scope,
    new_pull_id,
    new_request_id,
    pull_id_scope,
    request_id_scope,
)
from structlog.testing import capture_logs


@pytest.fixture(autouse=True)
def _clean_contextvars() -> Iterator[None]:
    """No pull_id may leak between tests (contextvars are context-local)."""
    cv.clear_contextvars()
    yield
    cv.clear_contextvars()


def test_pull_id_key_is_pull_id() -> None:
    assert PULL_ID_KEY == "pull_id"


def test_new_pull_id_is_twelve_hex_chars() -> None:
    pid = new_pull_id()
    assert len(pid) == 12
    assert all(c in "0123456789abcdef" for c in pid)


def test_new_pull_id_is_unique() -> None:
    assert len({new_pull_id() for _ in range(1000)}) == 1000


def test_current_pull_id_is_empty_when_unbound() -> None:
    assert current_pull_id() == ""


def test_scope_with_explicit_id_binds_and_yields_it() -> None:
    with pull_id_scope("deadbeef0001") as pid:
        assert pid == "deadbeef0001"
        assert current_pull_id() == "deadbeef0001"


def test_scope_with_none_generates_a_fresh_id() -> None:
    with pull_id_scope() as pid:
        # Generated branch: a real 12-hex id is minted + bound.
        assert len(pid) == 12
        assert current_pull_id() == pid


def test_scope_unbinds_on_exit() -> None:
    with pull_id_scope("abc123abc123"):
        assert current_pull_id() == "abc123abc123"
    # Restored to unbound — no leak past the pull.
    assert current_pull_id() == ""


def test_scope_nesting_restores_outer_id() -> None:
    with pull_id_scope("outer0000000"):
        with pull_id_scope("inner1111111"):
            assert current_pull_id() == "inner1111111"
        # Inner scope restores the OUTER id, not "unbound".
        assert current_pull_id() == "outer0000000"


def test_scoped_id_is_assertable_via_capture_logs() -> None:
    """A scoped pull_id, re-stamped explicitly, survives capture_logs.

    capture_logs strips the contextvars merge processor, so an event must carry
    pull_id as an EXPLICIT field (read back via current_pull_id) to be assertable
    — this pins the test-oracle contract the ingest spine relies on.
    """
    with capture_logs() as logs, pull_id_scope("feedface0000"):
        structlog.get_logger("t").info("ingest.window", pull_id=current_pull_id())
    assert logs[0]["pull_id"] == "feedface0000"


# ── inherited_pull_id_scope (DEC-PULL-BEAT-PERRUN-001) ────────────────────────


def test_inherited_scope_mints_when_unbound() -> None:
    """No ambient id → mint (byte-identical to pull_id_scope for standalone callers)."""
    with inherited_pull_id_scope() as pid:
        assert len(pid) == 12
        assert current_pull_id() == pid
    assert current_pull_id() == ""


def test_inherited_scope_inherits_a_bound_ambient_id() -> None:
    """A non-empty ambient id is INHERITED (the job-level run scope reaches inner
    entrypoint scopes instead of being shadow-minted over)."""
    with pull_id_scope("cafe00000001"):
        with inherited_pull_id_scope() as pid:
            assert pid == "cafe00000001"
            assert current_pull_id() == "cafe00000001"
        assert current_pull_id() == "cafe00000001"  # nesting-safe restore


def test_inherited_scope_explicit_id_wins_over_ambient() -> None:
    with pull_id_scope("cafe00000001"):
        with inherited_pull_id_scope("beef00000002") as pid:
            assert pid == "beef00000002"
            assert current_pull_id() == "beef00000002"
        assert current_pull_id() == "cafe00000001"


def test_inherited_scope_explicit_empty_binds_empty() -> None:
    """The documented ``pull_id_scope("")``-binds-empty trap is mirrored, not fixed:
    an explicit "" wins (pass None, not "", to mean 'no explicit id')."""
    with pull_id_scope("cafe00000001"), inherited_pull_id_scope("") as pid:
        assert pid == ""
        assert current_pull_id() == ""


def test_inherited_scope_nested_restore() -> None:
    """Exit restores the OUTER binding at every nesting depth (no leak)."""
    with inherited_pull_id_scope("outer0000000"), inherited_pull_id_scope() as inner:
        assert inner == "outer0000000"  # inherit, not shadow-mint
        with inherited_pull_id_scope("inner1111111"):
            assert current_pull_id() == "inner1111111"
        assert current_pull_id() == "outer0000000"
    assert current_pull_id() == ""


# ── request_id (DEC-DASHBOARD-REQUEST-LOGGING-001) — mirrors pull_id ──────────


def test_request_id_key_is_request_id() -> None:
    assert REQUEST_ID_KEY == "request_id"


def test_new_request_id_is_twelve_hex_chars() -> None:
    rid = new_request_id()
    assert len(rid) == 12
    assert all(c in "0123456789abcdef" for c in rid)


def test_new_request_id_is_unique() -> None:
    assert len({new_request_id() for _ in range(1000)}) == 1000


def test_current_request_id_is_empty_when_unbound() -> None:
    assert current_request_id() == ""


def test_request_scope_with_explicit_id_binds_and_yields_it() -> None:
    with request_id_scope("cafe00000001") as rid:
        assert rid == "cafe00000001"
        assert current_request_id() == "cafe00000001"


def test_request_scope_with_none_generates_a_fresh_id() -> None:
    with request_id_scope() as rid:
        # Generated branch: a real 12-hex id is minted + bound.
        assert len(rid) == 12
        assert current_request_id() == rid


def test_request_scope_unbinds_on_exit() -> None:
    with request_id_scope("0000cafe0000"):
        assert current_request_id() == "0000cafe0000"
    assert current_request_id() == ""


def test_request_and_pull_ids_coexist() -> None:
    """A request that triggers a pull carries BOTH correlation keys at once."""
    with request_id_scope("req000000001"), pull_id_scope("pull00000001"):
        assert current_request_id() == "req000000001"
        assert current_pull_id() == "pull00000001"
