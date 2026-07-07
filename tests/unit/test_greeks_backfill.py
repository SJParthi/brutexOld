# pyright: reportPrivateUsage=none
"""Unit tests for :mod:`brutex.data.greeks_backfill` (DEC-GREEKS-BULK-REENRICH-001).

100% line + branch on the bulk v1→v2 re-enrich backfill: the scanner
classification matrix (unstamped / v1 / inputs-drift / current / futures /
undecodable / expired-only / limit / force / corrupt-file / no-files / pause),
the content-addressed plan id (determinism + sensitivity), the env resolvers'
fail-loud contract, and the runner's batch loop (slicing, checkpoint
record/ignore/continue/delete, resume, halt escape, progress hook, pause,
empty plan, dry-run report).

Test types: unit (every branch); the exported staleness-predicate truth table
(``stamp_is_current_inputs`` / ``current_inputs_pack``) lives with its OWN
module's suite in ``tests/unit/test_greeks_enrich.py`` (the per-module
coverage-gate stem convention); the hypothesis properties live in
``tests/property/test_greeks_backfill_properties.py`` and the end-to-end lake
round-trips in ``tests/integration/test_greeks_bulk_reenrich.py``.
"""

from __future__ import annotations

import json
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
    CHECKPOINT_DIR_NAME,
    DEFAULT_BACKFILL_BATCH_SIZE,
    BackfillPlan,
    BackfillScan,
    _contract_window,
    _month_files,
    _rate_source_value_fingerprint,
    _stamped_rows_fixable,
    checkpoint_path,
    dry_run_backfill_report,
    plan_greeks_backfill,
    resolve_backfill_batch_size,
    resolve_backfill_pause_s,
    revision_marker_path,
    run_greeks_backfill,
    scan_stale_greeks_targets,
)
from brutex.data.greeks_enrich import (
    RATE_SOURCE_ID_FIXED,
    STATUS_OK,
    STYLE_EUROPEAN_EXACT,
    GreeksEnrichReport,
    GreeksEnrichTarget,
    pack_greeks_provenance,
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


def _write_month_prices(
    lake: Path,
    symbol: str,
    year: int,
    month: int,
    pids: list[int | None],
    closes: list[float | None],
    *,
    interval: str = "1minute",
    exchange: str = "NSE",
) -> Path:
    """Write a month partition with REAL OHLC columns (fix-round-4 fixability probes)."""
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
    n = len(pids)
    frame = pl.DataFrame(
        {
            "open": pl.Series([100.0] * n, dtype=pl.Float64),
            "high": pl.Series([101.0] * n, dtype=pl.Float64),
            "low": pl.Series([99.0] * n, dtype=pl.Float64),
            "close": pl.Series(closes, dtype=pl.Float64),
            "greeks_provenance_id": pl.Series(pids, dtype=pl.Int32),
        }
    )
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

    def test_expired_only_boundary_expiry_today_is_still_live(self, lake: Path) -> None:
        """A contract expiring TODAY trades until 15:30 IST — LIVE, never expired.

        Exact-boundary regression pin (refuter-confirmed): kills the
        ``expiry >= today`` → ``expiry > today`` mutant, which survived the
        whole suite because no fixture seeded ``expiry == today``.
        """
        today_ce = "NSE-NIFTY-06Jul26-21500-CE"  # expiry == the injected _TODAY
        _write_month(lake, today_ce, 2026, 7, [None])
        scan = _scan(lake, expired_only=True)
        assert scan.live_skipped == 1 and scan.targets == ()

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

    def test_force_with_limit_is_refused_loudly(self, lake: Path) -> None:
        """force+limit never progresses (stamp-independent selection re-picks the
        SAME head every run) — refused, never a silent chipping illusion."""
        _write_month(lake, _CE, 2024, 1, [None])
        with pytest.raises(ValueError, match="force combined with limit never progresses"):
            _scan(lake, force=True, limit=1)

    def test_schema_poisoned_utf8_stamp_column_is_probe_failed_not_fatal(self, lake: Path) -> None:
        """A wrong-dtype ``greeks_provenance_id`` (utf8) folds into probe_failed.

        Refuter-confirmed: the ``int("poison")`` coercion previously raised an
        uncaught ``ValueError`` that killed the WHOLE scan — the
        DEC-MOVERS-CORRUPT-SKIP-001 one-corrupt-file class.
        """
        from structlog.testing import capture_logs

        poisoned = _write_month(lake, _CE, 2024, 1, [_CURRENT_PID])
        pl.DataFrame({"greeks_provenance_id": pl.Series(["poison"], dtype=pl.Utf8)}).write_parquet(
            poisoned
        )
        _write_month(lake, _PE, 2024, 1, [_V1_PID])  # the healthy sibling still scans
        with capture_logs() as logs:
            scan = _scan(lake)
        assert scan.probe_failed == 1 and scan.current == 1
        assert [t.symbol for t in scan.targets] == [_PE]
        assert any(r["event"] == "greeks_backfill.probe_skip" and "dtype" in r for r in logs)

    def test_schema_poisoned_nan_float_stamp_column_is_probe_failed_not_fatal(
        self, lake: Path
    ) -> None:
        """A non-null NaN float stamp (bypasses the null fast path) never crashes."""
        poisoned = _write_month(lake, _CE, 2024, 1, [_CURRENT_PID])
        pl.DataFrame(
            {"greeks_provenance_id": pl.Series([float("nan")], dtype=pl.Float64)}
        ).write_parquet(poisoned)
        scan = _scan(lake)
        assert scan.probe_failed == 1 and scan.targets == () and scan.current == 1

    @pytest.mark.parametrize("poison", [float("inf"), float("-inf"), 1e999])
    def test_schema_poisoned_inf_float_stamp_column_is_probe_failed_not_fatal(
        self, lake: Path, poison: float
    ) -> None:
        """A non-null inf/1e999 float stamp folds into probe_failed, never a crash.

        Fix round 6 (refuter-confirmed): ``int(float("inf"))`` raises
        ``OverflowError`` — neither ``ValueError`` nor ``TypeError`` — so the
        round-1 schema-poisoned-stamp catch let ONE inf-stamped month file
        kill the WHOLE scan with a raw traceback, recurring on every run until
        the file was manually repaired (the persistent-DoS class the round-1
        fix claims closed; the sibling NaN stamp in the SAME code path maps to
        ``ValueError`` and was already handled — the exact asymmetry).
        """
        from structlog.testing import capture_logs

        poisoned = _write_month(lake, _CE, 2024, 1, [_CURRENT_PID])
        pl.DataFrame({"greeks_provenance_id": pl.Series([poison], dtype=pl.Float64)}).write_parquet(
            poisoned
        )
        _write_month(lake, _PE, 2024, 1, [_V1_PID])  # the healthy sibling still scans
        with capture_logs() as logs:
            scan = _scan(lake)
        assert scan.probe_failed == 1 and scan.current == 1
        assert [t.symbol for t in scan.targets] == [_PE]
        assert any(r["event"] == "greeks_backfill.probe_skip" and "dtype" in r for r in logs)

    @pytest.mark.parametrize("poison", [float("nan"), float("inf"), float("-inf")])
    def test_nonfinite_ohlc_null_stamp_is_stale_unfixable_never_targeted(
        self, lake: Path, poison: float
    ) -> None:
        """The fix-round-4 non-convergence regression (refuter-confirmed).

        A null-stamped row whose OHLC carries a non-finite value is one the
        driver's per-bar IVX-2 guard SKIPS without a write — the old probe
        classified it ``stale_null``, so the contract was re-targeted on EVERY
        scan while the driver honestly reported it ``already_current``: the
        census never converged and the run read a dishonest green. The probe
        now mirrors the driver's skip predicate: such a contract classifies
        ``stale_unfixable``, is NEVER targeted, and is logged loudly.
        """
        from structlog.testing import capture_logs

        _write_month_prices(lake, _CE, 2024, 1, [_CURRENT_PID, None], [100.0, poison])
        with capture_logs() as logs:
            scan = _scan(lake)
        assert scan.targets == ()
        assert scan.stale_unfixable == 1
        assert scan.stale_null == 0 and scan.current == 0
        assert any(r["event"] == "greeks_backfill.contract_unfixable" for r in logs)
        # Convergence: a re-scan reproduces the SAME stable census — never a
        # re-queued target (the finding's forever-loop).
        rescan = _scan(lake)
        assert rescan.targets == () and rescan.stale_unfixable == 1

    def test_mixed_fixable_and_unfixable_null_rows_still_target_stale_null(
        self, lake: Path
    ) -> None:
        """One driver-stampable null row is enough to target the contract."""
        _write_month_prices(lake, _CE, 2024, 1, [None, None], [float("nan"), 100.0])
        scan = _scan(lake)
        assert [t.symbol for t in scan.targets] == [_CE]
        assert scan.stale_null == 1 and scan.stale_unfixable == 0

    def test_unfixable_file_with_v1_sibling_stamp_still_targets_v1(self, lake: Path) -> None:
        """An unfixable null never masks a genuinely-stale NON-null stamp."""
        _write_month_prices(lake, _CE, 2024, 1, [_V1_PID, None], [100.0, float("nan")])
        scan = _scan(lake)
        assert [t.symbol for t in scan.targets] == [_CE]
        assert scan.stale_v1 == 1 and scan.stale_unfixable == 0

    @pytest.mark.parametrize(
        ("stale_pid", "census"),
        [(_V1_PID, "stale_v1"), (_DRIFT_PID, "stale_inputs_drift")],
        ids=["v1", "drift"],
    )
    def test_stale_stamp_on_nonfinite_ohlc_row_is_stale_unfixable_never_targeted(
        self, lake: Path, stale_pid: int, census: str
    ) -> None:
        """The fix-round-6 non-convergence regression (refuter-confirmed).

        Fix round 4 gated the fixability probe on ``null_count() > 0`` only,
        so a NON-null stale stamp (v1 / inputs-drift) sitting on the SAME
        driver-skippable non-finite-OHLC row took the stale-stamp path with no
        actionability check: the driver skipped the row BEFORE its reuse gate,
        ``changed == 0`` read ``already_current``, the stale stamp persisted,
        and the contract was re-targeted on EVERY scan under a ✅/EXIT_OK
        false green — the exact eternal re-feed the round-4 fix closed for the
        null variant. Such a contract now classifies ``stale_unfixable``.
        """
        from structlog.testing import capture_logs

        _write_month_prices(lake, _CE, 2024, 1, [_CURRENT_PID, stale_pid], [100.0, float("nan")])
        with capture_logs() as logs:
            scan = _scan(lake)
        assert scan.targets == ()
        assert scan.stale_unfixable == 1
        assert getattr(scan, census) == 0 and scan.current == 0
        assert any(r["event"] == "greeks_backfill.contract_unfixable" for r in logs)
        # Convergence: a re-scan reproduces the SAME stable census — never a
        # re-queued target (the finding's forever-loop).
        rescan = _scan(lake)
        assert rescan.targets == () and rescan.stale_unfixable == 1

    def test_mixed_fixable_and_unfixable_stale_rows_still_target(self, lake: Path) -> None:
        """One driver-stampable stale-stamped row is enough to target the contract."""
        _write_month_prices(lake, _CE, 2024, 1, [_V1_PID, _V1_PID], [float("nan"), 100.0])
        scan = _scan(lake)
        assert [t.symbol for t in scan.targets] == [_CE]
        assert scan.stale_v1 == 1 and scan.stale_unfixable == 0

    def test_unfixable_stale_file_does_not_mask_stale_sibling_month(self, lake: Path) -> None:
        """An all-skippable stale month never masks a genuinely-stale sibling month."""
        _write_month_prices(lake, _CE, 2024, 1, [_V1_PID], [float("nan")])
        _write_month_prices(lake, _CE, 2024, 2, [_V1_PID], [100.0])
        scan = _scan(lake)
        assert [t.symbol for t in scan.targets] == [_CE]
        assert scan.stale_v1 == 1 and scan.stale_unfixable == 0
        assert scan.files_probed == 2

    def test_mixed_v1_and_drift_stamps_in_one_file_classify_first_stale(self, lake: Path) -> None:
        """Two distinct stale kinds in one file: first row-order stale wins the
        census label; the contract is targeted exactly once either way."""
        _write_month_prices(lake, _CE, 2024, 1, [_V1_PID, _DRIFT_PID], [100.0, 100.0])
        scan = _scan(lake)
        assert [t.symbol for t in scan.targets] == [_CE]
        assert scan.stale_v1 == 1 and scan.stale_inputs_drift == 0

    def test_null_price_null_stamp_stays_stale_null(self, lake: Path) -> None:
        """A NULL price errors the target LOUDLY in the driver — so re-feed it."""
        _write_month_prices(lake, _CE, 2024, 1, [None], [None])
        scan = _scan(lake)
        assert [t.symbol for t in scan.targets] == [_CE]
        assert scan.stale_null == 1 and scan.stale_unfixable == 0

    def test_junk_typed_price_column_stays_stale_null(self, lake: Path) -> None:
        """A junk-typed price column errors the target LOUDLY in the driver — re-feed."""
        dest = _write_month_prices(lake, _CE, 2024, 1, [None], [1.0])
        pl.DataFrame(
            {
                "open": pl.Series(["poison"], dtype=pl.Utf8),
                "high": pl.Series(["poison"], dtype=pl.Utf8),
                "low": pl.Series(["poison"], dtype=pl.Utf8),
                "close": pl.Series(["poison"], dtype=pl.Utf8),
                "greeks_provenance_id": pl.Series([None], dtype=pl.Int32),
            }
        ).write_parquet(dest)
        scan = _scan(lake)
        assert [t.symbol for t in scan.targets] == [_CE]
        assert scan.stale_null == 1 and scan.stale_unfixable == 0

    def test_fixability_probe_fails_open_without_visible_null_rows(self, lake: Path) -> None:
        """The defensive no-null-rows arm fails OPEN (re-feed the driver)."""
        dest = _write_month_prices(lake, _CE, 2024, 1, [_CURRENT_PID], [100.0])
        assert _stamped_rows_fixable(dest) is True

    def test_fixability_probe_fails_open_without_visible_stale_rows(self, lake: Path) -> None:
        """The stale-pid selector's no-visible-rows arm ALSO fails OPEN."""
        dest = _write_month_prices(lake, _CE, 2024, 1, [_CURRENT_PID], [100.0])
        assert _stamped_rows_fixable(dest, [_V1_PID]) is True

    def test_scan_beats_the_stall_watchdog_per_contract_and_per_file(self, lake: Path) -> None:
        """The O(files) scan feeds the pull stall watchdog (refuter-confirmed):

        beat-less it false-tripped the dashboard job's 600 s watchdog into a
        CRITICAL "pull halted" alert + stalled-suspect on a healthy backfill
        (the DEC-PIPELINE-STALL-HEARTBEAT-001 class).
        """
        from brutex.observability.progress_beat import (
            register_progress_beat,
            unregister_progress_beat,
        )

        _write_month(lake, _CE, 2024, 1, [_CURRENT_PID])
        _write_month(lake, _CE, 2024, 2, [_CURRENT_PID])
        _write_month(lake, _PE, 2024, 1, [_CURRENT_PID])
        beats: list[None] = []
        handle = register_progress_beat(lambda: beats.append(None))
        try:
            scan = _scan(lake)
        finally:
            unregister_progress_beat(handle)
        assert scan.files_probed == 3
        # ≥ one beat per ENUMERATED symbol dir (2 — the manifest-absent walk
        # fallback, fix round 2: enumeration fully materialises BEFORE the
        # first per-contract beat and was the beat-less 29.7-minute segment on
        # the production lake) + one per considered contract (2) + one per
        # probed month file (3).
        assert len(beats) >= 2 + 2 + scan.files_probed

    def test_force_scan_still_beats_per_contract(self, lake: Path) -> None:
        """force skips the probe (no per-file beats) — the per-contract beat covers it."""
        from brutex.observability.progress_beat import (
            register_progress_beat,
            unregister_progress_beat,
        )

        _write_month(lake, _CE, 2024, 1, [_CURRENT_PID])
        beats: list[None] = []
        handle = register_progress_beat(lambda: beats.append(None))
        try:
            scan = _scan(lake, force=True)
        finally:
            unregister_progress_beat(handle)
        assert scan.files_probed == 0 and len(beats) >= 1

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
        files, unreachable = _month_files(base)
        assert [(y, m) for y, m, _ in files] == [(2024, 1)]
        assert unreachable == []  # non-numeric = foreign, silently skipped

    def test_month_files_skips_out_of_range_numeric_names(self, lake: Path) -> None:
        """Numeric-but-invalid names (13.parquet / 0.parquet / year 0 / year 9999)
        are skipped like non-numeric ones — the docstring's "a foreign tree
        never raises" claim made true (refuter-confirmed ``ValueError`` crash)."""
        base = lake / "1minute"
        (base / "2024").mkdir(parents=True)
        (base / "2024" / "01.parquet").write_bytes(b"x")
        (base / "2024" / "13.parquet").write_bytes(b"x")
        (base / "2024" / "00.parquet").write_bytes(b"x")
        (base / "0000").mkdir()
        (base / "0000" / "01.parquet").write_bytes(b"x")
        # date.max.year: _contract_window's December wrap computes year + 1, so
        # 9999 must be skipped too (date(10000, …) raises).
        (base / "9999").mkdir()
        (base / "9999" / "12.parquet").write_bytes(b"x")
        files, unreachable = _month_files(base)
        assert [(y, m) for y, m, _ in files] == [(2024, 1)]
        assert unreachable == []  # out-of-range = can never hold month data

    def test_month_files_rejects_non_canonical_in_range_names(self, lake: Path) -> None:
        """Fix round 5 (refuter-confirmed): an in-range NON-zero-padded name
        (``2024/1.parquet`` / a ``023`` year dir / ``2024/001.parquet``) is one
        the driver's reader (``ist_span_partition_files``, which constructs
        ONLY ``f"{year:04d}/{month:02d}.parquet"`` candidates) can NEVER open —
        accepting it made the scanner target the contract on EVERY run while
        the wet run read zero bars (an eternal false-green re-queue). Such
        names are now returned as driver-unreachable rejects, never probed."""
        base = lake / "1minute"
        (base / "2024").mkdir(parents=True)
        (base / "2024" / "01.parquet").write_bytes(b"x")
        (base / "2024" / "1.parquet").write_bytes(b"x")  # non-padded month
        (base / "2024" / "001.parquet").write_bytes(b"x")  # over-padded month
        (base / "023").mkdir()
        (base / "023" / "01.parquet").write_bytes(b"x")  # non-padded year dir
        (base / "0023").mkdir()
        (base / "0023" / "01.parquet").write_bytes(b"x")  # canonical year 23 — readable
        files, unreachable = _month_files(base)
        assert [(y, m) for y, m, _ in files] == [(23, 1), (2024, 1)]
        assert sorted(p.parent.name + "/" + p.name for p in unreachable) == [
            "023/01.parquet",
            "2024/001.parquet",
            "2024/1.parquet",
        ]

    def test_scan_survives_a_numeric_out_of_range_month_file(self, lake: Path) -> None:
        """One foreign ``13.parquet`` must never kill the WHOLE scan (refuter repro:
        an uncaught ``ValueError: month must be in 1..12`` crashed both the CLI
        command and the dashboard job with zero census)."""
        _write_month(lake, _CE, 2024, 1, [None])
        junk = lake / "bars" / "NSE" / "FNO" / _CE / "1minute" / "2024" / "13.parquet"
        junk.write_bytes(b"junk - not even parquet")
        # A sibling whose ONLY file is out-of-range degrades to no_files.
        only_junk = lake / "bars" / "NSE" / "FNO" / _PE / "1minute" / "2024" / "00.parquet"
        only_junk.parent.mkdir(parents=True)
        only_junk.write_bytes(b"junk")
        scan = _scan(lake)
        assert [t.symbol for t in scan.targets] == [_CE]
        (target,) = scan.targets
        assert target.end == date(2024, 2, 2)  # window derived from VALID months only
        assert scan.no_files == 1

    def test_non_padded_month_file_folds_into_probe_failed_never_targeted_forever(
        self, lake: Path
    ) -> None:
        """Fix round 5 (refuter-confirmed): a stale (v1) stamp inside a
        non-canonical ``2024/1.parquet`` was targeted on EVERY scan while the
        driver — whose ``ist_span_partition_files`` reader builds ONLY
        zero-padded names — read ZERO files, wrote nothing, and the wet run
        exited 0 with a ✅ head: the stamp persisted forever and the census
        never converged. The scanner now surfaces the file LOUDLY
        (``probe_failed`` + one WARN) instead of targeting it."""
        from structlog.testing import capture_logs

        path = _write_month(lake, _CE, 2024, 1, [_V1_PID])
        path.rename(path.parent / "1.parquet")  # the refuter repro's rename
        with capture_logs() as logs:
            scan = _scan(lake)
        assert scan.targets == ()  # never targeted — the driver could not read it
        assert scan.probe_failed == 1  # surfaced in the census, not hidden
        assert scan.no_files == 1  # nothing probe-able remains for the contract
        assert scan.stale_v1 == 0 and scan.files_probed == 0
        (warn,) = [r for r in logs if r["event"] == "greeks_backfill.month_file_driver_unreachable"]
        assert warn["symbol"] == _CE and warn["count"] == 1
        assert warn["files"] == [str(path.parent / "1.parquet")]
        assert "rename" in warn["reason"]
        # Convergence: a re-scan reproduces the SAME stable census — never a
        # re-queued target (the refuter-reproduced forever-loop).
        rescan = _scan(lake)
        assert rescan.targets == () and rescan.probe_failed == 1

    def test_non_canonical_sibling_never_masks_a_canonical_stale_month(self, lake: Path) -> None:
        """A driver-unreachable reject rides ALONGSIDE the canonical months: the
        contract is still targeted off its readable stale file, with the window
        derived from canonical months only (exactly what the driver can read)."""
        _write_month(lake, _CE, 2024, 1, [_V1_PID])
        junk = lake / "bars" / "NSE" / "FNO" / _CE / "1minute" / "2024" / "2.parquet"
        junk.write_bytes(b"x")  # non-padded Feb — invisible to the driver
        scan = _scan(lake)
        assert [t.symbol for t in scan.targets] == [_CE] and scan.stale_v1 == 1
        (target,) = scan.targets
        assert target.end == date(2024, 2, 2)  # window from CANONICAL months only
        assert scan.probe_failed == 1  # the reject still surfaces in the census

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

    def test_sensitive_to_the_fixed_rate_value(self) -> None:
        """Fix round 4 (refuter-confirmed): a rate VALUE change under an
        unchanged source KIND must re-key the plan — kind-only hashing let a
        force plan (stamp-independent targets) reproduce the IDENTICAL id
        across the change, so a crashed force run's checkpoint was ADOPTED
        and the recorded prefix silently exempted from the driver's
        value-checked gate.
        """
        a = _plan_of((_T1,), rate_source=FixedRateSource(rate=0.065), force=True)
        b = _plan_of((_T1,), rate_source=FixedRateSource(rate=0.10), force=True)
        assert a.backfill_id != b.backfill_id

    def test_sensitive_to_the_csv_curve_values(self, tmp_path: Path) -> None:
        """Two same-kind CSVs with different VALUES yield different ids."""
        from brutex.options.risk_free import load_risk_free_csv

        a_csv = tmp_path / "rates.csv"
        a_csv.write_text("date,yield,tenor_days\n2024-01-01,0.065,91\n", encoding="utf-8")
        b_csv = tmp_path / "rates_b.csv"
        b_csv.write_text("date,yield,tenor_days\n2024-01-01,0.080,91\n", encoding="utf-8")
        # Same source LABEL (file name) so ONLY the curve values differ.
        a = load_risk_free_csv(a_csv)
        b = load_risk_free_csv(b_csv)
        b = type(b)(dates=b.dates, rates=b.rates, source=a.source)
        assert (
            _plan_of((_T1,), rate_source=a).backfill_id
            != _plan_of((_T1,), rate_source=b).backfill_id
        )

    def test_fingerprint_values(self, tmp_path: Path) -> None:
        """The fingerprint components are deterministic + honestly labelled."""
        from brutex.options.risk_free import load_risk_free_csv

        assert _rate_source_value_fingerprint(FixedRateSource()) == "fixed=0.065"
        csv = tmp_path / "rates.csv"
        csv.write_text("date,yield,tenor_days\n2024-01-01,0.065,91\n", encoding="utf-8")
        source = load_risk_free_csv(csv)
        fp = _rate_source_value_fingerprint(source)
        assert fp.startswith("csv=") and fp == _rate_source_value_fingerprint(source)

        class _Odd:
            def rate_on(self, on_date: date) -> None:
                return None

        assert _rate_source_value_fingerprint(_Odd()) == "unlabelled"

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

    @pytest.mark.parametrize("raw", ["inf", "-inf", "nan"])
    def test_pause_nonfinite_fails_loud(self, raw: str) -> None:
        """Fix round 4 (refuter-confirmed): ``float()`` parses inf/nan and a
        bare ``< 0`` check passes both (NaN compares False to everything) —
        ``inf`` then crashed the wet run mid-batch with a raw ``OverflowError``
        from ``time.sleep`` AFTER the full O(files) scan, and ``nan`` SILENTLY
        disabled the configured pause, the exact silent fallback the FAIL-LOUD
        env contract forbids.
        """
        with pytest.raises(ValueError, match="must be finite"):
            resolve_backfill_pause_s(env={BACKFILL_PAUSE_ENV: raw})

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

    def test_resume_never_skips_an_errored_prefix(self, lake: Path) -> None:
        """A crash-resume RE-FEEDS errored (stamps-unchanged) contracts (fix round 3).

        Refuter-confirmed: batch 1's contract ERRORED (per-contract driver
        quarantine — its stale stamps unchanged by design), batch 2 halted
        (``LakeWriteHalt`` — the module's own advertised resume path). The old
        unconditional per-batch ``record`` checkpointed PAST the errored
        contract; because an unchanged re-scan reproduces the IDENTICAL
        content-addressed ``backfill_id``, the resumed run adopted the surviving
        checkpoint, SKIPPED the errored contract entirely, and reported a
        false-clean ``errored=0`` run — bypassing the CLI's
        ``EXIT_USER_ERROR`` gate and firing the ✅ Telegram head for a plan
        with a known-errored contract. The freeze holds the high-water mark at
        the last fully-clean prefix boundary instead.
        """
        from structlog.testing import capture_logs

        plan = _plan(2)
        spy = _SpyEnrich(
            reports=[_enrich_report(enriched=0, errored=1, bars_written=0, bars_solved=0)],
            raise_on_call=2,
            raise_exc=LakeWriteHalt("disk_full"),
        )
        with capture_logs() as logs, pytest.raises(LakeWriteHalt):
            run_greeks_backfill(lake, plan=plan, rate_source=_RATE, batch_size=1, enrich=spy)
        # The freeze is LOUD (the held batch logs why the checkpoint stopped) …
        assert any(r["event"] == "greeks_backfill.checkpoint_held" for r in logs)
        # … and DURABLE: no checkpoint was ever recorded past the errored batch.
        assert not checkpoint_path(lake).exists()
        resumed = _SpyEnrich()
        report = run_greeks_backfill(
            lake, plan=plan, rate_source=_RATE, batch_size=1, enrich=resumed
        )
        # The errored contract IS re-fed (idempotent + cheap via the driver's
        # stamp+value reuse gate), then the halted one.
        assert resumed.calls == [list(plan.targets[0:1]), list(plan.targets[1:2])]
        assert report.resumed_from == 0
        assert report.enriched == 2 and report.errored == 0  # honest THIS run

    def test_checkpoint_freezes_at_the_last_fully_clean_boundary(self, lake: Path) -> None:
        """A CLEAN batch after an errored one must NOT leapfrog the checkpoint.

        The freeze is CUMULATIVE (``tallies.errored``, not the batch's own
        count): gating ``record`` on the batch-local errored count would let a
        later clean batch's monotonic ``record`` advance the high-water mark
        past the earlier errored contract anyway — the same silent skip
        through a side door.
        """
        plan = _plan(4)
        spy = _SpyEnrich(
            reports=[
                _enrich_report(enriched=1),  # batch 1 (target 0): clean → recorded
                _enrich_report(  # batch 2 (target 1): ERRORED → freeze
                    enriched=0, errored=1, bars_written=0, bars_solved=0
                ),
                _enrich_report(enriched=1),  # batch 3 (target 2): clean — must NOT record
            ],
            raise_on_call=4,
            raise_exc=LakeWriteHalt("disk_full"),  # batch 4: crash
        )
        with pytest.raises(LakeWriteHalt):
            run_greeks_backfill(lake, plan=plan, rate_source=_RATE, batch_size=1, enrich=spy)
        resumed = _SpyEnrich()
        report = run_greeks_backfill(
            lake, plan=plan, rate_source=_RATE, batch_size=1, enrich=resumed
        )
        # Resume starts at the last fully-CLEAN prefix boundary (after target
        # 0): the errored target 1 IS re-fed; only the genuinely-clean prefix
        # is skipped.
        assert resumed.calls == [
            list(plan.targets[1:2]),
            list(plan.targets[2:3]),
            list(plan.targets[3:4]),
        ]
        assert report.resumed_from == 1 and "resumed from target 1" in report.note

    def test_force_resume_across_a_rate_value_change_feeds_index_zero(self, lake: Path) -> None:
        """Fix round 4 (refuter-confirmed MAJOR): force crash → rate VALUE change
        → force re-run must feed index 0, never ``next_index``.

        Kind-only id hashing made the two force plans IDENTICAL across the
        value change, so the surviving checkpoint was ADOPTED and the recorded
        prefix — enriched under rate A — was never routed through the driver's
        rate-B value-checked gate: a false-clean ``errored=0`` green run whose
        prefix kept the wrong-rate Greeks stamped CURRENT forever (the stamp
        cannot represent a rate VALUE). The value fingerprint in the id makes
        the old checkpoint foreign, so the re-run starts fresh.
        """
        targets = _targets(4)
        plan_a = _plan_of(targets, rate_source=FixedRateSource(rate=0.065), force=True)
        crashing = _SpyEnrich(raise_on_call=2, raise_exc=LakeWriteHalt("disk_full"))
        with pytest.raises(LakeWriteHalt):
            run_greeks_backfill(
                lake,
                plan=plan_a,
                rate_source=FixedRateSource(rate=0.065),
                batch_size=2,
                enrich=crashing,
            )
        assert checkpoint_path(lake).exists()  # batch 1's clean record survived
        # The operator revises the rate VALUE (unchanged KIND) — the documented
        # --force remedy for exactly this stamp-invisible drift class.
        plan_b = _plan_of(targets, rate_source=FixedRateSource(rate=0.10), force=True)
        assert plan_b.backfill_id != plan_a.backfill_id
        resumed = _SpyEnrich()
        report = run_greeks_backfill(
            lake,
            plan=plan_b,
            rate_source=FixedRateSource(rate=0.10),
            batch_size=2,
            enrich=resumed,
        )
        # EVERY contract — including the recorded prefix — goes through the
        # driver's value-checked gate under the NEW rate.
        assert resumed.calls[0] == list(targets[0:2])
        assert report.resumed_from == 0

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

    @pytest.mark.parametrize("payload", ["[]", "null", '"hello"', "42", "true"])
    def test_non_object_json_checkpoint_is_ignored_loudly_never_a_crash(
        self, lake: Path, payload: str
    ) -> None:
        """Valid-JSON-NON-OBJECT corruption lands in the SAME loud fresh start.

        Refuter-confirmed: ``SweepCheckpoint.from_dict`` raised ``AttributeError``
        on these shapes, escaping the narrow ``except SweepCheckpointError`` and
        wedging EVERY wet run behind one planted/hand-edited file.
        """
        path = checkpoint_path(lake)
        path.parent.mkdir(parents=True)
        path.write_text(payload, encoding="utf-8")
        from structlog.testing import capture_logs

        with capture_logs() as logs:
            report = run_greeks_backfill(
                lake, plan=_plan(1), rate_source=_RATE, batch_size=1, enrich=_SpyEnrich()
            )
        assert report.resumed_from == 0 and report.batches == 1
        assert any(r["event"] == "greeks_backfill.checkpoint_ignored" for r in logs)
        # Clean completion deletes the sidecar — the poison never wedges twice.
        assert not checkpoint_path(lake).exists()

    def test_recursion_bomb_checkpoint_is_ignored_loudly_never_a_crash(self, lake: Path) -> None:
        """A deeply-nested valid-JSON checkpoint takes the LOUD fresh-start path.

        Refuter repro (fix round 2): ``json.loads`` raised ``RecursionError``
        — neither ``JSONDecodeError`` nor ``SweepCheckpointError`` — escaping
        the runner's advisory catch and crashing EVERY wet run until the
        operator deleted the file.
        """
        depth = 200_000
        path = checkpoint_path(lake)
        path.parent.mkdir(parents=True)
        path.write_text("[" * depth + "]" * depth, encoding="utf-8")
        from structlog.testing import capture_logs

        with capture_logs() as logs:
            report = run_greeks_backfill(
                lake, plan=_plan(1), rate_source=_RATE, batch_size=1, enrich=_SpyEnrich()
            )
        assert report.resumed_from == 0 and report.batches == 1
        assert any(r["event"] == "greeks_backfill.checkpoint_ignored" for r in logs)
        assert not checkpoint_path(lake).exists()  # clean completion deletes the poison

    @pytest.mark.parametrize("literal", ["1e999", "Infinity", "-Infinity"])
    def test_infinite_int_field_checkpoint_is_ignored_loudly_never_a_crash(
        self, lake: Path, literal: str
    ) -> None:
        """An Infinity/1e999 int field takes the LOUD fresh-start path.

        Refuter repro (fix round 6): ``json.loads`` default-parses all three
        literals to ``float('inf')`` and ``int(inf)`` raised a RAW
        ``OverflowError`` — outside ``from_dict``'s ``(TypeError, ValueError)``
        catch — escaping the runner's ``except SweepCheckpointError`` and
        crashing EVERY wet run (dashboard job → error state; CLI → raw
        traceback) instead of the documented graceful fresh start; the file is
        unlinked only on clean completion, so the wedge persisted until manual
        deletion — the exact class the round-2 hardening (size cap,
        RecursionError, non-object) was added to close.
        """
        path = checkpoint_path(lake)
        path.parent.mkdir(parents=True)
        path.write_text(
            '{"version": 1, "sweep_id": "x", "total_combos": ' + literal + ", "
            '"last_completed_index": 0, "updated_at": 0.0}',
            encoding="utf-8",
        )
        from structlog.testing import capture_logs

        with capture_logs() as logs:
            report = run_greeks_backfill(
                lake, plan=_plan(1), rate_source=_RATE, batch_size=1, enrich=_SpyEnrich()
            )
        assert report.resumed_from == 0 and report.batches == 1
        assert any(r["event"] == "greeks_backfill.checkpoint_ignored" for r in logs)
        assert not checkpoint_path(lake).exists()  # clean completion deletes the poison

    def test_tampered_inflated_checkpoint_never_zero_batches(self, lake: Path) -> None:
        """A same-``backfill_id`` checkpoint with an inflated total is REFUSED.

        Refuter repro (fix round 2): the store validated the index only against
        the FILE's own total, so a tampered ``total_combos=10000 /
        last_completed_index=9999`` adopted ``next_index=10000`` — the run
        silently zero-batched "complete" and DELETED the checkpoint.
        """
        plan = _plan(3)
        path = checkpoint_path(lake)
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "sweep_id": plan.backfill_id,
                    "total_combos": 10_000,
                    "last_completed_index": 9_999,
                    "updated_at": 0.0,
                }
            ),
            encoding="utf-8",
        )
        spy = _SpyEnrich()
        from structlog.testing import capture_logs

        with capture_logs() as logs:
            report = run_greeks_backfill(
                lake, plan=plan, rate_source=_RATE, batch_size=2, enrich=spy
            )
        assert report.resumed_from == 0 and report.batches == 2  # every target enriched
        assert [len(c) for c in spy.calls] == [2, 1]
        assert any(r["event"] == "greeks_backfill.checkpoint_ignored" for r in logs)

    def test_stray_file_at_the_checkpoint_dir_logs_and_continues(self, lake: Path) -> None:
        """A stray FILE at ``.greeks_backfill`` never aborts the wet run.

        Refuter repro (fix round 2): ``save()``'s parent ``mkdir`` ran outside
        the OSError → SweepCheckpointError mapping, so the ``FileExistsError``
        escaped the runner's documented "advisory — log + continue" catch and
        aborted the whole backfill after batch 1's real work — persistently,
        on every re-run.
        """
        (lake / CHECKPOINT_DIR_NAME).write_text("not a directory", encoding="utf-8")
        from structlog.testing import capture_logs

        with capture_logs() as logs:
            report = run_greeks_backfill(
                lake, plan=_plan(2), rate_source=_RATE, batch_size=1, enrich=_SpyEnrich()
            )
        assert report.batches == 2  # checkpoint loss ≠ halt (the documented contract)
        assert sum(1 for r in logs if r["event"] == "greeks_backfill.checkpoint_failed") == 2
        # The best-effort freshness marker shares the sidecar dir: same posture.
        assert sum(1 for r in logs if r["event"] == "greeks_backfill.revision_marker_failed") == 2

    def test_wet_report_carries_the_scan_census(self, lake: Path) -> None:
        """The wet report's ``stale_*``/``probe_failed`` carry the PLAN's census.

        Refuter-confirmed test-blindness (fix round 2): four mutants zeroing
        the census carry survived all 123 backfill tests — the operator-facing
        ``greeks_backfill.done`` log would have reported ``probe_failed=0`` on
        a lake with unreadable files (Golden Rule #1).
        """
        scan = BackfillScan(
            targets=_targets(2),
            stale_null=3,
            stale_v1=5,
            stale_inputs_drift=7,
            stale_unfixable=2,
            current=1,
            probe_failed=11,
            files_probed=9,
        )
        plan = plan_greeks_backfill(scan, rate_source=_RATE, force=False, expired_only=False)
        report = run_greeks_backfill(
            lake, plan=plan, rate_source=_RATE, batch_size=2, enrich=_SpyEnrich()
        )
        assert report.stale_null == 3
        assert report.stale_v1 == 5
        assert report.stale_inputs_drift == 7
        assert report.stale_unfixable == 2
        assert report.probe_failed == 11
        # The completion note surfaces the unfixable census LOUDLY (fix round
        # 4): a green note while null stamps persist on driver-skippable rows
        # would be a false clean.
        assert "2 contract(s) UNFIXABLE" in report.note
        payload = report.to_dict()
        assert payload["stale_null"] == 3 and payload["stale_v1"] == 5
        assert payload["stale_inputs_drift"] == 7 and payload["probe_failed"] == 11
        assert payload["stale_unfixable"] == 2

    def test_bar_writing_run_touches_the_marker_and_moves_the_lake_revision_token(
        self, lake: Path
    ) -> None:
        """A wet bar-writing batch moves the cross-process freshness token.

        Refuter-confirmed (fix round 2): the backfill never touches the window
        ledger, so a CLI run was INVISIBLE to a running dashboard's
        generation-keyed caches — ``/movers`` kept serving the pre-backfill
        iv/delta until an unrelated pull or a restart.
        """
        from brutex.dashboard.lake_revision import lake_revision

        before = lake_revision(lake)
        report = run_greeks_backfill(
            lake, plan=_plan(2), rate_source=_RATE, batch_size=1, enrich=_SpyEnrich()
        )
        assert report.bars_written > 0
        marker = revision_marker_path(lake)
        assert marker.exists()
        payload = json.loads(marker.read_text(encoding="utf-8"))
        assert payload["backfill_id"] == report.backfill_id
        assert payload["bars_written"] == report.bars_written
        after = lake_revision(lake)
        assert before is not None and after is not None
        assert after != before  # the dashboard probe sees the write on its next poll

    def test_zero_write_run_leaves_the_revision_marker_untouched(self, lake: Path) -> None:
        """An all-reused (zero bars written) batch fabricates NO freshness signal."""
        spy = _SpyEnrich(
            reports=[
                _enrich_report(
                    enriched=0, already_current=1, bars_written=0, bars_solved=0, bars_reused=3
                )
            ]
        )
        report = run_greeks_backfill(
            lake, plan=_plan(1), rate_source=_RATE, batch_size=1, enrich=spy
        )
        assert report.bars_written == 0
        assert not revision_marker_path(lake).exists()

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

    @pytest.mark.parametrize("pause", [float("inf"), float("-inf"), float("nan")])
    def test_explicit_nonfinite_pause_fails_loud(self, lake: Path, pause: float) -> None:
        """Fix round 4: an explicit inf/nan pause is refused EAGERLY — never an
        ``OverflowError`` from ``time.sleep(inf)`` after batch 1, never a
        silently-disabled nan pause.
        """
        with pytest.raises(ValueError, match="pause_s must be finite"):
            run_greeks_backfill(lake, plan=_plan(1), rate_source=_RATE, pause_s=pause)

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
            "stale_unfixable",
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

    def test_dry_run_note_names_the_unfixable_census(self) -> None:
        """Fix round 4: the dry-run note surfaces driver-unfixable contracts."""
        scan = BackfillScan(targets=(_T1,), stale_null=1, stale_unfixable=3)
        plan = plan_greeks_backfill(scan, rate_source=_RATE, force=False, expired_only=False)
        report = dry_run_backfill_report(plan)
        assert "3 contract(s) UNFIXABLE" in report.note
        assert report.stale_unfixable == 3


# ────────────────────────────────────────────────────────────────────────
# Module-docstring honesty (Golden Rule #10)
# ────────────────────────────────────────────────────────────────────────


class TestModuleDocstringHonesty:
    def test_engine_impact_claim_names_the_sweep_checkpoint_hardening(self) -> None:
        """Fix round 5 (refuter-confirmed): the docstring claimed "ZERO engine
        impact: nothing under brutex.engine changes semantics" — falsified by
        this change set's OWN fix round 2, which hardened
        ``brutex/engine/sweep_checkpoint.py`` with fail-loud corruption
        refusals (size cap / RecursionError mapping / same-id ``total_combos``
        mismatch / mkdir-in-mapping) that the sweep engine consumes UNCAUGHT
        (``sweep_supervisor``/``sweep_parallel`` call ``load()``), so a sweep
        now aborts loudly where it previously resumed. The claim must stay
        honest, not absolute (Golden Rule #10)."""
        import brutex.data.greeks_backfill as module

        doc = module.__doc__
        assert doc is not None
        flat = " ".join(doc.split())  # wrap-tolerant: claims span source lines
        assert "ZERO engine impact" not in flat
        assert "nothing under :mod:`brutex.engine` changes semantics" not in flat
        # The reworded sentence NAMES the real engine-visible change.
        assert "sweep_checkpoint" in flat and "fail-loud corruption refusals" in flat
        assert "NIFTY/BANKNIFTY/SENSEX" in flat  # the scope claim that IS true stays
