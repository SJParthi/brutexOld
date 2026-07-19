"""Hypothesis differential — the REAL ``brutex_ops`` wheel vs the Python oracles.

DEC-BACKUP-RUST-OPS-001. Drives the routed public functions FLAG-ON against
the retained Python oracles over hostile inputs (the outcome — value or
raised :class:`BackupError` type+message — must be identical; the seam's
abstain arms make raise parity structural). Skips wholesale when the wheel
is not installed (the maturin build is a local/dev step — the crate is
OPTIONAL, the flag default-OFF).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from brutex.backup import backup_rust, deadman, restic_runner
from brutex.backup.errors import BackupError
from brutex.backup.state import BackupState, DeadmanState, empty_backup_state
from hypothesis import given, settings
from hypothesis import strategies as st

pytest.importorskip("brutex_ops", reason="brutex_ops wheel not installed")


@pytest.fixture(autouse=True)
def _fresh_seam_on(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv(backup_rust.BACKUP_RUST_ENV, "1")
    # Steady state: the crate DECIDES with no oracle cushion in the path —
    # the differential compares the served outcome against the oracle itself.
    backup_rust.reset_backup_rust_for_tests(remaining=0)
    yield
    backup_rust.reset_backup_rust_for_tests()


NOW = datetime(2026, 7, 18, 12, 0, 0, tzinfo=UTC)


def _stamp(offset_s: int | None) -> datetime | None:
    if offset_s is None:
        return None
    return NOW + timedelta(seconds=offset_s)


_OFFSETS = st.one_of(st.none(), st.integers(min_value=-(10**9), max_value=10**9))


@st.composite
def _states(draw: st.DrawFn) -> BackupState | None:
    if draw(st.booleans()):
        return None
    base = empty_backup_state()
    return BackupState(
        schema=base.schema,
        running_since=_stamp(draw(_OFFSETS)),
        last_attempt_at=None,
        last_success_at=_stamp(draw(_OFFSETS)),
        last_snapshot_id=None,
        last_failure_at=None,
        last_failure_reason=None,
        consecutive_failures=0,
        consecutive_partials=0,
        maintenance_ok=True,
        verify_subset_cursor=0,
        lake_device=None,
    )


@settings(max_examples=200, deadline=None)
@given(
    state=_states(),
    alert_offset=_OFFSETS,
    state_error=st.one_of(st.none(), st.text(max_size=20)),
    threshold_hours=st.floats(min_value=0.1, max_value=1000.0, allow_nan=False),
    realert_hours=st.floats(min_value=0.1, max_value=1000.0, allow_nan=False),
    skew_tolerance_s=st.floats(min_value=0.0, max_value=100_000.0, allow_nan=False),
    max_runtime_hours=st.floats(min_value=0.1, max_value=1000.0, allow_nan=False),
)
def test_deadman_verdict_differential(
    state: BackupState | None,
    alert_offset: int | None,
    state_error: str | None,
    threshold_hours: float,
    realert_hours: float,
    skew_tolerance_s: float,
    max_runtime_hours: float,
) -> None:
    sidecar = DeadmanState(last_overdue_alert_at=_stamp(alert_offset))
    kwargs = {
        "threshold_hours": threshold_hours,
        "realert_hours": realert_hours,
        "skew_tolerance_s": skew_tolerance_s,
        "max_runtime_hours": max_runtime_hours,
    }
    routed = deadman.evaluate_deadman(
        state, sidecar, clock=lambda: NOW, state_error=state_error, **kwargs
    )
    oracle = deadman._evaluate_deadman_python(
        state, sidecar, clock=lambda: NOW, state_error=state_error, **kwargs
    )
    assert routed == oracle
    assert deadman._deadman_outcome_key(("ok", routed)) == deadman._deadman_outcome_key(
        ("ok", oracle)
    )
    assert backup_rust.disabled_reason() is None


#: Hostile stdout material: JSON-ish lines, CPython-only dialect (NaN /
#: Infinity), control chars, non-JSON garbage, real restic shapes.
_LINES = st.one_of(
    st.just('{"message_type":"summary","snapshot_id":"abc"}'),
    st.just('{"message_type":"summary","snapshot_id":""}'),
    st.just('{"message_type":"summary"}'),
    st.just('{"type":"file","path":"/a/b"}'),
    st.just('{"type":"dir","path":"/d"}'),
    st.just('{"time":"2026-01-01T00:00:00Z"}'),
    st.just('{"time":"2026-01-01T00:00:00"}'),
    st.just('{"time":42}'),
    st.just('{"type":null,"time":"t"}'),
    st.just("[1, 2]"),
    st.just('"bare string"'),
    st.just("NaN"),
    st.just('{"x": NaN}'),
    st.just("Infinity garbage"),
    st.just("\x1c{}"),
    st.just(""),
    st.just("   "),
    st.text(max_size=24),
)

_STDOUTS = st.lists(_LINES, max_size=8).map("\n".join)


def _outcome(func: object, stdout: str) -> object:
    try:
        return ("ok", func(stdout))  # type: ignore[operator]
    except BackupError as exc:
        return ("raise", type(exc).__name__, str(exc))


@settings(max_examples=200, deadline=None)
@given(stdout=_STDOUTS)
def test_json_lines_parsers_differential(stdout: str) -> None:
    pairs = [
        (
            restic_runner.parse_backup_summary_snapshot_id,
            restic_runner._parse_backup_summary_snapshot_id_python,
        ),
        (restic_runner.parse_ls_file_paths, restic_runner._parse_ls_file_paths_python),
        (restic_runner.parse_ls_snapshot_time, restic_runner._parse_ls_snapshot_time_python),
    ]
    for routed, oracle in pairs:
        assert _outcome(routed, stdout) == _outcome(oracle, stdout)
    assert backup_rust.disabled_reason() is None


_SNAPSHOT_DOCS = st.one_of(
    _STDOUTS,
    st.just("[]"),
    st.just('[{"short_id":"aaa","id":"a-full","time":"2026-01-01T00:00:00Z"}]'),
    st.just('[{"short_id":"","id":"full","time":"2026-01-01T00:00:00Z"}]'),
    st.just('[{"short_id":0,"id":"full","time":"2026-01-01T00:00:00+05:30"}]'),
    st.just('[{"id":"x","time":42}]'),
    st.just('[{"id":"x","time":"naive"}]'),
    st.just('[{"id":"x"}]'),
    st.just("[1]"),
    st.just('{"a":1}'),
    st.just("99999999999999999999999999"),
)


@settings(max_examples=200, deadline=None)
@given(stdout=_SNAPSHOT_DOCS)
def test_snapshots_latest_differential(stdout: str) -> None:
    assert _outcome(restic_runner.parse_snapshots_latest, stdout) == _outcome(
        restic_runner._parse_snapshots_latest_python, stdout
    )
    assert backup_rust.disabled_reason() is None


@settings(max_examples=100, deadline=None)
@given(
    binary=st.text(min_size=1, max_size=12),
    paths=st.lists(st.text(min_size=1, max_size=16), max_size=4),
    excludes=st.lists(st.text(max_size=12), max_size=4),
    tags=st.lists(st.text(max_size=8), max_size=3),
)
def test_build_backup_argv_differential(
    binary: str, paths: list[str], excludes: list[str], tags: list[str]
) -> None:
    from pathlib import Path

    path_objs = [Path(p) for p in paths]
    routed = restic_runner.build_backup_argv(binary, path_objs, excludes=excludes, tags=tags)
    oracle = restic_runner._build_backup_argv_python(
        binary, path_objs, excludes=excludes, tags=tags
    )
    assert routed == oracle
    assert backup_rust.disabled_reason() is None
