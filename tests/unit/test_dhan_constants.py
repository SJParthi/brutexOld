"""Unit tests for :mod:`brutex.adapters.dhan.constants` — 100% line+branch.

Pins the VERIFIED facts, the ``_UNVERIFIED`` stamps, and the two env resolvers.
"""

from __future__ import annotations

from datetime import date

import pytest
from brutex.adapters.dhan import constants as c


def test_verified_rate_limits() -> None:
    assert c.DHAN_DATA_RATE_LIMIT_RPS == 5
    assert c.DHAN_DATA_DAILY_QUOTA == 100_000
    assert c.DHAN_OPTION_CHAIN_MIN_INTERVAL_S == 3.0
    assert c.DHAN_QUOTE_RATE_LIMIT_RPS == 1
    assert c.DHAN_NON_TRADING_RATE_LIMIT_RPS == 20
    assert c.DHAN_RATE_LIMIT_RPS_CEILING == 5.0


def test_order_rate_limits_stamped_unverified() -> None:
    """The order/trading tier is OUT of the verified canon — ``_UNVERIFIED``-stamped.

    Round-1 vendor-honesty finding: the verified rate-limit canon covers only
    Data 5/s + 100k/day, option-chain 1/3s, Quote 1/s, Non-Trading 20/s. The
    order tier is recorded for structural completeness only (unused by a
    data-only adapter) and must carry the honesty suffix in its identifier.
    """
    assert c.DHAN_ORDER_RATE_LIMITS_UNVERIFIED["per_day"] == 7000
    assert "DHAN_ORDER_RATE_LIMITS_UNVERIFIED" in c.__all__
    assert not hasattr(c, "DHAN_ORDER_RATE_LIMITS")  # the un-suffixed name is GONE


def test_endpoints_and_base_url() -> None:
    assert c.DHAN_BASE_URL == "https://api.dhan.co"
    assert c.DHAN_ENDPOINT_HISTORICAL == "/v2/charts/historical"
    assert c.DHAN_ENDPOINT_INTRADAY == "/v2/charts/intraday"
    assert c.DHAN_ENDPOINT_OPTION_CHAIN == "/v2/optionchain"
    assert c.DHAN_ENDPOINT_EXPIRY_LIST == "/v2/optionchain/expirylist"
    assert c.DHAN_ENDPOINT_ROLLING_OPTION == "/v2/charts/rollingoption"
    assert c.DHAN_ENDPOINT_RENEW_TOKEN == "/v2/RenewToken"
    assert c.DHAN_ENDPOINT_PROFILE == "/v2/profile"


def test_intervals_and_windows() -> None:
    assert c.INTRADAY_INTERVAL_TO_DHAN["1minute"] == "1"
    assert c.INTRADAY_INTERVAL_TO_DHAN["60minute"] == "60"
    assert c.INTRADAY_INTERVAL_TO_DHAN["1hour"] == "60"
    assert c.DHAN_DAILY_INTERVAL == "1day"
    assert c.DHAN_DAILY_INTERVAL in c.DHAN_SUPPORTED_INTERVALS
    assert "1minute" in c.DHAN_SUPPORTED_INTERVALS
    assert c.DHAN_WINDOW_DAYS_INTRADAY == 90
    assert c.DHAN_WINDOW_DAYS_ROLLING_OPTION == 30
    assert c.DHAN_WINDOW_DAYS_DAILY_UNVERIFIED == 365


def test_segments_and_instrument_types_and_mapping() -> None:
    assert "IDX_I" in c.DHAN_SEGMENTS
    assert "NSE_FNO" in c.DHAN_SEGMENTS
    assert "OPTIDX" in c.DHAN_INSTRUMENT_TYPES
    assert c.DHAN_EXCH_SEGMENT_MAP[("NSE", "E")] == "NSE_EQ"
    assert c.DHAN_EXCH_SEGMENT_MAP[("NSE", "I")] == "IDX_I"
    assert c.DHAN_EXCH_SEGMENT_MAP[("BSE", "D")] == "BSE_FNO"


def test_scrip_master_and_hosts() -> None:
    assert c.DHAN_SCRIP_MASTER_URL.endswith("api-scrip-master.csv")
    assert c.DHAN_SCRIP_MASTER_DETAILED_URL.endswith("api-scrip-master-detailed.csv")
    assert "images.dhan.co" in c.DHAN_ALLOWED_HOSTS
    assert c.DHAN_SCRIP_HEADERS_UNVERIFIED["security_id"] == "SEM_SMST_SECURITY_ID"


def test_unverified_stamps() -> None:
    assert date(2020, 1, 1) == c.EARLIEST_HISTORY_UNVERIFIED
    assert c.DHAN_WINDOW_DAYS_DAILY_UNVERIFIED == 365
    # every UNVERIFIED-suffixed identifier carries the honesty in its NAME —
    # pin the actual attribute names on the module so a rename loses the stamp loudly
    unverified_names = [name for name in dir(c) if name.endswith("_UNVERIFIED")]
    assert "EARLIEST_HISTORY_UNVERIFIED" in unverified_names
    assert "DHAN_WINDOW_DAYS_DAILY_UNVERIFIED" in unverified_names
    assert "DHAN_SCRIP_HEADERS_UNVERIFIED" in unverified_names


def test_token_lifetime_and_ledger_namespaces() -> None:
    assert c.DHAN_TOKEN_LIFETIME_HOURS == 24
    assert c.DHAN_MAX_REAUTHS == 1
    # ledger namespaces are DISTINCT from Groww's "groww"
    assert c.DHAN_COVERAGE_MARKET == "dhan"
    assert c.DHAN_OPTION_COVERAGE_MARKET == "dhan-option"
    assert c.DHAN_FUTURE_COVERAGE_MARKET == "dhan-future"


def test_resolve_token_broker_region_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(c.DHAN_TOKEN_AWS_REGION_ENV, " us-east-1 ")
    assert c.resolve_token_broker_region() == "us-east-1"


def test_resolve_token_broker_region_aws_region_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(c.DHAN_TOKEN_AWS_REGION_ENV, raising=False)
    monkeypatch.setenv(c.AWS_REGION_ENV, "ap-southeast-1")
    assert c.resolve_token_broker_region() == "ap-southeast-1"


def test_resolve_token_broker_region_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(c.DHAN_TOKEN_AWS_REGION_ENV, raising=False)
    monkeypatch.delenv(c.AWS_REGION_ENV, raising=False)
    assert c.resolve_token_broker_region() == c.DHAN_TOKEN_AWS_REGION_DEFAULT


def test_resolve_token_broker_region_blank_env_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(c.DHAN_TOKEN_AWS_REGION_ENV, "   ")
    monkeypatch.delenv(c.AWS_REGION_ENV, raising=False)
    assert c.resolve_token_broker_region() == c.DHAN_TOKEN_AWS_REGION_DEFAULT


def test_resolve_token_param_path_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(c.DHAN_TOKEN_PARAM_ACCESS_TOKEN_ENV, " /custom/token ")
    assert (
        c.resolve_token_param_path(
            c.DHAN_TOKEN_PARAM_ACCESS_TOKEN_ENV, c.DHAN_TOKEN_PARAM_ACCESS_TOKEN_DEFAULT
        )
        == "/custom/token"
    )


def test_resolve_token_param_path_default_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(c.DHAN_TOKEN_PARAM_ACCESS_TOKEN_ENV, raising=False)
    assert (
        c.resolve_token_param_path(
            c.DHAN_TOKEN_PARAM_ACCESS_TOKEN_ENV, c.DHAN_TOKEN_PARAM_ACCESS_TOKEN_DEFAULT
        )
        == c.DHAN_TOKEN_PARAM_ACCESS_TOKEN_DEFAULT
    )


def test_resolve_token_param_path_blank_env_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(c.DHAN_TOKEN_PARAM_CLIENT_ID_ENV, "  ")
    assert (
        c.resolve_token_param_path(
            c.DHAN_TOKEN_PARAM_CLIENT_ID_ENV, c.DHAN_TOKEN_PARAM_CLIENT_ID_DEFAULT
        )
        == c.DHAN_TOKEN_PARAM_CLIENT_ID_DEFAULT
    )


class TestMutationWorkflowWiring:
    """Pin the FIVE Dhan pure-law pairs INTO ``mutation.yml``'s per-module mutmut loop.

    DEC-DHAN-ADAPTER-001 (the detectors_rust / lake_rust_stats precedent): each
    ``module:suite`` pair sits in the workflow's per-module zero-survivor loop with
    the EMPTY third (timeout) field — this deterministic pin fails if a pair is ever
    dropped. HONEST: it pins the WIRING only; the weekly run executes on its own
    schedule.
    """

    _WORKFLOW = None  # resolved lazily below (Path import kept local to the pin)

    def test_pure_law_pairs_wired_in_the_mutmut_loop(self) -> None:
        from pathlib import Path

        workflow = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "mutation.yml"
        text = workflow.read_text(encoding="utf-8")
        for module in ("constants", "errors", "parser", "chunks", "instruments"):
            pair = f'"brutex/adapters/dhan/{module}.py:tests/unit/test_dhan_{module}.py:"'
            assert pair in text, (
                f"dhan {module}.py is not wired into mutation.yml's per-module mutmut loop"
            )
