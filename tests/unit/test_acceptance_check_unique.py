"""Unique-check pins: clean PASS, planted-dup FAIL, skip≠clean, abstain arms.

DEC-OPERATOR-ACCEPTANCE-VERIFY-001 (MIGRATION.md #1756).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from brutex.acceptance.checks import unique
from brutex.acceptance.model import AbstainReason, Verdict
from brutex.acceptance.selftest import _month_file, poison_duplicate_row

from tests.fakes.acceptance_twin import DAY2, make_ctx, make_twin


class TestRecentMonths:
    def test_year_boundary(self) -> None:
        got = unique.recent_months(date(2026, 2, 15), 4)
        assert got == [(2025, 11), (2025, 12), (2026, 1), (2026, 2)]


class TestUniqueCheck:
    def test_clean_twin_passes_with_counts(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        check = unique.run(make_ctx(lake))
        assert check.verdict is Verdict.PASS
        census = next(s for s in check.sub_checks if s.id == "unique.census")
        assert census.evidence["findings_total"] == 0
        assert census.evidence["units_checked"] > 0
        engine = next(s for s in check.sub_checks if s.id == "unique.engine")
        assert engine.evidence["engine_detail"]["NIFTY"] == "CLEAN"
        assert check.coverage.mode == "bounded"

    def test_planted_dup_fails_naming_offender(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        poison_duplicate_row(lake, symbol="NIFTY", day=DAY2)
        check = unique.run(make_ctx(lake))
        assert check.verdict is Verdict.FAIL
        census = next(s for s in check.sub_checks if s.id == "unique.census")
        assert census.verdict is Verdict.FAIL
        assert any("NIFTY" in path for path in census.evidence["offenders"])
        engine = next(s for s in check.sub_checks if s.id == "unique.engine")
        assert engine.verdict is Verdict.FAIL  # engine headline flips too

    def test_unreadable_file_fails_never_certifies_clean(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        _month_file(lake, "NIFTY", "1minute", DAY2).write_bytes(b"not parquet")
        check = unique.run(make_ctx(lake))
        census = next(s for s in check.sub_checks if s.id == "unique.census")
        assert census.verdict is Verdict.FAIL
        assert census.evidence["files_skipped"] >= 1
        assert census.evidence["first_skipped_path"] is not None

    def test_missing_lake_abstains(self, tmp_path: Path) -> None:
        check = unique.run(make_ctx(tmp_path / "nowhere"))
        assert check.verdict is Verdict.ABSTAIN
        assert check.sub_checks[0].abstain_reason is AbstainReason.NO_LAKE
        assert check.coverage.mode == "none"

    def test_pull_active_abstains_and_force_overrides(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        lease = lake / ".locks" / "pull_lease.json"
        lease.parent.mkdir(parents=True, exist_ok=True)
        lease.write_text("{}", encoding="utf-8")
        from datetime import UTC, datetime

        live_now = datetime.now(UTC)
        gated = unique.run(make_ctx(lake, now_utc=lambda: live_now))
        assert gated.sub_checks[0].abstain_reason is AbstainReason.PULL_IN_PROGRESS
        forced = unique.run(make_ctx(lake, now_utc=lambda: live_now, force_during_pull=True))
        assert forced.verdict is Verdict.PASS

    def test_full_mode_single_scan_engine_from_census(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        check = unique.run(make_ctx(lake, full=True))
        assert check.verdict is Verdict.PASS
        assert check.coverage.mode == "full"
        assert check.limits == ()
        engine = next(s for s in check.sub_checks if s.id == "unique.engine")
        assert engine.verdict is Verdict.PASS

    def test_empty_bars_tree_nothing_to_check(self, tmp_path: Path) -> None:
        (tmp_path / "bars").mkdir()
        check = unique.run(make_ctx(tmp_path))
        census = next(s for s in check.sub_checks if s.id == "unique.census")
        assert census.verdict is Verdict.ABSTAIN
        assert census.abstain_reason is AbstainReason.NOTHING_TO_CHECK

    def test_offender_cap_disclosed(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        poison_duplicate_row(lake, symbol="NIFTY", day=DAY2)
        check = unique.run(make_ctx(lake))
        census = next(s for s in check.sub_checks if s.id == "unique.census")
        assert census.evidence["offenders_total"] >= 1
        assert census.evidence["offenders_truncated"] is False


def _append_real_bar(lake: Path, *, minute: int) -> None:
    """One REAL-writer append to NIFTY's 1m partition (the refuter's mutation)."""
    from datetime import UTC, datetime, time, timedelta
    from decimal import Decimal

    from brutex.data.models import CandleBar
    from brutex.data.parquet_writer import ParquetWriter

    stamp = datetime.combine(DAY2, time(3, 45), tzinfo=UTC) + timedelta(minutes=minute)
    ParquetWriter(lake_root=lake).write(
        exchange="NSE",
        segment="INDEX",
        symbol="NIFTY",
        interval="1minute",
        bars=[
            CandleBar(
                timestamp=stamp,
                open=Decimal("101.00"),
                high=Decimal("101.10"),
                low=Decimal("100.90"),
                close=Decimal("101.00"),
                volume=1,
                open_interest=None,
            )
        ],
    )


class TestToctouLaw:
    """Round 1 MAJOR-1 regressions: the §12.7 fingerprint law is WIRED.

    The refuter's exact scenario — a REAL ParquetWriter append to NIFTY's 1m
    partition INSIDE one ``unique.run()`` — must never yield a PASS with
    ``moved: False`` again.
    """

    def test_mid_check_writer_retries_then_abstains(self, tmp_path: Path, monkeypatch) -> None:
        lake = make_twin(tmp_path)
        real_body = unique._run_body
        state = {"calls": 0}

        def mutating_body(ctx, **kwargs):
            state["calls"] += 1
            _append_real_bar(lake, minute=200 + state["calls"])  # moves EVERY attempt
            return real_body(ctx, **kwargs)

        monkeypatch.setattr(unique, "_run_body", mutating_body)
        check = unique.run(make_ctx(lake))
        assert state["calls"] == 2  # retried ONCE, never a third attempt
        assert check.verdict is Verdict.ABSTAIN
        sub = check.sub_checks[0]
        assert sub.abstain_reason is AbstainReason.INPUT_MOVED_DURING_CHECK
        assert any("NIFTY" in mover for mover in sub.evidence["movers"])
        assert check.input_fingerprint["moved"] is True
        assert check.input_fingerprint["retried"] is True

    def test_single_move_retries_and_serves_fresh_verdict(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        lake = make_twin(tmp_path)
        real_body = unique._run_body
        state = {"calls": 0}

        def mutating_once(ctx, **kwargs):
            state["calls"] += 1
            if state["calls"] == 1:
                _append_real_bar(lake, minute=200)
            return real_body(ctx, **kwargs)

        monkeypatch.setattr(unique, "_run_body", mutating_once)
        check = unique.run(make_ctx(lake))
        assert state["calls"] == 2
        assert check.verdict is Verdict.PASS  # the RETRIED compute's verdict
        assert check.input_fingerprint == {"moved": True, "retried": True}

    def test_clean_run_stamps_unmoved_fingerprint(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        check = unique.run(make_ctx(lake))
        assert check.input_fingerprint == {"moved": False, "retried": False}

    def test_fingerprint_covers_contracts_and_options_tree(self, tmp_path: Path) -> None:
        from datetime import UTC, datetime
        from decimal import Decimal

        from brutex.acceptance.selftest import _twin_bars
        from brutex.data.models import OptionBar
        from brutex.data.option_parquet_writer import OptionParquetWriter
        from brutex.data.parquet_writer import ParquetWriter

        lake = make_twin(tmp_path)
        ParquetWriter(lake_root=lake).write(
            exchange="NSE",
            segment="FNO",
            symbol="NSE-NIFTY-30Jul26-FUT",
            interval="1minute",
            bars=_twin_bars(DAY2, base=200.0),
        )
        from brutex.data.option_parquet_writer import OptionSeriesCoordinate

        OptionParquetWriter(lake_root=lake).write(
            partition_key="NSE_FNO|13|WEEK|0|ATM|CALL",
            coordinate=OptionSeriesCoordinate(
                exch_id="NSE",
                exchange_segment="NSE_FNO",
                security_id=13,
                underlying_symbol="NIFTY",
                expiry_flag="WEEK",
                expiry_code=0,
                strike_offset="ATM",
            ),
            bars=[
                OptionBar(
                    timestamp=datetime(2026, 7, 9, 3, 45, tzinfo=UTC),
                    open=Decimal("1.00"),
                    high=Decimal("1.10"),
                    low=Decimal("0.90"),
                    close=Decimal("1.00"),
                    volume=1,
                    open_interest=None,
                    side="CE",
                    strike=Decimal("24000.00"),
                    spot=Decimal("23990.00"),
                    iv=Decimal("0.15"),
                )
            ],
        )
        check = unique.run(make_ctx(lake))
        assert check.verdict is Verdict.PASS
        assert any("NSE-NIFTY-30Jul26-FUT" in key for key in check.coverage.selection)
        # both trees contributed fingerprinted inputs → clean stamp
        assert check.input_fingerprint == {"moved": False, "retried": False}
