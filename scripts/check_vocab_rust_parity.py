#!/usr/bin/env python3
"""Verify the `trading_core` PyO3 binding is bit-identical to the Python vocab.

Phase 2a of `DEC-SHARED-CORE-001` (#441): the Rust crate's golden test already
proves the *Rust* `evaluate_atoms` reproduces the 408-case oracle (400 seeded +
8 deterministic express cases, DEC-FIB-TEMPLATE-EXPRESS-001) bit-for-bit.
This script proves the **Python-callable binding** (`trading_core.evaluate_from_raw`,
built by maturin) reproduces the same frozen `u128` masks — i.e. BruteX can call
into the Rust crate and get an identical result. It is run in CI's
`rust-trading-core` job after `maturin build` + `pip install`, isolated from the
main Python suite.

Exit 0 = every one of the 408 cases matches; exit 1 = a divergence (printed);
exit 2 = the wheel is not importable (build/install problem).
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_FIXTURE = _REPO_ROOT / "tests" / "golden" / "fixtures" / "vocab_golden_vectors.json"


def _tod_seconds(ts: str) -> int:
    """IST wall-clock seconds-of-day from an ISO timestamp (tz offset ignored)."""
    time_part = ts.split("T")[1]
    hms = re.split(r"[+\-Z]", time_part)[0]
    parts = [*hms.split(":"), "0", "0", "0"][:3]
    return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(float(parts[2]))


def main() -> int:
    """Run the 408-case Python↔Rust parity check."""
    try:
        import trading_core  # optional native wheel — imported at runtime, may be absent
    except ImportError as exc:  # pragma: no cover - exercised only without the wheel
        print(f"⚠️  trading_core wheel not importable: {exc}", file=sys.stderr)
        return 2

    data = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    cases = data["cases"]
    mismatches: list[str] = []
    for c in cases:
        # The appended-last member args (ST / gap / swing) ride kwargs and
        # tolerate ABSENT keys: the 8 keyless express cases (ids 400-407,
        # DEC-FIB-TEMPLATE-EXPRESS-001 #2205) omit the ST/gap/vwap keys and
        # instead carry the swing pair + `proximity.swing_bps`; an absent key
        # takes the pyo3 signature default — the SAME law the Rust fixture
        # readers pin via `#[serde(default)]` (DEC-VOCAB-VWAP-RETAIN-001
        # #2206 mandatory pin 2).
        tail_kwargs: dict[str, object] = {
            "supertrend_x2_paisa": c.get("supertrend_x2_paisa"),
            "gap_mid_paisa": c.get("gap_mid_paisa"),
            "last_swing_high_paisa": c.get("last_swing_high_paisa"),
            "last_swing_low_paisa": c.get("last_swing_low_paisa"),
        }
        if "gap_mid_bps" in c["proximity"]:
            tail_kwargs["gap_mid_bps"] = c["proximity"]["gap_mid_bps"]
        if "swing_bps" in c["proximity"]:
            tail_kwargs["swing_bps"] = c["proximity"]["swing_bps"]
        got = trading_core.evaluate_from_raw(
            _tod_seconds(c["timestamp_ist"]),
            c["open_paisa"],
            c["high_paisa"],
            c["low_paisa"],
            c["close_paisa"],
            c["ema20_paisa"],
            c["ema200_paisa"],
            # Session VWAP at its historical mid-list position (bits 52/53
            # RETAINED — DEC-VOCAB-VWAP-RETAIN-001, MIGRATION.md #2206).
            c.get("vwap_paisa"),
            c["prev_pdh_paisa"],
            c["prev_pdl_paisa"],
            c["prev_pdc_paisa"],
            c["day_high_paisa"],
            c["day_low_paisa"],
            c["open_of_day_paisa"],
            c["proximity"]["pivot_bps"],
            c["proximity"]["pdh_pdl_bps"],
            c["proximity"]["fib_bps"],
            c["prior"]["prior_n"],
            c["prior"]["flags"],
            c["market_structure"]["bos_bullish"],
            c["market_structure"]["bos_bearish"],
            c["market_structure"]["choch_bullish"],
            c["market_structure"]["choch_bearish"],
            **tail_kwargs,
        )
        expected = int(c["expected_mask"], 16)
        if got != expected:
            mismatches.append(f"case {c['id']}: expected {expected:#034x}, got {got:#034x}")

    print(
        f"trading_core {trading_core.version()} | TOTAL_CONDITIONS={trading_core.TOTAL_CONDITIONS}"
    )
    print(f"cases={len(cases)} mismatches={len(mismatches)}")
    if mismatches:
        print("❌ Python↔Rust binding DIVERGES from the oracle:")
        for line in mismatches[:20]:
            print(f"   {line}")
        return 1
    print("✅ Python↔Rust binding is bit-identical across all golden vectors")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
