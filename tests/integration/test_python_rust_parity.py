"""Cross-language Python ↔ Rust parity — the bit-identity contract anchor.

The single source of truth for the shared condition vocabulary + the pivot/Fib
indicators is the Rust ``trading-core`` crate (``DEC-SHARED-CORE-001``,
``docs/MIGRATION.md`` #441). This test pins the two things that the golden
suite did not directly cover before the Phase-2b post-merge hostile review:

1. **The PRODUCTION entry point.** BruteX's per-bar evaluator delegates to
   ``trading_core.evaluate_with_levels`` (pre-built levels, no recompute), but
   the dedicated CI parity check (``scripts/check_vocab_rust_parity.py``) only
   exercised ``evaluate_from_raw`` (the recompute path). Here we drive
   ``evaluate_with_levels`` **directly** over every golden case, cross-check it
   against ``evaluate_from_raw``, and exercise the ``pdc == 0`` gap-day path the
   second entry point was specifically built for (absent from the 400 fixtures).

2. **The pivot/Fib wrapper faithfully delegates.** As of Phase 2d
   ``brutex.indicators.compute_pivot_levels`` / ``compute_fib_levels`` delegate
   their arithmetic to ``trading_core.compute_pivot_levels`` /
   ``compute_fib_levels`` — the Python copy of the math was REMOVED, so the math
   is single-source (Rust). This test pins the wrapper's pack order against the
   Rust tuple over a deterministic grid, so a packing/order regression fails CI.

This file supersedes the retired ``fno-math`` parity skeleton
(``DEC-PHASE1-INTEGRATION-SKELETONS-001``); the Greeks-stub operations it
referenced were removed in ``DEC-P0-025``.
"""

from __future__ import annotations

import json
import random
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest
import trading_core
from brutex.indicators.ema import EMAState, compute_ema
from brutex.indicators.fib_levels import FibLevels, compute_fib_levels
from brutex.indicators.pivots import PivotLevels, compute_pivot_levels
from brutex.vocabulary import bits as B

_REPO_ROOT = Path(__file__).resolve().parents[2]
_FIXTURE = _REPO_ROOT / "tests" / "golden" / "fixtures" / "vocab_golden_vectors.json"
_DATA: dict[str, Any] = json.loads(_FIXTURE.read_text(encoding="utf-8"))
_CASES: list[dict[str, Any]] = _DATA["cases"]


def _tod_seconds(ts_iso: str) -> int:
    """IST wall-clock seconds-of-day from an ISO timestamp (offset ignored)."""
    ts = datetime.fromisoformat(ts_iso)
    return ts.hour * 3600 + ts.minute * 60 + ts.second


def _pivot_levels_12(pv: PivotLevels) -> list[int]:
    """The 12-element ``pivot_levels`` list ``evaluate_with_levels`` expects.

    PR-2 (DEC-NIFTY-INDICATORS-001) appended R5/S5 at indices 10/11 (the original
    10 plus the two new extended-pivot levels), so the production ABI now reads a
    12-entry list ``[p, r1, r2, r3, s1, s2, s3, pdh, pdl, pdc, r5, s5]``.
    """
    return [
        pv.p_paisa,
        pv.r1_paisa,
        pv.r2_paisa,
        pv.r3_paisa,
        pv.s1_paisa,
        pv.s2_paisa,
        pv.s3_paisa,
        pv.pdh_paisa,
        pv.pdl_paisa,
        pv.pdc_paisa,
        pv.r5_paisa,
        pv.s5_paisa,
    ]


def _fib_levels_19(fb: FibLevels) -> list[int]:
    """The 19-element ``fib_levels`` list ``evaluate_with_levels`` expects.

    DEC-FIB-TEMPLATE-EXPRESS-001 grew the evaluator fib ABI 15 -> 19: the
    bull23/bull78/up424/dn424 slots ride appended-last for bits 69-71.
    """
    return [
        fb.retrace_level_at_pdh_paisa,
        fb.retrace_23_paisa,
        fb.retrace_38_paisa,
        fb.retrace_50_paisa,
        fb.retrace_62_paisa,
        fb.retrace_78_paisa,
        fb.retrace_level_at_pdl_paisa,
        fb.ext_up_127_paisa,
        fb.ext_up_162_paisa,
        fb.ext_up_200_paisa,
        fb.ext_up_262_paisa,
        fb.ext_down_127_paisa,
        fb.ext_down_162_paisa,
        fb.ext_down_200_paisa,
        fb.ext_down_262_paisa,
        fb.retrace_bull_23_paisa,
        fb.retrace_bull_78_paisa,
        fb.ext_up_424_paisa,
        fb.ext_down_424_paisa,
    ]


def _with_levels(case: dict[str, Any], pivot_levels: list[int], fib_levels: list[int]) -> int:
    """Call the PRODUCTION entry point exactly as the BruteX shim does."""
    result: int = trading_core.evaluate_with_levels(
        _tod_seconds(case["timestamp_ist"]),
        case["open_paisa"],
        case["high_paisa"],
        case["low_paisa"],
        case["close_paisa"],
        case["ema20_paisa"],
        case["ema200_paisa"],
        # RETAINED-ACTIVE vwap slot at its pre-wipe position
        # (DEC-VOCAB-VWAP-RETAIN-001, MIGRATION.md #2206) — express cases
        # (ids >= 400) are keyless, defaulting to None.
        case.get("vwap_paisa"),
        pivot_levels,
        fib_levels,
        case["day_high_paisa"],
        case["day_low_paisa"],
        case["open_of_day_paisa"],
        case["proximity"]["pivot_bps"],
        case["proximity"]["pdh_pdl_bps"],
        case["proximity"]["fib_bps"],
        case["prior"]["prior_n"],
        case["prior"]["flags"],
        case["market_structure"]["bos_bullish"],
        case["market_structure"]["bos_bearish"],
        case["market_structure"]["choch_bullish"],
        case["market_structure"]["choch_bearish"],
        case.get("supertrend_x2_paisa"),
        # Group-15 gap-midpoint (DEC-GAP-MIDPOINT-CONDITIONS-001) —
        # appended-last, carried VERBATIM like the VWAP; the express cases
        # (ids >= 400) omit the keys, so readers default (None / 10).
        case.get("gap_mid_paisa"),
        case["proximity"].get("gap_mid_bps", 10),
        # DEC-FIB-TEMPLATE-EXPRESS-001 — appended-last swing pair + band.
        case.get("last_swing_high_paisa"),
        case.get("last_swing_low_paisa"),
        case["proximity"].get("swing_bps", 10),
    )
    return result


class TestProductionPathParity:
    """The path BruteX actually runs: ``evaluate_with_levels`` over real levels."""

    def test_evaluate_with_levels_reproduces_every_golden_vector(self) -> None:
        """The production entry point reproduces every frozen mask (u128 hex string)."""
        mismatches: list[str] = []
        for case in _CASES:
            day = datetime.fromisoformat(case["timestamp_ist"]).date()
            pv = compute_pivot_levels(
                day, case["prev_pdh_paisa"], case["prev_pdl_paisa"], case["prev_pdc_paisa"]
            )
            fb = compute_fib_levels(day, case["prev_pdh_paisa"], case["prev_pdl_paisa"])
            got = _with_levels(case, _pivot_levels_12(pv), _fib_levels_19(fb))
            expected = int(case["expected_mask"], 16)
            if got != expected:
                mismatches.append(f"case {case['id']}: expected {expected:#034x}, got {got:#034x}")
        assert not mismatches, "production-path drift:\n" + "\n".join(mismatches[:20])

    def test_evaluate_with_levels_equals_evaluate_from_raw(self) -> None:
        """Both Rust entry points agree bit-for-bit on every golden case."""
        mismatches: list[str] = []
        for case in _CASES:
            day = datetime.fromisoformat(case["timestamp_ist"]).date()
            pv = compute_pivot_levels(
                day, case["prev_pdh_paisa"], case["prev_pdl_paisa"], case["prev_pdc_paisa"]
            )
            fb = compute_fib_levels(day, case["prev_pdh_paisa"], case["prev_pdl_paisa"])
            with_levels = _with_levels(case, _pivot_levels_12(pv), _fib_levels_19(fb))
            from_raw: int = trading_core.evaluate_from_raw(
                _tod_seconds(case["timestamp_ist"]),
                case["open_paisa"],
                case["high_paisa"],
                case["low_paisa"],
                case["close_paisa"],
                case["ema20_paisa"],
                case["ema200_paisa"],
                # The retained vwap slot (DEC-VOCAB-VWAP-RETAIN-001 #2206).
                case.get("vwap_paisa"),
                case["prev_pdh_paisa"],
                case["prev_pdl_paisa"],
                case["prev_pdc_paisa"],
                case["day_high_paisa"],
                case["day_low_paisa"],
                case["open_of_day_paisa"],
                case["proximity"]["pivot_bps"],
                case["proximity"]["pdh_pdl_bps"],
                case["proximity"]["fib_bps"],
                case["prior"]["prior_n"],
                case["prior"]["flags"],
                case["market_structure"]["bos_bullish"],
                case["market_structure"]["bos_bearish"],
                case["market_structure"]["choch_bullish"],
                case["market_structure"]["choch_bearish"],
                case.get("supertrend_x2_paisa"),
                # Group-15 gap-midpoint — appended-last, carried verbatim
                # (express cases are keyless: readers default None / 10).
                case.get("gap_mid_paisa"),
                case["proximity"].get("gap_mid_bps", 10),
                # DEC-FIB-TEMPLATE-EXPRESS-001 — the swing pair + band.
                case.get("last_swing_high_paisa"),
                case.get("last_swing_low_paisa"),
                case["proximity"].get("swing_bps", 10),
            )
            if with_levels != from_raw:
                mismatches.append(
                    f"case {case['id']}: with_levels {with_levels:#015x} != from_raw {from_raw:#015x}"
                )
        assert not mismatches, "entry-point divergence:\n" + "\n".join(mismatches[:20])

    def test_evaluate_with_levels_handles_zero_pdc_gap_day(self) -> None:
        """The ``pdc == 0`` hand-built path (the reason this entry point exists).

        ``compute_pivot_levels`` rejects ``pdc == 0``; the production engine can
        carry such levels for a session with no valid prior close, and the Group-9
        gap conditions must simply stay clear (no recompute to reject them).
        """
        # [p, r1, r2, r3, s1, s2, s3, pdh, pdl, pdc=0, r5, s5] — 12 entries after
        # PR-2 appended R5/S5 at indices 10/11 (DEC-NIFTY-INDICATORS-001).
        pivot_levels = [
            24_000_00,
            24_050_00,
            24_100_00,
            24_150_00,
            23_950_00,
            23_900_00,
            23_850_00,
            24_050_00,
            23_950_00,
            0,
            24_250_00,
            23_750_00,
        ]
        # 19 fib slots since the express ABI growth (FIB_LEVELS_LEN = 19).
        fib_levels = [24_050_00] * 19
        mask: int = trading_core.evaluate_with_levels(
            10 * 3600 + 30 * 60,  # 10:30 IST
            24_000_00,
            24_010_00,
            23_990_00,
            24_005_00,
            None,
            None,
            None,  # vwap_paisa — the retained slot (#2206); None = abstain
            pivot_levels,
            fib_levels,
            24_010_00,
            23_980_00,
            24_000_00,
            10,
            10,
            10,
            3,
            [],
            False,  # bos_bullish — no market-structure break in this gap-day case
            False,  # bos_bearish
            False,  # choch_bullish
            False,  # choch_bearish
        )
        assert not (mask & (1 << B.BIT_GAP_UP_DAY)), "gap-up must stay clear when pdc==0"
        assert not (mask & (1 << B.BIT_GAP_DOWN_DAY)), "gap-down must stay clear when pdc==0"


class TestIndicatorComputeParity:
    """Pin the wrapper's pack order against the Rust tuple.

    Post-Phase-2d the Python ``compute_*`` wrappers DELEGATE to these same Rust
    bindings (the math is single-source), so this no longer guards math
    divergence (the Rust-side golden test + the 400 frozen vectors do that) — it
    catches a pack-order / field-transposition bug in the wrappers.
    """

    def test_rust_pivot_compute_matches_python(self) -> None:
        """``trading_core.compute_pivot_levels`` == the Python wrapper, bit-for-bit."""
        rnd = random.Random(20260603)
        day = date(2026, 5, 13)
        mismatches = 0
        for _ in range(5000):
            pdl = rnd.randint(1_00, 80_000_00)
            pdh = pdl + rnd.randint(0, 5_000_00)
            pdc = rnd.randint(pdl, pdh)
            pv = compute_pivot_levels(day, pdh, pdl, pdc)
            rust = trading_core.compute_pivot_levels(pdh, pdl, pdc)
            # R5/S5 appended in PR-2 (DEC-NIFTY-INDICATORS-001): the wrapper now
            # packs 11 entries ``[r5, r4, r3, r2, r1, p, s1, s2, s3, s4, s5]``.
            expected = [
                pv.r5_paisa,
                pv.r4_paisa,
                pv.r3_paisa,
                pv.r2_paisa,
                pv.r1_paisa,
                pv.p_paisa,
                pv.s1_paisa,
                pv.s2_paisa,
                pv.s3_paisa,
                pv.s4_paisa,
                pv.s5_paisa,
            ]
            if list(rust) != expected:
                mismatches += 1
        assert mismatches == 0, f"{mismatches} pivot compute divergences"

    def test_rust_fib_compute_matches_python(self) -> None:
        """``trading_core.compute_fib_levels`` == the Python wrapper, bit-for-bit."""
        rnd = random.Random(20260604)
        day = date(2026, 5, 13)
        mismatches = 0
        for _ in range(5000):
            pdl = rnd.randint(1_00, 80_000_00)
            pdh = pdl + rnd.randint(0, 5_000_00)
            fb = compute_fib_levels(day, pdh, pdl)
            rust = trading_core.compute_fib_levels(pdh, pdl)
            expected = [
                fb.retrace_23_paisa,
                fb.retrace_38_paisa,
                fb.retrace_50_paisa,
                fb.retrace_62_paisa,
                fb.retrace_78_paisa,
                fb.ext_up_127_paisa,
                fb.ext_up_162_paisa,
                fb.ext_up_200_paisa,
                fb.ext_up_262_paisa,
                fb.ext_up_424_paisa,
                fb.ext_down_127_paisa,
                fb.ext_down_162_paisa,
                fb.ext_down_200_paisa,
                fb.ext_down_262_paisa,
                fb.ext_down_424_paisa,
                # The bullish retracement tail, appended LAST to the compute-FFI
                # projection (19 entries; the evaluator ABI stays 15 —
                # DEC-FIB-BULLISH-RETRACE-001).
                fb.retrace_bull_23_paisa,
                fb.retrace_bull_38_paisa,
                fb.retrace_bull_62_paisa,
                fb.retrace_bull_78_paisa,
            ]
            if list(rust) != expected:
                mismatches += 1
        assert mismatches == 0, f"{mismatches} fib compute divergences"


class TestFfiOverflowGuards:
    """DEC-ADVERSARIAL-HARDENING-001 — the FFI boundary rejects an overflow-prone
    input with a catchable ``ValueError`` instead of aborting the interpreter
    (SIGABRT) or returning a poisoning non-finite value.

    These exercise the PRODUCTION Rust entry points BruteX's ``stage1a`` engine
    drives directly (``trading_core.compute_ema``; the ``VwapState`` pyclass
    is RETAINED-LIVE again per DEC-VOCAB-VWAP-RETAIN-001 (MIGRATION.md #2206)
    and carries its own in-crate guards), NOT the pure-Python shims (which
    use unbounded ``int`` arithmetic and never overflow), so they reproduce
    the exact adversarial-sweep findings.
    """

    def test_compute_ema_finite_but_huge_seed_raises_not_inf(self) -> None:
        """FIX 4: two finite-but-huge closes (1e308) overflow the SMA seed sum to
        +inf, which previously returned a poisoning ``inf`` EMA. Now the computed
        seed is re-checked and a catchable ``ValueError`` is raised."""
        with pytest.raises(ValueError, match="seed overflowed"):
            trading_core.compute_ema([1e308, 1e308], 2)

    def test_compute_ema_real_series_byte_identical(self) -> None:
        """A normal-magnitude series is byte-identical (the guard never fires)."""
        assert trading_core.compute_ema([10.0, 20.0, 30.0, 40.0], 2) == [
            None,
            15.0,
            25.0,
            35.0,
        ]


class TestEmaStreamingSeedParity:
    """DEC-EMA-PARITY-001 — the Python STREAMING EMA path (``EMAState.update``,
    the LIVE ``engine.stage1a`` hot path) must behave IDENTICALLY to the Python
    batch path and the Rust core on a seed that overflows to a non-finite value.

    Before this fix the streaming ``update`` returned a poisoning ``+inf`` on an
    overflowed SMA seed (the bare ``trading_core.ema_seed`` FFI is a pure
    division that only guards ``period < 1``), while BOTH the Rust
    ``EmaState::update`` seed guard and the Python batch ``compute_ema`` (which
    delegates to that same Rust ``update``) RAISED. This pins streaming == batch
    == Rust so the divergence can never silently regress.
    """

    @staticmethod
    def _stream(values: list[float], period: int) -> list[float | None]:
        state = EMAState.new(period)
        return [state.update(v) for v in values]

    @staticmethod
    def _value_error_message(fn: Any) -> str:
        try:
            fn()
        except ValueError as exc:
            return str(exc)
        raise AssertionError("expected a ValueError but none was raised")  # pragma: no cover

    def test_streaming_seed_overflow_raises_like_batch_and_rust(self) -> None:
        """Streaming, batch, and Rust all raise the byte-identical ValueError."""
        values = [1e308, 1e308]

        def _stream_raise() -> None:
            self._stream(values, 2)

        streaming_msg = self._value_error_message(_stream_raise)
        batch_msg = self._value_error_message(lambda: compute_ema(values, 2))
        rust_msg = self._value_error_message(lambda: trading_core.compute_ema(values, 2))

        assert streaming_msg == batch_msg == rust_msg
        assert "seed overflowed to a non-finite value" in streaming_msg

    def test_streaming_finite_series_bit_identical_to_batch_and_rust(self) -> None:
        """A real-magnitude series streams == batch == Rust bit-for-bit (the
        guard never fires on normal inputs)."""
        values = [10.0, 20.0, 30.0, 40.0, 50.0]
        streamed = self._stream(values, 3)
        assert streamed == compute_ema(values, 3)
        assert streamed == trading_core.compute_ema(values, 3)
