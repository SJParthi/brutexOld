"""``BRUTEX_FEED_RUST`` flag + kernel resolution + refuse-poison validation
(the LEAF half of ``DEC-FEED-RUST-CORE-001``).

The optional ``brutex_feed`` Rust wheel carries the DECISION plane of
:func:`brutex.feed.replay.arrow_table_to_bars` (positivity refusal order +
the bad-tick keep/skip screen) as ONE pure function returning INTEGERS ONLY.
This module owns everything about *whether and how* the seam may consult it:

* ``feed_rust_enabled`` — the default-OFF opt-in flag (Pattern A: ONLY an
  explicit truthy value enables; unset/empty/falsy/junk are all OFF, with a
  warn-once courtesy on non-empty junk so an operator typo is surfaced).
* ``resolve_feed_kernel`` — the lazy wheel import with the named degrade
  tokens (``wheel_missing`` / ``wheel_stale``) — a missing or stale wheel can
  only ever mean "use the byte-identical Python oracle", never a failure.
* ``validate_scan`` — the refuse-poison structural gate over the kernel's
  output (the ``rust_metrics._valid_drawdown`` precedent): a stale/corrupt
  wheel whose output violates ANY shape law degrades loudly to the oracle,
  never serves.
* ``warn_degrade_once`` / ``reset_for_tests`` — the per-process per-reason
  warn-once registry shared by every degrade site.

LEAF DISCIPLINE (verified import cycle): ``brutex.engine.sweep_lookahead``
imports ``brutex.feed.lookahead`` at module scope, so this module — imported
by ``brutex.feed.replay`` — must NOT import ``env_flag_tristate`` from the
engine (or anything from ``brutex.engine`` / ``brutex.costs`` /
``brutex.dashboard`` / ``brutex.data`` / ``brutex.feed``). It carries its own
inline ``_TRUTHY`` / ``_FALSY`` vocabulary (the ``bad_tick.py`` inline
precedent), mirror-pinned against ``sweep_lookahead``'s sets by a test.

Ships INERT: the flag is default-OFF, read per call (no global latch), and
every existing suite passes unmodified with the flag unset. The one-constant
default flip is the LATER `DEC-FEED-RUST-FLIP-001` (burn-in + autorevert per
the DEC-SWEEP-RUST-METRICS-DEFAULT-ON-001 precedent).
"""

from __future__ import annotations

import os
from typing import Final, Protocol, cast

import structlog

_LOG = structlog.get_logger(__name__)

#: The opt-in environment flag (default OFF — Pattern A).
FEED_RUST_ENV: Final[str] = "BRUTEX_FEED_RUST"

#: ONE structlog event per degrade reason per process (``reason=`` field).
FEED_RUST_FALLBACK_EVENT: Final[str] = "feed_rust.fallback"

#: ONE structlog event per process for a non-empty unrecognised flag value.
FEED_RUST_FLAG_EVENT: Final[str] = "feed_rust.flag_unrecognised"

#: Inline flag vocabulary (LEAF discipline — see the module docstring; the
#: mirror-pin test asserts byte-equality with ``sweep_lookahead``'s sets).
_TRUTHY: Final[frozenset[str]] = frozenset({"1", "true", "yes", "on"})
_FALSY: Final[frozenset[str]] = frozenset({"0", "false", "no", "off"})

#: Warn-once registry: reasons (and the flag-junk token) already logged by
#: this process. Reset via :func:`reset_for_tests`.
_WARNED: Final[set[str]] = set()

#: Structural arities of the kernel result (the frozen FFI shape).
_RESULT_ARITY: Final[int] = 3
_STOP_ARITY: Final[int] = 2
_EVENT_ARITY: Final[int] = 5

#: Highest positivity-stop field index (0/1/2/3 = open/high/low/close).
_MAX_FIELD_INDEX: Final[int] = 3

#: One kernel warn event: ``(emit_at_row, session_first_row, skipped,
#: first_prev_row, first_skip_row)`` — integer row coordinates only.
ScanEvent = tuple[int, int, int, int, int]

#: The kernel's outcome: ``(kept_rows, warn_events, positivity_stop)``.
ScanResult = tuple[list[int], list[ScanEvent], tuple[int, int] | None]


class _ScanKernel(Protocol):
    """The ``brutex_feed.scan_bars`` callable (typed via the wheel's stub)."""

    def __call__(
        self,
        ts_us: list[int],
        opens: list[float],
        highs: list[float],
        lows: list[float],
        closes: list[float],
        screen_enabled: bool,
        max_ratio: float,
        /,
    ) -> ScanResult | None:
        """Scan the raw columns; ``None`` = abstain (degrade to the oracle)."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed


def reset_for_tests() -> None:
    """Clear the per-process warn-once registry (test isolation seam)."""
    _WARNED.clear()


def warn_degrade_once(reason: str, **fields: object) -> None:
    """Log ONE :data:`FEED_RUST_FALLBACK_EVENT` per *reason* per process.

    ``kernel_error`` (the defensive belt around a raising kernel) logs at
    ERROR level; every other degrade reason is a WARNING. Repeat degrades for
    an already-logged reason are silent — a wheel-less box polling the feed
    must never log-storm.
    """
    if reason in _WARNED:
        return
    _WARNED.add(reason)
    if reason == "kernel_error":
        _LOG.error(FEED_RUST_FALLBACK_EVENT, reason=reason, **fields)
    else:
        _LOG.warning(FEED_RUST_FALLBACK_EVENT, reason=reason, **fields)


def feed_rust_enabled(raw: str | None = None) -> bool:
    """Return whether the Rust feed kernel is ENABLED (default ``False``).

    Pattern A (the ``sweep_lookahead_guard_enabled`` law): ONLY an explicit
    truthy literal (``1``/``true``/``yes``/``on``, case-insensitive,
    whitespace-stripped) enables; unset, empty, explicit-falsy and junk are
    all OFF — an unrecognised value must never silently enable an alternate
    execution path (fail-safe default). A NON-EMPTY unrecognised value
    additionally gets one :data:`FEED_RUST_FLAG_EVENT` warning per process
    (the ``rust_metrics`` courtesy — an operator typo is surfaced, not
    silently ignored).

    Parameters
    ----------
    raw
        Explicit value to interpret (tests). ``None`` reads
        :data:`FEED_RUST_ENV` from the environment — per call, no global
        latch.
    """
    value = os.environ.get(FEED_RUST_ENV) if raw is None else raw
    if value is None:
        return False
    stripped = value.strip().lower()
    if stripped in _TRUTHY:
        return True
    if stripped == "" or stripped in _FALSY:
        return False
    if FEED_RUST_FLAG_EVENT not in _WARNED:
        _WARNED.add(FEED_RUST_FLAG_EVENT)
        _LOG.warning(FEED_RUST_FLAG_EVENT, env=FEED_RUST_ENV, value=value)
    return False


def resolve_feed_kernel() -> _ScanKernel | None:
    """Return ``brutex_feed.scan_bars``, or ``None`` (warn-once) when absent.

    The import is LAZY and in-function so this module — and everything that
    imports it, including the repo-wide doctest walker — imports cleanly with
    NO wheel present. A missing wheel (ImportError, including the shim's own
    stale-wheel handshake refusal, or the AttributeError shape a partially
    initialised module can raise) degrades as ``wheel_missing``; an
    importable wheel missing the ``scan_bars`` symbol degrades as
    ``wheel_stale``.
    """
    try:
        import brutex_feed
    except (ImportError, AttributeError):
        warn_degrade_once("wheel_missing")
        return None
    kernel = getattr(brutex_feed, "scan_bars", None)
    if kernel is None:
        warn_degrade_once("wheel_stale")
        return None
    return cast("_ScanKernel", kernel)


def _invalid(n_rows: int, detail: str) -> bool:
    """Refuse a poisoned kernel result: warn once, return ``False``."""
    warn_degrade_once("kernel_invalid", n_rows=n_rows, detail=detail)
    return False


def validate_scan(result: object, n_rows: int) -> bool:
    """Refuse-poison gate over a kernel result (``rust_metrics`` precedent).

    Structural laws (proven for every well-formed input by the crate's own
    property suite — a violation here can only mean a stale/corrupt wheel):

    * the result is a 3-tuple ``(kept_rows, warn_events, positivity_stop)``;
    * ``kept_rows`` — ints, strictly ascending, each in ``[0, n_rows)``;
    * ``warn_events`` — 5-int tuples, strictly ascending by ``emit_at_row``
      with ``emit_at_row <= n_rows``, ``session_first_row in [0, n_rows)``,
      ``skipped >= 1``, ``0 <= first_prev_row < first_skip_row < n_rows``;
    * ``positivity_stop`` — ``None`` or ``(row, field)`` with
      ``row in [0, n_rows)`` and ``field in [0, 3]``; when present, every
      kept row AND every warn anchor is strictly before ``row`` (no boundary
      flush at the stop row, no finish event).

    Negative or out-of-range indices are refused OUTRIGHT — a negative index
    would silently wrap Python list selection. Any violation emits one
    ``kernel_invalid`` warn and the caller degrades to the oracle.
    """
    if not (isinstance(result, tuple) and len(result) == _RESULT_ARITY):
        return _invalid(n_rows, "result_shape")
    kept, warns, stop = result
    if not (isinstance(kept, list) and isinstance(warns, list)):
        return _invalid(n_rows, "column_shape")
    if stop is None:
        kept_bound = n_rows
        emit_bound = n_rows + 1  # a finish event anchors AT n_rows
    else:
        if not (isinstance(stop, tuple) and len(stop) == _STOP_ARITY):
            return _invalid(n_rows, "stop_shape")
        stop_row, stop_field = stop
        if not (isinstance(stop_row, int) and isinstance(stop_field, int)):
            return _invalid(n_rows, "stop_types")
        if not (0 <= stop_row < n_rows and 0 <= stop_field <= _MAX_FIELD_INDEX):
            return _invalid(n_rows, "stop_range")
        # Stop truncation law: nothing recorded at/after the stop row.
        kept_bound = stop_row
        emit_bound = stop_row
    prev_kept = -1
    for idx in kept:
        if not isinstance(idx, int):
            return _invalid(n_rows, "kept_type")
        if idx <= prev_kept or idx >= kept_bound:
            return _invalid(n_rows, "kept_order")
        prev_kept = idx
    prev_emit = -1
    for event in warns:
        if not (isinstance(event, tuple) and len(event) == _EVENT_ARITY):
            return _invalid(n_rows, "event_shape")
        emit, session_first, skipped, first_prev, first_skip = event
        if not all(isinstance(v, int) for v in event):
            return _invalid(n_rows, "event_types")
        if emit <= prev_emit or emit >= emit_bound:
            return _invalid(n_rows, "event_order")
        if not 0 <= session_first < n_rows:
            return _invalid(n_rows, "session_first_range")
        if skipped < 1:
            return _invalid(n_rows, "skipped_floor")
        if not 0 <= first_prev < first_skip < n_rows:
            return _invalid(n_rows, "offender_range")
        prev_emit = emit
    return True


__all__ = [
    "FEED_RUST_ENV",
    "FEED_RUST_FALLBACK_EVENT",
    "FEED_RUST_FLAG_EVENT",
    "ScanEvent",
    "ScanResult",
    "feed_rust_enabled",
    "reset_for_tests",
    "resolve_feed_kernel",
    "validate_scan",
    "warn_degrade_once",
]
