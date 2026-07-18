"""The Rust pull-state kernel facade — THE decision plane, fail-closed
(DEC-RUST-PULL-STATE-001, flip phase).

The ``brutex_pull_state`` Rust wheel IS the pull-state decision plane: the
market-hours guard predicate, the pull-stall wedge/alive laws, the last-pull
outcome codes, the pull-scope note surgery, the ``--auto`` gate ladder, the
intent parse/resume decisions, the progress-derived metrics, the auto-pull
scheduler ladder, the census fold, the master-reload predicate and the
rate-bridge/memo decision laws. The Python oracles that once mirrored these
laws are DELETED — there is no fallback arm, no opt-in flag, no silent
degrade. Every ``rust_*`` accessor either serves the kernel's VALIDATED
result or raises the typed :class:`PullStateKernelError` — FAIL-CLOSED by
design: for the guard chain protecting the shared-TOTP Groww account
(DEC-GROWW-MARKET-HOURS-GUARD-001) a raise means a pull is REFUSED, never
silently allowed.

Error surface:

* :class:`PullStateFlagRemovedError` — the retired ``BRUTEX_PULL_STATE_RUST``
  opt-in knob is still SET (ANY non-empty value, truthy spellings included).
  Raised by :func:`require_pull_state_env_unset`, which every kernel resolve
  checks first — the DEC-FILL-WORST-CASE-ONLY-001 require_fill_mode_env_unset
  precedent: a stale deployment config must fail LOUD, never read as "still
  honoured".
* :class:`PullStateKernelError` — the wheel is absent (``wheel_missing``) or
  predates a symbol (``wheel_stale``), the kernel raised (``kernel_error``),
  or it served a result the refuse-poison / domain gates refuse
  (``kernel_invalid``). The validation LOGIC of the pre-flip facade is
  preserved byte-for-byte (bool-exact, int-not-bool, vocabulary sets, tuple
  arities, domain checks) — only the disposition changed: refuse-and-raise
  instead of degrade-to-oracle.

ONE resolver (:func:`_resolve_kernel_symbol`) + ONE guarded chokepoint
(:func:`_call_kernel`) serve every accessor. ``KeyboardInterrupt`` /
``SystemExit`` / ``MemoryError`` always propagate RAW.

LEAF DISCIPLINE: imported by ``brutex.calendars.market_hours`` — the
dependency-light guard BOTH ingest spines consult — so this module imports
ONLY stdlib (never ``brutex.engine`` / ``brutex.dashboard`` /
``brutex.data`` / ``brutex.feed`` / ``brutex.calendars``).
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Final, cast

#: The RETIRED opt-in environment flag — kept ONLY so the refusal guard
#: (:func:`require_pull_state_env_unset`) can name the stale variable it
#: refuses. Nothing resolves an execution arm from it any more.
PULL_STATE_RUST_ENV: Final[str] = "BRUTEX_PULL_STATE_RUST"


class PullStateFlagRemovedError(ValueError):
    """A launch found the removed ``BRUTEX_PULL_STATE_RUST`` knob still SET.

    DEC-RUST-PULL-STATE-001 (flip phase): the Rust pull-state kernel is no
    longer optional — it is THE decision plane, so the opt-in knob is
    removed. Raised (never warned) for ANY non-empty value — truthy
    spellings included — so a stale deployment config fails LOUD instead of
    silently pretending the knob is honoured (the
    DEC-FILL-WORST-CASE-ONLY-001 require_fill_mode_env_unset precedent).
    """


class PullStateKernelError(RuntimeError):
    """The pull-state Rust kernel is unavailable, raised, or served poison.

    FAIL-CLOSED: the Python oracles were deleted in the flip phase, so there
    is nothing to degrade to — the caller's operation stops. For the
    market-hours guard chain this means a vendor pull is REFUSED, never
    silently allowed (the shared-TOTP account protection of
    DEC-GROWW-MARKET-HOURS-GUARD-001 must never fail OPEN). The message
    carries the machine ``reason=`` token (``wheel_missing`` /
    ``wheel_stale`` / ``kernel_error`` / ``kernel_invalid``) + the kernel
    symbol involved.
    """


def require_pull_state_env_unset(env: Mapping[str, str] | None = None) -> None:
    """Refuse LOUDLY when the removed ``BRUTEX_PULL_STATE_RUST`` knob is set.

    Checked at EVERY kernel resolve (:func:`_resolve_kernel_symbol`) — i.e.
    on the first real use of any accessor — so a stale config surfaces at
    the first pull-state decision, before any verdict is served.

    Parameters
    ----------
    env
        The environment mapping to read (defaults to ``os.environ`` — the
        injection seam keeps the guard pure for tests).

    Raises
    ------
    PullStateFlagRemovedError
        When the variable is present with ANY non-empty value — truthy
        spellings ("1"/"true") AND falsy spellings ("0"/"off") alike: the
        knob itself is removed (DEC-RUST-PULL-STATE-001), and accepting any
        spelling would silently bless stale configs. An UNSET or
        empty/whitespace-only value passes (the ``VAR= cmd`` unset idiom).
    """
    source = os.environ if env is None else env
    raw = source.get(PULL_STATE_RUST_ENV, "")
    if raw.strip():
        raise PullStateFlagRemovedError(
            f"{PULL_STATE_RUST_ENV}={raw!r} is set, but the Rust pull-state "
            f"kernel is no longer optional — it is the ONLY decision path "
            f"(DEC-RUST-PULL-STATE-001). Unset the removed variable; every "
            f"value is refused, truthy spellings included."
        )


def _kernel_error_exc(fn: str, exc: BaseException) -> PullStateKernelError:
    """Build the typed error for a RAISING kernel call (``reason=kernel_error``)."""
    return PullStateKernelError(
        f"pull-state kernel call {fn!r} raised "
        f"(reason=kernel_error, error={exc!r}) — fail-closed: the Python "
        f"oracle was deleted (DEC-RUST-PULL-STATE-001)"
    )


def _kernel_invalid_exc(fn: str, detail: str) -> PullStateKernelError:
    """Build the typed error for a REFUSED kernel result (``reason=kernel_invalid``).

    Used by every refuse-poison gate (a stale/corrupt wheel's out-of-contract
    result) AND by the input-domain gates (an input outside the kernel's
    modeled domain — the pre-flip silent-Python-arm inputs, now refused
    loudly because no Python arm exists); *detail* names which.
    """
    return PullStateKernelError(
        f"pull-state kernel result for {fn!r} refused "
        f"(reason=kernel_invalid, detail={detail}) — fail-closed: the Python "
        f"oracle was deleted (DEC-RUST-PULL-STATE-001)"
    )


def _resolve_kernel_symbol(name: str) -> Callable[..., object]:
    """Return the named ``brutex_pull_state`` callable — or raise, fail-closed.

    The ONE resolver (the pre-flip trio ``resolve_pull_state_kernel`` /
    ``_resolve_wheel_symbol`` / ``_resolve_kernel_symbol`` consolidated).
    Order: the SET-knob refusal first (:func:`require_pull_state_env_unset`),
    then the lazy in-function import — a missing wheel (ImportError, incl.
    the shim's own stale-wheel handshake refusal, or a partially-initialised
    module's AttributeError) raises ``reason=wheel_missing`` chained
    ``from`` the import failure; an importable wheel missing *name* raises
    ``reason=wheel_stale`` naming the symbol.

    Raises
    ------
    PullStateFlagRemovedError
        When the removed ``BRUTEX_PULL_STATE_RUST`` knob is still set.
    PullStateKernelError
        When the wheel is absent (``wheel_missing``) or predates *name*
        (``wheel_stale``).
    """
    require_pull_state_env_unset()
    try:
        import brutex_pull_state
    except (ImportError, AttributeError) as exc:
        raise PullStateKernelError(
            f"the brutex_pull_state wheel is not importable "
            f"(reason=wheel_missing, needed for {name!r}) — fail-closed: "
            f"the Rust kernel is the only decision path "
            f"(DEC-RUST-PULL-STATE-001); build/install the wheel"
        ) from exc
    kernel = getattr(brutex_pull_state, name, None)
    if kernel is None:
        raise PullStateKernelError(
            f"the installed brutex_pull_state wheel lacks the {name!r} "
            f"symbol (reason=wheel_stale) — fail-closed: rebuild/upgrade "
            f"the wheel (DEC-RUST-PULL-STATE-001)"
        )
    return cast("Callable[..., object]", kernel)


def _call_kernel(name: str, *args: object) -> object:
    """One guarded kernel call: resolve + call — every fault a typed raise.

    Returns the kernel's RAW result (the caller's refuse-poison gate types
    it). A kernel raise becomes ``PullStateKernelError``
    (``reason=kernel_error``) chained ``from`` the original —
    ``KeyboardInterrupt`` / ``SystemExit`` (BaseException, outside the
    ``except Exception`` belt) and ``MemoryError`` (explicitly re-raised)
    always propagate RAW.

    Raises
    ------
    PullStateFlagRemovedError
        When the removed knob is still set (via the resolver).
    PullStateKernelError
        Wheel absent/stale (via the resolver) or the kernel raised.
    """
    kernel = _resolve_kernel_symbol(name)
    try:
        return kernel(*args)
    except MemoryError:
        raise
    except Exception as exc:  # the belt: ANY kernel raise becomes the typed error
        raise _kernel_error_exc(name, exc) from exc


def rust_pull_allowed(ist_dt: datetime, is_trading_day_flag: bool) -> bool:
    """The market-hours guard verdict — served by the Rust kernel, fail-closed.

    Packs *ist_dt*'s wall-clock time to MICROSECOND-OF-DAY (only ``.time()``
    is read — the caller resolves the date's trading-day status; the packing
    is order-isomorphic to ``datetime.time`` comparison so the ``[08:45,
    15:35)`` boundary is bit-exact) and calls ``pull_allowed_us``. The
    refuse-poison gate is bool-EXACT: a non-``bool`` result (an int/None/
    object pun from a stale/corrupt wheel) is REFUSED, never served as a
    truthy pun.

    Raises
    ------
    PullStateFlagRemovedError
        When the removed ``BRUTEX_PULL_STATE_RUST`` knob is still set.
    PullStateKernelError
        Wheel absent/stale, the kernel raised, or a non-bool result — the
        guard chain fails CLOSED (a pull is refused, never allowed).
    """
    t = ist_dt.time()
    us_of_day = ((t.hour * 60 + t.minute) * 60 + t.second) * 1_000_000 + t.microsecond
    verdict = _call_kernel("pull_allowed_us", us_of_day, bool(is_trading_day_flag))
    if not isinstance(verdict, bool):
        raise _kernel_invalid_exc("pull_allowed_us", type(verdict).__name__)
    return verdict


# --- pull_stall (Wave 1C) ---------------------------------------------------
# The wedge/alive decision plane of ``brutex/dashboard/pull_stall.py``
# (DEC-PULL-WEDGE-ESCALATE-001 + DEC-PULL-TELEMETRY-TRIAD-001). The caller
# computes every input (ONE monotonic-clock read → ages; timers/locks stay
# Python), the accessor packs nothing but plain bools/floats/tokens, and the
# refuse-poison gate rejects any out-of-vocabulary result — a refusal RAISES
# (fail-closed), never falls back.

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
) -> int:
    """The Rust wedge-escalation arm token — fail-closed.

    Returns the arm token (:data:`WEDGE_TOKEN_NO_PROBE` ..
    :data:`WEDGE_TOKEN_ESCALATE`) — the caller (``PullWatchdog``) maps it to
    the arm's ACTIONS verbatim in Python so every log line / event payload
    stays byte-identical. Refuse-poison gate: an int EXACTLY (bool is an int
    subclass — a bool pun is refused) inside the token vocabulary.

    Raises
    ------
    PullStateFlagRemovedError
        When the removed ``BRUTEX_PULL_STATE_RUST`` knob is still set.
    PullStateKernelError
        Wheel absent/stale, the kernel raised, or an out-of-vocabulary /
        bool-pun result.
    """
    token = _call_kernel(
        "wedge_decision",
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
    if isinstance(token, bool) or not isinstance(token, int) or token not in _WEDGE_TOKENS:
        raise _kernel_invalid_exc("wedge_decision", type(token).__name__)
    return token


def rust_alive_log_due(since_last_s: float, interval_s: float) -> bool:
    """The Rust alive-throttle emit-now? verdict — fail-closed.

    The emit-now? half of ``_alive_payload_locked``'s throttle law (the
    payload build + the ``_last_alive_log`` stamp stay Python). Bool-exact
    refuse-poison gate.

    Raises
    ------
    PullStateFlagRemovedError
        When the removed ``BRUTEX_PULL_STATE_RUST`` knob is still set.
    PullStateKernelError
        Wheel absent/stale, the kernel raised, or a non-bool result.
    """
    verdict = _call_kernel("alive_log_due", since_last_s, interval_s)
    if not isinstance(verdict, bool):
        raise _kernel_invalid_exc("alive_log_due", type(verdict).__name__)
    return verdict


__all__ = [
    "PULL_STATE_RUST_ENV",
    # --- pull_stall (Wave 1C) ---
    "WEDGE_TOKEN_ABSTAIN_PROBE_NONE",
    "WEDGE_TOKEN_ADOPT_BASELINE",
    "WEDGE_TOKEN_DEFER",
    "WEDGE_TOKEN_ESCALATE",
    "WEDGE_TOKEN_NO_PROBE",
    "WEDGE_TOKEN_REBASELINE",
    # --- the fail-closed error surface (flip phase) ---
    "PullStateFlagRemovedError",
    "PullStateKernelError",
    "require_pull_state_env_unset",
    "rust_alive_log_due",
    "rust_pull_allowed",
    "rust_wedge_decision",
]


# --- pull_outcome (Wave 1A) -------------------------------------------------
# DEC-RUST-PULL-STATE-001 Wave 1A: the last-pull OUTCOME decision kernel
# (DEC-OPERATOR-HEALTH-TRUTH-001 re-hosted). The wheel's
# ``classify_pull_outcome`` returns MACHINE CODES only — ``(outcome, tone,
# headline_code, detail_code)``, with the ``@verdict`` / ``@verdict_prepull``
# markers handing the banner fold back to Python — and ``verdict_correlates``
# carries the run-id correlation law. Every human string and the
# ``verdict_banner`` colour authority stay in
# ``brutex.dashboard.pull_outcome`` (CLAUDE.md §11 COMMON). Append-only
# section layout preserved across the flip.

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
) -> tuple[str, str, str, str]:
    """The Rust outcome-classification codes — fail-closed.

    Refuse-poison gate: anything but a 4-tuple of ``str`` is refused. The
    CODE-level vocabulary itself is validated by the consumer
    (``brutex.dashboard.pull_outcome``), which owns the composition tables.

    Raises
    ------
    PullStateFlagRemovedError
        When the removed ``BRUTEX_PULL_STATE_RUST`` knob is still set.
    PullStateKernelError
        Wheel absent/stale, the kernel raised (including the pyo3
        ``OverflowError`` on an out-of-``i64`` *failed*), or a non-4-tuple /
        non-str-member result.
    """
    raw = _call_kernel(
        "classify_pull_outcome",
        state,
        detail,
        failed,
        bool(stalled),
        bool(wedged),
        bool(has_verdict),
    )
    if (
        not isinstance(raw, tuple)
        or len(raw) != _OUTCOME_CODES_ARITY
        or not all(isinstance(part, str) for part in raw)
    ):
        raise _kernel_invalid_exc("classify_pull_outcome", type(raw).__name__)
    return cast("tuple[str, str, str, str]", raw)


def rust_verdict_correlates(retained_id: str, verdict_id: str) -> bool:
    """The Rust run-id correlation-law verdict — fail-closed.

    Bool-exact refuse-poison gate: a truthy pun from a stale/corrupt wheel
    is refused, never served.

    Raises
    ------
    PullStateFlagRemovedError
        When the removed ``BRUTEX_PULL_STATE_RUST`` knob is still set.
    PullStateKernelError
        Wheel absent/stale, the kernel raised, or a non-bool result.
    """
    raw = _call_kernel("verdict_correlates", retained_id, verdict_id)
    if not isinstance(raw, bool):
        raise _kernel_invalid_exc("verdict_correlates", type(raw).__name__)
    return raw


__all__ += [
    "rust_classify_pull_outcome",
    "rust_verdict_correlates",
]


# --- pull_scope (Wave 1B) ---
# DEC-RUST-PULL-STATE-001 Wave 1B: the ``brutex/dashboard/pull_scope.py``
# note-surgery / scope-token kernel accessors. STRINGS cross this edge (the
# ONE sanctioned exception to the integers-only law — the surgery outputs
# ARE strings, so byte-exactness IS the contract; every anchor literal is
# drift-pinned by the committed golden fixture pair). Append-only section
# layout preserved across the flip.


#: ``scope_token``'s unknown-token verdict code — the CALLER composes the
#: ``{token!r}`` refusal itself (CPython repr is never re-minted in Rust).
SCOPE_CODE_UNKNOWN: Final[int] = 255

#: The whole ``scope_token`` verdict vocabulary (0=full · 1=spot-only ·
#: 2=fno-only · 255=unknown) — anything else is a stale/corrupt wheel.
_SCOPE_TOKEN_CODES: Final[frozenset[int]] = frozenset({0, 1, 2, SCOPE_CODE_UNKNOWN})

#: The surgery kernels' result arity — ``(note, prefix_present, clause_present)``.
_SURGERY_TRIPLE_ARITY: Final[int] = 3


def rust_scope_token(token: str | None) -> int:
    """The kernel's ``scope_token`` verdict code — fail-closed.

    Refuse-poison gate: EXACTLY ``int`` (``bool`` is an ``int`` subclass —
    a ``True`` pun from a stale wheel is refused, never read as spot-only)
    and EXACTLY one of the four vocabulary codes.

    Raises
    ------
    PullStateFlagRemovedError
        When the removed ``BRUTEX_PULL_STATE_RUST`` knob is still set.
    PullStateKernelError
        Wheel absent/stale, the kernel raised, or an out-of-vocabulary /
        bool-pun result.
    """
    verdict = _call_kernel("scope_token", token)
    if type(verdict) is not int or verdict not in _SCOPE_TOKEN_CODES:
        raise _kernel_invalid_exc("scope_token", repr(verdict)[:80])
    return verdict


def rust_market_hours_note_rewrite(note: str, phases_replacement: str) -> str:
    """The kernel's market-hours note rewrite — fail-closed, str-exact gate.

    Raises
    ------
    PullStateFlagRemovedError
        When the removed ``BRUTEX_PULL_STATE_RUST`` knob is still set.
    PullStateKernelError
        Wheel absent/stale, the kernel raised, or a non-str result.
    """
    result = _call_kernel("market_hours_note_rewrite", note, phases_replacement)
    if type(result) is not str:
        raise _kernel_invalid_exc("market_hours_note_rewrite", type(result).__name__)
    return result


def _validated_surgery_triple(name: str, result: object) -> tuple[str, bool, bool]:
    """The surgery accessors' shared refuse-poison gate — fail-closed.

    EXACTLY a 3-tuple of ``(str, bool, bool)`` — the rewritten note + the
    two ``note_surgery_miss`` WARN inputs; anything else (wrong arity, a
    list pun, an int-for-bool pun) can only mean a stale/corrupt wheel and
    is REFUSED.

    Raises
    ------
    PullStateKernelError
        On any shape/type violation (``reason=kernel_invalid``).
    """
    if (
        isinstance(result, tuple)
        and len(result) == _SURGERY_TRIPLE_ARITY
        and type(result[0]) is str
        and type(result[1]) is bool
        and type(result[2]) is bool
    ):
        return (result[0], result[1], result[2])
    raise _kernel_invalid_exc(name, type(result).__name__)


def rust_spot_only_note_surgery(base_note: str) -> tuple[str, bool, bool]:
    """The kernel's spot-only note surgery — fail-closed.

    Returns ``(rewritten_note, prefix_present, clause_present)`` — the
    caller fires the ``note_surgery_miss`` WARN off the booleans and appends
    the Greeks + crossverify clauses itself (they compose the tail outcome).

    Raises
    ------
    PullStateFlagRemovedError
        When the removed ``BRUTEX_PULL_STATE_RUST`` knob is still set.
    PullStateKernelError
        Wheel absent/stale, the kernel raised, or a malformed triple.
    """
    return _validated_surgery_triple(
        "spot_only_note_surgery", _call_kernel("spot_only_note_surgery", base_note)
    )


def rust_fno_only_note_surgery(base_note: str) -> tuple[str, bool, bool]:
    """The kernel's fno-only note surgery — fail-closed (the spot-axis mirror).

    Raises
    ------
    PullStateFlagRemovedError
        When the removed ``BRUTEX_PULL_STATE_RUST`` knob is still set.
    PullStateKernelError
        Wheel absent/stale, the kernel raised, or a malformed triple.
    """
    return _validated_surgery_triple(
        "fno_only_note_surgery", _call_kernel("fno_only_note_surgery", base_note)
    )


__all__ += [
    "SCOPE_CODE_UNKNOWN",
    "rust_fno_only_note_surgery",
    "rust_market_hours_note_rewrite",
    "rust_scope_token",
    "rust_spot_only_note_surgery",
]


# --- auto_pull_gate (Wave 2D) -----------------------------------------------
# The ``--auto`` gate decision plane of ``brutex/calendars/auto_pull_gate.py``
# (DEC-PULL-SCOPED-ALL-ONECLICK-001 + DEC-SETTLE-EVIDENCE-ADOPTION-001). The
# caller (the calendars module) resolves the clock, the NSE calendar
# predicates and the settle tier ITSELF (``resolve_settle_resolution`` — env
# reads + the fail-loud junk raise — stays Python by design), then packs
# microsecond-of-day integers + plain flags; codes/bools come back, Python
# composes every human string. Append-only section layout preserved.

#: The ``auto_gate_decision`` reason vocabulary (mirrors
#: ``rust/brutex-pull-state/src/auto_pull_gate.rs`` — index == code value).
GATE_CODE_OK: Final[int] = 0
GATE_CODE_NON_TRADING_DAY: Final[int] = 1
GATE_CODE_MUHURAT_DEFERRAL: Final[int] = 2
GATE_CODE_BEFORE_SETTLE: Final[int] = 3
GATE_CODE_MARKET_HOURS_BLOCKED: Final[int] = 4

#: Every legal gate reason — the refuse-poison vocabulary gate.
_GATE_CODES: Final[frozenset[int]] = frozenset(range(GATE_CODE_MARKET_HOURS_BLOCKED + 1))

#: The ``settle_token_verdict`` OK code (the ONLY arm whose value slot the
#: caller consumes — every non-OK verdict routes the caller to its own
#: byte-identical fail-loud ``ValueError`` composition).
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
) -> tuple[int, bool]:
    """The Rust ``--auto`` gate ladder decision — fail-closed.

    Returns ``(reason_code, coverage_warn)`` — the caller maps the code to
    its own verdict builders (every detail string stays single-sourced in
    the calendars module) and fires the coverage WARN itself. Refuse-poison
    gate: EXACTLY a 2-tuple of (a non-bool ``int`` inside the reason
    vocabulary, an exact ``bool``).

    Raises
    ------
    PullStateFlagRemovedError
        When the removed ``BRUTEX_PULL_STATE_RUST`` knob is still set.
    PullStateKernelError
        Wheel absent/stale, the kernel raised, or a malformed pair.
    """
    result = _call_kernel(
        "auto_gate_decision",
        trading_day,
        coverage_available,
        muhurat_listed,
        now_us_of_day,
        settle_us_of_day,
    )
    if (
        isinstance(result, tuple)
        and len(result) == _GATE_PAIR_ARITY
        and type(result[0]) is int
        and result[0] in _GATE_CODES
        and type(result[1]) is bool
    ):
        return (result[0], result[1])
    raise _kernel_invalid_exc("auto_gate_decision", repr(result)[:80])


def rust_muhurat_window_active(
    now_us_of_day: int,
    open_us_of_day: int,
    close_us_of_day: int,
    pre_margin_us: int,
    post_margin_us: int,
) -> bool:
    """The Rust margined Muhurat-window verdict — fail-closed.

    The session LOOKUP (``MUHURAT_SESSIONS``) stays Python — only a LISTED
    session's interval ever crosses. Bool-exact refuse-poison gate.

    Raises
    ------
    PullStateFlagRemovedError
        When the removed ``BRUTEX_PULL_STATE_RUST`` knob is still set.
    PullStateKernelError
        Wheel absent/stale, the kernel raised, or a non-bool result.
    """
    verdict = _call_kernel(
        "muhurat_window_active",
        now_us_of_day,
        open_us_of_day,
        close_us_of_day,
        pre_margin_us,
        post_margin_us,
    )
    if not isinstance(verdict, bool):
        raise _kernel_invalid_exc("muhurat_window_active", type(verdict).__name__)
    return verdict


def rust_settle_token_verdict(token: str, block_end_us: int) -> tuple[int, int]:
    """The Rust settle-token parse verdict — fail-closed.

    *token* must arrive PRE-STRIPPED (``str.strip`` stays Python — its
    whitespace set is CPython's ``isspace`` law, not Unicode
    ``White_Space``). Returns ``(verdict_code, us_of_day_or_-1)``; the
    caller consumes the value ONLY on :data:`SETTLE_CODE_OK`, whose slot is
    gated to a minute-granular in-range instant (the strict ``HH:MM`` law —
    a poisoned off-grid value can only mean a stale/corrupt wheel); every
    non-OK verdict routes the caller to its own byte-identical fail-loud
    ``ValueError`` composition.

    Raises
    ------
    PullStateFlagRemovedError
        When the removed ``BRUTEX_PULL_STATE_RUST`` knob is still set.
    PullStateKernelError
        Wheel absent/stale, the kernel raised, or a malformed pair /
        off-grid OK value.
    """
    result = _call_kernel("settle_token_verdict", token, block_end_us)
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
    raise _kernel_invalid_exc("settle_token_verdict", repr(result)[:80])


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
# parse-shape + resume-decision kernel accessors. Append-only section layout
# preserved across the flip.
#
# BOUNDARY (design risk #3 — the corruption-taxonomy law): the JSON DECODE
# of the intent sidecar STAYS PYTHON.  The consumer classifies the
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
# and now RAISES — fail-closed, no oracle exists).

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
#: (1..=8, the first violation in the kernel's probe order) means the
#: corrupt/absent answer; 255 (out-of-vocabulary inputs) is NOT in the
#: served vocabulary, so it is refused below.
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
#: is OUTSIDE the kernel's modeled domain and is REFUSED loudly (the
#: pre-flip silent Python arm was deleted with the oracle).
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
) -> int:
    """The kernel's intent shape verdict — fail-closed.

    Takes the 9-slot classified-payload tuple UNPACKED (the
    ``pull_intent._classify_payload_for_kernel`` output — ``root_mapping``
    rides as the classifier's 0/1 int and is boolified here).  Refuse-poison
    gate: EXACTLY ``int`` and EXACTLY one of the nine served verdict codes —
    the kernel's own 255 out-of-vocabulary-input answer (a classifier/wheel
    mismatch) is REFUSED, never served as a verdict.

    Raises
    ------
    PullStateFlagRemovedError
        When the removed ``BRUTEX_PULL_STATE_RUST`` knob is still set.
    PullStateKernelError
        Wheel absent/stale, the kernel raised, or an out-of-vocabulary /
        bool-pun result.
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
    if type(verdict) is not int or verdict not in _INTENT_SHAPE_CODES:
        raise _kernel_invalid_exc("intent_shape_verdict", repr(verdict)[:80])
    return verdict


def rust_intent_resume_decision(
    *,
    has_intent: bool,
    probe_acquirable: bool,
    restarts: int,
    max_restarts: int,
) -> int:
    """The kernel's resume arm token — fail-closed.

    Counters outside u64 are OUTSIDE the kernel's modeled domain and are
    refused loudly (the pre-flip silent Python arm was deleted with the
    oracle).  Beyond the type + vocabulary refuse-poison gate, the ONE
    site-impossible arm is gated too: ``NO_INTENT`` is total in
    ``has_intent`` (the kernel lattice returns it iff the intent is absent),
    so a mismatch there can only mean a stale/corrupt wheel — refused,
    never served.

    Raises
    ------
    PullStateFlagRemovedError
        When the removed ``BRUTEX_PULL_STATE_RUST`` knob is still set.
    PullStateKernelError
        Wheel absent/stale, the kernel raised, an out-of-u64 counter, an
        out-of-vocabulary result, or the NO_INTENT consistency violation.
    """
    if not (0 <= restarts <= _U64_MAX and 0 <= max_restarts <= _U64_MAX):
        raise _kernel_invalid_exc(
            "intent_resume_decision",
            f"counter outside the kernel u64 domain: "
            f"restarts={restarts!r} max_restarts={max_restarts!r}",
        )
    verdict = _call_kernel(
        "intent_resume_decision",
        bool(has_intent),
        bool(probe_acquirable),
        restarts,
        max_restarts,
    )
    if type(verdict) is not int or verdict not in _RESUME_ARMS:
        raise _kernel_invalid_exc("intent_resume_decision", repr(verdict)[:80])
    if (verdict == RESUME_ARM_NO_INTENT) != (not has_intent):
        raise _kernel_invalid_exc(
            "intent_resume_decision",
            f"no_intent arm inconsistent with has_intent={has_intent}",
        )
    return verdict


def rust_intent_refused_restore_decision(
    *,
    dry_run: bool,
    started: bool,
    current_present: bool,
    prior_present: bool,
) -> int:
    """The kernel's refused-start restore arm — fail-closed.

    The ``NO_ACTION`` arm is total in ``dry_run or started`` — the
    consistency gate refuses a wheel that breaks that totality (the
    resume-decision posture), so the caller can never be steered into
    skipping a restore (or performing one) on a short-circuit input.

    Raises
    ------
    PullStateFlagRemovedError
        When the removed ``BRUTEX_PULL_STATE_RUST`` knob is still set.
    PullStateKernelError
        Wheel absent/stale, the kernel raised, an out-of-vocabulary
        result, or the NO_ACTION consistency violation.
    """
    verdict = _call_kernel(
        "intent_refused_restore_decision",
        bool(dry_run),
        bool(started),
        bool(current_present),
        bool(prior_present),
    )
    if type(verdict) is not int or verdict not in _RESTORE_ARMS:
        raise _kernel_invalid_exc("intent_refused_restore_decision", repr(verdict)[:80])
    if (verdict == RESTORE_ARM_NO_ACTION) != (dry_run or started):
        raise _kernel_invalid_exc(
            "intent_refused_restore_decision",
            f"no_action arm inconsistent with dry_run={dry_run} started={started}",
        )
    return verdict


def rust_intent_resume_enabled(raw: str | None) -> bool:
    """The kernel's resume-env token law — fail-closed.

    Keeps the Wave-2E input contract exactly: *raw* is the UNPROCESSED env
    token (or ``None`` for unset) — the strip+lower+falsy-set law lives in
    the kernel.  Refuse-poison gate: EXACTLY ``bool`` (an int pun from a
    stale wheel is refused, never served as truthy).  A lone-surrogate
    *raw* fails the pyo3 ``&str`` edge inside the belt and now RAISES
    (``kernel_error``) — fail-closed, no oracle exists.

    Raises
    ------
    PullStateFlagRemovedError
        When the removed ``BRUTEX_PULL_STATE_RUST`` knob is still set.
    PullStateKernelError
        Wheel absent/stale, the kernel raised, or a non-bool result.
    """
    verdict = _call_kernel("intent_resume_enabled", raw)
    if not isinstance(verdict, bool):
        raise _kernel_invalid_exc("intent_resume_enabled", type(verdict).__name__)
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
# ``to_dict`` and the injected clock stay Python. The pre-flip SILENT domain
# gate (:func:`_progress_inputs_in_domain`) survives as the input-domain
# REFUSAL gate: an input outside the spike-proven ``[0, 2**53]`` exactness
# envelope (or a non-float instant / a truthy pun) is refused loudly — the
# Python arm that once served it was deleted with the oracle. Unreachable
# from the production tracker, whose counters are clamped non-negative ints.

#: The kernel count-domain ceiling (inclusive): both division operands stay
#: exactly representable in f64, so the kernel's division equals CPython's
#: correctly-rounded ``int/int`` true division (the Wave-2F spike law).
PROGRESS_COUNT_MAX: Final[int] = 2**53

#: The derived bundle's kernel arity —
#: ``(percent, elapsed, units_per_sec, eta_present, eta_seconds)``.
_PROGRESS_TUPLE_ARITY: Final[int] = 5


def _progress_count_in_domain(value: int) -> bool:
    """EXACTLY ``int`` (a ``bool`` pun fails) inside ``[0, 2**53]``."""
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
    """The input-domain eligibility gate for :func:`rust_progress_derived`.

    Counts must be genuine ints in the exactness domain; instants must be
    genuine ``float`` instances (a hostile int-typed instant would have
    taken CPython's exact ``int`` arithmetic through the deleted oracle — a
    semantics the f64 kernel deliberately does not model); ``open_ended``
    must be a genuine ``bool``.  NaN/inf FLOATS are IN domain — the kernel
    mirrors the oracle's IEEE comparisons exactly.
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


def _validated_progress_tuple(result: object) -> tuple[float, float, float, float | None]:
    """The refuse-poison gate + the ``eta_present`` fold — fail-closed.

    EXACTLY a 5-tuple ``(float, float, float, bool, float)`` (bool puns in
    the float slots and int puns in the bool slot are each refused — only a
    stale/corrupt wheel produces them); folds to the facade quadruple
    ``(percent, elapsed, units_per_sec, eta_or_None)``.

    Raises
    ------
    PullStateKernelError
        On any shape/type violation (``reason=kernel_invalid``).
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
    raise _kernel_invalid_exc("progress_derived", type(result).__name__)


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
) -> tuple[float, float, float, float | None]:
    """The Rust derived-metrics bundle — fail-closed.

    Returns ``(percent, elapsed_seconds, units_per_sec, eta_seconds)`` —
    each bit-identical to the corresponding pre-flip ``ProgressSnapshot``
    property (``eta_seconds`` already folded to ``float | None``). The
    caller passes ``state_running`` as the PRE-COMPUTED ``state ==
    STATE_RUNNING`` verdict (string comparison stays Python) and
    ``open_ended`` as a genuine ``bool``.

    Raises
    ------
    PullStateFlagRemovedError
        When the removed ``BRUTEX_PULL_STATE_RUST`` knob is still set.
    PullStateKernelError
        Wheel absent/stale, the kernel raised, an input outside the
        kernel's modeled domain, or a malformed result tuple.
    """
    if not _progress_inputs_in_domain(
        open_ended, total, completed, phase_base_completed, updated_at, started_at, phase_started_at
    ):
        raise _kernel_invalid_exc("progress_derived", "input outside the kernel's modeled domain")
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
    return _validated_progress_tuple(result)


__all__ += [
    "PROGRESS_COUNT_MAX",
    "rust_progress_derived",
]


# --- auto_pull_scheduler (Wave 3G) -------------------------------------------
# DEC-RUST-PULL-STATE-001 Wave 3G: the ``brutex/dashboard/auto_pull_scheduler``
# tick-decision + durable-state-parse kernel accessors. Append-only section
# layout preserved across the flip.
#
# BOUNDARY: the ticker thread, the corruption-hardened store I/O
# (``read_sidecar_bytes`` + the corrupt ⇒ DISARMED + one WARN posture), the
# per-(day, reason) log-dedup memo, every log/status string and the
# fail-LOUD ``BRUTEX_AUTO_PULL_TICK_S`` resolver STAY PYTHON.  The JSON
# decode of the state sidecar stays Python too (the Wave-2E taxonomy law) —
# every decode failure folds BEFORE any kernel consult.  The consumer's
# two-stage tick consult (pre-gate placeholders, then the post-gate real
# facts) preserves the LAZY gate/launcher evaluation order — the ladder's
# own arm ORDER makes the placeholder stage exact (pinned by the exhaustive
# lattice golden + the crate's placeholder-leak test).

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
#: inputs) is NOT in it, so a classifier/wheel mismatch is REFUSED.
_STATE_CODES: Final[frozenset[int]] = frozenset(range(STATE_CODE_BAD_LAST_SUCCESS + 1))


def rust_auto_tick_decision(
    *,
    armed: bool,
    succeeded_today: bool,
    attempted_today: bool,
    gate_allowed: bool,
    launcher_bound: bool,
) -> int:
    """The kernel's tick-decision ladder token — fail-closed.

    The caller (``AutoPullScheduler``) resolves every fact —
    ``succeeded_today``/``attempted_today`` are the Python-side ISO-date
    comparisons (dates never cross the FFI) — and exploits the ladder's arm
    ORDER for its two-stage consult (pre-gate placeholders first; the
    kernel can only answer a state arm or DEFERRED for them).  Refuse-poison
    gate: EXACTLY ``int`` (a ``bool`` pun is refused) inside the six-token
    vocabulary.

    Raises
    ------
    PullStateFlagRemovedError
        When the removed ``BRUTEX_PULL_STATE_RUST`` knob is still set.
    PullStateKernelError
        Wheel absent/stale, the kernel raised, or an out-of-vocabulary /
        bool-pun result.
    """
    verdict = _call_kernel(
        "auto_tick_decision",
        armed,
        succeeded_today,
        attempted_today,
        gate_allowed,
        launcher_bound,
    )
    if type(verdict) is not int or verdict not in _TICK_CODES:
        raise _kernel_invalid_exc("auto_tick_decision", repr(verdict)[:80])
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
) -> int:
    """The kernel's durable-state shape verdict — fail-closed.

    Codes in (the consumer's mechanical classification of the
    already-decoded payload), ONE verdict code out; every refusal MESSAGE
    stays Python (``_state_from_payload`` composes the byte-identical
    ``ValueError`` text per code).  Refuse-poison gate: EXACTLY ``int``
    inside the seven-code served vocabulary (the kernel's 255
    input-mismatch answer is refused — a classifier/wheel mismatch).

    Raises
    ------
    PullStateFlagRemovedError
        When the removed ``BRUTEX_PULL_STATE_RUST`` knob is still set.
    PullStateKernelError
        Wheel absent/stale, the kernel raised, or an out-of-vocabulary /
        bool-pun result.
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
    if type(verdict) is not int or verdict not in _STATE_CODES:
        raise _kernel_invalid_exc("auto_state_verdict", repr(verdict)[:80])
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
# fold accessor. Append-only section layout preserved across the flip.
#
# BOUNDARY: the consumer pre-extracts + scope-filters the ledger rows / lake
# spans (the ``str()``/``int()`` conversions over arbitrary payload objects
# and the lazy-``int()``-after-the-scope-filter law stay Python) and renders
# the ``repr()`` sample strings from the returned triples; the kernel folds
# sets, sorts (UTF-8 byte order ≡ code-point order — valid UTF-8 only; a
# lone-surrogate symbol fails the pyo3 edge and RAISES, fail-closed) and
# caps.

#: Sample-kind codes (mirrors ``rust/brutex-pull-state/src/pull_census.rs``).
SAMPLE_KIND_SETTLED_UNWRITTEN: Final[int] = 0
SAMPLE_KIND_WRITTEN_UNSETTLED: Final[int] = 1

_SAMPLE_KINDS: Final[frozenset[int]] = frozenset(
    {SAMPLE_KIND_SETTLED_UNWRITTEN, SAMPLE_KIND_WRITTEN_UNSETTLED}
)

#: The kernel's sample cap — sync-pinned against the Python
#: ``pull_census.CENSUS_SAMPLE_CAP`` by the consumer suite; a longer sample
#: list can only mean a stale/corrupt wheel (refused below).
_CENSUS_SAMPLE_CAP: Final[int] = 20

#: The census result arity — ``(settled_unwritten, written_unsettled, samples)``.
_CENSUS_TRIPLE_ARITY: Final[int] = 3


def _census_count_ok(value: object) -> bool:
    """EXACTLY a non-negative ``int`` (a ``bool`` pun fails)."""
    return type(value) is int and value >= 0


def rust_census_fold(
    rows: Sequence[tuple[str, str, bool]],
    written_pairs: Sequence[tuple[str, str]],
) -> tuple[int, int, tuple[tuple[int, str, str], ...]]:
    """The kernel's census fold — fail-closed.

    Refuse-poison gate: EXACTLY a 3-tuple of two non-negative ints + a
    ≤-cap sequence of ``(kind, symbol, interval)`` triples with kind in the
    two-token vocabulary — anything else is a stale/corrupt wheel and is
    REFUSED.

    Raises
    ------
    PullStateFlagRemovedError
        When the removed ``BRUTEX_PULL_STATE_RUST`` knob is still set.
    PullStateKernelError
        Wheel absent/stale, the kernel raised, or a malformed fold result.
    """
    result = _call_kernel("census_fold", list(rows), list(written_pairs))
    if not isinstance(result, tuple) or len(result) != _CENSUS_TRIPLE_ARITY:
        raise _kernel_invalid_exc("census_fold", type(result).__name__)
    settled_unwritten, written_unsettled, samples = result
    if (
        not _census_count_ok(settled_unwritten)
        or not _census_count_ok(written_unsettled)
        or not isinstance(samples, (list, tuple))
        or len(samples) > _CENSUS_SAMPLE_CAP
    ):
        raise _kernel_invalid_exc("census_fold", repr(result)[:80])
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
            raise _kernel_invalid_exc("census_fold", repr(item)[:80])
        validated.append((item[0], item[1], item[2]))
    return (settled_unwritten, written_unsettled, tuple(validated))


__all__ += [
    "SAMPLE_KIND_SETTLED_UNWRITTEN",
    "SAMPLE_KIND_WRITTEN_UNSETTLED",
    "rust_census_fold",
]


# --- auto_pull (Wave 3G) -----------------------------------------------------
# DEC-RUST-PULL-STATE-001 Wave 3G: the ``brutex/dashboard/auto_pull``
# reload-predicate accessor. Append-only section layout preserved.

#: The i64 FFI band for the two counts — an out-of-band CPython int is
#: OUTSIDE the kernel's modeled domain and is REFUSED loudly (the pre-flip
#: silent Python arm was deleted with the oracle; the intent ``_U64_MAX``
#: posture).
_I64_MIN: Final[int] = -(2**63)
_I64_MAX: Final[int] = 2**63 - 1


def rust_should_reload_master(
    *,
    index_count: int,
    stock_count: int,
    legs_present: bool,
    stale_economics: bool,
    stale_eligibility: bool,
) -> bool:
    """The kernel's master-reload predicate — fail-closed.

    An out-of-i64 count is outside the kernel's modeled domain and is
    refused loudly; the refuse-poison gate is bool-exact.

    Raises
    ------
    PullStateFlagRemovedError
        When the removed ``BRUTEX_PULL_STATE_RUST`` knob is still set.
    PullStateKernelError
        Wheel absent/stale, the kernel raised, an out-of-i64 count, or a
        non-bool result.
    """
    if not (_I64_MIN <= index_count <= _I64_MAX and _I64_MIN <= stock_count <= _I64_MAX):
        raise _kernel_invalid_exc(
            "should_reload_master",
            f"count outside the kernel i64 domain: "
            f"index_count={index_count!r} stock_count={stock_count!r}",
        )
    verdict = _call_kernel(
        "should_reload_master",
        index_count,
        stock_count,
        legs_present,
        stale_economics,
        stale_eligibility,
    )
    if not isinstance(verdict, bool):
        raise _kernel_invalid_exc("should_reload_master", type(verdict).__name__)
    return verdict


__all__ += [
    "rust_should_reload_master",
]


# --- rate_bridge + rate_bridge_cache (Wave 3H) -------------------------------
# DEC-RUST-PULL-STATE-001 Wave 3H: the ``brutex/data/rate_bridge.py``
# payload-verdict kernel + the ``brutex/dashboard/rate_bridge_cache.py``
# memo decision kernels. Append-only section layout preserved.
#
# BOUNDARY (the Wave-2E decode-stays-Python precedent): the bridge-file
# read, the JSON decode (CPython's WHOLE failure taxonomy) and the
# ``float()``/``int()`` coercion PROBES stay Python — the consumer
# classifies the already-decoded payload's concrete types / coercibility
# FACTS into the small integer codes below (a MECHANICAL projection in
# ``rate_bridge._classify_bridge_payload_for_kernel``); a payload whose
# coercion probe raises ``OverflowError`` (the documented pre-existing
# raises envelope — a 400-digit int in a float field, ``Infinity`` in an
# int field) NEVER consults the kernel (the classifier's own arm preserves
# the raise byte-for-byte).  For the cache, the caller computes the age
# from ONE monotonic-clock read (clocks never cross the FFI) and the
# thread core — lock, deadline guard, ``_inflight`` clearing law, degrade
# latch — is PERMANENT-PYTHON (the design §2.3 ruling); the consumer's own
# fail-safe belt FORCES the follower arm while ``_inflight`` is set, so a
# poisoned kernel can only ever be refused, never spawn a second refresh.

#: ``bridge_payload_verdict``'s VALID verdict — the caller falls through to
#: its construction; every other in-vocabulary code (1..=12, the first
#: violation in the kernel's probe order) means the ``None`` answer.
BRIDGE_VERDICT_VALID: Final[int] = 0

#: The whole served bridge-verdict vocabulary (0=valid · 1=not-mapping ·
#: 2=schema · 3..=12 the ten required fields in constructor probe order) —
#: anything else (incl. the kernel's own 255 bad-input answer, a
#: classifier/wheel mismatch) is a stale/corrupt wheel and is refused.
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
) -> tuple[int, int]:
    """The kernel's bridge payload verdict — fail-closed.

    Takes the classified-payload tuple UNPACKED (the
    ``rate_bridge._classify_bridge_payload_for_kernel`` output —
    ``root_mapping`` rides as the classifier's 0/1 int and is boolified
    here).  Refuse-poison gate: EXACTLY a 2-tuple of EXACTLY-``int`` codes
    inside the served vocabularies — the kernel's own 255
    out-of-vocabulary-input answer (a classifier/wheel mismatch) is
    REFUSED, never served as a verdict.

    Raises
    ------
    PullStateFlagRemovedError
        When the removed ``BRUTEX_PULL_STATE_RUST`` knob is still set.
    PullStateKernelError
        Wheel absent/stale, the kernel raised, or a malformed pair.
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
    if (
        isinstance(result, tuple)
        and len(result) == _BRIDGE_PAIR_ARITY
        and type(result[0]) is int
        and type(result[1]) is int
        and result[0] in _BRIDGE_VERDICT_CODES
        and result[1] in _BRIDGE_PA_CODES
    ):
        return (result[0], result[1])
    raise _kernel_invalid_exc("bridge_payload_verdict", repr(result)[:80])


#: The memo serve-arm vocabulary (mirrors
#: ``rust/brutex-pull-state/src/rate_bridge_cache.rs`` — index == token).
MEMO_ARM_CACHED_HIT: Final[int] = 0
MEMO_ARM_FOLLOWER: Final[int] = 1
MEMO_ARM_OWNER: Final[int] = 2

_MEMO_ARMS: Final[frozenset[int]] = frozenset(range(MEMO_ARM_OWNER + 1))


def rust_memo_serve_arm(age_s: float | None, ttl_s: float, inflight: bool) -> int:
    """The three-arm memo serve token — fail-closed.

    ``age_s`` is the caller's PRE-COMPUTED clamped monotonic delta
    (``None`` = never probed); the caller maps the arm to its ACTIONS
    verbatim in Python — under its own fail-safe belt (an OWNER arm while
    ``_inflight`` is set is FORCED to follower, so a poisoned kernel can
    never break the single-flight law).  Int-exact vocabulary gate.

    Raises
    ------
    PullStateFlagRemovedError
        When the removed ``BRUTEX_PULL_STATE_RUST`` knob is still set.
    PullStateKernelError
        Wheel absent/stale, the kernel raised, or an out-of-vocabulary /
        bool-pun result.
    """
    token = _call_kernel("memo_serve_arm", age_s, ttl_s, inflight)
    if type(token) is not int or token not in _MEMO_ARMS:
        raise _kernel_invalid_exc("memo_serve_arm", repr(token)[:80])
    return token


def rust_token_revalidates(token: str | None, prior: str | None) -> bool:
    """The token-revalidation verdict — fail-closed.

    The ``RateBridgeCache._refresh_body`` law — ``True`` = keep the memo,
    skip the read; ``False`` = re-read.  A lone-surrogate token fails the
    pyo3 ``&str`` edge inside the belt and now RAISES (``kernel_error``) —
    fail-closed, no oracle exists.  Bool-exact refuse-poison gate.

    Raises
    ------
    PullStateFlagRemovedError
        When the removed ``BRUTEX_PULL_STATE_RUST`` knob is still set.
    PullStateKernelError
        Wheel absent/stale, the kernel raised, or a non-bool result.
    """
    verdict = _call_kernel("token_revalidates", token, prior)
    if not isinstance(verdict, bool):
        raise _kernel_invalid_exc("token_revalidates", repr(verdict)[:80])
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
