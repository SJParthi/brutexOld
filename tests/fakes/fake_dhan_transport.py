"""``FakeDhanTransport`` — a deterministic, in-memory, network-free Dhan HTTP surface.

DEC-DHAN-ADAPTER-001. The Dhan client (:mod:`brutex.adapters.dhan.client`) makes its
network calls through the :class:`~brutex.adapters.dhan.transport.DhanTransport`
Protocol (``post_json`` / ``get_json`` → ``(status, json_body)``). This fake satisfies
that Protocol so it flows through the REAL client + REAL parsers — NOT a bypass —
driving them to 100% line+branch with NO ``requests`` install and NO network (the exact
role of ``tests/fakes/fake_groww_sdk.py`` + ``fake_aws_client.py``).

It is SCRIPTABLE: a per-call ``script`` of outcomes (a fixed response, or an exception
to raise for the transport-error paths — empty body / network blip / timeout), falling
back to a per-path ``default`` when the script is exhausted. Fully deterministic +
thread-safe (a lock guards the call counter).
"""

from __future__ import annotations

import json
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field


class FakeReadTimeout(Exception):
    """A fake socket read timeout — its class NAME matches ``_TIMEOUT_ERROR_NAMES``."""


# The client matches transport-timeout by ``type(exc).__name__`` — align exactly.
FakeReadTimeout.__name__ = "ReadTimeout"
FakeReadTimeout.__qualname__ = "ReadTimeout"


def empty_body_error() -> json.JSONDecodeError:
    """A fake empty/non-JSON body decode error → retryable DhanEmptyResponseError."""
    return json.JSONDecodeError("Expecting value", "", 0)


def network_error() -> ConnectionError:
    """A fake transient network blip → retryable DhanNetworkError (name ``ConnectionError``)."""
    return ConnectionError("Failed to resolve api.dhan.co")


def timeout_error() -> FakeReadTimeout:
    """A fake socket read timeout → retryable DhanTimeoutError."""
    return FakeReadTimeout("read timed out")


@dataclass(frozen=True, slots=True)
class Response:
    """A scripted ``(status, body)`` outcome."""

    status: int
    body: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class Raise:
    """A scripted transport EXCEPTION outcome (the client maps it via ``from_transport_exception``)."""

    exc: Exception


@dataclass(slots=True)
class FakeDhanTransport:
    """A deterministic in-memory :class:`~brutex.adapters.dhan.transport.DhanTransport`.

    Parameters
    ----------
    script
        Ordered outcomes consumed one per call (a :class:`Response` or a :class:`Raise`).
        When exhausted, *default* is returned. This drives retry / reauth ladders.
    default
        The ``(status, body)`` returned once the script is exhausted (a clean 200 + an
        empty body by default).
    """

    script: list[Response | Raise] = field(default_factory=list)
    default: Response = field(default_factory=lambda: Response(200, {}))

    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    calls: int = field(default=0, init=False)
    seen_paths: list[str] = field(default_factory=list, init=False)
    seen_headers: list[Mapping[str, str]] = field(default_factory=list, init=False)
    seen_bodies: list[Mapping[str, object]] = field(default_factory=list, init=False)

    def _next(
        self, *, path: str, headers: Mapping[str, str], body: Mapping[str, object]
    ) -> Response:
        with self._lock:
            index = self.calls
            self.calls += 1
            self.seen_paths.append(path)
            self.seen_headers.append(dict(headers))
            self.seen_bodies.append(dict(body))
            outcome = self.script[index] if index < len(self.script) else self.default
        if isinstance(outcome, Raise):
            raise outcome.exc
        return outcome

    def post_json(
        self, *, path: str, body: Mapping[str, object], headers: Mapping[str, str]
    ) -> tuple[int, Mapping[str, object]]:
        outcome = self._next(path=path, headers=headers, body=body)
        return outcome.status, outcome.body

    def get_json(
        self, *, path: str, headers: Mapping[str, str]
    ) -> tuple[int, Mapping[str, object]]:
        outcome = self._next(path=path, headers=headers, body={})
        return outcome.status, outcome.body

    # ── synthetic payloads the REAL parsers accept ─────────────────────────

    @staticmethod
    def candle_payload(*, n: int = 2, with_oi: bool = False) -> dict[str, object]:
        """A COLUMN-array candle payload the real ``parse_candle_columns`` accepts."""
        base_ts = 1_700_000_000
        payload: dict[str, object] = {
            "open": [100.0 + i for i in range(n)],
            "high": [101.0 + i for i in range(n)],
            "low": [99.0 + i for i in range(n)],
            "close": [100.5 + i for i in range(n)],
            "volume": [1000 + i for i in range(n)],
            "timestamp": [base_ts + 60 * i for i in range(n)],
        }
        if with_oi:
            payload["open_interest"] = [5000 + i for i in range(n)]
        return payload

    @staticmethod
    def rolling_option_payload(*, n: int = 2) -> dict[str, object]:
        """A separate-ce/pe rolling-option payload the real ``parse_rolling_option`` accepts."""
        base_ts = 1_700_000_000

        def side(strike: float) -> dict[str, object]:
            return {
                "open": [10.0 + i for i in range(n)],
                "high": [11.0 + i for i in range(n)],
                "low": [9.0 + i for i in range(n)],
                "close": [10.5 + i for i in range(n)],
                "volume": [100 + i for i in range(n)],
                "timestamp": [base_ts + 60 * i for i in range(n)],
                "oi": [2000 + i for i in range(n)],
                "strike": [strike for _ in range(n)],
                "spot": [20000.0 + i for i in range(n)],
                "iv": [0.15 for _ in range(n)],
            }

        return {"ce": side(20000.0), "pe": side(20000.0)}

    @staticmethod
    def option_chain_payload() -> dict[str, object]:
        """An option-chain payload the real ``parse_option_chain`` accepts."""
        return {
            "data": {
                "last_price": 20050.0,
                "oc": {
                    "20000.0": {
                        "ce": {"last_price": 120.0, "oi": 3000, "implied_volatility": 0.14},
                        "pe": {"last_price": 80.0, "oi": 2500, "implied_volatility": 0.16},
                    },
                    "20100.0": {
                        "ce": {"last_price": 70.0, "oi": 1500},
                        "pe": {"last_price": 130.0, "oi": 1800},
                    },
                },
            }
        }

    @staticmethod
    def expiry_list_payload(*, dates: Sequence[str] | None = None) -> dict[str, object]:
        """An expiry-list payload the real ``parse_expiry_list`` accepts."""
        return {
            "status": "success",
            "data": list(dates) if dates is not None else ["2026-08-27", "2026-09-24"],
        }


__all__ = [
    "FakeDhanTransport",
    "FakeReadTimeout",
    "Raise",
    "Response",
    "empty_body_error",
    "network_error",
    "timeout_error",
]
