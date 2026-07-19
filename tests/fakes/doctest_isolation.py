"""Structlog isolation for the doctest gates — the shard-2 stdout-race fix.

``doctest.testmod`` captures example output by SWAPPING ``sys.stdout`` for its
internal fake while each example runs. structlog's default lazy proxies
re-resolve the logger factory on EVERY call (``cache_logger_on_first_use``
defaults ``False``), and the default ``PrintLogger`` prints to the DYNAMIC
current ``sys.stdout`` (``structlog._output.PrintLogger.msg`` passes
``file=None`` — the live stdout — whenever its bound file is the original
stdout object). So a background daemon completing MID-example lands its log
lines INSIDE the doctest capture and fails an unrelated pure example.

Executed evidence (CI run 29668853084, unit shard 2, head 7077a6a): the
fire-and-forget lake-heal L3 ``StartupSweeper`` daemon (spawned by an earlier
test in the same shard process; detached BY DESIGN — never joinable from the
gate) finished while ``doctest.testmod(combo_mask)`` was capturing, and the
``bits_to_mask()`` example's "Got" carried ``lake.invariant.scan_complete`` +
``lake_heal.sweep_complete`` INFO lines before the expected ``0`` — a
pre-existing cross-thread race surfaced by the shard-population reshuffle.

:func:`quiet_structlog` closes the race for the doctest window: structlog is
temporarily configured with ``ReturnLoggerFactory`` (a no-op logger), so any
cross-thread structlog write during the window goes nowhere instead of into
the doctest fake; the prior configuration is restored in ``finally``. Used by
BOTH doctest gates (``tests/unit/test_combo_mask_doctests.py`` and the
repo-wide ``tests/unit/test_all_brutex_doctests.py`` — the same vulnerability,
fixed at ONE source per CLAUDE.md §11 COMMON).

Honest residual: a logger CACHED as a concrete instance BEFORE the window
(possible only if a prior test configured ``cache_logger_on_first_use=True``
and the logger's first use bound the original stdout) is not re-routed by a
temporary ``configure`` — unreached today: the suite runs on structlog
defaults (``tests/conftest.py`` configures no structlog state) and the tests
that do call ``configure_logging`` restore defaults on exit.
"""

from __future__ import annotations

import contextlib
from collections.abc import Generator

import structlog


@contextlib.contextmanager
def quiet_structlog() -> Generator[None]:
    """Silence structlog for the duration; restore the prior config on exit.

    Any structlog write resolved during the window — including one from
    ANOTHER thread (the polluting class) — lands on a no-op ``ReturnLogger``
    instead of whatever ``sys.stdout`` currently points at (the doctest fake).
    """
    saved = structlog.get_config()
    structlog.configure(logger_factory=structlog.ReturnLoggerFactory())
    try:
        yield
    finally:
        structlog.configure(**saved)
