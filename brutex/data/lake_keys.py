"""Pure lake spot-key normalisation — the #911 drift-recovery ladder, COMMON home.

DEC-LAKE-HEAL-001: moved verbatim from ``brutex/dashboard/completeness.py``
(``_resolve_spot_category``, DEC-COMPLETENESS-SPOT-KEY-RECOVER-001 #911) so the
data-side scanner/healer (:mod:`brutex.data.lake_invariants` /
``brutex.data.lake_heal``) reuse it WITHOUT a ``data → dashboard`` import (the
wrong dependency direction — ``brutex/data`` never imports ``brutex/dashboard``
at module level). ``completeness._resolve_spot_category`` is now a thin
delegating wrapper over :func:`resolve_spot_key` (the established thin-wrapper
pattern, cf. ``cli._emit_pull_halt_alert`` → ``sweep_alerts.emit_pull_halt_alert``).

This module is **intentionally pure** (stdlib only — no file I/O, no clock, no
polars/duckdb imports; :func:`fno_index_alias_pairs` reads ONE env var,
defensively), mirroring the :mod:`brutex.data.fno_symbol` precedent, so it is
importable from anywhere in the data layer without extras.

DEC-DB-MONEYNESS-KEY-RECOVER-001: the FULL F&O→spot drift-recovery ladder
(:func:`spot_recovery_candidates` — index aliases BOTH directions + the env
``BRUTEX_FNO_INDEX_ALIASES`` override + the ``"<EXCH>-<underlying>"`` #911 form
+ the :func:`spot_key_candidates` rungs) moved here VERBATIM from
``brutex/data/greeks_enrich.py`` (its DEC-GREEKS-CORRECTNESS-AUDIT-001
SPOTJOIN-2 fix) so the ``/db`` per-bar moneyness spot-join
(``brutex/dashboard/db_view.py``) and the Greeks enrichment share ONE candidate
builder (CLAUDE.md §11 COMMON — never copy-paste).

The two documented drift classes (and ONLY those — no case-folding, no
``&``-encoding recovery; Golden Rule #1, a genuinely unknown stray STAYS
unresolved) are:

1. surrounding whitespace (``"M&M "`` → ``"M&M"``) — an INTERIOR space
   (``"INDIA VIX"``) is a legitimate ``symbol_name`` and never touched;
2. the row's OWN ``"<exchange>-"`` prefix (``"NSE-M&M"`` → ``"M&M"``) —
   data-driven off the partition's exchange, never a hardcoded exchange list, so
   a hyphenated symbol like ``BAJAJ-AUTO`` is never mangled.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from typing import Final


def spot_key_candidates(symbol: str, exchange: str) -> tuple[str, ...]:
    """Ordered drift-recovery candidate keys for *symbol*, EXCLUDING the identity.

    Exactly the two #911 drift classes, in ladder order: (1) the
    whitespace-stripped form (only when it differs from *symbol*), (2) the row's
    OWN ``"<exchange>-"`` prefix stripped off the STRIPPED form (data-driven —
    ``BAJAJ-AUTO`` never mangled; a blank *exchange* skips this class). May
    return ``()`` — a clean key has no drift candidates. An empty-string
    candidate (the pathological ``"<exchange>-"`` dir name) is dropped: an empty
    key can never name a real partition dir or canonical symbol. The identity is
    excluded BY CONSTRUCTION: the stripped form is only added when it differs,
    and the prefix-stripped form is strictly shorter than the stripped form (a
    non-blank prefix), so it can equal neither *symbol* nor the first candidate.

    Parameters
    ----------
    symbol
        The on-disk partition directory name (a possibly-drifted spot key).
    exchange
        The partition's own exchange coordinate (``"NSE"`` / ``"BSE"``).

    Returns
    -------
    tuple[str, ...]
        The drift candidates in recovery-ladder order, identity excluded.
    """
    candidates: list[str] = []
    stripped = symbol.strip()
    if stripped != symbol and stripped:
        candidates.append(stripped)
    exch = exchange.strip()
    if exch:
        prefix = f"{exch}-"
        if stripped.startswith(prefix):
            candidate = stripped.removeprefix(prefix)
            if candidate:
                candidates.append(candidate)
    return tuple(candidates)


def resolve_spot_key[V](
    symbol: str, exchange: str, canonical: Mapping[str, V]
) -> tuple[str, V] | None:
    """Exact → stripped → ``"<exch>-"``-prefix-stripped lookup; byte-equivalent to #911.

    The #911 recovery ladder (DEC-COMPLETENESS-SPOT-KEY-RECOVER-001) generalised
    over the mapping value type: (1) exact ``canonical.get(symbol)``, (2) the
    whitespace-stripped form (only when it differs), (3) the row's OWN
    ``"<exchange>-"`` prefix stripped off the stripped form. The implementation
    is the ``completeness.py`` ``_resolve_spot_category`` body verbatim, with the
    mapping value made generic — a ``None`` VALUE in *canonical* reads as absent
    (exactly the #911 semantics).

    Parameters
    ----------
    symbol
        The on-disk partition directory name (a possibly-drifted spot key).
    exchange
        The partition's own exchange coordinate (``"NSE"`` / ``"BSE"``).
    canonical
        Canonical key → value mapping (e.g. the scored-category map, or an
        instrument-master ``symbol_name`` index).

    Returns
    -------
    tuple[str, V] | None
        ``(canonical_key, canonical[canonical_key])`` for the FIRST ladder rung
        that resolves, or ``None`` — a genuinely unknown stray STAYS unresolved,
        never fabricated (Golden Rule #1).
    """
    value = canonical.get(symbol)
    if value is not None:
        return symbol, value
    stripped = symbol.strip()
    if stripped != symbol:
        value = canonical.get(stripped)
        if value is not None:
            return stripped, value
    exch = exchange.strip()
    if exch:
        prefix = f"{exch}-"
        if stripped.startswith(prefix):
            candidate = stripped.removeprefix(prefix)
            value = canonical.get(candidate)
            if value is not None:
                return candidate, value
    return None


#: Sourced spot-``trading_symbol`` ↔ F&O ``underlying_symbol`` equivalences for
#: the two NSE index derivatives whose Groww spot ``trading_symbol`` differs
#: from the symbol the vendor stamps on the option legs (CLAUDE.md §6 / the
#: DEC-FNO-INDEX-ALIAS-001 sourced pairs). This deliberately MIRRORS
#: ``brutex/dashboard/instrument_loader._SOURCED_FNO_INDEX_ALIASES`` — the map
#: is not imported from there because ``brutex/data`` must never import
#: ``brutex/dashboard`` (the locked import direction); the env override below
#: keeps the two surfaces operator-extensible from the SAME single lever.
_SOURCED_INDEX_SPOT_ALIASES: Final[dict[str, str]] = {
    "NIFTYJR": "NIFTYNXT50",
    "NIFTYMIDSELECT": "MIDCPNIFTY",
}

#: The operator JSON override env (``{spot_trading_symbol: fno_underlying}``)
#: shared with the dashboard's ``resolve_fno_index_aliases`` — parsed LOCALLY
#: and defensively here (absent / malformed → sourced defaults only).
FNO_INDEX_ALIASES_ENV: Final[str] = "BRUTEX_FNO_INDEX_ALIASES"


def fno_index_alias_pairs() -> tuple[tuple[str, str], ...]:
    """The ``(spot trading_symbol, F&O underlying)`` pairs — sourced + env override.

    Reads :data:`FNO_INDEX_ALIASES_ENV` defensively (the exact contract of
    the dashboard's ``resolve_fno_index_aliases``): a missing / non-JSON /
    non-object payload or a non-``str→non-blank-str`` entry is IGNORED, never
    a crash — a lost override costs only the extra recovery rungs.
    """
    aliases = dict(_SOURCED_INDEX_SPOT_ALIASES)
    raw = os.environ.get(FNO_INDEX_ALIASES_ENV)
    if raw:
        try:
            parsed: object = json.loads(raw)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            for key, value in parsed.items():
                if (
                    isinstance(key, str)
                    and isinstance(value, str)
                    and key.strip()
                    and value.strip()
                ):
                    aliases[key.strip().upper()] = value.strip()
    return tuple(aliases.items())


def spot_recovery_candidates(underlying: str, exchange: str) -> tuple[str, ...]:
    """Ordered spot-key drift candidates for *underlying* — identity excluded.

    The FULL F&O→spot recovery ladder (DEC-GREEKS-CORRECTNESS-AUDIT-001
    SPOTJOIN-2, moved here verbatim per DEC-DB-MONEYNESS-KEY-RECOVER-001),
    every rung traceable to a repo-verified drift class (Golden Rule #1 —
    nothing invented): (1) the index-alias map in BOTH directions (reverse —
    the chain names the F&O ``underlying_symbol``, the spot dir its
    ``trading_symbol``; forward — the mirror case), (2) the
    ``"<exchange>-<underlying>"`` #911 drifted-directory form, (3) the
    :func:`spot_key_candidates` #911 ladder applied to *underlying* itself (a
    drifted-SHAPED underlying walks back down to its canonical key). STOCK
    drift pairs (``BAJAJ-AUTO`` vs ``BAJAJAUTO``) are deliberately ABSENT: the
    normalisation that produced them is not reversible without an instrument
    master, so the stock arm stays an honest no-spot + a named follow-up (the
    operator env lever above covers a known pair today via the reverse rung).
    """
    candidates: list[str] = []

    def _add(key: str) -> None:
        if key != underlying and key not in candidates:
            candidates.append(key)

    upper = underlying.strip().upper()
    for spot_key, fno_key in fno_index_alias_pairs():
        if fno_key.upper() == upper:
            _add(spot_key)
        if spot_key == upper:
            _add(fno_key)
    _add(f"{exchange}-{underlying}")
    for drifted in spot_key_candidates(underlying, exchange):
        _add(drifted)
    return tuple(candidates)


__all__ = [
    "FNO_INDEX_ALIASES_ENV",
    "fno_index_alias_pairs",
    "resolve_spot_key",
    "spot_key_candidates",
    "spot_recovery_candidates",
]
