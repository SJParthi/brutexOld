"""ONE COMMON Rust↔Python parity wrapper core (slice-4 M8).

The burn-in / autorevert pattern proven by :mod:`brutex.engine.rust_metrics`
(DEC-SWEEP-RUST-METRICS-001 / DEC-SWEEP-EXIT-INDEX-FASTPATH-001), EXTRACTED
into one reusable core BEFORE a second wrapper exists (CLAUDE.md §11 COMMON —
the slice-4 design's integration must-fix): the flag-gated Rust bindings
(:mod:`brutex.ranking.rust_ranking` now; the post-K2
``brutex.metrics.rust_result_metrics`` later) are thin consumers of THIS
module's latch / compare / taxonomy machinery, never a third copy. The
shipped :mod:`brutex.engine.rust_metrics` is deliberately NOT migrated onto
this core pre-K2 — its numeric semantics are FROZEN while K2 (fold_block) is
in flight (K2's S2 baseline + burn-in oracle reference its current
behaviour); its post-K2 re-home is a named follow-up, joint with
kernel-track.

## Per-family policy — burn-in for (a)/(b), ALWAYS-ON for (b*)

Each converted kernel family carries a :class:`ParityFamily` whose policy
mirrors its float-parity class (the slice-4 ruling, §1.1):

* :data:`POLICY_BURN_IN` — class (a)/(b) rows (theorem-backed bit-exact):
  the first :data:`BURN_IN_SAMPLES` kernel-served calls per process ALSO run
  the Python oracle and bit-compare (the #1531 template — a runtime
  TRIPWIRE; the guarantee is the proof + the CI adjudicators).
* :data:`POLICY_ALWAYS` — class (b*) rows (host-adjudicated pow-identity
  surfaces): EVERY call double-computes the Python oracle and bit-compares.
  Deliberately no sampled mode and no off mode while the family's flag is
  ON — the wire invariant "the served value is ALWAYS the Python oracle's
  value or a bit-identical Rust value" holds on every call by construction.
  Cost: double-compute forever on those rows — bounded, per-finalist,
  bench-pinned by the binding's differential-cost pin.

On ANY bit mismatch the family SHIPS THE ORACLE VALUE (correctness first —
a divergent bit can never reach a ranking, a persisted row, or a redo
comparison), latches itself OFF for the rest of the process
(``rust_parity.autoreverted`` ERROR — every later call short-circuits to
the oracle with a warn-once ``kernel_disabled`` fallback), and increments
the per-family divergence counter on the process-global
:class:`~brutex.observability.MetricRegistry`.

## Compare keys — bit-exact, never float ``==``

Ints compare by ``==`` (exact); floats compare by their IEEE-754 bit
pattern via ``struct.pack('<d', …)`` (:func:`pack_float` /
:func:`pack_floats` — signed zeros included, the same discipline as
:func:`brutex.engine.rust_metrics.drawdown_compare_key`); orderings compare
as index/verdict tuples; sequences element-wise. Each binding supplies the
key function for its family's value shape.

## Observability + state (the documented exceptions)

The warn-once memo, the engaged-once latch, the burn-in counter and the
autorevert latch are per-family LOG-VOLUME / routing state only — results
never depend on them (a mismatch ships the oracle value either way; the
latch only decides WHICH engine computes). ``reset_for_tests`` restores a
fresh-process view — the same documented exception to the
no-global-mutable-state rule as ``rust_metrics``' memos. The counters/latch
are plain writes (no lock): a racing thread can at worst run a handful of
extra self-checks — never a wrong result.

## Honest envelope (Golden Rule #10)

This core adds work, never removes it: burn-in double-computes its first 16
calls, always-on rows double-compute forever. Every wrapped function is
per-finalist / per-leaderboard rank-time code — NOT the sweep hot loop. No
speedup is claimed anywhere on this seam.
"""

from __future__ import annotations

import os
import struct
from collections.abc import Callable, Iterable, Sequence
from typing import Final, Literal

import structlog

from brutex.engine.sweep_lookahead import env_flag_tristate
from brutex.observability import get_registry

_LOG = structlog.get_logger("brutex.engine.rust_parity_core")

#: Burn-in policy — class (a)/(b) rows: first-N-per-process double-compute.
POLICY_BURN_IN: Final = "burn_in"
#: Always-on policy — class (b*) rows: EVERY call double-computes.
POLICY_ALWAYS: Final = "always"

ParityPolicy = Literal["burn_in", "always"]

#: Kernel-served calls bit-compared against the Python oracle per process
#: for a :data:`POLICY_BURN_IN` family (the rust_metrics
#: ``SELF_CHECK_SAMPLES`` precedent — bounded, amortised-nil).
BURN_IN_SAMPLES: Final[int] = 16

#: The per-family divergence counter (Prometheus). Registered lazily on
#: first increment — ``MetricRegistry.counter`` is idempotent on identical
#: re-registration, so repeated registration is safe.
DIVERGENCE_COUNTER: Final[str] = "brutex_rust_parity_divergence_total"

#: Memo key for the engaged-once INFO (namespaced apart from warn reasons).
_ENGAGED_KEY: Final[str] = "__engaged__"


class ParityFamily:
    """One kernel family's parity policy + latch + observability state.

    Parameters
    ----------
    name
        The family label (e.g. ``"ranking.composite"``) — carried on every
        log event and the divergence counter.
    policy
        :data:`POLICY_BURN_IN` for class (a)/(b) rows or
        :data:`POLICY_ALWAYS` for class (b*) rows (module docstring).
    burn_in_samples
        The per-process burn-in budget for a :data:`POLICY_BURN_IN` family
        (ignored by :data:`POLICY_ALWAYS` — its budget is every call).
    """

    __slots__ = ("_disabled_reason", "_memo", "_remaining", "name", "policy")

    def __init__(
        self, name: str, *, policy: ParityPolicy, burn_in_samples: int = BURN_IN_SAMPLES
    ) -> None:
        self.name = name
        self.policy: ParityPolicy = policy
        self._remaining = burn_in_samples
        self._disabled_reason: str | None = None
        self._memo: set[str] = set()

    def disabled(self) -> bool:
        """``True`` iff the autorevert latch has disabled this family."""
        return self._disabled_reason is not None

    def disable(self, reason: str, **context: object) -> None:
        """Latch the family OFF for the remainder of the process.

        The first call logs ``rust_parity.autoreverted`` (ERROR — a genuine
        kernel-wrongness signal, never throttled below the operator's view)
        and increments the family's divergence counter; repeat calls are
        no-ops (the latch is already set).
        """
        if self._disabled_reason is not None:
            return
        self._disabled_reason = reason
        registry = get_registry()
        registry.counter(
            DIVERGENCE_COUNTER,
            "Rust-vs-Python parity divergences (oracle shipped, family latched)",
            labels=("family",),
        )
        registry.inc(DIVERGENCE_COUNTER, labels=(self.name,))
        _LOG.error("rust_parity.autoreverted", family=self.name, reason=reason, **context)

    def self_check_pending(self) -> bool:
        """``True`` while this family still owes a kernel-vs-oracle compare.

        An :data:`POLICY_ALWAYS` family ALWAYS owes one (the (b*) wire
        invariant); a :data:`POLICY_BURN_IN` family owes one while its
        burn-in budget lasts.
        """
        if self.policy == POLICY_ALWAYS:
            return True
        return self._remaining > 0

    def consume_sample(self) -> None:
        """Consume one burn-in sample after a PASSED bit-compare.

        Clamped at zero (a racing extra call can never go negative); a
        FAILED compare never consumes — it latches :meth:`disable` instead.
        An :data:`POLICY_ALWAYS` family's budget is untouched (every call
        compares regardless).
        """
        if self.policy == POLICY_BURN_IN and self._remaining > 0:
            self._remaining -= 1

    def warn_fallback_once(self, reason: str, **context: object) -> None:
        """Log ``rust_parity.fallback`` ONCE per process per *reason*.

        A sticky reason (a stale wheel, a latched family) would otherwise
        flood the logs at leaderboard scale — the rust_metrics warn-once
        memo rationale. The fallback itself is bit-exact regardless (the
        Python oracle serves the call).
        """
        if reason in self._memo:
            return
        self._memo.add(reason)
        _LOG.warning("rust_parity.fallback", family=self.name, reason=reason, **context)

    def refuse_result_once(self, **context: object) -> None:
        """Log ``rust_parity.kernel_result_refused`` (ERROR) once per process.

        A domain-invalid kernel result indicates a kernel BUG — refused
        loudly, never adopted silently; the call is served by the oracle.
        """
        if "kernel_result_refused" in self._memo:
            return
        self._memo.add("kernel_result_refused")
        _LOG.error("rust_parity.kernel_result_refused", family=self.name, **context)

    def mark_engaged(self, **context: object) -> None:
        """Log ``rust_parity.engaged`` (INFO) once per process per family.

        The observable "the wired ON path actually runs Rust" signal the
        flag-ON golden legs assert (the vacuous-gate anti-trap).
        """
        if _ENGAGED_KEY in self._memo:
            return
        self._memo.add(_ENGAGED_KEY)
        _LOG.info("rust_parity.engaged", family=self.name, **context)

    def reset_for_tests(self, *, remaining: int = BURN_IN_SAMPLES) -> None:
        """Reset latch + burn-in budget + observability memo (tests ONLY).

        ``remaining=0`` puts a burn-in family in the post-burn-in steady
        state; the default restores a fresh-process view. Never called from
        production code paths.
        """
        self._remaining = remaining
        self._disabled_reason = None
        self._memo.clear()


def env_flag_enabled(env_name: str, *, default: bool, warn_family: ParityFamily) -> bool:
    """Resolve a per-call binding flag over the COMMON tristate vocabulary.

    The exact :func:`brutex.engine.rust_metrics.sweep_rust_metrics_enabled`
    resolution law, parameterised: a ``_TRUTHY`` value forces ON, a
    ``_FALSY`` value forces OFF (the explicit operator kill switch),
    unset/whitespace-empty falls to *default* (the one-constant flip
    lever), and any OTHER value fails SAFE to OFF with one warn-once
    ``flag_unrecognised`` event on *warn_family* — a typo'd kill switch
    must still KILL, never be silently ignored. Read per call: no global
    flag state, and every parallel worker reads its own environment.
    """
    raw = os.environ.get(env_name, "")
    verdict = env_flag_tristate(raw)
    if verdict is not None:
        return verdict
    if raw.strip():
        warn_family.warn_fallback_once("flag_unrecognised", env=env_name, value=raw)
        return False
    return default


def resolve_kernel_symbol(warn_family: ParityFamily, symbol: str) -> Callable[..., object] | None:
    """Return ``fno_math.<symbol>``, or ``None`` with a warn-once.

    Mirrors :func:`brutex.engine.rust_metrics._resolve_kernel`: a truly
    wheel-ABSENT environment warns ``wheel_missing``; a STALE wheel that
    imports but lacks the symbol warns ``wheel_stale``. Either way the
    caller serves the whole call from the verbatim Python path — wheel
    state can never change results or error behaviour.
    """
    try:
        import fno_math
    except ImportError:
        warn_family.warn_fallback_once("wheel_missing", symbol=symbol)
        return None
    kernel: Callable[..., object] | None = getattr(fno_math, symbol, None)
    if kernel is None:
        warn_family.warn_fallback_once("wheel_stale", symbol=symbol)
        return None
    return kernel


def pack_float(value: float) -> bytes:
    """The IEEE-754 bit pattern of one float (``struct.pack('<d', …)``).

    The bit-exact compare unit — a signed-zero or NaN-payload difference
    can never hide behind float equality (the
    :func:`brutex.engine.rust_metrics.drawdown_compare_key` discipline).
    """
    return struct.pack("<d", value)


def pack_floats(values: Iterable[float]) -> tuple[bytes, ...]:
    """Element-wise :func:`pack_float` over a float sequence."""
    return tuple(struct.pack("<d", v) for v in values)


def pack_float_rows(rows: Iterable[Sequence[float]]) -> tuple[tuple[bytes, ...], ...]:
    """Row-wise :func:`pack_floats` over a sequence of float rows."""
    return tuple(tuple(struct.pack("<d", v) for v in row) for row in rows)


def run_with_parity[T](
    family: ParityFamily,
    *,
    kernel: Callable[[], T],
    oracle: Callable[[], T],
    key: Callable[[T], object],
    validate: Callable[[T], bool] | None = None,
) -> T:
    """Serve one call under *family*'s parity policy (module docstring).

    Parameters
    ----------
    family
        The kernel family (policy + latch + memo state).
    kernel
        Zero-arg closure running the Rust kernel. May raise ``ValueError``
        (the kernel's own boundary arms) / ``TypeError`` / ``OverflowError``
        (PyO3 argument-conversion failures) — each degrades to the oracle
        with a warn-once ``kernel_error``; ``MemoryError`` re-raises BARE
        (OOM is a GLOBAL stop, DEC-LAKE-WRITE-HALT-001); any OTHER exception
        type PROPAGATES (a genuine code bug still raises — CLAUDE.md §4).
    oracle
        Zero-arg closure running the retained-forever Python original.
    key
        Maps a value to its BIT-exact comparison key (ints exact, floats
        via :func:`pack_float`-family helpers, orderings as tuples).
    validate
        Optional O(1)-ish domain check on the kernel value (shape/type) —
        the refuse-poison gate a post-burn-in (a)/(b) call needs (its value
        ships uncompared). ``False`` refuses the result loudly
        (``kernel_result_refused`` ERROR) and serves the oracle.

    Returns
    -------
    T
        The served value: the oracle's on any fallback/divergence, the
        (bit-identical) kernel value otherwise.
    """
    if family.disabled():
        family.warn_fallback_once("kernel_disabled")
        return oracle()
    try:
        kernel_value = kernel()
    except MemoryError:
        raise
    except (ValueError, TypeError, OverflowError) as exc:
        family.warn_fallback_once("kernel_error", error=repr(exc))
        return oracle()
    if validate is not None and not validate(kernel_value):
        family.refuse_result_once(result=repr(kernel_value))
        return oracle()
    if family.self_check_pending():
        oracle_value = oracle()
        if key(kernel_value) != key(oracle_value):
            # Ship the oracle value (correctness first — never a divergent
            # bit on the wire) and latch the family OFF process-wide.
            family.disable(
                "divergence",
                kernel_value=repr(kernel_value),
                oracle_value=repr(oracle_value),
            )
            return oracle_value
        family.consume_sample()
        family.mark_engaged()
        return kernel_value
    family.mark_engaged()
    return kernel_value


__all__ = [
    "BURN_IN_SAMPLES",
    "DIVERGENCE_COUNTER",
    "POLICY_ALWAYS",
    "POLICY_BURN_IN",
    "ParityFamily",
    "ParityPolicy",
    "env_flag_enabled",
    "pack_float",
    "pack_float_rows",
    "pack_floats",
    "resolve_kernel_symbol",
    "run_with_parity",
]
