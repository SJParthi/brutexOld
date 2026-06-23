"""Unit tests for ``brutex.adapters.groww.deadline`` (the wall-clock freeze guard).

DEC-GROWW-PULL-CALL-DEADLINE-001 + MINOR-1 of DEC-GROWW-TOKEN-REAUTH-001. The
:func:`run_with_deadline` helper bounds an otherwise-unbounded SDK call (historical
candles OR the token re-mint) by a wall-clock deadline. These cover all three
branches with NO ``growwapi`` / ``pyotp`` and no network — the fast success path, the
inner-exception re-raise, and the TIMEOUT path (a fn that blocks on a
:class:`threading.Event` past a short deadline → :class:`TimeoutError`).
"""

from __future__ import annotations

import threading
from collections.abc import Mapping

import pytest
from brutex.adapters.groww.deadline import run_with_deadline


def test_returns_the_value_when_fast() -> None:
    """A fn that returns within the deadline yields its value verbatim."""
    assert run_with_deadline(lambda: {"candles": []}, 5.0) == {"candles": []}


def test_propagates_the_underlying_error() -> None:
    """An inner exception (not a timeout) is re-raised on the caller thread."""

    def _boom() -> Mapping[str, object]:
        raise ValueError("real failure, not a timeout")

    with pytest.raises(ValueError, match="real failure"):
        run_with_deadline(_boom, 5.0)


def test_raises_timeout_when_fn_blows_the_deadline() -> None:
    """A fn that blocks past the deadline raises a ``deadline exceeded`` TimeoutError.

    The released-after event lets the abandoned daemon worker exit cleanly once the
    deadline has already fired (it never blocks the test process — it is a daemon).
    """
    release = threading.Event()

    def _hang() -> Mapping[str, object]:
        release.wait(timeout=5.0)  # blocks well past the 0.05s deadline below
        return {"candles": []}

    try:
        with pytest.raises(TimeoutError, match="deadline exceeded"):
            run_with_deadline(_hang, 0.05)
    finally:
        release.set()  # let the abandoned daemon worker exit


def test_is_generic_over_the_return_type() -> None:
    """The guard preserves a non-Mapping return type (e.g. a tuple — the re-mint shape)."""
    out = run_with_deadline(lambda: ("a", "b", "c"), 5.0)
    assert out == ("a", "b", "c")
