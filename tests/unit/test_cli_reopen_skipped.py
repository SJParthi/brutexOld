"""Tests for ``brutex reopen-skipped`` (DEC-EMPTY-WINDOW-CIRCUIT-BREAKER-001).

The operator un-cement action for ``SKIPPED`` (settled no-data) pull windows:
loads the durable window ledger, demotes matching rows to retryable ``FAILED``
(streak reset), persists atomically. Covers the happy path (ledger round-trip
through the real Parquet store), the symbol/market/limit filters, the 0-match
no-write path, and the missing-ledger refusal.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from brutex import cli
from brutex.data.fetch_coverage import WindowKey
from brutex.data.window_ledger import FAILED, SKIPPED, WindowLedger
from brutex.data.window_ledger_store import load_window_ledger, save_window_ledger

_NOW = datetime(2026, 6, 19, 8, 0, tzinfo=UTC)


def _key(symbol: str, market: str = "groww") -> WindowKey:
    return WindowKey(
        market=market,
        security_id=symbol,
        interval="1minute",
        start=date(2021, 1, 1),
        end=date(2021, 1, 31),
    )


def _seed_lake(lake_root: Path) -> None:
    """Persist a ledger with two SKIPPED spot windows + one SKIPPED option window."""
    ledger = WindowLedger()
    for symbol in ("NIFTY", "BANKNIFTY"):
        ledger.record(
            _key(symbol),
            raw_bars=0,
            error_type=None,
            bars_appended=0,
            now=_NOW,
            settle_empty=True,
        )
    ledger.record(
        _key("NSE-NIFTY-02Jan25-28500-CE", market="groww-option"),
        raw_bars=0,
        error_type=None,
        bars_appended=0,
        now=_NOW,
        settle_empty=True,
    )
    save_window_ledger(ledger, lake_root)


class TestReopenSkippedCommand:
    def test_reopens_all_skipped_and_persists(self, tmp_path: Path) -> None:
        _seed_lake(tmp_path)
        rc = cli.main(["reopen-skipped", "--lake-root", str(tmp_path)])
        assert rc == cli.EXIT_OK
        restored = load_window_ledger(tmp_path)
        assert all(r.status == FAILED for r in restored.records())
        assert all(r.last_error == "REOPENED" for r in restored.records())
        assert all(r.empty_streak == 0 for r in restored.records())
        # Un-cemented: the skip authority re-fetches every window on the next pull.
        assert not restored.is_skippable(_key("NIFTY"))

    def test_symbol_filter_scopes_the_reopen(self, tmp_path: Path) -> None:
        _seed_lake(tmp_path)
        rc = cli.main(["reopen-skipped", "--lake-root", str(tmp_path), "--symbol", "NIFTY"])
        assert rc == cli.EXIT_OK
        restored = load_window_ledger(tmp_path)
        by_symbol = {r.security_id: r.status for r in restored.records()}
        assert by_symbol["NIFTY"] == FAILED
        assert by_symbol["BANKNIFTY"] == SKIPPED  # untouched

    def test_market_filter_scopes_the_reopen(self, tmp_path: Path) -> None:
        _seed_lake(tmp_path)
        rc = cli.main(["reopen-skipped", "--lake-root", str(tmp_path), "--market", "groww-option"])
        assert rc == cli.EXIT_OK
        restored = load_window_ledger(tmp_path)
        by_market = {r.market: r.status for r in restored.records()}
        assert by_market["groww-option"] == FAILED
        assert by_market["groww"] == SKIPPED  # both spot rows untouched

    def test_limit_bounds_the_sample(self, tmp_path: Path) -> None:
        _seed_lake(tmp_path)
        rc = cli.main(["reopen-skipped", "--lake-root", str(tmp_path), "--limit", "1"])
        assert rc == cli.EXIT_OK
        restored = load_window_ledger(tmp_path)
        statuses = [r.status for r in restored.records()]
        assert statuses.count(FAILED) == 1  # the bounded re-verify sample
        assert statuses.count(SKIPPED) == 2

    def test_zero_matches_is_a_clean_noop(self, tmp_path: Path) -> None:
        _seed_lake(tmp_path)
        rc = cli.main(["reopen-skipped", "--lake-root", str(tmp_path), "--symbol", "NOSUCHSYMBOL"])
        assert rc == cli.EXIT_OK
        restored = load_window_ledger(tmp_path)
        assert all(r.status == SKIPPED for r in restored.records())  # unchanged

    def test_missing_ledger_refuses_loudly(self, tmp_path: Path) -> None:
        rc = cli.main(["reopen-skipped", "--lake-root", str(tmp_path / "empty")])
        assert rc == cli.EXIT_USER_ERROR

    def test_delta_only_lake_is_a_live_ledger_not_a_refusal(self, tmp_path: Path) -> None:
        # P2 fix-round regression (DEC-REOPEN-DELTA-AWARE-001): a base-less lake
        # holding only deltas (a first pull's mid-flight crash checkpoint,
        # DEC-LEDGER-DELTA-FLUSH-001) is a LIVE ledger — exactly the state
        # load_or_migrate_window_ledger loads via the delta fold — so the
        # command must operate on it, not refuse "no window ledger found".
        from brutex.data.window_ledger_store import append_ledger_delta, ledger_path

        ledger = WindowLedger()
        ledger.record(
            _key("NIFTY"),
            raw_bars=0,
            error_type=None,
            bars_appended=0,
            now=_NOW,
            settle_empty=True,
        )
        assert append_ledger_delta(ledger, tmp_path) == 1
        assert not ledger_path(tmp_path).is_file()  # genuinely base-less
        rc = cli.main(["reopen-skipped", "--lake-root", str(tmp_path)])
        assert rc == cli.EXIT_OK
        restored = load_window_ledger(tmp_path)
        assert [r.status for r in restored.records()] == [FAILED]  # re-opened

    def test_refuses_while_a_pull_holds_the_account_lock(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Minor M-2 (single-flight guard): while a pull holds the account-wide
        # single-flight lock, reopen-skipped REFUSES loudly instead of racing the
        # pull's own throttled ledger flushes (a mid-pull re-open would be silently
        # overwritten by the pull's next whole-file ledger persist).
        from brutex.adapters.groww.pull_singleflight import pull_singleflight_lock

        _seed_lake(tmp_path)
        with pull_singleflight_lock(tmp_path):  # "a pull is running"
            rc = cli.main(["reopen-skipped", "--lake-root", str(tmp_path)])
        assert rc == cli.EXIT_USER_ERROR
        assert "REFUSED" in capsys.readouterr().err
        # Nothing was mutated — every settle survives untouched.
        restored = load_window_ledger(tmp_path)
        assert all(r.status == SKIPPED for r in restored.records())

    def test_lock_released_after_a_normal_reopen(self, tmp_path: Path) -> None:
        # The guard holds the lock only for the ms-scale local mutation — a pull
        # starting right after a reopen acquires it cleanly.
        from brutex.adapters.groww.pull_singleflight import pull_singleflight_lock

        _seed_lake(tmp_path)
        assert cli.main(["reopen-skipped", "--lake-root", str(tmp_path)]) == cli.EXIT_OK
        with pull_singleflight_lock(tmp_path):  # would raise if still held
            pass
