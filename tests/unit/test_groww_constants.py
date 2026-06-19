"""Unit tests for ``brutex.adapters.groww.constants`` (DEC-GROWW-DEPS-REINSTATE-001).

Pure-data module — these assertions pin the verified facts (21-col schema, the
interval tiers, the in-scope universe) so a silent drift fails loudly.
"""

from __future__ import annotations

from brutex.adapters.groww import constants as c


def test_instrument_csv_has_21_columns_in_verified_order() -> None:
    assert len(c.INSTRUMENT_CSV_COLUMNS) == 21
    assert c.INSTRUMENT_CSV_COLUMNS[0] == "exchange"
    assert c.INSTRUMENT_CSV_COLUMNS[3] == "groww_symbol"
    assert c.INSTRUMENT_CSV_COLUMNS[5] == "instrument_type"
    assert c.INSTRUMENT_CSV_COLUMNS[-1] == "is_intraday"


def test_chunk_window_tiers_are_disjoint() -> None:
    tiers = (
        c.CHUNK_WINDOW_30D_INTERVALS,
        c.CHUNK_WINDOW_90D_INTERVALS,
        c.CHUNK_WINDOW_180D_INTERVALS,
    )
    union: set[str] = set()
    for tier in tiers:
        assert union.isdisjoint(tier), f"overlapping interval tier: {tier & union}"
        union |= set(tier)


def test_every_groww_interval_has_a_chunk_tier() -> None:
    tiered = (
        c.CHUNK_WINDOW_30D_INTERVALS | c.CHUNK_WINDOW_90D_INTERVALS | c.CHUNK_WINDOW_180D_INTERVALS
    )
    assert set(c.INTERVAL_TO_GROWW) == tiered


def test_track1_intervals_are_in_the_interval_map() -> None:
    assert c.TRACK1_SPOT_INTERVAL in c.INTERVAL_TO_GROWW
    assert c.TRACK1_DAILY_INTERVAL in c.INTERVAL_TO_GROWW


def test_scope_excludes_commodity_and_mcx() -> None:
    assert "COMMODITY" not in c.GROWW_SEGMENTS
    assert "COMMODITY" in c.EXCLUDED_SEGMENTS
    assert "MCX" not in c.GROWW_EXCHANGES
    assert frozenset({"NSE", "BSE"}) == c.GROWW_EXCHANGES


def test_brute_force_universe_and_vix_present() -> None:
    assert frozenset({"NSE-NIFTY", "NSE-BANKNIFTY", "BSE-SENSEX"}) == c.BRUTE_FORCE_INDEX_SYMBOLS
    assert c.INDIA_VIX_SYMBOL == "NSE-INDIAVIX"
    assert c.EXPECTED_FNO_STOCK_COUNT == 212


def test_window_day_constants() -> None:
    assert (c.WINDOW_DAYS_30, c.WINDOW_DAYS_90, c.WINDOW_DAYS_180) == (30, 90, 180)


def test_unverified_gates_are_flagged() -> None:
    # The rate-limit ceiling is an OPEN probe gate — present but UNVERIFIED.
    assert isinstance(c.GROWW_RATE_LIMIT_RPS_UNVERIFIED, float)
    assert c.GROWW_DAILY_QUOTA_UNVERIFIED is None


def test_operator_directed_per_second_ceiling_stays_under_the_per_minute_governor() -> None:
    # DEC-GROWW-RATE-CEILING-8-001: the production per-second ceiling is the
    # operator-DIRECTED 8 r/s — the per-second backtesting cap itself stays officially
    # UNVERIFIED. 8 is chosen because 8×60 = 480/min sits just UNDER the 500/min
    # governor, so the per-second ceiling is the binding gate yet never overshoots
    # the per-minute cap (GROWW_RATE_LIMIT_PER_MIN).
    assert c.GROWW_RATE_LIMIT_RPS_CEILING == 8.0
    assert c.GROWW_RATE_LIMIT_PER_MIN == 500
    assert c.GROWW_RATE_LIMIT_RPS_CEILING * 60 < c.GROWW_RATE_LIMIT_PER_MIN


def test_instrument_master_url_matches_allowlist_host() -> None:
    assert c.GROWW_CSV_ALLOWED_HOST in c.GROWW_INSTRUMENT_CSV_URL
    assert c.GROWW_INSTRUMENT_CSV_URL.startswith("https://")


# ── resolve_pool_size — env override, default, clamp, fail-safe (DEC-GROWW-CLIENT-POOL-001) ──


def test_resolve_pool_size_default_when_env_unset(monkeypatch) -> None:
    monkeypatch.delenv(c.GROWW_POOL_SIZE_ENV, raising=False)
    assert c.resolve_pool_size() == c.GROWW_CLIENT_POOL_SIZE


def test_resolve_pool_size_honours_valid_env(monkeypatch) -> None:
    monkeypatch.setenv(c.GROWW_POOL_SIZE_ENV, "3")
    assert c.resolve_pool_size() == 3


def test_resolve_pool_size_allows_explicit_one(monkeypatch) -> None:
    """Setting 1 disables the pool (single connection) — the safe escape hatch."""
    monkeypatch.setenv(c.GROWW_POOL_SIZE_ENV, "1")
    assert c.resolve_pool_size() == 1


def test_resolve_pool_size_clamps_above_max(monkeypatch) -> None:
    """A value above the hard MAX falls back to the audited default (never overshoots)."""
    monkeypatch.setenv(c.GROWW_POOL_SIZE_ENV, str(c.GROWW_CLIENT_POOL_MAX + 5))
    assert c.resolve_pool_size() == c.GROWW_CLIENT_POOL_SIZE


def test_resolve_pool_size_rejects_zero_and_negative(monkeypatch) -> None:
    for bad in ("0", "-2"):
        monkeypatch.setenv(c.GROWW_POOL_SIZE_ENV, bad)
        assert c.resolve_pool_size() == c.GROWW_CLIENT_POOL_SIZE


def test_resolve_pool_size_fails_safe_on_non_integer(monkeypatch) -> None:
    for bad in ("abc", "2.5", ""):
        monkeypatch.setenv(c.GROWW_POOL_SIZE_ENV, bad)
        assert c.resolve_pool_size() == c.GROWW_CLIENT_POOL_SIZE


def test_resolve_pool_size_strips_whitespace(monkeypatch) -> None:
    monkeypatch.setenv(c.GROWW_POOL_SIZE_ENV, "  ")
    assert c.resolve_pool_size() == c.GROWW_CLIENT_POOL_SIZE


def test_pool_size_constants_are_sane() -> None:
    assert 1 <= c.GROWW_CLIENT_POOL_SIZE <= c.GROWW_CLIENT_POOL_MAX
    assert c.GROWW_CLIENT_POOL_MAX >= 1
