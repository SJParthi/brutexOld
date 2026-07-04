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
    # The production per-second ceiling is the operator-DIRECTED 8 r/s (RAISED 5 → 8
    # on 2026-06-24, DEC-GROWW-RATE-CEILING-8-002) — the per-second backtesting cap
    # itself stays officially UNVERIFIED. 8 is safe because 8×60 = 480/min stays UNDER
    # the 500/min governor (the safety property: the per-second ceiling never overshoots
    # the per-minute cap GROWW_RATE_LIMIT_PER_MIN). It is a CEILING raise — it only
    # ALLOWS up to 8; the AIMD ladder still steps DOWN on 429s and recovers only as far
    # as the vendor tolerates, so the worst case is identical to 5 (self-correcting).
    assert c.GROWW_RATE_LIMIT_RPS_CEILING == 8.0
    assert c.GROWW_RATE_LIMIT_PER_MIN == 500
    # The 480 ≤ 500 invariant — the per-second ceiling stays under the per-minute governor.
    assert c.GROWW_RATE_LIMIT_RPS_CEILING * 60 == 480
    assert c.GROWW_RATE_LIMIT_RPS_CEILING * 60 <= c.GROWW_RATE_LIMIT_PER_MIN


def test_instrument_master_url_matches_allowlist_host() -> None:
    assert c.GROWW_CSV_ALLOWED_HOST in c.GROWW_INSTRUMENT_CSV_URL
    assert c.GROWW_INSTRUMENT_CSV_URL.startswith("https://")


def test_max_reauths_production_budget_is_two_and_bounded() -> None:
    # FP-1 hardening (DEC-GROWW-AUTH-HALT-001): the production re-auth budget is 2 —
    # one slot for the 06:00 IST rotation, one for its possible propagation lag —
    # and stays a SMALL bound (a dead credential must still halt promptly, never loop).
    assert c.GROWW_MAX_REAUTHS == 2
    assert 1 <= c.GROWW_MAX_REAUTHS <= 3
    assert "GROWW_MAX_REAUTHS" in c.__all__


def test_http_socket_timeouts_are_below_the_wall_clock_cap() -> None:
    # DEC-GROWW-HTTP-SOCKET-TIMEOUT-001: the SDK per-request connect + read socket
    # timeouts MUST sit comfortably BELOW the 45 s wall-clock FREEZE GUARD so a dead
    # socket fails via the socket timeout FIRST, leaving the wall-clock guard only as
    # belt-and-suspenders. Positive + finite + connect < read < 45 s (a real handshake
    # is quick; a slow ≤180-day window read is the longer bound).
    assert 0.0 < c.GROWW_HTTP_CONNECT_TIMEOUT_S < c.GROWW_HTTP_READ_TIMEOUT_S
    assert c.GROWW_HTTP_READ_TIMEOUT_S < c.GROWW_PULL_CALL_TIMEOUT_S
    assert c.GROWW_HTTP_CONNECT_TIMEOUT_S == 10.0
    assert c.GROWW_HTTP_READ_TIMEOUT_S == 30.0
    # exported for downstream import (the http_timeout seam + docs cite them).
    assert "GROWW_HTTP_CONNECT_TIMEOUT_S" in c.__all__
    assert "GROWW_HTTP_READ_TIMEOUT_S" in c.__all__


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


def test_pool_size_default_is_four_for_saturation() -> None:
    """DEC-FETCH-WRITE-OVERLAP-001 (Lever 2): the default is pinned at 4.

    A fresh-log audit proved 2 connections cap the achieved rate at ~3.2 r/s while the
    shared controller held its ceiling (time-weighted 4.96 r/s, ~1.76 r/s unused slack).
    With the ~625 ms per-contract cycle, L = λ·W ⇒ 4 connections generate 4/0.625 = 6.4 r/s
    of demand vs 2/0.625 = 3.2 — DOUBLE the demand, consuming the slack. An accidental
    revert to 2 would silently re-cap the achieved rate at ~3.2.
    """
    assert c.GROWW_CLIENT_POOL_SIZE == 4
    # 4 connections × (1 / 0.625 s cycle) = 6.4 r/s of demand — STRICTLY MORE than the
    # ~3.2 r/s the prior 2 connections achieved, so the demand-side slack is consumed.
    assert c.GROWW_CLIENT_POOL_SIZE / 0.625 > 2 / 0.625
    # HONEST (Golden Rule #10): 6.4 r/s demand is BELOW the operator-raised 8.0 ceiling, so
    # with ceiling=8 the pull is demand-limited at ~6.4 (the controller ALLOWS 8 but the
    # 4-connection demand does not reach it). Reaching a true 8 needs a shorter per-contract
    # cycle — NOT more connections (MAX=4 token-sharing cap) and NOT the append-overlap
    # (DEC-FETCH-WRITE-OVERLAP-001 Lever 1, which a write-rate benchmark refuted: a fresh
    # append is ~30 ms, not the assumed ~440 ms).
    assert c.GROWW_CLIENT_POOL_SIZE / 0.625 < c.GROWW_RATE_LIMIT_RPS_CEILING
    # 4 stays at/below the hard token-sharing cap.
    assert c.GROWW_CLIENT_POOL_SIZE <= c.GROWW_CLIENT_POOL_MAX == 4


def test_resolve_pool_size_accepts_exact_max(monkeypatch) -> None:
    """The exact MAX is ACCEPTED (off-by-one guard on the `> MAX` clamp)."""
    monkeypatch.setenv(c.GROWW_POOL_SIZE_ENV, str(c.GROWW_CLIENT_POOL_MAX))
    assert c.resolve_pool_size() == c.GROWW_CLIENT_POOL_MAX


def test_resolve_pool_size_rejects_one_above_max(monkeypatch) -> None:
    """MAX+1 is rejected → fall back to the audited default (the boundary, not MAX+5)."""
    monkeypatch.setenv(c.GROWW_POOL_SIZE_ENV, str(c.GROWW_CLIENT_POOL_MAX + 1))
    assert c.resolve_pool_size() == c.GROWW_CLIENT_POOL_SIZE


# ── resolve_smooth_pacing — env toggle, default-OFF, both ways (DEC-GROWW-SMOOTH-PACING-001) ──


def test_resolve_smooth_pacing_default_off_when_unset(monkeypatch) -> None:
    """Unset env → OFF (the safe default; smooth pacing never enabled by accident)."""
    monkeypatch.delenv(c.GROWW_SMOOTH_PACING_ENV, raising=False)
    assert c.resolve_smooth_pacing() is False


def test_resolve_smooth_pacing_truthy_values_enable(monkeypatch) -> None:
    """Each accepted truthy spelling (case-insensitive, whitespace-trimmed) → ON."""
    for raw in ("1", "true", "TRUE", "Yes", " on ", "On"):
        monkeypatch.setenv(c.GROWW_SMOOTH_PACING_ENV, raw)
        assert c.resolve_smooth_pacing() is True


def test_resolve_smooth_pacing_falsy_and_unknown_stay_off(monkeypatch) -> None:
    """Blank / "0" / "false" / any unrecognised value → OFF (fail-safe to default)."""
    for raw in ("", "  ", "0", "false", "no", "off", "maybe", "2"):
        monkeypatch.setenv(c.GROWW_SMOOTH_PACING_ENV, raw)
        assert c.resolve_smooth_pacing() is False


def test_token_disk_cache_constants() -> None:
    """DEC-GROWW-TOKEN-DISK-CACHE-001 constants are present + sane (regression pins)."""
    assert c.GROWW_TOKEN_RESET_HOUR_IST == 6  # 06:00 IST reset (CLAUDE.md §6 / ADR-0009)
    assert c.GROWW_TOKEN_RESET_HOUR_ENV == "BRUTEX_GROWW_TOKEN_RESET_HOUR_IST"
    assert c.GROWW_TOKEN_CACHE_PATH_ENV == "BRUTEX_GROWW_TOKEN_CACHE_PATH"
    # the basename MUST match the .gitignore **/groww_access_token* glob (M3)
    assert c.GROWW_TOKEN_CACHE_FILENAME.startswith("groww_access_token")
    assert c.GROWW_TOKEN_CACHE_SCHEMA == 1
    # a positive, capped back-off budget so the cold-start throttle ride-out is bounded
    assert c.GROWW_TOKEN_ISSUANCE_RETRY_ATTEMPTS >= 1
    assert c.GROWW_TOKEN_ISSUANCE_RETRY_BASE_S > 0.0
    assert c.GROWW_TOKEN_ISSUANCE_RETRY_CAP_S >= c.GROWW_TOKEN_ISSUANCE_RETRY_BASE_S


# ── token-broker resolvers — env toggle / region / param-path (DEC-TOKEN-BROKER-AWS-001) ──


def test_resolve_token_broker_region_prefers_brutex_env(monkeypatch) -> None:
    """A non-blank BRUTEX_TOKEN_AWS_REGION wins over AWS_REGION + the default."""
    monkeypatch.setenv(c.GROWW_TOKEN_AWS_REGION_ENV, " us-east-1 ")
    monkeypatch.setenv(c.AWS_REGION_ENV, "eu-west-1")
    assert c.resolve_token_broker_region() == "us-east-1"


def test_resolve_token_broker_region_falls_back_to_aws_region(monkeypatch) -> None:
    """A blank/unset BRUTEX var → the non-blank AWS_REGION is used."""
    monkeypatch.setenv(c.GROWW_TOKEN_AWS_REGION_ENV, "  ")
    monkeypatch.setenv(c.AWS_REGION_ENV, " eu-west-1 ")
    assert c.resolve_token_broker_region() == "eu-west-1"


def test_resolve_token_broker_region_default_when_both_unset(monkeypatch) -> None:
    """Both unset/blank → the ap-south-1 default (never None — a concrete region)."""
    monkeypatch.delenv(c.GROWW_TOKEN_AWS_REGION_ENV, raising=False)
    monkeypatch.delenv(c.AWS_REGION_ENV, raising=False)
    assert c.resolve_token_broker_region() == c.GROWW_TOKEN_AWS_REGION_DEFAULT == "ap-south-1"


def test_resolve_token_param_path_env_override_wins(monkeypatch) -> None:
    """A non-blank env value (whitespace-trimmed) overrides the default path."""
    monkeypatch.setenv(c.GROWW_TOKEN_PARAM_API_KEY_ENV, "  /custom/api-key  ")
    assert (
        c.resolve_token_param_path(
            c.GROWW_TOKEN_PARAM_API_KEY_ENV, c.GROWW_TOKEN_PARAM_API_KEY_DEFAULT
        )
        == "/custom/api-key"
    )


def test_resolve_token_param_path_default_when_unset_or_blank(monkeypatch) -> None:
    """Unset OR blank env → the operator default path (each of the three secrets)."""
    for env_name, default in (
        (c.GROWW_TOKEN_PARAM_ACCESS_TOKEN_ENV, c.GROWW_TOKEN_PARAM_ACCESS_TOKEN_DEFAULT),
        (c.GROWW_TOKEN_PARAM_API_KEY_ENV, c.GROWW_TOKEN_PARAM_API_KEY_DEFAULT),
        (c.GROWW_TOKEN_PARAM_TOTP_SECRET_ENV, c.GROWW_TOKEN_PARAM_TOTP_SECRET_DEFAULT),
    ):
        monkeypatch.delenv(env_name, raising=False)
        assert c.resolve_token_param_path(env_name, default) == default
        monkeypatch.setenv(env_name, "   ")
        assert c.resolve_token_param_path(env_name, default) == default


# ── resolve_smooth_pacing_mode — the TRI-STATE resolver (DEC-SMOOTH-PACING-AUTOLATCH-001) ──


def test_resolve_smooth_pacing_mode_unset_is_adaptive(monkeypatch) -> None:
    """Unset env → ADAPTIVE (the new default: latch-armed, pacing byte-identical)."""
    from brutex.adapters.rate_common import SMOOTH_PACING_ADAPTIVE

    monkeypatch.delenv(c.GROWW_SMOOTH_PACING_ENV, raising=False)
    assert c.resolve_smooth_pacing_mode() == SMOOTH_PACING_ADAPTIVE


def test_resolve_smooth_pacing_mode_truthy_is_on(monkeypatch) -> None:
    from brutex.adapters.rate_common import SMOOTH_PACING_ON

    for raw in ("1", "true", "TRUE", "Yes", " on "):
        monkeypatch.setenv(c.GROWW_SMOOTH_PACING_ENV, raw)
        assert c.resolve_smooth_pacing_mode() == SMOOTH_PACING_ON


def test_resolve_smooth_pacing_mode_explicit_falsy_is_off(monkeypatch) -> None:
    """The escape hatch: "0"/"false"/"no"/"off" disables the gate AND the latch."""
    from brutex.adapters.rate_common import SMOOTH_PACING_OFF

    for raw in ("0", "false", "No", " OFF "):
        monkeypatch.setenv(c.GROWW_SMOOTH_PACING_ENV, raw)
        assert c.resolve_smooth_pacing_mode() == SMOOTH_PACING_OFF


def test_resolve_smooth_pacing_mode_blank_or_unknown_degrades_to_adaptive(monkeypatch) -> None:
    """A bad env value never FORCES smoothing — it degrades to the safe adaptive default."""
    from brutex.adapters.rate_common import SMOOTH_PACING_ADAPTIVE

    for raw in ("", "  ", "maybe", "2"):
        monkeypatch.setenv(c.GROWW_SMOOTH_PACING_ENV, raw)
        assert c.resolve_smooth_pacing_mode() == SMOOTH_PACING_ADAPTIVE
