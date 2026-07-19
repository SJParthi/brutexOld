"""Flag-gated Rust parquet-render seam — the datalake-roadmap render facade.

DEC-RUST-DATALAKE-IO-ROADMAP-001 (MIGRATION.md #1891; the conversion roadmap
ADR at ``docs/adr/0020-datalake-rust-conversion-roadmap.md``). When — and ONLY
when — ``BRUTEX_LAKE_RUST_RENDER`` is set truthy, the two lake writers route
their frame → parquet-bytes renders through the ``brutex_lake`` crate (a
Rust build of polars REV-pinned at the commit the installed Python wheel's
release tag pointed at — tags are movable, so the manifest pins the commit;
the frame crosses as uncompressed Arrow IPC bytes). This module owns
the flag, the three fail-closed layers, and the process-local latches; the
crate side lives in ``rust/brutex-lake/src/lib.rs``.

## Render PROFILES (PR-2) — one facade, one crate callable per byte surface

Two callers, two distinct wheel call shapes, ONE guard/latch/burn-in
apparatus (CLAUDE.md §11 COMMON — no sibling facade):

* :data:`PROFILE_BARS` (the default — PR-1 back-compat):
  :meth:`ParquetWriter._render_bytes`'s pinned options (zstd level 3,
  ``row_group_size=100_000``, statistics OFF) →
  ``brutex_lake.render_parquet_from_ipc``.
* :data:`PROFILE_OPTIONS_DEFAULTS` (PR-2):
  ``option_parquet_writer._atomic_write_parquet``'s ALL-DEFAULTS
  ``df.write_parquet(path)`` shape (Zstd(None), statistics
  min/max/null_count ON + distinct OFF, ``row_group_size=None`` → the
  streaming sink's 122,880-row default split) →
  ``brutex_lake.render_parquet_defaults_from_ipc``.

The profile changes ONLY (a) which crate callable renders, (b) the burn-in
count-budget cell (per-profile — a bars-warmed process has ZERO byte
witnesses on the statistics/Zstd(None)/122,880-split surfaces), and (c) the
shape-token namespace (profile-prefixed inside the facade, so a bars witness
can never license a defaults shape). The flag, the latch, the
import/version/metadata guards, the degrade/panic-belt sets and the OOM
doctrine are ONE shared code path. An UNKNOWN profile value fails CLOSED:
one loud ``lake_rust_render.fallback`` WARN (reason ``profile_unknown``) and
the oracle bytes — never a ``KeyError`` into a write path, and never a latch
(the install is not implicated).

SHARED-LATCH DISCLOSURE (deliberate, fail-closed both directions): the
process latch is ONE cell for BOTH profiles — any guard trip or burn-in
autorevert on either writer's path disables the OTHER writer's Rust path
too. A byte mismatch anywhere is evidence the INSTALL's lockstep is broken
(same crate, same rev); the oracle always serves, payoff ≈ 0, so
over-disabling costs nothing. Likewise a stale PR-1-vintage crate (missing
the defaults callable) latches ``crate_unavailable`` for BOTH surfaces —
the lockstep is an ATOMIC pin (crate == repo move together; rebuild per
``docs/runbooks/lake-rust-render-lockstep.md``).

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
   the process. The resolved module's API surface is VERIFIED (all FOUR
   callables present, all-or-nothing across both profiles — a
   name-collision / hand-broken / stale-PR-1 ``brutex_lake`` that
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
   engaged renders per process **per PROFILE** ALSO run the Python render
   and byte-compare. A mismatch logs ``lake_rust_render.render_autoreverted``
   (ERROR — a genuine wrongness signal), SHIPS THE PYTHON BYTES (correctness
   first; ``raw_hash`` and the on-disk partition see only oracle bytes), and
   latches OFF for the process (BOTH profiles — the shared latch). After 16
   clean compares per profile (``lake_rust_render.burn_in_complete``,
   ``profile=`` field), Rust bytes ship directly. The
   pattern is the sweep-metrics kernel's burn-in verbatim
   (DEC-SWEEP-EXIT-INDEX-FASTPATH-001 / DEC-SWEEP-RUST-METRICS-DEFAULT-ON-001
   — see ``brutex/engine/rust_metrics.py``), EXTENDED with a SCHEMA-SHAPE
   gate: the count budget alone would let a process that burnt in on 16
   spot partitions ship crate bytes for its FIRST-EVER Greeks-overlay
   partition with zero byte witness, so the caller passes a
   ``schema_token`` (the frame's column/dtype fingerprint), the facade
   prefixes it with the PROFILE (a bars witness never licenses a defaults
   shape — structural, by construction), and every DISTINCT prefixed shape
   is byte-compared at least once before crate bytes ship for it — even
   after the count budget is spent. HONEST BOUND (PR-2 correction of the
   PR-1 "bounded by construction (two writer shapes)" claim, which the
   option writer's heal path FALSIFIES — a heal re-renders whatever stored
   schema a hand-planted file carries, so the token space is open-ended):
   the seen-set is CAPPED at :data:`_BURN_IN_SHAPES_CAP` entries; once
   saturated, an unseen shape is STILL byte-compared but no longer
   recorded — the degrade direction is compare-ALWAYS for novel shapes
   (more oracle compares on a pathological lake, never fewer). A ``None``
   token honestly degrades that call to the count budget alone.

## Log events (all literal — greppable)

* ``lake_rust_render.fallback`` (WARN, once per process per ``reason`` ∈
  {``flag_unrecognised``, ``profile_unknown``, ``render_error``,
  ``render_result_refused``}) — the Python bytes served.
* ``lake_rust_render.rust_path_disabled`` (WARN, once — ``reason`` ∈
  {``crate_unavailable``, ``crate_metadata_error``,
  ``polars_version_mismatch``, ``crate_panic``}) — the guard latch.
* ``lake_rust_render.render_autoreverted`` (ERROR, once) — the burn-in
  byte-compare mismatch; oracle bytes shipped, latch set.
* ``lake_rust_render.engaged`` (INFO, once per PROFILE) — the first
  Rust-served render on that profile (the observable "the wired ON path
  actually runs Rust" signal; carries ``profile=``).
* ``lake_rust_render.burn_in_complete`` (INFO, once per PROFILE) — 16 clean
  compares on that profile.

Render-path events fired from :func:`render_parquet_bytes` carry a
``profile=`` field; NONE carries the destination path (the option writer's
series dir name is vendor-influenceable — the DEC-LAKE-HEAL-OPTIONS-FOREIGN-001
log-injection discipline — so the facade's call shape simply never receives
``dest``; events name lengths/tokens/versions only).

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
  — a racing writer thread can at worst run a few extra burn-in compares,
  transiently overshoot the shape-cap by ≤ the racing-thread count
  (memory-only), or emit a duplicate warn, never a wrong byte (the compare
  ships oracle bytes on ANY mismatch; thread-fuzz-pinned in the unit suite
  across all three concurrency surfaces — the single-profile latch, two
  profiles sharing the latch under interleaved per-profile budget
  decrements, and concurrent NOVEL shapes past the seen-set cap).
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
    from collections.abc import Callable, Mapping
    from types import ModuleType

    import polars as pl

_LOG = structlog.get_logger("brutex.data.lake_rust_render")

#: The env flag. DEFAULT OFF — the Python render stays the production path
#: until the lockstep gates (version guard + parity corpus + burn-in) have
#: an operator-directed default flip behind a future DEC.
LAKE_RUST_RENDER_ENV: Final[str] = "BRUTEX_LAKE_RUST_RENDER"

#: Engaged renders byte-compared against the Python oracle per process PER
#: PROFILE (the burn-in self-check). 16 mirrors the sweep-metrics kernel's
#: ``SELF_CHECK_SAMPLES`` (the #1531/#1532 precedent); per-profile because
#: each profile is a distinct byte SURFACE (module docstring layer 3).
BURN_IN_SAMPLES: Final[int] = 16

#: Render profile: the PR-1 bars writer (zstd-3 / rg=100k / stats OFF) —
#: the DEFAULT, so ``parquet_writer.py``'s PR-1 call site is byte-untouched.
PROFILE_BARS: Final[str] = "bars"

#: Render profile: the PR-2 option writer's ALL-DEFAULTS
#: ``df.write_parquet(path)`` shape (Zstd(None) / rg=None→122,880 split /
#: statistics min+max+null_count).
PROFILE_OPTIONS_DEFAULTS: Final[str] = "options_defaults"

#: profile → the ``brutex_lake`` pyfunction that renders it. TWO thin crate
#: pyfunctions over ONE shared Rust core — no stringly profile enum crosses
#: the FFI (a missing FUNCTION fails once, loudly, at the import guard).
_PROFILE_RENDER_ATTR: Final[Mapping[str, str]] = {
    PROFILE_BARS: "render_parquet_from_ipc",
    PROFILE_OPTIONS_DEFAULTS: "render_parquet_defaults_from_ipc",
}

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

#: The budget every lazily-initialised per-profile burn-in cell starts from
#: (``BURN_IN_SAMPLES`` in production; the test reset seam re-points it —
#: same documented exception).
_BURN_IN_DEFAULT: Final[list[int]] = [BURN_IN_SAMPLES]

#: Burn-in samples still to run this process, PER PROFILE (lazily seeded
#: from :data:`_BURN_IN_DEFAULT` on a profile's first engaged render — same
#: documented exception; dict ``setdefault`` is atomic under the GIL).
_BURN_IN_REMAINING: Final[dict[str, int]] = {}

#: PROFILE-PREFIXED schema-shape tokens already byte-compared this process
#: (the shape gate — module docstring layer 3). Keys are minted inside the
#: facade as ``f"{profile}|{schema_token}"`` so a bars witness can never
#: license a defaults-profile shape. HONESTLY UNBOUNDED input space (the
#: option HEAL path re-renders whatever stored schema a hand-planted file
#: carries — the PR-2 correction of PR-1's "two writer shapes" claim), so
#: the set is capped at :data:`_BURN_IN_SHAPES_CAP`: past the cap an unseen
#: shape is byte-compared but NOT recorded (compare-always for novel shapes
#: — more oracle compares, never fewer).
_BURN_IN_SHAPES_SEEN: Final[set[str]] = set()

#: The seen-set cap (fail-safe bound on facade memory under a pathological
#: hand-planted lake; production traffic is a handful of shapes).
_BURN_IN_SHAPES_CAP: Final[int] = 64

#: Whether the shape gate is armed. Always ``True`` in production; the test
#: reset seam disarms it together with a zeroed count budget so
#: ``remaining=0`` keeps meaning "steady state — crate bytes, no oracle".
_BURN_IN_SHAPE_GATE: Final[list[bool]] = [True]

#: The crate API surface the import guard verifies (layer 1) — a module that
#: imports but lacks any of these is a foreign/broken install and latches
#: ``crate_unavailable`` instead of raising ``AttributeError`` mid-render.
#: ALL-OR-NOTHING deliberately (PR-2): a stale PR-1-vintage crate missing
#: ``render_parquet_defaults_from_ipc`` latches BOTH profiles — the lockstep
#: is an ATOMIC pin, never a partial-install gray state (rebuild ritual:
#: docs/runbooks/lake-rust-render-lockstep.md).
_CRATE_API: Final[tuple[str, ...]] = (
    "pinned_polars_version",
    "render_parquet_from_ipc",
    "render_parquet_defaults_from_ipc",
    "linked_zstd_version",
)

#: Once-per-process-per-reason warn memo + the engaged-once latch. LOG-VOLUME
#: state ONLY: rendered bytes never depend on it.
_OBSERVABILITY_MEMO: Final[set[str]] = set()

#: Memo key for the engaged-once INFO (namespaced apart from warn reasons).
#: The BARS profile records this literal key (PR-1 back-compat — the frozen
#: bars parity corpus's vacuous-green guard reads it); every OTHER profile
#: records :func:`_engaged_memo_key`'s ``"__engaged__|<profile>"`` so each
#: profile's first Rust-served render is independently observable.
_ENGAGED_KEY: Final[str] = "__engaged__"


def _engaged_memo_key(profile: str) -> str:
    """The engaged-once memo key for *profile* (bars keeps the PR-1 literal)."""
    return _ENGAGED_KEY if profile == PROFILE_BARS else f"{_ENGAGED_KEY}|{profile}"


def ipc_handoff_bytes(df: pl.DataFrame) -> bytes:
    """Serialise *df* to UNCOMPRESSED Arrow IPC bytes — the Rust-render handoff.

    DEC-RUST-DATALAKE-IO-ROADMAP-001 (hoisted here from
    ``parquet_writer._ipc_bytes`` in PR-2 — §11 COMMON: the option writer is
    the second caller, and a private import from a sibling production module
    is the house anti-pattern; ``parquet_writer._ipc_bytes`` is now a thin
    delegating alias). When a ``BRUTEX_LAKE_RUST_RENDER`` seam is engaged,
    the ALREADY-BUILT render frame crosses the FFI as Arrow IPC (the
    spike-proven handoff — the crate ``rechunk``s after the read so the
    memory layout matches the single-chunk frame both writers hand over).
    Uncompressed deliberately: the handoff must never introduce its own
    compression variable into the byte-parity surface. MEMORY HONESTY: this
    materialises a FULL uncompressed-IPC copy of the frame (≈ the frame's
    in-RAM size, LARGER than the compressed parquet) that lives alongside
    the source ``df``, the crate's re-decoded frame and the parquet
    buffer(s) — the engaged render's transient peak is several× the frame
    footprint (quantified in the runbook cost table,
    docs/runbooks/lake-rust-render-lockstep.md §0). Reached ONLY from the
    flag-ON paths inside the writers' ``lake_write_guard`` blocks, so an
    allocation failure here maps to ``LakeWriteHalt`` like every other
    render allocation. Imports ``io`` locally — this leaf stays polars-free
    at module scope (the frame arrives already built; only the caller
    imports polars).
    """
    import io

    bio = io.BytesIO()
    df.write_ipc(bio, compression="uncompressed")
    return bio.getvalue()


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
    """Reset the latch + burn-in budgets + shape gate + warn memo (test isolation ONLY).

    ``remaining=0`` puts the process in the post-burn-in steady state for
    EVERY profile — all count budgets spent AND the shape gate disarmed, so
    the returned bytes are the crate's with no oracle in the path (the
    parity corpora depend on that: an armed shape gate would cushion a
    first-shape divergence by shipping oracle bytes). The default restores
    a fresh-process view (every profile's lazily-seeded budget full, shape
    gate armed, no shapes seen). Never called from production code paths
    (the ``rust_metrics`` reset-seam convention).
    """
    _DISABLED_REASON[0] = None
    _BURN_IN_DEFAULT[0] = remaining
    _BURN_IN_REMAINING.clear()
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


def _resolve_crate(*, profile: str) -> ModuleType | None:
    """Import guard (layer 1): return ``brutex_lake``, or latch OFF + ``None``.

    An absent wheel (``ImportError``) latches ``crate_unavailable`` — the
    facade is default-OFF and opt-in, so an operator who set the flag
    without installing the crate gets ONE loud WARN naming the remedy, and
    every render serves the Python bytes. A module that IMPORTS but lacks
    any :data:`_CRATE_API` callable (a name collision / hand-broken /
    STALE-PR-1 install) latches the SAME reason — the fail-closed contract
    is "never a new crash class", so an ``AttributeError`` must never leave
    this seam; the surface check is all-or-nothing across BOTH profiles
    (the atomic-pin doctrine). *profile* is log context only — the latch is
    shared.
    """
    try:
        crate = importlib.import_module("brutex_lake")
    except ImportError as exc:
        _disable(
            "crate_unavailable",
            profile=profile,
            error_detail=repr(exc),
            remedy="pip install rust/brutex-lake (see docs/runbooks/lake-rust-render-lockstep.md)",
        )
        return None
    for name in _CRATE_API:
        if not callable(getattr(crate, name, None)):
            _disable(
                "crate_unavailable",
                profile=profile,
                error_detail=f"brutex_lake.{name} missing or not callable — foreign/broken install",
                remedy="pip install rust/brutex-lake (see docs/runbooks/lake-rust-render-lockstep.md)",
            )
            return None
    return crate


#: Sentinel for "the metadata call raised" — distinct from any value a
#: foreign crate could RETURN (even ``None``, which flows on into the
#: version guard's ``!=`` and fails closed there, loudly).
_METADATA_ERROR: Final[object] = object()


def _crate_metadata(crate: ModuleType, attr: str, *, profile: str) -> object:
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
    profile: str = PROFILE_BARS,
) -> bytes:
    """The routed render — called by the writer seams ONLY when the flag is ON.

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
        ``write_parquet`` render for *profile* — the live parity oracle.
    polars_version
        The installed ``polars.__version__`` (the caller already imported
        polars; passed in so this leaf never imports it at module scope).
    schema_token
        The frame's column/dtype fingerprint (the production callers pass
        ``str(df.schema)``) — keys the burn-in SHAPE gate so every distinct
        (profile, schema) shape is byte-compared at least once before crate
        bytes ship for it (the facade prefixes the profile). ``None``
        honestly degrades this call to the count budget alone (module
        docstring layer 3).
    profile
        The render profile (:data:`PROFILE_BARS` — the PR-1 back-compat
        default — or :data:`PROFILE_OPTIONS_DEFAULTS`); selects the crate
        callable + the burn-in budget cell + the shape-token namespace and
        NOTHING else. An unknown value fails CLOSED to the oracle with one
        loud ``profile_unknown`` WARN (never a ``KeyError`` into a write
        path; latches nothing — the install is not implicated).
    """
    render_attr = _PROFILE_RENDER_ATTR.get(profile)
    if render_attr is None:
        _warn_once("profile_unknown", profile=profile)
        return python_render()
    if rust_render_disabled():
        return python_render()
    crate = _resolve_crate(profile=profile)
    if crate is None:
        return python_render()
    pinned = _crate_metadata(crate, "pinned_polars_version", profile=profile)
    if pinned is _METADATA_ERROR:
        return python_render()
    if pinned != polars_version:
        # Layer 2 — the version guard (roadmap hazard 1): fail-closed,
        # ONE loud WARN naming BOTH versions, never silently different bytes.
        _disable(
            "polars_version_mismatch",
            profile=profile,
            installed_polars=polars_version,
            crate_pinned_polars=pinned,
            remedy="align the wheel and the crate's pinned polars rev, then "
            "re-run the parity corpus "
            "(docs/runbooks/lake-rust-render-lockstep.md)",
        )
        return python_render()
    try:
        rust_bytes = getattr(crate, render_attr)(ipc_bytes_factory())
    except _degrade_errors() as exc:
        _warn_once("render_error", profile=profile, error_detail=repr(exc))
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
        _disable("crate_panic", profile=profile, error_detail=repr(exc))
        return python_render()
    if not isinstance(rust_bytes, bytes):
        # Refuse-poison gate (the `rust_metrics._valid_drawdown` stance): a
        # non-bytes return is a crate BUG — never persisted, served loudly
        # from the oracle. Structurally unreachable through the real crate
        # (its FFI constructs PyBytes); guards a stale/foreign install.
        _warn_once("render_result_refused", profile=profile, result_type=type(rust_bytes).__name__)
        return python_render()
    engaged_key = _engaged_memo_key(profile)
    if engaged_key not in _OBSERVABILITY_MEMO:
        linked_zstd = _crate_metadata(crate, "linked_zstd_version", profile=profile)
        if linked_zstd is _METADATA_ERROR:
            # A raising metadata pyfunction marks a foreign/stale install —
            # its bytes are not trusted either (fail-closed; latched above).
            return python_render()
        _OBSERVABILITY_MEMO.add(engaged_key)
        _LOG.info(
            "lake_rust_render.engaged",
            profile=profile,
            crate_pinned_polars=pinned,
            linked_zstd=linked_zstd,
        )
    shape_key = None if schema_token is None else f"{profile}|{schema_token}"
    shape_unseen = (
        _BURN_IN_SHAPE_GATE[0] and shape_key is not None and shape_key not in _BURN_IN_SHAPES_SEEN
    )
    remaining = _BURN_IN_REMAINING.setdefault(profile, _BURN_IN_DEFAULT[0])
    if remaining > 0 or shape_unseen:
        # Layer 3 — burn-in self-check (the #1531/#1532 pattern, plus the
        # shape gate — module docstring): both engines render; ANY byte
        # difference ships the ORACLE bytes and autoreverts the Rust path
        # for the process (BOTH profiles — the shared latch).
        oracle_bytes = python_render()
        if rust_bytes != oracle_bytes:
            _disable(
                "burn_in_byte_mismatch",
                error=True,
                profile=profile,
                rust_len=len(rust_bytes),
                python_len=len(oracle_bytes),
                schema_token=schema_token,
            )
            return oracle_bytes
        if shape_key is not None and len(_BURN_IN_SHAPES_SEEN) < _BURN_IN_SHAPES_CAP:
            # Past the cap the shape is deliberately NOT recorded — novel
            # shapes then compare on EVERY render (fail-safe direction:
            # more oracle compares, never fewer — module docstring).
            _BURN_IN_SHAPES_SEEN.add(shape_key)
        if remaining > 0:
            _BURN_IN_REMAINING[profile] = remaining - 1
            if remaining == 1:
                _LOG.info(
                    "lake_rust_render.burn_in_complete",
                    profile=profile,
                    samples=BURN_IN_SAMPLES,
                )
        return rust_bytes
    return rust_bytes


#: PUBLIC re-exports for the sibling lake facades (ADR-0020 PR-3+ — §11
#: COMMON): the degrade / panic-belt catch sets are PURE and stateless —
#: nothing about them is render-specific — so sibling seams
#: (``brutex/data/lake_rust_merge.py``) import THESE names instead of
#: minting copies. The per-facade LATCH/warn state stays per-facade by
#: design (a render autorevert must never silence the merge seam and vice
#: versa), so only the stateless helpers are shared.
degrade_errors = _degrade_errors
panic_belt_errors = _panic_belt_errors


__all__ = [
    "BURN_IN_SAMPLES",
    "LAKE_RUST_RENDER_ENV",
    "PROFILE_BARS",
    "PROFILE_OPTIONS_DEFAULTS",
    "degrade_errors",
    "env_flag_tristate",
    "ipc_handoff_bytes",
    "lake_rust_render_enabled",
    "panic_belt_errors",
    "render_parquet_bytes",
    "reset_lake_rust_render_for_tests",
    "rust_render_disabled",
]
