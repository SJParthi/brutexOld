"""fno_symbol_rust — the default-OFF Rust routing facade for the F&O symbol grammar.

DEC-DATA-RUST-PURELAW-001 (ADR-0020 slice T10 PR-1; MIGRATION.md #1940).
Routes :func:`brutex.data.fno_symbol.parse_fno_symbol` through the
``rust/brutex-data`` crate (``brutex_data.parse_fno_symbol_coords``) behind
the default-OFF ``BRUTEX_DATA_RUST`` flag, with the verbatim Python law
retained as the LIVE parity oracle (``_parse_fno_symbol_python`` — the
#1913 ``tf_resampler`` seam shape). The FFI is INTEGERS ONLY: Rust returns
token-role SPANS into the ORIGINAL Python string + the resolved expiry
y/m/d + a type code (or ``None``); THIS facade materializes every object —
string slices (byte-identical by construction), :class:`datetime.date`,
the ``Decimal`` strike via the INJECTED ``parse_strike_token`` oracle
helper (all Decimal semantics — trailing zeros, ``1E4``, ``24_000``, sign,
whitespace — stay Python forever), and ``FnoSymbolParts`` via the injected
constructor. The facade never imports ``fno_symbol`` at runtime (no cycle
— the ``window_ledger_rust``/``WindowKey`` move).

## The fail-closed layers (the ``window_ledger_rust`` template, adapted)

* **Flag** — ``BRUTEX_DATA_RUST`` default OFF (ONE flag for the whole
  brutex-data crate); truthy → ON, falsy → OFF, unset/blank → OFF
  silently, any other non-empty value → OFF with ONE loud
  ``fno_symbol_rust.fallback`` WARN (``flag_unrecognised``). Read per call
  so spawn workers resolve their own environment. Claim scoping:
  unset/falsy = byte-identical (the bomb-spy pin's domain); a junk-SET
  value legitimately emits the ONE new WARN line before behaving OFF.
* **ASCII pre-gate** — a non-ASCII symbol routes STRAIGHT to the oracle
  with NO log: CPython ``strptime``'s ``\\d`` and ``int()`` accept Unicode
  digits, so reproducing Unicode Nd tables in Rust would be drift bait for
  zero product value (all real vendor symbols are ASCII); this is a scope
  gate on ordinary junk-directory input during lake walks, not a degrade —
  a warn per junk dir would flood. Behavior identical by construction.
* **Import guard / SYMBOL handshake** — an absent wheel latches
  ``crate_unavailable``; a wheel that imports but lacks the
  :data:`_CRATE_API` callable is a pre-T10 (or foreign/broken) install and
  latches ``crate_stale``. NO wheel VERSION guard — the law is
  polars-free/stdlib-pure, so the PR-1 polars-lockstep guard is
  meaningless here (the ``window_ledger_rust`` / ``lake_rust_stats``
  precedent); the guard this law actually needs is the LOCALE guard below.
* **LOCALE GUARD (the version-guard analogue for a locale-coupled law)** —
  ``strptime %b`` builds its month alternation from the LC_TIME locale
  (``_strptime.LocaleTime`` re-derives on ``_getlang()`` change), so the
  ORACLE's accept set moves on plain-ASCII tokens under a non-C locale
  (es_ES accepts ``30ene25``; de_DE has ``okt`` so ``30Oct25`` rejects).
  At first engage the facade probes the LIVE oracle's month vocabulary via
  :func:`_live_month_table` (``tuple(_strptime.LocaleTime().a_month[1:])``
  — ``LocaleTime`` is the EXACT surface ``datetime.strptime`` consults, so
  the probe provably tracks the oracle rather than reconstructing it) and
  ENGAGES only if it equals the crate's ASCII English reference table
  :data:`_ENGLISH_MONTHS` (both tuples — the compare is type-coherent);
  anything else — including the probe RAISING (a private-surface drift in
  a future CPython) — latches ``locale_unsupported`` and every call stays
  oracle. HONEST RESIDUAL: the probe runs at first engage + latches the
  POSITIVE ``_LOCALE_OK`` sentinel; a mid-process ``setlocale(LC_TIME,…)``
  flip AFTER engagement is not re-probed per call (the oracle re-checks
  ``_getlang()`` per strptime call; the engaged Rust arm would not) —
  bounded by (i) the grep-verified fact that NOTHING in BruteX calls
  ``setlocale``/pins LC_TIME anywhere (recorded 2026-07-17: conftest,
  workflows, brutex/, scripts/, Makefile, bootstrap.sh all empty — the OFF
  path was ALREADY locale-variable, a PRE-EXISTING envelope this seam
  surfaces honestly and does not widen), (ii) burn-in if the flip lands
  within the first 16 engaged calls, (iii) the flag ships default-OFF.
* **Degrade set** — ``ValueError``/``RuntimeError`` (the crate's declared
  FFI error surface — the ``catch_unwind`` panic boundary maps panics to
  ``RuntimeError``), ``TypeError``/``OverflowError`` (PyO3 argument
  conversion), ``AttributeError`` (broken-crate-internals belt).
  ``MemoryError`` is DELIBERATELY absent — OOM re-raises raw, identical
  to the pure-Python path. The narrow panic belt (``_panic_belt_errors``
  — imported from the sibling facade, ONE copy per §11 COMMON) latches
  ``crate_panic``.
* **Refuse-poison gates** — the FULL STRUCTURAL enumeration (each → one
  warn + oracle): non-9-tuple / non-``int`` members; ``type_code ∉
  {0,1,2}``; ``exchange_end < 1`` (empty exchange is an oracle ``None``);
  ``underlying_start != exchange_end + 1`` (the derived TRUE relation —
  exchange is ``parts[0]``, so the underlying span starts immediately
  after the first hyphen); empty underlying span; any span out of bounds;
  a year outside [``EXPIRY_YEAR_MIN``, ``EXPIRY_YEAR_MAX``] (=[2019,
  2059] — ``date(1975, 6, 30)`` constructs cleanly, so the ``date()``
  guard alone cannot catch it); ``m``/``d`` out of [1,12]/[1,31] BEFORE
  ``date()`` construction (which stays guarded too, the calendar backstop
  for 31Apr/29Feb-nonleap); FUT ⇒ strike span ``== (0, 0)`` (absent — the
  grammar has NO FUT strike token); CE/PE ⇒ non-empty strike span.
  HONEST claim scoping: this gate refuses STRUCTURALLY-IMPOSSIBLE tuples
  (crash/OOB protection + the enumerable oracle-``None`` classes); it is
  deliberately NOT a semantic re-verification — semantic wrongness WITHIN
  a structurally-valid tuple is the job of burn-in + the frozen goldens +
  the lane's differential + the flag-ON verbatim suite, never this gate.
* **Burn-in + autorevert** — the first :data:`BURN_IN_SAMPLES` engaged
  calls compare the materialized Rust-routed result against the live
  oracle via :func:`_parts_equal_strict` (dataclass equality AND
  ``str(strike)`` equality — dataclass ``__eq__`` compares ``Decimal``
  numerically, so the strict form pins representation; both arms build
  the strike via the SAME injected law, so a mismatch can only mean a
  span-law drift — exactly what burn-in must catch). ANY difference ships
  the ORACLE value and autoreverts for the process (ERROR log — the
  #1531/#1532 pattern). ``None`` results burn-in-compare too (a Rust
  false-reject is as wrong as a false-accept) — INCLUDING the option
  strike-reject arm (the injected ``strike_law`` returning ``None`` on
  the Rust-located token): during the burn-in window that ``None`` is
  compared against the live oracle exactly like every other decision, so
  a drifted/poisoned wheel that points the strike span at an in-bounds
  NON-numeric token for an option the oracle ACCEPTS trips the autorevert
  (the oracle value ships) instead of shipping a silent false-reject —
  the round-1 review-fix fold. Post-burn-in the arm ships inside the SAME
  burned-in envelope as every other decision (a healthy burned-in crate's
  strike token IS ``parts[-2]``, so facade-``None`` ⟺ oracle-``None`` by
  slice identity; no special-case disclaimer beyond the standard burn-in
  residual applies).

## Honesty (ADR-0020 banner; CLAUDE.md Golden Rule #10)

Expected payoff ≈ 0 — conversion is for uniformity, not speed.
``parse_fno_symbol`` runs on read/display/classification paths (the /db
F&O dataset discovery, movers futures/options loops, greeks target
discovery, lake heal/invariants option-key classification, crossverify
enumeration, universe reconciliation — O(lake-FNO-dirs) walks, thousands
of microsecond-scale calls per page/scan). The seam ADDS one env probe +
(engaged) one FFI hop + a Python-side materialization per call. NO
performance claim is made in either direction (the in-session sandbox
measurement over a 100k mixed-symbol walk read 6.70 µs/parse OFF vs
3.77 µs/parse ON — the engaged arm happened to measure FASTER because
CPython ``strptime`` dominates the oracle's cost — but that is an
observation, not an SLA; the conversion exists for uniformity).
Acceptable because the paths are I/O-dominated directory walks and the
flag ships DEFAULT-OFF. Not on the ~33 B-trial engine mask path; not in
run identity on the production spot engine.

Thread-safety posture (the HOUSE posture, mirrored from
``window_ledger_rust`` / ``lake_rust_render``): plain UNLOCKED writes to
the process-local counters/memos — a racing writer thread can at worst
run a few extra burn-in compares or double-emit a warn-once line, NEVER a
wrong shipped value (every burn-in-window call still oracle-compares
before shipping, and a mismatch latches for all threads). "16 burn-in
compares" is therefore best-effort under threadpools.

This module is deliberately import-light (stdlib + structlog + the
sibling facade's flag/panic helpers); ``fno_symbol`` imports it at module
scope and neither may grow a heavy dependency edge. Do NOT write a
literal ``import brutex_data`` anywhere under ``brutex/`` — the
OPTIONAL-kernel registry (``tests/unit/test_run_builds_rust_kernels.py``)
matches even indented literals and would reclassify the crate
REQUIRED-class; the ``importlib.import_module("brutex_data")`` string
form below is load-bearing.
"""

from __future__ import annotations

import importlib
import os
from datetime import date
from typing import TYPE_CHECKING, Final, Protocol

import structlog

# §11 COMMON — ONE flag vocabulary + ONE panic belt across the Rust-seam
# facades. `env_flag_tristate` is the sibling's public helper;
# `_panic_belt_errors` is a same-package private import (the underscore is
# module-privacy toward OUTSIDE consumers — duplicating the guardedly-
# resolved PanicException fold here would be the §11 violation).
# `lake_rust_render` is module-scope import-light (stdlib + structlog;
# polars lazy) and carries its OWN env_flag_tristate twin precisely so
# data modules never import brutex.engine (the fno_math wheel edge).
from brutex.data.lake_rust_render import _panic_belt_errors, env_flag_tristate

if TYPE_CHECKING:
    from decimal import Decimal
    from types import ModuleType

    from brutex.data.fno_symbol import FnoSymbolParts

_LOG = structlog.get_logger(__name__)

#: The opt-in env flag (default OFF; junk fails loud-OFF). ONE flag for the
#: whole brutex-data crate — a future PR-2 facade imports
#: :func:`data_rust_enabled` from here (the cross-facade import precedent).
DATA_RUST_ENV: Final[str] = "BRUTEX_DATA_RUST"

#: Engaged calls that are compared against the live Python oracle before
#: Rust-routed values ship uncompared (the #1531/#1532 pattern).
BURN_IN_SAMPLES: Final[int] = 16

#: The parse-law burn-in budget key (ONE law in PR-1).
_LAW_PARSE: Final[str] = "parse"

#: Process-local OFF latch — the disable reason, or ``None`` when live.
_DISABLED_REASON: Final[list[str | None]] = [None]

#: The locale guard's POSITIVE success sentinel — set ``True`` after the
#: ONE first-engage probe passes, so ``_strptime.LocaleTime()`` is never
#: re-constructed per call (the fail side lives in :data:`_DISABLED_REASON`
#: as ``locale_unsupported``). Cleared ONLY by the test reset seam.
_LOCALE_OK: Final[list[bool]] = [False]

#: Per-law burn-in budgets (mutated in place; reset seam below).
_BURN_IN_REMAINING: Final[dict[str, int]] = {_LAW_PARSE: BURN_IN_SAMPLES}

#: The crate callable the law needs — the SYMBOL handshake surface. A
#: wheel missing it is a pre-T10 install (``crate_stale``).
_CRATE_API: Final[tuple[str, ...]] = ("parse_fno_symbol_coords",)

#: Once-per-process-per-reason log memo (the seam runs per lake-dir entry —
#: thousands per walk; a sticky reason must not flood the logs).
_OBSERVABILITY_MEMO: Final[set[str]] = set()

#: The engaged-INFO memo key (not a fallback reason — namespaced apart).
_ENGAGED_KEY: Final[str] = "__engaged__"

#: The expiry-year window mirrored from ``fno_symbol`` WITHOUT importing it
#: (no runtime cycle — the values are the DEC-FNO-EXPIRY-YEAR-PIVOT-001
#: constants, equality-pinned against the oracle module AND the crate's
#: cargo constants in the unit suites).
EXPIRY_YEAR_MIN_MIRROR: Final[int] = 2019
EXPIRY_YEAR_MAX_MIRROR: Final[int] = 2059

#: The FFI tuple's slot count (the §2.3 layout — 9 named fields).
_COORDS_ARITY: Final[int] = 9

#: The refuse-poison gate's month/day range bounds (pre-``date()``
#: enumeration; the ``date()`` construction stays the calendar backstop).
_MONTH_MAX: Final[int] = 12
_DAY_MAX: Final[int] = 31

#: The narrow degrade-to-oracle catch set (module docstring). MemoryError
#: deliberately absent — OOM re-raises raw, identical to the Python path.
_DEGRADE_ERRORS: Final[tuple[type[Exception], ...]] = (
    ValueError,
    RuntimeError,
    TypeError,
    OverflowError,
    AttributeError,
)

#: The crate's ASCII English month REFERENCE table — a ``tuple`` so the
#: locale-guard equality compare is TYPE-COHERENT (the probe tuple-izes
#: ``LocaleTime.a_month``, a ``list``; ``list == tuple`` is ALWAYS False —
#: the silent permanent-OFF trap the English-locale control test kills).
_ENGLISH_MONTHS: Final[tuple[str, ...]] = (
    "jan",
    "feb",
    "mar",
    "apr",
    "may",
    "jun",
    "jul",
    "aug",
    "sep",
    "oct",
    "nov",
    "dec",
)

#: type_code → (instrument_type, option_type) — the facade-local
#: materialization table (identity strings NEVER cross the FFI).
_TYPE_TABLE: Final[tuple[tuple[str, str | None], ...]] = (
    ("FUT", None),
    ("CE", "CE"),
    ("PE", "PE"),
)


class ParseSymbolLaw(Protocol):
    """The Python parse-law oracle's call shape (``parse_fno_symbol``)."""

    def __call__(self, symbol: str) -> FnoSymbolParts | None:
        """Decode *symbol* into parts, or ``None`` (never raising)."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed


class StrikeTokenLaw(Protocol):
    """The Python strike-token oracle's call shape (``parse_strike_token``)."""

    def __call__(self, token: str) -> Decimal | None:
        """Parse a strike token into a positive ``Decimal``, or ``None``."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed


class BuildPartsLaw(Protocol):
    """The parts-constructor shape (``FnoSymbolParts`` itself satisfies it)."""

    def __call__(
        self,
        *,
        exchange: str,
        underlying: str,
        expiry: date,
        instrument_type: str,
        strike: Decimal | None,
        option_type: str | None,
    ) -> FnoSymbolParts:
        """Construct the decoded-parts value object."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed


def data_rust_enabled() -> bool:
    """Resolve ``BRUTEX_DATA_RUST`` (default OFF, junk loud-OFF).

    Truthy literal → ``True``; falsy literal → ``False``; unset/blank →
    ``False`` (the default); any OTHER non-empty value → ``False`` with one
    ``fno_symbol_rust.fallback`` WARN (``flag_unrecognised``) per process.
    Read per call so spawn workers resolve their own environment (the
    sibling facades' law, byte-identical vocabulary via the imported
    :func:`env_flag_tristate`).
    """
    raw = os.environ.get(DATA_RUST_ENV, "")
    verdict = env_flag_tristate(raw)
    if verdict is not None:
        return verdict
    if raw.strip():
        _warn_once("flag_unrecognised", value=raw)
    return False


def data_rust_disabled() -> bool:
    """``True`` iff a guard/autorevert latched the Rust path OFF this process."""
    return _DISABLED_REASON[0] is not None


def reset_fno_symbol_rust_for_tests(*, remaining: int = BURN_IN_SAMPLES) -> None:
    """Restore the FULL virgin state (test isolation ONLY).

    Clears the latch AND the POSITIVE ``_LOCALE_OK`` sentinel — BOTH halves
    of the two-state locale mechanism (else a real-locale test arm could
    leak a prior English-passed state and wrongly engage) — plus the
    burn-in budget + the warn-once/engaged memos. ``remaining=0`` puts the
    process in the post-burn-in steady state (the Rust-routed value ships
    with no oracle in the path — the differential suites depend on that).
    Never called from production code (the sibling reset-seam convention).
    """
    _DISABLED_REASON[0] = None
    _LOCALE_OK[0] = False
    _BURN_IN_REMAINING[_LAW_PARSE] = remaining
    _OBSERVABILITY_MEMO.clear()


def _live_month_table() -> tuple[str, ...]:
    """The LIVE oracle's lowercase month-abbreviation vocabulary (probe seam).

    ``_strptime.LocaleTime.__init__`` builds ``a_month =
    [calendar.month_abbr[i].lower() for i in range(13)]`` (slot 0 empty) —
    the EXACT surface ``datetime.strptime``'s ``%b`` consults — so probing
    ``LocaleTime`` provably tracks the oracle. The slice is TUPLE-ized to
    match the annotation (``a_month`` is a ``list``; a raw slice would make
    the guard's ``list == tuple`` compare ALWAYS False and silently latch
    the seam OFF everywhere — the English-locale control test kills that).
    Module-level so tests can monkeypatch it (the probe seam). The
    stdlib-private module is resolved via ``importlib`` at probe time (this
    mypy/pyright toolchain ships no ``_strptime`` stub — a static import
    would red the strict gates); ``str()`` re-anchors the ``Any``-typed
    entries onto the annotated ``tuple[str, ...]``.
    """
    strptime_mod = importlib.import_module("_strptime")
    return tuple(str(month) for month in strptime_mod.LocaleTime().a_month[1:])


def _warn_once(reason: str, **context: object) -> None:
    """Log ``fno_symbol_rust.fallback`` WARN once per process per *reason*."""
    if reason in _OBSERVABILITY_MEMO:
        return
    _OBSERVABILITY_MEMO.add(reason)
    _LOG.warning("fno_symbol_rust.fallback", reason=reason, **context)


def _disable(reason: str, *, error: bool = False, **context: object) -> None:
    """Latch the Rust path OFF for the remainder of the process.

    ``error=True`` logs ``fno_symbol_rust.autoreverted`` at ERROR (the
    burn-in autorevert — a genuine wrongness signal); guard latches log
    ``fno_symbol_rust.rust_path_disabled`` at WARN. Repeats are no-ops.
    """
    if _DISABLED_REASON[0] is not None:
        return
    _DISABLED_REASON[0] = reason
    if error:
        _LOG.error("fno_symbol_rust.autoreverted", reason=reason, **context)
    else:
        _LOG.warning("fno_symbol_rust.rust_path_disabled", reason=reason, **context)


def _resolve_crate() -> ModuleType | None:
    """Import guard + SYMBOL handshake: ``brutex_data``, or latch OFF + ``None``.

    An absent wheel (``ImportError``) latches ``crate_unavailable``. A wheel
    that imports but lacks the :data:`_CRATE_API` callable is a pre-T10 (or
    foreign/broken) install — latch ``crate_stale`` (the stale-wheel
    handshake; a wheel VERSION guard is meaningless for a stdlib-pure law —
    the LOCALE guard is this law's lockstep guard, module docstring).
    Fail-closed: never a new crash class.
    """
    try:
        crate = importlib.import_module("brutex_data")
    except ImportError as exc:
        _disable(
            "crate_unavailable",
            error_detail=repr(exc),
            remedy="pip install rust/brutex-data (built by data-rust-parity.yml)",
        )
        return None
    for name in _CRATE_API:
        if not callable(getattr(crate, name, None)):
            _disable(
                "crate_stale",
                error_detail=f"brutex_data.{name} missing or not callable — "
                "the installed wheel predates the fno_symbol law (or is foreign/broken)",
                remedy="rebuild + reinstall rust/brutex-data",
            )
            return None
    return crate


def _locale_engaged() -> bool:
    """The fail-closed LOCALE GUARD (module docstring) — probe once, latch.

    ``True`` iff the live oracle's LC_TIME month vocabulary equals the
    crate's English table. A non-English table — or the probe RAISING (a
    private-surface drift in a future CPython) — latches
    ``locale_unsupported`` (one ``fno_symbol_rust.fallback`` WARN naming
    the degrade + the ``rust_path_disabled`` latch record) and every call
    stays oracle. The POSITIVE outcome memoises in :data:`_LOCALE_OK` so
    ``LocaleTime`` is never re-constructed per call.
    """
    if _LOCALE_OK[0]:
        return True
    try:
        live = _live_month_table()
    except (KeyboardInterrupt, SystemExit, MemoryError):
        raise  # the escape set — OOM/interrupt never a silent latch
    except Exception as exc:
        # probe failure (stdlib-private surface) must latch, never crash a parse.
        _warn_once("locale_unsupported", error_detail=repr(exc))
        _disable("locale_unsupported", error_detail=repr(exc))
        return False
    if live != _ENGLISH_MONTHS:
        _warn_once("locale_unsupported", live_months=list(live))
        _disable("locale_unsupported", live_months=list(live))
        return False
    _LOCALE_OK[0] = True
    return True


def _log_engaged_once(crate: ModuleType) -> None:
    """Log ``fno_symbol_rust.engaged`` INFO once per process."""
    if _ENGAGED_KEY in _OBSERVABILITY_MEMO:
        return
    _OBSERVABILITY_MEMO.add(_ENGAGED_KEY)
    _LOG.info(
        "fno_symbol_rust.engaged",
        crate_version=getattr(crate, "__version__", None),
    )


def _coords_structurally_valid(coords: object, symbol: str) -> bool:
    """The refuse-poison STRUCTURAL gate (module docstring — full enumeration).

    ``True`` only for a 9-tuple of ``int``s whose spans/date/type are
    structurally possible for *symbol*. Deliberately NOT a semantic
    re-verification (burn-in + goldens + differential own that).
    """
    if not (isinstance(coords, tuple) and len(coords) == _COORDS_ARITY):
        return False
    # bool is an int subclass — a poison True/False member must refuse too.
    if not all(type(v) is int for v in coords):
        return False
    type_code, exchange_end, u_start, u_end, year, month, day, s_start, s_end = coords
    length = len(symbol)
    if type_code not in (0, 1, 2):
        return False
    if exchange_end < 1:
        return False  # empty exchange is an oracle None — never materialized
    if u_start != exchange_end + 1:
        return False  # the derived TRUE structural relation
    if not (u_start < u_end <= length):
        # Empty underlying / out-of-bounds span. (An out-of-bounds
        # exchange_end is SUBSUMED: u_start == exchange_end + 1 and
        # u_start < u_end <= length bound exchange_end <= length - 2.)
        return False
    if not (EXPIRY_YEAR_MIN_MIRROR <= year <= EXPIRY_YEAR_MAX_MIRROR):
        return False  # date(1975, 6, 30) constructs cleanly — the window law
    if not (1 <= month <= _MONTH_MAX and 1 <= day <= _DAY_MAX):
        return False  # pre-date() range enumeration (date() stays the backstop)
    if type_code == 0:
        if (s_start, s_end) != (0, 0):
            return False  # FUT: the grammar has NO strike token — absent
    elif not (0 <= s_start < s_end <= length):
        return False  # CE/PE: non-empty in-bounds strike token
    return True


def _parts_equal_strict(a: FnoSymbolParts | None, b: FnoSymbolParts | None) -> bool:
    """Burn-in equality: dataclass equality AND ``str(strike)`` equality.

    Dataclass ``__eq__`` compares ``Decimal`` numerically
    (``Decimal("24000.5") == Decimal("24000.50")``), so the strict form
    additionally pins the REPRESENTATION — both arms build the strike via
    the SAME injected law, so a mismatch can only mean a span-law drift.
    """
    if a is None or b is None:
        return a is None and b is None
    return a == b and str(a.strike) == str(b.strike)


def routed_parse_fno_symbol(
    symbol: str,
    *,
    python_law: ParseSymbolLaw,
    strike_law: StrikeTokenLaw,
    build_parts: BuildPartsLaw,
) -> FnoSymbolParts | None:
    """The routed parse law — flag-gated Rust structure, Python materialization.

    EVERY path returns the oracle-contract result: flag-OFF / latched /
    non-ASCII / guarded / degraded calls return ``python_law(symbol)`` (the
    oracle); a burn-in mismatch returns the ORACLE value + autoreverts; and
    only a guard-clean, burn-in-clean call returns the Rust-routed value.
    The ONLY caller is ``fno_symbol.parse_fno_symbol`` (grep-pinned) — the
    three injected callables are always that module's own oracle helpers.
    """
    if not data_rust_enabled() or data_rust_disabled():
        return python_law(symbol)
    if not symbol.isascii():
        # ASCII pre-gate (module docstring) — a scope gate, deliberately
        # log-free: byte offsets ≡ char offsets no longer holds and the
        # oracle's Unicode-digit acceptance is out of the Rust contract.
        return python_law(symbol)
    crate = _resolve_crate()
    if crate is None:
        return python_law(symbol)
    if not _locale_engaged():
        return python_law(symbol)
    try:
        coords = crate.parse_fno_symbol_coords(symbol)
    except _DEGRADE_ERRORS as exc:
        _warn_once("parse_error", error_detail=repr(exc))
        return python_law(symbol)
    except (KeyboardInterrupt, SystemExit, MemoryError):
        # The escape set (the errors.py doctrine): operator interrupt /
        # interpreter shutdown / OOM stay global — never a silent fallback.
        # Sits ABOVE the panic belt so its `Exception` member can never
        # swallow a MemoryError.
        raise
    except _panic_belt_errors() as exc:
        _disable("crate_panic", error_detail=repr(exc))
        return python_law(symbol)

    if coords is None:
        rust_result: FnoSymbolParts | None = None
    else:
        if not _coords_structurally_valid(coords, symbol):
            _warn_once(
                "result_refused",
                result=repr(coords),
                symbol=repr(symbol),
            )
            return python_law(symbol)
        type_code, exchange_end, u_start, u_end, year, month, day, s_start, s_end = coords
        try:
            expiry = date(year, month, day)
        except ValueError as exc:
            # The calendar backstop (31Apr / 29Feb-nonleap poison shapes).
            _warn_once("result_refused", error_detail=repr(exc), symbol=repr(symbol))
            return python_law(symbol)
        instrument_type, option_type = _TYPE_TABLE[type_code]
        strike: Decimal | None = None
        strike_rejected = False
        if type_code != 0:
            strike = strike_law(symbol[s_start:s_end])
            strike_rejected = strike is None
        if strike_rejected:
            # Option strike-token reject: the Rust-located span materialized
            # to a token the SHARED injected law refuses. Falls THROUGH to
            # the burn-in compare like every other decision (the round-1
            # review-fix fold — module docstring): a drifted/poisoned wheel
            # pointing the strike span at an in-bounds non-numeric token for
            # an oracle-ACCEPTED option is a false-reject the burn-in window
            # catches (autorevert + ship the oracle); post-burn-in the arm
            # ships within the same burned-in envelope as every decision.
            rust_result = None
        else:
            rust_result = build_parts(
                exchange=symbol[:exchange_end],
                underlying=symbol[u_start:u_end],
                expiry=expiry,
                instrument_type=instrument_type,
                strike=strike,
                option_type=option_type,
            )

    _log_engaged_once(crate)
    if _BURN_IN_REMAINING[_LAW_PARSE] > 0:
        oracle_result = python_law(symbol)
        if not _parts_equal_strict(rust_result, oracle_result):
            _disable(
                "burn_in_mismatch_parse",
                error=True,
                rust_result=repr(rust_result),
                python_result=repr(oracle_result),
                symbol=repr(symbol),
            )
            return oracle_result
        _BURN_IN_REMAINING[_LAW_PARSE] -= 1
        if _BURN_IN_REMAINING[_LAW_PARSE] == 0:
            _LOG.info(
                "fno_symbol_rust.burn_in_complete",
                law=_LAW_PARSE,
                samples=BURN_IN_SAMPLES,
            )
    return rust_result


__all__ = [
    "BURN_IN_SAMPLES",
    "DATA_RUST_ENV",
    "EXPIRY_YEAR_MAX_MIRROR",
    "EXPIRY_YEAR_MIN_MIRROR",
    "BuildPartsLaw",
    "ParseSymbolLaw",
    "StrikeTokenLaw",
    "data_rust_disabled",
    "data_rust_enabled",
    "reset_fno_symbol_rust_for_tests",
    "routed_parse_fno_symbol",
]
