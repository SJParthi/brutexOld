"""Flag-gated Rust DAILY-resample seam — the ADR-0020 Wave-B tail facade.

DEC-LAKE-RUST-RESAMPLE-DAILY-001 (MIGRATION.md #1946); flipped default ON
by DEC-RUST-DEFAULT-ON-WAVE-001 (MIGRATION.md #2215). When
``BRUTEX_LAKE_RUST_RESAMPLE_DAILY`` is unset/blank/truthy (the explicit
``0`` value is the OFF kill switch), the two daily
derives — :func:`brutex.data.spot_daily_resample.resample_1m_to_daily`
(spot, OI ``None``) and
:func:`brutex.data.fno_daily_resample.resample_1m_to_fno_daily` (F&O,
carry-last OI) — route their pure FRAME core (375-minute aggregation →
forming-session drop → midnight-IST re-stamp) through the ``brutex_lake``
crate's ``resample_daily_ipc`` (``rust/brutex-lake/src/resample_daily.rs``).
The ``Decimal``/``CandleBar`` materialization stays Python on BOTH arms
(ADR-0020 row 14 — "output objects stay Python-side"); the forming-session
cutoff crosses the FFI as a DAYS-SINCE-EPOCH integer (ints-only
discipline — nothing richer than an int ever crosses).

The SIBLING of :mod:`brutex.data.lake_rust_resample` (the G5 facade) with
an INDEPENDENT flag, latch, and burn-in state — a daily autorevert never
disables the intraday seam and vice versa — while the STATELESS guard
machinery (flag vocabulary, degrade/panic-belt sets, the dtype-exact frame
comparator, the IPC handoff) is IMPORTED from the G5 module's public
aliases, never copied (CLAUDE.md §11 COMMON; the
``lake_rust_render.degrade_errors`` precedent).

## The flag — ``BRUTEX_LAKE_RUST_RESAMPLE_DAILY`` (DEFAULT ON)

The exact G5 vocabulary via the SHARED :func:`env_flag_tristate`: truthy →
ON; falsy ``0`` → OFF kill switch; unset/whitespace → ON (default); any
OTHER value → OFF with one
loud ``lake_rust_resample_daily.fallback`` WARN (``flag_unrecognised``) per
process. Independent of ``BRUTEX_LAKE_RUST_RESAMPLE`` — the daily law is a
DIFFERENT output surface (the lake's ``1day`` partition, the PDH/PDL
anchor) and flips on its own evidence.

## The fail-closed layers (every failure serves the Python oracle)

1. **Import/API guard** — an absent wheel or a module missing any
   :data:`_CRATE_API_DAILY` callable latches ``crate_unavailable`` (a
   pre-T11 wheel lacks ``resample_daily_ipc`` and latches HERE, leaving
   the G5 intraday seam untouched); every daily derive runs the oracle.
2. **Version guard** — the crate's ``pinned_polars_version()`` must equal
   the installed ``polars.__version__`` (ADR-0020 §7 hazard 1; the daily
   law IS a polars-frame law, so the guard is KEPT — unlike the
   polars-free stats/locks facades' documented deviation).
3. **Burn-in self-check** — the first :data:`BURN_IN_SAMPLES` engaged
   derives per process (PLUS every unseen ``schema × oi_aware`` shape
   token) run BOTH engines and compare at DTYPE-EXACT frame strength; ANY
   difference ships the ORACLE frame and autoreverts
   (``lake_rust_resample_daily.autoreverted`` at ERROR). The token carries
   NO ``before`` value (it varies daily; the cutoff law is
   value-independent integer arithmetic — value coverage lives in the
   parity corpus).

Any crate raise degrades to the oracle; ``MemoryError`` re-raises RAW;
``KeyboardInterrupt``/``SystemExit`` propagate; a non-``bytes`` return is
refuse-poison — all via the SHARED helpers. VALIDATION STAYS PYTHON:
``tf_resampler.validate_resample_frame`` runs BEFORE this seam at both
callers.

Flag OFF (the default) is BYTE-IDENTICAL to today — the seam is never
called (bomb-spy-pinned); no module here imports polars at module scope.
Process-local latch state is the documented no-global-mutable-state
exception; :func:`reset_lake_rust_resample_daily_for_tests` restores a
fresh process view — test isolation only.

Honesty banner (ADR-0020 §0, verbatim): expected payoff ≈ 0 — conversion
is for uniformity, not speed.
"""

from __future__ import annotations

import importlib
import io
import os
from datetime import date
from typing import TYPE_CHECKING, Any, Final

import structlog

from brutex.config.rust_strict import refuse_if_strict
from brutex.data.lake_rust_resample import (
    BURN_IN_SAMPLES,
    degrade_errors,
    env_flag_tristate,
    frames_equal_dtype_exact,
    ipc_payload,
    panic_belt_errors,
)

if TYPE_CHECKING:  # pragma: no cover - import only for static typing
    from collections.abc import Callable
    from types import ModuleType

    import polars as pl

_LOG = structlog.get_logger("brutex.data.lake_rust_resample_daily")

#: The env flag. DEFAULT ON per DEC-RUST-DEFAULT-ON-WAVE-001 (operator
#: directive 2026-07-23) — the explicit ``0`` value is the OFF kill switch
#: that restores the verbatim Python daily derive (honesty banner: expected
#: payoff ≈ 0 — uniformity, not speed).
LAKE_RUST_RESAMPLE_DAILY_ENV: Final[str] = "BRUTEX_LAKE_RUST_RESAMPLE_DAILY"

#: The Unix epoch date — ``before`` crosses the FFI as ``(before - _EPOCH).days``.
_EPOCH: Final[date] = date(1970, 1, 1)

#: The latch: the disable reason once the daily Rust path is off for this
#: process, else ``None`` (the documented no-global-mutable-state exception).
_DISABLED_REASON: Final[list[str | None]] = [None]

#: Burn-in samples still to run this process (same documented exception).
_BURN_IN_REMAINING: Final[list[int]] = [BURN_IN_SAMPLES]

#: ``schema × oi_aware`` shape tokens already frame-compared this process.
#: Bounded by construction: the two production callers see the lake's few
#: 1-minute schemas (OHLCV-6 spot / 7-column OI / 17-column Greeks overlay)
#: × the two ``oi_aware`` arms.
_BURN_IN_SHAPES_SEEN: Final[set[str]] = set()

#: Whether the shape gate is armed (disarmed together with a zeroed budget
#: by the test reset so ``remaining=0`` means the true steady state).
_BURN_IN_SHAPE_GATE: Final[list[bool]] = [True]

#: The crate API surface the import guard verifies — a module that imports
#: but lacks any of these (e.g. a pre-T11 wheel without the daily face) is
#: latched ``crate_unavailable`` instead of raising ``AttributeError``
#: mid-derive. The metadata pair is shared with the G5 face; the daily face
#: symbol is THIS facade's own gate.
_CRATE_API_DAILY: Final[tuple[str, ...]] = (
    "pinned_polars_version",
    "resample_daily_ipc",
    "linked_zstd_version",
)

#: Once-per-process-per-reason warn memo + the engaged-once latch.
_OBSERVABILITY_MEMO: Final[set[str]] = set()

#: Memo key for the engaged-once INFO (namespaced apart from warn reasons).
_ENGAGED_KEY: Final[str] = "__engaged__"


def lake_rust_resample_daily_enabled() -> bool:
    """Resolve ``BRUTEX_LAKE_RUST_RESAMPLE_DAILY`` (default ON per
    DEC-RUST-DEFAULT-ON-WAVE-001, operator directive 2026-07-23; junk fails
    loud-OFF).

    The exact G5 policy via the SHARED tristate: truthy → ``True``; falsy →
    ``False``; unset/whitespace-empty → ``True`` (the default); any OTHER
    non-empty value → ``False`` with one loud
    ``lake_rust_resample_daily.fallback`` WARN (``flag_unrecognised``) per
    process. Read per call so parallel workers resolve their own inherited
    environment.
    """
    raw = os.environ.get(LAKE_RUST_RESAMPLE_DAILY_ENV, "")
    verdict = env_flag_tristate(raw)
    if verdict is not None:
        return verdict
    if raw.strip():
        _warn_once("flag_unrecognised", value=raw)
        return False
    return True


def rust_resample_daily_disabled() -> bool:
    """Return ``True`` iff a guard/autorevert latched the daily Rust path OFF this process."""
    return _DISABLED_REASON[0] is not None


def reset_lake_rust_resample_daily_for_tests(*, remaining: int = BURN_IN_SAMPLES) -> None:
    """Reset the latch + burn-in budget + shape gate + warn memo (test isolation ONLY).

    ``remaining=0`` puts the process in the post-burn-in steady state —
    count budget spent AND the shape gate disarmed, so the returned frame
    is the crate's with no oracle in the path (the parity corpus depends on
    that). The default restores a fresh-process view. Never called from
    production code paths (the G5 reset-seam convention).
    """
    _DISABLED_REASON[0] = None
    _BURN_IN_REMAINING[0] = remaining
    _BURN_IN_SHAPES_SEEN.clear()
    _BURN_IN_SHAPE_GATE[0] = remaining > 0
    _OBSERVABILITY_MEMO.clear()


def days_since_epoch(before: date) -> int:
    """Render *before* to the days-since-epoch int the FFI carries.

    Pure calendar arithmetic (``(before - 1970-01-01).days``) — the crate's
    ``before_days`` compares it against each bucket's IST day ordinal
    (``div_euclid`` of the +05:30-shifted physical value), so the two sides
    share ONE integer day vocabulary.
    """
    return (before - _EPOCH).days


def _warn_once(reason: str, **context: object) -> None:
    """Log ``lake_rust_resample_daily.fallback`` WARN once per process per *reason*.

    DEC-RUST-STRICT-NO-FALLBACK-001: refuses under ``BRUTEX_RUST_STRICT``.
    Every reason funnelled here is CLASS A (``flag_unrecognised`` — the
    junk-flag arm; ``resample_error`` — the crate raised) or CLASS B
    (``resample_result_refused`` — a non-``bytes`` return;
    ``result_decode_error`` — the payload would not decode as a frame).
    There is NO class-C reason at this choke point, so no exemption set: the
    seam takes a 1-minute frame plus an integer ``days_since_epoch`` bound
    the crate always claims. Strict-OFF is byte-identical to the prior
    behaviour.
    """
    refuse_if_strict("brutex.data.lake_rust_resample_daily", reason, **context)
    if reason in _OBSERVABILITY_MEMO:
        return
    _OBSERVABILITY_MEMO.add(reason)
    _LOG.warning("lake_rust_resample_daily.fallback", reason=reason, **context)


def _disable(reason: str, *, error: bool = False, **context: object) -> None:
    """Latch the daily Rust path OFF for the remainder of the process.

    ``error=True`` logs ``lake_rust_resample_daily.autoreverted`` at ERROR
    (the burn-in autorevert); guard latches log
    ``lake_rust_resample_daily.rust_path_disabled`` at WARN. Repeat calls
    are no-ops. INDEPENDENT of the G5 latch — a daily autorevert never
    disables the intraday seam.

    DEC-RUST-STRICT-NO-FALLBACK-001: refuses under ``BRUTEX_RUST_STRICT``.
    Reasons here are CLASS A (``crate_unavailable`` /
    ``crate_metadata_error`` / ``polars_version_mismatch`` / ``crate_panic``)
    or CLASS B (``burn_in_frame_mismatch``); none is class C, so none is
    exempt. The refusal fires BEFORE the latch is written, so under strict
    mode the latch is never set and every
    ``rust_resample_daily_disabled()`` early-return is structurally
    unreachable. Strict-OFF is byte-identical to the prior behaviour.
    """
    refuse_if_strict("brutex.data.lake_rust_resample_daily", reason, error=error, **context)
    if _DISABLED_REASON[0] is not None:
        return
    _DISABLED_REASON[0] = reason
    if error:
        _LOG.error("lake_rust_resample_daily.autoreverted", reason=reason, **context)
    else:
        _LOG.warning("lake_rust_resample_daily.rust_path_disabled", reason=reason, **context)


def _resolve_crate() -> ModuleType | None:
    """Import guard (layer 1): return ``brutex_lake``, or latch OFF + ``None``.

    The G5 resolver's exact policy over THIS facade's API tuple: an absent
    wheel (``ImportError``) or a module missing any
    :data:`_CRATE_API_DAILY` callable (a pre-T11 wheel without the daily
    face / a foreign install) latches ``crate_unavailable`` — never an
    ``AttributeError`` mid-derive.
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
    for name in _CRATE_API_DAILY:
        if not callable(getattr(crate, name, None)):
            _disable(
                "crate_unavailable",
                error_detail=f"brutex_lake.{name} missing or not callable — foreign/stale install",
                remedy="pip install rust/brutex-lake (see docs/runbooks/lake-rust-render-lockstep.md)",
            )
            return None
    return crate


#: Sentinel for "the metadata call raised" — distinct from any RETURNED value.
_METADATA_ERROR: Final[object] = object()


def _crate_metadata(crate: ModuleType, attr: str) -> object:
    """Call a crate metadata pyfunction defensively; latch OFF on ANY raise.

    The G5 policy over THIS facade's latch: a raising metadata call marks a
    foreign/stale install → ``crate_metadata_error`` + the oracle serves;
    ``MemoryError`` re-raises bare. The raw value is returned un-typechecked
    (a non-str pinned version fails the ``!=`` version guard fail-closed).
    """
    try:
        return getattr(crate, attr)()
    except MemoryError:
        raise
    except Exception as exc:
        _disable("crate_metadata_error", metadata_call=attr, error_detail=repr(exc))
        return _METADATA_ERROR


def _engage_crate() -> tuple[ModuleType, object] | None:
    """The pre-invoke guard ladder (latch → import/API → version) — daily state.

    The structural twin of the G5 module's ``_engage_crate`` over THIS
    facade's latch/API; ``None`` ⇒ the caller serves its oracle.
    """
    if rust_resample_daily_disabled():
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
        # ONE loud WARN naming BOTH versions — daily VALUES transitively
        # determine the 1day partition bytes (the PDH/PDL anchor chain).
        _disable(
            "polars_version_mismatch",
            installed_polars=pl.__version__,
            crate_pinned_polars=pinned,
            remedy="align the wheel and the crate's pinned polars rev, then "
            "re-run the parity corpus "
            "(docs/runbooks/lake-rust-render-lockstep.md)",
        )
        return None
    return crate, pinned


def resample_daily_frame_via_rust(
    df_1m: pl.DataFrame,
    before: date,
    *,
    oi_aware: bool,
    oracle: Callable[[Any, date], pl.DataFrame],
) -> pl.DataFrame:
    """The routed daily-frame derive — called ONLY when the daily flag is ON.

    Returns the midnight-IST-stamped daily FRAME (``ts_utc`` +
    OHLCV ± ``open_interest``; Greeks overlays dropped — the daily face's
    select law). EVERY path returns a valid frame: a latched / guarded /
    degraded call returns ``oracle(df_1m, before)`` (the verbatim Python
    frame composite each caller retains), a burn-in mismatch returns the
    ORACLE frame, and only a guard-clean, burn-in-clean call returns the
    crate's frame. The input is ALREADY validated
    (``tf_resampler.validate_resample_frame`` — the callers' contract).

    Parameters
    ----------
    df_1m
        The validated 1-minute frame.
    before
        The forming-session cutoff (IST date) — sessions ``>= before`` are
        dropped; crosses the FFI as :func:`days_since_epoch`.
    oi_aware
        ``True`` for the F&O daily (carry-last OI when the column is
        present), ``False`` for spot (the OHLCV-6 output).
    oracle
        The verbatim Python daily-frame body
        (``spot_daily_resample._resample_daily_frame_python`` /
        ``fno_daily_resample._fno_daily_frame_python``) — the live parity
        oracle every degrade path serves.
    """
    engaged = _engage_crate()
    if engaged is None:
        return oracle(df_1m, before)
    crate, pinned = engaged
    before_days = days_since_epoch(before)
    try:
        rust_bytes = crate.resample_daily_ipc(ipc_payload(df_1m), before_days, oi_aware)
    except degrade_errors() as exc:
        _warn_once("resample_error", error_detail=repr(exc))
        return oracle(df_1m, before)
    except (KeyboardInterrupt, SystemExit, MemoryError):
        # The escape set (the errors.py doctrine): operator interrupt /
        # interpreter shutdown / OOM stay global — never a fallback; the
        # clause sits ABOVE the panic belt so its `Exception` member can
        # never swallow a MemoryError.
        raise
    except panic_belt_errors() as exc:
        # The panic belt over the crate's own catch_unwind (the G5/render
        # facade doctrine verbatim).
        _disable("crate_panic", error_detail=repr(exc))
        return oracle(df_1m, before)
    if not isinstance(rust_bytes, bytes):
        # Refuse-poison gate: a non-bytes return is a crate BUG — never
        # decoded, served loudly from the oracle.
        _warn_once("resample_result_refused", result_type=type(rust_bytes).__name__)
        return oracle(df_1m, before)
    import polars as pl

    decode_errors: tuple[type[Exception], ...] = (*degrade_errors(), OSError)
    try:
        rust_df = pl.read_ipc(io.BytesIO(rust_bytes))
    except decode_errors as exc:
        # OSError joins the DECODE leg only (the G5 probe: polars' IPC
        # reader surfaces a truncated payload as an OSError from its seek).
        _warn_once("result_decode_error", error_detail=repr(exc))
        return oracle(df_1m, before)
    if _ENGAGED_KEY not in _OBSERVABILITY_MEMO:
        linked_zstd = _crate_metadata(crate, "linked_zstd_version")
        if linked_zstd is _METADATA_ERROR:
            # A raising metadata pyfunction marks a foreign/stale install —
            # its frames are not trusted either (fail-closed; latched above).
            return oracle(df_1m, before)
        _OBSERVABILITY_MEMO.add(_ENGAGED_KEY)
        _LOG.info(
            "lake_rust_resample_daily.engaged",
            crate_pinned_polars=pinned,
            linked_zstd=linked_zstd,
        )
    schema_token = f"{df_1m.schema}|daily|oi_aware={oi_aware}"
    shape_unseen = _BURN_IN_SHAPE_GATE[0] and schema_token not in _BURN_IN_SHAPES_SEEN
    if _BURN_IN_REMAINING[0] > 0 or shape_unseen:
        # Layer 3 — burn-in self-check (the #1531/#1532 pattern + the shape
        # gate) on the INDEPENDENT daily state: both engines derive; ANY
        # dtype-exact frame difference ships the ORACLE frame + autoreverts.
        oracle_df = oracle(df_1m, before)
        if not frames_equal_dtype_exact(rust_df, oracle_df):
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
                _LOG.info("lake_rust_resample_daily.burn_in_complete", samples=BURN_IN_SAMPLES)
        return rust_df
    return rust_df


__all__ = [
    "BURN_IN_SAMPLES",
    "LAKE_RUST_RESAMPLE_DAILY_ENV",
    "days_since_epoch",
    "lake_rust_resample_daily_enabled",
    "resample_daily_frame_via_rust",
    "reset_lake_rust_resample_daily_for_tests",
    "rust_resample_daily_disabled",
]
