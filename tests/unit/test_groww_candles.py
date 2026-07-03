"""Unit tests for ``brutex.adapters.groww.candles`` (DEC-GROWW-OPTION-INGEST-001).

100% line+branch: the shared tile → fetch → parse → concat primitive — interval
validation (fail-loud), per-tier window tiling, the ``drop_volume`` pass-through
(index vs tradable), the empty-range short-circuit, and the ``%26`` ampersand
symbol-form fallback (DEC-GROWW-AMPERSAND-CANDLE-FALLBACK-001). Driven by a FAKE
SDK behind a REAL :class:`GrowwClient` — no ``growwapi``, no network.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date

import pytest
from brutex.adapters.groww.candles import fetch_candle_windows
from brutex.adapters.groww.client import GrowwClient
from brutex.adapters.groww.errors import GrowwRateLimitError
from structlog.testing import capture_logs


class _FakeSDK:
    """Returns one canned candle per call; records the per-window kwargs."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def get_historical_candles(self, **kw: object) -> Mapping[str, object]:
        self.calls.append(dict(kw))
        return {"candles": [["2024-01-01T09:15:00", 100.0, 110.0, 90.0, 105.0, 777, 42]]}

    def get_expiries(self, **_kw: object) -> Mapping[str, object]:  # pragma: no cover
        raise AssertionError("unused")

    def get_contracts(self, **_kw: object) -> Mapping[str, object]:  # pragma: no cover
        raise AssertionError("unused")


def _client() -> tuple[GrowwClient, _FakeSDK]:
    sdk = _FakeSDK()
    return GrowwClient(sdk=sdk, sleep=lambda _s: None), sdk


def test_tiles_range_and_concatenates() -> None:
    client, sdk = _client()
    bars = fetch_candle_windows(
        client,
        exchange="NSE",
        segment="FNO",
        groww_symbol="NSE-NIFTY-02Jan25-28500-PE",
        interval="1minute",  # 30-day tier
        start=date(2024, 1, 1),
        end=date(2024, 3, 16),  # 75 days → 3 windows
        drop_volume=False,
    )
    assert len(bars) == 3 and sdk.calls == sdk.calls[:3]
    assert len(sdk.calls) == 3
    # the contract symbol + FNO segment are forwarded verbatim
    assert sdk.calls[0]["groww_symbol"] == "NSE-NIFTY-02Jan25-28500-PE"
    assert sdk.calls[0]["segment"] == "FNO"
    assert sdk.calls[0]["candle_interval"] == "1minute"


def test_keeps_volume_and_open_interest_for_tradable() -> None:
    client, _ = _client()
    bars = fetch_candle_windows(
        client,
        exchange="NSE",
        segment="FNO",
        groww_symbol="NSE-NIFTY-02Jan25-28500-CE",
        interval="1day",
        start=date(2024, 1, 1),
        end=date(2024, 1, 2),
        drop_volume=False,
    )
    assert bars[0].volume == 777
    assert bars[0].open_interest == 42


def test_drop_volume_zeroes_volume_for_index() -> None:
    client, _ = _client()
    bars = fetch_candle_windows(
        client,
        exchange="NSE",
        segment="CASH",
        groww_symbol="NSE-NIFTY",
        interval="1day",
        start=date(2024, 1, 1),
        end=date(2024, 1, 2),
        drop_volume=True,
    )
    assert bars[0].volume == 0


def test_bad_interval_raises() -> None:
    client, _ = _client()
    with pytest.raises(ValueError, match="unsupported Groww interval"):
        fetch_candle_windows(
            client,
            exchange="NSE",
            segment="FNO",
            groww_symbol="NSE-NIFTY-02Jan25-28500-PE",
            interval="7minute",
            start=date(2024, 1, 1),
            end=date(2024, 1, 2),
            drop_volume=False,
        )


def test_empty_range_fetches_nothing() -> None:
    client, sdk = _client()
    bars = fetch_candle_windows(
        client,
        exchange="NSE",
        segment="FNO",
        groww_symbol="NSE-NIFTY-02Jan25-28500-PE",
        interval="1minute",
        start=date(2024, 1, 2),
        end=date(2024, 1, 1),  # start >= end → no windows
        drop_volume=False,
    )
    assert bars == [] and sdk.calls == []


# ── `%26` ampersand symbol-form fallback (DEC-GROWW-AMPERSAND-CANDLE-FALLBACK-001) ──


# Named like the SDK exception so from_groww_exception maps it to the RETRYABLE
# GrowwRateLimitError (mirrors tests/unit/test_groww_client.py).
class GrowwAPIRateLimitException(Exception):
    """Fake SDK 429 (mapped retryable by class name)."""


_CANDLE_PAYLOAD: Mapping[str, object] = {
    "candles": [["2024-01-01T09:15:00", 100.0, 110.0, 90.0, 105.0, 777, 42]]
}


class _FormFakeSDK:
    """Symbol-form-aware fake: a per-symbol QUEUE of payloads (last entry clamps).

    A symbol absent from *responses* returns a clean-EMPTY ``200`` (the un-encoded-``&``
    silent-miss shape). ``raises`` makes EVERY call raise (the transient path).
    Records every fetched ``groww_symbol`` in call order.
    """

    def __init__(
        self,
        responses: Mapping[str, Sequence[Mapping[str, object]]] | None = None,
        *,
        raises: Exception | None = None,
    ) -> None:
        self._responses = {k: list(v) for k, v in (responses or {}).items()}
        self._served: dict[str, int] = {}
        self._raises = raises
        self.symbols: list[str] = []

    def get_historical_candles(self, **kw: object) -> Mapping[str, object]:
        symbol = str(kw["groww_symbol"])
        self.symbols.append(symbol)
        if self._raises is not None:
            raise self._raises
        queue = self._responses.get(symbol)
        if not queue:
            return {"candles": []}
        idx = min(self._served.get(symbol, 0), len(queue) - 1)
        self._served[symbol] = idx + 1
        return queue[idx]

    def get_expiries(self, **_kw: object) -> Mapping[str, object]:  # pragma: no cover
        raise AssertionError("unused")

    def get_contracts(self, **_kw: object) -> Mapping[str, object]:  # pragma: no cover
        raise AssertionError("unused")


def _form_client(sdk: _FormFakeSDK, *, max_attempts: int = 6) -> GrowwClient:
    return GrowwClient(sdk=sdk, sleep=lambda _s: None, max_attempts=max_attempts)


def _fetch_mm(
    client: GrowwClient,
    *,
    symbol: str = "NSE-M&M",
    interval: str = "1day",
    start: date = date(2024, 1, 1),
    end: date = date(2024, 1, 2),
) -> list[object]:
    return list(
        fetch_candle_windows(
            client,
            exchange="NSE",
            segment="CASH",
            groww_symbol=symbol,
            interval=interval,
            start=start,
            end=end,
            drop_volume=False,
        )
    )


def test_ampersand_clean_empty_falls_back_to_encoded_form_and_wins() -> None:
    """Plain ``NSE-M&M`` returns clean-empty → ONE ``%26`` retry recovers the bars.

    The candle twin of the #898 resolution fallback: without it the M&M candles
    silently read empty forever even though the chain RESOLVED (the audit's #1
    finding). First non-empty form wins; the recovery is logged with the winner.
    """
    sdk = _FormFakeSDK({"NSE-M%26M": [_CANDLE_PAYLOAD]})
    with capture_logs() as logs:
        bars = _fetch_mm(_form_client(sdk))
    assert len(bars) == 1  # the encoded form's data came back
    assert sdk.symbols == ["NSE-M&M", "NSE-M%26M"]  # plain FIRST, encoded on clean-empty only
    events = [e for e in logs if e["event"] == "groww.candles.symbol_form_fallback_result"]
    assert len(events) == 1
    assert events[0]["form_won"] == "encoded" and events[0]["bars"] == 1
    assert events[0]["log_level"] == "info"  # a recovery is operator-visible


def test_ampersand_all_empty_returns_empty_after_both_forms() -> None:
    """BOTH forms clean-empty → the same honest empty as before (never fabricated)."""
    sdk = _FormFakeSDK()
    with capture_logs() as logs:
        bars = _fetch_mm(_form_client(sdk))
    assert bars == []
    assert sdk.symbols == ["NSE-M&M", "NSE-M%26M"]  # exactly one fallback retry, bounded
    events = [e for e in logs if e["event"] == "groww.candles.symbol_form_fallback_result"]
    assert len(events) == 1
    assert events[0]["form_won"] == "none"
    assert events[0]["log_level"] == "debug"  # a still-empty double-check stays quiet


def test_no_ampersand_symbol_clean_empty_never_falls_back() -> None:
    """A no-``&`` symbol is BYTE-IDENTICAL to before: one form, no retry, no log."""
    sdk = _FormFakeSDK()
    with capture_logs() as logs:
        bars = _fetch_mm(_form_client(sdk), symbol="NSE-NIFTY")
    assert bars == []
    assert sdk.symbols == ["NSE-NIFTY"]  # single form — zero extra vendor calls
    assert not [e for e in logs if str(e["event"]).startswith("groww.candles.symbol_form")]


def test_ampersand_symbol_with_data_never_falls_back() -> None:
    """A plain-form fetch that RETURNS bars never triggers the encoded retry."""
    sdk = _FormFakeSDK({"NSE-M&M": [_CANDLE_PAYLOAD]})
    bars = _fetch_mm(_form_client(sdk))
    assert len(bars) == 1
    assert sdk.symbols == ["NSE-M&M"]  # no fallback — plain form works


def test_ampersand_partial_data_across_windows_never_falls_back() -> None:
    """A multi-window fetch with data in ONLY one window is non-empty → no fallback.

    The fallback keys off the WHOLE tiled fetch being clean-empty, never per-window
    density (a thin-but-present series is real data, not a silent miss).
    """
    # 75 days at the 1minute 30-day tier → 3 windows; only the 2nd returns bars.
    sdk = _FormFakeSDK({"NSE-M&M": [{"candles": []}, _CANDLE_PAYLOAD, {"candles": []}]})
    bars = _fetch_mm(
        _form_client(sdk), interval="1minute", start=date(2024, 1, 1), end=date(2024, 3, 16)
    )
    assert len(bars) == 1
    assert sdk.symbols == ["NSE-M&M"] * 3  # 3 windows, plain form only


def test_ampersand_transient_error_propagates_without_fallback() -> None:
    """A RAISED transient (429) rides the retry ladder — it NEVER triggers the fallback.

    The fallback keys ONLY off a clean 200-empty; an exception propagates out of the
    plain-form fetch before the emptiness check, so the encoded form is never sent.
    """
    sdk = _FormFakeSDK(raises=GrowwAPIRateLimitException("Rate limit has breached"))
    with pytest.raises(GrowwRateLimitError):
        # max_attempts=1 → the client's own fast-retry never fires; the raise is direct.
        _fetch_mm(_form_client(sdk, max_attempts=1))
    assert sdk.symbols == ["NSE-M&M"]  # one attempt, encoded form NEVER tried


def test_ampersand_empty_range_no_fallback_no_calls() -> None:
    """An empty window plan (start >= end) fetches nothing — no fallback signal exists."""
    sdk = _FormFakeSDK()
    with capture_logs() as logs:
        bars = _fetch_mm(_form_client(sdk), start=date(2024, 1, 2), end=date(2024, 1, 1))
    assert bars == [] and sdk.symbols == []
    assert not [e for e in logs if str(e["event"]).startswith("groww.candles.symbol_form")]


def test_ampersand_fallback_fires_raw_payload_hook_for_both_forms() -> None:
    """The raw-integrity hook captures the fallback fetch too (every physical window)."""
    sdk = _FormFakeSDK({"NSE-M%26M": [_CANDLE_PAYLOAD]})
    captured: list[tuple[Mapping[str, object], str, str, str]] = []

    def _hook(payload: Mapping[str, object], interval: str, start: str, end: str) -> None:
        captured.append((payload, interval, start, end))

    bars = fetch_candle_windows(
        _form_client(sdk),
        exchange="NSE",
        segment="CASH",
        groww_symbol="NSE-M&M",
        interval="1day",
        start=date(2024, 1, 1),
        end=date(2024, 1, 2),
        drop_volume=False,
        on_raw_payload=_hook,
    )
    assert len(bars) == 1
    assert len(captured) == 2  # the plain (empty) AND the encoded (winning) fetch
    assert captured[0][0] == {"candles": []}
    assert captured[1][0] == _CANDLE_PAYLOAD
