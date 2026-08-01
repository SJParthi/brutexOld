"""Flag-gated Rust merge/dedup DECISION seam — the ADR-0020 PR-3 facade.

DEC-LAKE-RUST-MERGE-DEDUP-001 (MIGRATION.md #1911 — allocator-final). When —
and ONLY when — ``BRUTEX_LAKE_RUST_MERGE`` is set truthy,
:meth:`ParquetWriter._merge_write` routes its timestamp-keyed merge FOLD
DECISION through the ``brutex_lake`` crate's ``merge_decision_from_ipc``:
the stored partition's (keep-first collapsed) bars and the incoming batch
cross the FFI as two Arrow-IPC frames of the always-17-column decision
schema (``parquet_writer._decision_frame``), and the crate returns ONE
action code per batch row (``0=skip · 1=insert · 2=overwrite ·
3=conflict``). Python materializes EVERYTHING from those integers — every
``CandleBar``/``BarConflict`` object, the ``added``/``rewrite`` counters,
the sort, the tripwire and the render stay Python — so partition bytes /
``raw_hash`` / ``WriteResult`` fields are identical BY CONSTRUCTION and no
float ever crosses BACK over the FFI (the DEC-FEED-RUST-CORE-001
decision-kernel shape, not a byte-renderer). This module owns the flag, the
fail-closed layers and the process-local latches; the crate side lives in
``rust/brutex-lake/src/merge.rs``.

## The flag — ``BRUTEX_LAKE_RUST_MERGE`` (DEFAULT ON per DEC-RUST-DEFAULT-ON-WAVE-001)

Resolved per call through the SHARED classifier
(:func:`brutex.data.lake_rust_render.env_flag_tristate` — imported, never
copied): truthy → ON; falsy → the silent OFF kill switch; unset /
whitespace-empty → ON (the default); any OTHER value → OFF with one loud
``lake_rust_merge.fallback`` WARN (reason ``flag_unrecognised``) per
process — the wave law (DEC-RUST-DEFAULT-ON-WAVE-001).

## Fail-closed layers (the PR-1 pattern, per-facade latch state)

1. **Import guard** — the crate resolves lazily on first engaged merge; an
   absent/broken ``brutex_lake`` (or one missing any
   :data:`_MERGE_CRATE_API` callable — e.g. a PR-1-era wheel predating the
   merge kernel) latches ``crate_unavailable``; a raising metadata
   pyfunction latches ``crate_metadata_error``. DELIBERATE deviation from
   the handoff sketch (disclosed): this facade owns its OWN surface check
   instead of extending ``lake_rust_render._CRATE_API`` — coupling the
   RENDER seam's engagement to the merge kernel's presence would regress
   PR-1 operators on the pre-merge wheel, and keeping the tuple here keeps
   ``lake_rust_render.py`` byte-stable for concurrently-authored sibling
   seams.
2. **Version guard** — the installed ``polars.__version__`` must equal the
   crate's ``pinned_polars_version()``. A decision kernel is
   polars-version-AGNOSTIC (it reads IPC and compares values — no encoder
   bytes in the surface), but the handshake is KEPT deliberately (the
   handoff's documented choice): one lockstep law for the whole wheel is
   simpler to operate than per-function exemptions, and a version-drifted
   wheel is exactly the stale-install shape the guards exist to refuse.
3. **Burn-in self-check + autorevert** — the first
   :data:`~brutex.data.lake_rust_render.BURN_IN_SAMPLES` engaged merges per
   process ALSO run the verbatim Python fold on a COPY of the stored dict
   and bit-compare the outcomes (winner-object IDENTITY per timestamp +
   ``added`` + ``rewrite`` + per-conflict object identity). A mismatch logs
   ``lake_rust_merge.merge_autoreverted`` (ERROR), SHIPS THE PYTHON RESULT
   and latches OFF (the #1531/#1532/#1892 pattern). The count budget is
   EXTENDED with a shape gate keyed on ``(merge_mode, stored-has-Greeks,
   batch-has-Greeks)`` — every distinct comparison-arm combination is
   bit-compared at least once before undoubled Rust decisions ship for it
   (bounded ≤ 8 tokens by construction).
4. **Refuse-poison gate** — a decision payload with the wrong length, a
   non-int / unknown action code, or an action that contradicts the fold
   law (an INSERT at a stored timestamp; an OVERWRITE/CONFLICT/SKIP at an
   absent one; an OVERWRITE under keep-first; a CONFLICT under latest-wins)
   is REFUSED wholesale: the work copy is discarded, one
   ``lake_rust_merge.fallback`` WARN (reason ``decision_refused``) fires,
   and the verbatim Python fold serves the result. The ORIGINAL ``merged``
   dict is never touched until a decision is fully validated — a poisoned
   crate can only degrade, never corrupt.

A crate/IPC error degrades to the Python fold with one warn-once
(``merge_error``); an escaped ``pyo3`` panic latches ``crate_panic`` via
the SHARED narrow panic belt
(:func:`brutex.data.lake_rust_render.panic_belt_errors`);
``KeyboardInterrupt``/``SystemExit``/``MemoryError`` re-raise immediately —
the caller holds ``lake_write_guard`` around this call, so an OOM anywhere
in here maps to ``LakeWriteHalt("out_of_memory")`` exactly as on the
pure-Python path (DEC-LAKE-MERGE-OOM-HALT-001 — the seam sits strictly
INSIDE the guarded fold block).

## Log events (all literal — greppable)

* ``lake_rust_merge.fallback`` (WARN, once per process per ``reason`` ∈
  {``flag_unrecognised``, ``merge_error``, ``decision_refused``}).
* ``lake_rust_merge.rust_path_disabled`` (WARN, once — ``reason`` ∈
  {``crate_unavailable``, ``crate_metadata_error``,
  ``polars_version_mismatch``, ``crate_panic``}).
* ``lake_rust_merge.merge_autoreverted`` (ERROR, once) — the burn-in
  mismatch; Python result shipped, latch set.
* ``lake_rust_merge.engaged`` (INFO, once) — the first Rust-served decision.
* ``lake_rust_merge.burn_in_complete`` (INFO, once).

## Honest envelope (Golden Rule #10)

* **Expected payoff ≈ 0 — the conversion is for uniformity, not speed**
  (ADR-0020 §0): the engaged path pays TWO decision-frame builds + two
  uncompressed Arrow-IPC serialisations + one FFI crossing per merge, then
  re-materializes the fold in Python anyway; during burn-in it ALSO runs
  the verbatim Python fold. Flag OFF (the default) pays one env probe and
  is byte-identical (never-routes spy-pinned).
* The engaged materialization works on a SHALLOW COPY of the stored dict
  (bars are frozen dataclasses), so a refused/poisoned decision leaves the
  caller's dict untouched; the guard-failed and flag-OFF paths mutate it
  in place exactly like today (zero extra copies).
* Value comparisons run on the f64s the decision frame carries (the SAME
  ``float(Decimal)`` conversion the render frame uses; values arrive
  2dp-snapped and the kernel never re-rounds). Two DISTINCT 2dp prices
  alias in f64 only above ~2.2e13 — outside any admissible price — and the
  burn-in bit-compare autoreverts the class regardless (the kernel-side
  honesty note in ``rust/brutex-lake/src/merge.rs`` carries the arithmetic).
* The latches + warn-once memo are the same *documented exception* to the
  no-global-mutable-state rule as the PR-1 facade's: plain unlocked writes —
  a racing writer thread can at worst run an extra burn-in compare or emit
  a duplicate warn, never a wrong object (any mismatch ships the Python
  result).
* :func:`reset_lake_rust_merge_for_tests` restores a fresh process view —
  test isolation only, never called from production paths.
"""

from __future__ import annotations

import importlib
import os
from typing import TYPE_CHECKING, Final, Protocol

import structlog

from brutex.config.rust_strict import refuse_if_strict
from brutex.data.lake_rust_render import (
    BURN_IN_SAMPLES,
    degrade_errors,
    env_flag_tristate,
    panic_belt_errors,
)
from brutex.data.models import CandleBar, FnoGreeksBar

if TYPE_CHECKING:  # pragma: no cover - import only for static typing
    from collections.abc import Callable
    from datetime import datetime
    from types import ModuleType

    from brutex.data.parquet_writer import BarConflict

_LOG = structlog.get_logger("brutex.data.lake_rust_merge")

#: The env flag. DEFAULT ON per DEC-RUST-DEFAULT-ON-WAVE-001 (operator
#: directive 2026-07-23) — the engaged Rust fold is guarded by burn-in +
#: autorevert + degrade-to-oracle; the explicit-falsy value is the kill
#: switch.
LAKE_RUST_MERGE_ENV: Final[str] = "BRUTEX_LAKE_RUST_MERGE"

#: Action-code vocabulary — MUST mirror ``rust/brutex-lake/src/merge.rs``
#: (``ACTION_*`` there; the crate-side constants test pins the values, and
#: the golden parity corpus exercises the pairing end-to-end).
ACTION_SKIP: Final[int] = 0
ACTION_INSERT: Final[int] = 1
ACTION_OVERWRITE: Final[int] = 2
ACTION_CONFLICT: Final[int] = 3

_VALID_ACTIONS: Final[frozenset[int]] = frozenset(
    {ACTION_SKIP, ACTION_INSERT, ACTION_OVERWRITE, ACTION_CONFLICT}
)

#: The crate API surface the import guard verifies — this facade's OWN
#: check (module docstring layer 1): a PR-1-era wheel that lacks the merge
#: kernel latches ``crate_unavailable`` HERE without touching the render
#: seam's engagement.
_MERGE_CRATE_API: Final[tuple[str, ...]] = (
    "merge_decision_from_ipc",
    "pinned_polars_version",
    "linked_zstd_version",
)

#: The latch (the documented no-global-mutable-state exception — module
#: docstring; the PR-1 single-cell shape).
_DISABLED_REASON: Final[list[str | None]] = [None]

#: Burn-in samples still to run this process.
_BURN_IN_REMAINING: Final[list[int]] = [BURN_IN_SAMPLES]

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
#: Cleared by :func:`reset_lake_rust_merge_for_tests` — a test that swaps
#: ``sys.modules['brutex_lake']`` must reset, or it is served the crate
#: resolved before its swap.
_CRATE_MEMO: Final[list[ModuleType | None]] = [None]

#: Shape tokens already bit-compared this process (mode × overlay arms —
#: bounded ≤ 8 by construction, module docstring layer 3).
_BURN_IN_SHAPES_SEEN: Final[set[str]] = set()

#: Whether the shape gate is armed (disarmed together with a zeroed count
#: budget by the test reset seam so ``remaining=0`` means steady state).
_BURN_IN_SHAPE_GATE: Final[list[bool]] = [True]

#: Once-per-process-per-reason warn memo + the engaged-once latch.
_OBSERVABILITY_MEMO: Final[set[str]] = set()

#: Memo key for the engaged-once INFO (namespaced apart from warn reasons).
_ENGAGED_KEY: Final[str] = "__engaged__"

#: Sentinel for "the metadata call raised" (the PR-1 shape — distinct from
#: any value a foreign crate could return).
_METADATA_ERROR: Final[object] = object()


class PythonFold(Protocol):
    """The verbatim Python merge fold (``ParquetWriter._merge_fold``).

    Mutates ``merged`` IN PLACE and returns ``(added, rewrite, conflicts)``
    — the exact seam contract the flag-OFF branch of ``_merge_write`` calls
    directly.
    """

    def __call__(
        self,
        merged: dict[datetime, CandleBar],
        new_bars: list[CandleBar],
        *,
        merge_mode: str,
    ) -> tuple[int, bool, list[BarConflict]]:
        """Fold *new_bars* into *merged*; return ``(added, rewrite, conflicts)``."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed


class _DecisionRefused(Exception):
    """Internal: the crate's action payload failed the refuse-poison gate."""


def lake_rust_merge_enabled() -> bool:
    """Resolve ``BRUTEX_LAKE_RUST_MERGE`` (default ON per
    DEC-RUST-DEFAULT-ON-WAVE-001, operator directive 2026-07-23; junk fails
    loud-OFF).

    The flag law (via the SHARED
    :func:`~brutex.data.lake_rust_render.env_flag_tristate`): truthy → ON;
    falsy → OFF (the kill switch); unset / whitespace-empty → ON (the
    default); any other non-empty value → OFF with one loud
    ``lake_rust_merge.fallback`` WARN (reason ``flag_unrecognised``) per
    process. Read per call so parallel workers resolve their own inherited
    environment.
    """
    raw = os.environ.get(LAKE_RUST_MERGE_ENV, "")
    verdict = env_flag_tristate(raw)
    if verdict is not None:
        return verdict
    if raw.strip():
        _warn_once("flag_unrecognised", value=raw)
        return False
    return True


def rust_merge_disabled() -> bool:
    """Return ``True`` iff a guard/autorevert latched the Rust path OFF this process."""
    return _DISABLED_REASON[0] is not None


def reset_lake_rust_merge_for_tests(*, remaining: int = BURN_IN_SAMPLES) -> None:
    """Reset the latch + burn-in budget + shape gate + warn memo (test isolation ONLY).

    ``remaining=0`` puts the process in the post-burn-in steady state —
    count budget spent AND the shape gate disarmed, so the returned fold is
    the crate decision's with no oracle in the path (the parity corpus
    depends on that). The default restores a fresh-process view. Never
    called from production code paths (the PR-1 reset-seam convention).
    """
    _DISABLED_REASON[0] = None
    _BURN_IN_REMAINING[0] = remaining
    _BURN_IN_SHAPES_SEEN.clear()
    _BURN_IN_SHAPE_GATE[0] = remaining > 0
    _OBSERVABILITY_MEMO.clear()
    _CRATE_MEMO[0] = None


def _warn_once(reason: str, **context: object) -> None:
    """Log ``lake_rust_merge.fallback`` WARN once per process per *reason*.

    DEC-RUST-STRICT-NO-FALLBACK-001: refuses under ``BRUTEX_RUST_STRICT``.
    Every reason funnelled here is CLASS A (``flag_unrecognised`` — the
    junk-flag arm; ``merge_error`` — the crate raised, or polars is absent
    for the version probe) or CLASS B (``decision_refused`` — the layer-4
    refuse-poison rejected the crate's action list). There is NO class-C
    reason at this choke point, so no exemption set: the seam takes a bar
    dict + a bar list the crate always claims — the merge kernel declares no
    input envelope it can decline. Strict-OFF is byte-identical to the prior
    behaviour.

    STORED-BYTE SAFETY: this facade decides a MERGE, it renders nothing. A
    refusal propagates out of ``_merge_write``'s guarded block with the
    caller's dict untouched (the Rust arm works on a COPY — ``work``), so no
    partition is written at all; a stored byte is never rewritten, only
    absent.
    """
    refuse_if_strict("brutex.data.lake_rust_merge", reason, **context)
    if reason in _OBSERVABILITY_MEMO:
        return
    _OBSERVABILITY_MEMO.add(reason)
    _LOG.warning("lake_rust_merge.fallback", reason=reason, **context)


def _disable(reason: str, *, error: bool = False, **context: object) -> None:
    """Latch the Rust merge path OFF for the remainder of the process.

    ``error=True`` logs ``lake_rust_merge.merge_autoreverted`` at ERROR (the
    burn-in autorevert); guard latches log
    ``lake_rust_merge.rust_path_disabled`` at WARN. Repeats are no-ops.

    DEC-RUST-STRICT-NO-FALLBACK-001: refuses under ``BRUTEX_RUST_STRICT``.
    Reasons here are CLASS A (``crate_unavailable`` /
    ``crate_metadata_error`` / ``polars_version_mismatch`` / ``crate_panic``)
    or CLASS B (``burn_in_fold_mismatch`` — the crate's fold disagreed with
    the oracle's); none is class C, so none is exempt. The refusal fires
    BEFORE the latch is written, so under strict mode the latch is never set
    and every ``rust_merge_disabled()`` early-return is structurally
    unreachable. Strict-OFF is byte-identical to the prior behaviour.
    """
    refuse_if_strict("brutex.data.lake_rust_merge", reason, error=error, **context)
    if _DISABLED_REASON[0] is not None:
        return
    _DISABLED_REASON[0] = reason
    if error:
        _LOG.error("lake_rust_merge.merge_autoreverted", reason=reason, **context)
    else:
        _LOG.warning("lake_rust_merge.rust_path_disabled", reason=reason, **context)


def _resolve_crate() -> ModuleType | None:
    """Import guard (layer 1): return ``brutex_lake``, or latch OFF + ``None``.

    The PR-1 shape against :data:`_MERGE_CRATE_API` — a PR-1-era wheel
    missing ``merge_decision_from_ipc`` latches ``crate_unavailable`` here
    (rebuild remedy named) while the render seam keeps engaging on its own
    check (module docstring layer 1).
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
    for name in _MERGE_CRATE_API:
        if not callable(getattr(crate, name, None)):
            _disable(
                "crate_unavailable",
                error_detail=(
                    f"brutex_lake.{name} missing or not callable — foreign/broken/pre-PR-3 install"
                ),
                remedy="pip install rust/brutex-lake (see docs/runbooks/lake-rust-render-lockstep.md)",
            )
            return None
    _CRATE_MEMO[0] = crate
    return crate


def _crate_metadata(crate: ModuleType, attr: str) -> object:
    """Call a crate metadata pyfunction defensively; latch OFF on ANY raise.

    The PR-1 contract verbatim: ``MemoryError`` re-raises bare (OOM stays a
    global stop riding ``lake_write_guard`` → ``LakeWriteHalt``); any other
    raise latches ``crate_metadata_error`` and the caller serves the Python
    fold. Returned values are un-typechecked — a non-str pin fails the
    ``!=`` version guard, fail-closed already.
    """
    try:
        return getattr(crate, attr)()
    except MemoryError:
        raise
    except Exception as exc:
        _disable("crate_metadata_error", metadata_call=attr, error_detail=repr(exc))
        return _METADATA_ERROR


def _shape_token(
    merged: dict[datetime, CandleBar], new_bars: list[CandleBar], merge_mode: str
) -> str:
    """The burn-in shape-gate key: mode × per-side Greeks-overlay presence.

    Keys the comparison ARMS the kernel exercises (keep-first vs
    latest-wins; plain vs overlay-bearing sides) — every distinct
    combination is bit-compared at least once (module docstring layer 3).
    O(bars) ``isinstance`` scans, short-circuiting; paid only on the
    flag-ON path.
    """
    stored_greeks = any(isinstance(bar, FnoGreeksBar) for bar in merged.values())
    batch_greeks = any(isinstance(bar, FnoGreeksBar) for bar in new_bars)
    return f"{merge_mode}|stored_greeks={stored_greeks}|batch_greeks={batch_greeks}"


def _apply_actions(
    work: dict[datetime, CandleBar],
    new_bars: list[CandleBar],
    actions: object,
    *,
    keep_first: bool,
) -> tuple[int, bool, list[BarConflict]]:
    """Materialize the crate's action codes into *work* (a COPY of the stored dict).

    The refuse-poison gate (module docstring layer 4): every violation
    raises :class:`_DecisionRefused` — the caller discards *work* and runs
    the verbatim Python fold on the untouched original. In keep-first mode a
    winner is NEVER replaced after its first assignment, so
    ``work[bar.timestamp]`` at conflict-record time IS the fold-time
    ``prev`` (the stored bar, or the earlier batch bar that inserted) —
    object-identical to what the Python fold records.
    """
    from brutex.data.parquet_writer import BarConflict

    if not isinstance(actions, list) or len(actions) != len(new_bars):
        raise _DecisionRefused(
            f"decision payload shape refused: {type(actions).__name__} of length "
            f"{len(actions) if isinstance(actions, list) else 'n/a'} for {len(new_bars)} batch bars"
        )
    added = 0
    rewrite = False
    conflicts: list[BarConflict] = []
    for bar, action in zip(new_bars, actions, strict=True):
        if not isinstance(action, int) or action not in _VALID_ACTIONS:
            raise _DecisionRefused(f"unknown action code {action!r}")
        present = bar.timestamp in work
        if action == ACTION_INSERT:
            if present:
                raise _DecisionRefused("INSERT at an already-stored timestamp")
            work[bar.timestamp] = bar
            added += 1
        elif action == ACTION_OVERWRITE:
            if keep_first or not present:
                raise _DecisionRefused("OVERWRITE outside the latest-wins law")
            work[bar.timestamp] = bar
            rewrite = True
        elif action == ACTION_CONFLICT:
            if not keep_first or not present:
                raise _DecisionRefused("CONFLICT outside the keep-first law")
            conflicts.append(
                BarConflict(timestamp=bar.timestamp, stored=work[bar.timestamp], vendor=bar)
            )
        elif not present:
            # ACTION_SKIP at an absent timestamp contradicts the fold law
            # (a skip means "a winner already holds this timestamp").
            raise _DecisionRefused("SKIP at an absent timestamp")
    return added, rewrite, conflicts


def _fold_results_equal(
    rust: tuple[dict[datetime, CandleBar], int, bool, list[BarConflict]],
    oracle: tuple[dict[datetime, CandleBar], int, bool, list[BarConflict]],
) -> bool:
    """Bit-strict burn-in compare: winner IDENTITY per timestamp + counters.

    Object identity (``is``) rather than value equality — both
    materializations pick from the SAME bar-object pools (the stored dict's
    values and ``new_bars``), so identity is exact and strictly stronger
    than ``==`` (it distinguishes a stored bar from an equal-valued batch
    bar — the keep-first-vs-latest-wins witness a value compare would miss).
    """
    rust_merged, rust_added, rust_rewrite, rust_conflicts = rust
    oracle_merged, oracle_added, oracle_rewrite, oracle_conflicts = oracle
    if (
        rust_added != oracle_added
        or rust_rewrite != oracle_rewrite
        or len(rust_conflicts) != len(oracle_conflicts)
        or rust_merged.keys() != oracle_merged.keys()
    ):
        return False
    if any(rust_merged[ts] is not oracle_merged[ts] for ts in oracle_merged):
        return False
    return all(
        r.timestamp == o.timestamp and r.stored is o.stored and r.vendor is o.vendor
        for r, o in zip(rust_conflicts, oracle_conflicts, strict=True)
    )


def merge_fold(
    merged: dict[datetime, CandleBar],
    new_bars: list[CandleBar],
    *,
    merge_mode: str,
    python_fold: PythonFold,
    decision_ipc: Callable[[list[CandleBar]], bytes],
) -> tuple[dict[datetime, CandleBar], int, bool, list[BarConflict]]:
    """The routed merge fold — called by ``_merge_write`` ONLY when the flag is ON.

    Returns ``(merged, added, rewrite, conflicts)`` — the dict to sort and
    render plus the fold outcome. EVERY path returns a correct fold: a
    latched / guarded / degraded / refused call runs *python_fold* on the
    caller's dict IN PLACE (byte-identical to the flag-OFF branch); a
    burn-in mismatch ships the PYTHON result; only a guard-clean,
    burn-in-clean call ships the Rust-decided materialization (built on a
    shallow COPY, so the caller's dict is never half-mutated). The caller
    holds ``lake_write_guard`` around this call, so a ``MemoryError``
    anywhere in here maps to ``LakeWriteHalt`` exactly as on the
    pure-Python path.

    Parameters
    ----------
    merged
        The stored partition's keep-first-collapsed bars keyed by timestamp
        (``_read_existing_bars``' output) — mutated in place ONLY on the
        Python-fold paths.
    new_bars
        The incoming batch, sorted by timestamp (RAW — within-batch
        duplicates are part of the fold law).
    merge_mode
        ``"keep-first"`` (default law) or ``"latest-wins"`` (the derived
        ``1day`` restate law). Anything non-``"latest-wins"`` folds
        keep-first, exactly like the Python fold's ``==`` test.
    python_fold
        The verbatim Python fold (``ParquetWriter._merge_fold``) — the live
        oracle for every degrade path and the burn-in compare.
    decision_ipc
        Serialiser from a bar list to the 17-column decision frame's
        uncompressed Arrow-IPC bytes
        (``lambda bars: _ipc_bytes(_decision_frame(bars))`` at the one
        production call site) — injected so this leaf never imports polars.
    """

    def _python_result() -> tuple[dict[datetime, CandleBar], int, bool, list[BarConflict]]:
        added, rewrite, conflicts = python_fold(merged, new_bars, merge_mode=merge_mode)
        return merged, added, rewrite, conflicts

    if rust_merge_disabled():
        return _python_result()
    crate = _resolve_crate()
    if crate is None:
        return _python_result()
    pinned = _crate_metadata(crate, "pinned_polars_version")
    if pinned is _METADATA_ERROR:
        return _python_result()
    try:
        installed_polars = _installed_polars_version()
    except ImportError as exc:
        # Structurally unreachable on the production path (`_merge_write`
        # just read the stored partition through polars) — a defensive belt
        # for a bare-environment direct call: degrade, never a crash class.
        _warn_once("merge_error", error_detail=repr(exc))
        return _python_result()
    if pinned != installed_polars:
        # Layer 2 — the version guard (kept deliberately for a
        # version-agnostic decision kernel; module docstring layer 2).
        _disable(
            "polars_version_mismatch",
            installed_polars=installed_polars,
            crate_pinned_polars=pinned,
            remedy="align the wheel and the crate's pinned polars rev, then "
            "re-run the parity corpus "
            "(docs/runbooks/lake-rust-render-lockstep.md)",
        )
        return _python_result()
    keep_first = merge_mode != "latest-wins"
    try:
        actions = crate.merge_decision_from_ipc(
            decision_ipc(list(merged.values())),
            decision_ipc(new_bars),
            keep_first,
        )
    except degrade_errors() as exc:
        _warn_once("merge_error", error_detail=repr(exc))
        return _python_result()
    except (KeyboardInterrupt, SystemExit, MemoryError):
        # The escape set (the errors.py doctrine): OOM must reach the
        # caller's lake_write_guard → LakeWriteHalt, never a fallback. Sits
        # ABOVE the panic belt so its `Exception` member never swallows one.
        raise
    except panic_belt_errors() as exc:
        # An escaped pyo3 PanicException (BaseException) — latch + serve the
        # Python fold (the PR-1 belt verbatim; a foreign BaseException the
        # resolver cannot name PROPAGATES, the disclosed narrowing).
        _disable("crate_panic", error_detail=repr(exc))
        return _python_result()
    work = dict(merged)
    try:
        added, rewrite, conflicts = _apply_actions(work, new_bars, actions, keep_first=keep_first)
    except _DecisionRefused as exc:
        # Refuse-poison (layer 4): the work COPY is discarded — the caller's
        # dict is untouched, the verbatim Python fold serves the result.
        _warn_once("decision_refused", error_detail=str(exc))
        return _python_result()
    if _ENGAGED_KEY not in _OBSERVABILITY_MEMO:
        linked_zstd = _crate_metadata(crate, "linked_zstd_version")
        if linked_zstd is _METADATA_ERROR:
            return _python_result()
        _OBSERVABILITY_MEMO.add(_ENGAGED_KEY)
        _LOG.info(
            "lake_rust_merge.engaged",
            crate_pinned_polars=pinned,
            linked_zstd=linked_zstd,
        )
    token = _shape_token(merged, new_bars, merge_mode)
    shape_unseen = _BURN_IN_SHAPE_GATE[0] and token not in _BURN_IN_SHAPES_SEEN
    if _BURN_IN_REMAINING[0] > 0 or shape_unseen:
        # Layer 3 — burn-in self-check (module docstring): the verbatim
        # Python fold runs on ITS OWN copy; ANY divergence ships the Python
        # result and autoreverts the Rust path for the process.
        oracle_merged = dict(merged)
        oracle_added, oracle_rewrite, oracle_conflicts = python_fold(
            oracle_merged, new_bars, merge_mode=merge_mode
        )
        oracle = (oracle_merged, oracle_added, oracle_rewrite, oracle_conflicts)
        if not _fold_results_equal((work, added, rewrite, conflicts), oracle):
            _disable(
                "burn_in_fold_mismatch",
                error=True,
                rust_added=added,
                python_added=oracle_added,
                rust_rewrite=rewrite,
                python_rewrite=oracle_rewrite,
                rust_conflicts=len(conflicts),
                python_conflicts=len(oracle_conflicts),
                shape_token=token,
            )
            return oracle
        _BURN_IN_SHAPES_SEEN.add(token)
        if _BURN_IN_REMAINING[0] > 0:
            _BURN_IN_REMAINING[0] -= 1
            if _BURN_IN_REMAINING[0] == 0:
                _LOG.info("lake_rust_merge.burn_in_complete", samples=BURN_IN_SAMPLES)
    return work, added, rewrite, conflicts


def _installed_polars_version() -> str:
    """The installed ``polars.__version__`` (lazy — this leaf never imports
    polars at module scope; in practice polars is already loaded when the
    seam runs, `_merge_write` having just read the stored partition)."""
    import polars as pl

    return pl.__version__


__all__ = [
    "ACTION_CONFLICT",
    "ACTION_INSERT",
    "ACTION_OVERWRITE",
    "ACTION_SKIP",
    "LAKE_RUST_MERGE_ENV",
    "PythonFold",
    "lake_rust_merge_enabled",
    "merge_fold",
    "reset_lake_rust_merge_for_tests",
    "rust_merge_disabled",
]
