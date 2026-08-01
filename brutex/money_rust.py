"""Default-ON Rust seam for the DEC-MONEY-2DP-001 money laws (slice T35).

Default flipped ON by DEC-RUST-DEFAULT-ON-WAVE-001 (operator directive
2026-07-23); explicit falsy is the kill switch back to the Decimal oracle.

DEC-CONFIG-RUST-LEAVES-001 (MIGRATION.md #1943). The FOUR routed surfaces
are :mod:`brutex.money`'s public functions — when ``BRUTEX_CORE_RUST_MONEY``
is ON they delegate to the ``brutex_core`` crate
(``rust/brutex-core/src/money.rs``, exact decimal-STRING arithmetic)
through this facade; the flag-OFF default is the verbatim CPython
``Decimal`` oracle, output byte-identical (behavior-identical; see the
r1 wording correction in MIGRATION #1943) AND crate-untouched
(bomb-crate spy-pinned).

Honesty banner (ADR-0020, mandatory verbatim): expected payoff ≈ 0 —
conversion is for uniformity, not speed. Every one of these laws is a
sub-microsecond ``Decimal`` op; flag-ON ADDS an env read, an eligibility
gate and an FFI hop per call. ``round_money`` additionally snaps EVERY
stored OHLC value at the lake-write boundary (run-identity /
stored-bytes critical — the audit-session addendum), which is exactly
why the seam is burn-in-checked, refuse-poison-gated and default-ON
(DEC-RUST-DEFAULT-ON-WAVE-001).

MEASURED COST (2026-08-01, DEC-MONEY-RUST-WRAPPER-COST-001 — this
supersedes the pre-measurement "a small slowdown" / "lake-write HOT path
is unaffected … aggregate ~seconds" wording, both of which were
falsified on the build box):

* per call, ``round_money(Decimal("14020.7999"))`` steady state:
  flag-ON 6,638 ns vs flag-OFF 1,019 ns — **6.5× slower**, NOT "small";
* the Rust kernel itself is NOT the cost. ``brutex_core.round_money_str``
  measures **265 ns**, against the **406 ns** CPython ``quantize`` oracle
  it replaces — the crate is 1.5× FASTER at the actual arithmetic. The
  slowdown is entirely this facade's PYTHON guard rails, which measured
  ~5.9 µs (≈22× the kernel): eligibility gate 1,322 ns (of which
  ``as_tuple()`` alone is 541), shape validation 1,508 ns, crate resolve
  911 ns, flag probe 542 ns, marshalling 328 ns, reconstruct 243 ns;
* the SWEEP path DOES pay it. ``Stage1AMatrixBuilder.collect_batch_inputs``
  calls ``_price_to_paisa`` (→ ``round_money``) per OHLC field per bar, so
  over the 1,440-bar fixture the prepass measures **24.1 ms flag-OFF →
  78.7 ms flag-ON (3.26×, +54.5 ms per tuple)**. The earlier "hot path is
  unaffected" claim was scoped to the LAKE write — where the vectorized
  ``_snap_price_expr`` genuinely never calls scalar ``round_money`` — and
  wrongly generalised from there.

Two wrapper costs are cut by this DEC (both behaviour-preserving, both
re-measured below): the per-call crate resolve is MEMOIZED (success only)
and the shape-check digit scan uses the C-level ``str`` predicates. The
remainder is irreducible without moving the eligibility gate itself into
Rust — a separate, larger DEC. Flag-OFF cost is unchanged: the DEFAULT
path still pays a per-call env probe (~700 ns ``os.environ.get`` miss),
DELIBERATELY — spawn workers must read their own inherited environment
(the PR-1 posture; caching the flag resolve would break that contract).

The :mod:`brutex.data.lake_rust_stats` facade pattern, with TWO deliberate
structural differences, both documented:

* **A LOCAL flag-vocabulary twin, not an import** (the
  :mod:`brutex.feed_rust` precedent): ``brutex/money.py`` is a ROOT leaf —
  ``brutex.data.parquet_writer`` (and ~19 other modules across data /
  engine / dashboard / costs) import it at module scope, so this facade
  must NOT import ``env_flag_tristate`` from
  ``brutex.data.lake_rust_render`` (executing ``brutex/data/__init__``
  from under ``brutex.money`` would be a circular import) nor from
  ``brutex.engine.sweep_lookahead`` (``brutex/engine/__init__``
  hard-imports the ``fno_math`` wheel — a money render must never require
  the engine wheel). The inline ``_TRUTHY``/``_FALSY`` sets are
  mirror-pinned against ``sweep_lookahead``'s by a vocabulary-equality
  test (``tests/unit/test_money_rust.py``), so the homes cannot drift
  silently.
* **No version guard** (the ``lake_rust_stats`` reasoning verbatim): the
  crate side is pure std-Rust digit arithmetic locked to the *stdlib*
  ``decimal`` byte law — there is no wheel↔crate lockstep to attest. The
  per-law burn-in bit-compare + the frozen literal goldens (Python:
  ``tests/golden/test_money_rounding_golden.py``; cargo: the snap-corpus
  cross-crate pin) are the drift teeth instead.

Fail-closed layers, in order (every path returns the PYTHON-law value):

1. the caller-side flag gate in :mod:`brutex.money`
   (:func:`money_rust_enabled` — default ON per
   DEC-RUST-DEFAULT-ON-WAVE-001; explicit falsy is the kill switch; junk
   values loud-OFF once
   per process);
2. the ELIGIBILITY gate (:func:`_eligible_rupees` /
   :func:`_eligible_paisa` — the gate law below): an input whose oracle
   behavior could be raise-vs-value divergent is routed to the oracle
   WITHOUT touching the crate;
3. the import/API-surface guard (:func:`_resolve_crate` — absent wheel
   latches ``crate_unavailable``; a wheel missing a callable latches
   ``crate_stale``);
4. the refuse-poison gate — a non-``str`` crate return, or a returned
   string failing its law's cheap shape check, is refused with one WARN
   (a poisoned crate can only degrade, never serve);
5. the per-law burn-in self-check — the first :data:`BURN_IN_SAMPLES`
   engaged calls PER LAW compute BOTH arms and compare the canonical
   strings; ANY difference ships the ORACLE value and autoreverts the
   Rust path for the process (the #1531/#1532 pattern; per-law budgets
   per the :mod:`brutex.data.window_ledger_rust` precedent);
6. the degrade set (:data:`_DEGRADE_ERRORS` + the pyo3 panic belt) — a
   crate raise serves the oracle with one WARN, never a new crash class.

``MemoryError`` / ``KeyboardInterrupt`` / ``SystemExit`` re-raise RAW (the
``errors.py`` escape-set doctrine): money laws run inside cost/lake-write
paths whose upstream policy treats an OOM as a global stop — this seam
must not launder one into a fallback.

## Strict mode (DEC-RUST-STRICT-NO-FALLBACK-001)

Layers 1 (junk flag), 3, 4, 5 and 6 all funnel through exactly TWO choke
points — :func:`_warn_once` and :func:`_disable` — so
:func:`~brutex.config.rust_strict.refuse_if_strict` at those two points is
a *complete* block on the Python arm: there is no unannounced path around
them. Every reason that reaches them means **Rust is absent, latched,
broke, or answered wrongly** (``flag_unrecognised`` / ``crate_unavailable``
/ ``crate_stale`` / ``crate_panic`` / ``<law>_error`` /
``<law>_result_refused`` / ``burn_in_mismatch_<law>``) — so this facade
needs NO per-reason exemption allowlist, and refusing all of them turns no
legitimate input into a crash.

**Why layer 2 is deliberately NOT wired** — the ELIGIBILITY GATE
(:func:`_eligible_rupees` / :func:`_eligible_paisa`, law below) is the
CLASS-C case the ``rust_strict`` docstring names: an input the crate
deliberately cannot represent (a non-``Decimal``, a ``NaN``, an armed
``Inexact`` trap, a narrowed exponent window …) was ALWAYS meant to be
served by the Python oracle — it is not a Rust failure, and refusing it
would turn a legitimate call into a crash. The gate needs no exemption
list because it is structurally outside the choke points: ``route_*``
returns ``python_law(...)`` *before* any warn/latch, so an ineligible
input reaches neither :func:`_warn_once` nor :func:`_disable`. That
silence is also why the fall-back census cannot see the gate — a
pre-existing, deliberate property of layer 2, unchanged here.

Because :func:`_disable` refuses before it latches, the process-local
``money_rust_disabled()`` short-circuit in ``route_*`` is UNREACHABLE
under strict mode (every call re-attempts and re-refuses, loudly) — there
is no silently-latched steady state for Python to serve through.

The ``rust_strict`` import is DELIBERATELY function-scope. ``brutex.money``
is a ROOT leaf: at module scope ``brutex.config.rust_strict`` executes
``brutex/config/__init__`` → ``config.runtime`` → ``brutex.data.atomic_io``
→ ``brutex/data/__init__`` → ``data.parquet_writer``, which imports
``brutex.money`` — a genuine cycle, executed + confirmed
(``ImportError: cannot import name 'round_money' from partially
initialized module 'brutex.money'``). Deferring the import to the
fall-back path (already a warn-once-rare, already-degraded path) keeps the
module-scope import surface stdlib + structlog, exactly as the AST leaf
pin requires.

## The ELIGIBILITY GATE LAW (layer 2 — the strings-only FFI contract)

Only values whose oracle outcome is a PLAIN VALUE under the AMBIENT
decimal context may cross (anything else → oracle, crate untouched,
spy-pinned):

* rupee ``Decimal``s: exact type :class:`~decimal.Decimal` (a subclass's
  overridden methods must reach the oracle — the ``window_ledger_rust``
  exact-type precedent; a non-Decimal reaches the oracle and reproduces
  today's exact ``AttributeError``), FINITE (``NaN``/``sNaN``/``±Inf``
  quantize-raise in the oracle — preserved by routing them there),
  ``as_tuple().exponent >= -60`` and ``adjusted() <= 20`` (bounds the
  ``format(d, "f")`` crossing string to ~85 chars — every real money
  value is orders of magnitude inside), ``adjusted() + 5 <=
  getcontext().prec`` (the quantize/×100 headroom law: a 2dp quantize
  needs ``adjusted+3`` result digits and the ``× 100`` shift two more —
  past that the oracle raises ``InvalidOperation`` at default traps, so
  those inputs must take the oracle), NO ``Inexact``/``Rounded``
  trap armed (a caller that traps them makes the oracle RAISE on any
  rounding — raise-vs-value divergence, so trapped contexts take the
  oracle wholesale), and an ambient EXPONENT WINDOW covering the laws'
  whole result envelope — ``Emax >= 25`` AND ``Emin <= -65`` (the r2
  fold: under a narrower window the oracle can raise
  ``Overflow``/``InvalidOperation`` — the quantize range check vs
  ``Emax`` and ``Etiny = Emin − prec + 1`` — where the
  context-INDEPENDENT crate returns a value; executed repro:
  ``localcontext(Emax=1)`` → ``round_money(Decimal("100"))`` flag-ON
  served ``100.00`` where flag-OFF raises ``InvalidOperation``. Bound
  derivation on the constants below);
* paisa ``int``s: exact type :class:`int` (``bool`` is an ``int``
  subclass whose ``str()`` is not a digit string — oracle), ``|paisa| <
  2**63`` (the milestone gate law; the crossing is a string either way —
  this bounds the crate's worst case), digit-count headroom
  ``len(str(abs(paisa))) + 2 <= getcontext().prec`` (the ``/ 100`` +
  quantize exactness law under a shrunken ambient precision), and the
  same trap + exponent-window conjuncts.
"""

from __future__ import annotations

import importlib
import os
from decimal import Decimal, Inexact, Rounded, getcontext
from typing import TYPE_CHECKING, Final

import structlog

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import ModuleType

_LOG = structlog.get_logger(__name__)

#: The env flag (default ON per DEC-RUST-DEFAULT-ON-WAVE-001; explicit
#: falsy is the kill switch). Canonical ``BRUTEX_<CRATE>_RUST_<SEAM>`` form.
MONEY_RUST_ENV: Final[str] = "BRUTEX_CORE_RUST_MONEY"

#: Engaged calls bit-compared against the Decimal oracle PER LAW before
#: crate strings ship un-oracled (the #1531/#1532 burn-in law).
BURN_IN_SAMPLES: Final[int] = 16

#: The four law tokens (per-law burn-in budgets + reason prefixes).
_LAW_ROUND: Final[str] = "round_money"
_LAW_TO_PAISA: Final[str] = "rupees_to_paisa"
_LAW_TO_RUPEES: Final[str] = "paisa_to_rupees"
_LAW_FORMAT: Final[str] = "format_inr"

#: The process-local autorevert latch — ``[None]`` = live; a reason string
#: latches the Rust path OFF for the remainder of the process.
_DISABLED_REASON: Final[list[str | None]] = [None]

#: Per-law burn-in budgets (mutated in place; reset seam below).
_BURN_IN_REMAINING: Final[dict[str, int]] = {
    _LAW_ROUND: BURN_IN_SAMPLES,
    _LAW_TO_PAISA: BURN_IN_SAMPLES,
    _LAW_TO_RUPEES: BURN_IN_SAMPLES,
    _LAW_FORMAT: BURN_IN_SAMPLES,
}

#: Warn-once registry (per-process, test-resettable — money laws run at
#: per-value frequency; a sticky reason would flood the logs).
_OBSERVABILITY_MEMO: Final[set[str]] = set()

#: Resolved-crate memo (``[None]`` = not yet resolved this process) —
#: DEC-MONEY-RUST-WRAPPER-COST-001. :func:`_resolve_crate` measured 911
#: ns/call, 13.7% of an engaged ``round_money``, because
#: ``importlib.import_module`` costs ~574 ns even on a ``sys.modules``
#: HIT and the 4-symbol handshake another ~337.
#:
#: THE MODULE is memoized, never a bound callable: ``_adopt_crate_string``
#: still does ``getattr(crate, crate_name)`` per call, so a test that
#: monkeypatches a crate ATTRIBUTE (``test_money_rust.py``'s real-wheel
#: spy) is still seen — a callable memo would silently miss it.
#:
#: SUCCESS ONLY. A FAILED resolve is never cached, because both failure
#: arms go through :func:`_disable`, which under strict mode REFUSES
#: *before* it latches — the module docstring's "every call re-attempts
#: and re-refuses, loudly; there is no silently-latched steady state"
#: contract. Caching a failure would manufacture exactly the latched
#: state strict mode exists to forbid.
#:
#: Cleared by :func:`reset_money_rust_for_tests`, which
#: ``test_money_rust.py`` runs from an ``autouse`` fixture BEFORE AND
#: AFTER every test — so the ``sys.modules`` crate swaps in that file
#: (``_arm``) are never served a stale memo.
_CRATE_MEMO: Final[list[ModuleType | None]] = [None]

#: The engaged-INFO memo key (never a warn reason).
_ENGAGED_KEY: Final[str] = "__engaged__"

#: The crate callables this seam requires (the API-surface handshake).
_CRATE_API: Final[tuple[str, ...]] = (
    "round_money_str",
    "rupees_to_paisa_str",
    "paisa_to_rupees_str",
    "format_inr_str",
)

#: The narrow degrade-to-oracle catch set. ``ValueError`` is the crate's
#: declared parse/shape refusal; ``RuntimeError`` its declared panic
#: mapping; ``TypeError``/``OverflowError`` are PyO3 argument-conversion
#: failures; ``AttributeError`` the broken-crate-internals belt.
#: ``MemoryError`` is DELIBERATELY absent (module docstring).
_DEGRADE_ERRORS: Final[tuple[type[Exception], ...]] = (
    ValueError,
    RuntimeError,
    TypeError,
    OverflowError,
    AttributeError,
)

#: Inline flag vocabulary (LEAF discipline — module docstring; the
#: mirror-pin test asserts byte-equality with ``sweep_lookahead``'s sets).
_TRUTHY: Final[frozenset[str]] = frozenset({"1", "true", "yes", "on"})
_FALSY: Final[frozenset[str]] = frozenset({"0", "false", "no", "off"})

#: Eligibility-gate bounds (the gate law — module docstring).
_MAX_ADJUSTED: Final[int] = 20
_MIN_EXPONENT: Final[int] = -60
_PREC_HEADROOM: Final[int] = 5
_MAX_PAISA: Final[int] = 2**63

#: Ambient exponent-window safe bounds (the r2 context-narrowing gate).
#: DERIVATION from the gate's own envelope: eligible rupee inputs carry
#: ``adjusted() <= 20`` / ``exponent >= -60`` (paisa ints: <= 19 digits,
#: exponent 0). Result envelope: a 2dp quantize can CARRY one digit
#: (``9.99… → 10.00``, adjusted +1) and the ``× 100`` shift in
#: ``rupees_to_paisa`` adds two more, so result adjusted reaches 23
#: EXACTLY (probed 2026-07-17: ``Emax=22`` overflows the ceiling input,
#: ``Emax=23`` is clean); the ``/ 100`` in ``paisa_to_rupees`` subtracts
#: 2 and the rupee quantize target is exponent −2, so no operand or
#: result exponent sits below −62 (only the raw INPUT carries −60).
#: ``Emax >= 25`` / ``Emin <= -65`` therefore cover the whole envelope
#: with ≥ 2 exponent-positions of headroom on each side (``Etiny =
#: Emin − prec + 1 <= Emin <= -65 < -62``, so the quantize-vs-Etiny
#: range check can never fire either). Under a NARROWER window the
#: oracle can raise ``Overflow``/``InvalidOperation`` where the
#: context-independent crate returns a value (executed repro:
#: ``localcontext(Emax=1)`` → ``round_money(Decimal("100"))`` ON
#: ``100.00`` vs OFF ``InvalidOperation``; the ``Emin`` arm needs a
#: jointly-narrowed prec — ``(prec=2, Emin=0)`` puts ``Etiny=-1`` above
#: the −2 quantize target), so such contexts route to the oracle
#: WHOLESALE (the armed-trap posture).
_MIN_SAFE_EMAX: Final[int] = 25
_MAX_SAFE_EMIN: Final[int] = -65

#: ASCII-strict digit set for the refuse-poison shape checks (``int()`` /
#: ``Decimal()`` would happily parse Unicode digits and ``int()`` even
#: ``"1_0"`` — the validators, not the constructors, are the gate).
_ASCII_DIGITS: Final[frozenset[str]] = frozenset("0123456789")

#: Shape-law lengths: a 2dp body is at least ``D.dd`` (4 chars) and a
#: display grouping chunk is exactly 3 digits (Python ``:,`` grouping).
_MIN_FIXED2_LEN: Final[int] = 4
_GROUP_LEN: Final[int] = 3


def money_rust_enabled() -> bool:
    """Resolve ``BRUTEX_CORE_RUST_MONEY`` (default ON, junk loud-OFF).

    DEC-RUST-DEFAULT-ON-WAVE-001 (operator directive 2026-07-23): truthy →
    ``True``; falsy → ``False`` (the silent explicit kill switch); unset /
    whitespace-empty → ``True`` (the default); any OTHER non-empty value →
    ``False`` with one ``money_rust.fallback`` WARN (reason
    ``flag_unrecognised``) per process — the junk arm is unchanged. Read
    per call so spawn workers resolve their own inherited environment (the
    PR-1 posture).

    ABSENT fast path (review r1, MINOR): an UNSET flag — the production
    default — returns ``True`` off the raw ``environ`` probe alone,
    skipping the strip/lower/set-lookup normalization; any SET value takes
    the full tristate handling byte-identically.
    """
    raw = os.environ.get(MONEY_RUST_ENV)
    if raw is None:
        return True
    stripped = raw.strip().lower()
    if stripped in _TRUTHY:
        return True
    if stripped in _FALSY:
        return False
    if stripped:
        _warn_once("flag_unrecognised", value=stripped)
        return False
    return True


def money_rust_disabled() -> bool:
    """``True`` iff a guard/autorevert latched the Rust path OFF this process."""
    return _DISABLED_REASON[0] is not None


def reset_money_rust_for_tests(*, remaining: int = BURN_IN_SAMPLES) -> None:
    """Reset the latch + per-law burn-in budgets + warn memo (test isolation ONLY).

    ``remaining=0`` puts the process in the post-burn-in steady state so
    the adopted value is the crate's with no oracle in the path (the
    parity legs depend on that — an armed burn-in would cushion a
    divergence by shipping oracle values). Never called from production
    paths (the PR-1 reset-seam convention).

    Clears :data:`_CRATE_MEMO` too (DEC-MONEY-RUST-WRAPPER-COST-001) — a
    test that swaps ``sys.modules["brutex_core"]`` must never be served a
    crate resolved before its swap.
    """
    _DISABLED_REASON[0] = None
    for law in _BURN_IN_REMAINING:
        _BURN_IN_REMAINING[law] = remaining
    _OBSERVABILITY_MEMO.clear()
    _CRATE_MEMO[0] = None


def _refuse_if_strict(reason: str, **context: object) -> None:
    """Raise :class:`~brutex.config.rust_strict.RustFallbackRefused` if strict.

    The ONE strict-mode adapter for this facade, shared by both choke points
    (:func:`_warn_once`, :func:`_disable`) — DEC-RUST-STRICT-NO-FALLBACK-001.
    Every reason reaching either choke point means Rust is absent / latched /
    broke / answered wrongly, so there is no exemption allowlist here; the
    Class-C eligibility gate never reaches this function (module docstring).

    The import is function-scope BY NECESSITY, not style: at module scope it
    closes an executed-and-confirmed ``brutex.money`` ⇄ ``brutex.data``
    circular import (module docstring). A ``sys.modules`` hit on an already
    degraded path is a non-cost.
    """
    from brutex.config.rust_strict import refuse_if_strict

    refuse_if_strict("brutex.money_rust", reason, **context)


def _refuse_if_strict(reason: str, **context: object) -> None:
    """Raise :class:`~brutex.config.rust_strict.RustFallbackRefused` if strict.

    The ONE strict-mode adapter for this facade, shared by both choke points
    (:func:`_warn_once`, :func:`_disable`) — DEC-RUST-STRICT-NO-FALLBACK-001.
    Every reason reaching either choke point means Rust is absent / latched /
    broke / answered wrongly, so there is no exemption allowlist here; the
    Class-C eligibility gate never reaches this function (module docstring).

    The import is function-scope BY NECESSITY, not style: at module scope it
    closes an executed-and-confirmed ``brutex.money`` ⇄ ``brutex.data``
    circular import (module docstring). A ``sys.modules`` hit on an already
    degraded path is a non-cost.
    """
    from brutex.config.rust_strict import refuse_if_strict

    refuse_if_strict("brutex.money_rust", reason, **context)


def _warn_once(reason: str, **context: object) -> None:
    """Refuse under strict mode; else log ``money_rust.fallback`` warn-once.

    One of the TWO choke points every Python fall-back funnels through
    (:func:`_disable` is the other), so refusing here is a *complete* block —
    see the module docstring's strict-mode section for the reason census and
    for why the eligibility gate is deliberately exempt by construction.
    With strict mode OFF the historic warn-once behaviour is byte-identical.
    """
    _refuse_if_strict(reason, **context)
    if reason in _OBSERVABILITY_MEMO:
        return
    _OBSERVABILITY_MEMO.add(reason)
    _LOG.warning("money_rust.fallback", reason=reason, **context)


def _disable(reason: str, *, error: bool = False, **context: object) -> None:
    """Latch the Rust path OFF for the remainder of the process.

    ``error=True`` logs ``money_rust.autoreverted`` at ERROR (the burn-in
    autorevert — a genuine wrongness signal on a stored-bytes-critical
    law); guard latches log ``money_rust.rust_path_disabled`` at WARN.
    Repeats are no-ops.

    Under strict mode this REFUSES before latching (the second choke point —
    DEC-RUST-STRICT-NO-FALLBACK-001), which is why the latched state is
    unreachable there: every later call re-attempts and re-refuses rather
    than degrading silently behind ``money_rust_disabled()``.
    """
    _refuse_if_strict(reason, **context)
    if _DISABLED_REASON[0] is not None:
        return
    _DISABLED_REASON[0] = reason
    if error:
        _LOG.error("money_rust.autoreverted", reason=reason, **context)
    else:
        _LOG.warning("money_rust.rust_path_disabled", reason=reason, **context)


def _resolve_crate() -> ModuleType | None:
    """Import guard + SYMBOL handshake: ``brutex_core``, or latch OFF + ``None``.

    An absent wheel (``ImportError``) latches ``crate_unavailable``. A
    wheel that imports but lacks any callable :data:`_CRATE_API` member is
    a pre-money-seam (or foreign/broken) install — latch ``crate_stale``
    (the ``window_ledger_rust`` stale-wheel handshake; no version guard —
    module docstring). Fail-closed: never an ``AttributeError`` out of
    this seam.

    A SUCCESSFUL resolve is memoized in :data:`_CRATE_MEMO` (module
    constant — the reasoning, and why failures are deliberately NOT
    cached, is on that constant). Cleared by
    :func:`reset_money_rust_for_tests`.
    """
    memo = _CRATE_MEMO[0]
    if memo is not None:
        return memo
    try:
        crate = importlib.import_module("brutex_core")
    except ImportError as exc:
        _disable(
            "crate_unavailable",
            error_detail=repr(exc),
            remedy="pip install rust/brutex-core (maturin build; see rust/brutex-core/README.md)",
        )
        return None
    for name in _CRATE_API:
        if not callable(getattr(crate, name, None)):
            _disable(
                "crate_stale",
                error_detail=f"brutex_core.{name} missing or not callable — "
                "the installed wheel predates the money seam (or is foreign/broken)",
                remedy="rebuild + reinstall rust/brutex-core (see rust/brutex-core/README.md)",
            )
            return None
    _CRATE_MEMO[0] = crate
    return crate


def _panic_belt_errors() -> tuple[type[BaseException], ...]:
    """The narrow FFI panic-belt catch set — the ``lake_rust_stats`` twin.

    ``pyo3_runtime.PanicException`` is the one ``BaseException`` subclass a
    pyo3 boundary can leak; the repo convention (CodeQL
    ``py/catch-base-exception``) forbids a non-re-raising literal ``except
    BaseException``, so the class is resolved GUARDEDLY by name and folded
    beside ``Exception``. ONE home is probed — ``pyo3_runtime`` (the
    ``polars.exceptions`` home of the lake twins is irrelevant to a
    polars-free crate; a documented narrowing). The crate's own
    ``catch_unwind`` maps panics to ``RuntimeError`` (the degrade set), so
    this belt is defense-in-depth over a broken boundary.
    """
    belt: list[type[BaseException]] = [Exception]
    try:
        module = importlib.import_module("pyo3_runtime")
    except ImportError:
        return tuple(belt)
    panic = getattr(module, "PanicException", None)
    if isinstance(panic, type) and issubclass(panic, BaseException) and panic not in belt:
        belt.append(panic)
    return tuple(belt)


def _log_engaged_once(crate: ModuleType) -> None:
    """Log ``money_rust.engaged`` INFO once per process."""
    if _ENGAGED_KEY in _OBSERVABILITY_MEMO:
        return
    _OBSERVABILITY_MEMO.add(_ENGAGED_KEY)
    _LOG.info("money_rust.engaged", crate_version=getattr(crate, "__version__", None))


def _context_hazard() -> bool:
    """``True`` iff the ambient context could make the oracle RAISE where
    the context-independent crate returns a value (gate law): an armed
    ``Inexact``/``Rounded`` trap, or an exponent window narrower than the
    laws' whole result envelope (``Emax < 25`` / ``Emin > -65`` — the r2
    fold; derivation on :data:`_MIN_SAFE_EMAX`/:data:`_MAX_SAFE_EMIN`).
    """
    ctx = getcontext()
    if ctx.traps[Inexact] or ctx.traps[Rounded]:
        return True
    return ctx.Emax < _MIN_SAFE_EMAX or ctx.Emin > _MAX_SAFE_EMIN


def _eligible_rupees(rupees: object) -> bool:
    """The rupee-input eligibility gate (module docstring gate law)."""
    if type(rupees) is not Decimal or not rupees.is_finite():
        return False
    adjusted = rupees.adjusted()
    # exponent = adjusted − len(digits) + 1 (the ``as_tuple`` identity,
    # computed int-only — a FINITE Decimal's ``as_tuple().exponent`` is
    # always an int, but its TYPE carries the non-finite str markers, so
    # deriving it keeps mypy strict clean with no dead defensive arm).
    exponent = adjusted - len(rupees.as_tuple().digits) + 1
    if exponent < _MIN_EXPONENT:
        return False
    if adjusted > _MAX_ADJUSTED or adjusted + _PREC_HEADROOM > getcontext().prec:
        return False
    return not _context_hazard()


def _eligible_paisa(paisa: object) -> bool:
    """The paisa-input eligibility gate (module docstring gate law)."""
    if type(paisa) is not int:
        return False
    if not -_MAX_PAISA < paisa < _MAX_PAISA:
        return False
    if len(str(abs(paisa))) + 2 > getcontext().prec:
        return False
    return not _context_hazard()


def _is_digits(chunk: str) -> bool:
    """``True`` iff *chunk* is non-empty and ASCII-0-9 only (shape checks).

    Uses the C-level ``str`` predicates rather than a Python-level scan
    over :data:`_ASCII_DIGITS` (DEC-MONEY-RUST-WRAPPER-COST-001): measured
    615 → 105 ns on a 5-char chunk (5.9×), and :func:`_valid_fixed2_str`
    calls this TWICE per validated crate return.

    EXACT-EQUIVALENCE PROOF (executed 2026-08-01, not asserted) — the swap
    is byte-for-byte the same predicate:

    * ``str.isdigit()`` is ``True`` iff the string is NON-EMPTY and every
      character is a Unicode digit, so it already carries the
      ``bool(chunk)`` conjunct;
    * ``str.isascii()`` is ``True`` iff every character is < U+0080 (and
      vacuously ``True`` for ``""`` — irrelevant, ``isdigit()`` has
      already rejected it);
    * their conjunction is therefore "non-empty AND every char is an
      ASCII Unicode-digit". The ASCII Unicode-digits are EXACTLY U+0030–
      U+0039 = :data:`_ASCII_DIGITS` — verified EXHAUSTIVELY over all
      0x110000 code points: zero divergences. The classic traps are
      covered by that sweep — ``"²"`` (U+00B2) is ``isdigit()`` but not
      ``isascii()``; ``"١٢٣"``/``"１２３"`` are ``isdigit()`` but not
      ``isascii()``; ``"1_0"``/``"+12"``/``"12 "`` fail ``isdigit()``.

    :data:`_ASCII_DIGITS` is retained as the DECLARATIVE statement of the
    law (and is what the proof above is stated against).
    """
    return chunk.isdigit() and chunk.isascii()


def _valid_fixed2_str(value: str) -> bool:
    """Shape law for a 2dp fixed-point crate return (``-?\\d+\\.\\d\\d``)."""
    body = value.removeprefix("-")
    if len(body) < _MIN_FIXED2_LEN or body[-3] != ".":
        return False
    return _is_digits(body[:-3]) and _is_digits(body[-2:])


def _valid_int_str(value: str) -> bool:
    """Shape law for an integer paisa crate return (``-?\\d+``, ASCII-strict)."""
    return _is_digits(value.removeprefix("-"))


def _valid_inr_str(value: str) -> bool:
    """Shape law for a ``format_inr`` crate return (``₹-?\\d{1,3}(,\\d{3})*\\.\\d\\d``)."""
    if not value.startswith("₹"):
        return False
    body = value[1:].removeprefix("-")
    if len(body) < _MIN_FIXED2_LEN or body[-3] != ".":
        return False
    if not _is_digits(body[-2:]):
        return False
    groups = body[:-3].split(",")
    if not 1 <= len(groups[0]) <= _GROUP_LEN or not _is_digits(groups[0]):
        return False
    return all(len(group) == _GROUP_LEN and _is_digits(group) for group in groups[1:])


def _adopt_crate_string(
    law: str,
    crate_name: str,
    argument: str,
    validate: Callable[[str], bool],
    oracle_canonical: Callable[[], str],
) -> str | None:
    """Run layers 3-6 for one engaged call; return the crate string to
    ADOPT, or ``None`` (= serve the Python oracle).

    ``oracle_canonical`` renders the oracle result in the law's canonical
    string form (``str()`` of the Decimal/int, the ``format_inr`` string
    itself) — only invoked while the law's burn-in budget is live. A
    burn-in mismatch autoreverts and returns ``None``: the caller
    recomputes the (pure, deterministic) oracle — one redundant compute on
    the one-shot autorevert path, never on the steady state.
    """
    crate = _resolve_crate()
    if crate is None:
        return None
    try:
        rust_value = getattr(crate, crate_name)(argument)
    except _DEGRADE_ERRORS as exc:
        _warn_once(f"{law}_error", error_detail=repr(exc))
        return None
    except (KeyboardInterrupt, SystemExit, MemoryError):
        # The escape set (the errors.py doctrine): operator interrupt /
        # interpreter shutdown / OOM stay global — never a silent
        # fallback. Sits ABOVE the panic belt so its `Exception` member
        # can never swallow a MemoryError.
        raise
    except _panic_belt_errors() as exc:
        # A panic escaping the crate's catch_unwind boundary (or any
        # out-of-degrade-set Exception) marks a broken FFI contract —
        # latch OFF and serve the oracle, never a new crash class into a
        # cost/lake-write path.
        _disable("crate_panic", error_detail=repr(exc))
        return None
    if not isinstance(rust_value, str) or not validate(rust_value):
        # Refuse-poison gate: a non-str return or a shape-law violation is
        # a crate BUG — never served, never even CONSTRUCTED from (the
        # validators are ASCII-strict, so `Decimal(...)`/`int(...)` on an
        # adopted string cannot raise — the shape check IS the
        # construction guard). Structurally unreachable through the real
        # crate; guards a stale/foreign install.
        head = repr(rust_value[:24]) if isinstance(rust_value, str) else None
        _warn_once(
            f"{law}_result_refused",
            result_type=type(rust_value).__name__,
            value_head=head,
        )
        return None
    _log_engaged_once(crate)
    if _BURN_IN_REMAINING[law] > 0:
        oracle_value = oracle_canonical()
        if rust_value != oracle_value:
            _disable(
                f"burn_in_mismatch_{law}",
                error=True,
                rust_value=rust_value,
                python_value=oracle_value,
            )
            return None
        _BURN_IN_REMAINING[law] -= 1
        if _BURN_IN_REMAINING[law] == 0:
            _LOG.info("money_rust.burn_in_complete", law=law, samples=BURN_IN_SAMPLES)
    return rust_value


def route_round_money(rupees: Decimal, python_law: Callable[[Decimal], Decimal]) -> Decimal:
    """The routed ``round_money`` — called by :mod:`brutex.money` ONLY when
    the flag is ON. EVERY path returns the correct quantized ``Decimal``.
    """
    if money_rust_disabled() or not _eligible_rupees(rupees):
        return python_law(rupees)
    adopted = _adopt_crate_string(
        _LAW_ROUND,
        "round_money_str",
        format(rupees, "f"),
        _valid_fixed2_str,
        lambda: str(python_law(rupees)),
    )
    if adopted is None:
        return python_law(rupees)
    return Decimal(adopted)


def route_rupees_to_paisa(rupees: Decimal, python_law: Callable[[Decimal], int]) -> int:
    """The routed ``rupees_to_paisa`` — every path returns the exact paisa int."""
    if money_rust_disabled() or not _eligible_rupees(rupees):
        return python_law(rupees)
    adopted = _adopt_crate_string(
        _LAW_TO_PAISA,
        "rupees_to_paisa_str",
        format(rupees, "f"),
        _valid_int_str,
        lambda: str(python_law(rupees)),
    )
    if adopted is None:
        return python_law(rupees)
    return int(adopted)


def route_paisa_to_rupees(paisa: int, python_law: Callable[[int], Decimal]) -> Decimal:
    """The routed ``paisa_to_rupees`` — every path returns the exact 2dp Decimal."""
    if money_rust_disabled() or not _eligible_paisa(paisa):
        return python_law(paisa)
    adopted = _adopt_crate_string(
        _LAW_TO_RUPEES,
        "paisa_to_rupees_str",
        str(paisa),
        _valid_fixed2_str,
        lambda: str(python_law(paisa)),
    )
    if adopted is None:
        return python_law(paisa)
    return Decimal(adopted)


def route_format_inr(paisa: int, python_law: Callable[[int], str]) -> str:
    """The routed ``format_inr`` — every path returns the display string."""
    if money_rust_disabled() or not _eligible_paisa(paisa):
        return python_law(paisa)
    adopted = _adopt_crate_string(
        _LAW_FORMAT,
        "format_inr_str",
        str(paisa),
        _valid_inr_str,
        lambda: python_law(paisa),
    )
    if adopted is None:
        return python_law(paisa)
    return adopted


__all__ = [
    "BURN_IN_SAMPLES",
    "MONEY_RUST_ENV",
    "money_rust_disabled",
    "money_rust_enabled",
    "reset_money_rust_for_tests",
    "route_format_inr",
    "route_paisa_to_rupees",
    "route_round_money",
    "route_rupees_to_paisa",
]
