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
BOS/CHoCH state machines through real signal (the five VWAP feed columns are
RE-EMITTED per DEC-VOCAB-VWAP-RETAIN-001, MIGRATION.md #2206 — the wipe-era
schema-v2 drop is reversed), with DIRECTION-VARIED opens
(prev-close continuation clamped into range; each day's first bar a doji) so
the Group-6 prior-flag deque is genuinely exercised — refuter RF-B F1
regenerated the fixture from this fixed derivation (the original collapsed
every bar to a doji, leaving the rolling direction window untested).

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
#: VWAP-position + BOS/CHoCH bits within one 26-bar day (the VWAP pair is
#: LIVE again per DEC-VOCAB-VWAP-RETAIN-001, MIGRATION.md #2206).
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
        # Refuter RF-B F1: the open CONTINUES from the prior close (clamped
        # into the bar's own range) so the corpus carries REAL directions —
        # the original `min(h, max(lo, c))` collapsed to `open == close` on
        # every row (`lo <= c <= h`), leaving the prior-flag/direction deque
        # untested by the fixture. `prev_close` resets per day, so each day's
        # FIRST bar is a deliberate DOJI anchor (all three classes present).
        prev_close: int | None = None
        for m, (h, lo, c) in enumerate(_HLC_SCRIPT):
            ts = datetime(d.year, d.month, d.day, 9, 15, tzinfo=_IST) + timedelta(minutes=m)
            open_ = (c if prev_close is None else min(h, max(lo, prev_close))) + basis
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
            prev_close = c
    # The F1 corpus-honesty gate: bull, bear AND doji bars must all be present
    # or the direction dimension of the frozen fixture is degenerate again.
    bull = sum(1 for b in out if b.close > b.open)
    bear = sum(1 for b in out if b.close < b.open)
    doji = sum(1 for b in out if b.close == b.open)
    if not (bull and bear and doji):
        raise AssertionError(f"degenerate direction corpus: bull={bull} bear={bear} doji={doji}")
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
        # v3: the vocab-66 SuperTrend implemented-flip (crate 0.5.0) — the
        # per-bar `supertrend_x2_paisa` column joins the batch ABI (the
        # 26-bar day script warms the ATR-10 state at bar 9 of each roll
        # segment, so the frozen corpus genuinely exercises bits 64/65 both
        # ways) — composed at the train fold with
        # DEC-FIB-TEMPLATE-EXPRESS-001: the fib day rows widen 15→19
        # (bull_23/bull_78/ext_up_424/ext_down_424 appended), the config
        # gains ``swing_bps`` and the masks may carry the express bits 69-73
        # (72/73 from the kernel's own MarketStructureState swing reads) and
        # the gap bits 66-68 (DEC-GAP-MIDPOINT-CONDITIONS-001 X1 day tables).
        # Regen re-lock (DEC-VOCAB-VWAP-RETAIN-001, MIGRATION.md #2206 +
        # #2204): the schema TOKEN stays v3 — the Rust reader's
        # `#[serde(default)]` arms make the five VWAP feed columns + the
        # Group-15 X1 day tables ADDITIVE-tolerant under one schema — but the
        # regenerated fixture now EMITS them for real (the wipe-era drop
        # reversed; the pre-gap all-cold X1 materialization arm goes dormant),
        # so the frozen masks legitimately carry bits 52/53 + 66-68.
        "schema": "stage1a-batch-golden-v3",
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
            "supertrend_x2_paisa": inputs.supertrend_x2_paisa,
        },
        "day_tables": {
            "day_pivot_levels": inputs.day_pivot_levels,
            "day_fib_levels": inputs.day_fib_levels,
            "day_has_pivot": inputs.day_has_pivot,
            "day_has_fib": inputs.day_has_fib,
            "day_x1_hi_paisa": inputs.day_x1_hi_paisa,
            "day_x1_lo_paisa": inputs.day_x1_lo_paisa,
            "day_has_x1": inputs.day_has_x1,
        },
        "config": {
            "pivot_bps": inputs.pivot_bps,
            "pdh_pdl_bps": inputs.pdh_pdl_bps,
            "fib_bps": inputs.fib_bps,
            "swing_bps": inputs.swing_bps,
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
