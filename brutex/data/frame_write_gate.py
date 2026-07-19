"""Control leaf for the FRAME-DIRECT derived-TF prebuild write (DEC-PREBUILD-FRAME-WRITE-001).

The verify-or-degrade FRAME-DIRECT fast lane (``ParquetWriter.write_frame`` /
``ParquetBarSink.append_frame``) never materialises per-bar Python objects on
the prebuild's derived-timeframe writes — the measured GIL-held wall
(polars→CandleBar→polars round-trip: CONVERT 21.9–23.9% + ``_tick_round``
18.3–20.1% of the cold prebuild; convert+tick re-paid in full on 1,581/1,581
warm no-op merges — Phase-1/V2, sandbox ratios). This module is its
rust_metrics-shaped CONTROL surface (DEC-SWEEP-RUST-METRICS-DEFAULT-ON-001
precedent), deliberately WHEEL-FREE and polars-free:

* the tristate + ``shadow`` kill switch (:func:`resolve_frame_write_mode` over
  ``BRUTEX_PREBUILD_FRAME_WRITE``) with the one-constant default flip lever
  (:data:`PREBUILD_FRAME_WRITE_DEFAULT` — ``False`` in PR-1, dark);
* the per-process burn-in budgets (8 fresh + 8 merge admissions run BOTH
  lanes, ship the LEGACY bytes, bit-compare) + the durable-for-the-process
  autorevert latch;
* the CLOSED degrade-reason taxonomy (:class:`DegradeReason`) + the warn-once
  observability memos — a degrade NEVER decides correctness (it hands the
  partition to the verbatim legacy path, which owns bytes AND errors); the
  taxonomy exists for observability only;
* the per-builder :class:`FrameWriteStats` counters the
  ``lake.derived.write_summary`` INFO folds in as ADDITIVE fields.

Kill-switch law (the rust_metrics kill-must-kill rule — a SANCTIONED,
DEC-recorded deviation from the house fail-loud-env law): an unrecognised
value fails SAFE to OFF with one warn-once ``flag_unrecognised`` event —
under a future default-ON flip a typo'd kill switch (``0ff``, ``flase``)
must still KILL, never crash the prebuild and never be silently ignored.

Thread-safety: the burn-in budgets + the latch are single-cell module state
(the documented no-global-mutable-state exception —
``brutex/engine/rust_metrics.py`` precedent): decrements are clamped at zero,
the latch is first-writer-wins, and a racing double-check merely runs the
legacy/fast compare twice (benign — both racers ship legacy bytes).
``LakeWriteHalt`` / ``MemoryError`` are NEVER caught by any consumer of this
module (the G9 belt re-raises them — V3 C7).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum, unique
from typing import Final

import structlog

from brutex.config.env_flags import env_flag_tristate

_LOG = structlog.get_logger("brutex.data.frame_write_gate")

#: The kill switch. ``1/true/yes/on`` → the fast lane (+ burn-in);
#: ``0/false/no/off`` → legacy, silent; ``shadow`` → both lanes, ship legacy,
#: compare + count; unset/blank → :data:`PREBUILD_FRAME_WRITE_DEFAULT`;
#: anything else → fail-safe OFF + warn-once (kill-must-kill).
PREBUILD_FRAME_WRITE_ENV: Final[str] = "BRUTEX_PREBUILD_FRAME_WRITE"

#: The one-constant default flip lever (the ADR-0017 /
#: DEC-SWEEP-RUST-METRICS-DEFAULT-ON-001 template). PR-1 ships DARK
#: (``False`` — at defaults the change is provably inert: the Seam-A branch
#: executes the verbatim legacy call and no fast-lane code touches the
#: frame, negative-pinned). The PR-2 flip to ``True`` is its own DEC,
#: licensed by a clean operator shadow transcript
#: (``frame_shadow_mismatches == 0`` on the real ~750-symbol lake).
PREBUILD_FRAME_WRITE_DEFAULT: Final[bool] = False

#: The extra flag literal beyond the COMMON tristate vocabulary: run BOTH
#: lanes on every eligible write, SHIP the legacy bytes, bit-compare + count
#: — the default-ON evidence generator (zero byte risk).
SHADOW_LITERAL: Final[str] = "shadow"

#: Fresh-lane admissions bit-compared against the legacy render per process
#: (legacy bytes ship while the budget lasts). Bounded so the double
#: computation is an amortised-nil startup cost (≤16 double-computed
#: partitions ≈ 1% of one symbol's ~1,581 derived writes), re-armed at every
#: process start so each dashboard boot / nightly / crossverify leg
#: re-verifies the environment (polars bump, platform change).
FRESH_SELF_CHECK_SAMPLES: Final[int] = 8

#: Merge-lane (no-op verdict) admissions cross-checked per process — the
#: warm-steady-state twin of :data:`FRESH_SELF_CHECK_SAMPLES`.
MERGE_SELF_CHECK_SAMPLES: Final[int] = 8


@unique
class FrameWriteMode(Enum):
    """The resolved kill-switch verdict for one write call."""

    OFF = "off"
    ON = "on"
    SHADOW = "shadow"


@unique
class DegradeReason(Enum):
    """The CLOSED degrade taxonomy (DEC-PREBUILD-FRAME-WRITE-001 §4.2).

    A degrade NEVER decides correctness — it hands the partition to the
    VERBATIM legacy code path, which owns the bytes AND the failure
    semantics; the reason exists for observability only. Exhaustiveness is
    unit-pinned (a new guard must name its member here or fail the pin).

    ``MERGE_DELTA`` is the ONE expected-in-production reason (the ~20 real
    current-month rewrites per symbol per daily pull) and is classified
    NORMAL (DEBUG + counter, never a WARN). ``DISABLED`` / ``AUTOREVERTED``
    / ``FLAG_UNRECOGNISED`` are gate verdicts, not degrades — a merely-
    disabled call is never counted. ``SHADOW_MISMATCH`` is non-latching
    (shadow's job is to enumerate divergences, not to stop).
    """

    DISABLED = "disabled"
    AUTOREVERTED = "autoreverted"
    FLAG_UNRECOGNISED = "flag_unrecognised"
    SCHEMA_MISMATCH = "schema_mismatch"
    DUP_OR_UNSORTED_TS = "dup_or_unsorted_ts"
    DOMAIN_VIOLATION = "domain_violation"
    MAGNITUDE_OVERFLOW = "magnitude_overflow"
    SNAP_OVERFLOW = "snap_overflow"
    MERGE_STORED_SNAP = "merge_stored_snap"
    MERGE_STORED_DUPS = "merge_stored_dups"
    MERGE_OVERLAY = "merge_overlay"
    MERGE_STORED_SCHEMA = "merge_stored_schema"
    MERGE_DELTA = "merge_delta"
    FAST_LANE_ERROR = "fast_lane_error"
    SHADOW_MISMATCH = "shadow_mismatch"


#: The gate-verdict members that are NOT partition degrades (never counted in
#: :attr:`FrameWriteStats.degraded`): a disabled/latched/junk-flag call takes
#: the legacy path by GATE decision, not because a guard found an unprovable
#: shape. ``SHADOW_MISMATCH`` is its own counter, not a degrade either.
GATE_VERDICT_REASONS: Final[frozenset[DegradeReason]] = frozenset(
    {
        DegradeReason.DISABLED,
        DegradeReason.AUTOREVERTED,
        DegradeReason.FLAG_UNRECOGNISED,
        DegradeReason.SHADOW_MISMATCH,
    }
)

#: The one expected-in-production degrade — logged at DEBUG per partition,
#: counted per symbol, never a WARN (a genuine add/restate is NORMAL on the
#: daily warm set).
NORMAL_DEGRADE_REASONS: Final[frozenset[DegradeReason]] = frozenset({DegradeReason.MERGE_DELTA})


@dataclass(slots=True)
class FrameWriteStats:
    """Per-builder frame-write counters (one instance per symbol derive).

    Folded into the existing ``lake.derived.write_summary`` INFO as ADDITIVE
    fields (``frame_fast`` / ``frame_degraded`` / ``frame_degrade_reasons`` /
    ``frame_shadow_checked`` / ``frame_shadow_mismatches``) — emitted only
    while the gate is engaged, so the flag-OFF summary payload is
    byte-identical to the pre-change one. MUTABLE by design: one instance is
    created per ``make_lake_write_tf`` builder and every write inside that
    symbol's derive runs on the ONE derive thread (the per-symbol
    ``derive_write_lock`` is exclusive), so no lock is needed — documented,
    never assumed elsewhere.

    Attributes
    ----------
    fast
        Fast-lane ADMISSIONS in ON mode — writes whose verdict the fast lane
        reached (including the ≤16 burn-in admissions, which ship legacy
        bytes while verifying). NOT incremented in shadow mode (those count
        under :attr:`shadow_checked`).
    degraded
        Partition degrades (guard failures routed to the verbatim legacy
        path). Gate verdicts (:data:`GATE_VERDICT_REASONS`) never count.
    degrade_reasons
        ``{reason.value: count}`` per degrade — bounded by the closed enum.
    shadow_checked
        Shadow-mode writes that ran both lanes and compared.
    shadow_mismatches
        Shadow comparisons that DIVERGED (each also raises the never-throttled
        ``prebuild.frame_write.shadow_mismatch`` ERROR at the write site).
    """

    fast: int = 0
    degraded: int = 0
    degrade_reasons: dict[str, int] = field(default_factory=dict)
    shadow_checked: int = 0
    shadow_mismatches: int = 0

    def count_degrade(self, reason: DegradeReason) -> None:
        """Count one partition degrade (gate verdicts excluded by the caller)."""
        self.degraded += 1
        self.degrade_reasons[reason.value] = self.degrade_reasons.get(reason.value, 0) + 1


#: Burn-in samples still owed this process, per lane (single-cell mutable —
#: the documented no-global-mutable-state exception, module docstring).
_FRESH_REMAINING: Final[list[int]] = [FRESH_SELF_CHECK_SAMPLES]
_MERGE_REMAINING: Final[list[int]] = [MERGE_SELF_CHECK_SAMPLES]

#: The autorevert latch: the mismatch reason once the fast lane is disabled
#: for this process, else ``None`` (same documented exception).
_AUTOREVERT_REASON: Final[list[str | None]] = [None]

#: Once-per-process-per-key observability memo (warn-once fallbacks, the
#: engaged-once INFO, the per-lane burn-in-complete INFO). LOG-VOLUME state
#: ONLY — results never depend on it.
_OBSERVABILITY_MEMO: Final[set[str]] = set()


def resolve_frame_write_mode() -> FrameWriteMode:
    """Resolve ``BRUTEX_PREBUILD_FRAME_WRITE`` over :data:`PREBUILD_FRAME_WRITE_DEFAULT`.

    * a truthy literal (``1/true/yes/on``) → :attr:`FrameWriteMode.ON` (the
      fast lane + burn-in);
    * a falsy literal (``0/false/no/off``) → :attr:`FrameWriteMode.OFF` — the
      explicit operator kill switch, honoured regardless of the default;
    * ``shadow`` (case-insensitive, whitespace-stripped) →
      :attr:`FrameWriteMode.SHADOW` — both lanes, ship legacy, compare +
      count;
    * unset or whitespace-empty → the :data:`PREBUILD_FRAME_WRITE_DEFAULT`
      constant (the one-constant flip lever);
    * any OTHER value → **fail-safe OFF** with one warn-once
      ``flag_unrecognised`` event — the rust_metrics kill-must-kill rule
      (under a default-ON flip a typo'd kill switch must still KILL), a
      sanctioned + DEC-recorded deviation from the house fail-loud-env law,
      applied uniformly in both PR phases (one behavior, no phase fork).

    Vocabulary via the COMMON :func:`brutex.config.env_flags.env_flag_tristate`
    (this function owns only the ``shadow`` literal + the default/warn policy
    for the ``None`` verdict). Read per call — env vars are process-stable and
    the per-call read keeps parallel prebuild workers self-consistent (the
    rust_metrics precedent).
    """
    raw = os.environ.get(PREBUILD_FRAME_WRITE_ENV, "")
    if raw.strip().lower() == SHADOW_LITERAL:
        return FrameWriteMode.SHADOW
    verdict = env_flag_tristate(raw)
    if verdict is True:
        return FrameWriteMode.ON
    if verdict is False:
        return FrameWriteMode.OFF
    if raw.strip():
        # An unrecognised NON-EMPTY value fails SAFE to OFF, loudly (warn-once):
        # kill-must-kill — never a silently-ignored operator intent, never a
        # crashed prebuild on a typo (the sanctioned fail-loud-law deviation).
        warn_once(
            "flag_unrecognised",
            "prebuild.frame_write.degraded",
            reason=DegradeReason.FLAG_UNRECOGNISED.value,
            value=raw,
        )
        return FrameWriteMode.OFF
    return FrameWriteMode.ON if PREBUILD_FRAME_WRITE_DEFAULT else FrameWriteMode.OFF


def fresh_self_check_pending() -> bool:
    """``True`` while this process still owes FRESH-lane burn-in samples."""
    return _FRESH_REMAINING[0] > 0


def merge_self_check_pending() -> bool:
    """``True`` while this process still owes MERGE-lane burn-in samples."""
    return _MERGE_REMAINING[0] > 0


def consume_fresh_self_check_sample() -> None:
    """Consume one FRESH burn-in sample after a PASSED fast-vs-legacy bit-compare.

    Clamped at zero so a racing extra call can never go negative. A FAILED
    compare never consumes — it latches :func:`autorevert` instead. Emits the
    once-per-process ``burn_in_complete`` INFO when the lane's budget reaches
    zero.
    """
    if _FRESH_REMAINING[0] > 0:
        _FRESH_REMAINING[0] -= 1
        if _FRESH_REMAINING[0] == 0:
            info_once("burn_in_fresh", "prebuild.frame_write.burn_in_complete", lane="fresh")


def consume_merge_self_check_sample() -> None:
    """Consume one MERGE burn-in sample after a PASSED no-op-verdict cross-check."""
    if _MERGE_REMAINING[0] > 0:
        _MERGE_REMAINING[0] -= 1
        if _MERGE_REMAINING[0] == 0:
            info_once("burn_in_merge", "prebuild.frame_write.burn_in_complete", lane="merge")


def autoreverted() -> bool:
    """``True`` iff the autorevert latch has disabled the fast lane this process."""
    return _AUTOREVERT_REASON[0] is not None


def autorevert(reason: str, **context: object) -> None:
    """Latch the fast lane OFF for the remainder of the process (the autorevert).

    Called by the burn-in self-check on a fast-vs-legacy bit-compare MISMATCH
    — a fast-lane wrongness signal the admission guards cannot see. The first
    call logs ``prebuild.frame_write.autoreverted`` (ERROR — never throttled
    below the operator's view) carrying the divergence context (dest, lane,
    bytes-vs-which-field, both sha256s — supplied by the caller); repeat calls
    are no-ops (first-writer-wins). Every later ``write_frame`` call
    short-circuits to the verbatim legacy path with a warn-once fallback
    event. Because the LEGACY bytes always ship during burn-in, a latched
    process has never written a fast-lane byte.
    """
    if _AUTOREVERT_REASON[0] is None:
        _AUTOREVERT_REASON[0] = reason
        _LOG.error("prebuild.frame_write.autoreverted", reason=reason, **context)


def note_degrade(
    reason: DegradeReason, stats: FrameWriteStats | None = None, **context: object
) -> None:
    """Record one partition degrade — counted + observable, NEVER silent.

    The ONE degrade-observability chokepoint (CLAUDE.md §11 COMMON):

    * :data:`GATE_VERDICT_REASONS` members are NOT counted (a merely-disabled
      / latched call is a gate verdict, not a degrade) — the latch fallback
      still warns once per process so the operator sees the short-circuit;
    * :data:`NORMAL_DEGRADE_REASONS` (``merge_delta``) log DEBUG per
      partition + count (the expected daily warm-set shape);
    * every OTHER reason is an anomaly on a healthy lake: warn-once per
      reason per process (``prebuild.frame_write.degraded`` WARNING naming
      the first dest), DEBUG on repeats, always counted.
    """
    if reason in GATE_VERDICT_REASONS:
        if reason is DegradeReason.AUTOREVERTED:
            warn_once(
                "fallback_autoreverted",
                "prebuild.frame_write.degraded",
                reason=reason.value,
                **context,
            )
        return
    if stats is not None:
        stats.count_degrade(reason)
    if reason in NORMAL_DEGRADE_REASONS:
        _LOG.debug("prebuild.frame_write.degraded", reason=reason.value, **context)
        return
    warn_once(
        f"degrade_{reason.value}", "prebuild.frame_write.degraded", reason=reason.value, **context
    )


def warn_once(key: str, event: str, **context: object) -> None:
    """Log *event* as WARNING once per process per *key*; DEBUG on repeats.

    The write seam runs once per PARTITION, so a sticky anomaly reason would
    flood the logs at prebuild scale (~10⁵–10⁶ derived writes per full run —
    the DEC-PREBUILD-LOG-FLOOD-001 posture). The first occurrence is loud;
    repeats stay visible at DEBUG for a deliberately-verbose session. Never
    used for ``autoreverted`` / ``shadow_mismatch`` — those are ERROR and
    never throttled.
    """
    if key in _OBSERVABILITY_MEMO:
        _LOG.debug(event, **context)
        return
    _OBSERVABILITY_MEMO.add(key)
    _LOG.warning(event, **context)


def info_once(key: str, event: str, **context: object) -> None:
    """Log *event* as INFO once per process per *key* (engaged / burn-in-complete)."""
    if key in _OBSERVABILITY_MEMO:
        return
    _OBSERVABILITY_MEMO.add(key)
    _LOG.info(event, **context)


def note_engaged(mode: FrameWriteMode) -> None:
    """Emit the once-per-process ``engaged`` INFO (mode + burn-in budgets)."""
    info_once(
        "engaged",
        "prebuild.frame_write.engaged",
        mode=mode.value,
        fresh_self_check_samples=FRESH_SELF_CHECK_SAMPLES,
        merge_self_check_samples=MERGE_SELF_CHECK_SAMPLES,
    )


def note_shadow_mismatch(**context: object) -> None:
    """Log one NEVER-throttled shadow-mismatch ERROR (non-latching by design).

    Shadow's job is to ENUMERATE divergences over a whole real-lake run, so
    the latch is deliberately not set — the legacy bytes ship on every shadow
    write regardless, so a mismatch carries zero byte risk.
    """
    _LOG.error("prebuild.frame_write.shadow_mismatch", **context)


def reset_gate_for_tests(
    *,
    fresh_remaining: int = FRESH_SELF_CHECK_SAMPLES,
    merge_remaining: int = MERGE_SELF_CHECK_SAMPLES,
) -> None:
    """Reset the burn-in budgets + the autorevert latch (test isolation ONLY).

    ``fresh_remaining=0, merge_remaining=0`` puts the process in the
    post-burn-in steady state (the fast-lane-serves gates use it); the
    defaults restore a fresh-process view. Never called from production code.
    """
    _FRESH_REMAINING[0] = fresh_remaining
    _MERGE_REMAINING[0] = merge_remaining
    _AUTOREVERT_REASON[0] = None


def reset_observability_memo_for_tests() -> None:
    """Clear the warn-once / info-once memo (test isolation ONLY)."""
    _OBSERVABILITY_MEMO.clear()


__all__ = [
    "FRESH_SELF_CHECK_SAMPLES",
    "GATE_VERDICT_REASONS",
    "MERGE_SELF_CHECK_SAMPLES",
    "NORMAL_DEGRADE_REASONS",
    "PREBUILD_FRAME_WRITE_DEFAULT",
    "PREBUILD_FRAME_WRITE_ENV",
    "SHADOW_LITERAL",
    "DegradeReason",
    "FrameWriteMode",
    "FrameWriteStats",
    "autorevert",
    "autoreverted",
    "consume_fresh_self_check_sample",
    "consume_merge_self_check_sample",
    "fresh_self_check_pending",
    "info_once",
    "merge_self_check_pending",
    "note_degrade",
    "note_engaged",
    "note_shadow_mismatch",
    "reset_gate_for_tests",
    "reset_observability_memo_for_tests",
    "resolve_frame_write_mode",
    "warn_once",
]
