"""Unit gate for :mod:`brutex.meta.features` (DEC-META-LABEL-ANNOTATE-001).

The fixed documented feature vector: name/order pin, every one-hot bucket,
the vix missing/NaN normalisation, the label law, and the TOTAL-function
malformed-row matrix (every junk shape reads ``None``, never a raise).
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from brutex.engine.session_lens import SessionLens, TrendRegime
from brutex.meta.features import (
    FEATURE_NAMES,
    N_FEATURES,
    REGIME_TOKENS,
    trade_features_from_wire,
)

#: 2024-01-02 09:30 IST == 04:00 UTC.
ENTRY_ISO = "2024-01-02T04:00:00+00:00"
EXIT_ISO = "2024-01-02T05:00:00+00:00"
ENTRY_DATE = date(2024, 1, 2)

EMPTY_LENS = SessionLens(trend_by_date={}, event_flags_by_date={})


def wire_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "trade_seq": 0,
        "entry_ts": ENTRY_ISO,
        "exit_ts": EXIT_ISO,
        "pnl_paisa": 500,
        "vix_entry": 14.5,
    }
    row.update(overrides)
    return row


def feature(vector: tuple[float, ...], name: str) -> float:
    return vector[FEATURE_NAMES.index(name)]


class TestVectorContract:
    def test_feature_names_are_the_pinned_wire_tuple(self) -> None:
        assert FEATURE_NAMES == (
            "intercept",
            "vix_entry",
            "vix_missing",
            "hour_09",
            "hour_10",
            "hour_11",
            "hour_12",
            "hour_13",
            "hour_14",
            "hour_15",
            "hour_other",
            "regime_up",
            "regime_down",
            "regime_sideways",
            "regime_unknown",
            "event_day",
        )
        assert N_FEATURES == 16
        assert REGIME_TOKENS == ("up", "down", "sideways", "unknown")

    def test_happy_row_extracts_the_documented_vector(self) -> None:
        lens = SessionLens(
            trend_by_date={ENTRY_DATE: TrendRegime.UP},
            event_flags_by_date={ENTRY_DATE: ("expiry",)},
        )
        extracted = trade_features_from_wire(wire_row(), lens=lens)
        assert extracted is not None
        assert extracted.trade_seq == 0
        assert extracted.label_win is True
        assert extracted.regime == "up"
        assert extracted.vix_entry == 14.5
        assert extracted.event_day is True
        v = extracted.vector
        assert len(v) == N_FEATURES
        assert feature(v, "intercept") == 1.0
        assert feature(v, "vix_entry") == 14.5
        assert feature(v, "vix_missing") == 0.0
        assert feature(v, "hour_09") == 1.0
        assert feature(v, "hour_other") == 0.0
        assert feature(v, "regime_up") == 1.0
        assert feature(v, "regime_down") == 0.0
        assert feature(v, "event_day") == 1.0
        # Timestamps are exact epoch-µs (the W4 fold-key law: ts-keyed only).
        assert extracted.entry_ts_us == int(
            datetime(2024, 1, 2, 4, 0, tzinfo=UTC).timestamp() * 1_000_000
        )
        assert extracted.exit_ts_us > extracted.entry_ts_us

    @pytest.mark.parametrize(
        ("utc_hour", "expected"),
        [
            (4, "hour_09"),  # 09:30 IST
            (5, "hour_10"),
            (6, "hour_11"),
            (7, "hour_12"),
            (8, "hour_13"),
            (9, "hour_14"),
            (10, "hour_15"),  # 15:30 IST
            (13, "hour_other"),  # 18:30 IST — an evening Muhurat shape
            (2, "hour_other"),  # 07:30 IST — pre-open junk still buckets
        ],
    )
    def test_entry_hour_one_hot_buckets(self, utc_hour: int, expected: str) -> None:
        # +05:30 IST: UTC HH:00 → IST (HH+5):30 — e.g. 04:00 UTC = 09:30 IST.
        row = wire_row(entry_ts=f"2024-01-02T{utc_hour:02d}:00:00+00:00")
        extracted = trade_features_from_wire(row, lens=EMPTY_LENS)
        assert extracted is not None
        hour_names = [n for n in FEATURE_NAMES if n.startswith("hour_")]
        hot = [n for n in hour_names if feature(extracted.vector, n) == 1.0]
        assert hot == [expected]

    @pytest.mark.parametrize("regime", [TrendRegime.UP, TrendRegime.DOWN, TrendRegime.SIDEWAYS])
    def test_regime_one_hot(self, regime: TrendRegime) -> None:
        lens = SessionLens(trend_by_date={ENTRY_DATE: regime}, event_flags_by_date={})
        extracted = trade_features_from_wire(wire_row(), lens=lens)
        assert extracted is not None
        assert extracted.regime == regime.value
        assert feature(extracted.vector, f"regime_{regime.value}") == 1.0
        others = [t for t in REGIME_TOKENS if t != regime.value]
        assert all(feature(extracted.vector, f"regime_{t}") == 0.0 for t in others)

    def test_lens_miss_reads_unknown_regime_and_no_event(self) -> None:
        extracted = trade_features_from_wire(wire_row(), lens=EMPTY_LENS)
        assert extracted is not None
        assert extracted.regime == "unknown"
        assert feature(extracted.vector, "regime_unknown") == 1.0
        assert extracted.event_day is False
        assert feature(extracted.vector, "event_day") == 0.0

    def test_label_rule_zero_pnl_is_a_loss(self) -> None:
        # Conservative: a scratch trade (pnl == 0) labels as a LOSS.
        for pnl, expected in ((500, True), (0, False), (-1, False)):
            extracted = trade_features_from_wire(wire_row(pnl_paisa=pnl), lens=EMPTY_LENS)
            assert extracted is not None
            assert extracted.label_win is expected


class TestVixHandling:
    def test_missing_vix_reads_zero_plus_indicator(self) -> None:
        extracted = trade_features_from_wire(wire_row(vix_entry=None), lens=EMPTY_LENS)
        assert extracted is not None
        assert extracted.vix_entry is None
        assert feature(extracted.vector, "vix_entry") == 0.0
        assert feature(extracted.vector, "vix_missing") == 1.0

    @pytest.mark.parametrize("junk", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_vix_normalises_to_missing(self, junk: float) -> None:
        extracted = trade_features_from_wire(wire_row(vix_entry=junk), lens=EMPTY_LENS)
        assert extracted is not None
        assert extracted.vix_entry is None
        assert feature(extracted.vector, "vix_missing") == 1.0

    def test_int_vix_accepted_bool_rejected_to_missing(self) -> None:
        extracted = trade_features_from_wire(wire_row(vix_entry=14), lens=EMPTY_LENS)
        assert extracted is not None
        assert extracted.vix_entry == 14.0
        extracted = trade_features_from_wire(wire_row(vix_entry=True), lens=EMPTY_LENS)
        assert extracted is not None
        assert extracted.vix_entry is None

    def test_junk_typed_vix_reads_missing(self) -> None:
        extracted = trade_features_from_wire(wire_row(vix_entry="14.5"), lens=EMPTY_LENS)
        assert extracted is not None
        assert extracted.vix_entry is None


class TestTotalityMatrix:
    """Every malformed shape reads ``None`` — counted skip, never a raise."""

    @pytest.mark.parametrize(
        "row",
        [
            {},  # everything missing
            wire_row(trade_seq=None),
            wire_row(trade_seq="0"),
            wire_row(trade_seq=True),  # bool is not a count
            wire_row(pnl_paisa=None),
            wire_row(pnl_paisa=5.0),
            wire_row(pnl_paisa=True),
            wire_row(entry_ts=None),
            wire_row(entry_ts=12.5),  # junk type (not str/int)
            wire_row(entry_ts="not-a-timestamp"),
            wire_row(entry_ts="2024-01-02T04:00:00"),  # NAIVE — date-shift hazard
            wire_row(exit_ts="junk"),
            wire_row(exit_ts=None),
        ],
    )
    def test_malformed_rows_read_none(self, row: dict[str, object]) -> None:
        assert trade_features_from_wire(row, lens=EMPTY_LENS) is None

    def test_exact_epoch_us_ints_accepted(self) -> None:
        entry_us = int(datetime(2024, 1, 2, 4, 0, tzinfo=UTC).timestamp() * 1_000_000)
        row = wire_row(entry_ts=entry_us, exit_ts=entry_us + 60_000_000)
        extracted = trade_features_from_wire(row, lens=EMPTY_LENS)
        assert extracted is not None
        assert extracted.entry_ts_us == entry_us

    def test_bool_timestamp_reads_none(self) -> None:
        # bool is an int subtype — it must NOT pass as epoch-µs (True → 1 µs
        # would silently read 1970-01-01).
        assert trade_features_from_wire(wire_row(entry_ts=True), lens=EMPTY_LENS) is None

    def test_out_of_datetime_range_us_reads_none(self) -> None:
        # 2**62 µs ≈ year 148108 — past datetime.max (the #1919 stamp class).
        assert (
            trade_features_from_wire(wire_row(entry_ts=2**62, exit_ts=2**62 + 1), lens=EMPTY_LENS)
            is None
        )
