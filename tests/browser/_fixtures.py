"""Fixture-backed REAL ``build_app`` apps for the browser truth suite.

COMMON (CLAUDE.md §2 Rule 11): the lake/store seeding REUSES the existing
integration/unit fixture helpers BY IMPORT — the movers lake shape
(``tests/integration/test_dashboard_movers.py``: NIFTY +10% / BANKNIFTY −10%
on 2024-06-13), the universe explorer's instrument-row builders
(``tests/integration/test_universe_explorer_store.py``), and the sweep-tree
writers (``tests/unit/test_surface_trial_results.py``) — never copies.

Each builder returns an :class:`AppFixture`: the built FastAPI app + a
``ground`` dict of pinned display expectations (the independent truth the DOM
assertions compare against) + a ``cleanup`` callable the harness invokes at
teardown (store close).

Hostile strings: the production lake boundary REJECTS ``/ \\ : * ? " < > |``
in symbols (``brutex/data/partition.py`` ``_FORBIDDEN_SYMBOL_CHARS``), so a
markup-bearing name can never be a lake directory — :data:`XSS_NAME` is
therefore a lake-LEGAL two-arm payload (single quotes, ``&``, ``;`` and ``=``
are all legal): an attribute-breakout arm PLUS an ``&lt;…&gt;`` HTML-entity
arm whose double-escape divergence gives the movers ``esc()`` sink MUTATION
TEETH (an unescaped body sink decodes the entities and the cell's
``textContent`` diverges from the raw string — see :data:`XSS_NAME`). It
flows through the REAL lake + instrument master into the movers rows, while
:data:`XSS_NAME2` (real ``<script>`` markup) rides the store-only universe
display-name surface where no path validation intervenes.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from brutex.dashboard.app import DashboardState, build_app
from brutex.dashboard.prebuild_gate import PrebuildGate
from brutex.data.instrument_store import InstrumentStore
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.sink import ParquetBarSink

from tests.integration.test_dashboard_movers import (
    _DAY as _MOVERS_DAY_DATE,
)
from tests.integration.test_dashboard_movers import (
    _day_bar,
    _min_bar,
    _seed_spot_lake,
)
from tests.integration.test_dashboard_movers import (
    _idx as _movers_idx,
)
from tests.integration.test_dashboard_movers import (
    _write as _movers_write,
)
from tests.integration.test_universe_explorer_store import _idx as _universe_idx
from tests.unit.test_surface_trial_results import _SPECS, _nifty, _seed_sweep_tree

if TYPE_CHECKING:  # pragma: no cover — typing-only import
    from collections.abc import Callable

# ── ground-truth constants (single source for the page tests) ─────────────

#: The movers fixture snapshot day (mirrors test_dashboard_movers._DAY).
MOVERS_DAY: Final[str] = _MOVERS_DAY_DATE.isoformat()

#: Total 1-minute bars the /db paging fixture stores (3 pages at limit=10 → 2).
DB_BAR_COUNT: Final[int] = 15

#: The /db paging fixture's one symbol/segment/interval coordinate.
DB_SYMBOL: Final[str] = "NSE-NIFTY"
DB_SEGMENT: Final[str] = "INDEX"
DB_INTERVAL: Final[str] = "1minute"
#: The /db fixture's session day (all 15 bars 09:15–09:29 IST on this day).
DB_DAY: Final[str] = "2025-01-02"

#: Lake-LEGAL hostile instrument name (every char passes the production
#: ``_validate_symbol`` — no ``< > " | * ? : / \\``), carrying TWO arms:
#:
#: * an **HTML-entity smuggling arm** ``&lt;img …&gt;`` — the arm that gives
#:   the ``esc()`` escape MUTATION TEETH in the innerHTML body-context sink
#:   (movers.html renderRows): WITH ``esc()`` the ``&`` double-escapes to
#:   ``&amp;lt;`` and the cell's ``textContent`` equals this raw string;
#:   WITHOUT it the browser decodes ``&lt;``/``&gt;`` to ``<``/``>`` and the
#:   textContent diverges (a bare quote/handler payload is inert in body
#:   context either way — the round-1 vacuity finding this arm closes);
#: * a **single-quote attribute-breakout arm** with a REAL event handler — an
#:   unescaped single-quoted-attribute sink would attach the handler and
#:   stamp ``window.__bx_xss`` (the hover tripwire).
XSS_NAME: Final[str] = (
    "EVIL&lt;img src=x onerror=window.__bx_xss=1&gt;' onmouseover='window.__bx_xss=1"
)

#: Element-breakout hostile display name with REAL ``<script>`` markup —
#: store-field only (the lake boundary forbids ``< > " /`` in symbols), so it
#: rides the universe display-name surface: an innerHTML sink would execute it
#: and stamp ``window.__bx_xss``.
XSS_NAME2: Final[str] = '"/><script>window.__bx_xss=2</script>'

#: The plain symbol carrying the hostile display name in the universe fixture.
XSS_SYMBOL2: Final[str] = "EVILCO"


def _noop() -> None:
    """Default no-op cleanup."""


@dataclass
class AppFixture:
    """A built dashboard app + pinned ground truth + teardown hook."""

    #: The FastAPI app from the REAL ``build_app`` (served by the harness).
    app: Any
    #: Pinned display expectations the DOM assertions compare against.
    ground: dict[str, Any]
    #: Invoked once by the harness at session teardown (e.g. store close).
    cleanup: Callable[[], None] = field(default=_noop)


def _open_store(tmp_path: Path) -> InstrumentStore:
    """The real store at ``<lake>/instruments.duckdb`` (lake root = parent)."""
    return InstrumentStore.open(tmp_path / "instruments.duckdb")


_SNAPSHOT_DATE: Final[date] = date(2024, 1, 2)
_RUN_ID: Final[str] = "browser-fixture"
_CSV_SHA: Final[str] = "0" * 64


def build_movers_app(tmp_path: Path) -> AppFixture:
    """/movers: seeded gainer/loser lake + hostile symbol + an injectable gate.

    Reuses the pinned movers lake shape (NIFTY 22000 = +10% vs 20000 prev
    close; BANKNIFTY 40500 = −10% vs 45000; day 2024-06-13) and ADDS one
    hostile-named index (+5.00%) whose raw name must round-trip through the
    real server payload into a text-only cell. The returned
    ``ground["gate"]`` is the injected :class:`PrebuildGate` (fresh — NOT
    blocking) for the gate/paint-once/timer-disarm proofs.
    """
    _seed_spot_lake(tmp_path)
    _movers_write(
        tmp_path,
        XSS_NAME,
        "1minute",
        [
            _min_bar(hour=9, minute=15, close="100", volume=10),
            _min_bar(hour=9, minute=16, close="105", volume=15),
        ],
    )
    _movers_write(
        tmp_path,
        XSS_NAME,
        "1day",
        [
            _day_bar(day=date(2024, 6, 12), open_="100", close="100", volume=10),
            _day_bar(day=_MOVERS_DAY_DATE, open_="100", close="105", volume=25),
        ],
    )
    store = _open_store(tmp_path)
    store.upsert_many(
        [_movers_idx("NIFTY"), _movers_idx("BANKNIFTY"), _movers_idx(XSS_NAME)],
        snapshot_date=_SNAPSHOT_DATE,
        run_id=_RUN_ID,
        csv_source_sha256=_CSV_SHA,
    )
    gate = PrebuildGate()
    app = build_app(DashboardState(), instrument_store=store, prebuild_gate=gate)
    ground: dict[str, Any] = {
        "gate": gate,
        "day": MOVERS_DAY,
        "hostile_symbol": XSS_NAME,
        # NIFTY: close 22000 vs prev close 20000 → ₹-grouped price + signed pct.
        "nifty_price": "₹22,000.00",
        "nifty_pct": "+10.00%",
        "banknifty_pct": "-10.00%",
        "hostile_pct": "+5.00%",
        # Recovery render (non-gated, default daily view — movers.html :482).
        "asof_recovered": f"as-of {MOVERS_DAY} (daily) · 1day",
        "note_prefix": f"As-of {MOVERS_DAY} (daily) · 1day — reference-only lake view",
    }
    return AppFixture(app=app, ground=ground, cleanup=store.close)


def build_db_app(tmp_path: Path) -> AppFixture:
    """/db: 15 one-minute NSE-NIFTY bars (distinct closes) for the paging proof.

    09:15–09:29 IST on 2025-01-02, written through the production
    :class:`ParquetBarSink` (the spot-cascade fixture pattern). Distinct
    closes ⇒ page-1 vs page-2 first cells provably differ. With the top-bar
    Rows select at 10 the pager reads ``1–10 / 15`` then ``11–15 / 15``.
    """
    bars = [
        CandleBar(
            # 09:15 IST + i minutes = 03:45 UTC + i (fixed +05:30, no DST).
            timestamp=datetime(2025, 1, 2, 3, 45 + i, tzinfo=UTC),
            open=Decimal("100.00") + i,
            high=Decimal("100.50") + i,
            low=Decimal("99.50") + i,
            close=Decimal("100.25") + i,
            volume=10 + i,
            open_interest=None,
        )
        for i in range(DB_BAR_COUNT)
    ]
    ParquetBarSink(writer=ParquetWriter(lake_root=tmp_path)).append(
        exchange="NSE", segment=DB_SEGMENT, symbol=DB_SYMBOL, interval=DB_INTERVAL, bars=bars
    )
    store = _open_store(tmp_path)
    app = build_app(DashboardState(), instrument_store=store)
    ground: dict[str, Any] = {
        "bar_count": DB_BAR_COUNT,
        "symbol": DB_SYMBOL,
        "segment": DB_SEGMENT,
        "interval": DB_INTERVAL,
        "day": DB_DAY,
        # EN-DASH ranges (db.html gridRangeText).
        "page1_range": "1–10 / 15",
        "page2_range": "11–15 / 15",
    }
    return AppFixture(app=app, ground=ground, cleanup=store.close)


def build_universe_app(tmp_path: Path) -> AppFixture:
    """/universe: two seeded indices, one carrying a hostile display name."""
    store = _open_store(tmp_path)
    store.upsert_many(
        [_universe_idx("NIFTY", "NIFTY 50"), _universe_idx(XSS_SYMBOL2, XSS_NAME2)],
        snapshot_date=_SNAPSHOT_DATE,
        run_id=_RUN_ID,
        csv_source_sha256=_CSV_SHA,
    )
    app = build_app(DashboardState(), instrument_store=store)
    ground: dict[str, Any] = {
        "hostile_symbol": XSS_SYMBOL2,
        "hostile_name": XSS_NAME2,
    }
    return AppFixture(app=app, ground=ground, cleanup=store.close)


def build_results_app(tmp_path: Path) -> AppFixture:
    """/results: the surface-trial sweep tree (real ResultsStore writers).

    Reuses ``_seed_sweep_tree`` verbatim: two readable stores + one corrupt
    parquet (skipped, never a 500) + one cross-store duplicate run_id, so the
    leaderboard row count = the DISTINCT run_ids in ``_SPECS``.
    """
    store = _open_store(tmp_path)
    store.upsert_many(
        [_nifty()],
        snapshot_date=_SNAPSHOT_DATE,
        run_id=_RUN_ID,
        csv_source_sha256=_CSV_SHA,
    )
    _seed_sweep_tree(tmp_path)
    app = build_app(DashboardState(), instrument_store=store)
    ground: dict[str, Any] = {
        "row_count": len({spec["run_id"] for spec in _SPECS}),
    }
    return AppFixture(app=app, ground=ground, cleanup=store.close)
