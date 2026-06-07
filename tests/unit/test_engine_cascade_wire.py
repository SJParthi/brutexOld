"""Unit tests for the Stage-4 cascade wiring (DEC-CASCADE-WIRE-001).

Strict 100% line + branch coverage of :mod:`brutex.engine.cascade_wire` — the
adapter that re-builds the cascade's ``CandidateInput``\\s from a finished sweep
and runs the nine-filter cascade per ``(instrument, tf)`` pool. Covers:

* the manifest data model + every validation branch (``CascadeTupleSpec``,
  ``BarsContext``);
* deterministic config / manifest / report (de)serialisation + every error path;
* ``CandidateInput`` reconstruction (bull + bear) over hand-built bars, the
  per-bar return series, the drawdown helper, and the Monte-Carlo exit closure;
* the shared core ``run_cascade_over_specs`` (group runs / no-bars skip / empty
  candidates skip) + the end-to-end auto/manual write paths;
* the **byte-identity** guarantee — the AUTOMATIC path and the MANUAL replay
  produce the same report bytes.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import numpy as np
import pytest
from brutex.data.models import CandleBar
from brutex.engine import cascade_wire as cw
from brutex.engine.results_store import ResultRow
from brutex.metrics.cascade import (
    CandidateVerdict,
    CascadeReport,
    FilterStage,
)

_IST = timezone(timedelta(hours=5, minutes=30))


def _rid(seed: int) -> str:
    """Return a valid 64-char lowercase-hex run-identity from *seed*."""
    return f"{seed:064x}"


def _bar(minute: int, o: str, h: str, lo: str, c: str) -> CandleBar:
    """Build one IST-session CandleBar (stored UTC) with Decimal prices."""
    ts = datetime(2026, 5, 13, 9, minute, tzinfo=_IST).astimezone(UTC)
    return CandleBar(
        timestamp=ts,
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(lo),
        close=Decimal(c),
        volume=100,
        open_interest=None,
    )


def _session_bars() -> list[CandleBar]:
    """A small rising-then-falling intraday session (6 one-minute bars)."""
    return [
        _bar(16, "100.00", "101.00", "99.50", "100.50"),
        _bar(17, "100.50", "102.00", "100.00", "101.50"),
        _bar(18, "101.50", "101.80", "100.20", "100.40"),
        _bar(19, "100.40", "100.90", "99.80", "100.10"),
        _bar(20, "100.10", "100.30", "99.00", "99.20"),
        _bar(21, "99.20", "99.80", "98.50", "98.70"),
    ]


def _spec(**overrides: object) -> cw.CascadeTupleSpec:
    """Build a valid one-winner :class:`CascadeTupleSpec`, overridable by keyword."""
    base: dict[str, object] = {
        "instrument": "NIFTY",
        "tf_label": "1minute",
        "tf_minutes": 1,
        "direction_label": "bull",
        "sl_paisa": None,
        "tp_paisa": None,
        "prior_n": 3,
        "proximity_bps": None,
        "start_equity_paisa": 10_000_000,
        "winners": ((_rid(1), 0),),
    }
    base.update(overrides)
    return cw.CascadeTupleSpec(**base)  # type: ignore[arg-type]


def _bars_for(bars: list[CandleBar]) -> Callable[[str, str], list[CandleBar]]:
    """Return a ``bars_for`` that serves *bars* for NIFTY/1minute, else empty."""

    def loader(instrument: str, tf_label: str) -> list[CandleBar]:
        if (instrument, tf_label) == ("NIFTY", "1minute"):
            return bars
        return []

    return loader


# ── CascadeTupleSpec validation ──────────────────────────────────────────────


class TestCascadeTupleSpec:
    def test_valid_spec_round_trips(self) -> None:
        spec = _spec()
        assert spec.instrument == "NIFTY"
        assert spec.winners == ((_rid(1), 0),)

    @pytest.mark.parametrize("field", ["instrument", "tf_label", "direction_label"])
    def test_empty_label_raises(self, field: str) -> None:
        with pytest.raises(cw.CascadeWireError, match="must be non-empty"):
            _spec(**{field: ""})

    def test_bad_direction_raises(self) -> None:
        with pytest.raises(cw.CascadeWireError, match="bull.*bear"):
            _spec(direction_label="HOLD")

    def test_tf_minutes_below_one_raises(self) -> None:
        with pytest.raises(cw.CascadeWireError, match="tf_minutes must be >= 1"):
            _spec(tf_minutes=0)

    @pytest.mark.parametrize("leg", ["sl_paisa", "tp_paisa"])
    def test_non_positive_leg_raises(self, leg: str) -> None:
        with pytest.raises(cw.CascadeWireError, match="must be > 0 or None"):
            _spec(**{leg: 0})

    def test_legs_accept_none_and_positive(self) -> None:
        spec = _spec(sl_paisa=50, tp_paisa=100)
        assert spec.sl_paisa == 50
        assert spec.tp_paisa == 100

    def test_prior_n_below_one_raises(self) -> None:
        with pytest.raises(cw.CascadeWireError, match="prior_n must be >= 1"):
            _spec(prior_n=0)

    def test_non_positive_equity_raises(self) -> None:
        with pytest.raises(cw.CascadeWireError, match="start_equity_paisa must be > 0"):
            _spec(start_equity_paisa=0)

    def test_bad_proximity_length_raises(self) -> None:
        with pytest.raises(cw.CascadeWireError, match="proximity_bps must be a 3-tuple"):
            _spec(proximity_bps=(10, 20))

    def test_valid_proximity_round_trips(self) -> None:
        spec = _spec(proximity_bps=(10, 20, 30))
        assert spec.proximity_bps == (10, 20, 30)

    @pytest.mark.parametrize("bad", ["abc", "A" * 64, "f" * 63])
    def test_malformed_run_id_raises(self, bad: str) -> None:
        with pytest.raises(cw.CascadeWireError, match="lowercase hex"):
            _spec(winners=((bad, 0),))

    @pytest.mark.parametrize("mask", [-1, (1 << 52)])
    def test_out_of_range_mask_raises(self, mask: int) -> None:
        with pytest.raises(cw.CascadeWireError, match="out of range"):
            _spec(winners=((_rid(1), mask),))

    def test_empty_winners_is_valid(self) -> None:
        assert _spec(winners=()).winners == ()


class TestBarsContext:
    def test_valid_context(self) -> None:
        ctx = cw.BarsContext(exchange="NSE", segment="INDEX", start="2021-01-01", end="2021-02-01")
        assert ctx.exchange == "NSE"

    @pytest.mark.parametrize("field", ["exchange", "segment", "start", "end"])
    def test_empty_field_raises(self, field: str) -> None:
        kwargs = {"exchange": "NSE", "segment": "INDEX", "start": "a", "end": "b"}
        kwargs[field] = ""
        with pytest.raises(cw.CascadeWireError, match="must be non-empty"):
            cw.BarsContext(**kwargs)  # type: ignore[arg-type]


# ── Cascade config helpers ────────────────────────────────────────────────────


class TestCascadeConfig:
    def test_seed_is_deterministic_from_sweep_id(self) -> None:
        cfg_a = cw.cascade_config_for_sweep("deadbeef" * 8)
        cfg_b = cw.cascade_config_for_sweep("deadbeef" * 8)
        assert cfg_a.seed == cfg_b.seed
        assert 0 <= cfg_a.seed < 2**31

    def test_overrides_applied(self) -> None:
        cfg = cw.cascade_config_for_sweep("a" * 64, overrides={"dsr_min": 0.5})
        assert cfg.dsr_min == 0.5

    def test_empty_sweep_id_raises(self) -> None:
        with pytest.raises(cw.CascadeWireError, match="must be non-empty"):
            cw.cascade_config_for_sweep("")

    def test_non_hex_sweep_id_raises(self) -> None:
        with pytest.raises(cw.CascadeWireError, match="not hex"):
            cw.cascade_config_for_sweep("zzzzzzzzzzzzzzzz")

    def test_config_dict_round_trips(self) -> None:
        cfg = cw.cascade_config_for_sweep("a" * 64)
        restored = cw.cascade_config_from_dict(cw.cascade_config_to_dict(cfg))
        assert restored == cfg

    def test_config_from_bad_dict_raises(self) -> None:
        # An unknown field is a structural error → TypeError → CascadeWireError.
        with pytest.raises(cw.CascadeWireError, match="invalid cascade_config"):
            cw.cascade_config_from_dict({"seed": 1, "not_a_real_field": 7})


# ── Pure numeric helpers ──────────────────────────────────────────────────────


class TestNumericHelpers:
    def test_finite_passes_real_value(self) -> None:
        assert cw._finite(1.5) == 1.5

    def test_finite_maps_none_to_zero(self) -> None:
        assert cw._finite(None) == 0.0

    @pytest.mark.parametrize("bad", [float("inf"), float("-inf"), float("nan")])
    def test_finite_maps_non_finite_to_zero(self, bad: float) -> None:
        assert cw._finite(bad) == 0.0

    def test_per_bar_returns(self) -> None:
        out = cw._per_bar_returns((1_100, 1_100, 900), start_equity_paisa=1_000)
        assert out == (0.1, 0.0, -0.2)

    def test_max_drawdown_empty_is_zero(self) -> None:
        assert cw._max_drawdown_return_units(()) == 0.0

    def test_max_drawdown_monotonic_is_zero(self) -> None:
        assert cw._max_drawdown_return_units((0.1, 0.2, 0.1)) == 0.0

    def test_max_drawdown_picks_largest_decline(self) -> None:
        # cumulative: 0.5, 0.3, 0.6, 0.1 → peak 0.6, trough 0.1 → dd 0.5.
        assert cw._max_drawdown_return_units((0.5, -0.2, 0.3, -0.5)) == pytest.approx(0.5)


# ── CandidateInput reconstruction ─────────────────────────────────────────────


class TestBuildCandidateInput:
    def _bar_bits(self, bars: list[CandleBar], spec: cw.CascadeTupleSpec) -> list[int]:
        from brutex.engine.stage1a import build_bar_bits

        return build_bar_bits(bars, proximity=None, prior_n=spec.prior_n)

    def test_reconstructs_bull_candidate(self) -> None:
        bars = _session_bars()
        spec = _spec()
        cand = cw.candidates_for_spec(spec, bars)[0]
        assert cand.candidate_id == _rid(1)
        # Per-bar return series is the equal-length pool axis (one per bar).
        assert len(cand.returns) == len(bars)
        assert len(cand.lockbox_days) == len(bars)
        assert cand.mc_n_bars == len(bars)
        # Every scalar metric is finite (the CandidateInput constructor enforces it).
        assert np.isfinite(cand.sharpe_ratio)
        assert np.isfinite(cand.training_sharpe)
        assert cand.training_max_drawdown >= 0.0

    def test_reconstructs_bear_candidate(self) -> None:
        bars = _session_bars()
        cand = cw.candidates_for_spec(_spec(direction_label="bear"), bars)[0]
        assert cand.candidate_id == _rid(1)
        assert cand.mc_n_bars == len(bars)

    def test_candidates_for_spec_with_proximity(self) -> None:
        bars = _session_bars()
        cands = cw.candidates_for_spec(_spec(proximity_bps=(10, 10, 10)), bars)
        assert len(cands) == 1

    def test_candidates_for_spec_empty_winners(self) -> None:
        assert cw.candidates_for_spec(_spec(winners=()), _session_bars()) == []

    def test_mc_evaluate_returns_finite_or_nan(self) -> None:
        bars = _session_bars()
        cand = cw.candidates_for_spec(_spec(sl_paisa=50, tp_paisa=50), bars)[0]
        # Many entries → at least one trade closes → a (possibly defined) Sharpe.
        many = cand.mc_evaluate(np.array([0, 1, 2, 3], dtype=np.intp))
        assert isinstance(many, float)
        # A single entry → at most one trade → undefined per-trade Sharpe → nan.
        one = cand.mc_evaluate(np.array([0], dtype=np.intp))
        assert np.isnan(one)


# ── The shared pure core ──────────────────────────────────────────────────────


class TestRunCascadeOverSpecs:
    def test_runs_per_pool(self) -> None:
        bars = _session_bars()
        report = cw.run_cascade_over_specs(
            [_spec()],
            bars_for=_bars_for(bars),
            config=cw.cascade_config_for_sweep("a" * 64),
            sweep_id="a" * 64,
        )
        assert report.sweep_id == "a" * 64
        assert len(report.groups) == 1
        instrument, tf_label, _ = report.groups[0]
        assert (instrument, tf_label) == ("NIFTY", "1minute")
        assert report.n_input == 1
        # One trade << the 30-trade hard gate ⇒ rejected, never crashes.
        assert report.n_survived == 0

    def test_skips_pool_with_no_bars(self) -> None:
        report = cw.run_cascade_over_specs(
            [_spec()],
            bars_for=lambda _i, _t: [],
            config=cw.cascade_config_for_sweep("a" * 64),
            sweep_id="a" * 64,
        )
        assert report.groups == ()
        assert report.n_input == 0
        assert report.survivor_ids == ()

    def test_skips_pool_with_no_candidates(self) -> None:
        report = cw.run_cascade_over_specs(
            [_spec(winners=())],
            bars_for=_bars_for(_session_bars()),
            config=cw.cascade_config_for_sweep("a" * 64),
            sweep_id="a" * 64,
        )
        assert report.groups == ()


# ── Report serialisation ──────────────────────────────────────────────────────


def _manual_report() -> cw.CascadeWireReport:
    """A hand-built report carrying BOTH a survivor and a rejected verdict."""
    inner = CascadeReport(
        verdicts=(
            CandidateVerdict(
                candidate_id=_rid(1),
                passed=True,
                rejected_at=None,
                detail="survived all Stage-4 filters",
                composite_score=0.75,
            ),
            CandidateVerdict(
                candidate_id=_rid(2),
                passed=False,
                rejected_at=FilterStage.HARD_GATES,
                detail="total_trades 1 < 30",
                composite_score=None,
            ),
        ),
        survivor_ids=(_rid(1),),
        n_input=2,
        n_survived=1,
        stage_survivors={FilterStage.HARD_GATES.value: 1},
    )
    return cw.CascadeWireReport(sweep_id="a" * 64, groups=(("NIFTY", "1minute", inner),))


class TestReportSerialisation:
    def test_wire_report_to_dict_both_verdict_kinds(self) -> None:
        payload = cw.wire_report_to_dict(_manual_report())
        assert payload["version"] == cw.REPORT_VERSION
        assert payload["n_survived"] == 1
        verdicts = payload["groups"][0]["report"]["verdicts"]  # type: ignore[index]
        assert verdicts[0]["rejected_at"] is None
        assert verdicts[1]["rejected_at"] == FilterStage.HARD_GATES.value

    def test_serialize_is_deterministic(self) -> None:
        report = _manual_report()
        assert cw.serialize_wire_report(report) == cw.serialize_wire_report(report)

    def test_report_properties(self) -> None:
        report = _manual_report()
        assert report.n_input == 2
        assert report.n_survived == 1
        assert report.survivor_ids == (_rid(1),)


# ── Manifest serialisation ────────────────────────────────────────────────────


def _manifest(specs: tuple[cw.CascadeTupleSpec, ...] | None = None) -> cw.CascadeManifest:
    return cw.CascadeManifest(
        version=cw.MANIFEST_VERSION,
        sweep_id="a" * 64,
        cascade_config=cw.cascade_config_for_sweep("a" * 64),
        bars_context=cw.BarsContext(
            exchange="NSE", segment="INDEX", start="2021-01-01", end="2021-02-01"
        ),
        specs=(_spec(),) if specs is None else specs,
    )


class TestManifestSerialisation:
    def test_round_trip_preserves_everything(self) -> None:
        # Two specs exercise BOTH legs of every nullable field: the first has
        # sl/tp/proximity all None, the second has them all set.
        manifest = _manifest(
            specs=(_spec(), _spec(proximity_bps=(10, 20, 30), sl_paisa=50, tp_paisa=100))
        )
        restored = cw.manifest_from_json(cw.manifest_to_json(manifest))
        assert restored == manifest

    def test_round_trip_is_byte_stable(self) -> None:
        manifest = _manifest()
        once = cw.manifest_to_json(manifest)
        twice = cw.manifest_to_json(cw.manifest_from_json(once))
        assert once == twice

    def test_bad_json_raises(self) -> None:
        with pytest.raises(cw.CascadeWireError, match="not valid JSON"):
            cw.manifest_from_json(b"{not json")

    def test_non_object_json_raises(self) -> None:
        with pytest.raises(cw.CascadeWireError, match="must be a JSON object"):
            cw.manifest_from_json(b"[1, 2, 3]")

    def test_version_mismatch_raises(self) -> None:
        data = cw.manifest_to_json(_manifest())
        bumped = data.replace(b'"version":1', b'"version":99')
        with pytest.raises(cw.CascadeWireError, match="unsupported cascade manifest version"):
            cw.manifest_from_json(bumped)

    def test_missing_field_raises(self) -> None:
        import json

        payload = json.loads(cw.manifest_to_json(_manifest()))
        del payload["bars_context"]
        with pytest.raises(cw.CascadeWireError, match="missing/invalid field"):
            cw.manifest_from_json(json.dumps(payload).encode("utf-8"))

    def test_bad_spec_in_manifest_raises(self) -> None:
        import json

        payload = json.loads(cw.manifest_to_json(_manifest()))
        del payload["specs"][0]["tf_minutes"]
        with pytest.raises(cw.CascadeWireError, match="invalid cascade tuple spec"):
            cw.manifest_from_json(json.dumps(payload).encode("utf-8"))


# ── Spec building from persisted rows ─────────────────────────────────────────


def _result_row(seed: int, *, mask: int, sharpe: float) -> ResultRow:
    return ResultRow(
        run_id=_rid(seed),
        instrument="NIFTY",
        tf="1minute",
        direction="bull",
        mode="A",
        mask_bits=mask,
        net_pnl_paisa=1_000,
        gross_profit_paisa=1_500,
        gross_loss_paisa=500,
        max_drawdown_paisa=250,
        expectancy_paisa=25,
        total_trades=40,
        winning_trades=25,
        losing_trades=15,
        sharpe_ratio=sharpe,
        sortino_ratio=2.0,
        calmar_ratio=1.25,
        profit_factor=1.8,
        win_rate=0.625,
        max_drawdown_pct=12.5,
        avg_entry_price=Decimal("14020.50"),
        avg_exit_price=Decimal("14100.25"),
    )


class TestSpecFromStoreRows:
    def test_empty_rows_returns_none(self) -> None:
        assert (
            cw.spec_from_store_rows(
                instrument="NIFTY",
                tf_label="1minute",
                tf_minutes=1,
                direction_label="bull",
                sl_paisa=None,
                tp_paisa=None,
                prior_n=3,
                proximity_bps=None,
                start_equity_paisa=10_000_000,
                rows=[],
            )
            is None
        )

    def test_selects_all_when_top_k_none(self) -> None:
        rows = [_result_row(1, mask=1, sharpe=0.5), _result_row(2, mask=2, sharpe=1.5)]
        spec = cw.spec_from_store_rows(
            instrument="NIFTY",
            tf_label="1minute",
            tf_minutes=1,
            direction_label="bull",
            sl_paisa=None,
            tp_paisa=None,
            prior_n=3,
            proximity_bps=None,
            start_equity_paisa=10_000_000,
            rows=rows,
        )
        assert spec is not None
        # Sorted by Sharpe descending — the 1.5 winner ranks first.
        assert spec.winners == ((_rid(2), 2), (_rid(1), 1))

    def test_top_k_truncates_by_sharpe(self) -> None:
        rows = [
            _result_row(1, mask=1, sharpe=0.5),
            _result_row(2, mask=2, sharpe=1.5),
            _result_row(3, mask=3, sharpe=1.0),
        ]
        spec = cw.spec_from_store_rows(
            instrument="NIFTY",
            tf_label="1minute",
            tf_minutes=1,
            direction_label="bull",
            sl_paisa=None,
            tp_paisa=None,
            prior_n=3,
            proximity_bps=None,
            start_equity_paisa=10_000_000,
            rows=rows,
            top_k=2,
        )
        assert spec is not None
        assert spec.winners == ((_rid(2), 2), (_rid(3), 3))


# ── Output paths + IO + the end-to-end paths ──────────────────────────────────


class TestEndToEnd:
    def test_paths(self, tmp_path: Path) -> None:
        assert cw.manifest_path(tmp_path) == tmp_path / "cascade" / "manifest.json"
        assert cw.report_path(tmp_path) == tmp_path / "cascade" / "cascade-report.json"

    def test_load_manifest_missing_raises(self, tmp_path: Path) -> None:
        with pytest.raises(cw.CascadeWireError, match="no cascade manifest"):
            cw.load_manifest(tmp_path)

    def test_write_then_load_manifest_round_trips(self, tmp_path: Path) -> None:
        manifest = _manifest()
        cw.write_manifest(tmp_path, manifest)
        assert cw.load_manifest(tmp_path) == manifest

    def test_write_report(self, tmp_path: Path) -> None:
        path = cw.write_report(tmp_path, _manual_report())
        assert path.exists()
        assert path.read_bytes() == cw.serialize_wire_report(_manual_report())

    def test_auto_path_writes_manifest_and_report(self, tmp_path: Path) -> None:
        bars = _session_bars()
        report = cw.run_and_write_from_specs(
            tmp_path,
            sweep_id="a" * 64,
            specs=[_spec()],
            bars_for=_bars_for(bars),
            bars_context=cw.BarsContext(
                exchange="NSE", segment="INDEX", start="2021-01-01", end="2021-02-01"
            ),
            cascade_config=cw.cascade_config_for_sweep("a" * 64),
        )
        assert cw.manifest_path(tmp_path).exists()
        assert cw.report_path(tmp_path).exists()
        assert report.sweep_id == "a" * 64

    def test_auto_equals_manual_byte_for_byte(self, tmp_path: Path) -> None:
        """The golden contract: AUTOMATIC report bytes == MANUAL replay bytes."""
        bars = _session_bars()
        bars_for = _bars_for(bars)
        specs = [_spec(), _spec(winners=((_rid(2), 0),), direction_label="bear")]
        auto = cw.run_and_write_from_specs(
            tmp_path,
            sweep_id="a" * 64,
            specs=specs,
            bars_for=bars_for,
            bars_context=cw.BarsContext(
                exchange="NSE", segment="INDEX", start="2021-01-01", end="2021-02-01"
            ),
            cascade_config=cw.cascade_config_for_sweep("a" * 64),
        )
        auto_bytes = cw.report_path(tmp_path).read_bytes()

        manual = cw.run_and_write_from_manifest(tmp_path, bars_for=bars_for)
        manual_bytes = cw.report_path(tmp_path).read_bytes()

        assert manual_bytes == auto_bytes
        assert cw.serialize_wire_report(manual) == cw.serialize_wire_report(auto)
