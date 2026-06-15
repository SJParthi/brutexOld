"""Unit tests for :mod:`brutex.adapters.groww.contract_pull` (DEC-LIVE-LEG-RESOLUTION-001).

100% line + branch: the nearest-N expiry selector (upcoming-first, expired backfill,
``limit<=0`` / oversize / short cases), the per-underlying pull loop (expiries →
contracts → registry upsert), the per-underlying QUARANTINE on a terminal Groww
error (run continues + crash-safe partial registry), the IDEMPOTENT re-pull, the
report aggregation, and — the FEATURE PROOF — the populated registry crossing the
1000/conn cap so :func:`~brutex.live.multiconn.plan_connections` shards into N>1
connections (vs 1 on the spot-only / empty-registry path). Driven by a FAKE SDK
behind a REAL :class:`~brutex.adapters.groww.client.GrowwClient` + a REAL temp-DuckDB
:class:`~brutex.data.registry_store.RegistryStore` — no ``growwapi``, no network.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from brutex.adapters.groww.client import GrowwClient
from brutex.adapters.groww.contract_pull import (
    DEFAULT_EXPIRIES_PER_UNDERLYING,
    ContractPullReport,
    PullTarget,
    UnderlyingPullResult,
    pull_contracts_to_registry,
    select_nearest_expiries,
)
from brutex.dashboard.live_manifest import build_leg_rows
from brutex.data.registry_store import RegistryStore
from brutex.live.multiconn import plan_connections

_NOW = datetime(2026, 6, 15, 9, 15, tzinfo=UTC)
_TODAY = date(2026, 6, 15)


class _FakeSDK:
    """Fake Groww SDK: injected expiry/contract callables; records every kwarg."""

    def __init__(
        self,
        *,
        expiries: Callable[[Mapping[str, object]], Mapping[str, object]] | None = None,
        contracts: Callable[[Mapping[str, object]], Mapping[str, object]] | None = None,
    ) -> None:
        self._expiries = expiries or (lambda _kw: {"expiries": []})
        self._contracts = contracts or (lambda _kw: {"contracts": []})
        self.expiry_calls: list[dict[str, object]] = []
        self.contract_calls: list[dict[str, object]] = []

    def get_historical_candles(self, **_kw: object) -> Mapping[str, object]:  # pragma: no cover
        raise AssertionError("the contract pull never fetches candles")

    def get_expiries(self, **kw: object) -> Mapping[str, object]:
        self.expiry_calls.append(dict(kw))
        return self._expiries(kw)

    def get_contracts(self, **kw: object) -> Mapping[str, object]:
        self.contract_calls.append(dict(kw))
        return self._contracts(kw)


def _client(sdk: _FakeSDK) -> GrowwClient:
    """A REAL client over the fake SDK (no real sleep on the bounded retry path)."""
    return GrowwClient(sdk=sdk, sleep=lambda _s: None)


def _store(tmp_path: Path) -> RegistryStore:
    return RegistryStore.open(tmp_path / "registry.duckdb")


def _expiry_tag(kw: Mapping[str, object]) -> str:
    """A compact ``DDMmmYY`` tag from the ``get_contracts`` ``expiry_date`` kwarg.

    The resolver passes the authoritative ISO expiry; tagging the symbol with it
    keeps each expiry's CE/PE/FUT symbols DISTINCT (so the registry PK does not
    collapse two expiries' legs into one) — the realistic vendor shape.
    """
    iso = str(kw["expiry_date"])[:10]
    return iso.replace("-", "")


def _three_leg_chain(kw: Mapping[str, object]) -> Mapping[str, object]:
    """A CE/PE/FUT chain for the kwargs' ``(exchange, underlying, expiry)`` — one strike."""
    exch = kw["exchange"]
    underlying = kw["underlying_symbol"]
    tag = _expiry_tag(kw)
    return {
        "contracts": [
            f"{exch}-{underlying}-{tag}-23000-CE",
            f"{exch}-{underlying}-{tag}-23000-PE",
            f"{exch}-{underlying}-{tag}-FUT",
        ]
    }


# ── select_nearest_expiries ──────────────────────────────────────────────────


def test_select_nearest_prefers_upcoming() -> None:
    expiries = [date(2026, 6, 25), date(2026, 7, 30), date(2026, 5, 28)]
    chosen = select_nearest_expiries(expiries, today=_TODAY, limit=2)
    # The two upcoming-nearest (past 2026-05-28 dropped), ascending.
    assert chosen == [date(2026, 6, 25), date(2026, 7, 30)]


def test_select_nearest_backfills_with_recent_expired() -> None:
    # Only ONE upcoming → backfill with the most-recent expired to reach the limit.
    expiries = [date(2026, 6, 25), date(2026, 5, 28), date(2026, 4, 30)]
    chosen = select_nearest_expiries(expiries, today=_TODAY, limit=2)
    assert chosen == [date(2026, 5, 28), date(2026, 6, 25)]  # ascending, recent-expired kept


def test_select_nearest_limit_zero_returns_empty() -> None:
    assert select_nearest_expiries([date(2026, 6, 25)], today=_TODAY, limit=0) == []


def test_select_nearest_limit_negative_returns_empty() -> None:
    assert select_nearest_expiries([date(2026, 6, 25)], today=_TODAY, limit=-1) == []


def test_select_nearest_limit_larger_than_input_returns_all() -> None:
    expiries = [date(2026, 6, 25), date(2026, 5, 28)]
    assert select_nearest_expiries(expiries, today=_TODAY, limit=10) == [
        date(2026, 5, 28),
        date(2026, 6, 25),
    ]


def test_select_nearest_today_counts_as_upcoming() -> None:
    # An expiry == today is UPCOMING (>= today), not expired.
    assert select_nearest_expiries([_TODAY], today=_TODAY, limit=1) == [_TODAY]


# ── pull_contracts_to_registry — happy path ──────────────────────────────────


def test_pull_populates_registry(tmp_path: Path) -> None:
    sdk = _FakeSDK(
        expiries=lambda _kw: {"expiries": ["2026-06-25", "2026-07-30"]},
        contracts=_three_leg_chain,
    )
    store = _store(tmp_path)
    report = pull_contracts_to_registry(
        _client(sdk),
        store,
        targets=[PullTarget("NSE", "NIFTY")],
        expiries_per_underlying=2,
        now=_NOW,
        today=_TODAY,
    )
    assert report.underlyings == 1
    assert report.expiries_selected == 2
    # 2 expiries × 3 contracts upsert-calls = 6 persisted-attempts (distinct symbols).
    assert report.contracts_persisted == 6
    assert report.failures == 0
    # The registry holds the 2 expiries × 3 distinct symbols = 6 contract rows.
    assert store.count_expiries() == 2
    assert store.count_contracts() == 6
    rows = store.list_contracts()
    assert {r.side for r in rows} == {"CE", "PE", "FUT"}
    store.close()


def test_pull_queries_current_and_next_year(tmp_path: Path) -> None:
    sdk = _FakeSDK(contracts=_three_leg_chain)  # default: no expiries
    store = _store(tmp_path)
    pull_contracts_to_registry(
        _client(sdk),
        store,
        targets=[PullTarget("NSE", "NIFTY")],
        now=_NOW,
        today=_TODAY,
    )
    years = sorted({int(str(call["year"])) for call in sdk.expiry_calls})
    assert years == [2026, 2027]  # current + next year both probed
    store.close()


def test_pull_default_expiries_constant(tmp_path: Path) -> None:
    sdk = _FakeSDK(
        expiries=lambda _kw: {"expiries": ["2026-06-25", "2026-07-30", "2026-08-27"]},
        contracts=_three_leg_chain,
    )
    store = _store(tmp_path)
    report = pull_contracts_to_registry(
        _client(sdk), store, targets=[PullTarget("NSE", "NIFTY")], now=_NOW, today=_TODAY
    )
    # The default bound selects exactly DEFAULT_EXPIRIES_PER_UNDERLYING expiries.
    assert report.expiries_selected == DEFAULT_EXPIRIES_PER_UNDERLYING
    store.close()


def test_pull_defaults_now_and_today(tmp_path: Path) -> None:
    # No now/today injected → uses datetime.now(UTC); the chain still resolves.
    sdk = _FakeSDK(
        expiries=lambda _kw: {"expiries": ["2099-12-31"]},  # far-future = always upcoming
        contracts=_three_leg_chain,
    )
    store = _store(tmp_path)
    report = pull_contracts_to_registry(
        _client(sdk), store, targets=[PullTarget("NSE", "NIFTY")], expiries_per_underlying=1
    )
    assert report.contracts_persisted == 3
    store.close()


# ── idempotence (Golden Rule #7) ─────────────────────────────────────────────


def test_pull_is_idempotent(tmp_path: Path) -> None:
    sdk = _FakeSDK(expiries=lambda _kw: {"expiries": ["2026-06-25"]}, contracts=_three_leg_chain)
    store = _store(tmp_path)

    def _pull() -> None:
        pull_contracts_to_registry(
            _client(sdk),
            store,
            targets=[PullTarget("NSE", "NIFTY")],
            expiries_per_underlying=1,
            now=_NOW,
            today=_TODAY,
        )

    _pull()
    first = store.count_contracts()
    _pull()
    # Re-pull yields the SAME rows (PK INSERT OR REPLACE) — count unchanged.
    assert store.count_contracts() == first == 3
    store.close()


# ── quarantine + crash-safety ────────────────────────────────────────────────


def _raise_terminal(_kw: Mapping[str, object]) -> Mapping[str, object]:
    """Raise a terminal SDK error (an unrecognised exception → terminal GrowwError)."""
    raise RuntimeError("upstream chain unavailable")


def test_pull_quarantines_failing_underlying_and_continues(tmp_path: Path) -> None:
    # First underlying's CONTRACTS resolution raises terminal; the second succeeds.
    def contracts(kw: Mapping[str, object]) -> Mapping[str, object]:
        if kw["underlying_symbol"] == "BADSYM":
            return _raise_terminal(kw)
        return _three_leg_chain(kw)

    sdk = _FakeSDK(expiries=lambda _kw: {"expiries": ["2026-06-25"]}, contracts=contracts)
    store = _store(tmp_path)
    report = pull_contracts_to_registry(
        _client(sdk),
        store,
        targets=[PullTarget("NSE", "BADSYM"), PullTarget("NSE", "NIFTY")],
        expiries_per_underlying=1,
        now=_NOW,
        today=_TODAY,
    )
    assert report.underlyings == 2
    assert report.failures == 1
    bad, good = report.results
    assert not bad.ok and bad.error is not None
    assert good.ok and good.contracts_persisted == 3
    # Crash-safe: the GOOD underlying's legs survived the bad one's failure.
    assert store.count_contracts() == 3
    store.close()


def test_pull_quarantine_on_expiry_resolution(tmp_path: Path) -> None:
    # The EXPIRY resolution itself fails → quarantined before any contract call.
    sdk = _FakeSDK(expiries=_raise_terminal)
    store = _store(tmp_path)
    report = pull_contracts_to_registry(
        _client(sdk),
        store,
        targets=[PullTarget("NSE", "NIFTY")],
        now=_NOW,
        today=_TODAY,
    )
    assert report.failures == 1
    assert store.count_contracts() == 0
    assert sdk.contract_calls == []  # never reached the contract phase
    store.close()


# ── report aggregation ───────────────────────────────────────────────────────


def test_report_aggregates_and_serialises() -> None:
    results = (
        UnderlyingPullResult(
            PullTarget("NSE", "NIFTY"), expiries_selected=2, contracts_persisted=6
        ),
        UnderlyingPullResult(
            PullTarget("NSE", "BADSYM"), expiries_selected=0, contracts_persisted=0, error="boom"
        ),
    )
    report = ContractPullReport(results=results)
    assert report.underlyings == 2
    assert report.expiries_selected == 2
    assert report.contracts_persisted == 6
    assert report.failures == 1
    assert report.to_dict() == {
        "underlyings": 2,
        "expiries_selected": 2,
        "contracts_persisted": 6,
        "failures": 1,
    }


# ── the FEATURE PROOF: N-connection sharding engages once legs are populated ──


def _spot_universe(n: int) -> list[dict[str, str]]:
    """A spot universe of *n* distinct EQ feed-rows (mirrors the live ~779)."""
    return [
        {"exchange": "NSE", "segment": "CASH", "exchange_token": str(i), "instrument_type": "EQ"}
        for i in range(n)
    ]


def test_spot_only_universe_is_one_connection() -> None:
    # 779 spot < the 1000/conn cap → ONE connection (the status quo the PR fixes).
    _bindings, _skipped, plan = plan_connections(_spot_universe(779), [])
    assert plan.num_connections == 1


def test_populated_registry_engages_n_connection_sharding(tmp_path: Path) -> None:
    """End-to-end: pull legs → build_leg_rows → spot+legs > 1000 → N>1 connections."""

    # Resolve enough legs that spot (779) + legs crosses the 1000/conn cap: two
    # underlyings × 120 strikes × CE/PE = 480 legs, which with 779 spot = 1259 > 1000.
    def many_contracts(kw: Mapping[str, object]) -> Mapping[str, object]:
        exch, u = kw["exchange"], kw["underlying_symbol"]
        legs: list[str] = []
        for strike in range(120):  # 120 strikes × CE/PE = 240 legs/underlying/expiry
            legs.append(f"{exch}-{u}-25Jun26-{20000 + strike * 50}-CE")
            legs.append(f"{exch}-{u}-25Jun26-{20000 + strike * 50}-PE")
        return {"contracts": legs}

    sdk = _FakeSDK(expiries=lambda _kw: {"expiries": ["2026-06-25"]}, contracts=many_contracts)
    store = _store(tmp_path)
    pull_contracts_to_registry(
        _client(sdk),
        store,
        targets=[PullTarget("NSE", "NIFTY"), PullTarget("NSE", "BANKNIFTY")],
        expiries_per_underlying=1,
        now=_NOW,
        today=_TODAY,
    )
    store.close()
    # 2 underlyings × 240 legs = 480 distinct contract rows persisted.
    reg_path = tmp_path / "registry.duckdb"
    legs = build_leg_rows(reg_path, resolve_feed_token=lambda symbol: symbol)
    assert len(legs) == 480
    spot = _spot_universe(779)
    _bindings, _skipped, plan = plan_connections(spot, legs)
    assert plan.total == 779 + 480  # 1259 > 1000
    # The sharding ENGAGES: 1259 / 1000 cap → 2 connections (was 1 spot-only).
    assert plan.num_connections == 2


def test_build_leg_rows_empty_on_unpopulated_registry(tmp_path: Path) -> None:
    # The status-quo degrade: an absent / never-pulled registry → no legs.
    assert build_leg_rows(tmp_path / "absent.duckdb") == []


def test_build_leg_rows_returns_legs_from_populated_registry(tmp_path: Path) -> None:
    sdk = _FakeSDK(expiries=lambda _kw: {"expiries": ["2026-06-25"]}, contracts=_three_leg_chain)
    store = _store(tmp_path)
    pull_contracts_to_registry(
        _client(sdk),
        store,
        targets=[PullTarget("NSE", "NIFTY")],
        expiries_per_underlying=1,
        now=_NOW,
        today=_TODAY,
    )
    store.close()
    legs = build_leg_rows(tmp_path / "registry.duckdb", resolve_feed_token=lambda s: "12345")
    assert len(legs) == 3
    assert {leg["instrument_type"] for leg in legs} == {"CE", "PE", "FUT"}
    assert all(leg["segment"] == "FNO" for leg in legs)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
