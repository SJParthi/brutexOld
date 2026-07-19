"""Flag-gated Rust Wave-D seam — the ADR-0020 heal/scan/read conversion facade.

The ONE shared facade for the Wave-D lake conversion
(`timeframe_materialize` → `lake_heal` → `catalog_reconcile` →
`lake_invariants`, in the ruled runbook order). When — and ONLY when —
``BRUTEX_LAKE_WAVED_RUST`` is set truthy, the converted surfaces route
through the ``brutex_lake`` crate's ``waved_*`` pyfunctions
(``rust/brutex-lake/src/waved/``); this module owns the flag, the
fail-closed guard layers, the process-local latches, and the per-surface
burn-in cells. ONE facade for all four modules (the Wave-D design §D): the
four surfaces flip as one wave with one crate surface, so a shared latch is
the atomic-pin doctrine — any guard trip is evidence the INSTALL is broken,
the Python oracle serves, and (payoff ≈ 0 — uniformity, not speed)
over-disabling costs nothing. Section-for-section the
``lake_rust_resample.py`` mirror (itself the PR-1 ``lake_rust_render.py``
template); the degrade/panic-belt catch sets are IMPORTED from
``lake_rust_render`` (its public ``degrade_errors``/``panic_belt_errors``
re-exports — §11 COMMON, never a fourth copy).

## W-1 (this slice): the ``timeframe_materialize`` read surface

:func:`read_1m_via_rust` routes the ``make_lake_read_1m._read`` body —
collect + rename/select/sort + ts-datetime-range gate + price-sanity gate +
bad-tick screen — through ``brutex_lake.waved_read_1m_ipc``. The SOURCE
DECISION (months / whole-tree / empty-partition, with the pinned
``NoStored1mBarsError`` + months-arm ``ValueError`` raises) stays in the
caller BEFORE this seam, so both arms raise byte-identically by
construction. Gate verdicts keep exact parity through the DEGRADE-TO-ORACLE
law: a crate-side refusal (a ts-range / price-sanity violation, a corrupt
file, …) re-runs the live Python oracle, which either raises the verbatim
typed error (``ResampleInputError`` with today's message bytes) or succeeds
— the oracle's verdict always wins. Bad-tick WARNs stay Python: the crate
returns per-session warn COORDINATES which this facade replays through the
ONE :func:`brutex.data.bad_tick.emit_bad_tick_warn` seam (the
``rust/brutex-feed`` ``scan_bars`` precedent) — and ONLY when the oracle did
not run (a burn-in call's oracle emits its own WARNs, so events are never
double-emitted).

## W-2 (this slice): the ``lake_heal`` mutation core + content token

:func:`heal_batch_via_rust` routes ``heal_findings``' per-finding mutation
loop through ``brutex_lake.waved_heal_batch``. The POLICY SHELL (observe
mode, mass rail, quarantine-symlink refusal, stamp/serial minting, the
PriceSanityRows filter, ordering, the recall, the report/stats/alert tail)
stays in ``lake_heal.py`` BEFORE/AFTER this seam; the crate streams every
log/audit/catalog/stats/manifest side effect through ONE ``on_event``
callback the shell replays (house laws i-iii). NO burn-in and NO
degrade-around-the-call (heal MUTATES — the §B2 asymmetry): the crate's own
read-only preflight is the sanctioned degrade arm (``{"degrade": reason}``
certifies zero mutation → the shell runs the verbatim Python core), any
exception out of the engaged call is LOUD, and the pre-flip verification is
the offline heal differential
(``tests/integration/test_waved_heal_differential.py``).
:func:`lake_content_token_via_rust` routes the read-only sentinel walk with
the full W-1 doctrine (degrade-to-oracle + burn-in token compare).

## W-3 (this slice): the ``catalog_reconcile`` per-file scan

:func:`reconcile_file_via_rust` routes the per-file read/hash/classify body
of ``reconcile_catalog_from_lake`` — the ``timestamp``-column read + row
count + physical min/max span + the ``sha256(file bytes)`` ``raw_hash``
(**run-identity-adjacent**, Golden Rule #4: it feeds the FR-CAT-004 catalog
rollup → ``data_snapshot_hash``) + the L1 ``classify_bar_keys`` dup/foreign
counts — through ``brutex_lake.waved_reconcile_file``. The LOOP (glob
enumeration, ``parse_partition_path``, the stat-cache branch,
``CatalogEntry`` build + upsert, the drift fold, all logging) stays in
``catalog_reconcile.py``. Read-only, so the full W-1 fail-closed doctrine
applies — degrade-to-oracle (a crate refusal re-runs the verbatim Python
body, which raises the REAL skip-class exception with today's message
bytes, so the GAP-#9 ``catalog.reconcile.parquet_skip`` verdict stays
byte-identical by construction), refuse-poison shapes, and the per-surface
burn-in whose tuple compare makes the sha256 hex the tooth. The
``IntraFileDup`` / ``ForeignMonthRows`` finding OBJECTS are reconstructed
facade-side from the crate counts (dataclasses stay Python), value-equal to
``classify_bar_keys``' output by the frozen-dataclass equality the burn-in
compares.

## W-4 (this slice): the ``lake_invariants`` whole-lake scan

:func:`scan_lake_via_rust` routes the WHOLE ``scan_lake_invariants`` body —
the streamed bars-tree walk + per-file read/classify (dup / foreign+overlap
/ price-sanity / alien-schema), the options-tree walk (symlink probes, key
gate, dup / foreign / memoised-sibling overlap), every skip verdict and the
counters — through ``brutex_lake.waved_scan_lake``. STAYS PYTHON (house
laws i-ii): structlog (the crate returns an ORDERED event stream with
per-site ``repr()`` flags this facade replays through the
``lake_invariants`` logger + ``log_lake_finding``), the
``detect_legacy_drift`` fold (needs the ``lake_keys`` ladder — run
facade-side over the crate-enumerated symbol dirs and SPLICED at the
crate's insert point), the injected ``per_file_pause`` callback (ticked by
the crate through the FFI after every read attempt — the L3 throttle law),
and the frozen finding dataclasses (rebuilt from the wire records).
Read-only, so the full W-1 doctrine applies — degrade-to-oracle,
refuse-poison wire validation, and the per-surface burn-in comparing the
reports ORDER-EXACT with the wall clock popped (burn-in serves the ORACLE
report so the replayed event stream never double-logs). The wire also
carries the design-§F2 ``reads_performed`` counter
(:func:`last_rust_scan_reads`) — the hook the delete-time read-count spy
pins rebind onto. A raising pause callback re-raises VERBATIM via the
``_PauseEscape`` parking wrap, never folded into the degrade set.

## The flag — ``BRUTEX_LAKE_WAVED_RUST`` (DEFAULT OFF)

A truthy literal (``1/true/yes/on``) → ON; a falsy literal
(``0/false/no/off``) → OFF; unset or whitespace-empty → the default (OFF);
any OTHER value → OFF with one loud ``lake_rust_waved.fallback`` WARN
(reason ``flag_unrecognised``) per process. The truthy/falsy vocabulary is
a LOCAL twin of the render/resample facades' (equality-pinned in the unit
suite) — NOT an import from the engine copy, because the engine package
hard-imports the ``fno_math`` wheel and a lake read must never require it.

## The fail-closed layers (every failure serves the Python oracle)

1. **Import/API guard** — an absent wheel or a module missing any
   :data:`_CRATE_API` callable latches ``crate_unavailable`` (the crate and
   the repo move together — a conversion-step-N wheel lacking a step-N+1
   function is a stale install, ALL-OR-NOTHING).
2. **Version guard** — the crate's ``pinned_polars_version()`` must equal
   the installed ``polars.__version__`` (ADR-0020 §7 hazard 1); the waved
   surfaces are polars-bearing, so the guard is kept (unlike the polars-free
   window-ledger laws).
3. **Burn-in self-check** — per SURFACE: the first :data:`BURN_IN_SAMPLES`
   engaged calls (plus every previously-unseen shape token) run BOTH
   engines and compare at DTYPE-EXACT frame strength; ANY difference ships
   the ORACLE result and autoreverts the whole waved seam for the process
   (``lake_rust_waved.autoreverted`` at ERROR — the shared-latch doctrine).
   The W-2 ``heal`` surface has NO burn-in (heal MUTATES — the Wave-D
   design §B2 recorded asymmetry; its pre-flip verification is the offline
   heal differential).

Flag OFF (the default) is BYTE-IDENTICAL to today — the seam is never
called and the crate is never imported (bomb-spy-pinned); no module here
imports polars at module scope. Process-local latch state is the documented
no-global-mutable-state exception (the ``lake_rust_render`` shape);
:func:`reset_lake_rust_waved_for_tests` restores a fresh process view —
test isolation only, never called from production paths.
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

from brutex.data.lake_rust_render import degrade_errors, panic_belt_errors

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
    #: raw_hash, findings)`` — the W-3 seam's shared oracle/crate shape
    #: (``None`` = a zero-row file, the caller's counted skip).
    WavedFileScan = tuple[int, object, object, str, tuple["IntraFileDup | ForeignMonthRows", ...]]

_LOG = structlog.get_logger("brutex.data.lake_rust_waved")

#: The env flag. DEFAULT OFF — the Python cores stay the production path
#: until the Wave-D parity gates license an operator-directed default flip
#: behind its own DEC (honesty banner: payoff ≈ 0 — uniformity, not speed).
LAKE_WAVED_RUST_ENV: Final[str] = "BRUTEX_LAKE_WAVED_RUST"

#: The W-1 surface token (the per-surface burn-in cell + event key).
SURFACE_READ_1M: Final[str] = "read_1m"

#: The W-2 read-only surface token (the ``lake_content_token`` walk twin).
SURFACE_CONTENT_TOKEN: Final[str] = "content_token"  # noqa: S105 — a surface NAME, not a secret

#: The W-3 surface token (the ``catalog_reconcile`` per-file scan).
SURFACE_RECONCILE_FILE: Final[str] = "reconcile_file"

#: The W-4 surface token (the ``lake_invariants`` whole-lake scan).
SURFACE_SCAN: Final[str] = "scan_lake"

#: Engaged calls compared against the Python oracle per process PER surface
#: (the burn-in self-check) — 16, the render/resample/#1531 precedent.
BURN_IN_SAMPLES: Final[int] = 16

#: Truthy env literals — the LOCAL twin of the render/resample facades'
#: vocabulary (equality-pinned in the unit suite; module docstring).
_TRUTHY: Final[frozenset[str]] = frozenset({"1", "true", "yes", "on"})

#: Falsy env literals — the explicit kill-switch vocabulary.
_FALSY: Final[frozenset[str]] = frozenset({"0", "false", "no", "off"})

#: The latch: the disable reason once the waved seam is off for this
#: process, else ``None`` (the documented no-global-mutable-state exception).
_DISABLED_REASON: Final[list[str | None]] = [None]

#: Per-surface burn-in samples still to run this process. The W-2 HEAL
#: surface is DELIBERATELY absent — heal MUTATES, so an in-process
#: double-run comparator is impossible (the Wave-D design §B2 recorded
#: asymmetry); its pre-flip verification is the offline heal differential.
_BURN_IN_REMAINING: Final[dict[str, int]] = {
    SURFACE_READ_1M: BURN_IN_SAMPLES,
    SURFACE_CONTENT_TOKEN: BURN_IN_SAMPLES,
    SURFACE_RECONCILE_FILE: BURN_IN_SAMPLES,
    SURFACE_SCAN: BURN_IN_SAMPLES,
}

#: Per-surface shape tokens already compared this process (the shape gate).
#: Bounded by construction: read_1m sees at most the four-token
#: (with_oi x screen) grid — see :func:`read_1m_via_rust`'s token; the
#: content-token surface is single-shape (its set stays empty — the entry
#: exists so the reset seam's per-surface loop stays uniform).
_BURN_IN_SHAPES_SEEN: Final[dict[str, set[str]]] = {
    SURFACE_READ_1M: set(),
    SURFACE_CONTENT_TOKEN: set(),
    SURFACE_RECONCILE_FILE: set(),
    SURFACE_SCAN: set(),
}

#: Whether each surface's shape gate is armed (the resample-facade shape:
#: the test reset seam disarms it with a zeroed budget so ``remaining=0``
#: keeps meaning "steady state — crate results, no oracle in the path").
_BURN_IN_SHAPE_GATE: Final[dict[str, bool]] = {
    SURFACE_READ_1M: True,
    SURFACE_CONTENT_TOKEN: True,
    SURFACE_RECONCILE_FILE: True,
    SURFACE_SCAN: True,
}

#: The crate API surface the import guard verifies — ALL-OR-NOTHING over
#: every SHIPPED waved conversion step (W-2..W-4 append their names in
#: their own PRs; a wheel missing ANY listed name is a stale/foreign
#: install and latches ``crate_unavailable``).
_CRATE_API: Final[tuple[str, ...]] = (
    "pinned_polars_version",
    "waved_read_1m_ipc",
    "waved_has_stored_1m_bars",
    "waved_month_partition_files",
    "waved_heal_batch",
    "waved_lake_content_token",
    "waved_reconcile_file",
    "waved_scan_lake",
    # W-2b: the multi-file heal engagement witness (drift + bars-foreign +
    # option-foreign join the engaged set). A wheel lacking it predates the
    # multi-file heal port and MUST latch crate_unavailable — its
    # waved_heal_batch would degrade those kinds back to Python silently.
    "waved_heal_engaged_kinds",
)

#: Once-per-process-per-reason warn memo + per-surface engaged latches.
#: LOG-VOLUME state ONLY: served frames never depend on it.
_OBSERVABILITY_MEMO: Final[set[str]] = set()

#: The waved read result's tuple arity — ``(ipc_bytes, events_json)``.
_READ_RESULT_ARITY: Final[int] = 2

#: The event-payload keys every bad-tick warn record must carry (the crate
#: wire contract — see ``rust/brutex-lake/src/waved/mod.rs``).
_EVENT_KEYS: Final[frozenset[str]] = frozenset(
    {"session", "skipped", "max_ratio", "first_prev_close", "first_close"}
)


def env_flag_tristate(raw: str) -> bool | None:
    """Classify *raw* against the flag vocabulary (local twin — module docs).

    ``True`` for a :data:`_TRUTHY` literal, ``False`` for a :data:`_FALSY`
    literal, ``None`` for anything else (unset/empty AND unrecognised — the
    caller owns the default + warn policy). Byte-identical semantics to the
    render/resample facade copies, equality-pinned in the unit suite.
    """
    value = raw.strip().lower()
    if value in _TRUTHY:
        return True
    if value in _FALSY:
        return False
    return None


def lake_rust_waved_enabled() -> bool:
    """Resolve ``BRUTEX_LAKE_WAVED_RUST`` (default OFF, junk fails loud-OFF).

    * a :data:`_TRUTHY` value → ``True`` (the opt-in);
    * a :data:`_FALSY` value → ``False`` (the explicit, silent OFF);
    * unset / whitespace-empty → ``False`` (the default);
    * any OTHER non-empty value → ``False`` with one loud
      ``lake_rust_waved.fallback`` WARN (reason ``flag_unrecognised``) per
      process — a typo'd opt-in must be VISIBLE, never silently ignored.

    Read per call (one ``os.environ`` probe against a whole-partition read)
    so parallel workers resolve their own inherited environment.
    """
    raw = os.environ.get(LAKE_WAVED_RUST_ENV, "")
    verdict = env_flag_tristate(raw)
    if verdict is not None:
        return verdict
    if raw.strip():
        _warn_once("flag_unrecognised", value=raw)
    return False


def rust_waved_disabled() -> bool:
    """``True`` iff a guard/autorevert latched the waved seam OFF this process."""
    return _DISABLED_REASON[0] is not None


def reset_lake_rust_waved_for_tests(*, remaining: int = BURN_IN_SAMPLES) -> None:
    """Reset the latch + every surface's burn-in cell + warn memo (tests ONLY).

    ``remaining=0`` puts the process in the post-burn-in steady state —
    every surface's count budget spent AND shape gate disarmed, so a served
    result is the crate's with no oracle in the path (the parity/differential
    suites depend on that: an armed shape gate would cushion a first-shape
    divergence by shipping the oracle result). The default restores a
    fresh-process view. Never called from production code paths (the
    ``lake_rust_render`` reset-seam convention).
    """
    _DISABLED_REASON[0] = None
    for surface in _BURN_IN_REMAINING:
        _BURN_IN_REMAINING[surface] = remaining
        _BURN_IN_SHAPES_SEEN[surface].clear()
        _BURN_IN_SHAPE_GATE[surface] = remaining > 0
    _OBSERVABILITY_MEMO.clear()
    _LAST_SCAN_READS[0] = None


def _warn_once(reason: str, **context: object) -> None:
    """Log ``lake_rust_waved.fallback`` WARN once per process per *reason*.

    The read seam runs once per symbol per prebuild (hundreds of symbols per
    run), so a sticky reason would flood the logs; the first occurrence is
    loud, repeats fall through silently (the Python oracle serves
    regardless). The event name is a LITERAL with the reason as a field —
    the render/resample grep-able convention.
    """
    if reason in _OBSERVABILITY_MEMO:
        return
    _OBSERVABILITY_MEMO.add(reason)
    _LOG.warning("lake_rust_waved.fallback", reason=reason, **context)


def _disable(reason: str, *, error: bool = False, **context: object) -> None:
    """Latch the waved seam OFF for the remainder of the process.

    ``error=True`` logs ``lake_rust_waved.autoreverted`` at ERROR (the
    burn-in autorevert — a genuine wrongness signal); guard latches log
    ``lake_rust_waved.rust_path_disabled`` at WARN. ONE latch for every
    waved surface (the shared-latch doctrine — module docstring); repeat
    calls are no-ops.
    """
    if _DISABLED_REASON[0] is not None:
        return
    _DISABLED_REASON[0] = reason
    if error:
        _LOG.error("lake_rust_waved.autoreverted", reason=reason, **context)
    else:
        _LOG.warning("lake_rust_waved.rust_path_disabled", reason=reason, **context)


def _resolve_crate() -> ModuleType | None:
    """Import guard (layer 1): return ``brutex_lake``, or latch OFF + ``None``.

    An absent wheel (``ImportError``) latches ``crate_unavailable`` — the
    facade is default-OFF and opt-in, so an operator who set the flag
    without installing the crate gets ONE loud WARN naming the remedy. A
    module that IMPORTS but lacks any :data:`_CRATE_API` callable (a name
    collision / a pre-Wave-D wheel / a hand-broken install) latches the
    SAME reason — the fail-closed contract is "never a new crash class", so
    an ``AttributeError`` must never leave this seam.
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
                error_detail=f"brutex_lake.{name} missing or not callable — foreign/stale install",
                remedy="pip install rust/brutex-lake "
                "(see docs/runbooks/lake-rust-render-lockstep.md)",
            )
            return None
    return crate


#: Sentinel for "the metadata call raised" — distinct from any value a
#: foreign crate could RETURN (even ``None``, which flows on into the
#: version guard's ``!=`` and fails closed there, loudly).
_METADATA_ERROR: Final[object] = object()


def _crate_metadata(crate: ModuleType, attr: str) -> object:
    """Call a crate metadata pyfunction defensively; latch OFF on ANY raise.

    A raising ``pinned_polars_version`` marks a foreign/stale install whose
    results must not be trusted — latch ``crate_metadata_error`` and let
    the caller serve the oracle (fail-closed, never a new crash class).
    ``MemoryError`` re-raises bare (a global stop, never a fallback).
    Returns the raw value un-typechecked: a non-str version simply fails
    the ``!=`` version guard (fail-closed already).
    """
    try:
        return getattr(crate, attr)()
    except MemoryError:
        raise
    except Exception as exc:
        _disable("crate_metadata_error", metadata_call=attr, error_detail=repr(exc))
        return _METADATA_ERROR


def _crate_ready() -> ModuleType | None:
    """Run guard layers 1+2 (import/API + polars version); ``None`` = serve the oracle."""
    if rust_waved_disabled():
        return None
    crate = _resolve_crate()
    if crate is None:
        return None
    pinned = _crate_metadata(crate, "pinned_polars_version")
    if pinned is _METADATA_ERROR:
        return None
    import polars as pl

    if pinned != pl.__version__:
        # Layer 2 — the version guard (ADR-0020 §7 hazard 1): fail-closed,
        # ONE loud WARN naming BOTH versions — the waved surfaces decode
        # frames via the pinned polars, so a drifted wheel must never serve.
        _disable(
            "polars_version_mismatch",
            installed_polars=pl.__version__,
            crate_pinned_polars=pinned,
            remedy="align the wheel and the crate's pinned polars rev, then re-run "
            "the Wave-D differential (docs/runbooks/lake-rust-render-lockstep.md)",
        )
        return None
    if "__engaged__" not in _OBSERVABILITY_MEMO:
        _OBSERVABILITY_MEMO.add("__engaged__")
        _LOG.info("lake_rust_waved.engaged", crate_pinned_polars=pinned)
    return crate


def _frames_equal_dtype_exact(left: pl.DataFrame, right: pl.DataFrame) -> bool:
    """Frame equality at DTYPE-EXACT strength — the burn-in comparator.

    ``DataFrame.equals`` is dtype-INSENSITIVE (the #1795b documented
    semantics), so the schema (column names, ORDER, dtypes) is compared
    FIRST — the read frames feed the resamplers + parquet writers, where a
    dtype pun WOULD change downstream bytes. The ``bool()`` wrap keeps the
    return typed under a polars-stub-free checker (the pre-commit mypy env).
    """
    return bool(left.schema == right.schema and left.equals(right))


def _parse_bad_tick_events(events_json: str) -> list[dict[str, object]]:
    """Parse + shape-validate the crate's bad-tick events channel.

    Raises ``ValueError`` on ANY junk shape (non-JSON, a non-list root, a
    non-dict item, wrong keys, a non-str session, a bool-punned/non-int
    ``skipped``, a non-float ratio/close) — the caller treats that as
    refuse-poison and serves the oracle. Empty list for ``"[]"``.
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
    precedent), so event name + field names can never fork between the two
    arms; the ratio is the EVENT's own (the crate wire carries it), so the
    two sides can never silently disagree about the threshold. Lazy import:
    this leaf stays polars-free + bad_tick-free at module scope.
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
    oracle: Callable[[], pl.DataFrame],
) -> pl.DataFrame:
    """The routed 1-minute read — called by ``make_lake_read_1m`` ONLY flag-ON.

    Runs the fail-closed layers (module docstring) and returns the read
    frame. EVERY path returns a valid frame or raises exactly what the
    Python oracle raises: a latched / guarded / degraded call returns
    ``oracle()`` (the verbatim pre-seam collect body — which re-raises the
    typed gate errors with today's message bytes when the input is genuinely
    corrupt), a burn-in mismatch returns the ORACLE frame, and only a
    guard-clean, burn-in-clean call returns the crate's frame with its
    bad-tick WARN events replayed. The months / whole-tree / empty-partition
    SOURCE DECISION already ran in the caller (its raises are shared by both
    arms — the seam's contract), so exactly one of *files* / *tree_root* is
    non-``None``.

    Parameters
    ----------
    files
        The months arm: the explicit existing month-file paths (the Python
        ``month_partition_files`` output), or ``None`` for the whole-tree arm.
    tree_root
        The whole-tree arm: the symbol's ``1minute`` partition dir (the
        crate walks it — exact paths, never a glob), or ``None``.
    with_open_interest
        Select ``open_interest`` alongside OHLCV (the F&O prebuild path).
    screen_enabled
        The caller-resolved ``bad_tick_screen_enabled()`` verdict (ONE kill
        switch for both arms).
    warn_source
        The ``feed.bad_tick_skipped`` WARN source (the prebuild passes
        ``"prebuild:{exchange}/{segment}/{symbol}/1minute"``).
    oracle
        The verbatim Python read body — the live parity oracle every
        degrade path serves.
    """
    if (files is None) == (tree_root is None):
        # Defensive seam-contract check: exactly one source arm. Refuse the
        # crate call loudly and serve the oracle (never a crash class).
        _warn_once(
            "invalid_seam_args",
            files_given=files is not None,
            tree_root_given=tree_root is not None,
        )
        return oracle()
    crate = _crate_ready()
    if crate is None:
        return oracle()
    import polars as pl

    try:
        result = crate.waved_read_1m_ipc(
            [] if files is None else list(files),
            tree_root,
            with_open_interest,
            screen_enabled,
        )
    except degrade_errors() as exc:
        # The degrade-to-oracle law (module docstring): a crate refusal —
        # including a GENUINE gate violation — re-runs the oracle, which
        # reproduces the exact typed error (ResampleInputError et al.) or
        # succeeds; the oracle's verdict always wins.
        _warn_once("read_error", error_detail=repr(exc))
        return oracle()
    except (KeyboardInterrupt, SystemExit, MemoryError):
        # The escape set (the errors.py doctrine): operator interrupt /
        # interpreter shutdown / OOM stay global — never a fallback. Sits
        # ABOVE the panic belt so its `Exception` member never swallows an OOM.
        raise
    except panic_belt_errors() as exc:
        # The panic belt over the crate's own catch_unwind (the render
        # facade's doctrine verbatim).
        _disable("crate_panic", error_detail=repr(exc))
        return oracle()
    if (
        not isinstance(result, tuple)
        or len(result) != _READ_RESULT_ARITY
        or not isinstance(result[0], bytes)
        or not isinstance(result[1], str)
    ):
        # Refuse-poison gate: a mis-shaped return is a crate BUG — never
        # decoded, served loudly from the oracle. Structurally unreachable
        # through the real crate; guards a stale/foreign install.
        _warn_once("read_result_refused", result_type=type(result).__name__)
        return oracle()
    ipc_bytes, events_json = result
    decode_errors: tuple[type[Exception], ...] = (*degrade_errors(), OSError)
    try:
        rust_df = pl.read_ipc(io.BytesIO(ipc_bytes))
    except decode_errors as exc:
        # OSError joins the DECODE leg only (the resample-facade probe:
        # polars surfaces a truncated in-memory IPC payload as an OSError).
        _warn_once("result_decode_error", error_detail=repr(exc))
        return oracle()
    try:
        events = _parse_bad_tick_events(events_json)
    except ValueError as exc:
        # A poisoned events channel refuses the WHOLE result (the frame may
        # be equally untrustworthy) — the oracle serves + emits its own WARNs.
        _warn_once("events_refused", error_detail=repr(exc))
        return oracle()
    shape_token = f"oi={with_open_interest}|screen={screen_enabled}"
    shape_unseen = (
        _BURN_IN_SHAPE_GATE[SURFACE_READ_1M]
        and shape_token not in _BURN_IN_SHAPES_SEEN[SURFACE_READ_1M]
    )
    if _BURN_IN_REMAINING[SURFACE_READ_1M] > 0 or shape_unseen:
        # Layer 3 — the per-surface burn-in self-check: both engines read;
        # ANY dtype-exact frame difference ships the ORACLE frame and
        # autoreverts the whole waved seam. The oracle emits its OWN
        # bad-tick WARNs here, so the crate events are deliberately NOT
        # replayed on any burn-in path (never a double warn); an oracle
        # RAISE propagates — production-identical on a corrupt lake.
        oracle_df = oracle()
        if not _frames_equal_dtype_exact(rust_df, oracle_df):
            _disable(
                "burn_in_frame_mismatch",
                error=True,
                surface=SURFACE_READ_1M,
                rust_shape=str(rust_df.shape),
                python_shape=str(oracle_df.shape),
                shape_token=shape_token,
            )
            return oracle_df
        _BURN_IN_SHAPES_SEEN[SURFACE_READ_1M].add(shape_token)
        if _BURN_IN_REMAINING[SURFACE_READ_1M] > 0:
            _BURN_IN_REMAINING[SURFACE_READ_1M] -= 1
            if _BURN_IN_REMAINING[SURFACE_READ_1M] == 0:
                _LOG.info(
                    "lake_rust_waved.burn_in_complete",
                    surface=SURFACE_READ_1M,
                    samples=BURN_IN_SAMPLES,
                )
        return rust_df
    _replay_bad_tick_events(events, warn_source=warn_source)
    return rust_df


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
) -> dict[str, object] | None:
    """The routed heal-batch mutation core — called by ``heal_findings`` ONLY flag-ON.

    The Wave-D design §B2 asymmetry, verbatim: heal MUTATES, so this surface
    has NO burn-in and NO degrade-to-oracle around the call — the crate's own
    read-only PREFLIGHT is the sanctioned degrade arm (a ``{"degrade":
    reason}`` verdict certifies ZERO bytes were touched → return ``None`` and
    the caller runs the verbatim Python core), while ANY exception out of the
    engaged call is LOUD (it may be post-mutation; the posture is the
    documented idempotent Python re-run — the healer converges, an
    already-healed file reads ``changed=False`` next batch). A crate-side
    disk-full crosses as a real ``OSError(ENOSPC)`` for the caller's
    ``lake_write_guard`` → ``LakeWriteHalt`` mapping; a crate panic crosses
    as the FFI boundary's ``RuntimeError``.

    Parameters
    ----------
    lake_root, quarantine_root
        The batch roots, pre-derived by the Python policy shell (stamp +
        serial + the symlink refusal already ran there).
    findings
        The ORDERED healable findings as plain dicts (the shell serializes
        the dataclasses — deviation D-10).
    on_event
        The ONE replay channel ``(kind, payload) -> None`` — beats, logs,
        audit lines, catalog/stats/manifest side effects (Wave-D house laws
        i-iii: structlog + injected seams stay Python). A raising callback
        propagates out of the crate verbatim.

    Returns
    -------
    dict | None
        The batch outcome (``healed``/``skipped``/``errors``/
        ``quarantined_files``/``actions``/``touched_minute``), or ``None`` =
        run the Python core (guard latch / crate-absent / preflight degrade —
        all ZERO-mutation states).

    Raises
    ------
    RuntimeError
        A crate panic, or a mis-shaped outcome dict from a foreign install
        (refuse-poison — loud, never guessed at: mutation may have happened).
    OSError
        The crate's disk-full class (``ENOSPC``/``EDQUOT``) — the caller
        maps it via ``lake_write_guard``.
    """
    crate = _crate_ready()
    if crate is None:
        return None
    outcome = crate.waved_heal_batch(lake_root, quarantine_root, findings, on_event)
    if not isinstance(outcome, dict):
        raise RuntimeError(
            f"waved_heal_batch returned {type(outcome).__name__} — refuse-poison "
            "(foreign/stale brutex_lake install; re-install the crate wheel)"
        )
    degrade = outcome.get("degrade")
    if degrade is not None:
        # The crate's read-only preflight refused the batch BEFORE touching a
        # byte (a file/destination outside the byte-parity envelope, or — as
        # defense in depth — a foreign future finding kind; since W-2b all
        # six healable kinds are ported) — the sanctioned pre-mutation
        # degrade arm (§B2).
        _warn_once("heal_degrade", degrade_reason=str(degrade))
        return None
    if set(outcome) != _HEAL_OUTCOME_KEYS:
        raise RuntimeError(
            "waved_heal_batch outcome keys refused — refuse-poison "
            "(foreign/stale brutex_lake install; re-install the crate wheel)"
        )
    return outcome


def lake_content_token_via_rust(
    *,
    lake_root: str,
    on_skip: Callable[[str, str, str], None],
    oracle: Callable[[], str],
) -> str:
    """The routed content-token walk — called by ``lake_content_token`` ONLY flag-ON.

    Read-only, so the full W-1 fail-closed doctrine applies: guard latches /
    crate refusals / refuse-poison shapes serve ``oracle()`` (the verbatim
    Python walk, which emits its OWN skip WARNs), and the first
    :data:`BURN_IN_SAMPLES` engaged calls run BOTH walks — a token mismatch
    ships the ORACLE token and autoreverts the whole waved seam. Walk-skip
    events (a raced-away entry/dir) cross as ``{"event", "path", "error"}``
    dicts and are replayed through *on_skip* — ONLY when the oracle did not
    run (never a double WARN), and the caller owns the pinned
    ``lake_heal.token_*_skipped`` emission sites (house law i: structlog
    stays with the module that owns the event).

    HONEST envelope: the token is stat-derived (``mtime_ns``), so a
    concurrent writer moving files BETWEEN the two burn-in walks can produce
    a false mismatch → a spurious (safe, loud) autorevert — the same
    disclosed race class as the W-1 read burn-in.
    """
    crate = _crate_ready()
    if crate is None:
        return oracle()
    try:
        result = crate.waved_lake_content_token(lake_root)
    except degrade_errors() as exc:
        _warn_once("token_error", error_detail=repr(exc))
        return oracle()
    except (KeyboardInterrupt, SystemExit, MemoryError):
        # The escape set (the errors.py doctrine) — never a fallback.
        raise
    except panic_belt_errors() as exc:
        _disable("crate_panic", error_detail=repr(exc))
        return oracle()
    if (
        not isinstance(result, tuple)
        or len(result) != _TOKEN_RESULT_ARITY
        or not isinstance(result[0], str)
        or not isinstance(result[1], list)
    ):
        # Refuse-poison gate: a mis-shaped return is a crate BUG — served
        # loudly from the oracle (structurally unreachable through the real
        # crate; guards a stale/foreign install).
        _warn_once("token_result_refused", result_type=type(result).__name__)
        return oracle()
    token: str = result[0]
    raw_skips = result[1]
    skips: list[tuple[str, str, str]] = []
    for item in raw_skips:
        if (
            not isinstance(item, dict)
            or set(item) != _TOKEN_SKIP_KEYS
            or not all(isinstance(item[key], str) for key in _TOKEN_SKIP_KEYS)
        ):
            # A poisoned skip channel refuses the WHOLE result (the token may
            # be equally untrustworthy) — the oracle serves + warns itself.
            _warn_once("token_events_refused")
            return oracle()
        skips.append((str(item["event"]), str(item["path"]), str(item["error"])))
    if _BURN_IN_REMAINING[SURFACE_CONTENT_TOKEN] > 0:
        # Layer 3 — the burn-in self-check (single-shape surface: the count
        # budget alone; the oracle walk emits its OWN skip WARNs here, so the
        # crate events are deliberately NOT replayed on any burn-in path).
        oracle_token = oracle()
        if token != oracle_token:
            _disable(
                "burn_in_token_mismatch",
                error=True,
                surface=SURFACE_CONTENT_TOKEN,
                rust_token=token,
                python_token=oracle_token,
            )
            return oracle_token
        _BURN_IN_REMAINING[SURFACE_CONTENT_TOKEN] -= 1
        if _BURN_IN_REMAINING[SURFACE_CONTENT_TOKEN] == 0:
            _LOG.info(
                "lake_rust_waved.burn_in_complete",
                surface=SURFACE_CONTENT_TOKEN,
                samples=BURN_IN_SAMPLES,
            )
        return token
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

    Value-equal to ``classify_bar_keys``' output by construction: the same
    lake-relative ``coords.relpath()`` path stamp, the same coords object,
    the same dup-then-foreign order, ``overlap_rows=None`` (the free L1 path
    never reads the canonical sibling) — pinned by the burn-in's
    frozen-dataclass tuple compare and the twin-catalog differential. Lazy
    import: the finding dataclasses live in ``lake_invariants`` (which
    imports polars); this leaf stays polars-free at module scope.
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
    non-64-char hash, an out-of-``datetime``-range span) — the caller treats
    that as refuse-poison and serves the oracle. ``None`` (the crate's
    zero-row verdict) passes through as ``None``.
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
    oracle: Callable[[], WavedFileScan | None],
) -> WavedFileScan | None:
    """The routed per-file reconcile scan — called by ``reconcile_catalog_from_lake`` ONLY flag-ON.

    Read-only, so the full W-1 fail-closed doctrine applies (module
    docstring): guard latches / crate refusals / refuse-poison shapes serve
    ``oracle()`` — the verbatim Python read/hash/classify body, which either
    returns the scan tuple or raises the REAL skip-class exception
    (``PolarsError`` / ``OverflowError`` / ``OSError`` …) the caller's
    GAP-#9 guard maps to the pinned ``catalog.reconcile.parquet_skip``
    verdict with today's message bytes (the degrade-to-oracle law) — and the
    first :data:`BURN_IN_SAMPLES` engaged calls (plus each unseen
    ``classify=`` shape) run BOTH arms, autoreverting the whole waved seam
    on ANY tuple difference (the ``raw_hash`` sha256 equality is the
    run-identity tooth — Golden Rule #4).

    Parameters
    ----------
    path
        The partition file's concrete path (the caller's walked file — the
        crate reads it verbatim, never a glob pattern).
    coords
        The file's already-parsed :class:`PartitionCoords` when the L1
        classify piggyback is on (``check_invariants=True``), else ``None``
        — the crate then skips the dup/foreign counts and the scan carries
        no findings (both arms agree by construction).
    oracle
        The verbatim Python per-file body — the live parity oracle every
        degrade path serves.

    Returns
    -------
    WavedFileScan | None
        ``(row_count, first_ts, last_ts, raw_hash, findings)``, or ``None``
        for a zero-row file (the caller's counted skip).
    """
    crate = _crate_ready()
    if crate is None:
        return oracle()
    classify_home = None if coords is None else (coords.year, coords.month)
    try:
        result = crate.waved_reconcile_file(path, classify_home)
    except degrade_errors() as exc:
        # The degrade-to-oracle law: a crate refusal — including a GENUINE
        # skip-class file (corrupt bytes, an out-of-range stamp) — re-runs
        # the oracle, which raises the real exception with today's message
        # bytes or succeeds; the oracle's verdict always wins.
        _warn_once("reconcile_error", error_detail=repr(exc))
        return oracle()
    except (KeyboardInterrupt, SystemExit, MemoryError):
        # The escape set (the errors.py doctrine) — never a fallback.
        raise
    except panic_belt_errors() as exc:
        _disable("crate_panic", error_detail=repr(exc))
        return oracle()
    try:
        scan = _reconcile_scan_from_result(result, coords)
    except ValueError as exc:
        # Refuse-poison gate: a mis-shaped wire is a crate BUG — served
        # loudly from the oracle (structurally unreachable through the real
        # crate; guards a stale/foreign install).
        _warn_once("reconcile_result_refused", error_detail=repr(exc))
        return oracle()
    shape_token = f"classify={coords is not None}"
    shape_unseen = (
        _BURN_IN_SHAPE_GATE[SURFACE_RECONCILE_FILE]
        and shape_token not in _BURN_IN_SHAPES_SEEN[SURFACE_RECONCILE_FILE]
    )
    if _BURN_IN_REMAINING[SURFACE_RECONCILE_FILE] > 0 or shape_unseen:
        # Layer 3 — the burn-in self-check: both arms scan; ANY tuple
        # difference (counts, span datetimes, the sha256 hex, the finding
        # objects) ships the ORACLE scan and autoreverts the whole waved
        # seam. An oracle RAISE propagates — production-identical on a file
        # the Python arm refuses.
        oracle_scan = oracle()
        if scan != oracle_scan:
            _disable(
                "burn_in_reconcile_mismatch",
                error=True,
                surface=SURFACE_RECONCILE_FILE,
                shape_token=shape_token,
                file_path=path,
            )
            return oracle_scan
        _BURN_IN_SHAPES_SEEN[SURFACE_RECONCILE_FILE].add(shape_token)
        if _BURN_IN_REMAINING[SURFACE_RECONCILE_FILE] > 0:
            _BURN_IN_REMAINING[SURFACE_RECONCILE_FILE] -= 1
            if _BURN_IN_REMAINING[SURFACE_RECONCILE_FILE] == 0:
                _LOG.info(
                    "lake_rust_waved.burn_in_complete",
                    surface=SURFACE_RECONCILE_FILE,
                    samples=BURN_IN_SAMPLES,
                )
        return scan
    return scan


# ────────────────────────────────────────────────────────────────────────
# W-4 — the ``lake_invariants`` whole-lake scan surface
# ────────────────────────────────────────────────────────────────────────

#: The most recent engaged Rust scan's ``_read_key_columns``-equivalent
#: read-attempt count (the Wave-D design §F2 wire counter — the hook the
#: delete-time read-count spy pins rebind onto). ``None`` until an engaged
#: scan serves; reset by :func:`reset_lake_rust_waved_for_tests`.
_LAST_SCAN_READS: Final[list[int | None]] = [None]

#: The logger the scan-event replay writes through — the SAME structlog
#: name as ``lake_invariants._LOG``, so a flag-ON replayed WARN is
#: indistinguishable from the Python arm's at the sink (house law i:
#: structlog stays Python — the crate returns event COORDINATES only).
_LI_LOG = structlog.get_logger("brutex.data.lake_invariants")

#: The two scan log-event names the crate may replay (refuse-poison
#: vocabulary — any other token is a mis-shaped wire, served from the
#: oracle).
_SCAN_LOG_EVENTS: Final[frozenset[str]] = frozenset(
    {"lake.invariant.scan_skip", "lake.invariant.symlink_skipped"}
)

#: The scan wire's ``files_by_segment`` entry arity — ``(segment, count)``.
_SCAN_SEGMENT_PAIR_ARITY: Final[int] = 2

#: The scan wire's ``symbol_dirs`` entry arity — ``(exchange, segment, symbols)``.
_SCAN_SYMBOL_DIRS_ARITY: Final[int] = 3

#: The scan event-record kind tokens (the crate wire contract).
_SCAN_EVENT_KINDS: Final[frozenset[str]] = frozenset({"log", "finding", "drift"})


class _PauseEscape(BaseException):
    """Marker for a raising ``per_file_pause`` callback crossing the seam.

    The facade wraps the injected pause callback so ITS OWN exception —
    whatever class, ``KeyboardInterrupt`` included (hence the
    ``BaseException`` base) — is parked and re-raised VERBATIM by
    :func:`scan_lake_via_rust`, never mistaken for a crate degrade (a pause
    raising ``ValueError`` would otherwise fold into ``degrade_errors()``
    and re-run the whole oracle scan, doubling the callback's side effects
    before the exception finally propagated).
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

    Value-equal to the Python scanner's object by construction (same path
    string, same coords/series/counts, same ``overlap_rows`` including the
    honest ``None`` abstain) — pinned by the burn-in's normalized-report
    compare and the poison-corpus differential. Raises ``ValueError`` on
    ANY junk shape (refuse-poison — the caller serves the oracle).
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
    findings at the crate's ``drift_insert_at`` — exactly the Python
    scanner's position (after the bars tree, before the options tree). The
    report's ``duration_s`` is a placeholder 0.0 — the caller stamps the
    real wall clock at serve time (the burn-in compare pops it either way).
    Raises ``ValueError`` on ANY junk wire shape (refuse-poison).
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
        # ``{key: sorted(names)}`` shape the Python scanner feeds the fold.
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


def _scan_reports_equal(left: LakeScanReport, right: LakeScanReport) -> bool:
    """Order-exact report equality with the wall clock popped.

    STRONGER than the poison-corpus harness's normalized verdict (which
    sorts findings and folds the lake root): both arms scanned the SAME
    lake, so paths compare raw, and the findings ORDER is part of the law
    (the event replay indexes into it). The census is a pure function of
    the report, so report equality implies census equality — comparing it
    here would add cost with zero discrimination power.
    """
    left_raw = dataclasses.asdict(left)
    right_raw = dataclasses.asdict(right)
    left_raw.pop("duration_s")
    right_raw.pop("duration_s")
    return left_raw == right_raw


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
    exact Python log position (dup/price/alien at classification, foreign
    at overlap resolution); the drift marker logs the spliced drift
    findings in order. A crate finding index at/after ``drift_at`` shifts
    past the spliced drift block.

    HONEST (recorded deviation): a skip event's ``error`` text is the
    RUST-side exception rendering — same event name, same path, same count,
    different message bytes than the Python arm's (the harness's "log TEXT
    is deliberately NOT compared" law; readiness doc §3).
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
    """The engaged Rust scan's most recent read-attempt count (§F2 seam).

    ``None`` until an engaged scan serves in this process. The Wave-D
    delete step rebinds the ``_read_key_columns`` read-count spy pins onto
    this counter once the Python core is gone (design §F2 — the wire
    carries it NOW so the pins survive the delete).
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
    oracle: Callable[[], LakeScanReport],
) -> LakeScanReport:
    """The routed whole-lake invariant scan — called by ``scan_lake_invariants`` ONLY flag-ON.

    Read-only, so the full W-1 fail-closed doctrine applies (module
    docstring): guard latches / crate refusals / refuse-poison wire shapes
    serve ``oracle()`` — the verbatim Python scanner. The crate returns the
    findings + counters + an ORDERED event stream; this facade rebuilds the
    frozen dataclasses, runs the Python ``detect_legacy_drift`` fold over
    the crate-enumerated symbol dirs (the fold STAYS Python — house law
    ii), splices its findings at the crate's insert point, and replays the
    log/finding events through the Python sinks (house law i). The first
    :data:`BURN_IN_SAMPLES` engaged calls (plus each unseen shape token)
    run BOTH arms and compare the reports order-exact (wall clock popped);
    ANY difference ships the ORACLE report and autoreverts the whole waved
    seam. Burn-in calls SERVE the oracle report (its logs already fired —
    replaying the crate stream too would double-log) and tick the injected
    pause callback in BOTH arms (the read_1m double-run property,
    documented).

    A raising *per_file_pause* re-raises VERBATIM (class + message) via the
    :class:`_PauseEscape` parking wrap — never folded into the degrade set.
    """
    crate = _crate_ready()
    if crate is None:
        return oracle()
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
        raise RuntimeError("scan pause escape without a captured exception") from marker
    except degrade_errors() as exc:
        # The degrade-to-oracle law: a crate refusal (an int()-ambiguous dir
        # name, an unmappable dtype, a non-UTF-8 path …) re-runs the oracle,
        # whose verdict — findings, skips, or a raise — always wins.
        _warn_once("scan_error", error_detail=repr(exc))
        return oracle()
    except (KeyboardInterrupt, SystemExit, MemoryError):
        # The escape set (the errors.py doctrine) — never a fallback.
        raise
    except panic_belt_errors() as exc:
        _disable("crate_panic", error_detail=repr(exc))
        return oracle()
    try:
        report, events, drift_at, drift_len, reads = _scan_report_from_wire(
            wire, lake_root=lake_root, canonical_keys=canonical_keys, include_bars=include_bars
        )
    except ValueError as exc:
        # Refuse-poison gate: a mis-shaped wire is a crate BUG — served
        # loudly from the oracle.
        _warn_once("scan_result_refused", error_detail=repr(exc))
        return oracle()
    shape_token = f"bars={include_bars}/options={include_options}/scoped={months is not None}"
    shape_unseen = (
        _BURN_IN_SHAPE_GATE[SURFACE_SCAN] and shape_token not in _BURN_IN_SHAPES_SEEN[SURFACE_SCAN]
    )
    if _BURN_IN_REMAINING[SURFACE_SCAN] > 0 or shape_unseen:
        # Layer 3 — the burn-in self-check. The ORACLE report is served
        # either way: its logs fired naturally inside oracle(), so the
        # crate event stream is deliberately NOT replayed on a burn-in call
        # (no double-log); the engaged replay path is exercised by the
        # steady-state differential with the burn-in disarmed.
        oracle_report = oracle()
        if not _scan_reports_equal(report, oracle_report):
            _disable(
                "burn_in_scan_mismatch",
                error=True,
                surface=SURFACE_SCAN,
                shape_token=shape_token,
                lake=str(lake_root),
            )
            return oracle_report
        _BURN_IN_SHAPES_SEEN[SURFACE_SCAN].add(shape_token)
        if _BURN_IN_REMAINING[SURFACE_SCAN] > 0:
            _BURN_IN_REMAINING[SURFACE_SCAN] -= 1
            if _BURN_IN_REMAINING[SURFACE_SCAN] == 0:
                _LOG.info(
                    "lake_rust_waved.burn_in_complete",
                    surface=SURFACE_SCAN,
                    samples=BURN_IN_SAMPLES,
                )
        return oracle_report
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
    "BURN_IN_SAMPLES",
    "LAKE_WAVED_RUST_ENV",
    "SURFACE_CONTENT_TOKEN",
    "SURFACE_READ_1M",
    "SURFACE_RECONCILE_FILE",
    "SURFACE_SCAN",
    "env_flag_tristate",
    "heal_batch_via_rust",
    "lake_content_token_via_rust",
    "lake_rust_waved_enabled",
    "last_rust_scan_reads",
    "read_1m_via_rust",
    "reconcile_file_via_rust",
    "reset_lake_rust_waved_for_tests",
    "rust_waved_disabled",
    "scan_lake_via_rust",
]
