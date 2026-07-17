"""Unit suite for ``brutex.data.lake_rust_resample_daily`` — 100% line+branch standalone.

Every test injects a FAKE ``brutex_lake`` module straight into
``sys.modules`` (or a poisoned ``None`` entry for the import-guard arm), so
the suite runs with NO wheel installed — the real-wheel parity corpus lives
in ``tests/integration/test_lake_rust_resample_daily_parity.py``. The
harness mirrors ``tests/unit/test_lake_rust_resample.py`` (the G5 facade
suite) section-for-section over the DAILY facade's INDEPENDENT state
(DEC-LAKE-RUST-RESAMPLE-DAILY-001).
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import io
import sys
import types
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

import polars as pl
import pytest
from brutex.data import lake_rust_resample as lrs
from brutex.data import lake_rust_resample_daily as lrd
from structlog.testing import capture_logs

if TYPE_CHECKING:
    from collections.abc import Iterator

# ---------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _fresh_process_view() -> Iterator[None]:
    """Reset BOTH facades' latches around every test (independence pins read both)."""
    lrd.reset_lake_rust_resample_daily_for_tests()
    lrs.reset_lake_rust_resample_for_tests()
    yield
    lrd.reset_lake_rust_resample_daily_for_tests()
    lrs.reset_lake_rust_resample_for_tests()


def _df_1m() -> pl.DataFrame:
    """A tiny valid 1-minute frame (two bars at the 09:15 IST open)."""
    import datetime as dt

    return pl.DataFrame(
        {
            "ts_utc": [dt.datetime(2024, 1, 1, 3, 45), dt.datetime(2024, 1, 1, 3, 46)],
            "open": [100.0, 101.0],
            "high": [110.0, 111.0],
            "low": [90.0, 91.0],
            "close": [105.0, 106.0],
            "volume": [10, 11],
        }
    ).with_columns(pl.col("ts_utc").cast(pl.Datetime("us")))


#: The forming-session cutoff every test passes (the day after the fixture).
_BEFORE = date(2024, 1, 2)

#: The frame the ORACLE stub returns.
_ORACLE_DF = pl.DataFrame({"ts_utc": [1], "close": [105.0]})

#: A value-DIFFERENT frame (burn-in value-mismatch arm).
_WRONG_DF = pl.DataFrame({"ts_utc": [1], "close": [999.0]})

#: A value-IDENTICAL, dtype-PUNNED frame (Float32 close) — the #1795b tooth.
_PUN_DF = _ORACLE_DF.with_columns(pl.col("close").cast(pl.Float32))


def _ipc(df: pl.DataFrame) -> bytes:
    buf = io.BytesIO()
    df.write_ipc(buf, compression="uncompressed")
    return buf.getvalue()


def _fake_crate(
    *,
    pinned: Any = pl.__version__,
    daily: Any = None,
    zstd: Any = None,
) -> types.ModuleType:
    """Build a fake ``brutex_lake`` module with injectable daily behaviour."""
    module = types.ModuleType("brutex_lake")
    module.pinned_polars_version = (  # type: ignore[attr-defined]
        pinned if callable(pinned) else (lambda: pinned)
    )
    if zstd is None:
        zstd = lambda: "1.5.7"  # noqa: E731 - test stub
    module.linked_zstd_version = zstd  # type: ignore[attr-defined]
    if daily is None:
        daily = lambda ipc, before_days, oi: _ipc(_ORACLE_DF)  # noqa: E731 - test stub
    module.resample_daily_ipc = daily  # type: ignore[attr-defined]
    return module


def _install_crate(monkeypatch: pytest.MonkeyPatch, module: types.ModuleType | None) -> None:
    monkeypatch.setitem(sys.modules, "brutex_lake", module)


class _CountingOracle:
    """Daily-oracle stub counting invocations (returns :data:`_ORACLE_DF`)."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, df_1m: Any, before: date) -> pl.DataFrame:
        self.calls += 1
        return _ORACLE_DF


def _run(
    monkeypatch: pytest.MonkeyPatch,
    module: types.ModuleType | None,
    *,
    oi_aware: bool = True,
) -> pl.DataFrame:
    _install_crate(monkeypatch, module)
    return lrd.resample_daily_frame_via_rust(
        _df_1m(), _BEFORE, oi_aware=oi_aware, oracle=_CountingOracle()
    )


# ---------------------------------------------------------------------------
# flag parsing
# ---------------------------------------------------------------------------


class TestFlagParsing:
    @pytest.mark.parametrize("raw", ["1", "true", "YES", " on "])
    def test_truthy_literals_enable(self, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
        monkeypatch.setenv(lrd.LAKE_RUST_RESAMPLE_DAILY_ENV, raw)
        assert lrd.lake_rust_resample_daily_enabled() is True

    @pytest.mark.parametrize("raw", ["0", "false", "NO", " off "])
    def test_falsy_literals_disable_silently(
        self, monkeypatch: pytest.MonkeyPatch, raw: str
    ) -> None:
        monkeypatch.setenv(lrd.LAKE_RUST_RESAMPLE_DAILY_ENV, raw)
        with capture_logs() as logs:
            assert lrd.lake_rust_resample_daily_enabled() is False
        assert logs == []

    def test_unset_is_the_silent_default_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(lrd.LAKE_RUST_RESAMPLE_DAILY_ENV, raising=False)
        with capture_logs() as logs:
            assert lrd.lake_rust_resample_daily_enabled() is False
        assert logs == []

    def test_whitespace_is_the_silent_default_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(lrd.LAKE_RUST_RESAMPLE_DAILY_ENV, "   ")
        with capture_logs() as logs:
            assert lrd.lake_rust_resample_daily_enabled() is False
        assert logs == []

    def test_junk_is_loud_off_warned_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(lrd.LAKE_RUST_RESAMPLE_DAILY_ENV, "onn")
        with capture_logs() as logs:
            assert lrd.lake_rust_resample_daily_enabled() is False
            assert lrd.lake_rust_resample_daily_enabled() is False
        warns = [e for e in logs if e["event"] == "lake_rust_resample_daily.fallback"]
        assert len(warns) == 1
        assert warns[0]["reason"] == "flag_unrecognised"

    def test_daily_flag_is_independent_of_the_g5_flag(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arming the G5 intraday flag must NOT arm the daily seam and vice
        # versa — two flags, two flips, two evidence trails.
        monkeypatch.setenv(lrs.LAKE_RUST_RESAMPLE_ENV, "1")
        monkeypatch.delenv(lrd.LAKE_RUST_RESAMPLE_DAILY_ENV, raising=False)
        assert lrd.lake_rust_resample_daily_enabled() is False
        monkeypatch.delenv(lrs.LAKE_RUST_RESAMPLE_ENV, raising=False)
        monkeypatch.setenv(lrd.LAKE_RUST_RESAMPLE_DAILY_ENV, "1")
        assert lrs.lake_rust_resample_enabled() is False
        assert lrd.lake_rust_resample_daily_enabled() is True


# ---------------------------------------------------------------------------
# days-since-epoch rendering
# ---------------------------------------------------------------------------


class TestDaysSinceEpoch:
    def test_epoch_is_zero(self) -> None:
        assert lrd.days_since_epoch(date(1970, 1, 1)) == 0

    def test_known_ordinals(self) -> None:
        # 2024-01-01 == 19_723 days after the epoch (the crate test's twin
        # constant); pre-epoch dates go negative (pure calendar arithmetic).
        assert lrd.days_since_epoch(date(2024, 1, 1)) == 19_723
        assert lrd.days_since_epoch(date(1969, 12, 31)) == -1


# ---------------------------------------------------------------------------
# latch / reset / independence
# ---------------------------------------------------------------------------


class TestLatchAndReset:
    def test_disabled_short_circuits_before_any_crate_touch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _bomb(*_args: object) -> bytes:
            raise AssertionError("crate must not be touched once latched")

        lrd._disable("test_latch")
        assert lrd.rust_resample_daily_disabled() is True
        _install_crate(monkeypatch, _fake_crate(daily=_bomb))
        oracle = _CountingOracle()
        out = lrd.resample_daily_frame_via_rust(_df_1m(), _BEFORE, oi_aware=True, oracle=oracle)
        assert out.equals(_ORACLE_DF)
        assert oracle.calls == 1

    def test_repeat_disable_is_a_noop_keeping_the_first_reason(self) -> None:
        lrd._disable("first")
        with capture_logs() as logs:
            lrd._disable("second")
        assert logs == []
        assert lrd._DISABLED_REASON[0] == "first"

    def test_reset_restores_a_fresh_view(self) -> None:
        lrd._disable("test_latch")
        lrd._BURN_IN_REMAINING[0] = 0
        lrd.reset_lake_rust_resample_daily_for_tests()
        assert lrd.rust_resample_daily_disabled() is False
        assert lrd._BURN_IN_REMAINING[0] == lrd.BURN_IN_SAMPLES
        assert lrd._BURN_IN_SHAPE_GATE[0] is True

    def test_reset_remaining_zero_is_the_steady_state(self) -> None:
        lrd.reset_lake_rust_resample_daily_for_tests(remaining=0)
        assert lrd._BURN_IN_REMAINING[0] == 0
        assert lrd._BURN_IN_SHAPE_GATE[0] is False

    def test_daily_latch_never_touches_the_g5_latch(self) -> None:
        # The independence law: a daily autorevert must not disable the
        # intraday seam (and the G5 latch stays whatever it was).
        lrd._disable("daily_only")
        assert lrd.rust_resample_daily_disabled() is True
        assert lrs.rust_resample_disabled() is False
        lrd.reset_lake_rust_resample_daily_for_tests()
        lrs._disable("g5_only")
        assert lrs.rust_resample_disabled() is True
        assert lrd.rust_resample_daily_disabled() is False


# ---------------------------------------------------------------------------
# import / API guard
# ---------------------------------------------------------------------------


class TestImportGuard:
    def test_absent_wheel_latches_and_serves_the_oracle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with capture_logs() as logs:
            out = _run(monkeypatch, None)  # None in sys.modules → ImportError
        assert out.equals(_ORACLE_DF)
        assert lrd._DISABLED_REASON[0] == "crate_unavailable"
        events = [e["event"] for e in logs]
        assert "lake_rust_resample_daily.rust_path_disabled" in events

    def test_pre_t11_wheel_missing_the_daily_face_latches_daily_only(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A G5-era wheel imports fine but lacks resample_daily_ipc — the
        # DAILY API guard latches crate_unavailable while the G5 facade's
        # latch is untouched (its own guard would still pass on such a
        # wheel — the independence law).
        module = _fake_crate()
        delattr(module, "resample_daily_ipc")
        out = _run(monkeypatch, module)
        assert out.equals(_ORACLE_DF)
        assert lrd._DISABLED_REASON[0] == "crate_unavailable"
        assert lrs.rust_resample_disabled() is False

    def test_non_callable_api_member_latches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        module = _fake_crate()
        module.resample_daily_ipc = "not-callable"  # type: ignore[attr-defined]
        out = _run(monkeypatch, module)
        assert out.equals(_ORACLE_DF)
        assert lrd._DISABLED_REASON[0] == "crate_unavailable"


# ---------------------------------------------------------------------------
# version / metadata guards
# ---------------------------------------------------------------------------


class TestVersionGuard:
    def test_version_mismatch_latches_naming_both(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with capture_logs() as logs:
            out = _run(monkeypatch, _fake_crate(pinned="9.99.9"))
        assert out.equals(_ORACLE_DF)
        assert lrd._DISABLED_REASON[0] == "polars_version_mismatch"
        disabled = [e for e in logs if e["event"] == "lake_rust_resample_daily.rust_path_disabled"]
        assert disabled[0]["installed_polars"] == pl.__version__
        assert disabled[0]["crate_pinned_polars"] == "9.99.9"
        # Operator-guidance prose pinned EXACTLY (mutmut zero-survivor law —
        # the remedy string is the operator's recovery path, not decoration).
        assert disabled[0]["remedy"] == (
            "align the wheel and the crate's pinned polars rev, then "
            "re-run the parity corpus "
            "(docs/runbooks/lake-rust-render-lockstep.md)"
        )

    def test_matching_version_engages(self, monkeypatch: pytest.MonkeyPatch) -> None:
        lrd.reset_lake_rust_resample_daily_for_tests(remaining=0)
        out = _run(monkeypatch, _fake_crate())
        assert out.equals(_ORACLE_DF)
        assert lrd.rust_resample_daily_disabled() is False

    def test_none_returning_pinned_version_fails_the_version_guard(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        out = _run(monkeypatch, _fake_crate(pinned=lambda: None))
        assert out.equals(_ORACLE_DF)
        assert lrd._DISABLED_REASON[0] == "polars_version_mismatch"


class TestMetadataGuard:
    def test_raising_pinned_version_latches_metadata_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom() -> str:
            raise RuntimeError("stale install")

        out = _run(monkeypatch, _fake_crate(pinned=_boom))
        assert out.equals(_ORACLE_DF)
        assert lrd._DISABLED_REASON[0] == "crate_metadata_error"

    def test_memoryerror_in_metadata_re_raises_raw(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _oom() -> str:
            raise MemoryError

        _install_crate(monkeypatch, _fake_crate(pinned=_oom))
        with pytest.raises(MemoryError):
            lrd.resample_daily_frame_via_rust(
                _df_1m(), _BEFORE, oi_aware=True, oracle=_CountingOracle()
            )

    def test_raising_zstd_metadata_on_the_engaged_path_latches(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom() -> str:
            raise ValueError("no fingerprint")

        out = _run(monkeypatch, _fake_crate(zstd=_boom))
        assert out.equals(_ORACLE_DF)
        assert lrd._DISABLED_REASON[0] == "crate_metadata_error"


# ---------------------------------------------------------------------------
# burn-in self-check
# ---------------------------------------------------------------------------


class TestBurnIn:
    def test_matching_frames_serve_rust_and_decrement_budget(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        before = lrd._BURN_IN_REMAINING[0]
        out = _run(monkeypatch, _fake_crate())
        assert out.equals(_ORACLE_DF)
        assert lrd._BURN_IN_REMAINING[0] == before - 1
        assert lrd.rust_resample_daily_disabled() is False

    def test_value_mismatch_autoreverts_and_ships_the_oracle_frame(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with capture_logs() as logs:
            out = _run(monkeypatch, _fake_crate(daily=lambda i, b, o: _ipc(_WRONG_DF)))
        assert out.equals(_ORACLE_DF)
        assert lrd._DISABLED_REASON[0] == "burn_in_frame_mismatch"
        errors = [e for e in logs if e["event"] == "lake_rust_resample_daily.autoreverted"]
        assert len(errors) == 1

    def test_dtype_pun_mismatch_autoreverts_despite_equals_true(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert _PUN_DF.equals(_ORACLE_DF)
        out = _run(monkeypatch, _fake_crate(daily=lambda i, b, o: _ipc(_PUN_DF)))
        assert out.equals(_ORACLE_DF)
        assert out.schema == _ORACLE_DF.schema
        assert lrd._DISABLED_REASON[0] == "burn_in_frame_mismatch"

    def test_burn_in_complete_logs_once_and_enters_steady_state(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lrd.reset_lake_rust_resample_daily_for_tests(remaining=1)
        lrd._BURN_IN_SHAPE_GATE[0] = True
        _install_crate(monkeypatch, _fake_crate())
        with capture_logs() as logs:
            lrd.resample_daily_frame_via_rust(
                _df_1m(), _BEFORE, oi_aware=True, oracle=_CountingOracle()
            )
        assert lrd._BURN_IN_REMAINING[0] == 0
        events = [e["event"] for e in logs]
        assert "lake_rust_resample_daily.burn_in_complete" in events

    def test_steady_state_serves_rust_with_no_oracle_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lrd.reset_lake_rust_resample_daily_for_tests(remaining=0)
        oracle = _CountingOracle()
        _install_crate(monkeypatch, _fake_crate())
        out = lrd.resample_daily_frame_via_rust(_df_1m(), _BEFORE, oi_aware=True, oracle=oracle)
        assert out.equals(_ORACLE_DF)
        assert oracle.calls == 0

    def test_engaged_info_fires_once_per_process(self, monkeypatch: pytest.MonkeyPatch) -> None:
        zstd_calls = [0]

        def _zstd() -> str:
            zstd_calls[0] += 1
            return "1.5.7"

        _install_crate(monkeypatch, _fake_crate(zstd=_zstd))
        with capture_logs() as logs:
            lrd.resample_daily_frame_via_rust(
                _df_1m(), _BEFORE, oi_aware=True, oracle=_CountingOracle()
            )
            lrd.resample_daily_frame_via_rust(
                _df_1m(), _BEFORE, oi_aware=True, oracle=_CountingOracle()
            )
        engaged = [e for e in logs if e["event"] == "lake_rust_resample_daily.engaged"]
        assert len(engaged) == 1
        assert zstd_calls[0] == 1


class TestShapeGate:
    def test_unseen_oi_arm_after_budget_spent_still_burns_in(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The daily token is {schema}|daily|oi_aware={flag} — the OTHER
        # oi_aware arm of the SAME schema is a new token.
        lrd.reset_lake_rust_resample_daily_for_tests(remaining=1)
        oracle = _CountingOracle()
        _install_crate(monkeypatch, _fake_crate())
        lrd.resample_daily_frame_via_rust(_df_1m(), _BEFORE, oi_aware=True, oracle=oracle)
        assert lrd._BURN_IN_REMAINING[0] == 0
        assert oracle.calls == 1
        lrd.resample_daily_frame_via_rust(_df_1m(), _BEFORE, oi_aware=False, oracle=oracle)
        assert oracle.calls == 2
        # The SAME token again is steady state — no oracle; a DIFFERENT
        # `before` never re-tokens (value-independent law).
        lrd.resample_daily_frame_via_rust(_df_1m(), date(2025, 6, 6), oi_aware=False, oracle=oracle)
        assert oracle.calls == 2

    def test_shape_gate_mismatch_autoreverts_after_budget_spent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lrd.reset_lake_rust_resample_daily_for_tests(remaining=1)
        crate = _fake_crate()
        _install_crate(monkeypatch, crate)
        lrd.resample_daily_frame_via_rust(
            _df_1m(), _BEFORE, oi_aware=True, oracle=_CountingOracle()
        )
        crate.resample_daily_ipc = lambda i, b, o: _ipc(_WRONG_DF)  # type: ignore[attr-defined]
        out = lrd.resample_daily_frame_via_rust(
            _df_1m(), _BEFORE, oi_aware=False, oracle=_CountingOracle()
        )
        assert out.equals(_ORACLE_DF)
        assert lrd._DISABLED_REASON[0] == "burn_in_frame_mismatch"


# ---------------------------------------------------------------------------
# degrade paths
# ---------------------------------------------------------------------------


class TestDegradePaths:
    @pytest.mark.parametrize(
        "exc",
        [
            ValueError("crate says no"),
            RuntimeError("panicked (caught at the FFI boundary)"),
            TypeError("bad argument"),
            OverflowError("before_days too large"),
            AttributeError("broken internals"),
            pl.exceptions.ComputeError("ipc leg"),
        ],
    )
    def test_crate_raise_degrades_to_the_oracle_without_latching(
        self, monkeypatch: pytest.MonkeyPatch, exc: Exception
    ) -> None:
        def _raise(*_args: object) -> bytes:
            raise exc

        with capture_logs() as logs:
            out = _run(monkeypatch, _fake_crate(daily=_raise))
        assert out.equals(_ORACLE_DF)
        assert lrd.rust_resample_daily_disabled() is False
        warns = [e for e in logs if e["event"] == "lake_rust_resample_daily.fallback"]
        assert warns[0]["reason"] == "resample_error"

    @pytest.mark.parametrize("exc_type", [KeyboardInterrupt, SystemExit, MemoryError])
    def test_escape_set_re_raises_raw(
        self, monkeypatch: pytest.MonkeyPatch, exc_type: type[BaseException]
    ) -> None:
        def _raise(*_args: object) -> bytes:
            raise exc_type

        _install_crate(monkeypatch, _fake_crate(daily=_raise))
        with pytest.raises(exc_type):
            lrd.resample_daily_frame_via_rust(
                _df_1m(), _BEFORE, oi_aware=True, oracle=_CountingOracle()
            )

    def test_non_bytes_result_is_refuse_poison(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with capture_logs() as logs:
            out = _run(monkeypatch, _fake_crate(daily=lambda i, b, o: 12345))
        assert out.equals(_ORACLE_DF)
        assert lrd.rust_resample_daily_disabled() is False
        warns = [e for e in logs if e["event"] == "lake_rust_resample_daily.fallback"]
        assert warns[0]["reason"] == "resample_result_refused"
        assert warns[0]["result_type"] == "int"

    def test_undecodable_result_bytes_degrade_to_the_oracle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with capture_logs() as logs:
            out = _run(monkeypatch, _fake_crate(daily=lambda i, b, o: b"garbage"))
        assert out.equals(_ORACLE_DF)
        warns = [e for e in logs if e["event"] == "lake_rust_resample_daily.fallback"]
        assert warns[0]["reason"] == "result_decode_error"

    def test_warn_once_dedupes_per_reason(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_crate(monkeypatch, _fake_crate(daily=lambda i, b, o: b"garbage"))
        with capture_logs() as logs:
            for _ in range(3):
                lrd.resample_daily_frame_via_rust(
                    _df_1m(), _BEFORE, oi_aware=True, oracle=_CountingOracle()
                )
        warns = [e for e in logs if e["event"] == "lake_rust_resample_daily.fallback"]
        assert len(warns) == 1


# ---------------------------------------------------------------------------
# panic belt (shared machinery, exercised through the DAILY router)
# ---------------------------------------------------------------------------


class _FakePanicException(BaseException):
    """Stand-in for pyo3_runtime.PanicException (a BaseException subclass)."""


class TestPanicBelt:
    def test_escaped_panic_latches_crate_panic_and_serves_the_oracle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        module = types.ModuleType("pyo3_runtime")
        module.PanicException = _FakePanicException  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "pyo3_runtime", module)

        def _panic(*_args: object) -> bytes:
            raise _FakePanicException("thread panicked")

        out = _run(monkeypatch, _fake_crate(daily=_panic))
        assert out.equals(_ORACLE_DF)
        assert lrd._DISABLED_REASON[0] == "crate_panic"

    def test_out_of_degrade_set_exception_latches_via_the_belt(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _weird(*_args: object) -> bytes:
            raise KeyError("not in the degrade set")

        out = _run(monkeypatch, _fake_crate(daily=_weird))
        assert out.equals(_ORACLE_DF)
        assert lrd._DISABLED_REASON[0] == "crate_panic"


# ---------------------------------------------------------------------------
# call-shape + engaged-arg pins
# ---------------------------------------------------------------------------


class TestCallShape:
    def test_before_crosses_as_days_since_epoch_and_oi_aware_passes_through(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[tuple[int, bool]] = []

        def _spy(ipc: bytes, before_days: int, oi: bool) -> bytes:
            seen.append((before_days, oi))
            return _ipc(_ORACLE_DF)

        _install_crate(monkeypatch, _fake_crate(daily=_spy))
        lrd.resample_daily_frame_via_rust(
            _df_1m(), date(2024, 1, 2), oi_aware=True, oracle=_CountingOracle()
        )
        lrd.resample_daily_frame_via_rust(
            _df_1m(), date(1970, 1, 1), oi_aware=False, oracle=_CountingOracle()
        )
        assert seen == [(19_724, True), (0, False)]

    def test_ipc_payload_round_trips_the_input_frame(self) -> None:
        df = _df_1m()
        back = pl.read_ipc(io.BytesIO(lrs.ipc_payload(df)))
        assert back.schema == df.schema
        assert back.equals(df)


# ---------------------------------------------------------------------------
# mutation hardening / source pins
# ---------------------------------------------------------------------------


class TestMutationHardening:
    def test_flag_env_name_literal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert lrd.LAKE_RUST_RESAMPLE_DAILY_ENV == "BRUTEX_LAKE_RUST_RESAMPLE_DAILY"
        monkeypatch.setenv("BRUTEX_LAKE_RUST_RESAMPLE_DAILY", "1")
        assert lrd.lake_rust_resample_daily_enabled() is True

    def test_burn_in_samples_shared_with_the_g5_facade(self) -> None:
        # Imported, never re-minted (the #1531/#1532 16-sample law).
        assert lrd.BURN_IN_SAMPLES is lrs.BURN_IN_SAMPLES
        assert lrd.BURN_IN_SAMPLES == 16

    def test_crate_api_daily_names_are_registered_in_the_shim(self) -> None:
        from pathlib import Path

        root = Path(__file__).resolve().parents[2]
        shim = (
            root / "rust" / "brutex-lake" / "python" / "brutex_lake" / "__init__.py"
        ).read_text()
        for name in lrd._CRATE_API_DAILY:
            assert name in shim, f"{name} missing from the brutex_lake shim re-exports"
        # The session face rides the G5 facade but ships in the same wheel.
        assert "resample_session_ipc" in shim

    def test_import_guard_remedy_and_detail_are_exact(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        remedy = "pip install rust/brutex-lake (see docs/runbooks/lake-rust-render-lockstep.md)"
        with capture_logs() as logs:
            _run(monkeypatch, None)
        disabled = [e for e in logs if e["event"] == "lake_rust_resample_daily.rust_path_disabled"]
        assert disabled[0]["remedy"] == remedy
        lrd.reset_lake_rust_resample_daily_for_tests()
        module = _fake_crate()
        module.resample_daily_ipc = "not-callable"  # type: ignore[attr-defined]
        with capture_logs() as logs:
            _run(monkeypatch, module)
        disabled = [e for e in logs if e["event"] == "lake_rust_resample_daily.rust_path_disabled"]
        assert disabled[0]["error_detail"] == (
            "brutex_lake.resample_daily_ipc missing or not callable — foreign/stale install"
        )
        # The not-callable arm's remedy pinned too (mutmut zero-survivor law).
        assert disabled[0]["remedy"] == remedy

    def test_budget_arm_alone_triggers_burn_in(self, monkeypatch: pytest.MonkeyPatch) -> None:
        lrd.reset_lake_rust_resample_daily_for_tests(remaining=1)
        lrd._BURN_IN_SHAPE_GATE[0] = False
        oracle = _CountingOracle()
        _install_crate(monkeypatch, _fake_crate())
        lrd.resample_daily_frame_via_rust(_df_1m(), _BEFORE, oi_aware=True, oracle=oracle)
        assert oracle.calls == 1
        assert lrd._BURN_IN_REMAINING[0] == 0

    def test_spent_budget_never_decrements_below_zero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lrd.reset_lake_rust_resample_daily_for_tests(remaining=1)
        _install_crate(monkeypatch, _fake_crate())
        lrd.resample_daily_frame_via_rust(
            _df_1m(), _BEFORE, oi_aware=True, oracle=_CountingOracle()
        )
        assert lrd._BURN_IN_REMAINING[0] == 0
        lrd.resample_daily_frame_via_rust(
            _df_1m(), _BEFORE, oi_aware=False, oracle=_CountingOracle()
        )
        assert lrd._BURN_IN_REMAINING[0] == 0

    def test_source_literal_pins(self) -> None:
        from pathlib import Path

        source = (
            Path(__file__).resolve().parents[2] / "brutex" / "data" / "lake_rust_resample_daily.py"
        ).read_text()
        for literal in (
            '_LOG = structlog.get_logger("brutex.data.lake_rust_resample_daily")',
            '_ENGAGED_KEY: Final[str] = "__engaged__"',
            "_BURN_IN_SHAPE_GATE: Final[list[bool]] = [True]",
            "_DISABLED_REASON: Final[list[str | None]] = [None]",
            '    schema_token = f"{df_1m.schema}|daily|oi_aware={oi_aware}"',
            "_EPOCH: Final[date] = date(1970, 1, 1)",
        ):
            assert literal in source, f"pinned literal drifted: {literal!r}"


class TestMutationWorkflowWiring:
    """Pin this module INTO ``mutation.yml``'s per-module zero-survivor loop.

    The ``TestMutationWorkflowWiring`` precedent (#1614/#1797 pattern): the
    module:suite pair sits in the workflow's per-module ``mutmut`` loop with
    the EMPTY third (timeout) field — this deterministic pin fails if the
    pair is ever dropped. HONEST: it pins the WIRING only; the weekly run
    executes on its own schedule (DEC-LAKE-RUST-RESAMPLE-DAILY-001).
    """

    _WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "mutation.yml"

    def test_module_paired_with_this_suite_in_the_mutmut_loop(self) -> None:
        text = self._WORKFLOW.read_text(encoding="utf-8")
        pair = (
            '"brutex/data/lake_rust_resample_daily.py:tests/unit/test_lake_rust_resample_daily.py:"'
        )
        assert pair in text, (
            "lake_rust_resample_daily.py is not wired into mutation.yml's per-module mutmut loop"
        )
