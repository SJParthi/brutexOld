"""Suffix-independence property gates for the brute-force SWEEP spine.

DEC-SWEEP-LOOKAHEAD-GUARD-001 — the permanent CI teeth behind the sweep
path's no-look-ahead claim (CLAUDE.md §4). Two audit agents verified the
spine is structurally causal TODAY (Stage-1A updates prior-bar history only
AFTER evaluation; the trade sim enters at the NEXT bar's open); these
properties pin that causality forever, so a FUTURE look-ahead regression
anywhere in the spine fails CI:

* **Stage-1A builder state** — ``build_bar_bits`` (and the resident-kernel
  path that wraps it) is *suffix-independent*: the truth-matrix over
  ``bars[0..k]`` is bit-identical whether ``bars[k+1..]`` are ABSENT
  (prefix-truncation form — the streaming state must behave identically
  when the stream simply ends) or MUTATED to arbitrary / extreme prices
  (perturbation form).
* **The resample path into the engine** — mutating a suffix DAY of the 1m
  feed never changes an earlier derived bucket, on BOTH resamplers the
  bars can reach the engine through: the epoch-anchored
  :func:`brutex.feed.resampler.resample_bars` and the canonical
  session-anchored :func:`brutex.data.tf_resampler.resample_1m_to_tf`;
  plus the appended-suffix form (every fully-closed prefix bucket is
  unchanged when later bars arrive).
* **Trade-sim exits through the FULL spine** — extending the walker-local
  property in ``tests/property/test_engine_trade_sim_properties.py``
  (``trade_sim.py`` §"No look-ahead"): running bars → ``build_bar_bits`` →
  ``mask_to_entry_signals`` → ``simulate_trades`` end-to-end, a trade
  closed at bar ``K`` is bit-identical when every bar STRICTLY AFTER ``K``
  is mutated — the mutation flows through the *recomputed* bar bits and
  entry signals, so this covers the whole derived-state chain, not just
  the walker.
* **Engine statelessness** — ``metrics_for_mask`` and a full
  ``run_persisted_sweep`` over a PREFIX window are bit-identical
  (``store_content_hash`` and all) whether or not a full-series sweep ran
  first in the same process: no global mutable state can smuggle future
  information into a prefix evaluation.

Honest envelope (Golden Rule #10): these gates prove *suffix independence*
(outputs attributable to bars ``0..k`` never depend on bars ``k+1..``) —
the precise falsifiable form of "no look-ahead" for a batch spine. They do
not, and cannot, prove anything about data the spine was never given.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.data.models import CandleBar
from brutex.data.option_dedup import OptionDedupIndex
from brutex.engine.combo_mask import mask_to_entry_signals
from brutex.engine.results_store import ResultsStore
from brutex.engine.stage1a import build_bar_bits
from brutex.engine.sweep_driver import DEFAULT_START_EQUITY_PAISA, metrics_for_mask
from brutex.engine.sweep_kernel import ResidentSweepKernel
from brutex.engine.sweep_run import ComboDedup, SweepRunConfig, run_persisted_sweep
from brutex.engine.trade_sim import simulate_trades
from brutex.feed.resampler import resample_bars
from brutex.orchestrator.audit import AuditWriter
from brutex.strategies.types import Side
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# pyright: reportCallIssue=false
# Rationale (DEC-PYRIGHT-UNMASK-001): the Hypothesis `@st.composite` strategies
# below are invoked with no args (the decorator injects `draw` at runtime);
# pyright does not model the `@composite` signature rewrite. Scoped to this
# file only — same convention as tests/property/test_engine_stage1a_properties.py.

_IST = timezone(timedelta(hours=5, minutes=30))

# 09:16 IST session bar anchor (matches the sweep_run property suite).
_SESSION_OPEN_IST = datetime(2026, 1, 5, 9, 16, tzinfo=_IST)

#: Integer-rupee prices INCLUDING the extreme edges (₹1 and ₹1,000,000) with
#: extra weight, so the "extreme suffix" arm of the decision is genuinely
#: exercised, not just reachable by shrinking.
_price = st.one_of(
    st.just(1),
    st.just(1_000_000),
    st.integers(min_value=1, max_value=1_000_000),
)

_BARS_PER_DAY = 15


def _bar_at(day: int, minute: int, open_i: int, close_i: int) -> CandleBar:
    """One valid 1-minute IST session bar (stored UTC), high/low spanning O/C."""
    ts = (_SESSION_OPEN_IST + timedelta(days=day, minutes=minute)).astimezone(UTC)
    return CandleBar(
        timestamp=ts,
        open=Decimal(open_i),
        high=Decimal(max(open_i, close_i)),
        low=Decimal(min(open_i, close_i)),
        close=Decimal(close_i),
        volume=100,
        open_interest=None,
    )


def _series_from_pairs(pairs: list[tuple[int, int]]) -> list[CandleBar]:
    """Chronological multi-day series: `_BARS_PER_DAY` bars per IST day."""
    return [
        _bar_at(i // _BARS_PER_DAY, i % _BARS_PER_DAY, open_i, close_i)
        for i, (open_i, close_i) in enumerate(pairs)
    ]


@st.composite
def _bar_series(draw: st.DrawFn, *, min_size: int = 2, max_size: int = 45) -> list[CandleBar]:
    """Draw a chronological multi-day bar series (spans 1-3 IST days)."""
    n = draw(st.integers(min_value=min_size, max_value=max_size))
    pairs = draw(st.lists(st.tuples(_price, _price), min_size=n, max_size=n))
    return _series_from_pairs(pairs)


def _tf_frame(series: list[CandleBar]):  # type: ignore[no-untyped-def]
    """Build the naive-UTC 1m polars frame ``resample_1m_to_tf`` consumes.

    Module-level (not per-test) so every tf-resampler gate feeds the deriver
    through ONE identical frame construction. Imports polars lazily so the
    ``.[dev]``-only job (no ``[data]`` extra) can still collect this module.
    """
    import polars as pl

    return pl.DataFrame(
        {
            "ts_utc": [b.timestamp.replace(tzinfo=None) for b in series],
            "open": [float(b.open) for b in series],
            "high": [float(b.high) for b in series],
            "low": [float(b.low) for b in series],
            "close": [float(b.close) for b in series],
            "volume": [b.volume for b in series],
        },
        schema={
            "ts_utc": pl.Datetime,
            "open": pl.Float64,
            "high": pl.Float64,
            "low": pl.Float64,
            "close": pl.Float64,
            "volume": pl.Int64,
        },
    )


def _mutate_suffix(bars: list[CandleBar], start: int, data: st.DataObject) -> list[CandleBar]:
    """Replace every bar at index >= *start* with fresh (extreme-capable) prices.

    Timestamps are preserved (the day/bucket structure is unchanged); prices —
    the information content — are redrawn from the extreme-weighted strategy.
    """
    mutated = list(bars)
    for j in range(start, len(bars)):
        open_i = data.draw(_price)
        close_i = data.draw(_price)
        mutated[j] = CandleBar(
            timestamp=bars[j].timestamp,
            open=Decimal(open_i),
            high=Decimal(max(open_i, close_i)),
            low=Decimal(min(open_i, close_i)),
            close=Decimal(close_i),
            volume=bars[j].volume,
            open_interest=None,
        )
    return mutated


# ────────────────────────────────────────────────────────────────────────
# Stage-1A builder state — suffix ABSENT (prefix truncation) + MUTATED
# ────────────────────────────────────────────────────────────────────────


@given(bars=_bar_series(), data=st.data())
@settings(suppress_health_check=[HealthCheck.too_slow], deadline=None)
def test_stage1a_prefix_projection_suffix_absent(
    bars: list[CandleBar], data: st.DataObject
) -> None:
    """`build_bar_bits(bars)[: k+1]` == `build_bar_bits(bars[: k+1])` — bit-identical.

    The ABSENT-suffix form: the builder's streaming state at bar ``k`` must be
    byte-for-byte the same whether the stream continues or simply ends, so the
    prefix of the full matrix equals the matrix of the prefix.
    """
    k = data.draw(st.integers(min_value=0, max_value=len(bars) - 1))
    assert build_bar_bits(bars)[: k + 1] == build_bar_bits(bars[: k + 1])


@given(bars=_bar_series(min_size=2), data=st.data())
@settings(suppress_health_check=[HealthCheck.too_slow], deadline=None)
def test_kernel_bar_bits_immune_to_extreme_suffix_mutation(
    bars: list[CandleBar], data: st.DataObject
) -> None:
    """The resident-kernel Stage-1A path is suffix-mutation independent.

    The kernel arm (`ResidentSweepKernel.bar_bits`) is the one the grid
    orchestrator actually reuses across tuples — pin it directly, with the
    suffix redrawn from the extreme-weighted price strategy.
    """
    k = data.draw(st.integers(min_value=0, max_value=len(bars) - 2))
    mutated = _mutate_suffix(bars, k + 1, data)
    base_bits = ResidentSweepKernel(bars).bar_bits
    mutated_bits = ResidentSweepKernel(mutated).bar_bits
    assert base_bits[: k + 1] == mutated_bits[: k + 1]


def test_gap_mid_bits_fire_within_the_property_series_shape() -> None:
    """Non-vacuity teeth for bits 60-62 (DEC-GAP-MIDPOINT-CONDITIONS-001).

    The suffix-independence gates above compare FULL u128 truth-vectors, so
    they cover the Group-13 gap-midpoint bits automatically — PROVIDED the
    generator's series shape can actually fire them. This deterministic case
    is built through the SAME ``_series_from_pairs`` shape the
    ``_bar_series`` strategy draws (multi-day, ``_BARS_PER_DAY`` bars per IST
    day, high/low spanning open/close): day 2 gaps up (open 150 > pdc 102 ⇒
    level = (X1_hi 10200 + X2_hi 15100) / 2 = ₹126.50) and its later closes
    sit strictly above — bit 60 fires, so the property comparisons are
    provably NON-VACUOUS over the gap trio (any draw whose series spans two
    days with a nonzero gap exercises them the same way).
    """
    from brutex.vocabulary import bits as B

    gap_trio = (
        (1 << B.BIT_CLOSE_ABOVE_GAP_MID)
        | (1 << B.BIT_CLOSE_BELOW_GAP_MID)
        | (1 << B.BIT_NEAR_GAP_MID)
    )
    pairs = [(100, 101)] * (_BARS_PER_DAY - 1) + [(101, 102)]  # day 1; X1 = (102, 101)
    pairs += [(150, 151)] + [(160, 161)] * (_BARS_PER_DAY - 1)  # day 2: gap-up
    bits = build_bar_bits(_series_from_pairs(pairs))
    fired = [i for i, m in enumerate(bits) if m & gap_trio]
    assert fired  # the property-series shape genuinely reaches bits 60-62
    assert min(fired) >= _BARS_PER_DAY + 1  # …and only from day 2's SECOND bar


# ────────────────────────────────────────────────────────────────────────
# Stage-1A futures arm — the STITCHED roll-aware stream is suffix-independent
# (ADR-0012 / DEC-STAGE1A-ROLL-RESET-001, extending DEC-SWEEP-LOOKAHEAD-GUARD-001
# to the roll machinery: neither the monotone roll cursor nor the per-contract
# hard reset may consult a bar past the consumption front)
# ────────────────────────────────────────────────────────────────────────


@st.composite
def _futures_stream(draw: st.DrawFn) -> tuple[list[CandleBar], tuple[int, ...], list[int]]:
    """Draw (stitched multi-contract bars, roll_schedule, boundary indices).

    2-3 per-contract segments over consecutive IST dates (1-2 dates × 2-6
    volume-bearing bars each); the schedule entry for each segment is its
    LAST date's ordinal (the resolver's ``(E_{k-1}, E_k]`` ownership law), so
    every segment boundary is a genuine roll. The returned boundary indices
    (index of each segment's first bar, beyond bar 0) let the cut draw be
    boundary-adjacent-weighted.
    """
    n_segments = draw(st.integers(min_value=2, max_value=3))
    day_cursor = 0
    bars: list[CandleBar] = []
    schedule: list[int] = []
    boundaries: list[int] = []
    for seg_index in range(n_segments):
        if seg_index > 0:
            boundaries.append(len(bars))
        n_days = draw(st.integers(min_value=1, max_value=2))
        for _ in range(n_days):
            day_offset = day_cursor
            day_cursor += 1
            n_bars = draw(st.integers(min_value=2, max_value=6))
            for minute in range(n_bars):
                open_i = draw(_price)
                close_i = draw(_price)
                ts = _SESSION_OPEN_IST + timedelta(days=day_offset, minutes=minute)
                bars.append(
                    CandleBar(
                        timestamp=ts.astimezone(UTC),
                        open=Decimal(open_i),
                        high=Decimal(max(open_i, close_i)),
                        low=Decimal(min(open_i, close_i)),
                        close=Decimal(close_i),
                        volume=100,
                        open_interest=None,
                    )
                )
        schedule.append((_SESSION_OPEN_IST + timedelta(days=day_cursor - 1)).date().toordinal())
    return bars, tuple(schedule), boundaries


@given(stream=_futures_stream(), data=st.data())
@settings(suppress_health_check=[HealthCheck.too_slow], deadline=None)
def test_stage1a_futures_prefix_projection_suffix_absent(
    stream: tuple[list[CandleBar], tuple[int, ...], list[int]], data: st.DataObject
) -> None:
    """`build_bar_bits(bars, roll_schedule=S)[: k+1] == build_bar_bits(bars[: k+1], roll_schedule=S)`.

    The ABSENT-suffix form over the STITCHED futures stream, with the cut ``k``
    drawn boundary-adjacent-weighted (the bar before a roll, the roll bar
    itself, the bar after) so the reset/cursor edges are genuinely exercised —
    the roll machinery's state at bar ``k`` must be byte-identical whether the
    stream continues across the next roll or simply ends.
    """
    bars, schedule, boundaries = stream
    boundary_adjacent = sorted(
        {min(max(b + delta, 0), len(bars) - 1) for b in boundaries for delta in (-1, 0, 1)}
    )
    k = data.draw(
        st.one_of(
            st.sampled_from(boundary_adjacent),
            st.integers(min_value=0, max_value=len(bars) - 1),
        )
    )
    full = build_bar_bits(bars, roll_schedule=schedule)
    prefix = build_bar_bits(bars[: k + 1], roll_schedule=schedule)
    assert full[: k + 1] == prefix


# ────────────────────────────────────────────────────────────────────────
# The resample path into the engine — earlier buckets never change
# ────────────────────────────────────────────────────────────────────────


@given(bars=_bar_series(min_size=_BARS_PER_DAY + 1, max_size=3 * _BARS_PER_DAY), data=st.data())
@settings(suppress_health_check=[HealthCheck.too_slow], deadline=None)
def test_feed_resampler_prior_day_buckets_immune_to_suffix_day_mutation(
    bars: list[CandleBar], data: st.DataObject
) -> None:
    """Mutating every bar of the LAST day never changes an earlier derived bucket."""
    last_day_start = ((len(bars) - 1) // _BARS_PER_DAY) * _BARS_PER_DAY
    mutated = _mutate_suffix(bars, last_day_start, data)
    # Compare buckets strictly BEFORE the bucket containing the first mutated
    # bar (that bucket legitimately changes — it aggregates mutated rows).
    epoch = int(bars[last_day_start].timestamp.timestamp())
    boundary_ts = datetime.fromtimestamp((epoch // 300) * 300, tz=UTC)
    base_out = resample_bars(bars=bars, source_interval="1minute", target_interval="5minute")
    mutated_out = resample_bars(bars=mutated, source_interval="1minute", target_interval="5minute")
    base_prior = [b for b in base_out if b.timestamp < boundary_ts]
    mutated_prior = [b for b in mutated_out if b.timestamp < boundary_ts]
    assert base_prior == mutated_prior


@given(bars=_bar_series(min_size=6), data=st.data())
@settings(suppress_health_check=[HealthCheck.too_slow], deadline=None)
def test_feed_resampler_closed_buckets_immune_to_appended_suffix(
    bars: list[CandleBar], data: st.DataObject
) -> None:
    """Every fully-closed prefix bucket is unchanged when later bars arrive.

    ``resample(prefix)`` and ``resample(full)`` may differ only in the
    prefix's LAST bucket (which the suffix can legitimately extend) — every
    earlier bucket is already closed and must be bit-identical.
    """
    m = data.draw(st.integers(min_value=1, max_value=len(bars) - 1))
    prefix_out = resample_bars(bars=bars[:m], source_interval="1minute", target_interval="5minute")
    full_out = resample_bars(bars=bars, source_interval="1minute", target_interval="5minute")
    closed = len(prefix_out) - 1
    assert prefix_out[:closed] == full_out[:closed]


@given(bars=_bar_series(min_size=_BARS_PER_DAY + 1, max_size=3 * _BARS_PER_DAY), data=st.data())
@settings(suppress_health_check=[HealthCheck.too_slow], deadline=None)
def test_tf_resampler_prior_day_buckets_immune_to_suffix_day_mutation(
    bars: list[CandleBar], data: st.DataObject
) -> None:
    """The canonical session-anchored 1m→TF deriver is suffix-day independent.

    ``brutex.data.tf_resampler.resample_1m_to_tf`` is the resampler that
    materialises the derived-TF partitions the sweep's ``bars_for`` reads, so
    it IS the resample path into the engine — mutating the last day's 1m rows
    must leave every earlier derived 5m bucket bit-identical.
    """
    pl = pytest.importorskip("polars")  # the [data] extra; skips the .[dev]-only job
    from brutex.data.tf_resampler import resample_1m_to_tf

    last_day_start = ((len(bars) - 1) // _BARS_PER_DAY) * _BARS_PER_DAY
    mutated = _mutate_suffix(bars, last_day_start, data)
    # Compare buckets strictly BEFORE the session-anchored (09:15 IST,
    # DEC-RESAMPLE-ANCHOR-002) bucket containing the first mutated bar —
    # that bucket legitimately changes, every earlier one may not.
    first_mutated_ist = bars[last_day_start].timestamp.astimezone(_IST)
    anchor_ist = first_mutated_ist.replace(hour=9, minute=15, second=0, microsecond=0)
    bucket_index = (first_mutated_ist - anchor_ist) // timedelta(minutes=5)
    boundary = (
        (anchor_ist + bucket_index * timedelta(minutes=5)).astimezone(UTC).replace(tzinfo=None)
    )

    base_out = resample_1m_to_tf(_tf_frame(bars), 5).filter(pl.col("ts_utc") < boundary)
    mutated_out = resample_1m_to_tf(_tf_frame(mutated), 5).filter(pl.col("ts_utc") < boundary)
    assert base_out.equals(mutated_out)


@given(bars=_bar_series(min_size=8, max_size=3 * _BARS_PER_DAY), data=st.data())
@settings(suppress_health_check=[HealthCheck.too_slow], deadline=None)
def test_tf_resampler_closed_buckets_immune_to_mid_session_suffix_mutation(
    bars: list[CandleBar], data: st.DataObject
) -> None:
    """WITHIN-session teeth: a mid-session suffix mutation never changes a CLOSED bucket.

    Teeth-verification round (DEC-SWEEP-LOOKAHEAD-GUARD-001): an adversarial
    reviewer PLANTED a within-session cross-bucket look-ahead in
    ``resample_1m_to_tf`` (bucket label off-by-one — every closed bucket
    incorporated the NEXT bucket's first bar) and the original suffix-DAY
    gate above PASSED, because session anchoring confines buckets to their
    own IST date so a within-day leak never crossed that gate's comparison
    boundary. THIS gate closes the hole: mutate everything strictly after an
    arbitrary (typically mid-session) cut and require every bucket whose
    window ENDS at/before the first mutated bar to be bit-identical — a
    cross-bucket leak inside one session now crosses the comparison line.
    Verified to FAIL against the exact planted off-by-one before commit.
    """
    pl = pytest.importorskip("polars")  # the [data] extra; skips the .[dev]-only job
    from brutex.data.tf_resampler import resample_1m_to_tf

    k = data.draw(st.integers(min_value=0, max_value=len(bars) - 2))
    mutated = _mutate_suffix(bars, k + 1, data)
    # A 5m bucket labeled s covers [s, s+5): it is CLOSED at the mutation
    # point iff s + 5min <= ts(first mutated bar) ⟺ s <= boundary − 5min.
    boundary = bars[k + 1].timestamp.replace(tzinfo=None)
    closed_horizon = boundary - timedelta(minutes=5)
    base_out = resample_1m_to_tf(_tf_frame(bars), 5).filter(pl.col("ts_utc") <= closed_horizon)
    mutated_out = resample_1m_to_tf(_tf_frame(mutated), 5).filter(
        pl.col("ts_utc") <= closed_horizon
    )
    assert base_out.equals(mutated_out)


@given(bars=_bar_series(min_size=6), data=st.data())
@settings(suppress_health_check=[HealthCheck.too_slow], deadline=None)
def test_tf_resampler_closed_buckets_immune_to_appended_suffix(
    bars: list[CandleBar], data: st.DataObject
) -> None:
    """The tf-resampler twin of the feed-resampler appended-suffix gate.

    ``resample_1m_to_tf(prefix)`` and ``resample_1m_to_tf(full)`` may differ
    only in the prefix's LAST bucket (which appended bars can legitimately
    extend) — every earlier bucket is already closed and must be
    bit-identical whether the later bars exist or not.
    """
    pytest.importorskip("polars")  # the [data] extra; skips the .[dev]-only job
    from brutex.data.tf_resampler import resample_1m_to_tf

    m = data.draw(st.integers(min_value=1, max_value=len(bars) - 1))
    prefix_out = resample_1m_to_tf(_tf_frame(bars[:m]), 5)
    full_out = resample_1m_to_tf(_tf_frame(bars), 5)
    closed = prefix_out.height - 1
    assert prefix_out.head(closed).equals(full_out.head(closed))


# ────────────────────────────────────────────────────────────────────────
# Trade-sim exits through the FULL spine (bars → bits → signals → trades)
# ────────────────────────────────────────────────────────────────────────


@given(bars=_bar_series(min_size=8), data=st.data())
@settings(suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much], deadline=None)
def test_full_spine_trades_closed_before_mutation_point_are_unchanged(
    bars: list[CandleBar], data: st.DataObject
) -> None:
    """A trade closed at bar K survives ANY mutation of bars strictly after K.

    The full-spine extension of the walker-local property: the suffix
    mutation here flows through the RECOMPUTED ``bar_bits`` and entry
    signals (mask 0 fires on every bar), so future information would have
    every derived channel available to leak through — and still may not
    change a trade already closed at bar ``K``.
    """
    base_bits = build_bar_bits(bars)
    base_signals = mask_to_entry_signals(base_bits, 0)
    base_trades = simulate_trades(
        bars,
        base_signals,
        direction=Side.BUY,
        tf_minutes=1,
        stop_loss_paisa=80,
        take_profit_paisa=50,
    )
    ts_to_index = {bar.timestamp: i for i, bar in enumerate(bars)}
    closed_early = [
        (j, ts_to_index[t.exit_ts])
        for j, t in enumerate(base_trades)
        if ts_to_index[t.exit_ts] < len(bars) - 1
    ]
    if not closed_early:
        return  # nothing closed before the final bar — no suffix to mutate
    j, exit_index = data.draw(st.sampled_from(closed_early))
    mutated = _mutate_suffix(bars, exit_index + 1, data)
    mutated_bits = build_bar_bits(mutated)
    mutated_signals = mask_to_entry_signals(mutated_bits, 0)
    mutated_trades = simulate_trades(
        mutated,
        mutated_signals,
        direction=Side.BUY,
        tf_minutes=1,
        stop_loss_paisa=80,
        take_profit_paisa=50,
    )
    assert mutated_trades[: j + 1] == base_trades[: j + 1]


# ────────────────────────────────────────────────────────────────────────
# Engine statelessness — a prefix evaluation is bit-identical before/after
# a full-series run in the SAME process (no global-state future leak)
# ────────────────────────────────────────────────────────────────────────


def _deterministic_series(n_days: int = 8, per_day: int = 60) -> list[CandleBar]:
    """A fixed multi-day drifting series (the sweep_run property-suite shape)."""
    bars: list[CandleBar] = []
    for d in range(n_days):
        price = 100.0
        for m in range(per_day):
            close = price + 0.6 if m % 3 else price - 0.3
            ts = (_SESSION_OPEN_IST + timedelta(days=d, minutes=m)).astimezone(UTC)
            hi = max(price, close) + 0.5
            lo = min(price, close) - 0.5
            bars.append(
                CandleBar(
                    timestamp=ts,
                    open=Decimal(str(round(price, 2))),
                    high=Decimal(str(round(hi, 2))),
                    low=Decimal(str(round(lo, 2))),
                    close=Decimal(str(round(close, 2))),
                    volume=100,
                    open_interest=None,
                )
            )
            price = close
    return bars


def _config() -> SweepRunConfig:
    return SweepRunConfig(
        instrument="NIFTY",
        tf="1m",
        direction=Side.BUY,
        mode="A",
        condition_bits=[0, 1, 2, 3, 4],
        k=1,
        data_snapshot_hash="snap0001",
        indicator_version="ind-v1",
        code_commit_sha="deadbeef",
        tf_minutes=1,
        sl_paisa=80,
        tp_paisa=50,
    )


def test_prefix_sweep_is_bit_identical_before_and_after_a_full_series_run(
    tmp_path: Path,
) -> None:
    pytest.importorskip("polars")  # flush needs the [data] extra; skips the .[dev]-only job
    full = _deterministic_series()
    prefix = full[: len(full) // 2]
    cfg = _config()

    def _run(tag: str, bars: list[CandleBar]) -> tuple[str, bytes]:
        store_path = tmp_path / f"{tag}.parquet"
        store = ResultsStore(path=store_path)
        dedup = ComboDedup(OptionDedupIndex(capacity=10_000, fpr_target=1e-6))
        with AuditWriter.open(tmp_path / f"{tag}.jsonl") as audit:
            summary = run_persisted_sweep(bars, cfg, store=store, dedup=dedup, audit=audit)
        return summary.store_content_hash, store_path.read_bytes()

    # Prefix metrics BEFORE any full-series evaluation …
    prefix_bits = build_bar_bits(prefix)
    metrics_before = metrics_for_mask(
        prefix,
        prefix_bits,
        0,
        direction=Side.BUY,
        tf_minutes=1,
        stop_loss_paisa=80,
        take_profit_paisa=50,
        start_equity_paisa=DEFAULT_START_EQUITY_PAISA,
    )
    hash_before, bytes_before = _run("prefix-before", prefix)

    # … the full-series sweep runs in between (any hypothetical global state
    # would now have seen every future bar) …
    _run("full", full)

    # … and the SAME prefix evaluation afterwards is bit-identical.
    metrics_after = metrics_for_mask(
        prefix,
        build_bar_bits(prefix),
        0,
        direction=Side.BUY,
        tf_minutes=1,
        stop_loss_paisa=80,
        take_profit_paisa=50,
        start_equity_paisa=DEFAULT_START_EQUITY_PAISA,
    )
    hash_after, bytes_after = _run("prefix-after", prefix)
    assert metrics_after == metrics_before
    assert hash_after == hash_before
    assert bytes_after == bytes_before
