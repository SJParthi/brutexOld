"""Golden — the ADR-0012 two-contract stitched roll fixture (frozen mask matrix).

DEC-STAGE1A-ROLL-RESET-001 + the 60→66 reshape: an engineered stream crosses
ONE roll boundary with a ~40-pt basis gap. The PRE-roll contract runs TWO
days of the 26-bar (h, l, c) script (day 2 is ANCHOR-WARM — its prev-day
pivots/PDH/Fib/day-type/CPR anchors resolve from day 1, so the
anchor-dependent bits INCLUDING the new CPR bits 60-63 get real teeth across
the roll); the POST-roll contract runs one (anchor-cold) day. The fixture
pins, byte-for-byte:

* the four BOS/CHoCH market-structure bits (56-59) FIRE pre-roll;
* the anchor-dependent bits fire on the WARM pre-roll day 2 — every warm
  bar carries exactly one CPR position bit (the 60/61/62 partition);
* the TOTAL per-contract hard reset at the roll — the first post-roll bars
  carry NO market-structure bit (the fresh ``MarketStructureState`` ring is
  unfilled, k=2 ⇒ no signal before 2k+1 bars) and the first post-roll day
  carries NO anchor-dependent bit (the ``_NON_ANCHOR_MASK`` cold-start,
  CPR included);
* the structure bits RE-FIRE on the post-roll contract (a reset is a
  restart, never a permanent silence);
* the RETIRED VWAP tombstones (52/53) are 0 in EVERY mask — positively
  asserted (the VWAP wipe: the bits' math was deleted at crate 0.4.0).

The full 78-mask matrix is frozen in
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
# DELIBERATE RE-LOCK (the 60→66 reshape + the 3-day warm-day extension —
# G-COMBIN-03): old digest
# 1210f918e909090190ffde9231879dbb3111aa539077cdbc6113c9e4baa458f4 (v1,
# 52 masks, both days anchor-cold, VWAP bits still live).
_EXPECTED_SHA256 = "e4accd47db6fde1c478ec0b3924812fb9168e8a233d148f483751100cb2e0621"

#: The pre-roll contract's TWO IST days (day 2 = the vendor June-2025
#: monthly expiry — owned by the EXPIRING contract per the ``>=`` law; day 1
#: makes day 2 anchor-WARM) and the first post-roll day.
_DAY_PRE_WARMUP = date(2025, 6, 25)
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
#: The four stateful market-structure bits (the former "_SIX_BITS" also
#: carried the VWAP pair — retired tombstones since the 60→66 reshape).
_STRUCTURE_BITS = (
    B.BIT_BOS_BULLISH,
    B.BIT_BOS_BEARISH,
    B.BIT_CHOCH_BULLISH,
    B.BIT_CHOCH_BEARISH,
)
#: The RETIRED VWAP tombstone pair — positively asserted 0 in EVERY mask.
_VWAP_TOMBSTONE_MASK = (1 << B.BIT_CLOSE_ABOVE_VWAP) | (1 << B.BIT_CLOSE_BELOW_VWAP)
#: The CPR position bits — exactly one fires per anchor-WARM bar.
_CPR_POSITION_MASK = (1 << B.BIT_ABOVE_CPR_TC) | (1 << B.BIT_BELOW_CPR_BC) | (1 << B.BIT_INSIDE_CPR)

#: LITERAL boundary masks — the last pre-roll bar (index 25) and the first
#: post-roll bar (index 26). Re-pin via ``--update``.
_EXPECTED_LAST_PRE_ROLL_MASK = 1225564335183544321
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
    """Segment A (warm-up day + expiry day, base script) + segment B (+40 basis).

    Segment A runs the script on TWO consecutive contract days so its second
    day is anchor-WARM (prev-day pivots/PDH/Fib/day-type/CPR all resolve);
    segment B is the first post-roll (anchor-cold) day.
    """
    seg_a = [
        _script_bar(d, m, h, low, c)
        for d in (_DAY_PRE_WARMUP, _DAY_PRE_ROLL)
        for m, (h, low, c) in enumerate(_HLC_SCRIPT)
    ]
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
    n_pre = 2 * len(_HLC_SCRIPT)
    assert masks[n_pre - 1] == _EXPECTED_LAST_PRE_ROLL_MASK
    assert masks[n_pre] == _EXPECTED_FIRST_POST_ROLL_MASK


def test_structure_bits_fire_reset_and_refire_across_the_roll() -> None:
    """The semantic contract behind the frozen bytes (version-robust form)."""
    masks = _masks()
    n_pre = 2 * len(_HLC_SCRIPT)
    pre, post = masks[:n_pre], masks[n_pre:]
    for bit in _STRUCTURE_BITS:
        assert any(m & (1 << bit) for m in pre), f"bit {bit} never fired pre-roll"
        assert any(m & (1 << bit) for m in post), f"bit {bit} never re-fired post-roll"
    # The reset: a fresh k=2 ring confirms nothing before 2k+1 bars, so the
    # first 4 post-roll bars carry NO market-structure bit …
    assert all((m & _STRUCTURE_MASK) == 0 for m in post[:4])
    # … and the whole first post-roll day is anchor-cold (D11: the prev-day
    # levels 40 pts below are never consulted across the basis gap — the CPR
    # bits 60-63 included).
    assert all((m & ANCHOR_DEPENDENT_MASK) == 0 for m in post)


def test_vwap_tombstones_never_fire() -> None:
    """The RETIRED bits 52/53 are 0 in EVERY mask — the positive wipe pin."""
    assert all((m & _VWAP_TOMBSTONE_MASK) == 0 for m in _masks())


def test_warm_day_fires_anchor_dependent_and_cpr_bits() -> None:
    """Pre-roll day 2 is anchor-WARM: the anchor-dependent bits genuinely
    fire (the anti-vacuity teeth for the post-roll cold assertion) and every
    warm bar carries EXACTLY one CPR position bit (the 60/61/62 partition)."""
    masks = _masks()
    n_day = len(_HLC_SCRIPT)
    day1, day2 = masks[:n_day], masks[n_day : 2 * n_day]
    # Day 1 is the contract's own cold start.
    assert all((m & ANCHOR_DEPENDENT_MASK) == 0 for m in day1)
    # Day 2: anchors warm — some anchor-dependent bit fires…
    assert any(m & ANCHOR_DEPENDENT_MASK for m in day2)
    # …and the CPR partition covers every warm bar exactly once.
    for i, m in enumerate(day2):
        position = m & _CPR_POSITION_MASK
        assert position, f"warm bar {i}: no CPR position bit"
        assert bin(position).count("1") == 1, f"warm bar {i}: CPR partition violated"


def test_matrix_is_deterministic() -> None:
    assert _masks() == _masks()


def _update() -> None:  # pragma: no cover — the manual regeneration entry point
    masks = _masks()
    _FIXTURE_PATH.write_text(
        json.dumps(
            {"schema": "futures-roll-golden-v2", "masks": masks},
            indent=1,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    n_pre = 2 * len(_HLC_SCRIPT)
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
