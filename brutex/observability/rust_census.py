"""Runtime census of which Rust facades are ACTUALLY engaged.

DEC-RUST-ENGAGEMENT-CENSUS-001.

THE PROBLEM THIS SOLVES. Every Rust facade in this repo follows the same
shape: an env flag, a lazily-imported crate, and a Python oracle to fall
back on. When the crate is missing or stale the facade DEGRADES — it warns
**once per process** and then runs Python silently for the rest of the run.

That once-per-process latch is correct behaviour (a per-call warn would
drown the logs) but it has a consequence nobody can argue away: **absence
of a degrade message in a log proves nothing**. A log capture that starts
after the latch fired looks identical to a log from a fully-Rust run. A
census over 328,717 production log lines could therefore only *prove*
engagement for 2 of 34 facades; the other 32 were INDETERMINATE — not
broken, just unknowable from that evidence.

"Is it really running Rust?" is not answerable by reading logs, and it is
not answerable by reading source (the source says what SHOULD happen). It
is only answerable by ASKING THE PROCESS. That is what this module does.

WHAT IT REPORTS. For each discovered facade, three independently-checked
facts and the verdict they force:

===========  ==========  =============  ====================================
flag         crate       verdict        meaning
===========  ==========  =============  ====================================
ON           present     ENGAGED        Rust runs.
ON           MISSING     **DEGRADED**   Python runs. The dangerous cell —
                                        it is the one that looks fine.
OFF          any         OFF            Python runs, by explicit choice.
n/a          required    REQUIRED       No oracle exists; a missing crate
                                        is a loud typed refusal, not a
                                        silent fallback.
===========  ==========  =============  ====================================

DISCOVERY IS STRUCTURAL, NOT A LIST. A hardcoded facade table is a lie
waiting to happen: the next facade added would be absent from it and the
census would report a clean bill of health while ignoring it. Instead
:func:`discover_facades` walks the package for modules exposing the house
facade signature — a ``<NAME>_ENV`` string constant AND a matching
``<name>_enabled()`` predicate — so a new facade is enrolled the moment it
is written, and one that DROPS the signature is reported as a shape
change rather than silently vanishing.

...WITH A CANDIDATE PRE-FILTER, stated here because leaving it unstated is
how this module under-reported for as long as it did. "Structural" once
described only the signature check while a NAME test quietly ran first and
narrowed the field to modules with ``rust`` or ``seam`` in their path. Two
real facades — ``ranking/cvar_gate`` and ``data/greeks_enrich`` — own
genuine flags under names carrying neither word, so the census reported 37
where this filter now admits 39, and reported it with no gap of any kind
visible in the output. A filter that silently shrinks a census is the same
failure class as the degrade latch above: **the narrowed answer and the
complete answer are indistinguishable from the outside.** The filter now
admits a module on the name test OR on a source-read ``*RUST*_ENV``
declaration (:func:`_declares_rust_env`), and :func:`discover_facades`
documents both halves as contract. DEC-RUST-MANDATORY-POSTURE-001 §D9.1.

Read "39" as *what this filter admits*, never as *how many facades exist* —
the two are not the same number and saying so plainly is the whole point of
the paragraph above. Three modules are known to carry the REQUIRED shape and
to be invisible to BOTH halves of the union, because their names carry
neither ``rust`` nor ``seam`` and their env constants carry no ``RUST``
token: ``data/window_ledger_store`` (``LEDGER_RENDER_ENV`` +
``LedgerRenderFlagRemovedError``), ``engine/combo_mask``
(``WideKernelUnavailableError``) and ``feed/replay`` + ``feed/lookahead``
(both call ``require_feed_kernel``). Enrolling them is
DEC-RUST-MANDATORY-POSTURE-001 §D9.3, and until it lands the honest reading
of the exact-set pin is "these 39 are still here", not "these 39 are all".

WHAT THIS DOES NOT DO (stated so the guarantee is not oversold).

* It reports what the facade WOULD do on its next call, from the flag and
  the crate's importability. It does not intercept an actual call, so a
  facade whose burn-in has already AUTOREVERTED (a bit-compare mismatch
  latched it onto the oracle for this process) still reports ENGAGED here.
  That is a genuine blind spot, and it is narrow: an autorevert emits a
  loud ERROR that is *not* rate-limited, so unlike a silent degrade it
  cannot pass unnoticed.
* It says nothing about COMPLEXITY. A facade can be fully ENGAGED and
  still be O(n) — see DEC-LAKE-SCAN-CHILD-DIRS-O1-001 (#2261), where a
  Rust function on the hot path was measured at 94.46x growth. Engagement
  and complexity are independent properties with independent gates; this
  module is the engagement half only.
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import inspect
import pkgutil
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from types import ModuleType
from typing import Final

#: Verdict tokens. Stable strings — surfaced in reports and asserted by
#: tests, so treat them as a wire vocabulary rather than prose.
ENGAGED: Final[str] = "ENGAGED"
DEGRADED: Final[str] = "DEGRADED"
OFF: Final[str] = "OFF"
REQUIRED: Final[str] = "REQUIRED"
UNKNOWN: Final[str] = "UNKNOWN"

#: The predicate half of the house facade signature. A module qualifies when
#: it exports BOTH an env flag and a zero-argument ``<name>_enabled()``
#: predicate. Both halves are required: a flag alone appears in consumers
#: that merely reference a neighbour's kill switch, and the predicate alone
#: appears in wheel-REQUIRED seams that carry no flag at all (handled
#: separately via :data:`_REQUIRED_SEAM_MARKERS`).
#:
#: The flag half is matched by VALUE (:data:`_BRUTEX_ENV_VALUE_RE`), not by
#: a ``_ENV`` name suffix — two live seams spell their constant ``ENV_FLAG``
#: and ``ENV_RUST_FRONT``, so a name-suffix test was measurably blind to
#: them. See that constant for the full reasoning.
_ENABLED_SUFFIX: Final[str] = "_enabled"

#: Source markers identifying a wheel-REQUIRED seam — one whose Python
#: oracle was DELETED, so a missing crate raises rather than degrades.
#: These are the gold standard and are reported separately so they are
#: never miscounted as "flag OFF".
#: A NAMED-CLASS rule rather than a per-facade allowlist. The house
#: retired-axis law gives every wheel-required seam a refusal class whose
#: name says so — ``…UnavailableError`` when the wheel is absent and
#: ``…FlagRemovedError`` when a retired flag is set. Matching the SHAPE
#: means a facade retired tomorrow is classified correctly without editing
#: this file; the earlier literal list silently mis-reported
#: ``pull_state_rust`` as ``UNKNOWN``.
#:
#: Deliberately NARROW. A first cut also matched ``…KernelError`` and
#: ``Stale…WheelError``, which promoted two CONDITIONAL seams to REQUIRED:
#: ``rust_trade_sim`` refuses only on the >=64-bit wide-mask path and
#: degrades on u64, and ``exec_rust`` refuses only on a STALE wheel while an
#: ABSENT one degrades. Both would have inflated the count of seams whose
#: completion proves Rust ran. REQUIRED here means unconditionally required.
_REQUIRED_CLASS_RE: Final[re.Pattern[str]] = re.compile(r"\b\w*(?:Unavailable|FlagRemoved)Error\b")

_REQUIRED_SEAM_MARKERS: Final[tuple[str, ...]] = (
    "require_feed_kernel",
    "_require_crate",
    "require_materialize_flags",
)

#: Positive marker for a facade whose MODULE NAME carries no ``rust``/``seam``
#: hint (DEC-RUST-MANDATORY-POSTURE-001 §D9.1). Matches a module-level
#: constant whose OWN NAME contains ``RUST`` and ends in ``_ENV`` — e.g.
#: ``CVAR_RUST_ENV`` in ``ranking/cvar_gate.py`` and ``GREEKS_RUST_BATCH_ENV``
#: in ``data/greeks_enrich.py``, the two facades the name pre-filter alone
#: could never see.
#:
#: The discriminator was CHOSEN BY MEASUREMENT, not by taste. Deleting the
#: name pre-filter outright was the obvious fix and is the wrong one: it
#: admits 66 further modules owning some ``*_ENV`` and some ``*_enabled()``
#: (``market_hours``, ``sweep_apriori``, ``loop_watchdog`` …) or tripping the
#: ``_is_required_seam`` substring scan, taking the census from 39 rows to
#: **105** — see :func:`discover_facades` for the full measurement. Requiring
#: ``RUST`` inside the constant's own name admits **exactly** the two above
#: across all 598 walked modules and nothing else.
#:
#: Anchored at column 0 so a mention inside a comment or a function body
#: cannot enrol a module that merely *references* a neighbour's flag — the
#: same both-halves discipline as :data:`_ENABLED_SUFFIX`. The anchor does NOT
#: exclude a DOCSTRING whose example sits at column 0; that shape matches,
#: and an ``ast`` cross-check of the whole package confirms zero instances
#: today. Cost if one ever appears: one wasted source read and a candidate
#: the signature check then rejects.
#:
#: ``RUST`` must be a whole underscore-delimited TOKEN, not a substring.
#: A first cut used ``[A-Z0-9_]*RUST[A-Z0-9_]*`` and matched
#: ``MY_RUSTY_ENV`` — RUST inside another word — while simultaneously
#: REJECTING a bare ``RUST_ENV`` because it demanded a character before the
#: token. Both were caught by the unit matrix rather than by review, which
#: is why the matrix enumerates the near-misses explicitly.
_RUST_ENV_MARKER_RE: Final[re.Pattern[str]] = re.compile(
    r"^_?(?:[A-Z0-9]+_)*RUST(?:_[A-Z0-9]+)*_ENV\s*[:=]",
    re.MULTILINE,
)

#: A facade's env flag recognised by its VALUE, not by its constant's name.
#: The house convention names the constant ``<NAME>_ENV``, but two live
#: seams do not follow it — ``brutex/exec_rust.py`` spells it ``ENV_FLAG``
#: and ``brutex/dashboard/rust_front.py`` spells it ``ENV_RUST_FRONT``. Both
#: hold a real ``BRUTEX_*`` kill switch; both were invisible to a name-only
#: test, so a degrade from either went unwatched while the census read clean.
#:
#: Matching the VALUE is the narrower test, not the looser one: every env var
#: this repo owns is namespaced ``BRUTEX_``, and the discovery gate still
#: demands the ``*_enabled()`` half alongside it, so a module that merely
#: mentions a neighbour's flag name in a string never qualifies.
_BRUTEX_ENV_VALUE_RE: Final[re.Pattern[str]] = re.compile(r"BRUTEX_[A-Z0-9_]+")

#: The attribute a facade uses to DISOWN a switch the two heuristics above
#: would otherwise read as its Rust flag.
#:
#: Both heuristics are shape tests: "a ``BRUTEX_*`` string constant" and "a
#: zero-arg ``*_enabled`` callable". They are right about every seam whose
#: only switch IS the Rust kill switch, and they cannot be right about a
#: module that owns TWO unrelated switches — because nothing in the shape
#: distinguishes a Rust kill switch from a business gate that happens to be
#: spelled the same way.
#:
#: ``engine/alpha_metric`` is the live case and the reason this exists. It
#: owns ``BRUTEX_ALPHA_GATE`` / ``resolve_alpha_gate_enabled`` — the
#: DEFAULT-OFF operator RANKING gate of DEC-ALPHA-BUYHOLD-METRIC-001, which
#: decides whether alpha<=0 candidates are EXCLUDED from the top-100 build.
#: It does not gate the Rust arm: ``_kernel()`` is resolved and the
#: ``fno_math.alpha_vs_buyhold_pct`` kernel attempted on every call whatever
#: that gate says. Read as a Rust flag it reported the facade ``OFF`` —
#: "Python oracle runs by choice" — of a seam that runs Rust unconditionally.
#:
#: Why a DECLARATION rather than another shape rule: there is none available.
#: The distinction is semantic (what the switch governs), and every syntactic
#: proxy was measured and rejected — a ``RUST``-in-the-name rule breaks
#: ``meta/meta_rust`` (``BRUTEX_META_GATE`` genuinely IS its Rust flag), and a
#: "predicate is called inside its own module" rule breaks 24 facades whose
#: caller legitimately lives elsewhere (``lake_rust_render_enabled`` is
#: consulted by ``parquet_writer``).
#:
#: The blast radius is exactly the module that declares it, and the failure
#: mode is bounded: a facade that disowns EVERY switch it has falls to
#: :func:`_classify`'s flag-free branch, which still reports ``DEGRADED``
#: when the crate is absent. So the worst a wrong declaration can do is
#: under-report an operator's deliberate OFF — it can never hide a missing
#: wheel. A test pins the census-visible effect of the one live declaration.
_NON_RUST_FLAGS_ATTR: Final[str] = "RUST_CENSUS_NON_RUST_FLAGS"


def _non_rust_flags(module: ModuleType) -> frozenset[str]:
    """Return the switch names *module* declares as NOT gating its Rust arm.

    Holds env VALUES (``"BRUTEX_ALPHA_GATE"``) and/or predicate ATTRIBUTE
    names (``"resolve_alpha_gate_enabled"``) in one set — the census filters
    both loops through it. A malformed declaration (not an iterable of
    strings) is ignored rather than raised: a diagnostic must not crash on a
    facade's typo, and ignoring it fails toward the pre-declaration reading.
    """
    declared = getattr(module, _NON_RUST_FLAGS_ATTR, None)
    if not isinstance(declared, (frozenset, set, tuple, list)):
        return frozenset()
    return frozenset(item for item in declared if isinstance(item, str))


#: The fall-back ANNOUNCEMENT — the third discovery signal, and the one that
#: makes the census independent of how a facade spells its choke point.
#:
#: It exists for a specific hole. ``engine/rust_trade_sim`` runs per-mask on
#: the sweep hot path, carries NO env flag at all, and raises no refusal
#: class — so neither the signature test nor the required-seam test can see
#: it. It was in the census only by accident, via a false REQUIRED promotion
#: that the ``ast`` rewrite of :func:`_is_required_seam` correctly removed.
#: Without a third signal, fixing one hole opened a larger one.
#:
#: TWO shapes, because the repo genuinely has two and matching only the first
#: is what left ``rust_trade_sim`` invisible:
#:
#: * a named helper CALL — ``warn_fallback_once`` (76 sites),
#:   ``warn_fallback`` (9), ``_log_fallback`` (8), ``note_degrade`` (5),
#:   ``warn_stored_stale_fallback`` (3);
#: * an INLINE ``structlog`` event whose NAME ends ``.fallback`` /
#:   ``.autoreverted`` / ``.degraded`` — ``rust_trade_sim`` emits
#:   ``"rust_trade_sim.fallback"`` directly, with no helper in between.
#:
#: The event-name half is the more principled of the two: it matches the
#: ANNOUNCEMENT rather than one particular helper's identifier, so a facade
#: that invents a new helper tomorrow is still discovered as long as it obeys
#: the house event-naming law. It is the exact mirror of
#: :data:`_ENGAGED_LITERAL_RE`, which scans for the ``.engaged`` counterpart.
#:
#: Requiring a call paren / a quoted literal (never the bare identifier) means
#: an ``import`` of a helper, or prose naming an event in a docstring, does
#: not enrol a module that never actually degrades.
_DEGRADE_MARKER_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:warn_stored_stale_fallback|warn_fallback_once|warn_fallback"
    r"|_log_fallback|note_degrade)\s*\("
    r"""|["'][a-z_0-9.]+\.(?:fallback|autoreverted|degraded)["']"""
)

#: The STRICT-ENROLMENT marker — a fourth discovery signal, and the only one
#: that reads the enforcement rather than a symptom of it.
#:
#: The three signals above all infer enrolment from something adjacent: an env
#: var's value, a ``require_*`` raise, an announcement string. Each is a proxy,
#: and each proxy has a shape it cannot see. ``engine/combo_mask`` was the
#: worst case of that: it carries no env flag, its ``<=64-bit`` stale-wheel arm
#: raised nothing and announced nothing — no helper call, no ``.fallback``
#: event — and it sits on the production sweep hot path, because at today's
#: 60-bit vocabulary EVERY batch is ``fits_u64``. All three proxies read clean
#: on a module that could silently move an entire grid sweep onto numpy.
#:
#: A ``refuse_if_strict`` / ``refuse`` call is not a proxy. It IS the thing the
#: census exists to confirm: this module has been through
#: DEC-RUST-STRICT-NO-FALLBACK-001 and its choke point refuses. A module that
#: calls it is enrolled by construction, whatever else it does or does not
#: spell. That makes this the signal least able to go stale — a facade cannot
#: acquire the enforcement without acquiring the marker in the same edit.
#:
#: This is a CHEAP PRE-FILTER ONLY — never the decision. Text alone is not
#: sufficient here and the repo has already paid for that lesson once: the
#: hostile review's MAJOR-1 was a required-seam marker matched by regex, fixed
#: by walking the AST instead. The identical trap fires here — a bare
#: ``refuse(`` matches the ordinary English in ``config/backup_env``'s line
#: 449, ``"run/restore-drill will refuse (EXIT_CONFIG)"``, and enrolled a
#: module with no crate and no strict wiring as a phantom ``UNKNOWN`` row.
#: :func:`_calls_strict_refusal` re-checks every hit against the parse tree, so
#: prose, comments and imports are rejected and only a real CALL enrols.
_STRICT_MARKER_RE: Final[re.Pattern[str]] = re.compile(r"\brefuse(?:_if_strict)?\s*\(")

#: The two names a strict-enrolled choke point calls.
_STRICT_CALL_NAMES: Final[frozenset[str]] = frozenset({"refuse", "refuse_if_strict"})


def _calls_strict_refusal(source: str) -> bool:
    """Whether *source* really CALLS the strict-refusal gate (AST, not text).

    :data:`_STRICT_MARKER_RE` is the cheap pre-filter; this is the decision.
    Only a ``Call`` node counts, and only when the callee resolves to
    ``refuse`` / ``refuse_if_strict`` either bare (``refuse_if_strict(...)``)
    or attributed (``rust_strict.refuse(...)``).

    Prose is therefore rejected — the sentence "run/restore-drill will refuse
    (EXIT_CONFIG)" is a string constant, not a call — and so is a bare
    ``import``. Unparseable source returns ``False`` rather than raising: a
    module the census cannot parse must not blind it to the other 598.
    """
    if _STRICT_MARKER_RE.search(source) is None:
        return False
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        return False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id in _STRICT_CALL_NAMES:
            return True
        if isinstance(func, ast.Attribute) and func.attr in _STRICT_CALL_NAMES:
            return True
    return False


#: Package root scanned for facades.
_PACKAGE: Final[str] = "brutex"


def _source_of(module: ModuleType) -> str:
    """Return a module's source, or ``""`` when it cannot be read.

    A compiled or zip-imported module has no readable source. That costs
    the source-derived facts (crate name, required-seam marker) but must
    never abort the census, so the caller sees an empty string and the row
    falls through to the honest ``UNKNOWN``.
    """
    try:
        return inspect.getsource(module)
    except (OSError, TypeError):
        return ""


def _source_unreadable(module: ModuleType) -> bool:
    """Report whether a module's source genuinely CANNOT be read.

    Deliberately NOT spelled ``_source_of(module) == ""``. An empty but
    perfectly readable file — a bare ``__init__.py`` — also yields ``""``,
    and the two facts must not be conflated: one is "this module says
    nothing", the other is "this census cannot tell what it says". Only the
    second may keep a module in the audit as ``UNKNOWN``; the first is a
    genuine negative. Measured on this tree: ZERO pre-filtered modules hit
    either case, so the distinction costs nothing today and exists so the
    behaviour stays correct if a compiled or zip-imported module ever
    appears.
    """
    try:
        inspect.getsource(module)
    except (OSError, TypeError):
        return True
    return False


def _import_or_none(name: str) -> ModuleType | None:
    """Import ``name``, or return ``None`` when it cannot be imported.

    The census walks EVERY module under the package, including optional
    ones whose third-party dependency may be absent on this box. A module
    that refuses to import is a fact about that module, never a reason to
    abort the audit and blind every OTHER facade — so the failure is
    swallowed here and the caller simply skips the row.

    Written as a returns-``None`` helper rather than an inline
    ``try/except/continue`` deliberately: that shape is bandit ``B112``,
    and suppressing it with a pragma would hide the pattern instead of
    removing it. The broad ``except`` is load-bearing — an arbitrary
    module's import can raise literally anything at module scope — but
    ``KeyboardInterrupt``/``SystemExit`` are ``BaseException`` and so still
    propagate, which is the behaviour we want.
    """
    try:
        return importlib.import_module(name)
    except Exception:
        return None


def _call_flag_or_none(fn: Callable[[], object]) -> bool | None:
    """Call a zero-arg ``*_enabled`` predicate, or ``None`` if it will not.

    A predicate can legitimately refuse: ``pull_state_rust`` exposes an
    ARG-TAKING ``…_enabled(raw)``, and a fail-loud facade raises on a junk
    env value rather than guessing. Either way the honest answer for that
    facade is "could not resolve", never an assumed ``True`` — so the
    caller drops the sample and the row falls back to the wheel-required
    marker for its verdict.

    Same rationale as :func:`_import_or_none` for the shape: a
    returns-``None`` helper instead of an inline ``try/except/continue``
    (bandit ``B112``), and a broad ``except`` because a facade predicate
    may raise anything while ``BaseException`` still propagates.
    """
    try:
        return bool(fn())
    except Exception:
        return None


def _declares_rust_env(name: str) -> bool:
    """Report whether a module DECLARES a ``*RUST*_ENV`` constant.

    Reads the module's source file and **never imports the module itself**.
    Stated exactly, because the looser "without importing" is false as a
    standalone claim: ``find_spec`` imports the module's PARENT packages, so
    a cold call on ``brutex.ranking.cvar_gate`` walks ``brutex.ranking``'s
    ``__init__``, which in this repo re-exports the child — measured at 172
    modules from a fresh process. Inside :func:`discover_facades` that cost
    is already paid (``walk_packages`` imported every parent before the
    first call), so the guarantee that matters there is the one this
    sentence makes: the ~550 rejected modules are never themselves
    executed. Importing THEM would pull polars, duckdb and the rest of the
    transitive graph to answer a question a file read answers. A read is a
    stat plus a few KB; an import runs arbitrary module-level code.

    MEASURED COST, so the trade is a number rather than a hunch: the sweep
    is 552 modules in ~120 ms (~218 us each, dominated by ``find_spec``'s
    finder lookup) against a ~1.2 s total discovery — the other ~1.1 s is
    importing the 46 name-matched modules, which pull polars and duckdb.
    So the honest reading is that this bought two invisible facades for
    ~10% of a diagnostic whose only callers today are its own tests and
    :mod:`tests.unit.test_rust_sentinel` — it is NOT wired into ``brutex
    doctor`` or any CLI command, and it never runs on a sweep hot path.
    (Wiring it into a preflight is deliberate future work; until then this
    docstring must not claim an operator surface that does not exist.)

    Every failure mode returns ``False`` rather than raising — a module
    with no locatable source, a namespace package, an unreadable file. A
    census that cannot read one file must still audit the other 598. The
    two exceptions are the house-wide ones: ``KeyboardInterrupt`` and
    ``SystemExit`` propagate, because swallowing an operator's Ctrl-C to
    protect a diagnostic is the wrong trade.
    """
    try:
        spec = importlib.util.find_spec(name)
    except Exception:
        # ``find_spec`` imports PARENT packages, which ``walk_packages`` has
        # already imported, so this is cheap. It still runs their
        # module-level code, which can raise ANYTHING — the narrow
        # ``(ImportError, AttributeError, ValueError)`` this once caught let
        # a parent raising ``RuntimeError`` blind the whole census, which is
        # the exact outcome the sentence above promises it prevents.
        return False
    if spec is None or spec.origin is None or not spec.origin.endswith(".py"):
        return False
    try:
        source = Path(spec.origin).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    # The strict-enrolment marker admits a module the env test alone rejects.
    # ``engine/combo_mask`` is the live case and the reason this arm exists:
    # its tail carries neither "rust" nor "seam", it declares no ``BRUTEX_*``
    # env constant, and it is a hot-path facade whose stale-wheel arm falls to
    # numpy — so the CHEAP NAME PRE-FILTER dropped it before any of
    # :func:`discover_facades`'s four signals could be asked. That is the same
    # failure shape as the pre-filter bug this function was written to fix; a
    # signal is worthless if a name test rejects the module first.
    #
    # It costs nothing: the source is already read and in hand at this point,
    # so this is one extra regex over a string the function had to produce
    # anyway. No additional stat, no additional import.
    return _RUST_ENV_MARKER_RE.search(source) is not None or _calls_strict_refusal(source)


def _raises_required_class(source: str) -> bool:
    """Whether *source* RAISES a retired-axis refusal class (AST, not text).

    A text scan cannot tell a ``raise FooUnavailableError(...)`` from a
    docstring that merely *names* it — and that is not hypothetical: the
    earlier ``re.search`` over raw source promoted
    :mod:`brutex.engine.rust_trade_sim` to ``REQUIRED`` purely because three
    of its docstrings mention ``HolidayDataUnavailableError``. That module
    raises no refusal class at all and does carry degrade tokens, so the
    census reported the one hot-path seam that CAN silently serve Python as
    the gold-standard "cannot degrade" tier — a false guarantee in exactly
    the direction this census exists to catch.

    Walking ``ast.Raise`` nodes fixes it structurally: string content is not
    an expression node, so no amount of docstring prose can match. Both the
    bare (``raise Foo``) and called (``raise Foo(...)``) forms are handled;
    a dotted ``raise mod.FooUnavailableError`` matches on its attribute name.

    A syntactically-invalid module returns ``False`` — an unparseable facade
    is never promoted to the tier whose whole meaning is "completion proves
    Rust ran".
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Raise) or node.exc is None:
            continue
        exc = node.exc
        if isinstance(exc, ast.Call):
            exc = exc.func
        name = (
            exc.id
            if isinstance(exc, ast.Name)
            else exc.attr
            if isinstance(exc, ast.Attribute)
            else None
        )
        if name is not None and _REQUIRED_CLASS_RE.fullmatch(name) is not None:
            return True
    return False


def _is_required_seam(source: str) -> bool:
    """Report whether a facade REFUSES rather than degrades on a bad wheel.

    Two independent signals, either sufficient: an explicit ``require_*``
    helper call, or a refusal class actually RAISED (see
    :func:`_raises_required_class` — an AST check, deliberately not a text
    scan). The distinction is load-bearing — a required seam that cannot find
    its crate raises, so "the run completed" is itself proof the Rust path
    executed. A degrading seam offers no such proof, which is exactly the gap
    this census exists to surface, so a false promotion here is worse than no
    census at all.
    """
    if any(marker in source for marker in _REQUIRED_SEAM_MARKERS):
        return True
    return _raises_required_class(source)


def _crate_names() -> frozenset[str]:
    """Every crate this repo builds, read from ``rust/*/Cargo.toml``.

    Derived rather than hardcoded so a new crate is recognised without a
    census edit — the same anti-list discipline as facade discovery.
    Falls back to an empty set when the Rust tree is absent (an installed
    wheel-only deployment), which degrades the census to flag-only facts
    rather than crashing.
    """
    root = Path(__file__).resolve().parents[2] / "rust"
    if not root.is_dir():
        return frozenset()
    names: set[str] = set()
    for manifest in sorted(root.glob("*/Cargo.toml")):
        try:
            text = manifest.read_text(encoding="utf-8")
        except OSError:
            continue
        lib = re.search(r"^\[lib\][^\[]*?name\s*=\s*\"([^\"]+)\"", text, re.S | re.M)
        if lib is not None:
            names.add(lib.group(1))
            continue
        pkg = re.search(r"^\[package\][^\[]*?name\s*=\s*\"([^\"]+)\"", text, re.S | re.M)
        if pkg is not None:
            names.add(pkg.group(1).replace("-", "_"))
    return frozenset(names)


@dataclass(frozen=True, slots=True)
class FacadeStatus:
    """One facade's independently-checked engagement facts."""

    module: str
    env_var: str | None
    flag_on: bool | None
    crate: str | None
    crate_present: bool | None
    verdict: str
    detail: str = ""

    @property
    def is_rust_running(self) -> bool:
        """True only when Rust genuinely executes on the next call."""
        return self.verdict in (ENGAGED, REQUIRED)


# ──────────────────────────────────────────────────────────────────────
# Call-level proof — DEC-RUST-ENGAGEMENT-CENSUS-002
# ──────────────────────────────────────────────────────────────────────
#
# The flag-level verdicts above answer "is Rust configured to run, and does
# its crate import?". That is strictly weaker than "did Rust answer", and
# the gap is not academic: every facade's remaining guards fire at CALL
# time, inside the work function, not at flag resolution. The sharpest
# example is the lake family's polars lockstep guard, which lives inside
# ``render_parquet_bytes`` (brutex/data/lake_rust_render.py): it reads the
# crate's pinned polars version, compares it against the installed wheel's,
# and on any difference disables the facade with ``polars_version_mismatch``
# and returns the Python render.
#
# So a wheel/crate version drift (a one-line dependency bump moving
# ``polars==`` without moving the crate rev) silently routes the WHOLE
# ``BRUTEX_LAKE_RUST_*`` family back to the Python oracle while every
# flag-level verdict still reads ENGAGED. Measured, not reasoned: patching
# ``polars.__version__`` before import leaves :func:`census` reporting
# ``ENGAGED=32 DEGRADED=0``.
#
# The fix is to stop inferring and start observing. Each facade emits
# exactly one ``<name>.engaged`` structlog event the first time its crate
# genuinely answers, and a degrade/disable event otherwise. Those events
# are per-process warn-once, which is precisely why they are useless for
# reading a long-running log — and precisely why they are reliable when
# captured from a COLD start around a known workload: at that point every
# first decision is still unlatched, so the event stream is a complete
# record of who answered.
#
# :func:`engaged_events` captures that stream. A facade whose event fires
# is PROVEN — Rust ran, measured on this machine, this build, this wheel
# set. A facade that stays silent is CONFIGURED: the flags are right and
# the crate imports, but nothing in the workload called it, so this run
# says nothing either way about it. Keeping those two apart is the whole
# point; collapsing them is how "Rust is on" becomes a claim nobody
# checked.

PROVEN: Final[str] = "PROVEN"
CONFIGURED: Final[str] = "CONFIGURED"

#: The uniform per-facade first-answer event suffix.
_ENGAGED_EVENT_SUFFIX: Final[str] = ".engaged"

#: Literal ``"<name>.engaged"`` occurrences in a facade's own source.
_ENGAGED_LITERAL_RE: Final[re.Pattern[str]] = re.compile(r"""["']([a-z_0-9]+)\.engaged["']""")


def _accepted_events(module_name: str, source: str) -> frozenset[str]:
    """Event names that count as proof for this facade, DERIVED from source.

    The obvious join -- "the module's leaf name is the event name" -- is
    WRONG for a third of the census, and measurably so: 10 of 32 ENGAGED
    facades emit under a different name (``hardware_envelope_rust`` emits
    ``hwenv_rust.engaged``, ``compute_rust`` emits ``metrics_rust_fold``,
    ``rust_enum`` emits ``sweep_rust_enum``, and so on). Joining on the leaf
    would have reported CONFIGURED for facades that genuinely answered.

    Two rules, in order, each derived rather than listed:

    * The literals in the facade's OWN source. This is exact and covers
      every direct emitter.
    * Failing that, the leaf name. This is not a guess: the config trio
      (``paths_rust`` / ``dotenv_rust`` / ``backup_env_rust``) holds no
      literal because ``ConfigRustSeam`` renders ``f"{seam}_rust.engaged"``
      on their behalf -- which evaluates to exactly the leaf.

    TWO facades stay structurally unprovable by this mechanism, named here
    rather than papered over, and BOTH fail in the conservative direction
    (they read CONFIGURED when Rust may in fact have answered -- never a
    false PROVEN):

    * ``ranking.rust_ranking`` delegates to ``ParityFamily``, whose event is
      the SHARED ``rust_parity.engaged``. Accepting a shared name would let
      one family's answer prove another's, so it is deliberately refused.
    * ``meta.meta_rust`` emits no ``.engaged`` event at all; it has a
      fallback, a flag and an autorevert event, but no first-answer one.
    """
    literals = frozenset(_ENGAGED_LITERAL_RE.findall(source))
    return literals or frozenset({module_name.rsplit(".", 1)[-1]})


def engaged_events(workload: Callable[[], object]) -> frozenset[str]:
    """Run *workload* and return the ``<name>`` of every facade that answered.

    Captures structlog centrally for the duration of the call, then keeps
    the events whose name ends in ``.engaged`` — the house convention for
    "the crate genuinely answered, first time this process". The capture is
    restored even when *workload* raises, and the exception propagates: a
    workload that blew up measured nothing, and silently returning a
    half-populated set would read as "these facades are not engaged" when
    the truth is "we never got that far".
    """
    import structlog
    from structlog.testing import LogCapture

    capture = LogCapture()
    saved = structlog.get_config()
    structlog.configure(processors=[capture])
    try:
        workload()
    finally:
        structlog.configure(**saved)
    return frozenset(
        event[: -len(_ENGAGED_EVENT_SUFFIX)]
        for entry in capture.entries
        if isinstance(event := entry.get("event"), str) and event.endswith(_ENGAGED_EVENT_SUFFIX)
    )


def _crate_for_source(source: str, crates: frozenset[str]) -> str | None:
    """Identify the crate a facade imports, by scanning its source.

    Deliberately source-scanning rather than import-triggering: a facade's
    crate import is lazy by design (so the dashboard boots wheel-free) and
    forcing it here would change the very state we are measuring.

    Two shapes are recognised, because the house pattern uses both:

    * ``importlib.import_module("brutex_lake")`` -- the DOMINANT shape.  The
      lazy resolve is the whole point of the facade, so the crate name is a
      string literal, not an import statement.  Scanning only for ``import
      <crate>`` reported ``UNKNOWN`` for 29 of 37 facades on the first run.
    * ``import fno_math`` / ``from trading_core import ...`` -- the few
      module-scope hard imports (a wheel-required seam has nothing to be
      lazy about).
    """
    for crate in sorted(crates, key=len, reverse=True):
        escaped = re.escape(crate)
        if re.search(rf"""import_module\(\s*["']{escaped}["']""", source):
            return crate
        if re.search(rf"\b(?:import|from)\s+{escaped}\b", source):
            return crate
    return None


def _delegated_crate(source: str, crates: frozenset[str]) -> str | None:
    """Resolve the crate for a facade that delegates its resolve to a helper.

    Several facades hold no crate literal of their own because the lazy
    resolve lives in a SHARED seam helper -- ``config.seam_core.ConfigRustSeam``
    for the config trio, ``engine.rust_parity_core.ParityFamily`` for the
    ranking family.  Scanning only the facade's own source reported ``UNKNOWN``
    for those.

    One hop, and derived rather than hardcoded: read the source of each
    ``brutex.*`` module the facade imports directly and look for the crate
    literal there.  One hop is deliberate -- it covers the delegation shape
    the codebase actually uses, and refuses to chase a transitive graph whose
    answer would no longer be "the crate THIS facade routes to".
    """
    for match in re.finditer(r"^from\s+(brutex\.[\w.]+)\s+import\b", source, re.MULTILINE):
        try:
            helper = importlib.import_module(match.group(1))
            helper_source = inspect.getsource(helper)
        except (ImportError, OSError, TypeError):
            continue
        crate = _crate_for_source(helper_source, crates)
        if crate is not None:
            return crate
    return None


def _classify(
    *,
    env_var: str | None,
    flag_on: bool | None,
    crate: str | None,
    crate_present: bool | None,
    required_seam: bool,
) -> tuple[str, str]:
    """Derive the verdict from the three checked facts."""
    if required_seam and crate is not None:
        if crate_present:
            return REQUIRED, "wheel-required seam; crate present"
        return (
            DEGRADED,
            f"wheel-REQUIRED seam but {crate} is NOT importable — this raises at call time",
        )
    if crate is None:
        return UNKNOWN, "no crate import found in source — facade shape may have changed"
    if env_var is None and flag_on is None:
        # GENUINELY FLAG-FREE: no kill switch exists, so the Rust arm is
        # unconditionally attempted and the only question is whether the crate
        # is there. ``engine/rust_trade_sim`` is the live case — per-mask on
        # the sweep hot path, no env flag, and warn-once degrade tokens
        # (``wheel_missing``, ``*_kernel_stale``) that hand the call to
        # Python. It was previously visible ONLY through a false REQUIRED
        # promotion off a docstring mention; once that was fixed it vanished
        # from the census entirely — strictly worse, an unwatched hot-path
        # seam reported as nothing at all.
        #
        # Note the AND: a facade with no flag CONSTANT but a resolvable
        # PREDICATE is not flag-free, it merely keeps the constant in a
        # sibling module, and it falls through to the ordinary flag logic
        # below. Routing it here instead reported ``lake_keys_rust`` as
        # DEGRADED whenever its shared ``BRUTEX_DATA_RUST`` was off — turning
        # a deliberate operator choice into an alarm.
        if crate_present:
            return ENGAGED, f"no flag (unconditionally attempted) and {crate} importable"
        return (
            DEGRADED,
            f"no flag (unconditionally attempted) but {crate} is NOT importable — "
            "Python runs SILENTLY",
        )
    if flag_on is None:
        return UNKNOWN, "flag state could not be resolved"
    # ``env_var`` may still be None here — the flag resolved through a
    # predicate whose constant lives elsewhere (``data/lake_keys_rust`` shares
    # ``BRUTEX_DATA_RUST``; ``engine/rust_parity_core`` calls an imported
    # ``env_flag_enabled``). Name the shape rather than claiming "no flag",
    # which would invite someone to "fix" a kill switch that already exists.
    label = env_var if env_var is not None else "the flag (held in a sibling module)"
    if not flag_on:
        return OFF, f"{label} resolves falsy — Python oracle runs by choice"
    if crate_present:
        return ENGAGED, f"{label} on and {crate} importable"
    return DEGRADED, f"{label} is ON but {crate} is NOT importable — Python runs SILENTLY"


def discover_facades() -> list[str]:
    """Return every module exposing the house Rust-facade signature.

    Structural discovery on FOUR independent signals, any one sufficient:

    1. **flag + predicate** — the module exposes a ``BRUTEX_*`` env-var
       VALUE (:data:`_BRUTEX_ENV_VALUE_RE`) *and* a ``<name>_enabled()``
       callable. Both halves are required: the constant alone appears in
       consumers that merely reference a neighbour's flag, the predicate
       alone in wheel-REQUIRED seams that carry no flag.
    2. **wheel-REQUIRED marker** — a ``require_*`` helper or a refusal class
       actually RAISED (:func:`_is_required_seam`).
    3. **flag-free degrading seam** — it imports a crate *and* announces its
       fall-backs (:data:`_DEGRADE_MARKER_RE`).
    4. **strict-enrolment marker** — it imports a crate *and* calls
       ``refuse_if_strict`` / ``refuse`` (:data:`_STRICT_MARKER_RE`).

    Signal 3 is not decoration. ``engine/rust_trade_sim`` runs per-mask on
    the sweep hot path and satisfies neither of the first two — no flag, no
    refusal class. It appeared in the census only through a false REQUIRED
    promotion, and once that bug was fixed it vanished entirely. A seam that
    can silently serve Python on the hottest path in the product is exactly
    the one the census must not lose.

    Signal 4 exists because signals 1-3 are all PROXIES — an env value, a
    raise, an announcement — and ``engine/combo_mask`` defeated all three at
    once: no flag, no refusal class, and a ``<=64-bit`` stale-wheel arm that
    fell to numpy in silence. At the shipped 60-bit vocabulary every batch
    takes that arm, so the census read clean on the one module that could
    quietly demote an entire grid sweep. Signal 4 does not infer enrolment
    from a symptom; it reads the enforcement itself, so it cannot be defeated
    by a facade that simply declines to speak.

    DISCLOSED EXCLUSION: ``dashboard/rust_front`` owns two real ``BRUTEX_*``
    flags but is a SUBPROCESS seam — it spawns the ``brutexd`` binary rather
    than importing a PyO3 wheel. It has no ``*_enabled()`` predicate and no
    crate, so ``importlib.util.find_spec`` — the census's whole
    "did Rust actually arrive" test — has nothing to ask about it. Admitting
    it would mean reporting ``crate=None`` for a seam that genuinely has no
    crate, which reads as a defect rather than a fact. Covering it needs a
    binary-presence probe, i.e. a feature; it is recorded in
    ADR-0026 §D9.3's invisible-seam list rather than faked here. It is off
    the sweep path (it serves ``/static/*`` and ``/health``), so this costs
    the sweep guarantee nothing.

    Import failures are skipped rather than raised so one broken optional
    module cannot blind the whole census (the skip is visible as an absent
    row, and the count pins in the tests catch a collapse).

    A CANDIDATE PRE-FILTER RUNS FIRST, and it is part of the contract
    rather than an implementation detail — stated here because omitting it
    is how the census under-reported for as long as it did. It admits a
    module on EITHER of two signals:

    * its name carries ``rust`` or ``seam`` (46 modules), or
    * its SOURCE declares a ``*RUST*_ENV`` constant, read without importing
      the module itself (:func:`_declares_rust_env`).

    The union is load-bearing in **both directions**, and both were
    measured (DEC-RUST-MANDATORY-POSTURE-001 §D9.1). The name test alone
    misses ``ranking/cvar_gate`` and ``data/greeks_enrich``, which own real
    flags under names that mention neither word. The marker alone would
    lose two real facades — ``engine/rust_trade_sim`` and ``meta/meta_rust``
    — that hold their flag elsewhere. So neither test may replace the other.

    THE FILTER IS A CORRECTNESS GATE, NOT A SPEED ONE, and calling it an
    optimisation was the second thing this docstring got wrong. Deleting it
    and running the signature test on all 598 walked modules does not
    produce the same answer more slowly — it produces a DIFFERENT and wrong
    answer: **105 rows instead of 39**. 66 extra modules qualify, and 19 of
    them would be classified ``REQUIRED``, the census's own gold standard —
    ``data/errors``, ``dashboard/app``, ``calendars/nse``,
    ``engine/trade_sim`` and more. The cause is :func:`_is_required_seam`,
    a substring scan that cannot tell a module DECLARING a
    ``…UnavailableError`` from one merely MENTIONING it. So the pre-filter
    is what makes the signature test's answer correct, and the honest
    reading of the trade is: it suppresses 66 signature-qualifying
    non-facades (37 of them via that substring weakness, 25 via the
    signature alone, 4 via both) and costs one source read per rejected
    module to do it.
    """
    package = importlib.import_module(_PACKAGE)
    found: list[str] = []
    for info in pkgutil.walk_packages(package.__path__, prefix=f"{_PACKAGE}."):
        name = info.name
        # Cheap pre-filter before paying an import. Name first because it is
        # free; the source read only runs for the ~550 modules it rejects.
        tail = name.rsplit(".", 1)[-1]
        if "rust" not in tail and "seam" not in tail and not _declares_rust_env(name):
            continue
        # The census is not a facade. It matches its own name pre-filter and
        # carries the required-seam MARKER STRINGS as data, so without this
        # it audits itself and reports a phantom REQUIRED row.
        # ``__name__`` is ``"__main__"`` under ``python -m``, so compare
        # against the SPEC name, which stays the dotted path either way.
        if __spec__ is not None and name == __spec__.name:
            continue
        module = _import_or_none(name)
        if module is None:  # a broken optional module must not blind the census
            continue
        # The env signal is VALUE-based, not name-based. A name test on
        # ``*_ENV`` misses a real facade that spells its constant otherwise:
        # ``brutex/exec_rust.py`` declares ``ENV_FLAG: Final[str] =
        # "BRUTEX_EXEC_RUST"`` and ``brutex/dashboard/rust_front.py`` uses
        # ``ENV_RUST_FRONT``. Both are live flag-gated Rust seams with real
        # kill switches, and both were dropped with NO diagnostic — so a
        # degrade from either was unwatched while the census reported clean.
        has_env = any(
            _BRUTEX_ENV_VALUE_RE.fullmatch(value)
            for attr in dir(module)
            if isinstance(value := getattr(module, attr, None), str)
        )
        has_enabled = any(
            attr.endswith(_ENABLED_SUFFIX) and callable(getattr(module, attr, None))
            for attr in dir(module)
        )
        source = _source_of(module)
        required_seam = _is_required_seam(source)
        # Third signal: a FLAG-FREE degrading seam — no kill switch, but it
        # imports a crate and announces its fall-backs. ``rust_trade_sim`` is
        # the live case (per-mask on the sweep hot path). Without this it is
        # invisible: it has no flag to find and, since the AST fix, no false
        # REQUIRED promotion to ride in on either.
        degrading_seam = (
            _DEGRADE_MARKER_RE.search(source) is not None
            and _crate_for_source(source, _crate_names()) is not None
        )
        # Fourth signal: a STRICT-ENROLLED seam — it imports a crate and its
        # choke point calls ``refuse_if_strict``. ``engine/combo_mask`` is the
        # live case: no flag, no refusal class, and a stale-wheel arm that fell
        # to numpy without a word, on the hot path, for every batch the shipped
        # 60-bit vocabulary produces. The other three signals all read a
        # symptom; this one reads the enforcement, so a silent facade cannot
        # hide from it.
        strict_seam = (
            _calls_strict_refusal(source) and _crate_for_source(source, _crate_names()) is not None
        )
        # Fifth signal: "I CANNOT TELL" IS A ROW, NEVER AN ABSENCE. Signals
        # 2-4 all read source, so a module whose source cannot be read is
        # findable by signal 1 alone — and a facade that holds no
        # ``BRUTEX_*`` value plus ``*_enabled()`` pair was simply DROPPED:
        # no row, no diagnostic, no count change to notice. That is the one
        # answer this census may never give, because an absent row is
        # indistinguishable from a clean audit, which is exactly the lie
        # the whole P1 guarantee exists to prevent.
        #
        # MEASURED, not theorised: planting a bare ``types.ModuleType`` over
        # ``brutex.config.seam_core`` in ``sys.modules`` removed it from
        # ``discover_facades()`` silently — 44 rows became 43 with nothing
        # said. Keeping the name routes it through ``census``/``_classify``,
        # which reports the honest ``UNKNOWN`` the docstring above already
        # promised for unreadable source (before this arm that promise was
        # false for signals 2-4 — they vanished the module instead).
        #
        # Inert on a source-shipping tree: ZERO of the ~48 pre-filtered
        # modules have unreadable source, so this adds no row in practice.
        # It arms only for a compiled/zip-imported module or a sourceless
        # stand-in — cases where silence would be the worst possible answer.
        if (
            (has_env and has_enabled)
            or required_seam
            or degrading_seam
            or strict_seam
            or _source_unreadable(module)
        ):
            found.append(name)
    return sorted(found)


def census(modules: list[str] | None = None) -> list[FacadeStatus]:
    """Ask every facade what it WOULD do on its next call."""
    crates = _crate_names()
    names = modules if modules is not None else discover_facades()
    rows: list[FacadeStatus] = []
    for name in names:
        try:
            module = importlib.import_module(name)
        except Exception as exc:
            rows.append(
                FacadeStatus(
                    module=name,
                    env_var=None,
                    flag_on=None,
                    crate=None,
                    crate_present=None,
                    verdict=UNKNOWN,
                    detail=f"import failed: {type(exc).__name__}",
                )
            )
            continue

        # VALUE-based, matching :func:`discover_facades`. A name-only scan for
        # ``*_ENV`` finds the flag in most facades but not in ``exec_rust``
        # (``ENV_FLAG``) or ``rust_front`` (``ENV_RUST_FRONT``) — and once
        # discovery admits those two, a ``None`` here would send them down the
        # ``_classify`` no-flag branch and report them as unconditionally
        # attempted when in fact they each hold a live kill switch. The two
        # scans must therefore agree, so they share one regex.
        #
        # ``sorted(dir(...))`` keeps the pick deterministic when a module owns
        # several flags; the resolved-predicate loop below is the part that
        # speaks for ALL of them.
        # Switches the facade DISOWNS — see :data:`_NON_RUST_FLAGS_ATTR`. The
        # same set filters both loops below, because a business gate that
        # fools the value test fools the predicate test too (they are the two
        # halves of one shape).
        disowned = _non_rust_flags(module)

        env_var: str | None = None
        for attr in sorted(dir(module)):
            value = getattr(module, attr, None)
            if not isinstance(value, str) or not _BRUTEX_ENV_VALUE_RE.fullmatch(value):
                continue
            if attr in disowned or value in disowned:
                continue
            env_var = value
            break

        # Resolve EVERY zero-arg ``*_enabled`` predicate and AND them, rather
        # than taking the first alphabetically. Three reasons, each hit in
        # practice on the first run:
        #   * ``detectors_rust`` owns THREE independent flags -- one sampled
        #     answer would speak for laws it does not govern.
        #   * ``dir()`` also surfaces IMPORTED predicates, so "first
        #     alphabetically" could resolve a neighbour's flag.
        #   * ``pull_state_rust`` exposes an ARG-TAKING ``…_enabled(raw)``;
        #     breaking on it left the whole row unresolved. Skipping it lets
        #     the wheel-required marker classify the row correctly instead.
        # A facade with no resolvable predicate stays ``None`` -- an honest
        # "could not resolve", never an assumed True.
        resolved: list[bool] = []
        for attr in sorted(dir(module)):
            if not attr.endswith(_ENABLED_SUFFIX) or attr in disowned:
                continue
            fn = getattr(module, attr, None)
            if not callable(fn):
                continue
            sample = _call_flag_or_none(fn)
            if sample is not None:  # an unresolvable flag is a fact, not a crash
                resolved.append(sample)
        flag_on: bool | None = all(resolved) if resolved else None

        source = _source_of(module)
        crate = _crate_for_source(source, crates) or _delegated_crate(source, crates)
        required_seam = _is_required_seam(source)
        crate_present: bool | None = None
        if crate is not None:
            try:
                crate_present = importlib.util.find_spec(crate) is not None
            except (ImportError, ValueError):
                crate_present = False

        verdict, detail = _classify(
            env_var=env_var,
            flag_on=flag_on,
            crate=crate,
            crate_present=crate_present,
            required_seam=required_seam,
        )
        # Say WHY, not just "unknown". Every source-derived fact above —
        # crate, required-seam — is silently absent for an unreadable
        # module, so ``_classify`` reaches its no-crate arm and reports
        # "no crate import found". On this path that sentence is a
        # misreading dressed as a finding: the crate import may well be
        # there, unread. Naming the real cause keeps the row honest and
        # tells the operator the row is uninformative rather than negative.
        if _source_unreadable(module):
            detail = "source unreadable - crate and required-seam signals unavailable"
        rows.append(
            FacadeStatus(
                module=name,
                env_var=env_var,
                flag_on=flag_on,
                crate=crate,
                crate_present=crate_present,
                verdict=verdict,
                detail=detail,
            )
        )
    return rows


def summarize(rows: list[FacadeStatus]) -> dict[str, int]:
    """Count rows per verdict."""
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.verdict] = counts.get(row.verdict, 0) + 1
    return counts


def render_table(rows: list[FacadeStatus]) -> str:
    """Human-readable census, widest-first so the verdict column lines up."""
    if not rows:
        return "NO FACADES DISCOVERED — the discovery signature has changed."
    mod_w = max(len(r.module) for r in rows)
    env_w = max(len(r.env_var or "-") for r in rows)
    crate_w = max(len(r.crate or "-") for r in rows)
    out = [
        f"{'FACADE':<{mod_w}}  {'FLAG':<{env_w}}  {'CRATE':<{crate_w}}  VERDICT",
        f"{'-' * mod_w}  {'-' * env_w}  {'-' * crate_w}  {'-' * 8}",
    ]
    for row in sorted(rows, key=lambda r: (r.verdict, r.module)):
        out.append(
            f"{row.module:<{mod_w}}  {row.env_var or '-':<{env_w}}  "
            f"{row.crate or '-':<{crate_w}}  {row.verdict}"
        )
    counts = summarize(rows)
    out.append("")
    out.append("  ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    degraded = [r for r in rows if r.verdict == DEGRADED]
    if degraded:
        out.append("")
        out.append("DEGRADED — Python is running where Rust was expected:")
        for row in degraded:
            out.append(f"  {row.module}: {row.detail}")
    return "\n".join(out)


#: Verdicts that fail ``--strict``. A facade is only allowed through when
#: it is provably going to take the Rust path: ENGAGED (flag on, crate
#: importable) or REQUIRED (no Python arm exists at all).
_STRICT_FAILING: Final[frozenset[str]] = frozenset({DEGRADED, OFF, UNKNOWN})


def strict_failures(rows: list[FacadeStatus]) -> list[FacadeStatus]:
    """Return the facades that would NOT take the Rust path.

    This is the preflight half of the guarantee. Running it immediately
    before a sweep converts "Rust is the default" -- a statement about
    configuration -- into "Rust will run" -- a statement about this
    process, on this machine, with these wheels installed.

    What it proves: every facade's flag is on and its crate imports here
    and now, so no facade can silently take a Python arm for want of a
    wheel or a flag.

    What it does NOT prove, stated plainly because the difference matters:

    * **Complexity.** A facade can be Rust and still be O(n) -- exactly
      the defect measured in ``child_dirs`` (94.46x at 100x the siblings),
      which every correctness test passed. Rust-ness and O(1) are
      independent properties; the bench tier is what gates the second.
    * **Burn-in autorevert.** A crate that imports cleanly but disagrees
      with its Python oracle is dropped mid-run. That path is not silent
      -- it logs an ERROR that is not rate-limited -- but it happens
      after this gate has already passed.
    * **Call-time guards.** Every remaining guard fires inside the work
      function, not at flag resolution -- the lake family's polars
      lockstep check most of all. Use :func:`prove` for that half;
      ENGAGED here means "configured and importable", never "answered".
    """
    return [row for row in rows if row.verdict in _STRICT_FAILING]


def prove(
    workload: Callable[[], object], rows: list[FacadeStatus] | None = None
) -> list[FacadeStatus]:
    """Re-verdict *rows* from what actually happened while *workload* ran.

    Upgrades each ENGAGED row to :data:`PROVEN` when that facade's
    ``.engaged`` event fired during the workload, and downgrades it to
    :data:`CONFIGURED` when it stayed silent. REQUIRED, DEGRADED, OFF and
    UNKNOWN rows pass through untouched -- they were already decided by
    stronger evidence than a workload trace, and a silent REQUIRED facade
    is not a finding (it has no Python arm to fall back to).

    The distinction is the honest one: PROVEN is a measurement, CONFIGURED
    is an expectation nothing exercised. A run that reports CONFIGURED has
    not found a defect -- it has found an untested seam, which is a
    different and much less alarming thing, and conflating the two would
    manufacture either false alarm or false comfort depending on which way
    you rounded.
    """
    base = census() if rows is None else rows
    answered = engaged_events(workload)
    out: list[FacadeStatus] = []
    for row in base:
        if row.verdict != ENGAGED:
            out.append(row)
            continue
        # ``sys.modules``, never a fresh import: an ENGAGED row was imported
        # by the census that produced it, and importing here would change
        # the very state being measured (the same reason the crate resolver
        # source-scans instead of import-triggering).
        module = sys.modules.get(row.module)
        source = _source_of(module) if module is not None else ""
        accepted = _accepted_events(row.module, source)
        fired = sorted(accepted & answered)
        out.append(
            replace(
                row,
                verdict=PROVEN if fired else CONFIGURED,
                detail=(
                    f"{fired[0]}.engaged observed" if fired else "flag on, crate imports, uncalled"
                ),
            )
        )
    return out


def _emit(line: str) -> None:
    """Write one census line to stdout.

    Deliberately ``sys.stdout.write`` and not ``print``. The repo bans the
    ``print`` builtin in every nested ``brutex/`` module
    (``scripts/ci/scan-banned-patterns.sh``) so that a stray debug print can
    never contaminate stdout — which matters here because the CLI contract
    documented in ``brutex/cli.py`` is that **stdout carries EXACTLY the
    table / JSON and every log line goes to stderr**. This is that contract,
    not an exception to it: the census table IS the deliverable, so it is
    written to stdout explicitly through one named seam, while every
    diagnostic in this module still goes through structlog to stderr.

    Routing through a single function also makes the CLI's output surface
    greppable — one place to audit — instead of three scattered calls.
    """
    sys.stdout.write(f"{line}\n")


def main(argv: list[str] | None = None) -> int:
    """Write the census table to stdout.

    Exit 1 if any facade is DEGRADED; under ``--strict`` also fail on OFF
    or UNKNOWN, so a clean exit means every facade will take the Rust path.
    """
    args = sys.argv[1:] if argv is None else argv
    strict = "--strict" in args
    rows = census()
    _emit(render_table(rows))

    failures = strict_failures(rows) if strict else [r for r in rows if r.verdict == DEGRADED]
    if not failures:
        return 0
    _emit("\nSTRICT FAILURES:" if strict else "\nDEGRADED:")
    for row in failures:
        _emit(f"  {row.module}  [{row.verdict}]  {row.detail}")
    return 1


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
