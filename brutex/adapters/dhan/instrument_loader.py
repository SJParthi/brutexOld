"""Dhan instrument-master loader — download the scrip master → universe.

Glue over :mod:`brutex.adapters.dhan.instruments`: fetch the detailed
scrip-master CSV via an **injected** ``http_get`` (``url → text``), parse it
with :class:`csv.DictReader`, validate the header, and return the
brute-force universe (:func:`~brutex.adapters.dhan.instruments.select_universe`).

Injecting ``http_get`` keeps the parse/select logic 100% unit-testable
without a network; the real :func:`urllib_http_get` (SSRF-guarded
``urllib`` shim to ``images.dhan.co``) is the only ``# pragma: no cover``
surface, exactly like the Groww instrument fetcher.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Callable, Sequence
from typing import cast

from brutex.adapters.dhan.constants import INSTRUMENT_MASTER_DETAILED_URL
from brutex.adapters.dhan.instruments import (
    DhanInstrumentError,
    UniverseReport,
    select_universe_report,
    validate_header,
)
from brutex.models import Instrument

#: A CSV fetcher: ``url → decoded CSV text``.
HttpGet = Callable[[str], str]

#: Host the scrip-master CSV is served from (SSRF allowlist).
_ALLOWED_HOST: str = "images.dhan.co"

#: Whole-download timeout (seconds).
_DOWNLOAD_TIMEOUT: float = 60.0


def load_universe_report(
    http_get: HttpGet,
    *,
    url: str = INSTRUMENT_MASTER_DETAILED_URL,
    reference_security_ids: frozenset[int] = frozenset(),
    extra_index_symbols: frozenset[str] = frozenset(),
) -> UniverseReport:
    """Download + parse the detailed scrip master into a :class:`UniverseReport`.

    Returns the selected universe (all NSE indices + BSE SENSEX + every
    F&O-eligible single-stock SPOT) plus parse accounting (parsed / persisted
    / legs_dropped / filtered).

    ``reference_security_ids`` (DEC-NIFTY-TOTAL-MARKET-WIRE-001, Stage 2b PR-4b):
    optional NSE-equity ``SECURITY_ID``s to ALSO admit as REFERENCE_ONLY (the
    resolved NIFTY-Total-Market constituents), threaded to
    :func:`~brutex.adapters.dhan.instruments.select_universe_report`. Defaults
    empty → the selection is byte-identical to the F&O-only universe.

    ``extra_index_symbols`` (DEC-NIFTY-TOTAL-MARKET-DISCOVER-001): optional
    ``SYMBOL_NAME`` tokens to ALSO admit as INDEX rows (the runtime-discovered
    NIFTY Total Market token), threaded to ``select_universe_report``. Default
    empty → byte-identical.

    Raises
    ------
    DhanInstrumentError
        If the CSV header is missing required columns, or a universe row is
        malformed.
    """
    text = http_get(url)
    reader = csv.DictReader(io.StringIO(text), restval="")
    validate_header(reader.fieldnames)
    # validate_header raised if fieldnames was None/incomplete → safe to narrow.
    fieldnames = cast(Sequence[str], reader.fieldnames)
    rows = [{name: (row.get(name) or "") for name in fieldnames} for row in reader]
    return select_universe_report(
        rows,
        reference_security_ids=reference_security_ids,
        extra_index_symbols=extra_index_symbols,
    )


def load_universe(
    http_get: HttpGet,
    *,
    url: str = INSTRUMENT_MASTER_DETAILED_URL,
    reference_security_ids: frozenset[int] = frozenset(),
    extra_index_symbols: frozenset[str] = frozenset(),
) -> list[Instrument]:
    """Download + parse the detailed scrip master into the universe list.

    See :func:`load_universe_report`; ``reference_security_ids`` (default empty)
    admits extra NSE equities by ``SECURITY_ID`` as REFERENCE_ONLY, and
    ``extra_index_symbols`` (default empty) admits extra INDEX rows by
    ``SYMBOL_NAME`` (the discovered NIFTY Total Market token).
    """
    return load_universe_report(
        http_get,
        url=url,
        reference_security_ids=reference_security_ids,
        extra_index_symbols=extra_index_symbols,
    ).universe


def urllib_http_get(url: str) -> str:  # pragma: no cover — SSRF-guarded network shim
    """Real ``urllib`` CSV fetch, restricted to ``https://images.dhan.co``."""
    import urllib.request
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != _ALLOWED_HOST:
        raise DhanInstrumentError(f"refusing non-allowlisted instrument URL: {url!r}")
    with urllib.request.urlopen(  # nosec B310 — https + host allow-listed above  # noqa: S310
        url, timeout=_DOWNLOAD_TIMEOUT
    ) as resp:
        raw: bytes = resp.read()
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DhanInstrumentError(f"scrip-master CSV was not valid UTF-8: {exc}") from exc


__all__ = [
    "HttpGet",
    "load_universe",
    "load_universe_report",
    "urllib_http_get",
]
