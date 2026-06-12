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


def test_instrument_master_url_matches_allowlist_host() -> None:
    assert c.GROWW_CSV_ALLOWED_HOST in c.GROWW_INSTRUMENT_CSV_URL
    assert c.GROWW_INSTRUMENT_CSV_URL.startswith("https://")
