"""Slice-2 fold-arm seam units — DESIGN_FINAL_RUST_SWEEP_CORE.md §1.3/§5/§7.4.

Covers the ``BRUTEX_SWEEP_RUST_CORE`` plan resolution (arm tristate fail-OFF,
belt INVERSE fail-ON, fold-cells dual clamp, oracle-ns raise-only clamp), the
layout golden (Python tuples == wheel exports + strides + ABI + LE +
itemsize), the §1.3 decode refusal law vectors, the in-band ``scans_run``
seam pin, the width-keyed fold burn-in window + latch, the bit-compare +
three-way disambiguation, the mechanised-pure oracle (independent-oracle
parity + the AST closed-set pin + the runtime poison pin), and the fold-arm
engagement chain in §5 precedence order.
"""

from __future__ import annotations

import ast
import inspect
import struct
import sys
import textwrap
from collections.abc import Iterator, MutableMapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import fno_math
import pytest
from brutex.data.models import CandleBar
from brutex.engine import exit_index, sweep_core, trade_sim
from brutex.engine.combo_mask import count_combos
from brutex.engine.sweep_core import (
    DEFAULT_FOLD_WORK_CELLS,
    DEFAULT_ORACLE_NS_PER_BAR,
    EXPECTED_SWEEP_CORE_ABI_VERSION,
    FOLD_CELLS_LOWER_CLAMP,
    FOLD_CELLS_UPPER_CLAMP,
    FOLD_FLAG_PYTHON_REQUIRED,
    FOLD_FLAG_ZERO_TRADE,
    FOLD_FLOAT_FIELDS,
    FOLD_FLOAT_STRIDE,
    FOLD_INT_FIELDS,
    FOLD_INT_STRIDE,
    ORACLE_NS_UPPER_FACTOR,
    SWEEP_CORE_CHUNK,
    SWEEP_CORE_ORACLE_NS_ENV,
    SWEEP_CORE_SELF_CHECK,
    SWEEP_CORE_SELF_CHECK_CAP,
    SWEEP_CORE_SELF_CHECK_NONTRIVIAL,
    SWEEP_CORE_SPOT_CANDIDATES,
    SWEEP_CORE_WORK_CELLS_ENV,
    SWEEP_RUST_CORE_BELT_ENV,
    SWEEP_RUST_CORE_ENV,
    CoreDecodeRefused,
    CorePlan,
    consume_fold_burnin,
    core_fold_context,
    decode_fold_block,
    disambiguate_core_mismatch,
    fold_burnin_open,
    fold_burnin_total_remaining,
    fold_chunk_masks,
    fold_latch_reason,
    fold_row_metrics,
    fold_span_years,
    is_kernel_panic,
    latch_fold_kernel,
    metrics_compare_key,
    metrics_equal,
    reset_enum_self_check_for_tests,
    reset_observability_memo_for_tests,
    reset_self_check_for_tests,
    resolve_sweep_rust_core_plan,
    spot_check_candidates,
    zero_trade_metrics,
)
from brutex.engine.sweep_driver import metrics_from_trades
from brutex.metrics.compute import ResultMetrics
from brutex.strategies.types import Side
from structlog.testing import capture_logs

_IST = timezone(timedelta(hours=5, minutes=30))


@pytest.fixture(autouse=True)
def _fresh_seam_state() -> Iterator[None]:
    reset_enum_self_check_for_tests()
    reset_self_check_for_tests()
    reset_observability_memo_for_tests()
    yield
    reset_enum_self_check_for_tests()
    reset_self_check_for_tests()
    reset_observability_memo_for_tests()


def _bars(n: int = 32) -> list[CandleBar]:
    out: list[CandleBar] = []
    price = 100.0
    for i in range(n):
        ts = (datetime(2026, 1, 5, 9, 16, tzinfo=_IST) + timedelta(minutes=i)).astimezone(UTC)
        close = price + (0.6 if i % 3 else -0.3)
        out.append(
            CandleBar(
                timestamp=ts,
                open=Decimal(str(round(price, 2))),
                high=Decimal(str(round(max(price, close) + 0.5, 2))),
                low=Decimal(str(round(min(price, close) - 0.5, 2))),
                close=Decimal(str(round(close, 2))),
                volume=100,
                open_interest=None,
            )
        )
        price = close
    return out


def _fallback_reasons(logs: Sequence[MutableMapping[str, Any]]) -> list[str]:
    return [str(e["reason"]) for e in logs if e["event"] == "sweep_rust_core.fallback"]


def _plan(**overrides: Any) -> CorePlan:
    base: dict[str, Any] = {
        "requested": True,
        "belt": True,
        "fold_cells": DEFAULT_FOLD_WORK_CELLS,
        "oracle_ns_per_bar": DEFAULT_ORACLE_NS_PER_BAR,
    }
    base.update(overrides)
    return CorePlan(**base)


def _ctx(
    *,
    bars: list[CandleBar] | None = None,
    bar_bits: list[int] | None = None,
    bits: list[int] | None = None,
    k: int = 2,
    digest_path: bool = True,
    top_k: int | None = 5,
    plan: CorePlan | None = None,
) -> sweep_core.CoreFoldContext | None:
    bars = _bars() if bars is None else bars
    bar_bits = (
        [0b1111 if i % 2 else 0b0101 for i in range(len(bars))] if bar_bits is None else bar_bits
    )
    bits = [0, 1, 2, 3] if bits is None else bits
    return core_fold_context(
        bars=bars,
        bar_bits=bar_bits,
        condition_bits=bits,
        tf_minutes=1,
        n_masks=count_combos(k, len(set(bits))),
        digest_path=digest_path,
        top_k=top_k,
        plan=plan if plan is not None else _plan(),
    )


class TestCorePlanResolution:
    """§12.2: arm tristate fail-OFF; belt INVERSE fail-ON; knob clamps."""

    def test_unset_defaults_off_and_knobs_inert(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(SWEEP_RUST_CORE_ENV, raising=False)
        monkeypatch.setenv(SWEEP_CORE_WORK_CELLS_ENV, "garbage")
        monkeypatch.setenv(SWEEP_CORE_ORACLE_NS_ENV, "garbage")
        plan = resolve_sweep_rust_core_plan()
        assert plan == CorePlan(
            requested=False,
            belt=True,
            fold_cells=DEFAULT_FOLD_WORK_CELLS,
            oracle_ns_per_bar=DEFAULT_ORACLE_NS_PER_BAR,
        )

    def test_falsy_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(SWEEP_RUST_CORE_ENV, "0")
        assert resolve_sweep_rust_core_plan().requested is False

    def test_unrecognised_arm_fails_safe_off_with_warn(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(SWEEP_RUST_CORE_ENV, "bananas")
        with capture_logs() as logs:
            plan = resolve_sweep_rust_core_plan()
        assert plan.requested is False
        assert _fallback_reasons(logs) == ["flag_unrecognised"]

    def test_belt_default_on_and_off_parses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(SWEEP_RUST_CORE_ENV, "1")
        monkeypatch.delenv(SWEEP_RUST_CORE_BELT_ENV, raising=False)
        assert resolve_sweep_rust_core_plan().belt is True
        monkeypatch.setenv(SWEEP_RUST_CORE_BELT_ENV, "0")
        assert resolve_sweep_rust_core_plan().belt is False
        monkeypatch.setenv(SWEEP_RUST_CORE_BELT_ENV, "on")
        assert resolve_sweep_rust_core_plan().belt is True

    def test_belt_unrecognised_fails_on_with_warn(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # INVERSE fail-ON: a typo'd kill switch must still BELT (§5).
        monkeypatch.setenv(SWEEP_RUST_CORE_ENV, "1")
        monkeypatch.setenv(SWEEP_RUST_CORE_BELT_ENV, "banans")
        with capture_logs() as logs:
            plan = resolve_sweep_rust_core_plan()
        assert plan.belt is True
        assert _fallback_reasons(logs) == ["belt_flag_unrecognised"]

    def test_fold_cells_junk_fails_loud(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(SWEEP_RUST_CORE_ENV, "1")
        monkeypatch.setenv(SWEEP_CORE_WORK_CELLS_ENV, "not-a-number")
        with pytest.raises(ValueError, match=SWEEP_CORE_WORK_CELLS_ENV):
            resolve_sweep_rust_core_plan()

    @pytest.mark.parametrize(
        ("raw", "expected", "direction"),
        [
            ("1", FOLD_CELLS_LOWER_CLAMP, "lower"),
            (str(2**44), FOLD_CELLS_UPPER_CLAMP, "upper"),
        ],
    )
    def test_fold_cells_dual_clamp(
        self, monkeypatch: pytest.MonkeyPatch, raw: str, expected: int, direction: str
    ) -> None:
        monkeypatch.setenv(SWEEP_RUST_CORE_ENV, "1")
        monkeypatch.setenv(SWEEP_CORE_WORK_CELLS_ENV, raw)
        with capture_logs() as logs:
            plan = resolve_sweep_rust_core_plan()
        assert plan.fold_cells == expected
        clamps = [e for e in logs if e["event"] == "sweep_core.work_cap_clamped"]
        assert [str(e["direction"]) for e in clamps] == [direction]
        assert clamps[0]["env"] == SWEEP_CORE_WORK_CELLS_ENV

    def test_fold_cells_in_range_passthrough(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(SWEEP_RUST_CORE_ENV, "1")
        monkeypatch.setenv(SWEEP_CORE_WORK_CELLS_ENV, str(1 << 20))
        assert resolve_sweep_rust_core_plan().fold_cells == 1 << 20

    @pytest.mark.parametrize("raw", ["junk", "inf", "nan", "-inf"])
    def test_oracle_ns_junk_and_nonfinite_fail_loud(
        self, monkeypatch: pytest.MonkeyPatch, raw: str
    ) -> None:
        monkeypatch.setenv(SWEEP_RUST_CORE_ENV, "1")
        monkeypatch.setenv(SWEEP_CORE_ORACLE_NS_ENV, raw)
        with pytest.raises(ValueError, match=SWEEP_CORE_ORACLE_NS_ENV):
            resolve_sweep_rust_core_plan()

    def test_oracle_ns_raise_only_lower_clamp(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # §5 round-6: a digit-dropping typo can never UNDER-price the guard.
        monkeypatch.setenv(SWEEP_RUST_CORE_ENV, "1")
        monkeypatch.setenv(SWEEP_CORE_ORACLE_NS_ENV, "4.0")
        with capture_logs() as logs:
            plan = resolve_sweep_rust_core_plan()
        assert plan.oracle_ns_per_bar == DEFAULT_ORACLE_NS_PER_BAR
        clamps = [e for e in logs if e["event"] == "sweep_core.stall_guard_clamped"]
        assert [str(e["direction"]) for e in clamps] == ["lower"]

    def test_oracle_ns_upper_clamp_and_passthrough(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(SWEEP_RUST_CORE_ENV, "1")
        monkeypatch.setenv(SWEEP_CORE_ORACLE_NS_ENV, "1e12")
        with capture_logs() as logs:
            plan = resolve_sweep_rust_core_plan()
        assert plan.oracle_ns_per_bar == DEFAULT_ORACLE_NS_PER_BAR * ORACLE_NS_UPPER_FACTOR
        assert [
            str(e["direction"]) for e in logs if e["event"] == "sweep_core.stall_guard_clamped"
        ] == ["upper"]
        monkeypatch.setenv(SWEEP_CORE_ORACLE_NS_ENV, "5000.0")
        assert resolve_sweep_rust_core_plan().oracle_ns_per_bar == 5000.0


class TestLayoutGolden:
    """§1.3 ONE layout law: Python tuples == wheel exports, literal compare."""

    def test_wheel_tuples_equal_python_tuples(self) -> None:
        assert tuple(fno_math.SWEEP_CORE_FOLD_INT_FIELDS) == FOLD_INT_FIELDS
        assert tuple(fno_math.SWEEP_CORE_FOLD_FLOAT_FIELDS) == FOLD_FLOAT_FIELDS

    def test_strides_and_abi(self) -> None:
        assert FOLD_INT_STRIDE == 15
        assert FOLD_FLOAT_STRIDE == 9
        assert fno_math.SWEEP_CORE_ABI_VERSION == EXPECTED_SWEEP_CORE_ABI_VERSION == 1

    def test_host_transport_preconditions(self) -> None:
        # LE + itemsize==8 — the decode law's hardcoded ×8 arithmetic.
        assert sys.byteorder == "little"
        assert sweep_core._int_itemsize() == 8
        assert sweep_core._float_itemsize() == 8


def _real_block(
    masks: list[int],
    *,
    bars: list[CandleBar] | None = None,
    bar_bits: list[int] | None = None,
    start_equity: int = 10_000_000,
) -> tuple[tuple[int, int, bytes, bytes, bytes], list[CandleBar], list[int]]:
    from brutex.engine.rust_trade_sim import derive_kernel_inputs

    bars = _bars() if bars is None else bars
    bar_bits = (
        [0b1111 if i % 2 else 0b0101 for i in range(len(bars))] if bar_bits is None else bar_bits
    )
    o, h, lo, c, forced, days = derive_kernel_inputs(bars, 1)
    core = fno_math.ResidentSweepCore(o, h, lo, c, forced, days, bar_bits)
    return (
        core.fold_block(masks, True, 80, 50, False, False, False, start_equity),
        bars,
        bar_bits,
    )


class TestDecodeRefusalLaw:
    """§1.3: every malformed return is a NAMED refusal, never a silent row."""

    def test_clean_decode_round_trip(self) -> None:
        block, _, _ = _real_block([0b01, 0b10])
        decoded = decode_fold_block(block, 2)
        assert len(decoded.flags) == 2
        assert len(decoded.ints) == 2 * FOLD_INT_STRIDE
        assert len(decoded.floats) == 2 * FOLD_FLOAT_STRIDE

    def test_abi_version_mismatch(self) -> None:
        block, _, _ = _real_block([0b01])
        bad = (block[0] + 1, *block[1:])
        with pytest.raises(CoreDecodeRefused) as exc:
            decode_fold_block(bad, 1)
        assert exc.value.reason == "abi_version_mismatch"

    def test_row_count_mismatch_truncated_but_stride_clean(self) -> None:
        # A stride-clean n−1-row block passes divisibility — the refusal law
        # must catch it via len(flags) vs the SUBMITTED chunk length.
        block, _, _ = _real_block([0b01, 0b10])
        abi, scans, ints_le, floats_le, flags = block
        truncated = (
            abi,
            scans,
            ints_le[: FOLD_INT_STRIDE * 8],
            floats_le[: FOLD_FLOAT_STRIDE * 8],
            flags[:1],
        )
        with pytest.raises(CoreDecodeRefused) as exc:
            decode_fold_block(truncated, 2)
        assert exc.value.reason == "row_count_mismatch"

    def test_cross_buffer_length_mismatches(self) -> None:
        block, _, _ = _real_block([0b01, 0b10])
        abi, scans, ints_le, floats_le, flags = block
        with pytest.raises(CoreDecodeRefused) as exc:
            decode_fold_block((abi, scans, ints_le[:-8], floats_le, flags), 2)
        assert exc.value.reason == "ints_length_mismatch"
        with pytest.raises(CoreDecodeRefused) as exc:
            decode_fold_block((abi, scans, ints_le, floats_le[:-8], flags), 2)
        assert exc.value.reason == "floats_length_mismatch"

    def test_reserved_flag_bits_refused(self) -> None:
        block, _, _ = _real_block([0b01, 0b10])
        abi, scans, ints_le, floats_le, flags = block
        poisoned = bytes([flags[0], flags[1] | 0x04])
        with pytest.raises(CoreDecodeRefused) as exc:
            decode_fold_block((abi, scans, ints_le, floats_le, poisoned), 2)
        assert exc.value.reason == "reserved_flag_bits"


class TestScansRunSeamPin:
    """§1.3 zero-trade short-circuit law — the in-band byte-observable oracle."""

    def test_scans_run_equals_plain_rows(self) -> None:
        # bit 3 never fires below → mask 0b1000 is a zero-trade row.
        bars = _bars()
        bar_bits = [0b0111 if i % 2 else 0b0101 for i in range(len(bars))]
        block, _, _ = _real_block([0b0001, 0b1000, 0b0010], bars=bars, bar_bits=bar_bits)
        decoded = decode_fold_block(block, 3)
        plain = sum(1 for f in decoded.flags if f == 0)
        assert decoded.flags[1] & FOLD_FLAG_ZERO_TRADE
        assert decoded.scans_run == plain

    def test_all_zero_trade_chunk_runs_zero_scans(self) -> None:
        bars = _bars()
        bar_bits = [0b0011] * len(bars)
        block, _, _ = _real_block([0b1000, 0b0100], bars=bars, bar_bits=bar_bits)
        decoded = decode_fold_block(block, 2)
        assert all(f & FOLD_FLAG_ZERO_TRADE for f in decoded.flags)
        assert decoded.scans_run == 0

    def test_python_required_row_counts_no_scan(self) -> None:
        # SCAN-LAST ordering (§1.3 round-4): a bit1 trip precedes the scan.
        block, _, _ = _real_block([0b0001], start_equity=(1 << 52) - 1)
        decoded = decode_fold_block(block, 1)
        assert decoded.flags[0] & FOLD_FLAG_PYTHON_REQUIRED
        assert decoded.scans_run == 0


class TestFoldBurnin:
    """§5 width-keyed dual-counter window + cap + latch."""

    def test_window_closure_needs_both_floors(self) -> None:
        assert fold_burnin_open(64)
        for _ in range(SWEEP_CORE_SELF_CHECK):
            consume_fold_burnin(64, plain=False)
        # 64 total met, plain floor unmet → still open.
        assert fold_burnin_open(64)
        assert fold_burnin_total_remaining(64) == 0
        for _ in range(SWEEP_CORE_SELF_CHECK_NONTRIVIAL):
            consume_fold_burnin(64, plain=True)
        assert not fold_burnin_open(64)

    def test_cap_trip_with_plain_floor_unmet_warns_once_and_closes(self) -> None:
        with capture_logs() as logs:
            for _ in range(SWEEP_CORE_SELF_CHECK_CAP):
                consume_fold_burnin(64, plain=False)
            consume_fold_burnin(128, plain=False)  # other width unaffected
        assert not fold_burnin_open(64)
        assert fold_burnin_total_remaining(64) == 0
        assert fold_burnin_open(128)
        floor_warns = [
            e for e in logs if str(e.get("reason", "")) == "burnin_nontrivial_floor_unmet"
        ]
        assert len(floor_warns) == 1
        assert floor_warns[0]["width"] == 64

    def test_width_keyed_counters_and_latch(self) -> None:
        consume_fold_burnin(64, plain=True)
        assert fold_burnin_total_remaining(64) == SWEEP_CORE_SELF_CHECK - 1
        assert fold_burnin_total_remaining(128) == SWEEP_CORE_SELF_CHECK
        with capture_logs() as logs:
            latch_fold_kernel(64, "self_check_mismatch")
            latch_fold_kernel(64, "belt_mismatch")  # second flip is a no-op
        assert fold_latch_reason(64) == "self_check_mismatch"
        assert fold_latch_reason(128) is None
        reverts = [e for e in logs if e["event"] == "sweep_rust_core.kernel_autoreverted"]
        assert len(reverts) == 1

    def test_reset_seam(self) -> None:
        latch_fold_kernel(64, "x")
        reset_self_check_for_tests(width=64, total_remaining=2, plain_remaining=1, latched=None)
        assert fold_latch_reason(64) is None
        assert fold_burnin_total_remaining(64) == 2


class TestCompareAndDisambiguate:
    """§5 bit-compare law + the three-way verdicts."""

    def _metrics(self) -> ResultMetrics:
        bars = _bars()
        return metrics_from_trades(bars, [], start_equity_paisa=10_000_000, tf_minutes=1)

    def test_signed_zero_and_ulp_distinguished(self) -> None:
        m = self._metrics()
        assert metrics_equal(m, m)
        assert not metrics_equal(m, replace(m, total_return_pct=-0.0))
        key = metrics_compare_key(m)
        assert struct.pack("<d", 0.0) in key

    def test_none_bits_exact(self) -> None:
        m = self._metrics()
        assert m.sharpe_ratio is None
        assert not metrics_equal(m, replace(m, sharpe_ratio=0.0))

    def test_verdicts(self) -> None:
        m = self._metrics()
        other = replace(m, net_pnl_paisa=m.net_pnl_paisa + 1)
        third = replace(m, net_pnl_paisa=m.net_pnl_paisa + 2)
        assert disambiguate_core_mismatch(m, m, m) == "clean"
        assert disambiguate_core_mismatch(other, m, m) == "core_defect"
        assert disambiguate_core_mismatch(m, m, other) == "preexisting"
        assert disambiguate_core_mismatch(m, other, third) == "threeway"

    def test_is_kernel_panic(self) -> None:
        panic_cls = type("PanicException", (Exception,), {})
        assert is_kernel_panic(panic_cls("boom"))
        assert not is_kernel_panic(ValueError("boom"))


class TestFoldSizingAndSampling:
    def test_fold_chunk_masks_divisor_floored(self) -> None:
        assert fold_chunk_masks(0, DEFAULT_FOLD_WORK_CELLS) == SWEEP_CORE_CHUNK
        assert fold_chunk_masks(10**9, DEFAULT_FOLD_WORK_CELLS) == 1
        assert fold_chunk_masks(2048, 1 << 23) == SWEEP_CORE_CHUNK
        assert fold_chunk_masks(1 << 20, 1 << 23) == 8

    def test_spot_candidates_small_kept_identity(self) -> None:
        kept = list(range(10))
        assert spot_check_candidates(kept, "0f" * 8) == kept

    def test_spot_candidates_deterministic_subset(self) -> None:
        kept = list(range(100, 200))
        first = spot_check_candidates(kept, "0123456789abcdef")
        second = spot_check_candidates(kept, "0123456789abcdef")
        assert first == second
        assert len(first) == SWEEP_CORE_SPOT_CANDIDATES
        assert set(first) <= set(kept)
        assert spot_check_candidates(kept, "fedcba9876543210") != first


class TestRowMetrics:
    """§1.4 row routing: plain rows via combine, zero-trade via the flat call —
    the INDEPENDENT-ORACLE parity differential (never live compute.py leaf
    calls on the expectation side: the expected object is built through the
    PURE curve pipeline, a different code path than the fold+combine chain)."""

    def test_zero_trade_metrics_matches_spine(self) -> None:
        bars = _bars()
        expected = metrics_from_trades(bars, [], start_equity_paisa=10_000_000, tf_minutes=1)
        got = zero_trade_metrics(
            total_bars=len(bars),
            start_equity_paisa=10_000_000,
            span_years=fold_span_years(bars, 1),
        )
        assert metrics_equal(got, expected)

    def test_fold_span_years_mirrors_kernel_arm(self) -> None:
        bars = _bars()
        from brutex.engine.sweep_driver import _MINUTES_PER_YEAR

        span_seconds = (bars[-1].timestamp - bars[0].timestamp).total_seconds()
        assert fold_span_years(bars, 1) == span_seconds / (_MINUTES_PER_YEAR * 60.0)
        one_bar = bars[:1]
        assert fold_span_years(one_bar, 3) == 3 / _MINUTES_PER_YEAR

    def test_plain_row_parity_vs_pure_oracle(self) -> None:
        # The mandated parity differential: the real kernel's fold row,
        # combined Python-side, must be BIT-identical to the mechanised-pure
        # oracle (walker + curve + compute_metrics — no fno_math anywhere).
        masks = [0b0001, 0b0010, 0b0100, 0b0011]
        block, bars, bar_bits = _real_block(masks)
        decoded = decode_fold_block(block, len(masks))
        span = fold_span_years(bars, 1)
        for row, mask in enumerate(masks):
            if decoded.flags[row]:
                continue
            got = fold_row_metrics(
                decoded, row, total_bars=len(bars), start_equity_paisa=10_000_000, span_years=span
            )
            expected = sweep_core._oracle_metrics_pure(
                bars,
                bar_bits,
                mask,
                direction=Side.BUY,
                tf_minutes=1,
                stop_loss_paisa=80,
                take_profit_paisa=50,
                start_equity_paisa=10_000_000,
            )
            assert metrics_equal(got, expected), f"mask {mask:#x} diverged"

    def test_parity_teeth_a_perturbed_m3_fails(self) -> None:
        # The Pébay-m3 sensitivity teeth: flipping the fold's m3 sign (the
        # delta-sign mutant class) must FAIL the parity compare — proves the
        # differential is not vacuous on the skew-bearing column.
        masks = [0b0011]
        block, bars, bar_bits = _real_block(masks)
        abi, scans, ints_le, floats_le, flags = block
        assert flags[0] == 0
        from array import array

        floats = array("d")
        floats.frombytes(floats_le)
        assert floats[2] != 0.0  # m3 present
        floats[2] = -floats[2]
        decoded = decode_fold_block((abi, scans, ints_le, floats.tobytes(), flags), 1)
        got = fold_row_metrics(
            decoded,
            0,
            total_bars=len(bars),
            start_equity_paisa=10_000_000,
            span_years=fold_span_years(bars, 1),
        )
        expected = sweep_core._oracle_metrics_pure(
            bars,
            bar_bits,
            masks[0],
            direction=Side.BUY,
            tf_minutes=1,
            stop_loss_paisa=80,
            take_profit_paisa=50,
            start_equity_paisa=10_000_000,
        )
        assert not metrics_equal(got, expected)


#: The §5 closed set: every function body the mechanised-pure oracle may
#: transitively execute. An out-of-set call or ANY fno_math reference fails.
def _closed_set_sources() -> dict[str, str]:
    from brutex.engine import combo_mask, sweep_driver
    from brutex.metrics import compute

    out: dict[str, str] = {
        "_oracle_metrics_pure": inspect.getsource(sweep_core._oracle_metrics_pure),
        "mask_to_entry_signals": inspect.getsource(combo_mask.mask_to_entry_signals),
        "simulate_trades": inspect.getsource(trade_sim.simulate_trades),
        "build_equity_curve": inspect.getsource(sweep_driver.build_equity_curve),
        "build_ts_index": inspect.getsource(exit_index.build_ts_index),
        "compute_metrics": inspect.getsource(compute.compute_metrics),
        "_compute_metrics_impl": inspect.getsource(compute._compute_metrics_impl),
        "_drawdown_stats": inspect.getsource(compute._drawdown_stats),
        "_fold_trades": inspect.getsource(compute._fold_trades),
        "combine_metrics_from_folds": inspect.getsource(compute.combine_metrics_from_folds),
        "_empty_metrics": inspect.getsource(compute._empty_metrics),
        "_accumulate_moments": inspect.getsource(compute._accumulate_moments),
    }
    # TradeSimWalker: simulate_trades delegates to the walker class.
    out["TradeSimWalker"] = inspect.getsource(trade_sim.TradeSimWalker)
    return out


class TestOracleIndependencePins:
    """§5: the pure oracle's independence is MECHANISED, not asserted."""

    def test_ast_closed_set_no_fno_math_reference(self) -> None:
        for name, source in _closed_set_sources().items():
            tree = ast.parse(textwrap.dedent(source))
            for node in ast.walk(tree):
                if isinstance(node, ast.Name) and node.id == "fno_math":
                    pytest.fail(f"{name} references fno_math by Name")
                if isinstance(node, ast.Attribute) and node.attr == "fno_math":
                    pytest.fail(f"{name} references fno_math by Attribute")
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    modules = [a.name for a in node.names]
                    if isinstance(node, ast.ImportFrom) and node.module:
                        modules.append(node.module)
                    banned = [m for m in modules if "fno_math" in m or "rust_metrics" in m]
                    assert not banned, f"{name} imports {banned}"

    def test_runtime_poison_pin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Poison the ONE fno_math symbol the shipping metrics tail routes
        # through; the pure oracle's output must be unmoved (never consulted).
        bars = _bars()
        bar_bits = [0b0011] * len(bars)
        kwargs: dict[str, Any] = {
            "direction": Side.BUY,
            "tf_minutes": 1,
            "stop_loss_paisa": 80,
            "take_profit_paisa": 50,
            "start_equity_paisa": 10_000_000,
        }
        baseline = sweep_core._oracle_metrics_pure(bars, bar_bits, 0b0001, **kwargs)

        def _poison(*args: object, **kw: object) -> object:
            raise AssertionError("pure oracle consulted fno_math.sweep_drawdown_stats")

        monkeypatch.setattr(fno_math, "sweep_drawdown_stats", _poison, raising=True)
        poisoned = sweep_core._oracle_metrics_pure(bars, bar_bits, 0b0001, **kwargs)
        assert metrics_equal(baseline, poisoned)


class TestFoldEngagementChain:
    """§5 CORE column, precedence order; each refusal warns its ONE reason."""

    def test_engages_on_real_wheel(self) -> None:
        with capture_logs() as logs:
            ctx = _ctx()
        assert ctx is not None
        assert ctx.width == 64
        assert ctx.n_bars == 32
        assert ctx.chunk_masks >= 1
        engaged = [e for e in logs if e["event"] == "sweep_rust_core.engaged"]
        assert len(engaged) == 1

    def test_not_requested_is_silent(self) -> None:
        with capture_logs() as logs:
            assert _ctx(plan=_plan(requested=False)) is None
        assert not _fallback_reasons(logs)

    def test_legacy_dedup_path_wins_precedence_over_top_k(self) -> None:
        # The pinned precedence-law fixture: digest_path=False AND top_k=None
        # fail together — ONE warn, the FIRST listed condition.
        with capture_logs() as logs:
            assert _ctx(digest_path=False, top_k=None) is None
        assert _fallback_reasons(logs) == ["legacy_dedup_path"]

    def test_wheel_missing_and_stale(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sweep_core, "_load_kernel_module", lambda: None)
        with capture_logs() as logs:
            assert _ctx() is None
        assert _fallback_reasons(logs) == ["wheel_missing"]

        class _Stale:
            ResidentSweepCore = object
            ResidentSweepCoreU128 = object
            SWEEP_CORE_ABI_VERSION = EXPECTED_SWEEP_CORE_ABI_VERSION
            SWEEP_CORE_FOLD_INT_FIELDS = ("wrong",)
            SWEEP_CORE_FOLD_FLOAT_FIELDS = FOLD_FLOAT_FIELDS

        stale = _Stale()
        monkeypatch.setattr(sweep_core, "_load_kernel_module", lambda: stale)
        with capture_logs() as logs:
            assert _ctx() is None
        assert _fallback_reasons(logs) == ["wheel_stale"]

    def test_abi_version_mismatch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _Future:
            ResidentSweepCore = object
            ResidentSweepCoreU128 = object
            SWEEP_CORE_ABI_VERSION = EXPECTED_SWEEP_CORE_ABI_VERSION + 1
            SWEEP_CORE_FOLD_INT_FIELDS = FOLD_INT_FIELDS
            SWEEP_CORE_FOLD_FLOAT_FIELDS = FOLD_FLOAT_FIELDS

        future = _Future()
        monkeypatch.setattr(sweep_core, "_load_kernel_module", lambda: future)
        with capture_logs() as logs:
            assert _ctx() is None
        assert _fallback_reasons(logs) == ["abi_version_mismatch"]

    def test_big_endian_host_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sweep_core, "_host_byteorder", lambda: "big")
        with capture_logs() as logs:
            assert _ctx() is None
        assert _fallback_reasons(logs) == ["big_endian_host"]

    def test_native_itemsize_mismatch_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sweep_core, "_int_itemsize", lambda: 16)
        with capture_logs() as logs:
            assert _ctx() is None
        assert _fallback_reasons(logs) == ["native_itemsize_mismatch"]

    def test_ts_uncertified_refused(self) -> None:
        bars = _bars()
        dup = [*bars, bars[-1]]  # duplicate trailing timestamp
        with capture_logs() as logs:
            assert _ctx(bars=dup, bar_bits=[0b1111] * len(dup)) is None
        assert _fallback_reasons(logs) == ["ts_uncertified"]

    def test_unbounded_top_k_refused(self) -> None:
        with capture_logs() as logs:
            assert _ctx(top_k=None) is None
        assert _fallback_reasons(logs) == ["unbounded_top_k"]

    def test_width_beyond_u128_refused(self) -> None:
        with capture_logs() as logs:
            assert _ctx(bar_bits=[1 << 128] * 32) is None
        assert _fallback_reasons(logs) == ["width_beyond_u128"]

    def test_u128_width_selected(self) -> None:
        ctx = _ctx(bar_bits=[(1 << 100) | 0b11] * 32, bits=[0, 1, 100], k=2)
        assert ctx is not None
        assert ctx.width == 128

    def test_kernel_disabled_after_latch_is_width_keyed(self) -> None:
        reset_self_check_for_tests(width=64, latched="self_check_mismatch")
        with capture_logs() as logs:
            assert _ctx() is None
        assert _fallback_reasons(logs) == ["kernel_disabled"]
        assert _ctx(bar_bits=[(1 << 100) | 0b11] * 32, bits=[0, 1, 100], k=2) is not None

    def test_stall_budget_exceeded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sweep_core, "_effective_stall_deadline", lambda n, b: 1e-9)
        with capture_logs() as logs:
            assert _ctx() is None
        assert _fallback_reasons(logs) == ["stall_budget_exceeded"]

    def test_stall_guard_unavailable_on_none_and_raise(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sweep_core, "_effective_stall_deadline", lambda n, b: None)
        with capture_logs() as logs:
            assert _ctx() is None
        assert _fallback_reasons(logs) == ["stall_guard_unavailable"]

        def _boom(n: int, b: int) -> float:
            raise RuntimeError("wedged")

        monkeypatch.setattr(sweep_core, "_effective_stall_deadline", _boom)
        with capture_logs() as logs:
            assert _ctx() is None
        assert _fallback_reasons(logs) == ["stall_guard_unavailable"]

    def test_kernel_error_on_construction(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _RaisingCls:
            def __init__(self, *args: object) -> None:
                raise ValueError("bad columns")

        class _Module:
            ResidentSweepCore = _RaisingCls
            ResidentSweepCoreU128 = _RaisingCls
            SWEEP_CORE_ABI_VERSION = EXPECTED_SWEEP_CORE_ABI_VERSION
            SWEEP_CORE_FOLD_INT_FIELDS = FOLD_INT_FIELDS
            SWEEP_CORE_FOLD_FLOAT_FIELDS = FOLD_FLOAT_FIELDS

        module = _Module()
        monkeypatch.setattr(sweep_core, "_load_kernel_module", lambda: module)
        with capture_logs() as logs:
            assert _ctx() is None
        assert _fallback_reasons(logs) == ["kernel_error"]

    def test_bars_length_mismatch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _WrongBars:
            def __init__(self, *args: object) -> None:
                pass

            def n_bars(self) -> int:
                return 7

        class _Module:
            ResidentSweepCore = _WrongBars
            ResidentSweepCoreU128 = _WrongBars
            SWEEP_CORE_ABI_VERSION = EXPECTED_SWEEP_CORE_ABI_VERSION
            SWEEP_CORE_FOLD_INT_FIELDS = FOLD_INT_FIELDS
            SWEEP_CORE_FOLD_FLOAT_FIELDS = FOLD_FLOAT_FIELDS

        module = _Module()
        monkeypatch.setattr(sweep_core, "_load_kernel_module", lambda: module)
        with capture_logs() as logs:
            assert _ctx() is None
        assert _fallback_reasons(logs) == ["bars_length_mismatch"]


class TestWarnOnceMemos:
    """Second hits of the once-per-process memos are silent (log-volume law)."""

    def test_stall_guard_clamp_warns_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(SWEEP_RUST_CORE_ENV, "1")
        monkeypatch.setenv(SWEEP_CORE_ORACLE_NS_ENV, "1.0")
        with capture_logs() as logs:
            resolve_sweep_rust_core_plan()
            resolve_sweep_rust_core_plan()
        assert len([e for e in logs if e["event"] == "sweep_core.stall_guard_clamped"]) == 1

    def test_core_flag_unrecognised_warns_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(SWEEP_RUST_CORE_ENV, "bananas")
        with capture_logs() as logs:
            resolve_sweep_rust_core_plan()
            resolve_sweep_rust_core_plan()
        assert _fallback_reasons(logs) == ["flag_unrecognised"]
