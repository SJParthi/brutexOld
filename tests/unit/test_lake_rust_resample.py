"""Unit suite for ``brutex.data.lake_rust_resample`` — 100% line+branch standalone.

Every test injects a FAKE ``brutex_lake`` module straight into
``sys.modules`` (or a poisoned ``None`` entry for the import-guard arm), so
the suite runs with NO wheel installed — the real-wheel parity corpus lives
in ``tests/integration/test_lake_rust_resample_parity.py``. The harness
mirrors ``tests/unit/test_lake_rust_render.py`` (the PR-1 facade suite)
section-for-section; the comparator here is FRAME-strength (dtype-exact —
schema equality AND ``.equals``) instead of byte-strength.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import io
import sys
import types
from typing import TYPE_CHECKING, Any

import polars as pl
import pytest
from brutex.data import lake_rust_render as lrr
from brutex.data import lake_rust_resample as lrs
from brutex.data.models import GREEKS_OVERLAY_FIELDS
from structlog.testing import capture_logs

if TYPE_CHECKING:
    from collections.abc import Iterator

# ---------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _fresh_process_view() -> Iterator[None]:
    """Reset the latch + burn-in budget + warn memo around every test."""
    lrs.reset_lake_rust_resample_for_tests()
    yield
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


#: The frame the ORACLE stub returns — any recognisable frame works; the
#: seam never inspects it beyond the burn-in comparison.
_ORACLE_DF = pl.DataFrame({"ts_utc": [1], "close": [105.0]})

#: A value-DIFFERENT frame (burn-in value-mismatch arm).
_WRONG_DF = pl.DataFrame({"ts_utc": [1], "close": [999.0]})

#: A value-IDENTICAL, dtype-PUNNED frame (Float32 close) — ``.equals`` alone
#: calls it equal (#1795b), the dtype-exact comparator must NOT.
_PUN_DF = _ORACLE_DF.with_columns(pl.col("close").cast(pl.Float32))


def _ipc(df: pl.DataFrame) -> bytes:
    buf = io.BytesIO()
    df.write_ipc(buf, compression="uncompressed")
    return buf.getvalue()


def _fake_crate(
    *,
    pinned: Any = pl.__version__,
    resample: Any = None,
    zstd: Any = None,
) -> types.ModuleType:
    """Build a fake ``brutex_lake`` module with injectable behaviour."""
    module = types.ModuleType("brutex_lake")
    module.pinned_polars_version = (  # type: ignore[attr-defined]
        pinned if callable(pinned) else (lambda: pinned)
    )
    if zstd is None:
        zstd = lambda: "1.5.7"  # noqa: E731 - test stub
    module.linked_zstd_version = zstd  # type: ignore[attr-defined]
    if resample is None:
        resample = lambda ipc, tf, oi: _ipc(_ORACLE_DF)  # noqa: E731 - test stub
    module.resample_1m_ipc = resample  # type: ignore[attr-defined]
    module.resample_all_ipc = lambda ipc, tfs, oi: []  # type: ignore[attr-defined]
    # The sibling faces (DEC-LAKE-RUST-RESAMPLE-SIBLINGS-001) — a real wheel
    # ships the WHOLE _CRATE_API surface, so the fake must too (the import
    # guard latches crate_unavailable on ANY missing member by design).
    module.daily_restamp_ipc = lambda ipc, before_days, keep_oi: _ipc(  # type: ignore[attr-defined]
        _ORACLE_DF
    )
    module.session_cutoff_ipc = lambda ipc, tf, cutoff: _ipc(  # type: ignore[attr-defined]
        _ORACLE_DF
    )
    return module


def _install_crate(monkeypatch: pytest.MonkeyPatch, module: types.ModuleType | None) -> None:
    """Install a fake crate (or a poisoned ``None`` → ImportError) in sys.modules."""
    monkeypatch.setitem(sys.modules, "brutex_lake", module)


class _CountingOracle:
    """Oracle stub counting invocations (returns :data:`_ORACLE_DF`)."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, df_1m: Any, target_tf_minutes: int) -> pl.DataFrame:
        self.calls += 1
        return _ORACLE_DF


def _run(monkeypatch: pytest.MonkeyPatch, module: types.ModuleType | None) -> pl.DataFrame:
    _install_crate(monkeypatch, module)
    return lrs.resample_frame_via_rust(_df_1m(), 5, oracle=_CountingOracle())


# ---------------------------------------------------------------------------
# flag parsing
# ---------------------------------------------------------------------------


class TestFlagParsing:
    @pytest.mark.parametrize("raw", ["1", "true", "YES", " on "])
    def test_truthy_literals_enable(self, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
        monkeypatch.setenv(lrs.LAKE_RUST_RESAMPLE_ENV, raw)
        assert lrs.lake_rust_resample_enabled() is True

    @pytest.mark.parametrize("raw", ["0", "false", "NO", " off "])
    def test_falsy_literals_disable_silently(
        self, monkeypatch: pytest.MonkeyPatch, raw: str
    ) -> None:
        monkeypatch.setenv(lrs.LAKE_RUST_RESAMPLE_ENV, raw)
        with capture_logs() as logs:
            assert lrs.lake_rust_resample_enabled() is False
        assert logs == []

    def test_unset_is_the_silent_default_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(lrs.LAKE_RUST_RESAMPLE_ENV, raising=False)
        with capture_logs() as logs:
            assert lrs.lake_rust_resample_enabled() is False
        assert logs == []

    def test_whitespace_is_the_silent_default_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(lrs.LAKE_RUST_RESAMPLE_ENV, "   ")
        with capture_logs() as logs:
            assert lrs.lake_rust_resample_enabled() is False
        assert logs == []

    def test_junk_is_loud_off_warned_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(lrs.LAKE_RUST_RESAMPLE_ENV, "onn")
        with capture_logs() as logs:
            assert lrs.lake_rust_resample_enabled() is False
            assert lrs.lake_rust_resample_enabled() is False
        warns = [e for e in logs if e["event"] == "lake_rust_resample.fallback"]
        assert len(warns) == 1
        assert warns[0]["reason"] == "flag_unrecognised"

    def test_tristate_semantics_equal_the_render_facade_twin(self) -> None:
        # The LOCAL-twin equality pin (module docstring): same vocabulary as
        # the PR-1 facade's copy, byte-for-byte semantics.
        corpus = ["1", "true", "YES", " on ", "0", "false", "NO", " off ", "", " ", "junk", "2"]
        for raw in corpus:
            assert lrs.env_flag_tristate(raw) == lrr.env_flag_tristate(raw), raw


# ---------------------------------------------------------------------------
# latch / reset
# ---------------------------------------------------------------------------


class TestLatchAndReset:
    def test_disabled_short_circuits_before_any_crate_touch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _bomb(*_args: object) -> bytes:
            raise AssertionError("crate must not be touched once latched")

        lrs._disable("test_latch")
        assert lrs.rust_resample_disabled() is True
        oracle = _CountingOracle()
        _install_crate(monkeypatch, _fake_crate(resample=_bomb))
        out = lrs.resample_frame_via_rust(_df_1m(), 5, oracle=oracle)
        assert out.equals(_ORACLE_DF)
        assert oracle.calls == 1

    def test_repeat_disable_is_a_noop_keeping_the_first_reason(self) -> None:
        lrs._disable("first")
        with capture_logs() as logs:
            lrs._disable("second")
        assert logs == []
        assert lrs._DISABLED_REASON[0] == "first"

    def test_reset_restores_a_fresh_view(self) -> None:
        lrs._disable("test_latch")
        lrs._BURN_IN_REMAINING[0] = 0
        lrs.reset_lake_rust_resample_for_tests()
        assert lrs.rust_resample_disabled() is False
        assert lrs._BURN_IN_REMAINING[0] == lrs.BURN_IN_SAMPLES
        assert lrs._BURN_IN_SHAPE_GATE[0] is True

    def test_reset_remaining_zero_is_the_steady_state(self) -> None:
        lrs.reset_lake_rust_resample_for_tests(remaining=0)
        assert lrs._BURN_IN_REMAINING[0] == 0
        assert lrs._BURN_IN_SHAPE_GATE[0] is False


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
        assert lrs._DISABLED_REASON[0] == "crate_unavailable"
        events = [e["event"] for e in logs]
        assert "lake_rust_resample.rust_path_disabled" in events

    def test_pre_wave_b_wheel_missing_the_resample_face_latches(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A PR-1 era wheel imports fine but lacks resample_1m_ipc — the API
        # guard must latch crate_unavailable, never AttributeError mid-derive.
        module = _fake_crate()
        delattr(module, "resample_all_ipc")
        out = _run(monkeypatch, module)
        assert out.equals(_ORACLE_DF)
        assert lrs._DISABLED_REASON[0] == "crate_unavailable"

    def test_non_callable_api_member_latches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        module = _fake_crate()
        module.resample_1m_ipc = "not-callable"  # type: ignore[attr-defined]
        out = _run(monkeypatch, module)
        assert out.equals(_ORACLE_DF)
        assert lrs._DISABLED_REASON[0] == "crate_unavailable"


# ---------------------------------------------------------------------------
# version / metadata guards
# ---------------------------------------------------------------------------


class TestVersionGuard:
    def test_version_mismatch_latches_naming_both(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with capture_logs() as logs:
            out = _run(monkeypatch, _fake_crate(pinned="9.99.9"))
        assert out.equals(_ORACLE_DF)
        assert lrs._DISABLED_REASON[0] == "polars_version_mismatch"
        disabled = [e for e in logs if e["event"] == "lake_rust_resample.rust_path_disabled"]
        assert disabled[0]["installed_polars"] == pl.__version__
        assert disabled[0]["crate_pinned_polars"] == "9.99.9"

    def test_matching_version_engages(self, monkeypatch: pytest.MonkeyPatch) -> None:
        lrs.reset_lake_rust_resample_for_tests(remaining=0)
        out = _run(monkeypatch, _fake_crate())
        assert out.equals(_ORACLE_DF)
        assert lrs.rust_resample_disabled() is False


class TestMetadataGuard:
    def test_raising_pinned_version_latches_metadata_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom() -> str:
            raise RuntimeError("stale install")

        out = _run(monkeypatch, _fake_crate(pinned=_boom))
        assert out.equals(_ORACLE_DF)
        assert lrs._DISABLED_REASON[0] == "crate_metadata_error"

    def test_memoryerror_in_metadata_re_raises_raw(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _oom() -> str:
            raise MemoryError

        _install_crate(monkeypatch, _fake_crate(pinned=_oom))
        with pytest.raises(MemoryError):
            lrs.resample_frame_via_rust(_df_1m(), 5, oracle=_CountingOracle())

    def test_raising_zstd_metadata_on_the_engaged_path_latches(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom() -> str:
            raise ValueError("no fingerprint")

        out = _run(monkeypatch, _fake_crate(zstd=_boom))
        assert out.equals(_ORACLE_DF)
        assert lrs._DISABLED_REASON[0] == "crate_metadata_error"


# ---------------------------------------------------------------------------
# burn-in self-check
# ---------------------------------------------------------------------------


class TestBurnIn:
    def test_matching_frames_serve_rust_and_decrement_budget(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        before = lrs._BURN_IN_REMAINING[0]
        out = _run(monkeypatch, _fake_crate())
        assert out.equals(_ORACLE_DF)
        assert lrs._BURN_IN_REMAINING[0] == before - 1
        assert lrs.rust_resample_disabled() is False

    def test_value_mismatch_autoreverts_and_ships_the_oracle_frame(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with capture_logs() as logs:
            out = _run(monkeypatch, _fake_crate(resample=lambda i, t, o: _ipc(_WRONG_DF)))
        assert out.equals(_ORACLE_DF)
        assert lrs._DISABLED_REASON[0] == "burn_in_frame_mismatch"
        errors = [e for e in logs if e["event"] == "lake_rust_resample.autoreverted"]
        assert len(errors) == 1

    def test_dtype_pun_mismatch_autoreverts_despite_equals_true(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The #1795b tooth: .equals is dtype-INSENSITIVE, so a Float32 pun
        # compares equal by value — the dtype-exact comparator must refuse.
        assert _PUN_DF.equals(_ORACLE_DF)
        out = _run(monkeypatch, _fake_crate(resample=lambda i, t, o: _ipc(_PUN_DF)))
        assert out.equals(_ORACLE_DF)
        assert out.schema == _ORACLE_DF.schema
        assert lrs._DISABLED_REASON[0] == "burn_in_frame_mismatch"

    def test_burn_in_complete_logs_once_and_enters_steady_state(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lrs.reset_lake_rust_resample_for_tests(remaining=1)
        lrs._BURN_IN_SHAPE_GATE[0] = True
        _install_crate(monkeypatch, _fake_crate())
        with capture_logs() as logs:
            lrs.resample_frame_via_rust(_df_1m(), 5, oracle=_CountingOracle())
        assert lrs._BURN_IN_REMAINING[0] == 0
        events = [e["event"] for e in logs]
        assert "lake_rust_resample.burn_in_complete" in events

    def test_steady_state_serves_rust_with_no_oracle_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lrs.reset_lake_rust_resample_for_tests(remaining=0)
        oracle = _CountingOracle()
        _install_crate(monkeypatch, _fake_crate())
        out = lrs.resample_frame_via_rust(_df_1m(), 5, oracle=oracle)
        assert out.equals(_ORACLE_DF)
        assert oracle.calls == 0

    def test_engaged_info_fires_once_per_process(self, monkeypatch: pytest.MonkeyPatch) -> None:
        zstd_calls = [0]

        def _zstd() -> str:
            zstd_calls[0] += 1
            return "1.5.7"

        _install_crate(monkeypatch, _fake_crate(zstd=_zstd))
        with capture_logs() as logs:
            lrs.resample_frame_via_rust(_df_1m(), 5, oracle=_CountingOracle())
            lrs.resample_frame_via_rust(_df_1m(), 5, oracle=_CountingOracle())
        engaged = [e for e in logs if e["event"] == "lake_rust_resample.engaged"]
        assert len(engaged) == 1
        assert zstd_calls[0] == 1


class TestShapeGate:
    def test_unseen_shape_after_budget_spent_still_burns_in(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lrs.reset_lake_rust_resample_for_tests(remaining=1)
        oracle = _CountingOracle()
        _install_crate(monkeypatch, _fake_crate())
        lrs.resample_frame_via_rust(_df_1m(), 5, oracle=oracle)  # spends the budget
        assert lrs._BURN_IN_REMAINING[0] == 0
        assert oracle.calls == 1
        # A DIFFERENT tf is a new schema×tf token — the shape gate compares.
        lrs.resample_frame_via_rust(_df_1m(), 15, oracle=oracle)
        assert oracle.calls == 2
        # The SAME token again is steady state — no oracle.
        lrs.resample_frame_via_rust(_df_1m(), 15, oracle=oracle)
        assert oracle.calls == 2

    def test_shape_gate_mismatch_autoreverts_after_budget_spent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lrs.reset_lake_rust_resample_for_tests(remaining=1)
        crate = _fake_crate()
        _install_crate(monkeypatch, crate)
        lrs.resample_frame_via_rust(_df_1m(), 5, oracle=_CountingOracle())
        crate.resample_1m_ipc = lambda i, t, o: _ipc(_WRONG_DF)  # type: ignore[attr-defined]
        out = lrs.resample_frame_via_rust(_df_1m(), 15, oracle=_CountingOracle())
        assert out.equals(_ORACLE_DF)
        assert lrs._DISABLED_REASON[0] == "burn_in_frame_mismatch"


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
            OverflowError("tf too large"),
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
            out = _run(monkeypatch, _fake_crate(resample=_raise))
        assert out.equals(_ORACLE_DF)
        assert lrs.rust_resample_disabled() is False
        warns = [e for e in logs if e["event"] == "lake_rust_resample.fallback"]
        assert warns[0]["reason"] == "resample_error"

    @pytest.mark.parametrize("exc_type", [KeyboardInterrupt, SystemExit, MemoryError])
    def test_escape_set_re_raises_raw(
        self, monkeypatch: pytest.MonkeyPatch, exc_type: type[BaseException]
    ) -> None:
        def _raise(*_args: object) -> bytes:
            raise exc_type

        _install_crate(monkeypatch, _fake_crate(resample=_raise))
        with pytest.raises(exc_type):
            lrs.resample_frame_via_rust(_df_1m(), 5, oracle=_CountingOracle())

    def test_non_bytes_result_is_refuse_poison(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with capture_logs() as logs:
            out = _run(monkeypatch, _fake_crate(resample=lambda i, t, o: 12345))
        assert out.equals(_ORACLE_DF)
        assert lrs.rust_resample_disabled() is False
        warns = [e for e in logs if e["event"] == "lake_rust_resample.fallback"]
        assert warns[0]["reason"] == "resample_result_refused"
        assert warns[0]["result_type"] == "int"

    def test_undecodable_result_bytes_degrade_to_the_oracle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with capture_logs() as logs:
            out = _run(monkeypatch, _fake_crate(resample=lambda i, t, o: b"garbage"))
        assert out.equals(_ORACLE_DF)
        warns = [e for e in logs if e["event"] == "lake_rust_resample.fallback"]
        assert warns[0]["reason"] == "result_decode_error"

    def test_degrade_errors_without_polars_exceptions_is_the_base_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(sys.modules, "polars.exceptions", None)
        errors = lrs._degrade_errors()
        assert errors == (ValueError, RuntimeError, TypeError, OverflowError, AttributeError)


# ---------------------------------------------------------------------------
# panic belt
# ---------------------------------------------------------------------------


class _FakePanicException(BaseException):
    """Stand-in for pyo3_runtime.PanicException (a BaseException subclass)."""


def _install_panic_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    module = types.ModuleType("pyo3_runtime")
    module.PanicException = _FakePanicException  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pyo3_runtime", module)


class TestPanicBelt:
    def test_escaped_panic_latches_crate_panic_and_serves_the_oracle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_panic_runtime(monkeypatch)

        def _panic(*_args: object) -> bytes:
            raise _FakePanicException("thread panicked")

        out = _run(monkeypatch, _fake_crate(resample=_panic))
        assert out.equals(_ORACLE_DF)
        assert lrs._DISABLED_REASON[0] == "crate_panic"

    def test_out_of_degrade_set_exception_latches_via_the_belt(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _weird(*_args: object) -> bytes:
            raise KeyError("not in the degrade set")

        out = _run(monkeypatch, _fake_crate(resample=_weird))
        assert out.equals(_ORACLE_DF)
        assert lrs._DISABLED_REASON[0] == "crate_panic"


class TestPanicBeltResolver:
    def test_both_homes_unimportable_yields_exception_only(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(sys.modules, "pyo3_runtime", None)
        monkeypatch.setitem(sys.modules, "polars.exceptions", None)
        assert lrs._panic_belt_errors() == (Exception,)

    def test_resolved_panic_class_joins_the_belt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_panic_runtime(monkeypatch)
        monkeypatch.setitem(sys.modules, "polars.exceptions", None)
        assert lrs._panic_belt_errors() == (Exception, _FakePanicException)

    def test_module_without_the_attr_is_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "pyo3_runtime", types.ModuleType("pyo3_runtime"))
        monkeypatch.setitem(sys.modules, "polars.exceptions", None)
        assert lrs._panic_belt_errors() == (Exception,)

    def test_non_class_attr_is_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        module = types.ModuleType("pyo3_runtime")
        module.PanicException = "not-a-class"  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "pyo3_runtime", module)
        monkeypatch.setitem(sys.modules, "polars.exceptions", None)
        assert lrs._panic_belt_errors() == (Exception,)

    def test_duplicate_class_across_homes_folds_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_panic_runtime(monkeypatch)
        fake_pl = types.ModuleType("polars.exceptions")
        fake_pl.PanicException = _FakePanicException  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "polars.exceptions", fake_pl)
        assert lrs._panic_belt_errors() == (Exception, _FakePanicException)


# ---------------------------------------------------------------------------
# dtype-exact comparator
# ---------------------------------------------------------------------------


class TestFrameComparator:
    def test_identical_frames_compare_equal(self) -> None:
        assert lrs._frames_equal_dtype_exact(_ORACLE_DF, _ORACLE_DF.clone()) is True

    def test_value_difference_compares_unequal(self) -> None:
        assert lrs._frames_equal_dtype_exact(_ORACLE_DF, _WRONG_DF) is False

    def test_dtype_pun_compares_unequal_despite_equals(self) -> None:
        assert _PUN_DF.equals(_ORACLE_DF)
        assert lrs._frames_equal_dtype_exact(_PUN_DF, _ORACLE_DF) is False


# ---------------------------------------------------------------------------
# cross-language drift pins
# ---------------------------------------------------------------------------


class TestCrateSourceDriftPins:
    """Source-level pins holding the Rust constants to their Python twins."""

    def _crate_source(self) -> str:
        from pathlib import Path

        root = Path(__file__).resolve().parents[2]
        return (root / "rust" / "brutex-lake" / "src" / "resample.rs").read_text()

    def test_greeks_overlay_fields_order_matches_models_py(self) -> None:
        # The crate's carry-last append order must be the Python tuple's
        # order EXACTLY (output column order is part of frame equality).
        source = self._crate_source()
        block = source.split("GREEKS_OVERLAY_FIELDS: [&str; 10] = [", 1)[1].split("];", 1)[0]
        rust_fields = [line.strip().strip('",') for line in block.splitlines() if '"' in line]
        rust_fields = [f.lstrip('"') for f in rust_fields]
        assert tuple(rust_fields) == GREEKS_OVERLAY_FIELDS

    def test_crate_api_names_are_registered_in_the_shim(self) -> None:
        from pathlib import Path

        root = Path(__file__).resolve().parents[2]
        shim = (
            root / "rust" / "brutex-lake" / "python" / "brutex_lake" / "__init__.py"
        ).read_text()
        for name in lrs._CRATE_API:
            assert name in shim, f"{name} missing from the brutex_lake shim re-exports"


# ---------------------------------------------------------------------------
# mutation hardening (the zero-survivor weekly-gate law — each test kills a
# mutant class the behavioral suite above left alive; mutmut-executed)
# ---------------------------------------------------------------------------


class TestMutationHardening:
    def test_flag_env_name_literal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Kills the env-NAME string mutant: the literal (not the constant)
        # must arm the flag, or every operator opt-in silently dies.
        assert lrs.LAKE_RUST_RESAMPLE_ENV == "BRUTEX_LAKE_RUST_RESAMPLE"
        monkeypatch.setenv("BRUTEX_LAKE_RUST_RESAMPLE", "1")
        assert lrs.lake_rust_resample_enabled() is True

    def test_burn_in_samples_is_sixteen(self) -> None:
        # The #1531/#1532 16-sample law (kills the 16->17 mutant).
        assert lrs.BURN_IN_SAMPLES == 16

    def test_import_guard_remedy_and_detail_are_exact(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        remedy = "pip install rust/brutex-lake (see docs/runbooks/lake-rust-render-lockstep.md)"
        with capture_logs() as logs:
            _run(monkeypatch, None)
        disabled = [e for e in logs if e["event"] == "lake_rust_resample.rust_path_disabled"]
        assert disabled[0]["remedy"] == remedy
        lrs.reset_lake_rust_resample_for_tests()
        module = _fake_crate()
        module.resample_1m_ipc = "not-callable"  # type: ignore[attr-defined]
        with capture_logs() as logs:
            _run(monkeypatch, module)
        disabled = [e for e in logs if e["event"] == "lake_rust_resample.rust_path_disabled"]
        assert disabled[0]["remedy"] == remedy
        assert disabled[0]["error_detail"] == (
            "brutex_lake.resample_1m_ipc missing or not callable — foreign/stale install"
        )

    def test_version_mismatch_remedy_is_exact(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with capture_logs() as logs:
            _run(monkeypatch, _fake_crate(pinned="9.99.9"))
        disabled = [e for e in logs if e["event"] == "lake_rust_resample.rust_path_disabled"]
        assert disabled[0]["remedy"] == (
            "align the wheel and the crate's pinned polars rev, then "
            "re-run the parity corpus "
            "(docs/runbooks/lake-rust-render-lockstep.md)"
        )

    def test_none_returning_pinned_version_fails_the_version_guard(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The documented fail-closed law: a non-str (even None) pinned
        # version flows into `!=` and latches polars_version_mismatch —
        # kills the `_METADATA_ERROR = None` sentinel mutant, under which a
        # None-returning foreign crate would degrade SILENTLY, unlatched.
        out = _run(monkeypatch, _fake_crate(pinned=lambda: None))
        assert out.equals(_ORACLE_DF)
        assert lrs._DISABLED_REASON[0] == "polars_version_mismatch"

    def test_polars_exceptions_home_resolves_alone(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # pyo3_runtime unimportable + ONLY polars.exceptions carrying the
        # class: the resolver must CONTINUE past the failed first home
        # (kills the continue->break mutant AND the home-name string mutant).
        monkeypatch.setitem(sys.modules, "pyo3_runtime", None)
        fake_pl = types.ModuleType("polars.exceptions")
        fake_pl.PanicException = _FakePanicException  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "polars.exceptions", fake_pl)
        assert lrs._panic_belt_errors() == (Exception, _FakePanicException)

    def test_crate_is_called_with_oi_aware_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The presence-decides law rides oi_aware=True ALWAYS (kills the
        # True->False mutant, under which every F&O derive would silently
        # drop OI + the Greeks overlay).
        seen: list[tuple[int, bool]] = []

        def _spy(ipc: bytes, tf: int, oi: bool) -> bytes:
            seen.append((tf, oi))
            return _ipc(_ORACLE_DF)

        _install_crate(monkeypatch, _fake_crate(resample=_spy))
        lrs.resample_frame_via_rust(_df_1m(), 5, oracle=_CountingOracle())
        assert seen == [(5, True)]

    def test_budget_arm_alone_triggers_burn_in(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # remaining=1 with the shape gate DISARMED: only the count-budget
        # arm can enter the burn-in (kills the `> 0` -> `> 1` mutant).
        lrs.reset_lake_rust_resample_for_tests(remaining=1)
        lrs._BURN_IN_SHAPE_GATE[0] = False
        oracle = _CountingOracle()
        _install_crate(monkeypatch, _fake_crate())
        lrs.resample_frame_via_rust(_df_1m(), 5, oracle=oracle)
        assert oracle.calls == 1
        assert lrs._BURN_IN_REMAINING[0] == 0

    def test_spent_budget_never_decrements_below_zero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A shape-gate compare after the budget is spent must NOT touch the
        # count (kills the `> 0` -> `>= 0` decrement-guard mutant).
        lrs.reset_lake_rust_resample_for_tests(remaining=1)
        _install_crate(monkeypatch, _fake_crate())
        lrs.resample_frame_via_rust(_df_1m(), 5, oracle=_CountingOracle())
        assert lrs._BURN_IN_REMAINING[0] == 0
        lrs.resample_frame_via_rust(_df_1m(), 15, oracle=_CountingOracle())
        assert lrs._BURN_IN_REMAINING[0] == 0

    def test_source_literal_pins(self) -> None:
        # Source-text pins for the remaining string/annotation/default
        # mutants whose effect is internally-consistent state the behavioral
        # suite cannot distinguish (logger name · engaged-memo key · shape
        # gate armed default · latch cell annotation · the schema-token
        # shape). mutmut mutates the file ON DISK, so a literal pin is a
        # genuine kill (the killed-token/AST-pin house idiom).
        from pathlib import Path

        source = (
            Path(__file__).resolve().parents[2] / "brutex" / "data" / "lake_rust_resample.py"
        ).read_text()
        for literal in (
            '_LOG = structlog.get_logger("brutex.data.lake_rust_resample")',
            '_ENGAGED_KEY: Final[str] = "__engaged__"',
            "_BURN_IN_SHAPE_GATE: Final[list[bool]] = [True]",
            "_DISABLED_REASON: Final[list[str | None]] = [None]",
            '    schema_token = f"{df_1m.schema}|tf={target_tf_minutes}"',
        ):
            assert literal in source, f"pinned literal drifted: {literal!r}"
