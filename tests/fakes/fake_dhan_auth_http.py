"""``FakeDhanAuthHttp`` — a deterministic, network-free :class:`HttpTransport` for Dhan auth.

DEC-DHAN-AUTH-001. Satisfies :class:`~brutex.adapters.dhan.auth.HttpTransport` so the real
mint / renew code flows through it (not a bypass), driving every branch to 100% with NO
network. Scripts a queue of per-call outcomes (a JSON dict to RETURN, or an exception to
RAISE) and records the full request transcript so a test can pin exact URLs / headers /
call-count.

Fixture secrets are obviously fake (``FAKE-`` prefixed, no ``eyJ`` JWT strings).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


class FakeHttpError(Exception):
    """A fake transport error carrying an HTTP ``status`` the classifier reads.

    Its ``__str__`` deliberately embeds the *url* (as a real urllib ``HTTPError`` would) so a
    test can prove the auth boundary SCRUBS it — the fake never sees the secrets, the message
    just carries whatever the caller passes.
    """

    def __init__(self, status: int | None, message: str = "fake http error") -> None:
        super().__init__(message)
        self.status = status


@dataclass
class _Request:
    method: str
    url: str
    headers: dict[str, str]
    timeout_s: float


@dataclass
class FakeDhanAuthHttp:
    """A scripted :class:`HttpTransport`.

    Parameters
    ----------
    responses
        A list consumed left-to-right, one per ``request_json`` call. Each item is either a
        ``Mapping`` (returned as the JSON body) or an ``Exception`` (raised).
    """

    responses: list[Mapping[str, Any] | Exception] = field(default_factory=list)
    requests: list[_Request] = field(default_factory=list, init=False)

    @property
    def call_count(self) -> int:
        """Number of ``request_json`` calls made so far."""
        return len(self.requests)

    def request_json(
        self, method: str, url: str, *, headers: Mapping[str, str], timeout_s: float
    ) -> Mapping[str, Any]:
        """Return / raise the next scripted outcome, recording the request."""
        self.requests.append(
            _Request(method=method, url=url, headers=dict(headers), timeout_s=timeout_s)
        )
        if not self.responses:
            raise AssertionError("FakeDhanAuthHttp exhausted: no scripted response left")
        outcome = self.responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


__all__ = ["FakeDhanAuthHttp", "FakeHttpError"]
