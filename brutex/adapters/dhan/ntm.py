"""NIFTY Total Market (NTM) — runtime-verified reference index + constituents.

NTM is admitted as a **REFERENCE_ONLY** index — it is fetched + stored but NEVER
brute-forced (Golden Rule #3 scope-lock; the engine stays NIFTY/BANKNIFTY/SENSEX,
locked by DEC-ENGINE-ISOLATION-INVARIANT-001) — alongside its ~755 constituents
(also REFERENCE_ONLY, resolved by ISIN against the Dhan master).

The two facts BruteX cannot derive — NTM's Dhan ``SECURITY_ID`` + symbol — are
**VERIFIED from Dhan's own data** and cross-checked against the LIVE master at
runtime, so nothing here is a hallucinated guess (Golden Rule #1). The Dhan web
chart ``web.dhan.co/Charts?exch=IDX&seg=I&secid=46`` ("NIFTY TOTAL MKT · 1 · IDX",
operator screenshot 2026-06-06) shows the CHART's ``secid=46``; the operator's
live ``make run`` then resolved the **detailed-master** ``SECURITY_ID = 443`` for
that same "NIFTY TOTAL MKT" INDEX row (``universe.load.ntm_admitted
index_security_id=443``, verified 2026-06-06) — and **443 is the id the candle
API needs**, so it is the cross-check value (DEC-NIFTY-TOTAL-MARKET-ID-RECONCILE-001).

→ ``SECURITY_ID = 443``, symbol ``"NIFTY TOTAL MKT"``. :func:`resolve_ntm_index`
locates the matching ``INDEX`` row in the live master by pattern and FAILS LOUD if
its ``SECURITY_ID`` disagrees with this verified id — the real-time assurance that
the master still matches what the operator confirmed before any pull trusts the id
(DEC-NIFTY-TOTAL-MARKET-DISCOVER-001).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from brutex.adapters.dhan.instruments import (
    DiscoveredIndex,
    IndexDiscoveryError,
    discover_index,
    parse_nse_equities,
)
from brutex.dashboard.nse_constituents import parse_constituents
from brutex.models import Exchange
from brutex.universe_constituents import resolve_reference_security_ids

#: NTM's Dhan detailed-master ``SECURITY_ID`` — the id the candle API needs.
#: VERIFIED 2026-06-06 from the operator's live ``make run``
#: (``universe.load.ntm_admitted index_security_id=443``). NOTE: the Dhan web
#: CHART URL shows ``secid=46`` (its chart-segment param) — a DIFFERENT value from
#: the detailed-master ``SECURITY_ID`` (443); 443 is the candle-API id, so it is
#: the verified cross-check (DEC-NIFTY-TOTAL-MARKET-ID-RECONCILE-001).
NTM_SECURITY_ID: Final[int] = 443

#: NTM's Dhan display symbol — VERIFIED from the chart title + the live load.
NTM_SYMBOL: Final[str] = "NIFTY TOTAL MKT"

#: Case-insensitive regex matched against ``INDEX`` rows' ``SYMBOL_NAME`` in the
#: live master. Loose enough to match the web token "NIFTY TOTAL MKT" AND a
#: "...MARKET" spelling, since the master's exact abbreviation is the one fact we
#: read at runtime rather than assert; :func:`discover_index`'s ambiguity guard
#: protects against an over-broad match resolving to >1 index.
NTM_NAME_PATTERN: Final[str] = r"TOTAL\s*M(?:AR)?K(?:E)?T"


@dataclass(frozen=True, slots=True)
class NtmReferencePlan:
    """The inputs ``refresh_instruments`` needs to admit + tag NTM REFERENCE_ONLY.

    ``index`` is the live-master ``INDEX`` row located + id-verified by
    :func:`resolve_ntm_index`. ``extra_index_symbols`` admits that index row;
    ``reference_security_ids`` (the index's own id ∪ the matched constituent ids)
    both ADMITS the constituent equities and TAGS the index + constituents
    ``reference_only=TRUE`` at upsert. ``constituent_count`` is the number of
    matched constituents (excludes the index id) for an at-a-glance audit.
    """

    index: DiscoveredIndex
    reference_security_ids: frozenset[int]
    extra_index_symbols: frozenset[str]
    constituent_count: int


def resolve_ntm_index(
    master_rows: Iterable[Mapping[str, str]],
    *,
    pattern: str = NTM_NAME_PATTERN,
    expected_security_id: int | None = NTM_SECURITY_ID,
    exchange: Exchange = Exchange.NSE,
) -> DiscoveredIndex:
    """Locate NTM's ``INDEX`` row in the live master and verify its id.

    Wraps :func:`discover_index` (which fails loud on zero/ambiguous matches),
    then cross-checks the discovered ``SECURITY_ID`` against the operator-verified
    Dhan id. ``expected_security_id=None`` skips the cross-check (pure discovery).

    Raises
    ------
    IndexDiscoveryError
        From :func:`discover_index` (no match / ambiguous / no id), OR when the
        discovered id disagrees with ``expected_security_id`` — meaning the live
        master and Dhan's web app no longer agree, so the operator must
        investigate before trusting the pull.
    """
    found = discover_index(master_rows, pattern=pattern, exchange=exchange)
    if expected_security_id is not None and found.security_id != expected_security_id:
        raise IndexDiscoveryError(
            f"NTM index discovered as {found.symbol_name!r} id={found.security_id}, "
            f"but the operator-verified Dhan detailed-master SECURITY_ID is "
            f"{expected_security_id} (verified 2026-06-06 via the live make-run "
            f"ntm_admitted log) — the live master disagrees with the confirmed id; "
            f"investigate before trusting the pull"
        )
    return found


def prepare_ntm_reference_plan(
    master_rows: Sequence[Mapping[str, str]],
    constituent_csv_text: str,
    *,
    pattern: str = NTM_NAME_PATTERN,
    expected_security_id: int | None = NTM_SECURITY_ID,
    require_full_coverage: bool = True,
) -> NtmReferencePlan:
    """Build the :class:`NtmReferencePlan` from the live master + constituent CSV.

    End-to-end, network-free assembly (the operator fetches both inputs):

    1. :func:`resolve_ntm_index` — locate + id-verify NTM's index row.
    2. :func:`brutex.dashboard.nse_constituents.parse_constituents` — parse the
       niftyindices constituent CSV (Symbol + ISIN Code).
    3. :func:`brutex.universe_constituents.resolve_reference_security_ids` —
       resolve constituents → NSE cash-equity ``SECURITY_ID``s, FAIL LOUD on any
       unmatched constituent when ``require_full_coverage`` (so "all 755" can
       never silently become "755 − N" — DEC-NIFTY-TOTAL-MARKET-WIRE-001).
    4. Assemble ``extra_index_symbols`` (admits the index row) +
       ``reference_security_ids`` (index id ∪ constituent ids — admits the
       constituents AND tags index + constituents ``reference_only=TRUE``).

    Raises
    ------
    IndexDiscoveryError
        If the NTM index cannot be located or its id disagrees (step 1).
    ConstituentCoverageError
        If ``require_full_coverage`` and any constituent is unmatched (step 3).
    """
    index = resolve_ntm_index(
        master_rows, pattern=pattern, expected_security_id=expected_security_id
    )
    constituents = parse_constituents(constituent_csv_text)
    equities = parse_nse_equities(master_rows)
    constituent_ids = resolve_reference_security_ids(
        constituents, equities, require_full_coverage=require_full_coverage
    )
    return NtmReferencePlan(
        index=index,
        reference_security_ids=frozenset({index.security_id}) | constituent_ids,
        extra_index_symbols=frozenset({index.symbol_name.upper()}),
        constituent_count=len(constituent_ids),
    )


__all__ = [
    "NTM_NAME_PATTERN",
    "NTM_SECURITY_ID",
    "NTM_SYMBOL",
    "NtmReferencePlan",
    "prepare_ntm_reference_plan",
    "resolve_ntm_index",
]
