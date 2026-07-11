"""Unit tests for the batch Rust Stage-1A seam (`brutex.engine.rust_stage1a`).

DEC-STAGE1A-RUST-BATCH-001 — 100% line + branch coverage of the flag /
burn-in / autorevert / observability leaf PLUS the `build_bar_bits` dispatch
and the `collect_batch_inputs` prepass glue:

* the flag tristate matrix (truthy / falsy / unset→OFF / junk→OFF + one
  ``flag_unrecognised`` warn + the reset seam);
* wheel-missing / stale-symbol → ``None`` + warn-once (the oracle serves);
* the burn-in whole-series compare — counter decrement, a fabricated FFI
  mismatch ships the ORACLE list + logs ``kernel_autoreverted`` with the G-3
  bounded context + latches, the ``engaged`` INFO fires exactly once;
* dispatch: flag OFF never touches the Rust arm (and a generator input still
  builds correctly); flag ON returns the identical list; a fallback falls
  through to the oracle without re-consuming an iterator;
* error parity (G-6/G-7): a beyond-schedule bar and an out-of-envelope price
  raise IDENTICAL exceptions ON vs OFF;
* the oracle-probe column parity pin: `collect_batch_inputs`' columns equal
  the per-bar values a REAL `Stage1AMatrixBuilder.build` marshals into
  `evaluate_with_levels` / `VwapState.update`, field-for-field, over a
  multi-day + multi-roll + Muhurat-boundary feed;
* Muhurat WARN parity (same event/fields/count from the prepass, incl. the
  documented burn-in double-emission).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
import trading_core
from brutex.data.models import CandleBar
from brutex.engine import rust_stage1a
from brutex.engine.rust_stage1a import (
    SELF_CHECK_BUILDS,
    STAGE1A_RUST_DEFAULT,
    STAGE1A_RUST_ENV,
    Stage1ABatchInputs,
    _first_diff_index,  # pyright: ignore[reportPrivateUsage]
    consume_self_check_sample,
    disable_kernel,
    kernel_disabled,
    note_engaged_once,
    reset_observability_memo_for_tests,
    reset_self_check_for_tests,
    rust_bar_bits_or_none,
    self_check_pending,
    stage1a_rust_enabled,
    warn_fallback_once,
)
from brutex.engine.stage1a import Stage1AMatrixBuilder, build_bar_bits
from structlog.testing import capture_logs

# 09:15:00 IST == 03:45 UTC (the NSE session-open anchor).
_OPEN_UTC_H, _OPEN_UTC_M = 3, 45


@pytest.fixture(autouse=True)
def _fresh_seam_state() -> Iterator[None]:
    """Every test observes a fresh-process burn-in budget + log memo."""
    reset_self_check_for_tests()
    reset_observability_memo_for_tests()
    yield
    reset_self_check_for_tests()
    reset_observability_memo_for_tests()


def _bar(
    ts: datetime, open_: str, high: str, low: str, close: str, *, volume: int = 10
) -> CandleBar:
    return CandleBar(
        timestamp=ts,
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=volume,
        open_interest=None,
    )


def _minute_bar(day: date, minute: int, open_: str, high: str, low: str, close: str) -> CandleBar:
    ts = datetime(day.year, day.month, day.day, _OPEN_UTC_H, _OPEN_UTC_M, tzinfo=UTC) + timedelta(
        minutes=minute
    )
    return _bar(ts, open_, high, low, close)


def _two_day_feed() -> list[CandleBar]:
    """A small deterministic 2-day spot feed (warm anchors on day 2)."""
    day1 = [_minute_bar(date(2024, 1, 2), i, "100", "110", "90", "105") for i in range(3)]
    day2 = [_minute_bar(date(2024, 1, 3), i, "106", "112", "104", "108") for i in range(3)]
    return day1 + day2


def _inputs_for(bars: list[CandleBar]) -> Stage1ABatchInputs:
    return Stage1AMatrixBuilder().collect_batch_inputs(bars)


def _oracle_for(bars: list[CandleBar]) -> list[int]:
    return Stage1AMatrixBuilder().build(bars)


# ────────────────────────────────────────────────────────────────────────
# Flag tristate matrix
# ────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("raw", ["1", "true", "YES", " on "])
def test_flag_truthy_values_enable(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv(STAGE1A_RUST_ENV, raw)
    assert stage1a_rust_enabled() is True


@pytest.mark.parametrize("raw", ["0", "false", "No", " OFF "])
def test_flag_falsy_values_disable(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv(STAGE1A_RUST_ENV, raw)
    assert stage1a_rust_enabled() is False


def test_flag_unset_falls_to_the_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(STAGE1A_RUST_ENV, raising=False)
    assert STAGE1A_RUST_DEFAULT is False  # the inert-ship constant
    assert stage1a_rust_enabled() is False


def test_flag_whitespace_empty_falls_to_the_default_without_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(STAGE1A_RUST_ENV, "   ")
    with capture_logs() as logs:
        assert stage1a_rust_enabled() is False
    assert not [e for e in logs if e.get("event") == "rust_stage1a.fallback"]


def test_flag_junk_fails_safe_off_with_one_warn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(STAGE1A_RUST_ENV, "flase")
    with capture_logs() as logs:
        assert stage1a_rust_enabled() is False
        assert stage1a_rust_enabled() is False  # repeat: memoised, no second warn
    warns = [e for e in logs if e.get("event") == "rust_stage1a.fallback"]
    assert len(warns) == 1
    assert warns[0]["reason"] == "flag_unrecognised"
    assert warns[0]["value"] == "flase"


# ────────────────────────────────────────────────────────────────────────
# Burn-in counter / latch / memo primitives
# ────────────────────────────────────────────────────────────────────────


def test_self_check_budget_counts_down_and_clamps() -> None:
    assert self_check_pending() is True
    for _ in range(SELF_CHECK_BUILDS):
        consume_self_check_sample()
    assert self_check_pending() is False
    consume_self_check_sample()  # clamped — never negative
    assert self_check_pending() is False
    reset_self_check_for_tests(remaining=1)
    assert self_check_pending() is True


def test_disable_kernel_latches_once_and_logs_once() -> None:
    assert kernel_disabled() is False
    with capture_logs() as logs:
        disable_kernel("mask_mismatch", n_bars=3)
        disable_kernel("some_other_reason")  # no-op: already latched
    errors = [e for e in logs if e.get("event") == "rust_stage1a.kernel_autoreverted"]
    assert len(errors) == 1
    assert errors[0]["reason"] == "mask_mismatch"
    assert kernel_disabled() is True


def test_warn_fallback_once_suppresses_repeats() -> None:
    with capture_logs() as logs:
        warn_fallback_once("wheel_missing")
        warn_fallback_once("wheel_missing")
        warn_fallback_once("kernel_refused")
    events = [e for e in logs if e.get("event") == "rust_stage1a.fallback"]
    assert [e["reason"] for e in events] == ["wheel_missing", "kernel_refused"]


def test_note_engaged_once_suppresses_repeats() -> None:
    with capture_logs() as logs:
        note_engaged_once(10)
        note_engaged_once(20)
    events = [e for e in logs if e.get("event") == "rust_stage1a.engaged"]
    assert len(events) == 1
    assert events[0]["n_bars"] == 10


def test_first_diff_index_finds_the_divergence_and_length_skews() -> None:
    assert _first_diff_index([1, 2, 3], [1, 9, 3]) == 1
    assert _first_diff_index([1, 2], [1, 2, 3]) == 2  # equal prefix, size skew
    assert _first_diff_index([], []) == 0


# ────────────────────────────────────────────────────────────────────────
# rust_bar_bits_or_none — the FFI seam
# ────────────────────────────────────────────────────────────────────────


def test_latched_kernel_short_circuits_to_none() -> None:
    disable_kernel("mask_mismatch")
    bars = _two_day_feed()
    with capture_logs() as logs:
        got = rust_bar_bits_or_none(_inputs_for(bars), oracle=lambda: _oracle_for(bars))
    assert got is None
    reasons = [e["reason"] for e in logs if e.get("event") == "rust_stage1a.fallback"]
    assert reasons == ["kernel_disabled"]


def test_stale_wheel_symbol_falls_back_with_warn_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delattr(trading_core, "stage1a_bar_bits_batch")
    bars = _two_day_feed()
    with capture_logs() as logs:
        assert rust_bar_bits_or_none(_inputs_for(bars), oracle=lambda: _oracle_for(bars)) is None
        assert rust_bar_bits_or_none(_inputs_for(bars), oracle=lambda: _oracle_for(bars)) is None
    reasons = [e["reason"] for e in logs if e.get("event") == "rust_stage1a.fallback"]
    assert reasons == ["wheel_missing"]  # warn-once


def test_ffi_value_error_falls_back_as_kernel_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    def _refuse(*_args: object, **_kwargs: object) -> list[int]:
        raise ValueError("boundary refusal")

    monkeypatch.setattr(trading_core, "stage1a_bar_bits_batch", _refuse)
    bars = _two_day_feed()
    with capture_logs() as logs:
        assert rust_bar_bits_or_none(_inputs_for(bars), oracle=lambda: _oracle_for(bars)) is None
    events = [e for e in logs if e.get("event") == "rust_stage1a.fallback"]
    assert events[0]["reason"] == "kernel_refused"
    assert "boundary refusal" in events[0]["error"]


def test_memory_error_propagates_bare(monkeypatch: pytest.MonkeyPatch) -> None:
    def _oom(*_args: object, **_kwargs: object) -> list[int]:
        raise MemoryError

    monkeypatch.setattr(trading_core, "stage1a_bar_bits_batch", _oom)
    bars = _two_day_feed()
    with pytest.raises(MemoryError):
        rust_bar_bits_or_none(_inputs_for(bars), oracle=lambda: _oracle_for(bars))


@pytest.mark.parametrize("hostile_k", [65, 2**62, 2**64 - 1])
def test_hostile_fractal_k_raises_value_error_never_aborts(hostile_k: int) -> None:
    # Refuter RF-B F2: an unbounded fractal_k used to SIGABRT the whole
    # interpreter through the REAL FFI (MarketStructureState::new computes a
    # 2*k + 1 capacity under overflow-checks + panic="abort" — exit 134 for
    # 2**64-1 AND 2**62); validate_structure now caps it at MAX_FRACTAL_K=64
    # so the panic is unreachable — a clean ValueError instead. The
    # pre-existing MarketStructureState PYCLASS keeps the identical abort
    # behaviour for a direct hostile construction (frozen TickVault-shared
    # surface — deliberately untouched, out of this slice's scope).
    inputs = _inputs_for(_two_day_feed())
    with pytest.raises(ValueError, match="fractal_k"):
        trading_core.stage1a_bar_bits_batch(
            inputs.open_paisa,
            inputs.high_paisa,
            inputs.low_paisa,
            inputs.close_paisa,
            inputs.typical_paisa,
            inputs.volume,
            inputs.tod_seconds,
            inputs.pre_open,
            inputs.session_key,
            inputs.session_day,
            inputs.contract_reset,
            inputs.day_index,
            inputs.ema20_paisa,
            inputs.ema200_paisa,
            inputs.day_pivot_levels,
            inputs.day_fib_levels,
            inputs.day_has_pivot,
            inputs.day_has_fib,
            inputs.pivot_bps,
            inputs.pdh_pdl_bps,
            inputs.fib_bps,
            inputs.prior_n,
            inputs.anchor_dependent_mask,
            inputs.fib_dependent_mask,
            inputs.applies_to_1day_mask,
            inputs.apply_1day_mask,
            hostile_k,
        )


def test_burn_in_pass_consumes_budget_and_engages() -> None:
    bars = _two_day_feed()
    remaining_before = SELF_CHECK_BUILDS
    with capture_logs() as logs:
        got = rust_bar_bits_or_none(_inputs_for(bars), oracle=lambda: _oracle_for(bars))
    assert got == _oracle_for(bars)
    engaged = [e for e in logs if e.get("event") == "rust_stage1a.engaged"]
    assert len(engaged) == 1
    # One sample consumed.
    consumed = 0
    while self_check_pending():
        consume_self_check_sample()
        consumed += 1
    assert consumed == remaining_before - 1


def test_burn_in_mismatch_ships_oracle_autoreverts_and_latches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bars = _two_day_feed()
    real = trading_core.stage1a_bar_bits_batch

    def _flipped(*args: Any, **kwargs: Any) -> list[int]:
        out: list[int] = real(*args, **kwargs)
        out[2] ^= 1  # flip one bit — the fabricated kernel bug
        return out

    monkeypatch.setattr(trading_core, "stage1a_bar_bits_batch", _flipped)
    expected = _oracle_for(bars)
    with capture_logs() as logs:
        got = rust_bar_bits_or_none(_inputs_for(bars), oracle=lambda: list(expected))
    assert got == expected  # the ORACLE list ships — never the poisoned one
    reverted = [e for e in logs if e.get("event") == "rust_stage1a.kernel_autoreverted"]
    assert len(reverted) == 1
    assert reverted[0]["reason"] == "mask_mismatch"
    assert reverted[0]["n_bars"] == len(bars)
    assert reverted[0]["first_diff_index"] == 2
    assert reverted[0]["expected_hex"] == hex(expected[2])
    assert reverted[0]["got_hex"] == hex(expected[2] ^ 1)
    assert not [e for e in logs if e.get("event") == "rust_stage1a.engaged"]
    # Latched: the next call short-circuits (no FFI, no oracle).
    with capture_logs() as logs:
        assert (
            rust_bar_bits_or_none(
                _inputs_for(bars), oracle=lambda: pytest.fail("latched call ran the oracle")
            )
            is None
        )
    reasons = [e["reason"] for e in logs if e.get("event") == "rust_stage1a.fallback"]
    assert reasons == ["kernel_disabled"]


def test_burn_in_mismatch_truncated_result_names_the_size_skew(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bars = _two_day_feed()
    real = trading_core.stage1a_bar_bits_batch

    def _truncated(*args: Any, **kwargs: Any) -> list[int]:
        out: list[int] = real(*args, **kwargs)
        return out[:-1]

    monkeypatch.setattr(trading_core, "stage1a_bar_bits_batch", _truncated)
    expected = _oracle_for(bars)
    with capture_logs() as logs:
        got = rust_bar_bits_or_none(_inputs_for(bars), oracle=lambda: list(expected))
    assert got == expected
    reverted = [e for e in logs if e.get("event") == "rust_stage1a.kernel_autoreverted"]
    assert reverted[0]["first_diff_index"] == len(bars) - 1
    assert reverted[0]["got_hex"] is None  # the truncated arm has no value there
    assert reverted[0]["expected_hex"] == hex(expected[-1])


def test_burn_in_mismatch_extended_result_names_the_size_skew(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bars = _two_day_feed()
    real = trading_core.stage1a_bar_bits_batch

    def _extended(*args: Any, **kwargs: Any) -> list[int]:
        out: list[int] = real(*args, **kwargs)
        return [*out, 0]

    monkeypatch.setattr(trading_core, "stage1a_bar_bits_batch", _extended)
    expected = _oracle_for(bars)
    with capture_logs() as logs:
        got = rust_bar_bits_or_none(_inputs_for(bars), oracle=lambda: list(expected))
    assert got == expected
    reverted = [e for e in logs if e.get("event") == "rust_stage1a.kernel_autoreverted"]
    assert reverted[0]["first_diff_index"] == len(expected)
    assert reverted[0]["expected_hex"] is None  # the oracle has no value there
    assert reverted[0]["got_hex"] == hex(0)


def test_post_burn_in_success_never_runs_the_oracle() -> None:
    reset_self_check_for_tests(remaining=0)
    bars = _two_day_feed()
    with capture_logs() as logs:
        got = rust_bar_bits_or_none(
            _inputs_for(bars), oracle=lambda: pytest.fail("steady state ran the oracle")
        )
    assert got == _oracle_for(bars)
    assert [e for e in logs if e.get("event") == "rust_stage1a.engaged"]


# ────────────────────────────────────────────────────────────────────────
# build_bar_bits dispatch
# ────────────────────────────────────────────────────────────────────────


def test_flag_off_never_touches_the_rust_arm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(STAGE1A_RUST_ENV, raising=False)
    monkeypatch.setattr(
        rust_stage1a,
        "rust_bar_bits_or_none",
        lambda *a, **k: pytest.fail("OFF arm reached the Rust seam"),
    )
    bars = _two_day_feed()
    # A one-shot generator input: the OFF arm streams it (never materializes
    # under the flag branch) and still yields the identical matrix.
    got = build_bar_bits(bar for bar in bars)
    assert got == _oracle_for(bars)


def test_flag_on_returns_the_identical_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(STAGE1A_RUST_ENV, "1")
    bars = _two_day_feed()
    with capture_logs() as logs:
        got = build_bar_bits(bars)
    assert got == _oracle_for(bars)
    assert [e for e in logs if e.get("event") == "rust_stage1a.engaged"]


def test_flag_on_fallback_falls_through_without_reconsuming_the_iterator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(STAGE1A_RUST_ENV, "1")
    disable_kernel("mask_mismatch")  # force the named fallback path
    bars = _two_day_feed()
    got = build_bar_bits(bar for bar in bars)  # a one-shot generator
    assert got == _oracle_for(bars)


# ────────────────────────────────────────────────────────────────────────
# Error parity (G-6 / G-7)
# ────────────────────────────────────────────────────────────────────────


def _arm_error(
    monkeypatch: pytest.MonkeyPatch, bars: list[CandleBar], *, flag: str | None, **kwargs: Any
) -> BaseException:
    if flag is None:
        monkeypatch.delenv(STAGE1A_RUST_ENV, raising=False)
    else:
        monkeypatch.setenv(STAGE1A_RUST_ENV, flag)
    with pytest.raises((ValueError, OverflowError)) as excinfo:
        build_bar_bits(bars, **kwargs)
    return excinfo.value


def test_beyond_schedule_bar_raises_identical_message_both_arms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # G-7: the ONE copied f-string in the prepass glue is message-pinned.
    bars = _two_day_feed()
    schedule = (date(2024, 1, 2).toordinal(),)  # day 2 is beyond the last expiry
    off = _arm_error(monkeypatch, bars, flag=None, roll_schedule=schedule)
    reset_self_check_for_tests()
    on = _arm_error(monkeypatch, bars, flag="1", roll_schedule=schedule)
    assert type(off) is type(on) is ValueError
    assert str(off) == str(on)
    assert "beyond the last roll_schedule expiry" in str(off)


def test_out_of_envelope_price_raises_identical_error_both_arms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # G-6: ₹2·10¹² = 2·10¹⁴ paisa exceeds the kernel's MAX_BAR_PAISA — the ON
    # arm's boundary refuses, falls back, and the ORACLE raises the same
    # per-bar evaluate ValueError the OFF arm raises.
    bars = [
        _minute_bar(date(2024, 1, 2), 0, "100", "110", "90", "105"),
        _minute_bar(
            date(2024, 1, 2), 1, "2000000000000", "2000000000000", "2000000000000", "2000000000000"
        ),
    ]
    off = _arm_error(monkeypatch, bars, flag=None)
    reset_self_check_for_tests()
    reset_observability_memo_for_tests()
    with capture_logs() as logs:
        on = _arm_error(monkeypatch, bars, flag="1")
    assert type(off) is type(on)
    assert str(off) == str(on)
    assert "overflow-safe range" in str(off)
    # The ON arm went through the named kernel_refused fallback first.
    reasons = [e["reason"] for e in logs if e.get("event") == "rust_stage1a.fallback"]
    assert reasons == ["kernel_refused"]


# ────────────────────────────────────────────────────────────────────────
# Oracle-probe column parity (the prepass-glue pin)
# ────────────────────────────────────────────────────────────────────────


def _probe_feed() -> tuple[list[CandleBar], tuple[int, ...]]:
    """Multi-day + multi-roll + Muhurat-boundary futures feed.

    Days: 2020-11-12/13 owned by expiry 2020-11-13; 2020-11-14 (the real
    Diwali Muhurat session) + 11-17 + 11-18 owned by 2020-12-31 — so the roll
    fires ON the Muhurat day and the Muhurat anchor skip fires when 11-14
    completes into 11-17.
    """
    days = [
        date(2020, 11, 12),
        date(2020, 11, 13),
        date(2020, 11, 14),
        date(2020, 11, 17),
        date(2020, 11, 18),
    ]
    bars: list[CandleBar] = []
    for d_idx, d in enumerate(days):
        base = 100 + 10 * d_idx
        for m in range(3):
            o = f"{base + m}.00"
            h = f"{base + m + 2}.50"
            lo = f"{base + m - 2}.25"
            c = f"{base + m + 1}.75"
            bars.append(_minute_bar(d, m, o, h, lo, c))
    schedule = (date(2020, 11, 13).toordinal(), date(2020, 12, 31).toordinal())
    return bars, schedule


def test_collect_batch_inputs_columns_equal_the_real_push_marshal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bars, schedule = _probe_feed()

    eval_calls: list[tuple[Any, ...]] = []
    real_eval = trading_core.evaluate_with_levels

    def _recording_eval(*args: Any) -> int:
        eval_calls.append(args)
        result: int = real_eval(*args)
        return result

    vwap_calls: list[tuple[Any, ...]] = []
    constructions: list[int] = [0]
    real_vwap_cls = trading_core.VwapState

    class _RecordingVwap:
        def __init__(self) -> None:
            constructions[0] += 1
            self._inner = real_vwap_cls()

        def update(self, *args: Any) -> int | None:
            vwap_calls.append(args)
            out: int | None = self._inner.update(*args)
            return out

    monkeypatch.setattr(trading_core, "evaluate_with_levels", _recording_eval)
    monkeypatch.setattr(trading_core, "VwapState", _RecordingVwap)

    # The REAL builder walk records the per-bar marshalled values…
    Stage1AMatrixBuilder(roll_schedule=schedule).build(bars)
    push_constructions = constructions[0]
    assert len(eval_calls) == len(bars)
    assert len(vwap_calls) == len(bars)

    # …and the prepass columns must equal them field-for-field.
    constructions[0] = 0
    inputs = Stage1AMatrixBuilder(roll_schedule=schedule).collect_batch_inputs(bars)
    assert push_constructions == 1 + sum(inputs.contract_reset)
    assert constructions[0] == push_constructions  # same reset cursor

    for i in range(len(bars)):
        args = eval_calls[i]
        assert args[0] == inputs.tod_seconds[i]
        assert args[1] == inputs.open_paisa[i]
        assert args[2] == inputs.high_paisa[i]
        assert args[3] == inputs.low_paisa[i]
        assert args[4] == inputs.close_paisa[i]
        assert args[5] == inputs.ema20_paisa[i]
        assert args[6] == inputs.ema200_paisa[i]
        d = inputs.day_index[i]
        assert args[8] == inputs.day_pivot_levels[12 * d : 12 * (d + 1)]
        assert args[9] == inputs.day_fib_levels[15 * d : 15 * (d + 1)]
        assert args[13] == inputs.pivot_bps
        assert args[14] == inputs.pdh_pdl_bps
        assert args[15] == inputs.fib_bps
        assert args[16] == inputs.prior_n

        typical, volume, pre_open, session_key, session_day, tod = vwap_calls[i]
        assert typical == inputs.typical_paisa[i]
        assert volume == inputs.volume[i]
        assert pre_open == inputs.pre_open[i]
        assert session_key == inputs.session_key[i]
        assert session_day == inputs.session_day[i]
        assert tod == inputs.tod_seconds[i]

    # Day-table shape: one row per distinct builder day roll, all consumed.
    n_days = len(inputs.day_has_pivot)
    assert inputs.day_index[0] == 0
    assert inputs.day_index[-1] == n_days - 1
    assert len(inputs.day_pivot_levels) == 12 * n_days
    assert len(inputs.day_fib_levels) == 15 * n_days
    assert len(inputs.day_has_fib) == n_days
    # The roll fires exactly once, on the Muhurat day's first bar (bar 6).
    assert [i for i, r in enumerate(inputs.contract_reset) if r] == [6]


def test_muhurat_warn_parity_between_prepass_and_builder() -> None:
    bars, schedule = _probe_feed()
    with capture_logs() as logs:
        Stage1AMatrixBuilder(roll_schedule=schedule).build(bars)
    build_warns = [e for e in logs if e.get("event") == "stage1a.muhurat_prev_day_anchor_skip"]
    with capture_logs() as logs:
        Stage1AMatrixBuilder(roll_schedule=schedule).collect_batch_inputs(bars)
    prepass_warns = [e for e in logs if e.get("event") == "stage1a.muhurat_prev_day_anchor_skip"]
    assert len(build_warns) == len(prepass_warns) == 1
    assert build_warns[0]["completed_day"] == prepass_warns[0]["completed_day"] == "2020-11-14"
    assert build_warns[0]["new_day"] == prepass_warns[0]["new_day"] == "2020-11-17"


def test_muhurat_warn_double_emission_during_burn_in_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The documented burn-in double-emission: the flag-ON dispatch runs the
    # prepass (1 warn) AND — while burn-in samples are pending — the oracle
    # build (1 more warn). Bounded by SELF_CHECK_BUILDS, log-volume only.
    monkeypatch.setenv(STAGE1A_RUST_ENV, "1")
    bars, schedule = _probe_feed()
    with capture_logs() as logs:
        on = build_bar_bits(bars, roll_schedule=schedule)
    warns = [e for e in logs if e.get("event") == "stage1a.muhurat_prev_day_anchor_skip"]
    assert len(warns) == 2  # prepass + burn-in oracle
    monkeypatch.delenv(STAGE1A_RUST_ENV, raising=False)
    assert on == build_bar_bits(bars, roll_schedule=schedule)


# ────────────────────────────────────────────────────────────────────────
# Stage1ABatchInputs carrier
# ────────────────────────────────────────────────────────────────────────


def test_batch_inputs_carrier_holds_the_positive_masks() -> None:
    from brutex.engine.stage1a import ANCHOR_DEPENDENT_MASK, FIB_DEPENDENT_MASK
    from brutex.vocabulary import bits as B

    inputs = _inputs_for(_two_day_feed())
    assert inputs.anchor_dependent_mask == ANCHOR_DEPENDENT_MASK > 0
    assert inputs.fib_dependent_mask == FIB_DEPENDENT_MASK > 0
    assert inputs.applies_to_1day_mask == B.APPLIES_TO_1DAY_MASK > 0
    assert inputs.fractal_k == 2
    assert inputs.prior_n == 3
    assert len(inputs.open_paisa) == 6
