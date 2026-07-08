"""Unit tests for ``brutex.adapters.groww.epoch_verdict`` (DEC-GROWW-EPOCH-VERDICT-001).

The deterministic session-plausibility cross-check of the parser's UNVERIFIED
Unix-UTC epoch assumption. Sample epochs are COMPUTED from wall-clock instants
(never hardcoded magic numbers) against real calendar dates:

* 2024-06-19 — a regular Wednesday trading session (09:15-15:30 IST);
* 2024-06-16 — a Sunday (no session);
* 2040-01-04 — an out-of-holiday-coverage weekday (the strict calendar
  refuses → the sample abstains).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from brutex.adapters.groww.epoch_verdict import (
    MAX_VERDICT_SAMPLES,
    EpochSampleEvidence,
    EpochVerdictKind,
    classify_epoch_sample,
    classify_epoch_timestamps,
)
from structlog.testing import capture_logs


def _epoch_utc(year: int, month: int, day: int, hour: int, minute: int) -> float:
    """The Unix-UTC epoch of the given UTC wall instant."""
    return datetime(year, month, day, hour, minute, tzinfo=UTC).timestamp()


#: Unix-UTC reading lands 10:30 IST on the 2024-06-19 session; IST-naive
#: reading lands 05:00 (pre-open) — only the current assumption fits.
EPOCH_SUPPORTS_UNIX_UTC = _epoch_utc(2024, 6, 19, 5, 0)
#: IST-naive reading lands 10:30 IST in-session; Unix-UTC reading lands 16:00
#: (post-close) — only the FEARED interpretation fits.
EPOCH_SUPPORTS_IST_NAIVE = _epoch_utc(2024, 6, 19, 10, 30)
#: Unix-UTC 15:00 / IST-naive 09:30 — BOTH in-session (the overlap window).
EPOCH_AMBIGUOUS = _epoch_utc(2024, 6, 19, 9, 30)
#: Unix-UTC 23:00 / IST-naive 17:30 — NEITHER in-session.
EPOCH_OUT_OF_SESSION = _epoch_utc(2024, 6, 19, 17, 30)
#: Both readings land on the out-of-coverage weekday 2040-01-04 → abstain.
EPOCH_ABSTAINED = _epoch_utc(2040, 1, 4, 5, 0)
#: Both readings land on Sunday 2024-06-16 (no session) → out_of_session
#: through the ``session is None`` arm.
EPOCH_SUNDAY = _epoch_utc(2024, 6, 16, 5, 0)


# ── classify_epoch_sample ───────────────────────────────────────────────────
class TestClassifyEpochSample:
    @pytest.mark.parametrize(
        ("epoch", "expected"),
        [
            (EPOCH_SUPPORTS_UNIX_UTC, EpochSampleEvidence.SUPPORTS_UNIX_UTC),
            (EPOCH_SUPPORTS_IST_NAIVE, EpochSampleEvidence.SUPPORTS_IST_NAIVE),
            (EPOCH_AMBIGUOUS, EpochSampleEvidence.AMBIGUOUS),
            (EPOCH_OUT_OF_SESSION, EpochSampleEvidence.OUT_OF_SESSION),
            (EPOCH_ABSTAINED, EpochSampleEvidence.ABSTAINED),
            (EPOCH_SUNDAY, EpochSampleEvidence.OUT_OF_SESSION),
        ],
        ids=["unix-utc", "ist-naive", "ambiguous", "out-of-session", "abstained", "sunday"],
    )
    def test_session_classification(self, epoch: float, expected: EpochSampleEvidence) -> None:
        assert classify_epoch_sample(epoch) == expected

    def test_int_epoch_classifies_like_float(self) -> None:
        assert (
            classify_epoch_sample(int(EPOCH_SUPPORTS_UNIX_UTC))
            == EpochSampleEvidence.SUPPORTS_UNIX_UTC
        )

    def test_session_open_boundary_is_in_session(self) -> None:
        # Exactly 09:15:00 IST under Unix-UTC (03:45 UTC); IST-naive 03:45 is
        # out — inclusive-open makes this directional, not out-of-session.
        epoch = _epoch_utc(2024, 6, 19, 3, 45)
        assert classify_epoch_sample(epoch) == EpochSampleEvidence.SUPPORTS_UNIX_UTC

    def test_session_close_boundary_is_in_session(self) -> None:
        # Exactly 15:30:00 IST under Unix-UTC (10:00 UTC); IST-naive 10:00 is
        # ALSO in-session — the deliberately-inclusive close boundary can only
        # ADD ambiguity, never manufacture directional evidence.
        epoch = _epoch_utc(2024, 6, 19, 10, 0)
        assert classify_epoch_sample(epoch) == EpochSampleEvidence.AMBIGUOUS

    @pytest.mark.parametrize(
        "junk",
        [True, False, "1718773200", None, [1_700_000_000]],
        ids=["true", "false", "str", "none", "list"],
    )
    def test_non_numeric_is_invalid(self, junk: object) -> None:
        # bool is an int subclass — must be rejected explicitly, mirroring the parser.
        assert classify_epoch_sample(junk) == EpochSampleEvidence.INVALID

    @pytest.mark.parametrize(
        "epoch",
        [float("nan"), float("inf"), float("-inf")],
        ids=["nan", "inf", "-inf"],
    )
    def test_non_finite_is_invalid(self, epoch: float) -> None:
        assert classify_epoch_sample(epoch) == EpochSampleEvidence.INVALID

    def test_fromtimestamp_out_of_range_is_invalid(self) -> None:
        assert classify_epoch_sample(2**62) == EpochSampleEvidence.INVALID
        assert classify_epoch_sample(-(2**62)) == EpochSampleEvidence.INVALID

    def test_ist_offset_datetime_overflow_is_invalid(self) -> None:
        # fromtimestamp succeeds (year 9999) but +05:30 overflows datetime.max
        # — the addition itself must be inside the guard, never a raw
        # OverflowError out of an observe-only classifier.
        epoch = _epoch_utc(9999, 12, 31, 21, 0)
        assert classify_epoch_sample(epoch) == EpochSampleEvidence.INVALID


# ── classify_epoch_timestamps ───────────────────────────────────────────────
class TestClassifyEpochTimestamps:
    def test_unix_utc_consistent(self) -> None:
        verdict = classify_epoch_timestamps(
            [EPOCH_SUPPORTS_UNIX_UTC, EPOCH_AMBIGUOUS, EPOCH_OUT_OF_SESSION]
        )
        assert verdict.verdict == EpochVerdictKind.UNIX_UTC_CONSISTENT
        assert verdict.supports_unix_utc == 1
        assert verdict.supports_ist_naive == 0
        assert verdict.ambiguous == 1
        assert verdict.out_of_session == 1
        assert verdict.abstained == 0
        assert verdict.invalid == 0
        assert verdict.total == 3
        assert verdict.sampled == 3

    def test_ist_naive_consistent_is_detected(self) -> None:
        # The whole point of the verdict: the feared +05:30 mis-send yields a
        # DIRECTIONAL aggregate, not a silent pass.
        verdict = classify_epoch_timestamps([EPOCH_SUPPORTS_IST_NAIVE, EPOCH_AMBIGUOUS])
        assert verdict.verdict == EpochVerdictKind.IST_NAIVE_CONSISTENT
        assert verdict.supports_ist_naive == 1

    def test_contradictory(self) -> None:
        verdict = classify_epoch_timestamps([EPOCH_SUPPORTS_UNIX_UTC, EPOCH_SUPPORTS_IST_NAIVE])
        assert verdict.verdict == EpochVerdictKind.CONTRADICTORY

    def test_no_evidence_from_nondirectional_samples(self) -> None:
        verdict = classify_epoch_timestamps(
            [EPOCH_AMBIGUOUS, EPOCH_OUT_OF_SESSION, EPOCH_ABSTAINED, float("nan")]
        )
        assert verdict.verdict == EpochVerdictKind.NO_EVIDENCE
        assert verdict.ambiguous == 1
        assert verdict.out_of_session == 1
        assert verdict.abstained == 1
        assert verdict.invalid == 1

    def test_empty_samples_is_no_evidence(self) -> None:
        verdict = classify_epoch_timestamps([])
        assert verdict.verdict == EpochVerdictKind.NO_EVIDENCE
        assert verdict.total == 0
        assert verdict.sampled == 0

    def test_cap_classifies_first_n_in_input_order(self) -> None:
        # Deterministic first-N: with cap 2 the trailing ist-naive sample is
        # NOT classified, so the verdict stays unix_utc_consistent while
        # total/sampled report the truncation honestly.
        samples: list[object] = [
            EPOCH_SUPPORTS_UNIX_UTC,
            EPOCH_AMBIGUOUS,
            EPOCH_SUPPORTS_IST_NAIVE,
        ]
        verdict = classify_epoch_timestamps(samples, max_samples=2)
        assert verdict.verdict == EpochVerdictKind.UNIX_UTC_CONSISTENT
        assert verdict.total == 3
        assert verdict.sampled == 2
        assert verdict.supports_ist_naive == 0

    def test_default_cap_is_module_constant(self) -> None:
        samples: list[object] = [EPOCH_AMBIGUOUS] * (MAX_VERDICT_SAMPLES + 5)
        verdict = classify_epoch_timestamps(samples)
        assert verdict.total == MAX_VERDICT_SAMPLES + 5
        assert verdict.sampled == MAX_VERDICT_SAMPLES
        assert verdict.ambiguous == MAX_VERDICT_SAMPLES

    @pytest.mark.parametrize("bad_cap", [0, -1])
    def test_non_positive_cap_refused_loudly(self, bad_cap: int) -> None:
        with pytest.raises(ValueError, match="max_samples must be >= 1"):
            classify_epoch_timestamps([EPOCH_AMBIGUOUS], max_samples=bad_cap)

    def test_verdict_is_frozen(self) -> None:
        verdict = classify_epoch_timestamps([EPOCH_AMBIGUOUS])
        with pytest.raises(AttributeError):
            verdict.verdict = EpochVerdictKind.CONTRADICTORY  # type: ignore[misc]


# ── silent out-of-coverage abstain (round-1 review log-storm fold) ──────────


class TestOutOfCoverageSilentAbstain:
    """Out-of-coverage samples abstain with ZERO per-sample calendar WARNs.

    Pre-fix, ``_wall_in_session`` routed every out-of-coverage date through
    the strict ``get_session_checked``, whose ``is_holiday_checked`` emits one
    undeduplicated ``calendar.holiday_data_unavailable`` WARNING per call —
    2 checks/sample x 1024 samples/payload x every window of a pull: an
    all-epoch out-of-coverage payload (e.g. a 2027 pull after a vendor epoch
    flip — the exact scenario this module diagnoses) stormed up to 2,048
    WARNs PER WINDOW. The non-warning ``is_holiday_data_available`` pre-check
    abstains silently; the ONE aggregate ``epoch_timestamp_verdict`` WARN
    (the parser) stays the loud surface.
    """

    def test_out_of_coverage_payload_emits_zero_calendar_warns(self) -> None:
        # FAILS BEFORE the fix: 2 calendar WARNs per sample (128 here).
        samples: list[object] = [EPOCH_ABSTAINED] * 64
        with capture_logs() as logs:
            verdict = classify_epoch_timestamps(samples)
        calendar_warns = [e for e in logs if e["event"] == "calendar.holiday_data_unavailable"]
        assert calendar_warns == []
        assert verdict.verdict == EpochVerdictKind.NO_EVIDENCE
        assert verdict.abstained == 64

    def test_out_of_coverage_weekend_abstains_deliberately(self) -> None:
        # 2040-01-01 is a Sunday: its no-session verdict is data-complete, but
        # the sample abstains anyway (the documented conservative choice — an
        # out-of-coverage date can never read in-session, so abstaining loses
        # no directional evidence and keeps the pre-check WARN-free).
        epoch_2040_sunday = _epoch_utc(2040, 1, 1, 5, 0)
        assert classify_epoch_sample(epoch_2040_sunday) == EpochSampleEvidence.ABSTAINED

    def test_in_coverage_classification_unaffected_by_the_pre_check(self) -> None:
        # The covered-span paths are byte-identical: directional, ambiguous,
        # out-of-session and the covered SUNDAY (session is None) all classify
        # exactly as before, with zero calendar WARNs either.
        with capture_logs() as logs:
            assert (
                classify_epoch_sample(EPOCH_SUPPORTS_UNIX_UTC)
                == EpochSampleEvidence.SUPPORTS_UNIX_UTC
            )
            assert classify_epoch_sample(EPOCH_SUNDAY) == EpochSampleEvidence.OUT_OF_SESSION
        assert [e for e in logs if e["event"] == "calendar.holiday_data_unavailable"] == []
