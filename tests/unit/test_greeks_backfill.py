# pyright: reportPrivateUsage=none
"""Unit tests for :mod:`brutex.data.greeks_backfill` (DEC-GREEKS-BULK-REENRICH-001).

100% line + branch on the bulk v1→v2 re-enrich backfill: the exported
staleness predicate truth table, the scanner classification matrix (unstamped /
v1 / inputs-drift / current / futures / undecodable / expired-only / limit /
force / corrupt-file / no-files / pause), the content-addressed plan id
(determinism + sensitivity), the env resolvers' fail-loud contract, and the
runner's batch loop (slicing, checkpoint record/ignore/continue/delete,
resume, halt escape, progress hook, pause, empty plan, dry-run report).

Test types: unit (every branch) + golden-style frozen expectations for the
packed stamp; the hypothesis properties live in
``tests/property/test_greeks_backfill_properties.py`` and the end-to-end lake
round-trips in ``tests/integration/test_greeks_bulk_reenrich.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("trading_core", reason="greeks enrichment needs the trading_core wheel")
pytest.importorskip("polars", reason="the scanner probe needs polars")

import polars as pl
from brutex.data.errors import GreeksEnrichPoolBroken, LakeWriteHalt
from brutex.data.greeks_backfill import (
    BACKFILL_BATCH_ENV,
    BACKFILL_PAUSE_ENV,
    DEFAULT_BACKFILL_BATCH_SIZE,
    BackfillPlan,
    BackfillScan,
    _contract_window,
    _month_files,
    checkpoint_path,
    dry_run_backfill_report,
    plan_greeks_backfill,
    resolve_backfill_batch_size,
    resolve_backfill_pause_s,
    run_greeks_backfill,
    scan_stale_greeks_targets,
)
from brutex.data.greeks_enrich import (
    RATE_SOURCE_ID_CSV,
    RATE_SOURCE_ID_FIXED,
    STATUS_NO_SPOT,
    STATUS_OK,
    STYLE_EUROPEAN_EXACT,
    GreeksEnrichReport,
    GreeksEnrichTarget,
    current_inputs_pack,
    pack_greeks_provenance,
    stamp_is_current_inputs,
)
from brutex.engine.sweep_checkpoint import (
    SweepCheckpointError,
    SweepCheckpointStore,
)
from brutex.options.risk_free import FixedRateSource

_RATE = FixedRateSource()

#: A stamp packed EXACTLY as the current pass packs an OK bar (v2 / fixed rate).
_CURRENT_PID = pack_greeks_provenance(
    rate_source_id=RATE_SOURCE_ID_FIXED, style_flag=STYLE_EUROPEAN_EXACT, status=STATUS_OK
)
#: The same inputs stamped under formula v1 — the #1188 frozen-expired class.
_V1_PID = pack_greeks_provenance(
    rate_source_id=RATE_SOURCE_ID_FIXED,
    style_flag=STYLE_EUROPEAN_EXACT,
    status=STATUS_OK,
    formula_version=1,
)
#: Current formula but a drifted rate VERSION — the inputs-drift class.
_DRIFT_PID = pack_greeks_provenance(
    rate_source_id=RATE_SOURCE_ID_FIXED,
    style_flag=STYLE_EUROPEAN_EXACT,
    status=STATUS_OK,
    rate_version=2,
)

_CE = "NSE-NIFTY-25Jan24-21500-CE"
_PE = "NSE-NIFTY-25Jan24-21500-PE"
_FUT = "NSE-NIFTY-25Jan24-FUT"
_TODAY = date(2026, 7, 6)


# ────────────────────────────────────────────────────────────────────────
# Lake scaffolding (probe-shaped: only the stamp column matters)
# ────────────────────────────────────────────────────────────────────────


def _write_month(
    lake: Path,
    symbol: str,
    year: int,
    month: int,
    pids: list[int | None],
    *,
    interval: str = "1minute",
    exchange: str = "NSE",
    with_pid_column: bool = True,
) -> Path:
    """Write a minimal month partition carrying (or omitting) the stamp column."""
    dest = (
        lake
        / "bars"
        / exchange
        / "FNO"
        / symbol
        / interval
        / f"{year:04d}"
        / f"{month:02d}.parquet"
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    if with_pid_column:
        frame = pl.DataFrame({"greeks_provenance_id": pl.Series(pids, dtype=pl.Int32)})
    else:
        frame = pl.DataFrame({"close": [1.0] * max(1, len(pids))})
    frame.write_parquet(dest)
    return dest


@pytest.fixture
def lake(tmp_path: Path) -> Path:
    root = tmp_path / "lake"
    root.mkdir()
    return root


def _scan(lake: Path, **kwargs: Any) -> BackfillScan:
    kwargs.setdefault("rate_source", _RATE)
    kwargs.setdefault("today", _TODAY)
    return scan_stale_greeks_targets(lake, **kwargs)


# ────────────────────────────────────────────────────────────────────────
# stamp_is_current_inputs / current_inputs_pack — the exported predicate
# ────────────────────────────────────────────────────────────────────────


class TestStampPredicate:
    def test_none_is_never_current(self) -> None:
        assert stamp_is_current_inputs(None, rate_source=_RATE) is False

    def test_current_v2_stamp_is_current(self) -> None:
        assert stamp_is_current_inputs(_CURRENT_PID, rate_source=_RATE) is True

    def test_v1_stamp_is_stale(self) -> None:
        assert stamp_is_current_inputs(_V1_PID, rate_source=_RATE) is False

    def test_rate_version_drift_is_stale(self) -> None:
        assert stamp_is_current_inputs(_DRIFT_PID, rate_source=_RATE) is False

    def test_rate_source_kind_drift_is_stale(self) -> None:
        csv_stamped = pack_greeks_provenance(
            rate_source_id=RATE_SOURCE_ID_CSV, style_flag=0, status=0
        )
        assert stamp_is_current_inputs(csv_stamped, rate_source=_RATE) is False

    def test_style_and_status_bits_do_not_affect_the_inputs_verdict(self) -> None:
        """Style + status are per-bar DESCRIPTIONS, not inputs (bits 20-26)."""
        demoted = pack_greeks_provenance(
            rate_source_id=RATE_SOURCE_ID_FIXED, style_flag=0, status=STATUS_NO_SPOT
        )
        assert stamp_is_current_inputs(demoted, rate_source=_RATE) is True

    def test_pack_matches_the_driver_gate_bytes(self) -> None:
        """The predicate's pack IS the gate's pack — ONE source of truth."""
        expected = pack_greeks_provenance(
            rate_source_id=RATE_SOURCE_ID_FIXED, style_flag=0, status=0
        ) & ((1 << 20) - 1)
        assert current_inputs_pack(_RATE) == expected

    def test_frozen_current_pack_golden(self) -> None:
        """Golden pin: the v2/fixed-rate input bits are exactly this integer."""
        # formula=2 | rate_source=1<<8 | rate_version=1<<12 | q_bias=1<<16 | tte=1<<18
        assert current_inputs_pack(_RATE) == 2 + (1 << 8) + (1 << 12) + (1 << 16) + (1 << 18)


# ────────────────────────────────────────────────────────────────────────
# Scanner classification matrix
# ────────────────────────────────────────────────────────────────────────


class TestScanner:
    def test_unstamped_null_classifies_stale_null(self, lake: Path) -> None:
        _write_month(lake, _CE, 2024, 1, [None, _CURRENT_PID])
        scan = _scan(lake)
        assert scan.stale_null == 1 and len(scan.targets) == 1
        assert scan.targets[0] == GreeksEnrichTarget(
            exchange="NSE", symbol=_CE, start=date(2024, 1, 1), end=date(2024, 2, 2)
        )

    def test_legacy_seven_column_partition_classifies_stale_null(self, lake: Path) -> None:
        _write_month(lake, _CE, 2024, 1, [None], with_pid_column=False)
        scan = _scan(lake)
        assert scan.stale_null == 1 and len(scan.targets) == 1

    def test_v1_stamp_classifies_stale_v1(self, lake: Path) -> None:
        _write_month(lake, _CE, 2024, 1, [_V1_PID])
        scan = _scan(lake)
        assert scan.stale_v1 == 1 and scan.stale_null == 0 and len(scan.targets) == 1

    def test_inputs_drift_classifies_stale_inputs_drift(self, lake: Path) -> None:
        _write_month(lake, _CE, 2024, 1, [_DRIFT_PID])
        scan = _scan(lake)
        assert scan.stale_inputs_drift == 1 and scan.stale_v1 == 0 and len(scan.targets) == 1

    def test_fully_current_contract_is_not_targeted(self, lake: Path) -> None:
        _write_month(lake, _CE, 2024, 1, [_CURRENT_PID, _CURRENT_PID])
        scan = _scan(lake)
        assert scan.current == 1 and scan.targets == ()

    def test_future_is_skipped_by_design(self, lake: Path) -> None:
        _write_month(lake, _FUT, 2024, 1, [None])
        scan = _scan(lake)
        assert scan.futures_skipped == 1 and scan.targets == ()

    def test_undecodable_symbol_is_counted_never_crashed(self, lake: Path) -> None:
        _write_month(lake, "NSE-JUNKNAME", 2024, 1, [None])
        scan = _scan(lake)
        assert scan.undecodable == 1 and scan.targets == ()

    def test_non_minute_interval_is_never_scanned(self, lake: Path) -> None:
        """Derived TFs never carry the overlay — deliberately out of scope."""
        _write_month(lake, _CE, 2024, 1, [None], interval="5minute")
        scan = _scan(lake)
        assert scan.targets == () and scan.files_probed == 0

    def test_minute_and_derived_intervals_dedupe_to_one_contract(self, lake: Path) -> None:
        _write_month(lake, _CE, 2024, 1, [None])
        _write_month(lake, _CE, 2024, 1, [None], interval="5minute")
        scan = _scan(lake)
        assert len(scan.targets) == 1 and scan.stale_null == 1

    def test_expired_only_skips_live_contract_counted(self, lake: Path) -> None:
        _write_month(lake, _CE, 2024, 1, [None])  # expiry 25Jan24 < today
        live = "NSE-NIFTY-30Dec26-21500-CE"  # expiry after the injected today
        _write_month(lake, live, 2026, 12, [None])
        scan = _scan(lake, expired_only=True)
        assert scan.live_skipped == 1
        assert [t.symbol for t in scan.targets] == [_CE]

    def test_default_scan_reaches_live_contracts_gap_b(self, lake: Path) -> None:
        """expired_only defaults False so a CLI-pulled lake's LIVE contracts are reachable."""
        live = "NSE-NIFTY-30Dec26-21500-CE"
        _write_month(lake, live, 2026, 12, [None])
        scan = _scan(lake)
        assert [t.symbol for t in scan.targets] == [live] and scan.live_skipped == 0

    def test_no_month_files_is_counted_honestly(self, lake: Path) -> None:
        """A parquet at a non-month path lists the dataset but yields no probe unit."""
        dest = lake / "bars" / "NSE" / "FNO" / _CE / "1minute" / "junk" / "01.parquet"
        dest.parent.mkdir(parents=True)
        pl.DataFrame({"greeks_provenance_id": pl.Series([None], dtype=pl.Int32)}).write_parquet(
            dest
        )
        scan = _scan(lake)
        assert scan.no_files == 1 and scan.targets == ()

    def test_corrupt_file_is_probe_failed_not_fatal(self, lake: Path) -> None:
        path = _write_month(lake, _CE, 2024, 1, [_CURRENT_PID])
        path.write_bytes(b"garbage - not a parquet file")
        scan = _scan(lake)
        assert scan.probe_failed == 1 and scan.targets == ()
        assert scan.current == 1  # nothing ELSE stale — honestly not targeted

    def test_corrupt_file_does_not_mask_a_stale_sibling_month(self, lake: Path) -> None:
        path = _write_month(lake, _CE, 2024, 1, [_CURRENT_PID])
        path.write_bytes(b"garbage")
        _write_month(lake, _CE, 2024, 1, [_V1_PID])  # rewrite month 1 clean? no — month 2:
        _write_month(lake, _CE, 2024, 2, [_V1_PID])
        path.write_bytes(b"garbage")  # keep month 1 corrupt
        scan = _scan(lake)
        assert scan.probe_failed == 1 and scan.stale_v1 == 1 and len(scan.targets) == 1

    def test_probe_short_circuits_on_first_stale_file(self, lake: Path) -> None:
        _write_month(lake, _CE, 2024, 1, [None])
        _write_month(lake, _CE, 2024, 2, [_CURRENT_PID])
        scan = _scan(lake)
        assert scan.files_probed == 1  # month 2 never read

    def test_window_spans_first_to_last_month_incl_december_wrap(self, lake: Path) -> None:
        _write_month(lake, _CE, 2023, 12, [None])
        _write_month(lake, _CE, 2024, 1, [None])
        scan = _scan(lake)
        (target,) = scan.targets
        assert target.start == date(2023, 12, 1)
        assert target.end == date(2024, 2, 2)  # day 1 after Jan + 1 day (IST skew pad)

    def test_targets_sorted_by_underlying_then_exchange_then_symbol(self, lake: Path) -> None:
        banknifty = "NSE-BANKNIFTY-25Jan24-45000-CE"
        _write_month(lake, _PE, 2024, 1, [None])
        _write_month(lake, _CE, 2024, 1, [None])
        _write_month(lake, banknifty, 2024, 1, [None])
        scan = _scan(lake)
        assert [t.symbol for t in scan.targets] == [banknifty, _CE, _PE]

    def test_limit_truncates_the_sorted_list(self, lake: Path) -> None:
        banknifty = "NSE-BANKNIFTY-25Jan24-45000-CE"
        _write_month(lake, _CE, 2024, 1, [None])
        _write_month(lake, banknifty, 2024, 1, [None])
        scan = _scan(lake, limit=1)
        assert [t.symbol for t in scan.targets] == [banknifty]

    def test_force_skips_the_probe_entirely(self, lake: Path) -> None:
        _write_month(lake, _CE, 2024, 1, [_CURRENT_PID])  # fully current
        scan = _scan(lake, force=True)
        assert len(scan.targets) == 1 and scan.files_probed == 0 and scan.current == 0

    def test_per_file_pause_sleeps_between_probes(self, lake: Path) -> None:
        _write_month(lake, _CE, 2024, 1, [_CURRENT_PID])
        _write_month(lake, _CE, 2024, 2, [_CURRENT_PID])
        naps: list[float] = []
        _scan(lake, per_file_pause=0.01, sleep=naps.append)
        assert naps == [0.01, 0.01]

    def test_empty_lake_scans_clean(self, lake: Path) -> None:
        scan = _scan(lake)
        assert scan == BackfillScan(targets=())

    def test_duplicate_dataset_rows_dedupe_to_one_contract(
        self, lake: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A duplicated lister row (e.g. a dup manifest row) never double-targets."""
        _write_month(lake, _CE, 2024, 1, [None])
        row = {"exchange": "NSE", "segment": "FNO", "symbol": _CE, "interval": "1minute"}
        monkeypatch.setattr(
            "brutex.dashboard.db_view.list_fno_candle_datasets", lambda _root: [row, dict(row)]
        )
        scan = _scan(lake)
        assert len(scan.targets) == 1 and scan.stale_null == 1

    def test_today_defaults_to_the_real_ist_date(self, lake: Path) -> None:
        """No injected today → the fixed +05:30 IST calendar date (never naive-local)."""
        from datetime import UTC as _UTC
        from datetime import datetime as _dt
        from datetime import timedelta as _td

        from brutex.data.greeks_backfill import _today_ist

        before = (_dt.now(_UTC) + _td(hours=5, minutes=30)).date()
        scan = scan_stale_greeks_targets(lake, rate_source=_RATE)
        after = (_dt.now(_UTC) + _td(hours=5, minutes=30)).date()
        assert scan == BackfillScan(targets=())
        assert before <= _today_ist() <= after  # the real IST date, midnight-race tolerant

    def test_month_files_skips_non_numeric_names(self, lake: Path) -> None:
        base = lake / "1minute"
        (base / "2024").mkdir(parents=True)
        (base / "2024" / "01.parquet").write_bytes(b"x")
        (base / "2024" / "junk.parquet").write_bytes(b"x")
        assert [(y, m) for y, m, _ in _month_files(base)] == [(2024, 1)]

    def test_contract_window_non_december(self) -> None:
        files = [(2024, 3, Path("a")), (2024, 5, Path("b"))]
        assert _contract_window(files) == (date(2024, 3, 1), date(2024, 6, 2))


# ────────────────────────────────────────────────────────────────────────
# Plan — content-addressed id
# ────────────────────────────────────────────────────────────────────────


def _plan_of(targets: tuple[GreeksEnrichTarget, ...], **kwargs: Any) -> BackfillPlan:
    scan = BackfillScan(targets=targets)
    kwargs.setdefault("rate_source", _RATE)
    kwargs.setdefault("force", False)
    kwargs.setdefault("expired_only", False)
    return plan_greeks_backfill(scan, **kwargs)


_T1 = GreeksEnrichTarget(exchange="NSE", symbol=_CE, start=date(2024, 1, 1), end=date(2024, 2, 2))
_T2 = GreeksEnrichTarget(exchange="NSE", symbol=_PE, start=date(2024, 1, 1), end=date(2024, 2, 2))


class TestPlan:
    def test_deterministic_same_inputs_same_id(self) -> None:
        assert _plan_of((_T1, _T2)).backfill_id == _plan_of((_T1, _T2)).backfill_id

    def test_sensitive_to_the_target_set(self) -> None:
        assert _plan_of((_T1,)).backfill_id != _plan_of((_T1, _T2)).backfill_id

    def test_sensitive_to_force(self) -> None:
        assert _plan_of((_T1,)).backfill_id != _plan_of((_T1,), force=True).backfill_id

    def test_sensitive_to_expired_only(self) -> None:
        assert _plan_of((_T1,)).backfill_id != _plan_of((_T1,), expired_only=True).backfill_id

    def test_sensitive_to_the_rate_source_kind(self, tmp_path: Path) -> None:
        from brutex.options.risk_free import load_risk_free_csv

        csv = tmp_path / "rates.csv"
        csv.write_text("date,yield,tenor_days\n2024-01-01,0.065,91\n", encoding="utf-8")
        csv_source = load_risk_free_csv(csv)
        assert _plan_of((_T1,)).backfill_id != _plan_of((_T1,), rate_source=csv_source).backfill_id

    def test_plan_carries_scan_and_modes(self) -> None:
        plan = _plan_of((_T1,), force=True, expired_only=True)
        assert plan.force is True and plan.expired_only is True
        assert plan.targets == (_T1,) and plan.scan.targets == (_T1,)


# ────────────────────────────────────────────────────────────────────────
# Env resolvers — fail-loud contract
# ────────────────────────────────────────────────────────────────────────


class TestEnvResolvers:
    def test_batch_default(self) -> None:
        assert resolve_backfill_batch_size(env={}) == DEFAULT_BACKFILL_BATCH_SIZE

    def test_batch_blank_is_default(self) -> None:
        assert resolve_backfill_batch_size(env={BACKFILL_BATCH_ENV: "  "}) == (
            DEFAULT_BACKFILL_BATCH_SIZE
        )

    def test_batch_valid_override(self) -> None:
        assert resolve_backfill_batch_size(env={BACKFILL_BATCH_ENV: "7"}) == 7

    def test_batch_junk_fails_loud(self) -> None:
        with pytest.raises(ValueError, match="not an integer"):
            resolve_backfill_batch_size(env={BACKFILL_BATCH_ENV: "many"})

    def test_batch_below_one_fails_loud(self) -> None:
        with pytest.raises(ValueError, match="must be >= 1"):
            resolve_backfill_batch_size(env={BACKFILL_BATCH_ENV: "0"})

    def test_batch_reads_os_environ_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(BACKFILL_BATCH_ENV, "3")
        assert resolve_backfill_batch_size() == 3

    def test_pause_default(self) -> None:
        assert resolve_backfill_pause_s(env={}) == 0.0

    def test_pause_blank_is_default(self) -> None:
        assert resolve_backfill_pause_s(env={BACKFILL_PAUSE_ENV: ""}) == 0.0

    def test_pause_valid_override(self) -> None:
        assert resolve_backfill_pause_s(env={BACKFILL_PAUSE_ENV: "0.25"}) == 0.25

    def test_pause_junk_fails_loud(self) -> None:
        with pytest.raises(ValueError, match="not a number"):
            resolve_backfill_pause_s(env={BACKFILL_PAUSE_ENV: "slow"})

    def test_pause_negative_fails_loud(self) -> None:
        with pytest.raises(ValueError, match="must be >= 0"):
            resolve_backfill_pause_s(env={BACKFILL_PAUSE_ENV: "-1"})

    def test_pause_reads_os_environ_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(BACKFILL_PAUSE_ENV, "0.5")
        assert resolve_backfill_pause_s() == 0.5


# ────────────────────────────────────────────────────────────────────────
# Runner — batch loop + checkpoint discipline
# ────────────────────────────────────────────────────────────────────────


def _enrich_report(**overrides: Any) -> GreeksEnrichReport:
    base: dict[str, Any] = {
        "enriched": 1,
        "already_current": 0,
        "futures_skipped": 0,
        "undecodable": 0,
        "no_bars": 0,
        "errored": 0,
        "bars_written": 3,
        "bars_solved": 3,
        "bars_null": 0,
        "bars_reused": 0,
        "spot_reads": 1,
        "spot_hits": 0,
        "bars_skipped": 0,
    }
    base.update(overrides)
    return GreeksEnrichReport(**base)


@dataclass
class _SpyEnrich:
    """Records every batch handed to the driver seam; scripted per-call reports."""

    reports: list[GreeksEnrichReport] = field(default_factory=list)
    calls: list[list[GreeksEnrichTarget]] = field(default_factory=list)
    raise_on_call: int | None = None
    raise_exc: Exception | None = None

    def __call__(self, **kwargs: Any) -> GreeksEnrichReport:
        self.calls.append(list(kwargs["targets"]))
        if self.raise_on_call is not None and len(self.calls) == self.raise_on_call:
            assert self.raise_exc is not None
            raise self.raise_exc
        if self.reports:
            return self.reports.pop(0)
        return _enrich_report(enriched=len(kwargs["targets"]))


def _targets(n: int) -> tuple[GreeksEnrichTarget, ...]:
    return tuple(
        GreeksEnrichTarget(
            exchange="NSE",
            symbol=f"NSE-NIFTY-25Jan24-{21000 + i}-CE",
            start=date(2024, 1, 1),
            end=date(2024, 2, 2),
        )
        for i in range(n)
    )


def _plan(n: int) -> BackfillPlan:
    return _plan_of(_targets(n))


class TestRunner:
    def test_batches_slice_the_plan_incl_last_partial(self, lake: Path) -> None:
        spy = _SpyEnrich()
        report = run_greeks_backfill(
            lake, plan=_plan(5), rate_source=_RATE, batch_size=2, enrich=spy
        )
        assert [len(c) for c in spy.calls] == [2, 2, 1]
        assert report.batches == 3 and report.planned == 5 and report.enriched == 5
        assert report.dry_run is False and report.resumed_from == 0

    def test_checkpoint_deleted_on_clean_completion(self, lake: Path) -> None:
        run_greeks_backfill(
            lake, plan=_plan(3), rate_source=_RATE, batch_size=2, enrich=_SpyEnrich()
        )
        assert not checkpoint_path(lake).exists()

    def test_halt_escapes_with_checkpoint_intact(self, lake: Path) -> None:
        spy = _SpyEnrich(raise_on_call=2, raise_exc=LakeWriteHalt("disk_full"))
        with pytest.raises(LakeWriteHalt):
            run_greeks_backfill(lake, plan=_plan(4), rate_source=_RATE, batch_size=2, enrich=spy)
        assert checkpoint_path(lake).exists()  # batch 1's record survives for resume

    def test_pool_broken_escapes_uncaught(self, lake: Path) -> None:
        spy = _SpyEnrich(raise_on_call=1, raise_exc=GreeksEnrichPoolBroken("worker died"))
        with pytest.raises(GreeksEnrichPoolBroken):
            run_greeks_backfill(lake, plan=_plan(2), rate_source=_RATE, batch_size=2, enrich=spy)

    def test_resume_starts_after_the_checkpointed_batch(self, lake: Path) -> None:
        plan = _plan(4)
        spy = _SpyEnrich(raise_on_call=2, raise_exc=LakeWriteHalt("disk_full"))
        with pytest.raises(LakeWriteHalt):
            run_greeks_backfill(lake, plan=plan, rate_source=_RATE, batch_size=2, enrich=spy)
        resumed = _SpyEnrich()
        report = run_greeks_backfill(
            lake, plan=plan, rate_source=_RATE, batch_size=2, enrich=resumed
        )
        assert resumed.calls == [list(plan.targets[2:4])]  # targets 0-1 never redone
        assert report.resumed_from == 2 and "resumed from target 2" in report.note

    def test_foreign_backfill_id_checkpoint_is_ignored(self, lake: Path) -> None:
        other = SweepCheckpointStore(
            path=checkpoint_path(lake), sweep_id="someone-else", total_combos=4
        )
        other.record(1)
        spy = _SpyEnrich()
        report = run_greeks_backfill(
            lake, plan=_plan(2), rate_source=_RATE, batch_size=2, enrich=spy
        )
        assert report.resumed_from == 0 and len(spy.calls) == 1

    def test_corrupt_checkpoint_is_ignored_loudly_never_a_crash(self, lake: Path) -> None:
        path = checkpoint_path(lake)
        path.parent.mkdir(parents=True)
        path.write_text("{not json", encoding="utf-8")
        from structlog.testing import capture_logs

        with capture_logs() as logs:
            report = run_greeks_backfill(
                lake, plan=_plan(1), rate_source=_RATE, batch_size=1, enrich=_SpyEnrich()
            )
        assert report.resumed_from == 0 and report.batches == 1
        assert any(r["event"] == "greeks_backfill.checkpoint_ignored" for r in logs)

    def test_record_failure_logs_and_continues(self, lake: Path) -> None:
        class _BoomStore(SweepCheckpointStore):
            def record(self, completed_index: int) -> bool:
                raise SweepCheckpointError("disk sneezed")

        store = _BoomStore(
            path=checkpoint_path(lake), sweep_id=_plan(2).backfill_id, total_combos=2
        )
        from structlog.testing import capture_logs

        with capture_logs() as logs:
            report = run_greeks_backfill(
                lake,
                plan=_plan(2),
                rate_source=_RATE,
                batch_size=1,
                enrich=_SpyEnrich(),
                checkpoint_store=store,
            )
        assert report.batches == 2  # checkpoint loss ≠ halt
        assert sum(1 for r in logs if r["event"] == "greeks_backfill.checkpoint_failed") == 2

    def test_progress_hook_gets_absolute_counts_per_batch(self, lake: Path) -> None:
        ticks: list[tuple[int, int, int, int, str]] = []

        def _hook(completed: int, total: int, bars: int, errored: int, label: str) -> None:
            ticks.append((completed, total, bars, errored, label))

        spy = _SpyEnrich(
            reports=[
                _enrich_report(enriched=2, bars_written=10),
                _enrich_report(enriched=0, errored=1, bars_written=0),
            ]
        )
        run_greeks_backfill(
            lake, plan=_plan(3), rate_source=_RATE, batch_size=2, enrich=spy, on_progress=_hook
        )
        assert ticks == [
            (2, 3, 10, 0, "contracts 2/3 (batch 1)"),
            (3, 3, 10, 1, "contracts 3/3 (batch 2)"),
        ]

    def test_pause_sleeps_between_batches_never_after_the_last(self, lake: Path) -> None:
        naps: list[float] = []
        run_greeks_backfill(
            lake,
            plan=_plan(4),
            rate_source=_RATE,
            batch_size=2,
            pause_s=0.05,
            enrich=_SpyEnrich(),
            sleep=naps.append,
        )
        assert naps == [0.05]

    def test_empty_plan_is_an_instant_honest_no_op(self, lake: Path) -> None:
        spy = _SpyEnrich()
        report = run_greeks_backfill(lake, plan=_plan(0), rate_source=_RATE, enrich=spy)
        assert spy.calls == [] and report.batches == 0 and report.planned == 0
        assert "0 contract(s) re-stamped v2" in report.note
        assert not checkpoint_path(lake).exists()

    def test_explicit_batch_below_one_fails_loud(self, lake: Path) -> None:
        with pytest.raises(ValueError, match="batch_size must be >= 1"):
            run_greeks_backfill(lake, plan=_plan(1), rate_source=_RATE, batch_size=0)

    def test_explicit_negative_pause_fails_loud(self, lake: Path) -> None:
        with pytest.raises(ValueError, match="pause_s must be >= 0"):
            run_greeks_backfill(lake, plan=_plan(1), rate_source=_RATE, pause_s=-0.1)

    def test_env_defaults_are_used_when_params_are_none(
        self, lake: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(BACKFILL_BATCH_ENV, "1")
        monkeypatch.setenv(BACKFILL_PAUSE_ENV, "0")
        spy = _SpyEnrich()
        run_greeks_backfill(lake, plan=_plan(2), rate_source=_RATE, enrich=spy)
        assert [len(c) for c in spy.calls] == [1, 1]

    def test_workers_kwarg_is_threaded_to_the_driver_seam(self, lake: Path) -> None:
        seen: list[int | None] = []

        def _enrich(**kwargs: Any) -> GreeksEnrichReport:
            seen.append(kwargs["workers"])
            return _enrich_report()

        run_greeks_backfill(
            lake, plan=_plan(1), rate_source=_RATE, batch_size=1, workers=1, enrich=_enrich
        )
        assert seen == [1]

    def test_checkpoint_cleanup_failure_is_logged_never_raised(
        self, lake: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        plan = _plan(1)
        store = SweepCheckpointStore(
            path=checkpoint_path(lake), sweep_id=plan.backfill_id, total_combos=1
        )

        def _boom(*args: Any, **kwargs: Any) -> None:
            raise OSError("unlink refused")

        monkeypatch.setattr(type(store.path), "unlink", _boom)
        from structlog.testing import capture_logs

        with capture_logs() as logs:
            report = run_greeks_backfill(
                lake,
                plan=plan,
                rate_source=_RATE,
                batch_size=1,
                enrich=_SpyEnrich(),
                checkpoint_store=store,
            )
        assert report.batches == 1
        assert any(r["event"] == "greeks_backfill.checkpoint_cleanup_failed" for r in logs)

    def test_report_to_dict_round_trips_every_field(self, lake: Path) -> None:
        report = run_greeks_backfill(
            lake, plan=_plan(1), rate_source=_RATE, batch_size=1, enrich=_SpyEnrich()
        )
        payload = report.to_dict()
        assert payload["planned"] == 1 and payload["dry_run"] is False
        assert payload["note"] == report.note and payload["backfill_id"] == report.backfill_id
        assert set(payload) == {
            "backfill_id",
            "planned",
            "batches",
            "enriched",
            "already_current",
            "errored",
            "no_bars",
            "futures_skipped",
            "undecodable",
            "bars_written",
            "bars_solved",
            "bars_null",
            "bars_reused",
            "bars_skipped",
            "stale_null",
            "stale_v1",
            "stale_inputs_drift",
            "probe_failed",
            "resumed_from",
            "dry_run",
            "note",
        }


class TestDryRunReport:
    def test_dry_run_report_is_inert_and_honest(self) -> None:
        scan = BackfillScan(
            targets=(_T1,), stale_null=1, futures_skipped=2, undecodable=1, probe_failed=1
        )
        plan = plan_greeks_backfill(scan, rate_source=_RATE, force=False, expired_only=False)
        report = dry_run_backfill_report(plan)
        assert report.dry_run is True and report.enriched == 0 and report.bars_written == 0
        assert "DRY RUN — would re-enrich 1 contract(s)" in report.note
        assert "zero writes, zero checkpoint, zero alerts" in report.note
        assert report.futures_skipped == 2 and report.probe_failed == 1

    def test_dry_run_force_note_names_the_skipped_probe(self) -> None:
        plan = _plan_of((_T1,), force=True)
        assert "force — stamp probe skipped" in dry_run_backfill_report(plan).note
