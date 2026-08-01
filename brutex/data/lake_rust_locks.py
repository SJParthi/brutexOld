"""Default-ON Rust routing for the lake lock/atomic-replace primitives (ADR-0020 PR-4).

DEC-LAKE-RUST-LOCKS-ATOMIC-001 (MIGRATION.md #1912). The facade leaf for the
``rust/brutex-lake`` lock/atomic primitives, cloned from the PR-1 pattern
(:mod:`brutex.data.lake_rust_render`) MINUS two deliberately-dropped layers:

* **No polars version guard** — the locks primitives never touch polars
  (pure syscalls: ``flock`` / ``chmod`` / ``fsync`` / ``rename``), so the
  wheel↔crate polars lockstep is irrelevant here. The import guard + the
  :data:`_CRATE_API` surface check remain (a foreign/broken install latches
  OFF loudly, never an ``AttributeError`` mid-write).
* **No byte burn-in** — there are no rendered bytes to bit-compare; the
  parity contract is BEHAVIORAL (mutual exclusion, errno taxonomy, commit
  ordering, temp cleanup), proven by the unit + integration suites instead
  (the handoff's proof plan; DEC documents the deviation).

WHAT ROUTES (flag ``BRUTEX_LAKE_RUST_LOCKS``, default ON per
DEC-RUST-DEFAULT-ON-WAVE-001; explicit-falsy = kill switch; junk = loud OFF):

* :func:`rust_flock_attempt` — ONE non-blocking ``LOCK_EX`` attempt per
  call. The bounded retry loop, the poll sleep, the progress-beat emission
  and the ``PartitionLockTimeout`` deadline law all STAY in
  ``partition_lock._acquire_bounded`` (Python) — the handoff's sanctioned
  ALTERNATIVE FFI shape, chosen over the loop-in-Rust primary because a
  Rust-side loop would silently drop the watchdog liveness beats a
  contending pull must keep firing (DEC-PULL-CONTENTION-ANTISTALL-001
  GAP #15).
* :func:`rust_flock_release` — ``flock(LOCK_UN)`` + ``close`` (the
  ``os.close`` twin).
* :func:`rust_atomic_commit` — the chmod→``fsync``→``rename`` COMMIT of an
  already-written temp. Temp CREATION stays ``tempfile.mkstemp`` in BOTH
  arms (the ``.brutex-write-`` prefix law + the mkstemp-ENOSPC guard law
  stay byte-identical), the payload write stays the caller's ``write_to``
  callback, and the error-path temp cleanup stays the core's ``finally``.

WHAT NEVER ROUTES: the in-process ``BoundedLockRegistry`` (ADR-0020 PR-4 —
"in-process lock acquisition REMAINS on the Python facade"), the sha256
sidecar PATH derivation (``partition_lock_path`` — Python computes it and
opens the fd; the crate receives ONLY the fd, so a path-law drift through
Rust is structurally impossible), :func:`~brutex.data.atomic_io.lake_write_guard`
(the ONE error-taxonomy mapper — the crate raises real ``OSError`` with the
errno intact and the guard does the mapping), timeout/env resolution, and
every consumer-facing signature.

Fail-closed degrade law (the PR-1 posture): flag OFF / junk / latched /
absent wheel / API-missing crate / crate fault / FFI panic — each serves
the VERBATIM Python arm with at most one loud WARN per process per reason;
a real ``OSError`` (errno taxonomy) and ``MemoryError`` (OOM → the guard's
``LakeWriteHalt``) always PROPAGATE from either arm — never latched, never
swallowed (Golden Rule #1).

Honest envelope (Golden Rule #10): a uniformity conversion (ADR-0020's
banner — "expected payoff ≈ 0"), not a speedup; the routed arm issues the
SAME syscalls; a wedged ``fsync`` on a dead volume is equally unkillable
from either arm (the #999 zombie envelope).
"""

from __future__ import annotations

import contextlib
import importlib
import os
from typing import TYPE_CHECKING, Final

import structlog

from brutex.config.rust_strict import refuse_if_strict
from brutex.data.lake_rust_render import env_flag_tristate

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path
    from types import ModuleType

_LOG = structlog.get_logger(__name__)

__all__ = [
    "LAKE_RUST_LOCKS_ENV",
    "lake_rust_locks_enabled",
    "reset_lake_rust_locks_for_tests",
    "rust_atomic_commit",
    "rust_flock_attempt",
    "rust_flock_release",
    "rust_locks_disabled",
]

#: The env flag. Default ON per DEC-RUST-DEFAULT-ON-WAVE-001; the
#: explicit-falsy value is the kill switch; junk values are LOUD-OFF (one WARN).
LAKE_RUST_LOCKS_ENV: Final = "BRUTEX_LAKE_RUST_LOCKS"

#: The crate API surface the import guard verifies — a module that imports
#: but lacks any of these is a foreign/broken install and latches
#: ``crate_unavailable`` instead of raising ``AttributeError`` mid-write.
_CRATE_API: Final[tuple[str, ...]] = (
    "atomic_commit_replace",
    "lock_release",
    "lock_try_exclusive",
)

#: The process-wide autorevert latch (``None`` = healthy). A single-slot
#: list, not a module global rebind — the PR-1 idiom.
_DISABLED_REASON: Final[list[str | None]] = [None]

#: Once-per-process-per-reason warn memo + the engaged-once INFO latch.
#: LOG-VOLUME state ONLY: lock/write behavior never depends on it.
_OBSERVABILITY_MEMO: Final[set[str]] = set()

#: Memo key for the engaged-once INFO (namespaced apart from warn reasons).
_ENGAGED_KEY: Final = "__engaged__"


def lake_rust_locks_enabled() -> bool:
    """Resolve ``BRUTEX_LAKE_RUST_LOCKS`` (default ON per
    DEC-RUST-DEFAULT-ON-WAVE-001, operator directive 2026-07-23; junk fails
    loud-OFF).

    * a truthy literal (``1/true/yes/on``) → ``True`` (explicit ON);
    * a falsy literal (``0/false/no/off``) → ``False`` (explicit, silent
      OFF — the kill switch);
    * unset / whitespace-empty → ``True`` (the default);
    * any OTHER non-empty value → ``False`` with one loud
      ``lake_rust_locks.fallback`` WARN (reason ``flag_unrecognised``) per
      process — a typo'd opt-in must be VISIBLE, never silently ignored.

    Read per call (one ``os.environ`` probe per lock acquire / atomic
    commit) so parallel workers resolve their own inherited environment —
    no global flag state. The flag vocabulary is the ONE
    :func:`~brutex.data.lake_rust_render.env_flag_tristate` (imported, never
    copied — CLAUDE.md §11 COMMON; the render leaf is import-light).
    """
    raw = os.environ.get(LAKE_RUST_LOCKS_ENV, "")
    verdict = env_flag_tristate(raw)
    if verdict is not None:
        return verdict
    if raw.strip():
        _warn_once("flag_unrecognised", value=raw)
        return False
    return True


def rust_locks_disabled() -> bool:
    """Return ``True`` iff a guard/autorevert latched the Rust path OFF this process."""
    return _DISABLED_REASON[0] is not None


def reset_lake_rust_locks_for_tests() -> None:
    """Reset the latch + warn memo (test isolation ONLY — never production)."""
    _DISABLED_REASON[0] = None
    _OBSERVABILITY_MEMO.clear()


def _warn_once(reason: str, **context: object) -> None:
    """Log ``lake_rust_locks.fallback`` WARN once per process per *reason*.

    DEC-RUST-STRICT-NO-FALLBACK-001: refuses under ``BRUTEX_RUST_STRICT``.
    Every reason funnelled here is CLASS A (``flag_unrecognised`` — the
    junk-flag arm; ``crate_unavailable`` — the wheel is absent). There is no
    class-C reason at this choke point, so no exemption set: the seams take
    only ints (an fd, a path pair) that the crate always claims. Strict-OFF is
    byte-identical to the prior behaviour.

    The seams run once per lock acquire / atomic write (thousands per
    pull), so a sticky reason would flood the logs; the first occurrence is
    loud, repeats fall through silently (the Python arm serves regardless).
    """
    refuse_if_strict("brutex.data.lake_rust_locks", reason, **context)
    if reason in _OBSERVABILITY_MEMO:
        return
    _OBSERVABILITY_MEMO.add(reason)
    _LOG.warning("lake_rust_locks.fallback", reason=reason, **context)


def _note_engaged() -> None:
    """Log ``lake_rust_locks.engaged`` INFO once per process (the PR-1 idiom)."""
    if _ENGAGED_KEY in _OBSERVABILITY_MEMO:
        return
    _OBSERVABILITY_MEMO.add(_ENGAGED_KEY)
    _LOG.info("lake_rust_locks.engaged")


def _disable(reason: str, *, error: bool = False, **context: object) -> None:
    """Refuse under strict mode; else latch the Rust path OFF for the process.

    DEC-RUST-STRICT-NO-FALLBACK-001. Reasons here are CLASS A
    (``crate_panic``; ``flock_attempt_failed`` / ``flock_release_failed`` /
    ``atomic_commit_failed`` — the crate raised mid-operation); none is class
    C, so none is exempt. The refusal fires BEFORE the latch is written, so
    under strict mode the latch is never set and every ``lake_rust_locks_
    disabled()`` early-return is structurally unreachable.

    CONSEQUENCE, stated honestly: the refusal replaces the Python arm that
    would otherwise have run AFTER the announcement. For ``flock_release_
    failed`` that arm is ``python_close(fd)`` — under strict mode the fd is
    left open and the kernel releases the flock at process exit (a leaked
    descriptor on a fatal path, never a stuck lock). For
    ``atomic_commit_failed`` the arm is ``python_commit(...)`` — the temp file
    is left in place and the destination is untouched (no partial write, no
    corruption; the caller's own error path removes the temp). Both are
    strictly safer than silently completing an operation the Rust arm was
    supposed to own. Strict-OFF is byte-identical to the prior behaviour.

    ``error=True`` logs ``lake_rust_locks.locks_autoreverted`` at ERROR (a
    crate fault mid-operation — a genuine wrongness signal, never throttled
    below the operator's view); guard latches log
    ``lake_rust_locks.rust_path_disabled`` at WARN. Repeat calls are no-ops
    (the latch is already set).
    """
    refuse_if_strict("brutex.data.lake_rust_locks", reason, error=error, **context)
    if _DISABLED_REASON[0] is not None:
        return
    _DISABLED_REASON[0] = reason
    if error:
        _LOG.error("lake_rust_locks.locks_autoreverted", reason=reason, **context)
    else:
        _LOG.warning("lake_rust_locks.rust_path_disabled", reason=reason, **context)


def _resolve_crate() -> ModuleType | None:
    """Import guard: return ``brutex_lake``, or latch OFF + ``None``.

    An absent wheel (``ImportError``) latches ``crate_unavailable`` — the
    facade is default-ON with degrade-to-oracle, so a box that engaged the
    default (or explicitly opted in) without a built crate gets ONE loud
    WARN naming the remedy, and every lock/write serves the Python arm. A module that IMPORTS but lacks
    any :data:`_CRATE_API` callable (a name collision / hand-broken /
    PRE-PR-4 STALE install missing the lock symbols) latches the SAME
    reason — the fail-closed contract is "never a new crash class", so an
    ``AttributeError`` must never leave this seam. NO version guard: the
    locks primitives are polars-free (module docstring — the ONE deliberate
    PR-1-pattern deviation beside the dropped burn-in).
    """
    try:
        crate = importlib.import_module("brutex_lake")
    except ImportError as exc:
        _disable(
            "crate_unavailable",
            error_detail=repr(exc),
            remedy="pip install the brutex-lake wheel (maturin build in rust/brutex-lake)",
        )
        return None
    for name in _CRATE_API:
        if not callable(getattr(crate, name, None)):
            _disable(
                "crate_unavailable",
                error_detail=f"crate module lacks callable {name!r} (stale/foreign install)",
                remedy="rebuild + reinstall the brutex-lake wheel (PR-4 added the lock symbols)",
            )
            return None
    return crate


def _degrade_errors() -> tuple[type[Exception], ...]:
    """The narrow degrade-to-Python-arm catch set for an engaged crate call.

    ``RuntimeError`` is the crate's declared panic mapping (every primitive
    wraps ``catch_unwind`` at the FFI boundary); ``ValueError`` /
    ``TypeError`` / ``OverflowError`` are PyO3 argument-conversion
    failures; ``AttributeError`` is the broken-crate-internals defensive
    belt (the import guard already refuses a module MISSING the API).
    ``OSError`` and ``MemoryError`` are DELIBERATELY absent: a real errno
    (``ENOSPC``/``EACCES``/``EBADF``…) and an OOM must PROPAGATE exactly as
    the Python arm raises them — the callers' ``lake_write_guard`` /
    except-arms own that taxonomy (Golden Rule #1: a host condition is
    never laundered into a silent Rust-path degrade).
    """
    return (ValueError, RuntimeError, TypeError, OverflowError, AttributeError)


def _panic_belt_errors() -> tuple[type[BaseException], ...]:
    """The FFI panic-belt catch set — ``(Exception, *guarded panic classes)``.

    The local twin of ``lake_rust_render._panic_belt_errors`` (that helper
    is private to the render leaf; the resolution logic is 10 lines and the
    docstring rationale there applies verbatim — see it for the full
    honest-narrowing discussion). Every crate primitive carries its OWN
    ``catch_unwind`` → ``RuntimeError`` mapping, so this belt is
    defense-in-depth for a panic escaping that boundary
    (``pyo3_runtime.PanicException`` is a ``BaseException`` subclass a raw
    ``except Exception`` would miss). ``Exception`` membership keeps every
    out-of-degrade-set ``Exception`` latching; ``OSError``/``MemoryError``
    re-raise in the clause ABOVE the belt at every call site, so the belt
    never swallows a real errno or an OOM.
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


def rust_flock_attempt(python_attempt: Callable[[int], bool]) -> Callable[[int], bool]:
    """Return the routed one-shot flock attempt (``fd -> acquired?``).

    Flag OFF / junk / latched / unresolvable crate → *python_attempt*
    returned UNCHANGED (identity — the OFF arm's call graph is the Python
    arm's, spy-pinned). ON + healthy → a wrapper that tries the crate's
    ``lock_try_exclusive`` per call and degrades PER CALL: a crate fault /
    panic latches the process OFF (loud) and serves *python_attempt* for
    this and every later attempt — a mid-retry-loop degrade converges to
    the oracle without restarting the caller's deadline. A would-block
    ``BlockingIOError`` from a foreign crate folds to ``False`` (the
    not-acquired verdict — parity belt); a real ``OSError`` / ``MemoryError``
    PROPAGATES (the Python arm's ``fcntl.flock`` raises them raw too).

    The caller (``partition_lock._acquire_bounded``) owns the retry loop,
    the poll sleep, the progress beats and the ``PartitionLockTimeout``
    law — this seam is attempt-grained by design (module docstring).
    """
    if not lake_rust_locks_enabled() or rust_locks_disabled():
        return python_attempt
    crate = _resolve_crate()
    if crate is None:
        return python_attempt

    def _attempt(fd: int) -> bool:
        if rust_locks_disabled():  # a mid-loop latch converges to the oracle
            return python_attempt(fd)
        try:
            acquired = bool(crate.lock_try_exclusive(fd))
        except BlockingIOError:
            return False  # parity belt: would-block is "not acquired", never a fault
        except (MemoryError, OSError):
            raise  # real errno taxonomy / OOM — parity with the Python arm
        except _degrade_errors() as exc:
            _disable("flock_attempt_failed", error=True, error_detail=repr(exc))
            return python_attempt(fd)
        except _panic_belt_errors() as exc:
            _disable("crate_panic", error=True, error_detail=repr(exc))
            return python_attempt(fd)
        _note_engaged()
        return acquired

    return _attempt


def rust_flock_release(fd: int, *, python_close: Callable[[int], None]) -> None:
    """Release + close the lock *fd* (routed ``os.close`` twin).

    Flag OFF / junk / latched / unresolvable crate → ``python_close(fd)``
    verbatim. ON + healthy → the crate's ``lock_release`` (``LOCK_UN`` +
    ``close``); a real ``OSError`` (the close's own errno) PROPAGATES —
    parity: the Python arm's ``os.close`` raises too. On a crate fault /
    panic the process latches OFF (loud) and the fallback close is
    BEST-EFFORT (``OSError`` suppressed on that fallback ONLY: the faulted
    crate may or may not have reached its own ``close``, and a double-close
    ``EBADF`` must never mask the already-latched fault; the kernel
    releases an flock on process exit regardless — the no-stale-lock law).
    """
    if not lake_rust_locks_enabled() or rust_locks_disabled():
        python_close(fd)
        return
    crate = _resolve_crate()
    if crate is None:
        python_close(fd)
        return
    try:
        crate.lock_release(fd)
    except (MemoryError, OSError):
        raise
    except _degrade_errors() as exc:
        _disable("flock_release_failed", error=True, error_detail=repr(exc))
        with contextlib.suppress(OSError):
            python_close(fd)
    except _panic_belt_errors() as exc:
        _disable("crate_panic", error=True, error_detail=repr(exc))
        with contextlib.suppress(OSError):
            python_close(fd)


def rust_atomic_commit(
    tmp: Path,
    dest: Path,
    chmod: int | None,
    *,
    python_commit: Callable[[Path, Path, int | None], None],
) -> None:
    """Route the chmod→``fsync``→``rename`` commit of an already-written temp.

    Flag OFF / junk / latched / unresolvable crate → ``python_commit(tmp,
    dest, chmod)`` verbatim (the oracle arm — ``atomic_io``'s extracted
    commit tail, byte-order-identical to the pre-PR-4 inline dance). ON +
    healthy → the crate's ``atomic_commit_replace``; a real ``OSError``
    (``ENOSPC``/``EDQUOT``/``EACCES``…) and ``MemoryError`` PROPAGATE with
    the errno intact — the caller's ``lake_write_guard`` owns the taxonomy
    (``atomic_write_via``) or the caller maps its own domain error
    (``atomic_write_simple``), exactly as on the Python arm. On a crate
    fault / panic the process latches OFF (loud) and *python_commit*
    finishes the SAME commit over the still-intact temp — the payload is
    never re-rendered and ``write_to`` is never re-run (the rename is the
    crate's LAST step, so a fault-class failure leaves the temp in place;
    the one pathological renamed-then-panicked shape surfaces as the
    oracle's own loud ``FileNotFoundError`` with *dest* already correct —
    disclosed, never silent).
    """
    if not lake_rust_locks_enabled() or rust_locks_disabled():
        python_commit(tmp, dest, chmod)
        return
    crate = _resolve_crate()
    if crate is None:
        python_commit(tmp, dest, chmod)
        return
    try:
        crate.atomic_commit_replace(str(tmp), str(dest), chmod)
    except (MemoryError, OSError):
        raise
    except _degrade_errors() as exc:
        _disable("atomic_commit_failed", error=True, error_detail=repr(exc))
        python_commit(tmp, dest, chmod)
        return
    except _panic_belt_errors() as exc:
        _disable("crate_panic", error=True, error_detail=repr(exc))
        python_commit(tmp, dest, chmod)
        return
    _note_engaged()
