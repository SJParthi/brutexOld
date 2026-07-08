"""Hypothesis properties for :func:`brutex.backup.deadman.evaluate_deadman`.

DEC-BACKUP-OFFBOX-001 §14 (the 7-type property slot #2): the verdict function
is TOTAL over arbitrary stamps/thresholds (never raises, always lands in the
enum), and FRESH holds exactly when the success stamp is bounded, un-skewed
and no run marker is set — with ``should_alert`` never True under
FRESH/RUNNING (the no-false-page invariant).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from brutex.backup.deadman import ALARM_VERDICTS, DeadmanVerdict, evaluate_deadman
from brutex.backup.state import DeadmanState, empty_backup_state
from hypothesis import given
from hypothesis import strategies as st

pytestmark = pytest.mark.property

_SECONDS_PER_HOUR = 3600.0

_aware_dt = st.datetimes(
    min_value=datetime(2000, 1, 1),
    max_value=datetime(2100, 1, 1),
    timezones=st.just(UTC),
)
_opt_dt = st.none() | _aware_dt
# Finite-only strategies are the PRODUCTION domain, not a convenience gap:
# since round 8 the ONE config entry point (resolve_backup_env._float_env)
# rejects non-finite values (and non-positive hour/deadline knobs) with the
# loud malformed-numeric fallback, so evaluate_deadman can never receive a
# nan/inf threshold in production — pinned by the resolver's own totality
# property (tests/unit/test_backup_env.py::TestResolverTotality).
_hours = st.floats(min_value=0.0, max_value=10_000.0, allow_nan=False, allow_infinity=False)
_skew = st.floats(min_value=0.0, max_value=86_400.0, allow_nan=False, allow_infinity=False)
_runtime = st.floats(min_value=0.001, max_value=10_000.0, allow_nan=False, allow_infinity=False)


@given(
    success=_opt_dt,
    running=_opt_dt,
    alerted=_opt_dt,
    now=_aware_dt,
    threshold=_hours,
    realert=_hours,
    skew=_skew,
    max_runtime=_runtime,
    state_missing=st.booleans(),
    state_error=st.none() | st.just("corrupt"),
)
def test_verdict_total_function(
    success: datetime | None,
    running: datetime | None,
    alerted: datetime | None,
    now: datetime,
    threshold: float,
    realert: float,
    skew: float,
    max_runtime: float,
    state_missing: bool,
    state_error: str | None,
) -> None:
    """Never raises; the verdict is always a DeadmanVerdict; alerts only on alarms."""
    state = (
        None
        if state_missing
        else replace(empty_backup_state(), last_success_at=success, running_since=running)
    )
    report = evaluate_deadman(
        state,
        DeadmanState(last_overdue_alert_at=alerted),
        threshold_hours=threshold,
        realert_hours=realert,
        skew_tolerance_s=skew,
        max_runtime_hours=max_runtime,
        clock=lambda: now,
        state_error=state_error,
    )
    assert isinstance(report.verdict, DeadmanVerdict)
    assert report.threshold_hours == threshold
    if report.should_alert:
        assert report.verdict in ALARM_VERDICTS
    if report.verdict not in ALARM_VERDICTS:
        assert report.should_alert is False


@given(
    success=_opt_dt,
    running=_opt_dt,
    now=_aware_dt,
    threshold=_hours,
    skew=_skew,
    max_runtime=_runtime,
)
def test_fresh_iff_bounded_and_no_skew_and_not_running(
    success: datetime | None,
    running: datetime | None,
    now: datetime,
    threshold: float,
    skew: float,
    max_runtime: float,
) -> None:
    """FRESH ⟺ no run marker ∧ a success stamp ∧ future-skew ≤ tolerance ∧
    elapsed ≤ threshold; and FRESH/RUNNING never set should_alert."""
    state = replace(empty_backup_state(), last_success_at=success, running_since=running)
    report = evaluate_deadman(
        state,
        DeadmanState(last_overdue_alert_at=now - timedelta(days=365)),  # window long past
        threshold_hours=threshold,
        realert_hours=0.0,
        skew_tolerance_s=skew,
        max_runtime_hours=max_runtime,
        clock=lambda: now,
        state_error=None,
    )
    expected_fresh = (
        running is None
        and success is not None
        and (success - now).total_seconds() <= skew
        and max(0.0, (now - success).total_seconds() / _SECONDS_PER_HOUR) <= threshold
    )
    assert (report.verdict is DeadmanVerdict.FRESH) == expected_fresh
    if report.verdict in (DeadmanVerdict.FRESH, DeadmanVerdict.RUNNING):
        assert report.should_alert is False
