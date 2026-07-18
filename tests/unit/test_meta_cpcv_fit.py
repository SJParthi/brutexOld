"""Unit gate for :mod:`brutex.meta.cpcv_fit` (DEC-META-LABEL-ANNOTATE-001).

The conservative trade-index label-span law, the train-only vix
standardization, the degenerate-honesty arms (``None``, never a fabricated
probability), split-skip accounting, determinism, and input-order
invariance of the out-of-fold column.
"""

from __future__ import annotations

import struct

import pytest
from brutex.meta.cpcv_fit import (
    MIN_TRADES_FOR_FIT,
    OOF_N_GROUPS,
    OOF_N_TEST_GROUPS,
    REASON_SINGLE_CLASS,
    REASON_TOO_FEW_TRADES,
    _standardize_vix,
    compute_label_span,
    oof_p_win,
)
from brutex.meta.features import FEATURE_NAMES, N_FEATURES, TradeFeatures

VIX_COL = FEATURE_NAMES.index("vix_entry")
VIX_MISSING_COL = FEATURE_NAMES.index("vix_missing")
DAY_US = 86_400_000_000


def trade(
    seq: int,
    *,
    entry_us: int,
    exit_us: int | None = None,
    win: bool,
    vix: float = 14.0,
) -> TradeFeatures:
    vector = [0.0] * N_FEATURES
    vector[0] = 1.0
    vector[VIX_COL] = vix
    # A weakly informative signal column so separable fits exist.
    vector[FEATURE_NAMES.index("hour_09")] = 1.0 if win else 0.0
    vector[FEATURE_NAMES.index("hour_10")] = 0.0 if win else 1.0
    return TradeFeatures(
        trade_seq=seq,
        entry_ts_us=entry_us,
        exit_ts_us=exit_us if exit_us is not None else entry_us + 60_000_000,
        vector=tuple(vector),
        label_win=win,
        prev_regime="unknown",
        vix_entry=vix,
        event_day=False,
    )


def non_overlapping(n: int, *, losses_first: int = 0) -> list[TradeFeatures]:
    """*n* trades, one per day, exits well before the next entry."""
    return [
        trade(
            i,
            entry_us=1_700_000_000_000_000 + i * DAY_US,
            win=i >= losses_first if losses_first else i % 2 == 0,
            vix=10.0 + (i % 7),
        )
        for i in range(n)
    ]


class TestComputeLabelSpan:
    def test_non_overlapping_trades_span_zero(self) -> None:
        trades = non_overlapping(5)
        entries = [t.entry_ts_us for t in trades]
        exits = [t.exit_ts_us for t in trades]
        assert compute_label_span(entries, exits) == 0

    def test_overlapping_spans_count_later_entries(self) -> None:
        # Trade 0 holds across the next two entries; trade 1 across one.
        entries = [0, 10, 20, 1_000]
        exits = [25, 15, 30, 1_001]
        assert compute_label_span(entries, exits) == 2

    def test_boundary_entry_at_exit_counts(self) -> None:
        # A later entry EXACTLY at an exit instant overlaps (inclusive span).
        entries = [0, 100]
        exits = [100, 200]
        assert compute_label_span(entries, exits) == 1

    def test_length_mismatch_refuses(self) -> None:
        with pytest.raises(ValueError, match="entries"):
            compute_label_span([0, 1], [2])

    def test_unsorted_entries_refuse(self) -> None:
        with pytest.raises(ValueError, match="ascending"):
            compute_label_span([10, 0], [11, 1])


def _present_row(vix: float) -> list[float]:
    """A feature row with a PRESENT vix stamp (`vix_missing` indicator 0)."""
    row = [1.0] * N_FEATURES
    row[VIX_COL] = vix
    row[VIX_MISSING_COL] = 0.0
    return row


def _missing_row() -> list[float]:
    """A feature row with the MISSING encoding: vix 0.0 + indicator 1.0."""
    row = [1.0] * N_FEATURES
    row[VIX_COL] = 0.0
    row[VIX_MISSING_COL] = 1.0
    return row


class TestStandardizeVix:
    def test_train_only_statistics(self) -> None:
        matrix = [_present_row(v) for v in (10.0, 20.0, 30.0, 1000.0)]
        out = _standardize_vix(matrix, [0, 1, 2])  # the 1000.0 row is TEST
        # Train mean 20, population std over {10,20,30}.
        std = ((100 + 0 + 100) / 3) ** 0.5
        assert out[0][VIX_COL] == pytest.approx((10.0 - 20.0) / std)
        assert out[3][VIX_COL] == pytest.approx((1000.0 - 20.0) / std)
        # Input never mutated; other columns untouched.
        assert matrix[0][VIX_COL] == 10.0
        assert out[0][0] == 1.0

    def test_placeholder_rows_excluded_from_stats_and_untouched(self) -> None:
        # The round-1 M-6 law: a missing-vix TRAIN row's 0.0 placeholder is
        # (a) EXCLUDED from the mean/std and (b) NEVER shifted by them.
        matrix = [_present_row(10.0), _missing_row(), _present_row(30.0), _missing_row()]
        out = _standardize_vix(matrix, [0, 1, 2])  # rows 0-2 TRAIN, 3 TEST
        # Stats over the PRESENT train rows {10, 30} only: mean 20, std 10.
        assert out[0][VIX_COL] == pytest.approx(-1.0)
        assert out[2][VIX_COL] == pytest.approx(1.0)
        # Missing rows (train AND test) keep exactly 0.0 + the indicator.
        assert out[1][VIX_COL] == 0.0
        assert out[1][VIX_MISSING_COL] == 1.0
        assert out[3][VIX_COL] == 0.0
        assert out[3][VIX_MISSING_COL] == 1.0

    def test_all_missing_train_fold_is_the_identity_arm(self) -> None:
        # The documented degenerate: no present train row → mean 0 / std 1
        # equivalent — the column ships raw (nothing to scale by).
        matrix = [_missing_row(), _missing_row(), _present_row(50.0)]
        out = _standardize_vix(matrix, [0, 1])  # every TRAIN row missing
        assert out == matrix
        assert out is not matrix
        assert out[2][VIX_COL] == 50.0  # the test row also stays raw

    def test_zero_std_leaves_the_column_raw(self) -> None:
        matrix = [_present_row(5.0), _present_row(5.0)]
        out = _standardize_vix(matrix, [0, 1])
        assert out[0][VIX_COL] == 5.0

    def test_empty_train_rows_copies_raw(self) -> None:
        matrix = [_present_row(2.0)]
        out = _standardize_vix(matrix, [])
        assert out == matrix
        assert out is not matrix


class TestDegenerateArms:
    def test_too_few_trades_reads_all_none(self) -> None:
        trades = non_overlapping(MIN_TRADES_FOR_FIT - 1)
        result = oof_p_win(trades, scorer=None)
        assert result.p_win == (None,) * len(trades)
        assert result.degenerate_reason == REASON_TOO_FEW_TRADES
        assert result.n_splits_fit == 0

    def test_empty_trades_reads_empty(self) -> None:
        result = oof_p_win([])
        assert result.p_win == ()
        assert result.degenerate_reason == REASON_TOO_FEW_TRADES

    def test_single_class_reads_all_none(self) -> None:
        trades = [
            trade(i, entry_us=1_700_000_000_000_000 + i * DAY_US, win=True)
            for i in range(MIN_TRADES_FOR_FIT)
        ]
        result = oof_p_win(trades)
        assert result.p_win == (None,) * len(trades)
        assert result.degenerate_reason == REASON_SINGLE_CLASS
        assert result.label_span == 0

    def test_total_overlap_purges_every_train_set(self) -> None:
        # Every trade holds across every later entry → span = n-1 → every
        # split's purge window swallows the whole series → all skipped,
        # every p_win an honest None (no fabricated coverage).
        n = MIN_TRADES_FOR_FIT
        trades = [
            trade(
                i,
                entry_us=1_700_000_000_000_000 + i * 1_000_000,
                exit_us=1_700_000_000_000_000 + (n + 1) * 1_000_000,
                win=i % 2 == 0,
            )
            for i in range(n)
        ]
        result = oof_p_win(trades, n_groups=2, n_test_groups=1)
        assert result.label_span == n - 1
        assert result.n_splits_fit == 0
        assert result.n_splits_skipped == 2
        assert result.p_win == (None,) * n


class TestOofColumn:
    def test_full_coverage_on_a_healthy_stream(self) -> None:
        trades = non_overlapping(40)
        result = oof_p_win(trades)
        assert result.degenerate_reason is None
        assert result.n_splits_fit + result.n_splits_skipped == 28  # C(8, 2)
        assert result.n_splits_fit > 0
        assert all(p is not None and 0.0 <= p <= 1.0 for p in result.p_win)
        assert result.embargo == result.label_span

    def test_single_class_train_splits_are_skipped_and_counted(self) -> None:
        # All 4 losses live in the FIRST time-group (of 8 over 32 trades):
        # every split with group 0 in TEST trains single-class → skipped.
        trades = non_overlapping(32, losses_first=4)
        result = oof_p_win(trades)
        assert result.degenerate_reason is None
        assert result.n_splits_skipped == 7  # C(7,1) splits carry group 0
        assert result.n_splits_fit == 21

    def test_deterministic_bit_for_bit(self) -> None:
        trades = non_overlapping(40)
        a = oof_p_win(trades)
        b = oof_p_win(trades)
        packed_a = struct.pack(f"<{len(a.p_win)}d", *[p or 0.0 for p in a.p_win])
        packed_b = struct.pack(f"<{len(b.p_win)}d", *[p or 0.0 for p in b.p_win])
        assert packed_a == packed_b

    def test_input_order_invariance(self) -> None:
        trades = non_overlapping(40)
        shuffled = trades[::-1]
        by_seq_a = dict(zip([t.trade_seq for t in trades], oof_p_win(trades).p_win, strict=True))
        by_seq_b = dict(
            zip([t.trade_seq for t in shuffled], oof_p_win(shuffled).p_win, strict=True)
        )
        assert by_seq_a == by_seq_b

    def test_scorer_seam_receives_the_standardized_test_rows(self) -> None:
        calls: list[tuple[int, int]] = []

        def recording_scorer(
            flat: list[float], n_features: int, weights: list[float], bias: float
        ) -> list[float]:
            calls.append((len(flat), n_features))
            assert len(weights) == n_features
            assert bias == 0.0
            return [0.5] * (len(flat) // n_features)

        trades = non_overlapping(40)
        result = oof_p_win(trades, scorer=recording_scorer)
        assert calls, "the injected scorer was never consulted"
        assert all(n == N_FEATURES for _, n in calls)
        assert all(p == 0.5 for p in result.p_win)

    def test_default_grid_constants(self) -> None:
        assert (OOF_N_GROUPS, OOF_N_TEST_GROUPS) == (8, 2)

    def test_failed_fits_skip_and_count_every_split(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A numerically failed solve (fit_logistic → None) skips the split —
        # its test trades read an honest None, never a stale/fabricated p.
        import brutex.meta.cpcv_fit as cpcv_fit_module

        monkeypatch.setattr(cpcv_fit_module, "fit_logistic", lambda *a, **k: None)
        trades = non_overlapping(40)
        result = oof_p_win(trades)
        assert result.n_splits_fit == 0
        assert result.n_splits_skipped == 28
        assert result.p_win == (None,) * 40
        assert result.degenerate_reason is None
