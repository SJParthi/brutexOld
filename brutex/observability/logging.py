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

Reference: Agent 2 of DEC-P0-031 PR #50d review flagged that the vendor
adapter layer cannot log token / response-body / URL safely without
a known event-dict shape. Configuring structlog with a fixed processor
chain pins that shape so the adapter's audit calls are reviewable.
"""

from __future__ import annotations

import contextlib
import logging
import logging.handlers
import os
import sys
from pathlib import Path
from typing import Any, ClassVar, Final

import structlog

#: Default log level — matches the stdlib ``logging`` module's INFO.
#: Production deployments override via :func:`configure_logging`'s
#: ``level`` argument from a config file or env var.
_DEFAULT_LEVEL: Final[str] = "INFO"

#: Environment variable read by :func:`resolve_env_log_level` — the operator's
#: per-process log-level override for entry points that wrap
#: :func:`configure_logging` with env-var control (per this module's design
#: contract, the env-var resolution lives BESIDE the core function, never
#: inside it). First consumer: the CLI sweep entry points
#: (DEC-SWEEP-LOG-VOLUME-GUARD-001).
LOG_LEVEL_ENV: Final[str] = "BRUTEX_LOG_LEVEL"

#: The exact level names :func:`resolve_env_log_level` accepts (matched
#: case-insensitively). Deliberately NARROWER than stdlib
#: ``getLevelNamesMapping`` — an operator env var wants four obvious words,
#: not numeric strings or ``FATAL`` aliases; anything else fails loud.
ENV_ALLOWED_LEVELS: Final[tuple[str, ...]] = ("DEBUG", "INFO", "WARNING", "ERROR")

#: Default rotating-file size before a rollover (10 MiB) and number of
#: retained backups. Override per-call via :func:`configure_logging`.
_DEFAULT_MAX_BYTES: Final[int] = 10 * 1024 * 1024
_DEFAULT_BACKUP_COUNT: Final[int] = 5

#: Marker attribute set on the file handler we add, so repeated
#: :func:`configure_logging` calls can find + replace *our* handler without
#: disturbing any handler a host app may have attached.
_FILE_HANDLER_TAG: Final[str] = "_brutex_file_handler"


class _ConfigState:
    """Module-private state holder for :func:`configure_logging`.

    Held on a class (not a module-level global) so flow-analysis tools
    (CodeQL) can see the read + write in the same scope. Stores the
    ``(level, json_renderer)`` tuple of the last successful call, or
    ``None`` if never configured.
    """

    last: ClassVar[tuple[str, bool, str | None] | None] = None


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


def resolve_log_file(log_file: Path | str | None = None) -> Path:
    """Resolve the rotating log-file path from an argument or the environment.

    Resolution order (first match wins):

    1. The explicit *log_file* argument, if given.
    2. ``$BRUTEX_LOG_FILE`` — a full file path.
    3. ``$BRUTEX_LOG_DIR/brutex.log`` if ``$BRUTEX_LOG_DIR`` is set.
    4. ``~/.brutex/logs/brutex.log`` as the final default.

    The path is returned expanded (``~`` resolved) but **not** created — the
    handler factory in :func:`_build_file_handler` is responsible for
    ``mkdir`` so this function stays a pure path computation (and therefore
    trivially unit-testable).

    Parameters
    ----------
    log_file
        An explicit path override. ``None`` (the default) falls through to
        the env-var chain.

    Returns
    -------
    Path
        The resolved, ``~``-expanded log-file path.
    """
    if log_file is not None:
        return Path(log_file).expanduser()
    env_file = os.environ.get("BRUTEX_LOG_FILE")
    if env_file:
        return Path(env_file).expanduser()
    env_dir = os.environ.get("BRUTEX_LOG_DIR")
    base = Path(env_dir).expanduser() if env_dir else Path.home() / ".brutex" / "logs"
    return base / "brutex.log"


def resolve_env_log_level(*, default: str = _DEFAULT_LEVEL) -> str:
    """Resolve the process log level from ``$BRUTEX_LOG_LEVEL`` (fail-loud).

    The env-var wrapper this module's design contract prescribes for callers
    that want env-var control over :func:`configure_logging` — the core
    function itself stays env-free. An unset or blank/whitespace-only
    variable returns *default*; a recognised name (one of
    :data:`ENV_ALLOWED_LEVELS`, matched case-insensitively after stripping)
    returns its canonical uppercase form; anything else raises — junk is
    NEVER silently coerced to a default (Golden Rule #10: a typo'd
    ``BRUTEX_LOG_LEVEL=DEBG`` silently running at INFO would hide the very
    events the operator asked for).

    Parameters
    ----------
    default
        The level name returned when the variable is unset or blank.

    Returns
    -------
    str
        A canonical uppercase stdlib level name.

    Raises
    ------
    ValueError
        If the variable is set to anything outside
        :data:`ENV_ALLOWED_LEVELS`.
    """
    raw = os.environ.get(LOG_LEVEL_ENV)
    if raw is None or not raw.strip():
        return default
    candidate = raw.strip().upper()
    if candidate not in ENV_ALLOWED_LEVELS:
        allowed = "/".join(ENV_ALLOWED_LEVELS)
        raise ValueError(
            f"invalid {LOG_LEVEL_ENV}={raw!r}: expected one of {allowed} "
            "(case-insensitive) — refusing to guess a log level"
        )
    return candidate


def _build_file_handler(
    path: Path,
    *,
    formatter: logging.Formatter,
    level: int,
    max_bytes: int,
    backup_count: int,
) -> logging.Handler:
    """Construct a tagged :class:`RotatingFileHandler` for *path*.

    Ensures the parent directory exists, attaches the shared structlog
    formatter, sets the level, and tags the handler with
    :data:`_FILE_HANDLER_TAG` so :func:`configure_logging` can find +
    replace it on a subsequent call without touching other handlers.

    Parameters
    ----------
    path
        Destination log file (parent dirs created if absent).
    formatter
        The shared structlog ``ProcessorFormatter`` (so file lines match
        the console renderer's shape).
    level
        Numeric log level for the handler.
    max_bytes
        Rollover threshold in bytes.
    backup_count
        Number of rotated backups to retain.

    Returns
    -------
    logging.Handler
        A configured, tagged rotating file handler.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        path,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    handler.setFormatter(formatter)
    handler.setLevel(level)
    setattr(handler, _FILE_HANDLER_TAG, True)
    return handler


def configure_logging(
    *,
    level: str = _DEFAULT_LEVEL,
    json_renderer: bool = True,
    log_file: Path | str | None = None,
    enable_file: bool = False,
    max_bytes: int = _DEFAULT_MAX_BYTES,
    backup_count: int = _DEFAULT_BACKUP_COUNT,
) -> Path | None:
    """Configure structlog + stdlib logging for BruteX.

    Parameters
    ----------
    level
        Stdlib level name (``"DEBUG"``, ``"INFO"``, ``"WARNING"``, etc.).
    json_renderer
        If True, emit JSON lines (production-friendly). If False, use
        :class:`structlog.dev.ConsoleRenderer` with colours.
    log_file
        Explicit rotating-file path. When ``None`` (and ``enable_file`` is
        True) the path is resolved from the environment via
        :func:`resolve_log_file`.
    enable_file
        Opt-in switch for the rotating file sink. When ``True`` (or when an
        explicit ``log_file`` is supplied) structlog ALSO writes to a
        rotating file in addition to the console handler. Defaults to
        ``False`` so existing callers keep console-only behaviour.
    max_bytes
        Rollover threshold for the file handler (default 10 MiB).
    backup_count
        Number of rotated backups to retain (default 5).

    Returns
    -------
    Path | None
        The resolved log-file path when the file sink is active, else
        ``None``.

    Notes
    -----
    Idempotent for identical arguments. Subsequent calls with different
    arguments override the prior config (last-call-wins). The file sink is
    guarded so repeated calls replace (never stack) the BruteX file handler.

    Call this **once per process**, ideally before any logger is first used.
    structlog caches the logger factory on a logger's first *use*
    (``cache_logger_on_first_use=True``), so enabling the file sink *after* a
    logger has already emitted will not retro-route that cached logger's events
    to the file — call :func:`reset_configuration` first if you must
    reconfigure mid-process. (The one production caller, ``cli.cmd_dashboard``,
    configures once at startup before any request-path logger fires.)
    """
    file_enabled = enable_file or log_file is not None
    resolved_file = resolve_log_file(log_file) if file_enabled else None
    file_key = str(resolved_file) if resolved_file is not None else None

    cfg = (level, json_renderer, file_key)
    if cfg == _ConfigState.last:
        return resolved_file

    level_int = _level_to_int(level)
    shared = _shared_processors()
    renderer: Any = (
        structlog.processors.JSONRenderer()
        if json_renderer
        else structlog.dev.ConsoleRenderer(colors=True)
    )

    if resolved_file is None:
        # Console-only path — preserved verbatim from the pre-file-sink
        # design. structlog's PrintLogger writes straight to stderr; the
        # stdlib handler bridges third-party logs through the same chain.
        structlog.configure(
            processors=[*shared, renderer],
            wrapper_class=structlog.make_filtering_bound_logger(level_int),
            context_class=dict,
            logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
            cache_logger_on_first_use=True,
        )
        formatter = structlog.stdlib.ProcessorFormatter(processors=[*shared, renderer])
        console_handler: logging.Handler = logging.StreamHandler(sys.stderr)
        console_handler.setFormatter(formatter)
        root_logger = logging.getLogger()
        _close_handlers(root_logger.handlers)
        root_logger.handlers = [console_handler]
        root_logger.setLevel(level_int)
        _ConfigState.last = cfg
        return None

    # File-sink path — route structlog itself through stdlib logging so
    # BOTH the console handler AND the rotating file handler receive every
    # BruteX event (the PrintLogger path would bypass stdlib handlers and
    # only the file's third-party logs would appear).
    structlog.configure(
        processors=[
            *shared,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level_int),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
    console_formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setFormatter(console_formatter)

    # File lines must be machine-parseable for the /logs viewer + audit
    # tooling, so the file sink ALWAYS uses the JSON renderer regardless of
    # the console's renderer choice.
    file_formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
        ],
    )
    file_handler = _build_file_handler(
        resolved_file,
        formatter=file_formatter,
        level=level_int,
        max_bytes=max_bytes,
        backup_count=backup_count,
    )
    handlers: list[logging.Handler] = [console_handler, file_handler]

    root_logger = logging.getLogger()
    # Close + replace handlers so repeated calls don't stack them and never
    # leak open file descriptors (the BruteX file handler is tagged, so this
    # never double-adds a rotating sink).
    _close_handlers(root_logger.handlers)
    root_logger.handlers = handlers
    root_logger.setLevel(level_int)

    _ConfigState.last = cfg
    return resolved_file


def _close_handlers(handlers: list[logging.Handler]) -> None:
    """Close every handler in *handlers*, swallowing close errors.

    Used when :func:`configure_logging` swaps the root logger's handler
    list and by :func:`reset_configuration`, so a discarded rotating file
    handler releases its file descriptor instead of leaking it.

    Parameters
    ----------
    handlers
        The handler list being discarded.
    """
    for handler in handlers:
        with contextlib.suppress(OSError, ValueError):
            handler.close()


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
    """Reset the idempotence guard and release any file handler. Test-only helper."""
    root_logger = logging.getLogger()
    _close_handlers(root_logger.handlers)
    root_logger.handlers = []
    _ConfigState.last = None
    structlog.reset_defaults()
