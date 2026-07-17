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
defensively) + the default-OFF Rust seam facade (structlog transitively),
mirroring the :mod:`brutex.data.fno_symbol` precedent, so it is
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

DEC-GREEKS-SPOTJOIN-STOCK-001: case/space/hyphen folding happens ONLY inside
the master-VERIFIED injected ``stock_drift`` map (evidence-grounded — built by
:mod:`brutex.data.spot_drift` from the REAL on-disk spot dirs cross-checked
against the instrument master; :func:`stock_drift_fold` is the shared fold);
the raw ladder itself remains fold-free and unconditional case-folding remains
refused, and the ``&``-encoding refusal above stands verbatim.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from typing import Final

from brutex.data import lake_keys_rust

#: The evidence-grounded stock drift map (DEC-GREEKS-SPOTJOIN-STOCK-001):
#: ``(exchange, fold)`` → the ORDERED on-disk spot dir names that fold to that
#: unique master symbol (master-exact name first, then lexicographic — the #911
#: "best real data wins" rank-merge semantics; the PROBE decides which fragment
#: carries data). Built and verified in :mod:`brutex.data.spot_drift`; this
#: module stays pure and I/O-free — the map is passed IN. Treat as frozen: the
#: production value is a plain ``dict`` so it can cross the spawn pickle
#: boundary (never a ``MappingProxyType``). Importable by name
#: (``from brutex.data.lake_keys import StockDriftMap``) but intentionally
#: NOT listed in ``__all__``: it is a PEP 695 ``type`` statement, which some
#: static analysers (CodeQL) do not yet model as an ``__all__`` binding, and
#: type aliases are idiomatically not part of the ``import *`` surface anyway.
type StockDriftMap = Mapping[tuple[str, str], tuple[str, ...]]


def stock_drift_fold(symbol: str) -> str:
    """Irreversible-normalisation fold used ONLY for evidence matching.

    EXACTLY mirrors :func:`brutex.universe.normalize_underlying_symbol`
    (``symbol.upper().replace(" ", "").replace("-", "")``) — the
    documented-mirror pattern of :data:`_SOURCED_INDEX_SPOT_ALIASES` /
    the #1038 ``_is_pool_isolatable`` pin: ``lake_keys`` cannot import
    :mod:`brutex.universe` (this module is a pure stdlib leaf), so a
    mirror-pin unit test enforces agreement and both sites carry this
    cross-reference comment. NO ``'%26'``→``'&'`` arm (the stored-key
    encoding is UNVERIFIED — the module-docstring pledge stands); NO prefix
    arm (the own-exchange legacy-prefix strip is DISK-SIDE ONLY, in
    :mod:`brutex.data.spot_drift`, never applied to master symbols or
    lookup-time underlyings). ``replace(" ", "")`` folds interior AND
    trailing/leading ASCII spaces; other whitespace is deliberately NOT
    folded (a tab-bearing dir is junk → unverified — on EVERY arm: the
    disk-side prefix strip in :mod:`brutex.data.spot_drift` is likewise
    ASCII-space-only, ``strip(" ")``, so it cannot launder a tab-/
    newline-wrapped dir past this pledge).

    Delegates through the default-OFF :mod:`brutex.data.lake_keys_rust`
    seam (DEC-DATA-RUST-LAKE-KEYS-001); :func:`_stock_drift_fold_python`
    is the verbatim body — the live parity oracle every degrade path ships.
    """
    return lake_keys_rust.routed_stock_drift_fold(symbol, python_law=_stock_drift_fold_python)


def _stock_drift_fold_python(symbol: str) -> str:
    """The verbatim :func:`stock_drift_fold` body — the law-A parity oracle."""
    return symbol.upper().replace(" ", "").replace("-", "")


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

    Delegates through the default-OFF :mod:`brutex.data.lake_keys_rust`
    seam (DEC-DATA-RUST-LAKE-KEYS-001); :func:`_spot_key_candidates_python`
    is the verbatim body — the live parity oracle every degrade path ships.
    """
    return lake_keys_rust.routed_spot_key_candidates(
        symbol, exchange, python_law=_spot_key_candidates_python
    )


def _spot_key_candidates_python(symbol: str, exchange: str) -> tuple[str, ...]:
    """The verbatim :func:`spot_key_candidates` body — the law-B parity oracle."""
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


def spot_recovery_candidates(
    underlying: str,
    exchange: str,
    *,
    stock_drift: StockDriftMap | None = None,
) -> tuple[str, ...]:
    """Ordered spot-key drift candidates for *underlying* — identity excluded.

    The FULL F&O→spot recovery ladder (DEC-GREEKS-CORRECTNESS-AUDIT-001
    SPOTJOIN-2, moved here verbatim per DEC-DB-MONEYNESS-KEY-RECOVER-001),
    every rung traceable to a repo-verified drift class (Golden Rule #1 —
    nothing invented): (1) the index-alias map in BOTH directions (reverse —
    the chain names the F&O ``underlying_symbol``, the spot dir its
    ``trading_symbol``; forward — the mirror case), (2) the
    ``"<exchange>-<underlying>"`` #911 drifted-directory form, (3) the
    :func:`spot_key_candidates` #911 ladder applied to *underlying* itself (a
    drifted-SHAPED underlying walks back down to its canonical key), (4) the
    STOCK drift rung (``BAJAJ-AUTO`` vs ``BAJAJAUTO``) — supplied
    evidence-grounded via the optional *stock_drift* map
    (:mod:`brutex.data.spot_drift`, DEC-GREEKS-SPOTJOIN-STOCK-001: every
    candidate is a REAL on-disk spot dir verified against a UNIQUE instrument
    master fold-match); this module stays pure and I/O-free — the map is
    built elsewhere and injected. ``stock_drift=None`` (the default) or an
    empty map is byte-identical to the pre-stock ladder, so the stock rung can
    only APPEND: ``spot_recovery_candidates(u, e, stock_drift=m)`` always
    startswith ``spot_recovery_candidates(u, e)`` (the pinned prefix
    invariant).

    Delegates through the default-OFF :mod:`brutex.data.lake_keys_rust`
    seam (DEC-DATA-RUST-LAKE-KEYS-001): the alias pairs (env read) + the
    ``stock_drift`` dict PROBE run ONCE here and feed BOTH arms, so
    burn-in compares LAWS, not I/O; the fold for the probe rides the
    routed :func:`stock_drift_fold` (internally consistent).
    :func:`_spot_recovery_candidates_python` is the parity oracle.
    """
    pairs = fno_index_alias_pairs()
    bucket: tuple[str, ...] = ()
    if stock_drift:
        fold = stock_drift_fold(underlying)
        if fold:
            bucket = tuple(stock_drift.get((exchange, fold), ()))
    return lake_keys_rust.routed_spot_recovery_candidates(
        underlying,
        exchange,
        alias_pairs=pairs,
        drift_bucket=bucket,
        python_law=_spot_recovery_candidates_python,
    )


def _spot_recovery_candidates_python(
    underlying: str,
    exchange: str,
    *,
    alias_pairs: Sequence[tuple[str, str]],
    drift_bucket: Sequence[str],
) -> tuple[str, ...]:
    """The verbatim :func:`spot_recovery_candidates` body — the law-C oracle.

    Signature surgery (DEC-DATA-RUST-LAKE-KEYS-001, documented + pinned):
    (a) the inline ``fno_index_alias_pairs()`` call becomes the
    *alias_pairs* param and (b) the ``if stock_drift:`` fold+dict-probe
    block becomes the *drift_bucket* param — both computed ONCE in the
    public wrapper and fed to BOTH arms, so a mid-call env write can never
    diverge a burn-in compare; (c) the #911 ladder rung calls
    :func:`_spot_key_candidates_python` directly — the oracle arm stays
    oracle-PURE (a routed call inside the oracle would let Rust
    contaminate the burn-in reference).
    """
    candidates: list[str] = []

    def _add(key: str) -> None:
        if key != underlying and key not in candidates:
            candidates.append(key)

    upper = underlying.strip().upper()
    for spot_key, fno_key in alias_pairs:
        if fno_key.upper() == upper:
            _add(spot_key)
        if spot_key == upper:
            _add(fno_key)
    _add(f"{exchange}-{underlying}")
    for drifted in _spot_key_candidates_python(underlying, exchange):
        _add(drifted)
    for recovered in drift_bucket:
        _add(recovered)
    return tuple(candidates)


__all__ = [
    "FNO_INDEX_ALIASES_ENV",
    "fno_index_alias_pairs",
    "resolve_spot_key",
    "spot_key_candidates",
    "spot_recovery_candidates",
    "stock_drift_fold",
]
