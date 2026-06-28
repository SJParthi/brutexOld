"""Spot ``trading_symbol`` → Groww F&O ``underlying_symbol`` resolution (COMMON).

DEC-FNO-UNDERLYING-RESOLVE-001. The single source of truth for the F&O
underlying-symbol form(s) to hand the Groww SDK's ``get_expiries`` /
``get_contracts`` for an F&O-eligible spot row.

**The bug this closes.** The metadata F&O pull
(:func:`brutex.dashboard.metadata_pull._underlying_grid`) keyed the expiry/contract
chain on the SPOT ``trading_symbol`` (``Instrument.symbol_name``) — e.g.
``BAJAJ-AUTO`` (hyphenated), ``NIFTY NEXT 50`` (spaced), ``NIFTYJR`` (a genuine
abbreviation). Groww's F&O addressing wants the symbol the vendor stamps on the
contract legs as ``underlying_symbol`` (``BAJAJAUTO`` / ``NIFTYNEXT50`` /
``NIFTYNXT50``). When the wrong form is passed, ``get_expiries`` returns a
clean-but-EMPTY ``200`` → 0 expiries → 0 contracts → the WHOLE underlying is silently
missed (the "701 F&O-eligible spots → 0 expiries" symptom), which also leaves the
``/movers`` futures + options CHANGE / CHANGE% / OI columns blank.

**Why not just read** ``Instrument.underlying_symbol``? For the F&O-eligible CASH /
INDEX SPOT rows the lean universe persists (DEC-INSTRUMENT-UNIVERSE-FAST-001), the
Groww master leaves ``underlying_symbol`` EMPTY — it is populated ONLY on the
DERIVATIVE legs, which the lean universe discards. So a ``BAJAJ-AUTO`` EQ row has
``underlying_symbol = None``; the F&O form must be DERIVED from ``symbol_name``.

**The derivation (COMMON, no invention — Golden Rule #1).** Two already-proven repo
bridges, applied in order, with a no-worse-than-before fallback:

1. **Sourced alias map** (``aliases`` — ``NIFTYJR`` → ``NIFTYNXT50`` /
   ``NIFTYMIDSELECT`` → ``MIDCPNIFTY``, env-extensible
   ``BRUTEX_FNO_INDEX_ALIASES``; DEC-FNO-INDEX-ALIAS-001). For the genuine
   abbreviations that normalization cannot bridge.
2. **Normalization** (:func:`~brutex.universe.normalize_underlying_symbol` —
   uppercase, strip ASCII space + ``-``; DEC-FNO-INDEX-MATCH-DIAG-001). The exact,
   total transform ``BAJAJ-AUTO`` → ``BAJAJAUTO`` / ``NIFTY NEXT 50`` →
   ``NIFTYNEXT50``. A no-op for a symbol with no space/hyphen, so the 3 brute-force
   indices (NIFTY / BANKNIFTY / SENSEX) + most stocks are byte-unchanged.
3. **The original spot spelling** appended LAST as a try-also fallback — so if a
   vendor already answers the raw spot symbol we never regress, and a genuinely
   absent chain records the same empty as before (never a fabricated chain).

The forms feed :func:`~brutex.adapters.groww.contracts.resolve_expiries` /
``resolve_contracts``, which already try each form in turn (the ``&`` → ``%26``
ampersand fallback rides ON TOP per DEC-GROWW-AMPERSAND-FALLBACK-001), so handing
them the correct F&O form keeps every existing fallback working.

**Scope (CLAUDE.md §3):** lake / REFERENCE_ONLY pull-side only — the brute-force
ENGINE is UNCHANGED (NIFTY / BANKNIFTY / SENSEX). Pure functions, no I/O, no network.
"""

from __future__ import annotations

from collections.abc import Mapping

from brutex.dashboard.instrument_loader import (
    FNO_INDEX_ALIASES_OVERRIDE_ENV,
    resolve_fno_index_aliases,
)
from brutex.universe import normalize_underlying_symbol


def fno_underlying_forms(spot_symbol: str, *, aliases: Mapping[str, str]) -> tuple[str, ...]:
    """Return the F&O underlying-symbol form(s) to try for *spot_symbol*, in order.

    The ordered, de-duplicated tuple of underlying spellings to hand the Groww SDK:

    1. the sourced/override **alias** for *spot_symbol* (keyed by its UPPER-cased,
       trimmed spelling), when present — for a genuine abbreviation
       (``NIFTYJR`` → ``NIFTYNXT50``);
    2. the **normalized** form (:func:`~brutex.universe.normalize_underlying_symbol`)
       — the case/space/hyphen bridge (``BAJAJ-AUTO`` → ``BAJAJAUTO``);
    3. the **original** *spot_symbol* spelling as a try-also fallback.

    Order-preserving first-wins de-dup, so a symbol that needs no bridge (an alias-free
    symbol with no space/hyphen, e.g. ``NIFTY`` / ``BANKNIFTY``) collapses to ONE form
    — zero extra SDK calls, byte-identical to the pre-fix behaviour.

    Parameters
    ----------
    spot_symbol
        The spot ``trading_symbol`` (``Instrument.symbol_name``), e.g.
        ``"BAJAJ-AUTO"``, ``"NIFTYJR"``, ``"NIFTY"``.
    aliases
        The sourced + operator-override spot→F&O index alias map (keys UPPER-cased +
        trimmed). Injected (CLAUDE.md §4 — no global mutable state);
        :func:`resolve_fno_underlying_forms` supplies the production map.

    Returns
    -------
    tuple[str, ...]
        At least one form (the original spelling is always a candidate); 1–3 forms,
        de-duplicated, in try order. Never empty.
    """
    forms: list[str] = []

    def _add(form: str) -> None:
        if form and form not in forms:
            forms.append(form)

    alias = aliases.get(spot_symbol.strip().upper())
    if alias is not None:
        _add(alias.strip())
    _add(normalize_underlying_symbol(spot_symbol))
    _add(spot_symbol)
    return tuple(forms)


def primary_fno_underlying(spot_symbol: str, *, aliases: Mapping[str, str]) -> str:
    """Return the FIRST (preferred) F&O underlying form for *spot_symbol*.

    The head of :func:`fno_underlying_forms` — the single best-guess F&O underlying
    spelling, used as the grid key the metadata pull persists. Never empty (the
    original spelling is always a fallback candidate).
    """
    return fno_underlying_forms(spot_symbol, aliases=aliases)[0]


def resolve_fno_underlying_forms(spot_symbol: str) -> tuple[str, ...]:
    """:func:`fno_underlying_forms` with the PRODUCTION alias map resolved.

    Reads the sourced + operator-override index alias map via
    :func:`brutex.dashboard.instrument_loader.resolve_fno_index_aliases` (the single
    source of truth for the sourced defaults + the ``BRUTEX_FNO_INDEX_ALIASES``
    override). Pure given the environment.
    """
    return fno_underlying_forms(spot_symbol, aliases=resolve_fno_index_aliases())


__all__ = [
    "FNO_INDEX_ALIASES_OVERRIDE_ENV",
    "fno_underlying_forms",
    "primary_fno_underlying",
    "resolve_fno_underlying_forms",
]
