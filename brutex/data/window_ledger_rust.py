"""window_ledger_rust — the default-ON Rust routing facade for the window-ledger PURE laws.

DEC-LAKE-RUST-WINDOW-LEDGER-001 (ADR-0020 Wave C-3, slice G8 FIRST CUT;
MIGRATION.md #1915); default flipped ON by DEC-RUST-DEFAULT-ON-WAVE-001
(operator directive 2026-07-23). Routes the TWO pure window-ledger laws
through the ``rust/brutex-lake`` crate (``brutex_lake.classify_window_status``
+ ``brutex_lake.date_isoformat``) behind the default-ON
``BRUTEX_LAKE_RUST_WINDOW_LEDGER`` flag, with the verbatim Python law
retained as the LIVE parity oracle (the ADR-0020 / PR-1
``lake_rust_render`` template):

1. **Status classification** — ``window_status.classify_window_status``
   (error → ``FAILED``; clean bars → ``FAILED`` if ``degraded`` else
   ``SUCCESS``; else ``EMPTY``).
2. **Merge-key law** — the 5-tuple window identity ``WindowLedger._key`` /
   ``WindowStatusLog._key``: ``(market, security_id, interval,
   start.isoformat(), end.isoformat())``. ONLY the two computed date
   strings are rendered in Rust; the three identity strings NEVER cross the
   FFI — this facade assembles the tuple from the ORIGINAL Python objects,
   so those elements are byte-identical by construction (the
   DEC-FEED-RUST-CORE-001 ints-in / computed-ASCII-out discipline).

## The fail-closed layers (the PR-1 template, adapted)

* **Flag** — ``BRUTEX_LAKE_RUST_WINDOW_LEDGER`` default ON per
  DEC-RUST-DEFAULT-ON-WAVE-001; truthy → ON, unset → ON (the default),
  falsy → the silent OFF kill switch, any other non-empty value → OFF with
  ONE loud ``window_ledger_rust.fallback`` WARN (``flag_unrecognised``). OFF returns
  the oracle's result directly — the crate is never imported, so the
  default path is byte-identical to pre-facade behaviour (spy-pinned).
* **Import guard / SYMBOL handshake** — the laws are polars-free, so the
  PR-1 polars VERSION guard is meaningless here (a DELIBERATE, disclosed
  deviation): the stale-wheel guard is instead the symbol handshake — an
  absent wheel latches ``crate_unavailable``; a wheel that imports but
  lacks either law callable is a PRE-G8 (or foreign/broken) install and
  latches ``crate_stale``. Both serve the oracle with one loud WARN.
* **Degrade set** — ``ValueError``/``RuntimeError`` (the crate's declared
  FFI error surface), ``TypeError``/``OverflowError`` (PyO3 argument
  conversion — an out-of-``i64`` ``raw_bars`` lands here;
  production-unreachable, the vendor never returns >9.2e18 bars, but the
  hypothesis suite is honestly scoped to the ``i64`` domain because of it),
  ``AttributeError`` (broken-crate-internals belt). ``MemoryError`` is
  DELIBERATELY absent — OOM re-raises raw, identical to the pure-Python
  path. The narrow panic belt (``_panic_belt_errors`` — imported from the
  sibling facade, ONE copy per §11 COMMON) latches ``crate_panic``.
* **Refuse-poison gates** — a non-``str`` status / non-``str`` date render
  / non-exact-``date`` key field is REFUSED to the oracle: the exact-type
  ``date`` gate matters because ``datetime`` IS a ``date`` subclass whose
  ``isoformat()`` differs — post-burn-in a subclass field could otherwise
  render a silently WRONG key.
* **Burn-in + autorevert** — the first :data:`BURN_IN_SAMPLES` engaged
  calls PER LAW compare the Rust value against the live oracle; ANY
  difference ships the ORACLE value and autoreverts the Rust path for the
  process (ERROR log — the #1531/#1532 pattern).

## Honesty (ADR-0020 banner; CLAUDE.md Golden Rule #10)

Expected payoff ≈ 0 — conversion is for uniformity, not speed. Both laws
are nanosecond-scale in Python and the FFI hop is a small SLOWDOWN;
acceptable because the seams run per window RECORD / per ledger key (an
I/O-dominated vendor-fetch path) and the engaged Rust path is guarded by
the burn-in + autorevert + degrade-to-oracle layers
(DEC-RUST-DEFAULT-ON-WAVE-001, default ON). The flag is read
per call (one ``os.environ`` probe) so spawn workers resolve their own
inherited environment. The stateful machinery AROUND these laws —
``record()``'s streak/tombstone rules, the merge-on-save conflict law
(DEC-WINDOW-LEDGER-DEGRADED-TOMBSTONE-001 ×
DEC-WINDOW-LEDGER-RESCUE-001), optional-column ``None`` back-compat —
stays PYTHON: the full #1548 pyclass single-source flip is the NAMED
follow-up ``DEC-LAKE-RUST-WINDOW-LEDGER-002``.

This module is deliberately import-light (stdlib + structlog + the sibling
facade's flag/panic helpers): ``window_status``/``window_ledger`` import it
at module scope, and neither may grow a heavy dependency edge.
"""

from __future__ import annotations

import importlib
import os
from datetime import date
from typing import TYPE_CHECKING, Final, Protocol

import structlog

from brutex.config.rust_strict import refuse_if_strict

# §11 COMMON — ONE flag vocabulary + ONE panic belt for the brutex-lake
# facades. `env_flag_tristate` is the sibling's public helper;
# `_panic_belt_errors` is a same-package private import (the underscore is
# module-privacy toward OUTSIDE consumers — duplicating the guardedly-
# resolved PanicException fold here would be the §11 violation).
from brutex.data.lake_rust_render import _panic_belt_errors, env_flag_tristate

if TYPE_CHECKING:
    from types import ModuleType

    from brutex.data.fetch_coverage import WindowKey

_LOG = structlog.get_logger(__name__)

#: The env flag (default ON per DEC-RUST-DEFAULT-ON-WAVE-001; the
#: explicit-falsy value is the kill switch, junk fails loud-OFF).
LAKE_RUST_WINDOW_LEDGER_ENV: Final[str] = "BRUTEX_LAKE_RUST_WINDOW_LEDGER"

#: Engaged calls PER LAW that are bit-compared against the live Python
#: oracle before Rust values ship uncompared (the #1531/#1532 pattern).
BURN_IN_SAMPLES: Final[int] = 16

#: Resolved-crate memo (``[None]`` = not yet resolved this process) — the
#: ``money_rust`` pattern (DEC-MONEY-RUST-WRAPPER-COST-001), which measured
#: ``_resolve_crate`` at 911 ns/call because ``importlib.import_module``
#: costs ~574 ns even on a ``sys.modules`` HIT and the symbol handshake the
#: rest. This seam pays that resolve on EVERY routed call.
#:
#: THE MODULE is memoized, never a bound callable: every routed law does
#: ``getattr(crate, name)`` per call, so a test that monkeypatches a crate
#: ATTRIBUTE is still seen — a callable memo would silently miss it.
#:
#: SUCCESS ONLY. A FAILED resolve is never cached: the failure arms go
#: through :func:`_disable`, which under strict mode REFUSES *before* it
#: latches, so caching a failure would convert "every call re-attempts and
#: re-refuses, loudly" into the silently-latched steady state strict mode
#: exists to forbid.
#:
#: Deliberately NOT shared with :func:`_panic_belt_errors`, which loops over
#: SEVERAL import homes and whose per-home blocking the suites use to prove
#: belt narrowing — this memo is scoped to ``_resolve_crate`` alone.
#:
#: Cleared by :func:`reset_window_ledger_rust_for_tests` — a test that swaps
#: ``sys.modules['brutex_lake']`` must reset, or it is served the crate
#: resolved before its swap.
_CRATE_MEMO: Final[list[ModuleType | None]] = [None]

#: The classify-law burn-in budget key.
_LAW_CLASSIFY: Final[str] = "classify"
#: The merge-key-law burn-in budget key.
_LAW_MERGE_KEY: Final[str] = "merge_key"

#: Process-local OFF latch — the disable reason, or ``None`` when live.
_DISABLED_REASON: Final[list[str | None]] = [None]

#: Per-law burn-in budgets (mutated in place; reset seam below).
_BURN_IN_REMAINING: Final[dict[str, int]] = {
    _LAW_CLASSIFY: BURN_IN_SAMPLES,
    _LAW_MERGE_KEY: BURN_IN_SAMPLES,
}

#: The crate callables BOTH laws need — the SYMBOL handshake surface. A
#: wheel missing either is a pre-G8 install (``crate_stale``).
_CRATE_API: Final[tuple[str, ...]] = ("classify_window_status", "date_isoformat")

#: Once-per-process-per-reason log memo (the seams run per window record —
#: thousands per pull; a sticky reason must not flood the logs).
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


class ClassifyLaw(Protocol):
    """The Python status-law oracle's call shape (``window_status`` law)."""

    def __call__(self, raw_bars: int, error_type: str | None, *, degraded: bool = False) -> str:
        """Classify a window outcome — SUCCESS / EMPTY / FAILED."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed


class MergeKeyLaw(Protocol):
    """The Python merge-key oracle's call shape (the 5-tuple law)."""

    def __call__(self, key: WindowKey) -> tuple[str, str, str, str, str]:
        """Render *key*'s 5-tuple window identity."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed


def window_ledger_rust_enabled() -> bool:
    """Resolve ``BRUTEX_LAKE_RUST_WINDOW_LEDGER`` (default ON per
    DEC-RUST-DEFAULT-ON-WAVE-001, operator directive 2026-07-23; junk loud-OFF).

    Truthy literal → ``True``; falsy literal → ``False``; unset/blank →
    ``True`` (the default); any OTHER non-empty value → ``False`` with one
    ``window_ledger_rust.fallback`` WARN (``flag_unrecognised``) per
    process. Read per call so spawn workers resolve their own environment
    (the sibling facade's law, byte-identical vocabulary via the imported
    :func:`env_flag_tristate`).
    """
    raw = os.environ.get(LAKE_RUST_WINDOW_LEDGER_ENV, "")
    verdict = env_flag_tristate(raw)
    if verdict is not None:
        return verdict
    if raw.strip():
        _warn_once("flag_unrecognised", value=raw)
        return False
    return True


def rust_window_ledger_disabled() -> bool:
    """``True`` iff a guard/autorevert latched the Rust path OFF this process."""
    return _DISABLED_REASON[0] is not None


def reset_window_ledger_rust_for_tests(*, remaining: int = BURN_IN_SAMPLES) -> None:
    """Reset latch + per-law burn-in budgets + warn memo (test isolation ONLY).

    ``remaining=0`` puts the process in the post-burn-in steady state (the
    Rust value ships with no oracle in the path — the differential suites
    depend on that). Never called from production code (the sibling
    facade's reset-seam convention).
    """
    _DISABLED_REASON[0] = None
    _BURN_IN_REMAINING[_LAW_CLASSIFY] = remaining
    _BURN_IN_REMAINING[_LAW_MERGE_KEY] = remaining
    _OBSERVABILITY_MEMO.clear()
    _CRATE_MEMO[0] = None


#: CLASS-C reasons EXEMPT from the strict-mode refusal
#: (DEC-RUST-STRICT-NO-FALLBACK-001). Enumerated + reviewable here, never
#: buried in an inline condition. Everything NOT listed refuses.
#:
#: * ``merge_key_nondate_refused`` — :func:`routed_window_merge_key`'s
#:   EXACT-TYPE input gate. ``datetime`` IS a ``date`` subclass whose
#:   ``isoformat()`` renders a DIFFERENT string, and the crate's FFI takes
#:   plain ``(year, month, day)`` ints, so a ``WindowKey`` carrying a
#:   ``datetime`` (or any other ``date`` subclass) start/end is an input the
#:   crate never claimed — the Python oracle was ALWAYS the intended handler
#:   for it, and the gate exists precisely so a subclass can never render a
#:   silently WRONG merge key post-burn-in. Refusing here would crash a
#:   legitimate caller over a value Python handles correctly; the honest fix
#:   is to widen the crate/FFI to accept the subclass render until this arm
#:   is unreachable, then delete the entry — not to refuse.
_STRICT_EXEMPT_REASONS: Final[frozenset[str]] = frozenset({"merge_key_nondate_refused"})


def _warn_once(reason: str, **context: object) -> None:
    """Log ``window_ledger_rust.fallback`` WARN once per process per *reason*.

    DEC-RUST-STRICT-NO-FALLBACK-001: refuses under ``BRUTEX_RUST_STRICT``
    unless *reason* is in :data:`_STRICT_EXEMPT_REASONS`. The refused set is
    CLASS A (``flag_unrecognised`` — the junk-flag arm; ``classify_error`` /
    ``merge_key_error`` — the crate raised) and CLASS B
    (``classify_result_refused`` / ``merge_key_result_refused`` —
    refuse-poison rejected the crate's return type). Strict-OFF is
    byte-identical to the prior behaviour.

    STORED-BYTE SAFETY: neither routed law renders a persisted byte — the
    ledger Parquet is written by ``window_ledger_store``'s own pyarrow/
    parquet-rs render, which this facade never touches. A refusal removes a
    ledger RECORD/merge-key computation, so no stored byte is rewritten.
    """
    if reason not in _STRICT_EXEMPT_REASONS:
        refuse_if_strict("brutex.data.window_ledger_rust", reason, **context)
    if reason in _OBSERVABILITY_MEMO:
        return
    _OBSERVABILITY_MEMO.add(reason)
    _LOG.warning("window_ledger_rust.fallback", reason=reason, **context)


def _disable(reason: str, *, error: bool = False, **context: object) -> None:
    """Latch the Rust path OFF for the remainder of the process.

    ``error=True`` logs ``window_ledger_rust.autoreverted`` at ERROR (the
    burn-in autorevert — a genuine wrongness signal); guard latches log
    ``window_ledger_rust.rust_path_disabled`` at WARN. Repeats are no-ops.

    DEC-RUST-STRICT-NO-FALLBACK-001: refuses under ``BRUTEX_RUST_STRICT``.
    Reasons here are CLASS A (``crate_unavailable`` / ``crate_stale`` /
    ``crate_panic``) or CLASS B (``burn_in_mismatch_classify`` /
    ``burn_in_mismatch_merge_key`` — the crate disagreed with the oracle);
    none is class C, so none is exempt. The refusal fires BEFORE the latch
    is written, so under strict mode the latch is never set and every
    ``rust_window_ledger_disabled()`` early-return is structurally
    unreachable. Strict-OFF is byte-identical to the prior behaviour.
    """
    refuse_if_strict("brutex.data.window_ledger_rust", reason, error=error, **context)
    if _DISABLED_REASON[0] is not None:
        return
    _DISABLED_REASON[0] = reason
    if error:
        _LOG.error("window_ledger_rust.autoreverted", reason=reason, **context)
    else:
        _LOG.warning("window_ledger_rust.rust_path_disabled", reason=reason, **context)


def _resolve_crate() -> ModuleType | None:
    """Import guard + SYMBOL handshake: ``brutex_lake``, or latch OFF + ``None``.

    An absent wheel (``ImportError``) latches ``crate_unavailable``. A wheel
    that imports but lacks either :data:`_CRATE_API` callable is a PRE-G8
    (or foreign/broken) install — latch ``crate_stale`` (the stale-wheel
    handshake; the polars version guard is meaningless for a polars-free
    law, module docstring). Fail-closed: never a new crash class.
    """
    memo = _CRATE_MEMO[0]
    if memo is not None:
        return memo
    try:
        crate = importlib.import_module("brutex_lake")
    except ImportError as exc:
        _disable(
            "crate_unavailable",
            error_detail=repr(exc),
            remedy="pip install rust/brutex-lake (see docs/runbooks/lake-rust-render-lockstep.md)",
        )
        return None
    for name in _CRATE_API:
        if not callable(getattr(crate, name, None)):
            _disable(
                "crate_stale",
                error_detail=f"brutex_lake.{name} missing or not callable — "
                "the installed wheel predates the window-ledger laws (or is foreign/broken)",
                remedy="rebuild + reinstall rust/brutex-lake "
                "(see docs/runbooks/lake-rust-render-lockstep.md)",
            )
            return None
    _CRATE_MEMO[0] = crate
    return crate


def _log_engaged_once(crate: ModuleType) -> None:
    """Log ``window_ledger_rust.engaged`` INFO once per process."""
    if _ENGAGED_KEY in _OBSERVABILITY_MEMO:
        return
    _OBSERVABILITY_MEMO.add(_ENGAGED_KEY)
    _LOG.info(
        "window_ledger_rust.engaged",
        crate_version=getattr(crate, "__version__", None),
    )


def routed_classify_window_status(
    raw_bars: int,
    error_type: str | None,
    *,
    degraded: bool,
    python_law: ClassifyLaw,
) -> str:
    """The routed status law — flag-gated Rust with the live Python oracle.

    EVERY path returns a valid status token: flag-OFF / latched / guarded /
    degraded calls return ``python_law(...)`` (the oracle), a burn-in
    mismatch returns the ORACLE value + autoreverts, and only a
    guard-clean, burn-in-clean call returns the Rust value.
    """
    if not window_ledger_rust_enabled() or rust_window_ledger_disabled():
        return python_law(raw_bars, error_type, degraded=degraded)
    crate = _resolve_crate()
    if crate is None:
        return python_law(raw_bars, error_type, degraded=degraded)
    try:
        rust_value = crate.classify_window_status(raw_bars, error_type, degraded)
    except _DEGRADE_ERRORS as exc:
        _warn_once("classify_error", error_detail=repr(exc))
        return python_law(raw_bars, error_type, degraded=degraded)
    except (KeyboardInterrupt, SystemExit, MemoryError):
        # The escape set (the errors.py doctrine): operator interrupt /
        # interpreter shutdown / OOM stay global — never a silent fallback.
        # Sits ABOVE the panic belt so its `Exception` member can never
        # swallow a MemoryError.
        raise
    except _panic_belt_errors() as exc:
        _disable("crate_panic", error_detail=repr(exc))
        return python_law(raw_bars, error_type, degraded=degraded)
    if not isinstance(rust_value, str):
        # Refuse-poison gate: a non-str return is a crate BUG — never
        # served. Structurally unreachable through the real crate (its FFI
        # returns &'static str); guards a stale/foreign install.
        _warn_once("classify_result_refused", result_type=type(rust_value).__name__)
        return python_law(raw_bars, error_type, degraded=degraded)
    _log_engaged_once(crate)
    if _BURN_IN_REMAINING[_LAW_CLASSIFY] > 0:
        oracle_value = python_law(raw_bars, error_type, degraded=degraded)
        if rust_value != oracle_value:
            _disable(
                "burn_in_mismatch_classify",
                error=True,
                rust_value=rust_value,
                python_value=oracle_value,
            )
            return oracle_value
        _BURN_IN_REMAINING[_LAW_CLASSIFY] -= 1
        if _BURN_IN_REMAINING[_LAW_CLASSIFY] == 0:
            _LOG.info(
                "window_ledger_rust.burn_in_complete",
                law=_LAW_CLASSIFY,
                samples=BURN_IN_SAMPLES,
            )
    return rust_value


def routed_window_merge_key(
    key: WindowKey,
    *,
    python_law: MergeKeyLaw,
) -> tuple[str, str, str, str, str]:
    """The routed merge-key law — flag-gated Rust date render, Python identity.

    Only the two ``date.isoformat()`` renders run in Rust; ``market`` /
    ``security_id`` / ``interval`` are the ORIGINAL Python string objects
    (never crossing the FFI — byte-identity by construction). The exact-type
    ``date`` gate refuses subclass fields to the oracle: ``datetime`` IS a
    ``date`` whose ``isoformat()`` differs, so post-burn-in a subclass field
    could otherwise render a silently WRONG key (module docstring).
    """
    if not window_ledger_rust_enabled() or rust_window_ledger_disabled():
        return python_law(key)
    start = key.start
    end = key.end
    if type(start) is not date or type(end) is not date:
        _warn_once(
            "merge_key_nondate_refused",
            start_type=type(start).__name__,
            end_type=type(end).__name__,
        )
        return python_law(key)
    crate = _resolve_crate()
    if crate is None:
        return python_law(key)
    try:
        start_iso = crate.date_isoformat(start.year, start.month, start.day)
        end_iso = crate.date_isoformat(end.year, end.month, end.day)
    except _DEGRADE_ERRORS as exc:
        _warn_once("merge_key_error", error_detail=repr(exc))
        return python_law(key)
    except (KeyboardInterrupt, SystemExit, MemoryError):
        raise
    except _panic_belt_errors() as exc:
        _disable("crate_panic", error_detail=repr(exc))
        return python_law(key)
    if not (isinstance(start_iso, str) and isinstance(end_iso, str)):
        _warn_once(
            "merge_key_result_refused",
            start_type=type(start_iso).__name__,
            end_type=type(end_iso).__name__,
        )
        return python_law(key)
    rust_key = (key.market, key.security_id, key.interval, start_iso, end_iso)
    _log_engaged_once(crate)
    if _BURN_IN_REMAINING[_LAW_MERGE_KEY] > 0:
        oracle_key = python_law(key)
        if rust_key != oracle_key:
            _disable(
                "burn_in_mismatch_merge_key",
                error=True,
                rust_key=list(rust_key),
                python_key=list(oracle_key),
            )
            return oracle_key
        _BURN_IN_REMAINING[_LAW_MERGE_KEY] -= 1
        if _BURN_IN_REMAINING[_LAW_MERGE_KEY] == 0:
            _LOG.info(
                "window_ledger_rust.burn_in_complete",
                law=_LAW_MERGE_KEY,
                samples=BURN_IN_SAMPLES,
            )
    return rust_key


__all__ = [
    "BURN_IN_SAMPLES",
    "LAKE_RUST_WINDOW_LEDGER_ENV",
    "ClassifyLaw",
    "MergeKeyLaw",
    "reset_window_ledger_rust_for_tests",
    "routed_classify_window_status",
    "routed_window_merge_key",
    "rust_window_ledger_disabled",
    "window_ledger_rust_enabled",
]
