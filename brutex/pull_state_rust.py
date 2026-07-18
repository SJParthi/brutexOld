"""``BRUTEX_PULL_STATE_RUST`` flag + kernel resolution + the guarded consult
(the LEAF half of the ``DEC-RUST-PULL-STATE-001`` lane, Phase 1).

The optional ``brutex_pull_state`` Rust wheel carries the DECISION plane of
the pull-state modules, starting with the market-hours guard predicate
:func:`brutex.calendars.market_hours.pull_allowed_now`
(DEC-GROWW-MARKET-HOURS-GUARD-001, MIGRATION.md #1692) as ONE pure function
over INTEGERS ONLY (microsecond-of-day + the caller-resolved trading-day
flag). This module owns everything about *whether and how* the seam may
consult it — the exact ``brutex/feed_rust.py`` shape
(DEC-FEED-RUST-CORE-001):

* ``pull_state_rust_enabled`` — the default-OFF opt-in flag (Pattern A: ONLY
  an explicit truthy value enables; unset/empty/falsy/junk are all OFF, with
  a warn-once courtesy on non-empty junk so an operator typo is surfaced).
* ``resolve_pull_state_kernel`` — the lazy wheel import with the named
  degrade tokens (``wheel_missing`` / ``wheel_stale``) — a missing or stale
  wheel can only ever mean "use the byte-identical Python oracle", never a
  failure.
* ``rust_pull_allowed`` — the whole guarded consult the market-hours seam
  calls: integer packing, the defensive ``kernel_error`` belt around a
  raising kernel, and the refuse-poison ``kernel_invalid`` gate (a non-bool
  result can only mean a stale/corrupt wheel — degrade loudly, never serve).
* ``warn_degrade_once`` / ``reset_for_tests`` — the per-process per-reason
  warn-once registry shared by every degrade site.

LEAF DISCIPLINE: this module is imported by ``brutex.calendars.market_hours``
— the dependency-light guard BOTH ingest spines consult — so it imports
ONLY stdlib + structlog (never ``brutex.engine`` / ``brutex.dashboard`` /
``brutex.data`` / ``brutex.feed`` / ``brutex.calendars``). It carries its own
inline ``_TRUTHY`` / ``_FALSY`` vocabulary (the ``feed_rust.py`` /
``bad_tick.py`` inline precedent, mirror-pinned against
``sweep_lookahead``'s sets by a test).

Ships INERT: the flag is default-OFF, read per call (no global latch), and
every existing suite passes unmodified with the flag unset — flag OFF is
byte-identical AND never imports the wheel (bomb-kernel spy-pinned). The
one-constant default flip is a LATER lane phase (burn-in + autorevert per
the DEC-SWEEP-RUST-METRICS-DEFAULT-ON-001 precedent), never this one.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Final, Protocol, cast

import structlog

_LOG = structlog.get_logger(__name__)

#: The opt-in environment flag (default OFF — Pattern A).
PULL_STATE_RUST_ENV: Final[str] = "BRUTEX_PULL_STATE_RUST"

#: ONE structlog event per degrade reason per process (``reason=`` field).
PULL_STATE_RUST_FALLBACK_EVENT: Final[str] = "pull_state_rust.fallback"

#: ONE structlog event per process for a non-empty unrecognised flag value.
PULL_STATE_RUST_FLAG_EVENT: Final[str] = "pull_state_rust.flag_unrecognised"

#: Inline flag vocabulary (LEAF discipline — see the module docstring; the
#: mirror-pin test asserts byte-equality with ``sweep_lookahead``'s sets).
_TRUTHY: Final[frozenset[str]] = frozenset({"1", "true", "yes", "on"})
_FALSY: Final[frozenset[str]] = frozenset({"0", "false", "no", "off"})

#: Warn-once registry: reasons (and the flag-junk token) already logged by
#: this process. Reset via :func:`reset_for_tests`.
_WARNED: Final[set[str]] = set()


class _PullAllowedKernel(Protocol):
    """The ``brutex_pull_state.pull_allowed_us`` callable (typed via the wheel's stub)."""

    def __call__(self, us_of_day: int, is_trading_day: bool, /) -> bool:
        """Return the guard verdict for the packed IST instant."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed


def reset_for_tests() -> None:
    """Clear the per-process warn-once registry (test isolation seam)."""
    _WARNED.clear()


def warn_degrade_once(reason: str, **fields: object) -> None:
    """Log ONE :data:`PULL_STATE_RUST_FALLBACK_EVENT` per *reason* per process.

    ``kernel_error`` (the defensive belt around a raising kernel) logs at
    ERROR level; every other degrade reason is a WARNING. Repeat degrades for
    an already-logged reason are silent — a wheel-less box consulting the
    guard per pull window must never log-storm.
    """
    # Deliberately unlocked check-then-add (the feed_rust/rust_metrics
    # warn-once precedent): two threads racing the same reason can at worst
    # BOTH log it once (an at-most-double line, never a storm and never a
    # miss) — a lock here would buy nothing.
    if reason in _WARNED:
        return
    _WARNED.add(reason)
    if reason == "kernel_error":
        _LOG.error(PULL_STATE_RUST_FALLBACK_EVENT, reason=reason, **fields)
    else:
        _LOG.warning(PULL_STATE_RUST_FALLBACK_EVENT, reason=reason, **fields)


def pull_state_rust_enabled(raw: str | None = None) -> bool:
    """Return whether the Rust pull-state kernel is ENABLED (default ``False``).

    Pattern A (the ``feed_rust_enabled`` law): ONLY an explicit truthy
    literal (``1``/``true``/``yes``/``on``, case-insensitive,
    whitespace-stripped) enables; unset, empty, explicit-falsy and junk are
    all OFF — an unrecognised value must never silently enable an alternate
    execution path (fail-safe default). A NON-EMPTY unrecognised value
    additionally gets one :data:`PULL_STATE_RUST_FLAG_EVENT` warning per
    process (an operator typo is surfaced, not silently ignored).

    Parameters
    ----------
    raw
        Explicit value to interpret (tests). ``None`` reads
        :data:`PULL_STATE_RUST_ENV` from the environment — per call, no
        global latch.
    """
    value = os.environ.get(PULL_STATE_RUST_ENV) if raw is None else raw
    if value is None:
        return False
    stripped = value.strip().lower()
    if stripped in _TRUTHY:
        return True
    if stripped == "" or stripped in _FALSY:
        return False
    if PULL_STATE_RUST_FLAG_EVENT not in _WARNED:
        _WARNED.add(PULL_STATE_RUST_FLAG_EVENT)
        _LOG.warning(PULL_STATE_RUST_FLAG_EVENT, env=PULL_STATE_RUST_ENV, value=value)
    return False


def resolve_pull_state_kernel() -> _PullAllowedKernel | None:
    """Return ``brutex_pull_state.pull_allowed_us``, or ``None`` (warn-once) when absent.

    The import is LAZY and in-function so this module — and everything that
    imports it, including the repo-wide doctest walker — imports cleanly with
    NO wheel present. A missing wheel (ImportError, including the shim's own
    stale-wheel handshake refusal, or the AttributeError shape a partially
    initialised module can raise) degrades as ``wheel_missing``; an
    importable wheel missing the ``pull_allowed_us`` symbol degrades as
    ``wheel_stale``.
    """
    try:
        import brutex_pull_state
    except (ImportError, AttributeError):
        warn_degrade_once("wheel_missing")
        return None
    kernel = getattr(brutex_pull_state, "pull_allowed_us", None)
    if kernel is None:
        warn_degrade_once("wheel_stale")
        return None
    return cast("_PullAllowedKernel", kernel)


def rust_pull_allowed(ist_dt: datetime, is_trading_day_flag: bool) -> bool | None:
    """Consult the Rust kernel for the guard verdict; ``None`` = use the oracle.

    The whole guarded consult the market-hours seam calls when the flag is
    ON: packs *ist_dt*'s wall-clock time to MICROSECOND-OF-DAY (only
    ``.time()`` is read — exactly the Python predicate's law; the packing is
    order-isomorphic to ``datetime.time`` comparison so the ``[08:45,
    15:35)`` boundary is bit-exact), calls the kernel under the defensive
    belt (ANY kernel raise degrades as ``kernel_error`` — never a failed
    pull decision), and refuse-poison-gates the result (a non-``bool`` can
    only mean a stale/corrupt wheel → ``kernel_invalid`` → the oracle).

    Returns
    -------
    bool | None
        The kernel's verdict, or ``None`` when the caller must fall through
        to the byte-identical Python oracle.
    """
    kernel = resolve_pull_state_kernel()
    if kernel is None:
        return None
    t = ist_dt.time()
    us_of_day = ((t.hour * 60 + t.minute) * 60 + t.second) * 1_000_000 + t.microsecond
    # Cast to ``object`` deliberately: the Protocol promises ``bool``, but the
    # refuse-poison gate below must stay REACHABLE for a stale/corrupt wheel
    # that breaks the promise (the validate_scan posture — a type-checker
    # narrowing to ``bool`` would mark the gate unreachable).
    try:
        verdict = cast("object", kernel(us_of_day, bool(is_trading_day_flag)))
    except Exception as exc:  # the defensive belt: ANY kernel raise degrades to the oracle
        warn_degrade_once("kernel_error", error=repr(exc))
        return None
    # Refuse-poison gate: bool EXACTLY (a stale/corrupt wheel returning an
    # int/None/object must degrade loudly, never serve as a truthy pun).
    if not isinstance(verdict, bool):
        warn_degrade_once("kernel_invalid", detail=type(verdict).__name__)
        return None
    return verdict


__all__ = [
    "PULL_STATE_RUST_ENV",
    "PULL_STATE_RUST_FALLBACK_EVENT",
    "PULL_STATE_RUST_FLAG_EVENT",
    "pull_state_rust_enabled",
    "reset_for_tests",
    "resolve_pull_state_kernel",
    "rust_pull_allowed",
    "warn_degrade_once",
]


# --- pull_outcome (Wave 1A) -------------------------------------------------
# DEC-RUST-PULL-STATE-001 Wave 1A: the last-pull OUTCOME decision kernel
# (DEC-OPERATOR-HEALTH-TRUTH-001 re-hosted). The wheel's
# ``classify_pull_outcome`` returns MACHINE CODES only — ``(outcome, tone,
# headline_code, detail_code)``, with the ``@verdict`` / ``@verdict_prepull``
# markers handing the banner fold back to Python — and ``verdict_correlates``
# carries the run-id correlation law. Every human string and the
# ``verdict_banner`` colour authority stay in
# ``brutex.dashboard.pull_outcome`` (CLAUDE.md §11 COMMON). This section is
# APPEND-ONLY over the Phase-1 surface above (the integrator's hub-merge
# convention); it reuses the SAME flag, warn-once registry and degrade
# vocabulary (``wheel_missing`` / ``wheel_stale`` / ``kernel_error`` /
# ``kernel_invalid``).


class _ClassifyOutcomeKernel(Protocol):
    """The ``brutex_pull_state.classify_pull_outcome`` callable (codes only)."""

    def __call__(
        self,
        state: str,
        detail: str,
        failed: int,
        stalled: bool,
        wedged: bool,
        has_verdict: bool,
        /,
    ) -> tuple[str, str, str, str]:
        """Return the decision codes for the packed snapshot fields."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed


class _VerdictCorrelatesKernel(Protocol):
    """The ``brutex_pull_state.verdict_correlates`` callable."""

    def __call__(self, retained_id: str, verdict_id: str, /) -> bool:
        """Return whether the verdict holder correlates with the retained run."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed


def _resolve_wheel_symbol(symbol: str) -> object | None:
    """Return the named wheel callable, or ``None`` (warn-once) when absent.

    The Wave-1A twin of :func:`resolve_pull_state_kernel` (which stays
    byte-untouched — append-only discipline): lazy in-function import, the
    SAME ``wheel_missing`` / ``wheel_stale`` degrade tokens — a Phase-1-only
    wheel that predates *symbol* degrades as ``wheel_stale``, never a crash.
    """
    try:
        import brutex_pull_state
    except (ImportError, AttributeError):
        warn_degrade_once("wheel_missing")
        return None
    kernel: object | None = getattr(brutex_pull_state, symbol, None)
    if kernel is None:
        warn_degrade_once("wheel_stale", symbol=symbol)
        return None
    return kernel


def rust_classify_pull_outcome(
    state: str,
    detail: str,
    failed: int,
    stalled: bool,
    wedged: bool,
    has_verdict: bool,
) -> tuple[str, str, str, str] | None:
    """Consult the Rust outcome kernel; ``None`` = use the Python oracle.

    Calls the kernel under the defensive belt (ANY raise — including the pyo3
    ``OverflowError`` on an out-of-``i64`` *failed* — degrades as
    ``kernel_error``, never a failed classification) and refuse-poison-gates
    the result: anything but a 4-tuple of ``str`` can only mean a
    stale/corrupt wheel → ``kernel_invalid`` → the oracle. The CODE-level
    vocabulary itself is validated by the consumer
    (``brutex.dashboard.pull_outcome``), which owns the composition tables.
    """
    kernel = _resolve_wheel_symbol("classify_pull_outcome")
    if kernel is None:
        return None
    typed = cast("_ClassifyOutcomeKernel", kernel)
    try:
        raw = cast(
            "object",
            typed(state, detail, failed, bool(stalled), bool(wedged), bool(has_verdict)),
        )
    except Exception as exc:  # the defensive belt: ANY kernel raise degrades to the oracle
        warn_degrade_once("kernel_error", error=repr(exc))
        return None
    if (
        not isinstance(raw, tuple)
        or len(raw) != 4
        or not all(isinstance(part, str) for part in raw)
    ):
        warn_degrade_once("kernel_invalid", detail=type(raw).__name__)
        return None
    return cast("tuple[str, str, str, str]", raw)


def rust_verdict_correlates(retained_id: str, verdict_id: str) -> bool | None:
    """Consult the Rust correlation-law kernel; ``None`` = use the oracle.

    The same belt + refuse-poison shape as :func:`rust_pull_allowed`: a raise
    degrades as ``kernel_error``; a non-``bool`` result (a truthy pun from a
    stale/corrupt wheel) degrades as ``kernel_invalid`` — never served.
    """
    kernel = _resolve_wheel_symbol("verdict_correlates")
    if kernel is None:
        return None
    typed = cast("_VerdictCorrelatesKernel", kernel)
    try:
        raw = cast("object", typed(retained_id, verdict_id))
    except Exception as exc:  # the defensive belt: ANY kernel raise degrades to the oracle
        warn_degrade_once("kernel_error", error=repr(exc))
        return None
    if not isinstance(raw, bool):
        warn_degrade_once("kernel_invalid", detail=type(raw).__name__)
        return None
    return raw


__all__ += [
    "rust_classify_pull_outcome",
    "rust_verdict_correlates",
]
