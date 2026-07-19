"""Unit suite for ``brutex.data.lake_rust_waved`` — 100% line+branch standalone.

Every test injects a FAKE ``brutex_lake`` module straight into
``sys.modules`` (or a poisoned ``None`` entry for the import-guard arm), so
the suite runs with NO wheel installed — the real-wheel differential lives
in ``tests/integration/test_waved_read1m_differential.py``. The harness
mirrors ``tests/unit/test_lake_rust_resample.py`` (the Wave-B facade suite)
section-for-section, with the W-1 specifics: the ``(ipc_bytes, events_json)``
tuple wire, the events-channel refuse-poison arms, and the events replay
through the ONE ``emit_bad_tick_warn`` seam.
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
from brutex.data import lake_rust_waved as lrw
from structlog.testing import capture_logs

if TYPE_CHECKING:
    from collections.abc import Iterator

# ---------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _fresh_process_view() -> Iterator[None]:
    """Reset the latch + burn-in cells + warn memo around every test."""
    lrw.reset_lake_rust_waved_for_tests()
    yield
    lrw.reset_lake_rust_waved_for_tests()


#: The frame the ORACLE stub returns — any recognisable frame works; the
#: seam never inspects it beyond the burn-in comparison.
_ORACLE_DF = pl.DataFrame({"ts_utc": [1], "close": [105.0]})

#: A value-DIFFERENT frame (burn-in value-mismatch arm).
_WRONG_DF = pl.DataFrame({"ts_utc": [1], "close": [999.0]})

#: A value-IDENTICAL, dtype-PUNNED frame (Float32 close) — ``.equals`` alone
#: calls it equal (#1795b), the dtype-exact comparator must NOT.
_PUN_DF = _ORACLE_DF.with_columns(pl.col("close").cast(pl.Float32))

#: A well-formed single-event payload (the crate wire shape).
_ONE_EVENT_JSON = (
    '[{"session":"2024-01-01","skipped":2,"max_ratio":4.0,'
    '"first_prev_close":100.0,"first_close":1000.0}]'
)


def _ipc(df: pl.DataFrame) -> bytes:
    buf = io.BytesIO()
    df.write_ipc(buf, compression="uncompressed")
    return buf.getvalue()


def _fake_crate(*, pinned: Any = pl.__version__, read: Any = None) -> types.ModuleType:
    """Build a fake ``brutex_lake`` module with injectable behaviour."""
    module = types.ModuleType("brutex_lake")
    module.pinned_polars_version = (  # type: ignore[attr-defined]
        pinned if callable(pinned) else (lambda: pinned)
    )
    if read is None:
        read = lambda files, root, oi, screen: (_ipc(_ORACLE_DF), "[]")  # noqa: E731
    module.waved_read_1m_ipc = read  # type: ignore[attr-defined]
    module.waved_has_stored_1m_bars = lambda base: False  # type: ignore[attr-defined]
    module.waved_month_partition_files = lambda base, months: []  # type: ignore[attr-defined]
    return module


def _install_crate(monkeypatch: pytest.MonkeyPatch, module: types.ModuleType | None) -> None:
    """Install a fake crate (or a poisoned ``None`` → ImportError) in sys.modules."""
    monkeypatch.setitem(sys.modules, "brutex_lake", module)


class _CountingOracle:
    """Oracle stub counting invocations (returns :data:`_ORACLE_DF`)."""

    def __init__(self, result: pl.DataFrame = _ORACLE_DF) -> None:
        self.calls = 0
        self._result = result

    def __call__(self) -> pl.DataFrame:
        self.calls += 1
        return self._result


def _read(
    monkeypatch: pytest.MonkeyPatch,
    module: types.ModuleType | None,
    *,
    oracle: _CountingOracle | None = None,
    screen_enabled: bool = True,
    warn_source: str | None = "prebuild:NSE/INDEX/NIFTY/1minute",
) -> pl.DataFrame:
    _install_crate(monkeypatch, module)
    return lrw.read_1m_via_rust(
        files=["2024/01.parquet"],
        tree_root=None,
        with_open_interest=False,
        screen_enabled=screen_enabled,
        warn_source=warn_source,
        oracle=oracle if oracle is not None else _CountingOracle(),
    )


# ---------------------------------------------------------------------------
# flag parsing
# ---------------------------------------------------------------------------


class TestFlagParsing:
    @pytest.mark.parametrize("raw", ["1", "true", "YES", " on "])
    def test_truthy_literals_enable(self, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
        monkeypatch.setenv(lrw.LAKE_WAVED_RUST_ENV, raw)
        assert lrw.lake_rust_waved_enabled() is True

    @pytest.mark.parametrize("raw", ["0", "false", "NO", " off "])
    def test_falsy_literals_disable_silently(
        self, monkeypatch: pytest.MonkeyPatch, raw: str
    ) -> None:
        monkeypatch.setenv(lrw.LAKE_WAVED_RUST_ENV, raw)
        with capture_logs() as logs:
            assert lrw.lake_rust_waved_enabled() is False
        assert logs == []

    def test_unset_is_the_silent_default_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(lrw.LAKE_WAVED_RUST_ENV, raising=False)
        with capture_logs() as logs:
            assert lrw.lake_rust_waved_enabled() is False
        assert logs == []

    def test_whitespace_is_the_silent_default_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(lrw.LAKE_WAVED_RUST_ENV, "   ")
        with capture_logs() as logs:
            assert lrw.lake_rust_waved_enabled() is False
        assert logs == []

    def test_junk_is_loud_off_warned_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(lrw.LAKE_WAVED_RUST_ENV, "onn")
        with capture_logs() as logs:
            assert lrw.lake_rust_waved_enabled() is False
            assert lrw.lake_rust_waved_enabled() is False
        warns = [e for e in logs if e["event"] == "lake_rust_waved.fallback"]
        assert len(warns) == 1
        assert warns[0]["reason"] == "flag_unrecognised"

    def test_tristate_semantics_equal_the_render_and_resample_twins(self) -> None:
        # The LOCAL-twin equality pin (module docstring): same vocabulary as
        # the PR-1 render facade + the Wave-B resample facade copies.
        corpus = ["1", "true", "YES", " on ", "0", "false", "NO", " off ", "", " ", "junk", "2"]
        for raw in corpus:
            assert lrw.env_flag_tristate(raw) == lrr.env_flag_tristate(raw), raw
            assert lrw.env_flag_tristate(raw) == lrs.env_flag_tristate(raw), raw


# ---------------------------------------------------------------------------
# module purity (flag OFF never touches the crate)
# ---------------------------------------------------------------------------


class TestModulePurity:
    def test_facade_import_never_imports_the_crate(self) -> None:
        # Re-import the facade fresh with the crate evicted: importing the
        # MODULE must not import brutex_lake (only an ENGAGED read may).
        import importlib

        sys.modules.pop("brutex_lake", None)
        importlib.reload(sys.modules["brutex.data.lake_rust_waved"])
        assert "brutex_lake" not in sys.modules
        # Restore a clean view for the sibling tests (reload minted fresh
        # module-level state; the autouse fixture resets the reloaded one).
        lrw.reset_lake_rust_waved_for_tests()


# ---------------------------------------------------------------------------
# latch / reset
# ---------------------------------------------------------------------------


class TestLatchAndReset:
    def test_disabled_short_circuits_before_any_crate_touch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _bomb(*_args: object) -> tuple[bytes, str]:
            raise AssertionError("crate must not be touched once latched")

        lrw._disable("test_latch")
        assert lrw.rust_waved_disabled() is True
        oracle = _CountingOracle()
        out = _read(monkeypatch, _fake_crate(read=_bomb), oracle=oracle)
        assert out.equals(_ORACLE_DF)
        assert oracle.calls == 1

    def test_repeat_disable_is_a_noop_keeping_the_first_reason(self) -> None:
        lrw._disable("first")
        with capture_logs() as logs:
            lrw._disable("second")
        assert logs == []
        assert lrw._DISABLED_REASON[0] == "first"

    def test_disable_error_logs_autoreverted_at_error(self) -> None:
        with capture_logs() as logs:
            lrw._disable("burn_in_frame_mismatch", error=True)
        assert logs[0]["event"] == "lake_rust_waved.autoreverted"
        assert logs[0]["log_level"] == "error"

    def test_reset_restores_a_fresh_view(self) -> None:
        lrw._disable("test_latch")
        lrw._BURN_IN_REMAINING[lrw.SURFACE_READ_1M] = 0
        lrw._BURN_IN_SHAPE_GATE[lrw.SURFACE_READ_1M] = False
        lrw.reset_lake_rust_waved_for_tests()
        assert lrw.rust_waved_disabled() is False
        assert lrw._BURN_IN_REMAINING[lrw.SURFACE_READ_1M] == lrw.BURN_IN_SAMPLES
        assert lrw._BURN_IN_SHAPE_GATE[lrw.SURFACE_READ_1M] is True

    def test_reset_remaining_zero_is_the_steady_state(self) -> None:
        lrw.reset_lake_rust_waved_for_tests(remaining=0)
        assert lrw._BURN_IN_REMAINING[lrw.SURFACE_READ_1M] == 0
        assert lrw._BURN_IN_SHAPE_GATE[lrw.SURFACE_READ_1M] is False
        assert lrw._BURN_IN_SHAPES_SEEN[lrw.SURFACE_READ_1M] == set()

    def test_warn_once_memoises_per_reason(self) -> None:
        with capture_logs() as logs:
            lrw._warn_once("some_reason", detail="a")
            lrw._warn_once("some_reason", detail="b")
            lrw._warn_once("other_reason")
        events = [e["event"] for e in logs]
        assert events == ["lake_rust_waved.fallback", "lake_rust_waved.fallback"]
        assert [e["reason"] for e in logs] == ["some_reason", "other_reason"]


# ---------------------------------------------------------------------------
# import / API / metadata / version guards
# ---------------------------------------------------------------------------


class TestGuards:
    def test_absent_wheel_latches_crate_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        oracle = _CountingOracle()
        with capture_logs() as logs:
            out = _read(monkeypatch, None, oracle=oracle)
        assert out.equals(_ORACLE_DF)
        assert oracle.calls == 1
        assert lrw._DISABLED_REASON[0] == "crate_unavailable"
        assert logs[0]["event"] == "lake_rust_waved.rust_path_disabled"

    @pytest.mark.parametrize("missing", list(lrw._CRATE_API))
    def test_missing_api_callable_latches_crate_unavailable(
        self, monkeypatch: pytest.MonkeyPatch, missing: str
    ) -> None:
        crate = _fake_crate()
        delattr(crate, missing)
        oracle = _CountingOracle()
        out = _read(monkeypatch, crate, oracle=oracle)
        assert out.equals(_ORACLE_DF)
        assert oracle.calls == 1
        assert lrw._DISABLED_REASON[0] == "crate_unavailable"

    def test_non_callable_api_member_latches_crate_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        crate = _fake_crate()
        crate.waved_read_1m_ipc = "not callable"  # type: ignore[attr-defined]
        out = _read(monkeypatch, crate)
        assert out.equals(_ORACLE_DF)
        assert lrw._DISABLED_REASON[0] == "crate_unavailable"

    def test_raising_metadata_latches_crate_metadata_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom() -> str:
            raise RuntimeError("metadata exploded")

        out = _read(monkeypatch, _fake_crate(pinned=_boom))
        assert out.equals(_ORACLE_DF)
        assert lrw._DISABLED_REASON[0] == "crate_metadata_error"

    def test_metadata_memory_error_re_raises_raw(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _oom() -> str:
            raise MemoryError

        _install_crate(monkeypatch, _fake_crate(pinned=_oom))
        with pytest.raises(MemoryError):
            lrw.read_1m_via_rust(
                files=["f"],
                tree_root=None,
                with_open_interest=False,
                screen_enabled=True,
                warn_source=None,
                oracle=_CountingOracle(),
            )
        assert lrw._DISABLED_REASON[0] is None

    def test_polars_version_mismatch_latches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        out = _read(monkeypatch, _fake_crate(pinned="0.0.1"))
        assert out.equals(_ORACLE_DF)
        assert lrw._DISABLED_REASON[0] == "polars_version_mismatch"

    def test_none_pinned_version_fails_the_version_guard(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        out = _read(monkeypatch, _fake_crate(pinned=lambda: None))
        assert out.equals(_ORACLE_DF)
        assert lrw._DISABLED_REASON[0] == "polars_version_mismatch"

    def test_engaged_info_logs_once_per_process(self, monkeypatch: pytest.MonkeyPatch) -> None:
        lrw.reset_lake_rust_waved_for_tests(remaining=0)
        with capture_logs() as logs:
            _read(monkeypatch, _fake_crate())
            _read(monkeypatch, _fake_crate())
        engaged = [e for e in logs if e["event"] == "lake_rust_waved.engaged"]
        assert len(engaged) == 1
        assert engaged[0]["crate_pinned_polars"] == pl.__version__


# ---------------------------------------------------------------------------
# seam-args contract
# ---------------------------------------------------------------------------


class TestSeamArgs:
    @pytest.mark.parametrize(
        ("files", "tree_root"),
        [(None, None), (["f"], "/root")],
        ids=["neither", "both"],
    )
    def test_invalid_source_arm_combination_serves_the_oracle(
        self,
        monkeypatch: pytest.MonkeyPatch,
        files: list[str] | None,
        tree_root: str | None,
    ) -> None:
        def _bomb(*_args: object) -> tuple[bytes, str]:
            raise AssertionError("crate must not be called on invalid args")

        _install_crate(monkeypatch, _fake_crate(read=_bomb))
        oracle = _CountingOracle()
        with capture_logs() as logs:
            out = lrw.read_1m_via_rust(
                files=files,
                tree_root=tree_root,
                with_open_interest=False,
                screen_enabled=True,
                warn_source=None,
                oracle=oracle,
            )
        assert out.equals(_ORACLE_DF)
        assert oracle.calls == 1
        assert logs[0]["reason"] == "invalid_seam_args"
        assert lrw._DISABLED_REASON[0] is None  # a caller bug never latches

    def test_tree_root_arm_passes_empty_files_list(self, monkeypatch: pytest.MonkeyPatch) -> None:
        lrw.reset_lake_rust_waved_for_tests(remaining=0)
        seen: dict[str, object] = {}

        def _capture(
            files: list[str], root: str | None, oi: bool, screen: bool
        ) -> tuple[bytes, str]:
            seen.update(files=files, root=root, oi=oi, screen=screen)
            return (_ipc(_ORACLE_DF), "[]")

        _install_crate(monkeypatch, _fake_crate(read=_capture))
        lrw.read_1m_via_rust(
            files=None,
            tree_root="/lake/bars/NSE/INDEX/NIFTY/1minute",
            with_open_interest=True,
            screen_enabled=False,
            warn_source=None,
            oracle=_CountingOracle(),
        )
        assert seen == {
            "files": [],
            "root": "/lake/bars/NSE/INDEX/NIFTY/1minute",
            "oi": True,
            "screen": False,
        }


# ---------------------------------------------------------------------------
# degrade / escape / panic arms
# ---------------------------------------------------------------------------


class TestDegradeArms:
    def test_crate_value_error_degrades_with_one_warn(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _raise(*_args: object) -> tuple[bytes, str]:
            raise ValueError("gate refusal")

        oracle = _CountingOracle()
        with capture_logs() as logs:
            out = _read(monkeypatch, _fake_crate(read=_raise), oracle=oracle)
            out2 = _read(monkeypatch, _fake_crate(read=_raise), oracle=oracle)
        assert out.equals(_ORACLE_DF) and out2.equals(_ORACLE_DF)
        assert oracle.calls == 2
        warns = [e for e in logs if e.get("reason") == "read_error"]
        assert len(warns) == 1  # warn-once memo
        assert lrw._DISABLED_REASON[0] is None  # degrade, never a latch

    def test_oracle_raise_after_crate_refusal_propagates(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The degrade-to-oracle parity law: a GENUINE gate violation makes
        # the crate refuse AND the oracle raise the typed error — which must
        # propagate exactly as flag-OFF would.
        def _raise(*_args: object) -> tuple[bytes, str]:
            raise ValueError("gate refusal")

        def _oracle() -> pl.DataFrame:
            raise ValueError("stored 1-minute bars carry 1 price-insane row(s)")

        _install_crate(monkeypatch, _fake_crate(read=_raise))
        with pytest.raises(ValueError, match="price-insane"):
            lrw.read_1m_via_rust(
                files=["f"],
                tree_root=None,
                with_open_interest=False,
                screen_enabled=True,
                warn_source=None,
                oracle=_oracle,
            )

    @pytest.mark.parametrize("exc", [KeyboardInterrupt, SystemExit, MemoryError])
    def test_escape_set_re_raises_raw(
        self, monkeypatch: pytest.MonkeyPatch, exc: type[BaseException]
    ) -> None:
        def _raise(*_args: object) -> tuple[bytes, str]:
            raise exc

        _install_crate(monkeypatch, _fake_crate(read=_raise))
        with pytest.raises(exc):
            lrw.read_1m_via_rust(
                files=["f"],
                tree_root=None,
                with_open_interest=False,
                screen_enabled=True,
                warn_source=None,
                oracle=_CountingOracle(),
            )
        assert lrw._DISABLED_REASON[0] is None

    def test_out_of_degrade_set_exception_latches_crate_panic(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _weird(*_args: object) -> tuple[bytes, str]:
            raise KeyError("not in the degrade set")

        oracle = _CountingOracle()
        out = _read(monkeypatch, _fake_crate(read=_weird), oracle=oracle)
        assert out.equals(_ORACLE_DF)
        assert oracle.calls == 1
        assert lrw._DISABLED_REASON[0] == "crate_panic"

    def test_escaped_panic_exception_latches_crate_panic(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _FakePanicException(BaseException):
            pass

        panic_module = types.ModuleType("pyo3_runtime")
        panic_module.PanicException = _FakePanicException  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "pyo3_runtime", panic_module)

        def _panic(*_args: object) -> tuple[bytes, str]:
            raise _FakePanicException("thread panicked")

        out = _read(monkeypatch, _fake_crate(read=_panic))
        assert out.equals(_ORACLE_DF)
        assert lrw._DISABLED_REASON[0] == "crate_panic"


# ---------------------------------------------------------------------------
# refuse-poison / decode / events-channel arms
# ---------------------------------------------------------------------------


class TestRefusePoison:
    @pytest.mark.parametrize(
        "result",
        [
            b"bare bytes",
            (b"x",),
            (b"x", "[]", "extra"),
            ("not-bytes", "[]"),
            (b"x", 123),
            None,
        ],
        ids=["non-tuple", "arity-1", "arity-3", "non-bytes-first", "non-str-second", "none"],
    )
    def test_misshaped_result_refused_to_the_oracle(
        self, monkeypatch: pytest.MonkeyPatch, result: object
    ) -> None:
        oracle = _CountingOracle()
        with capture_logs() as logs:
            out = _read(monkeypatch, _fake_crate(read=lambda *a: result), oracle=oracle)
        assert out.equals(_ORACLE_DF)
        assert oracle.calls == 1
        assert any(e.get("reason") == "read_result_refused" for e in logs)
        assert lrw._DISABLED_REASON[0] is None

    def test_undecodable_ipc_payload_degrades(self, monkeypatch: pytest.MonkeyPatch) -> None:
        oracle = _CountingOracle()
        with capture_logs() as logs:
            out = _read(
                monkeypatch,
                _fake_crate(read=lambda *a: (b"not ipc bytes", "[]")),
                oracle=oracle,
            )
        assert out.equals(_ORACLE_DF)
        assert oracle.calls == 1
        assert any(e.get("reason") == "result_decode_error" for e in logs)

    @pytest.mark.parametrize(
        "events_json",
        [
            "not json",
            '{"session": "x"}',
            "[42]",
            '[{"session":"s","skipped":1}]',
            '[{"session":1,"skipped":1,"max_ratio":4.0,"first_prev_close":1.0,"first_close":2.0}]',
            '[{"session":"s","skipped":true,"max_ratio":4.0,"first_prev_close":1.0,'
            '"first_close":2.0}]',
            '[{"session":"s","skipped":0,"max_ratio":4.0,"first_prev_close":1.0,'
            '"first_close":2.0}]',
            '[{"session":"s","skipped":1,"max_ratio":true,"first_prev_close":1.0,'
            '"first_close":2.0}]',
            '[{"session":"s","skipped":1,"max_ratio":"4","first_prev_close":1.0,'
            '"first_close":2.0}]',
        ],
        ids=[
            "non-json",
            "non-list-root",
            "non-dict-item",
            "missing-keys",
            "non-str-session",
            "bool-skipped",
            "zero-skipped",
            "bool-ratio",
            "str-ratio",
        ],
    )
    def test_poisoned_events_channel_refuses_the_whole_result(
        self, monkeypatch: pytest.MonkeyPatch, events_json: str
    ) -> None:
        oracle = _CountingOracle()
        with capture_logs() as logs:
            out = _read(
                monkeypatch,
                _fake_crate(read=lambda *a: (_ipc(_ORACLE_DF), events_json)),
                oracle=oracle,
            )
        assert out.equals(_ORACLE_DF)
        assert oracle.calls == 1
        assert any(e.get("reason") == "events_refused" for e in logs)


# ---------------------------------------------------------------------------
# burn-in self-check
# ---------------------------------------------------------------------------


class TestBurnIn:
    def test_matching_burn_in_returns_the_crate_frame_and_decrements(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        oracle = _CountingOracle()
        out = _read(monkeypatch, _fake_crate(), oracle=oracle)
        assert out.equals(_ORACLE_DF)
        assert oracle.calls == 1  # the burn-in comparison ran the oracle
        assert lrw._BURN_IN_REMAINING[lrw.SURFACE_READ_1M] == lrw.BURN_IN_SAMPLES - 1
        assert lrw._DISABLED_REASON[0] is None

    def test_burn_in_never_replays_crate_events(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The oracle emits its OWN WARNs on a burn-in call — replaying the
        # crate events too would double-warn (module docstring law).
        with capture_logs() as logs:
            _read(
                monkeypatch,
                _fake_crate(read=lambda *a: (_ipc(_ORACLE_DF), _ONE_EVENT_JSON)),
            )
        assert not any(e["event"] == "feed.bad_tick_skipped" for e in logs)

    @pytest.mark.parametrize("wrong", [_WRONG_DF, _PUN_DF], ids=["value", "dtype-pun"])
    def test_mismatch_autoreverts_and_ships_the_oracle_frame(
        self, monkeypatch: pytest.MonkeyPatch, wrong: pl.DataFrame
    ) -> None:
        oracle = _CountingOracle()
        with capture_logs() as logs:
            out = _read(
                monkeypatch, _fake_crate(read=lambda *a: (_ipc(wrong), "[]")), oracle=oracle
            )
        assert out.equals(_ORACLE_DF)
        assert oracle.calls == 1
        assert lrw._DISABLED_REASON[0] == "burn_in_frame_mismatch"
        errors = [e for e in logs if e["event"] == "lake_rust_waved.autoreverted"]
        assert len(errors) == 1
        assert errors[0]["surface"] == lrw.SURFACE_READ_1M

    def test_oracle_raise_during_burn_in_propagates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A crate SUCCESS the oracle refuses (a gate the crate missed): the
        # oracle's typed raise propagates — production-identical fail-safe.
        def _oracle() -> pl.DataFrame:
            raise ValueError("oracle gate refusal")

        _install_crate(monkeypatch, _fake_crate())
        with pytest.raises(ValueError, match="oracle gate refusal"):
            lrw.read_1m_via_rust(
                files=["f"],
                tree_root=None,
                with_open_interest=False,
                screen_enabled=True,
                warn_source=None,
                oracle=_oracle,
            )

    def test_burn_in_complete_logs_once_at_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        lrw.reset_lake_rust_waved_for_tests(remaining=1)
        lrw._BURN_IN_SHAPE_GATE[lrw.SURFACE_READ_1M] = True
        with capture_logs() as logs:
            _read(monkeypatch, _fake_crate())
        done = [e for e in logs if e["event"] == "lake_rust_waved.burn_in_complete"]
        assert len(done) == 1
        assert done[0]["surface"] == lrw.SURFACE_READ_1M
        assert lrw._BURN_IN_REMAINING[lrw.SURFACE_READ_1M] == 0

    def test_unseen_shape_token_compares_after_budget_spent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Budget spent but the shape gate armed: a NEW (oi, screen) shape
        # still runs one comparison (and records the token).
        lrw.reset_lake_rust_waved_for_tests(remaining=0)
        lrw._BURN_IN_SHAPE_GATE[lrw.SURFACE_READ_1M] = True
        oracle = _CountingOracle()
        out = _read(monkeypatch, _fake_crate(), oracle=oracle)
        assert out.equals(_ORACLE_DF)
        assert oracle.calls == 1
        assert "oi=False|screen=True" in lrw._BURN_IN_SHAPES_SEEN[lrw.SURFACE_READ_1M]
        # The SAME shape again: steady state — no oracle.
        oracle2 = _CountingOracle()
        out2 = _read(monkeypatch, _fake_crate(), oracle=oracle2)
        assert out2.equals(_ORACLE_DF)
        assert oracle2.calls == 0

    def test_frames_equal_dtype_exact_is_schema_first(self) -> None:
        assert lrw._frames_equal_dtype_exact(_ORACLE_DF, _ORACLE_DF) is True
        assert lrw._frames_equal_dtype_exact(_ORACLE_DF, _PUN_DF) is False
        assert lrw._frames_equal_dtype_exact(_ORACLE_DF, _WRONG_DF) is False


# ---------------------------------------------------------------------------
# steady state — crate frame served raw + events replayed
# ---------------------------------------------------------------------------


class TestSteadyState:
    def test_steady_state_never_calls_the_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        lrw.reset_lake_rust_waved_for_tests(remaining=0)

        def _bomb_oracle() -> pl.DataFrame:
            raise AssertionError("oracle must not run in steady state")

        _install_crate(monkeypatch, _fake_crate())
        out = lrw.read_1m_via_rust(
            files=["f"],
            tree_root=None,
            with_open_interest=False,
            screen_enabled=True,
            warn_source=None,
            oracle=_bomb_oracle,
        )
        assert out.equals(_ORACLE_DF)

    def test_steady_state_replays_bad_tick_events_via_the_one_seam(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lrw.reset_lake_rust_waved_for_tests(remaining=0)
        with capture_logs() as logs:
            _read(
                monkeypatch,
                _fake_crate(read=lambda *a: (_ipc(_ORACLE_DF), _ONE_EVENT_JSON)),
                warn_source="prebuild:NSE/INDEX/NIFTY/1minute",
            )
        warns = [e for e in logs if e["event"] == "feed.bad_tick_skipped"]
        assert len(warns) == 1
        assert warns[0]["source"] == "prebuild:NSE/INDEX/NIFTY/1minute"
        assert warns[0]["session"] == "2024-01-01"
        assert warns[0]["skipped"] == 2
        assert warns[0]["max_ratio"] == 4.0
        assert warns[0]["first_prev_close"] == 100.0
        assert warns[0]["first_close"] == 1000.0

    def test_steady_state_empty_events_emit_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        lrw.reset_lake_rust_waved_for_tests(remaining=0)
        with capture_logs() as logs:
            _read(monkeypatch, _fake_crate())
        assert not any(e["event"] == "feed.bad_tick_skipped" for e in logs)

    def test_parse_accepts_multiple_events_in_order(self) -> None:
        two = (
            '[{"session":"2024-01-01","skipped":1,"max_ratio":4.0,'
            '"first_prev_close":1.0,"first_close":5.0},'
            '{"session":"2024-01-02","skipped":3,"max_ratio":4.0,'
            '"first_prev_close":2.0,"first_close":9.0}]'
        )
        events = lrw._parse_bad_tick_events(two)
        assert [e["session"] for e in events] == ["2024-01-01", "2024-01-02"]
        assert lrw._parse_bad_tick_events("[]") == []
