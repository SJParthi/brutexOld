"""Bench pins — the window-ledger cache-residency collapse (fix 3).

DEC-MEMORY-FOOTPRINT-HARDENING-001 fix 3: the shared ledger read cache
(``db_view._LEDGER_READ_CACHE``) previously held TWO whole-ledger per-row
payloads resident per lake — the parsed-records family (``ledger-records|``,
~1.0 KB/row of tracked bytes) AND the projected-rows family (``ledger-rows|``,
~1.0 KB/row tracked) — process-lifetime (the measured shape: a production
841k-row ledger ≈ 0.8 GB PER family of tracked bytes). The fix retires the
rows family: the records family is THE single resident per-row representation
and the /db grid rows are projected ON DEMAND (O(page) per route poll via
``window_ledger_rows_page``; the whole-projection compat surface pays a
disclosed O(rows) per call).

Three pin groups, ceilings per the B-4/B-6 measured-slack discipline of
``test_ledger_delta_o_page_o1.py`` (ABSOLUTE bounds where a ratio cannot
discriminate, :func:`ratio_ceiling` where flatness is the claim):

* M-1  RESIDENCY: after serving the page consumer AND the whole-rows consumer
       AND the records consumer AND the rollup, the cache's own tracked bytes
       are ≤ 60% of the VERBATIM pre-fix two-family residency (computed
       in-test from the exact values the retired design cached) — measured
       2026-07-11 (sandbox, 50k rows): two-family 60.0 MB → post-fix 30.0 MB,
       ratio 0.500 under deep-size accounting; 32.2 MB / ratio 0.537 since
       the M1 hostile-review sizing hint (the records family is now TRACKED
       at the deliberately-conservative 640 B/record estimate, OVER the
       ~600 B/record deep-walk figure — the safe direction for a byte bound);
* M-2  REPEAT-POLL: page + records + rollup repeat polls stay O(1)-class —
       each median ≪ the cold parse (measured 2026-07-11: cold 966 ms @ 50k
       rows vs page 88 µs / records 32 µs / rollup 45 µs medians), and the
       page poll is FLAT vs ledger depth (10k vs 50k rows, the honest
       O(pending-delta) stamp probe + O(limit) projection being the only
       per-poll terms); the whole-rows COMPAT surface honestly pays O(rows)
       per call (measured 34 ms @ 50k) — bounded under the cold parse (it
       re-projects, never re-parses), never claimed O(1);
* M-3  BYTE-PARITY at scale: the paged rows equal the verbatim pre-fix
       whole-projection's slice, and the whole-rows surface equals the exact
       value the retired ``ledger-rows`` family cached.

HONEST (Golden Rule #10): the tracked figures ride the watermark's
best-effort accounting — the records family via the M1 O(1)
``_ledger_payload_size_hint`` estimate (pinned within 3× of the deep walk),
everything else via ``deep_size_bytes`` — NOT exact RSS (the pre-fix rows
family SHARED leaf objects with the records in one steady-state generation,
so true RSS savings are smaller steady-state but larger across
floor-staggered generations, where the two families held rows from DIFFERENT
parses); the FIRST read per stamp stays O(rows) parse — never claimed
O(page)-total.

Plus the M1 hostile-review pin (``test_cold_serve_sizing_cost_bounded_at_scale``):
the watermarked cold serve's SIZING step is O(1) in rows — measured 0.01 ms
best-of-3 @ 200k records post-fix vs the 1.52 s pre-fix deep walk at the same
scale (5.28 s reviewer-measured at the 841k production shape).
"""

from __future__ import annotations

# Whitebox bench pins read module-private helpers by design (DEC-P0-017 convention).
# pyright: reportPrivateUsage=false
import statistics
import time
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

pytest.importorskip("pyarrow", reason="the ledger store writes Parquet via pyarrow")
pytest.importorskip("duckdb", reason="db_view imports duckdb at module scope")

from brutex.dashboard import db_view
from brutex.dashboard.movers_cache import deep_size_bytes
from brutex.data import window_ledger_store as wls
from brutex.data.fetch_coverage import WindowKey
from brutex.data.window_ledger import WindowLedger, ledger_record_row

from tests.bench._o1_tiers import ratio_ceiling

_NOW = datetime(2026, 7, 6, 12, 0, tzinfo=UTC)

#: The fix's residency budget: the post-fix tracked bytes must stay at or
#: below this fraction of the verbatim two-family baseline. Measured 0.500 on
#: 2026-07-11 (sandbox, 50k rows: 60.0 MB two-family → 30.0 MB post-fix — the
#: records family plus the small spot-rollup dict) under deep-size
#: accounting; 0.537 (32.2 MB) since the M1 sizing hint tracks the records
#: family at the conservative 640 B/record estimate. 0.60 leaves slack for
#: interpreter/layout variance while a regression that re-caches a second
#: per-row family (~2× ⇒ ratio ~1.0) fails loudly.
_RESIDENCY_CEILING = 0.60


def _seeded_lake(root: Path, *, rows: int, tag: str) -> Path:
    """A REAL lake with *rows* ledger records via the production writer."""
    lake = root / tag
    ledger = WindowLedger()
    for i in range(rows):
        ledger.record(
            WindowKey(
                market=("groww", "groww-option", "groww-future")[i % 3],
                security_id=f"SYM{i % 800}",
                interval="1minute",
                start=date(2021 + (i // 12_000) % 5, 1 + (i // 1_000) % 12, 1),
                end=date(2021 + (i // 12_000) % 5, 1 + (i // 1_000) % 12, 28),
            ),
            raw_bars=5,
            error_type=None,
            bars_appended=5,
            now=_NOW,
        )
    wls.save_window_ledger(ledger, lake)
    return lake


def _median_us(fn: Callable[[], object], *, iterations: int) -> float:
    samples: list[int] = []
    for _ in range(iterations):
        t0 = time.perf_counter_ns()
        fn()
        samples.append(time.perf_counter_ns() - t0)
    return statistics.median(samples) / 1e3


def test_residency_collapse_and_repeat_polls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M-1 + M-2 (absolute arms) + M-3 — one 50k-row fixture, every consumer served."""
    rows_n = 50_000
    lake = _seeded_lake(tmp_path, rows=rows_n, tag="lake")
    monkeypatch.setattr(db_view, "_LEDGER_READ_CACHE", db_view._make_ledger_read_cache())

    # The COLD parse cost — the yardstick every repeat-poll bound is scaled by.
    t0 = time.perf_counter()
    records = tuple(wls.load_window_ledger(lake).records())
    cold_s = time.perf_counter() - t0
    assert len(records) == rows_n

    # M-1 baseline: the VERBATIM pre-fix two-family cached values — the records
    # family's ``(generation, records)`` and the retired rows family's whole
    # projection — sized by the deep-size accounting the byte watermark (fix 2)
    # used pre-hint; the post side (`total_bytes`) now tracks the records
    # family via the M1 hint's conservative 640 B/record estimate (slightly
    # OVER the deep walk — the ratio moved 0.500 → 0.537, still a genuine
    # one-family residency against the same two-family baseline).
    old_rows = tuple(ledger_record_row(r) for r in records)
    baseline = deep_size_bytes((0, records)) + deep_size_bytes(old_rows)

    # Serve EVERY consumer of the two former families through the new design.
    total, page = db_view.window_ledger_rows_page(lake, offset=100, limit=200)
    whole = db_view.window_ledger_rows_cached(lake)
    served_records = db_view._ledger_records_cached(lake)
    rollup = db_view.spot_window_rollup(lake)
    assert total == rows_n and rollup

    # M-3 byte-parity at scale: page == the verbatim old projection's slice;
    # the whole-rows compat surface == the retired family's exact cached value.
    assert page == old_rows[100:300]
    assert whole == old_rows
    assert served_records == records

    # M-1: the cache's own tracked residency after serving everything.
    post = db_view._LEDGER_READ_CACHE.total_bytes()
    families = sorted({k.split("|", 1)[0] for k in db_view._LEDGER_READ_CACHE._entries})
    assert families == ["ledger-records", "spot-rollup"]  # NO ledger-rows family
    assert post <= _RESIDENCY_CEILING * baseline, (
        f"resident ledger-cache bytes {post / 1e6:.1f} MB exceed "
        f"{_RESIDENCY_CEILING:.0%} of the pre-fix two-family residency "
        f"({baseline / 1e6:.1f} MB) — a second per-row family may have been "
        f"re-cached (measured 2026-07-11: ratio 0.500 at 50k rows under "
        f"deep-size accounting, 0.537 under the M1 sizing hint)."
    )

    # M-2 absolute arms: repeat polls ≪ the cold parse (measured 2026-07-11:
    # cold 966 ms vs page 88 µs / records 32 µs / rollup 45 µs — ~4 orders of
    # slack; the /10 bound only needs to catch a regression back to per-poll
    # O(rows) work, which lands AT cold-parse scale).
    page_us = _median_us(
        lambda: db_view.window_ledger_rows_page(lake, offset=100, limit=200), iterations=30
    )
    records_us = _median_us(lambda: db_view._ledger_records_cached(lake), iterations=30)
    rollup_us = _median_us(lambda: db_view.spot_window_rollup(lake), iterations=30)
    cold_us = cold_s * 1e6
    for label, measured in (("page", page_us), ("records", records_us), ("rollup", rollup_us)):
        assert measured < cold_us / 10.0, (
            f"repeat {label} poll median {measured / 1e3:.2f} ms is not O(1)-class vs the "
            f"{cold_s * 1e3:.0f} ms cold parse — the {label} consumer may have regressed "
            f"to per-poll O(rows) work."
        )
    # The whole-rows COMPAT surface honestly pays O(rows) per call (a
    # projection, measured 34 ms @ 50k) — bounded under the cold parse (it
    # must never re-PARSE), deliberately NOT held to the O(1) bounds above.
    whole_us = _median_us(lambda: db_view.window_ledger_rows_cached(lake), iterations=5)
    assert whole_us < cold_us, (
        f"whole-rows compat call median {whole_us / 1e3:.1f} ms exceeds the cold parse "
        f"({cold_s * 1e3:.0f} ms) — the per-call projection may have regressed to a re-parse."
    )


def test_page_poll_flat_vs_ledger_depth(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """M-2 (ratio arm): the route-shape page poll is FLAT vs ledger depth.

    10k vs 50k base rows, one 200-row page within one stamp: the per-poll
    terms are the O(1) records-cache handoff + the O(deltas) stamp probe
    (zero pending here) + the O(limit) projection — none scales with the
    stored row count. Ceiling: :func:`ratio_ceiling` (8.0 shared-CI tier;
    measured 2026-07-11 sandbox ~1.0×)."""

    def _time(rows_n: int, tag: str) -> float:
        lake = _seeded_lake(tmp_path, rows=rows_n, tag=tag)
        monkeypatch.setattr(db_view, "_LEDGER_READ_CACHE", db_view._make_ledger_read_cache())

        def _poll() -> None:
            total, page = db_view.window_ledger_rows_page(lake, offset=0, limit=200)
            assert total == rows_n and len(page) == 200

        for _ in range(3):  # warm: the one real parse per stamp
            _poll()
        return _median_us(_poll, iterations=50)

    t_small = _time(10_000, "small")
    t_large = _time(50_000, "large")
    ratio = t_large / max(t_small, 1.0)
    assert ratio < ratio_ceiling(8.0), (
        f"page-poll ratio {ratio:.2f}× (50k vs 10k ledger rows, one 200-row page) exceeds "
        f"the ceiling — the route read may have regressed to per-poll O(rows) projection. "
        f"t_10k={t_small:.1f}µs, t_50k={t_large:.1f}µs."
    )


def test_cold_serve_sizing_cost_bounded_at_scale() -> None:
    """M1 (hostile-review fold): the watermarked cold serve's SIZING step is O(1).

    FAILS BEFORE the fix: every cold ledger parse ran ``deep_size_bytes`` over
    the whole ``(generation, tuple[WindowLedgerRecord, ...])`` payload —
    reviewer-measured **5.28 s** at the documented 841k-row production scale
    (~25M getsizeof/getattr ops through the ``__slots__`` MRO branch) plus a
    transient ~1.7M-entry ``seen`` id-set (~100+ MB), recurring at pull cadence
    (the stamp moves per delta flush). The ``_ledger_payload_size_hint`` makes
    sizing O(1) in rows.

    Scale honesty: 200k records stand in for the 841k production shape because
    the deep walk is LINEAR in records (measured ~8-10 µs/record in this
    sandbox — a walk regression at 200k lands at ~1.5-2 s, vastly over the
    ceiling), while the hint is O(1); 200k is generatable in seconds where 841k
    is not CI-friendly. The timed call's ``compute`` returns a PREBUILT payload
    (O(1)), so the measured cold serve ≈ sizing + cache bookkeeping. Best-of-3
    per the house best-of-N-MIN idiom; the 50 ms ceiling is ~30-100× the
    post-fix cost and ~30-40× UNDER the pre-fix walk at this scale.
    """
    rows_n = 200_000
    ledger = WindowLedger()
    for i in range(rows_n):
        ledger.record(
            WindowKey(
                market="groww",
                security_id=f"SYM{i}",  # unique per row — 200k DISTINCT records
                interval="1minute",
                start=date(2021, 1, 1),
                end=date(2021, 1, 31),
            ),
            raw_bars=5,
            error_type=None,
            bars_appended=5,
            now=_NOW,
        )
    records = tuple(ledger.records())
    assert len(records) == rows_n
    payload = (0, records)
    cache = db_view._make_ledger_read_cache()
    best_s = float("inf")
    for i in range(3):
        t0 = time.perf_counter()
        served = cache.get_or_compute(
            f"ledger-records|/bench-{i}", generation=0, compute=lambda: payload
        )
        best_s = min(best_s, time.perf_counter() - t0)
        assert served is payload
    assert cache.total_bytes() > 0  # the watermark accounting genuinely ran
    assert best_s < 0.050, (
        f"the watermarked cold serve's sizing step took {best_s * 1e3:.1f} ms at "
        f"{rows_n} records — the O(rows) deep walk may have regressed back into "
        f"the ledger cache's insert path (pre-fix: ~1.5-2 s at this scale, "
        f"5.28 s at the 841k production shape)."
    )
