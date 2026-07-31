"""Flag-gated Rust seam for the backup DECISION laws — the ops-kernel facade.

DEC-BACKUP-RUST-OPS-001; default flipped ON by
DEC-RUST-DEFAULT-ON-WAVE-001 (operator directive 2026-07-23). Unless
``BRUTEX_BACKUP_RUST`` is set explicitly falsy (the kill switch), the pure
decision laws of :mod:`brutex.backup` route through the
``brutex_ops`` crate (``rust/brutex-ops``): the dead-man verdict state
machine (:func:`brutex.backup.deadman.evaluate_deadman`), the four
golden-pinned template renderers (:mod:`brutex.backup.schedule_render`), and
the restic ``--json`` parse + argv laws (:mod:`brutex.backup.restic_runner`).
This module owns the flag, the fail-closed layers and the process-local
latches; the crate side lives in ``rust/brutex-ops/src/backup.rs``.

THE HOUSE DECISION-KERNEL SPLIT (the ``DEC-FEED-RUST-CORE-001`` shape): Rust
DECIDES on plain data (arm codes, verdict tokens, id/time strings, argv
lists); Python keeps ALL I/O, subprocess, alerting, datetime parsing, the
error taxonomy and every logged/raised/alerted string. The retained Python
implementations are the LIVE PARITY ORACLE — never deleted here (oracle
retirement is its own later lane), and every degrade path serves them.

## The flag — ``BRUTEX_BACKUP_RUST`` (DEFAULT ON — DEC-RUST-DEFAULT-ON-WAVE-001)

Resolved per call (spawn workers read their own inherited environment): a
truthy literal (``1/true/yes/on``) → ON; a falsy literal
(``0/false/no/off``) → OFF (the kill switch); unset / whitespace-empty → ON
(the default per DEC-RUST-DEFAULT-ON-WAVE-001);
any OTHER value → OFF with one loud ``backup_rust.fallback`` WARN (reason
``flag_unrecognised``) per process. The vocabulary is a LOCAL twin of the
sweep flags' :func:`brutex.engine.sweep_lookahead.env_flag_tristate`
(equality-pinned in the unit suite, never imported — the engine package
hard-imports the ``fno_math`` wheel, and a backup check must never require
the ENGINE wheel; the ``lake_rust_render`` local-twin precedent).

## Fail-closed layers (all process-local; flag OFF touches NONE of them)

1. **Import + API-surface guard** — ``brutex_ops`` is resolved lazily on the
   first engaged call; an absent/stale/foreign install latches
   ``crate_unavailable`` (the :data:`_CRATE_API` symbol handshake is
   all-or-nothing) and every call serves the Python oracle.
2. **Per-LAW burn-in self-check + autorevert** — the first
   :data:`BURN_IN_SAMPLES` engaged calls per law ALSO run the Python oracle
   and compare full OUTCOMES (value, or the raised
   :class:`~brutex.backup.errors.BackupError`'s type + message; the deadman
   law bit-compares its floats via ``struct.pack`` through a caller-supplied
   key). ANY difference logs ``backup_rust.autoreverted`` (ERROR), SHIPS THE
   ORACLE OUTCOME, and latches the seam OFF for the process (ONE shared
   latch across all laws — a mismatch anywhere is evidence the install is
   broken; the ``lake_rust_render`` shared-latch doctrine).
3. **Decided-degrade codes (the abstain contract)** — a crate decision whose
   Python spelling embeds a CPython repr / ``json`` error message, or any
   input serde parses differently than CPython's ``json`` (``NaN`` /
   ``Infinity`` literals, lone surrogates, ``\\x1c``-class whitespace),
   returns the ORACLE code and the facade serves the Python oracle verbatim
   (:data:`USE_ORACLE`) — behavior byte-identical for EVERY input by
   construction, no warn (a decided arm, not a failure).
4. **Degrade set + pyo3 panic belt** — a crate raise serves the oracle with
   one ``backup_rust.fallback`` WARN; a ``pyo3_runtime.PanicException``
   latches ``crate_panic``; ``KeyboardInterrupt``/``SystemExit``/
   ``MemoryError`` re-raise RAW (the ``errors.py`` escape-set doctrine).
   The catch sets are IMPORTED from
   :mod:`brutex.data.lake_rust_render`'s public stateless re-exports
   (``degrade_errors`` / ``panic_belt_errors`` — §11 COMMON; the
   per-facade LATCH state stays per-facade by that module's own doctrine).
5. **Refuse-poison gates** — the law wrappers type-check every crate output
   before it is served; a wrong-typed/inconsistent decision raises into the
   degrade set and serves the oracle (one WARN), never a new crash class.

DELIBERATE DEVIATION (disclosed): this facade is a per-facade copy of the
house engaged-call machinery rather than a
:class:`brutex.config.seam_core.ConfigRustSeam` instance — that core is
single-VALUE-shaped (its ``adopt`` compares crate values directly) and
hardcodes the ``brutex_core`` crate module, while these laws need
THUNK-shaped outcomes (a raised ``BackupError`` IS a decided outcome) and
the :data:`USE_ORACLE` abstain arm that must SKIP the burn-in compare (a
serde-dialect abstain during burn-in must not read as a mismatch). Folding
this facade onto a generalised ``seam_core`` is a named follow-up in
MIGRATION.md (DEC-BACKUP-RUST-OPS-001).

## Honest envelope (Golden Rule #10)

Expected payoff ≈ 0 — the conversion is for uniformity, not speed (the
ADR-0020 banner, mandatory verbatim): every routed law runs at
once-per-backup-run / once-per-hourly-tick / once-per-drill frequency, and
the engaged path ADDS an env probe + an FFI hop + (during burn-in) a full
oracle recompute per call. The explicit-falsy kill switch is byte-identical
AND crate-untouched (bomb-module spy-pinned); the default is ON per
DEC-RUST-DEFAULT-ON-WAVE-001 (operator directive 2026-07-23).
The latches + warn memo are the same *documented exception* to the
no-global-mutable-state rule as ``rust_metrics``'s (plain unlocked writes —
a racing thread can at worst run an extra burn-in compare or duplicate a
warn, never serve a wrong value).
"""

from __future__ import annotations

import importlib
import os
from typing import TYPE_CHECKING, Final, cast

import structlog

from brutex.backup.errors import BackupError
from brutex.config.rust_strict import refuse_if_strict
from brutex.data.lake_rust_render import degrade_errors, panic_belt_errors

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import ModuleType

_LOG = structlog.get_logger(__name__)

#: The env flag. DEFAULT ON per DEC-RUST-DEFAULT-ON-WAVE-001 (operator
#: directive 2026-07-23); explicit falsy is the kill switch back to the
#: Python-oracle path.
BACKUP_RUST_ENV: Final[str] = "BRUTEX_BACKUP_RUST"

#: Engaged calls compared against the Python oracle PER LAW before crate
#: outcomes ship un-oracled (the #1531/#1532 burn-in law).
BURN_IN_SAMPLES: Final[int] = 16

#: The sentinel a law wrapper's ``compute`` returns for a DECIDED
#: degrade-to-oracle arm (abstain / raise-shaped outcome) — the facade
#: serves the Python oracle verbatim, no warn, no burn-in compare.
USE_ORACLE: Final[object] = object()

#: Truthy env literals (case-insensitive, whitespace-stripped) — the LOCAL
#: twin of ``brutex.engine.sweep_lookahead._TRUTHY`` (module docstring;
#: equality-pinned in the unit suite).
_TRUTHY: Final[frozenset[str]] = frozenset({"1", "true", "yes", "on"})

#: Falsy env literals — the explicit kill-switch vocabulary (same twin).
_FALSY: Final[frozenset[str]] = frozenset({"0", "false", "no", "off"})

#: The crate API surface the import guard verifies — all-or-nothing: a
#: module that imports but lacks ANY of these is a stale/foreign install and
#: latches ``crate_unavailable``, never an ``AttributeError`` mid-check.
_CRATE_API: Final[tuple[str, ...]] = (
    "build_backup_argv",
    "evaluate_deadman_decision",
    "parse_backup_summary_snapshot_id",
    "parse_ls_file_paths",
    "parse_ls_snapshot_time_decision",
    "parse_snapshots_latest_decision",
    "render_backup_plist",
    "render_cron_lines",
    "render_deadman_plist",
    "render_drill_plist",
)

#: The latch: the disable reason once the Rust path is off for this process,
#: else ``None`` (the documented no-global-mutable-state exception).
_DISABLED_REASON: Final[list[str | None]] = [None]

#: The budget every lazily-seeded per-law burn-in cell starts from
#: (``BURN_IN_SAMPLES`` in production; the test reset seam re-points it).
_BURN_IN_DEFAULT: Final[list[int]] = [BURN_IN_SAMPLES]

#: Burn-in samples still to run this process, PER LAW (lazily seeded from
#: :data:`_BURN_IN_DEFAULT` on a law's first engaged call).
_BURN_IN_REMAINING: Final[dict[str, int]] = {}

#: Once-per-process-per-reason warn memo + the engaged-once latch.
#: LOG-VOLUME state ONLY: served outcomes never depend on it.
_OBSERVABILITY_MEMO: Final[set[str]] = set()

#: Memo key for the engaged-once INFO (namespaced apart from warn reasons).
_ENGAGED_KEY: Final[str] = "__engaged__"

#: A captured outcome: ``("ok", value)`` or ``("raise", BackupError)``.
_Outcome = tuple[str, object]


def env_flag_tristate(raw: str) -> bool | None:
    """Classify *raw* against the flag vocabulary (local twin — module docs).

    ``True`` for a :data:`_TRUTHY` literal, ``False`` for a :data:`_FALSY`
    literal, ``None`` for anything else (unset/empty AND unrecognised — the
    caller owns the default + warn policy). Byte-identical semantics to
    :func:`brutex.engine.sweep_lookahead.env_flag_tristate`, equality-pinned
    in the unit suite; NOT imported from there (module docstring).
    """
    value = raw.strip().lower()
    if value in _TRUTHY:
        return True
    if value in _FALSY:
        return False
    return None


def backup_rust_enabled() -> bool:
    """Resolve ``BRUTEX_BACKUP_RUST`` (default ON, junk fails loud-OFF).

    DEC-RUST-DEFAULT-ON-WAVE-001 (operator directive 2026-07-23):
    unset/blank → ON (the default); explicit falsy → OFF (the kill
    switch); junk → loud-OFF unchanged. Read per call (one ``os.environ``
    probe per routed law call — the once-per-run/tick call frequency makes
    it immaterial; caching would break the spawn-worker inherit contract).
    """
    raw = os.environ.get(BACKUP_RUST_ENV, "")
    verdict = env_flag_tristate(raw)
    if verdict is not None:
        return verdict
    if raw.strip():
        _warn_once("flag_unrecognised", value=repr(raw))
        return False
    return True


def backup_rust_disabled() -> bool:
    """``True`` iff a guard/autorevert latched the Rust path OFF this process."""
    return _DISABLED_REASON[0] is not None


def disabled_reason() -> str | None:
    """The latch reason (``None`` = live) — test/observability read."""
    return _DISABLED_REASON[0]


def burn_in_remaining(law: str) -> int:
    """The live burn-in budget for *law* (test/observability read)."""
    return _BURN_IN_REMAINING.get(law, _BURN_IN_DEFAULT[0])


def reset_backup_rust_for_tests(*, remaining: int = BURN_IN_SAMPLES) -> None:
    """Reset the latch + burn-in budgets + warn memo (test isolation ONLY).

    ``remaining=0`` puts the process in the post-burn-in steady state for
    EVERY law — crate outcomes ship with no oracle in the path (the parity
    legs depend on that). The default restores a fresh-process view. Never
    called from production paths (the ``rust_metrics`` reset-seam
    convention).
    """
    _DISABLED_REASON[0] = None
    _BURN_IN_DEFAULT[0] = remaining
    _BURN_IN_REMAINING.clear()
    _OBSERVABILITY_MEMO.clear()


def _warn_once(reason: str, **context: object) -> None:
    """Refuse under strict mode; else log ``backup_rust.fallback`` warn-once.

    DEC-RUST-STRICT-NO-FALLBACK-001. Every reason funnelled here is CLASS A
    (the crate is absent/stale/raised — ``flag_unrecognised``, ``law_error``)
    or CLASS B (the crate ran and its output was structurally refused —
    ``result_refused``); both refuse under ``BRUTEX_RUST_STRICT``, so the
    refusal is UNCONDITIONAL and this module carries no exemption set.

    There is NO class-C reason at this choke point. The one input-envelope
    arm — the :data:`USE_ORACLE` abstain contract (layer 3: a crate decision
    whose Python spelling embeds a CPython repr, or an input serde parses
    differently than CPython's ``json`` — ``NaN``/``Infinity`` literals,
    lone surrogates, ``\\x1c``-class whitespace) — returns the Python oracle
    from :func:`route_law` WITHOUT announcing, so those inputs still reach
    Python under strict mode exactly as they do today. That is deliberate:
    refusing there would break backup rendering on a legitimate input; the
    honest fix is widening the crate until the abstain is unreachable.
    """
    refuse_if_strict("brutex.backup.backup_rust", reason, **context)
    if reason in _OBSERVABILITY_MEMO:
        return
    _OBSERVABILITY_MEMO.add(reason)
    _LOG.warning("backup_rust.fallback", reason=reason, **context)


def _disable(reason: str, *, error: bool = False, **context: object) -> None:
    """Refuse under strict mode; else latch the Rust path OFF for the process.

    ``error=True`` logs ``backup_rust.autoreverted`` at ERROR (the burn-in
    autorevert — a genuine wrongness signal); guard latches log
    ``backup_rust.rust_path_disabled`` at WARN. Repeat calls are no-ops.

    DEC-RUST-STRICT-NO-FALLBACK-001: reasons here are CLASS A
    (``crate_unavailable``, ``crate_panic``) or CLASS B
    (``burn_in_mismatch`` — the crate DISAGREED with the oracle), so the
    refusal is UNCONDITIONAL. It fires BEFORE the latch is written, so under
    strict mode the latch can never be set and the latched early-return in
    :func:`route_law` is structurally unreachable — wiring these two
    functions therefore covers every announced fall-back this facade takes.
    """
    refuse_if_strict("brutex.backup.backup_rust", reason, error=error, **context)
    if _DISABLED_REASON[0] is not None:
        return
    _DISABLED_REASON[0] = reason
    if error:
        _LOG.error("backup_rust.autoreverted", reason=reason, **context)
    else:
        _LOG.warning("backup_rust.rust_path_disabled", reason=reason, **context)


def _log_engaged_once(crate: ModuleType) -> None:
    """Log ``backup_rust.engaged`` INFO once per process (the wired-ON signal)."""
    if _ENGAGED_KEY in _OBSERVABILITY_MEMO:
        return
    _OBSERVABILITY_MEMO.add(_ENGAGED_KEY)
    _LOG.info("backup_rust.engaged", crate_version=getattr(crate, "__version__", None))


def _resolve_crate() -> ModuleType | None:
    """Import guard + SYMBOL handshake: ``brutex_ops``, or latch OFF + ``None``.

    An absent wheel (``ImportError`` — including the shim's own stale-wheel
    handshake refusal) latches ``crate_unavailable``; a module that imports
    but lacks any callable :data:`_CRATE_API` member (foreign / hand-broken
    / stale) latches the SAME reason — fail-closed, never an
    ``AttributeError`` out of this seam.
    """
    try:
        crate = importlib.import_module("brutex_ops")
    except ImportError as exc:
        _disable(
            "crate_unavailable",
            error_detail=repr(exc),
            remedy="maturin develop --release in rust/brutex-ops (project venv active)",
        )
        return None
    for name in _CRATE_API:
        if not callable(getattr(crate, name, None)):
            _disable(
                "crate_unavailable",
                error_detail=f"brutex_ops.{name} missing or not callable — stale/foreign install",
                remedy="maturin develop --release in rust/brutex-ops (project venv active)",
            )
            return None
    return crate


def default_outcome_key(outcome: _Outcome) -> object:
    """The burn-in comparison key for an outcome (exact-equality default).

    A raised :class:`BackupError` compares by exception type name + message
    (the decided-raise arms are byte-identical by construction — both arms
    run the SAME Python expressions); values compare by ``==``. Laws whose
    values carry floats (the deadman report) pass their own bit-packing key.
    """
    kind, payload = outcome
    if kind == "raise":
        return (kind, type(payload).__name__, str(payload))
    return outcome


def _capture_oracle[T](oracle: Callable[[], T]) -> _Outcome:
    """Run *oracle* capturing its value or its decided :class:`BackupError`."""
    try:
        return ("ok", oracle())
    except BackupError as exc:
        return ("raise", exc)


def _capture_rust[T](thunk: Callable[[], T], law: str) -> _Outcome | None:
    """Run a crate-outcome *thunk*; ``None`` = poisoned (serve the oracle).

    A :class:`BackupError` is the DECIDED outcome (captured); any
    degrade-set raise marks a wrong-typed/inconsistent crate output — the
    refuse-poison gate (one ``result_refused`` WARN, oracle served).
    ``KeyboardInterrupt``/``SystemExit``/``MemoryError`` propagate raw
    (never caught here).
    """
    try:
        return ("ok", thunk())
    except BackupError as exc:
        return ("raise", exc)
    except degrade_errors() as exc:
        _warn_once("result_refused", law=law, error_detail=repr(exc))
        return None


def _release(outcome: _Outcome) -> object:
    """Materialise a captured outcome — return the value or re-raise."""
    kind, payload = outcome
    if kind == "raise":
        raise cast("BackupError", payload)
    return payload


def route_law[T](
    law: str,
    *,
    oracle: Callable[[], T],
    compute: Callable[[ModuleType], object],
    outcome_key: Callable[[_Outcome], object] = default_outcome_key,
) -> T:
    """Route one law call: Rust when ON + healthy, the Python oracle otherwise.

    Parameters
    ----------
    law
        The law token (keys the per-law burn-in budget + warn context).
    oracle
        The verbatim Python implementation — may raise :class:`BackupError`
        as its decided outcome. Serves every OFF/latched/degraded call.
    compute
        Given the resolved crate module, performs the crate call + decision
        mapping and returns either a zero-arg THUNK producing the final
        value (which may raise :class:`BackupError` as the decided outcome)
        or :data:`USE_ORACLE` (a decided degrade arm — oracle served
        verbatim, no warn, no burn-in compare).
    outcome_key
        Burn-in comparison key over captured outcomes
        (:func:`default_outcome_key` unless the law's values carry floats).
    """
    if not backup_rust_enabled() or _DISABLED_REASON[0] is not None:
        return oracle()
    crate = _resolve_crate()
    if crate is None:
        return oracle()
    try:
        outcome_thunk = compute(crate)
    except degrade_errors() as exc:
        _warn_once("law_error", law=law, error_detail=repr(exc))
        return oracle()
    except (KeyboardInterrupt, SystemExit, MemoryError):
        # The escape set (the errors.py doctrine): operator interrupt /
        # interpreter shutdown / OOM stay global — never a silent fallback.
        # Sits ABOVE the panic belt so its `Exception` member can never
        # swallow a MemoryError.
        raise
    except panic_belt_errors() as exc:
        # A panic escaping the crate's FFI boundary (or any
        # out-of-degrade-set Exception) marks a broken contract — latch OFF
        # and serve the oracle, never a new crash class into a backup path.
        # PANIC-PROFILE RULING (the brutex-ops unification, superseding the
        # review NIT-1 note): the unified crate builds release with the
        # cargo-default UNWIND (NO `panic = "abort"`), so a genuine runtime
        # Rust panic DOES reach this belt as a catchable PanicException and
        # degrades to the oracle — the belt is LIVE for both Rust panics
        # and out-of-degrade-set Python-side exceptions. The kernel is
        # still written structurally panic-free (no unwrap/expect/unchecked
        # indexing — verified); the belt is defense-in-depth.
        _disable("crate_panic", law=law, error_detail=repr(exc))
        return oracle()
    if outcome_thunk is USE_ORACLE:
        # A DECIDED degrade arm (abstain / raise-shaped decision code) —
        # byte-identical by construction; no warn, no burn-in compare.
        return oracle()
    _log_engaged_once(crate)
    thunk = cast("Callable[[], T]", outcome_thunk)
    rust_outcome = _capture_rust(thunk, law)
    if rust_outcome is None:
        return oracle()
    remaining = _BURN_IN_REMAINING.setdefault(law, _BURN_IN_DEFAULT[0])
    if remaining <= 0:
        return cast("T", _release(rust_outcome))
    oracle_outcome = _capture_oracle(oracle)
    if outcome_key(rust_outcome) != outcome_key(oracle_outcome):
        _disable(
            "burn_in_mismatch",
            error=True,
            law=law,
            rust_kind=rust_outcome[0],
            oracle_kind=oracle_outcome[0],
        )
        return cast("T", _release(oracle_outcome))
    _BURN_IN_REMAINING[law] = remaining - 1
    if remaining == 1:
        _LOG.info("backup_rust.burn_in_complete", law=law, samples=BURN_IN_SAMPLES)
    return cast("T", _release(rust_outcome))


__all__ = [
    "BACKUP_RUST_ENV",
    "BURN_IN_SAMPLES",
    "USE_ORACLE",
    "backup_rust_disabled",
    "backup_rust_enabled",
    "burn_in_remaining",
    "default_outcome_key",
    "disabled_reason",
    "env_flag_tristate",
    "reset_backup_rust_for_tests",
    "route_law",
]
