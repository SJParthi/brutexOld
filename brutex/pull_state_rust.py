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
from collections.abc import Callable
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


# --- pull_stall (Wave 1C) ---------------------------------------------------
# The wedge/alive decision plane of ``brutex/dashboard/pull_stall.py``
# (DEC-PULL-WEDGE-ESCALATE-001 + DEC-PULL-TELEMETRY-TRIAD-001). Same seam
# discipline as the market-hours consult above: the caller computes every
# input (ONE monotonic-clock read → ages; timers/locks stay Python), the
# accessor packs nothing but plain bools/floats/tokens, the defensive belt
# catches ANY kernel raise (``kernel_error`` — ERROR, warn-once, with the
# ``fn=`` field naming the symbol) and the refuse-poison gate rejects any
# out-of-vocabulary result (``kernel_invalid``) — ``None`` always means "use
# the byte-identical Python oracle".

#: The wedge-decision arm-token vocabulary (mirrors
#: ``rust/brutex-pull-state/src/stall.rs`` — index == token value).
WEDGE_TOKEN_NO_PROBE: Final[int] = 0
WEDGE_TOKEN_ABSTAIN_PROBE_NONE: Final[int] = 1
WEDGE_TOKEN_ADOPT_BASELINE: Final[int] = 2
WEDGE_TOKEN_REBASELINE: Final[int] = 3
WEDGE_TOKEN_DEFER: Final[int] = 4
WEDGE_TOKEN_ESCALATE: Final[int] = 5

#: Every legal ``wedge_decision`` result — the refuse-poison vocabulary gate.
_WEDGE_TOKENS: Final[frozenset[int]] = frozenset(range(WEDGE_TOKEN_ESCALATE + 1))


def _resolve_kernel_symbol(name: str) -> Callable[..., object] | None:
    """Return the named ``brutex_pull_state`` callable, or ``None`` (warn-once).

    The generic Wave-1B/1C sibling of :func:`resolve_pull_state_kernel` for
    the pull_scope + pull_stall symbols (ONE resolver — the integrator's
    union of the two byte-near-identical wave copies): a missing wheel
    (ImportError, incl. the shim's own stale-wheel handshake refusal, or a
    partially-initialised module's AttributeError) degrades as
    ``wheel_missing``; an importable wheel missing *name* degrades as
    ``wheel_stale`` naming the symbol under BOTH wave field spellings
    (``symbol=`` — the 1B pin — and ``missing=`` — the 1C pin) — a stale
    phase-1 wheel can only ever mean "use the Python oracle", never a
    failure.
    """
    try:
        import brutex_pull_state
    except (ImportError, AttributeError):
        warn_degrade_once("wheel_missing")
        return None
    kernel = getattr(brutex_pull_state, name, None)
    if kernel is None:
        warn_degrade_once("wheel_stale", symbol=name, missing=name)
        return None
    return cast("Callable[..., object]", kernel)


def rust_wedge_decision(
    *,
    hung: bool,
    already_fired: bool,
    hook_bound: bool,
    baseline_captured: bool,
    last_beat_age_s: float,
    wedge_window_s: float,
    restart_age_s: float | None,
    baseline: str | None,
    current: str | None,
) -> int | None:
    """Consult the Rust wedge-escalation kernel; ``None`` = use the oracle.

    Returns the arm token (:data:`WEDGE_TOKEN_NO_PROBE` ..
    :data:`WEDGE_TOKEN_ESCALATE`) the Python oracle would take — the caller
    (``PullWatchdog``) maps it to the arm's ACTIONS verbatim in Python so
    every log line / event payload stays byte-identical. The defensive belt
    (``kernel_error``) and the refuse-poison vocabulary gate
    (``kernel_invalid`` — a non-int / bool pun / out-of-range token can only
    mean a stale/corrupt wheel) both degrade to ``None``.
    """
    kernel = _resolve_kernel_symbol("wedge_decision")
    if kernel is None:
        return None
    try:
        token = kernel(
            hung,
            already_fired,
            hook_bound,
            baseline_captured,
            last_beat_age_s,
            wedge_window_s,
            restart_age_s,
            baseline,
            current,
        )
    except Exception as exc:  # the defensive belt: ANY kernel raise degrades to the oracle
        warn_degrade_once("kernel_error", fn="wedge_decision", error=repr(exc))
        return None
    # Refuse-poison gate: an int EXACTLY (bool is an int subclass — a bool
    # pun degrades) inside the token vocabulary, else the oracle serves.
    if isinstance(token, bool) or not isinstance(token, int) or token not in _WEDGE_TOKENS:
        warn_degrade_once("kernel_invalid", fn="wedge_decision", detail=type(token).__name__)
        return None
    return token


def rust_alive_log_due(since_last_s: float, interval_s: float) -> bool | None:
    """Consult the Rust alive-throttle kernel; ``None`` = use the oracle.

    The emit-now? half of ``_alive_payload_locked``'s throttle law (the
    payload build + the ``_last_alive_log`` stamp stay Python). Belt +
    bool-exact refuse-poison gate as everywhere on this facade.
    """
    kernel = _resolve_kernel_symbol("alive_log_due")
    if kernel is None:
        return None
    try:
        verdict = kernel(since_last_s, interval_s)
    except Exception as exc:  # the defensive belt: ANY kernel raise degrades to the oracle
        warn_degrade_once("kernel_error", fn="alive_log_due", error=repr(exc))
        return None
    if not isinstance(verdict, bool):
        warn_degrade_once("kernel_invalid", fn="alive_log_due", detail=type(verdict).__name__)
        return None
    return verdict


__all__ = [
    "PULL_STATE_RUST_ENV",
    "PULL_STATE_RUST_FALLBACK_EVENT",
    "PULL_STATE_RUST_FLAG_EVENT",
    # --- pull_stall (Wave 1C) ---
    "WEDGE_TOKEN_ABSTAIN_PROBE_NONE",
    "WEDGE_TOKEN_ADOPT_BASELINE",
    "WEDGE_TOKEN_DEFER",
    "WEDGE_TOKEN_ESCALATE",
    "WEDGE_TOKEN_NO_PROBE",
    "WEDGE_TOKEN_REBASELINE",
    "pull_state_rust_enabled",
    "reset_for_tests",
    "resolve_pull_state_kernel",
    "rust_alive_log_due",
    "rust_pull_allowed",
    "rust_wedge_decision",
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


#: The outcome kernel's result arity — ``(outcome, tone, headline_code,
#: detail_code)`` (the refuse-poison shape gate consults it).
_OUTCOME_CODES_ARITY: Final[int] = 4


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
        or len(raw) != _OUTCOME_CODES_ARITY
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


# --- pull_scope (Wave 1B) ---
# DEC-RUST-PULL-STATE-001 Wave 1B: the ``brutex/dashboard/pull_scope.py``
# note-surgery / scope-token kernel accessors — the SAME guarded-consult
# discipline as ``rust_pull_allowed`` above (lazy resolve → defensive belt →
# refuse-poison gate → ``None`` = use the byte-identical Python oracle).
# STRINGS cross this edge (the ONE sanctioned exception to the integers-only
# law — the surgery outputs ARE strings, so byte-exactness IS the contract;
# every anchor literal is drift-pinned by the committed golden fixture pair +
# the cross-language source pin in ``tests/unit/test_pull_scope_rust_seam.py``).
# Append-only section (the wave-worker convention); the generic
# ``_resolve_kernel_symbol`` it consults is the ONE shared Wave-1B/1C
# resolver defined above (integrator union — zero copies).


#: ``scope_token``'s unknown-token verdict code — the Python seam falls
#: through to the oracle, which composes the ``{token!r}`` refusal itself
#: (CPython repr is never re-minted in Rust).
SCOPE_CODE_UNKNOWN: Final[int] = 255

#: The whole ``scope_token`` verdict vocabulary (0=full · 1=spot-only ·
#: 2=fno-only · 255=unknown) — anything else is a stale/corrupt wheel.
_SCOPE_TOKEN_CODES: Final[frozenset[int]] = frozenset({0, 1, 2, SCOPE_CODE_UNKNOWN})

#: The ``_call_kernel`` fault sentinel — distinct from EVERY legitimate
#: kernel return (incl. a poisoned literal ``None``, which must reach the
#: per-accessor refuse-poison gate as ``kernel_invalid``, never silently
#: read as a degrade).
_KERNEL_FAULT: Final[object] = object()

#: The surgery kernels' result arity — ``(note, prefix_present, clause_present)``.
_SURGERY_TRIPLE_ARITY: Final[int] = 3


def _call_kernel(name: str, *args: object) -> object:
    """One guarded kernel call: resolve + the defensive belt.

    Returns the kernel's RAW result (the caller's refuse-poison gate types
    it), or the :data:`_KERNEL_FAULT` sentinel when the wheel is
    missing/stale or the kernel raised (each a named warn-once; ANY kernel
    raise degrades — never a failed note build / scope parse).
    """
    kernel = _resolve_kernel_symbol(name)
    if kernel is None:
        return _KERNEL_FAULT
    try:
        return kernel(*args)
    except Exception as exc:  # the defensive belt (the rust_pull_allowed law)
        warn_degrade_once("kernel_error", symbol=name, error=repr(exc))
        return _KERNEL_FAULT


def rust_scope_token(token: str | None) -> int | None:
    """Consult the kernel's ``scope_token`` verdict; ``None`` = use the oracle.

    Refuse-poison gate: EXACTLY ``int`` (``bool`` is an ``int`` subclass —
    a ``True`` pun from a stale wheel must degrade loudly, never read as
    spot-only) and EXACTLY one of the four vocabulary codes.
    """
    verdict = _call_kernel("scope_token", token)
    if verdict is _KERNEL_FAULT:
        return None
    if type(verdict) is not int or verdict not in _SCOPE_TOKEN_CODES:
        warn_degrade_once("kernel_invalid", symbol="scope_token", detail=repr(verdict)[:80])
        return None
    return verdict


def rust_market_hours_note_rewrite(note: str, phases_replacement: str) -> str | None:
    """Consult the kernel's market-hours note rewrite; ``None`` = the oracle."""
    result = _call_kernel("market_hours_note_rewrite", note, phases_replacement)
    if result is _KERNEL_FAULT:
        return None
    if type(result) is not str:
        warn_degrade_once(
            "kernel_invalid",
            symbol="market_hours_note_rewrite",
            detail=type(result).__name__,
        )
        return None
    return result


def _validated_surgery_triple(name: str, result: object) -> tuple[str, bool, bool] | None:
    """The surgery accessors' shared refuse-poison gate.

    EXACTLY a 3-tuple of ``(str, bool, bool)`` — the rewritten note + the
    two ``note_surgery_miss`` WARN inputs; anything else (wrong arity,
    a list pun, an int-for-bool pun) can only mean a stale/corrupt wheel.
    """
    if (
        isinstance(result, tuple)
        and len(result) == _SURGERY_TRIPLE_ARITY
        and type(result[0]) is str
        and type(result[1]) is bool
        and type(result[2]) is bool
    ):
        return (result[0], result[1], result[2])
    warn_degrade_once("kernel_invalid", symbol=name, detail=type(result).__name__)
    return None


def rust_spot_only_note_surgery(base_note: str) -> tuple[str, bool, bool] | None:
    """Consult the kernel's spot-only note surgery; ``None`` = the oracle.

    Returns ``(rewritten_note, prefix_present, clause_present)`` — the seam
    fires the ``note_surgery_miss`` WARN off the booleans and appends the
    Greeks + crossverify clauses itself (they compose the tail outcome).
    """
    result = _call_kernel("spot_only_note_surgery", base_note)
    if result is _KERNEL_FAULT:
        return None
    return _validated_surgery_triple("spot_only_note_surgery", result)


def rust_fno_only_note_surgery(base_note: str) -> tuple[str, bool, bool] | None:
    """Consult the kernel's fno-only note surgery; ``None`` = the oracle."""
    result = _call_kernel("fno_only_note_surgery", base_note)
    if result is _KERNEL_FAULT:
        return None
    return _validated_surgery_triple("fno_only_note_surgery", result)


__all__ += [
    "SCOPE_CODE_UNKNOWN",
    "rust_fno_only_note_surgery",
    "rust_market_hours_note_rewrite",
    "rust_scope_token",
    "rust_spot_only_note_surgery",
]


# --- auto_pull_gate (Wave 2D) -----------------------------------------------
# The ``--auto`` gate decision plane of ``brutex/calendars/auto_pull_gate.py``
# (DEC-PULL-SCOPED-ALL-ONECLICK-001 + DEC-SETTLE-EVIDENCE-ADOPTION-001). Same
# seam discipline as every section above: the caller (the calendars module)
# resolves the clock, the NSE calendar predicates and the settle tier ITSELF
# (``resolve_settle_resolution`` — env reads + the fail-loud junk raise —
# stays Python by design, the pull_stall env-resolver precedent), then packs
# microsecond-of-day integers + plain flags; codes/bools come back, Python
# composes every human string. ``None`` always means "use the byte-identical
# Python oracle". Append-only section (the wave-worker convention) reusing
# the ONE shared ``_resolve_kernel_symbol`` resolver + ``_call_kernel`` belt.

#: The ``auto_gate_decision`` reason vocabulary (mirrors
#: ``rust/brutex-pull-state/src/auto_pull_gate.rs`` — index == code value).
GATE_CODE_OK: Final[int] = 0
GATE_CODE_NON_TRADING_DAY: Final[int] = 1
GATE_CODE_MUHURAT_DEFERRAL: Final[int] = 2
GATE_CODE_BEFORE_SETTLE: Final[int] = 3
GATE_CODE_MARKET_HOURS_BLOCKED: Final[int] = 4

#: Every legal gate reason — the refuse-poison vocabulary gate.
_GATE_CODES: Final[frozenset[int]] = frozenset(range(GATE_CODE_MARKET_HOURS_BLOCKED + 1))

#: The ``settle_token_verdict`` OK code (the ONLY arm the seam consumes —
#: every non-OK verdict routes back to the Python oracle, which composes +
#: raises the byte-identical fail-loud ``ValueError`` itself).
SETTLE_CODE_OK: Final[int] = 0

#: The whole settle-verdict vocabulary (0=ok · 1=bad_shape ·
#: 2=not_wall_clock · 3=below_floor) — anything else is a stale/corrupt wheel.
_SETTLE_CODES: Final[frozenset[int]] = frozenset({0, 1, 2, 3})

#: Microseconds per civil day / per minute — the FFI packing units (the
#: kernel's ``DAY_US`` twin; a settle instant is minute-granular by the
#: strict ``HH:MM`` law, which the OK-arm value gate below enforces).
_US_PER_DAY: Final[int] = 86_400_000_000
_US_PER_MINUTE: Final[int] = 60_000_000

#: The gate/settle kernels' result arity — ``(code, payload)``.
_GATE_PAIR_ARITY: Final[int] = 2


def rust_auto_gate_decision(
    *,
    trading_day: bool,
    coverage_available: bool,
    muhurat_listed: bool,
    now_us_of_day: int,
    settle_us_of_day: int,
) -> tuple[int, bool] | None:
    """Consult the Rust ``--auto`` gate ladder; ``None`` = use the oracle.

    Returns ``(reason_code, coverage_warn)`` — the caller maps the code to
    its own verdict builders (every detail string stays single-sourced in
    the calendars module) and fires the coverage WARN itself. Belt +
    refuse-poison gates as everywhere on this facade: EXACTLY a 2-tuple of
    (a non-bool ``int`` inside the reason vocabulary, an exact ``bool``).
    """
    result = _call_kernel(
        "auto_gate_decision",
        trading_day,
        coverage_available,
        muhurat_listed,
        now_us_of_day,
        settle_us_of_day,
    )
    if result is _KERNEL_FAULT:
        return None
    if (
        isinstance(result, tuple)
        and len(result) == _GATE_PAIR_ARITY
        and type(result[0]) is int
        and result[0] in _GATE_CODES
        and type(result[1]) is bool
    ):
        return (result[0], result[1])
    warn_degrade_once("kernel_invalid", symbol="auto_gate_decision", detail=repr(result)[:80])
    return None


def rust_muhurat_window_active(
    now_us_of_day: int,
    open_us_of_day: int,
    close_us_of_day: int,
    pre_margin_us: int,
    post_margin_us: int,
) -> bool | None:
    """Consult the Rust margined Muhurat-window law; ``None`` = the oracle.

    The session LOOKUP (``MUHURAT_SESSIONS``) stays Python — only a LISTED
    session's interval ever crosses. Bool-exact refuse-poison gate.
    """
    verdict = _call_kernel(
        "muhurat_window_active",
        now_us_of_day,
        open_us_of_day,
        close_us_of_day,
        pre_margin_us,
        post_margin_us,
    )
    if verdict is _KERNEL_FAULT:
        return None
    if not isinstance(verdict, bool):
        warn_degrade_once(
            "kernel_invalid", symbol="muhurat_window_active", detail=type(verdict).__name__
        )
        return None
    return verdict


def rust_settle_token_verdict(token: str, block_end_us: int) -> tuple[int, int] | None:
    """Consult the Rust settle-token parse verdict; ``None`` = the oracle.

    *token* must arrive PRE-STRIPPED (``str.strip`` stays Python — its
    whitespace set is CPython's ``isspace`` law, not Unicode
    ``White_Space``). Returns ``(verdict_code, us_of_day_or_-1)``; the seam
    consumes the value ONLY on :data:`SETTLE_CODE_OK`, whose slot is gated
    to a minute-granular in-range instant (the strict ``HH:MM`` law — a
    poisoned off-grid value can only mean a stale/corrupt wheel); every
    non-OK verdict routes the caller back to the Python oracle, which
    composes + raises the byte-identical fail-loud ``ValueError``.
    """
    result = _call_kernel("settle_token_verdict", token, block_end_us)
    if result is _KERNEL_FAULT:
        return None
    if (
        isinstance(result, tuple)
        and len(result) == _GATE_PAIR_ARITY
        and type(result[0]) is int
        and result[0] in _SETTLE_CODES
        and type(result[1]) is int
        and (
            result[0] != SETTLE_CODE_OK
            or (0 <= result[1] < _US_PER_DAY and result[1] % _US_PER_MINUTE == 0)
        )
    ):
        return (result[0], result[1])
    warn_degrade_once("kernel_invalid", symbol="settle_token_verdict", detail=repr(result)[:80])
    return None


__all__ += [
    "GATE_CODE_BEFORE_SETTLE",
    "GATE_CODE_MARKET_HOURS_BLOCKED",
    "GATE_CODE_MUHURAT_DEFERRAL",
    "GATE_CODE_NON_TRADING_DAY",
    "GATE_CODE_OK",
    "SETTLE_CODE_OK",
    "rust_auto_gate_decision",
    "rust_muhurat_window_active",
    "rust_settle_token_verdict",
]
