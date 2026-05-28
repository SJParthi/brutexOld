"""End-to-end: `GrowwCandleFetcher` driven by the real `run_fetch` loop.

Proves the broker-neutral exception translation actually closes the loop —
the ``Vendor*`` names the client mints are exactly the keys
:func:`brutex.orchestrator.retry.classify_exception` knows, so a fake Groww
SDK raising fake ``Groww*`` exceptions retries / re-auths / quarantines / settles
correctly. If a translated name were wrong, ``classify_exception`` would raise
``UnknownExceptionError`` and these tests would fail loudly.

Imports the orchestrator (LMDB/Bloom chain) so it runs in the global suite,
not the standalone per-module coverage gate.
"""

from __future__ import annotations

from unittest import mock

from brutex.adapters.groww.client import GrowwCandleFetcher
from brutex.adapters.groww.fetch_loop import run_fetch
from brutex.data.watermark import ChunkKey, ChunkStatus, FetchWatermark


class BaseGrowwException(Exception): ...


class GrowwAPIException(BaseGrowwException): ...


class GrowwAPIRateLimitException(GrowwAPIException): ...


class GrowwAPIAuthenticationException(GrowwAPIException): ...


class GrowwAPINotFoundException(GrowwAPIException): ...


class GrowwAPIBadRequestException(GrowwAPIException): ...


class _ScriptedSDK:
    """Pops one action per ``get_historical_candles`` call (raise or return)."""

    EXCHANGE_NSE = "EX_NSE"
    SEGMENT_CASH = "SEG_CASH"
    CANDLE_INTERVAL_MIN_1 = "CI_MIN_1"

    def __init__(self, actions: list[object]) -> None:
        self._actions = actions
        self.calls = 0

    def get_historical_candles(self, **_kw: object) -> object:
        action = self._actions[self.calls]
        self.calls += 1
        if isinstance(action, BaseException):
            raise action
        return action


def _key() -> ChunkKey:
    return ChunkKey("NSE-NIFTY", "1m", "2021-01-04T09:15:00+00:00", "2021-02-03T09:15:00+00:00")


def _fetcher(actions: list[object], **kw: object) -> GrowwCandleFetcher:
    return GrowwCandleFetcher(
        sdk=_ScriptedSDK(actions),
        bucket=mock.Mock(),
        base_exception=BaseGrowwException,
        **kw,  # type: ignore[arg-type]
    )


_ZERO_DELAY = lambda _n: 0.0  # noqa: E731 — deterministic, no real backoff


def test_rate_limit_then_success_completes() -> None:
    wm = FetchWatermark()
    fetcher = _fetcher([GrowwAPIRateLimitException("429"), {"candles": [["bar"]]}])
    out = run_fetch(fetcher, [_key()], wm, sleep_fn=mock.Mock(), delay_fn=_ZERO_DELAY)
    assert out.completed == 1
    assert wm.get(_key()) == ChunkStatus.COMPLETED


def test_not_found_settles_no_data() -> None:
    wm = FetchWatermark()
    fetcher = _fetcher([GrowwAPINotFoundException("404")])
    out = run_fetch(fetcher, [_key()], wm, sleep_fn=mock.Mock(), delay_fn=_ZERO_DELAY)
    assert out.no_data == 1
    assert wm.get(_key()) == ChunkStatus.NO_DATA


def test_bad_request_quarantines() -> None:
    wm = FetchWatermark()
    fetcher = _fetcher([GrowwAPIBadRequestException("bad")])
    out = run_fetch(fetcher, [_key()], wm, sleep_fn=mock.Mock(), delay_fn=_ZERO_DELAY)
    assert out.failed == 1
    assert wm.get(_key()) == ChunkStatus.FAILED


def test_auth_expiry_reauths_then_completes() -> None:
    wm = FetchWatermark()
    fresh = _ScriptedSDK([{"candles": [["bar"]]}])
    fetcher = _fetcher([GrowwAPIAuthenticationException("401")], reauth_fn=lambda: fresh)
    out = run_fetch(
        fetcher,
        [_key()],
        wm,
        reauth=fetcher.reauth,
        sleep_fn=mock.Mock(),
        delay_fn=_ZERO_DELAY,
    )
    assert out.completed == 1
    assert wm.get(_key()) == ChunkStatus.COMPLETED
