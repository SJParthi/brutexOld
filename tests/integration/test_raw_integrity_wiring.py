"""Integration tests for the raw-data integrity capture + verify wiring.

DEC-RAW-DATA-INTEGRITY-001. Proves the hook fires through the REAL fetch paths
(``fetch_candle_windows`` → ``GrowwMarketAdapter.fetch_ohlcv`` /
``fetch_option_candles``) and that the verify path re-fetches + compares — all
through a FAKE SDK behind a REAL :class:`GrowwClient` (no ``growwapi``, no
network). Confirms the capture is purely additive (same bars returned) and
records the byte-correct hash of EXACTLY what the fake vendor sent.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from pathlib import Path

from brutex.adapters.groww.adapter import GrowwMarketAdapter
from brutex.adapters.groww.candles import fetch_candle_windows
from brutex.adapters.groww.client import GrowwClient
from brutex.adapters.groww.constants import INSTRUMENT_CSV_COLUMNS
from brutex.adapters.groww.instruments import InstrumentIndex, load_index_from_csv
from brutex.adapters.groww.integrity_verify import verify_lake_records, verify_one
from brutex.adapters.groww.option_ingest import fetch_option_candles
from brutex.adapters.protocol import DerivativeContract
from brutex.data.raw_integrity import hash_payload
from brutex.data.raw_integrity_store import find_record, load_records, make_capture_hook
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


def _index() -> InstrumentIndex:
    return load_index_from_csv("\n".join([_HEADER, _NIFTY_ROW]) + "\n")


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
