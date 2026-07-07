"""Tests for `brutex.engine.sweep_estimate` (DEC-SWEEP-PREFLIGHT-ESTIMATOR-001).

The live trials/GB/hours estimator + the fail-CLOSED results-disk decision +
the confirm token — BUILD-SPEC §2.2/§5/§7, extreme tests 10/11/12/14
(§10.1 rows 10-14). Pure module: every test is injected-data only, zero I/O.
"""

from __future__ import annotations

import inspect
import json
from dataclasses import replace
from datetime import date
from decimal import Decimal
from math import nan
from pathlib import Path

import brutex.engine.sweep_estimate as sweep_estimate_module
import pytest
from brutex.data.timeframes import LAKE_INTERVAL_LABELS
from brutex.engine.sweep_calibration import CalibrationSnapshot
from brutex.engine.sweep_estimate import (
    ABSURD_K_MAX,
    AUDIT_BYTES_PER_TUPLE,
    ESTIMATE_SCHEMA_VERSION,
    FALLBACK_RESULT_BYTES_PER_ROW,
    SWEEP_DISK_SAFETY_MARGIN,
    TRIAL_SIDECAR_BYTES,
    DiskPreflightDecision,
    EstimateWarning,
    SweepEstimate,
    SweepSelection,
    check_results_disk,
    default_condition_bits,
    estimate_selection,
    estimate_trials,
    estimate_tuples,
    selection_confirm_token,
    selection_from_raw,
)
from structlog.testing import capture_logs

_START = date(2020, 1, 1)
_END = date(2026, 7, 3)


def _default_selection(**overrides: object) -> SweepSelection:
    """The blank-everything dashboard default selection (BUILD-SPEC §5.1 worked example)."""
    kwargs: dict[str, object] = {
        "symbols": ["NIFTY", "BANKNIFTY", "SENSEX"],
        "intervals": list(LAKE_INTERVAL_LABELS.values()),
        "directions": ["BUY", "SELL"],
        "k_values": [1, 2, 3],
        "stop_losses": [None, 5, 100, 200],
        "take_profits": [None, 10, 250, 500],
        "fib_anchors": ["prev_day"],
        "top_k": 20,
        "start": _START,
        "end": _END,
    }
    kwargs.update(overrides)
    return selection_from_raw(**kwargs)  # type: ignore[arg-type]


def _calibrated_snapshot(
    *,
    trials_per_second: float | None = 1210.0,
    bytes_per_row: float | None = 987.2,
    samples: int = 3,
    qualifying_samples: int = 2,
    hardware_match: bool = True,
    last_run_ts: str | None = "2026-07-07T10:00:00Z",
) -> CalibrationSnapshot:
    """A hand-built snapshot (the §2.3 worked derived block)."""
    return CalibrationSnapshot(
        trials_per_second=trials_per_second,
        bytes_per_row=bytes_per_row,
        samples=samples,
        qualifying_samples=qualifying_samples,
        fingerprint="cores=10;ram=51539607552",
        hardware_match=hardware_match,
        last_run_ts=last_run_ts,
    )


def _mini_estimate(est_result_bytes: int) -> SweepEstimate:
    """A directly-built minimal estimate for the disk-decision arithmetic pins."""
    return SweepEstimate(
        trials=1,
        tuples=1,
        masks=1,
        n_conditions=60,
        rows_upper_bound=1,
        est_result_bytes=est_result_bytes,
        store_bytes=est_result_bytes,
        sidecar_bytes=0,
        audit_bytes=0,
        dedup_map_bytes=0,
        est_seconds=30.0,
        est_seconds_fallback=30.0,
        time_model="per_tuple_sla_fallback",
        bytes_model="projection_fallback",
        provisional=True,
        calibration_samples=0,
        calibration_last_ts=None,
        absurd_k=False,
        warnings=(),
        notes=(),
    )


class TestDefaultConditionBits:
    def test_every_implemented_bit_ascending(self) -> None:
        bits = default_condition_bits()
        assert bits == tuple(range(60))
        assert isinstance(bits, tuple)


class TestSelectionFromRaw:
    def test_dedup_is_first_seen_order(self) -> None:
        sel = _default_selection(
            symbols=["NIFTY", "NIFTY", "SENSEX"],
            k_values=[2, 1, 2],
            stop_losses=[None, 5, None],
            take_profits=[10, 10],
            fib_anchors=["prev_day", "prev_day"],
        )
        assert sel.symbols == ("NIFTY", "SENSEX")
        assert sel.k_values == (2, 1)
        assert sel.stop_losses == (None, 5)
        assert sel.take_profits == (10,)
        assert sel.fib_anchors == ("prev_day",)

    def test_default_condition_bits_give_n60(self) -> None:
        assert _default_selection().n_conditions == 60

    def test_explicit_condition_bits_deduped(self) -> None:
        sel = _default_selection(condition_bits=[0, 1, 2, 2, 1])
        assert sel.n_conditions == 3

    def test_proximities_eighth_axis_never_dropped(self) -> None:
        sel = _default_selection(proximities=[None, 0.5, 0.5])
        assert sel.proximities == (None, 0.5)
        assert estimate_tuples(sel) == 6_048 * 2

    def test_empty_axis_is_value_error(self) -> None:
        with pytest.raises(ValueError, match="'symbols' must be non-empty"):
            _default_selection(symbols=[])

    def test_empty_condition_bits_is_value_error(self) -> None:
        with pytest.raises(ValueError, match="condition_bits must be non-empty"):
            _default_selection(condition_bits=[])

    def test_top_k_below_one_is_value_error(self) -> None:
        with pytest.raises(ValueError, match="top_k must be >= 1"):
            _default_selection(top_k=0)

    def test_negative_k_is_value_error(self) -> None:
        with pytest.raises(ValueError, match=r"k=-1 out of range"):
            _default_selection(k_values=[-1])

    def test_k_past_n_conditions_is_value_error(self) -> None:
        with pytest.raises(ValueError, match=r"k=61 out of range"):
            _default_selection(k_values=[61])


def test_r20_estimate_tracks_selection_no_stale_default() -> None:
    """R20 / extreme test 11 — the estimate is LIVE from the selection.

    The blank-everything default is hand-derived in BUILD-SPEC §5.1:
    ``masks = C(60,1)+C(60,2)+C(60,3) = 60+1,770+34,220 = 36,050``; axis
    points ``3·21·2·16 = 2,016`` ⇒ **72,676,800 trials / 6,048 tuples**.
    Dropping one timeframe moves X by the closed-form delta; the RETIRED
    33B/730B figures appear NOWHERE in any payload.
    """
    sel = _default_selection()
    assert estimate_trials(sel) == 72_676_800
    assert estimate_tuples(sel) == 6_048

    est = estimate_selection(sel, calibration=None)
    assert est.trials == 72_676_800
    assert est.tuples == 6_048
    assert est.masks == 36_050
    # Closed-form delta: trials scale linearly with the interval axis width.
    intervals = list(LAKE_INTERVAL_LABELS.values())
    sel_minus_one = _default_selection(intervals=intervals[:-1])
    assert estimate_trials(sel_minus_one) == 72_676_800 // 21 * 20 == 69_216_000
    assert estimate_tuples(sel_minus_one) == 5_760
    # The retired hardcoded figures never surface (R20 "33B RETIRED").
    blob = json.dumps(est.payload())
    assert "33000000000" not in blob
    assert "730000000000" not in blob


def test_r20_absurd_k_warns_before_compute() -> None:
    """R20 / extreme test 10 — k>3 WARNS (computed text), never blocks.

    Purity pin (the "zero lake reads" spy): the module holds NO I/O
    machinery at runtime — no ``os``, no ``pathlib``/``Path``, no ``open``
    — so the warning is computed before any I/O could even exist.
    """
    sel = _default_selection(k_values=[1, 2, 3, 4, 5])
    est = estimate_selection(sel, calibration=None)
    assert est.absurd_k is True
    assert len(est.warnings) == 1
    warning = est.warnings[0]
    assert warning.code == "absurd_depth"
    assert "4, 5" in warning.text
    assert f"k≤{ABSURD_K_MAX}" in warning.text
    assert "never a block" in warning.text
    assert "SLA verdict is the time wall" in warning.text
    # k <= 3 never warns.
    calm = estimate_selection(_default_selection(), calibration=None)
    assert calm.absurd_k is False
    assert calm.warnings == ()
    # Purity: no runtime I/O symbols in the module namespace.
    assert not hasattr(sweep_estimate_module, "os")
    assert not hasattr(sweep_estimate_module, "Path")


def test_r23_uncalibrated_estimate_is_provisional() -> None:
    """R23 / extreme test 12 — every time figure carries PROVISIONAL until calibrated."""
    sel = _default_selection()
    est = estimate_selection(sel, calibration=None)
    assert est.provisional is True
    assert est.time_model == "per_tuple_sla_fallback"
    assert est.est_seconds == est.est_seconds_fallback == 6_048 * 30.0
    assert "PROVISIONAL" in est.headline()
    assert any("UNBENCHMARKED" in note for note in est.notes)
    # A hardware-MISMATCHED snapshot stays PROVISIONAL (R23: foreign store never calibrates).
    foreign = _calibrated_snapshot(hardware_match=False)
    est_foreign = estimate_selection(sel, calibration=foreign)
    assert est_foreign.provisional is True
    assert est_foreign.time_model == "per_tuple_sla_fallback"
    assert "PROVISIONAL" in est_foreign.headline()
    # A qualifying hardware-matched snapshot flips the label with provenance.
    est_cal = estimate_selection(sel, calibration=_calibrated_snapshot())
    assert est_cal.provisional is False
    assert est_cal.time_model == "calibrated_trials"
    assert est_cal.est_seconds == pytest.approx(72_676_800 / 1210.0)
    assert est_cal.est_seconds_fallback == 6_048 * 30.0
    assert "calibrated from 3 runs, last 2026-07-07" in est_cal.headline()


class TestBytesModels:
    def test_projection_fallback_terms(self) -> None:
        sel = _default_selection()
        est = estimate_selection(sel, calibration=None)
        # rows_upper = Σ_k tuples_per_k × min(top_k, C(60, k)) = 2,016 × 20 × 3.
        assert est.rows_upper_bound == 120_960
        assert est.store_bytes == 120_960 * FALLBACK_RESULT_BYTES_PER_ROW
        assert est.sidecar_bytes == 6_048 * TRIAL_SIDECAR_BYTES
        assert est.audit_bytes == 6_048 * AUDIT_BYTES_PER_TUPLE
        assert est.dedup_map_bytes == 0
        assert est.est_result_bytes == est.store_bytes + est.sidecar_bytes + est.audit_bytes
        assert est.bytes_model == "projection_fallback"
        assert any("UNMEASURED projections" in note for note in est.notes)

    def test_top_k_truncation_uses_mask_count_when_smaller(self) -> None:
        # k=1 ⇒ C(60,1)=60 < top_k=100 ⇒ min() takes the mask count.
        sel = _default_selection(k_values=[1], top_k=100)
        est = estimate_selection(sel, calibration=None)
        assert est.rows_upper_bound == estimate_tuples(sel) * 60

    def test_calibrated_bytes_per_row(self) -> None:
        sel = _default_selection()
        est = estimate_selection(sel, calibration=_calibrated_snapshot())
        assert est.bytes_model == "calibrated_bytes_per_row"
        assert est.store_bytes == int(120_960 * 987.2)
        assert any("calibrated bytes/row" in note for note in est.notes)

    def test_calibrated_time_without_bytes_rate_mixes_models(self) -> None:
        snapshot = _calibrated_snapshot(bytes_per_row=None)
        est = estimate_selection(_default_selection(), calibration=snapshot)
        assert est.time_model == "calibrated_trials"
        assert est.bytes_model == "projection_fallback"

    def test_zero_bytes_rate_falls_back(self) -> None:
        snapshot = _calibrated_snapshot(bytes_per_row=0.0)
        est = estimate_selection(_default_selection(), calibration=snapshot)
        assert est.bytes_model == "projection_fallback"

    def test_degenerate_zero_trials_rate_stays_provisional(self) -> None:
        # A corrupt snapshot claiming a 0 trials/s rate must never divide-by-zero:
        # the time model degrades honestly to the fallback + PROVISIONAL.
        snapshot = _calibrated_snapshot(trials_per_second=0.0)
        est = estimate_selection(_default_selection(), calibration=snapshot)
        assert est.provisional is True
        assert est.time_model == "per_tuple_sla_fallback"

    def test_none_trials_rate_stays_provisional(self) -> None:
        snapshot = _calibrated_snapshot(trials_per_second=None)
        est = estimate_selection(_default_selection(), calibration=snapshot)
        assert est.provisional is True


class TestTimeKnobs:
    def test_explicit_per_tuple_seconds(self) -> None:
        est = estimate_selection(_default_selection(), calibration=None, per_tuple_seconds=2.0)
        assert est.est_seconds_fallback == 6_048 * 2.0

    def test_env_override_is_honored(self) -> None:
        est = estimate_selection(
            _default_selection(),
            calibration=None,
            env={"BRUTEX_SWEEP_PER_TUPLE_SECONDS": "2.5"},
        )
        assert est.est_seconds_fallback == 6_048 * 2.5

    def test_env_garbage_fails_loud(self) -> None:
        with pytest.raises(ValueError, match="BRUTEX_SWEEP_PER_TUPLE_SECONDS"):
            estimate_selection(
                _default_selection(),
                calibration=None,
                env={"BRUTEX_SWEEP_PER_TUPLE_SECONDS": "fast"},
            )

    def test_explicit_zero_per_tuple_seconds_fails_loud(self) -> None:
        with pytest.raises(ValueError, match="per_tuple_seconds"):
            estimate_selection(_default_selection(), calibration=None, per_tuple_seconds=0.0)

    def test_explicit_nan_per_tuple_seconds_fails_loud(self) -> None:
        with pytest.raises(ValueError, match="per_tuple_seconds"):
            estimate_selection(_default_selection(), calibration=None, per_tuple_seconds=nan)

    def test_negative_dedup_map_bytes_fails_loud(self) -> None:
        with pytest.raises(ValueError, match="dedup_map_bytes"):
            estimate_selection(_default_selection(), calibration=None, dedup_map_bytes=-1)

    def test_estimate_event_emitted(self) -> None:
        with capture_logs() as logs:
            estimate_selection(_default_selection(), calibration=None)
        assert [entry["event"] for entry in logs] == ["sweep.estimate"]


class TestHeadline:
    def test_default_provisional_headline_golden(self) -> None:
        """The BUILD-SPEC §2.2 golden-pinned headline shape, byte-exact."""
        est = estimate_selection(_default_selection(), calibration=None, dedup_map_bytes=2 * 2**30)
        assert est.headline() == (
            "this selection = 72,676,800 trials, est. 50.4 h "
            "(PROVISIONAL — uncalibrated), ~0.14 GB results "
            "(+2.0 GiB dedup map reserve) — proceed?"
        )
        assert any("dedup map reserve" in note for note in est.notes)

    def test_calibrated_headline_without_dedup_reserve(self) -> None:
        est = estimate_selection(_default_selection(), calibration=_calibrated_snapshot())
        headline = est.headline()
        assert "(calibrated from 3 runs, last 2026-07-07)" in headline
        assert "dedup map reserve" not in headline
        assert headline.endswith("— proceed?")

    def test_calibrated_headline_with_unknown_last_ts(self) -> None:
        # Defensive display arm: a calibrated estimate whose snapshot carried no
        # timestamp reads "last unknown", never a crash.
        est = replace(
            _mini_estimate(100),
            provisional=False,
            calibration_samples=2,
            calibration_last_ts=None,
        )
        assert "calibrated from 2 runs, last unknown" in est.headline()


class TestPayload:
    def test_schema_version_and_dual_time_figures(self) -> None:
        est = estimate_selection(_default_selection(), calibration=None)
        payload = est.payload()
        assert payload["schema_version"] == ESTIMATE_SCHEMA_VERSION == 1
        assert payload["est_hours"] == pytest.approx(50.4)
        assert payload["est_hours_fallback"] == pytest.approx(50.4)
        assert payload["time_model"] == "per_tuple_sla_fallback"
        assert payload["bytes_model"] == "projection_fallback"
        assert payload["headline"] == est.headline()
        assert isinstance(payload["notes"], list)
        # R27 honesty note rides every payload.
        assert any("NOT proportional to trials" in note for note in est.notes)
        # JSON-safe end to end.
        json.dumps(payload)

    def test_warnings_serialized_as_dicts(self) -> None:
        est = estimate_selection(_default_selection(k_values=[5]), calibration=None)
        payload = est.payload()
        assert payload["warnings"] == [{"code": est.warnings[0].code, "text": est.warnings[0].text}]
        assert payload["absurd_k"] is True

    def test_warning_dataclass_shape(self) -> None:
        warning = EstimateWarning(code="absurd_depth", text="t")
        assert (warning.code, warning.text) == ("absurd_depth", "t")


def test_disk_preflight_insufficient_free_refuses_fail_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Extreme test 14 — free < need ⇒ blocked; probe failure ⇒ blocked (fail-CLOSED).

    NEGATIVE pin: NO kill switch exists — ``BRUTEX_SWEEP_DISK_GUARD`` is not
    consulted anywhere in the module (BUILD-SPEC excision #2), and setting it
    changes nothing.
    """
    est = _mini_estimate(100)
    out_root = tmp_path / "sweeps"
    blocked = check_results_disk(est, out_root=out_root, free_bytes=109)
    assert blocked.blocked is True
    assert blocked.ok is False
    assert blocked.need_bytes == 110  # 100 × (1 + 0.10)
    refusal = blocked.refusal()
    assert "GiB free" in refusal
    assert "GiB needed" in refusal
    assert str(out_root) in refusal
    assert "--results-root" in refusal
    # Probe failure (caller mapped OSError → None) is fail-CLOSED.
    failed = check_results_disk(est, out_root=out_root, free_bytes=None)
    assert failed.blocked is True
    assert failed.free_bytes is None
    assert failed.probe_error is not None
    assert "fail-CLOSED" in failed.refusal()
    assert "--results-root" in failed.refusal()
    # NO kill switch: not in the source, and the env var changes nothing.
    source = inspect.getsource(sweep_estimate_module)
    assert "BRUTEX_SWEEP_DISK_GUARD" not in source
    monkeypatch.setenv("BRUTEX_SWEEP_DISK_GUARD", "off")
    still_blocked = check_results_disk(est, out_root=out_root, free_bytes=None)
    assert still_blocked.blocked is True


class TestDiskDecision:
    def test_sufficient_free_allows(self, tmp_path: Path) -> None:
        decision = check_results_disk(_mini_estimate(100), out_root=tmp_path, free_bytes=110)
        assert decision.blocked is False
        assert decision.ok is True
        assert decision.probe_error is None
        assert decision.margin == SWEEP_DISK_SAFETY_MARGIN

    def test_need_is_ceiling_rounded_never_truncated(self, tmp_path: Path) -> None:
        decision = check_results_disk(_mini_estimate(1), out_root=tmp_path, free_bytes=1)
        assert decision.need_bytes == 2  # ceil(1 × 1.10) — fail-closed rounding
        assert decision.blocked is True

    def test_nan_margin_fails_loud(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="margin"):
            check_results_disk(
                _mini_estimate(1), out_root=tmp_path, free_bytes=1, margin=Decimal("NaN")
            )

    def test_negative_margin_fails_loud(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="margin"):
            check_results_disk(
                _mini_estimate(1), out_root=tmp_path, free_bytes=1, margin=Decimal("-0.1")
            )

    def test_disk_event_emitted_both_arms(self, tmp_path: Path) -> None:
        with capture_logs() as logs:
            check_results_disk(_mini_estimate(100), out_root=tmp_path, free_bytes=200)
            check_results_disk(_mini_estimate(100), out_root=tmp_path, free_bytes=0)
        events = [(entry["event"], entry["log_level"]) for entry in logs]
        assert events == [("sweep.estimate.disk", "info"), ("sweep.estimate.disk", "warning")]

    def test_decision_dataclass_is_frozen(self, tmp_path: Path) -> None:
        decision = check_results_disk(_mini_estimate(1), out_root=tmp_path, free_bytes=100)
        assert isinstance(decision, DiskPreflightDecision)
        with pytest.raises(AttributeError):
            decision.blocked = True  # type: ignore[misc]


class TestConfirmToken:
    def test_deterministic_16_hex(self) -> None:
        sel = _default_selection()
        token = selection_confirm_token(
            sel, launches=[("NSE", "INDEX"), ("BSE", "INDEX")], lake_token="rev-1"
        )
        assert token == selection_confirm_token(
            sel, launches=[("NSE", "INDEX"), ("BSE", "INDEX")], lake_token="rev-1"
        )
        assert len(token) == 16
        assert all(c in "0123456789abcdef" for c in token)

    def test_token_moves_with_selection_dates_launches_and_lake(self) -> None:
        sel = _default_selection()
        launches = [("NSE", "INDEX")]
        base = selection_confirm_token(sel, launches=launches, lake_token="rev-1")
        edited = _default_selection(symbols=["NIFTY"])
        assert selection_confirm_token(edited, launches=launches, lake_token="rev-1") != base
        redated = _default_selection(end=date(2026, 7, 2))
        assert selection_confirm_token(redated, launches=launches, lake_token="rev-1") != base
        assert selection_confirm_token(sel, launches=[("BSE", "INDEX")], lake_token="rev-1") != base
        assert selection_confirm_token(sel, launches=launches, lake_token="rev-2") != base
        assert selection_confirm_token(sel, launches=launches, lake_token=None) != base
