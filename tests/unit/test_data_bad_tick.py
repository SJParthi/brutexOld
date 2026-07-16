"""Unit tests for the bounded bad-tick screen (``brutex.data.bad_tick``).

100% line + branch coverage of the spike / fat-finger / vendor-scaling guard at
the engine read boundary (DEC-SWEEP-DATA-EDGE-GUARDS-001, D-MAJ-2). The
end-to-end wiring through :func:`arrow_table_to_bars` is exercised in
``tests/integration/test_feed_replay_bad_tick_screen.py`` (stem-named
``test_feed_replay*`` so the coverage-gate discovery maps it to
``brutex/feed/replay.py``).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from brutex.data.bad_tick import (
    BAD_TICK_MAX_RATIO,
    BAD_TICK_SKIP_EVENT,
    BadTickScreen,
    bad_tick_screen_enabled,
    emit_bad_tick_warn,
    is_bad_tick,
    screen_frame_bad_ticks,
)
from structlog.testing import capture_logs

# ────────────────────────────────────────────────────────────────────────
# bad_tick_screen_enabled — default ON, falsy kill switch
# ────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("raw", ["0", "false", "FALSE", " no ", "Off", "off"])
def test_disabled_only_for_explicit_falsy(raw: str) -> None:
    assert bad_tick_screen_enabled(raw) is False


@pytest.mark.parametrize("raw", ["", "1", "true", "yes", "on", "anything", "  "])
def test_enabled_for_everything_else(raw: str) -> None:
    assert bad_tick_screen_enabled(raw) is True


def test_enabled_default_true_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BRUTEX_BAD_TICK_SCREEN", raising=False)
    assert bad_tick_screen_enabled() is True


def test_enabled_reads_env_when_raw_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRUTEX_BAD_TICK_SCREEN", "off")
    assert bad_tick_screen_enabled() is False


# ────────────────────────────────────────────────────────────────────────
# is_bad_tick — the pure bounded-ratio predicate
# ────────────────────────────────────────────────────────────────────────


def test_scaled_down_by_ten_is_bad() -> None:
    # 27940 -> 2794 (÷10): ratio 0.1 < 1/4 → bad.
    assert is_bad_tick(27940.0, 2794.0) is True


def test_scaled_up_by_ten_is_bad() -> None:
    # 27940 -> 279400 (×10): ratio 10 >= 4 → bad.
    assert is_bad_tick(27940.0, 279400.0) is True


def test_normal_minute_move_is_good() -> None:
    # A ~0.04% minute move is nowhere near the 4x band.
    assert is_bad_tick(27940.0, 27950.0) is False


def test_exact_boundary_is_bad_both_directions() -> None:
    # Exactly max_ratio (>= / <=) counts as bad.
    assert is_bad_tick(100.0, 400.0) is True  # up: 400 >= 100*4
    assert is_bad_tick(400.0, 100.0) is True  # down: 100*4 <= 400


def test_just_inside_boundary_is_good() -> None:
    assert is_bad_tick(100.0, 399.99) is False
    assert is_bad_tick(400.0, 100.01) is False


def test_custom_max_ratio() -> None:
    assert is_bad_tick(100.0, 250.0, max_ratio=2.0) is True
    assert is_bad_tick(100.0, 250.0, max_ratio=3.0) is False


def test_default_max_ratio_constant() -> None:
    assert BAD_TICK_MAX_RATIO == 4.0


# ────────────────────────────────────────────────────────────────────────
# BadTickScreen — stateful single-pass in-session screen
# ────────────────────────────────────────────────────────────────────────

_D1 = date(2024, 1, 2)
_D2 = date(2024, 1, 3)


def test_first_bar_of_session_always_kept() -> None:
    screen = BadTickScreen()
    # No prior in-session close → exempt, even a wild value.
    assert screen.keep(ist_date=_D1, close=999999.0) is True


def test_reopen_gap_across_sessions_never_trips() -> None:
    screen = BadTickScreen()
    assert screen.keep(ist_date=_D1, close=27940.0) is True
    # New session opens far away — first bar of D2 is exempt (no false trip).
    assert screen.keep(ist_date=_D2, close=2000.0) is True


def test_bad_tick_skipped_and_baseline_holds_last_good_close() -> None:
    screen = BadTickScreen()
    assert screen.keep(ist_date=_D1, close=27940.0) is True
    # ÷10 scaled bar → skipped (False).
    assert screen.keep(ist_date=_D1, close=2794.0) is False
    # The NEXT good bar is judged against the last KEPT close (27940), not the
    # skipped spike — so a lone spike cannot cascade-flag its healthy successor.
    assert screen.keep(ist_date=_D1, close=27950.0) is True


def test_two_consecutive_spikes_both_skipped_one_warn() -> None:
    with capture_logs() as logs:
        screen = BadTickScreen(source="NSE/INDEX/NSE-NIFTY/1minute")
        screen.keep(ist_date=_D1, close=27940.0)  # kept baseline
        assert screen.keep(ist_date=_D1, close=2794.0) is False  # spike 1
        assert screen.keep(ist_date=_D1, close=2795.0) is False  # spike 2 (vs 27940)
        screen.finish()
    warns = [e for e in logs if e.get("event") == BAD_TICK_SKIP_EVENT]
    assert len(warns) == 1
    assert warns[0]["skipped"] == 2
    assert warns[0]["session"] == "2024-01-02"
    assert warns[0]["source"] == "NSE/INDEX/NSE-NIFTY/1minute"
    assert warns[0]["first_prev_close"] == 27940.0
    assert warns[0]["first_close"] == 2794.0
    assert warns[0]["max_ratio"] == BAD_TICK_MAX_RATIO


def test_one_warn_per_session_flushed_at_session_boundary() -> None:
    with capture_logs() as logs:
        screen = BadTickScreen()
        screen.keep(ist_date=_D1, close=27940.0)
        screen.keep(ist_date=_D1, close=2794.0)  # D1 spike
        # Crossing into D2 flushes the D1 warn.
        screen.keep(ist_date=_D2, close=27000.0)
        screen.keep(ist_date=_D2, close=2700.0)  # D2 spike
        screen.finish()
    warns = [e for e in logs if e.get("event") == BAD_TICK_SKIP_EVENT]
    assert [w["session"] for w in warns] == ["2024-01-02", "2024-01-03"]
    assert all(w["skipped"] == 1 for w in warns)


def test_clean_session_emits_no_warn() -> None:
    with capture_logs() as logs:
        screen = BadTickScreen()
        screen.keep(ist_date=_D1, close=27940.0)
        screen.keep(ist_date=_D1, close=27950.0)
        screen.finish()
    assert not [e for e in logs if e.get("event") == BAD_TICK_SKIP_EVENT]


def test_finish_on_empty_screen_is_noop() -> None:
    with capture_logs() as logs:
        BadTickScreen().finish()  # never fed a bar → _session None, no warn
    assert not [e for e in logs if e.get("event") == BAD_TICK_SKIP_EVENT]


def test_injected_logger_is_used() -> None:
    events: list[tuple[str, dict[str, object]]] = []

    class _FakeLog:
        def warning(self, event: str, **kw: object) -> None:
            events.append((event, kw))

    screen = BadTickScreen(source="src", log=_FakeLog())  # type: ignore[arg-type]
    screen.keep(ist_date=_D1, close=100.0)
    screen.keep(ist_date=_D1, close=10.0)  # ÷10 spike
    screen.finish()
    assert events == [
        (
            BAD_TICK_SKIP_EVENT,
            {
                "source": "src",
                "session": "2024-01-02",
                "skipped": 1,
                "max_ratio": BAD_TICK_MAX_RATIO,
                "first_prev_close": 100.0,
                "first_close": 10.0,
            },
        )
    ]


# ────────────────────────────────────────────────────────────────────────
# emit_bad_tick_warn — the ONE shared WARN seam (DEC-FEED-RUST-CORE-001)
# ────────────────────────────────────────────────────────────────────────


def test_emit_bad_tick_warn_exact_kwargs_injected_logger() -> None:
    events: list[tuple[str, dict[str, object]]] = []

    class _FakeLog:
        def warning(self, event: str, **kw: object) -> None:
            events.append((event, kw))

    emit_bad_tick_warn(
        source="NSE/INDEX/NSE-NIFTY/1minute",
        session_iso="2024-01-02",
        skipped=2,
        max_ratio=BAD_TICK_MAX_RATIO,
        first_prev_close=27945.0,
        first_close=2794.5,
        log=_FakeLog(),  # type: ignore[arg-type]
    )
    assert events == [
        (
            BAD_TICK_SKIP_EVENT,
            {
                "source": "NSE/INDEX/NSE-NIFTY/1minute",
                "session": "2024-01-02",
                "skipped": 2,
                "max_ratio": BAD_TICK_MAX_RATIO,
                "first_prev_close": 27945.0,
                "first_close": 2794.5,
            },
        )
    ]


def test_emit_bad_tick_warn_default_logger_record_shape() -> None:
    with capture_logs() as logs:
        emit_bad_tick_warn(
            source=None,
            session_iso=None,
            skipped=1,
            max_ratio=2.0,
            first_prev_close=100.0,
            first_close=10.0,
        )
    assert logs == [
        {
            "event": BAD_TICK_SKIP_EVENT,
            "log_level": "warning",
            "source": None,
            "session": None,
            "skipped": 1,
            "max_ratio": 2.0,
            "first_prev_close": 100.0,
            "first_close": 10.0,
        }
    ]


def test_flush_delegates_to_emit_bad_tick_warn_byte_equivalently() -> None:
    # The screen's _flush record and a DIRECT emit with the same coordinates
    # are the SAME dict — the delegation-equivalence law both arms of the
    # DEC-FEED-RUST-CORE-001 seam rely on.
    with capture_logs() as via_screen:
        screen = BadTickScreen(source="src")
        screen.keep(ist_date=date(2024, 1, 2), close=100.0)
        screen.keep(ist_date=date(2024, 1, 2), close=10.0)
        screen.finish()
    with capture_logs() as direct:
        emit_bad_tick_warn(
            source="src",
            session_iso="2024-01-02",
            skipped=1,
            max_ratio=BAD_TICK_MAX_RATIO,
            first_prev_close=100.0,
            first_close=10.0,
        )
    assert via_screen == direct
# screen_frame_bad_ticks — the frame form (DEC-READ-GUARD-PARITY-001)
# ────────────────────────────────────────────────────────────────────────


class TestScreenFrameBadTicks:
    """Frame twin of :class:`BadTickScreen` at the prebuild 1m read seam.

    The sequential :class:`BadTickScreen` arm is the single source of truth
    for WHICH rows drop + the WARN semantics; the vectorized candidate
    trigger only decides whether the sequential arm runs at all — a clean
    frame must come back as the INPUT OBJECT (zero-copy). Requires polars
    (skipped wheel-free — the scalar tests above stay dependency-free).
    """

    @staticmethod
    def _frame(
        rows: list[tuple[datetime, float]],
        *,
        oi: bool = False,
        naive: bool = False,
    ) -> Any:
        """Build a sorted ts_utc/OHLCV(+open_interest) frame from (ts, close) rows."""
        pl = pytest.importorskip("polars")
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

    @staticmethod
    def _ts(minute: int, *, day: int = 0) -> datetime:
        """09:15 IST (03:45 UTC) + *minute* on session day 2024-01-02 + *day*."""
        return datetime(2024, 1, 2, 3, 45, tzinfo=UTC) + timedelta(days=day, minutes=minute)

    def test_spike_row_dropped_mid_session(self) -> None:
        frame = self._frame(
            [
                (self._ts(0), 100.0),
                (self._ts(1), 101.0),
                (self._ts(2), 1000.0),
                (self._ts(3), 102.0),
            ]
        )
        result = screen_frame_bad_ticks(frame)
        assert result.get_column("close").to_list() == [100.0, 101.0, 102.0]

    def test_first_bar_of_session_always_kept(self) -> None:
        # A wild ABSOLUTE first bar has no baseline → exempt; the screen still
        # judges LATER in-session bars against it (the ÷4 down-spike drops).
        frame = self._frame(
            [(self._ts(0), 999999.0), (self._ts(1), 999990.0), (self._ts(2), 240.0)]
        )
        result = screen_frame_bad_ticks(frame)
        assert result.get_column("close").to_list() == [999999.0, 999990.0]

    def test_reopen_gap_across_sessions_never_trips(self) -> None:
        # Day-1 closes at ~100; day-2 reopens ×10 away — first bar of the new
        # session is exempt, so the whole frame is clean → identity return.
        frame = self._frame(
            [
                (self._ts(0), 100.0),
                (self._ts(1), 101.0),
                (self._ts(0, day=1), 1000.0),
                (self._ts(1, day=1), 1001.0),
            ]
        )
        result = screen_frame_bad_ticks(frame)
        assert result is frame

    def test_session_boundary_resets_baseline_mid_frame(self) -> None:
        # Three sessions in ONE frame: a bad ratio ACROSS each boundary never
        # drops (session reset), while a bad ratio WITHIN session 2 drops.
        frame = self._frame(
            [
                (self._ts(0), 100.0),
                (self._ts(1), 101.0),
                (self._ts(0, day=1), 500.0),  # ratio 4.95 vs day-1 close: KEPT (new session)
                (self._ts(1, day=1), 505.0),
                (self._ts(2, day=1), 5050.0),  # ×10 within session 2: DROPPED
                (self._ts(3, day=1), 506.0),
                (self._ts(0, day=2), 2100.0),  # ratio 4.15 vs day-2 close: KEPT (new session)
                (self._ts(1, day=2), 2110.0),
            ]
        )
        result = screen_frame_bad_ticks(frame)
        assert result.get_column("close").to_list() == [
            100.0,
            101.0,
            500.0,
            505.0,
            506.0,
            2100.0,
            2110.0,
        ]

    def test_skipped_spike_never_advances_baseline(self) -> None:
        # The M1 mutant killer: the spike's successor is judged against the
        # last KEPT close (100), never the skipped 1000 — a naive adjacent
        # mask would also flag the healthy 102.
        frame = self._frame([(self._ts(0), 100.0), (self._ts(1), 1000.0), (self._ts(2), 102.0)])
        result = screen_frame_bad_ticks(frame)
        assert result.get_column("close").to_list() == [100.0, 102.0]

    def test_two_consecutive_spikes_both_dropped_one_warn(self) -> None:
        frame = self._frame(
            [
                (self._ts(0), 100.0),
                (self._ts(1), 1000.0),
                (self._ts(2), 1005.0),
                (self._ts(3), 101.0),
            ]
        )
        with capture_logs() as logs:
            result = screen_frame_bad_ticks(frame)
        assert result.get_column("close").to_list() == [100.0, 101.0]
        warns = [e for e in logs if e.get("event") == BAD_TICK_SKIP_EVENT]
        assert len(warns) == 1
        assert warns[0]["skipped"] == 2

    def test_exact_ratio_boundary_dropped_both_directions(self) -> None:
        # `>=` / `<=` — exactly max_ratio-x counts as bad, in BOTH directions
        # (also the M7 trigger-boundary killer: a `>`-drifted trigger finds no
        # candidate here and would return the frame unscreened).
        frame = self._frame(
            [
                (self._ts(0), 100.0),
                (self._ts(1), 400.0),  # up: 400 >= 100 * 4 → dropped
                (self._ts(0, day=1), 400.0),
                (self._ts(1, day=1), 100.0),  # down: 100 * 4 <= 400 → dropped
            ]
        )
        result = screen_frame_bad_ticks(frame)
        assert result.get_column("close").to_list() == [100.0, 400.0]

    def test_exact_up_boundary_sole_candidate_dropped(self) -> None:
        # The UP boundary as the frame's ONLY violation: 400.0 == 100.0 * 4.0
        # exactly in floats. Kills the single-direction trigger mutant
        # `>=` -> `>` ALONE (with no other candidate in the frame, a drifted
        # up-trigger finds nothing and would return the frame unscreened —
        # the two-boundary test cannot catch it because its other session's
        # intact direction still routes the frame to the sequential arm).
        frame = self._frame([(self._ts(0), 100.0), (self._ts(1), 400.0)])
        result = screen_frame_bad_ticks(frame)
        assert result.get_column("close").to_list() == [100.0]

    def test_exact_down_boundary_sole_candidate_dropped(self) -> None:
        # The DOWN boundary as the frame's ONLY violation: 100.0 * 4.0 ==
        # 400.0 exactly. Kills the `<=` -> `<` single-direction trigger
        # mutant, symmetrically to the up-boundary killer above.
        frame = self._frame([(self._ts(0), 400.0), (self._ts(1), 100.0)])
        result = screen_frame_bad_ticks(frame)
        assert result.get_column("close").to_list() == [400.0]

    def test_just_inside_boundary_kept(self) -> None:
        frame = self._frame(
            [
                (self._ts(0), 100.0),
                (self._ts(1), 399.99),
                (self._ts(0, day=1), 400.0),
                (self._ts(1, day=1), 100.01),
            ]
        )
        result = screen_frame_bad_ticks(frame)
        assert result is frame  # nothing dropped → the input object, zero-copy

    def test_one_warn_per_session_with_source_fields(self) -> None:
        # Spikes in TWO sessions → two WARNs; the SECOND sits in the FINAL
        # session, so it only flushes via finish() (the M6 mutant killer).
        frame = self._frame(
            [
                (self._ts(0), 100.0),
                (self._ts(1), 1000.0),
                (self._ts(0, day=1), 200.0),
                (self._ts(1, day=1), 2000.0),
            ]
        )
        with capture_logs() as logs:
            result = screen_frame_bad_ticks(frame, source="prebuild:NSE/INDEX/NSE-NIFTY/1minute")
        assert result.get_column("close").to_list() == [100.0, 200.0]
        warns = [e for e in logs if e.get("event") == BAD_TICK_SKIP_EVENT]
        assert [w["session"] for w in warns] == ["2024-01-02", "2024-01-03"]
        for warn, (prev_close, close) in zip(
            warns, [(100.0, 1000.0), (200.0, 2000.0)], strict=True
        ):
            assert warn["source"] == "prebuild:NSE/INDEX/NSE-NIFTY/1minute"
            assert warn["skipped"] == 1
            assert warn["first_prev_close"] == prev_close
            assert warn["first_close"] == close

    def test_clean_frame_returns_input_object_identity(self) -> None:
        frame = self._frame([(self._ts(0), 100.0), (self._ts(1), 100.5), (self._ts(2), 101.0)])
        assert screen_frame_bad_ticks(frame) is frame

    def test_clean_frame_no_warn(self) -> None:
        frame = self._frame([(self._ts(0), 100.0), (self._ts(1), 100.5)])
        with capture_logs() as logs:
            screen_frame_bad_ticks(frame)
        assert not [e for e in logs if e.get("event") == BAD_TICK_SKIP_EVENT]

    def test_empty_frame_returned_unchanged(self) -> None:
        frame = self._frame([])
        with capture_logs() as logs:
            result = screen_frame_bad_ticks(frame)
        assert result is frame
        assert result.height == 0
        assert not [e for e in logs if e.get("event") == BAD_TICK_SKIP_EVENT]

    def test_single_row_frame_kept(self) -> None:
        frame = self._frame([(self._ts(0), 100.0)])
        assert screen_frame_bad_ticks(frame) is frame

    def test_month_straddling_sessions_reset_on_ist_date(self) -> None:
        # Jan-31 + Feb-01 sessions concatenated by the whole-span glob: the ×10
        # reopen gap across the month boundary never trips (session reset on
        # the IST calendar date, not on frame position).
        jan = datetime(2024, 1, 31, 3, 45, tzinfo=UTC)
        feb = datetime(2024, 2, 1, 3, 45, tzinfo=UTC)
        frame = self._frame(
            [
                (jan, 100.0),
                (jan + timedelta(minutes=1), 101.0),
                (feb, 1010.0),
                (feb + timedelta(minutes=1), 1011.0),
            ]
        )
        assert screen_frame_bad_ticks(frame) is frame

    def test_open_interest_column_preserved_and_row_aligned(self) -> None:
        pl = pytest.importorskip("polars")
        frame = self._frame(
            [
                (self._ts(0), 100.0),
                (self._ts(1), 101.0),
                (self._ts(2), 1000.0),
                (self._ts(3), 102.0),
            ],
            oi=True,
        )
        result = screen_frame_bad_ticks(frame)
        assert result.columns == frame.columns
        # The dropped spike removes ITS OWN row wholesale — OI included,
        # row-aligned with every other column (the hand-filtered oracle).
        expected = frame.filter(pl.Series("keep", [True, True, False, True]))
        assert result.equals(expected)
        assert result.get_column("open_interest").to_list() == [0, 10, 30]

    def test_naive_ts_treated_as_utc(self) -> None:
        rows = [(self._ts(0), 100.0), (self._ts(1), 1000.0), (self._ts(2), 102.0)]
        aware = screen_frame_bad_ticks(self._frame(rows))
        naive = screen_frame_bad_ticks(self._frame(rows, naive=True))
        assert naive.get_column("close").to_list() == aware.get_column("close").to_list()
        assert naive.get_column("close").to_list() == [100.0, 102.0]

    def test_custom_max_ratio(self) -> None:
        rows = [(self._ts(0), 100.0), (self._ts(1), 250.0)]
        # The default 4.0 keeps a 2.5x move (identity return) …
        default_frame = self._frame(rows)
        assert screen_frame_bad_ticks(default_frame) is default_frame
        # … while an explicit max_ratio=2.0 drops it.
        tight = screen_frame_bad_ticks(self._frame(rows), max_ratio=2.0)
        assert tight.get_column("close").to_list() == [100.0]
