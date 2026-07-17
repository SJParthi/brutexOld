"""Tests for the ``bad_tick_rust`` frame-screen routing facade (SLICE T24 PR-2).

DEC-DATA-RUST-INTEGRITY-DETECTORS-001. Yields 100% line + branch on
``brutex/data/bad_tick_rust.py`` ALONE (the discovery-gate contract): the
flag matrix, the latch + reset seam, the kernel-unavailable latch, the
ts-dtype / null / abstain / validate-refusal / positivity-stop degrade
arms, the narrow degrade set + the escape-set re-raise + the panic belt,
the burn-in mismatch autorevert (ships the ORACLE frame) + the clean
burn-in countdown + the steady state, the warn-field materialization
parity (log-sequence equal to the oracle), and the engaged/warn-once
memos. The fake kernel is the shared law-faithful
``tests/fakes/feed_scan_crate.py`` transcription — the real-wheel
differentials live in the seam/property suites.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import brutex.data.bad_tick_rust as btr
import polars as pl
import pytest
from brutex.data.bad_tick import BAD_TICK_MAX_RATIO, _screen_frame_sequential_python
from brutex.data.session import ist_offset_duration
from structlog.testing import capture_logs
from structlog.typing import EventDict

from tests.fakes.feed_scan_crate import fake_scan_bars

#: 09:15 IST (03:45 UTC) on a regular session day — the per-day bar anchor.
_OPEN_UTC = datetime(2024, 1, 2, 3, 45, tzinfo=UTC)


def _frame(
    closes: list[float],
    *,
    days: list[int] | None = None,
    dtype: pl.DataType | None = None,
) -> pl.DataFrame:
    """A sorted candidate-bearing-shaped frame (one row per minute)."""
    day_of = days if days is not None else [0] * len(closes)
    ts = [_OPEN_UTC + timedelta(days=d, minutes=i) for i, d in enumerate(day_of)]
    schema_dtype = dtype if dtype is not None else pl.Datetime("us", "UTC")
    return pl.DataFrame(
        {"ts_utc": ts, "close": closes},
        schema={"ts_utc": schema_dtype, "close": pl.Float64},
    )


#: A frame with one genuine x10 spike at row 2 (dropped by the screen).
_SPIKED = [100.0, 100.5, 1005.0, 101.0]


def _ist_expr() -> pl.Expr:
    return (pl.col("ts_utc") + ist_offset_duration()).dt.date()


def _oracle(frame: pl.DataFrame) -> pl.DataFrame:
    return _screen_frame_sequential_python(
        frame, ist_date=_ist_expr(), source=None, max_ratio=BAD_TICK_MAX_RATIO, log=None
    )


def _routed(
    frame: pl.DataFrame,
    *,
    max_ratio: float = BAD_TICK_MAX_RATIO,
    python_law: Callable[[], pl.DataFrame] | None = None,
) -> pl.DataFrame:
    return btr.routed_screen_frame(
        frame,
        ts_column="ts_utc",
        source=None,
        max_ratio=max_ratio,
        log=None,
        python_law=(python_law if python_law is not None else lambda: _oracle(frame)),
    )


def _use_kernel(monkeypatch: pytest.MonkeyPatch, kernel: Callable[..., object] | None) -> None:
    """Route the facade's kernel resolution at *kernel* (None = wheel absent)."""
    monkeypatch.setattr(btr, "resolve_feed_kernel", lambda: kernel)


@pytest.fixture(autouse=True)
def _isolated_facade(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv(btr.FEED_RUST_FRAME_ENV, raising=False)
    btr.reset_bad_tick_rust_for_tests()
    yield
    btr.reset_bad_tick_rust_for_tests()


class TestFlagResolution:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("1", True),
            ("true", True),
            (" YES ", True),
            ("on", True),
            ("0", False),
            ("off", False),
            ("", False),
            (None, False),
        ],
    )
    def test_flag_matrix(
        self, value: str | None, expected: bool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        if value is None:
            monkeypatch.delenv(btr.FEED_RUST_FRAME_ENV, raising=False)
        else:
            monkeypatch.setenv(btr.FEED_RUST_FRAME_ENV, value)
        assert btr.frame_screen_rust_enabled() is expected

    def test_junk_is_loud_off_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(btr.FEED_RUST_FRAME_ENV, "banana")
        with capture_logs() as logs:
            assert btr.frame_screen_rust_enabled() is False
            assert btr.frame_screen_rust_enabled() is False  # memoised repeat
        warns = [e for e in logs if e["event"] == "bad_tick_rust.fallback"]
        assert len(warns) == 1
        assert warns[0]["reason"] == "flag_unrecognised"
        assert warns[0]["env_var"] == btr.FEED_RUST_FRAME_ENV


class TestLatchAndGuards:
    def test_latched_process_serves_oracle_without_kernel(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _bomb() -> object:
            raise AssertionError("latched routed call resolved the kernel")

        monkeypatch.setattr(btr, "resolve_feed_kernel", _bomb)
        btr._disable("test_latch")
        frame = _frame(_SPIKED)
        assert _routed(frame).equals(_oracle(frame))

    def test_kernel_unavailable_latches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _use_kernel(monkeypatch, None)
        frame = _frame(_SPIKED)
        with capture_logs() as logs:
            assert _routed(frame).equals(_oracle(frame))
        assert any(
            e["event"] == "bad_tick_rust.rust_path_disabled" and e["reason"] == "kernel_unavailable"
            for e in logs
        )
        assert btr.frame_screen_rust_disabled() is True

    def test_disable_repeat_is_noop(self) -> None:
        btr._disable("first")
        btr._disable("second")
        assert btr._DISABLED_REASON[0] == "first"

    def test_warn_once_repeat_is_memoised(self) -> None:
        with capture_logs() as logs:
            btr._warn_once("some_reason")
            btr._warn_once("some_reason")
        assert len([e for e in logs if e["event"] == "bad_tick_rust.fallback"]) == 1

    def test_reset_seam_restores_live_state(self) -> None:
        btr._disable("test_latch")
        btr._BURN_IN_REMAINING[0] = 0
        btr._warn_once("some_reason")
        btr.reset_bad_tick_rust_for_tests()
        assert btr.frame_screen_rust_disabled() is False
        assert btr._BURN_IN_REMAINING[0] == btr.BURN_IN_SAMPLES
        assert not btr._OBSERVABILITY_MEMO


class TestInputGates:
    def test_non_us_time_unit_degrades(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _use_kernel(monkeypatch, fake_scan_bars)
        frame = _frame(_SPIKED, dtype=pl.Datetime("ms", "UTC"))
        with capture_logs() as logs:
            assert _routed(frame).equals(_oracle(frame))
        assert any(
            e["event"] == "bad_tick_rust.fallback" and e["reason"] == "frame_ts_dtype" for e in logs
        )
        assert btr.frame_screen_rust_disabled() is False  # degrade, never a latch

    def test_non_utc_zone_degrades(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _use_kernel(monkeypatch, fake_scan_bars)
        frame = _frame(_SPIKED, dtype=pl.Datetime("us", "Asia/Kolkata"))
        with capture_logs() as logs:
            assert _routed(frame).equals(_oracle(frame))
        assert any(e.get("reason") == "frame_ts_dtype" for e in logs)

    def test_null_ts_degrades(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _use_kernel(monkeypatch, fake_scan_bars)
        frame = _frame(_SPIKED).with_columns(
            pl.when(pl.int_range(pl.len()) == 3)
            .then(None)
            .otherwise(pl.col("ts_utc"))
            .alias("ts_utc")
        )
        sentinel = _frame([1.0])
        with capture_logs() as logs:
            assert _routed(frame, python_law=lambda: sentinel) is sentinel
        assert any(e.get("reason") == "frame_nulls" for e in logs)

    def test_null_close_degrades(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _use_kernel(monkeypatch, fake_scan_bars)
        frame = _frame(_SPIKED).with_columns(
            pl.when(pl.int_range(pl.len()) == 3)
            .then(None)
            .otherwise(pl.col("close"))
            .alias("close")
        )
        sentinel = _frame([1.0])
        with capture_logs() as logs:
            assert _routed(frame, python_law=lambda: sentinel) is sentinel
        assert any(e.get("reason") == "frame_nulls" for e in logs)


class TestKernelFaults:
    def test_degrade_error_serves_oracle_with_one_warn(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _raises(*_a: object) -> object:
            raise ValueError("kernel precondition")

        _use_kernel(monkeypatch, _raises)
        frame = _frame(_SPIKED)
        with capture_logs() as logs:
            assert _routed(frame).equals(_oracle(frame))
            assert _routed(frame).equals(_oracle(frame))  # warn memoised
        assert (
            len(
                [
                    e
                    for e in logs
                    if e["event"] == "bad_tick_rust.fallback" and e["reason"] == "kernel_error"
                ]
            )
            == 1
        )
        assert btr.frame_screen_rust_disabled() is False

    @pytest.mark.parametrize("exc_type", [MemoryError, KeyboardInterrupt, SystemExit])
    def test_escape_set_reraises_raw(
        self, exc_type: type[BaseException], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _raises(*_a: object) -> object:
            raise exc_type("boom")

        _use_kernel(monkeypatch, _raises)
        with pytest.raises(exc_type):
            _routed(_frame(_SPIKED))

    def test_panic_belt_latches_kernel_panic(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from polars.exceptions import PanicException

        def _panics(*_a: object) -> object:
            raise PanicException("kernel panicked")

        _use_kernel(monkeypatch, _panics)
        frame = _frame(_SPIKED)
        with capture_logs() as logs:
            assert _routed(frame).equals(_oracle(frame))
        assert any(
            e["event"] == "bad_tick_rust.rust_path_disabled" and e["reason"] == "kernel_panic"
            for e in logs
        )
        assert btr.frame_screen_rust_disabled() is True

    def test_abstain_none_degrades(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _use_kernel(monkeypatch, lambda *_a: None)
        frame = _frame(_SPIKED)
        with capture_logs() as logs:
            assert _routed(frame).equals(_oracle(frame))
        assert any(e.get("reason") == "ts_out_of_ist_envelope" for e in logs)

    def test_poisoned_result_refused_via_validate_scan(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A negative kept index would silently wrap Python list selection —
        # validate_scan refuses; the oracle serves.
        _use_kernel(monkeypatch, lambda *_a: ([-1], [], None))
        frame = _frame(_SPIKED)
        with capture_logs() as logs:
            assert _routed(frame).equals(_oracle(frame))
        assert any(
            e["event"] == "feed_rust.fallback" and e["reason"] == "kernel_invalid" for e in logs
        )
        assert btr.frame_screen_rust_disabled() is False

    def test_positivity_stop_degrades_to_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The tf_autoderive precondition trap: a non-positive close legally
        # reaches this seam there — the kernel STOPS, the frame law must
        # DEGRADE (the oracle screens it as a ratio decision), never raise.
        _use_kernel(monkeypatch, fake_scan_bars)
        closes = [100.0, -5.0, 101.0, 1005.0]
        frame = _frame(closes)
        with capture_logs() as logs:
            got = _routed(frame)
        assert any(e.get("reason") == "unexpected_nonpositive_close" for e in logs)
        assert got.equals(_oracle(frame))
        assert btr.frame_screen_rust_disabled() is False


class TestBurnInAndSteadyState:
    def test_clean_burn_in_counts_down_and_completes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _use_kernel(monkeypatch, fake_scan_bars)
        frame = _frame(_SPIKED)
        btr.reset_bad_tick_rust_for_tests(remaining=2)
        with capture_logs() as logs:
            assert _routed(frame).equals(_oracle(frame))
            assert btr._BURN_IN_REMAINING[0] == 1
            assert _routed(frame).equals(_oracle(frame))
            assert btr._BURN_IN_REMAINING[0] == 0
        assert any(e["event"] == "bad_tick_rust.burn_in_complete" for e in logs)
        assert any(e["event"] == "bad_tick_rust.engaged" for e in logs)

    def test_burn_in_mismatch_ships_oracle_and_autoreverts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A keeps-everything kernel is shape-valid but decision-WRONG on a
        # spiked frame — burn-in ships the ORACLE frame (spike dropped, warn
        # emitted by the oracle itself) and latches at ERROR.
        _use_kernel(monkeypatch, lambda ts, *_a: (list(range(len(ts))), [], None))
        frame = _frame(_SPIKED)
        with capture_logs() as logs:
            got = _routed(frame)
        assert got.equals(_oracle(frame))  # the spike is still dropped
        assert any(
            e["event"] == "bad_tick_rust.autoreverted" and e["reason"] == "burn_in_mismatch"
            for e in logs
        )
        assert btr.frame_screen_rust_disabled() is True
        # Exactly ONE warn emission (the oracle's) — never a double.
        assert len([e for e in logs if e["event"] == "feed.bad_tick_skipped"]) == 1

    def test_steady_state_ships_kernel_decisions_uncompared(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The anti-vacuity teeth: at remaining=0 the SAME wrong kernel ships
        # its own decisions (production carries the burn-in against exactly
        # this — bypassed BY DESIGN here to prove the routed arm serves).
        _use_kernel(monkeypatch, lambda ts, *_a: (list(range(len(ts))), [], None))
        btr.reset_bad_tick_rust_for_tests(remaining=0)
        frame = _frame(_SPIKED)
        got = _routed(frame)
        assert got.equals(frame)  # everything kept — the kernel decided
        assert btr.frame_screen_rust_disabled() is False

    def test_steady_state_warn_fields_match_oracle_log_sequence(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Two poisoned sessions → two boundary/finish warns; the routed
        # warn STREAM (event name + all field values) equals the oracle's.
        _use_kernel(monkeypatch, fake_scan_bars)
        btr.reset_bad_tick_rust_for_tests(remaining=0)
        closes = [100.0, 1005.0, 101.0, 200.0, 2000.5, 2001.0, 201.0]
        days = [0, 0, 0, 1, 1, 1, 1]
        frame = _frame(closes, days=days)
        with capture_logs() as routed_logs:
            routed = _routed(frame)
        with capture_logs() as oracle_logs:
            oracle = _oracle(frame)
        assert routed.equals(oracle)

        def _warn_stream(logs: list[EventDict]) -> list[tuple[Any, ...]]:
            return [
                (
                    e["session"],
                    e["skipped"],
                    e["first_prev_close"],
                    e["first_close"],
                    e["max_ratio"],
                    e["source"],
                )
                for e in logs
                if e["event"] == "feed.bad_tick_skipped"
            ]

        routed_stream = _warn_stream(routed_logs)
        assert routed_stream == _warn_stream(oracle_logs)
        assert len(routed_stream) == 2  # anti-vacuous: both sessions warned

    def test_engaged_info_logs_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _use_kernel(monkeypatch, fake_scan_bars)
        btr.reset_bad_tick_rust_for_tests(remaining=0)
        frame = _frame(_SPIKED)
        with capture_logs() as logs:
            _routed(frame)
            _routed(frame)
        assert len([e for e in logs if e["event"] == "bad_tick_rust.engaged"]) == 1


class TestSessionIso:
    def test_naive_and_aware_utc_agree(self) -> None:
        naive = datetime(2024, 1, 2, 3, 45)
        aware = datetime(2024, 1, 2, 3, 45, tzinfo=UTC)
        assert btr._session_iso(naive) == "2024-01-02"
        assert btr._session_iso(aware) == "2024-01-02"

    def test_sub_0530_utc_lands_on_prior_ist_day_boundary(self) -> None:
        # 23:45 UTC = 05:15 IST next day — the IST day advances.
        assert btr._session_iso(datetime(2024, 1, 1, 23, 45, tzinfo=UTC)) == "2024-01-02"
        # 18:29 UTC = 23:59 IST same day.
        assert btr._session_iso(datetime(2024, 1, 1, 18, 29, tzinfo=UTC)) == "2024-01-01"


class TestMutationWorkflowWiring:
    """Pin this module INTO ``mutation.yml``'s per-module zero-survivor loop.

    The ``test_detectors_rust.py`` / ``test_today_completeness.py``
    precedent: the facade's degrade-set membership, burn-in countdown and
    warn-replay index arithmetic are exactly the killable-mutant classes,
    so the module:suite pair sits in the workflow's per-module ``mutmut``
    loop with the MANDATORY 30 s wrap cap (the warn-drain / decision loops
    carry spin-capable loop mutants — the audit-mutant-173 wedge class).
    HONEST: this pins the WIRING only — the weekly run executes on its own
    schedule/push triggers.
    """

    def test_module_paired_with_this_suite_in_the_mutmut_loop(self) -> None:
        from pathlib import Path

        workflow = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "mutation.yml"
        text = workflow.read_text(encoding="utf-8")
        assert '"brutex/data/bad_tick_rust.py:tests/unit/test_bad_tick_rust.py:30"' in text, (
            "bad_tick_rust.py is not wired into mutation.yml's per-module mutmut loop"
        )
