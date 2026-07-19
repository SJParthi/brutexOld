"""Underlying-class (Indices / Stocks / All) filter for the ``/movers`` screens (COMMON).

``DEC-MOVERS-UNDERLYING-CLASS-001``. The ONE source of truth (CLAUDE.md §11 COMMON)
for the ``All | Indices | Stocks`` underlying-class selector shared by all three
``/movers`` tabs (Spot / Futures / Options). It follows the
``DEC-MOVERS-MIN-VOLUME-001`` template exactly: a single ``normalize_*`` helper +
ONE predicate over a candidate's already-resolved kind, applied at **RANK time** in
``build_*_payload`` over the SAME cached candidate set — never a re-scan, never a
new lake read.

## Classification is viewer-reads-storage-only (Golden Rule #1)

The index-vs-stock kind is resolved from the **STORED** instrument master, never
derived live:

* **Spot** — the candidate's lake *segment* IS the stored reflection of the
  master's ``is_index`` (an ``IDX`` row is ingested under the neutral ``INDEX``
  segment, an ``EQ`` row under ``CASH`` — CLAUDE.md §6), so
  :func:`spot_kind_from_segment` maps ``INDEX`` → index, ``CASH`` → stock, anything
  else → unknown.
* **Futures / Options** — the contract's parsed underlying token is looked up in a
  ``{underlying → kind}`` map built from the stored master via
  :func:`build_underlying_kind_map`, which REUSES the SAME
  :func:`~brutex.dashboard.fno_underlying.primary_fno_underlying` + ``is_index``
  classification the six-step pull order uses
  (``DEC-PULL-SIX-STEP-ORDER-001`` — ``historical_pull._underlying_kind_map``), so
  the two never diverge. The map KEYS are the ``primary_fno_underlying`` FORMS
  (``NIFTYJR`` → ``NIFTYNXT50``, ``BAJAJ-AUTO`` → ``BAJAJAUTO``), while the
  candidate's PARSED underlying token is right-anchored and hyphen-PRESERVING
  (:func:`~brutex.data.fno_symbol.parse_fno_symbol` yields ``BAJAJ-AUTO``, never
  ``BAJAJAUTO``) — so :func:`fno_kind_from_underlying` normalises the token INTO
  the map's key space AT the lookup, through the SAME resolver + alias map the
  builder uses (review round 1: the prior raw ``dict`` lookup silently
  misclassified every hyphenated / aliased F&O underlying as ``unknown``,
  dropping it from BOTH Indices and Stocks).

An underlying whose kind cannot be resolved is :data:`KIND_UNKNOWN`: it is honestly
INCLUDED in ``All`` but EXCLUDED from BOTH ``Indices`` and ``Stocks`` (never a
guess — Golden Rule #1), and the drop is COUNTED so the payload note can surface it.

## Why this is a rank-time filter, not a candidate-key dimension

Unlike the Near/Next/Far expiry tier (``DEC-MOVERS-FUTURES-TIER-001``, which
changes WHICH contract each underlying contributes and so MUST be in
``candidate_key`` — the #917 stale-cache lesson), the underlying class does NOT
change which candidates are computed: every candidate carries its resolved
``underlying_kind`` and the class merely filters the shared set at rank time. It is
therefore deliberately NOT part of ``candidate_key`` (exactly like the
``DEC-MOVERS-MIN-VOLUME-001`` floor), so ONE cached candidate set correctly serves
all three classes — Indices vs Stocks filter that same set to different rows.

Pure, I/O-free, standalone (imports only the stdlib + the COMMON F&O-underlying
resolver + the segment vocabulary).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Final, Protocol

import structlog

from brutex.dashboard.fno_underlying import BLANK_SYMBOL_SAMPLE_CAP, primary_fno_underlying
from brutex.dashboard.instrument_loader import resolve_fno_index_aliases
from brutex.data.partition import SPOT_PROBE_SEGMENTS
from brutex.models import Instrument

__all__ = [
    "CLASS_ALL",
    "CLASS_INDICES",
    "CLASS_STOCKS",
    "KIND_INDEX",
    "KIND_STOCK",
    "KIND_UNKNOWN",
    "build_underlying_kind_map",
    "candidate_passes_class",
    "class_echo",
    "class_note_suffix",
    "filter_by_underlying_class",
    "fno_kind_from_underlying",
    "normalize_underlying_class",
    "spot_kind_from_segment",
]

_LOG = structlog.get_logger(__name__)

#: The three canonical underlying-class selector tokens (lower-case wire form).
CLASS_ALL: Final[str] = "all"
CLASS_INDICES: Final[str] = "indices"
CLASS_STOCKS: Final[str] = "stocks"

#: The three canonical resolved-kind tokens a candidate can carry.
KIND_INDEX: Final[str] = "index"
KIND_STOCK: Final[str] = "stock"
#: The honest sentinel for an underlying whose index-vs-stock kind could not be
#: resolved from the stored master — included in ``all``, excluded from BOTH
#: ``indices`` and ``stocks`` (never a guess, Golden Rule #1).
KIND_UNKNOWN: Final[str] = "unknown"

#: The two spot segments an underlying's cash/index series lives under, mapped to
#: the resolved kind. ``INDEX`` (an ``IDX`` master row) → index, ``CASH`` (an ``EQ``
#: row) → stock — the stored reflection of the master's ``is_index`` (CLAUDE.md §6).
#: Keyed off the ONE :data:`~brutex.data.partition.SPOT_PROBE_SEGMENTS` vocabulary so
#: the segment names can never drift (CLAUDE.md §11 COMMON).
_INDEX_SEGMENT: Final[str] = SPOT_PROBE_SEGMENTS[0]  # "INDEX"
_CASH_SEGMENT: Final[str] = SPOT_PROBE_SEGMENTS[1]  # "CASH"

#: The set of selector tokens that are a real class filter (i.e. not the default).
_KNOWN_CLASSES: Final[frozenset[str]] = frozenset({CLASS_ALL, CLASS_INDICES, CLASS_STOCKS})


def normalize_underlying_class(raw: str | None) -> str:
    """Normalise the raw ``?underlying_class=`` selector to a canonical token.

    Lower-cases the value; a blank / ``None`` / unknown value ⇒ :data:`CLASS_ALL`
    (the byte-identical default — no filtering). The single source of truth for the
    normalisation so the rank-time filter can never be fed a stale spelling
    (CLAUDE.md §11 COMMON) — the underlying-class twin of
    :func:`~brutex.dashboard.movers_futures.normalize_futures_tier` /
    :func:`~brutex.dashboard.movers_snapshot.normalize_min_volume`.

    Parameters
    ----------
    raw
        The raw ``?underlying_class=`` query value (``All`` / ``Indices`` /
        ``Stocks`` from the page, any case; blank / ``None`` / unknown ⇒
        :data:`CLASS_ALL`).

    Returns
    -------
    str
        One of :data:`CLASS_ALL` / :data:`CLASS_INDICES` / :data:`CLASS_STOCKS`.
    """
    token = (raw or CLASS_ALL).lower()
    return token if token in _KNOWN_CLASSES else CLASS_ALL


def spot_kind_from_segment(segment: str) -> str:
    """Resolve a SPOT candidate's index-vs-stock kind from its lake *segment*.

    The spot segment is the STORED reflection of the master's ``is_index``
    (``IDX`` → ``INDEX`` segment, ``EQ`` → ``CASH`` segment — CLAUDE.md §6), so this
    is viewer-reads-storage-only, no live derivation, no master re-read. ``INDEX`` →
    :data:`KIND_INDEX`, ``CASH`` → :data:`KIND_STOCK`, any other spelling →
    :data:`KIND_UNKNOWN` (honest, never a guess — Golden Rule #1).

    Parameters
    ----------
    segment
        The candidate's lake partition segment (``INDEX`` / ``CASH``).

    Returns
    -------
    str
        One of :data:`KIND_INDEX` / :data:`KIND_STOCK` / :data:`KIND_UNKNOWN`.
    """
    if segment == _INDEX_SEGMENT:
        return KIND_INDEX
    if segment == _CASH_SEGMENT:
        return KIND_STOCK
    return KIND_UNKNOWN


def build_underlying_kind_map(instruments: Iterable[Instrument]) -> dict[str, str]:
    """Build ``{F&O underlying key → kind}`` from the STORED instrument master.

    REUSES the SAME classification the six-step pull order uses
    (``DEC-PULL-SIX-STEP-ORDER-001`` — ``historical_pull._underlying_kind_map`` /
    ``composite_pull._underlying_kind_map``): each instrument's key goes through
    :func:`~brutex.dashboard.fno_underlying.primary_fno_underlying` (the same alias /
    normalization bridge — ``NIFTYJR`` → ``NIFTYNXT50``, ``BAJAJ-AUTO`` →
    ``BAJAJAUTO``), so the map key space is the ``primary_fno_underlying`` FORM
    space — never re-minted (Golden Rule #11 COMMON). NOTE (review round 1): the
    parsed contract underlying tokens the futures/options movers carry are
    hyphen-PRESERVING (``BAJAJ-AUTO``), NOT this form space — the lookup side
    (:func:`fno_kind_from_underlying`) folds each token into it through the SAME
    resolver, so the two sides meet at the lookup, not "by construction". The value
    is :data:`KIND_INDEX` / :data:`KIND_STOCK` from the master's ``is_index``.
    First-wins on a key collision (two spot spellings sharing one F&O primary),
    mirroring the pull-side de-dup.

    A blank / whitespace-only ``symbol_name`` row (retained in the universe by the
    instrument parser, which excludes it only from the PAIR maps —
    ``adapters/groww/instruments.py``) is SKIPPED + counted here with ONE aggregate
    ``movers.underlying_kind_blank_symbol_skipped`` WARNING per build — one junk
    vendor row never poisons the whole map (guarantee-audit BUG-3,
    DEC-FNO-UNDERLYING-EMPTY-GUARD-001); before this guard
    ``primary_fno_underlying("")`` raised ``IndexError`` and the entire compute
    died.

    Parameters
    ----------
    instruments
        The stored universe underlyings (e.g.
        ``InstrumentStore.list_index_equity_instruments()``).

    Returns
    -------
    dict[str, str]
        ``{primary_fno_underlying(symbol_name): KIND_INDEX | KIND_STOCK}``.
    """
    aliases = resolve_fno_index_aliases()
    kinds: dict[str, str] = {}
    skipped = 0
    samples: list[str] = []
    for inst in instruments:
        if not inst.symbol_name.strip():
            skipped += 1
            if len(samples) < BLANK_SYMBOL_SAMPLE_CAP:
                samples.append(f"{inst.exch_id.value}:{inst.security_id}")
            continue
        key = primary_fno_underlying(inst.symbol_name, aliases=aliases)
        if key not in kinds:
            kinds[key] = KIND_INDEX if inst.is_index else KIND_STOCK
    if skipped:
        _LOG.warning(
            "movers.underlying_kind_blank_symbol_skipped",
            skipped=skipped,
            samples=samples,  # security-id refs — the symbol is blank, name what we can
        )
    return kinds


def fno_kind_from_underlying(
    underlying: str, kinds: dict[str, str] | None, *, aliases: Mapping[str, str]
) -> str:
    """Resolve a FUTURES/OPTIONS candidate's kind from its parsed *underlying* token.

    The *kinds* map (:func:`build_underlying_kind_map`) is keyed by
    :func:`~brutex.dashboard.fno_underlying.primary_fno_underlying` FORMS
    (``BAJAJAUTO``), but the contract's parsed underlying token is right-anchored
    and hyphen-PRESERVING (``BAJAJ-AUTO`` —
    :func:`~brutex.data.fno_symbol.parse_fno_symbol`), so the token is normalised
    into the map's key space HERE, through the SAME resolver + *aliases* map the
    builder uses — never a second normalisation law (CLAUDE.md §11 COMMON). This is
    the review-round-1 fix: the prior raw ``dict`` lookup silently misclassified
    every hyphenated / aliased F&O underlying as unknown. An absent map (``None``,
    e.g. a direct/test caller that threaded none) or an unrecognised underlying
    yields :data:`KIND_UNKNOWN` — honest, never a guess (Golden Rule #1).

    Parameters
    ----------
    underlying
        The contract's parsed underlying token (``NIFTY`` / ``BAJAJ-AUTO``).
    kinds
        The ``{underlying → kind}`` map from :func:`build_underlying_kind_map`, or
        ``None`` when no map was threaded in.
    aliases
        The sourced spot→F&O index alias map
        (:func:`~brutex.dashboard.instrument_loader.resolve_fno_index_aliases`).
        Resolved ONCE per compute call by the caller and threaded per lookup, so
        the per-candidate cost stays O(1) — never an env re-parse per row.

    Returns
    -------
    str
        One of :data:`KIND_INDEX` / :data:`KIND_STOCK` / :data:`KIND_UNKNOWN`.
    """
    if kinds is None:
        return KIND_UNKNOWN
    return kinds.get(primary_fno_underlying(underlying, aliases=aliases), KIND_UNKNOWN)


def candidate_passes_class(kind: str, wanted: str) -> bool:
    """Whether a candidate of resolved *kind* is included under the *wanted* class.

    The ONE predicate the spot / futures / options rank-time filters share
    (CLAUDE.md §11 COMMON): :data:`CLASS_ALL` includes everything;
    :data:`CLASS_INDICES` includes only :data:`KIND_INDEX`; :data:`CLASS_STOCKS`
    includes only :data:`KIND_STOCK`. A :data:`KIND_UNKNOWN` candidate passes ONLY
    ``all`` — excluded from both ``indices`` and ``stocks`` (Golden Rule #1).

    Parameters
    ----------
    kind
        The candidate's resolved kind (:data:`KIND_INDEX` / :data:`KIND_STOCK` /
        :data:`KIND_UNKNOWN`).
    wanted
        The normalised class token (from :func:`normalize_underlying_class`).

    Returns
    -------
    bool
        ``True`` iff the candidate is included under *wanted*.
    """
    if wanted == CLASS_ALL:
        return True
    if wanted == CLASS_INDICES:
        return kind == KIND_INDEX
    # wanted == CLASS_STOCKS (the only remaining canonical token).
    return kind == KIND_STOCK


#: Human labels for the two non-default classes, shown in the payload note.
_CLASS_LABEL: Final[dict[str, str]] = {CLASS_INDICES: "Indices", CLASS_STOCKS: "Stocks"}


def class_note_suffix(wanted: str, excluded: int) -> str:
    """Return the note fragment for the active class filter (EMIT-ON-non-default).

    ``""`` for :data:`CLASS_ALL` (so the default note is byte-identical, mirroring
    the ``DEC-MOVERS-MIN-VOLUME-001`` ``floor_note`` pattern); otherwise a leading-
    space fragment naming the active class and, when non-zero, the count of
    unclassified underlyings EXCLUDED from both Indices and Stocks — e.g.
    ``" Class: Indices (3 unclassified excluded)."``.

    Parameters
    ----------
    wanted
        The normalised class token (from :func:`normalize_underlying_class` —
        the ONLY sanctioned producer; a non-canonical token raises ``KeyError``
        loudly below, never a silent raw echo — review-round-1 tightening).
    excluded
        The count of :data:`KIND_UNKNOWN` candidates dropped by the filter.

    Returns
    -------
    str
        The note suffix (empty for ``all``).
    """
    if wanted == CLASS_ALL:
        return ""
    # Total by precondition: past the `all` early return, a normalised token is
    # exactly indices/stocks — both keys. Fail-loud on junk (Golden Rule #1).
    label = _CLASS_LABEL[wanted]
    excluded_note = f" ({excluded:,} unclassified excluded)" if excluded > 0 else ""
    return f" Class: {label}{excluded_note}."


def class_echo(wanted: str, excluded: int) -> dict[str, object]:
    """Return the ADDITIVE payload echo keys for the active class filter (EMIT-ON-non-default).

    ``{}`` for :data:`CLASS_ALL` (so the default payload is byte-identical — no new
    keys); otherwise ``{"underlying_class": <wanted>, "excluded_unclassified":
    <excluded>}`` so the page can confirm the active selector + the honest excluded
    count.

    Parameters
    ----------
    wanted
        The normalised class token (from :func:`normalize_underlying_class`).
    excluded
        The count of :data:`KIND_UNKNOWN` candidates dropped by the filter.

    Returns
    -------
    dict[str, object]
        The additive echo keys (empty for ``all``).
    """
    if wanted == CLASS_ALL:
        return {}
    return {"underlying_class": wanted, "excluded_unclassified": excluded}


class _HasUnderlyingKind(Protocol):
    """A candidate carrying a resolved index-vs-stock ``underlying_kind``.

    Declared as a read-only property so a FROZEN dataclass attribute (the
    ``MoverSnapshot`` / ``FuturesMover`` / ``OptionMover`` slotted fields) satisfies
    it — a mutable attribute member would reject a frozen slot.
    """

    @property
    def underlying_kind(self) -> str:
        """The candidate's resolved index-vs-stock kind."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed


def filter_by_underlying_class[C: _HasUnderlyingKind](
    items: Sequence[C], wanted: str
) -> tuple[list[C], int]:
    """Filter *items* to the *wanted* class, returning ``(kept, n_unclassified_excluded)``.

    The RANK-time filter (``DEC-MOVERS-UNDERLYING-CLASS-001``) applied in
    ``build_*_payload`` over the SAME cached candidate set — never a re-scan. When
    *wanted* is :data:`CLASS_ALL` the input is returned unchanged with a zero excluded
    count, so the default is byte-identical. Otherwise the items whose
    ``underlying_kind`` passes :func:`candidate_passes_class` are kept, and the number
    of :data:`KIND_UNKNOWN` items dropped (excluded from BOTH indices and stocks) is
    returned so the caller can surface it honestly in the note. The excluded count is
    taken over the FULL pre-top-N candidate set the payload builder passes in (the
    ``min_volume`` precedent — the filter runs at rank time BEFORE the top-N slice),
    so the note reflects the whole screened population, not just the visible page.
    Single pass, O(items).

    Parameters
    ----------
    items
        The computed candidates (each carries a ``underlying_kind: str``).
    wanted
        The normalised class token (from :func:`normalize_underlying_class`).

    Returns
    -------
    tuple[list, int]
        ``(kept_items, n_unclassified_excluded)``.
    """
    if wanted == CLASS_ALL:
        return list(items), 0
    kept: list[C] = []
    excluded = 0
    for item in items:
        if candidate_passes_class(item.underlying_kind, wanted):
            kept.append(item)
        elif item.underlying_kind == KIND_UNKNOWN:
            # Past the `all` early return, `wanted` is indices/stocks, under which
            # an unknown NEVER passes — the elif totals the unknown count exactly.
            excluded += 1
    return kept, excluded
