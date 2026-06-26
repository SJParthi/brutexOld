"""Integration tests for the raw-data integrity capture + verify wiring.

DEC-RAW-DATA-INTEGRITY-001. Proves the hook fires through the REAL fetch paths
(``fetch_candle_windows`` → ``GrowwMarketAdapter.fetch_ohlcv`` /
``fetch_option_candles``) and that the verify path re-fetches + compares — all
through a FAKE SDK behind a REAL :class:`GrowwClient` (no ``growwapi``, no
network). Confirms the capture is purely additive (same bars returned) and
records the byte-correct hash of EXACTLY what the fake vendor sent.
"""
# Whitebox test: builds the live `_build_*` private adapter factories.
# pyright: reportPrivateUsage=none

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from pathlib import Path

import pytest
from brutex.adapters.groww.adapter import GrowwMarketAdapter
from brutex.adapters.groww.candles import fetch_candle_windows
from brutex.adapters.groww.client import GrowwClient
from brutex.adapters.groww.constants import INSTRUMENT_CSV_COLUMNS
from brutex.adapters.groww.instruments import InstrumentIndex, load_index_from_csv
from brutex.adapters.groww.integrity_verify import verify_lake_records, verify_one
from brutex.adapters.groww.option_ingest import fetch_option_candles
from brutex.adapters.protocol import DerivativeContract
from brutex.config.credentials import GrowwFileCredentials
from brutex.data.raw_integrity import hash_payload
from brutex.data.raw_integrity_store import (
    find_record,
    load_records,
    make_capture_hook,
    make_capture_hook_factory,
)
from brutex.models import Exchange

_HEADER = ",".join(INSTRUMENT_CSV_COLUMNS)


def _row(**over: str) -> str:
    cells = dict.fromkeys(INSTRUMENT_CSV_COLUMNS, "")
    cells.update(over)
    return ",".join(cells[c] for c in INSTRUMENT_CSV_COLUMNS)


_NIFTY_ROW = _row(
    exchange="NSE",
    exchange_token="NIFTY",
    trading_symbol="NIFTY",
    groww_symbol="NSE-NIFTY",
    name="NIFTY 50",
    instrument_type="IDX",
    segment="CASH",
)


_SENSEX_ROW = _row(
    exchange="BSE",
    exchange_token="SENSEX",
    trading_symbol="SENSEX",
    groww_symbol="BSE-SENSEX",
    name="SENSEX",
    instrument_type="IDX",
    segment="CASH",
)


def _index() -> InstrumentIndex:
    return load_index_from_csv("\n".join([_HEADER, _NIFTY_ROW]) + "\n")


def _two_symbol_index() -> InstrumentIndex:
    return load_index_from_csv("\n".join([_HEADER, _NIFTY_ROW, _SENSEX_ROW]) + "\n")


class _FakeSDK:
    """One canned candle per call; records per-window kwargs; mutable payload."""

    def __init__(self, payload: Mapping[str, object] | None = None) -> None:
        self.calls: list[dict[str, object]] = []
        self.payload: Mapping[str, object] = payload or {
            "candles": [["2024-01-01T09:15:00", 100.0, 110.0, 90.0, 105.0, 777, 42]]
        }

    def get_historical_candles(self, **kw: object) -> Mapping[str, object]:
        self.calls.append(dict(kw))
        return self.payload

    def get_expiries(self, **_kw: object) -> Mapping[str, object]:  # pragma: no cover
        raise AssertionError("unused")

    def get_contracts(self, **_kw: object) -> Mapping[str, object]:  # pragma: no cover
        raise AssertionError("unused")


def _client(payload: Mapping[str, object] | None = None) -> tuple[GrowwClient, _FakeSDK]:
    sdk = _FakeSDK(payload)
    return GrowwClient(sdk=sdk, sleep=lambda _s: None), sdk


# --------------------------------------------------------------------------- #
# fetch_candle_windows — the COMMON capture seam
# --------------------------------------------------------------------------- #
def test_hook_fires_per_window_with_raw_payload() -> None:
    client, sdk = _client()
    captured: list[tuple[Mapping[str, object], str, str, str]] = []
    bars = fetch_candle_windows(
        client,
        exchange="NSE",
        segment="CASH",
        groww_symbol="NSE-NIFTY",
        interval="1minute",  # 30-day tier
        start=date(2024, 1, 1),
        end=date(2024, 3, 16),  # 75 days → 3 windows
        drop_volume=False,
        on_raw_payload=lambda p, i, ws, we: captured.append((p, i, ws, we)),
    )
    assert len(bars) == 3  # one canned bar per window, unaffected by the hook
    assert len(captured) == 3  # hook fired once per window
    payload, interval, ws, _we = captured[0]
    assert payload is sdk.payload  # the RAW payload, by reference (pre-parse)
    assert interval == "1minute"  # the resolved Groww interval, not the canonical
    # The hook receives the window bounds as the COMMON primitive tiles them (ISO `T`
    # form); the SDK receives the same bound with the documented space separator. Both
    # forms map 1:1, and verify re-issues through the SAME client transform, so the
    # recorded coord is the `T` form and the SDK call's is its space twin.
    assert ws == "2024-01-01T00:00:00"
    assert str(sdk.calls[0]["start_time"]) == ws.replace("T", " ")


def test_no_hook_is_byte_identical() -> None:
    client, _sdk = _client()
    bars = fetch_candle_windows(
        client,
        exchange="NSE",
        segment="CASH",
        groww_symbol="NSE-NIFTY",
        interval="1minute",
        start=date(2024, 1, 1),
        end=date(2024, 1, 15),
        drop_volume=False,
    )
    assert len(bars) == 1  # one window, one bar — no capture, no change


# --------------------------------------------------------------------------- #
# GrowwMarketAdapter.fetch_ohlcv — the spot wiring (adapter-field hook)
# --------------------------------------------------------------------------- #
def test_adapter_threads_capture_hook_to_sidecar(tmp_path: Path) -> None:
    client, sdk = _client()
    hook = make_capture_hook(tmp_path, exchange="NSE", segment="CASH", groww_symbol="NSE-NIFTY")
    adapter = GrowwMarketAdapter(client=client, index=_index(), on_raw_payload=hook)
    bars = adapter.fetch_ohlcv(
        symbol="NSE-NIFTY",
        exchange=Exchange.NSE,
        interval="1minute",
        start=date(2024, 1, 1),
        end=date(2024, 1, 15),
    )
    assert len(bars) == 1
    # The recorded window coords are the COMMON primitive's `T`-form ISO bounds (the
    # SDK's start_time is the space twin of the same instant).
    record = find_record(
        tmp_path,
        exchange="NSE",
        segment="CASH",
        groww_symbol="NSE-NIFTY",
        interval="1minute",
        window_start="2024-01-01T00:00:00",
        window_end="2024-01-15T00:00:00",
    )
    assert record is not None
    assert record.payload_hash == hash_payload(sdk.payload)
    assert str(sdk.calls[0]["start_time"]) == record.window_start.replace("T", " ")


def test_adapter_without_hook_persists_nothing(tmp_path: Path) -> None:
    client, _sdk = _client()
    adapter = GrowwMarketAdapter(client=client, index=_index())  # no hook
    adapter.fetch_ohlcv(
        symbol="NSE-NIFTY",
        exchange=Exchange.NSE,
        interval="1minute",
        start=date(2024, 1, 1),
        end=date(2024, 1, 15),
    )
    assert load_records(tmp_path) == {}


# --------------------------------------------------------------------------- #
# fetch_option_candles — the option wiring
# --------------------------------------------------------------------------- #
def test_option_path_threads_capture_hook() -> None:
    client, _sdk = _client()
    contract = DerivativeContract(
        underlying_symbol="NIFTY",
        expiry=date(2025, 1, 2),
        symbol="NSE-NIFTY-02Jan25-28500-PE",
        contract_type="PE",
        strike=28500.0,
    )
    captured: list[str] = []
    bars = fetch_option_candles(
        client,
        contract=contract,
        interval="1minute",
        start=date(2024, 12, 20),
        end=date(2025, 1, 3),
        on_raw_payload=lambda _p, _i, _ws, _we: captured.append(_i),
    )
    assert len(bars) == 1
    assert captured == ["1minute"]


# --------------------------------------------------------------------------- #
# verify path — re-fetch + compare
# --------------------------------------------------------------------------- #
def test_verify_one_matches_unchanged_vendor(tmp_path: Path) -> None:
    client, _sdk = _client()
    hook = make_capture_hook(tmp_path, exchange="NSE", segment="CASH", groww_symbol="NSE-NIFTY")
    GrowwMarketAdapter(client=client, index=_index(), on_raw_payload=hook).fetch_ohlcv(
        symbol="NSE-NIFTY",
        exchange=Exchange.NSE,
        interval="1minute",
        start=date(2024, 1, 1),
        end=date(2024, 1, 15),
    )
    (record,) = load_records(tmp_path).values()
    result = verify_one(client, record)  # same fake → same bytes → match
    assert result.matches is True


def test_verify_lake_records_all_matched(tmp_path: Path) -> None:
    client, _sdk = _client()
    hook = make_capture_hook(tmp_path, exchange="NSE", segment="CASH", groww_symbol="NSE-NIFTY")
    GrowwMarketAdapter(client=client, index=_index(), on_raw_payload=hook).fetch_ohlcv(
        symbol="NSE-NIFTY",
        exchange=Exchange.NSE,
        interval="1minute",
        start=date(2024, 1, 1),
        end=date(2024, 1, 15),
    )
    run = verify_lake_records(client, tmp_path)  # same fake → unchanged → all match
    assert run.total == 1
    assert run.matched == 1
    assert run.all_matched is True
    assert run.mismatched == ()


def test_verify_lake_records_flags_drift(tmp_path: Path) -> None:
    client, sdk = _client()
    hook = make_capture_hook(tmp_path, exchange="NSE", segment="CASH", groww_symbol="NSE-NIFTY")
    GrowwMarketAdapter(client=client, index=_index(), on_raw_payload=hook).fetch_ohlcv(
        symbol="NSE-NIFTY",
        exchange=Exchange.NSE,
        interval="1minute",
        start=date(2024, 1, 1),
        end=date(2024, 1, 15),
    )
    # Mutate the vendor payload so the re-fetch DIFFERS — drift must be flagged.
    sdk.payload = {"candles": [["2024-01-01T09:15:00", 100.0, 110.0, 90.0, 999.0, 1, 1]]}
    run = verify_lake_records(client, tmp_path)
    assert run.total == 1
    assert run.matched == 0
    assert run.all_matched is False
    assert len(run.mismatched) == 1


def test_verify_lake_records_empty_sidecar(tmp_path: Path) -> None:
    client, _sdk = _client()
    run = verify_lake_records(client, tmp_path)
    assert run.total == 0
    assert run.all_matched is True
    assert run.matched == 0


# --------------------------------------------------------------------------- #
# capture_hook_factory — the PRODUCTION seam (one adapter, many symbols)
# --------------------------------------------------------------------------- #
def test_adapter_factory_attributes_per_symbol(tmp_path: Path) -> None:
    # ONE adapter (as the live bulk pull builds it) pulling TWO symbols must record
    # each window under its OWN groww_symbol — the property the factory provides and
    # a single pre-bound make_capture_hook cannot. This is the FIX-1 correctness proof.
    client, _sdk = _client()
    adapter = GrowwMarketAdapter(
        client=client,
        index=_two_symbol_index(),
        capture_hook_factory=make_capture_hook_factory(tmp_path),
    )
    for symbol, exchange in (("NSE-NIFTY", Exchange.NSE), ("BSE-SENSEX", Exchange.BSE)):
        adapter.fetch_ohlcv(
            symbol=symbol,
            exchange=exchange,
            interval="1minute",
            start=date(2024, 1, 1),
            end=date(2024, 1, 15),
        )
    loaded = load_records(tmp_path)
    assert {record.groww_symbol for record in loaded.values()} == {"NSE-NIFTY", "BSE-SENSEX"}


def test_adapter_explicit_hook_wins_over_factory(tmp_path: Path) -> None:
    # An explicit pre-bound on_raw_payload takes precedence over the factory.
    client, _sdk = _client()
    captured: list[str] = []
    adapter = GrowwMarketAdapter(
        client=client,
        index=_index(),
        on_raw_payload=lambda _p, i, _ws, _we: captured.append(i),
        capture_hook_factory=make_capture_hook_factory(tmp_path),
    )
    adapter.fetch_ohlcv(
        symbol="NSE-NIFTY",
        exchange=Exchange.NSE,
        interval="1minute",
        start=date(2024, 1, 1),
        end=date(2024, 1, 15),
    )
    assert captured == ["1minute"]  # the explicit hook fired
    assert load_records(tmp_path) == {}  # the factory's sidecar path was NOT used


def test_adapter_no_capture_persists_nothing(tmp_path: Path) -> None:
    client, _sdk = _client()
    GrowwMarketAdapter(client=client, index=_index()).fetch_ohlcv(  # neither seam
        symbol="NSE-NIFTY",
        exchange=Exchange.NSE,
        interval="1minute",
        start=date(2024, 1, 1),
        end=date(2024, 1, 15),
    )
    assert load_records(tmp_path) == {}


def test_option_path_factory_attributes_per_contract(tmp_path: Path) -> None:
    # The option ingest run is the same one-object-many-contracts shape; the factory
    # must attribute each contract's window under its OWN contract symbol.
    from brutex.adapters.groww.option_ingest import run_groww_option_ingest
    from brutex.data.parquet_writer import ParquetWriter
    from brutex.data.sink import ParquetBarSink

    client, _sdk = _client()
    contracts = [
        DerivativeContract(
            underlying_symbol="NIFTY",
            expiry=date(2025, 1, 2),
            symbol="NSE-NIFTY-02Jan25-28500-PE",
            contract_type="PE",
            strike=28500.0,
        ),
        DerivativeContract(
            underlying_symbol="NIFTY",
            expiry=date(2025, 1, 2),
            symbol="NSE-NIFTY-02Jan25-28600-CE",
            contract_type="CE",
            strike=28600.0,
        ),
    ]
    sink = ParquetBarSink(writer=ParquetWriter(lake_root=tmp_path))
    run_groww_option_ingest(
        client,
        sink,
        contracts=contracts,
        intervals=("1minute",),
        start=date(2024, 12, 20),
        end=date(2025, 1, 3),
        capture_hook_factory=make_capture_hook_factory(tmp_path),
    )
    symbols = {record.groww_symbol for record in load_records(tmp_path).values()}
    assert symbols == {"NSE-NIFTY-02Jan25-28500-PE", "NSE-NIFTY-02Jan25-28600-CE"}


# --------------------------------------------------------------------------- #
# Production build sites BIND the factory (FIX 1) — run the real builders with the
# network/credential leaves faked, then prove the returned adapter captures.
# --------------------------------------------------------------------------- #
def _fake_build_leaves(
    monkeypatch: pytest.MonkeyPatch, sdk: _FakeSDK, index: InstrumentIndex
) -> None:
    """Fake every network/credential leaf the live builders import lazily."""

    def _creds() -> GrowwFileCredentials:
        return GrowwFileCredentials(api_key="k", totp_secret="s")

    def _client_from_sdk(_creds_arg: object) -> GrowwClient:
        return GrowwClient(sdk=sdk, sleep=lambda _s: None)

    def _pool_from_sdk(_creds_arg: object, size: int = 2) -> GrowwClient:
        return GrowwClient(sdk=sdk, sleep=lambda _s: None)

    def _load_index(_http: object) -> InstrumentIndex:
        return index

    def _pool_size() -> int:
        return 2

    monkeypatch.setattr("brutex.config.credentials.resolve_groww_file_credentials", _creds)
    monkeypatch.setattr("brutex.adapters.groww.live.build_live_client", _client_from_sdk)
    monkeypatch.setattr("brutex.adapters.groww.live.build_live_pool", _pool_from_sdk)
    monkeypatch.setattr("brutex.adapters.groww.instrument_loader.load_index", _load_index)
    monkeypatch.setattr("brutex.adapters.groww.constants.resolve_pool_size", _pool_size)


def test_cli_build_site_binds_capture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from brutex.cli import _build_live_groww_spot

    sdk = _FakeSDK()
    _fake_build_leaves(monkeypatch, sdk, _index())
    adapter, _sink = _build_live_groww_spot(tmp_path)
    assert adapter.capture_hook_factory is not None  # FIX 1: the CLI binds the factory
    adapter.fetch_ohlcv(
        symbol="NSE-NIFTY",
        exchange=Exchange.NSE,
        interval="1minute",
        start=date(2024, 1, 1),
        end=date(2024, 1, 15),
    )
    (record,) = load_records(tmp_path).values()  # a real record was written on a pull
    assert record.groww_symbol == "NSE-NIFTY"
    assert record.payload_hash == hash_payload(sdk.payload)


def test_dashboard_build_site_binds_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from brutex.dashboard.pull_controller import _build_live_spot_adapter

    sdk = _FakeSDK()
    _fake_build_leaves(monkeypatch, sdk, _index())
    adapter, _sink = _build_live_spot_adapter(tmp_path, ())
    assert isinstance(adapter, GrowwMarketAdapter)  # narrow the MarketAdapter return
    assert adapter.capture_hook_factory is not None  # FIX 1: the dashboard binds it
    adapter.fetch_ohlcv(
        symbol="NSE-NIFTY",
        exchange=Exchange.NSE,
        interval="1minute",
        start=date(2024, 1, 1),
        end=date(2024, 1, 15),
    )
    (record,) = load_records(tmp_path).values()
    assert record.groww_symbol == "NSE-NIFTY"
