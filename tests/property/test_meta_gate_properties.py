"""Hypothesis properties for the meta/abstention layer
(DEC-META-LABEL-ANNOTATE-001 + DEC-ABSTAIN-GATE-STREAM-001).

* Python-vs-Rust scorer + abstain parity over hostile floats (bit-exact on
  the same box — the two arms share one libm; skipped honestly when the
  ``brutex_meta`` wheel is absent).
* τ-curve monotonicity (kept counts never increase with τ).
* ``abstain_passes ⇔ reasons == ()`` and the bitmask↔tuple equivalence.
* The CPCV purge invariant over the fit's own split parameters (no train
  index inside any test block's ``[start − span, end + span + embargo]``
  window — the leakage-free law the OOF fit rides).
* Intraday ENTRY-OBSERVABILITY of the feature vector (the round-1 MAJOR-1
  law): a day-``D`` trade's vector is computable from lens data of
  sessions strictly ``< D`` — mutating day ``D``'s + later days' OHLC in
  the lens inputs never changes it.
* The end-to-end composition law (round-1 N-9): ``compute_label_span`` +
  ``cpcv_splits(label_span=span, embargo=span)`` leave NO train trade
  time-overlapping ANY test trade's ``[entry, exit]`` window.
"""

from __future__ import annotations

import math
import random
import struct
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import pairwise

from brutex.data.models import CandleBar
from brutex.engine.session_lens import build_session_lens
from brutex.meta.abstain import (
    REGIME_CODES,
    AbstainGateConfig,
    abstain_masks_python,
    abstain_passes,
    abstain_reasons,
    reasons_from_mask,
)
from brutex.meta.cpcv_fit import compute_label_span
from brutex.meta.features import trade_features_from_wire
from brutex.meta.logistic import score_logistic
from brutex.meta.threshold import tau_curve
from brutex.metrics.cpcv import cpcv_splits
from hypothesis import given, settings
from hypothesis import strategies as st

NAN = float("nan")

#: Hostile-but-bounded floats: finance ranges + IEEE specials.
hostile_floats = st.one_of(
    st.floats(min_value=-1e3, max_value=1e3),
    st.floats(allow_nan=True, allow_infinity=True, width=64),
    st.sampled_from([0.0, -0.0, 5e-324, 1e300, -1e300, NAN, math.inf, -math.inf]),
)

finite_floats = st.floats(min_value=-500.0, max_value=500.0, allow_nan=False)


def _bits(values: list[float]) -> bytes:
    return struct.pack(f"<{len(values)}d", *values)


class TestScorerParity:
    """Python oracle ≡ compiled Rust kernel, bit-for-bit, same box."""

    @settings(max_examples=200, deadline=None)
    @given(
        rows=st.lists(hostile_floats, max_size=48),
        weight=hostile_floats,
        bias=hostile_floats,
    )
    def test_single_feature_parity(self, rows: list[float], weight: float, bias: float) -> None:
        import pytest

        brutex_meta = pytest.importorskip("brutex_meta", reason="wheel not built")
        oracle = score_logistic(rows, 1, [weight], bias)
        rust = brutex_meta.score_logistic(rows, 1, [weight], bias)
        assert _bits(oracle) == _bits(rust)

    @settings(max_examples=100, deadline=None)
    @given(
        pairs=st.lists(st.tuples(finite_floats, finite_floats), max_size=24),
        w0=finite_floats,
        w1=finite_floats,
        bias=finite_floats,
    )
    def test_two_feature_parity_and_unit_interval(
        self,
        pairs: list[tuple[float, float]],
        w0: float,
        w1: float,
        bias: float,
    ) -> None:
        import pytest

        brutex_meta = pytest.importorskip("brutex_meta", reason="wheel not built")
        flat = [value for pair in pairs for value in pair]
        oracle = score_logistic(flat, 2, [w0, w1], bias)
        rust = brutex_meta.score_logistic(flat, 2, [w0, w1], bias)
        assert _bits(oracle) == _bits(rust)
        assert all(p == 0.0 or (p > 0.0 and p <= 1.0) for p in oracle)


class TestAbstainParity:
    @settings(max_examples=200, deadline=None)
    @given(
        regimes=st.lists(st.integers(min_value=-4, max_value=70), max_size=32),
        allowed=st.integers(min_value=0, max_value=2**64 - 1),
        vix_seed=st.lists(hostile_floats, max_size=32),
        lo=hostile_floats,
        hi=hostile_floats,
        flags_seed=st.lists(st.integers(min_value=0, max_value=3), max_size=32),
        block=st.booleans(),
        conf_seed=st.lists(hostile_floats, max_size=32),
        tau=hostile_floats,
    )
    def test_python_rust_mask_parity(
        self,
        regimes: list[int],
        allowed: int,
        vix_seed: list[float],
        lo: float,
        hi: float,
        flags_seed: list[int],
        block: bool,
        conf_seed: list[float],
        tau: float,
    ) -> None:
        import pytest

        brutex_meta = pytest.importorskip("brutex_meta", reason="wheel not built")
        n = len(regimes)
        vix = (vix_seed + [NAN] * n)[:n]
        flags = (flags_seed + [0] * n)[:n]
        conf = (conf_seed + [NAN] * n)[:n]
        oracle = abstain_masks_python(regimes, allowed, vix, lo, hi, flags, block, conf, tau)
        rust = list(
            brutex_meta.abstain_masks(regimes, allowed, vix, lo, hi, flags, block, conf, tau)
        )
        assert oracle == rust


configs = st.builds(
    AbstainGateConfig,
    allowed_regimes=st.sets(st.sampled_from(sorted(REGIME_CODES)), min_size=1).map(frozenset),
    vix_lo=st.one_of(st.none(), st.floats(min_value=0.0, max_value=30.0)),
    vix_hi=st.one_of(st.none(), st.floats(min_value=30.0, max_value=90.0)),
    block_event_days=st.booleans(),
    tau=st.one_of(st.none(), st.floats(min_value=0.0, max_value=1.0)),
)

snapshots = st.tuples(
    st.sampled_from(sorted(REGIME_CODES)),
    st.one_of(st.none(), st.floats(min_value=0.0, max_value=100.0), st.just(NAN)),
    st.booleans(),
    st.one_of(st.none(), st.floats(min_value=0.0, max_value=1.0), st.just(NAN)),
)


class TestReasonLaws:
    @settings(max_examples=300, deadline=None)
    @given(config=configs, snapshot=snapshots)
    def test_passes_iff_reasons_empty(
        self,
        config: AbstainGateConfig,
        snapshot: tuple[str, float | None, bool, float | None],
    ) -> None:
        regime, vix, event_day, confidence = snapshot
        passes = abstain_passes(
            regime=regime, vix=vix, event_day=event_day, confidence=confidence, config=config
        )
        reasons = abstain_reasons(
            regime=regime, vix=vix, event_day=event_day, confidence=confidence, config=config
        )
        assert passes == (reasons == ())

    @settings(max_examples=300, deadline=None)
    @given(config=configs, snapshot=snapshots)
    def test_bitmask_oracle_equals_the_tuple_function(
        self,
        config: AbstainGateConfig,
        snapshot: tuple[str, float | None, bool, float | None],
    ) -> None:
        # decode(mask(x)) == reasons(x) — the FFI encoding loses nothing.
        regime, vix, event_day, confidence = snapshot
        allowed_mask = 0
        for token in config.allowed_regimes:
            allowed_mask |= 1 << REGIME_CODES[token]
        (mask,) = abstain_masks_python(
            [REGIME_CODES[regime]],
            allowed_mask,
            [vix if vix is not None else NAN],
            config.vix_lo if config.vix_lo is not None else NAN,
            config.vix_hi if config.vix_hi is not None else NAN,
            [1 if event_day else 0],
            config.block_event_days,
            [confidence if confidence is not None else NAN],
            config.tau if config.tau is not None else NAN,
        )
        assert reasons_from_mask(mask) == abstain_reasons(
            regime=regime, vix=vix, event_day=event_day, confidence=confidence, config=config
        )


class TestTauMonotonicity:
    @settings(max_examples=200, deadline=None)
    @given(
        p_win=st.lists(st.one_of(st.none(), st.floats(min_value=0.0, max_value=1.0)), max_size=64),
        seed=st.randoms(use_true_random=False),
    )
    def test_kept_never_increases_with_tau(
        self, p_win: list[float | None], seed: random.Random
    ) -> None:
        wins = [seed.random() < 0.5 for _ in p_win]
        points = tau_curve(p_win, wins)
        kept = [point.n_kept for point in points]
        assert all(a >= b for a, b in pairwise(kept))
        pcts = [point.kept_pct for point in points if point.kept_pct is not None]
        assert all(a >= b for a, b in pairwise(pcts))


def _daily_bar(day_index: int, close_int: int) -> CandleBar:
    ts = datetime(2024, 1, 1, 10, 0, tzinfo=UTC) + timedelta(days=day_index)
    close = Decimal(close_int)
    return CandleBar(
        timestamp=ts,
        open=close,
        high=close + 1,
        low=close - 1,
        close=close,
        volume=0,
        open_interest=None,
    )


class TestEntryObservability:
    """MAJOR-1: the vector for a day-D trade reads sessions strictly < D."""

    @settings(max_examples=60, deadline=None)
    @given(
        closes=st.lists(st.integers(min_value=95, max_value=105), min_size=4, max_size=10),
        mutated=st.lists(st.integers(min_value=200, max_value=400), min_size=10, max_size=10),
        data=st.data(),
    )
    def test_mutating_day_d_and_later_ohlc_never_moves_a_day_d_vector(
        self, closes: list[int], mutated: list[int], data: st.DataObject
    ) -> None:
        n = len(closes)
        d = data.draw(st.integers(min_value=1, max_value=n - 1), label="entry_day_index")
        original = [_daily_bar(i, c) for i, c in enumerate(closes)]
        # Mutate day D's AND every later day's OHLC (a wildly different
        # price path from the entry day onward — post-entry information).
        poisoned = [_daily_bar(i, closes[i] if i < d else mutated[i]) for i in range(n)]
        lens_a = build_session_lens(original, instrument="NIFTY")
        lens_b = build_session_lens(poisoned, instrument="NIFTY")
        entry = datetime(2024, 1, 1, 4, 0, tzinfo=UTC) + timedelta(days=d)  # 09:30 IST day D
        row = {
            "trade_seq": 0,
            "entry_ts": entry.isoformat(),
            "exit_ts": (entry + timedelta(minutes=30)).isoformat(),
            "pnl_paisa": 100,
            "vix_entry": 14.0,
        }
        a = trade_features_from_wire(row, lens=lens_a)
        b = trade_features_from_wire(row, lens=lens_b)
        assert a is not None and b is not None
        assert a.vector == b.vector
        assert a.prev_regime == b.prev_regime
        assert a.event_day == b.event_day


class TestLeakageFreeComposition:
    """N-9: span + cpcv_splits(span, span) ⇒ zero train/test time overlap."""

    @settings(max_examples=60, deadline=None)
    @given(
        gaps=st.lists(st.integers(min_value=0, max_value=5_000), min_size=30, max_size=80),
        holds=st.data(),
    )
    def test_no_train_trade_overlaps_any_test_trade_window(
        self, gaps: list[int], holds: st.DataObject
    ) -> None:
        n = len(gaps)
        entries: list[int] = []
        t = 0
        for gap in gaps:
            t += gap
            entries.append(t)
        holding = holds.draw(
            st.lists(st.integers(min_value=0, max_value=20_000), min_size=n, max_size=n),
            label="holding",
        )
        exits = [entries[i] + holding[i] for i in range(n)]
        span = compute_label_span(entries, exits)
        for split in cpcv_splits(n, n_groups=8, n_test_groups=2, label_span=span, embargo=span):
            for j in split.test.tolist():
                for i in split.train.tolist():
                    # Closed-interval time overlap of trade i and trade j.
                    overlaps = entries[j] <= exits[i] and entries[i] <= exits[j]
                    assert not overlaps, (
                        f"train trade {i} [{entries[i]}, {exits[i]}] overlaps test "
                        f"trade {j} [{entries[j]}, {exits[j]}] (span={span})"
                    )


class TestPurgeInvariant:
    @settings(max_examples=60, deadline=None)
    @given(
        n=st.integers(min_value=30, max_value=120),
        span=st.integers(min_value=0, max_value=8),
    )
    def test_no_train_index_inside_a_purge_window(self, n: int, span: int) -> None:
        # The OOF fit's exact split parameters (label_span == embargo == span,
        # the conservative trade-index law): every train index must sit
        # OUTSIDE [block_start − span, block_end + span + span] of every
        # contiguous test block — the AFML leakage-free discipline.
        for split in cpcv_splits(n, n_groups=8, n_test_groups=2, label_span=span, embargo=span):
            test = split.test.tolist()
            train = set(split.train.tolist())
            assert train.isdisjoint(test)
            blocks: list[tuple[int, int]] = []
            for idx in test:
                if blocks and idx == blocks[-1][1] + 1:
                    blocks[-1] = (blocks[-1][0], idx)
                else:
                    blocks.append((idx, idx))
            for start, end in blocks:
                lo = max(0, start - span)
                hi = min(n - 1, end + span + span)
                assert all(t < lo or t > hi for t in train)
