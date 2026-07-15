"""Cost gate for the window-ledger LEGACY-degrade arm's parse memo (H4d).

DEC-O1-GUARANTEE-HARDEN-001: ``query_window_ledger_page``'s legacy degrade
(``conn=None`` — also the double-failure torn-read / legacy-shape arm) serves
``window_ledger_rows_cached``, whose full O(rows) Python ledger parse is
memoised ONCE per ledger stamp on the shared ``_LEDGER_READ_CACHE``
(DEC-LEDGER-DELTA-O-PAGE-001 / DEC-DB-LATENCY-HARDENING-001). That law was
unit-pinned for the FAST path's caches but the degrade arm had NO bench-dir
cost gate (the reader-C missing-gates census). This gate pins it
DETERMINISTICALLY (a parse-count spy on ``load_window_ledger`` — no wall
clock, no flake):

* N degraded clicks (repeat + paging offsets) within ONE ledger stamp run
  EXACTLY ONE Python ledger parse — the O(1)-on-repeat handoff;
* a mid-pull delta append (the stamp moves) re-parses exactly ONCE across
  ANY number of later clicks — once per generation, never per click — and
  the fresh rows are served within the documented SWR convergence window.

Harness REUSED from ``tests/unit/test_dashboard_db_view_window_ledger_page.py``
(§11 COMMON — the ``test_sweep_worker_scaling`` cross-suite import precedent);
the cache is rebuilt through the ONE ``_make_ledger_read_cache`` construction
seam with an injected clock + INLINE ``swr_runner`` (the established
``test_dashboard_db_view_ledger_read_cache`` idiom) so the #920 adaptive floor
and the #1751 stale-while-revalidate arm are deterministic in-test.

HONEST envelope (Golden Rule #10): O(1)-on-repeat — the FIRST degraded click
per ledger stamp pays the real O(rows) parse + row projection, and after a
stamp move the SWR arm serves the PRIOR stamp's rows for up to two clicks
(records then rows revalidate — each family under its own floor) before the
fresh rows land; the re-parse itself still happens exactly once.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("pyarrow")
pytest.importorskip("duckdb")

from brutex.dashboard import db_view
from brutex.dashboard.db_view import query_window_ledger_page
from brutex.data import window_ledger_store as wls

from tests.unit.test_dashboard_db_view_window_ledger_page import (
    _NOW,
    _key,
    _seed_mixed_ledger,
)


def _inject_cache(monkeypatch: pytest.MonkeyPatch, clock: dict[str, float]) -> None:
    """Fresh shared ledger cache with an injected clock + INLINE SWR runner."""
    monkeypatch.setattr(
        db_view,
        "_LEDGER_READ_CACHE",
        db_view._make_ledger_read_cache(  # pyright: ignore[reportPrivateUsage]
            clock=lambda: clock["t"], swr_runner=lambda job: job()
        ),
    )


def _parse_spy(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Count every real ``load_window_ledger`` parse the degrade arm triggers."""
    calls = {"parses": 0}
    real = wls.load_window_ledger

    def _spy(lake_root: Path, *args: Any, **kwargs: Any) -> Any:
        calls["parses"] += 1
        return real(lake_root, *args, **kwargs)

    monkeypatch.setattr(wls, "load_window_ledger", _spy)
    return calls


class TestDegradedRecountMemoisesOncePerGeneration:
    def test_repeat_and_paging_clicks_parse_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Within one ledger stamp, N degraded clicks = EXACTLY ONE parse."""
        _seed_mixed_ledger(tmp_path)
        clock = {"t": 0.0}
        _inject_cache(monkeypatch, clock)
        spy = _parse_spy(monkeypatch)
        pages = [
            query_window_ledger_page(None, tmp_path, limit=4, offset=off) for off in (0, 0, 4, 8, 0)
        ]
        totals = {total for _rows, total in pages}
        assert totals == {10}  # the seeded 8 base + 1 within-triple + 1 delta key
        assert len(pages[0][0]) == 4 and len(pages[3][0]) == 2  # real O(page) slices
        assert spy["parses"] == 1, (
            f"expected ONE ledger parse for five degraded clicks within one stamp, "
            f"measured {spy['parses']} — the legacy-degrade memo has regressed to "
            f"a per-click O(rows) re-parse"
        )

    def test_stamp_move_reparses_exactly_once_never_per_click(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A delta append re-parses ONCE across any number of later clicks.

        The stamp move re-keys the records family; past the adaptive floor
        (clock advanced beyond the 120 s cap) the SWR arm serves the prior
        rows while ONE inline revalidator re-parses — the fresh total (11)
        must land within the documented two-click convergence window, with
        the parse count moving 1 → 2 EXACTLY (never one per click).
        """
        _seed_mixed_ledger(tmp_path)
        clock = {"t": 0.0}
        _inject_cache(monkeypatch, clock)
        spy = _parse_spy(monkeypatch)
        _rows, total = query_window_ledger_page(None, tmp_path, limit=500, offset=0)
        assert total == 10 and spy["parses"] == 1
        # A mid-pull flush lands a NEW key — the composite stamp moves.
        live = wls.load_window_ledger(tmp_path)  # the spy counts this setup read
        live.record(_key("ZZLATE"), raw_bars=5, error_type=None, bars_appended=5, now=_NOW)
        assert wls.append_ledger_delta(live, tmp_path) == 1
        setup_parses = spy["parses"]  # 2 — the setup read above, not a click
        clock["t"] = 130.0  # past the 120 s floor cap — the SWR arm engages
        totals = [
            query_window_ledger_page(None, tmp_path, limit=500, offset=0)[1] for _ in range(4)
        ]
        assert spy["parses"] == setup_parses + 1, (
            f"expected EXACTLY ONE re-parse for the moved stamp across four clicks, "
            f"measured {spy['parses'] - setup_parses} — the degraded recount must "
            f"memoise once per generation, never per click"
        )
        # SWR convergence: the first click(s) may serve the prior stamp's rows
        # (documented, bounded); the fresh total lands by the third click and
        # every later click serves it.
        assert totals[0] in (10, 11)
        assert totals[2] == 11 and totals[3] == 11
        rows, total = query_window_ledger_page(None, tmp_path, limit=500, offset=0)
        assert total == 11 and any(r[1] == "ZZLATE" for r in rows)
        assert spy["parses"] == setup_parses + 1  # still exactly one re-parse
