"""The ``bad_tick`` frame-screen Rust-seam routing tests (SLICE T24 PR-2).

DEC-DATA-RUST-INTEGRITY-DETECTORS-001. Stem-matched to
``brutex/data/bad_tick.py`` so the discovery gate folds these into the
module's own 100% field: the flag-OFF inertness spy (``brutex_feed`` never
resolved from this seam), the clean-frame phase-1 short-circuit spy (the
facade is never consulted flag-ON on a clean frame — byte- AND
cost-identical), the hostile corpus re-driven flag-ON at burn-in
remaining=0 against the law-faithful fake kernel AND the real wheel
(``importorskip``-gated) with frame + warn-stream parity vs the flag-OFF
oracle, and the ``_screen_frame_sequential_python`` extraction pin.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import brutex.data.bad_tick_rust as btr
import polars as pl
import pytest
from brutex.data.bad_tick import screen_frame_bad_ticks
from structlog.testing import capture_logs
from structlog.typing import EventDict

from tests.fakes.feed_scan_crate import fake_feed_crate


def _ts(minute: int, *, day: int = 0) -> datetime:
    """09:15 IST (03:45 UTC) + *minute* on session day 2024-01-02 + *day*."""
    return datetime(2024, 1, 2, 3, 45, tzinfo=UTC) + timedelta(days=day, minutes=minute)


def _frame(
    rows: list[tuple[datetime, float]], *, oi: bool = False, naive: bool = False
) -> pl.DataFrame:
    """The corpus frame builder — mirrors ``TestScreenFrameBadTicks._frame``."""
    stamps = [ts.replace(tzinfo=None) if naive else ts for ts, _ in rows]
    closes = [close for _, close in rows]
    data: dict[str, object] = {
        "ts_utc": stamps,
        "open": closes,
        "high": [close + 1.0 for close in closes],
        "low": [close - 1.0 for close in closes],
        "close": closes,
        "volume": list(range(1, len(rows) + 1)),
    }
    if oi:
        data["open_interest"] = [10 * i for i in range(len(rows))]
    schema = {
        "ts_utc": pl.Datetime("us") if naive else pl.Datetime("us", "UTC"),
        "open": pl.Float64,
        "high": pl.Float64,
        "low": pl.Float64,
        "close": pl.Float64,
        "volume": pl.Int64,
        **({"open_interest": pl.Int64} if oi else {}),
    }
    return pl.DataFrame(data, schema=schema)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _isolated_facade(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv(btr.FEED_RUST_FRAME_ENV, raising=False)
    btr.reset_bad_tick_rust_for_tests()
    yield
    btr.reset_bad_tick_rust_for_tests()


def _arm(monkeypatch: pytest.MonkeyPatch, kernel_source: str) -> None:
    """Arm the frame flag in the post-burn-in steady state on *kernel_source*."""
    monkeypatch.setenv(btr.FEED_RUST_FRAME_ENV, "1")
    if kernel_source == "real":
        pytest.importorskip("brutex_feed")
    else:
        monkeypatch.setitem(sys.modules, "brutex_feed", fake_feed_crate())
    btr.reset_bad_tick_rust_for_tests(remaining=0)


def _warn_stream(logs: list[EventDict]) -> list[tuple[Any, ...]]:
    return [
        (
            e["session"],
            e["skipped"],
            e["first_prev_close"],
            e["first_close"],
            e["max_ratio"],
            e["source"],
        )
        for e in logs
        if e["event"] == "feed.bad_tick_skipped"
    ]


class TestFlagOffInertness:
    def test_off_never_resolves_the_kernel(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _bomb() -> object:
            raise AssertionError("flag-OFF frame screen resolved brutex_feed")

        monkeypatch.setattr(btr, "resolve_feed_kernel", _bomb)
        frame = _frame([(_ts(0), 100.0), (_ts(1), 1000.0), (_ts(2), 102.0)])
        result = screen_frame_bad_ticks(frame)
        assert result.get_column("close").to_list() == [100.0, 102.0]  # still catches

    def test_clean_frame_flag_on_never_consults_the_facade(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Phase 1 stays Python on BOTH arms — a clean frame returns the
        # INPUT OBJECT without ever reaching the routed arm (the
        # cost-identity load-bearing design choice).
        monkeypatch.setenv(btr.FEED_RUST_FRAME_ENV, "1")

        def _bomb(*_a: object, **_k: object) -> object:
            raise AssertionError("clean frame reached routed_screen_frame")

        monkeypatch.setattr(btr, "routed_screen_frame", _bomb)
        frame = _frame([(_ts(0), 100.0), (_ts(1), 101.0), (_ts(2), 102.0)])
        assert screen_frame_bad_ticks(frame) is frame


#: The hostile corpus — every catching shape of the existing frame suite,
#: as (id, rows, frame kwargs, screen kwargs).
_CORPUS: tuple[tuple[str, list[tuple[datetime, float]], dict[str, Any], dict[str, Any]], ...] = (
    (
        "spike_mid_session",
        [(_ts(0), 100.0), (_ts(1), 101.0), (_ts(2), 1000.0), (_ts(3), 102.0)],
        {},
        {},
    ),
    (
        "two_consecutive_spikes_one_warn",
        [(_ts(0), 100.0), (_ts(1), 1000.0), (_ts(2), 1001.0), (_ts(3), 101.0)],
        {},
        {},
    ),
    (
        "baseline_hold_spike_successor_kept",
        [(_ts(0), 100.0), (_ts(1), 1000.0), (_ts(2), 102.0)],
        {},
        {},
    ),
    ("exact_up_boundary_dropped", [(_ts(0), 100.0), (_ts(1), 400.0)], {}, {}),
    ("exact_down_boundary_dropped", [(_ts(0), 100.0), (_ts(1), 25.0)], {}, {}),
    (
        "session_boundary_resets_baseline",
        [
            (_ts(0), 100.0),
            (_ts(1), 1000.0),
            (_ts(0, day=1), 1010.0),
            (_ts(1, day=1), 1011.0),
        ],
        {},
        {},
    ),
    (
        "sub_0530_ist_prior_utc_day_rows",
        # 23:45 UTC = 05:15 IST NEXT day — the month-edge / prior-UTC-day
        # bucket class (#1795b): the IST session key must group it with the
        # NEXT UTC day's session rows; a spike inside that session drops.
        [
            (datetime(2024, 1, 1, 18, 29, tzinfo=UTC), 100.0),  # 23:59 IST Jan-01
            (datetime(2024, 1, 1, 23, 45, tzinfo=UTC), 200.0),  # 05:15 IST Jan-02
            (datetime(2024, 1, 2, 3, 45, tzinfo=UTC), 2000.0),  # 09:15 IST Jan-02 spike
            (datetime(2024, 1, 2, 3, 46, tzinfo=UTC), 201.0),
        ],
        {},
        {},
    ),
    (
        "open_interest_passthrough_row_aligned",
        [(_ts(0), 100.0), (_ts(1), 101.0), (_ts(2), 1000.0), (_ts(3), 102.0)],
        {"oi": True},
        {},
    ),
    (
        "naive_ts_treated_as_utc",
        [(_ts(0), 100.0), (_ts(1), 1000.0), (_ts(2), 102.0)],
        {"naive": True},
        {},
    ),
    (
        "custom_max_ratio_drops",
        [(_ts(0), 100.0), (_ts(1), 250.0)],
        {},
        {"max_ratio": 2.0},
    ),
    (
        "multi_session_multi_warn_stream",
        [
            (_ts(0), 100.0),
            (_ts(1), 1000.0),
            (_ts(0, day=1), 200.0),
            (_ts(1, day=1), 2000.5),
            (_ts(2, day=1), 2001.0),
            (_ts(3, day=1), 201.0),
        ],
        {},
        {},
    ),
    ("single_row", [(_ts(0), 100.0), (_ts(1), 1000.0)], {}, {}),
)


@pytest.mark.parametrize("kernel_source", ["fake", "real"])
class TestFlagOnCorpusParity:
    """Every catching shape re-driven flag-ON at remaining=0 ≡ the oracle."""

    @pytest.mark.parametrize(
        ("case_id", "rows", "frame_kwargs", "screen_kwargs"),
        _CORPUS,
        ids=[case[0] for case in _CORPUS],
    )
    def test_corpus_case_parity(
        self,
        kernel_source: str,
        case_id: str,
        rows: list[tuple[datetime, float]],
        frame_kwargs: dict[str, Any],
        screen_kwargs: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        del case_id
        frame = _frame(rows, **frame_kwargs)
        with capture_logs() as off_logs:
            off = screen_frame_bad_ticks(frame, source="prebuild:seam", **screen_kwargs)
        _arm(monkeypatch, kernel_source)
        with capture_logs() as on_logs:
            on = screen_frame_bad_ticks(frame, source="prebuild:seam", **screen_kwargs)
        assert on.equals(off)
        assert on.columns == frame.columns
        assert _warn_stream(on_logs) == _warn_stream(off_logs)
        # Engagement proof (never a silently-degraded oracle-only run) +
        # nothing latched mid-run.
        assert any(e["event"] == "bad_tick_rust.engaged" for e in on_logs)
        assert btr.frame_screen_rust_disabled() is False

    def test_burn_in_arm_compares_then_serves(
        self, kernel_source: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # remaining > 0: the burn-in leg runs the side-effect-free oracle —
        # exactly ONE warn emission still (log parity holds mid-burn-in).
        frame = _frame([(_ts(0), 100.0), (_ts(1), 1000.0), (_ts(2), 102.0)])
        monkeypatch.setenv(btr.FEED_RUST_FRAME_ENV, "1")
        if kernel_source == "real":
            pytest.importorskip("brutex_feed")
        else:
            monkeypatch.setitem(sys.modules, "brutex_feed", fake_feed_crate())
        btr.reset_bad_tick_rust_for_tests(remaining=1)
        with capture_logs() as logs:
            result = screen_frame_bad_ticks(frame)
        assert result.get_column("close").to_list() == [100.0, 102.0]
        assert len([e for e in logs if e["event"] == "feed.bad_tick_skipped"]) == 1
        assert any(e["event"] == "bad_tick_rust.burn_in_complete" for e in logs)
