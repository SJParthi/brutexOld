"""Fetch-count parity gate: the six-step order REORDERS, never RE-FETCHES — T8.

DEC-PULL-SIX-STEP-ORDER-001: the strict INDICES-SPOT → … → STOCKS-OPTIONS
ordering is enforced purely at the two existing execution chokepoints (the spot
group loop + the 4-key F&O bucket sort); it must move only WHEN a window is
fetched, never HOW MANY vendor fetches the pull pays. This bench replays ONE
representative chain (two underlyings — an index + a stock — each FUT + a CE/PE
strike band) through the REAL F&O spine in synchronous mode twice:

* the STEPPED path (``underlying_kinds`` classification → 4 buckets, 3→4→5→6);
* the LEGACY path (``underlying_kinds=None`` → the pre-change 2-bucket
  FUT-then-OPT order);

and asserts the TOTAL vendor fetch count AND the per-symbol fetch multiset are
IDENTICAL — the permanent "reorders, never re-fetches" CI pin
(DEC-FETCH-PER-WINDOW-GUARANTEE-001 style: a structural regression that adds
even one extra vendor call per contract fails this gate loudly). The SPOT arm
pins the same invariant for steps 1-2: a mixed grouped pull issues EXACTLY the
fetches of the indices-only + stocks-only pulls combined (grouping adds zero).

Honest scope (Golden Rule #10): a CONTROLLED deterministic replay (canned
non-empty candles, one chunk per window, serial pool) measuring the ORDERING
change's fetch cost — zero by construction, pinned here. It does not model
empty-retry amplification or chunk tiling (those own their own gate:
``test_fetch_per_window_guarantee.py``).
"""

from __future__ import annotations

import threading
from collections.abc import Iterator, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.adapters.groww.client import GrowwClient
from brutex.adapters.ingest_common import SyntheticSpotAdapter
from brutex.adapters.protocol import (
    DerivativeContract,
    InstrumentPage,
    MarketCapabilities,
)
from brutex.dashboard.historical_pull import pull_contract_candles
from brutex.dashboard.pull_controller import pull_all_spot
from brutex.data.models import CandleBar
from brutex.data.registry_store import RegistryStore
from brutex.data.sink import BarSink, SinkAppendResult
from brutex.models import Exchange, Instrument, InstrumentType, Segment

pytest.importorskip("duckdb", reason="RegistryStore needs duckdb")

_NOW = datetime(2026, 6, 14, 9, 15, tzinfo=UTC)
_FROM = date(2024, 1, 1)
_TO = date(2024, 1, 20)
_EXPIRY = date(2024, 1, 25)
_SPOT_FROM = "2024-01-02"
_SPOT_TO = "2024-01-03"

#: The write/read-symmetric classification (NIFTY the index, ADANIENT the stock).
_KINDS: dict[str, bool] = {"NIFTY": True, "ADANIENT": False}

#: A representative chain: each underlying carries FUT + a CE/PE strike band.
_CHAIN: tuple[tuple[str, str, str], ...] = (
    ("NIFTY", "NSE-NIFTY-25Jan24-FUT", "FUT"),
    ("NIFTY", "NSE-NIFTY-25Jan24-20800-CE", "CE"),
    ("NIFTY", "NSE-NIFTY-25Jan24-21000-CE", "CE"),
    ("NIFTY", "NSE-NIFTY-25Jan24-21000-PE", "PE"),
    ("NIFTY", "NSE-NIFTY-25Jan24-21200-PE", "PE"),
    ("ADANIENT", "NSE-ADANIENT-25Jan24-FUT", "FUT"),
    ("ADANIENT", "NSE-ADANIENT-25Jan24-2900-CE", "CE"),
    ("ADANIENT", "NSE-ADANIENT-25Jan24-3000-CE", "CE"),
    ("ADANIENT", "NSE-ADANIENT-25Jan24-3000-PE", "PE"),
)


class _CountingSDK:
    """A ``GrowwSDK`` fake counting every vendor candle fetch per symbol."""

    def __init__(self) -> None:
        self.candle_symbols: list[str] = []

    def get_expiries(self, **_kw: object) -> dict[str, object]:
        """Never expected — the contracts come from the registry."""
        return {"expiries": []}

    def get_contracts(self, **_kw: object) -> dict[str, object]:
        """Never expected — the contracts come from the registry."""
        return {"contracts": []}

    def get_historical_candles(self, **kw: object) -> dict[str, object]:
        """Count the fetch; return one canned in-window bar (a clean SUCCESS)."""
        self.candle_symbols.append(str(kw["groww_symbol"]))
        return {"candles": [["2024-01-15T09:15:00", 100.0, 110.0, 90.0, 105.0, 50, 7]]}


class _NullRecordingSink:
    """A no-op :class:`BarSink` that reports every bar appended."""

    def append(
        self,
        *,
        exchange: str,
        segment: str,
        symbol: str,
        interval: str,
        bars: Sequence[object],
        merge_mode: str = "keep-first",
    ) -> SinkAppendResult:
        """Report the input count appended (no disk I/O — a pure fetch counter)."""
        del exchange, segment, symbol, interval, merge_mode
        return SinkAppendResult(bars_appended=len(bars), partitions_written=1, write_results=())


def _seed_registry(root: Path) -> RegistryStore:
    """A real temp registry seeded with the representative chain."""
    store = RegistryStore.open(root / "registry.duckdb")
    for underlying, symbol, side in _CHAIN:
        store.upsert_contract(
            "NSE",
            underlying,
            _EXPIRY,
            symbol,
            side=side,
            strike=21000.0 if side in {"CE", "PE"} else None,
            run_id="t8",
            fetched_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
    return store


def _replay_fno(root: Path, kinds: dict[str, bool] | None) -> list[str]:
    """One synchronous replay of the chain; returns the vendor fetch log."""
    registry = _seed_registry(root)
    sdk = _CountingSDK()
    report = pull_contract_candles(
        registry.list_contracts(),
        lake_root=root / "lake",
        dry_run=False,
        intervals=("1minute",),
        registry=registry,
        from_date=_FROM,
        to_date=_TO,
        client_factory=lambda _root: (
            GrowwClient(sdk=sdk, sleep=lambda _s: None, max_attempts=1),
            _NullRecordingSink(),
        ),
        now=_NOW,
        underlying_kinds=kinds,
    )
    assert report.contracts_completed == len(_CHAIN)  # every leg landed clean
    return sdk.candle_symbols


class TestFnoFetchParity:
    """The F&O arm: stepped (4-bucket) vs legacy (2-bucket) fetch parity."""

    @pytest.fixture(autouse=True)
    def _sync_writes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Synchronous write tails — deterministic fetch accounting.
        monkeypatch.setenv("BRUTEX_GROWW_FETCH_PIPELINE", "off")

    def test_total_fetch_count_identical_pre_post_reordering(self, tmp_path: Path) -> None:
        stepped = _replay_fno(tmp_path / "stepped", _KINDS)
        legacy = _replay_fno(tmp_path / "legacy", None)
        assert stepped, "the stepped replay issued no vendor fetches"
        # THE pin: the six-step order pays EXACTLY the legacy fetch total…
        assert len(stepped) == len(legacy)
        # …over EXACTLY the same per-symbol fetch multiset (no contract is
        # fetched more, fewer, or instead-of another) — only the ORDER moves.
        assert sorted(stepped) == sorted(legacy)
        assert stepped != legacy, (
            "the stepped replay should REORDER the legacy fetch sequence "
            "(index legs first per market) — identical order means the "
            "4-bucket sort silently degraded to the legacy path"
        )
        # One fetch per (contract, window) on this single-chunk chain.
        assert len(stepped) == len(_CHAIN)


class _CountingSpotAdapter:
    """A synthetic spot adapter counting every fetch (thread-safe)."""

    def __init__(self) -> None:
        self._inner = SyntheticSpotAdapter()
        self.fetched: list[str] = []
        self._lock = threading.Lock()

    @property
    def capabilities(self) -> MarketCapabilities:
        """Delegate the capability surface."""
        return self._inner.capabilities

    def list_instruments(self, *, cursor: str | None = None) -> InstrumentPage:
        """Delegate the instrument page."""
        return self._inner.list_instruments(cursor=cursor)

    def list_derivatives(
        self, *, underlying_symbol: str, exchange: Exchange
    ) -> Iterator[DerivativeContract]:
        """Delegate the (empty) derivatives iterator."""
        return self._inner.list_derivatives(underlying_symbol=underlying_symbol, exchange=exchange)

    def fetch_ohlcv(
        self, *, symbol: str, exchange: Exchange, interval: str, start: date, end: date
    ) -> list[CandleBar]:
        """Count the fetch, then return the synthetic bars."""
        with self._lock:
            self.fetched.append(symbol)
        return self._inner.fetch_ohlcv(
            symbol=symbol, exchange=exchange, interval=interval, start=start, end=end
        )


def _spot_instrument(symbol: str, kind: InstrumentType) -> Instrument:
    """A minimal spot instrument of the given *kind*."""
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=1,
        symbol_name=symbol,
        display_name=None,
        instrument=kind,
        segment=Segment.INDEX if kind == InstrumentType.IDX else Segment.CASH,
        series=None,
        isin=None,
        underlying_symbol=None,
        underlying_security_id=None,
        lot_size=0 if kind == InstrumentType.IDX else 500,
        tick_size=Decimal("0") if kind == InstrumentType.IDX else Decimal("0.05"),
        sm_freeze_qty=None,
        sm_expiry_date=None,
        strike_price=None,
        asm_gsm_flag=False,
    )


def _replay_spot(root: Path, instruments: list[Instrument]) -> list[str]:
    """One synchronous grouped spot replay; returns the vendor fetch log."""
    adapter = _CountingSpotAdapter()
    pull_all_spot(
        instruments,
        lake_root=root,
        dry_run=False,
        now=_NOW,
        from_date=_SPOT_FROM,
        to_date=_SPOT_TO,
        adapter_factory=lambda _root, _insts: (adapter, _null_sink()),
    )
    return adapter.fetched


def _null_sink() -> BarSink:
    """A no-op sink for the spot replays."""
    from brutex.data.sink import NullBarSink

    return NullBarSink()


class TestSpotFetchParity:
    """The SPOT arm: the always-on group split adds ZERO vendor fetches."""

    @pytest.fixture(autouse=True)
    def _sync_writes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRUTEX_GROWW_FETCH_PIPELINE", "off")

    def test_grouped_mixed_pull_fetches_equal_per_class_union(self, tmp_path: Path) -> None:
        mixed = [
            _spot_instrument("ADANIENT", InstrumentType.EQ),
            _spot_instrument("NIFTY", InstrumentType.IDX),
            _spot_instrument("TCS", InstrumentType.EQ),
            _spot_instrument("SENSEX", InstrumentType.IDX),
        ]
        grouped = _replay_spot(tmp_path / "mixed", mixed)
        indices_only = _replay_spot(tmp_path / "idx", [i for i in mixed if i.is_index])
        stocks_only = _replay_spot(tmp_path / "stk", [i for i in mixed if not i.is_index])
        assert grouped, "the grouped replay issued no vendor fetches"
        # THE pin: the grouped pull pays EXACTLY the union of the class pulls —
        # the always-on partition reorders, it never re-fetches.
        assert len(grouped) == len(indices_only) + len(stocks_only)
        assert sorted(grouped) == sorted(indices_only + stocks_only)
