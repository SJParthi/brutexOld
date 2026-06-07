"""Golden replay for **P-06** — a known-good strategy touches only ≤ N.

DEC-LOOKAHEAD-BARRIER-001. Locks the byte-exact touched-index trail and the
:class:`~brutex.feed.lookahead.LookAheadReport` a deterministic, known-good
forward replay produces through the instrumented barrier. Any change to the
accessor's recording or verdict semantics breaks this golden — exactly the
trip-wire CLAUDE.md §11 (COMPREHENSIVE: bit-exact oracle) demands.

Two locked scenarios:

* a **pure forward** replay over 8 bars (the canonical no-look-ahead pattern
  the engine consumes), and
* a **forward + bounded look-back** replay (each bar also re-reads the prior
  bar, e.g. an indicator with ``lookback=1``) — still only ever reaching
  indices ``≤ N``.

The barrier is pure (no Rust wheel, no lake), so this golden runs on a bare
checkout.
"""

from __future__ import annotations

import sys
import types

from brutex.feed.lookahead import LookAheadBarrier, verify_with_barrier

# ── Locked golden answer-keys ───────────────────────────────────────────

# Scenario A — pure forward replay over 8 bars: read bar i at step i.
_GOLDEN_A_TOUCHED: tuple[int, ...] = (0, 1, 2, 3, 4, 5, 6, 7)
_GOLDEN_A_MAX = 7
_GOLDEN_A_CURSOR = 7

# Scenario B — forward + lookback(1): at bar i, read i then (i-1) when i>0.
_GOLDEN_B_TOUCHED: tuple[int, ...] = (
    0,
    1,
    0,
    2,
    1,
    3,
    2,
    4,
    3,
)
_GOLDEN_B_MAX = 4
_GOLDEN_B_CURSOR = 4


def _install_structlog_stub() -> None:
    """Make ``verify_with_barrier`` runnable wheel-free + dep-light.

    ``verify_with_barrier`` lazy-imports the core ``structlog`` runtime dep for
    one observability log line. In CI structlog is installed (a core dep) so the
    real module is used and this is a no-op — we only install a no-op stub when
    structlog genuinely cannot be imported (a bare-checkout golden runner), so a
    real-but-not-yet-imported structlog is NEVER shadowed. The log line is
    observability, not part of the locked output, so a stub does not weaken the
    assertion.
    """
    try:
        import structlog  # noqa: F401  — probe: real dep present → no stub needed
    except ImportError:
        stub = types.ModuleType("structlog")

        class _Logger:
            def info(self, *args: object, **kwargs: object) -> None:
                return None

        stub.get_logger = lambda *args, **kwargs: _Logger()  # type: ignore[attr-defined]
        sys.modules["structlog"] = stub


def test_golden_pure_forward_replay_trail() -> None:
    """A known-good forward replay yields the locked touched-index trail."""
    barrier = LookAheadBarrier(record=True)
    for index in range(8):
        barrier.advance_to(index)
        barrier.access(index)
    assert barrier.touched_indices == _GOLDEN_A_TOUCHED
    assert barrier.max_touched == _GOLDEN_A_MAX
    assert barrier.current_index == _GOLDEN_A_CURSOR


def test_golden_forward_with_lookback_trail() -> None:
    """A known-good forward+lookback(1) replay only ever reaches indices ≤ N."""
    barrier = LookAheadBarrier(record=True)
    for index in range(5):
        barrier.advance_to(index)
        barrier.access(index)
        if index > 0:
            barrier.access(index - 1)  # bounded look-back — still ≤ N
    assert barrier.touched_indices == _GOLDEN_B_TOUCHED
    assert barrier.max_touched == _GOLDEN_B_MAX
    assert barrier.current_index == _GOLDEN_B_CURSOR
    assert barrier.max_touched <= barrier.current_index


def test_golden_verify_report_is_locked() -> None:
    """The manual-recheck verdict over scenario A is byte-stable."""
    _install_structlog_stub()
    barrier = LookAheadBarrier(record=True)

    def replay() -> None:
        for index in range(8):
            barrier.advance_to(index)
            barrier.access(index)

    report = verify_with_barrier(replay, barrier)
    assert report.passed is True
    assert report.indices_checked == 8
    assert report.max_index_touched == _GOLDEN_A_MAX
    assert report.final_cursor == _GOLDEN_A_CURSOR
    assert report.violation is None
