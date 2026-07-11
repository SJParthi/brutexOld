"""Root pytest configuration for BruteX."""

from __future__ import annotations

import os
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from brutex.adapters.groww.constants import GROWW_TOKEN_CACHE_PATH_ENV
from brutex.calendars.market_hours import MARKET_HOURS_GUARD_ENV
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from hypothesis import HealthCheck
from hypothesis import settings as _hyp_settings

# DEC-TEST-APPARATUS-DEBTS-001(b): EXPLICIT CI determinism. Honesty note
# (Golden Rule #1): hypothesis >=6.113 ALREADY auto-loads its own built-in
# "ci" profile (derandomize=True) whenever a CI env var (CI /
# GITHUB_ACTIONS / ...) is set, so GHA property runs were implicitly
# derandomized even before this block existed. This block's real job is to
# make the selection EXPLICIT and env-driven, and to pin the profile's
# fields IN-REPO independent of upstream's built-in: every field is set
# explicitly (never inherited from the ambient default at import time), so
# the profile is environment-invariant — a local `HYPOTHESIS_PROFILE=ci`
# run is field-identical to CI. Per-test @settings decorators inherit
# UNSPECIFIED fields from the loaded profile, so derandomize=True
# propagates through every hypothesis test with zero per-file edits.
# Selected via HYPOTHESIS_PROFILE=ci (ci.yml / verify-m4pro.yml /
# mutation.yml). SANCTIONED FLIP: a CI-env run that does NOT set
# HYPOTHESIS_PROFILE (stress-nightly + the other scheduled arms) loads
# "default" here, OVERRIDING hypothesis's auto-loaded built-in ci profile —
# those arms are deliberately RANDOM (exploration off the PR path; before
# this block they were implicitly derandomized by the auto-load). Currently
# vacuous — no @given tests live in those suites — but stated honestly.
# Pinned by tests/unit/test_hypothesis_ci_profile.py (fields + source pin +
# CI-env-stripped subprocess selection pin + run-twice draw identity).
_hyp_settings.register_profile(
    "ci",
    derandomize=True,
    print_blob=True,
    deadline=None,
    database=None,
    suppress_health_check=(HealthCheck.too_slow,),
)
_hyp_settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "default"))

# DEC-GROWW-MARKET-HOURS-GUARD-001 test hermeticity (the #1380 philosophy —
# hermetic against the real HOST, here the WALL CLOCK instead of the disk):
# the market-hours START gate's production default (`guard_active_default`)
# reads the REAL clock + `os.environ`, so any test that starts a
# `BackgroundPullJob` / drives a pull route WITHOUT injecting a guard would
# pass out-of-hours and FAIL between 08:45-15:35 IST on an NSE trading day (a
# wall-clock flake class over the whole suite — reproduced locally 2026-07-10
# 12:02 IST: 13 untouched `test_dashboard_pull_stall.py` starts deferred).
# The suite therefore runs under the DOCUMENTED operator kill switch
# (`BRUTEX_MARKET_HOURS_GUARD=0`, `setdefault` so an explicit outer choice
# wins). The guard's OWN tests stay fully armed on both arms — they inject
# `env=` mappings / guard callables / monkeypatched clocks explicitly
# (test_market_hours.py, the gate tests in test_pull_jobs.py /
# test_cli_retry_storm.py / test_nightly_pipeline_binding.py) — so this
# default disables ONLY the ambient real-clock read, never the tested law.
os.environ.setdefault(MARKET_HOURS_GUARD_ENV, "0")


@pytest.fixture
def _inert_sweep_logging_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the CLI dispatch-level sweep logging guard inert (opt-in).

    The ``main()`` sweep dispatch configures PROCESS-GLOBAL structlog state
    (DEC-SWEEP-LOG-VOLUME-GUARD-001). Test files that drive
    ``main([... "sweep*" ...])`` for OTHER behavior opt in via a module-level
    ``pytestmark = pytest.mark.usefixtures("_inert_sweep_logging_guard")`` so
    their assertions stay byte-identical and no global logging config leaks
    into later tests in the same process. The guard's own behavior is pinned
    in ``tests/unit/test_cli_sweep_log_level.py``.
    """
    from brutex import cli

    monkeypatch.setattr(cli, "_configure_sweep_logging", lambda: None)


@pytest.fixture(autouse=True)
def _isolate_groww_token_cache(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Redirect the on-disk Groww daily-token cache (DEC-GROWW-TOKEN-DISK-CACHE-001) at a
    per-test tmp file for the WHOLE suite, so no test ever reads/writes the real
    ``~/.brutex/cache/groww_access_token.json``.

    The cache-first cold-start ``SdkRefresher`` build (and every ``build_live_client`` /
    ``build_live_pool`` that constructs one) would otherwise touch the operator's home
    cache — leaking a fake test token to disk and making a token-bearing run order-
    dependent. A fresh, unwritten tmp path is always a clean cache MISS, so the byte-
    identical mint-at-construction behaviour the legacy tests pin is preserved. Set via
    the public ``BRUTEX_GROWW_TOKEN_CACHE_PATH`` env override (a per-test module/file
    fixture can still re-point it; ``monkeypatch`` auto-restores at teardown).
    """
    cache = tmp_path_factory.mktemp("groww-token-cache") / "groww_access_token.json"
    monkeypatch.setenv(GROWW_TOKEN_CACHE_PATH_ENV, str(cache))


@pytest.fixture(autouse=True)
def _testclient_loopback_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point every ``TestClient`` at ``base_url="http://localhost"`` by default (R2-DS-F1).

    The dashboard's production ``Host`` allowlist is loopback-ONLY: the
    ``testserver`` convenience name was removed from
    ``brutex.dashboard.app.DEFAULT_ALLOWED_HOSTS`` because an attacker who can
    make that single-label name resolve to the operator machine (malicious
    DHCP search-domain / intranet DNS — the rebinding class the guard closes)
    would bypass it. Starlette's ``TestClient`` still defaults to
    ``http://testserver``, so this fixture re-points the DEFAULT at the
    allowlisted ``http://localhost`` for the whole suite — the test-side home
    of the old convenience. A test that passes its own ``base_url``
    (positional or keyword) is left untouched, and tests may still inject
    ``allowed_hosts`` into ``build_app`` explicitly. No-op when starlette is
    not installed (suites that never touch the dashboard).

    WebSocket twin: ``TestClient.websocket_connect`` HARDCODES
    ``urljoin("ws://testserver", url)`` (starlette ``testclient.py``, ignoring
    ``base_url`` entirely), so the WS handshake's ``Host`` header reads
    ``testserver`` and the loopback-only ``TrustedHostMiddleware`` would 400
    every suite WS connect. The same patch discipline applies: default the
    handshake ``host`` header to ``localhost`` unless the test supplies its
    own (a test probing the guard itself passes an explicit ``host``).
    """
    try:
        from starlette.testclient import TestClient
    except ImportError:
        return
    original_init = TestClient.__init__

    def _loopback_init(self: TestClient, *args: Any, **kwargs: Any) -> None:
        # args[0] is the ASGI app; a positional base_url would be args[1].
        if len(args) < 2 and "base_url" not in kwargs:
            kwargs["base_url"] = "http://localhost"
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(TestClient, "__init__", _loopback_init)

    original_ws_connect = TestClient.websocket_connect

    def _loopback_ws_connect(self: TestClient, url: str, *args: Any, **kwargs: Any) -> Any:
        headers = dict(kwargs.get("headers") or {})
        if not any(key.lower() == "host" for key in headers):
            headers["host"] = "localhost"
        kwargs["headers"] = headers
        return original_ws_connect(self, url, *args, **kwargs)

    monkeypatch.setattr(TestClient, "websocket_connect", _loopback_ws_connect)


# ────────────────────────────────────────────────────────────────────────
# Raw-row fixtures mimicking the shape of a broker's pandas DataFrame
# rows. These are dicts (same shape returned by `df.to_dict('records')[i]`).
# Used by orchestrator/data unit tests as generic instrument rows. They
# include an `internal_trading_symbol` column observed in legacy adapter
# output; new adapters do not need to expose it. Phase 2 will reshape
# these fixtures around Groww's response format.
# ────────────────────────────────────────────────────────────────────────


# raw_equity_row_reliance fixture removed in DEC-P0-032 (PR #50e).
# Orphan fixture — zero callers per Agent 4 of PR #50e pre-impl review.
# Track 1 is index-only per CLAUDE.md §3; EQ-rejection-path coverage is
# already provided by inline `_make_equity` helpers in test_universe.py.


@pytest.fixture
def raw_index_row_nifty() -> dict[str, Any]:
    """Spot NIFTY index row."""
    return {
        "exchange": "NSE",
        "exchange_token": 26000,
        "trading_symbol": "NIFTY",
        "vendor_symbol": "NSE-NIFTY",
        "name": "Nifty 50",
        "instrument_type": "IDX",
        "segment": "CASH",
        "series": "",
        "isin": "",
        "underlying_symbol": "",
        "underlying_exchange_token": float("nan"),
        "lot_size": 1,
        "expiry_date": "",
        "strike_price": float("nan"),
        "tick_size": 0.05,  # float rupees form (CSV-text form)
        "freeze_quantity": float("nan"),
        "is_reserved": 0,
        "buy_allowed": 1,
        "sell_allowed": 1,
        "internal_trading_symbol": "NIFTY",
    }


@pytest.fixture
def raw_banknifty_ce_row_dec2025() -> dict[str, Any]:
    """Matches the real BANKNIFTY Dec 2025 27000 CE row from the CSV sample.

    lot_size 35 (post-SEBI-revision — see https://groww.in/trade-api/docs/docs/v2/annexure).
    """
    return {
        "exchange": "NSE",
        "exchange_token": 49445,
        "trading_symbol": "BANKNIFTY25DEC27000PE",
        "vendor_symbol": "NSE-BANKNIFTY-24Dec25-27000-PE",
        "name": float("nan"),
        "instrument_type": "PE",
        "segment": "FNO",
        "series": float("nan"),
        "isin": float("nan"),
        "underlying_symbol": "BANKNIFTY",
        "underlying_exchange_token": 26009,
        "lot_size": 35,
        "expiry_date": "2025-12-24",
        "strike_price": 27000,
        "tick_size": 0.05,
        "freeze_quantity": 601,
        "is_reserved": 1,
        "buy_allowed": 1,
        "sell_allowed": 1,
        "internal_trading_symbol": "BANKNIFTY25DEC27000PE",
    }


@pytest.fixture
def raw_banknifty_ce_row_jun2025() -> dict[str, Any]:
    """Matches the real BANKNIFTY Jun 2025 35400 PE row — lot_size 30
    (pre-SEBI-revision). Used to prove temporal lot-size tracking.
    """
    return {
        "exchange": "NSE",
        "exchange_token": 41942,
        "trading_symbol": "BANKNIFTY25JUN35400PE",
        "vendor_symbol": "NSE-BANKNIFTY-26Jun25-35400-PE",
        "name": float("nan"),
        "instrument_type": "PE",
        "segment": "FNO",
        "series": float("nan"),
        "isin": float("nan"),
        "underlying_symbol": "BANKNIFTY",
        "underlying_exchange_token": 26009,
        "lot_size": 30,
        "expiry_date": "2025-06-26",
        "strike_price": 35400,
        "tick_size": 0.05,
        "freeze_quantity": 601,
        "is_reserved": 1,
        "buy_allowed": 1,
        "sell_allowed": 1,
        "internal_trading_symbol": "BANKNIFTY25JUN35400PE",
    }


@pytest.fixture
def raw_sensex_ce_row() -> dict[str, Any]:
    """BSE SENSEX weekly Tuesday option — required for BSE scope coverage."""
    return {
        "exchange": "BSE",
        "exchange_token": 86000,
        "trading_symbol": "SENSEX25SEP79500CE",
        "vendor_symbol": "BSE-SENSEX-25Sep25-79500-CE",
        "name": float("nan"),
        "instrument_type": "CE",
        "segment": "FNO",
        "series": float("nan"),
        "isin": float("nan"),
        "underlying_symbol": "SENSEX",
        "underlying_exchange_token": 1,
        "lot_size": 20,
        "expiry_date": "2025-09-25",
        "strike_price": 79500,
        "tick_size": 0.05,
        "freeze_quantity": 1000,
        "is_reserved": 0,
        "buy_allowed": 1,
        "sell_allowed": 1,
        "internal_trading_symbol": "SENSEX25SEP79500CE",
    }


@pytest.fixture
def nifty_option_instance() -> Instrument:
    """A pre-built Instrument useful for universe/scope tests."""
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=35044,
        symbol_name="NIFTY25MAR29050PE",
        display_name=None,
        instrument=InstrumentType.PE,
        segment=Segment.FNO,
        series=None,
        isin=None,
        underlying_symbol="NIFTY",
        underlying_security_id=26000,
        lot_size=25,
        tick_size=Decimal("0.05"),
        sm_freeze_qty=7200,
        sm_expiry_date=date(2025, 3, 27),
        strike_price=Decimal("29050"),
        option_type="PE",
        expiry_flag="W",
        asm_gsm_flag=True,
    )
