"""calendar_rust — the default-ON Rust routing facade for the calendar-leaf PURE laws.

DEC-RUST-CALENDAR-FACADE-FLIP-001 (MIGRATION.md #2218); the facade flip of the
crate landed INERT by DEC-RUST-CALENDAR-LEAVES-001 (#1962). Routes the THREE
pure calendar-leaf decision laws through the ``rust/brutex-calendar`` crate
behind the default-ON ``BRUTEX_CALENDAR_RUST`` flag, with each verbatim Python
law retained as the LIVE parity oracle (the ``notify_rust`` / ``lake_rust_stats``
template):

1. **Observed-sessions drift classify** — ``observed_sessions.classify_month_drift``
   (the pure admission → veto → month-cap-first / year-cap-mutually-exclusive
   core). The crate consumes ``(day_iso, zero_bars_all_indices,
   indices_with_month_data, clean_success_windows, failed_windows_touching)``
   evidence rows (the decision-inert ``month`` / ``detected_at`` /
   ``zero_bar_indices`` never cross — the caller re-attaches them).
2. **Settle-evidence validate** — ``settle_evidence.validate_evidence_payload``
   (the full truth-table validator; every refusal ``message`` byte-identical to
   the Python ``ValueError`` text — ``path_repr`` is passed PRE-``repr()``'d by
   the caller). The crate ABSTAINS (returns ``None``) when its serde engine
   refuses bytes CPython's ``json.loads`` accepted (NaN/Infinity literals,
   lone-surrogate escapes, deep nesting) — degrade to the Python validator.
3. **Observed-sidecar validate** — the whole-accept / whole-reject overlay
   validation half of ``observed_sessions.load_observed_holidays`` (version
   punning + entries-list + per-entry labeled-canonical, ONE bad entry rejects
   ALL). The crate's serde-divergence LOUD abstain (a raised ``ValueError``)
   is caught by the degrade set and served from the Python oracle.

## Why Pattern A (default-ON, Python oracle KEPT), not the #1962 endgame delete

DEC-RUST-CALENDAR-LEAVES-001 (#1962) named the future flip's endgame as "facade
flip + oracle DELETION + CI wheel wiring". This flip deliberately KEEPS the
Python oracles LIVE (Pattern A) rather than deleting them (Pattern B — the
``feed_rust`` / ``lake_rust_waved`` / ``refine_rust`` wheel-REQUIRED shape),
because the calendar laws are module-TOP imported into the WHEEL-FREE-BOOT
critical paths — pull-gating (``auto_pull_gate`` → ``settle_evidence``), the
dashboard, ``brutex doctor``, and the indicator layer all reach these modules,
and several must import + run WITHOUT the compiled wheel present (local dev, the
box's pre-build boot, the many ci.yml jobs that never install this wheel). A
wheel-required delete would break that boot. So the crate is OPTIONAL for
correctness: a wheel-absent process degrades to the byte-identical Python oracle
(ONE loud ``crate_unavailable`` WARN per process). This is the disclosed,
DEC-RUST-DEFAULT-ON-WAVE-001-sanctioned deviation from #1962's "oracle deletion"
token — the operator's keep-EVERY-safety-layer directive over an endgame delete.

## The fail-closed layers (the sibling-facade template)

* **Flag** — ``BRUTEX_CALENDAR_RUST`` default ON (DEC-RUST-DEFAULT-ON-WAVE-001):
  unset/blank/truthy are ON; an explicit falsy literal is the silent OFF kill
  switch; any other non-empty value is OFF with ONE loud ``calendar_rust.fallback``
  WARN (``flag_unrecognised``). Read per call (no global latch) so spawn workers
  resolve their own inherited environment. Explicit-OFF serves the oracle
  directly — the crate is NEVER imported (spy-pinned).
* **LEAF discipline** — this module imports ONLY stdlib + structlog +
  ``brutex.config.env_flags`` (the dependency-free COMMON flag-vocabulary leaf,
  importable from any layer — NOT ``brutex.data`` / ``brutex.engine``, which
  would risk a cycle from this low-level ``brutex.calendars`` leaf and drag the
  ``fno_math`` wheel into it). The law-specific dataclasses (``DriftVerdict`` /
  ``ObservedHoliday`` / ``SettleEvidence``) never appear here — the CALLERS own
  every marshalling callable (``rust_call`` / ``materialize`` / ``oracle``), so
  the facade never imports them and no import cycle exists.
* **Import guard / SYMBOL handshake** — the laws are stdlib-only, so the
  ``lake_rust_render`` polars VERSION guard is meaningless (the
  ``window_ledger_rust`` documented deviation): the stale-wheel guard is the
  symbol handshake — an absent wheel (including the shim's OWN stale-wheel
  ``ImportError``) latches ``crate_unavailable``; a wheel that imports but lacks
  any law callable latches ``crate_stale``. Both serve the oracle with one WARN.
* **Degrade set** — ``ValueError`` (covers the law-3 serde-divergence LOUD
  abstain + a malformed rust string reaching ``date.fromisoformat`` in
  ``materialize``) / ``RuntimeError`` (the crate's ``catch_unwind`` panic map) /
  ``TypeError`` / ``OverflowError`` (PyO3 int conversion — a beyond-i64 counter)
  / ``AttributeError`` (broken-crate-internals belt). ``MemoryError`` /
  ``KeyboardInterrupt`` / ``SystemExit`` re-raise raw (the errors.py doctrine),
  above the panic belt so its ``Exception`` member can never swallow an OOM.
* **Abstain-to-oracle** — ``materialize`` returning ``None`` (a crate ``None``
  return, or a refuse-poison structural surprise) serves the oracle. The law-2
  serde abstain (crate returns ``None``) is silent (the designed NaN/nesting
  degrade); the law-3 serde abstain (crate RAISES ``ValueError``) rides the
  degrade set with a warn — both correctness-neutral (the oracle is authoritative).
* **Refuse-poison** — a wrong-shaped / wrong-typed crate return is REFUSED to the
  oracle (``materialize`` returns ``None`` + one WARN): structurally unreachable
  through the real crate, guards a stale/foreign install.
* **Burn-in + autorevert** — the first :data:`BURN_IN_SAMPLES` engaged calls PER
  LAW compare the materialized Rust outcome against the live oracle outcome; ANY
  difference ships the ORACLE outcome and autoreverts the Rust path for the
  process (ONE ``calendar_rust.autoreverted`` ERROR — the #1531/#1532 pattern).

## Honesty (ADR-0020 banner; CLAUDE.md Golden Rule #10)

Expected payoff ≈ 0 — conversion is for uniformity, not speed. Every law is
micro-scale in Python and the FFI hop is a small SLOWDOWN; acceptable because the
laws run per drift-detection / per settle-evidence load / per overlay read (rare,
I/O-adjacent paths) and the engaged Rust path is guarded by the burn-in +
autorevert + degrade-to-oracle layers above. All I/O, logging, env resolution,
memoization and the ``frozenset``/dataclass materialization STAY Python.
"""

from __future__ import annotations

import importlib
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import structlog

from brutex.config.env_flags import env_flag_tristate

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import ModuleType

_LOG = structlog.get_logger(__name__)

#: The env flag (default ON per DEC-RUST-DEFAULT-ON-WAVE-001; explicit falsy =
#: the kill switch; junk fails loud-OFF).
CALENDAR_RUST_ENV: Final[str] = "BRUTEX_CALENDAR_RUST"

#: Engaged calls PER LAW that are outcome-compared against the live Python
#: oracle before Rust outcomes ship uncompared (the #1531/#1532 pattern).
BURN_IN_SAMPLES: Final[int] = 16

#: The classify-law burn-in budget key.
_LAW_CLASSIFY: Final[str] = "classify"
#: The settle-validate-law burn-in budget key.
_LAW_SETTLE: Final[str] = "settle"
#: The observed-sidecar-validate-law burn-in budget key.
_LAW_OBSERVED: Final[str] = "observed"

#: Process-local OFF latch — the disable reason, or ``None`` when live.
_DISABLED_REASON: Final[list[str | None]] = [None]

#: Per-law burn-in budgets (mutated in place; reset seam below).
_BURN_IN_REMAINING: Final[dict[str, int]] = {
    _LAW_CLASSIFY: BURN_IN_SAMPLES,
    _LAW_SETTLE: BURN_IN_SAMPLES,
    _LAW_OBSERVED: BURN_IN_SAMPLES,
}

#: The crate callables ALL laws need — the SYMBOL handshake surface. A wheel
#: missing any is a foreign/broken/stale install (``crate_stale``).
_CRATE_API: Final[tuple[str, ...]] = (
    "classify_month_drift",
    "parse_observed_sidecar",
    "validate_settle_evidence",
)

#: Once-per-process-per-reason log memo (a sticky degrade reason must never
#: log-storm a long-running dashboard / pull / sweep).
_OBSERVABILITY_MEMO: Final[set[str]] = set()

#: The engaged-INFO memo key (not a fallback reason — namespaced apart).
_ENGAGED_KEY: Final[str] = "__engaged__"

#: The narrow degrade-to-oracle catch set (module docstring). ``MemoryError``
#: deliberately absent — OOM re-raises raw, identical to the Python path.
_DEGRADE_ERRORS: Final[tuple[type[Exception], ...]] = (
    ValueError,
    RuntimeError,
    TypeError,
    OverflowError,
    AttributeError,
)


@dataclass(frozen=True, slots=True)
class Value[OutcomeT]:
    """A materialized law outcome carrying its value (compared by ``==``)."""

    value: OutcomeT


@dataclass(frozen=True, slots=True)
class Raise:
    """A materialized law outcome that RAISES ``ValueError(message)`` on unwrap.

    Stored as the message STRING (never the exception object): two ``ValueError``
    instances with equal messages are NOT ``==`` (identity-based), so the auto
    dataclass ``__eq__`` over the ``str`` gives the byte-exact-message burn-in
    comparison the design needs, and :func:`unwrap` re-raises a plain
    ``ValueError`` with byte-identical text.
    """

    message: str


#: The tagged law outcome — a value, or a deferred ``ValueError``.
type LawOutcome[OutcomeT] = Value[OutcomeT] | Raise


def unwrap[OutcomeT](outcome: LawOutcome[OutcomeT]) -> OutcomeT:
    """Return the value, or raise the deferred ``ValueError`` — byte-identical."""
    if isinstance(outcome, Raise):
        raise ValueError(outcome.message)
    return outcome.value


def calendar_rust_enabled(raw: str | None = None) -> bool:
    """Resolve ``BRUTEX_CALENDAR_RUST`` (default ON, junk loud-OFF).

    DEC-RUST-DEFAULT-ON-WAVE-001 (operator directive 2026-07-23): unset, blank
    and explicit truthy literals are ON; an explicit falsy literal is the silent
    OFF kill switch; any OTHER non-empty value is OFF with one
    ``calendar_rust.fallback`` WARN (``flag_unrecognised``) per process — an
    unrecognised value must never silently pick an execution path. Read per
    call, no global latch.

    Parameters
    ----------
    raw
        Explicit value to interpret (tests). ``None`` reads
        :data:`CALENDAR_RUST_ENV` from the environment.
    """
    value = os.environ.get(CALENDAR_RUST_ENV, "") if raw is None else raw
    verdict = env_flag_tristate(value)
    if verdict is not None:
        return verdict
    if value.strip():
        _warn_once("flag_unrecognised", env=CALENDAR_RUST_ENV, value=value)
        return False
    return True


def rust_calendar_disabled() -> bool:
    """``True`` iff a guard/autorevert latched the Rust path OFF this process."""
    return _DISABLED_REASON[0] is not None


def reset_calendar_rust_for_tests(*, remaining: int = BURN_IN_SAMPLES) -> None:
    """Reset latch + per-law burn-in budgets + warn memo (test isolation ONLY).

    ``remaining=0`` puts the process in the post-burn-in steady state (the Rust
    outcome ships with no oracle in the path — the differential suites depend on
    that). Never called from production code (the sibling facades' reset seam).
    """
    _DISABLED_REASON[0] = None
    for law in _BURN_IN_REMAINING:
        _BURN_IN_REMAINING[law] = remaining
    _OBSERVABILITY_MEMO.clear()


def _warn_once(reason: str, **context: object) -> None:
    """Log ``calendar_rust.fallback`` WARN once per process per *reason*."""
    if reason in _OBSERVABILITY_MEMO:
        return
    _OBSERVABILITY_MEMO.add(reason)
    _LOG.warning("calendar_rust.fallback", reason=reason, **context)


def warn_fallback(reason: str, **context: object) -> None:
    """Public once-per-process WARN seam for a caller ``materialize``.

    A caller's ``materialize`` returns ``None`` to ABSTAIN-to-oracle; when that
    abstain is a refuse-poison surprise (a wrong-shaped / wrong-typed crate
    return — structurally unreachable through the real crate, so a foreign /
    broken / stale install) it calls this FIRST so the degrade is LOUD once. A
    designed silent abstain (the law-2 serde ``None``) simply returns ``None``
    without calling this.
    """
    _warn_once(reason, **context)


def _disable(reason: str, *, error: bool = False, **context: object) -> None:
    """Latch the Rust path OFF for the remainder of the process.

    ``error=True`` logs ``calendar_rust.autoreverted`` at ERROR (the burn-in
    autorevert — a genuine wrongness signal); guard latches log
    ``calendar_rust.rust_path_disabled`` at WARN. Repeats are no-ops.
    """
    if _DISABLED_REASON[0] is not None:
        return
    _DISABLED_REASON[0] = reason
    if error:
        _LOG.error("calendar_rust.autoreverted", reason=reason, **context)
    else:
        _LOG.warning("calendar_rust.rust_path_disabled", reason=reason, **context)


def _panic_belt_errors() -> tuple[type[BaseException], ...]:
    """The narrow FFI panic-belt catch set — ``(Exception, *panic classes)``.

    ``pyo3_runtime.PanicException`` is the one ``BaseException`` subclass a pyo3
    boundary can leak; it must latch ``crate_panic`` and degrade to the oracle,
    but the repo convention (CodeQL ``py/catch-base-exception``) forbids a
    non-re-raising literal ``except BaseException``, so the class is resolved
    GUARDEDLY by name from its two plausible homes and folded beside
    ``Exception`` (the ``notify_rust`` / ``lake_rust_render`` local-twin
    technique — NOT imported from ``brutex.data``, which this wheel-free leaf
    must not pull in). ``Exception`` membership keeps every out-of-degrade-set
    ``Exception`` latching; ``KeyboardInterrupt`` / ``SystemExit`` /
    ``MemoryError`` re-raise in the clause ABOVE the belt.
    """
    belt: list[type[BaseException]] = [Exception]
    for home in ("pyo3_runtime", "polars.exceptions"):
        try:
            module = importlib.import_module(home)
        except ImportError:
            continue
        panic = getattr(module, "PanicException", None)
        if isinstance(panic, type) and issubclass(panic, BaseException) and panic not in belt:
            belt.append(panic)
    return tuple(belt)


def _resolve_crate() -> ModuleType | None:
    """Import guard + SYMBOL handshake: ``brutex_calendar``, or latch OFF + ``None``.

    An absent wheel (``ImportError`` — including the shim's OWN stale-wheel
    handshake refusal) latches ``crate_unavailable``. A wheel that imports but
    lacks any :data:`_CRATE_API` callable is a foreign/broken/stale install —
    latch ``crate_stale``. Fail-closed: never a new crash class.
    """
    try:
        crate = importlib.import_module("brutex_calendar")
    except ImportError as exc:
        _disable(
            "crate_unavailable",
            error_detail=repr(exc),
            remedy="pip install rust/brutex-calendar (or maturin develop --release there)",
        )
        return None
    for name in _CRATE_API:
        if not callable(getattr(crate, name, None)):
            _disable(
                "crate_stale",
                error_detail=f"brutex_calendar.{name} missing or not callable — "
                "the installed wheel predates the calendar laws (or is foreign/broken)",
                remedy="rebuild + reinstall rust/brutex-calendar",
            )
            return None
    return crate


def _log_engaged_once(crate: ModuleType) -> None:
    """Log ``calendar_rust.engaged`` INFO once per process."""
    if _ENGAGED_KEY in _OBSERVABILITY_MEMO:
        return
    _OBSERVABILITY_MEMO.add(_ENGAGED_KEY)
    _LOG.info("calendar_rust.engaged", crate_version=getattr(crate, "__version__", None))


def routed[OutcomeT](
    law: str,
    *,
    rust_call: Callable[[ModuleType], object],
    materialize: Callable[[object], LawOutcome[OutcomeT] | None],
    oracle: Callable[[], LawOutcome[OutcomeT]],
) -> LawOutcome[OutcomeT]:
    """The ONE routing engine every law wrapper delegates to (§11 COMMON).

    EVERY path returns a law-valid outcome: flag-OFF / latched / guarded /
    degraded / abstained / refuse-poison calls return ``oracle()`` (the live
    oracle), a burn-in mismatch returns the ORACLE outcome + autoreverts, and
    only a guard-clean, burn-in-clean call returns the materialized Rust outcome.

    Parameters
    ----------
    law
        The per-law burn-in budget key (one of ``_LAW_*``).
    rust_call
        ``crate -> raw``: the FFI call producing the crate's raw return.
    materialize
        ``raw -> LawOutcome | None``: re-hydrate the crate's raw into the law's
        typed outcome; return ``None`` to ABSTAIN-to-oracle (a crate ``None``,
        or a refuse-poison structural surprise + one WARN). Runs INSIDE the
        degrade guard, so a malformed rust string reaching ``date.fromisoformat``
        degrades rather than crashes.
    oracle
        ``() -> LawOutcome``: the caller's verbatim Python law, wrapped as the
        outcome — the live parity oracle. MUST be side-effect-free (no logging):
        it may run during burn-in on a clean engaged call.
    """
    if not calendar_rust_enabled() or rust_calendar_disabled():
        return oracle()
    crate = _resolve_crate()
    if crate is None:
        return oracle()
    try:
        outcome = materialize(rust_call(crate))
    except _DEGRADE_ERRORS as exc:
        _warn_once(f"{law}_error", error_detail=repr(exc))
        return oracle()
    except (KeyboardInterrupt, SystemExit, MemoryError):
        # The escape set (the errors.py doctrine): operator interrupt /
        # interpreter shutdown / OOM stay global — never a silent fallback.
        # Sits ABOVE the panic belt so its ``Exception`` member can never
        # swallow a MemoryError.
        raise
    except _panic_belt_errors() as exc:
        _disable("crate_panic", error_detail=repr(exc))
        return oracle()
    if outcome is None:
        # Abstain-to-oracle: crate ``None`` (law-2 serde abstain) or a
        # refuse-poison surprise (materialize already WARNed). Not an engaged
        # call — no burn-in decrement, no engaged INFO.
        return oracle()
    if _BURN_IN_REMAINING[law] > 0:
        oracle_outcome = oracle()
        if outcome != oracle_outcome:
            _disable(
                f"burn_in_mismatch_{law}",
                error=True,
                rust_outcome=repr(outcome),
                python_outcome=repr(oracle_outcome),
            )
            return oracle_outcome
        _BURN_IN_REMAINING[law] -= 1
        if _BURN_IN_REMAINING[law] == 0:
            _LOG.info("calendar_rust.burn_in_complete", law=law, samples=BURN_IN_SAMPLES)
    _log_engaged_once(crate)
    return outcome


__all__ = [
    "BURN_IN_SAMPLES",
    "CALENDAR_RUST_ENV",
    "_LAW_CLASSIFY",
    "_LAW_OBSERVED",
    "_LAW_SETTLE",
    # NOTE: ``LawOutcome`` (the PEP 695 ``type`` alias at module scope) is
    # deliberately NOT re-exported here — CodeQL's Python extractor does not
    # model ``type X = …`` statements as name bindings and false-flags it as an
    # undefined export (py/undefined-export). The alias remains defined and
    # importable by name (`from … import LawOutcome`), which is how the two
    # caller modules + the test suite consume it; ``__all__`` only governs
    # ``import *``, which no consumer uses for it.
    "Raise",
    "Value",
    "calendar_rust_enabled",
    "reset_calendar_rust_for_tests",
    "routed",
    "rust_calendar_disabled",
    "unwrap",
    "warn_fallback",
]
