"""Flag-gated Rust parquet-render seam — the ADR-0020 PR-1 facade.

DEC-RUST-DATALAKE-IO-ROADMAP-001 (MIGRATION.md #1726). When — and ONLY when —
``BRUTEX_LAKE_RUST_RENDER`` is set truthy, :meth:`ParquetWriter._render_bytes`
routes its frame → parquet-bytes render through the ``brutex_lake`` crate (a
Rust build of polars REV-pinned at the commit the installed Python wheel's
release tag pointed at — tags are movable, so the manifest pins the commit;
the frame crosses as uncompressed Arrow IPC bytes). This module owns
the flag, the three fail-closed layers, and the process-local latches; the
crate side lives in ``rust/brutex-lake/src/lib.rs``.

## The flag — ``BRUTEX_LAKE_RUST_RENDER`` (DEFAULT OFF)

Resolved per call like the sibling sweep flags: a truthy literal
(``1/true/yes/on``) → ON; a falsy literal (``0/false/no/off``) → OFF; unset or
whitespace-empty → the default (OFF); any OTHER value → OFF with one loud
``lake_rust_render.fallback`` WARN (reason ``flag_unrecognised``) per
process — a typo'd opt-in is never silently honoured, and (since the
default is OFF) never silently armed either. The truthy/falsy vocabulary DELIBERATELY mirrors the sweep
flags' COMMON :func:`brutex.engine.sweep_lookahead.env_flag_tristate`
byte-for-byte, but the classifier is a LOCAL twin: importing anything under
``brutex.engine`` executes ``brutex/engine/__init__.py``, which hard-imports
the ``fno_math`` wheel (``engine/combo_mask.py``) — a LAKE WRITE must never
require the ENGINE wheel. The mirror is pinned by a vocabulary-equality test
(``tests/unit/test_lake_rust_render.py``) that compares both literal sets
whenever the engine package is importable, so the two homes cannot drift
silently. (The same wheel-boundary reasoning is why
``brutex/data/write_consumer.py`` carries its own truthy set.)

## Three fail-closed layers (ADR-0020 §3 PR-1 gate set), all process-local

1. **Import guard** — the crate is resolved lazily on first engaged render;
   an absent/broken ``brutex_lake`` install logs ONE
   ``lake_rust_render.crate_unavailable`` WARN and LATCHES the seam OFF for
   the process. The resolved module's API surface is VERIFIED (all three
   callables present — a name-collision / hand-broken ``brutex_lake`` that
   imports but lacks the API latches the SAME ``crate_unavailable``, never
   an ``AttributeError`` into the pull spine), and a metadata pyfunction
   that RAISES latches ``crate_metadata_error`` (a foreign/stale install
   must fail closed, not crash). Every render (including the failed one)
   serves the Python bytes.
2. **Version guard** — the installed ``polars.__version__`` must equal the
   crate's ``pinned_polars_version()``. Any difference logs ONE loud
   ``lake_rust_render.polars_version_mismatch`` WARN naming BOTH versions
   and latches OFF. Fail-closed: the repo range-pins ``polars>=1.0,<2.0``
   with no lockfile (ADR-0020 F4), so a wheel bump past the crate must
   REFUSE — never silently different bytes (§7 hazard 1).
3. **Burn-in self-check + autorevert** — the first :data:`BURN_IN_SAMPLES`
   engaged renders per process ALSO run the Python render and byte-compare.
   A mismatch logs ``lake_rust_render.render_autoreverted`` (ERROR — a
   genuine wrongness signal), SHIPS THE PYTHON BYTES (correctness first;
   ``raw_hash`` and the on-disk partition see only oracle bytes), and
   latches OFF for the process. After 16 clean compares
   (``lake_rust_render.burn_in_complete``), Rust bytes ship directly. The
   pattern is the sweep-metrics kernel's burn-in verbatim
   (DEC-SWEEP-EXIT-INDEX-FASTPATH-001 / DEC-SWEEP-RUST-METRICS-DEFAULT-ON-001
   — see ``brutex/engine/rust_metrics.py``), EXTENDED with a SCHEMA-SHAPE
   gate: the count budget alone would let a process that burnt in on 16
   spot partitions ship crate bytes for its FIRST-EVER Greeks-overlay
   partition with zero byte witness, so the caller passes a
   ``schema_token`` (the frame's column/dtype fingerprint) and every
   DISTINCT shape is byte-compared at least once before crate bytes ship
   for it — even after the count budget is spent. The production token
   space is the writer's two schema shapes (7-column spot / 17-column
   Greeks overlay — the overlay is all-or-nothing), so the seen-set is
   bounded by construction; a ``None`` token honestly degrades that call
   to the count budget alone.

## Log events (all literal — greppable)

* ``lake_rust_render.fallback`` (WARN, once per process per ``reason`` ∈
  {``flag_unrecognised``, ``render_error``, ``render_result_refused``}) —
  the Python bytes served.
* ``lake_rust_render.rust_path_disabled`` (WARN, once — ``reason`` ∈
  {``crate_unavailable``, ``crate_metadata_error``,
  ``polars_version_mismatch``, ``crate_panic``}) — the guard latch.
* ``lake_rust_render.render_autoreverted`` (ERROR, once) — the burn-in
  byte-compare mismatch; oracle bytes shipped, latch set.
* ``lake_rust_render.engaged`` (INFO, once) — the first Rust-served render
  (the observable "the wired ON path actually runs Rust" signal).
* ``lake_rust_render.burn_in_complete`` (INFO, once) — 16 clean compares.

A Rust-path render error (crate ``ValueError``/``RuntimeError`` — including
a crate-side panic, which the crate catches at the FFI boundary and maps to
``RuntimeError`` — or a polars error from the IPC serialisation, or an
``AttributeError`` from a broken crate internal) degrades to the Python
render with one ``lake_rust_render.render_error`` WARN per process: NEVER a
new crash class — the Python oracle is always available. Belt on top of the
crate's own boundary: the render carries a NARROW panic belt —
``(Exception, <the resolved pyo3 PanicException class(es)>)`` via
:func:`_panic_belt_errors`, never a literal ``except BaseException`` (the
repo convention behind CodeQL ``py/catch-base-exception``: only an
UNCONDITIONALLY re-raising handler may catch ``BaseException``) — so a
``pyo3_runtime.PanicException`` that nonetheless escapes a pyo3 boundary
(a ``BaseException`` subclass no ``except Exception`` sees) latches
``crate_panic`` and serves the oracle, as does any out-of-degrade-set
``Exception``; ``KeyboardInterrupt``/``SystemExit``/``MemoryError``
re-raise immediately (the ``errors.py`` escape-set doctrine). HONEST
narrowing (deliberate, disclosed): the panic class is resolved by NAME from
``pyo3_runtime``/``polars.exceptions`` — a panic surfacing through a
DIFFERENT statically-linked pyo3 runtime whose ``PanicException`` is a
distinct type object (per-extension pyo3 statics) would now PROPAGATE
instead of latching; bounded by the import/API-surface/metadata/version
guards that precede every render and by the crate's own ``catch_unwind``
FFI boundary (its panics map to ``RuntimeError``, the degrade set) — the
belt is defence-in-depth for a doubly-broken contract, not the primary
rail.

**OOM honesty (scoped precisely):** a PYTHON-SURFACED ``MemoryError``
(the BytesIO/object-list allocations, the IPC handoff) is NEVER caught
here: it propagates into the caller's ``lake_write_guard`` and maps to
``LakeWriteHalt`` exactly as on the pure-Python path
(DEC-LAKE-WRITE-HALT-001). A Rust-encoder-INTERNAL allocation failure is a
DIFFERENT class and is NOT gracefully mapped on EITHER side: the wheel's
own ``write_parquet`` is the same Rust polars and shares it (allocator
abort, or a panic — which on the crate side maps to ``RuntimeError`` at the
FFI boundary and degrades to the oracle; an allocator ABORT kills the
process on both paths). The seam neither narrows nor widens that envelope.
``LakeWriteHalt`` semantics are untouched — this seam sits strictly INSIDE
the guarded render block.

## Honest envelope (Golden Rule #10)

* **Expected payoff ≈ 0 — the conversion is for uniformity, not speed**
  (ADR-0020 §0): the crate links the SAME polars encoder the wheel already
  runs, and the engaged path additionally pays one uncompressed Arrow-IPC
  serialisation + one FFI crossing per render. MEASURED (2026-07-11
  sandbox, median of 15, 100k-row corpus shape — a spike-transcript
  figure, no committed bench reproduces it; directional, and it DISCLOSES
  a slowdown): IPC handoff ~6.6 ms +
  crate render ~12.5 ms ≈ 19 ms vs ~13 ms for the Python ``write_parquet``
  tail alone — the engaged encode tail is ~1.5× the Python one (~3% of the
  FULL ``_render_bytes`` wall, which the Python-side frame build
  dominates). The engaged path is strictly a small SLOWDOWN — the runbook's
  cost table carries the numbers, including the engaged path's transient
  PEAK-MEMORY amplification (frame + uncompressed-IPC copy + the crate's
  re-decoded frame + parquet buffer(s) — several× the frame's in-RAM
  footprint; during burn-in BOTH engines' parquet buffers are live for the
  compare) and the crate's SEPARATE rayon thread pool beside the wheel's.
* The latches + warn-once memo are the same *documented exception* to the
  no-global-mutable-state rule as ``rust_metrics``'s: plain unlocked writes
  — a racing writer thread can at worst run a few extra burn-in compares or
  emit a duplicate warn, never a wrong byte (the compare ships oracle bytes
  on ANY mismatch; thread-fuzz-pinned in the unit suite).
* The burn-in doubles the render cost for its first 16 engaged renders per
  process (bounded, amortised-nil) — and since the caller holds the
  partition's cross-process flock around the render, those first renders
  hold that flock ~2-3× longer (a concurrent process waiting on the SAME
  partition waits with them; bounded to the burn-in window).
* :func:`reset_lake_rust_render_for_tests` restores a fresh process view —
  test isolation only, never called from production paths.
"""

from __future__ import annotations

import importlib
import os
from typing import TYPE_CHECKING, Final

import structlog

if TYPE_CHECKING:  # pragma: no cover - import only for static typing
    from collections.abc import Callable
    from types import ModuleType

_LOG = structlog.get_logger("brutex.data.lake_rust_render")

#: The env flag. DEFAULT OFF — the Python render stays the production path
#: until the lockstep gates (version guard + parity corpus + burn-in) have
#: an operator-directed default flip behind a future DEC.
LAKE_RUST_RENDER_ENV: Final[str] = "BRUTEX_LAKE_RUST_RENDER"

#: Engaged renders byte-compared against the Python oracle per process (the
#: burn-in self-check). 16 mirrors the sweep-metrics kernel's
#: ``SELF_CHECK_SAMPLES`` (the #1531/#1532 precedent).
BURN_IN_SAMPLES: Final[int] = 16

#: Truthy env literals (case-insensitive, whitespace-stripped). A DELIBERATE
#: byte-for-byte mirror of the sweep flags' vocabulary
#: (``brutex.engine.sweep_lookahead._TRUTHY``) — a LOCAL twin, not an import,
#: because the engine package hard-imports the ``fno_math`` wheel (module
#: docstring); the mirror is equality-pinned in the unit suite.
_TRUTHY: Final[frozenset[str]] = frozenset({"1", "true", "yes", "on"})

#: Falsy env literals — the explicit kill-switch vocabulary, the mirror of
#: :data:`_TRUTHY` (same local-twin rationale).
_FALSY: Final[frozenset[str]] = frozenset({"0", "false", "no", "off"})

#: The latch: the disable reason once the Rust path is off for this process,
#: else ``None`` (the documented no-global-mutable-state exception — module
#: docstring; single-cell mutable, the ``rust_metrics`` shape).
_DISABLED_REASON: Final[list[str | None]] = [None]

#: Burn-in samples still to run this process (same documented exception).
_BURN_IN_REMAINING: Final[list[int]] = [BURN_IN_SAMPLES]

#: Schema-shape tokens already byte-compared this process (the shape gate —
#: module docstring layer 3). Bounded by construction: the ONE production
#: caller derives the token from the writer's frame schema, which has
#: exactly two shapes (7-column spot / 17-column Greeks overlay).
_BURN_IN_SHAPES_SEEN: Final[set[str]] = set()

#: Whether the shape gate is armed. Always ``True`` in production; the test
#: reset seam disarms it together with a zeroed count budget so
#: ``remaining=0`` keeps meaning "steady state — crate bytes, no oracle".
_BURN_IN_SHAPE_GATE: Final[list[bool]] = [True]

#: The crate API surface the import guard verifies (layer 1) — a module that
#: imports but lacks any of these is a foreign/broken install and latches
#: ``crate_unavailable`` instead of raising ``AttributeError`` mid-render.
_CRATE_API: Final[tuple[str, ...]] = (
    "pinned_polars_version",
    "render_parquet_from_ipc",
    "linked_zstd_version",
)

#: Once-per-process-per-reason warn memo + the engaged-once latch. LOG-VOLUME
#: state ONLY: rendered bytes never depend on it.
_OBSERVABILITY_MEMO: Final[set[str]] = set()

#: Memo key for the engaged-once INFO (namespaced apart from warn reasons).
_ENGAGED_KEY: Final[str] = "__engaged__"


def env_flag_tristate(raw: str) -> bool | None:
    """Classify *raw* against the flag vocabulary (local twin — module docs).

    ``True`` for a :data:`_TRUTHY` literal, ``False`` for a :data:`_FALSY`
    literal, ``None`` for anything else (unset/empty AND unrecognised — the
    caller owns the default + warn policy). Byte-identical semantics to
    :func:`brutex.engine.sweep_lookahead.env_flag_tristate`, equality-pinned
    in the unit suite; NOT imported from there because the engine package
    requires the ``fno_math`` wheel (module docstring).
    """
    value = raw.strip().lower()
    if value in _TRUTHY:
        return True
    if value in _FALSY:
        return False
    return None


def lake_rust_render_enabled() -> bool:
    """Resolve ``BRUTEX_LAKE_RUST_RENDER`` (default OFF, junk fails loud-OFF).

    * a :data:`_TRUTHY` value → ``True`` (the opt-in);
    * a :data:`_FALSY` value → ``False`` (the explicit, silent OFF);
    * unset / whitespace-empty → ``False`` (the default);
    * any OTHER non-empty value → ``False`` with one loud
      ``lake_rust_render.fallback`` WARN (reason ``flag_unrecognised``)
      per process — a typo'd opt-in (``onn``, ``ture``) must be VISIBLE,
      never silently ignored (the ``rust_metrics`` flag posture, inverted
      default).

    Read per call (one ``os.environ`` probe against a multi-ms render) so
    parallel workers resolve their own inherited environment — no global
    flag state.
    """
    raw = os.environ.get(LAKE_RUST_RENDER_ENV, "")
    verdict = env_flag_tristate(raw)
    if verdict is not None:
        return verdict
    if raw.strip():
        _warn_once("flag_unrecognised", value=raw)
    return False


def rust_render_disabled() -> bool:
    """Return ``True`` iff a guard/autorevert latched the Rust path OFF this process."""
    return _DISABLED_REASON[0] is not None


def reset_lake_rust_render_for_tests(*, remaining: int = BURN_IN_SAMPLES) -> None:
    """Reset the latch + burn-in budget + shape gate + warn memo (test isolation ONLY).

    ``remaining=0`` puts the process in the post-burn-in steady state —
    count budget spent AND the shape gate disarmed, so the returned bytes
    are the crate's with no oracle in the path (the parity corpus depends
    on that: an armed shape gate would cushion a first-shape divergence by
    shipping oracle bytes). The default restores a fresh-process view
    (budget full, shape gate armed, no shapes seen). Never called from
    production code paths (the ``rust_metrics`` reset-seam convention).
    """
    _DISABLED_REASON[0] = None
    _BURN_IN_REMAINING[0] = remaining
    _BURN_IN_SHAPES_SEEN.clear()
    _BURN_IN_SHAPE_GATE[0] = remaining > 0
    _OBSERVABILITY_MEMO.clear()


def _warn_once(reason: str, **context: object) -> None:
    """Log ``lake_rust_render.fallback`` WARN once per process per *reason*.

    The seam runs once per partition render (thousands per pull), so a
    sticky reason would flood the logs; the first occurrence is loud,
    repeats fall through silently (the Python bytes serve regardless). The
    event name is a LITERAL with the reason as a field — the
    ``rust_metrics.fallback`` grep-able convention.
    """
    if reason in _OBSERVABILITY_MEMO:
        return
    _OBSERVABILITY_MEMO.add(reason)
    _LOG.warning("lake_rust_render.fallback", reason=reason, **context)


def _disable(reason: str, *, error: bool = False, **context: object) -> None:
    """Latch the Rust path OFF for the remainder of the process.

    ``error=True`` logs ``lake_rust_render.render_autoreverted`` at ERROR
    (the burn-in autorevert — a genuine wrongness signal, never throttled
    below the operator's view); guard latches log
    ``lake_rust_render.rust_path_disabled`` at WARN. Repeat calls are
    no-ops (the latch is already set).
    """
    if _DISABLED_REASON[0] is not None:
        return
    _DISABLED_REASON[0] = reason
    if error:
        _LOG.error("lake_rust_render.render_autoreverted", reason=reason, **context)
    else:
        _LOG.warning("lake_rust_render.rust_path_disabled", reason=reason, **context)


def _resolve_crate() -> ModuleType | None:
    """Import guard (layer 1): return ``brutex_lake``, or latch OFF + ``None``.

    An absent wheel (``ImportError``) latches ``crate_unavailable`` — the
    facade is default-OFF and opt-in, so an operator who set the flag
    without installing the crate gets ONE loud WARN naming the remedy, and
    every render serves the Python bytes. A module that IMPORTS but lacks
    any :data:`_CRATE_API` callable (a name collision / hand-broken
    install) latches the SAME reason — the fail-closed contract is "never a
    new crash class", so an ``AttributeError`` must never leave this seam.
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
                error_detail=f"brutex_lake.{name} missing or not callable — foreign/broken install",
                remedy="pip install rust/brutex-lake (see docs/runbooks/lake-rust-render-lockstep.md)",
            )
            return None
    return crate


#: Sentinel for "the metadata call raised" — distinct from any value a
#: foreign crate could RETURN (even ``None``, which flows on into the
#: version guard's ``!=`` and fails closed there, loudly).
_METADATA_ERROR: Final[object] = object()


def _crate_metadata(crate: ModuleType, attr: str) -> object:
    """Call a crate metadata pyfunction defensively; latch OFF on ANY raise.

    A metadata call (``pinned_polars_version`` / ``linked_zstd_version``)
    that raises marks a foreign/stale install whose bytes must not be
    trusted — latch ``crate_metadata_error`` (→ :data:`_METADATA_ERROR`)
    and let the caller serve the oracle, identically to
    ``crate_unavailable`` (fail-closed, never a new crash class).
    ``MemoryError`` re-raises bare: OOM stays a global stop riding
    ``lake_write_guard`` → ``LakeWriteHalt``. Returns the raw value
    un-typechecked: a non-str ``pinned_polars_version`` simply fails the
    ``!=`` version guard (fail-closed already), and the zstd fingerprint is
    log-only.
    """
    try:
        return getattr(crate, attr)()
    except MemoryError:
        raise
    except Exception as exc:
        _disable("crate_metadata_error", metadata_call=attr, error_detail=repr(exc))
        return _METADATA_ERROR


def _degrade_errors() -> tuple[type[Exception], ...]:
    """The narrow degrade-to-oracle catch set for the engaged render path.

    ``ValueError``/``RuntimeError`` are the crate's declared FFI error
    surface (a crate-side panic is caught at the FFI boundary and mapped to
    ``RuntimeError`` — never a ``pyo3_runtime.PanicException``, which is a
    ``BaseException`` that would escape any ``Exception`` catch; the render
    call carries its own narrow panic belt — :func:`_panic_belt_errors` —
    for exactly that shape);
    ``TypeError``/``OverflowError`` are PyO3 argument-conversion failures;
    ``AttributeError`` is the broken-crate-internals defensive belt (the
    import guard already refuses a module MISSING the API);
    ``PolarsError`` covers the IPC serialisation leg
    (``DataFrame.write_ipc``). Resolved lazily so this leaf keeps zero
    module-import-time engine deps; in practice polars is always already
    imported when the seam runs (``_render_bytes`` just used it) — the
    ``ImportError`` arm is a defensive belt. ``MemoryError`` is DELIBERATELY
    absent: OOM propagates into ``lake_write_guard`` → ``LakeWriteHalt``,
    identical to the pure-Python path.
    """
    base: tuple[type[Exception], ...] = (
        ValueError,
        RuntimeError,
        TypeError,
        OverflowError,
        AttributeError,
    )
    try:
        import polars.exceptions as _pl_exc
    except ImportError:
        return base
    return (*base, _pl_exc.PolarsError)


def _panic_belt_errors() -> tuple[type[BaseException], ...]:
    """The narrow FFI panic-belt catch set — ``(Exception, *panic classes)``.

    ``pyo3_runtime.PanicException`` is the one ``BaseException`` subclass a
    pyo3 boundary can leak (a Rust panic escaping ``catch_unwind``); it must
    latch ``crate_panic`` and degrade to the oracle, but the repo convention
    (CodeQL ``py/catch-base-exception``) forbids a non-re-raising literal
    ``except BaseException``, so the class is resolved GUARDEDLY by name
    from its two plausible homes and folded beside ``Exception``:

    * ``pyo3_runtime`` — the class's ``__module__``; pyo3 does NOT register
      a real importable module today (probed 2026-07-11: ``ImportError``
      even with polars loaded), so this arm is a forward belt for a pyo3
      that starts to;
    * ``polars.exceptions`` — polars re-exports the class
      (``PanicException``), the in-repo precedent
      (``brutex/data/errors.py``'s escape sets); the arm that resolves
      today.

    HONEST narrowing (module docstring): a DISTINCT per-extension
    ``PanicException`` type object (each statically-linked pyo3 runtime
    carries its own statics) is not catchable by name and now PROPAGATES —
    a deliberate, disclosed trade bounded by the crate's own
    ``catch_unwind`` boundary (panics map to ``RuntimeError``, the degrade
    set) plus the import/API/metadata/version guards preceding every
    render. Resolved lazily per raise — an ``except`` clause expression
    only evaluates while an exception is in flight (the
    ``_degrade_errors`` convention). ``Exception`` membership keeps every
    previously-latched out-of-degrade-set ``Exception`` latching exactly
    as before; ``KeyboardInterrupt``/``SystemExit``/``MemoryError``
    re-raise in the clause ABOVE the belt, so ``Exception`` here never
    swallows an OOM.
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


def render_parquet_bytes(
    *,
    ipc_bytes_factory: Callable[[], bytes],
    python_render: Callable[[], bytes],
    polars_version: str,
    schema_token: str | None = None,
) -> bytes:
    """The routed render — called by ``_render_bytes`` ONLY when the flag is ON.

    Runs the three fail-closed layers (module docstring) and returns the
    parquet bytes to persist. EVERY path returns valid bytes: a latched /
    guarded / degraded call returns ``python_render()`` (the live oracle),
    a burn-in mismatch returns the ORACLE bytes, and only a guard-clean,
    burn-in-clean call returns the Rust bytes. The caller holds
    ``lake_write_guard`` around this call, so a ``MemoryError`` anywhere in
    here maps to ``LakeWriteHalt`` exactly as on the pure-Python path.

    Parameters
    ----------
    ipc_bytes_factory
        Zero-arg callable serialising the ALREADY-BUILT frame to
        uncompressed Arrow IPC bytes. A callable (not bytes) so a latched
        process never pays the serialisation.
    python_render
        Zero-arg callable running the verbatim Python
        ``write_parquet`` render — the live parity oracle.
    polars_version
        The installed ``polars.__version__`` (the caller already imported
        polars; passed in so this leaf never imports it at module scope).
    schema_token
        The frame's column/dtype fingerprint (the production caller passes
        ``str(df.schema)``) — keys the burn-in SHAPE gate so every distinct
        schema shape is byte-compared at least once before crate bytes
        ship for it. ``None`` honestly degrades this call to the count
        budget alone (module docstring layer 3).
    """
    if rust_render_disabled():
        return python_render()
    crate = _resolve_crate()
    if crate is None:
        return python_render()
    pinned = _crate_metadata(crate, "pinned_polars_version")
    if pinned is _METADATA_ERROR:
        return python_render()
    if pinned != polars_version:
        # Layer 2 — the version guard (ADR-0020 §7 hazard 1): fail-closed,
        # ONE loud WARN naming BOTH versions, never silently different bytes.
        _disable(
            "polars_version_mismatch",
            installed_polars=polars_version,
            crate_pinned_polars=pinned,
            remedy="align the wheel and the crate's pinned polars rev, then "
            "re-run the parity corpus "
            "(docs/runbooks/lake-rust-render-lockstep.md)",
        )
        return python_render()
    try:
        rust_bytes = crate.render_parquet_from_ipc(ipc_bytes_factory())
    except _degrade_errors() as exc:
        _warn_once("render_error", error_detail=repr(exc))
        return python_render()
    except (KeyboardInterrupt, SystemExit, MemoryError):
        # The escape set (the errors.py doctrine): operator interrupt /
        # interpreter shutdown / OOM stay global — OOM in particular must
        # reach lake_write_guard → LakeWriteHalt, never a fallback. This
        # clause sits ABOVE the panic belt so the belt's `Exception` member
        # can never swallow a MemoryError.
        raise
    except _panic_belt_errors() as exc:
        # The panic belt over the crate's own catch_unwind: a `pyo3_runtime.
        # PanicException` is a BaseException subclass that no `except
        # Exception` sees — if one nonetheless escapes a pyo3 boundary, the
        # contract is broken; latch OFF and serve the oracle (fail-closed,
        # never a new crash class into the pull spine). The belt is the
        # NARROW `(Exception, <resolved PanicException class(es)>)` — never
        # a literal `except BaseException` (CodeQL py/catch-base-exception;
        # the repo's re-raise-only convention for BaseException handlers).
        # A foreign BaseException the resolver cannot name PROPAGATES — the
        # disclosed narrowing (_panic_belt_errors docstring); `Exception`
        # keeps every out-of-degrade-set Exception latching as before.
        _disable("crate_panic", error_detail=repr(exc))
        return python_render()
    if not isinstance(rust_bytes, bytes):
        # Refuse-poison gate (the `rust_metrics._valid_drawdown` stance): a
        # non-bytes return is a crate BUG — never persisted, served loudly
        # from the oracle. Structurally unreachable through the real crate
        # (its FFI constructs PyBytes); guards a stale/foreign install.
        _warn_once("render_result_refused", result_type=type(rust_bytes).__name__)
        return python_render()
    if _ENGAGED_KEY not in _OBSERVABILITY_MEMO:
        linked_zstd = _crate_metadata(crate, "linked_zstd_version")
        if linked_zstd is _METADATA_ERROR:
            # A raising metadata pyfunction marks a foreign/stale install —
            # its bytes are not trusted either (fail-closed; latched above).
            return python_render()
        _OBSERVABILITY_MEMO.add(_ENGAGED_KEY)
        _LOG.info(
            "lake_rust_render.engaged",
            crate_pinned_polars=pinned,
            linked_zstd=linked_zstd,
        )
    shape_unseen = (
        _BURN_IN_SHAPE_GATE[0]
        and schema_token is not None
        and schema_token not in _BURN_IN_SHAPES_SEEN
    )
    if _BURN_IN_REMAINING[0] > 0 or shape_unseen:
        # Layer 3 — burn-in self-check (the #1531/#1532 pattern, plus the
        # shape gate — module docstring): both engines render; ANY byte
        # difference ships the ORACLE bytes and autoreverts the Rust path
        # for the process.
        oracle_bytes = python_render()
        if rust_bytes != oracle_bytes:
            _disable(
                "burn_in_byte_mismatch",
                error=True,
                rust_len=len(rust_bytes),
                python_len=len(oracle_bytes),
                schema_token=schema_token,
            )
            return oracle_bytes
        if schema_token is not None:
            _BURN_IN_SHAPES_SEEN.add(schema_token)
        if _BURN_IN_REMAINING[0] > 0:
            _BURN_IN_REMAINING[0] -= 1
            if _BURN_IN_REMAINING[0] == 0:
                _LOG.info("lake_rust_render.burn_in_complete", samples=BURN_IN_SAMPLES)
        return rust_bytes
    return rust_bytes


__all__ = [
    "BURN_IN_SAMPLES",
    "LAKE_RUST_RENDER_ENV",
    "env_flag_tristate",
    "lake_rust_render_enabled",
    "render_parquet_bytes",
    "reset_lake_rust_render_for_tests",
    "rust_render_disabled",
]
