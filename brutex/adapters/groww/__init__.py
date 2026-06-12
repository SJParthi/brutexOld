"""Groww REST adapter for BruteX Track-1 backtesting (DEC-GROWW-REPIVOT-001).

Plain urllib transport — no growwapi SDK dependency. Only ``pyotp`` is
required for TOTP token generation (``pip install 'brutex[groww]'``).

Quick-start
-----------
.. code-block:: python

    from brutex.adapters.groww import GrowwMarketAdapter, build_adapter

    adapter = build_adapter()          # reads env vars; downloads CSV
    bars = adapter.fetch_ohlcv(
        symbol="NIFTY",
        exchange=Exchange.NSE,
        interval="1minute",
        start=date(2024, 1, 1),
        end=date(2024, 1, 31),
    )
"""

from __future__ import annotations

from brutex.adapters.groww.adapter import GrowwMarketAdapter
from brutex.adapters.groww.auth import GrowwAuth, build_from_env
from brutex.adapters.groww.client import GrowwClient
from brutex.adapters.groww.instruments import (
    GrowwInstrument,
    InstrumentRegistry,
    build_brute_force_registry,
    load_registry_from_csv,
)


def build_adapter(
    *,
    use_csv: bool = True,
) -> GrowwMarketAdapter:
    """Construct a production :class:`GrowwMarketAdapter` from environment variables.

    Parameters
    ----------
    use_csv:
        If ``True`` (default), downloads the full Groww instrument CSV and
        filters to index + equity instruments. If ``False``, uses the
        hardcoded :func:`~brutex.adapters.groww.instruments.build_brute_force_registry`
        (faster startup; only 4 brute-force instruments).

    Raises
    ------
    OSError
        If required env vars (``BRUTEX_GROWW_API_KEY``,
        ``BRUTEX_GROWW_TOTP_SECRET``) are absent.
    """
    auth = build_from_env()
    client = GrowwClient(auth=auth)
    if use_csv:
        registry = load_registry_from_csv(
            instrument_types=frozenset({"IDX", "EQ"}),
        )
    else:
        registry = build_brute_force_registry()
    return GrowwMarketAdapter(client=client, registry=registry)


__all__ = [
    "GrowwAuth",
    "GrowwClient",
    "GrowwInstrument",
    "GrowwMarketAdapter",
    "InstrumentRegistry",
    "build_adapter",
    "build_brute_force_registry",
    "build_from_env",
    "load_registry_from_csv",
]
