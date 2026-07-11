"""Generate the cross-language batch Stage-1A golden fixture (one-time).

DEC-STAGE1A-RUST-BATCH-001 §6.4 — the twin-state fixture discipline: the
Python ORACLE (`Stage1AMatrixBuilder.build`) + the prepass
(`collect_batch_inputs`) generate the frozen fixture ONCE; the Rust
integration test (`rust/trading-core/tests/stage1a_batch_golden.rs`) replays
the columns Rust-only and must reproduce every mask bit-for-bit. The fixture
is committed and NEVER regenerated casually (a regeneration is a REVIEWED
behaviour change by doctrine); the guard below refuses to overwrite an
existing file without ``BRUTEX_REGEN_STAGE1A_BATCH_GOLDEN=1``.

Feed: a 6-trading-day, 2-roll futures stream (three 2-day contracts with a
basis bump per roll — a superset of the design's named 3-day/2-roll shape so
WARM anchors, EMA(20) warm-up completion and the D11 hard reset are all
inside the frozen corpus), 26 volume-bearing bars per day driving the VWAP +
BOS/CHoCH state machines through real signal.

Run:  .venv/bin/python tests/golden/generate_stage1a_batch_golden.py
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from brutex.data.models import CandleBar
from brutex.engine.stage1a import Stage1AMatrixBuilder

_IST = timezone(timedelta(hours=5, minutes=30))

_FIXTURE_PATH = (
    Path(__file__).resolve().parents[2]
    / "rust"
    / "trading-core"
    / "tests"
    / "fixtures"
    / "stage1a_batch_golden.json"
)

#: Six consecutive real NSE trading days (June 2025 — weekday-checked).
_DAYS: tuple[date, ...] = (
    date(2025, 6, 23),
    date(2025, 6, 24),
    date(2025, 6, 25),
    date(2025, 6, 26),
    date(2025, 6, 27),
    date(2025, 6, 30),
)

#: The (h, l, c) day script from the roll golden's B1 fire-test — fires the
#: VWAP-position + BOS/CHoCH bits within one 26-bar day.
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


def _bars() -> list[CandleBar]:
    out: list[CandleBar] = []
    for d_idx, d in enumerate(_DAYS):
        basis = 40 * (d_idx // 2)  # +40 per contract (rolls after days 2 and 4)
        for m, (h, lo, c) in enumerate(_HLC_SCRIPT):
            ts = datetime(d.year, d.month, d.day, 9, 15, tzinfo=_IST) + timedelta(minutes=m)
            open_ = min(h, max(lo, c)) + basis
            out.append(
                CandleBar(
                    timestamp=ts.astimezone(UTC),
                    open=Decimal(open_),
                    high=Decimal(h + basis),
                    low=Decimal(lo + basis),
                    close=Decimal(c + basis),
                    volume=100,
                    open_interest=None,
                )
            )
    return out


def _schedule() -> tuple[int, ...]:
    # Three 2-day contracts: expiries on days 2, 4 and 6.
    return (_DAYS[1].toordinal(), _DAYS[3].toordinal(), _DAYS[5].toordinal())


def generate() -> dict[str, object]:
    """Build the fixture payload (pure — no file I/O)."""
    bars = _bars()
    schedule = _schedule()
    inputs = Stage1AMatrixBuilder(roll_schedule=schedule).collect_batch_inputs(bars)
    masks = Stage1AMatrixBuilder(roll_schedule=schedule).build(bars)
    return {
        "schema": "stage1a-batch-golden-v1",
        "columns": {
            "open_paisa": inputs.open_paisa,
            "high_paisa": inputs.high_paisa,
            "low_paisa": inputs.low_paisa,
            "close_paisa": inputs.close_paisa,
            "typical_paisa": inputs.typical_paisa,
            "volume": inputs.volume,
            "tod_seconds": inputs.tod_seconds,
            "pre_open": inputs.pre_open,
            "session_key": inputs.session_key,
            "session_day": inputs.session_day,
            "contract_reset": inputs.contract_reset,
            "day_index": inputs.day_index,
            "ema20_paisa": inputs.ema20_paisa,
            "ema200_paisa": inputs.ema200_paisa,
        },
        "day_tables": {
            "day_pivot_levels": inputs.day_pivot_levels,
            "day_fib_levels": inputs.day_fib_levels,
            "day_has_pivot": inputs.day_has_pivot,
            "day_has_fib": inputs.day_has_fib,
        },
        "config": {
            "pivot_bps": inputs.pivot_bps,
            "pdh_pdl_bps": inputs.pdh_pdl_bps,
            "fib_bps": inputs.fib_bps,
            "prior_n": inputs.prior_n,
            "fractal_k": inputs.fractal_k,
            "apply_1day_mask": inputs.apply_1day_mask,
            # Hex STRINGS — a u128 JSON number would lose precision past 2^53.
            "anchor_dependent_mask_hex": f"{inputs.anchor_dependent_mask:x}",
            "fib_dependent_mask_hex": f"{inputs.fib_dependent_mask:x}",
            "applies_to_1day_mask_hex": f"{inputs.applies_to_1day_mask:x}",
        },
        "expected_masks_hex": [f"{m:x}" for m in masks],
    }


def main() -> int:
    """Write the fixture (refusing a silent overwrite)."""
    if _FIXTURE_PATH.exists() and os.environ.get("BRUTEX_REGEN_STAGE1A_BATCH_GOLDEN") != "1":
        print(
            f"REFUSED: {_FIXTURE_PATH} exists — a regeneration is a reviewed "
            "behaviour change; set BRUTEX_REGEN_STAGE1A_BATCH_GOLDEN=1 to proceed",
            file=sys.stderr,
        )
        return 1
    _FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = generate()
    _FIXTURE_PATH.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {_FIXTURE_PATH} ({len(payload['expected_masks_hex'])} masks)")  # type: ignore[arg-type]
    return 0


if __name__ == "__main__":  # pragma: no cover — the one-time generator entry
    raise SystemExit(main())
