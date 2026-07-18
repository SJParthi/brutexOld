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
from collections.abc import Callable, Sequence
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


# --- pull_intent (Wave 2E) --------------------------------------------------
# DEC-RUST-PULL-STATE-001 Wave 2E: the ``brutex/dashboard/pull_intent.py``
# parse-shape + resume-decision kernel accessors — the SAME guarded-consult
# discipline as the sections above (lazy resolve via the ONE shared
# ``_resolve_kernel_symbol`` → ``_call_kernel`` defensive belt →
# refuse-poison gate → ``None`` = use the byte-identical Python oracle).
# Append-only section (the wave-worker convention).
#
# BOUNDARY (design risk #3 — the corruption-taxonomy law): the JSON DECODE
# of the intent sidecar STAYS PYTHON.  The seam classifies the
# already-decoded payload's concrete types into the small integer codes
# below (a MECHANICAL ``isinstance``/representability projection in
# ``pull_intent._classify_payload_for_kernel``), and every decode failure —
# ``UnicodeDecodeError`` ⊂ ``ValueError``, ``json.JSONDecodeError``,
# nesting-bomb ``RecursionError``, ``OverflowError`` — folds into the ONE
# not-a-Mapping root code BEFORE the kernel is consulted, so CPython's
# failure taxonomy remains the single decoder and serde/hand-rolled parser
# drift is structurally impossible.  Codes cross in; ONE verdict/arm code
# crosses out; every warn string, structlog event and the frozen
# ``PullIntent`` dataclass stay Python.  The one string edge is
# ``intent_resume_enabled``'s raw env token (the strip+lower+falsy-set law
# IS a string law; a lone-surrogate env value fails the pyo3 ``&str`` edge
# and the belt degrades it to the oracle — verdict-identical, since the
# oracle handles surrogates natively).

#: Field-kind codes for the five request-string fields + ``created_at``
#: (mirrors ``rust/brutex-pull-state/src/intent.rs``).
INTENT_KIND_ABSENT: Final[int] = 0
INTENT_KIND_NULL: Final[int] = 1
INTENT_KIND_STR: Final[int] = 2
INTENT_KIND_OTHER: Final[int] = 3

#: ``restarts``-kind codes.  ``BOOL`` is the concrete CPython type tag
#: (``bool`` subclasses ``int`` — accurate reporting probes it first; the
#: "a JSON ``true`` is not a count" LAW lives in the kernel); the two HUGE
#: codes are pure i64-representability facts for CPython's
#: arbitrary-precision ints (the sign law still lives in the kernel).
INTENT_RESTARTS_ABSENT: Final[int] = 0
INTENT_RESTARTS_NULL: Final[int] = 1
INTENT_RESTARTS_BOOL: Final[int] = 2
INTENT_RESTARTS_INT: Final[int] = 3
INTENT_RESTARTS_INT_HUGE_POS: Final[int] = 4
INTENT_RESTARTS_INT_HUGE_NEG: Final[int] = 5
INTENT_RESTARTS_OTHER: Final[int] = 6

#: ``intent_shape_verdict``'s VALID verdict — every other in-vocabulary code
#: (1..=8, the first violation in the oracle's own probe order) means the
#: corrupt/absent answer; 255 (out-of-vocabulary inputs) is NOT in the
#: served vocabulary, so it degrades as ``kernel_invalid`` below.
INTENT_SHAPE_VALID: Final[int] = 0

#: The whole served shape-verdict vocabulary (0=valid · 1=not-mapping ·
#: 2..=6 the five request fields in probe order · 7=restarts ·
#: 8=created_at) — anything else is a stale/corrupt wheel.
_INTENT_SHAPE_CODES: Final[frozenset[int]] = frozenset(range(9))

#: ``intent_resume_decision`` arm tokens (index == token value).
RESUME_ARM_NO_INTENT: Final[int] = 0
RESUME_ARM_PEER_ACTIVE: Final[int] = 1
RESUME_ARM_OVER_CAP: Final[int] = 2
RESUME_ARM_PROCEED: Final[int] = 3

_RESUME_ARMS: Final[frozenset[int]] = frozenset(range(RESUME_ARM_PROCEED + 1))

#: ``intent_refused_restore_decision`` arm tokens.
RESTORE_ARM_NO_ACTION: Final[int] = 0
RESTORE_ARM_LEAVE_ABSENT: Final[int] = 1
RESTORE_ARM_CLEAR_ORPHAN: Final[int] = 2
RESTORE_ARM_RESTORE_PRIOR: Final[int] = 3

_RESTORE_ARMS: Final[frozenset[int]] = frozenset(range(RESTORE_ARM_RESTORE_PRIOR + 1))

#: The u64 FFI ceiling for ``intent_resume_decision``'s counters — a count
#: outside ``[0, 2**64)`` (a forged intent file's arbitrary-precision int)
#: stays on the DOCUMENTED Python arm (a representability fact, not a
#: kernel fault — no warn; the oracle decides byte-identically).
_U64_MAX: Final[int] = 2**64 - 1


def rust_intent_shape_verdict(
    root_mapping: int,
    symbols: int,
    symbol: int,
    from_date: int,
    to_date: int,
    scope: int,
    restarts_kind: int,
    restarts_value: int,
    created_at_kind: int,
) -> int | None:
    """Consult the kernel's intent shape verdict; ``None`` = use the oracle.

    Takes the 9-slot classified-payload tuple UNPACKED (the
    ``pull_intent._classify_payload_for_kernel`` output — ``root_mapping``
    rides as the classifier's 0/1 int and is boolified here).  Refuse-poison
    gate: EXACTLY ``int`` and EXACTLY one of the nine served verdict codes —
    the kernel's own 255 out-of-vocabulary-input answer (a classifier/wheel
    mismatch) degrades as ``kernel_invalid``, never a verdict.
    """
    verdict = _call_kernel(
        "intent_shape_verdict",
        bool(root_mapping),
        symbols,
        symbol,
        from_date,
        to_date,
        scope,
        restarts_kind,
        restarts_value,
        created_at_kind,
    )
    if verdict is _KERNEL_FAULT:
        return None
    if type(verdict) is not int or verdict not in _INTENT_SHAPE_CODES:
        warn_degrade_once(
            "kernel_invalid", symbol="intent_shape_verdict", detail=repr(verdict)[:80]
        )
        return None
    return verdict


def rust_intent_resume_decision(
    *,
    has_intent: bool,
    probe_acquirable: bool,
    restarts: int,
    max_restarts: int,
) -> int | None:
    """Consult the kernel's resume arm token; ``None`` = use the oracle.

    Counters outside u64 keep the call on the documented Python arm
    (silently — a representability fact, never a kernel fault).  Beyond the
    type + vocabulary refuse-poison gate, the ONE site-impossible arm is
    gated too: ``NO_INTENT`` is total in ``has_intent`` (the kernel lattice
    returns it iff the intent is absent), so a mismatch there can only mean
    a stale/corrupt wheel — degrade, never serve.
    """
    if not (0 <= restarts <= _U64_MAX and 0 <= max_restarts <= _U64_MAX):
        return None
    verdict = _call_kernel(
        "intent_resume_decision",
        bool(has_intent),
        bool(probe_acquirable),
        restarts,
        max_restarts,
    )
    if verdict is _KERNEL_FAULT:
        return None
    if type(verdict) is not int or verdict not in _RESUME_ARMS:
        warn_degrade_once(
            "kernel_invalid", symbol="intent_resume_decision", detail=repr(verdict)[:80]
        )
        return None
    if (verdict == RESUME_ARM_NO_INTENT) != (not has_intent):
        warn_degrade_once(
            "kernel_invalid",
            symbol="intent_resume_decision",
            detail=f"no_intent arm inconsistent with has_intent={has_intent}",
        )
        return None
    return verdict


def rust_intent_refused_restore_decision(
    *,
    dry_run: bool,
    started: bool,
    current_present: bool,
    prior_present: bool,
) -> int | None:
    """Consult the kernel's refused-start restore arm; ``None`` = the oracle.

    The ``NO_ACTION`` arm is total in ``dry_run or started`` — the
    consistency gate degrades a wheel that breaks that totality (the
    resume-decision posture), so the seam can never be steered into
    skipping a restore (or performing one) on a short-circuit input.
    """
    verdict = _call_kernel(
        "intent_refused_restore_decision",
        bool(dry_run),
        bool(started),
        bool(current_present),
        bool(prior_present),
    )
    if verdict is _KERNEL_FAULT:
        return None
    if type(verdict) is not int or verdict not in _RESTORE_ARMS:
        warn_degrade_once(
            "kernel_invalid",
            symbol="intent_refused_restore_decision",
            detail=repr(verdict)[:80],
        )
        return None
    if (verdict == RESTORE_ARM_NO_ACTION) != (dry_run or started):
        warn_degrade_once(
            "kernel_invalid",
            symbol="intent_refused_restore_decision",
            detail=f"no_action arm inconsistent with dry_run={dry_run} started={started}",
        )
        return None
    return verdict


def rust_intent_resume_enabled(raw: str | None) -> bool | None:
    """Consult the kernel's resume-env token law; ``None`` = use the oracle.

    Refuse-poison gate: EXACTLY ``bool`` (an int pun from a stale wheel must
    degrade loudly, never serve as truthy).  A lone-surrogate *raw* fails
    the pyo3 ``&str`` edge inside the belt (``kernel_error``) — the oracle
    handles surrogates natively, so the verdict is unaffected.
    """
    verdict = _call_kernel("intent_resume_enabled", raw)
    if verdict is _KERNEL_FAULT:
        return None
    if not isinstance(verdict, bool):
        warn_degrade_once(
            "kernel_invalid", symbol="intent_resume_enabled", detail=type(verdict).__name__
        )
        return None
    return verdict


__all__ += [
    "INTENT_KIND_ABSENT",
    "INTENT_KIND_NULL",
    "INTENT_KIND_OTHER",
    "INTENT_KIND_STR",
    "INTENT_RESTARTS_ABSENT",
    "INTENT_RESTARTS_BOOL",
    "INTENT_RESTARTS_INT",
    "INTENT_RESTARTS_INT_HUGE_NEG",
    "INTENT_RESTARTS_INT_HUGE_POS",
    "INTENT_RESTARTS_NULL",
    "INTENT_RESTARTS_OTHER",
    "INTENT_SHAPE_VALID",
    "RESTORE_ARM_CLEAR_ORPHAN",
    "RESTORE_ARM_LEAVE_ABSENT",
    "RESTORE_ARM_NO_ACTION",
    "RESTORE_ARM_RESTORE_PRIOR",
    "RESUME_ARM_NO_INTENT",
    "RESUME_ARM_OVER_CAP",
    "RESUME_ARM_PEER_ACTIVE",
    "RESUME_ARM_PROCEED",
    "rust_intent_refused_restore_decision",
    "rust_intent_resume_decision",
    "rust_intent_resume_enabled",
    "rust_intent_shape_verdict",
]


# --- pull_progress (Wave 2F) -------------------------------------------------
# The derived-metrics plane of ``brutex/dashboard/pull_progress.py``
# (DEC-PULL-PROGRESS-HONESTY-001 — the ``ProgressSnapshot`` ``percent`` /
# ``elapsed_seconds`` / ``units_per_sec`` / ``eta_seconds`` properties). ONE
# consult serves all four metrics; the counter state machine, every string,
# ``to_dict`` and the injected clock stay Python (the pyclass flip is a
# LATER lane phase). Two degrade tiers:
#
# * the SILENT domain gate (:func:`_progress_inputs_in_domain`) — a snapshot
#   whose counts leave the spike-proven ``[0, 2**53]`` exactness envelope,
#   or whose instants are not genuine floats, is a LEGAL Python construction
#   the kernel deliberately does not model: the oracle serves with NO
#   warn (not an error — a documented domain boundary, unreachable from the
#   production tracker whose counters are clamped non-negative ints);
# * the shared belt/refuse-poison tiers (``wheel_missing`` / ``wheel_stale``
#   / ``kernel_error`` / ``kernel_invalid``) — warn-once, oracle serves.

#: The kernel count-domain ceiling (inclusive): both division operands stay
#: exactly representable in f64, so the kernel's division equals CPython's
#: correctly-rounded ``int/int`` true division (the Wave-2F spike law).
PROGRESS_COUNT_MAX: Final[int] = 2**53

#: The derived bundle's kernel arity —
#: ``(percent, elapsed, units_per_sec, eta_present, eta_seconds)``.
_PROGRESS_TUPLE_ARITY: Final[int] = 5


def _progress_count_in_domain(value: int) -> bool:
    """EXACTLY ``int`` (a ``bool`` pun degrades) inside ``[0, 2**53]``."""
    return type(value) is int and 0 <= value <= PROGRESS_COUNT_MAX


def _progress_inputs_in_domain(
    open_ended: bool,
    total: int,
    completed: int,
    phase_base_completed: int,
    updated_at: float,
    started_at: float,
    phase_started_at: float,
) -> bool:
    """The SILENT eligibility gate for :func:`rust_progress_derived`.

    Counts must be genuine ints in the exactness domain; instants must be
    genuine ``float`` instances (a hostile int-typed instant would take
    CPython's exact ``int`` arithmetic through the properties — a semantics
    the f64 kernel deliberately does not model); ``open_ended`` must be a
    genuine ``bool`` (the oracle consumes its TRUTHINESS — a truthy pun
    stays on the oracle rather than raising at the strict pyo3 edge).
    NaN/inf FLOATS are IN domain — the kernel mirrors the oracle's IEEE
    comparisons exactly.
    """
    return (
        type(open_ended) is bool
        and _progress_count_in_domain(total)
        and _progress_count_in_domain(completed)
        and _progress_count_in_domain(phase_base_completed)
        and type(updated_at) is float
        and type(started_at) is float
        and type(phase_started_at) is float
    )


def _validated_progress_tuple(
    result: object,
) -> tuple[float, float, float, float | None] | None:
    """The refuse-poison gate + the ``eta_present`` fold.

    EXACTLY a 5-tuple ``(float, float, float, bool, float)`` (bool puns in
    the float slots and int puns in the bool slot each degrade — only a
    stale/corrupt wheel produces them); folds to the facade quadruple
    ``(percent, elapsed, units_per_sec, eta_or_None)``.
    """
    if (
        isinstance(result, tuple)
        and len(result) == _PROGRESS_TUPLE_ARITY
        and type(result[0]) is float
        and type(result[1]) is float
        and type(result[2]) is float
        and type(result[3]) is bool
        and type(result[4]) is float
    ):
        return (result[0], result[1], result[2], result[4] if result[3] else None)
    warn_degrade_once("kernel_invalid", symbol="progress_derived", detail=type(result).__name__)
    return None


def rust_progress_derived(
    *,
    state_running: bool,
    open_ended: bool,
    total: int,
    completed: int,
    phase_base_completed: int,
    updated_at: float,
    started_at: float,
    phase_started_at: float,
) -> tuple[float, float, float, float | None] | None:
    """Consult the derived-metrics kernel; ``None`` = use the oracle.

    Returns ``(percent, elapsed_seconds, units_per_sec, eta_seconds)`` —
    each bit-identical to the corresponding ``ProgressSnapshot`` property
    (``eta_seconds`` already folded to the oracle's ``float | None``). The
    caller passes ``state_running`` as the PRE-COMPUTED ``state ==
    STATE_RUNNING`` verdict (string comparison stays Python) and
    ``open_ended`` as a genuine ``bool``.
    """
    if not _progress_inputs_in_domain(
        open_ended, total, completed, phase_base_completed, updated_at, started_at, phase_started_at
    ):
        return None  # the SILENT domain gate — a legal input, not a fault
    result = _call_kernel(
        "progress_derived",
        state_running,
        open_ended,
        total,
        completed,
        phase_base_completed,
        updated_at,
        started_at,
        phase_started_at,
    )
    if result is _KERNEL_FAULT:
        return None
    return _validated_progress_tuple(result)


__all__ += [
    "PROGRESS_COUNT_MAX",
    "rust_progress_derived",
]


# --- auto_pull_scheduler (Wave 3G) -------------------------------------------
# DEC-RUST-PULL-STATE-001 Wave 3G: the ``brutex/dashboard/auto_pull_scheduler``
# tick-decision + durable-state-parse kernel accessors — the SAME
# guarded-consult discipline as the sections above (lazy resolve via the ONE
# shared ``_resolve_kernel_symbol`` → ``_call_kernel`` defensive belt →
# refuse-poison gate → ``None`` = use the byte-identical Python oracle).
# Append-only section (the wave-worker convention).
#
# BOUNDARY: the ticker thread, the corruption-hardened store I/O
# (``read_sidecar_bytes`` + the corrupt ⇒ DISARMED + one WARN posture), the
# per-(day, reason) log-dedup memo, every log/status string and the
# fail-LOUD ``BRUTEX_AUTO_PULL_TICK_S`` resolver STAY PYTHON.  The JSON
# decode of the state sidecar stays Python too (the Wave-2E taxonomy law) —
# every decode failure folds BEFORE any kernel consult.  The seam's
# two-stage tick consult (pre-gate placeholders, then the post-gate real
# facts) preserves the oracle's LAZY gate/launcher evaluation order — the
# ladder's own arm ORDER makes the placeholder stage exact (pinned by the
# exhaustive lattice golden + the crate's placeholder-leak test).

#: ``auto_tick_decision`` ladder tokens (mirrors
#: ``rust/brutex-pull-state/src/auto_pull_scheduler.rs`` — index == value).
TICK_CODE_DISARMED: Final[int] = 0
TICK_CODE_ALREADY_SUCCEEDED: Final[int] = 1
TICK_CODE_ALREADY_ATTEMPTED: Final[int] = 2
TICK_CODE_DEFERRED: Final[int] = 3
TICK_CODE_LAUNCHER_UNBOUND: Final[int] = 4
TICK_CODE_FIRE: Final[int] = 5

#: Every legal ``auto_tick_decision`` result — the refuse-poison vocabulary.
_TICK_CODES: Final[frozenset[int]] = frozenset(range(TICK_CODE_FIRE + 1))

#: ``schema``-kind classification codes for ``auto_state_verdict`` (the
#: value law — CPython's NUMERIC ``schema == 1`` across bool/int/float —
#: lives in the kernel; these are mechanical type tags).
SCHEMA_KIND_MISSING: Final[int] = 0
SCHEMA_KIND_BOOL: Final[int] = 1
SCHEMA_KIND_INT: Final[int] = 2
SCHEMA_KIND_INT_HUGE: Final[int] = 3
SCHEMA_KIND_FLOAT: Final[int] = 4
SCHEMA_KIND_OTHER: Final[int] = 5

#: Optional-string field-kind codes — the ONE Wave-2E vocabulary, derived
#: (never re-minted): absent ≡ null load ``None``; a string loads itself;
#: anything else is the corruption the kernel refuses.
STATE_FIELD_ABSENT: Final[int] = INTENT_KIND_ABSENT
STATE_FIELD_NULL: Final[int] = INTENT_KIND_NULL
STATE_FIELD_STR: Final[int] = INTENT_KIND_STR
STATE_FIELD_OTHER: Final[int] = INTENT_KIND_OTHER

#: ``auto_state_verdict`` codes (0=valid · 1=not-object · 2=schema ·
#: 3=armed · 4..=6 the three optional strings in constructor-arg order).
STATE_CODE_VALID: Final[int] = 0
STATE_CODE_NOT_OBJECT: Final[int] = 1
STATE_CODE_BAD_SCHEMA: Final[int] = 2
STATE_CODE_ARMED_NOT_BOOL: Final[int] = 3
STATE_CODE_BAD_ARMED_AT: Final[int] = 4
STATE_CODE_BAD_LAST_ATTEMPT: Final[int] = 5
STATE_CODE_BAD_LAST_SUCCESS: Final[int] = 6

#: The whole served state-verdict vocabulary — 255 (out-of-vocabulary
#: inputs) is NOT in it, so a classifier/wheel mismatch degrades as
#: ``kernel_invalid`` and the oracle decides.
_STATE_CODES: Final[frozenset[int]] = frozenset(range(STATE_CODE_BAD_LAST_SUCCESS + 1))


def rust_auto_tick_decision(
    *,
    armed: bool,
    succeeded_today: bool,
    attempted_today: bool,
    gate_allowed: bool,
    launcher_bound: bool,
) -> int | None:
    """Consult the kernel's tick-decision ladder; ``None`` = use the oracle.

    The caller (``AutoPullScheduler``'s seam) resolves every fact —
    ``succeeded_today``/``attempted_today`` are the Python-side ISO-date
    comparisons (dates never cross the FFI) — and exploits the ladder's arm
    ORDER for its two-stage consult (pre-gate placeholders first; the
    kernel can only answer a state arm or DEFERRED for them).  Refuse-poison
    gate: EXACTLY ``int`` (a ``bool`` pun degrades) inside the six-token
    vocabulary.
    """
    verdict = _call_kernel(
        "auto_tick_decision",
        armed,
        succeeded_today,
        attempted_today,
        gate_allowed,
        launcher_bound,
    )
    if verdict is _KERNEL_FAULT:
        return None
    if type(verdict) is not int or verdict not in _TICK_CODES:
        warn_degrade_once("kernel_invalid", symbol="auto_tick_decision", detail=repr(verdict)[:80])
        return None
    return verdict


def rust_auto_state_verdict(
    *,
    root_mapping: bool,
    schema_kind: int,
    schema_i64: int,
    schema_f64_bits: int,
    armed_is_bool: bool,
    armed_at_kind: int,
    last_attempt_kind: int,
    last_success_kind: int,
) -> int | None:
    """Consult the kernel's state-shape verdict; ``None`` = use the oracle.

    Codes in (the seam's mechanical classification of the already-decoded
    payload), ONE verdict code out; every refusal MESSAGE stays Python
    (``_state_from_payload`` composes the byte-identical ``ValueError``
    text per code).  Refuse-poison gate: EXACTLY ``int`` inside the
    seven-code served vocabulary (the kernel's 255 input-mismatch answer
    deliberately degrades here — the oracle decides).
    """
    verdict = _call_kernel(
        "auto_state_verdict",
        root_mapping,
        schema_kind,
        schema_i64,
        schema_f64_bits,
        armed_is_bool,
        armed_at_kind,
        last_attempt_kind,
        last_success_kind,
    )
    if verdict is _KERNEL_FAULT:
        return None
    if type(verdict) is not int or verdict not in _STATE_CODES:
        warn_degrade_once("kernel_invalid", symbol="auto_state_verdict", detail=repr(verdict)[:80])
        return None
    return verdict


__all__ += [
    "SCHEMA_KIND_BOOL",
    "SCHEMA_KIND_FLOAT",
    "SCHEMA_KIND_INT",
    "SCHEMA_KIND_INT_HUGE",
    "SCHEMA_KIND_MISSING",
    "SCHEMA_KIND_OTHER",
    "STATE_CODE_ARMED_NOT_BOOL",
    "STATE_CODE_BAD_ARMED_AT",
    "STATE_CODE_BAD_LAST_ATTEMPT",
    "STATE_CODE_BAD_LAST_SUCCESS",
    "STATE_CODE_BAD_SCHEMA",
    "STATE_CODE_NOT_OBJECT",
    "STATE_CODE_VALID",
    "STATE_FIELD_ABSENT",
    "STATE_FIELD_NULL",
    "STATE_FIELD_OTHER",
    "STATE_FIELD_STR",
    "TICK_CODE_ALREADY_ATTEMPTED",
    "TICK_CODE_ALREADY_SUCCEEDED",
    "TICK_CODE_DEFERRED",
    "TICK_CODE_DISARMED",
    "TICK_CODE_FIRE",
    "TICK_CODE_LAUNCHER_UNBOUND",
    "rust_auto_state_verdict",
    "rust_auto_tick_decision",
]


# --- pull_census (Wave 3G) ---------------------------------------------------
# DEC-RUST-PULL-STATE-001 Wave 3G: the ``brutex/dashboard/pull_census``
# fold accessor — the SAME guarded-consult discipline (``_call_kernel``
# defensive belt → refuse-poison gate → ``None`` = the byte-identical
# Python oracle).  Append-only section (the wave-worker convention).
#
# BOUNDARY: the seam pre-extracts + scope-filters the ledger rows / lake
# spans (the ``str()``/``int()`` conversions over arbitrary payload objects
# and the lazy-``int()``-after-the-scope-filter law stay Python) and renders
# the ``repr()`` sample strings from the returned triples; the kernel folds
# sets, sorts (UTF-8 byte order ≡ code-point order — valid UTF-8 only; a
# lone-surrogate symbol fails the pyo3 edge and the belt degrades) and caps.

#: Sample-kind codes (mirrors ``rust/brutex-pull-state/src/pull_census.rs``).
SAMPLE_KIND_SETTLED_UNWRITTEN: Final[int] = 0
SAMPLE_KIND_WRITTEN_UNSETTLED: Final[int] = 1

_SAMPLE_KINDS: Final[frozenset[int]] = frozenset(
    {SAMPLE_KIND_SETTLED_UNWRITTEN, SAMPLE_KIND_WRITTEN_UNSETTLED}
)

#: The kernel's sample cap — sync-pinned against the Python
#: ``pull_census.CENSUS_SAMPLE_CAP`` by the seam suite; a longer sample list
#: can only mean a stale/corrupt wheel (refused below).
_CENSUS_SAMPLE_CAP: Final[int] = 20

#: The census result arity — ``(settled_unwritten, written_unsettled, samples)``.
_CENSUS_TRIPLE_ARITY: Final[int] = 3


def _census_count_ok(value: object) -> bool:
    """EXACTLY a non-negative ``int`` (a ``bool`` pun degrades)."""
    return type(value) is int and value >= 0


def rust_census_fold(
    rows: Sequence[tuple[str, str, bool]],
    written_pairs: Sequence[tuple[str, str]],
) -> tuple[int, int, tuple[tuple[int, str, str], ...]] | None:
    """Consult the kernel's census fold; ``None`` = use the oracle.

    Refuse-poison gate: EXACTLY a 3-tuple of two non-negative ints + a
    ≤-cap sequence of ``(kind, symbol, interval)`` triples with kind in the
    two-token vocabulary — anything else is a stale/corrupt wheel.
    """
    result = _call_kernel("census_fold", list(rows), list(written_pairs))
    if result is _KERNEL_FAULT:
        return None
    if not isinstance(result, tuple) or len(result) != _CENSUS_TRIPLE_ARITY:
        warn_degrade_once("kernel_invalid", symbol="census_fold", detail=type(result).__name__)
        return None
    settled_unwritten, written_unsettled, samples = result
    if (
        not _census_count_ok(settled_unwritten)
        or not _census_count_ok(written_unsettled)
        or not isinstance(samples, (list, tuple))
        or len(samples) > _CENSUS_SAMPLE_CAP
    ):
        warn_degrade_once("kernel_invalid", symbol="census_fold", detail=repr(result)[:80])
        return None
    validated: list[tuple[int, str, str]] = []
    for item in samples:
        if (
            not isinstance(item, tuple)
            or len(item) != _CENSUS_TRIPLE_ARITY
            or type(item[0]) is not int
            or item[0] not in _SAMPLE_KINDS
            or type(item[1]) is not str
            or type(item[2]) is not str
        ):
            warn_degrade_once("kernel_invalid", symbol="census_fold", detail=repr(item)[:80])
            return None
        validated.append((item[0], item[1], item[2]))
    return (settled_unwritten, written_unsettled, tuple(validated))


__all__ += [
    "SAMPLE_KIND_SETTLED_UNWRITTEN",
    "SAMPLE_KIND_WRITTEN_UNSETTLED",
    "rust_census_fold",
]


# --- auto_pull (Wave 3G) -----------------------------------------------------
# DEC-RUST-PULL-STATE-001 Wave 3G: the ``brutex/dashboard/auto_pull``
# reload-predicate accessor — the SAME guarded-consult discipline
# (``_call_kernel`` defensive belt → bool-exact refuse-poison gate →
# ``None`` = the byte-identical Python oracle).  Append-only section.

#: The i64 FFI band for the two counts — an out-of-band CPython int stays
#: on the DOCUMENTED Python arm (a representability fact, not a kernel
#: fault — no warn; the intent ``_U64_MAX`` precedent).
_I64_MIN: Final[int] = -(2**63)
_I64_MAX: Final[int] = 2**63 - 1


def rust_should_reload_master(
    *,
    index_count: int,
    stock_count: int,
    legs_present: bool,
    stale_economics: bool,
    stale_eligibility: bool,
) -> bool | None:
    """Consult the kernel's reload predicate; ``None`` = use the oracle.

    The representability gate keeps an out-of-i64 count on the Python arm
    SILENTLY; the refuse-poison gate is bool-exact.
    """
    if not (_I64_MIN <= index_count <= _I64_MAX and _I64_MIN <= stock_count <= _I64_MAX):
        return None
    verdict = _call_kernel(
        "should_reload_master",
        index_count,
        stock_count,
        legs_present,
        stale_economics,
        stale_eligibility,
    )
    if verdict is _KERNEL_FAULT:
        return None
    if not isinstance(verdict, bool):
        warn_degrade_once(
            "kernel_invalid", symbol="should_reload_master", detail=type(verdict).__name__
        )
        return None
    return verdict


__all__ += [
    "rust_should_reload_master",
]


# --- rate_bridge + rate_bridge_cache (Wave 3H) -------------------------------
# DEC-RUST-PULL-STATE-001 Wave 3H: the ``brutex/data/rate_bridge.py``
# payload-verdict kernel + the ``brutex/dashboard/rate_bridge_cache.py``
# memo decision kernels — the SAME guarded-consult discipline as the
# sections above (lazy resolve via the ONE shared ``_resolve_kernel_symbol``
# → ``_call_kernel`` defensive belt → refuse-poison gate → ``None`` = use
# the byte-identical Python oracle).  Append-only section (the wave-worker
# convention).
#
# BOUNDARY (the Wave-2E decode-stays-Python precedent): the bridge-file
# read, the JSON decode (CPython's WHOLE failure taxonomy) and the
# ``float()``/``int()`` coercion PROBES stay Python — the seam classifies
# the already-decoded payload's concrete types / coercibility FACTS into
# the small integer codes below (a MECHANICAL projection in
# ``rate_bridge._classify_bridge_payload_for_kernel``); a payload whose
# coercion probe raises ``OverflowError`` (the documented pre-existing
# raises envelope — a 400-digit int in a float field, ``Infinity`` in an
# int field) NEVER consults the kernel (the classifier abstains to the
# Python arm, preserving the oracle's raise byte-for-byte).  For the cache,
# the caller computes the age from ONE monotonic-clock read (clocks never
# cross the FFI) and the thread core — lock, deadline guard, ``_inflight``
# clearing law, degrade latch — is PERMANENT-PYTHON (the design §2.3
# ruling); the seam's own fail-safe belt FORCES the follower arm while
# ``_inflight`` is set, so a poisoned kernel can only ever degrade, never
# spawn a second refresh.

#: ``bridge_payload_verdict``'s VALID verdict — the seam falls through to
#: the oracle's construction; every other in-vocabulary code (1..=12, the
#: first violation in the oracle's probe order) means the ``None`` answer.
BRIDGE_VERDICT_VALID: Final[int] = 0

#: The whole served bridge-verdict vocabulary (0=valid · 1=not-mapping ·
#: 2=schema · 3..=12 the ten required fields in constructor probe order) —
#: anything else (incl. the kernel's own 255 bad-input answer, a
#: classifier/wheel mismatch) is a stale/corrupt wheel and degrades.
_BRIDGE_VERDICT_CODES: Final[frozenset[int]] = frozenset(range(13))

#: ``schema``-kind codes (mirrors ``rust/brutex-pull-state/src/rate_bridge.rs``
#: — ``bool`` probed before ``int`` because it subclasses ``int``; the
#: ``== 3`` EQUALITY law itself lives in the kernel).
BRIDGE_SCHEMA_MISSING: Final[int] = 0
BRIDGE_SCHEMA_BOOL: Final[int] = 1
BRIDGE_SCHEMA_INT: Final[int] = 2
BRIDGE_SCHEMA_INT_HUGE: Final[int] = 3
BRIDGE_SCHEMA_FLOAT: Final[int] = 4
BRIDGE_SCHEMA_OTHER: Final[int] = 5

#: Required-field coercibility codes (CPython-PROBED facts — the taxonomy
#: stays Python; the all-ten-must-coerce LAW lives in the kernel).
BRIDGE_FIELD_ABSENT: Final[int] = 0
BRIDGE_FIELD_OK: Final[int] = 1
BRIDGE_FIELD_BAD: Final[int] = 2

#: ``pull_active`` arm codes (the kernel echoes the bool-or-None
#: normalisation arm so the cross-language golden pins the law).
BRIDGE_PA_NONE: Final[int] = 0
BRIDGE_PA_FALSE: Final[int] = 1
BRIDGE_PA_TRUE: Final[int] = 2

_BRIDGE_PA_CODES: Final[frozenset[int]] = frozenset(range(BRIDGE_PA_TRUE + 1))

#: The bridge verdict's kernel arity — ``(verdict, pull_active_arm)``.
_BRIDGE_PAIR_ARITY: Final[int] = 2


def rust_bridge_payload_verdict(
    root_mapping: int,
    schema_kind: int,
    schema_int: int,
    schema_float: float,
    field_kinds: list[int],
    pa_kind: int,
) -> tuple[int, int] | None:
    """Consult the kernel's bridge payload verdict; ``None`` = use the oracle.

    Takes the classified-payload tuple UNPACKED (the
    ``rate_bridge._classify_bridge_payload_for_kernel`` output —
    ``root_mapping`` rides as the classifier's 0/1 int and is boolified
    here).  Refuse-poison gate: EXACTLY a 2-tuple of EXACTLY-``int`` codes
    inside the served vocabularies — the kernel's own 255
    out-of-vocabulary-input answer (a classifier/wheel mismatch) degrades
    as ``kernel_invalid``, never a verdict.
    """
    result = _call_kernel(
        "bridge_payload_verdict",
        bool(root_mapping),
        schema_kind,
        schema_int,
        schema_float,
        field_kinds,
        pa_kind,
    )
    if result is _KERNEL_FAULT:
        return None
    if (
        isinstance(result, tuple)
        and len(result) == _BRIDGE_PAIR_ARITY
        and type(result[0]) is int
        and type(result[1]) is int
        and result[0] in _BRIDGE_VERDICT_CODES
        and result[1] in _BRIDGE_PA_CODES
    ):
        return (result[0], result[1])
    warn_degrade_once("kernel_invalid", symbol="bridge_payload_verdict", detail=repr(result)[:80])
    return None


#: The memo serve-arm vocabulary (mirrors
#: ``rust/brutex-pull-state/src/rate_bridge_cache.rs`` — index == token).
MEMO_ARM_CACHED_HIT: Final[int] = 0
MEMO_ARM_FOLLOWER: Final[int] = 1
MEMO_ARM_OWNER: Final[int] = 2

_MEMO_ARMS: Final[frozenset[int]] = frozenset(range(MEMO_ARM_OWNER + 1))


def rust_memo_serve_arm(age_s: float | None, ttl_s: float, inflight: bool) -> int | None:
    """Consult the three-arm memo serve kernel; ``None`` = use the oracle.

    ``age_s`` is the caller's PRE-COMPUTED clamped monotonic delta
    (``None`` = never probed); the caller maps the arm to its ACTIONS
    verbatim in Python — under its own fail-safe belt (an OWNER arm while
    ``_inflight`` is set is FORCED to follower, so a poisoned kernel can
    never break the single-flight law).  Belt + int-exact vocabulary gate
    as everywhere on this facade.
    """
    token = _call_kernel("memo_serve_arm", age_s, ttl_s, inflight)
    if token is _KERNEL_FAULT:
        return None
    if type(token) is not int or token not in _MEMO_ARMS:
        warn_degrade_once("kernel_invalid", symbol="memo_serve_arm", detail=repr(token)[:80])
        return None
    return token


def rust_token_revalidates(token: str | None, prior: str | None) -> bool | None:
    """Consult the token-revalidation kernel; ``None`` = use the oracle.

    The ``RateBridgeCache._refresh_body`` law — ``True`` = keep the memo,
    skip the read; ``False`` = re-read.  A lone-surrogate token fails the
    pyo3 ``&str`` edge inside the belt and degrades to the oracle
    (verdict-identical — the oracle handles surrogates natively).
    """
    verdict = _call_kernel("token_revalidates", token, prior)
    if verdict is _KERNEL_FAULT:
        return None
    if not isinstance(verdict, bool):
        warn_degrade_once("kernel_invalid", symbol="token_revalidates", detail=repr(verdict)[:80])
        return None
    return verdict


__all__ += [
    "BRIDGE_FIELD_ABSENT",
    "BRIDGE_FIELD_BAD",
    "BRIDGE_FIELD_OK",
    "BRIDGE_PA_FALSE",
    "BRIDGE_PA_NONE",
    "BRIDGE_PA_TRUE",
    "BRIDGE_SCHEMA_BOOL",
    "BRIDGE_SCHEMA_FLOAT",
    "BRIDGE_SCHEMA_INT",
    "BRIDGE_SCHEMA_INT_HUGE",
    "BRIDGE_SCHEMA_MISSING",
    "BRIDGE_SCHEMA_OTHER",
    "BRIDGE_VERDICT_VALID",
    "MEMO_ARM_CACHED_HIT",
    "MEMO_ARM_FOLLOWER",
    "MEMO_ARM_OWNER",
    "rust_bridge_payload_verdict",
    "rust_memo_serve_arm",
    "rust_token_revalidates",
]
