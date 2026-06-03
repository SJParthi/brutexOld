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
    current_pull_id,
    new_pull_id,
    pull_id_scope,
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
