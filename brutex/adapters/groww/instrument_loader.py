"""Groww instrument-master loader — download the live CSV → O(1) index.

DEC-DHAN-GROWW-REMIGRATE-001 (ADR-0009 §5). Thin glue over
:mod:`brutex.adapters.groww.instruments`: fetch the live instrument-master CSV
via an **injected** ``http_get`` (``url → text``) behind an SSRF allowlist, then
build the :class:`~brutex.adapters.groww.instruments.InstrumentIndex`.

Injecting ``http_get`` keeps the parse/index logic 100% unit-testable without a
network; the real :func:`urllib_http_get` (SSRF-guarded ``urllib`` shim to
``growwapi-assets.groww.in``) is the only ``# pragma: no cover`` surface, exactly
like the Dhan loader. The instrument master is a point-in-time snapshot (expiries
roll), so production fetches it fresh each ingest run.
"""

from __future__ import annotations

from collections.abc import Callable
from urllib.parse import urlparse

from brutex.adapters.groww.constants import GROWW_CSV_ALLOWED_HOST, GROWW_INSTRUMENT_CSV_URL
from brutex.adapters.groww.instruments import InstrumentIndex, load_index_from_csv
from brutex.models import BruteXError

#: A CSV fetcher: ``url → decoded CSV text``.
HttpGet = Callable[[str], str]

#: Whole-download timeout (seconds).
_DOWNLOAD_TIMEOUT: float = 60.0


class GrowwInstrumentFetchError(BruteXError):
    """The instrument-master URL is not the allowlisted https host (SSRF guard)."""


def _check_url(url: str) -> None:
    """Refuse any URL that is not ``https://`` on the allowlisted Groww host."""
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise GrowwInstrumentFetchError(f"instrument-master URL must be https, got {url!r}")
    if parsed.hostname != GROWW_CSV_ALLOWED_HOST:
        raise GrowwInstrumentFetchError(
            f"instrument-master host not allowlisted ({GROWW_CSV_ALLOWED_HOST}): {parsed.hostname!r}"
        )


def urllib_http_get(url: str) -> str:  # pragma: no cover — real network shim
    """SSRF-guarded ``urllib`` GET returning decoded CSV text (the live default)."""
    import urllib.request

    _check_url(url)
    with urllib.request.urlopen(url, timeout=_DOWNLOAD_TIMEOUT) as resp:  # noqa: S310 — host allowlisted by _check_url
        return str(resp.read().decode("utf-8"))


def load_index(http_get: HttpGet, *, url: str = GROWW_INSTRUMENT_CSV_URL) -> InstrumentIndex:
    """Download the instrument master via *http_get* and build the O(1) index.

    The URL is SSRF-checked here (independent of the injected getter), so even a
    custom ``http_get`` cannot be pointed at a non-allowlisted host.
    """
    _check_url(url)
    return load_index_from_csv(http_get(url))


__all__ = [
    "GrowwInstrumentFetchError",
    "HttpGet",
    "load_index",
    "urllib_http_get",
]
