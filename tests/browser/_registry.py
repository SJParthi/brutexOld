"""Page registry for the browser truth suite (COMMON · DYNAMIC).

One :class:`PageSpec` per dashboard page under browser truth: the route path,
the fixture builder that seeds a REAL ``build_app`` app for it, and the
DOM-ready condition pinned from the actual template. Adding a page to the
suite = adding ONE registry entry — ``test_harness_selfcheck.py`` iterates
:data:`PAGE_REGISTRY`, so every registered page automatically gets the
console-clean render proof.

Playwright-free and import-light (the heavy fixture module is resolved lazily
by name) so the module stays importable wherever ``_guards.py`` is.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:  # pragma: no cover — typing-only import (keeps runtime light)
    from tests.browser._fixtures import AppFixture


@dataclass(frozen=True)
class PageSpec:
    """One dashboard page under browser truth.

    ``fixture_builder`` is a NAME reference into ``tests.browser._fixtures``
    (resolved lazily by :meth:`build`) so importing the registry never drags
    in fastapi/duckdb/polars.
    """

    #: Short id (parametrize id + artifact naming).
    name: str
    #: The page route, e.g. ``"/movers"``.
    path: str
    #: Builder function name in ``tests.browser._fixtures``.
    fixture_builder: str
    #: CSS selector whose presence proves the page's data render completed
    #: (pinned from the template — see each entry's comment).
    ready_selector: str
    #: Optional selector that must ALSO carry non-empty text at readiness.
    nonempty_selector: str | None = None

    def build(self, tmp_path: Path) -> AppFixture:
        """Resolve + run this page's fixture builder against *tmp_path*."""
        from tests.browser import _fixtures

        builder = getattr(_fixtures, self.fixture_builder)
        fixture: AppFixture = builder(tmp_path)
        return fixture


PAGE_REGISTRY: Final[tuple[PageSpec, ...]] = (
    # /db sidebar rows render after GET /api/v1/db/tables resolves
    # (db.html renderTables — `.tbl` rows incl. the static candle/ledger rows).
    PageSpec(name="db", path="/db", fixture_builder="build_db_app", ready_selector="#tables .tbl"),
    # /movers table rows render after GET /api/v1/movers resolves; #asof carries
    # the as-of line (movers.html #rows tbody / #asof).
    PageSpec(
        name="movers",
        path="/movers",
        fixture_builder="build_movers_app",
        ready_selector="#rows tr",
        nonempty_selector="#asof",
    ),
    # /universe instrument rows render after GET /api/v1/universe/instruments
    # (universe.html renderInstruments — `.sym` per row; needs a seeded store).
    PageSpec(
        name="universe",
        path="/universe",
        fixture_builder="build_universe_app",
        ready_selector="#instruments .sym",
    ),
    # /results leaderboard table renders after GET /api/v1/sweep/leaderboard
    # (results.html #panel table — needs a seeded sweep tree).
    PageSpec(
        name="results",
        path="/results",
        fixture_builder="build_results_app",
        ready_selector="#panel table tbody tr",
    ),
)
