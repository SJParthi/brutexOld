"""ROLL pins for the front-month contract resolver (DEC-FUTURES-EXEC-TRANSLATION-001).

The registry calendar is AUTHORITATIVE (never a computed last-Thursday); the
lake-partition discovery is the graceful fallback; the pick is deterministic
earliest-expiry-≥-entry-date. Exercised against a REAL ``registry.duckdb``
written with the production :class:`~brutex.data.registry_store.RegistryStore`.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

pytest.importorskip("duckdb")

from brutex.futures.contract_resolver import (
    FutContract,
    RegistryFrontMonthResolver,
    resolve_front_month,
)

_FETCHED_AT = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)


def _write_registry(
    lake: Path, contracts: list[tuple[str, str, date]], *, exchange: str = "NSE"
) -> None:
    """Write (underlying, symbol, expiry) FUT rows with the REAL RegistryStore."""
    from brutex.data.registry_store import RegistryStore

    store = RegistryStore.open(lake / "registry.duckdb")
    try:
        for underlying, symbol, expiry in contracts:
            store.upsert_contracts(
                exchange,
                underlying,
                expiry,
                [(symbol, "FUT", None)],
                run_id="test-run",
                fetched_at=_FETCHED_AT,
            )
    finally:
        store.close()


def _write_option_row(lake: Path, *, exchange: str = "NSE") -> None:
    from brutex.data.registry_store import RegistryStore

    store = RegistryStore.open(lake / "registry.duckdb")
    try:
        store.upsert_contracts(
            exchange,
            "NIFTY",
            date(2025, 1, 30),
            [("NSE-NIFTY-30Jan25-22000-CE", "CE", 22_000.0)],
            run_id="test-run",
            fetched_at=_FETCHED_AT,
        )
    finally:
        store.close()


class TestResolveFrontMonthPure:
    _JAN = FutContract("NSE-NIFTY-30Jan25-FUT", date(2025, 1, 30))
    _FEB = FutContract("NSE-NIFTY-27Feb25-FUT", date(2025, 2, 27))
    _MAR = FutContract("NSE-NIFTY-27Mar25-FUT", date(2025, 3, 27))

    def test_r14_m_mplus1_deterministic_earliest_expiry(self) -> None:
        # [R14/R7 · ROLL-2] with M and M+1 both live the EARLIEST expiry wins —
        # there is no liquidity/volume input at all, and any input order gives
        # the same pick (bit-identical across runs).
        for contracts in (
            [self._JAN, self._FEB, self._MAR],
            [self._MAR, self._JAN, self._FEB],
            [self._FEB, self._MAR, self._JAN],
        ):
            assert (
                resolve_front_month(contracts, on_date=date(2025, 1, 15)) == "NSE-NIFTY-30Jan25-FUT"
            )

    def test_r14_expiry_day_stays_on_expiring_contract(self) -> None:
        # [R14/R18 · ROLL-3] entry date == expiry date keeps the EXPIRING
        # contract live through its session (>= is inclusive; the 15:20 forced
        # exit < 15:30 close means no trade straddles expiry).
        assert (
            resolve_front_month([self._JAN, self._FEB], on_date=date(2025, 1, 30))
            == "NSE-NIFTY-30Jan25-FUT"
        )

    def test_r14_post_expiry_tail_resolves_next_month(self) -> None:
        # [R14 · ROLL-4] a trade AFTER the January expiry but still inside
        # calendar January resolves February — never the dead January row
        # (the literal calendar-month rule would demand a dead contract).
        assert (
            resolve_front_month([self._JAN, self._FEB], on_date=date(2025, 1, 31))
            == "NSE-NIFTY-27Feb25-FUT"
        )

    def test_all_expired_returns_none(self) -> None:
        assert resolve_front_month([self._JAN], on_date=date(2025, 2, 1)) is None
        assert resolve_front_month([], on_date=date(2025, 1, 1)) is None

    def test_resolver_out_of_order_duplicate_expiries_deterministic(self) -> None:
        # [R7/hardening] duplicate + out-of-order rows still pick the earliest;
        # an expiry tie breaks on the symbol string (deterministic).
        dup = [self._FEB, self._JAN, self._JAN, self._FEB]
        assert resolve_front_month(dup, on_date=date(2025, 1, 2)) == "NSE-NIFTY-30Jan25-FUT"
        tie_a = FutContract("NSE-AAA-30Jan25-FUT", date(2025, 1, 30))
        tie_b = FutContract("NSE-BBB-30Jan25-FUT", date(2025, 1, 30))
        assert (
            resolve_front_month([tie_b, tie_a], on_date=date(2025, 1, 2)) == "NSE-AAA-30Jan25-FUT"
        )

    def test_blank_contract_symbol_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-blank"):
            FutContract("  ", date(2025, 1, 30))


class TestRegistryResolver:
    def test_r14_frontmonth_follows_holiday_shifted_registry(self, tmp_path: Path) -> None:
        # [R14 · ROLL-1] the registry lists a WEDNESDAY 2025-01-29 expiry
        # (holiday-shifted off the naive last-Thursday 2025-01-30). The
        # resolver follows the REGISTRY — a last-Thursday formula would
        # produce a different (nonexistent) contract and FAIL this pin.
        _write_registry(
            tmp_path,
            [
                ("NIFTY", "NSE-NIFTY-29Jan25-FUT", date(2025, 1, 29)),
                ("NIFTY", "NSE-NIFTY-27Feb25-FUT", date(2025, 2, 27)),
            ],
        )
        resolver = RegistryFrontMonthResolver(tmp_path)
        assert (
            resolver.resolve(instrument="NIFTY", on_date=date(2025, 1, 15))
            == "NSE-NIFTY-29Jan25-FUT"
        )
        naive_last_thursday = date(2025, 1, 30)
        assert naive_last_thursday.weekday() == 3  # the formula WOULD say Thursday
        assert date(2025, 1, 29).weekday() == 2  # the registry says Wednesday

    def test_r14_holiday_shifted_expiry_verbatim_from_registry(self, tmp_path: Path) -> None:
        # [R14 · ROLL-7] the shifted date + its DDMmmYY token carry VERBATIM:
        # the resolved symbol's token decodes to exactly the registry date.
        from brutex.data.fno_symbol import parse_fno_symbol

        _write_registry(tmp_path, [("NIFTY", "NSE-NIFTY-29Jan25-FUT", date(2025, 1, 29))])
        resolver = RegistryFrontMonthResolver(tmp_path)
        symbol = resolver.resolve(instrument="NIFTY", on_date=date(2025, 1, 2))
        assert symbol == "NSE-NIFTY-29Jan25-FUT"
        parts = parse_fno_symbol(symbol)
        assert parts is not None
        assert parts.expiry == date(2025, 1, 29)

    def test_option_rows_filtered_out(self, tmp_path: Path) -> None:
        # A registry holding ONLY option legs yields no FUT calendar — the
        # resolver falls through to lake discovery (empty here) → None.
        _write_option_row(tmp_path)
        resolver = RegistryFrontMonthResolver(tmp_path)
        assert resolver.resolve(instrument="NIFTY", on_date=date(2025, 1, 2)) is None

    def test_empty_month_refuses_honestly(self, tmp_path: Path) -> None:
        # [R14] an empty registry month (every row expired) → None → the
        # executor's NO_CONTRACT names the trade; nothing is fabricated.
        _write_registry(tmp_path, [("NIFTY", "NSE-NIFTY-30Jan25-FUT", date(2025, 1, 30))])
        resolver = RegistryFrontMonthResolver(tmp_path)
        assert resolver.resolve(instrument="NIFTY", on_date=date(2025, 6, 1)) is None

    def test_calendar_cached_once_per_underlying(self, tmp_path: Path) -> None:
        _write_registry(tmp_path, [("NIFTY", "NSE-NIFTY-30Jan25-FUT", date(2025, 1, 30))])
        resolver = RegistryFrontMonthResolver(tmp_path)
        assert resolver.resolve(instrument="NIFTY", on_date=date(2025, 1, 2)) is not None
        # Delete the registry — the cached calendar still answers (ONE load).
        (tmp_path / "registry.duckdb").unlink()
        assert (
            resolver.resolve(instrument="NIFTY", on_date=date(2025, 1, 10))
            == "NSE-NIFTY-30Jan25-FUT"
        )

    def test_sensex_resolves_on_bse(self, tmp_path: Path) -> None:
        _write_registry(
            tmp_path, [("SENSEX", "BSE-SENSEX-28Jan25-FUT", date(2025, 1, 28))], exchange="BSE"
        )
        resolver = RegistryFrontMonthResolver(tmp_path)
        assert (
            resolver.resolve(instrument="SENSEX", on_date=date(2025, 1, 2))
            == "BSE-SENSEX-28Jan25-FUT"
        )


def _make_lake_futures_dirs(lake: Path, names: list[str], *, exchange: str = "NSE") -> None:
    for name in names:
        (lake / "bars" / exchange / "FNO" / name / "1minute").mkdir(parents=True, exist_ok=True)


class TestLakeDiscoveryFallback:
    def test_registry_absent_falls_back_to_lake_partition_discovery(self, tmp_path: Path) -> None:
        # [R14/Golden#1 · ROLL-10] no registry.duckdb at all: the on-disk FNO
        # partition dirs ARE the calendar (dir name == contract_symbol).
        _make_lake_futures_dirs(
            tmp_path,
            [
                "NSE-NIFTY-30Jan25-FUT",
                "NSE-NIFTY-27Feb25-FUT",
                "NSE-NIFTY-30Jan25-22000-CE",  # option dir — skipped
                "not-a-contract",  # junk — skipped honestly
            ],
        )
        resolver = RegistryFrontMonthResolver(tmp_path)
        assert (
            resolver.resolve(instrument="NIFTY", on_date=date(2025, 1, 15))
            == "NSE-NIFTY-30Jan25-FUT"
        )

    def test_r14_registry_error_falls_back_to_lake_discovery(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # [R14/Golden#1 · ROLL-10] a broken registry (RegistryError on open)
        # WARNs and resolves the SAME front month from the lake dirs.
        import brutex.data.registry_store as registry_store_module
        from brutex.data.registry_store import RegistryError

        _write_registry(tmp_path, [("NIFTY", "NSE-NIFTY-30Jan25-FUT", date(2025, 1, 30))])
        _make_lake_futures_dirs(tmp_path, ["NSE-NIFTY-30Jan25-FUT"])

        def _broken(path: object) -> object:
            raise RegistryError("simulated corrupt registry")

        monkeypatch.setattr(registry_store_module, "open_registry", _broken)
        from structlog.testing import capture_logs

        resolver = RegistryFrontMonthResolver(tmp_path)
        with capture_logs() as logs:
            symbol = resolver.resolve(instrument="NIFTY", on_date=date(2025, 1, 15))
        assert symbol == "NSE-NIFTY-30Jan25-FUT"
        assert "futures_resolver.registry_error" in [x["event"] for x in logs]

    def test_no_registry_no_lake_returns_none(self, tmp_path: Path) -> None:
        resolver = RegistryFrontMonthResolver(tmp_path)
        assert resolver.resolve(instrument="NIFTY", on_date=date(2025, 1, 2)) is None

    def test_other_underlying_dirs_do_not_leak(self, tmp_path: Path) -> None:
        _make_lake_futures_dirs(tmp_path, ["NSE-BANKNIFTY-30Jan25-FUT"])
        resolver = RegistryFrontMonthResolver(tmp_path)
        assert resolver.resolve(instrument="NIFTY", on_date=date(2025, 1, 2)) is None

    def test_stray_file_in_fno_dir_skipped(self, tmp_path: Path) -> None:
        _make_lake_futures_dirs(tmp_path, ["NSE-NIFTY-30Jan25-FUT"])
        (tmp_path / "bars" / "NSE" / "FNO" / "stray.txt").write_text("junk")
        resolver = RegistryFrontMonthResolver(tmp_path)
        assert (
            resolver.resolve(instrument="NIFTY", on_date=date(2025, 1, 2))
            == "NSE-NIFTY-30Jan25-FUT"
        )
