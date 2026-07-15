"""Promoted Phase-2 property invariants (DEC-PHASE2-INVARIANTS-PROMOTE-001).

These were ``pytest.skip("Phase 2 implementation pending …")`` stubs in
``test_invariants_catalog.py``. The underlying subjects have since landed,
so this file turns the stubs into LIVE Hypothesis properties driving the
real, sandbox-importable components. The catalog rows now point here.

Promoted invariants
-------------------
* **P-11** — u64 mask AND is commutative + associative; ``evaluate_mask``
  encodes superset membership (``brutex.engine.combo_mask``).
* **P-21** — every persisted price is snapped to the 2-decimal (1-paisa)
  tick grid via ``round_money`` (``ROUND_HALF_UP``). NOTE: the original
  catalog docstring said "multiple of 5 paise" — that is a stale claim;
  the live lake-write snap (DEC-SPOT-PRICE-2DP-001) is a 2-decimal grid,
  which is what this property asserts (Golden Rule #1: test the code, not
  the stale comment).
* **P-22** — no NSE holiday is a trading day (→ no bars land on holidays).
* **P-23** — ``in_session`` keeps exactly the IST ``[09:15, 15:30)`` window;
  ``filter_intraday_session`` is idempotent and emits only in-session bars.
* **P-24** — the per-mask superset count is independent of bar/worker
  evaluation order and is additive across any partition of the bars
  (``count_supermasks`` Rust kernel == the pure-numpy reference).
* **P-25** — the shared condition vocabulary contains NO India-VIX bit
  (VIX is REFERENCE-ONLY per CLAUDE.md §2 Rule 3); it cannot leak into a
  pattern mask by construction.
* **P-19** — the Parquet writer is deterministic: writing identical bars
  twice yields a byte-identical file (equal ``raw_hash``).
* **P-26** — the audit→dedup replay oracle is consistent: replaying an
  ``add``-action audit log against the exact live subject set diffs clean,
  and dropping a subject is detected.

Deferred (honest envelope, Golden Rule #10)
-------------------------------------------
* **P-20** (``replay(audit_log) → Parquet`` byte-equality) stays a catalog
  skip — the end-to-end audit→Parquet reconstruction harness does not exist
  yet (only the dedup-subject set oracle of P-26 does).
"""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import pytest

# Bit / mask vocabulary — pure, no Rust wheel needed.
from brutex.engine.combo_mask import (
    count_supermasks,
    count_supermasks_reference,
    evaluate_mask,
)
from brutex.money import round_money
from brutex.vocabulary.bits import (
    ALL_CONDITION_NAMES,
    BIT_TO_NAME,
    NAME_TO_BIT,
    TOTAL_CONDITIONS,
)
from hypothesis import given, settings
from hypothesis import strategies as st

_U52_MAX = (1 << 52) - 1
_u52 = st.integers(min_value=0, max_value=_U52_MAX)


# ────────────────────────────────────────────────────────────────────
# P-11 — u64 mask AND algebra + superset membership
# ────────────────────────────────────────────────────────────────────
@given(a=_u52, b=_u52, c=_u52)
@settings(max_examples=400)
def test_p_11_mask_and_commutative_associative(a: int, b: int, c: int) -> None:
    """P-11: bitwise AND on the u64 vocabulary mask is commutative and
    associative, so the order masks are combined never changes the result."""
    assert (a & b) == (b & a)
    assert ((a & b) & c) == (a & (b & c))
    # Identity (full 52-bit mask) and absorbing (0) elements.
    assert (a & _U52_MAX) == a
    assert (a & 0) == 0


@given(bar_bits=_u52, required=_u52)
@settings(max_examples=400)
def test_p_11_evaluate_mask_is_superset_membership(bar_bits: int, required: int) -> None:
    """P-11: ``evaluate_mask`` is exactly the superset predicate
    ``(bar_bits & required) == required`` and order of the two operands in
    the AND does not matter."""
    expected = (bar_bits & required) == required
    assert evaluate_mask(bar_bits, required) is expected
    # A bar always satisfies its own bits, and the empty requirement.
    assert evaluate_mask(bar_bits, bar_bits) is True
    assert evaluate_mask(bar_bits, 0) is True


# ────────────────────────────────────────────────────────────────────
# P-21 — persisted prices snap to the 2-decimal tick grid (ROUND_HALF_UP)
# ────────────────────────────────────────────────────────────────────
@given(
    rupees=st.decimals(
        min_value=Decimal("0"),
        max_value=Decimal("100000"),
        allow_nan=False,
        allow_infinity=False,
        places=6,
    )
)
@settings(max_examples=400)
def test_p_21_price_snaps_to_2dp_tick_grid(rupees: Decimal) -> None:
    """P-21: every price written to the lake is quantized to 2 decimals
    (1-paisa grid), ROUND_HALF_UP, and the snap is idempotent."""
    snapped = round_money(rupees)
    # Exactly two decimal places — i.e. an integer number of paise.
    assert snapped == snapped.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    assert (snapped * 100) == (snapped * 100).to_integral_value()
    # Idempotent: snapping an already-snapped price is a no-op.
    assert round_money(snapped) == snapped


def test_p_21_round_half_up_not_bankers() -> None:
    """P-21: the tick snap is ROUND_HALF_UP (1.005 → 1.01), never bankers'
    rounding and never truncation (the ``14020.7999`` vendor-noise case)."""
    assert round_money(Decimal("1.005")) == Decimal("1.01")
    assert round_money(Decimal("1.004")) == Decimal("1.00")
    assert round_money(Decimal("14020.7999")) == Decimal("14020.80")


# ────────────────────────────────────────────────────────────────────
# P-22 — no NSE holiday is a trading day (→ no bars on holidays)
# ────────────────────────────────────────────────────────────────────
def test_p_22_no_holiday_is_a_trading_day() -> None:
    """P-22: every date in the NSE holiday set is NOT a trading day, so
    the ingest never lands an intraday bar on a market holiday."""
    from brutex.calendars.nse import (
        ALL_HOLIDAYS,
        SPECIAL_WEEKEND_TRADING_SESSIONS,
        is_trading_day,
    )

    # Special weekend sessions (Budget-Day Saturdays) are the only dates
    # that are both a "weekend" AND open; they are disjoint from holidays.
    for holiday in ALL_HOLIDAYS:
        if holiday in SPECIAL_WEEKEND_TRADING_SESSIONS:
            continue
        assert not is_trading_day(holiday), f"holiday {holiday} flagged as trading day"


@given(offset_days=st.integers(min_value=0, max_value=365 * 5))
@settings(max_examples=300)
def test_p_22_trading_day_partition_is_consistent(offset_days: int) -> None:
    """P-22: ``is_trading_day`` is exactly ``special_weekend OR (not weekend
    and not holiday)`` — a total, mutually-consistent partition of the
    calendar with no overlap between 'holiday' and 'trading day'."""
    from brutex.calendars.nse import (
        SPECIAL_WEEKEND_TRADING_SESSIONS,
        is_holiday,
        is_trading_day,
        is_weekend,
    )

    d = (datetime(2021, 1, 1, tzinfo=UTC) + timedelta(days=offset_days)).date()
    special = d in SPECIAL_WEEKEND_TRADING_SESSIONS
    expected = special or (not is_weekend(d) and not is_holiday(d))
    assert is_trading_day(d) is expected


# ────────────────────────────────────────────────────────────────────
# P-23 — session window [09:15, 15:30) IST
# ────────────────────────────────────────────────────────────────────
_IST = __import__("brutex.data.session", fromlist=["IST"]).IST
_SESSION_OPEN = time(9, 15)
_SESSION_CLOSE = time(15, 30)


@given(
    dt=st.datetimes(
        min_value=datetime(2021, 1, 1),
        max_value=datetime(2026, 12, 31),
        timezones=st.just(UTC),
    )
)
@settings(max_examples=500)
def test_p_23_in_session_matches_ist_window(dt: datetime) -> None:
    """P-23: ``in_session`` is true iff the bar's IST wall-clock time is in
    the half-open ``[09:15, 15:30)`` trading window."""
    from brutex.data.session import in_session

    ist_t = dt.astimezone(_IST).time()
    expected = _SESSION_OPEN <= ist_t < _SESSION_CLOSE
    assert in_session(dt) is expected


def test_p_23_filter_keeps_only_in_session_and_is_idempotent() -> None:
    """P-23: ``filter_intraday_session`` emits only in-session bars and is
    idempotent (re-filtering a filtered series is a no-op)."""
    from brutex.data.models import CandleBar
    from brutex.data.session import filter_intraday_session, in_session

    def _bar(hour: int, minute: int) -> CandleBar:
        # IST wall-clock → UTC (IST = UTC+5:30).
        ist = datetime(2024, 2, 20, hour, minute, tzinfo=_IST)
        return CandleBar(
            timestamp=ist.astimezone(UTC),
            open=Decimal("100.00"),
            high=Decimal("100.00"),
            low=Decimal("100.00"),
            close=Decimal("100.00"),
            volume=1,
            open_interest=None,
        )

    bars = [_bar(9, 0), _bar(9, 15), _bar(12, 0), _bar(15, 29), _bar(15, 30), _bar(16, 0)]
    kept = filter_intraday_session(bars)
    assert all(in_session(b.timestamp) for b in kept)
    # 09:15, 12:00, 15:29 are in; 09:00, 15:30, 16:00 are out.
    assert len(kept) == 3
    assert filter_intraday_session(kept) == kept


# ────────────────────────────────────────────────────────────────────
# P-24 — superset count independent of bar/worker order, additive over splits
# ────────────────────────────────────────────────────────────────────
@given(
    bars=st.lists(_u52, min_size=1, max_size=40),
    masks=st.lists(_u52, min_size=1, max_size=16),
    split=st.integers(min_value=0, max_value=40),
    seed=st.integers(min_value=0, max_value=2**32 - 1),
)
@settings(max_examples=300)
def test_p_24_supermask_count_order_and_partition_invariant(
    bars: list[int], masks: list[int], split: int, seed: int
) -> None:
    """P-24: the per-mask superset count is invariant to bar order and
    additive across any partition of the bars — so sharding bars across
    workers cannot change the result. The Rust kernel agrees with the
    pure-numpy reference on every input."""
    import random

    full = count_supermasks_reference(bars, masks)

    # (a) order independence
    shuffled = bars[:]
    random.Random(seed).shuffle(shuffled)
    assert count_supermasks_reference(shuffled, masks) == full

    # (b) additivity across a partition into two worker shards
    cut = split % (len(bars) + 1)
    left = count_supermasks_reference(bars[:cut], masks)
    right = count_supermasks_reference(bars[cut:], masks)
    assert [lhs + rhs for lhs, rhs in zip(left, right, strict=True)] == full

    # (c) Rust kernel parity (the hot-path) == reference (the oracle)
    assert list(count_supermasks(bars, masks)) == full


# ────────────────────────────────────────────────────────────────────
# P-25 — VIX never appears in the shared condition vocabulary
# ────────────────────────────────────────────────────────────────────
def test_p_25_vocabulary_has_no_vix_bit() -> None:
    """P-25: India-VIX is REFERENCE-ONLY (CLAUDE.md §2 Rule 3) — no atomic
    condition references it, so a generated pattern mask cannot leak VIX."""
    assert TOTAL_CONDITIONS == 66  # grow-only: 52 + 8 (ADR-0012 D4) + 6 (the 60→66 reshape)
    assert len(ALL_CONDITION_NAMES) == TOTAL_CONDITIONS
    assert set(NAME_TO_BIT) == set(ALL_CONDITION_NAMES)
    # The bit map is a dense 0..TOTAL-1 bijection.
    assert sorted(NAME_TO_BIT.values()) == list(range(TOTAL_CONDITIONS))
    assert set(BIT_TO_NAME) == set(range(TOTAL_CONDITIONS))
    # No condition name (or its bit alias) mentions VIX in any casing.
    for name in ALL_CONDITION_NAMES:
        assert "vix" not in name.lower()


@given(mask=_u52)
@settings(max_examples=200)
def test_p_25_every_mask_bit_maps_to_a_non_vix_condition(mask: int) -> None:
    """P-25: every set bit of any vocabulary mask resolves to a named non-VIX
    condition — there is no bit position a VIX leak could occupy."""
    for pos in range(TOTAL_CONDITIONS):
        if mask & (1 << pos):
            assert pos in BIT_TO_NAME
            assert "vix" not in BIT_TO_NAME[pos].lower()


# ────────────────────────────────────────────────────────────────────
# P-19 — deterministic Parquet writer (byte-identical on repeat write)
# ────────────────────────────────────────────────────────────────────
@given(
    closes=st.lists(
        st.decimals(
            min_value=Decimal("1"),
            max_value=Decimal("99999"),
            allow_nan=False,
            allow_infinity=False,
            places=2,
        ),
        min_size=1,
        max_size=12,
    )
)
@settings(max_examples=40, deadline=None)
def test_p_19_parquet_write_is_deterministic(closes: list[Decimal]) -> None:
    """P-19: writing the same bars twice produces a byte-identical Parquet
    partition (equal ``raw_hash``) — reproducibility (CLAUDE.md Rule #4)."""
    pytest.importorskip("polars")
    from brutex.data.models import CandleBar
    from brutex.data.parquet_writer import ParquetWriter

    base = datetime(2024, 2, 20, 4, 0, tzinfo=UTC)  # in-session UTC
    bars = [
        CandleBar(
            timestamp=base + timedelta(minutes=i),
            open=c,
            high=c,
            low=c,
            close=c,
            volume=i,
            open_interest=None,
        )
        for i, c in enumerate(closes)
    ]
    with tempfile.TemporaryDirectory() as tmp:
        writer = ParquetWriter(lake_root=Path(tmp))
        first = writer.write(
            exchange="NSE",
            segment="INDEX",
            symbol="NSE-NIFTY",
            interval="1minute",
            bars=bars,
        )
        second = writer.write(
            exchange="NSE",
            segment="INDEX",
            symbol="NSE-NIFTY",
            interval="1minute",
            bars=bars,
        )
        assert first.raw_hash == second.raw_hash


# ────────────────────────────────────────────────────────────────────
# P-26 — audit→dedup replay oracle is consistent (idempotence proof)
# ────────────────────────────────────────────────────────────────────
@given(
    subjects=st.lists(
        st.text(
            alphabet=st.characters(min_codepoint=48, max_codepoint=122),
            min_size=1,
            max_size=24,
        ),
        min_size=1,
        max_size=30,
        unique=True,
    )
)
@settings(max_examples=60, deadline=None)
def test_p_26_audit_replay_matches_live_dedup_set(subjects: list[str]) -> None:
    """P-26: replaying the ``add``-action audit log against the exact live
    dedup set diffs clean; dropping any subject is detected. This is the
    idempotence/replay oracle (CLAUDE.md Rules #7/#8)."""
    from brutex.orchestrator.audit import AuditWriter
    from brutex.orchestrator.replay import assert_dedup_consistent, replay_audit_to_diff

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "audit.jsonl"
        with AuditWriter.open(path) as log:
            for i, subject in enumerate(subjects):
                log.append(action="add", subject=subject, payload={}, ts_utc=float(i))

        live = frozenset(subjects)
        # Consistent: replayed subjects == live set → clean diff, no raise.
        diff = replay_audit_to_diff(audit_path=path, live_subjects=live)
        assert diff.is_consistent
        assert not diff.missing_from_live
        assert not diff.extra_in_live
        assert_dedup_consistent(audit_path=path, live_subjects=live)

        # Drop one subject from live → replay detects it as missing.
        if len(subjects) > 1:
            dropped = frozenset(subjects[1:])
            bad = replay_audit_to_diff(audit_path=path, live_subjects=dropped)
            assert not bad.is_consistent
            assert subjects[0] in bad.missing_from_live
