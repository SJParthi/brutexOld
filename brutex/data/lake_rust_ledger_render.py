"""lake_rust_ledger_render — the default-OFF Rust routing for the window-ledger parquet render.

DEC-LAKE-RUST-LEDGERSTORE-CONVERT-001 (ADR-0020 slice G10 CONVERT;
MIGRATION.md #1918 — evidence predecessor: the #1917 spike,
``docs/lake/g10_window_ledger_byte_parity/``). When — and ONLY when —
``BRUTEX_LAKE_RUST_LEDGER_RENDER`` is set truthy, the window-ledger store's
ONE shared parquet render (``window_ledger_store._ledger_parquet_bytes`` —
the body both ``append_ledger_delta`` and ``save_window_ledger`` write
through) routes its 18-column row table through the ``brutex_lake`` crate's
``render_ledger_parquet_from_ipc`` (the rev-pinned polars streaming-sink
write path), with the verbatim pyarrow render retained as the LIVE oracle
(``window_ledger_store._render_ledger_parquet_python``).

## The standard is ROW-EQUIVALENCE + reader parity, never bytes

Per the operator ruling of 2026-07-16 as relayed via the coordinator:
byte-identity with the pyarrow production bytes is MEASURED-INFEASIBLE —
the #1917 spike swept 14 polars-native arms × 7 production-writer corpus
cases with ZERO sha256 matches, the divergence being ENCODER IDENTITY
(``created_by`` · the footer format-version marker · the ``ARROW:schema``
flatbuffer bytes · the first data-page header thrift · the empty-file
1-vs-0 row-group structure), none an exposed polars knob — and golden
re-baseline is FORBIDDEN. NO consumer hashes or byte-prefixes ledger bytes
(verified: the restore-drill byte-PREFIX law is audit-JSONL-only; the
off-box backup sha256s the DESTINATION copy self-consistently;
``lake_revision`` / ``lake_manifest`` read only ``(mtime_ns:size)`` stat
tokens — a rewrite moving them is EXPECTED), so the sound + sufficient
contract is the store module's OWN documented row-level fixed point
(``window_ledger_store.py:16``): identical ordered 18-column rows through
every production reader (pyarrow · polars · DuckDB — the reader-parity
gates, ``tests/integration/test_window_ledger_rust_reader_parity.py``).
The burn-in below therefore compares ROW EQUIVALENCE, never bytes — the
ONE deliberate, disclosed deviation from the PR-1
:mod:`~brutex.data.lake_rust_render` template's byte compare.

## The fail-closed layers (the PR-1 template, adapted)

* **Flag** — ``BRUTEX_LAKE_RUST_LEDGER_RENDER`` default OFF; truthy → ON,
  falsy → OFF, any other non-empty value → OFF with ONE loud
  ``lake_rust_ledger_render.fallback`` WARN (``flag_unrecognised``). OFF
  never imports the crate and the store's oracle render runs directly —
  byte-identical to the pre-conversion writer (bomb-spy-pinned).
* **Import guard / SYMBOL handshake** — an absent wheel latches
  ``crate_unavailable``; a wheel that imports but lacks
  ``render_ledger_parquet_from_ipc`` is a PRE-G10 (or foreign/broken)
  install and latches ``crate_stale`` (the G8
  :mod:`~brutex.data.window_ledger_rust` handshake law). Both serve the
  oracle with one loud WARN.
* **Version guard** — unlike the G8 pure laws this render IS polars, so
  the PR-1 polars-lockstep guard applies verbatim: the installed
  ``polars.__version__`` must equal the crate's
  ``pinned_polars_version()`` or the seam latches
  ``polars_version_mismatch`` naming both. A raising metadata call
  latches ``crate_metadata_error``; an unimportable polars (the store
  itself only needs pyarrow) latches ``polars_unavailable``.
* **Refuse-poison gate** — a non-``bytes`` / EMPTY crate return is
  refused to the oracle (``render_result_refused``): never persisted,
  never a new crash class into a ledger save.
* **Degrade set / panic belt / OOM doctrine** — the sibling facade's
  :func:`~brutex.data.lake_rust_render.degrade_errors` +
  :func:`~brutex.data.lake_rust_render.panic_belt_errors` (imported, ONE
  copy — §11 COMMON); ``KeyboardInterrupt``/``SystemExit``/``MemoryError``
  re-raise RAW in a clause ABOVE the belt. The store's write sites run
  this render INSIDE :func:`~brutex.data.atomic_io.atomic_write_via`'s
  guarded callable, so a raw ``MemoryError`` here still maps to
  ``LakeWriteHalt("out_of_memory")`` exactly as on the pure-Python path
  (DEC-WINDOW-LEDGER-WRITE-HALT-001 — semantics byte-identical).

## Burn-in + autorevert (ROW-EQUIVALENCE, the G10 twist)

The first :data:`BURN_IN_SAMPLES` engaged renders per process read BOTH
byte buffers back through pyarrow and compare (1) SCHEMA equality —
names + dtypes + order — under the ONE documented STRING-FAMILY
normalization, and (2) ordered-ROW equality (``to_pylist() ==``). Any
difference ships the ORACLE bytes, latches the Rust path OFF and logs
``lake_rust_ledger_render.render_autoreverted`` at ERROR.

**The string-family normalization (empirically measured 2026-07-17,
pyarrow 25.0.0 / polars 1.42.1):** polars maps its ``String`` dtype to
Arrow ``large_string`` in the embedded ``ARROW:schema``, so a pyarrow
READBACK of the crate's file reports ``large_string`` where the pyarrow
oracle's file reports ``string`` — a representation-metadata artifact of
the SAME logical string column (parquet physical BYTE_ARRAY/UTF8 either
way; DuckDB describes VARCHAR either way; ``to_pylist()`` yields
identical ``str`` objects — the #1917 cross-encoder row fixed point).
The compare folds ``large_string``/``string_view`` → ``string`` and
NOTHING else — the critical watch-item survives at full strength: a
``last_attempt_at`` rendered as ANY timestamp dtype (which would silently
change the DuckDB ``last_attempt_at >= ?`` VARCHAR-compare surface,
``db_view._window_counts_since``) still mismatches → autorevert.

There is NO schema-shape gate (the PR-1 layer-3 extension): the ledger
writer has exactly ONE 18-column schema by construction
(``window_ledger_store._rows_to_table`` builds every file), unlike the
option heal path whose stored-schema space is open-ended.

## Log events (all literal — greppable)

* ``lake_rust_ledger_render.fallback`` (WARN, once per process per
  ``reason`` ∈ {``flag_unrecognised``, ``render_error``,
  ``render_result_refused``}) — the oracle bytes served.
* ``lake_rust_ledger_render.rust_path_disabled`` (WARN, once — ``reason``
  ∈ {``crate_unavailable``, ``crate_stale``, ``crate_metadata_error``,
  ``polars_unavailable``, ``polars_version_mismatch``, ``crate_panic``}).
* ``lake_rust_ledger_render.render_autoreverted`` (ERROR, once —
  ``reason`` ∈ {``burn_in_row_mismatch``, ``burn_in_compare_error``}) —
  the row-equivalence burn-in tripped; oracle bytes shipped, latch set.
* ``lake_rust_ledger_render.engaged`` (INFO, once) +
  ``lake_rust_ledger_render.burn_in_complete`` (INFO, once).

## Honesty (ADR-0020 banner; CLAUDE.md Golden Rule #10)

Expected payoff ≈ 0 — conversion is for uniformity, not speed. The
engaged render pays one polars frame build + one uncompressed-IPC copy +
one FFI crossing the pyarrow path does not (a small SLOWDOWN; the seam
runs per throttled ledger flush on an I/O-dominated pull path and ships
DEFAULT-OFF — a default flip needs its own DEC). During burn-in BOTH
engines render AND both buffers are decoded back for the compare — the
first 16 flushes cost ~3× a plain render, bounded + amortised-nil. The
latch/memo cells are the same documented no-global-mutable-state
exception as every sibling facade's. The flag is read per call so spawn
workers resolve their own environment.
"""

from __future__ import annotations

import importlib
import os
from typing import TYPE_CHECKING, Any, Final

import structlog

# §11 COMMON — ONE flag vocabulary + ONE degrade set + ONE panic belt for
# the brutex-lake facades (the sibling's PUBLIC re-exports; the G8 facade's
# same-package-import precedent). The per-facade LATCH/warn state stays
# per-facade by design — a ledger autorevert must never silence the bars
# render seam and vice versa.
from brutex.data.lake_rust_render import (
    degrade_errors,
    env_flag_tristate,
    ipc_handoff_bytes,
    panic_belt_errors,
)

if TYPE_CHECKING:  # pragma: no cover - import only for static typing
    from collections.abc import Callable, Sequence
    from types import ModuleType

    import pyarrow as pa

_LOG = structlog.get_logger("brutex.data.lake_rust_ledger_render")

#: The opt-in env flag (default OFF; junk fails loud-OFF).
LAKE_RUST_LEDGER_RENDER_ENV: Final[str] = "BRUTEX_LAKE_RUST_LEDGER_RENDER"

#: Engaged renders ROW-EQUIVALENCE-compared against the live pyarrow oracle
#: per process before crate bytes ship uncompared (the #1531/#1532 pattern;
#: 16 mirrors every sibling facade).
BURN_IN_SAMPLES: Final[int] = 16

#: The 10 STRING columns of the ledger schema, in :data:`LEDGER_COLUMNS`
#: order positions — ``pa.string()`` cells in the oracle table
#: (``window_ledger_store._rows_to_table``), ``pl.String`` in the engaged
#: frame. ``first_seen_at`` / ``last_attempt_at`` are DELIBERATELY strings
#: (ISO-8601 UTC — the lexicographic-compare law; the module-docstring
#: watch-item), never timestamps.
_LEDGER_STRING_COLUMNS: Final[frozenset[str]] = frozenset(
    {
        "market",
        "security_id",
        "interval",
        "window_start",
        "window_end",
        "status",
        "last_error",
        "first_seen_at",
        "last_attempt_at",
        "streak_provenance",
    }
)

#: The full 18-column on-disk order — MUST equal
#: ``window_ledger_store._COLUMNS`` (pinned by test, not imported: this leaf
#: must stay importable by the store without a cycle, the
#: ``db_view._LEDGER_ROW_COLUMNS`` precedent).
LEDGER_COLUMNS: Final[tuple[str, ...]] = (
    "market",
    "security_id",
    "interval",
    "window_start",
    "window_end",
    "status",
    "bars",
    "attempts",
    "last_error",
    "first_seen_at",
    "last_attempt_at",
    "empty_streak",
    "in_pull_fetches",
    "bars_net_new",
    "empty_retries",
    "transient_retries",
    "streak_provenance",
    # DEC-AUTO-RESUME-ZERO-SURPRISES-001 appended the running counter LAST —
    # an Int64 column (the store schema's ``pa.int64()``), so it deliberately
    # does NOT join :data:`_LEDGER_STRING_COLUMNS`.
    "failed_streak",
)

#: Process-local OFF latch — the disable reason, or ``None`` when live (the
#: documented no-global-mutable-state exception, the sibling facades' cell).
_DISABLED_REASON: Final[list[str | None]] = [None]

#: Burn-in renders still to row-compare this process (single budget — ONE
#: fixed schema, no per-profile/per-law split; reset seam below).
_BURN_IN_REMAINING: Final[list[int]] = [BURN_IN_SAMPLES]

#: The crate API surface the import guard verifies. The RENDER symbol is the
#: G10 stale-wheel handshake (a pre-G10 wheel imports but lacks it →
#: ``crate_stale``); the metadata callable feeds the polars version guard.
_CRATE_API: Final[tuple[str, ...]] = (
    "render_ledger_parquet_from_ipc",
    "pinned_polars_version",
)

#: Once-per-process-per-reason warn memo + the engaged-once key. LOG-VOLUME
#: state only — rendered bytes never depend on it.
_OBSERVABILITY_MEMO: Final[set[str]] = set()

#: Memo key for the engaged-once INFO (namespaced apart from warn reasons).
_ENGAGED_KEY: Final[str] = "__engaged__"

#: Sentinel for "the metadata call raised" — distinct from any value a
#: foreign crate could return (the sibling's `_METADATA_ERROR` shape).
_METADATA_ERROR: Final[object] = object()


def lake_rust_ledger_render_enabled() -> bool:
    """Resolve ``BRUTEX_LAKE_RUST_LEDGER_RENDER`` (default OFF, junk loud-OFF).

    Truthy literal → ``True``; falsy literal → ``False``; unset/blank →
    ``False`` (the default); any OTHER non-empty value → ``False`` with one
    ``lake_rust_ledger_render.fallback`` WARN (``flag_unrecognised``) per
    process. Read per call so spawn workers resolve their own environment
    (byte-identical vocabulary via the imported :func:`env_flag_tristate`).
    """
    raw = os.environ.get(LAKE_RUST_LEDGER_RENDER_ENV, "")
    verdict = env_flag_tristate(raw)
    if verdict is not None:
        return verdict
    if raw.strip():
        _warn_once("flag_unrecognised", value=raw)
    return False


def rust_ledger_render_disabled() -> bool:
    """``True`` iff a guard/autorevert latched the Rust path OFF this process."""
    return _DISABLED_REASON[0] is not None


def reset_lake_rust_ledger_render_for_tests(*, remaining: int = BURN_IN_SAMPLES) -> None:
    """Reset the latch + burn-in budget + warn memo (test isolation ONLY).

    ``remaining=0`` puts the process in the post-burn-in steady state — the
    crate bytes ship with no oracle decode in the path (the reader-parity
    corpora depend on that: an armed burn-in would cushion a divergence by
    shipping oracle bytes). The default restores a fresh-process view.
    Never called from production code paths (the sibling reset-seam
    convention).
    """
    _DISABLED_REASON[0] = None
    _BURN_IN_REMAINING[0] = remaining
    _OBSERVABILITY_MEMO.clear()


def _warn_once(reason: str, **context: object) -> None:
    """Log ``lake_rust_ledger_render.fallback`` WARN once per process per *reason*."""
    if reason in _OBSERVABILITY_MEMO:
        return
    _OBSERVABILITY_MEMO.add(reason)
    _LOG.warning("lake_rust_ledger_render.fallback", reason=reason, **context)


def _disable(reason: str, *, error: bool = False, **context: object) -> None:
    """Latch the Rust path OFF for the remainder of the process.

    ``error=True`` logs ``lake_rust_ledger_render.render_autoreverted`` at
    ERROR (the burn-in autorevert — a genuine wrongness signal); guard
    latches log ``lake_rust_ledger_render.rust_path_disabled`` at WARN.
    Repeat calls are no-ops (the latch is already set).
    """
    if _DISABLED_REASON[0] is not None:
        return
    _DISABLED_REASON[0] = reason
    if error:
        _LOG.error("lake_rust_ledger_render.render_autoreverted", reason=reason, **context)
    else:
        _LOG.warning("lake_rust_ledger_render.rust_path_disabled", reason=reason, **context)


def _resolve_crate() -> ModuleType | None:
    """Import guard + SYMBOL handshake: ``brutex_lake``, or latch OFF + ``None``.

    An absent wheel (``ImportError``) latches ``crate_unavailable``. A wheel
    that imports but lacks any :data:`_CRATE_API` callable is a PRE-G10 (or
    foreign/broken) install — latch ``crate_stale`` (the G8 handshake law:
    the G10 render symbol landed AFTER the PR-1/G8 surfaces, so symbol
    absence means "rebuild the wheel", a distinct remedy from "install
    it"). Fail-closed: never an ``AttributeError`` into a ledger save.
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
                "the installed wheel predates the G10 ledger render (or is foreign/broken)",
                remedy="rebuild + reinstall rust/brutex-lake "
                "(see docs/runbooks/lake-rust-render-lockstep.md)",
            )
            return None
    return crate


def _crate_pinned_version(crate: ModuleType) -> object:
    """Call ``pinned_polars_version()`` defensively; latch OFF on ANY raise.

    A raising metadata pyfunction marks a foreign/stale install whose bytes
    must not be trusted — latch ``crate_metadata_error`` (→
    :data:`_METADATA_ERROR`) and serve the oracle (the sibling's
    ``_crate_metadata`` law; a LOCAL twin because the shared helper latches
    the SIBLING's cell, not this facade's). ``MemoryError`` re-raises bare.
    Returns the raw value un-typechecked: a non-str simply fails the ``!=``
    version guard, fail-closed already.
    """
    try:
        return crate.pinned_polars_version()
    except MemoryError:
        raise
    except Exception as exc:
        _disable("crate_metadata_error", error_detail=repr(exc))
        return _METADATA_ERROR


def _ledger_frame(rows: Sequence[tuple[Any, ...]], pl: ModuleType) -> Any:
    """Build the 18-column polars frame from the store's row tuples.

    The engaged-path twin of ``window_ledger_store._rows_to_table``: the
    SAME :data:`LEDGER_COLUMNS` order (test-pinned against the store), the
    SAME logical dtypes (``pl.String`` where the oracle schema says
    ``pa.string()``, ``pl.Int64`` where it says ``pa.int64()``), built
    ``strict=True`` so a mis-typed cell raises (→ the caller's degrade set)
    instead of coercing silently. A 0-row input builds a valid EMPTY
    18-column frame (the empty-ledger save shape).
    """
    schema = {
        name: (pl.String if name in _LEDGER_STRING_COLUMNS else pl.Int64) for name in LEDGER_COLUMNS
    }
    return pl.DataFrame(list(rows), schema=schema, orient="row", strict=True)


def _normalized_dtypes(schema: pa.Schema) -> list[tuple[str, Any]]:
    """The burn-in schema view: (name, dtype) in order, string-family folded.

    The ONE documented normalization (module docstring): pyarrow reads the
    crate's file with ``large_string`` columns (polars' ``ARROW:schema``
    metadata) where the oracle's file reads ``string`` — the same logical
    column. ``large_string``/``string_view`` fold to ``string``; EVERY other
    dtype compares EXACTLY, so a timestamp-typed ``last_attempt_at`` (the
    critical watch-item) still mismatches → autorevert.
    """
    import pyarrow as pa_mod

    folded: list[tuple[str, Any]] = []
    for field in schema:
        dtype = field.type
        if dtype in (pa_mod.large_string(), pa_mod.string_view()):
            dtype = pa_mod.string()
        folded.append((field.name, dtype))
    return folded


def _row_equivalence_mismatch(rust_bytes: bytes, oracle_bytes: bytes) -> str | None:
    """Compare the two rendered buffers for ROW EQUIVALENCE via pyarrow readback.

    Returns ``None`` when equivalent, else the mismatch kind (``"schema"`` /
    ``"rows"``). Schema = names + normalized dtypes + ORDER
    (:func:`_normalized_dtypes`); rows = ordered ``to_pylist()`` equality
    (per-row dicts — identical Python values in identical row order). Never
    a byte compare — the module-docstring standard.
    """
    import io

    import pyarrow.parquet as pq

    rust_table = pq.read_table(io.BytesIO(rust_bytes))  # type: ignore[no-untyped-call, unused-ignore]
    oracle_table = pq.read_table(io.BytesIO(oracle_bytes))  # type: ignore[no-untyped-call, unused-ignore]
    if _normalized_dtypes(rust_table.schema) != _normalized_dtypes(oracle_table.schema):
        return "schema"
    if rust_table.to_pylist() != oracle_table.to_pylist():
        return "rows"
    return None


def render_ledger_parquet_bytes(
    *,
    rows: Sequence[tuple[Any, ...]],
    python_render: Callable[[], bytes],
) -> bytes:
    """The routed ledger render — called by the store seam ONLY when the flag is ON.

    Runs the fail-closed layers (module docstring) and returns the parquet
    bytes the store persists. EVERY path returns valid bytes: a latched /
    guarded / degraded call returns ``python_render()`` (the live pyarrow
    oracle), a burn-in mismatch returns the ORACLE bytes, and only a
    guard-clean, burn-in-clean call returns the Rust bytes. The store's
    write sites invoke this INSIDE :func:`~brutex.data.atomic_io.atomic_write_via`'s
    guarded callable, so a ``MemoryError`` anywhere in here maps to
    ``LakeWriteHalt`` exactly as on the pure-Python path.

    Parameters
    ----------
    rows
        The serialised 18-field ledger row tuples
        (:meth:`WindowLedger.to_rows` shape) — the SAME sequence the oracle
        table is built from; the engaged path builds the polars frame from
        them (:func:`_ledger_frame`).
    python_render
        Zero-arg callable running the verbatim pyarrow render for *rows* —
        the live row-equivalence oracle
        (``window_ledger_store._render_ledger_parquet_python``).
    """
    if rust_ledger_render_disabled():
        return python_render()
    crate = _resolve_crate()
    if crate is None:
        return python_render()
    try:
        import polars as pl
    except ImportError as exc:
        # The store itself only needs pyarrow — polars is the [data]-extra
        # sibling the ENGAGED path additionally requires (frame build + IPC
        # handoff + the crate's own linked polars). Absent → fail-closed.
        _disable("polars_unavailable", error_detail=repr(exc))
        return python_render()
    pinned = _crate_pinned_version(crate)
    if pinned is _METADATA_ERROR:
        return python_render()
    if pinned != pl.__version__:
        # The polars-lockstep version guard (the PR-1 layer 2, verbatim law):
        # fail-closed, ONE loud WARN naming BOTH versions — never silently
        # different bytes from a drifted wheel/crate pair.
        _disable(
            "polars_version_mismatch",
            installed_polars=pl.__version__,
            crate_pinned_polars=pinned,
            remedy="align the wheel and the crate's pinned polars rev, then "
            "re-run the reader-parity gates "
            "(docs/runbooks/lake-rust-render-lockstep.md)",
        )
        return python_render()
    try:
        # DELIBERATE template deviation (disclosed): the frame construction
        # (`_ledger_frame`) runs INSIDE this catch — the PR-1 template has
        # the CALLER build the frame outside it. Fail-closed on purpose: a
        # construction fault (a mis-typed cell under strict=True, a polars
        # raise) degrades to the oracle — correct bytes, one `render_error`
        # warn-once — and construction DRIFT is caught by the CI parity
        # lane's anti-vacuous bytes-differ pins + the LEDGER_COLUMNS /
        # string-int-split drift pins, never a new crash class into a save.
        rust_bytes = crate.render_ledger_parquet_from_ipc(
            ipc_handoff_bytes(_ledger_frame(rows, pl))
        )
    except degrade_errors() as exc:
        _warn_once("render_error", error_detail=repr(exc))
        return python_render()
    except (KeyboardInterrupt, SystemExit, MemoryError):
        # The escape set (the errors.py doctrine): operator interrupt /
        # interpreter shutdown / OOM stay global — OOM in particular must
        # reach the caller's lake_write_guard → LakeWriteHalt, never a
        # fallback. Sits ABOVE the panic belt so the belt's `Exception`
        # member can never swallow a MemoryError.
        raise
    except panic_belt_errors() as exc:
        # The narrow FFI panic belt over the crate's own catch_unwind — a
        # `pyo3_runtime.PanicException` that nonetheless escapes latches
        # OFF and serves the oracle (fail-closed, never a new crash class
        # into a ledger save; the sibling facade's belt, imported).
        _disable("crate_panic", error_detail=repr(exc))
        return python_render()
    if not isinstance(rust_bytes, bytes) or not rust_bytes:
        # Refuse-poison gate: a non-bytes / EMPTY return is a crate BUG —
        # never persisted (an empty file is not a parquet), served loudly
        # from the oracle. Structurally unreachable through the real crate
        # (its FFI constructs PyBytes from a rendered PAR1 buffer).
        _warn_once("render_result_refused", result_type=type(rust_bytes).__name__)
        return python_render()
    if _ENGAGED_KEY not in _OBSERVABILITY_MEMO:
        _OBSERVABILITY_MEMO.add(_ENGAGED_KEY)
        _LOG.info(
            "lake_rust_ledger_render.engaged",
            crate_pinned_polars=pinned,
            crate_version=getattr(crate, "__version__", None),
        )
    if _BURN_IN_REMAINING[0] > 0:
        # Burn-in self-check — ROW EQUIVALENCE, never bytes (module
        # docstring): both engines render; the buffers are read back through
        # pyarrow and compared on normalized schema + ordered rows. ANY
        # difference ships the ORACLE bytes and autoreverts for the process.
        oracle_bytes = python_render()
        try:
            mismatch = _row_equivalence_mismatch(rust_bytes, oracle_bytes)
        except MemoryError:
            raise
        except Exception as exc:
            # A compare that cannot even DECODE the buffers (a corrupt crate
            # render pyarrow refuses) is itself disqualifying evidence —
            # fail-closed: latch + ship the oracle bytes.
            _disable("burn_in_compare_error", error=True, error_detail=repr(exc))
            return oracle_bytes
        if mismatch is not None:
            _disable(
                "burn_in_row_mismatch",
                error=True,
                mismatch=mismatch,
                rust_len=len(rust_bytes),
                python_len=len(oracle_bytes),
                rows=len(rows),
            )
            return oracle_bytes
        _BURN_IN_REMAINING[0] -= 1
        if _BURN_IN_REMAINING[0] == 0:
            _LOG.info(
                "lake_rust_ledger_render.burn_in_complete",
                samples=BURN_IN_SAMPLES,
            )
    return rust_bytes


__all__ = [
    "BURN_IN_SAMPLES",
    "LAKE_RUST_LEDGER_RENDER_ENV",
    "LEDGER_COLUMNS",
    "lake_rust_ledger_render_enabled",
    "render_ledger_parquet_bytes",
    "reset_lake_rust_ledger_render_for_tests",
    "rust_ledger_render_disabled",
]
