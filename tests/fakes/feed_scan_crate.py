"""A law-faithful stand-in ``brutex_feed`` crate for the scan kernel.

DEC-DATA-RUST-INTEGRITY-DETECTORS-001 (SLICE T24 PR-2). The ONE fake
``scan_bars`` builder the ``bad_tick_rust`` facade suite, the
``bad_tick`` frame-seam suite, the 3-way frame-equivalence property and
the stored-outcome gate share (CLAUDE.md §11 COMMON — no per-file copy):
a ``SimpleNamespace`` exposing ``scan_bars`` with a pure-Python
transcription of ``rust/brutex-feed/src/scan.rs`` (DEC-FEED-RUST-CORE-001
— positivity stop + the ``BadTickScreen`` plane over the fixed-IST
day-ordinal session key + warn coordinates + the timestamp-envelope
ABSTAIN), so ON-arm wiring/coverage tests run WITHOUT the built wheel
(the real-wheel differentials are separate, ``importorskip``-gated).
The kernel callable is overridable for poison/degrade/panic scenarios
(the ``fake_detectors_crate`` convention).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from types import SimpleNamespace

#: Fixed IST offset (+05:30) in microseconds (`scan.rs::IST_OFFSET_US`).
IST_OFFSET_US = 19_800_000_000

#: One civil day in microseconds (`scan.rs::DAY_US`).
DAY_US = 86_400_000_000

#: Exclusive upper bound of the safe timestamp envelope
#: (`scan.rs::MAX_SAFE_TS_US` — 9999-12-31T00:00:00Z in epoch µs).
MAX_SAFE_TS_US = 253_402_214_400_000_000


def fake_scan_bars(
    ts_us: Sequence[int],
    opens: Sequence[float],
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    screen_enabled: bool,
    max_ratio: float,
) -> tuple[list[int], list[tuple[int, int, int, int, int]], tuple[int, int] | None] | None:
    """The `scan.rs::scan_bars` law, block for block, in Python."""
    n = len(opens)
    if len(highs) != n or len(lows) != n or len(closes) != n:
        raise ValueError("scan_bars: price column lengths diverge")
    if screen_enabled:
        if len(ts_us) != n:
            raise ValueError("scan_bars: ts_us length mismatch with screen_enabled")
    elif ts_us:
        raise ValueError("scan_bars: ts_us must be empty when screen_enabled is false")

    kept: list[int] = []
    warns: list[tuple[int, int, int, int, int]] = []
    cur_session: int | None = None
    cur_session_first_row = 0
    prev_close: float | None = None
    prev_row = 0
    skipped = 0
    first_offender: tuple[int, int] | None = None

    for i in range(n):
        for field, value in enumerate((opens[i], highs[i], lows[i], closes[i])):
            if value <= 0.0:
                return (kept, warns, (i, field))
        if screen_enabled:
            t = ts_us[i]
            if not 0 <= t < MAX_SAFE_TS_US:
                return None
            session_day = (t + IST_OFFSET_US) // DAY_US
            if cur_session != session_day:
                if skipped > 0 and first_offender is not None:
                    warns.append((i, cur_session_first_row, skipped, *first_offender))
                cur_session = session_day
                cur_session_first_row = i
                prev_close = None
                skipped = 0
                first_offender = None
            if prev_close is not None:
                close = closes[i]
                if close >= prev_close * max_ratio or close * max_ratio <= prev_close:
                    skipped += 1
                    if first_offender is None:
                        first_offender = (prev_row, i)
                    continue
            prev_close = closes[i]
            prev_row = i
        kept.append(i)

    if skipped > 0 and first_offender is not None:
        warns.append((n, cur_session_first_row, skipped, *first_offender))
    return (kept, warns, None)


def fake_feed_crate(
    *,
    scan_bars: Callable[..., object] | None = None,
    version: str = "0.1.0-fake",
) -> SimpleNamespace:
    """A stand-in ``brutex_feed`` module exposing ``scan_bars``."""
    return SimpleNamespace(
        scan_bars=(scan_bars if scan_bars is not None else fake_scan_bars),
        __version__=version,
    )
