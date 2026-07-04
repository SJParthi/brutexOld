"""Unit tests for ``brutex.adapters.groww.expiry_scope`` (DEC-FNO-EXPIRY-SCOPE-001).

The pure expiry-level scoping helper: an expiry is IN SCOPE iff it lies within
``[scope_start, scope_end]`` (BOTH endpoints inclusive; ``None`` = unbounded).
Covers the boundary permutations the decree names (expiry == to_date IN,
to_date + 1 day OUT, single-expiry selection, year-boundary ranges, the
52-weekly index calendar), the ``(underlying, expiry)`` grouping, and the
one-log-line-per-expiry ``fno.expiry_out_of_scope`` emitter. 100% line+branch
on the module.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from brutex.adapters.groww.expiry_scope import (
    ScopePartition,
    expiry_in_scope,
    log_out_of_scope_expiries,
    normalize_expiry_scope,
    partition_contracts_by_scope,
)
from brutex.data.registry_store import ContractRow
from structlog.testing import capture_logs

_FETCHED = datetime.fromisoformat("2026-01-01T00:00:00+00:00")


def _row(underlying: str, expiry: date, *, side: str = "CE", n: int = 0) -> ContractRow:
    """Build one registry :class:`ContractRow` for *underlying* at *expiry*."""
    token = expiry.strftime("%d%b%y")
    return ContractRow(
        exch="NSE",
        underlying=underlying,
        expiry_date=expiry,
        contract_symbol=f"NSE-{underlying}-{token}-{21000 + n}-{side}",
        strike=float(21000 + n),
        side=side,
        candle_status="none",
        last_run_id="scope-test",
        fetched_at=_FETCHED,
    )


# ── expiry_in_scope — the inclusive-boundary law ─────────────────────────────


def test_expiry_equal_to_scope_end_is_in_scope() -> None:
    """Permutation (a): ``expiry == to_date`` is IN (both endpoints inclusive)."""
    assert expiry_in_scope(
        date(2026, 6, 25), scope_start=date(2026, 6, 1), scope_end=date(2026, 6, 25)
    )


def test_expiry_one_day_past_scope_end_is_out() -> None:
    """Permutation (b, boundary): ``expiry == to_date + 1 day`` is OUT."""
    assert not expiry_in_scope(
        date(2026, 6, 26), scope_start=date(2026, 6, 1), scope_end=date(2026, 6, 25)
    )


def test_expiry_equal_to_scope_start_is_in_scope() -> None:
    assert expiry_in_scope(
        date(2026, 6, 1), scope_start=date(2026, 6, 1), scope_end=date(2026, 6, 25)
    )


def test_expiry_one_day_before_scope_start_is_out() -> None:
    assert not expiry_in_scope(
        date(2026, 5, 31), scope_start=date(2026, 6, 1), scope_end=date(2026, 6, 25)
    )


def test_none_scope_start_is_unbounded_below() -> None:
    assert expiry_in_scope(date(2020, 1, 1), scope_start=None, scope_end=date(2026, 6, 25))


def test_none_scope_end_is_unbounded_above() -> None:
    assert expiry_in_scope(date(2099, 1, 1), scope_start=date(2026, 6, 1), scope_end=None)


def test_both_none_everything_in_scope() -> None:
    """The default (no dates) scope puts EVERYTHING in scope — the unscoped pull."""
    assert expiry_in_scope(date(1990, 1, 1), scope_start=None, scope_end=None)
    assert expiry_in_scope(date(2099, 12, 31), scope_start=None, scope_end=None)


def test_single_day_scope_selects_exactly_that_expiry() -> None:
    """Permutation (f): ``from == to == expiry`` selects exactly that expiry."""
    expiry = date(2026, 6, 25)
    assert expiry_in_scope(expiry, scope_start=expiry, scope_end=expiry)
    assert not expiry_in_scope(expiry - timedelta(days=1), scope_start=expiry, scope_end=expiry)
    assert not expiry_in_scope(expiry + timedelta(days=1), scope_start=expiry, scope_end=expiry)


# ── partition_contracts_by_scope — grouping + the calendar permutations ──────


def test_partition_default_scope_keeps_everything_in_order() -> None:
    """Regression pin (permutation h, unit level): (None, None) is byte-identical."""
    rows = [
        _row("NIFTY", date(2026, 6, 25)),
        _row("SENSEX", date(2026, 6, 26)),
        _row("NIFTY", date(2026, 7, 30), side="PE"),
    ]
    part = partition_contracts_by_scope(rows, scope_start=None, scope_end=None)
    assert part.in_scope == tuple(rows)  # every row, input order preserved
    assert part.out_by_expiry == {}
    assert part.contracts_out_of_scope == 0
    assert part.expiries_out_of_scope == 0


def test_partition_groups_out_of_scope_by_underlying_and_expiry() -> None:
    """Out rows group per (underlying, expiry) with per-leg counts — one log per group."""
    far = date(2026, 9, 29)
    rows = [
        _row("NIFTY", date(2026, 6, 25)),  # in
        _row("NIFTY", far, n=1),  # out — group 1, leg 1
        _row("NIFTY", far, side="PE", n=2),  # out — group 1, leg 2
        _row("SENSEX", far, n=3),  # out — group 2 (same expiry, other underlying)
    ]
    part = partition_contracts_by_scope(rows, scope_start=None, scope_end=date(2026, 6, 30))
    assert [r.underlying for r in part.in_scope] == ["NIFTY"]
    assert part.out_by_expiry == {("NIFTY", far): 2, ("SENSEX", far): 1}
    assert part.contracts_out_of_scope == 3
    assert part.expiries_out_of_scope == 2


def test_partition_weekly_and_monthly_mixed_subset_selected() -> None:
    """Permutation (c): weekly + monthly expiries in one month, scope a subset.

    February carries four weeklies (01/08/15/22-Feb) plus the monthly (29-Feb);
    a ``[2024-02-05, 2024-02-16]`` scope selects EXACTLY the 08-Feb + 15-Feb
    weeklies — the monthly and the out-of-range weeklies are grouped out.
    """
    expiries = [
        date(2024, 2, 1),
        date(2024, 2, 8),
        date(2024, 2, 15),
        date(2024, 2, 22),
        date(2024, 2, 29),  # the monthly
    ]
    rows = [_row("NIFTY", e, n=i) for i, e in enumerate(expiries)]
    part = partition_contracts_by_scope(
        rows, scope_start=date(2024, 2, 5), scope_end=date(2024, 2, 16)
    )
    assert [r.expiry_date for r in part.in_scope] == [date(2024, 2, 8), date(2024, 2, 15)]
    assert set(part.out_by_expiry) == {
        ("NIFTY", date(2024, 2, 1)),
        ("NIFTY", date(2024, 2, 22)),
        ("NIFTY", date(2024, 2, 29)),
    }


def test_partition_index_weeklies_two_week_scope_count_pinned() -> None:
    """Permutation (d): NIFTY + SENSEX 52 weeklies/yr, a 2-week scope selects 2 each.

    A full 52-Thursday 2025 weekly calendar per index (104 contract rows); the
    ``[2025-06-05, 2025-06-18]`` scope covers EXACTLY the 05-Jun + 12-Jun
    weeklies → 2 expiries × 2 indexes = 4 rows in scope, 100 rows out across
    100 (underlying, expiry) groups (one leg per weekly here).
    """
    first_thursday = date(2025, 1, 2)
    weeklies = [first_thursday + timedelta(weeks=w) for w in range(52)]
    assert len(weeklies) == 52 and weeklies[-1].year == 2025
    rows = [
        _row(underlying, expiry, n=i)
        for underlying in ("NIFTY", "SENSEX")
        for i, expiry in enumerate(weeklies)
    ]
    part = partition_contracts_by_scope(
        rows, scope_start=date(2025, 6, 5), scope_end=date(2025, 6, 18)
    )
    assert len(rows) == 104
    assert {r.expiry_date for r in part.in_scope} == {date(2025, 6, 5), date(2025, 6, 12)}
    assert len(part.in_scope) == 4  # 2 weeklies × 2 indexes
    assert part.expiries_out_of_scope == 100  # 50 out-of-range weeklies × 2 indexes
    assert part.contracts_out_of_scope == 100


def test_partition_year_boundary_range() -> None:
    """Permutation (e): a 2025-12-15 → 2026-01-15 scope keeps Dec + Jan, drops Nov + Feb."""
    rows = [
        _row("NIFTY", date(2025, 11, 27), n=1),  # out (before)
        _row("NIFTY", date(2025, 12, 30), n=2),  # in (December side)
        _row("NIFTY", date(2026, 1, 8), n=3),  # in (January side)
        _row("NIFTY", date(2026, 2, 26), n=4),  # out (after)
    ]
    part = partition_contracts_by_scope(
        rows, scope_start=date(2025, 12, 15), scope_end=date(2026, 1, 15)
    )
    assert [r.expiry_date for r in part.in_scope] == [date(2025, 12, 30), date(2026, 1, 8)]
    assert set(part.out_by_expiry) == {
        ("NIFTY", date(2025, 11, 27)),
        ("NIFTY", date(2026, 2, 26)),
    }


# ── log_out_of_scope_expiries — one honest line per skipped expiry ───────────


def test_log_emits_one_line_per_expiry_group_with_fields() -> None:
    far = date(2026, 9, 29)
    rows = [
        _row("NIFTY", far, n=1),
        _row("NIFTY", far, side="PE", n=2),
        _row("SENSEX", far, n=3),
    ]
    part = partition_contracts_by_scope(rows, scope_start=None, scope_end=date(2026, 6, 30))
    with capture_logs() as logs:
        log_out_of_scope_expiries(part, scope_start=None, scope_end=date(2026, 6, 30))
    events = [e for e in logs if e["event"] == "fno.expiry_out_of_scope"]
    assert len(events) == 2  # ONE line per (underlying, expiry), never per leg
    nifty, sensex = events  # sorted: NIFTY before SENSEX
    assert nifty["underlying"] == "NIFTY"
    assert nifty["expiry"] == "2026-09-29"
    assert nifty["scope_start"] is None
    assert nifty["scope_end"] == "2026-06-30"
    assert nifty["contracts_skipped"] == 2
    assert sensex["underlying"] == "SENSEX"
    assert sensex["contracts_skipped"] == 1


def test_log_bounded_scope_start_rendered_iso() -> None:
    rows = [_row("NIFTY", date(2020, 3, 26))]
    part = partition_contracts_by_scope(
        rows, scope_start=date(2021, 1, 1), scope_end=date(2021, 12, 31)
    )
    with capture_logs() as logs:
        log_out_of_scope_expiries(part, scope_start=date(2021, 1, 1), scope_end=date(2021, 12, 31))
    (event,) = [e for e in logs if e["event"] == "fno.expiry_out_of_scope"]
    assert event["scope_start"] == "2021-01-01"
    assert event["scope_end"] == "2021-12-31"


def test_log_emits_nothing_when_everything_in_scope() -> None:
    part = ScopePartition(in_scope=(_row("NIFTY", date(2026, 6, 25)),), out_by_expiry={})
    with capture_logs() as logs:
        log_out_of_scope_expiries(part, scope_start=None, scope_end=None)
    assert [e for e in logs if e["event"] == "fno.expiry_out_of_scope"] == []


# ── normalize_expiry_scope — the live-edge law (DEC-EXPIRY-SCOPE-LIVE-EDGE-001) ──

_ENVELOPE_END = date(2026, 7, 3)  # the request-time "previous day" default bound
_FLOOR = date(2020, 1, 1)  # GROWW_HISTORY_FLOOR (CLAUDE.md §3)


def _normalize(start: date | None, end: date | None) -> tuple[date | None, date | None]:
    return normalize_expiry_scope(start, end, envelope_end=_ENVELOPE_END, floor=_FLOOR)


def test_normalize_default_shaped_bounds_lift_to_unbounded() -> None:
    """The picker's untouched pre-fill (floor → prev-day) lifts to (None, None).

    THE live-edge bug (DEC-EXPIRY-SCOPE-LIVE-EDGE-001): the default one-click
    pull sent exactly these bounds, so ``scope_end = prev-day`` scoped OUT every
    live/unexpired contract. Default-shaped in ⇒ unbounded out.
    """
    assert _normalize(_FLOOR, _ENVELOPE_END) == (None, None)


def test_normalize_scope_end_equal_to_envelope_end_lifts() -> None:
    """Boundary: ``scope_end == envelope_end`` lifts (>= is the law)."""
    assert _normalize(None, _ENVELOPE_END) == (None, None)


def test_normalize_scope_end_after_envelope_end_lifts() -> None:
    """An explicit 'today' (or any later) pick also lifts — never a narrower scope."""
    assert _normalize(None, _ENVELOPE_END + timedelta(days=1)) == (None, None)


def test_normalize_scope_end_one_day_before_envelope_is_kept() -> None:
    """Boundary teeth: prev-day − 1 is a GENUINE narrowing — passes through."""
    kept = _ENVELOPE_END - timedelta(days=1)
    assert _normalize(None, kept) == (None, kept)


def test_normalize_scope_start_at_floor_lifts() -> None:
    """``scope_start == floor`` is the default pre-fill — lifted to None."""
    assert _normalize(_FLOOR, None) == (None, None)


def test_normalize_scope_start_below_floor_lifts() -> None:
    """A pre-2020 pick is default-equivalent (nothing predates the floor)."""
    assert _normalize(date(2019, 6, 1), None) == (None, None)


def test_normalize_scope_start_one_day_after_floor_is_kept() -> None:
    """Boundary teeth: floor + 1 day is a genuine narrowing — kept."""
    kept = _FLOOR + timedelta(days=1)
    assert _normalize(kept, None) == (kept, None)


def test_normalize_genuinely_narrowed_range_passes_through_unchanged() -> None:
    """#1146 permutation-preservation: a narrowed range scopes exactly as today."""
    start, end = date(2021, 3, 1), date(2022, 4, 15)
    assert _normalize(start, end) == (start, end)


def test_normalize_both_none_is_identity_and_silent() -> None:
    """The blank pull is byte-identical: (None, None) in ⇒ (None, None) out, no log."""
    with capture_logs() as logs:
        assert _normalize(None, None) == (None, None)
    assert [e for e in logs if e["event"] == "fno.expiry_scope.live_edge_unbounded"] == []


def test_normalize_kept_range_emits_no_log() -> None:
    """A genuinely narrowed range is not a lift — no live-edge log fires."""
    with capture_logs() as logs:
        _normalize(date(2021, 3, 1), date(2022, 4, 15))
    assert [e for e in logs if e["event"] == "fno.expiry_scope.live_edge_unbounded"] == []


def test_normalize_lift_logs_raw_vs_effective() -> None:
    """The one live-edge log names the raw AND effective bounds + the envelope."""
    with capture_logs() as logs:
        _normalize(_FLOOR, _ENVELOPE_END)
    (event,) = [e for e in logs if e["event"] == "fno.expiry_scope.live_edge_unbounded"]
    assert event["raw_start"] == "2020-01-01"
    assert event["raw_end"] == "2026-07-03"
    assert event["effective_start"] is None
    assert event["effective_end"] is None
    assert event["envelope_end"] == "2026-07-03"
    assert event["floor"] == "2020-01-01"


def test_normalize_partial_lift_end_only_logs_and_keeps_start() -> None:
    """Only the default-shaped bound lifts; the narrowed one is kept + named raw."""
    with capture_logs() as logs:
        result = _normalize(date(2021, 3, 1), _ENVELOPE_END)
    assert result == (date(2021, 3, 1), None)
    (event,) = [e for e in logs if e["event"] == "fno.expiry_scope.live_edge_unbounded"]
    assert event["raw_start"] == "2021-03-01"
    assert event["effective_start"] == "2021-03-01"
    assert event["effective_end"] is None


def test_normalize_lifted_default_keeps_live_expiry_in_scope() -> None:
    """END-TO-END teeth: the normalized default scope keeps a LIVE expiry pulled.

    Before the fix the default bounds excluded an expiry after prev-day; after
    normalization the partition keeps the live contract IN SCOPE with zero
    out-of-scope groups.
    """
    live = _row("NIFTY", _ENVELOPE_END + timedelta(days=20))
    start, end = _normalize(_FLOOR, _ENVELOPE_END)
    part = partition_contracts_by_scope([live], scope_start=start, scope_end=end)
    assert part.in_scope == (live,)
    assert part.out_by_expiry == {}
    # Regression contrast: the RAW default bounds would have scoped it OUT.
    raw = partition_contracts_by_scope([live], scope_start=_FLOOR, scope_end=_ENVELOPE_END)
    assert raw.in_scope == ()
