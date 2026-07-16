# pyright: reportPrivateUsage=none
"""The staged sequencer seams on ``run_full_pipeline`` (DEC-STAGED-SEQUENCER-SEAMS-001).

WS-B of the dual-vendor orchestration track, per the frozen ADR-0021 design
contract (Decisions 2/3, §10-§12): the ADDITIVE staged callables, the
ABSENT-vs-FAILED vacuously-settled contract (incl. the REQUIRED
second-vendor-SHAPED PARTIAL-absent parity case), per-stage fail-closed
gates with sequencer-minted ``stopped_after`` tokens, the per-vendor-stage
market-hours belt, the ``pipeline.stage.*`` events, the supersession-4
movers-warm law, the fail-loud legacy-XOR-staged junk-mix refusal, and the
PINNED all-``None``-defaults byte-identity of the legacy chain (report dict
FROZEN — verified equal to ``origin/main``'s output by the A/B evidence run
recorded in the PR). Fully offline; every concrete report is the REAL
dataclass (Golden Rule #1).
"""

from __future__ import annotations

from collections.abc import MutableMapping, Sequence
from dataclasses import dataclass

import pytest
from brutex.dashboard.movers_warm import MoversWarmResult
from brutex.dashboard.pipeline_pull import (
    PHASE_MARKET_HOURS,
    PipelinePullReport,
    run_full_pipeline,
)
from brutex.dashboard.stage_outcome import (
    STAGE_STATUS_ABSENT,
    STAGE_STATUS_FAILED,
    STAGE_STATUS_SETTLED,
    STAGE_STATUS_SKIPPED,
)
from brutex.data.errors import LakeWriteHalt, MarketHoursHalt
from structlog.testing import capture_logs

# The REAL-dataclass report factories (the established cross-test import
# pattern, e.g. tests/golden/test_bench_baseline_golden.py).
from tests.unit.test_pipeline_pull import _composite, _historical_inner, _metadata, _spot

type _LogEvent = MutableMapping[str, object]


def _events(logs: Sequence[_LogEvent], name: str) -> list[_LogEvent]:
    """All captured events with ``event == name`` (in emission order)."""
    return [e for e in logs if e["event"] == name]


@dataclass(frozen=True, slots=True)
class _Outcome:
    """A minimal protocol-shaped stage outcome (a second-vendor stand-in)."""

    ok: bool | None = True


class _Stage:
    """A recording stage callable returning a fixed outcome (or raising)."""

    def __init__(self, outcome: _Outcome | None = None, raises: Exception | None = None) -> None:
        self.outcome = outcome if outcome is not None else _Outcome()
        self.raises = raises
        self.calls = 0

    def __call__(self) -> _Outcome:
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return self.outcome


def _warm(warmed: int = 2, failed: int = 0) -> MoversWarmResult:
    return MoversWarmResult(warmed=warmed, failed=failed, total=warmed + failed)


# --------------------------------------------------------------------------- #
# The second-vendor-SHAPED PARTIAL-absent parity case (ADR-0021 Decision 3 /
# CAPA-02 — the REQUIRED test the all-None parity does NOT cover)
# --------------------------------------------------------------------------- #


class TestPartialAbsentParity:
    """spot_daily+spot+spot_prebuild+options+greeks bound; metadata / futures /
    futures_prebuild ABSENT — the second-vendor graph shape."""

    def _run(self, *, strict_gate: bool = False) -> tuple[PipelinePullReport, dict[str, _Stage]]:
        stages = {
            "spot_daily": _Stage(),
            "spot": _Stage(),
            "spot_prebuild": _Stage(),
            "options": _Stage(),
            "greeks": _Stage(_Outcome(ok=None)),  # the ran-but-nothing-to-do verdict
        }
        report = run_full_pipeline(
            run_spot=stages["spot"],
            run_spot_daily=stages["spot_daily"],
            run_prebuild_spot=stages["spot_prebuild"],
            run_options=stages["options"],
            run_greeks=stages["greeks"],
            strict_gate=strict_gate,
            vendor="vendor2",
        )
        return report, stages

    def test_absent_stages_never_invoked_and_score_absent(self) -> None:
        report, _stages = self._run()
        assert report.stage_status["metadata"] == STAGE_STATUS_ABSENT
        assert report.stage_status["futures"] == STAGE_STATUS_ABSENT
        assert report.stage_status["futures_prebuild"] == STAGE_STATUS_ABSENT
        # Vacuously settled — the DISTINCT token, ok=True, never `failed`.
        assert report.ok is True
        assert report.stopped_after is None
        assert STAGE_STATUS_FAILED not in report.stage_status.values()

    def test_greeks_not_withheld_over_absent_stages(self) -> None:
        """The pinned contract: the settle gate never withholds Greeks over
        ABSENT stages — the Greeks stage is INVOKED."""
        report, stages = self._run()
        assert stages["greeks"].calls == 1
        assert report.stage_status["greeks"] == STAGE_STATUS_SETTLED

    def test_strict_gate_never_withholds_over_absent_stages(self) -> None:
        """``strict_gate=True`` + absent metadata/futures: Greeks STILL runs
        (the ABSENT-vs-FAILED contract's strict arm, ADR-0021 Decision 3)."""
        report, stages = self._run(strict_gate=True)
        assert stages["greeks"].calls == 1
        assert report.ok is True
        assert report.post_steps_strict_skipped is False

    def test_present_stages_run_in_canonical_order(self) -> None:
        order: list[str] = []

        def _recorder(token: str) -> _Outcome:
            order.append(token)
            return _Outcome()

        report = run_full_pipeline(
            run_spot=lambda: _recorder("spot"),
            run_spot_daily=lambda: _recorder("spot_daily"),
            run_prebuild_spot=lambda: _recorder("spot_prebuild"),
            run_options=lambda: _recorder("options"),
            vendor="vendor2",
        )
        assert order == ["spot_daily", "spot", "spot_prebuild", "options"]
        assert report.ok is True

    def test_report_dict_carries_vendor_and_absent_tokens(self) -> None:
        report, _stages = self._run()
        payload = report.to_dict()
        assert payload["vendor"] == "vendor2"
        assert payload["stage_status"]["metadata"] == "absent"
        assert payload["stage_status"]["options"] == "settled"
        assert payload["ok"] is True
        # The concrete legacy report slots stay honestly None in the staged shape.
        assert payload["spot"] is None
        assert payload["metadata"] is None
        assert payload["historical"] is None


# --------------------------------------------------------------------------- #
# Fail-closed per stage + sequencer-owned stopped_after
# --------------------------------------------------------------------------- #


class TestFailClosedGates:
    def test_failed_options_stops_chain_and_skips_greeks(self) -> None:
        greeks = _Stage()
        with capture_logs() as logs:
            report = run_full_pipeline(
                run_spot=_Stage(),
                run_options=_Stage(_Outcome(ok=False)),
                run_greeks=greeks,
                vendor="vendor2",
            )
        assert report.stopped_after == "options"
        assert report.ok is False
        assert report.stage_status["options"] == STAGE_STATUS_FAILED
        # The bound-but-unreached stage is `skipped` — DISTINCT from `absent`.
        assert report.stage_status["greeks"] == STAGE_STATUS_SKIPPED
        assert greeks.calls == 0
        settled = _events(logs, "pipeline.stage.settled")
        assert settled[-1]["stage"] == "options"
        assert settled[-1]["ok"] is False
        assert "stopped after options" in report.note

    def test_failed_spot_daily_stops_before_any_one_minute_call(self) -> None:
        """RULING C's observability requirement: a native-daily failure halts
        BEFORE the 1-minute stage — ``stopped_after=spot_daily``."""
        spot = _Stage()
        report = run_full_pipeline(
            run_spot=spot,
            run_spot_daily=_Stage(_Outcome(ok=False)),
            run_options=_Stage(),
            vendor="vendor2",
        )
        assert report.stopped_after == "spot_daily"
        assert spot.calls == 0
        assert report.stage_status["spot"] == STAGE_STATUS_SKIPPED

    def test_none_outcome_is_not_a_failure(self) -> None:
        """``ok=None`` (ran, nothing to do) settles — the gate law."""
        report = run_full_pipeline(
            run_spot=_Stage(),
            run_options=_Stage(_Outcome(ok=None)),
            vendor="vendor2",
        )
        assert report.stopped_after is None
        assert report.stage_status["options"] == STAGE_STATUS_SETTLED


# --------------------------------------------------------------------------- #
# Halts + the market-hours belt
# --------------------------------------------------------------------------- #


class TestHaltsAndBelt:
    def test_lake_write_halt_reraises_with_stage_halted_event(self) -> None:
        with capture_logs() as logs, pytest.raises(LakeWriteHalt):
            run_full_pipeline(
                run_spot=_Stage(),
                run_options=_Stage(raises=LakeWriteHalt("disk_full")),
                vendor="vendor2",
            )
        halted = _events(logs, "pipeline.stage.halted")
        assert len(halted) == 1
        assert halted[0]["stage"] == "options"
        assert halted[0]["vendor"] == "vendor2"
        assert halted[0]["error_type"] == "LakeWriteHalt"

    def test_market_hours_halt_mid_stage_defers_cleanly(self) -> None:
        greeks = _Stage()
        report = run_full_pipeline(
            run_spot=_Stage(raises=MarketHoursHalt("inside window")),
            run_options=_Stage(),
            run_greeks=greeks,
            market_closed=lambda: False,
            vendor="vendor2",
        )
        assert report.stopped_after == PHASE_MARKET_HOURS
        assert report.stage_status["spot"] == STAGE_STATUS_SKIPPED
        assert report.stage_status["options"] == STAGE_STATUS_SKIPPED
        assert report.stage_status["metadata"] == STAGE_STATUS_ABSENT
        assert greeks.calls == 0
        assert "deferred" in report.note

    def test_belt_defers_before_a_vendor_stage(self) -> None:
        """The belt trips before the FIRST vendor stage — nothing runs."""
        spot = _Stage()
        with capture_logs() as logs:
            report = run_full_pipeline(
                run_spot=spot,
                run_options=_Stage(),
                market_closed=lambda: True,
                vendor="vendor2",
            )
        assert report.stopped_after == PHASE_MARKET_HOURS
        assert spot.calls == 0
        deferred = _events(logs, "pipeline.deferred.market_hours")
        assert deferred[0]["before"] == "spot"
        assert report.ok is False

    def test_belt_checked_per_vendor_stage_only(self) -> None:
        """The belt fires once per VENDOR stage (spot_daily/spot/metadata/
        futures/options) — never for prebuilds/Greeks (local compute)."""
        calls: list[bool] = []

        def market_closed() -> bool:
            calls.append(True)
            return False

        run_full_pipeline(
            run_spot=_Stage(),
            run_spot_daily=_Stage(),
            run_metadata=_Stage(),
            run_prebuild_spot=_Stage(),
            run_futures=_Stage(),
            run_prebuild_futures=_Stage(),
            run_options=_Stage(),
            run_greeks=_Stage(),
            market_closed=market_closed,
            vendor="vendor2",
        )
        assert len(calls) == 5  # exactly the five PRESENT vendor stages

    def test_belt_mid_chain_marks_prior_settled_and_rest_skipped(self) -> None:
        flips = iter([False, False, True])  # spot_daily ok, spot ok, options trips

        report = run_full_pipeline(
            run_spot=_Stage(),
            run_spot_daily=_Stage(),
            run_options=_Stage(),
            run_greeks=_Stage(),
            market_closed=lambda: next(flips),
            vendor="vendor2",
        )
        assert report.stopped_after == PHASE_MARKET_HOURS
        assert report.stage_status["spot_daily"] == STAGE_STATUS_SETTLED
        assert report.stage_status["spot"] == STAGE_STATUS_SETTLED
        assert report.stage_status["options"] == STAGE_STATUS_SKIPPED
        assert report.stage_status["greeks"] == STAGE_STATUS_SKIPPED


# --------------------------------------------------------------------------- #
# Observability (§10)
# --------------------------------------------------------------------------- #


class TestStageEvents:
    def test_planned_stage_list_logged_at_start(self) -> None:
        with capture_logs() as logs:
            run_full_pipeline(
                run_spot=_Stage(),
                run_spot_daily=_Stage(),
                run_options=_Stage(),
                vendor="vendor2",
            )
        planned = _events(logs, "pipeline.stages.planned")
        assert len(planned) == 1
        assert planned[0]["vendor"] == "vendor2"
        assert planned[0]["stages"] == ["spot_daily", "spot", "options"]
        assert planned[0]["absent"] == [
            "spot_prebuild",
            "metadata",
            "futures",
            "futures_prebuild",
            "greeks",
        ]

    def test_started_and_settled_events_carry_vendor_and_stage(self) -> None:
        with capture_logs() as logs:
            run_full_pipeline(run_spot=_Stage(), run_options=_Stage(), vendor="vendor2")
        started = _events(logs, "pipeline.stage.started")
        settled = _events(logs, "pipeline.stage.settled")
        assert [e["stage"] for e in started] == ["spot", "options"]
        assert [e["stage"] for e in settled] == ["spot", "options"]
        assert all(e["vendor"] == "vendor2" for e in started + settled)
        # settled(k) strictly precedes started(k+1) — sequential by construction.
        assert logs.index(settled[0]) < logs.index(started[1])

    def test_default_vendor_token_is_groww(self) -> None:
        with capture_logs() as logs:
            run_full_pipeline(run_spot=_Stage(), run_options=_Stage())
        assert _events(logs, "pipeline.stages.planned")[0]["vendor"] == "groww"


# --------------------------------------------------------------------------- #
# The movers-warm staged-graph law (ADR-0021 §4.1 supersession 4 — pinned here)
# --------------------------------------------------------------------------- #


class TestStagedMoversWarm:
    def test_warm_fires_on_settled_prebuilds_and_options(self) -> None:
        report = run_full_pipeline(
            run_spot=_Stage(),
            run_prebuild_spot=_Stage(),
            run_prebuild_futures=_Stage(),
            run_options=_Stage(),
            warm_spot_movers=lambda: _warm(3),
            warm_futures_movers=lambda: _warm(1),
            vendor="vendor2",
        )
        assert report.segment_movers_warmed == {"spot": 3, "futures": 1}
        assert report.movers_warmed == 4
        assert "movers spot 3 + futures 1 snapshot(s) warmed" in report.note

    def test_absent_futures_prebuild_is_vacuously_ok_for_the_warm(self) -> None:
        """A vendor without a futures capability never has its warm withheld
        by the missing stage — absent is vacuously ok (Decision 3)."""
        report = run_full_pipeline(
            run_spot=_Stage(),
            run_prebuild_spot=_Stage(),
            run_options=_Stage(),
            warm_spot_movers=_warm,
            vendor="vendor2",
        )
        assert report.segment_movers_warmed == {"spot": 2}

    def test_failed_options_withholds_the_warm(self) -> None:
        warmed: list[int] = []

        def _recording_warm() -> MoversWarmResult:
            warmed.append(1)
            return _warm(1)

        report = run_full_pipeline(
            run_spot=_Stage(),
            run_prebuild_spot=_Stage(),
            run_options=_Stage(_Outcome(ok=False)),
            warm_spot_movers=_recording_warm,
            vendor="vendor2",
        )
        assert warmed == []
        assert report.segment_movers_warmed == {}
        assert report.movers_warmed is None

    def test_warm_failure_counts_surface(self) -> None:
        report = run_full_pipeline(
            run_spot=_Stage(),
            run_prebuild_spot=_Stage(),
            run_options=_Stage(),
            warm_spot_movers=lambda: _warm(2, failed=1),
            vendor="vendor2",
        )
        assert report.segment_movers_failed == {"spot": 1}
        assert "(1 warm job(s) FAILED)" in report.note


# --------------------------------------------------------------------------- #
# The fail-loud shape laws (legacy XOR staged; legacy narrowing)
# --------------------------------------------------------------------------- #


class TestShapeDispatch:
    def test_mixing_legacy_and_staged_families_refuses(self) -> None:
        with pytest.raises(ValueError, match="mutually exclusive"):
            run_full_pipeline(
                run_spot=_Stage(),
                run_metadata=_metadata,
                run_historical=lambda _prior: _composite(historical=_historical_inner()),
                run_options=_Stage(),
            )

    def test_mixing_terminal_prebuild_with_staged_refuses(self) -> None:
        with pytest.raises(ValueError, match="mutually exclusive"):
            run_full_pipeline(
                run_spot=_Stage(),
                run_prebuild=lambda _names: pytest.fail("never invoked"),
                run_greeks=_Stage(),
            )

    def test_legacy_shape_missing_metadata_or_historical_refuses(self) -> None:
        with pytest.raises(ValueError, match="requires BOTH run_metadata"):
            run_full_pipeline(run_spot=_spot)

    def test_legacy_shape_refuses_protocol_shaped_spot(self) -> None:
        """The legacy narrowing: a non-BulkSpotReport spot outcome is a typed
        refusal — never a duck-typed wrong-shape crash later."""
        with pytest.raises(TypeError, match="run_spot must return a BulkSpotReport"):
            run_full_pipeline(
                run_spot=_Stage(),
                run_metadata=_metadata,
                run_historical=lambda _prior: _composite(historical=_historical_inner()),
            )

    def test_legacy_shape_refuses_protocol_shaped_metadata(self) -> None:
        with pytest.raises(TypeError, match="run_metadata must return a MetadataPullReport"):
            run_full_pipeline(
                run_spot=_spot,
                run_metadata=_Stage(),
                run_historical=lambda _prior: _composite(historical=_historical_inner()),
            )


# --------------------------------------------------------------------------- #
# The all-None-defaults byte-identity pin (ADR-0021 Decision 3)
# --------------------------------------------------------------------------- #

#: The FROZEN legacy report dict for the representative fake chain below —
#: captured from THIS tree and verified BYTE-EQUAL to ``origin/main``'s
#: ``run_full_pipeline`` output for the SAME chain by the A/B evidence run
#: recorded in the WS-B PR (the old-vs-new parity requirement). A drift in
#: any legacy key/value fails this pin.
_FROZEN_LEGACY_NOTE = (
    "pipeline complete — spot clean (3 pair(s)), metadata 12 expiries + 400 "
    "contracts, historical 5000 bars over 10 contract(s), timeframes pre-built "
    "for 4 symbol(s) (100 bars), movers spot 2 snapshot(s) warmed"
)


class TestAllNoneDefaultsParity:
    def test_legacy_chain_report_dict_is_byte_identical(self) -> None:
        """The representative legacy fake chain: report dict frozen, NO staged
        keys, the historical callable still receives the PriorStepsVerdict."""
        # A prebuild is required for the movers warm on the legacy path — use
        # the real PrebuildOutcome via the sibling suite's factory.
        from tests.unit.test_pipeline_pull import _outcome

        report = run_full_pipeline(
            run_spot=_spot,
            run_metadata=_metadata,
            run_historical=lambda _prior: _composite(historical=_historical_inner()),
            run_prebuild=lambda _names: _outcome(),
            warm_spot_movers=_warm,
        )
        payload = report.to_dict()
        assert report.ok is True
        assert report.stopped_after is None
        # EMIT-ON-STAGED: the legacy payload carries NEITHER staged key.
        assert "vendor" not in payload
        assert "stage_status" not in payload
        assert report.stage_status == {}
        assert report.vendor == "groww"
        assert payload["note"] == _FROZEN_LEGACY_NOTE

    def test_legacy_chain_without_prebuild_note_frozen(self) -> None:
        report = run_full_pipeline(
            run_spot=_spot,
            run_metadata=_metadata,
            run_historical=lambda _prior: _composite(historical=_historical_inner()),
        )
        assert report.note == (
            "pipeline complete — spot clean (3 pair(s)), metadata 12 expiries + "
            "400 contracts, historical 5000 bars over 10 contract(s)"
        )
        assert "vendor" not in report.to_dict()
