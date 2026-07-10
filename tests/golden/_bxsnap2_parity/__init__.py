"""Observational parity oracle for a FUTURE Rust ``arrow_table_to_bars`` port.

This helper package is the **observational parity oracle** referenced by
``DEC-BXSNAP2-PARITY-GOLDEN-001`` (MIGRATION.md #1695). It changes **NO
production behaviour** — it only *reads* the current Python bar-materialization
path and freezes its output.

Why it exists
-------------
The brute-force sweep's run identity folds a ``data_snapshot_hash`` (preimage
version ``bxsnap2``, :func:`brutex.engine.stage3_wire.data_snapshot_hash`) into
the ``blake3(… ‖ data_snapshot_hash ‖ …)`` run id (CLAUDE.md Golden Rule #4).
That hash streams each materialized :class:`~brutex.data.models.CandleBar`'s
seven fields — and the price fields are rendered as
``Decimal(str(float)).__str__()``. CPython's ``str(float)`` is
shortest-round-trip: an integer price renders ``"20000.0"`` (NOT ``"20000.00"``),
a half value ``"20000.5"`` (NOT ``"20000.50"``); ``Decimal`` then re-renders a
large magnitude with an *uppercase* exponent (``1e+16`` → ``"1E+16"``) and
*expands* a small one (``1e-05`` → ``"0.00001"``). A future Rust/Arrow port of
:func:`brutex.feed.replay.arrow_table_to_bars` that formats ``f64`` the naive
way would silently diverge the run identity and break the twin-run proof.

This package FREEZES the current Python path (through the real
``arrow_table_to_bars`` → ``CandleBar`` list → ``data_snapshot_hash``) as the
oracle. The golden test (``tests/golden/test_bxsnap2_parity_golden.py``) asserts
the current path reproduces the frozen digests EXACTLY, so any future divergence
— a Rust port or an accidental Python edit — fails loudly and mechanically.

Regeneration
------------
The frozen fixture (``tests/golden/fixtures/bxsnap2_parity_golden.json``) is a
byte contract. Regenerate it ONLY when the Python path *intentionally* changes,
and only with a new decision-ID + a MIGRATION.md note that every dedup/cache
run identity goes cold (the v1→v2 ``bxsnap2`` precedent)::

    uv run python tests/golden/generate_bxsnap2_parity_golden.py

Coverage note
-------------
Every function + branch here is exercised by the golden test — this module is a
pure read-only oracle over the production path, so it must itself be
100% line + branch (CLAUDE.md §12).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import Final

import pyarrow as pa
from brutex.data.models import CandleBar
from brutex.engine.stage3_wire import data_snapshot_hash
from brutex.feed.replay import arrow_table_to_bars

# ── Fixed data-scope labels ─────────────────────────────────────────────────
# The four scope labels folded verbatim into the ``bxsnap2|…`` domain tag, plus
# the single (symbol, interval) slice key. Held constant across every case so a
# digest change can only ever come from the bar CONTENT (the thing under test),
# never from a scope-label edit.
PARITY_EXCHANGE: Final[str] = "NSE"
PARITY_SEGMENT: Final[str] = "INDEX"
PARITY_START: Final[str] = "2024-01-01"
PARITY_END: Final[str] = "2024-12-31"
PARITY_SYMBOL: Final[str] = "NSE-NIFTY"
PARITY_INTERVAL: Final[str] = "1minute"

#: IST fixed-offset (no DST) — used ONLY to build Muhurat evening-session
#: timestamps in their natural IST wall-clock form (the arrow schema is UTC).
_IST: Final = timezone(timedelta(hours=5, minutes=30))

#: A clean regular-session anchor: 2024-01-02 09:15 IST == 03:45 UTC.
_SESSION_1: Final = datetime(2024, 1, 2, 3, 45, tzinfo=UTC)

#: One row of the canonical 7-column bar table:
#: ``(timestamp, open, high, low, close, volume, open_interest)``.
Row = tuple[datetime, float, float, float, float, int, int | None]


@dataclass(frozen=True, slots=True)
class ParityCase:
    """One frozen parity case: an input bar table + its expected kept-set size.

    Attributes
    ----------
    name
        Stable identifier — the golden-JSON key.
    description
        Human-readable statement of the edge case the digest freezes.
    rows
        The input bar rows fed to :func:`build_bar_table`.
    expected_kept
        The number of bars the CURRENT path materializes (i.e. survives the
        positivity + bad-tick screens) — an independent tripwire on the screen
        wiring, orthogonal to the digest.
    source
        The ``source`` label passed to ``arrow_table_to_bars`` (drives the
        bad-tick WARN origin; ``None`` keeps the bare per-bar message).
    """

    name: str
    description: str
    rows: tuple[Row, ...]
    expected_kept: int
    source: str | None = None


def build_bar_table(rows: Sequence[Row]) -> pa.Table:
    """Build the canonical 7-column bar table ``arrow_table_to_bars`` consumes.

    Mirrors the schema :meth:`brutex.data.catalog.Catalog.get_bars` returns
    (``timestamp[us, tz=UTC]`` + four ``float64`` prices + two ``int64``), so
    the fixture is byte-shape-identical to what the production reader feeds the
    materializer.
    """
    return pa.table(
        {
            "timestamp": pa.array([r[0] for r in rows], type=pa.timestamp("us", tz="UTC")),
            "open": pa.array([r[1] for r in rows], type=pa.float64()),
            "high": pa.array([r[2] for r in rows], type=pa.float64()),
            "low": pa.array([r[3] for r in rows], type=pa.float64()),
            "close": pa.array([r[4] for r in rows], type=pa.float64()),
            "volume": pa.array([r[5] for r in rows], type=pa.int64()),
            "open_interest": pa.array([r[6] for r in rows], type=pa.int64()),
        }
    )


def materialize(case: ParityCase) -> list[CandleBar]:
    """Run the CURRENT production Python path for *case* — ``arrow_table_to_bars``.

    This is the exact seam a future Rust port replaces: Arrow table →
    ``list[CandleBar]`` (with the positivity + bad-tick screens + the
    ``Decimal(str(price))`` re-wrap).
    """
    return arrow_table_to_bars(build_bar_table(case.rows), source=case.source)


def digest_for_bars(bars: Sequence[CandleBar]) -> str:
    """Fold a materialized bar list through the production ``bxsnap2`` hash.

    Uses the fixed parity scope labels so the digest is a pure function of the
    bar content — the run-identity contract a Rust port must reproduce.
    """
    return data_snapshot_hash(
        exchange=PARITY_EXCHANGE,
        segment=PARITY_SEGMENT,
        start=PARITY_START,
        end=PARITY_END,
        slices={(PARITY_SYMBOL, PARITY_INTERVAL): list(bars)},
    )


def run_case(case: ParityCase) -> tuple[list[CandleBar], str]:
    """Materialize + hash — the whole Python oracle path for one case.

    Returns
    -------
    tuple[list[CandleBar], str]
        The kept bars and their ``bxsnap2`` digest.
    """
    bars = materialize(case)
    return bars, digest_for_bars(bars)


def _integer_price_case() -> ParityCase:
    """All-integer OHLC + a mixed (None → int) OI run.

    Pins the shortest-round-trip contract at its most load-bearing point: an
    integer f64 price renders ``"20000.0"`` (NOT ``"20000.00"``) inside the
    hash, and a ``None`` open_interest renders the literal ``"None"``.
    """
    return ParityCase(
        name="integer_prices",
        description=(
            "All-integer OHLC prices — the Decimal(str(float)) render is "
            "'20000.0' not '20000.00'; first bar OI is None (renders 'None')."
        ),
        rows=(
            (_SESSION_1, 20000.0, 20050.0, 19950.0, 20000.0, 1000, None),
            (_SESSION_1 + timedelta(minutes=1), 20000.0, 20100.0, 19990.0, 20050.0, 1200, 100),
            (_SESSION_1 + timedelta(minutes=2), 20050.0, 20080.0, 20010.0, 20030.0, 900, 200),
        ),
        expected_kept=3,
        source="NSE/INDEX/NSE-NIFTY/1minute",
    )


def _half_paisa_grid_case() -> ParityCase:
    """2dp tick-grid values — half-paisa and hundredth-rupee closes.

    Pins that a ``.5`` value renders ``"100.5"`` (not ``"100.50"``) and a
    genuine 2dp value renders exactly its two decimals (``"99.95"``).
    """
    return ParityCase(
        name="half_paisa_grid",
        description=(
            "2dp tick-grid closes: 100.5 renders '100.5' (not '100.50'); "
            "99.95 / 100.05 render their exact two decimals."
        ),
        rows=(
            (_SESSION_1, 100.00, 100.05, 99.95, 100.05, 500, None),
            (_SESSION_1 + timedelta(minutes=1), 100.05, 100.55, 99.90, 100.50, 550, None),
            (_SESSION_1 + timedelta(minutes=2), 100.10, 100.15, 100.00, 100.00, 600, None),
        ),
        expected_kept=3,
        source=None,
    )


def _bad_tick_kept_set_case() -> ParityCase:
    """The #1616 bad-tick screen KEPT-SET, spanning two sessions.

    Session 1 carries a ``÷10`` scaled spike (SKIPPED, never in the digest) and
    a healthy successor (KEPT — judged against the last good close, not the
    spike). Session 2's first bar is a large reopen gap (KEPT — first-bar
    exempt). Freezes both the spike-skip and the first-bar-exempt behaviour in
    one digest.
    """
    base2 = _SESSION_1 + timedelta(days=1)
    return ParityCase(
        name="bad_tick_kept_set",
        description=(
            "Bad-tick screen: a divide-by-10 scaled spike is SKIPPED (not in "
            "the digest); its healthy successor is KEPT; a new session's first "
            "bar (large reopen gap) is KEPT (first-bar exempt). Kept 3, skip 1."
        ),
        rows=(
            (_SESSION_1, 27940.0, 27950.0, 27930.0, 27945.0, 10, None),
            (_SESSION_1 + timedelta(minutes=1), 2794.5, 2795.0, 2793.0, 2794.5, 10, None),
            (_SESSION_1 + timedelta(minutes=2), 27946.0, 27960.0, 27940.0, 27955.0, 10, None),
            (base2, 6000.0, 6010.0, 5990.0, 6005.0, 10, None),
        ),
        expected_kept=3,
        source="NSE/INDEX/NSE-NIFTY/1minute",
    )


def _muhurat_kept_set_case() -> ParityCase:
    """A Diwali Muhurat evening-session KEPT-SET (2024-11-01 18:00–19:00 IST).

    The engine READ boundary applies NO trading-calendar filter — every
    Muhurat evening bar is materialized (the Muhurat-anchor guard lives later,
    at the Stage-1A roll seam). Freezes the digest of those kept bars so a Rust
    port keeps them too.
    """
    muhurat_open = datetime(2024, 11, 1, 18, 0, tzinfo=_IST).astimezone(UTC)
    return ParityCase(
        name="muhurat_kept_set",
        description=(
            "Diwali Muhurat evening session (2024-11-01 18:00-19:00 IST): the "
            "read boundary keeps every bar (no calendar filter). Kept 3."
        ),
        rows=(
            (muhurat_open, 104.00, 104.50, 103.80, 104.20, 300, None),
            (muhurat_open + timedelta(minutes=1), 104.20, 104.60, 104.10, 104.50, 320, None),
            (muhurat_open + timedelta(minutes=2), 104.50, 104.70, 104.30, 104.40, 310, None),
        ),
        expected_kept=3,
        source="NSE/INDEX/NSE-NIFTY/1minute",
    )


#: Every digest-producing parity case, in stable order (the golden-JSON order).
#: A non-positive price is a REFUSAL not a kept-set (the current path raises), so
#: it is pinned directly in the golden test, not carried here.
DIGEST_CASES: Final[tuple[ParityCase, ...]] = (
    _integer_price_case(),
    _half_paisa_grid_case(),
    _bad_tick_kept_set_case(),
    _muhurat_kept_set_case(),
)


#: A positivity-refusal case: an internally-ordered but NON-positive bar. The
#: current read boundary RAISES (never materializes it into the digest), so this
#: is a refusal contract, pinned by ``pytest.raises`` in the golden test — the
#: positivity guard runs BEFORE construction, so it is what fires.
POSITIVITY_REFUSAL_ROWS: Final[tuple[Row, ...]] = (
    (_SESSION_1, 100.0, 101.0, 99.0, 100.0, 10, None),
    (_SESSION_1 + timedelta(minutes=1), -100.0, -99.0, -101.0, -100.0, 10, None),
)


__all__ = [
    "DIGEST_CASES",
    "PARITY_END",
    "PARITY_EXCHANGE",
    "PARITY_INTERVAL",
    "PARITY_SEGMENT",
    "PARITY_START",
    "PARITY_SYMBOL",
    "POSITIVITY_REFUSAL_ROWS",
    "ParityCase",
    "Row",
    "build_bar_table",
    "digest_for_bars",
    "materialize",
    "run_case",
]
