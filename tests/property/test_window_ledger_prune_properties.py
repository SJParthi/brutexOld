"""Property invariants for `WindowLedger.prune_superseded` (DEC-WINDOW-TRAILING-GC-001).

For ANY simulated multi-day pull history (a fixed tiling anchor, a sequence of
advancing ``end`` dates, arbitrary per-window outcomes recorded between pulls),
after each plan→prune cycle:

* **No-mask monotonicity** — for EVERY key probed, ``is_skippable`` after the
  prune implies ``is_skippable`` before it (pruning can only turn skips into
  re-fetches, never a fetch into a skip — the #598 guarantee by construction).
* **Plan completeness** — every key of the current tiling has a row (the
  equal-end rule never prunes a current-plan key).
* **Zero survivors** — no surviving row of a planned series sits on the tiling
  with an end strictly shorter than the tile's planned end.
* **Idempotence** — a second prune with the same index removes nothing.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from brutex.data.fetch_coverage import WindowKey
from brutex.data.window_ledger import WindowLedger
from hypothesis import given
from hypothesis import strategies as st

_ANCHOR = date(2026, 1, 1)
_NOW = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)

#: Per-window outcome scripts the simulation may record between pulls
#: (raw_bars, error_type) — SUCCESS / EMPTY / FAILED, plus "leave it PLANNED".
_OUTCOMES = (None, (5, None), (0, None), (0, "DhanServerError"))


def _tiles(end_offset: int, *, span_days: int) -> list[tuple[date, date]]:
    """The canonical clipped tiling of ``[_ANCHOR, _ANCHOR + end_offset)``."""
    end = _ANCHOR + timedelta(days=end_offset)
    tiles: list[tuple[date, date]] = []
    cursor = _ANCHOR
    span = timedelta(days=span_days)
    while cursor < end:
        window_end = min(cursor + span, end)
        tiles.append((cursor, window_end))
        cursor = window_end
    return tiles


def _key(series: str, ws: date, we: date) -> WindowKey:
    return WindowKey(market="dhan", security_id=series, interval="1minute", start=ws, end=we)


@given(
    end_offsets=st.lists(st.integers(min_value=1, max_value=40), min_size=2, max_size=6),
    span_days=st.integers(min_value=3, max_value=15),
    outcome_picks=st.lists(st.sampled_from(_OUTCOMES), min_size=1, max_size=64),
    series_names=st.sets(st.sampled_from(("NIFTY", "BANKNIFTY", "SENSEX")), min_size=1, max_size=3),
)
def test_prune_never_masks_and_tracks_the_live_tiling(
    end_offsets: list[int],
    span_days: int,
    outcome_picks: list[tuple[int, str | None] | None],
    series_names: set[str],
) -> None:
    """The four invariants hold across ANY advancing-end pull history."""
    ledger = WindowLedger()
    series = sorted(series_names)
    picks = iter(outcome_picks * (len(end_offsets) * len(series) * 50))
    # Make the offsets cumulative so ``end`` only ever advances (the daily pull).
    cumulative = 0
    for offset in end_offsets:
        cumulative += offset
        tiles = _tiles(cumulative, span_days=span_days)
        tiles_iso = {ws.isoformat(): we.isoformat() for ws, we in tiles}
        planned_index = {("dhan", sym, "1minute"): tiles_iso for sym in series}
        # Plan today's tiling (idempotent), as the planners do.
        for sym in series:
            for ws, we in tiles:
                ledger.plan(_key(sym, ws, we), now=_NOW)

        # Snapshot every key's skippability BEFORE the prune (rows + plan keys).
        probe_keys = [
            _key(
                r.security_id, date.fromisoformat(r.window_start), date.fromisoformat(r.window_end)
            )
            for r in ledger.records()
        ]
        before = {WindowLedger._key(k): ledger.is_skippable(k) for k in probe_keys}

        pruned = ledger.prune_superseded(planned_index)

        # No-mask monotonicity: skippable after ⇒ skippable before.
        for k in probe_keys:
            if ledger.is_skippable(k):
                assert before[WindowLedger._key(k)] is True
        # Plan completeness: every current-tiling key has a row.
        surviving = {(r.security_id, r.window_start, r.window_end) for r in ledger.records()}
        for sym in series:
            for ws, we in tiles:
                assert (sym, ws.isoformat(), we.isoformat()) in surviving
        # Zero survivors below the planned end on the tiling.
        for r in ledger.records():
            planned_end = tiles_iso.get(r.window_start)
            if planned_end is not None:
                assert r.window_end >= planned_end
        # Idempotence: the prune is a fixed point.
        assert ledger.prune_superseded(planned_index) == 0
        assert pruned >= 0

        # Record some outcomes on the live tiling (tomorrow's orphan seeds).
        for sym in series:
            for ws, we in tiles:
                pick = next(picks)
                if pick is None:
                    continue
                raw_bars, error_type = pick
                ledger.record(
                    _key(sym, ws, we),
                    raw_bars=raw_bars,
                    error_type=error_type,
                    bars_appended=raw_bars,
                    now=_NOW,
                )


@given(
    end_offsets=st.lists(st.integers(min_value=1, max_value=40), min_size=2, max_size=6),
    span_days=st.integers(min_value=3, max_value=15),
    outcome_picks=st.lists(st.sampled_from(_OUTCOMES), min_size=1, max_size=64),
)
def test_no_planned_ghost_survives_any_end_history(
    end_offsets: list[int],
    span_days: int,
    outcome_picks: list[tuple[int, str | None] | None],
) -> None:
    """DEC-PLAN-CLAMP-PARITY-001 — the ghost-free invariant under ANY end history.

    Ends here are NOT cumulative: the sequence may shrink as well as grow,
    exactly what the settled-data clamp does around the 15:30 IST close and the
    daily bound (and what the pre-parity planner did vs the clamped fetch).
    After every plan→prune cycle, a surviving ``PLANNED`` row with zero attempts
    whose start is on the current tiling ends EXACTLY at its planned tile end —
    stale intent (shorter OR longer) never accumulates. Settled/attempted rows
    are exempt (history is never deleted by the longer-end rule), and the
    current plan is always complete.
    """
    ledger = WindowLedger()
    picks = iter(outcome_picks * (len(end_offsets) * 50))
    for offset in end_offsets:
        tiles = _tiles(offset, span_days=span_days)
        tiles_iso = {ws.isoformat(): we.isoformat() for ws, we in tiles}
        planned_index = {("dhan", "NIFTY", "1minute"): tiles_iso}
        for ws, we in tiles:
            ledger.plan(_key("NIFTY", ws, we), now=_NOW)
        ledger.prune_superseded(planned_index)

        surviving = {(r.window_start, r.window_end) for r in ledger.records()}
        for ws, we in tiles:  # plan completeness
            assert (ws.isoformat(), we.isoformat()) in surviving
        for r in ledger.records():  # ghost-free: pure intent sits ONLY on the plan
            planned_end = tiles_iso.get(r.window_start)
            if planned_end is not None and r.status == "PLANNED" and r.attempts == 0:
                assert r.window_end == planned_end
        assert ledger.prune_superseded(planned_index) == 0  # fixed point

        for ws, we in tiles:  # seed outcomes for the next (maybe shorter) plan
            pick = next(picks)
            if pick is None:
                continue
            raw_bars, error_type = pick
            ledger.record(
                _key("NIFTY", ws, we),
                raw_bars=raw_bars,
                error_type=error_type,
                bars_appended=raw_bars,
                now=_NOW,
            )
