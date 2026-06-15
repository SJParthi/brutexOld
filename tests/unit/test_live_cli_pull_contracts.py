"""Tests for the ``python -m brutex.live pull-contracts`` CLI command (DEC-LIVE-LEG-RESOLUTION-001).

Drives :func:`brutex.live.__main__.main` with an injected
:class:`~brutex.live.__main__.LiveDeps` carrying a FAKE target resolver + a FAKE
Groww client (the ``# pragma`` instrument-master + TOTP boundaries are bypassed),
asserting the handler POPULATES the registry, reports honestly, and exits with the
honest code (0 on a partial/full success, 1 only when EVERY underlying is
quarantined). No ``growwapi``, no network. Covers the new handler + subparser in
:mod:`brutex.live.__main__`.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from brutex.adapters.groww.client import GrowwClient
from brutex.adapters.groww.contract_pull import PullTarget
from brutex.data.registry_store import RegistryStore
from brutex.live.__main__ import LiveDeps, main


class _FakeSDK:
    """Fake SDK: a one-strike CE/PE/FUT chain per underlying (or a raised error)."""

    def __init__(self, *, fail: bool = False) -> None:
        self._fail = fail

    def get_historical_candles(self, **_kw: object) -> Mapping[str, object]:  # pragma: no cover
        raise AssertionError("pull-contracts never fetches candles")

    def get_expiries(self, **_kw: object) -> Mapping[str, object]:
        if self._fail:
            raise RuntimeError("chain unavailable")
        return {"expiries": ["2099-12-31"]}  # far-future = always upcoming

    def get_contracts(self, **kw: object) -> Mapping[str, object]:
        exch, u = kw["exchange"], kw["underlying_symbol"]
        return {
            "contracts": [
                f"{exch}-{u}-31Dec99-23000-CE",
                f"{exch}-{u}-31Dec99-23000-PE",
                f"{exch}-{u}-31Dec99-FUT",
            ]
        }


def _deps(*, targets: list[PullTarget], fail: bool = False) -> LiveDeps:
    """LiveDeps wiring the fake target resolver + the fake client (no network)."""
    log: list[dict[str, Any]] = []
    return LiveDeps(
        feed_builder=lambda: None,
        instrument_resolver=lambda _s, _k: [],
        contract_target_resolver=lambda: targets,
        client_builder=lambda: GrowwClient(sdk=_FakeSDK(fail=fail), sleep=lambda _s: None),
        configure_logging=lambda _p: log.append({"configured": True}),
    )


def test_pull_contracts_populates_registry_and_exits_zero(tmp_path: Path) -> None:
    reg = tmp_path / "registry.duckdb"
    deps = _deps(targets=[PullTarget("NSE", "NIFTY"), PullTarget("NSE", "RELIANCE")])
    code = main(
        ["pull-contracts", "--registry", str(reg), "--expiries", "1", "--run-id", "t1"],
        deps=deps,
    )
    assert code == 0
    store = RegistryStore.open(reg)
    # 2 underlyings × 3 legs = 6 distinct contract rows persisted.
    assert store.count_contracts() == 6
    assert store.count_expiries() == 2
    store.close()


def test_pull_contracts_default_expiries_and_run_id(tmp_path: Path) -> None:
    reg = tmp_path / "registry.duckdb"
    deps = _deps(targets=[PullTarget("NSE", "NIFTY")])
    code = main(["pull-contracts", "--registry", str(reg)], deps=deps)
    assert code == 0
    store = RegistryStore.open(reg)
    assert store.count_contracts() == 3  # one far-future expiry's CE/PE/FUT
    store.close()


def test_pull_contracts_all_quarantined_exits_one(tmp_path: Path) -> None:
    reg = tmp_path / "registry.duckdb"
    deps = _deps(targets=[PullTarget("NSE", "NIFTY")], fail=True)
    code = main(["pull-contracts", "--registry", str(reg)], deps=deps)
    # EVERY underlying quarantined (a credential/connectivity failure) → exit 1.
    assert code == 1
    store = RegistryStore.open(reg)
    assert store.count_contracts() == 0
    store.close()


def test_pull_contracts_partial_success_exits_zero(tmp_path: Path) -> None:
    reg = tmp_path / "registry.duckdb"

    class _MixedSDK(_FakeSDK):
        def get_expiries(self, **kw: object) -> Mapping[str, object]:
            if kw["underlying_symbol"] == "BADSYM":
                raise RuntimeError("dead chain")
            return {"expiries": ["2099-12-31"]}

    deps = LiveDeps(
        feed_builder=lambda: None,
        instrument_resolver=lambda _s, _k: [],
        contract_target_resolver=lambda: [PullTarget("NSE", "BADSYM"), PullTarget("NSE", "NIFTY")],
        client_builder=lambda: GrowwClient(sdk=_MixedSDK(), sleep=lambda _s: None),
        configure_logging=lambda _p: None,
    )
    code = main(["pull-contracts", "--registry", str(reg), "--expiries", "1"], deps=deps)
    # One quarantined, one succeeded → partial success is still 0 (legs DID land).
    assert code == 0
    store = RegistryStore.open(reg)
    assert store.count_contracts() == 3
    store.close()


def test_pull_contracts_no_targets_exits_zero(tmp_path: Path) -> None:
    # A degenerate empty-target run (nothing to resolve) is a clean no-op, exit 0.
    reg = tmp_path / "registry.duckdb"
    deps = _deps(targets=[])
    code = main(["pull-contracts", "--registry", str(reg)], deps=deps)
    assert code == 0
    store = RegistryStore.open(reg)
    assert store.count_contracts() == 0
    store.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
