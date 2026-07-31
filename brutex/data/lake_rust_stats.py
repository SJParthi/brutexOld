"""Default-ON Rust seam for the stats-manifest shard render (ADR-0020 Wave C).

DEC-LAKE-RUST-STATS-MANIFEST-001 (MIGRATION.md #1914). The ONE routed
surface is :func:`brutex.data.lake_stats_manifest.render_shard` — when
``BRUTEX_LAKE_RUST_STATS_MANIFEST`` is ON it delegates the canonical shard
JSON assembly to ``brutex_lake.render_stats_shard_json`` (the
`rust/brutex-lake` crate's CPython ``json.dumps`` byte-law twin) through
this facade; the flag-OFF default is the verbatim stdlib ``json.dumps``,
byte-identical AND crate-untouched (bomb-crate spy-pinned).

The PR-1 :mod:`brutex.data.lake_rust_render` pattern, with ONE deliberate
structural difference: **no version guard.** PR-1's layer 2 exists because
its crate links a third-party polars whose bytes drift across releases;
this seam's crate side is a pure std-Rust encoder locked to the *stdlib*
``json.dumps`` byte law — there is no wheel↔crate lockstep to attest, so a
version guard would attest nothing. The burn-in bit-compare (layer 3) +
the frozen cross-language golden are the drift teeth instead.

Fail-closed layers, in order (every path returns the PYTHON string):

1. the caller-side flag gate (:func:`lake_rust_stats_enabled` — default
   OFF; junk values loud-OFF once per process);
2. the import/API-surface guard (:func:`_resolve_crate` — absent wheel or
   a foreign install missing the callable latches ``crate_unavailable``);
3. the burn-in self-check — the first :data:`BURN_IN_SAMPLES` engaged
   renders compute BOTH arms and bit-compare the two strings; ANY
   difference ships the ORACLE string and autoreverts the Rust path for
   the process (the #1531/#1532 pattern);
4. the degrade set (:data:`_DEGRADE_ERRORS` + the pyo3 panic belt) — a
   crate raise serves the oracle with one WARN, never a new crash class.

``MemoryError`` re-raises raw: the production caller is
``lake_stats_manifest._write_shard`` inside the writers' best-effort
partition-locked hooks, whose upstream catch set
(``lake_stats_manifest._BEST_EFFORT_ERRORS``) deliberately EXCLUDES
``MemoryError`` so an OOM stays a global stop (the review-round-4 law
there) — this seam must not launder one into a fallback.

Ints-only law: the shard schema carries NO floats; a float smuggled into
an int field fails PyO3 extraction (``TypeError``/``OverflowError``) and
degrades to the oracle. A lone-surrogate ``str`` (which CPython would
escape as ``\\udXXX``) cannot cross PyO3 — ``UnicodeEncodeError`` (⊂
``ValueError``) degrades likewise; both parity-pinned.
"""

from __future__ import annotations

import importlib
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import structlog

from brutex.config.rust_strict import refuse_if_strict
from brutex.data.lake_rust_render import env_flag_tristate

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import ModuleType

_LOG = structlog.get_logger(__name__)

#: The env flag (default ON per DEC-RUST-DEFAULT-ON-WAVE-001; the
#: explicit-falsy value is the kill switch, junk fails loud-OFF).
LAKE_RUST_STATS_ENV: Final[str] = "BRUTEX_LAKE_RUST_STATS_MANIFEST"

#: Engaged renders bit-compared against the stdlib oracle before crate
#: strings ship un-oracled (the #1531/#1532 burn-in law).
BURN_IN_SAMPLES: Final[int] = 16

#: The process-local autorevert latch — ``[None]`` = live; a reason string
#: latches the Rust path OFF for the remainder of the process.
_DISABLED_REASON: Final[list[str | None]] = [None]

#: Remaining burn-in budget (mutable cell; reset seam below).
_BURN_IN_REMAINING: Final[list[int]] = [BURN_IN_SAMPLES]

#: Warn-once registry (per-process, test-resettable — a sticky reason
#: would flood the logs at one shard write per partition write).
_OBSERVABILITY_MEMO: Final[set[str]] = set()

#: The engaged-INFO memo key (never a warn reason).
_ENGAGED_KEY: Final[str] = "__engaged__"

#: The crate callables this seam requires (the API-surface guard).
_CRATE_API: Final[tuple[str, ...]] = ("render_stats_shard_json",)

#: The narrow degrade-to-oracle catch set. ``ValueError`` covers
#: ``UnicodeEncodeError`` (lone-surrogate extraction); ``TypeError``/
#: ``OverflowError`` are PyO3 argument-conversion failures (floats /
#: out-of-i64 ints); ``RuntimeError`` is the crate's declared panic
#: mapping; ``AttributeError`` is the broken-crate-internals belt.
#: ``MemoryError`` is DELIBERATELY absent (module docstring).
_DEGRADE_ERRORS: Final[tuple[type[Exception], ...]] = (
    ValueError,
    RuntimeError,
    TypeError,
    OverflowError,
    AttributeError,
)


@dataclass(frozen=True, slots=True)
class ShardRenderPayload:
    """The pre-formatted ints+strings-only payload crossing the FFI.

    Built by ``render_shard``'s Rust arm from the SAME ``DatasetStats`` the
    stdlib arm serializes — every timestamp is pre-formatted with CPython's
    own ``datetime.isoformat()`` (the ADR-0020 row-4 "isoformat formatter
    pinned both arms" satisfaction: both arms consume the identical
    strings, so no Rust re-derivation of the formatter exists). ``files``
    rows are ``(rel, rows, min_ts_iso, max_ts_iso, mtime_ns, size,
    raw_hash)`` 7-tuples, the crate's ``FileRowIn`` shape.
    """

    version: int
    kind: str
    key: tuple[tuple[str, str], ...]
    identity_key: tuple[str, ...]
    row_count: int
    min_ts_iso: str
    max_ts_iso: str
    files: tuple[tuple[str, int, str, str, int, int, str | None], ...]
    updated_utc: str


def lake_rust_stats_enabled() -> bool:
    """Resolve ``BRUTEX_LAKE_RUST_STATS_MANIFEST`` (default ON per
    DEC-RUST-DEFAULT-ON-WAVE-001, operator directive 2026-07-23; junk
    loud-OFF).

    Truthy → ``True``; falsy → ``False`` (the silent explicit kill switch);
    unset / whitespace-empty → ``True`` (the default); any OTHER non-empty
    value → ``False`` with one ``lake_rust_stats.fallback`` WARN (reason
    ``flag_unrecognised``) per process. Read per call so spawn workers
    resolve their own inherited environment (the PR-1 posture).
    """
    raw = os.environ.get(LAKE_RUST_STATS_ENV, "")
    verdict = env_flag_tristate(raw)
    if verdict is not None:
        return verdict
    if raw.strip():
        _warn_once("flag_unrecognised", value=raw)
        return False
    return True


def rust_stats_disabled() -> bool:
    """``True`` iff a guard/autorevert latched the Rust path OFF this process."""
    return _DISABLED_REASON[0] is not None


def reset_lake_rust_stats_for_tests(*, remaining: int = BURN_IN_SAMPLES) -> None:
    """Reset the latch + burn-in budget + warn memo (test isolation ONLY).

    ``remaining=0`` puts the process in the post-burn-in steady state so
    the returned string is the crate's with no oracle in the path (the
    parity corpus depends on that — an armed burn-in would cushion a
    divergence by shipping oracle bytes). Never called from production
    paths (the PR-1 reset-seam convention).
    """
    _DISABLED_REASON[0] = None
    _BURN_IN_REMAINING[0] = remaining
    _OBSERVABILITY_MEMO.clear()


#: The ONE class-C reason at this choke point: the payload carries a
#: legitimate Python value pyo3 cannot represent (an int outside ``i64``, a
#: lone-surrogate ``str``), classified by :func:`_beyond_crate` BEFORE the
#: crate is called. The stdlib oracle was always the intended handler for
#: these, so refusing would drop a shard write over an input the Rust arm
#: never claimed — see that function for the pyo3 evidence.
_STRICT_EXEMPT_REASONS: Final[frozenset[str]] = frozenset({"render_input_beyond_crate"})


def _warn_once(reason: str, **context: object) -> None:
    """Log ``lake_rust_stats.fallback`` WARN once per process per *reason*.

    DEC-RUST-STRICT-NO-FALLBACK-001: refuses under ``BRUTEX_RUST_STRICT``.
    Every reason funnelled here is CLASS A (``flag_unrecognised`` — the
    junk-flag arm; ``render_error`` — the crate ran and raised) or CLASS B
    (``render_result_refused`` — a non-``str`` crate return), EXCEPT the
    single class-C member of :data:`_STRICT_EXEMPT_REASONS`
    (``render_input_beyond_crate``), which is decided by a Python-side gate
    before the FFI so ``render_error`` keeps its fault-only meaning.
    Strict-OFF is byte-identical to the prior behaviour.

    STORED-BYTE SAFETY: the shard body this renders is written by the
    caller's own ``atomic_write_simple``, and every hook is best-effort — a
    refusal removes a shard WRITE, it never rewrites a shard byte. The shard
    is a read-accelerator whose absence degrades to the legacy live scan
    (DEC-LAKE-STATS-MANIFEST-001), never to a wrong number.
    """
    if reason not in _STRICT_EXEMPT_REASONS:
        refuse_if_strict("brutex.data.lake_rust_stats", reason, **context)
    if reason in _OBSERVABILITY_MEMO:
        return
    _OBSERVABILITY_MEMO.add(reason)
    _LOG.warning("lake_rust_stats.fallback", reason=reason, **context)


def _disable(reason: str, *, error: bool = False, **context: object) -> None:
    """Latch the Rust path OFF for the remainder of the process.

    ``error=True`` logs ``lake_rust_stats.render_autoreverted`` at ERROR
    (the burn-in autorevert — a genuine wrongness signal); guard latches
    log ``lake_rust_stats.rust_path_disabled`` at WARN. Repeats are no-ops.

    DEC-RUST-STRICT-NO-FALLBACK-001: refuses under ``BRUTEX_RUST_STRICT``.
    Reasons here are CLASS A (``crate_unavailable`` — absent/foreign install;
    ``crate_panic``) or CLASS B (``burn_in_byte_mismatch`` — the crate's
    shard body disagreed with the stdlib oracle's); none is class C, so none
    is exempt. The refusal fires BEFORE the latch is written, so under strict
    mode the latch is never set and every ``rust_stats_disabled()``
    early-return is structurally unreachable. Strict-OFF is byte-identical
    to the prior behaviour.
    """
    refuse_if_strict("brutex.data.lake_rust_stats", reason, error=error, **context)
    if _DISABLED_REASON[0] is not None:
        return
    _DISABLED_REASON[0] = reason
    if error:
        _LOG.error("lake_rust_stats.render_autoreverted", reason=reason, **context)
    else:
        _LOG.warning("lake_rust_stats.rust_path_disabled", reason=reason, **context)


def _resolve_crate() -> ModuleType | None:
    """Import guard: return ``brutex_lake``, or latch OFF + ``None``.

    An absent wheel latches ``crate_unavailable`` with one loud WARN naming
    the remedy; a module that imports but lacks a callable
    :data:`_CRATE_API` member (a PRE-stats-shard wheel, a name collision, a
    hand-broken install) latches the SAME reason — an ``AttributeError``
    must never leave this seam (the PR-1 fail-closed contract).
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
                "crate_unavailable",
                error_detail=f"brutex_lake.{name} missing or not callable — stale/foreign install",
                remedy="pip install rust/brutex-lake (see docs/runbooks/lake-rust-render-lockstep.md)",
            )
            return None
    return crate


def _panic_belt_errors() -> tuple[type[BaseException], ...]:
    """The narrow FFI panic-belt catch set — the ``lake_rust_render`` twin.

    ``pyo3_runtime.PanicException`` is the one ``BaseException`` subclass a
    pyo3 boundary can leak; the repo convention (CodeQL
    ``py/catch-base-exception``) forbids a non-re-raising literal ``except
    BaseException``, so the class is resolved GUARDEDLY by name from its
    two plausible homes and folded beside ``Exception`` (see
    ``lake_rust_render._panic_belt_errors`` for the full doctrine — a
    LOCAL twin because that helper is private to its module; the homes +
    semantics are equality-pinned in the unit suite). The crate's own
    ``catch_unwind`` maps panics to ``RuntimeError`` (the degrade set), so
    this belt is defense-in-depth over a broken boundary.
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


#: The crate's integer envelope: every ``FileRowIn`` / header int is
#: extracted as a Rust ``i64``, so a Python ``int`` outside this range is
#: unrepresentable ACROSS THE FFI — not a crate fault.
_I64_MIN: Final = -(2**63)
_I64_MAX: Final = 2**63 - 1


def _beyond_crate(payload: ShardRenderPayload) -> str | None:
    """Name the first payload field OUTSIDE the crate's FFI envelope, else ``None``.

    DEC-RUST-STRICT-NO-FALLBACK-001 CLASS C. Two legitimate Python values
    cannot cross pyo3 at all, and in BOTH cases the stdlib oracle is the
    intended handler rather than a symptom of a broken crate:

    * an ``int`` outside ``i64`` (a filesystem can report an ``mtime_ns``
      of ``2**63``; Python ints are unbounded, Rust's are not) — pyo3
      raises ``TypeError("failed to extract field FileRowIn.4")``;
    * a ``str`` carrying a lone surrogate (``"bad\\ud800rel"`` — legal in a
      Python ``str``, illegal in Rust's UTF-8 ``String``) — pyo3 raises a
      ``UnicodeEncodeError``.

    Both previously arrived at the ``render_error`` choke point, which is a
    fault-only CLASS A token and therefore REFUSES under strict mode — so a
    lake with one such file would have lost its shard write instead of
    falling back to the byte-identical CPython render. This gate classifies
    them BEFORE the call, so ``render_error`` keeps its honest meaning (the
    crate ran and broke) and the out-of-envelope input takes the exempt
    ``render_input_beyond_crate`` reason.

    Returns the offending field name (for the WARN context) so the log says
    WHICH value was out of range, never a bare "something was wrong".
    """

    def _bad_int(value: int, field: str) -> str | None:
        return field if not _I64_MIN <= value <= _I64_MAX else None

    def _bad_str(value: str, field: str) -> str | None:
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            return field
        return None

    checks: list[str | None] = [
        _bad_int(payload.version, "version"),
        _bad_int(payload.row_count, "row_count"),
        _bad_str(payload.kind, "kind"),
        _bad_str(payload.min_ts_iso, "min_ts_iso"),
        _bad_str(payload.max_ts_iso, "max_ts_iso"),
        _bad_str(payload.updated_utc, "updated_utc"),
    ]
    for index, (name, value) in enumerate(payload.key):
        checks.append(_bad_str(name, f"key[{index}].name"))
        checks.append(_bad_str(value, f"key[{index}].value"))
    for index, ident in enumerate(payload.identity_key):
        checks.append(_bad_str(ident, f"identity_key[{index}]"))
    for index, row in enumerate(payload.files):
        rel, rows, row_min, row_max, mtime_ns, size, raw_hash = row
        checks.append(_bad_str(rel, f"files[{index}].rel"))
        checks.append(_bad_int(rows, f"files[{index}].rows"))
        checks.append(_bad_str(row_min, f"files[{index}].min_ts_iso"))
        checks.append(_bad_str(row_max, f"files[{index}].max_ts_iso"))
        checks.append(_bad_int(mtime_ns, f"files[{index}].mtime_ns"))
        checks.append(_bad_int(size, f"files[{index}].size"))
        if raw_hash is not None:
            checks.append(_bad_str(raw_hash, f"files[{index}].raw_hash"))
    for field in checks:
        if field is not None:
            return field
    return None


def render_shard_json(
    *,
    payload_factory: Callable[[], ShardRenderPayload],
    python_render: Callable[[], str],
) -> str:
    """The routed shard render — called by ``render_shard`` ONLY when the flag is ON.

    Runs the fail-closed layers (module docstring) and returns the shard
    body to persist. EVERY path returns a valid canonical string: a
    latched / guarded / degraded call returns ``python_render()`` (the
    live stdlib oracle), a burn-in mismatch returns the ORACLE string, and
    only a guard-clean, burn-in-clean call returns the Rust string.

    Parameters
    ----------
    payload_factory
        Zero-arg callable building the :class:`ShardRenderPayload` (the
        isoformat pre-formatting) — a callable so a latched process never
        pays that construction.
    python_render
        Zero-arg callable running the verbatim stdlib ``json.dumps``
        render — the live parity oracle.
    """
    if rust_stats_disabled():
        return python_render()
    crate = _resolve_crate()
    if crate is None:
        return python_render()
    try:
        payload = payload_factory()
        beyond = _beyond_crate(payload)
        if beyond is not None:
            # CLASS C: a legitimate value pyo3 cannot carry. The oracle is
            # the intended handler, so this is exempt from the strict
            # refusal — see _beyond_crate + _STRICT_EXEMPT_REASONS.
            _warn_once("render_input_beyond_crate", field=beyond)
            return python_render()
        rust_body = crate.render_stats_shard_json(
            payload.version,
            payload.kind,
            payload.key,
            payload.identity_key,
            payload.row_count,
            payload.min_ts_iso,
            payload.max_ts_iso,
            payload.files,
            payload.updated_utc,
        )
    except _DEGRADE_ERRORS as exc:
        _warn_once("render_error", error_detail=repr(exc))
        return python_render()
    except (KeyboardInterrupt, SystemExit, MemoryError):
        # The escape set (the errors.py doctrine): operator interrupt /
        # interpreter shutdown / OOM stay global — MemoryError in
        # particular must reach the write hook's OWN policy (it is
        # deliberately OUTSIDE _BEST_EFFORT_ERRORS there), never a
        # fallback. Sits ABOVE the panic belt so its `Exception` member
        # can never swallow an OOM.
        raise
    except _panic_belt_errors() as exc:
        # A panic escaping the crate's catch_unwind boundary (or any
        # out-of-degrade-set Exception) marks a broken FFI contract —
        # latch OFF and serve the oracle, never a new crash class into
        # the partition-locked write hook.
        _disable("crate_panic", error_detail=repr(exc))
        return python_render()
    if not isinstance(rust_body, str):
        # Refuse-poison gate: a non-str return is a crate BUG — never
        # persisted. Structurally unreachable through the real crate
        # (its FFI constructs a PyString); guards a stale/foreign install.
        _warn_once("render_result_refused", result_type=type(rust_body).__name__)
        return python_render()
    if _ENGAGED_KEY not in _OBSERVABILITY_MEMO:
        _OBSERVABILITY_MEMO.add(_ENGAGED_KEY)
        _LOG.info("lake_rust_stats.engaged")
    if _BURN_IN_REMAINING[0] > 0:
        oracle_body = python_render()
        if rust_body != oracle_body:
            _disable(
                "burn_in_byte_mismatch",
                error=True,
                rust_len=len(rust_body),
                python_len=len(oracle_body),
            )
            return oracle_body
        _BURN_IN_REMAINING[0] -= 1
        if _BURN_IN_REMAINING[0] == 0:
            _LOG.info("lake_rust_stats.burn_in_complete", samples=BURN_IN_SAMPLES)
        return rust_body
    return rust_body


__all__ = [
    "BURN_IN_SAMPLES",
    "LAKE_RUST_STATS_ENV",
    "ShardRenderPayload",
    "lake_rust_stats_enabled",
    "render_shard_json",
    "reset_lake_rust_stats_for_tests",
    "rust_stats_disabled",
]
