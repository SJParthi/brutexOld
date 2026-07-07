"""Memory-leak stress harness — O(1)-bounded memory over ≥100K sustained events.

DEC-MEMLEAK-GATE-001 (MIGRATION.md entry #616). Four long-running hot paths are
proven to hold a FLAT Python-heap footprint under sustained event load — the
leak class that one-shot unit tests structurally cannot catch:

1. **Audit-chain writer** (``brutex.orchestrator.audit.AuditWriter``) — appends
   forever during an ingest run; only ``_seq`` + ``_prev_hash`` may persist
   per append (the FILE grows by design; the WRITER must not).
2. **WindowLedger** (``brutex.data.window_ledger.WindowLedger``) — re-pulls
   cycle the SAME window universe run after run (plan → ``is_skippable`` probe
   → ``record`` on the non-settled remainder); every outcome must REPLACE its
   row, never accumulate.
3. **Sweep results-store append** (``brutex.engine.results_store.ResultsStore``)
   — idempotent re-appends of an existing run-identity must be allocation-flat
   no-ops, including the full ``ResultRow`` construction + validation +
   ``condition_names`` derivation each event (the real per-batch hot path).
4. **Dhan rate controller** (``brutex.adapters.rate_common
   .RateController``) — ``pace``/``on_rate_limit``/``on_timeout``/
   ``on_success`` across full deterministic 5→…→1→…→5 ladder cycles; the
   ``_hits``/``_timeouts`` deques are bounded by window eviction at any
   physical rate ≤ the 5 r/s ceiling (CLAUDE.md §6).

Why tracemalloc is the PRIMARY signal (not RSS)
-----------------------------------------------
The first RSS-growth assertion in this suite was REMOVED in PR #99 — see the
"Note on memory profiling" in ``tests/stress/test_audit_chain_sustained.py`` —
because (a) ``ru_maxrss`` units differ by platform, (b) process-wide peak RSS
includes collection + prior tests, and (c) shared-runner jitter makes hard RSS
ceilings flaky. ``tracemalloc`` resolves all three objections: it counts
Python-level allocations exactly, scoped to the measured region by a per-test
baseline + ``reset_peak()``, platform-independently, immune to allocator/arena
retention noise. RSS is still read (Linux ``/proc/self/status`` ``VmRSS`` — a
CURRENT value, not the ``ru_maxrss`` process-lifetime peak) but only as (1) an
observability print and (2) a deliberately-generous backstop for native-side
leaks tracemalloc cannot see; off-Linux the backstop is skipped.

Measurement recipe (per test)
-----------------------------
1. UNTRACKED warmup — absorbs one-time allocations (interned strings, logger
   templates, dicts growing to final size, code-object caches).
2. ``tracemalloc.start()`` → a TRACKED warmup that turns the steady-state
   working set over at least once, so sustained-phase replacements are
   tracked-for-tracked (an object allocated BEFORE tracing frees invisibly,
   which would otherwise show as one-time baseline drift).
3. ``gc.collect()`` → baseline from ``get_traced_memory()`` → ``reset_peak()``.
4. ≥100K sustained events (the knob below). No O(N) snapshot calls inside the
   measured region — functional end-state assertions run after step 5.
5. ``gc.collect()`` → assert ``net ≤ NET_CEILING`` and ``peak ≤ PEAK_CEILING``.

The ceilings are ABSOLUTE and N-independent — the honest O(1) claim: a genuine
per-event leak crosses them at the 100K default (even 1 byte/event ≈ 100 KB ≫
the 64 KiB net ceilings), a leak-free run does not grow with N at all, and a
larger soak strictly STRENGTHENS the proof (detection floor shrinks as 1/N).
Verified by a 1M probe during calibration (controller net +7.9 KB at 1M vs
+12.8 KB at 100K: one-time noise, not per-event).

Calibration (honest envelope, Golden Rule #10)
----------------------------------------------
Measured on the dev container (Linux, CPython 3.12.3, pytest 9, 2026-06-10),
two full-suite runs at N=100_000 (plus three prototype runs of the same
recipe); net/peak deltas were run-to-run byte-identical for tests 1-3 and
jittered ±15% for test 4:

* audit writer    — net −88 B,    peak +17.9 KB, ~20 s
* window ledger   — net −88 B,    peak  +1.7 KB,  ~2 s
* results store   — net −88 B,    peak  +4.1 KB, ~19 s
* rate controller — net +8-13 KB, peak +30-35 KB, ~3 s

(The small NEGATIVE nets are a few baseline-tracked blocks freed during the
sustained run — the leak signal of interest is positive growth.) Net ceilings
carry ≥20× headroom over measured (≥5000× for tests 1-3); the peak ceiling
≥30×. Detection floor at the 100K default: ~0.7 B/event for tests 1-3,
~2.7 B/event for test 4. Whole module ≈ 44 s at the default. Numbers WILL
differ on other platforms / CPython versions — recalibrate before tightening.
Shared-GHA wall time is plausibly 2-3× the container numbers; the per-push
budget cost is stated in MIGRATION.md entry #616, not hidden.

Budget / knob (``STRESS_MEMLEAK_EVENTS``)
-----------------------------------------
``_resolve_events``: an EXPLICIT ``STRESS_MEMLEAK_EVENTS`` wins (local
iteration / targeted soak); otherwise ``max(100_000, STRESS_EVENTS)``. So the
≥100K proof holds under every CI default (the nightly's ``STRESS_EVENTS=5000``
can never silently weaken it), and the existing ``workflow_dispatch``
``stress_events`` soak input scales this module up automatically — no workflow
change needed. A 10M soak puts the audit-writer test alone at ~25+ min under
tracemalloc plus a multi-GB JSONL: raise the stress workflow's
``timeout-minutes`` first (stated per Golden Rule #10).

Honest envelope — what this module does NOT prove
-------------------------------------------------
* Growth with DISTINCT keys: the ``WindowLedger`` / ``ResultsStore`` in-memory
  indexes are O(distinct keys) BY DESIGN (the store is sized for the funnel's
  10²-10⁴ finalist rows per its docstring). The leak claim covers the
  sustained steady-state path over a FIXED working set — the actual production
  re-pull / re-append pattern.
* Native-extension allocations invisible to tracemalloc (blake3, fastbloom,
  fno_math, trading_core) — only the generous RSS backstop partially covers
  these. The ``run_id``s here are sha256-derived 64-hex stand-ins satisfying
  the store's contract, NOT real BLAKE3 combo hashes (Golden Rule #1).
* The LMDB-backed ``DailyQuotaTracker`` — excluded via the documented
  ``quota_tracker=None`` bucket-only test mode; production wires one.
* O(rows) paths: ``ResultsStore.flush()``/``load()``, the orchestrator
  ``verify_chain`` and both modules' ``read_entries`` (the list contract),
  ``AuditWriter.open(resume=True)`` whole-file replay. (The sweep-side twin
  ``brutex.engine.sweep_audit.verify_chain`` now STREAMS at O(1) RAM —
  DEC-SWEEP-SCALE-OPERABILITY-001, proven by its unit-level comparative
  tracemalloc oracle — the orchestrator twin remains O(rows).)
* fd lifecycle across ``AuditWriter`` reopen/close cycles (one writer held
  open here); chain INTEGRITY at scale (that is
  ``test_audit_chain_sustained.py``'s invariant).
* Multi-threaded contention growth — all four tests are single-threaded so the
  tracemalloc measurement is deterministic; the controller/ledger concurrency
  contracts are covered by their own unit/integration suites.
* Production structlog sink growth: these tests rely on structlog's default
  PrintLogger (stdout → pytest fd-capture → disk temp file; measured +32 B
  for 8K emissions); no test in this repo mutates the global structlog config
  (only scoped ``capture_logs`` context managers elsewhere).

Environment constraints (measured, 4-agent hostile review)
-----------------------------------------------------------
* The measured region must emit ZERO stdlib ``logging`` records ≥ WARNING:
  pytest's logging plugin retains ~1,005 B/record in every capture mode
  (measured: 8K records → +8.0 MB net). The controller's per-429/timeout
  structlog warnings go through the default PrintLogger (NOT stdlib), which
  measured clean — the constraint holds today and is asserted implicitly by
  the net ceiling itself.
* The gates assume pytest's default fd capture (what CI uses). Under
  ``--capture=sys`` every captured output line becomes tracked heap
  (measured: 8K lines → +712 KB) — an environment artifact, not a leak.
* The recipe was leak-validated: an injected 95 B/event leak measured
  +9.50 MB at 100K events — exactly linear, far beyond every ceiling.
"""

from __future__ import annotations

import gc
import hashlib
import os
import tracemalloc
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Final

import pytest
from brutex.adapters.rate_common import (
    CEILING_RPS,
    FLOOR_RPS,
    STEP_DOWN_WINDOW,
    TIMEOUT_FLOOR_RPS,
    RateController,
)
from brutex.data.fetch_coverage import WindowKey
from brutex.data.window_ledger import EMPTY, FAILED, WindowLedger
from brutex.engine.results_store import ResultRow, ResultsStore
from brutex.observability.process_probe import read_rss_kib_current
from brutex.orchestrator.audit import AuditWriter

#: All stress tests carry the ``stress`` marker (registered in pyproject.toml) so
#: the nightly ``pytest tests/stress -m stress`` soak selects them (DEC-STRESS-001).
pytestmark = pytest.mark.stress


def _resolve_events() -> int:
    """Resolve the sustained-event count (see "Budget / knob" above).

    An explicit ``STRESS_MEMLEAK_EVENTS`` wins; otherwise the floor is the
    ≥100K proof, scaled up (never down) by the suite-wide ``STRESS_EVENTS``
    soak knob — so no CI default can silently weaken the gate.
    """
    explicit = os.environ.get("STRESS_MEMLEAK_EVENTS")
    if explicit is not None:
        return int(explicit)
    return max(100_000, int(os.environ.get("STRESS_EVENTS", "0")))


#: Sustained events per test (default 100_000 — the ≥100K proof of
#: DEC-MEMLEAK-GATE-001).
_EVENTS: Final[int] = _resolve_events()

#: Untracked + tracked warmup sizes (events). Untracked absorbs one-time
#: allocations; tracked turns the working set over at least once under tracing
#: (recipe steps 1-2). 2_000 covers every working set below multiple times.
_WARMUP: Final[int] = 2_000

#: Net traced-growth ceilings (bytes) — measured value × honest headroom (see
#: "Calibration" above). The controller's ceiling is wider: its measured net is
#: ~8-13 KB of N-independent one-time noise (structlog/deque block effects).
_NET_CEILING_FLAT: Final[int] = 64 * 1024
_NET_CEILING_CONTROLLER: Final[int] = 256 * 1024

#: Peak traced-delta ceiling (bytes) — bounds the TRANSIENT footprint of the
#: measured region (largest measured peak: controller ~35 KB → ~30× headroom).
_PEAK_CEILING: Final[int] = 1024 * 1024

#: RSS backstop (KiB growth) — deliberately generous; catches only a
#: catastrophic native-side leak that tracemalloc cannot see (measured RSS
#: deltas were 0-4 KiB). NOT a tight gate, per the PR-#99 flake history.
_RSS_BACKSTOP_KIB: Final[int] = 192 * 1024


@dataclass(frozen=True, slots=True)
class _MemoryGrowth:
    """One measured region's memory outcome (recipe steps 2-5).

    Attributes
    ----------
    net_bytes
        Traced Python-heap growth across the sustained phase, after
        ``gc.collect()`` — the leak signal.
    peak_bytes
        Traced peak delta above the baseline — the transient-footprint bound.
    rss_delta_kib
        Current-``VmRSS`` growth in KiB across the whole measurement, or
        ``None`` off-Linux (observability + generous native backstop only).
    top_allocs
        Populated ONLY when the net ceiling was breached: the top allocation
        diffs vs the post-baseline snapshot, so a red gate is an actionable
        leak report (file:line attribution), not just a number.
    """

    net_bytes: int
    peak_bytes: int
    rss_delta_kib: int | None
    top_allocs: str | None


def _read_rss_kib() -> int | None:
    """Return the CURRENT process RSS in KiB — delegates to the ONE COMMON probe.

    Thin alias for :func:`brutex.observability.process_probe.read_rss_kib_current`
    (DEC-SOAK-REAL-TIER-001, Golden Rule #11 — one RSS source of truth). The
    backstop below is a before/after growth DELTA, so this deliberately takes
    the SLOPE-SAFE reader: Linux reads current ``VmRSS`` (a genuine slope);
    off-Linux (the operator's macOS reference machine, CLAUDE.md §8) it returns
    ``None`` and the backstop is SKIPPED — never a delta of the ``ru_maxrss``
    process-lifetime PEAK, which process_probe's contract forbids using as a
    slope (a large transient allocation raises the peak where current-RSS-
    after-gc would not — the PR-#99 objection class this file's docstring
    records). The macOS resource-ceiling signal lives in the soak tier's
    ``SoakDriftMonitor`` RSS-CEILING rule instead. Callers here treat ``None``
    as "skip the backstop, keep the tracemalloc gates".
    """
    return read_rss_kib_current()


def _measure_growth(
    tracked_warmup: Callable[[], None],
    sustained: Callable[[], None],
    *,
    net_ceiling: int,
) -> _MemoryGrowth:
    """Run the measurement recipe and return the region's memory outcome.

    Parameters
    ----------
    tracked_warmup
        Turns the steady-state working set over at least once UNDER tracing
        (recipe step 2) so sustained replacements are tracked-for-tracked.
    sustained
        The ≥100K-event sustained phase being gated (recipe step 4).
    net_ceiling
        The net-growth ceiling (bytes). On a breach, a tracemalloc snapshot
        diff (top 10 by lineno) is captured for the failure report — outside
        the measured region, so the diagnostics never pollute the numbers.
    """
    rss_before = _read_rss_kib()
    started_here = not tracemalloc.is_tracing()
    if started_here:
        tracemalloc.start()  # default nframe=1 — deeper frames cost 3x wall time
    try:
        tracked_warmup()
        gc.collect()
        # Snapshot BEFORE the baseline read so the retained Snapshot object's
        # own allocation sits inside the baseline and cancels out of net.
        snap_before = tracemalloc.take_snapshot()
        baseline, _ = tracemalloc.get_traced_memory()
        tracemalloc.reset_peak()
        sustained()
        gc.collect()
        current, peak = tracemalloc.get_traced_memory()
        top_allocs: str | None = None
        if current - baseline > net_ceiling:
            # Red gate → actionable leak report (DEC-MEMLEAK-GATE-001:
            # STOP and report with reproduction, never just a number).
            diff = tracemalloc.take_snapshot().compare_to(snap_before, "lineno")
            top_allocs = "\n".join(str(stat) for stat in diff[:10])
    finally:
        if started_here:
            tracemalloc.stop()
    rss_after = _read_rss_kib()
    rss_delta = None if rss_before is None or rss_after is None else rss_after - rss_before
    return _MemoryGrowth(
        net_bytes=current - baseline,
        peak_bytes=peak - baseline,
        rss_delta_kib=rss_delta,
        top_allocs=top_allocs,
    )


def _assert_bounded(label: str, growth: _MemoryGrowth, *, net_ceiling: int) -> None:
    """Assert the measured region stayed inside its ceilings; print the numbers.

    The print (allowed in tests — visibility, like the smoke suite) puts the
    measured values in the nightly log even on success, so ceiling drift is
    observable before it becomes a failure.
    """
    print(
        f"memleak[{label}] events={_EVENTS} net={growth.net_bytes:+d}B "
        f"peak={growth.peak_bytes:+d}B rss_delta_kib={growth.rss_delta_kib}"
    )
    assert growth.net_bytes <= net_ceiling, (
        f"{label}: net traced growth {growth.net_bytes}B exceeds the {net_ceiling}B "
        f"ceiling over {_EVENTS} sustained events — unbounded memory growth "
        f"(a real leak): STOP and report per DEC-MEMLEAK-GATE-001; do not retune "
        f"the ceiling without a calibration run.\n"
        f"Top allocation diffs (sustained vs baseline):\n{growth.top_allocs}"
    )
    assert growth.peak_bytes <= _PEAK_CEILING, (
        f"{label}: peak traced delta {growth.peak_bytes}B exceeds the "
        f"{_PEAK_CEILING}B transient-footprint ceiling"
    )
    if growth.rss_delta_kib is not None:
        assert growth.rss_delta_kib <= _RSS_BACKSTOP_KIB, (
            f"{label}: RSS grew {growth.rss_delta_kib} KiB — beyond the generous "
            f"native-leak backstop ({_RSS_BACKSTOP_KIB} KiB); a native-side leak "
            f"tracemalloc cannot attribute"
        )


# ────────────────────────────────────────────────────────────────────────
# 1. Audit-chain writer
# ────────────────────────────────────────────────────────────────────────


def test_audit_writer_memory_flat(tmp_path: Path) -> None:
    """≥100K sustained ``AuditWriter.append`` calls hold O(1) writer memory.

    The log FILE grows by design (one JSONL line per entry, flushed each
    append); the WRITER may persist only the rolling ``seq`` + ``prev_hash``.
    Chain INTEGRITY at scale is ``test_audit_chain_sustained.py``'s invariant
    — deliberately not re-verified here (replay is O(N) and would sit inside
    the measurement window).
    """
    log_path = tmp_path / "memleak_audit.jsonl"
    appended = 0

    with AuditWriter.open(log_path) as log:

        def burst(events: int) -> None:
            """Append *events* entries with rotating subjects + small payloads."""
            nonlocal appended
            for _ in range(events):
                log.append(
                    action="MEMLEAK_BURST",
                    subject=f"event:{appended % 1024:08d}",
                    payload={"seq": appended, "kind": "memleak", "bucket": appended % 64},
                )
                appended += 1

        burst(_WARMUP)
        growth = _measure_growth(
            lambda: burst(_WARMUP), lambda: burst(_EVENTS), net_ceiling=_NET_CEILING_FLAT
        )
        # The writer must still be live + correctly chained after the burst:
        # the next entry's seq is exactly the number of entries appended.
        tail = log.append(action="MEMLEAK_DONE", subject="memleak:tail", payload={})
        assert tail.seq == appended

    _assert_bounded("audit-writer", growth, net_ceiling=_NET_CEILING_FLAT)


# ────────────────────────────────────────────────────────────────────────
# 2. WindowLedger
# ────────────────────────────────────────────────────────────────────────

#: Fixed window universe — the steady-state working set a re-pull cycles over,
#: partitioned production-faithfully: settled-``SUCCESS`` rows, settled
#: ``DH-905``-gap rows (``SKIPPED``), and perpetual-churn rows that took two
#: clean empties (``empty_streak == 2``, one short of the threshold-3 promotion)
#: and then keep TIMING OUT — every later outcome is a ``FAILED``, which under
#: DEC-EMPTY-STREAK-RATELIMIT-001 PRESERVES the streak (a timeout is not evidence
#: of data) WITHOUT ever incrementing it to 3, so the EMPTY→SKIPPED promotion
#: never fires — exactly the windows production re-fetches every pull.
_WINDOW_UNIVERSE: Final[int] = 512
_SETTLED_SUCCESS: Final[int] = 128
_SETTLED_GAP: Final[int] = 64
_CHURN: Final[int] = _WINDOW_UNIVERSE - _SETTLED_SUCCESS - _SETTLED_GAP


def _window_keys() -> list[WindowKey]:
    """Build the fixed 512-window universe (distinct by ``security_id``)."""
    return [
        WindowKey(
            market="dhan",
            security_id=f"SYM{i:04d}",
            interval="1",
            start=date(2026, 1, 1),
            end=date(2026, 3, 31),
        )
        for i in range(_WINDOW_UNIVERSE)
    ]


def test_window_ledger_memory_flat() -> None:
    """≥100K sustained probe-first pull-cycle ops over a FIXED universe stay flat.

    Mirrors the production re-pull loop (DEC-WINDOW-LEDGER-001): every cycle
    ``plan``s all 512 windows (idempotent no-op on existing rows), probes
    ``is_skippable`` on all 512, and ``record``s ONLY the non-settled churn
    windows — settled rows are never re-recorded (production never fetches
    them, so it can never un-settle them). Every recorded outcome REPLACES the
    window's row; the dict must end exactly at 512 entries.
    """
    keys = _window_keys()
    success_keys = keys[:_SETTLED_SUCCESS]
    gap_keys = keys[_SETTLED_SUCCESS : _SETTLED_SUCCESS + _SETTLED_GAP]
    churn_keys = keys[_SETTLED_SUCCESS + _SETTLED_GAP :]
    assert len(churn_keys) == _CHURN
    now = datetime(2026, 6, 10, 9, 15, 0)
    ledger = WindowLedger()

    # Pull 0 (cold start): plan everything, then settle the settled partitions
    # the way production does — one SUCCESS fetch / one DH-905 settle each.
    for key in keys:
        ledger.plan(key, now=now)
    for key in success_keys:
        ledger.record(key, raw_bars=375, error_type=None, bars_appended=375, now=now)
    for key in gap_keys:
        ledger.settle_known_gap(key, now=now, reason="DH-905 no data in range")
    # Seed each churn window with two CLEAN empties (streak 2, one short of the
    # threshold-3 promotion); the pull loop below then keeps them TIMING OUT, and
    # a FAILED preserves the streak at 2 forever without ever settling.
    for key in churn_keys:
        assert ledger.record(key, raw_bars=0, error_type=None, bars_appended=0, now=now) == EMPTY
        assert ledger.record(key, raw_bars=0, error_type=None, bars_appended=0, now=now) == EMPTY

    ops = 0

    def pull_cycles(min_ops: int) -> None:
        """Run whole probe-first pull cycles until *min_ops* more ledger ops ran."""
        nonlocal ops
        target = ops + min_ops
        while ops < target:
            for key in keys:
                ledger.plan(key, now=now)  # production upsert — no-op on existing
                ops += 1
            for key in keys:
                skippable = ledger.is_skippable(key)
                ops += 1
                if skippable:
                    continue  # production never fetches a settled window
                # Perpetual timeout: a FAILED PRESERVES the streak (frozen at 2)
                # without ever reaching 3 — the window never settles, never skips
                # (DEC-EMPTY-STREAK-RATELIMIT-001).
                status = ledger.record(
                    key, raw_bars=0, error_type="DhanTimeoutError", bars_appended=0, now=now
                )
                assert status == FAILED, "churn window must never settle"
                ops += 1

    pull_cycles(_WARMUP)
    growth = _measure_growth(
        lambda: pull_cycles(_WARMUP),
        lambda: pull_cycles(_EVENTS),
        net_ceiling=_NET_CEILING_FLAT,
    )

    # Functional end state (post-measurement — records() is an O(N) snapshot).
    assert ledger.count == _WINDOW_UNIVERSE, (
        f"window universe grew: {ledger.count} != {_WINDOW_UNIVERSE} — "
        f"a re-record inserted instead of replacing"
    )
    assert all(ledger.is_skippable(key) for key in success_keys)
    assert all(ledger.is_skippable(key) for key in gap_keys)
    assert not any(ledger.is_skippable(key) for key in churn_keys)
    for row in ledger.records():
        assert row.empty_streak <= 2, "churn cycle must keep every streak below promotion"
    _assert_bounded("window-ledger", growth, net_ceiling=_NET_CEILING_FLAT)


# ────────────────────────────────────────────────────────────────────────
# 3. Sweep results-store append
# ────────────────────────────────────────────────────────────────────────

#: Fixed result working set — the funnel's finalist rows (store is sized for
#: 10²-10⁴ per its docstring). Distinct-row growth is by design; the leak gate
#: is the idempotent re-append path including full row reconstruction.
_RESULT_ROWS: Final[int] = 256


def _result_row(i: int) -> ResultRow:
    """Build the i-th deterministic, validation-passing ``ResultRow``.

    Every field is a pure function of *i*, and ``__post_init__`` canonicalises
    signed zero + snaps prices — so reconstructing the SAME *i* compares equal
    to the stored row (the idempotent no-op append path). The ``run_id`` is a
    sha256-derived 64-lowercase-hex stand-in satisfying the store's contract,
    NOT a real BLAKE3 combo hash (Golden Rule #1 — see the module docstring).
    """
    run_id = hashlib.sha256(f"memleak-row-{i}".encode()).hexdigest()
    return ResultRow(
        run_id=run_id,
        instrument=("NIFTY", "BANKNIFTY", "SENSEX")[i % 3],
        tf=("1m", "5m", "15m")[(i // 3) % 3],
        direction="bull" if i % 2 == 0 else "bear",
        mode="A",
        mask_bits=(1 << (i % 52)) | (1 << ((i * 7) % 52)),
        net_pnl_paisa=1_000 * i - 50_000,
        gross_profit_paisa=2_000 * i,
        gross_loss_paisa=1_000 * i,
        max_drawdown_paisa=500 * i,
        expectancy_paisa=10 * i - 1_000,
        total_trades=100 + i,
        winning_trades=60,
        losing_trades=40,
        sharpe_ratio=1.0 + i / 256,
        sortino_ratio=1.5 + i / 256,
        calmar_ratio=0.5,
        profit_factor=1.8,
        win_rate=0.6,
        max_drawdown_pct=12.5,
        avg_entry_price=Decimal("23001.55"),
        avg_exit_price=Decimal("23042.10"),
        deflated_sharpe=None if i % 4 == 0 else 0.9 + i / 1024,
    )


def test_results_store_append_memory_flat(tmp_path: Path) -> None:
    """≥100K idempotent re-appends (with full row reconstruction) stay flat.

    Each event RE-CONSTRUCTS an identical ``ResultRow`` (validation +
    ``condition_names`` derivation included — the realistic per-batch hot
    path) and appends it; every append must be the documented silent no-op
    (returns ``False``), and the store must end exactly at 256 rows.
    ``flush()``/``load()`` (O(rows) serialisation; the lazy polars import)
    are out of scope — nothing here needs the ``[data]`` extra.
    """
    store = ResultsStore(path=tmp_path / "memleak_results.parquet")
    for i in range(_RESULT_ROWS):
        # Side-effecting call OUTSIDE the assert (py/side-effect-in-assert):
        # the seed insert is load-bearing and must survive `python -O`.
        seeded = store.append(_result_row(i))
        assert seeded, "seed insert must be new"
    tick = 0

    def pump(events: int) -> None:
        """Re-append *events* reconstructed-identical rows over the fixed set."""
        nonlocal tick
        for _ in range(events):
            row = _result_row(tick % _RESULT_ROWS)
            added = store.append(row)  # the hot-path call itself — never in assert
            assert added is False, "re-append must be the idempotent no-op"
            tick += 1

    pump(_WARMUP)
    growth = _measure_growth(
        lambda: pump(_WARMUP), lambda: pump(_EVENTS), net_ceiling=_NET_CEILING_FLAT
    )

    assert len(store) == _RESULT_ROWS, f"results index grew: {len(store)} != {_RESULT_ROWS}"
    _assert_bounded("results-store", growth, net_ceiling=_NET_CEILING_FLAT)


# ────────────────────────────────────────────────────────────────────────
# 4. Dhan rate controller
# ────────────────────────────────────────────────────────────────────────

#: Event-schedule block: 100 sustained 429s (drives the two-tier ladder 5→4→3
#: on STEP_DOWN_HITS, then the STORM_HITS rungs 3→2→1 toward the deep floor,
#: then pins at the floor — the deque-accumulation worst case, where the
#: step-down branch can no longer clear the window), 10 timeout stalls (the
#: at-floor no-step path), then successes long enough for the STEP_UP_WINDOW
#: ladder to recover 1→2→3→4→5 (~3_000 events at the stepped rates, leaving
#: ~890 events of ceiling headroom) — every ladder transition is exercised
#: every block, and all four log templates (429/timeout/stepdown/stepup) are
#: warmed before the baseline because both warmup phases each run a full block.
_BLOCK: Final[int] = 4_000

#: Bound on the rolling-window deques: physically ≤ window × ceiling rate
#: (60 s × 5 r/s = 300) — the fake clock advances exactly ``1/current_rps``
#: per pace (burst=1, start_empty bucket), modelling the real ≤5 r/s stream —
#: plus slack for boundary effects.
_RECENT_BOUND: Final[int] = int(STEP_DOWN_WINDOW * CEILING_RPS) + 30


class _FakeClock:
    """Deterministic monotonic clock whose ``sleep`` advances it.

    Contract (pinned by the bucket's ``acquire`` loop): ``now`` is a PURE read
    — it never advances time, because ``acquire``/the ladder hooks read the
    clock several times per event and an auto-advancing read would distort the
    60 s eviction windows. Only ``sleep`` advances. ``acquire`` computes the
    one-token wait then sleeps exactly that long, so every ``pace()``
    terminates after one refill (epsilon drift is absorbed by the bucket's
    ``_ACQUIRE_LOOP_CAP`` backstop) and simulated time advances by exactly
    ``1/current_rps`` per event — the physical ≤5 r/s stream.
    """

    def __init__(self) -> None:
        self.now_s = 0.0

    def now(self) -> float:
        """Return the current simulated monotonic time (seconds)."""
        return self.now_s

    def sleep(self, seconds: float) -> None:
        """Advance simulated time by *seconds* (never really sleeps)."""
        assert seconds >= 0.0, "the bucket must never request a negative sleep"
        self.now_s += seconds


def test_rate_controller_memory_flat() -> None:
    """≥100K sustained pace+outcome events across full ladder cycles stay flat.

    ``quota_tracker=None`` is the documented bucket-only test mode (no LMDB).
    Single-threaded by design — tracemalloc needs deterministic measurement;
    the controller's concurrency contract is covered by its own suites. The
    in-loop assertions are deterministic and falsifiable: every block must
    recover to the EXACT ceiling (the steps are exact ±1.0 floats), the storm
    must reach at least the soft floor, and the ``snapshot`` deques must stay
    inside the physical window×rate bound even while pinned at the floor.
    """
    clock = _FakeClock()
    controller = RateController(quota_tracker=None, clock=clock.now, sleep=clock.sleep)
    tick = 0

    def drive(events: int) -> None:
        """Drive *events* pace+outcome cycles through the block schedule."""
        nonlocal tick
        for _ in range(events):
            phase = tick % _BLOCK
            if phase == 0 and tick > 0:
                # Every block's clean tail must recover the ladder to the
                # exact ceiling — the deterministic recovery proof.
                assert controller.current_rps == CEILING_RPS
            controller.pace()
            if phase < 100:
                controller.on_rate_limit()
            elif phase < 110:
                controller.on_timeout()
            else:
                controller.on_success()
            if phase == 110:
                # The 429 storm must have eased at least to the soft floor
                # (empirically it reaches the deep storm floor 1.0).
                assert controller.current_rps <= FLOOR_RPS
            if phase % 1_000 == 0:
                snap = controller.snapshot()
                hits = snap["recent_hits"]
                stalls = snap["recent_timeouts"]
                assert isinstance(hits, int) and hits <= _RECENT_BOUND
                assert isinstance(stalls, int) and stalls <= _RECENT_BOUND
            tick += 1

    drive(_BLOCK)
    growth = _measure_growth(
        lambda: drive(_BLOCK), lambda: drive(_EVENTS), net_ceiling=_NET_CEILING_CONTROLLER
    )

    # The ladder must end inside its envelope, and the fake-clock contract
    # must have held: each pace advances 1/current_rps, so total simulated
    # time is bracketed by the ceiling/deep-floor rates (small slack for the
    # bucket's epsilon re-waits).
    assert TIMEOUT_FLOOR_RPS <= controller.current_rps <= CEILING_RPS
    assert tick / CEILING_RPS <= clock.now_s <= (tick / TIMEOUT_FLOOR_RPS) * 1.01
    _assert_bounded("rate-controller", growth, net_ceiling=_NET_CEILING_CONTROLLER)
