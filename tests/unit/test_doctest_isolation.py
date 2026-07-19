"""Regression teeth for ``tests.fakes.doctest_isolation.quiet_structlog``.

The shard-2 race (CI run 29668853084): a detached daemon thread's structlog
write during ``doctest.testmod``'s ``sys.stdout`` swap pollutes an example's
captured "Got" output and fails an unrelated pure example. The probe below
reproduces the race DETERMINISTICALLY (the cross-thread write is joined
INSIDE the example, so it always lands mid-capture) and proves:

1. FAILS-BEFORE: without the helper the doctest genuinely fails (the
   pollution is real, the fix is load-bearing — not a vacuous wrapper).
2. PASSES-AFTER: under ``quiet_structlog`` the same doctest passes.
3. RESTORE: the prior structlog configuration is restored on exit, raise
   path included (a leaked silencing config would mute the whole suite).
"""

from __future__ import annotations

import doctest
import threading
import types

import pytest
import structlog

from tests.fakes.doctest_isolation import quiet_structlog


def _build_polluting_probe_module() -> types.ModuleType:
    """A synthetic module whose ONE doctest triggers a cross-thread structlog write.

    The example calls a helper that spawns a thread, JOINS it (so the write
    deterministically happens while doctest's fake stdout is installed), then
    returns ``0`` — the expected output. Unsilenced, the thread's log line
    lands in the doctest capture before the ``0`` and the example fails —
    the exact CI shard-2 shape (``lake.invariant.scan_complete`` +
    ``lake_heal.sweep_complete`` before ``bits_to_mask()``'s expected ``0``).
    """
    mod = types.ModuleType("doctest_pollution_probe")
    mod.__doc__ = """Probe module.

    >>> _emit_from_thread()
    0
    """

    def _emit_from_thread() -> int:
        thread = threading.Thread(
            target=lambda: structlog.get_logger("pollution_probe").info("probe.cross_thread_write")
        )
        thread.start()
        thread.join()
        return 0

    # Module-attribute install so doctest's module-globals lookup finds the
    # helper (a plain attribute assignment on ModuleType trips pyright's
    # reportAttributeAccessIssue; B010 is the deliberate escape).
    setattr(mod, "_emit_from_thread", _emit_from_thread)  # noqa: B010
    return mod


def test_cross_thread_structlog_write_pollutes_doctest_without_helper() -> None:
    """FAILS-BEFORE half: the race is real — unsilenced, the doctest fails."""
    mod = _build_polluting_probe_module()
    results = doctest.testmod(mod, verbose=False)
    assert results.attempted == 1
    assert results.failed == 1, (
        "expected the cross-thread structlog write to pollute the doctest "
        "capture (the CI shard-2 race) — if this now passes without "
        "quiet_structlog, the structlog resolution semantics changed and the "
        "helper should be re-evaluated"
    )


def test_quiet_structlog_makes_the_same_doctest_pass() -> None:
    """PASSES-AFTER half: under the helper the identical doctest is clean."""
    mod = _build_polluting_probe_module()
    with quiet_structlog():
        results = doctest.testmod(mod, verbose=False)
    assert results.attempted == 1
    assert results.failed == 0


def test_quiet_structlog_restores_prior_config() -> None:
    """The prior logger factory is restored on clean exit AND on a raise."""
    before = structlog.get_config()["logger_factory"]
    with quiet_structlog():
        inside = structlog.get_config()["logger_factory"]
        assert isinstance(inside, structlog.ReturnLoggerFactory)
    assert structlog.get_config()["logger_factory"] is before

    # CodeQL restructure (code-scanning alert 1335): a literal ``raise`` as
    # the whole ``with pytest.raises(...)`` body made CodeQL's
    # intraprocedural unreachable-code query flag the assert below (it does
    # not model pytest.raises swallowing the exception). Raising through a
    # helper keeps the post-block assert visibly reachable — behavior
    # identical, no suppression comment.
    def _boom() -> None:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"), quiet_structlog():
        _boom()
    assert structlog.get_config()["logger_factory"] is before
