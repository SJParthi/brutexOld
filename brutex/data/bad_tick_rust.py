"""bad_tick_rust — the default-OFF Rust routing facade for the FRAME bad-tick screen.

DEC-DATA-RUST-INTEGRITY-DETECTORS-001 (SLICE T24 PR-2). Routes the
sequential arm of :func:`brutex.data.bad_tick.screen_frame_bad_ticks` (the
derived-TF prebuild / freshness-gate 1-minute read screen,
DEC-READ-GUARD-PARITY-001 #1811) through the EXISTING compiled
``brutex_feed`` decision kernel — the WHEEL-BOUNDARY reuse ruling: the
whole ``BadTickScreen`` state machine (session-boundary reset,
baseline-hold, first-offender capture, per-session warn coordinates, the
fixed-IST day-ordinal session key) is ALREADY compiled in
``rust/brutex-feed/src/scan.rs::scan_bars`` (DEC-FEED-RUST-CORE-001
#1797), so PR-2 ships ZERO new Rust for this module — both crates and both
committed ``Cargo.lock`` files stay byte-untouched, and the ONE compiled
state machine is the shared source (the strongest form of the CLAUDE.md
§11 COMMON one-source rule; the rejected hoist / cross-crate-dependency
alternatives are recorded in the DEC entry).

## Division of labour (the DEC-FEED-RUST-CORE-001 discipline)

* **Rust decides** — ``scan_bars(ts_us, closes, closes, closes, closes,
  True, max_ratio)`` returns kept-row indices + warn-event ROW COORDINATES
  only (the four price slots all receive the close column: the screen
  plane reads ONLY closes, and the kernel's positivity plane degenerates
  to a ``close <= 0`` probe — see the degrade contract below).
* **Python materializes** — the WARN records ride the ONE shared
  :func:`brutex.data.bad_tick.emit_bad_tick_warn` seam (session ISO string
  from Python's own datetime object, offender closes the SAME float
  objects the oracle would log), and the keep-mask ``frame.filter`` builds
  the SAME filtered frame the oracle builds. Nothing but ints crosses the
  FFI outward.

## The fail-closed layers (the T24 PR-1 facade template, adapted)

* **Flag** — :data:`FEED_RUST_FRAME_ENV` (``BRUTEX_FEED_RUST_FRAME``),
  default OFF (truthy → ON, falsy → OFF, junk → loud-OFF once; the §11
  COMMON :func:`~brutex.data.lake_rust_render.env_flag_tristate`).
  DELIBERATELY a separate flag from the engine boundary's
  ``BRUTEX_FEED_RUST`` (the design's Q1 recommendation, adopted): the two
  seams share the kernel but must keep INDEPENDENT blast radii — an
  operator who armed the replay seam must not silently arm this
  WRITE-feeding seam, and a frame-arm autorevert must not disable the
  engine-boundary arm. The flag gate lives in the TARGET
  (``screen_frame_bad_ticks``'s phase-2 branch — the G6 shape), so flag
  OFF is the literal verbatim sequential arm and ``brutex_feed`` is never
  imported from this seam (bomb-spy-pinned; the replay seam may import it
  under its OWN flag).
* **Engagement scope** — phase 1 (the vectorized candidate trigger) stays
  Python polars on BOTH arms: a clean frame returns the INPUT OBJECT and
  never reaches this facade, so the clean prebuild read is byte- AND
  cost-identical flag-ON. Only candidate-BEARING frames (poisoned lakes)
  route — the O(rows) ``to_list`` payload extraction below is paid by
  exactly those frames, disclosed.
* **Burn-in + autorevert** — the first :data:`BURN_IN_SAMPLES` engaged
  calls compare the kernel DECISIONS (kept rows + fully-materialized warn
  fields) against a side-effect-free run of the live ``BadTickScreen``
  oracle; ANY difference ships the FULL oracle result (warns emitted by
  the oracle itself — exactly once) and autoreverts the Rust path for the
  process (ERROR log). Justified here where the replay seam was
  golden-gated instead: this seam feeds STORED derived partitions.
* **Degrade set** — wheel missing/stale (latch), a non-``us``-unit /
  non-UTC ``Datetime`` ts column, ts/close nulls, a kernel raise from the
  narrow PR-1 set (``ValueError``/``RuntimeError``/``TypeError``/
  ``OverflowError``/``AttributeError``), the kernel ABSTAIN (``None`` —
  a ts outside the fixed-IST envelope), a
  :func:`~brutex.feed_rust.validate_scan` refusal, and — LOAD-BEARING for
  the ``tf_autoderive.read_lake_bars_frame`` consumer (#1932), which runs
  NO price gate before this screen — a kernel ``positivity_stop`` (a
  non-positive close legally reaches this seam there; the frame law never
  raises on it, so the stop DEGRADES to the oracle, never raises): each →
  oracle with one WARN per reason per process.
  ``MemoryError``/``KeyboardInterrupt``/``SystemExit`` re-raise RAW; a
  panic escaping the kernel (``pyo3_runtime.PanicException`` — the
  brutex-feed pyfunction carries no ``catch_unwind``) latches via the
  imported panic belt.

## Honesty (ADR-0020 banner; CLAUDE.md Golden Rule #10)

Expected payoff ≈ 0 — conversion is for uniformity, not speed. The routed
arm replaces a per-row Python walk on candidate-bearing frames only (rare
— poisoned lakes); the payload extraction + FFI hop is comparable cost.
Ships DEFAULT-OFF and INERT (a default flip needs its own DEC). The seam
feeds stored derived-partition bytes (transitively inside
``data_snapshot_hash`` for derived-TF sweeps), hence the burn-in + the
stored-outcome byte gates — decision parity, not speed, is the bar.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Final, cast

import structlog

from brutex.data.bad_tick import BadTickScreen, emit_bad_tick_warn

# §11 COMMON — ONE flag vocabulary + ONE panic belt for the data facades
# (the detectors_rust same-package import precedent; the #1911 PUBLIC
# ``panic_belt_errors`` alias, never the private twin).
from brutex.data.lake_rust_render import env_flag_tristate, panic_belt_errors
from brutex.data.session import ist_offset_duration
from brutex.feed_rust import resolve_feed_kernel, validate_scan

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import date, datetime

    import polars as pl

    from brutex.feed_rust import ScanResult

_LOG = structlog.get_logger(__name__)

#: The frame-screen opt-in flag (default OFF; junk fails loud-OFF once).
FEED_RUST_FRAME_ENV: Final[str] = "BRUTEX_FEED_RUST_FRAME"

#: Engaged calls that are decision-compared against the live oracle before
#: kernel decisions ship uncompared (the #1531/#1532 pattern).
BURN_IN_SAMPLES: Final[int] = 16

#: Process-local OFF latch (``None`` = live).
_DISABLED_REASON: Final[list[str | None]] = [None]

#: Burn-in budget (mutated in place; reset seam below).
_BURN_IN_REMAINING: Final[list[int]] = [BURN_IN_SAMPLES]

#: Once-per-process-per-reason log memo.
_OBSERVABILITY_MEMO: Final[set[str]] = set()

#: The engaged-INFO memo key (not a fallback reason — namespaced apart).
_ENGAGED_KEY: Final[str] = "__engaged__"

#: The narrow degrade-to-oracle catch set (the PR-1 facade set; module
#: docstring). MemoryError deliberately absent — OOM re-raises raw.
_DEGRADE_ERRORS: Final[tuple[type[Exception], ...]] = (
    ValueError,
    RuntimeError,
    TypeError,
    OverflowError,
    AttributeError,
)

#: The fixed UTC→IST wall-clock shift — the Python twin of the phase-1
#: polars ``ts + ist_offset_duration()`` expression, used ONLY for the ≤ a
#: few WARN rows' session ISO strings (never a whole-column pass).
_IST_SHIFT: Final[timedelta] = timedelta(hours=5, minutes=30)

#: One fully-materialized warn record for the burn-in / emission legs:
#: ``(session_iso, skipped, first_prev_close, first_close)``.
_WarnFields = tuple[str, int, float, float]


def frame_screen_rust_enabled() -> bool:
    """Resolve :data:`FEED_RUST_FRAME_ENV` (default OFF, junk loud-OFF once).

    Per-call ``os.environ`` read (spawn workers resolve their own inherited
    environment); truthy literal → ``True``, falsy/unset/blank → ``False``,
    any other non-empty value → ``False`` with one ``bad_tick_rust.fallback``
    WARN (``flag_unrecognised``) per process.
    """
    import os

    raw = os.environ.get(FEED_RUST_FRAME_ENV, "")
    verdict = env_flag_tristate(raw)
    if verdict is not None:
        return verdict
    if raw.strip():
        _warn_once("flag_unrecognised", value=raw, env_var=FEED_RUST_FRAME_ENV)
    return False


def frame_screen_rust_disabled() -> bool:
    """``True`` iff a guard/autorevert latched the Rust frame path OFF this process."""
    return _DISABLED_REASON[0] is not None


def reset_bad_tick_rust_for_tests(*, remaining: int = BURN_IN_SAMPLES) -> None:
    """Reset latch + burn-in budget + warn memo (test isolation ONLY).

    ``remaining=0`` puts the process in the post-burn-in steady state (the
    kernel decisions ship with no oracle in the path — the differential
    suites depend on that). Never called from production code.
    """
    _DISABLED_REASON[0] = None
    _BURN_IN_REMAINING[0] = remaining
    _OBSERVABILITY_MEMO.clear()


def _warn_once(reason: str, **context: object) -> None:
    """Log ``bad_tick_rust.fallback`` WARN once per process per *reason*."""
    if reason in _OBSERVABILITY_MEMO:
        return
    _OBSERVABILITY_MEMO.add(reason)
    _LOG.warning("bad_tick_rust.fallback", reason=reason, **context)


def _disable(reason: str, *, error: bool = False, **context: object) -> None:
    """Latch the Rust frame path OFF for the remainder of the process.

    ``error=True`` logs ``bad_tick_rust.autoreverted`` at ERROR (the
    burn-in autorevert — a genuine wrongness signal); guard latches log
    ``bad_tick_rust.rust_path_disabled`` at WARN. Repeats are no-ops.
    """
    if _DISABLED_REASON[0] is not None:
        return
    _DISABLED_REASON[0] = reason
    if error:
        _LOG.error("bad_tick_rust.autoreverted", reason=reason, **context)
    else:
        _LOG.warning("bad_tick_rust.rust_path_disabled", reason=reason, **context)


def _log_engaged_once() -> None:
    """Log ``bad_tick_rust.engaged`` INFO once per process."""
    if _ENGAGED_KEY in _OBSERVABILITY_MEMO:
        return
    _OBSERVABILITY_MEMO.add(_ENGAGED_KEY)
    _LOG.info("bad_tick_rust.engaged")


class _WarnRecorder:
    """A swallowing duck-typed logger capturing the oracle's warn FIELDS.

    Fed to the burn-in's side-effect-free ``BadTickScreen`` run so the
    oracle DECISIONS (incl. the exact warn field values) can be compared
    without emitting anything — log parity holds in every burn-in state
    (exactly one warn emission per call, whichever arm serves).
    """

    __slots__ = ("records",)

    def __init__(self) -> None:
        self.records: list[_WarnFields] = []

    def warning(self, event: str, **fields: object) -> None:
        del event
        self.records.append(
            (
                cast("str", fields["session"]),
                cast("int", fields["skipped"]),
                cast("float", fields["first_prev_close"]),
                cast("float", fields["first_close"]),
            )
        )


def _oracle_decisions(
    ist_dates: list[date],
    closes: list[float],
    *,
    source: str | None,
    max_ratio: float,
) -> tuple[list[int], list[_WarnFields]]:
    """Run the live ``BadTickScreen`` oracle SIDE-EFFECT-FREE (burn-in leg).

    Returns the kept row indices + the warn field records the oracle WOULD
    emit — the decision surface the kernel result is bit-compared against.
    """
    recorder = _WarnRecorder()
    screen = BadTickScreen(
        source=source,
        max_ratio=max_ratio,
        log=cast("structlog.stdlib.BoundLogger", recorder),
    )
    kept = [
        i
        for i, (d, close) in enumerate(zip(ist_dates, closes, strict=True))
        if screen.keep(ist_date=d, close=close)
    ]
    screen.finish()
    return kept, recorder.records


def _session_iso(ts_value: datetime) -> str:
    """The IST session ISO date of one bar's timestamp (warn materialization).

    ``(ts + 05:30).date()`` — for the routed dtype gate's admitted columns
    (naive-as-UTC or tz-aware UTC, the lake convention) this equals the
    phase-1 polars ``(ts + ist_offset_duration()).dt.date()`` session key
    AND the kernel's fixed-IST day-ordinal law (burn-in-compared against
    the oracle's own polars-derived dates).
    """
    return (ts_value + _IST_SHIFT).date().isoformat()


def routed_screen_frame(
    frame: pl.DataFrame,
    *,
    ts_column: str,
    source: str | None,
    max_ratio: float,
    log: structlog.stdlib.BoundLogger | None,
    python_law: Callable[[], pl.DataFrame],
) -> pl.DataFrame:
    """The routed sequential arm — called ONLY when the flag is ON.

    *frame* is the candidate-bearing sorted 1-minute frame (phase 1 already
    found a candidate); *python_law* is the zero-arg verbatim sequential
    oracle (:func:`~brutex.data.bad_tick._screen_frame_sequential_python`,
    closed over the same arguments). EVERY path returns a screened frame:
    latched / guarded / degraded calls return ``python_law()`` (which emits
    its own warns), a burn-in mismatch ships the ORACLE frame + autoreverts,
    and only a guard-clean call materializes the kernel decisions — warns
    through the shared :func:`~brutex.data.bad_tick.emit_bad_tick_warn`
    seam, rows through the same ``frame.filter`` construction. The function
    never raises beyond what the oracle itself could raise (plus the raw
    escape set — MemoryError/KeyboardInterrupt/SystemExit).
    """
    import polars as pl

    if frame_screen_rust_disabled():
        return python_law()
    kernel = resolve_feed_kernel()
    if kernel is None:
        # resolve_feed_kernel already warned (wheel_missing/wheel_stale on
        # the shared feed_rust registry); latch so later candidate frames
        # skip the re-import attempt.
        _disable("kernel_unavailable")
        return python_law()
    ts_dtype = frame.schema[ts_column]
    if not (
        isinstance(ts_dtype, pl.Datetime)
        and ts_dtype.time_unit == "us"
        and ts_dtype.time_zone in (None, "UTC")
    ):
        # The kernel's fixed-IST day-ordinal session law is provably equal
        # to the phase-1 polars expression ONLY for epoch-µs UTC instants
        # (naive = UTC by lake convention). Any other unit/zone → oracle.
        _warn_once("frame_ts_dtype", dtype=str(ts_dtype))
        return python_law()
    ts_col = frame.get_column(ts_column)
    close_col = frame.get_column("close")
    if ts_col.null_count() > 0 or close_col.null_count() > 0:
        # Nulls cannot cross the ints/floats-only FFI — the oracle owns
        # whatever behavior the null shape has today (unchanged).
        _warn_once("frame_nulls")
        return python_law()
    ts_us = cast("list[int]", ts_col.to_physical().to_list())
    closes = cast("list[float]", close_col.to_list())
    n = frame.height
    try:
        result: ScanResult | None = kernel(ts_us, closes, closes, closes, closes, True, max_ratio)
    except (KeyboardInterrupt, SystemExit, MemoryError):
        # The escape set (the errors.py doctrine): operator interrupt /
        # interpreter shutdown / OOM stay global — never a silent fallback.
        # Checked BEFORE the degrade set so RuntimeError can never shadow
        # them, and the panic belt below never swallows them either.
        raise
    except _DEGRADE_ERRORS as exc:
        _warn_once("kernel_error", error_detail=repr(exc))
        return python_law()
    except panic_belt_errors() as exc:
        _disable("kernel_panic", error_detail=repr(exc))
        return python_law()
    if result is None:
        # The kernel ABSTAIN: some ts lies outside the fixed-IST envelope
        # [epoch 0, 9999-12-31T00:00:00Z) — the #1919 ts-range gate catches
        # this class upstream at make_lake_read_1m but NOT at
        # tf_autoderive.read_lake_bars_frame, so the abstain is the belt.
        _warn_once("ts_out_of_ist_envelope")
        return python_law()
    if not validate_scan(result, n):
        # validate_scan warned (kernel_invalid on the feed_rust registry) —
        # a poisoned wheel can only degrade, never serve.
        return python_law()
    kept, warns, stop = result
    if stop is not None:
        # The tf_autoderive precondition trap (module docstring): the frame
        # law screens a non-positive close as an ordinary ratio decision —
        # it NEVER raises — while the kernel's positivity plane STOPS the
        # scan. Any stop therefore degrades the whole call to the oracle.
        _warn_once("unexpected_nonpositive_close")
        return python_law()
    kernel_warn_fields: list[_WarnFields] = [
        (
            _session_iso(cast("datetime", ts_col[session_first_row])),
            skipped,
            closes[first_prev_row],
            closes[first_skip_row],
        )
        for (_, session_first_row, skipped, first_prev_row, first_skip_row) in warns
    ]
    if _BURN_IN_REMAINING[0] > 0:
        ist_dates = cast(
            "list[date]",
            frame.select((pl.col(ts_column) + ist_offset_duration()).dt.date().alias("d"))
            .get_column("d")
            .to_list(),
        )
        oracle_kept, oracle_warns = _oracle_decisions(
            ist_dates, closes, source=source, max_ratio=max_ratio
        )
        if kept != oracle_kept or kernel_warn_fields != oracle_warns:
            _disable(
                "burn_in_mismatch",
                error=True,
                kernel_kept=len(kept),
                oracle_kept=len(oracle_kept),
                kernel_warns=repr(kernel_warn_fields),
                oracle_warns=repr(oracle_warns),
            )
            return python_law()
        _BURN_IN_REMAINING[0] -= 1
        if _BURN_IN_REMAINING[0] == 0:
            _LOG.info("bad_tick_rust.burn_in_complete", samples=BURN_IN_SAMPLES)
    for session_iso, skipped, first_prev_close, first_close in kernel_warn_fields:
        emit_bad_tick_warn(
            source=source,
            session_iso=session_iso,
            skipped=skipped,
            max_ratio=max_ratio,
            first_prev_close=first_prev_close,
            first_close=first_close,
            log=log,
        )
    _log_engaged_once()
    keep_mask = [False] * n
    for i in kept:
        keep_mask[i] = True
    return frame.filter(pl.Series("keep", keep_mask, dtype=pl.Boolean))


__all__ = [
    "BURN_IN_SAMPLES",
    "FEED_RUST_FRAME_ENV",
    "frame_screen_rust_disabled",
    "frame_screen_rust_enabled",
    "reset_bad_tick_rust_for_tests",
    "routed_screen_frame",
]
