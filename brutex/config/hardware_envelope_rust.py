"""Default-ON Rust seam for the hardware-envelope DERIVATION laws (slice T35).

Default flipped ON by DEC-RUST-DEFAULT-ON-WAVE-001 (operator directive
2026-07-23); explicit falsy is the kill switch back to the Python oracle.

DEC-CONFIG-RUST-LEAVES-001 (MIGRATION.md #1943), milestone 2. The FOUR
routed surfaces are :mod:`brutex.config.hardware_envelope`'s pure
derivation laws — when ``BRUTEX_CORE_RUST_HWENV`` is ON they delegate to
the ``brutex_core`` crate (``rust/brutex-core/src/hwenv.rs``) through this
facade; the flag-OFF default is the verbatim CPython oracle, output
byte-identical (behavior-identical; see the r1 wording correction in
MIGRATION #1943) AND crate-untouched (bomb-crate spy-pinned):

* ``cpu_pool_workers``' derivation arm (base selection + reserve clamp +
  cap — DEC-HARDWARE-ENVELOPE-001);
* ``library_thread_budget`` (DEC-THREAD-BUDGET-BOUNDS-001; inherited free
  by ``polars_thread_budget`` / ``duckdb_thread_budget``);
* ``memory_budget_bytes``' post-override arm (the DEC-RAM-BUDGETS-001
  sentinel + derived laws: undetected RAM → the floor EXACTLY, detected →
  ``max(1, int(total × fraction / workers))`` never floor-clamped);
* ``hardware_key``'s render arm (the DEC-BENCH-ABS-BASELINE-001
  ``format_hardware_key`` + ``slugify`` law — a Rust TWIN of the ONE
  Python law owned by :mod:`brutex.observability.bench_baseline`; the
  twin-impl coordination note lives in ``rust/brutex-core/src/hwenv.rs``).

Honesty banner (ADR-0020, mandatory verbatim): expected payoff ≈ 0 —
conversion is for uniformity, not speed. Every routed law is a
sub-microsecond int/string op; flag-ON ADDS an env read, an eligibility
gate and an FFI hop per call — a small slowdown, shipped INERT (a default
flip needs its own DEC). These laws size WORKER POOLS and BYTE BUDGETS
(sizing/parallelism-only — NO run-identity exposure, per the milestone
audit), so the parity bar is BEHAVIORAL (same derived numbers/keys on the
same box), enforced by the burn-in + the parity lane, never a stored-bytes
claim.

Flag-OFF cost disclosure (review r1, measured 2026-07-17 — the money_rust
mirror): the DEFAULT (flag-OFF) path pays a per-call env probe —
``os.environ.get`` on a miss is ~700 ns/call on the build box (an
exception-mediated ``_Environ`` lookup); the unset-fast-path in
:func:`hwenv_rust_enabled` shaves the strip/lower/set-lookup tail
(~65 ns) but the probe itself is DELIBERATE — spawn workers must read
their own inherited environment (the PR-1 posture; caching the resolve
would break that contract). These laws run at pool-build / budget-resolve
frequency (once per stage start, never per bar), so the probe is
aggregate-negligible; no hot path is affected.

## THE SEAM SPLIT (the honest convertible core — recorded here)

CONVERTED (this facade + ``src/hwenv.rs``): exactly the four PURE
derivation laws above — total over ints/strings, parity-provable on any
box, the per-call surfaces.

DELIBERATELY STAYS PYTHON (each with its reason):

* **Detection probes** (``sysctl`` / ``sched_getaffinity`` / ``sysconf``
  / ``/proc/cpuinfo``): once-per-process cached, fail-soft by design; a
  Rust port would add a ``libc`` dependency and a macOS ``sysctlbyname``
  arm that is UNTESTABLE in this Linux build environment — it would ship
  oracle-gated (dead) with zero payoff (Golden Rule #10). The smaller
  honest seam wins.
* **The env-override / fail-loud parse laws** (``_env_int``, the
  ``memory_budget_bytes`` validation + ``env_var`` override, the
  ``BENCH_BASELINE_KEY_ENV`` slug check): already-parsed ints cross the
  FFI, so every fail-loud env law stays byte-identical in Python.
* **``apply_polars_thread_bound``** (env-var write before the first
  ``import polars``) and **``apply_duckdb_thread_bound``** (the
  duck-typed ``SET threads`` pragma): side-effect surfaces, never
  converted.
* **The process-global caches + reset seams**, the frozen
  ``HardwareEnvelope`` / ``HardwareFingerprint`` construction (only
  ints/strings cross the FFI — Python materializes every dataclass), the
  ``_log_once`` observability, and ``hardware_fingerprint``'s ``slugify``
  calls (owned by :mod:`brutex.observability.bench_baseline` — the ONE
  Python home; only the KEY RENDER twin routes, and its burn-in compares
  against that home's law).

## Structural notes (the :mod:`brutex.money_rust` template, adapted)

* **A LOCAL flag-vocabulary twin, not an import** (the feed_rust /
  money_rust precedent): this is a CONFIG leaf — importing
  ``env_flag_tristate`` from ``brutex.engine.sweep_lookahead`` would drag
  the ``fno_math`` wheel under every config consumer, and importing the
  sibling money facade's private sets would couple two independent seams.
  The inline ``_TRUTHY``/``_FALSY`` sets are mirror-pinned against
  ``money_rust``'s by a vocabulary-equality test
  (``tests/unit/test_hardware_envelope_rust.py``).
* **No version guard** (the money_rust reasoning verbatim): the crate
  side is pure std-Rust int/string arithmetic locked to the CPython
  oracles — there is no wheel↔crate lockstep to attest. The per-law
  burn-in + the frozen literal goldens are the drift teeth.
* **ONE documented FFI-type deviation from milestone 1's strings-only
  law**: ``memory_budget_bytes``' ``fraction`` crosses as a native IEEE
  ``f64`` (PyO3 passes the bits through EXACTLY — no textual round-trip
  needed, no parse failure surface); everything else is ints/strings/bool
  in, int/string out. The eligibility gate admits ONLY exact-type
  ``float`` fractions (an ``int``/``Fraction``/``Decimal`` fraction runs
  a DIFFERENT CPython arithmetic law — exact rational vs f64 — and routes
  to the oracle).

Fail-closed layers, in order (every path returns the PYTHON-law value):

1. the caller-side flag gate in :mod:`brutex.config.hardware_envelope`
   (:func:`hwenv_rust_enabled` — default ON per
   DEC-RUST-DEFAULT-ON-WAVE-001; explicit falsy is the kill switch; junk
   values loud-OFF once
   per process);
2. the ELIGIBILITY gate (:func:`_eligible_pool` /
   :func:`_eligible_library` / :func:`_eligible_memory` /
   :func:`_eligible_key` — the gate law below): an input outside the
   crate's proven-exact domain is routed to the oracle WITHOUT touching
   the crate;
3. the import/API-surface guard (:func:`_resolve_crate` — absent wheel
   latches ``crate_unavailable``; a wheel missing a callable latches
   ``crate_stale``);
4. the refuse-poison gate — a non-exact-type or nonsense crate return
   (a ``bool``, a ``< 1`` worker/budget count, a slug-law-violating key
   string) is refused with one WARN (a poisoned crate can only degrade,
   never serve);
5. the per-law burn-in self-check — the first :data:`BURN_IN_SAMPLES`
   engaged calls PER LAW compute BOTH arms and compare; ANY difference
   ships the ORACLE value and autoreverts the Rust path for the process
   (the #1531/#1532 pattern);
6. the degrade set (:data:`_DEGRADE_ERRORS` + the pyo3 panic belt) — a
   crate raise serves the oracle with one WARN, never a new crash class.

``MemoryError`` / ``KeyboardInterrupt`` / ``SystemExit`` re-raise RAW (the
``errors.py`` escape-set doctrine): these laws size sweep/prebuild pools
and RAM budgets at stage start — an OOM there must stay a global signal,
never be laundered into a fallback.

## The ELIGIBILITY GATE LAW (layer 2)

Only values whose oracle outcome is provably reproduced by the crate's
i64/f64/ASCII arithmetic may cross (anything else → oracle, crate
untouched, spy-pinned):

* core counts / reserves / workers-divisors: exact type :class:`int`
  (``bool`` is an ``int`` subclass — oracle), ``|v| <=`` 2**31 (the
  crate's checked-arithmetic bound; every real count is tiny);
* ``prefer_performance``: exact type :class:`bool`;
* ``cap``: ``None`` or an exact ``int`` in ``[1, 2**31]`` (a ``< 1`` cap
  is a nonsense caller input whose oracle result can legitimately be
  ``< 1`` — routed to the oracle so the crate's engaged results are
  always ``>= 1`` and the poison gate stays sharp);
* ``detected_ram_bytes``: exact ``int`` in ``[0, 2**62)`` — the bound
  that keeps the f64 chain's trunc→int cast EXACT on the Rust side (a
  ``2**63``-scale total saturates an i64 cast where CPython's ``int()``
  yields the big int — the one divergence hazard, excluded here);
  ``floor_bytes``: exact ``int`` in ``[1, 2**62)``; ``workers``: exact
  ``int`` in ``[1, 2**31]``; ``fraction``: exact :class:`float`, finite,
  in ``(0, 1]`` (the deviation note above);
* key fields: ``model_slug``/``arch`` exact :class:`str`, ASCII-only
  (Python ``str.lower()`` ≡ Rust ``to_ascii_lowercase`` holds ONLY on
  ASCII — the Unicode-casefold divergence class is excluded wholesale),
  ``<=`` 200 chars; the four count fields per the count law.
"""

from __future__ import annotations

import importlib
import math
import os
from typing import TYPE_CHECKING, Final, cast

import structlog

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import ModuleType

_LOG = structlog.get_logger(__name__)

#: The env flag (default ON per DEC-RUST-DEFAULT-ON-WAVE-001; explicit
#: falsy is the kill switch). Canonical ``BRUTEX_<CRATE>_RUST_<SEAM>`` form.
HWENV_RUST_ENV: Final[str] = "BRUTEX_CORE_RUST_HWENV"

#: Engaged calls compared against the Python oracle PER LAW before crate
#: values ship un-oracled (the #1531/#1532 burn-in law).
BURN_IN_SAMPLES: Final[int] = 16

#: The four law tokens (per-law burn-in budgets + reason prefixes).
_LAW_POOL: Final[str] = "pool_workers"
_LAW_LIBRARY: Final[str] = "library_thread_budget"
_LAW_MEMORY: Final[str] = "memory_budget"
_LAW_KEY: Final[str] = "hardware_key"

#: The process-local autorevert latch — ``[None]`` = live; a reason string
#: latches the Rust path OFF for the remainder of the process.
_DISABLED_REASON: Final[list[str | None]] = [None]

#: Per-law burn-in budgets (mutated in place; reset seam below).
_BURN_IN_REMAINING: Final[dict[str, int]] = {
    _LAW_POOL: BURN_IN_SAMPLES,
    _LAW_LIBRARY: BURN_IN_SAMPLES,
    _LAW_MEMORY: BURN_IN_SAMPLES,
    _LAW_KEY: BURN_IN_SAMPLES,
}

#: Warn-once registry (per-process, test-resettable).
_OBSERVABILITY_MEMO: Final[set[str]] = set()

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
#: Cleared by :func:`reset_hwenv_rust_for_tests` — a test that swaps
#: ``sys.modules['brutex_core']`` must reset, or it is served the crate
#: resolved before its swap.
_CRATE_MEMO: Final[list[ModuleType | None]] = [None]

#: The engaged-INFO memo key (never a warn reason).
_ENGAGED_KEY: Final[str] = "__engaged__"

#: The crate callables this seam requires (the API-surface handshake —
#: independent of the milestone-1 money handshake by design: each seam
#: attests its OWN surface).
_CRATE_API: Final[tuple[str, ...]] = (
    "hwenv_pool_workers",
    "hwenv_library_thread_budget",
    "hwenv_memory_budget",
    "hwenv_hardware_key",
)

#: The narrow degrade-to-oracle catch set. ``ValueError`` is the crate's
#: declared domain refusal; ``RuntimeError`` its declared panic mapping;
#: ``TypeError``/``OverflowError`` are PyO3 argument-conversion failures;
#: ``AttributeError`` the broken-crate-internals belt. ``MemoryError`` is
#: DELIBERATELY absent (module docstring).
_DEGRADE_ERRORS: Final[tuple[type[Exception], ...]] = (
    ValueError,
    RuntimeError,
    TypeError,
    OverflowError,
    AttributeError,
)

#: Inline flag vocabulary (LEAF discipline — module docstring; the
#: mirror-pin test asserts byte-equality with ``money_rust``'s sets).
_TRUTHY: Final[frozenset[str]] = frozenset({"1", "true", "yes", "on"})
_FALSY: Final[frozenset[str]] = frozenset({"0", "false", "no", "off"})

#: Eligibility-gate bounds (the gate law — module docstring). The count
#: and RAM bounds mirror the crate's ``MAX_COUNT_MAGNITUDE`` /
#: ``MAX_DETECTED_RAM_BYTES`` constants.
_MAX_COUNT: Final[int] = 2**31
_MAX_DETECTED_RAM: Final[int] = 2**62
_MAX_KEY_FIELD_LEN: Final[int] = 200

#: The key shape law's alphabet (ASCII-strict — mirror of the rendered
#: ``slugify`` output; a subset of ``bench_baseline.KEY_SLUG_RE``, pinned
#: by the mirror test).
_KEY_CHARS: Final[frozenset[str]] = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-")


def hwenv_rust_enabled() -> bool:
    """Resolve ``BRUTEX_CORE_RUST_HWENV`` (default ON, junk loud-OFF).

    DEC-RUST-DEFAULT-ON-WAVE-001 (operator directive 2026-07-23): truthy →
    ``True``; falsy → ``False`` (the silent explicit kill switch); unset /
    whitespace-empty → ``True`` (the default); any OTHER non-empty value →
    ``False`` with one ``hwenv_rust.fallback`` WARN (reason
    ``flag_unrecognised``) per process — the junk arm is unchanged. Read per call so spawn workers
    resolve their own inherited environment (the PR-1 posture). The seam
    flag deliberately reads :data:`os.environ` — the callers' injectable
    ``env`` mappings scope their WORKER-COUNT overrides, not the seam.

    ABSENT fast path (review r1, MINOR — the money_rust mirror): an UNSET
    flag — the production default — returns ``True`` off the raw
    ``environ`` probe alone, skipping the strip/lower/set-lookup
    normalization; any SET value takes the full tristate handling
    byte-identically.
    """
    raw = os.environ.get(HWENV_RUST_ENV)
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


def hwenv_rust_disabled() -> bool:
    """``True`` iff a guard/autorevert latched the Rust path OFF this process."""
    return _DISABLED_REASON[0] is not None


def reset_hwenv_rust_for_tests(*, remaining: int = BURN_IN_SAMPLES) -> None:
    """Reset the latch + per-law burn-in budgets + warn memo (test isolation ONLY).

    ``remaining=0`` puts the process in the post-burn-in steady state so
    the adopted value is the crate's with no oracle in the path (the
    parity legs depend on that — an armed burn-in would cushion a
    divergence by shipping oracle values). Never called from production
    paths (the PR-1 reset-seam convention).
    """
    _DISABLED_REASON[0] = None
    for law in _BURN_IN_REMAINING:
        _BURN_IN_REMAINING[law] = remaining
    _OBSERVABILITY_MEMO.clear()
    _CRATE_MEMO[0] = None


def _refuse_if_strict(reason: str, **context: object) -> None:
    """Route a fall-back reason through the DEC-RUST-STRICT-NO-FALLBACK-001 gate.

    The import is DEFERRED (function-local) to keep this module a CONFIG LEAF:
    ``test_hardware_envelope_rust.py``'s AST pin requires stdlib + structlog
    ONLY at module scope, and ``brutex.config.rust_strict`` pulls in
    ``brutex.models``. The gate runs once per *announced fall-back*, never on
    the hot path, so the import cost is irrelevant.
    """
    from brutex.config.rust_strict import refuse_if_strict

    refuse_if_strict("brutex.config.hardware_envelope_rust", reason, **context)


def _warn_once(reason: str, **context: object) -> None:
    """Refuse under strict mode; else log ``hwenv_rust.fallback`` warn-once.

    DEC-RUST-STRICT-NO-FALLBACK-001. Every reason funnelled here is CLASS A
    (``flag_unrecognised`` — the junk-flag arm; ``{law}_error`` — the crate
    raised) or CLASS B (``{law}_result_refused`` — refuse-poison rejected the
    crate's answer); both refuse under ``BRUTEX_RUST_STRICT``.

    NO class-C reason reaches this choke point. This facade routes only the
    DERIVATION laws; hardware DETECTION is deliberately Python-only and never
    enters the seam, and the per-route eligibility gates (``_eligible_count``
    / ``_eligible_pool`` / ``_eligible_library`` / ``_eligible_memory`` /
    ``_eligible_key`` — inputs the crate never claimed) return the Python law
    SILENTLY, so they still serve Python under strict mode exactly as today.
    Strict-OFF is byte-identical to the prior behaviour.
    """
    _refuse_if_strict(reason, **context)
    if reason in _OBSERVABILITY_MEMO:
        return
    _OBSERVABILITY_MEMO.add(reason)
    _LOG.warning("hwenv_rust.fallback", reason=reason, **context)


def _disable(reason: str, *, error: bool = False, **context: object) -> None:
    """Refuse under strict mode; else latch the Rust path OFF for the process.

    DEC-RUST-STRICT-NO-FALLBACK-001. Reasons here are CLASS A
    (``crate_unavailable`` / ``crate_stale`` / ``crate_panic``) or CLASS B
    (``burn_in_mismatch_{law}`` — the crate disagreed with the oracle on a
    pool-sizing law). The refusal fires BEFORE the latch is written, so under
    strict mode the latch is never set and the ``hwenv_rust_disabled()``
    early-returns in the four routes are structurally unreachable — wiring
    these two functions therefore covers every ANNOUNCED fall-back.
    Strict-OFF is byte-identical to the prior behaviour.

    ``error=True`` logs ``hwenv_rust.autoreverted`` at ERROR (the burn-in
    autorevert — a genuine wrongness signal on a pool-sizing law); guard
    latches log ``hwenv_rust.rust_path_disabled`` at WARN. Repeats are
    no-ops.
    """
    _refuse_if_strict(reason, error=error, **context)
    if _DISABLED_REASON[0] is not None:
        return
    _DISABLED_REASON[0] = reason
    if error:
        _LOG.error("hwenv_rust.autoreverted", reason=reason, **context)
    else:
        _LOG.warning("hwenv_rust.rust_path_disabled", reason=reason, **context)


def _resolve_crate() -> ModuleType | None:
    """Import guard + SYMBOL handshake: ``brutex_core``, or latch OFF + ``None``.

    An absent wheel (``ImportError``) latches ``crate_unavailable``. A
    wheel that imports but lacks any callable :data:`_CRATE_API` member is
    a pre-hwenv-seam (or foreign/broken) install — latch ``crate_stale``
    (the money_rust stale-wheel handshake; no version guard — module
    docstring). Fail-closed: never an ``AttributeError`` out of this seam.
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
                "the installed wheel predates the hwenv seam (or is foreign/broken)",
                remedy="rebuild + reinstall rust/brutex-core (see rust/brutex-core/README.md)",
            )
            return None
    _CRATE_MEMO[0] = crate
    return crate


def _panic_belt_errors() -> tuple[type[BaseException], ...]:
    """The narrow FFI panic-belt catch set — the money_rust twin.

    ``pyo3_runtime.PanicException`` is the one ``BaseException`` subclass a
    pyo3 boundary can leak; the repo convention (CodeQL
    ``py/catch-base-exception``) forbids a non-re-raising literal ``except
    BaseException``, so the class is resolved GUARDEDLY by name and folded
    beside ``Exception``. Each facade carries its OWN belt copy (the
    money_rust ↔ lake_rust_stats precedent — the helper is private to each
    leaf; promoting a shared home would touch milestone-1 surface
    mid-review). The crate's own ``catch_unwind`` maps panics to
    ``RuntimeError`` (the degrade set), so this belt is defense-in-depth
    over a broken boundary.
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
    """Log ``hwenv_rust.engaged`` INFO once per process."""
    if _ENGAGED_KEY in _OBSERVABILITY_MEMO:
        return
    _OBSERVABILITY_MEMO.add(_ENGAGED_KEY)
    _LOG.info("hwenv_rust.engaged", crate_version=getattr(crate, "__version__", None))


# ── Eligibility gates (layer 2 — module docstring gate law) ─────────────


def _eligible_count(value: object) -> bool:
    """Exact-type ``int`` within the crate's ``|v| <= 2**31`` bound."""
    return type(value) is int and -_MAX_COUNT <= value <= _MAX_COUNT


def _eligible_pool(
    performance_cores: object,
    logical_cores: object,
    prefer_performance: object,
    reserve: object,
    cap: object,
) -> bool:
    """The ``pool_workers`` input gate (cap ``None`` or ``[1, 2**31]``)."""
    if type(prefer_performance) is not bool:
        return False
    if not (
        _eligible_count(performance_cores)
        and _eligible_count(logical_cores)
        and _eligible_count(reserve)
    ):
        return False
    if cap is None:
        return True
    return type(cap) is int and 1 <= cap <= _MAX_COUNT


def _eligible_library(physical_cores: object, workers: object) -> bool:
    """The ``library_thread_budget`` input gate."""
    return _eligible_count(physical_cores) and _eligible_count(workers)


def _eligible_memory(
    detected_ram_bytes: object,
    fraction: object,
    floor_bytes: object,
    workers: object,
) -> bool:
    """The ``memory_budget`` input gate (the f64-exactness bounds)."""
    if type(detected_ram_bytes) is not int or not 0 <= detected_ram_bytes < _MAX_DETECTED_RAM:
        return False
    if type(floor_bytes) is not int or not 1 <= floor_bytes < _MAX_DETECTED_RAM:
        return False
    if type(workers) is not int or not 1 <= workers <= _MAX_COUNT:
        return False
    if type(fraction) is not float:
        return False
    return math.isfinite(fraction) and 0.0 < fraction <= 1.0


def _eligible_key(
    model_slug: object,
    physical_cores: object,
    performance_cores: object,
    efficiency_cores: object,
    total_ram_gib: object,
    arch: object,
) -> bool:
    """The ``hardware_key`` input gate (ASCII-only strings — gate law)."""
    for text in (model_slug, arch):
        if type(text) is not str or len(text) > _MAX_KEY_FIELD_LEN or not text.isascii():
            return False
    return all(
        _eligible_count(value)
        for value in (physical_cores, performance_cores, efficiency_cores, total_ram_gib)
    )


# ── Refuse-poison shape laws (layer 4) ──────────────────────────────────


def _valid_positive_int(value: object) -> bool:
    """Shape law for the numeric laws' crate returns: exact ``int``, ``>= 1``.

    A ``bool`` (an ``int`` subclass whose equality would fool the burn-in
    compare) or a ``< 1`` "worker/budget count" is nonsense — refused. The
    engaged domains guarantee every correct result is ``>= 1`` (the pool
    gate excludes ``< 1`` caps precisely to keep this law sharp).
    """
    return type(value) is int and value >= 1


def _valid_key_str(value: object) -> bool:
    """Shape law for a ``hardware_key`` crate return.

    Non-empty, ``[a-z0-9-]`` only, alnum first + last char — the exact
    render range of the slugify law, a strict subset of the
    ``bench_baseline.KEY_SLUG_RE`` slug law (mirror-pinned in the tests).
    """
    if type(value) is not str or not value:
        return False
    if value[0] == "-" or value[-1] == "-":
        return False
    return all(ch in _KEY_CHARS for ch in value)


# ── The engaged-call core (layers 3-6) ──────────────────────────────────


def _adopt_crate_value[T](
    law: str,
    crate_name: str,
    arguments: tuple[object, ...],
    validate: Callable[[object], bool],
    oracle: Callable[[], T],
) -> T | None:
    """Run layers 3-6 for one engaged call; return the crate value to
    ADOPT, or ``None`` (= serve the Python oracle).

    *oracle* computes the pure Python-law result — only invoked while the
    law's burn-in budget is live. A burn-in mismatch autoreverts and
    returns ``None``: the caller recomputes the (pure, deterministic)
    oracle — one redundant compute on the one-shot autorevert path, never
    on the steady state. The ``cast`` on the adopted value is sound by
    construction: every call site's *validate* pins the exact runtime type
    its *oracle* returns (``int`` for the numeric laws, ``str`` for the
    key), so a type-punned crate return can never reach the cast.
    """
    crate = _resolve_crate()
    if crate is None:
        return None
    try:
        rust_value: object = getattr(crate, crate_name)(*arguments)
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
        # pool-sizing path.
        _disable("crate_panic", error_detail=repr(exc))
        return None
    if not validate(rust_value):
        # Refuse-poison gate: a wrong-typed or nonsense return (a bool, a
        # < 1 count, a slug-law-violating key) is a crate BUG — never
        # served. Structurally unreachable through the real crate; guards
        # a stale/foreign install.
        _warn_once(
            f"{law}_result_refused",
            result_type=type(rust_value).__name__,
            value_head=repr(rust_value)[:48],
        )
        return None
    _log_engaged_once(crate)
    if _BURN_IN_REMAINING[law] > 0:
        oracle_value = oracle()
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
            _LOG.info("hwenv_rust.burn_in_complete", law=law, samples=BURN_IN_SAMPLES)
    return cast("T", rust_value)


# ── The routed laws (called by hardware_envelope ONLY when the flag is ON) ──


def route_pool_workers(
    performance_cores: int,
    logical_cores: int,
    prefer_performance: bool,
    reserve: int,
    cap: int | None,
    python_law: Callable[[int, int, bool, int, int | None], int],
) -> int:
    """The routed ``cpu_pool_workers`` derivation — every path returns the
    correct worker count.
    """
    if hwenv_rust_disabled() or not _eligible_pool(
        performance_cores, logical_cores, prefer_performance, reserve, cap
    ):
        return python_law(performance_cores, logical_cores, prefer_performance, reserve, cap)
    adopted = _adopt_crate_value(
        _LAW_POOL,
        "hwenv_pool_workers",
        (performance_cores, logical_cores, prefer_performance, reserve, cap),
        _valid_positive_int,
        lambda: python_law(performance_cores, logical_cores, prefer_performance, reserve, cap),
    )
    if adopted is None:
        return python_law(performance_cores, logical_cores, prefer_performance, reserve, cap)
    return adopted


def route_library_thread_budget(
    physical_cores: int,
    workers: int,
    python_law: Callable[[int, int], int],
) -> int:
    """The routed ``library_thread_budget`` — every path returns the budget."""
    if hwenv_rust_disabled() or not _eligible_library(physical_cores, workers):
        return python_law(physical_cores, workers)
    adopted = _adopt_crate_value(
        _LAW_LIBRARY,
        "hwenv_library_thread_budget",
        (physical_cores, workers),
        _valid_positive_int,
        lambda: python_law(physical_cores, workers),
    )
    if adopted is None:
        return python_law(physical_cores, workers)
    return adopted


def route_memory_budget(
    detected_ram_bytes: int,
    fraction: float,
    floor_bytes: int,
    workers: int,
    python_law: Callable[[int, float, int, int], int],
) -> int:
    """The routed ``memory_budget_bytes`` post-override arm — every path
    returns the resolved budget (sentinel → floor EXACTLY; detected →
    derived, never floor-clamped).
    """
    if hwenv_rust_disabled() or not _eligible_memory(
        detected_ram_bytes, fraction, floor_bytes, workers
    ):
        return python_law(detected_ram_bytes, fraction, floor_bytes, workers)
    adopted = _adopt_crate_value(
        _LAW_MEMORY,
        "hwenv_memory_budget",
        (detected_ram_bytes, fraction, floor_bytes, workers),
        _valid_positive_int,
        lambda: python_law(detected_ram_bytes, fraction, floor_bytes, workers),
    )
    if adopted is None:
        return python_law(detected_ram_bytes, fraction, floor_bytes, workers)
    return adopted


def route_hardware_key(
    model_slug: str,
    physical_cores: int,
    performance_cores: int,
    efficiency_cores: int,
    total_ram_gib: int,
    arch: str,
    python_law: Callable[[str, int, int, int, int, str], str],
) -> str:
    """The routed ``hardware_key`` render — every path returns the key string."""
    if hwenv_rust_disabled() or not _eligible_key(
        model_slug, physical_cores, performance_cores, efficiency_cores, total_ram_gib, arch
    ):
        return python_law(
            model_slug, physical_cores, performance_cores, efficiency_cores, total_ram_gib, arch
        )
    adopted = _adopt_crate_value(
        _LAW_KEY,
        "hwenv_hardware_key",
        (model_slug, physical_cores, performance_cores, efficiency_cores, total_ram_gib, arch),
        _valid_key_str,
        lambda: python_law(
            model_slug, physical_cores, performance_cores, efficiency_cores, total_ram_gib, arch
        ),
    )
    if adopted is None:
        return python_law(
            model_slug, physical_cores, performance_cores, efficiency_cores, total_ram_gib, arch
        )
    return adopted


__all__ = [
    "BURN_IN_SAMPLES",
    "HWENV_RUST_ENV",
    "hwenv_rust_disabled",
    "hwenv_rust_enabled",
    "reset_hwenv_rust_for_tests",
    "route_hardware_key",
    "route_library_thread_budget",
    "route_memory_budget",
    "route_pool_workers",
]
