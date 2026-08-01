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
  `evaluate_with_levels`, field-for-field, over a
  multi-day + multi-roll + Muhurat-boundary feed;
* Muhurat WARN parity (same event/fields/count from the prepass, incl. the
  documented burn-in double-emission).
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
import trading_core
from brutex.config.rust_strict import RUST_STRICT_ENV, RustFallbackRefused
from brutex.data.models import CandleBar
from brutex.engine import rust_stage1a
from brutex.engine.rust_stage1a import (
    _INPUT_REFUSED_MARKER,  # pyright: ignore[reportPrivateUsage]
    SELF_CHECK_BUILDS,
    STAGE1A_RUST_DEFAULT,
    STAGE1A_RUST_ENV,
    Stage1ABatchInputs,
    _first_diff_index,  # pyright: ignore[reportPrivateUsage]
    _kernel_degrade_reason,  # pyright: ignore[reportPrivateUsage]
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
from brutex.strategies import combo_rust
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


@pytest.fixture
def strict_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the degrade-to-oracle contract in the mode that still HAS one.

    Under the shipped strict default the CLASS-A/B reasons this seam emits
    (``flag_unrecognised`` / ``wheel_missing`` / ``kernel_disabled``) are a
    ``RustFallbackRefused``, not a fall-back — Rust is absent, mis-configured
    or latched, and DEC-RUST-STRICT-NO-FALLBACK-001 forbids Python serving it.
    That refusal is pinned POSITIVELY in
    :class:`TestStrictModeRefusesTheFallback`; the tests below pin the *other*
    half of the contract — that with the documented kill switch thrown, the
    historic oracle path is byte-identical — so both halves stay covered.

    (``kernel_refused`` joined that set once the crate learned to MARK its
    input-envelope refusals: the CLASS-C half now reports the distinct,
    strict-EXEMPT ``kernel_input_refused``, so the UNMARKED remainder is
    CLASS A/B and refuses like its siblings. Tests that drive an out-of-envelope
    INPUT still need no kill switch — see ``TestStrictModeRefusesTheFallback``.)

    Uses the SHARED ``monkeypatch`` — never a module-scoped
    ``MonkeyPatch.context()`` — so set/unset ordering with the sibling
    ``STAGE1A_RUST_ENV`` pins in this file stays deterministic.
    """
    monkeypatch.setenv(RUST_STRICT_ENV, "0")


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


def test_flag_unset_falls_to_the_default_on(monkeypatch: pytest.MonkeyPatch) -> None:
    # DEC-RUST-DEFAULT-ON-WAVE-001: the kernel is ON by default.
    monkeypatch.delenv(STAGE1A_RUST_ENV, raising=False)
    assert STAGE1A_RUST_DEFAULT is True  # the default-ON constant
    assert stage1a_rust_enabled() is True


def test_flag_whitespace_empty_falls_to_the_default_without_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(STAGE1A_RUST_ENV, "   ")
    with capture_logs() as logs:
        assert stage1a_rust_enabled() is STAGE1A_RUST_DEFAULT
    assert not [e for e in logs if e.get("event") == "rust_stage1a.fallback"]


def test_flag_junk_fails_safe_off_with_one_warn(
    monkeypatch: pytest.MonkeyPatch, strict_off: None
) -> None:
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


def test_warn_fallback_once_suppresses_repeats(strict_off: None) -> None:
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


def test_latched_kernel_short_circuits_to_none(strict_off: None) -> None:
    disable_kernel("mask_mismatch")
    bars = _two_day_feed()
    with capture_logs() as logs:
        got = rust_bar_bits_or_none(_inputs_for(bars), oracle=lambda: _oracle_for(bars))
    assert got is None
    reasons = [e["reason"] for e in logs if e.get("event") == "rust_stage1a.fallback"]
    assert reasons == ["kernel_disabled"]


def test_stale_wheel_symbol_falls_back_with_warn_once(
    monkeypatch: pytest.MonkeyPatch, strict_off: None
) -> None:
    monkeypatch.delattr(trading_core, "stage1a_bar_bits_batch")
    bars = _two_day_feed()
    with capture_logs() as logs:
        assert rust_bar_bits_or_none(_inputs_for(bars), oracle=lambda: _oracle_for(bars)) is None
        assert rust_bar_bits_or_none(_inputs_for(bars), oracle=lambda: _oracle_for(bars)) is None
    reasons = [e["reason"] for e in logs if e.get("event") == "rust_stage1a.fallback"]
    assert reasons == ["wheel_missing"]  # warn-once


def test_ffi_value_error_falls_back_as_kernel_refused(
    monkeypatch: pytest.MonkeyPatch, strict_off: None
) -> None:
    # UNMARKED → CLASS A/B ``kernel_refused``; strict mode refuses it (pinned
    # in ``TestStrictModeRefusesTheFallback``), so the degrade half needs the
    # documented kill switch.
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
    # Positional order mirrors the retention FFI signature — the five VWAP
    # feed columns re-inserted at their pre-wipe positions
    # (DEC-VOCAB-VWAP-RETAIN-001, MIGRATION.md #2206); hostile_k rides the
    # fractal_k slot exactly where the production call passes it.
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
            inputs.swing_bps,
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
    monkeypatch: pytest.MonkeyPatch, strict_off: None
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
    # RETARGETED (DEC-RUST-DEFAULT-ON-WAVE-001): the never-touches-Rust pin
    # now rides the explicit "0" kill-switch arm (unset is default-ON).
    monkeypatch.setenv(STAGE1A_RUST_ENV, "0")
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
    monkeypatch: pytest.MonkeyPatch, strict_off: None
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
        monkeypatch.setenv(
            STAGE1A_RUST_ENV, "0"
        )  # OFF arm = the Python oracle (default flipped ON)
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
    #
    # Runs under the SHIPPED strict default (no ``strict_off``): this is the
    # end-to-end proof of the CLASS-C exemption over the REAL crate — the
    # marked refusal reports ``kernel_input_refused``, which strict does NOT
    # refuse, so the oracle's precise diagnosis survives instead of being
    # replaced by a generic ``RustFallbackRefused``.
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
    # BOTH arms carry the crate's CLASS-C marker (the OFF arm reaches the same
    # guard per bar through ``evaluate_with_levels``) — that identity is what
    # makes the exemption provable rather than guessed.
    assert _INPUT_REFUSED_MARKER in str(off)
    # The ON arm went through the named kernel_input_refused fallback first.
    reasons = [e["reason"] for e in logs if e.get("event") == "rust_stage1a.fallback"]
    assert reasons == ["kernel_input_refused"]


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

    constructions: list[int] = [0]
    real_ms_cls = trading_core.MarketStructureState

    class _RecordingStructure:
        def __init__(self) -> None:
            constructions[0] += 1
            self._inner = real_ms_cls()

        def update(self, *args: Any) -> tuple[bool, bool, bool, bool]:
            out: tuple[bool, bool, bool, bool] = self._inner.update(*args)
            return out

        # DEC-FIB-TEMPLATE-EXPRESS-001 — push() reads the confirmed-swing
        # getters strictly POST-update for bits 72/73; delegate to the real
        # pyclass so the spy stays behaviour-transparent.
        @property
        def last_swing_high(self) -> int | None:
            value: int | None = self._inner.last_swing_high
            return value

        @property
        def last_swing_low(self) -> int | None:
            value: int | None = self._inner.last_swing_low
            return value

    monkeypatch.setattr(trading_core, "evaluate_with_levels", _recording_eval)
    monkeypatch.setattr(trading_core, "MarketStructureState", _RecordingStructure)

    # The REAL builder walk records the per-bar marshalled values… (the
    # constructions counter rides MarketStructureState; VwapState is real —
    # RETAINED per DEC-VOCAB-VWAP-RETAIN-001, MIGRATION.md #2206.)
    Stage1AMatrixBuilder(roll_schedule=schedule).build(bars)
    push_constructions = constructions[0]
    assert len(eval_calls) == len(bars)

    # …and the prepass columns must equal them field-for-field (the retention
    # 28-arg ABI, DEC-VOCAB-VWAP-RETAIN-001 #2206: the LIVE vwap arg sits at
    # index 7 again, pivot_levels at 8; the express swing pair + band ride
    # appended-last, DEC-FIB-TEMPLATE-EXPRESS-001).
    constructions[0] = 0
    inputs = Stage1AMatrixBuilder(roll_schedule=schedule).collect_batch_inputs(bars)
    assert push_constructions == 1 + sum(inputs.contract_reset)

    # Replay teeth for the five re-inserted VWAP feed columns: the push arm's
    # marshalled vwap (args[7]) must equal a REAL trading_core.VwapState
    # replay over the prepass columns (recreated at every contract reset —
    # the batch kernel's own reset law), binding the columns to the marshal.
    vwap_state = trading_core.VwapState()
    for i in range(len(bars)):
        if inputs.contract_reset[i]:
            vwap_state = trading_core.VwapState()
        expected_vwap = vwap_state.update(
            inputs.typical_paisa[i],
            inputs.volume[i],
            inputs.pre_open[i],
            inputs.session_key[i],
            inputs.session_day[i],
            inputs.tod_seconds[i],
        )
        args = eval_calls[i]
        assert args[0] == inputs.tod_seconds[i]
        assert args[1] == inputs.open_paisa[i]
        assert args[2] == inputs.high_paisa[i]
        assert args[3] == inputs.low_paisa[i]
        assert args[4] == inputs.close_paisa[i]
        assert args[5] == inputs.ema20_paisa[i]
        assert args[6] == inputs.ema200_paisa[i]
        assert args[7] == expected_vwap
        d = inputs.day_index[i]
        assert args[8] == inputs.day_pivot_levels[12 * d : 12 * (d + 1)]
        assert args[9] == inputs.day_fib_levels[19 * d : 19 * (d + 1)]
        assert args[13] == inputs.pivot_bps
        assert args[14] == inputs.pdh_pdl_bps
        assert args[15] == inputs.fib_bps
        assert args[16] == inputs.prior_n

    # Day-table shape: one row per distinct builder day roll, all consumed.
    n_days = len(inputs.day_has_pivot)
    assert inputs.day_index[0] == 0
    assert inputs.day_index[-1] == n_days - 1
    assert len(inputs.day_pivot_levels) == 12 * n_days
    assert len(inputs.day_fib_levels) == 19 * n_days
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
    monkeypatch.setenv(STAGE1A_RUST_ENV, "0")  # kill switch → Python oracle recompute
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


# ════════════════════════════════════════════════════════════════════════════
# Mutation-gate pins — refuter RF-α round 4, the 10 mutmut survivors
# (each pin below kills a demonstrated gate survivor; the weekly mutation.yml
# python loop is ZERO-survivor by law, so every one of these is load-bearing)
# ════════════════════════════════════════════════════════════════════════════


def test_env_var_name_is_the_documented_literal(monkeypatch: pytest.MonkeyPatch) -> None:
    # Survivor 3: every other test reaches the env through the CONSTANT
    # (self-referential), so a typo'd constant would silently orphan the
    # documented operator lever. Pin the literal, then drive it end-to-end
    # via the literal name.
    assert STAGE1A_RUST_ENV == "BRUTEX_SWEEP_RUST_STAGE1A"
    monkeypatch.setenv("BRUTEX_SWEEP_RUST_STAGE1A", "1")  # the LITERAL name
    assert stage1a_rust_enabled() is True
    monkeypatch.setenv("BRUTEX_SWEEP_RUST_STAGE1A", "0")
    assert stage1a_rust_enabled() is False


def test_unset_env_serves_the_default_with_no_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Survivor 18 (`os.environ.get(..., "")` default junked): the unset arm
    # must serve STAGE1A_RUST_DEFAULT with ZERO fallback events — and must
    # KEEP serving the default once it flips ON (the one-constant
    # DEC-STAGE1A-RUST-DEFAULT-ON-001 lever; a junked get-default would turn
    # unset into the flag_unrecognised path and pin the arm to False).
    monkeypatch.delenv(STAGE1A_RUST_ENV, raising=False)
    with capture_logs() as logs:
        assert stage1a_rust_enabled() is STAGE1A_RUST_DEFAULT
    assert not [e for e in logs if e.get("event") == "rust_stage1a.fallback"]
    monkeypatch.setattr(rust_stage1a, "STAGE1A_RUST_DEFAULT", True)
    with capture_logs() as logs:
        assert rust_stage1a.stage1a_rust_enabled() is True
    assert not [e for e in logs if e.get("event") == "rust_stage1a.fallback"]


def test_burn_in_budget_is_exactly_four_by_behavior() -> None:
    # Survivor 7 (SELF_CHECK_BUILDS 4 → 5): the ledgered burn-in budget is
    # pinned by the LITERAL 4 and by counting a fresh process's budget to
    # exhaustion through the public API.
    assert SELF_CHECK_BUILDS == 4
    reset_self_check_for_tests()  # the fresh-process view
    for _ in range(4):
        assert self_check_pending() is True
        consume_self_check_sample()
    assert self_check_pending() is False


def test_consume_clamps_at_zero_never_negative() -> None:
    # Survivor 28 (`> 0` → `>= 0`): at remaining == 0 a racing extra consume
    # must be a NO-OP (the docstring's "clamped at zero" law) — unobservable
    # through self_check_pending alone, so the cell is read directly.
    reset_self_check_for_tests(remaining=0)
    consume_self_check_sample()
    assert rust_stage1a._SELF_CHECK_REMAINING[0] == 0  # pyright: ignore[reportPrivateUsage]


def test_logger_name_is_the_module_path_literal() -> None:
    # Survivor 1: capture_logs is name-blind and the prod processor chain
    # renders no logger name — pin the factory args (the house precedent:
    # test_greeks_enrich_pass_dashboard_identity.py).
    assert getattr(rust_stage1a._LOG, "_logger_factory_args", None) == (  # pyright: ignore[reportPrivateUsage]
        "brutex.engine.rust_stage1a",
    )


def test_engaged_memo_key_is_the_namespaced_literal() -> None:
    # Survivors 10/11 (_ENGAGED_KEY junked / None): the engaged-once key is a
    # str namespaced apart from every warn reason; a None key would silently
    # break the memo-set semantics.
    assert rust_stage1a._ENGAGED_KEY == "__engaged__"  # pyright: ignore[reportPrivateUsage]
    reset_observability_memo_for_tests()
    with capture_logs():
        note_engaged_once(5)
    memo = rust_stage1a._OBSERVABILITY_MEMO  # pyright: ignore[reportPrivateUsage]
    assert "__engaged__" in memo


def test_batch_inputs_carrier_is_frozen_with_slots() -> None:
    # Survivors 15/16 (@dataclass(frozen=..., slots=...) flags flipped): the
    # carrier is IMMUTABLE (a consumer can never poke a column between the
    # burn-in compare and the FFI call) and __dict__-free (slots).
    inputs = _inputs_for(_two_day_feed())
    with pytest.raises(dataclasses.FrozenInstanceError):
        inputs.prior_n = 9  # type: ignore[misc]  # pyright: ignore[reportAttributeAccessIssue]
    assert not hasattr(inputs, "__dict__")


# ────────────────────────────────────────────────────────────────────────
# Strict mode — DEC-RUST-STRICT-NO-FALLBACK-001
# ────────────────────────────────────────────────────────────────────────


class TestStrictModeRefusesTheFallback:
    """The POSITIVE half of the contract the ``strict_off`` fixture defers.

    Under the shipped default (``BRUTEX_RUST_STRICT`` unset ⇒ strict ON) the
    CLASS-A/B reasons this seam emits are a hard :class:`RustFallbackRefused`,
    never a quiet Python re-run. The ``strict_off`` tests above pin the other
    half — that with the documented kill switch thrown the historic oracle path
    is byte-identical — so between the two every reason is covered in BOTH
    modes.

    The one ``except ValueError`` around the batch call used to bundle a
    kernel refusal (CLASS A/B) with an out-of-envelope price (CLASS C), so
    ``kernel_refused`` was HELD in ``_STRICT_UNRESOLVED_REASONS``. The crate
    now MARKS its input-envelope refusals, so the split is real:
    ``kernel_input_refused`` is the ruled CLASS-C exemption (degrade — the
    oracle raises the IDENTICAL error) and the UNMARKED ``kernel_refused``
    remainder REFUSES. Both halves are pinned below, so neither the exemption
    nor the refusal can be silently flipped, and the HOLD set is now EMPTY.
    """

    def test_wheel_missing_refuses_under_the_shipped_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(RUST_STRICT_ENV, raising=False)  # the shipped default
        monkeypatch.delattr(trading_core, "stage1a_bar_bits_batch")
        bars = _two_day_feed()
        with pytest.raises(RustFallbackRefused, match="wheel_missing"):
            rust_bar_bits_or_none(_inputs_for(bars), oracle=lambda: _oracle_for(bars))

    def test_refusal_names_this_facade(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(RUST_STRICT_ENV, raising=False)
        monkeypatch.delattr(trading_core, "stage1a_bar_bits_batch")
        bars = _two_day_feed()
        with pytest.raises(RustFallbackRefused, match="brutex.engine.rust_stage1a"):
            rust_bar_bits_or_none(_inputs_for(bars), oracle=lambda: _oracle_for(bars))

    @pytest.mark.parametrize("reason", ["wheel_missing", "flag_unrecognised", "kernel_disabled"])
    def test_every_class_ab_reason_refuses(
        self, monkeypatch: pytest.MonkeyPatch, reason: str
    ) -> None:
        monkeypatch.delenv(RUST_STRICT_ENV, raising=False)
        with pytest.raises(RustFallbackRefused, match=reason):
            warn_fallback_once(reason)

    def test_nothing_is_held_out_of_the_taxonomy(self) -> None:
        # The GOAL of the crate-side split: no reason is classified-by-refusal
        # any more, so strict mode's coverage of this facade is complete.
        assert frozenset() == rust_stage1a._STRICT_UNRESOLVED_REASONS  # pyright: ignore[reportPrivateUsage]

    def test_class_c_input_refusal_is_exempt_and_only_logs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The ruled CLASS-C exemption: the crate declined an out-of-envelope
        # INPUT, the oracle raises the identical error, so strict degrades.
        monkeypatch.delenv(RUST_STRICT_ENV, raising=False)
        with capture_logs() as logs:
            warn_fallback_once("kernel_input_refused")
        events = [e for e in logs if e.get("event") == "rust_stage1a.fallback"]
        assert [e["reason"] for e in events] == ["kernel_input_refused"]

    def test_unmarked_kernel_refused_now_refuses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The other half of the split: the UNMARKED remainder is a genuine
        # kernel/marshalling fault and must NOT be papered over by the oracle.
        monkeypatch.delenv(RUST_STRICT_ENV, raising=False)
        with pytest.raises(RustFallbackRefused, match="kernel_refused"):
            warn_fallback_once("kernel_refused")

    def test_marked_ffi_error_degrades_under_strict(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # End-to-end through the real seam: a MARKED boundary ValueError is
        # classified CLASS C, so strict returns None (the oracle serves).
        monkeypatch.delenv(RUST_STRICT_ENV, raising=False)

        def _refuse(*_args: object, **_kwargs: object) -> list[int]:
            raise ValueError(
                f"bar price/level magnitude 999 out of the overflow-safe range "
                f"[-1, 1] paisa — {_INPUT_REFUSED_MARKER}"
            )

        monkeypatch.setattr(trading_core, "stage1a_bar_bits_batch", _refuse)
        bars = _two_day_feed()
        with capture_logs() as logs:
            assert (
                rust_bar_bits_or_none(_inputs_for(bars), oracle=lambda: _oracle_for(bars)) is None
            )
        events = [e for e in logs if e.get("event") == "rust_stage1a.fallback"]
        assert [e["reason"] for e in events] == ["kernel_input_refused"]

    def test_unmarked_ffi_error_refuses_under_strict(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The mirror: an UNMARKED boundary ValueError (a structural refusal —
        # e.g. a column-length skew) is CLASS A/B and hard-refuses.
        monkeypatch.delenv(RUST_STRICT_ENV, raising=False)

        def _refuse(*_args: object, **_kwargs: object) -> list[int]:
            raise ValueError("high_paisa length 0 != open_paisa length 1")

        monkeypatch.setattr(trading_core, "stage1a_bar_bits_batch", _refuse)
        bars = _two_day_feed()
        with pytest.raises(RustFallbackRefused, match="kernel_refused"):
            rust_bar_bits_or_none(_inputs_for(bars), oracle=lambda: _oracle_for(bars))

    @pytest.mark.parametrize(
        ("detail", "expected"),
        [
            (f"whatever — {_INPUT_REFUSED_MARKER}", "kernel_input_refused"),
            ("stage1a_batch: day_index 3 beyond the validated day table", "kernel_refused"),
        ],
    )
    def test_degrade_reason_classifier_reads_the_crate_marker(
        self, detail: str, expected: str
    ) -> None:
        assert _kernel_degrade_reason(ValueError(detail)) == expected

    def test_marker_literal_matches_the_real_crate(self) -> None:
        # Cross-language drift pin against the REAL wheel: an out-of-envelope
        # bar price must carry the exact fragment this facade classifies on.
        bars = _two_day_feed()
        poisoned = dataclasses.replace(
            _inputs_for(bars),
            open_paisa=[10**15, *_inputs_for(bars).open_paisa[1:]],  # past MAX_BAR_PAISA
        )
        with pytest.raises(ValueError, match="overflow-safe range") as excinfo:
            trading_core.stage1a_bar_bits_batch(
                poisoned.open_paisa,
                poisoned.high_paisa,
                poisoned.low_paisa,
                poisoned.close_paisa,
                poisoned.typical_paisa,
                poisoned.volume,
                poisoned.tod_seconds,
                poisoned.pre_open,
                poisoned.session_key,
                poisoned.session_day,
                poisoned.contract_reset,
                poisoned.day_index,
                poisoned.ema20_paisa,
                poisoned.ema200_paisa,
                poisoned.day_pivot_levels,
                poisoned.day_fib_levels,
                poisoned.day_has_pivot,
                poisoned.day_has_fib,
                poisoned.pivot_bps,
                poisoned.pdh_pdl_bps,
                poisoned.fib_bps,
                poisoned.swing_bps,
                poisoned.prior_n,
                poisoned.anchor_dependent_mask,
                poisoned.fib_dependent_mask,
                poisoned.applies_to_1day_mask,
                poisoned.apply_1day_mask,
            )
        assert _INPUT_REFUSED_MARKER in str(excinfo.value)

    def test_marker_is_the_shared_cross_crate_literal(self) -> None:
        # ONE fragment for both crates — a rename must land on both sides.
        assert _INPUT_REFUSED_MARKER == combo_rust._INPUT_REFUSED_MARKER  # pyright: ignore[reportPrivateUsage]

    def test_junk_strict_env_keeps_strict_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Inverted polarity: only an explicit-falsy value disarms strict; junk
        # must NOT silently re-open the Python path.
        monkeypatch.setenv(RUST_STRICT_ENV, "garbage")
        with pytest.raises(RustFallbackRefused, match="wheel_missing"):
            warn_fallback_once("wheel_missing")
