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


def _fake_crate(
    *,
    pinned: Any = pl.__version__,
    read: Any = None,
    heal: Any = None,
    token: Any = None,
    reconcile: Any = None,
    scan: Any = None,
) -> types.ModuleType:
    """Build a fake ``brutex_lake`` module with injectable behaviour.

    Carries the FULL W-1 + W-2 + W-2b + W-3 + W-4
    :data:`brutex.data.lake_rust_waved._CRATE_API` surface (the
    all-or-nothing import guard — a fake missing any name would latch
    ``crate_unavailable`` and every engaged-path test would silently
    exercise the oracle arm instead).
    """
    module = types.ModuleType("brutex_lake")
    module.pinned_polars_version = (  # type: ignore[attr-defined]
        pinned if callable(pinned) else (lambda: pinned)
    )
    if read is None:
        read = lambda files, root, oi, screen: (_ipc(_ORACLE_DF), "[]")  # noqa: E731
    module.waved_read_1m_ipc = read  # type: ignore[attr-defined]
    module.waved_has_stored_1m_bars = lambda base: False  # type: ignore[attr-defined]
    module.waved_month_partition_files = lambda base, months: []  # type: ignore[attr-defined]
    if heal is None:
        heal = lambda lake_root, quarantine_root, findings, on_event: _EMPTY_HEAL_OUTCOME()  # noqa: E731
    module.waved_heal_batch = heal  # type: ignore[attr-defined]
    if token is None:
        token = lambda lake_root: ("0" * 64, [])  # noqa: E731
    module.waved_lake_content_token = token  # type: ignore[attr-defined]
    if reconcile is None:
        reconcile = lambda path, classify_home: _RECON_CLEAN_WIRE  # noqa: E731
    module.waved_reconcile_file = reconcile  # type: ignore[attr-defined]
    if scan is None:
        scan = lambda *args: _scan_clean_wire()  # noqa: E731
    module.waved_scan_lake = scan  # type: ignore[attr-defined]
    # W-2b: the multi-file heal engagement witness (all-or-nothing lockstep).
    module.waved_heal_engaged_kinds = lambda: [  # type: ignore[attr-defined]
        "IntraFileDup",
        "OptionIntraFileDup",
        "SchemaAlienFile",
        "ForeignMonthRows",
        "OptionForeignMonthRows",
        "LegacyKeyDrift",
    ]
    return module


def _EMPTY_HEAL_OUTCOME() -> dict[str, object]:  # noqa: N802 — outcome factory, dict-shaped constant
    """A fresh empty-batch heal outcome dict (the crate wire shape)."""
    return {
        "healed": 0,
        "skipped": 0,
        "errors": 0,
        "quarantined_files": 0,
        "actions": [],
        "touched_minute": [],
    }


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


# ── W-2: the heal-batch surface ─────────────────────────────────────────────


def _heal_call(
    monkeypatch: pytest.MonkeyPatch,
    module: types.ModuleType | None,
    *,
    findings: list[dict[str, Any]] | None = None,
    on_event: Any = None,
) -> dict[str, Any] | None:
    _install_crate(monkeypatch, module)
    monkeypatch.setenv(lrw.LAKE_WAVED_RUST_ENV, "1")
    return lrw.heal_batch_via_rust(
        lake_root="/lake",
        quarantine_root="/lake/.quarantine/x",
        findings=[] if findings is None else findings,
        on_event=(lambda kind, payload: None) if on_event is None else on_event,
    )


class TestHealBatchSurface:
    """The §B2 heal seam — pre-mutation degrade only, everything else LOUD."""

    def test_absent_crate_returns_none_and_latches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert _heal_call(monkeypatch, None) is None
        assert lrw.rust_waved_disabled()

    def test_missing_w2_api_returns_none_and_latches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        crate = _fake_crate()
        delattr(crate, "waved_heal_batch")
        assert _heal_call(monkeypatch, crate) is None
        assert lrw.rust_waved_disabled()

    def test_degrade_verdict_returns_none_with_one_warn(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        crate = _fake_crate(heal=lambda *a: {"degrade": "unported_algorithm: X"})
        with capture_logs() as logs:
            assert _heal_call(monkeypatch, crate) is None
        falls = [e for e in logs if e["event"] == "lake_rust_waved.fallback"]
        assert [e["reason"] for e in falls] == ["heal_degrade"]
        assert falls[0]["degrade_reason"] == "unported_algorithm: X"
        assert not lrw.rust_waved_disabled()  # pre-mutation degrade never latches

    def test_good_outcome_is_returned_verbatim(self, monkeypatch: pytest.MonkeyPatch) -> None:
        outcome = _EMPTY_HEAL_OUTCOME()
        crate = _fake_crate(heal=lambda *a: outcome)
        assert _heal_call(monkeypatch, crate) is outcome

    def test_non_dict_outcome_is_refuse_poison_loud(self, monkeypatch: pytest.MonkeyPatch) -> None:
        crate = _fake_crate(heal=lambda *a: ["not", "a", "dict"])
        with pytest.raises(RuntimeError, match="refuse-poison"):
            _heal_call(monkeypatch, crate)

    def test_wrong_outcome_keys_are_refuse_poison_loud(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        crate = _fake_crate(heal=lambda *a: {"healed": 1})
        with pytest.raises(RuntimeError, match="outcome keys refused"):
            _heal_call(monkeypatch, crate)

    def test_crate_exception_propagates_loud(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(*args: Any) -> dict[str, Any]:
            raise OSError(28, "No space left on device")

        crate = _fake_crate(heal=_boom)
        with pytest.raises(OSError, match="No space left"):
            _heal_call(monkeypatch, crate)

    def test_arguments_cross_verbatim(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: dict[str, Any] = {}

        def _capture(
            lake_root: str, quarantine_root: str, findings: Any, on_event: Any
        ) -> dict[str, Any]:
            seen.update(
                lake_root=lake_root,
                quarantine_root=quarantine_root,
                findings=findings,
                on_event=on_event,
            )
            return _EMPTY_HEAL_OUTCOME()

        sentinel_findings = [{"kind": "IntraFileDup"}]
        sentinel_cb = lambda kind, payload: None  # noqa: E731
        _heal_call(
            monkeypatch,
            _fake_crate(heal=_capture),
            findings=sentinel_findings,
            on_event=sentinel_cb,
        )
        assert seen["lake_root"] == "/lake"
        assert seen["quarantine_root"] == "/lake/.quarantine/x"
        assert seen["findings"] is sentinel_findings
        assert seen["on_event"] is sentinel_cb


# ── W-2: the content-token surface ──────────────────────────────────────────


def _token_call(
    monkeypatch: pytest.MonkeyPatch,
    module: types.ModuleType | None,
    *,
    on_skip: Any = None,
    oracle: Any = None,
) -> str:
    _install_crate(monkeypatch, module)
    monkeypatch.setenv(lrw.LAKE_WAVED_RUST_ENV, "1")
    skips: list[tuple[str, str, str]] = []
    return lrw.lake_content_token_via_rust(
        lake_root="/lake",
        on_skip=(lambda e, p, err: skips.append((e, p, err))) if on_skip is None else on_skip,
        oracle=(lambda: "oracle-token") if oracle is None else oracle,
    )


class TestContentTokenSurface:
    """The §C-5 token twin — the full W-1 read-surface doctrine."""

    def test_absent_crate_serves_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert _token_call(monkeypatch, None) == "oracle-token"
        assert lrw.rust_waved_disabled()

    def test_degrade_error_serves_oracle_with_one_warn(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(lake_root: str) -> tuple[str, list[Any]]:
            raise RuntimeError("crate fault")

        with capture_logs() as logs:
            assert _token_call(monkeypatch, _fake_crate(token=_boom)) == "oracle-token"
        assert any(
            e["event"] == "lake_rust_waved.fallback" and e["reason"] == "token_error" for e in logs
        )
        assert not lrw.rust_waved_disabled()

    def test_memory_error_escapes_raw(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _oom(lake_root: str) -> tuple[str, list[Any]]:
            raise MemoryError

        with pytest.raises(MemoryError):
            _token_call(monkeypatch, _fake_crate(token=_oom))

    def test_panic_exception_latches_and_serves_oracle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import polars.exceptions as pl_exc

        def _panic(lake_root: str) -> tuple[str, list[Any]]:
            raise pl_exc.PanicException("boom")

        assert _token_call(monkeypatch, _fake_crate(token=_panic)) == "oracle-token"
        assert lrw.rust_waved_disabled()

    @pytest.mark.parametrize(
        "result",
        [
            "not-a-tuple",
            ("only-one",),
            (123, []),
            ("tok", "not-a-list"),
        ],
        ids=["non_tuple", "wrong_arity", "non_str_token", "non_list_events"],
    )
    def test_refuse_poison_shapes_serve_oracle(
        self, monkeypatch: pytest.MonkeyPatch, result: Any
    ) -> None:
        with capture_logs() as logs:
            assert _token_call(monkeypatch, _fake_crate(token=lambda lr: result)) == "oracle-token"
        assert any(
            e["event"] == "lake_rust_waved.fallback" and e["reason"] == "token_result_refused"
            for e in logs
        )

    @pytest.mark.parametrize(
        "item",
        [
            "not-a-dict",
            {"event": "e", "path": "p"},
            {"event": "e", "path": "p", "error": 5},
            {"event": "e", "path": "p", "error": "x", "extra": "y"},
        ],
        ids=["non_dict", "missing_key", "non_str_value", "extra_key"],
    )
    def test_poisoned_skip_channel_serves_oracle(
        self, monkeypatch: pytest.MonkeyPatch, item: Any
    ) -> None:
        with capture_logs() as logs:
            token = _token_call(monkeypatch, _fake_crate(token=lambda lr: ("t" * 64, [item])))
        assert token == "oracle-token"
        assert any(
            e["event"] == "lake_rust_waved.fallback" and e["reason"] == "token_events_refused"
            for e in logs
        )

    def test_burn_in_match_counts_down_and_completes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        lrw.reset_lake_rust_waved_for_tests(remaining=1)
        crate = _fake_crate(token=lambda lr: ("same-token", []))
        with capture_logs() as logs:
            token = _token_call(monkeypatch, crate, oracle=lambda: "same-token")
        assert token == "same-token"
        assert lrw._BURN_IN_REMAINING[lrw.SURFACE_CONTENT_TOKEN] == 0
        assert any(
            e["event"] == "lake_rust_waved.burn_in_complete"
            and e["surface"] == lrw.SURFACE_CONTENT_TOKEN
            for e in logs
        )
        assert not lrw.rust_waved_disabled()

    def test_burn_in_mismatch_autoreverts_and_ships_oracle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lrw.reset_lake_rust_waved_for_tests(remaining=lrw.BURN_IN_SAMPLES)
        crate = _fake_crate(token=lambda lr: ("rust-token", []))
        with capture_logs() as logs:
            token = _token_call(monkeypatch, crate, oracle=lambda: "python-token")
        assert token == "python-token"
        assert lrw.rust_waved_disabled()
        assert any(
            e["event"] == "lake_rust_waved.autoreverted" and e["reason"] == "burn_in_token_mismatch"
            for e in logs
        )

    def test_burn_in_never_replays_skip_events(self, monkeypatch: pytest.MonkeyPatch) -> None:
        lrw.reset_lake_rust_waved_for_tests(remaining=lrw.BURN_IN_SAMPLES)
        crate = _fake_crate(token=lambda lr: ("same", [{"event": "e", "path": "p", "error": "x"}]))
        replayed: list[tuple[str, str, str]] = []
        token = _token_call(
            monkeypatch,
            crate,
            on_skip=lambda e, p, err: replayed.append((e, p, err)),
            oracle=lambda: "same",
        )
        assert token == "same"
        assert replayed == []  # the oracle ran + warned itself — never doubled

    def test_steady_state_replays_skips_and_skips_oracle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lrw.reset_lake_rust_waved_for_tests(remaining=0)
        crate = _fake_crate(
            token=lambda lr: (
                "tok",
                [
                    {"event": "lake_heal.token_stat_skipped", "path": "/p1", "error": "e1"},
                    {"event": "lake_heal.token_walk_skipped", "path": "/p2", "error": "e2"},
                ],
            )
        )
        replayed: list[tuple[str, str, str]] = []

        def _bomb() -> str:
            raise AssertionError("oracle must not run in steady state")

        token = _token_call(
            monkeypatch,
            crate,
            on_skip=lambda e, p, err: replayed.append((e, p, err)),
            oracle=_bomb,
        )
        assert token == "tok"
        assert replayed == [
            ("lake_heal.token_stat_skipped", "/p1", "e1"),
            ("lake_heal.token_walk_skipped", "/p2", "e2"),
        ]


# ---------------------------------------------------------------------------
# W-3: the reconcile-file surface
# ---------------------------------------------------------------------------

#: 2024-06-03T04:01:00Z, epoch microseconds (the reconcile fixtures' anchor).
_JUNE_US = 1_717_387_260_000_000

#: A well-formed clean-file crate wire ``(row_count, first_ts_us, last_ts_us,
#: tz_aware, sha256_hex, dup, foreign_rows)``.
_RECON_CLEAN_WIRE = (2, _JUNE_US, _JUNE_US + 60_000_000, True, "a" * 64, None, 0)


def _recon_coords() -> Any:
    """The reconcile fixtures' partition coords (June 2024 home month)."""
    from brutex.data.lake_invariants import PartitionCoords

    return PartitionCoords(
        exchange="NSE",
        segment="INDEX",
        symbol="NIFTY",
        interval="1minute",
        year=2024,
        month=6,
    )


def _recon_scan(findings: tuple[Any, ...] = ()) -> tuple[Any, ...]:
    """The scan tuple matching :data:`_RECON_CLEAN_WIRE` (the oracle twin)."""
    from datetime import UTC, datetime, timedelta

    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    return (
        2,
        epoch + timedelta(microseconds=_JUNE_US),
        epoch + timedelta(microseconds=_JUNE_US + 60_000_000),
        "a" * 64,
        findings,
    )


#: "Use the default scan" sentinel for :class:`_CountingScanOracle` — a
#: plain ``None`` default would shadow the legitimate zero-row ``None`` scan.
_DEFAULT_SCAN = object()


class _CountingScanOracle:
    """Reconcile oracle stub counting invocations."""

    def __init__(self, result: Any = _DEFAULT_SCAN) -> None:
        self.calls = 0
        self._result = _recon_scan() if result is _DEFAULT_SCAN else result

    def __call__(self) -> Any:
        self.calls += 1
        return self._result


def _recon_call(
    monkeypatch: pytest.MonkeyPatch,
    module: types.ModuleType | None,
    *,
    coords: Any = None,
    oracle: Any = None,
) -> Any:
    _install_crate(monkeypatch, module)
    monkeypatch.setenv(lrw.LAKE_WAVED_RUST_ENV, "1")
    return lrw.reconcile_file_via_rust(
        path="/lake/bars/NSE/INDEX/NIFTY/1minute/2024/06.parquet",
        coords=coords,
        oracle=_CountingScanOracle() if oracle is None else oracle,
    )


class TestReconcileFileSurface:
    """The §B3/§C-3 per-file scan — the full W-1 read-surface doctrine."""

    def test_absent_crate_serves_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        oracle = _CountingScanOracle()
        assert _recon_call(monkeypatch, None, oracle=oracle) == _recon_scan()
        assert oracle.calls == 1
        assert lrw.rust_waved_disabled()

    def test_missing_w3_api_latches_crate_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The all-or-nothing lockstep: a W-2-era wheel (no waved_reconcile_file)
        # is a stale install — the WHOLE waved seam latches.
        module = _fake_crate()
        delattr(module, "waved_reconcile_file")
        with capture_logs() as logs:
            assert _recon_call(monkeypatch, module) == _recon_scan()
        assert lrw.rust_waved_disabled()
        assert any(
            e["event"] == "lake_rust_waved.rust_path_disabled"
            and e["reason"] == "crate_unavailable"
            for e in logs
        )

    def test_degrade_error_serves_oracle_with_one_warn(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(path: str, home: Any) -> Any:
            raise ValueError("brutex-lake waved reconcile failed: corrupt")

        with capture_logs() as logs:
            assert _recon_call(monkeypatch, _fake_crate(reconcile=_boom)) == _recon_scan()
        assert any(
            e["event"] == "lake_rust_waved.fallback" and e["reason"] == "reconcile_error"
            for e in logs
        )
        assert not lrw.rust_waved_disabled()

    def test_oracle_raise_after_crate_refusal_propagates(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The degrade-to-oracle law end-to-end: the crate refuses, the oracle
        # re-raises the REAL skip-class exception — the caller's GAP-#9 guard
        # owns the verdict (production-identical on a corrupt file).
        def _boom(path: str, home: Any) -> Any:
            raise ValueError("brutex-lake waved reconcile failed: corrupt")

        def _oracle() -> Any:
            raise OverflowError("date value out of range")

        with pytest.raises(OverflowError):
            _recon_call(monkeypatch, _fake_crate(reconcile=_boom), oracle=_oracle)

    def test_memory_error_escapes_raw(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _oom(path: str, home: Any) -> Any:
            raise MemoryError

        with pytest.raises(MemoryError):
            _recon_call(monkeypatch, _fake_crate(reconcile=_oom))

    def test_panic_exception_latches_and_serves_oracle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import polars.exceptions as pl_exc

        def _panic(path: str, home: Any) -> Any:
            raise pl_exc.PanicException("boom")

        assert _recon_call(monkeypatch, _fake_crate(reconcile=_panic)) == _recon_scan()
        assert lrw.rust_waved_disabled()

    @pytest.mark.parametrize(
        "wire",
        [
            "not-a-tuple",
            (1, 2, 3),
            (True, _JUNE_US, _JUNE_US, True, "a" * 64, None, 0),
            (2, "x", _JUNE_US, True, "a" * 64, None, 0),
            (0, _JUNE_US, _JUNE_US, True, "a" * 64, None, 0),
            (2, _JUNE_US, _JUNE_US, True, "a" * 64, None, 3),
            (2, _JUNE_US + 1, _JUNE_US, True, "a" * 64, None, 0),
            (2, _JUNE_US, _JUNE_US, "yes", "a" * 64, None, 0),
            (2, _JUNE_US, _JUNE_US, True, "short", None, 0),
            (2, _JUNE_US, _JUNE_US, True, 1234, None, 0),
            (2, _JUNE_US, _JUNE_US, True, "a" * 64, (2,), 0),
            (2, _JUNE_US, _JUNE_US, True, "a" * 64, (2, True), 0),
            (2, _JUNE_US, _JUNE_US, True, "a" * 64, (3, 1), 0),
            (2, _JUNE_US, _JUNE_US, True, "a" * 64, (2, 2), 0),
            (2, 2**62, 2**62 + 1, True, "a" * 64, None, 0),
        ],
        ids=[
            "non_tuple",
            "wrong_arity",
            "bool_row_count",
            "non_int_first",
            "zero_rows_nonnull",
            "foreign_gt_rows",
            "inverted_span",
            "non_bool_tz",
            "short_hash",
            "non_str_hash",
            "dup_wrong_arity",
            "dup_bool_member",
            "dup_physical_drift",
            "dup_not_greater",
            "span_past_datetime_range",
        ],
    )
    def test_refuse_poison_shapes_serve_oracle(
        self, monkeypatch: pytest.MonkeyPatch, wire: Any
    ) -> None:
        with capture_logs() as logs:
            result = _recon_call(monkeypatch, _fake_crate(reconcile=lambda p, h: wire))
        assert result == _recon_scan()
        assert any(
            e["event"] == "lake_rust_waved.fallback" and e["reason"] == "reconcile_result_refused"
            for e in logs
        )
        assert not lrw.rust_waved_disabled()

    def test_zero_row_none_passes_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Burn-in arm: crate None vs oracle None — equal, counts down.
        lrw.reset_lake_rust_waved_for_tests(remaining=1)
        oracle = _CountingScanOracle(result=None)
        crate = _fake_crate(reconcile=lambda p, h: None)
        assert _recon_call(monkeypatch, crate, oracle=oracle) is None
        assert oracle.calls == 1
        assert not lrw.rust_waved_disabled()
        # Steady state: the oracle never runs, None served from the crate.
        lrw.reset_lake_rust_waved_for_tests(remaining=0)

        def _bomb() -> Any:
            raise AssertionError("oracle must not run in steady state")

        assert _recon_call(monkeypatch, crate, oracle=_bomb) is None

    def test_burn_in_match_counts_down_and_completes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        lrw.reset_lake_rust_waved_for_tests(remaining=2)
        with capture_logs() as first_logs:
            assert _recon_call(monkeypatch, _fake_crate()) == _recon_scan()
        # 2 → 1: counted down, NOT complete yet.
        assert lrw._BURN_IN_REMAINING[lrw.SURFACE_RECONCILE_FILE] == 1
        assert not any(e["event"] == "lake_rust_waved.burn_in_complete" for e in first_logs)
        with capture_logs() as logs:
            assert _recon_call(monkeypatch, _fake_crate()) == _recon_scan()
        assert lrw._BURN_IN_REMAINING[lrw.SURFACE_RECONCILE_FILE] == 0
        assert any(
            e["event"] == "lake_rust_waved.burn_in_complete"
            and e["surface"] == lrw.SURFACE_RECONCILE_FILE
            for e in logs
        )
        assert not lrw.rust_waved_disabled()

    def test_burn_in_mismatch_autoreverts_and_ships_oracle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The sha256 tooth: a hash-divergent scan autoreverts the WHOLE seam.
        lrw.reset_lake_rust_waved_for_tests(remaining=lrw.BURN_IN_SAMPLES)
        divergent = (2, _JUNE_US, _JUNE_US + 60_000_000, True, "b" * 64, None, 0)
        oracle = _CountingScanOracle()
        with capture_logs() as logs:
            result = _recon_call(
                monkeypatch, _fake_crate(reconcile=lambda p, h: divergent), oracle=oracle
            )
        assert result == _recon_scan()  # the ORACLE scan shipped
        assert lrw.rust_waved_disabled()
        assert any(
            e["event"] == "lake_rust_waved.autoreverted"
            and e["reason"] == "burn_in_reconcile_mismatch"
            for e in logs
        )

    def test_burn_in_none_vs_scan_mismatch_autoreverts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lrw.reset_lake_rust_waved_for_tests(remaining=lrw.BURN_IN_SAMPLES)
        oracle = _CountingScanOracle()  # a real scan; the crate says zero-row
        result = _recon_call(monkeypatch, _fake_crate(reconcile=lambda p, h: None), oracle=oracle)
        assert result == _recon_scan()
        assert lrw.rust_waved_disabled()

    def test_oracle_raise_during_burn_in_propagates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        lrw.reset_lake_rust_waved_for_tests(remaining=lrw.BURN_IN_SAMPLES)

        def _oracle() -> Any:
            raise OverflowError("date value out of range")

        with pytest.raises(OverflowError):
            _recon_call(monkeypatch, _fake_crate(), oracle=_oracle)

    def test_unseen_shape_token_compares_after_budget_spent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lrw.reset_lake_rust_waved_for_tests(remaining=1)
        # Burn the whole budget on the classify=False shape.
        first = _CountingScanOracle()
        assert _recon_call(monkeypatch, _fake_crate(), oracle=first) == _recon_scan()
        assert first.calls == 1
        assert lrw._BURN_IN_REMAINING[lrw.SURFACE_RECONCILE_FILE] == 0
        # classify=True is an UNSEEN shape — the oracle is still consulted.
        second = _CountingScanOracle()
        assert _recon_call(monkeypatch, _fake_crate(), coords=_recon_coords(), oracle=second) == (
            _recon_scan()
        )
        assert second.calls == 1
        # A repeat classify=True shape is steady state — no oracle.
        third = _CountingScanOracle()
        assert _recon_call(monkeypatch, _fake_crate(), coords=_recon_coords(), oracle=third) == (
            _recon_scan()
        )
        assert third.calls == 0

    def test_steady_state_never_calls_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        lrw.reset_lake_rust_waved_for_tests(remaining=0)

        def _bomb() -> Any:
            raise AssertionError("oracle must not run in steady state")

        assert _recon_call(monkeypatch, _fake_crate(), oracle=_bomb) == _recon_scan()

    def test_findings_reconstruction_matches_classify_law(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Steady state: dup + foreign counts rebuild the finding OBJECTS —
        # relpath-stamped, coords-carrying, dup-then-foreign order,
        # overlap_rows=None (the free L1 path).
        from pathlib import Path

        from brutex.data.lake_invariants import ForeignMonthRows, IntraFileDup

        lrw.reset_lake_rust_waved_for_tests(remaining=0)
        coords = _recon_coords()
        wire = (3, _JUNE_US, _JUNE_US + 60_000_000, True, "a" * 64, (3, 2), 1)
        result = _recon_call(
            monkeypatch, _fake_crate(reconcile=lambda p, h: wire), coords=coords, oracle=object
        )
        assert result is not None
        rel = Path(coords.relpath())
        assert result[4] == (
            IntraFileDup(path=rel, coords=coords, physical_rows=3, unique_keys=2),
            ForeignMonthRows(path=rel, coords=coords, foreign_rows=1, overlap_rows=None),
        )

    def test_no_coords_builds_no_findings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # check_invariants=False: even a count-carrying wire builds NOTHING
        # (the classification is the caller's choice, not the crate's).
        lrw.reset_lake_rust_waved_for_tests(remaining=0)
        wire = (3, _JUNE_US, _JUNE_US + 60_000_000, True, "a" * 64, (3, 2), 1)
        result = _recon_call(
            monkeypatch, _fake_crate(reconcile=lambda p, h: wire), coords=None, oracle=object
        )
        assert result is not None
        assert result[4] == ()

    def test_naive_column_builds_naive_datetimes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from datetime import datetime, timedelta

        lrw.reset_lake_rust_waved_for_tests(remaining=0)
        wire = (2, _JUNE_US, _JUNE_US + 60_000_000, False, "a" * 64, None, 0)
        result = _recon_call(monkeypatch, _fake_crate(reconcile=lambda p, h: wire), oracle=object)
        assert result is not None
        expected = datetime(1970, 1, 1) + timedelta(microseconds=_JUNE_US)
        assert result[1] == expected
        assert result[1].tzinfo is None and result[2].tzinfo is None

    def test_arguments_cross_verbatim(self, monkeypatch: pytest.MonkeyPatch) -> None:
        lrw.reset_lake_rust_waved_for_tests(remaining=0)
        seen: list[tuple[Any, Any]] = []

        def _record(path: str, home: Any) -> Any:
            seen.append((path, home))
            return _RECON_CLEAN_WIRE

        _recon_call(monkeypatch, _fake_crate(reconcile=_record), coords=_recon_coords())
        _recon_call(monkeypatch, _fake_crate(reconcile=_record), coords=None)
        assert seen == [
            ("/lake/bars/NSE/INDEX/NIFTY/1minute/2024/06.parquet", (2024, 6)),
            ("/lake/bars/NSE/INDEX/NIFTY/1minute/2024/06.parquet", None),
        ]


# ---------------------------------------------------------------------------
# W-4 — the lake_invariants whole-lake scan surface
# ---------------------------------------------------------------------------


def _scan_clean_wire(**overrides: Any) -> dict[str, Any]:
    """A fresh clean-lake scan wire dict (the crate wire shape)."""
    wire: dict[str, Any] = {
        "findings": [],
        "drift_insert_at": 0,
        "events": [{"kind": "drift"}],
        "files_scanned": 0,
        "files_skipped": 0,
        "series_scanned": 0,
        "option_files_scanned": 0,
        "option_files_skipped": 0,
        "files_by_segment": [],
        "engine_files": 0,
        "engine_files_skipped": 0,
        "first_skipped_parse": None,
        "first_skipped_read": None,
        "symbol_dirs": [],
        "reads_performed": 0,
    }
    wire.update(overrides)
    return wire


def _scan_coords_wire(**overrides: Any) -> dict[str, Any]:
    """A bars finding record's coords fields (the crate wire shape)."""
    rec: dict[str, Any] = {
        "exchange": "NSE",
        "segment": "INDEX",
        "symbol": "NIFTY",
        "interval": "1minute",
        "year": 2024,
        "month": 6,
    }
    rec.update(overrides)
    return rec


def _empty_scan_report(**overrides: Any) -> Any:
    """A ``LakeScanReport`` with every counter zero (the clean-wire twin)."""
    from brutex.data.lake_invariants import LakeScanReport

    base: dict[str, Any] = {
        "findings": (),
        "files_scanned": 0,
        "files_skipped": 0,
        "series_scanned": 0,
        "option_files_scanned": 0,
        "option_files_skipped": 0,
        "duration_s": 0.123,
        "files_by_segment": (),
        "engine_files": 0,
        "engine_files_skipped": 0,
        "first_skipped_path": None,
    }
    base.update(overrides)
    return LakeScanReport(**base)


class _CountingWholeLakeOracle:
    """Scan-oracle stub counting invocations (returns an empty report)."""

    def __init__(self, result: Any = None) -> None:
        self.calls = 0
        self._result = result if result is not None else _empty_scan_report()

    def __call__(self) -> Any:
        self.calls += 1
        return self._result


def _scan_call(
    monkeypatch: pytest.MonkeyPatch,
    module: types.ModuleType | None,
    *,
    oracle: Any = None,
    months: Any = None,
    include_bars: bool = True,
    include_options: bool = True,
    canonical_keys: Any = None,
    per_file_pause: Any = None,
) -> Any:
    from pathlib import Path

    _install_crate(monkeypatch, module)
    return lrw.scan_lake_via_rust(
        Path("/tmp/waved-scan-lake"),
        exchange=None,
        segment=None,
        symbol=None,
        interval=None,
        months=months,
        include_options=include_options,
        include_bars=include_bars,
        canonical_keys=canonical_keys,
        per_file_pause=per_file_pause,
        oracle=oracle if oracle is not None else _CountingWholeLakeOracle(),
    )


class TestScanWireValidation:
    """Refuse-poison over the scan wire (every arm raises ``ValueError``)."""

    def test_all_seven_finding_kinds_reconstruct(self) -> None:
        from pathlib import Path

        from brutex.data import lake_invariants as li

        coords_rec = _scan_coords_wire()
        records: list[dict[str, Any]] = [
            {
                "kind": "bars_dup",
                "path": "/l/a",
                **coords_rec,
                "physical_rows": 4,
                "unique_keys": 3,
            },
            {
                "kind": "bars_foreign",
                "path": "/l/b",
                **coords_rec,
                "foreign_rows": 2,
                "overlap_rows": 1,
            },
            {"kind": "price", "path": "/l/c", **coords_rec, "bad_rows": 5},
            {
                "kind": "alien_bars",
                "path": "/l/d",
                **coords_rec,
                "reason": "missing column(s): open",
            },
            {"kind": "alien_option", "path": "/l/e", "series": "S", "reason": "r"},
            {
                "kind": "option_dup",
                "path": "/l/f",
                "series": "S",
                "physical_rows": 3,
                "unique_keys": 2,
            },
            {
                "kind": "option_foreign",
                "path": "/l/g",
                "series": "S",
                "year": 2024,
                "month": 7,
                "foreign_rows": 1,
                "overlap_rows": None,
            },
        ]
        built = [lrw._scan_finding_from_wire(rec) for rec in records]
        coords = li.PartitionCoords(
            exchange="NSE", segment="INDEX", symbol="NIFTY", interval="1minute", year=2024, month=6
        )
        assert built[0] == li.IntraFileDup(
            path=Path("/l/a"), coords=coords, physical_rows=4, unique_keys=3
        )
        assert built[1] == li.ForeignMonthRows(
            path=Path("/l/b"), coords=coords, foreign_rows=2, overlap_rows=1
        )
        assert built[2] == li.PriceSanityRows(path=Path("/l/c"), coords=coords, bad_rows=5)
        assert built[3] == li.SchemaAlienFile(
            path=Path("/l/d"), reason="missing column(s): open", coords=coords
        )
        assert built[4] == li.SchemaAlienFile(path=Path("/l/e"), reason="r", series="S")
        assert built[5] == li.OptionIntraFileDup(
            path=Path("/l/f"), series="S", physical_rows=3, unique_keys=2
        )
        assert built[6] == li.OptionForeignMonthRows(
            path=Path("/l/g"), series="S", year=2024, month=7, foreign_rows=1, overlap_rows=None
        )

    @pytest.mark.parametrize(
        "rec",
        [
            "not-a-mapping",
            {"kind": "unknown", "path": "/l/x"},
            {"kind": "bars_dup", "path": 7},
            {"kind": "bars_dup", "path": "/l/a", "exchange": 1},
            {
                "kind": "bars_dup",
                "path": "/l/a",
                "exchange": "NSE",
                "segment": "INDEX",
                "symbol": "N",
                "interval": "1minute",
                "year": "2024",
                "month": 6,
            },
            {
                "kind": "bars_dup",
                "path": "/l/a",
                **{
                    "exchange": "NSE",
                    "segment": "INDEX",
                    "symbol": "N",
                    "interval": "1minute",
                    "year": 2024,
                    "month": 6,
                },
                "physical_rows": True,
                "unique_keys": 1,
            },
            {
                "kind": "option_dup",
                "path": "/l/f",
                "series": "S",
                "physical_rows": -1,
                "unique_keys": 1,
            },
            {
                "kind": "option_foreign",
                "path": "/l/g",
                "series": "S",
                "year": 2024,
                "month": 7,
                "foreign_rows": 1,
                "overlap_rows": True,
            },
            {
                "kind": "option_foreign",
                "path": "/l/g",
                "series": "S",
                "year": 2024,
                "month": 7,
                "foreign_rows": 1,
                "overlap_rows": -2,
            },
        ],
    )
    def test_junk_finding_records_refuse(self, rec: Any) -> None:
        with pytest.raises(ValueError, match="scan|unknown"):
            lrw._scan_finding_from_wire(rec)

    @pytest.mark.parametrize(
        ("events", "include_bars", "match"),
        [
            ("junk", True, "must be a sequence"),
            (["rec"], True, "must be a mapping"),
            ([{"kind": "bogus"}], True, "unknown scan event kind"),
            (
                [
                    {
                        "kind": "log",
                        "event": "other.event",
                        "path": "p",
                        "path_repr": False,
                        "error": None,
                        "error_repr": False,
                    }
                ],
                True,
                "unknown scan log event",
            ),
            (
                [
                    {
                        "kind": "log",
                        "event": "lake.invariant.scan_skip",
                        "path": "p",
                        "path_repr": "no",
                        "error": None,
                        "error_repr": False,
                    }
                ],
                True,
                "repr flags must be bools",
            ),
            (
                [
                    {
                        "kind": "log",
                        "event": "lake.invariant.scan_skip",
                        "path": "p",
                        "path_repr": False,
                        "error": 7,
                        "error_repr": False,
                    }
                ],
                True,
                "error must be None or a string",
            ),
            ([{"kind": "finding", "index": 3}], True, "index out of range"),
            ([], True, "drift marker count"),
            ([{"kind": "drift"}], False, "drift marker count"),
        ],
    )
    def test_junk_event_streams_refuse(self, events: Any, include_bars: bool, match: str) -> None:
        with pytest.raises(ValueError, match=match):
            lrw._scan_events_from_wire(events, finding_count=1, include_bars=include_bars)

    @pytest.mark.parametrize(
        ("mutate", "match"),
        [
            (lambda w: "junk", "must be a mapping"),
            (lambda w: {**w, "findings": "junk"}, "findings must be a sequence"),
            (lambda w: {**w, "drift_insert_at": 9}, "drift_insert_at out of range"),
            (lambda w: {**w, "files_scanned": -1}, "non-negative int"),
            (lambda w: {**w, "reads_performed": True}, "non-negative int"),
            (lambda w: {**w, "files_by_segment": "junk"}, "files_by_segment must be a sequence"),
            (lambda w: {**w, "files_by_segment": [("INDEX",)]}, "must be .segment, count."),
            (lambda w: {**w, "files_by_segment": [(1, 2)]}, "must be .segment, count."),
            (lambda w: {**w, "files_by_segment": [("INDEX", -1)]}, "count must be a non-negative"),
            (lambda w: {**w, "first_skipped_parse": 7}, "None or a string"),
            (lambda w: {**w, "symbol_dirs": "junk"}, "symbol_dirs must be a sequence"),
            (lambda w: {**w, "symbol_dirs": [("NSE",)]}, "exchange, segment, symbols"),
            (lambda w: {**w, "symbol_dirs": [(1, "INDEX", [])]}, "keys must be strings"),
            (lambda w: {**w, "symbol_dirs": [("NSE", "INDEX", "N")]}, "names must be a sequence"),
            (lambda w: {**w, "symbol_dirs": [("NSE", "INDEX", [7])]}, "names must be strings"),
        ],
    )
    def test_junk_wire_shapes_refuse(self, mutate: Any, match: str) -> None:
        from pathlib import Path

        with pytest.raises(ValueError, match=match):
            lrw._scan_report_from_wire(
                mutate(_scan_clean_wire()),
                lake_root=Path("/tmp/waved-scan-lake"),
                canonical_keys=None,
                include_bars=True,
            )

    def test_include_bars_false_requires_zero_drift_at(self) -> None:
        from pathlib import Path

        wire = _scan_clean_wire(
            findings=[
                {
                    "kind": "option_dup",
                    "path": "/l/f",
                    "series": "S",
                    "physical_rows": 3,
                    "unique_keys": 2,
                }
            ],
            drift_insert_at=1,
            events=[{"kind": "finding", "index": 0}],
        )
        with pytest.raises(ValueError, match="drift_insert_at out of range"):
            lrw._scan_report_from_wire(
                wire, lake_root=Path("/l"), canonical_keys=None, include_bars=False
            )

    def test_first_skipped_parse_beats_read(self) -> None:
        from pathlib import Path

        wire = _scan_clean_wire(first_skipped_parse="/l/p", first_skipped_read="/l/r")
        report, _events, _at, _n, _reads = lrw._scan_report_from_wire(
            wire, lake_root=Path("/l"), canonical_keys=None, include_bars=True
        )
        assert report.first_skipped_path == "/l/p"
        wire = _scan_clean_wire(first_skipped_read="/l/r")
        report, *_rest = lrw._scan_report_from_wire(
            wire, lake_root=Path("/l"), canonical_keys=None, include_bars=True
        )
        assert report.first_skipped_path == "/l/r"

    def test_reports_equal_pops_only_the_wall_clock(self) -> None:
        left = _empty_scan_report(duration_s=1.0)
        right = _empty_scan_report(duration_s=2.0)
        assert lrw._scan_reports_equal(left, right)
        assert not lrw._scan_reports_equal(left, _empty_scan_report(files_scanned=1))


class TestScanReplay:
    """The ordered event replay through the Python sinks."""

    def test_replay_covers_every_event_shape(self) -> None:
        from pathlib import Path

        from brutex.data import lake_invariants as li

        coords = li.PartitionCoords(
            exchange="NSE", segment="INDEX", symbol="NIFTY", interval="1minute", year=2024, month=6
        )
        drift = li.LegacyKeyDrift(
            exchange="NSE",
            segment="CASH",
            drifted_symbol="NSE-M&M",
            canonical_symbol="M&M",
            drifted_dir=Path("/l/bars/NSE/CASH/NSE-M&M"),
            intervals=("1minute",),
            files=1,
        )
        findings: tuple[Any, ...] = (
            li.IntraFileDup(path=Path("/l/a"), coords=coords, physical_rows=4, unique_keys=3),
            drift,
            li.OptionIntraFileDup(path=Path("/l/f"), series="S", physical_rows=3, unique_keys=2),
        )
        events: tuple[dict[str, Any], ...] = (
            {
                "kind": "log",
                "event": "lake.invariant.scan_skip",
                "path": "/l/raw",
                "path_repr": False,
                "error": "boom",
                "error_repr": False,
            },
            {
                "kind": "log",
                "event": "lake.invariant.symlink_skipped",
                "path": "/l/reprd",
                "path_repr": True,
                "error": None,
                "error_repr": False,
            },
            {
                "kind": "log",
                "event": "lake.invariant.scan_skip",
                "path": "/l/opt",
                "path_repr": True,
                "error": "polars boom",
                "error_repr": True,
            },
            {"kind": "finding", "index": 0},
            {"kind": "drift"},
            {"kind": "finding", "index": 1},  # crate index 1 = post-drift shift
        )
        with capture_logs() as logs:
            lrw._replay_scan_events(events, findings, drift_at=1, drift_len=1)
        by_event = [(rec["event"], rec) for rec in logs]
        assert by_event[0][0] == "lake.invariant.scan_skip"
        assert by_event[0][1]["path"] == "/l/raw" and by_event[0][1]["error"] == "boom"
        assert by_event[1][0] == "lake.invariant.symlink_skipped"
        assert by_event[1][1]["path"] == repr("/l/reprd")
        assert "error" not in by_event[1][1]
        assert by_event[2][1]["path"] == repr("/l/opt")
        assert by_event[2][1]["error"] == repr("polars boom")
        assert by_event[3][0] == "lake.invariant.intra_file_dup"
        assert by_event[4][0] == "lake.invariant.legacy_key_drift"
        assert by_event[5][0] == "lake.invariant.option_intra_file_dup"


class TestScanLakeSurface:
    """The routed whole-lake scan — guards, burn-in, steady state, pause."""

    def test_absent_crate_serves_the_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        oracle = _CountingWholeLakeOracle()
        report = _scan_call(monkeypatch, None, oracle=oracle)
        assert oracle.calls == 1 and report is oracle._result

    def test_degrade_error_serves_the_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _refuse(*_args: Any) -> Any:
            raise ValueError("brutex-lake waved scan degraded: int()-ambiguous")

        oracle = _CountingWholeLakeOracle()
        with capture_logs() as logs:
            report = _scan_call(monkeypatch, _fake_crate(scan=_refuse), oracle=oracle)
        assert oracle.calls == 1 and report is oracle._result
        assert any(rec.get("reason") == "scan_error" for rec in logs)
        assert not lrw.rust_waved_disabled()

    def test_escape_errors_reraise(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _interrupt(*_args: Any) -> Any:
            raise KeyboardInterrupt

        oracle = _CountingWholeLakeOracle()
        with pytest.raises(KeyboardInterrupt):
            _scan_call(monkeypatch, _fake_crate(scan=_interrupt), oracle=oracle)
        assert oracle.calls == 0

    def test_panic_belt_disables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _FakePanicException(BaseException):
            pass

        panic_module = types.ModuleType("pyo3_runtime")
        panic_module.PanicException = _FakePanicException  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "pyo3_runtime", panic_module)

        def _panic(*_args: Any) -> Any:
            raise _FakePanicException("thread panicked")

        oracle = _CountingWholeLakeOracle()
        report = _scan_call(monkeypatch, _fake_crate(scan=_panic), oracle=oracle)
        assert oracle.calls == 1 and report is oracle._result
        assert lrw.rust_waved_disabled()

    def test_refused_wire_serves_the_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        oracle = _CountingWholeLakeOracle()
        with capture_logs() as logs:
            report = _scan_call(
                monkeypatch, _fake_crate(scan=lambda *a: {"findings": "junk"}), oracle=oracle
            )
        assert oracle.calls == 1 and report is oracle._result
        assert any(rec.get("reason") == "scan_result_refused" for rec in logs)

    def test_burn_in_match_serves_the_oracle_report(self, monkeypatch: pytest.MonkeyPatch) -> None:
        oracle = _CountingWholeLakeOracle(_empty_scan_report())
        report = _scan_call(monkeypatch, _fake_crate(), oracle=oracle)
        assert oracle.calls == 1 and report is oracle._result
        assert lrw._BURN_IN_REMAINING[lrw.SURFACE_SCAN] == lrw.BURN_IN_SAMPLES - 1
        assert not lrw.rust_waved_disabled()
        # Burn-in never publishes the reads counter (the oracle served).
        assert lrw.last_rust_scan_reads() is None

    def test_burn_in_mismatch_autoreverts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        oracle = _CountingWholeLakeOracle(_empty_scan_report(files_scanned=7))
        with capture_logs() as logs:
            report = _scan_call(monkeypatch, _fake_crate(), oracle=oracle)
        assert report is oracle._result
        assert lrw.rust_waved_disabled()
        assert any(rec.get("reason") == "burn_in_scan_mismatch" for rec in logs)

    def test_burn_in_completion_logs_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        lrw.reset_lake_rust_waved_for_tests(remaining=1)
        # The reset disarms the shape gate at remaining<=0 only; here one
        # budget slot remains, so the next engaged call completes burn-in.
        with capture_logs() as logs:
            _scan_call(monkeypatch, _fake_crate(), oracle=_CountingWholeLakeOracle())
        assert any(rec["event"] == "lake_rust_waved.burn_in_complete" for rec in logs)
        assert lrw._BURN_IN_REMAINING[lrw.SURFACE_SCAN] == 0

    def test_unseen_shape_token_compares_past_the_budget(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lrw.reset_lake_rust_waved_for_tests(remaining=1)
        oracle_a = _CountingWholeLakeOracle()
        _scan_call(monkeypatch, _fake_crate(), oracle=oracle_a)
        assert lrw._BURN_IN_REMAINING[lrw.SURFACE_SCAN] == 0
        # A NEW shape (options-only) still runs the comparator (oracle called)
        # even with the count budget spent.
        oracle_b = _CountingWholeLakeOracle()
        wire = _scan_clean_wire(events=[])
        report = _scan_call(
            monkeypatch,
            _fake_crate(scan=lambda *a: wire),
            oracle=oracle_b,
            include_bars=False,
        )
        assert oracle_b.calls == 1 and report is oracle_b._result

    def test_steady_state_serves_the_crate_report(self, monkeypatch: pytest.MonkeyPatch) -> None:
        lrw.reset_lake_rust_waved_for_tests(remaining=0)
        wire = _scan_clean_wire(
            findings=[
                {
                    "kind": "option_dup",
                    "path": "/l/f",
                    "series": "S",
                    "physical_rows": 3,
                    "unique_keys": 2,
                }
            ],
            events=[
                {"kind": "drift"},
                {
                    "kind": "log",
                    "event": "lake.invariant.symlink_skipped",
                    "path": "/l/link",
                    "path_repr": True,
                    "error": None,
                    "error_repr": False,
                },
                {"kind": "finding", "index": 0},
            ],
            option_files_scanned=1,
            reads_performed=3,
        )
        oracle = _CountingWholeLakeOracle()
        with capture_logs() as logs:
            report = _scan_call(monkeypatch, _fake_crate(scan=lambda *a: wire), oracle=oracle)
        assert oracle.calls == 0
        assert report.option_files_scanned == 1 and len(report.findings) == 1
        assert report.duration_s > 0.0
        assert lrw.last_rust_scan_reads() == 3
        names = [rec["event"] for rec in logs]
        assert "lake.invariant.symlink_skipped" in names
        assert "lake.invariant.option_intra_file_dup" in names
        assert names[-1] == "lake.invariant.scan_complete"

    def test_arguments_cross_with_vocabularies(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from brutex.data import partition as partition_mod
        from brutex.data.lake_invariants import ENGINE_INDEX_COORDS

        lrw.reset_lake_rust_waved_for_tests(remaining=0)
        seen: list[tuple[Any, ...]] = []

        def _record(*args: Any) -> Any:
            seen.append(args)
            return _scan_clean_wire()

        _scan_call(
            monkeypatch,
            _fake_crate(scan=_record),
            months=[(2024, 6)],
        )
        (args,) = seen
        assert args[0] == "/tmp/waved-scan-lake"
        assert args[5] == [(2024, 6)]
        assert args[6] is True and args[7] is True
        assert args[8] == sorted(partition_mod.VALID_EXCHANGES)
        assert args[9] == sorted(partition_mod.VALID_SEGMENTS)
        assert args[10] == sorted(partition_mod.VALID_INTERVALS)
        assert set(args[11]) == set(partition_mod._FORBIDDEN_SYMBOL_CHARS)
        assert args[12] == [tuple(c) for c in ENGINE_INDEX_COORDS]
        assert args[13] is None  # no pause callback

    def test_raising_pause_reraises_verbatim(self, monkeypatch: pytest.MonkeyPatch) -> None:
        lrw.reset_lake_rust_waved_for_tests(remaining=0)
        marker = RuntimeError("throttle exploded")

        def _pause() -> None:
            raise marker

        def _tick_once(*args: Any) -> Any:
            args[13]()  # the wrapped pause callback
            raise AssertionError("unreachable — the wrap raises")

        oracle = _CountingWholeLakeOracle()
        with pytest.raises(RuntimeError) as caught:
            _scan_call(
                monkeypatch, _fake_crate(scan=_tick_once), oracle=oracle, per_file_pause=_pause
            )
        assert caught.value is marker
        assert oracle.calls == 0  # NEVER folded into the degrade set

    def test_pause_escape_without_capture_is_defensive(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lrw.reset_lake_rust_waved_for_tests(remaining=0)

        def _fake_escape(*_args: Any) -> Any:
            raise lrw._PauseEscape

        with pytest.raises(RuntimeError, match="pause escape without a captured exception"):
            _scan_call(
                monkeypatch,
                _fake_crate(scan=_fake_escape),
                per_file_pause=lambda: None,
            )

    def test_reset_clears_the_reads_counter(self, monkeypatch: pytest.MonkeyPatch) -> None:
        lrw.reset_lake_rust_waved_for_tests(remaining=0)
        _scan_call(monkeypatch, _fake_crate(scan=lambda *a: _scan_clean_wire(reads_performed=9)))
        assert lrw.last_rust_scan_reads() == 9
        lrw.reset_lake_rust_waved_for_tests()
        assert lrw.last_rust_scan_reads() is None

    def test_populated_segments_and_symbol_dirs_reconstruct(self) -> None:
        from pathlib import Path

        wire = _scan_clean_wire(
            files_scanned=2,
            files_by_segment=[("CASH", 1), ("INDEX", 1)],
            symbol_dirs=[("NSE", "CASH", ["M&M", "NSE-M&M"]), ("NSE", "INDEX", ["NIFTY"])],
        )
        report, _events, drift_at, drift_len, _reads = lrw._scan_report_from_wire(
            wire, lake_root=Path("/tmp/waved-scan-lake"), canonical_keys=None, include_bars=True
        )
        assert report.files_by_segment == (("CASH", 1), ("INDEX", 1))
        # The drift fold ran over the crate-enumerated dirs: NSE-M&M drifts
        # against its sibling M&M (the lake_keys ladder — Python-side).
        assert drift_at == 0 and drift_len == 1
        from brutex.data.lake_invariants import LegacyKeyDrift

        drift = report.findings[0]
        assert isinstance(drift, LegacyKeyDrift)
        assert drift.drifted_symbol == "NSE-M&M" and drift.canonical_symbol == "M&M"
