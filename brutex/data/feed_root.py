"""Feed-tagged lake-root resolution — the feed-root nesting law (leaf).

DEC-FEED-LAKE-ROOT-001 (MIGRATION.md #1755). Track 1 stores every historical
feed's bars under the SAME canonical lake layout
(``bars/<exchange>/<segment>/<symbol>/<interval>/<yyyy>/<mm>.parquet``,
:mod:`brutex.data.partition`), but a NON-default feed's entire tree nests under
a per-feed subtree so two feeds can never share a partition FILE (the keep-first
``timestamp``-keyed merge would silently collapse a cross-feed same-minute bar,
DEC-LAKE-WRITE-UNIQUE-001). Attribution is **path-structural** — there is NO
``feed`` column on :class:`~brutex.data.models.CandleBar` or the Parquet schema,
so an old single-feed lake is correctly attributed with zero migration.

Law
---
* The **default feed** (:data:`DEFAULT_FEED`) resolves to the TOP lake root by
  **object identity** — ``feed_lake_root(root, feed=DEFAULT_FEED) is root``. The
  existing write stack therefore runs byte-for-byte on the top root and executes
  zero new code for the default feed (the Groww byte-identity guarantee).
* A **non-default feed** ``<f>`` nests under ``<lake_root>/feeds/<f>/`` — its
  own ``bars/`` / ``options/`` / ``catalog.duckdb`` / ``.manifest/`` / ``.locks/``
  tree, no DDL migration, no shared-file contention.

This module is a **stdlib-only leaf** (plus ``structlog`` for the no-``print``
rule): no I/O except :func:`list_feed_roots` (a single ``scandir`` of the
``feeds/`` dir), no heavy imports, no clock. Feed ids are grammar-validated data
so PR-2 carries no retired-vendor token (the vendor-token CI guard stays green);
the registry ships with only the default feed and tests use a synthetic id.

See TRACK1_BACKTEST_SPEC.md §"Data lake / partitioning" and ADR-0021.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import structlog

from brutex.models import BruteXError

_LOG = structlog.get_logger("brutex.data.feed_root")


class FeedRootError(BruteXError):
    """Raised on an invalid feed id or a malformed feed-root request.

    Its own typed error (never a bare :class:`ValueError`) so a caller can
    distinguish a feed-tagging refusal from an unrelated failure.
    """


#: The default historical feed. It resolves to the TOP lake root by object
#: identity (see :func:`feed_lake_root`) so the existing single-feed write path
#: is byte-identical. The registry ships with ONLY this feed (PR-2 is
#: vendor-token-free); a second feed id is registered in a later PR.
DEFAULT_FEED: Final[str] = "groww"

#: The subdirectory under the lake root that holds every non-default feed's
#: nested tree: ``<lake_root>/feeds/<feed_id>/bars/...``.
FEEDS_DIR_NAME: Final[str] = "feeds"

#: Feed-id grammar: a lowercase-first token, 2–16 chars, ``[a-z][a-z0-9_]{1,15}``.
#: Lowercase-only + no leading digit / dot keeps a feed id filesystem-safe on
#: every OS and structurally excludes a dot-prefixed hidden dir (``.manifest``,
#: ``.locks``, ``.audit``, ``.quarantine``).
_FEED_ID_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9_]{1,15}$")

#: Grammar-valid names that would COLLIDE with a top-root artifact directory if
#: used as a feed id — a feed nested at ``feeds/bars/`` (etc.) would masquerade
#: as, or shadow, a real lake dir. Rejected in addition to the grammar. The
#: ``feeds`` self-reference is included so a ``feeds/feeds/`` tree is impossible.
#: (Dot-prefixed artifact dirs — ``.manifest`` / ``.locks`` / ``.audit`` /
#: ``.quarantine`` — are already excluded by the grammar's lowercase-letter
#: first char, so they need no explicit entry.)
RESERVED_FEED_IDS: Final[frozenset[str]] = frozenset(
    {"bars", "options", FEEDS_DIR_NAME, "sweeps", "sweep_runs"}
)

#: Minimum number of leading path components a feed partition must have for
#: :func:`feed_of_partition_path` to attribute it to a non-default feed:
#: ``feeds`` / ``<feed_id>`` / (at least one more — the ``bars``/``options`` tree).
_MIN_FEED_PATH_COMPONENTS: Final[int] = 3


def is_valid_feed_id(feed_id: str) -> bool:
    """Return whether *feed_id* satisfies the grammar and is not reserved.

    Pure, allocation-light, zero I/O — the boolean twin of
    :func:`validate_feed_id` used by :func:`feed_of_partition_path` to decide
    whether a ``feeds/<x>/`` path component names a real feed subtree.
    """
    return bool(_FEED_ID_RE.match(feed_id)) and feed_id not in RESERVED_FEED_IDS


def validate_feed_id(feed_id: str) -> None:
    """Raise :class:`FeedRootError` if *feed_id* is not a legal feed id.

    A legal feed id matches :data:`_FEED_ID_RE` and is not in
    :data:`RESERVED_FEED_IDS`. Called by :func:`feed_lake_root` and the
    :class:`~brutex.feeds.binding.FeedBinding` invariant so a malformed id can
    never reach the filesystem.
    """
    if not _FEED_ID_RE.match(feed_id):
        raise FeedRootError(
            f"feed_id {feed_id!r} must match {_FEED_ID_RE.pattern!r} "
            f"(lowercase, 2-16 chars, leading letter)"
        )
    if feed_id in RESERVED_FEED_IDS:
        raise FeedRootError(
            f"feed_id {feed_id!r} is reserved — it would collide with a top-root "
            f"lake artifact ({sorted(RESERVED_FEED_IDS)})"
        )


def feed_lake_root(lake_root: Path, *, feed: str) -> Path:
    """Resolve the effective lake root for *feed*.

    The default feed (:data:`DEFAULT_FEED`) returns *lake_root* itself — the
    SAME object (``feed_lake_root(root, feed=DEFAULT_FEED) is root``) — so the
    existing write stack runs byte-for-byte on the top root. A non-default feed
    returns ``lake_root / feeds / <feed>``.

    Raises
    ------
    FeedRootError
        If *feed* is not a legal feed id (:func:`validate_feed_id`).
    """
    validate_feed_id(feed)
    if feed == DEFAULT_FEED:
        return lake_root
    return lake_root / FEEDS_DIR_NAME / feed


def feed_of_partition_path(path: Path | str, *, lake_root: Path) -> str:
    """Attribute a partition *path* to its feed — path-structural, zero fs ops.

    Pure + lexical (no ``resolve()``, no ``stat``): a path lexically under
    ``<lake_root>/feeds/<f>/`` whose ``<f>`` is a legal feed id is attributed to
    feed ``<f>``; every other path (including an old single-feed lake, or a
    ``feeds/<junk>/`` dir whose second component is not a legal feed id) is
    attributed to :data:`DEFAULT_FEED`. A path not lexically under *lake_root*
    is parsed from its own tail.

    O(1) in the number of path components; touches the filesystem zero times.
    """
    try:
        rel = Path(path).relative_to(lake_root)
    except ValueError:
        rel = Path(path)
    parts = rel.parts
    if (
        len(parts) >= _MIN_FEED_PATH_COMPONENTS
        and parts[0] == FEEDS_DIR_NAME
        and is_valid_feed_id(parts[1])
    ):
        return parts[1]
    return DEFAULT_FEED


def list_feed_roots(lake_root: Path) -> list[tuple[str, Path]]:
    """Enumerate the non-default feed subtrees under ``<lake_root>/feeds/``.

    Returns ``(feed_id, feed_root)`` pairs sorted by ``feed_id`` for every direct
    child of ``feeds/`` that is a directory with a legal feed id. A missing
    ``feeds/`` dir yields ``[]``. Junk — a stray file, or a directory whose name
    is not a legal feed id — is SKIPPED with one ``feed_root.skip_junk`` WARN
    (never raised), so a later heal / census / backup enumeration over the feed
    trees is fail-soft. This is the PR-3 observability enumeration seam
    (DEC-FEED-TREE-OBSERVABILITY-001), defined + tested now.
    """
    feeds_dir = lake_root / FEEDS_DIR_NAME
    if not feeds_dir.is_dir():
        return []
    roots: list[tuple[str, Path]] = []
    for child in sorted(feeds_dir.iterdir(), key=lambda p: p.name):
        if not child.is_dir():
            _LOG.warning("feed_root.skip_junk", path=repr(str(child)), reason="not_a_directory")
            continue
        if not is_valid_feed_id(child.name):
            _LOG.warning("feed_root.skip_junk", path=repr(str(child)), reason="invalid_feed_id")
            continue
        roots.append((child.name, child))
    return roots


__all__ = [
    "DEFAULT_FEED",
    "FEEDS_DIR_NAME",
    "RESERVED_FEED_IDS",
    "FeedRootError",
    "feed_lake_root",
    "feed_of_partition_path",
    "is_valid_feed_id",
    "list_feed_roots",
    "validate_feed_id",
]
