"""Adaptive strike-chain bounding — drive fetches-per-window toward ~1.0 (no data lost).

DEC-FETCH-PER-WINDOW-GUARANTEE-001 (MIGRATION.md #1156). A measured pull of the whole
F&O option chain spends ``~2.54`` vendor fetches per ``(contract, interval)`` *window*,
almost entirely because the in-pull EMPTY-retry budget
(:data:`~brutex.adapters.ingest_common.DEFAULT_EMPTY_RETRY_ATTEMPTS` = ``3``) fires on EVERY
clean-but-empty window — and on an options chain the bulk of the strikes are deep
out-of-the-money and **never traded over the pulled window**, so they return a clean empty
``200`` and pay ``3×`` to re-confirm a strike that is dead anyway. The 3× empty-retry exists
to recover a transient vendor glitch on a window that DOES have data (``DEC-EMPTY-INPULL-RETRY-
001``); a strike far outside the underlying's traded range almost certainly has no data to
glitch-recover, so paying 3× there is pure waste.

This module turns that observation into a **DYNAMIC, data-driven** per-contract
empty-retry budget — NEVER a hardcoded NSE strike rule:

* It looks at the chain's OWN strike distribution to find a robust liquidity CENTER (the
  median strike — a spot/ATM proxy that needs no live spot fetch) and a band half-width
  measured in *strikes* (the band tracks the actual strike grid, so it is the same shape
  for a wide-grid index and a tight-grid stock).
* A contract whose strike is INSIDE the band keeps the FULL empty-retry budget (it is
  plausibly live — never starve a strike that might have data).
* A contract whose strike is OUTSIDE the band is "confirmed-deep": it gets a REDUCED
  empty-retry budget (default ``1``), so a dead far-OTM strike costs ONE fetch to confirm
  empty instead of three.

**Correctness — no data is ever lost (Golden Rule #1).** A reduced budget changes ONLY how
many times an EMPTY window is re-confirmed; a deep strike that DOES return bars on its single
fetch is stored EXACTLY as before (the first non-empty fetch always wins —
:func:`~brutex.adapters.groww.option_ingest.ingest_option_contract` breaks the empty loop the
moment any attempt returns bars). So this never SKIPS a strike, never drops a bar, and never
fabricates one — it only declines to pay 2 extra glitch-recovery fetches on a strike the data
says is dead. The transient (429 / timeout) retry ladder is UNTOUCHED (a deep strike that
429s is still retried), and the futures leg (``FUT`` — no strike) always keeps the full budget.

**Honest envelope (Golden Rule #10).** This is a HEURISTIC band, not an oracle: a genuinely-
live strike that happens to sit outside the median band loses 2 glitch-recovery attempts (it
is still fetched once, and if it returns data it is stored). The band is deliberately WIDE
(default ``±12`` strikes ≈ the liquid ATM±12 envelope) so the common liquid strikes always sit
inside; widen :data:`STRIKE_BAND_ENV` if a thin name needs more. Pure + O(chain) — one median
+ one pass over the chain, no network, no lake I/O.

**Scope (CLAUDE.md §3).** Option/future bars are LAKE / REFERENCE_ONLY (DEC-P0-063) — this
module bounds their reference-only ingest and NEVER touches the brute-force engine or the
shared condition vocabulary.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from statistics import median
from typing import Final

import structlog

from brutex.adapters.ingest_common import DEFAULT_EMPTY_RETRY_ATTEMPTS
from brutex.adapters.protocol import DerivativeContract

_LOG = structlog.get_logger("brutex.adapters.groww.strike_chain_bound")

#: The per-chain grouping key — ``(underlying_symbol, expiry)``. Keying on the UNDERLYING too
#: (not expiry alone) keeps two underlyings that share one monthly expiry from pooling their
#: strikes into one bogus median (DEC-FETCH-PER-WINDOW-GUARANTEE-001 — the multi-underlying
#: all-cached pull case). ``expiry`` is typed ``object`` because :class:`DerivativeContract`
#: allows ``expiry=None`` (a perpetual); a ``None`` expiry simply forms its own chain bucket.
_ChainKey = tuple[str, object]

#: The OPTION strike types this bounding applies to (``FUT`` has no strike and always keeps
#: the full budget). Mirrors :data:`~brutex.adapters.groww.contracts._OPTION_TYPES` — kept
#: local so this pure module imports nothing from the contracts resolver.
_OPTION_TYPES: Final[frozenset[str]] = frozenset({"CE", "PE"})

#: Default band half-width in STRIKES around the chain's median strike. A contract whose
#: strike rank distance from the median exceeds this is "confirmed-deep" → reduced empty-retry.
#: ``12`` ≈ the liquid ATM±12 envelope on a typical chain — deliberately WIDE so the common
#: liquid strikes always keep the full glitch-recovery budget (Golden Rule #10). Env-tunable
#: via :data:`STRIKE_BAND_ENV`.
DEFAULT_STRIKE_BAND: Final[int] = 12

#: Env override for the band half-width (:func:`resolve_strike_band`).
STRIKE_BAND_ENV: Final[str] = "BRUTEX_FNO_STRIKE_BAND"

#: The reduced in-pull EMPTY-retry budget for a confirmed-deep strike. ``1`` = fetch ONCE to
#: confirm the empty (the transient-error retry ladder is separate + untouched). Env-tunable
#: via :data:`DEEP_EMPTY_RETRY_ENV`. Clamped ``>= 1`` (a deep strike is ALWAYS fetched at least
#: once — never silently recorded empty without calling the vendor, the out-of-box guard the
#: ingest spine's ``max(1, …)`` also enforces).
DEFAULT_DEEP_EMPTY_RETRY: Final[int] = 1

#: Env override for the confirmed-deep empty-retry budget (:func:`resolve_deep_empty_retry`).
DEEP_EMPTY_RETRY_ENV: Final[str] = "BRUTEX_FNO_DEEP_EMPTY_RETRY"

#: A chain SMALLER than this many distinct strikes is left at the FULL budget for EVERY
#: contract — too few strikes to estimate a liquidity center, so bounding would be guessing
#: (false-positive guard, mirrors the suspicious-empty ``_ALL_EMPTY_MIN_CONTRACTS`` discipline).
_MIN_CHAIN_STRIKES: Final[int] = 7

__all__ = [
    "DEEP_EMPTY_RETRY_ENV",
    "DEFAULT_DEEP_EMPTY_RETRY",
    "DEFAULT_STRIKE_BAND",
    "STRIKE_BAND_ENV",
    "StrikeChainBudget",
    "build_empty_retry_budget",
    "resolve_deep_empty_retry",
    "resolve_strike_band",
]


def resolve_strike_band() -> int:
    """Resolve the band half-width (in strikes) from ``BRUTEX_FNO_STRIKE_BAND`` — default 12.

    Reads :data:`STRIKE_BAND_ENV`. Falls back to :data:`DEFAULT_STRIKE_BAND` when unset, blank,
    non-integer, or ``< 0`` — a bad value can never narrow the band to an unsafe width (a
    negative would starve every strike), so the fail-safe is the audited wide default. A band
    of ``0`` is permitted (only the exact-median strike keeps the full budget) for an operator
    who explicitly wants the most aggressive bounding. O(1).
    """
    raw = os.environ.get(STRIKE_BAND_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_STRIKE_BAND
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_STRIKE_BAND
    if value < 0:
        return DEFAULT_STRIKE_BAND
    return value


def resolve_deep_empty_retry() -> int:
    """Resolve the confirmed-deep empty-retry budget from env — default 1, clamped ``>= 1``.

    Reads :data:`DEEP_EMPTY_RETRY_ENV`. Falls back to :data:`DEFAULT_DEEP_EMPTY_RETRY` when
    unset, blank, or non-integer; a value ``< 1`` is clamped UP to ``1`` so a deep strike is
    ALWAYS fetched at least once (never recorded empty without a vendor call — the out-of-box
    guard). A value larger than the full default is allowed (an operator may want a gentler
    cut), but it can never EXCEED the full budget at use time (the budget is the ``min`` of
    this and the full attempts). O(1).
    """
    raw = os.environ.get(DEEP_EMPTY_RETRY_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_DEEP_EMPTY_RETRY
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_DEEP_EMPTY_RETRY
    return max(1, value)


@dataclass(frozen=True, slots=True)
class StrikeChainBudget:
    """The per-contract empty-retry budget map for ONE pull, plus an honest summary.

    Attributes
    ----------
    by_symbol
        ``{contract groww_symbol: empty_retry_attempts}`` — present ONLY for contracts whose
        budget DIFFERS from *full_attempts* (the reduced confirmed-deep strikes). A symbol
        ABSENT from the map keeps *full_attempts* (the never-narrow default), so the map is
        sparse and the ingest spine treats an absent symbol byte-identically to today.
    full_attempts
        The full empty-retry budget every in-band (and futures) contract keeps.
    deep_attempts
        The reduced budget the mapped (confirmed-deep) contracts get.
    deep_contracts
        Count of contracts assigned the reduced budget (the fetches-saved driver).
    total_contracts
        Count of OPTION legs actually CONSIDERED for bounding — the in-band + deep options
        across all evaluated `(underlying, expiry)` chains. EXCLUDES the FUT legs (no strike)
        and the small-chain options left FULL, so the `deep_contracts / total_contracts` ratio
        honestly reflects the option-level reduction, not an inflated whole-list denominator.
    """

    by_symbol: Mapping[str, int]
    full_attempts: int
    deep_attempts: int
    deep_contracts: int
    total_contracts: int

    def attempts_for(self, symbol: str) -> int:
        """The empty-retry budget for *symbol* — the reduced budget iff it is confirmed-deep.

        O(1) — a single dict lookup, defaulting to *full_attempts* for any symbol not in the
        sparse :attr:`by_symbol` map (every in-band / futures / unmapped contract).
        """
        return self.by_symbol.get(symbol, self.full_attempts)


def build_empty_retry_budget(
    contracts: Iterable[DerivativeContract],
    *,
    full_attempts: int = DEFAULT_EMPTY_RETRY_ATTEMPTS,
    band: int | None = None,
    deep_attempts: int | None = None,
) -> StrikeChainBudget:
    """Build the per-contract empty-retry budget for one chain — pure, O(chain).

    DEC-FETCH-PER-WINDOW-GUARANTEE-001. Groups the OPTION (``CE`` / ``PE``) contracts by
    ``(underlying_symbol, expiry)`` — NOT expiry alone — and within each chain finds the median
    strike (a robust ATM/liquidity-center proxy that needs no live spot fetch). A contract whose
    strike sits OUTSIDE the ``±band``-strikes envelope around ITS chain's median is
    "confirmed-deep" and gets the REDUCED *deep_attempts* empty-retry budget; every other
    contract (in-band options, every ``FUT``, every contract in a too-small chain) keeps
    *full_attempts*. Keying on the underlying too is essential for the production all-cached
    pull (one call builds the budget over a chain spanning MANY underlyings): two underlyings
    sharing the same monthly expiry must NOT have their strikes pooled into one median, or the
    pooled center lands in the inter-cluster gap and over-flags live strikes (no data is lost,
    but glitch-recovery on potentially-live strikes erodes — the per-(underlying,expiry) key
    keeps every median its OWN underlying's ATM proxy).

    The band is measured in STRIKES (rank distance on the sorted distinct-strike list of the
    chain), NOT in absolute price, so it adapts to the strike grid: a wide-grid index
    (50-pt strikes) and a tight-grid stock (2.50-pt strikes) get the same ``±band``-strike
    envelope. A chain with FEWER than :data:`_MIN_CHAIN_STRIKES` distinct strikes is left
    FULL for every contract (too few strikes to estimate a center — the false-positive guard).

    **Never narrows below safe (Golden Rule #1/#10).** *deep_attempts* is clamped into
    ``[1, full_attempts]`` so a deep strike is always fetched at least once and never MORE than
    the full budget; ``band < 0`` or a degenerate input falls back to the wide default. The
    returned map is SPARSE — only the reduced contracts appear, so an empty map is byte-
    identical to the pre-feature full-budget behaviour.

    Parameters
    ----------
    contracts
        The resolved derivative chain (typically one underlying's whole CE/PE/FUT set across
        the pulled expiries). Materialised once.
    full_attempts
        The full empty-retry budget (default :data:`~brutex.adapters.ingest_common.DEFAULT_EMPTY_RETRY_ATTEMPTS`).
    band
        Band half-width in strikes (``None`` → :func:`resolve_strike_band`). ``< 0`` is treated
        as the default.
    deep_attempts
        Confirmed-deep budget (``None`` → :func:`resolve_deep_empty_retry`), clamped into
        ``[1, full_attempts]``.

    Returns
    -------
    StrikeChainBudget
        The sparse per-contract budget map + the honest summary counts.
    """
    contract_list = list(contracts)
    resolved_band = resolve_strike_band() if band is None else max(0, band)
    resolved_deep = resolve_deep_empty_retry() if deep_attempts is None else deep_attempts
    # Clamp the deep budget into [1, full]: at least one fetch (never record empty without a
    # vendor call), at most the full budget (a "reduction" must never become an inflation).
    safe_full = max(1, full_attempts)
    safe_deep = min(max(1, resolved_deep), safe_full)

    # Group OPTION strikes by (underlying_symbol, expiry) → the distinct sorted strike list per
    # chain. Keying on the UNDERLYING too (not expiry alone) is essential for the production
    # all-cached pull, where ONE call builds the budget over a chain spanning MANY underlyings:
    # two underlyings sharing the same monthly expiry (the common last-Thursday case) must NOT
    # have their strikes POOLED into one median — RELIANCE (₹2800-3200) + HDFCBANK (₹1600-1780)
    # on the same expiry would land the pooled median in the inter-cluster gap and over-flag
    # live-cluster strikes (it cannot lose data, but it erodes glitch-recovery on live strikes
    # and makes "median = ATM proxy" false for a mixed pull). The composite key keeps each
    # underlying's chain independent so every median is its OWN underlying's ATM proxy
    # (the per-chain key type is the module-level :data:`_ChainKey`).
    strikes_by_chain: dict[_ChainKey, list[float]] = {}
    for contract in contract_list:
        if contract.contract_type not in _OPTION_TYPES or contract.strike is None:
            continue
        chain_key: _ChainKey = (contract.underlying_symbol, contract.expiry)
        strikes_by_chain.setdefault(chain_key, []).append(contract.strike)

    # Per (underlying, expiry): the median (center) + the sorted distinct strikes for
    # rank-distance bounding.
    center_by_chain: dict[_ChainKey, float] = {}
    sorted_strikes_by_chain: dict[_ChainKey, list[float]] = {}
    for chain_key, strikes in strikes_by_chain.items():
        distinct = sorted(set(strikes))
        if len(distinct) < _MIN_CHAIN_STRIKES:
            continue  # too few strikes to bound — leave the whole chain FULL (false-positive guard)
        sorted_strikes_by_chain[chain_key] = distinct
        center_by_chain[chain_key] = median(distinct)

    by_symbol: dict[str, int] = {}
    deep = 0
    # ``options_considered`` counts only the OPTION legs this bounding actually evaluated (an
    # in-band or deep classification), so the deep/total ratio is honest about the option-level
    # reduction — it EXCLUDES the FUT legs (no strike) + the small-chain options left FULL, which
    # ``len(contract_list)`` would have inflated the denominator with (the #919-style honesty).
    options_considered = 0
    for contract in contract_list:
        if contract.contract_type not in _OPTION_TYPES or contract.strike is None:
            continue
        chain_key = (contract.underlying_symbol, contract.expiry)
        chain_strikes = sorted_strikes_by_chain.get(chain_key)
        if chain_strikes is None:
            continue  # small chain left full
        options_considered += 1
        if _is_deep(contract.strike, chain_strikes, center_by_chain[chain_key], resolved_band):
            by_symbol[contract.symbol] = safe_deep
            deep += 1

    if deep:
        _LOG.info(
            "strike_chain.budget",
            deep_contracts=deep,
            options_considered=options_considered,
            total_contracts=len(contract_list),
            band=resolved_band,
            deep_attempts=safe_deep,
            full_attempts=safe_full,
        )
    return StrikeChainBudget(
        by_symbol=by_symbol,
        full_attempts=safe_full,
        deep_attempts=safe_deep,
        deep_contracts=deep,
        total_contracts=options_considered,
    )


def _is_deep(strike: float, distinct_strikes: list[float], center: float, band: int) -> bool:
    """Is *strike* MORE than *band* strikes (by rank) away from the chain's *center*? — pure.

    The rank distance is the count of distinct strikes lying strictly BETWEEN *strike* and the
    nearest strike to *center* — measured on the sorted distinct-strike list so the envelope is
    expressed in strikes (grid-relative), not absolute price. A ``band`` of ``0`` marks every
    non-center strike deep; a strike AT/inside the band is never deep (returns ``False``). The
    *center* (the median) is guaranteed to be one of the distinct strikes for an odd count and
    the lower-middle average for an even count — either way ``min(range(...), key=…)`` finds the
    nearest grid strike to it, so the rank arithmetic is well-defined for any chain.
    """
    # The grid index nearest the center (the ATM rank), and this strike's grid index.
    center_idx = min(range(len(distinct_strikes)), key=lambda i: abs(distinct_strikes[i] - center))
    strike_idx = min(range(len(distinct_strikes)), key=lambda i: abs(distinct_strikes[i] - strike))
    return abs(strike_idx - center_idx) > band
