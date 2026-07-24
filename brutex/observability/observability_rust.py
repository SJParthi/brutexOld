"""observability_rust — the default-ON Rust routing facade for the observability DECISION laws.

DEC-RUST-OBSERVABILITY-001 (MIGRATION.md row coordinator-assigned;
ADR-0020 lane); default flipped ON by DEC-RUST-DEFAULT-ON-WAVE-001
(operator directive 2026-07-23). Routes the four PURE observability decision laws through the
``rust/brutex-observability`` crate behind the ONE default-ON
``BRUTEX_OBSERVABILITY_RUST`` flag, with the verbatim Python law retained as
the LIVE parity oracle (the ``window_ledger_rust`` template; decide-in-Rust,
materialize-in-Python — the DEC-FEED-RUST-CORE-001 shape):

* **Law A** — ``loop_watchdog.check_once`` (the tiered
  OK→WARNED→CRITICAL→recovered stall state machine).
* **Law B** — ``watchdog.Watchdog.check_once``'s per-worker HEALTHY↔HUNG
  decision (``(elapsed, hung, edge)``).
* **Law C** — ``sla.SLADetector``'s predicate + hysteresis edge
  (``(breached, edge)``).
* **Laws D1/D2** — ``stage_stamp``'s strict-parse acceptance ORDER + the
  ``validate_stage_stamp`` verdict matrix. ``json.loads`` and
  ``content_hash_of`` deliberately STAY Python — the CPython-parse twin is
  the named follow-up ``DEC-RUST-OBSERVABILITY-STAMP-PARSE-003``, and hashlib.sha256
  is already native (streaming a Path into Rust would violate the
  scalars-only FFI discipline).

## The fail-closed layers (in order)

* **Flag** — ``BRUTEX_OBSERVABILITY_RUST`` default ON
  (DEC-RUST-DEFAULT-ON-WAVE-001); unset/blank/truthy → ON, falsy → OFF (the
  kill switch), any other non-empty value → OFF with ONE loud
  ``observability_rust.fallback`` WARN (``flag_unrecognised``). Read PER CALL
  (one ``os.environ`` probe) so spawn workers resolve their own inherited
  environment. Explicit-OFF returns the oracle's result DIRECTLY — the crate is never
  imported, so the kill-switch path is byte-identical to pre-facade behaviour
  (bomb-crate-spy-pinned), including law A's OK→OK state OBJECT identity.
* **Import guard / SYMBOL handshake** — an absent wheel latches
  ``crate_unavailable``; a wheel that imports but lacks ANY
  :data:`_CRATE_API` callable is a pre-lane (or foreign/broken) install and
  latches ``crate_stale``. Both serve the oracle with one loud WARN. The
  handshake includes the crate's own ``PanicException`` class (the panic
  belt's anchor — a pre-fix wheel without it reads ``crate_stale``, so the
  belt never runs against a wheel whose panic class it cannot resolve).
  **NO version guard — the documented deviation**: every routed law is
  polars-free pure arithmetic/string compare, so there is no third-party
  lockstep to attest; the symbol handshake + burn-in are the drift teeth
  (the stats/window_ledger/locks precedent).
* **Refuse-poison gates** — exact-type INPUT gates route non-``float``
  numerics (int/bool/Decimal — Python compares them against floats with
  EXACT arbitrary precision; an f64 round-trip is lossy) and non-``str``
  tiers to the oracle; a wrong-TYPE/wrong-vocabulary crate RETURN is refused
  to the oracle (never served — a poisoned crate can only degrade).
* **Degrade set** — ``ValueError``/``RuntimeError`` (the crate's declared
  FFI error surface; a lone-surrogate string's ``UnicodeEncodeError`` ⊂
  ``ValueError`` lands here, and so does the crate's checked ``episodes +
  1`` overflow refusal — "episodes counter overflow" at a warn edge on
  ``episodes == 2**64 - 1``, where the oracle's unbounded Python int is the
  honest value), ``TypeError``/``OverflowError`` (PyO3 argument conversion —
  a negative/out-of-u64 ``episodes`` int lands here), ``AttributeError``
  (broken-crate-internals belt). The ESCAPE clause
  (``KeyboardInterrupt``/``SystemExit``/``MemoryError``) re-raises RAW and
  sits ABOVE the panic belt so the belt's ``Exception`` member can never
  swallow an OOM. The panic belt (:func:`_panic_belt_errors`) resolves the
  crate's OWN per-extension ``PanicException`` class FIRST (plus the
  name-based sibling homes as fallback members), latches ``crate_panic``
  and serves the oracle.
* **Burn-in + autorevert** — the first :data:`BURN_IN_SAMPLES` engaged calls
  PER LAW bit-compare the Rust value against the live oracle
  (``struct.pack('<d', …)`` float legs — signed zeros + NaN payload bits
  distinguished, the #1531/#1532 discipline); ANY difference ships the
  ORACLE value and autoreverts the Rust path for the process (ERROR log).

## MemoryError scoping (where an OOM lands)

``MemoryError`` is NEVER degraded — it re-raises RAW from either arm, so it
lands exactly where the pure-Python path lands it today (ON ≡ OFF). For law
A it propagates into the caller's monitor thread
(``LoopWatchdog.sample_once`` has no blanket guard around ``check_once``).
For laws B–C driven through ``MonitorLoop.tick_once``, the loop's broad
``except Exception`` guards (the DEC-MONITOR-NEVER-DIE-001 never-die law)
CATCH the ``MemoryError`` — an ``Exception`` subclass — log "… raised; loop
continues" and keep ticking, identically on both arms; a direct
``Watchdog.check_once``/``SLADetector.check_once`` caller outside
``MonitorLoop`` sees the raw raise. For law D it propagates out of
``validate_stage_stamp``/``_parse_stamp`` exactly as today.

## Import-edge decision (the LOCAL twins)

This module must NOT import ``brutex.data.lake_rust_render``:
``brutex.data`` ⇄ ``brutex.observability`` package-level cycles are live in
both directions, and law B/C's host modules (``watchdog.py``/``sla.py``) are
deliberately brutex-import-free leaves. So the flag vocabulary
(:data:`_TRUTHY`/:data:`_FALSY`) and the panic belt
(:func:`_panic_belt_errors`) are LOCAL twins, mirror-pinned by test against
``lake_rust_render`` (the feed_rust precedent). The panic belt goes ONE
step further than the sibling fold: it resolves the crate's OWN exported
``PanicException`` class first (pyo3 panic classes are per-extension
statics — a name-resolved class can never be OUR wheel's; see the
:func:`_panic_belt_errors` docstring), keeping the sibling name-based homes
(``pyo3_runtime`` + ``polars.exceptions``) as fallback members so the
sibling fold stays a pinned SUBSET of ours. Module imports are stdlib +
structlog ONLY (loop_watchdog's leaf weight); the crate import is LAZY, so
this module imports clean with no wheel present (the repo-wide doctest
walker depends on that).

The same edge law covers the CONSUMER direction (the CodeQL
py/cyclic-import fix): the four consumer modules import THIS facade at
module level, so the facade must never import them back — not even
type-only (an ``if TYPE_CHECKING:`` import is still a module-level import
statement to a cyclic-import scan, and a consumer-first import order would
enter a partially-initialized facade). The consumer types are therefore
mirrored as facade-local STRUCTURAL stand-ins —
:class:`WatchdogStateLike` / :class:`WatchdogVerdictLike` for law A's
``loop_watchdog.WatchdogState``/``WatchdogVerdict`` and a ``bound=str``
predicate TypeVar for law C's ``sla.SLAPredicate`` (a ``StrEnum``) — and
the routed signatures are GENERIC over them, so the concrete consumer
types flow through the consumers' own call sites unchanged (the routers'
declared return types stay exact). Runtime-inert by construction: every
stand-in reference is an annotation under ``from __future__ import
annotations``, and the runtime decompose/recompose was ALREADY duck-typed
through the caller-passed factories (no-consumer-import AST-pinned).

## Log-event vocabulary (literal, greppable)

``observability_rust.fallback`` (WARN, once per reason) ·
``observability_rust.rust_path_disabled`` (WARN) ·
``observability_rust.autoreverted`` (ERROR) ·
``observability_rust.engaged`` (INFO, once) ·
``observability_rust.burn_in_complete`` (INFO, per law).

## Honesty (ADR-0020 banner; CLAUDE.md Golden Rule #10)

Expected payoff ≈ 0 — the conversion is for uniformity, not speed. Every
routed law is nanosecond-scale in Python and the FFI hop is a small
SLOWDOWN, acceptable because the seams run per monitor tick / per validate
call (never a hot mask path) and the engaged Rust path is guarded by the
burn-in + autorevert + degrade-to-oracle layers above
(DEC-RUST-DEFAULT-ON-WAVE-001, default ON). ONE lane flag covers all
five laws (task-mandated; a poisoned-crate autorevert latches ALL five
together — the window_ledger single-latch precedent, wider here). Flag-ON
recomposes law A's frozen dataclasses EQUAL but not IDENTICAL to the
oracle's (no consumer relies on identity; flag-OFF preserves it). The
warn-once/latch memos are deliberately UNLOCKED check-then-add — the
documented no-global-mutable-state exception (worst case: duplicate log
lines / extra burn-in compares, never a wrong value; thread-fuzz-pinned).
"""

from __future__ import annotations

import importlib
import os
import struct
from typing import TYPE_CHECKING, Any, Final, Protocol, TypeVar, runtime_checkable

import structlog

if TYPE_CHECKING:
    from types import ModuleType

_LOG = structlog.get_logger(__name__)

#: The env flag (default ON per DEC-RUST-DEFAULT-ON-WAVE-001; explicit falsy
#: = the kill switch; junk fails loud-OFF) — ONE flag for the
#: whole lane (module docstring).
OBSERVABILITY_RUST_ENV: Final[str] = "BRUTEX_OBSERVABILITY_RUST"

#: Engaged calls PER LAW that are bit-compared against the live Python
#: oracle before Rust values ship uncompared (the #1531/#1532 pattern).
BURN_IN_SAMPLES: Final[int] = 16

#: The law-A (loop_watchdog check_once) burn-in budget key.
_LAW_LOOP: Final[str] = "loop"
#: The law-B (watchdog decide) burn-in budget key.
_LAW_WATCHDOG: Final[str] = "watchdog"
#: The law-C (sla decide) burn-in budget key.
_LAW_SLA: Final[str] = "sla"
#: The law-D1 (stamp shape index) burn-in budget key.
_LAW_STAMP_SHAPE: Final[str] = "stamp_shape"
#: The law-D2 (stamp verdict) burn-in budget key.
_LAW_STAMP_VERDICT: Final[str] = "stamp_verdict"

#: Flag vocabulary — a LOCAL twin of ``lake_rust_render._TRUTHY``
#: (equality-pinned by test; module docstring's import-edge decision).
_TRUTHY: Final[frozenset[str]] = frozenset({"1", "true", "yes", "on"})

#: The falsy twin of :data:`_TRUTHY` (same local-twin rationale).
_FALSY: Final[frozenset[str]] = frozenset({"0", "false", "no", "off"})

#: Process-local OFF latch — EMPTY = live; latched holds the winning
#: disable reason at ``[0]`` (first call wins). DELIBERATE shape deviation
#: from the sibling facades' ``[None]`` single slot: the slot's
#: ``list[str | None]`` AnnAssign union mints a runtime-invisible mutmut
#: annotation mutant (PEP 563 — the annotation is never evaluated), an
#: unkillable breaker of the zero-survivor mutation gate; the #1614
#: endgame precedent restructures the line away rather than pragma it.
#: Semantics are unchanged (and the unlocked-race worst case improves:
#: two racing first latches APPEND, so ``[0]`` keeps the FIRST winner
#: instead of the slot shape's last-assign-wins).
_DISABLED_REASON: Final[list[str]] = []

#: Per-law burn-in budgets (mutated in place; reset seam below).
_BURN_IN_REMAINING: Final[dict[str, int]] = {
    _LAW_LOOP: BURN_IN_SAMPLES,
    _LAW_WATCHDOG: BURN_IN_SAMPLES,
    _LAW_SLA: BURN_IN_SAMPLES,
    _LAW_STAMP_SHAPE: BURN_IN_SAMPLES,
    _LAW_STAMP_VERDICT: BURN_IN_SAMPLES,
}

#: The crate callables the lane needs — the ALL-OR-NOTHING symbol handshake
#: surface. A wheel missing any is a pre-lane install (``crate_stale``).
#: ``PanicException`` (a class — callable) is DELIBERATELY part of the
#: handshake: the panic belt resolves the crate's OWN per-extension panic
#: class (:func:`_panic_belt_errors`), so a stale pre-fix wheel that lacks
#: the export must latch ``crate_stale`` — the belt never runs against a
#: wheel whose own panic class it cannot resolve (the crate_stale teeth of
#: the panic-containment fix).
_CRATE_API: Final[tuple[str, ...]] = (
    "loop_watchdog_check_once",
    "watchdog_decide",
    "sla_decide",
    "stamp_shape_index",
    "stamp_verdict",
    "PanicException",
)

#: Once-per-process-per-reason log memo (the seams run per monitor tick /
#: per validate call; a sticky reason must not flood the logs).
_OBSERVABILITY_MEMO: Final[set[str]] = set()

#: The engaged-INFO memo key (not a fallback reason — namespaced apart).
_ENGAGED_KEY: Final[str] = "__engaged__"

#: The narrow degrade-to-oracle catch set (module docstring). MemoryError
#: deliberately absent — OOM re-raises raw, identical to the Python path.
_DEGRADE_ERRORS: Final[tuple[type[Exception], ...]] = (
    ValueError,
    RuntimeError,
    TypeError,
    OverflowError,
    AttributeError,
)

#: The law-A crate wire width (the 12-scalar verdict tuple).
_LOOP_TUPLE_LEN: Final[int] = 12

#: The law-B crate wire width (``(elapsed, hung, edge)``).
_WATCHDOG_TUPLE_LEN: Final[int] = 3

#: The law-C crate wire width (``(breached, edge)``).
_SLA_TUPLE_LEN: Final[int] = 2

#: The law-D1 index vocabulary ceiling (``-1`` accept … ``5`` extra).
_STAMP_SHAPE_MAX_INDEX: Final[int] = 5

#: The only tier tokens a law-A crate return may carry (output gate).
_LOOP_TIERS: Final[frozenset[str]] = frozenset({"ok", "warned", "critical"})

#: The only tokens a law-D2 crate return may carry (output gate) — a junk
#: token would otherwise crash ``StampVerdict(token)`` with a ValueError the
#: pure path can never produce.
_STAMP_VERDICT_TOKENS: Final[frozenset[str]] = frozenset({"ok", "hash_mismatch"})


@runtime_checkable
class WatchdogStateLike(Protocol):
    """Structural stand-in for ``loop_watchdog.WatchdogState`` (read-only).

    The facade must never import its consumer modules — not even type-only
    (module docstring's import-edge decision; the CodeQL cyclic-import
    fix) — so the law-A state is typed by SHAPE: exactly the five scalar
    fields the routed decomposition reads. The production ``WatchdogState``
    satisfies this protocol structurally (drift-pinned by test); the
    generic routed signature carries the CONCRETE consumer type through
    unchanged. Read-only ``@property`` members so the frozen dataclass
    matches (a plain protocol variable would demand a settable attribute).
    """

    @property
    def tier(self) -> str:
        """The stall tier token (``ok`` / ``warned`` / ``critical``)."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed

    @property
    def stall_started_beat(self) -> float:
        """The last-beat value observed when the stall episode began."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed

    @property
    def last_logged_at(self) -> float:
        """The sample instant of the last stall log line."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed

    @property
    def last_alerted_at(self) -> float:
        """The sample instant of the last critical alert."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed

    @property
    def episodes(self) -> int:
        """The WARN-edge episode counter."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed


@runtime_checkable
class WatchdogVerdictLike(Protocol):
    """Structural stand-in for ``loop_watchdog.WatchdogVerdict`` (read-only).

    Exactly the members :func:`_loop_verdict_tuple` decomposes (the
    crate's 12-scalar order = the 5 state fields + these 7 scalars).
    Same cycle-free rationale as :class:`WatchdogStateLike`.
    """

    @property
    def state(self) -> WatchdogStateLike:
        """The new persistent state carried by the verdict."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed

    @property
    def lag_s(self) -> float:
        """The sampled heartbeat lag (clamped ≥ 0.0)."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed

    @property
    def warn_edge(self) -> bool:
        """Episode-start edge (WARN log + episode counter)."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed

    @property
    def critical_edge(self) -> bool:
        """Critical-tier edge (ERROR log + Telegram)."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed

    @property
    def relog(self) -> bool:
        """Periodic re-log while stalled."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed

    @property
    def realert(self) -> bool:
        """Periodic re-alert while critical."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed

    @property
    def recovered(self) -> bool:
        """Recovery edge (INFO with ``stall_total_s``)."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed

    @property
    def stall_total_s(self) -> float:
        """The total stall span reported on recovery."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed


# Variance-correct TypeVars for the generic law protocols + routers — the
# cycle-free replacement for importing the consumer types (module
# docstring's import-edge decision). TypeVar calls are RUNTIME expressions
# (unlike PEP 563 annotations), so their name strings + variance booleans
# are mutable by mutmut — every one is runtime-visible via the TypeVar's
# __name__/__bound__/__covariant__/__contravariant__ attributes and
# exact-pinned by test (the zero-survivor law).
_StateT_contra = TypeVar("_StateT_contra", bound=WatchdogStateLike, contravariant=True)
_StateT_co = TypeVar("_StateT_co", bound=WatchdogStateLike, covariant=True)
_VerdictT_co = TypeVar("_VerdictT_co", bound=WatchdogVerdictLike, covariant=True)
_PredicateT_contra = TypeVar("_PredicateT_contra", bound=str, contravariant=True)
_StateT = TypeVar("_StateT", bound=WatchdogStateLike)
_VerdictT = TypeVar("_VerdictT", bound=WatchdogVerdictLike)
_PredicateT = TypeVar("_PredicateT", bound=str)


class LoopCheckOnceLaw(Protocol[_StateT_contra, _VerdictT_co]):
    """The Python law-A oracle's call shape (``loop_watchdog._check_once_python``)."""

    def __call__(
        self,
        now: float,
        last_beat: float,
        state: _StateT_contra,
        *,
        warn_after_s: float,
        critical_after_s: float,
        relog_every_s: float,
        realert_every_s: float,
    ) -> _VerdictT_co:
        """Run one pure loop-watchdog decision."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed


class LoopStateFactory(Protocol[_StateT_co]):
    """The law-A state recomposer (production: the ``WatchdogState`` class)."""

    def __call__(
        self,
        *,
        tier: str,
        stall_started_beat: float,
        last_logged_at: float,
        last_alerted_at: float,
        episodes: int,
    ) -> _StateT_co:
        """Recompose a state dataclass from the crate's scalars."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed


class LoopVerdictFactory(Protocol[_StateT_contra, _VerdictT_co]):
    """The law-A verdict recomposer (production: the ``WatchdogVerdict`` class)."""

    def __call__(
        self,
        *,
        state: _StateT_contra,
        lag_s: float,
        warn_edge: bool,
        critical_edge: bool,
        relog: bool,
        realert: bool,
        recovered: bool,
        stall_total_s: float,
    ) -> _VerdictT_co:
        """Recompose a verdict dataclass from the crate's scalars."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed


class WatchdogDecideLaw(Protocol):
    """The Python law-B oracle's call shape (``watchdog._worker_decide_python``)."""

    def __call__(
        self, now: float, last_beat: float, timeout_seconds: float, prev_hung: bool
    ) -> tuple[float, bool, bool]:
        """Return ``(elapsed, hung, edge)`` for one worker."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed


class SlaDecideLaw(Protocol[_PredicateT_contra]):
    """The Python law-C oracle's call shape (``sla._sla_decide_python``)."""

    def __call__(
        self, predicate: _PredicateT_contra, value: float, threshold: float, prev_breach: bool
    ) -> tuple[bool, bool]:
        """Return ``(breached, edge)`` for one rule observation."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed


class StampShapeIndexLaw(Protocol):
    """The Python law-D1 oracle's call shape (``stage_stamp._stamp_shape_index_python``)."""

    def __call__(
        self,
        root_is_dict: bool,
        stage_is_str: bool,
        run_id_is_str: bool,
        hash_is_str: bool,
        produced_ok: bool,
        extra_ok: bool,
    ) -> int:
        """Return the first-reject index (``-1`` = accept)."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed


class StampVerdictLaw(Protocol):
    """The Python law-D2 oracle's call shape (``stage_stamp._stamp_verdict_python``)."""

    def __call__(
        self,
        recomputed: str,
        content_hash: str,
        stage: str,
        expected_stage: str,
        run_id: str,
        expected_run_id: str | None,
        stamped_snapshot: str | None,
        expected_snapshot_hash: str | None,
    ) -> str:
        """Return the verdict token (``"ok"`` / ``"hash_mismatch"``)."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed


def _env_flag_tristate(raw: str) -> bool | None:
    """Classify *raw* against the flag vocabulary (local twin — module docs).

    ``True`` for a :data:`_TRUTHY` literal, ``False`` for a :data:`_FALSY`
    literal, ``None`` for anything else (unset/empty AND unrecognised — the
    caller owns the default + warn policy). Byte-identical semantics to
    ``lake_rust_render.env_flag_tristate`` / the engine's canonical helper,
    equality-pinned in the unit suite; NOT imported from either home
    (module docstring's import-edge decision).
    """
    value = raw.strip().lower()
    if value in _TRUTHY:
        return True
    if value in _FALSY:
        return False
    return None


def observability_rust_enabled() -> bool:
    """Resolve ``BRUTEX_OBSERVABILITY_RUST`` (default ON, junk loud-OFF).

    DEC-RUST-DEFAULT-ON-WAVE-001 (operator directive 2026-07-23): truthy
    literal → ``True``; falsy literal → ``False`` (the kill switch);
    unset/blank → ``True`` (the default); any OTHER non-empty value →
    ``False`` with one ``observability_rust.fallback`` WARN
    (``flag_unrecognised``) per process — the junk arm is unchanged. Read
    per call so spawn workers resolve their own environment.
    """
    raw = os.environ.get(OBSERVABILITY_RUST_ENV, "")
    verdict = _env_flag_tristate(raw)
    if verdict is not None:
        return verdict
    if raw.strip():
        _warn_once("flag_unrecognised", value=raw)
        return False
    return True


def rust_observability_disabled() -> bool:
    """``True`` iff a guard/autorevert latched the Rust path OFF this process."""
    return bool(_DISABLED_REASON)


def reset_observability_rust_for_tests(*, remaining: int = BURN_IN_SAMPLES) -> None:
    """Reset latch + ALL five per-law burn-in budgets + warn memo.

    ``remaining=0`` puts the process in the post-burn-in steady state (the
    Rust value ships with no oracle in the path — the differential suites
    depend on that). Test isolation ONLY — never called from production
    paths (the sibling facades' reset-seam convention).
    """
    _DISABLED_REASON.clear()
    for law in _BURN_IN_REMAINING:
        _BURN_IN_REMAINING[law] = remaining
    _OBSERVABILITY_MEMO.clear()


def _warn_once(reason: str, **context: object) -> None:
    """Log ``observability_rust.fallback`` WARN once per process per *reason*."""
    if reason in _OBSERVABILITY_MEMO:
        return
    _OBSERVABILITY_MEMO.add(reason)
    _LOG.warning("observability_rust.fallback", reason=reason, **context)


def _disable(reason: str, *, error: bool = False, **context: object) -> None:
    """Latch the Rust path OFF for the remainder of the process.

    ``error=True`` logs ``observability_rust.autoreverted`` at ERROR (the
    burn-in autorevert — a genuine wrongness signal); guard latches log
    ``observability_rust.rust_path_disabled`` at WARN. Repeats are no-ops
    (first call wins).
    """
    if _DISABLED_REASON:
        return
    _DISABLED_REASON.append(reason)
    if error:
        _LOG.error("observability_rust.autoreverted", reason=reason, **context)
    else:
        _LOG.warning("observability_rust.rust_path_disabled", reason=reason, **context)


def _panic_belt_errors() -> tuple[type[BaseException], ...]:
    """The narrow FFI panic-belt catch set — ``(Exception, *panic classes)``.

    THE PER-EXTENSION-CLASS TRUTH: pyo3's ``PanicException`` is a
    per-extension-module STATIC — each abi3 wheel lazily inits its OWN type
    object (its module name reads ``pyo3_runtime`` cosmetically, but no such
    module is ever importable), so a class resolved BY NAME from
    ``pyo3_runtime`` or another extension (``polars.exceptions``) can NEVER
    be OUR wheel's class. The belt therefore resolves the crate's OWN
    exported ``PanicException`` FIRST (the ``brutex_observability`` home —
    the same lazy-import chokepoint :func:`_resolve_crate` guards; the class
    is part of the :data:`_CRATE_API` handshake, so an engaged crate always
    carries it) and keeps the two name-based homes as FALLBACK members —
    they cost nothing and keep the sibling-shape parity (the
    ``lake_rust_render`` fold stays a subset of ours, pinned by test; the
    sibling facades' name-based-only resolution is THEIR documented
    narrowing, deliberately not edited from this lane). The repo convention
    (CodeQL ``py/catch-base-exception``) forbids a non-re-raising literal
    ``except BaseException``, so every class is resolved GUARDEDLY and
    folded beside ``Exception``. Resolved lazily per raise (an absent wheel
    just skips the crate home); ``Exception`` membership keeps every
    out-of-degrade-set ``Exception`` latching, while ``KeyboardInterrupt``/
    ``SystemExit``/``MemoryError`` re-raise in the clause ABOVE the belt.
    (The formerly-disclosed ``episodes + 1`` overflow edge no longer
    panics at all — the crate's ``checked_add`` maps it to a ``ValueError``
    the degrade set serves; the belt guards any OTHER future panic.)
    """
    belt: list[type[BaseException]] = [Exception]
    for home in ("brutex_observability", "pyo3_runtime", "polars.exceptions"):
        try:
            module = importlib.import_module(home)
        except ImportError:
            continue
        panic = getattr(module, "PanicException", None)
        if isinstance(panic, type) and issubclass(panic, BaseException) and panic not in belt:
            belt.append(panic)
    return tuple(belt)


def _resolve_crate() -> ModuleType | None:
    """Import guard + SYMBOL handshake: ``brutex_observability``, or latch OFF.

    An absent wheel (``ImportError`` — including the shim's own stale-wheel
    handshake raise) latches ``crate_unavailable``. A wheel that imports but
    lacks ANY :data:`_CRATE_API` callable is a pre-lane (or foreign/broken)
    install — latch ``crate_stale``. Fail-closed: never a new crash class.
    """
    try:
        crate = importlib.import_module("brutex_observability")
    except ImportError as exc:
        _disable(
            "crate_unavailable",
            error_detail=repr(exc),
            remedy="pip install rust/brutex-observability (or `maturin develop --release` "
            "inside rust/brutex-observability with the project venv active)",
        )
        return None
    for name in _CRATE_API:
        if not callable(getattr(crate, name, None)):
            _disable(
                "crate_stale",
                error_detail=f"brutex_observability.{name} missing or not callable — "
                "the installed wheel predates the observability laws (or is foreign/broken)",
                remedy="rebuild + reinstall rust/brutex-observability "
                "(`maturin develop --release` with the project venv active)",
            )
            return None
    return crate


def _log_engaged_once(crate: ModuleType) -> None:
    """Log ``observability_rust.engaged`` INFO once per process."""
    if _ENGAGED_KEY in _OBSERVABILITY_MEMO:
        return
    _OBSERVABILITY_MEMO.add(_ENGAGED_KEY)
    _LOG.info(
        "observability_rust.engaged",
        crate_version=getattr(crate, "__version__", None),
    )


def _burn_in_check(law: str, rust_pack: object, oracle_pack: object, **context: object) -> bool:
    """One burn-in step for *law* — ``True`` iff the Rust value may ship.

    While the law's budget is positive the caller has ALREADY computed both
    canonical packs; a mismatch autoreverts (ERROR) and returns ``False``
    (ship the oracle), a clean compare decrements the budget and logs the
    ``burn_in_complete`` INFO at zero.
    """
    if rust_pack != oracle_pack:
        _disable(f"burn_in_mismatch_{law}", error=True, **context)
        return False
    _BURN_IN_REMAINING[law] -= 1
    if _BURN_IN_REMAINING[law] == 0:
        _LOG.info("observability_rust.burn_in_complete", law=law, samples=BURN_IN_SAMPLES)
    return True


def _pack_loop(v: tuple[Any, ...]) -> tuple[Any, ...]:
    """The law-A canonical burn-in pack over the 12-scalar verdict tuple.

    The ``struct.pack('<d', …)`` legs distinguish signed zeros and NaN
    payload bits (#1531/#1532 discipline); tuple equality on str/int/bool is
    exact. NaN payloads: both arms compute NaN via the same hardware IEEE
    ops — bit-equal on one host; a payload mismatch during burn-in
    autoreverts, which is the safe direction (disclosed, not papered over).
    """
    tier, ssb, lla, laa, eps, lag, we, ce, rl, ra, rec, tot = v
    return (
        tier,
        int(eps),
        bool(we),
        bool(ce),
        bool(rl),
        bool(ra),
        bool(rec),
        struct.pack("<d", ssb),
        struct.pack("<d", lla),
        struct.pack("<d", laa),
        struct.pack("<d", lag),
        struct.pack("<d", tot),
    )


def _loop_verdict_tuple(verdict: WatchdogVerdictLike) -> tuple[Any, ...]:
    """Decompose an oracle ``WatchdogVerdict`` into the crate's 12-scalar order."""
    state = verdict.state
    return (
        state.tier,
        state.stall_started_beat,
        state.last_logged_at,
        state.last_alerted_at,
        state.episodes,
        verdict.lag_s,
        verdict.warn_edge,
        verdict.critical_edge,
        verdict.relog,
        verdict.realert,
        verdict.recovered,
        verdict.stall_total_s,
    )


def _watchdog_result_ok(value: object) -> bool:
    """Law-B output refuse-poison gate — the exact ``(float, bool, bool)`` triple.

    A named helper like :func:`_loop_result_ok` (NOT an inline chain): the
    inline spelling duplicated the ``type(rust_value[1]) is bool`` line
    TEXT across the law-B and law-C gates, and mutmut 2.5.1 keys mutants
    by (line text, index) — a duplicated multi-mutation line text mints a
    PHANTOM registered-but-never-run cache row whose "untested" id trips
    the zero-survivor gate's ``^[0-9]+`` results grep exactly like a
    survivor. Distinct unpacked names keep every gate line text unique.
    """
    if type(value) is not tuple or len(value) != _WATCHDOG_TUPLE_LEN:
        return False
    elapsed, hung, edge = value
    return type(elapsed) is float and type(hung) is bool and type(edge) is bool


def _sla_result_ok(value: object) -> bool:
    """Law-C output refuse-poison gate — the exact ``(bool, bool)`` pair.

    Named for the same phantom-row reason as :func:`_watchdog_result_ok`.
    """
    if type(value) is not tuple or len(value) != _SLA_TUPLE_LEN:
        return False
    breached, edge = value
    return type(breached) is bool and type(edge) is bool


def _loop_result_ok(value: object) -> bool:
    """Law-A output refuse-poison gate — the exact 12-scalar shape + types."""
    if type(value) is not tuple or len(value) != _LOOP_TUPLE_LEN:
        return False
    tier, ssb, lla, laa, eps, lag, we, ce, rl, ra, rec, tot = value
    return (
        type(tier) is str
        and tier in _LOOP_TIERS
        and type(ssb) is float
        and type(lla) is float
        and type(laa) is float
        and type(eps) is int
        and type(lag) is float
        and type(we) is bool
        and type(ce) is bool
        and type(rl) is bool
        and type(ra) is bool
        and type(rec) is bool
        and type(tot) is float
    )


def routed_loop_check_once(  # noqa: UP047 — PEP 695 unsupported by the mutmut-2.5.1/parso-0.8 gate
    now: float,
    last_beat: float,
    state: _StateT,
    *,
    warn_after_s: float,
    critical_after_s: float,
    relog_every_s: float,
    realert_every_s: float,
    python_law: LoopCheckOnceLaw[_StateT, _VerdictT],
    state_factory: LoopStateFactory[_StateT],
    verdict_factory: LoopVerdictFactory[_StateT, _VerdictT],
) -> _VerdictT:
    """The routed law A — flag-gated Rust with the live Python oracle.

    EVERY path returns a valid ``WatchdogVerdict``: flag-OFF / latched /
    gate-refused / degraded calls return ``python_law(...)``'s object
    DIRECTLY (preserving the OK→OK arm's state OBJECT identity), a burn-in
    mismatch returns the ORACLE verdict + autoreverts, and only a
    guard-clean, burn-in-clean call returns the recomposed Rust verdict
    (EQUAL but not IDENTICAL frozen dataclasses — module docstring).

    The state is decomposed by duck attribute access and recomposed via the
    caller-passed *state_factory*/*verdict_factory* — the facade never
    imports its consumer modules (cycle-free by construction).
    """

    def _oracle() -> _VerdictT:
        return python_law(
            now,
            last_beat,
            state,
            warn_after_s=warn_after_s,
            critical_after_s=critical_after_s,
            relog_every_s=relog_every_s,
            realert_every_s=realert_every_s,
        )

    if not observability_rust_enabled() or rust_observability_disabled():
        return _oracle()
    tier = state.tier
    stall_started_beat = state.stall_started_beat
    last_logged_at = state.last_logged_at
    last_alerted_at = state.last_alerted_at
    episodes = state.episodes
    if not (
        type(now) is float
        and type(last_beat) is float
        and type(stall_started_beat) is float
        and type(last_logged_at) is float
        and type(last_alerted_at) is float
        and type(warn_after_s) is float
        and type(critical_after_s) is float
        and type(relog_every_s) is float
        and type(realert_every_s) is float
        and type(episodes) is int
        and type(tier) is str
    ):
        # Exact-type gate: Python compares int/Decimal against float with
        # arbitrary precision — an f64 round-trip could differ (module docs).
        _warn_once("loop_input_refused", tier_type=type(tier).__name__)
        return _oracle()
    crate = _resolve_crate()
    if crate is None:
        return _oracle()
    try:
        rust_value = crate.loop_watchdog_check_once(
            now,
            last_beat,
            tier,
            stall_started_beat,
            last_logged_at,
            last_alerted_at,
            episodes,
            warn_after_s,
            critical_after_s,
            relog_every_s,
            realert_every_s,
        )
    except _DEGRADE_ERRORS as exc:
        _warn_once("loop_error", error_detail=repr(exc))
        return _oracle()
    except (KeyboardInterrupt, SystemExit, MemoryError):
        # The escape set (the errors.py doctrine): operator interrupt /
        # interpreter shutdown / OOM stay global — never a silent fallback.
        # Sits ABOVE the panic belt so its `Exception` member can never
        # swallow a MemoryError.
        raise
    except _panic_belt_errors() as exc:
        _disable("crate_panic", error_detail=repr(exc))
        return _oracle()
    if not _loop_result_ok(rust_value):
        # Refuse-poison gate: a wrong-shape return is a crate BUG — never
        # served. Structurally unreachable through the real crate (its FFI
        # returns the typed 12-tuple); guards a stale/foreign install.
        _warn_once("loop_result_refused", result_type=type(rust_value).__name__)
        return _oracle()
    _log_engaged_once(crate)
    if _BURN_IN_REMAINING[_LAW_LOOP] > 0:
        oracle_verdict = _oracle()
        oracle_tuple = _loop_verdict_tuple(oracle_verdict)
        if not _burn_in_check(
            _LAW_LOOP,
            _pack_loop(tuple(rust_value)),
            _pack_loop(oracle_tuple),
            rust_value=list(rust_value),
            python_value=list(oracle_tuple),
        ):
            return oracle_verdict
    (r_tier, r_ssb, r_lla, r_laa, r_eps, r_lag, r_we, r_ce, r_rl, r_ra, r_rec, r_tot) = rust_value
    new_state = state_factory(
        tier=r_tier,
        stall_started_beat=r_ssb,
        last_logged_at=r_lla,
        last_alerted_at=r_laa,
        episodes=r_eps,
    )
    return verdict_factory(
        state=new_state,
        lag_s=r_lag,
        warn_edge=r_we,
        critical_edge=r_ce,
        relog=r_rl,
        realert=r_ra,
        recovered=r_rec,
        stall_total_s=r_tot,
    )


def routed_watchdog_decide(
    now: float,
    last_beat: float,
    timeout_seconds: float,
    prev_hung: bool,
    *,
    python_law: WatchdogDecideLaw,
) -> tuple[float, bool, bool]:
    """The routed law B — ``(elapsed, hung, edge)`` for one worker.

    EVERY path returns the triple: flag-OFF / latched / gate-refused /
    degraded calls return ``python_law(...)`` (the oracle), a burn-in
    mismatch returns the ORACLE triple + autoreverts, and only a
    guard-clean, burn-in-clean call returns the Rust triple.
    """
    if not observability_rust_enabled() or rust_observability_disabled():
        return python_law(now, last_beat, timeout_seconds, prev_hung)
    if not (type(now) is float and type(last_beat) is float and type(timeout_seconds) is float):
        # Fake clocks may return int — Python int arithmetic is exact; f64
        # is lossy past 2**53 (module docs).
        _warn_once("watchdog_input_refused", now_type=type(now).__name__)
        return python_law(now, last_beat, timeout_seconds, prev_hung)
    crate = _resolve_crate()
    if crate is None:
        return python_law(now, last_beat, timeout_seconds, prev_hung)
    try:
        rust_value = crate.watchdog_decide(now, last_beat, timeout_seconds, prev_hung)
    except _DEGRADE_ERRORS as exc:
        _warn_once("watchdog_error", error_detail=repr(exc))
        return python_law(now, last_beat, timeout_seconds, prev_hung)
    except (KeyboardInterrupt, SystemExit, MemoryError):
        raise
    except _panic_belt_errors() as exc:
        _disable("crate_panic", error_detail=repr(exc))
        return python_law(now, last_beat, timeout_seconds, prev_hung)
    if not _watchdog_result_ok(rust_value):
        _warn_once("watchdog_result_refused", result_type=type(rust_value).__name__)
        return python_law(now, last_beat, timeout_seconds, prev_hung)
    _log_engaged_once(crate)
    elapsed, hung, edge = rust_value
    if _BURN_IN_REMAINING[_LAW_WATCHDOG] > 0:
        oracle = python_law(now, last_beat, timeout_seconds, prev_hung)
        rust_pack = (struct.pack("<d", elapsed), bool(hung), bool(edge))
        oracle_pack = (struct.pack("<d", oracle[0]), bool(oracle[1]), bool(oracle[2]))
        if not _burn_in_check(
            _LAW_WATCHDOG,
            rust_pack,
            oracle_pack,
            rust_value=list(rust_value),
            python_value=list(oracle),
        ):
            return oracle
    return (elapsed, hung, edge)


def routed_sla_decide(  # noqa: UP047 — PEP 695 unsupported by the mutmut-2.5.1/parso-0.8 gate
    predicate: _PredicateT,
    value: float,
    threshold: float,
    prev_breach: bool,
    *,
    python_law: SlaDecideLaw[_PredicateT],
) -> tuple[bool, bool]:
    """The routed law C — ``(breached, edge)`` for one rule observation.

    ``predicate`` is the rule's ``SLAPredicate`` StrEnum member — a ``str``
    SUBCLASS, so PyO3 extracts its value directly and the oracle receives
    the SAME enum object. EVERY path returns the pair (oracle on flag-OFF /
    latched / gate-refused / degraded / burn-in-mismatch arms).
    """
    if not observability_rust_enabled() or rust_observability_disabled():
        return python_law(predicate, value, threshold, prev_breach)
    if not (type(value) is float and type(threshold) is float and isinstance(predicate, str)):
        # CRITICAL exact-type gate: providers are typed float | None but
        # Python ducks — int (exact arbitrary-precision compare vs float),
        # bool (int subclass), Decimal/Fraction (exact comparisons; PyO3
        # would coerce via __float__, LOSSY) all route to the oracle.
        _warn_once("sla_input_refused", value_type=type(value).__name__)
        return python_law(predicate, value, threshold, prev_breach)
    crate = _resolve_crate()
    if crate is None:
        return python_law(predicate, value, threshold, prev_breach)
    try:
        rust_value = crate.sla_decide(predicate, value, threshold, prev_breach)
    except _DEGRADE_ERRORS as exc:
        _warn_once("sla_error", error_detail=repr(exc))
        return python_law(predicate, value, threshold, prev_breach)
    except (KeyboardInterrupt, SystemExit, MemoryError):
        raise
    except _panic_belt_errors() as exc:
        _disable("crate_panic", error_detail=repr(exc))
        return python_law(predicate, value, threshold, prev_breach)
    if not _sla_result_ok(rust_value):
        _warn_once("sla_result_refused", result_type=type(rust_value).__name__)
        return python_law(predicate, value, threshold, prev_breach)
    _log_engaged_once(crate)
    breached, edge = rust_value
    if _BURN_IN_REMAINING[_LAW_SLA] > 0:
        oracle = python_law(predicate, value, threshold, prev_breach)
        if not _burn_in_check(
            _LAW_SLA,
            (bool(breached), bool(edge)),
            (bool(oracle[0]), bool(oracle[1])),
            rust_value=list(rust_value),
            python_value=list(oracle),
        ):
            return oracle
    return (breached, edge)


def routed_stamp_shape_index(
    root_is_dict: bool,
    stage_is_str: bool,
    run_id_is_str: bool,
    hash_is_str: bool,
    produced_ok: bool,
    extra_ok: bool,
    *,
    python_law: StampShapeIndexLaw,
) -> int:
    """The routed law D1 — the first-reject index (``-1`` = accept).

    Python probes each shape check into a bool (isinstance over the parsed
    JSON tree — runtime introspection that cannot cross the FFI); Rust owns
    the acceptance ORDER; Python materializes the exact error message for
    the returned index. EVERY path returns an index in ``[-1, 5]``.
    """
    if not observability_rust_enabled() or rust_observability_disabled():
        return python_law(
            root_is_dict, stage_is_str, run_id_is_str, hash_is_str, produced_ok, extra_ok
        )
    crate = _resolve_crate()
    if crate is None:
        return python_law(
            root_is_dict, stage_is_str, run_id_is_str, hash_is_str, produced_ok, extra_ok
        )
    try:
        rust_value = crate.stamp_shape_index(
            root_is_dict, stage_is_str, run_id_is_str, hash_is_str, produced_ok, extra_ok
        )
    except _DEGRADE_ERRORS as exc:
        _warn_once("stamp_shape_error", error_detail=repr(exc))
        return python_law(
            root_is_dict, stage_is_str, run_id_is_str, hash_is_str, produced_ok, extra_ok
        )
    except (KeyboardInterrupt, SystemExit, MemoryError):
        raise
    except _panic_belt_errors() as exc:
        _disable("crate_panic", error_detail=repr(exc))
        return python_law(
            root_is_dict, stage_is_str, run_id_is_str, hash_is_str, produced_ok, extra_ok
        )
    if type(rust_value) is not int or not -1 <= rust_value <= _STAMP_SHAPE_MAX_INDEX:
        # A bool (int subclass) or out-of-vocabulary index is refused — the
        # consumer maps the index to a raise arm, so a junk index would mint
        # a wrong (or missing) error class.
        _warn_once("stamp_shape_result_refused", result_type=type(rust_value).__name__)
        return python_law(
            root_is_dict, stage_is_str, run_id_is_str, hash_is_str, produced_ok, extra_ok
        )
    _log_engaged_once(crate)
    if _BURN_IN_REMAINING[_LAW_STAMP_SHAPE] > 0:
        oracle = python_law(
            root_is_dict, stage_is_str, run_id_is_str, hash_is_str, produced_ok, extra_ok
        )
        if not _burn_in_check(
            _LAW_STAMP_SHAPE,
            int(rust_value),
            int(oracle),
            rust_value=rust_value,
            python_value=oracle,
        ):
            return oracle
    return rust_value


def routed_stamp_verdict(
    recomputed: str,
    content_hash: str,
    stage: str,
    expected_stage: str,
    run_id: str,
    expected_run_id: str | None,
    stamped_snapshot: str | None,
    expected_snapshot_hash: str | None,
    *,
    python_law: StampVerdictLaw,
) -> str:
    """The routed law D2 — the verdict token (``"ok"`` / ``"hash_mismatch"``).

    A lone-surrogate string raises ``UnicodeEncodeError ⊂ ValueError``
    during pyo3 conversion → the degrade set → the oracle (which then
    compares Python-side — behavior preserved). EVERY path returns a token
    the consumer's ``StampVerdict(token)`` value-call accepts.
    """

    def _oracle() -> str:
        return python_law(
            recomputed,
            content_hash,
            stage,
            expected_stage,
            run_id,
            expected_run_id,
            stamped_snapshot,
            expected_snapshot_hash,
        )

    if not observability_rust_enabled() or rust_observability_disabled():
        return _oracle()
    crate = _resolve_crate()
    if crate is None:
        return _oracle()
    try:
        rust_value = crate.stamp_verdict(
            recomputed,
            content_hash,
            stage,
            expected_stage,
            run_id,
            expected_run_id,
            stamped_snapshot,
            expected_snapshot_hash,
        )
    except _DEGRADE_ERRORS as exc:
        _warn_once("stamp_verdict_error", error_detail=repr(exc))
        return _oracle()
    except (KeyboardInterrupt, SystemExit, MemoryError):
        raise
    except _panic_belt_errors() as exc:
        _disable("crate_panic", error_detail=repr(exc))
        return _oracle()
    if not (isinstance(rust_value, str) and rust_value in _STAMP_VERDICT_TOKENS):
        # A junk token would crash `StampVerdict(token)` with a ValueError
        # the pure path can never produce — refused to the oracle instead.
        _warn_once("stamp_verdict_result_refused", result_type=type(rust_value).__name__)
        return _oracle()
    _log_engaged_once(crate)
    if _BURN_IN_REMAINING[_LAW_STAMP_VERDICT] > 0:
        oracle = _oracle()
        if not _burn_in_check(
            _LAW_STAMP_VERDICT,
            str(rust_value),
            str(oracle),
            rust_value=rust_value,
            python_value=oracle,
        ):
            return oracle
    return rust_value


__all__ = [
    "BURN_IN_SAMPLES",
    "OBSERVABILITY_RUST_ENV",
    "LoopCheckOnceLaw",
    "LoopStateFactory",
    "LoopVerdictFactory",
    "SlaDecideLaw",
    "StampShapeIndexLaw",
    "StampVerdictLaw",
    "WatchdogDecideLaw",
    "WatchdogStateLike",
    "WatchdogVerdictLike",
    "observability_rust_enabled",
    "reset_observability_rust_for_tests",
    "routed_loop_check_once",
    "routed_sla_decide",
    "routed_stamp_shape_index",
    "routed_stamp_verdict",
    "routed_watchdog_decide",
    "rust_observability_disabled",
]
