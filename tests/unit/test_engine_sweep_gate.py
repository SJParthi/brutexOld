"""Tests for `brutex.engine.sweep_gate` (DEC-SWEEP-PREFLIGHT-ESTIMATOR-001, WP5).

100% line+branch over the composed G0-G7 runner: gather-all refuse-whole
folding (the pipeline ORDER is under test exactly ONCE, in
``TestPipelineOrder``), the probe-only law (a refusal writes NOTHING — tree
snapshots), the per-pair scan-defect fence (``MemoryError`` re-raises raw,
anything else folds to a named ``CORRUPT_PARTITION``), the fail-CLOSED disk
probe, the ONE-verdict SLA feed, the confirm-token minting rules, THE
renderer + payload, and the exactly-one-structlog-event contract.
"""

from __future__ import annotations

import fcntl
import os
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import polars as pl
import pytest
from brutex.data.interior_gap_detect import INTERIOR_GAP
from brutex.data.session import ALL_FILTERED
from brutex.data.session_coverage import LedgerRowFlag, QuarantineScan
from brutex.data.window_ledger import FAILED, SKIPPED, WindowLedger
from brutex.data.window_ledger_store import ledger_path, save_window_ledger
from brutex.engine.sweep_data_preflight import RefusalCode, clamp_dates
from brutex.engine.sweep_estimate import selection_confirm_token, selection_from_raw
from brutex.engine.sweep_gate import (
    GateRefusal,
    SweepPreflightOutcome,
    run_sweep_data_preflight,
)
from brutex.engine.sweep_sla_guard import SlaDecision
from structlog.testing import capture_logs

_EXCHANGE = "NSE"
_SEGMENT = "INDEX"
_SYMBOL = "NSE-NIFTY"
_TODAY = date(2025, 4, 1)
_START = date(2025, 3, 3)
_END = date(2025, 3, 7)
#: The canonical expected sessions of the test range (computed via the REAL
#: clamp — the fixtures seed exactly these dates, so the suite never encodes
#: a hand-guessed calendar).
_EXPECTED = clamp_dates(_START, _END, today=_TODAY).expected_sessions

_GATE_EVENTS = {"sweep.preflight.passed", "sweep.preflight.refused"}


def _write_days(lake: Path, interval: str, days: tuple[date, ...]) -> None:
    """Seed real month parquets holding one 09:30-IST bar per session date."""
    by_month: dict[tuple[int, int], list[datetime]] = {}
    for day in days:
        by_month.setdefault((day.year, day.month), []).append(
            datetime(day.year, day.month, day.day, 4, 0, tzinfo=UTC)
        )
    for (year, month), timestamps in by_month.items():
        directory = lake / "bars" / _EXCHANGE / _SEGMENT / _SYMBOL / interval / f"{year:04d}"
        directory.mkdir(parents=True, exist_ok=True)
        frame = pl.DataFrame(
            {"timestamp": timestamps}, schema={"timestamp": pl.Datetime("us", "UTC")}
        )
        frame.write_parquet(directory / f"{month:02d}.parquet")


def _run(lake: Path, out_root: Path, **overrides: Any) -> SweepPreflightOutcome:
    """Run the composed gate with the suite's small deterministic base selection."""
    kwargs: dict[str, Any] = {
        "lake_root": lake,
        "out_root": out_root,
        "exchange": _EXCHANGE,
        "segment": _SEGMENT,
        "symbols": [_SYMBOL],
        "intervals": ["1minute"],
        "directions": ["BUY"],
        "k_values": [1],
        "condition_bits": [0, 1, 2],
        "stop_losses": [None],
        "take_profits": [None],
        "fib_anchors": ["prev_day"],
        "top_k": 5,
        "start": _START,
        "end": _END,
        "today": _TODAY,
        "observed": frozenset(),
        "free_bytes": 10**12,
        "lake_token": None,
        "env": {},
    }
    kwargs.update(overrides)
    return run_sweep_data_preflight(**kwargs)


def _gate_events(logs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Filter the captured structlog events down to the gate's own verdict events."""
    return [entry for entry in logs if entry["event"] in _GATE_EVENTS]


def _tree_snapshot(root: Path) -> list[tuple[str, int, int]]:
    """Recursive ``(relpath, size, mtime_ns)`` snapshot — the nothing-written oracle."""
    out: list[tuple[str, int, int]] = []
    for path in sorted(root.rglob("*")):
        stat = path.stat()
        out.append((str(path.relative_to(root)), stat.st_size, stat.st_mtime_ns))
    return out


_NOW = "2026-07-01T00:00:00+00:00"


def _ledger_row(
    security_id: str,
    interval: str,
    window_start: str,
    window_end: str,
    status: str,
    last_error: str | None,
) -> tuple[
    str, str, str, str, str, str, int, int, str | None, str, str | None, int, int, int | None
]:
    """One raw window-ledger row tuple (the ``WindowLedger.from_rows`` shape)."""
    return (
        "groww",
        security_id,
        interval,
        window_start,
        window_end,
        status,
        0,
        1,
        last_error,
        _NOW,
        _NOW,
        0,
        1,
        None,
    )


def _seed_ledger(lake: Path, rows: list[tuple[Any, ...]]) -> None:
    """Persist a real window ledger via the real store writer."""
    save_window_ledger(WindowLedger.from_rows(rows), lake)


class TestHappyPath:
    def test_ok_outcome_every_gate_green(self, tmp_path: Path) -> None:
        """A fully-seeded lake passes every gate with zero refusals."""
        lake = tmp_path / "lake"
        _write_days(lake, "1minute", _EXPECTED)
        with capture_logs() as logs:
            outcome = _run(lake, tmp_path / "out")
        assert outcome.ok is True
        assert outcome.refusals == ()
        assert outcome.exclusion.ok is True
        assert outcome.clamp.ok is True
        assert outcome.coverage is not None
        assert outcome.coverage.ok is True
        assert outcome.quarantine is not None
        assert outcome.quarantine.ok is True
        assert outcome.sla.decision is SlaDecision.ALLOWED
        assert outcome.disk.ok is True
        assert outcome.confirm_token is None  # no lake_token supplied (CLI shape)
        assert outcome.refusal_text() == ""
        events = _gate_events(logs)
        assert len(events) == 1
        assert events[0]["event"] == "sweep.preflight.passed"
        assert events[0]["codes"] == []

    def test_confirm_token_minted_only_when_ok_and_lake_token_supplied(
        self, tmp_path: Path
    ) -> None:
        """The token binds the exact selection + clamped dates + launch + lake state."""
        lake = tmp_path / "lake"
        _write_days(lake, "1minute", _EXPECTED)
        outcome = _run(lake, tmp_path / "out", lake_token="rev-token-1")
        assert outcome.ok is True
        expected_sel = selection_from_raw(
            symbols=[_SYMBOL],
            intervals=["1minute"],
            directions=["BUY"],
            k_values=[1],
            stop_losses=[None],
            take_profits=[None],
            fib_anchors=["prev_day"],
            condition_bits=[0, 1, 2],
            top_k=5,
            start=outcome.clamp.start,
            end=outcome.clamp.end,
        )
        assert outcome.confirm_token == selection_confirm_token(
            expected_sel, launches=((_EXCHANGE, _SEGMENT),), lake_token="rev-token-1"
        )

    def test_confirm_token_none_on_refusal_even_with_lake_token(self, tmp_path: Path) -> None:
        """A refused verdict never mints a proceed token."""
        outcome = _run(tmp_path / "lake", tmp_path / "out", lake_token="rev-token-1")
        assert outcome.ok is False  # empty lake ⇒ coverage refusal
        assert outcome.confirm_token is None

    def test_sla_fed_the_same_displayed_time_model(self, tmp_path: Path) -> None:
        """ONE verdict, one figure — the SLA projection equals the displayed estimate."""
        lake = tmp_path / "lake"
        _write_days(lake, "1minute", _EXPECTED)
        outcome = _run(lake, tmp_path / "out")
        assert outcome.sla.tuple_count == outcome.estimate.tuples
        assert outcome.sla.projected_seconds == pytest.approx(outcome.estimate.est_seconds)

    def test_duplicate_request_tokens_scan_once(self, tmp_path: Path) -> None:
        """Deduped selection axes feed the scans — the grid-mirroring dedup law."""
        lake = tmp_path / "lake"
        _write_days(lake, "1minute", _EXPECTED)
        outcome = _run(lake, tmp_path / "out", symbols=[_SYMBOL, _SYMBOL])
        assert outcome.coverage is not None
        assert outcome.coverage.checked_pairs == 1

    def test_observed_none_loads_the_lake_overlay(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``observed=None`` consults ``load_observed_holidays`` (an overlaid day drops)."""
        lake = tmp_path / "lake"
        _write_days(lake, "1minute", _EXPECTED)
        monkeypatch.setattr(
            "brutex.engine.sweep_gate.load_observed_holidays",
            lambda _lake: frozenset({_EXPECTED[0]}),
        )
        outcome = _run(lake, tmp_path / "out", observed=None)
        assert outcome.ok is True  # stored extras are never-missing
        assert len(outcome.clamp.expected_sessions) == len(_EXPECTED) - 1
        assert _EXPECTED[0] not in outcome.clamp.expected_sessions

    def test_today_none_uses_the_real_ist_clock(self, tmp_path: Path) -> None:
        """``today=None`` resolves the injectable clock; a past range stays unclamped."""
        lake = tmp_path / "lake"
        _write_days(lake, "1minute", _EXPECTED)
        outcome = _run(lake, tmp_path / "out", today=None)
        assert outcome.clamp.start == _START
        assert outcome.clamp.end == _END
        assert outcome.ok is True


class TestExclusionGate:
    def test_held_pull_lock_refuses_probe_only(self, tmp_path: Path) -> None:
        """G0: a REAL held pull LOCK_EX refuses PULL_IN_FLIGHT and writes NOTHING."""
        lake = tmp_path / "lake"
        _write_days(lake, "1minute", _EXPECTED)
        lock_path = lake / ".locks" / "pull_singleflight.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            before = _tree_snapshot(tmp_path)
            outcome = _run(lake, tmp_path / "out")
            assert _tree_snapshot(tmp_path) == before  # probe-only: zero writes anywhere
        finally:
            os.close(fd)
        assert outcome.ok is False
        assert outcome.refusals[0].code is RefusalCode.PULL_IN_FLIGHT
        assert "a pull is running" in outcome.refusals[0].message
        assert "lock wins" in outcome.refusals[0].message

    def test_derive_active_refuses(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        lake = tmp_path / "lake"
        _write_days(lake, "1minute", _EXPECTED)
        monkeypatch.setattr("brutex.engine.sweep_gate.derive_in_progress", lambda _lake: True)
        outcome = _run(lake, tmp_path / "out")
        assert [r.code for r in outcome.refusals] == [RefusalCode.DERIVE_IN_FLIGHT]
        assert "a prebuild is running" in outcome.refusals[0].message

    def test_both_locks_share_one_message_rendered_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two codes off one gate share ONE complete message — never repeated text."""
        lake = tmp_path / "lake"
        _write_days(lake, "1minute", _EXPECTED)
        monkeypatch.setattr("brutex.engine.sweep_gate.pull_in_progress", lambda _lake: True)
        monkeypatch.setattr("brutex.engine.sweep_gate.derive_in_progress", lambda _lake: True)
        outcome = _run(lake, tmp_path / "out")
        assert [r.code for r in outcome.refusals] == [
            RefusalCode.PULL_IN_FLIGHT,
            RefusalCode.DERIVE_IN_FLIGHT,
        ]
        message = outcome.refusals[0].message
        assert outcome.refusals[1].message == message
        assert "a pull and a prebuild are running" in message
        text = outcome.refusal_text()
        assert text.count(message) == 1  # the renderer folds shared messages
        assert "[pull_in_flight, derive_in_flight]" in text


class TestClampGate:
    def test_reversed_range_hard_refusal_skips_coverage_and_quarantine(
        self, tmp_path: Path
    ) -> None:
        """G1 hard-refusal ⇒ coverage=None AND quarantine=None (no expected list)."""
        outcome = _run(
            tmp_path / "lake",
            tmp_path / "out",
            start=date(2025, 3, 10),
            end=date(2025, 3, 3),
        )
        assert outcome.ok is False
        assert [r.code for r in outcome.refusals] == [RefusalCode.REVERSED_RANGE]
        assert outcome.coverage is None
        assert outcome.quarantine is None
        # The estimate is STILL computed — the operator sees the refused cost.
        assert outcome.estimate.trials == 3  # C(3,1) × 1 axis point

    def test_zero_session_weekend_range_refuses(self, tmp_path: Path) -> None:
        outcome = _run(
            tmp_path / "lake",
            tmp_path / "out",
            start=date(2025, 3, 8),  # Saturday
            end=date(2025, 3, 9),  # Sunday
        )
        assert [r.code for r in outcome.refusals] == [RefusalCode.ZERO_SESSIONS]
        assert "0 expected sessions" in outcome.refusal_text()


class TestCoverageGate:
    def test_missing_sessions_refuse_whole_with_exact_span(self, tmp_path: Path) -> None:
        lake = tmp_path / "lake"
        hole = _EXPECTED[1]
        _write_days(lake, "1minute", tuple(d for d in _EXPECTED if d != hole))
        outcome = _run(lake, tmp_path / "out")
        assert outcome.ok is False
        assert [r.code for r in outcome.refusals] == [RefusalCode.COVERAGE_MISSING]
        message = outcome.refusals[0].message
        assert f"{hole.isoformat()} (1 session)" in message
        assert "pull on /universe first" in message

    def test_empty_lake_absent_scan_key_is_fail_closed_not_ready(self, tmp_path: Path) -> None:
        outcome = _run(tmp_path / "lake", tmp_path / "out")
        assert outcome.ok is False
        assert outcome.coverage is not None
        assert outcome.coverage.not_ready[0].reason is RefusalCode.COVERAGE_MISSING

    def test_derived_tf_never_materialized_refuses(self, tmp_path: Path) -> None:
        lake = tmp_path / "lake"
        _write_days(lake, "1minute", _EXPECTED)
        outcome = _run(lake, tmp_path / "out", intervals=["1minute", "5minute"])
        assert [r.code for r in outcome.refusals] == [RefusalCode.DERIVED_TF_MISSING]
        assert "never materialized" in outcome.refusals[0].message

    def test_corrupt_month_gathers_both_codes_named_path(self, tmp_path: Path) -> None:
        """A garbage month file → CORRUPT_PARTITION naming the path, gathered with
        the coverage-missing verdict (R38 — a named refusal, never a traceback)."""
        lake = tmp_path / "lake"
        day = _EXPECTED[0]
        directory = lake / "bars" / _EXCHANGE / _SEGMENT / _SYMBOL / "1minute" / f"{day.year:04d}"
        directory.mkdir(parents=True)
        corrupt = directory / f"{day.month:02d}.parquet"
        corrupt.write_bytes(b"garbage, not parquet")
        outcome = _run(lake, tmp_path / "out")
        codes = [r.code for r in outcome.refusals]
        assert codes == [RefusalCode.COVERAGE_MISSING, RefusalCode.CORRUPT_PARTITION]
        text = outcome.refusal_text()
        assert str(corrupt) in text
        assert "[coverage_missing, corrupt_partition]" in text  # shared gate message, one line

    def test_scan_defect_folds_to_named_corrupt_partition(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-MemoryError per-pair scan defect folds — named, fail-closed."""
        lake = tmp_path / "lake"
        _write_days(lake, "1minute", _EXPECTED)

        def _boom(**_kwargs: Any) -> dict[str, Any]:
            raise ValueError("polars exploded")

        monkeypatch.setattr("brutex.engine.sweep_gate.scan_selection_coverage", _boom)
        outcome = _run(lake, tmp_path / "out")
        assert outcome.ok is False
        codes = [r.code for r in outcome.refusals]
        # The absent scan entry ALSO folds the pair not-ready (fail-closed).
        assert codes == [RefusalCode.COVERAGE_MISSING, RefusalCode.CORRUPT_PARTITION]
        defect = outcome.refusals[1].message
        assert f"coverage scan defect for {_SYMBOL} × 1minute" in defect
        assert "ValueError: polars exploded" in defect

    def test_memory_error_during_scan_reraises_raw(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lake = tmp_path / "lake"
        _write_days(lake, "1minute", _EXPECTED)

        def _oom(**_kwargs: Any) -> dict[str, Any]:
            raise MemoryError

        monkeypatch.setattr("brutex.engine.sweep_gate.scan_selection_coverage", _oom)
        with pytest.raises(MemoryError):
            _run(lake, tmp_path / "out")


class TestQuarantineGate:
    def test_demoted_open_window_refuses(self, tmp_path: Path) -> None:
        lake = tmp_path / "lake"
        _write_days(lake, "1minute", _EXPECTED)
        _seed_ledger(
            lake,
            [_ledger_row(_SYMBOL, "1minute", "2025-03-01", "2025-03-31", FAILED, INTERIOR_GAP)],
        )
        outcome = _run(lake, tmp_path / "out")
        assert [r.code for r in outcome.refusals] == [RefusalCode.QUARANTINED_RANGE]
        message = outcome.refusals[0].message
        assert "OPEN demoted window(s)" in message
        assert f"{_SYMBOL} × 1minute [2025-03-01 → 2025-03-31: {INTERIOR_GAP}]" in message
        assert "pull on /universe first" in message

    def test_accepted_gap_window_refuses_with_reopen_remedy(self, tmp_path: Path) -> None:
        lake = tmp_path / "lake"
        _write_days(lake, "1minute", _EXPECTED)
        _seed_ledger(
            lake,
            [_ledger_row(_SYMBOL, "1minute", "2025-03-01", "2025-03-31", SKIPPED, ALL_FILTERED)],
        )
        outcome = _run(lake, tmp_path / "out")
        assert [r.code for r in outcome.refusals] == [RefusalCode.QUARANTINED_RANGE]
        message = outcome.refusals[0].message
        assert "accepted-gap window(s)" in message
        assert "KNOWN-incomplete" in message
        assert "brutex reopen-skipped" in message

    def test_unreadable_ledger_fail_closed(self, tmp_path: Path) -> None:
        lake = tmp_path / "lake"
        _write_days(lake, "1minute", _EXPECTED)
        path = ledger_path(lake)
        path.parent.mkdir(parents=True)
        path.write_bytes(b"garbage, not a parquet ledger")
        outcome = _run(lake, tmp_path / "out")
        assert [r.code for r in outcome.refusals] == [RefusalCode.LEDGER_UNREADABLE]
        assert "UNREADABLE" in outcome.refusals[0].message
        assert "fail-CLOSED" in outcome.refusals[0].message

    def test_load_failed_and_flags_gather_both_codes_and_cap_fires(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Both G3 codes gather; a 14-window class caps at 12 with the TRUE total."""
        lake = tmp_path / "lake"
        _write_days(lake, "1minute", _EXPECTED)
        flags = tuple(
            LedgerRowFlag(
                security_id=_SYMBOL,
                interval="1minute",
                window_start=f"2025-03-{day:02d}",
                window_end=f"2025-03-{day + 1:02d}",
                status=FAILED,
                last_error=INTERIOR_GAP,
            )
            for day in range(1, 15)
        )
        monkeypatch.setattr(
            "brutex.engine.sweep_gate.scan_ledger_quarantine",
            lambda *_args, **_kwargs: QuarantineScan(flags=flags, load_failed=True),
        )
        outcome = _run(lake, tmp_path / "out")
        assert [r.code for r in outcome.refusals] == [
            RefusalCode.LEDGER_UNREADABLE,
            RefusalCode.QUARANTINED_RANGE,
        ]
        message = outcome.refusals[0].message
        assert outcome.refusals[1].message == message  # one gate, one complete message
        assert "(+2 more; 14 windows in total)" in message
        assert "[2025-03-12 → 2025-03-13" in message  # the 12th named window
        assert "[2025-03-13 → 2025-03-14" not in message  # the 13th is capped away


class TestSlaGate:
    def test_overrun_refuses_with_the_relocated_verdict(self, tmp_path: Path) -> None:
        lake = tmp_path / "lake"
        _write_days(lake, "1minute", _EXPECTED)
        outcome = _run(lake, tmp_path / "out", env={"BRUTEX_SWEEP_PER_TUPLE_SECONDS": "400000"})
        assert outcome.sla.decision is SlaDecision.REFUSED
        assert [r.code for r in outcome.refusals] == [RefusalCode.SLA_OVERRUN]
        assert "REFUSED: projected wall-clock" in outcome.refusals[0].message
        # ONE figure: the refusal quotes the SAME hours the estimate displays.
        assert outcome.sla.projected_seconds == pytest.approx(outcome.estimate.est_seconds)

    def test_acknowledged_overrun_is_a_note_never_a_refusal(self, tmp_path: Path) -> None:
        lake = tmp_path / "lake"
        _write_days(lake, "1minute", _EXPECTED)
        outcome = _run(
            lake,
            tmp_path / "out",
            env={"BRUTEX_SWEEP_PER_TUPLE_SECONDS": "400000"},
            acknowledge_sla=True,
        )
        assert outcome.ok is True
        assert outcome.sla.decision is SlaDecision.ACKNOWLEDGED
        assert any("SLA OVERRUN ACKNOWLEDGED" in note for note in outcome.notes)


class TestDiskGate:
    def test_insufficient_injected_free_bytes_refuses(self, tmp_path: Path) -> None:
        lake = tmp_path / "lake"
        _write_days(lake, "1minute", _EXPECTED)
        outcome = _run(lake, tmp_path / "out", free_bytes=0)
        assert [r.code for r in outcome.refusals] == [RefusalCode.DISK_SPACE]
        message = outcome.refusals[0].message
        assert "GiB" in message
        assert str(tmp_path / "out") in message
        assert "--results-root" in message

    def test_free_bytes_none_probes_the_real_volume(self, tmp_path: Path) -> None:
        lake = tmp_path / "lake"
        _write_days(lake, "1minute", _EXPECTED)
        outcome = _run(lake, tmp_path / "out", free_bytes=None)
        assert outcome.disk.free_bytes is not None
        assert outcome.disk.free_bytes > 0
        assert outcome.disk.probe_error is None

    def test_probe_oserror_maps_to_fail_closed_refusal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lake = tmp_path / "lake"
        _write_days(lake, "1minute", _EXPECTED)

        def _boom(_root: Path) -> int:
            raise OSError("statvfs failed")

        monkeypatch.setattr("brutex.engine.sweep_gate.volume_free_bytes", _boom)
        outcome = _run(lake, tmp_path / "out", free_bytes=None)
        assert [r.code for r in outcome.refusals] == [RefusalCode.DISK_SPACE]
        assert outcome.disk.blocked is True
        assert outcome.disk.free_bytes is None
        assert outcome.disk.probe_error is not None
        assert "OSError: statvfs failed" in outcome.disk.probe_error
        assert any("probe failed" in note for note in outcome.notes)

    def test_probe_memory_error_reraises_raw(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lake = tmp_path / "lake"
        _write_days(lake, "1minute", _EXPECTED)

        def _oom(_root: Path) -> int:
            raise MemoryError

        monkeypatch.setattr("brutex.engine.sweep_gate.volume_free_bytes", _oom)
        with pytest.raises(MemoryError):
            _run(lake, tmp_path / "out", free_bytes=None)


class TestCalibrationNotes:
    def test_absent_store_note(self, tmp_path: Path) -> None:
        lake = tmp_path / "lake"
        _write_days(lake, "1minute", _EXPECTED)
        outcome = _run(lake, tmp_path / "out")
        assert any(
            "calibration store: absent" in note and "PROVISIONAL" in note for note in outcome.notes
        )
        assert outcome.estimate.provisional is True

    def test_calibrated_store_note_and_non_provisional_estimate(self, tmp_path: Path) -> None:
        from brutex.engine.sweep_calibration import (
            RunActuals,
            hardware_fingerprint,
            record_run_calibration,
        )

        lake = tmp_path / "lake"
        out_root = tmp_path / "out"
        _write_days(lake, "1minute", _EXPECTED)
        baseline = _run(lake, out_root)  # estimate to feed the recorder
        record_run_calibration(
            out_root,
            estimate=baseline.estimate,
            actuals=RunActuals(
                run_identity="run-1",
                wall_seconds=120.0,
                trials_enumerated=200_000,
                tuples_run=1,
                rows_persisted=10,
                result_bytes=10_240,
            ),
            fingerprint=hardware_fingerprint(),
        )
        outcome = _run(lake, out_root)
        assert outcome.estimate.provisional is False
        assert any(
            "hardware-matched" in note and "1 ring entries (1 qualifying)" in note
            for note in outcome.notes
        )

    def test_present_but_uncalibrated_store_note(self, tmp_path: Path) -> None:
        from brutex.engine.sweep_calibration import (
            RunActuals,
            hardware_fingerprint,
            record_run_calibration,
        )

        lake = tmp_path / "lake"
        out_root = tmp_path / "out"
        _write_days(lake, "1minute", _EXPECTED)
        baseline = _run(lake, out_root)
        record_run_calibration(  # a sub-threshold toy run — records, never flips
            out_root,
            estimate=baseline.estimate,
            actuals=RunActuals(
                run_identity="toy",
                wall_seconds=2.0,
                trials_enumerated=10,
                tuples_run=1,
                rows_persisted=1,
                result_bytes=100,
            ),
            fingerprint=hardware_fingerprint(),
        )
        outcome = _run(lake, out_root)
        assert outcome.estimate.provisional is True
        assert any("present but NOT calibrated" in note for note in outcome.notes)


class TestPipelineOrder:
    def test_gather_all_refusals_in_pipeline_order_exactly_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """THE pipeline-order pin (under test exactly ONCE, BUILD-SPEC §2.6):
        every gate refuses simultaneously and the gathered codes come out in
        G0 → G1(skipped: clamp ok here) → G2 → G3 → G5 → G6 order, in ONE
        text, with exactly ONE ``sweep.preflight.refused`` event."""
        lake = tmp_path / "lake"
        hole = _EXPECTED[2]
        _write_days(lake, "1minute", tuple(d for d in _EXPECTED if d != hole))
        _seed_ledger(
            lake,
            [_ledger_row(_SYMBOL, "1minute", "2025-03-01", "2025-03-31", FAILED, INTERIOR_GAP)],
        )
        monkeypatch.setattr("brutex.engine.sweep_gate.pull_in_progress", lambda _lake: True)
        with capture_logs() as logs:
            outcome = _run(
                lake,
                tmp_path / "out",
                env={"BRUTEX_SWEEP_PER_TUPLE_SECONDS": "400000"},
                free_bytes=0,
                lake_token="rev",
            )
        assert [r.code for r in outcome.refusals] == [
            RefusalCode.PULL_IN_FLIGHT,
            RefusalCode.COVERAGE_MISSING,
            RefusalCode.QUARANTINED_RANGE,
            RefusalCode.SLA_OVERRUN,
            RefusalCode.DISK_SPACE,
        ]
        assert outcome.ok is False
        assert outcome.confirm_token is None
        text = outcome.refusal_text()
        for refusal in outcome.refusals:  # refuse-whole: EVERYTHING in ONE text
            assert refusal.message in text
        events = _gate_events(logs)
        assert len(events) == 1
        assert events[0]["event"] == "sweep.preflight.refused"
        assert events[0]["codes"] == [
            "pull_in_flight",
            "coverage_missing",
            "quarantined_range",
            "sla_overrun",
            "disk_space",
        ]


class TestRenderer:
    def test_refusal_text_shape_and_remedy_tail(self, tmp_path: Path) -> None:
        outcome = _run(tmp_path / "lake", tmp_path / "out")  # empty lake ⇒ refused
        text = outcome.refusal_text()
        assert text != ""
        assert text.startswith("sweep preflight REFUSED (1 refusal(s); codes: coverage_missing)")
        assert "- [coverage_missing] " in text
        assert text.endswith(
            "fix every cause above, then re-run the sweep — this refusal wrote NOTHING "
            "(probe-only preflight; the sweep NEVER pulls, R3)."
        )

    def test_coverage_block_appears_verbatim_in_the_renderer(self, tmp_path: Path) -> None:
        """CLI stderr == payload text == the pure fold's own refusal block."""
        outcome = _run(tmp_path / "lake", tmp_path / "out")
        assert outcome.coverage is not None
        block = outcome.coverage.refusal()
        assert block in outcome.refusal_text()
        payload = outcome.payload()
        assert payload["refusal_text"] == outcome.refusal_text()

    def test_gate_refusal_is_frozen(self) -> None:
        refusal = GateRefusal(code=RefusalCode.DISK_SPACE, message="x")
        with pytest.raises(AttributeError):
            refusal.message = "y"  # type: ignore[misc]


class TestPayload:
    def test_ok_payload_shape(self, tmp_path: Path) -> None:
        lake = tmp_path / "lake"
        _write_days(lake, "1minute", _EXPECTED)
        outcome = _run(lake, tmp_path / "out", lake_token="rev")
        payload = outcome.payload()
        assert payload["schema_version"] == 1
        assert payload["ok"] is True
        assert payload["refusals"] == []
        assert payload["refusal_text"] == ""
        assert payload["confirm_token"] == outcome.confirm_token
        coverage = payload["coverage"]
        assert isinstance(coverage, dict)
        assert coverage["ok"] is True
        assert coverage["covered_sessions"] == [
            {"symbol": _SYMBOL, "interval": "1minute", "covered": len(_EXPECTED)}
        ]
        quarantine = payload["quarantine"]
        assert isinstance(quarantine, dict)
        assert quarantine == {
            "ok": True,
            "ledger_load_failed": False,
            "demoted_open": [],
            "accepted_gaps": [],
        }

    def test_hard_refusal_payload_carries_none_sections(self, tmp_path: Path) -> None:
        outcome = _run(
            tmp_path / "lake", tmp_path / "out", start=date(2025, 3, 10), end=date(2025, 3, 3)
        )
        payload = outcome.payload()
        assert payload["coverage"] is None
        assert payload["quarantine"] is None
        refusals = payload["refusals"]
        assert isinstance(refusals, list)
        assert refusals[0]["code"] == "reversed_range"

    def test_refused_payload_spans_and_flags_serialize(self, tmp_path: Path) -> None:
        lake = tmp_path / "lake"
        hole = _EXPECTED[1]
        _write_days(lake, "1minute", tuple(d for d in _EXPECTED if d != hole))
        _seed_ledger(
            lake,
            [_ledger_row(_SYMBOL, "1minute", "2025-03-01", "2025-03-31", SKIPPED, ALL_FILTERED)],
        )
        payload = _run(lake, tmp_path / "out").payload()
        coverage = payload["coverage"]
        assert isinstance(coverage, dict)
        verdict = coverage["not_ready"][0]
        assert verdict["reason"] == "coverage_missing"
        assert verdict["spans"] == [
            {"start": hole.isoformat(), "end": hole.isoformat(), "sessions": 1}
        ]
        assert verdict["text"].startswith(f"{_SYMBOL} × 1minute")
        quarantine = payload["quarantine"]
        assert isinstance(quarantine, dict)
        assert quarantine["accepted_gaps"] == [
            {
                "security_id": _SYMBOL,
                "interval": "1minute",
                "window_start": "2025-03-01",
                "window_end": "2025-03-31",
                "status": SKIPPED,
                "last_error": ALL_FILTERED,
            }
        ]
