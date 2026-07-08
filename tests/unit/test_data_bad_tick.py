"""Unit tests for the bounded bad-tick screen (``brutex.data.bad_tick``).

100% line + branch coverage of the spike / fat-finger / vendor-scaling guard at
the engine read boundary (DEC-SWEEP-DATA-EDGE-GUARDS-001, D-MAJ-2). The
end-to-end wiring through :func:`arrow_table_to_bars` is exercised in
``tests/integration/test_feed_replay_bad_tick_screen.py`` (stem-named
``test_feed_replay*`` so the coverage-gate discovery maps it to
``brutex/feed/replay.py``).
"""

from __future__ import annotations

from datetime import date

import pytest
from brutex.data.bad_tick import (
    BAD_TICK_MAX_RATIO,
    BAD_TICK_SKIP_EVENT,
    BadTickScreen,
    bad_tick_screen_enabled,
    is_bad_tick,
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
