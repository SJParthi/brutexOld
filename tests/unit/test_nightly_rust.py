# pyright: reportPrivateUsage=none
"""Unit matrix for :mod:`brutex.nightly.nightly_rust` (DEC-NIGHTLY-RUST-CORE-001).

Flag parsing / import guard / per-kernel burn-in autorevert / degrade set /
panic belt / refuse-poison / reset seam — every facade path exercised
WITHOUT the wheel (fake crate modules injected via ``sys.modules``, the
``test_lake_rust_stats.py`` idiom), so the 100% line+branch gate holds on
wheel-less CI. The seam-site routing (chain/report/schedule flag-ON parity
+ the OFF-arm bomb-spy byte-identity pins) lives in the stem-matched
``test_nightly_{chain,report,schedule}_rust_seam.py`` suites.
"""

from __future__ import annotations

import sys
import types
from typing import TYPE_CHECKING

import pytest
from brutex.data import lake_rust_render
from brutex.nightly import nightly_rust
from structlog.testing import capture_logs

from tests.fakes.fake_brutex_ops import build_fake_crate

if TYPE_CHECKING:
    from collections.abc import Iterator


def _route(
    crate_result: object = "RUST",
    *,
    python_body: str = "PY",
    validate: object = None,
) -> object:
    """Drive ``route_kernel`` with a trivially-shaped kernel."""
    return nightly_rust.route_kernel(
        "unit_kernel",
        crate_call=(crate_result if callable(crate_result) else (lambda _crate: crate_result)),
        python_call=lambda: python_body,
        validate=nightly_rust.is_str if validate is None else validate,  # type: ignore[arg-type]
    )


@pytest.fixture(autouse=True)
def _fresh_seam(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Test isolation: env unset + latch/burn-in/memo reset both sides."""
    monkeypatch.delenv(nightly_rust.NIGHTLY_RUST_ENV, raising=False)
    monkeypatch.setitem(sys.modules, "brutex_ops", build_fake_crate())
    nightly_rust.reset_nightly_rust_for_tests()
    yield
    nightly_rust.reset_nightly_rust_for_tests()


class TestFlagParsing:
    @pytest.mark.parametrize("raw", ["1", "true", "yes", "on", " TRUE "])
    def test_truthy_literals_enable(self, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
        monkeypatch.setenv(nightly_rust.NIGHTLY_RUST_ENV, raw)
        assert nightly_rust.nightly_rust_enabled() is True

    @pytest.mark.parametrize("raw", ["0", "false", "no", "off"])
    def test_falsy_literals_disable_silently(
        self, monkeypatch: pytest.MonkeyPatch, raw: str
    ) -> None:
        monkeypatch.setenv(nightly_rust.NIGHTLY_RUST_ENV, raw)
        with capture_logs() as logs:
            assert nightly_rust.nightly_rust_enabled() is False
        assert logs == []

    def test_unset_and_whitespace_default_off_silently(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with capture_logs() as logs:
            assert nightly_rust.nightly_rust_enabled() is False
        monkeypatch.setenv(nightly_rust.NIGHTLY_RUST_ENV, "   ")
        with capture_logs() as more:
            assert nightly_rust.nightly_rust_enabled() is False
        assert logs == [] and more == []

    def test_junk_fails_loud_off_and_warns_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(nightly_rust.NIGHTLY_RUST_ENV, "onn")
        with capture_logs() as logs:
            assert nightly_rust.nightly_rust_enabled() is False
            assert nightly_rust.nightly_rust_enabled() is False
        warns = [entry for entry in logs if entry["event"] == "nightly_rust.fallback"]
        assert len(warns) == 1
        assert warns[0]["reason"] == "flag_unrecognised"
        assert warns[0]["value"] == "onn"

    def test_flag_vocabulary_is_the_shared_tristate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The flag law is IMPORTED from lake_rust_render, never copied."""
        assert nightly_rust.env_flag_tristate is lake_rust_render.env_flag_tristate
        monkeypatch.setenv(nightly_rust.NIGHTLY_RUST_ENV, "  yes  ")
        assert nightly_rust.nightly_rust_enabled() is True

    def test_env_name_burn_in_and_crate_api_pinned(self) -> None:
        assert nightly_rust.NIGHTLY_RUST_ENV == "BRUTEX_NIGHTLY_RUST"
        assert nightly_rust.BURN_IN_SAMPLES == 16
        assert nightly_rust._CRATE_API == (
            "nightly_first_breaker_index",
            "nightly_exit_code",
            "nightly_audit_reconcile_detail",
            "nightly_count_text",
            "nightly_verdict",
            "nightly_summary_lines",
            "nightly_xml_escape",
            "nightly_render_launchd_plist",
            "nightly_parse_at",
            "nightly_pmset_wake_command",
            "nightly_seconds_since_fire",
        )


class TestResetSeam:
    def test_reset_restores_fresh_process_view(self) -> None:
        nightly_rust._disable("poison")
        nightly_rust._warn_once("stale")
        nightly_rust._BURN_IN_REMAINING["unit_kernel"] = 3
        nightly_rust.reset_nightly_rust_for_tests()
        assert nightly_rust.nightly_rust_disabled() is False
        assert nightly_rust._BURN_IN_REMAINING == {}
        assert nightly_rust._BURN_IN_DEFAULT[0] == nightly_rust.BURN_IN_SAMPLES
        assert set() == nightly_rust._OBSERVABILITY_MEMO

    def test_reset_remaining_zero_is_steady_state(self) -> None:
        nightly_rust.reset_nightly_rust_for_tests(remaining=0)
        with capture_logs():
            assert _route("RUST") == "RUST"
        # Steady state: no oracle in the path, the kernel never burned in.
        assert nightly_rust._BURN_IN_REMAINING["unit_kernel"] == 0

    def test_warn_once_suppresses_repeats(self) -> None:
        with capture_logs() as logs:
            nightly_rust._warn_once("reason_x", detail=1)
            nightly_rust._warn_once("reason_x", detail=2)
        assert len(logs) == 1
        assert logs[0]["event"] == "nightly_rust.fallback"

    def test_disable_latches_once_and_repeat_is_noop(self) -> None:
        with capture_logs() as logs:
            nightly_rust._disable("first")
            nightly_rust._disable("second")
        assert nightly_rust._DISABLED_REASON[0] == "first"
        assert len(logs) == 1
        assert logs[0]["event"] == "nightly_rust.rust_path_disabled"

    def test_disable_error_logs_autoreverted(self) -> None:
        with capture_logs() as logs:
            nightly_rust._disable("mismatch", error=True)
        assert logs[0]["event"] == "nightly_rust.kernel_autoreverted"
        assert logs[0]["log_level"] == "error"


class TestImportGuard:
    def test_absent_crate_latches_and_serves_python(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "brutex_ops", None)
        with capture_logs() as logs:
            assert _route() == "PY"
        assert nightly_rust.nightly_rust_disabled() is True
        assert nightly_rust._DISABLED_REASON[0] == "crate_unavailable"
        assert any(entry["event"] == "nightly_rust.rust_path_disabled" for entry in logs)

    def test_module_missing_a_kernel_latches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        crate = build_fake_crate()
        delattr(crate, "nightly_parse_at")
        monkeypatch.setitem(sys.modules, "brutex_ops", crate)
        assert _route() == "PY"
        assert nightly_rust._DISABLED_REASON[0] == "crate_unavailable"

    def test_non_callable_kernel_latches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(
            sys.modules, "brutex_ops", build_fake_crate(nightly_exit_code="not-callable")
        )
        assert _route() == "PY"
        assert nightly_rust._DISABLED_REASON[0] == "crate_unavailable"

    def test_latched_process_short_circuits_without_reimport(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(sys.modules, "brutex_ops", None)
        assert _route() == "PY"

        def _bomb(_crate: types.ModuleType) -> str:
            raise AssertionError("a latched facade must never call the crate")

        assert _route(_bomb) == "PY"


class TestBurnIn:
    def test_burn_in_compares_and_completes(self) -> None:
        with capture_logs() as logs:
            for _ in range(nightly_rust.BURN_IN_SAMPLES):
                assert _route("PY") == "PY"  # crate == oracle → the crate value ships
            assert _route("PY") == "PY"  # steady state — no more oracle calls
        assert nightly_rust._BURN_IN_REMAINING["unit_kernel"] == 0
        completes = [entry for entry in logs if entry["event"] == "nightly_rust.burn_in_complete"]
        assert len(completes) == 1
        assert completes[0]["kernel"] == "unit_kernel"

    def test_burn_in_budgets_are_per_kernel(self) -> None:
        assert _route("PY") == "PY"
        assert nightly_rust._BURN_IN_REMAINING["unit_kernel"] == 15
        assert (
            nightly_rust.route_kernel(
                "other_kernel",
                crate_call=lambda _crate: 7,
                python_call=lambda: 7,
                validate=nightly_rust.is_int,
            )
            == 7
        )
        assert nightly_rust._BURN_IN_REMAINING["other_kernel"] == 15

    def test_burn_in_mismatch_ships_oracle_and_autoreverts(self) -> None:
        with capture_logs() as logs:
            assert _route("WRONG") == "PY"  # the ORACLE value ships
        assert nightly_rust.nightly_rust_disabled() is True
        assert nightly_rust._DISABLED_REASON[0] == "burn_in_mismatch"
        errors = [entry for entry in logs if entry["event"] == "nightly_rust.kernel_autoreverted"]
        assert len(errors) == 1
        assert errors[0]["kernel"] == "unit_kernel"
        # Every later call — ANY kernel — short-circuits to the oracle.
        assert (
            nightly_rust.route_kernel(
                "other_kernel",
                crate_call=lambda _crate: 1,
                python_call=lambda: 2,
                validate=nightly_rust.is_int,
            )
            == 2
        )

    def test_post_burn_in_divergence_is_not_caught(self) -> None:
        """HONEST envelope: the burn-in guards the first N calls per kernel."""
        nightly_rust.reset_nightly_rust_for_tests(remaining=0)
        assert _route("WRONG") == "WRONG"


class TestDegradeSet:
    @pytest.mark.parametrize(
        "exc",
        [
            ValueError("v"),
            RuntimeError("r"),
            TypeError("t"),
            OverflowError("o"),
            AttributeError("a"),
        ],
    )
    def test_degrade_errors_warn_once_and_serve_python(self, exc: Exception) -> None:
        def _raise(_crate: types.ModuleType) -> str:
            raise exc

        with capture_logs() as logs:
            assert _route(_raise) == "PY"
            assert _route(_raise) == "PY"
        warns = [entry for entry in logs if entry["event"] == "nightly_rust.fallback"]
        assert len(warns) == 1
        assert warns[0]["reason"] == "unit_kernel_error"
        assert nightly_rust.nightly_rust_disabled() is False  # degrade never latches

    def test_oracle_exception_propagates_untouched(self) -> None:
        """The refuse-to-guess channel: a crate refusal serves the oracle,
        whose OWN exception (the genuine Python law surface) propagates."""

        def _refuse(_crate: types.ModuleType) -> str:
            raise ValueError("non-ASCII --at input — python oracle decides")

        def _oracle() -> str:
            raise ValueError("--at must be HH:MM (24h), got 'junk'")

        with capture_logs(), pytest.raises(ValueError, match="--at must be HH:MM"):
            nightly_rust.route_kernel(
                "unit_kernel",
                crate_call=_refuse,
                python_call=_oracle,
                validate=nightly_rust.is_str,
            )

    @pytest.mark.parametrize("exc_type", [MemoryError, KeyboardInterrupt, SystemExit])
    def test_escape_set_reraises_raw(self, exc_type: type[BaseException]) -> None:
        def _raise(_crate: types.ModuleType) -> str:
            raise exc_type()

        with pytest.raises(exc_type):
            _route(_raise)
        assert nightly_rust.nightly_rust_disabled() is False

    def test_out_of_set_exception_takes_the_panic_belt(self) -> None:
        def _raise(_crate: types.ModuleType) -> str:
            raise KeyError("broken FFI contract")

        with capture_logs() as logs:
            assert _route(_raise) == "PY"
        assert nightly_rust._DISABLED_REASON[0] == "crate_panic"
        assert any(entry["event"] == "nightly_rust.rust_path_disabled" for entry in logs)

    def test_panic_belt_resolves_the_pyo3_exception_homes(self) -> None:
        belt = nightly_rust._panic_belt_errors()
        assert Exception in belt
        panic_exception: type[BaseException] | None
        try:
            from polars.exceptions import PanicException
        except ImportError:  # pragma: no cover - polars always in the test env
            panic_exception = None
        else:
            panic_exception = PanicException
        if panic_exception is not None:
            assert panic_exception in belt

    def test_panic_belt_skips_a_home_with_a_junk_panic_symbol(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A resolvable home whose ``PanicException`` is not a BaseException
        class is SKIPPED (the guarded-resolve law), never appended."""
        junk_home = types.ModuleType("pyo3_runtime")
        junk_home.PanicException = "not-a-class"  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "pyo3_runtime", junk_home)
        belt = nightly_rust._panic_belt_errors()
        assert "not-a-class" not in belt
        assert Exception in belt


class TestRefusePoison:
    def test_wrong_shape_serves_python_with_one_warn(self) -> None:
        with capture_logs() as logs:
            assert _route(b"bytes") == "PY"
            assert _route(b"bytes") == "PY"
        warns = [entry for entry in logs if entry["event"] == "nightly_rust.fallback"]
        assert len(warns) == 1
        assert warns[0]["reason"] == "unit_kernel_result_refused"
        assert warns[0]["result_type"] == "bytes"
        assert nightly_rust.nightly_rust_disabled() is False

    def test_engaged_info_logs_once_per_process(self) -> None:
        with capture_logs() as logs:
            assert _route("PY") == "PY"
            assert _route("PY") == "PY"
        engaged = [entry for entry in logs if entry["event"] == "nightly_rust.engaged"]
        assert len(engaged) == 1


class TestMutationWorkflowWiring:
    def test_facade_pair_is_wired_into_the_weekly_mutation_loop(self) -> None:
        """The module:suite pair rides mutation.yml's historical runner
        (the lake_rust_stats precedent — presence-pinned so the pair can
        never silently drop out of the weekly zero-survivor loop)."""
        from pathlib import Path

        workflow = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "mutation.yml"
        text = workflow.read_text(encoding="utf-8")
        assert '"brutex/nightly/nightly_rust.py:tests/unit/test_nightly_rust.py:"' in text, (
            "the nightly_rust mutation pair is no longer wired into mutation.yml"
        )


class TestShapeHelpers:
    def test_is_int_refuses_bool_and_non_ints(self) -> None:
        assert nightly_rust.is_int(3) is True
        assert nightly_rust.is_int(True) is False
        assert nightly_rust.is_int("3") is False

    def test_is_str_and_is_float(self) -> None:
        assert nightly_rust.is_str("x") is True
        assert nightly_rust.is_str(b"x") is False
        assert nightly_rust.is_float(0.5) is True
        assert nightly_rust.is_float(1) is False

    def test_is_summary_tuple(self) -> None:
        assert nightly_rust.is_summary_tuple((["a", "b"], "OK")) is True
        assert nightly_rust.is_summary_tuple(([], "OK")) is True
        assert nightly_rust.is_summary_tuple((["a"], b"OK")) is False
        assert nightly_rust.is_summary_tuple((["a", 1], "OK")) is False
        assert nightly_rust.is_summary_tuple(("a", "OK")) is False
        assert nightly_rust.is_summary_tuple((["a"], "OK", "extra")) is False
        assert nightly_rust.is_summary_tuple(["a", "OK"]) is False

    def test_is_hm_tuple_or_none(self) -> None:
        assert nightly_rust.is_hm_tuple_or_none(None) is True
        assert nightly_rust.is_hm_tuple_or_none((1, 30)) is True
        assert nightly_rust.is_hm_tuple_or_none((1,)) is False
        assert nightly_rust.is_hm_tuple_or_none((1, "30")) is False
        assert nightly_rust.is_hm_tuple_or_none((True, 30)) is False
        assert nightly_rust.is_hm_tuple_or_none([1, 30]) is False
