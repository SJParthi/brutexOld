"""The Rust Wave-D seam — the REQUIRED heal/scan/read/reconcile crate facade.

The ONE shared facade for the Wave-D lake conversion
(``timeframe_materialize`` → ``lake_heal`` → ``catalog_reconcile`` →
``lake_invariants``, in the ruled runbook order). Since the
DEC-RUST-LAKE-WAVED-001 ENDGAME (W-6a) the ``brutex_lake`` crate's
``waved_*`` pyfunctions are the ONLY implementation of the four converted
surfaces — the Python cores (the read collect body, the per-finding heal
algorithms, the per-file reconcile scan, the whole-lake invariant scan, the
content-token walk) are DELETED, so this module's job is no longer
flag-routing/burn-in/degrade-to-oracle but the fail-LOUD preconditions +
the wire validation + the Python-side replays (Wave-D house laws i-iii:
structlog, injected seams and exception TYPES stay Python).

## The endgame posture (Wave-D design §D, binding)

1. The burn-in / oracle / degrade machinery is REMOVED (there is no oracle
   to compare against or fall back to).
2. The guards are FAIL-LOUD preconditions: an absent wheel, a wheel missing
   any :data:`_CRATE_API` callable (a pre-endgame install — the
   ``waved_endgame_witness`` name is the lockstep tooth), or a polars
   version drift raises the typed
   :class:`~brutex.data.errors.LakeRustWavedUnavailableError` naming the
   rebuild runbook — never silent, never a fabricated verdict
   (``brutex_lake`` is a REQUIRED wheel for the lake heal/scan surface,
   the ``fno_math``-for-engine precedent).
3. The flag is RETIRED: a SET ``$BRUTEX_LAKE_WAVED_RUST`` (any non-empty
   value, ``1``/``0`` included) refuses loudly via
   :func:`require_waved_env_unset` (the ``$BRUTEX_FILL_MODE`` retirement
   precedent, DEC-FILL-WORST-CASE-ONLY-001).
4. Run-time fault posture per surface mirrors each module's documented
   isolation law (the ruled endgame semantics — out-of-envelope shapes are
   ISOLATED, never process-killing, never silent):

   * **read** (:func:`read_1m_via_rust`): ANY crate-call failure — a
     genuine gate violation (ts-range / price-sanity), a corrupt file, a
     crate panic — raises the typed quarantining
     :class:`~brutex.data.errors.ResampleInputError` (already in the
     prebuild's per-symbol quarantine set), message = the Rust rendering;
   * **heal** (:func:`heal_batch_via_rust`): the crate owns per-finding
     isolation (an out-of-envelope finding is a counted isolated error and
     the batch CONTINUES — the W-6a crate law); a crate-side disk-full
     crosses as a real ``OSError(ENOSPC)`` for the caller's
     ``lake_write_guard``; anything else out of the engaged call is LOUD
     (mutation may have happened — the idempotent-convergence posture);
   * **reconcile** (:func:`reconcile_file_via_rust`): ANY crate-call
     failure raises ``ValueError`` naming the file — a member of the
     caller's GAP-#9 skip set (``datetime_out_of_range_errors``), so the
     verdict is the pinned per-file ``catalog.reconcile.parquet_skip``;
   * **scan** (:func:`scan_lake_via_rust`): the crate converts every
     judged-unjudgeable shape into a LOUD counted
     ``lake.invariant.scan_skip`` on its own path; only the
     structurally-unreachable defensive belt still raises (propagated
     raw); a raising injected ``per_file_pause`` re-raises VERBATIM via
     the :class:`_PauseEscape` parking wrap;
   * **token** (:func:`lake_content_token_via_rust`): walk-skip events
     replay through the caller's pinned WARN sites; a crate failure
     propagates raw (the startup sweep's own guard set owns the
     never-crash-boot law).

5. A mis-shaped crate RETURN (refuse-poison — a foreign/stale install the
   name guard somehow missed) raises ``RuntimeError`` naming the remedy:
   with no oracle, guessing at a poisoned wire would fabricate verdicts.

## Cycle note (CodeQL py/cyclic-import — dispositioned, fix round)

``lake_invariants`` ⇄ this module is a DELIBERATE two-way FUNCTION-LOCAL
edge: ``scan_lake_invariants`` lazily imports :func:`scan_lake_via_rust`,
and this facade lazily imports the finding dataclasses /
``log_lake_finding`` back (plus a ``TYPE_CHECKING``-only block for the
annotations). There is NO import-time cycle — every runtime edge resolves
inside a call frame after both modules exist. Re-homing the finding
dataclasses into a leaf module was assessed and DECLINED: every external
consumer (census/CLI/doctor/tests) imports them from ``lake_invariants``,
so the move buys nothing the lazy edge does not already guarantee.

The per-process :data:`_LAST_SCAN_READS` slot (the §F2 read-count witness)
is the one piece of process-local state;
:func:`reset_lake_rust_waved_for_tests` clears it (tests only).
"""

from __future__ import annotations

import dataclasses
import importlib
import io
import json
import os
import time
from collections.abc import Collection, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Final, cast

import structlog

from brutex.data.errors import (
    LakeRustWavedFlagRemovedError,
    LakeRustWavedUnavailableError,
    ResampleInputError,
)

if TYPE_CHECKING:  # pragma: no cover - import only for static typing
    from collections.abc import Callable
    from types import ModuleType

    import polars as pl

    from brutex.data.lake_invariants import (
        ForeignMonthRows,
        IntraFileDup,
        LakeFinding,
        LakeScanReport,
        PartitionCoords,
    )

    #: The per-file reconcile scan wire ``(row_count, first_ts, last_ts,
    #: raw_hash, findings)`` — the W-3 seam's wire shape (``None`` = a
    #: zero-row file, the caller's counted skip).
    WavedFileScan = tuple[int, object, object, str, tuple["IntraFileDup | ForeignMonthRows", ...]]

#: The RETIRED env flag (DEC-RUST-LAKE-WAVED-001 endgame): a SET value —
#: any non-empty spelling — refuses loudly via
#: :func:`require_waved_env_unset`; the name survives only so the refusal
#: can cite the exact variable.
LAKE_WAVED_RUST_ENV: Final[str] = "BRUTEX_LAKE_WAVED_RUST"

#: The crate API surface the import guard verifies — ALL-OR-NOTHING over
#: every shipped waved conversion step. ``waved_endgame_witness`` is the
#: W-6a lockstep tooth: a pre-endgame wheel (whose heal batch could still
#: return a whole-batch degrade this shell no longer has an oracle for)
#: must latch :class:`LakeRustWavedUnavailableError`, never half-work.
_CRATE_API: Final[tuple[str, ...]] = (
    "pinned_polars_version",
    "waved_read_1m_ipc",
    "waved_has_stored_1m_bars",
    "waved_month_partition_files",
    "waved_heal_batch",
    "waved_lake_content_token",
    "waved_reconcile_file",
    "waved_scan_lake",
    "waved_heal_engaged_kinds",
    "waved_endgame_witness",
)

#: The rebuild remedy every unavailable-error names (the lockstep ritual).
_REMEDY: Final[str] = (
    "pip install rust/brutex-lake (see docs/runbooks/lake-rust-render-lockstep.md)"
)

#: The waved read result's tuple arity — ``(ipc_bytes, events_json)``.
_READ_RESULT_ARITY: Final[int] = 2

#: The event-payload keys every bad-tick warn record must carry (the crate
#: wire contract — see ``rust/brutex-lake/src/waved/mod.rs``).
_EVENT_KEYS: Final[frozenset[str]] = frozenset(
    {"session", "skipped", "max_ratio", "first_prev_close", "first_close"}
)


def require_waved_env_unset(env: Mapping[str, str] | None = None) -> None:
    """Refuse LOUDLY when the retired ``$BRUTEX_LAKE_WAVED_RUST`` flag is set.

    Fired by :func:`_require_crate` at every surface entry (exactly where
    the retired resolver used to read the env), so a stale config refuses
    before any lake work.

    Parameters
    ----------
    env
        The environment mapping to read (defaults to ``os.environ`` — the
        injection seam keeps the guard pure for tests).

    Raises
    ------
    LakeRustWavedFlagRemovedError
        When the variable is present with ANY non-empty value — ``1`` and
        ``0`` included: the seam is no longer flag-gated
        (DEC-RUST-LAKE-WAVED-001 endgame), and honouring either spelling
        would silently bless a stale config whose oracle/kill-switch
        intent no longer exists. An UNSET or empty-string variable passes
        (the ``VAR= cmd`` unset idiom — the ``$BRUTEX_FILL_MODE``
        precedent's exact semantics).
    """
    source = os.environ if env is None else env
    raw = source.get(LAKE_WAVED_RUST_ENV, "")
    if raw.strip():
        raise LakeRustWavedFlagRemovedError(
            f"${LAKE_WAVED_RUST_ENV}={raw!r} is set, but the Wave-D lake seam is "
            f"no longer flag-gated — the brutex_lake crate cores are the ONLY "
            f"implementation (DEC-RUST-LAKE-WAVED-001 endgame). Unset the "
            f"variable; the retired flag is refused for EVERY value, "
            f"including '1' and '0'."
        )


def _require_crate() -> ModuleType:
    """The fail-LOUD precondition chain: env unset → import → API → version.

    Returns the live ``brutex_lake`` module (resolved via
    ``importlib.import_module`` at USE time — never a collection-time
    binding, the ``_live_crate()`` law) or raises the typed
    :class:`LakeRustWavedUnavailableError` naming the exact failure + the
    rebuild remedy. ``MemoryError`` out of the metadata probe re-raises
    raw (a global stop, never re-labelled).
    """
    require_waved_env_unset()
    try:
        crate = importlib.import_module("brutex_lake")
    except ImportError as exc:
        raise LakeRustWavedUnavailableError(
            f"the REQUIRED brutex_lake crate wheel is not importable "
            f"({exc!r}) — the Wave-D Python cores are deleted "
            f"(DEC-RUST-LAKE-WAVED-001 endgame). Remedy: {_REMEDY}"
        ) from exc
    for name in _CRATE_API:
        if not callable(getattr(crate, name, None)):
            raise LakeRustWavedUnavailableError(
                f"brutex_lake.{name} is missing or not callable — a "
                f"foreign/stale (pre-endgame) install cannot serve the "
                f"Wave-D surfaces (DEC-RUST-LAKE-WAVED-001). Remedy: {_REMEDY}"
            )
    try:
        pinned = crate.pinned_polars_version()
    except MemoryError:
        raise
    except Exception as exc:
        raise LakeRustWavedUnavailableError(
            f"brutex_lake.pinned_polars_version() raised ({exc!r}) — a "
            f"foreign/stale install cannot be trusted. Remedy: {_REMEDY}"
        ) from exc
    import polars as pl

    if pinned != pl.__version__:
        # ADR-0020 §7 hazard 1: the waved surfaces decode frames via the
        # pinned polars — a drifted wheel must never serve (the lockstep is
        # LOAD-BEARING post-delete; risk register §G row 2).
        raise LakeRustWavedUnavailableError(
            f"installed polars {pl.__version__} != the crate's pinned "
            f"{pinned!r} — the wheel/crate lockstep is broken "
            f"(DEC-RUST-LAKE-WAVED-001 endgame). Remedy: {_REMEDY}"
        )
    return crate


def _refuse_poison(surface: str, detail: str) -> RuntimeError:
    """The refuse-poison error for a mis-shaped crate RETURN (loud, never guessed)."""
    return RuntimeError(
        f"brutex_lake {surface} returned a mis-shaped result ({detail}) — "
        f"refuse-poison: with the Python cores deleted there is no oracle to "
        f"serve, and guessing would fabricate verdicts. Remedy: {_REMEDY}"
    )


def _parse_bad_tick_events(events_json: str) -> list[dict[str, object]]:
    """Parse + shape-validate the crate's bad-tick events channel.

    Raises ``ValueError`` on ANY junk shape (non-JSON, a non-list root, a
    non-dict item, wrong keys, a non-str session, a bool-punned/non-int
    ``skipped``, a non-float ratio/close) — the caller converts that into
    the refuse-poison ``RuntimeError``. Empty list for ``"[]"``.
    """
    try:
        payload = json.loads(events_json)
    except json.JSONDecodeError as exc:
        raise ValueError(f"undecodable events payload: {exc}") from exc
    if not isinstance(payload, list):
        raise ValueError(f"events root must be a list, got {type(payload).__name__}")
    events: list[dict[str, object]] = []
    for item in payload:
        if not isinstance(item, dict) or set(item) != _EVENT_KEYS:
            raise ValueError("event item is not a bad-tick warn record")
        if not isinstance(item["session"], str):
            raise ValueError("event session must be a string")
        skipped = item["skipped"]
        if isinstance(skipped, bool) or not isinstance(skipped, int) or skipped < 1:
            raise ValueError("event skipped must be a positive int")
        for key in ("max_ratio", "first_prev_close", "first_close"):
            value = item[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"event {key} must be a number")
        events.append(item)
    return events


def _replay_bad_tick_events(events: list[dict[str, object]], *, warn_source: str | None) -> None:
    """Replay the crate's warn coordinates through the ONE emission seam.

    ``emit_bad_tick_warn`` is the SINGLE ``feed.bad_tick_skipped`` emission
    point (CLAUDE.md §11 COMMON — the ``rust/brutex-feed`` replay
    precedent), so event name + field names can never fork; the ratio is
    the EVENT's own (the crate wire carries it). Lazy import: this leaf
    stays polars-free + bad_tick-free at module scope.
    """
    from brutex.data.bad_tick import emit_bad_tick_warn

    for event in events:
        emit_bad_tick_warn(
            source=warn_source,
            session_iso=str(event["session"]),
            skipped=int(event["skipped"]),  # type: ignore[call-overload]
            max_ratio=float(event["max_ratio"]),  # type: ignore[arg-type]
            first_prev_close=float(event["first_prev_close"]),  # type: ignore[arg-type]
            first_close=float(event["first_close"]),  # type: ignore[arg-type]
        )


def read_1m_via_rust(
    *,
    files: Sequence[str] | None,
    tree_root: str | None,
    with_open_interest: bool,
    screen_enabled: bool,
    warn_source: str | None,
    context: str,
) -> pl.DataFrame:
    """The 1-minute read core — called by ``make_lake_read_1m`` (the ONE arm).

    The months / whole-tree / empty-partition SOURCE DECISION already ran
    in the caller (its pinned raises live there), so exactly one of *files*
    / *tree_root* is non-``None``. The crate runs collect →
    rename/select/sort → the ts-datetime-range gate → the price-sanity
    gate → the bad-tick screen and returns the frame + the per-session
    bad-tick warn coordinates, replayed here through the ONE
    ``emit_bad_tick_warn`` seam.

    Raises
    ------
    ResampleInputError
        ANY crate-call failure — a genuine gate violation, a corrupt or
        vanished file, a crate panic — with the Rust-rendered reason
        naming *context* (the ruled endgame semantics: the typed
        quarantining raise; the class is already in the prebuild's
        per-symbol quarantine set, so one poisoned symbol never aborts the
        run).
    RuntimeError
        A mis-shaped crate return (refuse-poison).
    LakeRustWavedUnavailableError, LakeRustWavedFlagRemovedError
        The precondition chain (module docstring).
    """
    crate = _require_crate()
    if (files is None) == (tree_root is None):
        raise _refuse_poison(
            "read_1m seam",
            f"exactly one of files/tree_root must be given "
            f"(files={files is not None}, tree_root={tree_root is not None})",
        )
    import polars as pl

    try:
        result = crate.waved_read_1m_ipc(
            [] if files is None else list(files),
            tree_root,
            with_open_interest,
            screen_enabled,
        )
    except (KeyboardInterrupt, SystemExit, MemoryError):
        # The escape set (the errors.py doctrine): operator interrupt /
        # interpreter shutdown / OOM stay global — never re-labelled.
        raise
    except _panic_belt_errors() as exc:
        # The ruled endgame semantics — the typed quarantining raise:
        # every crate failure class (gate refusal, corrupt file, panic)
        # isolates the SYMBOL at the caller's quarantine boundary.
        raise ResampleInputError(
            f"1minute read for {context} refused by the native lake core: {exc}"
        ) from exc
    if (
        not isinstance(result, tuple)
        or len(result) != _READ_RESULT_ARITY
        or not isinstance(result[0], bytes)
        or not isinstance(result[1], str)
    ):
        raise _refuse_poison("waved_read_1m_ipc", f"result type {type(result).__name__}")
    ipc_bytes, events_json = result
    # OSError joins the DECODE leg only (polars surfaces a truncated
    # in-memory IPC payload as an OSError). Named tuple expression — mypy
    # --strict rejects a starred-unpack tuple DISPLAY in an except clause.
    decode_errors: tuple[type[BaseException], ...] = (*_panic_belt_errors(), OSError)
    try:
        rust_df = pl.read_ipc(io.BytesIO(ipc_bytes))
    except decode_errors as exc:
        raise _refuse_poison("waved_read_1m_ipc", f"undecodable IPC payload: {exc!r}") from exc
    try:
        events = _parse_bad_tick_events(events_json)
    except ValueError as exc:
        raise _refuse_poison("waved_read_1m_ipc", f"poisoned events channel: {exc}") from exc
    _replay_bad_tick_events(events, warn_source=warn_source)
    return rust_df


def _panic_belt_errors() -> tuple[type[BaseException], ...]:
    """The FFI catch set — ``lake_rust_render``'s panic belt (one import)."""
    from brutex.data.lake_rust_render import panic_belt_errors

    return panic_belt_errors()


#: The heal outcome dict's exact key set (the crate wire contract — see
#: ``rust/brutex-lake/src/waved/heal.rs`` ``heal_batch_core``).
_HEAL_OUTCOME_KEYS: Final[frozenset[str]] = frozenset(
    {"healed", "skipped", "errors", "quarantined_files", "actions", "touched_minute"}
)

#: The token-skip event dict's exact key set (the crate wire contract).
_TOKEN_SKIP_KEYS: Final[frozenset[str]] = frozenset({"event", "path", "error"})

#: The waved token result's tuple arity — ``(token_hex, skip_events)``.
_TOKEN_RESULT_ARITY: Final[int] = 2


def heal_batch_via_rust(
    *,
    lake_root: str,
    quarantine_root: str,
    findings: list[dict[str, object]],
    on_event: Callable[[str, dict[str, object]], None],
) -> dict[str, object]:
    """The heal-batch mutation core — called by ``heal_findings`` (the ONE arm).

    The crate owns per-finding isolation since W-6a: an out-of-envelope
    finding is a counted isolated error (its ``lake_heal.partition_error``
    log replayed through *on_event*) and the batch CONTINUES — there is no
    whole-batch degrade and no Python fallback. Any exception out of the
    call is LOUD (mutation may have happened; the healer is
    idempotent-convergent, so the posture is the documented re-run next
    batch/boot). A crate-side disk-full crosses as a real
    ``OSError(ENOSPC)`` for the caller's ``lake_write_guard`` →
    ``LakeWriteHalt`` mapping; a crate panic crosses as the FFI boundary's
    ``RuntimeError``.

    Raises
    ------
    RuntimeError
        A crate panic, a mis-shaped outcome dict, or a stale-wheel
        ``{"degrade"}`` verdict (refuse-poison — loud, never guessed at:
        mutation may have happened).
    OSError
        The crate's disk-full class (``ENOSPC``/``EDQUOT``) — the caller
        maps it via ``lake_write_guard``.
    LakeRustWavedUnavailableError, LakeRustWavedFlagRemovedError
        The precondition chain (module docstring).
    """
    crate = _require_crate()
    outcome = crate.waved_heal_batch(lake_root, quarantine_root, findings, on_event)
    if not isinstance(outcome, dict):
        raise _refuse_poison("waved_heal_batch", f"outcome type {type(outcome).__name__}")
    if "degrade" in outcome:
        # A pre-endgame wheel's whole-batch degrade — structurally
        # unreachable behind the waved_endgame_witness API guard; refuse
        # loudly rather than guess (there is no Python core to run).
        raise _refuse_poison(
            "waved_heal_batch", f"stale whole-batch degrade verdict {outcome.get('degrade')!r}"
        )
    if set(outcome) != _HEAL_OUTCOME_KEYS:
        raise _refuse_poison("waved_heal_batch", "outcome keys refused")
    return outcome


def lake_content_token_via_rust(
    *,
    lake_root: str,
    on_skip: Callable[[str, str, str], None],
) -> str:
    """The content-token walk — called by ``lake_content_token`` (the ONE arm).

    Walk-skip events (a raced-away entry/dir) cross as
    ``{"event", "path", "error"}`` dicts and are replayed through
    *on_skip* — the caller owns the pinned ``lake_heal.token_*_skipped``
    emission sites (house law i). A crate failure propagates RAW (the
    startup sweep's ``_SWEEP_GUARD_ERRORS`` own the never-crash-boot law);
    a mis-shaped return raises the refuse-poison ``RuntimeError``.
    """
    crate = _require_crate()
    result = crate.waved_lake_content_token(lake_root)
    if (
        not isinstance(result, tuple)
        or len(result) != _TOKEN_RESULT_ARITY
        or not isinstance(result[0], str)
        or not isinstance(result[1], list)
    ):
        raise _refuse_poison("waved_lake_content_token", f"result type {type(result).__name__}")
    token: str = result[0]
    skips: list[tuple[str, str, str]] = []
    for item in result[1]:
        if (
            not isinstance(item, dict)
            or set(item) != _TOKEN_SKIP_KEYS
            or not all(isinstance(item[key], str) for key in _TOKEN_SKIP_KEYS)
        ):
            raise _refuse_poison("waved_lake_content_token", "poisoned skip channel")
        skips.append((str(item["event"]), str(item["path"]), str(item["error"])))
    for event, path, error in skips:
        on_skip(event, path, error)
    return token


#: The waved reconcile result's tuple arity — ``(row_count, first_ts_us,
#: last_ts_us, tz_aware, sha256_hex, dup, foreign_rows)``.
_RECONCILE_RESULT_ARITY: Final[int] = 7

#: The sha256 hexdigest length (the ``raw_hash`` wire shape).
_SHA256_HEX_LEN: Final[int] = 64

#: The dup-counts pair arity — ``(physical_rows, unique_keys)``.
_DUP_PAIR_ARITY: Final[int] = 2

#: Epoch anchors for rebuilding polars' ``Series.min()/max()`` datetimes
#: from the crate's PHYSICAL epoch-µs: an AWARE column's min/max compare
#: instant-equal under any tz (aware ``datetime`` equality is by instant),
#: a NAIVE column's are the naive wall times — both are epoch + physical µs.
_EPOCH_UTC: Final[datetime] = datetime(1970, 1, 1, tzinfo=UTC)
_EPOCH_NAIVE: Final[datetime] = datetime(1970, 1, 1)


def _is_count(value: object) -> bool:
    """``True`` iff *value* is a genuine int (a bool-punned int is poison)."""
    return isinstance(value, int) and not isinstance(value, bool)


def _reconcile_findings(
    coords: PartitionCoords, dup: tuple[int, int] | None, foreign_rows: int
) -> tuple[IntraFileDup | ForeignMonthRows, ...]:
    """Rebuild the L1 finding objects from the crate's classify counts.

    Value-equal to the historical ``classify_bar_keys`` output by
    construction: the same lake-relative ``coords.relpath()`` path stamp,
    the same coords object, the same dup-then-foreign order,
    ``overlap_rows=None`` (the free L1 path never reads the canonical
    sibling) — pinned by the twin-catalog differential. Lazy import: the
    finding dataclasses live in ``lake_invariants`` (which imports
    polars); this leaf stays polars-free at module scope.
    """
    from brutex.data.lake_invariants import ForeignMonthRows, IntraFileDup

    rel = Path(coords.relpath())
    findings: list[IntraFileDup | ForeignMonthRows] = []
    if dup is not None:
        findings.append(
            IntraFileDup(
                path=rel, coords=coords, physical_rows=int(dup[0]), unique_keys=int(dup[1])
            )
        )
    if foreign_rows:
        findings.append(
            ForeignMonthRows(
                path=rel, coords=coords, foreign_rows=int(foreign_rows), overlap_rows=None
            )
        )
    return tuple(findings)


def _reconcile_scan_from_result(
    result: object, coords: PartitionCoords | None
) -> WavedFileScan | None:
    """Shape-validate + convert the crate's reconcile wire to the scan tuple.

    Raises ``ValueError`` on ANY junk shape (a non-tuple/None root, wrong
    arity, bool-punned/non-int counts, a law-violating count relation, a
    non-64-char hash, an out-of-``datetime``-range span) — the caller
    converts that into the refuse-poison ``RuntimeError``. ``None`` (the
    crate's zero-row verdict) passes through as ``None``.
    """
    if result is None:
        return None
    if not isinstance(result, tuple) or len(result) != _RECONCILE_RESULT_ARITY:
        raise ValueError(f"reconcile result must be a 7-tuple or None, got {type(result).__name__}")
    row_count, first_us, last_us, tz_aware, raw_hash, dup, foreign_rows = result
    for label, value in (
        ("row_count", row_count),
        ("first_ts_us", first_us),
        ("last_ts_us", last_us),
        ("foreign_rows", foreign_rows),
    ):
        if not _is_count(value):
            raise ValueError(f"reconcile {label} must be an int")
    if row_count < 1 or foreign_rows < 0 or foreign_rows > row_count or first_us > last_us:
        raise ValueError("reconcile counts/span violate the scan laws")
    if not isinstance(tz_aware, bool):
        raise ValueError("reconcile tz_aware must be a bool")
    if not isinstance(raw_hash, str) or len(raw_hash) != _SHA256_HEX_LEN:
        raise ValueError("reconcile sha256 must be a 64-char hex string")
    if dup is not None and (
        not isinstance(dup, tuple)
        or len(dup) != _DUP_PAIR_ARITY
        or not all(_is_count(value) for value in dup)
        or dup[0] != row_count
        or not dup[0] > dup[1] >= 1
    ):
        raise ValueError("reconcile dup counts violate the classify law")
    try:
        epoch = _EPOCH_UTC if tz_aware else _EPOCH_NAIVE
        first_ts = epoch + timedelta(microseconds=first_us)
        last_ts = epoch + timedelta(microseconds=last_us)
    except OverflowError as exc:
        raise ValueError(f"reconcile span outside the datetime range: {exc}") from exc
    findings: tuple[IntraFileDup | ForeignMonthRows, ...] = ()
    if coords is not None:
        findings = _reconcile_findings(coords, dup, foreign_rows)
    return (int(row_count), first_ts, last_ts, raw_hash, findings)


def reconcile_file_via_rust(
    *,
    path: str,
    coords: PartitionCoords | None,
) -> WavedFileScan | None:
    """The per-file reconcile scan — called by ``reconcile_catalog_from_lake``.

    Returns ``(row_count, first_ts, last_ts, raw_hash, findings)`` or
    ``None`` for a zero-row file (the caller's counted skip). The
    ``raw_hash`` sha256 is run-identity-adjacent (Golden Rule #4 — it
    feeds the FR-CAT-004 catalog rollup → ``data_snapshot_hash``).

    Raises
    ------
    ValueError
        ANY crate-call failure — a corrupt file, an out-of-envelope
        timestamp shape (the D-15 ms/ns class), a crate panic — naming the
        file (the ruled endgame semantics: ``ValueError`` is a member of
        the caller's GAP-#9 skip set via ``datetime_out_of_range_errors``,
        so ONE poisoned file is a loud counted
        ``catalog.reconcile.parquet_skip``, never a crashed reconcile).
    RuntimeError
        A mis-shaped crate return (refuse-poison — a structurally broken
        wheel must crash the reconcile LOUDLY rather than skip the whole
        lake into an empty catalog).
    LakeRustWavedUnavailableError, LakeRustWavedFlagRemovedError
        The precondition chain (module docstring).
    """
    crate = _require_crate()
    classify_home = None if coords is None else (coords.year, coords.month)
    try:
        result = crate.waved_reconcile_file(path, classify_home)
    except (KeyboardInterrupt, SystemExit, MemoryError):
        raise
    except _panic_belt_errors() as exc:
        raise ValueError(f"native reconcile scan refused {path}: {exc}") from exc
    try:
        return _reconcile_scan_from_result(result, coords)
    except ValueError as exc:
        raise _refuse_poison("waved_reconcile_file", str(exc)) from exc


# ────────────────────────────────────────────────────────────────────────
# The ``lake_invariants`` whole-lake scan surface
# ────────────────────────────────────────────────────────────────────────

#: The most recent Rust scan's ``reads_performed`` wire counter (the
#: Wave-D design §F2 read-count witness — the seam the deleted
#: ``_read_key_columns`` spy pins re-bound onto). ``None`` until a scan
#: serves; reset by :func:`reset_lake_rust_waved_for_tests`.
_LAST_SCAN_READS: Final[list[int | None]] = [None]

#: The logger the scan-event replay writes through — the SAME structlog
#: name as ``lake_invariants._LOG``, so a replayed WARN is
#: indistinguishable from the historical Python arm's at the sink (house
#: law i: structlog stays Python — the crate returns event COORDINATES).
_LI_LOG = structlog.get_logger("brutex.data.lake_invariants")

#: The two scan log-event names the crate may replay (refuse-poison
#: vocabulary — any other token is a mis-shaped wire).
_SCAN_LOG_EVENTS: Final[frozenset[str]] = frozenset(
    {"lake.invariant.scan_skip", "lake.invariant.symlink_skipped"}
)

#: The scan wire's ``files_by_segment`` entry arity — ``(segment, count)``.
_SCAN_SEGMENT_PAIR_ARITY: Final[int] = 2

#: The scan wire's ``symbol_dirs`` entry arity — ``(exchange, segment, symbols)``.
_SCAN_SYMBOL_DIRS_ARITY: Final[int] = 3

#: The scan event-record kind tokens (the crate wire contract).
_SCAN_EVENT_KINDS: Final[frozenset[str]] = frozenset({"log", "finding", "drift"})


def reset_lake_rust_waved_for_tests() -> None:
    """Reset the per-process scan-reads witness (tests ONLY)."""
    _LAST_SCAN_READS[0] = None


class _PauseEscape(BaseException):
    """Marker for a raising ``per_file_pause`` callback crossing the seam.

    The facade wraps the injected pause callback so ITS OWN exception —
    whatever class, ``KeyboardInterrupt`` included (hence the
    ``BaseException`` base) — is parked and re-raised VERBATIM by
    :func:`scan_lake_via_rust`, never re-labelled.
    """


def _scan_coords_from_wire(rec: Mapping[str, object]) -> PartitionCoords:
    """Build the ``PartitionCoords`` a bars-arm finding record carries."""
    from brutex.data.lake_invariants import PartitionCoords

    for key in ("exchange", "segment", "symbol", "interval"):
        if not isinstance(rec.get(key), str):
            raise ValueError(f"scan finding {key} must be a string")
    if not _is_count(rec.get("year")) or not _is_count(rec.get("month")):
        raise ValueError("scan finding year/month must be ints")
    return PartitionCoords(
        exchange=cast("str", rec["exchange"]),
        segment=cast("str", rec["segment"]),
        symbol=cast("str", rec["symbol"]),
        interval=cast("str", rec["interval"]),
        year=cast("int", rec["year"]),
        month=cast("int", rec["month"]),
    )


def _scan_str(rec: Mapping[str, object], key: str) -> str:
    """*rec[key]* as a genuine ``str`` (anything else refuses the wire)."""
    value = rec.get(key)
    if not isinstance(value, str):
        raise ValueError(f"scan finding {key} must be a string")
    return value


def _scan_count(rec: Mapping[str, object], key: str) -> int:
    """*rec[key]* as a genuine non-negative int (bool-punned ints refuse)."""
    value = rec.get(key)
    if not _is_count(value) or cast("int", value) < 0:
        raise ValueError(f"scan {key} must be a non-negative int")
    return cast("int", value)


def _scan_overlap(rec: Mapping[str, object]) -> int | None:
    """``overlap_rows`` — ``None`` (unresolved) or a non-negative int."""
    value = rec.get("overlap_rows")
    if value is None:
        return None
    if not _is_count(value) or cast("int", value) < 0:
        raise ValueError("scan overlap_rows must be None or a non-negative int")
    return cast("int", value)


def _scan_finding_from_wire(rec: object) -> LakeFinding:
    """Rebuild ONE frozen finding dataclass from its crate wire record.

    Raises ``ValueError`` on ANY junk shape (refuse-poison — the caller
    converts it into the loud ``RuntimeError``).
    """
    from brutex.data import lake_invariants as li

    if not isinstance(rec, Mapping):
        raise ValueError(f"scan finding must be a mapping, got {type(rec).__name__}")
    kind = rec.get("kind")
    path = Path(_scan_str(rec, "path"))
    if kind == "bars_dup":
        return li.IntraFileDup(
            path=path,
            coords=_scan_coords_from_wire(rec),
            physical_rows=_scan_count(rec, "physical_rows"),
            unique_keys=_scan_count(rec, "unique_keys"),
        )
    if kind == "bars_foreign":
        return li.ForeignMonthRows(
            path=path,
            coords=_scan_coords_from_wire(rec),
            foreign_rows=_scan_count(rec, "foreign_rows"),
            overlap_rows=_scan_overlap(rec),
        )
    if kind == "price":
        return li.PriceSanityRows(
            path=path,
            coords=_scan_coords_from_wire(rec),
            bad_rows=_scan_count(rec, "bad_rows"),
        )
    if kind == "alien_bars":
        return li.SchemaAlienFile(
            path=path, reason=_scan_str(rec, "reason"), coords=_scan_coords_from_wire(rec)
        )
    if kind == "alien_option":
        return li.SchemaAlienFile(
            path=path, reason=_scan_str(rec, "reason"), series=_scan_str(rec, "series")
        )
    if kind == "option_dup":
        return li.OptionIntraFileDup(
            path=path,
            series=_scan_str(rec, "series"),
            physical_rows=_scan_count(rec, "physical_rows"),
            unique_keys=_scan_count(rec, "unique_keys"),
        )
    if kind == "option_foreign":
        return li.OptionForeignMonthRows(
            path=path,
            series=_scan_str(rec, "series"),
            year=_scan_count(rec, "year"),
            month=_scan_count(rec, "month"),
            foreign_rows=_scan_count(rec, "foreign_rows"),
            overlap_rows=_scan_overlap(rec),
        )
    raise ValueError(f"unknown scan finding kind: {kind!r}")


def _scan_events_from_wire(
    events: object, *, finding_count: int, include_bars: bool
) -> tuple[Mapping[str, object], ...]:
    """Shape-validate the ordered replay stream (refuse-poison on ANY junk)."""
    if not isinstance(events, Sequence) or isinstance(events, (str, bytes)):
        raise ValueError("scan events must be a sequence")
    drift_markers = 0
    validated: list[Mapping[str, object]] = []
    for rec in events:
        if not isinstance(rec, Mapping):
            raise ValueError("scan event must be a mapping")
        kind = rec.get("kind")
        if kind not in _SCAN_EVENT_KINDS:
            raise ValueError(f"unknown scan event kind: {kind!r}")
        if kind == "log":
            if _scan_str(rec, "event") not in _SCAN_LOG_EVENTS:
                raise ValueError(f"unknown scan log event: {rec.get('event')!r}")
            _scan_str(rec, "path")
            if not isinstance(rec.get("path_repr"), bool) or not isinstance(
                rec.get("error_repr"), bool
            ):
                raise ValueError("scan log repr flags must be bools")
            if rec.get("error") is not None and not isinstance(rec.get("error"), str):
                raise ValueError("scan log error must be None or a string")
        elif kind == "finding":
            index = _scan_count(rec, "index")
            if index >= finding_count:
                raise ValueError("scan finding event index out of range")
        else:
            drift_markers += 1
        validated.append(rec)
    if drift_markers != (1 if include_bars else 0):
        raise ValueError(f"scan drift marker count {drift_markers} violates include_bars")
    return tuple(validated)


def _scan_report_from_wire(
    wire: object,
    *,
    lake_root: Path,
    canonical_keys: Collection[str] | None,
    include_bars: bool,
) -> tuple[LakeScanReport, tuple[Mapping[str, object], ...], int, int, int]:
    """Wire dict → ``(report, events, drift_at, drift_len, reads_performed)``.

    Runs the Python :func:`~brutex.data.lake_invariants.detect_legacy_drift`
    fold (STAYS Python — it needs the ``lake_keys`` ladder + the instrument
    vocabulary) over the crate's enumerated ``symbol_dirs`` and splices its
    findings at the crate's ``drift_insert_at`` — the scanner's canonical
    position (after the bars tree, before the options tree). The report's
    ``duration_s`` is a placeholder 0.0 — the caller stamps the real wall
    clock at serve time. Raises ``ValueError`` on ANY junk wire shape
    (refuse-poison).
    """
    from brutex.data import lake_invariants as li

    if not isinstance(wire, Mapping):
        raise ValueError(f"scan wire must be a mapping, got {type(wire).__name__}")
    raw_findings = wire.get("findings")
    if not isinstance(raw_findings, Sequence) or isinstance(raw_findings, (str, bytes)):
        raise ValueError("scan findings must be a sequence")
    findings = tuple(_scan_finding_from_wire(rec) for rec in raw_findings)
    drift_at = _scan_count(wire, "drift_insert_at")
    if drift_at > len(findings) or (not include_bars and drift_at != 0):
        raise ValueError("scan drift_insert_at out of range")
    events = _scan_events_from_wire(
        wire.get("events"), finding_count=len(findings), include_bars=include_bars
    )
    counters = {
        key: _scan_count(wire, key)
        for key in (
            "files_scanned",
            "files_skipped",
            "series_scanned",
            "option_files_scanned",
            "option_files_skipped",
            "engine_files",
            "engine_files_skipped",
            "reads_performed",
        )
    }
    by_segment_raw = wire.get("files_by_segment")
    if not isinstance(by_segment_raw, Sequence) or isinstance(by_segment_raw, (str, bytes)):
        raise ValueError("scan files_by_segment must be a sequence")
    by_segment: list[tuple[str, int]] = []
    for pair in by_segment_raw:
        if (
            not isinstance(pair, Sequence)
            or len(pair) != _SCAN_SEGMENT_PAIR_ARITY
            or not isinstance(pair[0], str)
        ):
            raise ValueError("scan files_by_segment entries must be (segment, count)")
        if not _is_count(pair[1]) or cast("int", pair[1]) < 0:
            raise ValueError("scan files_by_segment count must be a non-negative int")
        by_segment.append((pair[0], cast("int", pair[1])))
    for key in ("first_skipped_parse", "first_skipped_read"):
        if wire.get(key) is not None and not isinstance(wire.get(key), str):
            raise ValueError(f"scan {key} must be None or a string")
    symbol_dirs_raw = wire.get("symbol_dirs")
    if not isinstance(symbol_dirs_raw, Sequence) or isinstance(symbol_dirs_raw, (str, bytes)):
        raise ValueError("scan symbol_dirs must be a sequence")
    symbol_dirs: dict[tuple[str, str], list[str]] = {}
    for entry in symbol_dirs_raw:
        if not isinstance(entry, Sequence) or len(entry) != _SCAN_SYMBOL_DIRS_ARITY:
            raise ValueError("scan symbol_dirs entries must be (exchange, segment, symbols)")
        exchange_name, segment_name, names = entry
        if not isinstance(exchange_name, str) or not isinstance(segment_name, str):
            raise ValueError("scan symbol_dirs keys must be strings")
        if not isinstance(names, Sequence) or isinstance(names, (str, bytes)):
            raise ValueError("scan symbol_dirs names must be a sequence")
        for name in names:
            if not isinstance(name, str):
                raise ValueError("scan symbol_dirs names must be strings")
        symbol_dirs[(exchange_name, segment_name)] = list(names)
    drift: tuple[LakeFinding, ...] = ()
    if include_bars:
        # The crate's symbol names arrive sorted (BTreeSet) — the exact
        # ``{key: sorted(names)}`` shape the scanner feeds the fold.
        drift = tuple(
            li.detect_legacy_drift(symbol_dirs, lake_root=lake_root, canonical_keys=canonical_keys)
        )
    full = findings[:drift_at] + drift + findings[drift_at:]
    first_skipped = wire.get("first_skipped_parse")
    if first_skipped is None:
        first_skipped = wire.get("first_skipped_read")
    report = li.LakeScanReport(
        findings=full,
        files_scanned=counters["files_scanned"],
        files_skipped=counters["files_skipped"],
        series_scanned=counters["series_scanned"],
        option_files_scanned=counters["option_files_scanned"],
        option_files_skipped=counters["option_files_skipped"],
        duration_s=0.0,
        files_by_segment=tuple(by_segment),
        engine_files=counters["engine_files"],
        engine_files_skipped=counters["engine_files_skipped"],
        first_skipped_path=cast("str | None", first_skipped),
    )
    return report, events, drift_at, len(drift), counters["reads_performed"]


def _replay_scan_events(
    events: tuple[Mapping[str, object], ...],
    findings: tuple[LakeFinding, ...],
    *,
    drift_at: int,
    drift_len: int,
) -> None:
    """Replay the crate's ordered event stream through the Python sinks.

    House law i — structlog stays Python: log WARNs write through the SAME
    logger name as ``lake_invariants._LOG`` with the crate's per-site
    ``repr()`` flags (the #907/#1594 log-injection law); finding events
    replay :func:`~brutex.data.lake_invariants.log_lake_finding` at the
    exact canonical log position (dup/price/alien at classification,
    foreign at overlap resolution); the drift marker logs the spliced
    drift findings in order. A crate finding index at/after ``drift_at``
    shifts past the spliced drift block.
    """
    from brutex.data.lake_invariants import log_lake_finding

    for rec in events:
        kind = rec["kind"]
        if kind == "log":
            path_text = cast("str", rec["path"])
            path_value = repr(path_text) if rec["path_repr"] else path_text
            error = rec.get("error")
            if error is None:
                _LI_LOG.warning(cast("str", rec["event"]), path=path_value)
            else:
                error_text = cast("str", error)
                error_value = repr(error_text) if rec["error_repr"] else error_text
                _LI_LOG.warning(cast("str", rec["event"]), path=path_value, error=error_value)
        elif kind == "finding":
            index = cast("int", rec["index"])
            log_lake_finding(findings[index if index < drift_at else index + drift_len])
        else:  # drift — log the spliced Python drift findings in order
            for finding in findings[drift_at : drift_at + drift_len]:
                log_lake_finding(finding)


def last_rust_scan_reads() -> int | None:
    """The Rust scan's most recent read-attempt count (the §F2 seam).

    ``None`` until a scan serves in this process — the wire counter the
    historical ``_read_key_columns`` read-count spy pins re-bound onto at
    the Wave-D delete (design §F2).
    """
    return _LAST_SCAN_READS[0]


def scan_lake_via_rust(
    lake_root: Path,
    *,
    exchange: str | None,
    segment: str | None,
    symbol: str | None,
    interval: str | None,
    months: Collection[tuple[int, int]] | None,
    include_options: bool,
    include_bars: bool,
    canonical_keys: Collection[str] | None,
    per_file_pause: Callable[[], None] | None,
) -> LakeScanReport:
    """The whole-lake invariant scan — called by ``scan_lake_invariants``.

    The crate returns the findings + counters + an ORDERED event stream;
    this facade rebuilds the frozen dataclasses, runs the Python
    ``detect_legacy_drift`` fold over the crate-enumerated symbol dirs
    (house law ii), splices its findings at the crate's insert point, and
    replays the log/finding events through the Python sinks (house law i).
    Isolation is CRATE-OWNED (the W-6a law): an unjudgeable shape is a
    loud counted ``lake.invariant.scan_skip`` inside the report, so a
    crate-call raise here is the structurally-unreachable defensive belt —
    propagated RAW. A raising *per_file_pause* re-raises VERBATIM (class +
    message) via the :class:`_PauseEscape` parking wrap. A mis-shaped wire
    raises the refuse-poison ``RuntimeError``.
    """
    crate = _require_crate()
    from brutex.data import lake_invariants as li
    from brutex.data import partition as partition_mod

    started = time.perf_counter()
    pause_error: list[BaseException] = []
    pause_arg: Callable[[], None] | None = None
    if per_file_pause is not None:
        pause = per_file_pause

        def _ticked() -> None:
            try:
                pause()
            except BaseException as exc:
                pause_error.append(exc)
                raise _PauseEscape from exc

        pause_arg = _ticked
    try:
        wire = crate.waved_scan_lake(
            str(lake_root),
            exchange,
            segment,
            symbol,
            interval,
            None if months is None else [(int(year), int(month)) for year, month in months],
            include_bars,
            include_options,
            sorted(partition_mod.VALID_EXCHANGES),
            sorted(partition_mod.VALID_SEGMENTS),
            sorted(partition_mod.VALID_INTERVALS),
            # The forbidden-chars vocabulary is DATA crossing the FFI —
            # imported from its ONE partition.py home (§11 COMMON; a
            # re-minted copy could silently drift from the writer law).
            "".join(
                sorted(partition_mod._FORBIDDEN_SYMBOL_CHARS)  # pyright: ignore[reportPrivateUsage]
            ),
            [(exch, seg, sym) for exch, seg, sym in li.ENGINE_INDEX_COORDS],
            pause_arg,
        )
    except _PauseEscape as marker:
        if pause_error:
            raise pause_error[0] from None
        raise _refuse_poison("waved_scan_lake", "pause escape without a captured exception") from (
            marker
        )
    try:
        report, events, drift_at, drift_len, reads = _scan_report_from_wire(
            wire, lake_root=lake_root, canonical_keys=canonical_keys, include_bars=include_bars
        )
    except ValueError as exc:
        raise _refuse_poison("waved_scan_lake", str(exc)) from exc
    _LAST_SCAN_READS[0] = reads
    report = dataclasses.replace(report, duration_s=time.perf_counter() - started)
    _replay_scan_events(events, report.findings, drift_at=drift_at, drift_len=drift_len)
    _LI_LOG.info(
        "lake.invariant.scan_complete",
        files=report.files_scanned + report.option_files_scanned,
        findings=len(report.findings),
        duration_ms=round(report.duration_s * 1000.0, 3),
    )
    return report


__all__ = [
    "LAKE_WAVED_RUST_ENV",
    "heal_batch_via_rust",
    "lake_content_token_via_rust",
    "last_rust_scan_reads",
    "read_1m_via_rust",
    "reconcile_file_via_rust",
    "require_waved_env_unset",
    "reset_lake_rust_waved_for_tests",
    "scan_lake_via_rust",
]
