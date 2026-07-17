"""Flag-gated Rust resample seam — the ADR-0020 Wave-B slice G5 facade.

DEC-LAKE-RUST-RESAMPLE-001 (MIGRATION.md #1913 — allocator-final). When — and
ONLY when — ``BRUTEX_LAKE_RUST_RESAMPLE`` is set truthy,
:func:`brutex.data.tf_resampler.resample_1m_to_tf` routes its 1-minute →
derived-TF resample through the ``brutex_lake`` crate's
``resample_1m_ipc`` (the frame crosses as uncompressed Arrow IPC bytes —
the PR-1 deviation, NOT Arrow C-data zero-copy). This module owns the flag,
the fail-closed guard layers, and the process-local latches; the crate side
lives in ``rust/brutex-lake/src/resample.rs``. The section-for-section
mirror of ``brutex/data/lake_rust_render.py`` (the PR-1 facade) — same flag
vocabulary, same import/version guards, same burn-in + autorevert machinery,
compared at FRAME strength instead of byte strength.

## The flag — ``BRUTEX_LAKE_RUST_RESAMPLE`` (DEFAULT OFF)

Resolved per call like the sibling lake/sweep flags: a truthy literal
(``1/true/yes/on``) → ON; a falsy literal (``0/false/no/off``) → OFF; unset
or whitespace-empty → the default (OFF); any OTHER value → OFF with one loud
``lake_rust_resample.fallback`` WARN (reason ``flag_unrecognised``) per
process — a typo'd opt-in is never silently honoured, and (since the default
is OFF) never silently armed either. The truthy/falsy vocabulary mirrors the
COMMON :func:`brutex.engine.sweep_lookahead.env_flag_tristate` as a LOCAL
twin (equality-pinned in the unit suite) — NOT an import, because the engine
package hard-imports the ``fno_math`` wheel and a lake resample must never
require it (the ``lake_rust_render`` precedent, verbatim).

## The fail-closed layers (every failure serves the Python oracle)

1. **Import/API guard** — an absent wheel or a module missing any
   :data:`_CRATE_API` callable latches ``crate_unavailable`` (one loud WARN
   naming the remedy); every resample runs the Python oracle.
2. **Version guard** — the crate's ``pinned_polars_version()`` must equal
   the installed ``polars.__version__`` (ADR-0020 §7 hazard 1); a mismatch
   latches ``polars_version_mismatch`` naming BOTH versions.
3. **Burn-in self-check** — the first :data:`BURN_IN_SAMPLES` engaged
   resamples per process (PLUS every previously-unseen ``schema × tf``
   shape token) run BOTH engines and compare at DTYPE-EXACT frame strength
   (schema equality AND ``DataFrame.equals`` — ``.equals`` alone is
   dtype-INSENSITIVE, the #1795b documented semantics); ANY difference
   ships the ORACLE frame and autoreverts the Rust path for the process
   (``lake_rust_resample.autoreverted`` at ERROR).

Any crate raise degrades to the oracle (:func:`_degrade_errors` /
:func:`_panic_belt_errors` — the render facade's exact sets);
``MemoryError`` re-raises RAW (an OOM stays a global stop — never a silent
fallback); ``KeyboardInterrupt``/``SystemExit`` propagate. A non-``bytes``
crate return is refuse-poison (never decoded, served loudly from the
oracle). VALIDATION STAYS PYTHON: ``tf_resampler._validate_input`` + the
``ALL_TF_MINUTES`` membership check run BEFORE this seam, so the crate may
assume a clean frame.

Flag OFF (the default) is BYTE-IDENTICAL to today — the seam is never
called (bomb-spy-pinned in the unit suite); no module here imports polars
at module scope. Process-local latch state is the documented
no-global-mutable-state exception (the ``lake_rust_render`` shape);
:func:`reset_lake_rust_resample_for_tests` restores a fresh process view —
test isolation only, never called from production paths.
"""

from __future__ import annotations

import importlib
import io
import os
from typing import TYPE_CHECKING, Any, Final

import structlog

if TYPE_CHECKING:  # pragma: no cover - import only for static typing
    from collections.abc import Callable
    from types import ModuleType

    import polars as pl

_LOG = structlog.get_logger("brutex.data.lake_rust_resample")

#: The env flag. DEFAULT OFF — the Python resample stays the production path
#: until the parity gates have an operator-directed default flip behind a
#: future DEC (honesty banner: expected payoff ≈ 0 — uniformity, not speed).
LAKE_RUST_RESAMPLE_ENV: Final[str] = "BRUTEX_LAKE_RUST_RESAMPLE"

#: Engaged resamples frame-compared against the Python oracle per process
#: (the burn-in self-check). 16 mirrors the render facade / the
#: sweep-metrics kernel's ``SELF_CHECK_SAMPLES`` (the #1531/#1532 precedent).
BURN_IN_SAMPLES: Final[int] = 16

#: Truthy env literals — the LOCAL twin of the sweep flags' vocabulary
#: (equality-pinned vs the render facade + the engine copy in the unit
#: suite; not imported — module docstring).
_TRUTHY: Final[frozenset[str]] = frozenset({"1", "true", "yes", "on"})

#: Falsy env literals — the explicit kill-switch vocabulary.
_FALSY: Final[frozenset[str]] = frozenset({"0", "false", "no", "off"})

#: The latch: the disable reason once the Rust path is off for this process,
#: else ``None`` (the documented no-global-mutable-state exception).
_DISABLED_REASON: Final[list[str | None]] = [None]

#: Burn-in samples still to run this process (same documented exception).
_BURN_IN_REMAINING: Final[list[int]] = [BURN_IN_SAMPLES]

#: ``schema × tf`` shape tokens already frame-compared this process (the
#: shape gate — module docstring layer 3). Bounded by construction: the ONE
#: production caller sees the lake's few frame schemas (OHLCV-6 spot /
#: 7-column OI / 17-column Greeks overlay) × the 21 fixed TFs.
_BURN_IN_SHAPES_SEEN: Final[set[str]] = set()

#: Whether the shape gate is armed. Always ``True`` in production; the test
#: reset seam disarms it together with a zeroed count budget so
#: ``remaining=0`` keeps meaning "steady state — crate frames, no oracle".
_BURN_IN_SHAPE_GATE: Final[list[bool]] = [True]

#: The crate API surface the import guard verifies (layer 1) — a module that
#: imports but lacks any of these is a foreign/broken install and latches
#: ``crate_unavailable`` instead of raising ``AttributeError`` mid-derive.
_CRATE_API: Final[tuple[str, ...]] = (
    "pinned_polars_version",
    "resample_1m_ipc",
    "resample_all_ipc",
    "linked_zstd_version",
)

#: Once-per-process-per-reason warn memo + the engaged-once latch. LOG-VOLUME
#: state ONLY: resampled frames never depend on it.
_OBSERVABILITY_MEMO: Final[set[str]] = set()

#: Memo key for the engaged-once INFO (namespaced apart from warn reasons).
_ENGAGED_KEY: Final[str] = "__engaged__"


def env_flag_tristate(raw: str) -> bool | None:
    """Classify *raw* against the flag vocabulary (local twin — module docs).

    ``True`` for a :data:`_TRUTHY` literal, ``False`` for a :data:`_FALSY`
    literal, ``None`` for anything else (unset/empty AND unrecognised — the
    caller owns the default + warn policy). Byte-identical semantics to
    :func:`brutex.data.lake_rust_render.env_flag_tristate` (and the engine
    copy), equality-pinned in the unit suite.
    """
    value = raw.strip().lower()
    if value in _TRUTHY:
        return True
    if value in _FALSY:
        return False
    return None


def lake_rust_resample_enabled() -> bool:
    """Resolve ``BRUTEX_LAKE_RUST_RESAMPLE`` (default OFF, junk fails loud-OFF).

    * a :data:`_TRUTHY` value → ``True`` (the opt-in);
    * a :data:`_FALSY` value → ``False`` (the explicit, silent OFF);
    * unset / whitespace-empty → ``False`` (the default);
    * any OTHER non-empty value → ``False`` with one loud
      ``lake_rust_resample.fallback`` WARN (reason ``flag_unrecognised``)
      per process — a typo'd opt-in must be VISIBLE, never silently ignored.

    Read per call (one ``os.environ`` probe against a group-by-scale
    resample) so parallel workers resolve their own inherited environment —
    no global flag state.
    """
    raw = os.environ.get(LAKE_RUST_RESAMPLE_ENV, "")
    verdict = env_flag_tristate(raw)
    if verdict is not None:
        return verdict
    if raw.strip():
        _warn_once("flag_unrecognised", value=raw)
    return False


def rust_resample_disabled() -> bool:
    """Return ``True`` iff a guard/autorevert latched the Rust path OFF this process."""
    return _DISABLED_REASON[0] is not None


def reset_lake_rust_resample_for_tests(*, remaining: int = BURN_IN_SAMPLES) -> None:
    """Reset the latch + burn-in budget + shape gate + warn memo (test isolation ONLY).

    ``remaining=0`` puts the process in the post-burn-in steady state —
    count budget spent AND the shape gate disarmed, so the returned frame
    is the crate's with no oracle in the path (the parity corpus depends on
    that: an armed shape gate would cushion a first-shape divergence by
    shipping the oracle frame). The default restores a fresh-process view
    (budget full, shape gate armed, no shapes seen). Never called from
    production code paths (the ``lake_rust_render`` reset-seam convention).
    """
    _DISABLED_REASON[0] = None
    _BURN_IN_REMAINING[0] = remaining
    _BURN_IN_SHAPES_SEEN.clear()
    _BURN_IN_SHAPE_GATE[0] = remaining > 0
    _OBSERVABILITY_MEMO.clear()


def _warn_once(reason: str, **context: object) -> None:
    """Log ``lake_rust_resample.fallback`` WARN once per process per *reason*.

    The seam runs once per derived TF per symbol (hundreds of thousands per
    prebuild), so a sticky reason would flood the logs; the first occurrence
    is loud, repeats fall through silently (the Python frame serves
    regardless). The event name is a LITERAL with the reason as a field —
    the ``lake_rust_render.fallback`` grep-able convention.
    """
    if reason in _OBSERVABILITY_MEMO:
        return
    _OBSERVABILITY_MEMO.add(reason)
    _LOG.warning("lake_rust_resample.fallback", reason=reason, **context)


def _disable(reason: str, *, error: bool = False, **context: object) -> None:
    """Latch the Rust path OFF for the remainder of the process.

    ``error=True`` logs ``lake_rust_resample.autoreverted`` at ERROR (the
    burn-in autorevert — a genuine wrongness signal, never throttled below
    the operator's view); guard latches log
    ``lake_rust_resample.rust_path_disabled`` at WARN. Repeat calls are
    no-ops (the latch is already set).
    """
    if _DISABLED_REASON[0] is not None:
        return
    _DISABLED_REASON[0] = reason
    if error:
        _LOG.error("lake_rust_resample.autoreverted", reason=reason, **context)
    else:
        _LOG.warning("lake_rust_resample.rust_path_disabled", reason=reason, **context)


def _resolve_crate() -> ModuleType | None:
    """Import guard (layer 1): return ``brutex_lake``, or latch OFF + ``None``.

    An absent wheel (``ImportError``) latches ``crate_unavailable`` — the
    facade is default-OFF and opt-in, so an operator who set the flag
    without installing the crate gets ONE loud WARN naming the remedy, and
    every resample serves the Python frame. A module that IMPORTS but lacks
    any :data:`_CRATE_API` callable (a name collision / a PRE-Wave-B wheel
    without the resample face / a hand-broken install) latches the SAME
    reason — the fail-closed contract is "never a new crash class", so an
    ``AttributeError`` must never leave this seam.
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
    that raises marks a foreign/stale install whose frames must not be
    trusted — latch ``crate_metadata_error`` (→ :data:`_METADATA_ERROR`) and
    let the caller serve the oracle, identically to ``crate_unavailable``
    (fail-closed, never a new crash class). ``MemoryError`` re-raises bare
    (a global stop, never a fallback). Returns the raw value un-typechecked:
    a non-str ``pinned_polars_version`` simply fails the ``!=`` version
    guard (fail-closed already), and the zstd fingerprint is log-only.
    """
    try:
        return getattr(crate, attr)()
    except MemoryError:
        raise
    except Exception as exc:
        _disable("crate_metadata_error", metadata_call=attr, error_detail=repr(exc))
        return _METADATA_ERROR


def _degrade_errors() -> tuple[type[Exception], ...]:
    """The narrow degrade-to-oracle catch set for the engaged resample path.

    ``ValueError``/``RuntimeError`` are the crate's declared FFI error
    surface (a crate-side panic is caught at the FFI boundary and mapped to
    ``RuntimeError``); ``TypeError``/``OverflowError`` are PyO3
    argument-conversion failures; ``AttributeError`` is the
    broken-crate-internals defensive belt (the import guard already refuses
    a module MISSING the API); ``PolarsError`` covers the IPC
    serialise/decode legs (``DataFrame.write_ipc`` / ``read_ipc``).
    Resolved lazily so this leaf keeps zero module-import-time engine deps;
    the ``ImportError`` arm is a defensive belt (polars is always already
    imported when the seam runs — the caller just validated a polars
    frame). ``MemoryError`` is DELIBERATELY absent: OOM propagates raw,
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

    The verbatim ``lake_rust_render._panic_belt_errors`` doctrine:
    ``pyo3_runtime.PanicException`` is the one ``BaseException`` subclass a
    pyo3 boundary can leak (a Rust panic escaping ``catch_unwind``); it must
    latch ``crate_panic`` and degrade to the oracle, but the repo convention
    (CodeQL ``py/catch-base-exception``) forbids a non-re-raising literal
    ``except BaseException``, so the class is resolved GUARDEDLY by name
    from its two plausible homes (``pyo3_runtime`` — a forward belt;
    ``polars.exceptions`` — the arm that resolves today) and folded beside
    ``Exception``. HONEST narrowing: a DISTINCT per-extension
    ``PanicException`` type object is not catchable by name and PROPAGATES —
    a deliberate, disclosed trade bounded by the crate's own ``catch_unwind``
    boundary plus the import/API/metadata/version guards preceding every
    engaged resample. ``KeyboardInterrupt``/``SystemExit``/``MemoryError``
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


def _frames_equal_dtype_exact(left: pl.DataFrame, right: pl.DataFrame) -> bool:
    """Frame equality at DTYPE-EXACT strength — the burn-in comparator.

    ``DataFrame.equals`` is dtype-INSENSITIVE (the #1795b documented
    semantics: a value-identical Float32/Float64 pun compares equal), so the
    schema (column names, ORDER, and dtypes) is compared FIRST; only a
    schema-identical pair proceeds to the value/row-order comparison. The
    derived frames feed the parquet writer downstream, where a dtype pun
    WOULD change bytes — hence dtype-exact, stricter than ``.equals`` alone.
    """
    return left.schema == right.schema and left.equals(right)


def resample_frame_via_rust(
    df_1m: pl.DataFrame,
    target_tf_minutes: int,
    *,
    oracle: Callable[[Any, int], pl.DataFrame],
) -> pl.DataFrame:
    """The routed resample — called by ``resample_1m_to_tf`` ONLY when the flag is ON.

    Runs the fail-closed layers (module docstring) and returns the derived
    frame. EVERY path returns a valid frame: a latched / guarded / degraded
    call returns ``oracle(df_1m, target_tf_minutes)`` (the live Python
    oracle — the verbatim pre-seam body), a burn-in mismatch returns the
    ORACLE frame, and only a guard-clean, burn-in-clean call returns the
    crate's frame. The input frame is ALREADY validated (schema / null-ts /
    strictly-increasing / TF membership — the caller's contract), so the
    crate may assume a clean frame; the crate carries OI + Greeks-overlay
    columns by PRESENCE (``oi_aware=True`` always — the Python core's own
    presence law, so spot frames stay OHLCV-6 verbatim).

    Parameters
    ----------
    df_1m
        The validated 1-minute frame (polars DataFrame).
    target_tf_minutes
        The target timeframe — an ``ALL_TF_MINUTES`` member (caller-gated).
    oracle
        The verbatim Python resample body
        (``tf_resampler._resample_1m_to_tf_python``) — the live parity
        oracle every degrade path serves.
    """
    if rust_resample_disabled():
        return oracle(df_1m, target_tf_minutes)
    crate = _resolve_crate()
    if crate is None:
        return oracle(df_1m, target_tf_minutes)
    pinned = _crate_metadata(crate, "pinned_polars_version")
    if pinned is _METADATA_ERROR:
        return oracle(df_1m, target_tf_minutes)
    import polars as pl

    if pinned != pl.__version__:
        # Layer 2 — the version guard (ADR-0020 §7 hazard 1): fail-closed,
        # ONE loud WARN naming BOTH versions, never silently different
        # frames (resampled VALUES transitively determine downstream
        # parquet bytes — the ADR Wave-B ordering law).
        _disable(
            "polars_version_mismatch",
            installed_polars=pl.__version__,
            crate_pinned_polars=pinned,
            remedy="align the wheel and the crate's pinned polars rev, then "
            "re-run the parity corpus "
            "(docs/runbooks/lake-rust-render-lockstep.md)",
        )
        return oracle(df_1m, target_tf_minutes)
    try:
        buffer = io.BytesIO()
        df_1m.write_ipc(buffer, compression="uncompressed")
        rust_bytes = crate.resample_1m_ipc(buffer.getvalue(), target_tf_minutes, True)
    except _degrade_errors() as exc:
        _warn_once("resample_error", error_detail=repr(exc))
        return oracle(df_1m, target_tf_minutes)
    except (KeyboardInterrupt, SystemExit, MemoryError):
        # The escape set (the errors.py doctrine): operator interrupt /
        # interpreter shutdown / OOM stay global — never a fallback. This
        # clause sits ABOVE the panic belt so the belt's `Exception` member
        # can never swallow a MemoryError.
        raise
    except _panic_belt_errors() as exc:
        # The panic belt over the crate's own catch_unwind (the render
        # facade's doctrine verbatim — see _panic_belt_errors).
        _disable("crate_panic", error_detail=repr(exc))
        return oracle(df_1m, target_tf_minutes)
    if not isinstance(rust_bytes, bytes):
        # Refuse-poison gate: a non-bytes return is a crate BUG — never
        # decoded, served loudly from the oracle. Structurally unreachable
        # through the real crate (its FFI constructs PyBytes); guards a
        # stale/foreign install.
        _warn_once("resample_result_refused", result_type=type(rust_bytes).__name__)
        return oracle(df_1m, target_tf_minutes)
    decode_errors: tuple[type[Exception], ...] = (*_degrade_errors(), OSError)
    try:
        rust_df = pl.read_ipc(io.BytesIO(rust_bytes))
    except decode_errors as exc:
        # OSError joins the DECODE leg only (probed: polars' IPC reader
        # surfaces a truncated/corrupt in-memory payload as an OSError from
        # its seek) — a poisoned result payload degrades, never crashes.
        _warn_once("result_decode_error", error_detail=repr(exc))
        return oracle(df_1m, target_tf_minutes)
    if _ENGAGED_KEY not in _OBSERVABILITY_MEMO:
        linked_zstd = _crate_metadata(crate, "linked_zstd_version")
        if linked_zstd is _METADATA_ERROR:
            # A raising metadata pyfunction marks a foreign/stale install —
            # its frames are not trusted either (fail-closed; latched above).
            return oracle(df_1m, target_tf_minutes)
        _OBSERVABILITY_MEMO.add(_ENGAGED_KEY)
        _LOG.info(
            "lake_rust_resample.engaged",
            crate_pinned_polars=pinned,
            linked_zstd=linked_zstd,
        )
    schema_token = f"{df_1m.schema}|tf={target_tf_minutes}"
    shape_unseen = _BURN_IN_SHAPE_GATE[0] and schema_token not in _BURN_IN_SHAPES_SEEN
    if _BURN_IN_REMAINING[0] > 0 or shape_unseen:
        # Layer 3 — burn-in self-check (the #1531/#1532 pattern + the shape
        # gate): both engines resample; ANY dtype-exact frame difference
        # ships the ORACLE frame and autoreverts the Rust path.
        oracle_df = oracle(df_1m, target_tf_minutes)
        if not _frames_equal_dtype_exact(rust_df, oracle_df):
            _disable(
                "burn_in_frame_mismatch",
                error=True,
                rust_shape=str(rust_df.shape),
                python_shape=str(oracle_df.shape),
                schema_token=schema_token,
            )
            return oracle_df
        _BURN_IN_SHAPES_SEEN.add(schema_token)
        if _BURN_IN_REMAINING[0] > 0:
            _BURN_IN_REMAINING[0] -= 1
            if _BURN_IN_REMAINING[0] == 0:
                _LOG.info("lake_rust_resample.burn_in_complete", samples=BURN_IN_SAMPLES)
        return rust_df
    return rust_df


__all__ = [
    "BURN_IN_SAMPLES",
    "LAKE_RUST_RESAMPLE_ENV",
    "env_flag_tristate",
    "lake_rust_resample_enabled",
    "resample_frame_via_rust",
    "reset_lake_rust_resample_for_tests",
    "rust_resample_disabled",
]
