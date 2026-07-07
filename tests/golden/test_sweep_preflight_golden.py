"""Golden pins for the sweep preflight gate + estimator surfaces
(DEC-SWEEP-PREFLIGHT-ESTIMATOR-001, BUILD-SPEC §10.3).

Byte-pins the operator-facing text contracts (the coverage refusal block
incl. the ``(+K more spans)`` cap arm; the PROVISIONAL and calibrated
estimate headlines), freezes the machine contracts (the composed gate's
payload KEY-SET + ``schema_version: 1`` — a key-set pin, not a whole-JSON
byte-pin, so P4's frontend work never churns it; the calibration sidecar
JSON round-trip), and carries the NEGATIVE pins: deleted routes are never
suggested in any remediation, "pull on /universe first" is ALWAYS present
in data-remediation refusals, the writing ``probe_pull_singleflight`` is
imported by NO preflight module (AST pin), and the §10.3 structlog event
vocabulary exists verbatim in its owning modules.
"""

from __future__ import annotations

import ast
import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import polars as pl
from brutex.data.interior_gap_detect import INTERIOR_GAP
from brutex.data.session_coverage import PairScan, ScanKind
from brutex.data.timeframes import LAKE_INTERVAL_LABELS
from brutex.data.window_ledger import FAILED, WindowLedger
from brutex.data.window_ledger_store import save_window_ledger
from brutex.engine.sweep_calibration import (
    CALIBRATION_SCHEMA_VERSION,
    CalibrationSnapshot,
    RunActuals,
    calibration_path,
    hardware_fingerprint,
    load_calibration,
    record_run_calibration,
)
from brutex.engine.sweep_data_preflight import clamp_dates, preflight_disk_coverage
from brutex.engine.sweep_estimate import estimate_selection, selection_from_raw
from brutex.engine.sweep_gate import SweepPreflightOutcome, run_sweep_data_preflight

_EXCHANGE = "NSE"
_SEGMENT = "INDEX"
_SYMBOL = "NSE-NIFTY"
_TODAY = date(2026, 7, 7)

#: Text fragments of the REMOVED derive surfaces (DEC-PIPELINE-AUTOPREBUILD-001)
#: — a remediation suggesting any of them would send the operator to a 404.
_DELETED_ROUTE_FRAGMENTS = ("derive-timeframes", "BRUTEX_AUTO_DERIVE_TFS", "Pre-build timeframes")


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


def _run_gate(lake: Path, out_root: Path, **overrides: Any) -> SweepPreflightOutcome:
    """Run the composed gate over a small deterministic selection."""
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
        "start": date(2025, 3, 3),
        "end": date(2025, 3, 7),
        "today": date(2025, 4, 1),
        "observed": frozenset(),
        "free_bytes": 10**12,
        "lake_token": None,
        "env": {},
    }
    kwargs.update(overrides)
    return run_sweep_data_preflight(**kwargs)


def _default_estimate_kwargs() -> dict[str, Any]:
    """The blank-everything dashboard default (BUILD-SPEC §5.1 worked example)."""
    return {
        "symbols": ["NIFTY", "BANKNIFTY", "SENSEX"],
        "intervals": list(LAKE_INTERVAL_LABELS.values()),
        "directions": ["BUY", "SELL"],
        "k_values": [1, 2, 3],
        "stop_losses": [None, 5, 100, 200],
        "take_profits": [None, 10, 250, 500],
        "fib_anchors": ["prev_day"],
        "top_k": 20,
        "start": date(2020, 1, 1),
        "end": date(2026, 7, 6),
    }


class TestCoverageRefusalTextGolden:
    def test_refusal_block_byte_pinned(self) -> None:
        """The §0.3-style coverage refusal block — exact spans + the one remedy."""
        clamp = clamp_dates(date(2023, 5, 1), date(2023, 5, 5), today=_TODAY)
        expected = clamp.expected_sessions
        # The real calendar: 2023-05-01 is a holiday (Maharashtra Day) — pinned
        # so the byte-pin below is self-explaining.
        assert expected == (
            date(2023, 5, 2),
            date(2023, 5, 3),
            date(2023, 5, 4),
            date(2023, 5, 5),
        )
        scan = PairScan(
            symbol=_SYMBOL,
            interval="1minute",
            kind=ScanKind.DATES,
            covered=frozenset({date(2023, 5, 5)}),
            present=True,
            corrupt_files=(),
            bars_found=1,
            months_probed=1,
        )
        report = preflight_disk_coverage(
            expected, {(_SYMBOL, "1minute"): scan}, symbols=[_SYMBOL], intervals=["1minute"]
        )
        assert report.refusal() == (
            "data not ready on disk (1 not-ready verdict(s) over 1 requested pair(s)): "
            "NSE-NIFTY × 1minute: 3 of 4 expected sessions absent on disk — "
            "2023-05-02→2023-05-04 (3 sessions) — "
            "pull on /universe first (▶ Pull · full pipeline); the sweep NEVER pulls (R3)"
        )

    def test_span_cap_arm_byte_pinned(self) -> None:
        """15 isolated holes cap at 12 named spans + the TRUE totals tail."""
        expected = clamp_dates(date(2025, 1, 1), date(2025, 3, 31), today=_TODAY).expected_sessions
        missing = expected[0:29:2]  # 15 isolated single-session holes
        assert len(missing) == 15
        scan = PairScan(
            symbol=_SYMBOL,
            interval="1minute",
            kind=ScanKind.DATES,
            covered=frozenset(expected) - frozenset(missing),
            present=True,
            corrupt_files=(),
            bars_found=0,
            months_probed=3,
        )
        report = preflight_disk_coverage(
            expected, {(_SYMBOL, "1minute"): scan}, symbols=[_SYMBOL], intervals=["1minute"]
        )
        (verdict,) = report.not_ready
        named = ", ".join(f"{day.isoformat()} (1 session)" for day in missing[:12])
        assert verdict.describe() == (
            f"NSE-NIFTY × 1minute: 15 of {len(expected)} expected sessions absent on disk — "
            f"{named} (+3 more spans; 15 spans / 15 missing sessions in total)"
        )

    def test_block_appears_verbatim_in_the_composed_renderer(self, tmp_path: Path) -> None:
        """One renderer, two surfaces: refusal_text carries the pure fold's block
        and ALWAYS ends with the pinned remedy line."""
        outcome = _run_gate(tmp_path / "lake", tmp_path / "out")  # empty lake ⇒ refused
        assert outcome.coverage is not None
        text = outcome.refusal_text()
        assert outcome.coverage.refusal() in text
        assert text.endswith(
            "fix every cause above, then re-run the sweep — this refusal wrote NOTHING "
            "(probe-only preflight; the sweep NEVER pulls, R3)."
        )
        assert outcome.payload()["refusal_text"] == text


class TestHeadlineGolden:
    def test_provisional_headline_byte_pinned(self) -> None:
        """The §2.2 worked default — 72,676,800 trials / 50.4 h / PROVISIONAL."""
        sel = selection_from_raw(**_default_estimate_kwargs())
        estimate = estimate_selection(sel, calibration=None, dedup_map_bytes=2 * 2**30, env={})
        assert estimate.headline() == (
            "this selection = 72,676,800 trials, est. 50.4 h (PROVISIONAL — uncalibrated), "
            "~0.14 GB results (+2.0 GiB dedup map reserve) — proceed?"
        )

    def test_calibrated_headline_byte_pinned(self) -> None:
        """A qualifying hardware-matched snapshot flips the label with provenance."""
        sel = selection_from_raw(**_default_estimate_kwargs())
        snapshot = CalibrationSnapshot(
            trials_per_second=1210.0,
            bytes_per_row=987.2,
            samples=3,
            qualifying_samples=2,
            fingerprint="cores=10;ram=51539607552",
            hardware_match=True,
            last_run_ts="2026-07-07T10:00:00Z",
        )
        estimate = estimate_selection(sel, calibration=snapshot, dedup_map_bytes=2 * 2**30, env={})
        assert estimate.headline() == (
            "this selection = 72,676,800 trials, est. 16.7 h "
            "(calibrated from 3 runs, last 2026-07-07), "
            "~0.13 GB results (+2.0 GiB dedup map reserve) — proceed?"
        )
        # The retired program-scale figures appear NOWHERE (R20 — extreme test 11).
        blob = json.dumps(estimate.payload())
        assert "33000000000" not in blob.replace(",", "").replace("_", "")
        assert "730000000000" not in blob.replace(",", "").replace("_", "")


class TestPayloadKeySetGolden:
    def test_payload_key_set_and_schema_version_pinned(self, tmp_path: Path) -> None:
        """The P2-P6 machine contract: schema_version=1 + frozen key-sets."""
        lake = tmp_path / "lake"
        expected = clamp_dates(date(2025, 3, 3), date(2025, 3, 7), today=date(2025, 4, 1))
        _write_days(lake, "1minute", expected.expected_sessions)
        payload = _run_gate(lake, tmp_path / "out", lake_token="rev").payload()
        assert payload["schema_version"] == 1
        assert set(payload) == {
            "schema_version",
            "ok",
            "refusals",
            "refusal_text",
            "exclusion",
            "clamp",
            "coverage",
            "quarantine",
            "estimate",
            "sla",
            "disk",
            "notes",
            "confirm_token",
        }
        exclusion = payload["exclusion"]
        assert isinstance(exclusion, dict)
        assert set(exclusion) == {"ok", "pull_active", "derive_active"}
        clamp = payload["clamp"]
        assert isinstance(clamp, dict)
        assert set(clamp) == {
            "requested_start",
            "requested_end",
            "start",
            "end",
            "notes",
            "reversed_range",
            "zero_sessions",
            "out_of_calendar",
            "expected_sessions",
        }
        coverage = payload["coverage"]
        assert isinstance(coverage, dict)
        assert set(coverage) == {
            "ok",
            "checked_pairs",
            "expected_sessions",
            "not_ready",
            "covered_sessions",
        }
        quarantine = payload["quarantine"]
        assert isinstance(quarantine, dict)
        assert set(quarantine) == {"ok", "ledger_load_failed", "demoted_open", "accepted_gaps"}
        estimate = payload["estimate"]
        assert isinstance(estimate, dict)
        assert estimate["schema_version"] == 1
        assert set(estimate) == {
            "schema_version",
            "trials",
            "tuples",
            "masks",
            "n_conditions",
            "rows_upper_bound",
            "est_result_bytes",
            "store_bytes",
            "sidecar_bytes",
            "audit_bytes",
            "dedup_map_bytes",
            "est_seconds",
            "est_hours",
            "est_seconds_fallback",
            "est_hours_fallback",
            "time_model",
            "bytes_model",
            "provisional",
            "calibration_samples",
            "calibration_last_ts",
            "absurd_k",
            "warnings",
            "notes",
            "headline",
        }
        sla = payload["sla"]
        assert isinstance(sla, dict)
        assert set(sla) == {
            "decision",
            "tuple_count",
            "per_tuple_seconds",
            "projected_seconds",
            "projected_days",
            "envelope_seconds",
        }
        disk = payload["disk"]
        assert isinstance(disk, dict)
        assert set(disk) == {
            "ok",
            "blocked",
            "free_bytes",
            "need_bytes",
            "margin",
            "probe_error",
            "volume",
        }
        # JSON-safety: the whole payload round-trips losslessly.
        assert json.loads(json.dumps(payload)) == payload

    def test_refused_payload_nested_entry_key_sets(self, tmp_path: Path) -> None:
        """Verdict / span / flag entry key-sets frozen on a refused payload."""
        lake = tmp_path / "lake"  # empty ⇒ coverage refusal
        save_window_ledger(
            WindowLedger.from_rows(
                [
                    (
                        "groww",
                        _SYMBOL,
                        "1minute",
                        "2025-03-01",
                        "2025-03-31",
                        FAILED,
                        0,
                        1,
                        INTERIOR_GAP,
                        "2026-07-01T00:00:00+00:00",
                        "2026-07-01T00:00:00+00:00",
                        0,
                        1,
                        None,
                    )
                ]
            ),
            lake,
        )
        payload = _run_gate(lake, tmp_path / "out").payload()
        assert payload["ok"] is False
        refusals = payload["refusals"]
        assert isinstance(refusals, list)
        assert all(set(entry) == {"code", "message"} for entry in refusals)
        coverage = payload["coverage"]
        assert isinstance(coverage, dict)
        verdict = coverage["not_ready"][0]
        assert set(verdict) == {"symbol", "interval", "reason", "detail", "spans", "text"}
        assert all(set(span) == {"start", "end", "sessions"} for span in verdict["spans"])
        assert all(
            set(entry) == {"symbol", "interval", "covered"}
            for entry in coverage["covered_sessions"]
        )
        quarantine = payload["quarantine"]
        assert isinstance(quarantine, dict)
        flag = quarantine["demoted_open"][0]
        assert set(flag) == {
            "security_id",
            "interval",
            "window_start",
            "window_end",
            "status",
            "last_error",
        }


class TestCalibrationStoreGolden:
    def test_sidecar_json_round_trip_key_set(self, tmp_path: Path) -> None:
        """The §2.3 sidecar schema: key-sets frozen; load == the returned snapshot."""
        sel = selection_from_raw(**_default_estimate_kwargs())
        estimate = estimate_selection(sel, calibration=None, env={})
        fingerprint = hardware_fingerprint()
        snapshot = record_run_calibration(
            tmp_path,
            estimate=estimate,
            actuals=RunActuals(
                run_identity="run-golden",
                wall_seconds=120.0,
                trials_enumerated=200_000,
                tuples_run=6_048,
                rows_persisted=120_960,
                result_bytes=123_900_411,
            ),
            fingerprint=fingerprint,
            now=datetime(2026, 7, 7, 12, 0, 0, tzinfo=UTC),
        )
        stored = json.loads(calibration_path(tmp_path).read_text(encoding="utf-8"))
        assert stored["schema_version"] == CALIBRATION_SCHEMA_VERSION == 1
        assert set(stored) == {"schema_version", "fingerprint", "updated_at", "derived", "runs"}
        assert set(stored["derived"]) == {
            "trials_per_second",
            "bytes_per_row",
            "samples",
            "qualifying_samples",
            "last_run_ts",
        }
        (run_entry,) = stored["runs"]
        assert set(run_entry) == {
            "ts",
            "run_identity",
            "trials_est",
            "trials_actual",
            "seconds_est",
            "seconds_actual",
            "bytes_est",
            "bytes_actual",
            "tuples",
            "rows_persisted",
            "time_model",
            "qualifying",
        }
        assert run_entry["qualifying"] is True
        # Round-trip: the fail-soft loader reproduces the derived snapshot.
        assert load_calibration(tmp_path, fingerprint=fingerprint) == snapshot


class TestNegativePins:
    def test_deleted_routes_never_suggested_anywhere(self, tmp_path: Path) -> None:
        """No remediation may point at the REMOVED derive surfaces (#1145)."""
        lake = tmp_path / "lake"  # empty + quarantined + disk-starved refusal
        save_window_ledger(
            WindowLedger.from_rows(
                [
                    (
                        "groww",
                        _SYMBOL,
                        "1minute",
                        "2025-03-01",
                        "2025-03-31",
                        FAILED,
                        0,
                        1,
                        INTERIOR_GAP,
                        "2026-07-01T00:00:00+00:00",
                        "2026-07-01T00:00:00+00:00",
                        0,
                        1,
                        None,
                    )
                ]
            ),
            lake,
        )
        outcome = _run_gate(lake, tmp_path / "out", intervals=["1minute", "5minute"], free_bytes=0)
        assert outcome.ok is False
        blob = outcome.refusal_text() + json.dumps(outcome.payload())
        for fragment in _DELETED_ROUTE_FRAGMENTS:
            assert fragment not in blob

    def test_pull_on_universe_always_present_in_data_remediation(self, tmp_path: Path) -> None:
        """Every data-readiness refusal (coverage + demoted quarantine) names the
        ONE remedy: pull on /universe first."""
        lake = tmp_path / "lake"
        save_window_ledger(
            WindowLedger.from_rows(
                [
                    (
                        "groww",
                        _SYMBOL,
                        "1minute",
                        "2025-03-01",
                        "2025-03-31",
                        FAILED,
                        0,
                        1,
                        INTERIOR_GAP,
                        "2026-07-01T00:00:00+00:00",
                        "2026-07-01T00:00:00+00:00",
                        0,
                        1,
                        None,
                    )
                ]
            ),
            lake,
        )
        outcome = _run_gate(lake, tmp_path / "out")
        by_code = {str(r.code): r.message for r in outcome.refusals}
        assert "pull on /universe first" in by_code["coverage_missing"]
        assert "pull on /universe first" in by_code["quarantined_range"]

    def test_probe_pull_singleflight_in_no_preflight_import_graph(self) -> None:
        """AST pin: the WRITING probe is imported by NO preflight module, and no
        preflight module imports ``brutex.adapters`` at all (D6 layering)."""
        import brutex.data.session_coverage
        import brutex.data.sweep_exclusion
        import brutex.engine.sweep_calibration
        import brutex.engine.sweep_data_preflight
        import brutex.engine.sweep_estimate
        import brutex.engine.sweep_gate

        modules = [
            brutex.engine.sweep_gate,
            brutex.engine.sweep_data_preflight,
            brutex.engine.sweep_estimate,
            brutex.engine.sweep_calibration,
            brutex.data.session_coverage,
            brutex.data.sweep_exclusion,
        ]
        for module in modules:
            source_path = module.__file__
            assert source_path is not None
            tree = ast.parse(Path(source_path).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        assert not alias.name.startswith("brutex.adapters"), module.__name__
                elif isinstance(node, ast.ImportFrom):
                    from_module = node.module or ""
                    assert not from_module.startswith("brutex.adapters"), module.__name__
                    for alias in node.names:
                        assert alias.name != "probe_pull_singleflight", module.__name__


class TestStructlogVocabularyPins:
    def test_event_names_exist_verbatim_in_their_owning_modules(self) -> None:
        """The §10.3 vocabulary — the P2 funnel contract, one owner per name."""
        import brutex.data.sweep_exclusion
        import brutex.engine.sweep_calibration
        import brutex.engine.sweep_estimate
        import brutex.engine.sweep_gate

        owners: dict[Any, tuple[str, ...]] = {
            brutex.engine.sweep_gate: ("sweep.preflight.passed", "sweep.preflight.refused"),
            brutex.data.sweep_exclusion: (
                "sweep.preflight.lock_refused",
                "pull.sweep_exclusion_refused",
            ),
            brutex.engine.sweep_estimate: ("sweep.estimate", "sweep.estimate.disk"),
            brutex.engine.sweep_calibration: (
                "sweep.calibration.load_failed",
                "sweep.calibration.hardware_mismatch",
                "sweep.estimate_vs_actual",
                "sweep.calibration.record_failed",
            ),
        }
        for module, events in owners.items():
            source_path = module.__file__
            assert source_path is not None
            source = Path(source_path).read_text(encoding="utf-8")
            for event in events:
                assert f'"{event}"' in source, (module.__name__, event)
