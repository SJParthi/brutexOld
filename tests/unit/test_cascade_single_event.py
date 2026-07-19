"""Unit tests for the DEFAULT-OFF single-event sensitivity cascade gate.

DEC-SWEEP-SINGLE-EVENT-SENSITIVITY-GATE-001: FAIL a candidate iff
``total <= 0`` OR ``total - best_single_IST_day < (X/100) * total`` (strict
``<`` — exactly X% remaining passes). Default OFF (``None``) — the stage is
fully absent from behavior. The fluke/legit shapes are ported from the C2
measurement repro (tests/scratch/single_event_gate_repro.py — a REBUILD, not
the original scripts; SEED frozen).
"""

from __future__ import annotations

import math
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import numpy.typing as npt
import pytest
from brutex.engine.cascade_wire import cascade_config_from_dict, cascade_config_to_dict
from brutex.engine.session_lens import ist_trade_date
from brutex.metrics import cascade as cascade_mod
from brutex.metrics.cascade import (
    CandidateInput,
    CascadeConfig,
    FilterStage,
    _single_event_verdict,
    run_stage4_cascade,
    validate_single_event_pct,
)

# pyright: reportPrivateUsage=none

SEED = 20260719
BARS_PER_DAY = 75
START_EQUITY_PAISA = 10_000_000


def _null_exit(positions: npt.NDArray[np.intp]) -> float:
    return 0.0


def _trading_days(n: int) -> list[datetime]:
    days: list[datetime] = []
    d = datetime(2024, 1, 1, 10, 0, tzinfo=UTC)
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


def _fluke_trades() -> list[tuple[datetime, int]]:
    """30 trades / 30 days; ONE +100,000-paisa windfall; 29 scratches (net +264)."""
    rng = random.Random(SEED)
    days = _trading_days(30)
    pnls: list[int] = []
    for i in range(29):
        pnls.append(rng.randint(90, 170) if i % 2 == 0 else -rng.randint(90, 170))
    pnls.insert(14, 100_000)
    return list(zip(days, pnls, strict=True))


def _steady_trades() -> list[tuple[datetime, int]]:
    rng = random.Random(SEED + 1)
    out = []
    for d in _trading_days(120):
        win = rng.random() < 0.60
        out.append((d, rng.randint(300, 900) if win else -rng.randint(250, 700)))
    return out


def _modest_trades() -> list[tuple[datetime, int]]:
    rng = random.Random(SEED + 2)
    out = []
    for d in _trading_days(120):
        for k in range(2):
            win = rng.random() < 0.52
            pnl = rng.randint(400, 800) if win else -rng.randint(320, 640)
            out.append((d + timedelta(minutes=30 * k), pnl))
    return out


def _lumpy_trades() -> list[tuple[datetime, int]]:
    rng = random.Random(SEED + 3)
    out = []
    for d in _trading_days(60):
        win = rng.random() < 0.55
        out.append((d, rng.randint(500, 1500) if win else -rng.randint(400, 1100)))
    out[20] = (out[20][0], 4_500)
    return out


def _per_bar(trades: list[tuple[datetime, int]]) -> tuple[tuple[float, ...], tuple[int, ...]]:
    """The repro's CandidateInput shape: per-bar returns + per-bar IST day ordinals."""
    days = sorted({ist_trade_date(ts) for ts, _ in trades})
    day_index = {d: i for i, d in enumerate(days)}
    pnl_by_bar: dict[int, int] = {}
    per_day_count: dict[int, int] = {}
    for ts, pnl in trades:
        di = day_index[ist_trade_date(ts)]
        slot = per_day_count.get(di, 0)
        per_day_count[di] = slot + 1
        bar = di * BARS_PER_DAY + 30 + slot * 7
        pnl_by_bar[bar] = pnl_by_bar.get(bar, 0) + pnl
    n_bars = len(days) * BARS_PER_DAY
    equity = float(START_EQUITY_PAISA)
    rets: list[float] = []
    ords: list[int] = []
    for b in range(n_bars):
        pnl = pnl_by_bar.get(b, 0)
        rets.append(pnl / equity)
        equity += pnl
        ords.append(b // BARS_PER_DAY)
    return tuple(rets), tuple(ords)


def _candidate(
    cid: str,
    returns: tuple[float, ...],
    day_ords: tuple[int, ...],
) -> CandidateInput:
    """A hand-built candidate whose day ordinals align with ``returns`` (the
    production ``build_candidate_input`` shape) and whose scalars clear the
    hard gates, so a rejection can only come from the single-event stage."""
    return CandidateInput(
        candidate_id=cid,
        sharpe_ratio=2.0,
        sortino_ratio=2.4,
        calmar_ratio=1.6,
        profit_factor=1.8,
        win_rate=0.55,
        win_loss_ratio=2.5,
        recovery_factor=2.1,
        max_drawdown_pct=12.0,
        max_consecutive_losses=4,
        total_trades=600,
        training_sharpe=0.1,
        training_max_drawdown=5.0,
        skew=0.0,
        kurtosis=3.0,
        returns=returns,
        lockbox_returns=returns,
        lockbox_days=day_ords,
        mc_n_bars=400,
        mc_n_entries=20,
        mc_evaluate=_null_exit,
    )


def _simple(cid: str, day_sums: tuple[float, ...]) -> CandidateInput:
    """One bar per day — the day sums ARE the returns (law-boundary shapes)."""
    return _candidate(cid, day_sums, tuple(range(len(day_sums))))


# ── the LAW (measured shapes) ─────────────────────────────────────────────────


def test_fluke_fails_at_default_5pct() -> None:
    """The measured fluke (remain 0.3% of total) FAILS at X=5.0 with a full detail."""
    rets, ords = _per_bar(_fluke_trades())
    detail = _single_event_verdict(_candidate("fluke", rets, ords), 5.0)
    assert detail is not None
    # The detail names total, best day, the remaining percent and X.
    assert "total" in detail and "best day" in detail
    assert "% of total" in detail and "5.0% floor" in detail


@pytest.mark.parametrize("shape", [_steady_trades, _modest_trades, _lumpy_trades])
def test_legit_comparators_pass_at_5pct(shape) -> None:  # type: ignore[no-untyped-def]
    """All three measured legit comparators (worst remain% = 76.7) PASS at X=5.0."""
    rets, ords = _per_bar(shape())
    assert _single_event_verdict(_candidate("legit", rets, ords), 5.0) is None


def test_boundary_exactly_x_percent_passes() -> None:
    """remaining == exactly X% of total → strict ``<`` does NOT fail (pinned)."""
    # total = 100.0, best day = 95.0 → remaining = 5.0 == (5.0/100)*100.0.
    assert _single_event_verdict(_simple("edge", (95.0, 5.0)), 5.0) is None


def test_boundary_one_unit_under_fails() -> None:
    """remaining one notch under X% of total → FAIL."""
    detail = _single_event_verdict(_simple("under", (95.0, 4.99)), 5.0)
    assert detail is not None and "% of total" in detail


def test_total_nonpositive_arm_fails() -> None:
    """total <= 0 FAILS with its own reason (negative AND exactly-zero totals)."""
    neg = _single_event_verdict(_simple("neg", (-1.0, 0.5)), 5.0)
    assert neg is not None and "<= 0" in neg
    zero = _single_event_verdict(_simple("zero", (1.0, -1.0)), 5.0)
    assert zero is not None and "<= 0" in zero


def test_misaligned_day_ordinals_reject_honestly() -> None:
    """Day ordinals not 1:1 with returns → an honest evaluation-failure reject."""
    c = _candidate("mis", (1.0, 2.0, 3.0), (0, 1, 2))
    mis = CandidateInput(
        **{
            **{f: getattr(c, f) for f in c.__dataclass_fields__},  # type: ignore[arg-type]
            "lockbox_days": (0, 1),
        }
    )
    detail = _single_event_verdict(mis, 5.0)
    assert detail is not None and "could not be evaluated" in detail


# ── the stage wiring (run_stage4_cascade) ─────────────────────────────────────


def test_stage_rejects_fluke_in_cascade_when_on() -> None:
    rets, ords = _per_bar(_fluke_trades())
    cfg = CascadeConfig(seed=7, single_event_max_best_day_pct=5.0)
    rep = run_stage4_cascade([_candidate("fluke", rets, ords)], cfg)
    (verdict,) = rep.verdicts
    assert verdict.passed is False
    assert verdict.rejected_at is FilterStage.SINGLE_EVENT_SENSITIVITY
    assert rep.stage_survivors[FilterStage.SINGLE_EVENT_SENSITIVITY.value] == 0


def test_stage_keeps_legit_candidate_when_on() -> None:
    """A legit candidate SURVIVES the stage when ON (the keep branch; later
    stages may still reject it — never at the single-event stage)."""
    rets, ords = _per_bar(_steady_trades())
    cfg = CascadeConfig(seed=7, single_event_max_best_day_pct=5.0)
    rep = run_stage4_cascade([_candidate("legit", rets, ords)], cfg)
    assert rep.stage_survivors[FilterStage.SINGLE_EVENT_SENSITIVITY.value] == 1
    (verdict,) = rep.verdicts
    assert verdict.rejected_at is not FilterStage.SINGLE_EVENT_SENSITIVITY


def test_off_arm_stage_fully_absent() -> None:
    """knob None (default) → the verdict helper is NEVER invoked and the stage
    token is absent from stage_survivors (behavior identical to pre-change)."""
    rets, ords = _per_bar(_fluke_trades())

    def _bomb(c: CandidateInput, x: float) -> str | None:
        raise AssertionError("single-event verdict must not run when OFF")

    orig = cascade_mod._single_event_verdict
    cascade_mod._single_event_verdict = _bomb  # type: ignore[assignment]
    try:
        cfg = CascadeConfig(seed=7)
        assert cfg.single_event_max_best_day_pct is None
        rep = run_stage4_cascade([_candidate("fluke", rets, ords)], cfg)
    finally:
        cascade_mod._single_event_verdict = orig  # type: ignore[assignment]
    assert FilterStage.SINGLE_EVENT_SENSITIVITY.value not in rep.stage_survivors
    (verdict,) = rep.verdicts
    assert verdict.rejected_at is not FilterStage.SINGLE_EVENT_SENSITIVITY


# ── config validation + manifest round-trip ───────────────────────────────────


@pytest.mark.parametrize(
    "junk", [-1.0, 0.0, 100.0, 150.0, float("inf"), float("-inf"), float("nan"), True]
)
def test_junk_config_refused_loudly(junk: object) -> None:
    with pytest.raises(ValueError, match="single_event_max_best_day_pct"):
        CascadeConfig(seed=7, single_event_max_best_day_pct=junk)  # type: ignore[arg-type]


def test_valid_config_values_accepted() -> None:
    assert (
        CascadeConfig(seed=7, single_event_max_best_day_pct=5.0).single_event_max_best_day_pct
        == 5.0
    )
    assert CascadeConfig(seed=7).single_event_max_best_day_pct is None


def test_old_manifest_without_key_loads_off() -> None:
    """An OLD manifest dict WITHOUT the key loads as None/OFF (additive field,
    REPORT_VERSION unchanged — the pool_pbo precedent)."""
    data = cascade_config_to_dict(CascadeConfig(seed=7))
    assert data["single_event_max_best_day_pct"] is None
    del data["single_event_max_best_day_pct"]  # simulate a pre-change manifest
    cfg = cascade_config_from_dict(data)
    assert cfg.single_event_max_best_day_pct is None


def test_manifest_round_trip_preserves_knob() -> None:
    cfg = CascadeConfig(seed=7, single_event_max_best_day_pct=5.0)
    rt = cascade_config_from_dict(cascade_config_to_dict(cfg))
    assert rt.single_event_max_best_day_pct == 5.0


def test_measured_fluke_remain_pct_matches_c2() -> None:
    """The ported fluke shape reproduces the C2 table: total 100,264 paisa,
    best day 100,000, remain 0.3% — 16x below the 5.0 default."""
    rets, ords = _per_bar(_fluke_trades())
    total = math.fsum(rets)
    day_sums: dict[int, float] = {}
    for d, r in zip(ords, rets, strict=True):
        day_sums[d] = day_sums.get(d, 0.0) + r
    remain_pct = (total - max(day_sums.values())) / total * 100.0
    assert remain_pct == pytest.approx(0.3, abs=0.1)


class TestValidateSingleEventPct:
    """The ONE eager-validation law (review round 1) — pure, shared with the CLI."""

    def test_none_passes_through(self) -> None:
        assert validate_single_event_pct(None) is None

    def test_valid_value_passes_through(self) -> None:
        assert validate_single_event_pct(5.0) == 5.0

    @pytest.mark.parametrize(
        "junk", [0.0, 100.0, -5.0, float("nan"), float("inf"), float("-inf"), True]
    )
    def test_junk_ladder_refused_naming_the_flag(self, junk: object) -> None:
        with pytest.raises(ValueError, match="--single-event-max-best-day-pct"):
            validate_single_event_pct(junk)  # type: ignore[arg-type]

    def test_post_init_delegates_to_the_one_law(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: list[object] = []
        real = cascade_mod.validate_single_event_pct

        def _spy(value: float | None) -> float | None:
            seen.append(value)
            return real(value)

        monkeypatch.setattr(cascade_mod, "validate_single_event_pct", _spy)
        CascadeConfig(seed=7, single_event_max_best_day_pct=7.5)
        assert seen == [7.5]


class TestCliEagerValidation:
    """cmd_sweep_all refuses a junk knob BEFORE any compute (review round 1 MAJOR)."""

    def test_junk_knob_clean_early_exit_before_compute(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import brutex.engine.results_depth_pass as rdp
        from brutex import cli as cli_mod

        def _bomb() -> bool:
            raise AssertionError("sweep body reached — eager validation failed")

        # The results-depth resolve is the very next eager step after the new
        # validation block (and the lake preload/grid sit far beyond it) — the
        # bomb proves the clean exit happens BEFORE any compute.
        monkeypatch.setattr(rdp, "resolve_results_depth_enabled", _bomb)
        lake = tmp_path / "lake"
        lake.mkdir()
        rc = cli_mod.cmd_sweep_all(
            lake_root=lake,
            catalog_path=None,
            results_root=tmp_path / "sweeps",
            exchange="NSE",
            segment="INDEX",
            symbols="NIFTY",
            intervals="5minute",
            start="2024-01-02",
            end="2024-01-03",
            directions="BUY",
            k="1",
            conditions="0,1",
            sl="none",
            tp="none",
            top=5,
            mode="A",
            max_tuples=None,
            max_combos=None,
            data_snapshot_hash=None,
            single_event_max_best_day_pct=0.0,
            options_sim=False,
        )
        assert rc == cli_mod.EXIT_USER_ERROR
        err = capsys.readouterr().err
        assert "sweep-all config error" in err
        assert "--single-event-max-best-day-pct" in err
