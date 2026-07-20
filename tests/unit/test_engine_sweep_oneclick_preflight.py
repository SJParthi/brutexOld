"""100% line + branch coverage of :mod:`brutex.engine.sweep_oneclick_preflight`.

Every R4/R20/gap-hunt-ruling arm is exercised: edge clamps (pre-2020 floor,
future-to-date, coverage-ceiling, the degenerate fully-clamped range),
all-holiday zero-session refusal, the sweep-vs-pull exclusion probe, 1-minute
DISK-verified coverage (both the refuse-whole and the allow_missing_slices
flag-and-run arms), lake-invariant findings, G1 derived-freshness reuse,
futures-coverage (P5's call), the India-VIX arm, free-disk, VWAP dead-bits
flag-and-run, and every SLA decision (allowed/acknowledged/refused).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from brutex.adapters.groww.pull_singleflight import pull_singleflight_lock
from brutex.data.lake_footprint import DiskGuardDecision
from brutex.data.partition import build_partition_path
from brutex.engine import sweep_oneclick_preflight as mod
from brutex.engine.sweep_estimator import PROVISIONAL_BASIS
from brutex.engine.sweep_oneclick_preflight import (
    Clamp,
    Refusal,
    WindowClamp,
    clamp_sweep_window,
    missing_1m_partitions,
    run_oneclick_preflight,
)
from brutex.futures.preflight import FuturesPreflightReport

_TODAY = date(2026, 7, 8)


def _touch_partition(
    lake_root: Path,
    *,
    exchange: str,
    segment: str,
    symbol: str,
    interval: str,
    year: int,
    month: int,
) -> None:
    path = build_partition_path(
        lake_root=lake_root,
        exchange=exchange,
        segment=segment,
        symbol=symbol,
        interval=interval,
        year=year,
        month=month,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")


def _base_kwargs(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "exchange": "NSE",
        "segment": "INDEX",
        "symbols": ["NIFTY"],
        "intervals": ["1minute"],
        "start": date(2026, 1, 5),
        "end": date(2026, 1, 9),
        "today": _TODAY,
        "k_values": [1],
        "other_axes_product": 1,
        "tuple_count": 1,
        "top": 10,
        "allow_missing_slices": False,
        "acknowledge_sla_overrun": False,
        "check_vix_coverage": False,
        "free_bytes": 10**12,
    }
    base.update(overrides)
    return base


# ── clamp_sweep_window ────────────────────────────────────────────────────────


def test_clamp_no_change_when_already_in_range() -> None:
    result = clamp_sweep_window(date(2024, 1, 1), date(2024, 1, 31), today=_TODAY)
    assert result == WindowClamp(start=date(2024, 1, 1), end=date(2024, 1, 31), clamps=())


def test_clamp_pre_2020_floor() -> None:
    result = clamp_sweep_window(date(2018, 1, 1), date(2020, 6, 1), today=_TODAY)
    assert result.start == date(2020, 1, 1)
    assert result.clamps[0].field == "start"
    assert "floor clamp" in result.clamps[0].reason


def test_clamp_future_to_date() -> None:
    result = clamp_sweep_window(date(2026, 1, 1), date(2026, 12, 31), today=_TODAY)
    assert result.end == _TODAY
    assert result.clamps[0].field == "end"
    assert "future-to-date clamp" in result.clamps[0].reason


def test_clamp_coverage_ceiling() -> None:
    result = clamp_sweep_window(date(2027, 1, 1), date(2027, 2, 1), today=date(2027, 3, 1))
    assert result.end == date(2026, 12, 31)
    assert "coverage-ceiling clamp" in result.clamps[0].reason


def test_clamp_degenerate_fully_clamped_range() -> None:
    """start way in the future, end also in the future but before today's later value."""
    result = clamp_sweep_window(date(2030, 1, 1), date(2030, 6, 1), today=date(2026, 7, 8))
    assert result.start == result.end
    assert result.end == date(2026, 7, 8)


def test_clamp_window_entirely_before_floor_collapses_without_crashing() -> None:
    """Regression pin for a hostile-review-found crash: a window entirely
    before the pre-2020 floor (both endpoints pre-floor) used to clamp only
    ``start`` up to the floor while leaving the unclamped, still-pre-floor
    ``end`` to reach the calendar walk, raising
    ``HolidayDataUnavailableError``. The interval-intersection rewrite must
    collapse the WHOLE window to the floor and disclose both clamps."""
    result = clamp_sweep_window(date(2015, 1, 1), date(2016, 1, 1), today=_TODAY)
    assert result.start == result.end == date(2020, 1, 1)
    assert {c.field for c in result.clamps} == {"start", "end"}
    assert all("coverage floor" in c.reason for c in result.clamps)


def test_clamp_window_entirely_after_ceiling_collapses_without_crashing() -> None:
    result = clamp_sweep_window(date(2027, 6, 1), date(2027, 12, 31), today=date(2027, 3, 1))
    assert result.start == result.end == date(2026, 12, 31)
    assert {c.field for c in result.clamps} == {"start", "end"}
    assert all("clamped ceiling" in c.reason for c in result.clamps)


def test_clamp_rejects_reversed_window() -> None:
    """Adversarial-refuter finding: a reversed ``(start, end)`` pair that
    ALSO sits inside ``[floor, ceiling]`` used to sail through unmodified —
    silently returning ``start > end`` with zero clamps recorded, a caller
    bug masquerading as a clean result. Must now raise loudly instead."""
    with pytest.raises(ValueError, match="must not precede"):
        clamp_sweep_window(date(2022, 6, 1), date(2022, 1, 1), today=_TODAY)


def test_clamp_ceiling_never_inverts_below_floor_for_an_unrealistic_today() -> None:
    """Adversarial-refuter finding: an unrealistic injected ``today`` before
    the coverage floor used to produce ``ceiling < floor`` (an inverted valid
    span), collapsing the window to a date YEARS before the injected "now"
    (i.e. `today`'s OWN value, since the un-clamped ceiling degrades to
    `today` itself) instead of the honest floor — contradicting the
    function's own future-to-date guarantee. The ceiling must never fall
    below the floor.

    A round-3 mutation-testing audit found the ORIGINAL version of this test
    (``start=2010-01-01, end=2010-06-01``) passed for the wrong reason: `end`
    alone was already `< floor`, so the separate `end < floor` branch chose
    `collapse_to = floor` regardless of whether the `max(floor, ...)` ceiling
    fix was even present — reverting the fix left this test green. This
    version uses a window that sits ENTIRELY ABOVE the floor (`>= floor`,
    both endpoints), so only the ceiling computation decides the outcome:
    without the fix, `ceiling` degrades to the unrealistic `today`
    (2005-01-01, itself before the floor) and the window collapses there;
    with the fix, `ceiling` is floored at 2020-01-01."""
    result = clamp_sweep_window(date(2021, 6, 1), date(2021, 6, 1), today=date(2005, 1, 1))
    assert result.start == result.end == date(2020, 1, 1)


# ── missing_1m_partitions ──────────────────────────────────────────────────────


def test_missing_1m_partitions_reports_gaps(tmp_path: Path) -> None:
    gaps = missing_1m_partitions(
        tmp_path, exchange="NSE", segment="INDEX", symbols=["NIFTY"], required_months=[(2026, 1)]
    )
    assert gaps == {"NIFTY": ("2026-01",)}


def test_missing_1m_partitions_empty_when_all_present(tmp_path: Path) -> None:
    _touch_partition(
        tmp_path,
        exchange="NSE",
        segment="INDEX",
        symbol="NIFTY",
        interval="1minute",
        year=2026,
        month=1,
    )
    gaps = missing_1m_partitions(
        tmp_path, exchange="NSE", segment="INDEX", symbols=["NIFTY"], required_months=[(2026, 1)]
    )
    assert gaps == {}


def test_missing_1m_partitions_multiple_symbols_independent(tmp_path: Path) -> None:
    _touch_partition(
        tmp_path,
        exchange="NSE",
        segment="INDEX",
        symbol="NIFTY",
        interval="1minute",
        year=2026,
        month=1,
    )
    gaps = missing_1m_partitions(
        tmp_path,
        exchange="NSE",
        segment="INDEX",
        symbols=["NIFTY", "BANKNIFTY"],
        required_months=[(2026, 1)],
    )
    assert gaps == {"BANKNIFTY": ("2026-01",)}


# ── run_oneclick_preflight: window + zero-session + pull-lock ────────────────


def test_zero_session_window_is_refused(tmp_path: Path) -> None:
    verdict = run_oneclick_preflight(
        **_base_kwargs(
            lake_root=tmp_path,
            results_root=tmp_path / "sweeps",
            start=date(2026, 1, 3),  # Saturday, well in the past — no future/ceiling clamp
            end=date(2026, 1, 4),  # Sunday
        )
    )
    assert verdict.ok is False
    assert any("zero tradable NSE sessions" in r.reason for r in verdict.refusals)
    # required_months is empty ⇒ the 1m-coverage/invariants/freshness/futures/VIX
    # arms (all gated on `if required_months:`) never run.
    assert not any(r.tf == "1minute" for r in verdict.refusals)


def test_pull_in_progress_refuses_whole_request(tmp_path: Path) -> None:
    with pull_singleflight_lock(tmp_path):
        verdict = run_oneclick_preflight(
            **_base_kwargs(lake_root=tmp_path, results_root=tmp_path / "sweeps")
        )
    assert verdict.ok is False
    assert any("pull is already running" in r.reason for r in verdict.refusals)


def test_no_pull_in_progress_does_not_refuse_on_that_ground(tmp_path: Path) -> None:
    verdict = run_oneclick_preflight(
        **_base_kwargs(lake_root=tmp_path, results_root=tmp_path / "sweeps")
    )
    assert not any("pull is already running" in r.reason for r in verdict.refusals)


# ── 1-minute coverage: refuse-whole vs allow_missing_slices flag-and-run ─────


def test_missing_1m_coverage_refuses_whole_by_default(tmp_path: Path) -> None:
    verdict = run_oneclick_preflight(
        **_base_kwargs(lake_root=tmp_path, results_root=tmp_path / "sweeps")
    )
    assert verdict.ok is False
    coverage_refusals = [r for r in verdict.refusals if r.tf == "1minute"]
    assert len(coverage_refusals) == 1
    assert coverage_refusals[0].symbol == "NIFTY"
    assert "2026-01" in coverage_refusals[0].missing_spans


def test_missing_1m_coverage_flags_when_allow_missing_slices(tmp_path: Path) -> None:
    verdict = run_oneclick_preflight(
        **_base_kwargs(
            lake_root=tmp_path, results_root=tmp_path / "sweeps", allow_missing_slices=True
        )
    )
    assert not any(r.tf == "1minute" for r in verdict.refusals)
    assert any(c.field.startswith("1m_coverage") for c in verdict.clamps)


def test_full_1m_coverage_is_not_refused(tmp_path: Path) -> None:
    _touch_partition(
        tmp_path,
        exchange="NSE",
        segment="INDEX",
        symbol="NIFTY",
        interval="1minute",
        year=2026,
        month=1,
    )
    verdict = run_oneclick_preflight(
        **_base_kwargs(lake_root=tmp_path, results_root=tmp_path / "sweeps")
    )
    assert not any(r.tf == "1minute" for r in verdict.refusals)


# ── lake invariants (monkeypatched — the real scanner has its own suite) ────


def test_lake_invariant_finding_refuses_whole_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _touch_partition(
        tmp_path,
        exchange="NSE",
        segment="INDEX",
        symbol="NIFTY",
        interval="1minute",
        year=2026,
        month=1,
    )

    class _FakeFinding:
        pass

    class _FakeScan:
        findings = (_FakeFinding(),)

    monkeypatch.setattr(mod, "scan_lake_invariants", lambda *a, **k: _FakeScan())
    verdict = run_oneclick_preflight(
        **_base_kwargs(lake_root=tmp_path, results_root=tmp_path / "sweeps")
    )
    assert verdict.ok is False
    assert any("lake invariant finding" in r.reason for r in verdict.refusals)


def test_lake_invariant_no_findings_is_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _touch_partition(
        tmp_path,
        exchange="NSE",
        segment="INDEX",
        symbol="NIFTY",
        interval="1minute",
        year=2026,
        month=1,
    )

    class _FakeScan:
        findings: tuple[Any, ...] = ()

    monkeypatch.setattr(mod, "scan_lake_invariants", lambda *a, **k: _FakeScan())
    verdict = run_oneclick_preflight(
        **_base_kwargs(lake_root=tmp_path, results_root=tmp_path / "sweeps")
    )
    assert not any("lake invariant finding" in r.reason for r in verdict.refusals)


def test_duplicate_symbol_is_not_double_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repeated symbol token (e.g. ``symbols=["NIFTY", "NIFTY"]``, which a
    CSV round-trip like ``"NIFTY,NIFTY"`` can produce) must be de-duplicated
    before the per-symbol lake-invariants scan, so a real finding is reported
    (and the O(files) scan paid) exactly once, not once per repetition."""
    _touch_partition(
        tmp_path,
        exchange="NSE",
        segment="INDEX",
        symbol="NIFTY",
        interval="1minute",
        year=2026,
        month=1,
    )
    scan_calls: list[str] = []

    class _FakeFinding:
        pass

    class _FakeScan:
        findings = (_FakeFinding(),)

    def _fake_scan(*args: Any, symbol: str, **kwargs: Any) -> _FakeScan:
        scan_calls.append(symbol)
        return _FakeScan()

    monkeypatch.setattr(mod, "scan_lake_invariants", _fake_scan)
    verdict = run_oneclick_preflight(
        **_base_kwargs(
            lake_root=tmp_path, results_root=tmp_path / "sweeps", symbols=["NIFTY", "NIFTY"]
        )
    )
    assert scan_calls == ["NIFTY"]
    assert sum(1 for r in verdict.refusals if "lake invariant finding" in r.reason) == 1


# ── derived-TF freshness (G1 reuse, monkeypatched verdicts source) ──────────


def test_stale_derived_tf_refuses_whole_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _touch_partition(
        tmp_path,
        exchange="NSE",
        segment="INDEX",
        symbol="NIFTY",
        interval="1minute",
        year=2026,
        month=1,
    )
    monkeypatch.setattr(
        mod, "lake_derived_freshness_verdicts", lambda *a, **k: {("NIFTY", "5minute"): "stale"}
    )
    verdict = run_oneclick_preflight(
        **_base_kwargs(
            lake_root=tmp_path, results_root=tmp_path / "sweeps", intervals=["1minute", "5minute"]
        )
    )
    assert verdict.ok is False
    assert any(r.tf == "5minute" and "stale" in r.reason for r in verdict.refusals)


def test_stale_derived_tf_flags_when_allow_missing_slices(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _touch_partition(
        tmp_path,
        exchange="NSE",
        segment="INDEX",
        symbol="NIFTY",
        interval="1minute",
        year=2026,
        month=1,
    )
    monkeypatch.setattr(
        mod, "lake_derived_freshness_verdicts", lambda *a, **k: {("NIFTY", "5minute"): "stale"}
    )
    verdict = run_oneclick_preflight(
        **_base_kwargs(
            lake_root=tmp_path,
            results_root=tmp_path / "sweeps",
            intervals=["1minute", "5minute"],
            allow_missing_slices=True,
        )
    )
    assert not any(r.tf == "5minute" for r in verdict.refusals)
    assert any(c.field == "freshness[NIFTY/5minute]" for c in verdict.clamps)


def test_fresh_derived_tf_is_not_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _touch_partition(
        tmp_path,
        exchange="NSE",
        segment="INDEX",
        symbol="NIFTY",
        interval="1minute",
        year=2026,
        month=1,
    )
    monkeypatch.setattr(
        mod, "lake_derived_freshness_verdicts", lambda *a, **k: {("NIFTY", "5minute"): "fresh"}
    )
    verdict = run_oneclick_preflight(
        **_base_kwargs(
            lake_root=tmp_path, results_root=tmp_path / "sweeps", intervals=["1minute", "5minute"]
        )
    )
    assert not any(r.tf == "5minute" for r in verdict.refusals)


def test_no_derived_intervals_skips_freshness_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """intervals == ["1minute"] only ⇒ derived_intervals is empty, freshness never runs."""

    def _boom(*_a: object, **_k: object) -> None:
        raise AssertionError("lake_derived_freshness_verdicts must not be called")

    monkeypatch.setattr(mod, "lake_derived_freshness_verdicts", _boom)
    _touch_partition(
        tmp_path,
        exchange="NSE",
        segment="INDEX",
        symbol="NIFTY",
        interval="1minute",
        year=2026,
        month=1,
    )
    ok_report = FuturesPreflightReport(missing_months=(), requested=(("NIFTY", 2026, 1),))
    monkeypatch.setattr(mod, "preflight_futures_months", lambda *a, **k: ok_report)
    verdict = run_oneclick_preflight(
        **_base_kwargs(lake_root=tmp_path, results_root=tmp_path / "sweeps")
    )
    assert verdict.ok is True


# ── futures coverage (P5's call — INDEX segment only) ───────────────────────


def test_futures_coverage_checked_for_index_segment(tmp_path: Path) -> None:
    _touch_partition(
        tmp_path,
        exchange="NSE",
        segment="INDEX",
        symbol="NIFTY",
        interval="1minute",
        year=2026,
        month=1,
    )
    verdict = run_oneclick_preflight(
        **_base_kwargs(lake_root=tmp_path, results_root=tmp_path / "sweeps")
    )
    # No registry/lake FUT data exists in the empty tmp lake ⇒ refused.
    assert verdict.ok is False
    assert any("futures translation preflight REFUSED" in r.reason for r in verdict.refusals)


def test_futures_coverage_ok_stops_refusal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _touch_partition(
        tmp_path,
        exchange="NSE",
        segment="INDEX",
        symbol="NIFTY",
        interval="1minute",
        year=2026,
        month=1,
    )
    ok_report = FuturesPreflightReport(missing_months=(), requested=(("NIFTY", 2026, 1),))
    monkeypatch.setattr(mod, "preflight_futures_months", lambda *a, **k: ok_report)
    verdict = run_oneclick_preflight(
        **_base_kwargs(lake_root=tmp_path, results_root=tmp_path / "sweeps")
    )
    assert not any("futures translation preflight REFUSED" in r.reason for r in verdict.refusals)


def test_futures_coverage_skipped_for_non_index_segment(tmp_path: Path) -> None:
    _touch_partition(
        tmp_path,
        exchange="NSE",
        segment="FNO",
        symbol="NIFTY",
        interval="1minute",
        year=2026,
        month=1,
    )
    verdict = run_oneclick_preflight(
        **_base_kwargs(lake_root=tmp_path, results_root=tmp_path / "sweeps", segment="FNO")
    )
    assert not any("futures translation preflight" in r.reason for r in verdict.refusals)


# ── India-VIX coverage arm (ruling #7) ──────────────────────────────────────


def test_vix_coverage_off_by_default_flag(tmp_path: Path) -> None:
    _touch_partition(
        tmp_path,
        exchange="NSE",
        segment="INDEX",
        symbol="NIFTY",
        interval="1minute",
        year=2026,
        month=1,
    )
    verdict = run_oneclick_preflight(
        **_base_kwargs(
            lake_root=tmp_path, results_root=tmp_path / "sweeps", check_vix_coverage=False
        )
    )
    assert not any(c.field == "vix_coverage" for c in verdict.clamps)


def test_vix_coverage_missing_is_a_clamp_never_a_refusal(tmp_path: Path) -> None:
    _touch_partition(
        tmp_path,
        exchange="NSE",
        segment="INDEX",
        symbol="NIFTY",
        interval="1minute",
        year=2026,
        month=1,
    )
    verdict = run_oneclick_preflight(
        **_base_kwargs(
            lake_root=tmp_path, results_root=tmp_path / "sweeps", check_vix_coverage=True
        )
    )
    assert not any(r.symbol == mod.INDIA_VIX_SYMBOL for r in verdict.refusals)
    assert any(c.field == "vix_coverage" for c in verdict.clamps)


def test_vix_coverage_present_is_silent(tmp_path: Path) -> None:
    _touch_partition(
        tmp_path,
        exchange="NSE",
        segment="INDEX",
        symbol="NIFTY",
        interval="1minute",
        year=2026,
        month=1,
    )
    _touch_partition(
        tmp_path,
        exchange="NSE",
        segment="INDEX",
        symbol=mod.INDIA_VIX_SYMBOL,
        interval="1minute",
        year=2026,
        month=1,
    )
    verdict = run_oneclick_preflight(
        **_base_kwargs(
            lake_root=tmp_path, results_root=tmp_path / "sweeps", check_vix_coverage=True
        )
    )
    assert not any(c.field == "vix_coverage" for c in verdict.clamps)


def test_vix_coverage_arm_skipped_for_bse_exchange_group(tmp_path: Path) -> None:
    """Round-3 adversarial-refuter finding: India VIX is an NSE-only index —
    there is no BSE India-VIX partition, ever, by design. The arm previously
    probed the CALLING group's own ``exchange`` unconditionally, so a
    SENSEX/BSE exchange-group call always reported a false "VIX coverage
    missing" clamp regardless of true VIX data health. With no VIX partition
    anywhere (not even under NSE), a BSE-exchange call must stay silent —
    the arm is skipped entirely for a non-NSE group, never falsely
    evaluated."""
    _touch_partition(
        tmp_path,
        exchange="BSE",
        segment="INDEX",
        symbol="SENSEX",
        interval="1minute",
        year=2026,
        month=1,
    )
    verdict = run_oneclick_preflight(
        **_base_kwargs(
            lake_root=tmp_path,
            results_root=tmp_path / "sweeps",
            exchange="BSE",
            symbols=["SENSEX"],
            check_vix_coverage=True,
        )
    )
    assert not any(c.field == "vix_coverage" for c in verdict.clamps)


# ── free-disk-space preflight (ruling #22) ──────────────────────────────────


def test_free_disk_blocked_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _touch_partition(
        tmp_path,
        exchange="NSE",
        segment="INDEX",
        symbol="NIFTY",
        interval="1minute",
        year=2026,
        month=1,
    )
    blocked = DiskGuardDecision(
        blocked=True,
        free_bytes=1,
        estimated_need_bytes=10**12,
        source_1m_bytes=0,
        derived_bytes=0,
        multiplier=Decimal(3),
        multiplier_source="fallback",
        margin=Decimal("0.2"),
    )
    monkeypatch.setattr(mod, "evaluate_disk_guard", lambda *a, **k: blocked)
    verdict = run_oneclick_preflight(
        **_base_kwargs(lake_root=tmp_path, results_root=tmp_path / "sweeps")
    )
    assert verdict.ok is False
    assert any("disk guard" in r.reason for r in verdict.refusals)


def test_free_disk_not_blocked_is_silent(tmp_path: Path) -> None:
    _touch_partition(
        tmp_path,
        exchange="NSE",
        segment="INDEX",
        symbol="NIFTY",
        interval="1minute",
        year=2026,
        month=1,
    )
    verdict = run_oneclick_preflight(
        **_base_kwargs(lake_root=tmp_path, results_root=tmp_path / "sweeps", free_bytes=10**15)
    )
    assert not any("disk guard" in r.reason for r in verdict.refusals)


# ── VWAP dead-bits flag-and-run (gap-hunt #8) ───────────────────────────────


def test_vwap_dead_bit_clamp_fires_for_index_segment_on_retention_layout(
    tmp_path: Path,
) -> None:
    """74/74 retention layout (DEC-VOCAB-VWAP-RETAIN-001, MIGRATION.md
    #2206): the VWAP bits 52/53 are IMPLEMENTED again yet structurally dead
    on zero-volume INDEX spot candles, so the gap-hunt-#8 annotation
    SELF-REVIVED — the condition_count clamp row FIRES with effective ==
    nominal - 2 (flag-and-run; the swept vocabulary itself is never
    reduced). The absent arm keeps its teeth in the FNO sibling below."""
    _touch_partition(
        tmp_path,
        exchange="NSE",
        segment="INDEX",
        symbol="NIFTY",
        interval="1minute",
        year=2026,
        month=1,
    )
    verdict = run_oneclick_preflight(
        **_base_kwargs(lake_root=tmp_path, results_root=tmp_path / "sweeps")
    )
    rows = [c for c in verdict.clamps if c.field == "condition_count"]
    assert len(rows) == 1
    assert verdict.estimator.nominal_condition_count == 74
    assert verdict.estimator.effective_condition_count == 72
    assert rows[0].applied == "72 effective (flag-and-run)"


def test_vwap_dead_bit_clamp_fires_when_a_dead_bit_is_implemented(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The flag-and-run clamp arm stays LIVE: if a volume-dependent bit ever
    returns to the implemented set, ``effective < nominal`` on INDEX and the
    clamp row fires. Driven via a monkeypatched annotation (no such bit
    exists on the tombstoned 66 layout — the mechanism must not rot)."""
    from brutex.engine import sweep_estimator

    def _fake(*, segment: str, n: int = sweep_estimator.SWEPT_CONDITIONS) -> tuple[int, int]:
        return (n, n - 2) if segment.upper() == "INDEX" else (n, n)

    monkeypatch.setattr(sweep_estimator, "effective_condition_count", _fake)
    _touch_partition(
        tmp_path,
        exchange="NSE",
        segment="INDEX",
        symbol="NIFTY",
        interval="1minute",
        year=2026,
        month=1,
    )
    verdict = run_oneclick_preflight(
        **_base_kwargs(lake_root=tmp_path, results_root=tmp_path / "sweeps")
    )
    assert any(c.field == "condition_count" for c in verdict.clamps)
    assert (
        verdict.estimator.effective_condition_count == verdict.estimator.nominal_condition_count - 2
    )


def test_vwap_dead_bit_clamp_absent_for_non_index_segment(tmp_path: Path) -> None:
    _touch_partition(
        tmp_path,
        exchange="NSE",
        segment="FNO",
        symbol="NIFTY",
        interval="1minute",
        year=2026,
        month=1,
    )
    verdict = run_oneclick_preflight(
        **_base_kwargs(lake_root=tmp_path, results_root=tmp_path / "sweeps", segment="FNO")
    )
    assert not any(c.field == "condition_count" for c in verdict.clamps)


# ── SLA decisions (allowed / acknowledged / refused) ────────────────────────


def test_sla_allowed_is_silent(tmp_path: Path) -> None:
    _touch_partition(
        tmp_path,
        exchange="NSE",
        segment="INDEX",
        symbol="NIFTY",
        interval="1minute",
        year=2026,
        month=1,
    )
    verdict = run_oneclick_preflight(
        **_base_kwargs(lake_root=tmp_path, results_root=tmp_path / "sweeps", tuple_count=1)
    )
    assert not any("SLA OVERRUN" in c.reason for c in verdict.clamps if hasattr(c, "reason"))
    assert not any("REFUSED" in r.reason and "envelope" in r.reason for r in verdict.refusals)


def test_sla_refused_when_projection_exceeds_envelope_unacknowledged(tmp_path: Path) -> None:
    _touch_partition(
        tmp_path,
        exchange="NSE",
        segment="INDEX",
        symbol="NIFTY",
        interval="1minute",
        year=2026,
        month=1,
    )
    huge_tuple_count = 10**7  # 10M tuples * 30s >> 4-day envelope
    verdict = run_oneclick_preflight(
        **_base_kwargs(
            lake_root=tmp_path, results_root=tmp_path / "sweeps", tuple_count=huge_tuple_count
        )
    )
    assert verdict.ok is False
    assert any("envelope" in r.reason for r in verdict.refusals)


def test_sla_acknowledged_proceeds_with_a_clamp_note(tmp_path: Path) -> None:
    _touch_partition(
        tmp_path,
        exchange="NSE",
        segment="INDEX",
        symbol="NIFTY",
        interval="1minute",
        year=2026,
        month=1,
    )
    huge_tuple_count = 10**7
    verdict = run_oneclick_preflight(
        **_base_kwargs(
            lake_root=tmp_path,
            results_root=tmp_path / "sweeps",
            tuple_count=huge_tuple_count,
            acknowledge_sla_overrun=True,
        )
    )
    assert not any("envelope" in r.reason for r in verdict.refusals)
    assert any(c.field == "sla_overrun" for c in verdict.clamps)


# ── to_dict shapes ───────────────────────────────────────────────────────────


def test_refusal_to_dict_shape() -> None:
    refusal = Refusal(symbol="NIFTY", tf="1minute", missing_spans=("2026-01",), reason="x")
    assert refusal.to_dict() == {
        "symbol": "NIFTY",
        "tf": "1minute",
        "missing_spans": ["2026-01"],
        "reason": "x",
    }


def test_clamp_to_dict_shape() -> None:
    clamp = Clamp(field="start", requested="2018-01-01", applied="2020-01-01", reason="x")
    assert clamp.to_dict() == {
        "field": "start",
        "requested": "2018-01-01",
        "applied": "2020-01-01",
        "reason": "x",
    }


def test_verdict_to_dict_shape(tmp_path: Path) -> None:
    verdict = run_oneclick_preflight(
        **_base_kwargs(lake_root=tmp_path, results_root=tmp_path / "sweeps")
    )
    rendered = verdict.to_dict()
    assert set(rendered) == {"ok", "refusals", "clamps", "estimator"}
    assert isinstance(rendered["refusals"], list)
    assert isinstance(rendered["clamps"], list)
    assert isinstance(rendered["estimator"], dict)
    assert rendered["estimator"]["estimate_basis"] == PROVISIONAL_BASIS
