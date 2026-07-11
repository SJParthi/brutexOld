"""Tests for ``brutex.data.lake_rust_render`` — the ADR-0019 PR-1 facade.

DEC-RUST-DATALAKE-IO-ROADMAP-001 (MIGRATION.md #1726). Crate-FREE by design:
every test drives the facade with a FAKE ``brutex_lake`` module injected into
``sys.modules`` (or a poisoned ``None`` entry for the import-guard arm), so
the whole flag / import-guard / version-guard / burn-in / autorevert law is
proven without the compiled wheel — the real-crate byte parity lives in
``tests/golden/test_lake_rust_render_parity.py`` (auto-SKIPPED when the crate
is absent; the lake-rust-parity.yml lane runs it for real).

Whitebox: exercises the private latch/memo helpers directly (the DEC-P0-017
convention), so ``reportPrivateUsage`` is silenced at the top.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import sys
import types
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from brutex.data import lake_rust_render as lrr
from structlog.testing import capture_logs

# ---------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _fresh_process_view() -> Iterator[None]:
    """Reset the latch + burn-in budget + warn memo around every test."""
    lrr.reset_lake_rust_render_for_tests()
    yield
    lrr.reset_lake_rust_render_for_tests()


def _fake_crate(
    *,
    pinned: str = "1.42.1",
    render: Any = None,
    zstd: str = "1.5.7",
) -> types.ModuleType:
    """Build a fake ``brutex_lake`` module with injectable behaviour."""
    module = types.ModuleType("brutex_lake")
    module.pinned_polars_version = lambda: pinned  # type: ignore[attr-defined]
    module.linked_zstd_version = lambda: zstd  # type: ignore[attr-defined]
    if render is None:
        render = lambda ipc: b"rust-bytes"  # noqa: E731 - test stub
    module.render_parquet_from_ipc = render  # type: ignore[attr-defined]
    return module


def _install_crate(monkeypatch: pytest.MonkeyPatch, module: types.ModuleType | None) -> None:
    """Install a fake crate (or a poisoned ``None`` → ImportError) in sys.modules."""
    monkeypatch.setitem(sys.modules, "brutex_lake", module)


PY_BYTES = b"python-oracle-bytes"


def _python_render() -> bytes:
    return PY_BYTES


def _ipc_factory() -> bytes:
    return b"ipc-payload"


# ---------------------------------------------------------------------------
# flag parsing
# ---------------------------------------------------------------------------


class TestFlagParsing:
    @pytest.mark.parametrize("raw", ["1", "true", "YES", " on "])
    def test_truthy_literals_enable(self, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
        monkeypatch.setenv(lrr.LAKE_RUST_RENDER_ENV, raw)
        assert lrr.lake_rust_render_enabled() is True

    @pytest.mark.parametrize("raw", ["0", "false", "NO", " off "])
    def test_falsy_literals_disable_silently(
        self, monkeypatch: pytest.MonkeyPatch, raw: str
    ) -> None:
        monkeypatch.setenv(lrr.LAKE_RUST_RENDER_ENV, raw)
        with capture_logs() as logs:
            assert lrr.lake_rust_render_enabled() is False
        assert logs == []

    def test_unset_defaults_off_silently(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(lrr.LAKE_RUST_RENDER_ENV, raising=False)
        with capture_logs() as logs:
            assert lrr.lake_rust_render_enabled() is False
        assert logs == []

    def test_whitespace_empty_defaults_off_silently(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(lrr.LAKE_RUST_RENDER_ENV, "   ")
        with capture_logs() as logs:
            assert lrr.lake_rust_render_enabled() is False
        assert logs == []

    def test_junk_fails_loud_off_and_warns_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(lrr.LAKE_RUST_RENDER_ENV, "onn")
        with capture_logs() as logs:
            assert lrr.lake_rust_render_enabled() is False
            assert lrr.lake_rust_render_enabled() is False  # repeat — memoised
        warns = [e for e in logs if e["event"] == "lake_rust_render.fallback"]
        assert len(warns) == 1
        assert warns[0]["reason"] == "flag_unrecognised"
        assert warns[0]["value"] == "onn"

    def test_tristate_vocabulary(self) -> None:
        assert lrr.env_flag_tristate("1") is True
        assert lrr.env_flag_tristate(" TRUE ") is True
        assert lrr.env_flag_tristate("0") is False
        assert lrr.env_flag_tristate("Off") is False
        assert lrr.env_flag_tristate("") is None
        assert lrr.env_flag_tristate("junk") is None

    def test_vocabulary_mirrors_the_engine_common_classifier(self) -> None:
        """The local twin must stay byte-identical to the sweep vocabulary.

        The leaf deliberately does NOT import the engine helper (the engine
        package hard-imports the fno_math wheel — module docstring); this
        pin keeps the two homes from drifting. Skips honestly where the
        engine package cannot import (no fno_math wheel in the env).
        """
        engine_flags = pytest.importorskip(
            "brutex.engine.sweep_lookahead",
            reason="engine package needs the fno_math wheel — mirror pin runs in CI",
        )
        assert lrr._TRUTHY == engine_flags._TRUTHY
        assert lrr._FALSY == engine_flags._FALSY
        for raw in ["1", "true", "YES", " on ", "0", "false", "No", "off", "", "junk", "  "]:
            assert lrr.env_flag_tristate(raw) == engine_flags.env_flag_tristate(raw)


# ---------------------------------------------------------------------------
# import guard (layer 1)
# ---------------------------------------------------------------------------


class TestImportGuard:
    def test_absent_crate_warns_once_latches_and_serves_python(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_crate(monkeypatch, None)  # None in sys.modules → ImportError
        with capture_logs() as logs:
            first = lrr.render_parquet_bytes(
                ipc_bytes_factory=_ipc_factory,
                python_render=_python_render,
                polars_version="1.42.1",
            )
            second = lrr.render_parquet_bytes(
                ipc_bytes_factory=_ipc_factory,
                python_render=_python_render,
                polars_version="1.42.1",
            )
        assert first == PY_BYTES
        assert second == PY_BYTES
        assert lrr.rust_render_disabled()
        disabled = [e for e in logs if e["event"] == "lake_rust_render.rust_path_disabled"]
        assert len(disabled) == 1  # the latch logs ONCE; the repeat short-circuits
        assert disabled[0]["reason"] == "crate_unavailable"
        assert "pip install rust/brutex-lake" in disabled[0]["remedy"]


# ---------------------------------------------------------------------------
# version guard (layer 2)
# ---------------------------------------------------------------------------


class TestVersionGuard:
    def test_mismatch_warns_naming_both_versions_and_latches(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        crate = _fake_crate(pinned="1.42.1")
        _install_crate(monkeypatch, crate)
        with capture_logs() as logs:
            out = lrr.render_parquet_bytes(
                ipc_bytes_factory=_ipc_factory,
                python_render=_python_render,
                polars_version="1.43.0",
            )
        assert out == PY_BYTES
        assert lrr.rust_render_disabled()
        disabled = [e for e in logs if e["event"] == "lake_rust_render.rust_path_disabled"]
        assert len(disabled) == 1
        assert disabled[0]["reason"] == "polars_version_mismatch"
        assert disabled[0]["installed_polars"] == "1.43.0"
        assert disabled[0]["crate_pinned_polars"] == "1.42.1"

    def test_mismatch_never_calls_the_crate_render(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[bytes] = []

        def _render(ipc: bytes) -> bytes:
            calls.append(ipc)
            return PY_BYTES

        _install_crate(monkeypatch, _fake_crate(pinned="1.42.1", render=_render))
        lrr.render_parquet_bytes(
            ipc_bytes_factory=_ipc_factory,
            python_render=_python_render,
            polars_version="2.0.0",
        )
        assert calls == []  # fail-closed BEFORE any render/IPC crossing


# ---------------------------------------------------------------------------
# burn-in + autorevert (layer 3)
# ---------------------------------------------------------------------------


class TestBurnIn:
    def test_clean_burn_in_ships_rust_bytes_and_completes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_crate(monkeypatch, _fake_crate(render=lambda ipc: PY_BYTES))
        with capture_logs() as logs:
            for _ in range(lrr.BURN_IN_SAMPLES):
                out = lrr.render_parquet_bytes(
                    ipc_bytes_factory=_ipc_factory,
                    python_render=_python_render,
                    polars_version="1.42.1",
                )
                assert out == PY_BYTES
        assert not lrr.rust_render_disabled()
        engaged = [e for e in logs if e["event"] == "lake_rust_render.engaged"]
        assert len(engaged) == 1  # once per process, not per render
        assert engaged[0]["crate_pinned_polars"] == "1.42.1"
        assert engaged[0]["linked_zstd"] == "1.5.7"
        complete = [e for e in logs if e["event"] == "lake_rust_render.burn_in_complete"]
        assert len(complete) == 1
        assert complete[0]["samples"] == lrr.BURN_IN_SAMPLES

    def test_burn_in_runs_the_python_oracle_each_sample(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        oracle_calls: list[int] = []

        def _oracle() -> bytes:
            oracle_calls.append(1)
            return PY_BYTES

        _install_crate(monkeypatch, _fake_crate(render=lambda ipc: PY_BYTES))
        for _ in range(3):
            lrr.render_parquet_bytes(
                ipc_bytes_factory=_ipc_factory,
                python_render=_oracle,
                polars_version="1.42.1",
            )
        assert len(oracle_calls) == 3

    def test_mismatch_ships_python_bytes_errors_and_autoreverts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        crate_calls: list[int] = []

        def _wrong(ipc: bytes) -> bytes:
            crate_calls.append(1)
            return b"WRONG-bytes"

        _install_crate(monkeypatch, _fake_crate(render=_wrong))
        with capture_logs() as logs:
            out = lrr.render_parquet_bytes(
                ipc_bytes_factory=_ipc_factory,
                python_render=_python_render,
                polars_version="1.42.1",
            )
            after = lrr.render_parquet_bytes(
                ipc_bytes_factory=_ipc_factory,
                python_render=_python_render,
                polars_version="1.42.1",
            )
        assert out == PY_BYTES  # the ORACLE bytes ship — never the wrong ones
        assert after == PY_BYTES
        assert lrr.rust_render_disabled()
        assert len(crate_calls) == 1  # the latch short-circuits the second call
        errors = [e for e in logs if e["event"] == "lake_rust_render.render_autoreverted"]
        assert len(errors) == 1
        assert errors[0]["log_level"] == "error"
        assert errors[0]["reason"] == "burn_in_byte_mismatch"
        assert errors[0]["rust_len"] == len(b"WRONG-bytes")
        assert errors[0]["python_len"] == len(PY_BYTES)

    def test_post_burn_in_ships_rust_bytes_without_the_oracle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lrr.reset_lake_rust_render_for_tests(remaining=0)  # steady state

        def _oracle_must_not_run() -> bytes:
            raise AssertionError("python oracle must not run after burn-in")

        _install_crate(monkeypatch, _fake_crate(render=lambda ipc: b"rust-steady"))
        out = lrr.render_parquet_bytes(
            ipc_bytes_factory=_ipc_factory,
            python_render=_oracle_must_not_run,
            polars_version="1.42.1",
        )
        assert out == b"rust-steady"

    def test_burn_in_budget_decrements_to_steady_state(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lrr.reset_lake_rust_render_for_tests(remaining=2)
        oracle_calls: list[int] = []

        def _oracle() -> bytes:
            oracle_calls.append(1)
            return PY_BYTES

        _install_crate(monkeypatch, _fake_crate(render=lambda ipc: PY_BYTES))
        for _ in range(4):
            lrr.render_parquet_bytes(
                ipc_bytes_factory=_ipc_factory,
                python_render=_oracle,
                polars_version="1.42.1",
            )
        assert len(oracle_calls) == 2  # only the 2 burn-in samples ran the oracle


# ---------------------------------------------------------------------------
# degrade paths
# ---------------------------------------------------------------------------


class TestDegradePaths:
    @pytest.mark.parametrize(
        "exc",
        [
            ValueError("bad ipc"),
            RuntimeError("panicked"),
            TypeError("arg"),
            OverflowError("x"),
            AttributeError("broken crate internals"),
        ],
    )
    def test_crate_errors_degrade_to_python_with_one_warn(
        self, monkeypatch: pytest.MonkeyPatch, exc: Exception
    ) -> None:
        def _raise(ipc: bytes) -> bytes:
            raise exc

        _install_crate(monkeypatch, _fake_crate(render=_raise))
        with capture_logs() as logs:
            out = lrr.render_parquet_bytes(
                ipc_bytes_factory=_ipc_factory,
                python_render=_python_render,
                polars_version="1.42.1",
            )
            again = lrr.render_parquet_bytes(
                ipc_bytes_factory=_ipc_factory,
                python_render=_python_render,
                polars_version="1.42.1",
            )
        assert out == PY_BYTES
        assert again == PY_BYTES
        assert not lrr.rust_render_disabled()  # degrade, not latch
        warns = [e for e in logs if e["event"] == "lake_rust_render.fallback"]
        assert len(warns) == 1  # warn-once per reason
        assert warns[0]["reason"] == "render_error"

    def test_polars_error_from_the_ipc_factory_degrades(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import polars.exceptions as pl_exc

        def _bad_factory() -> bytes:
            raise pl_exc.ComputeError("ipc serialisation failed")

        _install_crate(monkeypatch, _fake_crate())
        with capture_logs() as logs:
            out = lrr.render_parquet_bytes(
                ipc_bytes_factory=_bad_factory,
                python_render=_python_render,
                polars_version="1.42.1",
            )
        assert out == PY_BYTES
        warns = [e for e in logs if e["event"] == "lake_rust_render.fallback"]
        assert warns and warns[0]["reason"] == "render_error"

    def test_memory_error_propagates_bare(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """OOM is a GLOBAL stop — never swallowed into a fallback.

        The caller's ``lake_write_guard`` maps it to ``LakeWriteHalt``
        exactly as on the pure-Python path (DEC-LAKE-WRITE-HALT-001).
        """

        def _oom(ipc: bytes) -> bytes:
            raise MemoryError

        _install_crate(monkeypatch, _fake_crate(render=_oom))
        with pytest.raises(MemoryError):
            lrr.render_parquet_bytes(
                ipc_bytes_factory=_ipc_factory,
                python_render=_python_render,
                polars_version="1.42.1",
            )

    def test_non_bytes_result_is_refused_loudly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_crate(monkeypatch, _fake_crate(render=lambda ipc: "not-bytes"))
        with capture_logs() as logs:
            out = lrr.render_parquet_bytes(
                ipc_bytes_factory=_ipc_factory,
                python_render=_python_render,
                polars_version="1.42.1",
            )
        assert out == PY_BYTES
        warns = [e for e in logs if e["event"] == "lake_rust_render.fallback"]
        assert warns and warns[0]["reason"] == "render_result_refused"
        assert warns[0]["result_type"] == "str"

    def test_degrade_error_set_without_polars_installed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The defensive belt: polars unimportable → the stdlib set only."""
        monkeypatch.setitem(sys.modules, "polars.exceptions", None)
        errors = lrr._degrade_errors()
        assert errors == (ValueError, RuntimeError, TypeError, OverflowError, AttributeError)

    def test_degrade_error_set_includes_polars_error(self) -> None:
        import polars.exceptions as pl_exc

        assert pl_exc.PolarsError in lrr._degrade_errors()


# ---------------------------------------------------------------------------
# foreign-crate API guard + metadata guard (review-round folds)
# ---------------------------------------------------------------------------


class TestCrateApiGuard:
    @pytest.mark.parametrize("missing", sorted(lrr._CRATE_API))
    def test_importable_module_missing_api_latches_never_raises(
        self, monkeypatch: pytest.MonkeyPatch, missing: str
    ) -> None:
        """A name-collision/hand-broken ``brutex_lake`` must fail CLOSED.

        The review-round repro: a module that IMPORTS but lacks the API
        previously escaped as a raw ``AttributeError`` into the pull spine
        (the docstring's 'never a new crash class' contract violated).
        """
        crate = _fake_crate()
        delattr(crate, missing)
        _install_crate(monkeypatch, crate)
        with capture_logs() as logs:
            out = lrr.render_parquet_bytes(
                ipc_bytes_factory=_ipc_factory,
                python_render=_python_render,
                polars_version="1.42.1",
            )
        assert out == PY_BYTES
        assert lrr.rust_render_disabled()
        disabled = [e for e in logs if e["event"] == "lake_rust_render.rust_path_disabled"]
        assert len(disabled) == 1
        assert disabled[0]["reason"] == "crate_unavailable"
        assert missing in disabled[0]["error_detail"]

    def test_non_callable_api_attr_latches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        crate = _fake_crate()
        crate.render_parquet_from_ipc = "not-callable"  # type: ignore[attr-defined]
        _install_crate(monkeypatch, crate)
        out = lrr.render_parquet_bytes(
            ipc_bytes_factory=_ipc_factory,
            python_render=_python_render,
            polars_version="1.42.1",
        )
        assert out == PY_BYTES
        assert lrr.rust_render_disabled()

    def test_empty_foreign_module_latches_never_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The reviewer's exact repro: a bare ``types.ModuleType`` shadow."""
        _install_crate(monkeypatch, types.ModuleType("brutex_lake"))
        out = lrr.render_parquet_bytes(
            ipc_bytes_factory=_ipc_factory,
            python_render=_python_render,
            polars_version="1.42.1",
        )
        assert out == PY_BYTES
        assert lrr.rust_render_disabled()


class TestMetadataGuard:
    def test_raising_pinned_version_latches_and_never_renders(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A raising metadata pyfunction previously ESCAPED (outside the try)."""
        calls: list[bytes] = []

        def _render(ipc: bytes) -> bytes:
            calls.append(ipc)
            return PY_BYTES

        crate = _fake_crate(render=_render)

        def _boom() -> str:
            raise RuntimeError("stale/foreign crate metadata")

        crate.pinned_polars_version = _boom  # type: ignore[attr-defined]
        _install_crate(monkeypatch, crate)
        with capture_logs() as logs:
            out = lrr.render_parquet_bytes(
                ipc_bytes_factory=_ipc_factory,
                python_render=_python_render,
                polars_version="1.42.1",
            )
        assert out == PY_BYTES
        assert calls == []  # fail-closed BEFORE any render/IPC crossing
        assert lrr.rust_render_disabled()
        disabled = [e for e in logs if e["event"] == "lake_rust_render.rust_path_disabled"]
        assert len(disabled) == 1
        assert disabled[0]["reason"] == "crate_metadata_error"
        assert disabled[0]["metadata_call"] == "pinned_polars_version"

    def test_raising_linked_zstd_latches_and_serves_the_oracle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The reviewer's standalone repro: linked_zstd_version() raising ESCAPED."""
        crate = _fake_crate(render=lambda ipc: b"rust-bytes-not-trusted")

        def _boom() -> str:
            raise RuntimeError("zstd probe broke")

        crate.linked_zstd_version = _boom  # type: ignore[attr-defined]
        _install_crate(monkeypatch, crate)
        with capture_logs() as logs:
            out = lrr.render_parquet_bytes(
                ipc_bytes_factory=_ipc_factory,
                python_render=_python_render,
                polars_version="1.42.1",
            )
        assert out == PY_BYTES  # the foreign crate's bytes are NOT shipped
        assert lrr.rust_render_disabled()
        disabled = [e for e in logs if e["event"] == "lake_rust_render.rust_path_disabled"]
        assert disabled and disabled[0]["reason"] == "crate_metadata_error"
        assert disabled[0]["metadata_call"] == "linked_zstd_version"
        # The engaged INFO must NOT have been emitted for the refused render.
        assert not any(e["event"] == "lake_rust_render.engaged" for e in logs)

    def test_none_returning_pinned_version_fails_the_version_guard(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A literal-``None`` metadata VALUE is not the error sentinel — it
        flows into the ``!=`` version guard and fails closed there, loudly."""
        crate = _fake_crate()
        crate.pinned_polars_version = lambda: None  # type: ignore[attr-defined]
        _install_crate(monkeypatch, crate)
        with capture_logs() as logs:
            out = lrr.render_parquet_bytes(
                ipc_bytes_factory=_ipc_factory,
                python_render=_python_render,
                polars_version="1.42.1",
            )
        assert out == PY_BYTES
        disabled = [e for e in logs if e["event"] == "lake_rust_render.rust_path_disabled"]
        assert disabled and disabled[0]["reason"] == "polars_version_mismatch"

    def test_memory_error_from_metadata_propagates_bare(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        crate = _fake_crate()

        def _oom() -> str:
            raise MemoryError

        crate.pinned_polars_version = _oom  # type: ignore[attr-defined]
        _install_crate(monkeypatch, crate)
        with pytest.raises(MemoryError):
            lrr.render_parquet_bytes(
                ipc_bytes_factory=_ipc_factory,
                python_render=_python_render,
                polars_version="1.42.1",
            )
        assert not lrr.rust_render_disabled()  # OOM is a global stop, not a latch


# ---------------------------------------------------------------------------
# the narrow PanicException belt (review-round fold; CodeQL-narrowed shape)
# ---------------------------------------------------------------------------


class _FakePanicException(BaseException):
    """The ``pyo3_runtime.PanicException`` shape: BaseException, not Exception."""


def _install_panic_runtime(
    monkeypatch: pytest.MonkeyPatch, panic: object = _FakePanicException
) -> None:
    """Install a fake ``pyo3_runtime`` module exposing *panic* as the class."""
    module = types.ModuleType("pyo3_runtime")
    module.PanicException = panic  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pyo3_runtime", module)


class TestPanicBelt:
    def test_resolved_panic_class_from_the_render_latches_and_serves_the_oracle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _panic(ipc: bytes) -> bytes:
            raise _FakePanicException("polars worker panicked past catch_unwind")

        _install_panic_runtime(monkeypatch)  # the belt resolves the class by name
        _install_crate(monkeypatch, _fake_crate(render=_panic))
        with capture_logs() as logs:
            out = lrr.render_parquet_bytes(
                ipc_bytes_factory=_ipc_factory,
                python_render=_python_render,
                polars_version="1.42.1",
            )
            again = lrr.render_parquet_bytes(
                ipc_bytes_factory=_ipc_factory,
                python_render=_python_render,
                polars_version="1.42.1",
            )
        assert out == PY_BYTES
        assert again == PY_BYTES
        assert lrr.rust_render_disabled()  # a broken boundary contract LATCHES
        disabled = [e for e in logs if e["event"] == "lake_rust_render.rust_path_disabled"]
        assert len(disabled) == 1
        assert disabled[0]["reason"] == "crate_panic"

    def test_the_real_polars_panic_exception_latches_via_the_polars_home(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The arm that resolves TODAY: ``polars.exceptions.PanicException``
        (``pyo3_runtime`` is not a real importable module — probed) is
        caught, latched ``crate_panic``, and the oracle serves."""
        import polars.exceptions as pl_exc

        def _panic(ipc: bytes) -> bytes:
            raise pl_exc.PanicException("simulated escaped panic")

        _install_crate(monkeypatch, _fake_crate(render=_panic))
        with capture_logs() as logs:
            out = lrr.render_parquet_bytes(
                ipc_bytes_factory=_ipc_factory,
                python_render=_python_render,
                polars_version="1.42.1",
            )
        assert out == PY_BYTES
        assert lrr.rust_render_disabled()
        disabled = [e for e in logs if e["event"] == "lake_rust_render.rust_path_disabled"]
        assert disabled and disabled[0]["reason"] == "crate_panic"

    def test_out_of_degrade_set_exception_latches_crate_panic(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The belt's ``Exception`` member preserves the pre-narrowing law:
        an Exception OUTSIDE the degrade set (never a warn-and-retry shape)
        latches ``crate_panic`` and serves the oracle."""

        def _os_error(ipc: bytes) -> bytes:
            raise OSError("crate touched the filesystem it should not have")

        _install_crate(monkeypatch, _fake_crate(render=_os_error))
        with capture_logs() as logs:
            out = lrr.render_parquet_bytes(
                ipc_bytes_factory=_ipc_factory,
                python_render=_python_render,
                polars_version="1.42.1",
            )
        assert out == PY_BYTES
        assert lrr.rust_render_disabled()
        disabled = [e for e in logs if e["event"] == "lake_rust_render.rust_path_disabled"]
        assert disabled and disabled[0]["reason"] == "crate_panic"

    def test_unresolvable_foreign_base_exception_propagates(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The DISCLOSED narrowing (module docstring): a BaseException the
        resolver cannot name — e.g. a distinct per-extension PanicException
        type object — PROPAGATES bare instead of latching (the CodeQL
        py/catch-base-exception re-raise-only convention; pre-narrowing it
        latched)."""
        monkeypatch.setitem(sys.modules, "pyo3_runtime", None)  # unimportable

        def _panic(ipc: bytes) -> bytes:
            raise _FakePanicException("a foreign extension's own panic class")

        _install_crate(monkeypatch, _fake_crate(render=_panic))
        with pytest.raises(_FakePanicException):
            lrr.render_parquet_bytes(
                ipc_bytes_factory=_ipc_factory,
                python_render=_python_render,
                polars_version="1.42.1",
            )
        assert not lrr.rust_render_disabled()  # propagated, never half-latched

    @pytest.mark.parametrize("exc_type", [KeyboardInterrupt, SystemExit])
    def test_interrupt_and_exit_re_raise_bare(
        self, monkeypatch: pytest.MonkeyPatch, exc_type: type[BaseException]
    ) -> None:
        def _interrupt(ipc: bytes) -> bytes:
            raise exc_type

        _install_panic_runtime(monkeypatch)  # even with the belt fully armed
        _install_crate(monkeypatch, _fake_crate(render=_interrupt))
        with pytest.raises(exc_type):
            lrr.render_parquet_bytes(
                ipc_bytes_factory=_ipc_factory,
                python_render=_python_render,
                polars_version="1.42.1",
            )
        assert not lrr.rust_render_disabled()  # never swallowed into a latch


class TestPanicBeltResolver:
    """``_panic_belt_errors`` — the guarded name-resolution law."""

    def test_no_home_importable_narrows_to_exception_alone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(sys.modules, "pyo3_runtime", None)
        monkeypatch.setitem(sys.modules, "polars.exceptions", None)
        assert lrr._panic_belt_errors() == (Exception,)

    def test_fake_pyo3_runtime_class_joins_the_belt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_panic_runtime(monkeypatch)
        monkeypatch.setitem(sys.modules, "polars.exceptions", None)
        assert lrr._panic_belt_errors() == (Exception, _FakePanicException)

    def test_the_real_polars_home_resolves_today(self) -> None:
        import polars.exceptions as pl_exc

        belt = lrr._panic_belt_errors()
        assert Exception in belt
        assert pl_exc.PanicException in belt

    @pytest.mark.parametrize(
        "junk",
        ["not-a-class", type("NotAnException", (), {})],
        ids=["non-class", "non-BaseException-class"],
    )
    def test_junk_panic_attr_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, junk: object
    ) -> None:
        _install_panic_runtime(monkeypatch, panic=junk)
        monkeypatch.setitem(sys.modules, "polars.exceptions", None)
        assert lrr._panic_belt_errors() == (Exception,)

    def test_module_without_the_attr_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "pyo3_runtime", types.ModuleType("pyo3_runtime"))
        monkeypatch.setitem(sys.modules, "polars.exceptions", None)
        assert lrr._panic_belt_errors() == (Exception,)

    def test_the_same_class_in_both_homes_dedupes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_panic_runtime(monkeypatch)
        fake_pl = types.ModuleType("polars.exceptions")
        fake_pl.PanicException = _FakePanicException  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "polars.exceptions", fake_pl)
        assert lrr._panic_belt_errors() == (Exception, _FakePanicException)


# ---------------------------------------------------------------------------
# the burn-in SHAPE gate (review-round fold)
# ---------------------------------------------------------------------------


class TestShapeGate:
    def test_new_shape_after_spent_budget_is_byte_compared_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The reviewer's scenario: 16 spot renders complete the count budget,
        then the FIRST-EVER Greeks shape must still be byte-compared."""
        oracle_calls: list[int] = []

        def _oracle() -> bytes:
            oracle_calls.append(1)
            return PY_BYTES

        _install_crate(monkeypatch, _fake_crate(render=lambda ipc: PY_BYTES))
        lrr.reset_lake_rust_render_for_tests(remaining=1)
        lrr.render_parquet_bytes(
            ipc_bytes_factory=_ipc_factory,
            python_render=_oracle,
            polars_version="1.42.1",
            schema_token="spot-7col",
        )
        assert len(oracle_calls) == 1  # count-budget compare; budget now 0
        lrr.render_parquet_bytes(
            ipc_bytes_factory=_ipc_factory,
            python_render=_oracle,
            polars_version="1.42.1",
            schema_token="greeks-17col",
        )
        assert len(oracle_calls) == 2  # NEW shape → compare despite spent budget
        lrr.render_parquet_bytes(
            ipc_bytes_factory=_ipc_factory,
            python_render=_oracle,
            polars_version="1.42.1",
            schema_token="greeks-17col",
        )
        assert len(oracle_calls) == 2  # seen shape → steady state, no oracle
        assert not lrr.rust_render_disabled()

    def test_shape_compare_mismatch_autoreverts_with_the_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_crate(monkeypatch, _fake_crate(render=lambda ipc: b"WRONG"))
        lrr.reset_lake_rust_render_for_tests(remaining=1)
        # Burn the budget on a clean shape first.
        clean = _fake_crate(render=lambda ipc: PY_BYTES)
        _install_crate(monkeypatch, clean)
        lrr.render_parquet_bytes(
            ipc_bytes_factory=_ipc_factory,
            python_render=_python_render,
            polars_version="1.42.1",
            schema_token="shape-a",
        )
        _install_crate(monkeypatch, _fake_crate(render=lambda ipc: b"WRONG"))
        with capture_logs() as logs:
            out = lrr.render_parquet_bytes(
                ipc_bytes_factory=_ipc_factory,
                python_render=_python_render,
                polars_version="1.42.1",
                schema_token="shape-b",
            )
        assert out == PY_BYTES  # the ORACLE bytes ship on the shape compare too
        assert lrr.rust_render_disabled()
        errors = [e for e in logs if e["event"] == "lake_rust_render.render_autoreverted"]
        assert errors and errors[0]["schema_token"] == "shape-b"

    def test_none_token_degrades_to_the_count_budget_alone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        oracle_calls: list[int] = []

        def _oracle() -> bytes:
            oracle_calls.append(1)
            return PY_BYTES

        _install_crate(monkeypatch, _fake_crate(render=lambda ipc: PY_BYTES))
        lrr.reset_lake_rust_render_for_tests(remaining=1)
        for _ in range(3):
            lrr.render_parquet_bytes(
                ipc_bytes_factory=_ipc_factory,
                python_render=_oracle,
                polars_version="1.42.1",
            )
        assert len(oracle_calls) == 1  # only the count-budget sample compared

    def test_steady_state_reset_disarms_the_shape_gate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``remaining=0`` must mean CRATE bytes with no oracle in the path —
        the parity corpus depends on it (an armed gate would cushion a
        first-shape divergence)."""
        lrr.reset_lake_rust_render_for_tests(remaining=0)

        def _oracle_must_not_run() -> bytes:
            raise AssertionError("steady state must not run the oracle")

        _install_crate(monkeypatch, _fake_crate(render=lambda ipc: b"rust-steady"))
        out = lrr.render_parquet_bytes(
            ipc_bytes_factory=_ipc_factory,
            python_render=_oracle_must_not_run,
            polars_version="1.42.1",
            schema_token="never-seen-shape",
        )
        assert out == b"rust-steady"


# ---------------------------------------------------------------------------
# thread-fuzz: the unlocked latch/counter/memo invariant (review-round fold)
# ---------------------------------------------------------------------------


class TestThreadFuzz:
    def test_concurrent_renders_never_ship_a_wrong_byte(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """K threads × distinct frames, flag ON: every returned byte equals
        that frame's oracle, and at least the full burn-in budget of
        compares ran (the documented race can only LOSE decrements — extra
        compares, never fewer, never a wrong byte)."""
        import threading

        def _render(ipc: bytes) -> bytes:
            return b"rendered:" + ipc

        _install_crate(monkeypatch, _fake_crate(render=_render))
        oracle_calls: list[int] = []
        results: dict[int, list[bytes]] = {}
        errors: list[BaseException] = []
        n_threads, per_thread = 8, 10

        def _const(payload: bytes) -> Callable[[], bytes]:
            return lambda: payload

        def _oracle_for(payload: bytes) -> Callable[[], bytes]:
            def _oracle() -> bytes:
                oracle_calls.append(1)
                return b"rendered:" + payload

            return _oracle

        def _capture(args: threading.ExceptHookArgs) -> None:
            # Worker escapes of ANY tier — ``Exception`` AND ``BaseException``
            # — land here and fail the ``assert errors == []`` below: a
            # ``threading.excepthook`` override instead of a literal
            # ``except BaseException:`` handler in the worker (CodeQL
            # py/catch-base-exception; the test_dashboard_rate_bridge_cache.py
            # precedent), so the teeth are unchanged.
            exc = args.exc_value if args.exc_value is not None else args.exc_type()
            errors.append(exc)

        monkeypatch.setattr(threading, "excepthook", _capture)

        def _worker(idx: int) -> None:
            out: list[bytes] = []
            for j in range(per_thread):
                payload = f"frame-{idx}-{j}".encode()
                out.append(
                    lrr.render_parquet_bytes(
                        ipc_bytes_factory=_const(payload),
                        python_render=_oracle_for(payload),
                        polars_version="1.42.1",
                        schema_token="fuzz-shape",
                    )
                )
            results[idx] = out

        threads = [threading.Thread(target=_worker, args=(i,)) for i in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        assert not lrr.rust_render_disabled()  # no autorevert — all compares agreed
        for idx in range(n_threads):
            expected = [f"rendered:frame-{idx}-{j}".encode() for j in range(per_thread)]
            assert results[idx] == expected  # every stored byte == that frame's oracle
        total = n_threads * per_thread
        assert lrr.BURN_IN_SAMPLES <= len(oracle_calls) <= total  # lost decrements only ADD


# ---------------------------------------------------------------------------
# latch / reset seams
# ---------------------------------------------------------------------------


class TestLatchAndReset:
    def test_disabled_short_circuits_before_the_import_guard(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lrr._disable("crate_unavailable")

        def _must_not_import(name: str) -> types.ModuleType:
            raise AssertionError("a latched process must not re-attempt the crate import")

        monkeypatch.setattr(lrr.importlib, "import_module", _must_not_import)
        out = lrr.render_parquet_bytes(
            ipc_bytes_factory=_ipc_factory,
            python_render=_python_render,
            polars_version="1.42.1",
        )
        assert out == PY_BYTES

    def test_disable_is_idempotent_and_logs_once(self) -> None:
        with capture_logs() as logs:
            lrr._disable("crate_unavailable")
            lrr._disable("polars_version_mismatch")  # second reason ignored
        assert lrr._DISABLED_REASON[0] == "crate_unavailable"
        assert len(logs) == 1

    def test_reset_restores_a_fresh_process_view(self) -> None:
        lrr._disable("crate_unavailable")
        lrr._warn_once("render_error")
        lrr._BURN_IN_SHAPES_SEEN.add("stale-shape")
        lrr.reset_lake_rust_render_for_tests()
        assert not lrr.rust_render_disabled()
        assert lrr._BURN_IN_REMAINING[0] == lrr.BURN_IN_SAMPLES
        assert set() == lrr._OBSERVABILITY_MEMO
        assert set() == lrr._BURN_IN_SHAPES_SEEN
        assert lrr._BURN_IN_SHAPE_GATE[0] is True  # armed on a fresh view

    def test_reset_steady_state_disarms_the_shape_gate(self) -> None:
        lrr.reset_lake_rust_render_for_tests(remaining=0)
        assert lrr._BURN_IN_SHAPE_GATE[0] is False  # steady state — crate bytes, no oracle
