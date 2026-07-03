"""Unit tests for the AUTHORITATIVE registry strike-chain loader.

DEC-MONEYNESS-REGISTRY-STEP-001 —
:func:`brutex.dashboard.registry_strikes.load_registry_strike_chains` reads the FULL
vendor-listed option strike ladders per ``(underlying, expiry)`` from
``registry.duckdb`` so the display strike-grid step no longer overestimates on a
sparsely-pulled lake. Covers the graceful-fallback triad (absent / empty / unreadable
→ ``None``, mirroring ``load_registry_expiries``), the CE+PE grouping, the FUT /
``None``-strike exclusion, the float→Decimal conversion, and every narrowing filter —
plus the BOUNDED single-chain read ``load_registry_strike_chain`` and the LAZY
memoised ``make_registry_chain_loader`` (Golden Rule #5, DEC-FNO-HONESTY-PACK-001)
that the ``/movers`` route + post-pull warm thread per tier-target key.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("duckdb")

from brutex.dashboard.movers_cache import MoversSnapshotCache
from brutex.dashboard.registry_strikes import (
    load_registry_strike_chain,
    load_registry_strike_chains,
    make_registry_chain_loader,
    registry_chain_key,
)
from brutex.data.registry_store import RegistryError, RegistryStore

_NOW = datetime(2025, 6, 25, tzinfo=UTC)
_JUL7 = date(2025, 7, 7)
_JUL28 = date(2025, 7, 28)


def _seed_registry(path: Path) -> None:
    """A NIFTY July-7 chain (3 strikes CE+PE) + a FUT leg + a BANKNIFTY July-28 pair."""
    with RegistryStore.open(path) as reg:
        for strike in (24000.0, 24050.0, 24100.0):
            reg.upsert_contract(
                "NSE",
                "NIFTY",
                _JUL7,
                f"NSE-NIFTY-07Jul25-{int(strike)}-CE",
                side="CE",
                strike=strike,
                run_id="r1",
                fetched_at=_NOW,
            )
        reg.upsert_contract(
            "NSE",
            "NIFTY",
            _JUL7,
            "NSE-NIFTY-07Jul25-24000-PE",
            side="PE",
            strike=24000.0,
            run_id="r1",
            fetched_at=_NOW,
        )
        # A FUT leg carries no strike — must be excluded from the ladder.
        reg.upsert_contract(
            "NSE",
            "NIFTY",
            _JUL7,
            "NSE-NIFTY-07Jul25-FUT",
            side="FUT",
            strike=None,
            run_id="r1",
            fetched_at=_NOW,
        )
        reg.upsert_contract(
            "NSE",
            "BANKNIFTY",
            _JUL28,
            "NSE-BANKNIFTY-28Jul25-52000-CE",
            side="CE",
            strike=52000.0,
            run_id="r1",
            fetched_at=_NOW,
        )
        reg.upsert_contract(
            "BSE",
            "SENSEX",
            _JUL28,
            "BSE-SENSEX-28Jul25-80000-CE",
            side="CE",
            strike=80000.0,
            run_id="r1",
            fetched_at=_NOW,
        )


class TestLoadRegistryStrikeChains:
    """The graceful-fallback loader — the full chain in, ``None`` degrades out."""

    def test_missing_registry_returns_none(self, tmp_path: Path) -> None:
        assert load_registry_strike_chains(tmp_path / "registry.duckdb") is None

    def test_empty_registry_returns_none(self, tmp_path: Path) -> None:
        path = tmp_path / "registry.duckdb"
        with RegistryStore.open(path):
            pass  # create the schema, persist nothing
        assert load_registry_strike_chains(path) is None

    def test_fut_only_registry_returns_none(self, tmp_path: Path) -> None:
        # Rows exist but NONE carries a strike (all FUT) → no chain, honest None.
        path = tmp_path / "registry.duckdb"
        with RegistryStore.open(path) as reg:
            reg.upsert_contract(
                "NSE",
                "NIFTY",
                _JUL7,
                "NSE-NIFTY-07Jul25-FUT",
                side="FUT",
                strike=None,
                run_id="r1",
                fetched_at=_NOW,
            )
        assert load_registry_strike_chains(path) is None

    def test_populated_registry_groups_ce_and_pe_per_underlying_expiry(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "registry.duckdb"
        _seed_registry(path)
        chains = load_registry_strike_chains(path)
        assert chains is not None
        # CE + PE share the grid; the FUT leg is excluded; floats became Decimals.
        nifty = chains[("NIFTY", _JUL7)]
        assert sorted(nifty) == [
            Decimal("24000.0"),
            Decimal("24000.0"),
            Decimal("24050.0"),
            Decimal("24100.0"),
        ]
        assert all(isinstance(s, Decimal) for s in nifty)
        assert chains[("BANKNIFTY", _JUL28)] == (Decimal("52000.0"),)
        assert chains[("SENSEX", _JUL28)] == (Decimal("80000.0"),)

    def test_exchange_filter_scopes_the_chains(self, tmp_path: Path) -> None:
        path = tmp_path / "registry.duckdb"
        _seed_registry(path)
        nse = load_registry_strike_chains(path, exchange="NSE")
        assert nse is not None
        assert set(nse) == {("NIFTY", _JUL7), ("BANKNIFTY", _JUL28)}

    def test_underlying_and_expiry_filters_narrow_to_one_chain(self, tmp_path: Path) -> None:
        path = tmp_path / "registry.duckdb"
        _seed_registry(path)
        one = load_registry_strike_chains(path, exchange="NSE", underlying="NIFTY", expiry=_JUL7)
        assert one is not None
        assert set(one) == {("NIFTY", _JUL7)}

    def test_registry_error_is_swallowed_to_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "registry.duckdb"
        path.write_text("not a real duckdb file")  # exists but unreadable

        def _boom(_p: Path) -> object:
            raise RegistryError("corrupt cache")

        # Patch the `RegistryStore.open` the production module looks up (the
        # load_registry_expiries test pattern — one dotted-path binding).
        monkeypatch.setattr("brutex.dashboard.registry_strikes.RegistryStore.open", _boom)
        assert load_registry_strike_chains(path) is None


class TestLoadRegistryStrikeChain:
    """The BOUNDED single-chain read (Golden Rule #5, DEC-FNO-HONESTY-PACK-001)."""

    def test_missing_registry_returns_none(self, tmp_path: Path) -> None:
        assert (
            load_registry_strike_chain(
                tmp_path / "registry.duckdb", exchange="NSE", underlying="NIFTY", expiry=_JUL7
            )
            is None
        )

    def test_populated_registry_returns_exactly_one_narrowed_chain(self, tmp_path: Path) -> None:
        path = tmp_path / "registry.duckdb"
        _seed_registry(path)
        chain = load_registry_strike_chain(path, exchange="NSE", underlying="NIFTY", expiry=_JUL7)
        assert chain is not None
        assert sorted(chain) == [
            Decimal("24000.0"),
            Decimal("24000.0"),
            Decimal("24050.0"),
            Decimal("24100.0"),
        ]
        # A different (underlying, expiry) never leaks in (the SQL narrowing).
        other = load_registry_strike_chain(
            path, exchange="NSE", underlying="BANKNIFTY", expiry=_JUL7
        )
        assert other is None  # BANKNIFTY has no July-7 chain

    def test_none_exchange_reads_across_exchanges(self, tmp_path: Path) -> None:
        path = tmp_path / "registry.duckdb"
        _seed_registry(path)
        chain = load_registry_strike_chain(path, exchange=None, underlying="SENSEX", expiry=_JUL28)
        assert chain == (Decimal("80000.0"),)


class TestRegistryChainKey:
    """The per-(exchange, underlying, expiry) cache key — stable + unambiguous."""

    def test_key_shape(self) -> None:
        assert registry_chain_key("NSE", "NIFTY", _JUL7) == "options_strikes|NSE|NIFTY|2025-07-07"

    def test_none_exchange_keys_as_empty_segment(self) -> None:
        assert registry_chain_key(None, "NIFTY", _JUL7) == "options_strikes||NIFTY|2025-07-07"


class TestMakeRegistryChainLoader:
    """The LAZY memoised loader both the movers route and the post-pull warm build."""

    def test_loader_reads_one_narrowed_chain_and_memoises_per_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import brutex.dashboard.registry_strikes as rs_module

        path = tmp_path / "registry.duckdb"
        _seed_registry(path)
        reads: list[tuple[str | None, str, date]] = []
        real = rs_module.load_registry_strike_chain

        def _spy(
            registry_path: Path,
            *,
            exchange: str | None,
            underlying: str,
            expiry: date,
        ) -> tuple[Decimal, ...] | None:
            reads.append((exchange, underlying, expiry))
            return real(registry_path, exchange=exchange, underlying=underlying, expiry=expiry)

        monkeypatch.setattr(rs_module, "load_registry_strike_chain", _spy)
        cache = MoversSnapshotCache()  # the COMMON injected cache satisfies the Protocol
        loader = make_registry_chain_loader(path, exchange="NSE", cache=cache, generation=7)
        first = loader("NIFTY", _JUL7)
        second = loader("NIFTY", _JUL7)
        assert first == second
        assert first is not None and sorted(first)[0] == Decimal("24000.0")
        assert reads == [("NSE", "NIFTY", _JUL7)]  # ONE narrowed read; 2nd call O(1) hit
        # A different key reads once more — still narrowed, never the whole registry.
        assert loader("BANKNIFTY", _JUL28) == (Decimal("52000.0"),)
        assert reads == [("NSE", "NIFTY", _JUL7), ("NSE", "BANKNIFTY", _JUL28)]

    def test_generation_bump_re_reads(self, tmp_path: Path) -> None:
        path = tmp_path / "registry.duckdb"
        _seed_registry(path)
        cache = MoversSnapshotCache()
        gen1 = make_registry_chain_loader(path, exchange="NSE", cache=cache, generation=1)
        gen2 = make_registry_chain_loader(path, exchange="NSE", cache=cache, generation=2)
        assert gen1("NIFTY", _JUL7) == gen2("NIFTY", _JUL7)  # re-read, same truth

    def test_absent_registry_yields_none_per_key(self, tmp_path: Path) -> None:
        cache = MoversSnapshotCache()
        loader = make_registry_chain_loader(
            tmp_path / "registry.duckdb", exchange=None, cache=cache, generation=0
        )
        assert loader("NIFTY", _JUL7) is None  # the honest observed-ladder fallback
