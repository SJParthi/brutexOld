"""Unit suite for ``brutex.data.lake_rust_waved`` — 100% line+branch standalone.

W-6a ENDGAME (DEC-RUST-LAKE-WAVED-001): the Wave-D Python cores are DELETED,
so the facade is fail-LOUD — an absent/stale/partial ``brutex_lake`` wheel
raises the typed :class:`LakeRustWavedUnavailableError` (never a silent
degrade; there is no oracle left to serve), a SET ``$BRUTEX_LAKE_WAVED_RUST``
(ANY value, ``1`` and ``0`` included) refuses via
:class:`LakeRustWavedFlagRemovedError` (the ``$BRUTEX_FILL_MODE`` retirement
precedent), and every out-of-envelope shape maps to its surface's ISOLATED
error class (read → ``ResampleInputError``; reconcile → ``ValueError`` in
the caller's GAP-#9 skip set; heal/scan → crate-owned isolation, so a
facade-level raise there is refuse-poison only).

Every test injects a FAKE ``brutex_lake`` module straight into
``sys.modules`` (or a poisoned ``None`` entry for the import-guard arm), so
the suite runs with NO wheel installed — the real-wheel behavior pins live
in the ``tests/integration/test_waved_*_differential.py`` frozen-expectation
suites + the #1916 poison-corpus harness.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import io
import sys
import types
from typing import TYPE_CHECKING, Any

import polars as pl
import pytest
from brutex.data import lake_rust_waved as lrw
from brutex.data.errors import (
    LakeRustWavedFlagRemovedError,
    LakeRustWavedUnavailableError,
    ResampleInputError,
)
from structlog.testing import capture_logs

if TYPE_CHECKING:
    from collections.abc import Iterator

# ---------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _fresh_process_view(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Reset the reads counter + guarantee the retired flag is unset."""
    monkeypatch.delenv(lrw.LAKE_WAVED_RUST_ENV, raising=False)
    lrw.reset_lake_rust_waved_for_tests()
    yield
    lrw.reset_lake_rust_waved_for_tests()


#: The frame the fake crate serves (any recognisable frame works).
_CRATE_DF = pl.DataFrame({"ts_utc": [1], "close": [105.0]})

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

    Carries the FULL endgame :data:`brutex.data.lake_rust_waved._CRATE_API`
    surface (the all-or-nothing import guard incl. the W-6a
    ``waved_endgame_witness`` lockstep tooth — a fake missing any name must
    raise :class:`LakeRustWavedUnavailableError`, never half-work).
    """
    module = types.ModuleType("brutex_lake")
    module.pinned_polars_version = (  # type: ignore[attr-defined]
        pinned if callable(pinned) else (lambda: pinned)
    )
    if read is None:
        read = lambda files, root, oi, screen: (_ipc(_CRATE_DF), "[]")  # noqa: E731
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
    module.waved_heal_engaged_kinds = lambda: [  # type: ignore[attr-defined]
        "IntraFileDup",
        "OptionIntraFileDup",
        "SchemaAlienFile",
        "ForeignMonthRows",
        "OptionForeignMonthRows",
        "LegacyKeyDrift",
    ]
    # The W-6a endgame lockstep tooth: a pre-endgame wheel lacks this.
    module.waved_endgame_witness = lambda: True  # type: ignore[attr-defined]
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


def _read(
    monkeypatch: pytest.MonkeyPatch,
    module: types.ModuleType | None,
    *,
    files: list[str] | None = None,
    tree_root: str | None = None,
    screen_enabled: bool = True,
    warn_source: str | None = "prebuild:NSE/INDEX/NIFTY/1minute",
) -> pl.DataFrame:
    _install_crate(monkeypatch, module)
    return lrw.read_1m_via_rust(
        files=["2024/01.parquet"] if files is None and tree_root is None else files,
        tree_root=tree_root,
        with_open_interest=False,
        screen_enabled=screen_enabled,
        warn_source=warn_source,
        context="NSE/INDEX/NIFTY 1minute",
    )


# ---------------------------------------------------------------------------
# the retired flag (the $BRUTEX_FILL_MODE precedent)
# ---------------------------------------------------------------------------


class TestRetiredFlag:
    @pytest.mark.parametrize("raw", ["1", "0", "true", "false", " on ", "junk"])
    def test_any_set_value_refuses_loudly(self, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
        # '1' and '0' INCLUDED: the seam is no longer flag-gated, so either
        # spelling is a stale config whose intent no longer exists.
        monkeypatch.setenv(lrw.LAKE_WAVED_RUST_ENV, raw)
        with pytest.raises(LakeRustWavedFlagRemovedError, match="no longer flag-gated"):
            lrw.require_waved_env_unset()

    @pytest.mark.parametrize("raw", [None, "", "   "], ids=["unset", "empty", "whitespace"])
    def test_unset_and_empty_pass(self, monkeypatch: pytest.MonkeyPatch, raw: str | None) -> None:
        if raw is None:
            monkeypatch.delenv(lrw.LAKE_WAVED_RUST_ENV, raising=False)
        else:
            monkeypatch.setenv(lrw.LAKE_WAVED_RUST_ENV, raw)
        lrw.require_waved_env_unset()  # must not raise

    def test_injected_env_mapping_is_honoured(self) -> None:
        with pytest.raises(LakeRustWavedFlagRemovedError):
            lrw.require_waved_env_unset(env={lrw.LAKE_WAVED_RUST_ENV: "1"})
        lrw.require_waved_env_unset(env={})

    def test_set_flag_refuses_at_every_surface(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The guard fires INSIDE _require_crate, so every surface entry
        # refuses BEFORE any crate/lake work — even with a healthy fake.
        monkeypatch.setenv(lrw.LAKE_WAVED_RUST_ENV, "1")
        _install_crate(monkeypatch, _fake_crate())
        with pytest.raises(LakeRustWavedFlagRemovedError):
            lrw.read_1m_via_rust(
                files=["f"],
                tree_root=None,
                with_open_interest=False,
                screen_enabled=True,
                warn_source=None,
                context="ctx",
            )
        with pytest.raises(LakeRustWavedFlagRemovedError):
            lrw.heal_batch_via_rust(
                lake_root="/lake",
                quarantine_root="/q",
                findings=[],
                on_event=lambda kind, payload: None,
            )
        with pytest.raises(LakeRustWavedFlagRemovedError):
            lrw.lake_content_token_via_rust(lake_root="/lake", on_skip=lambda e, p, err: None)
        with pytest.raises(LakeRustWavedFlagRemovedError):
            lrw.reconcile_file_via_rust(path="/lake/f.parquet", coords=None)
        with pytest.raises(LakeRustWavedFlagRemovedError):
            _scan_call(monkeypatch, _fake_crate(), set_env=False)


# ---------------------------------------------------------------------------
# module purity (importing the facade never imports the crate)
# ---------------------------------------------------------------------------


class TestModulePurity:
    def test_facade_import_never_imports_the_crate(self) -> None:
        # Re-import the facade fresh with the crate evicted: importing the
        # MODULE must not import brutex_lake (only a surface CALL may).
        import importlib

        sys.modules.pop("brutex_lake", None)
        importlib.reload(sys.modules["brutex.data.lake_rust_waved"])
        assert "brutex_lake" not in sys.modules
        # Restore a clean view for the sibling tests (reload minted fresh
        # module-level state; the autouse fixture resets the reloaded one).
        lrw.reset_lake_rust_waved_for_tests()


# ---------------------------------------------------------------------------
# the fail-LOUD precondition chain (_require_crate)
# ---------------------------------------------------------------------------


class TestRequireCrate:
    def test_absent_wheel_raises_unavailable_naming_the_remedy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_crate(monkeypatch, None)
        with pytest.raises(LakeRustWavedUnavailableError, match="pip install rust/brutex-lake"):
            _read(monkeypatch, None)

    @pytest.mark.parametrize("missing", list(lrw._CRATE_API))
    def test_missing_api_callable_raises_unavailable(
        self, monkeypatch: pytest.MonkeyPatch, missing: str
    ) -> None:
        # The all-or-nothing lockstep: `waved_endgame_witness` is the W-6a
        # tooth — a PRE-endgame wheel (whose heal batch could still return a
        # whole-batch degrade this shell has no oracle for) must refuse
        # wholesale, never half-work.
        crate = _fake_crate()
        delattr(crate, missing)
        _install_crate(monkeypatch, crate)
        with pytest.raises(LakeRustWavedUnavailableError, match=missing):
            _read(monkeypatch, crate)

    def test_non_callable_api_member_raises_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        crate = _fake_crate()
        crate.waved_read_1m_ipc = "not callable"  # type: ignore[attr-defined]
        with pytest.raises(LakeRustWavedUnavailableError, match="not callable"):
            _read(monkeypatch, crate)

    def test_raising_metadata_raises_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom() -> str:
            raise RuntimeError("metadata exploded")

        with pytest.raises(LakeRustWavedUnavailableError, match="cannot be trusted"):
            _read(monkeypatch, _fake_crate(pinned=_boom))

    def test_metadata_memory_error_re_raises_raw(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _oom() -> str:
            raise MemoryError

        with pytest.raises(MemoryError):
            _read(monkeypatch, _fake_crate(pinned=_oom))

    def test_polars_version_mismatch_raises_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with pytest.raises(LakeRustWavedUnavailableError, match="lockstep is broken"):
            _read(monkeypatch, _fake_crate(pinned="0.0.1"))

    def test_none_pinned_version_fails_the_version_guard(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with pytest.raises(LakeRustWavedUnavailableError, match="lockstep is broken"):
            _read(monkeypatch, _fake_crate(pinned=lambda: None))

    def test_unavailable_error_is_a_data_lake_error(self) -> None:
        from brutex.data.errors import DataLakeError

        assert issubclass(LakeRustWavedUnavailableError, DataLakeError)
        assert issubclass(LakeRustWavedFlagRemovedError, DataLakeError)


# ---------------------------------------------------------------------------
# the read_1m surface — typed quarantining isolation
# ---------------------------------------------------------------------------


class TestRead1mSurface:
    def test_happy_path_serves_the_crate_frame(self, monkeypatch: pytest.MonkeyPatch) -> None:
        out = _read(monkeypatch, _fake_crate())
        assert out.equals(_CRATE_DF)

    def test_crate_value_error_raises_resample_input_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The ruled endgame semantics: every crate failure class isolates
        # the SYMBOL at the caller's quarantine boundary (the prebuild's
        # per-symbol ResampleInputError machinery) — never a run abort.
        def _raise(*_args: object) -> tuple[bytes, str]:
            raise ValueError("gate refusal: price-insane row")

        with pytest.raises(ResampleInputError, match="NSE/INDEX/NIFTY 1minute"):
            _read(monkeypatch, _fake_crate(read=_raise))

    def test_panic_exception_raises_resample_input_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _FakePanicException(BaseException):
            pass

        panic_module = types.ModuleType("pyo3_runtime")
        panic_module.PanicException = _FakePanicException  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "pyo3_runtime", panic_module)

        def _panic(*_args: object) -> tuple[bytes, str]:
            raise _FakePanicException("thread panicked")

        with pytest.raises(ResampleInputError, match="refused by the native lake core"):
            _read(monkeypatch, _fake_crate(read=_panic))

    @pytest.mark.parametrize("exc", [KeyboardInterrupt, SystemExit, MemoryError])
    def test_escape_set_re_raises_raw(
        self, monkeypatch: pytest.MonkeyPatch, exc: type[BaseException]
    ) -> None:
        def _raise(*_args: object) -> tuple[bytes, str]:
            raise exc

        with pytest.raises(exc):
            _read(monkeypatch, _fake_crate(read=_raise))

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
    def test_misshaped_result_is_refuse_poison_loud(
        self, monkeypatch: pytest.MonkeyPatch, result: object
    ) -> None:
        with pytest.raises(RuntimeError, match="refuse-poison"):
            _read(monkeypatch, _fake_crate(read=lambda *a: result))

    def test_undecodable_ipc_payload_is_refuse_poison_loud(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with pytest.raises(RuntimeError, match="undecodable IPC payload"):
            _read(monkeypatch, _fake_crate(read=lambda *a: (b"not ipc bytes", "[]")))

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
    def test_poisoned_events_channel_is_refuse_poison_loud(
        self, monkeypatch: pytest.MonkeyPatch, events_json: str
    ) -> None:
        with pytest.raises(RuntimeError, match="poisoned events channel"):
            _read(monkeypatch, _fake_crate(read=lambda *a: (_ipc(_CRATE_DF), events_json)))

    def test_replays_bad_tick_events_via_the_one_seam(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with capture_logs() as logs:
            _read(
                monkeypatch,
                _fake_crate(read=lambda *a: (_ipc(_CRATE_DF), _ONE_EVENT_JSON)),
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

    def test_empty_events_emit_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
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

    @pytest.mark.parametrize(
        ("files", "tree_root"),
        [(None, None), (["f"], "/root")],
        ids=["neither", "both"],
    )
    def test_invalid_source_arm_combination_is_refuse_poison(
        self,
        monkeypatch: pytest.MonkeyPatch,
        files: list[str] | None,
        tree_root: str | None,
    ) -> None:
        # A caller bug (the files-XOR-tree_root seam contract) is loud —
        # never a guessed read; the crate is never called.
        def _bomb(*_args: object) -> tuple[bytes, str]:
            raise AssertionError("crate must not be called on invalid args")

        _install_crate(monkeypatch, _fake_crate(read=_bomb))
        with pytest.raises(RuntimeError, match="exactly one of files/tree_root"):
            lrw.read_1m_via_rust(
                files=files,
                tree_root=tree_root,
                with_open_interest=False,
                screen_enabled=True,
                warn_source=None,
                context="ctx",
            )

    def test_tree_root_arm_passes_empty_files_list(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: dict[str, object] = {}

        def _capture(
            files: list[str], root: str | None, oi: bool, screen: bool
        ) -> tuple[bytes, str]:
            seen.update(files=files, root=root, oi=oi, screen=screen)
            return (_ipc(_CRATE_DF), "[]")

        _install_crate(monkeypatch, _fake_crate(read=_capture))
        lrw.read_1m_via_rust(
            files=None,
            tree_root="/lake/bars/NSE/INDEX/NIFTY/1minute",
            with_open_interest=True,
            screen_enabled=False,
            warn_source=None,
            context="ctx",
        )
        assert seen == {
            "files": [],
            "root": "/lake/bars/NSE/INDEX/NIFTY/1minute",
            "oi": True,
            "screen": False,
        }


# ── W-2: the heal-batch surface ─────────────────────────────────────────────


def _heal_call(
    monkeypatch: pytest.MonkeyPatch,
    module: types.ModuleType | None,
    *,
    findings: list[dict[str, Any]] | None = None,
    on_event: Any = None,
) -> dict[str, Any]:
    _install_crate(monkeypatch, module)
    return lrw.heal_batch_via_rust(
        lake_root="/lake",
        quarantine_root="/lake/.quarantine/x",
        findings=[] if findings is None else findings,
        on_event=(lambda kind, payload: None) if on_event is None else on_event,
    )


class TestHealBatchSurface:
    """The §B2 heal seam — everything is LOUD (mutation may have happened)."""

    def test_absent_crate_raises_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with pytest.raises(LakeRustWavedUnavailableError):
            _heal_call(monkeypatch, None)

    def test_missing_w2_api_raises_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        crate = _fake_crate()
        delattr(crate, "waved_heal_batch")
        with pytest.raises(LakeRustWavedUnavailableError):
            _heal_call(monkeypatch, crate)

    def test_stale_degrade_verdict_is_refuse_poison_loud(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A pre-endgame wheel's whole-batch degrade — structurally
        # unreachable behind the waved_endgame_witness guard; a wheel that
        # carries the witness AND still degrades is poison, never guessed at.
        crate = _fake_crate(heal=lambda *a: {"degrade": "unported_algorithm: X"})
        with pytest.raises(RuntimeError, match="stale whole-batch degrade"):
            _heal_call(monkeypatch, crate)

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
        # An ENOSPC crosses RAW — the caller's lake_write_guard owns the
        # LakeWriteHalt mapping.
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
) -> str:
    _install_crate(monkeypatch, module)
    return lrw.lake_content_token_via_rust(
        lake_root="/lake",
        on_skip=(lambda e, p, err: None) if on_skip is None else on_skip,
    )


class TestContentTokenSurface:
    """The §C-5 token twin — crate-only, skip events replayed."""

    def test_absent_crate_raises_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with pytest.raises(LakeRustWavedUnavailableError):
            _token_call(monkeypatch, None)

    def test_crate_exception_propagates_raw(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The startup sweep's _SWEEP_GUARD_ERRORS own the never-crash-boot
        # law — the facade adds NO catch of its own.
        def _boom(lake_root: str) -> tuple[str, list[Any]]:
            raise RuntimeError("crate fault")

        with pytest.raises(RuntimeError, match="crate fault"):
            _token_call(monkeypatch, _fake_crate(token=_boom))

    def test_memory_error_escapes_raw(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _oom(lake_root: str) -> tuple[str, list[Any]]:
            raise MemoryError

        with pytest.raises(MemoryError):
            _token_call(monkeypatch, _fake_crate(token=_oom))

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
    def test_refuse_poison_shapes_raise_loud(
        self, monkeypatch: pytest.MonkeyPatch, result: Any
    ) -> None:
        with pytest.raises(RuntimeError, match="refuse-poison"):
            _token_call(monkeypatch, _fake_crate(token=lambda lr: result))

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
    def test_poisoned_skip_channel_raises_loud(
        self, monkeypatch: pytest.MonkeyPatch, item: Any
    ) -> None:
        with pytest.raises(RuntimeError, match="poisoned skip channel"):
            _token_call(monkeypatch, _fake_crate(token=lambda lr: ("t" * 64, [item])))

    def test_poisoned_skip_channel_replays_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The whole skip list is validated BEFORE any replay — a poisoned
        # tail never half-replays the head.
        replayed: list[tuple[str, str, str]] = []
        with pytest.raises(RuntimeError):
            _token_call(
                monkeypatch,
                _fake_crate(
                    token=lambda lr: (
                        "tok",
                        [
                            {"event": "lake_heal.token_stat_skipped", "path": "/p", "error": "e"},
                            "poison",
                        ],
                    )
                ),
                on_skip=lambda e, p, err: replayed.append((e, p, err)),
            )
        assert replayed == []

    def test_token_served_with_skips_replayed_in_order(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
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
        token = _token_call(
            monkeypatch, crate, on_skip=lambda e, p, err: replayed.append((e, p, err))
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
    """The scan tuple matching :data:`_RECON_CLEAN_WIRE` (the expected build)."""
    from datetime import UTC, datetime, timedelta

    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    return (
        2,
        epoch + timedelta(microseconds=_JUNE_US),
        epoch + timedelta(microseconds=_JUNE_US + 60_000_000),
        "a" * 64,
        findings,
    )


def _recon_call(
    monkeypatch: pytest.MonkeyPatch,
    module: types.ModuleType | None,
    *,
    coords: Any = None,
) -> Any:
    _install_crate(monkeypatch, module)
    return lrw.reconcile_file_via_rust(
        path="/lake/bars/NSE/INDEX/NIFTY/1minute/2024/06.parquet",
        coords=coords,
    )


class TestReconcileFileSurface:
    """The §B3/§C-3 per-file scan — ValueError joins the GAP-#9 skip set."""

    def test_absent_crate_raises_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with pytest.raises(LakeRustWavedUnavailableError):
            _recon_call(monkeypatch, None)

    def test_missing_w3_api_raises_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The all-or-nothing lockstep: a W-2-era wheel (no waved_reconcile_file)
        # is a stale install — the WHOLE waved seam refuses.
        module = _fake_crate()
        delattr(module, "waved_reconcile_file")
        with pytest.raises(LakeRustWavedUnavailableError):
            _recon_call(monkeypatch, module)

    def test_clean_wire_builds_the_scan_tuple(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert _recon_call(monkeypatch, _fake_crate()) == _recon_scan()

    def test_crate_error_raises_value_error_naming_the_file(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The ruled endgame semantics: ValueError is a member of the
        # caller's GAP-#9 skip set (datetime_out_of_range_errors), so ONE
        # poisoned file is a loud counted catalog.reconcile.parquet_skip —
        # never a crashed reconcile.
        from brutex.data.errors import datetime_out_of_range_errors

        def _boom(path: str, home: Any) -> Any:
            raise ValueError("brutex-lake waved reconcile failed: corrupt")

        with pytest.raises(ValueError, match="06.parquet") as caught:
            _recon_call(monkeypatch, _fake_crate(reconcile=_boom))
        assert isinstance(caught.value, datetime_out_of_range_errors())

    def test_panic_exception_raises_value_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import polars.exceptions as pl_exc

        def _panic(path: str, home: Any) -> Any:
            raise pl_exc.PanicException("boom")

        with pytest.raises(ValueError, match="native reconcile scan refused"):
            _recon_call(monkeypatch, _fake_crate(reconcile=_panic))

    def test_memory_error_escapes_raw(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _oom(path: str, home: Any) -> Any:
            raise MemoryError

        with pytest.raises(MemoryError):
            _recon_call(monkeypatch, _fake_crate(reconcile=_oom))

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
    def test_refuse_poison_shapes_raise_loud(
        self, monkeypatch: pytest.MonkeyPatch, wire: Any
    ) -> None:
        # A mis-shaped crate RETURN is a structurally broken wheel — it must
        # crash the reconcile LOUDLY (RuntimeError is NOT in the GAP-#9 skip
        # set), never skip the whole lake into an empty catalog.
        with pytest.raises(RuntimeError, match="refuse-poison"):
            _recon_call(monkeypatch, _fake_crate(reconcile=lambda p, h: wire))

    def test_zero_row_none_passes_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert _recon_call(monkeypatch, _fake_crate(reconcile=lambda p, h: None)) is None

    def test_findings_reconstruction_matches_classify_law(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Dup + foreign counts rebuild the finding OBJECTS — relpath-stamped,
        # coords-carrying, dup-then-foreign order, overlap_rows=None (the
        # free L1 path).
        from pathlib import Path

        from brutex.data.lake_invariants import ForeignMonthRows, IntraFileDup

        coords = _recon_coords()
        wire = (3, _JUNE_US, _JUNE_US + 60_000_000, True, "a" * 64, (3, 2), 1)
        result = _recon_call(monkeypatch, _fake_crate(reconcile=lambda p, h: wire), coords=coords)
        assert result is not None
        rel = Path(coords.relpath())
        assert result[4] == (
            IntraFileDup(path=rel, coords=coords, physical_rows=3, unique_keys=2),
            ForeignMonthRows(path=rel, coords=coords, foreign_rows=1, overlap_rows=None),
        )

    def test_no_coords_builds_no_findings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # check_invariants=False: even a count-carrying wire builds NOTHING
        # (the classification is the caller's choice, not the crate's).
        wire = (3, _JUNE_US, _JUNE_US + 60_000_000, True, "a" * 64, (3, 2), 1)
        result = _recon_call(monkeypatch, _fake_crate(reconcile=lambda p, h: wire), coords=None)
        assert result is not None
        assert result[4] == ()

    def test_naive_column_builds_naive_datetimes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from datetime import datetime, timedelta

        wire = (2, _JUNE_US, _JUNE_US + 60_000_000, False, "a" * 64, None, 0)
        result = _recon_call(monkeypatch, _fake_crate(reconcile=lambda p, h: wire))
        assert result is not None
        expected = datetime(1970, 1, 1) + timedelta(microseconds=_JUNE_US)
        assert result[1] == expected
        assert result[1].tzinfo is None and result[2].tzinfo is None

    def test_arguments_cross_verbatim(self, monkeypatch: pytest.MonkeyPatch) -> None:
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


def _scan_call(
    monkeypatch: pytest.MonkeyPatch,
    module: types.ModuleType | None,
    *,
    months: Any = None,
    include_bars: bool = True,
    include_options: bool = True,
    canonical_keys: Any = None,
    per_file_pause: Any = None,
    set_env: bool = True,
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

    def test_include_bars_false_skips_the_drift_fold(self) -> None:
        # An options-only wire (no drift marker, drift_insert_at=0) builds
        # the report with the Python drift fold SKIPPED entirely.
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
            events=[{"kind": "finding", "index": 0}],
            option_files_scanned=1,
            symbol_dirs=[("NSE", "CASH", ["M&M", "NSE-M&M"])],
        )
        report, _events, drift_at, drift_len, _reads = lrw._scan_report_from_wire(
            wire, lake_root=Path("/l"), canonical_keys=None, include_bars=False
        )
        assert drift_at == 0 and drift_len == 0
        assert len(report.findings) == 1  # the drifted dir pair folded NOTHING

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
    """The routed whole-lake scan — crate-only, isolation crate-owned."""

    def test_absent_crate_raises_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with pytest.raises(LakeRustWavedUnavailableError):
            _scan_call(monkeypatch, None)

    def test_crate_raise_propagates_raw(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Isolation is CRATE-OWNED (per-file loud counted scan_skip inside
        # the report) — a crate-call raise is the structurally-unreachable
        # defensive belt, propagated RAW.
        def _boom(*_args: Any) -> Any:
            raise ValueError("brutex-lake waved scan failed: poison")

        with pytest.raises(ValueError, match="waved scan failed"):
            _scan_call(monkeypatch, _fake_crate(scan=_boom))

    def test_escape_errors_reraise(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _interrupt(*_args: Any) -> Any:
            raise KeyboardInterrupt

        with pytest.raises(KeyboardInterrupt):
            _scan_call(monkeypatch, _fake_crate(scan=_interrupt))

    def test_refused_wire_is_refuse_poison_loud(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with pytest.raises(RuntimeError, match="refuse-poison"):
            _scan_call(monkeypatch, _fake_crate(scan=lambda *a: {"findings": "junk"}))

    def test_serves_the_crate_report_with_replay(self, monkeypatch: pytest.MonkeyPatch) -> None:
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
        with capture_logs() as logs:
            report = _scan_call(monkeypatch, _fake_crate(scan=lambda *a: wire))
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
        marker = RuntimeError("throttle exploded")

        def _pause() -> None:
            raise marker

        def _tick_once(*args: Any) -> Any:
            args[13]()  # the wrapped pause callback
            raise AssertionError("unreachable — the wrap raises")

        with pytest.raises(RuntimeError) as caught:
            _scan_call(monkeypatch, _fake_crate(scan=_tick_once), per_file_pause=_pause)
        assert caught.value is marker

    def test_pause_escape_without_capture_is_defensive(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _fake_escape(*_args: Any) -> Any:
            raise lrw._PauseEscape

        with pytest.raises(RuntimeError, match="pause escape without a captured exception"):
            _scan_call(
                monkeypatch,
                _fake_crate(scan=_fake_escape),
                per_file_pause=lambda: None,
            )

    def test_reset_clears_the_reads_counter(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _scan_call(monkeypatch, _fake_crate(scan=lambda *a: _scan_clean_wire(reads_performed=9)))
        assert lrw.last_rust_scan_reads() == 9
        lrw.reset_lake_rust_waved_for_tests()
        assert lrw.last_rust_scan_reads() is None
