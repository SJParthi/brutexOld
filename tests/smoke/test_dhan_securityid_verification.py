"""Opt-in smoke test: verify every Track 1 securityId against web.dhan.co.

This test is **gated by the ``BRUTEX_DHAN_SMOKE`` env var** and is
skipped by default. It is intended to run quarterly (per CLAUDE.md §6:247)
to catch any silent re-assignment of securityIds by Dhan.

To run locally::

    BRUTEX_DHAN_SMOKE=1 pytest tests/smoke/test_dhan_securityid_verification.py -v

What this test verifies:

* For each of the 4 Track 1 spot instruments
  (NIFTY=13, BANKNIFTY=25, SENSEX=51, INDIAVIX=21), GET
  ``https://web.dhan.co/Charts?exch=IDX&seg=I&secid=N`` and assert the
  response is HTTP 200. A 404 / 500 / redirect-to-error-page indicates
  that the securityId no longer points to the expected instrument.

What this test does NOT verify:

* That the response body actually renders the instrument we expect
  (web.dhan.co is a single-page React app — meaningful body inspection
  requires a headless browser, out of scope).
* That the Dhan Data API ``POST /v2/charts/intraday`` for the same
  securityId returns OHLC for the expected ticker (that's a follow-up
  smoke test with credentials).

Security posture:

* ``httpx.Timeout(30.0)`` — never hangs forever.
* ``verify=True`` — TLS certificate verification on by default.
* ``follow_redirects=False`` — a 30x is treated as anomaly (logged
  but the test still asserts 200, so a 302 to login fails the test).
* Response body is NOT logged — only status + length, to avoid
  leaking any session cookies / CSRF tokens / partial UI state that
  might end up in the response page.
"""

from __future__ import annotations

import os

import pytest
from brutex.adapters.dhan.constants import ALL_SPOT_INSTRUMENTS, DhanSpotInstrument

# httpx is in the optional ``[dhan]`` extra. If the runner doesn't have it
# installed, skip the entire smoke module — opt-in by design.
httpx = pytest.importorskip("httpx")


pytestmark = [
    pytest.mark.smoke,
    pytest.mark.skipif(
        not os.environ.get("BRUTEX_DHAN_SMOKE"),
        reason="opt-in smoke test — set BRUTEX_DHAN_SMOKE=1 to run",
    ),
]


_WEB_DHAN_CHART_URL = "https://web.dhan.co/Charts"
_HTTP_TIMEOUT_SECONDS = 30.0


@pytest.mark.parametrize(
    "instrument",
    ALL_SPOT_INSTRUMENTS,
    ids=[inst.symbol for inst in ALL_SPOT_INSTRUMENTS],
)
def test_security_id_resolves_on_web_dhan_co(instrument: DhanSpotInstrument) -> None:
    """Each Track 1 securityId returns HTTP 200 on the chart page.

    A non-200 response means the securityId has been reassigned by Dhan
    (or web.dhan.co is down — re-run before opening an issue).
    """
    params = {
        "exch": "IDX",
        "seg": "I",
        "secid": str(instrument.security_id),
    }
    with httpx.Client(
        timeout=httpx.Timeout(_HTTP_TIMEOUT_SECONDS),
        verify=True,
        follow_redirects=False,
    ) as client:
        response = client.get(_WEB_DHAN_CHART_URL, params=params)

    assert response.status_code == 200, (
        f"Dhan chart endpoint returned {response.status_code} for "
        f"{instrument.symbol} (securityId={instrument.security_id}). "
        f"Verify the securityId is still valid at "
        f"{_WEB_DHAN_CHART_URL}?exch=IDX&seg=I&secid={instrument.security_id}"
    )
