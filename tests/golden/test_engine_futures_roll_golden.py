"""Golden — the ADR-0012 two-contract stitched roll fixture (frozen mask matrix).

DEC-STAGE1A-ROLL-RESET-001: a volume-bearing engineered stream crosses ONE
roll boundary with a ~40-pt basis gap. Each contract-day runs the same
26-bar (h, l, c) script the B1 fire-test proved fires ALL SIX resurrected
bits — the VWAP-position bits 52/53 and the BOS/CHoCH bits 56-59 — so the
fixture pins, byte-for-byte:

* the six bits FIRE on the pre-roll contract;
* the TOTAL per-contract hard reset at the roll — the first post-roll bars
  carry NO market-structure bit (the fresh ``MarketStructureState`` ring is
  unfilled, k=2 ⇒ no signal before 2k+1 bars) and the first post-roll day
  carries NO anchor-dependent bit (the ``_NON_ANCHOR_MASK`` cold-start);
* the six bits RE-FIRE on the post-roll contract (a reset is a restart,
  never a permanent silence).

The full 52-mask matrix is frozen in
``tests/golden/fixtures/futures_roll_golden.json`` and locked by an in-file
SHA-256 of the fixture bytes plus LITERAL boundary masks (the last pre-roll
bar + the first post-roll bar).

Regeneration (a REVIEWED behaviour change by doctrine): run

    python tests/golden/test_engine_futures_roll_golden.py --update

which rewrites the fixture JSON from the current builder output and prints
the new ``_EXPECTED_SHA256`` + boundary-mask literals to re-pin here.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from brutex.data.models import CandleBar
from brutex.engine.stage1a import ANCHOR_DEPENDENT_MASK, build_bar_bits
from brutex.vocabulary import bits as B

_IST = timezone(timedelta(hours=5, minutes=30))

_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "futures_roll_golden.json"

#: SHA-256 of the frozen fixture file bytes (regenerate via ``--update``).
_EXPECTED_SHA256 = "1210f918e909090190ffde9231879dbb3111aa539077cdbc6113c9e4baa458f4"

#: The pre-roll contract's IST day (the vendor June-2025 monthly expiry) and
#: the first post-roll day — the roll boundary the schedule encodes.
_DAY_PRE_ROLL = date(2025, 6, 26)
_DAY_POST_ROLL = date(2025, 6, 27)
_ROLL_SCHEDULE = (_DAY_PRE_ROLL.toordinal(), date(2025, 7, 31).toordinal())

#: The engineered single-day (h, l, c) script from the B1 fire-test
#: (``tests/integration/test_stage1a_vwap_bos_bits_fire.py``): with the k=2
#: fractal lag baked in it prints BOS_BEAR (bar 5), CHOCH_BULL (10),
#: CHOCH_BEAR (15), CHOCH_BULL (20), BOS_BULL (25), and crosses the running
#: session VWAP BOTH ways.
_HLC_SCRIPT: tuple[tuple[int, int, int], ...] = (
    (100, 90, 95),
    (95, 85, 90),
    (88, 70, 75),
    (90, 80, 85),
    (92, 82, 88),
    (85, 60, 65),
    (95, 85, 90),
    (110, 100, 105),
    (98, 88, 92),
    (96, 86, 90),
    (120, 100, 115),
    (118, 108, 112),
    (114, 95, 100),
    (116, 106, 110),
    (117, 107, 111),
    (100, 80, 85),
    (108, 98, 102),
    (130, 118, 125),
    (122, 112, 116),
    (121, 111, 115),
    (140, 120, 135),
    (138, 128, 132),
    (150, 140, 145),
    (142, 132, 136),
    (141, 131, 135),
    (160, 150, 155),
)

#: The ~40-pt basis gap between the expiring and the next contract (D11: no
#: level may ever be compared across it).
_BASIS_GAP = 40

_STRUCTURE_MASK = (
    (1 << B.BIT_BOS_BULLISH)
    | (1 << B.BIT_BOS_BEARISH)
    | (1 << B.BIT_CHOCH_BULLISH)
    | (1 << B.BIT_CHOCH_BEARISH)
)
_SIX_BITS = (
    B.BIT_CLOSE_ABOVE_VWAP,
    B.BIT_CLOSE_BELOW_VWAP,
    B.BIT_BOS_BULLISH,
    B.BIT_BOS_BEARISH,
    B.BIT_CHOCH_BULLISH,
    B.BIT_CHOCH_BEARISH,
)

#: LITERAL boundary masks — the last pre-roll bar (index 25) and the first
#: post-roll bar (index 26). Re-pin via ``--update``.
_EXPECTED_LAST_PRE_ROLL_MASK = 76583480250597377
_EXPECTED_FIRST_POST_ROLL_MASK = 21474836480


def _script_bar(day: date, minute: int, h: int, low: int, c: int) -> CandleBar:
    """One volume-bearing 1-minute bar of the script on *day* (09:15 IST + minute)."""
    ts = datetime(day.year, day.month, day.day, 9, 15, tzinfo=_IST) + timedelta(minutes=minute)
    open_ = min(h, max(low, c))
    return CandleBar(
        timestamp=ts.astimezone(UTC),
        open=Decimal(open_),
        high=Decimal(h),
        low=Decimal(low),
        close=Decimal(c),
        volume=100,
        open_interest=None,
    )


def _stitched_bars() -> list[CandleBar]:
    """Segment A (expiry day, base script) + segment B (next day, +40 basis)."""
    seg_a = [_script_bar(_DAY_PRE_ROLL, m, h, low, c) for m, (h, low, c) in enumerate(_HLC_SCRIPT)]
    seg_b = [
        _script_bar(_DAY_POST_ROLL, m, h + _BASIS_GAP, low + _BASIS_GAP, c + _BASIS_GAP)
        for m, (h, low, c) in enumerate(_HLC_SCRIPT)
    ]
    return seg_a + seg_b


def _masks() -> list[int]:
    return build_bar_bits(_stitched_bars(), roll_schedule=_ROLL_SCHEDULE)


def _load_fixture() -> dict[str, object]:
    return json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def test_fixture_bytes_are_frozen() -> None:
    digest = hashlib.sha256(_FIXTURE_PATH.read_bytes()).hexdigest()
    assert digest == _EXPECTED_SHA256, (
        f"futures_roll_golden.json drifted (sha256 {digest}); regenerate deliberately via "
        "`python tests/golden/test_engine_futures_roll_golden.py --update` and re-pin"
    )


def test_masks_match_the_frozen_fixture() -> None:
    fixture = _load_fixture()
    assert _masks() == fixture["masks"]


def test_boundary_masks_are_literal_pinned() -> None:
    masks = _masks()
    n_pre = len(_HLC_SCRIPT)
    assert masks[n_pre - 1] == _EXPECTED_LAST_PRE_ROLL_MASK
    assert masks[n_pre] == _EXPECTED_FIRST_POST_ROLL_MASK


def test_six_bits_fire_reset_and_refire_across_the_roll() -> None:
    """The semantic contract behind the frozen bytes (version-robust form)."""
    masks = _masks()
    n_pre = len(_HLC_SCRIPT)
    pre, post = masks[:n_pre], masks[n_pre:]
    for bit in _SIX_BITS:
        assert any(m & (1 << bit) for m in pre), f"bit {bit} never fired pre-roll"
        assert any(m & (1 << bit) for m in post), f"bit {bit} never re-fired post-roll"
    # The reset: a fresh k=2 ring confirms nothing before 2k+1 bars, so the
    # first 4 post-roll bars carry NO market-structure bit …
    assert all((m & _STRUCTURE_MASK) == 0 for m in post[:4])
    # … and the whole first post-roll day is anchor-cold (D11: the prev-day
    # levels 40 pts below are never consulted across the basis gap).
    assert all((m & ANCHOR_DEPENDENT_MASK) == 0 for m in post)


def test_matrix_is_deterministic() -> None:
    assert _masks() == _masks()


def _update() -> None:  # pragma: no cover — the manual regeneration entry point
    masks = _masks()
    _FIXTURE_PATH.write_text(
        json.dumps(
            {"schema": "futures-roll-golden-v1", "masks": masks},
            indent=1,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    n_pre = len(_HLC_SCRIPT)
    digest = hashlib.sha256(_FIXTURE_PATH.read_bytes()).hexdigest()
    print(f"wrote {_FIXTURE_PATH}")
    print(f'_EXPECTED_SHA256 = "{digest}"')
    print(f"_EXPECTED_LAST_PRE_ROLL_MASK = {masks[n_pre - 1]}")
    print(f"_EXPECTED_FIRST_POST_ROLL_MASK = {masks[n_pre]}")


if __name__ == "__main__":  # pragma: no cover
    import sys

    if "--update" in sys.argv:
        _update()
    else:
        print(__doc__)
