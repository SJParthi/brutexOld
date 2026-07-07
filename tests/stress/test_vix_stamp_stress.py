"""Sustained-load stress for the India-VIX trade stamp (DEC-VIX-STAMP-WIRE-001 Stage D).

Three envelopes proven AT SCALE (the one-shot unit tests structurally cannot):

* **dict-container memory per series entry** — building a
  :class:`TradeReferenceStamper` over a ≥100K-instant VIX series costs a
  bounded number of bytes of NEW probe-dict storage per entry (bars built
  UNTRACKED, so the dict's re-referenced key/value objects are excluded;
  measured ~52 B/entry). The ``tracemalloc`` recipe (untracked warmup →
  baseline → build → net) mirrors ``tests/stress/test_memory_growth.py``.
* **RETAINED memory per series entry** (round-2 panel fold) — the recipe the
  first gate cannot see: production (``build_vix_stamper``) DROPS the bar
  lists after ``from_vix_bars``, so what the process retains per entry is the
  dict slot PLUS its datetime key PLUS its Decimal value. Bars are built
  INSIDE the traced region with a DISTINCT Decimal per entry, then dropped —
  the net is the true retained footprint (~204 B/entry, the round-1 figure),
  gated against the SAME 512 B ceiling. Spawn-pool arithmetic (§8, stated
  because ``sweep_parallel`` only says the stamper "crosses the SPAWN
  boundary verbatim"): every spawn worker unpickles its OWN full copy, so a
  full 2020→today ~610K-instant series costs ≈ 204 B × 610K ≈ 125 MB per
  process ≈ 1.25 GB at the M4-Pro's 8-9 workers + parent — comfortably
  inside the 48 GB envelope (an arithmetic projection at the measured
  per-entry cost, not a measured 610K-series run — Golden Rule #10).
* **WARN-volume cap under an all-miss flood** — a sweep stamping ≥100K miss
  occurrences emits AT MOST :data:`MISS_WARN_DETAIL_CAP` + 1 warnings (the
  per-instant details + the ONE ``vix_miss_capped`` sentinel), the memo set
  stays bounded at the cap, and the honest ``miss_count`` tally is exact —
  the log/memory bound never hides a single miss (Golden Rule #1/#10).
"""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import gc
import os
import tracemalloc
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from brutex.data.models import CandleBar
from brutex.engine.reference_series import MISS_WARN_DETAIL_CAP, TradeReferenceStamper
from structlog.testing import capture_logs

#: All stress tests carry the ``stress`` marker (registered in pyproject.toml)
#: so the nightly ``pytest tests/stress -m stress`` soak selects them.
pytestmark = pytest.mark.stress

#: An EXPLICIT knob wins (targeted soak); otherwise the ≥100K proof default.
_DEFAULT_ENTRIES = 100_000
_DEFAULT_MISS_EVENTS = 100_000

#: Absolute per-entry ceiling for the stamper's probe dict. Round-1 measured
#: ~204 B/entry on CPython 3.12 (dict slot + key/value references + the
#: shared-key overallocation); 512 B leaves honest interpreter headroom while
#: still catching a per-entry blow-up (e.g. an accidental per-entry object).
_PER_ENTRY_CEILING_BYTES = 512


def _resolve(env: str, default: int) -> int:
    raw = os.environ.get(env, "").strip()
    if raw:
        return max(1, int(raw))
    return default


_T0 = datetime(2026, 1, 5, 3, 46, tzinfo=UTC)  # 09:16 IST


def _vix_bars(n: int) -> list[CandleBar]:
    level = Decimal("14.25")
    return [
        CandleBar(
            timestamp=_T0 + timedelta(minutes=i),
            open=level,
            high=level,
            low=level,
            close=level,
            volume=0,
            open_interest=None,
        )
        for i in range(n)
    ]


def _vix_bars_distinct(n: int) -> list[CandleBar]:
    """Bars whose Decimal level is a DISTINCT object per entry.

    ``_vix_bars`` shares ONE ``Decimal("14.25")`` across every bar — fine for
    the dict-container gate, but the RETAINED-footprint gate must count a
    real per-entry value object (a lake read materialises one Decimal per
    bar), so each bar here carries its own level object.
    """
    return [
        CandleBar(
            timestamp=_T0 + timedelta(minutes=i),
            open=(level := Decimal(f"14.{i % 100:02d}")),
            high=level,
            low=level,
            close=level,
            volume=0,
            open_interest=None,
        )
        for i in range(n)
    ]


def test_stamper_memory_per_series_entry_is_bounded() -> None:
    """Building a ≥100K-entry stamper costs ≤ the per-entry byte ceiling."""
    entries = _resolve("STRESS_VIX_STAMP_ENTRIES", _DEFAULT_ENTRIES)
    # Bars are built UNTRACKED — the stamper's own footprint is the claim
    # (its dict re-references the bars' datetime/Decimal objects; the net new
    # cost is the probe-dict storage, not a copy of the series).
    bars = _vix_bars(entries)
    warmup = TradeReferenceStamper.from_vix_bars(bars[:64])  # absorb one-time paths
    assert warmup.vix.level_at(_T0) == Decimal("14.25")
    del warmup

    gc.collect()
    tracemalloc.start()
    try:
        gc.collect()
        baseline, _ = tracemalloc.get_traced_memory()
        stamper = TradeReferenceStamper.from_vix_bars(bars)
        gc.collect()
        current, _ = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    net = current - baseline
    per_entry = net / entries
    print(
        f"\nVIX stamper memory stress (DEC-VIX-STAMP-WIRE-001): "
        f"{entries:,} entries | net {net / 1024:.0f} KiB | "
        f"{per_entry:.0f} B/entry (ceiling {_PER_ENTRY_CEILING_BYTES} B)\n"
    )
    assert len(stamper.vix.close_by_ts) == entries  # the whole series is servable
    assert per_entry <= _PER_ENTRY_CEILING_BYTES, (
        f"stamper series memory {per_entry:.0f} B/entry over {entries:,} entries "
        f"exceeds the {_PER_ENTRY_CEILING_BYTES} B ceiling — the probe dict may "
        f"have grown a per-entry object."
    )


def test_stamper_retained_footprint_per_entry_is_bounded() -> None:
    """The RETAINED per-entry cost — dict slot + key + value — stays ≤ ceiling.

    Round-2 panel fold: the container gate above builds bars UNTRACKED, so
    the ~52 B/entry it measures excludes the datetime keys + Decimal values
    the dict re-references — objects production RETAINS after
    ``build_vix_stamper`` drops the bar lists. Building the bars INSIDE the
    traced region and dropping them makes the net the TRUE retained
    footprint; a regression that bloats the per-entry key/value objects
    (invisible to the container gate) trips this one.
    """
    entries = _resolve("STRESS_VIX_STAMP_ENTRIES", _DEFAULT_ENTRIES)
    warmup = TradeReferenceStamper.from_vix_bars(_vix_bars_distinct(64))
    assert warmup.vix.level_at(_T0) == Decimal("14.00")
    del warmup

    gc.collect()
    tracemalloc.start()
    try:
        gc.collect()
        baseline, _ = tracemalloc.get_traced_memory()
        bars = _vix_bars_distinct(entries)
        stamper = TradeReferenceStamper.from_vix_bars(bars)
        del bars  # production drops the slices — only the stamper retains
        gc.collect()
        current, _ = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    net = current - baseline
    per_entry = net / entries
    print(
        f"\nVIX stamper RETAINED-memory stress (DEC-VIX-STAMP-WIRE-001): "
        f"{entries:,} entries | net {net / 1024:.0f} KiB | "
        f"{per_entry:.0f} B/entry retained (ceiling {_PER_ENTRY_CEILING_BYTES} B)\n"
    )
    assert len(stamper.vix.close_by_ts) == entries  # the whole series is servable
    assert per_entry <= _PER_ENTRY_CEILING_BYTES, (
        f"retained stamper memory {per_entry:.0f} B/entry over {entries:,} entries "
        f"exceeds the {_PER_ENTRY_CEILING_BYTES} B ceiling — a per-entry key/value "
        f"object may have grown (this is the figure every SPAWN worker pays)."
    )


def test_all_miss_flood_holds_the_warn_cap_and_the_honest_tally() -> None:
    """≥100K sustained misses: ≤ cap+1 warnings, bounded memo, exact count."""
    miss_events = _resolve("STRESS_VIX_MISS_EVENTS", _DEFAULT_MISS_EVENTS)
    n_calls = max(miss_events // 2, MISS_WARN_DETAIL_CAP + 2)  # 2 misses per stamp
    stamper = TradeReferenceStamper.from_vix_bars([])  # zero coverage — all miss

    with capture_logs() as logs:
        for i in range(n_calls):
            entry_ts = _T0 + timedelta(minutes=2 * i)
            stamper.stamp(entry_ts, entry_ts + timedelta(minutes=1))

    details = [e for e in logs if e["event"] == "reference_series.vix_miss"]
    sentinels = [e for e in logs if e["event"] == "reference_series.vix_miss_capped"]
    print(
        f"\nVIX miss-WARN flood stress (DEC-VIX-STAMP-WIRE-001): "
        f"{2 * n_calls:,} misses over {2 * n_calls:,} distinct instants | "
        f"{len(details)} detail WARNs + {len(sentinels)} sentinel "
        f"(cap {MISS_WARN_DETAIL_CAP})\n"
    )
    # The log-volume bound: cap detailed warnings + exactly ONE sentinel.
    assert len(details) == MISS_WARN_DETAIL_CAP
    assert len(sentinels) == 1
    assert sentinels[0]["distinct_misses"] == MISS_WARN_DETAIL_CAP
    assert len(logs) == MISS_WARN_DETAIL_CAP + 1  # nothing else leaked
    # The memory bound: the memo never grows past the cap.
    assert len(stamper._miss_memo.warned_instants) == MISS_WARN_DETAIL_CAP
    # The honesty bound: every single miss occurrence is tallied.
    assert stamper.miss_count == 2 * n_calls
