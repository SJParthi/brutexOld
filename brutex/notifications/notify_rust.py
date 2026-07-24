"""notify_rust — the default-ON Rust routing facade for the notification PURE laws.

DEC-NOTIFY-RUST-CORE-001 (MIGRATION.md #1957); default flipped ON by
DEC-RUST-DEFAULT-ON-WAVE-001 (operator directive 2026-07-23). Routes the FOUR pure
notification laws through the ``rust/brutex-notify`` crate behind the
default-ON ``BRUTEX_NOTIFY_RUST`` flag, with each verbatim Python law
retained as the LIVE parity oracle (the ``window_ledger_rust`` /
``feed_rust`` template):

1. **MarkdownV2 escape** — ``sweep_alerts.escape_markdown_v2`` (the
   per-code-point backslash-escape over the 18-char reserved set; the
   backslash itself deliberately NOT escaped — preserved bit-exactly).
2. **Honest-unknown count** — ``sweep_alerts._count_or_question``
   (``"?"`` on ``None``, NEVER a fabricated 0).
3. **Dedupe-window drop predicate** — ``telegram._dedupe_should_drop_python``
   (the ``(now - last_seen) < window`` strict-float law). The floats cross
   the FFI as IEEE-754 BIT PATTERNS carried in ``int`` (``struct`` pack →
   ``f64::from_bits``), so the Rust compare is bit-exact vs CPython while
   honoring the house ints-only FFI discipline — no float OBJECT, no
   datetime ever crosses.
4. **12-code-point id truncation** — the ``text[:12]`` law (Python slices
   by CODE POINT; the crate uses ``chars().take(12)`` — equivalence pinned
   by the crate matrix + the hypothesis differential).

## The fail-closed layers (the sibling-facade template, adapted)

* **Flag** — ``BRUTEX_NOTIFY_RUST`` default ON
  (DEC-RUST-DEFAULT-ON-WAVE-001): unset/blank/truthy are ON; an explicit
  falsy literal is the OFF kill switch (silent); any other non-empty value
  is OFF with ONE loud ``notify_rust.fallback`` WARN
  (``flag_unrecognised``). Read per call (no global latch) so spawn workers
  resolve their own environment. Explicit-OFF returns the oracle's result
  directly — the crate is NEVER imported (spy-pinned), so the kill-switch
  path is byte-identical to pre-facade behaviour.
* **LEAF discipline** — this module imports ONLY stdlib + structlog. It may
  NOT import ``env_flag_tristate`` from ``brutex.engine.sweep_lookahead``:
  importing anything under ``brutex.engine`` triggers the engine package's
  eager ``fno_math``-wheel-bearing import chain, and ``sweep_alerts`` /
  ``telegram`` (this facade's importers) are deliberately WHEEL-FREE at
  import for their isolated ci.yml coverage gates — several engine/dashboard
  modules also import ``sweep_alerts`` at MODULE level, so an engine import
  from here would be an outright cycle. The inline ``_TRUTHY``/``_FALSY``
  vocabulary is the ``feed_rust`` local-twin precedent, mirror-pinned
  against ``sweep_lookahead``'s sets by a test (which imports the engine
  only inside the test body).
* **Import guard / SYMBOL handshake** — the laws are stdlib-only, so the
  ``lake_rust_render`` polars VERSION guard is meaningless here (the
  ``window_ledger_rust`` documented-deviation precedent): the stale-wheel
  guard is the symbol handshake — an absent wheel latches
  ``crate_unavailable``; a wheel that imports but lacks any law callable is
  a foreign/broken/stale install and latches ``crate_stale``. Both serve
  the oracle with one loud WARN.
* **Degrade set** — ``RuntimeError``/``ValueError``/``TypeError``/
  ``UnicodeEncodeError`` (a LONE-SURROGATE ``str`` — reachable via
  vendor-derived ``reason``/``detail`` text — fails UTF-8 conversion at the
  pyo3 edge as ``UnicodeEncodeError`` and must degrade, never raise: the
  notifier's never-raises contract is load-bearing at 30+ call sites) +
  ``OverflowError`` (PyO3 int conversion — an out-of-``i64`` count) +
  ``AttributeError`` (broken-crate-internals belt). ``MemoryError`` is
  DELIBERATELY absent — OOM re-raises raw, identical to the pure-Python
  path; ``KeyboardInterrupt``/``SystemExit`` likewise re-raise.
* **Refuse-poison gates** — a non-``str`` (or non-``bool`` for the dedupe
  law) crate return is REFUSED to the oracle with one WARN: structurally
  unreachable through the real crate (its FFI returns ``String``/``bool``),
  guards a stale/foreign install.
* **Burn-in + autorevert** — the first :data:`BURN_IN_SAMPLES` engaged
  calls PER LAW bit-compare the Rust value against the live oracle; ANY
  difference ships the ORACLE value and autoreverts the Rust path for the
  process (ONE ``notify_rust.autoreverted`` ERROR — the #1531/#1532
  pattern).

## Honesty (ADR-0020 banner; CLAUDE.md Golden Rule #10)

Expected payoff ≈ 0 — conversion is for uniformity, not speed. Every law is
micro/nanosecond-scale in Python, the escape is O(len(text)) either way,
and the FFI hop is a small SLOWDOWN; acceptable because the laws run per
ALERT EMIT (a rare, network-I/O-dominated path) and the engaged Rust path is
guarded by the burn-in + autorevert + degrade-to-oracle layers above
(DEC-RUST-DEFAULT-ON-WAVE-001, default ON). The
Telegram HTTP send, the notifier thread/queue/atexit lifecycle and the
wiring singleton STAY Python — network I/O, honestly disclosed as such; the
~30 f-string message builders STAY Python too (their 12 fixed-spec float
format sites are a Rust parity hazard — the named follow-up
``DEC-NOTIFY-RUST-BUILDERS-002``). No secret (bot token / chat id / config)
ever crosses the FFI — the Rust surface is message text + integer bit
patterns only.
"""

from __future__ import annotations

import importlib
import os
import struct
from collections.abc import Callable
from typing import TYPE_CHECKING, Final, cast

import structlog

if TYPE_CHECKING:
    from types import ModuleType

_LOG = structlog.get_logger(__name__)

#: The env flag (default ON per DEC-RUST-DEFAULT-ON-WAVE-001; explicit
#: falsy = the kill switch; junk fails loud-OFF).
NOTIFY_RUST_ENV: Final[str] = "BRUTEX_NOTIFY_RUST"

#: Engaged calls PER LAW that are bit-compared against the live Python
#: oracle before Rust values ship uncompared (the #1531/#1532 pattern).
BURN_IN_SAMPLES: Final[int] = 16

#: Inline flag vocabulary (LEAF discipline — module docstring; the
#: mirror-pin test asserts byte-equality with ``sweep_lookahead``'s sets).
_TRUTHY: Final[frozenset[str]] = frozenset({"1", "true", "yes", "on"})
_FALSY: Final[frozenset[str]] = frozenset({"0", "false", "no", "off"})

#: The escape-law burn-in budget key.
_LAW_ESCAPE: Final[str] = "escape"
#: The count-law burn-in budget key.
_LAW_COUNT: Final[str] = "count"
#: The dedupe-law burn-in budget key.
_LAW_DEDUPE: Final[str] = "dedupe"
#: The truncation-law burn-in budget key.
_LAW_TRUNCATE: Final[str] = "truncate"

#: Process-local OFF latch — the disable reason, or ``None`` when live.
_DISABLED_REASON: Final[list[str | None]] = [None]

#: Per-law burn-in budgets (mutated in place; reset seam below).
_BURN_IN_REMAINING: Final[dict[str, int]] = {
    _LAW_ESCAPE: BURN_IN_SAMPLES,
    _LAW_COUNT: BURN_IN_SAMPLES,
    _LAW_DEDUPE: BURN_IN_SAMPLES,
    _LAW_TRUNCATE: BURN_IN_SAMPLES,
}

#: The crate callables ALL laws need — the SYMBOL handshake surface. A
#: wheel missing any is a foreign/broken/stale install (``crate_stale``).
_CRATE_API: Final[tuple[str, ...]] = (
    "count_or_question",
    "dedupe_should_drop",
    "escape_markdown_v2",
    "truncate_id_12",
)

#: Once-per-process-per-reason log memo (alert emits are rare, but a sticky
#: degrade reason must never log-storm a long-running dashboard).
_OBSERVABILITY_MEMO: Final[set[str]] = set()

#: The engaged-INFO memo key (not a fallback reason — namespaced apart).
_ENGAGED_KEY: Final[str] = "__engaged__"

#: The narrow degrade-to-oracle catch set (module docstring). MemoryError
#: deliberately absent — OOM re-raises raw, identical to the Python path.
_DEGRADE_ERRORS: Final[tuple[type[Exception], ...]] = (
    ValueError,  # UnicodeEncodeError (the lone-surrogate pyo3 edge) is a subclass
    RuntimeError,
    TypeError,
    OverflowError,  # PyO3 int conversion — an out-of-i64 count
    AttributeError,
)


def notify_rust_enabled(raw: str | None = None) -> bool:
    """Resolve ``BRUTEX_NOTIFY_RUST`` (default ON, junk loud-OFF).

    DEC-RUST-DEFAULT-ON-WAVE-001 (operator directive 2026-07-23): unset,
    blank and explicit truthy literals (``1``/``true``/``yes``/``on``,
    case-insensitive, whitespace-stripped) are ON; an explicit falsy
    literal is the silent OFF kill switch; any OTHER non-empty value is
    OFF with one ``notify_rust.fallback`` WARN (``flag_unrecognised``)
    per process — an unrecognised value must never silently pick an
    execution path (fail-safe junk arm unchanged). Read per call, no
    global latch.

    Parameters
    ----------
    raw
        Explicit value to interpret (tests). ``None`` reads
        :data:`NOTIFY_RUST_ENV` from the environment.
    """
    value = os.environ.get(NOTIFY_RUST_ENV) if raw is None else raw
    if value is None:
        return True
    stripped = value.strip().lower()
    if stripped in _TRUTHY:
        return True
    if stripped in _FALSY:
        return False
    if stripped == "":
        return True
    _warn_once("flag_unrecognised", env=NOTIFY_RUST_ENV, value=value)
    return False


def rust_notify_disabled() -> bool:
    """``True`` iff a guard/autorevert latched the Rust path OFF this process."""
    return _DISABLED_REASON[0] is not None


def reset_notify_rust_for_tests(*, remaining: int = BURN_IN_SAMPLES) -> None:
    """Reset latch + per-law burn-in budgets + warn memo (test isolation ONLY).

    ``remaining=0`` puts the process in the post-burn-in steady state (the
    Rust value ships with no oracle in the path — the differential suites
    depend on that). Never called from production code (the sibling
    facades' reset-seam convention).
    """
    _DISABLED_REASON[0] = None
    for law in _BURN_IN_REMAINING:
        _BURN_IN_REMAINING[law] = remaining
    _OBSERVABILITY_MEMO.clear()


def _warn_once(reason: str, **context: object) -> None:
    """Log ``notify_rust.fallback`` WARN once per process per *reason*."""
    if reason in _OBSERVABILITY_MEMO:
        return
    _OBSERVABILITY_MEMO.add(reason)
    _LOG.warning("notify_rust.fallback", reason=reason, **context)


def _disable(reason: str, *, error: bool = False, **context: object) -> None:
    """Latch the Rust path OFF for the remainder of the process.

    ``error=True`` logs ``notify_rust.autoreverted`` at ERROR (the burn-in
    autorevert — a genuine wrongness signal); guard latches log
    ``notify_rust.rust_path_disabled`` at WARN. Repeats are no-ops.
    """
    if _DISABLED_REASON[0] is not None:
        return
    _DISABLED_REASON[0] = reason
    if error:
        _LOG.error("notify_rust.autoreverted", reason=reason, **context)
    else:
        _LOG.warning("notify_rust.rust_path_disabled", reason=reason, **context)


def _panic_belt_errors() -> tuple[type[BaseException], ...]:
    """The narrow FFI panic-belt catch set — ``(Exception, *panic classes)``.

    ``pyo3_runtime.PanicException`` is the one ``BaseException`` subclass a
    pyo3 boundary can leak; it must latch ``crate_panic`` and degrade to the
    oracle, but the repo convention (CodeQL ``py/catch-base-exception``)
    forbids a non-re-raising literal ``except BaseException``, so the class
    is resolved GUARDEDLY by name and folded beside ``Exception`` (the
    ``lake_rust_render`` technique — NOT imported from there: that module
    pulls the data package + polars into this deliberately wheel-free leaf,
    so the small resolver is carried locally, a documented §11 trade beside
    the ``feed_rust`` local-twin precedent). ``pyo3_runtime`` is the class's
    ``__module__`` (not a real importable module today — a forward belt);
    ``polars.exceptions`` re-exports it in environments that carry polars
    (guarded — this leaf never REQUIRES polars). HONEST narrowing: a
    per-extension ``PanicException`` type object is not catchable by name
    and now propagates — bounded by the crate's panic-free-by-construction
    laws + the release profile's ``panic = "abort"`` (an abort never
    unwinds). ``Exception`` membership keeps every out-of-degrade-set
    ``Exception`` latching; ``KeyboardInterrupt``/``SystemExit``/
    ``MemoryError`` re-raise in the clause ABOVE the belt, so ``Exception``
    here never swallows an OOM.
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
    """Import guard + SYMBOL handshake: ``brutex_notify``, or latch OFF + ``None``.

    An absent wheel (``ImportError`` — including the shim's own stale-wheel
    handshake refusal) latches ``crate_unavailable``. A wheel that imports
    but lacks any :data:`_CRATE_API` callable is a foreign/broken/stale
    install — latch ``crate_stale``. Fail-closed: never a new crash class.
    (The ``lake_rust_render`` polars VERSION guard is deliberately absent —
    these laws are stdlib-only; the ``window_ledger_rust`` documented
    deviation.)
    """
    try:
        crate = importlib.import_module("brutex_notify")
    except ImportError as exc:
        _disable(
            "crate_unavailable",
            error_detail=repr(exc),
            remedy="pip install rust/brutex-notify (or maturin develop --release there)",
        )
        return None
    for name in _CRATE_API:
        if not callable(getattr(crate, name, None)):
            _disable(
                "crate_stale",
                error_detail=f"brutex_notify.{name} missing or not callable — "
                "the installed wheel predates the notification laws (or is foreign/broken)",
                remedy="rebuild + reinstall rust/brutex-notify",
            )
            return None
    return crate


def _log_engaged_once(crate: ModuleType) -> None:
    """Log ``notify_rust.engaged`` INFO once per process."""
    if _ENGAGED_KEY in _OBSERVABILITY_MEMO:
        return
    _OBSERVABILITY_MEMO.add(_ENGAGED_KEY)
    _LOG.info(
        "notify_rust.engaged",
        crate_version=getattr(crate, "__version__", None),
    )


def _f64_bits(value: float) -> int:
    """The IEEE-754 bit pattern of *value* as an unsigned 64-bit ``int``.

    The lossless float→int carrier for the dedupe law (``f64::from_bits``
    on the Rust side reconstructs the EXACT double — NaN payloads, signed
    zeros, subnormals included), honoring the house ints-only FFI
    discipline without a lossy decimal round-trip.
    """
    return cast("int", struct.unpack("<Q", struct.pack("<d", value))[0])


def _routed[T: (str, bool)](
    law: str,
    *,
    rust_call: Callable[[ModuleType], object],
    python_value: Callable[[], T],
    result_type: type[T],
) -> T:
    """The ONE routing engine every law wrapper delegates to (§11 COMMON).

    EVERY path returns a law-valid value: flag-OFF / latched / guarded /
    degraded calls return ``python_value()`` (the live oracle), a burn-in
    mismatch returns the ORACLE value + autoreverts, and only a
    guard-clean, burn-in-clean call returns the Rust value.
    """
    if not notify_rust_enabled() or rust_notify_disabled():
        return python_value()
    crate = _resolve_crate()
    if crate is None:
        return python_value()
    try:
        rust_value = rust_call(crate)
    except _DEGRADE_ERRORS as exc:
        _warn_once(f"{law}_error", error_detail=repr(exc))
        return python_value()
    except (KeyboardInterrupt, SystemExit, MemoryError):
        # The escape set (the errors.py doctrine): operator interrupt /
        # interpreter shutdown / OOM stay global — never a silent fallback.
        # Sits ABOVE the panic belt so its `Exception` member can never
        # swallow a MemoryError.
        raise
    except _panic_belt_errors() as exc:
        _disable("crate_panic", error_detail=repr(exc))
        return python_value()
    if type(rust_value) is not result_type:
        # Refuse-poison gate: a wrong-typed return is a crate BUG — never
        # served. Structurally unreachable through the real crate (its FFI
        # returns String/bool); guards a stale/foreign install. Exact-type:
        # a bool IS an int subclass in Python, so isinstance would pun.
        _warn_once(f"{law}_result_refused", result_type=type(rust_value).__name__)
        return python_value()
    if _BURN_IN_REMAINING[law] > 0:
        oracle_value = python_value()
        if rust_value != oracle_value:
            _disable(
                f"burn_in_mismatch_{law}",
                error=True,
                rust_value=repr(rust_value),
                python_value=repr(oracle_value),
            )
            return oracle_value
        _BURN_IN_REMAINING[law] -= 1
        if _BURN_IN_REMAINING[law] == 0:
            _LOG.info("notify_rust.burn_in_complete", law=law, samples=BURN_IN_SAMPLES)
    _log_engaged_once(crate)
    # The exact-type gate above already narrowed rust_value to result_type.
    return rust_value


def routed_escape_markdown_v2(text: str, *, python_law: Callable[[str], str]) -> str:
    """The routed MarkdownV2 escape law — flag-gated Rust, live Python oracle."""
    return _routed(
        _LAW_ESCAPE,
        rust_call=lambda crate: crate.escape_markdown_v2(text),
        python_value=lambda: python_law(text),
        result_type=str,
    )


def routed_count_or_question(value: int | None, *, python_law: Callable[[int | None], str]) -> str:
    """The routed honest-unknown count law (``"?"`` on ``None``)."""
    return _routed(
        _LAW_COUNT,
        rust_call=lambda crate: crate.count_or_question(value),
        python_value=lambda: python_law(value),
        result_type=str,
    )


def routed_dedupe_should_drop(
    window: float,
    last_seen: float | None,
    now: float,
    *,
    python_law: Callable[[float, float | None, float], bool],
) -> bool:
    """The routed dedupe-window drop predicate (bit-pattern FFI carrier).

    The three floats cross as IEEE-754 bit patterns in ``int`` (module
    docstring law #3); the Rust ``f64::from_bits`` subtraction + strict
    ``<`` is bit-exact vs the Python oracle on every input, NaN/inf/signed
    zeros included.
    """
    return _routed(
        _LAW_DEDUPE,
        rust_call=lambda crate: crate.dedupe_should_drop(
            _f64_bits(window),
            None if last_seen is None else _f64_bits(last_seen),
            _f64_bits(now),
        ),
        python_value=lambda: python_law(window, last_seen, now),
        result_type=bool,
    )


def routed_truncate_id_12(text: str, *, python_law: Callable[[str], str]) -> str:
    """The routed ``text[:12]`` code-point truncation law."""
    return _routed(
        _LAW_TRUNCATE,
        rust_call=lambda crate: crate.truncate_id_12(text),
        python_value=lambda: python_law(text),
        result_type=str,
    )


__all__ = [
    "BURN_IN_SAMPLES",
    "NOTIFY_RUST_ENV",
    "notify_rust_enabled",
    "reset_notify_rust_for_tests",
    "routed_count_or_question",
    "routed_dedupe_should_drop",
    "routed_escape_markdown_v2",
    "routed_truncate_id_12",
    "rust_notify_disabled",
]
