"""Pure lake spot-key normalisation — the #911 drift-recovery ladder, COMMON home.

DEC-LAKE-HEAL-001: moved verbatim from ``brutex/dashboard/completeness.py``
(``_resolve_spot_category``, DEC-COMPLETENESS-SPOT-KEY-RECOVER-001 #911) so the
data-side scanner/healer (:mod:`brutex.data.lake_invariants` /
``brutex.data.lake_heal``) reuse it WITHOUT a ``data → dashboard`` import (the
wrong dependency direction — ``brutex/data`` never imports ``brutex/dashboard``
at module level). ``completeness._resolve_spot_category`` is now a thin
delegating wrapper over :func:`resolve_spot_key` (the established thin-wrapper
pattern, cf. ``cli._emit_pull_halt_alert`` → ``sweep_alerts.emit_pull_halt_alert``).

This module is **intentionally pure** (stdlib only — no I/O, no clock, no
polars/duckdb imports), mirroring the :mod:`brutex.data.fno_symbol` precedent, so
it is importable from anywhere in the data layer without extras.

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

from collections.abc import Mapping


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


__all__ = ["resolve_spot_key", "spot_key_candidates"]
