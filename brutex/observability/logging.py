"""Structlog configuration — first production logging wiring for BruteX.

The base ``brutex.observability.__init__`` docstring has long noted that
"structlog is configured in `brutex.__init__` (wired later)". This module
is the "later". It exposes a single :func:`configure_logging` entry point
that downstream callers (orchestrator workers, CLI, dashboard, anything
that needs structured log emission) invoke once at process start.

Design contract:

1. **Idempotent.** Calling :func:`configure_logging` more than once with
   identical arguments is a no-op; subsequent calls with different
   arguments override the prior config. The orchestrator may invoke
   this from multiple entry points (CLI / dashboard / worker pool); we
   accept the last call wins rather than fight it.
2. **No hidden globals.** The function takes explicit ``level`` and
   ``json_renderer`` arguments; no env-var magic. Callers that want
   env-var control wrap this function — keeps this module easy to test.
3. **Renderer pluggable.** ``json_renderer=True`` (default) emits one
   JSON object per log line (production / file-tailing friendly).
   ``json_renderer=False`` swaps in ``ConsoleRenderer`` with colours
   for interactive development.
4. **Stdlib-bridged.** ``foreign_pre_chain`` lets third-party libraries
   that use stdlib ``logging`` (httpx, uvicorn, fastapi) flow through
   the same processor chain, so JSON output is uniform whether the
   event originated in our code or a dependency.

Why structlog (not stdlib logging directly):

* Structured key-value pairs are first-class — every log entry is a
  dict, not an interpolated f-string. This is the audit-log discipline
  from CLAUDE.md Golden Rule #8 carried down to runtime logging.
* Processors are composable and testable. Tests can capture the event
  dict directly without parsing strings.

Reference: Agent 2 of DEC-P0-031 PR #50d review flagged that the
adapter layer cannot log token / response-body / URL safely without
a known event-dict shape. Configuring structlog with a fixed processor
chain pins that shape so the adapter's audit calls are reviewable.
"""

from __future__ import annotations

import logging
import sys
from typing import Any, Final

import structlog

#: Default log level — matches the stdlib ``logging`` module's INFO.
#: Production deployments override via :func:`configure_logging`'s
#: ``level`` argument from a config file or env var.
_DEFAULT_LEVEL: Final[str] = "INFO"

#: Module-level guard so :func:`configure_logging` is idempotent for
#: identical arguments. Stores ``(level, json_renderer)`` tuple of the
#: last successful call, or ``None`` if never configured.
_LAST_CONFIG: tuple[str, bool] | None = None


def _shared_processors() -> list[Any]:
    """Build the processor chain shared between structlog + stdlib logging.

    Keeping this in one place ensures both native structlog calls and
    stdlib-routed third-party logs (httpx, uvicorn, etc.) go through
    the same enrichment pipeline.
    """
    return [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]


def configure_logging(
    *,
    level: str = _DEFAULT_LEVEL,
    json_renderer: bool = True,
) -> None:
    """Configure structlog + stdlib logging for BruteX.

    Parameters
    ----------
    level
        Stdlib level name (``"DEBUG"``, ``"INFO"``, ``"WARNING"``, etc.).
    json_renderer
        If True, emit JSON lines (production-friendly). If False, use
        :class:`structlog.dev.ConsoleRenderer` with colours.

    Notes
    -----
    Idempotent for identical arguments. Subsequent calls with different
    arguments override the prior config (last-call-wins).
    """
    global _LAST_CONFIG  # noqa: PLW0603  # intentional idempotence guard
    cfg = (level, json_renderer)
    if cfg == _LAST_CONFIG:
        return

    shared = _shared_processors()
    renderer: Any = (
        structlog.processors.JSONRenderer()
        if json_renderer
        else structlog.dev.ConsoleRenderer(colors=True)
    )

    # Configure structlog
    structlog.configure(
        processors=[*shared, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(_level_to_int(level)),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )

    # Bridge stdlib logging → structlog so third-party libs use the same
    # processor chain. Handler emits via structlog's print logger.
    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[*shared, renderer],
    )
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    # Replace handlers so repeated calls don't stack them.
    root_logger.handlers = [handler]
    root_logger.setLevel(_level_to_int(level))

    _LAST_CONFIG = cfg


def _level_to_int(level: str) -> int:
    """Translate a stdlib level name (or int as str) to its int value."""
    if level.isdigit():
        return int(level)
    mapping = logging.getLevelNamesMapping()
    upper = level.upper()
    if upper not in mapping:
        raise ValueError(f"_level_to_int: unrecognised level name {level!r}")
    return mapping[upper]


def reset_configuration() -> None:
    """Reset the idempotence guard. Test-only helper."""
    global _LAST_CONFIG  # noqa: PLW0603  # test-only reset path
    _LAST_CONFIG = None
    structlog.reset_defaults()
