"""detectors_rust — the default-OFF Rust routing facade for the T24 detector laws.

DEC-DATA-RUST-INTEGRITY-DETECTORS-001 (SLICE T24 — PR-1 laws 1-3,
MIGRATION.md #1948; PR-2 adds law 4; PR-3a adds laws 5-6). Routes SIX cold
data-integrity detector/audit pure laws through the ``rust/brutex-lake``
crate behind six default-OFF flags, with the verbatim Python law retained
as the LIVE parity oracle (the ADR-0020 ``lake_rust_render`` / G8
template, in the G6 ``render_shard`` integration shape — the TARGET module
gates its own flag and hands this facade zero-arg factories, so no
verdict/report dataclass ever has to import back into this module):

1. **Raw-integrity digest** (``BRUTEX_LAKE_RUST_RAW_INTEGRITY``) — the
   ``hashlib.blake2b(canonical, digest_size=32).hexdigest()`` leg of
   :func:`brutex.data.raw_integrity.hash_payload`. The CANONICALISATION
   (``json.dumps`` — CPython's ``float.__repr__`` byte law) stays Python:
   bytes cross IN, lowercase ASCII hex crosses OUT.
2. **Interior bar-less run walker** (``BRUTEX_LAKE_RUST_INTERIOR_GAP``) —
   the grouping decision of
   :func:`brutex.data.interior_gap_detect._interior_barless_runs`. The NSE
   calendar + observed overlay stay Python (the target's payload factory
   walks the interior once and passes TRADING-session ordinals); Rust groups
   the maximal bar-less runs; this facade materializes the dates.
3. **Partial-window truncation verdict** (``BRUTEX_LAKE_RUST_PARTIAL_WINDOW``)
   — the WHOLE decision tree of
   :func:`brutex.data.partial_window_detect.classify_partial_window` over
   plain ordinals + a Python-computed per-day SESSION GRID; the target
   materializes the ``PartialWindowVerdict`` via its ``materialize``
   callback. A session walk stepping OFF the grid ABSTAINS (the
   DEC-FEED-RUST-CORE-001 envelope contract) — one WARN per process, the
   oracle serves.
4. **Pre-settle TODAY minute-completeness verdict**
   (``BRUTEX_LAKE_RUST_TODAY_COMPLETENESS``, SLICE T24 PR-2) — the
   per-class criteria decision tree of
   :func:`brutex.data.today_completeness.classify_today_completeness`
   (DEC-PULL-TODAY-COMPLETENESS-GATE-001 #1774). The three inert arms
   (not-covering / coverage abstain / non-trading) and every calendar
   consult (``get_session`` / ``is_holiday_data_available`` /
   ``session_last_bar_complete``) stay Python — the target routes only the
   count/shape criteria table over per-bar IST minute ordinals + bool
   masks; the verdict reason crosses back as an INDEX into
   :data:`_TODAY_REASONS`. LOAD-BEARING degrade discipline: the
   crossverify producer binds ``classify_today_completeness`` DIRECTLY as
   a fail-closed publish gate (``brutex/crossverify/gate_seam.py``), so a
   crate fault under the flag must ALWAYS resolve to the oracle verdict —
   never a new exception class out of the classifier (pinned).
5. **Economics lot fold** (``BRUTEX_LAKE_RUST_ECONOMICS_INTEGRITY``,
   SLICE T24 PR-3a) — the group folds + verdict derivation of
   :func:`brutex.data.economics_integrity.audit_economics_integrity`
   (DEC-INSTRUMENT-ECONOMICS-INTEGRITY-001). Ints-only in the STRONGEST
   form — no string semantics enter Rust at all: the target interns every
   ``(exchange, underlying, expiry)`` / ``(exchange, underlying)`` key to
   a dense id and pre-computes the Unicode-sensitive predicates (the
   ``*NSETEST*`` ``.upper()`` marker containment, the empty-underlying
   skip) BEFORE the per-leg vectors are built; the report-visible
   capped-sample ORDER (the full-key string sort) stays Python. The crate
   returns the flagged group/underlying ids + their sorted distinct lots.
6. **Master-integrity fold** (``BRUTEX_LAKE_RUST_MASTER_INTEGRITY``,
   SLICE T24 PR-3a) — the two-pass spot-table build + leg classification
   of :func:`brutex.data.master_integrity.audit_master_integrity`
   (DEC-INSTRUMENT-MASTER-INTEGRITY-001) over interned ids ONLY: the
   FIRST-identity/ambiguity precedence of pass 1 and the 7-way pass-2
   bucket order (ambiguous > disagreement > both > symbol_only >
   token_only > orphan). The Unicode traps (``str.isdigit()`` on
   ``feed_token``, marker containment, the special-char tally), the alias
   reversal, the Decimal-bearing dedup key and the type counts stay
   PYTHON (the dedup is a set membership on a tuple with a ``Decimal``
   member — interning it for Rust to re-count would be pure ceremony; a
   recorded design decision). Per-leg bucket CODES cross back (the
   DEC-FEED-RUST-CORE-001 indices shape) — the target tallies the counts
   it must walk anyway for the input-order orphan sample.

## The fail-closed layers (the PR-1 template, adapted)

* **Flags** — six per-module flags, each default OFF (truthy → ON, falsy →
  OFF, junk → loud-OFF once; the §11 COMMON :func:`env_flag_tristate`
  imported from the sibling facade). The flag gate lives in the TARGET
  modules (the G6 shape) so flag OFF is the literal verbatim oracle call —
  the crate is never imported (bomb-crate spy-pinned).
* **ONE shared latch** — the six laws share one process-local OFF latch
  (the PR-2 lake_rust_render precedent: either profile's trip disables
  both — DISCLOSED, and re-disclosed at each ``_CRATE_API`` growth): any
  law's crate-absent / crate-stale / panic / burn-in-mismatch latch serves
  the oracle for ALL detector laws for the remainder of the process.
  Per-law burn-in budgets stay independent.
* **Import guard / SYMBOL handshake** — polars-free laws, so the PR-1
  polars VERSION guard is meaningless here (the G8 disclosed deviation):
  an absent wheel latches ``crate_unavailable``; a wheel missing ANY of the
  six law callables is a pre-PR-3a (or foreign/broken) install and latches
  ``crate_stale`` for ALL laws (the all-or-nothing consequence, disclosed:
  a PR-1/PR-2-era wheel serves the oracle for its own laws too until
  rebuilt).
* **Degrade set** — the FIVE-member :data:`_DEGRADE_ERRORS` =
  ``ValueError``/``RuntimeError``/``TypeError``/``OverflowError``/
  ``AttributeError`` → oracle with one capped WARN per reason (the
  round-1 review wording fold — the executed truth, never the two-member
  shorthand: ``ValueError``/``RuntimeError`` are the crate's declared FFI
  error surface — NOTE: a Rust panic the crate's own ``catch_unwind``
  boundary CATCHES surfaces as ``PyRuntimeError`` and lands in THIS arm,
  a warn-once degrade with NO latch; ``TypeError``/``OverflowError`` are
  PyO3 argument conversion — an out-of-``i64`` ordinal lands here;
  ``AttributeError`` is the broken-crate-internals belt). Every
  payload-factory call (the TODAY law's + both PR-3a audit laws')
  additionally degrades on ``KeyError``
  (:data:`_PAYLOAD_DEGRADE_ERRORS` — for TODAY the target's class-code
  lookup, structurally total today over the closed ``InstrumentClass``
  enum; defense-in-depth so a poisoned payload build resolves to the
  oracle with one WARN, never a ``crate_panic`` latch via the
  ``Exception``-inclusive panic belt). ``MemoryError`` is
  DELIBERATELY absent — OOM re-raises raw, identical to the pure-Python
  path; ``KeyboardInterrupt``/``SystemExit`` likewise. The narrow panic
  belt (:func:`_panic_belt_errors` — imported from the sibling facade, ONE
  copy per §11 COMMON) latches ``crate_panic`` ONLY for a panic that
  ESCAPES ``catch_unwind`` as a ``pyo3_runtime.PanicException`` (the
  executed truth — the round-1 review wording fold).
* **Refuse-poison gates** — a wrong-shaped crate return (non-str hex,
  junk run tuples / out-of-``date``-range ordinals, malformed verdict
  fields, an unknown reason token, out-of-interned-range audit ids,
  non-ascending lot lists, a bucket-code list diverging from the leg
  count or an out-of-vocabulary bucket code) is REFUSED to the oracle
  with one WARN — a poisoned wheel can only degrade, never corrupt (the
  PR-1/PR-2 depth, mirrored — the design Q8 recording: shape + range +
  known-token sets; value drift is burn-in's territory).
* **Burn-in + autorevert** — the first :data:`BURN_IN_SAMPLES` engaged
  calls PER LAW compare the (materialized) Rust value against the live
  oracle; ANY difference ships the ORACLE value and autoreverts the Rust
  path for the process (ERROR log — the #1531/#1532 pattern).

## Honesty (ADR-0020 banner; CLAUDE.md Golden Rule #10)

Expected payoff ≈ 0 — conversion is for uniformity, not speed. All six
laws are cold pull-tail / doctor / master-load diagnostics; the FFI hop
plus the payload prep is a small SLOWDOWN, accepted because the
surrounding paths are vendor-I/O-dominated and the seams ship DEFAULT-OFF
(a default flip needs its own DEC). For the PR-3a audit laws the payload
INTERNING is itself an O(legs)/O(rows) Python pass with dicts — the same
complexity class as the fold it replaces (the honest today_completeness
disclosure reused); and each runs ~once per master load per process, so
the 16-call burn-in effectively means COMPARE-ALWAYS in production — a
permanent live differential, honest and cheap on a cold path (the PR-1
raw-integrity law has the same property; re-disclosed, never hidden).
These detectors gate REAL ledger demotions
(DEC-INTERIOR-GAP-DETECT-001 / DEC-PARTIAL-WINDOW-REPULL-001 #1031) + the
/universe integrity panels' persisted schema_meta verdicts — decision
parity, not speed, is the bar, hence the burn-in + the hostile
golden/differential corpus. Flags are read per call (one ``os.environ``
probe) so spawn workers resolve their own inherited environment.
"""

from __future__ import annotations

import importlib
import os
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Final

import structlog

# §11 COMMON — ONE flag vocabulary + ONE panic belt for the brutex-lake
# facades. `env_flag_tristate` is the sibling's public helper;
# `_panic_belt_errors` is a same-package private import (the underscore is
# module-privacy toward OUTSIDE consumers — duplicating the guardedly-
# resolved PanicException fold here would be the §11 violation; the G8
# window_ledger_rust precedent).
from brutex.data.lake_rust_render import _panic_belt_errors, env_flag_tristate

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from types import ModuleType

_LOG = structlog.get_logger(__name__)

#: The raw-integrity digest opt-in flag (default OFF; junk fails loud-OFF).
LAKE_RUST_RAW_INTEGRITY_ENV: Final[str] = "BRUTEX_LAKE_RUST_RAW_INTEGRITY"
#: The interior-gap run-walker opt-in flag.
LAKE_RUST_INTERIOR_GAP_ENV: Final[str] = "BRUTEX_LAKE_RUST_INTERIOR_GAP"
#: The partial-window verdict opt-in flag.
LAKE_RUST_PARTIAL_WINDOW_ENV: Final[str] = "BRUTEX_LAKE_RUST_PARTIAL_WINDOW"
#: The TODAY minute-completeness verdict opt-in flag (SLICE T24 PR-2).
LAKE_RUST_TODAY_COMPLETENESS_ENV: Final[str] = "BRUTEX_LAKE_RUST_TODAY_COMPLETENESS"
#: The economics lot-fold opt-in flag (SLICE T24 PR-3a).
LAKE_RUST_ECONOMICS_INTEGRITY_ENV: Final[str] = "BRUTEX_LAKE_RUST_ECONOMICS_INTEGRITY"
#: The master-integrity fold opt-in flag (SLICE T24 PR-3a).
LAKE_RUST_MASTER_INTEGRITY_ENV: Final[str] = "BRUTEX_LAKE_RUST_MASTER_INTEGRITY"

#: Engaged calls PER LAW that are bit-compared against the live Python
#: oracle before Rust values ship uncompared (the #1531/#1532 pattern).
BURN_IN_SAMPLES: Final[int] = 16

#: The digest-law burn-in budget key.
_LAW_HASH: Final[str] = "hash"
#: The interior-runs-law burn-in budget key.
_LAW_INTERIOR_RUNS: Final[str] = "interior_runs"
#: The partial-window-law burn-in budget key.
_LAW_PARTIAL_WINDOW: Final[str] = "partial_window"
#: The today-completeness-law burn-in budget key (PR-2).
_LAW_TODAY_COMPLETENESS: Final[str] = "today_completeness"
#: The economics-lot-fold-law burn-in budget key (PR-3a).
_LAW_ECONOMICS_INTEGRITY: Final[str] = "economics_integrity"
#: The master-integrity-fold-law burn-in budget key (PR-3a).
_LAW_MASTER_INTEGRITY: Final[str] = "master_integrity"

#: Process-local OFF latch — ONE latch shared by all six laws (module
#: docstring; the PR-2 shared-latch precedent, disclosed).
_DISABLED_REASON: Final[list[str | None]] = [None]

#: Per-law burn-in budgets (mutated in place; reset seam below).
_BURN_IN_REMAINING: Final[dict[str, int]] = {
    _LAW_HASH: BURN_IN_SAMPLES,
    _LAW_INTERIOR_RUNS: BURN_IN_SAMPLES,
    _LAW_PARTIAL_WINDOW: BURN_IN_SAMPLES,
    _LAW_TODAY_COMPLETENESS: BURN_IN_SAMPLES,
    _LAW_ECONOMICS_INTEGRITY: BURN_IN_SAMPLES,
    _LAW_MASTER_INTEGRITY: BURN_IN_SAMPLES,
}

#: The crate callables ALL six laws need — the all-or-nothing SYMBOL
#: handshake surface. A wheel missing any is a pre-PR-3a install
#: (``crate_stale`` — latching every law, the disclosed #1771 precedent).
_CRATE_API: Final[tuple[str, ...]] = (
    "raw_integrity_blake2b_hex",
    "interior_barless_runs",
    "classify_partial_window_verdict",
    "classify_today_completeness_verdict",
    "economics_lot_fold",
    "master_integrity_fold",
)

#: Once-per-process-per-reason log memo.
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

#: The PAYLOAD-factory-call degrade set — :data:`_DEGRADE_ERRORS` +
#: ``KeyError`` (the PR-2 round-1 review payload-arm addition, module
#: docstring; generalized to every payload-bearing law at PR-3a: the TODAY
#: law's class-code lookup is structurally total today, and the audit
#: laws' interning walks carry no lookup at all — defense-in-depth so a
#: poisoned payload build degrades warn-once to the oracle, never
#: mis-latching ``crate_panic`` through the panic belt).
_PAYLOAD_DEGRADE_ERRORS: Final[tuple[type[Exception], ...]] = (
    *_DEGRADE_ERRORS,
    KeyError,
)

#: The production digest width (`raw_integrity._DIGEST_SIZE` twin — the
#: routed call passes it explicitly so the two can never drift silently:
#: the burn-in compares against the oracle's own digest).
_RAW_INTEGRITY_DIGEST_SIZE: Final[int] = 32

#: `date.toordinal()` bounds — the refuse-poison range for ordinals a
#: crate returns (a `date.fromordinal` outside it would raise).
_MIN_DATE_ORDINAL: Final[int] = 1
_MAX_DATE_ORDINAL: Final[int] = 3_652_059  # date.max.toordinal()

#: The crate run tuple arity — `(run_first, run_last, run_sessions)`.
_RUN_TUPLE_ARITY: Final[int] = 3

#: The crate verdict-fields arity — `(reason, last_bar, expected_end, missing)`.
_VERDICT_FIELDS_ARITY: Final[int] = 4


@dataclass(frozen=True, slots=True)
class InteriorRunsPayload:
    """The ints-only FFI payload for the interior run walker.

    Built by the target's payload factory: ``covered_ordinals`` are the
    covered-date ordinals (order-preserving — membership is a set lookup
    crate-side) and ``interior_session_ordinals`` the ordered TRADING-session
    ordinals STRICTLY between the first and last covered date, walked with
    the SAME :func:`~brutex.calendars.observed_sessions.is_trading_day_observed`
    the oracle consults (the calendar stays Python — never re-implemented
    in Rust).
    """

    covered_ordinals: tuple[int, ...]
    interior_session_ordinals: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class PartialWindowPayload:
    """The ints-only FFI payload for the partial-window verdict.

    ``trading_flags`` is the Python-computed SESSION GRID — one
    ``is_trading_day_observed`` flag per day over
    ``[span_start_ordinal, span_start_ordinal + len(trading_flags))``;
    ``coverage_ok`` folds the three ``is_holiday_data_available`` facts
    (calendar-availability stays Python). ``tolerance``/``live_edge`` are
    the caller-resolved tunables (ONE env read, shared by both arms).
    """

    last_bar_ordinal: int | None
    window_end_ordinal: int
    today_ordinal: int
    expiry_ordinal: int | None
    coverage_ok: bool
    tolerance: int
    live_edge: int
    span_start_ordinal: int
    trading_flags: tuple[bool, ...]


@dataclass(frozen=True, slots=True)
class TodayCompletenessPayload:
    """The ints/bools-only FFI payload for the today-completeness verdict.

    Built by the target's payload factory (PR-2): ``minute_ordinals`` are
    per-bar ABSOLUTE IST minute ordinals
    (``ist_date.toordinal() * 1440 + hour * 60 + minute`` — date-preserving,
    so distinct ordinals ≡ the oracle's distinct IST minute-floor datetimes
    over EVERY input, no single-date precondition); ``zero_volume`` /
    ``oi_null`` the parallel per-bar shape flags; ``last_bar_complete`` the
    Python-computed :func:`~brutex.calendars.nse.session_last_bar_complete`
    close-edge law (the calendar stays Python — never re-implemented in
    Rust; the target consults it LAZILY, mirroring the oracle —
    ``INDEX_SPOT`` only, every other class ships the neutral ``True`` the
    crate ignores). ``instrument_class_code`` maps the target's
    ``InstrumentClass`` onto the crate's 0..3 codes.
    """

    instrument_class_code: int
    expected_minutes: int
    minute_ordinals: tuple[int, ...]
    zero_volume: tuple[bool, ...]
    oi_null: tuple[bool, ...]
    last_bar_complete: bool


@dataclass(frozen=True, slots=True)
class EconomicsIntegrityPayload:
    """The ints-only FFI payload for the economics lot fold (PR-3a).

    Built by the target's payload factory over the FILTERED legs (non-leg
    rows, empty underlyings and ``*NSETEST*``-marked legs are skipped
    PYTHON-side — the Unicode ``.upper()`` containment never crosses):
    ``group_ids`` / ``under_ids`` are the dense interned ids of each kept
    leg's ``(exchange, underlying, expiry)`` / ``(exchange, underlying)``
    keys, ``lots`` the parallel ``lot_size`` values. ``group_count`` /
    ``under_count`` are the interned-id ranges — the refuse-poison gate
    holds every crate-returned id inside them and the crate's observed
    group count EQUAL to ``group_count`` (a mismatch is a poisoned crate →
    oracle).
    """

    group_ids: tuple[int, ...]
    under_ids: tuple[int, ...]
    lots: tuple[int, ...]
    group_count: int
    under_count: int


@dataclass(frozen=True, slots=True)
class MasterIntegrityPayload:
    """The ints-only FFI payload for the master-integrity fold (PR-3a).

    Built by the target's payload factory: one entry per IDX/EQ row (input
    order) in the three ``spot_*`` vectors — the interned ``(exchange,
    symbol_name)`` key, the interned ``(exchange, security_id)`` identity,
    and the interned token key (``-1`` when the row carries no GENUINE
    numeric ``feed_token`` — the ``str.isdigit()`` Unicode-trap predicate
    stays Python) — and one entry per ROUTED FUT/CE/PE leg (input order,
    POST the Python empty-underlying/test-marker filtering) in the two
    ``leg_*`` vectors (``-1`` = no symbol / no token; the leg symbol key
    is alias-REVERSED Python-side before interning). ``identity_count``
    is the interned identity range — the refuse-poison gate holds the
    crate's unreferenced-spot count inside ``[0, identity_count]``.
    """

    spot_sym_key_ids: tuple[int, ...]
    spot_identity_ids: tuple[int, ...]
    spot_token_key_ids: tuple[int, ...]
    leg_sym_key_ids: tuple[int, ...]
    leg_tok_key_ids: tuple[int, ...]
    identity_count: int


def raw_integrity_rust_enabled() -> bool:
    """Resolve ``BRUTEX_LAKE_RUST_RAW_INTEGRITY`` (default OFF, junk loud-OFF)."""
    return _flag_enabled(LAKE_RUST_RAW_INTEGRITY_ENV)


def interior_gap_rust_enabled() -> bool:
    """Resolve ``BRUTEX_LAKE_RUST_INTERIOR_GAP`` (default OFF, junk loud-OFF)."""
    return _flag_enabled(LAKE_RUST_INTERIOR_GAP_ENV)


def partial_window_rust_enabled() -> bool:
    """Resolve ``BRUTEX_LAKE_RUST_PARTIAL_WINDOW`` (default OFF, junk loud-OFF)."""
    return _flag_enabled(LAKE_RUST_PARTIAL_WINDOW_ENV)


def today_completeness_rust_enabled() -> bool:
    """Resolve ``BRUTEX_LAKE_RUST_TODAY_COMPLETENESS`` (default OFF, junk loud-OFF)."""
    return _flag_enabled(LAKE_RUST_TODAY_COMPLETENESS_ENV)


def economics_integrity_rust_enabled() -> bool:
    """Resolve ``BRUTEX_LAKE_RUST_ECONOMICS_INTEGRITY`` (default OFF, junk loud-OFF)."""
    return _flag_enabled(LAKE_RUST_ECONOMICS_INTEGRITY_ENV)


def master_integrity_rust_enabled() -> bool:
    """Resolve ``BRUTEX_LAKE_RUST_MASTER_INTEGRITY`` (default OFF, junk loud-OFF)."""
    return _flag_enabled(LAKE_RUST_MASTER_INTEGRITY_ENV)


def rust_detectors_disabled() -> bool:
    """``True`` iff a guard/autorevert latched the Rust path OFF this process."""
    return _DISABLED_REASON[0] is not None


def reset_detectors_rust_for_tests(*, remaining: int = BURN_IN_SAMPLES) -> None:
    """Reset latch + per-law burn-in budgets + warn memo (test isolation ONLY).

    ``remaining=0`` puts the process in the post-burn-in steady state (the
    Rust value ships with no oracle in the path — the differential suites
    depend on that). Never called from production code (the sibling
    facades' reset-seam convention).
    """
    _DISABLED_REASON[0] = None
    _BURN_IN_REMAINING[_LAW_HASH] = remaining
    _BURN_IN_REMAINING[_LAW_INTERIOR_RUNS] = remaining
    _BURN_IN_REMAINING[_LAW_PARTIAL_WINDOW] = remaining
    _BURN_IN_REMAINING[_LAW_TODAY_COMPLETENESS] = remaining
    _BURN_IN_REMAINING[_LAW_ECONOMICS_INTEGRITY] = remaining
    _BURN_IN_REMAINING[_LAW_MASTER_INTEGRITY] = remaining
    _OBSERVABILITY_MEMO.clear()


def _flag_enabled(env_var: str) -> bool:
    """The ONE tristate resolution all three flags share (per-call env read).

    Truthy literal → ``True``; falsy literal → ``False``; unset/blank →
    ``False`` (the default); any OTHER non-empty value → ``False`` with one
    ``detectors_rust.fallback`` WARN (``flag_unrecognised``, naming the
    variable) per process per variable.
    """
    raw = os.environ.get(env_var, "")
    verdict = env_flag_tristate(raw)
    if verdict is not None:
        return verdict
    if raw.strip():
        _warn_once(f"flag_unrecognised:{env_var}", value=raw, env_var=env_var)
    return False


def _warn_once(reason: str, **context: object) -> None:
    """Log ``detectors_rust.fallback`` WARN once per process per *reason*."""
    if reason in _OBSERVABILITY_MEMO:
        return
    _OBSERVABILITY_MEMO.add(reason)
    _LOG.warning("detectors_rust.fallback", reason=reason, **context)


def _disable(reason: str, *, error: bool = False, **context: object) -> None:
    """Latch the Rust path OFF for the remainder of the process.

    ``error=True`` logs ``detectors_rust.autoreverted`` at ERROR (the
    burn-in autorevert — a genuine wrongness signal); guard latches log
    ``detectors_rust.rust_path_disabled`` at WARN. Repeats are no-ops.
    """
    if _DISABLED_REASON[0] is not None:
        return
    _DISABLED_REASON[0] = reason
    if error:
        _LOG.error("detectors_rust.autoreverted", reason=reason, **context)
    else:
        _LOG.warning("detectors_rust.rust_path_disabled", reason=reason, **context)


def _resolve_crate() -> ModuleType | None:
    """Import guard + SYMBOL handshake: ``brutex_lake``, or latch OFF + ``None``.

    An absent wheel (``ImportError``) latches ``crate_unavailable``. A wheel
    that imports but lacks ANY :data:`_CRATE_API` callable is a pre-T24
    (or foreign/broken) install — latch ``crate_stale`` (the stale-wheel
    handshake; the polars version guard is meaningless for a polars-free
    law, module docstring). Fail-closed: never a new crash class.
    """
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
                "the installed wheel predates the T24 detector laws (or is foreign/broken)",
                remedy="rebuild + reinstall rust/brutex-lake "
                "(see docs/runbooks/lake-rust-render-lockstep.md)",
            )
            return None
    return crate


def _log_engaged_once(crate: ModuleType) -> None:
    """Log ``detectors_rust.engaged`` INFO once per process."""
    if _ENGAGED_KEY in _OBSERVABILITY_MEMO:
        return
    _OBSERVABILITY_MEMO.add(_ENGAGED_KEY)
    _LOG.info("detectors_rust.engaged", crate_version=getattr(crate, "__version__", None))


def _burn_in_check[VerdictT](
    law: str, rust_value: VerdictT, python_law: Callable[[], VerdictT]
) -> VerdictT:
    """The per-law burn-in leg: compare *rust_value* against the live oracle.

    Returns the value to SHIP — the Rust value on a clean compare (or in
    the post-burn-in steady state), the ORACLE value (+ autorevert latch,
    ERROR log) on any difference.
    """
    if _BURN_IN_REMAINING[law] <= 0:
        return rust_value
    oracle_value = python_law()
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
        _LOG.info("detectors_rust.burn_in_complete", law=law, samples=BURN_IN_SAMPLES)
    return rust_value


def routed_hash_payload_hex(canonical: bytes, *, python_law: Callable[[], str]) -> str:
    """The routed raw-integrity digest — called ONLY when the flag is ON.

    *canonical* is the Python-computed canonical JSON byte string (the
    ``json.dumps`` law stays Python — module docstring); *python_law* is the
    zero-arg verbatim ``hashlib.blake2b`` oracle. EVERY path returns a valid
    hex digest: latched / guarded / degraded calls return ``python_law()``,
    a burn-in mismatch returns the ORACLE digest + autoreverts, and only a
    guard-clean, burn-in-clean call returns the Rust digest.
    """
    if rust_detectors_disabled():
        return python_law()
    crate = _resolve_crate()
    if crate is None:
        return python_law()
    try:
        rust_hex = crate.raw_integrity_blake2b_hex(canonical, _RAW_INTEGRITY_DIGEST_SIZE)
    except _DEGRADE_ERRORS as exc:
        _warn_once("hash_error", error_detail=repr(exc))
        return python_law()
    except (KeyboardInterrupt, SystemExit, MemoryError):
        # The escape set (the errors.py doctrine): operator interrupt /
        # interpreter shutdown / OOM stay global — never a silent fallback.
        # Sits ABOVE the panic belt so its `Exception` member can never
        # swallow a MemoryError.
        raise
    except _panic_belt_errors() as exc:
        _disable("crate_panic", error_detail=repr(exc))
        return python_law()
    if not isinstance(rust_hex, str):
        # Refuse-poison gate: a non-str return is a crate BUG — never
        # served (structurally unreachable through the real crate; guards a
        # stale/foreign install).
        _warn_once("hash_result_refused", result_type=type(rust_hex).__name__)
        return python_law()
    _log_engaged_once(crate)
    return _burn_in_check(_LAW_HASH, rust_hex, python_law)


def routed_interior_barless_runs(
    *,
    payload_factory: Callable[[], InteriorRunsPayload],
    python_law: Callable[[], list[tuple[date, date, int]]],
) -> list[tuple[date, date, int]]:
    """The routed interior run walker — called ONLY when the flag is ON.

    *payload_factory* builds the ints-only :class:`InteriorRunsPayload`
    (the target's one calendar walk — a latched process never pays it);
    *python_law* is the zero-arg verbatim walker oracle. The crate's int
    3-tuples are materialized to ``(date, date, int)`` here
    (``date.fromordinal``) behind a refuse-poison shape gate.
    """
    if rust_detectors_disabled():
        return python_law()
    crate = _resolve_crate()
    if crate is None:
        return python_law()
    try:
        payload = payload_factory()
        rust_runs = crate.interior_barless_runs(
            payload.covered_ordinals, payload.interior_session_ordinals
        )
    except _DEGRADE_ERRORS as exc:
        _warn_once("interior_runs_error", error_detail=repr(exc))
        return python_law()
    except (KeyboardInterrupt, SystemExit, MemoryError):
        raise
    except _panic_belt_errors() as exc:
        _disable("crate_panic", error_detail=repr(exc))
        return python_law()
    materialized = _materialize_runs(rust_runs)
    if materialized is None:
        _warn_once("interior_runs_result_refused", result_type=type(rust_runs).__name__)
        return python_law()
    _log_engaged_once(crate)
    return _burn_in_check(_LAW_INTERIOR_RUNS, materialized, python_law)


def _materialize_runs(rust_runs: object) -> list[tuple[date, date, int]] | None:
    """Materialize the crate's run tuples to dates, or ``None`` to refuse.

    The refuse-poison gate for the interior-runs law: anything but a list
    of ``(int, int, int)`` 3-tuples with both ordinals inside the ``date``
    domain is refused to the oracle — a poisoned wheel can only degrade,
    never mint a junk date.
    """
    if not isinstance(rust_runs, list):
        return None
    out: list[tuple[date, date, int]] = []
    for item in rust_runs:
        if not (isinstance(item, tuple) and len(item) == _RUN_TUPLE_ARITY):
            return None
        first, last, sessions = item
        if not all(isinstance(value, int) for value in (first, last, sessions)):
            return None
        if not (
            _MIN_DATE_ORDINAL <= first <= _MAX_DATE_ORDINAL
            and _MIN_DATE_ORDINAL <= last <= _MAX_DATE_ORDINAL
        ):
            return None
        out.append((date.fromordinal(first), date.fromordinal(last), sessions))
    return out


def routed_classify_partial_window[VerdictT](
    *,
    payload_factory: Callable[[], PartialWindowPayload],
    python_law: Callable[[], VerdictT],
    materialize: Callable[[str, int | None, int | None, int], VerdictT],
) -> VerdictT:
    """The routed partial-window verdict — called ONLY when the flag is ON.

    *payload_factory* builds the ints-only :class:`PartialWindowPayload`
    (the target's IST conversions + coverage facts + session grid);
    *python_law* is the zero-arg verbatim classifier oracle; *materialize*
    turns the crate's ``(reason, last_bar_ordinal, expected_end_ordinal,
    missing_sessions)`` fields into the target's verdict object (defined
    target-side so this facade never imports the verdict dataclass — no
    import cycle). A crate ``None`` is the ABSTAIN contract (a session walk
    stepped off the grid) — one WARN per process, the oracle serves.
    """
    if rust_detectors_disabled():
        return python_law()
    crate = _resolve_crate()
    if crate is None:
        return python_law()
    try:
        payload = payload_factory()
        fields = crate.classify_partial_window_verdict(
            payload.last_bar_ordinal,
            payload.window_end_ordinal,
            payload.today_ordinal,
            payload.expiry_ordinal,
            payload.coverage_ok,
            payload.tolerance,
            payload.live_edge,
            payload.span_start_ordinal,
            payload.trading_flags,
        )
    except _DEGRADE_ERRORS as exc:
        _warn_once("partial_window_error", error_detail=repr(exc))
        return python_law()
    except (KeyboardInterrupt, SystemExit, MemoryError):
        raise
    except _panic_belt_errors() as exc:
        _disable("crate_panic", error_detail=repr(exc))
        return python_law()
    if fields is None:
        # The envelope ABSTAIN (a walk stepped off the Python-built grid) —
        # an engagement miss, never a fault: warn once, serve the oracle.
        _warn_once("partial_window_abstain")
        return python_law()
    if not _partial_fields_well_formed(fields):
        _warn_once("partial_window_result_refused", result_type=type(fields).__name__)
        return python_law()
    reason, last_bar_ordinal, expected_end_ordinal, missing = fields
    verdict = materialize(reason, last_bar_ordinal, expected_end_ordinal, missing)
    _log_engaged_once(crate)
    return _burn_in_check(_LAW_PARTIAL_WINDOW, verdict, python_law)


#: The today-completeness reason vocabulary, INDEXED — the crate returns a
#: ``reason_idx`` into THIS tuple (the same order as the crate's
#: ``TODAY_REASON_*`` constants). SOURCE: the ``REASON_*`` constants of
#: :mod:`brutex.data.today_completeness` (importing that module here would
#: be the verdict-dataclass import cycle the G6 routing shape exists to
#: avoid), so the tuple is defined beside its gate and MIRROR-PINNED
#: against the oracle's own constants by ``tests/unit/test_detectors_rust.py``.
#: Only the ROUTED arms appear — the three inert reasons (not_covering /
#: abstain_coverage / non_trading) are decided Python-side pre-route.
_TODAY_REASONS: Final[tuple[str, ...]] = (
    "complete",
    "no_today_bars",
    "count_short",
    "count_excess",
    "last_bar_incomplete",
    "oi_null_settling",
    "volume_all_zero",
)

#: The crate today-verdict fields arity —
#: ``(incomplete, reason_idx, distinct, oi_null_count, zero_volume_count,
#: count_short_observed)``.
_TODAY_FIELDS_ARITY: Final[int] = 6


def _today_fields_well_formed(fields: object) -> bool:
    """The refuse-poison shape gate for the today-completeness fields.

    Arity 6; ``incomplete`` / ``count_short_observed`` exact ``bool``;
    ``reason_idx`` a non-bool ``int`` inside :data:`_TODAY_REASONS`; the
    three counts non-bool non-negative ``int``. Anything else is a
    stale/foreign crate speaking a different verdict vocabulary — refused
    to the oracle with one WARN (degrade, never a latch).
    """
    if not (isinstance(fields, tuple) and len(fields) == _TODAY_FIELDS_ARITY):
        return False
    incomplete, reason_idx, distinct, oi_null_count, zero_volume_count, count_short = fields
    if not (isinstance(incomplete, bool) and isinstance(count_short, bool)):
        return False
    if isinstance(reason_idx, bool) or not isinstance(reason_idx, int):
        return False
    if not 0 <= reason_idx < len(_TODAY_REASONS):
        return False
    for count in (distinct, oi_null_count, zero_volume_count):
        if isinstance(count, bool) or not isinstance(count, int):
            return False
        if count < 0:
            return False
    return True


def routed_classify_today_completeness[VerdictT](
    *,
    payload_factory: Callable[[], TodayCompletenessPayload],
    python_law: Callable[[], VerdictT],
    materialize: Callable[[str, bool, int, int, int, bool], VerdictT],
) -> VerdictT:
    """The routed today-completeness verdict — called ONLY when the flag is ON.

    *payload_factory* builds the ints/bools-only
    :class:`TodayCompletenessPayload` (the target's IST conversions +
    the Python-computed close-edge law); *python_law* is the zero-arg
    verbatim criteria-table oracle; *materialize* turns
    ``(reason_token, incomplete, distinct, oi_null_count,
    zero_volume_count, count_short_observed)`` into the target's verdict
    object (defined target-side so this facade never imports the verdict
    dataclass — no import cycle). EVERY path returns a verdict object:
    latched / guarded / degraded / refused calls return ``python_law()``,
    a burn-in mismatch returns the ORACLE verdict + autoreverts — the
    crossverify fail-closed gate seam depends on exactly this
    never-a-new-exception-class discipline (module docstring, pinned).
    The PAYLOAD call carries its OWN degrade arm including ``KeyError``
    (the target's ``_TODAY_CLASS_CODES`` lookup — structurally total
    today; round-1 review defense-in-depth) so a poisoned/unknown class
    token degrades warn-once to the oracle instead of mis-latching
    ``crate_panic`` through the ``Exception``-inclusive panic belt.
    """
    if rust_detectors_disabled():
        return python_law()
    crate = _resolve_crate()
    if crate is None:
        return python_law()
    try:
        payload = payload_factory()
    except _PAYLOAD_DEGRADE_ERRORS as exc:
        # The payload factory is pure Python (no FFI, no panic belt needed):
        # a `KeyError` here is the target's class-code lookup missing a
        # token — structurally unreachable today (the closed `InstrumentClass`
        # enum makes `_TODAY_CLASS_CODES` total), but under the flag it must
        # resolve to the ORACLE verdict with one WARN, never a `crate_panic`
        # latch (the `Exception` member of the panic belt would otherwise
        # swallow it) and never a new exception class out of the classifier
        # (the crossverify gate-seam law). KeyboardInterrupt/SystemExit/
        # MemoryError propagate structurally — none is a member of this set.
        _warn_once("today_completeness_payload_error", error_detail=repr(exc))
        return python_law()
    try:
        fields = crate.classify_today_completeness_verdict(
            payload.instrument_class_code,
            payload.expected_minutes,
            payload.minute_ordinals,
            payload.zero_volume,
            payload.oi_null,
            payload.last_bar_complete,
        )
    except _DEGRADE_ERRORS as exc:
        _warn_once("today_completeness_error", error_detail=repr(exc))
        return python_law()
    except (KeyboardInterrupt, SystemExit, MemoryError):
        raise
    except _panic_belt_errors() as exc:
        _disable("crate_panic", error_detail=repr(exc))
        return python_law()
    if not _today_fields_well_formed(fields):
        _warn_once("today_completeness_result_refused", result_type=type(fields).__name__)
        return python_law()
    incomplete, reason_idx, distinct, oi_null_count, zero_volume_count, count_short = fields
    verdict = materialize(
        _TODAY_REASONS[reason_idx],
        incomplete,
        distinct,
        oi_null_count,
        zero_volume_count,
        count_short,
    )
    _log_engaged_once(crate)
    return _burn_in_check(_LAW_TODAY_COMPLETENESS, verdict, python_law)


#: The KNOWN partial-window reason-token vocabulary — the refuse-poison
#: MEMBERSHIP set for the crate-returned ``reason`` field (the round-1
#: review hardening). SOURCE: the inline ``reason="…"`` literals of
#: :func:`brutex.data.partial_window_detect._classify_partial_window_python`
#: (the oracle mints them as literals — no named constant exists there to
#: import, and importing that module HERE would be the verdict-dataclass
#: import cycle the G6 routing shape exists to avoid), so the set is
#: defined beside its gate and MIRROR-PINNED against the oracle's own
#: source by ``tests/unit/test_detectors_rust.py``.
_KNOWN_PARTIAL_REASONS: Final[frozenset[str]] = frozenset(
    {
        "empty",
        "out_of_calendar_coverage",
        "at_expiry",
        "live_edge",
        "complete",
        "truncated",
        "sparse_complete",
    }
)


def _partial_fields_well_formed(fields: object) -> bool:
    """The refuse-poison shape gate for the partial-window verdict fields.

    Beyond arity / field types / the ``date``-domain ordinal bounds, the
    ``reason`` must be a member of :data:`_KNOWN_PARTIAL_REASONS` — an
    unknown token is a stale/foreign crate speaking a different verdict
    vocabulary, refused to the oracle exactly like every other shape
    failure (one WARN; degrade, never a latch).
    """
    if not (isinstance(fields, tuple) and len(fields) == _VERDICT_FIELDS_ARITY):
        return False
    reason, last_bar_ordinal, expected_end_ordinal, missing = fields
    if not isinstance(reason, str) or reason not in _KNOWN_PARTIAL_REASONS:
        return False
    for ordinal in (last_bar_ordinal, expected_end_ordinal):
        if ordinal is None:
            continue
        if not isinstance(ordinal, int):
            return False
        if not _MIN_DATE_ORDINAL <= ordinal <= _MAX_DATE_ORDINAL:
            return False
    return isinstance(missing, int)


# ---------------------------------------------------------------------------
# SLICE T24 PR-3a — laws 5 (economics lot fold) + 6 (master-integrity fold).
# ---------------------------------------------------------------------------

#: The crate economics-fold fields arity — ``(mismatch_group_ids,
#: mismatch_lots, revision_under_ids, revision_lots, expiry_group_count)``.
_ECONOMICS_FIELDS_ARITY: Final[int] = 5

#: The crate master-fold fields arity — ``(bucket_codes,
#: unreferenced_spot_count)``.
_MASTER_FIELDS_ARITY: Final[int] = 2

#: The master-fold bucket-code vocabulary size (codes ``0..5`` — the
#: ``BUCKET_*`` constants of ``rust/brutex-lake/src/audits.rs``, mirrored
#: by the target's ``_CODE_*`` constants; an out-of-range code is a
#: stale/foreign crate speaking a different vocabulary — refused).
_MASTER_BUCKET_CODE_COUNT: Final[int] = 6


def _non_bool_int(value: object) -> bool:
    """``True`` iff *value* is an ``int`` that is not a ``bool`` (the gates' int law)."""
    return isinstance(value, int) and not isinstance(value, bool)


def _flagged_ids_well_formed(ids: object, lots_lists: object, *, upper: int) -> bool:
    """One flagged-id family of the economics gate: ids + parallel lot lists.

    Ids must be non-bool ints inside the Python-interned ``[0, upper)``
    range; each parallel lots list must be a ``list`` of ≥ 2 non-bool ints
    in STRICTLY ascending order (a flagged group by definition holds ≥ 2
    distinct lots, and the crate law returns them sorted-distinct).
    """
    if not (isinstance(ids, list) and isinstance(lots_lists, list)):
        return False
    if len(ids) != len(lots_lists):
        return False
    for flagged_id in ids:
        if not _non_bool_int(flagged_id):
            return False
        if not 0 <= flagged_id < upper:
            return False
    for lots in lots_lists:
        if not isinstance(lots, list) or len(lots) < 2:
            return False
        previous: int | None = None
        for lot in lots:
            if not _non_bool_int(lot):
                return False
            if previous is not None and lot <= previous:
                return False
            previous = lot
    return True


def _economics_fields_well_formed(fields: object, payload: EconomicsIntegrityPayload) -> bool:
    """The refuse-poison shape gate for the economics-fold fields.

    Arity 5; both flagged-id families via :func:`_flagged_ids_well_formed`
    (group ids bounded by ``payload.group_count``, underlying ids by
    ``payload.under_count``); ``expiry_group_count`` a non-bool int EQUAL
    to the Python-known interned group count (the design §1.3 handshake —
    a crate that saw a different group universe is poisoned). Anything
    else is refused to the oracle with one WARN (degrade, never a latch).
    """
    if not (isinstance(fields, tuple) and len(fields) == _ECONOMICS_FIELDS_ARITY):
        return False
    mismatch_ids, mismatch_lots, revision_ids, revision_lots, group_count = fields
    if not _flagged_ids_well_formed(mismatch_ids, mismatch_lots, upper=payload.group_count):
        return False
    if not _flagged_ids_well_formed(revision_ids, revision_lots, upper=payload.under_count):
        return False
    return _non_bool_int(group_count) and group_count == payload.group_count


def routed_audit_economics_integrity[ReportT](
    *,
    payload_factory: Callable[[], EconomicsIntegrityPayload],
    python_law: Callable[[], ReportT],
    materialize: Callable[
        [Sequence[int], Sequence[Sequence[int]], Sequence[int], Sequence[Sequence[int]]],
        ReportT,
    ],
) -> ReportT:
    """The routed economics lot fold — called ONLY when the flag is ON.

    *payload_factory* builds the ints-only :class:`EconomicsIntegrityPayload`
    (the target's one interning walk — a latched process never pays it);
    *python_law* is the zero-arg verbatim audit oracle; *materialize* turns
    the crate's ``(mismatch_group_ids, mismatch_lots, revision_under_ids,
    revision_lots)`` into the target's report object (defined target-side —
    the string sort + ``SAMPLE_CAP`` + dataclass build live where the
    strings live; this facade never imports the report dataclass — no
    import cycle). EVERY path returns a report: latched / guarded /
    degraded / refused calls return ``python_law()``, a burn-in mismatch
    returns the ORACLE report + autoreverts. The PAYLOAD call carries its
    OWN :data:`_PAYLOAD_DEGRADE_ERRORS` arm (the PR-2 template).
    """
    if rust_detectors_disabled():
        return python_law()
    crate = _resolve_crate()
    if crate is None:
        return python_law()
    try:
        payload = payload_factory()
    except _PAYLOAD_DEGRADE_ERRORS as exc:
        # Pure-Python payload build (no FFI, no panic belt needed) — a
        # fault here degrades warn-once to the oracle, never a latch;
        # KeyboardInterrupt/SystemExit/MemoryError propagate structurally.
        _warn_once("economics_integrity_payload_error", error_detail=repr(exc))
        return python_law()
    try:
        fields = crate.economics_lot_fold(payload.group_ids, payload.under_ids, payload.lots)
    except _DEGRADE_ERRORS as exc:
        _warn_once("economics_integrity_error", error_detail=repr(exc))
        return python_law()
    except (KeyboardInterrupt, SystemExit, MemoryError):
        raise
    except _panic_belt_errors() as exc:
        _disable("crate_panic", error_detail=repr(exc))
        return python_law()
    if not _economics_fields_well_formed(fields, payload):
        _warn_once("economics_integrity_result_refused", result_type=type(fields).__name__)
        return python_law()
    mismatch_ids, mismatch_lots, revision_ids, revision_lots, _group_count = fields
    report = materialize(mismatch_ids, mismatch_lots, revision_ids, revision_lots)
    _log_engaged_once(crate)
    return _burn_in_check(_LAW_ECONOMICS_INTEGRITY, report, python_law)


def _master_fields_well_formed(fields: object, payload: MasterIntegrityPayload) -> bool:
    """The refuse-poison shape gate for the master-fold fields.

    Arity 2; ``bucket_codes`` a ``list`` whose length EQUALS the routed
    leg-vector length, every element a non-bool int inside the
    :data:`_MASTER_BUCKET_CODE_COUNT` vocabulary; the unreferenced-spot
    count a non-bool int inside ``[0, payload.identity_count]``. The
    design-sketch count returns (and their sum-consistency channel) are
    structurally absent — Python tallies the codes itself (the Q8
    recording); anything else is refused to the oracle with one WARN.
    """
    if not (isinstance(fields, tuple) and len(fields) == _MASTER_FIELDS_ARITY):
        return False
    bucket_codes, unreferenced = fields
    if not isinstance(bucket_codes, list):
        return False
    if len(bucket_codes) != len(payload.leg_sym_key_ids):
        return False
    for code in bucket_codes:
        if not _non_bool_int(code):
            return False
        if not 0 <= code < _MASTER_BUCKET_CODE_COUNT:
            return False
    return _non_bool_int(unreferenced) and 0 <= unreferenced <= payload.identity_count


def routed_audit_master_integrity[ReportT](
    *,
    payload_factory: Callable[[], MasterIntegrityPayload],
    python_law: Callable[[], ReportT],
    materialize: Callable[[Sequence[int], int], ReportT],
) -> ReportT:
    """The routed master-integrity fold — called ONLY when the flag is ON.

    *payload_factory* builds the ints-only :class:`MasterIntegrityPayload`
    (the target's two-pass interning walk — type counts, the
    Decimal-bearing dedup, the Unicode predicates and the alias reversal
    all happen there, Python-side); *python_law* is the zero-arg verbatim
    audit oracle; *materialize* turns the crate's ``(bucket_codes,
    unreferenced_spot_count)`` into the target's report object (the
    input-order orphan-sample walk + count tallies + dataclass build live
    target-side — no import cycle). EVERY path returns a report: latched /
    guarded / degraded / refused calls return ``python_law()``, a burn-in
    mismatch returns the ORACLE report + autoreverts. The PAYLOAD call
    carries its OWN :data:`_PAYLOAD_DEGRADE_ERRORS` arm (the PR-2
    template).
    """
    if rust_detectors_disabled():
        return python_law()
    crate = _resolve_crate()
    if crate is None:
        return python_law()
    try:
        payload = payload_factory()
    except _PAYLOAD_DEGRADE_ERRORS as exc:
        _warn_once("master_integrity_payload_error", error_detail=repr(exc))
        return python_law()
    try:
        fields = crate.master_integrity_fold(
            payload.spot_sym_key_ids,
            payload.spot_identity_ids,
            payload.spot_token_key_ids,
            payload.leg_sym_key_ids,
            payload.leg_tok_key_ids,
        )
    except _DEGRADE_ERRORS as exc:
        _warn_once("master_integrity_error", error_detail=repr(exc))
        return python_law()
    except (KeyboardInterrupt, SystemExit, MemoryError):
        raise
    except _panic_belt_errors() as exc:
        _disable("crate_panic", error_detail=repr(exc))
        return python_law()
    if not _master_fields_well_formed(fields, payload):
        _warn_once("master_integrity_result_refused", result_type=type(fields).__name__)
        return python_law()
    bucket_codes, unreferenced = fields
    report = materialize(bucket_codes, unreferenced)
    _log_engaged_once(crate)
    return _burn_in_check(_LAW_MASTER_INTEGRITY, report, python_law)


__all__ = [
    "BURN_IN_SAMPLES",
    "LAKE_RUST_ECONOMICS_INTEGRITY_ENV",
    "LAKE_RUST_INTERIOR_GAP_ENV",
    "LAKE_RUST_MASTER_INTEGRITY_ENV",
    "LAKE_RUST_PARTIAL_WINDOW_ENV",
    "LAKE_RUST_RAW_INTEGRITY_ENV",
    "LAKE_RUST_TODAY_COMPLETENESS_ENV",
    "EconomicsIntegrityPayload",
    "InteriorRunsPayload",
    "MasterIntegrityPayload",
    "PartialWindowPayload",
    "TodayCompletenessPayload",
    "economics_integrity_rust_enabled",
    "interior_gap_rust_enabled",
    "master_integrity_rust_enabled",
    "partial_window_rust_enabled",
    "raw_integrity_rust_enabled",
    "reset_detectors_rust_for_tests",
    "routed_audit_economics_integrity",
    "routed_audit_master_integrity",
    "routed_classify_partial_window",
    "routed_classify_today_completeness",
    "routed_hash_payload_hex",
    "routed_interior_barless_runs",
    "rust_detectors_disabled",
    "today_completeness_rust_enabled",
]
